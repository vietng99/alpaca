"""operating-surface.12 - curation BY EXCEPTION (bounded exception queue).

Confident deterministic extractions write SILENTLY (already the absorb-path default). The curator's
attention is spent only on genuine exceptions, so this module escalates EXACTLY four classes and
nothing else:

    1. CONTRADICTS      - an active edge with reconcile_verdict='contradicts'
    2. LOW_CONFIDENCE   - an active edge whose confidence < threshold
    3. QUARANTINED      - an edge parked at status='quarantined'
    4. MERGE_CANDIDATE  - a merge_candidate row still 'pending'

The queue is bounded per day (overflow deferred + flagged, never dropped), reusing the os.10 budget
machinery. Curation load is bounded by the exception RATE, not by ingest volume: a clean confident
document produces ZERO human-review items.
"""
from __future__ import annotations

from typing import Optional

from ..store.db import DB
from .curation_budget import Budget, apply_budget

# an edge at or above this confidence is 'confident' and never escalates on the confidence class.
DEFAULT_CONFIDENCE_THRESHOLD = 0.6

# per-class severity for priority ordering (higher = more urgent).
CLASS_SEVERITY: dict[str, int] = {
    "CONTRADICTS": 5,
    "QUARANTINED": 4,
    "LOW_CONFIDENCE": 3,
    "MERGE_CANDIDATE": 3,
}


def _exc(kind: str, ref: str, detail: str) -> dict:
    # gap_type carries the class so the shared budget effort_weight() applies uniformly.
    return {"gap_type": kind, "ref": ref, "severity": CLASS_SEVERITY.get(kind, 3), "detail": detail}


def collect_exceptions(db: DB, threshold: float = DEFAULT_CONFIDENCE_THRESHOLD) -> list[dict]:
    """The four escalation classes, deterministically ordered by (-severity, gap_type, ref)."""
    items: list[dict] = []

    for r in db.conn.execute(
        "SELECT edge_id FROM edges WHERE status='active' AND reconcile_verdict='contradicts'"
    ).fetchall():
        items.append(_exc("CONTRADICTS", r["edge_id"], "active contradiction"))

    for r in db.conn.execute(
        "SELECT edge_id, confidence FROM edges WHERE status='active' AND confidence < ?",
        (threshold,),
    ).fetchall():
        items.append(_exc("LOW_CONFIDENCE", r["edge_id"], f"confidence {r['confidence']}"))

    for r in db.conn.execute(
        "SELECT edge_id FROM edges WHERE status='quarantined'"
    ).fetchall():
        items.append(_exc("QUARANTINED", r["edge_id"], "quarantined extraction"))

    for r in db.conn.execute(
        "SELECT cand_id, node_a, node_b FROM merge_candidate WHERE status='pending'"
    ).fetchall():
        items.append(_exc("MERGE_CANDIDATE", f"cand:{r['cand_id']}",
                          f"{r['node_a']} ~ {r['node_b']}"))

    items.sort(key=lambda it: (-int(it["severity"]), it["gap_type"], it["ref"]))
    return items


def exception_queue(db: DB, cap: int, threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
                    budget: Optional[Budget] = None) -> dict:
    """Bounded per-day exception queue. Surfaces <= cap escalations; overflow deferred + flagged.
    A clean confident corpus yields an empty queue by construction."""
    if budget is not None:
        cap = budget.max_items_per_day
    items = collect_exceptions(db, threshold=threshold)
    out = apply_budget(items, cap)
    out["exception_count"] = out["total"]
    return out
