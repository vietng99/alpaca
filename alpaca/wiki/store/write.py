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
from ..determinism import canonical_json, normalize_text, sha256_hex
from ..engine.compartment import PrivatePathViolation, assert_private_path, is_private_path
from .db import DB
from .ledger import GENESIS, Ledger, event_checksum


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

    def _committed_event_rows(self) -> list[dict[str, Any]]:
        rows = self.db.conn.execute(
            "SELECT seq,prev_checksum,checksum,ts,doc_id,op,payload FROM ingest_event "
            "ORDER BY seq ASC"
        ).fetchall()
        return [
            {
                "seq": r["seq"], "prev_checksum": r["prev_checksum"],
                "checksum": r["checksum"], "ts": r["ts"], "doc_id": r["doc_id"],
                "op": r["op"], "payload": json.loads(r["payload"]),
            }
            for r in rows
        ]

    def sync_ledger(self) -> None:
        from .ledger import LedgerDivergence
        last_error = None
        for _attempt in range(3):
            try:
                self.ledger.sync_from_rows(self._committed_event_rows())
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
                             source_created_at: str | None, private: int,
                             sensitivity: str) -> None:
        _validate_doc_privacy(private, path)
        self.db.conn.execute(
            "UPDATE docs SET path=?,kind=?,domain=?,source_created_at=?,private=?,sensitivity=?,"
            "privacy_scanned=1 "
            "WHERE doc_id=?",
            (path, kind, domain, source_created_at, private, sensitivity, doc_id),
        )
        self.db.conn.execute(
            "UPDATE blocks SET domain=?,source_created_at=? WHERE doc_id=? AND status='active'",
            (domain, source_created_at, doc_id),
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
            "domain=excluded.domain, source_created_at=excluded.source_created_at",
            vals,
        )

    def retire_block(self, block_id: str, reason: str = "source-removed") -> dict[str, int]:
        """Retire source block without deleting its text, then close unsupported current claims."""
        row = self.db.conn.execute(
            "SELECT block_id FROM blocks WHERE block_id=? AND status='active'", (block_id,)
        ).fetchone()
        if not row:
            return {"blocks": 0, "reanchored": 0, "retracted": 0}
        now = self.clock()
        self.db.conn.execute(
            "UPDATE blocks SET status='superseded', superseded_at=?, valid_until=COALESCE(valid_until,?) "
            "WHERE block_id=? AND status='active'", (now, now, block_id)
        )
        stats = {"blocks": 1, "reanchored": 0, "retracted": 0}
        edges = self.db.conn.execute(
            "SELECT edge_id, source_quote FROM edges WHERE source_block_id=? "
            "AND status='active' AND valid_until IS NULL", (block_id,)
        ).fetchall()
        for edge in edges:
            alternatives = self.db.conn.execute(
                "SELECT ec.source_block_id,b.doc_id,b.text FROM edge_corroborations ec "
                "JOIN blocks b ON b.block_id=ec.source_block_id "
                "WHERE ec.edge_id=? AND ec.source_block_id<>? AND b.status='active' "
                "ORDER BY ec.source_block_id",
                (edge["edge_id"], block_id),
            ).fetchall()
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

    def retire_doc(self, doc_id: str, reason: str = "source-document-removed") -> dict[str, int]:
        total = {"blocks": 0, "reanchored": 0, "retracted": 0}
        block_ids = [r["block_id"] for r in self.db.conn.execute(
            "SELECT block_id FROM blocks WHERE doc_id=? AND status='active' ORDER BY block_id",
            (doc_id,),
        ).fetchall()]
        for block_id in block_ids:
            result = self.retire_block(block_id, reason=reason)
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

    def supersede_edge(self, prior_edge_id: str, successor_edge_id: str,
                       world_change: bool = False, world_at: str | None = None) -> None:
        """Transaction supersede (a correction) vs domain end (a world change) - NEVER conflated.

        A correction sets superseded_at (the KB stopped believing it). A world-change instead ends
        the DOMAIN window at `world_at` (the moment the world changed = the successor's valid_from),
        NOT at 'now' - the prior stays currently-believed for its historical window.
        """
        now = self.clock()
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
        self.db.conn.execute(
            "UPDATE edges SET supersedes_edge_id=? WHERE edge_id=?",
            (prior_edge_id, successor_edge_id),
        )
        self._event("supersede_edge", None,
                    {"prior": prior_edge_id, "successor": successor_edge_id,
                     "world_change": world_change, "world_at": boundary if world_change else None})

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
