"""t-105: the `alpaca research` verbs (alpaca/research/cli.py), the owner grant
(alpaca/research/owner.py), the run report (alpaca/research/report.py) and the contract catalog
(alpaca/research/catalog.py); design/research-engine/operator-surface.md sections 4.1 to 4.8.

Done when (the contract of t-105):

  * CLI tests for each verb: start, status, stop, extend, resume, report, grant, contracts and
    template each have a positive and a negative case below, and each verb's event is asserted
    on the record (research-run-start, research-status, research-stop, research-extend,
    research-resume, research-report, research-grant, research-refused);
  * `alpaca --help` lists them: `research` is in the top help and the nine sub-verbs in
    `alpaca research --help`;
  * surface.py registers them: surface.TABLE has the research row, the CLI dispatches it, the
    MANUAL names it, surface.lint is clean, and every top-level JSON key a verb prints is a
    surface key.

Fail case: extend without an owner-authorized record. `extend` exits 3 with a research-refused
event and the run stays halted, also when an owner-kind `go` decision with the grant's pointer
and actor owner was recorded through `alpaca decide` (which an agent can type), because no
terminal-confirmed research-grant names it.

The loop runner is faked behind runner.open_run and runner.launch (section 4.8): the fake
records the research-run-start an open_run records, or raises the refusal a test queues. Its
runs have no frozen contract folder, so the tests that read a run's round bound (extend and the
extension grant, through runner.rounds_bound) fake contract.read_frozen with _frozen. The
tests that need the real loader and preflight (a raised budget cap, the budget posture, the
concurrency tier) call the real open_run in a small git project and skip without bubblewrap. The
owner's terminal is a fake stdin whose isatty() is True and a patched input(); the agent markers
are cleared unless a test sets one. No test touches the network or launches an agent runtime.
"""
from __future__ import annotations

import argparse
import builtins
import fcntl
import json
import os
import socket
import sys

import pytest

from alpaca import cli, db, ops, review, surface, taskcontract, util
from alpaca.gates import verdict as vc
from alpaca.posture import level as level_mod, loops
from alpaca.research import (catalog, errors, events, holds, jail, owner, paths, report,
                             runner, state)
from alpaca.research import cli as research_cli
from alpaca.tests.conftest import REPO
from alpaca.tests.plain_op import plain_op

SESSION = "s-test"
RB = "pack/rb.yaml"
MINI = "runbook: 3\nid: mini\ntitle: mini\nresearch:\n  mode: improve\n"
needs_jail = pytest.mark.skipif(not jail.available()[0],
                                reason="bubblewrap is not available on this host")


# ------------------------------------------------------------------------------ fixtures
class FakeRunner:
    """runner.open_run and runner.launch without a loader, a jail or a process: open_run records
    the research-run-start the real one records (with a report task), or raises the refusal a
    test queued; launch records the call."""

    def __init__(self, root):
        self.root = root
        self.refusals = []
        self.starts = []
        self.launches = []
        self.mode = "improve"
        self.runtime_mode = "test"          # a session run's tests set "session", a headless one None
        self.foreground_code = vc.PASS

    def open_run(self, root, *, runbook, task, op, control, via, level, session, runtime=None,
                 model=None):
        self.starts.append({"runbook": runbook, "task": task, "op": op, "control": control,
                            "via": via, "level": level, "session": session, "model": model})
        if self.refusals:
            raise self.refusals.pop(0)
        conn = db.connect(root)
        try:
            run = "rr-%03d" % (len(state.run_ids(conn)) + 1)
            data = {"mode": self.mode, "runbook": runbook, "contract_sha256": "c" * 64,
                    "override_diff": [], "k": 2, "n": 2, "intent": "i-1",
                    "metrics": [{"id": "value", "unit": "count", "direction": "lower"}],
                    "targets": [{"metric": "value", "value": 10}],
                    "budget": {"usd": 10, "tokens": 1000, "wall": None}, "control": control,
                    "via": via, "runtime_mode": self.runtime_mode, "level": level,
                    "session": session, "base_commit": "b" * 40, "model": model,
                    "research_branch": "research/%s" % run, "repair_tasks": []}
            with db.transaction(conn):
                rtask = ops.add_task(conn, op or "op-001", "the report of %s" % run,
                                     "%s report" % run, session="research-%s" % run,
                                     actor=events.ACTOR,
                                     extra={"research": {"run": run, "role": "report",
                                                         "key": "%s:report" % run}})
                ev = events.emit(conn, events.RUN_START, run=run, ref=run, op=op or "op-001",
                                 session="research-%s" % run, in_txn=True,
                                 data=dict(data, report_task=rtask))
            return dict(json.loads(ev["data"]), run=run, op=op or "op-001", event=ev["id"])
        finally:
            conn.close()

    def launch(self, root, run, *, foreground, session):
        self.launches.append((run, bool(foreground)))
        out = {"pid": 4242, "host": socket.gethostname(), "log": paths.runner_log(root, run)}
        if foreground:
            out["code"] = self.foreground_code
        return out


class _Tty:
    def isatty(self):
        return True


def _write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


@pytest.fixture
def rec(project, monkeypatch):
    """A tmp project with a record, op-001, the format 3 runbook pack/rb.yaml, the fake runner
    and no agent marker set."""
    cli.main(["init"])
    plain_op("intent", "bar")
    _write(project, RB, MINI)
    fake = FakeRunner(project)
    monkeypatch.setattr(runner, "open_run", fake.open_run)
    monkeypatch.setattr(runner, "launch", fake.launch)
    for name in owner.AGENT_MARKERS:
        monkeypatch.delenv(name, raising=False)
    return argparse.Namespace(root=project, fake=fake)


def _call(capsys, *argv, json_out=True):
    """`alpaca --session s-test research <argv> [--json]`: (exit code, the printed JSON object or
    the text)."""
    capsys.readouterr()
    code = cli.main(["--session", SESSION, "research"] + list(argv) + (["--json"] if json_out
                                                                        else []))
    out = capsys.readouterr().out
    if not json_out:
        return code, out
    lines = [line for line in out.strip().splitlines() if line.startswith("{")]
    return code, (json.loads(lines[-1]) if lines else None)


def _owner(monkeypatch, typed):
    """The owner at an interactive terminal outside an agent session, typing `typed`."""
    for name in owner.AGENT_MARKERS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr(builtins, "input", lambda prompt="": typed)


def _conn(root):
    return db.connect(root)


def _ev(root, kind, ref=None):
    conn = _conn(root)
    try:
        return state._events_of(conn, kind, ref)
    finally:
        conn.close()


def _start(capsys, *extra):
    code, body = _call(capsys, "start", "--runbook", RB, *extra)
    assert code == vc.PASS, body
    return body["run"]


def _hold(root, run, gate="extension", *, finalize=True):
    """Put a run at a hold the way the runner does: the hold, then a finalize halted or
    paused."""
    conn = _conn(root)
    try:
        detail = {"extension": "round 2 of 2 done, target value 10 not met",
                  "budget": "spent 10.0 of 10.0 USD (reached), 10 of 1000 tokens; posture GO",
                  "ship": "research/%s at c0ffee" % run}[gate]
        hold = holds.open_hold(conn, root, ref=run, gate=gate, code=holds.HOLD_CODES[gate],
                               detail=detail, op="op-001",
                               bound=2 if gate == "extension" else None,
                               session="research-%s" % run)
        if finalize and gate != "ship":
            events.emit(conn, events.FINALIZE, run=run, ref=run, op="op-001",
                        session="research-%s" % run, code=vc.PAUSED,
                        reason=holds.HOLD_CODES[gate],
                        data={"state": "halted" if gate == "extension" else "paused",
                              "reason": detail})
        return hold
    finally:
        conn.close()


def _finalize(root, run, st="finished"):
    conn = _conn(root)
    try:
        events.emit(conn, events.FINALIZE, run=run, ref=run, op="op-001",
                    session="research-%s" % run, code=vc.PASS, data={"state": st, "reason": st})
    finally:
        conn.close()


def _refusals(root, code=None):
    got = _ev(root, events.REFUSED)
    return [e for e in got if code is None or e["data"].get("code") == code]


def _grant(capsys, monkeypatch, typed, *argv):
    _owner(monkeypatch, typed)
    return _call(capsys, "grant", *argv)


def _frozen(monkeypatch, *, rounds=None, error=None):
    """contract.read_frozen for the fake runner's runs: a contract whose rounds is `rounds`, or
    the run start's n when None; or the refusal `error`. Returns the list of runs it read."""
    from types import SimpleNamespace
    from alpaca.research import contract as contract_mod
    read = []

    def fake(root, run):
        read.append(run)
        if error is not None:
            raise error
        if rounds is not None:
            return SimpleNamespace(rounds=rounds)
        conn = _conn(root)
        try:
            return SimpleNamespace(rounds=state._events_of(conn, events.RUN_START, run)[0]
                                   ["data"]["n"])
        finally:
            conn.close()
    monkeypatch.setattr(contract_mod, "read_frozen", fake)
    return read


# ------------------------------------------------------------------------------ registration
def test_help_lists_research_and_its_nine_sub_verbs(capsys):
    import subprocess
    # the real entry point in a fresh interpreter, where only cli.VERB_MODULES registers the verb
    got = subprocess.run([sys.executable, "-m", "alpaca", "--help"], cwd=REPO,
                         capture_output=True, text=True, timeout=120)
    assert got.returncode == vc.PASS and "research" in got.stdout.split(), got.stderr
    assert cli.main(["--help"]) == vc.PASS
    assert "research" in capsys.readouterr().out
    assert cli.main(["research", "--help"]) == vc.PASS
    text = capsys.readouterr().out
    for verb in research_cli.VERBS:
        assert verb in text, verb
    assert len(research_cli.VERBS) == 11     # the nine verbs of the operator surface, next-job, submit
    # negative: a sub-verb the design lacks is a usage error
    assert cli.main(["research", "launch-now"]) == vc.USAGE


def test_surface_registers_research_and_the_lint_is_clean(monkeypatch):
    assert "research.cli" in cli.VERB_MODULES
    assert "research" in cli.registered_verbs()
    row = surface.TABLE["research"]
    assert row["group"] == "research" and "research" not in surface.FRONT
    assert "research" in surface.documented(REPO)
    assert [f for f in surface.lint(REPO) if f["verb"] == "research"] == []
    # negative: without the row the lint names the verb as undeclared
    table = dict(surface.TABLE)
    table.pop("research")
    monkeypatch.setattr(surface, "TABLE", table)
    assert [f for f in surface.lint(REPO) if f["verb"] == "research"]


def test_every_json_key_a_verb_prints_is_a_surface_key(rec, capsys, monkeypatch):
    keys = set(surface.TABLE["research"]["keys"])
    seen = []
    run = _start(capsys)
    for argv in (("status",), ("status", run), ("report", run), ("contracts",),
                 ("template", "--mode", "improve"), ("extend", run), ("stop", "rr-404")):
        seen.append(_call(capsys, *argv)[1])
    _hold(rec.root, run, "budget")
    seen.append(_call(capsys, "resume", run)[1])
    seen.append(_grant(capsys, monkeypatch, run, run, "stop")[1])
    seen.append(_call(capsys, "stop", run)[1])
    for body in seen:
        assert body is not None and set(body) <= keys, sorted(set(body) - keys)


# ------------------------------------------------------------------------------ start
def test_start_records_the_run_start_with_via_and_level(rec, capsys):
    code, body = _call(capsys, "start", "--runbook", RB, "--via", "autoresearch")
    assert code == vc.PASS and body["run"] == "rr-001" and body["decision"] is None
    assert body["start"]["via"] == "autoresearch" and body["start"]["level"] == "L2"
    start = _ev(rec.root, events.RUN_START, "rr-001")[0]["data"]
    assert (start["via"], start["level"], start["session"]) == ("autoresearch", "L2", SESSION)
    assert rec.fake.launches == [("rr-001", False)]
    # the level read is the session's: an owner level decision for the session moves it
    conn = _conn(rec.root)
    level_mod.set(conn, SESSION, 6, "the owner's goal", actor="owner", root=rec.root)
    conn.close()
    _finalize(rec.root, "rr-001")
    code, body = _call(capsys, "start", "--runbook", RB, "--foreground")
    assert code == vc.PASS and body["start"]["level"] == "L6" and body["start"]["via"] == "cli"
    assert rec.fake.launches[-1] == ("rr-002", True)
    # negative: a runbook that is not format 3 is refused and recorded
    _write(rec.root, "pack/old.yaml", "runbook: 2\nid: old\n")
    code, body = _call(capsys, "start", "--runbook", "pack/old.yaml")
    assert code == vc.FAIL and body["code"] == "RESEARCH-NOT-FORMAT-3"
    assert _refusals(rec.root, "RESEARCH-NOT-FORMAT-3")[-1]["ref"] == "contract:pack/old.yaml#-"


def test_start_takes_no_flag_that_sets_the_bar(rec, capsys):
    top = argparse.ArgumentParser()
    research_cli.parser(top.add_subparsers(dest="verb"))
    entry = top._subparsers._group_actions[0].choices["research"]
    start = entry._subparsers._group_actions[0].choices["start"]
    flags = {s for a in start._actions for s in a.option_strings}
    # --model names the one model of every agent job (F13); the bar is the contract's
    assert flags == {"-h", "--help", "--runbook", "--task", "--op", "--control", "--via",
                     "--model", "--foreground", "--json"}
    # negative: a flag that would set the bar is a usage error, and nothing starts
    for bad in (("--rounds", "9"), ("--target", "1"), ("--usd", "100"), ("--slots", "4")):
        assert cli.main(["research", "start", "--runbook", RB] + list(bad)) == vc.USAGE
    assert rec.fake.starts == []


def test_start_refused_before_the_noop_burn_in_and_recorded(rec, capsys):
    rec.fake.refusals.append(errors.refusal("RESEARCH-CONTROL-REQUIRED",
                                            "the no-op burn-in under contract cccc is needed"))
    code, body = _call(capsys, "start", "--runbook", RB)
    assert code == vc.FAIL and body["code"] == "RESEARCH-CONTROL-REQUIRED"
    got = _refusals(rec.root)
    assert len(got) == 1 and got[0]["data"]["verb"] == "start" and got[0]["data"]["exit"] == 1
    assert _ev(rec.root, events.RUN_START) == [] and rec.fake.launches == []
    # positive: the control run starts, and then a real run
    assert _call(capsys, "start", "--runbook", RB, "--control", "noop")[0] == vc.PASS
    assert rec.fake.starts[-1]["control"] == "noop"


def _loosened(ref, field, accepted, new):
    return errors.ContractRefusal(vc.PAUSED, "CONTRACT-LOOSEN-UNGRANTED",
                                  "%s %s: accepted %s, new %s" % (ref, field, accepted, new),
                                  loosened=[{"ref": ref, "field": field, "accepted": accepted,
                                             "new": new}])


def test_start_opens_a_loosen_hold_for_a_loosened_override_without_a_grant(rec, capsys):
    ref = "contract:%s#t-001" % RB
    rec.fake.refusals.append(_loosened(ref, "research.stop.slots", 2, 3))
    code, body = _call(capsys, "start", "--runbook", RB, "--task", "t-001")
    assert code == vc.PAUSED and body["code"] == "CONTRACT-LOOSEN-UNGRANTED"
    hold = _ev(rec.root, events.HOLD, ref)
    assert len(hold) == 1 and hold[0]["data"]["gate"] == "loosen"
    assert hold[0]["data"]["code"] == "CONTRACT-LOOSEN-UNGRANTED"
    assert "research.stop.slots: accepted 2, new 3" in hold[0]["data"]["detail"]
    assert "--set research.stop.slots=3" in hold[0]["data"]["detail"]
    refused = _refusals(rec.root, "CONTRACT-LOOSEN-UNGRANTED")
    assert len(refused) == 1 and refused[0]["id"] > hold[0]["id"]
    assert body["decision"]["id"] == hold[0]["id"]
    # negative: the same start again meets the open hold and opens no second one
    code, body = _call(capsys, "start", "--runbook", RB, "--task", "t-001")
    assert code == vc.PAUSED and body["code"] == "RESEARCH-HOLD-OPEN"
    assert len(_ev(rec.root, events.HOLD, ref)) == 1


def test_a_pack_edit_hold_sits_on_the_pack_ref_and_its_grant_lets_start_task_through(
        rec, capsys, monkeypatch):
    pack_ref = "contract:%s#-" % RB
    stage = {"id": "unit-pass", "run": "pytest -q", "timeout": "10m"}
    rec.fake.refusals.append(_loosened(pack_ref, "stages[unit-pass]", None, stage))
    code, body = _call(capsys, "start", "--runbook", RB, "--task", "t-001")
    assert code == vc.PAUSED
    assert [h["ref"] for h in _ev(rec.root, events.HOLD)] == [pack_ref]
    # negative: the grant of a pack form with --task is refused
    code, body = _grant(capsys, monkeypatch, RB, "--runbook", RB, "--task", "t-001", "loosen",
                        "--set", "stages[unit-pass]=%s" % util.canonical_json(stage))
    assert code == vc.FAIL and body["code"] == "RESEARCH-GRANT-TERMS"
    code, body = _grant(capsys, monkeypatch, RB, "--runbook", RB, "loosen",
                        "--set", "stages[unit-pass]=%s" % util.canonical_json(stage))
    assert code == vc.PASS, body
    code, body = _call(capsys, "start", "--runbook", RB, "--task", "t-001")
    assert code == vc.PASS, body
    lifted = _ev(rec.root, events.HOLD_LIFTED, pack_ref)
    run_start = _ev(rec.root, events.RUN_START)[0]
    assert len(lifted) == 1 and lifted[0]["id"] > run_start["id"]
    assert lifted[0]["data"]["answer"] == "loosen"


def test_a_granted_start_the_preflight_still_refuses_leaves_the_budget_hold_open(
        rec, capsys, monkeypatch):
    ref = "contract:%s#-" % RB
    rec.fake.refusals.append(errors.refusal("RUN-BUDGET-HOLD", "the project budget posture is "
                                            "HOLD"))
    code, body = _call(capsys, "start", "--runbook", RB)
    assert code == vc.PAUSED and body["code"] == "RUN-BUDGET-HOLD"
    hold = _ev(rec.root, events.HOLD, ref)[0]
    assert hold["data"]["gate"] == "budget" and hold["data"]["code"] == "RUN-BUDGET-HOLD"
    conn = _conn(rec.root)
    qid = hold["data"]["question"]
    from alpaca import questions
    assert questions._state(conn)[qid]["class"] == "owner-only"
    conn.close()
    # the owner's grant on the contract ref; the preflight still refuses: the hold stays open
    code, body = _grant(capsys, monkeypatch, RB, "--runbook", RB, "budget")
    assert code == vc.PASS, body
    rec.fake.refusals.append(errors.refusal("RUN-BUDGET-HOLD", "still HOLD"))
    code, body = _call(capsys, "start", "--runbook", RB)
    assert code == vc.PAUSED
    conn = _conn(rec.root)
    assert [h["id"] for h in holds.open_holds(conn, ref)] == [hold["id"]]
    conn.close()
    # positive: the next start goes through and lifts the hold after the run start
    code, body = _call(capsys, "start", "--runbook", RB)
    assert code == vc.PASS
    lifted = _ev(rec.root, events.HOLD_LIFTED, ref)
    assert len(lifted) == 1 and lifted[0]["id"] > _ev(rec.root, events.RUN_START)[0]["id"]


def test_start_meets_an_extension_hold_of_an_earlier_run_also_with_a_task(rec, capsys):
    run = _start(capsys)
    hold = _hold(rec.root, run)
    for extra in ((), ("--task", "t-001")):
        code, body = _call(capsys, "start", "--runbook", RB, *extra)
        assert code == vc.PAUSED and body["code"] == "RESEARCH-HOLD-OPEN"
        assert body["decision"]["id"] == hold["id"]
    assert len(_refusals(rec.root, "RESEARCH-HOLD-OPEN")) == 2
    assert len(rec.fake.starts) == 1


def test_start_refuses_a_busy_runbook_until_the_live_run_finalizes(rec, capsys):
    run = _start(capsys)
    code, body = _call(capsys, "start", "--runbook", RB)
    assert code == vc.FAIL and body["code"] == "RESEARCH-CONTRACT-BUSY"
    # a control run is another kind: it may start beside the live real run
    assert _call(capsys, "start", "--runbook", RB, "--control", "noop")[0] == vc.PASS
    _finalize(rec.root, run)
    assert _call(capsys, "start", "--runbook", RB)[0] == vc.PASS


def test_the_owners_stop_at_a_hold_refuses_new_real_runs_until_a_reopen(rec, capsys,
                                                                        monkeypatch):
    run = _start(capsys)
    _hold(rec.root, run)
    code, body = _grant(capsys, monkeypatch, run, run, "stop")
    assert code == vc.PASS and body["grant"]["applied"] is True
    code, body = _call(capsys, "start", "--runbook", RB)
    assert code == vc.PAUSED and body["code"] == "RESEARCH-RUNBOOK-STOPPED"
    # negative: reopen typed with a wrong confirmation records nothing
    code, body = _grant(capsys, monkeypatch, "wrong", "--runbook", RB, "reopen")
    assert code == vc.FAIL and body["code"] == "RESEARCH-OWNER-CONFIRM-MISMATCH"
    code, body = _grant(capsys, monkeypatch, RB, "--runbook", RB, "reopen")
    assert code == vc.PASS
    grant = [g for g in _ev(rec.root, events.GRANT) if g["data"]["gate"] == "reopen"]
    assert len(grant) == 1 and grant[0]["ref"] == RB
    assert _call(capsys, "start", "--runbook", RB)[0] == vc.PASS


# ------------------------------------------------------------------------------ start, real loader
def _real_project(project, monkeypatch, **kw):
    from alpaca.tests import research_loopkit as kit
    kit.make_project(project, **kw)
    cli.main(["init"])
    plain_op("intent", "bar")
    # no session is in use under pytest: the stand-in reads a test run (runtime_mode test), so no
    # pin, live marker or driver registration engages in a temporary project
    monkeypatch.setattr(runner, "_check_session_runtime",
                        lambda runtime, root: ("control" if runtime == "control" else "claude",
                                               "test"))
    monkeypatch.setattr(runner, "launch", FakeRunner(project).launch)
    # a real run waits for the no-op burn-in (t-102, tested in test_research_controls.py); these
    # tests start real runs to check other refusals, so the burn-in reads done
    from alpaca.research import controls
    monkeypatch.setattr(controls, "burn_in", lambda conn, sha: {"state": "done", "run": "rr-0",
                                                                   "rounds": 2, "of": 2})
    for name in owner.AGENT_MARKERS:
        monkeypatch.delenv(name, raising=False)
    return project


@needs_jail
def test_a_raised_budget_cap_needs_the_owners_loosen_grant(project, capsys, monkeypatch):
    root = _real_project(project, monkeypatch, usd=10)
    conn = _conn(root)
    tid = ops.add_task(conn, "op-001", "a research task", "research task", phase="build")
    taskcontract.record(conn, tid, {"done_bar": ["x"], "research": {"budget": {"usd": 20}}},
                        session="test", actor="human")
    conn.close()
    ref = "contract:%s#%s" % (RB, tid)
    code, body = _call(capsys, "start", "--runbook", RB, "--task", tid)
    assert code == vc.PAUSED and body["code"] == "CONTRACT-LOOSEN-UNGRANTED", body
    hold = _ev(root, events.HOLD, ref)
    assert len(hold) == 1
    assert "research.budget.usd: accepted 10, new 20" in hold[0]["data"]["detail"]
    # negative: a grant of another value does not let the start through
    code, _ = _grant(capsys, monkeypatch, RB, "--runbook", RB, "--task", tid, "loosen",
                     "--set", "research.budget.usd=15")
    assert code == vc.PASS
    code, body = _call(capsys, "start", "--runbook", RB, "--task", tid)
    assert code == vc.PAUSED
    code, _ = _grant(capsys, monkeypatch, RB, "--runbook", RB, "--task", tid, "loosen",
                     "--set", "research.budget.usd=20")
    assert code == vc.PASS
    code, body = _call(capsys, "start", "--runbook", RB, "--task", tid)
    assert code == vc.PASS, body
    start = _ev(root, events.RUN_START)[0]
    lifted = _ev(root, events.HOLD_LIFTED, ref)
    assert start["data"]["budget"]["usd"] == 20
    assert len(lifted) == 1 and lifted[0]["id"] > start["id"]


@needs_jail
def test_start_ignores_the_project_budget_posture_and_the_concurrency_tier(
        project, capsys, monkeypatch):
    """t-297 (in-session-runtime.md section 21): the project allowance never holds a research
    start; before t-297 a posture of HOLD opened a budget hold on the contract ref."""
    root = _real_project(project, monkeypatch)
    _write(root, "project.yaml", "name: loop-test\nresource_class: small\nbudget:\n  totals:\n"
           "    small: 10\n  reserve: 5\nconcurrency:\n  tiers:\n    small: 1\n")
    conn = _conn(root)
    for i in range(3):                                   # three live claims over a tier of 1
        tid = ops.add_task(conn, "op-001", "busy %d" % i, "busy %d" % i, phase="build")
        from alpaca import claims
        claims.take(conn, tid, "worker-%d" % i, minutes=60, session="s")
    from alpaca.resilience import budget
    assert budget.preflight(conn, root=root)["ok"] is False
    conn.close()
    code, body = _call(capsys, "start", "--runbook", RB)
    assert code == vc.PASS, body                   # the tier is not the start's check
    _finalize(root, body["run"])
    conn = _conn(root)
    budget.spend(conn, 6.0, ref="elsewhere")               # 4 left, under the reserve of 5
    conn.close()
    conn = _conn(root)
    assert budget.posture(conn, root=root) == budget.HOLD
    conn.close()
    code, body = _call(capsys, "start", "--runbook", RB)
    assert code == vc.PASS, body
    assert _ev(root, events.HOLD, "contract:%s#-" % RB) == []


# ------------------------------------------------------------------------------ status
def test_status_list_and_one_run_and_their_exit_codes(rec, capsys):
    a = _start(capsys)
    _finalize(rec.root, a)
    b = _start(capsys)
    _hold(rec.root, a, "ship")
    code, body = _call(capsys, "status")
    assert code == vc.PASS and [r["run"] for r in body["runs"]] == [b, a]
    assert body["runs"][1]["holds"] and body["level"] == "L2"
    assert body["level_source"] == "default"
    code, text = _call(capsys, "status", json_out=False)
    assert "level L2 (project default)" in text
    code, body = _call(capsys, "status", b)
    assert code == vc.PASS and body["run"]["state"] == "starting"
    assert body["run"]["runner"] == "not running"
    conn = _conn(rec.root)                       # a runner another host recorded
    events.emit(conn, events.RUNNER, run=a, ref=a, op="op-001", session="research-%s" % a,
                data={"pid": 99, "host": "elsewhere.invalid", "cause": "start"})
    conn.close()
    assert _call(capsys, "status", a)[1]["run"]["runner"] == "unknown"
    _hold(rec.root, b)
    code, body = _call(capsys, "status", b)
    assert code == vc.PAUSED and body["run"]["state"] == "halted"
    code, text = _call(capsys, "status", b, json_out=False)
    assert "extend --rounds <M>" in text and "GATE alpaca-research-status: PAUSED" in text
    # negative: an unknown run
    code, body = _call(capsys, "status", "rr-404")
    assert code == vc.FAIL and body["code"] == "RESEARCH-RUN-UNKNOWN"


def test_status_level_source_is_record_or_default_as_the_skill_reads_it(rec, capsys,
                                                                       monkeypatch):
    """level_source is `default` with no level decision of the session and `record` once the
    owner set one (operator-surface.md section 3.3, step 1); a level that cannot be read is
    neither word."""
    run = _start(capsys)
    code, body = _call(capsys, "status", run)
    assert (body["level"], body["level_source"]) == ("L2", "default")
    conn = _conn(rec.root)
    level_mod.set(conn, SESSION, 6, "the owner's goal", actor="owner", root=rec.root)
    conn.close()
    for argv in (("status",), ("status", run)):
        code, body = _call(capsys, *argv)
        assert (body["level"], body["level_source"]) == ("L6", "record"), argv
    code, text = _call(capsys, "status", run, json_out=False)
    first = text.splitlines()[0]
    assert "run level L2" in first and "level L6 (set by the owner)" in first
    code, text = _call(capsys, "status", json_out=False)
    assert "level L6 (set by the owner)" in text
    # a level that cannot be read: status still lists the runs, under neither word
    def broken(*_a, **_k):
        raise ValueError("unreadable")
    monkeypatch.setattr(level_mod, "in_force", broken)
    code, body = _call(capsys, "status")
    assert code == vc.PASS and body["level"] is None
    assert body["level_source"] not in ("record", "default")


def test_status_names_the_first_metric_of_the_run_start_list(rec, capsys, monkeypatch):
    """The headline metric is the first id of the run-start `metrics` list, as report.build
    reads it, not the first id in sorted order."""
    run = _start(capsys)
    real = research_cli._fold

    def fold(v, r):
        got = real(v, r)
        got["champion"] = {"metrics": {"aaa": 1, "value": 7}, "commit": "c" * 40,
                           "round": 1, "attempt": "t-9"}
        got["baseline"] = {"metrics": {"aaa": 2, "value": 9}, "commit": "b" * 40}
        return got
    monkeypatch.setattr(research_cli, "_fold", fold)
    code, body = _call(capsys, "status")
    row = body["runs"][0]
    assert (row["champion_metric"], row["champion_value"]) == ("value", 7)
    code, text = _call(capsys, "status", run, json_out=False)
    assert "champion 7 value" in text and "baseline 9;" in text and "target 10" in text
    # fallback: with no run-start metric in the fold, the sorted order
    assert research_cli._first_value({"champion": {"metrics": {"b": 1, "a": 2}}},
                                     "champion", ["zzz"]) == ("a", 2)


def test_status_writes_one_event_per_state_change_and_none_for_a_repeat(rec, capsys):
    run = _start(capsys)
    for _ in range(3):
        _call(capsys, "status", run)
    got = _ev(rec.root, events.STATUS, run)
    assert len(got) == 1 and got[0]["data"]["state"] == "starting"
    _hold(rec.root, run)
    _call(capsys, "status", run)
    _call(capsys, "status", run)
    got = _ev(rec.root, events.STATUS, run)
    assert [e["data"]["state"] for e in got] == ["starting", "halted"]
    assert got[1]["data"]["verdict"] == vc.name_of(vc.PAUSED)
    _call(capsys, "status")
    _call(capsys, "status")
    assert len(_ev(rec.root, events.STATUS, "-")) == 1


def test_status_of_a_failed_control_run_exits_1(rec, capsys):
    run = _start(capsys, "--control", "noop")
    conn = _conn(rec.root)
    tid = ops.add_task(conn, "op-001", "an attempt", "attempt",
                       extra={"research": {"run": run, "role": "attempt",
                                           "key": "%s:r1:s0" % run, "round": 1, "slot": 0}})
    events.emit(conn, events.ATTEMPT, run=run, ref=tid, op="op-001", session="t",
                data={"outcome": "improvement"})
    conn.close()
    code, body = _call(capsys, "status", run)
    assert code == vc.FAIL and body["run"]["control_failed"] is True
    # positive: the same run without an accepted attempt
    other = _start(capsys, "--control", "random")
    assert _call(capsys, "status", other)[0] == vc.PASS


def test_a_foreground_runner_reads_alive_while_it_holds_the_lock(rec, capsys):
    run = _start(capsys, "--foreground")
    lock = paths.runner_lock(rec.root, run)
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        code, body = _call(capsys, "status", run)
        assert body["run"]["runner"] == "alive"
        assert _call(capsys, "status")[1]["runs"][0]["runner"] == "alive"
        # resume refuses a live runner
        code, body = _call(capsys, "resume", run)
        assert code == vc.FAIL and body["code"] == "RESEARCH-RUN-ACTIVE"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert _call(capsys, "status", run)[1]["run"]["runner"] == "not running"
    assert _call(capsys, "status")[1]["runs"][0]["runner"] == "not running"


# ------------------------------------------------------------------------------ holds
def test_each_hold_halts_its_ref_on_the_pad_and_l6_may_end_its_turn(rec, capsys):
    from alpaca import questions
    run = _start(capsys)
    conn = _conn(rec.root)
    level_mod.set(conn, SESSION, 6, "the owner's goal", actor="owner", root=rec.root)
    assert loops.stop_should_refuse(conn, SESSION, root=rec.root)[0] is True
    conn.close()
    cases = [(run, "extension"), (run, "budget"), ("contract:%s#-" % RB, "loosen"),
             (run, "ship")]
    for ref, gate in cases:
        conn = _conn(rec.root)
        before = len([e for e in db.events(conn, kind="loop-halt", limit=10 ** 9)
                      if e["ref"] == ref])
        hold = holds.open_hold(conn, rec.root, ref=ref, gate=gate, code=holds.HOLD_CODES[gate],
                               detail="%s detail, cap 10 USD" % gate, op="op-001", bound=None,
                               session="t")
        halts = [e for e in db.events(conn, kind="loop-halt", limit=10 ** 9) if e["ref"] == ref]
        assert len(halts) == before + 1, gate
        assert any(ref in line for line in loops.pad_lines(conn, root=rec.root)), gate
        assert loops.stop_should_refuse(conn, SESSION, root=rec.root)[0] is False, gate
        if gate == "ship":
            assert hold["question"] is None
        else:
            q = questions._state(conn)[hold["question"]]
            assert q["class"] == ("stuck" if gate == "extension" else "owner-only")
            if gate == "budget":
                assert "cap 10 USD" in q["text"]
        conn.close()


def test_a_ship_hold_is_settled_by_the_next_verb_after_the_owners_card_move(rec, capsys):
    run = _start(capsys)
    hold = _hold(rec.root, run, "ship")
    conn = _conn(rec.root)
    review.move(conn, hold["card"], "adopt the champion", "owner")
    assert loops.is_halted(conn, run)
    conn.close()
    _call(capsys, "status")
    lifted = _ev(rec.root, events.HOLD_LIFTED, run)
    resumed = [e for e in _ev(rec.root, "loop-resume") if e["ref"] == run]
    assert len(lifted) == 1 and lifted[0]["data"]["answer"] == "adopt the champion"
    assert len(resumed) == 1
    conn = _conn(rec.root)
    assert not loops.is_halted(conn, run)
    conn.close()


# ------------------------------------------------------------------------------ stop
def test_stop_records_and_signals_a_live_runner(rec, capsys, monkeypatch):
    run = _start(capsys)
    signals = []
    monkeypatch.setattr(runner, "is_alive", lambda root, r: True)
    monkeypatch.setattr(runner, "_read_pid", lambda root, r: {"pid": 777,
                                                              "host": socket.gethostname()})
    monkeypatch.setattr(runner, "request_stop", lambda root, r: signals.append(r) or True)
    code, body = _call(capsys, "stop", run, "--reason", "enough")
    assert code == vc.PASS and signals == [run]
    stop = _ev(rec.root, events.STOP, run)[0]["data"]
    assert stop["final"] is False and stop["pid_signalled"] == 777
    assert stop["reason"] == "enough" and body["state"] == "stopping"


def test_stop_at_an_open_hold_inside_an_agent_session_waits_for_the_owner(rec, capsys,
                                                                         monkeypatch):
    run = _start(capsys)
    hold = _hold(rec.root, run)
    monkeypatch.setenv("CLAUDECODE", "1")
    code, body = _call(capsys, "stop", run)
    assert code == vc.PAUSED and body["code"] == "RESEARCH-OWNER-GRANT-MISSING"
    assert len(_refusals(rec.root, "RESEARCH-OWNER-GRANT-MISSING")) == 1
    assert _ev(rec.root, events.STOP) == []
    conn = _conn(rec.root)
    assert [h["id"] for h in holds.open_holds(conn, run)] == [hold["id"]]
    conn.close()
    # the owner at a terminal: a wrong confirmation is refused, the right one stops the run
    _owner(monkeypatch, "rr-999")
    code, body = _call(capsys, "stop", run)
    assert code == vc.FAIL and body["code"] == "RESEARCH-OWNER-CONFIRM-MISMATCH"
    _owner(monkeypatch, run)
    code, body = _call(capsys, "stop", run)
    # on exit 0, `decision` is null: it names only the open hold of an exit 3 (section 4.2)
    assert code == vc.PASS and body["state"] == "stopped" and body["decision"] is None
    stop = _ev(rec.root, events.STOP, run)[-1]["data"]
    grant = _ev(rec.root, events.GRANT, run)[-1]["data"]
    assert stop["final"] is True and stop["decision"] and stop["decision"] == grant["decision"]
    assert _ev(rec.root, events.HOLD_LIFTED, run)[-1]["data"]["answer"] == "stop"


def test_stop_with_no_runner_alive_is_final_and_a_stopped_run_is_never_resumed(rec, capsys):
    run = _start(capsys)
    code, body = _call(capsys, "stop", run)
    assert code == vc.PASS and body["state"] == "stopped"
    assert _ev(rec.root, events.STOP, run)[0]["data"]["final"] is True
    code, body = _call(capsys, "resume", run)
    assert code == vc.FAIL and body["code"] == "RESEARCH-RUN-FINISHED"
    # negative: stopping it again
    code, body = _call(capsys, "stop", run)
    assert code == vc.FAIL and body["code"] == "RESEARCH-RUN-FINISHED"


@pytest.mark.parametrize("gate", ["extension", "budget"])
def test_the_owners_stop_grant_at_a_hold_ends_the_run_stopped(rec, capsys, monkeypatch, gate):
    run = _start(capsys)
    _hold(rec.root, run, gate)
    code, body = _grant(capsys, monkeypatch, run, run, "stop")
    assert code == vc.PASS and body["grant"]["applied"] is True
    grant_id = body["grant"]["event"]
    code, body = _call(capsys, "status", run)
    assert body["run"]["state"] == "stopped" and code == vc.PASS
    code, body = _call(capsys, "resume", run)
    assert code == vc.FAIL and body["code"] == "RESEARCH-RUN-FINISHED"
    after = [e for kind in (events.RESUME, events.LAUNCH) for e in _ev(rec.root, kind)
             if e["id"] > grant_id]
    assert after == [] and rec.fake.launches == [(run, False)]


def test_a_run_lifted_by_extend_no_resume_then_stopped_reads_stopped(rec, capsys,
                                                                    monkeypatch):
    _frozen(monkeypatch)
    run = _start(capsys)
    _hold(rec.root, run)
    assert _grant(capsys, monkeypatch, run, run, "extend", "--rounds", "4",
                  "--no-apply")[0] == vc.PASS
    code, body = _call(capsys, "extend", run, "--no-resume")
    assert code == vc.PASS and body["state"] == "resumable"
    code, body = _call(capsys, "stop", run)
    assert code == vc.PASS and body["state"] == "stopped"
    assert _call(capsys, "resume", run)[1]["code"] == "RESEARCH-RUN-FINISHED"


# ------------------------------------------------------------------------------ extend
def test_extend_without_an_owner_record_exits_3_and_the_run_stays_halted(rec, capsys):
    """The fail case of t-105 (EC-022)."""
    run = _start(capsys)
    hold = _hold(rec.root, run)
    code, body = _call(capsys, "extend", run)
    assert code == vc.PAUSED and body["code"] == "RESEARCH-OWNER-GRANT-MISSING"
    assert body["decision"]["id"] == hold["id"]
    assert len(_refusals(rec.root, "RESEARCH-OWNER-GRANT-MISSING")) == 1
    assert _ev(rec.root, events.EXTEND) == [] and rec.fake.launches == [(run, False)]
    conn = _conn(rec.root)
    assert loops.is_halted(conn, run) and holds.open_holds(conn, run)
    conn.close()


def test_extend_refuses_an_owner_kind_decision_an_agent_typed_with_alpaca_decide(rec, capsys):
    run = _start(capsys)
    hold = _hold(rec.root, run)
    pointer = "research-grant:%s:extension:%s" % (run, hold["id"])
    code = cli.main(["--session", SESSION, "decide", "--kind", "go", "--context",
                     "%s halted at 2 rounds" % run, "--option", "extend %s to 4 rounds" % run,
                     "--choice", "extend %s to 4 rounds" % run, "--consequence",
                     "%s runs on to 4 rounds" % run, "--pointer", pointer, "--by", "owner"])
    assert code == vc.PASS
    conn = _conn(rec.root)
    dec = [e for e in db.events(conn, kind="decision", limit=10 ** 9)
           if e["data"].get("pointer") == pointer]
    assert len(dec) == 1 and dec[0]["actor"] == "owner" and dec[0]["data"]["kind"] == "go"
    conn.close()
    for extra in ((), ("--decision", dec[0]["ref"])):
        code, body = _call(capsys, "extend", run, *extra)
        assert code == vc.PAUSED and body["code"] == "RESEARCH-OWNER-GRANT-MISSING"
    assert _ev(rec.root, events.EXTEND) == []


def test_extend_refuses_a_grant_not_confirmed_at_a_terminal_or_not_decided_by_the_owner(
        rec, capsys, monkeypatch):
    """The fail case with research-grant events on the record that owner.authorized must not
    take: one confirmed "pty" whose decision the owner made, one confirmed "terminal" whose
    decision has actor "agent". Both answer the open hold with terms the task plan holds, so
    only the authorized checks keep the run halted."""
    from alpaca import decisions
    _frozen(monkeypatch)
    run = _start(capsys)
    hold = _hold(rec.root, run)
    pointer = "research-grant:%s:extension:%s" % (run, hold["id"])
    conn = _conn(rec.root)
    ids = []
    for confirmed, actor in (("pty", "owner"), ("terminal", "agent")):
        with db.transaction(conn):
            dec = decisions.record(conn, "go", "%s halted at 2 rounds (hold %s) %s"
                                   % (run, hold["id"], confirmed),
                                   ["extend %s to 4 rounds" % run, "stop %s" % run],
                                   "extend %s to 4 rounds" % run, "%s runs on to 4 rounds" % run,
                                   pointer, actor=actor, session=SESSION, root=rec.root)
            events.emit(conn, events.GRANT, run=run, ref=run, op="op-001", session=SESSION,
                        actor="owner", in_txn=True,
                        data={"ref": run, "gate": "extension", "hold": hold["id"],
                              "decision": dec["id"], "terms": {"rounds": 4}, "note": "",
                              "confirmed": confirmed, "by": "owner"})
        ids.append(dec["id"])
    conn.close()
    for extra in ((),) + tuple(("--decision", i) for i in ids):
        code, body = _call(capsys, "extend", run, *extra)
        assert code == vc.PAUSED and body["code"] == "RESEARCH-OWNER-GRANT-MISSING", body
    assert len(_refusals(rec.root, "RESEARCH-OWNER-GRANT-MISSING")) == 3
    assert _ev(rec.root, events.EXTEND) == [] and _ev(rec.root, events.HOLD_LIFTED) == []
    conn = _conn(rec.root)
    assert loops.is_halted(conn, run) and holds.open_holds(conn, run)
    conn.close()


def test_extend_and_the_extension_grant_read_the_bound_from_the_frozen_contract(rec, capsys,
                                                                                monkeypatch):
    """The bound is runner.rounds_bound over contract.read_frozen, the rule _Runner.bound
    applies: with a frozen contract of 3 rounds (the fake run start says 2), a grant of 3 is not
    above it, and extend records rounds_from 3."""
    read = _frozen(monkeypatch, rounds=3)
    run = _start(capsys)
    _hold(rec.root, run)
    code, body = _grant(capsys, monkeypatch, run, run, "extend", "--rounds", "3", "--no-apply")
    assert code == vc.FAIL and body["code"] == "RESEARCH-GRANT-TERMS" and "3 rounds" in str(body)
    code, body = _grant(capsys, monkeypatch, run, run, "extend", "--rounds", "4", "--no-apply")
    assert code == vc.PASS, body
    code, body = _call(capsys, "extend", run, "--no-resume")
    assert code == vc.PASS, body
    ext = _ev(rec.root, events.EXTEND, run)
    assert (ext[0]["data"]["rounds_from"], ext[0]["data"]["rounds_to"]) == (3, 4)
    assert read and set(read) == {run}


def test_extend_refuses_a_contract_that_no_longer_reads_as_frozen(rec, capsys, monkeypatch):
    """read_frozen's RUN-CONTRACT-CHANGED refuses extend (and the extension grant) before any
    write: the run stays halted, as the next launch would refuse it."""
    _frozen(monkeypatch)
    run = _start(capsys)
    _hold(rec.root, run)
    assert _grant(capsys, monkeypatch, run, run, "extend", "--rounds", "4",
                  "--no-apply")[0] == vc.PASS
    _frozen(monkeypatch, error=errors.refusal("RUN-CONTRACT-CHANGED", "contract.json moved"))
    code, body = _call(capsys, "extend", run)
    assert code == vc.FAIL and body["code"] == "RUN-CONTRACT-CHANGED", body
    assert len(_refusals(rec.root, "RUN-CONTRACT-CHANGED")) == 1
    assert _ev(rec.root, events.EXTEND) == [] and _ev(rec.root, events.HOLD_LIFTED) == []
    conn = _conn(rec.root)
    assert loops.is_halted(conn, run) and holds.open_holds(conn, run)
    conn.close()
    code, body = _grant(capsys, monkeypatch, run, run, "extend", "--rounds", "5", "--no-apply")
    assert code == vc.FAIL and body["code"] == "RUN-CONTRACT-CHANGED", body
    assert len(_ev(rec.root, events.GRANT)) == 1


def test_extend_with_the_owners_terminal_grant_lifts_the_halt_and_resumes(rec, capsys,
                                                                         monkeypatch):
    from alpaca import questions
    _frozen(monkeypatch)
    run = _start(capsys)
    hold = _hold(rec.root, run)
    # negative: an extension that does not raise N is refused before the terminal
    code, body = _grant(capsys, monkeypatch, run, run, "extend", "--rounds", "2")
    assert code == vc.FAIL and body["code"] == "RESEARCH-GRANT-TERMS"
    assert _ev(rec.root, events.GRANT) == [] and _ev(rec.root, "decision") == []
    code, body = _grant(capsys, monkeypatch, run, run, "extend", "--rounds", "4", "--no-apply")
    assert code == vc.PASS and body["grant"]["applied"] is False
    code, body = _call(capsys, "extend", run)
    assert code == vc.PASS, body
    ext = _ev(rec.root, events.EXTEND, run)
    assert len(ext) == 1 and (ext[0]["data"]["rounds_from"], ext[0]["data"]["rounds_to"]) == (2, 4)
    assert body["decision"] is None          # exit 0: no open hold (section 4.2)
    grant = _ev(rec.root, events.GRANT, run)[-1]["data"]
    assert ext[0]["data"]["decision"] and ext[0]["data"]["decision"] == grant["decision"]
    resumed = [e for e in _ev(rec.root, "loop-resume") if e["ref"] == run]
    lifted = _ev(rec.root, events.HOLD_LIFTED, run)
    assert len(resumed) == 1 and len(lifted) == 1 and lifted[0]["data"]["answer"] == "extension"
    res = _ev(rec.root, events.RESUME, run)
    assert len(res) == 1 and res[0]["data"]["cause"] == "extension"
    conn = _conn(rec.root)
    assert questions._state(conn)[hold["question"]]["status"] == "resolved"
    assert run not in loops.halted(conn) and not holds.open_holds(conn, run)
    conn.close()
    assert rec.fake.launches[-1] == (run, False)
    # the grant is used: a second extend finds no hold
    code, body = _call(capsys, "extend", run)
    assert code == vc.FAIL and body["code"] == "RESEARCH-RUN-NOT-HALTED"


def test_extend_refuses_a_grant_whose_rounds_pass_the_task_plan(rec, capsys, monkeypatch):
    from alpaca import taskplan
    _frozen(monkeypatch)
    run = _start(capsys)
    _hold(rec.root, run)
    monkeypatch.setattr(taskplan, "MAX_TASKS", 6)
    code, body = _grant(capsys, monkeypatch, run, run, "extend", "--rounds", "9")
    assert code == vc.FAIL and body["code"] == "RUN-PLAN-CAP" and body["grant"]["applied"] is False
    conn = _conn(rec.root)
    assert holds.open_holds(conn, run)
    conn.close()


# ------------------------------------------------------------------------------ resume
def test_resume_at_a_budget_hold_needs_the_owners_grant(rec, capsys, monkeypatch):
    run = _start(capsys)
    hold = _hold(rec.root, run, "budget")
    code, body = _call(capsys, "resume", run)
    assert code == vc.PAUSED and body["code"] == "RESEARCH-OWNER-GRANT-MISSING"
    assert [e for e in _ev(rec.root, events.LAUNCH) if e["id"] > hold["id"]] == []
    assert rec.fake.launches == [(run, False)]
    # negative: a grant that leaves the reached dollar cap where it is
    conn = _conn(rec.root)
    task = ops.add_task(conn, "op-001", "an attempt", "attempt",
                        extra={"research": {"run": run, "role": "attempt"}})
    from alpaca.research import spend
    spend.book(conn, run, task, cost_usd=10.0, tokens=5, session="t")
    conn.close()
    code, body = _grant(capsys, monkeypatch, run, run, "budget", "--tokens", "5000")
    assert code == vc.FAIL and body["code"] == "RESEARCH-GRANT-TERMS"
    code, body = _grant(capsys, monkeypatch, run, run, "budget", "--wall", "60")
    assert code == vc.FAIL and body["code"] == "RESEARCH-GRANT-TERMS"
    code, body = _grant(capsys, monkeypatch, run, run, "budget", "--usd", "25")
    assert code == vc.PASS and body["grant"]["applied"] is True
    res = _ev(rec.root, events.RESUME, run)
    assert len(res) == 1 and res[0]["data"]["cause"] == "budget"
    conn = _conn(rec.root)
    assert spend.caps(conn, run)["usd"] == 25
    conn.close()


def test_a_crash_resume_needs_no_grant(rec, capsys):
    run = _start(capsys)
    _finalize(rec.root, run, "interrupted")
    code, body = _call(capsys, "status", run)
    assert code == vc.PASS and body["run"]["state"] == "interrupted"
    code, body = _call(capsys, "resume", run)
    assert code == vc.PASS and body["decision"] is None
    assert _ev(rec.root, events.RESUME, run)[0]["data"]["cause"] == "crash"
    # negative: an unknown run
    code, body = _call(capsys, "resume", "rr-404")
    assert code == vc.FAIL and body["code"] == "RESEARCH-RUN-UNKNOWN"
    assert _refusals(rec.root, "RESEARCH-RUN-UNKNOWN")[-1]["ref"] == "rr-404"


def test_a_resumable_run_restarts_under_its_lift_without_a_second_grant(rec, capsys,
                                                                       monkeypatch):
    _frozen(monkeypatch)
    run = _start(capsys)
    _hold(rec.root, run)
    _grant(capsys, monkeypatch, run, run, "extend", "--rounds", "3", "--no-apply")
    code, body = _call(capsys, "extend", run, "--no-resume")
    assert code == vc.PASS and _ev(rec.root, events.RESUME) == [] and body["decision"] is None
    decision = _ev(rec.root, events.EXTEND, run)[-1]["data"]["decision"]
    assert decision
    code, body = _call(capsys, "resume", run)
    assert code == vc.PASS and body["decision"] is None          # exit 0 (section 4.2)
    res = _ev(rec.root, events.RESUME, run)[0]["data"]
    assert res["cause"] == "extension" and res["decision"] == decision
    # negative: the extension hold still open sends the caller to extend
    run2 = None
    _finalize(rec.root, run)
    run2 = _start(capsys)
    _hold(rec.root, run2)
    code, body = _call(capsys, "resume", run2)
    assert code == vc.PAUSED and body["code"] == "RESEARCH-HOLD-OPEN"


# ------------------------------------------------------------------------------ grant
def test_grant_refuses_an_agent_session_and_a_non_terminal_and_records_each(rec, capsys,
                                                                           monkeypatch):
    _frozen(monkeypatch)
    run = _start(capsys)
    _hold(rec.root, run)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr(sys, "stdin", _Tty())
    code, body = _call(capsys, "grant", run, "extend", "--rounds", "4")
    assert code == vc.FAIL and body["code"] == "RESEARCH-OWNER-NOT-TERMINAL"
    monkeypatch.delenv("CLAUDECODE")

    class _Pipe:
        def isatty(self):
            return False
    monkeypatch.setattr(sys, "stdin", _Pipe())
    code, body = _call(capsys, "grant", run, "extend", "--rounds", "4")
    assert code == vc.FAIL and body["code"] == "RESEARCH-OWNER-NOT-TERMINAL"
    assert len(_refusals(rec.root, "RESEARCH-OWNER-NOT-TERMINAL")) == 2
    assert _ev(rec.root, events.GRANT) == [] and _ev(rec.root, "decision") == []


def test_grant_at_the_terminal_records_an_owner_decision_and_a_confirmed_grant(rec, capsys,
                                                                              monkeypatch):
    _frozen(monkeypatch)
    run = _start(capsys)
    hold = _hold(rec.root, run)
    code, body = _grant(capsys, monkeypatch, run, run, "extend", "--rounds", "5", "--no-apply",
                        "--note", "one more try")
    assert code == vc.PASS
    grant = _ev(rec.root, events.GRANT, run)[0]
    assert grant["actor"] == "owner"
    d = grant["data"]
    assert (d["gate"], d["hold"], d["confirmed"], d["by"], d["terms"], d["note"]) == (
        "extension", hold["id"], "terminal", "owner", {"rounds": 5}, "one more try")
    conn = _conn(rec.root)
    dec = [e for e in db.events(conn, kind="decision", limit=10 ** 9)
           if e["ref"] == d["decision"]][0]
    conn.close()
    page = dec["data"]
    assert dec["actor"] == "owner" and page["kind"] == "go"
    assert page["choice"] == "extend %s to 5 rounds" % run and page["choice"] in page["options"]
    assert page["context"].endswith("(hold %s)" % hold["id"]) and page["consequence"]
    assert page["pointer"] == "research-grant:%s:extension:%s" % (run, hold["id"])
    assert "stays held" in page["consequence"]
    ok, got = owner.authorized(_conn(rec.root), run, "extension")
    assert ok and got == d["decision"]
    # negative: a stop grant with no hold open
    _call(capsys, "extend", run, "--no-resume")
    code, body = _grant(capsys, monkeypatch, run, run, "stop")
    assert code == vc.FAIL and body["code"] == "RESEARCH-HOLD-NONE"


def test_two_loosen_grants_with_the_same_terms_ahead_of_any_hold_get_two_ids(rec, capsys,
                                                                           monkeypatch):
    ids = []
    for _ in range(2):
        code, body = _grant(capsys, monkeypatch, RB, "--runbook", RB, "loosen", "--set",
                            "research.stop.slots=3")
        assert code == vc.PASS
        ids.append(body["grant"]["decision"])
    assert len(set(ids)) == 2
    conn = _conn(rec.root)
    page = [e for e in db.events(conn, kind="decision", limit=10 ** 9)][-1]["data"]
    conn.close()
    assert page["kind"] == "done_when" and "no hold open, record through event" in page["context"]


def test_the_agent_markers_match_the_release_module():
    from alpaca.release import cli as release_cli
    assert owner.AGENT_MARKERS == release_cli.AGENT_MARKERS
    # negative: confirm_at_terminal refuses each marker on its own
    for name in owner.AGENT_MARKERS:
        with pytest.raises(owner.OwnerRefusal) as exc:
            owner.confirm_at_terminal("rr-001", prompt="", environ={name: "1"},
                                      isatty=lambda: True, read=lambda p: "rr-001")
        assert exc.value.reason == "RESEARCH-OWNER-NOT-TERMINAL"
    assert owner.confirm_at_terminal("rr-001", prompt="", environ={}, isatty=lambda: True,
                                     read=lambda p: " rr-001 \n") is None


SIX = [("research.stop.slots=3", "research.stop.slots", 3),
       ("research.metrics[compares].target=17000", "research.metrics[compares].target", 17000),
       ('stages[unit-pass]={id: unit-pass, run: "pytest -q", timeout: 10m}', "stages[unit-pass]",
        {"id": "unit-pass", "run": "pytest -q", "timeout": "10m"}),
       ("knobs[k1]=null", "knobs[k1]", None),
       ("spec=specs/new.md", "spec", "specs/new.md"),
       ("files[tests/a=b.py]=%s" % ("ab" * 32), "files[tests/a=b.py]", "ab" * 32)]


def test_loosen_terms_take_the_five_path_forms(rec, capsys, monkeypatch):
    argv = ["--runbook", RB, "loosen"]
    for item, _path, _value in SIX:
        argv += ["--set", item]
    code, body = _grant(capsys, monkeypatch, RB, *argv)
    assert code == vc.PASS, body
    terms = _ev(rec.root, events.GRANT)[-1]["data"]["terms"]
    assert terms == {"set": {path: value for _item, path, value in SIX}}
    # a stage's canonical_json text pasted as VALUE records the same value
    stage = SIX[2][2]
    code, body = _grant(capsys, monkeypatch, RB, "--runbook", RB, "loosen", "--set",
                        "stages[unit-pass]=%s" % util.canonical_json(stage))
    assert code == vc.PASS
    assert _ev(rec.root, events.GRANT)[-1]["data"]["terms"] == {"set": {"stages[unit-pass]": stage}}


@pytest.mark.parametrize("argv", [
    ("--set", "bogus.path=1"),
    ("--set", "research.stop.slots=3", "--set", "research.stop.slots=4"),
    ("--task", "t-001", "--set", "stages[x]={id: x}"),
    ("--set", "files[a.py]=nothex"),
    ("--set", "research.stop.slots=[unclosed"),
    ("--set", "spec=[a, b]"),
    ("--set", "knobs[k]=3"),
], ids=["no-form", "twice", "pack-form-with-task", "files-not-hex", "unreadable-yaml",
        "spec-not-text", "knob-not-mapping"])
def test_loosen_terms_refused(rec, capsys, monkeypatch, argv):
    head = ["--runbook", RB]
    rest = list(argv)
    if rest[0] == "--task":
        head += rest[:2]
        rest = rest[2:]
    code, body = _grant(capsys, monkeypatch, RB, *(head + ["loosen"] + rest))
    assert code == vc.FAIL and body["code"] == "RESEARCH-GRANT-TERMS"
    assert _ev(rec.root, events.GRANT) == []
    assert _refusals(rec.root, "RESEARCH-GRANT-TERMS")


def test_grant_forms_that_do_not_parse_are_usage_errors(rec, capsys):
    for argv in (["grant", "rr-001"], ["grant", "rr-001", "loosen"],
                 ["grant", "--runbook", RB, "extend"], ["grant", "rr-001", "stop", "--rounds", "3"],
                 ["grant", "--runbook", RB, "--task", "t-001", "reopen"]):
        assert cli.main(["--session", SESSION, "research"] + argv) == vc.USAGE, argv


# ------------------------------------------------------------------------------ report
def _improve_run(root, capsys, *, champion="c" * 40, control=None):
    """A finished improve run on the record: round 0 at the base commit, a merge of round 1,
    the finalize."""
    run = _start(capsys, *(("--control", control) if control else ()))
    conn = _conn(root)
    events.emit(conn, events.ROUND, run=run, ref=run, op="op-001", session="research-%s" % run,
                code=vc.PASS, data={"round": 0, "repair_task": None, "winner": None,
                                    "champion_commit": "b" * 40,
                                    "champion_metrics": {"value": {"median": 100}},
                                    "accepted": 0, "targets_met": False})
    tid = ops.add_task(conn, "op-001", "an attempt", "attempt",
                       extra={"research": {"run": run, "role": "attempt",
                                           "key": "%s:r1:s0" % run, "round": 1, "slot": 0}})
    events.emit(conn, events.ATTEMPT, run=run, ref=tid, op="op-001", session="t",
                data={"outcome": "improvement", "metrics": {"value": {"median": 8}}})
    if champion != "b" * 40:
        events.emit(conn, events.MERGE, run=run, ref=run, op="op-001", session="t",
                    data={"round": 1, "repair_task": None, "attempt": tid, "commit": champion,
                          "parent": "b" * 40, "research_branch": "research/%s" % run})
    events.emit(conn, events.ROUND, run=run, ref=run, op="op-001", session="t", code=vc.PASS,
                data={"round": 1, "repair_task": None, "winner": tid,
                      "champion_commit": champion, "champion_metrics": {"value": {"median": 8}},
                      "accepted": 1, "targets_met": True})
    conn.close()
    _finalize(root, run)
    return run


def test_report_writes_where_the_hub_library_opens_it(rec, capsys):
    from alpaca import hub
    run = _improve_run(rec.root, capsys)
    code, body = _call(capsys, "report", run)
    assert code == vc.PAUSED                             # the ship card is open
    rel = ".alpaca/reports/research/%s/report.md" % run
    assert body["report"]["path"] == rel and body["report"]["new"] is True
    doc = hub.document(rec.root, rel)
    assert doc["text"].startswith("# Research run %s" % run)
    text = open(os.path.join(rec.root, rel), "rb").read()
    assert text.isascii()
    js = json.load(open(os.path.join(rec.root, body["report"]["json_path"])))
    assert js["metrics"][0]["champion"] == 8 and js["metrics"][0]["met"] is True
    assert js["end"]["state"] == "finished"
    # negative: an unknown run is refused and recorded
    code, body = _call(capsys, "report", "rr-404")
    assert code == vc.FAIL and body["code"] == "RESEARCH-RUN-UNKNOWN"


def test_the_same_run_events_give_the_same_bytes_and_one_event(rec, capsys, monkeypatch):
    from alpaca import clock
    run = _improve_run(rec.root, capsys, champion="b" * 40)      # nothing to ship
    code, first = _call(capsys, "report", run)
    assert code == vc.PASS, first
    util.set_clock(clock.FixedClock(start="2031-01-01T00:00:00+00:00"))
    try:
        code, second = _call(capsys, "report", run)
    finally:
        util.set_clock(None)
    assert first["report"]["sha256"] == second["report"]["sha256"]
    assert second["report"]["new"] is False
    assert len(_ev(rec.root, events.REPORT, run)) == 1
    # negative: a new run event changes the bytes and records a second report
    conn = _conn(rec.root)
    events.emit(conn, events.TAMPER, run=run, ref=run, op="op-001", session="t",
                data={"point": "close", "verdict": "PASS", "changed": []})
    conn.close()
    code, third = _call(capsys, "report", run)
    assert third["report"]["new"] is True and third["report"]["sha256"] != first["report"]["sha256"]
    assert len(_ev(rec.root, events.REPORT, run)) == 2


def test_a_report_rebuilt_from_the_run_events_alone_has_the_same_bytes(rec, capsys):
    from alpaca import clock
    from alpaca.research import proofs
    from alpaca.resilience import budget
    _write(rec.root, "project.yaml", "name: t\nbudget:\n  totals:\n    small: 100\n")
    run = _improve_run(rec.root, capsys)
    control = _improve_run(rec.root, capsys, control="noop")    # same contract hash
    conn = _conn(rec.root)
    plain_op("other", "bar")
    budget.spend(conn, 3.0, ref="t-other")
    conn.close()
    assert control != run
    assert _call(capsys, "report", run)[0] == vc.PAUSED
    conn = _conn(rec.root)
    exported = proofs.export_report_events(conn, rec.root, run)
    conn.close()
    util.set_clock(clock.FixedClock(start="2031-01-01T00:00:00+00:00"))
    try:
        out = os.path.join(rec.root, "replay")
        got = proofs.replay_report(exported["path"], run, out)
    finally:
        util.set_clock(None)
    assert got["match"] is True
    # negative: a replay of another run's export does not match this run's report
    rows = json.load(open(exported["path"]))
    rows["report_md_sha256"] = "0" * 64
    bad = _write(rec.root, "bad-events.json", json.dumps(rows))
    assert proofs.replay_report(bad, run, os.path.join(rec.root, "replay2"))["match"] is False


def test_the_ship_card_is_raised_once_and_only_for_a_finished_real_run_to_ship(rec, capsys):
    run = _improve_run(rec.root, capsys)
    assert _call(capsys, "report", run)[0] == vc.PAUSED
    hold = [h for h in _ev(rec.root, events.HOLD, run) if h["data"]["gate"] == "ship"]
    assert len(hold) == 1 and hold[0]["data"]["code"] == "RUN-SHIP-DECISION"
    conn = _conn(rec.root)
    card = review._state(conn)[hold[0]["data"]["card"]]
    assert card["kind"] == "ship" and card["subject"] == "research:%s:champion" % run
    with pytest.raises(ValueError):
        holds.open_hold(conn, rec.root, ref=run, gate="ship", code="RUN-BUDGET-HOLD",
                        detail="x", op="op-001", bound=None, session="t")
    conn.close()
    assert _call(capsys, "report", run)[0] == vc.PAUSED            # still open: exit 3
    conn = _conn(rec.root)
    review.move(conn, card["id"], "adopt it by hand", "owner")
    conn.close()
    code, body = _call(capsys, "report", run)
    assert code == vc.PASS and body["decision"] is None
    assert len([h for h in _ev(rec.root, events.HOLD, run) if h["data"]["gate"] == "ship"]) == 1
    conn = _conn(rec.root)
    assert len([c for c in review._state(conn).values() if c["kind"] == "ship"]) == 1
    conn.close()
    # negative: an unchanged champion and a control run raise no card
    _finalize(rec.root, run)
    same = _improve_run(rec.root, capsys, champion="b" * 40)
    ctl = _improve_run(rec.root, capsys, control="noop")
    for r in (same, ctl):
        _call(capsys, "report", r)
        assert [h for h in _ev(rec.root, events.HOLD, r) if h["data"]["gate"] == "ship"] == []


def _repair_run(rec, capsys, *, resolved):
    root = rec.root
    rec.fake.mode = "repair"
    run = _start(capsys)
    rec.fake.mode = "improve"
    conn = _conn(root)
    tid = ops.add_task(conn, "op-001", "repair case 1", "repair 1",
                       extra={"research": {"run": run, "role": "repair", "index": 1,
                                           "case": "stable-keys", "repair_task": None,
                                           "key": "%s:repair:1" % run}})
    events.emit(conn, events.REPAIR_RESULT, run=run, ref=tid, op="op-001", session="t",
                data={"outcome": "fixed" if resolved else "not-fixed", "head": "f" * 40,
                      "fix_attempt": None})
    if resolved:
        db.append_event(conn, session="t", actor="t", kind="task-move", op="op-001", ref=tid,
                        data={"from": "doing", "to": "done", "proof": "local:x.md"})
    conn.close()
    _finalize(root, run)
    return run, tid


def test_a_repair_run_ships_only_with_a_resolved_task_whose_branch_the_card_names(
        rec, capsys, monkeypatch):
    run, tid = _repair_run(rec, capsys, resolved=True)
    assert _call(capsys, "report", run)[0] == vc.PAUSED
    hold = [h for h in _ev(rec.root, events.HOLD, run) if h["data"]["gate"] == "ship"]
    assert len(hold) == 1
    detail = hold[0]["data"]["detail"]
    assert "research/%s/%s" % (run, tid) in detail and "f" * 40 in detail
    conn = _conn(rec.root)
    assert review._state(conn)[hold[0]["data"]["card"]]["subject"] == "research:%s:repair" % run
    conn.close()
    js = json.load(open(os.path.join(rec.root, ".alpaca/reports/research/%s/report.json" % run)))
    assert js["resolved_rate"] == {"resolved": 1, "of": 1}
    # negative: no resolved task, no card (after the owner answered the first card, which
    # would otherwise hold every start under the runbook)
    conn = _conn(rec.root)
    review.move(conn, hold[0]["data"]["card"], "adopt the fix", "owner")
    conn.close()
    run2, _ = _repair_run(rec, capsys, resolved=False)
    assert _call(capsys, "report", run2)[0] == vc.PASS
    assert [h for h in _ev(rec.root, events.HOLD, run2) if h["data"]["gate"] == "ship"] == []


def test_report_render_is_ascii_and_build_reads_no_clock(rec, capsys):
    from alpaca import clock
    run = _improve_run(rec.root, capsys)
    conn = _conn(rec.root)
    holds.open_hold(conn, rec.root, ref=run, gate="budget", code="RUN-BUDGET-HOLD",
                    detail="caf" + chr(0xe9) + " " + chr(0x2014) + " spent", op="op-001",
                    bound=None, session="t")
    built = report.build(conn, rec.root, run)
    util.set_clock(clock.FixedClock(start="2031-01-01T00:00:00+00:00"))
    try:
        again = report.build(conn, rec.root, run)
    finally:
        util.set_clock(None)
    conn.close()
    assert again == built
    text = report.render(built)
    assert text.isascii() and "Research run %s" % run in text
    assert "caf\\xe9 \\u2014 spent" in text            # escaped, never dropped
    assert built["through_event"] >= built["evidence"]["run_start"]


# ------------------------------------------------------------------------------ contracts, template
def test_contracts_lists_only_tracked_format_3_runbooks_with_their_burn_in(rec, capsys):
    from alpaca.tests import research_loopkit as kit
    root = rec.root
    kit.make_project(root)
    _write(root, "pack/old.yaml", "runbook: 2\nid: old\n")
    _write(root, "templates/research/improve.yaml", "runbook: 3\nresearch: {mode: improve}\n")
    kit.git(root, "add", "-A")
    kit.git(root, "commit", "-q", "-m", "more")
    code, body = _call(capsys, "contracts")
    assert code == vc.PASS, body
    paths_ = [c["path"] for c in body["contracts"]]
    assert paths_ == [RB]
    c = body["contracts"][0]
    assert c["id"] == "loop-improve" and c["mode"] == "improve" and c["check"] == "PASS"
    assert c["metrics"] == [{"id": "value", "unit": "count", "direction": "lower"}]
    assert c["targets"] == {"value": 10} and (c["k"], c["n"]) == (2, 2)
    assert len(c["contract_sha256"]) == 64 and len(c["pack_sha256"]) == 64
    assert c["burn_in"]["state"] == "needed" and c["runs"] == [] and c["holds"] == []
    assert c["reopen_needed"] is False and c["signoff"] is None
    # contracts writes no event
    before = len(_ev(root, events.STATUS)) + len(_ev(root, events.REFUSED))
    assert catalog.find(root)[0]["path"] == RB
    assert len(_ev(root, events.STATUS)) + len(_ev(root, events.REFUSED)) == before


def test_a_failed_burn_in_names_the_control_run_and_the_accepted_attempt(rec, capsys):
    """catalog's record reading of the burn-in (temporary until controls.burn_in is built):
    `failed` names the control run and its first accepted attempt (section 4.3.8)."""
    run = _start(capsys, "--control", "noop")
    conn = _conn(rec.root)
    try:
        assert catalog._burn_in(conn, "c" * 64, 2)["state"] == "needed"
        base = ops.add_task(conn, "op-001", "the baseline", "baseline",
                            extra={"research": {"run": run, "role": "attempt", "baseline": True,
                                                "key": "%s:r0" % run, "round": 0, "slot": 0}})
        events.emit(conn, events.ATTEMPT, run=run, ref=base, op="op-001", session="t",
                    data={"outcome": "improvement"})
        assert catalog._burn_in(conn, "c" * 64, 2)["state"] == "needed"
        tid = ops.add_task(conn, "op-001", "an attempt", "attempt",
                           extra={"research": {"run": run, "role": "attempt",
                                               "key": "%s:r1:s0" % run, "round": 1, "slot": 0}})
        events.emit(conn, events.ATTEMPT, run=run, ref=tid, op="op-001", session="t",
                    data={"outcome": "improvement"})
        got = catalog._burn_in(conn, "c" * 64, 2)
    finally:
        conn.close()
    assert got["state"] == "failed" and got["run"] == run and got["attempt"] == tid
    assert tid in got["reason"] and run in got["reason"]


def test_template_prints_copies_and_refuses_to_overwrite(rec, capsys):
    src = os.path.join(REPO, "templates", "research", "improve.yaml")
    _write(rec.root, "templates/research/improve.yaml", open(src).read())
    code, text = _call(capsys, "template", "--mode", "improve", json_out=False)
    assert code == vc.PASS and "runbook: 3" in text
    code, body = _call(capsys, "template", "--mode", "improve", "--out", "mine/rb.yaml")
    assert code == vc.PASS and body["template"] == {"mode": "improve", "path": "mine/rb.yaml"}
    assert open(os.path.join(rec.root, "mine/rb.yaml")).read() == open(src).read()
    code, body = _call(capsys, "template", "--mode", "improve", "--out", "mine/rb.yaml")
    assert code == vc.FAIL and body["code"] == "RESEARCH-TEMPLATE-EXISTS"
    # negative: a path outside the root is a usage error
    assert cli.main(["research", "template", "--mode", "improve", "--out", "/etc/x.yaml"]) \
        == vc.USAGE
