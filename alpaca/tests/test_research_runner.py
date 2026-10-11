"""The loop runner (op-009 t-100): alpaca/research/runner.py, lifecycle.py, proposal.py,
proofs.py, state.py, holds.py and spend.py, with the posture loops' bound and resume and
ops.open_op (design/research-engine/architecture.md sections 3.8, 4, 7, 8 and 10).

Done when (t-100's contract):
  * an integration test runs 2 rounds of 2 slots with scripted agents end to end
    (test_two_rounds_of_two_slots_end_to_end);
  * a killed run resumes without losing or duplicating attempts and takes its leases again
    (test_a_killed_run_resumes_without_losing_or_duplicating_attempts,
    test_a_resume_takes_the_repair_task_in_progress_again); a stop signal while a gate
    runs records no verdict for that gate and the resume runs it again, for an attempt and for
    the baseline (test_a_stop_signal_mid_gate_records_no_verdict_and_the_resume_runs_it_again,
    test_a_stop_signal_mid_baseline_gate_does_not_fail_the_run), and reaches the runner at once
    when another thread takes it
    (test_a_signal_another_thread_takes_reaches_the_main_thread_at_once);
  * the champion never lands on main (test_the_champion_never_lands_on_main);
  * a proposal that fails the proposal shape is recorded as broken: schema before any worktree is
    created (test_a_proposal_that_fails_its_shape_is_broken_before_any_worktree, which also
    feeds a reply nested too deep to parse, and the code table of
    test_proposal_check_names_every_code);
  * an infrastructure failure is retried up to the pack's count and recorded apart from the
    attempt's verdict; a quota refusal waits until the reset instant reset_wait.parse reads from
    what the refusal states
    (test_infrastructure_failures_are_retried_and_recorded_apart_from_the_verdict,
    test_a_quota_wait_folds_the_stated_reset_through_reset_wait,
    test_run_agent_skips_a_recorded_key_and_waits_the_folded_quota_reset); a lost clone is
    rebuilt from the record and recorded as class lost (test_a_lost_clone_is_rebuilt_and_recorded); a lost
    attempt worktree is rebuilt from its recorded diff
    (test_a_lost_attempt_worktree_is_rebuilt_from_its_diff);
  * an attempt task with no recorded diff, an unreadable one or two diffs is refused before
    scoring, with the attempt named (test_a_missing_changed_or_second_diff_is_refused_before_scoring);
  * every exit records a finalize event and the run summary, and an early exit keeps the runner
    log and the environment snapshot as run evidence (test_an_early_stop_records_finalize_summary_and_forensics,
    test_a_failed_baseline_records_finalize_and_forensics,
    test_an_error_in_the_loop_records_finalize_and_forensics);
  * the runner refuses to start below the declared free disk and records why
    (test_the_runner_refuses_to_start_below_the_declared_free_disk).
Fail cases: an attempt recorded without its gates and verdicts (checked in the integration test,
test_an_attempt_is_recorded_only_after_its_gates_and_verdicts and
test_a_stopped_attempt_is_recorded_with_its_gates_and_verdicts); a run that continues past N
without an extension (test_a_run_never_goes_past_n_without_an_extension).
Budgets (FR-041): test_launches_in_flight_reserve_their_share_of_the_dollar_cap,
test_parallel_slots_never_reserve_past_the_dollar_cap and
test_a_launch_that_ended_lost_is_unpriced_until_the_owner_grants.

The scripted agents and the project live in research_loopkit.py; the verifiers are the scripted
verifiers of test_research_verifier.py, which re-run every claim through the real verifier.serve
in bubblewrap. No test touches the network or starts an agent runtime. Tests that run a round
need bubblewrap and skip without it.
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from alpaca import db, ops
from alpaca.gates import verdict as vc
from alpaca.posture import loops
from alpaca.research import (errors, events, holds, jail, launcher, lifecycle, paths, proofs,
                             proposal, runner, spend, state)
from alpaca.research import procs
from alpaca.research.types import Category, ProposalField, RunContext
from alpaca.tests import research_loopkit as kit
from alpaca.tests.conftest import REPO
from alpaca.tests.test_research_gates import make_contract
from alpaca.tests.plain_op import plain_op

needs_jail = pytest.mark.skipif(not jail.available()[0],
                                reason="bubblewrap is not available on this host")


# ------------------------------------------------------------------------------ helpers
@pytest.fixture(autouse=True)
def _no_runner_thread_outlives_its_test():
    """A runner joins every thread it starts (slots, verifier pool, heartbeat) before it
    returns, so no thread of one run keeps writing after its test."""
    yield
    left = [t.name for t in threading.enumerate()
            if t.name.startswith(("slot-", "verify-", "heartbeat-"))]
    assert left == []


def _run(project, monkeypatch, *, script=None, rounds=2, slots=2, retries=2, usd=10, slow=None,
         **start):
    root = kit.make_project(project, rounds=rounds, slots=slots, retries=retries, usd=usd,
                            slow=slow)
    agents = kit.Agents(root, script)
    kit.install(monkeypatch, root, agents)
    got = kit.start(root, **start)
    return root, got["run"], agents


def _go(root, run):
    return runner.launch(root, run, foreground=True, session="test")


def _conn(root):
    return db.connect(root)


def _attempts(root, run):
    """task -> its research-attempt events."""
    out = {}
    for key, task in kit.tasks_of(root, run).items():
        if key.endswith(":report"):
            continue
        out[task] = kit.events_of(root, events.ATTEMPT, task)
    return out


def _task(root, run, round_n, slot):
    keys = kit.tasks_of(root, run)
    key = "%s:r%d:s%d" % (run, round_n, slot)
    if key not in keys:                                   # say why the round never opened
        print(open(os.path.join(root, ".alpaca", "research", run, "runner.log")).read())
        print(kit.events_of(root, events.FINALIZE, run))
    return keys[key]


def _git(root, *args):
    return kit.git(root, *args).strip()


# ------------------------------------------------------------------------------ integration
@needs_jail
def test_two_rounds_of_two_slots_end_to_end(project, monkeypatch):
    root, run, agents = _run(project, monkeypatch)
    out = _go(root, run)
    # at N rounds with the target unmet the run halts with an extension request, exit 3
    assert out["code"] == vc.PAUSED
    final = kit.events_of(root, events.FINALIZE, run)
    assert len(final) == 1 and final[0]["data"]["state"] == "halted"
    # a halt names no reason code on the finalize (section 8.3); its extension hold carries it
    assert "code" not in final[0]["data"] and final[0]["data"]["exit"] == 3
    assert kit.events_of(root, events.HOLD, run)[0]["data"]["code"] == "RUN-ROUNDS-REACHED"
    keys = kit.tasks_of(root, run)
    assert sorted(keys) == sorted(["%s:report" % run, "%s:r0:s0" % run] +
                                  ["%s:r%d:s%d" % (run, r, s) for r in (1, 2) for s in (0, 1)])
    # every attempt is one task with one research-attempt, recorded after its gates and verdicts
    conn = _conn(root)
    for task, recs in _attempts(root, run).items():
        assert len(recs) == 1, task
        rec = recs[0]
        for kind in (events.GATE, events.ACCEPT, events.TAMPER, events.DIFF):
            evs = [e for e in kit.events_of(root, kind, task)
                   if e["data"].get("side") in (None, "attempt")]      # not the verifiers' re-runs
            assert evs, (task, kind)
            assert max(e["id"] for e in evs) < rec["id"], (task, kind)
        assert rec["data"]["gates"] and rec["data"]["tamper"]["points"]
        assert rec["data"]["accept_event"] is not None
        # sealed, verified by both verifiers and moved to done through the done gate
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()[0] == "done"
        ends = kit.events_of(root, events.VERIFY_END, task)
        assert ends and ends[-1]["data"]["outcome"] == "verified"
    outcomes = {kit.tasks_of(root, run)["%s:r%d:s%d" % (run, r, s)]: None
                for r, s in ((0, 0), (1, 0), (1, 1), (2, 0), (2, 1))}
    for task in outcomes:
        outcomes[task] = _attempts(root, run)[task][0]["data"]["outcome"]
    assert outcomes[_task(root, run, 0, 0)] == "baseline"
    assert outcomes[_task(root, run, 1, 0)] == "improvement"
    assert outcomes[_task(root, run, 2, 0)] == "improvement"
    # one round event per round, the slot 0 winners merged into the research branch
    rounds = kit.events_of(root, events.ROUND, run)
    assert [r["data"]["round"] for r in rounds] == [0, 1, 2]
    merges = kit.events_of(root, events.MERGE, run)
    assert [(m["data"]["round"], m["data"]["attempt"]) for m in merges] == [
        (1, _task(root, run, 1, 0)), (2, _task(root, run, 2, 0))]
    assert rounds[2]["data"]["champion_metrics"]["value"]["median"] == 40.0
    assert rounds[2]["data"]["targets_met"] is False
    clone = os.path.join(root, ".alpaca", "research", run, "clones", "champion")
    assert _git(clone, "rev-parse", "research/%s" % run) == merges[-1]["data"]["commit"]
    assert _git(clone, "show", "research/%s:src/value.txt" % run) == "40"
    # the round barrier: every implementer launch of a round comes before its first attempt event
    for r in (1, 2):
        tasks = [_task(root, run, r, s) for s in (0, 1)]
        impl = [e["id"] for t in tasks for e in kit.events_of(root, events.LAUNCH, t)
                if e["data"]["role"] == "implementer"]
        att = [e["id"] for t in tasks for e in kit.events_of(root, events.ATTEMPT, t)]
        assert len(impl) == 2 and max(impl) < min(att)
    # lessons fed into the next round: round 2's proposers read round 1's notes lines
    r2 = [e for e in kit.events_of(root, events.LAUNCH, _task(root, run, 2, 0))
          if e["data"]["role"] == "proposer"][0]
    prompt = open(os.path.join(root, r2["data"]["prompt"]), encoding="utf-8").read()
    assert "round 1 slot 0 tried value 60" in prompt and "round 1 slot 1 tried value 80" in prompt
    r1 = [e for e in kit.events_of(root, events.LAUNCH, _task(root, run, 1, 0))
          if e["data"]["role"] == "proposer"][0]
    assert "tried value" not in open(os.path.join(root, r1["data"]["prompt"])).read()
    # the extension hold, its halt and the fold
    open_holds = holds.open_holds(conn, run)
    assert [h["gate"] for h in open_holds] == ["extension"]
    assert loops.is_halted(conn, run)
    fold = state.fold(conn, run)
    assert fold["state"] == "halted" and fold["round"] == 2
    assert fold["champion"]["metrics"] == {"value": 40.0} and fold["baseline"]["metrics"] == {"value": 100.0}
    assert fold["counts"]["improvement"] == 3 and fold["counts"]["baseline"] == 1
    assert fold["budget"]["usd_spent"] == pytest.approx(0.01 * 12)
    # the report task stays with its lease released (report.write is t-105's); its contract
    # carries every done-bar line of the design (section 8.2)
    report = keys["%s:report" % run]
    assert conn.execute("SELECT claimant FROM tasks WHERE id=?", (report,)).fetchone()[0] is None
    bar = kit.events_of(root, "task-contract", report)[-1]["data"]["done_bar"]
    assert "the report names the run's final state and its finalize event" in bar
    assert "every hold and grant of the run is in the report with its decision id" in bar
    # the attempt tasks: the design's title, and the category in the contract input
    base = _task(root, run, 0, 0)
    assert conn.execute("SELECT title FROM tasks WHERE id=?", (base,)).fetchone()[0] == \
        "%s r0 s0 improve attempt" % run
    inputs = kit.events_of(root, "task-contract", _task(root, run, 1, 1))[-1]["data"]["input"]
    assert "the category tidy" in inputs
    # one agent of each role per slot: 2 proposers, 2 implementers and 2 notes writers a round
    assert sorted(c[0] for c in agents.calls) == ["implementer"] * 4 + ["notes"] * 4 + ["proposer"] * 4
    conn.close()


@needs_jail
def test_the_champion_never_lands_on_main(project, monkeypatch):
    root, run, _agents = _run(project, monkeypatch, rounds=1)
    main_before = _git(root, "rev-parse", "main")
    branches_before = _git(root, "branch", "--list")
    assert _go(root, run)["code"] == vc.PAUSED
    merges = kit.events_of(root, events.MERGE, run)
    assert len(merges) == 1
    # the project's main checkout: the same commit, the same branches, the same bytes
    assert _git(root, "rev-parse", "main") == main_before
    assert _git(root, "branch", "--list") == branches_before
    assert _git(root, "status", "--porcelain", "--", "src") == ""
    assert open(os.path.join(root, "src", "value.txt")).read() == "100\n"
    # in the run's clone only the research branch moved; its main stays the base
    clone = os.path.join(root, ".alpaca", "research", run, "clones", "champion")
    start = kit.events_of(root, events.RUN_START, run)[0]["data"]
    assert _git(clone, "rev-parse", "main") == start["base_commit"]
    assert _git(clone, "rev-parse", "research/%s" % run) == merges[0]["data"]["commit"]
    assert merges[0]["data"]["research_branch"] == "research/%s" % run
    assert _git(clone, "remote") == ""


@needs_jail
def test_every_implemented_attempt_is_audited_from_its_saved_streams(project, monkeypatch):
    # check round s1 (design): t-095's audit reads the saved stream of every proposer and
    # implementer launch (verifier.md section 9.5). The kit's scripted launches save one, so the
    # real audit.run, with the scripted auditor, ends PASS and an accepted attempt can merge
    root, run, _agents = _run(project, monkeypatch, rounds=1)
    assert _go(root, run)["code"] == vc.PAUSED
    audits = kit.events_of(root, events.AUDIT_VERDICT)
    assert len(audits) == 2
    for audit in audits:
        d = audit["data"]
        assert (d["verdict"], d["voided"], d["blocked_by"]) == ("PASS", False, None), d
        assert d["fingerprint"]["ok"] and [x["role"] for x in d["launches"]] == [
            "proposer", "implementer"]
    assert len(kit.events_of(root, events.MERGE, run)) == 1


# ------------------------------------------------------------------------------ resume
@needs_jail
def test_a_killed_run_resumes_without_losing_or_duplicating_attempts(project, monkeypatch):
    root, run, _agents = _run(project, monkeypatch)
    env = dict(os.environ, PYTHONPATH=REPO)
    code = ("import sys; from alpaca.tests import research_loopkit as k; "
            "sys.exit(k.child_main(%r, %r, 'implementer', 1, 1))" % (root, run))
    child = subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True,
                           text=True, timeout=600)
    assert child.returncode == -9, child.stdout + child.stderr
    killed = _task(root, run, 1, 1)
    # the kill left no finalize and a free lock
    assert kit.events_of(root, events.FINALIZE, run) == []
    assert runner.is_alive(root, run) is False
    started = [e for e in kit.events_of(root, events.LAUNCH, killed) if e["data"]["role"] == "implementer"]
    assert [e["data"]["try"] for e in started] == [1]
    # the kill left the run's leases in place; let them lapse, as a resume after the lease span
    # finds them: the resume sweeps them and must take the round's open tasks again (section
    # 10.3 step 5, take_leases)
    conn = _conn(root)
    held = [row[0] for row in conn.execute(
        "SELECT id FROM tasks WHERE status='doing' AND claimant=?", (run,)).fetchall()]
    assert killed in held
    with db.transaction(conn):
        conn.execute("UPDATE tasks SET lease_until='2000-01-01T00:00:00+00:00' WHERE claimant=?",
                     (run,))
    conn.close()
    retaken = {}
    real_queue = runner._Runner.queue_earlier

    def queue_earlier(self):
        # after take_leases (the report and the current round's tasks) and queue_earlier (the
        # sealed tasks of earlier rounds it hands to the pool), before any round step runs
        real_queue(self)
        for task in held:
            row = self.conn.execute("SELECT status, claimant FROM tasks WHERE id=?",
                                    (task,)).fetchone()
            retaken[task] = (row["status"], row["claimant"])
    monkeypatch.setattr(runner._Runner, "queue_earlier", queue_earlier)
    # the resume
    out = _go(root, run)
    swept = [e["ref"] for e in kit.events_of(root, "lease-expired")]
    assert killed in swept
    assert retaken and all(v == ("doing", run) for v in retaken.values()), retaken
    assert out["code"] == vc.PAUSED
    runners = kit.events_of(root, events.RUNNER, run)
    assert [r["data"]["cause"] for r in runners] == ["start", "resume"]
    keys = kit.tasks_of(root, run)
    assert len(keys) == 6 and len(set(keys.values())) == 6            # one task per key
    for task, recs in _attempts(root, run).items():
        assert len(recs) == 1, task                                    # none lost, none twice
        assert len(kit.events_of(root, events.DIFF, task)) == 1, task
        assert len(kit.events_of(root, events.ACCEPT, task)) <= 1, task
    # the killed implementer's launch is recorded lost once (the scripted launcher writes that
    # event, as launcher.launch does on a resume, t-098), and the next try is a new key, which
    # is the runner's part
    lost = [e for e in kit.events_of(root, events.INFRA, killed) if e["data"]["class"] == "lost"]
    assert len(lost) == 1 and lost[0]["data"]["try"] == 1
    tries = [e["data"]["try"] for e in kit.events_of(root, events.LAUNCH, killed)
             if e["data"]["role"] == "implementer"]
    assert tries == [1, 2]
    ends = [e["data"]["key"] for e in kit.events_of(root, events.LAUNCH_END, killed)]
    assert len(ends) == len(set(ends))
    # its worktree was reclaimed before the new try, and a reopen point compared
    actions = [e["data"]["action"] for e in kit.events_of(root, events.WORKTREE, killed)]
    assert actions[:2] == ["create", "reclaim"]
    assert any(e["data"]["point"] == "reopen" for e in kit.events_of(root, events.TAMPER, killed))
    rounds = [e["data"]["round"] for e in kit.events_of(root, events.ROUND, run)]
    assert rounds == [0, 1, 2]
    steps = {}
    conn = _conn(root)
    for row in conn.execute("SELECT data FROM events WHERE kind='controller-step'").fetchall():
        d = json.loads(row[0])
        if d["verdict"] == 0:
            steps[(d["plan"], d["step"])] = steps.get((d["plan"], d["step"]), 0) + 1
    assert all(n == 1 for n in steps.values()), [k for k, n in steps.items() if n > 1]
    conn.close()


@needs_jail
def test_a_run_never_goes_past_n_without_an_extension(project, monkeypatch):
    root, run, _agents = _run(project, monkeypatch, rounds=1)
    assert _go(root, run)["code"] == vc.PAUSED
    assert [e["data"]["round"] for e in kit.events_of(root, events.ROUND, run)] == [0, 1]
    # a restart without the owner's extension halts again at once and opens no round 2
    assert _go(root, run)["code"] == vc.PAUSED
    assert kit.events_of(root, events.ROUND_OPEN, run)[-1]["data"]["round"] == 1
    assert len(kit.events_of(root, events.HOLD, run)) == 1              # the same hold
    finals = kit.events_of(root, events.FINALIZE, run)
    assert [f["data"]["state"] for f in finals] == ["halted", "halted"]
    # a research-extend whose extension hold is still open raises no bound: it halts again
    conn = _conn(root)
    hold = holds.open_holds(conn, run)[0]
    events.emit(conn, events.EXTEND, run=run, ref=run, op=None, session="owner", actor="owner",
                data={"decision": "d-test", "hold": hold["id"], "rounds_from": 1, "rounds_to": 2})
    conn.close()
    assert _go(root, run)["code"] == vc.PAUSED
    assert [e["data"]["round"] for e in kit.events_of(root, events.ROUND, run)] == [0, 1]
    # with the extension (research-extend rounds_to 2 and the hold lifted) round 2 runs
    conn = _conn(root)
    holds.lift_hold(conn, root, hold=hold["id"], decision="d-test", answer="extension",
                    session="owner")
    assert not loops.is_halted(conn, run)
    conn.close()
    assert _go(root, run)["code"] == vc.PAUSED
    assert [e["data"]["round"] for e in kit.events_of(root, events.ROUND, run)] == [0, 1, 2]
    assert len(holds.open_holds(_conn(root), run)) == 1
    # the owner answers the newer hold "stop": an extend citing it, with that decision or with
    # another one, raises nothing, so the restart halts at 2 rounds and asks again
    conn = _conn(root)
    newer = holds.open_holds(conn, run)[0]
    holds.lift_hold(conn, root, hold=newer["id"], decision="d-no", answer="stop",
                    session="owner")
    for decision in ("d-no", "d-other"):
        events.emit(conn, events.EXTEND, run=run, ref=run, op=None, session="owner",
                    actor="owner", data={"decision": decision, "hold": newer["id"],
                                         "rounds_from": 2, "rounds_to": 5})
    conn.close()
    assert _go(root, run)["code"] == vc.PAUSED
    assert [e["data"]["round"] for e in kit.events_of(root, events.ROUND, run)] == [0, 1, 2]
    third = holds.open_holds(_conn(root), run)
    assert len(third) == 1 and third[0]["id"] > newer["id"]
    # an extend citing the first hold again (lifted by d-test with "extension") raises the bound
    # to 5, but a newer extension hold is still open: no round runs past it
    conn = _conn(root)
    events.emit(conn, events.EXTEND, run=run, ref=run, op=None, session="owner", actor="owner",
                data={"decision": "d-test", "hold": hold["id"], "rounds_from": 2,
                      "rounds_to": 5})
    conn.close()
    assert _go(root, run)["code"] == vc.PAUSED
    assert [e["data"]["round"] for e in kit.events_of(root, events.ROUND, run)] == [0, 1, 2]
    assert [h["id"] for h in holds.open_holds(_conn(root), run)] == [third[0]["id"]]
    assert kit.events_of(root, events.FINALIZE, run)[-1]["data"]["state"] == "halted"


# ------------------------------------------------------------------------------ proposals
def test_proposal_check_names_every_code(tmp_path):
    c = make_contract(categories=(Category("small", "s"),), editable=("src/",),
                      protected=("src/secret.py",), proposal=(ProposalField("risk", "enum",
                                                                            values=("low", "high")),),
                      metrics=(kit_metric(),))
    good = {"attempt": "t-9", "title": "one", "category": "small", "hypothesis": "h",
            "files": ["src/a.py"], "expected": {"value": 3}, "risk": "low"}
    assert proposal.check(good, c, attempt="t-9", category="small") == []

    def codes(doc, **kw):
        return {f["code"] for f in proposal.check(doc, c, attempt=kw.get("attempt", "t-9"),
                                                  category=kw.get("category", "small"))}
    assert codes(dict(good, attempt="t-8")) == {"PROPOSAL-ATTEMPT-MISMATCH"}
    assert codes(dict(good, category="tidy")) == {"PROPOSAL-CATEGORY-MISMATCH"}
    assert codes({k: v for k, v in good.items() if k != "files"}) == {"PROPOSAL-FIELD-MISSING"}
    assert codes(dict(good, extra=1)) == {"PROPOSAL-FIELD-UNKNOWN"}
    assert codes(dict(good, expected={"nope": 1})) == {"PROPOSAL-FIELD-UNKNOWN"}
    assert codes(dict(good, expected={"value": "x"})) == {"PROPOSAL-FIELD-TYPE"}
    assert codes(dict(good, risk="huge")) == {"PROPOSAL-VALUE-UNKNOWN"}
    assert codes(dict(good, title="x" * 121)) == {"PROPOSAL-TOO-LONG"}
    for bad in (["/etc/passwd"], ["src/../x"], ["docs/a.md"], ["src/secret.py"]):
        assert codes(dict(good, files=bad)) == {"PROPOSAL-PATH-OUTSIDE"}, bad
    path = tmp_path / "p.json"
    path.write_text("[1, 2]")
    doc, found = proposal.load(str(path))
    assert doc is None and [f["code"] for f in found] == ["PROPOSAL-UNREADABLE"]
    path.write_text("x" * (proposal.MAX_BYTES + 1))
    assert proposal.load(str(path))[1][0]["code"] == "PROPOSAL-UNREADABLE"
    assert proposal.load(str(tmp_path / "none.json"))[1][0]["code"] == "PROPOSAL-UNREADABLE"
    got = proposal.parse(json.dumps(good), c, attempt="t-9", category="small",
                         path=str(tmp_path / "ok.json"))
    assert got.fields["title"] == "one" and len(got.sha256) == 64
    with pytest.raises(ValueError) as exc:
        proposal.parse("not: [json", c, attempt="t-9", category="small",
                       path=str(tmp_path / "bad.json"))
    assert exc.value.args[1][0]["code"] == "PROPOSAL-UNREADABLE"


def kit_metric():
    from alpaca.research.types import Metric
    return Metric(id="value", command="true", parser="markers", unit="n", direction="lower",
                  kind="deterministic", repeats=1, min_ok=1, margin=0, timeout_s=10)


@needs_jail
def test_a_proposal_that_fails_its_shape_is_broken_before_any_worktree(project, monkeypatch):
    nested = "[" * 3000 + "]" * 3000            # exhausts the YAML parser's recursion

    def script(role, research, spec):
        if role == "proposer" and research.get("slot") == 1:
            return {"final": json.dumps({"attempt": spec["attempt"], "title": "x"})}
        if role == "proposer" and research.get("slot") == 2:
            return {"final": nested}
        return {}
    root, run, agents = _run(project, monkeypatch, script=script, rounds=1, slots=3)
    assert _go(root, run)["code"] == vc.PAUSED
    bad = _task(root, run, 1, 1)
    props = kit.events_of(root, events.PROPOSAL, bad)
    assert len(props) == 1 and props[0]["data"]["valid"] is False
    assert props[0]["data"]["verdict"] == "FAIL"
    assert {p["code"] for p in props[0]["data"]["problems"]} == {"PROPOSAL-FIELD-MISSING"}
    # recorded broken: schema, with no worktree and no implementer
    assert kit.events_of(root, events.WORKTREE, bad) == []
    assert [c for c in agents.calls if c[1] == bad and c[0] == "implementer"] == []
    rec = kit.events_of(root, events.ATTEMPT, bad)[0]["data"]
    assert (rec["outcome"], rec["broken_class"]) == ("broken", "schema")
    diff = kit.events_of(root, events.DIFF, bad)[0]["data"]
    assert diff["empty"] is True and diff["reason"] == props[0]["id"]
    # a reply nested too deep to parse is one more broken (schema) proposal, never a run failure
    deep = _task(root, run, 1, 2)
    props = kit.events_of(root, events.PROPOSAL, deep)
    assert len(props) == 1 and props[0]["data"]["valid"] is False
    assert [p["code"] for p in props[0]["data"]["problems"]] == ["PROPOSAL-UNREADABLE"]
    assert kit.events_of(root, events.WORKTREE, deep) == []
    rec = kit.events_of(root, events.ATTEMPT, deep)[0]["data"]
    assert (rec["outcome"], rec["broken_class"]) == ("broken", "schema")
    assert [f["data"]["state"] for f in kit.events_of(root, events.FINALIZE, run)] == ["halted"]
    # negative: the other slot's valid proposal went on to a worktree
    good = _task(root, run, 1, 0)
    assert kit.events_of(root, events.PROPOSAL, good)[0]["data"]["valid"] is True
    assert kit.events_of(root, events.WORKTREE, good)[0]["data"]["action"] == "create"


# ------------------------------------------------------------------------------ infrastructure
@needs_jail
def test_infrastructure_failures_are_retried_and_recorded_apart_from_the_verdict(project, monkeypatch):
    # the refusal states its reset as a number of seconds, as a runtime may. The launcher records
    # that stated value unchanged as the research-infra wait_until (launcher._wait_until, and the
    # scripted launcher of research_loopkit the same), and "3" is no instant, so _Attempt.wait
    # would not wait on it: only run_agent folding it through quota_wait_until (reset_wait.parse
    # reads it as Retry-After) makes the retry wait the 3 seconds.
    seen = {}

    def script(role, research, spec):
        if role != "implementer" or research.get("round") != 1:
            return {}
        n = spec["try_n"]
        if research.get("slot") == 0:
            if n == 1:
                return {"outcome": "start-failed"}
            if n == 2:
                seen["quota"] = datetime.datetime.now(datetime.timezone.utc)
                return {"outcome": "quota", "reset_at": "3"}
            seen["third"] = datetime.datetime.now(datetime.timezone.utc)
            return {}
        return {"outcome": "crash"}                      # slot 1 fails every try
    root, run, agents = _run(project, monkeypatch, script=script, rounds=1, retries=2)
    assert _go(root, run)["code"] == vc.PAUSED
    ok, spent = _task(root, run, 1, 0), _task(root, run, 1, 1)
    infra = kit.events_of(root, events.INFRA, ok)
    assert [(e["data"]["class"], e["data"]["try"]) for e in infra] == [("start-failed", 1),
                                                                       ("quota", 2)]
    assert infra[1]["data"]["wait_until"] == "3"
    # the quota retry waited the 3 seconds the refusal stated, counted from after the refusal
    # (quota_wait_until keeps whole seconds, so the bound drops the refusal's microseconds)
    assert seen["third"] >= seen["quota"].replace(microsecond=0) + datetime.timedelta(seconds=3)
    # recorded apart from the verdict: the attempt still ran its gates and was accepted
    rec = kit.events_of(root, events.ATTEMPT, ok)[0]["data"]
    assert rec["outcome"] == "improvement"
    # retried up to the pack's count (2 retries, 3 tries), then abandoned with the class
    tries = [c[2] for c in agents.calls if c[1] == spent and c[0] == "implementer"]
    assert tries == [1, 2, 3]
    assert [e["data"]["class"] for e in kit.events_of(root, events.INFRA, spent)] == ["crash"] * 3
    rec = kit.events_of(root, events.ATTEMPT, spent)[0]["data"]
    assert (rec["outcome"], rec["reason"]) == ("abandoned", "crash")
    assert [e for e in kit.events_of(root, events.GATE, spent)
            if e["data"]["side"] == "attempt"] == []


@needs_jail
def test_a_lost_clone_is_rebuilt_and_recorded(project, monkeypatch):
    root, run, _agents = _run(project, monkeypatch, rounds=1)
    assert _go(root, run)["code"] == vc.PAUSED
    clone = os.path.join(root, ".alpaca", "research", run, "clones", "champion")
    head = _git(clone, "rev-parse", "research/%s" % run)
    kit.clean(clone)
    assert _go(root, run)["code"] == vc.PAUSED
    lost = [e for e in kit.events_of(root, events.INFRA, run) if e["data"]["class"] == "lost"]
    assert len(lost) == 1 and "rebuilt" in lost[0]["data"]["detail"]
    assert _git(clone, "rev-parse", "research/%s" % run) == head
    # negative: a resume that finds the clone as recorded rebuilds nothing
    assert _go(root, run)["code"] == vc.PAUSED
    assert len([e for e in kit.events_of(root, events.INFRA, run)
                if e["data"]["class"] == "lost"]) == 1
    # a clone path that is a link is removed, never followed, and the clone rebuilt
    decoy = os.path.join(project, "decoy")
    os.makedirs(decoy)
    kit.write(decoy, "keep.txt", "x\n")
    kit.clean(clone)
    os.symlink(decoy, clone)
    assert _go(root, run)["code"] == vc.PAUSED
    assert not os.path.islink(clone)
    assert _git(clone, "rev-parse", "research/%s" % run) == head
    assert os.listdir(decoy) == ["keep.txt"]


# ------------------------------------------------------------------------------ FR-018
@needs_jail
def test_a_missing_changed_or_second_diff_is_refused_before_scoring(project, monkeypatch):
    real_capture, real_gates = lifecycle._Attempt.capture, lifecycle._Attempt.gates

    def capture(self, conn):
        r = self.research(conn)
        if r.get("round") == 1 and r.get("slot") == 0:
            return vc.PASS                                # no research-diff at all
        return real_capture(self, conn)

    def gates(self, conn):
        code = real_gates(self, conn)
        r = self.research(conn)
        if r.get("round") == 1 and r.get("slot") == 1:    # the recorded file changed
            d = self.diffs(conn)[0]["data"]
            with open(lifecycle._abs(self.root, d["path"]), "ab") as fh:
                fh.write(b"# changed after capture\n")
        if r.get("round") == 1 and r.get("slot") == 2:    # a second diff event
            d = dict(self.diffs(conn)[0]["data"])
            for key in ("verdict", "exit", "run", "code"):
                d.pop(key, None)
            lifecycle._emit(conn, self.ctx, events.DIFF, self.task, code=vc.PASS, data=d)
        return code
    monkeypatch.setattr(lifecycle._Attempt, "capture", capture)
    monkeypatch.setattr(lifecycle._Attempt, "gates", gates)
    root, run, _agents = _run(project, monkeypatch, rounds=1, slots=3)
    assert _go(root, run)["code"] in (vc.PAUSED, vc.PASS)
    found = {}
    for s, want in ((0, 0), (1, 1), (2, 2)):
        task = _task(root, run, 1, s)
        refused = kit.events_of(root, events.DIFF_REFUSED, task)
        assert len(refused) == 1, task
        d = refused[0]["data"]
        assert d["attempt"] == "%s:r1:s%d" % (run, s) and d["step"] == "judge"
        assert d["found"] == want and d["verdict"] == "FAIL"
        found[s] = d
        # refused before scoring: no research-accept, and the attempt recorded broken (diff)
        assert kit.events_of(root, events.ACCEPT, task) == []
        rec = kit.events_of(root, events.ATTEMPT, task)[0]["data"]
        assert (rec["outcome"], rec["broken_class"]) == ("broken", "diff")
    assert found[1]["recorded_sha256"] != found[1]["actual_sha256"] and found[1]["actual_sha256"]
    # negative: the round 0 baseline, with its one intact diff, was scored
    base = _task(root, run, 0, 0)
    assert kit.events_of(root, events.DIFF_REFUSED, base) == []
    assert len(kit.events_of(root, events.ACCEPT, base)) == 1


@needs_jail
def test_an_attempt_is_recorded_only_after_its_gates_and_verdicts(project, monkeypatch):
    # a step that raises (the gate step of slot 0) ends its plan before `record`: no
    # research-attempt is written for it, and the run fails instead of recording it blind
    real = lifecycle._Attempt.gates

    def gates(self, conn):
        if self.research(conn).get("round") == 1 and self.research(conn).get("slot") == 0:
            raise RuntimeError("the gate jail is gone")
        return real(self, conn)
    monkeypatch.setattr(lifecycle._Attempt, "gates", gates)
    root, run, _agents = _run(project, monkeypatch, rounds=1)
    assert _go(root, run)["code"] == vc.FAIL
    task = _task(root, run, 1, 0)
    assert kit.events_of(root, events.ATTEMPT, task) == []
    final = kit.events_of(root, events.FINALIZE, run)[0]["data"]
    assert final["state"] == "failed" and "round 1" in final["reason"]
    # the sibling slot did reach its record, after its own gates and verdicts
    other = _task(root, run, 1, 1)
    rec = kit.events_of(root, events.ATTEMPT, other)
    assert len(rec) == 1
    assert max(e["id"] for e in kit.events_of(root, events.GATE, other)
               if e["data"]["side"] == "attempt") < rec[0]["id"]


# ------------------------------------------------------------------------------ finalize
@needs_jail
def test_an_early_stop_records_finalize_summary_and_forensics(project, monkeypatch):
    stopped = {}

    def script(role, research, spec):
        # what `alpaca research stop` does: record research-stop, then SIGTERM the runner
        if role == "proposer" and research.get("round") == 1 and not stopped:
            conn = db.connect(project)
            events.emit(conn, events.STOP, run=spec["run"], ref=spec["run"], op=None,
                        session="owner", actor="owner", data={"reason": "test stop", "final": False})
            conn.close()
            stopped["signalled"] = runner.request_stop(project, spec["run"])
        return {}
    root, run, _agents = _run(project, monkeypatch, script=script, rounds=2)
    out = _go(root, run)
    assert out["code"] == vc.PASS and stopped["signalled"] is True
    final = kit.events_of(root, events.FINALIZE, run)
    assert len(final) == 1
    d = final[0]["data"]
    assert d["state"] == "stopped" and d["exit"] == 0
    # the run summary is the fold
    assert d["summary"]["run"] == run and d["summary"]["counts"]["baseline"] == 1
    # the runner log and the environment snapshot, kept under forensics/
    assert d["log"] == ".alpaca/research/%s/runner.log" % run
    folder = os.path.join(root, d["forensics"])
    names = sorted(os.listdir(folder))
    assert "runner.log" in names and any(n.endswith(".json") for n in names)
    snap = json.load(open(os.path.join(root, d["env_snapshot"])))
    assert "PATH" in snap["env_names"] and snap["alpaca_version"]
    assert "runner start" in open(os.path.join(folder, "runner.log")).read()
    # nothing of round 2 opened, and the round 1 attempts moved to blocked with the reason
    assert [e["data"]["round"] for e in kit.events_of(root, events.ROUND_OPEN, run)] == [0, 1]
    conn = _conn(root)
    for s in (0, 1):
        task = _task(root, run, 1, s)
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()
        assert row[0] == "blocked"
        moves = kit.events_of(root, "task-move", task)
        assert moves[-1]["data"]["reason"] == "research run %s stopped" % run
    conn.close()


@needs_jail
def test_a_failed_baseline_records_finalize_and_forensics(project, monkeypatch):
    root = kit.make_project(project, rounds=1)
    os.remove(os.path.join(root, "src", "value.txt"))           # the build gate fails
    kit.git(root, "commit", "-qam", "no value")
    kit.install(monkeypatch, root, kit.Agents(root))
    run = kit.start(root)["run"]
    assert _go(root, run)["code"] == vc.FAIL
    d = kit.events_of(root, events.FINALIZE, run)[0]["data"]
    assert (d["state"], d["code"]) == ("failed", "RUN-BASELINE-FAILED")
    assert d["summary"]["baseline"]["metrics"] == {} and d["summary"]["run"] == run
    assert os.path.isfile(os.path.join(root, d["forensics"], "runner.log"))
    # a restart of the failed run fails again at once and opens no round 1
    assert _go(root, run)["code"] == vc.FAIL
    assert [e["data"]["round"] for e in kit.events_of(root, events.ROUND_OPEN, run)] == [0]


@needs_jail
def test_an_error_in_the_loop_records_finalize_and_forensics(project, monkeypatch):
    real = lifecycle.round_plan

    def plan(ctx, round_n, **kw):
        if round_n >= 1:
            raise RuntimeError("boom")
        return real(ctx, round_n, **kw)
    monkeypatch.setattr(lifecycle, "round_plan", plan)
    root, run, _agents = _run(project, monkeypatch, rounds=1)
    assert _go(root, run)["code"] == vc.FAIL
    finals = kit.events_of(root, events.FINALIZE, run)
    assert len(finals) == 1
    d = finals[0]["data"]
    assert d["state"] == "failed" and "RuntimeError: boom" in d["reason"] and d["exit"] == 1
    assert d["summary"]["counts"]["baseline"] == 1
    kept = sorted(os.listdir(os.path.join(root, d["forensics"])))
    assert "runner.log" in kept and len([n for n in kept if n.endswith(".json")]) == 1
    assert "error: RuntimeError: boom" in open(os.path.join(root, d["forensics"], "runner.log")).read()
    # the lock is dropped and the leases released at the exit
    assert runner.is_alive(root, run) is False
    conn = _conn(root)
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE claimant=?", (run,)).fetchone()[0] == 0
    conn.close()


@needs_jail
def test_the_run_start_records_the_runtime_mode_and_no_binary(project, monkeypatch):
    # the run-start event no longer names a runtime binary: the session spawns the agents itself,
    # so the version of the runtime is read from each job's transcript (research-job-result
    # runtime_version). The helper that reads a version from an install path stays for the
    # control runtime (in-session-runtime.md section 3.4)
    assert runner._runtime_version("claude", "/opt/claude/versions/2.1.7") == "2.1.7"
    assert runner._runtime_version("control", "/bin/true") is None
    assert runner._runtime_version("claude", None) is None
    root = kit.make_project(project, rounds=1)
    kit.install(monkeypatch, root, kit.Agents(root))
    got = kit.start(root)
    d = kit.events_of(root, events.RUN_START, got["run"])[0]["data"]
    assert d["runtime_version"] is None and d["binary"] is None
    assert d["runtime_mode"] == "test" and d["driver"] is None


@needs_jail
def test_the_runner_refuses_to_start_below_the_declared_free_disk(project, monkeypatch):
    root = kit.make_project(project, rounds=1, disk="1")
    kit.install(monkeypatch, root, kit.Agents(root))
    monkeypatch.setattr(runner, "_disk_free", lambda root, run=None: 1024)
    with pytest.raises(errors.ResearchRefusal) as exc:
        kit.start(root)
    assert exc.value.reason == "RUN-DISK-LOW" and exc.value.code == vc.BLOCKED
    assert "0.000 GiB free" in exc.value.detail and "declared 1.000 GiB" in exc.value.detail
    assert kit.events_of(root, events.RUN_START) == []
    # with room the run starts; the runner makes the same check at every start and records why
    monkeypatch.setattr(runner, "_disk_free", lambda root, run=None: 8 * 1024 ** 3)
    run = kit.start(root)["run"]
    monkeypatch.setattr(runner, "_disk_free", lambda root, run=None: 1024)
    assert _go(root, run)["code"] == vc.BLOCKED
    d = kit.events_of(root, events.FINALIZE, run)[0]["data"]
    assert (d["state"], d["code"], d["exit"]) == ("interrupted", "RUN-DISK-LOW", 2)
    assert "GiB free" in d["reason"] and d["forensics"]
    assert kit.events_of(root, events.ROUND_OPEN, run) == []
    # negative: with room again the same run goes on
    monkeypatch.setattr(runner, "_disk_free", lambda root, run=None: 8 * 1024 ** 3)
    assert _go(root, run)["code"] == vc.PAUSED


# ------------------------------------------------------------------------------ the parts
def _record(project):
    from alpaca import cli
    cli.main(["init"])
    plain_op("intent", "bar")
    return db.connect(project)


def test_loops_resume_lifts_a_halt_and_a_given_bound_is_used(project):
    conn = _record(project)
    for _ in range(2):
        loops.attempt(conn, "rr-001", "research-extension", verdict="targets-unmet")
    assert loops.exhausted(conn, "rr-001", "research-extension", root=project, bound=2) is True
    assert loops.exhausted(conn, "rr-001", "research-extension", root=project, bound=5) is False
    loops.halt(conn, "rr-001", "at N", instrument="research-extension", root=project, bound=2)
    rep = [e for e in db.events(conn, kind=loops.KIND_HALT)][-1]["data"]["report"]
    assert rep["bound"] == 2 and rep["count"] == 2
    assert loops.is_halted(conn, "rr-001") and loops.halted(conn) == ["rr-001"]
    assert loops.pad_lines(conn, root=project)
    got = loops.resume(conn, "rr-001", decision="d-1", hold=7, session="owner")
    assert got["decision"] == "d-1" and got["hold"] == 7
    assert not loops.is_halted(conn, "rr-001") and loops.halted(conn) == []
    assert loops.pad_lines(conn, root=project) == []
    # a halt after the resume is read again
    loops.halt(conn, "rr-001", "at N again", instrument="research-extension", root=project, emit=False)
    assert loops.is_halted(conn, "rr-001")


def test_open_op_records_what_op_new_records(project):
    conn = _record(project)
    oid = ops.open_op(conn, "research run rr-001: improve on x", "rr-001 finalized",
                      session="research-rr-001", actor="research-runner")
    assert oid == "op-002"
    ev = [e for e in db.events(conn, kind="op-open")][-1]
    assert (ev["actor"], ev["session"], ev["data"]["from_intent"]) == (
        "research-runner", "research-rr-001", None)
    assert db.rows(conn, "ops", "id=?", (oid,))[0]["status"] == "open"
    first = [e for e in db.events(conn, kind="op-open")][0]
    assert first["actor"] == "human" and first["op"] == "op-001"     # `op new` through open_op


def _fake_run(conn, run="rr-001", usd=1.0, tokens=1000, wall=None):
    events.emit(conn, events.RUN_START, run=run, ref=run, op="op-001", session="t",
                data={"mode": "improve", "budget": {"usd": usd, "tokens": tokens, "wall": wall},
                      "k": 2, "n": 1, "control": None, "base_commit": "b" * 40})
    tid = ops.add_task(conn, "op-001", "attempt", "attempt",
                       extra={"research": {"run": run, "role": "attempt", "key": run + ":r1:s0",
                                           "round": 1, "slot": 0}})
    return tid


def test_spend_check_holds_at_a_cap_until_a_grant_and_on_unpriced_spend(project):
    from alpaca.resilience import budget
    conn = _record(project)
    task = _fake_run(conn)
    got = spend.check(conn, project, "rr-001", role="proposer")
    assert got.posture == "GO" and got.usd_cap == 1.0 and got.tokens_cap == 1000
    spend.book(conn, "rr-001", task, cost_usd=1.5, tokens=10, session="s")
    spend.book(conn, "rr-001", task, cost_usd=None, tokens=10, session="s")   # books nothing
    assert len(db.events(conn, kind=budget.KIND_SPEND)) == 1
    got = spend.check(conn, project, "rr-001", role="proposer")
    assert got.posture == "HOLD" and "dollar cap" in got.reason and got.usd_spent == 1.5
    # the owner's terminal-confirmed grant raises the cap; an unconfirmed one does not
    for confirmed, usd in (("no", 50), ("terminal", 5)):
        events.emit(conn, events.GRANT, run=None, ref="rr-001", op=None, session="owner",
                    actor="owner", data={"ref": "rr-001", "gate": "budget", "confirmed": confirmed,
                                         "terms": {"usd": usd, "tokens": None, "wall": None}})
    got = spend.check(conn, project, "rr-001", role="proposer")
    assert got.posture == "GO" and got.usd_cap == 5
    # a launch that left spend nobody priced holds the run
    events.emit(conn, events.LAUNCH_END, run="rr-001", ref=task, op=None, session="s",
                code=vc.PASS, data={"key": "k", "tokens": 20, "priced": False, "outcome": "ok"})
    got = spend.check(conn, project, "rr-001", role="proposer")
    assert got.posture == "HOLD" and got.unpriced == 1 and got.tokens_spent == 20
    # outside a run only the project posture is asked
    assert spend.check(conn, project, None, role="verifier").posture == "GO"


def test_wall_time_counts_only_while_a_runner_lives(project):
    conn = _record(project)
    _fake_run(conn, wall=100)
    rows = [("research-runner", "2026-10-01T00:00:00+00:00"),
            ("research-finalize", "2026-10-01T00:01:00+00:00"),
            ("research-runner", "2026-10-01T05:00:00+00:00"),
            ("research-finalize", "2026-10-01T05:00:30+00:00")]
    for kind, ts in rows:
        db.append_event(conn, session="t", actor="t", kind=kind, ref="rr-001",
                        data={"run": "rr-001"}, clock=lambda ts=ts: ts)
    assert spend.wall_seconds(conn, "rr-001") == 90.0          # the parked hours do not count
    assert spend.check(conn, project, "rr-001", role="proposer").posture == "GO"
    db.append_event(conn, session="t", actor="t", kind="research-runner", ref="rr-001",
                    data={"run": "rr-001"}, clock=lambda: "2026-10-01T06:00:00+00:00")
    now = spend._parse_ts("2026-10-01T06:00:20+00:00")
    assert spend.wall_seconds(conn, "rr-001", now=now) == 110.0


def test_holds_open_once_lift_with_the_owner_and_settle_the_ship_card(project):
    from alpaca import questions, review
    conn = _record(project)
    _fake_run(conn)
    with pytest.raises(ValueError):
        holds.open_hold(conn, project, ref="rr-001", gate="extension", code="RUN-BUDGET-HOLD",
                        detail="x", op="op-001", bound=1, session="s")
    h = holds.open_hold(conn, project, ref="rr-001", gate="extension", code="RUN-ROUNDS-REACHED",
                        detail="1 round ran", op="op-001", bound=1, session="s")
    again = holds.open_hold(conn, project, ref="rr-001", gate="extension",
                            code="RUN-ROUNDS-REACHED", detail="again", op="op-001", bound=1,
                            session="s")
    assert again["id"] == h["id"] and len(kit.events_of(project, events.HOLD)) == 1
    ev = kit.events_of(project, events.HOLD)[0]["data"]
    assert (ev["code"], ev["verdict"], ev["exit"]) == ("RUN-ROUNDS-REACHED", vc.name_of(vc.PAUSED), 3)
    assert h["question"] and h["card"] and h["halt_event"] and loops.is_halted(conn, "rr-001")
    b = holds.open_hold(conn, project, ref="rr-001", gate="budget", code="RUN-BUDGET-HOLD",
                        detail="the dollar cap is reached", op="op-001", bound=None, session="s")
    assert [x["gate"] for x in holds.open_holds(conn, "rr-001")] == ["budget", "extension"]
    lifted = holds.lift_hold(conn, project, hold=h["id"], decision="d-9", answer="extension",
                             session="owner")
    assert lifted["lifted"]["decision"] == "d-9"
    assert questions._state(conn)[h["question"]]["status"] == "resolved"
    assert review._state(conn)[h["card"]]["status"] != "open"
    assert [x["gate"] for x in holds.open_holds(conn, "rr-001")] == ["budget"]
    assert holds.lift_hold(conn, project, hold=h["id"], decision="d-9", answer="extension",
                           session="owner")["lifted"]["event"] == lifted["lifted"]["event"]
    assert b["question"] != h["question"]
    # the ship hold: one per run, answered by the owner's card move, settled by the next verb
    s = holds.open_hold(conn, project, ref="rr-001", gate="ship", code="RUN-SHIP-DECISION",
                        detail="research/rr-001 at c0ffee", op="op-001", bound=None, session="s")
    assert s["question"] is None and review._state(conn)[s["card"]]["subject"] == "research:rr-001:champion"
    with pytest.raises(ValueError):
        holds.lift_hold(conn, project, hold=s["id"], decision="d", answer="x", session="owner")
    assert holds.settle_ship(conn, project, session="s") == []
    review.move(conn, s["card"], "adopt it", "owner")
    assert "ship" not in [x["gate"] for x in holds.open_holds(conn, "rr-001")]
    settled = holds.settle_ship(conn, project, session="s")
    assert len(settled) == 1 and settled[0]["lifted"]["answer"] == "adopt it"
    assert holds.settle_ship(conn, project, session="s") == []
    assert holds.open_hold(conn, project, ref="rr-001", gate="ship", code="RUN-SHIP-DECISION",
                           detail="y", op="op-001", bound=None, session="s")["id"] == s["id"]


def test_attempt_state_and_verdict_follow_the_record(project):
    conn = _record(project)
    task = _fake_run(conn)
    assert state.attempt_state(conn, task) == "planned"
    assert state.attempt_verdict(conn, task)["verdict"] is None
    for i, step in enumerate(("claim", "open-window", "propose", "check-proposer", "isolate")):
        db.append_event(conn, session="s", actor="controller", kind="controller-step",
                        ref="%s:%s" % (task, step),
                        data={"plan": task, "step": step, "index": i, "verdict": 0})
    assert state.attempt_state(conn, task) == "isolated"
    events.emit(conn, events.LAUNCH, run="rr-001", ref=task, op=None, session="s",
                data={"key": "%s/implementer/t1" % task, "role": "implementer"})
    assert state.attempt_state(conn, task) == "implementing"
    events.emit(conn, events.GATE, run="rr-001", ref=task, op=None, session="s", code=vc.FAIL,
                data={"stage": "build", "side": "attempt", "timed_out": False})
    got = state.attempt_verdict(conn, task)
    assert (got["verdict"], got["outcome"]) == ("rejected", "gate-failed")
    events.emit(conn, events.TAMPER, run="rr-001", ref=task, op=None, session="s", code=vc.FAIL,
                data={"point": "after-eval", "changed": ["contract:x"], "outside": []})
    got = state.attempt_verdict(conn, task)
    assert (got["verdict"], got["broken_class"]) == ("broken", "tamper")
    assert state.run_ids(conn) == ["rr-001"]
    with pytest.raises(KeyError):
        state.fold(conn, "rr-404")


def test_a_second_runner_exits_run_active_and_records_nothing(project, monkeypatch):
    import fcntl
    from alpaca.resilience import worker_launch
    takes = []
    real_acquire = worker_launch.ReentryLock.acquire

    def acquire(self):
        takes.append(str(self.lock_path))
        return real_acquire(self)
    monkeypatch.setattr(worker_launch.ReentryLock, "acquire", acquire)
    conn = _record(project)
    _fake_run(conn)
    lock = os.path.join(project, ".alpaca", "research", "rr-001", "runner.lock")
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    assert runner.is_alive(project, "rr-001") is False
    with open(lock, "a+b") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert runner.is_alive(project, "rr-001") is True
        assert runner.request_stop(project, "rr-001") is False      # no pid file names it
        monkeypatch.setattr(runner, "LOCK_RETRY_S", 0.3)
        before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert runner.main(["--run", "rr-001", "--root", project]) == vc.FAIL
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
        # the take is worker_launch.ReentryLock's, retried while the lock is held (section 4.2)
        assert len(takes) >= 2 and set(takes) == {lock}
    assert runner.is_alive(project, "rr-001") is False
    assert runner.main(["--nope"]) == vc.USAGE


def test_the_runner_reaps_orphans_on_a_thread_and_in_a_final_pass(project, monkeypatch):
    calls = []
    monkeypatch.setattr(procs, "reap_orphans", lambda: calls.append(1) or [4242])
    monkeypatch.setattr(runner, "_subreaper_flag", lambda: 1)
    monkeypatch.setattr(runner, "_REAP_S", 0.05)
    one = runner._Runner(project, "rr-001", foreground=True)
    one.start_reaper()
    try:
        assert _eventually(lambda: len(calls) >= 2)         # a daemon thread, every _REAP_S
    finally:
        one.stop.set()
        one.reaper.join(timeout=10)
    assert one.reaper.daemon and not one.reaper.is_alive()
    with open(paths.runner_log(project, "rr-001"), encoding="utf-8") as fh:
        assert "reaped 1 orphaned process(es): 4242" in fh.read()
    # a runner that ends runs one last pass before it drops the flag, even on a refusal
    calls.clear()
    monkeypatch.setattr(runner, "_subreaper_flag", lambda: 1)
    assert runner.main(["--run", "rr-404", "--root", project]) == vc.FAIL
    assert len(calls) >= 1
    # no pass at all when this process is not a subreaper
    calls.clear()
    monkeypatch.setattr(runner, "_subreaper_flag", lambda: 0)
    assert one.reap_pass() == [] and calls == []


def _eventually(check, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return check()


@needs_jail
def test_a_runner_starts_its_orphan_reaper_and_ends_with_a_final_pass(project, monkeypatch):
    started, passes = [], []
    real_start, real_pass = runner._Runner.start_reaper, runner._Runner.reap_pass
    monkeypatch.setattr(runner._Runner, "start_reaper",
                        lambda self: (started.append(1), real_start(self))[1])
    monkeypatch.setattr(runner._Runner, "reap_pass",
                        lambda self: (passes.append(1), real_pass(self))[1])
    root, run, _agents = _run(project, monkeypatch, rounds=1)
    assert _go(root, run)["code"] == vc.PAUSED
    assert started == [1] and len(passes) >= 1


@needs_jail
def test_a_foreground_runner_gives_back_the_subreaper_flag(project, monkeypatch):
    before = runner._subreaper_flag()
    root, run, _agents = _run(project, monkeypatch, rounds=1)
    assert _go(root, run)["code"] == vc.PAUSED
    assert runner._subreaper_flag() == before


# ------------------------------------------------------------------------------ stop signals
def _signal_when_measuring(root, run, round_n, slot):
    """A thread that sends SIGTERM to this process, the foreground runner, once a gate log of the
    attempt (round_n, slot) prints MEASURING: the operator's interrupt while that gate runs."""
    fired = {}

    def watch():
        deadline = time.time() + 300
        while time.time() < deadline:
            task = kit.tasks_of(root, run).get("%s:r%d:s%d" % (run, round_n, slot))
            folder = paths.attempt_dir(root, run, task) if task else None
            for dirpath, _dirs, files in os.walk(folder) if folder else ():
                for name in files:
                    if not name.endswith(".log"):
                        continue
                    try:
                        with open(os.path.join(dirpath, name), errors="replace") as fh:
                            text = fh.read()
                    except OSError:
                        continue
                    if "MEASURING" in text:
                        fired["log"] = os.path.join(dirpath, name)
                        os.kill(os.getpid(), signal.SIGTERM)
                        return
            time.sleep(0.05)
    th = threading.Thread(target=watch, name="watch-gate", daemon=True)
    th.start()
    return th, fired


def _metric_gates(root, task):
    return [e["data"] for e in kit.events_of(root, events.GATE, task)
            if e["data"]["stage"] == "metric:value" and e["data"]["side"] == "attempt"]


@needs_jail
def test_a_stop_signal_mid_gate_records_no_verdict_and_the_resume_runs_it_again(project,
                                                                                 monkeypatch):
    # EC-016: SIGTERM with no research-stop while the metric gate of r1 s0 (value 60) runs
    root, run, _agents = _run(project, monkeypatch, rounds=1, slow=60)
    th, fired = _signal_when_measuring(root, run, 1, 0)
    out = _go(root, run)
    th.join()
    assert fired["log"] and out["code"] == vc.BLOCKED
    states = [f["data"]["state"] for f in kit.events_of(root, events.FINALIZE, run)]
    assert states == ["interrupted"]
    task = _task(root, run, 1, 0)
    killed = _metric_gates(root, task)
    assert len(killed) == 1
    # the killed gate is recorded stopped with no verdict: BLOCKED, never FAIL, never reusable
    assert killed[0]["stopped"] is True and killed[0]["verdict"] == "BLOCKED"
    assert state.attempt_verdict(_conn(root), task)["verdict"] is None
    assert kit.events_of(root, events.ATTEMPT, task) == []
    # the gates step recorded no PASS, so the resume runs the gate again
    out = _go(root, run)
    assert out["code"] == vc.PAUSED
    got = _metric_gates(root, task)
    assert [g["verdict"] for g in got] == ["BLOCKED", "PASS"] and got[1]["reused"] is False
    rec = kit.events_of(root, events.ATTEMPT, task)
    assert len(rec) == 1 and rec[0]["data"]["outcome"] == "improvement"
    assert {"stage": "metric:value", "verdict": "PASS"} in rec[0]["data"]["gates"]
    row = _conn(root).execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()
    assert row[0] == "done"
    assert [f["data"]["state"] for f in kit.events_of(root, events.FINALIZE, run)] == [
        "interrupted", "halted"]


@needs_jail
def test_a_stop_signal_mid_baseline_gate_does_not_fail_the_run(project, monkeypatch):
    # the same interrupt during the round 0 baseline's metric gate (value 100): the run is
    # interrupted, never failed, and the resume measures the baseline again and runs round 1
    root, run, _agents = _run(project, monkeypatch, rounds=1, slow=100)
    th, fired = _signal_when_measuring(root, run, 0, 0)
    out = _go(root, run)
    th.join()
    assert fired["log"] and out["code"] == vc.BLOCKED
    final = kit.events_of(root, events.FINALIZE, run)
    assert [(f["data"]["state"], f["data"].get("code")) for f in final] == [("interrupted", None)]
    base = _task(root, run, 0, 0)
    assert kit.events_of(root, events.ATTEMPT, base) == []
    assert _go(root, run)["code"] == vc.PAUSED
    rec = kit.events_of(root, events.ATTEMPT, base)
    assert len(rec) == 1 and rec[0]["data"]["outcome"] == "baseline"
    assert [e["data"]["round"] for e in kit.events_of(root, events.ROUND, run)] == [0, 1]
    assert [f["data"]["state"] for f in kit.events_of(root, events.FINALIZE, run)] == [
        "interrupted", "halted"]


def test_a_signal_another_thread_takes_reaches_the_main_thread_at_once(project, monkeypatch):
    # the kernel hands a process signal to any thread that does not block it. When a slot thread
    # (or a pool worker) takes the runner's SIGTERM, the main thread, waiting on that thread in
    # the slots step (or in the pool's drain), must still run the handler at once, not when the
    # thread ends. The stand-in steps send SIGUSR1 to their own thread and wait for the handler.
    from alpaca.formation import controller
    conn = _record(project)
    events.emit(conn, events.RUN_START, run="rr-001", ref="rr-001", op="op-001", session="t",
                data={"mode": "improve", "k": 2, "n": 1, "control": None,
                      "budget": {"usd": 10, "tokens": None, "wall": None}})
    tasks = [ops.add_task(conn, "op-001", "a%d" % s, "a%d" % s, extra={"research": {
        "run": "rr-001", "role": "attempt", "key": "rr-001:r1:s%d" % s, "round": 1, "slot": s}})
        for s in (0, 1)]
    ctx = RunContext(root=project, run="rr-001", op="op-001", session="s",
                     contract=make_contract(slots=2), runtime="claude")
    sent, handled = [], []
    released = threading.Event()

    def from_a_thread():
        sent.append(time.monotonic())
        signal.pthread_kill(threading.get_ident(), signal.SIGUSR1)
        released.wait(5)
        return vc.PASS

    def handler(signum, frame):
        handled.append(time.monotonic())
        released.set()
    monkeypatch.setattr(lifecycle, "attempt_plan", lambda ctx, task: {"id": task})
    monkeypatch.setattr(controller, "run", lambda conn, plan, **kw: from_a_thread()
                        if plan["id"] == tasks[0] else vc.PASS)
    monkeypatch.setattr(lifecycle, "run_close", lambda conn, ctx, task: from_a_thread())
    old = signal.signal(signal.SIGUSR1, handler)
    try:
        assert lifecycle._Round(ctx, 1, None).slots(conn) == vc.PASS
        assert len(handled) == 1 and handled[0] - sent[0] < 2.0, (sent, handled)
        sent.clear(), handled.clear(), released.clear()
        pool = lifecycle.VerifierPool(ctx, 1)
        pool.put(tasks[0])
        pool.drain()
        pool.stop(drain=False)
        assert len(handled) == 1 and handled[0] - sent[0] < 2.0, (sent, handled)
    finally:
        signal.signal(signal.SIGUSR1, old)
    conn.close()


@needs_jail
def test_a_stopped_attempt_is_recorded_with_its_gates_and_verdicts(project, monkeypatch):
    # `alpaca research stop` after r1 s0's gates and accept ran, at its notes launch: row 57
    # records it abandoned (stopped) with every key of the record step, its gates first of all
    def script(role, research, spec):
        if role == "notes" and research.get("round") == 1 and research.get("slot") == 0:
            conn = db.connect(project)
            events.emit(conn, events.STOP, run=spec["run"], ref=spec["run"], op=None,
                        session="owner", actor="owner",
                        data={"reason": "test stop", "final": False})
            conn.close()
            runner.request_stop(project, spec["run"])
            return {"outcome": "crash"}           # the step then sees the stop before a retry
        return {}
    root, run, _agents = _run(project, monkeypatch, script=script, rounds=1)
    assert _go(root, run)["code"] == vc.PASS
    assert [f["data"]["state"] for f in kit.events_of(root, events.FINALIZE, run)] == ["stopped"]
    task = _task(root, run, 1, 0)
    rec = kit.events_of(root, events.ATTEMPT, task)
    assert len(rec) == 1
    d = rec[0]["data"]
    assert (d["outcome"], d["reason"], d["verdict"]) == ("abandoned", "stopped", "BLOCKED")
    assert d["gates"] == [{"stage": "build", "verdict": "PASS"},
                          {"stage": "metric:value", "verdict": "PASS"}]
    assert d["accept_event"] == kit.events_of(root, events.ACCEPT, task)[-1]["id"]
    assert "after-eval" in d["tamper"]["points"] and d["tamper"]["verdict"] == "PASS"
    diff = kit.events_of(root, events.DIFF, task)[0]["data"]
    assert d["diff"]["path"] == diff["path"] and d["diff"]["sha256"] == diff["sha256"]
    assert d["forked_from"] == diff["forked_from"] and d["head"] == diff["head"]
    assert d["metrics"]["value"]["median"] == 60.0 and d["launches"] >= 3
    assert d["alpaca_version"] and d["priced"] is True and d["cost_usd"] > 0
    row = _conn(root).execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()
    assert row[0] == "blocked"


# ------------------------------------------------------------------------------ a lost worktree
@needs_jail
def test_a_lost_attempt_worktree_is_rebuilt_from_its_diff(project, monkeypatch):
    # the worktree of r1 s0 is lost after its capture step: the purge step rebuilds it from the
    # recorded diff (research-worktree rebuild), and the gates run on the rebuilt tree
    real = lifecycle._Attempt.purge
    lost = {}

    def purge(self, conn):
        r = self.research(conn)
        if r.get("round") == 1 and r.get("slot") == 0 and not lost:
            wt = self.worktree(conn)
            lost["path"] = wt.path
            kit.clean(wt.path)
        return real(self, conn)
    monkeypatch.setattr(lifecycle._Attempt, "purge", purge)
    root, run, _agents = _run(project, monkeypatch, rounds=1)
    assert _go(root, run)["code"] == vc.PAUSED
    task = _task(root, run, 1, 0)
    diff = kit.events_of(root, events.DIFF, task)[0]["data"]
    trees = [e["data"] for e in kit.events_of(root, events.WORKTREE, task)]
    rebuilt = [t for t in trees if t["action"] == "rebuild"]
    assert len(rebuilt) == 1 and rebuilt[0]["head"] == diff["head"] and not rebuilt[0]["error"]
    purges = [e["data"] for e in kit.events_of(root, events.PURGE, task)
              if e["data"]["phase"] == "pre-eval"]
    assert len(purges) == 1 and purges[0]["rebuilt"] is True
    rec = kit.events_of(root, events.ATTEMPT, task)[0]["data"]
    assert rec["outcome"] == "improvement" and rec["head"] == diff["head"]
    # negative: the sibling slot kept its worktree and rebuilt nothing
    other = _task(root, run, 1, 1)
    actions = [e["data"]["action"] for e in kit.events_of(root, events.WORKTREE, other)]
    assert "rebuild" not in actions


# ------------------------------------------------------------------------------ quota waits
def test_a_quota_wait_folds_the_stated_reset_through_reset_wait():
    now = datetime.datetime(2026, 10, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
    fallback = "2026-10-01T12:05:00+00:00"
    # a stated reset instant is read as the provider's reset header
    assert lifecycle.quota_wait_until("2026-10-01T12:30:00Z", fallback, now=now) == \
        "2026-10-01T12:30:00+00:00"
    # a stated number of seconds is read as Retry-After
    assert lifecycle.quota_wait_until("90", fallback, now=now) == "2026-10-01T12:01:30+00:00"
    # nothing stated, or a value reset_wait.parse cannot read: the doubling fallback
    assert lifecycle.quota_wait_until(None, fallback, now=now) == fallback
    assert lifecycle.quota_wait_until("soon", fallback, now=now) == fallback
    got = datetime.datetime.fromisoformat(lifecycle._doubling(2))
    left = (got - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    assert 590 <= left <= 600
    assert lifecycle._doubling(9) and (datetime.datetime.fromisoformat(lifecycle._doubling(9))
                                       - datetime.datetime.now(datetime.timezone.utc)
                                       ).total_seconds() <= 3600


def test_run_agent_skips_a_recorded_key_and_waits_the_folded_quota_reset(project, monkeypatch):
    # run_agent itself, with a launch stand-in that would launch any key it is given (a replayed
    # one too): try 1 already failed before a restart, so it is skipped, not launched again; try
    # 2 is refused for quota with the reset stated as 90 seconds, which the launcher records
    # unchanged as wait_until (launcher._wait_until); the wait before try 3 is the instant
    # quota_wait_until folds from it, 90 s after the refusal
    c = make_contract(infra_retries=3)
    conn, task, a = _attempt_record(project, contract=c, research={
        "run": "rr-001", "role": "attempt", "key": "rr-001:r1:s0", "round": 1, "slot": 0})
    key1 = paths.launch_key(task, "implementer", 1)
    events.emit(conn, events.LAUNCH, run="rr-001", ref=task, op=None, session="s",
                data={"key": key1, "role": "implementer", "try": 1, "runtime": "claude"})
    events.emit(conn, events.LAUNCH_END, run="rr-001", ref=task, op=None, session="s",
                code=vc.BLOCKED, data={"key": key1, "role": "implementer", "try": 1,
                                       "outcome": "start-failed", "infra": "start-failed"})
    events.emit(conn, events.INFRA, run="rr-001", ref=task, op=None, session="s",
                code=vc.BLOCKED, data={"attempt": task, "role": "implementer", "try": 1,
                                       "class": "start-failed", "wait_until": None, "key": key1})
    launched, waits = [], []

    def stand_in(spec, **kw):
        launched.append(spec["try_n"])
        if spec["try_n"] == 2:
            events.emit(kw["conn"], events.INFRA, run="rr-001", ref=task, op=None, session="s",
                        code=vc.BLOCKED, data={"attempt": task, "role": "implementer",
                                               "try": 2, "class": "quota", "key": spec["key"],
                                               "wait_until": launcher._wait_until("90", 2)})
            return {"outcome": "quota", "infra": "quota", "reset_at": "90", "key": spec["key"]}
        return {"outcome": "ok", "key": spec["key"]}
    monkeypatch.setitem(lifecycle._hooks, "launch", stand_in)

    def wait(self, instant):
        waits.append((datetime.datetime.now(datetime.timezone.utc), instant))
        return True
    monkeypatch.setattr(lifecycle._Attempt, "wait", wait)

    def build(n):
        return {"run": "rr-001", "key": paths.launch_key(task, "implementer", n),
                "role": "implementer", "try_n": n, "runtime": "claude", "attempt": task,
                "session": "s"}
    how, result = a.run_agent(conn, "implementer", build)
    assert how == "ok" and result["outcome"] == "ok"
    assert launched == [2, 3]                               # the recorded try 1 is not relaunched
    (at, instant), = waits
    left = (datetime.datetime.fromisoformat(instant) - at).total_seconds()
    assert 88 <= left <= 90, (instant, at)
    conn.close()


# ------------------------------------------------------------------------------ budgets
def test_launches_in_flight_reserve_their_share_of_the_dollar_cap(project):
    conn = _record(project)
    task = _fake_run(conn, usd=1.0)                         # k 2: at most 6 launches in flight
    with spend.reserve(conn, project, "rr-001", "k1", role="proposer", runtime="claude",
                       slots=2) as (got, share):
        assert got.posture == "GO" and share == round(1.0 / 6, 6)
        seen = {}

        def other():
            own = db.connect(project)
            try:
                seen["reserved"] = spend._reserved(own, "rr-001")
                with spend.reserve(own, project, "rr-001", "k2", role="implementer",
                                   runtime="claude", slots=2) as (got2, share2):
                    seen["share"] = share2
            finally:
                own.close()
        th = threading.Thread(target=other)
        th.start()
        th.join()
        # the other thread counts this thread's share; its own share is taken from what is left
        assert seen["reserved"] == (share, 1)
        assert seen["share"] == round((1.0 - share) / 5, 6)
        # the launcher's own check in this thread does not count this thread's share
        assert spend.check(conn, project, "rr-001", role="proposer").posture == "GO"
    assert spend._reserved(conn, "rr-001") == (0.0, 0)
    # a launch on the record with no end reserves its max_usd
    events.emit(conn, events.LAUNCH, run="rr-001", ref=task, op=None, session="s",
                data={"key": "k1", "role": "proposer", "runtime": "claude", "max_usd": 0.4})
    assert spend.in_flight(conn, "rr-001") == {"k1": 0.4}
    spend.book(conn, "rr-001", task, cost_usd=0.6, tokens=1, session="s")
    got = spend.check(conn, project, "rr-001", role="implementer")
    assert got.posture == "HOLD" and "committed" in got.reason
    assert "0.4000 reserved by 1 launch(es)" in got.reason
    with spend.reserve(conn, project, "rr-001", "k3", role="implementer", runtime="claude",
                       slots=2) as (got, share):
        assert got.posture == "HOLD" and share is None
    # its end frees what it reserved
    events.emit(conn, events.LAUNCH_END, run="rr-001", ref=task, op=None, session="s",
                code=vc.PASS, data={"key": "k1", "tokens": 1, "priced": True, "outcome": "ok"})
    assert spend.in_flight(conn, "rr-001") == {}
    with spend.reserve(conn, project, "rr-001", "k3", role="implementer", runtime="claude",
                       slots=2) as (got, share):
        assert got.posture == "GO" and share == round(0.4 / 6, 6)
    # a control launch spends nothing and reserves nothing
    with spend.reserve(conn, project, "rr-001", "k4", role="proposer", runtime="control",
                       slots=2) as (got, share):
        assert got.posture == "GO" and share is None
    # the lifecycle's wrapper hands the share to the launch as max_usd, or holds it unlaunched
    calls = []
    ctx = RunContext(root=project, run="rr-001", op="op-001", session="s",
                     contract=make_contract(slots=2), runtime="claude")
    wrapped = lifecycle.reserving(ctx, lambda spec, **kw: calls.append(spec) or {"outcome": "ok"})
    spec = {"run": "rr-001", "key": "k5", "role": "proposer", "runtime": "claude",
            "session": "s", "try_n": 1, "attempt": task}
    assert wrapped(spec, conn=conn, root=project) == {"outcome": "ok"}
    assert calls[-1]["max_usd"] == round(0.4 / 6, 6)
    spend.book(conn, "rr-001", task, cost_usd=0.5, tokens=1, session="s")
    held = wrapped(dict(spec, key="k6"), conn=conn, root=project)
    assert held["held"] is True and held["outcome"] == "held" and len(calls) == 1
    conn.close()


def test_a_launch_that_ended_lost_is_unpriced_until_the_owner_grants(project):
    conn = _record(project)
    task = _fake_run(conn)
    for key, runtime, cls in (("k1", "claude", "lost"), ("k2", "control", "lost"),
                              ("k3", "claude", "quota")):
        events.emit(conn, events.LAUNCH, run="rr-001", ref=task, op=None, session="s",
                    data={"key": key, "role": "implementer", "runtime": runtime})
        events.emit(conn, events.INFRA, run="rr-001", ref=task, op=None, session="s",
                    code=vc.BLOCKED, data={"key": key, "role": "implementer", "try": 1,
                                           "class": cls, "wait_until": None})
    # the claude launch that ended lost ran and spent what nobody read: the run holds
    got = spend.check(conn, project, "rr-001", role="implementer")
    assert got.posture == "HOLD" and got.unpriced == 1 and "priced" in got.reason
    # the owner's terminal-confirmed budget grant acknowledges it
    events.emit(conn, events.GRANT, run=None, ref="rr-001", op=None, session="owner",
                actor="owner", data={"ref": "rr-001", "gate": "budget", "confirmed": "terminal",
                                     "terms": {"usd": 5, "tokens": None, "wall": None}})
    got = spend.check(conn, project, "rr-001", role="implementer")
    assert got.posture == "GO" and got.unpriced == 0
    # a later lost launch holds again
    events.emit(conn, events.LAUNCH, run="rr-001", ref=task, op=None, session="s",
                data={"key": "k4", "role": "notes", "runtime": "claude"})
    events.emit(conn, events.INFRA, run="rr-001", ref=task, op=None, session="s",
                code=vc.BLOCKED, data={"key": "k4", "role": "notes", "try": 1, "class": "lost",
                                       "wait_until": None})
    assert spend.check(conn, project, "rr-001", role="notes").unpriced == 1
    conn.close()


@needs_jail
def test_parallel_slots_never_reserve_past_the_dollar_cap(project, monkeypatch):
    # every launch reserves (no free runtime); each scripted launch books 0.01 USD against a
    # 0.035 USD cap, so round 1 runs into the cap and a budget hold opens
    monkeypatch.setattr(spend, "_FREE_RUNTIMES", ())
    root, run, _agents = _run(project, monkeypatch, rounds=1, usd="0.035")
    assert _go(root, run)["code"] == vc.PAUSED
    final = kit.events_of(root, events.FINALIZE, run)[-1]["data"]
    assert (final["state"], final["code"]) == ("paused", "RUN-BUDGET-HOLD")
    assert [h["gate"] for h in holds.open_holds(_conn(root), run)] == ["budget"]
    # at every launch, the booked spend and the shares of the launches in flight, its own
    # included, stay within the cap
    conn = _conn(root)
    rows = conn.execute("SELECT id, kind, ref, data FROM events WHERE kind IN ('research-launch', "
                        "'research-launch-end', 'research-infra', 'budget-spend') ORDER BY id"
                        ).fetchall()
    conn.close()
    flying, spent, shares = {}, 0.0, []
    for row in rows:
        d = json.loads(row["data"])
        if row["kind"] == "budget-spend":
            spent += float(d.get("amount") or 0)
        elif row["kind"] == "research-launch" and d.get("run") == run:
            assert d["max_usd"] is not None, d["key"]
            assert spent + sum(flying.values()) + d["max_usd"] <= 0.035 + 1e-9, d["key"]
            flying[d["key"]] = d["max_usd"]
            shares.append(d["max_usd"])
        elif d.get("run") == run:
            flying.pop(d.get("key"), None)
    # the two proposers of round 1 started side by side, each with a share, not the whole cap
    assert len(shares) >= 2 and max(shares) < 0.035 / 2


@needs_jail
def test_the_burn_in_state_decides_whether_a_real_run_starts(project, monkeypatch):
    from alpaca.research import controls
    root = kit.make_project(project, rounds=1)
    kit.install(monkeypatch, root, kit.Agents(root))
    for st, code in (("needed", "RESEARCH-CONTROL-REQUIRED"), (None, "RESEARCH-CONTROL-REQUIRED"),
                     ("failed", "RESEARCH-CONTROL-FAILED")):
        def burn_in(conn, contract_sha256, st=st):
            """The no-op burn-in of the contract (a stand-in)."""
            return {"state": st} if st else {}
        monkeypatch.setattr(controls, "burn_in", burn_in)
        with pytest.raises(errors.ResearchRefusal) as exc:
            kit.start(root)
        assert exc.value.reason == code, st
    assert kit.events_of(root, events.RUN_START) == []

    def done(conn, contract_sha256):
        """The no-op burn-in of the contract (a stand-in)."""
        return {"state": "done"}
    monkeypatch.setattr(controls, "burn_in", done)
    assert kit.start(root)["run"]


# ------------------------------------------------------------------------------ the parts, again
def _attempt_record(project, *, contract, research, run="rr-001"):
    """A record with run-start, one task with `research` and its worktree event."""
    conn = _record(project)
    events.emit(conn, events.RUN_START, run=run, ref=run, op="op-001", session="t",
                data={"mode": contract.mode, "k": 2, "n": 1,
                      "control": research.pop("control", None),
                      "budget": {"usd": 10, "tokens": None, "wall": None}, "base_commit": "b" * 40})
    tid = ops.add_task(conn, "op-001", "attempt", "attempt", extra={"research": research})
    ctx = RunContext(root=project, run=run, op="op-001", session="research-%s" % run,
                     contract=contract, runtime="claude")
    return conn, tid, lifecycle._Attempt(ctx, tid)


def test_the_pack_retries_and_timeouts_reach_the_verifier_and_the_audit(project, monkeypatch):
    from alpaca.research import audit, verifier
    from alpaca.research.types import AgentSettings
    c = make_contract(infra_retries=3, verifier_timeout_s=99,
                      agents=(AgentSettings("auditor", timeout_s=77),))
    conn, task, a = _attempt_record(project, contract=c, research={
        "run": "rr-001", "role": "attempt", "key": "rr-001:r1:s0", "round": 1, "slot": 0})
    events.emit(conn, events.LAUNCH, run="rr-001", ref=task, op=None, session="s",
                data={"key": "%s/proposer/t1" % task, "role": "proposer", "runtime": "control"})
    seen, launched = {}, []

    def stand_in(spec, **kw):
        launched.append(spec)
        return {"outcome": "ok"}
    monkeypatch.setitem(lifecycle._hooks, "verify_launch", stand_in)
    monkeypatch.setitem(lifecycle._hooks, "audit_launch", stand_in)

    def verify_run(conn, root, task, **kw):
        seen["verify"] = kw
        return {"state": "verified"}

    def audit_run(conn, root, attempt, **kw):
        """The audit (a stand-in)."""
        seen["audit"] = kw
        return {"state": "done"}
    monkeypatch.setattr(verifier, "run", verify_run)
    monkeypatch.setattr(audit, "run", audit_run)
    monkeypatch.setattr(lifecycle._Attempt, "point_step", lambda self, conn, point, **kw: vc.PASS)
    assert lifecycle.verify_task(conn, a.ctx, task) == vc.PASS
    assert (seen["verify"]["retries"], seen["verify"]["timeout_s"]) == (3, 99)
    assert a.audit(conn) == vc.PASS
    assert (seen["audit"]["retries"], seen["audit"]["timeout_s"]) == (3, 77)
    # both launches go through the run's reservation: the auditor gets the share as max_usd; a
    # verifier, which the budget check never holds, reserves no share (t-297)
    for name, role in (("verify", "verifier"), ("audit", "auditor")):
        seen[name]["launch"]({"run": "rr-001", "key": "%s-k" % name, "role": role,
                              "runtime": "claude"}, root=project)
    assert [s.get("max_usd") for s in launched] == [None, round(10 / 6, 6)]
    conn.close()


def test_a_control_launch_seed_is_the_contract_seed_plus_the_slot(project):
    c = make_contract(controls_seed=7)
    conn, task, a = _attempt_record(project, contract=c, research={
        "run": "rr-001", "role": "attempt", "key": "rr-001:r1:s1", "round": 1, "slot": 1,
        "control": "noop"})
    spec = a.spec(conn, "proposer", 1, cwd=project, workdir=project, prompt="p")
    assert (spec["control"], spec["control_seed"]) == ("noop", 8)
    conn.close()


def test_a_repair_fix_attempt_is_gated_on_its_base_tree_and_judged_by_the_repair_rule(
        project, monkeypatch):
    from alpaca.research import gates, isolation, repair
    from alpaca.research.types import PassToPass, WorktreeInfo, to_dict
    c = make_contract(mode="repair")
    conn = _record(project)
    events.emit(conn, events.RUN_START, run="rr-001", ref="rr-001", op="op-001", session="t",
                data={"mode": "repair", "k": 2, "n": 1, "control": None,
                      "budget": {"usd": 10, "tokens": None, "wall": None}})
    line = ops.add_task(conn, "op-001", "repair", "repair", extra={"research": {
        "run": "rr-001", "role": "repair", "key": "rr-001:repair:0", "case": "c1", "index": 0,
        "repair_task": "t-001"}})
    base_set = PassToPass(baseline=(), base_pass=("t1", "t2"), attempt_pass=("t1", "t2"),
                          lost=(), ok=True)
    # the section 8.3 shape: the PassToPass nests under pass_to_pass, beside the event's own
    # repair_task, ok and min_tests (the shape state.py's fold reads too)
    events.emit(conn, events.BASELINE, run="rr-001", ref=line, op=None, session="s",
                code=vc.PASS, data={"repair_task": line, "ok": True, "min_tests": 1,
                                    "pass_to_pass": to_dict(base_set)})
    # t-101: a fix is judged only against a reproducer proved before the fix attempt started
    events.emit(conn, events.REPRODUCER, run="rr-001", ref=line, op=None, session="s",
                code=vc.PASS, data={"repair_task": line, "case": "c1", "proved": True,
                                    "tests": ["r1"], "files": []})
    task = ops.add_task(conn, "op-001", "fix", "fix", extra={"research": {
        "run": "rr-001", "role": "attempt", "key": "rr-001:repair:0:r1:s0", "round": 1,
        "slot": 0, "mode": "repair", "repair_task": line}})
    ctx = RunContext(root=project, run="rr-001", op="op-001", session="s", contract=c,
                     runtime="claude", clone=object())
    a = lifecycle._Attempt(ctx, task)
    wt = WorktreeInfo(path=os.path.join(project, "wt"), label=task, commit="a" * 40, admin_dir=None)
    lifecycle._emit(conn, ctx, events.WORKTREE, task, data={"action": "create", "path": "wt",
                                                             "commit": "a" * 40, "error": None})
    made, destroyed, chains = [], [], []
    monkeypatch.setattr(repair, "case_base", lambda conn, ctx, t: "c" * 40)
    monkeypatch.setattr(isolation, "fresh_tree", lambda root, clone, **kw: made.append(kw) or
                        WorktreeInfo(path=kw["path"], label="base", commit=kw["commit"],
                                     admin_dir=None))
    monkeypatch.setattr(isolation, "destroy_worktree", lambda clone, t: destroyed.append(t.path))
    monkeypatch.setattr(isolation, "post_eval", lambda *a, **kw: type(
        "P", (), {"removed": (), "kept": (), "new": (), "changed": (), "nested": (), "ok": True})())
    monkeypatch.setattr(lifecycle._Attempt, "ensure_tree", lambda self, conn: wt)
    monkeypatch.setattr(lifecycle._Attempt, "point_step", lambda self, conn, point, **kw: vc.PASS)

    def run_chain(conn, root, **kw):
        chains.append(kw)
        ftp = {"ok": True, "base": {"r1": "FAIL"}, "attempt": {"r1": "PASS"}}
        for stage, data in (("fail-to-pass:c1", {"tests_named": ["r1"], "weak": [],
                                                 "fail_to_pass": ftp}),
                            ("pass-to-pass", {"pass_to_pass": {
                                "baseline": ["t1", "t2"], "base_pass": ["t1", "t2"],
                                "attempt_pass": ["t1", "t2"], "lost": [], "ok": True}})):
            events.emit(conn, events.GATE, run="rr-001", ref=task, op=None, session="s",
                        code=vc.PASS, data=dict(data, stage=stage, side="attempt"))
    monkeypatch.setattr(gates, "run_chain", run_chain)
    assert a.gates(conn) == vc.PASS
    # the chain ran with the case, the base tree cut at the case base and the base pass set
    kw = chains[0]
    assert kw["repair_case"] == "c1" and kw["baseline"] == base_set
    assert kw["base"].commit == "c" * 40 and made[0]["commit"] == "c" * 40
    assert destroyed == [kw["base"].path]                     # destroyed after the chain
    # judge: the repair rule over the recorded fail-to-pass and pass-to-pass results
    folder = a.attempt_dir()
    os.makedirs(folder, exist_ok=True)
    open(os.path.join(folder, "attempt.diff"), "wb").close()
    lifecycle._emit(conn, ctx, events.DIFF, task, code=vc.PASS, data={
        "path": os.path.relpath(os.path.join(folder, "attempt.diff"), project),
        "sha256": lifecycle._sha_file(os.path.join(folder, "attempt.diff")), "empty": True})
    assert a.judge(conn) == vc.PASS
    acc = kit.events_of(project, events.ACCEPT, task)[-1]["data"]
    assert (acc["phase"], acc["accepted"], acc["outcome"]) == ("repair", True, "fixed")
    conn.close()


def _repair_run(project, monkeypatch, n=2):
    """A started repair run of `n` repair tasks (the contract freeze made repair) and a runner
    object for it with its record connection and run start, never launched."""
    from alpaca.research import contract as contract_mod
    from alpaca.research.types import FailToPassSpec, RepairItem
    root = kit.make_project(project, rounds=1)
    kit.install(monkeypatch, root, kit.Agents(root))
    real = contract_mod.freeze

    def freeze(*a, **kw):
        frozen, fields = real(*a, **kw)
        items = tuple(RepairItem(index=i, case_id="c%d" % i, bug_report="pack/bug.md",
                                 fail_to_pass=FailToPassSpec(origin="store")) for i in range(n))
        return dataclasses.replace(frozen, mode="repair", repair=items), fields
    monkeypatch.setattr(contract_mod, "freeze", freeze)
    got = kit.start(root)
    r = runner._Runner(root, got["run"], foreground=True)
    r.conn = _conn(root)
    r.start = runner._run_start(r.conn, got["run"])["data"]
    return root, got, r


@needs_jail
def test_a_resume_takes_the_repair_task_in_progress_again(project, monkeypatch):
    # section 10.3 step 5: the repair task in progress (the first not done or blocked, which its
    # plan claimed) is taken again after the sweep; a later repair task nobody claimed is not
    from alpaca import claims
    root, got, r = _repair_run(project, monkeypatch)
    first, second = got["repair_tasks"]
    claims.take(r.conn, first, got["run"], 60, session=r.session, reason="repair")
    with db.transaction(r.conn):
        r.conn.execute("UPDATE tasks SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?",
                       (first,))
    r.take_leases()
    assert [e["ref"] for e in kit.events_of(root, "lease-expired")] == [first]
    row = r.conn.execute("SELECT status, claimant FROM tasks WHERE id=?", (first,)).fetchone()
    assert (row["status"], row["claimant"]) == ("doing", got["run"])
    assert r.status(second) == "open"
    r.conn.close()


@needs_jail
def test_a_stop_blocks_the_open_repair_tasks_of_a_repair_run(project, monkeypatch):
    # section 4.8: on a stop every planned task never finished moves to blocked; a repair run's
    # repair tasks are planned tasks, the report task is close_report's
    root, got, r = _repair_run(project, monkeypatch)
    first, second = got["repair_tasks"]
    conn = _conn(root)
    events.emit(conn, events.STOP, run=got["run"], ref=got["run"], op=None, session="owner",
                actor="owner", data={"reason": "test stop", "final": True})
    conn.close()
    # the first repair task already ended: the stop leaves it as it is
    from alpaca import cli
    assert cli.main(["--session", "t", "task", "move", first, "blocked", "--reason",
                     "an earlier end"]) == 0
    r.stop_cleanup()
    status = {t: r.status(t) for t in (first, second, got["report_task"])}
    assert status[second] == "blocked"
    assert status[got["report_task"]] not in ("done", "blocked")
    moves = [e for e in kit.events_of(root, "task-move", second)
             if e["data"].get("to") == "blocked"]
    assert len(moves) == 1 and moves[0]["data"].get("reason") == \
        "research run %s stopped" % got["run"]
    assert [e for e in kit.events_of(root, "task-move", first)
            if e["data"].get("reason") == "research run %s stopped" % got["run"]] == []
    r.conn.close()


@needs_jail
def test_a_repair_task_names_itself_as_its_line(project, monkeypatch):
    from alpaca.research import contract as contract_mod
    from alpaca.research.types import FailToPassSpec, RepairItem
    root = kit.make_project(project, rounds=1)
    kit.install(monkeypatch, root, kit.Agents(root))
    real = contract_mod.freeze

    def freeze(*a, **kw):
        frozen, fields = real(*a, **kw)
        item = RepairItem(index=0, case_id="c1", bug_report="pack/bug.md",
                          fail_to_pass=FailToPassSpec(origin="store"))
        return dataclasses.replace(frozen, mode="repair", repair=(item,)), fields
    monkeypatch.setattr(contract_mod, "freeze", freeze)
    got = kit.start(root)
    (task,) = got["repair_tasks"]
    conn = _conn(root)
    research = lifecycle._research(conn, task)
    assert (research["role"], research["repair_task"]) == ("repair", task)
    assert lifecycle._research(conn, got["report_task"])["repair_task"] is None
    conn.close()


def test_a_stopped_gate_is_recorded_blocked_and_never_reused(project):
    # gates.py: once the runner's stop signal killed a jail of the call, a record of the call is
    # BLOCKED with `stopped` true whatever the item computed, and _find_reusable skips it
    from alpaca.research import gates
    conn = _record(project)
    task = _fake_run(conn)
    cx = gates._Cx(conn, project, "rr-001", task, make_contract(), "s")
    tree = {"tree_sha256": "t" * 64, "tree_after_sha256": "t" * 64}
    cx.stopped = True
    gates._record(cx, item="build", side="attempt", verdict=vc.FAIL, detail="exit None",
                  fp="f" * 64, extra=dict(tree))
    d = kit.events_of(project, events.GATE, task)[-1]["data"]
    assert (d["verdict"], d["stopped"]) == ("BLOCKED", True) and "stop signal" in d["reason"]
    assert gates._find_reusable(cx, "build", "attempt", "f" * 64) is None
    assert state.attempt_verdict(conn, task)["verdict"] is None
    # negative: the same FAIL recorded with no stop is a verdict, and reusable
    cx.stopped = False
    gates._record(cx, item="build", side="attempt", verdict=vc.FAIL, detail="exit 1",
                  fp="f" * 64, extra=dict(tree))
    assert gates._find_reusable(cx, "build", "attempt", "f" * 64) is not None
    assert state.attempt_verdict(conn, task)["verdict"] == "rejected"
    conn.close()


# ------------------------------------------------------------------------------ t-351 F47: one clean tree per implementer try
#
# rr-021 attempt t-344: the second resume saw the second implementer try killed but skipped the
# reclaim because a reclaim of the first try existed, so the third try started on the file the second
# one wrote. The implement step now reclaims for the NEWEST killed launch, every try after the first
# in a step reclaims before its launch, and each reclaim takes its own reopen point. Since t-357 a
# reclaim cuts the next tree at its own name (a cut number above every recorded cut) and removes
# nothing (in-session-runtime.md section 28.2). These tests run
# the real `_Attempt.implement` and `run_agent` over an in-memory events table; the tree, the launch
# and the fingerprint are recorded instead of done.
import sqlite3                                                              # noqa: E402
import types                                                                # noqa: E402

from alpaca.research import isolation                                       # noqa: E402

_TASK = "t-100"


class _Tries:
    """An `_Attempt` over an in-memory record, and the log of what it did."""

    def __init__(self, tmp_path, monkeypatch, launches=()):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, ref TEXT, "
                          "op TEXT, session TEXT, data TEXT)")
        self.log = []
        wt = types.SimpleNamespace(path=str(tmp_path / "wt"), commit="c0ffee", admin_dir=None,
                                   private_objects=None, index=None, host_git=None, label=_TASK)
        a = lifecycle._Attempt.__new__(lifecycle._Attempt)
        a.ctx = types.SimpleNamespace(root=str(tmp_path), run="rr-000", clone=None, stop=None,
                                      contract=types.SimpleNamespace(infra_retries=3))
        a.task = _TASK
        a._research = {}
        a.verdict_set = lambda conn: False
        a.worktree = lambda conn: wt

        def record_tree(conn, action, w, **extra):
            self.log.append(("record_tree", action, extra.get("after_launch")))
            self.emit(events.WORKTREE, {"action": action, "path": "wt", "commit": w.commit, **extra})
        a.record_tree = record_tree
        a.tamper = lambda conn, point: lifecycle._Attempt.tamper(a, conn, point)
        a.take_point = lambda conn, point, **kw: self.log.append(("take_point", point)) or {}
        a.open_point = lambda: {}
        a.record_point = lambda conn, point, fp, **kw: self.emit(events.TAMPER, {"point": point})
        a.prompt = lambda *x, **k: "do it"
        a.repair_texts = lambda *x, **k: {}
        a.spec = lambda conn, role, try_n, **kw: {"key": paths.launch_key(_TASK, role, try_n)}
        queue = list(launches)

        def launch(conn, spec):
            lid = self.emit(events.LAUNCH, {"role": "implementer", "key": spec["key"]})
            self.log.append(("launch", spec["key"], lid))
            res = queue.pop(0) if queue else {"outcome": "ok"}
            if res.get("infra") == "lost":
                self.emit(events.INFRA, {"key": spec["key"], "class": "lost",
                                         "job": "j-" + spec["key"]})
            return res
        a.launch = launch
        self.attempt = a
        # since t-357 a reclaim cuts the next tree at its own name and removes nothing
        # (in-session-runtime.md section 28.2): `cuts` holds the cut of each create_worktree
        self.cuts = []
        monkeypatch.setattr(isolation, "reclaim", lambda *x, **k: self.log.append(("reclaim",)))
        monkeypatch.setattr(isolation, "create_worktree",
                            lambda *x, **k: self.log.append(("create_worktree",))
                            or self.cuts.append(k.get("cut")) or wt)

    def emit(self, kind, data):
        cur = self.conn.execute("INSERT INTO events (ts, kind, ref, op, session, data) VALUES (?,?,?,?,?,?)",
                                ("2026-10-07T00:00:00Z", kind, _TASK, None, "s", json.dumps(data)))
        return cur.lastrowid

    def killed_try(self, n, *, reclaimed=False):
        """try n launched and expired with its job lost; `reclaimed` adds the reclaim and the reopen
        point that the next resume recorded for it."""
        key = "%s/implementer/t%d" % (_TASK, n)
        lid = self.emit(events.LAUNCH, {"role": "implementer", "key": key})
        self.emit(events.INFRA, {"key": key, "class": "lost", "job": "%s-implementer-t%d" % (_TASK, n)})
        if reclaimed:
            self.emit(events.WORKTREE, {"action": "reclaim", "path": "wt", "commit": "c0ffee",
                                        "after_launch": lid})
            self.emit(events.TAMPER, {"point": "reopen"})
        return lid

    def run(self):
        self.attempt.implement(self.conn)
        return self.log

    def launches(self):
        return [i for i, e in enumerate(self.log) if e[0] == "launch"]


def _tries(tmp_path, monkeypatch, **kw):
    t = _Tries(tmp_path, monkeypatch, **kw)
    t.emit(events.WORKTREE, {"action": "create", "path": "wt", "commit": "c0ffee"})
    return t


def test_a_second_resume_reclaims_the_tree_of_the_second_killed_try(tmp_path, monkeypatch):
    t = _tries(tmp_path, monkeypatch)
    t.killed_try(1, reclaimed=True)
    second = t.killed_try(2)
    log = t.run()
    first_launch = t.launches()[0]
    assert log[first_launch][1] == "t-100/implementer/t3", log
    before = log[:first_launch]
    # a new cut above the recorded ones, and the old tree is not removed (section 28.2)
    assert ("reclaim",) not in before and ("create_worktree",) in before, log
    assert t.cuts == [2], t.cuts
    assert ("record_tree", "reclaim", second) in before, log
    # exactly one new cut before the third try: the in-step rule does not cut a second tree for it
    assert before.count(("create_worktree",)) == 1, log


def test_every_reclaim_takes_its_own_reopen_point(tmp_path, monkeypatch):
    t = _tries(tmp_path, monkeypatch)
    t.killed_try(1, reclaimed=True)                    # the first resume's reclaim and point
    t.killed_try(2)
    t.run()
    points = [e for e in t.conn.execute("SELECT data FROM events WHERE kind=?", (events.TAMPER,))
              if json.loads(e["data"])["point"] == "reopen"]
    assert len(points) == 2, "the second reclaim has no reopen point"


def test_the_first_killed_try_is_still_reclaimed_once(tmp_path, monkeypatch):
    t = _tries(tmp_path, monkeypatch)
    first = t.killed_try(1)
    log = t.run()
    assert log.count(("create_worktree",)) == 1 and ("record_tree", "reclaim", first) in log, log
    assert ("reclaim",) not in log and t.cuts == [2], (log, t.cuts)
    assert log[t.launches()[0]][1] == "t-100/implementer/t2"


def test_a_resume_that_already_reclaimed_the_newest_killed_try_does_not_cut_the_tree_again(
        tmp_path, monkeypatch):
    t = _tries(tmp_path, monkeypatch)
    t.killed_try(1, reclaimed=True)
    t.killed_try(2, reclaimed=True)
    log = t.run()
    assert ("reclaim",) not in log and ("create_worktree",) not in log, log
    assert log[t.launches()[0]][1] == "t-100/implementer/t3"


def test_an_in_step_retry_starts_on_a_reclaimed_tree_with_the_previous_launch_as_its_point(
        tmp_path, monkeypatch):
    t = _tries(tmp_path, monkeypatch, launches=[{"outcome": "infra", "infra": "lost"}, {"outcome": "ok"}])
    log = t.run()
    launches = t.launches()
    assert len(launches) == 2, log
    between = log[launches[0] + 1:launches[1]]
    assert ("reclaim",) not in between and ("create_worktree",) in between, log
    assert t.cuts == [2], t.cuts
    assert ("record_tree", "reclaim", log[launches[0]][2]) in between, log
    assert ("take_point", "reopen") in between, log


def test_each_reclaim_of_three_tries_is_named_for_the_launch_before_it(tmp_path, monkeypatch):
    t = _tries(tmp_path, monkeypatch, launches=[{"outcome": "infra", "infra": "lost"},
                                                {"outcome": "infra", "infra": "lost"},
                                                {"outcome": "ok"}])
    log = t.run()
    reclaims = [e[2] for e in log if e[0] == "record_tree"]
    launch_ids = [e[2] for e in log if e[0] == "launch"]
    assert len(launch_ids) == 3 and reclaims == launch_ids[:2], log


def test_the_first_try_of_a_step_cuts_no_extra_tree(tmp_path, monkeypatch):
    t = _tries(tmp_path, monkeypatch, launches=[{"outcome": "ok"}])
    log = t.run()
    assert ("create_worktree",) not in log and ("reclaim",) not in log, log
    assert [e[0] for e in log] == ["launch"]


def test_a_finished_try_is_not_reclaimed_on_replay(tmp_path, monkeypatch):
    t = _tries(tmp_path, monkeypatch)
    t.killed_try(1)
    lid = t.emit(events.LAUNCH, {"role": "implementer", "key": "t-100/implementer/t2"})
    t.emit(events.LAUNCH_END, {"role": "implementer", "key": "t-100/implementer/t2", "outcome": "ok"})
    # the step passes without a launch or a reclaim: the tree holds the accepted diff
    assert t.run() == []
    # and the in-step rule leaves the tree of a try whose end is recorded alone
    assert t.attempt._try_replayed(t.conn, 2) and not t.attempt._try_replayed(t.conn, 3)
    assert lid


def test_a_killed_try_with_no_infra_row_is_reclaimed_for_its_own_launch_and_not_twice(
        tmp_path, monkeypatch):
    # the runner died mid-launch: no end and no infra row; the same key is launched again
    t = _tries(tmp_path, monkeypatch)
    t.emit(events.LAUNCH, {"role": "implementer", "key": "t-100/implementer/t1"})
    t.emit(events.LAUNCH, {"role": "implementer", "key": "t-100/implementer/t2"})
    log = t.run()
    assert log.count(("create_worktree",)) == 1 and ("reclaim",) not in log, log
    assert log[t.launches()[0]][1] == "t-100/implementer/t1"
