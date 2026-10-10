"""Which conversation is a subagent of which session (t-143).

A subagent (a Claude Code child agent, or a Codex child thread) writes its own transcript.
`transcripts._snapshot_registry_children` copies each registered child to
`.alpaca/transcripts/<agentId>.jsonl`, right beside the session copies, so a reader that treats
every top-level copy as a session lists each child as a session of its own. This module names the
parent of every child the project can see, from four sources, strongest first:

1. the record: `obs_source` rows of kind 'transcript' whose `parent_session` is set;
2. a project copy whose first records say `isSidechain: true` with the parent's `sessionId`;
3. the project's own `<sid>/subagents/**/agent-<id>.jsonl` copies;
4. the registered parent transcript's native `<stem>/subagents/**/agent-<id>.jsonl` directory.

A child under a `workflows/wf_<run>/` directory was launched by a workflow run; any other child is
an ordinary subagent. Nothing here writes.
"""
import glob
import json
import os

from alpaca import paths, transcripts

MAX_HEAD_BYTES = 1024 * 1024
MAX_HEAD_LINES = 4


def _kind(path):
    """('workflow', run id) for a child under workflows/wf_<run>/, else ('subagent', None)."""
    parts = str(path or "").replace("\\", "/").split("/")
    if "workflows" in parts:
        at = parts.index("workflows")
        run = parts[at + 1] if at + 1 < len(parts) - 1 else None
        return "workflow", run
    return "subagent", None


def _readable(path):
    return bool(path) and os.path.isfile(path) and not os.path.islink(path)


def _sidechain_parent(path, stem):
    """The parent session named by a copy's first records, or None when it is not a child."""
    try:
        with open(path, "rb") as fh:
            for _ in range(MAX_HEAD_LINES):
                line = fh.readline(MAX_HEAD_BYTES)
                if not line:
                    return None
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                parent = rec.get("sessionId")
                if rec.get("isSidechain") is True and isinstance(parent, str) and parent != stem \
                        and str(rec.get("agentId") or stem) == stem:
                    return parent
                if rec.get("isSidechain") is False:
                    return None
    except OSError:
        return None
    return None


def _add(found, parent, child, path, origin):
    entry = found.setdefault(parent, {}).get(child)
    if entry is None:
        kind, run = _kind(path)
        found[parent][child] = {"path": path if _readable(path) else None, "kind": kind,
                                "workflow": run, "origin": origin}
        return
    if entry["path"] is None and _readable(path):
        entry["path"] = path
    if entry["kind"] != "workflow":
        kind, run = _kind(path)
        if kind == "workflow":
            entry.update(kind=kind, workflow=run)


def _scan(found, parent, base, origin):
    if not base or not os.path.isdir(base) or os.path.islink(base):
        return
    real = os.path.realpath(base)
    for path in sorted(glob.glob(os.path.join(base, "**", "agent-*.jsonl"), recursive=True)):
        if os.path.islink(path) or not os.path.realpath(path).startswith(real + os.sep):
            continue
        child = os.path.basename(path)[len("agent-"):-len(".jsonl")]
        if child:
            _add(found, parent, child, path, origin)


def discover(root, conn):
    """{parent sid: {child id: {path, kind, workflow, origin}}} for every child in view.

    `path` is a readable transcript for the child (its registered source first, then the project
    copy) or None when the record names a child whose transcript is gone.
    """
    found = {}
    tdir = paths.transcript_dir(root)
    try:
        rows = conn.execute("SELECT session, parent_session, locator FROM obs_source "
                            "WHERE kind='transcript' AND parent_session IS NOT NULL "
                            "AND parent_session != '' ORDER BY source_id").fetchall()
    except Exception as exc:  # an older record without the observability tables
        if "no such table" not in str(exc):
            raise
        rows = []
    for child, parent, locator in rows:
        if not child or child == parent:
            continue
        path = str(locator or "")
        if path and not os.path.isabs(path):
            path = os.path.join(root, path)
        local = transcripts.local_path(root, child)
        chosen = path if _readable(path) else local if _readable(local) else None
        entry = found.setdefault(parent, {}).get(child)
        if entry is None:
            kind, run = _kind(path or local)
            found[parent][child] = {"path": chosen, "kind": kind, "workflow": run, "origin": "record"}
        elif entry["path"] is None:
            entry["path"] = chosen
    known = {child for kids in found.values() for child in kids}
    try:
        sessions = {row[0] for row in conn.execute("SELECT sid FROM sessions")}
    except Exception:
        sessions = set()
    for path in sorted(glob.glob(os.path.join(tdir, "*.jsonl"))):
        stem = os.path.basename(path)[:-len(".jsonl")]
        if stem in known or stem in sessions or os.path.islink(path):
            continue
        parent = _sidechain_parent(path, stem)
        if parent:
            _add(found, parent, stem, path, "sidechain-header")
    parents = set(found) | sessions
    for parent in sorted(parents):
        _scan(found, parent, transcripts.subagents_dir(root, parent), "project-copy")
        try:
            registered = transcripts.registered(conn, parent)
        except Exception:
            registered = ""
        if registered:
            if not os.path.isabs(registered):
                registered = os.path.join(root, registered)
            stem = os.path.splitext(os.path.basename(registered))[0]
            _scan(found, parent, os.path.join(os.path.dirname(registered), stem, "subagents"), "native-directory")
    # A child that is itself the parent of others stays a child; a session never becomes its own child.
    for parent in list(found):
        found[parent].pop(parent, None)
        if not found[parent]:
            del found[parent]
    return found


def parents(families):
    """{child id: parent sid} from discover()."""
    return {child: parent for parent, kids in families.items() for child in kids}


def revision(kids):
    """A cheap stamp of one family's child files, so a fold moves when a child transcript grows."""
    stamp = []
    for child, entry in sorted(kids.items()):
        try:
            st = os.stat(entry["path"]) if entry.get("path") else None
        except OSError:
            st = None
        stamp.append([child, st.st_size if st else None, st.st_mtime_ns if st else None])
    return stamp
