"""M2.11 second proof module: a build of SQLite without FTS5 (the fallback path), vector-arm
hardening, PPR adjacency containment, and deterministic communities.

Ported (adapted to the vendored alpaca.wiki surface) from the upstream Rune-2 suite:
  tests/test_fts_fallback.py     - a fresh store works with no FTS5 module; the fallback schema
                                   transform drops ONLY the fts virtual table; BM25 still ranks;
  tests/test_vec_hardening.py    - the sqlite-vec load seam always disables extension loading,
                                   validates the embedding dimension, and skips corrupt vectors;
  tests/test_ppr_containment.py  - the PPR adjacency excludes expired / off-domain sources and the
                                   compartment defaults to 'general' until explicitly widened;
  tests/test_km7_communities.py  - deterministic label-propagation communities (min-node id,
                                   list-valued straddlers, byte-identical partition hash).

The upstream ppr-containment tests drive the ingest pipeline (absorber, M2.14). That pipeline is not
vendored in M2.11, so the SAME containment properties are proved on a Writer/SQL-seeded graph (no
absorb). A minimal compartment stand-in carries domain_filter, mirroring how the real M2.12 module
will win via setdefault.

Done-when #1 (the same ranked ids with and without FTS5) is carried here too, at the search_bm25
level, so both proof modules pin the deterministic fallback.
"""
from __future__ import annotations

import builtins
import math
import shutil
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# --- stand-in for the not-yet-vendored compartment sibling (real module lands in M2.12) ----------
_comp = sys.modules.get("alpaca.wiki.engine.compartment")
if _comp is None:
    _comp = types.ModuleType("alpaca.wiki.engine.compartment")
    sys.modules["alpaca.wiki.engine.compartment"] = _comp
if not hasattr(_comp, "domain_filter"):
    _comp.domain_filter = lambda sql, domain: (sql + " AND domain=?", (domain,))
if not hasattr(_comp, "PrivatePathViolation"):
    _comp.PrivatePathViolation = type("PrivatePathViolation", (RuntimeError,), {})
if not hasattr(_comp, "is_private_path"):
    _comp.is_private_path = lambda path: False
if not hasattr(_comp, "assert_private_path"):
    _comp.assert_private_path = lambda private, path: None

from alpaca.wiki.clock import FixedClock
from alpaca.wiki.config import Config
from alpaca.wiki.engine import retrieve
from alpaca.wiki.engine.tiering import community_hash, label_propagation, recompute_communities
from alpaca.wiki.store import db as db_module
from alpaca.wiki.store import vec
from alpaca.wiki.store.asof import AsOf
from alpaca.wiki.store.db import DB
from alpaca.wiki.store.fts import search_bm25
from alpaca.wiki.store.ledger import Ledger
from alpaca.wiki.store.write import Writer

PAST = "2020-01-01T00:00:00+00:00"
NOW = "2026-01-01T00:00:00+00:00"


def _fresh(prefix):
    vault = Path(tempfile.mkdtemp(prefix=prefix))
    return Config.for_vault(vault)


# ============================================================ FTS5 fallback build
class TestFtsFallback(unittest.TestCase):
    def test_fresh_store_works_without_fts5_module(self):
        # POSITIVE: with FTS5 forced absent the store still pours, writes and ranks by BM25 fallback.
        cfg = _fresh("tewiki_m211_ftsfb_")
        self.addCleanup(shutil.rmtree, str(cfg.vault_dir), ignore_errors=True)
        with mock.patch("alpaca.wiki.store.db._fts5_available", return_value=False):
            db = DB(cfg)
            db.pour()
        writer = Writer(db, Ledger(cfg.ledger_path, vault_dir=cfg.vault_dir),
                        clock=FixedClock(start=PAST))
        with writer.transaction():
            writer.upsert_doc("raw/d.md", "raw/d.md", "raw", "sha", private=0)
            writer.upsert_block({
                "block_id": "raw/d.md#b_test", "block_content_id": "content",
                "occurrence_index": 0, "doc_id": "raw/d.md", "ordinal": 0,
                "text": "Ada works at Acme", "domain": "general",
            })
        definition = db.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='blocks_fts'"
        ).fetchone()[0]
        self.assertNotIn("USING fts5", definition)
        self.assertEqual(search_bm25(db, "Ada Acme")[0][0], "raw/d.md#b_test")
        db.close()

    def test_fallback_transform_removes_virtual_table_only_from_fts_section(self):
        transformed = db_module._schema_with_fts_fallback(db_module.load_schema_sql())
        self.assertIn("CREATE TABLE IF NOT EXISTS blocks_fts", transformed)
        self.assertNotIn("CREATE VIRTUAL TABLE IF NOT EXISTS blocks_fts", transformed)
        self.assertIn("CREATE TABLE IF NOT EXISTS answers", transformed)


# ============================================================ deterministic fallback (Done-when #1)
class TestBm25FallbackParity(unittest.TestCase):
    def _seed(self, db, cfg):
        w = Writer(db, Ledger(cfg.ledger_path, vault_dir=cfg.vault_dir), clock=FixedClock(start=PAST))
        with w.transaction():
            for i, text in enumerate(("Ada works at Acme Corporation",
                                      "Grace works at Beta Industries",
                                      "the weather is nice today")):
                doc = f"raw/d{i}.md"
                w.upsert_doc(doc, doc, "raw", f"sha{i}", private=0)
                w.upsert_block({"block_id": f"{doc}#0", "block_content_id": f"c{i}",
                                "occurrence_index": 0, "doc_id": doc, "ordinal": 0,
                                "text": text, "domain": "general"})

    def test_search_bm25_returns_the_same_ids_with_and_without_fts5(self):
        q = "Where does Ada work at Acme"
        cfg1 = _fresh("tewiki_m211_par5_")
        self.addCleanup(shutil.rmtree, str(cfg1.vault_dir), ignore_errors=True)
        db1 = DB(cfg1)
        db1.pour()
        self.addCleanup(db1.close)
        self._seed(db1, cfg1)

        cfg0 = _fresh("tewiki_m211_par0_")
        self.addCleanup(shutil.rmtree, str(cfg0.vault_dir), ignore_errors=True)
        with mock.patch("alpaca.wiki.store.db._fts5_available", return_value=False):
            db0 = DB(cfg0)
            db0.pour()
        self.addCleanup(db0.close)
        self._seed(db0, cfg0)

        self.assertIn("USING fts5",
                      db1.conn.execute("SELECT sql FROM sqlite_master WHERE name='blocks_fts'").fetchone()[0])
        self.assertNotIn("USING fts5",
                         db0.conn.execute("SELECT sql FROM sqlite_master WHERE name='blocks_fts'").fetchone()[0])

        ids_fts = [b for b, _ in search_bm25(db1, q, 50)]
        ids_fallback = [b for b, _ in search_bm25(db0, q, 50)]
        self.assertEqual(set(ids_fts), set(ids_fallback))
        self.assertEqual(ids_fts[0], ids_fallback[0])
        self.assertEqual(ids_fts[0], "raw/d0.md#0")


# ============================================================ vector-arm hardening
class _FakeConn:
    def __init__(self, fail_table: bool = False):
        self.fail_table = fail_table
        self.enable_calls: list[bool] = []
        self.sql: list[str] = []

    def enable_load_extension(self, enabled: bool) -> None:
        self.enable_calls.append(enabled)

    def execute(self, sql: str):
        self.sql.append(sql)
        if self.fail_table:
            raise RuntimeError("injected table failure")
        return self


class _FakeDB:
    def __init__(self, embed_dim="64", fail_table: bool = False):
        self.embed_dim = embed_dim
        self.conn = _FakeConn(fail_table=fail_table)
        self.meta: dict[str, str] = {}
        self.commits = 0

    def get_meta(self, key: str):
        return self.embed_dim if key == "embed_dim" else self.meta.get(key)

    def set_meta(self, key: str, value: str) -> None:
        self.meta[key] = value

    def commit(self) -> None:
        self.commits += 1


class TestExtensionDisable(unittest.TestCase):
    def test_import_failure_still_disables_extension_loading(self):
        db = _FakeDB()
        original_import = builtins.__import__

        def reject_sqlite_vec(name, *args, **kwargs):
            if name == "sqlite_vec":
                raise ImportError("injected import failure")
            return original_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=reject_sqlite_vec):
            self.assertFalse(vec.try_load_sqlite_vec(db))
        self.assertEqual(db.conn.enable_calls, [False])

    def test_load_failure_disables_after_enable(self):
        db = _FakeDB()
        module = SimpleNamespace(
            load=mock.Mock(side_effect=RuntimeError("injected load failure")),
            __version__="test",
        )
        with mock.patch.dict(sys.modules, {"sqlite_vec": module}):
            self.assertFalse(vec.try_load_sqlite_vec(db))
        self.assertEqual(db.conn.enable_calls, [True, False])

    def test_table_failure_disables_after_enable(self):
        db = _FakeDB(fail_table=True)
        module = SimpleNamespace(load=lambda conn: None, __version__="test")
        with mock.patch.dict(sys.modules, {"sqlite_vec": module}):
            self.assertFalse(vec.try_load_sqlite_vec(db))
        self.assertEqual(db.conn.enable_calls, [True, False])

    def test_success_disables_and_uses_validated_dimension(self):
        db = _FakeDB(embed_dim="128")
        module = SimpleNamespace(load=lambda conn: None, __version__="test")
        with mock.patch.dict(sys.modules, {"sqlite_vec": module}):
            self.assertTrue(vec.try_load_sqlite_vec(db))
        self.assertEqual(db.conn.enable_calls, [True, False])
        self.assertEqual(db.commits, 1)
        self.assertEqual(len(db.conn.sql), 2)
        self.assertTrue(all("float[128]" in sql for sql in db.conn.sql))


class TestDimensionValidation(unittest.TestCase):
    def test_dimension_bounds_and_integer_shape(self):
        self.assertEqual(vec._validated_embed_dim(_FakeDB("1")), 1)
        self.assertEqual(vec._validated_embed_dim(_FakeDB(str(vec.MAX_EMBED_DIM))), vec.MAX_EMBED_DIM)
        for invalid in ("", "0", "-1", "1.5", "abc", str(vec.MAX_EMBED_DIM + 1), True):
            with self.subTest(invalid=invalid):
                with self.assertRaises(vec.VectorValidationError):
                    vec._validated_embed_dim(_FakeDB(invalid))

    def test_invalid_dimension_fails_before_enable_and_still_disables(self):
        db = _FakeDB(embed_dim="0")
        self.assertFalse(vec.try_load_sqlite_vec(db))
        self.assertEqual(db.conn.enable_calls, [False])
        self.assertEqual(db.conn.sql, [])


class TestVectorDataValidation(unittest.TestCase):
    def setUp(self):
        self.cfg = _fresh("tewiki_m211_vecdata_")
        self.addCleanup(shutil.rmtree, str(self.cfg.vault_dir), ignore_errors=True)
        self.db = DB(self.cfg)
        self.db.pour()
        self.addCleanup(self.db.close)
        vec.ensure_vec_tables(self.db)
        self.db.set_meta("embed_dim", "2")
        self.db.conn.execute(
            "INSERT INTO docs(doc_id,path,kind,content_sha256,domain,tier,private) "
            "VALUES('vectors.md','vectors.md','raw','sha','general',1,0)"
        )
        for block_id in ("good", "malformed", "nonfinite", "misaligned", "wrongtype"):
            self.db.conn.execute(
                "INSERT INTO blocks(block_id,block_content_id,doc_id,ordinal,text,valid_from,"
                "recorded_at,status) VALUES(?,?,?,?,?,?,?,'active')",
                (block_id, "content-" + block_id, "vectors.md", 0, block_id, "2020", "2020"),
            )
        for node_id in ("good-node", "bad-node"):
            self.db.conn.execute(
                "INSERT INTO nodes(node_id,display_name,valid_from,recorded_at,status) "
                "VALUES(?,?,?,?,'active')", (node_id, node_id, "2020", "2020"),
            )
        self.db.conn.executemany(
            "INSERT INTO vec_blocks_fallback(block_id,embedding) VALUES(?,?)",
            (
                ("good", vec._pack([1.0, 0.0])),
                ("malformed", b"bad"),
                ("nonfinite", struct.pack("<2d", math.nan, 0.0)),
                ("misaligned", vec._pack([1.0, 0.0, 0.0])),
                ("wrongtype", "not-a-blob"),
            ),
        )
        self.db.conn.executemany(
            "INSERT INTO vec_nodes_fallback(node_id,embedding) VALUES(?,?)",
            (("good-node", vec._pack([1.0, 0.0])), ("bad-node", b"bad")),
        )
        self.db.commit()

    def test_pack_and_unpack_reject_invalid_values_and_shapes(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                with self.assertRaises(vec.VectorValidationError):
                    vec._pack([value, 0.0])
        for blob in (b"", b"bad", "not-a-blob"):
            with self.subTest(blob=blob):
                with self.assertRaises(vec.VectorValidationError):
                    vec._unpack(blob, expected_dim=2)
        with self.assertRaises(vec.VectorValidationError):
            vec._unpack(vec._pack([1.0, 0.0, 0.0]), expected_dim=2)

    def test_upsert_rejects_invalid_or_misaligned_vectors(self):
        with self.assertRaises(vec.VectorValidationError):
            vec.upsert_block_vec(self.db, "rejected", [1.0, math.nan])
        with self.assertRaises(vec.VectorValidationError):
            vec.upsert_node_vec(self.db, "rejected", [1.0])
        row = self.db.conn.execute(
            "SELECT 1 FROM vec_blocks_fallback WHERE block_id='rejected'"
        ).fetchone()
        self.assertIsNone(row)

    def test_knn_skips_corrupt_rows_and_rejects_bad_query(self):
        self.assertEqual(vec.knn_blocks(self.db, [1.0, 0.0]), [("good", 1.0)])
        self.assertEqual(vec.knn_nodes(self.db, [1.0, 0.0]), [("good-node", 1.0)])
        self.assertEqual(vec.knn_blocks(self.db, [math.nan, 0.0]), [])
        self.assertEqual(vec.knn_nodes(self.db, [1.0]), [])


# ============================================================ PPR adjacency containment
def _seed_domain_graph(db):
    """Two domains, each a single related_to edge; direct SQL (no absorb)."""
    c = db.conn
    for doc, dom in (("general.md", "general"), ("career.md", "career")):
        c.execute("INSERT INTO docs(doc_id,path,kind,content_sha256,domain,tier,private) "
                  "VALUES(?,?,'raw','sha',?,1,0)", (doc, doc, dom))
    blocks = {
        "general.md#0": ("general.md", "general"),
        "career.md#0": ("career.md", "career"),
    }
    for bid, (doc, dom) in blocks.items():
        c.execute("INSERT INTO blocks(block_id,block_content_id,doc_id,ordinal,text,valid_from,"
                  "recorded_at,status,domain) VALUES(?,?,?,0,'t',?,?,'active',?)",
                  (bid, "c_" + bid, doc, PAST, PAST, dom))
    for n in ("general-a", "general-b", "career-a", "career-b"):
        c.execute("INSERT INTO nodes(node_id,display_name,valid_from,recorded_at,status) "
                  "VALUES(?,?,?,?, 'active')", (n, n, PAST, PAST))
    edges = [
        ("eg", "general-a", "general-b", "general.md#0"),
        ("ec", "career-a", "career-b", "career.md#0"),
    ]
    for eid, subj, obj, sb in edges:
        c.execute("INSERT INTO edges(edge_id,subj_node,predicate,obj_node,obj_datatype,"
                  "source_block_id,source_quote,extractor,recorded_at,valid_from,status) "
                  "VALUES(?,?, 'related_to',?, 'node',?,'q','deterministic',?,?, 'active')",
                  (eid, subj, obj, sb, PAST, PAST))
    db.commit()


class TestPprContainment(unittest.TestCase):
    def setUp(self):
        self.cfg = _fresh("tewiki_m211_ppr_")
        self.addCleanup(shutil.rmtree, str(self.cfg.vault_dir), ignore_errors=True)
        self.db = DB(self.cfg)
        self.db.pour()
        self.addCleanup(self.db.close)

    def test_adjacency_excludes_expired_and_off_domain_sources(self):
        from alpaca.wiki.store.adjacency import build_ppr_edges
        _seed_domain_graph(self.db)
        # expire the general edge so it drops out even though its domain IS authorized.
        self.db.conn.execute(
            "UPDATE edges SET expires_at='2021-01-01T00:00:00+00:00' WHERE edge_id='eg'")
        self.db.commit()
        edges = build_ppr_edges(self.db, AsOf(NOW), authorized_domains={"general"})
        flat = {(s, t) for s, t, _w in edges}
        self.assertNotIn(("general-a", "general-b"), flat)   # expired -> excluded
        self.assertNotIn(("career-a", "career-b"), flat)     # off-domain -> excluded

    def test_default_domain_is_general_and_requires_explicit_widening(self):
        _seed_domain_graph(self.db)
        general = retrieve._domain_visible_blocks(self.db, None, False)
        widened = retrieve._domain_visible_blocks(self.db, None, True)
        domains = {r["block_id"]: r["domain"] for r in self.db.conn.execute(
            "SELECT block_id,domain FROM blocks WHERE status='active'").fetchall()}
        self.assertIsNotNone(general)
        self.assertEqual({domains[b] for b in general}, {"general"})   # default: general only
        self.assertIsNone(widened)                                      # widening -> no filter


# ============================================================ km.7 deterministic communities
CLUSTER_EDGES = [
    ("a1", "a2"), ("a2", "a3"), ("a1", "a3"),
    ("b1", "b2"), ("b2", "b3"), ("b1", "b3"),
    ("s", "a1"), ("s", "a2"), ("s", "b1"), ("s", "b2"),
]
KM_NOW = "2020-01-01T00:00:00+00:00"


def _adjacency(edges):
    adj: dict[str, set] = {}
    for u, v in edges:
        adj.setdefault(u, set()).add(v)
        adj.setdefault(v, set()).add(u)
    return adj


def _seed_km_graph(db, edges):
    c = db.conn
    c.execute("INSERT INTO docs(doc_id,path,kind,content_sha256,domain,tier) "
              "VALUES('d','d','raw','sha0','general',1)")
    c.execute("INSERT INTO blocks(block_id,block_content_id,doc_id,ordinal,text,valid_from,recorded_at) "
              "VALUES('d#0','bc0','d',0,'t',?,?)", (KM_NOW, KM_NOW))
    nodes = {n for e in edges for n in e}
    for n in sorted(nodes):
        c.execute("INSERT INTO nodes(node_id,display_name,valid_from,recorded_at,status) "
                  "VALUES(?,?,?,?, 'active')", (n, n, KM_NOW, KM_NOW))
    for i, (u, v) in enumerate(edges):
        c.execute("INSERT INTO edges(edge_id,subj_node,predicate,obj_node,obj_datatype,"
                  "source_block_id,source_quote,extractor,recorded_at,valid_from,status) "
                  "VALUES(?,?, 'related_to',?, 'node','d#0','q','deterministic',?,?, 'active')",
                  (f"e{i}", u, v, KM_NOW, KM_NOW))
    db.conn.commit()


class TestLabelPropagation(unittest.TestCase):
    def test_partition_and_community_id_is_min_node(self):
        ids = sorted({n for e in CLUSTER_EDGES for n in e})
        comm = label_propagation(ids, _adjacency(CLUSTER_EDGES))
        self.assertEqual(comm["a1"], "a1")   # community id is the MIN member, not the max
        self.assertEqual(comm["a2"], "a1")
        self.assertEqual(comm["a3"], "a1")
        self.assertEqual(comm["b1"], "b1")
        self.assertEqual(comm["b2"], "b1")
        self.assertEqual(comm["s"], "a1")    # 2-2 straddler tie-breaks to the smaller label


class TestCommunitiesDB(unittest.TestCase):
    def setUp(self):
        self.cfg = _fresh("tewiki_m211_km7_")
        self.addCleanup(shutil.rmtree, str(self.cfg.vault_dir), ignore_errors=True)
        self.db = DB(self.cfg)
        self.db.pour()
        self.addCleanup(self.db.close)

    def test_straddler_has_list_valued_membership(self):
        _seed_km_graph(self.db, CLUSTER_EDGES)
        members = recompute_communities(self.db)
        self.assertEqual(members["s"], ["a1", "b1"])   # a true straddler owns two communities
        self.assertEqual(members["a3"], ["a1"])        # a non-straddler stays single
        rows = self.db.conn.execute(
            "SELECT label FROM communities WHERE node_id='s' ORDER BY label").fetchall()
        self.assertEqual([r["label"] for r in rows], ["a1", "b1"])

    def test_partition_hash_is_deterministic_and_reflects_the_graph(self):
        _seed_km_graph(self.db, CLUSTER_EDGES)
        recompute_communities(self.db)
        h1 = community_hash(self.db)
        recompute_communities(self.db)
        h2 = community_hash(self.db)
        self.assertEqual(h1, h2)                        # byte-identical across two runs
        self.db.conn.execute(
            "INSERT INTO nodes(node_id,display_name,valid_from,recorded_at,status) "
            "VALUES('z9','z9',?,?, 'active')", (KM_NOW, KM_NOW))
        self.db.conn.execute(
            "INSERT INTO edges(edge_id,subj_node,predicate,obj_node,obj_datatype,"
            "source_block_id,source_quote,extractor,recorded_at,valid_from,status) "
            "VALUES('ez','a3','related_to','z9','node','d#0','q','deterministic',?,?, 'active')",
            (KM_NOW, KM_NOW))
        self.db.conn.commit()
        recompute_communities(self.db)
        h3 = community_hash(self.db)
        self.assertNotEqual(h1, h3)                     # a different graph -> a different hash

    def test_algo_version_pinned_in_meta(self):
        _seed_km_graph(self.db, CLUSTER_EDGES)
        recompute_communities(self.db)
        v = self.db.get_meta("community_algo_version")
        self.assertIsNotNone(v)
        self.assertIn("lpa-min-label-v1", v)


if __name__ == "__main__":
    unittest.main()
