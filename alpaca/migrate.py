"""Schema version, additive migration, fail-closed legacy refusal, append-only guards.

M2.1 (spec:579). The record evolves only additively: a new column is an ALTER TABLE ADD
COLUMN, a new table is a CREATE ... IF NOT EXISTS, and a value that a new column should
have carried is backfilled from the data already in the store. There are no numbered
migration files; the current shape is alpaca/schema.sql plus the column map and backfills here.

Three guarantees this module owns, consumed by db.connect and alpaca doctor:

  * `current_version(conn)` / `apply(conn)`  -- an M0-era or M1-era store is brought to
    CURRENT_SCHEMA_VERSION additively, with its backfills, and its hash chain is untouched.
  * `UnsafeLegacySchema`  -- a store missing a required NOT NULL or CHECK constraint is
    refused fail-closed, before any write, with a reason that names the constraint and a
    recovery step. Deprecate-not-delete and additive-only are worthless on a store whose
    invariants were already stripped, so the migration never runs on one.
  * `install_append_only_guards(conn)`  -- BEFORE DELETE triggers on events and rows so
    deprecate-not-delete is enforced by the store, not by prose. db.connect installs these
    for a managed record (a root carrying ALPACA-MANIFEST); an ad-hoc store opened outside a
    project stays raw, which is what the property fuzzer needs to model out-of-band access.

The optional head anchor (KEEP opt-in, section 6 row 69) is carried here off by default:
`anchor_head` writes nothing unless explicitly enabled, and `test_migrate.py` asserts the
off state.
"""
from __future__ import annotations

import json
import os
import sqlite3

from alpaca import util

CURRENT_SCHEMA_VERSION = 4

_SCHEMA_KEY = "schema_version"
_ANCHOR_KEY = "head_anchor"

# The head anchor is opt-in and off by default (section 6 KEEP opt-in row 69).
HEAD_ANCHOR_DEFAULT = False


class UnsafeLegacySchema(Exception):
    """A store whose required invariants are absent. Raised before any migration write, so a
    mangled record is refused rather than silently carried forward."""


# ---- required constraints the migration verifies on every open (fail-closed) -------------
# Columns that must be NOT NULL. Checkable by PRAGMA on every era, so requiring them never
# refuses an honest M0/M1 store; it refuses only a store where the invariant was stripped.
_REQUIRED_NOT_NULL = {
    "events": ["ts", "session", "actor", "kind", "data",
               "content_hash", "prev_hash", "hash"],
    "rows": ["kind"],          # only enforced when a rows table is present
}

# A table -> (needle, message) CHECK requirement. Enforced only when the table already
# exists, so a pre-migration store (which has no run table yet) is never refused for it;
# a mangled store that carries a run table without its CHECK is.
_REQUIRED_CHECK = {
    "run": ("code", "the run census table must CHECK its code column"),
}

# Additive columns: {table: [(column, type_and_default_sql)]}. Applied only when absent.
_ADDITIVE_COLUMNS = {
    "rows": [("superseded_by", "TEXT")],
    "events": [("where_", "TEXT")],
    "tasks": [("title", "TEXT")],
}


# --------------------------------------------------------------------------- introspection
def _schema_sql_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def schema_sql() -> str:
    with open(_schema_sql_path(), encoding="utf-8") as fh:
        return fh.read()


def _table_exists(conn, table) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _table_info(conn, table):
    return list(conn.execute("PRAGMA table_info(%s)" % table))


def _columns(conn, table):
    return {r["name"] for r in _table_info(conn, table)}


def _table_sql(conn, table) -> str:
    r = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return (r[0] if r and r[0] else "") or ""


def _meta_get(conn, key, default=None):
    try:
        r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return default            # no meta table at all
    return r[0] if r else default


def _meta_set(conn, key, value):
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


# ---------------------------------------------------------------------------- version
def current_version(conn) -> int:
    """The store's schema version: the recorded marker if present, else inferred from shape
    (a rows table means M1-era version 1, otherwise M0-era version 0)."""
    marked = _meta_get(conn, _SCHEMA_KEY)
    if marked is not None:
        try:
            return int(marked)
        except (TypeError, ValueError):
            return 0
    if _table_exists(conn, "rows"):
        return 1
    return 0


# ------------------------------------------------------------------ fail-closed refusal
def _refuse(reason: str):
    raise UnsafeLegacySchema(
        reason + "; recover by rebuilding the record from its event chain (alpaca verify) or "
        "restoring the last good .alpaca/alpaca.db, then reopening")


def _verify_constraints(conn) -> None:
    """Refuse fail-closed, before any write, if a required NOT NULL or CHECK is absent."""
    if not _table_exists(conn, "events"):
        _refuse("the events table is absent, so this is not an Alpaca record")
    for table, cols in _REQUIRED_NOT_NULL.items():
        if not _table_exists(conn, table):
            continue                    # a rows table is optional before migration
        info = {r["name"]: r for r in _table_info(conn, table)}
        for c in cols:
            r = info.get(c)
            if r is None:
                _refuse("%s.%s is absent but required" % (table, c))
            # notnull==1 or part of the primary key both guarantee non-null.
            if not (r["notnull"] or r["pk"]):
                _refuse("%s.%s must be NOT NULL" % (table, c))
    for table, (needle, msg) in _REQUIRED_CHECK.items():
        if not _table_exists(conn, table):
            continue
        sql = _table_sql(conn, table).upper()
        if "CHECK" not in sql or needle.upper() not in sql:
            _refuse(msg + " (missing CHECK)")


# ------------------------------------------------------------------ additive migration
def _add_missing_columns(conn) -> None:
    for table, cols in _ADDITIVE_COLUMNS.items():
        if not _table_exists(conn, table):
            continue
        present = _columns(conn, table)
        for name, decl in cols:
            if name not in present:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))


def _backfill_superseded_by(conn) -> None:
    """Derive the forward supersession pointer from the existing backward `supersedes`
    links. Deterministic and idempotent: only rows whose forward pointer is still null are
    filled, and each is set to the single row that supersedes it."""
    if not _table_exists(conn, "rows"):
        return
    conn.execute(
        "UPDATE rows SET superseded_by = ("
        "  SELECT r2.id FROM rows r2 WHERE r2.supersedes = rows.id LIMIT 1) "
        "WHERE superseded_by IS NULL "
        "  AND EXISTS (SELECT 1 FROM rows r3 WHERE r3.supersedes = rows.id)")


def _backfill_run_census(conn) -> None:
    """Fold every historic kind='run' event into the run census. Idempotent: an event whose
    id is already folded is skipped, so re-running the migration never duplicates a row."""
    if not _table_exists(conn, "run"):
        return
    done = {r[0] for r in conn.execute(
        "SELECT event_id FROM run WHERE event_id IS NOT NULL")}
    for e in conn.execute("SELECT id, ts, data FROM events WHERE kind='run' ORDER BY id"):
        if e["id"] in done:
            continue
        try:
            d = json.loads(e["data"]) if e["data"] else {}
        except (TypeError, ValueError):
            d = {}
        code = d.get("code")
        if not isinstance(code, int) or code < 0:
            code = 0
        conn.execute(
            "INSERT INTO run (event_id, ts, gate, code, verdict, reason, evidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (e["id"], e["ts"], d.get("gate"), code, d.get("verdict"), d.get("reason"),
             util.canonical_json(d.get("evidence") or [])))


def _run_census_behind(conn) -> bool:
    if not _table_exists(conn, "run"):
        return False
    folded = conn.execute("SELECT COUNT(*) FROM run WHERE event_id IS NOT NULL").fetchone()[0]
    events = conn.execute("SELECT COUNT(*) FROM events WHERE kind='run'").fetchone()[0]
    return folded < events


def execute_schema(conn, script):
    """Execute DDL without executescript's implicit transaction commit."""
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            conn.execute(pending)
            pending = ""
    if pending.strip() and any(not l.lstrip().startswith("--") for l in pending.splitlines() if l.strip()):
        raise ValueError("incomplete schema statement")


def apply(conn) -> int:
    """Transactional additive migration. Current schemas cause no writes."""
    from alpaca.db import transaction
    _verify_constraints(conn)
    from_version = current_version(conn)
    if from_version > CURRENT_SCHEMA_VERSION:
        raise UnsafeLegacySchema("record schema is newer than this operator; upgrade Alpaca")
    if from_version == CURRENT_SCHEMA_VERSION:
        if _run_census_behind(conn):
            with transaction(conn):
                _backfill_run_census(conn)
        return from_version
    with transaction(conn):
        execute_schema(conn, schema_sql())
        _add_missing_columns(conn)
        _backfill_superseded_by(conn)
        _backfill_run_census(conn)
        for version in range(from_version + 1, CURRENT_SCHEMA_VERSION + 1):
            conn.execute("INSERT OR IGNORE INTO schema_migration VALUES (?,?,?)",
                         (version, util.now_iso(), "additive schema migration"))
        _meta_set(conn, _SCHEMA_KEY, CURRENT_SCHEMA_VERSION)
    return CURRENT_SCHEMA_VERSION


# ------------------------------------------------------------------ append-only guards
def install_append_only_guards(conn) -> None:
    """BEFORE DELETE triggers on events and rows: deprecate-not-delete enforced by the store.
    Idempotent (CREATE TRIGGER IF NOT EXISTS)."""
    if guards_installed(conn):
        return
    execute_schema(conn, """
      CREATE TRIGGER IF NOT EXISTS events_no_delete
      BEFORE DELETE ON events
      BEGIN
        SELECT RAISE(ABORT, 'events is append-only: deprecate, do not delete');
      END;
      CREATE TRIGGER IF NOT EXISTS events_no_update
      BEFORE UPDATE ON events
      BEGIN
        SELECT RAISE(ABORT, 'events is append-only: append a correction');
      END;
      CREATE TRIGGER IF NOT EXISTS rows_no_delete
      BEFORE DELETE ON rows
      BEGIN
        SELECT RAISE(ABORT, 'rows is append-only: supersede, do not delete');
      END;
    """)


def guards_installed(conn) -> bool:
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}
    return {"events_no_delete", "events_no_update", "rows_no_delete"} <= names


# ------------------------------------------------------------------ head anchor (opt-in)
_GENESIS = util.sha256_hex(b"")


def _effective_head(conn):
    """Recompute the chain head from the events' CURRENT content, independent of the stored
    hash column. So an in-place edit that does not re-chain (actor rewritten, hash column
    left alone) still moves the effective head and is caught against a recorded anchor."""
    prev = _GENESIS
    n = 0
    for r in conn.execute("SELECT * FROM events ORDER BY id"):
        fields = {"ts": r["ts"], "session": r["session"], "actor": r["actor"],
                  "kind": r["kind"], "op": r["op"], "ref": r["ref"], "data": r["data"]}
        ch = util.sha256_hex("alpaca-event/v1\n" + util.canonical_json(fields))
        prev = util.sha256_hex(prev + "\n" + ch)
        n += 1
    return {"head": prev, "count": n}


def _head(conn):
    return _effective_head(conn)


def anchor_head(conn, *, enabled=None):
    """Record the current chain head as an external anchor, only when enabled. Off by default
    (HEAD_ANCHOR_DEFAULT): with the default the anchor is never written. Returns the anchor
    dict when written, else None."""
    if enabled is None:
        enabled = HEAD_ANCHOR_DEFAULT
    if not enabled:
        return None
    anchor = _head(conn)
    _meta_set(conn, _ANCHOR_KEY, util.canonical_json(anchor))
    try:
        conn.commit()
    except sqlite3.OperationalError:
        pass
    return anchor


def verify_anchor(conn):
    """Compare the recorded anchor to the current head. -> (ok, reason). No anchor recorded
    is not a failure; it is the off-by-default state."""
    raw = _meta_get(conn, _ANCHOR_KEY)
    if raw is None:
        return True, "no head anchor recorded (off by default)"
    try:
        stored = json.loads(raw)
    except (TypeError, ValueError):
        return False, "head anchor is unreadable"
    now = _head(conn)
    if now["count"] < stored.get("count", 0):
        return False, "chain shorter than the anchor: %d < %d" % (
            now["count"], stored.get("count", 0))
    if stored.get("count", 0) == now["count"] and stored.get("head") != now["head"]:
        return False, "chain head does not match the anchor"
    return True, "head anchor holds"
