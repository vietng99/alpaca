"""Generic phase defaults, the phase ladder, and the 7.4 human-decision table as data.

Ported from spec 5.3 (the generic engineering-gate defaults table) and spec 7.4 (human
decision points by level, D2). Everything here is DATA, not code: a door reads the instrument
set for a phase from `GENERIC_DEFAULTS`, and whether an open boundary advances or pauses at a
given level is read from `BOUNDARY_AUTO_FROM`. A project adds its own checks under
`contracts/<phase>/` (run by `alpaca.contracts_runner`); it never edits this file to do so.

The vocabulary is full engineering (D3): requirement, design, build, verify, release.
"""
from __future__ import annotations

#: the phase ladder (D3, spec:157). Order is load-bearing: `previous_phase` walks it.
LADDER = ("requirement", "design", "build", "verify", "release")

#: The generic engineering-gate defaults, shipped in the Alpaca step models exactly as the 5.3
#: spec table states them. A project ADDS its own checks in contracts/; it never removes these.
#: This is the instrument set a door composes for the phase it closes (plus workspace_guard and
#: the project contracts).
GENERIC_DEFAULTS = {
    "requirement": [
        "spec file exists",
        "acceptance items are a keyed pipe table",
        "every item has an oracle class",
        "register derived",
        "closure R2 PASS",
    ],
    "design": [
        "design page exists",
        "every acceptance item is cited by a design section (traceability)",
        "decisions recorded as pages with a why",
    ],
    "build": [
        "build command exit 0",
        "commit hash recorded",
        "lint PASS",
        "every design section cites a commit or is marked deferred with a reason",
    ],
    "verify": [
        "test command exit 0",
        "one proof pointer per acceptance item or UNTESTED stamp",
        "blind verifier verdict row present",
        "tag oracle: Verified only with a behavioral probe",
    ],
    "release": [
        "tag or PR exists",
        "changelog derived from discharged rows",
        "manifest verified",
        "ship decision recorded (human-only, every level)",
    ],
}

#: One boundary per adjacent pair of phases, mapped to the phase the boundary CLOSES (whose
#: instrument set the door composes). These are the 7.4 rows an M1 op can reach.
BOUNDARY_PHASE = {
    "requirement->design": "requirement",
    "design->build": "design",
    "build->verify": "build",
    "verify->release": "verify",
}

#: The lowest autodrive level at which a boundary advances automatically once its links PASS,
#: read straight off the 7.4 table (D2, spec:775-786). Below it the boundary needs a recorded
#: human "go" (one event); at or above it an all-PASS door advances without one. The two
#: high-level cells the table distinguishes ("auto" vs "auto if PASS") behave identically here:
#: a door NEVER opens past a failing link, so "auto" already means "auto once every link PASSes".
#:
#:   requirement -> design : human go through L3, auto from L4
#:   design      -> build   : human go through L3, auto from L4
#:   build       -> verify  : human go through L2, auto from L3
#:   verify      -> release : human go through L4, auto from L5
BOUNDARY_AUTO_FROM = {
    "requirement->design": 4,
    "design->build": 4,
    "build->verify": 3,
    "verify->release": 5,
}

#: The level a phase declares for ENTRY (spec 5.2 item 8: "level in force >= the phase's
#: declared level"). It is the auto-from level of the boundary that enters the phase, so the
#: same 7.4 table that decides advance also decides autonomous entry. The first phase declares
#: the floor level 1: it is entered when the op is opened, gated only by the previous-phase rule
#: (which is vacuous for the first phase).
PHASE_ENTRY_LEVEL = {
    "requirement": 1,
    "design": 4,
    "build": 4,
    "verify": 3,
    "release": 5,
}


def level_num(level) -> int:
    """Parse an autodrive level to its integer rank. Accepts 'L5', '5', 5, or None. None is the
    most conservative reading (level 1): with no declared level in force, every boundary needs a
    human go and only the floor phase may be entered."""
    if level is None:
        return 1
    if isinstance(level, bool):
        raise ValueError("a boolean is not an autodrive level: %r" % (level,))
    if isinstance(level, int):
        return level
    s = str(level).strip().upper()
    if s.startswith("L"):
        s = s[1:]
    try:
        return int(s)
    except ValueError:
        raise ValueError("not an autodrive level: %r" % (level,))


def boundary_phase(boundary: str) -> str:
    """The phase a boundary closes (whose instrument set its door composes)."""
    if boundary not in BOUNDARY_PHASE:
        raise KeyError(boundary)
    return BOUNDARY_PHASE[boundary]


def boundary_decision(boundary: str, level_in_force) -> str:
    """'auto' when the level in force reaches the boundary's auto-from level, else 'human' (an
    open door at this level needs a recorded human go). Read straight off the 7.4 table."""
    if boundary not in BOUNDARY_AUTO_FROM:
        raise KeyError(boundary)
    return "auto" if level_num(level_in_force) >= BOUNDARY_AUTO_FROM[boundary] else "human"


def previous_phase(phase: str):
    """The phase immediately before `phase` on the ladder, or None for the first phase."""
    if phase not in LADDER:
        raise KeyError(phase)
    i = LADDER.index(phase)
    return LADDER[i - 1] if i > 0 else None


def phase_entry_level(phase: str) -> int:
    """The phase's declared entry level (spec 5.2 item 8)."""
    if phase not in PHASE_ENTRY_LEVEL:
        raise KeyError(phase)
    return PHASE_ENTRY_LEVEL[phase]
