#!/usr/bin/env python3
"""Build, verify, reconstruct, compare, install, and roll back the harness shipment (M4.15).

Ported UNCHANGED IN PURPOSE from the earlier harness package_candidate.py and re-based onto Alpaca: the shipment
boundary is MANIFEST.json (the digest map gen_manifest.py writes over the manifest's mechanism
class), never Git tracking state. Archives contain exactly the manifest's file keys plus
MANIFEST.json. Tar members are path-sorted; uid/gid, names and mtimes are normalised; the gzip
timestamp and filename are absent, so a rebuild from the same tree is byte-for-byte equal.

Rename-safe: no folder-name literal and no absolute path baked in. The source, live, archive and
backup roots are explicit arguments (or derived from the runtime-discovered project root); the
shipment policy is loaded from the sibling `shipment_policy.py`.
"""
from __future__ import annotations

import argparse
import getpass
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time


HERE = Path(__file__).resolve().parent
MANIFEST_NAME = "MANIFEST.json"
#: Distribution identity is fixed in the trusted helper, independent of onboarding and folder name.
def _product_name(root):
    return "Alpaca"

EXPECTED_PRODUCT = _product_name(HERE.parent)
POLICY_NAME = "shipment_policy.py"
POLICY_PATH = HERE / POLICY_NAME
POLICY_BYTES = POLICY_PATH.read_bytes()
_POLICY_SPEC = importlib.util.spec_from_file_location("alpaca_shipment_policy", POLICY_PATH)
if _POLICY_SPEC is None or _POLICY_SPEC.loader is None:
    raise RuntimeError("cannot load shipment policy: %s" % POLICY_PATH)
_shipment_policy = importlib.util.module_from_spec(_POLICY_SPEC)
_POLICY_SPEC.loader.exec_module(_shipment_policy)

_HARNESS_USAGE = 64  # verdict.USAGE; restated (not imported) so this helper runs detached.


class PackageError(RuntimeError):
    pass


class BlockError(PackageError):
    """A cutover/rollback that could NOT be adjudicated -- an absent store, an empty or absent
    recovery set, a backup whose bytes fail their recorded digest. It BLOCKS (exit 2); it is NEVER
    laundered into a green result. 'nothing was restored' is not a successful rollback."""


class CutoverRolledBack(PackageError):
    """The switch already happened and then verification FAILED, so the live tree was AUTO-ROLLED
    BACK to its pre-apply state. Distinct from a pristine pre-switch refusal."""


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_rel(raw):
    rel = PurePosixPath(raw)
    if not raw or raw == MANIFEST_NAME or rel.is_absolute() or ".." in rel.parts:
        raise PackageError("unsafe or reserved manifest path: %r" % raw)
    if raw != rel.as_posix() or any(part in ("", ".") for part in rel.parts):
        raise PackageError("non-canonical manifest path: %r" % raw)
    return rel


def read_manifest_bytes(raw):
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageError("invalid MANIFEST.json: %s" % exc) from exc
    files = manifest.get("files")
    modes = manifest.get("file_modes")
    if not isinstance(files, dict) or not isinstance(modes, dict):
        raise PackageError("MANIFEST.json must contain files and file_modes maps")
    if set(files) != set(modes):
        raise PackageError("manifest files/file_modes key sets differ")
    if manifest.get("file_count") != len(files):
        raise PackageError("manifest file_count does not equal files map size")
    for raw_path, digest in files.items():
        safe_rel(raw_path)
        if not isinstance(digest, str) or len(digest) != 64:
            raise PackageError("invalid sha256 for %s" % raw_path)
        marker = modes[raw_path]
        if not isinstance(marker, str) or not marker.startswith("f") or len(marker) != 5:
            raise PackageError("unsupported type/mode for %s: %r" % (raw_path, marker))
        try:
            int(marker[1:], 8)
        except ValueError as exc:
            raise PackageError("invalid mode for %s: %r" % (raw_path, marker)) from exc
    return manifest


def read_manifest(tree):
    raw = (Path(tree) / MANIFEST_NAME).read_bytes()
    return read_manifest_bytes(raw), raw


def expected_mode(manifest, rel):
    return int(manifest["file_modes"][rel][1:], 8)


#: mode bits a shipped file never carries (setup/gen_manifest.py UNSAFE_BITS).
UNSAFE_BITS = stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX


def same_exec_bit(got_mode, want_mode):
    """True when the owner exec bits agree, the one bit git keeps (setup/gen_manifest.py
    _type_mode): a checkout under umask 0002 gives 0664/0775 files, which are not drift."""
    return bool(got_mode & stat.S_IXUSR) == bool(want_mode & stat.S_IXUSR)


def mode_ok(got_mode, want_mode):
    """A file mode that matches its recorded mode: the same owner exec bit and no unsafe bit
    (other-write, setuid, setgid, sticky). Group write alone is the checkout's umask."""
    return same_exec_bit(got_mode, want_mode) and not got_mode & UNSAFE_BITS


def file_marker(mode):
    """The marker gen_manifest gives a regular file of this mode: f0755 or f0644 from the owner
    exec bit, plus any unsafe bit (so a world-writable file never matches a recorded f0644)."""
    return "f%04o" % ((0o755 if mode & stat.S_IXUSR else 0o644) | (mode & UNSAFE_BITS))


def norm_marker(marker):
    """A manifest marker as gen_manifest compares it today (older full modes such as f0664 keep
    only the exec bit and the unsafe bits)."""
    if isinstance(marker, str) and marker.startswith("f") and len(marker) == 5:
        try:
            return file_marker(int(marker[1:], 8))
        except ValueError:
            pass
    return marker


def verify_selected_tree(tree, exact=False):
    tree = Path(tree)
    manifest, _ = read_manifest(tree)
    errors = []
    expected = set(manifest["files"])
    for rel in sorted(expected):
        path = tree / Path(*PurePosixPath(rel).parts)
        try:
            status = path.lstat()
        except FileNotFoundError:
            errors.append("missing: %s" % rel)
            continue
        if not stat.S_ISREG(status.st_mode):
            errors.append("not a regular file: %s" % rel)
            continue
        if sha_file(path) != manifest["files"][rel]:
            errors.append("hash mismatch: %s" % rel)
        got_mode = stat.S_IMODE(status.st_mode)
        want_mode = expected_mode(manifest, rel)
        if not mode_ok(got_mode, want_mode):
            errors.append("mode mismatch: %s (%04o != %04o)" % (rel, got_mode, want_mode))
    if exact:
        actual = set()
        for base, dirs, names in os.walk(tree):
            dirs[:] = [name for name in dirs if name != "__pycache__"]
            for name in names:
                if name.endswith(".pyc"):
                    continue
                actual.add((Path(base) / name).relative_to(tree).as_posix())
        wanted = expected | {MANIFEST_NAME}
        for rel in sorted(actual - wanted):
            errors.append("unexpected file: %s" % rel)
        for rel in sorted(wanted - actual):
            errors.append("missing package member: %s" % rel)
    if errors:
        raise PackageError("selected-tree verification failed:\n  " + "\n  ".join(errors))
    return manifest


def run_manifest_verifier(tree):
    tree = Path(tree)
    gen = tree / "setup" / "gen_manifest.py"
    if not gen.is_file():
        gen = tree / "gen_manifest.py"
    proc = subprocess.run(
        [sys.executable, str(gen), "--verify", "--root", str(tree)],
        cwd=str(tree), text=True, encoding="utf-8", capture_output=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode:
        raise PackageError("gen_manifest.py --verify exited %d:\n%s" % (proc.returncode, output))
    return output


def normalized_tarinfo(rel, mode, size):
    info = tarfile.TarInfo(rel)
    info.type = tarfile.REGTYPE
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.size = size
    return info


def build_archive(source, archive, receipt):
    source, archive, receipt = Path(source), Path(archive), Path(receipt)
    manifest, manifest_raw = read_manifest(source)
    verify_selected_tree(source)
    run_manifest_verifier(source)
    members = sorted(set(manifest["files"]) | {MANIFEST_NAME})
    if (source / POLICY_NAME).is_file() and (source / POLICY_NAME).read_bytes() != POLICY_BYTES:
        raise PackageError("source shipment policy differs from the helper's shipped policy")

    def build_mutation():
        # First mutation: policy validation has completed before any path is created.
        archive.parent.mkdir(parents=True, exist_ok=True)
        temporary = archive.with_name(archive.name + ".tmp")
        try:
            with temporary.open("wb") as raw_out:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw_out, mtime=0) as gz_out:
                    with tarfile.open(fileobj=gz_out, mode="w",
                                      format=tarfile.USTAR_FORMAT) as tar:
                        for rel in members:
                            if rel == MANIFEST_NAME:
                                data = manifest_raw
                                mode = 0o644
                            else:
                                data = (source / Path(*PurePosixPath(rel).parts)).read_bytes()
                                mode = expected_mode(manifest, rel)
                            tar.addfile(normalized_tarinfo(rel, mode, len(data)),
                                        io.BytesIO(data))
            os.replace(temporary, archive)
        finally:
            temporary.unlink(missing_ok=True)
        digest = sha_file(archive)
        receipt.write_text("%s  %s\n" % (digest, archive.name), encoding="utf-8", newline="\n")
        return digest, len(members)

    result, _validated = _shipment_policy.prewrite_shipment_apply(members, build_mutation)
    return result


def verified_archive(archive):
    archive = Path(archive)
    try:
        with tarfile.open(archive, mode="r:gz") as tar:
            members = tar.getmembers()
            names = [member.name for member in members]
            if len(names) != len(set(names)):
                raise PackageError("archive contains duplicate member names")
            if names != sorted(names):
                raise PackageError("archive members are not path-sorted")
            _shipment_policy.validate_staged_paths(names)
            by_name = {member.name: member for member in members}
            if MANIFEST_NAME not in by_name:
                raise PackageError("archive has no MANIFEST.json")
            manifest_member = by_name[MANIFEST_NAME]
            if not manifest_member.isfile():
                raise PackageError("archive MANIFEST.json is not a regular file")
            manifest_stream = tar.extractfile(manifest_member)
            if manifest_stream is None:
                raise PackageError("archive MANIFEST.json cannot be read")
            manifest_raw = manifest_stream.read()
            manifest = read_manifest_bytes(manifest_raw)
            expected_names = set(manifest["files"]) | {MANIFEST_NAME}
            if set(names) != expected_names:
                missing = sorted(expected_names - set(names))
                extra = sorted(set(names) - expected_names)
                raise PackageError("archive member-set mismatch; missing=%s, extra=%s"
                                   % (missing, extra))
            content = {}
            errors = []
            for rel in names:
                member = by_name[rel]
                if not member.isfile():
                    errors.append("non-regular member: %s" % rel)
                    continue
                if (member.uid, member.gid, member.uname, member.gname, member.mtime) \
                        != (0, 0, "", "", 0):
                    errors.append("non-normalized metadata: %s" % rel)
                stream = tar.extractfile(member)
                if stream is None:
                    errors.append("unreadable member: %s" % rel)
                    continue
                data = stream.read()
                content[rel] = data
                if rel == MANIFEST_NAME:
                    if member.mode != 0o644:
                        errors.append("manifest archive mode mismatch: %04o" % member.mode)
                else:
                    if sha_bytes(data) != manifest["files"][rel]:
                        errors.append("member hash mismatch: %s" % rel)
                    want_mode = expected_mode(manifest, rel)
                    if member.mode != want_mode:
                        errors.append("member mode mismatch: %s (%04o != %04o)"
                                      % (rel, member.mode, want_mode))
            if errors:
                raise PackageError("archive verification failed:\n  " + "\n  ".join(errors))
            if content.get(POLICY_NAME) is not None and content.get(POLICY_NAME) != POLICY_BYTES:
                raise PackageError("archive shipment policy differs from the shipped policy")
            derived_lines = ["%s  %s\n" % (sha_bytes(content[rel]), rel)
                             for rel in sorted(content) if rel != MANIFEST_NAME]
            derived_tree_digest = sha_bytes("".join(derived_lines).encode("utf-8"))
            claimed = manifest.get("tree_digest")
            if not isinstance(claimed, str) or claimed != derived_tree_digest:
                raise PackageError(
                    "archive tree_digest does not describe the archived bytes: manifest claims "
                    "%r, the archived bytes re-derive %r" % (claimed, derived_tree_digest))
            if manifest.get("product") != EXPECTED_PRODUCT:
                raise PackageError("archive product %r is not this distribution (expected %r)"
                                   % (manifest.get("product"), EXPECTED_PRODUCT))
            return manifest, manifest_raw, content
    except (tarfile.TarError, OSError) as exc:
        raise PackageError("cannot read archive: %s" % exc) from exc


def cross_check_receipt(archive):
    """Consult the co-located `<archive>.sha256` receipt, if it resolves, and confirm it attests
    THIS archive's bytes. Returns the receipt path on a clean cross-check, or None when no receipt
    sits beside the archive. A co-located receipt is provenance corroboration, not an external
    trust root (the disclosed residual)."""
    archive = Path(archive)
    receipt = archive.with_name(archive.name + ".sha256")
    if not receipt.is_file():
        return None
    raw = receipt.read_text(encoding="utf-8").strip()
    claimed = raw.split()[0] if raw else ""
    if len(claimed) != 64:
        raise PackageError("receipt %s does not carry a sha256 digest" % receipt.name)
    actual = sha_file(archive)
    if claimed != actual:
        raise PackageError("archive sha256 does not match its receipt %s: receipt claims %s, "
                           "archive is %s" % (receipt.name, claimed, actual))
    return str(receipt)


def extract_archive(archive, destination):
    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise PackageError("extraction destination is not empty: %s" % destination)
    manifest, _manifest_raw, content = verified_archive(archive)
    destination.mkdir(parents=True, exist_ok=True)
    for rel in sorted(content):
        target = destination / Path(*PurePosixPath(rel).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content[rel])
        os.chmod(target, 0o644 if rel == MANIFEST_NAME else expected_mode(manifest, rel))
    verify_selected_tree(destination, exact=True)
    output = run_manifest_verifier(destination)
    return manifest, output


def actual_marker(path):
    try:
        status = Path(path).lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISREG(status.st_mode):
        return file_marker(stat.S_IMODE(status.st_mode))
    if stat.S_ISLNK(status.st_mode):
        return "l"
    return "?%04o" % stat.S_IMODE(status.st_mode)


def cutover_plan(archive, live):
    live = Path(live)
    manifest, manifest_raw, content = verified_archive(archive)
    plan = []
    for rel in sorted(manifest["files"]):
        target = live / Path(*PurePosixPath(rel).parts)
        marker = actual_marker(target)
        if marker is None:
            plan.append(("NEW", rel))
        elif marker != norm_marker(manifest["file_modes"][rel]) or sha_file(target) != manifest["files"][rel]:
            plan.append(("CHANGED", rel))
    live_manifest = live / MANIFEST_NAME
    if not live_manifest.exists():
        plan.append(("NEW", MANIFEST_NAME))
    elif live_manifest.read_bytes() != manifest_raw or actual_marker(live_manifest) != "f0644":
        plan.append(("CHANGED", MANIFEST_NAME))
    return manifest, manifest_raw, content, plan


def assert_live_overwrites_are_audited(live, plan):
    live = Path(live)
    live_manifest_path = live / MANIFEST_NAME
    if not live_manifest_path.is_file():
        raise PackageError("live MANIFEST.json is absent; cannot establish overwrite baseline")
    old, _ = read_manifest(live)
    errors = []
    for kind, rel in plan:
        if rel == MANIFEST_NAME or kind == "NEW":
            continue
        target = live / Path(*PurePosixPath(rel).parts)
        if rel not in old["files"]:
            errors.append("existing collision absent from live manifest: %s" % rel)
            continue
        if actual_marker(target) != norm_marker(old.get("file_modes", {}).get(rel)):
            errors.append("live mode/type drift from baseline: %s" % rel)
        elif sha_file(target) != old["files"][rel]:
            errors.append("live hash drift from baseline: %s" % rel)
    for kind, rel in plan:
        if kind == "NEW" and (live / Path(*PurePosixPath(rel).parts)).exists():
            errors.append("new-path collision: %s" % rel)
    if errors:
        raise PackageError("cutover refused unaudited live overwrite:\n  " + "\n  ".join(errors))


def print_plan(plan):
    changed = sum(kind == "CHANGED" for kind, _ in plan)
    new = sum(kind == "NEW" for kind, _ in plan)
    print("CUTOVER PLAN: %d changed, %d new, %d total" % (changed, new, len(plan)))
    for kind, rel in plan:
        print("  %-7s %s" % (kind, rel))


def _actor():
    for getter in (
        lambda: os.environ.get("ALPACA_ACTOR"),
        getpass.getuser,
        lambda: os.environ.get("USER") or os.environ.get("USERNAME"),
    ):
        try:
            value = getter()
        except Exception:
            value = None
        if value:
            return str(value)
    return "unknown"


def _ledger_path(backup_root):
    return Path(backup_root) / "cutover-ledger.jsonl"


def _append_ledger(backup_root, entry):
    backup_root = Path(backup_root)
    backup_root.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), "pid": os.getpid()}
    record.update(entry)
    line = json.dumps(record, sort_keys=True) + "\n"
    with _ledger_path(backup_root).open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(line)


def _rel_of(row_path):
    return PurePosixPath(MANIFEST_NAME) if row_path == MANIFEST_NAME else safe_rel(row_path)


def _validate_recovery_set(backup, rows, digest_check):
    backup = Path(backup)
    expected = 0
    for row in rows:
        kind = row.get("kind")
        rel = _rel_of(row["path"])
        if kind == "NEW":
            expected += 1
        elif kind == "CHANGED":
            source = backup / "files" / Path(*rel.parts)
            if not source.is_file():
                raise BlockError("recovery set incomplete: backup is missing changed path %s -- "
                                 "a rollback that cannot restore every path BLOCKS" % rel)
            if digest_check:
                want = row.get("backup_sha256")
                if not isinstance(want, str) or len(want) != 64:
                    raise BlockError("backup for %s carries no recorded digest -- its integrity "
                                     "cannot be checked, so the rollback BLOCKS" % rel)
                got = sha_file(source)
                if got != want:
                    raise BlockError("backup byte-set for %s FAILS its recorded digest (recorded "
                                     "%s, backup is %s) -- the backup is corrupted; BLOCKED"
                                     % (rel, want, got))
            expected += 1
        else:
            raise PackageError("invalid rollback kind: %r" % kind)
    return expected


def _restore_recovery_set(backup, live, rows):
    backup, live = Path(backup), Path(live)
    restored = 0
    for row in rows:
        kind = row.get("kind")
        rel = _rel_of(row["path"])
        target = live / Path(*rel.parts)
        if kind == "NEW":
            target.unlink(missing_ok=True)
            restored += 1
        elif kind == "CHANGED":
            source = backup / "files" / Path(*rel.parts)
            if not source.is_file():
                raise BlockError("recovery set incomplete: backup missing changed path %s" % rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target, follow_symlinks=False)
            restored += 1
        else:
            raise PackageError("invalid rollback kind: %r" % kind)
    return restored


def apply_cutover(archive, live, backup_root, redirect_note=None):
    archive, live, backup_root = Path(archive), Path(live), Path(backup_root)
    manifest, manifest_raw, content, plan = cutover_plan(archive, live)
    assert_live_overwrites_are_audited(live, plan)
    print_plan(plan)

    def apply_mutation():
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-%d" % os.getpid()
        backup = backup_root / stamp
        backup.mkdir(parents=True, exist_ok=False)
        actor = _actor()
        rows = []
        for kind, rel in plan:
            row = {"kind": kind, "path": rel}
            if kind == "CHANGED":
                source = live / Path(*PurePosixPath(rel).parts)
                target = backup / "files" / Path(*PurePosixPath(rel).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target, follow_symlinks=False)
                row["backup_sha256"] = sha_file(target)
            rows.append(row)
        metadata = {
            "actor": actor, "archive": archive.name, "archive_sha256": sha_file(archive),
            "live": str(live.resolve()), "paths": rows,
        }
        if redirect_note:
            metadata["redirect_note"] = redirect_note
        (backup / "receipt.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        _append_ledger(backup_root, {
            "event": "cutover-apply", "actor": actor, "archive": archive.name,
            "archive_sha256": metadata["archive_sha256"], "live": metadata["live"],
            "backup": backup.name, "changed": sum(k == "CHANGED" for k, _ in plan),
            "new": sum(k == "NEW" for k, _ in plan), "redirect_note": redirect_note or ""})

        try:
            for kind, rel in plan:
                target = live / Path(*PurePosixPath(rel).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                data = manifest_raw if rel == MANIFEST_NAME else content[rel]
                mode = 0o644 if rel == MANIFEST_NAME else expected_mode(manifest, rel)
                with tempfile.NamedTemporaryFile(
                        dir=target.parent, prefix="." + target.name + ".", delete=False) as stream:
                    temp = Path(stream.name)
                    stream.write(data)
                try:
                    os.chmod(temp, mode)
                    os.replace(temp, target)
                except OSError:
                    temp.unlink(missing_ok=True)
                    raise
            verify_selected_tree(live)
            output = run_manifest_verifier(live)
        except (PackageError, OSError) as switch_exc:
            _restore_recovery_set(backup, live, rows)
            _append_ledger(backup_root, {
                "event": "cutover-apply-rolled-back", "actor": actor, "backup": backup.name,
                "live": metadata["live"], "reason": str(switch_exc),
                "redirect_note": redirect_note or ""})
            raise CutoverRolledBack(
                "cutover FAILED during the switch or its verification; the live tree was "
                "AUTO-ROLLED BACK to its pre-apply state from the backup (fully restored -- not "
                "half-switched, not replaced-and-unverified): %s" % switch_exc) from switch_exc

        _append_ledger(backup_root, {
            "event": "cutover-apply-verified", "actor": actor, "backup": backup.name,
            "live": metadata["live"], "redirect_note": redirect_note or ""})
        print(output)
        print("CUTOVER APPLIED; backup=%s" % backup)
        return backup

    result, _validated = _shipment_policy.prewrite_shipment_apply(content.keys(), apply_mutation)
    return result


def rollback_cutover(live, backup_root):
    live, backup_root = Path(live), Path(backup_root)
    backups = sorted((p for p in backup_root.iterdir() if p.is_dir()), reverse=True) \
        if backup_root.exists() else []
    if not backups:
        raise BlockError("no rollback backup under %s -- BLOCKED, cannot adjudicate" % backup_root)
    backup = backups[0]
    receipt_path = backup / "receipt.json"
    if not receipt_path.is_file():
        raise BlockError("backup %s has no receipt.json -- cannot adjudicate a rollback"
                         % backup.name)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    claimed_live = receipt.get("live")
    if not claimed_live or Path(claimed_live).resolve() != live.resolve():
        raise PackageError("backup belongs to a different live root")
    if "paths" not in receipt:
        raise BlockError("rollback recovery set is ABSENT (receipt has no 'paths' key) -- a "
                         "rollback that restores nothing is not a success; BLOCKED")
    rows = receipt["paths"]
    if not isinstance(rows, list):
        raise BlockError("rollback recovery set is malformed (paths is not a list) -- BLOCKED")
    if len(rows) == 0:
        raise BlockError("rollback recovery set is EMPTY (zero paths) -- restoring nothing while "
                         "reporting success is a false green; BLOCKED")
    expected = _validate_recovery_set(backup, rows, digest_check=True)
    restored = _restore_recovery_set(backup, live, rows)
    if restored != expected:
        raise BlockError("rollback restored %d of %d recovery paths -- cardinality mismatch; a "
                         "rollback that did not restore its whole recovery set BLOCKS"
                         % (restored, expected))
    verify_selected_tree(live)
    output = run_manifest_verifier(live)
    print(output)
    print("CUTOVER ROLLED BACK from %s (%d path(s) restored)" % (backup, restored))
    return backup


class _BandParser(argparse.ArgumentParser):
    """Usage error and --help route into the reserved harness band (64), never onto a verdict-band
    code. Restated (not imported) so this helper runs detached, where `import verdict` does not
    resolve; it restates only the harness code 64, never the verdict<->code map."""

    def error(self, message):
        sys.stderr.write("HARNESS-ERROR %s [USAGE]: %s\n" % (self.prog, message))
        raise SystemExit(_HARNESS_USAGE)

    def exit(self, status=0, message=None):
        if message:
            sys.stderr.write(message)
        raise SystemExit(_HARNESS_USAGE)


def parser():
    ap = _BandParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)

    vs = sub.add_parser("verify-source")
    vs.add_argument("--source", type=Path, required=True)

    build = sub.add_parser("build")
    build.add_argument("--source", type=Path, required=True)
    build.add_argument("--archive", type=Path, required=True)
    build.add_argument("--receipt", type=Path, default=None)

    va = sub.add_parser("verify-archive")
    va.add_argument("--archive", type=Path, required=True)

    extract = sub.add_parser("extract")
    extract.add_argument("destination", type=Path)
    extract.add_argument("--archive", type=Path, required=True)

    changes = sub.add_parser("changes")
    changes.add_argument("--archive", type=Path, required=True)
    changes.add_argument("--live", type=Path, required=True)

    apply = sub.add_parser("apply")
    apply.add_argument("--archive", type=Path, required=True)
    apply.add_argument("--live", type=Path, required=True)
    apply.add_argument("--backup-root", type=Path, required=True)
    apply.add_argument("--redirect-note", default=None)

    rollback = sub.add_parser("rollback")
    rollback.add_argument("--live", type=Path, required=True)
    rollback.add_argument("--backup-root", type=Path, required=True)
    return ap


def main():
    args = parser().parse_args()
    try:
        if args.command == "verify-source":
            manifest = verify_selected_tree(args.source.resolve())
            run_manifest_verifier(args.source.resolve())
            print("SOURCE VERIFIED: %d selected files; tree=%s"
                  % (len(manifest["files"]), manifest["tree_digest"]))
        elif args.command == "build":
            source = args.source.resolve()
            archive = args.archive.resolve()
            receipt = (args.receipt or Path(str(args.archive) + ".sha256")).resolve()
            digest, count = build_archive(source, archive, receipt)
            print("ARCHIVE BUILT: %s" % archive)
            print("ARCHIVE SHA256: %s" % digest)
            print("ARCHIVE MEMBERS: %d" % count)
        elif args.command == "verify-archive":
            archive = args.archive.resolve()
            manifest, _raw, content = verified_archive(archive)
            receipt = cross_check_receipt(archive)
            print("ARCHIVE VERIFIED: %d members; %d selected files"
                  % (len(content), len(manifest["files"])))
            print("PRODUCT: %s" % manifest["product"])
            print("TREE DIGEST: %s (re-derived from archived bytes)" % manifest["tree_digest"])
            print("RECEIPT: %s" % ("%s matches (co-located; not an external anchor)"
                                   % os.path.basename(receipt) if receipt
                                   else "none co-located (external attestation is the residual)"))
        elif args.command == "extract":
            manifest, output = extract_archive(args.archive.resolve(), args.destination.resolve())
            print(output)
            print("PACKAGE RECONSTRUCTED: %d members at %s"
                  % (len(manifest["files"]) + 1, args.destination.resolve()))
        elif args.command == "changes":
            _m, _raw, _content, plan = cutover_plan(args.archive.resolve(), args.live.resolve())
            assert_live_overwrites_are_audited(args.live.resolve(), plan)
            print_plan(plan)
        elif args.command == "apply":
            apply_cutover(args.archive.resolve(), args.live.resolve(),
                          args.backup_root.resolve(), redirect_note=args.redirect_note)
        elif args.command == "rollback":
            rollback_cutover(args.live.resolve(), args.backup_root.resolve())
        return 0
    except CutoverRolledBack as exc:
        print("CUTOVER FAILED -- AUTO-ROLLED BACK (tree restored to pre-apply state): %s" % exc,
              file=sys.stderr)
        return 1
    except BlockError as exc:
        print("CUTOVER BLOCKED: %s" % exc, file=sys.stderr)
        return 2
    except (PackageError, OSError, KeyError, ValueError) as exc:
        print("PACKAGE ERROR: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
