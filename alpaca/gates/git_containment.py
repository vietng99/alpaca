"""git_containment.py -- GIT AS EXECUTION: scan a repo's git config for keys that turn an
ordinary git command into arbitrary code execution.

Why this file exists
--------------------
Git will run a command YOU did not type when certain config keys are set, and a repo-local
`.git/config` is trusted by git the moment you `cd` into the tree. `GIT_CONFIG_NOSYSTEM=1`
disables the SYSTEM file only; it does nothing about a repo-local config. So the control for
a foreign tree cannot be a flag -- it is a SCAN of the actual config bytes, plus an explicit
two-key override for the rare case an owner has consciously accepted one of these keys.

The keys that execute (absorb-gap AG-M13):

  core.hooksPath        points git's hook lookup at an arbitrary directory, so `git commit`
                        (and friends) run a script the tree shipped, not one you installed.
  diff.<name>.textconv  runs a command to turn a blob into text for a diff -- `git diff`,
                        `git log -p`, `git show` all trigger it.
  filter.<name>.clean   runs a command on `git add` (staging); with a matching gitattribute
  filter.<name>.smudge  runs a command on checkout. Either is code execution on ordinary use.

`scan(repo_dir)` returns a list of (token, config_file, line, detail) findings, empty when
contained. It reads only config bytes; it never runs git, so scanning a hostile tree buys no
code execution. It obeys the M1.3 verdict contract (alpaca.gates.verdict): the CLI boundary is
built through make_parser and terminates through emit_verdict, and it never restates the map.
"""
from __future__ import annotations

import os
import re
import sys

from alpaca.gates import verdict

NAME = "git-containment"

# Canonical BLOCK reason tokens.
R_HOOKSPATH = "GIT-CORE-HOOKSPATH"
R_TEXTCONV = "GIT-DIFF-TEXTCONV"
R_FILTER_CLEAN = "GIT-FILTER-CLEAN"
R_FILTER_SMUDGE = "GIT-FILTER-SMUDGE"

# The single confirmation key an override must carry IN ADDITION to the finding's own token,
# so suppressing a finding always takes two explicit keys, never one and never a bare flag.
OVERRIDE_CONFIRM = "I-ACCEPT-GIT-EXECUTES-THIS-REPO-CONFIG"

_SECTION_RE = re.compile(r'^\s*\[\s*([A-Za-z0-9.-]+)(?:\s+"(.*)")?\s*\]\s*$')
_KEY_RE = re.compile(r'^\s*([A-Za-z][A-Za-z0-9-]*)\s*(?:=(.*))?$')


def _config_files(repo_dir):
    """The git config file(s) that govern `repo_dir`, resolving a `.git` gitdir pointer file
    (a linked worktree) to its real gitdir and adding the shared common config. Only files that
    exist are returned; a path that is missing is simply not scanned."""
    dot_git = os.path.join(repo_dir, ".git")
    gitdirs = []
    if os.path.isdir(dot_git):
        gitdirs.append(dot_git)
    elif os.path.isfile(dot_git):
        try:
            with open(dot_git, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            text = ""
        m = re.search(r"gitdir:\s*(.+)", text)
        if m:
            gd = m.group(1).strip()
            if not os.path.isabs(gd):
                gd = os.path.join(repo_dir, gd)
            gitdirs.append(os.path.abspath(gd))
            # a linked worktree's commondir points at the shared gitdir
            commondir = os.path.join(gitdirs[-1], "commondir")
            if os.path.isfile(commondir):
                try:
                    with open(commondir, encoding="utf-8") as fh:
                        cd = fh.read().strip()
                    if not os.path.isabs(cd):
                        cd = os.path.join(gitdirs[-1], cd)
                    gitdirs.append(os.path.abspath(cd))
                except OSError:
                    pass
    files = []
    seen = set()
    for gd in gitdirs:
        for name in ("config", "config.worktree"):
            p = os.path.abspath(os.path.join(gd, name))
            if p not in seen and os.path.isfile(p):
                seen.add(p)
                files.append(p)
    return files


def _parse(path):
    """Yield (section, subsection, key, value, lineno) for every key line in a git config
    file. section/key are lowered (git treats them case-insensitively); subsection is kept as
    written (git treats it case-sensitively)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return
    section, subsection = "", ""
    for i, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        m = _SECTION_RE.match(raw)
        if m:
            section = (m.group(1) or "").lower()
            subsection = m.group(2)
            if subsection is None:
                # deprecated `[section.subsection]` form: split on the first dot
                if "." in section:
                    section, _, subsection = section.partition(".")
                else:
                    subsection = ""
            continue
        m = _KEY_RE.match(raw)
        if m:
            key = (m.group(1) or "").lower()
            value = (m.group(2) or "").strip()
            yield section, subsection or "", key, value, i


_FILTER_TOKENS = {"clean": R_FILTER_CLEAN, "smudge": R_FILTER_SMUDGE}


def scan(repo_dir, *, overrides=()):
    """Return dangerous-config findings for the git repo at `repo_dir`.

    Each finding is (token, config_file, lineno, detail). An empty list means contained.
    `overrides` suppresses a finding ONLY when it carries BOTH the confirmation key
    (OVERRIDE_CONFIRM) AND the finding's own token -- two keys, so a single stray value can
    never quietly disable the control.
    """
    ov = set(overrides or ())
    confirmed = OVERRIDE_CONFIRM in ov
    findings = []
    for path in _config_files(repo_dir):
        for section, _sub, key, value, lineno in _parse(path):
            tok = None
            detail = ""
            if section == "core" and key == "hookspath":
                tok = R_HOOKSPATH
                detail = ("core.hooksPath = %r points git's hook lookup at an arbitrary "
                          "directory; ordinary git commands then run its scripts" % value)
            elif section == "diff" and key == "textconv":
                tok = R_TEXTCONV
                detail = ("diff.*.textconv = %r runs a command on git diff/log -p/show" % value)
            elif section == "filter" and key in _FILTER_TOKENS:
                tok = _FILTER_TOKENS[key]
                detail = ("filter.*.%s = %r runs a command on git add/checkout" % (key, value))
            if tok is None:
                continue
            if confirmed and tok in ov:
                continue  # two-key override: confirm + this token
            findings.append((tok, path, lineno, detail))
    return findings


def verdict_of(findings) -> int:
    """Fold findings to a verdict-band code: BLOCKED on any finding, PASS otherwise. A repo
    that executes on ordinary use is a refusal to proceed, not a defect verdict."""
    return verdict.BLOCKED if findings else verdict.PASS


# --------------------------------------------------------------------------- selftest
def _write(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)


def selftest() -> int:
    import shutil
    import tempfile

    base = tempfile.mkdtemp(prefix="git-containment-selftest-")
    dangerous = os.path.join(base, "dangerous")
    _write(os.path.join(dangerous, ".git", "config"),
           "[core]\n\thooksPath = /tmp/evil\n"
           "[diff \"x\"]\n\ttextconv = cat\n"
           "[filter \"y\"]\n\tclean = ./c.sh\n\tsmudge = ./s.sh\n")
    clean = os.path.join(base, "clean")
    _write(os.path.join(clean, ".git", "config"), "[core]\n\tbare = false\n")

    rows = []

    def _ctl(cid, branch, ok, observed):
        rows.append((cid, branch, "FIRED" if ok else "DID-NOT-FIRE", observed, not ok))

    d = scan(dangerous)
    toks = {t for t, *_ in d}
    _ctl("GC-01", "core.hooksPath flagged", R_HOOKSPATH in toks, sorted(toks))
    _ctl("GC-02", "diff.*.textconv flagged", R_TEXTCONV in toks, sorted(toks))
    _ctl("GC-03", "filter.*.clean flagged", R_FILTER_CLEAN in toks, sorted(toks))
    _ctl("GC-04", "filter.*.smudge flagged", R_FILTER_SMUDGE in toks, sorted(toks))
    _ctl("GC-05", "each finding pins file:line",
         all(os.path.isfile(f) and isinstance(ln, int) and ln >= 1 for _t, f, ln, _dt in d),
         "%d finding(s)" % len(d))
    _ctl("GC-06", "dangerous repo folds to BLOCKED",
         verdict_of(d) == verdict.BLOCKED, verdict.name_of(verdict_of(d)))
    c = scan(clean)
    _ctl("GC-07", "POSITIVE: an ordinary repo is clean/PASS",
         c == [] and verdict_of(c) == verdict.PASS, "%d finding(s)" % len(c))
    one_key = scan(dangerous, overrides=[R_HOOKSPATH])
    _ctl("GC-08", "one-key override does NOT suppress",
         any(t == R_HOOKSPATH for t, *_ in one_key), "still flagged")
    two_key = scan(dangerous, overrides=[OVERRIDE_CONFIRM, R_HOOKSPATH])
    _ctl("GC-09", "two-key override suppresses just that finding",
         all(t != R_HOOKSPATH for t, *_ in two_key) and any(t == R_TEXTCONV for t, *_ in two_key),
         sorted({t for t, *_ in two_key}))

    shutil.rmtree(base, ignore_errors=True)

    failures = sum(1 for r in rows if r[4])
    w = max(len(r[1]) for r in rows)
    print("CONTROL TABLE -- %s (generated from executed calls)" % NAME)
    for cid, branch, state, observed, _failed in rows:
        print("  %-6s %-*s %-12s %s" % (cid, w, branch, state, observed))
    print("  %d control(s), %d did not fire" % (len(rows), failures))
    if failures:
        return verdict.emit_verdict(NAME + "-selftest", verdict.FAIL,
                                    "%d control(s) did not fire" % failures)
    return verdict.emit_verdict(NAME + "-selftest", verdict.PASS, "every control fired")


def main(argv=None) -> int:
    ap = verdict.make_parser(
        name=NAME,
        description="GIT AS EXECUTION scan: flag core.hooksPath / diff.*.textconv / "
                    "filter.*.clean|smudge in a repo's git config (a scan, never a flag).")
    ap.add_argument("--selftest", action="store_true", help="run the gate's own controls")
    ap.add_argument("--repo", default=".", help="the repo directory to scan (default: cwd)")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    findings = scan(a.repo)
    if findings:
        for tok, fpath, lineno, detail in findings:
            print("  finding %-20s %s:%d  %s" % (tok, fpath, lineno, detail))
        return verdict.emit_verdict(NAME, verdict.BLOCKED,
                                    "%d git-as-execution finding(s)" % len(findings),
                                    ["%s %s:%d" % (t, f, ln) for t, f, ln, _ in findings])
    return verdict.emit_verdict(NAME, verdict.PASS, "no git-as-execution config keys")


if __name__ == "__main__":
    sys.exit(main())
