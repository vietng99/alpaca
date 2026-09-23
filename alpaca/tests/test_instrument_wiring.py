"""M1.17 proof - instrument census, wiring audit, integration, regression, literal guard,
structural conformance.

Section 5.3 (spec:303-312): "present != wired". An instrument on disk is a self-description;
whether anything reaches it is a separate fact only a scan of the rest of the tree can settle.
This module drives the Done-when on BOTH the positive and the negative path, on OWN fixtures:

  * instrument_census enumerates every instrument under alpaca/gates/ and alpaca/checklist/ (never
    __init__.py) and lists each instrument's callers, re-derived from file content.
  * wiring_audit FAILs when an instrument has no INDEPENDENT (production) caller, proved on a
    temp orphan instrument WITH and WITHOUT a caller; a test-only or doc-only mention does not
    wire; an empty population BLOCKS (the floor); a fully wired tree PASSes.
  * integration_check drives a real discharge flow across synthesis, verdict rows and the tag
    oracle in a throwaway tempdir, each hop hard-asserted.
  * regression_suite wraps pytest as one instrument over a throwaway subset (never the live
    suite), green -> PASS, red -> FAIL, empty -> BLOCKED, and is re-entrancy guarded.
  * literal_guard reads the forbidden list from project.yaml and style/banned.txt (never code)
    and FAILs bound to file:line on a hit, ignores its allow-marker, BLOCKS on empty population.
  * structural_conformance reads required dirs/files from project.yaml and FAILs bound to the
    missing entry, BLOCKS on an empty declaration.

The live alpaca/gates + alpaca/checklist tree is exercised READ-ONLY (audit/census never write). The
live-tree wiring assertion is deliberately TOLERANT: instruments not yet wired into a door or
CLI are recorded, never hard-failed here (they are wired by boot-check, M4.15).
"""
import os
import sys

import pytest

from alpaca.gates import verdict as vc
from alpaca.gates import instrument_census as census
from alpaca.gates import wiring_audit
from alpaca.gates import integration_check
from alpaca.gates import regression_suite
from alpaca.gates import literal_guard
from alpaca.gates import structural_conformance

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _mk(root, files):
    """Materialise {relpath: text} under root; return root."""
    for rel, content in files.items():
        p = os.path.join(root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content)
    return root


# =========================================================== instrument_census
def test_census_enumerates_every_live_instrument_but_not_init():
    rows = census.census(REPO)
    names = {r["name"] for r in rows}
    # every *.py under alpaca/gates and alpaca/checklist is present, __init__.py is never an instrument.
    assert "__init__.py" not in names
    assert "wiring_audit.py" in names
    assert "instrument_census.py" in names
    assert "verdict_row.py" in names
    # every enumerated instrument carries its dotted module and a callers list.
    for r in rows:
        assert r["module"].startswith("alpaca.gates.") or r["module"].startswith("alpaca.checklist.")
        assert isinstance(r["callers"], list)


def test_census_lists_a_real_production_caller():
    # honest_tag_oracle is imported by alpaca/checklist/verdict_row.py (a production module), so the
    # census must show a caller for it (re-derived from content, not a hand-kept list).
    rows = {r["name"]: r for r in census.census(REPO)}
    oracle = rows["honest_tag_oracle.py"]
    assert any("verdict_row" in c for c in oracle["callers"]), oracle["callers"]


def test_census_empty_population_blocks(tmp_path):
    # a census over a tree with no instrument dirs measures an empty population -> BLOCKED.
    _mk(str(tmp_path), {"alpaca/util.py": "x = 1\n"})
    assert census.check(str(tmp_path)) == vc.BLOCKED


# =========================================================== wiring_audit fixtures
def _tree_with(files):
    return files


def test_wiring_fails_on_an_orphan_instrument(tmp_path):
    # an instrument present with NO independent caller anywhere in the tree.
    root = _mk(str(tmp_path / "orphan"), {
        "alpaca/gates/orphan_gate.py": "def check(root):\n    return 0\n",
        "alpaca/gates/wired_gate.py": "def check(root):\n    return 0\n",
        "alpaca/driver.py": "from alpaca.gates import wired_gate\nwired_gate.check('.')\n",
    })
    res = wiring_audit.audit(root)
    assert res.verdict == vc.FAIL
    assert any(wiring_audit.R_UNCALLED in r and "orphan_gate" in r for r in res.reasons), res.reasons
    # the wired sibling keeps the population non-vacuous, so this is a real FAIL not a BLOCK.
    assert any("wired_gate" in i for i in res.info["wired"])


def test_wiring_passes_when_every_instrument_has_a_production_caller(tmp_path):
    root = _mk(str(tmp_path / "clean"), {
        "alpaca/gates/alpha_gate.py": "def check(root):\n    return 0\n",
        "alpaca/checklist/beta_step.py": "def check(root):\n    return 0\n",
        "alpaca/driver.py": ("from alpaca.gates import alpha_gate\n"
                         "from alpaca.checklist import beta_step\n"
                         "alpha_gate.check('.'); beta_step.check('.')\n"),
    })
    res = wiring_audit.audit(root)
    assert res.verdict == vc.PASS, res.reasons


def test_a_test_only_caller_does_not_wire(tmp_path):
    # the ONLY file naming the instrument lives under tests/: a unit test is not a production
    # caller, so the instrument is still an orphan (this is the exact door/CLI distinction).
    root = _mk(str(tmp_path / "testonly"), {
        "alpaca/gates/lonely_gate.py": "def check(root):\n    return 0\n",
        "alpaca/gates/kept_gate.py": "def check(root):\n    return 0\n",
        "alpaca/driver.py": "from alpaca.gates import kept_gate\nkept_gate.check('.')\n",
        "tests/test_lonely.py": "from alpaca.gates import lonely_gate\n\ndef test_x():\n    lonely_gate.check('.')\n",
    })
    res = wiring_audit.audit(root)
    assert res.verdict == vc.FAIL
    assert any(wiring_audit.R_UNCALLED in r and "lonely_gate" in r for r in res.reasons), res.reasons


def test_wiring_empty_population_blocks(tmp_path):
    root = _mk(str(tmp_path / "empty"), {"alpaca/util.py": "x = 1\n"})
    res = wiring_audit.audit(root)
    assert res.verdict == vc.BLOCKED
    assert any(wiring_audit.R_NO_POP in r for r in res.reasons), res.reasons


def test_wiring_live_tree_is_tolerant_and_records_orphans():
    # READ-ONLY over the live tree: audit must return a verdict-band code and census must list
    # every instrument. Instruments not yet wired into a door/CLI are recorded, not hard-failed.
    res = wiring_audit.audit(REPO)
    assert res.verdict in vc.VERDICT_BAND
    orphans = wiring_audit.orphans(REPO)
    # an orphan record names the instrument and the WIRING-UNCALLED-INSTRUMENT reason.
    for o in orphans:
        assert o["name"].endswith(".py")
        assert o["reason"] == wiring_audit.R_UNCALLED
    # positive live check: an instrument with a KNOWN production caller is reported wired.
    assert "honest_tag_oracle.py" not in {o["name"] for o in orphans}


# =========================================================== integration_check
def test_integration_check_drives_the_discharge_seam_green():
    res = integration_check.audit()
    assert res.verdict == vc.PASS, res.reasons
    # the flow crossed several instruments; every control fired.
    assert res.info["controls"] >= 4
    assert res.info["failures"] == 0


def test_integration_check_check_never_touches_the_passed_root(tmp_path):
    # integration_check authors nothing durable: it builds its own throwaway project and never
    # writes into the root it is handed.
    root = str(tmp_path)
    code = integration_check.check(root)
    assert code == vc.PASS
    assert not os.path.exists(os.path.join(root, ".alpaca")), "the passed root was mutated"


# =========================================================== regression_suite
def _write_test(dir_path, body):
    os.makedirs(dir_path, exist_ok=True)
    with open(os.path.join(dir_path, "test_probe.py"), "w", encoding="utf-8") as fh:
        fh.write(body)
    return dir_path


def test_regression_suite_green_subset_passes(tmp_path):
    d = _write_test(str(tmp_path / "green"), "def test_ok():\n    assert True\n")
    code, summary = regression_suite.run_suite(d)
    assert code == vc.PASS, summary
    assert "passed" in summary


def test_regression_suite_red_subset_fails(tmp_path):
    d = _write_test(str(tmp_path / "red"), "def test_bad():\n    assert False\n")
    code, summary = regression_suite.run_suite(d)
    assert code == vc.FAIL, summary


def test_regression_suite_empty_population_blocks(tmp_path):
    d = str(tmp_path / "none")
    os.makedirs(d, exist_ok=True)
    code, summary = regression_suite.run_suite(d)
    assert code == vc.BLOCKED, summary


def test_regression_suite_is_reentrancy_guarded(tmp_path, monkeypatch):
    # if a regression-suite is already active, a nested run must short-circuit and NEVER spawn a
    # second pytest (the guard that stops boot-check --full recursing into the whole live suite).
    monkeypatch.setenv(regression_suite._ACTIVE_ENV, "1")
    d = _write_test(str(tmp_path / "guarded"), "def test_ok():\n    assert True\n")
    code, summary = regression_suite.run_suite(d)
    assert code == vc.PASS
    assert "re-entrant" in summary


# =========================================================== literal_guard
def test_literal_guard_flags_a_forbidden_literal_bound_to_file_line(tmp_path):
    root = _mk(str(tmp_path / "leak"), {
        "alpaca/gates/leaky_gate.py": "OK = 1\nREF = 'BANNED_TOKEN_XYZ'\n",
    })
    res = literal_guard.scan(root, forbidden=("BANNED_TOKEN_XYZ",))
    assert res.verdict == vc.FAIL
    assert any(literal_guard.R_HIT in r and "leaky_gate.py:2" in r for r in res.reasons), res.reasons


def test_literal_guard_clean_core_passes(tmp_path):
    root = _mk(str(tmp_path / "clean"), {"alpaca/gates/clean_gate.py": "print('generic core')\n"})
    res = literal_guard.scan(root, forbidden=("BANNED_TOKEN_XYZ",))
    assert res.verdict == vc.PASS


def test_literal_guard_allow_marker_scopes_the_opt_out(tmp_path):
    root = _mk(str(tmp_path / "marked"), {
        "alpaca/gates/doc_gate.py": ("# names BANNED_TOKEN_XYZ on purpose  "
                                 + literal_guard.ALLOW_MARKER + "\nprint('ok')\n"),
    })
    res = literal_guard.scan(root, forbidden=("BANNED_TOKEN_XYZ",))
    assert res.verdict == vc.PASS, res.reasons


def test_literal_guard_empty_population_blocks(tmp_path):
    root = _mk(str(tmp_path / "nopy"), {"alpaca/gates/notes.md": "no python here\n"})
    res = literal_guard.scan(root, forbidden=("BANNED_TOKEN_XYZ",))
    assert res.verdict == vc.BLOCKED
    assert any(literal_guard.R_NO_POP in r for r in res.reasons), res.reasons


def test_literal_guard_reads_forbidden_from_project_yaml_and_banned_txt(tmp_path):
    root = str(tmp_path / "cfg")
    _mk(root, {
        "ALPACA-MANIFEST": "alpaca\n",
        "project.yaml": "literal_guard:\n  forbidden:\n    - FROM_YAML_LIT\n",
        "style/banned.txt": "FROM_BANNED_LIT\n",
        "alpaca/gates/g.py": "print('clean')\n",
    })
    forbidden, source = literal_guard.load_forbidden(root)
    assert "FROM_YAML_LIT" in forbidden and "FROM_BANNED_LIT" in forbidden, (forbidden, source)


# =========================================================== structural_conformance
def _proj(tmp_path, yaml_text, extra=None):
    files = {"ALPACA-MANIFEST": "alpaca\n", "project.yaml": yaml_text}
    files.update(extra or {})
    return _mk(str(tmp_path), files)


def test_structural_conformance_missing_dir_fails(tmp_path):
    root = _proj(tmp_path,
                 "structure:\n  required_dirs:\n    - alpaca\n    - contracts\n"
                 "  required_files:\n    - ALPACA-MANIFEST\n",
                 {"alpaca/x.py": "x = 1\n"})
    reqs, block = structural_conformance.load_requirements(root)
    assert block is None, block
    res = structural_conformance.check_conformance(reqs, root)
    assert res.verdict == vc.FAIL
    assert any(structural_conformance.R_MISSING_DIR in r and "contracts" in r
               for r in res.reasons), res.reasons


def test_structural_conformance_all_present_passes(tmp_path):
    root = _proj(tmp_path,
                 "structure:\n  required_dirs:\n    - alpaca\n"
                 "  required_files:\n    - ALPACA-MANIFEST\n",
                 {"alpaca/x.py": "x = 1\n"})
    reqs, block = structural_conformance.load_requirements(root)
    assert block is None
    res = structural_conformance.check_conformance(reqs, root)
    assert res.verdict == vc.PASS, res.reasons


def test_structural_conformance_missing_file_fails(tmp_path):
    # alpaca/ is present (the witness that keeps this a FAIL bound to the missing file, not the
    # CONFORMANCE-NO-WITNESS floor that fires when NOTHING is satisfied).
    root = _proj(tmp_path,
                 "structure:\n  required_dirs:\n    - alpaca\n"
                 "  required_files:\n    - CHARTER.md\n",
                 {"alpaca/x.py": "x = 1\n"})
    reqs, block = structural_conformance.load_requirements(root)
    assert block is None
    res = structural_conformance.check_conformance(reqs, root)
    assert res.verdict == vc.FAIL
    assert any(structural_conformance.R_MISSING_FILE in r and "CHARTER.md" in r
               for r in res.reasons), res.reasons


def test_structural_conformance_no_witness_blocks(tmp_path):
    # a declaration where NOTHING is satisfied cannot tell "the tree is wrong" from "the wrong
    # tree was compared": the floor BLOCKs, never a FAIL that looks like a graded result.
    root = _proj(tmp_path, "structure:\n  required_dirs:\n    - absent-one\n    - absent-two\n")
    reqs, block = structural_conformance.load_requirements(root)
    assert block is None
    res = structural_conformance.check_conformance(reqs, root)
    assert res.verdict == vc.BLOCKED
    assert any(structural_conformance.R_NO_WITNESS in r for r in res.reasons), res.reasons


def test_structural_conformance_empty_declaration_blocks(tmp_path):
    root = _proj(tmp_path, "structure:\n  required_dirs: []\n  required_files: []\n")
    reqs, block = structural_conformance.load_requirements(root)
    assert reqs is None
    assert block[0] == structural_conformance.R_REQ_EMPTY


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
