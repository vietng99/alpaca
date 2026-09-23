#!/usr/bin/env python3
"""The MUTATION ORACLE [upstream tests/mutation_oracle.py] - a necessary-condition hollowness filter.

A PASS certifies each registered test is NOT HOLLOW - that a specific, checked-in source mutation of
its mechanism flips it to an assertion FAILURE. A pass NEVER means "verified / sound / proven"; it
means the test has teeth. This is the negative-control law turned on our own test suite.

Contract (tests/wiki/mutations.json - a list of):
  {
    "mechanism_id": "F1",                          # what mechanism this guards
    "file": "alpaca/wiki/guards.py",                    # file to patch (repo-relative)
    "find": "<exact source substring>",            # must occur EXACTLY once
    "replace": "<mutated substring>",              # the targeted break
    "flips_test": "tests.wiki.test_x.TestY.test_z",# this test MUST turn FAIL under the mutation
    "expect": "fail",                              # "fail" (assertion failure, default) | "error"
    "control_green": ["tests.wiki.test_x.TestY.test_other"]  # optional: must stay GREEN
  }

Isolation: subprocess-per-mutant against a full shutil.copytree sandbox, run via stdlib
`python3 -m unittest` (in-process reload is forbidden - unsound). The repo root is two levels up.

Run: python3 tests/wiki/mutation_oracle.py            # all mutations
     python3 tests/wiki/mutation_oracle.py --id F1    # one mechanism
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MUTATIONS = ROOT / "alpaca" / "tests" / "wiki" / "mutations.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from alpaca import manifest  # noqa: E402

_PATTERNS = shutil.ignore_patterns(
    "__pycache__", "*.pyc", ".git", ".claude", ".alpaca", "analytics", ".pytest_cache",
    "node_modules", "*.db", "*.db-wal", "*.db-shm")
_MEMORY = manifest.memory_ignore(ROOT)


def _IGNORE(dirpath, names):
    """Byte caches and stores by pattern, plus every [memory] path of the manifest anchored at
    ROOT (the local environment, host-local tool installs, build outputs). A sandbox stands for a
    fresh clone; copying runtime state into sixteen of them only costs disk and time."""
    return set(_PATTERNS(dirpath, names)) | set(_MEMORY(dirpath, names))


def _path() -> str:
    import os
    return os.environ.get("PATH", "/usr/bin:/bin")


def _run_unittest(sandbox: Path, test_id: str) -> tuple[int, int, int]:
    """Run one unittest id in the sandbox. Returns (returncode, failures, errors)."""
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "-v", test_id],
        cwd=str(sandbox), capture_output=True, text=True, encoding="utf-8",
        env={"PYTHONPATH": str(sandbox), "PYTHONHASHSEED": "0", "PATH": _path()},
    )
    out = proc.stdout + proc.stderr
    failures = errors = 0
    m = re.search(r"failures=(\d+)", out)
    if m:
        failures = int(m.group(1))
    m = re.search(r"errors=(\d+)", out)
    if m:
        errors = int(m.group(1))
    # a bare "FAILED" with no counts but non-zero rc => at least one problem; classify by traceback
    if proc.returncode != 0 and failures == 0 and errors == 0:
        errors = 1 if ("Error" in out and "AssertionError" not in out) else 0
        failures = 1 if failures == 0 and errors == 0 else failures
    return proc.returncode, failures, errors


def _apply(sandbox: Path, rel_file: str, find: str, replace: str) -> str | None:
    """Apply the find->replace patch in the sandbox. Returns an error string or None on success.

    HARDENED: the patch may ONLY touch a file inside the sandbox. An absolute `file` (or any path
    that escapes the sandbox) is normalized to ROOT-relative first, and the resolved target is
    asserted to live under the sandbox - a mutation can never write to the real source tree.
    """
    rel = rel_file
    p = Path(rel_file)
    if p.is_absolute():
        try:
            rel = str(p.resolve().relative_to(ROOT.resolve()))
        except ValueError:
            return f"path escapes the repo root: {rel_file}"
    target = (sandbox / rel).resolve()
    if not str(target).startswith(str(sandbox.resolve())):
        return f"path escapes the sandbox: {rel_file}"
    if not target.exists():
        return f"file not found: {rel}"
    src = target.read_text(encoding="utf-8")
    n = src.count(find)
    if n == 0:
        return f"find-string not present in {rel_file}"
    if n > 1:
        return f"find-string ambiguous ({n} occurrences) in {rel_file}"
    target.write_text(src.replace(find, replace), encoding="utf-8")
    return None


def evaluate(mutation: dict) -> dict:
    """Run one mutation in a fresh sandbox. Returns a verdict record."""
    mech = mutation["mechanism_id"]
    expect = mutation.get("expect", "fail")
    with tempfile.TemporaryDirectory(prefix="mut_") as td:
        sandbox = Path(td) / "root"
        shutil.copytree(ROOT, sandbox, ignore=_IGNORE)
        err = _apply(sandbox, mutation["file"], mutation["find"], mutation["replace"])
        if err:
            return {"mechanism_id": mech, "status": "MUTATION_MALFORMED", "detail": err}

        rc, failures, errors = _run_unittest(sandbox, mutation["flips_test"])
        if rc == 0:
            return {"mechanism_id": mech, "status": "HOLLOW",
                    "detail": f"{mutation['flips_test']} still PASSED under the mutation"}
        if expect == "error":
            ok = errors >= 1
        else:
            # a genuine flip is an ASSERTION FAILURE, not an import/runtime error (MALFORMED)
            if errors >= 1 and failures == 0:
                return {"mechanism_id": mech, "status": "MUTATION_MALFORMED",
                        "detail": f"{mutation['flips_test']} ERRORed (not an assertion failure)"}
            ok = failures >= 1
        if not ok:
            return {"mechanism_id": mech, "status": "HOLLOW",
                    "detail": f"did not flip as expected (failures={failures}, errors={errors})"}

        # blast-radius: control tests must stay green under the mutation
        for ctrl in mutation.get("control_green", []):
            crc, _, _ = _run_unittest(sandbox, ctrl)
            if crc != 0:
                return {"mechanism_id": mech, "status": "MUTATION_MALFORMED",
                        "detail": f"control test {ctrl} broke (blast radius too wide)"}
        return {"mechanism_id": mech, "status": "FLIPPED", "detail": mutation["flips_test"]}


def run(only: str | None = None) -> dict:
    """Programmatic entry [interface: mutation_oracle.run(root)]: returns flipped/hollow counts."""
    if not MUTATIONS.exists():
        return {"flipped": 0, "hollow": 0, "malformed": 0, "results": [], "total": 0}
    mutations = json.loads(MUTATIONS.read_text(encoding="utf-8"))
    if only:
        mutations = [m for m in mutations if m["mechanism_id"] == only]
    results = [evaluate(m) for m in mutations]
    flipped = [r for r in results if r["status"] == "FLIPPED"]
    hollow = [r for r in results if r["status"] == "HOLLOW"]
    malformed = [r for r in results if r["status"] == "MUTATION_MALFORMED"]
    return {"flipped": len(flipped), "hollow": len(hollow), "malformed": len(malformed),
            "total": len(results), "results": results}


def main(argv=None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    only = None
    if "--id" in args:
        only = args[args.index("--id") + 1]
    if not MUTATIONS.exists():
        print(f"no mutations file at {MUTATIONS}")
        return 2
    summary = run(only)
    for r in summary["results"]:
        mark = {"FLIPPED": "FLIPPED", "HOLLOW": "HOLLOW", "MUTATION_MALFORMED": "MALFORMED"}[r["status"]]
        print(f"  [{mark}]  {r['mechanism_id']}: {r['detail']}")
    print(f"\nmutation oracle: {summary['flipped']}/{summary['total']} flipped, "
          f"{summary['hollow']} HOLLOW, {summary['malformed']} MALFORMED")
    print("badge: NOT-HOLLOW (necessary condition only - NOT 'verified/sound/proven')")
    # HOLLOW is the only hard failure; MALFORMED means fix the mutation record, not the test
    return 0 if not summary["hollow"] and not summary["malformed"] else 1


if __name__ == "__main__":
    sys.exit(main())
