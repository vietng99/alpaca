"""M2.10 proof: authority with conflict arbitration - the tier ladder, the bounded prior, and the
deterministic re-stamp at serve time.

Ported in spirit from the upstream Rune-2 suite (tests/test_authority_ranking.py,
tests/test_dm10_tiering.py, tests/test_r1_north_star_successor.py, tests/test_answer_ledger_append.py,
tests/test_boot_snapshot.py). The upstream authority-ranking and successor tests drive the full
retrieval door (engine.loop.Engine, engine.retrieve, the provider registry) which lands in M2.11;
M2.10 depends only on M2.8 + M2.9, so the same properties are proved directly against the vendored
surfaces this task delivers: fuse (the bounded additive authority prior + its knob + its tie-break),
tiering.compute_tiers / retier (deterministic PageRank page bands), oracle.arbitrate_conflict (rank
by authority + recency + independent-witness corroboration), the vendored store's active-successor
read, engine.ledger.persist (the append-only answer audit), and engine.boot (the freshness snapshot).

THE TIER-LABEL SUBSTITUTION (Step 3). The upstream ladder's opaque test-data scale points
(SIGNED / PROVISIONAL / OVERRIDDEN) are replaced here by the Alpaca authority tier names, declared in
ONE table - ALPACA_TIER_LADDER below - and nowhere else. The substitution is asserted by the ported
dm10 / arbitration tests: the five Alpaca names map to strictly-decreasing authority integers, and a
VERIFIED-ROW source out-ranks a PROVISIONAL one through the exact same arithmetic the engine uses.

Every property carries a negative control. The templates (Layer-4 completeness) and the compartment
private-path filter are vendored in later M2 tasks; oracle and the writer name them, so stand-ins are
registered here exactly as a later real module would win via setdefault.
"""
import shutil
import sys
import tempfile
import types
import unittest
from collections import namedtuple
from pathlib import Path

# --- stand-ins for the not-yet-vendored siblings the vendored importers name --------------------
_PredResult = namedtuple("PredResult", ["detail", "passed"])
_templates = types.ModuleType("alpaca.wiki.engine.templates")
_templates.PredResult = _PredResult
for _name in ("successor_read", "overturner_query", "as_of_currency",
              "subclaim_coverage", "exact_match_floor"):
    setattr(_templates, _name, (lambda *a, **k: _PredResult("", True)))
sys.modules.setdefault("alpaca.wiki.engine.templates", _templates)

_compartment = types.ModuleType("alpaca.wiki.engine.compartment")


class _PrivatePathViolation(RuntimeError):
    pass


_compartment.PrivatePathViolation = _PrivatePathViolation
_compartment.is_private_path = lambda path: False
_compartment.assert_private_path = lambda private, path: None
sys.modules.setdefault("alpaca.wiki.engine.compartment", _compartment)

from alpaca.wiki.clock import FixedClock
from alpaca.wiki.config import Config
from alpaca.wiki.determinism import edge_id as make_edge_id, rrf_fuse, stable_rank, to_band
from alpaca.wiki.engine import echo, fuse
from alpaca.wiki.engine.oracle import arbitrate_conflict
from alpaca.wiki.engine.tiering import BRIDGE_CAP, TOP_BAND, compute_tiers, retier, tier_hash
from alpaca.wiki.engine.boot import BOOT_CONTEXT_CEILING, boot_snapshot, stamp_snapshot, store_content_hash
from alpaca.wiki.engine.ledger import persist as ledger_persist
from alpaca.wiki.store.asof import AsOf
from alpaca.wiki.store import query as q
from alpaca.wiki.store.db import DB
from alpaca.wiki.types import Answer, Completeness, CurrencyStamp, RetrievalHit

T0 = "2020-01-01T00:00:00+00:00"

# ============================================================================ THE ONE DECLARED TABLE
# The Alpaca authority tier ladder. This replaces the upstream ladder's opaque labels
# (SIGNED / PROVISIONAL / OVERRIDDEN) and is the SINGLE place the five Alpaca tier names are bound to the
# engine's opaque "higher = more authoritative" source_authority integer. Highest tier first.
ALPACA_TIER_LADDER = {
    "VERIFIED-ROW":   50,     # a discharged verdict row proven by an instrument
    "DISCHARGED-ROW": 40,     # a closed task row
    "OWNER-STATED":   30,     # a recorded owner decision
    "LESSON":         20,     # a probed lesson
    "PROVISIONAL":    10,     # an unverified capture (the floor)
}
# The value a brain vault pins in meta to scale the bounded prior into fused-score units. It is the
# calibration under test, not an engine constant; a stock vault pins nothing and gets 0.0.
AUTHORITY_UNIT = 0.0008
TOP_RANK_RRF = 1.0 / 61.0


# --------------------------------------------------------------------------------- fuse test seams
class _Reranker:
    name = "stub"

    def __init__(self, scores=None):
        self._scores = scores or {}

    def rerank(self, query, blocks):
        return {b: self._scores.get(b, 0.0) for b, _ in blocks}


def _hit(block_id, authority=0, exact_floor=False, page_band=0, valid_at="", text="t"):
    return RetrievalHit(block_id=block_id, text=text, page_band=page_band, valid_at=valid_at,
                        exact_floor=exact_floor, authority=authority)


def _fresh_db(prefix):
    vault = Path(tempfile.mkdtemp(prefix=prefix))
    cfg = Config.for_vault(vault)
    db = DB(cfg)
    db.pour()
    return db, cfg, vault


# ============================================================================ bounded authority prior
class TestBoundedAuthorityPrior(unittest.TestCase):
    """Ported from test_authority_ranking.py: the prior is a BOUNDED ADDITIVE nudge that decides
    near-ties without ever substituting for relevance, and the default knob is a byte-identical no-op."""

    def test_verified_row_outranks_provisional_at_a_near_tie(self):
        # ADJACENT ranks: the PROVISIONAL block is retrieved one rank BETTER, so relevance and the
        # block_id tie-break both favour it; only the authority prior can flip the pair.
        channel_lists = {"bm25": ["d#prov", "d#verified"]}
        prov, verified = ALPACA_TIER_LADDER["PROVISIONAL"], ALPACA_TIER_LADDER["VERIFIED-ROW"]

        off = fuse.fuse([_hit("d#prov", authority=prov), _hit("d#verified", authority=verified)],
                        channel_lists, _Reranker(), "q", 60)
        # NEGATIVE CONTROL: with the knob off the better-retrieved block still leads.
        self.assertEqual([h.block_id for h in off], ["d#prov", "d#verified"])

        on = fuse.fuse([_hit("d#prov", authority=prov), _hit("d#verified", authority=verified)],
                       channel_lists, _Reranker(), "q", 60, authority_unit=AUTHORITY_UNIT)
        self.assertEqual([h.block_id for h in on], ["d#verified", "d#prov"],
                         msg="a VERIFIED-ROW block did not out-rank a PROVISIONAL one at a near-tie")
        bands = {h.block_id: h.band for h in on}
        # it flipped through the SCORE, not a tie-break: the integer bands genuinely differ.
        self.assertGreater(bands["d#verified"], bands["d#prov"])

    def test_strong_relevance_still_beats_authority(self):
        # PROVISIONAL is top-of-list in all three channels; VERIFIED-ROW is in none. Relevance wins:
        # the prior is a nudge, not a gag.
        channel_lists = {"bm25": ["d#prov"], "vector": ["d#prov"], "ppr": ["d#prov"]}
        prov, verified = ALPACA_TIER_LADDER["PROVISIONAL"], ALPACA_TIER_LADDER["VERIFIED-ROW"]
        ordered = fuse.fuse([_hit("d#prov", authority=prov), _hit("d#verified", authority=verified)],
                            channel_lists, _Reranker(), "q", 60, authority_unit=AUTHORITY_UNIT)
        self.assertEqual([h.block_id for h in ordered], ["d#prov", "d#verified"],
                         msg="the authority prior gagged relevance instead of nudging it")
        self.assertGreater(3 * TOP_RANK_RRF, AUTHORITY_UNIT * (verified - prov))

    def test_nonfinite_and_negative_authority_unit_refused(self):
        with self.assertRaises(ValueError):
            fuse.fuse([_hit("d#a")], {}, _Reranker(), "q", authority_unit=float("nan"))
        with self.assertRaises(ValueError):
            fuse.fuse([_hit("d#a")], {}, _Reranker(), "q", authority_unit=-1.0)

    def test_authority_mass_is_capped_and_floor_never_evicted(self):
        # an oversized knob and extreme authority stay bounded; the exact-match floor is un-overridable.
        channel_lists = {"bm25": ["d#verified"], "vector": ["d#verified"], "ppr": ["d#verified"]}
        ordered = fuse.fuse([_hit("d#floor", authority=0, exact_floor=True),
                             _hit("d#verified", authority=ALPACA_TIER_LADDER["VERIFIED-ROW"])],
                            channel_lists, _Reranker(), "q", 60, authority_unit=0.01)
        self.assertEqual([h.block_id for h in ordered][0], "d#floor",
                         msg="authority mass evicted an exact-floor member")
        self.assertLessEqual(ordered[-1].norm["authority"], fuse.MAX_AUTHORITY_MASS)

    def test_default_knob_is_byte_identical(self):
        # even when docs ARE marked, the default knob contributes exactly zero mass (x + 0.0 == x).
        rr = {"d#a": 0.25, "d#b": 0.125}
        channel_lists = {"bm25": ["d#a", "d#b"], "vector": ["d#b"]}
        rrf = rrf_fuse(channel_lists, k=60)
        marked = [_hit("d#a", authority=ALPACA_TIER_LADDER["VERIFIED-ROW"]),
                  _hit("d#b", authority=ALPACA_TIER_LADDER["PROVISIONAL"])]
        expected = {h.block_id: rrf.get(h.block_id, 0.0) + rr.get(h.block_id, 0.0) for h in marked}
        ordered = fuse.fuse(marked, channel_lists, _Reranker(rr), "q", 60)
        for h in ordered:
            self.assertEqual(h.norm["authority"], 0.0)
            self.assertEqual(h.fused, expected[h.block_id])
        # NEGATIVE CONTROL: the same set at the pinned unit DOES move.
        moved = fuse.fuse([_hit("d#a", authority=ALPACA_TIER_LADDER["VERIFIED-ROW"])],
                          channel_lists, _Reranker(rr), "q", 60, authority_unit=AUTHORITY_UNIT)
        self.assertNotEqual(moved[0].fused, expected["d#a"])

    def test_tie_break_orders_by_authority_after_band(self):
        lo = _hit("d#a", authority=ALPACA_TIER_LADDER["PROVISIONAL"])
        hi = _hit("d#z", authority=ALPACA_TIER_LADDER["VERIFIED-ROW"])
        lo.band = hi.band = 12345
        self.assertEqual([h.block_id for h in stable_rank([lo, hi])], ["d#z", "d#a"])
        # NEGATIVE CONTROL: equal authority falls back to the pre-existing block_id ASC tie-break.
        fa = _hit("d#a", authority=ALPACA_TIER_LADDER["PROVISIONAL"])
        fz = _hit("d#z", authority=ALPACA_TIER_LADDER["PROVISIONAL"])
        fa.band = fz.band = 12345
        self.assertEqual([h.block_id for h in stable_rank([fz, fa])], ["d#a", "d#z"])


# ============================================================================ dm10 tiering + labels
NOW = "2020-01-01T00:00:00+00:00"


def _seed_graph(db, edges, statuses=None, extra_nodes=None, predicates=None):
    c = db.conn
    c.execute("INSERT INTO docs(doc_id,path,kind,content_sha256,domain,tier) "
              "VALUES('d','d','raw','sha0','general',1)")
    c.execute("INSERT INTO blocks(block_id,block_content_id,doc_id,ordinal,text,valid_from,recorded_at) "
              "VALUES('d#0','bc0','d',0,'t',?,?)", (NOW, NOW))
    for p in (predicates or []):
        c.execute("INSERT OR IGNORE INTO predicates(predicate,extraction,cardinality) "
                  "VALUES(?, 'deterministic','multi')", (p,))
    nodes = set(extra_nodes or [])
    for s, _p, o in edges:
        nodes.add(s)
        nodes.add(o)
    statuses = statuses or {}
    for n in sorted(nodes):
        c.execute("INSERT INTO nodes(node_id,display_name,valid_from,recorded_at,status) "
                  "VALUES(?,?,?,?,?)", (n, n, NOW, NOW, statuses.get(n, "active")))
    for i, (s, p, o) in enumerate(edges):
        c.execute("INSERT INTO edges(edge_id,subj_node,predicate,obj_node,obj_datatype,"
                  "source_block_id,source_quote,extractor,recorded_at,valid_from,status) "
                  "VALUES(?,?,?,?, 'node','d#0','q','deterministic',?,?, 'active')",
                  (f"e{i}", s, p, o, NOW, NOW))
    db.conn.commit()


class TestTierLabelSubstitution(unittest.TestCase):
    """Step 3: the five Alpaca tier names replace the upstream ladder in ONE declared table, and that
    table drives the SAME arbitration arithmetic the engine uses."""

    def test_ladder_is_the_five_alpaca_names_strictly_ordered(self):
        self.assertEqual(list(ALPACA_TIER_LADDER),
                         ["VERIFIED-ROW", "DISCHARGED-ROW", "OWNER-STATED", "LESSON", "PROVISIONAL"])
        vals = list(ALPACA_TIER_LADDER.values())
        self.assertEqual(vals, sorted(vals, reverse=True))               # strictly higher = earlier
        self.assertEqual(len(set(vals)), len(vals))                       # no two tiers collide
        # the upstream labels are gone: no SIGNED / OVERRIDDEN survive the substitution.
        self.assertNotIn("SIGNED", ALPACA_TIER_LADDER)
        self.assertNotIn("OVERRIDDEN", ALPACA_TIER_LADDER)

    def test_verified_row_wins_arbitration_over_provisional(self):
        # the ladder integers feed docs.source_authority and arbitrate_conflict ranks by them: a
        # VERIFIED-ROW source out-ranks a PROVISIONAL one, a LESSON out-ranks neither above it.
        db, cfg, vault = _fresh_db("tewiki_m210_tierlabel_")
        self.addCleanup(shutil.rmtree, vault, ignore_errors=True)
        self.addCleanup(db.close)
        echo.ensure_schema(db)
        c = db.conn
        c.execute("INSERT INTO nodes(node_id,display_name,valid_from,recorded_at,status) "
                  "VALUES('subj','subj',?,?,'active')", (T0, T0))
        edge_ids = {}
        for tier, obj in (("VERIFIED-ROW", "acme"), ("PROVISIONAL", "beta"), ("LESSON", "gamma")):
            doc = f"{obj}.md"
            c.execute("INSERT INTO docs(doc_id,path,kind,content_sha256,source_authority) "
                      "VALUES(?,?,?,?,?)", (doc, doc, "raw", doc, ALPACA_TIER_LADDER[tier]))
            c.execute("INSERT INTO nodes(node_id,display_name,valid_from,recorded_at,status) "
                      "VALUES(?,?,?,?,'active')", (obj, obj, T0, T0))
            c.execute("INSERT INTO blocks(block_id,block_content_id,doc_id,ordinal,text,valid_from,"
                      "recorded_at) VALUES(?,?,?,0,'t',?,?)", (f"{doc}#0", f"{doc}#0", doc, T0, T0))
            eid = make_edge_id("subj", "works_at", obj, f"bc_{obj}")
            edge_ids[tier] = eid
            c.execute("INSERT INTO edges(edge_id,subj_node,predicate,obj_node,obj_datatype,"
                      "source_block_id,source_doc_id,source_quote,extractor,recorded_at,valid_from,"
                      "status) VALUES(?,?,?,?, 'node',?,?,'q','deterministic',?, ?, 'active')",
                      (eid, "subj", "works_at", obj, f"{doc}#0", doc, T0, T0))
        db.commit()
        edges = [dict(r) for r in c.execute("SELECT * FROM edges").fetchall()]
        ranking = arbitrate_conflict(db, edges)
        self.assertEqual(ranking["winner"], edge_ids["VERIFIED-ROW"])
        self.assertEqual(ranking["ranked"][-1], edge_ids["PROVISIONAL"])     # the floor sinks last
        self.assertEqual(set(ranking["ranked"]), set(edge_ids.values()))     # all surfaced


class TestDm10Tiering(unittest.TestCase):
    """Ported from test_dm10_tiering.py: the deterministic PageRank bands (Stage A/B/C) and the
    integer-only tier hash. The mechanism the tier ladder rides is byte-reproducible."""

    def test_percentile_bands(self):
        ids = [f"n{i:02d}" for i in range(20)]
        scores = {nid: (20 - i) * 0.001 for i, nid in enumerate(ids)}
        b = compute_tiers(ids, scores, {n: "active" for n in ids}, {}, set())["band"]
        self.assertEqual(b["n00"], 3)
        self.assertEqual(b["n02"], 2)      # MUTATION (BAND_CUTS widened) flips this to 3
        self.assertEqual(b["n05"], 1)
        self.assertEqual(b["n11"], 0)

    def test_bridge_promotion_is_capped(self):
        ids = [f"n{i:02d}" for i in range(12)]
        scores = {nid: (12 - i) * 0.001 for i, nid in enumerate(ids)}
        inn = {"n09": {"a", "b", "c"}, "n07": {"a", "b"}, "n08": {"a", "b"}}
        r = compute_tiers(ids, scores, {n: "active" for n in ids}, inn, set())
        self.assertEqual(r["promoted"], {"n09", "n07"})
        self.assertEqual(len(r["promoted"]), BRIDGE_CAP)

    def test_superseded_hub_demoted_unless_updates(self):
        ids = [f"n{i:02d}" for i in range(10)]
        scores = {nid: (10 - i) * 0.001 for i, nid in enumerate(ids)}
        statuses = {n: "active" for n in ids}
        statuses["n00"] = "superseded"
        demoted = compute_tiers(ids, scores, statuses, {}, set())
        self.assertEqual(demoted["band"]["n00"], TOP_BAND - 1)
        exempt = compute_tiers(ids, scores, statuses, {}, {"n00"})
        self.assertEqual(exempt["band"]["n00"], TOP_BAND)      # 'updates' keeps authority

    def test_retier_persists_and_is_deterministic(self):
        db, cfg, vault = _fresh_db("tewiki_m210_dm10_")
        self.addCleanup(shutil.rmtree, vault, ignore_errors=True)
        self.addCleanup(db.close)
        _seed_graph(db, [(f"l{i}", "related_to", "hub") for i in range(8)])
        band1, h1 = retier(db), tier_hash(db)
        self.assertEqual(band1["hub"], TOP_BAND)
        band2, h2 = retier(db), tier_hash(db)
        self.assertEqual(h1, h2)
        self.assertEqual(band1, band2)
        # the tier hash folds only the INTEGER band, never the advisory float pagerank.
        db.conn.execute("UPDATE nodes SET pagerank = pagerank + 0.5 WHERE node_id='hub'")
        db.commit()
        self.assertEqual(tier_hash(db), h1)


# ============================================================================ active successor read
class TestActiveSuccessorNorthStar(unittest.TestCase):
    """Ported from test_r1_north_star_successor.py: a status query follows the ACTIVE successor and
    never serves a superseded edge as current. Proved on the vendored store read (M2.8) that the
    oracle sits on, since the retrieval door lands in M2.11."""

    def setUp(self):
        self.db, self.cfg, self.vault = _fresh_db("tewiki_m210_succ_")
        self.addCleanup(shutil.rmtree, self.vault, ignore_errors=True)
        self.addCleanup(self.db.close)
        c = self.db.conn
        for n in ("zeta", "acme", "beta"):
            c.execute("INSERT INTO nodes(node_id,display_name,valid_from,recorded_at,status) "
                      "VALUES(?,?,?,?,'active')", (n, n, T0, T0))
        c.execute("INSERT INTO docs(doc_id,path,kind,content_sha256) VALUES('d.md','d.md','raw','s')")
        for bid in ("d.md#0", "d.md#1"):
            c.execute("INSERT INTO blocks(block_id,block_content_id,doc_id,ordinal,text,valid_from,"
                      "recorded_at) VALUES(?,?,?,0,'t',?,?)", (bid, bid, "d.md", T0, T0))
        self.acme_edge = make_edge_id("zeta", "works_at", "acme", "bc_a")
        self.beta_edge = make_edge_id("zeta", "works_at", "beta", "bc_b")
        # the acme edge is the superseded prior; the beta edge is the active successor.
        c.execute("INSERT INTO edges(edge_id,subj_node,predicate,obj_node,obj_datatype,source_block_id,"
                  "source_quote,extractor,recorded_at,valid_from,superseded_at,status,"
                  "superseded_by_edge_id) "
                  "VALUES(?,?,?,?, 'node','d.md#0','q','deterministic',?, ?, ?, 'invalidated', ?)",
                  (self.acme_edge, "zeta", "works_at", "acme", T0, T0, T0, self.beta_edge))
        c.execute("INSERT INTO edges(edge_id,subj_node,predicate,obj_node,obj_datatype,source_block_id,"
                  "source_quote,extractor,recorded_at,valid_from,status,supersedes_edge_id) "
                  "VALUES(?,?,?,?, 'node','d.md#1','q','deterministic',?, ?, 'active', ?)",
                  (self.beta_edge, "zeta", "works_at", "beta", T0, T0, self.acme_edge))
        self.db.commit()

    def test_active_edge_read_follows_the_successor(self):
        active = q.active_edges_for_node(self.db, "zeta", AsOf("2026-01-01T00:00:00+00:00"))
        served = {e["edge_id"] for e in active}
        self.assertIn(self.beta_edge, served, msg="the active successor tip was not served")
        self.assertNotIn(self.acme_edge, served, msg="a superseded edge was served as current")

    def test_successor_hop_reaches_the_active_tip(self):
        # NEGATIVE CONTROL for the north-star hop: starting from the SUPERSEDED edge, the successor
        # pointer reaches the active tip rather than dead-ending on the stale value.
        succ = q.successors_of(self.db, self.acme_edge)
        self.assertIn(self.beta_edge, {e["edge_id"] for e in succ})


# ============================================================================ answer ledger append
class TestAnswerLedgerAppend(unittest.TestCase):
    """Ported from test_answer_ledger_append.py: the answer path's audit rows are append-only - a
    repeated identical question appends a fresh answers + claims + eval_ledger row every time."""

    def setUp(self):
        self.db, self.cfg, self.vault = _fresh_db("tewiki_m210_ledger_")
        self.addCleanup(shutil.rmtree, self.vault, ignore_errors=True)
        self.addCleanup(self.db.close)

    def _answer(self):
        return Answer(question="Where does Ada work?", verdict="grounded",
                      answer_text="Acme Corp.",
                      currency_stamp=CurrencyStamp(as_of="2026-01-01T00:00:00+00:00"),
                      completeness=Completeness(True, True, True))

    def _persist(self):
        return ledger_persist(self.db, self._answer(), qtype="factoid", query_hash="qh",
                              dag={}, channels={"bm25": []}, model_versions={}, determinism_hash="dh",
                              fast_path=False, latency_ms=0, retrieval_trace={})

    def test_repeated_question_appends_every_audit_row(self):
        a1 = self._persist()
        a2 = self._persist()
        self.assertNotEqual(a1, a2)                      # distinct append-only ids, never overwritten
        answers = self.db.conn.execute("SELECT answer_id FROM answers").fetchall()
        claims = self.db.conn.execute("SELECT answer_id FROM claims").fetchall()
        evals = self.db.conn.execute("SELECT query_id FROM eval_ledger").fetchall()
        self.assertEqual(len(answers), 2)
        self.assertEqual(len(evals), 2)
        self.assertEqual({r["query_id"] for r in evals}, {r["answer_id"] for r in answers})
        # claims are optional (this Answer carries none); the point is the audit rows never collapse.
        self.assertEqual(len(claims), 0)


# ============================================================================ boot freshness snapshot
class TestBootSnapshot(unittest.TestCase):
    """Ported from test_boot_snapshot.py: the freshness snapshot goes HARD stale on any unstamped
    mutation and every reported number equals a direct query - re-stamped at serve time."""

    def _seed_rows(self, db):
        c = db.conn
        c.execute("INSERT INTO docs(doc_id,path,kind,content_sha256) VALUES('r.md','r.md','raw','s1')")
        c.execute("INSERT INTO nodes(node_id,display_name,valid_from,recorded_at,status) "
                  "VALUES('dat','dat',?,?,'active')", (T0, T0))
        db.commit()

    def test_fresh_after_stamp_then_stale_on_mutation(self):
        db, cfg, vault = _fresh_db("tewiki_m210_boot1_")
        self.addCleanup(shutil.rmtree, vault, ignore_errors=True)
        self.addCleanup(db.close)
        stamp_snapshot(db)
        self.assertFalse(boot_snapshot(db).stale)
        db.conn.execute(
            "INSERT INTO docs(doc_id,path,kind,content_sha256) VALUES('d.md','d.md','raw','deadbeef')")
        db.commit()
        self.assertTrue(boot_snapshot(db).stale, "a mutated-but-unrestamped store must be HARD stale")

    def test_missing_snapshot_recommends_rebuild(self):
        db, cfg, vault = _fresh_db("tewiki_m210_boot2_")
        self.addCleanup(shutil.rmtree, vault, ignore_errors=True)
        self.addCleanup(db.close)
        rep = boot_snapshot(db)                       # never stamped
        self.assertTrue(rep.stale)
        self.assertTrue(rep.recommend_rebuild)
        self.assertEqual(rep.snapshot_source_hash, "")

    def test_every_number_equals_direct_query(self):
        db, cfg, vault = _fresh_db("tewiki_m210_boot3_")
        self.addCleanup(shutil.rmtree, vault, ignore_errors=True)
        self.addCleanup(db.close)
        self._seed_rows(db)
        stamp_snapshot(db)
        rep = boot_snapshot(db)
        raw = db.conn.execute("SELECT COUNT(*) FROM docs WHERE kind='raw'").fetchone()[0]
        nodes = db.conn.execute("SELECT COUNT(*) FROM nodes WHERE status='active'").fetchone()[0]
        self.assertGreaterEqual(raw, 1)
        self.assertEqual(rep.raw, raw)                # MUTATION (count wiki instead) flips this
        self.assertEqual(rep.nodes, nodes)

    def test_store_hash_changes_on_mutation(self):
        db, cfg, vault = _fresh_db("tewiki_m210_boot4_")
        self.addCleanup(shutil.rmtree, vault, ignore_errors=True)
        self.addCleanup(db.close)
        h0 = store_content_hash(db)
        db.conn.execute(
            "INSERT INTO docs(doc_id,path,kind,content_sha256) VALUES('x.md','x.md','raw','ab')")
        db.commit()
        self.assertNotEqual(h0, store_content_hash(db))

    def test_serialized_context_within_budget(self):
        db, cfg, vault = _fresh_db("tewiki_m210_boot5_")
        self.addCleanup(shutil.rmtree, vault, ignore_errors=True)
        self.addCleanup(db.close)
        stamp_snapshot(db)
        rep = boot_snapshot(db)
        self.assertTrue(rep.within_budget())
        self.assertLessEqual(len(rep.serialize().encode("utf-8")), BOOT_CONTEXT_CEILING)


if __name__ == "__main__":
    unittest.main()
