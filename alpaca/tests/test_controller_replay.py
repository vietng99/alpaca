"""M3.8 proof: the controller sequencer and its crash-only replay from events.

Ports the earlier harness controller sequencer MINUS the two-human phase sign-off,
with the workspace guard first and every step reflected into the record (Step 2). The state store
is the Alpaca record itself: `controller.run` reads which steps already reached PASS from the events and
resumes at the first non-done step, so a controller killed mid-plan replays to the same state and
never re-executes a completed step.

The final test is the ported control table: the earlier harness `--selftest` fired negative + positive
controls and PASSed only if none did-not-fire. Here that table is a pytest wrapper that FAILS on any
non-PASS control (the plan's Step 2 adaptation).
"""
import pytest

from alpaca import cli, db, paths
from alpaca.formation import controller
from alpaca.gates import verdict as vc


# workspace_guard's NO-TMP invariant would BLOCK a pytest tmp root; isolate the containment
# property from the tmp-root property with an empty tmp-prefix set, exactly as the M1.15 doors do.
NOTMP = frozenset()


def _setup(project):
    cli.main(["init"])
    return db.connect(project)


def _run(conn, plan, **kw):
    kw.setdefault("tmp_prefixes", NOTMP)
    return controller.run(conn, plan, **kw)


def _step(name, log, code=vc.PASS, boom=False):
    """A plan step whose side effect (an append to `log`) proves it actually ran, so a re-run that
    RE-executes a completed step is caught by a duplicate entry."""
    def _run(conn):
        if boom:
            raise RuntimeError("step %s crashed" % name)
        log.append(name)
        return code
    return {"name": name, "run": _run}


# ----------------------------------------------------------------- sequencing
def test_an_all_pass_plan_runs_every_step_and_returns_pass(project):
    conn = _setup(project)
    log = []
    plan = {"id": "c1", "steps": [_step("a", log), _step("b", log)]}
    assert _run(conn, plan) == vc.PASS
    assert log == ["a", "b"]


def test_a_failing_step_halts_and_the_later_step_never_runs(project):
    conn = _setup(project)
    log = []
    plan = {"id": "c2", "steps": [_step("a", log),
                                  _step("b", log, code=vc.FAIL),
                                  _step("c", log)]}
    assert _run(conn, plan) == vc.FAIL
    assert log == ["a", "b"]           # 'c' never ran


def test_a_crashing_step_blocks_never_a_traceback(project):
    conn = _setup(project)
    log = []
    plan = {"id": "c3", "steps": [_step("a", log), _step("b", log, boom=True)]}
    assert _run(conn, plan) == vc.BLOCKED
    assert log == ["a"]


# ----------------------------------------------------------------- crash-only replay
def test_a_controller_killed_mid_plan_replays_from_events_to_the_same_state(project):
    conn = _setup(project)
    log = []
    # first run: 'b' crashes (the "kill"), so only 'a' reaches PASS in the record.
    plan_bad = {"id": "cr", "steps": [_step("a", log), _step("b", log, boom=True),
                                      _step("c", log)]}
    assert _run(conn, plan_bad) == vc.BLOCKED
    assert log == ["a"]
    assert controller.completed_steps(conn, "cr") == {"a"}

    # resume with a repaired plan of the SAME id: 'a' is already PASS in the record and must NOT
    # re-run; 'b' and 'c' run; the campaign reaches PASS.
    log2 = []
    plan_ok = {"id": "cr", "steps": [_step("a", log2), _step("b", log2), _step("c", log2)]}
    assert _run(conn, plan_ok) == vc.PASS
    assert log2 == ["b", "c"]          # 'a' was skipped on resume (no double-execution)
    assert controller.completed_steps(conn, "cr") == {"a", "b", "c"}


def test_replay_reaches_the_same_completed_set_as_a_clean_run(project):
    conn = _setup(project)
    # a clean single run.
    log = []
    _run(conn, {"id": "clean", "steps": [_step("a", log), _step("b", log)]})
    clean = controller.completed_steps(conn, "clean")
    # an interrupted-then-resumed run over an equivalent plan reaches the same completed set.
    log2 = []
    _run(conn, {"id": "resu", "steps": [_step("a", log2),
                                                  _step("b", log2, boom=True)]})
    log3 = []
    _run(conn, {"id": "resu", "steps": [_step("a", log3), _step("b", log3)]})
    assert controller.completed_steps(conn, "resu") == clean


# ----------------------------------------------------------------- workspace guard first
def test_the_workspace_guard_runs_first_and_blocks_a_foreign_root(project):
    conn = _setup(project)
    log = []
    plan = {"id": "wg", "steps": [_step("a", log)]}
    # a root under /tmp violates the no-tmp invariant -> BLOCKED before any step runs.
    assert controller.run(conn, plan, root="/tmp/not-a-real-alpaca-root") == vc.BLOCKED
    assert log == []


# ----------------------------------------------------------------- the ported control table
def test_control_table_all_fire(project):
    """The earlier harness --selftest control table, ported as a pytest wrapper: it FAILS on any non-PASS
    control. Each control is a small campaign run whose observed verdict is checked against the
    expected one on both the positive and the negative side."""
    conn = _setup(project)
    rows = []

    def rec(cid, ok, observed):
        rows.append((cid, ok, observed))

    # CT-01 POSITIVE: an all-PASS plan completes -> PASS.
    log = []
    v = _run(conn, {"id": "t01", "steps": [_step("a", log), _step("b", log)]})
    rec("CT-01", v == vc.PASS and log == ["a", "b"], v)

    # CT-02 NEGATIVE: a FAIL step halts; the later step never runs.
    log = []
    v = _run(conn, {"id": "t02", "steps": [_step("a", log),
                                                     _step("b", log, code=vc.FAIL),
                                                     _step("c", log)]})
    rec("CT-02", v == vc.FAIL and log == ["a", "b"], v)   # 'b' ran and FAILed; 'c' never ran

    # CT-03 NEGATIVE: resume skips a completed step (no double-execution).
    log = []
    _run(conn, {"id": "t03", "steps": [_step("a", log), _step("b", log, boom=True)]})
    log2 = []
    v = _run(conn, {"id": "t03", "steps": [_step("a", log2), _step("b", log2)]})
    rec("CT-03", v == vc.PASS and log2 == ["b"], (v, log2))

    # CT-04 NEGATIVE: a foreign (tmp) root BLOCKS before any step.
    log = []
    v = controller.run(conn, {"id": "t04", "steps": [_step("a", log)]},
                       root="/tmp/nope-alpaca-root")
    rec("CT-04", v == vc.BLOCKED and log == [], v)

    # CT-05 NEGATIVE: an empty plan BLOCKS (block on absence, never a guessed step list).
    v = _run(conn, {"id": "t05", "steps": []})
    rec("CT-05", v == vc.BLOCKED, v)

    # CT-06 NEGATIVE: a crashing step -> BLOCKED, never a traceback.
    log = []
    v = _run(conn, {"id": "t06", "steps": [_step("a", log, boom=True)]})
    rec("CT-06", v == vc.BLOCKED and log == [], v)

    # CT-07 NEGATIVE: duplicate step names are refused (resume keys on the name) -> BLOCKED.
    log = []
    v = _run(conn, {"id": "t07", "steps": [_step("a", log), _step("a", log)]})
    rec("CT-07", v == vc.BLOCKED, v)

    failed = [r for r in rows if not r[1]]
    assert not failed, "controls did not fire: %s" % failed
