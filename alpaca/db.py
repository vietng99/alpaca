"""The record. One SQLite file, sole writer = the alpaca package.

events: append-only, hash-chained (content_hash over canonical JSON, hash over prev_hash + content_hash).
sessions/ops/tasks/messages/decisions: current state, mutated only after an event was appended.
"""
import contextlib, json, os, sqlite3
from alpaca import paths, util

GENESIS = util.sha256_hex(b"")

# Compatibility export; schema.sql is the sole schema definition.
from alpaca.migrate import schema_sql
SCHEMA = schema_sql()


def connect_readonly(root_dir: str) -> sqlite3.Connection:
    """Read committed state without migrations or project initialization.

    SQLite may maintain WAL shared-memory coordination files for a live WAL database;
    no logical record writes are permitted. Never use immutable=1 on a live WAL record.
    An absent record uses an empty in-memory schema and creates no filesystem state.
    """
    from pathlib import Path
    p = Path(paths.db_path(root_dir)).absolute()
    if p.exists():
        conn = sqlite3.connect(p.as_uri() + "?mode=ro", uri=True, timeout=10,
                               isolation_level=None)
    else:
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn

def connect(root_dir: str) -> sqlite3.Connection:
    p = paths.db_path(root_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    conn = sqlite3.connect(p, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # Initialize only a new record; migration owns all subsequent schema changes.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
        with transaction(conn):
            from alpaca.migrate import execute_schema
            execute_schema(conn, SCHEMA)
    # M2.1: bring the store to the current schema version additively (with its backfills),
    # refusing a mangled record fail-closed. A managed record (a root carrying ALPACA-MANIFEST,
    # the marker paths.root() uses) also gets the substrate's append-only guards installed;
    # an ad-hoc store opened outside a project stays raw, so out-of-band property tooling
    # keeps a guard-free substrate to tamper against.
    from alpaca import migrate
    try:
        migrate.apply(conn)
        if os.path.isfile(os.path.join(root_dir, paths.MANIFEST)):
            migrate.install_append_only_guards(conn)
    except BaseException:
        conn.close()
        raise
    return conn

@contextlib.contextmanager
def transaction(conn):
    """One record mutation = one transaction. BEGIN IMMEDIATE on entry, COMMIT on
    clean exit, ROLLBACK on any exception. Every write inside lands together or not
    at all. Re-entrant: if a transaction is already open the block joins it and lets
    the outermost caller commit. The ROLLBACK is guarded by conn.in_transaction so a
    failed BEGIN cannot mask the original error with a 'no transaction' error."""
    if conn.in_transaction:
        yield conn                       # join the open transaction, do not commit here
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        if conn.in_transaction:
            conn.execute("COMMIT")

def _content_hash(fields: dict) -> str:
    return util.sha256_hex("alpaca-event/v1\n" + util.canonical_json(fields))

def last_event(conn):
    r = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone()
    return dict(r) if r else None

def _append_event_row(conn, session, actor, kind, op, ref, data, clock=None, where=None) -> dict:
    ts = clock() if clock is not None else util.now_iso()
    fields = {"ts": ts, "session": session, "actor": actor, "kind": kind,
              "op": op, "ref": ref, "data": util.canonical_json(data or {})}
    ch = _content_hash(fields)
    prev = last_event(conn)
    prev_hash = prev["hash"] if prev else GENESIS
    h = util.sha256_hex(prev_hash + "\n" + ch)
    # M4.10: the where_ column is appended only when an attribution is passed, so the default
    # INSERT is byte-for-byte the M0 shape and still lands against a raw store that has no
    # where_ column (the migration fixtures, the property fuzzer). where_ is not a hashed field.
    cols = ["ts", "session", "actor", "kind", "op", "ref", "data", "content_hash", "prev_hash", "hash"]
    vals = [fields["ts"], session, actor, kind, op, ref, fields["data"], ch, prev_hash, h]
    if where is not None:
        cols.append("where_"); vals.append(where)
    conn.execute(
        "INSERT INTO events (%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))), vals)
    ev = dict(conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone())
    if kind == "run":
        _fold_run(conn, ev, data or {})
    return ev


def _fold_run(conn, ev, data) -> None:
    """Fold one kind='run' event into the run census, inside the appending transaction.

    data.json's `gates` and `runs` read the census table, so a census that is only filled by a
    schema migration stays empty on a live store and the status page shows no gate history. The
    fold is the same row the migration backfill writes. A raw store without the census table
    (the migration fixtures, the property fuzzer) is left alone."""
    code = data.get("code")
    if not isinstance(code, int) or isinstance(code, bool) or code < 0:
        code = 0
    try:
        conn.execute(
            "INSERT INTO run (event_id, ts, gate, code, verdict, reason, evidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ev["id"], ev["ts"], data.get("gate"), code, data.get("verdict"), data.get("reason"),
             util.canonical_json(data.get("evidence") or [])))
    except sqlite3.OperationalError:
        pass

def append_event(conn, *, session, actor, kind, op=None, ref=None, data=None,
                 conn_in_txn=False, clock=None, where=None) -> dict:
    """Append one hash-chained event. By default this is its own transaction. Pass
    conn_in_txn=True to join a transaction the caller already opened with
    db.transaction, so the event and the current-state write commit together.

    M2.2: pass clock=<Clock> to stamp ts from an injected clock (a FixedClock under test);
    when None the process clock via util.now_iso is used, so a clock installed with
    util.set_clock is picked up here too."""
    if conn_in_txn:
        return _append_event_row(conn, session, actor, kind, op, ref, data, clock, where)
    with transaction(conn):
        return _append_event_row(conn, session, actor, kind, op, ref, data, clock, where)

def verify_chain(conn):
    prev_hash = GENESIS
    n = 0
    for r in conn.execute("SELECT * FROM events ORDER BY id"):
        fields = {"ts": r["ts"], "session": r["session"], "actor": r["actor"], "kind": r["kind"],
                  "op": r["op"], "ref": r["ref"], "data": r["data"]}
        if _content_hash(fields) != r["content_hash"]:
            return False, "content drift at event %d" % r["id"]
        if r["prev_hash"] != prev_hash:
            return False, "chain break at event %d" % r["id"]
        if util.sha256_hex(prev_hash + "\n" + r["content_hash"]) != r["hash"]:
            return False, "hash drift at event %d" % r["id"]
        prev_hash = r["hash"]; n += 1
    return True, "%d events" % n

def events(conn, *, kind=None, session=None, limit=200):
    q = "SELECT * FROM events"; w = []; p = []
    if kind: w.append("kind=?"); p.append(kind)
    if session: w.append("session=?"); p.append(session)
    if w: q += " WHERE " + " AND ".join(w)
    q += " ORDER BY id DESC LIMIT ?"; p.append(limit)
    out = [dict(r) for r in conn.execute(q, p)]
    for e in out:
        e["data"] = json.loads(e["data"])
    return list(reversed(out))

def rows(conn, table, where="", params=()):
    q = "SELECT * FROM %s" % table + (" WHERE " + where if where else "")
    return [dict(r) for r in conn.execute(q, params)]

def upsert(conn, table, key_col, row: dict):
    cols = list(row.keys())
    sets = ", ".join("%s=excluded.%s" % (c, c) for c in cols if c != key_col)
    conn.execute(
        "INSERT INTO %s (%s) VALUES (%s) ON CONFLICT(%s) DO UPDATE SET %s"
        % (table, ",".join(cols), ",".join("?" * len(cols)), key_col, sets),
        [row[c] for c in cols])
    if not conn.in_transaction:   # inside db.transaction the caller commits, not us
        conn.commit()

def patch(conn, table, key_col, key, updates: dict):
    """Merge updates into an existing row; return the merged row, or None when the row is absent."""
    cur = rows(conn, table, "%s=?" % key_col, (key,))
    if not cur:
        return None
    row = dict(cur[0]); row.update(updates)
    upsert(conn, table, key_col, row)
    return row

def meta_get(conn, key, default=None):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default

def meta_set(conn, key, value):
    upsert(conn, "meta", "key", {"key": key, "value": value})
