"""CURE PROTOCOL (§0.1) - de-poisoning without a silent DELETE.

Detect (metamemory drift + query-time contradiction + user-spotted) -> RETRACT the poisoned
assertion by bitemporal supersede (never DELETE) -> re-extract the corrected successor from the
cited raw block where possible -> propagate (invalidate derived edges, decrement inflated
corroboration) -> record the cure as a chained ingest_event so verify_chain stays intact.
"""
from __future__ import annotations

from ..determinism import edge_id as make_edge_id
from ..ingest.encode import obj_key
from ..store.db import DB
from ..store import query as q
from ..store.write import Writer


def cure_edge(db: DB, writer: Writer, poison_edge_id: str, reason: str, retracted_by: str,
              successor_cand: dict | None = None) -> dict:
    """Retract poison; optionally insert a corrected successor re-extracted from the cited block."""
    poison = q.get_edge(db, poison_edge_id)
    if not poison:
        return {"ok": False, "reason": "edge not found"}

    successor_id = None
    if successor_cand:
        block = q.get_block(db, successor_cand.get("source_block_id") or poison["source_block_id"])
        bcid = block["block_content_id"] if block else poison["source_block_id"]
        successor_id = make_edge_id(successor_cand["subj_node"], successor_cand["predicate"],
                                    obj_key(successor_cand), bcid)
        successor_cand["edge_id"] = successor_id
        successor_cand.setdefault("reconcile_verdict", "refines")
        successor_cand["supersedes_edge_id"] = poison_edge_id
        writer.upsert_edge(successor_cand)

    writer.retract_edge(poison_edge_id, reason=reason, retracted_by=retracted_by,
                        successor_edge_id=successor_id)

    # PROPAGATE (F10): the poison's source block may have corroborated OTHER edges. Decrement each
    # such edge's corroboration_count and drop the tainted corroboration row, so retracting poison
    # deflates the confidence it inflated. (No Datalog materialization exists in the template, so the
    # "invalidate derived edges" clause is a no-op here; documented, not silently skipped.)
    poison_src = poison.get("source_block_id")
    deflated = []
    if poison_src:
        others = db.conn.execute(
            "SELECT edge_id FROM edge_corroborations WHERE source_block_id=? AND edge_id<>?",
            (poison_src, poison_edge_id),
        ).fetchall()
        for r in others:
            db.conn.execute(
                "UPDATE edges SET corroboration_count = MAX(1, corroboration_count - 1) WHERE edge_id=?",
                (r["edge_id"],),
            )
            db.conn.execute(
                "DELETE FROM edge_corroborations WHERE edge_id=? AND source_block_id=?",
                (r["edge_id"], poison_src),
            )
            deflated.append(r["edge_id"])
    return {"ok": True, "retracted": poison_edge_id, "successor": successor_id,
            "deflated": deflated}
