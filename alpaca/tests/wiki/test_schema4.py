"""op-008 t-062: schema 4 adds tables only (design/rune2-memory/architecture.md 10.1).

Pouring the schema-4 code over a schema-3 store adds the seven new tables and their append-only
triggers, adds no column (so `_migrate_existing_tables` reports no migration) and runs no FTS
rebuild, which would otherwise run inside a hook; a second pour changes nothing.
"""
import sqlite3

import pytest

from alpaca.wiki.config import Config
from alpaca.wiki.store import db as store_db
from alpaca.wiki.store.db import DB, load_schema_sql

NEW_TABLES = {"source_versions", "edge_origin", "candidates", "candidate_anchors", "exclusions",
              "corrections", "answer_outcomes"}
MARKER = "-- ------------------------------------------------------------------ schema 4"


def _schema3_store(cfg, monkeypatch):
    """A store poured by the schema-3 code path: the real DB.pour with the canonical schema text cut
    before its schema-4 section and version 3, holding one document and block so an FTS rebuild
    would have work to do."""
    text = load_schema_sql()
    assert MARKER in text
    with monkeypatch.context() as m:
        m.setattr(store_db, "load_schema_sql", lambda: text.split(MARKER, 1)[0])
        m.setattr(store_db, "CURRENT_SCHEMA_VERSION", 3)
        db = DB(cfg)
        try:
            db.pour()
            db.conn.execute("INSERT INTO docs(doc_id,path,kind,content_sha256,private,"
                            "privacy_scanned) VALUES('wiki/a.md','wiki/a.md','wiki','0',0,1)")
            db.conn.execute("INSERT INTO blocks(block_id,block_content_id,doc_id,ordinal,text,"
                            "valid_from,recorded_at) VALUES('wiki/a.md#0','c','wiki/a.md',0,"
                            "'kestrel text','2020-01-01','2020-01-01')")
            db.conn.commit()
            assert db.get_meta("schema_version") == "3"
            return _tables(db.conn)
        finally:
            db.close()


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_pour_over_schema3_adds_tables_only(tmp_path, monkeypatch):
    cfg = Config.for_vault(tmp_path / "vault")
    cfg.vault_dir.mkdir()
    before = _schema3_store(cfg, monkeypatch)
    assert not (before & NEW_TABLES)
    migrated = []
    real = DB._migrate_existing_tables

    def spy(self):
        result = real(self)
        migrated.append(result)
        return result

    monkeypatch.setattr(DB, "_migrate_existing_tables", spy)
    db = DB(cfg)
    statements = []
    db.conn.set_trace_callback(statements.append)
    try:
        columns_before = {t: [r[1] for r in db.conn.execute(f"PRAGMA table_info({t})")]
                          for t in before if not t.startswith(("blocks_fts", "sqlite_"))}
        db.pour()
        after = _tables(db.conn)
        assert NEW_TABLES <= after
        assert migrated and not any(migrated)
        assert not any("'rebuild'" in s for s in statements)
        assert db.get_meta("schema_version") == "4"
        columns_after = {t: [r[1] for r in db.conn.execute(f"PRAGMA table_info({t})")]
                         for t in columns_before}
        assert columns_after == columns_before
        assert db.conn.execute("SELECT text FROM blocks").fetchone()[0] == "kestrel text"
        # idempotent: a second pour changes no table and adds no migration row
        master = db.conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall()
        db.pour()
        assert db.conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name"
                               ).fetchall() == master
        assert [r[0] for r in db.conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version")] == [3, 4]
    finally:
        db.conn.set_trace_callback(None)
        db.close()
    assert store_db.CURRENT_SCHEMA_VERSION == 4


ROWS = {
    "source_versions": "INSERT INTO source_versions(source_ref,version_seq,doc_id,content_sha256,"
                       "recorded_at) VALUES('event:1',1,'raw/x.md','0','t')",
    "edge_origin": "INSERT INTO edge_origin(edge_id,claim_ref,origin,source_ref,quote,quote_sha256,"
                   "fact_kind) VALUES('e1','rule:r@1','record-rule','event:1','q','0','record-fact')",
    "candidates": "INSERT INTO candidates(candidate_id,kind,body_sha256,proposer,proposer_kind,"
                  "record_event_id,checks_json,status,created_at,updated_at) "
                  "VALUES('c1','fact','0','s','session',1,'[]','checked','t','t')",
    "candidate_anchors": "INSERT INTO candidate_anchors(candidate_id,ordinal,source_ref,"
                         "quote_sha256,role) VALUES('c1',0,'event:1','0','anchor')",
    "exclusions": "INSERT INTO exclusions(source_ref,reason,requested_by,record_event_id,"
                  "recorded_at) VALUES('event:1','r','owner',1,'t')",
    "corrections": "INSERT INTO corrections(correction_id,target_subj,target_predicate,"
                   "target_obj_key,target_valid_from,target_source_ref,target_quote_sha256,action,"
                   "reason,requested_by,record_event_id,recorded_at) "
                   "VALUES('k1','s','p','o','t','event:1','0','retract','r','owner',1,'t')",
}


@pytest.mark.parametrize("table", sorted(ROWS))
def test_new_record_tables_are_append_only(tmp_path, table):
    cfg = Config.for_vault(tmp_path / "vault")
    cfg.vault_dir.mkdir()
    db = DB(cfg)
    try:
        db.pour()
        db.conn.execute("PRAGMA foreign_keys=OFF")
        db.conn.execute(ROWS["candidates"] if table == "candidate_anchors" else ROWS[table])
        if table == "candidate_anchors":
            db.conn.execute(ROWS[table])
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.conn.execute(f"DELETE FROM {table}")
    finally:
        db.close()


def test_world_change_correction_needs_effective_at(tmp_path):
    cfg = Config.for_vault(tmp_path / "vault")
    cfg.vault_dir.mkdir()
    db = DB(cfg)
    try:
        db.pour()
        row = ("c1", "s", "p", "o", "2020", "event:1", "0" * 64, "world-change", None, None, "r",
               "owner", 1, "2020", "applied")
        with pytest.raises(sqlite3.IntegrityError):
            db.conn.execute("INSERT INTO corrections(correction_id,target_subj,target_predicate,"
                            "target_obj_key,target_valid_from,target_source_ref,"
                            "target_quote_sha256,action,replacement_json,effective_at,reason,"
                            "requested_by,record_event_id,recorded_at,status) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
    finally:
        db.close()
