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
  project_lock(root)    -> context manager one upgrade of a project at a time (apply and recover take it)
  busy(root)            -> bool            whether another process holds that lock now
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import os
import shutil
import stat
import threading
from dataclasses import dataclass, field

from alpaca import manifest, paths, util

# CLAUDE.md is merged by its boot-block markers, never clobbered; project.yaml is human-owned and
# project-specific (P-001) so it is preserved. Everything else in the mechanism class is refreshed.
MERGE = {"CLAUDE.md", "AGENTS.md"}
# intents/ holds the project's own intent queue (onboarding and `op new --from-intent` write it),
# so an upgrade keeps the live copy and only seeds it from the source when the project has none.
# README.md carries the project's name once `alpaca new` fills its name block, so it is kept too.
PRESERVE = {"project.yaml", "intents", "README.md"}

# The ordered phases. The journal records entry to each before it acts. `promote` is the pivot:
# a crash strictly before it rolls back to fully-old, a crash at or after it rolls forward to
# fully-new from the staged build.
PHASES = ("begin", "backup", "stage", "migrate", "promote", "done")
_FORWARD_FROM = ("promote", "done")

JOURNAL = "journal.json"


class UpgradeError(Exception):
    """Base for every refusal this module makes."""


class UpgradeLocked(UpgradeError):
    """A journal is already present (a prior upgrade crashed and must be recovered on the next
    start before a new upgrade may begin), or another process holds the project lock."""


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
    they are, `memory` paths are never touched. `modified` lists the refresh paths that differ from
    the incoming source AND hold a local edit (a changed, deleted or project-added file
    under it, judged against the live MANIFEST.json): reported before it is replaced. `changed`
    lists the refresh paths that differ only because the new release changed them: every live file
    still holds the bytes the installed release shipped. `local_files` names the live files
    behind `modified` and `merge_edited`, so a caller can save them first. `merge_edited` lists the
    merge paths (CLAUDE.md, AGENTS.md) whose boot block holds text the incoming block does not:
    the merge replaces that block, so the file counts as a local edit. `merge_links` lists the merge
    paths that are symlinks (`merge_link_to` maps each to its target text): the link is kept as it is
    and never written through; its target is merged on its own when it is a merge path too.
    `dropped` lists the mechanism paths only the live manifest names; `remove` is the files under
    them that still hold the shipped bytes (removed at promotion), `dropped_kept` the rest."""

    mechanism: list = field(default_factory=list)
    memory: list = field(default_factory=list)
    refresh: list = field(default_factory=list)
    merge: list = field(default_factory=list)
    preserve: list = field(default_factory=list)
    modified: list = field(default_factory=list)
    merge_edited: list = field(default_factory=list)
    merge_links: list = field(default_factory=list)
    merge_link_to: dict = field(default_factory=dict)
    changed: list = field(default_factory=list)
    local_files: list = field(default_factory=list)
    deleted_files: list = field(default_factory=list)
    missing_in_source: list = field(default_factory=list)
    dropped: list = field(default_factory=list)
    remove: list = field(default_factory=list)
    dropped_kept: list = field(default_factory=list)
    remove_sha: dict = field(default_factory=dict)


def _rel_norm(rel):
    return rel.rstrip("/")


# --------------------------------------------------------------------------- the project lock
#: held[(realpath of root, thread id)] = [file descriptor, depth]: one lock per project and thread, so
#: apply inside a caller that holds it (alpaca update) does not wait for itself, while another thread
#: or process does.
_held = {}
_BUSY_ERRNOS = (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES)


def lock_path(root):
    """The project lock file. It sits beside upgrade/, not in it: the scaffolding folder is removed
    when an upgrade ends, and a lock file that is deleted while held lets a third process lock a new
    one. The file itself is never removed."""
    return os.path.join(paths.runtime_dir(root), "upgrade.lock")


@contextlib.contextmanager
def project_lock(root):
    """Hold the project's upgrade lock (an exclusive, non-blocking flock): one update or upgrade of a
    project at a time. Raises UpgradeLocked when another process (or thread) holds it, UpgradeError
    when the lock cannot be taken at all (fails closed). It is re-entrant inside one thread."""
    key = (os.path.realpath(root), threading.get_ident())
    held = _held.get(key)
    if held is not None:
        held[1] += 1
        try:
            yield
        finally:
            held[1] -= 1
        return
    path = lock_path(root)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as e:
        raise UpgradeError("cannot open the project lock %s: %s; nothing was changed" % (path, e))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in _BUSY_ERRNOS:
            raise UpgradeLocked("another alpaca update or upgrade of %s is running (it holds %s); nothing "
                                "was changed" % (root, path))
        raise UpgradeError("cannot lock %s: %s; nothing was changed" % (path, e))
    _held[key] = [fd, 1]
    try:
        yield
    finally:
        _held.pop(key, None)
        os.close(fd)                 # closing the descriptor releases the lock


def busy(root):
    """True when another process (or thread) holds the project lock now. A missing lock file is not
    busy, and nothing is created."""
    try:
        fd = os.open(lock_path(root), os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        return e.errno in _BUSY_ERRNOS
    finally:
        os.close(fd)
    return False


# --------------------------------------------------------------------------- content digests
def _regular_bytes(path, follow=False):
    """The bytes of a regular file, or None for anything else (a named pipe, a device, a link, a
    folder) and for a file that cannot be read. It never blocks: the file is opened O_NONBLOCK, so a
    named pipe with no writer opens at once and is then seen to be no regular file. A link is not
    followed unless `follow`."""
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0) | (0 if follow else getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _entry_digest(path):
    """What one path contributes to a digest: the sha256 of a regular file's bytes, `symlink:<target>`
    for a link (the MANIFEST.json convention), `special` for a named pipe, device or socket, `?` for
    a regular file that cannot be read. Never opens anything that is not a regular file."""
    if os.path.islink(path):
        try:
            return "symlink:" + os.readlink(path)
        except OSError:
            return "?"
    data = _regular_bytes(path)
    if data is not None:
        return util.sha256_hex(data)
    try:
        return "?" if stat.S_ISREG(os.lstat(path).st_mode) else "special"
    except OSError:
        return "?"


def _tree_digest(path):
    """A content digest of a file or a directory tree, or None when the path is absent. Two paths
    with the same digest have identical bytes (dirs compared by relpath + content); a link counts by
    its target text and a special file by its kind, and neither is opened."""
    if not os.path.lexists(path):
        return None
    if os.path.isdir(path) and not os.path.islink(path):
        parts = []
        for dp, dn, fn in os.walk(path):
            dn.sort()
            for f in sorted(fn + [d for d in dn if os.path.islink(os.path.join(dp, d))]):
                fp = os.path.join(dp, f)
                parts.append(os.path.relpath(fp, path) + "\0" + _entry_digest(fp))
        return util.sha256_hex("\n".join(parts))
    token = _entry_digest(path)
    return None if token == "?" else token


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
    """The sha256 of a regular file's bytes; None for a link, a named pipe, a folder or a file that
    cannot be read."""
    data = _regular_bytes(path)
    return None if data is None else util.sha256_hex(data)


def _shipped_shas(root):
    """{relative path: sha256} the installed release shipped, from the live MANIFEST.json; {} when
    there is none or it cannot be read."""
    import json
    try:
        with open(os.path.join(root, "MANIFEST.json"), encoding="utf-8") as fh:
            shipped = json.load(fh).get("files")
    except (OSError, ValueError, AttributeError):
        shipped = None
    out = {}
    for name, value in (shipped.items() if isinstance(shipped, dict) else ()):
        out[name] = value.get("sha256") if isinstance(value, dict) else value
    return out


def _live_files(root, n):
    """The files under refresh path `n` of `root`, as sorted relative paths; symlinks count as files. A
    folder the walk cannot read is listed itself (as a path that is no readable file), so it counts as
    a local edit instead of vanishing from the plan."""
    full = os.path.join(root, n)
    if os.path.isdir(full) and not os.path.islink(full):
        found = []

        def unreadable(error):
            if error.filename:
                found.append(os.path.relpath(error.filename, root).replace(os.sep, "/"))
        for dp, dn, fn in os.walk(full, onerror=unreadable):
            dn[:] = sorted(d for d in dn if d != "__pycache__")   # the interpreter's cache is no one's file
            for f in sorted(fn + [d for d in dn if os.path.islink(os.path.join(dp, d))]):
                if not f.endswith(".pyc"):
                    found.append(os.path.relpath(os.path.join(dp, f), root).replace(os.sep, "/"))
        return sorted(set(found))
    return [n] if os.path.lexists(full) else []


def _locally_edited(root, n, shipped, keep_out):
    """(edited, deleted): the live files under refresh path `n` that are not the bytes the installed
    release shipped (a changed file or link, a project-added file), and the shipped files under
    it that are gone. With no shipped list at all (no MANIFEST.json) every live file counts."""
    live = [f for f in _live_files(root, n) if not _under(f, keep_out)]
    edited = []
    for f in live:
        fp = os.path.join(root, f)
        if os.path.islink(fp):   # MANIFEST.json hashes a link by its target text, not by the bytes it reaches
            sha = hashlib.sha256(("symlink:" + os.readlink(fp)).encode("utf-8")).hexdigest()
        else:
            sha = _file_sha(fp)
        # A file with no readable bytes (unreadable, a named pipe, a folder) has no sha to compare: it
        # is no shipped file, so it counts as an edit and is saved (or refused) before the upgrade.
        if not shipped or sha is None or shipped.get(f) != sha:
            edited.append(f)
    deleted = []
    if shipped:
        have = set(live)
        deleted = sorted(f for f in shipped if (f == n or f.startswith(n + "/")) and f not in have
                         and not _under(f, keep_out))
    return edited, deleted


def _merge_replaces_edit(root, source, n, shipped):
    """True when merging the boot block of `source` into the live merge file `n` (CLAUDE.md,
    AGENTS.md) would replace text the installed release did not ship: the live file is not the shipped
    bytes, and its marked block differs from the incoming one. A file with no block, a link (kept as
    it is) or a source with no block loses nothing; a live file that cannot be read counts as an edit."""
    fp = os.path.join(root, n)
    if os.path.islink(fp) or not os.path.isfile(fp):
        return False
    if _regular_bytes(fp) is None:
        return True              # it exists and cannot be read: the merge would replace what it cannot see
    live, incoming = _read_or_empty(fp), _read_or_empty(os.path.join(source, n))
    block_in, block_live = manifest._boot_block(incoming), manifest._boot_block(live)
    if block_in is None or block_live is None or block_live == block_in:
        return False
    return not shipped or shipped.get(n) != _file_sha(fp)


def _plan_dropped(root, p):
    """Sort the files under each dropped path into p.remove (a regular file whose bytes are the ones
    the installed release shipped, per the live MANIFEST.json) and p.dropped_kept (everything else:
    a local edit, a project's own file, a symlink, or no MANIFEST.json to compare with). A file that
    also falls under a path the release still ships, or under memory, is left to those rules."""
    shipped = _shipped_shas(root)
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
    shipped = _shipped_shas(root)
    keep_out = [_rel_norm(r) for r in p.memory]
    for rel in p.mechanism:
        n = _rel_norm(rel)
        if n in PRESERVE:
            p.preserve.append(rel)
            continue
        if n in MERGE:
            p.merge.append(rel)
            if os.path.islink(os.path.join(root, n)):
                p.merge_links.append(rel)
                p.merge_link_to[rel] = os.readlink(os.path.join(root, n))
            elif _merge_replaces_edit(root, source, n, shipped):
                p.merge_edited.append(rel)
                p.local_files.append(n)
            continue
        p.refresh.append(rel)
        src = os.path.join(source, n)
        if not os.path.lexists(src):
            p.missing_in_source.append(rel)
            continue
        if _differs(os.path.join(root, n), src):
            edited, deleted = _locally_edited(root, n, shipped, keep_out)
            if edited or deleted:
                p.modified.append(rel)
                p.local_files.extend(edited)
                p.deleted_files.extend(deleted)
            else:
                p.changed.append(rel)
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
    # A link is copied as the link it is, at the top level and inside a folder: backup, stage,
    # promote and recovery must put back what was there, not the file the link points to.
    if os.path.isdir(src) and not os.path.islink(src):
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst, follow_symlinks=False)
    return True


def _read_or_empty(path):
    """The text of a regular file (a link is followed), or "" for a missing, unreadable or special file.
    A named pipe is never opened."""
    data = _regular_bytes(path, follow=True)
    return "" if data is None else data.decode("utf-8")


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
            if os.path.islink(os.path.join(root, n)):
                _copy_into(root, rel, staged)        # the link stays a link; the update never writes through it
                continue
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

    The whole run, the journal test and the plan included, happens under the project lock: a second
    apply, or a start-up recovery, never works on the scaffolding of a running one.
    """
    source = os.path.abspath(source)
    with project_lock(root):
        return _apply_locked(root, source, kill_at, clock)


def _apply_locked(root, source, kill_at, clock):
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
    no journal it is a fast no-op that writes nothing. Otherwise it takes the project lock and rolls
    back to the fully-old tree or forward to the fully-new tree by the recorded phase, then clears the
    scaffolding. A journal whose upgrade is still running (another process holds the lock) is left
    alone: that journal is not a crash."""
    if _read_journal(root) is None:
        return {"recovered": False, "reason": "no upgrade in progress"}
    try:
        with project_lock(root):
            return _recover_locked(root)
    except UpgradeLocked:
        return {"recovered": False, "busy": True, "reason": "an upgrade of this project is running now"}


def _recover_locked(root):
    journal = _read_journal(root)           # the holder may have finished while this run took the lock
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
def print_plan(p):
    """Print the plan lines (what changes, what a local edit loses, what the release drops) and return
    the one-line summary the plan verdict carries."""
    for rel in p.changed:
        print("  will be refreshed: %s" % rel)
    for rel in p.modified:
        print("  local edit will be replaced: %s" % rel)
    for rel in p.merge_edited:
        print("  local edit in the boot block will be replaced by the release's block: %s" % rel)
    for rel in p.merge_links:
        print("  kept as a link (its target is merged on its own when it is CLAUDE.md or AGENTS.md): %s -> %s"
              % (rel, p.merge_link_to.get(rel, "?")))
    for rel in p.remove:
        print("  dropped by the release, will be removed: %s" % rel)
    for rel in p.dropped_kept:
        print("  dropped by the release, kept (not the shipped bytes): %s" % rel)
    return "refresh=%d merge=%d preserve=%d modified=%d changed=%d remove=%d memory-untouched=%d" % (
        len(p.refresh), len(p.merge), len(p.preserve), len(p.modified), len(p.changed), len(p.remove),
        len(p.memory))


def _cmd_upgrade(args):
    from alpaca import cli
    from alpaca.gates import verdict as vc
    target = getattr(args, "target", None)
    if target:
        # Push mode: the running install upgrades another project. The source defaults to this
        # install, so `<new-install>/bin/alpaca upgrade --target <project>` rolls a release out.
        root = os.path.abspath(util.from_caller(target))
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
        if res.get("busy"):
            return vc.emit_verdict("alpaca-upgrade-recover", vc.BLOCKED, res["reason"] + "; nothing was changed")
        detail = res.get("reason") or ("rolled %s from phase %s" % (res.get("direction"),
                                                                    res.get("phase")))
        return vc.emit_verdict("alpaca-upgrade-recover", vc.PASS, detail)
    source = getattr(args, "source", None)
    if not source:
        return vc.emit_verdict("alpaca-upgrade", vc.BLOCKED, "a --source harness tree is required")
    try:
        p = plan(root, source)
    except Exception as e:
        return vc.emit_verdict("alpaca-upgrade", vc.FAIL, vc.error_reason(e))
    if getattr(args, "plan", False):
        return vc.emit_verdict("alpaca-upgrade-plan", vc.PASS, print_plan(p))
    try:
        apply(root, source)
    except UpgradeLocked as e:
        return vc.emit_verdict("alpaca-upgrade", vc.BLOCKED, vc.error_reason(e))
    except Exception as e:
        return vc.emit_verdict("alpaca-upgrade", vc.FAIL, vc.error_reason(e))
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
