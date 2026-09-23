"""contracts/ runner (M1.8).

`contracts/` holds human-owned executable checks, one script per check, one directory per
phase (spec 5.11). Agents read and run them; they never write them (the two-change-class
enforcement is task M3.3). This module runs them: for a given phase it executes every
executable under `contracts/<phase>/` and folds their exit codes with the verdict fold
`worst()` (BLOCKED > FAIL > PAUSED > PASS).

Each contract reports under the verdict contract (exit 0 PASS, 1 FAIL, 2 BLOCKED, 3
PAUSED). A file with no executable bit is not a contract and is skipped. A contract that
exits outside the verdict band broke the contract: its verdict cannot be read, so it
cannot adjudicate and folds as BLOCKED rather than laundering into a pass. An empty or
absent phase directory folds to BLOCKED as well: `worst([])` is a vacuous universal, never
a pass.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys

from alpaca.gates import contract, verdict

INSTRUMENT = "contracts-runner"


def phase_dir(root: str, phase: str) -> str:
    return os.path.join(root, "contracts", str(phase))


def executables(root: str, phase: str) -> list:
    """Every executable regular file under contracts/<phase>/, sorted by name."""
    d = phase_dir(root, phase)
    if not os.path.isdir(d):
        return []
    out = []
    for name in sorted(os.listdir(d)):
        full = os.path.join(d, name)
        if not os.path.isfile(full):
            continue
        if os.stat(full).st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            out.append(full)
    return out


def _run_one(root: str, exe: str) -> int:
    """Run one contract; return its exit code clamped to the verdict band."""
    try:
        proc = subprocess.run(
            [exe], cwd=root, text=True, encoding="utf-8", capture_output=True)
        code = proc.returncode
    except OSError:
        return verdict.BLOCKED
    if code not in verdict.VERDICT_BAND:
        # A contract that exits outside 0..3 did not report a verdict; treat the missing
        # adjudication as BLOCKED rather than folding an unknown code.
        return verdict.BLOCKED
    return code


def run(root: str, phase: str) -> int:
    """Execute every executable under contracts/<phase>/ and fold with worst()."""
    codes = [_run_one(root, exe) for exe in executables(root, phase)]
    return contract.worst(codes)


# ------------------------------------------------------------------------- CLI boundary
def main(argv=None) -> int:
    ap = verdict.make_parser(
        name=INSTRUMENT,
        description="Run the human-owned contracts for one phase under the verdict contract")
    ap.add_argument("phase", help="the phase whose contracts/<phase>/ to run")
    ap.add_argument("--root", default=None, help="project root (default: discovered)")
    a = ap.parse_args(argv)

    root = a.root
    if root is None:
        from alpaca import paths
        root = paths.root()

    exes = executables(root, a.phase)
    code = run(root, a.phase)
    return verdict.emit_verdict(
        INSTRUMENT, code, "%d contract(s) under contracts/%s/" % (len(exes), a.phase),
        evidence=[os.path.relpath(e, root) for e in exes])


if __name__ == "__main__":
    sys.exit(main())
