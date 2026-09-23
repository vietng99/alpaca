"""wiring_audit.py - the "present != wired" standing instrument (M1.17).

Ported from the earlier harness gates/wiring_audit.py and re-based onto Alpaca. The earlier harness audited two
populations (drop-in reference artifacts and gates/ops instruments); the reference half was a
SystemVerilog-campaign domain concept and is dropped here. What remains is the generic,
load-bearing half the spec names (spec:300-307, spec:39): every instrument that is PRESENT must
have an INDEPENDENT caller, or it is dead weight that only looks installed.

This gate folds the `instrument_census` into a verdict:

  * an instrument with at least one PRODUCTION caller (a non-test module that imports or invokes
    it) is WIRED -> PASS.
  * an instrument with NO production caller is an orphan -> FAIL (WIRING-UNCALLED-INSTRUMENT),
    bound to its path, with any test-only or doc-only mentions disclosed in the detail. A unit
    test that drives a gate is NOT the gate being wired into a door or CLI, so a test-only
    instrument still FAILs - that is the exact distinction the spec draws.
  * an empty instrument population BLOCKS (WIRING-NO-INSTRUMENTS); a claim over an empty
    population is never a pass (the floor).
  * a population in which not one instrument is production-wired BLOCKS (NO-WIRING-WITNESS): a
    check that only ever refuses is not evidence that anything is connected.

The instrument's own file is never its own witness (the census excludes self), so a self-
attesting instrument - the exact defect class this gate refuses - can never pass itself.

HONEST LIMIT (do not soften): wiring is proven at the level of a NAMED reference (an import or a
path literal), not by executing the tree. A module that names an instrument but never reaches it
on a live path still reads as a caller (disclosed residual).
"""
from __future__ import annotations

from collections import namedtuple

from alpaca.gates import contract
from alpaca.gates import instrument_census as census
from alpaca.gates import verdict as vc

INSTRUMENT = "wiring-audit"

# reason tokens a caller greps, never prose.
R_UNCALLED = "WIRING-UNCALLED-INSTRUMENT"      # present, no production caller
R_NO_POP = "WIRING-NO-INSTRUMENTS"             # the instrument population is empty
R_NO_WITNESS = "NO-WIRING-WITNESS"             # not one instrument was positively wired

Result = namedtuple("Result", "verdict reasons info")


def _orphan_detail(name, row):
    extra = ""
    mentions = list(row["test_callers"]) + list(row["doc_mentions"])
    if mentions:
        extra = (" (named only by a test/doc, which is not production wiring: %s)"
                 % mentions[:3])
    return ("%s: %s is present under alpaca/gates/ or alpaca/checklist/ but no production module imports "
            "or invokes it - a verifier nobody runs%s" % (R_UNCALLED, row["rel"], extra))


def audit(root, instrument_dirs=census.INSTRUMENT_DIRS) -> Result:
    """Fold the census of `root` into a wiring verdict. Never writes; a pure read of the tree."""
    rows = census.census(root, instrument_dirs)
    info = {"instruments": [r["rel"] for r in rows], "wired": [], "orphans": []}
    if not rows:
        return Result(vc.BLOCKED,
                      ["%s: no *.py instrument under %s; an empty population is never a pass"
                       % (R_NO_POP, ", ".join(instrument_dirs))], info)
    verdicts, reasons = [], []
    for r in rows:
        if r["callers"]:
            verdicts.append(vc.PASS)
            info["wired"].append(r["rel"])
        else:
            verdicts.append(vc.FAIL)
            reasons.append(_orphan_detail(r["name"], r))
            info["orphans"].append(r["rel"])
    if not info["wired"]:
        # not one instrument is production-wired: a check that only refuses is not a witness.
        verdicts.append(vc.BLOCKED)
        reasons.append("%s: not one instrument under alpaca/gates/ or alpaca/checklist/ has a production "
                       "caller; a wiring check that only refuses is not evidence of wiring"
                       % R_NO_WITNESS)
    return Result(contract.worst(verdicts), reasons, info)


def orphans(root, instrument_dirs=census.INSTRUMENT_DIRS):
    """The live orphan records for the integrator: every instrument that currently lacks a
    production caller, with its test/doc mentions. A structured view of the audit's findings, so
    a caller (or a report) can list what still needs wiring without parsing prose."""
    rows = census.census(root, instrument_dirs)
    out = []
    for r in rows:
        if not r["callers"]:
            out.append({"name": r["name"], "rel": r["rel"], "reason": R_UNCALLED,
                        "test_callers": r["test_callers"], "doc_mentions": r["doc_mentions"]})
    return out


def check(root) -> int:
    """The uniform instrument entry: the wiring verdict for `root`."""
    return audit(root).verdict


# ------------------------------------------------------------------------- CLI boundary
def main(argv=None) -> int:
    ap = vc.make_parser(
        name=INSTRUMENT,
        description="'present != wired' auditor: every instrument under alpaca/gates/ and "
                    "alpaca/checklist/ must have an independent production caller, or FAIL.")
    ap.add_argument("--root", default=None, help="tree to audit (default: the discovered root)")
    ap.add_argument("--selftest", action="store_true", help="run the negative-control selftest")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    from alpaca import paths
    root = a.root or paths.root()
    res = audit(root)
    for r in res.reasons:
        print("  %s" % r)
    print("  wired: %d   orphans: %d" % (len(res.info["wired"]), len(res.info["orphans"])))
    return vc.emit_verdict(INSTRUMENT, res.verdict, res.reasons[0] if res.reasons else "all wired",
                           evidence=res.info["orphans"])


def _mk(base, files):
    import os
    for rel, body in files.items():
        p = os.path.join(base, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)
    return base


def selftest() -> int:
    """Negative-control discipline: every control seeds the BAD case (the guard FAILs/BLOCKs with
    the exact token) and the GOOD case (it PASSes). Scratch goes to a fresh tempdir, never the
    shipped tree."""
    import os
    import shutil
    import tempfile

    base = tempfile.mkdtemp(prefix="wiring-audit-selftest-")
    rows = []
    failures = 0
    seq = [0]

    def fresh():
        seq[0] += 1
        d = os.path.join(base, "t-%02d" % seq[0])
        os.makedirs(d, exist_ok=True)
        return d

    def audit_of(files):
        return audit(_mk(fresh(), files))

    def has(res, token):
        return any(token in r for r in res.reasons)

    def control(cid, desc, bad_res, bad_want, token, good_res):
        nonlocal failures
        bad_fires = bad_res.verdict == bad_want and has(bad_res, token)
        good_passes = good_res.verdict == vc.PASS and not has(good_res, token)
        ok = bad_fires and good_passes
        if not ok:
            failures += 1
        rows.append((cid, desc, "FIRED" if ok else "DID-NOT-FIRE",
                     "bad=%s good=%s" % ("FIRED" if bad_fires else "MISS",
                                         "PASS" if good_passes else "MISS")))

    GOOD = {
        "alpaca/gates/used_gate.py": "def check(root):\n    return 0\n",
        "alpaca/driver.py": "from alpaca.gates import used_gate\nused_gate.check('.')\n",
    }

    bad_orphan = dict(GOOD)
    bad_orphan["alpaca/gates/orphan_gate.py"] = "def check(root):\n    return 0\n"
    control("W-1", "an instrument with no production caller -> FAIL",
            audit_of(bad_orphan), vc.FAIL, R_UNCALLED, audit_of(dict(GOOD)))

    bad_test = dict(GOOD)
    bad_test["alpaca/gates/lonely_gate.py"] = "def check(root):\n    return 0\n"
    bad_test["tests/test_lonely.py"] = "from alpaca.gates import lonely_gate\n"
    control("W-2", "a test-only import does not wire (still FAIL)",
            audit_of(bad_test), vc.FAIL, R_UNCALLED, audit_of(dict(GOOD)))

    bad_empty = {"alpaca/util.py": "x = 1\n"}
    control("W-3", "an empty instrument population -> BLOCKED",
            audit_of(bad_empty), vc.BLOCKED, R_NO_POP, audit_of(dict(GOOD)))

    res_pos = audit_of(dict(GOOD))
    ok = res_pos.verdict == vc.PASS
    if not ok:
        failures += 1
    rows.append(("E-1", "a fully wired tree audits PASS", "FIRED" if ok else "DID-NOT-FIRE",
                 "%d wired" % len(res_pos.info["wired"])))

    shutil.rmtree(base, ignore_errors=True)
    print("CONTROL TABLE - %s" % INSTRUMENT)
    for cid, desc, state, obs in rows:
        print("  %-5s %-48s %-12s %s" % (cid, desc, state, obs))
    if failures:
        return vc.emit_verdict(INSTRUMENT + "-selftest", vc.FAIL,
                               "%d control(s) did not fire" % failures)
    return vc.emit_verdict(INSTRUMENT + "-selftest", vc.PASS, "every control fired")


if __name__ == "__main__":
    import sys
    sys.exit(main())
