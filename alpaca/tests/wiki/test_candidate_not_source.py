"""op-008 t-066: a candidate is never a source (design/rune2-memory/architecture.md 7.5, 9.3; FR-20;
`NOT_SOURCES` and `renders_as_source` of alpaca/wiki/ingest/drain.py).

The record events Rune2 appends about its own work are not rendered as notes, are not in the expected
event sets of `status` and `coverage`, and their quotes never come back through a query as evidence.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import appender, capture, events, fact_event, fact_proposal, store  # noqa: E402

from alpaca.wiki import service                              # noqa: E402
from alpaca.wiki.ingest import drain                         # noqa: E402

#: `transcript-version` (t-430): the version a capture landed, the store's own bookkeeping.
OWN = ("wiki-candidate", "wiki-admission", "wiki-candidate-withdrawn", "wiki-withdrawal",
       "capture-quarantined", "rune2-consolidate-failure", "transcript-version")


def test_the_own_work_kinds_are_not_sources():
    assert drain.NOT_SOURCES == frozenset(OWN)
    for kind in OWN:
        assert drain.renders_as_source(kind, {}) is False
    for kind in ("result", "intent", "task-move", "proof-report", "capture-deferred", "msg"):
        assert drain.renders_as_source(kind, {}) is True


def test_a_correction_is_a_source_only_when_the_owner_applied_it():
    assert drain.renders_as_source("wiki-correction", {"result": "applied", "actor": "owner"}) is True
    assert drain.renders_as_source("wiki-correction", {"result": "applied", "actor": "agent"}) is False
    assert drain.renders_as_source("wiki-correction", {"result": "refused", "actor": "owner"}) is False
    assert drain.renders_as_source("wiki-correction", None) is False


def test_the_drain_renders_no_note_for_a_candidate_event(project):
    eid = fact_event(project)
    service.propose(project, fact_proposal(project, eid), session="s-agent",
                    record=appender(project))
    capture(project)                                    # every record event, the candidate included
    (candidate,) = [e for e in events(project) if e["kind"] == "wiki-candidate"]
    with store(project) as conn:
        notes = [r[0] for r in conn.execute(
            "SELECT text FROM blocks WHERE ordinal=0 AND text LIKE 'op: %'")]
    assert not any(("event: %d\n" % candidate["id"]) in n + "\n" for n in notes)
    assert any(("event: %d\n" % eid) in n + "\n" for n in notes)


def test_status_and_coverage_leave_a_candidate_event_out_of_the_expected_set(project):
    eid = fact_event(project)
    before = service.status(project)["capture"]
    service.propose(project, fact_proposal(project, eid), session="s-agent",
                    record=appender(project))
    after = service.status(project)["capture"]
    assert after["source_events"] == before["source_events"]
    assert after["missing_events"] == 0 and after["captured_events"] == before["captured_events"]
    assert after["source_event_head"] == before["source_event_head"]
    result = service.query(project, "Task t-066 is blocked", filters=None)
    assert result["coverage"]["project_missing_events"] == 0


def test_a_candidate_quote_is_never_evidence_for_itself(project):
    eid = fact_event(project, "Task t-066 is blocked on the review of dec-38940e1fb35f.")
    body = fact_proposal(project, eid, quote="Task t-066 is blocked on the review")
    body["context"] = [{"source_ref": "transcript:s-later", "quote": "zebra-unique-sentinel-text"}]
    service.propose(project, body, session="s-agent", record=appender(project))
    capture(project)
    result = service.query(project, "zebra-unique-sentinel-text")
    assert result["outcome"] != "grounded" and "zebra-unique-sentinel-text" not in (
        result["answer_text"] or "")
    assert all("wiki-candidate" not in str(c) for c in result["citations"])
