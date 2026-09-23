"""M2.12 proof: the wiki write gates, retire-not-delete, and the ported lessons/knowledge stores.

Ported in spirit from the upstream Rune-2 suite (tests/test_shrink_gate.py, test_firewall.py,
test_shelf_life.py, test_curation_budget.py, test_curation_by_exception.py, test_merge_hardening.py,
test_rebuild_safety.py, test_keystone_reanchor.py, test_pkt3_cure.py, test_pkt3_expiry.py,
test_pkt3_mode.py, test_pkt4_dream.py). The upstream tests drive the vendored write path
(rune2.ingest.absorb) which lands in M2.14, not here; every property below is asserted directly
against the vendored gate/store surface, on BOTH a positive and a negative path.

Done-when properties proved:
  1. a re-ingest that loses more than the declared atom fraction ROLLS BACK (shrink gate);
  2. re-ingesting the same bytes changes nothing (hashgate idempotency);
  3. a vanished block RE-ANCHORS its edges to a surviving corroborating block and RETRACTS only if
     none exists (retire-not-delete + keystone re-anchor);
  4. a lesson without a discriminating probe is REFUSED (lessons.write);
  5. the ported lessons.* and knowledge.* negative-control tables run green (PROBED / ASSERTED /
     PROBE-BLOCKED + retention decay), and the second writer (reflect/dream) stays DISARMED, kill
     switch checked first.
"""
import shutil
import sqlite3
import hashlib
import sys
import tempfile
import types
import unittest
from pathlib import Path

# reconcile and resolve are the M2.14 ingest siblings (not vendored yet). encode.py imports them, and
# reflect.cure imports encode, so a stand-in lets cure import; the real modules win via setdefault.
_reconcile = types.ModuleType("alpaca.wiki.ingest.reconcile")
_resolve = types.ModuleType("alpaca.wiki.ingest.resolve")
_resolve.resolve_surface = lambda db, surface: []
sys.modules.setdefault("alpaca.wiki.ingest.reconcile", _reconcile)
sys.modules.setdefault("alpaca.wiki.ingest.resolve", _resolve)

# store.vec (embedding side-tables) and providers.registry are M2.11/M2.16 siblings the vendored
# reflect/dream.py imports. Dream ships DISARMED; the disarmed/kill-switch path never reaches either,
# so a stand-in that lets Dream construct is enough to assert the off state.
_vec = types.ModuleType("alpaca.wiki.store.vec")
_vec.ensure_vec_tables = lambda db: None
sys.modules.setdefault("alpaca.wiki.store.vec", _vec)
_prov_pkg = types.ModuleType("alpaca.wiki.providers")
_prov_pkg.__path__ = []
_prov_reg = types.ModuleType("alpaca.wiki.providers.registry")


class _Providers:
    def __init__(self, cfg):
        self.cfg = cfg


_prov_reg.Providers = _Providers
sys.modules.setdefault("alpaca.wiki.providers", _prov_pkg)
sys.modules.setdefault("alpaca.wiki.providers.registry", _prov_reg)

from alpaca.wiki.clock import FixedClock
from alpaca.wiki.config import Config
from alpaca.wiki.determinism import sha256_hex
from alpaca.wiki.ingest import firewall as fw
from alpaca.wiki.ingest import hashgate
from alpaca.wiki.ingest import shelf_life as sl
from alpaca.wiki.ingest.shrink import (
    BodyShrinkRejected, SHRINK_FLOOR_DEFAULT, assess_shrink, count_active_claims, shrink_floor_of,
)
from alpaca.wiki.engine import curation_budget as cbud
from alpaca.wiki.engine.curation_budget import Budget, apply_budget, effort_weight
from alpaca.wiki.reflect.cure import cure_edge
from alpaca.wiki.store import query as q
from alpaca.wiki.store.db import DB
from alpaca.wiki.store.ledger import Ledger
from alpaca.wiki.store.write import Writer

import alpaca.lessons as lessons
import alpaca.knowledge as knowledge

T0 = "2020-01-01T00:00:00+00:00"


def _edge(edge_id, obj_literal="Acme", predicate="works_at", source_block_id="d#0",
          source_quote="Alice works at Acme.", source_doc_id="d"):
    return {
        "edge_id": edge_id,
        "subj_node": "alice",
        "predicate": predicate,
        "obj_literal": obj_literal,
        "obj_datatype": "string",
        "source_block_id": source_block_id,
        "source_doc_id": source_doc_id,
        "source_quote": source_quote,
        "extractor": "deterministic",
        "valid_from": T0,
    }


class _VaultCase(unittest.TestCase):
    def setUp(self):
        self.vault = Path(tempfile.mkdtemp(prefix="tewiki_wg_"))
        self.addCleanup(shutil.rmtree, self.vault, ignore_errors=True)
        self.cfg = Config.for_vault(self.vault)
        self.db = DB(self.cfg)
        self.db.pour()
        self.addCleanup(self.db.conn.close)
        self.led = Ledger(self.cfg.ledger_path, vault_dir=self.cfg.vault_dir)
        self.w = Writer(self.db, self.led, clock=FixedClock(start=T0, step=1))

    def _seed_doc(self, doc_id="d", text="Alice works at Acme.", private=0):
        with self.w.transaction():
            self.w.upsert_doc(doc_id, f"raw/{doc_id}.md", "raw", sha256_hex(text), private=private)
            self.w.upsert_block({"block_id": f"{doc_id}#0", "block_content_id": f"{doc_id}c0",
                                 "occurrence_index": 0, "doc_id": doc_id, "ordinal": 0, "text": text})
            self.w.upsert_node("alice", "person", "Alice")


# --------------------------------------------------------------------- (1) shrink rollback


class TestShrinkGate(_VaultCase):
    def _four_edges(self):
        self._seed_doc()
        with self.w.transaction():
            for i in range(4):
                self.w.upsert_edge(_edge(f"e{i}", obj_literal=f"Org{i}"))

    def test_four_active_claims_measured(self):
        self._four_edges()
        self.assertEqual(count_active_claims(self.db, "d"), 4)

    def test_shrink_to_one_atom_rolls_back(self):
        # NEGATIVE: a re-ingest offering 1 atom against 4 prior claims trips the floor and raises,
        # so the caller's transaction rolls back rather than committing the collapse quietly.
        self._four_edges()
        with self.assertRaises(BodyShrinkRejected) as ctx:
            with self.w.transaction():
                assess_shrink(self.db, "d", 1)          # 1 < 0.70 * 4 = 2.8
                self.w.upsert_edge(_edge("e_should_not_persist", obj_literal="Ghost"))
        self.assertEqual(ctx.exception.n_before, 4)
        self.assertEqual(ctx.exception.n_after, 1)
        self.assertEqual(ctx.exception.claims_dropped, 3)
        # the whole transaction rolled back: the ghost edge never landed.
        self.assertIsNone(q.get_edge(self.db, "e_should_not_persist"))
        self.assertEqual(count_active_claims(self.db, "d"), 4)

    def test_waive_reason_bypasses_and_is_recorded(self):
        self._four_edges()
        report = assess_shrink(self.db, "d", 1, waive_reason="curator: source pruned by hand")
        self.assertTrue(report["waived"])
        self.assertEqual(report["waive_reason"], "curator: source pruned by hand")
        self.assertEqual(report["claims_dropped"], 3)

    def test_full_body_passes_gate(self):
        # POSITIVE: an equal-size re-ingest never trips.
        self._four_edges()
        report = assess_shrink(self.db, "d", 4)
        self.assertFalse(report["tripped"])
        self.assertEqual(report["claims_dropped"], 0)

    def test_text_length_shrink_trips_gate(self):
        self._four_edges()
        with self.assertRaises(BodyShrinkRejected):
            assess_shrink(self.db, "d", 4, old_text_len=1000, new_text_len=100)

    def test_floor_default_and_meta_override(self):
        self.assertEqual(shrink_floor_of(self.cfg), SHRINK_FLOOR_DEFAULT)
        self.cfg.meta["shrink_floor"] = "0.5"
        self.assertEqual(shrink_floor_of(self.cfg), 0.5)


# --------------------------------------------------------------------- (2) hashgate idempotency


class TestHashgateIdempotency(_VaultCase):
    def test_same_bytes_change_nothing(self):
        text = "Alice works at Acme."
        self._seed_doc(text=text)
        # NEGATIVE side: re-ingesting the identical bytes is a no-op - doc_changed says unchanged.
        changed, sha = hashgate.doc_changed(self.db, "d", text)
        self.assertFalse(changed)
        self.assertEqual(sha, sha256_hex(text))
        # POSITIVE side: a different body IS a change.
        changed2, _ = hashgate.doc_changed(self.db, "d", text + " Extra.")
        self.assertTrue(changed2)

    def test_reingesting_same_edge_is_identity_preserving(self):
        self._seed_doc()
        with self.w.transaction():
            self.w.upsert_edge(_edge("e0"))
        before = q.get_edge(self.db, "e0")
        n_before = self.db.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        corr_before = before["corroboration_count"]
        # re-ingest the SAME content-derived edge: ON CONFLICT DO NOTHING, no phantom corroboration.
        with self.w.transaction():
            self.w.upsert_edge(_edge("e0"))
        after = q.get_edge(self.db, "e0")
        n_after = self.db.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        self.assertEqual(n_before, n_after)
        self.assertEqual(after["corroboration_count"], corr_before)
        supersedes = self.db.conn.execute(
            "SELECT COUNT(*) FROM ingest_event WHERE op='supersede_edge'").fetchone()[0]
        self.assertEqual(supersedes, 0, "a pure re-ingest must not forge a supersede")


# --------------------------------------------------------------------- (3) retire-not-delete


class TestRetireNotDelete(_VaultCase):
    def test_retire_block_supersedes_and_retracts_without_deleting(self):
        self._seed_doc()
        with self.w.transaction():
            self.w.upsert_edge(_edge("e0"))
        with self.w.transaction():
            stats = self.w.retire_block("d#0")
        # NEGATIVE: no DELETE. The block row survives, superseded, its text intact.
        block = q.get_block(self.db, "d#0")
        self.assertIsNotNone(block, "retire must not delete the block row")
        self.assertEqual(block["status"], "superseded")
        self.assertEqual(block["text"], "Alice works at Acme.")
        # the unsupported edge is retracted (still present, status retracted), never deleted.
        edge = q.get_edge(self.db, "e0")
        self.assertIsNotNone(edge)
        self.assertEqual(edge["status"], "retracted")
        self.assertEqual(stats["retracted"], 1)
        self.assertEqual(stats["reanchored"], 0)

    def test_vanished_block_reanchors_to_a_surviving_corroborator(self):
        # POSITIVE: a corroborating block survives, so retiring the primary RE-ANCHORS rather than
        # retracts. Two blocks in two docs carry the same quote; the edge is corroborated by both.
        quote = "Alice works at Acme."
        self._seed_doc(doc_id="d", text=quote)
        with self.w.transaction():
            self.w.upsert_doc("d2", "raw/d2.md", "raw", sha256_hex(quote), private=0)
            self.w.upsert_block({"block_id": "d2#0", "block_content_id": "d2c0",
                                 "occurrence_index": 0, "doc_id": "d2", "ordinal": 0, "text": quote})
            self.w.upsert_edge(_edge("e0"))
            self.w.bump_corroboration("e0", "d2#0")
        with self.w.transaction():
            stats = self.w.retire_block("d#0")
        self.assertEqual(stats["reanchored"], 1)
        self.assertEqual(stats["retracted"], 0)
        edge = q.get_edge(self.db, "e0")
        self.assertEqual(edge["status"], "active", "a corroborated edge must survive its source")
        self.assertEqual(edge["source_block_id"], "d2#0", "edge re-anchored to the survivor")


# --------------------------------------------------------------------- (4) shelf-life / expiry


class TestShelfLife(_VaultCase):
    VOLATILE_DOC = ("---\nexpires: 2020-01-01\nvolatile: true\n---\n[[Roadmap]] refines [[Plan]].\n")

    def test_parse_expires_and_volatile(self):
        p = sl.parse_shelf_life(self.VOLATILE_DOC)
        self.assertEqual(p["expires_at"], "2020-01-01T00:00:00+00:00")
        self.assertEqual(p["volatile"], 1)

    def test_parse_absent_frontmatter_defaults(self):
        p = sl.parse_shelf_life("[[A]] works at [[B]].\n")
        self.assertIsNone(p["expires_at"])
        self.assertEqual(p["volatile"], 0)

    def test_expired_volatile_is_stale_not_fresh(self):
        edges = [{"edge_id": "e1", "expires_at": "2020-01-01T00:00:00+00:00", "volatile": 1}]
        fresh, stale = sl.expiry_partition(edges, "2026-08-16T00:00:00+00:00")
        self.assertEqual([e["edge_id"] for e in stale], ["e1"])
        self.assertEqual(fresh, [])

    def test_unexpired_stays_fresh(self):
        edges = [{"edge_id": "e1", "expires_at": "2099-01-01T00:00:00+00:00", "volatile": 1}]
        fresh, stale = sl.expiry_partition(edges, "2026-08-16T00:00:00+00:00")
        self.assertEqual([e["edge_id"] for e in fresh], ["e1"])
        self.assertEqual(stale, [])

    def test_stamp_edges_and_count_expired(self):
        self._seed_doc(doc_id="r", text="Roadmap refines Plan.")
        with self.w.transaction():
            self.w.upsert_edge(_edge("e_road", predicate="refines", source_block_id="r#0",
                                     source_doc_id="r", source_quote="Roadmap refines Plan."))
        p = sl.parse_shelf_life(self.VOLATILE_DOC)
        with self.w.transaction():
            n = sl.stamp_edges(self.db, "r", expires_at=p["expires_at"], volatile=p["volatile"])
        self.assertGreaterEqual(n, 1)
        self.assertEqual(sl.count_expired(self.db, "2026-08-16T00:00:00+00:00"), n)
        self.assertEqual(sl.count_expired(self.db, "2019-01-01T00:00:00+00:00"), 0)


# --------------------------------------------------------------------- (5) curation budget


class TestCurationBudget(unittest.TestCase):
    def _items(self, n):
        return [{"gap_id": f"g{i}", "gap_type": "STUB"} for i in range(n)]

    def test_conservation_surfaced_plus_deferred_equals_total(self):
        # POSITIVE + NEGATIVE (overflow): the cap surfaces some, defers the rest, drops nothing.
        out = apply_budget(self._items(10), max_items=3)
        self.assertEqual(out["surfaced_count"], 3)
        self.assertEqual(out["deferred_count"], 7)
        self.assertEqual(out["surfaced_count"] + out["deferred_count"], out["total"])

    def test_cap_zero_defers_everything(self):
        out = apply_budget(self._items(4), max_items=0)
        self.assertEqual(out["surfaced_count"], 0)
        self.assertEqual(out["deferred_count"], 4)

    def test_effort_weight_is_integer(self):
        w = effort_weight("OPEN_CONTRADICTION")
        self.assertIsInstance(w, int)
        self.assertEqual(effort_weight("NOT_A_TYPE"), cbud.DEFAULT_EFFORT)

    def test_budget_dataclass(self):
        b = Budget(max_items_per_day=5)
        self.assertEqual(b.max_items_per_day, 5)
        self.assertEqual(b.minutes_target, 60)


# --------------------------------------------------------------------- (6) firewall tripwire


class TestFirewall(unittest.TestCase):
    def test_tier1_marking_trips(self):
        r = fw.scan("This report is marked NOFORN and must be handled accordingly.")
        self.assertTrue(r.tripped)
        self.assertEqual(r.tier, 1)
        self.assertTrue(r.message.startswith("marking detected:"))

    def test_corporate_marking_does_not_trip(self):
        # a benign corporate stamp is NOT a government control marking.
        r = fw.scan("Company Confidential - internal distribution only.")
        self.assertFalse(r.tripped)

    def test_vietnamese_tier1_trips_and_benign_does_not(self):
        self.assertTrue(fw.scan("Tai lieu TUYỆT MẬT cua co quan.").tripped)
        # benign 'toi mat' (I lost) must NOT be dragged in by diacritic-stripping.
        self.assertFalse(fw.scan("Hom qua toi mat cai vi cua toi.").tripped)

    def test_escalate_is_raise_only(self):
        clean = fw.scan("an ordinary sentence")
        # a clean scan never lowers an existing label.
        self.assertEqual(fw.escalate_sensitivity("controlled-marking", clean), "controlled-marking")
        trip = fw.scan("NOFORN")
        self.assertEqual(fw.escalate_sensitivity("none", trip), "controlled-marking")


# --------------------------------------------------------------------- (7) cure: retract not delete


class TestCure(_VaultCase):
    def test_cure_edge_retracts_never_deletes(self):
        self._seed_doc()
        with self.w.transaction():
            self.w.upsert_edge(_edge("e_poison", obj_literal="EvilCorp",
                                     source_quote="Alice works at EvilCorp."))
        with self.w.transaction():
            res = cure_edge(self.db, self.w, "e_poison", reason="poison", retracted_by="test")
        self.assertTrue(res["ok"])
        edge = q.get_edge(self.db, "e_poison")
        self.assertIsNotNone(edge, "cure must retract, never delete")
        self.assertEqual(edge["status"], "retracted")

    def test_cure_missing_edge_is_a_clean_no_op(self):
        self._seed_doc()
        with self.w.transaction():
            res = cure_edge(self.db, self.w, "does_not_exist", reason="x", retracted_by="test")
        self.assertFalse(res["ok"])


# --------------------------------------------------------------------- (8) dream stays disarmed


class TestDreamDisarmed(unittest.TestCase):
    def _fresh_dream(self, **cfg_over):
        from dataclasses import replace
        from alpaca.wiki.reflect.dream import Dream
        vault = Path(tempfile.mkdtemp(prefix="tewiki_dream_"))
        self.addCleanup(shutil.rmtree, vault, ignore_errors=True)
        cfg = Config.for_vault(vault)
        if cfg_over:
            cfg = replace(cfg, **cfg_over)
        return Dream(cfg, clock=FixedClock()), cfg

    def test_disarmed_by_default_never_self_modifies(self):
        from alpaca.wiki.reflect.dream import read_dream_records
        dr, cfg = self._fresh_dream()
        try:
            self.assertFalse(cfg.dream_armed)
            res = dr.run()
            self.assertEqual(res["status"], "disarmed")
            self.assertEqual(res["reason"], "not armed")
            self.assertEqual(len(read_dream_records(dr.db)), 0)
        finally:
            dr.close()

    def test_kill_switch_checked_first_even_when_armed(self):
        from alpaca.wiki.reflect.dream import KILL_FILE, read_dream_records
        dr, cfg = self._fresh_dream(dream_armed=True)
        try:
            (Path(cfg.vault_dir) / KILL_FILE).write_text("stop", encoding="utf-8")
            res = dr.run()
            self.assertEqual(res["status"], "disarmed")
            self.assertIn("kill-switch", res["reason"])
            self.assertEqual(len(read_dream_records(dr.db)), 0)
        finally:
            dr.close()

    def test_config_kill_switch_forces_disarmed(self):
        dr, cfg = self._fresh_dream(dream_armed=True, dream_kill_switch=True)
        try:
            self.assertEqual(dr.run()["status"], "disarmed")
        finally:
            dr.close()


# --------------------------------------------------------------------- (9) lessons write-gate


class TestLessonsWriteGate(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="tewiki_lessons_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.prov = self.tmp / "grounding.md"
        self.prov.write_text("grounding\n", encoding="utf-8")

    def _lesson(self, **over):
        base = dict(
            label="pair able-to-fail with able-to-pass",
            description="a checker proven only able-to-fail is unfalsified where it matters",
            value="Prove any detector able-to-pass on known-good input as well as able-to-fail.",
            provenance="grounding.md",
            governs=["detector", "witness"],
            prescribes=r"able-to-pass|primary artifact",
            roots=[self.tmp],
        )
        base.update(over)
        return base

    def test_lesson_with_discriminating_probe_is_admitted(self):
        block = lessons.write(self.conn, self._lesson(), lessons.SEED_CORPUS)
        self.assertEqual(block["governs"], ["detector", "witness"])
        self.assertTrue(block["admission_probe_sig"].startswith("sha256:"))

    def test_lesson_without_a_discriminating_probe_is_refused(self):
        # NEGATIVE: a constant prescription cannot separate a good action from a bad one.
        with self.assertRaises(lessons.RejectedLesson) as ctx:
            lessons.write(self.conn, self._lesson(prescribes=r".*", label="be careful",
                                                 value="Always be careful."), lessons.SEED_CORPUS)
        self.assertEqual(ctx.exception.control, "NC-reward")

    def test_lesson_with_no_probe_corpus_fails_safe(self):
        with self.assertRaises(lessons.RejectedLesson):
            lessons.write(self.conn, self._lesson(), [])

    def test_control_table_runs_green(self):
        passed, total, rows = lessons.selftest()
        failed = [r for r in rows if not r[2]]
        self.assertEqual(passed, total, msg=f"lessons controls failed: {failed}")


# --------------------------------------------------------------------- (10) knowledge store


class TestKnowledgeStore(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="tewiki_knowledge_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.prov = self.tmp / "prov.md"
        self.prov.write_text("line one\nline two\n", encoding="utf-8")

    def _fact(self, **over):
        base = dict(
            claim="the emulator build requires ZEBU_IP_ROOT set before launch",
            scope="OP_ROOT", provenance="prov.md:1",
            falsifiability="cmd: unset ZEBU_IP_ROOT and re-run; refuted if the build completes. prov.md",
            run_id="t", roots=[self.tmp])
        base.update(over)
        return base

    def test_record_asserted_and_run_derived_lands_quarantine(self):
        r = knowledge.record(self.conn, self._fact(), knowledge.ASSERTED)
        self.assertEqual(r["stamp"], knowledge.ASSERTED)
        self.assertEqual(r["blind_class"], knowledge.QUARANTINE)

    def test_probed_state_requires_a_resolving_receipt(self):
        # NEGATIVE: a bare PROBED with no receipt fails safe rather than over-stamping.
        with self.assertRaises(knowledge.RejectedWrite):
            knowledge.record(self.conn, self._fact(), knowledge.PROBED)

    def test_probed_with_receipt_is_probed(self):
        r = knowledge.record(self.conn, self._fact(probe_receipt="prov.md:1"), knowledge.PROBED)
        self.assertEqual(r["stamp"], knowledge.PROBED)

    def test_dangling_provenance_is_refused(self):
        with self.assertRaises(knowledge.RejectedWrite):
            knowledge.record(self.conn, self._fact(provenance="ghost/none.md:9"), knowledge.ASSERTED)

    def test_retention_decay_removes_from_read_path_without_delete(self):
        knowledge.record(self.conn, self._fact(), knowledge.ASSERTED)
        ks = knowledge.KnowledgeStore(self.conn, roots=[self.tmp])
        cid = list(ks.query(scope="OP_ROOT"))[0]["claim_id"]
        ks.decay("OP_ROOT", cid)
        self.assertEqual(ks.query(scope="OP_ROOT").count(), 0, "a decayed claim drops from reads")
        # NEGATIVE on delete: the row still exists (retire-not-delete).
        surviving = self.conn.execute(
            "SELECT decay_state FROM knowledge_facts WHERE claim_id=?", (cid,)).fetchone()
        self.assertEqual(surviving["decay_state"], "DECAYED")

    def test_arbitrary_signature_text_cannot_promote_run_derived_fact(self):
        knowledge.record(self.conn, self._fact(), knowledge.ASSERTED)
        ks = knowledge.KnowledgeStore(self.conn, roots=[self.tmp])
        cid = list(ks.query(scope="OP_ROOT"))[0]["claim_id"]
        with self.assertRaises(knowledge.RejectedWrite) as denied:
            ks.owner_promote(cid, "OP_ROOT", knowledge.PUBLIC,
                             signature="arbitrary-nonempty-string")
        self.assertIn("authorization verifier", denied.exception.reason)
        self.assertEqual(list(ks.query(scope="OP_ROOT"))[0]["blind_class"], knowledge.QUARANTINE)

    def test_preconfigured_verifier_requires_exact_claim_binding(self):
        knowledge.record(self.conn, self._fact(), knowledge.ASSERTED)
        base = knowledge.KnowledgeStore(self.conn, roots=[self.tmp])
        cid = list(base.query(scope="OP_ROOT"))[0]["claim_id"]

        approvals = {
            "approval-42": {
                "approval_id": "approval-42", "claim_id": cid, "scope": "OP_ROOT",
                "claim_sha256": hashlib.sha256(self._fact()["claim"].encode("utf-8")).hexdigest(),
                "from_class": knowledge.QUARANTINE, "to_class": knowledge.PUBLIC,
            },
        }

        def verifier(_request, approval_ref):
            return approvals.get(approval_ref)

        ks = knowledge.KnowledgeStore(self.conn, roots=[self.tmp], authorization_verifier=verifier)
        with self.assertRaises(knowledge.RejectedWrite):
            ks.owner_promote(cid, "OP_ROOT", knowledge.PUBLIC, approval_ref="missing-approval")
        result = ks.owner_promote(cid, "OP_ROOT", knowledge.PUBLIC, approval_ref="approval-42")
        self.assertEqual(result["now"], knowledge.PUBLIC)

    def test_verifier_exception_fails_closed(self):
        knowledge.record(self.conn, self._fact(), knowledge.ASSERTED)
        cid = list(knowledge.KnowledgeStore(self.conn, roots=[self.tmp]).query(scope="OP_ROOT"))[0]["claim_id"]

        def unavailable(_request, _approval_ref):
            raise OSError("approval registry unavailable")

        ks = knowledge.KnowledgeStore(self.conn, roots=[self.tmp], authorization_verifier=unavailable)
        with self.assertRaises(knowledge.RejectedWrite) as denied:
            ks.owner_promote(cid, "OP_ROOT", knowledge.PUBLIC, approval_ref="approval-44")
        self.assertIn("authorization verifier failed", denied.exception.reason)

    def test_verifier_approval_with_wrong_content_binding_is_refused(self):
        knowledge.record(self.conn, self._fact(), knowledge.ASSERTED)
        base = knowledge.KnowledgeStore(self.conn, roots=[self.tmp])
        cid = list(base.query(scope="OP_ROOT"))[0]["claim_id"]

        def wrong_content(request, approval_ref):
            return {"approval_id": approval_ref, **request, "claim_sha256": "not-the-claim"}

        ks = knowledge.KnowledgeStore(self.conn, roots=[self.tmp], authorization_verifier=wrong_content)
        with self.assertRaises(knowledge.RejectedWrite) as denied:
            ks.owner_promote(cid, "OP_ROOT", knowledge.PUBLIC, approval_ref="approval-43")
        self.assertIn("approval binding mismatch", denied.exception.reason)

    def test_claim_change_during_verification_fails_closed(self):
        knowledge.record(self.conn, self._fact(), knowledge.ASSERTED)
        cid = list(knowledge.KnowledgeStore(self.conn, roots=[self.tmp]).query(scope="OP_ROOT"))[0]["claim_id"]

        def racing_verifier(request, approval_ref):
            self.conn.execute("UPDATE knowledge_facts SET claim=? WHERE scope=? AND claim_id=?",
                              ("changed after approval was read", request["scope"], request["claim_id"]))
            return {"approval_id": approval_ref, **request}

        ks = knowledge.KnowledgeStore(self.conn, roots=[self.tmp], authorization_verifier=racing_verifier)
        with self.assertRaises(knowledge.RejectedWrite) as denied:
            ks.owner_promote(cid, "OP_ROOT", knowledge.PUBLIC, approval_ref="approval-45")
        self.assertIn("claim changed during authorization", denied.exception.reason)

    def test_control_table_runs_green(self):
        passed, total, rows = knowledge.selftest()
        failed = [r for r in rows if not r[2]]
        self.assertEqual(passed, total, msg=f"knowledge controls failed: {failed}")


if __name__ == "__main__":
    unittest.main()
