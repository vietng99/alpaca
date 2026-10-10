"""Shared setup for the op-008 native Rune2 tests (t-062 on): a real project record, captured
through the real drain, answered through the real service. No provider or door is mocked here;
a test that injects a fault does so itself and says so."""
from __future__ import annotations

import shutil
import socket
from contextlib import contextmanager
from pathlib import Path

from alpaca import db as record

CONFIG_A = ("wiki_providers:\n  reranker: identity\n  entailer: structural\n"
            "  meta:\n    retrieval_profile: narrow\n")
CONFIG_B = ("wiki_providers:\n  reranker: lexical\n  entailer: ensemble\n"
            "  meta:\n    retrieval_profile: hybrid\n")


def append(root, kind, data, *, session="s-native", op=None, ref=None, actor="agent") -> int:
    conn = record.connect(str(root))
    try:
        return record.append_event(conn, session=session, actor=actor, kind=kind, op=op, ref=ref,
                                   data=data)["id"]
    finally:
        conn.close()


def events(root, ids=None) -> list[dict]:
    conn = record.connect_readonly(str(root))
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id")]
    finally:
        conn.close()
    return [r for r in rows if ids is None or r["id"] in ids]


def event_hash(root, event_id: int) -> str:
    return next(r["hash"] for r in events(root) if r["id"] == event_id)


def capture(root, ids=None) -> dict:
    """Capture record events through the existing drain entry (service.capture arrives in t-065)."""
    from alpaca.wiki.ingest import drain
    return drain.capture(str(root), events(root, ids))


def ingest(root, text: str, doc_id: str, name: str = "source.md") -> dict:
    from alpaca.wiki import service
    page = Path(root, name)
    page.write_text(text, encoding="utf-8")
    return service.ingest(str(root), page, doc_id=doc_id)


def vault(root) -> Path:
    return Path(root, ".alpaca", "wiki")


def set_config(root, text: str) -> None:
    Path(root, "project.yaml").write_text(text, encoding="utf-8")


def reset_vault(root, keep_as: str | None = None) -> None:
    """Move the Rune2 store aside (or drop it) so a rebuild starts empty; the record stays."""
    v = vault(root)
    if keep_as:
        shutil.move(str(v), str(Path(root, keep_as)))
    else:
        shutil.rmtree(v, ignore_errors=True)


@contextmanager
def store(root):
    """A writable connection to the project's Rune2 store, for test setup rows only (an exclusions
    or source_versions row before the Writer methods that write them exist)."""
    from alpaca.wiki.config import Config
    from alpaca.wiki.store.db import DB
    db = DB(Config.for_project(str(root)))
    try:
        db.pour()
        yield db.conn
        db.conn.commit()
    finally:
        db.close()


@contextmanager
def sockets_blocked():
    """K1: any socket creation raises while the block is active."""
    real = socket.socket

    class _Blocked(socket.socket):
        def __init__(self, *args, **kwargs):
            raise AssertionError("network access attempted on a Rune2 path")

    socket.socket = _Blocked
    real_create = socket.create_connection
    socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("network access attempted on a Rune2 path"))
    try:
        yield
    finally:
        socket.socket = real
        socket.create_connection = real_create


# ---------------------------------------------------------------- candidate intake (op-008 t-066)
def appender(root, session="s-agent"):
    """The RecordAppender the CLI binds (alpaca/cli.py `_wiki_record_appender`): the record's own
    append_event as actor `alpaca-wiki`, returning the event id."""
    def append_wiki(kind, data):
        conn = record.connect(str(root))
        try:
            return record.append_event(conn, session=session, actor="alpaca-wiki", kind=kind,
                                       data=data)["id"]
        finally:
            conn.close()
    return append_wiki


FACT_TEXT = "Task t-066 is blocked on the review of dec-38940e1fb35f since 2026-10-02."


def fact_event(root, body=FACT_TEXT, *, op="op-8", ref="t-066", session="s-native") -> int:
    """A captured `result` event whose note holds `body`; returns its record id."""
    eid = append(root, "result", {"body": body}, op=op, ref=ref, session=session)
    capture(root, {eid})
    return eid


def fact_proposal(root, eid, *, quote="Task t-066 is blocked", obj="blocked", subject="t-066",
                  valid_from=None, predicate="status", context=(), **extra) -> dict:
    """A well-formed fact proposal anchored on event `eid` (its record hash copied in)."""
    fact = {"subject": subject, "predicate": predicate, "obj": obj, "obj_datatype": "string"}
    if valid_from:
        fact["valid_from"] = valid_from
    out = {"kind": "fact", "fact": fact,
           "anchors": [{"source_ref": "event:%d" % eid, "quote": quote,
                        "hash": event_hash(root, eid)}], "context": list(context)}
    out.update(extra)
    return out


# ---------------------------------------------------------------- fact admission (op-008 t-066 s2)
import json  # noqa: E402

from alpaca.wiki import service  # noqa: E402
from alpaca.wiki.clock import FixedClock  # noqa: E402

T09, T10, T11 = ("2026-03-01T%02d:00:00+00:00" % h for h in (9, 10, 11))


def at(project, kind, data, ts, *, session="s-native", op="op-8", ref="t-9", actor="agent"):
    conn = record.connect(str(project))
    try:
        return record.append_event(conn, session=session, actor=actor, kind=kind, op=op, ref=ref,
                                   data=data, clock=FixedClock(ts))["id"]
    finally:
        conn.close()


def note(project, body, ts=T10, **kw):
    """A captured `result` event whose note holds `body`, stamped `ts`."""
    eid = at(project, "result", {"body": body}, ts, **kw)
    capture(project, {eid})
    return eid


def proposal(project, eid, quote, *, subject="t-9", predicate="status", obj="blocked",
             datatype="string", valid_from=None):
    row = next(e for e in events(project) if e["id"] == eid)
    fact = {"subject": subject, "predicate": predicate, "obj": obj, "obj_datatype": datatype}
    if valid_from:
        fact["valid_from"] = valid_from
    return {"kind": "fact", "fact": fact,
            "anchors": [{"source_ref": "event:%d" % eid, "quote": quote, "hash": row["hash"]}]}


def propose(project, body, session="s-agent"):
    return service.propose(project, body, session=session, record=appender(project, session))


def admit(project, cid, session="s-admin", probe=None):
    return service.admit(project, cid, session=session, record=appender(project, session),
                         probe=probe)


def withdraw(project, cid, session="s-agent", reason="wrong", by=None):
    return service.withdraw_candidate(project, cid, session=session,
                                      record=appender(project, session), reason=reason, by=by)


def edges(project, **where):
    with store(project) as conn:
        sql = "SELECT * FROM edges"
        if where:
            sql += " WHERE " + " AND ".join("%s=?" % k for k in where)
        return [dict(r) for r in conn.execute(sql + " ORDER BY edge_id", tuple(where.values()))]


def claims(project, edge_id=None):
    with store(project) as conn:
        sql, args = "SELECT * FROM edge_origin", ()
        if edge_id:
            sql, args = sql + " WHERE edge_id=?", (edge_id,)
        return [dict(r) for r in conn.execute(sql + " ORDER BY claim_ref", args)]


def corroborators(project, edge_id):
    with store(project) as conn:
        return [r[0] for r in conn.execute(
            "SELECT source_block_id FROM edge_corroborations WHERE edge_id=? ORDER BY 1", (edge_id,))]


def row_of(project, cid):
    with store(project) as conn:
        return dict(conn.execute("SELECT * FROM candidates WHERE candidate_id=?", (cid,)).fetchone())


def llm_edges(project):
    return [e for e in edges(project) if e["extractor"] == "llm"]


def wiki_events(project, kind):
    return [dict(e, data=json.loads(e["data"])) for e in events(project) if e["kind"] == kind]


def doc_like(eid):
    return "raw/%%/%06d-%%" % eid


def unknown_time(project, eid, proxy=T09):
    """Make an event note look like one captured before t-065: no source time, only a capture-time
    `valid_from` (the proxy)."""
    with store(project) as conn:
        conn.execute("UPDATE blocks SET source_created_at=NULL, valid_from=? WHERE doc_id IN "
                     "(SELECT doc_id FROM docs WHERE doc_id LIKE ?)", (proxy, doc_like(eid)))
        conn.execute("UPDATE docs SET source_created_at=NULL WHERE doc_id LIKE ?",
                     (doc_like(eid),))




def rebuild_store(project, session="s-recover") -> dict:
    """Drop the Rune2 store and rebuild it from the record: the real `wiki recover` path, which
    replays the candidate acts in record id order."""
    from alpaca.wiki import recovery
    reset_vault(project)
    return recovery.run(project, session=session)


def digest_of(project) -> str:
    from alpaca.wiki.digest import accepted_digest
    with store(project) as conn:
        return accepted_digest(conn)


def candidate_rows(project) -> list:
    """The candidate rows and their anchors, without the clock columns a rebuild sets itself."""
    with store(project) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM candidates ORDER BY candidate_id")]
        anchors = [dict(r) for r in conn.execute(
            "SELECT * FROM candidate_anchors ORDER BY candidate_id, ordinal")]
    for r in rows:
        r.pop("updated_at", None)
    return [rows, anchors]
