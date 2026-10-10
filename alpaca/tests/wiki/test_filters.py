"""op-008 t-065 stage s4-answers: structured filters and the bookkeeping default (FR-11;
design/rune2-memory/architecture.md 6.1, 6.2; requirements.md 6.2 item 4, dec-5ea0e31009b7).

`service.query` and `service.context` take `Filters`: op, task, operator, session, source kind and
fact kind narrow what every retrieval arm reads, and a default read leaves heartbeat notes out
(`include_bookkeeping` reads them). A filter narrows and never authorizes: a source that fails the
servable predicate stays out whatever the filter says. A filter value that cannot apply is
`unsupported`, not an empty store. Real drain, real door, no mock.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import append, capture, store  # noqa: E402

from alpaca.wiki import service                      # noqa: E402
from alpaca.wiki.api import Filters                  # noqa: E402
from alpaca.wiki.clock import FixedClock             # noqa: E402
from alpaca.wiki.config import Config                # noqa: E402
from alpaca.wiki.ingest.absorb import Absorber       # noqa: E402
from alpaca.wiki.store import filters as store_filters  # noqa: E402

QUESTION = "kestrel pier handles shipments"


def _fact(project, word, *, op, ref, session, kind="result"):
    return append(project, kind, {"body": "The kestrel pier handles %s shipments." % word},
                  session=session, op=op, ref=ref)


def _two_ops(project):
    """Two sessions, two operators, two ops and tasks, one event each, words that differ."""
    append(project, "session-start", {"operator": "claude"}, session="s-a", op="op-1")
    append(project, "session-start", {"operator": "codex"}, session="s-b", op="op-2")
    night = _fact(project, "night", op="op-1", ref="t-1", session="s-a")
    day = _fact(project, "day", op="op-2", ref="t-2", session="s-b")
    capture(project)
    return night, day


def _source_refs(result):
    return [s["source_ref"] for s in result["sources"]]


def test_the_clause_is_exact_and_empty_filters_only_leave_out_bookkeeping():
    clause, params = store_filters.block_clause(Filters(include_bookkeeping=True), "b")
    assert (clause, params) == ("1", {})
    default, params = store_filters.block_clause(None, "b")
    assert "NOT IN" in default and params == {"flt_bk0": "\nkind: heartbeat\n"}
    clause, params = store_filters.block_clause(Filters(op="op-1", session=" s  a "), "blocks")
    assert params["flt_op"] == "\nop: op-1\n" and params["flt_session"] == "\nsession: s a\n"
    assert clause.count("blocks.doc_id IN") == 1


def test_each_filter_narrows_to_its_own_notes(project):
    night, day = _two_ops(project)
    both = service.query(project, QUESTION)
    assert both["outcome"] == "grounded"
    for filters, event, other in (
            (Filters(op="op-1"), night, "day"), (Filters(op="op-2"), day, "night"),
            (Filters(task="t-1"), night, "day"), (Filters(task="t-2"), day, "night"),
            (Filters(session="s-a"), night, "day"), (Filters(session="s-b"), day, "night"),
            (Filters(operator="claude"), night, "day"), (Filters(operator="codex"), day, "night"),
            (Filters(source_kind="result", op="op-2"), day, "night")):
        result = service.query(project, QUESTION, filters=filters)
        assert result["outcome"] == "grounded", filters
        assert _source_refs(result) == ["event:%d" % event], filters
        assert other not in result["answer_text"], filters
    # a scope that holds no note answers nothing, it does not fall back to the whole store
    empty = service.query(project, QUESTION, filters=Filters(op="op-3"))
    assert empty["outcome"] == "empty" and empty["answer_text"] is None


def test_a_filter_matches_the_whole_value(project):
    """`op: op-1` is not `op: op-10`, and a session id is not a prefix match."""
    append(project, "result", {"body": "The kestrel pier handles tenfold shipments."},
           session="s-10", op="op-10", ref="t-10")
    capture(project)
    assert service.query(project, QUESTION, filters=Filters(op="op-1"))["outcome"] == "empty"
    assert service.query(project, QUESTION, filters=Filters(session="s-1"))["outcome"] == "empty"
    assert service.query(project, QUESTION, filters=Filters(task="t-1"))["outcome"] == "empty"
    assert service.query(project, QUESTION, filters=Filters(op="op-10"))["outcome"] == "grounded"


def test_source_kind_matches_an_event_kind_and_a_transcript(project):
    result_event = append(project, "result", {"body": "The kestrel pier handles result shipments."},
                          session="s-a", op="op-1", ref="t-1")
    note_event = append(project, "blocker", {"body": "The kestrel pier handles blocker shipments."},
                        session="s-a", op="op-1", ref="t-1")
    capture(project)
    cfg = Config.for_project(project)
    absorber = Absorber(cfg, clock=FixedClock(start="2026-03-01T00:00:00+00:00", step=0))
    try:
        absorber.absorb_text(
            "raw/transcripts/s-t/transcript-s-t.md",
            "role: transcript\nop: session\nsession: s-t\n\nThe kestrel pier handles transcript shipments.\n",
            kind="raw")
    finally:
        absorber.close()
    by_kind = {kind: service.query(project, QUESTION, filters=Filters(source_kind=kind))
               for kind in ("result", "blocker", "transcript")}
    assert _source_refs(by_kind["result"]) == ["event:%d" % result_event]
    assert _source_refs(by_kind["blocker"]) == ["event:%d" % note_event]
    assert _source_refs(by_kind["transcript"]) == ["transcript:s-t"]
    assert "transcript" in by_kind["transcript"]["answer_text"]


def test_default_read_leaves_heartbeat_notes_out(project):
    beat = append(project, "heartbeat", {"body": "The kestrel pier handles beat shipments."},
                  session="s-a", op="op-1")
    capture(project)
    default = service.query(project, QUESTION)
    assert default["outcome"] == "empty" and default["answer_text"] is None
    with_beats = service.query(project, QUESTION, filters=Filters(include_bookkeeping=True))
    assert with_beats["outcome"] == "grounded"
    assert _source_refs(with_beats) == ["event:%d" % beat]
    # the note stays in the store as evidence: the filter only leaves it out of default retrieval
    assert service.status(project)["capture"]["captured_events"] == 1
    # a kind filter names the kind explicitly, and the heartbeat default still applies to it
    assert service.query(project, QUESTION, filters=Filters(source_kind="heartbeat"))[
        "outcome"] == "empty"
    assert service.query(project, QUESTION, filters=Filters(
        source_kind="heartbeat", include_bookkeeping=True))["outcome"] == "grounded"


def test_a_narrow_filter_ranks_inside_its_scope(project):
    """The filter is in the query, not applied to a global top-k: fifty-odd notes of another op that
    rank higher cannot push the one note of the scope out of the candidates."""
    for n in range(60):
        append(project, "result",
               {"body": "kestrel pier handles shipments; kestrel pier handles shipments again, "
                        "kestrel pier handles shipments daily"},
               session="s-big", op="op-big", ref="t-big-%d" % n)
    lone = append(project, "result", {"body": "The kestrel pier handles lone shipments."},
                  session="s-lone", op="op-lone", ref="t-lone")
    capture(project)
    scoped = service.query(project, QUESTION, filters=Filters(op="op-lone"))
    assert scoped["outcome"] == "grounded"
    assert _source_refs(scoped) == ["event:%d" % lone]


def test_fact_kind_keeps_blocks_that_anchor_such_a_fact(project):
    append(project, "task-move", {"from": "doing", "to": "review"}, session="s-a", op="op-1",
           ref="t-77")
    append(project, "result", {"body": "t-77 review notes: the kestrel pier is not involved."},
           session="s-a", op="op-1", ref="t-77")
    capture(project)
    with store(project) as conn:
        anchors = {r[0] for r in conn.execute(
            "SELECT DISTINCT e.source_block_id FROM edges e JOIN edge_origin o ON o.edge_id=e.edge_id "
            "WHERE o.fact_kind='record-fact' AND o.status='live'")}
    assert anchors, "setup: the record rules placed a record-fact edge"
    plain = service.query(project, "t-77 review notes kestrel pier")
    assert plain["outcome"] in ("grounded", "abstained")
    for kind, expected in (("record-fact", True), ("agent-experience", False),
                           ("derived-hypothesis", False)):
        result = service.query(project, "t-77 doing review", filters=Filters(fact_kind=kind))
        cited = {c["source_block_id"] for c in result["citations"]}
        sources = {s["source_block_id"] for s in result["sources"]}
        assert bool(sources) is expected, kind
        assert sources <= anchors and cited <= anchors, kind


def test_filters_never_authorize(project):
    """A note a filter matches is still refused when it fails the servable predicate."""
    event = append(project, "result", {"body": "The kestrel pier handles secret shipments."},
                   session="s-a", op="op-1", ref="t-1")
    capture(project)
    with store(project) as conn:
        conn.execute("UPDATE docs SET private=1 WHERE doc_id IN "
                     "(SELECT doc_id FROM blocks WHERE text LIKE ?)", ("%%event: %d%%" % event,))
    result = service.query(project, QUESTION, filters=Filters(op="op-1", source_kind="result"))
    assert (result["outcome"], result["reason"]) == ("refused", "not-servable")
    assert result["answer_text"] is None and result["sources"] == []
    assert "secret" not in service.context(project, QUESTION, filters=Filters(op="op-1"))


@pytest.mark.parametrize("filters", [Filters(fact_kind="guess"), Filters(op=" "),
                                     Filters(source_kind=""), Filters(session=7)])
def test_a_value_that_cannot_apply_is_unsupported(project, filters):
    _two_ops(project)
    result = service.query(project, QUESTION, filters=filters)
    assert (result["outcome"], result["reason"]) == ("unsupported", "filter-invalid")
    assert result["answer_text"] is None and result["sources"] == []


def test_a_scope_filter_names_the_coverage_scope(project):
    """A filter on op, task or session scopes the coverage like the same id in the question."""
    _two_ops(project)
    gap = append(project, "result", {"body": "uncaptured op-1 result"}, session="s-a", op="op-1",
                 ref="t-1")
    result = service.query(project, "zebra quantum nebula", filters=Filters(op="op-1"))
    assert result["coverage"]["scope"]["op"] == "op-1"
    assert result["coverage"]["missing_event_ids_in_scope"] == [gap]
    assert (result["outcome"], result["reason"]) == ("partial", "scope-capture-gaps")
    other = service.query(project, "zebra quantum nebula", filters=Filters(op="op-2"))
    assert other["coverage"]["missing_event_ids_in_scope"] == []


def test_context_takes_the_same_filters(project):
    _two_ops(project)
    text = service.context(project, QUESTION, filters=Filters(op="op-2"))
    assert "day shipments" in text and "night shipments" not in text


def test_cli_flags_build_the_filters(project, capsys):
    import json
    from alpaca import cli
    _two_ops(project)
    assert cli.main(["wiki", "query", QUESTION, "--op", "op-1", "--source-kind", "result"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert "night shipments" in result["answer_text"] and "day" not in result["answer_text"]
    assert cli.main(["wiki", "context", QUESTION, "--filter-session", "s-b"]) == 0
    text = capsys.readouterr().out
    assert "day shipments" in text and "night shipments" not in text
    assert cli.main(["wiki", "query", QUESTION, "--fact-kind", "guess"]) == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "filter-invalid"


def test_the_bm25_query_keeps_the_fts_match_as_the_outer_loop(project):
    """The as-of clause on `blocks` must not let the planner walk the blocks index and probe the FTS
    per row (70 s on a 70,000-block store); the query pins the order with CROSS JOIN."""
    from alpaca.wiki.store import fts
    from alpaca.wiki.store.asof import AsOf
    from alpaca.wiki.store.db import DB
    _two_ops(project)
    db = DB(Config.for_project(project))
    seen = []
    try:
        db.pour()
        db.conn.set_trace_callback(seen.append)
        fts.search_bm25(db, QUESTION, 5, AsOf("2030-01-01T00:00:00+00:00"))
    finally:
        db.close()
    assert any("blocks_fts CROSS JOIN blocks" in sql for sql in seen)
