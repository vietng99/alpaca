"""M2.15 proof: `alpaca wiki extract` emits a portable, standalone Rune-2 vault.

Done-when (spec:911): the extract opens as a standalone vault with its own schema and ledger,
carries no path or identifier tied to this machine, excludes every row whose label is not public,
and is byte-identical across two runs under a fixed clock. Every property below is proved on BOTH a
positive and a negative path.

The store is built directly through the vendored write door (alpaca.wiki.store.write.Writer), the M2.8
pattern the sibling wiki tests use: the ingest absorber (M2.14) is not vendored here, so the public
rows are seeded straight against the store surface the extract reads.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from alpaca.wiki.clock import FixedClock
from alpaca.wiki.config import Config
from alpaca.wiki.determinism import canonical_json, sha256_hex
from alpaca.wiki.store.db import DB
from alpaca.wiki.store.ledger import Ledger, verify_chain
from alpaca.wiki.store.write import Writer
from alpaca.wiki import extract

T0 = "2020-01-01T00:00:00+00:00"
EMIT_CLOCK = "2030-01-01T00:00:00+00:00"


def _seed(vault: Path, *, lossy: bool = True, private: bool = True) -> None:
    """A public assertion (alice->acme), an optional PRIVATE assertion (bob->secretco under
    wiki/private/), and an optional LOSSY public page (mallory despises nemo: an out-of-vocabulary
    connector whose structural edge is never extracted)."""
    cfg = Config.for_vault(vault)
    db = DB(cfg)
    db.pour()
    try:
        w = Writer(db, Ledger(cfg.ledger_path, vault_dir=cfg.vault_dir),
                   clock=FixedClock(start=T0, step=1))
        with w.transaction():
            w.upsert_doc("a.md", "raw/a.md", "raw", sha256_hex("t1"), domain="general", private=0)
            w.upsert_block({"block_id": "a.md#0", "block_content_id": "ac0", "occurrence_index": 0,
                            "doc_id": "a.md", "ordinal": 0,
                            "text": "[[Alice]] works at [[Acme]].", "domain": "general"})
            w.upsert_node("alice", "person", "Alice")
            w.upsert_node("acme", "org", "Acme")
            w.add_node_block("alice", "a.md#0")
            w.add_node_block("acme", "a.md#0")
            w.add_alias("alice", "Alice", "alice", source_block_id="a.md#0")
            w.upsert_edge({"edge_id": "e_alice_acme", "subj_node": "alice", "predicate": "works_at",
                           "obj_node": "acme", "obj_datatype": "node", "source_block_id": "a.md#0",
                           "source_doc_id": "a.md", "source_quote": "Alice works at Acme",
                           "extractor": "deterministic"})
            if private:
                w.upsert_doc("s.md", "wiki/private/s.md", "wiki", sha256_hex("secret"),
                             domain="personal", private=1)
                w.upsert_block({"block_id": "s.md#0", "block_content_id": "sc0",
                                "occurrence_index": 0, "doc_id": "s.md", "ordinal": 0,
                                "text": "[[Bob]] works at [[SecretCo]].", "domain": "personal"})
                w.upsert_node("bob", "person", "Bob")
                w.upsert_node("secretco", "org", "SecretCo")
                w.add_node_block("bob", "s.md#0")
                w.add_node_block("secretco", "s.md#0")
                w.upsert_edge({"edge_id": "e_bob_secret", "subj_node": "bob",
                               "predicate": "works_at", "obj_node": "secretco",
                               "obj_datatype": "node", "source_block_id": "s.md#0",
                               "source_doc_id": "s.md", "source_quote": "Bob works at SecretCo",
                               "extractor": "deterministic"})
            if lossy:
                w.upsert_doc("loss.md", "raw/loss.md", "raw", sha256_hex("loss"),
                             domain="general", private=0)
                w.upsert_block({"block_id": "loss.md#0", "block_content_id": "lc0",
                                "occurrence_index": 0, "doc_id": "loss.md", "ordinal": 0,
                                "text": "[[Mallory]] despises [[Nemo]].", "domain": "general"})
                w.upsert_node("mallory", "person", "Mallory")
                w.upsert_node("nemo", "person", "Nemo")
                w.add_node_block("mallory", "loss.md#0")
                w.add_node_block("nemo", "loss.md#0")
    finally:
        db.close()


class _Base(unittest.TestCase):
    def setUp(self):
        self.src = Path(tempfile.mkdtemp(prefix="tewiki_src_"))
        self.addCleanup(shutil.rmtree, self.src, ignore_errors=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="tewiki_out_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _out(self, name: str) -> Path:
        return self.tmp / name / "vault"

    def _emit(self, name: str, **kw):
        return extract.emit(self.src, self._out(name),
                            clock=FixedClock(start=EMIT_CLOCK, step=0))


class TestStandaloneOpen(_Base):
    def test_opens_as_standalone_vault_with_own_schema_and_ledger(self):
        # POSITIVE: the emitted vault opens on its own, carries the public graph, and its ledger
        # verifies from the vault alone.
        _seed(self.src)
        manifest = self._emit("a")
        out = self._out("a")
        self.assertTrue((out / "rune.db").exists())
        self.assertTrue((out / "ledger" / "events.jsonl").exists())
        db = DB(Config.for_vault(out))
        try:
            docs = {r[0] for r in db.conn.execute("SELECT doc_id FROM docs")}
            nodes = {r[0] for r in db.conn.execute("SELECT node_id FROM nodes")}
            edges = {r[0] for r in db.conn.execute("SELECT edge_id FROM edges")}
            self.assertIn("a.md", docs)
            self.assertSetEqual({"alice", "acme"} & nodes, {"alice", "acme"})
            self.assertIn("e_alice_acme", edges)
            rows = [dict(seq=r["seq"], prev_checksum=r["prev_checksum"], checksum=r["checksum"],
                         op=r["op"], doc_id=r["doc_id"], payload=json.loads(r["payload"]))
                    for r in db.conn.execute("SELECT * FROM ingest_event ORDER BY seq")]
            self.assertGreater(len(rows), 0)
            ok, why = verify_chain(rows)
            self.assertTrue(ok, why)
        finally:
            db.close()

    def test_manifest_reports_public_counts(self):
        _seed(self.src)
        manifest = self._emit("a")
        self.assertEqual(manifest["schema"], extract.EXTRACT_SCHEMA)
        self.assertGreaterEqual(manifest["counts"]["nodes"], 2)
        self.assertEqual(manifest["counts"]["edges"], 1)


class TestPrivateExcluded(_Base):
    def test_private_rows_are_excluded_public_rows_survive(self):
        # NEGATIVE: the private doc/block/node/edge never appear in the standalone vault or its
        # projection; POSITIVE: the public ones do.
        _seed(self.src)
        self._emit("a")
        out = self._out("a")
        db = DB(Config.for_vault(out))
        try:
            docs = {r[0] for r in db.conn.execute("SELECT doc_id FROM docs")}
            nodes = {r[0] for r in db.conn.execute("SELECT node_id FROM nodes")}
            edges = {r[0] for r in db.conn.execute("SELECT edge_id FROM edges")}
            self.assertNotIn("s.md", docs)                 # private doc excluded
            self.assertNotIn("bob", nodes)                 # private node excluded
            self.assertNotIn("secretco", nodes)
            self.assertNotIn("e_bob_secret", edges)        # private edge excluded
            self.assertIn("a.md", docs)                    # public survives
            self.assertIn("alice", nodes)
        finally:
            db.close()
        blob = "\n".join(p.read_text(encoding="utf-8")
                         for p in (out / "projection").glob("*"))
        self.assertNotIn("Bob", blob)
        self.assertNotIn("SecretCo", blob)
        self.assertNotIn("secretco", blob)

    def test_no_private_edge_even_when_only_private_present(self):
        # A vault of ONLY private assertions extracts to an empty public graph (fail-closed).
        _seed(self.src, lossy=False)
        # rewrite: make a.md private too by re-seeding a private-only vault
        src2 = Path(tempfile.mkdtemp(prefix="tewiki_priv_"))
        self.addCleanup(shutil.rmtree, src2, ignore_errors=True)
        cfg = Config.for_vault(src2)
        db = DB(cfg); db.pour()
        try:
            w = Writer(db, Ledger(cfg.ledger_path, vault_dir=cfg.vault_dir),
                       clock=FixedClock(start=T0, step=1))
            with w.transaction():
                w.upsert_doc("p.md", "wiki/private/p.md", "wiki", sha256_hex("x"),
                             domain="personal", private=1)
                w.upsert_block({"block_id": "p.md#0", "block_content_id": "pc0",
                                "occurrence_index": 0, "doc_id": "p.md", "ordinal": 0,
                                "text": "[[Zed]] works at [[Hidden]].", "domain": "personal"})
                w.upsert_node("zed", "person", "Zed")
                w.upsert_node("hidden", "org", "Hidden")
                w.add_node_block("zed", "p.md#0")
                w.upsert_edge({"edge_id": "e_zed", "subj_node": "zed", "predicate": "works_at",
                               "obj_node": "hidden", "obj_datatype": "node",
                               "source_block_id": "p.md#0", "source_doc_id": "p.md",
                               "source_quote": "Zed works at Hidden", "extractor": "deterministic"})
        finally:
            db.close()
        out = self.tmp / "priv" / "vault"
        m = extract.emit(src2, out, clock=FixedClock(start=EMIT_CLOCK, step=0))
        self.assertEqual(m["counts"]["docs"], 0)
        self.assertEqual(m["counts"]["edges"], 0)
        self.assertEqual(m["counts"]["nodes"], 0)


class TestDeterminism(_Base):
    def test_byte_identical_across_two_runs_under_fixed_clock(self):
        # POSITIVE: two emits under the same fixed clock are byte-identical (manifest, files, db).
        _seed(self.src)
        m1 = self._emit("a")
        m2 = self._emit("b")
        self.assertEqual(canonical_json(m1), canonical_json(m2))
        self.assertEqual(m1["files"], m2["files"])
        self.assertEqual(m1["db_content_sha256"], m2["db_content_sha256"])
        # the on-disk manifest and ledger bytes match too
        self.assertEqual((self._out("a") / "manifest.json").read_bytes(),
                         (self._out("b") / "manifest.json").read_bytes())
        self.assertEqual((self._out("a") / "ledger" / "events.jsonl").read_bytes(),
                         (self._out("b") / "ledger" / "events.jsonl").read_bytes())

    def test_different_content_changes_the_manifest(self):
        # NEGATIVE control: the digest is content-sensitive, so byte-identity is not vacuous.
        _seed(self.src)
        m1 = self._emit("a")
        src2 = Path(tempfile.mkdtemp(prefix="tewiki_src2_"))
        self.addCleanup(shutil.rmtree, src2, ignore_errors=True)
        _seed(src2, lossy=False, private=False)  # only the public alice->acme assertion
        m2 = extract.emit(src2, self.tmp / "c" / "vault",
                          clock=FixedClock(start=EMIT_CLOCK, step=0))
        self.assertNotEqual(m1["db_content_sha256"], m2["db_content_sha256"])


class TestNoMachinePath(_Base):
    def test_output_carries_no_absolute_source_path(self):
        # POSITIVE: no absolute path tied to this machine leaks into the manifest / ledger /
        # projection; every recorded doc path is vault-relative.
        _seed(self.src)
        manifest = self._emit("a")
        out = self._out("a")
        src_abs = str(self.src)
        out_abs = str(out)
        bodies = [(out / "manifest.json").read_text(encoding="utf-8"),
                  (out / "ledger" / "events.jsonl").read_text(encoding="utf-8")]
        bodies += [p.read_text(encoding="utf-8") for p in (out / "projection").glob("*")]
        for body in bodies:
            self.assertNotIn(src_abs, body)
            self.assertNotIn(out_abs, body)
        for rel in manifest["files"]:
            self.assertFalse(rel.startswith("/"), rel)
        db = DB(Config.for_vault(out))
        try:
            for (path,) in db.conn.execute("SELECT path FROM docs"):
                self.assertFalse(Path(path).is_absolute(), path)
        finally:
            db.close()


class TestUnaccountedNeverSilentlyDropped(_Base):
    def test_lost_structural_edge_is_reported_unaccounted(self):
        # NEGATIVE: the lossy page's structural edge (out-of-vocabulary 'despises') has no surviving
        # twin, so it is listed UNACCOUNTED - reported, never dropped silently.
        _seed(self.src)
        manifest = self._emit("a")
        parity = manifest["parity"]
        self.assertFalse(parity["ok"])
        self.assertEqual(parity["unaccounted"], 1)
        self.assertIn(["mallory", "nemo", "loss.md"], parity["missing_edges"])
        # the docs and nodes themselves DID travel - only the structural edge was lost
        self.assertEqual(parity["missing_docs"], [])
        self.assertEqual(parity["missing_nodes"], [])

    def test_clean_vault_reports_zero_unaccounted(self):
        # POSITIVE: with every connector in-vocabulary, nothing is unaccounted.
        _seed(self.src, lossy=False, private=False)
        manifest = self._emit("a")
        self.assertTrue(manifest["parity"]["ok"])
        self.assertEqual(manifest["parity"]["unaccounted"], 0)


class TestCliVerbDirection(_Base):
    def test_extract_verb_emits_and_states_direction(self):
        from alpaca import cli
        _seed(self.src)
        code = cli.main(["wiki", "extract", str(self.src), str(self._out("cli"))])
        self.assertEqual(code, 0)
        self.assertTrue((self._out("cli") / "manifest.json").exists())

    def test_verb_help_states_the_extract_leaves_alpaca(self):
        from alpaca import cli
        _parser, sub = cli.build_parser()
        # the registered wiki/extract help states the one-way direction
        help_text = sub.choices["wiki"].format_help().lower()
        self.assertIn("leaves alpaca", help_text)
        self.assertIn("nothing", help_text)


if __name__ == "__main__":
    unittest.main()
