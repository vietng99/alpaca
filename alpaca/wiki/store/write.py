"""The write primitives - used ONLY by ingest/absorb and reflect/Dream (the two writers).

Never imported by the answer path (that path is read-only). Every mutation of record is wrapped
in the curator-gate transaction (atomic all-or-nothing) and, where it changes the graph, appends
a chained ingest_event mirrored to events.jsonl. Supersede-not-delete is enforced here: an
'invalidated'/'retracted' edge is stamped, never removed.
"""
from __future__ import annotations

import contextlib
import json
from typing import Any, Optional

from ..clock import Clock, now_iso
from ..determinism import canonical_json, edge_id as make_edge_id, normalize_text, sha256_hex, nfc, ppr
from ..engine.compartment import PrivatePathViolation, assert_private_path, is_private_path
from .db import DB
from .ledger import GENESIS, Ledger, event_checksum
from .tier_policy import compute_tiers, _graph


#: Every active block's text in block id order, for the vector cache of `maintain_indexes`. `+status`
#: walks the primary key in order instead of sorting every active block's text through SQLite temp
#: files under the write lock (t-070 RC4, A-4).
ACTIVE_BLOCKS_SQL = "SELECT block_id,text,context_header FROM blocks WHERE +status='active' ORDER BY block_id"


class LedgerSyncError(RuntimeError):
    """SQLite committed, but the JSONL mirror still needs a bounded recovery sync."""


class BlockIdentityCollision(RuntimeError):
    """One block_id resolved to different content, which would destroy citation evidence."""


def _validate_doc_privacy(private, path: str) -> None:
    if type(private) not in (bool, int) or private not in (0, 1):
        raise PrivatePathViolation("document privacy classification must be explicit")
    if bool(private) != is_private_path(path):
        raise PrivatePathViolation("document privacy classification/path mismatch")
    assert_private_path(private, path)


class Writer:
    def __init__(self, db: DB, ledger: Ledger, clock: Clock = now_iso):
        self.db = db
        self.ledger = ledger
        self.clock = clock
        self._tx_active = False
        self.sync_ledger()

    def sync_ledger(self) -> None:
        """Verify the store's event chain and its `events.jsonl` mirror and append what the mirror
        lacks (`Ledger.sync_from_committed`, which checks only what it has not proved yet: t-453)."""
        from .ledger import LedgerDivergence
        last_error = None
        for _attempt in range(3):
            try:
                cursor = self.db.conn.cursor()
                cursor.row_factory = None                  # plain tuples, as the ledger reads them
                rows = cursor.execute(
                    "SELECT seq,prev_checksum,checksum,ts,doc_id,op,payload FROM ingest_event "
                    "ORDER BY seq ASC").fetchall()
                self.ledger.sync_from_committed(rows)
                return
            except LedgerDivergence as exc:
                last_error = exc
                if "ledger ahead of committed DB" not in str(exc):
                    raise
        raise last_error

    # ---- transaction (curator gate) -------------------------------------
    @contextlib.contextmanager
    def transaction(self):
        from .db import assert_rebuild_access
        assert_rebuild_access(self.db.cfg)
        if self._tx_active:
            raise RuntimeError("nested Writer.transaction is not supported")
        self.sync_ledger()
        self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            assert_rebuild_access(self.db.cfg)
        except BaseException:
            self.db.conn.rollback()
            raise
        self._tx_active = True
        try:
            yield self
        except BaseException:
            self.db.conn.execute("ROLLBACK")
            raise
        else:
            try:
                self.db.conn.commit()
            except BaseException:
                if self.db.conn.in_transaction:
                    self.db.conn.rollback()
                raise
        finally:
            self._tx_active = False
        try:
            self.sync_ledger()
        except Exception as exc:
            raise LedgerSyncError(
                "SQLite commit succeeded, but events.jsonl sync is pending; reopen Writer to repair"
            ) from exc

    def maintain_indexes(self, providers, *, retier: bool = False) -> None:
        """Refresh recomputable indexes inside the caller's source/ledger transaction.

        A portable float64 cache is the deterministic backend on every machine. The
        optional extension may be installed, but its connection-local state is never
        used to decide whether a persisted vector is readable.
        """
        if not self._tx_active:
            raise RuntimeError("index maintenance requires Writer.transaction")
        db = self.db
        enabled = db.cfg.meta.get("retrieval_profile", "narrow") in ("hybrid", "full")
        exists = db.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='vec_blocks_fallback'"
        ).fetchone()
        if enabled or exists:
            from .vector_codec import _pack
            db.conn.execute("CREATE TABLE IF NOT EXISTS vec_blocks_fallback(block_id TEXT PRIMARY KEY, embedding BLOB)")
            db.conn.execute("CREATE TABLE IF NOT EXISTS vec_nodes_fallback(node_id TEXT PRIMARY KEY, embedding BLOB)")
            db.conn.execute("CREATE TABLE IF NOT EXISTS vector_cache_inputs("
                            "kind TEXT, item_id TEXT, input_hash TEXT, PRIMARY KEY(kind,item_id))")
            blocks = db.conn.execute(ACTIVE_BLOCKS_SQL).fetchall()
            nodes = db.conn.execute(
                "SELECT n.node_id,n.display_name FROM nodes n WHERE n.status='active' "
                "AND EXISTS (SELECT 1 FROM node_blocks nb JOIN blocks b ON b.block_id=nb.block_id "
                "WHERE nb.node_id=n.node_id AND b.status='active') ORDER BY n.node_id"
            ).fetchall()
            current = {
                'blocks': {r['block_id']: (r['context_header'] or '') + '\n' + r['text'] for r in blocks},
                'nodes': {r['node_id']: r['display_name'] or r['node_id'] for r in nodes},
            }
            signature = canonical_json({
                'provider': db.cfg.embedder, 'name': providers.embedder.name,
                'model': str(db.cfg.meta.get('embed_model', '')),
                'version': str(db.cfg.meta.get('embed_version', '0')),
                'dim': int(db.cfg.meta.get('embed_dim', 64)), 'format': 1,
            })
            rebuild = enabled and db.get_meta('vector_cache_signature') != signature
            for kind, inputs in current.items():
                key = 'block_id' if kind == 'blocks' else 'node_id'
                table = f'vec_{kind}_fallback'
                stored = {r[0] for r in db.conn.execute(f'SELECT {key} FROM {table}')}
                prior = dict(db.conn.execute(
                    'SELECT item_id,input_hash FROM vector_cache_inputs WHERE kind=?', (kind,)
                ).fetchall())
                for item_id in sorted((stored | set(prior)) - set(inputs)):
                    db.conn.execute(f'DELETE FROM {table} WHERE {key}=?', (item_id,))
                    db.conn.execute('DELETE FROM vector_cache_inputs WHERE kind=? AND item_id=?', (kind,item_id))
                if not enabled:
                    continue
                for item_id, text in inputs.items():
                    digest = sha256_hex(text)
                    if not rebuild and item_id in stored and prior.get(item_id) == digest:
                        continue
                    packed = _pack(providers.embedder.embed(text), int(db.cfg.meta.get('embed_dim', 64)))
                    db.conn.execute(f'INSERT OR REPLACE INTO {table}({key},embedding) VALUES(?,?)', (item_id,packed))
                    db.conn.execute('INSERT OR REPLACE INTO vector_cache_inputs(kind,item_id,input_hash) VALUES(?,?,?)',
                                    (kind,item_id,digest))
                    if kind == 'blocks':
                        db.conn.execute('UPDATE blocks SET embedded_model=? WHERE block_id=?',
                                        (providers.embedder.name,item_id))
            if enabled:
                for key in ('embed_model', 'embed_version', 'embed_dim'):
                    db.set_meta(key, str(db.cfg.meta.get(key, '')))
                db.set_meta('vector_cache_signature', signature)
        if retier:
            retier_nodes(db, autocommit=False)

    def mark_projection_superseded(self, doc_id: str, superseded_by: str, reason: str) -> None:
        if not self._tx_active:
            raise RuntimeError("projection retirement requires Writer.transaction")
        if doc_id == superseded_by:
            raise ValueError("a projection cannot supersede itself")
        db = self.db
        if not db.conn.execute('SELECT 1 FROM docs WHERE doc_id=?', (superseded_by,)).fetchone():
            raise ValueError("replacement projection must be ingested first")
        db.conn.execute('CREATE TABLE IF NOT EXISTS source_projections('
                        'doc_id TEXT PRIMARY KEY, superseded_by TEXT NOT NULL, reason TEXT NOT NULL)')
        prior = db.conn.execute('SELECT superseded_by,reason FROM source_projections WHERE doc_id=?',
                                (doc_id,)).fetchone()
        if prior and tuple(prior) == (superseded_by,reason):
            return
        db.conn.execute('INSERT OR REPLACE INTO source_projections VALUES(?,?,?)', (doc_id,superseded_by,reason))

    def projection_superseded(self, doc_id: str) -> bool:
        if not self.db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='source_projections'").fetchone():
            return False
        return bool(self.db.conn.execute('SELECT 1 FROM source_projections WHERE doc_id=?', (doc_id,)).fetchone())

    def revive_projection(self, doc_id: str) -> None:
        if self.projection_superseded(doc_id):
            if not self._tx_active:
                raise RuntimeError("projection revival requires Writer.transaction")
            self.db.conn.execute('DELETE FROM source_projections WHERE doc_id=?', (doc_id,))

    # ---- docs / blocks --------------------------------------------------
    def upsert_doc(self, doc_id, path, kind, content_sha256, byte_len=0, mtime="",
                   git_commit="", source_authority=0, domain="general", tier=1,
                   source_created_at=None, private=None, sensitivity="none") -> None:
        # os.6 WRITE barrier (fail-closed): a private:true doc outside wiki/private/ is refused here,
        # aborting the enclosing Writer.transaction() so nothing is persisted. Closes the write side
        # of the private-routing pair (the push side is is_pushable/scan_push_payload).
        _validate_doc_privacy(private, path)
        self.db.conn.execute(
            "INSERT INTO docs(doc_id,path,kind,content_sha256,byte_len,mtime,git_commit,"
            "source_authority,immutable,ingested_at,domain,tier,source_created_at,private,sensitivity,"
            "privacy_scanned) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(doc_id) DO UPDATE SET content_sha256=excluded.content_sha256,"
            "path=excluded.path,kind=excluded.kind,byte_len=excluded.byte_len,"
            "mtime=excluded.mtime,git_commit=excluded.git_commit,"
            "ingested_at=excluded.ingested_at,domain=excluded.domain,tier=excluded.tier,"
            "source_created_at=excluded.source_created_at,private=excluded.private,"
            "sensitivity=excluded.sensitivity,source_authority=excluded.source_authority,"
            "privacy_scanned=1",
            (doc_id, path, kind, content_sha256, byte_len, mtime, git_commit,
             source_authority, 1 if kind == "raw" else 0, self.clock(),
             domain, tier, source_created_at, private, sensitivity, 1),
        )

    def refresh_doc_metadata(self, doc_id: str, *, path: str, kind: str, domain: str,
                             source_created_at: str | None, private: int, sensitivity: str,
                             block_times: dict[str, str | None]) -> None:
        """Repair a document's metadata when its content is unchanged. `block_times` maps each
        active block id to its source time (a transcript block has its own, architecture.md 5.1);
        a known time also becomes the block's `valid_from`, an unknown one leaves it."""
        _validate_doc_privacy(private, path)
        self.db.conn.execute(
            "UPDATE docs SET path=?,kind=?,domain=?,source_created_at=?,private=?,sensitivity=?,"
            "privacy_scanned=1 "
            "WHERE doc_id=?",
            (path, kind, domain, source_created_at, private, sensitivity, doc_id),
        )
        self.db.conn.execute(
            "UPDATE blocks SET domain=? WHERE doc_id=? AND status='active'", (domain, doc_id))
        for block_id, moment in block_times.items():
            self.db.conn.execute(
                "UPDATE blocks SET source_created_at=?,valid_from=COALESCE(?,valid_from) "
                "WHERE block_id=? AND status='active'", (moment, moment, block_id),
            )

    def upsert_block(self, b: dict[str, Any]) -> None:
        now = self.clock()
        b.setdefault("valid_from", now)
        b.setdefault("recorded_at", now)
        b.setdefault("valid_until", None)
        b.setdefault("superseded_at", None)
        b.setdefault("status", "active")
        b.setdefault("domain", "general")               # os.5 compartment
        existing = self.db.conn.execute(
            "SELECT block_content_id, status FROM blocks WHERE block_id=?", (b["block_id"],)
        ).fetchone()
        if existing and existing["block_content_id"] != b.get("block_content_id"):
            raise BlockIdentityCollision(
                f"block identity collision for {b['block_id']}: refusing source-text overwrite"
            )
        if existing and existing["status"] != "active":
            raise BlockIdentityCollision(
                f"retired block identity cannot be silently reactivated: {b['block_id']}"
            )
        cols = ("block_id", "block_content_id", "occurrence_index", "chunk_ruleset_version",
                "doc_id", "ordinal", "heading_path", "char_start", "char_end", "block_sha256",
                "block_type", "context_header", "text", "embedded_model",
                "valid_from", "valid_until", "recorded_at", "superseded_at", "status",
                "domain", "source_created_at")
        vals = tuple(b.get(c) for c in cols)
        placeholders = ",".join("?" * len(cols))
        self.db.conn.execute(
            f"INSERT INTO blocks({','.join(cols)}) VALUES({placeholders}) "
            "ON CONFLICT(block_id) DO UPDATE SET "
            "text=excluded.text, context_header=COALESCE(excluded.context_header,blocks.context_header), "
            "block_sha256=excluded.block_sha256, heading_path=excluded.heading_path, "
            "block_content_id=excluded.block_content_id, "
            "embedded_model=COALESCE(excluded.embedded_model,blocks.embedded_model), "
            "ordinal=excluded.ordinal, char_start=excluded.char_start, char_end=excluded.char_end, "
            "domain=excluded.domain, source_created_at=excluded.source_created_at, "
            "valid_from=CASE WHEN excluded.source_created_at IS NOT NULL "
            "THEN excluded.valid_from ELSE blocks.valid_from END",
            vals,
        )

    def retire_block(self, block_id: str, reason: str = "source-removed",
                     successor_doc_id: str | None = None, *, edges: bool = True) -> dict[str, int]:
        """Retire source block without deleting its text, then close unsupported current claims.

        An edge anchored here moves to a block that still holds its quote: first a block that
        already corroborates it, then, when `successor_doc_id` is given, an active block of that
        document (the re-rendered note, or the document that replaced this one). Its edge id is
        kept (architecture.md 4.3, 7.6); only an edge with no such block is retracted. With
        `edges=False` only the block is retired: `withdraw_source` settles every edge itself, to a
        servable block only (architecture.md 10.2 step 4)."""
        row = self.db.conn.execute(
            "SELECT block_id FROM blocks WHERE block_id=? AND status='active'", (block_id,)
        ).fetchone()
        if not row:
            return {"blocks": 0, "reanchored": 0, "retracted": 0}
        now = self.clock()
        # Retirement is a knowledge-time fact (superseded_at), not an end of validity: valid_until
        # stays as it was, so a read at an earlier knowledge time still sees the block (architecture.md 5.2).
        self.db.conn.execute(
            "UPDATE blocks SET status='superseded', superseded_at=? "
            "WHERE block_id=? AND status='active'", (now, block_id)
        )
        stats = {"blocks": 1, "reanchored": 0, "retracted": 0}
        if not edges:
            return stats
        held = self.db.conn.execute(
            "SELECT edge_id, source_quote FROM edges WHERE source_block_id=? "
            "AND status='active' AND valid_until IS NULL", (block_id,)
        ).fetchall()
        for edge in held:
            alternatives = self.db.conn.execute(
                "SELECT ec.source_block_id,b.doc_id,b.text FROM edge_corroborations ec "
                "JOIN blocks b ON b.block_id=ec.source_block_id "
                "WHERE ec.edge_id=? AND ec.source_block_id<>? AND b.status='active' "
                "ORDER BY ec.source_block_id",
                (edge["edge_id"], block_id),
            ).fetchall()
            if successor_doc_id is not None:
                alternatives = list(alternatives) + [
                    {"source_block_id": r["block_id"], "doc_id": r["doc_id"], "text": r["text"]}
                    for r in self.db.conn.execute(
                        "SELECT block_id,doc_id,text FROM blocks WHERE doc_id=? AND block_id<>? "
                        "AND status='active' ORDER BY ordinal", (successor_doc_id, block_id))]
            alternative = next(
                (row for row in alternatives
                 if normalize_text(edge["source_quote"]) in normalize_text(row["text"])),
                None,
            )
            if alternative:
                self.reanchor_edge(
                    edge["edge_id"], alternative["source_block_id"],
                    alternative["doc_id"], edge["source_quote"],
                )
                self.db.conn.execute(
                    "INSERT OR IGNORE INTO edge_corroborations(edge_id, source_block_id, recorded_at) "
                    "VALUES(?,?,?)", (edge["edge_id"], alternative["source_block_id"], now),
                )
                self.db.conn.execute(
                    "DELETE FROM edge_corroborations WHERE edge_id=? AND source_block_id=?",
                    (edge["edge_id"], block_id),
                )
                self.db.conn.execute(
                    "UPDATE edges SET corroboration_count=(SELECT COUNT(*) FROM edge_corroborations "
                    "WHERE edge_id=?) WHERE edge_id=?", (edge["edge_id"], edge["edge_id"]),
                )
                stats["reanchored"] += 1
            else:
                self.retract_edge(edge["edge_id"], reason=reason, retracted_by="ingest")
                stats["retracted"] += 1
        return stats

    def clear_legacy_block_valid_until(self) -> int:
        """Repair, once and idempotently, the blocks the schema-3 `retire_block` closed: it wrote
        `valid_until = superseded_at`, which the as-of clause reads as the end of the block's validity
        and so hides the block from a read at an earlier knowledge time (OB-20, architecture.md 5.2).
        Clears `valid_until` where it equals `superseded_at` on a superseded block. Returns the
        row count; a second run finds none. It moves no edge, so it writes no `ingest_event`. Run by
        `wiki recover` inside a transaction, never in a hook: each row update is an FTS write
        (schema.sql blocks_au)."""
        if not self._tx_active:
            raise RuntimeError("block validity repair requires Writer.transaction")
        return self.db.conn.execute(
            "UPDATE blocks SET valid_until=NULL WHERE status='superseded' "
            "AND valid_until IS NOT NULL AND valid_until=superseded_at").rowcount

    def mark_knowledge_time_from_record(self) -> None:
        """Record in `meta` that this store's knowledge time comes from the record (OB-19): a
        rebuild lands every source with the Writer clock at the `ts` of its record event
        (architecture.md 10.2) and an import is such a rebuild (10.4), while live capture lags the
        event and no record event holds the live capture time. K-pinned reads on the store carry the
        trace note `knowledge-time-from-record` (engine/loop.py). Idempotent; never cleared, since a
        store once built from the record keeps those times."""
        if not self._tx_active:
            raise RuntimeError("store meta requires Writer.transaction")
        self.db.set_meta(self.db.KNOWLEDGE_TIME_KEY, "record")

    def claim_for_import(self, cutoff: int) -> bool:
        """Claim an empty store for an import (architecture.md 10.4; t-070 fix plan RC1), in the
        caller's transaction: whether the store is empty is read here, under the write lock, and when
        it is, the `meta` row `import_claim` is written with the export's record cutoff, so a second
        import, or a capture that would make the store hold something, finds it held. Returns False,
        writing nothing, when the store already holds something or a ledger is left from an earlier
        store."""
        from .db import holds_something
        if not self._tx_active:
            raise RuntimeError("store meta requires Writer.transaction")
        ledger = self.db.cfg.ledger_path
        if (ledger.exists() and ledger.stat().st_size > 0) or holds_something(self.db.conn):
            return False
        self.db.set_meta(self.db.IMPORT_CLAIM_KEY, str(int(cutoff)))
        return True

    #: The record act kinds that take content out of what the store serves (t-070 round 2 C-E). A
    #: `wiki-correction` is one only when its `result` is `applied`.
    REMOVAL_ACT_KINDS = ("wiki-withdrawal", "wiki-candidate-withdrawn", "wiki-correction")

    def record_removal_act(self, record, kind: str, data: dict) -> int:
        """The one writer of a content-removing record act (t-070 round 2 C-E): append it through
        `record(kind, data)` (the caller's `RecordAppender`) and advance the store's watermark
        (`DB.REMOVAL_ACTS_KEY`) to its id, in the caller's open transaction, before the caller
        applies the act. The record commits at the append and the store at the caller's commit, so a
        crash in between leaves an act the record holds and the store has not processed; the
        serving doors then answer `store-behind-record` until `wiki recover` applies it. The caller
        has checked, under the same write lock, that the store had processed every earlier act
        (`service.removal_acts_pending`), so the watermark never passes an act left unapplied."""
        from alpaca.wiki import faults
        if not self._tx_active:
            raise RuntimeError("a removal act is recorded inside Writer.transaction")
        if kind not in self.REMOVAL_ACT_KINDS:
            raise ValueError("not a removal act kind: %s" % kind)
        if kind == "wiki-correction" and (data or {}).get("result") != "applied":
            raise ValueError("only an applied correction is a removal act")
        event_id = record(kind, data)
        faults.boundary("act-recorded")
        self.advance_removal_watermark(event_id)
        return event_id

    def advance_removal_watermark(self, event_id: int) -> None:
        """Record in `meta` that this store has processed the removal act `event_id` and every one
        before it (`DB.REMOVAL_ACTS_KEY`), in the caller's transaction: the replay, after each act it
        processed, applied or skipped (`recovery.replay_candidates`). The value only grows."""
        if not self._tx_active:
            raise RuntimeError("store meta requires Writer.transaction")
        held = int(self.db.get_meta(self.db.REMOVAL_ACTS_KEY) or 0)
        if int(event_id) > held:
            self.db.set_meta(self.db.REMOVAL_ACTS_KEY, str(int(event_id)))

    def record_source_version(self, source_ref: str, doc_id: str, content_sha256: str, *,
                              origin_hash: str | None = None, operator: str | None = None,
                              session: str | None = None, op: str | None = None,
                              task: str | None = None, source_time: str | None = None,
                              private: int = 1) -> int:
        """One `source_versions` row per version of a source (FR-4, architecture.md 4.2), because
        `upsert_doc` overwrites a document's hash in place. The same document and bytes again only
        refresh the row; new bytes add the next `version_seq`, and the earlier `current` row becomes
        `replaced`. A `withdrawn` row stays as it is. Returns the version_seq written or refreshed."""
        if not self._tx_active:
            raise RuntimeError("source versions require Writer.transaction")
        conn = self.db.conn
        latest = conn.execute(
            "SELECT version_seq,doc_id,content_sha256 FROM source_versions WHERE source_ref=? "
            "ORDER BY version_seq DESC LIMIT 1", (source_ref,)).fetchone()
        if latest and latest["doc_id"] == doc_id and latest["content_sha256"] == content_sha256:
            conn.execute(
                "UPDATE source_versions SET origin_hash=COALESCE(?,origin_hash),operator=?,session=?,"
                "op=?,task=?,source_time=?,private=? WHERE source_ref=? AND version_seq=?",
                (origin_hash, operator, session, op, task, source_time, int(private),
                 source_ref, latest["version_seq"]))
            return latest["version_seq"]
        seq = (latest["version_seq"] if latest else 0) + 1
        conn.execute("UPDATE source_versions SET status='replaced' "
                     "WHERE source_ref=? AND status='current'", (source_ref,))
        conn.execute(
            "INSERT INTO source_versions(source_ref,version_seq,doc_id,content_sha256,origin_hash,"
            "operator,session,op,task,source_time,recorded_at,private,status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'current')",
            (source_ref, seq, doc_id, content_sha256, origin_hash, operator, session, op, task,
             source_time, self.clock(), int(private)))
        return seq

    def import_source_version(self, row: dict) -> bool:
        """Write the envelope of a withdrawn `wiki ingest` document from the row of a
        `rune2-portable/1` export (alpaca/wiki/portable.py; architecture.md 10.4). The record holds no
        copy of such a document and an export carries no text of a withdrawn source (6.2), so a
        rebuild cannot land it; its envelope is accepted state all the same (FR-4, `accepted_digest`).
        Clock-free: `recorded_at` is the row's `observed_at`, never the Writer clock. Accepted only
        when the record backs the withdrawal, that is an `exclusions` row (written by the replay of
        its `wiki-withdrawal` event) names the source, and only as a `withdrawn` version next to the
        versions already written. Returns True iff a row was written."""
        if not self._tx_active:
            raise RuntimeError("source versions require Writer.transaction")
        conn = self.db.conn
        ref = row.get("source_ref") or ""
        if (not ref.startswith("document:") or row.get("status") != "withdrawn"
                or not conn.execute("SELECT 1 FROM exclusions WHERE source_ref=?", (ref,)).fetchone()):
            return False
        if conn.execute("SELECT 1 FROM source_versions WHERE source_ref=? AND content_sha256=?",
                        (ref, row["content_sha256"])).fetchone():
            return False
        last = conn.execute("SELECT COALESCE(MAX(version_seq),0) FROM source_versions WHERE source_ref=?",
                            (ref,)).fetchone()[0]
        conn.execute(
            "INSERT INTO source_versions(source_ref,version_seq,doc_id,content_sha256,origin_hash,"
            "operator,session,op,task,source_time,recorded_at,private,status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'withdrawn')",
            (ref, last + 1, ref[len("document:"):], row["content_sha256"], row.get("origin_hash"),
             row.get("operator"), row.get("session"), row.get("op"), row.get("task"),
             row.get("source_time"), row.get("observed_at") or "", int(row.get("private", 1))))
        return True

    def retire_doc(self, doc_id: str, reason: str = "source-document-removed",
                   superseded_by: str | None = None) -> dict[str, int]:
        """Retire every active block of a document. `superseded_by` names the document that replaces
        it: an edge anchored here re-anchors to a block there that holds its quote (`retire_block`)."""
        total = {"blocks": 0, "reanchored": 0, "retracted": 0}
        block_ids = [r["block_id"] for r in self.db.conn.execute(
            "SELECT block_id FROM blocks WHERE doc_id=? AND status='active' ORDER BY block_id",
            (doc_id,),
        ).fetchall()]
        for block_id in block_ids:
            result = self.retire_block(block_id, reason=reason, successor_doc_id=superseded_by)
            for key in total:
                total[key] += result[key]
        return total

    def supersede_block(self, block_id: str) -> None:
        self.db.conn.execute(
            "UPDATE blocks SET superseded_at=?, status='superseded' WHERE block_id=? AND status='active'",
            (self.clock(), block_id),
        )

    # ---- nodes / aliases ------------------------------------------------
    def upsert_node(self, node_id, node_type="", display_name="", summary_block_id=None,
                    attrs=None) -> None:
        now = self.clock()
        self.db.conn.execute(
            "INSERT INTO nodes(node_id,node_type,display_name,summary_block_id,attrs,"
            "valid_from,recorded_at,status) VALUES(?,?,?,?,?,?,?, 'active') "
            "ON CONFLICT(node_id) DO UPDATE SET "
            "display_name=COALESCE(NULLIF(excluded.display_name,''), nodes.display_name), "
            "node_type=COALESCE(NULLIF(excluded.node_type,''), nodes.node_type)",
            (node_id, node_type, display_name or node_id, summary_block_id,
             canonical_json(attrs or {}), now, now),
        )
        self._event("upsert_node", None, {"node_id": node_id, "node_type": node_type})

    def add_alias(self, node_id, surface, norm_surface, kind="exact", status="bound",
                  confidence=1.0, source_block_id=None) -> None:
        self.db.conn.execute(
            "INSERT INTO aliases(node_id,surface,norm_surface,kind,status,confidence,"
            "source_block_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (node_id, surface, norm_surface, kind, status, confidence, source_block_id, self.clock()),
        )

    def add_node_block(self, node_id, block_id, role="mention", weight=1.0) -> None:
        self.db.conn.execute(
            "INSERT OR IGNORE INTO node_blocks(node_id,block_id,role,weight) VALUES(?,?,?,?)",
            (node_id, block_id, role, weight),
        )

    # ---- edges (the assertion) ------------------------------------------
    def upsert_edge(self, e: dict[str, Any]) -> str:
        """Idempotent on the content-derived edge_id. Returns edge_id. A pure re-anchor is a no-op."""
        now = self.clock()
        if not e.get("recorded_at"):
            e["recorded_at"] = now
        if not e.get("valid_from"):
            e["valid_from"] = now
        e.setdefault("corroboration_count", 1)
        e.setdefault("status", "active")
        e.setdefault("atom_type", "FACT")               # pc.3
        e.setdefault("learned_at", e.get("recorded_at"))  # km.4 capture date
        e.setdefault("volatile", 0)                     # km.6
        cols = ("edge_id", "subj_node", "predicate", "obj_node", "obj_literal", "obj_datatype",
                "source_block_id", "source_doc_id", "source_quote", "extractor", "extractor_version",
                "confidence", "reconcile_verdict", "corroboration_count", "recorded_at",
                "superseded_at", "valid_from", "valid_until", "supersedes_edge_id",
                "superseded_by_edge_id", "retraction_reason", "retracted_by", "status", "edge_tier",
                "atom_type", "learned_at", "source_created_at", "expires_at", "volatile")
        vals = tuple(e.get(c) for c in cols)
        placeholders = ",".join("?" * len(cols))
        # ON CONFLICT: same content-derived edge_id already present => identity-preserving no-op
        # (never a forged supersede, never a corroboration bump). Op-3-Keystone companion rule.
        self.db.conn.execute(
            f"INSERT INTO edges({','.join(cols)}) VALUES({placeholders}) "
            "ON CONFLICT(edge_id) DO NOTHING",
            vals,
        )
        # F9: record the originating source as the edge's first corroborator (idempotent).
        self.db.conn.execute(
            "INSERT OR IGNORE INTO edge_corroborations(edge_id, source_block_id, recorded_at) "
            "VALUES(?,?,?)", (e["edge_id"], e["source_block_id"], now),
        )
        self._event("upsert_edge", e.get("source_doc_id"),
                    {"edge_id": e["edge_id"], "subj": e["subj_node"], "pred": e["predicate"],
                     "obj_key_sha256": sha256_hex(
                         str(e.get("obj_node") or e.get("obj_literal") or "")
                     )})
        return e["edge_id"]

    def bump_corroboration(self, edge_id: str, source_block_id: str) -> bool:
        """'matches' verdict: same object, NEW distinct source. Idempotent BY CONSTRUCTION (F9):
        a source that already corroborated this edge is a no-op - corroboration cannot inflate on
        re-ingest or a re-chunk that re-emits the same source. Returns True iff it actually bumped."""
        cur = self.db.conn.execute(
            "INSERT OR IGNORE INTO edge_corroborations(edge_id, source_block_id, recorded_at) "
            "VALUES(?,?,?)", (edge_id, source_block_id, self.clock()),
        )
        if cur.rowcount == 0:
            return False                       # already corroborated by this source - no inflation
        self.db.conn.execute(
            "UPDATE edges SET corroboration_count = corroboration_count + 1 WHERE edge_id=?",
            (edge_id,),
        )
        return True

    def reanchor_edge(self, edge_id: str, source_block_id: str, source_doc_id, source_quote) -> bool:
        """F2: on a re-chunk the content-derived edge_id is stable but its positional source_block_id
        can point at a block whose text has shifted. Re-point the citation anchor to the CURRENT
        block so the answer->claim->source trace stays live. Returns True iff it moved."""
        row = self.db.conn.execute(
            "SELECT source_block_id FROM edges WHERE edge_id=?", (edge_id,)).fetchone()
        if not row or row["source_block_id"] == source_block_id:
            return False
        self.db.conn.execute(
            "UPDATE edges SET source_block_id=?, source_doc_id=?, source_quote=? WHERE edge_id=?",
            (source_block_id, source_doc_id, source_quote, edge_id),
        )
        self._event("upsert_edge", source_doc_id,
                    {"edge_id": edge_id, "reanchor_to": source_block_id,
                     "source_doc_id": source_doc_id,
                     "source_quote_sha256": sha256_hex(source_quote)})
        return True

    # ---- candidate rows (architecture.md 7.1, 7.3) ----------------------
    def write_candidate(self, row: dict[str, Any], anchors: list[dict[str, Any]]) -> bool:
        """The `candidates` row of a proposal and its `candidate_anchors` rows, in the caller's
        transaction. A new id inserts. A row whose status is `refused` is rewritten by a later
        proposal of the same id (the refusal may have been transient, dec-38940e1fb35f Q12): the
        row keeps its `created_at`, takes the new check results, body, event and anchors, and an
        earlier context entry the new proposal does not repeat is retired by a negative ordinal with
        its quote cleared (anchor rows are append-only, a trigger refuses their deletion). Any
        other status is left as it is. Returns True iff a row was written."""
        if not self._tx_active:
            raise RuntimeError("candidate rows require Writer.transaction")
        conn = self.db.conn
        from .. import lifecycle
        cid = row["candidate_id"]
        old = conn.execute("SELECT status,created_at FROM candidates WHERE candidate_id=?",
                           (cid,)).fetchone()
        if not lifecycle.propose(old).go:
            return False
        lifecycle.check_move(None if old is None else old["status"], row["status"])
        cols = ("kind", "body_json", "body_sha256", "proposer", "proposer_kind", "proposer_operator",
                "record_event_id", "checks_json", "status", "verdict", "prior_edge_id", "updated_at")
        if old is None:
            conn.execute(
                "INSERT INTO candidates(candidate_id,%s,created_at) VALUES(?,%s,?)"
                % (",".join(cols), ",".join("?" * len(cols))),
                (cid,) + tuple(row.get(c) for c in cols) + (row["created_at"],))
        else:
            conn.execute("UPDATE candidates SET %s WHERE candidate_id=?"
                         % ",".join("%s=?" % c for c in cols),
                         tuple(row.get(c) for c in cols) + (cid,))
        for ordinal, a in enumerate(anchors):
            conn.execute(
                "INSERT INTO candidate_anchors(candidate_id,ordinal,source_ref,quote,quote_sha256,"
                "role,doc_id,block_content_id,block_sha256) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(candidate_id,ordinal) DO UPDATE SET source_ref=excluded.source_ref,"
                "quote=excluded.quote,quote_sha256=excluded.quote_sha256,role=excluded.role,"
                "doc_id=excluded.doc_id,block_content_id=excluded.block_content_id,"
                "block_sha256=excluded.block_sha256",
                (cid, ordinal, a["source_ref"], a.get("quote"), a["quote_sha256"], a["role"],
                 a.get("doc_id"), a.get("block_content_id"), a.get("block_sha256")))
        if old is not None:
            for left in conn.execute("SELECT ordinal FROM candidate_anchors WHERE candidate_id=? "
                                     "AND ordinal>=? ORDER BY ordinal", (cid, len(anchors))).fetchall():
                low = conn.execute("SELECT MIN(ordinal) FROM candidate_anchors WHERE candidate_id=?",
                                   (cid,)).fetchone()[0]
                conn.execute("UPDATE candidate_anchors SET ordinal=?, quote=NULL "
                             "WHERE candidate_id=? AND ordinal=?",
                             (min(low, 0) - 1, cid, left["ordinal"]))
        return True

    def set_candidate_status(self, candidate_id: str, status: str, *, admission_ref: str | None = None,
                             admission_effect: str | None = None,
                             admission_event_id: int | None = None) -> None:
        """Move a candidate row along the lifecycle of architecture.md 7.3 in the caller's
        transaction; the admission fields are only set, never cleared. The row's status is read here,
        under the write lock, and a move the one transition table does not hold
        (`lifecycle.MOVES`) raises `lifecycle.IllegalTransition` and writes nothing (t-070 RC3)."""
        from .. import lifecycle
        if not self._tx_active:
            raise RuntimeError("candidate rows require Writer.transaction")
        held = self.db.conn.execute("SELECT status FROM candidates WHERE candidate_id=?",
                                    (candidate_id,)).fetchone()
        lifecycle.check_move(None if held is None else held["status"], status)
        self.db.conn.execute(
            "UPDATE candidates SET status=?, admission_ref=COALESCE(?,admission_ref), "
            "admission_effect=COALESCE(?,admission_effect), "
            "admission_event_id=COALESCE(?,admission_event_id), updated_at=? WHERE candidate_id=?",
            (status, admission_ref, admission_effect, admission_event_id, self.clock(), candidate_id))

    # ---- fact admission and withdrawal (architecture.md 5.1, 7.4, 7.6) ---
    def plan_candidate_edge(self, fact, block: dict, quote: str) -> dict:
        """What admitting an agent fact would do, written nowhere: the time by architecture.md 5.1,
        the nodes its surfaces resolve to, the content-derived edge id and the verdict of
        `reconcile.classify` on the current store. `refused` names why it cannot be admitted:
        `ambiguous-entity`, `valid-time-unreadable`, or `valid-time-required` (OB-05: a
        single-cardinality fact of unknown time that contradicts the current edge of its key, or that
        a capture-time proxy would have back-filled from a later edge, is never placed on the time
        axis)."""
        from .. import intake
        from ..ingest import reconcile
        from ..ingest.resolve import resolve_surface
        ids = {"subject": resolve_surface(self.db, fact.subject)}
        if fact.obj_datatype == "node":
            ids["object"] = resolve_surface(self.db, fact.obj)
        if any(len(nodes) > 1 for nodes in ids.values()):
            return {"refused": "ambiguous-entity"}
        cand = intake.fact_edge(fact, ids, block, quote)
        if cand is None:
            return {"refused": "valid-time-unreadable"}
        v = reconcile.classify(self.db, cand, cand["edge_id"])
        plan = {"refused": None, "cand": cand, "verdict": v.verdict, "prior_edge_id": v.prior_edge_id,
                "world_change": v.world_change,
                "surfaces": {"subject": fact.subject, "object": fact.obj}}
        if v.verdict == "duplicate" and v.prior_edge_id:
            # The fact's content-derived edge exists but is no longer active (retracted by a candidate
            # withdrawal or a correction, invalidated by a supersede, quarantined): a claim on it would
            # be `applied` with no live edge (review finding B-1). Refused with the edge named; a
            # retraction stays a retraction (architecture.md 7.4, 7.6).
            held = self.db.conn.execute("SELECT status FROM edges WHERE edge_id=?",
                                        (v.prior_edge_id,)).fetchone()
            if held is not None and held["status"] != "active":
                plan.update(refused="edge-not-active", edge_status=held["status"])
                return plan
        pred = self.db.conn.execute("SELECT cardinality FROM predicates WHERE predicate=?",
                                    (fact.predicate,)).fetchone()
        if (pred is not None and pred["cardinality"] == "single"
                and not reconcile.time_known(cand)):
            later = any(reconcile.time_known(dict(r)) for r in self.db.conn.execute(
                "SELECT * FROM edges WHERE subj_node=? AND predicate=? AND status='active' "
                "AND valid_from>?", (cand["subj_node"], fact.predicate, cand["valid_from"])))
            if v.verdict == "contradicts" or (v.verdict in ("novel", "refines") and later):
                plan["refused"] = "valid-time-required"
        return plan

    def apply_candidate_edge(self, plan: dict, *, candidate_id: str, anchor_ref: str, quote: str,
                             fact_kind: str, block: dict) -> dict:
        """Write what `plan_candidate_edge` found, in the caller's transaction (the only writer of an
        active `extractor='llm'` edge besides the replay of an applied admission and a matching
        import, architecture.md 7.4). `novel` and `refines` place the edge, `contradicts` places it
        and supersedes its prior (a world change when the verdicts says so, else a correction),
        `matches` adds the anchor block as one more corroborator, `duplicate` writes no edge. The
        support claim `candidate:<id>` is written in every case and nothing is deleted. Returns the
        effect and the net of it that `withdraw_candidate_effects` undoes."""
        from ..ingest.resolve import ensure_node
        if not self._tx_active:
            raise RuntimeError("admission requires Writer.transaction")
        cand, verdict, prior_id = plan["cand"], plan["verdict"], plan["prior_edge_id"]
        eid = cand["edge_id"]
        surfaces = plan["surfaces"]
        net = {"edge_id": eid, "wrote_edge": False, "wrote_corroboration": False, "prior": None,
               "world_change": False, "valid_from": cand["valid_from"],
               "source_created_at": cand["source_created_at"]}
        if verdict in ("novel", "refines", "contradicts"):
            row = dict(cand)
            row.update(extractor="llm", extractor_version="agent-candidate/1", status="active",
                       reconcile_verdict=verdict)
            if verdict == "refines":
                row["supersedes_edge_id"] = prior_id          # lineage only; both stay active
            ensure_node(self.db, self, cand["subj_node"], surfaces["subject"], block["block_id"])
            if cand["obj_datatype"] == "node":
                ensure_node(self.db, self, cand["obj_node"], surfaces["object"], block["block_id"])
            self.upsert_edge(row)
            self.add_node_block(cand["subj_node"], block["block_id"], role="claim")
            if cand["obj_datatype"] == "node":
                self.add_node_block(cand["obj_node"], block["block_id"], role="claim")
            net.update(wrote_edge=True, wrote_corroboration=True)
            effect = "edge-written"
            if verdict == "contradicts" and prior_id:
                before = self.db.conn.execute(
                    "SELECT * FROM edges WHERE edge_id=?", (prior_id,)).fetchone()
                from .. import intake
                net["prior"] = {"edge_id": prior_id, "status": before["status"],
                                "valid_until": before["valid_until"],
                                "superseded_at": before["superseded_at"],
                                "identity": intake.fact_identity(self.db, before)}
                net["world_change"] = bool(plan["world_change"])
                self.supersede_edge(prior_id, eid, world_change=net["world_change"],
                                    world_at=cand["valid_from"])
                effect = "superseded-prior"
            held = eid
        elif verdict == "matches":
            bumped = self.bump_corroboration(prior_id, block["block_id"])
            held, effect = prior_id, "corroborated" if bumped else "claim-only"
            net.update(edge_id=held, wrote_corroboration=bumped)
        else:
            held, effect = prior_id, "claim-only"
            net["edge_id"] = held
        self.add_edge_origin(held, "candidate:" + candidate_id, source_ref=anchor_ref, quote=quote,
                             origin="candidate", candidate_id=candidate_id, fact_kind=fact_kind)
        return {"effect": effect, "edge_id": held, "verdict": verdict, "net": net, "cand": cand}

    def admit_candidate_edge(self, *, candidate_id: str, fact, block: dict, anchor_ref: str,
                             quote: str, fact_kind: str) -> dict:
        """Admit one agent fact (architecture.md 7.4): plan it, then write it. The caller has run
        K-1 to K-5 again on the current store and holds the transaction. Returns `result`
        `applied` with the effect, or `refused` with the reason, writing nothing."""
        plan = self.plan_candidate_edge(fact, block, quote)
        if plan["refused"]:
            out = {"result": "refused", "reason": plan["refused"]}
            if plan.get("edge_status"):
                out.update(edge_id=plan["prior_edge_id"], edge_status=plan["edge_status"])
            return out
        out = self.apply_candidate_edge(plan, candidate_id=candidate_id, anchor_ref=anchor_ref,
                                        quote=quote, fact_kind=fact_kind, block=block)
        out.update(result="applied", reason=None, prior_edge_id=plan["prior_edge_id"])
        return out

    def revert_supersede(self, prior_edge_id: str, *, successor_edge_id: str, status: str = "active",
                         valid_until: str | None = None, superseded_at: str | None = None,
                         boundary: str | None = None) -> bool:
        """Undo a supersede, and only what that supersede did. A correction (`boundary` None) made the
        prior `invalidated` and named the successor in `superseded_by_edge_id`: the prior returns to
        the `status` and `superseded_at` it had before. A world change (`boundary` the instant it
        ended the window at) left `valid_until` at `boundary`: the prior returns to the `valid_until`
        it had before and keeps its `status`, since an edge that a later correction invalidated is
        still invalidated by it. Only a prior that the successor still holds is touched, and a
        retracted one is never revived. Returns True iff the prior moved."""
        if not self._tx_active:
            raise RuntimeError("revert requires Writer.transaction")
        row = self.db.conn.execute(
            "SELECT status,valid_until,superseded_by_edge_id FROM edges WHERE edge_id=?",
            (prior_edge_id,)).fetchone()
        if row is None or row["status"] == "retracted":
            return False
        if boundary is None:
            if not (row["status"] == "invalidated" and row["superseded_by_edge_id"] == successor_edge_id):
                return False
            self.db.conn.execute(
                "UPDATE edges SET status=?, superseded_at=?, superseded_by_edge_id=NULL WHERE edge_id=?",
                (status, superseded_at, prior_edge_id))
        else:
            if row["valid_until"] != boundary:
                return False
            self.db.conn.execute("UPDATE edges SET valid_until=? WHERE edge_id=?",
                                 (valid_until, prior_edge_id))
        self._event("supersede_edge", None, {"prior": prior_edge_id, "successor": successor_edge_id,
                                             "reverted": True})
        return True

    def _drop_corroboration(self, edge_id: str, block_id: str) -> None:
        self.db.conn.execute("DELETE FROM edge_corroborations WHERE edge_id=? AND source_block_id=?",
                             (edge_id, block_id))
        self.db.conn.execute(
            "UPDATE edges SET corroboration_count=(SELECT COUNT(*) FROM edge_corroborations "
            "WHERE edge_id=?) WHERE edge_id=?", (edge_id, edge_id))

    # ---- source withdrawal (architecture.md 10.2, FR-17, FR-22) -----------
    def _supersessions_of(self, successor_edge_id: str) -> list[dict]:
        """The `supersede_edge` events that name this successor and were not reverted since, oldest
        first: what the successor ended or invalidated, with the prior's state from before."""
        live: dict[str, dict] = {}
        for row in self.db.conn.execute(
                "SELECT payload FROM ingest_event WHERE op='supersede_edge' AND instr(payload,?)>0 "
                "ORDER BY seq", (successor_edge_id,)):
            payload = json.loads(row["payload"])
            if payload.get("successor") != successor_edge_id or not payload.get("prior"):
                continue
            if payload.get("reverted"):
                live.pop(payload["prior"], None)
            else:
                live[payload["prior"]] = payload
        return list(live.values())

    @staticmethod
    def _holds_value_of(prior, later) -> bool:
        """The refines rule of `reconcile.classify` for an edge that arrives after `prior`: a literal
        object that holds the prior's value and is not equal to it (`ingest/reconcile.py`). Such a
        pair stays active together; a less specific value arriving later contradicts instead."""
        pv = prior["obj_node"] or prior["obj_literal"] or ""
        cv = later["obj_node"] or later["obj_literal"] or ""
        return bool(later["obj_datatype"] != "node" and pv and cv and pv in cv and cv != pv)

    def _ends(self, prior, later) -> bool:
        """Whether `later`, which starts after `prior`, ends it by the reconcile rule. It does unless it
        refines `prior`: it is stored as the refinement of `prior` (`reconcile_verdict` `refines`,
        `supersedes_edge_id` naming it), or it holds the value of `prior` (`_holds_value_of`) and stands
        later in the record (`_arrival`), so that the two met as `reconcile.classify` meets an arriving
        edge with its prior (a store that never held a withdrawn edge has an edge that was placed
        against it meet `prior` instead). An edge that stood in the store before `prior` arrived ended
        it whatever its value: classify back-fills an arriving edge's window to the next dated edge."""
        if later["reconcile_verdict"] == "refines" and later["supersedes_edge_id"] == prior["edge_id"]:
            return False
        if self._holds_value_of(prior, later) and (
                (self._arrival(later["edge_id"]), later["edge_id"]) > (self._arrival(prior["edge_id"]), prior["edge_id"])):
            return False
        return True

    def _next_live_edge(self, prior) -> dict | None:
        """The edge that ends `prior` by the reconcile rule: on a single-valued key, the nearest later
        edge that has a time that can order history (OB-05) and is active or invalidated (a later
        same-instant correction of an edge does not give back the window that edge ended when it
        arrived), and that does not refine `prior` (`_ends`). Whatever its object otherwise: a later
        move to the state the log already holds starts its own edge and ends the earlier one (the
        rule of `encode._later_same_state`), so a store that never held an edge in between ends the
        earlier edge at that same instant. A refinement never ends the edge it refines; it ends the
        others, as `reconcile.classify` also ends an earlier-arriving edge at the start of a later
        refinement when it back-fills a window."""
        from ..ingest import reconcile
        conn = self.db.conn
        if prior is None or prior["status"] != "active" or not reconcile.time_known(dict(prior)):
            return None
        pred = conn.execute("SELECT cardinality FROM predicates WHERE predicate=?",
                            (prior["predicate"],)).fetchone()
        if pred is None or pred["cardinality"] != "single":
            return None
        later = [dict(r) for r in conn.execute(
            "SELECT * FROM edges WHERE subj_node=? AND predicate=? AND status IN ('active','invalidated') "
            "AND edge_id<>? AND valid_from>? ORDER BY valid_from, edge_id",
            (prior["subj_node"], prior["predicate"], prior["edge_id"], prior["valid_from"]))]
        later = [e for e in later if reconcile.time_known(e) and self._ends(prior, e)]
        return later[0] if later else None

    #: The reasons an edge is retracted when the thing that made it is withdrawn, so that the windows
    #: it ended come back (a source or a candidate withdrawn, a correction reverted). An owner
    #: `retract` is not one of them: it leaves the priors alone.
    _WITHDRAWN_REASONS = ("source-withdrawn", "candidate-withdrawn", "correction-reverted")

    def _settle_end(self, edge_id: str) -> bool:
        """After an edge was restored, or its window was closed by an edge since withdrawn: give its
        window the end that a store which never held the withdrawn edge gives it, the `valid_from` of
        the next live edge of the key (`_next_live_edge`) or no end when none follows (OB-11).

        * Open, or running past the next live edge: it ends at that edge. A restore gives a prior the
          end its supersede event recorded, which a later edge may since have cut short.
        * Ending before the next live edge, or with none after it: the end is given up only when a
          withdrawn edge starts there. `reconcile.classify` writes such an end when an edge arrives
          after a later one (the later edge's `valid_from` becomes its `valid_until`, with no event
          that names the later edge as successor), and `supersede_edge` keeps it in the `prior_before`
          of its event, so a restore can bring it back. An end that no withdrawn edge accounts for is
          left as it is.

        Returns True iff the window changed."""
        from ..ingest import reconcile
        conn = self.db.conn
        edge = conn.execute("SELECT * FROM edges WHERE edge_id=?", (edge_id,)).fetchone()
        if edge is None or edge["status"] != "active" or not reconcile.time_known(dict(edge)):
            return False
        pred = conn.execute("SELECT cardinality FROM predicates WHERE predicate=?",
                            (edge["predicate"],)).fetchone()
        if pred is None or pred["cardinality"] != "single":
            return False
        later = self._next_live_edge(edge)
        target = later["valid_from"] if later else None
        now = edge["valid_until"]
        if now == target:
            return False
        if now is None or (target is not None and target < now):
            self.supersede_edge(edge_id, later["edge_id"], world_change=True, world_at=target,
                                keep_lineage=True)
            return True
        gone = [r for r in conn.execute(
            "SELECT * FROM edges WHERE subj_node=? AND predicate=? AND status='retracted' "
            "AND valid_from=? ORDER BY edge_id", (edge["subj_node"], edge["predicate"], now))
            if r["retraction_reason"] in self._WITHDRAWN_REASONS and reconcile.time_known(dict(r))]
        if not gone:
            return False
        conn.execute("UPDATE edges SET valid_until=? WHERE edge_id=?", (target, edge_id))
        self._event("supersede_edge", None, {"prior": edge_id, "successor": gone[0]["edge_id"],
                                             "reverted": True, "backfilled": True})
        if later is not None:
            self.supersede_edge(edge_id, later["edge_id"], world_change=True, world_at=target,
                                keep_lineage=True)
        return True

    def _recorrect_by_successor(self, prior_edge_id: str, withdrawn_edge_id: str) -> bool:
        """A prior that a same-instant correction had invalidated is active again once the corrector
        is withdrawn. If the withdrawn edge had been corrected in its turn at that same instant, the
        prior is corrected by that edge instead, as a store that never held the withdrawn edge would
        have it: the corrections of one instant stay one chain, and two edges that start together are
        never both current. The corrector is the next edge of the chain that is not withdrawn, live or
        itself invalidated; a corrector that refines the prior (`_holds_value_of`) leaves it active, as
        `reconcile.classify` would have. Returns True iff the prior was corrected."""
        conn = self.db.conn
        prior = conn.execute("SELECT * FROM edges WHERE edge_id=?", (prior_edge_id,)).fetchone()
        gone = conn.execute("SELECT * FROM edges WHERE edge_id=?", (withdrawn_edge_id,)).fetchone()
        if prior is None or gone is None or prior["status"] != "active":
            return False
        seen: set[str] = set()
        step = gone["superseded_by_edge_id"]
        while step and step not in seen:
            seen.add(step)
            nxt = conn.execute("SELECT * FROM edges WHERE edge_id=?", (step,)).fetchone()
            if nxt is None:
                return False
            if nxt["status"] == "retracted":
                step = nxt["superseded_by_edge_id"]
                continue
            if nxt["status"] not in ("active", "invalidated") or (
                    nxt["subj_node"], nxt["predicate"], nxt["valid_from"]) != (
                    prior["subj_node"], prior["predicate"], prior["valid_from"]):
                return False
            if self._holds_value_of(prior, nxt):
                # Without the withdrawn edge the successor meets the prior itself, and refines it:
                # `reconcile.classify` keeps both active (check round 2, invariants B2).
                return False
            self.supersede_edge(prior_edge_id, nxt["edge_id"], world_change=False, keep_lineage=True)
            return True
        return False

    def _pass_correction_to_refined(self, withdrawn_edge_id: str) -> list[str]:
        """A refinement and the edge it refines start at one instant and are both active; a later
        value of that instant meets one of them (`reconcile.classify` takes one prior) and corrects
        it. When the one it met is withdrawn, a store that never held that edge has the value meet
        its partner instead and correct it: the correction passes to the active partner of the
        withdrawn edge at that instant (the edge it refines, or an edge that refines it), unless the
        value refines the partner in its turn (`_holds_value_of`). Returns the edges corrected."""
        conn = self.db.conn
        gone = conn.execute("SELECT * FROM edges WHERE edge_id=?", (withdrawn_edge_id,)).fetchone()
        if gone is None or not gone["superseded_by_edge_id"]:
            return []
        here = (gone["subj_node"], gone["predicate"], gone["valid_from"])
        corrector = conn.execute("SELECT * FROM edges WHERE edge_id=?",
                                 (gone["superseded_by_edge_id"],)).fetchone()
        if (corrector is None or corrector["status"] not in ("active", "invalidated")
                or (corrector["subj_node"], corrector["predicate"], corrector["valid_from"]) != here):
            return []
        partners = []
        if gone["reconcile_verdict"] == "refines" and gone["supersedes_edge_id"]:
            partners += [dict(r) for r in conn.execute("SELECT * FROM edges WHERE edge_id=?",
                                                       (gone["supersedes_edge_id"],))]
        partners += [dict(r) for r in conn.execute(
            "SELECT * FROM edges WHERE supersedes_edge_id=? AND reconcile_verdict='refines' "
            "ORDER BY edge_id", (withdrawn_edge_id,))]
        corrected = []
        for partner in partners:
            if (partner["status"] == "active" and partner["edge_id"] != corrector["edge_id"]
                    and (partner["subj_node"], partner["predicate"], partner["valid_from"]) == here
                    and not self._holds_value_of(partner, corrector)):
                self.supersede_edge(partner["edge_id"], corrector["edge_id"], world_change=False,
                                    keep_lineage=True)
                corrected.append(partner["edge_id"])
        return corrected

    def _arrival(self, edge_id: str) -> int:
        """Where an edge stands in the record: the id of the record event that its live claim rests on
        (the note a rule read, a correction) or, for an agent fact, of the admission that placed it.
        0 when neither is known."""
        conn = self.db.conn
        row = conn.execute("SELECT MAX(source_event_id) FROM edge_origin WHERE edge_id=? AND status='live'",
                           (edge_id,)).fetchone()
        if row and row[0] is not None:
            return int(row[0])
        row = conn.execute(
            "SELECT MAX(c.admission_event_id) FROM edge_origin o JOIN candidates c "
            "ON c.candidate_id=o.candidate_id WHERE o.edge_id=? AND o.status='live'", (edge_id,)).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    @staticmethod
    def _refinement_kin(group: list[dict], edge_id: str) -> set[str]:
        """The edges of `group` (one key, one instant) that `reconcile.classify` keeps active beside
        `edge_id`: a refinement (`reconcile_verdict` `refines`) names the edge it refines in
        `supersedes_edge_id` and both stay active (`ingest/reconcile.py`, `encode_candidate`,
        `apply_candidate_edge`). Read both ways and through a chain of refinements; `edge_id` itself
        is in the set."""
        ids = {e["edge_id"] for e in group}
        links: dict[str, set] = {}
        for e in group:
            if e["reconcile_verdict"] == "refines" and e["supersedes_edge_id"] in ids:
                links.setdefault(e["edge_id"], set()).add(e["supersedes_edge_id"])
                links.setdefault(e["supersedes_edge_id"], set()).add(e["edge_id"])
        kin, todo = {edge_id}, [edge_id]
        while todo:
            for other in links.get(todo.pop(), ()):
                if other not in kin:
                    kin.add(other)
                    todo.append(other)
        return kin

    def _one_survivor(self, subj_node: str, predicate: str, given_back) -> list[str]:
        """Edges whose standing a restore changed (`given_back`: active again by an undone correction, or
        without the edge they refined) are not left current beside another value that starts at the same
        instant: the edge that stands latest in the record among the active edges of that instant
        (`_arrival`, then the edge id) survives, and each given-back edge is corrected by it. A
        restore can bring several such edges back at once, when the edge that had corrected them is
        withdrawn. Exceptions, so that a withdrawal changes only what it undoes (architecture.md 7.6,
        OB-11): a given-back edge that is a refinement of the survivor, or that the survivor refines
        (`_refinement_kin`, or the survivor holds its value, `_holds_value_of`), stays active, as
        `reconcile.classify` keeps such a pair; and an edge the restore did not give back is never
        corrected here, whatever starts beside it. Returns the edges corrected."""
        from ..ingest import reconcile
        conn = self.db.conn
        given_back = set(given_back)
        if not given_back:
            return []
        pred = conn.execute("SELECT cardinality FROM predicates WHERE predicate=?", (predicate,)).fetchone()
        if pred is None or pred["cardinality"] != "single":
            return []
        groups: dict[str, list[dict]] = {}
        for r in conn.execute("SELECT * FROM edges WHERE subj_node=? AND predicate=? AND status='active' "
                              "ORDER BY valid_from, edge_id", (subj_node, predicate)):
            if reconcile.time_known(dict(r)):
                groups.setdefault(r["valid_from"], []).append(dict(r))
        corrected: list[str] = []
        for group in groups.values():
            if len(group) < 2 or not any(e["edge_id"] in given_back for e in group):
                continue
            winner = max(group, key=lambda e: (self._arrival(e["edge_id"]), e["edge_id"]))
            kin = self._refinement_kin(group, winner["edge_id"])
            for edge in group:
                if (edge["edge_id"] in given_back and edge["edge_id"] not in kin
                        and not self._holds_value_of(edge, winner)):
                    self.supersede_edge(edge["edge_id"], winner["edge_id"], world_change=False,
                                        keep_lineage=True)
                    corrected.append(edge["edge_id"])
        return corrected

    def _settle_key(self, subj_node: str, predicate: str) -> list[str]:
        """Settle the window of every active edge of one key, oldest first (`_settle_end`), once every
        edge a withdrawal retracts is retracted and every prior it gave back is back: an edge sees the
        live edges that remain, whatever order the restore met them in. Returns the edges whose
        window changed."""
        pred = self.db.conn.execute("SELECT cardinality FROM predicates WHERE predicate=?",
                                    (predicate,)).fetchone()
        if pred is None or pred["cardinality"] != "single":
            return []
        ids = [r["edge_id"] for r in self.db.conn.execute(
            "SELECT edge_id FROM edges WHERE subj_node=? AND predicate=? AND status='active' "
            "ORDER BY valid_from, edge_id", (subj_node, predicate))]
        return [edge_id for edge_id in ids if self._settle_end(edge_id)]

    def restore_priors(self, retracted_edge_ids, *, given_back=()) -> list[str]:
        """Give back each prior that a retracted edge had superseded or ended (architecture.md 10.2
        step 5, OB-11): what its supersede event recorded is undone (`revert_supersede`); a prior that a
        same-instant correction had invalidated goes to the next edge of that chain that is not
        withdrawn (`_recorrect_by_successor`); edges given back at one instant are left with one
        survivor and its refinements (`_one_survivor`); and the window of every active edge of each
        key the retracted edges sat on is settled against the live edges that remain (`_settle_key`);
        a correction that a later value of one instant made to a withdrawn edge passes to its
        refinement partner there (`_pass_correction_to_refined`). The aim is the store a record
        without the retracted edges would have built, whatever order the notes landed in; an edge the
        retracted edges did not touch keeps its status, those partners apart. It falls short where what
        `reconcile.classify` placed depended on edges that are gone in a way a restore cannot replay:
        at an instant where several values were active together (a refinement pair, or one fact
        written from several notes) the value a later edge met was the one with the smaller edge id,
        and an edge moved onto another claim keeps its id and its valid time (architecture.md 7.6).
        Refinements of a retracted edge at its instant join the one-survivor rule. `given_back` names
        priors the caller made active again before this call (`withdraw_candidate_effects`). Latest edge
        first, so a chain unwinds one link at a time. Returns the priors and other edges that moved."""
        if not self._tx_active:
            raise RuntimeError("restoring priors requires Writer.transaction")
        conn = self.db.conn
        marks = ",".join("?" * len(retracted_edge_ids))
        order = [r["edge_id"] for r in conn.execute(
            "SELECT edge_id FROM edges WHERE edge_id IN (%s) ORDER BY valid_from DESC, edge_id" % marks,
            tuple(retracted_edge_ids))] if retracted_edge_ids else []
        restored: list[str] = []
        # The edges whose standing the withdrawal changes at their instant: made active again by an undone
        # correction (not a window given back), or left without the edge they refined there, so that in a
        # store that never held it they meet each other as `reconcile.classify` meets arriving edges.
        revived = set(given_back)
        keys: list[tuple] = []
        for eid in order:
            gone = conn.execute("SELECT subj_node, predicate, valid_from FROM edges WHERE edge_id=?",
                                (eid,)).fetchone()
            if gone is not None and (gone[0], gone[1]) not in keys:
                keys.append((gone[0], gone[1]))
            if gone is not None:
                revived.update(r[0] for r in conn.execute(
                    "SELECT edge_id FROM edges WHERE supersedes_edge_id=? AND reconcile_verdict='refines' "
                    "AND status='active' AND subj_node=? AND predicate=? AND valid_from=?",
                    (eid, gone[0], gone[1], gone[2])))
            reverted = []
            for made in self._supersessions_of(eid):
                before = made.get("prior_before") or {}
                if self.revert_supersede(
                        made["prior"], successor_edge_id=eid, status=before.get("status") or "active",
                        valid_until=before.get("valid_until"), superseded_at=before.get("superseded_at"),
                        boundary=made.get("world_at") if made.get("world_change") else None):
                    reverted.append((made["prior"], made))
            for prior, made in reverted:
                if not made.get("world_change"):
                    revived.add(prior)
                    self._recorrect_by_successor(prior, eid)
                restored.append(prior)
            restored.extend(e for e in self._pass_correction_to_refined(eid) if e not in restored)
        for subj_node, predicate in keys:
            self._one_survivor(subj_node, predicate, revived)
            restored.extend(e for e in self._settle_key(subj_node, predicate) if e not in restored)
        return restored

    def _docs_of_refs(self, refs) -> list[str]:
        """The documents that carry these sources: those of their `source_versions` rows, else the
        event note of `event:<id>` (a note captured before schema 4 has no row), the document a
        `document:` ref names, or the documents whose header names the source (a transcript or a
        source copy captured before schema 4 has no row either: `servable.source_refs_for_docs`
        finds it by that header, so withdrawal has to as well)."""
        conn = self.db.conn
        docs: set[str] = set()
        for ref in sorted(refs):
            found = [r[0] for r in conn.execute(
                "SELECT DISTINCT doc_id FROM source_versions WHERE source_ref=?", (ref,))]
            if not found and ref.startswith("event:") and ref[6:].isdigit():
                found = [r[0] for r in conn.execute(
                    "SELECT doc_id FROM docs WHERE doc_id LIKE ? AND doc_id NOT LIKE 'raw/sources/%'",
                    ("%%raw/%%/%06d-%%.md" % int(ref[6:]),))]
            if not found and ref.startswith("document:"):
                found = [r[0] for r in conn.execute(
                    "SELECT doc_id FROM docs WHERE doc_id=?", (ref[9:],))]
            if not found:
                found = self._docs_by_header(ref)
            docs.update(found)
        return sorted(docs)

    def _docs_by_header(self, ref: str) -> list[str]:
        """The captured documents (`raw/...`) whose header names this source, by the one reader of
        that header (`drain.projection_identity`). The name is looked up in the first 1,024
        characters of each document's first block before the header is parsed."""
        from ..ingest.drain import projection_identity
        _, _, name = ref.partition(":")
        if not name:
            return []
        return [r[0] for r in self.db.conn.execute(
            "SELECT doc_id, substr(text,1,1024) FROM blocks WHERE ordinal=0 AND doc_id LIKE 'raw/%' "
            "AND instr(substr(text,1,1024), ?)>0 ORDER BY doc_id", (name,))
            if projection_identity(r[1]) == ref]

    def _servable_alternative(self, edge, away: set) -> tuple | None:
        """The first block that can carry this edge instead of one of `away`: a live claim of the
        edge whose source is servable and still resolves, else a corroborating block that holds the
        edge's quote; the block must itself pass the servable predicate (architecture.md 6.2, 7.6).
        Returns (block, quote) or None."""
        from .. import intake
        from ..servable import block_servable, servable_ref
        conn = self.db.conn
        for claim in conn.execute("SELECT * FROM edge_origin WHERE edge_id=? AND status='live' "
                                  "ORDER BY claim_ref", (edge["edge_id"],)).fetchall():
            if not servable_ref(self.db, claim["source_ref"]):
                continue
            block = intake.resolve_anchor(self.db, claim["source_ref"], claim["quote"])
            if (block is not None and block["block_id"] not in away
                    and block_servable(self.db, block["block_id"])):
                return block, claim["quote"]
        needle = normalize_text(edge["source_quote"])
        for row in conn.execute(
                "SELECT b.block_id,b.doc_id,b.text FROM edge_corroborations ec "
                "JOIN blocks b ON b.block_id=ec.source_block_id WHERE ec.edge_id=? "
                "AND b.status='active' ORDER BY b.block_id", (edge["edge_id"],)).fetchall():
            if (row["block_id"] not in away and needle in normalize_text(row["text"])
                    and block_servable(self.db, row["block_id"])):
                return dict(row), edge["source_quote"]
        return None

    def withdraw_source(self, source_ref: str, *, reason: str, requested_by: str,
                        record_event_id: int) -> dict:
        """Withdraw a source and everything derived from it, in the caller's transaction (the seven
        steps of architecture.md 10.2; steps 6 and 7 belong to the caller and to the servable
        predicate). The source and every copy that falls with it (a source page made from an
        excluded event, `servable.unservable_source_refs`) are treated alike:

        1. an `exclusions` row; every version of the source `withdrawn`;
        2. every live claim on it `withdrawn`;
        3. its active blocks retired, with no successor;
        4. every non-retracted edge anchored there, whatever its `valid_until`, moves to a servable
           block that still holds it (a live claim, a corroborating block) or is retracted
           (`source-withdrawn`);
        5. each prior a retracted edge superseded or ended is restored, its corroboration rows on
           the withdrawn blocks are removed and the counts follow.

        The `exclusions` row names `source_ref` only. A ref that falls with it has no row of its
        own: the servable predicate, the absorb skip, the expected event sets and `wiki recover`
        read it from `servable.excluded_source_refs` (the row and the links), so the next capture
        lands none of it and status counts its event as withdrawn (check round 4 invariants B1).
        A row per ref would feed the copy link again and take out more than the predicate hides:
        the event note of an older version of a source copy whose newer version was withdrawn.

        Returns what was done, including `refs` (the source and every ref that fell with it, the
        sources the caller looks for dependent candidates under). Nothing is deleted."""
        if not self._tx_active:
            raise RuntimeError("withdrawal requires Writer.transaction")
        from ..servable import unservable_source_refs
        conn = self.db.conn
        hidden_before = unservable_source_refs(self.db)
        conn.execute("INSERT INTO exclusions(source_ref,reason,requested_by,record_event_id,"
                     "recorded_at) VALUES(?,?,?,?,?)",
                     (source_ref, reason, requested_by, record_event_id, self.clock()))
        refs = sorted((unservable_source_refs(self.db) - hidden_before) | {source_ref})
        ref_marks = ",".join("?" * len(refs))
        # The exclusion covers every version of the source; the current one becomes `withdrawn`.
        # A version a later one replaced stays `replaced`: which versions a store landed before the
        # withdrawal is capture history, not record (a transcript captured twice), so it must not
        # enter the accepted state a rebuild reproduces (t-070 round 2 C-B; `digest` leaves
        # replaced versions out).
        conn.execute("UPDATE source_versions SET status='withdrawn' WHERE status='current' AND "
                     "source_ref IN (%s)" % ref_marks, refs)
        claims = conn.execute("UPDATE edge_origin SET status='withdrawn' WHERE status='live' "
                              "AND source_ref IN (%s)" % ref_marks, refs).rowcount
        docs = self._docs_of_refs(refs)
        blocks = {r[0] for doc in docs for r in conn.execute(
            "SELECT block_id FROM blocks WHERE doc_id=?", (doc,))}
        retired = 0
        for block_id in sorted(blocks):
            retired += self.retire_block(block_id, reason="source-withdrawn", edges=False)["blocks"]
        out = {"refs": refs, "docs": docs, "claims_withdrawn": claims, "blocks_retired": retired,
               "edges_reanchored": [], "edges_retracted": [], "priors_restored": [],
               "corroboration_dropped": 0}
        held = [r for b in sorted(blocks) for r in conn.execute(
            "SELECT * FROM edges WHERE source_block_id=? AND status IN ('active','quarantined',"
            "'invalidated') ORDER BY edge_id", (b,))]
        for edge in held:
            found = self._servable_alternative(edge, blocks)
            if found is None:
                self.retract_edge(edge["edge_id"], reason="source-withdrawn",
                                  retracted_by="source-withdrawal")
                out["edges_retracted"].append(edge["edge_id"])
                continue
            block, quote = found
            self.rebind_edge(edge["edge_id"], block["block_id"], block["doc_id"], quote)
            conn.execute("INSERT OR IGNORE INTO edge_corroborations(edge_id,source_block_id,"
                         "recorded_at) VALUES(?,?,?)", (edge["edge_id"], block["block_id"], self.clock()))
            out["edges_reanchored"].append(edge["edge_id"])
        for edge_id, block_id in [tuple(r) for b in sorted(blocks) for r in conn.execute(
                "SELECT edge_id,source_block_id FROM edge_corroborations WHERE source_block_id=? "
                "ORDER BY edge_id", (b,))]:
            self._drop_corroboration(edge_id, block_id)
            out["corroboration_dropped"] += 1
        out["priors_restored"] = self.restore_priors(out["edges_retracted"])
        return out

    # ---- owner corrections (architecture.md 5.1, 10.2; FR-21) -------------
    CORRECTION_ACTIONS = ("supersede", "world-change", "retract", "revert")
    _DATATYPES = ("node", "date", "number", "string", "bool")

    @staticmethod
    def correction_id(identity: dict, action: str, replacement: dict | None,
                      effective_at: str | None) -> str:
        """The id of a correction: its target's FactIdentity, action, replacement (without edge ids)
        and effective time, so the same correction sent twice is one row."""
        return "k_" + sha256_hex(canonical_json([
            [identity["subj_node"], identity["predicate"], identity["obj_key"],
             identity["valid_from"] or "", identity["anchor_source_ref"],
             identity["anchor_quote_sha256"]], action, replacement, effective_at]))[:32]

    def plan_correction(self, identity: dict, action: str, replacement: dict | None,
                        effective_at: str | None) -> dict:
        """What a correction would do, written nowhere (the read half of `apply_correction`). `refused`
        names why it cannot be applied: `unknown-target`, `ambiguous-target`, `target-not-active`,
        `replacement-required`, `replacement-not-allowed`, `valid-time-required` (a world change
        without `effective_at`, which would otherwise end the prior's window at the Writer clock),
        `valid-time-unreadable`, `effective-at-not-after-valid-from`, `effective-at-not-before-valid-until`
        (the world changed after the target's window had ended), `unknown-predicate` (a replacement
        whose predicate is not in `predicates` could not be written), `nothing-to-revert`,
        `ambiguous-entity`. Otherwise `target` (the edge row), and the replacement's `valid_from` and
        `valid_until`: it takes the target's window, so a fact that a later edge already ended is
        replaced by a fact that ends there too (a world change starts at `effective_at`)."""
        from .. import intake
        if action not in self.CORRECTION_ACTIONS:
            return {"refused": "unknown-action"}
        if action in ("supersede", "world-change"):
            if not replacement:
                return {"refused": "replacement-required"}
        elif replacement:
            return {"refused": "replacement-not-allowed"}
        if action == "world-change" and not effective_at:
            return {"refused": "valid-time-required"}
        if action != "world-change" and effective_at:
            return {"refused": "effective-at-only-for-world-change"}
        found = intake.resolve_fact_identity(self.db, identity)
        if not found:
            return {"refused": "unknown-target"}
        if len(found) > 1:
            return {"refused": "ambiguous-target"}
        target = dict(self.db.conn.execute("SELECT * FROM edges WHERE edge_id=?", (found[0],)).fetchone())
        plan = {"refused": None, "target": target, "valid_from": None, "valid_until": None}
        if action in ("supersede", "world-change"):
            if target["status"] != "active":
                return {"refused": "target-not-active"}
            if replacement["obj_datatype"] not in self._DATATYPES:
                return {"refused": "replacement-unreadable"}
            if not self.db.conn.execute("SELECT 1 FROM predicates WHERE predicate=?",
                                        (replacement["predicate"],)).fetchone():
                return {"refused": "unknown-predicate"}
            from ..ingest.resolve import resolve_surface
            if (replacement["obj_datatype"] == "node"
                    and len(resolve_surface(self.db, replacement["obj"])) > 1):
                return {"refused": "ambiguous-entity"}
            if replacement["subject"] != target["subj_node"] and len(
                    resolve_surface(self.db, replacement["subject"])) > 1:
                return {"refused": "ambiguous-entity"}
            plan["valid_from"] = target["valid_from"]
            plan["valid_until"] = target["valid_until"]
            if action == "world-change":
                if effective_at <= target["valid_from"]:
                    return {"refused": "effective-at-not-after-valid-from"}
                if target["valid_until"] and effective_at >= target["valid_until"]:
                    return {"refused": "effective-at-not-before-valid-until"}
                plan["valid_from"] = effective_at
        elif action == "revert":
            if not self._supersessions_of(target["edge_id"]):
                return {"refused": "nothing-to-revert"}
        return plan

    def apply_correction(self, identity: dict, action: str, replacement: dict | None,
                         effective_at: str | None, *, reason: str, requested_by: str,
                         record_event_id: int, note_ref: str | None = None,
                         statement: str | None = None) -> dict:
        """Apply an owner correction in the caller's transaction and write its `corrections` row
        (`applied`, or `stale` when the target no longer resolves to one edge on replay). `duplicate`
        when an `applied` row of the same id exists; a `stale` row is not a duplicate, and becomes
        the `applied` row of a later act that applies (`_write_correction_row`). `service.correct`
        plans, appends and applies under one write lock, so live it is applied whenever its plan held;
        `stale` is written by a replay of an act it cannot apply.

        * `supersede`: the target was wrong; it becomes `invalidated` (a transaction-time change) and
          the replacement, which takes the target's `valid_from` and `valid_until`, takes its place in
          the history: the current edge when the target was current, an edge that ends where a later
          one starts when it was not.
        * `world-change`: the world changed at `effective_at`; the target's window ends there and the
          replacement starts there and ends where the target's window ended.
        * `retract`: the target is withdrawn as a fact; nothing else moves.
        * `revert`: the target (a successor) is retracted and each prior it superseded or ended is
          restored, as in a source withdrawal (`restore_priors`).

        The replacement is anchored on the correction's own note (`note_ref`, `event:<id>`), on the
        `statement` line the owner gave; it carries a `record-fact` claim of that note. Returns
        {result, reason, correction_id, target_edge_id, replacement_edge_id}."""
        if not self._tx_active:
            raise RuntimeError("correction requires Writer.transaction")
        from .. import intake
        from ..ingest.encode import obj_key
        from ..ingest.resolve import ensure_node, resolve_surface
        cid = self.correction_id(identity, action, replacement, effective_at)
        # Only an applied row makes a duplicate: a `stale` row keeps an act the record could not apply,
        # and the same correction sent again later is applied when its plan holds then.
        if self.db.conn.execute("SELECT 1 FROM corrections WHERE correction_id=? AND status='applied'",
                                (cid,)).fetchone():
            return {"result": "duplicate", "reason": "already-applied", "correction_id": cid,
                    "target_edge_id": None, "replacement_edge_id": None}
        plan = self.plan_correction(identity, action, replacement, effective_at)
        target_edge = replacement_edge = None
        result, why = "applied", None
        block = None
        if plan["refused"] is None and replacement:
            block = intake.resolve_anchor(self.db, note_ref or "", statement or "")
            if block is None:
                plan = {"refused": "statement-not-anchored"}
        if plan["refused"] is not None:
            result, why = "stale", plan["refused"]
        else:
            target = plan["target"]
            target_edge = target["edge_id"]
            if action == "retract":
                self.retract_edge(target_edge, reason="owner-correction", retracted_by="owner")
            elif action == "revert":
                self.retract_edge(target_edge, reason="correction-reverted", retracted_by="owner")
                self.restore_priors([target_edge])
            else:
                subj = (target["subj_node"] if replacement["subject"] == target["subj_node"]
                        else (resolve_surface(self.db, replacement["subject"]) or [replacement["subject"]])[0])
                cand = {"subj_node": subj, "predicate": replacement["predicate"],
                        "obj_datatype": replacement["obj_datatype"], "obj_node": None,
                        "obj_literal": None, "valid_from": plan["valid_from"],
                        "valid_until": plan["valid_until"],
                        "source_created_at": plan["valid_from"], "extractor": "deterministic",
                        "extractor_version": "owner-correction/1", "confidence": 1.0,
                        "source_block_id": block["block_id"], "source_doc_id": block["doc_id"],
                        "source_quote": statement, "status": "active",
                        "reconcile_verdict": "novel"}
                if replacement["obj_datatype"] == "node":
                    found = resolve_surface(self.db, replacement["obj"])
                    cand["obj_node"] = found[0] if found else nfc(replacement["obj"])
                else:
                    cand["obj_literal"] = replacement["obj"]
                ensure_node(self.db, self, cand["subj_node"], cand["subj_node"], block["block_id"])
                if cand["obj_node"]:
                    ensure_node(self.db, self, cand["obj_node"], replacement["obj"], block["block_id"])
                replacement_edge = make_edge_id(cand["subj_node"], cand["predicate"], obj_key(cand),
                                                block["block_content_id"])
                cand["edge_id"] = replacement_edge
                self.upsert_edge(cand)
                self.add_node_block(cand["subj_node"], block["block_id"], role="claim")
                if cand["obj_node"]:
                    self.add_node_block(cand["obj_node"], block["block_id"], role="claim")
                self.add_edge_origin(replacement_edge, "correction:" + cid, source_ref=note_ref,
                                     quote=statement, origin="record-rule",
                                     rule_id="wiki-correction/%s@1" % action,
                                     source_event_id=record_event_id, fact_kind="record-fact")
                self.supersede_edge(target_edge, replacement_edge,
                                    world_change=action == "world-change",
                                    world_at=effective_at if action == "world-change" else None)
        self._write_correction_row(cid, identity, action, replacement, effective_at, reason=reason,
                                   requested_by=requested_by, record_event_id=record_event_id,
                                   status="applied" if result == "applied" else "stale",
                                   target_edge=target_edge, replacement_edge=replacement_edge)
        return {"result": result, "reason": why, "correction_id": cid, "target_edge_id": target_edge,
                "replacement_edge_id": replacement_edge}

    def _write_correction_row(self, cid: str, identity: dict, action: str, replacement: dict | None,
                              effective_at: str | None, *, reason: str, requested_by: str,
                              record_event_id: int, status: str, target_edge: str | None,
                              replacement_edge: str | None) -> None:
        """One `corrections` row per correction id. A `stale` row (an act of the record that could not
        be applied when it was replayed) becomes the `applied` row of a later act of the same
        correction that applies; an existing row is otherwise left as it is (rows are never
        deleted)."""
        values = (identity["subj_node"], identity["predicate"], identity["obj_key"],
                  identity["valid_from"] or "", identity["anchor_source_ref"],
                  identity["anchor_quote_sha256"], action,
                  canonical_json(replacement) if replacement else None,
                  "" if effective_at is None and action == "world-change" else effective_at, reason,
                  requested_by, record_event_id, self.clock(), status, target_edge, replacement_edge)
        held = self.db.conn.execute("SELECT status FROM corrections WHERE correction_id=?",
                                    (cid,)).fetchone()
        if held is None:
            self.db.conn.execute(
                "INSERT INTO corrections(target_subj,target_predicate,target_obj_key,"
                "target_valid_from,target_source_ref,target_quote_sha256,action,replacement_json,"
                "effective_at,reason,requested_by,record_event_id,recorded_at,status,target_edge_id,"
                "replacement_edge_id,correction_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values + (cid,))
        elif held["status"] == "stale" and status == "applied":
            self.db.conn.execute(
                "UPDATE corrections SET target_subj=?,target_predicate=?,target_obj_key=?,"
                "target_valid_from=?,target_source_ref=?,target_quote_sha256=?,action=?,"
                "replacement_json=?,effective_at=?,reason=?,requested_by=?,record_event_id=?,"
                "recorded_at=?,status=?,target_edge_id=?,replacement_edge_id=? WHERE correction_id=?",
                values + (cid,))

    def record_stale_correction(self, identity: dict, action: str, replacement: dict | None,
                                effective_at: str | None, *, reason: str, requested_by: str,
                                record_event_id: int) -> str:
        """Write a `stale` `corrections` row for an act of the record that cannot be applied: it
        changes no edge. A rebuild uses it for a recorded correction whose write failed, so that one
        act of the record never stops the replay of the others. Returns the correction id."""
        if not self._tx_active:
            raise RuntimeError("correction requires Writer.transaction")
        cid = self.correction_id(identity, action, replacement, effective_at)
        if not self.db.conn.execute("SELECT 1 FROM corrections WHERE correction_id=?", (cid,)).fetchone():
            self._write_correction_row(cid, identity, action, replacement, effective_at, reason=reason,
                                       requested_by=requested_by, record_event_id=record_event_id,
                                       status="stale", target_edge=None, replacement_edge=None)
        return cid

    def withdraw_candidate_effects(self, candidate_id: str, admission: dict | None = None) -> dict:
        """Undo the net effect of an admitted fact and nothing else (architecture.md 7.6). `admission`
        is the payload of its `wiki-admission` record event. The claim `candidate:<id>` becomes
        `withdrawn` (the row stays). A written edge that another live claim still holds on a servable
        block re-anchors there, keeping its id; with no such claim it is retracted
        (`candidate-withdrawn`) and a prior it superseded is restored. An edge the admission only
        supported (`corroborated`, `claim-only`) is retracted the same way when it rests on this
        admission's claim alone, which happens when a source withdrawal moved it onto that claim
        (`withdraw_source` step 4): otherwise it would stay current with no live claim at all. A
        corroboration row is removed only when this admission wrote it and no live claim resolves on
        that block. Returns what was undone."""
        from .. import intake
        from ..servable import block_servable, servable_ref
        if not self._tx_active:
            raise RuntimeError("withdrawal requires Writer.transaction")
        conn = self.db.conn
        claim_ref = "candidate:" + candidate_id
        claim = conn.execute("SELECT * FROM edge_origin WHERE claim_ref=? AND status='live'",
                             (claim_ref,)).fetchone()
        if claim is None:
            return {"claim": None}
        edge_id = claim["edge_id"]
        effect = (admission or {}).get("effect")
        net = ((admission or {}).get("edge") or {})
        self.withdraw_edge_origin(edge_id, claim_ref)
        undone: dict = {"claim": "withdrawn", "edge_id": edge_id}
        live = []
        for other in conn.execute("SELECT * FROM edge_origin WHERE edge_id=? AND status='live' "
                                  "ORDER BY claim_ref", (edge_id,)).fetchall():
            block = (intake.resolve_anchor(self.db, other["source_ref"], other["quote"])
                     if servable_ref(self.db, other["source_ref"]) else None)
            # A claim re-anchors the edge only where the block itself is servable (architecture.md 7.6).
            if block is not None and block_servable(self.db, block["block_id"]):
                live.append((block, other))
        live_ids = {b["block_id"] for b, _ in live}
        mine = intake.resolve_anchor(self.db, claim["source_ref"], claim["quote"])
        mine_id = mine["block_id"] if mine else None
        edge = conn.execute("SELECT * FROM edges WHERE edge_id=?", (edge_id,)).fetchone()
        # An admission that only supported an edge (`corroborated`, `claim-only`) holds it alone once a
        # source withdrawal moved the edge onto its claim (`withdraw_source` step 4) and no other live
        # claim is left: the edge then rests on this claim and goes with it, like an edge this
        # admission wrote (FR-17).
        rests_here = (effect in ("corroborated", "claim-only") and mine_id is not None
                      and edge["source_block_id"] == mine_id and not live)
        if ((effect in ("edge-written", "superseded-prior") or rests_here)
                and edge["status"] in ("active", "invalidated")):
            if live:
                if edge["source_block_id"] not in live_ids:
                    block, other = live[0]
                    self.rebind_edge(edge_id, block["block_id"], block["doc_id"], other["quote"])
                    undone["edge"] = "reanchored"
                else:
                    undone["edge"] = "kept"
                if mine_id and mine_id not in live_ids:
                    self._drop_corroboration(edge_id, mine_id)
                    undone["corroboration"] = "removed"
            else:
                self.retract_edge(edge_id, reason="candidate-withdrawn", retracted_by="candidate-withdrawal")
                undone["edge"] = "retracted"
                before = net.get("prior")
                given_back = []
                if effect == "superseded-prior" and before:
                    prior_id = edge["supersedes_edge_id"] or before["edge_id"]
                    if self.revert_supersede(
                            prior_id, successor_edge_id=edge_id, status=before["status"],
                            valid_until=before["valid_until"], superseded_at=before["superseded_at"],
                            boundary=edge["valid_from"] if net.get("world_change") else None):
                        undone["prior"] = prior_id
                        if not net.get("world_change"):
                            given_back.append(prior_id)
                            self._recorrect_by_successor(prior_id, edge_id)
                # whatever else the edge ended or invalidated since (a window settled again, a prior passed on
                # to it by a withdrawal), as for a withdrawn source; each supersede is undone once
                self.restore_priors([edge_id], given_back=given_back)
        elif effect == "corroborated":
            if net.get("wrote_corroboration") and mine_id and mine_id not in live_ids:
                self._drop_corroboration(edge_id, mine_id)
                undone["corroboration"] = "removed"
            else:
                undone["corroboration"] = "kept"
        return undone

    # ---- record-rule support claims (architecture.md 4.3, 8.1) ----------
    def add_edge_origin(self, edge_id: str, claim_ref: str, *, source_ref: str, quote: str,
                        origin: str = "record-rule", rule_id: str | None = None,
                        source_event_id: int | None = None, candidate_id: str | None = None,
                        fact_kind: str = "record-fact", status: str = "live") -> bool:
        """One support claim on an edge, written whether the verdict placed the edge or only
        supported it. Never deleted (a trigger refuses); idempotent on (edge_id, claim_ref).
        Returns True iff a row was written."""
        cur = self.db.conn.execute(
            "INSERT OR IGNORE INTO edge_origin(edge_id,claim_ref,origin,rule_id,source_event_id,"
            "candidate_id,source_ref,quote,quote_sha256,fact_kind,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (edge_id, claim_ref, origin, rule_id, source_event_id, candidate_id, source_ref, quote,
             sha256_hex(quote), fact_kind, status),
        )
        return cur.rowcount == 1

    def set_edge_origin_quote(self, edge_id: str, claim_ref: str, quote: str) -> None:
        """A claim follows its source when a note is re-rendered: the new span and its hash."""
        self.db.conn.execute(
            "UPDATE edge_origin SET quote=?, quote_sha256=? WHERE edge_id=? AND claim_ref=?",
            (quote, sha256_hex(quote), edge_id, claim_ref),
        )

    def withdraw_edge_origin(self, edge_id: str, claim_ref: str) -> None:
        """Withdrawal sets the status; the claim row stays (the support history is append-only)."""
        self.db.conn.execute(
            "UPDATE edge_origin SET status='withdrawn' WHERE edge_id=? AND claim_ref=?",
            (edge_id, claim_ref),
        )

    def rebind_edge(self, edge_id: str, source_block_id: str, source_doc_id: str,
                    source_quote: str) -> bool:
        """Point an edge at the block and quote that now carry it, keeping its edge id: a re-rendered
        note changes the content hash its edge id was cut from, not what the record said (OB-17).
        True iff the anchor or the quote moved."""
        row = self.db.conn.execute(
            "SELECT source_block_id, source_quote FROM edges WHERE edge_id=?", (edge_id,)).fetchone()
        if not row or (row["source_block_id"] == source_block_id
                       and row["source_quote"] == source_quote):
            return False
        self.db.conn.execute(
            "UPDATE edges SET source_block_id=?, source_doc_id=?, source_quote=? WHERE edge_id=?",
            (source_block_id, source_doc_id, source_quote, edge_id),
        )
        self._event("upsert_edge", source_doc_id,
                    {"edge_id": edge_id, "reanchor_to": source_block_id,
                     "source_doc_id": source_doc_id,
                     "source_quote_sha256": sha256_hex(source_quote)})
        return True

    def move_corroboration(self, edge_id: str, doc_id: str, source_block_id: str) -> None:
        """A document's corroboration of an edge moves to the block that holds it now: the other
        documents' rows stay, the rows of this document's earlier blocks go, the count follows."""
        self.db.conn.execute(
            "INSERT OR IGNORE INTO edge_corroborations(edge_id, source_block_id, recorded_at) "
            "VALUES(?,?,?)", (edge_id, source_block_id, self.clock()),
        )
        self.db.conn.execute(
            "DELETE FROM edge_corroborations WHERE edge_id=? AND source_block_id<>? "
            "AND source_block_id IN (SELECT block_id FROM blocks WHERE doc_id=?)",
            (edge_id, source_block_id, doc_id),
        )
        self.db.conn.execute(
            "UPDATE edges SET corroboration_count=(SELECT COUNT(*) FROM edge_corroborations "
            "WHERE edge_id=?) WHERE edge_id=?", (edge_id, edge_id),
        )

    def supersede_edge(self, prior_edge_id: str, successor_edge_id: str,
                       world_change: bool = False, world_at: str | None = None,
                       keep_lineage: bool = False) -> None:
        """Transaction supersede (a correction) vs domain end (a world change) - NEVER conflated.

        A correction sets superseded_at (the KB stopped believing it). A world-change instead ends
        the DOMAIN window at `world_at` (the moment the world changed = the successor's valid_from),
        NOT at 'now' - the prior stays currently-believed for its historical window.

        `keep_lineage` is for a window that is only settled again after a withdrawal: the successor keeps
        the `supersedes_edge_id` it already has.
        """
        now = self.clock()
        # What the prior was before this call, kept in the event so that `revert_supersede` can give
        # it back exactly (architecture.md 10.2 step 5, OB-11).
        was = self.db.conn.execute(
            "SELECT status,valid_until,superseded_at FROM edges WHERE edge_id=?",
            (prior_edge_id,)).fetchone()
        if world_change:
            boundary = world_at or now
            self.db.conn.execute(
                "UPDATE edges SET valid_until=CASE "
                "WHEN valid_until IS NULL OR ? < valid_until THEN ? ELSE valid_until END "
                "WHERE edge_id=?",
                (boundary, boundary, prior_edge_id),
            )
        else:
            self.db.conn.execute(
                "UPDATE edges SET superseded_at=?, superseded_by_edge_id=?, status='invalidated' "
                "WHERE edge_id=? AND status='active'",
                (now, successor_edge_id, prior_edge_id),
            )
        # `supersedes_edge_id` names the one edge the successor was admitted over, which a withdrawal of
        # its candidate reads back (architecture.md 7.6): a window that is only settled again keeps it.
        self.db.conn.execute(
            "UPDATE edges SET supersedes_edge_id=%s WHERE edge_id=?"
            % ("COALESCE(supersedes_edge_id, ?)" if keep_lineage else "?"),
            (prior_edge_id, successor_edge_id),
        )
        self._event("supersede_edge", None,
                    {"prior": prior_edge_id, "successor": successor_edge_id,
                     "world_change": world_change, "world_at": boundary if world_change else None,
                     "prior_before": None if was is None else {
                         "status": was["status"], "valid_until": was["valid_until"],
                         "superseded_at": was["superseded_at"]}})

    def retract_edge(self, edge_id: str, reason: str, retracted_by: str,
                     successor_edge_id: Optional[str] = None) -> None:
        """Cure protocol: RETRACT poison by bitemporal supersede - never DELETE."""
        self.db.conn.execute(
            "UPDATE edges SET superseded_at=?, status='retracted', retraction_reason=?, "
            "retracted_by=?, superseded_by_edge_id=COALESCE(?, superseded_by_edge_id) "
            "WHERE edge_id=? AND status IN ('active','quarantined','invalidated')",
            (self.clock(), reason, retracted_by, successor_edge_id, edge_id),
        )
        self._event("cure", None, {"edge_id": edge_id, "reason": reason, "by": retracted_by})

    def lift_quarantine(self, edge_id: str) -> None:
        self.db.conn.execute(
            "UPDATE edges SET status='active' WHERE edge_id=? AND status='quarantined'", (edge_id,)
        )

    # ---- merges (OWD-7, append-only) ------------------------------------
    def record_merge(self, from_node, to_node, method, decided_by, originals: dict) -> None:
        from .query import resolve_canonical
        if from_node == to_node:
            raise ValueError("cannot merge a node into itself")
        source = self.db.conn.execute(
            "SELECT canonical_node_id FROM nodes WHERE node_id=?", (from_node,)
        ).fetchone()
        target = self.db.conn.execute("SELECT 1 FROM nodes WHERE node_id=?", (to_node,)).fetchone()
        if not source or not target:
            raise ValueError("merge endpoints must both exist")
        if source["canonical_node_id"]:
            raise ValueError(f"merge source is already redirected: {from_node}")
        canonical_target = resolve_canonical(self.db, to_node)
        if canonical_target == from_node:
            raise ValueError("merge would create a canonical cycle")
        self.db.conn.execute(
            "INSERT INTO merge_events(from_node,to_node,method,decided_by,ts,originals_json) "
            "VALUES(?,?,?,?,?,?)",
            (from_node, canonical_target, method, decided_by, self.clock(), canonical_json(originals)),
        )
        self.db.conn.execute(
            "UPDATE nodes SET canonical_node_id=?, status='merged' WHERE node_id=?",
            (canonical_target, from_node),
        )
        self._event("merge", None, {"from": from_node, "to": canonical_target, "method": method})

    def surface_merge_candidate(self, node_a, node_b, method, score) -> None:
        self.db.conn.execute(
            "INSERT INTO merge_candidate(node_a,node_b,method,score,status,surfaced_at) "
            "VALUES(?,?,?,?, 'pending', ?)",
            (node_a, node_b, method, score, self.clock()),
        )

    # ---- hash-chained event log -----------------------------------------
    def _event(self, op: str, doc_id: Optional[str], payload: dict) -> None:
        if not self._tx_active:
            self.db.conn.rollback()
            raise RuntimeError("event-producing writes require Writer.transaction")
        row = self.db.conn.execute(
            "SELECT seq, checksum FROM ingest_event ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        seq = (row["seq"] + 1) if row else 1
        prev = row["checksum"] if row else GENESIS
        checksum = event_checksum(seq, prev, op, doc_id, payload)
        ts = self.clock()
        self.db.conn.execute(
            "INSERT INTO ingest_event(seq,prev_checksum,checksum,ts,doc_id,op,payload,git_commit) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (seq, prev, checksum, ts, doc_id, op, canonical_json(payload), None),
        )


# Derived tier persistence is shared with the engine's compatibility API.
TIER_EVENT_TAG = "km7.tierevent\x00"
EVENT_GENESIS = "km7.genesis"

def _last_event(db) -> tuple[int, str]:
    """(seq, event_hash) of the newest tier_event, or (0, GENESIS) if the ledger is empty."""
    r = db.conn.execute(
        "SELECT event_seq, event_hash FROM tier_events ORDER BY event_seq DESC LIMIT 1"
    ).fetchone()
    if r is None:
        return 0, EVENT_GENESIS
    return int(r["event_seq"]), r["event_hash"]


def _emit_tier_event(db, node_id: str, kind: str, old: str | None, new: str | None) -> str:
    """Append ONE versioned, hash-chained change event. seq is monotonic; event_hash folds the
    prior hash (a chain) so the ledger is tamper-evident. recorded_at is advisory (never hashed)."""
    seq, prev = _last_event(db)
    seq += 1
    payload = canonical_json({"seq": seq, "node_id": nfc(node_id), "kind": kind,
                              "old": old, "new": new, "prev": prev})
    h = sha256_hex(TIER_EVENT_TAG + payload)
    db.conn.execute(
        "INSERT INTO tier_events(event_seq,node_id,kind,old_value,new_value,prev_hash,event_hash,recorded_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (seq, node_id, kind, old, new, prev, h, now_iso()),
    )
    return h


def retier_nodes(db, autocommit: bool = True) -> dict[str, int]:
    """Run PPR + Stage-A/B/C, PERSIST `nodes.page_band` (+ advisory `pagerank`). Returns node->band.

    Fires on full rebuild AND incrementally from absorb; page_band is what `stable_rank` and map.md
    read as a real tier.
    """
    node_ids, statuses, node_edges, updates_exempt = _graph(db)
    if not node_ids:
        return {}

    # PPR and the tiers are a pure function of the graph `_graph` read (t-453): absorb re-tiers after
    # every document, and most documents change no node or edge, so the last result on this store
    # connection is reused while the graph read is equal; the bands are still compared with the
    # stored ones and written below, as before.
    inputs = (node_ids, statuses, node_edges, updates_exempt)
    memo = getattr(db, "_retier_memo", None)
    if memo is not None and memo[0] == inputs:
        scores, band = memo[1], memo[2]
    else:
        # in-neighbour sets (distinct sources) for Stage-B bridge detection.
        in_neighbors: dict[str, set] = {nid: set() for nid in node_ids}
        for s, o in node_edges:
            in_neighbors[o].add(s)

        # PPR: uniform restart over every node (seed = uniform), damping 0.85 == teleport 0.15.
        ppr_edges = [(s, o, 1.0) for (s, o) in node_edges]
        seed = {nid: 1.0 for nid in node_ids}
        scores = ppr(ppr_edges, seed)

        result = compute_tiers(node_ids, scores, statuses, in_neighbors, updates_exempt)
        band = result["band"]
        db._retier_memo = (inputs, scores, band)

    # prior persisted bands so a VERSIONED tier-change event fires only on an actual band change
    # (a no-op re-tier emits nothing; NULL prior == first assignment).
    prior_band = {
        nfc(r["node_id"]): (None if r["page_band"] is None else int(r["page_band"]))
        for r in db.conn.execute("SELECT node_id, page_band FROM nodes")
    }

    for nid in node_ids:
        old_band = prior_band.get(nid)
        new_band = band[nid]
        if old_band != new_band:
            _emit_tier_event(db, nid, "tier",
                             None if old_band is None else str(old_band), str(new_band))
        db.conn.execute(
            "UPDATE nodes SET page_band=?, pagerank=? WHERE node_id=?",
            (band[nid], float(scores.get(nid, 0.0)), nid),
        )
    if autocommit:
        db.commit()
    return dict(band)
