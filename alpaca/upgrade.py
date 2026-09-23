"""alpaca upgrade: a crash-safe seed-versus-refresh promotion (M4.2).

An upgrade takes a source harness tree (a fresh copy of the harness code) and refreshes every
mechanism path of a live project from it, while leaving every memory path untouched. The two
classes are the ones the manifest states (alpaca/manifest.py): mechanism travels and is refreshed;
memory is this project's own runtime state and never travels. `project.yaml` is a mechanism path
by P-001 but it is human-owned and project-specific, so it is preserved, not clobbered; `CLAUDE.md`
is merged by its boot-block markers so a target repo's own house rules survive.

Crash-safety is the point. An upgrade runs as ordered phases, each recorded in a journal under
`.alpaca/upgrade/` before it acts. A staged build is assembled and shape-verified BEFORE any live
mechanism path is touched, the originals are copied to a backup first, and the promotion is the
single pivot: a kill at any phase before promotion is rolled back to the fully-old tree on the next
start, and a kill at or after promotion is rolled forward to the fully-new tree from the staged
build. Recovery runs on next start (through the CLI), never on demand, so a crashed upgrade cannot
leave a half tree behind a working CLI. The schema migration (M2.1) runs as one phase, with the
hash chain verified before and after, so `.alpaca/` survives unchanged in meaning.

A mechanism path the live manifest names and the release no longer names is dropped. The files
under it that still hold the bytes the installed release shipped (the live MANIFEST.json) are
removed at promotion; a file the project edited or added there stays, and the plan names both.

Interfaces (consumed by `alpaca doctor`, the CLI and the manual):

  plan(root, source)    -> Plan            read-only, writes nothing
  apply(root, source)   -> dict            the phased upgrade
  recover(root)         -> dict            roll back / roll forward a crashed upgrade
  status(root)          -> dict | None     the pending journal, for alpaca doctor
  pending(root)         -> bool
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field

from alpaca import manifest, paths, util

# CLAUDE.md is merged by its boot-block markers, never clobbered; project.yaml is human-owned and
# project-specific (P-001) so it is preserved. Everything else in the mechanism class is refreshed.
MERGE = {"CLAUDE.md", "AGENTS.md"}
# intents/ holds the project's own intent queue (onboarding and `op new --from-intent` write it),
# so an upgrade keeps the live copy and only seeds it from the source when the project has none.
PRESERVE = {"project.yaml", "intents"}

# The ordered phases. The journal records entry to each before it acts. `promote` is the pivot:
# a crash strictly before it rolls back to fully-old, a crash at or after it rolls forward to
# fully-new from the staged build.
PHASES = ("begin", "backup", "stage", "migrate", "promote", "done")
_FORWARD_FROM = ("promote", "done")

JOURNAL = "journal.json"


class UpgradeError(Exception):
    """Base for every refusal this module makes."""


class UpgradeLocked(UpgradeError):
    """A journal is already present: a prior upgrade is in progress or crashed and must be
    recovered on the next start before a new upgrade may begin."""


class ShapeCheckFailed(UpgradeError):
    """The staged build did not pass its shape verification, so it is never promoted."""


class _KillInjected(Exception):
    """Test-only: a simulated crash at a phase boundary. It propagates out of apply and leaves
    the journal, lock, staged build and backup in place, exactly as a real kill would."""


# --------------------------------------------------------------------------- plan
@dataclass
class Plan:
    """What an upgrade would do, computed read-only. `refresh` paths are overwritten from source,
    `merge` paths (CLAUDE.md) are merged by markers, `preserve` paths (project.yaml) are kept as
    they are, `memory` paths are never touched, and `modified` lists the refresh paths whose live
    content differs from the incoming source: a local edit reported before it is replaced.
    `dropped` lists the mechanism paths only the live manifest names; `remove` is the files under
    them that still hold the shipped bytes (removed at promotion), `dropped_kept` the rest."""

    mechanism: list = field(default_factory=list)
    memory: list = field(default_factory=list)
    refresh: list = field(default_factory=list)
    merge: list = field(default_factory=list)
    preserve: list = field(default_factory=list)
    modified: list = field(default_factory=list)
    missing_in_source: list = field(default_factory=list)
    dropped: list = field(default_factory=list)
    remove: list = field(default_factory=list)
    dropped_kept: list = field(default_factory=list)
    remove_sha: dict = field(default_factory=dict)


def _rel_norm(rel):
    return rel.rstrip("/")


def _tree_digest(path):
    """A content digest of a file or a directory tree, or None when the path is absent. Two paths
    with the same digest have identical bytes (dirs compared by relpath + content)."""
    if not os.path.lexists(path):
        return None
    if os.path.isdir(path) and not os.path.islink(path):
        parts = []
        for dp, dn, fn in os.walk(path):
            dn.sort()
            for f in sorted(fn):
                fp = os.path.join(dp, f)
                rel = os.path.relpath(fp, path)
                try:
                    with open(fp, "rb") as fh:
                        parts.append(rel + "\0" + util.sha256_hex(fh.read()))
                except OSError:
                    parts.append(rel + "\0?")
        return util.sha256_hex("\n".join(parts))
    try:
        with open(path, "rb") as fh:
            return util.sha256_hex(fh.read())
    except OSError:
        return None


def _differs(a, b):
    da, dbg = _tree_digest(a), _tree_digest(b)
    return da != dbg


def _classes_for(root, source, p):
    """Fill p.mechanism and p.memory from BOTH manifests. The incoming source manifest names what
    the new release ships, so a path a release adds (a new launcher, a new docs page) is installed
    on its first upgrade; reading only the live manifest would skip it until a second upgrade.
    A path only the live manifest names goes to p.dropped: the release no longer ships it, so it is
    neither staged nor refreshed (see _plan_dropped). A path either manifest calls memory is never
    treated as mechanism."""
    src, live = manifest.classes(source), manifest.classes(root)
    seen = set()
    for rel in src["memory"] + live["memory"]:
        if _rel_norm(rel) not in seen:
            seen.add(_rel_norm(rel))
            p.memory.append(rel)
    memory = set(seen)
    seen = set()
    for rel in src["mechanism"]:
        n = _rel_norm(rel)
        if n in seen or n in memory:
            continue
        seen.add(n)
        p.mechanism.append(rel)
    for rel in live["mechanism"]:
        n = _rel_norm(rel)
        if n in seen or n in memory:
            continue
        seen.add(n)
        p.dropped.append(rel)


def _under(rel, prefixes):
    return any(rel == q or rel.startswith(q + "/") for q in prefixes)


def _file_sha(path):
    try:
        with open(path, "rb") as fh:
            return util.sha256_hex(fh.read())
    except OSError:
        return None


def _plan_dropped(root, p):
    """Sort the files under each dropped path into p.remove (a regular file whose bytes are the ones
    the installed release shipped, per the live MANIFEST.json) and p.dropped_kept (everything else:
    a local edit, a project's own file, a symlink, or no MANIFEST.json to compare with). A file that
    also falls under a path the release still ships, or under memory, is left to those rules."""
    import json
    try:
        with open(os.path.join(root, "MANIFEST.json"), encoding="utf-8") as fh:
            shipped = json.load(fh).get("files")
    except (OSError, ValueError, AttributeError):
        shipped = None
    shipped = shipped if isinstance(shipped, dict) else {}
    keep_out = [_rel_norm(r) for r in p.mechanism] + [_rel_norm(r) for r in p.memory]
    real_root = os.path.realpath(root)
    for rel in p.dropped:
        n = _rel_norm(rel)
        if not n or os.path.isabs(n) or ".." in n.split("/"):
            continue
        full = os.path.join(root, n)
        if not os.path.realpath(full).startswith(real_root + os.sep):
            continue
        if os.path.isdir(full) and not os.path.islink(full):
            found = []
            for dp, dn, fn in os.walk(full):
                dn.sort()
                for f in sorted(fn):
                    found.append(os.path.relpath(os.path.join(dp, f), root).replace(os.sep, "/"))
        elif os.path.lexists(full):
            found = [n]
        else:
            found = []
        for f in found:
            if _under(f, keep_out):
                continue
            fp = os.path.join(root, f)
            sha = None if os.path.islink(fp) else _file_sha(fp)
            if sha is not None and shipped.get(f) == sha:
                p.remove.append(f)
                p.remove_sha[f] = sha
            else:
                p.dropped_kept.append(f)


def _remove_dropped(root, remove_sha, dropped):
    """Remove each listed file that still holds the recorded bytes, then every directory under a
    dropped path that is left empty. Idempotent, so a roll forward can run it again."""
    dirs = [_rel_norm(r) for r in dropped]
    for rel, sha in sorted(remove_sha.items()):
        fp = os.path.join(root, rel)
        if os.path.islink(fp) or not os.path.isfile(fp) or _file_sha(fp) != sha:
            continue
        os.remove(fp)
        d = os.path.dirname(rel)
        while d and _under(d, dirs):
            full = os.path.join(root, d)
            if not os.path.isdir(full) or os.path.islink(full) or os.listdir(full):
                break
            os.rmdir(full)
            d = os.path.dirname(d)


def plan(root, source) -> Plan:
    """Compute the upgrade plan. Read-only: this writes nothing to root or source."""
    source = os.path.abspath(source)
    p = Plan(mechanism=[], memory=[])
    _classes_for(root, source, p)
    _plan_dropped(root, p)
    for rel in p.mechanism:
        n = _rel_norm(rel)
        if n in PRESERVE:
            p.preserve.append(rel)
            continue
        if n in MERGE:
            p.merge.append(rel)
            continue
        p.refresh.append(rel)
        src = os.path.join(source, n)
        if not os.path.lexists(src):
            p.missing_in_source.append(rel)
            continue
        if _differs(os.path.join(root, n), src):
            p.modified.append(rel)
    return p


# --------------------------------------------------------------------------- scaffolding
def _upgrade_dir(root):
    return os.path.join(paths.runtime_dir(root), "upgrade")


def _journal_path(root):
    return os.path.join(_upgrade_dir(root), JOURNAL)


def _lock_path(root):
    return os.path.join(_upgrade_dir(root), "lock")


def _staged_dir(root):
    return os.path.join(_upgrade_dir(root), "staged")


def _backup_dir(root):
    return os.path.join(_upgrade_dir(root), "backup")


def _read_journal(root):
    import json
    try:
        with open(_journal_path(root), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_journal(root, obj):
    os.makedirs(_upgrade_dir(root), exist_ok=True)
    util.write_text(_journal_path(root), util.canonical_json(obj) + "\n")


def _clean_dir(path):
    if os.path.lexists(path):
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    os.makedirs(path, exist_ok=True)


def _copy_into(src_root, rel, dst_root):
    """Copy one mechanism path (a file or a whole directory) from src_root to dst_root, replacing
    whatever is at the destination. Returns False when the source path is absent."""
    n = _rel_norm(rel)
    src = os.path.join(src_root, n)
    dst = os.path.join(dst_root, n)
    if not os.path.lexists(src):
        return False
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    if os.path.lexists(dst):
        if os.path.isdir(dst) and not os.path.islink(dst):
            shutil.rmtree(dst)
        else:
            os.remove(dst)
    if os.path.isdir(src) and not os.path.islink(src):
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
    return True


def _read_or_empty(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


# --------------------------------------------------------------------------- staged build
def _build_staged(root, source, staged, p: Plan):
    """Assemble the final desired content of every mechanism path into the staged directory. The
    live tree is not touched here; only the staged copy is written, so it can be shape-verified
    before it is promoted."""
    for rel in p.mechanism:
        n = _rel_norm(rel)
        if n in PRESERVE:
            # human-owned and project-specific: the staged copy is the live one, so promotion
            # restores identical content and project.yaml survives unchanged.
            if os.path.lexists(os.path.join(root, n)):
                _copy_into(root, rel, staged)
            elif os.path.lexists(os.path.join(source, n)):
                _copy_into(source, rel, staged)
        elif n in MERGE:
            src_txt = _read_or_empty(os.path.join(source, n))
            root_txt = _read_or_empty(os.path.join(root, n))
            merged = manifest.merge_boot_block(root_txt, src_txt) if src_txt else root_txt
            util.write_text(os.path.join(staged, n), merged)
        else:
            if os.path.lexists(os.path.join(source, n)):
                _copy_into(source, rel, staged)
            elif os.path.lexists(os.path.join(root, n)):
                _copy_into(root, rel, staged)


def _shape_ok(root, source, staged, p: Plan):
    """Every mechanism path that exists in the live tree or the source must be present in the
    staged build before promotion; a staged build missing one is refused rather than promoted."""
    source = os.path.abspath(source)
    for rel in p.mechanism:
        n = _rel_norm(rel)
        if os.path.lexists(os.path.join(root, n)) or os.path.lexists(os.path.join(source, n)):
            if not os.path.lexists(os.path.join(staged, n)):
                return False, "staged build is missing %s" % rel
    return True, "%d mechanism paths staged" % len(p.mechanism)


def _migrate_phase(root):
    """Run the schema migration (M2.1) as one phase and verify the hash chain before and after,
    so the record survives the upgrade unchanged in meaning."""
    if not os.path.isfile(paths.db_path(root)):
        return                       # no record yet, nothing to migrate
    from alpaca import db, migrate
    conn = db.connect(root)
    try:
        ok, reason = db.verify_chain(conn)
        if not ok:
            raise UpgradeError("chain broken before migration: %s" % reason)
        migrate.apply(conn)
        ok, reason = db.verify_chain(conn)
        if not ok:
            raise UpgradeError("chain broken after migration: %s" % reason)
    finally:
        conn.close()


def _install(root, staged, rel):
    """Promote one staged mechanism path into the live tree. A single-path helper so a partial
    promotion (a kill mid-loop) is recovered by re-running the loop from the staged build."""
    _copy_into(staged, rel, root)


def _cleanup(root):
    ud = _upgrade_dir(root)
    if os.path.isdir(ud):
        shutil.rmtree(ud)


# --------------------------------------------------------------------------- apply
def apply(root, source, kill_at=None, clock=None):
    """Refresh every mechanism path of the project at root from the source harness tree, leaving
    every memory path untouched. Phased and journalled: a crash (or an injected kill) at any phase
    is recovered on the next start to a tree that is either fully old or fully new.

    kill_at is a test-only seam: it names a phase at whose entry a simulated crash is raised, after
    the journal for that phase is written but before the phase acts.
    """
    source = os.path.abspath(source)
    if pending(root):
        raise UpgradeLocked(
            "an upgrade journal is present; it is recovered on the next start, so recover before "
            "beginning a new upgrade")
    p = plan(root, source)
    staged = _staged_dir(root)
    backup = _backup_dir(root)

    def enter(phase):
        _write_journal(root, {"phase": phase,
                              "started": clock() if clock is not None else util.now_iso(),
                              "source": source, "mechanism": p.mechanism,
                              "dropped": p.dropped, "remove": p.remove_sha})
        if kill_at == phase:
            raise _KillInjected(phase)

    enter("begin")
    util.write_text(_lock_path(root),
                    (clock() if clock is not None else util.now_iso()) + "\n")

    enter("backup")
    _clean_dir(backup)
    for rel in p.mechanism + p.dropped:
        if os.path.lexists(os.path.join(root, _rel_norm(rel))):
            _copy_into(root, rel, backup)

    enter("stage")
    _clean_dir(staged)
    _build_staged(root, source, staged, p)
    ok, reason = _shape_ok(root, source, staged, p)
    if not ok:
        raise ShapeCheckFailed(reason)

    enter("migrate")
    _migrate_phase(root)

    enter("promote")
    for rel in p.mechanism:
        if os.path.lexists(os.path.join(staged, _rel_norm(rel))):
            _install(root, staged, rel)
    _remove_dropped(root, p.remove_sha, p.dropped)

    enter("done")
    _cleanup(root)
    return {"ok": True, "source": source, "plan": p,
            "refreshed": list(p.refresh), "preserved": list(p.preserve),
            "removed": list(p.remove)}


# --------------------------------------------------------------------------- recover
def pending(root) -> bool:
    return _read_journal(root) is not None


def status(root):
    """The pending journal (a dict) or None. Consumed by alpaca doctor to surface a crashed upgrade."""
    return _read_journal(root)


def _roll_back(root, journal):
    """A crash strictly before promotion: the live mechanism paths were never touched, so restore
    the originals from the backup (a no-op when the tree is already untouched) and discard the
    staged build. The tree ends fully old."""
    backup = _backup_dir(root)
    for rel in journal.get("mechanism", []) + journal.get("dropped", []):
        if os.path.lexists(os.path.join(backup, _rel_norm(rel))):
            _copy_into(backup, rel, root)


def _roll_forward(root, journal):
    """A crash at or after promotion: complete the promotion from the staged build (idempotent, so
    a half-done promotion is finished). The tree ends fully new."""
    staged = _staged_dir(root)
    for rel in journal.get("mechanism", []):
        if os.path.lexists(os.path.join(staged, _rel_norm(rel))):
            _install(root, staged, rel)
    _remove_dropped(root, journal.get("remove") or {}, journal.get("dropped", []))


def recover(root):
    """Recover a crashed upgrade. Runs on the next start (through the CLI), never on demand. With
    no journal it is a fast no-op. Otherwise it rolls back to the fully-old tree or forward to the
    fully-new tree by the recorded phase, then clears the scaffolding."""
    journal = _read_journal(root)
    if journal is None:
        return {"recovered": False, "reason": "no upgrade in progress"}
    phase = journal.get("phase")
    if phase in _FORWARD_FROM:
        _roll_forward(root, journal)
        direction = "forward"
    else:
        _roll_back(root, journal)
        direction = "back"
    _cleanup(root)
    return {"recovered": True, "direction": direction, "phase": phase}


# --------------------------------------------------------------------------- CLI
def _cmd_upgrade(args):
    from alpaca import cli
    from alpaca.gates import verdict as vc
    target = getattr(args, "target", None)
    if target:
        # Push mode: the running install upgrades another project. The source defaults to this
        # install, so `<new-install>/bin/alpaca upgrade --target <project>` rolls a release out.
        root = os.path.abspath(target)
        if not os.path.isfile(paths.manifest_path(root)):
            return vc.emit_verdict("alpaca-upgrade", vc.BLOCKED,
                                   "--target %s has no %s" % (root, paths.MANIFEST))
        if not getattr(args, "source", None):
            args.source = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if pending(root) and not getattr(args, "recover", False):
            recover(root)
    else:
        root = cli._root()
    if getattr(args, "recover", False):
        res = recover(root)
        detail = res.get("reason") or ("rolled %s from phase %s" % (res.get("direction"),
                                                                    res.get("phase")))
        return vc.emit_verdict("alpaca-upgrade-recover", vc.PASS, detail)
    source = getattr(args, "source", None)
    if not source:
        return vc.emit_verdict("alpaca-upgrade", vc.BLOCKED, "a --source harness tree is required")
    try:
        p = plan(root, source)
    except Exception as e:
        return vc.emit_verdict("alpaca-upgrade", vc.FAIL, "%s: %s" % (type(e).__name__, e))
    if getattr(args, "plan", False):
        for rel in p.modified:
            print("  local edit will be replaced: %s" % rel)
        for rel in p.remove:
            print("  dropped by the release, will be removed: %s" % rel)
        for rel in p.dropped_kept:
            print("  dropped by the release, kept (not the shipped bytes): %s" % rel)
        return vc.emit_verdict(
            "alpaca-upgrade-plan", vc.PASS,
            "refresh=%d merge=%d preserve=%d modified=%d remove=%d memory-untouched=%d" % (
                len(p.refresh), len(p.merge), len(p.preserve), len(p.modified), len(p.remove),
                len(p.memory)))
    try:
        apply(root, source)
    except UpgradeLocked as e:
        return vc.emit_verdict("alpaca-upgrade", vc.BLOCKED, str(e))
    except Exception as e:
        return vc.emit_verdict("alpaca-upgrade", vc.FAIL, "%s: %s" % (type(e).__name__, e))
    return vc.emit_verdict("alpaca-upgrade", vc.PASS,
                           "mechanism refreshed, memory untouched", evidence=[os.path.abspath(source)])


def _parser(sub):
    u = sub.add_parser(
        "upgrade",
        help="refresh every mechanism path from a source harness tree; memory is untouched")
    u.add_argument("--source", default=None, help="the source harness tree to refresh from")
    u.add_argument("--target", default=None,
                   help="upgrade this project root from the running install (push mode); "
                        "--source defaults to the running install")
    u.add_argument("--plan", action="store_true", help="print the plan and write nothing")
    u.add_argument("--recover", action="store_true",
                   help="recover a crashed upgrade now (it also runs automatically on next start)")


def _register():
    from alpaca import cli
    cli.command("upgrade")(_cmd_upgrade)
    cli.register_parser("upgrade", _parser)


_register()
