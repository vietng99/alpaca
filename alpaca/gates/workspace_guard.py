"""workspace_guard.py -- the WORKSPACE ISOLATION gate: the harness reads and writes only
inside ONE declared root, never a scratch/tmp root, and never a path that realpath-resolves
outside that root.

Ported from the earlier harness gates/workspace_guard.py and adapted for Alpaca:

  * the earlier harness's `Verdict` enum and `exit_with` are replaced by the M1.3 verdict contract
    (alpaca.gates.verdict: integer codes, emit_verdict, make_parser). This file never restates
    the verdict<->code map; rc_conformance guards that.
  * The public entry is `check(root, targets)` -> a list of (token, path, detail) findings,
    empty when clean. The token is a canonical BLOCK reason word a caller greps, not prose.
    Every door in M1.15 runs this as its first precondition; a non-empty result is BLOCKED.
  * `atomic_write(root, relpath, data)` writes temp-then-rename INSIDE the root, so a crash
    mid-write never leaves a half file and never lands outside the tree.

Two invariants, both fail-closed:

  (I1) NO-TMP. The root MUST NOT resolve under a temp/scratch root (/tmp, $TMPDIR,
       /var/tmp, /dev/shm, %TEMP%): scratch overflows and evaporates on reboot.
  (I2) CONTAINMENT. Every declared target MUST realpath-resolve at or under the root. realpath
       is deliberate: a symlink that LOOKS internal but points outside is resolved to its real
       target and flagged -- you cannot smuggle a foreign file in behind an inside-looking name.

This gate BLOCKS; a containment breach is a refusal to proceed, not a verdict about a
subject's correctness, so it never FAILs and never silently passes.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

from alpaca.gates import verdict

NAME = "workspace-guard"

# Canonical BLOCK reason tokens: the leading word of every finding, so a caller greps ONE
# token, not prose.
R_ROOT_MALFORMED = "ROOT-MALFORMED"        # root is not a path string / carries a NUL
R_ROOT_ABSENT = "ROOT-ABSENT"              # root does not resolve to a directory
R_ROOT_UNDER_TMP = "ROOT-UNDER-TMP"        # I1: root resolves under a scratch/tmp root
R_TARGET_MALFORMED = "TARGET-MALFORMED"    # a target is not a path string / carries a NUL
R_TARGET_OUTSIDE = "TARGET-OUTSIDE-ROOT"   # I2: a target realpath-resolves outside the root
R_TARGET_ESCAPE = "TARGET-ESCAPES-ROOT"    # a relative write path escapes the root via `..`


def _real(p: str) -> str:
    """Fully resolved absolute path: expand ~, make absolute, resolve every symlink.
    Containment is decided on the REAL target, never on the pretty name."""
    return os.path.realpath(os.path.abspath(os.path.expanduser(p)))


def _bad_path_value(p):
    """None if `p` is a usable path string; else a short human reason it is refused. A non-str
    value would be coerced by os.path.* (an int read as a file descriptor, bytes str()-coerced
    into a literal name); a str carrying an embedded NUL raises inside realpath/exists."""
    if not isinstance(p, str):
        return "not a path string (got %s)" % type(p).__name__
    if "\x00" in p:
        return "contains an embedded NUL byte"
    return None


def default_tmp_prefixes():
    """The scratch/temp roots a root may not live under, each fully resolved. Built from the
    environment (so a host that relocates its temp dir is still covered) plus the fixed Unix
    scratch roots. Empty entries are dropped so a missing env var never yields the fs root."""
    prefixes = set()
    for env in ("TMPDIR", "TEMP", "TMP"):
        v = os.environ.get(env)
        if v:
            prefixes.add(_real(v))
    try:
        prefixes.add(_real(tempfile.gettempdir()))
    except Exception:  # pragma: no cover - gettempdir is extremely robust
        pass
    for fixed in ("/tmp", "/var/tmp", "/dev/shm"):
        prefixes.add(_real(fixed))
    return frozenset(p for p in prefixes if p and p != os.sep)


def is_within(root_real: str, path_real: str) -> bool:
    """True iff `path_real` is the root itself or lies strictly beneath it. Both arguments must
    already be realpaths. The `+ os.sep` guard stops `/root-evil` matching `/root`."""
    return path_real == root_real or path_real.startswith(root_real + os.sep)


def _under_any(path_real: str, prefixes):
    for pre in prefixes:
        if is_within(pre, path_real):
            return pre
    return None


def check(root, targets=(), *, tmp_prefixes=None):
    """Adjudicate I1/I2 for one root and its declared targets.

    Returns a list of (token, path, detail) findings; an empty list means clean (durable root,
    every target contained). A root-level breach (malformed / absent / under-tmp) short-circuits
    and is returned alone. `tmp_prefixes` defaults to default_tmp_prefixes(); pass frozenset()
    only to isolate a non-I1 property under test.
    """
    bad = _bad_path_value(root)
    if bad is not None:
        return [(R_ROOT_MALFORMED, repr(root),
                 "the root %r is %s; refusing to coerce it into a filesystem path"
                 % (root, bad))]
    root_real = _real(root)
    if not os.path.isdir(root_real):
        return [(R_ROOT_ABSENT, root,
                 "the declared root %r does not resolve to a directory (%s)"
                 % (root, root_real))]
    prefixes = default_tmp_prefixes() if tmp_prefixes is None else tmp_prefixes
    hit = _under_any(root_real, prefixes)
    if hit is not None:
        return [(R_ROOT_UNDER_TMP, root_real,
                 "the root %s resolves under scratch/tmp root %s; scratch overflows and "
                 "evaporates on reboot -- use a durable root" % (root_real, hit))]

    findings = []
    for t in targets:
        bad = _bad_path_value(t)
        if bad is not None:
            findings.append((R_TARGET_MALFORMED, repr(t),
                             "the target %r is %s; a trusted target must be a path string"
                             % (t, bad)))
            continue
        try:
            t_real = _real(t)
        except (ValueError, OSError) as e:
            findings.append((R_TARGET_MALFORMED, str(t),
                             "the target %r could not be resolved (%s: %s)"
                             % (t, type(e).__name__, e)))
            continue
        if not is_within(root_real, t_real):
            findings.append((R_TARGET_OUTSIDE, str(t),
                             "the target %r resolves to %s, OUTSIDE the root %s (symlinks are "
                             "resolved to their real target); refusing to consume a foreign path"
                             % (t, t_real, root_real)))
    return findings


def verdict_of(findings) -> int:
    """Fold a findings list to a verdict-band code: BLOCKED on any finding, PASS otherwise."""
    return verdict.BLOCKED if findings else verdict.PASS


def atomic_write(root, relpath, data, *, encoding="utf-8"):
    """Write `data` to `<root>/<relpath>` atomically: a temp file in the same directory, then
    an os.replace rename, so a reader never sees a half-written file and a crash leaves no
    partial target. Refuses (ValueError) when `relpath` would land outside the root."""
    bad = _bad_path_value(root)
    if bad is not None:
        raise ValueError("%s: %s" % (R_ROOT_MALFORMED, bad))
    bad = _bad_path_value(relpath)
    if bad is not None:
        raise ValueError("%s: %s" % (R_TARGET_MALFORMED, bad))
    root_real = _real(root)
    full = os.path.abspath(os.path.join(root_real, relpath))
    full_real_parent = _real(os.path.dirname(full))
    if not (is_within(root_real, os.path.abspath(full))
            and (full_real_parent == root_real or is_within(root_real, full_real_parent))):
        raise ValueError("%s: %r escapes the root %s" % (R_TARGET_ESCAPE, relpath, root_real))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    is_bytes = isinstance(data, (bytes, bytearray))
    fd, tmp = tempfile.mkstemp(prefix=".alpaca-tmp-", dir=os.path.dirname(full))
    try:
        if is_bytes:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        else:
            with os.fdopen(fd, "w", encoding=encoding, newline="") as fh:
                fh.write(data)
        os.replace(tmp, full)
    except BaseException:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise
    return full


# ------------------------------------------------------- change class (the floor, M3.3)
def change_class(root, target):
    """The floor's change class for a write `target`: "agent-writable" or "human-owned".

    This write path consults the floor rather than restating the split, so classification has
    ONE source (the manifest) and cannot drift between the guard and alpaca/posture/floor.py.
    Imported lazily to keep the gate's import graph free of the posture layer."""
    from alpaca.posture import floor
    return floor.classify(root, target)


# --------------------------------------------------------------------------- selftest
def _ctl(cid, branch, thunk, expect_token):
    """One control -> (cid, branch, state, observed, failed?). expect_token=None means the
    call must return no findings (a positive control)."""
    try:
        findings = thunk()
    except BaseException as e:  # a control must not blow up; that is a failure
        return (cid, branch, "DID-NOT-FIRE", "%s: %s" % (type(e).__name__, e), True)
    tokens = [tok for tok, _p, _d in findings]
    if expect_token is None:
        ok = not findings
        observed = "PASS" if ok else ("BLOCKED/%s" % ",".join(tokens))
    else:
        ok = expect_token in tokens
        observed = ("BLOCKED/%s" % ",".join(tokens)) if findings else "PASS"
    return (cid, branch, "FIRED" if ok else "DID-NOT-FIRE", observed, not ok)


def selftest() -> int:
    base = tempfile.mkdtemp(prefix="workspace-guard-selftest-")
    root = os.path.join(base, "root")
    os.makedirs(root)
    inside = os.path.join(root, "spec.md")
    with open(inside, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("# spec\n")
    outside_dir = os.path.join(base, "other_project")
    os.makedirs(outside_dir)
    outside = os.path.join(outside_dir, "their_spec.md")
    with open(outside, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("# a DIFFERENT project's spec\n")
    escape_link = os.path.join(root, "looks_internal.md")
    have_symlink = True
    try:
        os.symlink(outside, escape_link)
    except (OSError, NotImplementedError, AttributeError):  # pragma: no cover
        have_symlink = False

    NONE = frozenset()  # disable the I1 tmp check to isolate a non-I1 property

    rows = []
    rows.append(_ctl(
        "WG-01", "I1: a root UNDER the real tmp root -> BLOCKED/ROOT-UNDER-TMP",
        lambda: check(root, [inside]),  # default tmp_prefixes: root IS under temp
        R_ROOT_UNDER_TMP))
    rows.append(_ctl(
        "WG-02", "I2: a target in a DIFFERENT project dir -> BLOCKED/OUTSIDE",
        lambda: check(root, [outside], tmp_prefixes=NONE),
        R_TARGET_OUTSIDE))
    if have_symlink:
        rows.append(_ctl(
            "WG-03", "I2: a symlink that LOOKS internal but escapes -> BLOCKED/OUTSIDE",
            lambda: check(root, [escape_link], tmp_prefixes=NONE),
            R_TARGET_OUTSIDE))
    rows.append(_ctl(
        "WG-04", "a `..` traversal target that escapes -> BLOCKED/OUTSIDE",
        lambda: check(root, [os.path.join(root, "..", "other_project", "their_spec.md")],
                      tmp_prefixes=NONE),
        R_TARGET_OUTSIDE))
    rows.append(_ctl(
        "WG-05", "root does not resolve to a directory -> BLOCKED/ROOT-ABSENT",
        lambda: check(os.path.join(base, "no_such_root"), tmp_prefixes=NONE),
        R_ROOT_ABSENT))
    rows.append(_ctl(
        "WG-06", "a non-string ROOT (truthy int) -> BLOCKED/ROOT-MALFORMED",
        lambda: check(1, [inside], tmp_prefixes=NONE),
        R_ROOT_MALFORMED))
    rows.append(_ctl(
        "WG-07", "a bytes target (str()-coerced name) -> BLOCKED/TARGET-MALFORMED",
        lambda: check(root, [b"/etc/passwd"], tmp_prefixes=NONE),
        R_TARGET_MALFORMED))
    rows.append(_ctl(
        "WG-08", "an embedded NUL byte in a target -> clean BLOCKED, never a raise",
        lambda: check(root, ["\x00"], tmp_prefixes=NONE),
        R_TARGET_MALFORMED))
    # positive controls: a guard that cannot PASS is as broken as one that cannot BLOCK.
    rows.append(_ctl(
        "WG-09", "POSITIVE: durable root + a contained target -> PASS",
        lambda: check(root, [inside], tmp_prefixes=NONE),
        None))
    rows.append(_ctl(
        "WG-10", "POSITIVE: the root itself counts as contained -> PASS",
        lambda: check(root, [root], tmp_prefixes=NONE),
        None))

    # atomic_write controls: writes inside, refuses outside.
    def _aw_inside():
        atomic_write(root, os.path.join("notes", "n.txt"), "x\n")
        return [] if os.path.isfile(os.path.join(root, "notes", "n.txt")) else \
            [("WG-AW-NOWRITE", "", "atomic_write did not land the target")]
    rows.append(_ctl("WG-11", "POSITIVE: atomic_write lands a target inside the root",
                     _aw_inside, None))

    def _aw_outside():
        try:
            atomic_write(root, os.path.join("..", "escape.txt"), "x\n")
        except ValueError:
            return [(R_TARGET_ESCAPE, "..", "refused")]
        return []  # no finding -> the control did NOT fire
    rows.append(_ctl("WG-12", "atomic_write REFUSES a path that escapes the root",
                     _aw_outside, R_TARGET_ESCAPE))

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
        description="WORKSPACE ISOLATION gate: the harness reads and writes inside ONE "
                    "declared, durable (non-tmp) root; a target that escapes it is BLOCKED.")
    ap.add_argument("--selftest", action="store_true", help="run the gate's own controls")
    ap.add_argument("--root", help="the declared root directory")
    ap.add_argument("--target", action="append", default=[], metavar="PATH",
                    help="a target that must resolve inside the root (repeatable)")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not a.root:
        return verdict.emit_verdict(NAME, verdict.BLOCKED, "no --root declared")
    findings = check(a.root, a.target)
    if findings:
        for tok, p, detail in findings:
            print("  finding %-20s %s  %s" % (tok, p, detail))
        return verdict.emit_verdict(NAME, verdict.BLOCKED,
                                    "%d containment finding(s)" % len(findings),
                                    ["%s: %s" % (t, p) for t, p, _ in findings])
    return verdict.emit_verdict(NAME, verdict.PASS,
                                "root is durable and every target is contained")


if __name__ == "__main__":
    sys.exit(main())
