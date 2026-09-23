"""Oracle / verification (§6, R2). grounded-to-cited-source + completeness + conflict-arbitration
+ abstain. A reproducible DAG of yes/no checks - NEVER a lone LLM judge.

LAYER 1 structural · LAYER 2 resolvability (deterministic, hard-veto) · LAYER 3 entailment
(ensemble seam; disagreement -> abstain) · LAYER 4 completeness / wrong-by-omission (the templates)
· LAYER 5 conflict arbitration (authority+recency+corroboration, surfaced not collapsed).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..determinism import normalize_text
from ..store.asof import AsOf
from ..store.db import DB
from ..store import query as q
from ..types import Citation, Completeness
from . import templates as T

_NUM = re.compile(r"-?\d+(?:[.,]\d+)?|\d{4}-\d{2}-\d{2}")
_SENT = re.compile(r"[^.!?。！？\n]+(?:[.!?。！？]+|\n|$)")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_QUESTION_OPENERS = frozenset({
    "who", "what", "when", "where", "why", "how", "which",
    "is", "are", "was", "were", "do", "does", "did", "can", "could", "would", "should",
    "ai", "gì", "khi", "đâu", "sao", "nào", "bao",
})
_QUESTION_FILLER = _QUESTION_OPENERS | frozenset({
    "a", "an", "the", "at", "by", "for", "from", "in", "of", "on", "to",
    "he", "her", "hers", "him", "his", "it", "its", "she", "their", "them", "they",
    "this", "that", "these", "those", "we", "you", "your", "me", "please",
    "là", "của", "có", "ở", "và", "cho", "tôi",
})


@dataclass
class OracleResult:
    verdict: str                        # grounded | conflict | abstained | insufficient
    completeness: Completeness
    citations: list[Citation]
    dag: dict = field(default_factory=dict)
    reason: str = ""
    conflict_note: str = ""
    gap: dict = field(default_factory=dict)        # oracle-safety.1 GAP block, populated on abstain
    stripped: list[str] = field(default_factory=list)   # oracle-safety.3 refuse-to-invent flags


def span_grounded(source_quote: str, block_text: str) -> bool:
    """provenance-capture.7: the asserted quote must be an NFC-normalized substring of its cited
    block. Catches neighbour-block contamination - content present in the retrieved context but NOT
    in the block the claim actually cites. Deterministic, always-on hard veto."""
    if not source_quote:
        return False
    return normalize_text(source_quote) in normalize_text(block_text)


def _sentences(draft: str) -> list[str]:
    return [s.strip() for s in _SENT.findall(draft or "") if s.strip()]


def entailment_gate(sentences, winner_texts, entail) -> tuple[list[str], list[str]]:
    """oracle-safety.3 refuse-to-invent: every asserted sentence in a draft must be entailed by at
    least one read-winner atom, else it is stripped/flagged. Returns (kept, flagged). Adjudicated by
    whatever entailer is passed (the oracle-safety.1 ensemble in production)."""
    if isinstance(sentences, str):
        sentences = _sentences(sentences)
    kept: list[str] = []
    flagged: list[str] = []
    for s in sentences:
        ok = False
        for text in winner_texts:
            label, _score = entail(s, text)
            if label == "supported":
                ok = True
                break
        (kept if ok else flagged).append(s)
    return kept, flagged


def _required_subclaim_entail(claim: str, source: str, entail) -> tuple[str, float]:
    """Evaluate a planned question fragment against evidence without treating question words as facts."""
    label, score = entail(claim, source)
    words = [w.lower() for w in _WORD.findall(claim)]
    if label == "supported" or not words or words[0] not in _QUESTION_OPENERS:
        return label, score
    required = [w for w in words if w not in _QUESTION_FILLER and w != "s"]
    available = [w.lower() for w in _WORD.findall(source)]

    def matches(want: str) -> bool:
        return any(want == got or (len(want) >= 4 and len(got) >= 4
                                   and (want.startswith(got) or got.startswith(want)))
                   for got in available)

    if required and all(matches(word) for word in required):
        return "supported", 1.0
    return label, score


def evaluate(db: DB, entailer, asof: AsOf, plan_templates: list[str],
             claims: list[dict], cited_blocks: dict[str, str],
             used_edge_ids: list[str], read_edge_ids: set[str],
             answer_node_ids: list[str], floor_ids: list[str],
             final_ids: list[str],
             required_subclaims: list[str] | None = None) -> OracleResult:
    dag: dict = {}
    citations: list[Citation] = []
    hard_fail = None

    # LAYER 1 + 2 + 3 per claim
    for c in claims:
        bid = c.get("source_block_id")
        src = cited_blocks.get(bid, "")
        # structural
        if not bid or bid not in cited_blocks:
            citations.append(Citation(c["text"], c.get("kind", "prose"), bid,
                                      c.get("source_edge_id"), "unsupported", 0.0, "structural"))
            hard_fail = hard_fail or f"citation not well-formed: {bid}"
            continue
        # resolvability: numbers/dates in the claim must match the source block exactly
        cnums = set(_NUM.findall(c["text"]))
        snums = set(_NUM.findall(src))
        if cnums and not cnums.issubset(snums):
            citations.append(Citation(c["text"], c.get("kind", "prose"), bid,
                                      c.get("source_edge_id"), "contradicted", 0.9, "resolvability"))
            hard_fail = hard_fail or f"number/date mismatch vs source in claim: {c['text'][:50]}"
            continue
        # span-grounding (pc.7): the asserted quote must live INSIDE its cited block (NFC substring),
        # not merely somewhere in the retrieved context - a hard veto against neighbour contamination.
        if not span_grounded(c["text"], src):
            citations.append(Citation(c["text"], c.get("kind", "prose"), bid,
                                      c.get("source_edge_id"), "unsupported", 0.0, "span-grounding"))
            hard_fail = hard_fail or f"claim not span-grounded in its cited block: {c['text'][:50]}"
            continue
        # entailment (ensemble seam)
        label, score = entailer.entail(c["text"], src)
        citations.append(Citation(c["text"], c.get("kind", "prose"), bid,
                                   c.get("source_edge_id"), label, score, entailer.name))

    dag["structural_resolvability"] = {"hard_fail": hard_fail}
    dag["entailment"] = [{"claim": c.claim_text[:60], "verdict": c.verdict, "score": c.entailment_score}
                         for c in citations]

    # LAYER 4 completeness - evaluate the selected templates as code
    cited_texts = list(cited_blocks.values())
    # Production supplies the planner's requested subclaims so completeness measures the question,
    # not merely the claims the assembler happened to generate. The fallback preserves direct-call
    # compatibility for callers that exercise the oracle without a Plan.
    subclaims = required_subclaims if required_subclaims is not None else [c["text"] for c in claims]
    coverage_entail = (lambda claim, source: _required_subclaim_entail(
        claim, source, entailer.entail)) if required_subclaims is not None else entailer.entail
    results = {}
    if "SUCCESSOR_READ" in plan_templates:
        results["SUCCESSOR_READ"] = T.successor_read(db, used_edge_ids, read_edge_ids, asof)
    if "OVERTURNER_QUERY" in plan_templates:
        results["OVERTURNER_QUERY"] = T.overturner_query(db, answer_node_ids, read_edge_ids, asof)
    if "AS_OF_CURRENCY" in plan_templates:
        results["AS_OF_CURRENCY"] = T.as_of_currency(db, used_edge_ids, asof)
    if "SUBCLAIM_COVERAGE" in plan_templates:
        results["SUBCLAIM_COVERAGE"] = T.subclaim_coverage(subclaims, cited_texts, coverage_entail)
    if "EXACT_MATCH_FLOOR" in plan_templates:
        results["EXACT_MATCH_FLOOR"] = T.exact_match_floor(final_ids, floor_ids)
    dag["completeness"] = {k: {"passed": v.passed, "detail": v.detail} for k, v in results.items()}

    completeness = Completeness(
        successor_read=results.get("SUCCESSOR_READ", T.PredResult("", True)).passed,
        overturner_queried=results.get("OVERTURNER_QUERY", T.PredResult("", True)).passed,
        subclaim_coverage=results.get("SUBCLAIM_COVERAGE", T.PredResult("", True)).passed,
        unmet_predicate=next((k for k, v in results.items() if not v.passed), None),
    )

    # LAYER 5 conflict arbitration
    conflict_note = ""
    contradicted = [c for c in citations if c.verdict == "contradicted"]
    overturner_fail = results.get("OVERTURNER_QUERY") and not results["OVERTURNER_QUERY"].passed

    # ---- roll up the DAG into a verdict ----
    # oracle-safety.1 GAP block: on abstain we surface WHY and the closest pages we did read, so an
    # abstention is never a silent dead end. closest_pages are the top-N ranked block ids.
    def _gap(reason: str) -> dict:
        return {"reason": reason, "closest_pages": list(final_ids[:5])}

    if hard_fail:
        return OracleResult("abstained", completeness, citations, dag, reason=hard_fail,
                            gap=_gap(hard_fail))
    if contradicted or overturner_fail:
        # F7: actually RANK the disagreeing edges by source_authority + recency + corroboration and
        # surface the winner - never emit a static note that claims arbitration it did not perform.
        disagreeing = q.overturners_for_nodes(db, answer_node_ids, asof)
        for c in citations:
            if c.verdict == "contradicted" and c.source_edge_id:
                e = q.get_edge(db, c.source_edge_id)
                if e:
                    disagreeing.append(e)
        ranking = arbitrate_conflict(db, disagreeing)
        dag["conflict_arbitration"] = ranking
        if ranking["winner"]:
            w = q.get_edge(db, ranking["winner"]) or {}
            wobj = w.get("obj_node") or w.get("obj_literal") or "?"
            conflict_note = (f"sources disagree; per authority+recency+corroboration the most "
                             f"authoritative is {w.get('subj_node','?')} {w.get('predicate','?')} {wobj}")
        else:
            conflict_note = "sources disagree; no arbitrable edge set"
        return OracleResult("conflict", completeness, citations, dag,
                            reason="contradiction/overturner", conflict_note=conflict_note)
    unmet = completeness.unmet_predicate
    if unmet:
        return OracleResult("insufficient", completeness, citations, dag,
                            reason=f"unmet predicate: {unmet}", gap=_gap(f"unmet predicate: {unmet}"))
    if any(c.verdict != "supported" for c in citations) or not citations:
        r = "no relevant block" if not citations else "claim not entailed by a cited block"
        return OracleResult("insufficient", completeness, citations, dag,
                            reason=r, gap=_gap(r))
    return OracleResult("grounded", completeness, citations, dag, reason="all layers passed")


def _source_authority(db: DB, edge: dict) -> int:
    doc_id = edge.get("source_doc_id")
    if not doc_id:
        return 0
    r = db.conn.execute("SELECT source_authority FROM docs WHERE doc_id=?", (doc_id,)).fetchone()
    return int(r["source_authority"]) if r and r["source_authority"] is not None else 0


def arbitrate_conflict(db: DB, edges: list[dict]) -> dict:
    """Rank contradictory edges by source_authority + recency (valid_from) + corroboration (§6 L5).

    F7 fix: authority is now the PRIMARY key (it was computed then discarded); a static note that
    claimed authority-ranking without performing it is gone. De-duplicated by edge_id, stable.
    """
    seen, uniq = set(), []
    for e in edges:
        if e.get("edge_id") and e["edge_id"] not in seen:
            seen.add(e["edge_id"]); uniq.append(e)

    from .echo import corroboration_for_arbitration    # os.2: independent-witness count, or drop
    def key(e):
        iwc = corroboration_for_arbitration(db, e)
        corr = iwc if iwc is not None else 0           # unprovable independence -> drop the term
        return (_source_authority(db, e), e.get("valid_from") or "",
                corr, float(e.get("confidence") or 0.0))
    ranked = sorted(uniq, key=key, reverse=True)
    return {"winner": ranked[0]["edge_id"] if ranked else None,
            "ranked": [e["edge_id"] for e in ranked]}
