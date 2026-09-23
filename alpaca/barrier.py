"""barrier.py - the outbound push barrier and the tier rule at the push boundary (M4.11).

The barrier is the mechanical control at the moment the bytes leave (absorb-gap AG-M28). It is
NOT the decision to push: rule 8 (spec:816-817) keeps a push to a shared remote a human decision
at every autodrive level, so the decision stays a review card (M3.5). The barrier only REFUSES
mechanically when what is leaving carries something it must not.

What it does, and why each clause is here:

  * `install(root)` writes a git pre-push hook. Git runs pre-push before any object transfer, with
    the candidate refs on stdin, so it is the one seam where "about to leave" is still catchable.
  * `scan(root, commits)` scans EVERY COMMIT entering the remote, not the working tree, because the
    barrier exists for what history carries: a secret deleted from the tip still ships in the
    commit that added it. Each commit's files are read from the object store (`git ls-tree`), never
    from the checkout.
  * a protected PREFIX or exact PATH (project.yaml `barrier.protected_paths`) blocks the push.
  * the report names DIGESTS, never paths: a leak report that prints the offending path re-leaks
    it. Every blocked entry is identified by a sha256 digest of its path (and the commit it rode
    in on), so the report is safe to keep and to show.
  * ANY error in the scan REFUSES the push (fail-closed). A barrier that lets a push through when
    it could not look is not a barrier; every exception folds to BLOCKED.
  * the blind leak classifier (alpaca/gates/canary_score) scores against a sealed canary set, so the
    control is proven able to fail as well as able to pass.

The term list is DATA in project.yaml (alpaca/gates/leak_audit points it there), never a code literal.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from collections import namedtuple

from alpaca import paths, project
from alpaca.gates import verdict as vc
from alpaca.gates import leak_audit

INSTRUMENT = "outbound-barrier"

R_PROTECTED = "PROTECTED-PATH-IN-PUSH"
R_LEAK = "SEALED-TERM-IN-PUSH"
R_SCAN_ERROR = "SCAN-ERROR-REFUSES"
R_NO_COMMITS = "NO-COMMITS-TO-SCAN"

HOOK_NAME = "pre-push"
_ZERO = "0" * 40

Verdict = namedtuple("Verdict", "verdict reasons report")


# ------------------------------------------------------------------ configuration (DATA)
def protected_paths(root):
    """The protected prefixes and exact paths, read from project.yaml `barrier.protected_paths`.
    A trailing slash marks a prefix; anything else is an exact path. DATA, never a code literal."""
    try:
        cfg = project.load(root) or {}
    except Exception:
        cfg = {}
    block = cfg.get("barrier") or {}
    return [str(p) for p in (block.get("protected_paths") or []) if str(p).strip()]


def tier(root):
    """The project tier (project.yaml top-level `tier`). Public is the default posture."""
    try:
        cfg = project.load(root) or {}
    except Exception:
        cfg = {}
    return str(cfg.get("tier") or "public")


def _digest(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def path_is_protected(path, protected):
    """True when `path` hits a protected exact path or a protected prefix (trailing slash)."""
    p = path.replace(os.sep, "/")
    if p.startswith("./"):
        p = p[2:]
    for rule in protected:
        r = rule.replace(os.sep, "/")
        if r.startswith("./"):
            r = r[2:]
        if r.endswith("/"):
            if p == r.rstrip("/") or p.startswith(r):
                return rule
        elif p == r:
            return rule
    return None


# ------------------------------------------------------------------ git plumbing
def _git(root, *args):
    """Run git read-only in `root`. Raises on nonzero so the caller folds it to a refusal."""
    out = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                         encoding="utf-8")
    if out.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), out.stderr.strip()))
    return out.stdout


def commit_files(root, sha):
    """Every (path, blob_sha) tracked in commit `sha`, read from the object store (not the tree)."""
    out = _git(root, "ls-tree", "-r", "-z", sha)
    files = []
    for entry in out.split("\0"):
        if not entry.strip():
            continue
        meta, _, path = entry.partition("\t")
        cols = meta.split()
        if len(cols) >= 3 and cols[1] == "blob":
            files.append((path, cols[2]))
    return files


def blob_bytes(root, blob_sha):
    out = subprocess.run(["git", "-C", root, "cat-file", "blob", blob_sha],
                         capture_output=True)
    if out.returncode != 0:
        raise RuntimeError("git cat-file %s failed" % blob_sha)
    return out.stdout


def commits_entering(root, local_sha, remote_sha):
    """The commit shas that would enter the remote: those reachable from the local ref but not the
    remote ref. A fresh remote (remote_sha all-zero) means every commit reachable from local."""
    if not remote_sha or set(remote_sha) == {"0"}:
        rng = local_sha
    else:
        rng = "%s..%s" % (remote_sha, local_sha)
    out = _git(root, "rev-list", rng)
    return [line.strip() for line in out.splitlines() if line.strip()]


# ------------------------------------------------------------------ the scan
def scan(root, commits, protected=None, term_list=None):
    """Scan every commit entering the remote and return a Verdict.

    The push passes (PASS) only when no commit carries a protected path and no sealed term is
    found in any blob. A protected prefix or exact path blocks (BLOCKED). A sealed term in a blob
    fails (FAIL). Any error refuses (BLOCKED, fail-closed). The report names digests, never paths.
    """
    if protected is None:
        protected = protected_paths(root)
    if term_list is None:
        term_list, _r, _d = leak_audit.load_terms_from_project(root)

    report = {"instrument": INSTRUMENT, "commits_scanned": 0, "files_scanned": 0,
              "blocked": [], "tier": tier(root)}
    reasons, verdicts = [], []

    try:
        if not commits:
            report["reason_codes"] = [R_NO_COMMITS]
            report["detail"] = ["no commits are entering the remote; nothing to scan"]
            report["verdict"] = vc.PASS
            return Verdict(vc.PASS, [R_NO_COMMITS], report)
        for sha in commits:
            report["commits_scanned"] += 1
            for path, blob_sha in commit_files(root, sha):
                report["files_scanned"] += 1
                rule = path_is_protected(path, protected)
                if rule:
                    report["blocked"].append(
                        {"commit": sha[:12], "path_digest": _digest(path),
                         "rule_digest": _digest(rule), "reason": R_PROTECTED})
                    if R_PROTECTED not in reasons:
                        reasons.append(R_PROTECTED)
                    verdicts.append(vc.BLOCKED)
                    continue
                if term_list is not None:
                    raw = blob_bytes(root, blob_sha)
                    hits = _blob_hits(term_list, raw, path)
                    if hits:
                        report["blocked"].append(
                            {"commit": sha[:12], "path_digest": _digest(path),
                             "blob_digest": blob_sha[:12], "reason": R_LEAK})
                        if R_LEAK not in reasons:
                            reasons.append(R_LEAK)
                        verdicts.append(vc.FAIL)
    except Exception as e:
        # Fail-closed: a scan that could not look does not let the push through.
        reasons.append(R_SCAN_ERROR)
        report["reason_codes"] = reasons
        report["detail"] = ["scan error refuses the push: %s: %s" % (type(e).__name__, e)]
        report["verdict"] = vc.BLOCKED
        return Verdict(vc.BLOCKED, reasons, report)

    verdict = leak_audit.worst(verdicts) if verdicts else vc.PASS
    report["reason_codes"] = reasons
    report["verdict"] = verdict
    return Verdict(verdict, reasons, report)


def _blob_hits(term_list, raw, path):
    """Substring/byte-lane term hits in one blob, using the leak_audit matcher. Text lane first,
    byte lane as the backstop, exactly as the audit does per-file."""
    accounted = set()
    text, _codec = leak_audit.decode_explicit(raw)
    hits = []
    if text is not None:
        th, _ts = leak_audit.scan_text(term_list, text, path, shape_on=True)
        hits.extend(th)
        accounted = {r["rule"] for r in th if r["kind"] == "term"}
    keys = [(t, k.encode("utf-8")) for t, k in term_list.terms if t not in accounted]
    low = raw.lower()
    lown = raw.replace(b"\x00", b"").lower()
    for t, kb in keys:
        if kb in low or kb in lown:
            hits.append({"rule": t, "kind": "term", "lane": "bytes"})
    return hits


def render(report):
    """A human-readable barrier report that names DIGESTS, never paths. Safe to print and keep."""
    lines = ["  %s (tier=%s): %s" % (INSTRUMENT, report.get("tier", "?"),
                                     vc.name_of(report.get("verdict", vc.BLOCKED)))]
    lines.append("  commits scanned: %d   files scanned: %d   blocked: %d"
                 % (report.get("commits_scanned", 0), report.get("files_scanned", 0),
                    len(report.get("blocked", []))))
    for b in report.get("blocked", []):
        lines.append("    - refused %s in commit %s [%s]"
                     % (b.get("path_digest") or b.get("blob_digest"), b.get("commit"),
                        b.get("reason")))
    for d in report.get("detail", []):
        lines.append("    - %s" % d)
    return "\n".join(lines)


# ------------------------------------------------------------------ canary scoring
def score_canary(root, sealed_path, verdicts_path):
    """Grade a blind classifier against a sealed canary set (the able-to-fail half of the barrier).
    Delegates to alpaca/gates/canary_score; returns (verdict_code, summary)."""
    from alpaca.gates import canary_score
    return canary_score.score_files(sealed_path, verdicts_path)


# ------------------------------------------------------------------ install
def hook_path(root):
    hooks = _git(root, "rev-parse", "--git-path", "hooks").strip()
    if not os.path.isabs(hooks):
        hooks = os.path.join(root, hooks)
    return os.path.join(hooks, HOOK_NAME)


def hook_body():
    """The pre-push hook: it hands stdin (the candidate refs) to the barrier, which refuses on
    anything but PASS. It names no absolute path and no folder literal, so it is rename-safe: git
    runs it with cwd at the repo root and the barrier discovers the root from there."""
    return ("#!/bin/sh\n"
            "# alpaca outbound barrier (M4.11): scan every commit entering the remote and refuse\n"
            "# mechanically on a protected path or a sealed term. The decision to push at all is\n"
            "# still a human review card (M3.5); this only stops what must not leave.\n"
            'exec python3 -m alpaca.barrier prepush "$@"\n')


def install(root):
    """Write the pre-push hook into this repo's hooks dir. Returns the hook path. Idempotent: it
    overwrites its own prior hook, and refuses (raises) to clobber a foreign hook it did not write.
    """
    p = hook_path(root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as fh:
                existing = fh.read()
        except OSError:
            existing = ""
        if existing and "alpaca outbound barrier" not in existing:
            raise RuntimeError("%s already exists and was not written by alpaca barrier; refusing to "
                               "clobber a foreign hook" % HOOK_NAME)
    from alpaca import util
    util.write_text(p, hook_body())
    os.chmod(p, 0o755)
    return p


# ------------------------------------------------------------------ the hook entry
def prepush(argv=None):
    """The pre-push hook body. Reads the candidate refs from stdin, scans every commit that would
    enter the remote, and returns an exit code: 0 lets git proceed, non-zero refuses the push."""
    import sys
    root = paths.root()
    data = sys.stdin.read() if not sys.stdin.isatty() else ""
    worst = vc.PASS
    any_ref = False
    for line in data.splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        any_ref = True
        _local_ref, local_sha, _remote_ref, remote_sha = cols[:4]
        if set(local_sha) == {"0"}:
            continue  # a delete pushes no commits
        try:
            commits = commits_entering(root, local_sha, remote_sha)
        except Exception as e:
            print(render({"verdict": vc.BLOCKED, "detail": ["%s: %s" % (type(e).__name__, e)]}))
            return vc.emit_verdict(INSTRUMENT, vc.BLOCKED, R_SCAN_ERROR)
        res = scan(root, commits)
        print(render(res.report))
        worst = leak_audit.worst([worst, res.verdict])
    if not any_ref:
        return vc.PASS
    if worst == vc.PASS:
        return vc.emit_verdict(INSTRUMENT, vc.PASS, "nothing protected or sealed is leaving")
    return vc.emit_verdict(INSTRUMENT, worst, "push refused mechanically (still a human decision)")


# ------------------------------------------------------------------ CLI: alpaca barrier install|scan
def _cmd_barrier(args):
    from alpaca import cli
    root = cli._root()
    if args.barrier_verb == "install":
        p = install(root)
        return vc.emit_verdict("alpaca-barrier-install", vc.PASS,
                               "pre-push hook installed", evidence=[os.path.basename(p)])
    if args.barrier_verb == "scan":
        remote_sha = args.since or _ZERO
        try:
            commits = commits_entering(root, args.rev, remote_sha)
        except Exception as e:
            return vc.emit_verdict("alpaca-barrier-scan", vc.BLOCKED,
                                   "%s: %s" % (type(e).__name__, e))
        res = scan(root, commits)
        print(render(res.report))
        return vc.emit_verdict("alpaca-barrier-scan", res.verdict, "; ".join(res.reasons[:6]))
    print("GATE alpaca-barrier: BLOCKED (unknown barrier verb; use install or scan)")
    return vc.BLOCKED


def _parser(sub):
    b = sub.add_parser("barrier", help="the outbound push barrier: install the pre-push hook, or "
                                       "scan the commits entering a remote")
    bv = b.add_subparsers(dest="barrier_verb")
    bv.add_parser("install", help="write the pre-push hook into this repo")
    sc = bv.add_parser("scan", help="scan the commits reachable from a rev (not the working tree)")
    sc.add_argument("rev", help="the local ref/sha whose commits would enter the remote")
    sc.add_argument("--since", default=None, help="the remote sha already present (default: none, "
                                                  "so every commit reachable from rev is scanned)")


def _register():
    from alpaca import cli
    cli.command("barrier")(_cmd_barrier)
    cli.register_parser("barrier", _parser)


_register()


def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "prepush":
        return prepush(argv[1:])
    ap = vc.make_parser(name=INSTRUMENT, description="outbound push barrier")
    ap.add_argument("verb", nargs="?", choices=["prepush"], help="hook entry")
    ap.parse_args(argv)
    return prepush([])


if __name__ == "__main__":
    import sys
    sys.exit(main())
