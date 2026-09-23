"""Bounded, read-only session conversations from explicitly authorized sources.

The record registers external transcript paths. A request only supplies a session
ID and, optionally, an opaque child ID discovered under that transcript's child
directory. No account-wide discovery, database migration, or capture occurs here.
Counts describe the selected conversation, never a fold of its children. When a
read budget is exhausted, counts are lower bounds and coverage says so explicitly.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from alpaca import db, paths, pool, transcripts


MAX_READ_BYTES = 32 * 1024 * 1024
MAX_LINE_BYTES = 2 * 1024 * 1024
MAX_CONTENT_CHARS = 256 * 1024
MAX_RECORDS = 50_000
MAX_CHILDREN = 40
MAX_DIRECTORY_ENTRIES = 2000
MAX_PAGE = 200
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z")
_SECRET_KEY = re.compile(r"(?:password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|cookie|client[_-]?secret|private[_-]?key)\Z", re.I)
_SECRET_TEXT = re.compile(r"(?i)(\b(?:authorization\s*:\s*(?:bearer|basic)|bearer)\s+)[^\s\"']+|"
                          r"(\b(?:[A-Z0-9_]*(?:API_KEY|ACCESS_TOKEN|SECRET|PASSWORD))\s*[=:]\s*[\"']?)[^\s\"']+")
_PROVIDER_KEY = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,})\b")


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _inside(path, directory):
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(directory)]) == os.path.realpath(directory)
    except (ValueError, OSError):
        return False


def _local_file(path, root):
    return _inside(path, root) and os.path.isfile(path)


def _redact(value, depth=0):
    if depth > 24:
        return "[nested content omitted]"
    if isinstance(value, str):
        value = _SECRET_TEXT.sub(lambda m: (m.group(1) or m.group(2)) + "[redacted]", value)
        return _PROVIDER_KEY.sub("[redacted]", value)
    if isinstance(value, dict):
        return {str(k): "[redacted]" if _SECRET_KEY.search(str(k)) else _redact(v, depth + 1)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, depth + 1) for v in value]
    return value


def _bounded(value):
    value = _redact(value)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return (text[:MAX_CONTENT_CHARS], True) if len(text) > MAX_CONTENT_CHARS else (value, False)


def _text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(b["text"] for b in content if isinstance(b, dict)
                     and b.get("type") in ("text", "input_text", "output_text")
                     and isinstance(b.get("text"), str))


class _Budget:
    def __init__(self):
        self.remaining = MAX_READ_BYTES
        self.records = 0


class _Capture:
    def __init__(self, sid, kind, budget):
        self.sid = sid
        self.kind = kind
        self.budget = budget
        self.messages = []
        self.tools = {}
        self.title = ""
        self.operator = None
        self.stats = {"malformed_lines": 0, "partial_lines": 0, "bytes_read": 0,
                      "truncated": False, "total_is_exact": True, "unreadable_sources": 0}
        self.visible = None
        self.seen_records = set()

    def rows(self, path, kind, *, max_line_bytes=None):
        # A registered file and its durable project copy are one logical source.
        family = "transcript" if kind.endswith("transcript") else kind
        source_id = _hash(self.sid + ":" + family)
        line_bytes = MAX_LINE_BYTES if max_line_bytes is None else max_line_bytes
        try:
            try:
                with open(str(path) + ".source.json", "rb") as state_file:
                    state = json.loads(state_file.read(65536))
                if state.get("partial_line"):
                    self.stats["partial_lines"] += 1
                    self.stats["total_is_exact"] = False
            except FileNotFoundError:
                pass
            except (OSError, ValueError, AttributeError):
                self.stats["total_is_exact"] = False
                self.stats["malformed_lines"] += 1
            with open(path, "rb") as fh:
                size = os.fstat(fh.fileno()).st_size
                number = 0
                while fh.tell() < size:
                    if self.budget.remaining <= 0 or self.budget.records >= MAX_RECORDS:
                        self.stats.update(truncated=True, total_is_exact=False)
                        break
                    offset = fh.tell()
                    amount = min(line_bytes + 1, self.budget.remaining, size - offset)
                    raw = fh.readline(amount)
                    number += 1
                    self.stats["bytes_read"] += len(raw)
                    self.budget.remaining -= len(raw)
                    self.budget.records += 1
                    if not raw.endswith(b"\n"):
                        if fh.tell() == size:
                            self.stats["partial_lines"] += 1
                            self.stats["total_is_exact"] = False
                        else:
                            self.stats.update(truncated=True, total_is_exact=False)
                            if len(raw) > line_bytes:
                                # A large image/tool result must not hide all later work.
                                # Discard the rest of this one record within the same budget.
                                while self.budget.remaining > 0 and fh.tell() < size:
                                    tail = fh.readline(min(65536, self.budget.remaining))
                                    self.budget.remaining -= len(tail)
                                    self.stats["bytes_read"] += len(tail)
                                    if tail.endswith(b"\n"):
                                        break
                                else:
                                    break
                                if tail.endswith(b"\n"):
                                    continue
                        break
                    if len(raw) > line_bytes:
                        self.stats.update(truncated=True, total_is_exact=False)
                        continue
                    if not raw.strip():
                        continue
                    try:
                        obj = json.loads(raw)
                    except (ValueError, UnicodeError, RecursionError):
                        self.stats["malformed_lines"] += 1
                        self.stats["total_is_exact"] = False
                        continue
                    if not isinstance(obj, dict):
                        self.stats["malformed_lines"] += 1
                        self.stats["total_is_exact"] = False
                        continue
                    if obj.get("type") == "capture-malformed-record":
                        self.stats["malformed_lines"] += 1
                        self.stats["total_is_exact"] = False
                    yield obj, {"id": source_id, "kind": kind, "line": number, "offset": offset}
        except OSError:
            self.stats["unreadable_sources"] += 1

    def message(self, role, text, source, ts=None, identity=None, **extra):
        text, cut = _bounded(text)
        self.stats["truncated"] |= cut
        ident = _hash(source["id"] + ":" + str(identity or source.get("offset", source.get("event_id"))) + ":" + role)
        msg = {"id": ident, "ts": ts, "role": role, "text": text, "tools": [],
               "source": source, "truncated": cut, **extra}
        self.messages.append(msg)
        return msg

    def call(self, msg, ident, name, value, source):
        ident = str(ident or _hash(msg["id"] + ":" + str(len(msg["tools"]))))
        if ident in self.tools:
            return self.tools[ident]
        value, cut = _bounded(value)
        tool = {"id": ident, "name": str(name or "tool"), "input": value, "result": None,
                "status": "unmatched-start", "input_truncated": cut, "result_truncated": False,
                "sources": [source]}
        msg["tools"].append(tool)
        self.tools[ident] = tool
        self.stats["truncated"] |= cut
        return tool

    def result(self, ident, value, source, ts=None, error=False, truncated=False):
        tool = self.tools.get(str(ident))
        if tool is None:
            msg = self.message("tool", "", source, ts, identity=ident)
            tool = self.call(msg, ident, "tool result", None, source)
            tool["status"] = "unmatched-result"
        else:
            tool["status"] = "error" if error else "completed"
        tool["result"], cut = _bounded(value)
        tool["result_truncated"] = bool(cut or truncated)
        if source not in tool["sources"]:
            tool["sources"].append(source)
        self.stats["truncated"] |= tool["result_truncated"]
        return tool

    def transcript(self, path):
        for record, source in self.rows(path, self.kind):
            self.normalize(record, source)

    def normalize(self, rec, source):
        rec = transcripts.visible_record(rec)
        if rec is None:
            return
        kind = rec.get("type")
        ts = rec.get("timestamp") or rec.get("ts")
        identity = rec.get("uuid") or rec.get("id")
        identity = str(identity) if isinstance(identity, (str, int)) else None
        ts = ts if isinstance(ts, str) else None
        if identity:
            key = (str(kind), identity)
            if key in self.seen_records:
                return
            self.seen_records.add(key)
        if kind in ("ai-title", "custom-title"):
            self.title = rec.get("customTitle") or rec.get("aiTitle") or self.title
        elif kind == "queue-operation" and rec.get("operation") == "enqueue":
            text = _text(rec.get("content"))
            if text:
                self.message("user", text, source, ts, identity, queued=True,
                             queue_id=rec.get("queueId") or rec.get("queue_id"))
        elif kind in ("user", "assistant"):
            self.operator = self.operator or "claude"
            message = rec.get("message")
            if not isinstance(message, dict):
                return
            content = message.get("content")
            text = _text(content)
            blocks = content if isinstance(content, list) else []
            calls = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
            msg = None
            if text or calls:
                msg = self.message(kind, text, source, ts, identity,
                                   model=message.get("model"))
                usage = message.get("usage")
                if isinstance(usage, dict):
                    msg["usage"] = {k: v for k, v in usage.items() if k in (
                        "input_tokens", "output_tokens", "cache_read_input_tokens",
                        "cache_creation_input_tokens") and type(v) is int and v >= 0}
                for block in calls:
                    self.call(msg, block.get("id"), block.get("name"), block.get("input"), source)
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    value = block.get("content")
                    if isinstance(value, list):
                        value = _text(value)
                    self.result(block.get("tool_use_id"), value, source, ts, block.get("is_error", False))
        elif kind in ("response_item", "event_msg"):
            self.operator = "codex"
            payload = rec.get("payload")
            if not isinstance(payload, dict):
                return
            typ = payload.get("type")
            if kind == "response_item" and typ == "message":
                role = payload.get("role")
                # Only user-visible conversation, never private analysis or summaries.
                if role not in ("user", "assistant") or payload.get("channel") == "analysis":
                    return
                self.codex_message(role, _text(payload.get("content")), source, ts,
                                   payload.get("id") or identity, kind, payload.get("phase"), payload.get("turn_id"))
            elif kind == "event_msg" and typ in ("user_message", "agent_message"):
                self.codex_message("user" if typ == "user_message" else "assistant",
                                   _text(payload.get("message")), source, ts,
                                   payload.get("id") or identity, kind, payload.get("phase"), payload.get("turn_id"))
            elif kind == "response_item" and typ in ("function_call", "custom_tool_call"):
                value = payload.get("arguments", payload.get("input"))
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except (ValueError, RecursionError):
                        pass
                ident = payload.get("call_id") or payload.get("id")
                msg = self.message("assistant", "", source, ts, ident)
                self.call(msg, ident, payload.get("name"), value, source)
                self.visible = None
            elif kind == "response_item" and typ in ("function_call_output", "custom_tool_call_output"):
                self.result(payload.get("call_id"), payload.get("output"), source, ts)

    def codex_message(self, role, text, source, ts, identity, shape, phase, turn_id):
        if not text:
            return
        previous = self.visible
        # Codex exports the same visible turn through two adjacent event shapes.
        # Pair only opposite shapes at the same source timestamp (or native ID),
        # never repeated text within one shape or prompts at different times.
        if previous and previous["shape"] != shape and previous["role"] == role and previous["text"] == text:
            if ((ts and ts == previous["ts"]) or (identity and identity == previous["identity"])
                    or (turn_id and turn_id == previous["turn_id"])):
                previous["message"].setdefault("mirror_sources", []).append(source)
                self.visible = None
                return
        msg = self.message(role, text, source, ts, identity, phase=phase)
        self.visible = {"shape": shape, "role": role, "text": text, "ts": ts,
                        "identity": identity, "turn_id": turn_id, "message": msg}

    def pool(self, path, sid, *, matching_only=False):
        for rec, source in self.rows(path, "tool-pool"):
            if rec.get("sid") != sid or rec.get("phase") not in ("pre", "post"):
                continue
            ident = rec.get("tool_use_id")
            tool = self.tools.get(str(ident)) if ident else None
            if tool is None and matching_only:
                # A session pool can contain child or earlier retained history.
                # Only the selected transcript establishes conversation membership.
                continue
            if tool is None:
                msg = self.message("tool", "", source, rec.get("ts"), identity=ident)
                tool = self.call(msg, ident, rec.get("tool"), rec.get("input"), source)
            elif tool["input"] is None and rec.get("input") is not None:
                tool["input"], tool["input_truncated"] = _bounded(rec["input"])
            tool["input_truncated"] |= bool(rec.get("input_truncated"))
            self.stats["truncated"] |= tool["input_truncated"]
            if source not in tool["sources"]:
                tool["sources"].append(source)
            if rec["phase"] == "post" and rec.get("response") is not None and tool["result"] is None:
                self.result(tool["id"], rec["response"], source, rec.get("ts"),
                            truncated=rec.get("response_truncated", False))


def _connection(root):
    return db.connect_readonly(root)


def _record_rows(conn, sid, capture, table):
    """Read payloads only after checking their size against the shared budget."""
    payload = "data" if table == "events" else "body"
    columns = "actor,kind,ref" if table == "events" else "sender,to_,kind,ref"
    exclude = " AND kind NOT IN ('heartbeat','transcript-snapshot','msg')" if table == "events" else ""
    query = ("SELECT id,ts,%s,length(CAST(%s AS BLOB)) AS size FROM %s "
             "WHERE session=?%s ORDER BY id DESC LIMIT ?") % (columns, payload, table, exclude)
    rows = []
    for row in conn.execute(query, (sid, MAX_RECORDS + 1)):
        size = row["size"] or 0
        if size > capture.budget.remaining or capture.budget.records >= MAX_RECORDS:
            capture.stats.update(truncated=True, total_is_exact=False)
            break
        row = dict(row)
        if size > MAX_LINE_BYTES:
            capture.stats.update(truncated=True, total_is_exact=False)
            continue
        value = conn.execute("SELECT %s FROM %s WHERE id=?" % (payload, table), (row["id"],)).fetchone()[0]
        capture.stats["bytes_read"] += size
        capture.budget.remaining -= size
        capture.budget.records += 1
        row[payload] = value
        rows.append(row)
    return reversed(rows)


def _record(conn, sid, capture):
    if conn is None:
        return
    for row in _record_rows(conn, sid, capture, "events"):
        try:
            data = json.loads(row["data"])
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        texts = [str(data[k]) for k in ("note", "body", "statement", "reason", "summary", "next_action") if data.get(k)]
        text = "\n".join(dict.fromkeys(texts)) or row["kind"].replace("-", " ")
        capture.message("record", text, {"id": "record", "kind": "record", "event_id": row["id"]},
                        row["ts"], identity="event:" + str(row["id"]), actor=row["actor"],
                        kind=row["kind"], ref=row["ref"])
    for row in _record_rows(conn, sid, capture, "messages"):
        capture.message("message", row["body"] or "", {"id": "messages", "kind": "record-message", "event_id": row["id"]},
                        row["ts"], identity="message:" + str(row["id"]), actor=row["sender"],
                        recipient=row["to_"], kind=row["kind"], ref=row["ref"])


def _children(root, sid, registered):
    directories = []
    if registered:
        path = Path(registered)
        directories.append(path.parent / path.stem / "subagents")
    directories.append(Path(transcripts.subagents_dir(root, sid)))
    found = {}
    scanned = 0
    truncated = False
    for directory in directories:
        if directory.is_symlink() or directory.parent.is_symlink():
            continue
        if directory == directories[-1] and not _inside(directory, root):
            continue
        stack = [directory]
        while stack:
            current = stack.pop()
            if not _inside(current, directory):
                continue
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        scanned += 1
                        if scanned > MAX_DIRECTORY_ENTRIES:
                            return found, True
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.name.startswith("agent-") and entry.name.endswith(".jsonl") and entry.is_file(follow_symlinks=False):
                            rel = Path(entry.path).relative_to(directory).as_posix()
                            ident = "child-" + _hash(rel)
                            if ident not in found:
                                if len(found) >= MAX_CHILDREN:
                                    truncated = True
                                    continue
                                found[ident] = Path(entry.path)
            except OSError:
                continue
    conn = _connection(root)
    try:
        registered_children = conn.execute("SELECT source_id,session,locator FROM obs_source WHERE parent_session=? AND locator IS NOT NULL ORDER BY source_id", (sid,))
        for row in registered_children:
            child_path = Path(row['locator'])
            if not child_path.is_absolute():
                child_path = Path(root) / child_path
            ident = 'child-' + _hash(row['source_id'])
            matching = []
            for old_id, candidate in found.items():
                native_candidate = candidate
                local_dir = Path(transcripts.subagents_dir(root, sid))
                if registered and _inside(candidate, local_dir):
                    native_candidate = Path(registered).parent / Path(registered).stem / 'subagents' / Path(candidate).relative_to(local_dir)
                if os.path.realpath(native_candidate) == os.path.realpath(child_path):
                    matching.append((old_id, candidate))
            retained = Path(transcripts.local_path(root, row['session']))
            available = child_path if child_path.is_file() and not child_path.is_symlink() else retained if _local_file(retained, root) else matching[0][1] if matching else None
            if available is None:
                continue
            for old_id, _candidate in matching:
                del found[old_id]
            if len(found) >= MAX_CHILDREN:
                truncated = True
                break
            found[ident] = available
    except sqlite3.OperationalError as exc:
        if 'no such table' not in str(exc):
            raise
    finally:
        conn.close()
    return dict(sorted(found.items())), truncated


def source_revision(root, sid):
    """Metadata revision of every bounded detail source, including record fallback."""
    root = os.path.realpath(root)
    conn = _connection(root)
    try:
        row = conn.execute("SELECT transcript,started,ended FROM sessions WHERE sid=?", (sid,)).fetchone() if conn else None
        registered = transcripts.registered(conn, sid)
        if registered and not os.path.isabs(registered):
            registered = os.path.join(root, registered)
        local = transcripts.local_path(root, sid)
        primary = registered if registered and os.path.isfile(registered) else local
        children, cut = _children(root, sid, registered)
        candidates = [primary, str(primary) + ".source.json", pool.path(root, sid), *children.values(),
                      *(str(path) + ".source.json" for path in children.values())]
        values = []
        for candidate in candidates:
            if candidate != registered and not _inside(candidate, root):
                # Authorized children of a registered transcript may be external.
                if candidate not in children.values():
                    continue
            try:
                stat = os.stat(candidate)
                stamp = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
            except OSError:
                stamp = None
            values.append([_hash(str(candidate)), stamp])
        heads = [conn.execute("SELECT COALESCE(MAX(id),0) FROM " + table + " WHERE session=?", (sid,)).fetchone()[0]
                 for table in ("events", "messages")] if conn else [0, 0]
        manifest = conn.execute("SELECT key,value FROM meta WHERE key IN (?,?) ORDER BY key", ('capture-manifest:' + sid, 'operator:' + sid)).fetchall()
        try:
            sources = [dict(item) for item in conn.execute("SELECT * FROM obs_source WHERE session=? OR parent_session=? ORDER BY source_id", (sid, sid))]
        except sqlite3.OperationalError as exc:
            if 'no such table' not in str(exc):
                raise
            sources = []
        return {"sources": values, "record_heads": heads, "children_truncated": cut,
                "capability_revision": _hash(json.dumps([dict(item) for item in manifest] + sources, sort_keys=True)),
                "window": [row["started"], row["ended"]] if row else []}
    finally:
        if conn:
            conn.close()


def _coverage(capture, has_transcript, has_record, source, children_truncated=False):
    stats = capture.stats.copy()
    partial = (stats["truncated"] or stats["malformed_lines"] or stats["partial_lines"]
               or stats["unreadable_sources"] or children_truncated)
    if has_transcript:
        state = "partial" if partial else "captured"
        reason = "Visible records from the available transcript."
    elif capture.tools:
        state = "partial"
        reason = "Transcript unavailable; captured tool calls and attributed record entries are shown."
    elif has_record:
        state = "record-only"
        reason = "Transcript unavailable; attributed record entries do not capture the full conversation."
    else:
        state = "unavailable"
        reason = "No readable transcript or attributed conversation capture is available."
    if partial:
        reason += " Some source content is incomplete, malformed, unreadable, or exceeds the read budget."
    stats["total_is_exact"] = bool(has_transcript and not partial)
    return {"state": state, "reason": reason, "source": source, **stats,
            "file_complete": bool(has_transcript and not partial),
            "session_complete": False,
            "child_coverage": {"state": "unknown", "expected": None, "missing": []},
            "children_truncated": children_truncated, "usage": "unavailable",
            "totals_include_children": False}


def _session(capture, sid, row, operator, child=None):
    title = capture.title or next((m["text"].split("\n", 1)[0] for m in capture.messages if m["role"] == "user" and m["text"] and not m["text"].lstrip().startswith(
        ("<recommended_plugins>", "<environment_context>", "# AGENTS.md", "<INSTRUCTIONS>"))), "")
    return {"sid": sid, "title": _redact(str(title))[:180] or (operator or capture.operator or "Operator") + " session",
            "operator": operator or capture.operator or "unknown", "started": row.get("started"),
            "ended": row.get("ended"), "parent_sid": sid if child else None, "child_id": child,
            "turns": sum(m["role"] == "assistant" for m in capture.messages), "tool_calls": len(capture.tools),
            "usage_available": False, "tokens": None, "cost_reported_usd": None}


def _rollup(capture):
    tools, hourly, prompts, stamps = {}, {}, [], []
    for message in capture.messages:
        stamp = message.get("ts")
        if isinstance(stamp, str) and len(stamp) >= 13:
            stamps.append(stamp)
            hour = hourly.setdefault(stamp[:13], {"turns": 0, "tools": 0, "prompts": 0})
        else:
            hour = None
        if message["role"] == "assistant" and hour is not None:
            hour["turns"] += 1
        if message["role"] == "user" and message["text"]:
            prompts.append({"ts": stamp, "text": str(message["text"])[:1600]})
            if hour is not None:
                hour["prompts"] += 1
        for tool in message["tools"]:
            tools[tool["name"]] = tools.get(tool["name"], 0) + 1
            if hour is not None:
                hour["tools"] += 1
    return {"tools": tools, "hourly": hourly, "human_prompts": prompts,
            "first": min(stamps) if stamps else None, "last": max(stamps) if stamps else None}


def detail(root, sid, before=None, limit=80, child=None, *, include_rollup=False, around=None, after=None):
    """Return the latest chronological page; next_before retrieves earlier records.

    IDs are stable across appends. Invalid/expired cursors raise ValueError rather
    than silently restarting pagination. KeyError means an unknown session.
    Individual content is capped with flags; total_is_exact is false when the
    bounded scan cannot establish the complete number of available messages.
    """
    if not isinstance(sid, str) or not _IDENTIFIER.fullmatch(sid):
        raise ValueError("invalid session identifier")
    if child is not None and (not isinstance(child, str) or not re.fullmatch(r"child-[0-9a-f]{24}", child)):
        raise ValueError("invalid child identifier")
    if before is not None and (not isinstance(before, str) or not re.fullmatch(r"[0-9a-f]{24}", before)):
        raise ValueError("invalid conversation cursor")
    if after is not None and (not isinstance(after, str) or not re.fullmatch(r"[0-9a-f]{24}", after)):
        raise ValueError("invalid conversation cursor")
    if sum(value is not None for value in (before, after, around)) > 1:
        raise ValueError("choose one conversation cursor")
    if around is not None:
        try:
            around = int(around)
        except (TypeError, ValueError):
            raise ValueError("invalid source line") from None
        if around < 1:
            raise ValueError("invalid source line")
    try:
        limit = max(1, min(MAX_PAGE, int(limit)))
    except (TypeError, ValueError):
        raise ValueError("invalid page limit") from None
    root = os.path.realpath(root)
    conn = _connection(root)
    try:
        row = conn.execute("SELECT * FROM sessions WHERE sid=?", (sid,)).fetchone() if conn else None
        row = dict(row) if row else {}
        operator_row = conn.execute("SELECT value FROM meta WHERE key=?", ("operator:" + sid,)).fetchone() if conn else None
        operator = operator_row[0] if operator_row else None
        registered = transcripts.registered(conn, sid)
        if registered and not os.path.isabs(registered):
            registered = os.path.join(root, registered)
        local = transcripts.local_path(root, sid)
        pool_path = pool.path(root, sid)
        source = "none"
        chosen = None
        if registered and os.path.isfile(registered):
            chosen, source = registered, "registered-transcript"
        elif _local_file(local, root):
            chosen, source = local, "project-transcript"
        if not row and not chosen and not _local_file(pool_path, root):
            raise KeyError("unknown session")
        children, children_truncated = _children(root, sid, registered)
        if child is not None and child not in children:
            raise ValueError("unknown child identifier")
        budget = _Budget()
        if child:
            chosen = children[child]
            source = "project-transcript" if _inside(chosen, root) else "registered-transcript"
        capture = _Capture(sid + (":" + child if child else ""), source, budget)
        if chosen:
            capture.transcript(chosen)
        has_transcript = bool(chosen and not capture.stats["unreadable_sources"])
        if not child:
            if _local_file(pool_path, root):
                capture.pool(pool_path, sid, matching_only=has_transcript)
                if not chosen:
                    source = "tool-pool"
            if not has_transcript or not capture.messages:
                _record(conn, sid, capture)
                if not chosen and source == "none" and capture.messages:
                    source = "explicit-record"
        child_items = []
        if not child:
            for ident, path in children.items():
                nested = _Capture(sid + ":" + ident, "project-transcript" if _inside(path, root) else "registered-transcript", budget)
                nested.transcript(path)
                info = _session(nested, sid, {}, operator, ident)
                coverage = _coverage(nested, not nested.stats["unreadable_sources"], False, nested.kind)
                child_items.append({"id": ident, "title": info["title"], "parent_sid": sid,
                                    "turns": info["turns"], "tool_calls": info["tool_calls"], "coverage": coverage})
                capture.stats["bytes_read"] += nested.stats["bytes_read"]
                if not nested.stats["total_is_exact"]:
                    children_truncated = True
        # Stable source order wins ties. Records without timestamps retain capture
        # order, rather than sorting undated tool calls ahead of their conversation.
        messages = capture.messages
        if messages and all(m["ts"] for m in messages):
            messages.sort(key=lambda m: m["ts"])
        end = len(messages)
        if before:
            end = next((i for i, message in enumerate(messages) if message["id"] == before), -1)
            if end < 0:
                raise ValueError("conversation cursor is unavailable; reload this session")
        start = max(0, end - limit)
        if around is not None:
            candidates = [(abs(m["source"]["line"] - around), i) for i, m in enumerate(messages)
                          if isinstance(m["source"].get("line"), int)
                          and m["source"].get("kind", "").endswith("transcript")]
            if not candidates:
                raise ValueError("source line is not available in the captured conversation")
            center = min(candidates)[1]
            start = max(0, center - limit // 2)
            end = min(len(messages), start + limit)
        elif after:
            index = next((i for i, message in enumerate(messages) if message["id"] == after), -1)
            if index < 0:
                raise ValueError("conversation cursor is unavailable; reload this session")
            start = index + 1
            end = min(len(messages), start + limit)
        page = messages[start:end]
        return {"session": _session(capture, sid, row if not child else {}, operator, child),
                "messages": page, "children": child_items,
                "coverage": _coverage(capture, has_transcript, bool(messages), source, children_truncated),
                "next_before": page[0]["id"] if start and page else None,
                "next_after": page[-1]["id"] if end < len(messages) and page else None,
                "has_later": end < len(messages),
                "total": len(messages), "has_more": start > 0,
                **({"rollup": _rollup(capture)} if include_rollup else {})}
    finally:
        if conn:
            conn.close()
