"""M2.1 proof: schema version, additive migration, fail-closed legacy refusal,
append-only triggers, and the off-by-default head anchor.

The three Done-when cases (spec:579):
  (A) an M0-era database opens, migrates additively with its backfills and verifies
      its chain;
  (B) a database missing a required NOT NULL or CHECK constraint is refused
      fail-closed with a named reason;
  (C) a hard DELETE on events or rows aborts in the substrate via a BEFORE DELETE
      trigger.

The migration is additive only (ALTER TABLE ADD COLUMN plus CREATE ... IF NOT EXISTS);
there are no numbered migration files. The append-only guards are installed for a
managed record (a root carrying ALPACA-MANIFEST); an ad-hoc store opened outside a project
stays raw, so out-of-band property tooling still models a substrate with no guards.
"""
import json
import os
import sqlite3

import pytest

from alpaca import db, migrate, paths, util


# The M0-era shape: the seven tables M0 shipped, with NO `rows` table and no schema
# version marker. A byte copy of the M0 db.py SCHEMA (tag m0-done).
M0_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, session TEXT NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL,
  op TEXT, ref TEXT, data TEXT NOT NULL,
  content_hash TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
  sid TEXT PRIMARY KEY, started TEXT, ended TEXT, cwd TEXT, transcript TEXT,
  level TEXT, last_beat TEXT, beats INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS ops (
  id TEXT PRIMARY KEY, intent TEXT NOT NULL, done_when TEXT, status TEXT NOT NULL,
  opened TEXT, closed TEXT, phases TEXT);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, op TEXT, phase TEXT, statement TEXT NOT NULL, status TEXT NOT NULL,
  proof TEXT, where_ TEXT, why TEXT, claimant TEXT, lease_until TEXT, created TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, session TEXT, sender TEXT, to_ TEXT,
  kind TEXT, body TEXT, ref TEXT);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, session TEXT, actor TEXT, kind TEXT, body TEXT, ref TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# The M1-era rows table (M1.10) as it shipped: no `superseded_by` column yet.
M1_ROWS = """
CREATE TABLE IF NOT EXISTS rows (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, op TEXT, phase TEXT, step TEXT,
  statement TEXT, proof TEXT, where_ TEXT, how TEXT, when_ TEXT, why TEXT,
  session TEXT, operator TEXT, status TEXT, tag TEXT,
  content_hash TEXT, prev_hash TEXT, supersedes TEXT);
"""


def _raw(dbfile):
    conn = sqlite3.connect(dbfile, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn, table):
    return {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}


def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _make_m0_db(dbfile, n_run=2):
    """An honest M0-era record: seven tables, a valid hash chain, some run events."""
    conn = _raw(dbfile)
    conn.executescript(M0_SCHEMA)
    db.append_event(conn, session="s", actor="worker", kind="beat", ref="r0", data={"i": 0})
    for i in range(n_run):
        db.append_event(conn, session="s", actor="instrument", kind="run", ref="g%d" % i,
                        data={"gate": "g%d" % i, "code": 0, "verdict": "PASS",
                              "reason": "ok %d" % i, "evidence": ["e%d" % i]})
    db.append_event(conn, session="s", actor="worker", kind="note", ref="r1", data={"i": 1})
    ok, _ = db.verify_chain(conn)
    assert ok is True
    return conn


# ------------------------------------------------------------------ (A) additive migrate

def test_m0_era_db_migrates_additively_and_verifies_its_chain(tmp_path):
    dbfile = str(tmp_path / "alpaca.db")
    conn = _make_m0_db(dbfile)
    n_events_before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    # An M0-era store is version 0 (no rows table, no marker).
    assert migrate.current_version(conn) == 0
    assert migrate.CURRENT_SCHEMA_VERSION >= 2

    new_v = migrate.apply(conn)
    assert new_v == migrate.CURRENT_SCHEMA_VERSION
    assert migrate.current_version(conn) == migrate.CURRENT_SCHEMA_VERSION

    # New tables landed additively.
    assert _table_exists(conn, "rows")
    assert _table_exists(conn, "run")
    assert _table_exists(conn, "page")

    # New column landed on the rows table.
    assert "superseded_by" in _columns(conn, "rows")

    # Backfill: every historic run event is folded into the run census with decoded fields.
    run_rows = list(conn.execute("SELECT * FROM run ORDER BY id"))
    assert len(run_rows) == 2
    assert [r["gate"] for r in run_rows] == ["g0", "g1"]
    assert all(r["verdict"] == "PASS" and r["code"] == 0 for r in run_rows)
    assert json.loads(run_rows[0]["evidence"]) == ["e0"]

    # Nothing in the append-only chain was rewritten, and it still verifies.
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == n_events_before
    ok, _ = db.verify_chain(conn)
    assert ok is True


def test_superseded_by_is_backfilled_from_the_supersedes_graph(tmp_path):
    # An M1-era store (rows present, no superseded_by column) backfills the forward
    # supersession pointer from the existing backward `supersedes` links.
    dbfile = str(tmp_path / "alpaca.db")
    conn = _make_m0_db(dbfile, n_run=0)
    conn.executescript(M1_ROWS)
    assert "superseded_by" not in _columns(conn, "rows")
    assert migrate.current_version(conn) == 1        # rows present, no marker

    conn.execute("INSERT INTO rows (id, kind, supersedes) VALUES ('r1', 'item', NULL)")
    conn.execute("INSERT INTO rows (id, kind, supersedes) VALUES ('r2', 'item', 'r1')")

    migrate.apply(conn)

    got = dict(conn.execute("SELECT id, superseded_by FROM rows ORDER BY id").fetchall()[0])
    assert got["id"] == "r1" and got["superseded_by"] == "r2"
    r2 = conn.execute("SELECT superseded_by FROM rows WHERE id='r2'").fetchone()
    assert r2["superseded_by"] is None


def test_apply_is_idempotent(tmp_path):
    dbfile = str(tmp_path / "alpaca.db")
    conn = _make_m0_db(dbfile)
    migrate.apply(conn)
    n_run = conn.execute("SELECT COUNT(*) FROM run").fetchone()[0]
    # A second apply on an already-current store adds no columns and no duplicate backfill.
    assert migrate.apply(conn) == migrate.CURRENT_SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM run").fetchone()[0] == n_run
    ok, _ = db.verify_chain(conn)
    assert ok is True


# ------------------------------------------------------------------ (B) fail-closed refusal

def test_missing_not_null_constraint_is_refused_fail_closed(tmp_path):
    # A mangled store whose events.hash lost its NOT NULL is refused, and the reason
    # names the constraint and a recovery step.
    dbfile = str(tmp_path / "alpaca.db")
    conn = _raw(dbfile)
    conn.executescript("""
      CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL, session TEXT NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL,
        op TEXT, ref TEXT, data TEXT NOT NULL,
        content_hash TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT);
      CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    with pytest.raises(migrate.UnsafeLegacySchema) as ex:
        migrate.apply(conn)
    msg = str(ex.value)
    assert "events.hash" in msg
    assert "NOT NULL" in msg
    assert "recover" in msg.lower()


def test_missing_check_constraint_is_refused_fail_closed(tmp_path):
    # A pre-existing run census that lost its CHECK on code is refused by name.
    dbfile = str(tmp_path / "alpaca.db")
    conn = _make_m0_db(dbfile, n_run=0)
    conn.execute("CREATE TABLE run (id INTEGER PRIMARY KEY, code INTEGER)")  # no CHECK
    with pytest.raises(migrate.UnsafeLegacySchema) as ex:
        migrate.apply(conn)
    assert "run" in str(ex.value) and "CHECK" in str(ex.value)


def test_events_table_absent_is_refused(tmp_path):
    dbfile = str(tmp_path / "alpaca.db")
    conn = _raw(dbfile)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    with pytest.raises(migrate.UnsafeLegacySchema):
        migrate.apply(conn)


# ------------------------------------------------------------------ (C) append-only guards

def test_hard_delete_on_events_aborts_in_the_substrate(tmp_path):
    dbfile = str(tmp_path / "alpaca.db")
    conn = _make_m0_db(dbfile)
    migrate.apply(conn)
    migrate.install_append_only_guards(conn)
    n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM events WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM events")

    # The rows are all still there: the trigger aborted, it did not silently drop.
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == n


def test_hard_delete_on_rows_aborts_in_the_substrate(tmp_path):
    dbfile = str(tmp_path / "alpaca.db")
    conn = _make_m0_db(dbfile, n_run=0)
    migrate.apply(conn)
    migrate.install_append_only_guards(conn)
    conn.execute("INSERT INTO rows (id, kind) VALUES ('r1', 'item')")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM rows WHERE id='r1'")
    assert conn.execute("SELECT COUNT(*) FROM rows WHERE id='r1'").fetchone()[0] == 1


def test_db_connect_arms_guards_on_a_managed_root(project):
    # A managed record (ALPACA-MANIFEST at the root) is hardened by db.connect.
    conn = db.connect(project)
    db.append_event(conn, session="s", actor="a", kind="beat")
    assert migrate.current_version(conn) == migrate.CURRENT_SCHEMA_VERSION
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM events")


def test_db_connect_leaves_an_unmanaged_store_raw(tmp_path):
    # A store opened outside a project (no ALPACA-MANIFEST) stays raw, so out-of-band
    # property tooling still models a substrate with no append-only guards.
    root = tmp_path / "loose"
    root.mkdir()
    conn = db.connect(str(root))
    db.append_event(conn, session="s", actor="a", kind="beat")
    conn.execute("DELETE FROM events")   # must not raise
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_db_connect_refuses_a_mangled_managed_store(project):
    # Fail-closed reaches through db.connect: a mangled record does not open.
    os.remove(paths.db_path(project)) if os.path.exists(paths.db_path(project)) else None
    os.makedirs(os.path.dirname(paths.db_path(project)), exist_ok=True)
    bad = _raw(paths.db_path(project))
    bad.executescript("""
      CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL, session TEXT NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL,
        op TEXT, ref TEXT, data TEXT,
        content_hash TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
      CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    bad.close()
    with pytest.raises(migrate.UnsafeLegacySchema):
        db.connect(project)


# ------------------------------------------------------------------ head anchor (off by default)

def test_head_anchor_is_off_by_default(tmp_path):
    dbfile = str(tmp_path / "alpaca.db")
    conn = _make_m0_db(dbfile)
    migrate.apply(conn)
    # The opt-in head anchor writes nothing unless explicitly enabled.
    assert migrate.HEAD_ANCHOR_DEFAULT is False
    assert conn.execute("SELECT value FROM meta WHERE key='head_anchor'").fetchone() is None


def test_head_anchor_records_and_verifies_when_enabled(tmp_path):
    dbfile = str(tmp_path / "alpaca.db")
    conn = _make_m0_db(dbfile)
    migrate.apply(conn)

    anchor = migrate.anchor_head(conn, enabled=True)
    assert anchor is not None
    stored = conn.execute("SELECT value FROM meta WHERE key='head_anchor'").fetchone()
    assert stored is not None
    ok, _ = migrate.verify_anchor(conn)
    assert ok is True

    # An out-of-band rewrite of the head is caught against the anchor.
    conn.execute("UPDATE events SET actor='TAMPER' WHERE id=(SELECT MAX(id) FROM events)")
    ok2, reason = migrate.verify_anchor(conn)
    assert ok2 is False and reason


def test_anchor_head_off_writes_nothing(tmp_path):
    dbfile = str(tmp_path / "alpaca.db")
    conn = _make_m0_db(dbfile)
    migrate.apply(conn)
    assert migrate.anchor_head(conn, enabled=False) is None
    assert conn.execute("SELECT value FROM meta WHERE key='head_anchor'").fetchone() is None
