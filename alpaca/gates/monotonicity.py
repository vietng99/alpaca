"""The control-count monotonicity guard. Ported from the earlier harness gates/monotonicity_check.py.

WHAT THIS IS
    A change to a gate's CODE is allowed to change WHAT the gate checks, but it must never
    quietly DELETE a control while the gate's own selftest still goes green: a gate that
    silently drops a control from 30 to 29 and still prints PASS has laundered a shrink. This
    guard freezes a per-gate control-count baseline and BLOCKS any current census in which a
    gate's count DECREASED, or in which a baselined gate has DISAPPEARED entirely. Growth is
    always allowed: adding controls is the healthy direction and never blocks.

    The census is taken by RUNNING each gate's own `--selftest` and reading back the one
    canonical tally line every gate in this tree prints: `  N control(s), ...`. `_parse_count`
    anchors on the leading `N control(s)` of the FIRST such line.

HONEST LIMIT (C9, do not soften)
    This guard is COORDINATION + TRACING, ADVISORY, not cryptographic. The count is
    SELF-REPORTED by each gate on stdout: a tampered gate can print any tally it likes and
    this module will believe it. Monotonicity here proves only that the observed, self-reported
    control count did not shrink between two runs of the gates as they sit on disk. Binding a
    count to gate identity is an external-anchor problem this module does not solve.

Additive-only: imports the verdict<->exit-code contract from alpaca.gates.verdict and never
restates it; writes nothing into the tree and touches no existing gate.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from alpaca.gates import verdict as vc

INSTRUMENT = "monotonicity"

# The FROZEN baseline: the per-gate control counts this milestone ships. A census below any of
# these -> BLOCKED. These are the Alpaca instruments that carry their own `--selftest` control table.
BASELINE = {
    "verdict-contract": 5,
    "chain-check": 4,
    "fuzz-gate": 6,
    "checklist-gate": 18,
}

# Reason tokens a control binds to (the names double as the tokens).
SHRINK = "MONOTONICITY-SHRINK-BLOCKED"
MISSING = "MONOTONICITY-BASELINE-MISSING-BLOCKED"
GROWTH_OK = "MONOTONICITY-GROWTH-OK"
COUNT_UNPARSEABLE = "COUNT-UNPARSEABLE"
COUNT_GATE_CRASHED = "COUNT-GATE-CRASHED"
BASELINE_FLOOR = "MONOTONICITY-BASELINE-FLOOR-BLOCKED"
BASELINE_RAISED = "MONOTONICITY-BASELINE-RAISED"

# PER-LINE anchor: the number and the literal `control(s)` must sit on the SAME line, so only
# spaces/tabs -- never a newline -- may separate them. `\s` would match `\n`, letting a bare
# number line and a following `control(s)` line FUSE into a false tally; `[ \t]` cannot.
_COUNT_RE = re.compile(rb"^[ \t]*(\d+)[ \t]+control\(s\)", re.MULTILINE)


class MonotonicityError(Exception):
    """A refusal carrying its exact reason token. Never swallow one into a PASS."""

    def __init__(self, token, detail=""):
        super().__init__("%s%s" % (token, (": " + detail) if detail else ""))
        self.token = token
        self.detail = detail


def parse_count(data) -> int | None:
    """The integer N from the first `N control(s)` tally line in `data` (bytes), or None.

    Anchored at line start so a reason echo that merely mentions 'control(s)' mid-line is never
    mistaken for the tally. The number and the literal must sit on the SAME line, so a bare
    number line followed by a `control(s)` line cannot be fused across the newline."""
    m = _COUNT_RE.search(data or b"")
    return int(m.group(1)) if m else None


def _is_crash(rc) -> bool:
    """True for a flaky crash exit code that warrants a retry. 0 and the harness band are REAL
    results (parse the tally); a POSIX SIGSEGV/ABRT/BUS/FPE/ILL, or a killed run, is a crash."""
    if rc is None:
        return True
    if rc == 139:
        return True
    if rc < 0:
        return -rc in (4, 6, 7, 8, 11)
    return False


def count_controls(census, attempts=3, timeout=900) -> dict:
    """Run each gate's `--selftest` and return {gate: control_count}.

    `census` maps gate-name -> [argv...] (a command that prints the canonical tally line). A
    gate that crashes on every attempt raises MonotonicityError(COUNT_GATE_CRASHED); a gate
    that runs but prints no tally raises MonotonicityError(COUNT_UNPARSEABLE); neither is ever
    laundered into a silent zero."""
    counts = {}
    for gate, argv in census.items():
        last = "no attempt made"
        n = None
        for attempt in range(1, attempts + 1):
            try:
                proc = subprocess.run(list(argv), stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, timeout=timeout)
            except subprocess.TimeoutExpired:
                last = "timed out (attempt %d)" % attempt
                continue
            if _is_crash(proc.returncode):
                last = "exit rc=%s -- flaky crash (attempt %d)" % (proc.returncode, attempt)
                continue
            out = proc.stdout or b""
            n = parse_count(out) or parse_count(out + b"\n" + (proc.stderr or b""))
            if n is None:
                raise MonotonicityError(
                    COUNT_UNPARSEABLE,
                    "gate=%s rc=%s produced no 'N control(s)' tally line"
                    % (gate, proc.returncode))
            break
        if n is None:
            raise MonotonicityError(
                COUNT_GATE_CRASHED,
                "gate=%s never produced a countable run in %d attempt(s): %s"
                % (gate, attempts, last))
        counts[gate] = n
    return counts


def diff(baseline, current) -> list:
    """The list of monotonicity violations (each prefixed by its reason token); [] == clean.

    A baselined gate MISSING from current, or whose current count is LESS than its baseline, is
    a violation. A count that grew or held is not. A gate present in current but absent from the
    baseline is ignored (a new gate cannot violate monotonicity against a baseline that never
    measured it)."""
    violations = []
    for gate in sorted(baseline):
        base_n = baseline[gate]
        if gate not in current:
            violations.append("%s gate=%s baseline=%d (gate absent from current census)"
                              % (MISSING, gate, base_n))
            continue
        if current[gate] < base_n:
            violations.append("%s gate=%s baseline=%d current=%d"
                              % (SHRINK, gate, base_n, current[gate]))
    return violations


def check(baseline, current) -> int:
    """-> PASS | BLOCKED. BLOCKED iff any gate's count decreased or a baselined gate is missing
    from current. The words come from the imported contract, never restated here."""
    return vc.BLOCKED if diff(baseline, current) else vc.PASS


def apply_baseline_floor(supplied, frozen=None) -> tuple:
    """Reconcile a supplied baseline against the frozen BASELINE, which is a HARD FLOOR.

    A supplied map may only RAISE a gate's required count above the frozen floor; it may never
    lower one below it, and it may never erase the floor by omission or by an empty map. Returns
    (effective, raises). Raises MonotonicityError(BASELINE_FLOOR) when the supplied map is
    empty, is not a {gate:int} map, or would lower any gate below its frozen floor."""
    frozen = BASELINE if frozen is None else frozen
    if not isinstance(supplied, dict) or not supplied:
        raise MonotonicityError(
            BASELINE_FLOOR, "supplied baseline is empty or not a {gate:int} map; the frozen "
                            "floor is a HARD FLOOR and may not be erased or replaced")
    below, raises, effective = [], [], dict(frozen)
    for gate, sup_n in supplied.items():
        if not isinstance(sup_n, int) or isinstance(sup_n, bool):
            raise MonotonicityError(
                BASELINE_FLOOR, "supplied value for gate=%s is not an integer: %r" % (gate, sup_n))
        floor_n = frozen.get(gate)
        if floor_n is None:
            effective[gate] = sup_n
            raises.append("gate=%s (new)->%d" % (gate, sup_n))
        elif sup_n < floor_n:
            below.append("gate=%s frozen_floor=%d supplied=%d" % (gate, floor_n, sup_n))
        elif sup_n > floor_n:
            effective[gate] = sup_n
            raises.append("gate=%s %d->%d" % (gate, floor_n, sup_n))
    if below:
        raise MonotonicityError(
            BASELINE_FLOOR, "supplied baseline would LOWER the frozen floor (BLOCKED, not "
                            "replaced): " + "; ".join(below))
    return effective, raises


# ---------------------------------------------------------------------- selftest
def selftest() -> int:
    """Every control seeds a BAD case (the guard fires on the exact named token) AND a GOOD
    case (it passes), over in-memory censuses plus a real tally-parse. No real gate is run."""
    rows = []

    def _c(cid, desc, bad_fires, good_passes, detail=""):
        rows.append((cid, desc, bool(bad_fires and good_passes), detail))

    cur = {"a": 5}
    # SHRINK: a count below baseline BLOCKS; equal PASSES.
    _c(SHRINK, "count fell below baseline (6->5) BLOCKS; equal (5) PASSES",
       check({"a": 6}, cur) == vc.BLOCKED and any(v.startswith(SHRINK) for v in diff({"a": 6}, cur)),
       check({"a": 5}, cur) == vc.PASS and diff({"a": 5}, cur) == [])
    # MISSING: a baselined gate absent from the census BLOCKS; present PASSES.
    _c(MISSING, "a baselined gate absent from the census BLOCKS; present PASSES",
       check({"ghost": 4}, cur) == vc.BLOCKED
       and any(v.startswith(MISSING) for v in diff({"ghost": 4}, cur)),
       check({"a": 5}, cur) == vc.PASS)
    # GROWTH-OK: growth passes; a shrink still BLOCKS (growth is not vacuous).
    _c(GROWTH_OK, "count above baseline (4<5) PASSES; a shrink still BLOCKS",
       check({"a": 6}, cur) == vc.BLOCKED,
       check({"a": 4}, cur) == vc.PASS and diff({"a": 4}, cur) == [])
    # BASELINE-FLOOR: below-floor and empty both BLOCK; a raise is accepted and marked.
    below_blocked = empty_blocked = False
    try:
        apply_baseline_floor({"checklist-gate": 1}, {"checklist-gate": 20})
    except MonotonicityError as e:
        below_blocked = e.token == BASELINE_FLOOR
    try:
        apply_baseline_floor({}, {"checklist-gate": 20})
    except MonotonicityError as e:
        empty_blocked = e.token == BASELINE_FLOOR
    raise_ok = False
    try:
        eff, raises = apply_baseline_floor({"checklist-gate": 40}, {"checklist-gate": 20, "x": 3})
        raise_ok = eff["checklist-gate"] == 40 and eff["x"] == 3 and bool(raises)
    except MonotonicityError:
        pass
    _c(BASELINE_FLOOR, "below-floor and empty both BLOCK; a raise is accepted, floor preserved",
       below_blocked and empty_blocked, raise_ok)
    # COUNT-UNPARSEABLE: a clean run with no tally line BLOCKS; a real tally parses.
    no_tally = False
    try:
        count_controls({"nc": [sys.executable, "-c", "print('no tally here')"]})
    except MonotonicityError as e:
        no_tally = e.token == COUNT_UNPARSEABLE
    real = count_controls({"g": [sys.executable, "-c", "print('  7 control(s), 0 did not fire')"]})
    _c(COUNT_UNPARSEABLE, "a run with no tally line BLOCKS; a real tally line parses",
       no_tally, real == {"g": 7})
    # REGEX-PERLINE: adjacent 'N' and 'control(s)' lines do NOT fuse; a one-line tally parses.
    fused = b"  80\ncontrol(s), 0 did not fire\n"
    _c("MONOTONICITY-REGEX-PERLINE",
       "adjacent 'N' and 'control(s)' lines do NOT fuse; a one-line tally parses",
       parse_count(fused) is None,
       parse_count(b"  80 control(s), 0 did not fire\n") == 80)

    failures = [cid for cid, _d, ok, _x in rows if not ok]
    print("CONTROL TABLE -- %s/1 (bad-fires AND good-passes per control)" % INSTRUMENT)
    for cid, desc, ok, _detail in rows:
        print("  %-40s %-12s %s" % (cid, "FIRED" if ok else "DID-NOT-FIRE", desc))
    print("  %d control(s), %d did not fire" % (len(rows), len(failures)))
    code = vc.FAIL if failures else vc.PASS
    print(vc.gate_line("%s-selftest" % INSTRUMENT, code))
    return vc.SELFTEST if failures else vc.PASS


def main(argv=None) -> int:
    ap = vc.make_parser(name=INSTRUMENT,
                        description="control-count monotonicity guard (a gate's control set "
                                    "may not shrink)")
    ap.add_argument("--selftest", action="store_true", help="run the negative-control suite")
    ap.add_argument("--census", help="a JSON file mapping gate -> current control count")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.census:
        with open(a.census, encoding="utf-8") as fh:
            current = json.load(fh)
        code = check(BASELINE, current)
        return vc.emit_verdict(INSTRUMENT, code,
                               "; ".join(diff(BASELINE, current)) or "no shrink")
    ap.print_help()
    return vc.USAGE


if __name__ == "__main__":
    sys.exit(main())
