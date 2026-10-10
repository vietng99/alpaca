"""Repair mode (op-009 t-101): alpaca/research/repair.py, the repair steps of lifecycle.py (the
fix prompts' bug report and hints, the reproducer order check of `judge`), the reproducer
prompts, runner.rounds_bound and the `/contracts/research/` line of .gitignore
(design/research-engine/architecture.md sections 3.9, 4.3, 7.4 and 8.3; runbook-format-3.md
sections 11 and 14.5).

Done when (t-101's contract):
  * a repair is accepted end to end: a real repair run, its prepare stage committed as the case
    base, the store reproducer shown failing on the base live, the base pass-to-pass set
    measured, a fix proposed, implemented, gated, accepted by the repair rule, verified, merged
    into research/<run>/<task> and the repair task closed done
    (test_a_repair_is_accepted_end_to_end);
  * a fix without a reproducing test is refused: a reproducer that does not fail on the base
    blocks the task before any fix round
    (test_a_reproducer_that_passes_on_the_base_blocks_the_task_before_any_fix),
    and `judge` refuses a fix attempt with no proved reproducer
    (test_judge_refuses_a_fix_with_no_proved_reproducer);
  * a fix that breaks an old test is refused (test_a_fix_that_breaks_an_old_test_is_refused);
  * without a supplied reproducing test one agent writes it and a second reviews it, it counts
    only after base FAIL is shown live, and any later change needs the same proof
    (test_a_generated_reproducer_is_written_reviewed_and_proved_live,
    test_a_generated_reproducer_counts_only_with_review_and_base_fail,
    test_a_changed_reproducer_needs_the_same_proof);
  * the reproducer store is ignored by git and the runner refuses to start while a path under
    it is tracked (test_the_reproducer_store_is_ignored_by_git,
    test_the_runner_refuses_while_the_store_is_tracked).
Fail case: the reproducing test is written after the fix and tuned to it
(test_a_reproducer_proved_after_the_fix_started_never_accepts_it).
Also: early ends (test_a_failed_prepare_or_a_thin_base_set_blocks_the_task), the hints
(test_hints_name_the_suspects_the_chain_and_the_coupled_groups) and the history truncated at the
case base (checked in the end-to-end test).

The project is a git repository with a small shell calculator whose `max` returns its first
argument; the scripted agents stand in for the launcher (research_loopkit.Agents) and the
verifiers are the scripted verifiers of test_research_verifier.py, which re-run every claim
through the real verifier.serve. No test touches the network or starts an agent runtime. Tests
that run gates need bubblewrap and skip without it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from alpaca import db, ops
from alpaca.gates import verdict as vc
from alpaca.research import (contract as contract_mod, errors, events, jail, lifecycle, paths,
                             repair, runner, state)
from alpaca.research.types import RunContext
from alpaca.tests import research_loopkit as kit
from alpaca.tests.conftest import REPO
from alpaca.tests.plain_op import plain_op

needs_jail = pytest.mark.skipif(not jail.available()[0],
                                reason="bubblewrap is not available on this host")

RUNBOOK = "pack/rb.yaml"
RB_ID = "calc-repair"
CASE = "max-larger"

BUGGY = """case "$1" in
  add) echo $(( $2 + $3 )) ;;
  mul) echo $(( $2 * $3 )) ;;
  max) if [ "$2" -gt "$3" ]; then echo "$2"; else echo "$2"; fi ;;
esac
"""
FIXED = BUGGY.replace('else echo "$2"; fi', 'else echo "$3"; fi')
BREAKS_ADD = FIXED.replace("$(( $2 + $3 ))", "$(( $2 - $3 ))")

SPEC = """# Feature Specification: calc bug fixes

## User Scenarios & Testing

### Edge Cases

- **EC-001**: max of a smaller and a larger number gives the larger one.
- **EC-002**: A fix that edits the tests instead of the code is refused.

## Requirements

- **FR-001**: calc.sh answers add, mul and max.

## Success Criteria

- **SC-001**: max returns the larger of its two arguments.
- **SC-002**: Every test that passes on the base still passes after each fix.
- **SC-003**: The calc script is present.
"""

#: the old suite: five tests; max_second fails on the base (the bug), fixture_present passes
#: only once the prepare stage wrote data/fixture.txt
TESTS = """read -r T <&3; exec 3<&-
C="$WORKTREE/src/calc.sh"
t() {
  n=$1; w=$2; shift 2; g=$(sh "$C" "$@" 2>/dev/null)
  if [ "$g" = "$w" ]; then r=PASS; else r=FAIL; fi
  echo "TEST: $n ... $r $T"
}
echo ALPACA-RESULTS-BEGIN
t add_small 5 add 2 3
t add_zero 0 add 0 0
t mul_small 6 mul 2 3
t max_first 5 max 5 3
t max_second 2 max 1 2
if [ -f "$WORKTREE/data/fixture.txt" ]; then r=PASS; else r=FAIL; fi
echo "TEST: fixture_present ... $r $T"
echo "ALPACA-RESULTS-END $T"
"""

PREPARE = """mkdir -p "$WORKTREE/data"
echo prepared > "$WORKTREE/data/fixture.txt"
"""

#: a reproducer: max 1 2 must give 2
REPRODUCER = """read -r T <&3; exec 3<&-
echo ALPACA-RESULTS-BEGIN
g=$(sh "$WORKTREE/src/calc.sh" max 1 2)
if [ "$g" = 2 ]; then r=PASS; else r=FAIL; fi
echo "TEST: max_returns_larger ... $r $T"
echo "ALPACA-RESULTS-END $T"
"""
#: a "reproducer" that does not reproduce the bug: it passes on the base
NOT_REPRODUCING = REPRODUCER.replace("max 1 2)", "max 5 3)").replace('= 2 ]', '= 5 ]')

BUG = "calc.sh max 1 2 prints 1; it should print the larger number, 2.\n"

RB = """runbook: 3
id: calc-repair
title: Fix the reported calc bugs
description: A fix counts only when the reproducer fails on the base and passes with the fix.
spec: spec.md
owner: the test

stages:
  - id: build
    title: the calc script is present
    run: test -f ${WORKTREE}/src/calc.sh
    timeout: 60
    checks:
      - {id: build-exit, type: exit-code, covers: [SC-003], source: spec:SC-003}
PREPARE_STAGE
research:
  mode: repair
  accept:
    rule: repair
  paths:
    editable: [src/]
    protected: [pack/]
    covers: [EC-002]
    source: spec:EC-002
  gates:
    - {stage: build}
  stop:
    rounds: ROUNDS
    slots: 1
  budget:
    usd: 10
    tokens: 1000000
  disk: 0.001
  runtime:
    retries: 1
  controls:
    noop_rounds: 2
    random: false
  fingerprint:
    tools: [sh]
  repair:
PREPARE_KEY    pass_to_pass:
      command: sh ${CONTRACT_DIR}/pack/tests.sh
      min_tests: MIN_TESTS
      timeout: 60
      covers: [SC-002]
      source: spec:SC-002
    cases:
      - id: max-larger
        bug_report: bugs/max.md
        base: BASE
        fail_to_pass:
          origin: ORIGIN
TESTS_LINE          timeout: 60
        suspect_files: [src/calc.sh]
        covers: [SC-001, EC-001, FR-001]
        source: spec:SC-001
  coupled:
    - id: calc-docs
      files: [src/calc.sh, src/calc.txt]
      note: calc.txt documents every operation of calc.sh
    - id: unrelated
      files: [src/other.sh, src/other.txt]
"""

PREPARE_STAGE = """  - id: prepare
    title: write the test fixture into the case's base tree
    run: sh ${CONTRACT_DIR}/pack/prepare.sh
    timeout: 60
    checks:
      - {id: prepare-exit, type: exit-code, covers: [SC-003], source: spec:SC-003}
"""


def make_project(root, *, origin="store", rounds=2, prepare=True, min_tests=3,
                 prepare_script=PREPARE, store=REPRODUCER):
    """The calc project committed on main in two commits: the base (the bug) and a later commit
    that holds the runbook and the upstream fix, so the case base (the first commit) has a
    commit after it that no repair tree may hold. Returns (root, base commit, later commit)."""
    kit.write(root, "project.yaml", "name: repair-test\n")
    kit.write(root, ".gitignore", ".alpaca/\ncontracts/research/\n")
    kit.write(root, "src/calc.sh", BUGGY)
    kit.write(root, "src/calc.txt", "add, mul and max\n")
    kit.write(root, "pack/spec.md", SPEC)
    kit.write(root, "pack/tests.sh", TESTS)
    kit.write(root, "pack/prepare.sh", prepare_script)
    kit.write(root, "pack/bugs/max.md", BUG)
    kit.git(root, "init", "-q", "-b", "main")
    kit.git(root, "add", "-A")
    kit.git(root, "commit", "-q", "-m", "base")
    base = kit.git(root, "rev-parse", "HEAD").strip()
    rb = (RB.replace("ROUNDS", str(rounds)).replace("ORIGIN", origin).replace("BASE", base)
          .replace("MIN_TESTS", str(min_tests))
          .replace("PREPARE_STAGE\n", PREPARE_STAGE if prepare else "")
          .replace("PREPARE_KEY", "    prepare: prepare\n" if prepare else "")
          .replace("TESTS_LINE", "          tests: [max_returns_larger]\n"
                   if origin == "store" else ""))
    kit.write(root, RUNBOOK, rb)
    kit.write(root, "src/calc.sh", FIXED.replace("esac", "# upstream fix FIXED-UPSTREAM\nesac"))
    kit.git(root, "add", "-A")
    kit.git(root, "commit", "-q", "-m", "runbook and the upstream fix")
    later = kit.git(root, "rev-parse", "HEAD").strip()
    if origin == "store":
        kit.write(root, "contracts/research/%s/%s/run.sh" % (RB_ID, CASE), store, 0o755)
    return root, base, later


def proposal(spec, *, evidence="max prints its first argument in both branches of src/calc.sh"):
    return json.dumps({"attempt": spec["attempt"], "title": "max returns the larger argument",
                       "hypothesis": "The else branch of max echoes $2 instead of $3.",
                       "files": ["src/calc.sh"], "suspects": ["src/calc.sh"],
                       "evidence": evidence})


class Agents(kit.Agents):
    """The loop kit's scripted agents plus the two reproducer roles: the writer writes
    `writer_files` into its draft folder and names `writer_tests`; the reviewer answers
    `review`."""

    def __init__(self, root, *, fix=FIXED, writer_files=None, writer_tests=("max_returns_larger",),
                 review=None):
        self.fix = list(fix) if isinstance(fix, (list, tuple)) else [fix]
        self.reviewed = []
        self.writer_files = {"run.sh": REPRODUCER} if writer_files is None else writer_files
        self.writer_tests = list(writer_tests)
        self.review = review or {"verdict": "approve", "conditions": [], "reason": "it checks max"}
        self.specs = {}
        super().__init__(root, self.act)

    def act(self, role, research, spec):
        self.specs.setdefault(role, dict(spec))
        if role == "proposer":
            return {"final": proposal(spec)}
        if role == "implementer":
            fix = self.fix.pop(0) if len(self.fix) > 1 else self.fix[0]
            return {"files": {"src/calc.sh": fix}}
        if role == repair.WRITER:
            for rel, text in self.writer_files.items():
                path = os.path.join(spec["cwd"], repair.DRAFT_FOLDER, rel)
                if text is None:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    os.symlink("/etc/hostname", path)
                else:
                    kit.write(spec["cwd"], os.path.join(repair.DRAFT_FOLDER, rel), text, 0o755)
            return {"final": json.dumps({"tests": self.writer_tests, "summary": "max 1 2"})}
        if role == repair.REVIEWER:
            with open(os.path.join(spec["cwd"], "draft", "run.sh"), encoding="utf-8") as fh:
                self.reviewed.append(fh.read())
            return {"final": json.dumps(self.review)}
        return {}


def _start(root, monkeypatch, agents):
    kit.install(monkeypatch, root, agents)
    return kit.start(root, runbook=RUNBOOK)


def _go(root, run):
    return runner.launch(root, run, foreground=True, session="test")


def _ctx(root, got):
    conn = db.connect(root)
    try:
        frozen = contract_mod.load_frozen(conn, root, got["run"])
    finally:
        conn.close()
    return RunContext(root=root, run=got["run"], op=got["op"],
                      session=events.runner_session(got["run"]), contract=frozen,
                      runtime="control", tmp_prefixes=())


def _steps(root, ctx, task, names):
    """Run the named steps of the repair task's plan in order; their codes."""
    plan = {s["name"]: s["run"] for s in repair.repair_plan(ctx, task)["steps"]}
    conn = db.connect(root)
    try:
        return [plan[n](conn) for n in names]
    finally:
        conn.close()


def _status(root, task):
    conn = db.connect(root)
    try:
        return conn.execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()[0]
    finally:
        conn.close()


def _last(root, kind, ref):
    got = kit.events_of(root, kind, ref)
    return got[-1] if got else None


def _ids(root, kind, ref):
    return [e["id"] for e in kit.events_of(root, kind, ref)]


# ================================================================== end to end
@needs_jail
def test_a_repair_is_accepted_end_to_end(project, monkeypatch):
    root, base, later = make_project(project)
    agents = Agents(root)
    got = _start(root, monkeypatch, agents)
    run, (task,) = got["run"], got["repair_tasks"]
    out = _go(root, run)
    assert out["code"] in (vc.PASS, vc.PAUSED), out
    # the order FR-005 asks: prepare, reproducer proved, base set measured, then the fix rounds
    prep = _last(root, events.PREPARE, task)
    rep = _last(root, events.REPRODUCER, task)
    basel = _last(root, events.BASELINE, task)
    opened = [e for e in kit.events_of(root, events.ROUND_OPEN, run)
              if e["data"].get("repair_task") == task]
    assert prep["data"]["ok"] and rep["data"]["proved"] and basel["data"]["ok"]
    assert prep["id"] < rep["id"] < basel["id"] < opened[0]["id"]
    # base FAIL shown live: the base-side fail-to-pass gate of the repair task ran in a jail
    live = [e for e in kit.events_of(root, events.GATE, task)
            if e["id"] == rep["data"]["live_event"]]
    assert live and live[0]["data"]["side"] == "base" and live[0]["data"]["ran"] is True
    assert live[0]["data"]["tests"] == {"max_returns_larger": "FAIL"}
    assert rep["data"]["origin"] == "store" and rep["data"]["tests"] == ["max_returns_larger"]
    # the base pass set: the prepared fixture counts, the test that already fails is left out
    p2p = basel["data"]["pass_to_pass"]
    assert sorted(p2p["base_pass"]) == ["add_small", "add_zero", "fixture_present", "max_first",
                                        "mul_small"]
    assert basel["data"]["left_out"] == ["max_second"]
    # one fix attempt, accepted by the repair rule, verified and merged
    keys = kit.tasks_of(root, run)
    fix = keys["%s:repair:0:r1:s0" % run]
    acc = _last(root, events.ACCEPT, fix)["data"]
    assert (acc["phase"], acc["accepted"], acc["outcome"]) == ("repair", True, "fixed")
    assert _last(root, events.VERIFY_END, fix)["data"]["outcome"] == "verified"
    merge = [e for e in kit.events_of(root, events.MERGE, run) if e["data"]["repair_task"] == task]
    assert len(merge) == 1 and merge[0]["data"]["attempt"] == fix
    # the result the verifiers read, then the repair task sealed, verified and done
    res = _last(root, events.REPAIR_RESULT, task)["data"]
    assert res["outcome"] == "fixed" and res["fix_attempt"] == fix
    assert res["forked_from"] == prep["data"]["head"] and res["head"] == merge[0]["data"]["commit"]
    assert (res["prepare_event"], res["reproducer_event"], res["baseline_event"]) == (
        prep["id"], rep["id"], basel["id"])
    assert _last(root, events.VERIFY_END, task)["data"]["outcome"] == "verified"
    assert _status(root, task) == "done" and _status(root, fix) == "done"
    # the fix is on research/<run>/<task> of the repair repository, never on main
    clone = paths.clone_dir(root, run, task)
    branch = "research/%s/%s" % (run, task)
    assert kit.git(clone, "rev-parse", branch).strip() == res["head"]
    assert kit.git(clone, "show", "%s:src/calc.sh" % branch) == FIXED
    assert kit.git(clone, "show", "%s:data/fixture.txt" % branch) == "prepared\n"
    assert kit.git(root, "rev-parse", "main").strip() == later
    # history truncated at the case base: root, prepare commit, fix; nothing of the later commit
    assert kit.git(clone, "rev-list", "--count", branch).strip() == "3"
    assert subprocess.run(["git", "-C", clone, "cat-file", "-e", later],
                          capture_output=True).returncode != 0
    assert "FIXED-UPSTREAM" not in kit.git(clone, "log", "-p", "--all")
    assert os.path.basename(clone) == task and CASE not in clone and CASE not in branch
    # the fix prompts carry the bug report and the hints (FR-005)
    launches = {e["data"]["role"]: e for e in kit.events_of(root, events.LAUNCH, fix)}
    for role in ("proposer", "implementer"):
        text = open(os.path.join(root, launches[role]["data"]["prompt"]), encoding="utf-8").read()
        assert BUG.strip() in text and "Suspect files" in text and "- src/calc.sh" in text
        assert "calc-docs: src/calc.sh, src/calc.txt. calc.txt documents" in text
        assert "unrelated" not in text and "fail-to-pass:%s" % CASE in text
    impl = open(os.path.join(root, launches["implementer"]["data"]["prompt"]),
                encoding="utf-8").read()
    assert "max prints its first argument in both branches of src/calc.sh" in impl
    # the fold and the finalize
    conn = db.connect(root)
    try:
        item = state.fold(conn, run)["repair"][0]
    finally:
        conn.close()
    assert item["resolved"] is True and item["reproducer_proved"] and item["base_pass"] == 5
    assert kit.events_of(root, events.FINALIZE, run)


# ================================================================== no reproducer, no fix
@needs_jail
def test_a_reproducer_that_passes_on_the_base_blocks_the_task_before_any_fix(project, monkeypatch):
    root, _base, _later = make_project(project, store=NOT_REPRODUCING)
    got = _start(root, monkeypatch, Agents(root))
    run, (task,) = got["run"], got["repair_tasks"]
    _go(root, run)
    rep = _last(root, events.REPRODUCER, task)["data"]
    assert rep["proved"] is False and rep["code"] == "REPAIR-REPRODUCER-UNPROVED"
    assert "not FAIL on the case base: max_returns_larger (PASS)" in rep["reason"]
    # no base measurement, no fix round, no fix attempt task, no seal
    assert kit.events_of(root, events.BASELINE, task) == []
    assert [e for e in kit.events_of(root, events.ROUND_OPEN, run)
            if e["data"].get("repair_task") == task] == []
    assert [k for k in kit.tasks_of(root, run) if ":repair:0:r" in k] == []
    assert kit.events_of(root, "proof-report", task) == []
    res = _last(root, events.REPAIR_RESULT, task)["data"]
    assert (res["outcome"], res["code"], res["head"], res["diff"]) == (
        "reproducer-unproved", "REPAIR-REPRODUCER-UNPROVED", None, None)
    assert _status(root, task) == "blocked"
    move = [e for e in kit.events_of(root, "task-move", task) if e["data"]["to"] == "blocked"]
    assert move[-1]["data"]["reason"] == "REPAIR-REPRODUCER-UNPROVED"


@needs_jail
def test_a_fix_that_breaks_an_old_test_is_refused(project, monkeypatch):
    root, _base, _later = make_project(project, rounds=1)
    got = _start(root, monkeypatch, Agents(root, fix=BREAKS_ADD))
    run, (task,) = got["run"], got["repair_tasks"]
    out = _go(root, run)
    fix = kit.tasks_of(root, run)["%s:repair:0:r1:s0" % run]
    # the reproducer passes with the fix, but the old test add_small is lost: the pass-to-pass
    # item fails, so the attempt is rejected at the gates and never reaches the accept rule
    ftp = [e for e in kit.events_of(root, events.GATE, fix)
           if e["data"]["stage"] == "fail-to-pass:%s" % CASE and e["data"]["side"] == "attempt"]
    assert ftp[-1]["data"]["fail_to_pass"]["ok"] is True
    gate = [e for e in kit.events_of(root, events.GATE, fix)
            if e["data"]["stage"] == "pass-to-pass" and e["data"]["side"] == "attempt"][-1]
    assert gate["data"]["verdict"] == "FAIL"
    assert gate["data"]["pass_to_pass"]["lost"] == ["add_small"]
    rec = _last(root, events.ATTEMPT, fix)["data"]
    assert rec["outcome"] == "gate-failed" and kit.events_of(root, events.ACCEPT, fix) == []
    conn = db.connect(root)
    try:
        assert state.attempt_verdict(conn, fix)["verdict"] == "rejected"
    finally:
        conn.close()
    # nothing merged; at the bound the run halts on the extension hold, the task stays open
    assert [e for e in kit.events_of(root, events.MERGE, run)] == []
    assert out["code"] == vc.PAUSED
    hold = kit.events_of(root, events.HOLD, run)[-1]["data"]
    assert hold["code"] == "RUN-ROUNDS-REACHED"
    assert "%s reached 1 fix rounds" % task in hold["detail"]
    assert kit.events_of(root, events.REPAIR_RESULT, task) == []
    assert _status(root, task) not in ("done", "blocked")
    # positive control: the same run with a fix that keeps the old tests is accepted
    # (test_a_repair_is_accepted_end_to_end)


def _judge_setup(project, *, reproducer=None, after=False):
    """A repair task, its base set and one fix attempt whose gates recorded a met fail-to-pass
    and pass-to-pass; `reproducer` (proved True or False) recorded before the attempt task, or
    after it with `after`."""
    from alpaca import cli
    from alpaca.research.types import PassToPass, to_dict
    from alpaca.tests.test_research_gates import make_contract
    cli.main(["init"])
    plain_op("intent", "bar")
    conn = db.connect(project)
    c = make_contract(mode="repair")
    events.emit(conn, events.RUN_START, run="rr-001", ref="rr-001", op="op-001", session="t",
                data={"mode": "repair", "k": 1, "n": 1, "control": None,
                      "budget": {"usd": 10, "tokens": None, "wall": None}})
    line = ops.add_task(conn, "op-001", "repair", "repair", extra={"research": {
        "run": "rr-001", "role": "repair", "key": "rr-001:repair:0", "case": "c1", "index": 0,
        "repair_task": "t-001"}})
    base_set = PassToPass(baseline=(), base_pass=("t1",), attempt_pass=("t1",), lost=(), ok=True)
    events.emit(conn, events.BASELINE, run="rr-001", ref=line, op=None, session="s",
                code=vc.PASS, data={"repair_task": line, "ok": True, "min_tests": 1,
                                    "pass_to_pass": to_dict(base_set)})

    def rep():
        if reproducer is not None:
            events.emit(conn, events.REPRODUCER, run="rr-001", ref=line, op=None, session="s",
                        code=vc.PASS if reproducer else vc.FAIL,
                        data={"repair_task": line, "case": "c1", "proved": reproducer,
                              "tests": ["r1"], "files": []})
    if not after:
        rep()
    task = ops.add_task(conn, "op-001", "fix", "fix", extra={"research": {
        "run": "rr-001", "role": "attempt", "key": "rr-001:repair:0:r1:s0", "round": 1,
        "slot": 0, "mode": "repair", "repair_task": line}})
    if after:
        rep()
    ctx = RunContext(root=project, run="rr-001", op="op-001", session="s", contract=c,
                     runtime="control")
    for stage, data in (("fail-to-pass:c1", {"tests_named": ["r1"], "weak": [], "fail_to_pass": {
            "ok": True, "base": {"r1": "FAIL"}, "attempt": {"r1": "PASS"}}}),
                        ("pass-to-pass", {"pass_to_pass": {
                            "baseline": ["t1"], "base_pass": ["t1"], "attempt_pass": ["t1"],
                            "lost": [], "ok": True}})):
        events.emit(conn, events.GATE, run="rr-001", ref=task, op=None, session="s",
                    code=vc.PASS, data=dict(data, stage=stage, side="attempt"))
    a = lifecycle._Attempt(ctx, task)
    folder = a.attempt_dir()
    os.makedirs(folder, exist_ok=True)
    open(os.path.join(folder, "attempt.diff"), "wb").close()
    lifecycle._emit(conn, ctx, events.DIFF, task, code=vc.PASS, data={
        "path": os.path.relpath(os.path.join(folder, "attempt.diff"), project),
        "sha256": lifecycle._sha_file(os.path.join(folder, "attempt.diff")), "empty": True})
    return conn, ctx, a, task


@pytest.mark.parametrize("reproducer,why", [(None, "no reproducing test"),
                                            (False, "is not proved")])
def test_judge_refuses_a_fix_with_no_proved_reproducer(project, reproducer, why):
    conn, ctx, a, task = _judge_setup(project, reproducer=reproducer)
    assert a.judge(conn) == vc.PASS
    acc = kit.events_of(project, events.ACCEPT, task)[-1]["data"]
    assert (acc["accepted"], acc["outcome"]) == (False, "not-fixed") and why in acc["reason"]
    assert repair.fix_problem(conn, ctx, task) is not None
    conn.close()


def test_judge_accepts_a_fix_against_a_reproducer_proved_before_it(project):
    # the positive side of the two tests around it: same gates, a reproducer proved first
    conn, ctx, a, task = _judge_setup(project, reproducer=True)
    assert repair.fix_problem(conn, ctx, task) is None
    assert a.judge(conn) == vc.PASS
    acc = kit.events_of(project, events.ACCEPT, task)[-1]["data"]
    assert (acc["accepted"], acc["outcome"]) == (True, "fixed")
    conn.close()


def test_a_reproducer_proved_after_the_fix_started_never_accepts_it(project):
    # the fail case: a reproducing test written after the fix and tuned to it. Its gates say
    # FAIL on the base and PASS on the fix, but its proof came after the attempt began
    conn, ctx, a, task = _judge_setup(project, reproducer=True, after=True)
    problem = repair.fix_problem(conn, ctx, task)
    assert problem and "after the fix attempt %s started" % task in problem
    assert a.judge(conn) == vc.PASS
    acc = kit.events_of(project, events.ACCEPT, task)[-1]["data"]
    assert (acc["accepted"], acc["outcome"]) == (False, "not-fixed")
    assert "after the fix attempt" in acc["reason"]
    conn.close()


# ================================================================== the generated reproducer
def _generated(project, monkeypatch, **agent_kw):
    root, _base, _later = make_project(project, origin="generate")
    agents = Agents(root, **agent_kw)
    got = _start(root, monkeypatch, agents)
    ctx = _ctx(root, got)
    (task,) = got["repair_tasks"]
    return root, got, ctx, task, agents


@needs_jail
def test_a_generated_reproducer_is_written_reviewed_and_proved_live(project, monkeypatch):
    root, got, ctx, task, agents = _generated(project, monkeypatch)
    codes = _steps(root, ctx, task, ["claim", "repo", "prepare", "reproducer"])
    assert codes == [vc.PASS] * 4
    rep = _last(root, events.REPRODUCER, task)
    d = rep["data"]
    assert d["origin"] == "generate" and d["proved"] is True
    assert d["tests"] == ["max_returns_larger"]
    # one agent wrote it, a second reviewed it: two launches of the two roles, in that order
    roles = [(e["data"]["role"], e["id"]) for e in kit.events_of(root, events.LAUNCH_END, task)]
    assert [r for r, _ in roles] == [repair.WRITER, repair.REVIEWER]
    assert d["writer_event"] == roles[0][1] and d["review_event"] == roles[1][1]
    # base FAIL shown live, after the writer and before the review
    gate = [e for e in kit.events_of(root, events.GATE, task) if e["id"] == d["base_fail_event"]][0]
    assert gate["data"]["side"] == "base" and gate["data"]["ran"] is True
    assert gate["data"]["tests"] == {"max_returns_larger": "FAIL"}
    assert roles[0][1] < gate["id"] < roles[1][1] < rep["id"]
    # the writer worked in its own tree of the repair base, removed after the copy by bytes
    writer = [e for e in kit.events_of(root, events.LAUNCH, task)
              if e["data"]["role"] == repair.WRITER][0]
    folder = repair.repair_dir(root, got["run"], task)
    assert writer["data"]["cwd"] == os.path.join(folder, "writer")
    assert not os.path.exists(os.path.join(folder, "writer"))
    # the reviewer's packet: the bug report, the draft, the base FAIL log, the names; the byte
    # copies of the draft (draft/ and review/draft/) are gone once the proof is recorded
    review = os.path.join(folder, "review")
    assert agents.reviewed == [REPRODUCER]
    assert not os.path.lexists(os.path.join(review, "draft"))
    assert not os.path.lexists(os.path.join(folder, "draft"))
    assert "TEST: max_returns_larger ... FAIL" in open(os.path.join(review, "base-fail.log")).read()
    assert open(os.path.join(review, "bug_report.md")).read() == BUG
    # frozen with its proof under reproducers/<case>/, each file's sha256 in the event
    frozen = paths.reproducer_dir(root, got["run"], CASE)
    assert d["dir"] == os.path.relpath(frozen, root)
    assert [f["path"] for f in d["files"]] == ["run.sh"]
    assert repair._matches(root, d)
    # the prompts: the writer's names its draft folder, the reviewer's the names under review
    ptxt = open(os.path.join(root, writer["data"]["prompt"]), encoding="utf-8").read()
    assert repair.DRAFT_FOLDER + "/run.sh" in ptxt and BUG.strip() in ptxt
    assert "ALPACA-RESEARCH-ATTEMPT: %s" % task in ptxt
    # the launcher admits both specs: the writer writes only its tree and reads its packet;
    # the reviewer reads its packet and writes nothing; neither names the frozen reproducer
    from alpaca.research import launcher
    for role in (repair.WRITER, repair.REVIEWER):
        launcher.check_paths(root, agents.specs[role], ctx.contract)
    wspec, rspec = agents.specs[repair.WRITER], agents.specs[repair.REVIEWER]
    assert list(wspec["write_paths"]) == [os.path.join(folder, "writer")]
    assert list(wspec["read_paths"]) == [os.path.join(folder, "packet")]
    assert wspec["git_admin"] == os.path.join(folder, "writer.git-admin")
    assert list(rspec["write_paths"]) == [] and rspec["cwd"] == review
    assert (wspec["timeout_s"], rspec["timeout_s"]) == (1800, 900)
    for spec in (wspec, rspec):
        assert "reproducers" not in json.dumps(spec) and "hidden" not in json.dumps(spec)
    # a resumed step finds the proof and launches nothing
    assert repair.prove_reproducer(db.connect(root), ctx, task)["proved"] is True
    assert len(agents.calls) == 2


@needs_jail
@pytest.mark.parametrize("agent_kw,reason", [
    ({"review": {"verdict": "reject", "conditions": [], "reason": "tests the wrong thing"}},
     "did not approve: tests the wrong thing"),
    ({"review": {"verdict": "approve", "conditions": ["run it twice"], "reason": "ok"}},
     "approved with conditions"),
    ({"review": {"verdict": "approve", "reason": "ok if run twice"}}, "no `conditions` list"),
    ({"review": {"verdict": "approve", "condition": ["run it twice"], "reason": "ok"}},
     "no `conditions` list"),
    ({"review": {"verdict": "approve", "conditions": "run it twice", "reason": "ok"}},
     "no `conditions` list"),
    ({"writer_files": {"run.sh": NOT_REPRODUCING}}, "not FAIL on the case base"),
    ({"writer_files": {"run.sh": REPRODUCER, "link": None}}, "link or special file"),
    ({"writer_tests": ()}, "names no valid TEST list"),
])
def test_a_generated_reproducer_counts_only_with_review_and_base_fail(project, monkeypatch,
                                                                       agent_kw, reason):
    root, got, ctx, task, agents = _generated(project, monkeypatch, **agent_kw)
    codes = _steps(root, ctx, task, ["claim", "repo", "prepare", "reproducer", "base-measure",
                                     "rounds", "result", "seal"])
    assert codes == [vc.PASS] * 8
    d = _last(root, events.REPRODUCER, task)["data"]
    assert d["proved"] is False and reason in d["reason"], d["reason"]
    assert not os.path.exists(paths.reproducer_dir(root, got["run"], CASE))
    if "not FAIL" in reason:
        # the reviewer never saw a draft that did not fail on the base
        assert [c[0] for c in agents.calls] == [repair.WRITER]
    assert kit.events_of(root, events.BASELINE, task) == []
    assert [e for e in kit.events_of(root, events.ROUND_OPEN, got["run"])] == []
    assert _status(root, task) == "blocked"


@needs_jail
def test_a_changed_reproducer_needs_the_same_proof(project, monkeypatch):
    from alpaca.research import gates, isolation
    root, got, ctx, task, agents = _generated(project, monkeypatch)
    assert _steps(root, ctx, task, ["claim", "repo", "prepare", "reproducer"]) == [vc.PASS] * 4
    first = _last(root, events.REPRODUCER, task)
    frozen = paths.reproducer_dir(root, got["run"], CASE)
    # a change to the frozen copy after its proof: the fail-to-pass gate refuses to use it
    kit.write(frozen, "run.sh", NOT_REPRODUCING)
    conn = db.connect(root)
    ctx.clone = repair._clone(ctx, task)
    base = isolation.fresh_tree(root, ctx.clone, commit=repair.case_base(conn, ctx, task),
                                path=os.path.join(repair.repair_dir(root, got["run"], task), "t"))
    f2p = gates.fail_to_pass(conn, root, run=got["run"], task=task, contract=ctx.contract,
                             case=CASE, base=base, attempt=base, session=ctx.session)
    isolation.destroy_worktree(ctx.clone, base)
    conn.close()
    assert f2p.ok is False
    blocked = kit.events_of(root, events.GATE, task)[-1]["data"]
    assert blocked["verdict"] == "BLOCKED" and "no longer hashes" in blocked["reason"]
    # the `rounds` step proves it again before its first fix round. No writer launch of the
    # engine made this change, so it lacks the writer step of the same proof (gap map H02):
    # recorded unproved, no review launched, no fix round opened against it
    assert _steps(root, ctx, task, ["base-measure", "rounds", "result"]) == [vc.PASS] * 3
    reps = kit.events_of(root, events.REPRODUCER, task)
    assert len(reps) == 2 and reps[1]["data"]["supersedes"] == first["id"]
    again = reps[1]["data"]
    assert again["proved"] is False and "no writer launch of the engine" in again["reason"]
    assert [c[0] for c in agents.calls] == [repair.WRITER, repair.REVIEWER]
    assert [e for e in kit.events_of(root, events.ROUND_OPEN, got["run"])] == []
    res = _last(root, events.REPAIR_RESULT, task)["data"]
    assert res["outcome"] == "reproducer-unproved" and res["reproducer_event"] == reps[1]["id"]


@needs_jail
def test_a_changed_store_reproducer_that_still_fails_is_proved_before_the_round(project,
                                                                                monkeypatch):
    # origin store: the same proof is base FAIL shown live. The `rounds` step shows it again
    # for the changed copy before the round opens. The run's fingerprint (t-092) also sees the
    # changed frozen contract copy at the attempt's `open` point and records the attempt broken
    root, _base, _later = make_project(project, rounds=1)
    agents = Agents(root)
    got = _start(root, monkeypatch, agents)
    ctx = _ctx(root, got)
    (task,) = got["repair_tasks"]
    assert _steps(root, ctx, task, ["claim", "repo", "prepare", "reproducer",
                                    "base-measure"]) == [vc.PASS] * 5
    first = _last(root, events.REPRODUCER, task)
    hidden = paths.hidden_dir(root, got["run"], CASE)
    os.chmod(hidden, 0o755)                            # the frozen copy is read-only
    os.chmod(os.path.join(hidden, "run.sh"), 0o755)
    kit.write(hidden, "run.sh", REPRODUCER + "# a comment added later\n", 0o755)
    _steps(root, ctx, task, ["rounds"])
    reps = kit.events_of(root, events.REPRODUCER, task)
    assert len(reps) == 2 and reps[1]["data"]["supersedes"] == first["id"]
    again = reps[1]["data"]
    assert again["proved"] is True and again["tests"] == first["data"]["tests"]
    assert again["base_fail_event"] != first["data"]["base_fail_event"]
    assert repair._matches(root, again)
    opened = [e for e in kit.events_of(root, events.ROUND_OPEN, got["run"])
              if e["data"].get("repair_task") == task]
    assert opened and reps[1]["id"] < opened[0]["id"]
    fix = kit.tasks_of(root, got["run"])["%s:repair:0:r1:s0" % got["run"]]
    rec = _last(root, events.ATTEMPT, fix)["data"]
    assert rec["outcome"] == "broken" and kit.events_of(root, events.MERGE, got["run"]) == []


@needs_jail
def test_a_generated_reproducer_changed_outside_the_engine_is_not_proved_again(project,
                                                                               monkeypatch):
    # the attack of check round 1 (A2): a copy changed to suit a partial fix still fails on the
    # base, but a review of the changed bytes alone would not see the fixes tried since
    root, got, ctx, task, agents = _generated(project, monkeypatch)
    assert _steps(root, ctx, task, ["claim", "repo", "prepare", "reproducer"]) == [vc.PASS] * 4
    first = _last(root, events.REPRODUCER, task)
    frozen = paths.reproducer_dir(root, got["run"], CASE)
    kit.write(frozen, "run.sh", REPRODUCER + "# a comment added later\n")
    conn = db.connect(root)
    again = repair.prove_reproducer(conn, ctx, task)
    conn.close()
    assert again["proved"] is False and again["supersedes"] == first["id"], again
    assert "no writer launch of the engine made the change" in again["reason"]
    assert again["tests"] == first["data"]["tests"]
    assert [c[0] for c in agents.calls] == [repair.WRITER, repair.REVIEWER]
    assert again["base_fail_event"] is None


# ================================================================== early ends
@needs_jail
@pytest.mark.parametrize("kw,code,outcome", [
    ({"prepare_script": "exit 1\n"}, "REPAIR-PREPARE-FAILED", "prepare-failed"),
    ({"min_tests": 6}, "REPAIR-BASELINE-THIN", "baseline-thin"),
])
def test_a_failed_prepare_or_a_thin_base_set_blocks_the_task(project, monkeypatch, kw, code,
                                                              outcome):
    root, _base, _later = make_project(project, **kw)
    got = _start(root, monkeypatch, Agents(root))
    ctx = _ctx(root, got)
    (task,) = got["repair_tasks"]
    codes = _steps(root, ctx, task, list(repair.PLAN_STEPS))
    assert codes == [vc.PASS] * len(repair.PLAN_STEPS)
    assert repair.early_end(db.connect(root), task) == code
    res = _last(root, events.REPAIR_RESULT, task)["data"]
    assert (res["outcome"], res["code"]) == (outcome, code)
    assert [e for e in kit.events_of(root, events.ROUND_OPEN, got["run"])] == []
    assert _status(root, task) == "blocked"
    if outcome == "prepare-failed":
        prep = _last(root, events.PREPARE, task)["data"]
        assert prep["ok"] is False and prep["diff"] is None and prep["code"] == code
        assert kit.events_of(root, events.REPRODUCER, task) == []
        clone = repair._clone(ctx, task)
        assert clone.head == clone.base_commit                 # nothing committed
    else:
        basel = _last(root, events.BASELINE, task)["data"]
        assert basel["ok"] is False and len(basel["pass_to_pass"]["base_pass"]) == 5


# ================================================================== hints
def test_hints_name_the_suspects_the_chain_and_the_coupled_groups(project, monkeypatch):
    from alpaca.research.types import Proposal
    root, _base, _later = make_project(project)
    got = _start(root, monkeypatch, Agents(root))
    ctx = _ctx(root, got)
    (task,) = got["repair_tasks"]
    h = repair.hints(ctx, task)
    assert h["suspect_files"] == ["src/calc.sh"]
    assert h["chain"] == ["build", "fail-to-pass:%s" % CASE, "pass-to-pass"]
    assert [g["id"] for g in h["coupled"]] == ["calc-docs"]          # not `unrelated`
    assert h["evidence"] is None
    # the implementer's: the groups of the proposal's suspects and files join, with its chain
    p = Proposal(attempt="t-9", category="", sha256="", path="", fields={
        "suspects": ["src/other.sh"], "files": ["src/calc.sh"], "evidence": "symptom -> owner"})
    h2 = repair.hints(ctx, task, proposal=p)
    assert [g["id"] for g in h2["coupled"]] == ["calc-docs", "unrelated"]
    text = repair.hints_text(h2)
    assert "unrelated: src/other.sh, src/other.txt" in text and "symptom -> owner" in text
    assert "The proposal's suspects:\n- src/other.sh" in text


# ================================================================== the store
def test_the_reproducer_store_is_ignored_by_git(tmp_path):
    shutil.copy(os.path.join(REPO, ".gitignore"), tmp_path / ".gitignore")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    store = "contracts/research/%s/%s/run.sh" % (RB_ID, CASE)
    assert subprocess.run(["git", "check-ignore", "-q", store], cwd=tmp_path).returncode == 0
    # negative: the rest of contracts/ stays trackable
    assert subprocess.run(["git", "check-ignore", "-q", "contracts/README.md"],
                          cwd=tmp_path).returncode == 1
    lines = open(os.path.join(REPO, ".gitignore"), encoding="utf-8").read().splitlines()
    assert "/contracts/research/" in lines


@needs_jail
def test_the_runner_refuses_while_the_store_is_tracked(project, monkeypatch):
    root, _base, _later = make_project(project)
    store = "contracts/research/%s/%s/run.sh" % (RB_ID, CASE)
    kit.git(root, "add", "-f", store)
    kit.git(root, "commit", "-q", "-m", "track the store")
    kit.install(monkeypatch, root, Agents(root))
    with pytest.raises(errors.ResearchRefusal) as exc:
        kit.start(root, runbook=RUNBOOK)
    assert exc.value.reason == "RUN-STORE-TRACKED" and store in str(exc.value)
    # negative: untracked again, the same start opens the run
    kit.git(root, "rm", "-q", "--cached", store)
    kit.git(root, "commit", "-q", "-m", "untrack the store")
    assert kit.start(root, runbook=RUNBOOK)["repair_tasks"]


@needs_jail
def test_the_runner_refuses_a_store_tracked_by_the_root_when_the_target_is_another_repo(
        project, monkeypatch):
    # research.repo.path names a separate git repository (sub/); the store is
    # <root>/contracts/research and the project root's git tracks a path under it
    root, sub = project, os.path.join(project, "sub")
    kit.write(root, "project.yaml", "name: repair-test\n")
    kit.write(root, ".gitignore", ".alpaca/\nsub/\ncontracts/research/\n")
    for rel, text in (("src/calc.sh", BUGGY), ("src/calc.txt", "add, mul and max\n"),
                      ("pack/spec.md", SPEC), ("pack/tests.sh", TESTS),
                      ("pack/prepare.sh", PREPARE), ("pack/bugs/max.md", BUG)):
        kit.write(sub, rel, text)
    kit.git(sub, "init", "-q", "-b", "main")
    kit.git(sub, "add", "-A")
    kit.git(sub, "commit", "-q", "-m", "base")
    base = kit.git(sub, "rev-parse", "HEAD").strip()
    rb = (RB.replace("ROUNDS", "1").replace("ORIGIN", "store").replace("BASE", base)
          .replace("MIN_TESTS", "3").replace("PREPARE_STAGE\n", "").replace("PREPARE_KEY", "")
          .replace("TESTS_LINE", "          tests: [max_returns_larger]\n")
          .replace("research:\n  mode: repair\n",
                   "research:\n  mode: repair\n  repo:\n    path: sub\n    ref: HEAD\n"))
    kit.write(sub, RUNBOOK, rb)
    kit.git(sub, "add", "-A")
    kit.git(sub, "commit", "-q", "-m", "runbook")
    store = "contracts/research/%s/%s/run.sh" % (RB_ID, CASE)
    kit.write(root, store, REPRODUCER, 0o755)
    kit.git(root, "init", "-q", "-b", "main")
    kit.git(root, "add", "-A")
    kit.git(root, "add", "-f", store)
    kit.git(root, "commit", "-q", "-m", "root with a tracked store")
    kit.install(monkeypatch, root, Agents(root))
    with pytest.raises(errors.ResearchRefusal) as exc:
        kit.start(root, runbook="sub/" + RUNBOOK)
    assert exc.value.reason == "RUN-STORE-TRACKED" and store in str(exc.value)
    # negative: untracked in the root again, the same start opens the run
    kit.git(root, "rm", "-q", "--cached", store)
    kit.git(root, "commit", "-q", "-m", "untrack the store")
    assert kit.start(root, runbook="sub/" + RUNBOOK)["repair_tasks"]


class _Repo:
    def __init__(self, path):
        self.repo_path = path


def _git_init(path, tracked=()):
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init", "-q", path], check=True)
    for rel in tracked:
        kit.write(path, rel, "x\n")
        kit.git(path, "add", "-f", rel)
    if tracked:
        kit.git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "t")


def test_the_store_check_reads_the_store_s_own_repository(tmp_path):
    store = "contracts/research/rb/c1/run.sh"
    root = str(tmp_path / "proj")
    _git_init(root, tracked=(store,))
    target = str(tmp_path / "target")
    _git_init(target)
    # the target repository tracks nothing of the store; the root does: refused
    for repo in (target, root, "."):
        with pytest.raises(errors.ResearchRefusal) as exc:
            runner._store_tracked(root, _Repo(repo))
        assert exc.value.reason == "RUN-STORE-TRACKED" and store in str(exc.value)
    # the target is an outer repository that holds the project root: its tracking counts too
    outer = str(tmp_path / "outer")
    inner = os.path.join(outer, "proj")
    _git_init(outer, tracked=("proj/" + store,))
    os.makedirs(inner, exist_ok=True)
    with pytest.raises(errors.ResearchRefusal) as exc:
        runner._store_tracked(inner, _Repo(outer))
    assert "proj/" + store in str(exc.value)
    # negative: nothing tracked anywhere, and a root that is not in a git work tree, pass
    clean = str(tmp_path / "clean")
    _git_init(clean)
    assert runner._store_tracked(clean, _Repo(".")) is None
    plain = str(tmp_path / "plain")
    os.makedirs(plain)
    assert runner._store_tracked(plain, _Repo(".")) is None


@pytest.mark.parametrize("how", ["oserror", "timeout", "ls-files-fails", "rev-parse-fails"])
def test_the_store_check_fails_closed_when_git_cannot_answer(tmp_path, monkeypatch, how):
    root = str(tmp_path / "proj")
    _git_init(root)
    real = subprocess.run

    def fake(argv, **kw):
        if argv[:1] == ["git"]:
            if how == "oserror":
                raise OSError("git: not found")
            if how == "timeout":
                raise subprocess.TimeoutExpired(argv, 120)
            if how == "ls-files-fails" and "ls-files" in argv:
                return subprocess.CompletedProcess(argv, 128, b"", b"fatal: index broken")
            if how == "rev-parse-fails" and "rev-parse" in argv:
                return subprocess.CompletedProcess(argv, 128, b"", b"fatal: dubious ownership")
        return real(argv, **kw)
    monkeypatch.setattr(runner.subprocess, "run", fake)
    with pytest.raises(errors.ResearchRefusal) as exc:
        runner._store_tracked(root, _Repo("."))
    assert exc.value.reason == "RUN-STORE-TRACKED" and "could not be checked" in str(exc.value)


# ================================================================== the bound is per task
def _bound_events(conn, run, *, task, hold_detail_task=None, lifted=True, answer="extension",
                  rounds_to=5, decision="d-1"):
    """Round 1 of `task` opened, its extension hold, the owner's lift and extend."""
    events.emit(conn, events.ROUND_OPEN, run=run, ref=run, op=None, session="s",
                data={"round": 1, "repair_task": task})
    hold = events.emit(conn, events.HOLD, run=run, ref=run, op=None, session="s",
                       data={"gate": "extension", "detail": "%s reached 2 fix rounds with no fix"
                             % (hold_detail_task or task)})
    if lifted:
        events.emit(conn, events.HOLD_LIFTED, run=run, ref=run, op=None, session="owner",
                    data={"hold": hold["id"], "decision": decision, "answer": answer})
    events.emit(conn, events.EXTEND, run=run, ref=run, op=None, session="owner",
                data={"decision": decision, "hold": hold["id"], "rounds_from": 2,
                      "rounds_to": rounds_to})
    return hold


def test_an_extension_raises_only_the_bound_of_the_repair_task_it_was_granted_to(project):
    from alpaca import cli
    from alpaca.tests.test_research_gates import make_contract
    cli.main(["init"])
    c = make_contract(mode="repair", rounds=2)
    run = "rr-001"
    conn = db.connect(project)
    # task A halts at N and the owner extends it to 5: A's bound is 5
    _bound_events(conn, run, task="t-001")
    assert runner.rounds_bound(conn, run, c, repair_task="t-001") == 5
    # task B runs after A: A's extension leaves B at N, so B opens its own hold at N
    events.emit(conn, events.ROUND_OPEN, run=run, ref=run, op=None, session="s",
                data={"round": 1, "repair_task": "t-002"})
    assert runner.rounds_bound(conn, run, c, repair_task="t-002") == 2
    assert runner.rounds_bound(conn, run, c, repair_task="t-009") == 2      # never ran
    # the improve loop's run-wide bound (repair_task None) is unchanged
    assert runner.rounds_bound(conn, run, c) == 5
    # B's own extension raises B to 3 and leaves A at 5
    events.emit(conn, events.HOLD, run=run, ref=run, op=None, session="s",
                data={"gate": "extension", "detail": "t-002 reached 2 fix rounds"})
    hold_b = [e for e in kit.events_of(project, events.HOLD, run)][-1]
    events.emit(conn, events.HOLD_LIFTED, run=run, ref=run, op=None, session="owner",
                data={"hold": hold_b["id"], "decision": "d-2", "answer": "extension"})
    events.emit(conn, events.EXTEND, run=run, ref=run, op=None, session="owner",
                data={"decision": "d-2", "hold": hold_b["id"], "rounds_from": 2,
                      "rounds_to": 3})
    assert runner.rounds_bound(conn, run, c, repair_task="t-002") == 3
    assert runner.rounds_bound(conn, run, c, repair_task="t-001") == 5
    # a hold whose detail names another task, or one with no round of the task before it,
    # is not the task's own
    _bound_events(conn, "rr-002", task="t-003", hold_detail_task="t-004", decision="d-3")
    assert runner.rounds_bound(conn, "rr-002", c, repair_task="t-003") == 2
    assert runner.rounds_bound(conn, "rr-002", c, repair_task="t-004") == 2
    conn.close()


@needs_jail
def test_an_extension_of_one_repair_task_leaves_the_next_at_n(project, monkeypatch):
    # two cases, N = 1. The first repair task halts at 1 fix round; the owner extends it to 2
    # and its round 2 fix is accepted. The second task must still halt at N = 1
    # (architecture.md section 4.3: the granted rounds_to is that repair task's bound)
    from alpaca.research import holds
    root, _base, _later = make_project(project, rounds=1)
    text = open(os.path.join(root, RUNBOOK), encoding="utf-8").read()
    start = text.index("      - id: max-larger\n")
    end = text.index("  coupled:\n")
    text = text[:end] + text[start:end].replace("id: max-larger", "id: max-larger-b") + text[end:]
    kit.write(root, RUNBOOK, text)
    kit.git(root, "add", "-A")
    kit.git(root, "commit", "-q", "-m", "a second case")
    kit.write(root, "contracts/research/%s/max-larger-b/run.sh" % RB_ID, REPRODUCER, 0o755)
    got = _start(root, monkeypatch, Agents(root, fix=[BREAKS_ADD, FIXED, BREAKS_ADD]))
    run, (first, second) = got["run"], got["repair_tasks"]
    assert _go(root, run)["code"] == vc.PAUSED
    conn = db.connect(root)
    hold = holds.open_holds(conn, run)[0]
    assert hold["gate"] == "extension" and "%s reached 1 fix rounds" % first in hold["detail"]
    holds.lift_hold(conn, root, hold=hold["id"], decision="d-ext", answer="extension",
                    session="owner")
    events.emit(conn, events.EXTEND, run=run, ref=run, op=None, session="owner", actor="owner",
                data={"decision": "d-ext", "hold": hold["id"], "rounds_from": 1, "rounds_to": 2})
    conn.close()
    assert _go(root, run)["code"] == vc.PAUSED
    merged = [e["data"]["repair_task"] for e in kit.events_of(root, events.MERGE, run)]
    assert merged == [first]

    def rounds_of(task):
        return [e["data"]["round"] for e in kit.events_of(root, events.ROUND_OPEN, run)
                if e["data"].get("repair_task") == task]
    assert rounds_of(first) == [1, 2] and rounds_of(second) == [1]
    newest = holds.open_holds(db.connect(root), run)[0]
    assert newest["id"] > hold["id"]
    assert "%s reached 1 fix rounds" % second in newest["detail"]


# ---------------------------------------------------------------- t-357: one tree per writer try
# in-session-runtime.md section 28.1: writer try n works in `writer` (n = 1) or `writer-t<n>`; the
# draft is copied from the tree of the try whose result came back, and every writer tree is
# removed when the step ends (t-357 attack a08).
class _DeadFirstWriter(Agents):
    def act(self, role, research, spec):
        if role == repair.WRITER:
            self.writer_specs = getattr(self, "writer_specs", []) + [dict(spec)]
            if spec["try_n"] == 1:
                kit.write(spec["cwd"], os.path.join(repair.DRAFT_FOLDER, "stray.sh"),
                          "echo half-done work of try 1\n", 0o755)
                return {"outcome": "crash"}
            dead = self.writer_specs[0]["cwd"]           # try 1's call lands late
            kit.write(dead, os.path.join(repair.DRAFT_FOLDER, "late.sh"),
                      "echo a late write of try 1\n", 0o755)
        return super().act(role, research, spec)


@needs_jail
def test_each_writer_try_has_its_own_tree_and_the_draft_comes_from_the_last(project, monkeypatch):
    root, _base, _later = make_project(project, origin="generate")
    agents = _DeadFirstWriter(root)
    got = _start(root, monkeypatch, agents)
    ctx = _ctx(root, got)
    (task,) = got["repair_tasks"]
    codes = _steps(root, ctx, task, ["claim", "repo", "prepare", "reproducer"])
    assert codes == [vc.PASS] * 4, codes
    specs = agents.writer_specs
    assert [s["try_n"] for s in specs] == [1, 2]
    assert [os.path.basename(s["cwd"]) for s in specs] == ["writer", "writer-t2"]
    for s in specs:
        assert s["cwd"] in s["prompt"] and tuple(s["write_paths"]) == (s["cwd"],)
    d = _last(root, events.REPRODUCER, task)["data"]
    assert sorted(f["path"] for f in d.get("files") or []) == ["run.sh"]
    # every writer tree is gone once the step ends
    folder = os.path.dirname(specs[0]["cwd"])
    assert not [n for n in os.listdir(folder) if n.startswith("writer")], os.listdir(folder)


class _SeesLeftovers(Agents):
    """Lists the repair folder when the writer's first try runs."""
    def act(self, role, research, spec):
        if role == repair.WRITER and not hasattr(self, "seen"):
            self.seen = sorted(os.listdir(os.path.dirname(spec["cwd"])))
        return super().act(role, research, spec)


@needs_jail
def test_a_killed_runners_writer_trees_are_gone_before_the_first_writer_try(project, monkeypatch):
    """A writer tree a killed runner left (`writer-t3`, with a draft the dead try wrote, and the
    host folder of a cut it never finished) is removed when the step starts, so it is not on disk
    while the next writer works (section 28.1)."""
    root, _base, _later = make_project(project, origin="generate")
    agents = _SeesLeftovers(root)
    got = _start(root, monkeypatch, agents)
    ctx = _ctx(root, got)
    (task,) = got["repair_tasks"]
    assert _steps(root, ctx, task, ["claim", "repo", "prepare"]) == [vc.PASS] * 3
    folder = repair.repair_dir(root, got["run"], task)
    for name in ("writer-t3", "writer-t4.git-host"):
        kit.write(os.path.join(folder, name), os.path.join(repair.DRAFT_FOLDER, "late.sh"),
                  "echo a late write of a dead try\n", 0o755)
    assert _steps(root, ctx, task, ["reproducer"]) == [vc.PASS]
    assert not [n for n in agents.seen if n.startswith(("writer-t3", "writer-t4"))], agents.seen
    assert "writer" in agents.seen


def test_a_killed_runners_writer_trees_are_dropped_before_the_writer_starts(tmp_path):
    """Every `writer` and `writer-t<n>` tree and its host and admin folders go, also a host folder
    a killed create left alone; any other folder of the repair task stays."""
    import types as _types
    folder = tmp_path / "repair" / "t-9"
    for name in ("writer", "writer.git-host", "writer.git-admin", "writer-t2", "writer-t3.git-host",
                 "draft", "packet", "writers", "writer-x"):
        (folder / name).mkdir(parents=True)
        (folder / name / "f").write_text("x\n")
    repair._drop_writer_trees(_types.SimpleNamespace(folder=str(folder)), None)
    assert sorted(os.listdir(folder)) == ["draft", "packet", "writer-x", "writers"]
