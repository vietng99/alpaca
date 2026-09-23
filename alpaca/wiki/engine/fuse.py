"""FUSE (§5 STEP 2). RRF(k=60) over {PPR, BM25, vector}, exact-match floored in, then rerank over
real blocks, then the bounded additive AUTHORITY prior (C12, opt-in per vault), then the
deterministic final tie-break: score DESC -> authority DESC -> page_band DESC -> valid_at DESC ->
block_id ASC. RRF is rank-based with NO score normalization (the deterministic referee).

This module stays DB-free (R1: only `retrieve` imports `store.read`, pinned by
tests/test_r1_import_guard.py) - the authority integer is READ in retrieve and RIDES here on the
hit, so adding the prior did not open a second door to the store.
"""
from __future__ import annotations

import math

from ..determinism import rrf_fuse, stable_rank, to_band
from ..types import RetrievalHit

FLOOR_BONUS = 1000.0    # guarantees an exact hit is never evicted (it may be reordered by rerank)
MAX_AUTHORITY = 100
MAX_AUTHORITY_MASS = 0.04


def fuse(hits: list[RetrievalHit], channel_lists: dict[str, list[str]],
         reranker, question: str, rrf_k: int = 60,
         authority_unit: float = 0.0) -> list[RetrievalHit]:
    """C12 - a BOUNDED ADDITIVE authority prior mixed into the score, never a lone tie-break key.

    `authority_unit` scales docs.source_authority (carried onto each hit by
    retrieve._doc_marks_for) into the same units as the RRF/rerank mass. It is DEFAULT 0.0, and
    that default is the whole compatibility guarantee: `x + 0.0` is bit-identical to `x` in
    IEEE-754, so a vault that never pins the knob computes exactly the pre-change fused score and
    every existing golden determinism pin still reproduces. Only a vault that opts in (meta
    `authority_unit`) pays anything.

    WHY an additive prior and not a tie-break: the delivered order is sorted on the INTEGER band
    (`to_band`, scale 1e6), so two candidates tie only when their fused scores agree to ~1e-6.
    Ties that coarse are rare, so a tie-break-only implementation would leave a PROVISIONAL block
    out-ranking a SIGNED one at every NEAR-tie - the exact failure this mechanism exists to
    prevent. Adding the prior into the score moves the near-ties too.

    WHY bounded: the prior is a nudge, not a gag. At the brain vault's pinned unit the whole
    SIGNED-vs-PROVISIONAL span is worth about two channels' top-rank RRF mass, so a block that is
    genuinely top-ranked in every channel still beats an authoritative block nobody retrieved.
    Authority ranks what relevance already found; it never substitutes for relevance. And
    FLOOR_BONUS stays orders of magnitude larger, so the exact-match floor remains un-overridable
    by any authority mass.

    The term is computed HERE, in the deterministic middle, from an integer stored on the doc row
    - never by a model, never from adjacency, never from a caller-supplied score.
    """
    if not math.isfinite(authority_unit) or authority_unit < 0:
        raise ValueError("authority_unit must be a finite non-negative number")
    rrf = rrf_fuse(channel_lists, k=rrf_k)
    rr = reranker.rerank(question, [(h.block_id, h.text) for h in hits])
    for h in hits:
        base = rrf.get(h.block_id, 0.0)
        h.norm["rrf"] = base
        h.rerank = rr.get(h.block_id, 0.0)
        authority = max(0, min(MAX_AUTHORITY, int(getattr(h, "authority", 0) or 0)))
        h.authority = authority
        h.norm["authority"] = min(MAX_AUTHORITY_MASS, authority_unit * float(authority))
        h.fused = base + (h.rerank or 0.0) + h.norm["authority"] \
            + (FLOOR_BONUS if h.exact_floor else 0.0)
        h.band = to_band(h.fused)      # Fork-A: integer band is the hashed sort key, not the float
    return stable_rank(hits)
