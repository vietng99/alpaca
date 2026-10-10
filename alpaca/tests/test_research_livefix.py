"""t-191: the fixes the rr-003 live round called for (the findings are in
.alpaca/proofs/op-009/t-173.logs/live-round/run/findings.txt, the confirmation in confirm-final.md).

F25, F28 and F30 (prompt text) are pinned in test_research_prompt_rules.py and test_research_audit.py;
F29 (the job's private scratch folder) in test_research_session_gaps.py, test_research_audit.py and
test_research_session_guard.py. This file holds F27 (the accept claim), F32 (the live-round
confirmer) and F23 (the replay tool's guard pass). Each test fails when its fix is taken out.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

import pytest

from alpaca import db
from alpaca.research import claims
from alpaca.tests.conftest import REPO
from alpaca.tests.devfiles import require_dev_files


# ================================================================ F27: x3 names the accept rule's result
def _attempt(project, *, accept=True, outcome="broken", accept_outcome="improvement"):
    """The rr-003 shape: the accept rule gave `improvement`, the audit then broke the attempt, so
    the attempt event's outcome is `broken`."""
    from alpaca import ops
    from alpaca.gates import verdict as vc
    from alpaca.research import events as rev
    conn = db.connect(project)
    task = ops.add_task(conn, "op-001", "attempt", "attempt", extra={"research": {
        "run": "rr-001", "role": "attempt", "attempt": "rr-001:r0:s0"}})
    acc = None
    if accept:
        acc = rev.emit(conn, rev.ACCEPT, run="rr-001", ref=task, op="op-001", session="research-rr-001",
                       code=vc.PASS, data={"accepted": True, "outcome": accept_outcome,
                                           "phase": "strict", "reason": "75785 against 101578",
                                           "score": 75785.0, "champion_score": 101578.0})
    audit_ev = rev.emit(conn, rev.AUDIT_VERDICT, run="rr-001", ref=task, op="op-001",
                        session="research-rr-001", code=vc.FAIL,
                        data={"flags": [{"flag": "protected-path"}]})
    rev.emit(conn, rev.ATTEMPT, run="rr-001", ref=task, op="op-001", session="research-rr-001",
             code=vc.FAIL, data={"forked_from": "0" * 40, "head": "0" * 40, "outcome": outcome,
                                 "accept_event": acc["id"] if acc else None,
                                 "audit_event": audit_ev["id"],
                                 "champion_metrics": {"total_bytes": {"median": 101578.0}},
                                 "metrics": {"total_bytes": {"repeats": [75785.0] * 3,
                                                             "median": 75785.0}}})
    got = {c["id"]: c for c in claims.engine_claims(conn, task)}
    conn.close()
    return got, acc


def test_f27_x3_states_the_accept_rules_outcome_not_the_attempts(project):
    got, acc = _attempt(project)
    text = got["x3"]["text"]
    assert "gives improvement" in text and "accepted true" in text, text
    assert "gives broken" not in text and "broken" not in text, text
    assert "event:%d" % acc["id"] in text
    assert "75785.0" in text and "101578.0" in text      # the medians stay in the claim


def test_f27_the_audit_outcome_stays_its_own_claim(project):
    got, _acc = _attempt(project)
    assert "x4" in got and "verdict FAIL" in got["x4"]["text"] and got["x4"]["requires"] == "rehash"
    assert "improvement" not in got["x4"]["text"]


def test_f27_an_attempt_that_never_reached_the_accept_rule_has_no_x3(project):
    # an attempt whose outcome is broken before the rule ran: there is no accept event to state, and
    # x3 used to say "the accept rule gives broken" from the attempt outcome
    got, _acc = _attempt(project, accept=False)
    assert "x3" not in got
    assert "x4" in got            # the audit claim does not depend on the accept event


def test_f27_a_not_accepted_rule_result_is_stated_as_it_was_recorded(project):
    got, _acc = _attempt(project, accept_outcome="regression", outcome="regression")
    assert "gives regression" in got["x3"]["text"]


# ================================================================ F32: the live-round confirmer
@pytest.fixture(scope="module")
def lr():
    require_dev_files("tools/live_round.py")
    spec = importlib.util.spec_from_file_location("live_round_tool_t191",
                                                  os.path.join(REPO, "tools", "live_round.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _job(job, role="verifier", group=None, try_n=0, **kw):
    return {"id": 1, "kind": "research-job", "data": dict(
        {"job": job, "role": role, "group": group, "try": try_n}, **kw)}


def _res(job):
    return {"id": 2, "kind": "research-job-result", "data": {"job": job}}


def _exp(job, reason):
    return {"id": 3, "kind": "research-job-expired", "data": {"job": job, "reason": reason}}


def test_f32_a_job_a_stop_expired_is_not_a_missing_result(lr):
    jobs = [_job("a"), _job("b"), _job("c")]
    results = [_res("a")]
    by = {"research-job-expired": [_exp("b", "stopped"), _exp("c", "deadline")]}
    stopped = lr.stopped_jobs(by)
    assert stopped == {"b"}
    # b was expired by the stop and is left out; c expired for another reason and still counts
    assert lr.results_missing(jobs, results, stopped) == ["c"]
    # with no stop nothing is left out
    assert lr.results_missing(jobs, results, set()) == ["b", "c"]


def test_f32_verifier_pairs_group_by_try_not_by_round(lr):
    # rr-003 vf-t-186-79953/r0: two tries of a pair and a single-slot retry share one group key
    g = "vf-t-186-79953/r0"
    jobs = [_job("v1-t0", group=g, try_n=0), _job("v2-t0", group=g, try_n=0),
            _job("v1-t1", group=g, try_n=1), _job("v2-t1", group=g, try_n=1),
            _job("v1-t2", group=g, try_n=2), _job("p1", role="proposer", group=None)]
    pairs = lr.verifier_pairs(jobs)
    assert pairs == {(g, 0): ["v1-t0", "v2-t0"], (g, 1): ["v1-t1", "v2-t1"], (g, 2): ["v1-t2"]}


def _record(tmp_path, jobs, results, expired, hooks):
    """A record with the events the confirmer reads for run rr-009, as a read-only database."""
    import sqlite3
    root = tmp_path / "proj"
    (root / ".alpaca").mkdir(parents=True)
    conn = sqlite3.connect(str(root / ".alpaca" / "alpaca.db"))
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT, session TEXT, actor TEXT, "
                 "kind TEXT, op TEXT, ref TEXT, data TEXT)")
    n = 0

    def add(kind, data, ref="rr-009"):
        nonlocal n
        n += 1
        conn.execute("INSERT INTO events (id, ts, session, actor, kind, op, ref, data) "
                     "VALUES (?,?,?,?,?,?,?,?)", (n, "t", "s", "a", kind, None, ref,
                                                  json.dumps(dict(data, run="rr-009"))))
    for j in jobs:
        add("research-job", j)
    for r in results:
        add("research-job-result", r)
    for e in expired:
        add("research-job-expired", e)
    for kind, agent in hooks:
        add(kind, {"agent_id": agent}, ref=None)
    conn.commit()
    conn.close()
    return str(root)


def _confirm(lr, tmp_path, capsys, jobs, results, expired, hooks):
    root = _record(tmp_path, jobs, results, expired, hooks)
    out = tmp_path / "out"
    out.mkdir()
    (out / "headless-refusals.txt").write_text("")
    capsys.readouterr()
    lr.confirm(root, "rr-009", str(out), no_ps=True)
    return capsys.readouterr().out.splitlines()


def _line(lines, text):
    hit = [ln for ln in lines if text in ln]
    assert hit, (text, lines)
    return hit[0]


def test_f32_confirm_leaves_out_stopped_jobs_and_pairs_by_try(lr, tmp_path, capsys):
    g = "vf-x/r0"
    jobs = [{"job": "v1-t0", "role": "verifier", "group": g, "try": 0},
            {"job": "v2-t0", "role": "verifier", "group": g, "try": 0},
            {"job": "v1-t1", "role": "verifier", "group": g, "try": 1},
            {"job": "v2-t1", "role": "verifier", "group": g, "try": 1},
            {"job": "v1-t2", "role": "verifier", "group": g, "try": 2}]
    results = [{"job": j["job"], "agent_id": "ag-" + j["job"]} for j in jobs[:4]]
    # try 2 (a single-slot retry) was stopped: no result, expired with reason stopped
    expired = [{"job": "v1-t2", "reason": "stopped"}]
    # the pair of try 0 overlapped; the pair of try 1 overlapped; try 0 stopped before try 1 started,
    # which a check that groups a whole round into one pair reads as no overlap
    hooks = [("subagent-start", "ag-v1-t0"), ("subagent-start", "ag-v2-t0"),
             ("subagent-stop", "ag-v1-t0"), ("subagent-stop", "ag-v2-t0"),
             ("subagent-start", "ag-v1-t1"), ("subagent-start", "ag-v2-t1"),
             ("subagent-stop", "ag-v1-t1"), ("subagent-stop", "ag-v2-t1")]
    lines = _confirm(lr, tmp_path, capsys, jobs, results, expired, hooks)
    got = _line(lines, "an accepted result for each")
    assert got.startswith("PASS"), got
    assert "1 expired by a stop" in got
    pair = _line(lines, "the verifier pair of each group overlapped")
    assert pair.startswith("PASS") and "3 pairs" in pair, pair


def test_f32_a_job_expired_for_another_reason_still_fails_the_result_check(lr, tmp_path, capsys):
    jobs = [{"job": "a", "role": "notes", "group": None, "try": 1},
            {"job": "b", "role": "notes", "group": None, "try": 1}]
    lines = _confirm(lr, tmp_path, capsys, jobs, [{"job": "a", "agent_id": "x"}],
                     [{"job": "b", "reason": "deadline"}], [])
    got = _line(lines, "an accepted result for each")
    assert got.startswith("FAIL") and "missing ['b']" in got, got


def test_f32_a_pair_that_did_not_overlap_still_fails(lr, tmp_path, capsys):
    g = "vf-y/r0"
    jobs = [{"job": "v1", "role": "verifier", "group": g, "try": 0},
            {"job": "v2", "role": "verifier", "group": g, "try": 0}]
    results = [{"job": "v1", "agent_id": "a1"}, {"job": "v2", "agent_id": "a2"}]
    hooks = [("subagent-start", "a1"), ("subagent-stop", "a1"),
             ("subagent-start", "a2"), ("subagent-stop", "a2")]       # one after the other
    lines = _confirm(lr, tmp_path, capsys, jobs, results, [], hooks)
    got = _line(lines, "the verifier pair of each group overlapped")
    assert got.startswith("FAIL") and "vf-y/r0/t0" in got, got


# ================================================================ F23: the replay tool's guard pass is optional
@pytest.fixture(scope="module")
def kr():
    require_dev_files("tools/k_replay.py")
    spec = importlib.util.spec_from_file_location("k_replay_tool_t191",
                                                  os.path.join(REPO, "tools", "k_replay.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_main(kr, monkeypatch, tmp_path, argv):
    """kr.main with every pass stubbed to a recorder: returns the names of the passes that ran."""
    import io
    import alpaca.pool as pool
    from alpaca.research import jobs, session_engine as eng
    for mod, name in ((eng, "COMPILE_SITE_PACKAGES"), (eng, "PRELOAD_BLOCK"), (eng, "compile_engine"),
                      (jobs, "STRAGGLER_WAIT_S"), (jobs, "find_processes"), (pool, "pool_dir"),
                      (kr, "ORIG_POOL_DIR")):
        monkeypatch.setattr(mod, name, getattr(mod, name))
    stand_in = tmp_path / "stand-in.jsonl"
    stand_in.write_text('{"toolUseId": "x"}\n', encoding="utf-8")
    for name in ("TRANSCRIPT", "PROMPT", "META", "LIVE_DB", "LIVE_POOL"):
        monkeypatch.setattr(kr, name, str(stand_in))
    monkeypatch.setattr(kr, "REAL_DATA", (str(stand_in),))
    monkeypatch.setattr(kr, "make_base", lambda scratch: str(stand_in))
    ran = []

    class World:
        def __init__(self, *a, **k):
            self.conn = type("C", (), {"close": lambda s: None})()
            self.root = str(tmp_path / "world-root")
            os.makedirs(self.root, exist_ok=True)
            self.job = {"scanned_by": "scope", "allowed_tools": []}

        def build(self):
            pass

        def claim(self, **over):
            return {}

    monkeypatch.setattr(kr, "World", World)
    monkeypatch.setattr(kr, "per_check", lambda *a, **k: None)
    monkeypatch.setattr(kr, "submit_like", lambda *a, **k: None)
    monkeypatch.setattr(kr, "extra_probes", lambda *a, **k: ran.append("probes"))
    monkeypatch.setattr(kr, "guard_replay", lambda *a, **k: ran.append("guard"))
    monkeypatch.setattr(kr, "guard_other_agents", lambda *a, **k: ran.append("guard-other"))
    monkeypatch.setattr(kr, "corpus_pass", lambda *a, **k: ran.append("corpus"))
    monkeypatch.setattr(kr, "RESULTS", [])
    monkeypatch.setattr(kr, "OUT", io.StringIO())
    monkeypatch.setattr(sys, "argv", ["k_replay.py", "--scratch", str(tmp_path / "s")] + argv)
    kr.main()
    return ran


def test_f23_the_guard_pass_runs_only_with_the_guard_flag(kr, monkeypatch, tmp_path):
    default = _run_main(kr, monkeypatch, tmp_path, ["--profile", "notes"])
    assert default == ["probes"], default                      # the probe replay, no guard, no corpus
    asked = _run_main(kr, monkeypatch, tmp_path, ["--profile", "notes", "--guard"])
    assert asked == ["probes", "guard", "guard-other"], asked
    # the old flag still parses and changes nothing; it also wins over --guard
    old = _run_main(kr, monkeypatch, tmp_path, ["--profile", "notes", "--no-guard"])
    assert old == ["probes"], old
    both = _run_main(kr, monkeypatch, tmp_path, ["--profile", "notes", "--guard", "--no-guard"])
    assert both == ["probes"], both


def test_f23_the_corpus_pass_stays_optional_too(kr, monkeypatch, tmp_path):
    ran = _run_main(kr, monkeypatch, tmp_path, ["--profile", "notes", "--corpus"])
    assert ran == ["probes", "corpus"], ran


def test_f23_the_docstring_names_both_slow_passes(kr):
    doc = kr.__doc__
    assert "--guard" in doc and "--corpus" in doc and "per-change check" in doc
