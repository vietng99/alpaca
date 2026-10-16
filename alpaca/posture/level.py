"""The autodrive level in force, mirrored into the record and read at every gate (M3.1).

The rule is `doctrine/leaves/autodrive-levels.md` and `doctrine/leaves/level-gated-advance.md`.
The autodrive ladder is two instruments; the session-posture half is the one kept here (the
standing-authority half is alpaca/posture/authority.py).

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
import re

from alpaca import db, paths, project, util
from alpaca.phase import defaults

#: the actor a mirror records under when the skill state, not a named owner, drove the change.
ACTOR = "autodrive"

#: the actor a raise records under when the owner's own `/autodrive` call proves it (account_raise).
OWNER_ACTOR = "owner"

#: the decision kind the level mirror lands under. A level change is one decision event (Q15):
#: there is no separate override ledger, so the change lives on the board as this one row.
DECISION_KIND = "level"

#: the autodrive ladder band (L1-L6). A level off the band is refused.
MIN_LEVEL, MAX_LEVEL = 1, 6

#: the fresh-install default-level when project.yaml states none.
DEFAULT_FALLBACK = 2

#: `/autodrive off`: the skill writes the token OFF. In the record, off is the default-level with
#: this goal text, so an owner's off reaches every gate the same way a lower level does.
OFF = "OFF"
OFF_GOAL = "autodrive off"


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


def levels(conn, sessions, *, root=None) -> dict:
    """The level in force for each of `sessions`, as the hub shows it: sid -> {"level": "L5" or
    "OFF", "set": bool, "goal": str}. `set` is False when the record holds no level decision for
    the session and the project default-level applies. One read of the level decisions; never writes.
    """
    want = {s for s in sessions if s}
    latest = {}
    if want:
        for row in conn.execute("SELECT session, body FROM decisions WHERE kind=? ORDER BY id", (DECISION_KIND,)):
            if row[0] in want:
                try:
                    latest[row[0]] = json.loads(row[1])
                except (TypeError, ValueError):
                    continue
    fallback = "L%d" % default_level(root)
    out = {}
    for sid in want:
        rec = latest.get(sid)
        goal = str((rec or {}).get("consequence") or "").strip()
        if rec is not None and goal == OFF_GOAL:
            out[sid] = {"level": OFF, "set": True, "goal": ""}
            continue
        try:
            label = "L%d" % defaults.level_num(rec.get("choice")) if rec is not None else None
        except (ValueError, KeyError, TypeError):
            label = None
        out[sid] = {"level": label, "set": True, "goal": goal} if label else {"level": fallback, "set": False, "goal": ""}
    return out


def set(conn, session, n, goal, actor=ACTOR, *, root=None, owner_word=None, pointer="") -> dict:
    """Mirror the level a skill set for `session` into the record as a decision row (M2.6)
    carrying the verbatim goal. `owner_word` (an alpaca.ownerword row) and `pointer` ride on the
    decision when the owner's own words in the session transcript prove the change (`account_raise`).

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
        choice=label, consequence=consequence, pointer=pointer or "",
        actor=actor or ACTOR, session=session, root=root, owner_word=owner_word)
    if db.patch(conn, "sessions", "sid", session, {"level": label}) is None:
        db.upsert(conn, "sessions", "sid",
                  {"sid": session, "started": util.now_iso(), "level": label})
    # a new level or goal re-arms the L6 stop: clear the old goal's done file, as the shared
    # skill's setter does for its own marker
    try:
        os.remove(os.path.join(paths.config_dir(_root(root)), ".autodrive-done.%s" % session))
    except OSError:
        pass
    out = dict(rec)
    out["level"] = rank
    out["goal"] = goal_text
    return out


def set_local(conn, session, n, goal, actor=ACTOR, *, root=None, owner_word=None, pointer=""):
    """Set explicit project posture and keep the local skill mirror in agreement. `n` may be OFF:
    the record then holds the default-level and the skill file holds OFF."""
    off = str(n).strip().upper() == OFF
    if off:
        n, goal = default_level(root), OFF_GOAL
    rec = set(conn, session, n, goal, actor=actor, root=root, owner_word=owner_word,
              pointer=pointer)
    directory = paths.config_dir(root)
    util.write_text(os.path.join(directory, ".autodrive-level.%s" % session),
                    (OFF if off else "L%d" % rec["level"]) + "\n")
    util.write_text(os.path.join(directory, ".autodrive-goal.%s" % session),
                    str(goal or "") + "\n")
    return rec


def _read_level_file(path):
    """The level token in one skill state file: a rank, OFF, or None when absent or unreadable."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            v = fh.read().strip().upper()
    except OSError:
        return None
    if v == OFF:
        return OFF
    if v.startswith("L") and v[1:2].isdigit():
        try:
            return defaults.level_num(v[:2])
        except ValueError:
            return None
    return None


def skill_state(session, *, root=None):
    """The `/autodrive` skill's per-session state from the config dir: (rank, goal).

    Mirrors `alpaca.hooks.common.autodrive_level` and additionally reads the goal the skill stored.
    `rank` is OFF when the skill turned autodrive off for this session. A session file, whatever it
    holds, is the answer: the shared `.autodrive-level` file is read only when the session has none.
    Returns (None, "") when the skill set no level for this session, which is the signal that the
    project runs at the default-level and no op is opened on its own.
    """
    rank = None
    for name in (".autodrive-level.%s" % session, ".autodrive-level"):
        p = os.path.join(paths.config_dir(root), name)
        if os.path.isfile(p):
            rank = _read_level_file(p)
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


def account_dir():
    """The account config dir the shared `/autodrive` skill writes to when it runs outside Alpaca's
    own setter: $CLAUDE_CONFIG_DIR, else ~/.claude."""
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def account_drop(conn, session, *, root=None):
    """A level drop the owner made through the shared skill, read from the account dir.

    The owner's `/autodrive off` (or a lower level) lands in the account dir, not in this project's
    config dir, so the record never saw it and the L6 Stop refusal held. Posture stays
    project-local: this reads only this session's file and only honors it when it is a DROP
    (OFF, or a rank below the level in force) written after the latest level decision. Dropping to
    a safer level is always allowed and needs no owner words; a raise from the account dir is
    `account_raise`'s question and is honored only on the owner's own typed command. Returns the
    rank (or OFF) to mirror, or None.
    """
    p = os.path.join(account_dir(), ".autodrive-level.%s" % session)
    rank = _read_level_file(p)
    if rank is None:
        return None
    current = in_force(conn, session, root=root)
    target = default_level(root) if rank == OFF else rank
    if target >= current:
        return None
    since = latest_decision_epoch(conn, session)
    try:
        if since is not None and os.path.getmtime(p) <= since:
            return None
    except OSError:
        return None
    return rank


#: the owner's `/autodrive` command in one owner message: the skill invocation Claude Code writes
#: for a typed slash command, or a prompt whose whole text starts with the command (a message
#: queued mid-turn keeps the typed text).
#: The Alpaca plugin ships its own `autodrive` skill, which Claude Code names `/alpaca:autodrive`.
_SKILL_NAME = re.compile(r"<command-name>\s*/(?:alpaca:)?autodrive\s*</command-name>")
_SKILL_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
_PLAIN = re.compile(r"\A/(?:alpaca:)?autodrive(?:\s+(.*))?\Z", re.S)
_LEVEL_TOKEN = re.compile(r"L([1-6])", re.I)


def owner_command(text):
    """What one owner message asks of `/autodrive`: (rank, goal), (OFF, goal), (None, "") for an
    `/autodrive` call with no valid level, or None when the message is not an `/autodrive` call.
    The goal is the text after the level, one pair of surrounding quotes dropped. A message that
    only mentions the command (`why did /autodrive L6 not work?`) is not a call."""
    body = (text or "").strip()
    if _SKILL_NAME.search(body):
        m = _SKILL_ARGS.search(body)
        args = m.group(1).strip() if m else ""
    else:
        m = _PLAIN.match(body)
        if m is None:
            return None
        args = (m.group(1) or "").strip()
    parts = args.split(None, 1)
    if not parts:
        return None, ""
    token = parts[0].strip()
    if token.upper() == OFF:
        rank = OFF
    elif _LEVEL_TOKEN.fullmatch(token):
        rank = int(token[1])
    else:
        return None, ""
    goal = parts[1].strip() if len(parts) > 1 else ""
    if len(goal) >= 2 and goal[0] == goal[-1] and goal[0] in "\"'":
        goal = goal[1:-1].strip()
    return rank, goal


def account_raise(conn, session, *, root=None):
    """A level raise the OWNER made through the shared skill, read from the account dir.

    The shared `/autodrive` skill writes the owner's `/autodrive L<n>` to the account dir, so the
    record never saw it and gates ran at the default-level. Raising your own level is not allowed
    (CLAUDE.md rule 6), so the account file alone never raises: it counts only when
      * this session's file holds a rank ABOVE the level in force, written after the latest level
        decision for the session (the same file-age rule as `account_drop`), and
      * this session's transcript (the one its first session-start recorded, at the place Claude
        Code writes it: `alpaca.ownerword.transcript_path`) holds an owner message, under the
        owner-word filter (a typed prompt or a queued command of human origin, never a sidechain,
        meta, compact-summary or `claude -p` entry), that is an `/autodrive` call typed after the
        latest level decision, and the LATEST such call names exactly the file's rank.
    Returns (rank, goal, owner_word) or None. The owner_word row is the one the other owner-word
    writes store: the transcript path, the line, the timestamp and the whole message as the quote,
    so `ownerword.decision_is_owner` re-reads it. Reading never writes.
    """
    p = os.path.join(account_dir(), ".autodrive-level.%s" % session)
    rank = _read_level_file(p)
    if rank is None or rank == OFF:
        return None
    if rank <= in_force(conn, session, root=root):
        return None
    since_epoch = latest_decision_epoch(conn, session)
    try:
        if since_epoch is not None and os.path.getmtime(p) <= since_epoch:
            return None
    except OSError:
        return None
    from alpaca import ownerword
    try:
        path = ownerword.transcript_path(conn, session, _root(root))
    except ownerword.OwnerWordRefusal:
        return None
    found = _owner_call_for(conn, session, path, rank)
    if found is None:
        return None
    goal, word = found
    return rank, goal, word


def _latest_owner_call(conn, session, path):
    """The latest `/autodrive` call among the owner messages of the transcript at `path` typed
    after the latest level decision for `session`: (message, asked rank or OFF or None, goal), or
    None when there is none."""
    from alpaca import ownerword
    since = _latest_decision_ts(conn, session)
    latest = None
    for m in ownerword.owner_messages(path, contains="autodrive"):
        asked = owner_command(m["text"])
        if asked is None or not ownerword.typed_after(m, since):
            continue
        # the latest by typing time, not by file line: a resumed session can copy an older
        # message below a newer one; on equal times the later line wins
        at = ownerword.typed_at(m)
        if latest is None or (at is not None and (latest[0] is None or at >= latest[0])):
            latest = (at, m, asked)
    if latest is None:
        return None
    _at, m, (asked, goal) = latest
    return m, asked, goal


def _owner_call_for(conn, session, path, rank):
    """(goal, owner_word) when the latest owner `/autodrive` call in the transcript at `path`,
    typed after the latest level decision, names exactly `rank`; else None. The owner_word row is
    the one the other owner-word writes store, so `ownerword.decision_is_owner` re-reads it."""
    latest = _latest_owner_call(conn, session, path)
    if latest is None:
        return None
    m, asked, goal = latest
    if asked != rank:
        return None
    word = {"path": "session", "transcript": path, "line": m["line"],
            "timestamp": m["timestamp"], "quote": m["text"].strip(),
            "answer": OFF if rank == OFF else "L%d" % rank}
    return goal, word


def owner_raise(conn, session, rank, *, root=None, environ=None, isatty=None, read=None):
    """Prove the OWNER asked to raise `session` to `rank`, or raise ownerword.OwnerWordRefusal.
    Returns (goal, owner_word): the goal the owner typed with the command ("" when none, and
    always "" on the terminal path) and the owner_word row the decision stores. Writes nothing.

    Raising your own level is not allowed (CLAUDE.md rule 6). The two owner-word paths of
    `alpaca.ownerword` apply:
      * inside an agent session (`ownerword.real_session` names one), `session` must be that
        session (OWNER-WORD-SESSION), its transcript must be the one its first session-start
        recorded (OWNER-WORD-NO-TRANSCRIPT), and the latest owner `/autodrive` call in it, typed
        after the latest level decision for the session, must name exactly L<rank>
        (OWNER-WORD-LEVEL-RAISE). This is the same test `account_raise` applies.
      * with no agent session, the terminal path: no agent marker in the environment, stdin a
        terminal, and the person types L<rank> when asked (OWNER-WORD-NOT-TERMINAL otherwise).
    `environ`, `isatty` and `read` are for tests."""
    from alpaca import ownerword
    label = OFF if rank == OFF else "L%d" % rank
    real = ownerword.real_session(environ)
    if real is None:
        word = ownerword.human_write(conn, session, OWNER_ACTOR, subject=label, answer="yes",
                                     quote="", root=_root(root), environ=environ, isatty=isatty,
                                     read=read)
        return "", word
    if session != real:
        raise ownerword.OwnerWordRefusal(
            ownerword.R_SESSION, "this call runs in session %s but names session %s; only the "
            "owner of a session raises its level" % (real, session))
    path = ownerword.transcript_path(conn, session, _root(root), environ)
    found = _owner_call_for(conn, session, path, rank)
    if found is None:
        raise ownerword.OwnerWordRefusal(
            ownerword.R_LEVEL_RAISE, "no owner message of session %s typed after its latest level "
            "decision is an /autodrive call for %s" % (session, label))
    return found


def request(conn, session, n, goal, actor=ACTOR, *, root=None, environ=None, isatty=None,
            read=None):
    """Record a level someone ASKED for (`session level`, `session start --level`).

    A drop, OFF at or below the level in force, or the level already in force records as `set_local` always did: dropping to a
    safer level needs no owner words. A raise above the level in force for the session records
    only on the owner's own words (`owner_raise`), under the actor `owner` with that owner_word
    row and a pointer to it; the goal is the one the owner typed with the command, else `goal`.
    Otherwise ownerword.OwnerWordRefusal is raised and nothing is written. A level off L1-L6
    raises ValueError."""
    if str(n).strip().upper() == OFF:
        # OFF is the default level. Below it, OFF would lift the level: that is a raise, and it
        # needs the owner's own `/autodrive off` (or OFF typed at a terminal) like any other.
        if default_level(root) <= in_force(conn, session, root=root):
            return set_local(conn, session, OFF, goal, actor=actor, root=root)
        owner_goal, word = owner_raise(conn, session, OFF, root=root, environ=environ,
                                       isatty=isatty, read=read)
        pointer = ("%s:%s" % (word["transcript"], word["line"]) if word.get("path") == "session"
                   else "terminal: the owner typed OFF")
        return set_local(conn, session, OFF, goal, actor=OWNER_ACTOR, root=root, owner_word=word,
                         pointer=pointer)
    rank = defaults.level_num(n)
    if not (MIN_LEVEL <= rank <= MAX_LEVEL):
        raise ValueError("autodrive level out of the L1-L6 band: %r" % (n,))
    if rank <= in_force(conn, session, root=root):
        return set_local(conn, session, rank, goal, actor=actor, root=root)
    owner_goal, word = owner_raise(conn, session, rank, root=root, environ=environ,
                                   isatty=isatty, read=read)
    if word.get("path") == "session":
        pointer = "%s:%s" % (word["transcript"], word["line"])
    else:
        pointer = "terminal: the owner typed L%d" % rank
    return set_local(conn, session, rank, owner_goal or goal, actor=OWNER_ACTOR, root=root,
                     owner_word=word, pointer=pointer)


def _latest_decision_ts(conn, session):
    """The ISO timestamp of the latest level decision for `session`, or None."""
    r = conn.execute(
        "SELECT ts FROM decisions WHERE session=? AND kind=? ORDER BY id DESC LIMIT 1",
        (session, DECISION_KIND)).fetchone()
    return r["ts"] if r is not None else None


def latest_decision_epoch(conn, session):
    """When the latest level decision for `session` was recorded (epoch seconds), or None."""
    r = conn.execute(
        "SELECT ts FROM decisions WHERE session=? AND kind=? ORDER BY id DESC LIMIT 1",
        (session, DECISION_KIND)).fetchone()
    if r is None:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(r["ts"]).timestamp()
    except (TypeError, ValueError):
        return None


def mirror(conn, session, *, root=None, actor=ACTOR):
    """Reconcile the record with the skill's per-session state.

    When the skill has set a level that the record does not yet reflect (a new level, or a new
    goal at the same level), mirror it as a decision row carrying the verbatim goal. A no-op when
    the skill set nothing (the project stays at the default-level and NO op is opened) or when the
    record already reflects the skill's level and goal. Returns the new decision record, or None
    when nothing changed. Called from the SessionStart, UserPromptSubmit, PostToolUse and Stop
    hooks, so a level change reaches the record on the next boundary or tool call, and before the
    Stop hook decides whether to refuse a stop. OFF mirrors as the default-level. A drop written to
    the account dir (`account_drop`) wins over the project-local file and rewrites it. A raise
    written to the account dir (`account_raise`) lands only on the owner's own `/autodrive` call in
    this session's transcript; it is recorded under the actor `owner` with that owner_word row and
    rewrites the project-local file the same way.
    """
    rank, goal = skill_state(session, root=root)
    drop = account_drop(conn, session, root=root)
    if drop is not None:
        # rewrite the project-local file too, so the next mirror does not read the old level back
        return set_local(conn, session, drop, "", actor=actor, root=root)
    raised = account_raise(conn, session, root=root)
    if raised is not None:
        n, owner_goal, word = raised
        return set_local(conn, session, n, owner_goal, actor=OWNER_ACTOR, root=root,
                         owner_word=word, pointer="%s:%s" % (word["transcript"], word["line"]))
    if rank is None:
        return None                              # no /autodrive call: default-level, no op opened
    if rank == OFF:
        rank, goal = default_level(root), OFF_GOAL
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
