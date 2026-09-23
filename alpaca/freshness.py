"""alpaca/freshness.py - the projection freshness gate (M2.17).

The record is the only truth. `RESUME.md`, `CHECKLIST.md`, `board.json` and `data.json` are
projections: rendered from `.alpaca/alpaca.db` and never hand-edited. This gate proves that what is on
disk is exactly what the record renders right now.

The method (Steps 1-3 of the task):

  1. For each registered projection that exists on disk, REGENERATE it in memory from the
     record. The regeneration is the trusted source; disk never self-reports its own freshness.
  2. Diff the regeneration against the disk bytes, line by line, modulo the declared volatile
     lines. The FIRST differing line names the file and the line for the finding.
  3. The volatile lines - the generation stamp and any recorded duration, which move with the
     wall clock and carry no assertion - are declared in ONE place (`VOLATILE_LINES`) and are
     excluded from both the diff and every content hash.

A projection that differs - a hand-edit, or one left stale after the record moved on - is a
finding. `check` returns the findings list (empty when clean); `verdict_of` folds it to a
verdict-band code. Every phase door composes this gate as a link (`alpaca/phase/doors.py`), so a
door never opens over a stale projection.

The ruling (Step 4) is written in `docs/projection-ruling.md` and can be recorded as a decision
page with `record_ruling`: the store is truth, the markdown under `.alpaca/wiki/` (and every other
projection) is a rendering, and a correction targets the assertion in the record, never the
rendered page.
"""
from __future__ import annotations

import os
import re

from alpaca import util
from alpaca.gates import verdict as vc

NAME = "projection-freshness"

#: the canonical BLOCK/FAIL reason token every finding leads with, so a caller greps ONE token.
TOKEN = "PROJECTION-STALE"
#: a projection present on disk that cannot be regenerated from the record: freshness cannot be
#: proven, so it fails closed.
TOKEN_UNRENDERABLE = "PROJECTION-UNRENDERABLE"

# --------------------------------------------------------------------------------------------
# THE ONE PLACE the volatile lines are declared. A line matching any of these patterns carries
# a wall-clock stamp or a recorded duration: it moves between two honest renders of the SAME
# record, so it is neutralized before the diff and before every content hash. Nothing else in
# the codebase decides what is volatile; check() and content_hash() both read this tuple.
VOLATILE_LINES = (
    re.compile(r"^updated:"),                 # RESUME.md generation stamp + live counts
    re.compile(r"^last session:"),            # RESUME.md last-beat times
    re.compile(r'^\s*"generated"\s*:'),       # board.json / data.json generation stamp
    re.compile(r'^\s*"duration[^"]*"\s*:'),   # any recorded duration is a stamp, not an input
    re.compile(r"^generated:"),               # a bare "generated:" stamp line
)

# the placeholder a volatile line collapses to; identical for every volatile line so two renders
# whose only difference is a stamp compare equal.
_VOLATILE = "\x00alpaca-volatile\x00"


# --------------------------------------------------------------------- the projection registry
def _render_resume(root) -> str:
    from alpaca import pad
    return pad.render(root)


def _render_checklist(root) -> str:
    from alpaca import pad
    return pad.render_checklist(root)


def _render_board(root) -> str:
    from alpaca import board, db
    # board.write_json writes render_json(conn) + "\n"; regenerate the exact same bytes.
    return board.render_json(db.connect(root)) + "\n"


def _render_data(root) -> str:
    from alpaca import export
    return export.render(root)


#: (filename, renderer) for each projection. The renderer takes the root and returns the exact
#: bytes the writer lays on disk. `check` iterates this by default; a caller may pass its own.
PROJECTIONS = (
    ("RESUME.md", _render_resume),
    ("CHECKLIST.md", _render_checklist),
    ("board.json", _render_board),
    ("data.json", _render_data),
)


# --------------------------------------------------------------------- neutralize / hash / diff
def _neutralized(text, volatile) -> list:
    """Split `text` into lines with every volatile line collapsed to the shared placeholder."""
    out = []
    for ln in text.split("\n"):
        out.append(_VOLATILE if any(p.search(ln) for p in volatile) else ln)
    return out


def content_hash(text, volatile_lines=None) -> str:
    """A stable hash of `text` with the declared volatile lines excluded. Two renders of the same
    record that differ only in a stamp hash identical; a change to any asserted line moves it."""
    volatile = VOLATILE_LINES if volatile_lines is None else tuple(volatile_lines)
    return util.sha256_hex("\n".join(_neutralized(text, volatile)))


def _first_diff(want_lines, disk_lines):
    """The 1-based number of the first line that differs, with the (disk, want) values, or None
    when the two neutralized line lists are identical."""
    n = max(len(want_lines), len(disk_lines))
    for i in range(n):
        w = want_lines[i] if i < len(want_lines) else None
        d = disk_lines[i] if i < len(disk_lines) else None
        if w != d:
            return i + 1, d, w
    return None


def _finding(token, name, line, detail, disk=None, want=None) -> dict:
    return {"token": token, "file": name, "line": line, "detail": detail,
            "disk": disk, "want": want}


# --------------------------------------------------------------------------------- the gate
def check(root, artifacts=None, volatile_lines=None) -> list:
    """Prove every on-disk projection matches the record's fresh render.

    `artifacts` defaults to `PROJECTIONS`; `volatile_lines` defaults to `VOLATILE_LINES`. Only
    projections that EXIST on disk are checked: a projection that was never materialized cannot
    be hand-edited or stale, so its absence is silent. Returns a findings list, empty when every
    present projection is byte-identical to its fresh render modulo the volatile lines. Each
    finding names the file and the first differing line.
    """
    artifacts = PROJECTIONS if artifacts is None else artifacts
    volatile = VOLATILE_LINES if volatile_lines is None else tuple(volatile_lines)
    findings = []
    for name, render in artifacts:
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        try:
            disk = util.read_text(path)
        except OSError:
            continue
        try:
            want = render(root)
        except Exception as e:
            findings.append(_finding(
                TOKEN_UNRENDERABLE, name, 0,
                "%s exists on disk but cannot be regenerated from the record (%s: %s); "
                "freshness cannot be proven, so the door fails closed"
                % (name, type(e).__name__, e)))
            continue
        d = _first_diff(_neutralized(want, volatile), _neutralized(disk, volatile))
        if d is not None:
            lineno, disk_line, want_line = d
            findings.append(_finding(
                TOKEN, name, lineno,
                "%s differs from the record at line %d: the record renders %r, disk holds %r. "
                "The store is truth; correct the assertion, then re-render (never hand-edit the "
                "page)." % (name, lineno, want_line, disk_line),
                disk=disk_line, want=want_line))
    return findings


def verdict_of(findings) -> int:
    """Fold a findings list to a verdict-band code: FAIL on any finding, PASS otherwise. A stale
    projection is a failed assertion about the workspace, so the door over it CLOSES."""
    return vc.FAIL if findings else vc.PASS


# ------------------------------------------------------------------------------- the ruling
RULING_KIND = "projection-ruling"
_RULING_CONTEXT = (
    "The wiki store (.alpaca/alpaca.db) and the markdown rendered under .alpaca/wiki/ can disagree: one is a "
    "store, the other a projection. Q10 and spec 5.1:210-212 settle which is truth."
)
_RULING_CHOICE = (
    "The store is truth. RESUME.md, CHECKLIST.md, board.json, data.json and the wiki markdown "
    "are projections, rendered from the record and never hand-edited. A correction targets the "
    "assertion in the record; the rendered page is then re-rendered and never edited in place."
)
_RULING_CONSEQUENCE = (
    "The projection freshness gate (alpaca.freshness.check) proves every on-disk projection matches "
    "the record's fresh render modulo declared volatile lines, and every phase door composes it "
    "as a link, so a hand-edited or stale page fails the door closed."
)


def record_ruling(conn, *, root=None, session=None, actor="alpaca") -> dict:
    """Record the store-versus-projection ruling as a decision page and return its record. The
    page is the durable, resolvable form of docs/projection-ruling.md; the spec patch queue
    points at it (Step 4)."""
    from alpaca import decisions
    return decisions.record(
        conn, RULING_KIND, _RULING_CONTEXT,
        ["store is truth, projections are rendered", "the page is truth, the record follows it"],
        _RULING_CHOICE, _RULING_CONSEQUENCE,
        actor=actor, session=session, root=root)
