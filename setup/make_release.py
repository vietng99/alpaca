#!/usr/bin/env python3
"""make_release.py -- build the standalone, drop-in harness release (op-006).

The fresh-install shipment: the whole git-tracked harness tree carried to another machine, minus the
identity file in template mode. This is the artifact you carry; on the target you extract it, then
run `alpaca init` and `alpaca onboard`, and the harness boots as a fresh project.

This is the fresh-INSTALL companion to package_candidate.py, which refreshes only the manifest
mechanism class in place (an upgrade). A full install also needs the git-tracked support trees the
manifest does not enumerate as bare mechanism paths (doctrine/, MAP.md, plugin/, skills/,
formations/, agents/, setup/), so the source set is the whole TRACKED tree.

The tracked set is the boundary because it is the one line that already excludes every runtime
residue at once: the memory class (.alpaca/, RESUME.md, analytics/), the rendered board/data
projections, the agent worktrees under .claude/, byte caches, and the host-local settings file are
all gitignored, so none of them can enter a release. `git ls-files` is that boundary; the shipment
path policy re-checks it so a memory path can never slip through even if the ignore rules drift.

Two identity modes:
  * default (template): project.yaml is left OUT, so the target reads as true first contact
    (alpaca.adopt.detect FRESH) and onboarding re-authors a clean identity with the target's own name
    and root. No host path or source project name travels.
  * --keep-identity: project.yaml travels, so the SAME project moves to a new machine and keeps its
    id. Onboarding will refuse on the target (committed identity); you just start work.

Excluded from every release (all gitignored, so absent from the tracked set):
  * the memory class named in ALPACA-MANIFEST [memory] (.alpaca/, RESUME.md, analytics/);
  * the rendered board.json / data.json projections and every byte cache;
  * the host-local settings file and the agent worktrees dir under .claude/;
  * the dist/ output dir itself.

Determinism: tar members are path-sorted; modes are 0644 or 0755 (the owner exec bit, as git keeps
it); uid/gid, names and mtimes are normalised; the gzip timestamp and filename are absent. A rebuild from the same tree is byte-for-byte equal, so the
receipt sha256 is stable and a downstream consumer can compare bytes.

Rename-safe: no folder-name literal and no absolute path is baked in. The root is discovered at
runtime (CLAUDE_PROJECT_DIR, else the walk up to the directory holding ALPACA-MANIFEST); the product
name is read from the local project.yaml; the path policy is loaded from the sibling
shipment_policy.py so the memory class and the ignore list never drift.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import io
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tarfile


HERE = Path(__file__).resolve().parent
MANIFEST_NAME = "ALPACA-MANIFEST"
IDENTITY_FILE = "project.yaml"
_HARNESS_USAGE = 64  # verdict.USAGE; restated (not imported) so this helper runs detached.

POLICY_PATH = HERE / "shipment_policy.py"
_SPEC = importlib.util.spec_from_file_location("alpaca_shipment_policy_release", POLICY_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("cannot load shipment policy: %s" % POLICY_PATH)
_policy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_policy)


class ReleaseError(RuntimeError):
    pass


def discover_root(start=None):
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env and os.path.isfile(os.path.join(env, MANIFEST_NAME)):
        return os.path.abspath(env)
    cur = os.path.abspath(start or HERE)
    while True:
        if os.path.isfile(os.path.join(cur, MANIFEST_NAME)):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            raise ReleaseError("no %s above %s" % (MANIFEST_NAME, start or os.getcwd()))
        cur = parent


def _product_name(root):
    try:
        with open(os.path.join(root, IDENTITY_FILE), encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("name:"):
                    return line.split(":", 1)[1].strip().strip("'").strip('"')
    except OSError:
        pass
    return os.path.basename(os.path.abspath(root))


def _sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _mode_of(path):
    """The tar mode of a member: 0755 when the owner exec bit is set, else 0644. This is what git
    keeps; group write from the checkout's umask, other-write, setuid, setgid and sticky never
    travel, so the same commit gives the same archive on any host."""
    return 0o755 if os.lstat(path).st_mode & stat.S_IXUSR else 0o644


def _tracked_files(root):
    """Every path git tracks under root, as forward-slash relpaths. This is the shipment boundary:
    the memory class, the board/data projections, the worktrees dir, byte caches and the host-local
    settings file are all gitignored, so none appear here. A non-repo root is a hard error: a
    release is built from the tracked tree, never from a loose directory of uncertain provenance."""
    proc = subprocess.run(["git", "-C", root, "ls-files", "-z"],
                          capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise ReleaseError("git ls-files failed under %s (is it the tracked repo root?): %s"
                           % (root, proc.stderr.strip()))
    return [p for p in proc.stdout.split("\0") if p]


def select_members(root, *, keep_identity=False):
    """Return a sorted list of (relpath, mode) for every file the release carries.

    The population is the git-tracked set, minus the identity file in template mode. A symlink or any
    non-regular file is refused: a shipment is regular files only. The complete relpath population is
    validated against the shipment path policy (memory class + host-local floor) before any byte is
    written, so a memory path cannot enter the archive even if the ignore rules ever drift.
    """
    root = os.path.abspath(root)
    members = {}
    for rel in _tracked_files(root):
        if not keep_identity and rel == IDENTITY_FILE:
            continue
        full = os.path.join(root, *PurePosixPath(rel).parts)
        if os.path.islink(full) or not os.path.isfile(full):
            raise ReleaseError("release refuses a non-regular or missing tracked file: %s" % rel)
        members[rel] = _mode_of(full)
    rels = sorted(members)
    _policy.validate_staged_paths(rels, forbidden=_policy.forbidden_for_root(root))
    return [(rel, members[rel]) for rel in rels]


def _tarinfo(rel, mode, size):
    info = tarfile.TarInfo(rel)
    info.type = tarfile.REGTYPE
    info.mode = mode
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    info.size = size
    return info


def build(root, out_path, *, keep_identity=False):
    root = os.path.abspath(root)
    members = select_members(root, keep_identity=keep_identity)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    try:
        with tmp.open("wb") as raw_out:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw_out, mtime=0) as gz:
                with tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tar:
                    for rel, mode in members:
                        data = (Path(root) / Path(*PurePosixPath(rel).parts)).read_bytes()
                        tar.addfile(_tarinfo(rel, mode, len(data)), io.BytesIO(data))
        os.replace(tmp, out_path)
    finally:
        tmp.unlink(missing_ok=True)
    digest = _sha_bytes(out_path.read_bytes())
    receipt = out_path.with_name(out_path.name + ".sha256")
    receipt.write_text("%s  %s\n" % (digest, out_path.name), encoding="utf-8", newline="\n")
    return digest, len(members), str(receipt)


def verify(archive):
    """Open a release tarball and re-check the invariants: members path-sorted and unique, metadata
    normalised, no memory-class or host-local path present, identity handling consistent. Cross-check
    the co-located .sha256 receipt when it resolves."""
    archive = Path(archive)
    root = discover_root()
    forbidden = _policy.forbidden_for_root(root)
    try:
        with tarfile.open(archive, mode="r:gz") as tar:
            infos = tar.getmembers()
    except (tarfile.TarError, OSError) as exc:
        raise ReleaseError("cannot read archive: %s" % exc) from exc
    names = [i.name for i in infos]
    if len(names) != len(set(names)):
        raise ReleaseError("archive has duplicate members")
    if names != sorted(names):
        raise ReleaseError("archive members are not path-sorted")
    for i in infos:
        if not i.isfile():
            raise ReleaseError("non-regular member: %s" % i.name)
        if (i.uid, i.gid, i.uname, i.gname, i.mtime) != (0, 0, "", "", 0):
            raise ReleaseError("non-normalised metadata: %s" % i.name)
    _policy.validate_staged_paths(names, forbidden=forbidden)  # raises on a forbidden path
    receipt = archive.with_name(archive.name + ".sha256")
    cross = "none co-located"
    if receipt.is_file():
        claimed = (receipt.read_text(encoding="utf-8").split() or [""])[0]
        actual = _sha_bytes(archive.read_bytes())
        if claimed != actual:
            raise ReleaseError("receipt %s does not match the archive (%s != %s)"
                               % (receipt.name, claimed, actual))
        cross = "%s matches" % receipt.name
    identity = IDENTITY_FILE in names
    return len(names), identity, cross


class _BandParser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write("HARNESS-ERROR %s [USAGE]: %s\n" % (self.prog, message))
        raise SystemExit(_HARNESS_USAGE)

    def exit(self, status=0, message=None):
        if message:
            sys.stderr.write(message)
        raise SystemExit(_HARNESS_USAGE)


def _default_out(root, keep_identity):
    tag = "" if keep_identity else "-template"
    return os.path.join(root, "dist", "%s-release%s.tar.gz" % (_product_name(root), tag))


def parser():
    ap = _BandParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--out", default=None)
    b.add_argument("--keep-identity", action="store_true",
                   help="carry project.yaml so the SAME project moves; default re-authors identity")
    lst = sub.add_parser("list")
    lst.add_argument("--keep-identity", action="store_true")
    v = sub.add_parser("verify")
    v.add_argument("--archive", required=True)
    return ap


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "list":
            root = discover_root()
            members = select_members(root, keep_identity=args.keep_identity)
            for rel, mode in members:
                print("%04o  %s" % (mode, rel))
            print("RELEASE MEMBERS: %d (identity %s)"
                  % (len(members), "kept" if args.keep_identity else "re-authored on deploy"))
        elif args.command == "build":
            root = discover_root()
            out = args.out or _default_out(root, args.keep_identity)
            digest, count, receipt = build(root, out, keep_identity=args.keep_identity)
            print("RELEASE BUILT: %s" % out)
            print("RELEASE SHA256: %s" % digest)
            print("RELEASE MEMBERS: %d" % count)
            print("RECEIPT: %s" % receipt)
            print("IDENTITY: %s" % ("carried (same project moves)" if args.keep_identity
                                    else "left out (onboarding re-authors on the target)"))
        elif args.command == "verify":
            count, identity, cross = verify(args.archive)
            print("RELEASE VERIFIED: %d members" % count)
            print("IDENTITY: %s" % ("project.yaml present" if identity
                                    else "absent (template; onboarding re-authors)"))
            print("RECEIPT: %s" % cross)
        return 0
    except ReleaseError as exc:
        print("RELEASE ERROR: %s" % exc, file=sys.stderr)
        return 1
    except _policy.ShipmentPolicyError as exc:
        print("RELEASE BLOCKED: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
