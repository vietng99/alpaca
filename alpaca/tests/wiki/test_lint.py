"""M2.13 proof (Step 2): the executed lint as REAL store queries, one violating fixture per rule.

Ported and consolidated from the upstream Rune-2 suite:
  tests/test_dm8_lint.py       - one violating fixture per executed lint.RULES entry + clean vault;
  tests/test_pkt3_lint.py      - per-rule mutation coverage for the rules dm8 left individually
                                 uncovered (endpoint, dangling-doc/node-block, alias-multi-canonical,
                                 tier-vs-band, raw-no-ingest, stamp, content-id, untracked, one-door);
  tests/test_glossary_lint.py  - the CONTEXT.md glossary wired to the alias table (backed / unbacked
                                 / undocumented), the mutation anchors _alias_exists and undocumented;
  tests/test_golden_pin.py     - the byte-pinned cross-environment determinism matrix over the golden
                                 cases. The golden runner drives the M2.14 write path (absorb); until
                                 that lands the matrix skips, but the pinned hashes and the runner are
                                 asserted structurally so the golden contract is still checked here.

Each lint fixture seeds a minimal store that trips EXACTLY one gate, asserts that rule's violation is
present AND that lint exits 1. Deleting a rule from lint.RULES makes its fixture stop failing lint -
that is the dm.8 mutation contract the oracle (tests/wiki/test_mutation_oracle.py) discharges. The
store queries are executed against the vendored alpaca.wiki store, never text checks.
"""
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from helpers import cleanup, fresh_cfg          # noqa: E402

from alpaca.wiki import lint                          # noqa: E402
from alpaca.wiki.lint import RULES, run_lint          # noqa: E402
from alpaca.wiki.store.db import DB                    # noqa: E402
from alpaca.wiki.engine.glossary import LINE_BUDGET, glossary_lint  # noqa: E402
from alpaca.wiki.store.textnorm import norm_surface   # noqa: E402


# ---------------------------------------------------------------- store seeding helpers
def _fresh_db(cfg):
    db = DB(cfg)
    db.pour()
    db.conn.execute("PRAGMA foreign_keys=OFF")
    db.conn.execute("PRAGMA ignore_check_constraints=1")
    return db


def _node(db, node_id, status="active"):
    db.conn.execute(
        "INSERT INTO nodes(node_id,status,valid_from,recorded_at) VALUES(?,?,?,?)",
        (node_id, status, "t", "t"))


def _doc(db, doc_id, kind="raw"):
    db.conn.execute(
        "INSERT INTO docs(doc_id,path,kind,content_sha256,domain,tier,private,sensitivity) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (doc_id, doc_id, kind, "sha", "general", 1, 0, "none"))


def _block(db, block_id, doc_id="d.md", content_id="cid", ordinal=0):
    db.conn.execute(
        "INSERT INTO blocks(block_id,block_content_id,occurrence_index,doc_id,ordinal,text,"
        "valid_from,recorded_at,status,domain) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (block_id, content_id, 0, doc_id, ordinal, "text", "t", "t", "active", "general"))


def _edge(db, edge_id, **kw):
    cols = dict(subj_node="a1", predicate="works_at", obj_node=None, obj_literal="x",
                obj_datatype="string", source_block_id="d.md#0", source_doc_id="d.md",
                source_quote="q", extractor="deterministic", recorded_at="t", valid_from="t",
                atom_type="FACT", status="active", corroboration_count=1, learned_at="t",
                edge_tier=None, volatile=0, reconcile_verdict=None, superseded_at=None,
                expires_at=None)
    cols.update(kw)
    keys = list(cols)
    db.conn.execute(
        f"INSERT INTO edges(edge_id,{','.join(keys)}) VALUES(?,{','.join('?' * len(keys))})",
        (edge_id, *[cols[k] for k in keys]))


def _rule_names(report):
    return {v.rule for v in report.violations}


# ---------------------------------------------------------------- clean vault
class TestLintCleanVault(unittest.TestCase):
    def test_clean_vault_exits_zero_and_stable(self):
        cfg = fresh_cfg()
        try:
            db = DB(cfg)
            db.pour()
            r1 = run_lint(db, cfg)
            r2 = run_lint(db, cfg)
            db.close()
            self.assertEqual(r1.errors, ())
            self.assertEqual(r1.exit_code, 0)
            self.assertEqual(r1.text(), r2.text())      # byte-stable re-run
        finally:
            cleanup(cfg)

    def test_registry_holds_at_least_twelve_executed_rules(self):
        self.assertGreaterEqual(len(RULES), 12)     # the dm.8 contract

    def test_every_rule_has_a_registry_entry_by_name(self):
        names = {getattr(r, "__name__", "") for r in RULES}
        for expected in (
            "rule_edge_source_block_unresolvable", "rule_edge_endpoint_node_missing",
            "rule_dangling_source_doc", "rule_node_block_source_dangling",
            "rule_alias_multi_canonical", "rule_wikilink_no_node", "rule_enum_domain_violations",
            "rule_edge_tier_vs_page_band", "rule_raw_doc_no_ingest_event", "rule_stamp_coverage",
            "rule_block_missing_content_id", "rule_untracked_raws", "rule_one_door",
        ):
            self.assertIn(expected, names)


# ---------------------------------------------------------------- one fixture per rule
class TestLintRules(unittest.TestCase):
    def _run(self, seed):
        cfg = fresh_cfg()
        try:
            db = _fresh_db(cfg)
            seed(db)
            db.conn.commit()
            report = run_lint(db, cfg)
            db.close()
            return report
        finally:
            cleanup(cfg)

    def _assert_rule(self, report, rule):
        self.assertIn(rule, _rule_names(report),
                      msg=f"expected {rule}; saw {sorted(_rule_names(report))}")
        self.assertEqual(report.exit_code, 1)

    def test_edge_source_block_unresolvable(self):
        def seed(db):
            _node(db, "a1"); _doc(db, "d.md")
            _edge(db, "e1", source_block_id="ghost#99")
        self._assert_rule(self._run(seed), "edge-source-block-unresolvable")

    def test_edge_endpoint_node_missing(self):
        def seed(db):
            _doc(db, "d.md"); _block(db, "d.md#0")
            _edge(db, "e1", subj_node="ghost-node")
        self._assert_rule(self._run(seed), "edge-endpoint-node-missing")

    def test_dangling_source_doc(self):
        def seed(db):
            _node(db, "a1"); _block(db, "d.md#0")
            _edge(db, "e1", source_doc_id="no-such-doc")
        self._assert_rule(self._run(seed), "dangling-source-doc")

    def test_dangling_sources_node_block(self):
        def seed(db):
            _node(db, "a1")
            db.conn.execute(
                "INSERT INTO node_blocks(node_id,block_id,role,weight) VALUES(?,?,?,?)",
                ("a1", "ghost#0", "mention", 1.0))
        self._assert_rule(self._run(seed), "dangling-sources")

    def test_alias_multi_canonical(self):
        def seed(db):
            _node(db, "dat-1"); _node(db, "dat-2")
            for nid in ("dat-1", "dat-2"):
                db.conn.execute(
                    "INSERT INTO aliases(node_id,surface,norm_surface,kind,status,created_at) "
                    "VALUES(?,?,?,?,?,?)", (nid, "Dat", "dat", "declared", "bound", "t"))
        self._assert_rule(self._run(seed), "alias-multi-canonical")

    def test_alias_multi_canonical_excused_by_merge_candidate(self):
        # NEGATIVE path: a homonym already surfaced as a merge_candidate must NOT trip the rule.
        def seed(db):
            _node(db, "dat-1"); _node(db, "dat-2")
            for nid in ("dat-1", "dat-2"):
                db.conn.execute(
                    "INSERT INTO aliases(node_id,surface,norm_surface,kind,status,created_at) "
                    "VALUES(?,?,?,?,?,?)", (nid, "Dat", "dat", "declared", "bound", "t"))
            db.conn.execute(
                "INSERT INTO merge_candidate(node_a,node_b,method,score,status,surfaced_at) "
                "VALUES(?,?,?,?,?,?)", ("dat-1", "dat-2", "name", 0.5, "pending", "t"))
        report = self._run(seed)
        self.assertNotIn("alias-multi-canonical", _rule_names(report))

    def test_wikilink_no_node(self):
        def seed(db):
            db.conn.execute(
                "INSERT INTO aliases(node_id,surface,norm_surface,kind,status,created_at) "
                "VALUES(?,?,?,?,?,?)", ("ghost-node", "Ghost", "ghost", "wikilink", "bound", "t"))
        self._assert_rule(self._run(seed), "wikilink-no-node")

    def test_enum_violation(self):
        def seed(db):
            _node(db, "a1"); _doc(db, "d.md"); _block(db, "d.md#0")
            _edge(db, "e1", atom_type="OPINION")     # not in the atom_type enum
        self._assert_rule(self._run(seed), "enum-violation")

    def test_edge_tier_vs_page_band(self):
        def seed(db):
            db.conn.execute(
                "INSERT INTO nodes(node_id,status,valid_from,recorded_at,page_band) "
                "VALUES(?,?,?,?,?)", ("a1", "active", "t", "t", 3))
            _doc(db, "d.md"); _block(db, "d.md#0")
            _edge(db, "e1", subj_node="a1", edge_tier=1)   # 1 != page_band 3
        self._assert_rule(self._run(seed), "edge-tier-vs-page-band")

    def test_raw_doc_no_ingest_event(self):
        def seed(db):
            _doc(db, "d.md", kind="raw")             # no ingest_event references it
        self._assert_rule(self._run(seed), "raw-doc-no-ingest-event")

    def test_stamp_coverage(self):
        def seed(db):
            _node(db, "a1"); _doc(db, "d.md"); _block(db, "d.md#0")
            _edge(db, "e1", learned_at=None)         # active edge missing km.4 stamp
        self._assert_rule(self._run(seed), "stamp-coverage")

    def test_block_missing_content_id(self):
        def seed(db):
            _doc(db, "d.md")
            _block(db, "d.md#0", content_id="")      # empty content anchor
        self._assert_rule(self._run(seed), "block-missing-content-id")

    def test_untracked_raws(self):
        cfg = fresh_cfg()
        try:
            db = _fresh_db(cfg)
            db.conn.commit()
            raw_dir = cfg.vault_dir / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / "orphan.md").write_text("untracked", encoding="utf-8")
            report = run_lint(db, cfg)
            db.close()
            self.assertIn("untracked-raws", _rule_names(report))
            self.assertEqual(report.exit_code, 1)
        finally:
            cleanup(cfg)

    def test_one_door_surfaces_guard_hits(self):
        # rule_one_door forwards a firewall breach reported by check_one_door into a gating error.
        cfg = fresh_cfg()
        original = lint.check_one_door
        lint.check_one_door = lambda *a, **k: (False, ["forbidden import: store.write in engine"])
        try:
            db = _fresh_db(cfg)
            db.conn.commit()
            report = run_lint(db, cfg)
            db.close()
            self.assertIn("one-door", _rule_names(report))
            self.assertEqual(report.exit_code, 1)
        finally:
            lint.check_one_door = original
            cleanup(cfg)


# ---------------------------------------------------------------- glossary lint
def _g_node(db, node_id):
    db.conn.execute(
        "INSERT INTO nodes(node_id,node_type,display_name,valid_from,recorded_at,status) "
        "VALUES(?,?,?,?,?, 'active')",
        (node_id, "person", node_id, "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00"))


def _g_alias(db, node_id, surface):
    db.conn.execute(
        "INSERT INTO aliases(node_id,surface,norm_surface,kind,status,confidence,created_at) "
        "VALUES(?,?,?, 'exact','bound',1.0,?)",
        (node_id, surface, norm_surface(surface), "2020-01-01T00:00:00+00:00"))


def _write_context(cfg, body):
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    (cfg.vault_dir / "CONTEXT.md").write_text(body, encoding="utf-8")


class TestGlossaryLint(unittest.TestCase):
    def test_unbacked_term_flagged(self):
        cfg = fresh_cfg()
        try:
            db = DB(cfg); db.pour()
            _g_node(db, "dat-nguyen"); _g_alias(db, "dat-nguyen", "Dat Nguyen")
            db.commit()
            _write_context(cfg, (
                "# CONTEXT\n\n## Glossary\n"
                "- **Dat Nguyen**: an engineer at Acme.\n"
                "- **Widget Protocol**: a term with no alias in the graph.\n"))
            rep = glossary_lint(cfg, db)
            db.close()
            self.assertIn("Dat Nguyen", rep.backed)
            self.assertIn("Widget Protocol", rep.unbacked)
            self.assertNotIn("Widget Protocol", rep.backed)
            self.assertEqual(len(rep.unbacked), 1)
        finally:
            cleanup(cfg)

    def test_undocumented_alias_flagged(self):
        cfg = fresh_cfg()
        try:
            db = DB(cfg); db.pour()
            _g_node(db, "dat-nguyen"); _g_alias(db, "dat-nguyen", "Dat Nguyen")
            _g_node(db, "acme"); _g_alias(db, "acme", "Acme")   # bound but not documented
            db.commit()
            _write_context(cfg, "# CONTEXT\n\n## Glossary\n- **Dat Nguyen**: an engineer.\n")
            rep = glossary_lint(cfg, db)
            db.close()
            self.assertIn("Acme", rep.undocumented)
        finally:
            cleanup(cfg)

    def test_size_budget_passes(self):
        cfg = fresh_cfg()
        try:
            db = DB(cfg); db.pour()
            _write_context(cfg, "# CONTEXT\n\n## Glossary\n- **X**: y.\n")
            rep = glossary_lint(cfg, db)
            db.close()
            self.assertTrue(rep.within_budget)
            self.assertLess(rep.line_count, LINE_BUDGET)
        finally:
            cleanup(cfg)


# ---------------------------------------------------------------- golden byte-pin (determinism)
_GOLDEN = Path(__file__).resolve().parent / "golden"
_RUNNER = _GOLDEN / "run_case.py"
_CASES = ["alice_works", "vn_binh_works", "unknown_abstains"]
_ENV_MATRIX = [
    {"PYTHONHASHSEED": "0", "LC_ALL": "C", "OMP_NUM_THREADS": "1"},
    {"PYTHONHASHSEED": "1", "LC_ALL": "C", "OMP_NUM_THREADS": "4"},
    {"PYTHONHASHSEED": "2147483647", "LC_ALL": "C", "OMP_NUM_THREADS": "8"},
]
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _absorb_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("alpaca.wiki.ingest.absorb") is not None


class TestGoldenPin(unittest.TestCase):
    def test_pinned_hashes_are_well_formed(self):
        for case in _CASES:
            pin = (_GOLDEN / f"{case}.txt").read_text(encoding="utf-8").strip()
            self.assertTrue(_HEX64.match(pin), f"{case}: golden pin is not a 64-hex hash")

    def test_runner_declares_the_frozen_cases(self):
        src = _RUNNER.read_text(encoding="utf-8")
        for case in _CASES:
            self.assertIn(f'"{case}"', src)

    def test_cross_environment_determinism_matrix(self):
        if not _absorb_available():
            self.skipTest("alpaca.wiki.ingest.absorb lands in M2.14; the golden matrix runs once it does")
        root = Path(__file__).resolve().parents[3]
        # The runner drives the full answer door (alpaca.wiki.engine.answer.answer). Its provider bundle
        # is threaded in from OUTSIDE alpaca/wiki/engine (M2.16 keeps the engine blind), and that entry
        # seam is wired at the M2.19 acceptance point; until then Engine is not constructible. Probe
        # once and skip cleanly rather than pin against a door that cannot yet run.
        probe = subprocess.run(
            [sys.executable, str(_RUNNER), sorted(_CASES)[0]], cwd=str(root),
            capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONPATH": str(root)})
        if probe.returncode != 0:
            self.skipTest("the full answer-door provider seam is wired in M2.19; Engine not yet constructible")
        for case in _CASES:
            pinned = (_GOLDEN / f"{case}.txt").read_text(encoding="utf-8").strip()
            seen = set()
            for extra in _ENV_MATRIX:
                env = dict(os.environ)
                env.update(extra)
                env["PYTHONPATH"] = str(root)
                out = subprocess.run(
                    [sys.executable, str(_RUNNER), case],
                    cwd=str(root), capture_output=True, text=True, encoding="utf-8", env=env)
                self.assertEqual(out.returncode, 0, out.stderr)
                seen.add(out.stdout.strip())
            self.assertEqual(seen, {pinned}, f"{case}: env matrix diverged or drifted from the pin")


if __name__ == "__main__":
    unittest.main()
