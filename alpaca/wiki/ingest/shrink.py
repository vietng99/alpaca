"""Body-shrink sanity gate (provenance-capture.5).

On re-ingest of an EXISTING doc_id a sudden collapse in the number of claims it supports is almost
always a truncated paste / botched edit, not a genuine retraction. This module MEASURES the shrink
(claims_dropped) and, when it breaches the owner's floor, raises `BodyShrinkRejected` so the
absorb transaction rolls back atomically - UNLESS the curator supplies an explicit `waive_reason`
(recorded on the ingest_event payload by the caller). Dropped claims must leave via
supersede/tombstone (km.1), never a silent body-shrink.

Pure stdlib. The gate is a pure measurement over the current DB state + the incoming candidate
count; the caller (absorb) owns the transaction and the waive_reason ledger stamp.
"""
from __future__ import annotations

from typing import Any, Optional

SHRINK_FLOOR_DEFAULT = 0.70  # owner knob: new body must retain >= 70% of prior claims


class BodyShrinkRejected(Exception):
    """Raised when a re-ingest drops too many claims and no waive_reason was supplied."""

    def __init__(self, doc_id: str, n_before: int, n_after: int, floor: float,
                 claims_dropped: int) -> None:
        self.doc_id = doc_id
        self.n_before = n_before
        self.n_after = n_after
        self.floor = floor
        self.claims_dropped = claims_dropped
        super().__init__(
            f"body-shrink rejected for {doc_id!r}: {n_after} new atoms < "
            f"{floor:.2f} * {n_before} prior active claims "
            f"(claims_dropped={claims_dropped}); supply waive_reason to override"
        )


def shrink_floor_of(cfg: Any) -> float:
    """Resolve the shrink floor from cfg.meta (owner knob), falling back to the default 0.70."""
    try:
        v = cfg.meta.get("shrink_floor")
    except Exception:
        v = None
    return float(v) if v is not None else SHRINK_FLOOR_DEFAULT


def count_active_claims(db: Any, doc_id: str) -> int:
    """N_before: distinct ACTIVE, non-superseded edges anchored to this doc's blocks."""
    row = db.conn.execute(
        "SELECT COUNT(DISTINCT e.edge_id) AS n "
        "FROM edges e JOIN blocks b ON e.source_block_id = b.block_id "
        "WHERE b.doc_id = ? AND e.status = 'active' AND e.superseded_at IS NULL",
        (doc_id,),
    ).fetchone()
    return int(row["n"]) if row and row["n"] is not None else 0


def assess_shrink(db: Any, doc_id: str, new_atom_count: int, *,
                  new_text_len: Optional[int] = None, old_text_len: Optional[int] = None,
                  shrink_floor: float = SHRINK_FLOOR_DEFAULT,
                  waive_reason: Optional[str] = None) -> dict:
    """Measure the body-shrink of a re-ingest and enforce the floor.

    N_before = distinct active claims currently anchored to the doc.
    N_after  = new candidate atom count for the incoming body.
    The gate trips when N_after < floor*N_before OR new_text_len < floor*old_text_len.
    A tripped gate with NO waive_reason raises BodyShrinkRejected (caller must roll back).
    A tripped gate WITH a waive_reason returns a `waived=True` report carrying the reason so the
    caller can stamp it on the ingest_event payload. Returns the measurement report otherwise.
    """
    n_before = count_active_claims(db, doc_id)
    n_after = int(new_atom_count)
    claims_dropped = max(0, n_before - n_after)

    atom_shrink = n_before > 0 and n_after < shrink_floor * n_before
    text_shrink = (
        old_text_len is not None and new_text_len is not None
        and old_text_len > 0 and new_text_len < shrink_floor * old_text_len
    )
    tripped = atom_shrink or text_shrink

    report = {
        "doc_id": doc_id,
        "n_before": n_before,
        "n_after": n_after,
        "floor": shrink_floor,
        "claims_dropped": claims_dropped,
        "atom_shrink": atom_shrink,
        "text_shrink": text_shrink,
        "tripped": tripped,
        "waived": False,
        "waive_reason": None,
    }

    if tripped and waive_reason:
        report["waived"] = True
        report["waive_reason"] = waive_reason
        return report
    if tripped:
        raise BodyShrinkRejected(doc_id, n_before, n_after, shrink_floor, claims_dropped)
    return report
