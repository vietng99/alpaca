"""The floor, the two change classes, and de-escalation (M3.3).

The rule is `doctrine/leaves/two-change-classes.md` (the floor, the two change classes, and
de-escalation: a drop to a safer level is always allowed and does not discharge an obligation);
CLAUDE.md boot rules 3 and 6 state it for every session.

Two instruments live here, both pure:

  * `classify(root, path) -> "agent-writable" | "human-owned"`. Every write splits into two
    change classes (TD C0-C3 reduced to two). Agent-writable is the product tree, the `.alpaca/`
    runtime, the wiki and the rows. Human-owned is the authority surface: `contracts/`,
    `project.yaml`, `doctrine/` and the boot block (CLAUDE.md boot rule 6). The split is read
    BY PATH FROM THE MANIFEST (`ALPACA-MANIFEST`), so which paths the harness owns is the shipped
    tree's own answer and the classification cannot drift from it. `doctrine/` is an authority
    surface by boot rule 6 whether or not the manifest lists it yet, so it is always human-owned.

  * `check(action, level) -> "allow" | "review-card" | "refuse"`. The verdict a write path or
    the review card reads. A human-owned write is a review card up to L4 and an agent write with
    a recorded diff at L5 and above (Q18); an agent-writable write is allowed. The floor's
    default-deny categories refuse at every level. A de-escalation (dropping to a safer level) is
    always allowed, is one event, and discharges nothing already owed. A self-escalation (raising
    your own level) is refused at every level.

The floor is a FLOOR, not a boundary: the named default-deny categories are examples, not an
allow-list. Anything not recognised as explicitly safe -- an agent-writable write or a
de-escalation -- is refused. An unnamed irreversible action is still refused.

HONEST LIMITS: `check` returns a decision only. It never writes a review card (that is
alpaca/review.py, task M3.5), never records a diff (the write path does that), and never touches an
obligation ledger -- which is exactly why a de-escalation cannot discharge anything already owed.
It is a pure function of (action, level): no clock, no record, no side effect.
"""
from __future__ import annotations

import os

from alpaca import paths
from alpaca.phase import defaults

# ------------------------------------------------------------------ the vocabulary

#: the two change classes.
AGENT_WRITABLE = "agent-writable"
HUMAN_OWNED = "human-owned"

#: the three verdicts `check` returns.
ALLOW = "allow"
REVIEW_CARD = "review-card"
REFUSE = "refuse"

#: the autodrive band (L1-L6). A level off the band is refused (ValueError).
MIN_LEVEL, MAX_LEVEL = 1, 6

#: the level at which an agent writes a human-owned surface itself, with the diff recorded, rather
#: than raising a review card (CLAUDE.md boot rule 6).
AGENT_WRITE_FROM = 5

#: the authority surface names (boot rule 6). Matched against the manifest's [mechanism] entries so
#: the classification tracks the shipped tree; `doctrine` is added by rule even if the manifest
#: omits it. A name ending a dir is a prefix; a plain file name matches only itself.
HUMAN_OWNED_NAMES = frozenset({"contracts", "project.yaml", "doctrine", "CLAUDE.md", "AGENTS.md"})

#: default-deny categories that refuse at EVERY level (the floor). The seventh category, writing
#: the agent's own authority surface, is not here: it is the
#: human-owned change class, handled through `classify` (review card up to L4, agent write at
#: L5+). These six are flat refusals.
DENY_CATEGORIES = frozenset({
    "irreversible-external",     # an irreversible external action
    "leave-root",                # leaving the project root
    "unsanctioned-campaign",     # an unsanctioned campaign
    "edit-frozen-record",        # editing a frozen record
    "self-escalation",           # self-escalation
    "global-user-surface",       # writing a global user surface
})

#: action kinds that name a level change explicitly.
_DEESCALATE_KINDS = frozenset({"deescalate", "de-escalation"})
_ESCALATE_KINDS = frozenset({"escalate", "escalation", "level-change"})


class FloorError(ValueError):
    """A level off the L1-L6 band, or an action that is not a mapping or a name."""


# ------------------------------------------------------------------ the manifest read

def _manifest_mechanism(root: str) -> list:
    """The [mechanism] entries of `<root>/ALPACA-MANIFEST`, one path per line. An absent or
    unreadable manifest yields an empty list (only `doctrine/` and the boot block stay
    human-owned by rule); it never raises, so classify degrades safe."""
    path = os.path.join(root, paths.MANIFEST)
    entries: list = []
    section = None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return entries
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line in ("[mechanism]", "[memory]"):
            section = line[1:-1]
            continue
        if section == "mechanism":
            entries.append(line)
    return entries


def _human_owned_prefixes(root: str) -> set:
    """The human-owned path prefixes for this root, read FROM THE MANIFEST so they cannot drift
    from the shipped tree. A manifest entry whose name is an authority surface (HUMAN_OWNED_NAMES)
    is included; `doctrine` is always included by boot rule 6, whether or not the manifest lists it."""
    prefixes = set()
    for entry in _manifest_mechanism(root):
        base = entry.rstrip("/")
        if base in HUMAN_OWNED_NAMES:
            prefixes.add(base)
    prefixes.add("doctrine")  # authority surface by boot rule 6 even before the manifest lists it
    return prefixes


def _rel(root: str, path: str):
    """`path` as a forward-slash path relative to `root`, or None when it resolves at or outside
    the root. Accepts an absolute path or one relative to the root. Containment itself is
    workspace_guard's job; here a path outside the root simply carries no change class."""
    root_abs = os.path.abspath(root)
    if os.path.isabs(path):
        target = os.path.abspath(path)
    else:
        target = os.path.abspath(os.path.join(root_abs, path))
    rel = os.path.relpath(target, root_abs)
    if rel == os.curdir or rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    return rel.replace(os.sep, "/")


# ------------------------------------------------------------------ classify

def classify(root: str | None, path: str) -> str:
    """The change class of a write to `path`: HUMAN_OWNED for the authority surface
    (`contracts/`, `project.yaml`, `doctrine/`, the boot block), AGENT_WRITABLE for everything
    else (the product tree, the `.alpaca/` runtime, the wiki, the rows). Classified BY PATH FROM THE
    MANIFEST so it cannot drift from the shipped tree."""
    base = root or paths.root()
    rel = _rel(base, path)
    if rel is None:
        return AGENT_WRITABLE  # outside the root: no change class, containment is workspace_guard
    for pre in _human_owned_prefixes(base):
        if rel == pre or rel.startswith(pre + "/"):
            return HUMAN_OWNED
    return AGENT_WRITABLE


# ------------------------------------------------------------------ check

def _rank(level) -> int:
    """The integer rank of an autodrive level, validated against the L1-L6 band."""
    try:
        rank = defaults.level_num(level)
    except ValueError as e:
        raise FloorError(str(e))
    if not (MIN_LEVEL <= rank <= MAX_LEVEL):
        raise FloorError("autodrive level out of the L1-L6 band: %r" % (level,))
    return rank


def _as_action(action) -> dict:
    """Normalise an action to a mapping. A bare string is its `kind`."""
    if isinstance(action, str):
        return {"kind": action}
    if isinstance(action, dict):
        return dict(action)
    raise FloorError("an action must be a mapping or a kind name, not %s"
                     % type(action).__name__)


def check(action, level) -> str:
    """The floor verdict for `action` at the autodrive `level` in force.

    ALLOW an agent-writable write at any level, and a human-owned write at L5+ (the caller
    records the diff). REVIEW_CARD a human-owned write up to L4. A de-escalation is always
    ALLOW; a self-escalation is always REFUSE. A default-deny category, and anything not
    recognised as explicitly safe, is REFUSE -- the floor is default-deny, so an unnamed
    irreversible action is still refused.

    Pure: no clock, no record, no side effect. It cannot discharge an obligation, so a
    de-escalation leaves every open obligation open.
    """
    rank = _rank(level)
    act = _as_action(action)
    kind = act.get("kind")

    # de-escalation: always allowed, one event, discharges nothing already owed.
    if kind in _DEESCALATE_KINDS:
        return ALLOW
    # an escalate/level-change action: a raise is a self-escalation (refuse), a drop is a
    # de-escalation (allow). With no explicit from/to it is a bare self-escalation attempt.
    if kind in _ESCALATE_KINDS:
        frm, to = act.get("from"), act.get("to")
        if frm is not None and to is not None:
            return ALLOW if _rank(to) <= _rank(frm) else REFUSE
        return REFUSE

    # a write: split by change class.
    if kind == "write":
        cls = classify(act.get("root"), act.get("path"))
        if cls == AGENT_WRITABLE:
            return ALLOW
        return ALLOW if rank >= AGENT_WRITE_FROM else REVIEW_CARD

    # a named default-deny category (self-escalation among them): refuse at every level.
    if kind in DENY_CATEGORIES:
        return REFUSE

    # the floor is a floor, not a boundary: anything else is refused.
    return REFUSE
