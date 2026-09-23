"""M4.15 proof (boot-check) - the composed preflight folds the whole fleet into one verdict.

`boot_check.run(root, full)` composes, from the CENSUS (never a hand-kept list), every
selftest-bearing instrument, and under `--full` the wiki lint, the wiki mutation oracle and the
pytest suite (LAST). The row list matches the census, so a new instrument is picked up without
editing boot-check; the fold is the worst code (BLOCKED > FAIL > PAUSED > PASS).

The release door is the other half of the Done-when: with no owner-countersign row whose `ref` is
`render-neutralisation` (M4.9) on the record it exits 3 naming the missing ref, and folds its other
links normally once that row is present; it reads ONLY that ref and ignores `manual-phone-read`.

RE-ENTRANCY: `--full` runs the pytest suite via `regression_suite`, and this module runs inside
pytest. Every execution here either sets `ALPACA_REGRESSION_SUITE_ACTIVE` (so the suite short-circuits
and never re-spawns pytest) or runs boot-check over a THROWAWAY synthetic subtree; the live suite is
never re-entered.
"""
import importlib.util
import os
import shutil

import pytest

from alpaca import cli, clock, db, util
from alpaca.gates import instrument_census, verdict as vc
from alpaca.phase import doors
from alpaca.tests.conftest import REPO

SETUP = os.path.join(REPO, "setup")


def _load_boot_check():
    spec = importlib.util.spec_from_file_location("alpaca_boot_check", os.path.join(SETUP, "boot-check.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BC = _load_boot_check()

_REGRESSION = "alpaca.gates.regression_suite"


def _has_selftest(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return "--selftest" in fh.read()
    except OSError:
        return False


# ------------------------------------------------------ the row list is composed from the census
def test_plan_matches_the_census():
    """Every selftest-bearing census instrument (bar the regression instrument, which is the
    dedicated last row) is a boot-check row, derived FROM the census, so the two cannot drift."""
    expected = [r["rel"] for r in instrument_census.census(REPO)
                if r["module"] != _REGRESSION and _has_selftest(os.path.join(REPO, r["rel"]))]
    plan = BC.plan(REPO, full=True)
    instrument_rows = [n for n in plan if n.startswith("alpaca/")]
    assert instrument_rows == expected, "the instrument rows are exactly the census population"
    assert expected, "the census population is non-empty"


def test_full_plan_appends_wiki_lint_oracle_and_the_suite_last():
    plan = BC.plan(REPO, full=True)
    assert "wiki-lint" in plan
    assert "wiki-mutation-oracle" in plan
    assert plan[-1] == "regression-suite", "the regression suite is the LAST row"
    # core omits the three heavy composed rows.
    core = BC.plan(REPO, full=False)
    assert "regression-suite" not in core and "wiki-mutation-oracle" not in core


def test_a_new_instrument_is_picked_up_without_editing_boot_check(tmp_path):
    """Drop a fresh selftest-bearing instrument into alpaca/gates of a throwaway tree; the census, and
    so boot-check's plan, names it with no edit to boot-check."""
    root = str(tmp_path / "proj")
    _mk_min_tree(root)
    before = BC.plan(root, full=False)
    assert "alpaca/gates/alpha_gate.py" in before and "alpaca/gates/newcomer_gate.py" not in before
    _write_instrument(root, "newcomer_gate.py", "newcomer", ok=True)
    after = BC.plan(root, full=False)
    assert "alpaca/gates/newcomer_gate.py" in after, "a new instrument enters the plan via the census"


# ------------------------------------------------------------------ the fold is the worst code
def test_run_core_folds_to_pass_then_fail(tmp_path):
    root = str(tmp_path / "proj")
    _mk_min_tree(root)
    guarded = _guard_env()
    try:
        assert BC.run(root, full=False) == vc.PASS, "two green instruments fold to PASS"
        _write_instrument(root, "beta_gate.py", "beta", ok=False)
        assert BC.run(root, full=False) == vc.FAIL, "one failing selftest folds the whole run to FAIL"
    finally:
        _restore_env(guarded)


def test_run_full_composes_and_folds_to_one_verdict(tmp_path):
    """A throwaway tree with two green instruments, a clean wiki lint, a clean mutation-oracle stub
    and the (re-entrancy-guarded) suite folds to PASS; a HOLLOW oracle folds the whole run to FAIL."""
    root = str(tmp_path / "proj")
    _mk_min_tree(root)
    _write_oracle(root, flipped=1, hollow=0, total=1)
    _write_test(root, "def test_ok():\n    assert True\n")
    guarded = _guard_env()      # the suite short-circuits (re-entrant), never re-spawns pytest
    try:
        assert BC.run(root, full=True) == vc.PASS, "the whole composed run folds to PASS"
        _write_oracle(root, flipped=0, hollow=1, total=1)
        assert BC.run(root, full=True) == vc.FAIL, "a hollow mutation folds the whole run to FAIL"
    finally:
        _restore_env(guarded)


def test_regression_row_is_reentrancy_guarded(tmp_path):
    root = str(tmp_path / "proj")
    _mk_min_tree(root)
    _write_test(root, "def test_ok():\n    assert True\n")
    guarded = _guard_env()
    try:
        name, code = BC.regression_row(root)
        assert name == "regression-suite" and code == vc.PASS, \
            "with the guard set the suite short-circuits PASS without re-spawning pytest"
    finally:
        _restore_env(guarded)


def test_empty_population_folds_to_blocked(tmp_path):
    """A tree with no instrument at all: a preflight over nothing is BLOCKED, never a vacuous PASS."""
    root = str(tmp_path / "bare")
    os.makedirs(os.path.join(root, "alpaca", "gates"))
    open(os.path.join(root, "alpaca", "__init__.py"), "w").close()
    open(os.path.join(root, "alpaca", "gates", "__init__.py"), "w").close()
    with open(os.path.join(root, "ALPACA-MANIFEST"), "w", encoding="utf-8") as fh:
        fh.write("[mechanism]\nalpaca/\n[memory]\n.alpaca/\n")
    assert BC.run(root, full=False) == vc.BLOCKED


# ---------------------------------------------- the release door consumes the render-neutralisation
@pytest.fixture
def proj(project):
    util.set_clock(clock.FixedClock("2026-04-01T00:00:00+00:00", step=0))
    cli.main(["init"])
    cli.main(["onboard", "--name", "seed", "--who", "v:owner", "--what", "w"])
    yield project
    util.set_clock(None)


def test_release_door_pauses_and_names_the_missing_ref(proj):
    conn = db.connect(proj)
    code = doors.release_door(conn, "op-001", root=proj)
    assert code == vc.PAUSED, "the release door exits 3 with no render-neutralisation countersign"
    ev = db.events(conn, kind="release-door-closed")[-1]
    assert ev["data"]["missing_ref"] == "render-neutralisation", "the door names the missing ref"
    assert ev["data"]["reason_code"] == doors.R_COUNTERSIGN_MISSING


def test_release_door_folds_its_other_links_once_the_row_is_present(proj):
    conn = db.connect(proj)
    doors.record_countersign(conn, "render-neutralisation", ["RESUME.md", "analytics/index.html"],
                             pointer="journal:owner read both surfaces", root=proj)
    # with the countersign present the door returns the fold of its OTHER links.
    assert doors.release_door(conn, "op-001", root=proj,
                              extra_links=[("packaging", vc.PASS)]) == vc.PASS
    assert doors.release_door(conn, "op-001", root=proj,
                              extra_links=[("packaging", vc.FAIL)]) == vc.FAIL


def test_release_door_reads_only_render_neutralisation(proj):
    conn = db.connect(proj)
    # the OTHER countersign (manual-phone-read, M4.16) does not discharge this door.
    doors.record_countersign(conn, "manual-phone-read", ["docs/manual.html"],
                             pointer="journal:phone read", root=proj)
    assert doors.release_door(conn, "op-001", root=proj) == vc.PAUSED, \
        "manual-phone-read never stands in for render-neutralisation"


# --------------------------------------------------------------------------- synthetic-tree helpers
_INSTRUMENT_OK = ('import sys\n'
                  'if "--selftest" in sys.argv:\n'
                  '    print("GATE %s-selftest: PASS")\n'
                  '    sys.exit(0)\n'
                  'sys.exit(0)\n')
_INSTRUMENT_BAD = ('import sys\n'
                   'if "--selftest" in sys.argv:\n'
                   '    print("GATE %s-selftest: FAIL")\n'
                   '    sys.exit(1)\n'
                   'sys.exit(0)\n')


def _write_instrument(root, name, label, ok=True):
    body = (_INSTRUMENT_OK if ok else _INSTRUMENT_BAD) % label
    with open(os.path.join(root, "alpaca", "gates", name), "w", encoding="utf-8") as fh:
        fh.write(body)


_ORACLE_SEQ = [0]


def _write_oracle(root, *, flipped, hollow, total):
    d = os.path.join(root, "alpaca", "tests", "wiki")
    os.makedirs(d, exist_ok=True)
    # A unique, variable-length marker per write so a rewrite in the same second changes the file
    # SIZE too: `spec_from_file_location` validates cached bytecode by (mtime, size), and two rapid
    # rewrites of equal size within one mtime tick would otherwise load the stale first version.
    _ORACLE_SEQ[0] += 1
    marker = "# variant " + "v" * _ORACLE_SEQ[0]
    with open(os.path.join(d, "mutation_oracle.py"), "w", encoding="utf-8") as fh:
        fh.write("%s\ndef run(only=None):\n"
                 "    return {%r: %d, %r: %d, %r: %d, %r: %d, %r: []}\n"
                 % (marker, "flipped", flipped, "hollow", hollow, "malformed", 0,
                    "total", total, "results"))


def _write_test(root, body):
    d = os.path.join(root, "alpaca", "tests")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "test_probe.py"), "w", encoding="utf-8") as fh:
        fh.write(body)


def _mk_min_tree(root):
    os.makedirs(os.path.join(root, "alpaca", "gates"))
    open(os.path.join(root, "alpaca", "__init__.py"), "w").close()
    open(os.path.join(root, "alpaca", "gates", "__init__.py"), "w").close()
    with open(os.path.join(root, "ALPACA-MANIFEST"), "w", encoding="utf-8") as fh:
        fh.write("[mechanism]\nalpaca/\n[memory]\n.alpaca/\n")
    _write_instrument(root, "alpha_gate.py", "alpha", ok=True)
    _write_instrument(root, "beta_gate.py", "beta", ok=True)


def _guard_env():
    prior = os.environ.get("ALPACA_REGRESSION_SUITE_ACTIVE")
    os.environ["ALPACA_REGRESSION_SUITE_ACTIVE"] = "1"
    return prior


def _restore_env(prior):
    if prior is None:
        os.environ.pop("ALPACA_REGRESSION_SUITE_ACTIVE", None)
    else:
        os.environ["ALPACA_REGRESSION_SUITE_ACTIVE"] = prior
