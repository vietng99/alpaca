"""Value types of the Rune2 caller interface (design/rune2-memory/interfaces.py.txt section 1).

The functions live in alpaca/wiki/service.py; these are the plain-data shapes they return. No type
here holds a provider object, makes a model call or opens a connection. Outcomes are decided only by
alpaca/wiki/outcome.py:classify_outcome (architecture.md 6.1).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from enum import Enum
from typing import Literal, Protocol


class Outcome(str, Enum):
    """architecture.md 6.1, first match wins in outcome.classify_outcome."""
    UNSUPPORTED = "unsupported"      # an invalid filter value, or valid-time-unknown
    UNAVAILABLE = "unavailable"      # store cannot open, or a scoped source is missing
    REFUSED = "refused"              # every match failed the servable predicate; no text returned
    STALE = "stale"                  # only support is a derived item whose anchor no longer resolves
    CONFLICT = "conflict"            # door verdict
    HISTORICAL = "historical"        # grounded, but nothing cited is current at latest knowledge
    GROUNDED = "grounded"            # door verdict
    PARTIAL = "partial"              # door abstained and the question's scope has capture gaps
    EMPTY = "empty"                  # door abstained and retrieval returned nothing
    ABSTAINED = "abstained"          # door verdict otherwise


# evaluation.md section 1.3 class for each outcome (the t-063 runner reads this map).
EVAL_CLASS = {
    "grounded": "answer", "conflict": "conflict", "historical": "historical",
    "refused": "refuse", "unavailable": "unavailable", "partial": "unavailable",
    "empty": "abstain", "abstained": "abstain", "stale": None, "unsupported": None,
}

# Outcomes whose door text may be returned to the caller. Every other outcome returns no answer text.
TEXT_OUTCOMES = frozenset({"grounded", "historical", "conflict"})


#: The closed set of `edge_origin.fact_kind` values (architecture.md 7.2); `Filters.fact_kind` is one.
FACT_KINDS = ("record-fact", "agent-experience", "derived-hypothesis")


@dataclass(frozen=True)
class Filters:
    """FR-11 structured filters (store/filters.py). Filters narrow retrieval; they never authorize:
    the servable predicate still runs after them.

    `op`, `task`, `operator` and `session` match the header line of the same name of a note, a
    transcript or an Alpaca source. `source_kind` is an event kind (`result`, `proof-report`) or
    `transcript`, `proof`, `contract`, `decision`. `fact_kind` matches the blocks that anchor an edge
    with a live support claim of that kind. A default read leaves out bookkeeping notes
    (heartbeats, dec-5ea0e31009b7); `include_bookkeeping` reads them too."""
    op: str | None = None
    task: str | None = None
    operator: str | None = None
    session: str | None = None
    source_kind: str | None = None
    fact_kind: str | None = None
    include_bookkeeping: bool = False

    def is_empty(self) -> bool:
        return all(getattr(self, f.name) in (None, False) for f in fields(self))

    def invalid_reason(self) -> str | None:
        """Why a value cannot be applied, or None. A value that is not a nonempty string, or a
        `fact_kind` outside FACT_KINDS, is refused by the caller interface instead of matching
        nothing, so a typo never reads as an empty store."""
        for name in ("op", "task", "operator", "session", "source_kind", "fact_kind"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                return "%s must be a nonempty string" % name
        if self.fact_kind is not None and self.fact_kind not in FACT_KINDS:
            return "fact_kind must be one of %s" % ", ".join(FACT_KINDS)
        return None


@dataclass(frozen=True)
class Coverage:
    scope: dict                                   # {"op": ..., "task": ..., "session": ...}
    missing_event_ids_in_scope: tuple[int, ...]
    missing_transcripts_in_scope: tuple[str, ...]
    project_missing_events: int | None            # project-wide; never change the outcome alone;
                                                  # None when a locked store cannot be read
    project_missing_transcripts: int


@dataclass(frozen=True)
class SourceRef:
    """One cited source, with what an agent needs to anchor a proposal (architecture.md 7.1)."""
    source_ref: str                      # "event:<id>", "transcript:<session>", "document:<doc_id>"
    quote: str                           # the cited span, at most 400 characters, inside the block
    source_block_id: str                 # provenance and display only
    block_content_id: str                # provenance only (architecture.md 4.3)
    doc_id: str                          # provenance only
    path: str
    source_time: str | None              # the source's own time; null when unknown
    operator: str | None
    session: str | None
    event_hash: str | None = None        # event: sources, the record row's hash (K-1, OA-1)


@dataclass(frozen=True)
class QueryResult:
    question: str
    outcome: Outcome
    reason: str | None
    verdict: Literal["grounded", "conflict", "abstained"] | None   # None for pre-door outcomes
    answer_text: str | None
    citations: tuple[dict, ...]
    sources: tuple[SourceRef, ...]
    coverage: Coverage
    determinism_hash: str | None
    trace: tuple[str, ...] = ()
    trust: Literal["evidence-only"] = "evidence-only"

    def to_dict(self) -> dict:
        out = asdict(self)
        out["outcome"] = self.outcome.value
        out["citations"] = [dict(c) for c in self.citations]
        out["sources"] = [asdict(s) for s in self.sources]
        out["coverage"]["missing_event_ids_in_scope"] = list(self.coverage.missing_event_ids_in_scope)
        out["coverage"]["missing_transcripts_in_scope"] = list(
            self.coverage.missing_transcripts_in_scope)
        out["trace"] = list(self.trace)
        return out


class RecordAppender(Protocol):
    """Bound by the alpaca/cli.py wiki handlers to alpaca.db.append_event with actor "alpaca-wiki",
    returning the appended event id. Rune2 calls it where architecture.md 10.2 says and never writes
    the record itself. The write verbs that take it arrive with t-066 onward."""
    def __call__(self, kind: str, data: dict) -> int: ...


# ---------------------------------------------------------------- candidate intake (architecture.md 7)

@dataclass(frozen=True)
class Anchor:
    """Identity is (source_ref, quote), resolved to the current block (architecture.md 4.3)."""
    source_ref: str                      # anchors: event:, proof:, contract:, decision: (K-5)
    quote: str                           # an NFC-normalized substring of the block (K-2)
    event_hash: str | None = None        # required for event: anchors, the record row's hash (K-1)
    doc_id: str | None = None            # optional hints, kept as provenance
    block_content_id: str | None = None


@dataclass(frozen=True)
class FactBody:
    subject: str
    predicate: str                       # vocabulary predicate with extraction='llm-allowed'
    obj: str
    obj_datatype: Literal["node", "date", "number", "string", "bool"]
    valid_from: str | None = None        # must appear in a quote when given; else architecture.md 5.1
    # fact_kind is not proposed: Rune2 derives it from the anchor kinds (architecture.md 7.2)


@dataclass(frozen=True)
class ObservationBody:
    """Gate metadata an observation door needs (architecture.md 7.4)."""
    label: str | None = None             # procedural: required; [a-z0-9-]{1,64}
    governs: tuple[str, ...] = ()        # procedural: probe tags the lesson governs
    prescribes: str | None = None        # procedural: the action predicate
    falsifiability: str | None = None    # declarative: a checkable pointer


@dataclass(frozen=True)
class Proposal:
    """JSON body of `bin/alpaca --session <sid> wiki propose --file proposal.json`."""
    kind: Literal["fact", "observation", "summary"]
    anchors: tuple[Anchor, ...]          # 1 to 8 (K-6), each of an INDEPENDENT_KINDS kind (K-5)
    context: tuple[Anchor, ...] = ()     # transcript or wiki-ingest quotes: never counted as support
    fact: FactBody | None = None
    observation: ObservationBody | None = None
    text: str | None = None              # observation (<= 400 chars) or summary (<= 2000 chars)
    sentence_anchors: tuple[tuple[int, ...], ...] = ()   # one tuple per sentence (K-3)
    scope: dict | None = None            # {"op": ...} or {"task": ...}


@dataclass(frozen=True)
class CheckResult:
    check: Literal["K-1", "K-2", "K-3", "K-4", "K-5", "K-6"]
    passed: bool
    reason: str | None                   # e.g. "quote-not-in-span", "not-independent"
    detail: str | None                   # the anchor index or field that failed, never quote text


@dataclass(frozen=True)
class CandidateReceipt:
    candidate_id: str
    status: Literal["checked", "refused", "duplicate"]
    record_event_id: int                 # the wiki-candidate event
    checks: tuple[CheckResult, ...]
    verdict: str | None                  # fact only: advisory reconcile verdict; no edge is written
    prior_edge_id: str | None

    def to_dict(self) -> dict:
        out = asdict(self)
        out["checks"] = [asdict(c) for c in self.checks]
        return out


@dataclass(frozen=True)
class CandidateView:
    candidate_id: str
    kind: str
    status: str
    fresh: bool                          # computed at read time: every anchor still resolves
    proposer: str
    proposer_kind: Literal["session", "job"]
    proposer_operator: str | None
    body: dict | None                    # None when any anchor fails the servable predicate
    body_sha256: str
    body_redacted: bool
    anchors: tuple[dict, ...]            # {source_ref, quote or None, quote_sha256, redacted, role,
                                         #  checked}; checked is False for a context entry (OA-5)
    admission_ref: str | None
    admission_effect: str | None
    checks: tuple[dict, ...] = ()

    def to_dict(self) -> dict:
        out = asdict(self)
        out["anchors"] = [dict(a) for a in self.anchors]
        out["checks"] = [dict(c) for c in self.checks]
        return out


@dataclass(frozen=True)
class WriteReceipt:
    """What `admit` and `withdraw_candidate` return (interfaces.py.txt section 2)."""
    status: Literal["applied", "refused", "duplicate", "unavailable"]
    record_event_id: int | None
    reason: str | None                   # e.g. "self-admission", "valid-time-required", "capture-behind"
    effects: dict

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class JobResult:
    """What one bounded job step returns (interfaces.py.txt section 4, architecture.md 9.2)."""
    done: int
    remaining: int
    proposed: tuple[str, ...]            # candidate ids emitted; never admitted by the job
    failed: tuple[dict, ...]             # {key, class, retryable, attempts, attempted, [candidate_id]}
    checkpoint_event_id: int

    def to_dict(self) -> dict:
        out = asdict(self)
        out["proposed"] = list(self.proposed)
        out["failed"] = [dict(f) for f in self.failed]
        return out


#: The sections of a briefing, in display order (FR-19), and the label each item carries.
BRIEFING_SECTIONS = ("contract", "state", "blockers", "decisions", "failures", "agent-summaries")
BRIEFING_LABELS = ("source", "record-fact", "admitted", "agent-summary-candidate")


@dataclass(frozen=True)
class BriefingItem:
    """One cited item of a briefing (interfaces.py.txt section 1; FR-19). Every source passed the
    servable predicate when the briefing was built (architecture.md 6.2). `fresh` is False when an
    anchor no longer resolves in the current version of its source (stale); `conflict` is True
    when another current record fact on the same single-valued key disagrees."""
    section: Literal["contract", "state", "blockers", "decisions", "failures", "agent-summaries"]
    text: str
    sources: tuple[SourceRef, ...]
    fresh: bool
    conflict: bool
    label: Literal["source", "record-fact", "admitted", "agent-summary-candidate"]


@dataclass(frozen=True)
class Briefing:
    """A task or op briefing, built on demand and never stored (EX-22; alpaca/wiki/briefing.py).
    `marker` is the evidence-only marker line the text opens with (FR-20); a briefing is never an
    anchor kind. `items` are the items the text shows, `gaps` what is missing in the subject's scope
    (uncaptured events, no contract or state), `truncated` whether the budget cut or left out an
    item, and `text` the rendered briefing, never longer than `max_chars`."""
    subject: str                         # "task:t-061" or "op:op-008"
    marker: str
    items: tuple[BriefingItem, ...]
    gaps: tuple[str, ...]
    truncated: bool
    text: str = ""
    max_chars: int = 2500
    trust: Literal["evidence-only"] = "evidence-only"

    def to_dict(self) -> dict:
        out = asdict(self)
        out["items"] = [dict(asdict(i), sources=[asdict(s) for s in i.sources]) for i in self.items]
        out["gaps"] = list(self.gaps)
        return out


class Memory(Protocol):
    """The agent-facing interface: the module functions of alpaca/wiki/service.py, root first, one
    `bin/alpaca wiki <verb>` each (architecture.md 2; design/rune2-memory/interfaces.py.txt
    `Memory`). The names are the interface file's fourteen; each signature is the service function's
    own, so the keywords the service adds beside the interface's (`filters` and `briefing_task` of
    `context`, the owner's `by`, the capture runner's budget keywords) are listed too (t-070 D-6;
    `alpaca/tests/wiki/test_rc5_conformance.py` holds the two in step)."""

    def status(self, root) -> dict: ...
    def query(self, root, question: str, *, as_of=None, knowledge_as_of=None,
              filters: Filters | None = None) -> dict: ...
    def context(self, root, question: str, *, max_chars=2500, filters: Filters | None = None,
                briefing_task: str | None = None) -> str: ...
    def briefing(self, root, *, task: str | None = None, op: str | None = None,
                 max_chars: int = 2500) -> Briefing: ...
    def ingest(self, root, source, *, doc_id: str, session: str | None = None) -> dict: ...
    def record_facts_preview(self, root, *, from_event: int, through_event: int) -> list: ...
    def propose(self, root, proposal, *, session: str, record: RecordAppender) -> CandidateReceipt: ...
    def candidates(self, root, *, status: str | None = None, session: str | None = None,
                   kind: str | None = None) -> list: ...
    def admit(self, root, candidate_id: str, *, session: str, record: RecordAppender,
              probe: str | None = None) -> WriteReceipt: ...
    def withdraw_candidate(self, root, candidate_id: str, *, session: str, record: RecordAppender,
                           reason: str, by: str | None = None) -> WriteReceipt: ...
    def consolidate(self, root, *, deadline_s: float, record: RecordAppender) -> JobResult: ...
    def correct(self, root, target, action: str, *, session: str, record: RecordAppender,
                reason: str, replacement: dict | None = None, effective_at: str | None = None,
                by: str | None = None) -> WriteReceipt: ...
    def withdraw_source(self, root, source_ref: str, *, session: str, record: RecordAppender,
                        reason: str) -> WriteReceipt: ...
    def capture(self, root, *, sessions=(), events=(), deadline_s, session_events: bool = False,
                uncaptured_only: bool = False, transcripts: bool = True, hard_s: float | None = None,
                absorber=None, projection_index=None, min_documents: int = 0,
                progress=None) -> dict: ...


__all__ = ["Outcome", "EVAL_CLASS", "TEXT_OUTCOMES", "FACT_KINDS", "Filters", "Coverage", "SourceRef",
           "QueryResult", "RecordAppender", "Memory", "Anchor", "FactBody", "ObservationBody",
           "Proposal", "CheckResult", "CandidateReceipt", "CandidateView", "WriteReceipt", "JobResult",
           "BRIEFING_SECTIONS", "BRIEFING_LABELS", "BriefingItem", "Briefing"]
