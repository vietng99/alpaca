"""Withdrawal of a source and of the records derived from it (architecture.md 10.2 steps 1 to 7;
FR-17, FR-22). One function, `withdraw`, used by the live verb (`service.withdraw_source`) and by
the replay of a `wiki-withdrawal` event (`recovery.replay_candidates`), so a rebuild writes what the
live call wrote.

`Writer.withdraw_source` does steps 1 to 5 on the graph. This module adds step 6: the candidates
that cite the source. A `checked` one becomes `stale`; an `admitted` one becomes `retracted`, and
the lesson or prior it put into an Alpaca gate is withdrawn or decayed with the reason
`candidate-withdrawn`. Nothing is deleted, and no model is called.
"""
from __future__ import annotations

REASON = "candidate-withdrawn"


def source_of(store, source_ref: str) -> str:
    """The name the store gives the source that a `document:<doc_id>` ref points at, by the naming
    every serve path uses (`servable.source_refs_for_docs`: the document's `source_versions` row,
    else its header, else `document:<doc_id>`). A `wiki ingest` document is `document:<doc_id>`
    itself; a captured event note, transcript or Alpaca source copy is named by its own source
    (`event:N`, `transcript:<session>`, `proof:<path>`, `contract:<task>`, `decision:<id>`), and a
    withdrawal takes the whole source, every version of it (architecture.md 10.2 step 1). The
    exclusion, the claims, the expected sets and the absorb skip all go by that name, so the
    withdrawal verb turns a document ref into it before anything is recorded (check round 3
    invariants B1). Any other ref, and the ref of a document the store does not hold, comes back as
    given. Read-only."""
    if not isinstance(source_ref, str) or not source_ref.startswith("document:"):
        return source_ref
    from .servable import source_refs_for_docs
    doc_id = source_ref[len("document:"):]
    return source_refs_for_docs(store, [doc_id]).get(doc_id) or source_ref


def dependent_candidates(store, refs) -> list:
    """The candidates that cite one of `refs` as an anchor (a context entry is not support, so it
    does not make a candidate dependent), oldest first: [{candidate_id, status, admission_event_id}]."""
    conn = getattr(store, "conn", store)
    refs = sorted(set(refs))
    if not refs or not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='candidates'").fetchone():
        return []
    marks = ",".join("?" * len(refs))
    return [dict(r) for r in conn.execute(
        "SELECT DISTINCT c.candidate_id,c.status,c.admission_event_id,c.record_event_id "
        "FROM candidates c JOIN candidate_anchors a ON a.candidate_id=c.candidate_id "
        "WHERE a.role='anchor' AND a.ordinal>=0 AND a.source_ref IN (%s) "
        "ORDER BY c.record_event_id,c.candidate_id" % marks, refs)]


def undo_derived(store, admission: dict | None, reason: str = REASON) -> dict:
    """Withdraw what an admitted observation put into the Alpaca gates: its lesson (a
    `lesson-withdraw` row that `LessonsStore._retained` honors) and its prior (`KnowledgeStore.decay`
    with a reason). `admission` is the payload of its `wiki-admission` event; a fact has neither."""
    conn = getattr(store, "conn", store)
    out: dict = {}
    lesson = (admission or {}).get("lesson")
    if isinstance(lesson, dict) and lesson.get("id"):
        from alpaca.lessons import LessonsStore
        LessonsStore(conn).withdraw(lesson["id"], reason=reason)
        out["lesson"] = lesson["id"]
    prior = (admission or {}).get("knowledge")
    if isinstance(prior, dict) and prior.get("claim_id") and prior.get("scope"):
        from alpaca.knowledge import KnowledgeStore
        KnowledgeStore(conn).decay(prior["scope"], prior["claim_id"], reason=reason)
        out["prior"] = prior["claim_id"]
    return out


def withdraw(writer, source_ref: str, *, reason: str, requested_by: str, record_event_id: int,
             admission_of) -> dict:
    """Steps 1 to 6 in the caller's transaction. `admission_of(event_id)` returns the payload of a
    `wiki-admission` record event (or None). Returns the graph effects of `Writer.withdraw_source`
    and `dependents`: [{candidate_id, from, to, derived}]."""
    from . import lifecycle
    done = writer.withdraw_source(source_ref, reason=reason, requested_by=requested_by,
                                  record_event_id=record_event_id)
    done["dependents"] = []
    for dep in dependent_candidates(writer.db, done["refs"]):
        # the one transition table (lifecycle.py, t-070 RC3): checked -> stale, admitted -> retracted
        decided = lifecycle.source_withdrawn(dep)
        if not decided.go:
            continue
        to = decided.to
        derived: dict = {}
        if to == "retracted":
            admission = admission_of(dep["admission_event_id"]) if dep["admission_event_id"] else None
            # A claim on the withdrawn source was settled in steps 2 and 4; when another anchor of
            # the candidate is the one withdrawn, its net effect is undone here (architecture.md 7.6).
            derived = undo_derived(writer.db, admission)
            effects = writer.withdraw_candidate_effects(dep["candidate_id"], admission)
            if effects.get("claim"):
                derived["effects"] = effects
        writer.set_candidate_status(dep["candidate_id"], to)
        done["dependents"].append({"candidate_id": dep["candidate_id"], "from": dep["status"],
                                   "to": to, "derived": derived})
    return done
