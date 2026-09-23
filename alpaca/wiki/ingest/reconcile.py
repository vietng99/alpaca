"""RECONCILE (§4.7, R5). novel / duplicate / matches / refines / contradicts.

Deterministic where typed (single-cardinality + typed object => mechanical). A TRANSACTION
supersede (a correction) sets superseded_at on the prior; a genuine world-change instead sets
valid_until - the two are NEVER re-conflated (D-1 four axes). A re-anchored SAME-content edge has
the SAME content-derived edge_id, so it is identity-preserving and can never forge a supersede or
inflate corroboration (Op-3-Keystone companion rule).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..store.db import DB


@dataclass
class Verdict:
    verdict: str                    # novel|duplicate|matches|refines|contradicts
    prior_edge_id: Optional[str] = None
    world_change: bool = False


def _pred_row(db: DB, predicate: str) -> Optional[dict]:
    r = db.conn.execute("SELECT * FROM predicates WHERE predicate=?", (predicate,)).fetchone()
    return dict(r) if r else None


def _obj_value(edge: dict) -> str:
    return edge.get("obj_node") or edge.get("obj_literal") or ""


def classify(db: DB, cand: dict, edge_id: str) -> Verdict:
    """Classify a candidate assertion against the ACTIVE edges sharing its supersede_key."""
    # identity-preserving no-op: the exact content-derived edge already exists
    if db.conn.execute("SELECT 1 FROM edges WHERE edge_id=?", (edge_id,)).fetchone():
        return Verdict("duplicate", prior_edge_id=edge_id)

    pred = _pred_row(db, cand["predicate"])
    cardinality = (pred or {}).get("cardinality", "multi")
    cand_obj = cand.get("obj_node") or cand.get("obj_literal") or ""

    if cardinality == "single":
        key_rows = db.conn.execute(
            "SELECT * FROM edges WHERE subj_node=? AND predicate=? AND status='active' "
            "ORDER BY valid_from DESC, edge_id ASC",
            (cand["subj_node"], cand["predicate"]),
        ).fetchall()
        active = [dict(r) for r in key_rows]
        candidate_time = cand.get("valid_from") or ""
        if candidate_time:
            containing = [p for p in active
                          if (p.get("valid_from") or "") <= candidate_time
                          and (not p.get("valid_until") or candidate_time < p["valid_until"])]
            priors = ([max(containing, key=lambda p: p.get("valid_from") or "")]
                      if containing else [])
            if not cand.get("valid_until"):
                later = [p for p in active if (p.get("valid_from") or "") > candidate_time]
                if later:
                    cand["valid_until"] = min(
                        later, key=lambda p: p.get("valid_from") or "")["valid_from"]
        else:
            current = [p for p in active if not p.get("valid_until")]
            priors = ([max(current, key=lambda p: p.get("valid_from") or "")]
                      if current else [])
    else:
        key_rows = db.conn.execute(
            "SELECT * FROM edges WHERE subj_node=? AND predicate=? AND status='active' "
            "AND (obj_node=? OR obj_literal=?)",
            (cand["subj_node"], cand["predicate"], cand.get("obj_node"), cand.get("obj_literal")),
        ).fetchall()
        priors = [dict(r) for r in key_rows]
    if not priors:
        return Verdict("novel")

    for p in priors:
        if _obj_value(p) == cand_obj:
            if p["source_block_id"] == cand["source_block_id"]:
                return Verdict("duplicate", prior_edge_id=p["edge_id"])
            return Verdict("matches", prior_edge_id=p["edge_id"])   # same obj, new source

    if cardinality == "single":
        prior = priors[0]
        pv, cv = _obj_value(prior), cand_obj
        # refines: literal object made more specific (contains the prior), both stay active
        if cand["obj_datatype"] != "node" and pv and cv and (cv.startswith(pv) or pv in cv) and cv != pv:
            return Verdict("refines", prior_edge_id=prior["edge_id"])
        # world-change vs correction, decided by the DOMAIN axis (valid_from ordering)
        world = bool(cand.get("valid_from") and prior.get("valid_from")
                     and cand["valid_from"] > prior["valid_from"])
        return Verdict("contradicts", prior_edge_id=prior["edge_id"], world_change=world)

    return Verdict("novel")
