"""op-008 t-069 stage s2-portable: rollback to the schema-3 code over a schema-4 store with an exclusion
present (design/rune2-memory/architecture.md 10.1, 10.4; FR-23; risk R5).

The schema-3 code is the real `DB.pour` with the canonical schema text cut before its schema-4 section
and version 3, as T/test_schema4.py pours it, and its read path is the door without the exclusion
filter (the servable predicate of 6.2 and its `exclusions` read arrived with schema 4). Rolling back
resets `schema_version`, ignores the new tables and deletes none of their rows; a withdrawn source
stays retired, so the old read path serves none of it; rolling forward again finds the accepted state
as it was. The limit of R5 is shown too: a capture by the old code, which has no exclusion skip, lands
a withdrawn note again, whether its `wiki recover` or its SessionEnd or PreCompact hook runs it, so
no rollback may run while an exclusion exists, which docs/operators.md says. Real record, real drain,
real store; the two stand-ins for the old code say so where they are used.
"""
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import (T09, T10, T11, admit, appender, at, capture, events, note,  # noqa: E402
                            proposal, propose, store)

from alpaca.wiki import service                                  # noqa: E402
from alpaca.wiki.config import Config                            # noqa: E402
from alpaca.wiki.digest import accepted_digest                   # noqa: E402
from alpaca.wiki.store import db as store_db                     # noqa: E402
from alpaca.wiki.store.db import DB, load_schema_sql             # noqa: E402

MARKER = "-- ------------------------------------------------------------------ schema 4"
#: The schema-4 tables of accepted state; `answer_outcomes`, the answer audit, grows with every read
#: and is left out of the comparisons.
SCHEMA4 = ("source_versions", "edge_origin", "candidates", "candidate_anchors", "exclusions",
           "corrections")
GONE = "The heron gateway kept the zebra crossing keys in the ledger."
KEPT = "The lantern rota moved to Tuesday."
REPO = Path(__file__).resolve().parents[3]


def schema3_code(m):
    """The schema-3 pour, which every store open runs: the schema text before its schema-4 section,
    and version 3 (as T/test_schema4.py makes it)."""
    text = load_schema_sql()
    assert MARKER in text
    m.setattr(store_db, "load_schema_sql", lambda: text.split(MARKER, 1)[0])
    m.setattr(store_db, "CURRENT_SCHEMA_VERSION", 3)


def pour(project, monkeypatch, version):
    """Open and pour the store with the schema-3 code or with the current code (version 4)."""
    with monkeypatch.context() as m:
        if version == 3:
            schema3_code(m)
        db = DB(Config.for_project(project))
        try:
            db.pour()
            return db.get_meta("schema_version")
        finally:
            db.close()


@contextmanager
def readonly(project):
    """The store read as it is, without the pour every store open of the current code runs."""
    conn = sqlite3.connect("file:%s?mode=ro" % Path(project, ".alpaca", "wiki", "rune.db"), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def state(project) -> tuple:
    """schema_version, the accepted digest and every row of the schema-4 tables."""
    with readonly(project) as conn:
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        rows = {t: [tuple(r) for r in conn.execute("SELECT * FROM %s ORDER BY rowid" % t)]
                for t in SCHEMA4}
        return version, accepted_digest(conn), rows


def old_read_path(m):
    """Stand-in for the schema-3 read path: its pour, and no exclusion filter. Only `exclusions`,
    `source_versions` and the hidden-document read feed the servable set, so emptying it leaves the
    door as the old code had it (it read active blocks and active edges, and nothing of schema 4)."""
    from alpaca.wiki import servable
    schema3_code(m)
    m.setattr(servable, "unservable_source_refs", lambda db: set())


def served(project, question) -> str:
    """What the answer and the context pack serve (the question itself is left out)."""
    answer = {k: v for k, v in service.query(project, question).items() if k != "question"}
    return json.dumps([answer, service.context(project, question)])


def withdraw_source(project, ref):
    return service.withdraw_source(project, ref, session="s-owner",
                                   record=appender(project, "s-owner"), reason="wrong source")


def build(project) -> int:
    note(project, KEPT, T09)
    gone = note(project, GONE, T10)
    cid = propose(project, proposal(project, gone, "The heron gateway kept the zebra crossing keys",
                                    subject="heron gateway", predicate="related_to",
                                    obj="zebra crossing keys")).candidate_id
    assert admit(project, cid).status == "applied"
    assert withdraw_source(project, "event:%d" % gone).status == "applied"
    at(project, "result", {"body": "A later note."}, T11)
    capture(project)
    return gone


def test_schema3_code_over_a_schema4_store_with_an_exclusion(project, monkeypatch):
    gone = build(project)
    version, live, rows = state(project)
    assert version == "4" and rows["exclusions"] and rows["candidates"] and rows["edge_origin"]
    question = "Where did the heron gateway keep the zebra crossing keys?"
    # roll back: the schema-3 pour resets schema_version, ignores the new tables, deletes no row
    assert pour(project, monkeypatch, 3) == "3"
    assert state(project) == ("3", live, rows)
    with readonly(project) as conn:
        retired = {r[0] for r in conn.execute(
            "SELECT b.status FROM blocks b JOIN source_versions v ON v.doc_id=b.doc_id "
            "WHERE v.source_ref=?", ("event:%d" % gone,))}
        facts = {r[0] for r in conn.execute("SELECT status FROM edges WHERE extractor='llm'")}
    assert retired == {"superseded"} and facts == {"retracted"}
    # the old read path, which knows no exclusion, serves none of the withdrawn source: its blocks
    # stay retired and its fact retracted, which is all the old code reads
    with monkeypatch.context() as m:
        old_read_path(m)
        assert "zebra" not in served(project, question)
        assert "Tuesday" in served(project, "When did the lantern rota move?")
    assert state(project)[0] == "3"
    # roll forward: the current pour finds the accepted state as it was, and filters again
    assert pour(project, monkeypatch, 4) == "4"
    assert state(project) == ("4", live, rows)
    assert "zebra" not in served(project, question)
    assert "Tuesday" in served(project, "When did the lantern rota move?")
    assert service.status(project)["capture"]["withdrawn_events"] == 1


@pytest.mark.parametrize("old_capture", ["session-end-or-pre-compact-hook", "wiki-recover"])
def test_old_code_capture_would_revive_a_withdrawn_note(project, monkeypatch, old_capture):
    """R5 (check round 1 adversarial B3): the capture of the schema-3 code has no exclusion skip, so
    every capture it runs can land a withdrawn note as active blocks again, and its old read path
    then serves it: its `wiki recover`, which replays the record, and also its SessionEnd and
    PreCompact hooks, which drain the whole session on every end or compaction (`drain.run(root,
    sid)`, alpaca/hooks/session_end.py:38-40 and alpaca/hooks/pre_compact.py:35 at 4d7a12df1), so a
    session that holds a withdrawn event brings it back with no operator command. The stand-in is
    the current capture with the exclusion skip removed; the check round ran the real schema-3
    SessionEnd body against a schema-4 store with the same result
    (.alpaca/proofs/op-008/t-069.logs/attacks/a7b_rollback_old_session_end.py). The current
    capture, the current hooks' drain and `wiki recover` leave the note retired; rolling forward
    serves it no more, whatever the old code landed, since the exclusion rows stay; and
    docs/operators.md says not to roll back while an exclusion exists, naming the hooks."""
    from alpaca.wiki import recovery
    from alpaca.wiki.ingest import drain
    from alpaca.wiki.ingest.absorb import Absorber
    gone = build(project)
    question = "Where did the heron gateway keep the zebra crossing keys?"
    (session,) = {e["session"] for e in events(project, {gone})}

    def active(project):
        with store(project) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM blocks b JOIN source_versions v ON v.doc_id=b.doc_id "
                "WHERE v.source_ref=? AND b.status='active'", ("event:%d" % gone,)).fetchone()[0]

    # the current code: a capture of the event, a drain of its whole session (what the hooks run)
    # and a recovery leave the withdrawn note retired
    drain.capture(project, events(project, {gone}))
    drain.run(project, session)
    assert recovery.run(project, session="s-recover")["status"] == "ok"
    assert active(project) == 0
    # stand-in for the schema-3 capture: the hook's drain of the session the withdrawn event belongs
    # to, or recovery's capture of the record, without the exclusion skip
    with monkeypatch.context() as m:
        m.setattr(Absorber, "_excluded", lambda self, *a, **k: False)
        if old_capture == "wiki-recover":
            drain.capture(project, events(project, {gone}))
        else:
            drain.run(project, session)
        assert active(project) > 0
        old_read_path(m)
        assert "zebra" in served(project, question)
    # roll forward: the current read path serves none of it, since the exclusion row stayed
    assert "zebra" not in served(project, question)
    assert "Tuesday" in served(project, "When did the lantern rota move?")
    operators = " ".join((REPO / "docs" / "operators.md").read_text(encoding="utf-8").split())
    assert "not roll back while an exclusion exists" in operators
    assert "the SessionEnd and PreCompact hooks, which drain the whole session" in operators
