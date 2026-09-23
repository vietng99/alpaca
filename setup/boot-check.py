#!/usr/bin/env python3
"""boot-check.py -- the composed preflight that folds the whole instrument fleet into one verdict.

Ported from the earlier harness setup/boot-check.py and re-based onto Alpaca. The predecessor carried a
hand-maintained CHECKS_FULL list that had to be edited whenever an instrument landed; here the
population is COMPOSED FROM THE CENSUS (`alpaca.gates.instrument_census`), so a new instrument under
`alpaca/gates/` or `alpaca/checklist/` is picked up without editing this file, and the row list can never
drift from what the census reports.

`boot_check.run(root, full)` folds, with the verdict contract's `worst()` (BLOCKED > FAIL > PAUSED
> PASS):

  * every selftest-bearing instrument the census enumerates (each run by its own `--selftest`,
    two-channel: exit 0 AND its `GATE <name>-selftest: PASS` line), and, under `--full`:
  * the wiki lint (M2.13) over a clean store;
  * the wiki mutation oracle (M2.13) over its checked-in mutation set;
  * the pytest suite as ONE instrument (regression_suite, M2.13) -- the LAST row.

RE-ENTRANCY (critical). `--full` runs the pytest suite via `regression_suite`, and a test may run
`--full`. `regression_suite` carries the `ALPACA_REGRESSION_SUITE_ACTIVE` guard: a nested run
short-circuits (PASS, "re-entrant skip") and spawns no second pytest, so a boot-check run from
inside pytest never recurses into the live suite. The regression suite is deliberately LAST: the
fast per-instrument selftests run first, then the slower whole-suite sweep.

A `--selftest` is a statement about an INSTRUMENT, never a verdict about a subject store. In Alpaca
every instrument encodes a selftest PASS uniformly (exit 0 AND the `GATE <name>-selftest: PASS`
line), so both channels must agree before a row is counted green.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

MANIFEST_NAME = "ALPACA-MANIFEST"

#: the regression instrument, composed explicitly as the LAST row (running the live suite), so it
#: is never also run as a plain selftest row.
_REGRESSION_MODULE = "alpaca.gates.regression_suite"

_PASS_LINE = re.compile(r"GATE\s+\S*[-/]selftest:\s+PASS")


def _discover_root(start=None):
    cur = os.path.abspath(start or os.path.dirname(os.path.abspath(__file__)))
    while True:
        if os.path.isfile(os.path.join(cur, MANIFEST_NAME)):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            raise SystemExit("boot-check: no %s above %s" % (MANIFEST_NAME, start or os.getcwd()))
        cur = parent


# The live harness package supplies the census, the verdict contract and the regression wrapper.
# Bootstrap sys.path off the discovered root so `import alpaca...` resolves regardless of cwd.
_ROOT_FOR_IMPORT = _discover_root()
if _ROOT_FOR_IMPORT not in sys.path:
    sys.path.insert(0, _ROOT_FOR_IMPORT)

from alpaca.gates import contract, instrument_census, regression_suite, verdict as vc  # noqa: E402


def _has_selftest(abspath):
    try:
        with open(abspath, encoding="utf-8", errors="replace") as fh:
            return "--selftest" in fh.read()
    except OSError:
        return False


def selftest_instruments(root):
    """The census population, filtered to the selftest-bearing instruments this preflight runs,
    with the regression instrument pulled out (it is composed as the dedicated last row). Derived
    from the census, so a new instrument is picked up without editing this file."""
    out = []
    for r in instrument_census.census(root):
        if r["module"] == _REGRESSION_MODULE:
            continue
        if _has_selftest(os.path.join(root, r["rel"])):
            out.append(r)
    return out


def _run_instrument(abspath, root):
    """Run one instrument's `--selftest` by absolute path (PYTHONPATH pinned to root so the target
    tree's own `alpaca` resolves). Two-channel: exit 0 AND a `GATE <name>-selftest: PASS` line -> PASS;
    exit 1 -> FAIL; anything else, or a green exit with no agreeing line -> BLOCKED."""
    try:
        proc = subprocess.run(
            [sys.executable, abspath, "--selftest"], cwd=root,
            env={**os.environ, "PYTHONPATH": root, "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="backslashreplace")
    except (OSError, ValueError):
        return vc.BLOCKED
    out = proc.stdout or ""
    rc = proc.returncode
    line_ok = _PASS_LINE.search(out) is not None
    if rc == vc.PASS and line_ok:
        return vc.PASS
    if rc == vc.FAIL:
        return vc.FAIL
    if rc == vc.PAUSED:
        return vc.PAUSED
    return vc.BLOCKED


def instrument_rows(root):
    """(name, verdict-code) for every selftest-bearing census instrument."""
    return [(r["rel"], _run_instrument(os.path.join(root, r["rel"]), root))
            for r in selftest_instruments(root)]


def wiki_lint_row(root):
    """Run the vendored wiki lint (M2.13) over a clean store: 0 violations -> PASS, an error-level
    violation -> FAIL, the lint could not run -> BLOCKED. Side-effect free (a throwaway vault), so
    the preflight never writes a store into the project."""
    tmp = tempfile.mkdtemp(prefix="boot-check-wiki-lint-")
    try:
        from alpaca.wiki.config import Config
        from alpaca.wiki.store.db import DB
        from alpaca.wiki.lint import run_lint
        cfg = Config.for_vault(tmp)
        db = DB(cfg)
        try:
            db.pour()
        except Exception:
            pass
        try:
            report = run_lint(db, cfg)
            code = vc.PASS if report.exit_code == 0 else vc.FAIL
        finally:
            db.close()
    except Exception:
        code = vc.BLOCKED
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return ("wiki-lint", code)


def wiki_oracle_row(root):
    """Run the wiki mutation oracle (M2.13) over its checked-in mutation set at alpaca/tests/wiki. A
    PASS means every registered wiki mechanism has a source mutation that flips a named test with
    zero hollow -- never that a mechanism is proven. Uses the regression wrapper's mapping."""
    code, _summary = regression_suite.run_wiki_mutation_oracle(os.path.join(root, "alpaca"))
    return ("wiki-mutation-oracle", code)


def _tests_dir(root):
    cand = os.path.join(root, "alpaca", "tests")
    return cand if os.path.isdir(cand) else os.path.join(root, "tests")


def regression_row(root):
    """The pytest suite as ONE instrument -- the LAST row. Re-entrancy guarded, so a nested
    boot-check run from inside pytest short-circuits PASS without re-spawning the whole suite."""
    code, _summary = regression_suite.run_suite(_tests_dir(root), root=root)
    return ("regression-suite", code)


def plan(root, full=False):
    """The ordered ROW NAMES this preflight would compose, WITHOUT executing anything. The census
    population first, then (under --full) the wiki lint, the mutation oracle, and the regression
    suite LAST. Used to prove the row list matches the census without running the fleet."""
    names = [r["rel"] for r in selftest_instruments(root)]
    if full:
        names += ["wiki-lint", "wiki-mutation-oracle", "regression-suite"]
    return names


def rows(root, full=False):
    """Execute every composed row and return the ordered [(name, verdict-code), ...] list. The
    per-instrument selftests run first, then (under --full) the wiki lint, the mutation oracle, and
    the regression suite LAST."""
    out = list(instrument_rows(root))
    if full:
        out.append(wiki_lint_row(root))
        out.append(wiki_oracle_row(root))
        out.append(regression_row(root))            # LAST
    return out


def run(root, full=False):
    """Compose every row and fold to ONE verdict-band code with the contract's worst() precedence
    (BLOCKED > FAIL > PAUSED > PASS). An empty population folds to BLOCKED (a preflight over no
    instrument is never a pass)."""
    return contract.worst([code for _name, code in rows(root, full=full)])


def main(argv=None):
    ap = vc.make_parser(
        name="boot-check",
        description="Composed preflight: every census instrument (+ under --full the wiki lint, "
                    "the mutation oracle and the pytest suite) folded into one worst-code verdict.")
    ap.add_argument("--root", default=None, help="tree to check (default: the discovered root)")
    ap.add_argument("--full", action="store_true",
                    help="also run the wiki lint, the mutation oracle and the whole pytest suite")
    ap.add_argument("--plan", action="store_true",
                    help="print the composed row list (from the census) and exit, running nothing")
    a = ap.parse_args(argv)
    root = os.path.abspath(a.root) if a.root else _discover_root()
    if a.plan:
        for name in plan(root, full=a.full):
            print("  row %s" % name)
        return vc.PASS
    scope = "FULL (instruments + wiki lint + mutation oracle + pytest suite)" if a.full \
        else "core (instrument selftests)"
    print("boot-check: composing %s from the census over %s" % (scope, root))
    executed = rows(root, full=a.full)
    for name, code in executed:
        mark = "OK" if code == vc.PASS else "XX"
        print("  [%s] %-40s %s" % (mark, name, vc.name_of(code)))
    fold = contract.worst([code for _name, code in executed])
    return vc.emit_verdict("boot-check", fold,
                           "%d row(s) composed from the census; worst-code fold" % len(executed))


if __name__ == "__main__":
    sys.exit(main())
