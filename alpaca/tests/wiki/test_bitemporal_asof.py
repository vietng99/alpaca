"""M2.8 proof: bitemporal as-of over the vendored assertion-log engine (alpaca/wiki).

Ported in spirit from the upstream Rune-2 suite (tests/test_r6_bitemporal.py,
tests/test_km5_asof_no_leak.py, tests/test_dbalone_lineage.py). The upstream tests drive the
full answer path (rune2.engine.answer) and the ingest absorber (rune2.ingest.absorb), neither of
which is vendored in M2.8 (they land in later M2 tasks). This module proves the same four
Done-when properties directly against the vendored write/read/asof/query/replay surface:

  1. an edge cannot be written without a source_block_id (schema R3, NOT NULL + FK);
  2. an as-of query at the instant an op closed leaks nothing written later (km.5 no-leak, the
     transaction axis);
  3. a correction and a world-change move DIFFERENT time axes (R6 bitemporality: a correction
     moves superseded_at and leaves valid_until open; a world-change moves valid_until and leaves
     superseded_at open);
  4. the engine's clock/determinism are vendored under alpaca.wiki, not alpaca (positive side of the
     namespace separation; the AST walk that proves both directions lives in
     tests/wiki/test_engine_namespace.py).

Every property is asserted on BOTH a positive and a negative path.
"""
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

# The compartment helper (private-path routing) is vendored in a later M2 task. write.py needs it
# only for the doc-privacy validation path, which the assertion-log/edge tests here never take.
# Register a stand-in so the vendored Writer imports; a real alpaca.wiki.engine.compartment (once it
# lands) wins via setdefault.
_engine_pkg = types.ModuleType("alpaca.wiki.engine")
_engine_pkg.__path__ = []  # mark as a package so submodule import resolves
_compartment = types.ModuleType("alpaca.wiki.engine.compartment")


class _PrivatePathViolation(RuntimeError):
    pass


_compartment.PrivatePathViolation = _PrivatePathViolation
_compartment.is_private_path = lambda path: False
_compartment.assert_private_path = lambda private, path: None
sys.modules.setdefault("alpaca.wiki.engine", _engine_pkg)
sys.modules.setdefault("alpaca.wiki.engine.compartment", _compartment)

from alpaca.wiki.clock import FixedClock
from alpaca.wiki.config import Config
from alpaca.wiki.store import query as q
from alpaca.wiki.store import replay
from alpaca.wiki.store.asof import AsOf
from alpaca.wiki.store.db import DB
from alpaca.wiki.store.ledger import Ledger
from alpaca.wiki.store.write import Writer

T0 = "2020-01-01T00:00:00+00:00"
T_MID = "2020-06-01T00:00:00+00:00"
T_LATE = "2020-12-01T00:00:00+00:00"


def _edge(edge_id, obj_node, predicate="works_at", recorded_at=T0, valid_from=T0,
          valid_until=None, source_block_id="doc1#0"):
    return {
        "edge_id": edge_id,
        "subj_node": "alice",
        "predicate": predicate,
        "obj_node": obj_node,
        "obj_datatype": "node",
        "source_block_id": source_block_id,
        "source_doc_id": "doc1",
        "source_quote": "Alice works at Acme.",
        "extractor": "deterministic",
        "recorded_at": recorded_at,
        "valid_from": valid_from,
        "valid_until": valid_until,
    }


class BitemporalAsOf(unittest.TestCase):
    def setUp(self):
        self.vault = Path(tempfile.mkdtemp(prefix="tewiki_asof_"))
        self.addCleanup(shutil.rmtree, self.vault, ignore_errors=True)
        self.cfg = Config.for_vault(self.vault)
        self.db = DB(self.cfg)
        self.db.pour()
        self.addCleanup(self.db.conn.close)
        self.led = Ledger(self.cfg.ledger_path, vault_dir=self.cfg.vault_dir)
        self.w = Writer(self.db, self.led, clock=FixedClock(start=T0, step=1))
        with self.w.transaction():
            self.w.upsert_doc("doc1", "raw/a.md", "raw", "sha_a", private=0)
            self.w.upsert_block({"block_id": "doc1#0", "block_content_id": "c0",
                                 "occurrence_index": 0, "doc_id": "doc1", "ordinal": 0,
                                 "text": "Alice works at Acme."})
            for node in ("alice", "acme", "globex", "initech"):
                self.w.upsert_node(node, "person" if node == "alice" else "org", node.title())

    def _obj_of(self, node, asof):
        return {e["edge_id"]: e["obj_node"] for e in q.active_edges_for_node(self.db, node, asof)}

    # ---- Property 1: an edge cannot be written without a source_block_id -------------
    def test_edge_write_requires_source_block_id(self):
        # NEGATIVE: a NULL source_block_id is refused by the substrate (schema line 131,
        # source_block_id TEXT NOT NULL REFERENCES blocks(block_id)) and rolls the tx back.
        with self.assertRaises(sqlite3.IntegrityError):
            with self.w.transaction():
                self.w.upsert_edge(_edge("e_nosrc", "acme", source_block_id=None))
        self.assertIsNone(q.get_edge(self.db, "e_nosrc"),
                          "a source-block-less edge must not persist")

        # POSITIVE: the same edge with a real source_block_id is written and readable.
        with self.w.transaction():
            self.w.upsert_edge(_edge("e_ok", "acme"))
        row = q.get_edge(self.db, "e_ok")
        self.assertIsNotNone(row)
        self.assertEqual(row["source_block_id"], "doc1#0")

    # ---- Property 2: an as-of at an instant leaks nothing written later --------------
    def test_asof_at_instant_leaks_nothing_written_later(self):
        with self.w.transaction():
            self.w.upsert_edge(_edge("e_early", "acme", recorded_at=T0, valid_from=T0))
        # A later op writes a second assertion about the same subject.
        with self.w.transaction():
            self.w.upsert_edge(_edge("e_late", "globex", predicate="founded",
                                     recorded_at=T_MID, valid_from=T_MID))

        # NEGATIVE: an audit as-of pinned at the earlier op's close (K=T0) must NOT see the
        # later write; the transaction axis excludes recorded_at > K.
        early = self._obj_of("alice", AsOf(T=T_LATE, K=T0))
        self.assertIn("e_early", early)
        self.assertNotIn("e_late", early, "an as-of at T0 leaked a fact written at T_MID")

        # POSITIVE: an audit as-of pinned after the later write (K=T_LATE) sees both.
        later = self._obj_of("alice", AsOf(T=T_LATE, K=T_LATE))
        self.assertIn("e_early", later)
        self.assertIn("e_late", later)

    # ---- Property 3: a correction and a world-change move different axes -------------
    def test_correction_and_world_change_move_different_axes(self):
        # A correction: the KB was wrong. supersede_edge(world_change=False) stamps the
        # TRANSACTION axis (superseded_at) and leaves the DOMAIN window (valid_until) open.
        with self.w.transaction():
            self.w.upsert_edge(_edge("c_prior", "acme"))
        with self.w.transaction():
            self.w.upsert_edge(_edge("c_succ", "initech"))
            self.w.supersede_edge("c_prior", "c_succ", world_change=False)
        corrected = q.get_edge(self.db, "c_prior")

        # A world-change: the fact stopped being true at a world instant. supersede_edge(
        # world_change=True, world_at=tw) stamps the DOMAIN axis (valid_until) and leaves the
        # TRANSACTION axis (superseded_at) open - the prior stays currently-believed for its
        # historical window.
        with self.w.transaction():
            self.w.upsert_edge(_edge("w_prior", "acme"))
        with self.w.transaction():
            self.w.upsert_edge(_edge("w_succ", "globex", predicate="founded",
                                     recorded_at=T_MID, valid_from=T_MID))
            self.w.supersede_edge("w_prior", "w_succ", world_change=True, world_at=T_MID)
        world = q.get_edge(self.db, "w_prior")

        # The two operations touch DIFFERENT columns - this is the whole point of bitemporality.
        self.assertIsNotNone(corrected["superseded_at"])
        self.assertIsNone(corrected["valid_until"],
                          "a correction must not close the domain window")
        self.assertEqual(corrected["status"], "invalidated")

        self.assertIsNotNone(world["valid_until"])
        self.assertIsNone(world["superseded_at"],
                          "a world-change must not stamp the transaction axis")
        self.assertEqual(world["status"], "active")

        # NEGATIVE/POSITIVE read consequences prove the axes are read distinctly:
        # A correction disappears from latest-knowledge at every T (it is no longer believed)...
        self.assertNotIn("c_prior", self._obj_of("alice", AsOf(T=T0)))
        self.assertNotIn("c_prior", self._obj_of("alice", AsOf(T=T_LATE)))
        # ...but an audit as-of BEFORE the correction still recovers it.
        self.assertIn("c_prior", self._obj_of("alice", AsOf(T=T0, K=T0)))

        # A world-change is still current knowledge WITHIN its historical window (T < world_at)...
        self.assertIn("w_prior", self._obj_of("alice", AsOf(T=T0)))
        # ...and gone once the world moved on (T >= world_at), on the domain axis alone.
        self.assertNotIn("w_prior", self._obj_of("alice", AsOf(T=T_LATE)))

    # ---- Property 4 (positive side): the engine clock/determinism are alpaca.wiki's -----
    def test_engine_clock_and_determinism_are_vendored_under_alpaca_wiki(self):
        import alpaca.wiki.clock as wiki_clock
        import alpaca.wiki.determinism as wiki_det
        from alpaca.wiki.store import ledger as wiki_ledger
        self.assertTrue(wiki_clock.__name__.startswith("alpaca.wiki."))
        self.assertTrue(wiki_det.__name__.startswith("alpaca.wiki."))
        # ledger.py resolves its determinism helpers inside alpaca.wiki (never alpaca.determinism).
        self.assertIs(wiki_ledger.canonical_json, wiki_det.canonical_json)
        self.assertEqual(wiki_det.canonical_json.__module__, "alpaca.wiki.determinism")
        self.assertEqual(wiki_ledger.sha256_hex("rune2-genesis"), wiki_ledger.GENESIS)


if __name__ == "__main__":
    unittest.main()
