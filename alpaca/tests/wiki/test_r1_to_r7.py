"""M2.13 (Step 1): the R1-R7 coverage roll-up.

The upstream Rune-2 README (section "R1-R7") states the property set the vendored engine must hold:

  R1 one non-bypassable door           - engine.answer, the as-of filter, import-guarded store reads
  R2 content-derived oracle that abstains - a 5-layer DAG, never a lone LLM judge
  R3 write-time citation capture       - source_block_id NOT NULL, answer->claim->source in one hop
  R4 deterministic middle              - determinism.py owns every hash, number and ordering
  R5 write-time absorption             - set-returning cannot-merge resolver + incremental upsert
  R6 bitemporal                        - four axes; correction vs world-change never conflated
  R7 proportionality                   - one SQLite file, no server, pure-stdlib runnable

The property tests were imported across M2.8-M2.12, adapted to the vendored alpaca.wiki surface and
consolidated under new names (each ported module's docstring cites the upstream test_*.py it carries).
This roll-up NAMES, for each of R1 to R7, the ported test modules that discharge it, and BLOCKS if any
requirement has zero. That is the "no imported module lands untested" gate at the requirement level.

Read first-hand from the upstream tree: R1 is named in six files (test_r1_import_guard,
test_r1_one_door_law, test_r1_domain_compartment, test_r1_ladder_modes, test_r1_north_star_successor,
test_r1_unconstructable), R2 in test_r2_oracle, R4 in test_r4_determinism, R6 in test_r6_bitemporal,
R7 in test_r7_proportionality. THERE IS NO test_r3_*.py AND NO test_r5_*.py upstream: R3 is carried by
the schema NOT NULL constraint plus test_hash_chain and test_pkt2_prov (both folded into the ported
test_assertion_log), and R5 by test_resolve_two_dats and test_pkt2_km (pkt2_km folded into
test_assertion_log; resolve_two_dats lands with the M2.14 absorb port). The map records that carrier
per requirement so the two schema/absorb-carried requirements are not mistaken for gaps.
"""
import os
import unittest
from pathlib import Path

_TESTS = Path(__file__).resolve().parent

# requirement -> (list of ported tests/wiki module stems that discharge it, carrier note)
R_COVERAGE: dict[str, tuple[list[str], str]] = {
    "R1": (
        ["test_one_door_law", "test_one_write_door", "test_compartment",
         "test_retrieval", "test_authority_conflict", "test_abstain_or_answer"],
        "import guard + one read/write door + domain compartment + ladder modes + north-star "
        "successor + unconstructable Answer",
    ),
    "R2": (
        ["test_abstain_or_answer", "test_authority_conflict", "test_retrieval", "test_fts_fallback"],
        "the 5-layer oracle DAG abstains; authority arbitration never mints a groundless verdict",
    ),
    "R3": (
        ["test_assertion_log", "test_write_gates"],
        "NO test_r3_*.py upstream: carried by the schema source_block_id NOT NULL constraint plus "
        "test_hash_chain and test_pkt2_prov, both folded into test_assertion_log",
    ),
    "R4": (
        ["test_assertion_log", "test_bitemporal_asof", "test_authority_conflict"],
        "the deterministic middle: the hash chain (R3/R4), the vendored alpaca.wiki.determinism owning "
        "canonical_json/rrf_fuse/stable_rank/to_band, and its use in as-of ordering",
    ),
    "R5": (
        ["test_write_gates", "test_assertion_log"],
        "NO test_r5_*.py upstream: carried by test_resolve_two_dats (lands with the M2.14 absorb "
        "port) and test_pkt2_km (folded into test_assertion_log); merge/keystone hardening in "
        "test_write_gates guards the incremental upsert",
    ),
    "R6": (
        ["test_bitemporal_asof"],
        "four axes on edges/nodes/blocks; a correction and a world-change move different time axes",
    ),
    "R7": (
        ["test_assertion_log", "test_import_coverage"],
        "proportionality: the single-file vault-containment barrier (db_containment, folded into "
        "test_assertion_log) plus the closed minimal vendoring surface (no cli/onboard/surface/"
        "apply/eval second door), the pure-stdlib no-server shape",
    ),
}


class TestR1toR7Rollup(unittest.TestCase):
    def test_no_requirement_has_zero_ported_modules(self):
        for req in ("R1", "R2", "R3", "R4", "R5", "R6", "R7"):
            self.assertIn(req, R_COVERAGE, msg=f"{req} is not mapped at all")
            modules, _note = R_COVERAGE[req]
            self.assertGreater(len(modules), 0, msg=f"{req} has zero ported modules (BLOCK)")

    def test_every_named_module_exists_on_disk(self):
        for req, (modules, _note) in R_COVERAGE.items():
            for mod in modules:
                path = _TESTS / f"{mod}.py"
                self.assertTrue(path.exists(),
                                msg=f"{req} names {mod}.py which is not present in tests/wiki/")

    def test_all_seven_requirements_are_covered(self):
        self.assertEqual(set(R_COVERAGE), {"R1", "R2", "R3", "R4", "R5", "R6", "R7"})

    def test_every_named_module_is_a_real_test_module(self):
        # teeth: a named module must actually define at least one unittest TestCase / test function,
        # so a requirement cannot be "covered" by an empty or non-test file.
        for req, (modules, _note) in R_COVERAGE.items():
            for mod in modules:
                src = (_TESTS / f"{mod}.py").read_text(encoding="utf-8")
                self.assertIn("def test_", src,
                              msg=f"{req}: {mod}.py defines no test_ function")


if __name__ == "__main__":
    unittest.main()
