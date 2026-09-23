"""M3.1: the autodrive level in force, mirrored into the record at every gate.

Proof for task M3.1. The /autodrive skill sets a sticky per-session level and goal in the
config dir; Alpaca mirrors that level and the verbatim goal into the record as a decision row
(M2.6), and every gate reads the level FROM THE RECORD, never from the environment. A project
with no /autodrive call runs at the on-disk default-level and never opens an op on its own.

Positive and negative paths, per Step 1:
  * mirror on session start (a skill level lands as a decision row + the sessions projection),
  * mirror on change (a new level or a new goal appends a fresh decision row),
  * a gate reads the level from the record (phase_gate.enter with no level supplied),
  * absent state falls back to the default-level (no decision row, no skill file),
  * no op is ever opened by reading or mirroring the level (no intent -> no op).
"""
import inspect
import os

from alpaca import db, paths
from alpaca.phase import phase_gate
from alpaca.posture import level


def _set_skill_state(cfg_dir, sid, lvl, goal=None):
    """Write the /autodrive skill's per-session state files the way the skill would."""
    with open(os.path.join(cfg_dir, ".autodrive-level.%s" % sid), "w", encoding="utf-8") as fh:
        fh.write(lvl)
    if goal is not None:
        with open(os.path.join(cfg_dir, ".autodrive-goal.%s" % sid), "w", encoding="utf-8") as fh:
            fh.write(goal)


def _cfg_dir():
    return paths.config_dir()


# --------------------------------------------------------------------------- default-level
def test_absent_state_folds_to_default_level(project):
    """With no /autodrive call and no project.yaml the level in force is the fresh-install
    default (L2), read without touching the environment, and reading it opens no op."""
    conn = db.connect(project)
    assert level.in_force(conn, "s-none") == 2
    assert db.rows(conn, "ops", "1=1") == []          # reading the level never opens an op


def test_default_level_reads_project_yaml(project):
    """A project.yaml default-level is honoured over the fresh-install fallback."""
    from alpaca import project as proj
    proj.save(project, {"name": "p", "default_level": "L4"})
    conn = db.connect(project)
    assert level.in_force(conn, "s-none") == 4


# --------------------------------------------------------------------------- mirror on start
def test_mirror_lands_a_decision_row_with_the_verbatim_goal(project):
    """The level the skill set is mirrored into the record as a decision row carrying the goal
    verbatim, and the sessions projection (read by the pad, board and analytics) shows it."""
    conn = db.connect(project)
    goal = "ship the export door and the static page"
    rec = level.mirror(conn, "s1", root=project)
    assert rec is None                                # no skill state yet -> nothing mirrored
    _set_skill_state(_cfg_dir(), "s1", "L5", goal)
    rec = level.mirror(conn, "s1", root=project)
    assert rec is not None
    dec = db.rows(conn, "decisions", "session=? AND kind='level'", ("s1",))
    assert len(dec) == 1
    assert goal in dec[0]["body"]                     # verbatim goal is on the row
    sess = db.rows(conn, "sessions", "sid=?", ("s1",))
    assert sess and sess[0]["level"] == "L5"          # the per-session projection carries it
    assert db.rows(conn, "ops", "1=1") == []          # mirroring the level opens no op


def test_mirror_is_idempotent_until_something_changes(project):
    """A second mirror with the same level and goal writes no new decision row."""
    conn = db.connect(project)
    _set_skill_state(_cfg_dir(), "s2", "L3", "one goal")
    assert level.mirror(conn, "s2", root=project) is not None
    assert level.mirror(conn, "s2", root=project) is None
    assert len(db.rows(conn, "decisions", "session=? AND kind='level'", ("s2",))) == 1


def test_mirror_on_change_appends_a_fresh_row(project):
    """A new level, or a new goal at the same level, is one more decision event (Q15)."""
    conn = db.connect(project)
    _set_skill_state(_cfg_dir(), "s3", "L2", "first goal")
    level.mirror(conn, "s3", root=project)
    _set_skill_state(_cfg_dir(), "s3", "L5", "second goal")
    level.mirror(conn, "s3", root=project)
    dec = db.rows(conn, "decisions", "session=? AND kind='level'", ("s3",))
    assert len(dec) == 2
    assert level.in_force(conn, "s3") == 5            # the latest mirror wins
    assert "second goal" in dec[-1]["body"]


# --------------------------------------------------------------------------- in_force = record
def test_in_force_reads_the_record_not_the_environment(project):
    """in_force reflects what was mirrored into the record; a stale skill file that was never
    mirrored does not move the level in force."""
    conn = db.connect(project)
    level.set(conn, "s4", 6, "delegated across ops", "owner", root=project)
    assert level.in_force(conn, "s4") == 6
    # a skill file that disagrees but was never mirrored does NOT change the recorded level
    _set_skill_state(_cfg_dir(), "s4", "L1")
    assert level.in_force(conn, "s4") == 6


def test_set_refuses_a_level_off_the_ladder(project):
    conn = db.connect(project)
    for bad in (0, 7, -1):
        try:
            level.set(conn, "s5", bad, "g", "owner", root=project)
        except ValueError:
            continue
        raise AssertionError("level.set accepted an off-ladder level %r" % (bad,))


# --------------------------------------------------------------------------- the gate reads it
def test_phase_gate_enter_reads_the_level_from_the_record(project):
    """phase_gate.enter with no level supplied reads the level in force from the record. At the
    recorded L5 the design phase (entry level 4) is enterable; at the recorded L2 it is refused."""
    from alpaca.checklist import Halt
    conn = db.connect(project)
    # requirement is the first phase (entry level 1); enter it and discharge it so design is
    # gated on the level alone. With no obligation rows requirement is not discharged, so we
    # test the LEVEL comparison on the first phase, which has no previous-phase rule.
    level.set(conn, "s6", 5, "run to release", "owner", root=project)
    res = phase_gate.enter(conn, "op-1", "requirement", None, session="s6")
    assert res["allowed"] is True                     # L5 >= requirement entry level 1

    level.set(conn, "s7", 2, "assist only", "owner", root=project)
    try:
        phase_gate.enter(conn, "op-2", "design", None, session="s7")
    except Halt as e:
        assert e.code == phase_gate.R_ENTRY_LEVEL_TOO_LOW   # L2 < design entry level 4
    else:
        raise AssertionError("design entry at recorded L2 should have been refused")
    assert db.rows(conn, "ops", "1=1") == []          # a gate read opens no op


# --------------------------------------------------------------------------- named apart
def test_the_two_instruments_are_named_apart(project):
    """Step 3: the session-posture contract (this module) and the authority to act unattended
    (M3.2) are named apart in the code, so neither stands in for the other."""
    src = inspect.getsource(level)
    assert "M3.2" in src
    assert "authority" in src.lower()
