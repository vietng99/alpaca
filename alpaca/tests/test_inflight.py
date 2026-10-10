"""t-157: the In-flight tab. Lease filtering, the plan around a task, the claim-time window, progress
(measured, estimated, none), the rule-based narrative (feed, mode, test trend, the incremental
reader) and the "Open in-flight" link, on synthetic records and transcripts."""
import json
import shutil
import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

from alpaca import db, taskplan, transcripts
from alpaca.analytics import inflight

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc).timestamp()
WEB = Path(__file__).resolve().parents[1] / "web"
NODE = shutil.which("node")


def iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


def zulu(epoch):
    return iso(epoch).replace("+00:00", "Z")


def dump(path, records, mode="w"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records))
    return str(path)


def task(root, tid, status="doing", op="op-1", claimant="claude", lease=NOW + 3600, title=None):
    conn = db.connect(root)
    db.upsert(conn, "tasks", "id", {"id": tid, "op": op, "phase": "build", "statement": title or "Task " + tid,
                                     "title": title or "Task " + tid, "status": status, "claimant": claimant,
                                     "lease_until": iso(lease) if lease else None,
                                     "created": "2026-10-01T00:00:00+00:00", "updated": "2026-10-01T00:00:00+00:00"})
    conn.close()


def event(root, kind, at, session=None, ref=None, op="op-1", data=None):
    conn = db.connect(root)
    db.append_event(conn, session=session, actor="agent", kind=kind, ref=ref, op=op, data=data or {},
                    clock=lambda: iso(at))
    conn.close()


def session(root, sid, records):
    conn = db.connect(root)
    db.upsert(conn, "sessions", "sid", {"sid": sid, "started": "2026-10-01T00:00:00Z"})
    conn.close()
    return dump(transcripts.local_path(root, sid), records)


def resp(ident, at, blocks=None, inp=100, out=10):
    return {"type": "assistant", "timestamp": zulu(at),
            "message": {"id": ident, "model": "claude-opus-5", "content": blocks or [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": 0,
                                  "cache_creation_input_tokens": 0}}}


def use(ident, tool, **args):
    return {"type": "tool_use", "id": ident, "name": tool, "input": args}


def result(ident, at, text="", error=False):
    return {"type": "user", "timestamp": zulu(at),
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": ident, "is_error": error,
                                                     "content": text}]}}


def snap(root, **kw):
    return inflight.snapshot(root, now=NOW, **kw)


# ---- which tasks are in flight -----------------------------------------------------------------

def test_only_doing_tasks_with_a_live_lease_are_in_flight(project):
    task(project, "t-001")                                      # live
    task(project, "t-002", lease=NOW - 60)                      # lease expired
    task(project, "t-003", claimant=None)                       # nobody holds it
    task(project, "t-004", status="done")
    task(project, "t-005", lease=None)
    for t in ("t-001", "t-002", "t-003", "t-004", "t-005"):
        event(project, "claim", NOW - 600, "s1", t)
    d = snap(project)
    assert [c["id"] for c in d["tasks"]] == ["t-001"] and d["count"] == 1


def test_prerequisites_successors_and_neighbours_come_from_the_plan(project):
    for t, status in (("t-010", "done"), ("t-011", "doing"), ("t-012", "ready"), ("t-013", "ready")):
        task(project, t, status=status)
    event(project, "claim", NOW - 600, "s1", "t-011")
    plan = {"version": 1, "revision": 0, "groups": [{"id": "core", "title": "Core work"}],
            "tasks": [{"id": i, "group": "core", "after": after, "resources": [], "requires_stages": [], "hold": None}
                      for i, after in (("t-010", []), ("t-011", ["t-010"]), ("t-012", ["t-011"]), ("t-013", []))]}
    event(project, taskplan.KIND, NOW - 900, "s1", None, data=plan)
    c = snap(project)["tasks"][0]
    assert [(p["id"], p["status"]) for p in c["prerequisites"]] == [("t-010", "done")]
    assert [s["id"] for s in c["successors"]] == ["t-012"]
    assert c["stream_prev"]["id"] == "t-010" and c["stream_next"]["id"] == "t-012"
    assert c["workstream"] == {"id": "core", "title": "Core work"}


# ---- the claim-time window ---------------------------------------------------------------------

def test_activity_counts_only_what_the_session_did_after_the_claim(project):
    task(project, "t-020")
    session(project, "s1", [
        resp("before", NOW - 3000, [use("b1", "Bash", command="ls", description="List before the claim")]),
        resp("after1", NOW - 500, [{"type": "text", "text": "Starting the board."}, use("a1", "Edit", file_path="/p/x.py")]),
        resp("after2", NOW - 400, [use("a2", "Bash", command="pytest -q", description="Run the tests")]),
        result("a2", NOW - 390, "Exit code 1\n3 passed, 2 failed in 1.2s", error=True)])
    event(project, "claim", NOW - 1000, "s1", "t-020")
    c = snap(project)["tasks"][0]
    assert c["responses"] == 2 and c["tool_calls"] == 2 and c["total_tokens"] == 220
    assert c["now"] == "Starting the board." and c["running_s"] == 1000
    assert [l["text"] for l in c["feed"]] == ["Run the tests (exit 1, 3 passed, 2 failed)", "Edited /p/x.py"]
    assert [m["label"] for m in c["milestones"] if m["ts"]] == ["Claimed"]


# ---- progress ----------------------------------------------------------------------------------

def run_record(start, states, status="running"):
    agents = [{"type": "workflow_agent", "index": i, "label": "a%d" % i, "phaseIndex": 1 if i < 2 else 2,
               "agentId": "ag%d" % i, "model": "claude-opus-5", "state": s, "queuedAt": start * 1000,
               "startedAt": start * 1000, "attempt": 1} for i, s in enumerate(states)]
    return {"runId": "wf_1", "workflowName": "build", "status": status, "startTime": start * 1000, "agentCount": 5,
            "phases": [{"title": "Build"}, {"title": "Check"}], "workflowProgress": agents}


def test_progress_is_measured_from_an_active_workflow_run(project, tmp_path):
    task(project, "t-030")
    base = tmp_path / "native"
    main = dump(base / "s1.jsonl", [resp("m", NOW - 100)])
    conn = db.connect(project)
    db.upsert(conn, "sessions", "sid", {"sid": "s1", "started": "2026-10-01T00:00:00Z", "transcript": main})
    conn.close()
    (base / "s1" / "workflows").mkdir(parents=True)
    (base / "s1" / "workflows" / "wf_1.json").write_text(json.dumps(run_record(NOW - 300, ["done", "error", "progress", "progress"])))
    event(project, "claim", NOW - 600, "s1", "t-030")
    p = snap(project)["tasks"][0]["progress"]
    assert p["basis"] == "measured" and (p["done"], p["total"]) == (2, 5) and p["percent"] == 40.0
    assert [(x["phase"], x["done"], x["total"], x["failed"]) for x in p["phases"]] == [("Build", 2, 2, 1), ("Check", 0, 2, 0)]


def test_progress_estimates_from_similar_done_tasks_and_is_capped(project):
    for t, took in (("t-040", 1000), ("t-041", 3000)):
        task(project, t, status="done")
        event(project, "claim", NOW - 50000, "s9", t)
        event(project, "task-move", NOW - 50000 + took, "s9", t, data={"from": "doing", "to": "done"})
    task(project, "t-042")
    event(project, "claim", NOW - 1000, "s1", "t-042")
    p = snap(project)["tasks"][0]["progress"]
    assert p["basis"] == "estimate" and p["n"] == 2 and p["median_s"] == 2000 and p["percent"] == 50.0
    assert "estimate from 2 similar tasks" in p["label"]
    assert inflight.estimate(10000, [1000, 3000], "op")["percent"] == inflight.CAP


def test_no_basis_for_a_percentage(project):
    task(project, "t-050")
    event(project, "claim", NOW - 1000, "s1", "t-050")
    p = snap(project)["tasks"][0]["progress"]
    assert p == {"basis": "none", "percent": None, "label": "no basis for a percentage"}
    assert inflight.estimate(100, [500], "op") is None                     # one similar task is not enough


# ---- the narrative, by rule --------------------------------------------------------------------

def events(records):
    out = []
    for r in records:
        out.extend(inflight.parse_line(json.dumps(r).encode()))
    return out


def test_feed_rules_for_each_tool():
    t = NOW
    ev = events([
        resp("1", t, [use("r1", "Read", file_path="/root/a/x.py"), use("r2", "Grep", path="/root/a/y.py", pattern="foo"),
                      use("r3", "Glob", path="/root/a/z.py")]),
        resp("2", t + 1, [use("e1", "Edit", file_path="/root/a/x.py"), use("w1", "Write", file_path="/root/b/new.md")]),
        resp("3", t + 2, [use("b1", "Bash", command="make lint\nmore", description="")]),
        result("b1", t + 3, "ok"),
        resp("4", t + 4, [use("g1", "Agent", description="Review the diff", subagent_type="reviewer")]),
        result("g1", t + 9, "done"),
        resp("5", t + 10, [use("m1", "SendMessage", to="worker-2", message="hi"), use("q1", "AskUserQuestion", questions=[])]),
        resp("6", t + 11, [use("f1", "Workflow", name="op-build")])])
    got = [l["text"] for l in sorted(inflight.feed(ev, "/root"), key=lambda l: (l["ts"], l["text"]))]
    assert got == ["Read 3 files in a", "Edited a/x.py", "Wrote b/new.md", "make lint (exit 0)",
                   "Started agent: Review the diff", "Agent finished: Review the diff",
                   "Asked the owner", "Messaged worker-2", "Started workflow op-build"]
    assert len(inflight.feed(ev * 30, "/root", limit=60)) == 60


def test_no_tool_output_body_or_thinking_is_kept():
    ev = events([{"type": "assistant", "timestamp": zulu(NOW), "message": {"id": "x", "model": "m", "content": [
                    {"type": "thinking", "thinking": "secret plan"}, {"type": "text", "text": "Visible note"},
                    use("b", "Bash", command="cat .env", description="Read config")]}},
                 result("b", NOW + 1, "API_KEY=sk-123 and 4 passed")])
    blob = json.dumps(ev)
    assert "secret plan" not in blob and "sk-123" not in blob and "Visible note" in blob
    assert ev[-1]["passed"] == 4


def test_mode_detection():
    reads = events([resp(str(i), NOW + i, [use("r%d" % i, "Read", file_path="/a/%d" % i)]) for i in range(8)])
    assert inflight.mode(reads)["mode"] == "exploring"
    edits = reads + events([resp("e%d" % i, NOW + 20 + i, [use("e%d" % i, "Edit", file_path="/a/x")]) for i in range(10)])
    assert inflight.mode(edits)["mode"] == "editing"
    tests = edits + events([resp("t%d" % i, NOW + 40 + i, [use("t%d" % i, "Bash", command="pytest -q")]) for i in range(12)])
    assert inflight.mode(tests)["mode"] == "testing"
    asked = tests + events([resp("q", NOW + 60, [use("q", "AskUserQuestion")])])
    assert inflight.mode(asked)["mode"] == "waiting on the owner"
    stopped = tests + events([{"type": "system", "subtype": "stop_hook_summary", "timestamp": zulu(NOW + 70)}])
    assert inflight.mode(stopped)["mode"] == "waiting on the owner"
    assert inflight.mode(tests, workflow_running=True)["mode"] == "running a workflow"


def test_test_trend_parsing():
    def run(i, text, error):
        return [resp("t%d" % i, NOW + i, [use("t%d" % i, "Bash", command="python -m pytest -q tests")]),
                result("t%d" % i, NOW + i, text, error)]
    converging = events(run(1, "Exit code 1\n10 passed, 5 failed", True) + run(2, "Exit code 1\n13 passed, 2 failed", True))
    trend = inflight.test_trend(converging)
    assert [(r["passed"], r["failed"], r["ok"]) for r in trend["runs"]] == [(10, 5, False), (13, 2, False)]
    assert trend["verdict"] == "converging"
    assert inflight.test_trend(converging + events(run(3, "15 passed in 2s", False)))["verdict"] == "passing"
    assert inflight.test_trend(events(run(4, "Exit code 1\n1 failed", True) + run(5, "Exit code 1\n3 failed", True)))["verdict"] == "failing"
    assert inflight.test_trend([])["verdict"] == "no test runs"


def test_the_tail_reader_parses_only_new_lines(tmp_path, monkeypatch):
    path = dump(tmp_path / "t.jsonl", [resp("1", NOW, [{"type": "text", "text": "first"}])])
    calls = []
    real = inflight.parse_line
    monkeypatch.setattr(inflight, "parse_line", lambda line: calls.append(line) or real(line))
    st = inflight.tail(path)
    assert [e["text"] for e in st["events"]] == ["first"] and len(calls) == 1
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(resp("2", NOW + 1, [{"type": "text", "text": "second"}]), separators=(",", ":")) + "\n")
        fh.write('{"type":"assistant","partial')                        # still being written
    st = inflight.tail(path)
    assert [e["text"] for e in st["events"]] == ["first", "second"] and len(calls) == 2
    assert inflight.tail(path)["end"] == st["end"] and len(calls) == 2  # nothing new: nothing parsed
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('":1}\n')
    inflight.tail(path)
    assert len(calls) == 3
    # A long file is read from its tail; "show earlier" reads the bytes before an offset.
    monkeypatch.setattr(inflight, "TAIL_BYTES", 400)
    big = dump(tmp_path / "big.jsonl", [resp(str(i), NOW + i, [{"type": "text", "text": "note %d" % i}]) for i in range(30)])
    st = inflight.tail(big)
    assert st["start"] > 0 and st["events"][-1]["text"] == "note 29" and len(st["events"]) < 30
    older, start = inflight.earlier(big, st["events"][0]["off"], chunk=10 ** 6)
    assert start == 0 and older[0]["text"] == "note 0" and older[-1]["text"] == "note %d" % (30 - len(st["events"]) - 1)


# ---- the button --------------------------------------------------------------------------------

@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_open_in_flight_links_and_the_count(tmp_path):
    test = tmp_path / "inflight.test.mjs"
    test.write_text(textwrap.dedent("""
        import test from 'node:test';
        import assert from 'node:assert/strict';
        globalThis.document = {addEventListener() {}, querySelector() { return null; }};
        const m = await import(%r);
        const c = await import(%r);
        test('the link names the task', () => {
          assert.equal(m.inflightHref('t-094'), '#inflight?task=t-094');
          assert.match(m.inflightButton('t-094'), /href="#inflight\\?task=t-094"/);
        });
        test('the count follows the live lease', () => {
          const future = new Date(Date.now() + 60000).toISOString(), past = new Date(Date.now() - 60000).toISOString();
          assert.equal(m.inflightCount([{status: 'doing', claimant: 'c', lease_until: future},
            {status: 'doing', claimant: 'c', lease_until: past}, {status: 'done', claimant: 'c', lease_until: future}]), 1);
        });
        test('an in-progress work card carries the button, a ready one does not', () => {
          const html = c.renderWorkCards([{id: 't-094', status: 'doing', title: 'A', op: 'op-1'}, {id: 't-095', status: 'ready', title: 'B', op: 'op-1'}]);
          assert.match(html, /#inflight\\?task=t-094/);
          assert.doesNotMatch(html, /#inflight\\?task=t-095/);
        });
    """ % ((WEB / "inflight.js").as_uri(), (WEB / "cockpit.js").as_uri())), encoding="utf-8")
    got = subprocess.run([NODE, "--test", str(test)], capture_output=True, text=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_hub_wiring_names_the_tab_the_route_and_live_refresh():
    hub = (WEB / "hub.js").read_text(encoding="utf-8")
    pages = {p["id"]: p for p in json.loads((WEB / "pages.json").read_text(encoding="utf-8"))["pages"]}
    assert pages["inflight"]["label"] == "In-flight" and pages["inflight"]["nav"] == pages["cockpit"]["nav"] + 1
    assert pages["inflight"]["needs"] == ["overview"] and pages["inflight"]["quiet"] and pages["inflight"]["timer"]
    assert "inflight:inflightView" in hub and "id==='inflight'" in hub
    assert "${t.status==='doing'?`<p class=\"inflight-row\">${inflightButton(t.id,'button primary')}</p>`:''}" in hub
    # The in-flight marker is the working orb (orb.js); it draws one still frame under reduced motion.
    inflight = (WEB / "inflight.js").read_text(encoding="utf-8")
    assert "orb('working',20)" in inflight
    orb = (WEB / "orb.js").read_text(encoding="utf-8")
    assert "prefers-reduced-motion: reduce" in orb and "reduced() ? 0.6 / speed" in orb


def test_a_running_workflow_is_read_from_its_agent_folders(project, tmp_path):
    task(project, "t-060")
    base = tmp_path / "native"
    main = dump(base / "s1.jsonl", [
        resp("m1", NOW - 300, [use("w1", "Workflow", scriptPath="/x/wf-build.js")]),
        {"type": "user", "timestamp": zulu(NOW - 299), "toolUseResult": {"status": "async_launched", "runId": "wf_live",
                                                                         "workflowName": "op-build"},
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "w1", "content": "launched"}]}}])
    conn = db.connect(project)
    db.upsert(conn, "sessions", "sid", {"sid": "s1", "started": "2026-10-01T00:00:00Z", "transcript": main})
    conn.close()
    folder = base / "s1" / "subagents" / "workflows" / "wf_live"
    dump(folder / "agent-a1.jsonl", [resp("a", NOW - 200, [use("so", "StructuredOutput", verdict="pass")]),
                                     {"type": "attachment", "timestamp": zulu(NOW - 199), "attachment": {"type": "structured_output"}}])
    dump(folder / "agent-a2.jsonl", [resp("b", NOW - 100, [use("r", "Read", file_path="/x")])])
    dump(folder / "agent-a3.jsonl", [resp("c", NOW - 90, [{"type": "text", "text": "done"}])])
    for ident, label in (("a1", "check t-060 r1"), ("a2", "build t-060"), ("a3", "build t-061")):
        (folder / ("agent-%s.meta.json" % ident)).write_text(json.dumps({"description": label, "workflowPhase": "Build"}))
    event(project, "claim", NOW - 600, "s1", "t-060")
    c = inflight.snapshot(project, now=NOW + 1)["tasks"][0]
    live = [w for w in c["workflows"] if w.get("live")]
    assert len(live) == 1 and live[0]["name"] == "op-build" and live[0]["planned"] is None
    assert (live[0]["agents"], live[0]["done"], live[0]["running"]) == (3, 1, 2)   # a3 ended its turn without end_turn
    assert (live[0]["task_agents"], live[0]["task_done"]) == (2, 1)
    assert c["mode"]["mode"] == "running a workflow" and c["progress"]["basis"] == "none"
    assert any(l["text"].startswith("Workflow op-build: 1 of 3 agents") for l in c["feed"])
