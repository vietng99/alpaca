"""ingest.drain (Alpaca-native, no upstream): the session drain that feeds the vendored write path.

The drain reads one session's record events and its transcript out of the Alpaca store and lands them
as raw markdown notes in the wiki vault, then hands each note to the vendored write path
(ingest.absorb). It is the SOURCE in front of absorb, never a second write door.

Capture is DUMB (M2.14 Step 3): the drain assigns a note's role by a fixed mechanical lookup over
the event kind and NEVER judges the pairing of an intent with its result - that is `alpaca sort`'s job.
Re-running the drain over the same session is a no-op: the note bytes are identical, so absorb's
hashgate skips every unchanged block (idempotent), and the disk write is skipped when unchanged.

The note carries its op, role and pairing key in a plain first paragraph (`key: value` lines) so
`alpaca sort` can read them back off the ingested block without a second schema. No wikilinks are
emitted, so a captured note mints no graph edges of its own.
"""
from __future__ import annotations

import os
import json
import re
import stat
from contextlib import contextmanager
from pathlib import Path

from alpaca import db as tedb
from alpaca import paths

# A note's role is a MECHANICAL lookup on the event kind, not a judgment (Step 3: capture is dumb).
# Everything the session emitted is landed; only the intent/result split is pre-labelled here, and
# even that is a fixed table, never a decision about a specific event.
INTENT_KINDS = frozenset({
    "intent", "task-add", "task-claim", "claim", "handoff", "question",
    "phase-enter", "row-open", "op-open", "op-new",
})
RESULT_KINDS = frozenset({
    "result", "task-done", "row-done", "run", "verdict", "phase-gate", "op-close",
})


def wiki_vault_dir(root: str) -> str:
    """The wiki vault for an Alpaca project lives under the runtime dir, beside alpaca.db."""
    return os.path.join(paths.runtime_dir(root), "wiki")


def _slug(value, fallback: str = "none") -> str:
    out = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "")).strip("-")
    return out or fallback


def _role_of(kind: str, data=None) -> str:
    data = data or {}
    if kind == "msg":
        kind = data.get("kind")
    elif kind == "task-move" and data.get("to") == "done":
        kind = "task-done"
    if kind in INTENT_KINDS:
        return "intent"
    if kind in RESULT_KINDS:
        return "result"
    return "note"


def _pair_key(ev: dict) -> str:
    if ev.get("_pair_key"):
        return ev["_pair_key"]
    data = ev.get("data") or {}
    for candidate in (ev.get("ref"), data.get("row"), data.get("task"),
                      data.get("id"), data.get("op"), ev.get("op")):
        if candidate:
            return str(candidate)
    return ""


def _summary(ev: dict) -> str:
    """A plain one-line rendering of the event. Deliberately free of wikilinks so a note mints no
    edges; sort, not capture, writes the assertions."""
    data = ev.get("data") or {}
    bits = ["%s during session %s" % (ev.get("kind", "event"), ev.get("session", ""))]
    for k in ("reason", "statement", "title", "body", "verdict", "note", "detail", "error",
              "context", "options", "choice", "consequence", "pointer", "proof", "from", "to"):
        v = data.get(k)
        if v:
            value = json.dumps(v, ensure_ascii=True) if isinstance(v, (dict, list)) else str(v)
            bits.append(k + ": " + value.replace("\n", " ").strip())
    return ". ".join(b for b in bits if b).replace("[[", "[ [").replace("]]", "] ]")


def observed_prefixes(root) -> tuple:
    """Kind prefixes whose event data the wiki keeps verbatim as untrusted JSON: the domain
    profile's observation kinds (alpaca/profile.py `events`). Empty without a profile."""
    from alpaca import profile
    try:
        return tuple(profile.listed(profile.load(root).events(), "observation_prefixes"))
    except Exception:
        return ()


def _note_doc(ev: dict, prefixes=()):
    op = ev.get("op") or (ev.get("data") or {}).get("op") or "unassigned"
    role = _role_of(ev.get("kind"), ev.get("data"))
    seq = int(ev.get("id") or 0)
    header = [
        "op: %s" % op,
        "role: %s" % role,
        "key: %s" % _pair_key(ev),
        "kind: %s" % (ev.get("kind") or ""),
        "event: %s" % seq,
        "ts: %s" % (ev.get("ts") or ""),
    ]
    text = "\n".join(header) + "\n\n" + _summary(ev) + "\n"
    if prefixes and (ev.get('kind') or '').startswith(tuple(prefixes)):
        # A profile observation: preserve its receipt/job/source pointers. JSON is untrusted
        # observation data, never instructions and never an automatically admitted lesson.
        text += '\nUntrusted observation data:\n\n```json\n' + json.dumps(
            ev.get('data') or {}, sort_keys=True, ensure_ascii=True) + '\n```\n'
    doc_id = "raw/%s/%06d-%s.md" % (_slug(op), seq, role)
    return doc_id, text


def normalize_events(conn, events):
    """Resolve native message task/op references from earlier immutable source events.

    Never consult mutable current task state: a later replay at the same cutoff must
    give the same operation and pairing key. The original event dictionaries stay intact.
    """
    normalized = []
    for source in events:
        event = dict(source)
        data = event.get("data") or {}
        if isinstance(data, str):
            data = json.loads(data)
        event["data"] = data
        event["op"] = event.get("op") or data.get("op")
        if event.get("kind") == "msg":
            for target in (event.get("ref"), data.get("to")):
                if not target:
                    continue
                prior = conn.execute(
                    "SELECT op FROM events WHERE ref=? AND id<=? AND op IS NOT NULL "
                    "AND op<>'' ORDER BY id LIMIT 1", (str(target), event["id"]),
                ).fetchone()
                if prior:
                    event["op"] = event.get("op") or prior["op"]
                    event["_pair_key"] = str(target)
                    break
        normalized.append(event)
    return normalized


class TranscriptUnavailable(OSError):
    """A registered transcript has no readable native or preserved source."""


def _transcript_path(root: str, session: str, conn, override: str | None) -> str | None:
    from alpaca import transcripts
    registered = override or transcripts.registered(conn, session)
    if registered:
        candidate = registered if os.path.isabs(registered) else os.path.join(root, registered)
        if os.path.isfile(candidate):
            return candidate
    candidate = transcripts.local_path(root, session)
    if os.path.isfile(candidate):
        return candidate
    if registered:
        raise TranscriptUnavailable("registered transcript is unavailable: " + session)
    return None


def _visible_transcript(session, path):
    """Stream complete source records through the existing privacy/shape normalizer.

    UI pagination budgets do not apply to durable capture. Full visible values still
    use the canonical secret redactor; private reasoning is dropped by normalize.
    """
    import hashlib
    from alpaca.analytics import detail

    class Capture(detail._Capture):
        def rows(self, path, kind, **kwargs):
            digest = hashlib.sha256()
            source_id = hashlib.sha256((self.sid + ":transcript").encode()).hexdigest()[:24]
            with open(path, "rb") as stream:
                offset = 0
                for number, raw in enumerate(stream, 1):
                    digest.update(raw)
                    source = {"id": source_id, "kind": kind, "line": number, "offset": offset}
                    offset += len(raw)
                    if not raw.strip():
                        continue
                    try:
                        record = json.loads(raw)
                    except (ValueError, UnicodeError, RecursionError) as exc:
                        raise OSError("transcript contains an incomplete or malformed record") from exc
                    if not isinstance(record, dict) or record.get("type") == "capture-malformed-record":
                        raise OSError("transcript contains a malformed record")
                    yield record, source
            self.source_sha256 = digest.hexdigest()
            self.source_bytes = offset

        def message(self, role, text, *args, **kwargs):
            message = super().message(role, text, *args, **kwargs)
            message["text"] = detail._redact(text)
            message["truncated"] = False
            return message

        def call(self, msg, ident, name, value, source):
            tool = super().call(msg, ident, name, value, source)
            tool["input"] = detail._redact(value)
            tool["input_truncated"] = False
            return tool

        def result(self, ident, value, *args, **kwargs):
            tool = super().result(ident, value, *args, **kwargs)
            tool["result"] = detail._redact(value)
            tool["result_truncated"] = False
            return tool

    capture = Capture(session, "registered-transcript", detail._Budget())
    capture.transcript(path)
    capture.title = detail._redact(capture.title)
    return capture


def _transcript_doc(root: str, session: str, op: str, conn, override: str | None):
    tp = _transcript_path(root, session, conn, override)
    if not tp:
        return None
    try:
        visible = _visible_transcript(session, tp)
    except (FileNotFoundError, PermissionError, IsADirectoryError) as exc:
        raise TranscriptUnavailable("registered transcript is unreadable: " + session) from exc
    # Transcript identity is session-based, independent of the current event batch/op.
    lines = ["role: transcript", "op: session", "session: %s" % session,
             "trust: untrusted source", "source: " + tp,
             "source_sha256: " + visible.source_sha256,
             "source_bytes: " + str(visible.source_bytes), ""]
    if visible.title:
        lines.extend(["# " + str(visible.title).replace("\n", " ").strip(), ""])
    for message in visible.messages:
        text = message.get("text") or ""
        if text:
            lines.extend(["", message["role"] + ":", text])
        for tool in message["tools"]:
            lines.extend(["", "tool: " + tool["name"]])
            for field in ("input", "result"):
                value = tool.get(field)
                if value is not None:
                    rendered = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=True)
                    lines.extend([field + ":", rendered])
    # Keep immutable source projections for audit, retiring old versions from search.
    import hashlib
    text = "\n".join(lines).replace("[[", "[ [").replace("]]", "] ]") + "\n"
    version = hashlib.sha256(text.encode("utf-8")).hexdigest()
    identity = hashlib.sha256(session.encode("utf-8")).hexdigest()[:16]
    doc_id = "raw/transcripts/%s/transcript-%s-summary-%s.md" % (identity, _slug(session), version)
    return doc_id, text


def projection_identity(text):
    """One source event or session, regardless of renderer version or operation label."""
    meta = {}
    for line in text.split("\n\n", 1)[0].splitlines():
        key, separator, value = line.partition(":")
        if separator:
            meta[key] = value.strip()
    if meta.get("event"):
        return "event:" + meta["event"]
    if meta.get("role") == "transcript" and meta.get("session"):
        return "transcript:" + meta["session"]
    return None


def capture(root, events, *, sessions=(), clock=None, transcript_paths=None, absorber=None):
    """Shared bounded event-batch capture for drains, collector and recovery."""
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import Absorber

    cfg = Config.for_project(root)
    vault = str(cfg.vault_dir)
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    conn = tedb.connect(root)
    try:
        events = normalize_events(conn, events)
        prefixes = observed_prefixes(root)
        docs = [_note_doc(event, prefixes=prefixes) for event in events]
        for session in dict.fromkeys(sessions):
            document = _transcript_doc(root, session, "session", conn,
                                       (transcript_paths or {}).get(session))
            if document:
                docs.append(document)
    finally:
        conn.close()
    owns_absorber = absorber is None
    if owns_absorber:
        absorber = Absorber(cfg, **({"clock": clock} if clock is not None else {}))
    elif absorber.cfg.vault_dir.resolve() != cfg.vault_dir.resolve():
        raise ValueError("recovery absorber belongs to another vault")
    summary = {"notes": 0, "ingested": 0, "skipped": 0, "docs": [], "transcripts": [],
               "through_event": max((event["id"] for event in events), default=0)}
    try:
        # Only active first blocks count. Old immutable files remain evidence, not
        # competing searchable versions of the same source.
        previous = {}
        for row in absorber.db.conn.execute(
                "SELECT b.doc_id,b.text FROM blocks b JOIN docs d ON d.doc_id=b.doc_id "
                "WHERE d.kind='raw' AND b.status='active' AND b.ordinal=0"):
            identity = projection_identity(row["text"])
            if identity:
                previous.setdefault(identity, []).append(row["doc_id"])
        for doc_id, text in docs:
            fpath = os.path.join(vault, doc_id)
            with _projection_target(vault, doc_id) as parent_fd:
                result = absorber.absorb_text(doc_id, text, kind="raw", path=doc_id)
                _write_if_changed(fpath, text, parent_fd=parent_fd)
            old = [item for item in previous.get(projection_identity(text), []) if item != doc_id]
            if old:
                absorber.retire_documents(old, reason="source-projection-replaced", superseded_by=doc_id)
            if (projection_identity(text) or "").startswith("transcript:"):
                from alpaca.wiki.determinism import sha256_hex
                meta = dict(line.split(": ", 1) for line in text.split("\n\n", 1)[0].splitlines())
                summary["transcripts"].append({
                    "doc_id": doc_id, "content_sha256": sha256_hex(text),
                    "session": meta["session"], "source": meta["source"],
                    "source_sha256": meta["source_sha256"], "source_bytes": int(meta["source_bytes"]),
                })
            summary["notes"] += 1
            summary["docs"].append(doc_id)
            summary["skipped" if result.get("skipped") else "ingested"] += 1
    finally:
        if owns_absorber:
            absorber.close()
    return summary


@contextmanager
def _projection_target(vault, doc_id):
    """Pin the destination before ingest; reject symlinks in every path component."""
    from alpaca.wiki.store.db import open_vault_directory
    key = Path(doc_id)
    if key.is_absolute() or ".." in key.parts or not key.parts:
        raise ValueError("invalid projection path")
    _, directory = open_vault_directory(Path(vault))
    try:
        for component in key.parts[:-1]:
            try:
                os.mkdir(component, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        try:
            info = os.stat(key.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("non-regular projection file refused")
        yield directory
    finally:
        os.close(directory)


def _write_if_changed(path: str, text: str, *, parent_fd=None) -> bool:
    """Atomically publish accepted bytes through a pinned no-follow directory."""
    if parent_fd is None:
        with _projection_target(str(Path(path).parent), Path(path).name) as directory:
            return _write_if_changed(path, text, parent_fd=directory)
    data = text.encode("utf-8")
    name = Path(path).name
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    except FileNotFoundError:
        pass
    else:
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("non-regular projection file refused")
            if stream.read() == data:
                return False
    import uuid
    temporary = ".capture-" + uuid.uuid4().hex
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent_fd)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
    return True


def run(root: str, session: str, *, clock=None, transcript_path: str | None = None,
        through_event: int | None = None, absorber=None) -> dict:
    """Land one session's events and transcript into the wiki, idempotently.

    Returns a summary: how many notes were landed, how many were freshly ingested versus skipped by
    the hashgate, and the doc ids. A second call over the same session ingests nothing new.
    Recovery may lend an absorber to reuse its index across sessions; the caller owns it.
    """
    conn = tedb.connect(root)
    try:
        query = "SELECT * FROM events WHERE session=?"
        params = [session]
        if through_event is not None:
            query += " AND id<=?"
            params.append(through_event)
        events = [dict(row) for row in conn.execute(query + " ORDER BY id", params)]
    finally:
        conn.close()
    summary = capture(root, events, sessions=[session], clock=clock,
                      transcript_paths={session: transcript_path}, absorber=absorber)
    summary["session"] = session
    return summary
