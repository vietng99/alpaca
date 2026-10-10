"""The projected page of every node gets a file name that is safe, stable and its own.

`wiki extract` renders one markdown page per public node. The page name was `<node id>.md`, and a
node id may hold a path separator: every proof node does (`proof:.alpaca/proofs/op-008/t-065.md`,
`alpaca/wiki/ingest/record_extract.py` `_proof`), and the projection's writer refuses such a name
(`UnsafeProjectionPath`), so an extract of any store holding a proof node failed (t-070 s4).

The rule held here: an id that already made a safe name keeps `<id>.md`; any other id (a separator,
a control character, `.` or `..`, a leading `%`, a name an artifact of the projection owns, a name
longer than the file system allows) gets a percent-encoded name that starts with `%`, so it can meet
neither a kept name nor another encoded one. map.md names the same file as the page.
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from alpaca.wiki import extract
from alpaca.wiki.clock import FixedClock
from alpaca.wiki.config import Config
from alpaca.wiki.determinism import sha256_hex
from alpaca.wiki.store.db import DB
from alpaca.wiki.store.ledger import Ledger
from alpaca.wiki.store.write import Writer
from alpaca.wiki.project import artifacts as A

T0 = "2020-01-01T00:00:00+00:00"
STAMP = "2030-01-01T00:00:00+00:00"

PROOF = "proof:.alpaca/proofs/op-008/t-065.md"
REMOTE = "remote:https://example.org/a/b"
# Ids that each break the old `<id>.md` rule in a different way, next to safe ones.
UNSAFE = (PROOF, REMOTE, "..", ".", "map", "snapshot", "gaps-candidates", "%lead",
          "back\\slash", "tab\there", "x" * 300)
SAFE = ("acme", "dat-nguyen", "status:done", "error:TranscriptUnavailable", "dec-392bf1d64a89",
        "a%2Fb", "proof%3A.alpaca")


def _seed(cfg):
    db = DB(cfg)
    db.pour()
    w = Writer(db, Ledger(cfg.ledger_path, vault_dir=cfg.vault_dir),
               clock=FixedClock(start=T0, step=1))
    try:
        with w.transaction():
            w.upsert_doc("d.md", "raw/d.md", "raw", sha256_hex("d"), domain="general", private=0)
            w.upsert_block({"block_id": "d.md#0", "block_content_id": "d0", "occurrence_index": 0,
                            "doc_id": "d.md", "ordinal": 0,
                            "text": "t-065 proof is local:.alpaca/proofs/op-008/t-065.md.",
                            "domain": "general"})
            for nid in SAFE + UNSAFE:
                w.upsert_node(nid, "entity", nid[:40])
                w.add_node_block(nid, "d.md#0")
            w.upsert_edge({"edge_id": "e_proof", "subj_node": "acme", "predicate": "works_at",
                           "obj_node": PROOF, "obj_datatype": "node", "source_block_id": "d.md#0",
                           "source_doc_id": "d.md", "source_quote": "t-065 proof is",
                           "extractor": "deterministic"})
            w.upsert_edge({"edge_id": "e_back", "subj_node": PROOF, "predicate": "works_at",
                           "obj_node": "acme", "obj_datatype": "node", "source_block_id": "d.md#0",
                           "source_doc_id": "d.md", "source_quote": "t-065 proof is",
                           "extractor": "deterministic"})
    finally:
        db.close()


class TestPageName(unittest.TestCase):
    def test_safe_ids_keep_their_name(self):
        for nid in SAFE:
            self.assertEqual(A.page_name(nid), f"{nid}.md")

    def test_every_name_is_one_plain_file_name(self):
        for nid in SAFE + UNSAFE:
            name = A.page_name(nid)
            self.assertTrue(name.endswith(".md"), name)
            self.assertEqual(Path(name).name, name)
            self.assertNotIn("/", name)
            self.assertNotIn("\\", name)
            self.assertNotIn(name, (".", "..", ".md"))
            self.assertLessEqual(len(name.encode("utf-8")), 255)
            self.assertTrue(name.isprintable(), name)

    def test_unsafe_ids_get_an_encoded_name(self):
        for nid in UNSAFE:
            self.assertTrue(A.page_name(nid).startswith("%"), nid)
        self.assertEqual(A.page_name(PROOF), "%proof%3A.alpaca%2Fproofs%2Fop-008%2Ft-065.md.md")

    def test_names_never_collide_and_never_take_an_artifact_name(self):
        ids = SAFE + UNSAFE + ("/", "%2F", "%", "%25", "x" * 301, "map.md", "edges")
        names = [A.page_name(nid) for nid in ids]
        self.assertEqual(len(set(names)), len(names), names)
        for owned in (A.MAP_NAME, A.SNAPSHOT_NAME, A.GAPS_NAME, A.EDGES_NAME):
            self.assertNotIn(owned, names)

    def test_names_are_stable(self):
        for nid in SAFE + UNSAFE:
            self.assertEqual(A.page_name(nid), A.page_name(str(nid)))


class TestExtractWithUnsafeIds(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.mkdtemp(prefix="tewiki_projnames_"))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.vault = base / "vault"
        self.vault.mkdir()
        self.out = base / "out" / "vault"
        _seed(Config.for_vault(self.vault))

    def _emit(self):
        return extract.emit(self.vault, self.out, clock=FixedClock(start=STAMP, step=0))

    def test_a_store_holding_a_proof_node_extracts(self):
        self._emit()                                # raised UnsafeProjectionPath before the fix
        proj = Config.for_vault(self.out).projection_dir
        files = {p.name for p in proj.iterdir()}
        self.assertTrue(all(p.parent == proj for p in proj.rglob("*")),
                        "a page was written outside the projection directory")
        page = proj / A.page_name(PROOF)
        self.assertIn(page.name, files)
        text = page.read_text(encoding="utf-8")
        self.assertIn("**works_at**", text)
        self.assertIn("edge:`e_back`", text)

    def test_one_page_per_node_and_map_names_each_page(self):
        self._emit()
        cfg = Config.for_vault(self.out)
        proj = cfg.projection_dir
        files = {p.name for p in proj.iterdir()}
        db = DB(cfg)
        try:
            nodes = [r["node_id"] for r in db.conn.execute(
                f"SELECT node_id FROM nodes n WHERE status='active' AND {A._PUBLIC_NODE_SQL}")]
        finally:
            db.close()
        self.assertEqual(sorted(nodes), sorted(SAFE + UNSAFE))
        pages = {A.page_name(nid) for nid in nodes}
        self.assertEqual(len(pages), len(nodes))
        self.assertEqual(files - pages, {A.MAP_NAME, A.EDGES_NAME, A.SNAPSHOT_NAME, A.GAPS_NAME})
        self.assertTrue(pages <= files)
        # map.md is still the map (no node page overwrote it) and names the page file of each node
        map_lines = (proj / A.MAP_NAME).read_text(encoding="utf-8").splitlines()
        self.assertEqual(map_lines, A.build_map(cfg, write=False).splitlines())
        named = {ln.split("|")[1] for ln in map_lines if ln}
        self.assertEqual(named, pages)
        self.assertTrue((proj / "acme.md").exists())
        self.assertTrue((proj / "dat-nguyen.md").exists())

    def test_two_extracts_are_byte_identical(self):
        first = self._emit()
        second = self._emit()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
