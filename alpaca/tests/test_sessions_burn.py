"""t-143: subagents fold under their parent session, and the Sessions tab token and cost board.

Small synthetic Claude transcripts only. The fold: a registered child (or an unregistered copy whose
first record names its parent) is never a session row; its parent carries it; the project totals
equal what they were when the child was listed as a row. The board: active minutes, 15-minute
buckets, compaction drops, the median and middle-half band of context by turn, the turn-limit
what-if, event markers and the person. The fan-out: peak concurrency, queue wait, per-phase
counts, failures and retries, the token join with its run-record fallback, and the project copy
of each run record that outlives the operator's own storage.
"""
import http.server
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

from alpaca import db, observability, ownerword, serve, transcripts
from alpaca.analytics import build_index, burn, children, fanout, metrics, metrics_index

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc).timestamp()


def iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def response(ident, at, inp=100, out=10, cr=0, cw=0, model="claude-opus-5", **extra):
    return {"type": "assistant", "uuid": ident, "timestamp": iso(at), **extra,
            "message": {"id": ident, "model": model, "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
                                  "cache_creation_input_tokens": cw}}}


def write(path, records):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(rec) + "\n" for rec in records), encoding="utf-8")
    return str(path)


def register(root, sid):
    conn = db.connect(root)
    db.upsert(conn, "sessions", "sid", {"sid": sid, "started": "2026-09-30T00:00:00Z"})
    db.meta_set(conn, "operator:" + sid, "claude")
    conn.close()


def family(root):
    """A parent with one registered child copy and one unregistered sidechain copy."""
    register(root, "parent")
    write(transcripts.local_path(root, "parent"), [response("p1", NOW - 3600)])
    child = write(transcripts.local_path(root, "a1b2c3"), [response("c1", NOW - 3000), response("c2", NOW - 2900)])
    observability.expect_source(root, "a1b2c3", "claude", locator=child, parent_session="parent")
    write(transcripts.local_path(root, "d4e5f6"), [
        {"type": "user", "isSidechain": True, "sessionId": "parent", "agentId": "d4e5f6",
         "timestamp": iso(NOW - 2000), "message": {"role": "user", "content": "task"}},
        response("s1", NOW - 1990, isSidechain=True, sessionId="parent", agentId="d4e5f6")])


# ---- the fold ----------------------------------------------------------------------------------

def test_a_registered_child_is_never_a_session_row(project):
    family(project)
    rows = build_index.collect(project)
    assert [row["sid"] for row in rows] == ["parent"]
    assert rows[0]["subagent_ids"] == ["a1b2c3", "d4e5f6"]
    folded = build_index._fold(project, rows)
    assert [s["sid"] for s in folded["sessions"]] == ["parent"]


def test_parent_totals_include_each_child_and_overall_totals_are_unchanged(project):
    family(project)
    # What the page showed before the fold: every copy was a row of its own.
    before = metrics_index.index(project, [{"sid": sid, "class": "work"} for sid in ("parent", "a1b2c3", "d4e5f6")],
                                 families={})
    assert len(before["sessions"]) == 3
    after = metrics_index.index(project, build_index.collect(project))
    assert [row["sid"] for row in after["sessions"]] == ["parent"]
    row = after["sessions"][0]
    assert row["summary"]["tokens"]["total_tokens"] == 110          # the main conversation alone
    assert row["subagents"]["count"] == 2 and row["subagents"]["measured"] == 2
    assert row["subagents"]["total_tokens"] == 330
    assert row["family"]["total_tokens"] == 440 and row["family"]["responses"] == 4
    expected_cost = sum(metrics.analyze(project, sid)["summary"]["estimated_cost_usd"] for sid in ("parent", "a1b2c3", "d4e5f6"))
    assert row["family"]["estimated_cost_usd"] == pytest.approx(expected_cost)
    for key in ("total_tokens", "estimated_cost_usd", "responses", "costed_responses"):
        assert after["totals"][key] == pytest.approx(before["totals"][key]), key
    assert after["totals"]["listed_sessions"] == 1 and after["totals"]["subagents"] == 2
    assert after["totals"]["main_total_tokens"] == 110 and after["totals"]["children_total_tokens"] == 330


def test_discovery_names_workflow_children_from_their_directory(project):
    register(project, "parent")
    write(transcripts.local_path(project, "parent"), [response("p1", NOW - 60)])
    base = Path(transcripts.subagents_dir(project, "parent"))
    write(base / "agent-aaa111.jsonl", [response("x1", NOW - 50)])
    write(base / "workflows" / "wf_run1" / "agent-bbb222.jsonl", [response("x2", NOW - 40)])
    conn = db.connect_readonly(project)
    try:
        kids = children.discover(project, conn)["parent"]
    finally:
        conn.close()
    assert kids["aaa111"]["kind"] == "subagent"
    assert kids["bbb222"]["kind"] == "workflow" and kids["bbb222"]["workflow"] == "wf_run1"
    row = metrics_index.index(project, build_index.collect(project))["sessions"][0]
    assert row["subagents"]["workflow"] == 1 and row["family"]["total_tokens"] == 330


# ---- the board ---------------------------------------------------------------------------------

def board(root, window="24h"):
    return burn.board(root, build_index.collect(root), window, now=NOW)


def test_active_minutes_and_fifteen_minute_buckets(project):
    register(project, "s1")
    t0 = NOW - 7200 - (NOW % 900)                                   # the start of a quarter hour
    write(transcripts.local_path(project, "s1"), [
        response("r1", t0 + 5), response("r2", t0 + 20),              # one minute, one bucket
        response("r3", t0 + 75),                                      # the next minute, same bucket
        response("r4", t0 + 900 + 30, inp=1000),                      # the next bucket
        response("old", NOW - 3 * 86400)])                            # outside the 24 h window
    d = board(project)
    assert d["window"]["bucket_s"] == 900 and d["window"]["buckets"] == 97
    s = d["sessions"][0]
    assert s["requests"] == 4 and s["active_minutes"] == 3
    assert s["tokens"] == 110 * 3 + 1010 and s["rate"] == pytest.approx(s["tokens"] / 3)
    b0 = int((t0 - d["window"]["start"]) // 900)
    assert s["bins"] == {str(b0): 330, str(b0 + 1): 1010}
    week = board(project, "7d")
    assert week["sessions"][0]["requests"] == 5 and week["window"]["bucket_s"] == 1800


def test_compaction_is_a_context_drop(project):
    assert burn.compactions([100000, 150000, 200000, 30000, 60000, 58000]) == [3]
    assert burn.compactions([100000, 90000, 85000]) == []             # small dips are not compactions
    register(project, "s1")
    sizes = [50000, 120000, 190000, 25000, 90000, 160000, 20000]
    write(transcripts.local_path(project, "s1"),
          [response("r%d" % i, NOW - 3000 + 60 * i, inp=10, cr=size - 10) for i, size in enumerate(sizes)])
    curve = board(project)["sessions"][0]["curve"]
    assert curve["turns"] == 7 and curve["peak"] == 190000 and curve["compactions"] == 2
    assert curve["points"] == sizes


def rows(contexts, out=10):
    return [{"in": c, "out": out, "cr": 0, "cw": 0} for c in contexts]


def test_median_and_middle_half_band_by_turn():
    agents = [rows([10, 20, 30]), rows([20, 40]), rows([30, 60, 90, 120])]
    g = burn.growth(agents)
    assert [x["t"] for x in g] == [1, 2, 3, 4]
    assert g[0] == {"t": 1, "n": 3, "med": 20, "q1": 15, "q3": 25, "cost": 60 + 30}
    assert (g[1]["med"], g[1]["q1"], g[1]["q3"], g[1]["n"]) == (40, 30, 50, 3)
    assert (g[2]["med"], g[2]["n"]) == (60, 2) and (g[3]["med"], g[3]["n"]) == (120, 1)


def test_turn_limit_what_if_is_split_by_model():
    agents = [rows([100] * 5), rows([200] * 2), rows([50] * 8)]
    caps = burn.turn_caps(agents, ["opus", "sonnet", "opus"], candidates=(2, 4, 10))
    total = 5 * 110 + 2 * 210 + 8 * 60
    assert [c["cap"] for c in caps] == [2, 4]
    two = caps[0]
    assert two["tokens"] == 2 * 110 + 2 * 210 + 2 * 60 and two["save"] == total - two["tokens"]
    assert two["cut"] == 2 and two["split"] == {"opus": 2 * 110 + 2 * 60, "sonnet": 2 * 210}
    assert caps[1]["split"]["opus"] == 4 * 110 + 4 * 60


def test_the_subagent_section_uses_the_session_that_spent_most_on_subagents(project):
    family(project)
    d = board(project)
    crew = d["crew"]
    assert crew["sid"] == "parent" and crew["agents"] == 2
    assert crew["growth"][0]["n"] == 2 and crew["tokens"] == 330
    s = d["sessions"][0]
    assert s["subagents"] == 2 and s["split"]["main"]["tokens"] == 110
    assert s["split"]["subagent"]["tokens"] == 330 and s["split"]["subagent"]["agents"] == 2


def test_event_markers_and_limit_band_come_from_the_transcript(project):
    register(project, "s1")
    write(transcripts.local_path(project, "s1"), [
        response("r1", NOW - 1800),
        {"type": "system", "subtype": "api_error", "timestamp": iso(NOW - 1700), "retryAttempt": 1,
         "error": {"status": 529, "message": "Overloaded"}},
        {"type": "assistant", "timestamp": iso(NOW - 1600), "isApiErrorMessage": True, "error": "rate_limit",
         "apiErrorStatus": 429, "quotaLimits": {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": NOW - 600},
         "message": {"id": "e1", "model": "<synthetic>", "content": [], "usage": {"input_tokens": 0, "output_tokens": 0}}}])
    d = board(project)
    assert d["events"]["retries"] == 1 and d["events"]["refusals"] == 1
    assert d["events"]["bands"] == [{"start": NOW - 1600, "end": NOW - 600, "label": "five_hour"}]
    assert d["sessions"][0]["requests"] == 1 and d["sessions"][0]["errors"] == 2


def test_person_comes_from_the_account_in_the_transcript_path():
    pooled = "/".join((ownerword.ACCOUNTS_ROOT, "account-a", "projects/x/abc.jsonl"))
    assert burn.person(pooled, "claude") == "account-a"
    assert burn.person("/home/someone/.claude/projects/x/abc.jsonl") == "someone"
    assert burn.person("/data/abc.jsonl", "codex") == "codex"


def test_burn_route_serves_the_board_and_keeps_the_auth_gate(project):
    family(project)
    live = serve.Live(project)
    live.refresh()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(live, project))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d" % httpd.server_address[1]

    def get(path):
        try:
            r = urllib.request.urlopen(url + path, timeout=15)
        except urllib.error.HTTPError as exc:
            r = exc
        return r.status, r.read()
    try:
        status, body = get("/hub/burn.json?window=7d")
        assert status == 200 and body.isascii()
        data = json.loads(body)
        assert data["window"]["key"] == "7d" and "totals" in data and "crew" in data
        assert get("/hub/burn.json?window=year")[0] == 400
        for name in ("burn.js", "burn.css"):
            status, asset = get("/hub/assets/" + name)
            assert status == 200 and asset.isascii()
        index = json.loads(get("/hub/analytics-index.json")[1])
        assert [row["sid"] for row in index["sessions"]] == ["parent"]
        Path(project, ".alpaca/files-auth").write_text("reader:test-password")
        assert get("/hub/burn.json")[0] == 401
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---- workflow fan-out ---------------------------------------------------------------------------

def run_record(start, agents, status="killed", run="wf_test-1"):
    return {"runId": run, "workflowName": "demo-run", "status": status, "startTime": start * 1000,
            "durationMs": 40000, "agentCount": len(agents), "defaultModel": "claude-opus-5",
            "phases": [{"title": "Build", "detail": "in parallel"}, {"title": "Check", "detail": ""}],
            "workflowProgress": [{"type": "workflow_phase", "index": 1, "title": "Build"},
                                 {"type": "workflow_phase", "index": 2, "title": "Check"}] + agents}


def agent(i, ident, phase, queued, started, duration=None, state="done", attempt=1, tokens=None, last=None):
    entry = {"type": "workflow_agent", "index": i, "label": "agent %d" % i, "phaseIndex": phase,
             "phaseTitle": ["", "Build", "Check"][phase], "agentId": ident, "model": "claude-opus-5",
             "state": state, "queuedAt": queued * 1000, "startedAt": started * 1000, "attempt": attempt}
    if duration is not None:
        entry["durationMs"] = duration
    if tokens is not None:
        entry["tokens"] = tokens
    if last is not None:
        entry["lastProgressAt"] = last * 1000
    return entry


def test_fanout_figures_from_a_run_record():
    t = 1000
    record = run_record(t, [
        agent(1, "a1", 1, t, t + 2, 10000, tokens=100),
        agent(2, "a2", 1, t, t + 4, 10000, attempt=2, tokens=200),
        agent(3, "a3", 2, t + 15, t + 16, "5000", state="error", tokens=50),   # durationMs as a string
        agent(4, "a4", 2, t + 15, t + 16, state="progress", last=t + 30)])     # open when the run was killed
    run = fanout.summarize(record, usage={"a1": {"tokens": 1000, "cost": 0.5, "responses": 3}},
                           links={"a2": "child-abc"})
    f = run["figures"]
    assert f["agents"] == 4 and f["peak"] == 2 and run["rows"] == 2
    assert f["queue_median_s"] == pytest.approx(1.5) and f["queue_longest_s"] == pytest.approx(4)
    assert f["duration_median_s"] == pytest.approx(10) and f["duration_longest_s"] == pytest.approx(10)
    assert [(p["title"], p["agents"], p["peak"]) for p in run["phases"]] == [("Build", 2, 2), ("Check", 2, 2)]
    assert f["failed"] == 1 and f["unfinished"] == 1 and f["retries"] == 1 and f["extra_attempts"] == 1
    # The token join: a1 from its transcript, a2 and a3 from the run record, a4 unknown.
    by = {a["id"]: a for a in run["agents"]}
    assert (by["a1"]["tokens"], by["a1"]["src"], by["a1"]["cost"]) == (1000, "transcript", 0.5)
    assert (by["a2"]["tokens"], by["a2"]["src"]) == (200, "record") and by["a4"]["tokens"] is None
    assert f["tokens"] == 1250 and f["cost_usd"] == 0.5 and (f["joined"], f["from_record"]) == (1, 2)
    assert by["a4"]["e"] == pytest.approx(t + 30) and by["a2"]["child"] == "child-abc"
    assert fanout.peak([(0, 1), (1, 2)]) == 1 and fanout.peak([(0, 2), (1, 3), (1.5, 1.6)]) == 3


def native_session(project, tmp_path, sid="wfs"):
    """A registered session whose native directory holds one workflow run and one agent transcript."""
    base = tmp_path / "native"
    main = write(base / (sid + ".jsonl"), [response("m1", NOW - 3700)])
    conn = db.connect(project)
    db.upsert(conn, "sessions", "sid", {"sid": sid, "started": "2026-09-30T00:00:00Z", "transcript": main})
    conn.close()
    t = NOW - 3600
    record = run_record(t, [agent(1, "a1", 1, t, t + 1, 60000, tokens=10),
                            agent(2, "a2", 1, t, t + 1, 60000, tokens=77)], status="completed")
    write_json(base / sid / "workflows" / "wf_test-1.json", record)
    write(base / sid / "subagents" / "workflows" / "wf_test-1" / "agent-a1.jsonl",
          [response("x1", t + 5, inp=500), response("x2", t + 30, inp=700)])
    write_json(base / sid / "subagents" / "workflows" / "wf_test-1" / "agent-a1.meta.json",
               {"agentType": "workflow-subagent", "description": "agent 1"})
    return base, main


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_board_joins_agent_transcripts_and_falls_back_to_the_run_record(project, tmp_path):
    native_session(project, tmp_path)
    d = board(project)
    s = d["sessions"][0]
    run = s["runs"][0]
    by = {a["id"]: a for a in run["agents"]}
    assert by["a1"]["src"] == "transcript" and by["a1"]["tokens"] == 510 + 710
    assert by["a2"]["src"] == "record" and by["a2"]["tokens"] == 77
    assert run["figures"]["tokens"] == 1220 + 77 and run["figures"]["peak"] == 2
    assert by["a1"]["child"] and not by["a2"]["child"]            # the detail view can open a1 only
    assert d["fanout"] == [{"sid": "wfs", "run": 0, "tokens": 1297}]


def test_snapshot_copies_run_records_and_the_board_reads_them_when_the_source_is_gone(project, tmp_path):
    import shutil
    base, main = native_session(project, tmp_path)
    out = transcripts.snapshot(project, "wfs")
    copy = Path(transcripts.workflows_dir(project, "wfs")) / "wf_test-1.json"
    assert out["workflows"] == 1 and json.loads(copy.read_text())["runId"] == "wf_test-1"
    meta = Path(transcripts.subagents_dir(project, "wfs")) / "workflows" / "wf_test-1" / "agent-a1.meta.json"
    assert meta.is_file()
    # A run record is rewritten in place while it runs: the next snapshot replaces the copy whole.
    record = json.loads((base / "wfs" / "workflows" / "wf_test-1.json").read_text())
    record["status"] = "killed"
    write_json(base / "wfs" / "workflows" / "wf_test-1.json", record)
    transcripts.snapshot(project, "wfs")
    assert json.loads(copy.read_text())["status"] == "killed"
    shutil.rmtree(base)                                            # the operator's storage is cleared
    run = board(project)["sessions"][0]["runs"][0]
    assert run["status"] == "killed" and run["figures"]["agents"] == 2
    assert {a["id"]: a["src"] for a in run["agents"]} == {"a1": "transcript", "a2": "record"}
