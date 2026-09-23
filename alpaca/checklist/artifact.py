"""Acceptance-table parsing. Ported from the earlier harness gates/row_synthesis.parse_artifact.

Adapted for Alpaca: a single free function `parse(path, key_column)` takes a filesystem path
and the header of the key column, instead of the earlier harness's (kind, relpath, kind_schema, root,
find) plumbing. The mechanism is kept whole: markdown pipe tables are found by a
header/separator pair, the one region carrying the key column is the item table, and every
line of the file is dispositioned into a ledger so that intra-file omission is visible. An
obligation-shaped token that lives OUTSIDE the item table (residue) is refused, because an
obligation derived from nothing is worse than one that is missing.

Refusals are surfaced through `alpaca.checklist.Halt` carrying a verdict-band code. There is no
default-on-miss and no cap: a malformed row is refused, never skipped, because skipping
shrinks the measured population. An empty population is BLOCKED, never a pass.
"""
from __future__ import annotations

import re
import unicodedata

from alpaca.checklist import Halt
from alpaca.gates import contract, verdict

INSTRUMENT = "artifact-parse"

_WS = re.compile(r"\s+")
_SEP_CELL = re.compile(r":?-{3,}:?")
#: a fenced-code-block delimiter: 3+ backticks or tildes, up to 3 leading spaces (CommonMark).
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def _canon(s) -> str:
    """Canonical form of a column label used as a key: NFC, whitespace collapsed, casefold."""
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    return _WS.sub(" ", unicodedata.normalize("NFC", s)).strip().casefold()


# --------------------------------------------------------------------------- pipe tables
def _is_table_line(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.endswith("|") and s.count("|") >= 3


def _is_separator(line: str) -> bool:
    if not _is_table_line(line):
        return False
    cs = _cells(line)
    return len(cs) > 0 and all(_SEP_CELL.fullmatch(c) for c in cs)


def _split_unescaped_pipes(inner: str) -> list:
    """Split on UNESCAPED `|`, resolving GFM backslash escapes so a cell may hold a pipe."""
    out, buf, i, n = [], [], 0, len(inner)
    while i < n:
        ch = inner[i]
        if ch == "\\" and i + 1 < n and inner[i + 1] in ("|", "\\"):
            buf.append(inner[i + 1])
            i += 2
            continue
        if ch == "|":
            out.append("".join(buf)); buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    out.append("".join(buf))
    return out


def _cells(line: str) -> list:
    inner = line.strip()[1:-1]
    return [c.strip() for c in _split_unescaped_pipes(inner)]


def _key_column_index(cols, key_column):
    wanted = _canon(key_column)
    hits = [i for i, c in enumerate(cols) if _canon(c) == wanted]
    if len(hits) == 1:
        return hits[0]
    return None


def _shape_of(key: str):
    """Derive a character-class pattern from an observed item key.

    The residue scanner's pattern comes from the keys the artifact actually carries; it is
    never a pattern this file was born knowing. A hand-written key pattern would be the same
    curated-list defect one level down.
    """
    runs = []
    for ch in key:
        if ch.isupper():
            cls = "U"
        elif ch.islower():
            cls = "L"
        elif ch.isdigit():
            cls = "D"
        else:
            cls = "lit:" + ch
        if runs and runs[-1] == cls:
            continue
        runs.append(cls)
    parts = []
    classes = 0
    literals = 0
    for r in runs:
        if r == "U":
            parts.append("[A-Z]+"); classes += 1
        elif r == "L":
            parts.append("[a-z]+"); classes += 1
        elif r == "D":
            parts.append("[0-9]+"); classes += 1
        else:
            # A collapsed literal run stays quantified, exactly like the U/L/D runs, so the
            # shape can fullmatch its own source key (e.g. AB--01) and the scanner does not
            # silently miss an obligation-shaped token of that key family.
            parts.append(re.escape(r[4:]) + "+"); literals += 1
    pattern = "".join(parts)
    strong = (literals >= 1 and classes >= 1) or classes >= 3
    return pattern, strong


def _fenced_line_indices(lines) -> set:
    """0-based indices of every line inside a fenced code block, delimiters included."""
    inside = set()
    fence = None  # (fence_char, opener_length)
    for i, line in enumerate(lines):
        m = _FENCE_RE.match(line)
        if fence is None:
            if m:
                fence = (m.group(1)[0], len(m.group(1)))
                inside.add(i)
        else:
            inside.add(i)
            if (m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1]
                    and set(line.strip()) == {fence[0]}):
                fence = None
    return inside


def _build_line_ledger(lines, regions, item_region_index, fenced):
    """One disposition per line. len(ledger) == len(lines), always."""
    disposition = {}
    for idx, region in enumerate(regions):
        tag = "item-table" if idx == item_region_index else "other-table"
        disposition[region["header"]] = tag + "-header"
        disposition[region["separator"]] = tag + "-separator"
        for j in region["body"]:
            disposition[j] = tag + "-row"
    ledger = []
    for i, line in enumerate(lines):
        if i in disposition:
            d = disposition[i]
        elif i in fenced:
            d = "code-fence"
        elif not line.strip():
            d = "blank"
        else:
            d = "prose"
        ledger.append({"line": i + 1, "disposition": d, "chars": len(line)})
    return ledger


class _Findings:
    """Accumulated refusals. An empty finding set is not by itself a pass; the caller must
    still show a non-empty witness population before a clean parse is reachable."""

    def __init__(self):
        self.rows = []

    def add(self, code_verdict: int, reason: str, detail: str = ""):
        self.rows.append({"verdict": code_verdict, "code": reason, "detail": str(detail)})

    def any(self) -> bool:
        return bool(self.rows)

    def worst(self) -> int:
        return contract.worst([r["verdict"] for r in self.rows])

    def summary(self) -> str:
        return "; ".join("%s [%s] %s" % (verdict.name_of(r["verdict"]), r["code"], r["detail"])
                         for r in self.rows)


def parse(path: str, key_column: str) -> dict:
    """Parse one acceptance-table artifact into items keyed on `key_column`, plus a ledger.

    Raises `Halt(BLOCKED, ...)` when the file carries no table with the key column, more
    than one such table, an empty item population, or an obligation-shaped token / stray
    table row outside the item table. Raises `Halt` (folding row-level FAILs) when a body
    row is malformed, its key is blank, or a key repeats. Returns a dict on a clean parse.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise Halt(verdict.BLOCKED, "ARTIFACT-NOT-UTF8", "%s: %s" % (path, e))
    lines = text.splitlines()
    fenced = _fenced_line_indices(lines)

    regions = []
    i, n = 0, len(lines)
    while i < n:
        if (i not in fenced and (i + 1) not in fenced
                and _is_table_line(lines[i]) and i + 1 < n and _is_separator(lines[i + 1])):
            body = []
            j = i + 2
            while (j < n and j not in fenced
                   and _is_table_line(lines[j]) and not _is_separator(lines[j])):
                body.append(j)
                j += 1
            regions.append({"header": i, "separator": i + 1, "body": body,
                            "cols": _cells(lines[i])})
            i = j
        else:
            i += 1

    candidates = [idx for idx, r in enumerate(regions)
                  if _key_column_index(r["cols"], key_column) is not None]
    if len(candidates) == 0:
        raise Halt(verdict.BLOCKED, "ARTIFACT-NO-KEY-COLUMN",
                   "%s carries no table with a key column %r; a declared label may match the "
                   "content or be refused, never widen it" % (path, key_column))
    if len(candidates) > 1:
        raise Halt(verdict.BLOCKED, "ARTIFACT-MULTIPLE-ITEM-TABLES",
                   "%s carries %d tables with key column %r (lines %s); picking one would be an "
                   "undeclared early exit"
                   % (path, len(candidates), key_column,
                      [regions[c]["header"] + 1 for c in candidates]))

    ri = candidates[0]
    region = regions[ri]
    cols = region["cols"]
    kcol = _key_column_index(cols, key_column)

    find = _Findings()
    items = []
    seen = {}
    for j in region["body"]:
        cells = _cells(lines[j])
        if len(cells) != len(cols):
            find.add(verdict.FAIL, "TABLE-ROW-ARITY-MISMATCH",
                     "%s:%d has %d cells against %d columns; a malformed row is refused, never "
                     "skipped -- skipping shrinks the measured population"
                     % (path, j + 1, len(cells), len(cols)))
            continue
        key = cells[kcol]
        if not key.strip():
            find.add(verdict.FAIL, "ITEM-KEY-EMPTY",
                     "%s:%d key cell is blank; an item with no identity carries no obligation"
                     % (path, j + 1))
            continue
        if key in seen:
            find.add(verdict.FAIL, "ITEM-KEY-DUPLICATE",
                     "%s:%d repeats key %r first seen at line %d; a partition needs distinct "
                     "members" % (path, j + 1, key, seen[key]))
            continue
        seen[key] = j + 1
        items.append({"key": key, "line": j + 1,
                      "cells": dict((cols[c], cells[c]) for c in range(len(cols)))})

    ledger = _build_line_ledger(lines, regions, ri, fenced)

    if not items:
        raise Halt(verdict.BLOCKED, "ARTIFACT-ITEM-POPULATION-EMPTY",
                   "%s parsed to zero items; an empty measured population is never a pass" % path)

    # ---- per-occurrence residue scan (the intra-file granularity residual) ----
    shapes = {}
    for it in items:
        pat, strong = _shape_of(it["key"])
        shapes[pat] = strong
    keyset = set(it["key"] for it in items)
    for pat in sorted(shapes):
        if not shapes[pat]:
            # A shape with no literal separator and fewer than three classes would match
            # ordinary words; scanning prose with it is vacuous, so it is not scanned.
            continue
        rx = re.compile(pat)
        for entry in ledger:
            if entry["disposition"] in ("item-table-row", "item-table-header",
                                        "item-table-separator", "blank", "code-fence"):
                continue
            line = lines[entry["line"] - 1]
            for m in rx.finditer(line):
                if m.group(0) not in keyset:
                    find.add(verdict.BLOCKED, "RESIDUE-ITEM-KEY-OUTSIDE-TABLE",
                             "%s:%d carries %r, which matches the item-key shape derived from "
                             "this artifact but is absent from the item table (disposition=%s); "
                             "an obligation that lives outside the table is derived from nothing"
                             % (path, entry["line"], m.group(0), entry["disposition"]))

    for entry in ledger:
        if entry["disposition"] != "prose":
            continue
        line = lines[entry["line"] - 1]
        if "|" in line:
            find.add(verdict.BLOCKED, "RESIDUE-PIPE-LINE-OUTSIDE-TABLE",
                     "%s:%d carries a table delimiter outside any recognised table region; a row "
                     "that fails to join a table becomes no obligation and no error"
                     % (path, entry["line"]))

    if find.any():
        raise Halt(find.worst(), "ARTIFACT-REFUSED", find.summary())

    return {"path": path, "sha256_raw": contract.sha256_bytes(path),
            "line_count": len(lines), "items": items, "ledger": ledger,
            "item_table_line": region["header"] + 1,
            "key_column": cols[kcol], "columns": cols,
            "keys": [it["key"] for it in items]}


# ------------------------------------------------------------------------- CLI boundary
def main(argv=None) -> int:
    ap = verdict.make_parser(
        name=INSTRUMENT,
        description="Parse an acceptance-table artifact; refuse residue outside the table")
    ap.add_argument("path", help="the markdown artifact to parse")
    ap.add_argument("--key-column", default="item", help="header of the key column (AC-nn keys)")
    a = ap.parse_args(argv)
    try:
        result = parse(a.path, a.key_column)
    except Halt as h:
        return verdict.emit_verdict(INSTRUMENT, h.verdict, "%s: %s" % (h.code, h.detail))
    return verdict.emit_verdict(
        INSTRUMENT, verdict.PASS,
        "%d items on key %r" % (len(result["items"]), result["key_column"]),
        evidence=["%s:%d" % (a.path, result["item_table_line"])])


if __name__ == "__main__":
    import sys
    sys.exit(main())
