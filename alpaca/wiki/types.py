"""Frozen core types.

The **Answer** type is the R1 return-type seam (OWD-6): a 6-field unconstructable core whose
`currency_stamp` and `completeness` are non-null by construction - that pair is the confident-wrong
gate. `contract_version` + `extra` are the forward-compat escape hatches. Nothing outside
`engine.answer` should construct an Answer with `verdict='grounded'`.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

CONTRACT_VERSION = "1.0"


class UntrustedEvidenceError(ValueError):
    """Evidence-only recall cannot be used as a machine policy or instruction."""


@dataclass(frozen=True)
class CurrencyStamp:
    """As-of resolution proof (guard #1). Non-null on every Answer."""
    as_of: str                      # ISO-8601 T the read was fixed at
    txn_axis: str = "latest"        # 'latest' | explicit recorded_at bound
    resolved_from: str = "now"      # how T was parsed from the question


@dataclass(frozen=True)
class Completeness:
    """Wrong-by-omission verdict (guard #4). Non-null on every Answer."""
    successor_read: bool            # every used edge checked for a newer same-supersede_key
    overturner_queried: bool        # live contradicts edges on the answer's nodes were read
    subclaim_coverage: bool         # each sub-claim entailed by a cited block
    unmet_predicate: Optional[str] = None   # the specific failed template, if any


@dataclass(frozen=True)
class Citation:
    claim_text: str
    claim_kind: str                 # prose|number|date|count|citation
    source_block_id: Optional[str]
    source_edge_id: Optional[str]
    verdict: str                    # supported|unsupported|contradicted
    entailment_score: Optional[float] = None
    checker: Optional[str] = None


@dataclass(frozen=True)
class Answer:
    """THE frozen contract returned by the one door. Unconstructable as 'grounded' outside verify."""
    # --- 6-field unconstructable core ---
    question: str
    verdict: str                    # 'grounded' | 'conflict' | 'abstained'
    answer_text: Optional[str]
    currency_stamp: CurrencyStamp   # NON-NULL by construction
    completeness: Completeness      # NON-NULL by construction
    citations: tuple[Citation, ...] = ()
    # --- forward-compat hatches ---
    contract_version: str = CONTRACT_VERSION
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def abstained(self) -> bool:
        return self.verdict == "abstained"

    def as_trusted_policy(self) -> str:
        """Return policy text only for an answer backed entirely by attested sources.

        Raw recall remains visible as cited evidence, but callers must not treat it as executable
        direction. This is a consumer boundary, not a claim that text filtering prevents injection.
        """
        if self.extra.get("source_trust") != "policy-authorized":
            raise UntrustedEvidenceError(
                "answer is evidence-only and cannot be consumed as trusted machine policy")
        return self.answer_text or ""


@dataclass
class RetrievalHit:
    """Internal, mutable during fusion. Carries BOTH raw and normalized scores per channel."""
    block_id: str
    text: str
    channels: dict[str, float] = field(default_factory=dict)   # channel -> raw score
    norm: dict[str, float] = field(default_factory=dict)       # channel -> normalized score
    fused: float = 0.0
    band: int = 0                   # INTEGER band of fused (Fork-A): the hashed sort key, float-noise-immune
    page_band: int = 0
    valid_at: str = ""
    exact_floor: bool = False       # un-overridable exact-match floor member
    rerank: Optional[float] = None
    # C12: docs.source_authority of this block's doc, carried by retrieve._doc_marks_for. HIGHER =
    # more authoritative (the same scale oracle.arbitrate_conflict already ranks by). The default 0
    # is fail-closed in the only direction that matters: a hit built without a doc mark - a duck-typed
    # stand-in, a hand-made test hit, a vault that never marked its docs - carries NO authority mass
    # and so can never out-rank a doc that was actually attested.
    authority: int = 0
    source_trust: str = "evidence-only"  # Ordinary retrieval is evidence, never policy authority.


@dataclass
class Block:
    block_id: str
    block_content_id: str
    occurrence_index: int
    doc_id: str
    ordinal: int
    text: str
    heading_path: str = ""
    char_start: int = 0
    char_end: int = 0
    block_sha256: str = ""
    block_type: str = "prose"
    context_header: str = ""
    chunk_ruleset_version: str = ""
    embedded_model: str = ""


@dataclass
class EdgeCandidate:
    """An assertion proposed by extraction, before reconcile/stamp/upsert."""
    subj_node: str
    predicate: str
    obj_datatype: str               # node|date|number|string|bool
    source_block_id: str
    source_quote: str
    obj_node: Optional[str] = None
    obj_literal: Optional[str] = None
    source_doc_id: Optional[str] = None
    extractor: str = "deterministic"    # deterministic|llm|reflection
    extractor_version: str = "0"
    confidence: float = 1.0
    valid_from: str = ""
    valid_until: Optional[str] = None

    def obj_key(self) -> str:
        """The object identity folded into edge_id: node id for node-objects, canonical literal else."""
        if self.obj_datatype == "node":
            return f"node:{self.obj_node or ''}"
        return f"lit:{self.obj_datatype}:{self.obj_literal or ''}"
