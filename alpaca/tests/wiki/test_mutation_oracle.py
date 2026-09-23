"""M2.13 PROOF: the mutation oracle is executed over its checked-in mutation set, and it has teeth.

This is the task's Proof line (local:tests/wiki/test_mutation_oracle.py). It discharges Done-when's
second and part of its third clause: every registered wiki mechanism carries a checked-in source
mutation that flips a named unittest to an assertion FAILURE with zero hollow and zero malformed.

The honest meaning (Step 6): a PASS here certifies each named test is NOT HOLLOW - a specific,
checked-in break of its mechanism turns it red. A pass NEVER means the mechanism is verified, sound
or proven; it means the test has teeth. To keep THIS module itself non-hollow, it also runs a
deliberately inert mutation and asserts the oracle reports it HOLLOW (the meta negative-control): if
the oracle could not tell a real flip from a no-op, this proof would be worthless.

Step 4 debt: the oracle drives stdlib `python3 -m unittest`, which runs only unittest.TestCase
methods. The M1 instruments under alpaca/gates/ are covered by pytest-function tests (with pytest
fixtures), which `unittest` cannot load, so no M1 instrument carries a mutation in this set. Each is
listed as an explicit debt line below; extending the oracle to a pytest runner is left to a later
task. The wiki gates the oracle CAN reach (lint.RULES, the glossary lint, the one-read-door guard)
are all held to the bar here.
"""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import mutation_oracle  # noqa: E402

_ROOT = Path(__file__).resolve().parents[3]

# Step 4 debt: M1 instruments (alpaca/gates/*.py) with no mutation in this set, because their tests are
# pytest-native and the unittest-based oracle cannot run them. Concrete, file-checked, not vague.
_M1_INSTRUMENT_DEBT = (
    "alpaca/gates/verdict.py", "alpaca/gates/contract.py", "alpaca/gates/rc_conformance.py",
    "alpaca/gates/workspace_guard.py", "alpaca/gates/literal_guard.py", "alpaca/gates/honest_tag_oracle.py",
    "alpaca/gates/monotonicity.py", "alpaca/gates/fuzz_gate.py", "alpaca/gates/chain_check.py",
    "alpaca/gates/structural_conformance.py", "alpaca/gates/record_check.py", "alpaca/gates/quote_check.py",
    "alpaca/gates/wiring_audit.py", "alpaca/gates/instrument_census.py", "alpaca/gates/integration_check.py",
)


class TestMutationOracle(unittest.TestCase):
    def test_every_mutation_flips_with_zero_hollow(self):
        summary = mutation_oracle.run()
        # surface the flipped/hollow/malformed counts for the milestone-close record.
        print(f"\nmutation oracle: {summary['flipped']}/{summary['total']} flipped, "
              f"{summary['hollow']} HOLLOW, {summary['malformed']} MALFORMED")
        detail = {r["mechanism_id"]: r["status"] for r in summary["results"]}
        self.assertGreaterEqual(summary["total"], 16, "the mutation set must not be empty")
        self.assertEqual(summary["malformed"], 0,
                         msg=f"malformed mutation records: {detail}")
        self.assertEqual(summary["hollow"], 0,
                         msg=f"HOLLOW (toothless) tests: {detail}")
        self.assertEqual(summary["flipped"], summary["total"],
                         msg=f"not every mutation flipped: {detail}")

    def test_the_oracle_flags_a_no_op_mutation_as_hollow(self):
        # META negative-control: an inert edit (a docstring word) must NOT flip a passing test, and
        # the oracle MUST classify that as HOLLOW. This is what keeps this proof from being hollow.
        inert = {
            "mechanism_id": "meta.inert-noop",
            "file": "alpaca/wiki/lint.py",
            "find": "executed mechanical lint rules",
            "replace": "executed mechanical lint checks",
            "flips_test":
                "alpaca.tests.wiki.test_lint.TestLintCleanVault.test_registry_holds_at_least_twelve_executed_rules",
            "expect": "fail",
        }
        verdict = mutation_oracle.evaluate(inert)
        self.assertEqual(verdict["status"], "HOLLOW",
                         msg=f"the oracle failed to detect an inert mutation: {verdict}")

    def test_mutations_file_is_well_formed(self):
        mutations = json.loads(
            (_ROOT / "alpaca" / "tests" / "wiki" / "mutations.json").read_text(encoding="utf-8"))
        seen = set()
        for m in mutations:
            for key in ("mechanism_id", "file", "find", "replace", "flips_test"):
                self.assertIn(key, m, msg=f"mutation missing {key}: {m}")
            self.assertNotIn(m["mechanism_id"], seen, "mechanism ids must be unique")
            seen.add(m["mechanism_id"])
            self.assertTrue((_ROOT / m["file"]).exists(), f"target file missing: {m['file']}")

    def test_m1_instrument_debt_is_named_and_real(self):
        # The debt is concrete: every listed instrument is a file that actually exists.
        for rel in _M1_INSTRUMENT_DEBT:
            self.assertTrue((_ROOT / rel).exists(), f"debt line names a missing file: {rel}")


if __name__ == "__main__":
    unittest.main()
