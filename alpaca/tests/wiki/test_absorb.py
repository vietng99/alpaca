"""M2.14 vendored write path: absorb orchestrates segment/extract/enrich/resolve/reconcile/encode.

Ported from the upstream Rune-2 suite:
  - tests/test_pkt1_ingest.py     (shrink gate, block-changed skip, echo -> one witness, rel-time);
  - tests/test_resolve_two_dats.py (ADR-029 set-returning cannot-merge, surfaced never auto-bound);
  - tests/test_reconcile_timeline.py (single-cardinality valid-time windows);
  - tests/test_e2e_smoke.py        (the absorb_vault portion: the sample raw layer lands blocks).

Two Alpaca adaptations, both faithful to the plan:
  * providers.registry is the M2.16 sibling (not vendored yet); a deterministic HashEmbedder
    stand-in lets absorb build. It is installed with sys.modules.setdefault, so the real module
    wins once it lands.
  * the M2.14 write path carries NO vector side-write and NO on-ingest re-tier (both are members of
    the ranking surface the read-door guard fences off, and vectors are off by default per M2.11);
    the graph, edges, echo-provenance, shelf-life and valid-time behaviour asserted below are
    byte-faithful to upstream. The upstream answer/render/rebuild half of test_e2e_smoke rides on
    engine/project/cli tasks, not this one, so only its absorb_vault property is ported here.
"""
import hashlib
import math
import re
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

# ---- providers.registry stand-in (M2.16 sibling) --------------------------------------------
_TOK = re.compile(r"\w+", re.UNICODE)


class _HashEmbedder:
    def __init__(self, dim=64):
        self.dim = dim
        self.name = "deterministic-hash-%d" % dim

    def embed(self, text):
        v = [0.0] * self.dim
        for tok in (t for t in _TOK.findall(text.lower()) if len(t) > 1):
            h = hashlib.sha256(tok.encode("utf-8")).digest()
            v[int.from_bytes(h[:4], "big") % self.dim] += 1.0 if h[4] & 1 else -1.0
        n = math.sqrt(sum(x * x for x in v))
        return [x / n for x in v] if n > 0 else v


class _NullLLM:
    name = "off"

    def extract(self, block_text, context):
        return []


class _Providers:
    def __init__(self, cfg):
        self.embedder = _HashEmbedder(int(cfg.meta.get("embed_dim", 64)))
        self.reranker = None
        self.entailer = None
        self.llm_extractor = _NullLLM()


_pp = types.ModuleType("alpaca.wiki.providers")
_pp.__path__ = []
_pr = types.ModuleType("alpaca.wiki.providers.registry")
_pr.Providers = _Providers
sys.modules.setdefault("alpaca.wiki.providers", _pp)
sys.modules.setdefault("alpaca.wiki.providers.registry", _pr)

from alpaca.wiki.clock import FixedClock          # noqa: E402
from alpaca.wiki.config import Config             # noqa: E402
from alpaca.wiki.engine import echo               # noqa: E402
from alpaca.wiki.ingest.absorb import Absorber, absorb_vault   # noqa: E402
from alpaca.wiki.ingest.reconcile import classify  # noqa: E402
from alpaca.wiki.ingest.shrink import BodyShrinkRejected  # noqa: E402
from alpaca.wiki.store.db import DB                # noqa: E402


def fresh_cfg():
    return Config.for_vault(Path(tempfile.mkdtemp(prefix="tewiki_absorb_")))


def cleanup(cfg):
    shutil.rmtree(cfg.vault_dir, ignore_errors=True)


def absorber(cfg, start="2020-01-01T00:00:00+00:00", step=0):
    return Absorber(cfg, clock=FixedClock(start=start, step=step))


FOUR_EDGE_DOC = (
    "[[Alice]] works at [[Acme]].\n"
    "[[Bob]] founded [[Beta]].\n"
    "[[Carol]] attended [[Uni]].\n"
    "[[Paper A]] cites [[Paper B]].\n"
)


# ------------------------------------------------------- (pkt1) shrink gate wired into re-ingest
class TestShrinkGateWiredIntoReingest(unittest.TestCase):
    def test_shrinking_reingest_is_blocked_before_write(self):
        cfg = fresh_cfg()
        a = absorber(cfg)
        try:
            a.absorb_text("d.md", FOUR_EDGE_DOC)
            with self.assertRaises(BodyShrinkRejected) as ctx:
                a.absorb_text("d.md", "[[Alice]] works at [[Acme]].")
            self.assertEqual(ctx.exception.n_before, 4)
            self.assertEqual(ctx.exception.n_after, 1)
            self.assertEqual(ctx.exception.claims_dropped, 3)
            still = a.db.conn.execute(
                "SELECT COUNT(*) c FROM edges e JOIN blocks b ON e.source_block_id=b.block_id "
                "WHERE b.doc_id='d.md' AND e.status='active' AND e.superseded_at IS NULL"
            ).fetchone()["c"]
            self.assertEqual(still, 4)
        finally:
            a.close(); cleanup(cfg)

    def test_full_body_reingest_passes(self):
        cfg = fresh_cfg()
        a = absorber(cfg)
        try:
            a.absorb_text("d.md", FOUR_EDGE_DOC)
            out = a.absorb_text("d.md", FOUR_EDGE_DOC.replace("[[Uni]]", "[[Uni2]]"))
            self.assertNotIn("skipped", out)
        finally:
            a.close(); cleanup(cfg)


# ------------------------------------------------------- (pkt1) hashgate block-changed wired in
class TestBlockChangedWiredIntoAbsorb(unittest.TestCase):
    def test_unchanged_block_is_skipped_on_partial_reingest(self):
        cfg = fresh_cfg()
        a = absorber(cfg)
        try:
            s1 = a.absorb_text("d.md", "Para A alpha.\n\nPara B beta.")
            self.assertEqual(s1["blocks"], 2)
            s2 = a.absorb_text("d.md", "Para A alpha.\n\nPara B beta.\n\nPara C gamma.")
            self.assertEqual(s2["blocks"], 1)
        finally:
            a.close(); cleanup(cfg)


# ------------------------------------------------------- (pkt1) echo collapses to one witness
class TestEchoProvenanceWiredIntoAbsorb(unittest.TestCase):
    def test_real_echoes_collapse_to_one_witness(self):
        cfg = fresh_cfg()
        a = absorber(cfg)
        try:
            a.absorb_text("p.md", "[[Smith]] works at [[Acme]].")
            a.absorb_text("e1.md", "[[Smith]] works at [[Acme]].")
            a.absorb_text("e2.md", "[[Smith]] works at [[Acme]].")
            edges = a.db.conn.execute(
                "SELECT edge_id, corroboration_count FROM edges "
                "WHERE subj_node='smith' AND predicate='works_at'").fetchall()
            self.assertEqual(len(edges), 1)
            eid = edges[0]["edge_id"]
            self.assertEqual(edges[0]["corroboration_count"], 3)
            self.assertEqual(echo.independent_witness_count(a.db, eid), 1)
        finally:
            a.close(); cleanup(cfg)


# ------------------------------------------------------- (pkt1) relative time anchored to source
class TestRelativeTimeWiredIntoEncode(unittest.TestCase):
    def test_relative_claim_learned_at_anchored_to_source_date(self):
        cfg = fresh_cfg()
        a = absorber(cfg)
        try:
            a.absorb_text("rel.md", "[[Smith]] works at [[Acme]] recently.",
                          source_created_at="2020-06-15")
            row = a.db.conn.execute(
                "SELECT learned_at, recorded_at FROM edges "
                "WHERE subj_node='smith' AND predicate='works_at'").fetchone()
            self.assertEqual(row["learned_at"][:10], "2020-06-15")
            self.assertNotEqual(row["learned_at"], row["recorded_at"])
        finally:
            a.close(); cleanup(cfg)


# ------------------------------------------------------- (two-dats) set-returning cannot-merge
class TestTwoDats(unittest.TestCase):
    def test_distinct_targets_stay_distinct(self):
        cfg = fresh_cfg()
        try:
            a = absorber(cfg)
            a.absorb_text("d.md",
                          "[[Dat Nguyen]] works at [[Acme]].\n[[Dat Tran]] works at [[Beta]].")
            db = DB(cfg)
            ns = {r["node_id"] for r in db.conn.execute("SELECT node_id FROM nodes")}
            db.close(); a.close()
            self.assertIn("dat-nguyen", ns)
            self.assertIn("dat-tran", ns)
        finally:
            cleanup(cfg)

    def test_same_display_distinct_slug_surfaces_candidate_never_binds(self):
        cfg = fresh_cfg()
        try:
            a = absorber(cfg)
            a.absorb_text("d.md",
                          "[[dat-1|Dat]] works at [[Acme]].\n[[dat-2|Dat]] works at [[Beta]].")
            db = DB(cfg)
            cand = db.conn.execute("SELECT node_a, node_b, status FROM merge_candidate").fetchall()
            n1 = db.conn.execute(
                "SELECT canonical_node_id, status FROM nodes WHERE node_id='dat-1'").fetchone()
            n2 = db.conn.execute(
                "SELECT canonical_node_id, status FROM nodes WHERE node_id='dat-2'").fetchone()
            db.close(); a.close()
            self.assertTrue(any(c["status"] == "pending" for c in cand),
                            "a near-match must be surfaced as a pending candidate")
            self.assertIsNone(n1["canonical_node_id"])
            self.assertIsNone(n2["canonical_node_id"])
        finally:
            cleanup(cfg)


# ------------------------------------------------------- (reconcile) single-cardinality timeline
class TestSingleCardinalityTimeline(unittest.TestCase):
    def test_third_world_transition_closes_immediate_prior(self):
        cfg = fresh_cfg()
        a = absorber(cfg)
        try:
            a.absorb_text("a.md", "[[Worker]] works at [[Alpha]] @2020-01-01.")
            a.absorb_text("b.md", "[[Worker]] works at [[Beta]] @2024-01-01.")
            a.absorb_text("c.md", "[[Worker]] works at [[Gamma]] @2026-01-01.")
            rows = a.db.conn.execute(
                "SELECT obj_node, valid_from, valid_until FROM edges "
                "WHERE subj_node='worker' AND predicate='works_at' ORDER BY valid_from"
            ).fetchall()
            self.assertEqual(
                [(r["obj_node"], r["valid_from"][:10],
                  r["valid_until"][:10] if r["valid_until"] else None) for r in rows],
                [("alpha", "2020-01-01", "2024-01-01"),
                 ("beta", "2024-01-01", "2026-01-01"),
                 ("gamma", "2026-01-01", None)],
            )
        finally:
            a.close(); cleanup(cfg)

    def test_middle_backfill_splits_existing_historical_window(self):
        cfg = fresh_cfg()
        a = absorber(cfg)
        try:
            a.absorb_text("a.md", "[[Worker]] works at [[Alpha]] @2020-01-01.")
            a.absorb_text("b.md", "[[Worker]] works at [[Beta]] @2024-01-01.")
            a.absorb_text("c.md", "[[Worker]] works at [[Gamma]] @2022-01-01.")
            rows = a.db.conn.execute(
                "SELECT obj_node,valid_from,valid_until FROM edges "
                "WHERE subj_node='worker' AND predicate='works_at' ORDER BY valid_from"
            ).fetchall()
            self.assertEqual(
                [(r["obj_node"], r["valid_from"][:10],
                  r["valid_until"][:10] if r["valid_until"] else None) for r in rows],
                [("alpha", "2020-01-01", "2022-01-01"),
                 ("gamma", "2022-01-01", "2024-01-01"),
                 ("beta", "2024-01-01", None)],
            )
        finally:
            a.close(); cleanup(cfg)

    def test_classify_novel_on_empty_key(self):
        # a candidate with no prior on its supersede key is NOVEL (negative control for the pairing).
        cfg = fresh_cfg()
        a = absorber(cfg)
        try:
            with a.writer.transaction():
                a.writer.upsert_doc("d.md", "d.md", "raw", "sha", private=0)
                a.writer.upsert_node("worker")
                a.writer.upsert_block({"block_id": "d.md#0", "block_content_id": "c0",
                                       "occurrence_index": 0, "doc_id": "d.md", "ordinal": 0,
                                       "text": "seed"})
            verdict = classify(a.db, {
                "subj_node": "worker", "predicate": "works_at", "obj_node": "alpha",
                "obj_literal": None, "obj_datatype": "node", "source_block_id": "d.md#0",
                "valid_from": "2025-01-01T00:00:00+00:00"}, "e_new")
            self.assertEqual(verdict.verdict, "novel")
        finally:
            a.close(); cleanup(cfg)


# ------------------------------------------------------- (e2e) the raw layer lands blocks
class TestAbsorbVault(unittest.TestCase):
    def test_absorb_vault_lands_blocks(self):
        cfg = fresh_cfg()
        try:
            raw = cfg.vault_dir / "raw"
            raw.mkdir(parents=True, exist_ok=True)
            (raw / "a.md").write_text("[[Ada Placeholder]] works at [[Acme]].\n", encoding="utf-8")
            (raw / "b.md").write_text("[[Bob Placeholder]] founded [[Beta]].\n", encoding="utf-8")
            results = absorb_vault(cfg, clock=FixedClock())
            self.assertTrue(results and any(r.get("blocks", 0) > 0 for r in results))
        finally:
            cleanup(cfg)

    def test_absorb_vault_reingest_is_idempotent(self):
        cfg = fresh_cfg()
        try:
            raw = cfg.vault_dir / "raw"
            raw.mkdir(parents=True, exist_ok=True)
            (raw / "a.md").write_text("[[Ada Placeholder]] works at [[Acme]].\n", encoding="utf-8")
            absorb_vault(cfg, clock=FixedClock())
            again = absorb_vault(cfg, clock=FixedClock())
            self.assertTrue(all(r.get("skipped") or r.get("retired") for r in again),
                            "a second absorb of unchanged bytes must skip every doc")
        finally:
            cleanup(cfg)


if __name__ == "__main__":
    unittest.main()
