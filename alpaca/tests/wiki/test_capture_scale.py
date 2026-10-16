"""Merge of op-008 into main (2026-10-06), review blockers B1 and B3: what the merged capture did on the
live-size store (565,000 active blocks), pinned on small stores.

B3: the echo index (every active block) was read inside the absorber's write transaction, so on the
live store the wiki write lock was held about 30 s and every other writer (busy timeout 5 s) failed
with `database is locked`. It is read before the lock now, with the detector seeing the store as a
read under the lock would.

B1: the collector's wiki consumer ran each batch under a 45 s deadline with a fresh absorber, so the
30-37 s index read was paid per batch and every batch deferred. The consumer has no deadline now, the
collector lends one absorber to all its captures (index read once per process, again only after
another connection commits), a collector capture always lands a document before its deadline is
checked, and the index keeps one string per token.
"""
import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import append, events, store, vault  # noqa: E402

from alpaca import db as record                           # noqa: E402
from alpaca.wiki import service                           # noqa: E402
from alpaca.wiki.config import Config                     # noqa: E402
from alpaca.wiki.engine.echo import EchoIndex             # noqa: E402
from alpaca.wiki.ingest.absorb import Absorber            # noqa: E402

BODY = "The lantern rota moved to Tuesday at the north gate."


@pytest.fixture(autouse=True)
def _no_shared_absorber():
    """Each test starts and ends without the collector's shared absorber (process state)."""
    from alpaca.observability import consumers
    shared = getattr(consumers, "_SHARED", None)
    if shared is not None:
        shared.close()
    yield
    if shared is not None:
        shared.close()


def _seed(project, n=3, session="s-seed"):
    """A store that exists, with a few landed notes."""
    ids = [append(project, "result", {"body": "seed note %d about the harbour" % i}, session=session)
           for i in range(n)]
    service.capture(project, events=events(project, set(ids)), deadline_s=None)
    return ids


def _note_blocks(project, event_id):
    with store(project) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT b.block_id, b.text, p.relation, p.cites_block_id FROM blocks b "
            "LEFT JOIN block_provenance p ON p.block_id=b.block_id "
            "WHERE b.doc_id LIKE ? AND b.status='active' ORDER BY b.ordinal",
            ("raw/%%/%06d-%%" % event_id,))]


def _count_loads(monkeypatch, after=None):
    """Count `EchoIndex.load`; `after(index)` runs once the original load has returned."""
    loads = []
    original = EchoIndex.load

    def load(self, rows):
        original(self, rows)
        loads.append(self)
        if after is not None:
            after(self)
    monkeypatch.setattr(EchoIndex, "load", load)
    return loads


# ------------------------------------------------------------------ B3: the write lock
def test_the_echo_index_is_read_before_the_wiki_write_lock_is_taken(project, monkeypatch):
    _seed(project)
    db_path = str(vault(project) / "rune.db")
    taken = []

    def other_writer(index):
        # A hook's absorber wanting the lock while the index is being read: it must get it at once.
        conn = sqlite3.connect(db_path, timeout=0, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            taken.append(True)
        except sqlite3.OperationalError as exc:
            taken.append(str(exc))
        finally:
            conn.close()
    loads = _count_loads(monkeypatch, after=other_writer)
    new = append(project, "result", {"body": BODY}, session="s-new")
    report = service.capture(project, events=events(project, {new}), deadline_s=None)
    assert report["notes"] == 1 and report["deferred"] is None
    assert len(loads) == 1
    assert taken == [True]                    # was ['database is locked'] with the read under the lock


def test_a_commit_between_the_index_read_and_the_lock_is_seen_by_the_detector(project, monkeypatch):
    # The read before the lock must not change a verdict: a block another connection commits after
    # the read and before the lock is in the index the detector uses, as a read under the lock had it.
    _seed(project)
    cfg = Config.for_project(project)
    external_text = "result during session s-new. body: " + BODY
    state = {"committed": False}

    def commit_between(index):
        if state["committed"]:
            return                                 # the external absorber's own read, and the re-read
        state["committed"] = True
        other = Absorber(cfg)
        try:
            other.absorb_text("raw/external/witness.md", external_text)
        finally:
            other.close()
    _count_loads(monkeypatch, after=commit_between)
    new = append(project, "result", {"body": BODY}, session="s-new")
    report = service.capture(project, events=events(project, {new}), deadline_s=None)
    assert report["notes"] == 1 and state["committed"]
    with store(project) as conn:
        external = [r[0] for r in conn.execute(
            "SELECT block_id FROM blocks WHERE doc_id='raw/external/witness.md' AND status='active'")]
    body = [b for b in _note_blocks(project, new) if b["text"] == external_text]
    assert len(body) == 1 and len(external) == 1
    assert (body[0]["relation"], body[0]["cites_block_id"]) == ("quotes", external[0])


# ------------------------------------------------------------------ B1: the collector
def _consume_wiki(project, conn, monkeypatch):
    from alpaca.observability import consumers
    monkeypatch.setattr(consumers, "CONSUMERS", {"wiki": consumers._wiki})
    return consumers.consume(project, conn)["wiki"]


def test_the_wiki_consumer_reads_the_echo_index_once_across_batches(project, monkeypatch):
    _seed(project)
    built = []
    original_init = Absorber.__init__

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        built.append(self)
    monkeypatch.setattr(Absorber, "__init__", init)
    loads = _count_loads(monkeypatch)
    conn = record.connect(project)
    try:
        first = append(project, "result", {"body": BODY}, session="s-one")
        assert _consume_wiki(project, conn, monkeypatch) == {"status": "ok", "through_event": first}
        second = append(project, "result", {"body": BODY}, session="s-one")
        assert _consume_wiki(project, conn, monkeypatch) == {"status": "ok", "through_event": second}
    finally:
        conn.close()
    assert len(built) == 1 and len(loads) == 1           # was two of each: one per batch
    # The index the second batch used was kept current by the first batch's writes.
    one = [b for b in _note_blocks(project, first) if "body:" in b["text"]]
    two = [b for b in _note_blocks(project, second) if "body:" in b["text"]]
    assert (two[0]["relation"], two[0]["cites_block_id"]) == ("quotes", one[0]["block_id"])


def test_the_wiki_consumer_reads_the_index_again_after_another_writer_commits(project, monkeypatch):
    _seed(project)
    loads = _count_loads(monkeypatch)
    conn = record.connect(project)
    try:
        append(project, "result", {"body": "a first batch about the ferry"}, session="s-one")
        assert _consume_wiki(project, conn, monkeypatch)["status"] == "ok"
        assert len(loads) == 1
        external_text = "result during session s-two. body: " + BODY
        other = Absorber(Config.for_project(project))
        try:
            other.absorb_text("raw/external/witness.md", external_text)
        finally:
            other.close()
        assert len(loads) == 2                                # the other writer's own read
        second = append(project, "result", {"body": BODY}, session="s-two")
        assert _consume_wiki(project, conn, monkeypatch)["status"] == "ok"
    finally:
        conn.close()
    assert len(loads) == 3                                    # read again after the external commit
    with store(project) as db:
        external = db.execute("SELECT block_id FROM blocks WHERE doc_id='raw/external/witness.md'"
                              ).fetchone()[0]
    body = [b for b in _note_blocks(project, second) if b["text"] == external_text]
    assert (body[0]["relation"], body[0]["cites_block_id"]) == ("quotes", external)


def test_a_slow_index_read_does_not_defer_the_wiki_consumer(project, monkeypatch):
    # The live shape of B1: the index read eats the collector's capture budget. Scaled down here to
    # a 0.5 s budget and a 1 s read; the wiki consumer has no budget now, as on main.
    from alpaca.observability import consumers
    _seed(project)
    monkeypatch.setattr(consumers, "CAPTURE_DEADLINE_S", 0.5)
    _count_loads(monkeypatch, after=lambda index: time.sleep(1.0))
    conn = record.connect(project)
    try:
        ids = [append(project, "result", {"body": "batch note %d" % i}, session="s-slow")
               for i in range(4)]
        result = _consume_wiki(project, conn, monkeypatch)
    finally:
        conn.close()
    assert result == {"status": "ok", "through_event": ids[-1]}   # was failed: RuntimeError
    assert all(_note_blocks(project, i) for i in ids)


def test_a_collector_capture_lands_a_document_before_its_deadline_is_checked(project):
    _seed(project)
    ids = [append(project, "result", {"body": "late note %d" % i}, session="s-late") for i in range(3)]
    rows = events(project, set(ids))
    hook = service.capture(project, events=rows, deadline_s=0)          # a hook: unchanged
    assert hook["notes"] == 0 and hook["deferred"] == {"through_event": 0, "remaining": 3}
    collector = service.capture(project, events=rows, deadline_s=0, min_documents=1)
    assert collector["notes"] == 1
    assert collector["deferred"] == {"through_event": ids[0], "remaining": 2}


def test_a_snapshot_job_lands_its_transcript_past_its_budget(project, monkeypatch):
    import json
    from alpaca import observability
    from alpaca.observability import consumers
    _seed(project)
    native = os.path.join(project, "native.jsonl")
    with open(native, "w", encoding="utf-8") as out:
        out.write(json.dumps({"type": "user", "message": {"content": "a transcript line"}}) + "\n")
    observability.expect_source(project, "s-job", "claude", locator=native)
    monkeypatch.setattr(consumers, "CAPTURE_DEADLINE_S", 0.0)
    conn = record.connect(project)
    try:
        consumers.queue_snapshot(conn, "s-job")
        assert consumers.run_snapshot_jobs(project, conn) == {"completed": 1, "failed": 0}
    finally:
        conn.close()


def test_the_shared_absorber_is_replaced_when_its_store_or_root_changes(project):
    from alpaca.observability import consumers
    _seed(project)
    root = os.path.realpath(project)
    first = consumers._SHARED.lend(root)
    assert consumers._SHARED.lend(root) is first
    first.db.conn.execute("BEGIN")                 # a connection left inside a transaction
    consumers._SHARED.settle()
    second = consumers._SHARED.lend(root)
    assert second is not first and first.db._closed
    for name in ("rune.db", "rune.db-wal", "rune.db-shm"):
        path = vault(project) / name
        if path.exists():
            path.unlink()                          # the store file replaced under the absorber
    third = consumers._SHARED.lend(root)
    assert third is not second and second.db._closed


# ------------------------------------------------------------------ index size
def test_the_echo_index_keeps_one_string_per_token():
    # t-685: a block holds its tokens as ids into one vocabulary, so each token is one string
    index = EchoIndex()
    index.load([("a", "the lantern rota moved", "d1"), ("b", "lantern rota again", "d2")])
    index.add("c", "rota lantern twice", "d3")
    held = {block: set(index._tok[index._slot[block]]) for block in "abc"}
    for token in ("lantern", "rota"):
        assert all(index._vocab[token] in held[block] for block in "abc")
    assert len(index._vocab) == len(set("the lantern rota moved again twice".split()))
    assert index.detect("the lantern rota moved", exclude_doc_id="d9") == ("quotes", "a", None)
