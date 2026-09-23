"""Durable visible transcript snapshots with exact source generations.

Only registered sources and their contained child directories are copied.
Provider private reasoning is excluded from new snapshots. Historical generations
remain available until an explicit retention action; publication uses fsynced
staging files and atomic replacement under a per-session lock.
"""
from __future__ import annotations

import errno
import fcntl
import glob
import hashlib
import json
import re
import sqlite3
from pathlib import Path
import os
import tempfile
import time

from alpaca import db, paths
from alpaca.pool import slug

#: Compatibility constant; prefix validation now covers every retained byte.
TAIL = 4096

#: chunk size for the digest and the tail copy.
CHUNK = 1024 * 1024

#: Explicit prune_kept callers may retain this many generations. Snapshots never prune.
KEEP_COPIES = 3

#: how old a leaked `.tmp-*` file has to be before a snapshot sweeps it. Anything younger may
#: belong to a copy running right now.
TMP_MAX_AGE_S = 3600

#: the flock errors that mean another hook holds the lock, as opposed to a filesystem with no
#: lock support at all.
_BUSY_ERRNOS = {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES}


def local_path(root, sid) -> str:
    """Where this session's transcript copy lives inside the project."""
    return os.path.join(paths.transcript_dir(root), "%s.jsonl" % slug(sid))


def subagents_dir(root, sid) -> str:
    return os.path.join(paths.transcript_dir(root), slug(sid), "subagents")


def lock_path(dst) -> str:
    """The lock file that serialises snapshots of one destination."""
    return "%s.lock" % dst


def registered(conn, sid) -> str:
    """The transcript path the record carries for this session, or "" when none is registered."""
    rows = db.rows(conn, "sessions", "sid=?", (sid,))
    value = str(rows[0]["transcript"] or "") if rows else ""
    if value:
        return value
    try:
        sources = conn.execute("SELECT DISTINCT locator FROM obs_source WHERE session=? AND kind='transcript' AND locator IS NOT NULL", (sid,)).fetchall()
    except sqlite3.OperationalError as exc:
        if 'no such table' not in str(exc):
            raise
        return ""
    return str(sources[0][0]) if len(sources) == 1 else ""


def _unlink(path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _lock(dst):
    """Take the snapshot lock for one destination. Returns (fd, busy).

    `busy` is True when another hook already holds it: the caller skips its snapshot, because that
    other hook is doing the same work right now. Lock infrastructure errors fail
    closed so two writers cannot publish conflicting generations.
    """
    p = lock_path(dst)
    try:
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT, 0o666)
    except OSError:
        raise
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in _BUSY_ERRNOS:
            return None, True
        raise
    return fd, False


def _unlock(fd) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def _digest(path):
    """(bytes, sha256) of a local file, read in chunks. (0, None) when it is not there."""
    if not os.path.isfile(path):
        return 0, None
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
            n += len(chunk)
    return n, h.hexdigest()


def _is_prefix(src, dst, dst_size) -> bool:
    """Verify the entire retained prefix before accepting an append or current copy."""
    remaining = dst_size
    with open(src, "rb") as left, open(dst, "rb") as right:
        while remaining:
            amount = min(CHUNK, remaining)
            a, b = left.read(amount), right.read(amount)
            if len(a) != amount or a != b:
                return False
            remaining -= amount
    return True


def _numbered(dst):
    """(n, path) for every numbered old copy of `dst`, lowest number first."""
    d = os.path.dirname(os.path.abspath(dst))
    base = os.path.basename(dst)
    out = []
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for name in names:
        if not name.startswith(base + "."):
            continue
        tail = name[len(base) + 1:]
        if tail.isdigit():
            out.append((int(tail), os.path.join(d, name)))
    out.sort()
    return out


def _keep(dst) -> str:
    """Give the current local copy a second name, one past the highest numbered copy so far.

    A hard link, not a rename: `dst` holds its own name for the whole call, so the canonical copy
    is never absent, not even for the instant between two syscalls. A filesystem with no link
    support falls back to an atomic copied second name. Numbers only rise, so a higher number is always the newer copy.
    """
    nums = _numbered(dst)
    kept = "%s.%d" % (dst, (nums[-1][0] + 1) if nums else 1)
    try:
        os.link(dst, kept)
    except OSError:
        _full_copy(dst, kept)
    return kept


def prune_kept(dst) -> int:
    """Remove numbered old copies past KEEP_COPIES, newest first kept. Returns how many went.

    The canonical copy carries no number and is never a candidate. Housekeeping never raises: a
    copy that cannot be removed is left where it is.
    """
    nums = _numbered(dst)
    doomed = nums[:-KEEP_COPIES] if KEEP_COPIES > 0 else nums
    n = 0
    for _num, p in doomed:
        try:
            os.unlink(p)
            n += 1
        except OSError:
            continue
    return n


def sweep_tmp(root, sid, now=None) -> int:
    """Remove `.tmp-*` files older than TMP_MAX_AGE_S under this session's copies.

    A copy stopped at the hook deadline leaves its temporary file behind, and nothing else ever
    comes back for it. Anything younger than the cutoff may belong to a copy running right now, so
    only the old ones go. Returns how many were removed and never raises.
    """
    cutoff = (time.time() if now is None else now) - TMP_MAX_AGE_S
    dirs = [paths.transcript_dir(root)]
    for d, _subdirs, _files in os.walk(subagents_dir(root, sid)):
        dirs.append(d)
    n = 0
    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if not name.startswith(".tmp-"):
                continue
            p = os.path.join(d, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.unlink(p)
                    n += 1
            except OSError:
                continue
    return n


def _copy_to_temp(src, dst) -> str:
    """Copy `src` whole into a temporary file beside `dst`, flushed to the device, and return it.

    The caller renames it into place. Nothing touches the canonical name until the bytes are all
    there, so a copy cut off half way through leaves only the temporary file to sweep.
    """
    d = os.path.dirname(os.path.abspath(dst))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with open(src, "rb") as fin:
            while True:
                chunk = fin.read(CHUNK)
                if not chunk:
                    break
                off = 0
                while off < len(chunk):
                    written = os.write(fd, chunk[off:])
                    if written <= 0:
                        raise OSError("transcript write made no progress")
                    off += written
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        _unlink(tmp)
        raise
    os.close(fd)
    u = os.umask(0)
    os.umask(u)
    try:
        os.chmod(tmp, 0o666 & ~u)
    except OSError:
        pass
    return tmp


def _full_copy(src, dst) -> None:
    """Copy src over dst atomically: a temporary file beside it, then one os.replace."""
    tmp = _copy_to_temp(src, dst)
    try:
        os.replace(tmp, dst)
        _sync_directory(dst)
    except BaseException:
        _unlink(tmp)
        raise


def _sync_directory(path):
    fd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def copy_forward(src, dst) -> str:
    """Publish a fully verified source atomically; preserve every old generation.

    Even appends use an fsynced staging file so a short write or failed replace
    cannot expose a partial canonical copy. Retirement is a separate owner action.
    """
    if not os.path.isfile(dst):
        _full_copy(src, dst)
        return "copy"
    # Freeze source bytes first, then compare exactly the bytes to be published.
    tmp = _copy_to_temp(src, dst)
    try:
        src_size, dst_size = os.path.getsize(tmp), os.path.getsize(dst)
        prefix = dst_size <= src_size and _is_prefix(tmp, dst, dst_size)
        if prefix and dst_size == src_size:
            return "current"
        mode = "append" if prefix else "rewrite"
        if not prefix:
            _keep(dst)
        os.replace(tmp, dst)
        _sync_directory(dst)
        return mode
    finally:
        _unlink(tmp)


def visible_record(record):
    """Remove private provider reasoning structurally, without interpreting it.

    Unknown metadata and visible tool payloads remain intact. Compaction history
    is omitted because it can embed private response items and old conversations.
    """
    if not isinstance(record, dict):
        return record
    if record.get("type") in ("reasoning", "thinking", "redacted_thinking", "analysis", "compacted",
                               "agent_reasoning", "agent_reasoning_raw_content"):
        return None
    if record.get("channel") == "analysis":
        return None
    out = {}
    for key, value in record.items():
        if key in ("thinking", "reasoning", "encrypted_content", "replacement_history"):
            continue
        if key in ("payload", "message") and isinstance(value, dict):
            value = visible_record(value)
            if value is None:
                return None
        elif key in ("content", "summary") and isinstance(value, list):
            value = [filtered for item in value if (filtered := visible_record(item)) is not None]
        out[key] = value
    return out


def _filtered_source(src, dst):
    """Stage complete JSONL records, excluding private reasoning and partial tails."""
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(dst)), prefix=".tmp-")
    partial = False
    excluded = malformed = 0
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as source:
            initial = os.fstat(source.fileno())
            remaining = initial.st_size
            while remaining:
                line = source.readline(remaining)
                if not line:
                    raise OSError("transcript truncated while snapshotting; retry")
                remaining -= len(line)
                if not line.endswith(b"\n"):
                    partial = True
                    break
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError, RecursionError):
                    malformed += 1
                    # Opaque malformed payloads may contain private text; retain
                    # the fault location rather than copying unclassified bytes.
                    out.write(b'{"type":"capture-malformed-record"}\n')
                    continue
                filtered = visible_record(record)
                if filtered is None:
                    excluded += 1
                    continue
                if filtered == record:
                    out.write(line)
                else:
                    excluded += 1
                    out.write(json.dumps(filtered, ensure_ascii=True).encode() + b"\n")
            out.flush()
            os.fsync(out.fileno())
            final = os.fstat(source.fileno())
            if (initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns) != (final.st_size, final.st_mtime_ns, final.st_ctime_ns):
                raise OSError("transcript changed while snapshotting; retry")
        return tmp, {"partial_line": partial, "private_records_filtered": excluded,
                     "malformed_records": malformed}
    except BaseException:
        _unlink(tmp)
        raise


def _source_state(src, dst):
    state_path = str(dst) + ".source.json"
    try:
        previous = json.loads(Path(state_path).read_text())
    except (OSError, ValueError):
        previous = {}
    size, digest = _digest(src)
    generation = previous.get("generation", 1)
    prior_size = previous.get("source_bytes")
    if isinstance(prior_size, int):
        with open(src, "rb") as source:
            remaining, hashed = prior_size, hashlib.sha256()
            while remaining:
                block = source.read(min(CHUNK, remaining))
                if not block:
                    break
                hashed.update(block)
                remaining -= len(block)
        if remaining or hashed.hexdigest() != previous.get("source_sha256"):
            generation += 1
    return state_path, {"schema": 1, "generation": generation, "source_bytes": size,
                        "source_sha256": digest, "privacy": "visible-records-only"}


def _snapshot_file(src, dst):
    state_path, state = _source_state(src, dst)
    filtered, facts = _filtered_source(src, dst)
    try:
        # Ensure the state and filtered publication describe the same bytes.
        if _digest(src) != (state["source_bytes"], state["source_sha256"]):
            raise OSError("transcript changed while snapshotting; retry")
        mode = copy_forward(filtered, dst)
    finally:
        _unlink(filtered)
    state.update(facts)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(dst)), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(state, out, ensure_ascii=True)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, state_path)
        _sync_directory(state_path)
    finally:
        _unlink(tmp)
    return mode, state


def discover_codex(root, native_session_id, *, storage_root=None):
    """Locate only a supplied native ID and verify its session header project.

    Names are filtered before any contents are read. Symlinks and ambiguous
    matching files are rejected; no account-wide transcript import is performed.
    """
    if not isinstance(native_session_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", native_session_id):
        raise ValueError("invalid native session identifier")
    base = Path(storage_root) if storage_root is not None else Path.home() / ".codex" / "sessions"
    project_root = os.path.realpath(root)
    found = []
    scanned = 0
    for directory, dirs, names in os.walk(base, followlinks=False):
        dirs[:] = [name for name in sorted(dirs) if not Path(directory, name).is_symlink()]
        scanned += len(dirs) + len(names)
        if scanned > 100000:
            raise ValueError("native session discovery limit reached")
        for name in sorted(names):
            if not name.endswith(native_session_id + ".jsonl"):
                continue
            candidate = Path(directory, name)
            if candidate.is_symlink():
                continue
            try:
                with candidate.open("rb") as stream:
                    line = stream.readline(1024 * 1024 + 1)
                if not line.endswith(b"\n") or len(line) > 1024 * 1024:
                    continue
                header = json.loads(line)
                payload = header.get("payload", {})
                if (header.get("type") == "session_meta" and payload.get("id") == native_session_id
                        and isinstance(payload.get("cwd"), str)
                        and os.path.realpath(payload["cwd"]) == project_root):
                    found.append(str(candidate.absolute()))
            except (OSError, ValueError, TypeError, AttributeError):
                continue
    if len(found) > 1:
        raise ValueError("ambiguous native session source")
    return found[0] if found else None


def _subagent_pairs(src, root, sid):
    """(source, local) for every subagent transcript that sits beside the session transcript.

    The local name is built from the source name, so it is checked: a name that would climb out of
    the local subagents directory is dropped rather than written.
    """
    base = os.path.join(os.path.dirname(os.path.abspath(src)), Path(src).stem, "subagents")
    if not os.path.isdir(base):
        return []
    local_base = os.path.abspath(subagents_dir(root, sid))
    out = []
    for p in sorted(glob.glob(os.path.join(base, "**", "agent-*.jsonl"), recursive=True)):
        if os.path.islink(p) or not os.path.realpath(p).startswith(os.path.realpath(base) + os.sep):
            continue
        d = os.path.abspath(os.path.join(local_base, os.path.relpath(p, base)))
        if not d.startswith(local_base + os.sep):
            continue
        out.append((p, d))
    return out


def local_subagents(root, sid):
    """Every subagent transcript already copied into the project, sorted."""
    pattern = os.path.join(subagents_dir(root, sid), "**", "agent-*.jsonl")
    return sorted(glob.glob(pattern, recursive=True))


def _snapshot_registry_children(root, sid, conn):
    """Retain only explicitly registered child sources, under their own lock."""
    try:
        rows = conn.execute("SELECT source_id,session,locator FROM obs_source WHERE parent_session=? AND kind='transcript' ORDER BY source_id LIMIT 41", (sid,)).fetchall()
    except sqlite3.OperationalError as exc:
        if 'no such table' not in str(exc):
            raise
        return []
    results = []
    for row in rows[:40]:
        source, target = registered(conn, row['session']), local_path(root, row['session'])
        if source and not os.path.isabs(source):
            source = os.path.join(root, source)
        mode = 'missing'
        if source and os.path.isfile(source):
            fd, busy = _lock(target)
            try:
                if busy:
                    mode = 'busy'
                elif os.path.realpath(source) == os.path.realpath(target):
                    mode = 'local'
                else:
                    mode, _state = _snapshot_file(source, target)
            finally:
                _unlock(fd)
        results.append({'source_id': row['source_id'], 'session': row['session'],
                        'path': os.path.relpath(target, root), 'retained': os.path.isfile(target), 'mode': mode})
    if len(rows) > 40:
        results.append({'mode': 'truncated', 'retained': False})
    return results


def snapshot(root, sid, conn=None, event=False) -> dict:
    """Copy this session's registered transcript and its subagent files into the project.

    Returns the snapshot fact: the local path relative to the root, its size and sha256, how many
    subagent transcripts are held locally, whether the registered source is gone, and which of
    copy / append / current / rewrite / local / none / busy happened to the main file.

    `busy` means another hook held this session's snapshot lock, so this call copied nothing,
    recorded nothing and reports no size or digest: the hook that holds the lock is doing the work.

    With `event=True` one `transcript-snapshot` event is appended carrying the first five of those
    fields. Pass `conn` to join an open connection; without one a connection is opened and closed.
    """
    own = conn is None
    if own:
        conn = db.connect(root)
    try:
        src = registered(conn, sid)
        if src and not os.path.isabs(src):
            src = os.path.join(root, src)
        dst = local_path(root, sid)
        rel = os.path.relpath(dst, root)
        source_missing = not (src and os.path.isfile(src))
        fd, busy = _lock(dst)
        if busy:
            return {"path": rel, "bytes": None, "sha256": None,
                    "subagents": len(local_subagents(root, sid)),
                    "source_missing": source_missing, "mode": "busy", "source": src or None}
        try:
            sweep_tmp(root, sid)
            mode = "none"
            facts = {}
            if not source_missing:
                if os.path.abspath(src) == os.path.abspath(dst):
                    mode = "local"           # the record already points at the project-local copy
                else:
                    mode, facts = _snapshot_file(src, dst)
                for s, d in _subagent_pairs(src, root, sid):
                    if os.path.abspath(s) != os.path.abspath(d):
                        _snapshot_file(s, d)
            registry_children = _snapshot_registry_children(root, sid, conn)
            size, sha = _digest(dst)
            data = {"path": rel, "bytes": size, "sha256": sha,
                    "subagents": len(local_subagents(root, sid)),
                    "source_missing": source_missing, "registry_children": registry_children, **facts}
            if event:
                db.append_event(conn, session=sid, actor="alpaca", kind="transcript-snapshot",
                                data=data)
            return dict(data, mode=mode, source=src or None)
        finally:
            _unlock(fd)
    finally:
        if own:
            conn.close()


def checks(root, conn):
    """Findings as (name, level, detail) tuples, the shape alpaca.doctor folds in.

    A session with a registered transcript and no local copy, or whose source has grown past the
    local copy, is a WARN: that session's tail is only in storage the project does not own. A
    work session without a registered source produces a capture coverage warning.
    """
    from alpaca import sessions_view
    view = sessions_view.classify(conn)
    unregistered = [sid for sid, info in view.items()
                    if info['class'] == sessions_view.WORK and not registered(conn, sid)
                    and not os.path.isfile(local_path(root, sid))]
    findings = [("transcript-registration", "warn", "unregistered work session(s): " + ", ".join(sorted(unregistered)))] if unregistered else []
    total = 0
    missing = []
    behind = []
    lost = []
    live = 0
    for r in db.rows(conn, "sessions"):
        sid = r["sid"]
        src = registered(conn, sid)
        if not src:
            continue
        if not os.path.isabs(src):
            src = os.path.join(root, src)
        dst = local_path(root, sid)
        total += 1
        if os.path.abspath(src) == os.path.abspath(dst):
            continue
        if not os.path.isfile(dst):
            # A source that is already gone cannot be recovered by any later snapshot: it is a
            # stated loss, counted, and not a standing warning. One that still exists can be.
            (missing if os.path.isfile(src) else lost).append(sid)
            continue
        try:
            changed = False
            if os.path.isfile(src):
                try:
                    state = json.loads(Path(str(dst) + '.source.json').read_text())
                    changed = _digest(src) != (state.get('source_bytes'), state.get('source_sha256'))
                except (OSError, ValueError, AttributeError):
                    changed = _digest(src) != _digest(dst)
            if changed:
                if r["ended"]:
                    behind.append(sid)
                else:
                    live += 1             # an open session is behind between two boundaries
        except OSError:
            continue
    if not total:
        return findings
    prefix = "%d registered transcript(s)" % total
    notes = []
    if live:
        notes.append("%d open session(s) between snapshots" % live)
    if lost:
        notes.append("%d lost before the first snapshot (source gone)" % len(lost))
    parts = []
    if missing:
        parts.append("no local copy, run alpaca analytics snapshot: %s" % ", ".join(sorted(missing)))
    if behind:
        parts.append("local copy behind the source, run alpaca analytics snapshot: %s"
                     % ", ".join(sorted(behind)))
    if parts:
        return findings + [("transcript-snapshots", "warn", "; ".join([prefix] + parts + notes))]
    return findings + [("transcript-snapshots", "ok",
             "; ".join([prefix + ", each recoverable one with a current local copy"] + notes))]
