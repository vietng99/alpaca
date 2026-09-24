"""A record carried over from an earlier harness, kept byte for byte.

An earlier harness can write the same record (the same events and rows tables) but mix its own
tag into each content hash, where this package mixes `alpaca-event/v1` and `alpaca-obligation/v1`.
Such a record is carried here without rewriting one stored hash: `record()` checks that every
event and every obligation row on the record hashes under the earlier tags, then writes a lineage
into `meta` and appends one `lineage` event under this package's tag, chained onto the carried
head. From then on the chain is verified with the earlier tags up to and including the last
carried event (`through_event`, whose stored hash must equal `through_hash`) and with this
package's tags after it; rows are treated the same way by their SQLite rowid (`through_row`).

The boundary cannot be moved to cover a new event: the new event does not hash under the earlier
tag, and the boundary event's hash is pinned. An unreadable lineage fails verification closed.
Like the chain itself this is tamper-evident against edits, not proof against a wholesale rewrite.

Interfaces:
  read(conn)                   -> dict | None      the recorded lineage (LineageError if malformed)
  event_tag(lin, event_id)     -> str | None       the earlier tag an event is hashed under
  obligation_tag(lin, rowid)   -> str | None       the earlier tag a row is hashed under
  row_numbers(conn)            -> {row id: rowid}
  row_frozen(row, lin, rowid)  -> bool             the row's content still matches its frozen hash
  record(conn, ...)            -> dict             carry the record (idempotent)
"""
from __future__ import annotations

import json
import re
import sqlite3

from alpaca import util

META_KEY = "lineage"
EVENT_KIND = "lineage"

_TAG_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}/v[0-9]{1,4}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_MAX = 80


class LineageError(ValueError):
    """The lineage is malformed, or a record cannot be carried as asked."""


def _valid_tag(tag, what):
    if not isinstance(tag, str) or not _TAG_RE.match(tag):
        raise LineageError("%s %r is not a tag of the form name/vN" % (what, tag))
    return tag


def _meta_raw(conn):
    try:
        r = conn.execute("SELECT value FROM meta WHERE key=?", (META_KEY,)).fetchone()
    except sqlite3.OperationalError:
        return None                                  # a raw store with no meta table
    return None if r is None else r[0]


def read(conn):
    """The recorded lineage as a dict, or None when the record carries none. A lineage that is
    present but malformed raises LineageError, so a caller verifying the chain fails closed."""
    raw = _meta_raw(conn)
    if raw is None:
        return None
    try:
        lin = json.loads(raw)
    except (TypeError, ValueError):
        raise LineageError("the recorded lineage is not readable JSON")
    if not isinstance(lin, dict):
        raise LineageError("the recorded lineage is not an object")
    _valid_tag(lin.get("event_tag"), "event_tag")
    _valid_tag(lin.get("obligation_tag"), "obligation_tag")
    for key in ("through_event", "through_row"):
        v = lin.get(key)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise LineageError("the recorded lineage has no valid %s" % key)
    if lin["through_event"] < 1:
        raise LineageError("the recorded lineage carries no event")
    if not isinstance(lin.get("through_hash"), str) or not _HEX64.match(lin["through_hash"]):
        raise LineageError("the recorded lineage has no valid through_hash")
    return lin


def event_tag(lin, event_id):
    """The earlier tag an event is hashed under, or None for this package's own tag."""
    if lin and isinstance(event_id, int) and event_id <= lin["through_event"]:
        return lin["event_tag"]
    return None


def obligation_tag(lin, rowid):
    """The earlier tag a row is hashed under, or None for this package's own tag."""
    if lin and isinstance(rowid, int) and rowid <= lin["through_row"]:
        return lin["obligation_tag"]
    return None


def row_numbers(conn) -> dict:
    """{row id: SQLite rowid} for the rows table; rows are append-only, so a rowid is stable."""
    try:
        return {r[1]: r[0] for r in conn.execute("SELECT rowid, id FROM rows")}
    except sqlite3.OperationalError:
        return {}


def row_frozen(row, lin, rowid) -> bool:
    """True when the row's load-bearing content still hashes to its frozen `content_hash`, under
    the tag its position on the record calls for."""
    from alpaca.checklist.obligation_hash import content_hash
    return content_hash(row, obligation_tag(lin, rowid)) == row.get("content_hash")


def _event_hash(fields, tag) -> str:
    return util.sha256_hex(tag + "\n" + util.canonical_json(fields))


def _check_carried(conn, ev_tag, row_tag):
    """Every event must chain from genesis under ev_tag and every hashed row must hash under
    row_tag. Returns (last event id, last hash, last rowid)."""
    from alpaca import db
    from alpaca.checklist.obligation_hash import content_hash
    prev, last_id = db.GENESIS, 0
    for r in conn.execute("SELECT * FROM events ORDER BY id"):
        fields = {"ts": r["ts"], "session": r["session"], "actor": r["actor"], "kind": r["kind"],
                  "op": r["op"], "ref": r["ref"], "data": r["data"]}
        if _event_hash(fields, ev_tag) != r["content_hash"]:
            raise LineageError("event %d does not hash under %s" % (r["id"], ev_tag))
        if r["prev_hash"] != prev or util.sha256_hex(prev + "\n" + r["content_hash"]) != r["hash"]:
            raise LineageError("the chain breaks at event %d" % r["id"])
        prev, last_id = r["hash"], r["id"]
    if last_id == 0:
        raise LineageError("the record holds no event to carry")
    last_row = 0
    for r in conn.execute("SELECT rowid AS n, * FROM rows ORDER BY rowid"):
        row = dict(r)
        last_row = row["n"]
        if row.get("content_hash") and content_hash(row, row_tag) != row["content_hash"]:
            raise LineageError("row %s does not hash under %s" % (row.get("id"), row_tag))
    return last_id, prev, last_row


def record(conn, *, event_tag, obligation_tag, source, session, actor, clock=None) -> dict:
    """Carry an earlier harness's record: check it, write the lineage and append one `lineage`
    event, in one transaction. Calling it again with the same tags returns the recorded lineage
    and writes nothing; a different lineage on a record that already carries one is refused."""
    from alpaca import db
    _valid_tag(event_tag, "event_tag")
    _valid_tag(obligation_tag, "obligation_tag")
    source = str(source or "")
    if not source or len(source) > _SOURCE_MAX or not source.isprintable():
        raise LineageError("source must be a short printable name")
    current = read(conn)
    if current is not None:
        if current["event_tag"] == event_tag and current["obligation_tag"] == obligation_tag:
            return current
        raise LineageError("the record already carries a lineage from %s" % current.get("source"))
    with db.transaction(conn):
        last_id, last_hash, last_row = _check_carried(conn, event_tag, obligation_tag)
        lin = {"event_tag": event_tag, "obligation_tag": obligation_tag, "source": source,
               "through_event": last_id, "through_hash": last_hash, "through_row": last_row,
               "recorded": clock() if clock is not None else util.now_iso()}
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                     "value=excluded.value", (META_KEY, util.canonical_json(lin)))
        db.append_event(conn, session=session, actor=actor, kind=EVENT_KIND, data=lin,
                        conn_in_txn=True, clock=clock)
    return lin
