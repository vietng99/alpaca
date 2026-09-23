"""operating-surface.10 - bounded curation-load budget.

All gaps are always DETECTED (os.8). The scarce resource is the human curator's attention, so ONLY
the review queue is capped: at most `budget.max_items_per_day` items are surfaced per day, ordered
by a fixed integer priority key; overflow is DEFERRED and flagged (deferred_count), never dropped -
conservation is a property, not a hope. `curation_load` is the total fixed integer effort over all
open gaps; OVER_BUDGET flags when a single day's newly-opened inflow already exceeds the cap (an
ingestion-quality signal, not curator slowness).

The ordering + budget machinery here is reused by os.12 (curation by exception).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..store.db import DB

# fixed integer minutes-to-curate per gap type (effort_weight). Data - never a float.
EFFORT_WEIGHT: dict[str, int] = {
    "OPEN_CONTRADICTION": 15,
    "STALE_HOT": 10,
    "UNANSWERED_QUERY": 8,
    "MISSING_INVERSE": 6,
    "RED_LINK": 5,
    "STUB": 4,
    "ORPHAN_NODE": 3,
    # exception classes (os.12) reuse the same table
    "CONTRADICTS": 15,
    "LOW_CONFIDENCE": 8,
    "QUARANTINED": 12,
    "MERGE_CANDIDATE": 10,
}
DEFAULT_EFFORT = 5


@dataclass(frozen=True)
class Budget:
    max_items_per_day: int
    minutes_target: int = 60


def effort_weight(gap_type: str) -> int:
    return EFFORT_WEIGHT.get(gap_type, DEFAULT_EFFORT)


def apply_budget(items: list[dict], max_items: int) -> dict:
    """Shared cap machinery (reused by os.12). `items` MUST already be priority-ordered.

    Surfaces the first `max_items`; every remaining item is DEFERRED, never dropped, so
    len(surfaced)+len(deferred) == len(items) is an invariant the caller can assert."""
    cap = max(0, int(max_items))
    surfaced = items[:cap]
    deferred = items[cap:]                      # conservation: overflow parked, not discarded
    est = sum(effort_weight(it["gap_type"]) for it in surfaced)
    return {
        "surfaced": surfaced,
        "deferred": deferred,
        "deferred_count": len(deferred),
        "surfaced_count": len(surfaced),
        "total": len(items),
        "est_minutes": est,
    }


def _tier_of(db: DB, subject_key: str) -> int:
    """PageRank tier band of the gap's subject node (higher band = more central). A non-node
    subject or an un-banded node contributes 0 so it sorts last on this term."""
    row = db.conn.execute(
        "SELECT page_band FROM nodes WHERE node_id=?", (subject_key,)
    ).fetchone()
    if row and row["page_band"] is not None:
        return int(row["page_band"])
    return 0


def _priority_key(db: DB, g: dict):
    # total priority key (research brief 3): (-severity, -pagerank_tier_of_subject, first_seen_seq, gap_id)
    return (
        -int(g["severity"]),
        -_tier_of(db, g.get("subject_key") or ""),
        int(g.get("first_seen_seq") or 0),
        g["gap_id"],
    )


def _open_gaps(db: DB) -> list[dict]:
    rows = db.conn.execute(
        "SELECT gap_id, gap_type, subject_key, severity, distinct_referrers, first_seen_seq "
        "FROM gaps WHERE status='open'"
    ).fetchall()
    return [dict(r) for r in rows]


def curation_load(db: DB) -> int:
    """Total fixed integer effort over ALL open gaps (the standing backlog cost)."""
    return sum(effort_weight(g["gap_type"]) for g in _open_gaps(db))


def curation_queue(db: DB, budget: Budget, inflow: Optional[int] = None) -> dict:
    """Reconcile every OPEN gap into ONE priority-ordered review queue, capped at the daily budget.

    Returns surfaced (<= cap) + deferred (the rest, conserved) + load/effort accounting and an
    OVER_BUDGET flag. `inflow` (newly-opened-today count) defaults to the number of open gaps that
    share the newest first_seen_seq; OVER_BUDGET fires when that inflow already exceeds the cap."""
    gaps = _open_gaps(db)
    gaps.sort(key=lambda g: _priority_key(db, g))
    out = apply_budget(gaps, budget.max_items_per_day)
    out["curation_load"] = sum(effort_weight(g["gap_type"]) for g in gaps)
    if inflow is None:
        seqs = [int(g.get("first_seen_seq") or 0) for g in gaps]
        newest = max(seqs) if seqs else 0
        inflow = sum(1 for s in seqs if s == newest)
    out["inflow"] = inflow
    out["over_budget"] = inflow > budget.max_items_per_day
    out["minutes_target"] = budget.minutes_target
    out["within_minutes_target"] = out["est_minutes"] <= budget.minutes_target
    return out
