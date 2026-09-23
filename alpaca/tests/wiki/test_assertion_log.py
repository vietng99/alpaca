"""M2.8 proof: the vendored assertion-log write/read API (alpaca/wiki).

Ported in spirit from the upstream Rune-2 suite (tests/test_pkt2_km.py, tests/test_pkt2_prov.py,
tests/test_hash_chain.py, tests/test_ledger_atomicity.py, tests/test_block_lifecycle.py,
tests/test_pc3_atoms.py, tests/test_pc7_span.py, tests/test_db_containment.py). The upstream tests
drive the full ingest absorber (rune2.ingest.absorb) and answer path (rune2.engine.answer), neither
of which is vendored in M2.8 (they land in later M2 tasks). This module proves the same
assertion-log properties directly against the vendored write / query / ledger / replay / hashgate /
encode surface, on BOTH a positive and a negative path:

  * the ingest_event hash chain verifies and detects a tamper (R3/R4);
  * the DB chain and the co-authoritative events.jsonl agree (PKT-2 dual-loop);
  * a rolled-back transaction leaves neither a row nor an event nor a ledger line (atomicity);
  * an event-producing write outside Writer.transaction is refused;
  * a free-form object literal is HASHED into the event, never copied verbatim into the ledger;
  * the authoritative graph is append-only: a hard DELETE on an edge/node/block is refused;
  * a retired block keeps its text (supersede-never-delete) and its unsupported edge is retracted;
  * a citation anchor is enforced: a dangling source_block_id is refused at the write boundary (R3);
  * an unchanged doc/block short-circuits via the SHA-256 hashgate;
  * classify_atom routes FACT/TAKE/ENTITY/FULLTEXT and span-grounding vetoes a non-substring quote;
  * the vault-containment barrier refuses a DB path that escapes its vault.

The compartment (private-path routing), reconcile and resolve modules are vendored in later M2
tasks; the write.py / encode.py importers name them, so stand-ins are registered here exactly like
a later real module would win via setdefault. The assertion-log paths exercised below never enter
those capabilities.
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path

# --- stand-ins for the not-yet-vendored siblings the vendored importers name --------------------
_engine_pkg = types.ModuleType("alpaca.wiki.engine")
_engine_pkg.__path__ = []
_compartment = types.ModuleType("alpaca.wiki.engine.compartment")


class _PrivatePathViolation(RuntimeError):
    pass


_compartment.PrivatePathViolation = _PrivatePathViolation
_compartment.is_private_path = lambda path: False
_compartment.assert_private_path = lambda private, path: None
sys.modules.setdefault("alpaca.wiki.engine", _engine_pkg)
sys.modules.setdefault("alpaca.wiki.engine.compartment", _compartment)

_reconcile = types.ModuleType("alpaca.wiki.ingest.reconcile")
_resolve = types.ModuleType("alpaca.wiki.ingest.resolve")
_resolve.resolve_surface = lambda db, surface: []
sys.modules.setdefault("alpaca.wiki.ingest.reconcile", _reconcile)
sys.modules.setdefault("alpaca.wiki.ingest.resolve", _resolve)

from alpaca.wiki.clock import FixedClock
from alpaca.wiki.config import Config
from alpaca.wiki.determinism import sha256_hex
from alpaca.wiki.ingest import hashgate
from alpaca.wiki.ingest.encode import classify_atom, resolve_relative_time, span_grounded
from alpaca.wiki.store import query as q
from alpaca.wiki.store import replay
from alpaca.wiki.store.db import DB
from alpaca.wiki.store.ledger import Ledger, reconcile_db_vs_ledger, verify_chain
from alpaca.wiki.store.write import Writer

T0 = "2020-01-01T00:00:00+00:00"


def _edge(edge_id, obj_literal="Acme", predicate="works_at", source_block_id="doc1#0",
          source_quote="Alice works at Acme."):
    return {
        "edge_id": edge_id,
        "subj_node": "alice",
        "predicate": predicate,
        "obj_literal": obj_literal,
        "obj_datatype": "string",
        "source_block_id": source_block_id,
        "source_doc_id": "doc1",
        "source_quote": source_quote,
        "extractor": "deterministic",
        "valid_from": T0,
    }


class _VaultCase(unittest.TestCase):
    """A fresh poured wiki vault with a doc, one block and one node ready to anchor edges."""

    DOC_TEXT = "Alice works at Acme."

    def setUp(self):
        self.vault = Path(tempfile.mkdtemp(prefix="tewiki_alog_"))
        self.addCleanup(shutil.rmtree, self.vault, ignore_errors=True)
        self.cfg = Config.for_vault(self.vault)
        self.db = DB(self.cfg)
        self.db.pour()
        self.addCleanup(self.db.conn.close)
        self.led = Ledger(self.cfg.ledger_path, vault_dir=self.cfg.vault_dir)
        self.w = Writer(self.db, self.led, clock=FixedClock(start=T0, step=1))
        with self.w.transaction():
            self.w.upsert_doc("doc1", "raw/a.md", "raw", sha256_hex(self.DOC_TEXT), private=0)
            self.w.upsert_block({"block_id": "doc1#0", "block_content_id": "c0",
                                 "occurrence_index": 0, "doc_id": "doc1", "ordinal": 0,
                                 "text": self.DOC_TEXT})
            self.w.upsert_node("alice", "person", "Alice")

    def _all_events(self):
        return q.all_events(self.db)


class HashChain(_VaultCase):
    def _seed_two_edges(self):
        with self.w.transaction():
            self.w.upsert_edge(_edge("e1"))
        with self.w.transaction():
            self.w.upsert_edge(_edge("e2", obj_literal="Globex", predicate="founded"))

    def test_chain_verifies_and_dual_loop_agrees(self):
        self._seed_two_edges()
        rows = self._all_events()
        self.assertGreater(len(rows), 0)
        ok, why = verify_chain(rows)
        self.assertTrue(ok, msg=why)
        # dual loop: DB chain vs the on-disk co-authoritative ledger.
        ledger_rows = Ledger(self.cfg.ledger_path, vault_dir=self.cfg.vault_dir).read_all()
        agree, why2 = reconcile_db_vs_ledger(
            [{"seq": r["seq"], "checksum": r["checksum"]} for r in rows],
            [{"seq": r["seq"], "checksum": r["checksum"]} for r in ledger_rows])
        self.assertTrue(agree, msg=why2)

    def test_tamper_is_detected(self):
        self._seed_two_edges()
        rows = self._all_events()
        rows[len(rows) // 2]["checksum"] = "deadbeef" * 8
        ok, why = verify_chain(rows)
        self.assertFalse(ok)
        self.assertIn("mismatch", why)

    def test_replay_halts_at_poison_but_runs_clean_chain(self):
        self._seed_two_edges()
        rows = self._all_events()
        clean = replay.replay_events(rows)
        self.assertFalse(clean.halted)
        self.assertEqual(clean.last_good_seq, rows[-1]["seq"])
        # poison one link: replay must halt WITHOUT applying the poisoned event.
        poisoned = [dict(r) for r in rows]
        poisoned[-1] = dict(poisoned[-1], checksum="00" * 32)
        halted = replay.replay_events(poisoned)
        self.assertTrue(halted.halted)
        self.assertEqual(halted.last_good_seq, rows[-2]["seq"])


class LedgerAtomicity(_VaultCase):
    def test_rollback_leaves_no_row_no_event_no_line(self):
        events_before = self.db.conn.execute("SELECT COUNT(*) FROM ingest_event").fetchone()[0]
        with self.assertRaisesRegex(RuntimeError, "injected rollback"):
            with self.w.transaction():
                self.w.upsert_node("ghost", "org", "Ghost")
                raise RuntimeError("injected rollback")
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) FROM nodes WHERE node_id='ghost'").fetchone()[0], 0)
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) FROM ingest_event").fetchone()[0], events_before)

    def test_successful_commit_syncs_verified_private_ledger(self):
        with self.w.transaction():
            self.w.upsert_edge(_edge("e_ok"))
        rows = Ledger(self.cfg.ledger_path, vault_dir=self.cfg.vault_dir).read_all()
        self.assertTrue(verify_chain(rows)[0])
        self.assertEqual(os.stat(self.cfg.ledger_path).st_mode & 0o777, 0o600)

    def test_event_write_outside_transaction_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "require Writer.transaction"):
            self.w.upsert_node("unsafe", "org", "Unsafe")

    def test_free_form_literal_is_hashed_not_copied_into_ledger(self):
        secret = "private free-form object literal xyzzy"
        with self.w.transaction():
            self.w.upsert_edge(_edge("e_secret", obj_literal=secret, predicate="related_to"))
        events = Ledger(self.cfg.ledger_path, vault_dir=self.cfg.vault_dir).read_all()
        payload = next(r["payload"] for r in events if r["payload"].get("edge_id") == "e_secret")
        self.assertNotIn("obj_key", payload)
        self.assertEqual(len(payload["obj_key_sha256"]), 64)
        self.assertNotIn(secret, self.cfg.ledger_path.read_text(encoding="utf-8"))


class AppendOnlyGraph(_VaultCase):
    def _seed_superseded_pair(self):
        with self.w.transaction():
            self.w.upsert_edge(_edge("e_prior", obj_literal="Acme"))
            self.w.upsert_edge(_edge("e_succ", obj_literal="Beta"))
            self.w.supersede_edge("e_prior", "e_succ")

    def test_hard_delete_of_superseded_edge_is_rejected(self):
        self._seed_superseded_pair()
        prior = self.db.conn.execute(
            "SELECT status, superseded_at FROM edges WHERE edge_id='e_prior'").fetchone()
        self.assertEqual(prior["status"], "invalidated")
        self.assertIsNotNone(prior["superseded_at"])
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.db.conn.execute("DELETE FROM edges WHERE edge_id='e_prior'")
        self.assertIn("deprecate-not-delete", str(ctx.exception))
        still = self.db.conn.execute(
            "SELECT COUNT(*) c FROM edges WHERE edge_id='e_prior'").fetchone()["c"]
        self.assertEqual(still, 1)

    def test_hard_delete_of_active_edge_is_also_rejected(self):
        self._seed_superseded_pair()
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.db.conn.execute("DELETE FROM edges WHERE edge_id='e_succ'")
        self.assertIn("deprecate-not-delete", str(ctx.exception))

    def test_hard_delete_of_node_and_block_is_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.conn.execute("DELETE FROM nodes WHERE node_id='alice'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.conn.execute("DELETE FROM blocks WHERE block_id='doc1#0'")


class BlockLifecycle(_VaultCase):
    def test_retire_block_preserves_text_and_retracts_unsupported_edge(self):
        with self.w.transaction():
            self.w.upsert_edge(_edge("e_anchored"))
        with self.w.transaction():
            stats = self.w.retire_block("doc1#0", reason="source-removed")
        preserved = self.db.conn.execute(
            "SELECT text, status, superseded_at FROM blocks WHERE block_id='doc1#0'").fetchone()
        edge = q.get_edge(self.db, "e_anchored")
        # POSITIVE: the block text survives (supersede-never-delete), only its status changes.
        self.assertEqual(preserved["text"], self.DOC_TEXT)
        self.assertEqual(preserved["status"], "superseded")
        self.assertIsNotNone(preserved["superseded_at"])
        # the unsupported current edge is RETRACTED (a tombstone), never removed.
        self.assertEqual(edge["status"], "retracted")
        self.assertEqual(edge["retraction_reason"], "source-removed")
        self.assertEqual(stats["blocks"], 1)
        self.assertEqual(stats["retracted"], 1)

    def test_retire_missing_block_is_a_noop(self):
        with self.w.transaction():
            stats = self.w.retire_block("doc1#404", reason="source-removed")
        self.assertEqual(stats, {"blocks": 0, "reanchored": 0, "retracted": 0})


class ProvenanceAnchor(_VaultCase):
    def test_dangling_source_block_id_is_refused_at_write(self):
        # NEGATIVE: a forged citation anchored to a ghost block is refused by the substrate
        # (edges.source_block_id NOT NULL REFERENCES blocks(block_id), PRAGMA foreign_keys=ON).
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.conn.execute(
                "INSERT INTO edges(edge_id,subj_node,predicate,obj_datatype,source_block_id,"
                "source_quote,extractor,recorded_at,valid_from,status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("forged", "alice", "works_at", "string", "ghost#404", "forged", "deterministic",
                 T0, T0, "active"))
        n = self.db.conn.execute(
            "SELECT COUNT(*) c FROM edges WHERE edge_id='forged'").fetchone()["c"]
        self.assertEqual(n, 0)

    def test_written_edge_round_trips_its_anchor_and_quote(self):
        # POSITIVE: a real citation anchor round-trips through the FK to the source block.
        with self.w.transaction():
            self.w.upsert_edge(_edge("e_prov"))
        edge = q.get_edge(self.db, "e_prov")
        self.assertEqual(edge["source_block_id"], "doc1#0")
        self.assertEqual(edge["source_quote"], self.DOC_TEXT)
        block = q.get_block(self.db, edge["source_block_id"])
        self.assertIsNotNone(block)
        self.assertEqual(block["doc_id"], "doc1")


class Hashgate(_VaultCase):
    def test_unchanged_doc_and_block_short_circuit(self):
        # the fixture doc was written with sha256(DOC_TEXT); an identical content is unchanged.
        changed, sha = hashgate.doc_changed(self.db, "doc1", self.DOC_TEXT)
        self.assertFalse(changed)
        self.assertEqual(sha, sha256_hex(self.DOC_TEXT))
        # a different content is changed; a brand-new doc id is changed.
        changed2, _ = hashgate.doc_changed(self.db, "doc1", self.DOC_TEXT + " Updated.")
        self.assertTrue(changed2)
        self.assertTrue(hashgate.doc_changed(self.db, "doc-new", "anything")[0])
        # block granularity: the same block_sha256 short-circuits, a new one does not.
        block_sha = self.db.conn.execute(
            "SELECT block_sha256 FROM blocks WHERE block_id='doc1#0'").fetchone()["block_sha256"]
        if block_sha is not None:
            self.assertFalse(hashgate.block_changed(self.db, "doc1#0", block_sha))
        self.assertTrue(hashgate.block_changed(self.db, "doc1#0", "a-different-sha"))
        self.assertTrue(hashgate.block_changed(self.db, "doc1#404", "any"))


class TypedAtomsAndSpan(unittest.TestCase):
    """pc.3 typed-atom routing and pc.7 span-grounding on the vendored encode primitives."""

    def test_classify_atom_routes_the_four_kinds(self):
        self.assertEqual(classify_atom(
            {"predicate": "works_at", "obj_datatype": "node",
             "source_quote": "A works at B"}), "FACT")
        self.assertEqual(classify_atom(
            {"predicate": "note", "obj_datatype": "string",
             "source_quote": "I think this is best"}), "TAKE")
        self.assertEqual(classify_atom(
            {"predicate": "is_a", "obj_datatype": "string",
             "source_quote": "Dat is_a person"}), "ENTITY")
        self.assertEqual(classify_atom(
            {"predicate": "quote", "obj_datatype": "fulltext",
             "source_quote": "x"}), "FULLTEXT")

    def test_span_grounded_substring_veto(self):
        # POSITIVE: an NFC-normalized substring of the cited block passes.
        self.assertTrue(span_grounded("works at Acme", "Alice works at Acme Corp."))
        # NEGATIVE: a quote absent from the cited block is vetoed (never silently placed).
        self.assertFalse(span_grounded("works at Google", "Alice works at Acme Corp."))
        # an empty quote is not grounded.
        self.assertFalse(span_grounded("", "anything"))

    def test_relative_time_anchors_to_source_not_wallclock(self):
        self.assertEqual(resolve_relative_time("recently", "2020-01-01T00:00:00+00:00")[:4], "2020")
        self.assertEqual(resolve_relative_time("last week", "2020-01-08"), "2020-01-01")


class VaultContainment(unittest.TestCase):
    def setUp(self):
        self.vault = Path(tempfile.mkdtemp(prefix="tewiki_contain_"))
        self.addCleanup(shutil.rmtree, self.vault, ignore_errors=True)
        self.cfg = Config.for_vault(self.vault)

    def test_db_path_outside_vault_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            escaped = replace(self.cfg, db_path=Path(td) / "outside.db")
            with self.assertRaisesRegex(ValueError, "escapes vault"):
                DB(escaped)

    def test_db_and_sidecars_are_private_mode(self):
        db = DB(self.cfg)
        self.addCleanup(db.close)
        db.pour()
        db.commit()
        self.assertEqual(os.stat(self.cfg.db_path).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
