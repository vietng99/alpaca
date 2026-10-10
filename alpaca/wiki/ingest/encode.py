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


def _later_same_state(db: DB, cand: dict, verdict, successive_of: str | None,
                      rule_id: str | None) -> bool:
    """True when an ordered-log candidate meets a prior of its own origin and its own rule (the same
    event kind moving the same thing) that holds the same object of a single-valued predicate and
    started strictly earlier. Another rule naming the same fact is a second source: it corroborates."""
    if not (successive_of and rule_id and verdict.prior_edge_id and cand.get("valid_from")):
        return False
    prior = db.conn.execute("SELECT valid_from FROM edges WHERE edge_id=?",
                            (verdict.prior_edge_id,)).fetchone()
    pred = rec._pred_row(db, cand["predicate"])
    if prior is None or (pred or {}).get("cardinality", "multi") != "single":
        return False
    if not (prior[0] or "") < cand["valid_from"]:
        return False
    return db.conn.execute(
        "SELECT 1 FROM edge_origin WHERE edge_id=? AND origin=? AND rule_id=? AND status='live'",
        (verdict.prior_edge_id, successive_of, rule_id)).fetchone() is not None


def _prior_event_is_later(db: DB, verdict, successive_of: str | None, event_id: int | None) -> bool:
    """True when the prior edge holds a live ordered-log claim made from a record event with a higher
    id than this output's event (the record's own total order)."""
    if not (successive_of and event_id is not None and verdict.prior_edge_id):
        return False
    row = db.conn.execute(
        "SELECT MAX(source_event_id) FROM edge_origin WHERE edge_id=? AND origin=? AND status='live'",
        (verdict.prior_edge_id, successive_of)).fetchone()
    return row is not None and row[0] is not None and int(row[0]) > int(event_id)


def encode_candidate(db: DB, writer: Writer, cand: dict, block_content_id_val: str,
                     quarantined: bool = False, block_text: str | None = None,
                     successive_of: str | None = None, rule_id: str | None = None,
                     event_id: int | None = None) -> dict:
    """Reconcile one candidate against the graph and place it. Returns an outcome record.

    `successive_of` names an origin of ordered facts (`edge_origin.origin`, the record rules). A
    later value of a single-valued predicate whose prior edge holds a live claim of that origin is
    the next state of the same thing in an ordered log, not a source that disagrees: reconcile still
    ends the prior's window at the candidate's valid time (a world change) or invalidates it (the
    same instant), and links the two, but the successor is stored as `novel`, because every reader
    of a live `contradicts` edge (the overturner query, lucid, health) takes it for an open
    contention. A prior of any other origin keeps the `contradicts` verdict. `rule_id` names the rule
    whose output this is: a later same-valued output of that rule is the next state, not a second source."""
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
    elif verdict.verdict == "matches" and _later_same_state(db, cand, verdict, successive_of, rule_id):
        # A later move to the state the log already holds is the next state of the thing, not a
        # second source for the old one: capture order must not decide the status history. Were it
        # a corroboration, a move to another state captured afterwards (a session whose events land
        # later) would end the old edge before this move and drop it from the graph.
        row["reconcile_verdict"] = "novel"
        writer.upsert_edge(row)
        writer.supersede_edge(verdict.prior_edge_id, eid, world_change=True,
                              world_at=cand.get("valid_from"))
        action = "world-change"
        verdict = rec.Verdict("novel", prior_edge_id=verdict.prior_edge_id)
    elif verdict.verdict == "matches":
        bumped = writer.bump_corroboration(verdict.prior_edge_id, cand["source_block_id"])
        action = "corroborated" if bumped else "noop"     # F9: no inflation on a repeat source
    elif verdict.verdict == "novel":
        writer.upsert_edge(row)
    elif verdict.verdict == "refines":
        row["supersedes_edge_id"] = verdict.prior_edge_id # lineage only; both stay active
        writer.upsert_edge(row)
    elif verdict.verdict == "contradicts":
        if successive_of and verdict.prior_edge_id and db.conn.execute(
                "SELECT 1 FROM edge_origin WHERE edge_id=? AND origin=? AND status='live'",
                (verdict.prior_edge_id, successive_of)).fetchone():
            row["reconcile_verdict"] = "novel"
        writer.upsert_edge(row)
        if (not verdict.world_change and _prior_event_is_later(db, verdict, successive_of, event_id)
                and row.get("status") != "quarantined"):
            # Two ordered-log facts at one instant: the record's order (event id) picks the survivor,
            # not the order they reach the wiki (capture is per session, recovery too). The arriving
            # fact came earlier in the record than the live prior, so it is the one corrected.
            writer.supersede_edge(eid, verdict.prior_edge_id, world_change=False)
            action = "correction"
        elif verdict.prior_edge_id and row.get("status") != "quarantined":
            writer.supersede_edge(verdict.prior_edge_id, eid, world_change=verdict.world_change,
                                  world_at=cand.get("valid_from"))
            action = "world-change" if verdict.world_change else "correction"
        else:
            action = "world-change" if verdict.world_change else "correction"

    return {"edge_id": eid, "verdict": verdict.verdict, "action": action,
            "prior_edge_id": verdict.prior_edge_id,
            "quarantined": row.get("status") == "quarantined"}
