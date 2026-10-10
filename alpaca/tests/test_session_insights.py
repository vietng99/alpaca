"""t-145 to t-153: the nine off-track signals on the Sessions board, on synthetic transcripts and records.

Transcript lines are written compact, the way Claude Code writes them, because the reader looks for
compact markers before it parses a line.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from alpaca import db, transcripts
from alpaca.analytics import build_index, burn, insights

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc).timestamp()


def iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def dump(path, records):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records), encoding="utf-8")
    return str(path)


def usage(inp=100, out=10, cr=0, cw=0, cw1=None):
    u = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}
    if cw1 is not None:
        u["cache_creation"] = {"ephemeral_5m_input_tokens": cw - cw1, "ephemeral_1h_input_tokens": cw1}
    return u


def resp(ident, at, tool=None, args=None, tid=None, **u):
    content = [{"type": "tool_use", "id": tid or "tu-" + ident, "name": tool, "input": args or {}}] if tool else \
              [{"type": "text", "text": "ok"}]
    return {"type": "assistant", "timestamp": iso(at),
            "message": {"id": ident, "model": "claude-opus-5", "content": content, "usage": usage(**u)}}


def failed(tid, at, text="exit 1"):
    return {"type": "user", "timestamp": iso(at),
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "is_error": True,
                                                     "content": text}]}}


def prompt(at, text="please continue"):
    return {"type": "user", "timestamp": iso(at), "message": {"role": "user", "content": text}}


def register(root, sid):
    conn = db.connect(root)
    db.upsert(conn, "sessions", "sid", {"sid": sid, "started": "2026-09-30T00:00:00Z"})
    db.meta_set(conn, "operator:" + sid, "claude")
    conn.close()


def session(root, sid, records):
    register(root, sid)
    dump(transcripts.local_path(root, sid), records)


def event(root, kind, at, session=None, ref=None, op=None, data=None):
    conn = db.connect(root)
    db.append_event(conn, session=session, actor="agent", kind=kind, ref=ref, op=op, data=data or {},
                    clock=lambda: datetime.fromtimestamp(at, tz=timezone.utc).isoformat(timespec="seconds"))
    conn.close()


def task(root, tid, op, statement, status="done"):
    conn = db.connect(root)
    db.upsert(conn, "tasks", "id", {"id": tid, "op": op, "phase": "build", "statement": statement, "status": status,
                                     "title": statement[:40], "created": "2026-09-30T00:00:00Z",
                                     "updated": "2026-09-30T00:00:00Z"})
    conn.close()


def board(root, window="24h"):
    return burn.board(root, build_index.collect(root), window, now=NOW)


def signals(root, sid, window="24h"):
    return next(x for x in board(root, window)["sessions"] if x["sid"] == sid)["signals"]


# ---- t-146 loops ----------------------------------------------------------------------------

def call(i, name, key_arg, error=False):
    args = {"command": key_arg} if name == "Bash" else {"file_path": key_arg}
    c = burn._call(name, args, NOW + i, i, i + 1, "c%d" % i)
    return c


def test_loop_rules_fire_on_a_looping_conversation_and_not_on_a_normal_one():
    looping = [call(i, "Bash", "curl -s localhost:8000/health") for i in range(6)]
    looping += [call(10 + i, "Edit", "app/main.py") for i in range(8)]
    fails = []
    for k in range(3):                                   # fail, edit, fail, edit, fail
        fails.append(call(30 + 2 * k, "Bash", "pytest -q tests/test_app.py"))
        fails.append(call(31 + 2 * k, "Edit", "app/other.py"))
    errors = {c["id"]: {"ts": c["ts"], "line": c["line"], "text": "1 failed"} for c in fails if c["name"] == "Bash"}
    flags = insights.loops(looping + fails, errors)
    fired = {(f["rule"], f["count"]) for f in flags}
    assert ("R1", 6) in fired and ("R2", 8) in fired and ("R3", 3) in fired
    r1 = next(f for f in flags if f["rule"] == "R1")
    assert r1["call"] == "Bash: curl -s localhost:8000/health" and r1["within"] == 20
    normal = [call(i, "Bash", "ls step-%d" % i) for i in range(10)] + [call(20 + i, "Edit", "f%d.py" % i) for i in range(5)]
    normal += [call(40, "Bash", "pytest -q"), call(41, "Edit", "a.py"), call(42, "Bash", "pytest -q")]
    assert insights.loops(normal, {normal[-3]["id"]: {"ts": 0, "line": 0, "text": "1 failed"}}) == []
    # Spread out beyond the window, five identical calls are not a loop.
    spread = [burn._call("Bash", {"command": "make"}, NOW, 25 * i, i, "s%d" % i) for i in range(5)]
    assert insights.loops(spread, {}) == []


def test_a_looping_transcript_is_flagged_on_the_board_and_a_normal_one_is_not(project):
    t = NOW - 3600
    loop = []
    for i in range(6):
        loop.append(resp("l%d" % i, t + 60 * i, "Bash", {"command": "pytest -q tests/test_x.py"}, "b%d" % i))
        loop.append(failed("b%d" % i, t + 60 * i + 5, "FAILED tests/test_x.py 1 failed"))
        loop.append(resp("e%d" % i, t + 60 * i + 30, "Edit", {"file_path": "/x/app.py"}, "e%d" % i))
    session(project, "looper", loop)
    session(project, "steady", [resp("s%d" % i, t + 60 * i, "Bash", {"command": "ls dir%d" % i}) for i in range(12)])
    flags = signals(project, "looper")["loops"]
    assert {f["rule"] for f in flags} == {"R1", "R3"} and flags[0]["line"] >= 1
    assert signals(project, "steady")["loops"] == []


# ---- t-147 wasted spend -----------------------------------------------------------------------

def test_wasted_spend_names_killed_failed_retried_and_reopened_work(project, tmp_path):
    base = tmp_path / "native"
    main = dump(base / "w1.jsonl", [resp("m1", NOW - 7000)])
    conn = db.connect(project)
    db.upsert(conn, "sessions", "sid", {"sid": "w1", "started": "2026-09-30T00:00:00Z", "transcript": main})
    conn.close()
    t = NOW - 7200

    def agent(i, ident, state, attempt=1, duration=60000):
        return {"type": "workflow_agent", "index": i, "label": "agent " + ident, "phaseIndex": 1, "agentId": ident,
                "model": "claude-opus-5", "state": state, "queuedAt": t * 1000, "startedAt": t * 1000,
                "attempt": attempt, "durationMs": duration}
    record = {"runId": "wf_k", "workflowName": "build", "status": "killed", "startTime": t * 1000, "durationMs": 600000,
              "phases": [{"title": "Build"}], "workflowProgress": [agent(1, "a1", "error"), agent(2, "a2", "done", 2),
                                                                    agent(3, "a3", "progress", duration=None)]}
    dump(base / "w1" / "workflows" / "wf_k.json", [])
    (base / "w1" / "workflows" / "wf_k.json").write_text(json.dumps(record))
    for ident in ("a1", "a2", "a3"):
        dump(base / "w1" / "subagents" / "workflows" / "wf_k" / ("agent-%s.jsonl" % ident),
             [resp(ident + "r", t + 10, inp=100000)])
    task(project, "t-900", "op-9", "Build the thing")
    event(project, "claim", NOW - 7100, "w1", "t-900", "op-9")
    event(project, "task-move", NOW - 6900, "w1", "t-900", "op-9", {"from": "doing", "to": "done"})
    event(project, "task-move", NOW - 6800, "w1", "t-900", "op-9", {"from": "done", "to": "doing"})
    waste = signals(project, "w1")["waste"]
    causes = {w["cause"] for w in waste["items"]}
    assert causes == {"failed agent", "retried agent, 1 extra attempt", "killed run, agent unfinished", "reopened task"}
    one = next(w for w in waste["items"] if w["cause"] == "failed agent")["cost"]
    retry = next(w for w in waste["items"] if w["cause"].startswith("retried"))["cost"]
    assert one > 0 and retry == pytest.approx(one / 2)                   # attempt 2: half the agent's cost
    assert waste["runs"]["build"] == pytest.approx(one * 2.5)            # failed + unfinished + half a retry
    reopened = next(w for w in waste["items"] if w["cause"] == "reopened task")
    assert reopened["agent"] == "t-900" and reopened["cost"] > 0         # the main response inside the cycle


# ---- t-145 off-task spend ---------------------------------------------------------------------

def test_off_task_spend_names_the_files_outside_the_claimed_task(project):
    root = str(Path(project).resolve())
    t = NOW - 5000
    session(project, "s1", [
        resp("u1", t, "Edit", {"file_path": root + "/docs/before.md"}),              # nothing claimed yet
        resp("c1", t + 200, "Edit", {"file_path": root + "/alpaca/web/burn.js"}),    # the claimed task's file
        resp("o1", t + 400, "Write", {"file_path": root + "/docs/unrelated.md"}),    # off-task
        resp("n1", t + 600, "Edit", {"file_path": root + "/.alpaca/proofs/op-9/t-901.md"}),  # neutral
        resp("x1", t + 800)])                                                         # no target: claimed
    task(project, "t-901", "op-9", "Fix the board in alpaca/web/burn.js")
    event(project, "claim", t + 100, "s1", "t-901", "op-9")
    off = signals(project, "s1")["offtask"]
    one = burn._tokens({"in": 100, "out": 10, "cr": 0, "cw": 0})
    assert off["unclaimed"]["tokens"] == one and off["off"]["tokens"] == one and off["claimed"]["tokens"] == 3 * one
    assert [f["path"] for f in off["files"]] == ["docs/unrelated.md"] and off["tasks"] == ["t-901"]
    assert insights.matches("alpaca/web", {"alpaca/web/burn.js"}) is True
    assert insights.matches("docs/x.md", {"burn.js"}) is False and insights.matches("bin/alpaca", {"x"}) is None


# ---- t-148 cost per outcome -------------------------------------------------------------------

def test_every_done_task_shows_its_cost_and_outliers_stand_out(project):
    t = NOW - 20000
    records = []
    for k in range(5):                                   # five tasks, the last one ten times the work
        n = 10 if k == 4 else 1
        records += [resp("r%d-%d" % (k, i), t + 1000 * k + 10 * i + 5) for i in range(n)]
        task(project, "t-91%d" % k, "op-9", "Task %d" % k)
        event(project, "claim", t + 1000 * k, "s1", "t-91%d" % k, "op-9")
        event(project, "task-move", t + 1000 * k + 500, "s1", "t-91%d" % k, "op-9", {"from": "doing", "to": "done"})
    task(project, "t-999", "op-9", "Done before any transcript")
    session(project, "s1", records)
    op = next(o for o in board(project)["outcomes"] if o["op"] == "op-9")
    by = {r["task"]: r for r in op["tasks"]}
    assert op["done"] == 6 and op["captured"] == 5 and not by["t-999"]["captured"]
    assert by["t-914"]["tokens"] == pytest.approx(10 * by["t-910"]["tokens"])
    assert by["t-914"]["outlier"] and not by["t-910"]["outlier"] and op["tasks"][0]["task"] == "t-914"


def test_holds_end_on_a_move_a_lease_or_another_claim():
    ev = [{"ts": 1, "kind": "claim", "session": "a", "ref": "t-1"},
          {"ts": 5, "kind": "claim", "session": "b", "ref": "t-1"},
          {"ts": 9, "kind": "task-move", "session": "b", "ref": "t-1", "data": {"from": "doing", "to": "done"}},
          {"ts": 2, "kind": "claim", "session": "a", "ref": "t-2"},
          {"ts": 4, "kind": "lease-expired", "session": "alpaca", "ref": "t-2"}]
    assert insights.holds(ev) == [("t-1", "a", 1, 5), ("t-2", "a", 2, 4), ("t-1", "b", 5, 9)]


# ---- t-149 idle, cache re-writes and owner wait ------------------------------------------------

def test_idle_cache_rewrites_and_owner_wait():
    assert insights.idle([0, 60, 460, 500, 1500]) == (400 + 1000, 2)
    rows = [{"ts": 0, "cw": 5000}, {"ts": 100, "cw": 10}, {"ts": 500, "cw": 8000, "cwc": 0.05}, {"ts": 510, "cw": 0}]
    got = insights.cache_rewrites(rows)
    assert [(w["ts"], w["tokens"], w["cost"]) for w in got] == [(500, 8000, 0.05)]
    hourly = [{"ts": 0, "cw": 5000, "cw1": 5000}, {"ts": 1000, "cw": 900}, {"ts": 5000, "cw": 7000}]
    assert [w["ts"] for w in insights.cache_rewrites(hourly)] == [5000]      # 1-hour cache: only past an hour
    assert insights.owner_wait([10, 20, 100, 110], [50, 60, 400]) == (30 + 290, 2)


def test_each_session_shows_idle_rewrites_and_owner_wait(project):
    t = NOW - 4000
    session(project, "s1", [resp("a", t, cw=1000, cw1=0), prompt(t + 100), resp("b", t + 110),
                            resp("c", t + 1000, cw=50000, cw1=0), resp("d", t + 1010)])
    event(project, "question", t + 20, "s1", "q-1", "op-9", {"status": "open", "text": "Which way?"})
    i = signals(project, "s1")["idle"]
    assert i["idle_s"] == pytest.approx(890) and i["gaps"] == 1
    assert i["rewrites"] == 1 and i["rewrite_tokens"] == 50000 and i["rewrite_cost"] > 0
    assert i["owner_wait_s"] == pytest.approx(100) and i["owner_waits"] == 1
    assert [q["id"] for q in i["questions"]] == ["q-1"]


# ---- t-150 runaway ----------------------------------------------------------------------------

def test_runaway_marks_a_burst_and_not_steady_work(project):
    assert insights.runaway({1: 2e6, 2: 2e6, 3: 2e6, 4: 25e6}) == [{"bucket": 4, "tokens": 25e6, "median": 2e6, "ratio": 12.5}]
    assert insights.runaway({1: 2e6, 2: 3e6, 3: 2.5e6}) == []
    assert insights.runaway({1: 1e5, 2: 1e5, 3: 1e6}, floor=10e6) == []            # 10x, but under the floor
    t = NOW - 30000
    t -= t % 900
    steady = [resp("s%d" % k, t + 900 * k + 10, cr=2_000_000) for k in range(20)]
    burst = steady[:19] + [resp("b%d" % i, t + 900 * 19 + 10 + i, cr=2_000_000) for i in range(12)]
    session(project, "steady", steady)
    session(project, "burst", burst)
    hot = signals(project, "burst")["runaway"]["bins"]
    assert len(hot) == 1 and hot[0]["ratio"] >= 5 and hot[0]["start"] == t + 900 * 19
    assert signals(project, "steady")["runaway"]["bins"] == []
    rates = insights.runaway_agents({"a": 1.5e6, "b": 1.2e6, "c": 1.4e6, "d": 9e6})
    assert [r["agent"] for r in rates] == ["d"]


# ---- t-151 collisions -------------------------------------------------------------------------

def test_overlapping_edits_across_sessions_are_listed(project):
    t = NOW - 3000
    session(project, "one", [resp("a", t, "Edit", {"file_path": "/p/app.py"}), resp("b", t + 60, "Edit", {"file_path": "/p/solo.py"})])
    session(project, "two", [resp("c", t + 400, "Write", {"file_path": "/p/app.py"}),
                             resp("d", t + 5000 - 3000 + 2000, "Edit", {"file_path": "/p/solo.py"})])
    d = board(project)
    assert [(c["path"], c["a"], c["b"], c["start"], c["end"]) for c in d["collisions"]] == [("/p/app.py", "one", "two", t, t + 400)]
    assert signals(project, "one")["collisions"][0]["b"] == "two"
    assert insights.collisions([("x", 0, "f"), ("y", 5000, "f")]) == []


# ---- t-152 rule friction ----------------------------------------------------------------------

def test_friction_counts_come_with_references(project):
    t = NOW - 3000
    session(project, "s1", [
        resp("a", t, "Bash", {"command": "git stash"}, "g1"),
        failed("g1", t + 1, "Alpaca git guard: git stash moves every change in the shared main tree"),
        resp("b", t + 10, "Bash", {"command": "rm -rf build"}, "p1"),
        failed("p1", t + 11, "The user doesn't want to proceed with this tool use."),
        {"type": "attachment", "timestamp": iso(t + 20),
         "attachment": {"type": "hook_blocking_error", "hookName": "Stop", "hookEvent": "Stop",
                        "blockingError": {"blockingError": "[alpaca style] this text uses banned wording: leverage.",
                                          "command": "bin/alpaca-python -m alpaca.hooks.stop"}}},
        resp("c", t + 30)])
    event(project, "phase-door-closed", t + 40, "s1", "design->build", "op-9",
          {"reason": "a door never opens past a failing link", "verdict": "FAIL"})
    f = signals(project, "s1")["friction"]
    assert f["counts"] == {"git_guard": 1, "permission": 1, "style": 1, "phase_gate": 1}
    by = {i["kind"]: i for i in f["items"]}
    assert by["git_guard"]["line"] == 2 and by["style"]["line"] == 5 and by["phase_gate"]["event"] > 0


# ---- t-153 hook overhead ----------------------------------------------------------------------

def test_hook_time_failures_and_the_slowest_hooks(project):
    t = NOW - 3000

    def hook(kind, at, cmd, ms=None, text=""):
        a = {"type": kind, "hookName": "PostToolUse:Bash", "hookEvent": "PostToolUse", "command": cmd}
        if ms is not None:
            a["durationMs"] = ms
        if text:
            a["stderr"] = text
        return {"type": "attachment", "timestamp": iso(at), "attachment": a}
    post = 'cd "$CLAUDE_PROJECT_DIR" && bin/alpaca-python -m alpaca.hooks.post_tool'
    session(project, "s1", [
        resp("a", t), hook("hook_success", t + 1, post, 120), hook("hook_success", t + 2, post, "300"),
        hook("hook_success", t + 3, "bash other-hook.sh", 5000),                  # not an Alpaca hook
        hook("hook_non_blocking_error", t + 4, post, 40, "Traceback"),
        {"type": "system", "subtype": "stop_hook_summary", "timestamp": iso(t + 5),
         "hookInfos": [{"command": "bin/alpaca-python -m alpaca.hooks.stop", "durationMs": 700}], "hookErrors": []},
        resp("b", t + 10)])
    pool = Path(project, ".alpaca", "pool", "hooks", "s1.jsonl")
    pool.parent.mkdir(parents=True, exist_ok=True)
    pool.write_text(json.dumps({"type": "hook_outcome", "session": "s1", "hook": "stop", "outcome": "failed",
                                "duration_ms": 500, "error_type": "TimeoutError", "ts": iso(t + 6)}) + "\n")
    d = board(project)
    h = next(x for x in d["sessions"] if x["sid"] == "s1")["signals"]["hooks"]
    assert h["per_hook"]["post_tool"] == {"runs": 2, "ms": 460.0, "max_ms": 300.0, "failures": 1}
    assert h["per_hook"]["stop"] == {"runs": 1, "ms": 700.0, "max_ms": 700.0, "failures": 1}
    assert h["ms"] == 1160 and h["failures"] == 2 and h["pool_failures"][0]["error"] == "TimeoutError"
    assert [x["hook"] for x in d["hooks"]] == ["stop", "post_tool"]
