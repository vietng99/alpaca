"""THE single guarded RETRIEVAL surface (R1 one-door).

ONLY `engine/retrieve.py` may import this module - asserted by tests/test_r1_import_guard.py,
which greps the whole package's import graph. Any other importer is a second retrieval door and
fails the build. These are the ranking/seed primitives; plain by-id lookups live in `store.query`
(not guarded, opens no retrieval path).
"""
from __future__ import annotations

from ..api import Filters
from .adjacency import build_ppr_edges, count_ppr_edges
from .asof import AsOf, block_where
from .db import DB
from .filters import block_clause
from .fts import search_bm25
from .textnorm import norm_surface, surface_in_question
from .vec import knn_blocks, knn_nodes


# ---- the four arms' data access -------------------------------------------

def bm25(db: DB, question: str, limit: int = 50, asof: AsOf | None = None,
         filters: Filters | None = None) -> list[tuple[str, float]]:
    return search_bm25(db, question, limit, asof, block_clause(filters, "b", db))


def vector(db: DB, qvec: list[float], limit: int = 50) -> list[tuple[str, float]]:
    return knn_blocks(db, qvec, limit)


def exact_floor(db: DB, question: str, linked_nodes: list[str],
                asof: AsOf | None = None, filters: Filters | None = None,
                entity_cap: int | None = None, lexical_rank: dict[str, int] | None = None) -> list[str]:
    """The PERMANENT, un-overridable exact-match floor: guaranteed hits PPR/cosine may reorder
    but can NEVER evict. Sources: (a) blocks defining/claiming a linked entity, (b) exact
    case-insensitive phrase hits on the raw question. Blocks are read under the as-of clause
    (architecture.md 5.2); `status='active'` when no as-of is given."""
    floor: list[str] = []
    if linked_nodes:
        names = {f"n{i}": node for i, node in enumerate(linked_nodes)}
        marks = ",".join(":" + name for name in names)
        visible, params = block_where(asof, "bl")
        narrow, narrow_params = block_clause(filters, "bl", db)
        rows = db.conn.execute(
            f"SELECT DISTINCT nb.block_id AS b FROM node_blocks nb "
            f"JOIN blocks bl ON bl.block_id=nb.block_id "
            f"WHERE nb.node_id IN ({marks}) AND nb.role IN ('definition','claim') "
            f"AND {visible} AND {narrow} ORDER BY b",
            {**names, **params, **narrow_params},
        ).fetchall()
        members = [r["b"] for r in rows]
        if entity_cap is not None and len(members) > entity_cap:
            # An identifier that many notes name (a task has a dozen) would fill the whole top of the answer
            # with its own header blocks and push out the notes the rest of the question is about. The floor
            # keeps the entity-claim blocks the question's words reached first (their BM25 rank), then the
            # rest by block id, up to the cap; the others stay ordinary candidates.
            rank = lexical_rank or {}
            members = sorted(members, key=lambda b: (rank.get(b, len(rank) + 1), b))[:entity_cap]
        floor.extend(members)
    phrase = question.strip().lower()
    if len(phrase) >= 4:
        visible, params = block_where(asof, "blocks")
        narrow, narrow_params = block_clause(filters, "blocks", db)
        rows = db.conn.execute(
            f"SELECT block_id FROM blocks WHERE {visible} AND {narrow} "
            "AND instr(lower(text), :phrase)>0 ORDER BY block_id LIMIT 20",
            {**params, **narrow_params, "phrase": phrase},
        ).fetchall()
        floor.extend(r["block_id"] for r in rows)
    # stable de-dup
    seen, out = set(), []
    for b in floor:
        if b not in seen:
            seen.add(b); out.append(b)
    return out


def link_entities(db: DB, question: str, qnode_vec: list[float] | None = None) -> list[str]:
    """Query-entity linking: exact alias match + optional vec_nodes cosine. Returns canonical nodes."""
    linked: list[str] = []
    nq = norm_surface(question)
    rows = db.conn.execute(
        "SELECT node_id, norm_surface, kind FROM aliases WHERE status='bound'").fetchall()
    for r in rows:
        if surface_in_question(r["norm_surface"], nq, r["kind"]):
            linked.append(r["node_id"])
    if qnode_vec:
        for nid, cos in knn_nodes(db, qnode_vec, limit=10):
            if cos >= 0.5:
                linked.append(nid)
    # resolve canonical + stable de-dup
    seen, out = set(), []
    for nid in linked:
        cr = db.conn.execute("SELECT canonical_node_id FROM nodes WHERE node_id=?", (nid,)).fetchone()
        c = (cr["canonical_node_id"] if cr and cr["canonical_node_id"] else nid)
        if c not in seen:
            seen.add(c); out.append(c)
    return out


def ppr_seed(db: DB, bm25_hits: list[tuple[str, float]], vec_hits: list[tuple[str, float]],
             linked_nodes: list[str]) -> dict[str, float]:
    """Restart vector s: BM25/vector blocks -> their mention nodes + the blocks themselves, plus
    entity-linked nodes. Each weighted by its normalized fusion score. L1-normalized in ppr()."""
    seed: dict[str, float] = {}
    block_ids = [b for b, _ in bm25_hits] + [b for b, _ in vec_hits]
    for bid, sc in bm25_hits + vec_hits:
        seed[bid] = seed.get(bid, 0.0) + max(0.0, float(sc))
    if block_ids:
        marks = ",".join("?" * len(block_ids))
        rows = db.conn.execute(
            f"SELECT node_id, block_id, weight FROM node_blocks WHERE block_id IN ({marks})",
            tuple(block_ids),
        ).fetchall()
        for r in rows:
            seed[r["node_id"]] = seed.get(r["node_id"], 0.0) + 0.5 * float(r["weight"] or 1.0)
    for nid in linked_nodes:
        seed[nid] = seed.get(nid, 0.0) + 1.0
    return seed


def ppr_edges(db: DB, asof: AsOf,
              authorized_domains: set[str] | None = None,
              visible_blocks: set[str] | None = None) -> list[tuple[str, str, float]]:
    return build_ppr_edges(
        db, asof, authorized_domains=authorized_domains, visible_blocks=visible_blocks,
    )


def ppr_edge_count(db: DB, asof: AsOf,
                   authorized_domains: set[str] | None = None,
                   visible_blocks: set[str] | None = None) -> int:
    """The demand-gate input (architecture.md 8.2): the as-of active edges `ppr_edges` would read."""
    return count_ppr_edges(
        db, asof, authorized_domains=authorized_domains, visible_blocks=visible_blocks,
    )


def asof_visible_blocks(db: DB, asof: AsOf, filters: Filters | None = None) -> set[str]:
    """Block-level as-of currency guard (F4): the set of block_ids whose bitemporal window contains
    T under the same frozen four-axis predicate used for edges. Retrieval intersects every arm with
    this set so a past-as-of read never surfaces a future/non-current block. The FR-11 filters
    (store/filters.py) narrow it in the same query, so every arm reads only blocks inside them."""
    narrow, narrow_params = block_clause(filters, "blocks", db)
    rows = db.conn.execute(f"SELECT block_id FROM blocks WHERE {asof.clause('blocks')} AND {narrow}",
                           {**asof.params(), **narrow_params}).fetchall()
    return {r["block_id"] for r in rows}


def block_rows(db: DB, block_ids: list[str], asof: AsOf | None = None) -> dict[str, dict]:
    if not block_ids:
        return {}
    names = {f"i{i}": block_id for i, block_id in enumerate(block_ids)}
    marks = ",".join(":" + name for name in names)
    visible, params = block_where(asof, "b")
    rows = db.conn.execute(
        f"SELECT block_id, text, context_header, page_bandish, valid_from FROM ("
        f"  SELECT b.block_id, b.text, b.context_header, b.valid_from, "
        f"         COALESCE((SELECT MAX(n.page_band) FROM node_blocks nb "
        f"                   JOIN nodes n ON n.node_id=nb.node_id WHERE nb.block_id=b.block_id),0) AS page_bandish "
        f"  FROM blocks b WHERE b.block_id IN ({marks}) AND {visible})",
        {**names, **params},
    ).fetchall()
    return {r["block_id"]: dict(r) for r in rows}


def node_blocks_for(db: DB, block_ids: list[str]) -> dict[str, list[str]]:
    """block_id -> [node_id] via node_blocks, for passage-score entity aggregation."""
    if not block_ids:
        return {}
    marks = ",".join("?" * len(block_ids))
    rows = db.conn.execute(
        f"SELECT block_id, node_id FROM node_blocks WHERE block_id IN ({marks})", tuple(block_ids)
    ).fetchall()
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["block_id"], []).append(r["node_id"])
    return out
