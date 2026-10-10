"""The servable predicate (architecture.md 6.2; invariant OB-07), the one definition every serve path
uses: answers and context (engine/retrieve.py post-filter), the door-internal reads (engine/loop.py
successor walk and ambiguity gate, engine/oracle.py conflict note) and extract.emit.

A pure read over docs.private, docs.privacy_scanned, the schema-4 `exclusions` and `source_versions`
tables and the compartment. It follows the push predicate (engine/compartment.py
pushable_where_clause: a doc is public only when private=0 and privacy_scanned=1) and the domain
filter. It imports no ranking module and writes nothing.

`servable_ref(db, source_ref)` is false when an exclusions row names the source, when its current
version is private, or when any capture of it was quarantined (a private version, a private
source_versions row or a private document whose header names the same source). `servable` applies
the same per block, plus the compartment.
"""
from __future__ import annotations

import re
from typing import Iterable, Sequence

from .engine.compartment import is_pushable, pushable_where_clause

# The push predicate, reused verbatim: a doc is public only when private=0 and privacy_scanned=1.
_HIDDEN_DOCS = f"SELECT doc_id FROM docs WHERE NOT ({pushable_where_clause()})"
_HEADER_CHARS = 1024


def _conn(db):
    return getattr(db, "conn", db)


def _table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def _identity(text: str | None) -> str | None:
    if not text:
        return None
    from .ingest.drain import projection_identity   # the one header reader (drain.py)
    return projection_identity(text[:_HEADER_CHARS])


def source_refs_for_docs(db, doc_ids: Iterable[str]) -> dict[str, str]:
    """doc_id -> source_ref. A source_versions row wins; else the note header (event:<id>,
    transcript:<session>) read by drain.projection_identity; else document:<doc_id>."""
    conn = _conn(db)
    wanted = sorted(set(d for d in doc_ids if d))
    out: dict[str, str] = {}
    if not wanted:
        return out
    if _table_exists(conn, "source_versions"):
        for chunk in _chunks(wanted):
            marks = ",".join("?" * len(chunk))
            for r in conn.execute(
                    f"SELECT doc_id, source_ref FROM source_versions WHERE doc_id IN ({marks}) "
                    "ORDER BY version_seq", chunk):
                out[r[0]] = r[1]
    rest = [d for d in wanted if d not in out]
    for chunk in _chunks(rest):
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
                f"SELECT doc_id, substr(text,1,{_HEADER_CHARS}) FROM blocks "
                f"WHERE ordinal=0 AND doc_id IN ({marks})", chunk):
            ident = _identity(r[1])
            if ident:
                out[r[0]] = ident
    for d in wanted:
        out.setdefault(d, "document:" + d)
    return out


def _chunks(items: Sequence[str], size: int = 500):
    for i in range(0, len(items), size):
        yield list(items[i:i + size])


def unservable_source_refs(db) -> set[str]:
    """Every source_ref that fails servable_ref: excluded, or with any private or unscanned version
    (a quarantined capture lands private under wiki/private/, architecture.md 9.5).

    On a store.db.DB the set is kept for as long as no write lands on the store (the connection's
    own change count and SQLite's data_version for other connections), so one answer's several
    retrieval passes compute it once."""
    conn = _conn(db)
    key = None
    if conn is not db:
        key = (conn.total_changes, conn.execute("PRAGMA data_version").fetchone()[0])
        cached = getattr(db, "_rune2_unservable", None)
        if cached and cached[0] == key:
            return set(cached[1])
    bad = _unservable(conn)
    if key is not None:
        db._rune2_unservable = (key, frozenset(bad))
    return bad


def _unservable(conn) -> set[str]:
    bad = excluded_source_refs(conn)
    if _table_exists(conn, "source_versions"):
        bad.update(r[0] for r in conn.execute(
            "SELECT DISTINCT source_ref FROM source_versions WHERE COALESCE(private,1)<>0"))
    hidden = [r[0] for r in conn.execute(_HIDDEN_DOCS)]
    bad.update(source_refs_for_docs(conn, hidden).values())
    return bad


def excluded_source_refs(db, also=()) -> set[str]:
    """The exclusion part of `unservable_source_refs`, what withdrawals took out: every source an
    `exclusions` row names, every source that shares an origin_hash with one of them, and the source
    copy links (`_source_copy_links`), so a source copy and the event note it was made from fall
    together whichever of the two was withdrawn. The servable predicate, the absorb skip
    (`Absorber._excluded`, through `withdrawn`), the expected event sets of status, coverage and the
    admit capture step (`service._excluded_events`), `wiki recover`, the K-4 reason of intake and the
    duplicate answer of the withdrawal verb read this one set (check round 4 invariants B1). `also`
    names sources as if a row named each: what a withdrawal of them would take out. Read-only."""
    conn = _conn(db)
    named = _named(conn) | set(also)
    out = set(named)
    if not out:
        return out
    # A source copy (a decision, contract or proof page, FR-9) made from an excluded event shares
    # the event's origin_hash: withdrawing event:N withdraws every copy of it. Every version of a
    # source that has a non-empty origin_hash shared by a version of a named source.
    origins = _named_origins(conn, named)
    for chunk in _chunks(sorted(origins)):
        out.update(r[0] for r in conn.execute(
            "SELECT DISTINCT source_ref FROM source_versions WHERE origin_hash IN (%s)"
            % ",".join("?" * len(chunk)), chunk))
    out.update(_source_copy_links(conn, named))
    return out


def _named(conn) -> set[str]:
    """The sources the `exclusions` rows name."""
    if not _table_exists(conn, "exclusions"):
        return set()
    return {r[0] for r in conn.execute("SELECT DISTINCT source_ref FROM exclusions")}


def _named_origins(conn, named=None) -> set[str]:
    """The non-empty origin_hash of every version of a named source (by default, of a source an
    `exclusions` row names)."""
    if not _table_exists(conn, "source_versions"):
        return set()
    out: set[str] = set()
    for chunk in _chunks(sorted(_named(conn) if named is None else named)):
        out.update(r[0] for r in conn.execute(
            "SELECT DISTINCT origin_hash FROM source_versions WHERE origin_hash IS NOT NULL "
            "AND origin_hash<>'' AND source_ref IN (%s)" % ",".join("?" * len(chunk)), chunk))
    return out


class Withdrawn:
    """What withdrawals took out, as one capture reads it before it lands a document: `refs` is
    `excluded_source_refs`, `named` the sources the `exclusions` rows name, `origins` the origin_hash
    of every version of a named source."""

    __slots__ = ("refs", "named", "origins")

    def __init__(self, refs, named, origins):
        self.refs, self.named, self.origins = frozenset(refs), frozenset(named), frozenset(origins)

    def holds(self, source_ref: str, doc_id: str | None = None, origin_hash: str | None = None) -> bool:
        """Whether a document of `source_ref` falls with a withdrawal: its source is in `refs`, or the
        document would join them once it landed, as `_unservable` reads a landed one: a source copy
        (`raw/sources/<kind>/<N>-...`) of an event a row names, or a document made from what a named
        source was made from (its origin_hash)."""
        if source_ref in self.refs:
            return True
        m = _SOURCE_DOC.match(doc_id or "")
        if m and "event:%d" % int(m.group(1)) in self.named:
            return True
        return bool(origin_hash) and origin_hash in self.origins


def withdrawn(db) -> Withdrawn | None:
    """The `Withdrawn` view of the store, or None when no `exclusions` row exists. Read-only."""
    conn = _conn(db)
    if not _table_exists(conn, "exclusions"):
        return None
    named = {r[0] for r in conn.execute("SELECT DISTINCT source_ref FROM exclusions")}
    if not named:
        return None
    return Withdrawn(excluded_source_refs(conn), named, _named_origins(conn))


def exclusions_state(db) -> tuple | None:
    """A key that changes whenever an `exclusions` row is added (the table is append-only): the row
    count and the last rowid. None when the table does not exist."""
    conn = _conn(db)
    if not _table_exists(conn, "exclusions"):
        return None
    return tuple(conn.execute("SELECT COUNT(*), MAX(rowid) FROM exclusions").fetchone())


_SOURCE_DOC = re.compile(r"^raw/sources/[a-z]+/(\d+)-")
_EVENT_REF = re.compile(r"^event:(0|[1-9][0-9]*)$", re.ASCII)


def _source_copy_links(conn, named=None) -> set[str]:
    """A source copy (a decision, contract or proof page, FR-9) and the event note it was made from
    fall together. The copy names its event in its doc id, so the link needs no source_versions row:
    a note captured before the copies existed has none (the live store). event:N excluded hides the
    copy of event N; a copy excluded hides event:N. `named` stands for the `exclusions` rows."""
    excluded = _named(conn) if named is None else set(named)
    if not excluded:
        return set()
    docs = [r[0] for r in conn.execute("SELECT doc_id FROM docs WHERE doc_id LIKE 'raw/sources/%'")]
    refs = source_refs_for_docs(conn, docs)
    events = {int(m.group(1)) for e in excluded for m in [_EVENT_REF.match(e)] if m}
    out: set[str] = set()
    for doc_id in docs:
        m = _SOURCE_DOC.match(doc_id)
        if not m:
            continue
        seq = int(m.group(1))
        ref = refs.get(doc_id)
        if seq in events and ref:
            out.add(ref)
        if ref in excluded:
            out.add("event:%d" % seq)
    return out


def servable_ref(db, source_ref: str) -> bool:
    """architecture.md 6.2, the one definition (OB-07)."""
    if not source_ref:
        return False
    if source_ref[:6].lower() == "event:" and not _EVENT_REF.match(source_ref):
        return False                 # a second spelling of an event ref names no source (fix-r1 B1)
    return source_ref not in unservable_source_refs(db)


def servable(db, block_ids: Iterable[str], *, compartment: str | None) -> set[str]:
    """The subset of block_ids whose doc is public, whose source passes servable_ref and which lies
    inside the compartment (the target domain plus the shared `general` one; None = any domain)."""
    conn = _conn(db)
    ids = sorted(set(b for b in block_ids if b))
    if not ids:
        return set()
    domains = None if compartment is None else {compartment, "general"}
    rows = []
    for chunk in _chunks(ids):
        marks = ",".join("?" * len(chunk))
        rows.extend(conn.execute(
            f"SELECT b.block_id, b.doc_id, b.domain, d.private, d.privacy_scanned FROM blocks b "
            f"JOIN docs d ON d.doc_id=b.doc_id WHERE b.block_id IN ({marks})", chunk).fetchall())
    rows = [r for r in rows if is_pushable(r[3], r[4])
            and (domains is None or r[2] in domains)]
    bad = unservable_source_refs(db)
    if bad and rows:
        refs = source_refs_for_docs(conn, {r[1] for r in rows})
        rows = [r for r in rows if refs.get(r[1]) not in bad]
    return {r[0] for r in rows}


def block_servable(db, block_id: str | None, *, compartment: str | None = None) -> bool:
    return bool(block_id) and block_id in servable(db, [block_id], compartment=compartment)


def servable_docs(db, doc_ids: Iterable[str]) -> set[str]:
    """The subset of doc_ids that are public and whose source passes servable_ref (extract.emit)."""
    conn = _conn(db)
    ids = sorted(set(d for d in doc_ids if d))
    keep: set[str] = set()
    for chunk in _chunks(ids):
        marks = ",".join("?" * len(chunk))
        keep.update(r[0] for r in conn.execute(
            f"SELECT doc_id, private, privacy_scanned FROM docs WHERE doc_id IN ({marks})", chunk)
            if is_pushable(r[1], r[2]))
    bad = unservable_source_refs(db)
    if bad and keep:
        refs = source_refs_for_docs(conn, keep)
        keep = {d for d in keep if refs.get(d) not in bad}
    return keep


__all__ = ["servable", "servable_ref", "servable_docs", "block_servable", "source_refs_for_docs",
           "unservable_source_refs", "excluded_source_refs", "withdrawn", "Withdrawn",
           "exclusions_state"]


#: The serving surfaces of Rune2 (t-070 round 2 C-E; round 3 adds the record-facts preview and
#: `alpaca sort`, check round 2 R2-B1 and F-2): every door that hands stored text, the record's fact
#: text, or what the store holds, to a caller. Each one answers `unavailable` with reason
#: `store-behind-record` while the record holds a content-removing act the store has not processed
#: (`service._removal_acts_pending`), a missing store included: query and context through
#: `outcome.precheck`, briefing as an unavailable briefing, status in its `serving` field, and
#: candidates, export, extract, the record-facts preview and sort by raising
#: `service.StoreBehindRecord`. The session-start briefing is `context`.
#:
#: Neither this map nor `NOT_SERVING` exempts anything from a test: the kill-state test
#: (`alpaca/tests/wiki/test_kill_states.py`) runs every name of both, the Memory methods, the `wiki`
#: verbs and the other store-reading verbs (`alpaca/tests/wiki/store_readers.py` `RUNNERS`), on a
#: store behind the record in each state it can be left in (a lost commit, no store, a killed rebuild,
#: a killed import), and finds no withdrawn word in any output; and it fails on any function that
#: reads store rows or the record's fact text and is neither gated nor run there.
SURFACES = {
    "query": "alpaca.wiki.service:query",
    "context": "alpaca.wiki.service:context",
    "briefing": "alpaca.wiki.service:briefing",
    "status": "alpaca.wiki.service:status",
    "candidates": "alpaca.wiki.service:candidates",
    "export": "alpaca.wiki.portable:export_store",
    "extract": "alpaca.wiki.extract:emit",
    "record_facts_preview": "alpaca.wiki.service:record_facts_preview",
    "record-facts": "alpaca.wiki.service:record_facts_preview",
    "sort": "alpaca.sort:run",
}

#: Every other Memory method and store-reading verb, with the reason it is not a serving surface.
NOT_SERVING = {
    "ingest": "a write door: lands a document the caller gives",
    "propose": "a write door: its checks read the store, its answer is a receipt with no stored text",
    "admit": "a write door; answers unavailable store-behind-record itself, under the write lock",
    "withdraw_candidate": "a removal door; answers unavailable store-behind-record itself",
    "withdraw-candidate": "the verb of withdraw_candidate",
    "withdraw_source": "a removal door; answers unavailable store-behind-record itself",
    "withdraw": "the verb of withdraw_source",
    "correct": "a removal door; answers unavailable store-behind-record itself",
    "consolidate": "a job: reads record events, proposes candidates, serves nothing",
    "capture": "a write path: lands record events and transcripts",
    "recover": "the replay that brings the store up to the record",
    "import": "a write path: pours an empty store from an export and the record",
    "backup": "copies the record, the store and the vault as files (retained bytes, as the vault "
              "keeps them); a restore of it is behind its record exactly when its source was",
    "collect": "the collector: its wiki consumer lands record events and transcripts as capture does",
}
