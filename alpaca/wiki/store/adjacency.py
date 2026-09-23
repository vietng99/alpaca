"""As-of adjacency for the PPR arm.

Builds the transition graph from `edges` live as-of :T (four-axis, excludes quarantined via the
status filter) plus entity<->passage bridges from `node_blocks` (HippoRAG-2 mass projection).
Entity nodes keep their slug ids; passage nodes are the block_id (contains '#', disjoint from slugs).
"""
from __future__ import annotations

from .asof import AsOf, asof_where, asof_where_edges
from .db import DB


def build_ppr_edges(db: DB, asof: AsOf,
                    authorized_domains: set[str] | None = None,
                    visible_blocks: set[str] | None = None) -> list[tuple[str, str, float]]:
    """Return [(src, dst, weight)] over the as-of-active KG + node_blocks bridges."""
    edges: list[tuple[str, str, float]] = []
    clause, params = asof_where_edges(asof, "e")
    block_clause, _ = asof_where(asof, "sb")
    domain_sql = ""
    bridge_domain_sql = ""
    if authorized_domains is not None:
        domains = sorted(authorized_domains)
        marks = ",".join(f":domain_{i}" for i in range(len(domains)))
        domain_sql = f" AND sb.domain IN ({marks})"
        bridge_domain_sql = f" AND bl.domain IN ({marks})"
        params.update({f"domain_{i}": value for i, value in enumerate(domains)})
    rows = db.conn.execute(
        f"SELECT e.subj_node, e.obj_node, e.confidence, e.source_block_id FROM edges e "
        f"JOIN blocks sb ON sb.block_id=e.source_block_id "
        f"WHERE e.obj_datatype='node' AND e.obj_node IS NOT NULL "
        f"AND {clause} AND {block_clause}{domain_sql}",
        params,
    ).fetchall()
    for r in rows:
        if visible_blocks is not None and r["source_block_id"] not in visible_blocks:
            continue
        w = float(r["confidence"] or 1.0)
        edges.append((r["subj_node"], r["obj_node"], w))       # directed subj -> obj
        edges.append((r["obj_node"], r["subj_node"], w * 0.5)) # weak back-edge (undirected-ish mass)

    # entity <-> passage bridges (only for blocks that are active)
    bridge_clause, bridge_params = asof_where(asof, "bl")
    bridge_params.update({k: v for k, v in params.items() if k.startswith("domain_")})
    bridges = db.conn.execute(
        "SELECT nb.node_id AS n, nb.block_id AS b, nb.weight AS w "
        "FROM node_blocks nb JOIN blocks bl ON bl.block_id = nb.block_id "
        f"WHERE {bridge_clause}{bridge_domain_sql}",
        bridge_params,
    ).fetchall()
    for r in bridges:
        if visible_blocks is not None and r["b"] not in visible_blocks:
            continue
        w = float(r["w"] or 1.0)
        edges.append((r["n"], r["b"], w))
        edges.append((r["b"], r["n"], w))
    return edges
