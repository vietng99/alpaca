"""RESOLVE (§4.5, ADR-029 set-returning cannot-merge) + advisory near-match surfacing.

Exact/declared-alias match returns a SET of node_ids: |set|==1 -> link, ==0 -> mint, >1 -> keep
distinct (the two-Dats invariant - two people, one first name, stay separate). Near matches
(same normalized surface across DIFFERENT ids, or vec cosine) are written as advisory
merge_candidate rows, SURFACED to review-by-exception, NEVER auto-bound (R5).
"""
from __future__ import annotations

from ..store.db import DB
from ..store.textnorm import norm_surface
from ..store.write import Writer


def resolve_surface(db: DB, surface: str) -> list[str]:
    """Return the SET of bound node_ids an exact/declared alias surface maps to (canonical)."""
    ns = norm_surface(surface)
    rows = db.conn.execute(
        "SELECT DISTINCT node_id FROM aliases WHERE norm_surface=? AND status='bound'", (ns,)
    ).fetchall()
    out = []
    for r in rows:
        cr = db.conn.execute(
            "SELECT canonical_node_id FROM nodes WHERE node_id=?", (r["node_id"],)
        ).fetchone()
        out.append(cr["canonical_node_id"] if cr and cr["canonical_node_id"] else r["node_id"])
    return sorted(set(out))


def ensure_node(db: DB, writer: Writer, node_id: str, display: str, block_id: str) -> str:
    """Mint-or-link the node for a wikilink target (the slug is its stable identity), attach the
    mention, and surface - never bind - any distinct node sharing this normalized surface."""
    existing = db.conn.execute("SELECT node_id, canonical_node_id FROM nodes WHERE node_id=?",
                               (node_id,)).fetchone()
    if existing is None:
        writer.upsert_node(node_id, display_name=display)
        writer.add_alias(node_id, display, norm_surface(display), kind="wikilink",
                         status="bound", source_block_id=block_id)
        canonical = node_id
    else:
        canonical = existing["canonical_node_id"] or existing["node_id"]
    writer.add_node_block(canonical, block_id, role="mention")

    # advisory near-match: a DIFFERENT existing node with the same normalized surface
    ns = norm_surface(display)
    dupes = db.conn.execute(
        "SELECT DISTINCT a.node_id FROM aliases a WHERE a.norm_surface=? AND a.node_id<>?",
        (ns, canonical),
    ).fetchall()
    for d in dupes:
        _surface_once(db, writer, canonical, d["node_id"])
    return canonical


def _surface_once(db: DB, writer: Writer, a: str, b: str) -> None:
    lo, hi = sorted((a, b))
    exists = db.conn.execute(
        "SELECT 1 FROM merge_candidate WHERE node_a=? AND node_b=?", (lo, hi)
    ).fetchone()
    if not exists:
        writer.surface_merge_candidate(lo, hi, method="norm-surface", score=1.0)
