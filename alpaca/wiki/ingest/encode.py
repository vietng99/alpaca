"""The agentic ENCODE loop (§4.5): plan -> traverse -> resolve -> reconcile -> place -> record.

The write-side counterpart of the DECODE loop. It does NOT dump triples: it researches the
existing graph and integrates. Deterministic-first; the LLM is only a quarantined fallback. A
confident, cleanly-resolved, non-contradicting assertion is written SILENTLY; anything the
reconcile step cannot arbitrate is quarantined + surfaced (never silently dropped).
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from ..determinism import edge_id as make_edge_id, nfc, normalize_text
from ..store.db import DB
from ..store.write import Writer
from . import reconcile as rec
from .resolve import resolve_surface


def obj_key(cand: dict) -> str:
    if cand["obj_datatype"] == "node":
        return f"node:{nfc(cand.get('obj_node') or '')}"
    return f"lit:{cand['obj_datatype']}:{nfc(cand.get('obj_literal') or '')}"


# ===================================================== provenance-capture.3 typed atom split
_STANCE = re.compile(
    r"\b(think|thinks|believe|believes|opinion|should|prefer|feel|feels|argue|argues|"
    r"recommend|nghĩ|theo tôi|nên|cho rằng)\b", re.IGNORECASE)
_ENTITY_PREDS = frozenset({"is_a", "instance_of", "type_of", "aka", "alias", "same_as", "known_as"})


def classify_atom(cand: dict) -> str:
    """Deterministic FACT | TAKE | ENTITY | FULLTEXT routing (pc.3). Unambiguous signals only -
    an unclassifiable candidate defaults to FACT (its substrate CHECKs still guard it)."""
    pred = (cand.get("predicate") or "").lower()
    dt = cand.get("obj_datatype")
    if dt == "fulltext" or pred == "fulltext":
        return "FULLTEXT"
    if _STANCE.search(cand.get("source_quote") or "") or pred in ("thinks", "believes", "stance"):
        return "TAKE"
    if pred in _ENTITY_PREDS:
        return "ENTITY"
    return "FACT"


def entity_is_ambiguous(db: DB, cand: dict) -> bool:
    """pc.3 ENTITY resolver gate: an ENTITY atom whose bare surface resolves to MORE THAN ONE bound
    node is ambiguous and MUST be quarantined, never auto-bound to one (the two-Dat invariant)."""
    return classify_atom(cand) == "ENTITY" and len(resolve_surface(db, cand["subj_node"])) > 1


# ===================================================== provenance-capture.7 span-grounding + time
def span_grounded(source_quote: str, block_text: str) -> bool:
    """The asserted quote must be an NFC-normalized substring of its cited block (pc.7)."""
    if not source_quote:
        return False
    return normalize_text(source_quote) in normalize_text(block_text)


_REL_DAYS = {
    "last week": 7, "tuần trước": 7, "a week ago": 7,
    "last month": 30, "tháng trước": 30, "a month ago": 30,
    "last year": 365, "năm ngoái": 365, "năm trước": 365, "a year ago": 365,
    "yesterday": 1, "hôm qua": 1,
}
_REL_ZERO = ("now", "today", "currently", "recently", "lately", "hiện nay", "bây giờ", "hôm nay")


def _parse_date(iso: str) -> date:
    return date.fromisoformat((iso or "1970-01-01")[:10])


def resolve_relative_time(phrase: str, source_created_at: str) -> str:
    """Resolve a relative-time phrase AGAINST the source's own authored date - NEVER wall-clock (pc.7).

    'recently' in a 2020-01-01 source resolves to 2020-01-01, not the ingest day. This is the only
    correct anchor for a document that talks about ITS OWN present."""
    base = _parse_date(source_created_at)          # the source's date is the ONLY clock here
    p = (phrase or "").strip().lower()
    for key, days in _REL_DAYS.items():
        if key in p:
            return (base - timedelta(days=days)).isoformat()
    if any(z in p for z in _REL_ZERO):
        return base.isoformat()
    return base.isoformat()


def _has_relative_time(phrase: str) -> bool:
    """True iff the phrase carries a recognised relative-time cue (pc.7). Gates the learned_at
    resolution so only claims that actually SAY 'recently'/'last week'/… get re-anchored."""
    p = (phrase or "").strip().lower()
    return any(k in p for k in _REL_DAYS) or any(z in p for z in _REL_ZERO)


def encode_candidate(db: DB, writer: Writer, cand: dict, block_content_id_val: str,
                     quarantined: bool = False, block_text: str | None = None) -> dict:
    """Reconcile one candidate against the graph and place it. Returns an outcome record."""
    # Fork-A: NFC-normalize identity components at the write boundary before they enter edge_id.
    cand["subj_node"] = nfc(cand["subj_node"])
    cand["predicate"] = nfc(cand["predicate"])
    if cand.get("obj_node"):
        cand["obj_node"] = nfc(cand["obj_node"])

    # pc.7: span-grounding at the write boundary - quarantine a candidate whose source_quote is not
    # a normalized substring of its own cited block (extractor contamination), never silently place it.
    if block_text is not None and not span_grounded(cand.get("source_quote") or "", block_text):
        quarantined = True

    # pc.3: classify the atom deterministically and enforce its per-kind requireds.
    atom_type = classify_atom(cand)
    cand["atom_type"] = atom_type
    if atom_type == "FACT" and not cand.get("source_block_id"):
        raise ValueError("FACT atom rejected: missing source_block_id/stamp")
    if entity_is_ambiguous(db, cand):
        # ENTITY MUST go through the shared resolver; an ambiguous bare surface is QUARANTINED,
        # never auto-bound to one node (the two-Dat invariant).
        quarantined = True

    # pc.7: a claim carrying a relative-time phrase gets its learned_at (km.4 capture date) RESOLVED
    # against the source's OWN authored date - distinct from recorded_at (the wall-clock ingest
    # stamp). 'recently' in a 2020 source anchors to 2020, never the ingest day.
    _sca = cand.get("source_created_at")
    if _sca and _has_relative_time(cand.get("source_quote") or ""):
        cand["learned_at"] = resolve_relative_time(cand.get("source_quote") or "", _sca)

    eid = make_edge_id(cand["subj_node"], cand["predicate"], obj_key(cand), block_content_id_val)
    cand["edge_id"] = eid
    verdict = rec.classify(db, cand, eid)

    row = dict(cand)
    row["edge_id"] = eid
    row["reconcile_verdict"] = verdict.verdict
    if quarantined or cand.get("extractor") == "llm":
        row["status"] = "quarantined"

    action = verdict.verdict
    if verdict.verdict == "duplicate":
        # identity-preserving: same content-derived edge_id. On a re-chunk the positional
        # source_block_id may now point at shifted text - re-anchor it to the current block (F2).
        moved = writer.reanchor_edge(eid, cand["source_block_id"],
                                     cand.get("source_doc_id"), cand["source_quote"])
        action = "reanchored" if moved else "noop"
    elif verdict.verdict == "matches":
        bumped = writer.bump_corroboration(verdict.prior_edge_id, cand["source_block_id"])
        action = "corroborated" if bumped else "noop"     # F9: no inflation on a repeat source
    elif verdict.verdict == "novel":
        writer.upsert_edge(row)
    elif verdict.verdict == "refines":
        row["supersedes_edge_id"] = verdict.prior_edge_id # lineage only; both stay active
        writer.upsert_edge(row)
    elif verdict.verdict == "contradicts":
        writer.upsert_edge(row)
        if verdict.prior_edge_id and row.get("status") != "quarantined":
            writer.supersede_edge(verdict.prior_edge_id, eid, world_change=verdict.world_change,
                                  world_at=cand.get("valid_from"))
        action = "world-change" if verdict.world_change else "correction"

    return {"edge_id": eid, "verdict": verdict.verdict, "action": action,
            "quarantined": row.get("status") == "quarantined"}
