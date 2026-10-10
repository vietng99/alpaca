"""op-008 t-065 stage s6-latency: what one answer reads (bar L1, evaluation.md 5.4).

On the shipped narrow default `wiki context` took 1.88 times the baseline at p50 (1.73 at 16,000
characters) because the FR-11 default filter (heartbeat notes left out) was a subquery over every
first block of the store, run by each of the four reads of each retrieval pass, and the passes of
one answer re-read what the first pass had read. The cost is counted here in statements, not in
seconds: a statement count does not move with the load of the host. Nothing a filter or a visibility
check decides changes: the cached document list must select the same blocks as the subquery, and a
memo of one answer must give the same retrieval as none. Real drain, real store, no mock.
"""
import os
import re
import sys
from contextlib import closing

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import append, capture  # noqa: E402

from alpaca.wiki import service                              # noqa: E402
from alpaca.wiki.api import Filters                          # noqa: E402
from alpaca.wiki.config import Config                        # noqa: E402
from alpaca.wiki.engine import loop, retrieve                # noqa: E402
from alpaca.wiki.store import filters as store_filters, read  # noqa: E402
from alpaca.wiki.store.asof import AsOf                      # noqa: E402
from alpaca.wiki.store.db import DB                          # noqa: E402

QUESTION = "kestrel pier handles shipments"
FAR = "2099-01-01T00:00:00+00:00"
HEADER_SCAN = re.compile(r"h\.ordinal=0")
ASOF_BLOCKS = re.compile(r"blocks\.valid_from <= ")


def _store(project):
    append(project, "session-start", {"operator": "claude"}, session="s-a", op="op-1")
    for n in range(3):
        append(project, "heartbeat", {"body": "The kestrel pier handles beat %d shipments." % n},
                session="s-a", op="op-1")
    for n, (session, op) in enumerate((("s-a", "op-1"), ("s-b", "op-2"), ("s-b", "op-2"))):
        append(project, "result", {"body": "The kestrel pier handles result %d shipments." % n},
               session=session, op=op, ref="t-%d" % n)
    capture(project)


def _open(project):
    return DB(Config.for_project(str(project)))


class _Trace:
    """The statements one connection runs while the block is active."""

    def __init__(self, db):
        self.db, self.sql = db, []

    def __enter__(self):
        self.db.conn.set_trace_callback(self.sql.append)
        return self

    def __exit__(self, *exc):
        self.db.conn.set_trace_callback(None)

    def count(self, pattern):
        return sum(1 for s in self.sql if pattern.search(s))


def _reads(db, asof, filters=None):
    """The four reads one retrieval pass makes through the guarded surface."""
    return (read.asof_visible_blocks(db, asof, filters), read.bm25(db, QUESTION, 50, asof, filters),
            read.exact_floor(db, QUESTION, [], asof, filters),
            read.exact_floor(db, "kestrel pier handles result 0 shipments", [], asof, filters))


def test_header_documents_are_read_once_per_connection_state(project):
    _store(project)
    asof = AsOf(T=FAR)
    with closing(_open(project)) as db:
        with _Trace(db) as trace:
            first = _reads(db, asof)
            for _ in range(3):
                assert _reads(db, asof) == first
        assert trace.count(HEADER_SCAN) == 1, "twelve reads scanned the first blocks more than once"
        assert first[0], "setup: some block is visible"

        # another writer commits a heartbeat: the next read sees it and leaves it out
        append(project, "heartbeat", {"body": "The kestrel pier handles late beat shipments."},
               session="s-a", op="op-1")
        capture(project)
        with _Trace(db) as trace:
            after = _reads(db, asof)
        assert trace.count(HEADER_SCAN) == 1, "a commit by another connection must be read again"
        assert after[0] == first[0], "the new heartbeat note must stay out of the visible set"
        with _Trace(db) as trace:
            assert read.asof_visible_blocks(db, asof, Filters(include_bookkeeping=True)) > first[0]
        assert trace.count(HEADER_SCAN) == 0, "no header predicate: nothing to read"

        # inside an open transaction nothing is kept, so a rolled-back write leaves nothing behind
        db.conn.execute("BEGIN")
        with _Trace(db) as trace:
            read.asof_visible_blocks(db, asof)
            read.asof_visible_blocks(db, asof)
        db.conn.rollback()
        assert trace.count(HEADER_SCAN) == 2


FILTERS = (None, Filters(), Filters(include_bookkeeping=True), Filters(op="op-2"),
           Filters(session="s-b", include_bookkeeping=True), Filters(source_kind="result"),
           Filters(source_kind="heartbeat"), Filters(source_kind="heartbeat", include_bookkeeping=True),
           Filters(op="op-1", task="t-0"), Filters(operator="claude"), Filters(op="op-9"))


@pytest.mark.parametrize("filters", FILTERS, ids=repr)
def test_the_cached_clause_selects_the_same_blocks_as_the_subquery(project, filters):
    """The document list is the subquery's own rows, so no filter is weaker or stronger."""
    _store(project)
    with closing(_open(project)) as db:
        def blocks(with_db):
            clause, params = store_filters.block_clause(filters, "blocks", db if with_db else None)
            return {r["block_id"] for r in db.conn.execute(
                "SELECT block_id FROM blocks WHERE status='active' AND " + clause, params)}
        assert blocks(True) == blocks(False)
        if filters is None or not filters.include_bookkeeping:
            everything = {r[0] for r in db.conn.execute("SELECT block_id FROM blocks WHERE status='active'")}
            assert blocks(True) < everything, "the default filter must leave heartbeat blocks out"


def _gather(db, project, asof, memo, k=50):
    cfg = Config.for_project(str(project))
    return retrieve.gather(db, cfg.providers(), QUESTION, asof, k=k, skip_ppr=True,
                           use_vector=False, memo=memo)


def _shape(retr):
    return ([(h.block_id, sorted(h.channels), h.exact_floor) for h in retr.hits], retr.floor_ids,
            retr.linked_nodes, retr.channel_lists, retr.ladder, retr.excluded_ids)


def test_the_passes_of_one_answer_share_their_reads_and_get_the_same_retrieval(project):
    _store(project)
    asof = AsOf(T=FAR)
    with closing(_open(project)) as db:
        with _Trace(db) as trace:
            alone = [_shape(_gather(db, project, asof, None, k)) for k in (50, 100, 200)]
        alone_reads = trace.count(ASOF_BLOCKS)
        memo = {}
        with _Trace(db) as trace:
            shared = [_shape(_gather(db, project, asof, memo, k)) for k in (50, 100, 200)]
        shared_reads = trace.count(ASOF_BLOCKS)
        assert shared == alone, "a memo changed what a pass retrieved"
        assert alone_reads == 3 * shared_reads and shared_reads >= 1, (alone_reads, shared_reads)
        # a pass that differs in any input has its own entry: another as-of reads again
        with _Trace(db) as trace:
            _gather(db, project, AsOf(T="2026-03-01T00:00:00+00:00"), memo)
        assert trace.count(ASOF_BLOCKS) == shared_reads
        # and the memo never hands out a set a pass could change
        first = _gather(db, project, asof, memo)
        first.hits.clear()
        first.floor_ids.clear()
        assert _shape(_gather(db, project, asof, memo)) == alone[0]


def test_an_answer_reads_the_visible_set_once_across_its_passes(project, monkeypatch):
    _store(project)
    calls = {"gather": 0, "visible": 0}
    real_gather, real_visible = retrieve.gather, read.asof_visible_blocks

    def gather(*args, **kwargs):
        calls["gather"] += 1
        return real_gather(*args, **kwargs)

    def visible(*args, **kwargs):
        calls["visible"] += 1
        return real_visible(*args, **kwargs)

    monkeypatch.setattr(retrieve, "gather", gather)
    monkeypatch.setattr(read, "asof_visible_blocks", visible)
    with closing(loop.Engine(Config.for_project(str(project)))) as engine:
        answer = engine.answer("Which harbour handles quartz tonnage in winter?")
        assert answer.verdict == "abstained"
        assert calls["gather"] >= 2, "setup: an abstention walks every refinement pass"
        assert calls["visible"] == 1
        # the next answer of the same engine starts with an empty memo: a write in between is seen
        append(project, "result", {"body": "The harbour handles quartz tonnage in winter."},
               session="s-c", op="op-3", ref="t-7")
        capture(project)
        again = engine.answer("Which harbour handles quartz tonnage in winter?")
        assert again.verdict == "grounded", again.extra
        assert calls["visible"] == 2


def test_context_and_query_still_answer_through_the_cached_filter(project):
    """The service door end to end: a default read leaves heartbeats out, a bookkeeping read finds
    them, and a repeat of either returns the same text (bar Z8 on the cached path)."""
    _store(project)
    default = service.query(project, "kestrel pier handles beat shipments")
    assert default["outcome"] != "grounded" or "beat" not in (default["answer_text"] or "")
    with_beats = service.query(project, "kestrel pier handles beat shipments",
                               filters=Filters(include_bookkeeping=True))
    assert with_beats["outcome"] == "grounded" and "beat" in with_beats["answer_text"]
    text = service.context(project, QUESTION, max_chars=1200)
    assert service.context(project, QUESTION, max_chars=1200) == text
