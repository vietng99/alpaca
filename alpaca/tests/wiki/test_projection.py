"""M2.15 projection proof - the checkable markdown projection that travels with the extract.

Ported IN SPIRIT from the upstream Rune-2 suite (tests/test_dm4_map.py, test_dm9_freshness.py,
test_dm13_parity.py, test_dbalone_lineage.py). The upstream originals drive the ingest absorber and
the inward `content_port.port()`; neither is vendored under alpaca.wiki (that is M2.14, not this task).
The same properties are proved directly against the vendored alpaca.wiki projection surface
(alpaca.wiki.project.artifacts / .render / .freshness), the parity reporter (alpaca.wiki.migrate.content_port)
and the outward emitter (alpaca.wiki.extract), building the store through the write door.

  dm.4  map.md is deterministic, slug-sorted, alias-sorted, and public-only.
  dm.9  a freshly compiled projection is FRESH; a hand-edited or missing artifact is STALE and
        assert_fresh FAILS CLOSED; the volatile Generated: line does not trip staleness.
  dm.13 a page whose structural edge does not survive is UNACCOUNTED, never dropped silently.
  dbalone the extracted vault's belief lineage is queryable from the DB ALONE (survives rm ledger).
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from alpaca.wiki.clock import FixedClock
from alpaca.wiki.config import Config
from alpaca.wiki.determinism import sha256_hex
from alpaca.wiki.store.db import DB
from alpaca.wiki.store.ledger import Ledger
from alpaca.wiki.store.replay import lineage
from alpaca.wiki.store.write import Writer
from alpaca.wiki.project import artifacts as A
from alpaca.wiki.project import freshness as F
from alpaca.wiki.migrate import content_port as CP
from alpaca.wiki import extract

T0 = "2020-01-01T00:00:00+00:00"


def _writer(cfg):
    db = DB(cfg)
    db.pour()
    return db, Writer(db, Ledger(cfg.ledger_path, vault_dir=cfg.vault_dir),
                      clock=FixedClock(start=T0, step=1))


def _seed_map(cfg):
    """dat-nguyen (two aliases incl a VN-diacritic surface) + dat-tran, plus a PRIVATE bob."""
    db, w = _writer(cfg)
    try:
        with w.transaction():
            w.upsert_doc("d.md", "raw/d.md", "raw", sha256_hex("d"), domain="general", private=0)
            w.upsert_block({"block_id": "d.md#0", "block_content_id": "d0", "occurrence_index": 0,
                            "doc_id": "d.md", "ordinal": 0,
                            "text": "[[Dat Nguyen]] works at [[Acme]]. [[Dat Tran]] works at [[Beta]].",
                            "domain": "general"})
            for nid, name in (("dat-nguyen", "Dat Nguyen"), ("dat-tran", "Dat Tran"),
                              ("acme", "Acme"), ("beta", "Beta")):
                w.upsert_node(nid, "entity", name)
                w.add_node_block(nid, "d.md#0")
            w.add_alias("dat-nguyen", "Dat Nguyen", "dat nguyen", source_block_id="d.md#0")
            w.add_alias("dat-nguyen", "Đạt Nguyễn", "dat nguyen",
                        kind="declared", source_block_id="d.md#0")
            w.upsert_edge({"edge_id": "e_dn", "subj_node": "dat-nguyen", "predicate": "works_at",
                           "obj_node": "acme", "obj_datatype": "node", "source_block_id": "d.md#0",
                           "source_doc_id": "d.md", "source_quote": "Dat Nguyen works at Acme",
                           "extractor": "deterministic"})
            w.upsert_edge({"edge_id": "e_dt", "subj_node": "dat-tran", "predicate": "works_at",
                           "obj_node": "beta", "obj_datatype": "node", "source_block_id": "d.md#0",
                           "source_doc_id": "d.md", "source_quote": "Dat Tran works at Beta",
                           "extractor": "deterministic"})
            # a PRIVATE assertion that must never reach the public projection
            w.upsert_doc("p.md", "wiki/private/p.md", "wiki", sha256_hex("p"),
                         domain="personal", private=1)
            w.upsert_block({"block_id": "p.md#0", "block_content_id": "p0", "occurrence_index": 0,
                            "doc_id": "p.md", "ordinal": 0, "text": "[[Bob]] works at [[Secret]].",
                            "domain": "personal"})
            w.upsert_node("bob", "person", "Bob")
            w.upsert_node("secret", "org", "Secret")
            w.add_node_block("bob", "p.md#0")
            w.upsert_edge({"edge_id": "e_bob", "subj_node": "bob", "predicate": "works_at",
                           "obj_node": "secret", "obj_datatype": "node", "source_block_id": "p.md#0",
                           "source_doc_id": "p.md", "source_quote": "Bob works at Secret",
                           "extractor": "deterministic"})
    finally:
        db.close()


class _Base(unittest.TestCase):
    def setUp(self):
        self.vault = Path(tempfile.mkdtemp(prefix="tewiki_proj_"))
        self.addCleanup(shutil.rmtree, self.vault, ignore_errors=True)
        self.cfg = Config.for_vault(self.vault)


def _line_for(content, slug):
    for ln in content.splitlines():
        if ln.split("|", 1)[0] == slug:
            return ln
    return None


class TestDm4Map(_Base):
    def test_byte_identical_across_two_builds(self):
        _seed_map(self.cfg)
        first = A.build_map(self.cfg)
        second = A.build_map(self.cfg)
        self.assertEqual(first, second)
        self.assertTrue(first.endswith("\n"))

    def test_lines_sorted_by_slug_and_public_only(self):
        _seed_map(self.cfg)
        content = A.build_map(self.cfg)
        slugs = [ln.split("|", 1)[0] for ln in content.splitlines() if ln]
        self.assertIn("acme", slugs)
        self.assertIn("dat-nguyen", slugs)
        self.assertEqual(slugs, sorted(slugs),
                         msg="map.md lines must be ORDER BY slug, not insertion/DESC order")
        # NEGATIVE: the private node never appears in the public projection.
        self.assertNotIn("bob", slugs)
        self.assertNotIn("secret", slugs)

    def test_aliases_sorted_ascending_and_vn_findable(self):
        _seed_map(self.cfg)
        content = A.build_map(self.cfg)
        line = _line_for(content, "dat-nguyen")
        self.assertIsNotNone(line)
        aliases_field = line.split("|")[8]
        self.assertEqual(aliases_field, "Dat Nguyen,Đạt Nguyễn",
                         msg="aliases must be sorted ascending within the line")
        self.assertIn("Đạt Nguyễn", line)


class TestDm9Freshness(_Base):
    def test_compiled_tree_is_fresh(self):
        _seed_map(self.cfg)
        A.compile_all(self.cfg)
        res = F.check(self.cfg)
        self.assertTrue(res.fresh, msg=f"unexpected stale: {res.stale}")
        self.assertEqual(res.exit_code, 0)
        F.assert_fresh(self.cfg)                     # must not raise

    def test_generated_line_does_not_trip_staleness(self):
        _seed_map(self.cfg)
        A.compile_all(self.cfg)
        self.assertTrue(F.check(self.cfg).fresh)

    def test_hand_edited_artifact_is_stale_and_fails_closed(self):
        _seed_map(self.cfg)
        A.compile_all(self.cfg)
        p = self.cfg.projection_dir / A.MAP_NAME
        p.write_text(p.read_text(encoding="utf-8") + "forged|x|x|x|x|x|x|x|x|x|x|x\n",
                     encoding="utf-8")
        res = F.check(self.cfg)
        self.assertFalse(res.fresh)
        self.assertEqual(res.exit_code, 1)
        self.assertIn(A.MAP_NAME, res.stale)
        with self.assertRaises(F.StaleArtifactError):
            F.assert_fresh(self.cfg)

    def test_missing_artifact_is_stale(self):
        _seed_map(self.cfg)
        A.compile_all(self.cfg)
        (self.cfg.projection_dir / A.EDGES_NAME).unlink()
        res = F.check(self.cfg)
        self.assertFalse(res.fresh)
        self.assertIn(A.EDGES_NAME, res.stale)


class TestDm13Parity(unittest.TestCase):
    CLEAN = {
        "a.md": "[[Alice]] works at [[Acme]].\n",
        "b.md": "[[Bob]] founded [[Beta]].\n",
    }
    LOSSY = {
        "a.md": "[[Alice]] works at [[Acme]].\n",
        "loss.md": "[[Mallory]] despises [[Nemo]].\n",
    }

    def test_clean_pages_zero_unaccounted(self):
        # surviving edges reproduce every in-vocabulary structural edge
        actual_docs = set(self.CLEAN)
        actual_nodes = {"alice", "acme", "bob", "beta"}
        actual_edges = [("alice", "works_at", "acme", "a.md"),
                        ("bob", "founded", "beta", "b.md")]
        report = CP.reconcile(self.CLEAN, actual_docs, actual_nodes, actual_edges)
        self.assertTrue(report.ok)
        self.assertEqual(report.unaccounted, 0)
        self.assertEqual(report.matched_edges, report.source_edges)

    def test_out_of_vocab_edge_is_unaccounted(self):
        # 'despises' is out of vocabulary: no surviving edge, so it is reported, not dropped
        actual_docs = set(self.LOSSY)
        actual_nodes = {"alice", "acme", "mallory", "nemo"}
        actual_edges = [("alice", "works_at", "acme", "a.md")]
        report = CP.reconcile(self.LOSSY, actual_docs, actual_nodes, actual_edges)
        self.assertFalse(report.ok)
        self.assertEqual(report.unaccounted, 1)
        self.assertEqual(report.missing_edges, [("mallory", "nemo", "loss.md")])
        self.assertEqual(report.missing_docs, [])
        self.assertEqual(report.missing_nodes, [])

    def test_report_serializable_and_ok_property(self):
        r = CP.ParityReport()
        self.assertTrue(r.ok)
        r.missing_edges.append(("x", "y", "z.md"))
        self.assertFalse(r.ok)
        d = r.as_dict()
        self.assertEqual(d["unaccounted"], 1)
        self.assertIn(["x", "y", "z.md"], d["missing_edges"])

    def test_edge_parity_is_a_consumed_multiset(self):
        # one surviving edge cannot satisfy two distinct source assertions with the same endpoints
        pages = {"a.md": "[[Alice]] works at [[Acme]].\n",
                 "b.md": "[[Alice]] despises [[Acme]].\n"}
        report = CP.reconcile(pages, set(pages), {"alice", "acme"},
                              [("alice", "works_at", "acme", "a.md")])
        self.assertEqual(report.source_edges, 2)
        self.assertEqual(report.matched_edges, 1)
        self.assertEqual(report.missing_edges, [("alice", "acme", "b.md")])


class TestDbAloneLineage(_Base):
    def _seed_and_emit(self):
        _seed_map(self.cfg)
        out = self.vault.parent / (self.vault.name + "_extract") / "vault"
        self.addCleanup(shutil.rmtree, out.parent, ignore_errors=True)
        extract.emit(self.vault, out, clock=FixedClock(start="2030-01-01T00:00:00+00:00", step=0))
        return out

    def test_lineage_from_extracted_db_alone(self):
        out = self._seed_and_emit()
        db = DB(Config.for_vault(out))
        try:
            chain = lineage(db, "e_dn")
            self.assertEqual([w["obj"] for w in chain], ["acme"])
            self.assertIsNone(chain[0]["believed_until"])   # still believed
        finally:
            db.close()

    def test_lineage_survives_removing_the_ledger(self):
        out = self._seed_and_emit()
        (out / "ledger" / "events.jsonl").unlink()
        self.assertFalse((out / "ledger" / "events.jsonl").exists())
        db = DB(Config.for_vault(out))
        try:
            chain = lineage(db, "e_dn")                      # DB-alone, no events.jsonl
            self.assertEqual([w["obj"] for w in chain], ["acme"])
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
