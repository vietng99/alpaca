"""M2.10 proof: the answer oracle abstains or answers, and never mints a groundless verdict.

Ported in spirit from the upstream Rune-2 suite (tests/test_r2_oracle.py, tests/test_oracle_refuse.py,
tests/test_oracle_ensemble.py, tests/test_r1_unconstructable.py, tests/test_lucid_detectors.py,
tests/test_echo_collapse.py). The upstream oracle tests drive the full retrieval door
(rune2.engine.answer -> engine.loop.Engine), which pulls in the retrieval plan, the FTS/vector
channels, the provider registry and project freshness. Those land in M2.11 and M2.12, so this
milestone (M2.10 depends only on M2.8 + M2.9) proves the same abstain-or-answer properties directly
against the vendored verdict surface: oracle.evaluate (the five-layer DAG), oracle.entailment_gate,
oracle.span_grounded, the Answer type's unconstructable core, plus the vendored lucid detector set
and echo collapse rule. Entailers and the embedder are stubbed INLINE here exactly as a provider
would be in M2.11, so the mechanism is pinned in isolation with no second door.

Every property is asserted on BOTH a positive and a negative path, and the four Done-when scenarios
are covered explicitly:
  * empty store abstains (evaluate with no citations -> not 'grounded');
  * a question with no relevant block abstains even when a stub embedder returns a HIGH score (the
    deterministic span-grounding veto ignores the score);
  * a superseded / contradicted pair is surfaced as a ranked conflict, never collapsed;
  * a grounded answer type is UNCONSTRUCTABLE without a non-null currency_stamp + completeness pair.

The templates (Layer-4 completeness predicates) and the compartment private-path filter are vendored
in later M2 tasks; the oracle and the writer name them, so stand-ins are registered here exactly like
a later real module would win via setdefault. No path exercised below enters those capabilities.
"""
import re
import shutil
import sys
import tempfile
import types
import unittest
from collections import namedtuple
from pathlib import Path

# --- stand-ins for the not-yet-vendored siblings the vendored importers name --------------------
# engine is a real package now (engine/__init__.py); its templates (M2.11) and compartment (M2.12)
# submodules are not. Register minimal stand-ins so oracle and the writer import.
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
from alpaca.wiki.determinism import edge_id as make_edge_id, normalize_text
from alpaca.wiki.engine import echo, lucid, oracle
from alpaca.wiki.engine.oracle import (
    arbitrate_conflict, entailment_gate, evaluate, span_grounded,
)
from alpaca.wiki.store.asof import AsOf
from alpaca.wiki.store.db import DB
from alpaca.wiki.store.ledger import Ledger
from alpaca.wiki.store.write import Writer
from alpaca.wiki.types import Answer, Completeness, CurrencyStamp

T0 = "2020-01-01T00:00:00+00:00"
ASOF = AsOf("2026-01-01T00:00:00+00:00")


# --------------------------------------------------------------------------- inline provider seams
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_NUM = re.compile(r"-?\d+(?:[.,]\d+)?|\d{4}-\d{2}-\d{2}")


def _tokens(text):
    return [t for t in _TOKEN.findall((text or "").lower()) if len(t) > 1]


class _StructuralEntailer:
    """A deterministic, conservative structural checker mirroring the upstream StructuralEntailer:
    token coverage >= 0.8 is 'supported', a number in the claim absent from the source is
    'contradicted', otherwise 'unsupported' (abstain territory - never fabricates support)."""
    name = "structural"

    def entail(self, claim, source):
        ct, st = _tokens(claim), _tokens(source)
        if not ct:
            return "unsupported", 0.0
        cset, sset = set(ct), set(st)
        coverage = len(cset & sset) / len(cset)
        cnums, snums = set(_NUM.findall(claim)), set(_NUM.findall(source))
        if cnums and not cnums.issubset(snums):
            return "contradicted", 0.9
        if coverage >= 0.8:
            return "supported", coverage
        return "unsupported", coverage


class _HighScoreEmbedder:
    """A stub embedder that returns a HIGH similarity for EVERY block, relevant or not. The oracle
    must abstain anyway: relevance score is advisory, span-grounding is the hard veto."""
    name = "high-embedder"

    def score(self, question, block_text):
        return 0.99


def _fresh_vault(prefix):
    vault = Path(tempfile.mkdtemp(prefix=prefix))
    cfg = Config.for_vault(vault)
    db = DB(cfg)
    db.pour()
    return db, cfg, vault


def _claim(text, bid, eid=None, kind="prose"):
    return {"text": text, "kind": kind, "source_block_id": bid, "source_edge_id": eid}


# =============================================================================== R2: abstain/answer
class TestAbstainOrAnswer(unittest.TestCase):
    """Ported from test_r2_oracle.py: grounded when span-grounded + entailed; abstain otherwise.
    Exercised directly on the oracle DAG with an in-memory cited-block map (no retrieval door)."""

    def setUp(self):
        self.db, self.cfg, self.vault = _fresh_vault("tewiki_m210_r2_")
        self.addCleanup(shutil.rmtree, self.vault, ignore_errors=True)
        self.addCleanup(self.db.close)
        self.ent = _StructuralEntailer()

    def _evaluate(self, claims, blocks, **kw):
        return evaluate(self.db, self.ent, ASOF, [], claims, blocks,
                        [], set(), [], [], list(blocks.keys()), **kw)

    def test_grounded_answer_is_span_grounded_and_entailed(self):
        # POSITIVE: the claim lives inside its cited block and the entailer supports it.
        blocks = {"b1": "Ada Placeholder works at Acme Corp."}
        ores = self._evaluate([_claim("Ada Placeholder works at Acme Corp", "b1", "e1")], blocks)
        self.assertEqual(ores.verdict, "grounded", msg=ores.reason)
        self.assertTrue(ores.citations)
        self.assertIsNotNone(ores.completeness)                 # non-null by construction

    def test_empty_store_abstains(self):
        # NEGATIVE (Done-when #1): no citations at all -> never grounded, and completeness present.
        ores = self._evaluate([], {})
        self.assertNotEqual(ores.verdict, "grounded")
        self.assertIn(ores.verdict, ("abstained", "insufficient"))
        self.assertIsNotNone(ores.completeness)                 # still non-null on abstain
        self.assertTrue(ores.gap and "reason" in ores.gap)      # abstain is never a silent dead end

    def test_no_relevant_block_abstains_despite_high_embedder_score(self):
        # NEGATIVE (Done-when #2): the embedder rates the block a near-perfect match, but the claim
        # is NOT a substring of that block. The span-grounding hard veto abstains regardless.
        block_text = "Casual notes about breezes, weather, and the color of the sky."
        self.assertGreater(_HighScoreEmbedder().score("boiling point of mercury?", block_text), 0.9)
        self.assertFalse(span_grounded("Mercury boils at 356.7 degrees", block_text))
        ores = self._evaluate([_claim("Mercury boils at 356.7 degrees", "b1", "e1")],
                              {"b1": block_text})
        self.assertEqual(ores.verdict, "abstained", msg=ores.reason)
        self.assertIsNotNone(ores.completeness)
        self.assertIn("b1", ores.gap["closest_pages"])

    def test_number_mismatch_is_a_hard_veto(self):
        # NEGATIVE: a number in the claim absent from the source is a resolvability hard-fail.
        blocks = {"b1": "Ada Placeholder works at Acme Corp."}
        ores = self._evaluate([_claim("Ada Placeholder works at Acme Corp in 1999", "b1", "e1")],
                              blocks)
        self.assertEqual(ores.verdict, "abstained")
        self.assertIn("number", ores.reason.lower())


# =============================================================================== refuse-to-invent
class TestRefuseToInvent(unittest.TestCase):
    """Ported from test_oracle_refuse.py: every asserted sentence must be entailed by a winner atom,
    else it is stripped/flagged; a fully-sourced draft is never over-flagged."""

    def test_unsourced_sentence_flagged(self):
        draft = "Alpha beta gamma. Zulu yankee xray whiskey."
        kept, flagged = entailment_gate(draft, ["alpha beta gamma delta"],
                                        _StructuralEntailer().entail)
        self.assertTrue(any("alpha" in s.lower() for s in kept))
        self.assertTrue(any("zulu" in s.lower() for s in flagged),
                        "an unsourced sentence must be flagged, never asserted")

    def test_grounded_not_overflagged(self):
        kept, flagged = entailment_gate("Alpha beta gamma.", ["alpha beta gamma"],
                                        _StructuralEntailer().entail)
        self.assertEqual(flagged, [])
        self.assertEqual(len(kept), 1)


# =============================================================================== no lone judge
class _Ensemble:
    """An inline abstaining ensemble (>=3 deterministic members, strict-majority rule): the point
    the upstream test_oracle_ensemble.py pins is that there is NO lone judge and a split abstains."""

    def __init__(self, members=None):
        self.members = members or [_StructuralEntailer(), _StructuralEntailer(), _StructuralEntailer()]
        if len(self.members) < 3:
            raise ValueError("an ensemble needs at least three members")

    def entail(self, claim, source):
        votes = [m.entail(claim, source)[0] for m in self.members]
        supported = sum(1 for v in votes if v == "supported")
        return ("supported", 1.0) if supported * 2 > len(votes) else ("unsupported", 0.0)


class TestEnsembleAndGap(unittest.TestCase):
    """Ported from test_oracle_ensemble.py: >=3 checkers, abstain on disagreement, GAP block on
    abstain so an abstention is never a silent dead end."""

    def setUp(self):
        self.db, self.cfg, self.vault = _fresh_vault("tewiki_m210_ens_")
        self.addCleanup(shutil.rmtree, self.vault, ignore_errors=True)
        self.addCleanup(self.db.close)

    def test_ensemble_needs_at_least_three_members(self):
        with self.assertRaises(ValueError):
            _Ensemble(members=[_StructuralEntailer(), _StructuralEntailer()])
        self.assertGreaterEqual(len(_Ensemble().members), 3)

    def test_gap_populated_on_abstain(self):
        # a claim citing a block absent from cited_blocks -> hard_fail -> abstained WITH a GAP block.
        claims = [_claim("x", "missing", "e0")]
        ores = evaluate(self.db, _StructuralEntailer(), ASOF, [], claims, {}, [], set(),
                        [], [], ["blk1", "blk2", "blk3"])
        self.assertEqual(ores.verdict, "abstained")
        self.assertTrue(ores.gap, "GAP block must be populated on abstain")
        self.assertIn("blk1", ores.gap["closest_pages"])


# =============================================================================== unconstructable
class TestUnconstructableAnswer(unittest.TestCase):
    """Ported from test_r1_unconstructable.py: a grounded Answer cannot be minted without a non-null
    currency_stamp + completeness pair - the confident-wrong gate, enforced by the type itself."""

    def test_grounded_answer_needs_currency_stamp_and_completeness(self):
        with self.assertRaises(TypeError):
            Answer(question="q", verdict="grounded", answer_text="a")   # missing the two guards

    def test_full_core_constructs(self):
        ans = Answer(question="q", verdict="grounded", answer_text="a",
                     currency_stamp=CurrencyStamp(as_of=T0),
                     completeness=Completeness(True, True, True))
        self.assertIsNotNone(ans.currency_stamp)
        self.assertIsNotNone(ans.completeness)
        self.assertFalse(ans.abstained)


# =============================================================================== conflict surfaced
class TestConflictSurfacesBoth(unittest.TestCase):
    """Done-when #4 at the oracle layer: two contradicting sources are surfaced as a ranked conflict,
    never collapsed. The superseded/older tip is never the winner; both edges appear in the ranking."""

    def setUp(self):
        self.db, self.cfg, self.vault = _fresh_vault("tewiki_m210_conf_")
        self.addCleanup(shutil.rmtree, self.vault, ignore_errors=True)
        self.addCleanup(self.db.close)
        echo.ensure_schema(self.db)
        c = self.db.conn
        # two docs; the 'beta' source is more authoritative than the stale 'acme' source.
        c.execute("INSERT INTO docs(doc_id,path,kind,content_sha256,source_authority) "
                  "VALUES('acme.md','acme.md','raw','s1',10)")
        c.execute("INSERT INTO docs(doc_id,path,kind,content_sha256,source_authority) "
                  "VALUES('beta.md','beta.md','raw','s2',50)")
        for n in ("zeta", "acme", "beta"):
            c.execute("INSERT INTO nodes(node_id,display_name,valid_from,recorded_at,status) "
                      "VALUES(?,?,?,?,'active')", (n, n, T0, T0))
        for bid, doc in (("acme.md#0", "acme.md"), ("beta.md#0", "beta.md")):
            c.execute("INSERT INTO blocks(block_id,block_content_id,doc_id,ordinal,text,"
                      "valid_from,recorded_at) VALUES(?,?,?,0,'t',?,?)", (bid, bid, doc, T0, T0))
        self.acme_edge = make_edge_id("zeta", "works_at", "acme", "bc_a")
        self.beta_edge = make_edge_id("zeta", "works_at", "beta", "bc_b")
        c.execute("INSERT INTO edges(edge_id,subj_node,predicate,obj_node,obj_datatype,"
                  "source_block_id,source_doc_id,source_quote,extractor,recorded_at,valid_from,status)"
                  " VALUES(?,?,?,?, 'node','acme.md#0','acme.md','q','deterministic',?, ?, 'invalidated')",
                  (self.acme_edge, "zeta", "works_at", "acme", T0, T0))
        c.execute("INSERT INTO edges(edge_id,subj_node,predicate,obj_node,obj_datatype,"
                  "source_block_id,source_doc_id,source_quote,extractor,recorded_at,valid_from,status)"
                  " VALUES(?,?,?,?, 'node','beta.md#0','beta.md','q','deterministic',?, ?, 'active')",
                  (self.beta_edge, "zeta", "works_at", "beta", "2024-01-01T00:00:00+00:00",
                   "2024-01-01T00:00:00+00:00"))
        self.db.commit()

    def _edges(self):
        return [dict(r) for r in self.db.conn.execute("SELECT * FROM edges").fetchall()]

    def test_conflict_ranked_by_authority_surfaces_both(self):
        ranking = arbitrate_conflict(self.db, self._edges())
        # both contradicting edges are surfaced (never collapsed to one).
        self.assertEqual(set(ranking["ranked"]), {self.acme_edge, self.beta_edge})
        # the more authoritative + more recent 'beta' tip wins; the stale 'acme' edge is never it.
        self.assertEqual(ranking["winner"], self.beta_edge)
        self.assertNotEqual(ranking["winner"], self.acme_edge)

    def test_superseded_value_never_served_as_winner(self):
        # NEGATIVE CONTROL: flip authority so recency is the only discriminator; the active, later
        # 'beta' edge still wins - a superseded value is never the current answer.
        self.db.conn.execute("UPDATE docs SET source_authority=50 WHERE doc_id='acme.md'")
        self.db.conn.execute("UPDATE docs SET source_authority=50 WHERE doc_id='beta.md'")
        self.db.commit()
        ranking = arbitrate_conflict(self.db, self._edges())
        self.assertEqual(ranking["winner"], self.beta_edge)


# =============================================================================== lucid detectors
CLK = "2026-01-01T00:00:00+00:00"


def _lucid_writer(cfg):
    db = DB(cfg)
    db.pour()
    w = Writer(db, Ledger(cfg.ledger_path, vault_dir=cfg.vault_dir), FixedClock(start=CLK, step=1))
    return db, w


def _add_predicate(db, predicate, inverse):
    db.conn.execute(
        "INSERT OR IGNORE INTO predicates(predicate,inverse,domain_type,range_type,extraction,"
        "cardinality,supersede_key) VALUES(?,?,?,?, 'deterministic','multi','subj+predicate+obj')",
        (predicate, inverse, "concept", "concept"),
    )


def _lblock(w, doc_id, bid, text):
    w.upsert_block({"block_id": bid, "block_content_id": bid, "occurrence_index": 0,
                    "doc_id": doc_id, "ordinal": int(bid[1:]) if bid[1:].isdigit() else 0,
                    "text": text, "domain": "general"})


def _ledge(db, w, subj, pred, obj, bcid, **over):
    eid = make_edge_id(subj, pred, obj, bcid)
    e = {"edge_id": eid, "subj_node": subj, "predicate": pred, "obj_node": obj,
         "obj_datatype": "node", "source_block_id": bcid, "source_doc_id": "d.md",
         "source_quote": f"{subj} {pred} {obj}", "extractor": "deterministic",
         "confidence": 1.0, "reconcile_verdict": "novel"}
    e.update(over)
    w.upsert_edge(e)
    return eid


def _lucid_fixture(cfg):
    db, w = _lucid_writer(cfg)
    with w.transaction():
        _add_predicate(db, "employs", "works_at")
        w.upsert_doc("d.md", "d.md", "raw", "sha", private=0)
        for n in ("x", "acme", "y", "beta", "p", "q", "hub", "ghost"):
            w.upsert_node(n, "concept", n)
        for i in range(1, 6):
            _lblock(w, "d.md", f"b{i}", f"block {i}")
        e1 = _ledge(db, w, "x", "works_at", "acme", "b1")          # missing inverse (employs)
        _ledge(db, w, "y", "works_at", "beta", "b2")               # inverse satisfied by e3
        _ledge(db, w, "beta", "employs", "y", "b3")                # provides inverse of e2
        _ledge(db, w, "p", "related_to", "hub", "b4", reconcile_verdict="contradicts")
        e5 = _ledge(db, w, "q", "related_to", "hub", "b5",
                    volatile=1, expires_at="2000-01-01T00:00:00+00:00")
        db.conn.execute(
            "INSERT INTO eval_ledger(query_id,qtype,verdict,abstained) VALUES(?,?,?,1)",
            ("q-abstain-1", "factoid", "abstained"))
    return db, w, {"e1": e1, "e5": e5}


class TestLucidDetectors(unittest.TestCase):
    """Ported from test_lucid_detectors.py: the Layer-1 deterministic gap detectors fire on exactly
    their pinned candidate sets, gap ids are content-derived (idempotent), rank orders by severity."""

    def setUp(self):
        self.vault = Path(tempfile.mkdtemp(prefix="tewiki_m210_lucid_"))
        self.addCleanup(shutil.rmtree, self.vault, ignore_errors=True)
        self.cfg = Config.for_vault(self.vault)

    def _by_type(self, db):
        by_type = {}
        for g in lucid.detect_gaps(db, now=CLK):
            by_type.setdefault(g["gap_type"], set()).add(g["subject_key"])
        return by_type

    def test_pinned_candidate_sets(self):
        db, w, ids = _lucid_fixture(self.cfg)
        by_type = self._by_type(db)
        try:
            self.assertEqual(by_type.get("RED_LINK"), {"acme", "hub"})
            self.assertEqual(by_type.get("STUB"), {"x", "y", "beta", "p", "q"})
            self.assertEqual(by_type.get("ORPHAN_NODE"), {"ghost"})
            self.assertEqual(by_type.get("MISSING_INVERSE"), {ids["e1"]})
            self.assertEqual(by_type.get("STALE_HOT"), {ids["e5"]})
            self.assertEqual(by_type.get("OPEN_CONTRADICTION"), {"p|related_to"})
            self.assertEqual(by_type.get("UNANSWERED_QUERY"), {"q-abstain-1"})
        finally:
            db.close()

    def test_gap_id_stable_and_ledger_idempotent(self):
        db, w, _ = _lucid_fixture(self.cfg)
        try:
            first = {g["gap_id"] for g in lucid.detect_gaps(db, now=CLK)}
            n1 = db.conn.execute("SELECT COUNT(*) c FROM gaps").fetchone()["c"]
            second = {g["gap_id"] for g in lucid.detect_gaps(db, now=CLK)}
            n2 = db.conn.execute("SELECT COUNT(*) c FROM gaps").fetchone()["c"]
            self.assertEqual(first, second)             # content-derived: same gaps, same ids
            self.assertEqual(n1, n2)                     # re-detect does not grow the ledger
        finally:
            db.close()

    def test_rank_orders_contradiction_above_stub(self):
        db, w, _ = _lucid_fixture(self.cfg)
        try:
            ranked = lucid.rank_gaps(lucid.detect_gaps(db, now=CLK))
            first_contra = next(i for i, g in enumerate(ranked)
                                if g["gap_type"] == "OPEN_CONTRADICTION")
            first_stub = next(i for i, g in enumerate(ranked) if g["gap_type"] == "STUB")
            self.assertLess(first_contra, first_stub)
        finally:
            db.close()


# =============================================================================== echo collapse
def _eblock(block_id, doc_id, ordinal, text):
    return {"block_id": block_id, "block_content_id": f"c_{block_id}", "occurrence_index": 0,
            "doc_id": doc_id, "ordinal": ordinal, "text": text}


def _primary_plus_echoes(cfg, n_echoes=3, attribute_to_ghost=False):
    db = DB(cfg)
    db.pour()
    echo.ensure_schema(db)
    w = Writer(db, Ledger(cfg.ledger_path, vault_dir=cfg.vault_dir),
               clock=FixedClock(start=T0))
    with w.transaction():
        w.upsert_doc("primary.md", "primary.md", "raw", "sha", byte_len=10, private=0)
        w.upsert_node("smith", node_type="person", display_name="Smith")
        w.upsert_node("acme", node_type="org", display_name="Acme")
        w.upsert_block(_eblock("primary.md#0", "primary.md", 0, "Smith works at Acme."))
        edge_id = w.upsert_edge({
            "edge_id": "e_test_smith_acme", "subj_node": "smith", "predicate": "works_at",
            "obj_node": "acme", "obj_literal": None, "obj_datatype": "node",
            "source_block_id": "primary.md#0", "source_doc_id": "primary.md",
            "source_quote": "Smith works at Acme.", "extractor": "deterministic"})
        echo.record_provenance(db, "primary.md#0", "original", cites_node_id="smith")
        for i in range(n_echoes):
            bid = f"primary.md#{i + 1}"
            w.upsert_block(_eblock(bid, "primary.md", i + 1, f"Echo number {i} of the same claim."))
            w.bump_corroboration(edge_id, bid)
            if attribute_to_ghost and i == 0:
                echo.record_provenance(db, bid, "attributes", cites_node_id="ghost")
            else:
                echo.record_provenance(db, bid, "quotes", cites_block_id="primary.md#0")
    return db, edge_id


class TestEchoCollapse(unittest.TestCase):
    """Ported from test_echo_collapse.py: 1 primary + N echoes collapse to a witness count of 1;
    an unprovable attribution yields a hard NULL, never the echo-inflated corroboration_count."""

    def setUp(self):
        self.vault = Path(tempfile.mkdtemp(prefix="tewiki_m210_echo_"))
        self.addCleanup(shutil.rmtree, self.vault, ignore_errors=True)
        self.cfg = Config.for_vault(self.vault)

    def test_one_primary_n_echoes_counts_one(self):
        db, eid = _primary_plus_echoes(self.cfg, n_echoes=3)
        try:
            raw = db.conn.execute(
                "SELECT corroboration_count FROM edges WHERE edge_id=?", (eid,)).fetchone()[0]
            self.assertEqual(raw, 4)                                 # echo-inflated raw count
            self.assertEqual(echo.independent_witness_count(db, eid), 1)   # collapsed to 1 primary
        finally:
            db.close()

    def test_unprovable_attribution_is_hard_null(self):
        db, eid = _primary_plus_echoes(self.cfg, n_echoes=3, attribute_to_ghost=True)
        try:
            self.assertIsNone(echo.independent_witness_count(db, eid))
        finally:
            db.close()

    def test_arbitration_uses_iwc_not_raw_count(self):
        db, eid = _primary_plus_echoes(self.cfg, n_echoes=3)
        try:
            edge = dict(db.conn.execute("SELECT * FROM edges WHERE edge_id=?", (eid,)).fetchone())
            self.assertEqual(echo.corroboration_for_arbitration(db, edge), 1)   # not the raw 4
        finally:
            db.close()

    def test_detect_echo_forms(self):
        rel, cb, _cn = echo.detect_echo("Smith works at Acme.", {"P": "Smith works at Acme."})
        self.assertEqual(rel, "quotes")
        self.assertEqual(cb, "P")
        rel2, _cb2, cn2 = echo.detect_echo("As [[Smith]] noted, the plan changed.",
                                           {"P": "Totally different sentence."})
        self.assertEqual(rel2, "attributes")
        self.assertEqual(cn2, "smith")


if __name__ == "__main__":
    unittest.main()
