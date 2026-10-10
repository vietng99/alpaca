"""The one place a query outcome is decided (architecture.md 6.1; interfaces.py.txt section 4).

The answer door keeps its three verdicts (types.py Answer.verdict). `classify_outcome` adds the
caller-facing `outcome` and `reason`, first match wins. `precheck` is the pre-door check: it reads the
filters and the store and scope state. The two time pins come from engine/plan.classify (see
engine/loop.question_pins), so a date in the question text counts like the argument; the door reads
them (architecture.md 5.1, 5.2).
Pure functions: no store, no record, no provider, no network.
"""
from __future__ import annotations

from typing import Sequence

from .api import Filters, Outcome

FILTER_INVALID = "filter-invalid"
VALID_TIME_UNKNOWN = "valid-time-unknown"
STORE_LOCKED = "store-locked"
SCOPED_SOURCE_MISSING = "scoped-source-missing"
#: The record holds a content-removing act (a withdrawal, a retraction, an applied correction) that
#: the store has not processed: a crash between the record append and the store commit, or a
#: rebuild or import cut short (t-070 round 2 C-E). The store is not served until `wiki recover`.
STORE_BEHIND_RECORD = "store-behind-record"


def precheck(*, filters: Filters | None = None, store_locked: bool = False,
             missing_transcripts_in_scope: Sequence[str] = (),
             store_behind_record: bool = False) -> tuple[str | None, str | None]:
    """Return (precheck, reason) for the pre-door outcomes of architecture.md 6.1 rows 1 and 2.

    A knowledge-time pin (the argument, or "as known at <date>" in the question) and the structured
    filters are read by the door since t-065 (architecture.md 5.2; FR-11), so neither is refused
    here; a filter value that cannot be applied is `unsupported` with its reason. A store behind
    the record's removal acts (`store_behind_record`) is `unavailable`, never served.
    """
    if filters is not None and filters.invalid_reason():
        return "unsupported", FILTER_INVALID
    if store_locked:
        return "unavailable", STORE_LOCKED
    if store_behind_record:
        return "unavailable", STORE_BEHIND_RECORD
    if missing_transcripts_in_scope:
        return "unavailable", SCOPED_SOURCE_MISSING
    return None, None


def classify_outcome(*, precheck: str | None, precheck_reason: str | None,
                     door_verdict: str | None, served_ids: Sequence[str],
                     excluded_count: int, retrieved_count: int, cited_current: bool,
                     stale_only: bool, scope_gaps: bool,
                     valid_time_unknown: bool) -> tuple[Outcome, str | None]:
    """architecture.md 6.1, the only place outcomes are decided. valid_time_unknown: T explicit, at
    least one item cited (on abstention, served), and every such item has a null source time.
    excluded_count: how many retrieved matches the servable filter removed. The interface sketch
    passes their ids (interfaces.py.txt `excluded_ids`); only whether any were removed matters
    here, and the ids name private documents, so the door hands over the count."""
    if precheck == "unsupported":
        return Outcome.UNSUPPORTED, precheck_reason
    if valid_time_unknown:
        return Outcome.UNSUPPORTED, VALID_TIME_UNKNOWN
    if precheck == "unavailable":
        return Outcome.UNAVAILABLE, precheck_reason
    if excluded_count and not served_ids:
        return Outcome.REFUSED, "not-servable"
    if stale_only:
        return Outcome.STALE, "anchor-unresolved"
    if door_verdict == "conflict":
        return Outcome.CONFLICT, None
    if door_verdict == "grounded":
        return (Outcome.GROUNDED, None) if cited_current else (Outcome.HISTORICAL, "not-current")
    if scope_gaps:
        return Outcome.PARTIAL, "scope-capture-gaps"
    if retrieved_count == 0:
        return Outcome.EMPTY, None
    return Outcome.ABSTAINED, None


__all__ = ["classify_outcome", "precheck", "FILTER_INVALID",
           "VALID_TIME_UNKNOWN", "STORE_LOCKED", "SCOPED_SOURCE_MISSING", "STORE_BEHIND_RECORD"]
