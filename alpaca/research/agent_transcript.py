"""The reader of a subagent transcript, for the session runtime's judge (K5 to K12, K15, K16).

A subagent of a Claude Code session writes its own transcript beside the session's, as one JSON
record per line: user records (the spawn prompt, tool results), assistant records (text, thinking,
tool_use, the model and its usage) and attachment records (`type: attachment`, with the runtime's
own additions: hook bodies, the `instructions` files, `queued_command` and so on). Every record
carries the driving session's `sessionId`, the agent's `agentId` and `isSidechain` true. The
shapes are the ones measured on this machine (in-session-runtime.md section 8.1 and the measured
facts it cites); nothing here guesses beyond them, and a record shape this reader does not know is
returned as it is and never trusted.

This module only reads. The tool calls come from launcher._claude_calls, the reader the audit and
the verifier scans already use, so a session transcript and a headless stream are scanned by the
same code (in-session-runtime.md section 7). The Codex rollout form is read by
launcher._codex_calls; the Codex witnesses are not mapped yet (section 8.2), so this module reads
a Codex rollout only for its calls.

Design: design/research-engine/in-session-runtime.md sections 5, 8 and 14.
"""
from __future__ import annotations

import datetime
import json
import os
import re

#: the runtime's own tools, which are never a job's off-list calls (K9 and the guard)
RUNTIME_TOOLS = ("SubagentHandback", "ToolSearch")
#: the tools no job is ever given: the issue refuses a role whose list holds one, and the headless
#: launcher disables them (--disallowedTools, settings deny). The live deny and K9 read the job's
#: own allow list, never this list (in-session-runtime.md section 26).
FORBIDDEN_TOOLS = ("Agent", "Task", "Workflow", "Skill", "SendMessage", "ListAgents", "WebFetch",
                   "WebSearch", "PushNotification", "RemoteTrigger", "CronCreate", "CronDelete",
                   "CronList", "ScheduleWakeup", "DesignSync", "EnterWorktree", "ExitWorktree")
#: the text the runtime puts in front of a relayed message of the coordinator
_COORDINATOR = re.compile(r"coordinator sent a message", re.IGNORECASE)
_PERSISTED = re.compile(r"Full output saved to:\s*(\S+)")
_LINE_PREFIX = re.compile(r"^\s*[0-9]+\t", re.MULTILINE)
_REMINDER = re.compile(r"\s*<system-reminder>.*?</system-reminder>\s*$", re.DOTALL)
_FRAME_TAIL = "The computed task text follows:"


# ================================================================ reading
def ends_on_whole_line(path: str) -> bool:
    """True when the file is empty or its last byte is a newline (the file is complete)."""
    try:
        size = os.path.getsize(path)
        if size == 0:
            return True
        with open(path, "rb") as fh:
            fh.seek(size - 1)
            return fh.read(1) == b"\n"
    except OSError:
        return False


def read_records(path: str) -> list:
    """Every JSON object of the file, in order. A line that does not parse is kept as
    {"_unreadable": <text>} so a forged or damaged line is visible to the checks and never
    silently dropped."""
    out = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                out.append({"_unreadable": raw[:200]})
                continue
            out.append(obj if isinstance(obj, dict) else {"_unreadable": raw[:200]})
    return out


def parse_time(text) -> datetime.datetime | None:
    try:
        got = datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if got.tzinfo is None:
        got = got.replace(tzinfo=datetime.timezone.utc)
    return got


def span(records: list) -> tuple:
    """(first record time, last record time) as datetimes, or (None, None)."""
    stamps = [parse_time(r.get("timestamp")) for r in records if r.get("timestamp")]
    stamps = [s for s in stamps if s is not None]
    if not stamps:
        return None, None
    return min(stamps), max(stamps)


def version(records: list) -> str | None:
    for r in records:
        if isinstance(r.get("version"), str):
            return r["version"]
    return None


# ================================================================ the message
def content_items(rec: dict) -> list:
    """The content of a record's message as a list of items: a str becomes one text item."""
    msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
    content = msg.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [c for c in content if isinstance(c, dict)]
    return []


def record_text(rec: dict) -> str:
    """Every piece of text a record carries, joined: message text, tool results, attachment
    payloads. K16 reads the text of every record, whatever kind carried it."""
    parts = []
    msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
    parts.append(_text_of(msg.get("content")))
    att = rec.get("attachment")
    if isinstance(att, dict):
        parts.append(_text_of(att))
    for key in ("content", "text", "summary", "snippet"):
        if isinstance(rec.get(key), (str, list, dict)):
            parts.append(_text_of(rec[key]))
    return "\n".join(p for p in parts if p)


def _text_of(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(_text_of(v) for v in value)
    if isinstance(value, dict):
        return "\n".join(_text_of(v) for v in value.values())
    return str(value)


def first_user_index(records: list) -> int | None:
    for i, r in enumerate(records):
        if r.get("type") == "user":
            return i
    return None


def _user_text(rec: dict) -> str | None:
    items = content_items(rec)
    if len(items) == 1 and items[0].get("type") == "text":
        return items[0].get("text") if isinstance(items[0].get("text"), str) else None
    return None


def first_turn(records: list, spawn_prompt: str) -> tuple:
    """(form, end index): `agent` when the first user record is the spawn prompt itself (an Agent
    tool agent), `workflow` when the first two user records are the runtime's frames and the
    spawn prompt follows the computed-task frame line, indented two spaces a line (measured facts
    lines 9 and 10); (None, None) for any other shape. The match is byte for byte on the prompt.
    The frame matcher does not spell the dash the runtime puts in its frame."""
    users = [i for i, r in enumerate(records) if r.get("type") == "user"]
    if not users:
        return None, None
    first = _user_text(records[users[0]])
    if first == spawn_prompt:
        return "agent", users[0]
    if len(users) >= 2 and first is not None and first.startswith("[Workflow harness"):
        second = _user_text(records[users[1]])
        if second is not None and second.startswith("[Workflow harness") and _FRAME_TAIL in second:
            tail = second.split(_FRAME_TAIL, 1)[1]
            if tail.startswith("\n"):
                tail = tail[1:]
            lines = tail.split("\n")
            plain = []
            for line in lines:
                if line.startswith("  "):
                    plain.append(line[2:])
                elif line.strip() == "":
                    plain.append("")
                else:
                    return None, None
            if "\n".join(plain).rstrip("\n") == spawn_prompt.rstrip("\n"):
                return "workflow", users[1]
    return None, None


# ================================================================ calls
def off_list(name, allowed) -> bool:
    """True when a call of tool `name` is off the job's list: the name is in neither `allowed`
    (the job's `allowed_tools`; None or empty means none) nor RUNTIME_TOOLS. The comparison is
    exact and case-sensitive. The guard (before the call) and K9 (after it) both call this one
    test, so the guard denies exactly the calls K9 refuses for their name (section 26)."""
    name = str(name)
    return name not in set(allowed or ()) and name not in RUNTIME_TOOLS


def calls(records: list) -> list:
    """The tool calls in order, each {tool, kind, input, paths, ts, text, output, id}: the reader
    of the headless stream (launcher._claude_calls), which keeps each call's id."""
    from alpaca.research import launcher
    return launcher._claude_calls(records)


def tool_names(records: list) -> list:
    out = []
    for r in records:
        if r.get("type") == "assistant":
            for item in content_items(r):
                if item.get("type") == "tool_use":
                    out.append(str(item.get("name") or ""))
    return out


def tool_use_ids(records: list) -> list:
    out = []
    for r in records:
        if r.get("type") == "assistant":
            for item in content_items(r):
                if item.get("type") == "tool_use" and item.get("id"):
                    out.append(str(item["id"]))
    return out


def first_call(records: list) -> dict | None:
    got = calls(records)
    return got[0] if got else None


def strip_read_output(text) -> str:
    """The text a Read call returned, with the `cat -n` line prefixes and a trailing
    <system-reminder> block removed (K7)."""
    text = _text_of(text)
    text = _REMINDER.sub("", text)
    return _LINE_PREFIX.sub("", text)


_TRUNCATED = re.compile(r"\[Truncated:\s*PARTIAL view[^\]]*\]", re.DOTALL)
_TOKENS_CAP = re.compile(r"\((\d+) tokens, cap (\d+)\)")


def read_truncation(records: list, output, path: str | None = None) -> dict | None:
    """The sign that a Read of a large file answered with a PARTIAL view, or None: the
    `[Truncated: PARTIAL view ...]` banner in the tool result, or a `read_truncation_notice`
    attachment that names `path` (measured: 3 results with the banner, each with the attachment,
    1caa0300 and fa1598d8; the banner names `(<n> tokens, cap 25000)`). Returns {"banner",
    "tokens", "cap"}; tokens and cap are None when the banner does not carry them."""
    banner = None
    found = _TRUNCATED.search(_text_of(output))
    if found:
        banner = found.group(0)
    else:
        for att in attachments(records):
            if att.get("type") == "read_truncation_notice":
                text = _text_of(att.get("banner"))
                if "PARTIAL view" in text and (path is None or path in text):
                    banner = text
                    break
    if banner is None:
        return None
    nums = _TOKENS_CAP.search(banner)
    return {"banner": banner[:300], "tokens": int(nums.group(1)) if nums else None,
            "cap": int(nums.group(2)) if nums else None}


# ================================================================ model, final, failure
def models(records: list) -> list:
    """The `message.model` of every assistant record, in order, except the runtime's own
    <synthetic> notices."""
    out = []
    for r in records:
        if r.get("type") != "assistant":
            continue
        msg = r.get("message") if isinstance(r.get("message"), dict) else {}
        m = msg.get("model")
        if isinstance(m, str) and m and m != "<synthetic>":
            out.append(m)
    return out


def final_message(records: list) -> str | None:
    """The final message: the `message` of the last SubagentHandback call, else the text of the
    last assistant record that has any (K11, measured facts lines 7 and 8). None when there is
    none."""
    handback = None
    last_text = None
    for r in records:
        if r.get("type") != "assistant":
            continue
        if r.get("isApiErrorMessage"):
            continue
        msg = r.get("message") if isinstance(r.get("message"), dict) else {}
        if msg.get("model") == "<synthetic>":
            continue
        texts = []
        for item in content_items(r):
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                texts.append(item["text"])
            elif item.get("type") == "tool_use" and item.get("name") == "SubagentHandback":
                inp = item.get("input") if isinstance(item.get("input"), dict) else {}
                if isinstance(inp.get("message"), str):
                    handback = inp["message"]
        if texts and any(t.strip() for t in texts):
            last_text = "\n".join(texts)
    return handback if handback is not None else last_text


def last_assistant(records: list) -> dict | None:
    for r in reversed(records):
        if r.get("type") == "assistant":
            return r
    return None


def api_error(records: list) -> dict | None:
    """The last assistant record when it is an API error notice: {"error", "status", "text"}."""
    r = last_assistant(records)
    if r is None or not r.get("isApiErrorMessage"):
        return None
    status = r.get("apiErrorStatus") or r.get("quotaLimits")
    return {"error": r.get("error"), "status": status, "text": record_text(r)}


def pending_call(records: list) -> bool:
    """True when the last assistant record holds a tool_use no later tool_result answers."""
    r = last_assistant(records)
    if r is None:
        return False
    ids = [str(i.get("id")) for i in content_items(r) if i.get("type") == "tool_use" and i.get("id")]
    if not ids:
        return False
    answered = set()
    after = False
    for rec in records:
        if rec is r:
            after = True
            continue
        if after and rec.get("type") == "user":
            for item in content_items(rec):
                if item.get("type") == "tool_result" and item.get("tool_use_id"):
                    answered.add(str(item["tool_use_id"]))
    return any(i not in answered for i in ids)


_PERSISTED_FORM = re.compile(
    r"\A<persisted-output>\nOutput too large \([^)\n]*\)\. Full output saved to: ([^\n]+)\n\n"
    r"Preview \(first 2KB\):\n(.*)\n\.\.\.\n</persisted-output>\Z", re.DOTALL)
#: the shortest preview that counts: about half of what a real record shows (1985 characters
#: for a file of 33504 bytes), since Claude Code's cut rule is not written down
PREVIEW_MIN = 1000


def results_dir_of(transcript_path: str | None) -> str | None:
    """The `tool-results/` folder of the driving session that an agent transcript
    <session folder>/subagents/**/agent-<id>.jsonl belongs to, or None for a path with no
    `subagents` part."""
    if not transcript_path:
        return None
    parts = os.path.abspath(str(transcript_path)).split(os.sep)
    if "subagents" not in parts:
        return None
    i = len(parts) - 1 - parts[::-1].index("subagents")
    return os.path.join(os.sep.join(parts[:i]), "tool-results")


def _persisted_file_ok(path: str, preview: str, real_dir: str) -> bool:
    if not os.path.isabs(path) or "\x00" in path:
        return False
    try:
        if os.path.realpath(os.path.dirname(path)) != real_dir:
            return False
        if os.path.islink(path) or not os.path.isfile(path):
            return False
        want = preview.encode("utf-8")
        with open(path, "rb") as fh:
            head = fh.read(len(want) + 8)
    except (OSError, ValueError):
        return False
    text = head.decode("utf-8", errors="replace")
    return text.startswith(preview) or text.startswith(preview.rstrip("\ufffd"))


def persisted_outputs(records: list, results_dir: str | None = None) -> set:
    """The files the agent's own tool results announced as persisted outputs it may read back
    (in-session-runtime.md section 18.1a). The folder is shared by every agent of the session, so
    only a file whose announcement is in the form Claude Code writes counts: the `tool_result`
    content starts with `<persisted-output>\nOutput too large (` and ends with
    `\n...\n</persisted-output>`; the named path is a direct child of `results_dir` (the driver
    session's `tool-results/` folder, results_dir_of), a regular file and not a link; and the
    preview between `Preview (first 2KB):\n` and `\n...\n</persisted-output>` is a prefix of that
    file and at least PREVIEW_MIN characters long. A Bash call's result is the command's own
    output, so `echo 'Full output saved to: <folder>'`, or `printf` of the whole wrapper with an
    empty or short preview, never counts. With no `results_dir` nothing counts. Each path is
    allowed as that exact file, never as a folder prefix."""
    out = set()
    if not results_dir:
        return out
    real_dir = os.path.realpath(results_dir)
    for r in records:
        if r.get("type") != "user":
            continue
        for item in content_items(r):
            if item.get("type") != "tool_result":
                continue
            content = item.get("content")
            text = content if isinstance(content, str) else (
                "".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
                if isinstance(content, list) else "")
            m = _PERSISTED_FORM.match(text)
            if not m:
                continue
            path, preview = m.group(1), m.group(2)
            if len(preview) >= PREVIEW_MIN and _persisted_file_ok(path, preview, real_dir):
                out.add(os.path.normpath(path))
    return out


# ================================================================ attachments and relays
def attachments(records: list) -> list:
    """Every attachment record's attachment object, with its record index under `_index`."""
    out = []
    for i, r in enumerate(records):
        att = r.get("attachment")
        if r.get("type") == "attachment" and isinstance(att, dict):
            out.append(dict(att, _index=i))
    return out


def hook_bodies(records: list) -> list:
    """Each `hook_additional_context` attachment with its provenance: [{index, tool_use_id,
    hook_name, content, command, stdout_ok, has_success}]. The provenance is the `hook_success`
    attachment with the same toolUseID and hookName (every one of the 6856 bodies measured has
    one, facts r6 line 10): its `command` names what made the body, and its `stdout` is a JSON
    object whose hookSpecificOutput.additionalContext must carry the body."""
    successes = {}
    for att in attachments(records):
        if att.get("type") == "hook_success":
            successes[(att.get("toolUseID"), att.get("hookName"))] = att
    out = []
    for att in attachments(records):
        if att.get("type") != "hook_additional_context":
            continue
        content = att.get("content")
        content = "\n".join(str(c) for c in content) if isinstance(content, list) else str(content or "")
        success = successes.get((att.get("toolUseID"), att.get("hookName")))
        stdout_ok = False
        command = None
        if success is not None:
            command = success.get("command")
            stdout = success.get("stdout")
            try:
                obj = json.loads(stdout) if isinstance(stdout, str) else stdout
                carried = (obj.get("hookSpecificOutput") or {}).get("additionalContext")
                carried = "\n".join(str(c) for c in carried) if isinstance(carried, list) else carried
                stdout_ok = isinstance(carried, str) and carried.strip() == content.strip()
            except (ValueError, AttributeError, TypeError):
                stdout_ok = False
        out.append({"index": att.get("_index"), "tool_use_id": att.get("toolUseID"),
                    "hook_name": att.get("hookName"), "content": content, "command": command,
                    "stdout_ok": stdout_ok, "has_success": success is not None})
    return out


def relay_problems(records: list, first_end: int) -> list:
    """The pushed text K8 rule 1 refuses, as [(index, why)]: after the first turn, a user-side
    record that holds anything but tool results and runtime reminders, a `queued_command`
    attachment that is not the runtime's own notification, and a text that says it came from
    the coordinator."""
    out = []
    for i, r in enumerate(records):
        if i <= (first_end if first_end is not None else -1):
            continue
        kind = r.get("type")
        if kind == "user":
            for item in content_items(r):
                t = item.get("type")
                if t == "tool_result":
                    if _COORDINATOR.search(_text_of(item.get("content"))[:400]):
                        out.append((i, "a tool result says the coordinator sent a message"))
                    continue
                text = item.get("text") if t == "text" else None
                if isinstance(text, str):
                    if text.lstrip().startswith("<system-reminder>"):
                        continue
                    out.append((i, "a user text after the first turn"))
                else:
                    out.append((i, "a user record of kind %s after the first turn" % t))
        elif kind == "attachment":
            att = r.get("attachment") if isinstance(r.get("attachment"), dict) else {}
            if att.get("type") == "queued_command":
                prompt = att.get("prompt")
                prompt = prompt if isinstance(prompt, str) else _text_of(prompt)
                if att.get("commandMode") != "task-notification" \
                        or not prompt.lstrip().startswith("<task-notification>"):
                    out.append((i, "a queued command that is not the runtime's own notification"))
        elif isinstance(kind, str) and kind in ("system", "summary"):
            text = record_text(r)
            if _COORDINATOR.search(text[:400]):
                out.append((i, "a record says the coordinator sent a message"))
    return out


def instruction_files(records: list) -> list:
    """Every file an `instructions` attachment carries: [{path, type, content}]."""
    out = []
    for att in attachments(records):
        if att.get("type") == "instructions":
            for f in att.get("files") or []:
                if isinstance(f, dict) and isinstance(f.get("path"), str):
                    out.append({"path": f["path"], "type": f.get("type"),
                                "content": f.get("content") if isinstance(f.get("content"), str)
                                else _text_of(f.get("content")),
                                "index": att.get("_index")})
    return out


def file_attachments(records: list) -> list:
    """`nested_memory` and `edited_text_file` attachments: [{kind, path, content, index}]."""
    out = []
    for att in attachments(records):
        t = att.get("type")
        if t == "nested_memory":
            out.append({"kind": t, "path": att.get("path"), "content": _text_of(att.get("content")),
                        "index": att.get("_index")})
        elif t == "edited_text_file":
            out.append({"kind": t, "path": att.get("filename"), "content": _text_of(att.get("snippet")),
                        "index": att.get("_index")})
    return out


def scratchpad_dirs(records: list) -> list:
    """The `scratchpadDirectory` of every `environment` attachment: the folder Claude Code tells a
    subagent to use for its scratch files (measured on the probe agent ae0f0dca2662b9b37, record 2:
    <tmp>/claude-<uid>/<project>/<session id>/scratchpad). Returned as written; the judge accepts
    one only when it is the driving session's own."""
    out = []
    for att in attachments(records):
        if att.get("type") == "environment":
            snap = att.get("snapshot") if isinstance(att.get("snapshot"), dict) else {}
            got = snap.get("scratchpadDirectory")
            if isinstance(got, str) and got and got not in out:
                out.append(got)
    return out


def attachment_kinds(records: list) -> dict:
    """The count of each attachment kind the transcript holds."""
    counts: dict = {}
    for att in attachments(records):
        k = str(att.get("type"))
        counts[k] = counts.get(k, 0) + 1
    return dict(sorted(counts.items()))


def session_ids(records: list) -> set:
    return {r.get("sessionId") for r in records if r.get("sessionId")}


def agent_ids(records: list) -> set:
    return {r.get("agentId") for r in records if r.get("agentId")}
