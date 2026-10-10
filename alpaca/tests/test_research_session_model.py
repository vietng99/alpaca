"""The run-level model override (F13; design/research-engine/in-session-runtime.md sections 3.3,
3.4 and 4.3).

`alpaca research start --model <alias>` records one model on research-run-start, and every agent
job of the run names it with tier `override`, so the driver spawns every agent on it, K10 holds
each agent to it and the rate-card check reads that model alone. `resume` and `extend` take the
flag only to repeat the run's own model. Without the flag every role keeps its shield tier. Each
test fails when its behavior is taken out (.alpaca/proofs/op-009/t-173.logs/f13-mutations.txt).
"""
from __future__ import annotations

import json

import pytest

from alpaca import db
from alpaca.formation import shield
from alpaca.gates import verdict as vc
from alpaca.research import errors, events, jobs, session_runtime
from alpaca.tests import research_loopkit as kit
from alpaca.tests import research_session_kit as rk
from alpaca.tests.test_research_cli import RB, _call, _ev, _finalize, _refusals, rec  # noqa: F401
from alpaca.tests.test_research_session_runtime import (  # noqa: F401
    _new_run, _no_thread_outlives_its_test, _why, _winner, needs_jail)

ROLES = (("proposer", 0), ("implementer", 0), ("notes", 0), ("verifier", "v1"), ("verifier", "v2"),
         ("auditor", None))


@pytest.fixture
def world(project, monkeypatch):
    w = rk.JudgeWorld(monkeypatch, project)
    yield w
    w.close()


def _job_event(w, jid):
    row = w.conn.execute("SELECT data FROM events WHERE kind=? AND json_extract(data, '$.job')=?",
                         (events.JOB, jid)).fetchone()
    return json.loads(row["data"])


# ================================================================ the model of a job
def test_codex_roles_and_overrides_use_the_shield_mapping(project):
    expected = ["gpt-6.1-sol", "gpt-6.1-sol", "gpt-6.1-sol",
                "gpt-6.1-sol", "gpt-6-astra", "gpt-6-astra"]
    assert session_runtime.model_aliases(project, "codex") == sorted(set(expected))
    for (role, slot), model in zip(ROLES, expected):
        assert session_runtime.resolve_model(project, "codex", role, slot)[1] == model
        for override in set(expected):
            assert session_runtime.check_model_override(project, override, "codex") == override
            assert session_runtime.resolve_model(project, "codex", role, slot,
                                                 override=override) == ("override", override)
        for invalid in ("sonnet", "opus", "gpt-6-sol"):
            with pytest.raises(shield.ShieldError):
                session_runtime.resolve_model(project, "codex", role, slot, override=invalid)


def test_f13_resolve_model_names_the_override_for_every_role_and_refuses_an_unmapped_one(project):
    for role, slot in ROLES:
        assert session_runtime.resolve_model(project, "claude", role, slot, override="opus") == \
            ("override", "opus"), role
        # control: without the override the role keeps its shield tier
        assert session_runtime.resolve_model(project, "claude", role, slot)[0] != "override", role
    # the pair shares the override; without it the two verifiers run on two tiers
    assert session_runtime.resolve_model(project, "claude", "verifier", "v1", override="sonnet") == \
        session_runtime.resolve_model(project, "claude", "verifier", "v2", override="sonnet")
    assert session_runtime.resolve_model(project, "claude", "verifier", "v1") != \
        session_runtime.resolve_model(project, "claude", "verifier", "v2")
    for bad, runtime in (("gpt-9", "claude"), ("high", "claude"), ("opus", "codex")):
        with pytest.raises(shield.ShieldError):
            session_runtime.resolve_model(project, runtime, "notes", 0, override=bad)


def test_f13_the_alias_check_knows_the_shield_aliases_only(project):
    known = session_runtime.model_aliases(project, "claude")
    assert {"opus", "sonnet"} <= set(known)
    for alias in known:
        assert session_runtime.check_model_override(project, alias) == alias
        assert session_runtime.check_model_override(project, alias, "control") == alias
        assert session_runtime.check_model_override(project, alias, None) == alias
    for bad in ("gpt-9", "high", "Opus", "claude-opus-5-5", "", None):
        with pytest.raises(errors.ResearchRefusal) as exc:
            session_runtime.check_model_override(project, bad)
        assert exc.value.reason == "RUN-MODEL-UNKNOWN" and exc.value.code == vc.FAIL, bad


def test_f13_run_model_reads_the_runs_start_and_nothing_else(project):
    from alpaca import cli
    cli.main(["init"])
    conn = db.connect(project)
    try:
        events.emit(conn, events.RUN_START, run="rr-001", ref="rr-001", op=None, session="s",
                    data={"mode": "improve", "model": "opus"})
        events.emit(conn, events.RUN_START, run="rr-002", ref="rr-002", op=None, session="s",
                    data={"mode": "improve", "model": None})
        assert session_runtime.run_model(conn, "rr-001") == "opus"
        assert session_runtime.run_model(conn, "rr-002") is None
        assert session_runtime.run_model(conn, "rr-404") is None
        assert session_runtime.run_model(conn, None) is None
    finally:
        conn.close()


def test_f13_a_job_belongs_to_its_run_by_spec_by_scope_or_by_its_tasks_research_block(rec, capsys):
    # the report task's verification names no run on its spec: inside the runner the scope is the
    # run, and `alpaca proof verify` outside it finds the run in the task's research block
    from alpaca.research.types import LaunchSpec
    code, body = _call(capsys, "start", "--runbook", RB, "--model", "opus")
    run = body["run"]
    report_task = _ev(rec.root, events.RUN_START, run)[0]["data"]["report_task"]

    def spec(**kw):
        return LaunchSpec(role="verifier", runtime="claude", session="s", workdir="/w", cwd="/w",
                          prompt="p", timeout_s=60, key="k", **kw)
    conn = db.connect(rec.root)
    try:
        assert session_runtime.job_run(conn, spec(run="rr-077", attempt=report_task), "vf-x") == "rr-077"
        assert session_runtime.job_run(conn, spec(attempt="t-999"), run) == run
        assert session_runtime.job_run(conn, spec(attempt=report_task), "vf-%s-9" % report_task) == run
        assert session_runtime.run_model(conn, session_runtime.job_run(
            conn, spec(attempt=report_task), "vf-%s-9" % report_task)) == "opus"
        # a task with no research block, and no task, belong to no run
        assert session_runtime.job_run(conn, spec(attempt="t-999"), "vf-t-999-9") is None
        assert session_runtime.job_run(conn, spec(), "vf-x") is None
    finally:
        conn.close()


# ================================================================ K10 on an overridden job
def test_f13_k10_accepts_an_opus_agent_for_an_overridden_job_and_refuses_a_sonnet_one(world):
    w = world
    n = 0
    for role in ("implementer", "notes"):
        n += 1
        jid = w.issue(role, try_n=n, override="opus")
        assert (_job_event(w, jid)["tier"], _job_event(w, jid)["model"]) == ("override", "opus")
        assert jobs.read_job(w.root, w.SCOPE, jid)["model"] == "opus"
        w.hand()
        assert w.entries[jid]["model"] == "opus"          # what next-job hands the driver
        a = w.run_agent(jid)                              # the agent runs on the job's model
        c, problem = w.judge(jid, w.claim(jid, a))
        assert problem is None, (role, problem)
        assert c.store["models_seen"] == [rk.MODELS["opus"]]
        # the same job shape done by a sonnet agent is refused by K10
        n += 1
        jid = w.issue(role, try_n=n, override="opus")
        w.hand()
        a = w.run_agent(jid, model=rk.MODELS["sonnet"])
        c, problem = w.judge(jid, w.claim(jid, a))
        assert problem is not None and problem.code == "JOB-MODEL-MISMATCH", (role, problem)
        assert "names opus" in problem.detail
    # control: without the override an implementer job names its tier's model, and an opus agent
    # is the one K10 refuses
    jid = w.issue("implementer", try_n=50)
    assert (_job_event(w, jid)["tier"], _job_event(w, jid)["model"]) == ("mid", "sonnet")
    w.hand()
    a = w.run_agent(jid, model=rk.MODELS["opus"])
    c, problem = w.judge(jid, w.claim(jid, a))
    assert problem is not None and problem.code == "JOB-MODEL-MISMATCH", problem


def test_f13_k10_holds_a_verifier_and_the_auditor_to_the_override(world):
    # the pair and the auditor are judged against the same one model (their full checks need the
    # packets of a real verification; the K10 row of the diagnosis table is what this test reads)
    w = world
    for i, (role, slot) in enumerate((("verifier", "v1"), ("verifier", "v2"), ("auditor", None))):
        for k, (model, ok) in enumerate(((rk.MODELS["opus"], True), (rk.MODELS["sonnet"], False))):
            jid = w.issue(role, try_n=10 * (i + 1) + k, verifier_slot=slot, override="opus")
            assert _job_event(w, jid)["model"] == "opus" and _job_event(w, jid)["tier"] == "override"
            w.hand()
            a = w.run_agent(jid, model=model, behavior=lambda r, j: {"calls": []})
            rows, _first = jobs.run_checks_all(w.ctx_for(jid, w.claim(jid, a)))
            k10 = [r for r in rows if r["k"] == "K10"][0]
            assert (k10["status"] == "PASS") is ok, (role, slot, model, k10)
            if not ok:
                assert k10["code"] == "JOB-MODEL-MISMATCH"


# ================================================================ the verbs
def test_f13_start_records_the_model_and_refuses_an_unknown_alias(rec, capsys):
    code, body = _call(capsys, "start", "--runbook", RB, "--model", "opus")
    assert code == vc.PASS and body["start"]["model"] == "opus", body
    run = body["run"]
    assert rec.fake.starts[-1]["model"] == "opus"
    assert _ev(rec.root, events.RUN_START, run)[0]["data"]["model"] == "opus"
    tried = len(rec.fake.starts)
    for bad in ("gpt-9", "high", "Opus", "claude-opus-5-5"):
        code, got = _call(capsys, "start", "--runbook", RB, "--model", bad)
        assert code == vc.FAIL and got["code"] == "RUN-MODEL-UNKNOWN", (bad, got)
        assert bad in got["reason"] and "opus" in got["reason"]
    assert len(rec.fake.starts) == tried                 # no refusal reached open_run
    assert len(_refusals(rec.root, "RUN-MODEL-UNKNOWN")) == 4
    # without the flag the start is what it was: no model on the run, none in the answer
    _finalize(rec.root, run)
    code, body = _call(capsys, "start", "--runbook", RB)
    assert code == vc.PASS and "model" not in body["start"], body
    assert rec.fake.starts[-1]["model"] is None
    assert _ev(rec.root, events.RUN_START, body["run"])[0]["data"]["model"] is None


def test_f13_resume_and_extend_take_only_the_runs_own_model(rec, capsys):
    code, body = _call(capsys, "start", "--runbook", RB, "--model", "opus")
    run = body["run"]
    before = (len(_ev(rec.root, events.RESUME)), len(_ev(rec.root, events.EXTEND)),
              len(rec.fake.launches))
    for verb in ("resume", "extend"):
        code, got = _call(capsys, verb, run, "--model", "gpt-9")
        assert code == vc.FAIL and got["code"] == "RUN-MODEL-UNKNOWN", (verb, got)
        code, got = _call(capsys, verb, run, "--model", "sonnet")
        assert code == vc.FAIL and got["code"] == "RUN-MODEL-MISMATCH", (verb, got)
        assert "names opus" in got["reason"], got
    # a refusal changed nothing: no resume, no extension, no runner start
    assert (len(_ev(rec.root, events.RESUME)), len(_ev(rec.root, events.EXTEND)),
            len(rec.fake.launches)) == before
    assert len(_refusals(rec.root, "RUN-MODEL-MISMATCH")) == 2
    # the run's own model passes the check (the resume then restarts the runner as it would)
    code, got = _call(capsys, "resume", run, "--model", "opus")
    assert code == vc.PASS, got
    code, got = _call(capsys, "extend", run, "--model", "opus")
    assert got["code"] not in ("RUN-MODEL-UNKNOWN", "RUN-MODEL-MISMATCH"), got
    # a run started without the flag has no model to repeat
    _finalize(rec.root, run)
    code, body = _call(capsys, "start", "--runbook", RB)
    plain = body["run"]
    code, got = _call(capsys, "resume", plain, "--model", "opus")
    assert code == vc.FAIL and got["code"] == "RUN-MODEL-MISMATCH", got
    assert "no model override" in got["reason"]
    code, got = _call(capsys, "resume", "rr-404", "--model", "opus")
    assert got["code"] == "RESEARCH-RUN-UNKNOWN", got


# ================================================================ spend: the rate cards
def _result_naming(root, *models):
    conn = db.connect(root)
    try:
        events.emit(conn, events.JOB_RESULT, run="rr-001", ref="t-900", op=None, session="s",
                    data={"job": "j-x", "scope": "rr-001", "models_seen": list(models),
                          "agent_id": "a-x"})
    finally:
        conn.close()


def test_f13_the_rate_card_check_of_a_run_model_reads_that_model_alone(project, monkeypatch):
    root = kit.make_project(project, rounds=1, slots=2)
    session = rk.Session(monkeypatch, root)
    _result_naming(root, "claude-sonnet-9")              # an earlier result on a sonnet with no card
    assert [g["model"] for g in session_runtime.rate_card_gaps(root, "claude")] == ["claude-sonnet-9"]
    assert session_runtime.rate_card_gaps(root, "claude", models=["opus"]) == []
    assert [g["model"] for g in session_runtime.rate_card_gaps(root, "claude", models=["sonnet"])] \
        == ["claude-sonnet-9"]
    conn = db.connect(root)
    try:
        # a run whose every job runs on opus needs the opus cards alone
        assert session_runtime.drive_checks(conn, root, session.sid, level=6,
                                            model="opus")["runtime"] == "claude"
        for model in (None, "sonnet"):
            with pytest.raises(errors.ResearchRefusal) as exc:
                session_runtime.drive_checks(conn, root, session.sid, level=6, model=model)
            assert exc.value.reason == "RUN-RATECARD-MISSING", model
    finally:
        conn.close()


# ================================================================ a whole run, with and without the flag
@needs_jail
@pytest.mark.parametrize("model", [None, "opus"], ids=["no-flag", "opus"])
def test_f13_every_job_of_a_run_names_its_model_or_without_the_flag_its_tier(project, monkeypatch,
                                                                             model):
    """A whole run driven through the verbs: with --model opus every job of every role (the
    proposer, the implementer, the notes writer, the auditor, both verifiers of each pass and the
    report task's pair) names opus with tier override, next-job hands opus, every agent runs on
    opus and K10 accepts each; without the flag each job names its role's shield tier."""
    root, session = _new_run(project, monkeypatch, behavior=_winner)
    handed = []
    real = session.next_jobs

    def next_jobs(run, wait=0.5):
        rc, body = real(run, wait)
        if body and body.get("action") == "spawn":
            handed.extend(body["jobs"])
        return rc, body
    session.next_jobs = next_jobs
    body = session.start(kit.RUNBOOK, *(("--model", model) if model else ()))
    run = body["run"]
    assert body["start"].get("model") == model
    done = session.drive(run, timeout=900)
    assert done["action"] == "report", _why(root, run, done)
    assert kit.events_of(root, events.RUN_START, run)[0]["data"]["model"] == model
    issued = [e["data"] for e in kit.events_of(root, events.JOB) if e["data"]["scope"] == run]
    assert {d["role"] for d in issued} == {"proposer", "implementer", "notes", "auditor", "verifier"}
    assert {d["verifier_slot"] for d in issued if d["role"] == "verifier"} == {"v1", "v2"}
    # the launch of each job (the report task's pair names no run on its spec, so by job id)
    ids = {d["job"] for d in issued}
    launches = [e["data"] for e in kit.events_of(root, events.LAUNCH) if e["data"].get("job") in ids]
    results = kit.events_of(root, events.JOB_RESULT)
    # every job was accepted: K10 found each agent on the model its job names
    assert len(results) == len(issued) and kit.events_of(root, events.JOB_REFUSED) == []
    assert len(handed) == len(issued)
    if model:
        assert {(d["tier"], d["model"]) for d in issued} == {("override", "opus")}
        assert {d["model"] for d in launches} == {"opus"} and len(launches) == len(issued)
        # the report task's pair, issued after the finalize event, is the run's too
        report_task = kit.events_of(root, events.RUN_START, run)[0]["data"]["report_task"]
        pair = [d for d in issued if d["attempt"] == report_task]
        assert len(pair) == 2 and {(d["tier"], d["model"]) for d in pair} == {("override", "opus")}
        assert {e["model"] for e in handed} == {"opus"}
        seen = {m for r in results for m in r["data"]["models_seen"]}
        assert seen == {rk.MODELS["opus"]}, seen
        # the verifier pair keeps its rule: two jobs of one group, spawned together, two agents
        groups: dict = {}
        for e in handed:
            if e.get("group"):
                groups.setdefault(e["group"], []).append(e)
        assert groups and all(len(g) == 2 and all(x["spawn_together"] for x in g)
                              for g in groups.values()), groups
        agents = {r["data"]["job"]: r["data"]["agent_id"] for r in results}
        assert all(agents[g[0]["job"]] != agents[g[1]["job"]] for g in groups.values())
    else:
        for d in issued:
            want = session_runtime.resolve_model(root, "claude", d["role"],
                                                 d.get("verifier_slot") or d.get("slot"))
            assert (d["tier"], d["model"]) == want, d
        assert {(d["verifier_slot"], d["model"]) for d in issued if d["role"] == "verifier"} == \
            {("v1", "sonnet"), ("v2", "opus")}
        assert "override" not in {d["tier"] for d in issued}
