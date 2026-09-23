"""structural_conformance.py - presence-only "tree vs declared structure" (M1.17).

Ported from the earlier harness gates/structural_conformance.py and reduced to the generic half the plan
keeps. The earlier harness also re-derived SystemVerilog interfaces and their signal names from a reference env
and matched a filelist grammar; those are DOMAIN signal checks and are OUT OF SCOPE for M1-M4
(M1.17 Step 4). What remains is the load-bearing generic check: a tree either CONTAINS the
structural units a declaration names, or it does not.

The REQUIRED structure is a DECLARATION, read from `project.yaml`'s `structure` block - never
guessed from a live reference:

    structure:
      required_dirs:  [alpaca, contracts, step-models]
      required_files: [ALPACA-MANIFEST, project.yaml]

  * a required dir the tree lacks   -> CONFORMANCE-MISSING-DIR (FAIL, bound to the dir)
  * a required file the tree lacks  -> CONFORMANCE-MISSING-FILE (FAIL, bound to the file)
  * an absent declaration           -> REQUIREMENTS-ABSENT (BLOCKED)
  * a declaration that names nothing -> REQUIREMENTS-EMPTY (BLOCKED; a vacuous requirement
    certifies nothing)
  * a tree that satisfies not one requirement -> CONFORMANCE-NO-WITNESS (BLOCKED, never a
    vacuous pass)

HONEST LIMIT: this is a PRESENCE check (the path exists), not a content or a semantic check. A
green is "structurally present", never "correct".
"""
from __future__ import annotations

import os
from collections import namedtuple

from alpaca import project
from alpaca.gates import contract
from alpaca.gates import verdict as vc

INSTRUMENT = "structural-conformance"

# reason tokens.
R_MISSING_DIR = "CONFORMANCE-MISSING-DIR"
R_MISSING_FILE = "CONFORMANCE-MISSING-FILE"
R_NO_WITNESS = "CONFORMANCE-NO-WITNESS"
R_TREE_ABSENT = "CONFORMANCE-TREE-ABSENT"
R_REQ_ABSENT = "REQUIREMENTS-ABSENT"
R_REQ_EMPTY = "REQUIREMENTS-EMPTY"

Result = namedtuple("Result", "verdict reasons info")


def load_requirements(root):
    """Read the `structure` declaration from `root`'s project.yaml. Returns (reqs, block|None),
    where `block` is (token, detail) when the declaration is absent or vacuous (BLOCKED)."""
    if not os.path.isfile(project.path(root)):
        return None, (R_REQ_ABSENT,
                      "no project.yaml under %s declares a required structure; a conformance "
                      "check with no requirement is not a pass" % os.path.abspath(root))
    try:
        cfg = project.load(root)
    except Exception as e:
        return None, (R_REQ_ABSENT, "project.yaml under %s could not be read: %s"
                      % (os.path.abspath(root), e))
    block = (cfg or {}).get("structure") or {}
    dirs = [str(d) for d in (block.get("required_dirs") or [])]
    files = [str(f) for f in (block.get("required_files") or [])]
    if not dirs and not files:
        return None, (R_REQ_EMPTY,
                      "the structure declaration in %s names no required directory or file; a "
                      "vacuous requirement certifies nothing" % os.path.abspath(root))
    return {"required_dirs": dirs, "required_files": files}, None


def check_conformance(reqs, tree) -> Result:
    """Presence-only conformance of `tree` against a loaded requirements dict."""
    tree_abs = os.path.abspath(tree)
    info = {"tree": tree_abs, "required_dirs": reqs["required_dirs"],
            "required_files": reqs["required_files"]}
    if not os.path.isdir(tree_abs):
        return Result(vc.BLOCKED, ["%s: the tree %s does not exist or is not a directory"
                                   % (R_TREE_ABSENT, tree_abs)], info)
    verdicts, reasons = [], []
    passed = 0
    for d in reqs["required_dirs"]:
        if os.path.isdir(os.path.join(tree_abs, d.replace("/", os.sep))):
            passed += 1
            verdicts.append(vc.PASS)
        else:
            verdicts.append(vc.FAIL)
            reasons.append("%s: the declaration requires directory %r; the tree does not contain "
                           "it" % (R_MISSING_DIR, d))
    for f in reqs["required_files"]:
        if os.path.isfile(os.path.join(tree_abs, f.replace("/", os.sep))):
            passed += 1
            verdicts.append(vc.PASS)
        else:
            verdicts.append(vc.FAIL)
            reasons.append("%s: the declaration requires file %r; the tree does not contain it"
                           % (R_MISSING_FILE, f))
    info["passed"] = passed
    if passed == 0 and vc.BLOCKED not in verdicts:
        verdicts.append(vc.BLOCKED)
        reasons.append("%s: not one required unit is present; a check that only refuses is not "
                       "evidence of conformance" % R_NO_WITNESS)
    return Result(contract.worst(verdicts), reasons, info)


def check(root) -> int:
    """The uniform instrument entry. An absent `structure` block is an advisory PASS (nothing is
    required, so there is nothing to refute); a declared structure is checked and adjudicated."""
    reqs, block = load_requirements(root)
    if reqs is None:
        # a project that declares no structure is not a defect at this level.
        return vc.PASS if block and block[0] in (R_REQ_ABSENT, R_REQ_EMPTY) else vc.BLOCKED
    return check_conformance(reqs, root).verdict


# ------------------------------------------------------------------------- CLI boundary
def main(argv=None) -> int:
    ap = vc.make_parser(
        name=INSTRUMENT,
        description="Presence-only structural conformance: the tree must contain the dirs and "
                    "files declared in project.yaml's `structure` block, or FAIL.")
    ap.add_argument("--root", default=None, help="tree to check (default: the discovered root)")
    ap.add_argument("--selftest", action="store_true", help="run the negative-control selftest")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    from alpaca import paths
    root = a.root or paths.root()
    reqs, block = load_requirements(root)
    if reqs is None:
        token, detail = block
        return vc.emit_verdict(INSTRUMENT, vc.BLOCKED if token != R_REQ_ABSENT else vc.PASS,
                               "%s: %s" % (token, detail))
    res = check_conformance(reqs, root)
    for r in res.reasons:
        print("  %s" % r)
    return vc.emit_verdict(INSTRUMENT, res.verdict, res.reasons[0] if res.reasons else "conformant")


def _mk(base, files, dirs=()):
    for d in dirs:
        os.makedirs(os.path.join(base, d), exist_ok=True)
    for rel, body in files.items():
        p = os.path.join(base, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)
    return base


def selftest() -> int:
    import shutil
    import tempfile

    base = tempfile.mkdtemp(prefix="structural-conformance-selftest-")
    rows = []
    failures = 0
    seq = [0]

    def fresh():
        seq[0] += 1
        d = os.path.join(base, "t-%02d" % seq[0])
        os.makedirs(d, exist_ok=True)
        return d

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

    reqs = {"required_dirs": ["alpaca", "contracts"], "required_files": ["ALPACA-MANIFEST"]}

    bad_dir = _mk(fresh(), {"ALPACA-MANIFEST": "alpaca\n"}, dirs=["alpaca"])          # contracts missing
    good = _mk(fresh(), {"ALPACA-MANIFEST": "alpaca\n"}, dirs=["alpaca", "contracts"])
    control("SC-1", "a required dir the tree lacks -> FAIL bound to it",
            check_conformance(reqs, bad_dir), vc.FAIL, R_MISSING_DIR,
            check_conformance(reqs, good))

    reqs_f = {"required_dirs": ["alpaca"], "required_files": ["CHARTER.md"]}
    bad_file = _mk(fresh(), {"ALPACA-MANIFEST": "alpaca\n"}, dirs=["alpaca"])          # CHARTER.md missing
    good_f = _mk(fresh(), {"ALPACA-MANIFEST": "alpaca\n", "CHARTER.md": "x\n"}, dirs=["alpaca"])
    control("SC-2", "a required file the tree lacks -> FAIL bound to it",
            check_conformance(reqs_f, bad_file), vc.FAIL, R_MISSING_FILE,
            check_conformance(reqs_f, good_f))

    # empty declaration -> BLOCKED at load; a real declaration loads clean.
    empty_root = _mk(fresh(), {"project.yaml": "structure:\n  required_dirs: []\n"}, dirs=["alpaca"])
    _r, blk = load_requirements(empty_root)
    good_root = _mk(fresh(), {"project.yaml": "structure:\n  required_dirs:\n    - alpaca\n"},
                    dirs=["alpaca"])
    _r2, blk2 = load_requirements(good_root)
    ok = (blk is not None and blk[0] == R_REQ_EMPTY and blk2 is None)
    if not ok:
        failures += 1
    rows.append(("SC-3", "an empty structure declaration -> BLOCKED (a real one loads)",
                 "FIRED" if ok else "DID-NOT-FIRE",
                 "empty=%s real=%s" % (blk[0] if blk else "-", "loaded" if blk2 is None else blk2)))

    shutil.rmtree(base, ignore_errors=True)
    print("CONTROL TABLE - %s" % INSTRUMENT)
    for cid, desc, state, obs in rows:
        print("  %-5s %-52s %-12s %s" % (cid, desc, state, obs))
    if failures:
        return vc.emit_verdict(INSTRUMENT + "-selftest", vc.FAIL,
                               "%d control(s) did not fire" % failures)
    return vc.emit_verdict(INSTRUMENT + "-selftest", vc.PASS, "every control fired")


if __name__ == "__main__":
    import sys
    sys.exit(main())
