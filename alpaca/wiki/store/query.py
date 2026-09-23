"""Un-guarded by-id / by-node lookups (NOT retrieval ranking).

Distinct from `store.read` (the single guarded RETRIEVAL door). These are plain row fetches used
by verification (oracle re-reading a cited block), the projection renderer, metamemory, and the
CLI. They rank nothing and open no second retrieval path, so they are not R1-guarded.
"""
from __future__ import annotations

from typing import Optional

from .asof import AsOf, asof_where_edges
from .db import DB


class CanonicalCycleError(RuntimeError):
    """Merge redirects contain a cycle or exceed the bounded chain depth."""


def get_block(db: DB, block_id: str) -> Optional[dict]:
    r = db.conn.execute("SELECT * FROM blocks WHERE block_id=?", (block_id,)).fetchone()
    return dict(r) if r else None


def get_block_by_content_id(db: DB, block_content_id: str) -> Optional[dict]:
    r = db.conn.execute(
        "SELECT * FROM blocks WHERE block_content_id=? ORDER BY block_id LIMIT 1", (block_content_id,)
    ).fetchone()
    return dict(r) if r else None


def get_node(db: DB, node_id: str) -> Optional[dict]:
    r = db.conn.execute("SELECT * FROM nodes WHERE node_id=?", (node_id,)).fetchone()
    return dict(r) if r else None


def resolve_canonical(db: DB, node_id: str) -> str:
    """Follow bounded merge redirects to one canonical node and reject cycles."""
    current = node_id
    seen: set[str] = set()
    for _depth in range(64):
        if current in seen:
            raise CanonicalCycleError(f"canonical merge cycle at {current}")
        seen.add(current)
        row = db.conn.execute(
            "SELECT canonical_node_id FROM nodes WHERE node_id=?", (current,)
        ).fetchone()
        if not row or not row["canonical_node_id"]:
            return current
        current = row["canonical_node_id"]
    raise CanonicalCycleError(f"canonical merge chain exceeds 64 hops from {node_id}")


def get_edge(db: DB, edge_id: str) -> Optional[dict]:
    r = db.conn.execute("SELECT * FROM edges WHERE edge_id=?", (edge_id,)).fetchone()
    return dict(r) if r else None


def active_edges_for_node(db: DB, node_id: str, asof: AsOf) -> list[dict]:
    """Edges (subj or obj = node) live as-of T, four-axis filtered + km.6 shelf-life excluded."""
    clause, params = asof_where_edges(asof, "edges")
    params = dict(params, n=node_id)
    rows = db.conn.execute(
        f"SELECT * FROM edges WHERE (subj_node=:n OR obj_node=:n) AND {clause}", params
    ).fetchall()
    return [dict(r) for r in rows]


def successors_of(db: DB, edge_id: str) -> list[dict]:
    """Any edge that supersedes this one (SUCCESSOR_READ evidence)."""
    rows = db.conn.execute(
        "SELECT * FROM edges WHERE supersedes_edge_id=?", (edge_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def overturners_for_nodes(db: DB, node_ids: list[str], asof: AsOf) -> list[dict]:
    """Live 'contradicts' edges touching any of the answer's nodes (OVERTURNER_QUERY evidence)."""
    if not node_ids:
        return []
    clause, params = asof_where_edges(asof, "edges")
    marks = ",".join(f":n{i}" for i in range(len(node_ids)))
    for i, n in enumerate(node_ids):
        params[f"n{i}"] = n
    rows = db.conn.execute(
        f"SELECT * FROM edges WHERE reconcile_verdict='contradicts' "
        f"AND (subj_node IN ({marks}) OR obj_node IN ({marks})) AND {clause}",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def edges_for_blocks(db: DB, block_ids: list[str], asof: AsOf) -> list[dict]:
    """Active-as-of edges anchored to any of these cited blocks (source_block_id)."""
    if not block_ids:
        return []
    clause, params = asof_where_edges(asof, "edges")
    marks = ",".join(f":b{i}" for i in range(len(block_ids)))
    for i, b in enumerate(block_ids):
        params[f"b{i}"] = b
    rows = db.conn.execute(
        f"SELECT * FROM edges WHERE source_block_id IN ({marks}) AND {clause}", params
    ).fetchall()
    return [dict(r) for r in rows]


def all_events(db: DB) -> list[dict]:
    rows = db.conn.execute(
        "SELECT seq,prev_checksum,checksum,ts,doc_id,op,payload FROM ingest_event ORDER BY seq"
    ).fetchall()
    import json
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"]) if d["payload"] else {}
        out.append(d)
    return out


def doc_sha(db: DB, doc_id: str) -> Optional[str]:
    r = db.conn.execute("SELECT content_sha256 FROM docs WHERE doc_id=?", (doc_id,)).fetchone()
    return r["content_sha256"] if r else None
