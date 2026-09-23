"""Shelf-life / expiry (knowledge-model.6).

Parses `expires: YYYY-MM-DD` + `volatile: true` from a doc's frontmatter and stamps the derived
edges' `expires_at` / `volatile` columns, then provides the query-time as-of/expiry filter that
keeps an expired volatile claim from EVER being served bare as current. A row past its expiry is
partitioned to the STALE side ('stale - re-verify'), never dropped into the fresh set; the oracle
must abstain or flag it.

Pure stdlib. `stamp_edges` writes through an open transaction the caller owns (or commits itself
when `autocommit=True`). The filter helpers are pure functions over edge dicts + an as-of instant.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

_FM_FENCE = re.compile(r"^\s*---\s*$")
_EXPIRES = re.compile(r"^\s*expires\s*:\s*(\d{4}-\d{2}-\d{2})\s*$", re.IGNORECASE)
_VOLATILE = re.compile(r"^\s*volatile\s*:\s*(true|false|1|0|yes|no)\s*$", re.IGNORECASE)
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_TRUE = {"true", "1", "yes"}


def _to_instant(s: Optional[str]) -> Optional[str]:
    """Normalize a date-only shelf-life stamp to a full ISO instant so lexicographic compare with
    an as-of instant is well-defined. Already-full ISO strings pass through unchanged."""
    if not s:
        return None
    s = s.strip()
    if _DATE_ONLY.match(s):
        return s + "T00:00:00+00:00"
    return s


def parse_shelf_life(text: str) -> dict:
    """Parse a leading `--- ... ---` frontmatter block for `expires:` and `volatile:`.

    Returns {'expires_at': <iso|None>, 'volatile': 0|1}. Absent frontmatter => defaults.
    """
    expires_at: Optional[str] = None
    volatile = 0

    lines = text.split("\n")
    # frontmatter must be the very first non-empty content
    i = 0
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    if i >= len(lines) or not _FM_FENCE.match(lines[i]):
        return {"expires_at": None, "volatile": 0}
    i += 1
    while i < len(lines) and not _FM_FENCE.match(lines[i]):
        m = _EXPIRES.match(lines[i])
        if m:
            expires_at = _to_instant(m.group(1))
        m = _VOLATILE.match(lines[i])
        if m:
            volatile = 1 if m.group(1).lower() in _TRUE else 0
        i += 1
    return {"expires_at": expires_at, "volatile": volatile}


def stamp_edges(db: Any, doc_id: str, expires_at: Optional[str] = None,
                volatile: int = 0, autocommit: bool = False) -> int:
    """Propagate a doc's shelf-life onto the edges anchored to its blocks. Returns rows stamped.

    Only sets a column when the parsed value is meaningful (an expiry, or a volatile flag), so a
    plain re-stamp never clears an existing expiry. UPDATE of these bitemporal-metadata columns is
    permitted by the schema (only DELETE is trigger-forbidden)."""
    exp = _to_instant(expires_at)
    edge_ids = [
        r["edge_id"] for r in db.conn.execute(
            "SELECT e.edge_id FROM edges e JOIN blocks b ON e.source_block_id = b.block_id "
            "WHERE b.doc_id = ? AND b.status='active'", (doc_id,),
        ).fetchall()
    ]
    n = 0
    for eid in edge_ids:
        db.conn.execute(
            "UPDATE edges SET expires_at = ?, volatile = ? WHERE edge_id = ?",
            (exp, 1 if volatile else 0, eid),
        )
        n += 1
    if autocommit:
        db.commit()
    return n


# ---------------------------------------------------------------- as-of / expiry filter helpers

def is_expired(edge: dict, at_iso: str) -> bool:
    """True iff this edge carries an expiry that is at or before the as-of instant T."""
    exp = _to_instant(edge.get("expires_at"))
    if not exp:
        return False
    return exp <= _to_instant(at_iso)


def is_volatile(edge: dict) -> bool:
    return bool(edge.get("volatile"))


def expiry_partition(edges: Iterable[dict], at_iso: str) -> tuple[list[dict], list[dict]]:
    """Split edges into (fresh, stale) as-of T. An expired row is STALE ('re-verify'), never fresh.

    This is the gate the oracle consults: a stale row must not be served bare as current - the
    engine abstains or attaches a 'stale - re-verify' flag."""
    fresh: list[dict] = []
    stale: list[dict] = []
    for e in edges:
        if is_expired(e, at_iso):
            stale.append(e)
        else:
            fresh.append(e)
    return fresh, stale


def expiry_clause(alias: str = "edges", param: str = "T") -> str:
    """SQL fragment excluding expired rows as-of :T. Append to an edge SELECT's WHERE."""
    return f"({alias}.expires_at IS NULL OR {alias}.expires_at > :{param})"


def count_expired(db: Any, at_iso: str) -> int:
    """temporal_health primitive: how many active edges are past their expiry as-of T."""
    at = _to_instant(at_iso)
    row = db.conn.execute(
        "SELECT COUNT(*) AS n FROM edges "
        "WHERE expires_at IS NOT NULL AND expires_at <= ? AND status = 'active'",
        (at,),
    ).fetchone()
    return int(row["n"]) if row and row["n"] is not None else 0
