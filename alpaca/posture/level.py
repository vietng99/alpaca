"""The autodrive level in force, mirrored into the record and read at every gate (M3.1).

Sources: spec 5.6:389-401, the 7.4 table (spec:775-786), D16 (spec:164), Q16 (spec:182),
absorb-gap AG-A7 (the autodrive ladder is TWO instruments; the session-posture half is the half
Alpaca kept here).

The `/autodrive` skill (D16) is shipped unchanged: `/autodrive L<n> "<goal>"` sets a sticky
per-session level and goal in the config dir, and its hooks inject the level contract. Alpaca adds
exactly two things over the skill, and this module is the FIRST of them:

  * the session-posture contract. The level a skill set for a session, and its verbatim goal, are
    mirrored into the record as a decision row (M2.6). Every gate then reads the level in force
    FROM THE RECORD (`in_force`), never from the environment, so a stale or hand-set config file
    that was never mirrored cannot move a gate. The mirror also updates the per-session
    projection so the pad, the board and the analytics show the level per session.
  * (separately) the goal text opens an intent record with the owner's done_when. That is task
    M3.6 and does not live here.

Name the two instruments apart. This module is the SESSION-POSTURE contract, injected each
session. The authority to act unattended (expiry, revocation, supersession, re-verification,
scope) is a SEPARATE instrument, task M3.2 (standing authority). Alpaca keeps both and neither stands
in for the other: a level says how much the agent decides in THIS session; an authority says
whether a loop may act with no one watching. `in_force` here answers only the first question.

A project with no `/autodrive` call runs at the on-disk `default-level` (one owner-written value
in project.yaml, fresh install L2) and never opens an op on its own: reading the level never
writes, and `mirror` is a no-op when the skill has set nothing, so no op is opened without an
intent.
"""
from __future__ import annotations

import json
import os

from alpaca import db, paths, project, util
from alpaca.phase import defaults

#: the actor a mirror records under when the skill state, not a named owner, drove the change.
ACTOR = "autodrive"

#: the decision kind the level mirror lands under. A level change is one decision event (Q15):
#: there is no separate override ledger, so the change lives on the board as this one row.
DECISION_KIND = "level"

#: the autodrive ladder band (L1-L6, spec 5.6:390). A level off the band is refused.
MIN_LEVEL, MAX_LEVEL = 1, 6

#: the fresh-install default-level when project.yaml states none (spec 5.6:398).
DEFAULT_FALLBACK = 2


def _root(root=None) -> str:
    if root:
        return root
    try:
        return paths.root()
    except Exception:
        return os.getcwd()


def default_level(root=None) -> int:
    """The on-disk `default-level`: project.yaml `default_level`, else the fresh-install L2.

    This is the level a project runs at with no `/autodrive` call. Reading it never writes and
    never opens an op.
    """
    cfg = project.load(_root(root))
    return defaults.level_num(cfg.get("default_level") or DEFAULT_FALLBACK)


def _latest_decision(conn, session):
    """The most recent level-mirror decision row for `session`, parsed, or None."""
    r = conn.execute(
        "SELECT body FROM decisions WHERE session=? AND kind=? ORDER BY id DESC LIMIT 1",
        (session, DECISION_KIND)).fetchone()
    if r is None:
        return None
    try:
        return json.loads(r["body"])
    except (TypeError, ValueError):
        return None


def in_force(conn, session, *, root=None) -> int:
    """The level in force for `session`, READ FROM THE RECORD, never from the environment.

    The latest level decision mirrored for the session wins; with none, the on-disk
    default-level. This function only reads: it never writes a row and never opens an op, so a
    gate that asks the level in force cannot, by asking, advance the project.
    """
    rec = _latest_decision(conn, session)
    if rec is not None:
        try:
            return defaults.level_num(rec.get("choice"))
        except (ValueError, KeyError, TypeError):
            pass
    return default_level(root)


def set(conn, session, n, goal, actor=ACTOR, *, root=None) -> dict:
    """Mirror the level a skill set for `session` into the record as a decision row (M2.6)
    carrying the verbatim goal.

    Validates the level band (a level off L1-L6 raises ValueError), appends one hash-chained
    decision event and its page, and updates the per-session projection (sessions.level) that the
    pad, the board and the analytics read. Returns the decision record, with the parsed rank and
    the verbatim goal attached.
    """
    rank = defaults.level_num(n)
    if not (MIN_LEVEL <= rank <= MAX_LEVEL):
        raise ValueError("autodrive level out of the L1-L6 band: %r" % (n,))
    label = "L%d" % rank
    goal_text = "" if goal is None else str(goal)
    context = ("autodrive level %s in force for session %s. This is the session-posture "
               "contract; the authority to act unattended is a separate instrument (task M3.2) "
               "and does not travel with the level." % (label, session))
    consequence = goal_text.strip() or ("level %s in force for session %s" % (label, session))
    from alpaca import decisions
    rec = decisions.record(
        conn, kind=DECISION_KIND, context=context,
        options=["L1", "L2", "L3", "L4", "L5", "L6"],
        choice=label, consequence=consequence, pointer="",
        actor=actor or ACTOR, session=session, root=root)
    if db.patch(conn, "sessions", "sid", session, {"level": label}) is None:
        db.upsert(conn, "sessions", "sid",
                  {"sid": session, "started": util.now_iso(), "level": label})
    out = dict(rec)
    out["level"] = rank
    out["goal"] = goal_text
    return out


def set_local(conn, session, n, goal, actor=ACTOR, *, root=None):
    """Set explicit project posture and keep the local skill mirror in agreement."""
    rec = set(conn, session, n, goal, actor=actor, root=root)
    directory = paths.config_dir(root)
    util.write_text(os.path.join(directory, ".autodrive-level.%s" % session),
                    "L%d\n" % rec["level"])
    util.write_text(os.path.join(directory, ".autodrive-goal.%s" % session),
                    str(goal or "") + "\n")
    return rec


def skill_state(session, *, root=None):
    """The `/autodrive` skill's per-session state from the config dir: (rank, goal).

    Mirrors `alpaca.hooks.common.autodrive_level` and additionally reads the goal the skill stored.
    Returns (None, "") when the skill set no level for this session, which is the signal that the
    project runs at the default-level and no op is opened on its own.
    """
    rank = None
    for name in (".autodrive-level.%s" % session, ".autodrive-level"):
        p = os.path.join(paths.config_dir(root), name)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                v = fh.read().strip().upper()
        except OSError:
            continue
        if v.startswith("L") and v[1:2].isdigit():
            try:
                rank = defaults.level_num(v[:2])
            except ValueError:
                rank = None
            break
    goal = ""
    for name in (".autodrive-goal.%s" % session, ".autodrive-goal"):
        p = os.path.join(paths.config_dir(root), name)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                goal = fh.read().strip()
        except OSError:
            goal = ""
        if goal:
            break
    return rank, goal


def mirror(conn, session, *, root=None, actor=ACTOR):
    """Reconcile the record with the skill's per-session state.

    When the skill has set a level that the record does not yet reflect (a new level, or a new
    goal at the same level), mirror it as a decision row carrying the verbatim goal. A no-op when
    the skill set nothing (the project stays at the default-level and NO op is opened) or when the
    record already reflects the skill's level and goal. Returns the new decision record, or None
    when nothing changed. Called from the SessionStart, UserPromptSubmit and PostToolUse hooks, so
    a level change reaches the record on the next boundary or tool call.
    """
    rank, goal = skill_state(session, root=root)
    if rank is None:
        return None                              # no /autodrive call: default-level, no op opened
    prior = _latest_decision(conn, session)
    if prior is not None:
        try:
            same_level = defaults.level_num(prior.get("choice")) == rank
        except (ValueError, KeyError, TypeError):
            same_level = False
        if same_level:
            prior_goal = str(prior.get("consequence") or "")
            if not goal or prior_goal == goal.strip():
                return None                      # already mirrored, nothing changed
    return set(conn, session, rank, goal, actor=actor, root=root)
