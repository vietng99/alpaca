#!/usr/bin/env python3
"""gen_manifest.py -- (re)generate and verify this distribution's MANIFEST.json (M4.15).

Ported UNCHANGED IN PURPOSE from the earlier harness gen_manifest.py and re-based onto Alpaca: the walk is no
longer a whole-tree scan with a hard-coded ignore list, it is driven by the harness manifest
(`ALPACA-MANIFEST`). Only the MECHANISM class (the shipped, git-tracked tree) is measured; the MEMORY
class (runtime state: .alpaca/, RESUME.md, analytics/) is excluded, so the digest map describes the
shipment and verifies clean on a fresh clone.

The manifest is a full-tree integrity record: `product`, `file_count`, a per-file sha256 map, and a
`tree_digest` over the map. It lets a cutover (and any downstream consumer) confirm the delivered
tree is byte-for-byte what was verified.

Algorithm (kept identical to the value stored in MANIFEST.json so old and new agree):
    files[path] = sha256(raw bytes of <path>)          # path is forward-slash, tree-relative
    tree_digest = sha256( "".join("<sha256>  <path>\\n" for path in sorted(files)) )
Excluded from the walk: MANIFEST.json itself (a file cannot contain its own hash), every
`__pycache__/` dir, any `*.pyc`, the `.git` dir, and every memory-class path.

The distribution identity is independent of the project name and extraction directory. Root
discovery starts beside this script, or at an explicit --root, never in an ambient host session.

Usage:
    python3 gen_manifest.py                 # verify against the stored MANIFEST.json
    python3 gen_manifest.py --verify        # same (explicit)
    python3 gen_manifest.py --write         # regenerate MANIFEST.json from the current tree
    python3 gen_manifest.py --restore-modes # put back the recorded exec bit on manifest-listed files
    python3 gen_manifest.py --selftest      # end-to-end drift-class controls via the real CLI
    (--root <dir> overrides the discovered root on any of the above)
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys

MANIFEST_NAME = "ALPACA-MANIFEST"
LOCK_NAME = "MANIFEST.json"
DIGEST_NOTE = (
    "sha256 over '<sha256>  <path>' lines in path order, over every mechanism-class file except "
    "MANIFEST.json itself, every __pycache__/ dir and any *.pyc. Re-derive with: python3 "
    "gen_manifest.py --verify")
DESCRIPTION = (
    "The engineering harness. A full-tree integrity record over the mechanism class the "
    "manifest declares; the memory class (runtime state) is excluded so the digest map describes "
    "the shipment and verifies on a fresh clone.")


def _product_name(root):
    """A fixed distribution identity; onboarding only changes the project identity."""
    return "Alpaca"

# Dirs never descended into, wherever they occur under a mechanism path.
_PRUNE_DIRS = frozenset(("__pycache__", ".git"))


def discover_root(start=None):
    cur = os.path.abspath(start or os.path.dirname(os.path.abspath(__file__)))
    while True:
        if os.path.isfile(os.path.join(cur, MANIFEST_NAME)):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            raise SystemExit("gen_manifest: no %s above %s"
                             % (MANIFEST_NAME, start or os.getcwd()))
        cur = parent


def read_classes(root):
    """Parse `<root>/ALPACA-MANIFEST` into {'mechanism': [...], 'memory': [...]}. Inline parser so this
    tool stays runnable when copied out of the tree."""
    out = {"mechanism": [], "memory": []}
    try:
        with open(os.path.join(root, MANIFEST_NAME), encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return out
    section = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1]
            section = name if name in out else None
            continue
        if section:
            out[section].append(line)
    return out


def _sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _type_mode(path):
    """A compact, NON-following type/permission marker for one path, so an exec-bit flip or a
    regular-file/symlink swap whose bytes are identical is still visible.

    A regular file records only what git keeps: f0755 when the owner exec bit is set, else f0644.
    Group and other bits come from the umask of whoever checked the tree out (0664 under umask
    0002), so they are neither recorded nor compared."""
    st = os.lstat(path)
    m = st.st_mode
    if stat.S_ISLNK(m):
        return "l"
    if stat.S_ISREG(m):
        return "f0755" if m & stat.S_IXUSR else "f0644"
    return "?%04o" % stat.S_IMODE(m)


def _norm(marker):
    """A stored marker in the form _type_mode gives today: an older manifest that recorded full
    modes (f0664, f0775) compares by its owner exec bit only."""
    if isinstance(marker, str) and marker.startswith("f") and len(marker) == 5:
        try:
            return "f0755" if int(marker[1:], 8) & stat.S_IXUSR else "f0644"
        except ValueError:
            pass
    return marker


def _rel(path, root):
    try:
        return os.path.relpath(path, root).replace(os.sep, "/")
    except Exception:
        return str(path)


def _is_memory(rel, memory):
    for m in memory:
        if rel == m or rel.startswith(m + "/"):
            return True
    return False


def build(root):
    """Walk the mechanism class and return (manifest_dict, walk_errors, read_errors).

    walk_errors: OSError instances for directories os.walk could not list (captured, never silently
    dropped, so the caller BLOCKS instead of attesting a tree it never measured).
    read_errors: (rel, OSError) for files that could not be read (unreadable file / dangling
    symlink), so the caller emits a clean BLOCKED diagnostic rather than a bare traceback.
    """
    classes = read_classes(root)
    mechanism = classes["mechanism"]
    memory = [m.rstrip("/") for m in classes["memory"]]
    files = {}
    modes = {}
    walk_errors = []
    read_errors = []

    def _on_walk_err(err):
        walk_errors.append(err)

    def _add_file(full):
        rel = _rel(full, root)
        if rel == LOCK_NAME or rel.endswith(".pyc") or _is_memory(rel, memory):
            return
        try:
            files[rel] = _sha_file(full)
            modes[rel] = _type_mode(full)
        except OSError as e:
            read_errors.append((rel, e))
            files.pop(rel, None)
            modes.pop(rel, None)

    for entry in mechanism:
        entry = entry.rstrip("/")
        full = os.path.join(root, entry.replace("/", os.sep))
        if _is_memory(entry, memory):
            continue
        if os.path.isdir(full):
            for base, dirs, names in os.walk(full, onerror=_on_walk_err):
                dirs[:] = [d for d in dirs if d not in _PRUNE_DIRS]
                for name in names:
                    _add_file(os.path.join(base, name))
        elif os.path.isfile(full):
            _add_file(full)
        # a listed-but-absent path is simply not measured (a synthetic tree may omit it).

    lines = ["%s  %s\n" % (files[p], p) for p in sorted(files)]
    tree_digest = hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()
    m = {
        "product": _product_name(root),
        "description": DESCRIPTION,
        "file_count": len(files),
        "tree_digest": tree_digest,
        "tree_digest_note": DIGEST_NOTE,
        "files": {p: files[p] for p in sorted(files)},
        "file_modes": {p: modes[p] for p in sorted(modes)},
    }
    return m, walk_errors, read_errors


def _blocked_reason(walk_errors, read_errors):
    if not walk_errors and not read_errors:
        return None
    out = ["gen_manifest: BLOCKED -- tree could not be fully measured; integrity NOT attested"]
    for e in walk_errors:
        fn = getattr(e, "filename", None)
        out.append("  ! unlistable directory: %s [%s]" % (fn or "?", type(e).__name__))
    for rel, e in read_errors:
        out.append("  ! unreadable file / dangling symlink: %s [%s]" % (rel, type(e).__name__))
    return "\n".join(out)


def write(root):
    m, walk_errors, read_errors = build(root)
    blocked = _blocked_reason(walk_errors, read_errors)
    if blocked:
        print(blocked)
        return 2
    with open(os.path.join(root, LOCK_NAME), "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2)
        f.write("\n")
    print("MANIFEST written: product=%s file_count=%d tree_digest=%s"
          % (m["product"], m["file_count"], m["tree_digest"][:16] + "..."))
    return 0


def verify(root):
    lock = os.path.join(root, LOCK_NAME)
    if not os.path.exists(lock):
        print("VERIFY: MANIFEST.json absent"); return 2
    try:
        with open(lock, encoding="utf-8") as _mf:
            stored = json.load(_mf)
    except (OSError, ValueError) as e:
        print("VERIFY: MANIFEST.json corrupt -- %s" % e); return 2
    cur, walk_errors, read_errors = build(root)
    blocked = _blocked_reason(walk_errors, read_errors)
    if blocked:
        print(blocked)
        print("VERIFY: BLOCKED (%d files measured)" % cur["file_count"])
        return 2
    ok = True
    if stored.get("tree_digest") != cur["tree_digest"]:
        ok = False
        print("VERIFY: tree_digest MISMATCH")
        print("  stored :", stored.get("tree_digest"))
        print("  actual :", cur["tree_digest"])
    s, c = set(stored.get("files", {})), set(cur["files"])
    for p in sorted(c - s):
        ok = False; print("  + on disk, not in manifest:", p)
    for p in sorted(s - c):
        ok = False; print("  - in manifest, not on disk:", p)
    for p in sorted(s & c):
        if stored["files"][p] != cur["files"][p]:
            ok = False; print("  ~ changed:", p)
    if "file_modes" in stored:
        sm, cm = stored.get("file_modes", {}), cur.get("file_modes", {})
        for p in sorted(set(sm) & set(cm)):
            if _norm(sm[p]) != cm[p]:
                ok = False; print("  M mode/type changed: %s (%s -> %s)" % (p, _norm(sm[p]), cm[p]))
    if stored.get("product") != _product_name(root):
        ok = False; print("VERIFY: product is %r, expected %r" % (stored.get("product"), _product_name(root)))
    print("VERIFY: %s (%d files)"
          % ("OK -- tree matches manifest" if ok else "FAIL", cur["file_count"]))
    return 0 if ok else 1


def restore_modes(root):
    """Install-time self-heal: chmod every manifest-listed regular file whose owner exec bit differs
    from the recorded mode to that mode. Idempotent and narrow by construction: it only ever
    touches a path already in the manifest, only to the octal the manifest already records."""
    lock = os.path.join(root, LOCK_NAME)
    if not os.path.exists(lock):
        print("RESTORE-MODES: MANIFEST.json absent -- cannot know the recorded modes"); return 2
    try:
        with open(lock, encoding="utf-8") as _mf:
            stored = json.load(_mf)
    except (OSError, ValueError) as e:
        print("RESTORE-MODES: MANIFEST.json corrupt -- %s" % e); return 2
    modes = stored.get("file_modes", {})
    if not modes:
        print("RESTORE-MODES: no file_modes recorded (nothing to restore)"); return 0
    changed = []
    checked = 0
    for rel in sorted(modes):
        marker = modes[rel]
        if not (isinstance(marker, str) and marker.startswith("f")):
            continue
        try:
            desired = int(marker[1:], 8)
        except ValueError:
            continue
        full = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.exists(full) or os.path.islink(full) or not os.path.isfile(full):
            continue
        checked += 1
        current = stat.S_IMODE(os.lstat(full).st_mode)
        # only the exec bit is recorded (see _type_mode): a file whose exec bit already matches is
        # left as the checkout made it, group write included
        if bool(current & stat.S_IXUSR) != bool(desired & stat.S_IXUSR):
            os.chmod(full, desired)
            changed.append((rel, current, desired))
    if changed:
        for rel, cur, des in changed:
            print("  restored %s: %04o -> %04o" % (rel, cur, des))
        print("RESTORE-MODES: restored %d mode(s) (%d regular file(s) checked)"
              % (len(changed), checked))
    else:
        print("RESTORE-MODES: already correct (%d regular file mode(s) checked)" % checked)
    return 0


def selftest():
    """Self-verify every drift class end-to-end through the real CLI (subprocess a copy of this file
    over a throwaway manifest-bearing tree), so the exit-code contract itself is exercised. Each
    control fires when the fix holds."""
    import subprocess
    import tempfile
    import shutil

    src = os.path.abspath(__file__)
    base = tempfile.mkdtemp(prefix="gen-manifest-selftest-")
    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    rows = []
    failures = 0

    def _w(path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def _run(tree, *args):
        p = subprocess.run([sys.executable, os.path.join(tree, "gen_manifest.py"), *args],
                           capture_output=True, text=True, encoding="utf-8")
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    def _mktree(cid, write_manifest=True):
        T = os.path.join(base, cid)
        os.makedirs(os.path.join(T, "alpaca"))
        shutil.copy(src, os.path.join(T, "gen_manifest.py"))
        _w(os.path.join(T, MANIFEST_NAME),
           "[mechanism]\nalpaca/\na.txt\nrun.sh\n[memory]\n.alpaca/\n")
        _w(os.path.join(T, "a.txt"), "alpha\n")
        _w(os.path.join(T, "run.sh"), "#!/bin/sh\necho hi\n")
        os.chmod(os.path.join(T, "run.sh"), 0o644)
        _w(os.path.join(T, "alpaca", "inner.py"), "x = 1\n")
        os.makedirs(os.path.join(T, ".alpaca"))
        _w(os.path.join(T, ".alpaca", "record.db"), "runtime-state\n")   # memory: never measured
        if write_manifest:
            rc, out = _run(T, "--write")
            if rc != 0:
                raise AssertionError("baseline --write rc=%d: %s" % (rc, out))
        return T

    def _ctl(cid, desc, fn):
        nonlocal failures
        try:
            ok, observed = fn()
        except BaseException as e:
            ok, observed = False, "%s: %s" % (type(e).__name__, e)
        if not ok:
            failures += 1
        rows.append((cid, desc, "FIRED" if ok else "DID-NOT-FIRE", observed))

    def _c00():
        T = _mktree("c00")
        rc, out = _run(T, "--verify")
        return rc == 0 and "OK -- tree matches manifest" in out, "rc=%d" % rc
    _ctl("G-00", "clean tree verifies OK (0) [positive control]", _c00)

    def _c_mem():
        T = _mktree("cmem")
        _w(os.path.join(T, ".alpaca", "record.db"), "MUTATED runtime state\n")
        rc, out = _run(T, "--verify")
        return rc == 0, "rc=%d (memory-class change is not drift)" % rc
    _ctl("G-MEM", "a memory-class change is NOT drift (excluded from the map)", _c_mem)

    def _c01():
        T = _mktree("c01")
        _w(os.path.join(T, "a.txt"), "MUTATED\n")
        rc, out = _run(T, "--verify")
        return rc == 1 and "~ changed" in out, "rc=%d" % rc
    _ctl("G-01", "content byte change -> FAIL (1)", _c01)

    def _c02():
        T = _mktree("c02")
        _w(os.path.join(T, "alpaca", "extra.py"), "y = 2\n")
        rc, out = _run(T, "--verify")
        return rc == 1 and "+ on disk, not in manifest" in out, "rc=%d" % rc
    _ctl("G-02", "file added on disk -> FAIL (1)", _c02)

    def _c03():
        T = _mktree("c03")
        os.remove(os.path.join(T, "a.txt"))
        rc, out = _run(T, "--verify")
        return rc == 1 and "- in manifest, not on disk" in out, "rc=%d" % rc
    _ctl("G-03", "file removed from disk -> FAIL (1)", _c03)

    def _c04():
        T = _mktree("c04")
        os.chmod(os.path.join(T, "run.sh"), 0o755)
        rc, out = _run(T, "--verify")
        return rc == 1 and "mode/type changed" in out, "rc=%d" % rc
    _ctl("G-04", "exec-bit flip (byte-identical) -> FAIL (1) [mode axis]", _c04)

    def _c06():
        T = _mktree("c06")
        d = os.path.join(T, "alpaca")
        os.chmod(d, 0o000)
        try:
            rc, out = _run(T, "--verify")
        finally:
            os.chmod(d, 0o755)
        return rc == 2 and "unlistable directory" in out, "rc=%d" % rc
    if is_root:
        rows.append(("G-06", "unreadable dir -> BLOCKED (2)", "SKIPPED", "running as root"))
    else:
        _ctl("G-06", "unreadable directory -> BLOCKED (2), not silent OK", _c06)

    def _c10():
        T = _mktree("c10")
        _w(os.path.join(T, LOCK_NAME), "{ bad json,,,")
        rc, out = _run(T, "--verify")
        clean = "Traceback" not in out
        return rc == 2 and "corrupt" in out and clean, "rc=%d clean=%s" % (rc, clean)
    _ctl("G-10", "corrupt MANIFEST.json -> BLOCKED (2), clean message", _c10)

    shutil.rmtree(base, ignore_errors=True)

    w = max(len(r[1]) for r in rows)
    print("gen_manifest --selftest control table (end-to-end via real CLI exit codes)")
    for cid, desc, state, observed in rows:
        print("  %-6s %-*s %-12s %s" % (cid, w, desc, state, observed))
    fired = sum(1 for r in rows if r[2] == "FIRED")
    skipped = sum(1 for r in rows if r[2] == "SKIPPED")
    print("  %d control(s): %d fired, %d did-not-fire, %d skipped"
          % (len(rows), fired, failures, skipped))
    if failures:
        print("GATE gen-manifest-selftest: FAIL")
        return 1
    print("GATE gen-manifest-selftest: PASS")
    return 0


def main(argv):
    if "--selftest" in argv:
        return selftest()
    root = None
    if "--root" in argv:
        i = argv.index("--root")
        try:
            root = os.path.abspath(argv[i + 1])
        except IndexError:
            print("gen_manifest: --root needs a directory"); return 64
    root = root or discover_root()
    if "--restore-modes" in argv:
        return restore_modes(root)
    if "--write" in argv:
        return write(root)
    return verify(root)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
