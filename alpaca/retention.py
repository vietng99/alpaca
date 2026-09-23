"""Store retention, compaction, and the mandatory snapshot note (M4.7).

Sources: P10:109 and spec 5.1:218 (snapshot-out before any non-idempotent act); spec section 11
risks (spec:862-871); absorb-gap AG-M18 (retention and compaction policy), AG-A8 (naming
convention, mandatory note, not a promote source). The doctrine leaf is
`doctrine/leaves/operate-in-place.md`: the code is the mechanism, the leaf is the reason, so the
two never drift.

The STORE is the history a non-idempotent act leaves behind: before a verb mutates a file in place
it snapshots the file's pre-image so the change is reconstructable. It lives under the runtime
directory (`.alpaca/store/`, gitignored) because it is state, not mechanism. The store is history the
operator is mutating past, NOT a clone to promote from: nothing is ever restored FROM the store as
a new tree, it only serves as the diff reference for the resolve pass and as an audit trail.

A snapshot carries a MANDATORY NOTE. `snapshot(root, path, change_tag, note)` refuses without one,
because a pre-image with no stated reason is an unexplained mutation of the past. The stored file is
named `<file>.pre-<change-tag>.<date>` so the resolve pass can find the diff reference by the same
change tag and reference it drove the mutation under.

`compact(root, policy)` obeys a DECLARED policy (a size ceiling and an age ceiling, read from
project.yaml, never from code) and:

  * never removes a snapshot a live token or an unresolved collision still references. Those
    snapshots are the diff reference something still needs; dropping one would strand the resolve
    pass or a mid-verb resume with no base to reconstruct against.
  * refuses a zero-retention policy (a zero ceiling) while unresolved collisions exist, for the
    same reason: a policy that keeps nothing would delete the very diff reference an unresolved
    collision is waiting on.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date

from alpaca import paths, project, util

#: the store directory name under the runtime directory (`.alpaca/store`).
STORE = "store"

#: the sidecar that carries a snapshot's mandatory note and metadata beside its pre-image bytes.
NOTE_SUFFIX = ".note.json"

_TMP_PREFIX = ".tmp-"


class RetentionError(Exception):
    """A retention operation that cannot proceed: a missing note, a missing change tag, a target
    that is not a file, or a zero-retention policy asked for while a collision is unresolved."""


def store_dir(root) -> str:
    """The store directory for a root: `<root>/.alpaca/store`. The one place the name is joined."""
    return os.path.join(paths.runtime_dir(root), STORE)


def _date_of(now) -> str:
    """The `<date>` component of a store name: the ISO date (YYYY-MM-DD) of the snapshot. `now` is
    an injected offset-bearing ISO string (a FixedClock under test); None uses the process clock."""
    return (now or util.now_iso())[:10]


def store_name(path, change_tag, now=None) -> str:
    """The store file name for a snapshot: `<file>.pre-<change-tag>.<date>`, so the resolve pass can
    find the diff reference by the change tag it drove the mutation under."""
    return "%s.pre-%s.%s" % (os.path.basename(path), change_tag, _date_of(now))


def _atomic_write_bytes(dest, data: bytes) -> None:
    d = os.path.dirname(os.path.abspath(dest))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=_TMP_PREFIX)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, dest)


def snapshot(root, path, change_tag, note, *, now=None) -> dict:
    """Snapshot a file's pre-image into the store before a non-idempotent act mutates it in place.

    The note is mandatory: a snapshot without a stated reason is refused, because the store is a
    record of the past being mutated, not an anonymous backup. The change tag is mandatory too, so
    the resolve pass can find this diff reference by the same tag. The stored file is named
    `<file>.pre-<change-tag>.<date>`; a second snapshot for the same file, tag and date keeps the
    earliest pre-image (the true "before"), so re-running the guarded verb is idempotent here.

    Returns the snapshot metadata, including `store` (the absolute path to the pre-image) and
    `name` (the store file name)."""
    if not note or not str(note).strip():
        raise RetentionError(
            "a snapshot without a note is refused: state why the past is being mutated")
    if not change_tag or not str(change_tag).strip():
        raise RetentionError(
            "a snapshot needs a change tag so the resolve pass can find the diff reference")
    src = path if os.path.isabs(path) else os.path.join(root, path)
    if not os.path.isfile(src):
        raise RetentionError("nothing to snapshot: %s is not a file" % path)
    name = store_name(path, change_tag, now)
    sd = store_dir(root)
    os.makedirs(sd, exist_ok=True)
    dest = os.path.join(sd, name)
    ref = os.path.relpath(src, root)
    meta = {"ref": ref, "path": ref, "change_tag": str(change_tag), "date": _date_of(now),
            "note": str(note), "ts": now or util.now_iso(), "name": name, "store": dest}
    if not os.path.exists(dest):
        with open(src, "rb") as fh:
            _atomic_write_bytes(dest, fh.read())
        # the note sidecar rides beside the pre-image; it is what makes the note recoverable and
        # a note-less snapshot impossible to leave on disk.
        util.write_text(dest + NOTE_SUFFIX, json.dumps(meta, sort_keys=True) + "\n")
    return meta


def _load_meta(dest) -> dict:
    notep = dest + NOTE_SUFFIX
    if os.path.isfile(notep):
        try:
            return json.loads(util.read_text(notep))
        except ValueError:
            return {}
    return {}


def entries(root) -> list:
    """Every snapshot in the store, oldest date first, each with its size, ref, change tag, date and
    note. The note sidecars and any interrupted temp file are skipped."""
    sd = store_dir(root)
    if not os.path.isdir(sd):
        return []
    out = []
    for name in os.listdir(sd):
        if name.endswith(NOTE_SUFFIX) or name.startswith(_TMP_PREFIX):
            continue
        full = os.path.join(sd, name)
        if not os.path.isfile(full):
            continue
        meta = _load_meta(full)
        out.append({"name": name, "path": full, "size": os.path.getsize(full),
                    "ref": meta.get("ref"), "change_tag": meta.get("change_tag"),
                    "date": meta.get("date"), "note": meta.get("note"), "ts": meta.get("ts")})
    out.sort(key=lambda e: (e.get("date") or "", e["name"]))
    return out


def store_size(root) -> int:
    """Total bytes the store holds (pre-images plus their note sidecars). This is the size the
    doctor reports and the size ceiling in `compact` measures against."""
    sd = store_dir(root)
    if not os.path.isdir(sd):
        return 0
    total = 0
    for name in os.listdir(sd):
        full = os.path.join(sd, name)
        if os.path.isfile(full):
            total += os.path.getsize(full)
    return total


def reference(root, ref, change_tag=None):
    """The most recent snapshot for a reference, or None. This is how the resolve pass finds the
    diff reference: by the ref (and optionally the change tag) the mutation was taken under."""
    cands = [e for e in entries(root)
             if e.get("ref") == ref and (change_tag is None or e.get("change_tag") == change_tag)]
    return cands[-1] if cands else None


def load_policy(root) -> dict:
    """The retention policy, read from project.yaml (DATA, never from code). A size ceiling
    (`max_bytes`) and an age ceiling (`max_age_days`); either may be absent (no ceiling)."""
    r = (project.load(root) or {}).get("retention") or {}
    return {"store": r.get("store", ".alpaca/" + STORE),
            "max_bytes": r.get("max_bytes"), "max_age_days": r.get("max_age_days")}


def is_zero_retention(policy) -> bool:
    """A policy that keeps nothing: a zero size ceiling or a zero age ceiling."""
    return policy.get("max_bytes") == 0 or policy.get("max_age_days") == 0


def referenced_refs(conn) -> set:
    """Every reference a live token or an unresolved collision still points at. A snapshot naming
    one of these is a diff reference something still needs, so compaction must never remove it."""
    from alpaca import resolve, token
    refs = set()
    for u in token.unmatched(conn):
        t = u.get("target")
        if t:
            refs.add(t)
    for e in resolve.unresolved(conn):
        if e.get("ref"):
            refs.add(e["ref"])
    return refs


def _is_referenced(entry, refs) -> bool:
    for key in (entry.get("ref"), entry.get("change_tag"), entry.get("name")):
        if key is not None and key in refs:
            return True
    return False


def _age_days(entry, now_s):
    d = entry.get("date")
    if not d:
        return None
    try:
        return (date.fromisoformat(now_s[:10]) - date.fromisoformat(str(d)[:10])).days
    except ValueError:
        return None


def _remove(entry) -> None:
    for p in (entry["path"], entry["path"] + NOTE_SUFFIX):
        try:
            os.remove(p)
        except OSError:
            pass


def compact(root, policy=None, *, conn=None, now=None) -> dict:
    """Compact the store under a declared policy. Snapshots a live token or an unresolved collision
    still reference are kept regardless of the ceilings; only unreferenced snapshots are candidates
    for removal. A zero-retention policy is refused while unresolved collisions exist, because it
    would delete the diff reference those collisions are waiting on.

    Removal is by the two ceilings: an unreferenced snapshot older than `max_age_days` goes, and
    while the store is over `max_bytes` the oldest remaining unreferenced snapshots go until it is
    under the ceiling or none is left. Returns {removed, kept, referenced, store_bytes}."""
    from alpaca import db, resolve
    if policy is None:
        policy = load_policy(root)
    conn = conn or db.connect(root)
    now_s = now or util.now_iso()

    unresolved_n = resolve.unresolved_count(conn)
    if is_zero_retention(policy) and unresolved_n:
        raise RetentionError(
            "a zero-retention policy is refused while %d unresolved collision(s) still need their "
            "diff reference; run the resolve pass first" % unresolved_n)

    refs = referenced_refs(conn)
    ents = entries(root)
    referenced = [e for e in ents if _is_referenced(e, refs)]
    ref_names = {e["name"] for e in referenced}
    removable = [e for e in ents if e["name"] not in ref_names]

    removed = []
    max_age = policy.get("max_age_days")
    survivors = []
    for e in removable:
        age = _age_days(e, now_s)
        if max_age is not None and age is not None and age > max_age:
            _remove(e)
            removed.append(e)
        else:
            survivors.append(e)

    max_bytes = policy.get("max_bytes")
    if max_bytes is not None:
        survivors.sort(key=lambda e: (e.get("date") or "", e["name"]))
        while store_size(root) > max_bytes and survivors:
            victim = survivors.pop(0)
            _remove(victim)
            removed.append(victim)
    return _summary(root, referenced, removed)


def _summary(root, referenced, removed) -> dict:
    kept = [e["name"] for e in entries(root)]
    return {"removed": [e["name"] for e in removed], "kept": kept,
            "referenced": [e["name"] for e in referenced], "store_bytes": store_size(root)}
