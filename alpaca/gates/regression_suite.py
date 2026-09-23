"""regression_suite.py - the pytest suite wrapped as ONE instrument (M1.17 Step 6).

Ported from the earlier harness gates/regression_suite.py and re-based onto the Alpaca verdict contract. A
test suite that nothing in the preflight runs is "verified once, on someone's laptop" rather than
re-derived by a runner. This wraps `python3 -m pytest` as a single instrument so boot-check
(M4.15) can make it the LAST row of `boot-check --full` - the fast per-instrument selftests
first, then this slower whole-suite sweep.

TWO-CHANNEL, verdict never laundered (mapped through alpaca.gates.verdict, never restated):
  * pytest rc 0            -> PASS  (`<n> passed`)
  * pytest rc 1            -> FAIL  (tests ran and at least one failed - a measured verdict)
  * pytest rc 5            -> BLOCKED (no tests collected; an empty population is never a pass)
  * pytest not importable  -> BLOCKED (the suite could not run, so no verdict was reached)
  * any other rc (2/3/4)   -> BLOCKED (pytest did not complete a run)

RE-ENTRANCY GUARD (critical). If a test ever runs the whole suite (via boot-check --full, which
runs THIS instrument, which runs the suite, which would run that test again), the recursion is
unbounded. So before invoking pytest we set `ALPACA_REGRESSION_SUITE_ACTIVE=1` in the child env, and
the FIRST thing this instrument does is short-circuit (PASS, "re-entrant skip") when that variable
is already set. `run_suite` also takes an explicit tests dir, so a caller (a test, boot-check's
own selftest) can point it at a THROWAWAY subset and never re-enter the live suite.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

from alpaca.gates import verdict as vc

INSTRUMENT = "regression-suite"

#: the re-entrancy guard variable (Alpaca-namespaced). Set in the child env before pytest; a set
#: value on entry means we are already inside a regression-suite run and must not recurse.
_ACTIVE_ENV = "ALPACA_REGRESSION_SUITE_ACTIVE"

#: pytest exit codes: 0 passed, 1 failed, 2 interrupted, 3 internal error, 4 usage, 5 no tests.
_PYTEST_NO_TESTS = 5

_PASSED_RE = re.compile(r"(\d+)\s+passed")
_PYTEST_MISSING_RE = re.compile(r"No module named pytest", re.I)


def _passed_count(out):
    m = None
    for m in _PASSED_RE.finditer(out or ""):
        pass
    return m.group(1) if m else "?"


def run_suite(tests_dir, root=None):
    """Run pytest over `tests_dir` as a subprocess and return (verdict_code, summary). The dir is
    an explicit subset, never the live suite, so a caller inside a test never re-enters the whole
    tree. Re-entrancy guarded: a nested run short-circuits PASS without spawning a second pytest.

    verdict_code is a alpaca.gates.verdict band code (PASS / FAIL / BLOCKED)."""
    if os.environ.get(_ACTIVE_ENV):
        return vc.PASS, ("regression-suite re-entrant skip (%s is set; a nested run does not "
                         "re-spawn pytest)" % _ACTIVE_ENV)

    tests_dir = os.path.abspath(tests_dir)
    child_env = dict(os.environ)
    child_env[_ACTIVE_ENV] = "1"
    cwd = tests_dir if os.path.isdir(tests_dir) else (root or os.path.dirname(tests_dir))
    cmd = [sys.executable, "-m", "pytest", tests_dir, "-q", "-p", "no:cacheprovider"]
    try:
        completed = subprocess.run(
            cmd, cwd=cwd, env=child_env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="backslashreplace")
    except (OSError, ValueError) as e:
        return vc.BLOCKED, "could not invoke pytest (%s); no verdict about the suite was reached" % e

    out = completed.stdout or ""
    rc = completed.returncode
    if _PYTEST_MISSING_RE.search(out):
        return vc.BLOCKED, ("pytest is not importable in this interpreter (%s); the suite could "
                            "not run, so no verdict was reached" % sys.executable)
    if rc == 0:
        return vc.PASS, "pytest green (%s passed)" % _passed_count(out)
    if rc == 1:
        return vc.FAIL, "pytest reported failures (exit 1)"
    if rc == _PYTEST_NO_TESTS:
        return vc.BLOCKED, ("pytest collected no tests (exit 5); an empty measured population is "
                            "BLOCKED, never a pass")
    return vc.BLOCKED, "pytest did not complete a run (exit %d); no verdict about the suite" % rc


def check(root) -> int:
    """The uniform instrument entry: run `root/tests` as the suite. Re-entrancy guarded, so a
    nested boot-check --full does not re-spawn the whole suite from inside itself."""
    return run_suite(os.path.join(root, "tests"), root=root)[0]


# ------------------------------------------------------------------- M2.13 wiki mutation oracle
# Additive hook (consumed by boot-check, M4.15): drive the vendored alpaca.wiki mutation oracle over its
# checked-in mutation set and map its flipped/hollow/malformed counts to the verdict contract. The
# oracle lives with the property suite at tests/wiki/mutation_oracle.py (interface: run(root)); a
# PASS means every registered wiki mechanism has a source mutation that flips a named test with zero
# hollow, never that the mechanism is proven. The whole-suite sweep already runs the property suite;
# this is the narrow, directly-callable entry that boot-check names.
def run_wiki_mutation_oracle(root):
    """Return (verdict_code, summary). Every mutation flips with zero hollow/malformed -> PASS; a
    hollow (toothless) test -> FAIL; the oracle could not run at all -> BLOCKED."""
    import importlib.util

    oracle_path = os.path.join(root, "tests", "wiki", "mutation_oracle.py")
    if not os.path.isfile(oracle_path):
        return vc.BLOCKED, "no wiki mutation oracle at tests/wiki/mutation_oracle.py"
    try:
        spec = importlib.util.spec_from_file_location("alpaca_wiki_mutation_oracle", oracle_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        summary = mod.run()
    except Exception as e:  # the oracle could not complete a run, so no verdict was reached
        return vc.BLOCKED, "wiki mutation oracle could not run (%s)" % e
    if summary["total"] == 0:
        return vc.BLOCKED, "wiki mutation oracle has an empty mutation set"
    if summary["hollow"]:
        return vc.FAIL, "wiki mutation oracle found %d HOLLOW test(s)" % summary["hollow"]
    if summary["malformed"] or summary["flipped"] != summary["total"]:
        return vc.BLOCKED, ("wiki mutation oracle incomplete (flipped=%d/%d, malformed=%d)"
                            % (summary["flipped"], summary["total"], summary["malformed"]))
    return vc.PASS, "wiki mutation oracle: %d/%d flipped, 0 hollow" % (
        summary["flipped"], summary["total"])


# ------------------------------------------------------------------------- CLI boundary
def main(argv=None) -> int:
    ap = vc.make_parser(
        name=INSTRUMENT,
        description="Run the pytest suite as one composed instrument: rc 0 -> PASS, rc 1 -> FAIL, "
                    "a suite that cannot run -> BLOCKED (never a false PASS).")
    ap.add_argument("--root", default=None, help="tree whose tests/ to run (default: discovered)")
    ap.add_argument("--tests", default=None, help="explicit tests dir/subset to run")
    ap.add_argument("--selftest", action="store_true",
                    help="prove the mapping on a throwaway green/red/empty subset")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    from alpaca import paths
    root = a.root or paths.root()
    tests_dir = a.tests or os.path.join(root, "tests")
    code, summary = run_suite(tests_dir, root=root)
    return vc.emit_verdict(INSTRUMENT, code, summary)


def selftest() -> int:
    """Prove the pytest->verdict mapping on THROWAWAY subsets (never the live suite): a green
    subset PASSes, a red one FAILs, an empty one BLOCKs, and the re-entrancy guard short-circuits."""
    import shutil
    import tempfile

    base = tempfile.mkdtemp(prefix="regression-suite-selftest-")
    rows = []
    failures = 0

    def write(sub, body):
        d = os.path.join(base, sub)
        os.makedirs(d, exist_ok=True)
        if body is not None:
            with open(os.path.join(d, "test_probe.py"), "w", encoding="utf-8") as fh:
                fh.write(body)
        return d

    def control(cid, desc, got, want):
        nonlocal failures
        ok = got == want
        if not ok:
            failures += 1
        rows.append((cid, desc, "FIRED" if ok else "DID-NOT-FIRE",
                     "got=%s want=%s" % (vc.name_of(got), vc.name_of(want))))

    try:
        control("R-1", "a green subset -> PASS",
                run_suite(write("green", "def test_ok():\n    assert True\n"))[0], vc.PASS)
        control("R-2", "a red subset -> FAIL",
                run_suite(write("red", "def test_bad():\n    assert False\n"))[0], vc.FAIL)
        control("R-3", "an empty subset -> BLOCKED",
                run_suite(write("empty", None))[0], vc.BLOCKED)

        # re-entrancy: with the guard set, a nested run short-circuits PASS and spawns nothing.
        os.environ[_ACTIVE_ENV] = "1"
        try:
            code, summary = run_suite(write("guard", "def test_ok():\n    assert True\n"))
        finally:
            os.environ.pop(_ACTIVE_ENV, None)
        ok = code == vc.PASS and "re-entrant" in summary
        if not ok:
            failures += 1
        rows.append(("R-4", "the re-entrancy guard short-circuits without re-spawning pytest",
                     "FIRED" if ok else "DID-NOT-FIRE", summary[:48]))
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print("CONTROL TABLE - %s" % INSTRUMENT)
    for cid, desc, state, obs in rows:
        print("  %-5s %-56s %-12s %s" % (cid, desc, state, obs))
    if failures:
        return vc.emit_verdict(INSTRUMENT + "-selftest", vc.FAIL,
                               "%d control(s) did not fire" % failures)
    return vc.emit_verdict(INSTRUMENT + "-selftest", vc.PASS, "every control fired")


if __name__ == "__main__":
    sys.exit(main())
