"""METAMEMORY (§6) - drift sampling. A continuously-updated store cannot self-detect rot without
re-verifying stored assertions against their cited raw block: FRESH / DRIFTED / ORPHANED /
UNCHECKABLE. DRIFTED/ORPHANED become cure candidates.
"""
from __future__ import annotations

from ..clock import Clock, now_iso
from ..store.db import DB
from ..store import query as q


def sample_and_verify(db: DB, entailer, limit: int = 200, clock: Clock = now_iso,
                      autocommit: bool = True) -> dict:
    """Re-verify a deterministic sample (ORDER BY edge_id) of active edges vs their cited block."""
    rows = db.conn.execute(
        "SELECT edge_id, source_block_id, source_quote FROM edges "
        "WHERE status='active' ORDER BY edge_id LIMIT ?", (limit,)
    ).fetchall()
    counts = {"FRESH": 0, "DRIFTED": 0, "ORPHANED": 0, "UNCHECKABLE": 0}
    candidates: list[str] = []
    now = clock()
    for r in rows:
        block = q.get_block(db, r["source_block_id"])
        if block is None or block.get("status") == "superseded":
            verdict = "ORPHANED"
            candidates.append(r["edge_id"])
        elif not r["source_quote"]:
            verdict = "UNCHECKABLE"
        else:
            label, _ = entailer.entail(r["source_quote"], block["text"])
            if label == "supported":
                verdict = "FRESH"
            else:
                verdict = "DRIFTED"
                candidates.append(r["edge_id"])
        counts[verdict] += 1
        db.conn.execute(
            "INSERT INTO source_freshness(target_kind,target_id,last_checked,verdict) "
            "VALUES('edge',?,?,?) ON CONFLICT(target_kind,target_id) "
            "DO UPDATE SET last_checked=excluded.last_checked, verdict=excluded.verdict",
            (r["edge_id"], now, verdict),
        )
    if autocommit:
        db.commit()
    return {"counts": counts, "cure_candidates": candidates}
