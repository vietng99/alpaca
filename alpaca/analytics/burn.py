"""Token and cost board for the Sessions tab (t-143).

Reads the project's own transcripts (each session's main conversation and every subagent that
alpaca.analytics.children can place under it) and folds each model response into:

- 15-minute lanes per session (tokens per quarter hour, an idle rail from first to last request,
  API retry and refusal markers, limit bands when a refusal names when the limit resets);
- how hard each session pushed (tokens per active minute: a minute with at least one request)
  beside what it cost (estimated API list price from Alpaca's rate cards);
- each session's main-thread context per turn (input + cache_read + cache_creation of a
  response), with compactions found as drops;
- the subagents of the session that spent most on subagents: context by turn (median and middle
  half), what every agent together spent per turn, and a what-if for a limit on turns per agent,
  split by model;
- a table of the heaviest sessions with a breakdown into main thread, workflow subagents,
  ordinary subagents, models and token kinds, and how each workflow run fanned out
  (alpaca.analytics.fanout).

A token here is input + output + cache_read + cache_creation. Nothing
here writes; parsed files are kept in memory keyed by their size and modification time, and a
file that only grew is read from where the last read stopped.
"""
from __future__ import annotations

import glob
import json
import math
import os
import re
import stat
import threading
import time
from datetime import datetime, timezone

from alpaca import db, transcripts
from alpaca.analytics import children as family
from alpaca.analytics import fanout, insights, metrics, pricing, ratecards

WINDOWS = {"24h": 86400, "7d": 7 * 86400, "all": None}
MAX_RANGE_S = 3653 * 86400   # ten years at most (3653 days with three leap days); a longer range is refused
BUCKETS = (900, 1800, 3600, 7200, 10800, 21600, 43200, 86400)
MAX_BUCKETS = 400
TOP = 12
IDLE_TIME = 300          # longest time one response is credited with (seconds)
RUNAWAY_BUCKET = 900
FAN_LANES = 6
MAX_CURVE_POINTS = 360
MAX_TURNS = 300
CAP_CANDIDATES = (10, 25, 50, 100, 150, 200)
DROP_RATIO = 0.6            # a turn whose context is below 60% of the one before ...
DROP_MIN = 20000            # ... and at least 20k tokens smaller is a compaction
MAX_LINE = 64 * 1024 * 1024
_MARKERS = (b'"usage"', b"token_usage_record", b"api_error", b"isApiErrorMessage", b"turn_context",
            b"thread_settings_applied", b"compact", b'"is_error":true', b'"role":"user","content":"',
            b'"attachment":{"type":"hook_', b"stop_hook_summary", b'"function_call"', b'"custom_tool_call"')
TEXT = 400                  # characters kept from a failed tool result or a hook message

_FILES = {}
_LOCK = threading.RLock()


# ---- reading one transcript ------------------------------------------------------------------

def _epoch(value):
    if isinstance(value, (int, float)):
        return float(value) / (1000.0 if value > 1e11 else 1.0)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _int(value):
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else 0


def _fresh_state():
    # calls: tool calls in order; errors: failed tool results by call id; prompts: owner prompts;
    # hooks: hook runs and hook messages. `line` is the transcript line each one came from.
    return {"offset": 0, "size": 0, "mtime": 0, "responses": [], "index": {}, "requests": {},
            "events": [], "compactions": [], "model": None, "cards": None, "line": 0,
            "calls": [], "errors": {}, "prompts": [], "hooks": []}


def _text(value):
    """Plain text of a tool result or message content (a string or a list of text blocks)."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(str(b.get("text") or "") for b in value if isinstance(b, dict))
    return ""


def _call(name, args, ts, response, line, ident=""):
    """One tool call with the fields the insights read: a normalized key, a file it writes, a command."""
    args = args if isinstance(args, dict) else {}
    path = args.get("file_path") or args.get("notebook_path") or args.get("path")
    command = args.get("command") if isinstance(args.get("command"), str) else None
    if command is None and isinstance(args.get("cmd"), str):
        command = args["cmd"]
    if command is not None:
        key = "%s: %s" % (name, " ".join(command.split())[:240])
    elif name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        key = "%s: %s" % (name, path)
    elif name == "Read":
        key = "Read: %s %s %s" % (path, args.get("offset") or "", args.get("limit") or "")
    else:
        key = "%s: %s" % (name, json.dumps(args, sort_keys=True, default=str)[:200])
    return {"id": str(ident), "name": str(name)[:60], "key": key, "ts": ts, "r": response, "line": line,
            "path": str(path) if isinstance(path, str) and name in ("Edit", "Write", "MultiEdit", "NotebookEdit") else None,
            "cmd": " ".join(command.split())[:400] if command else None}


def _claude(rec, st):
    message = rec.get("message") if isinstance(rec.get("message"), dict) else {}
    ts = _epoch(rec.get("timestamp"))
    if rec.get("isApiErrorMessage") is True:
        quota = rec.get("quotaLimits") if isinstance(rec.get("quotaLimits"), dict) else {}
        status = rec.get("apiErrorStatus") or rec.get("error") or "error"
        st["events"].append({"ts": ts, "kind": "refusal", "status": str(status)[:40],
                             "limit": str(quota.get("rateLimitType") or "")[:40] if quota.get("status") == "rejected" else "",
                             "until": _epoch(quota.get("resetsAt")) if quota.get("status") == "rejected" else None})
        return
    usage = message.get("usage")
    if not isinstance(usage, dict) or message.get("model") == "<synthetic>" or ts is None:
        return
    ident = str(message.get("id") or rec.get("requestId") or rec.get("uuid") or "")
    request = rec.get("requestId")
    if isinstance(request, str) and ident:
        ident = st["requests"].setdefault(request, ident)
    creation = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    row = {"ts": ts, "model": str(message.get("model") or "unknown")[:80], "provider": "anthropic",
           "in": _int(usage.get("input_tokens")), "out": _int(usage.get("output_tokens")),
           "cr": _int(usage.get("cache_read_input_tokens")), "cw": _int(usage.get("cache_creation_input_tokens")),
           "cw5": creation.get("ephemeral_5m_input_tokens"), "cw1": creation.get("ephemeral_1h_input_tokens"),
           "side": rec.get("isSidechain") is True, "cost": None}
    at = st["index"].get(ident) if ident else None
    if at is None:
        if ident:
            st["index"][ident] = len(st["responses"])
        st["responses"].append(row)
        _tool_uses(message, ts, len(st["responses"]) - 1, st)
        return
    _tool_uses(message, ts, at, st)
    old = st["responses"][at]
    # Streamed blocks of one response repeat its counters; keep the largest, never the sum.
    for key in ("in", "out", "cr", "cw"):
        old[key] = max(old[key], row[key])
    for key in ("cw5", "cw1"):
        if isinstance(row[key], int) and (not isinstance(old[key], int) or row[key] > old[key]):
            old[key] = row[key]
    old["cost"] = None


def _tool_uses(message, ts, response, st):
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            st["calls"].append(_call(block.get("name") or "tool", block.get("input"), ts, response,
                                     st["line"], block.get("id") or ""))


def _user(rec, st):
    """A failed tool result (kept by call id) or a prompt the owner typed."""
    message = rec.get("message") if isinstance(rec.get("message"), dict) else {}
    ts = _epoch(rec.get("timestamp"))
    content = message.get("content")
    if isinstance(content, str):
        if not rec.get("isMeta") and not rec.get("isSidechain") and not content.lstrip().startswith("<") and ts:
            st["prompts"].append(ts)
        return
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error") is True:
            st["errors"][str(block.get("tool_use_id") or "")] = {"ts": ts, "line": st["line"],
                                                                "text": _text(block.get("content"))[:TEXT]}


def _attachment(rec, st):
    """A hook run (with its duration) or a hook message, from Claude Code's attachments and stop summaries."""
    ts = _epoch(rec.get("timestamp"))
    if rec.get("type") == "system" and rec.get("subtype") == "stop_hook_summary":
        for info in rec.get("hookInfos") or []:
            if isinstance(info, dict):
                st["hooks"].append({"ts": ts, "kind": "run", "event": "Stop", "cmd": str(info.get("command") or "")[:300],
                                    "ms": _ms(info.get("durationMs")), "text": "", "line": st["line"]})
        for err in rec.get("hookErrors") or []:
            st["hooks"].append({"ts": ts, "kind": "error", "event": "Stop", "cmd": "", "ms": None,
                                "text": str(err)[:TEXT], "line": st["line"]})
        return
    a = rec.get("attachment") if isinstance(rec.get("attachment"), dict) else {}
    kind = {"hook_success": "run", "hook_non_blocking_error": "error", "hook_blocking_error": "block",
            "hook_cancelled": "cancelled"}.get(a.get("type"))
    if not kind:
        return
    blocking = a.get("blockingError") if isinstance(a.get("blockingError"), dict) else {}
    text = blocking.get("blockingError") or a.get("stderr") or ""
    st["hooks"].append({"ts": ts, "kind": kind, "event": str(a.get("hookEvent") or "")[:40],
                        "name": str(a.get("hookName") or "")[:60],
                        "cmd": str(a.get("command") or blocking.get("command") or "")[:300],
                        "ms": _ms(a.get("durationMs")), "text": str(text)[:TEXT], "line": st["line"]})


def _ms(value):
    try:
        return float(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


_CODEX_CMD = re.compile(r'(?:cmd|command)\s*:\s*"((?:[^"\\]|\\.)*)"')
_PATCH_FILE = re.compile(r"\*\*\* (?:Update|Add|Delete) File: (\S+)")


def _codex_call(rec, st):
    """A Codex tool call: shell commands and apply_patch targets, attributed to the latest response."""
    payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
    ts = _epoch(rec.get("timestamp"))
    response = len(st["responses"])
    name = str(payload.get("name") or payload.get("type") or "tool")
    body = payload.get("arguments") if payload.get("type") == "function_call" else payload.get("input")
    body = body if isinstance(body, str) else json.dumps(body or "")
    for path in _PATCH_FILE.findall(body):
        st["calls"].append(dict(_call("Edit", {"file_path": path}, ts, response, st["line"], payload.get("call_id") or ""), name="apply_patch"))
    commands = []
    try:
        args = json.loads(body)
        if isinstance(args, dict) and isinstance(args.get("cmd") or args.get("command"), (str, list)):
            value = args.get("cmd") or args.get("command")
            commands.append(" ".join(value) if isinstance(value, list) else value)
    except ValueError:
        commands.extend(m.encode().decode("unicode_escape", "ignore") for m in _CODEX_CMD.findall(body))
    for command in commands:
        st["calls"].append(_call("Bash", {"command": command}, ts, response, st["line"], payload.get("call_id") or ""))


def _codex(rec, st):
    payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
    typ = rec.get("type")
    if typ == "turn_context" and payload.get("model"):
        st["model"] = str(payload["model"])[:80]
        return
    if typ == "event_msg" and payload.get("type") == "thread_settings_applied":
        model = (payload.get("thread_settings") or {}).get("model") if isinstance(payload.get("thread_settings"), dict) else None
        if model:
            st["model"] = str(model)[:80]
        return
    if typ == "token_usage_record" or payload.get("type") == "token_usage_record":
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
        ts = _epoch(rec.get("timestamp"))
        if not usage or ts is None:
            return
        ident = "codex:" + str(payload.get("response_id") or len(st["responses"]))
        if ident in st["index"]:
            return
        total_in, cached, write = _int(usage.get("input_tokens")), _int(usage.get("cached_input_tokens")), _int(usage.get("cache_write_input_tokens"))
        st["index"][ident] = len(st["responses"])
        st["responses"].append({"ts": ts, "model": str(payload.get("model") or st["model"] or "unknown")[:80],
                                "provider": "openai", "in": max(0, total_in - cached - write),
                                "out": _int(usage.get("output_tokens")), "cr": cached, "cw": write,
                                "cw5": None, "cw1": None, "side": False, "cost": None})
        return
    if typ == "compacted":
        st["compactions"].append(_epoch(rec.get("timestamp")))


def _feed(line, st):
    if not any(marker in line for marker in _MARKERS):
        return
    try:
        rec = json.loads(line)
    except ValueError:
        return
    if not isinstance(rec, dict):
        return
    typ = rec.get("type")
    if typ == "assistant":
        _claude(rec, st)
    elif typ == "user":
        _user(rec, st)
    elif typ == "attachment" or (typ == "system" and rec.get("subtype") == "stop_hook_summary") \
            or isinstance(rec.get("attachment"), dict):
        _attachment(rec, st)
    elif typ == "response_item" and isinstance(rec.get("payload"), dict) \
            and rec["payload"].get("type") in ("function_call", "custom_tool_call"):
        _codex_call(rec, st)
    elif typ == "system" and rec.get("subtype") == "api_error":
        err = rec.get("error") if isinstance(rec.get("error"), dict) else {}
        conn = err.get("connection") if isinstance(err.get("connection"), dict) else {}
        status = err.get("status") or conn.get("code") or ("overloaded" if "overload" in json.dumps(err).lower() else "error")
        st["events"].append({"ts": _epoch(rec.get("timestamp")), "kind": "retry", "status": str(status)[:40],
                             "attempt": _int(rec.get("retryAttempt")), "limit": "", "until": None})
    elif typ == "system" and rec.get("subtype") == "compact_boundary":
        st["compactions"].append(_epoch(rec.get("timestamp")))
    elif typ in ("turn_context", "event_msg", "token_usage_record", "compacted"):
        _codex(rec, st)


def _price(row, cards):
    if row["provider"] == "anthropic":
        raw = {"input_tokens": row["in"], "output_tokens": row["out"],
               "cache_read_input_tokens": row["cr"], "cache_creation_input_tokens": row["cw"]}
        if isinstance(row["cw5"], int) and isinstance(row["cw1"], int):
            raw["cache_creation"] = {"ephemeral_5m_input_tokens": row["cw5"], "ephemeral_1h_input_tokens": row["cw1"]}
    else:
        raw = {"input_tokens": row["in"] + row["cr"] + row["cw"], "cached_input_tokens": row["cr"],
               "cache_write_input_tokens": row["cw"], "output_tokens": row["out"]}
    usage = metrics._usage(raw, row["provider"])
    try:
        value = pricing.estimate(row["model"], usage or {}, provider=row["provider"], cards=cards)
    except Exception:
        return None, None
    parts = value.get("breakdown_usd") or {}
    writes = [v for k, v in parts.items() if k.startswith("cache_write") and isinstance(v, (int, float))]
    return value.get("total_usd"), (sum(writes) if writes else None)


def read(path, cards=None, cards_rev=""):
    """{responses, events, compactions} for one transcript, parsed at most once per change."""
    real = os.path.realpath(path)
    st_now = os.stat(real)
    with _LOCK:
        st = _FILES.get(real)
        if st is None or st_now.st_size < st["size"]:
            st = _fresh_state()
        if st_now.st_size != st["size"] or st_now.st_mtime_ns != st["mtime"]:
            with open(real, "rb") as fh:
                fh.seek(st["offset"])
                while True:
                    line = fh.readline(MAX_LINE)
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        if len(line) >= MAX_LINE:
                            # An oversized line is skipped whole: read to its end and move on.
                            while line and not line.endswith(b"\n"):
                                line = fh.readline(MAX_LINE)
                            st["offset"] = fh.tell()
                            st["line"] += 1
                            continue
                        break  # a line still being written: read it next time
                    st["offset"] = fh.tell()
                    st["line"] += 1
                    _feed(line, st)
            st["size"], st["mtime"] = st_now.st_size, st_now.st_mtime_ns
        if st["cards"] != cards_rev:
            for row in st["responses"]:
                row["cost"] = None
            st["cards"] = cards_rev
        for row in st["responses"]:
            if row["cost"] is None:
                row["cost"], row["cwc"] = _price(row, cards) if cards is not None else (None, None)
                if row["cost"] is None:
                    row["cost"] = -1.0  # priced and found unpriceable; never retried for this card set
        _FILES[real] = st
        return st


def _snapshot(st, rows):
    """What one board iterates for a transcript: the parse state as of now, taken under _LOCK.

    `rows` are the board's own copies of the responses, so a later read() that re-prices or grows
    the shared state (another window, a new rate card) cannot change a board that is being built.
    The lists are copied; the call, error and hook dicts are never changed once parsed."""
    return dict(st, responses=rows, calls=list(st["calls"]), errors=dict(st["errors"]), prompts=list(st["prompts"]),
                hooks=list(st["hooks"]), events=list(st["events"]), compactions=list(st["compactions"]), live=st)


def summary(path, cards=None, cards_rev=""):
    """Totals for one transcript: the fallback the index uses for a child metrics cannot open."""
    st = read(path, cards, cards_rev)
    rows = st["responses"]
    costs = [row["cost"] for row in rows if row["cost"] is not None and row["cost"] >= 0]
    return {"responses": len(rows), "costed_responses": len(costs),
            "total_tokens": sum(_tokens(row) for row in rows) if rows else None,
            "estimated_cost_usd": sum(costs) if costs else None}


def _tokens(row):
    return row["in"] + row["out"] + row["cr"] + row["cw"]


def _context(row):
    return row["in"] + row["cr"] + row["cw"]


# ---- helpers -----------------------------------------------------------------------------------

def person(path, operator=None):
    """The account a transcript belongs to: /srv/claude-accounts/<person>/..., /home/<person>/...,
    else the session's operator."""
    text = str(path or "").replace("\\", "/")
    for pattern in (r"/srv/claude-accounts/([^/]+)/", r"^/home/([^/]+)/", r"^/Users/([^/]+)/"):
        match = re.search(pattern, text)
        if match and not match.group(1).startswith("_"):
            return match.group(1)
    return operator or "unknown"


def family_of(model):
    name = str(model or "").lower()
    for key in ("opus", "sonnet", "haiku"):
        if key in name:
            return key
    if name.startswith(("gpt", "o1", "o3", "o4", "codex")) or "gpt" in name:
        return "gpt"
    return "other"


def _quantile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    at = (len(ordered) - 1) * q
    lo, hi = math.floor(at), math.ceil(at)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (at - lo)


def _nice(value):
    if value <= 0:
        return 0
    power = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 2.5, 5, 10):
        if value <= step * power:
            return step * power
    return 10 * power


def bucket_size(span_s):
    for size in BUCKETS:
        if span_s / size <= MAX_BUCKETS:
            return size
    return BUCKETS[-1]


def compactions(points):
    """Indexes where the context dropped: below DROP_RATIO of the turn before and DROP_MIN smaller."""
    return [i for i in range(1, len(points))
            if points[i] < points[i - 1] * DROP_RATIO and points[i - 1] - points[i] >= DROP_MIN]


def downsample(points, limit=MAX_CURVE_POINTS):
    """At most `limit` points that keep every peak and every trough (min and max per chunk, in order)."""
    if len(points) <= limit:
        return list(points)
    chunk = math.ceil(len(points) / (limit / 2))
    out = []
    for start in range(0, len(points), chunk):
        part = points[start:start + chunk]
        lo, hi = part.index(min(part)), part.index(max(part))
        out.extend(part[i] for i in sorted({lo, hi}))
    return out


def growth(agents, max_turns=MAX_TURNS):
    """Per turn number t: median and middle half of the context agents carried on turn t, how many
    agents got that far, and what every agent together spent on turn t.

    `agents` is a list of per-agent response lists in time order."""
    if not agents:
        return []
    count = len(agents)
    floor = max(1, math.ceil(0.05 * count)) if count >= 20 else 1
    out = []
    for t in range(1, max_turns + 1):
        rows = [agent[t - 1] for agent in agents if len(agent) >= t]
        if len(rows) < floor:
            break
        ctx = [_context(row) for row in rows]
        out.append({"t": t, "n": len(rows), "med": round(_quantile(ctx, .5)), "q1": round(_quantile(ctx, .25)),
                    "q3": round(_quantile(ctx, .75)), "cost": sum(_tokens(row) for row in rows)})
    return out


def turn_caps(agents, families, candidates=CAP_CANDIDATES):
    """What a limit on turns per agent would have spent: per cap, tokens kept, tokens saved,
    agents cut short, and the kept tokens split by model family."""
    if not agents:
        return []
    longest = max(len(agent) for agent in agents)
    caps = [cap for cap in candidates if cap < longest]
    if not caps and longest > 1:
        caps = sorted({max(1, longest // 4), max(1, longest // 2)} - {longest})
    total = sum(_tokens(row) for agent in agents for row in agent)
    out = []
    for cap in caps:
        split = {}
        for agent, fam in zip(agents, families):
            split[fam] = split.get(fam, 0) + sum(_tokens(row) for row in agent[:cap])
        kept = sum(split.values())
        out.append({"cap": cap, "tokens": kept, "save": total - kept,
                    "cut": sum(len(agent) > cap for agent in agents), "split": split})
    return out


# ---- the board -----------------------------------------------------------------------------

def _what(conn, sid, title):
    try:
        refs = [row[0] for row in conn.execute(
            "SELECT ref FROM events WHERE session=? AND kind='task-move' AND ref LIKE 't-%' "
            "GROUP BY ref ORDER BY MIN(id) LIMIT 3", (sid,))]
        named = []
        for ref in refs:
            row = conn.execute("SELECT title, statement FROM tasks WHERE id=?", (ref,)).fetchone()
            label = (row[0] or row[1] or "") if row else ""
            named.append("%s %s" % (ref, label[:70]) if label else ref)
    except Exception:
        named = []
    return "; ".join(named) if named else (title or "")


_INJECTED = re.compile(r"^\s*(Base directory for this skill|<[a-z-]+|Caveat:|\[Request interrupted|This session is being continued)", re.I)


def _title(title, what, sid):
    """A readable name: the session title, unless it is text a tool injected as the first prompt,
    in which case the first task the session moved, else its short identifier."""
    if title and not _INJECTED.match(title) and title != sid:
        return title
    first = (what or "").split("; ")[0]
    return first or "Session %s" % sid[:8]


def _session_sources(root, conn):
    from alpaca.analytics import build_index
    return build_index._transcripts(root, conn)


def _record(conn):
    """The record rows the insights read: task events, tasks with their contracts, gate refusals, questions."""
    events, tasks, contracts, gates, questions = [], {}, {}, [], []
    for row in conn.execute("SELECT id, ts, session, kind, ref, op, data FROM events WHERE kind IN "
                            "('claim','task-move','lease-expired','task-contract','phase-door-closed','question') "
                            "ORDER BY id"):
        try:
            data = json.loads(row[6]) if row[6] else {}
        except ValueError:
            data = {}
        item = {"id": row[0], "ts": _epoch(row[1]), "session": row[2], "kind": row[3], "ref": row[4], "op": row[5],
                "data": data if isinstance(data, dict) else {}}
        if item["ts"] is None:
            continue
        if row[3] == "task-contract":
            contracts.setdefault(row[4], []).append(json.dumps(data))
        elif row[3] == "phase-door-closed":
            gates.append(item)
        elif row[3] == "question":
            questions.append(item)
        else:
            events.append(item)
    for row in conn.execute("SELECT id, op, status, title, statement, where_, why FROM tasks"):
        tasks[row[0]] = {"id": row[0], "op": row[1], "status": row[2], "title": row[3], "statement": row[4],
                         "where_": row[5], "why": row[6]}
    return events, tasks, contracts, gates, questions


def _pool_failures(root):
    """{session: [hook failure]} from the hook pool (.alpaca/pool/hooks/<session>.jsonl)."""
    out = {}
    for path in sorted(glob.glob(os.path.join(root, ".alpaca", "pool", "hooks", "*.jsonl"))):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and rec.get("type") == "hook_outcome" and rec.get("outcome") == "failed":
                        out.setdefault(str(rec.get("session") or ""), []).append(
                            {"ts": _epoch(rec.get("ts")), "hook": str(rec.get("hook") or "")[:60],
                             "error": str(rec.get("error_type") or "")[:60], "ms": rec.get("duration_ms")})
        except OSError:
            continue
    return out


def _meta_hooks(conn):
    """{session: {hook: {runs, ms}}} as Alpaca recorded them in meta (hook-cost:<session>:<hook>)."""
    out = {}
    for key, value in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'hook-cost:%'"):
        parts = key.split(":")
        if len(parts) < 3:
            continue
        try:
            v = json.loads(value)
            out.setdefault(parts[1], {})[parts[2]] = {"runs": int(v.get("runs") or 0), "ms": float(v.get("total_ms") or 0)}
        except (ValueError, TypeError, AttributeError):
            continue
    return out


def _touched(st, realroot):
    """({response: files its calls touched}, {response: commands}) for one parsed transcript.

    Resolving every call's paths is the cost of the board, and a transcript that has not moved
    resolves to the same sets, so they are kept on its parsed state under the fingerprint read()
    parsed it at (size, mtime): an append changes it and only that transcript is resolved again.
    The sets are only read by the caller. The paths are resolved outside _LOCK (st is a board's
    own snapshot) and only the store is under it."""
    key = (st["size"], st["mtime"], realroot)
    keep = st.get("live", st)              # the memo lives on the shared parse state, not on a board's snapshot
    with _LOCK:
        memo = keep.get("touched")
    if memo is not None and memo[0] == key:
        return memo[1], memo[2]
    targets = {}
    commands = {}
    for c in st["calls"]:
        t = insights.targets(c, realroot, resolved=True)
        if t:
            targets.setdefault(c["r"], set()).update(t)
        if c.get("cmd"):
            commands.setdefault(c["r"], []).append(c["cmd"])
    with _LOCK:
        keep["touched"] = (key, targets, commands)
    return targets, commands


def _link_stamp(root, conn, sid):
    """What fanout.child_links reads, as a cheap comparable value: the state of every directory
    under the session's subagent directories and of the directory above each (lstat, so a swap
    for a symlink shows: its mtime and whether it is a link; an added, removed or replaced child
    moves its directory's mtime) and each registered child source with whether its file is there.
    None when it cannot be read, which is never cached."""
    from alpaca.analytics import detail
    try:
        registered = transcripts.registered(conn, sid)
        if registered and not os.path.isabs(registered):
            registered = os.path.join(root, registered)
        top = [transcripts.subagents_dir(root, sid)]
        if registered:
            top.insert(0, os.path.join(os.path.dirname(registered), os.path.splitext(os.path.basename(registered))[0], "subagents"))
        stamp = [registered]

        def look(path):
            try:
                st = os.lstat(path)
                return (path, st.st_mtime_ns, stat.S_ISLNK(st.st_mode), st.st_ino)
            except OSError:
                return (path, None, None, None)
        for directory in top:
            stamp.append(look(os.path.dirname(directory)))
            stack = [directory]
            while stack:
                here = stack.pop()
                stamp.append(look(here))
                try:
                    with os.scandir(here) as entries:
                        stack += [e.path for e in entries if e.is_dir(follow_symlinks=False)]
                except OSError:
                    pass
        try:
            for row in detail._obs_sources(conn, root, sid, parent_only=True):
                locator = row["locator"]
                if not os.path.isabs(locator):
                    locator = os.path.join(root, locator)      # as detail._children reads it
                local = transcripts.local_path(root, row["session"])
                stamp.append((row["source_id"], locator, os.path.isfile(locator), os.path.islink(locator),
                              os.path.isfile(local), os.path.islink(local)))
        except Exception as exc:
            if "no such table" not in str(exc):
                return None
        return tuple(stamp)
    except Exception:
        return None


_LINKS = {}
LINKS_KEEP = 256


def _child_links(root, conn, sid):
    """fanout.child_links, kept per session under _link_stamp: listing a session's children
    resolves every path under it, which a session that has not moved need not repeat."""
    stamp = _link_stamp(root, conn, sid)
    with _LOCK:
        hit = _LINKS.get((root, sid))
    if stamp is not None and hit is not None and hit[0] == stamp:
        return dict(hit[1])
    try:
        links = fanout.child_links(root, conn, sid, strict=True)
    except Exception:
        return {}                          # served as no children, but not kept: the next board reads it again
    if stamp is not None:
        with _LOCK:
            _LINKS.pop((root, sid), None)
            _LINKS[(root, sid)] = (stamp, dict(links))
            while len(_LINKS) > LINKS_KEEP:
                _LINKS.pop(next(iter(_LINKS)))
    return links


_SPEND = {}
SPEND_KEEP = 1024     # sessions whose cycle shares are kept
_ROWS = {}            # sid -> (fingerprint, spend rows) of the sessions read last, for the signals
ROWS_KEEP = 48


def _cycle_spend(root, loaded, cycle_rows, held, rows_of, active):
    """[(cost, tokens, sessions)] per cycle: what each session holding its task spent while it did.

    It reads every response of every session and does not depend on the window, so each session's
    share of a cycle is kept under that session's parse fingerprint (size, mtime and rate cards of
    each of its transcripts) and the holds it had: a session that has not moved is not read again
    for it, and one that did move is read once for all its cycles."""
    spend = []
    rows = {}
    for cycle in cycle_rows:
        ident = (cycle["task"], cycle["start"], cycle["done"])
        cost = tokens = 0.0
        sessions = set()
        for item in loaded:
            sid = item["s"]["sid"]
            mine = held.get(sid, ())
            if not any(task == cycle["task"] and a < cycle["done"] and b > cycle["start"] for task, a, b in mine):
                continue
            stamp = (root, tuple((thread, st["size"], st["mtime"], st["cards"]) for thread, st in item["threads"]), tuple(mine))
            with _LOCK:
                entry = _SPEND.pop(sid, None)
                if entry is None or entry[0] != stamp:
                    entry = (stamp, {})
                _SPEND[sid] = entry
                while len(_SPEND) > SPEND_KEEP:
                    _SPEND.pop(next(iter(_SPEND)))
                share = entry[1].get(ident)
            if share is None:
                if sid not in rows:
                    rows[sid] = rows_of(item)
                c = t = 0.0
                hit = False
                for r in rows[sid]:
                    if cycle["start"] <= r["ts"] <= cycle["done"]:
                        now_held = active(sid, r["ts"])
                        if cycle["task"] in now_held:
                            c += r["cost"] / len(now_held)
                            t += r["tokens"] / len(now_held)
                            hit = True
                share = (c, t, hit)
                with _LOCK:
                    entry[1][ident] = share
            if share[2]:
                cost += share[0]
                tokens += share[1]
                sessions.add(sid)
        spend.append((cost, tokens, sorted(sessions)))
    return spend


def _signals(root, conn, loaded, top, start, end, size, now):
    """The nine insights (alpaca.analytics.insights) for the sessions on the board, plus the
    project-wide lists: cost per outcome, collisions and the slowest hooks."""
    inside = lambda ts: ts is not None and start <= ts < end
    events, tasks, contracts, gates, questions = _record(conn)
    holds = insights.holds(events)
    held = {}
    for task, sid, a, b in holds:
        held.setdefault(sid, []).append((task, a, b))
    paths = {tid: insights.task_paths(t, contracts.get(tid, ())) for tid, t in tasks.items()}
    pool = _pool_failures(root)
    meta = _meta_hooks(conn)
    realroot = os.path.realpath(root)

    # Every response of a session, with the files its tool calls touched, for spend attribution.
    def rows_of(item):
        sid = item["s"]["sid"]
        stamp = (realroot, tuple((thread, st["size"], st["mtime"], st["cards"]) for thread, st in item["threads"]))
        with _LOCK:
            hit = _ROWS.get(sid)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        rows = []
        for thread, st in item["threads"]:
            targets, commands = _touched(st, realroot)
            resp = st["responses"]
            for i, r in enumerate(resp):
                nxt = resp[i + 1]["ts"] if i + 1 < len(resp) else r["ts"]
                rows.append({"ts": r["ts"], "tokens": _tokens(r), "cost": r["cost"] if r["cost"] is not None and r["cost"] >= 0 else 0.0,
                             "time": max(0.0, min(nxt - r["ts"], IDLE_TIME)), "targets": targets.get(i, set()),
                             "cmd": " ".join(commands.get(i, [])), "thread": thread})
        with _LOCK:
            _ROWS.pop(sid, None)
            _ROWS[sid] = (stamp, rows)
            while len(_ROWS) > ROWS_KEEP:
                _ROWS.pop(next(iter(_ROWS)))
        return rows

    def active(sid, ts):
        return [task for task, a, b in held.get(sid, ()) if a <= ts < b]

    # t-148 cost per outcome, and the reopened cycles t-147 counts as waste.
    cycle_rows = insights.cycles(events)
    task_cost = {}
    for cycle, (cost, tokens, sessions) in zip(cycle_rows, _cycle_spend(realroot, loaded, cycle_rows, held, rows_of, active)):
        cycle.update(cost=cost, tokens=tokens, sessions=list(sessions))
        entry = task_cost.setdefault(cycle["task"], {"cost": 0.0, "tokens": 0.0, "sessions": set(), "cycles": 0})
        entry["cost"] += cost
        entry["tokens"] += tokens
        entry["sessions"].update(sessions)
        entry["cycles"] += 1
    ops = {}
    for tid, t in tasks.items():
        if t["status"] != "done":
            continue
        c = task_cost.get(tid, {"cost": 0.0, "tokens": 0.0, "sessions": set(), "cycles": 0})
        ops.setdefault(t["op"] or "no operation", []).append(
            {"task": tid, "title": (t["title"] or t["statement"] or "")[:90], "cost": c["cost"], "tokens": c["tokens"],
             "sessions": sorted(c["sessions"]), "cycles": c["cycles"], "captured": bool(c["sessions"])})
    outcome_ops = []
    for op, rows in sorted(ops.items()):
        captured = [r["cost"] for r in rows if r["captured"]]
        med = insights._median(captured)
        for r in rows:
            r["outlier"] = bool(med and len(captured) >= 3 and r["cost"] > insights.OUTLIER * med)
            r["ratio"] = r["cost"] / med if med else None
        rows.sort(key=lambda r: -r["cost"])
        outcome_ops.append({"op": op, "done": len(rows), "captured": len(captured), "cost": sum(captured),
                            "median": med, "tasks": rows})
    outcome_ops.sort(key=lambda o: -o["cost"])

    # t-151 collisions, across every session, subagents counted toward their parent.
    edits = []
    for item in loaded:
        for thread, st in item["threads"]:
            for c in st["calls"]:
                if c["name"] in insights.EDIT_TOOLS and c.get("path") and inside(c["ts"]):
                    edits.append((item["s"]["sid"], c["ts"], os.path.normpath(c["path"])))
    collided = insights.collisions(edits)

    slow = {}
    by_sid = {item["s"]["sid"]: item for item in loaded}
    for x in top:
        item = by_sid[x["sid"]]
        sid = x["sid"]
        sig = {}
        # t-146 loops, per conversation, over the calls in the window.
        flags = []
        for thread, st in item["threads"]:
            calls = [c for c in st["calls"] if inside(c["ts"])]
            for f in insights.loops(calls, st["errors"]):
                flags.append(dict(f, thread=thread))
        sig["loops"] = flags[:12]
        # t-147 wasted spend.
        waste = []
        for run in x.get("runs") or []:
            killed = run["status"] in ("killed", "failed", "error", "cancelled", "canceled")
            for a in run["agents"]:
                cost, tok = a.get("cost"), a.get("tokens")
                if a["state"] in fanout.FAILED:
                    waste.append({"cause": "failed agent", "run": run["name"], "agent": a["label"] or a["id"], "cost": cost, "tokens": tok})
                elif killed and a.get("unfinished"):
                    waste.append({"cause": "killed run, agent unfinished", "run": run["name"], "agent": a["label"] or a["id"], "cost": cost, "tokens": tok})
                if a["attempt"] > 1 and cost is not None:
                    share = (a["attempt"] - 1) / a["attempt"]
                    waste.append({"cause": "retried agent, %d extra attempt%s" % (a["attempt"] - 1, "" if a["attempt"] == 2 else "s"),
                                  "run": run["name"], "agent": a["label"] or a["id"], "cost": cost * share,
                                  "tokens": (tok or 0) * share})
        for cycle in cycle_rows:
            if cycle["reopened_at"] and sid in cycle.get("sessions", ()) and inside(cycle["reopened_at"]):
                waste.append({"cause": "reopened task", "run": None, "agent": cycle["task"], "cost": cycle["cost"],
                              "tokens": cycle["tokens"]})
        runs_waste = {}
        for w in waste:
            if w["run"]:
                runs_waste[w["run"]] = runs_waste.get(w["run"], 0.0) + (w["cost"] or 0)
        sig["waste"] = {"cost": sum(w["cost"] or 0 for w in waste), "items": sorted(waste, key=lambda w: -(w["cost"] or 0))[:40],
                        "runs": runs_waste, "count": len(waste)}
        # t-145 claimed, off-task and unclaimed spend.
        mine_rows = rows_of(item)
        split = {k: {"tokens": 0.0, "cost": 0.0, "time": 0.0} for k in ("claimed", "off", "unclaimed")}
        files, seen_tasks, pathless = {}, set(), set()
        for r in mine_rows:
            if not inside(r["ts"]):
                continue
            now_held = active(sid, r["ts"])
            seen_tasks.update(now_held)
            if not now_held:
                kind = "unclaimed"
            else:
                task_paths = set().union(*(paths.get(t, set()) for t in now_held))
                pathless.update(t for t in now_held if not paths.get(t))
                verdicts = [insights.matches(t, task_paths, now_held, r["cmd"]) for t in r["targets"]]
                decided = [v for v in verdicts if v is not None]
                kind = "off" if decided and not any(decided) and task_paths else "claimed"
                if kind == "off":
                    for t, v in zip(r["targets"], verdicts):
                        if v is False:
                            f = files.setdefault(t, {"path": t, "tokens": 0.0, "cost": 0.0, "responses": 0})
                            f["tokens"] += r["tokens"] / len(r["targets"])
                            f["cost"] += r["cost"] / len(r["targets"])
                            f["responses"] += 1
            split[kind]["tokens"] += r["tokens"]
            split[kind]["cost"] += r["cost"]
            split[kind]["time"] += r["time"]
        sig["offtask"] = dict(split, files=sorted(files.values(), key=lambda f: -f["cost"])[:15],
                              tasks=sorted(seen_tasks), pathless=sorted(pathless))
        # t-149 idle, cache re-writes after idle, owner wait.
        stamps = [r["ts"] for r in mine_rows if inside(r["ts"])]
        idle_s, gaps = insights.idle(stamps)
        rewrites = []
        for thread, st in item["threads"]:
            rewrites += [dict(w, thread=thread) for w in insights.cache_rewrites([r for r in st["responses"] if inside(r["ts"])])]
        main_st = next((st for thread, st in item["threads"] if thread is None), None)
        wait_s, waits = insights.owner_wait([r["ts"] for r in main_st["responses"]] if main_st else [],
                                            [p for p in (main_st["prompts"] if main_st else []) if inside(p)])
        asked = [q for q in questions if q["session"] == sid and inside(q["ts"])]
        sig["idle"] = {"idle_s": idle_s, "gaps": gaps, "rewrites": len(rewrites),
                       "rewrite_tokens": sum(w["tokens"] for w in rewrites),
                       "rewrite_cost": sum(w["cost"] or 0 for w in rewrites),
                       "top_rewrites": sorted(rewrites, key=lambda w: -(w["cost"] or 0))[:5],
                       "owner_wait_s": wait_s, "owner_waits": waits,
                       "questions": [{"id": q["ref"], "ts": q["ts"], "status": q["data"].get("status"),
                                      "text": str(q["data"].get("text") or "")[:160]} for q in asked]}
        # t-150 runaway: the session's own 15-minute buckets over everything captured.
        bins = {}
        agent_min = {}
        for thread, st in item["threads"]:
            for r in st["responses"]:
                b = int(r["ts"] // RUNAWAY_BUCKET)
                bins[b] = bins.get(b, 0) + _tokens(r)
                if thread:
                    a = agent_min.setdefault(thread, [0, set()])
                    a[0] += _tokens(r)
                    a[1].add(int(r["ts"] // 60))
        flagged = [dict(f, start=f["bucket"] * RUNAWAY_BUCKET) for f in insights.runaway(bins)
                   if inside(f["bucket"] * RUNAWAY_BUCKET)]
        for f in flagged:
            f["cell"] = int((f["start"] - start) // size)
        agents_hot = insights.runaway_agents({a: v[0] / max(1, len(v[1])) for a, v in agent_min.items()})
        sig["runaway"] = {"bins": flagged, "agents": agents_hot[:8],
                          "median": insights._median([v for v in bins.values() if v > 0])}
        # t-151 collisions involving this session.
        sig["collisions"] = [c for c in collided if sid in (c["a"], c["b"])][:12]
        # t-152 friction.
        items = []
        for thread, st in item["threads"]:
            errs = {k: e for k, e in st["errors"].items() if inside(e["ts"])}
            items += insights.friction(errs, [h for h in st["hooks"] if inside(h["ts"])], thread)
        items += [{"kind": "phase_gate", "label": "Refused phase gate", "ts": g["ts"], "line": None, "thread": None,
                   "event": g["id"], "text": "%s: %s" % (g["ref"], g["data"].get("reason") or "")}
                  for g in gates if g["session"] == sid and inside(g["ts"])]
        counts = {}
        for f in items:
            counts[f["kind"]] = counts.get(f["kind"], 0) + 1
        sig["friction"] = {"counts": counts, "items": sorted(items, key=lambda f: f["ts"] or 0)[:30]}
        # t-153 hook overhead: measured hook runs in the transcripts, Alpaca's own record, the pool.
        runs = [h for thread, st in item["threads"] for h in st["hooks"] if inside(h["ts"])]
        per_hook = insights.hooks(runs)
        failures = [f for f in pool.get(sid, ()) if inside(f["ts"])]
        for f in failures:
            row = per_hook.setdefault(f["hook"] or "alpaca", {"runs": 0, "ms": 0.0, "max_ms": 0.0, "failures": 0})
            row["failures"] += 1
        for name, row in per_hook.items():
            agg = slow.setdefault(name, {"hook": name, "runs": 0, "ms": 0.0, "max_ms": 0.0, "failures": 0, "sessions": 0})
            agg["runs"] += row["runs"]
            agg["ms"] += row["ms"]
            agg["max_ms"] = max(agg["max_ms"], row["max_ms"])
            agg["failures"] += row["failures"]
            agg["sessions"] += 1
        sig["hooks"] = {"per_hook": per_hook, "ms": sum(r["ms"] for r in per_hook.values()),
                        "failures": sum(r["failures"] for r in per_hook.values()),
                        "pool_failures": failures[:10], "recorded": meta.get(sid, {})}
        # t-148 the tasks this session worked on, and what each cost overall.
        mine = [c for c in cycle_rows if sid in c.get("sessions", ())]
        sig["outcomes"] = [{"task": c["task"], "cost": c["cost"], "tokens": c["tokens"], "done": c["done"],
                            "reopened": bool(c["reopened_at"])} for c in mine][-12:]
        x["signals"] = sig
    return {"outcomes": outcome_ops, "collisions": collided[:40],
            "hooks": sorted(slow.values(), key=lambda h: -h["ms"])[:12],
            "rules": insights.__doc__}


def _instant(value, name):
    """An epoch (seconds or milliseconds) or an ISO time with its offset; a naive ISO time is refused
    because the browser picked the day in its own zone and the server must not guess it."""
    if isinstance(value, str):
        value = value.strip()
        try:
            value = float(value)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("%s is not an epoch or an ISO time with an offset" % name) from None
            if parsed.tzinfo is None:
                raise ValueError("%s needs a time zone offset" % name)
            return parsed.timestamp()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("%s is not an epoch or an ISO time with an offset" % name)
    return float(value) / (1000.0 if value > 1e11 else 1.0)


def custom_range(start, end, now):
    """(start, end) of a custom range in epoch seconds, or ValueError. The end is cut to `now`: the
    hours after now hold no request, so a range that includes today ends at the present."""
    if start in (None, "") or end in (None, ""):
        raise ValueError("a custom range needs both start and end")
    start, end = _instant(start, "start"), _instant(end, "end")
    if start >= end:
        raise ValueError("the range starts after it ends; pick a start before the end")
    if start >= now:
        raise ValueError("the range starts in the future; pick a start before now")
    if end - start > MAX_RANGE_S:
        raise ValueError("the range is longer than ten years; pick a shorter range")
    return start, min(end, now)


def board(root, sessions, window="24h", now=None, families=None, start=None, end=None):
    """The whole board for one time window. `sessions` are the fold's listed work sessions.

    With `start` and `end` (an epoch or an ISO time with offset) the board covers that range
    instead and `window` is reported as "range": the grid starts exactly at `start` (the browser
    sends its local midnight) in the smallest bucket that keeps the span within MAX_BUCKETS.
    """
    now = float(now if now is not None else time.time())
    custom = None
    if start is not None or end is not None:
        custom = custom_range(start, end, now)
        window = "range"
    elif window not in WINDOWS:
        raise ValueError("unknown window; use one of %s" % ", ".join(WINDOWS))
    conn = db.connect_readonly(root)
    try:
        cards = ratecards.load(conn)
        cards_rev = ratecards.revision(root, conn=conn)
        families = families if families is not None else family.discover(root, conn)
        sources = _session_sources(root, conn)
        read_errors = 0
        loaded = []
        for s in sessions:
            if s.get("class", "work") != "work":
                continue
            sid = s["sid"]
            path = sources.get(sid)
            main = []
            events, explicit = [], []
            threads = []
            if path and os.path.isfile(path):
                try:
                    with _LOCK:
                        st = read(path, cards, cards_rev)
                        main = [dict(row, agent=None, kind="main", wf=None) for row in st["responses"]]
                        threads.append((None, _snapshot(st, main)))
                        events += st["events"]
                        explicit += st["compactions"]
                except OSError:
                    read_errors += 1
            kids = []
            kinds = {}
            unreadable = 0
            for cid, entry in sorted((families.get(sid) or {}).items()):
                if not entry.get("path"):
                    unreadable += 1
                    continue
                kind = entry.get("kind") or "subagent"
                try:
                    with _LOCK:
                        st = read(entry["path"], cards, cards_rev)
                        kid = [dict(row, agent=cid, kind=kind, wf=entry.get("workflow")) for row in st["responses"]]
                        snap = _snapshot(st, kid)
                        events += [dict(ev, agent=cid) for ev in st["events"]]
                except OSError:
                    read_errors += 1
                    continue
                kinds[cid] = kind
                threads.append((cid, snap))
                kids.append(kid)
            for row in main:
                if row["side"]:
                    row["kind"] = "subagent"
            loaded.append({"s": s, "path": path, "main": main, "kids": kids, "events": events, "kinds": kinds,
                           "threads": threads,
                           "explicit": explicit, "unreadable": unreadable,
                           "known_children": len(families.get(sid) or {})})
        stamps = [row["ts"] for item in loaded for row in item["main"]] + \
                 [row["ts"] for item in loaded for kid in item["kids"] for row in kid]
        span = WINDOWS.get(window)
        if custom:
            start, end = custom
            size = bucket_size(max(end - start, 900))
        elif span is None:
            first = min(stamps) if stamps else now - 86400
            size = bucket_size(max(now - first, 900))
            start = math.floor(first / size) * size
        else:
            size = bucket_size(span)
            start = math.floor((now - span) / size) * size
        if not custom:
            end = math.floor(now / size) * size + size
        nb = max(1, int(math.ceil((end - start) / size - 1e-9)))
        inside = lambda ts: ts is not None and start <= ts < end

        out_sessions = []
        grand_tokens = grand_cost = grand_requests = 0
        grand_costed = grand_unpriced = 0
        agents_total = 0
        people = set()
        global_events = {}
        bands = []
        for item in loaded:
            s = item["s"]
            rows = [r for r in item["main"] if inside(r["ts"])]
            kid_rows = [[r for r in kid if inside(r["ts"])] for kid in item["kids"]]
            all_rows = rows + [r for kid in kid_rows for r in kid]
            if not all_rows:
                continue
            all_rows.sort(key=lambda r: r["ts"])
            tokens = sum(_tokens(r) for r in all_rows)
            priced = [r["cost"] for r in all_rows if r["cost"] is not None and r["cost"] >= 0]
            cost = sum(priced)
            minutes = {int(r["ts"] // 60) for r in all_rows}
            bins = {}
            for r in all_rows:
                b = int((r["ts"] - start) // size)
                bins[b] = bins.get(b, 0) + _tokens(r)
            ev = {}
            for e in item["events"]:
                if not inside(e.get("ts")):
                    continue
                b = int((e["ts"] - start) // size)
                slot = ev.setdefault(b, {"retries": 0, "refusals": 0, "statuses": {}})
                slot["retries" if e["kind"] == "retry" else "refusals"] += 1
                slot["statuses"][e["status"]] = slot["statuses"].get(e["status"], 0) + 1
                g = global_events.setdefault(b, {"retries": 0, "refusals": 0, "statuses": {}})
                g["retries" if e["kind"] == "retry" else "refusals"] += 1
                g["statuses"][e["status"]] = g["statuses"].get(e["status"], 0) + 1
                if e["kind"] == "refusal" and e.get("until") and e["until"] > e["ts"]:
                    bands.append([e["ts"], min(e["until"], end), e.get("limit") or "limit"])
            split = {}
            for r in all_rows:
                part = split.setdefault(r["kind"], {"tokens": 0, "cost": 0.0, "requests": 0, "agents": set()})
                part["tokens"] += _tokens(r)
                part["cost"] += r["cost"] if r["cost"] is not None and r["cost"] >= 0 else 0
                part["requests"] += 1
                if r["agent"]:
                    part["agents"].add(r["agent"])
            models = {}
            for r in all_rows:
                m = models.setdefault(r["model"], {"tokens": 0, "cost": 0.0, "requests": 0})
                m["tokens"] += _tokens(r)
                m["cost"] += r["cost"] if r["cost"] is not None and r["cost"] >= 0 else 0
                m["requests"] += 1
            fams = {}
            for r in all_rows:
                f = fams.setdefault(family_of(r["model"]), {"tokens": 0, "cost": 0.0})
                f["tokens"] += _tokens(r)
                f["cost"] += r["cost"] if r["cost"] is not None and r["cost"] >= 0 else 0
            main_turns = [r for r in rows if r["kind"] == "main"]
            points = [_context(r) for r in main_turns]
            drops = compactions(points)
            agents_active = {r["agent"] for r in all_rows if r["agent"]}
            runs = {r["wf"] for r in all_rows if r["wf"]}
            who = person(item["path"], s.get("operator"))
            people.add(who)
            what = _what(conn, s["sid"], s.get("title"))
            out_sessions.append({
                "sid": s["sid"], "title": _title(s.get("title"), what, s["sid"]), "what": what,
                "person": who, "operator": s.get("operator"),
                "first": all_rows[0]["ts"], "last": all_rows[-1]["ts"],
                "span_s": all_rows[-1]["ts"] - all_rows[0]["ts"],
                "requests": len(all_rows), "active_minutes": len(minutes),
                "rate": tokens / len(minutes) if minutes else 0,
                "tokens": tokens, "cost_usd": cost, "unpriced": len(all_rows) - len(priced),
                "bins": {str(k): v for k, v in sorted(bins.items())}, "events": {str(k): v for k, v in sorted(ev.items())},
                "subagents": len(agents_active), "subagents_known": item["known_children"],
                "subagents_unreadable": item["unreadable"], "workflow_runs": len(runs),
                "split": {k: {"tokens": v["tokens"], "cost": v["cost"], "requests": v["requests"], "agents": len(v["agents"])}
                          for k, v in split.items()},
                "models": sorted(({"model": k, **v} for k, v in models.items()), key=lambda m: -m["tokens"]),
                "families": fams,
                "kinds": {"input": sum(r["in"] for r in all_rows), "output": sum(r["out"] for r in all_rows),
                          "cache_read": sum(r["cr"] for r in all_rows), "cache_write": sum(r["cw"] for r in all_rows)},
                "peak_bucket": max(bins.values()) if bins else 0,
                "peak_bucket_at": max(bins, key=bins.get) if bins else None,
                "errors": sum(v["retries"] + v["refusals"] for v in ev.values()),
                "curve": {"points": downsample(points), "turns": len(points), "peak": max(points) if points else 0,
                          "compactions": len(drops),
                          "explicit_compactions": sum(1 for ts in item["explicit"] if inside(ts))},
                "_kids": [kid for kid in kid_rows if kid], "_kid_meta": item["kids"]})
            grand_tokens += tokens
            grand_cost += cost
            grand_requests += len(all_rows)
            grand_costed += len(priced)
            grand_unpriced += len(all_rows) - len(priced)
            agents_total += len(agents_active)
        out_sessions.sort(key=lambda x: (-x["tokens"], x["sid"]))
        for x in out_sessions:
            x["share"] = 100.0 * x["tokens"] / grand_tokens if grand_tokens else 0
        top = out_sessions[:TOP]

        # The subagent section: the session that spent most on subagents in the window.
        crew = None
        heavy = max(out_sessions, key=lambda x: sum(v["tokens"] for k, v in x["split"].items() if k != "main"), default=None)
        if heavy and any(heavy["_kids"]):
            agents = [kid for kid in heavy["_kids"] if kid]
            agent_fams = [family_of(max(set(r["model"] for r in kid), key=lambda m: sum(_tokens(r) for r in kid if r["model"] == m)))
                          for kid in agents]
            g = growth(agents)
            turns = [len(kid) for kid in agents]
            total = sum(_tokens(r) for kid in agents for r in kid)
            cr = sum(r["cr"] for kid in agents for r in kid)
            outp = sum(r["out"] for kid in agents for r in kid)
            probe = min(100, g[-1]["t"]) if g else None
            at = next((row for row in g if row["t"] == probe), None)
            by_model = []
            for fam in sorted(set(agent_fams)):
                mine = [kid for kid, f in zip(agents, agent_fams) if f == fam]
                spent = [sum(_tokens(r) for r in kid) for kid in mine]
                by_model.append({"family": fam, "agents": len(mine),
                                 "median_turns": _quantile([len(k) for k in mine], .5),
                                 "median_tokens": _quantile(spent, .5), "tokens": sum(spent)})
            crew = {"sid": heavy["sid"], "title": heavy["title"], "agents": len(agents),
                    "median_turns": _quantile(turns, .5), "max_turns": max(turns),
                    "ctx1": g[0]["med"] if g else None, "ctx_probe": at["med"] if at else None, "probe_turn": probe,
                    "reread_pct": 100.0 * cr / total if total else 0, "read_per_written": cr / outp if outp else None,
                    "tokens": total, "growth": g, "caps": turn_caps(agents, agent_fams), "by_model": by_model,
                    "workflow_runs": heavy["workflow_runs"]}
        # How each workflow run fanned out, and the plain subagents, for the sessions on the board.
        by_sid = {item["s"]["sid"]: item for item in loaded}
        fan = []
        for x in top:
            item = by_sid[x["sid"]]
            usage = {}
            for kid in item["kids"]:
                if kid:
                    priced = [r["cost"] for r in kid if r["cost"] is not None and r["cost"] >= 0]
                    usage[kid[0]["agent"]] = {"tokens": sum(_tokens(r) for r in kid), "responses": len(kid),
                                              "cost": sum(priced) if priced else None}
            links = _child_links(root, conn, x["sid"])
            x["child_links"] = links
            runs = []
            for name, path in sorted(fanout.run_files(root, conn, x["sid"]).items()):
                try:
                    record = fanout.load(path)
                except OSError:
                    record = None
                if not record:
                    continue
                run = fanout.summarize(record, usage, links, now)
                if run["start"] is not None and (run["end"] or now) >= start and run["start"] < end:
                    runs.append(run)
            runs.sort(key=lambda r: (r["start"], r["run"]))
            x["runs"] = runs
            meta = fanout.metas(root, conn, x["sid"])
            plain = []
            for kid in item["kids"]:
                if not kid or item["kinds"].get(kid[0]["agent"]) == "workflow":
                    continue
                inside_rows = [r for r in kid if inside(r["ts"])]
                if not inside_rows:
                    continue
                ident = kid[0]["agent"]
                info = meta.get(ident) or {}
                models = {}
                for r in kid:
                    models[r["model"]] = models.get(r["model"], 0) + _tokens(r)
                priced = [r["cost"] for r in kid if r["cost"] is not None and r["cost"] >= 0]
                plain.append({"id": ident, "s": kid[0]["ts"], "e": kid[-1]["ts"], "responses": len(kid),
                              "tokens": sum(_tokens(r) for r in kid), "cost": sum(priced) if priced else None,
                              "model": max(models, key=models.get), "fam": family_of(max(models, key=models.get)),
                              "type": str(info.get("agentType") or "")[:60],
                              "label": str(info.get("description") or "")[:120], "child": links.get(ident)})
            plain.sort(key=lambda p: (p["s"], p["id"]))
            slots, rows = fanout.pack([(p["s"], max(p["e"], p["s"] + size * 0.3)) for p in plain])
            for p, slot in zip(plain, slots):
                p["slot"] = slot
            x["plain"] = {"agents": plain, "rows": rows}
            for i, run in enumerate(runs):
                fan.append({"sid": x["sid"], "run": i, "tokens": run["figures"]["tokens"] or 0})
        fan.sort(key=lambda f: -f["tokens"])
        fan = sorted(fan[:FAN_LANES], key=lambda f: (by_sid[f["sid"]]["s"].get("first") or "", f["sid"], f["run"]))
        signals = _signals(root, conn, loaded, top, start, end, size, now)
        for x in out_sessions:
            x.pop("_kids", None)
            x.pop("_kid_meta", None)
        rest = out_sessions[TOP:]
        nonzero = sorted(v for x in top for v in x["bins"].values() if v > 0)
        edges = [_nice(_quantile(nonzero, q)) for q in (.2, .4, .6, .8)] if nonzero else [1, 2, 3, 4]
        for i in range(1, 4):
            if edges[i] <= edges[i - 1]:
                edges[i] = _nice(edges[i - 1] * 1.01) if edges[i - 1] else 1
        merged = []
        for band in sorted(bands):
            if merged and band[0] <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], band[1])
            else:
                merged.append(list(band))
        heaviest = out_sessions[0] if out_sessions else None
        stamps_in = [t for t in stamps if inside(t)]
        return {
            "schema": 1, "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "window": {"key": window, "start": start, "end": end, "now": now, "bucket_s": size, "buckets": nb},
            "totals": {"tokens": grand_tokens, "cost_usd": grand_cost, "requests": grand_requests,
                       "costed_requests": grand_costed, "unpriced_requests": grand_unpriced,
                       "sessions": len(out_sessions), "people": sorted(people), "subagents": agents_total,
                       "subagent_sessions": sum(1 for x in out_sessions if x["subagents"]),
                       "first": min(stamps_in) if stamps_in else None, "last": max(stamps_in) if stamps_in else None,
                       "top": {"sid": heaviest["sid"], "title": heaviest["title"], "tokens": heaviest["tokens"],
                               "share": heaviest["share"]} if heaviest else None},
            "heat": {"edges": edges},
            "events": {"buckets": {str(k): v for k, v in sorted(global_events.items())},
                       "retries": sum(v["retries"] for v in global_events.values()),
                       "refusals": sum(v["refusals"] for v in global_events.values()),
                       "bands": [{"start": a, "end": b, "label": c} for a, b, c in merged],
                       "note": "Markers come from the transcripts: API retries (system api_error records) and "
                               "refused requests (API error messages, with the limit and its reset when recorded). "
                               "Stalled agents are not recorded in these transcripts, so none are shown."},
            "cost": {"basis": "api_estimate",
                     "note": "Estimated API list-price cost of every request from Alpaca's rate cards, "
                             "subagents included. It is an estimate, not a bill."},
            "sessions": top,
            "rest": {"sessions": len(rest), "tokens": sum(x["tokens"] for x in rest),
                     "cost_usd": sum(x["cost_usd"] for x in rest), "requests": sum(x["requests"] for x in rest)},
            "crew": crew,
            "outcomes": signals["outcomes"],
            "collisions": signals["collisions"],
            "hooks": signals["hooks"],
            "rules": signals["rules"],
            "fanout": fan,
            "source": {"sessions_read": len(loaded), "read_errors": read_errors,
                       "children_unreadable": sum(item["unreadable"] for item in loaded),
                       "token_definition": "input + output + cache_read + cache_creation",
                       "context_definition": "input + cache_read + cache_creation of one response",
                       "compaction_rule": "a turn below %d%% of the turn before and at least %d tokens smaller"
                                          % (int(DROP_RATIO * 100), DROP_MIN)},
        }
    finally:
        conn.close()
