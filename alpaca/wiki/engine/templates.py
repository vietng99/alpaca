"""The fixed success-predicate template library (§5 STEP 1 / §6 LAYER 4).

Each template is a DETERMINISTIC function the verify stage evaluates as code. The planner selects
WHICH templates apply and their params; it never authors the criterion (that removes the
"doctrine-not-code" risk). This is the mechanical wrong-by-omission guard the old system lacked.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..store.asof import AsOf
from ..store.db import DB
from ..store import query as q


@dataclass
class PredResult:
    template: str
    passed: bool
    detail: str = ""


def successor_read(db: DB, used_edge_ids: list[str], read_edge_ids: set[str],
                   asof: AsOf | None = None) -> PredResult:
    """FAIL if a cited edge was CORRECTED on the TRANSACTION axis (superseded_by set) by an unread
    successor. A domain-axis world-change (valid_until only, superseded_by NULL) is NOT a
    correction - citing an older domain window as-of that time is complete, not wrong-by-omission.
    """
    for eid in used_edge_ids:
        e = q.get_edge(db, eid)
        if not e:
            continue
        succ = e.get("superseded_by_edge_id")
        if succ and asof is not None and asof.K is not None:
            successor = q.get_edge(db, succ)
            correction_at = ((successor or {}).get("recorded_at")
                             or e.get("superseded_at"))
            if correction_at and asof.K < correction_at:
                continue
        if succ and e.get("superseded_at") and succ not in read_edge_ids:
            return PredResult("SUCCESSOR_READ", False,
                              f"cited edge {eid} was corrected by unread successor {succ}")
    return PredResult("SUCCESSOR_READ", True)


def overturner_query(db: DB, answer_node_ids: list[str], read_edge_ids: set[str],
                     asof: AsOf) -> PredResult:
    """FAIL/surface if a live 'contradicts' edge touches an answer node and was not read."""
    for e in q.overturners_for_nodes(db, answer_node_ids, asof):
        if e["edge_id"] not in read_edge_ids:
            return PredResult("OVERTURNER_QUERY", False,
                              f"unread overturner {e['edge_id']} on answer nodes")
    return PredResult("OVERTURNER_QUERY", True)


def as_of_currency(db: DB, used_edge_ids: list[str], asof: AsOf) -> PredResult:
    """Every cited edge must be live as-of T (re-checked here even though reads were filtered)."""
    for eid in used_edge_ids:
        e = q.get_edge(db, eid)
        if not e:
            continue
        domain_ok = (e["valid_from"] <= asof.T and (e["valid_until"] is None or asof.T < e["valid_until"]))
        if asof.K is None:      # default: latest-knowledge => currently believed
            txn_ok = (e["superseded_at"] is None and e["status"] == "active")
        else:                   # audit: belief as-of knowledge-time K
            txn_ok = (e["recorded_at"] <= asof.K
                      and (e["superseded_at"] is None or asof.K < e["superseded_at"])
                      and e["status"] != "retracted")
        expiry_ok = (e.get("expires_at") is None or asof.T < e["expires_at"])
        live = domain_ok and txn_ok and expiry_ok
        if not live:
            return PredResult("AS_OF_CURRENCY", False, f"cited edge {eid} not live as-of {asof.T}")
    return PredResult("AS_OF_CURRENCY", True)


def subclaim_coverage(subclaims: list[str], cited_texts: list[str],
                      entail: Callable[[str, str], tuple[str, float]]) -> PredResult:
    """Each sub-claim must be entailed by at least one cited block."""
    for sc in subclaims:
        ok = False
        for text in cited_texts:
            label, _score = entail(sc, text)
            if label == "supported":
                ok = True
                break
        if not ok:
            return PredResult("SUBCLAIM_COVERAGE", False, f"uncovered sub-claim: {sc[:60]}")
    return PredResult("SUBCLAIM_COVERAGE", True)


def exact_match_floor(final_ids: list[str], floor_ids: list[str]) -> PredResult:
    """The un-overridable floor: no guaranteed exact hit may be evicted from the final ranking."""
    missing = [b for b in floor_ids if b not in set(final_ids)]
    if missing:
        return PredResult("EXACT_MATCH_FLOOR", False, f"evicted exact hits: {missing[:3]}")
    return PredResult("EXACT_MATCH_FLOOR", True)


TEMPLATE_IDS = ["SUCCESSOR_READ", "OVERTURNER_QUERY", "AS_OF_CURRENCY",
                "SUBCLAIM_COVERAGE", "EXACT_MATCH_FLOOR"]
