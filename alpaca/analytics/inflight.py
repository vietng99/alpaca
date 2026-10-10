"""In-flight tasks for the hub's In-flight tab (t-157): what each claimed task is doing right now.

An in-flight task is a task in status doing whose lease is still live (tasks.claimant, lease_until).
For each one this reads the record (claim, task-move, task-contract, proof-scaffold, proof-report
events, the operation's task plan) and the claiming session's own transcripts from the claim on:
the session's registered transcript, its subagents and its workflow runs, the way the Sessions
board reads them (alpaca.analytics.burn, children, fanout, insights). No model is called: every
line on a card is either a recorded value or a fixed rule applied to recorded values.

The live narrative comes from a tail reader. Each transcript keeps a byte offset; a poll parses only
the lines written since the last one, and the first read starts TAIL_BYTES from the end. Only the
assistant's own text, tool names, descriptions, paths, exit codes and test counts are kept; tool
output bodies and file contents never leave this module.

Progress is never invented:
- measured, when a workflow run the session started after the claim is still running: agents that
  finished (done, failed or killed) out of the agents the run planned, overall and per phase;
- estimated, otherwise: time since the claim over the median claim-to-done time of SIMILAR or more
  done tasks in the same operation and workstream (else the same operation), capped at CAP percent;
- none, when neither basis exists.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import threading
import time
from datetime import datetime

from alpaca import db, transcripts
from alpaca.analytics import burn, children, fanout, insights, ratecards

TAIL_BYTES = 4 * 1024 * 1024
MAX_EVENTS = 4000
FEED = 60
LIVE_S = 3600               # a workflow folder whose agents wrote within this long counts as a live run
STATUS_LINES = 8
TEXT = 240
RECENT_CALLS = 20
SIMILAR = 2
CAP = 95.0
READ_TOOLS = {"Read", "Grep", "Glob", "WebFetch", "WebSearch", "LS", "NotebookRead"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
AGENT_TOOLS = {"Agent", "Task"}
FINISHED = {"done", "error", "failed", "killed", "cancelled", "canceled"}
EXIT = re.compile(r"^\s*(?:Error: )?Exit code (\d+)", re.I)
PASSED = re.compile(r"(\d+) passed")
FAILED = re.compile(r"(\d+) failed")
ERRORS = re.compile(r"(\d+) errors?\b")

_TAILS = {}
_LOCK = threading.RLock()


def _epoch(value):
    return burn._epoch(value)


def _trim(text, limit=TEXT):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


# ---- the tail reader -----------------------------------------------------------------------

def _content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def parse_line(line):
    """The narrative events in one transcript line: assistant text, tool calls, tool results (counts
    and exit codes only), typed prompts and turn ends."""
    try:
        rec = json.loads(line)
    except ValueError:
        return []
    if not isinstance(rec, dict):
        return []
    ts = _epoch(rec.get("timestamp"))
    typ = rec.get("type")
    out = []
    if typ == "assistant":
        message = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        if message.get("model") == "<synthetic>":
            return []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and str(block.get("text") or "").strip():
                out.append({"t": "text", "ts": ts, "text": _trim(block["text"])})
            elif block.get("type") == "tool_use":
                args = block.get("input") if isinstance(block.get("input"), dict) else {}
                command = args.get("command") if isinstance(args.get("command"), str) else ""
                out.append({"t": "call", "ts": ts, "id": str(block.get("id") or ""), "name": str(block.get("name") or "tool")[:60],
                            "cwd": str(rec.get("cwd") or "")[:300],
                            "desc": _trim(args.get("description") or args.get("prompt_summary") or "", 160),
                            "path": str(args.get("file_path") or args.get("notebook_path") or args.get("path") or "")[:300],
                            "pattern": _trim(args.get("pattern") or "", 80),
                            "cmd": _trim(command.splitlines()[0] if command else "", 120),
                            "to": _trim(args.get("to") or args.get("recipient") or "", 60),
                            "agent": _trim(args.get("subagent_type") or "", 40),
                            "workflow": _trim(args.get("name") or args.get("workflow") or args.get("workflowName")
                                              or os.path.splitext(os.path.basename(str(args.get("scriptPath") or "")))[0], 80)})
    elif typ == "user":
        message = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        content = message.get("content")
        if isinstance(content, str):
            if not rec.get("isMeta") and not content.lstrip().startswith("<") and content.strip():
                out.append({"t": "prompt", "ts": ts, "text": _trim(content)})
            return out
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                text = _content_text(block.get("content"))
                code = EXIT.match(text)
                passed, failed, errors = PASSED.findall(text), FAILED.findall(text), ERRORS.findall(text)
                launched = rec.get("toolUseResult") if isinstance(rec.get("toolUseResult"), dict) else {}
                out.append({"t": "result", "ts": ts, "id": str(block.get("tool_use_id") or ""),
                            "error": block.get("is_error") is True,
                            "exit": int(code.group(1)) if code else (None if block.get("is_error") else 0),
                            "passed": int(passed[-1]) if passed else None, "failed": int(failed[-1]) if failed else None,
                            "errors": int(errors[-1]) if errors else None,
                            "run": _trim(launched.get("runId") or "", 60) or None,
                            "workflow": _trim(launched.get("workflowName") or "", 80) or None})
    elif typ == "system" and rec.get("subtype") in ("stop_hook_summary", "turn_duration"):
        out.append({"t": "stop", "ts": ts})
    return out


def _parse_at(line, offset):
    """parse_line, with each event carrying the byte offset of its line ("show earlier" reads before it)."""
    events = parse_line(line)
    for e in events:
        e["off"] = offset
    return events


def _parse_span(path, start, end):
    """Events between two byte offsets; a partial first line is skipped unless start is 0."""
    events = []
    with open(path, "rb") as fh:
        fh.seek(start)
        if start:
            fh.readline()
        while fh.tell() < end:
            at = fh.tell()
            line = fh.readline()
            if not line or not line.endswith(b"\n"):
                break
            events.extend(_parse_at(line, at))
    return events


def tail(path):
    """{events, start, end}: the tail of one transcript, read incrementally across polls."""
    real = os.path.realpath(path)
    size = os.path.getsize(real)
    with _LOCK:
        st = _TAILS.get(real)
        if st is None or size < st["end"]:
            start = max(0, size - TAIL_BYTES)
            st = {"start": start, "end": start, "events": [], "skip": bool(start)}
        if size > st["end"]:
            with open(real, "rb") as fh:
                fh.seek(st["end"])
                if st["skip"]:
                    partial = fh.readline()         # the first read starts mid-line: drop that line
                    if partial.endswith(b"\n"):
                        st["skip"] = False
                        st["end"] = fh.tell()
                while not st["skip"]:
                    at = fh.tell()
                    line = fh.readline()
                    if not line or not line.endswith(b"\n"):
                        break                      # a line still being written: next poll
                    st["events"].extend(_parse_at(line, at))
                    st["end"] = fh.tell()
            if len(st["events"]) > MAX_EVENTS:
                del st["events"][:len(st["events"]) - MAX_EVENTS]
        _TAILS[real] = st
        return st


def earlier(path, before, chunk=TAIL_BYTES):
    """Events from the chunk of the file just before byte offset `before`, and the new start."""
    start = max(0, before - chunk)
    return _parse_span(path, start, before), start


# ---- rules over the narrative ----------------------------------------------------------------

def _rel(path, root, cwd=""):
    """A path relative to the session's working directory or the project root, when under either."""
    path = str(path or "")
    for base in sorted({b.rstrip("/") for b in (cwd, root) if b}, key=len, reverse=True):
        if path.startswith(base + "/"):
            return path[len(base) + 1:]
    return path


def is_test(command):
    return bool(command and insights.TEST_RE.search(command))


def feed(events, root=None, limit=FEED):
    """Activity lines by rule, newest first: [{ts, kind, text, ok}]."""
    results = {e["id"]: e for e in events if e["t"] == "result"}
    lines, reads = [], []

    def flush():
        if not reads:
            return
        if len(reads) == 1:
            r = reads[0]
            lines.append({"ts": r["ts"], "kind": "read", "text": "Read %s" % (_rel(r["path"], root, r.get("cwd")) or r["pattern"] or r["name"]), "ok": None,
                          "off": r.get("off")})
        else:
            dirs = [os.path.dirname(_rel(r["path"], root, r.get("cwd"))) for r in reads if r["path"]]
            common = os.path.commonpath(dirs) if dirs and all(dirs) else ""
            lines.append({"ts": reads[-1]["ts"], "kind": "read",
                          "text": "Read %d files%s" % (len(reads), " in %s" % common if common else ""), "ok": None,
                          "off": reads[0].get("off")})
        reads.clear()

    for e in events:
        if e["t"] != "call":
            continue
        name, res = e["name"], results.get(e["id"])
        if name in READ_TOOLS:
            reads.append(e)
            continue
        flush()
        ok = None if res is None else not res["error"]
        if name == "Bash":
            text = e["desc"] or e["cmd"] or "Ran a command"
            if res is not None:
                status = "exit %s" % (res["exit"] if res["exit"] is not None else ("1" if res["error"] else "0"))
                if res["passed"] is not None or res["failed"] is not None:
                    status += ", %s passed, %s failed" % (res["passed"] or 0, res["failed"] or 0)
                text += " (%s)" % status
            kind = "test" if is_test(e["cmd"]) else "run"
        elif name in EDIT_TOOLS:
            text, kind = ("Wrote %s" if name == "Write" else "Edited %s") % _rel(e["path"], root, e.get("cwd")), "edit"
        elif name in AGENT_TOOLS:
            text, kind = "Started agent: %s" % (e["desc"] or e["agent"] or "subagent"), "agent"
            if res is not None:
                lines.append({"ts": e["ts"], "kind": kind, "text": text, "ok": None, "off": e.get("off")})
                text, ok = ("Agent finished: %s" if not res["error"] else "Agent failed: %s") % (e["desc"] or e["agent"] or "subagent"), not res["error"]
                lines.append({"ts": res["ts"], "kind": kind, "text": text, "ok": ok, "off": e.get("off")})
                continue
        elif name == "Workflow":
            text, kind = "Started workflow %s" % ((res or {}).get("workflow") or e["workflow"] or e["desc"] or ""), "workflow"
        elif name == "SendMessage":
            text, kind = "Messaged %s" % (e["to"] or "an agent"), "message"
        elif name == "AskUserQuestion":
            text, kind = "Asked the owner", "ask"
        else:
            text, kind = e["desc"] or name, "tool"
        lines.append({"ts": e["ts"], "kind": kind, "text": text.strip(), "ok": ok, "off": e.get("off")})
    flush()
    lines.sort(key=lambda x: x["ts"] or 0, reverse=True)
    return lines[:limit] if limit else lines


def mode(events, workflow_running=False):
    """What the session is doing now, from its last RECENT_CALLS tool calls and its last event."""
    calls = [e for e in events if e["t"] == "call"][-RECENT_CALLS:]
    answered = {e["id"] for e in events if e["t"] == "result"}
    last = next((e for e in reversed(events) if e["t"] in ("call", "stop", "prompt", "text")), None)
    counts = {"exploring": 0, "editing": 0, "testing": 0, "delegating": 0, "other": 0}
    for c in calls:
        if c["name"] in READ_TOOLS or (c["name"] == "Bash" and re.match(r"(cat|ls|grep|rg|find|head|tail|sed -n|wc|git (log|diff|status|show))\b", c["cmd"])):
            counts["exploring"] += 1
        elif c["name"] in EDIT_TOOLS:
            counts["editing"] += 1
        elif c["name"] == "Bash" and is_test(c["cmd"]):
            counts["testing"] += 1
        elif c["name"] in AGENT_TOOLS or c["name"] == "Workflow":
            counts["delegating"] += 1
        else:
            counts["other"] += 1
    asking = calls and calls[-1]["name"] == "AskUserQuestion" and calls[-1]["id"] not in answered
    if asking or (last is not None and last["t"] == "stop"):
        now = "waiting on the owner"
    elif workflow_running:
        now = "running a workflow"
    elif not calls:
        now = "no tool calls yet"
    else:
        order = ("testing", "editing", "exploring", "delegating", "other")
        now = max(order, key=lambda k: (counts[k], -order.index(k)))
        now = {"other": "running commands"}.get(now, now)
    return {"mode": now, "counts": counts, "window": len(calls)}


def focus(events, root=None, top=3):
    """The folders touched most since the claim: [(folder, count)]."""
    found = {}
    for e in events:
        if e["t"] == "call" and e["path"]:
            folder = os.path.dirname(_rel(e["path"], root, e.get("cwd"))) or "."
            found[folder] = found.get(folder, 0) + 1
    return sorted(found.items(), key=lambda kv: (-kv[1], kv[0]))[:top]


def test_trend(events):
    """Test runs in order with their counts, and whether they converge."""
    results = {e["id"]: e for e in events if e["t"] == "result"}
    runs = []
    for e in events:
        if e["t"] == "call" and e["name"] == "Bash" and is_test(e["cmd"]) and e["id"] in results:
            r = results[e["id"]]
            failed = (r["failed"] or 0) + (r["errors"] or 0)
            ok = not r["error"] and failed == 0
            runs.append({"ts": e["ts"], "passed": r["passed"], "failed": failed, "ok": ok})
    if not runs:
        verdict = "no test runs"
    elif runs[-1]["ok"]:
        verdict = "passing"
    else:
        fails = [r["failed"] for r in runs[-3:] if not r["ok"]]
        verdict = "converging" if len(fails) >= 2 and fails[-1] < fails[0] else "failing"
    return {"runs": runs[-12:], "verdict": verdict, "total": len(runs)}


# ---- progress --------------------------------------------------------------------------------

def measured(runs):
    """Agents finished out of agents planned across active workflow runs, overall and per phase."""
    total = done = 0
    phases = []
    for run in runs:
        planned = max(run.get("agent_count") or 0, len(run["agents"]))
        finished = sum(a["state"] in FINISHED for a in run["agents"])
        total += planned
        done += finished
        for p in run["phases"]:
            mine = [a for a in run["agents"] if a["phase"] == p["index"]]
            phases.append({"run": run["name"], "phase": p["title"], "done": sum(a["state"] in FINISHED for a in mine),
                           "total": len(mine), "failed": sum(a["state"] in fanout.FAILED for a in mine)})
    if not total:
        return None
    return {"basis": "measured", "percent": 100.0 * done / total, "done": done, "total": total, "phases": phases,
            "label": "measured: %d of %d workflow agents finished" % (done, total)}


def estimate(elapsed, durations, scope):
    """Elapsed over the median duration of similar done tasks, capped while not done."""
    durations = [d for d in durations if d and d > 0]
    if len(durations) < SIMILAR:
        return None
    med = statistics.median(durations)
    return {"basis": "estimate", "percent": min(CAP, 100.0 * elapsed / med), "median_s": med, "n": len(durations),
            "label": "estimate from %d similar tasks (%s), median %s" % (len(durations), scope, _duration(med))}


def progress(elapsed, active_runs, same_stream, same_op):
    found = measured(active_runs) if active_runs else None
    if found:
        return found
    found = estimate(elapsed, same_stream, "same workstream") or estimate(elapsed, same_op, "same operation")
    if found:
        return found
    return {"basis": "none", "percent": None, "label": "no basis for a percentage"}


def _duration(seconds):
    seconds = int(seconds or 0)
    h, m = divmod(seconds // 60, 60)
    return "%d h %02d m" % (h, m) if h else "%d m" % m


# ---- the record ------------------------------------------------------------------------------

def _live(task, now):
    if task["status"] != "doing" or not task.get("claimant"):
        return False
    try:
        return datetime.fromisoformat(task.get("lease_until") or "").timestamp() > now
    except (TypeError, ValueError):
        return False


def cycle(history):
    """(claim time, doing since) for the current doing cycle from a task's history rows."""
    claim = doing = None
    for e in reversed(history):
        kind, to = e["kind"], e.get("to")
        ts = _epoch(e["at"])
        if kind == "claim" or (kind == "task-move" and to == "doing"):
            if kind == "claim" and claim is None:
                claim = ts
            doing = ts
        elif kind == "task-move" and to and to != "doing":
            break
    return claim or doing, doing or claim


def durations(tasks):
    """{task id: seconds from its last cycle's first claim or move to doing to its move to done}."""
    out = {}
    for t in tasks:
        if t["status"] != "done":
            continue
        start = end = None
        for e in t.get("history") or []:
            ts = _epoch(e["at"])
            if e["kind"] == "claim" or (e["kind"] == "task-move" and e.get("to") == "doing"):
                start = start if start is not None else ts
            elif e["kind"] == "task-move" and e.get("to") == "done":
                end = ts
                if start is not None and end and end > start:
                    out[t["id"]] = end - start
                start = None
    return out


def _milestones(conn, task_id, claim_ts):
    rows = conn.execute("SELECT kind, ts, data FROM events WHERE ref=? AND kind IN "
                        "('claim','task-contract','proof-scaffold','proof-report','task-move') ORDER BY id", (task_id,)).fetchall()
    marks = {"claim": None, "contract": None, "proof_opened": None, "proof_sealed": None, "done": None}
    for kind, ts, data in rows:
        at = _epoch(ts)
        if kind == "claim":
            marks["claim"] = at
        elif kind == "task-contract":
            marks["contract"] = at
        elif kind == "proof-scaffold" and (claim_ts is None or at >= claim_ts - 1):
            marks["proof_opened"] = at
        elif kind == "proof-report" and (claim_ts is None or at >= claim_ts - 1):
            marks["proof_sealed"] = at
        elif kind == "task-move":
            try:
                if json.loads(data or "{}").get("to") == "done":
                    marks["done"] = at
            except ValueError:
                pass
    labels = (("claim", "Claimed"), ("contract", "Contract recorded"), ("proof_opened", "Proof opened"),
              ("proof_sealed", "Proof sealed"), ("done", "Done"))
    return [{"key": k, "label": label, "ts": marks[k]} for k, label in labels]


def _plan(conn, op):
    from alpaca import taskplan
    try:
        return taskplan.latest(conn, op) or {}
    except Exception:
        return {}


# ---- the snapshot ----------------------------------------------------------------------------

def snapshot(root, now=None, only=None):
    """Every in-flight task with its record context, its session's activity since the claim and its progress."""
    from alpaca import work_record
    from alpaca.analytics import build_index
    now = float(now if now is not None else time.time())
    root = os.path.realpath(root)
    conn = db.connect_readonly(root)
    try:
        tasks = work_record.tasks(conn)
        by_id = {t["id"]: t for t in tasks}
        flying = [t for t in tasks if _live(t, now) and (only is None or t["id"] == only)]
        if not flying:
            return {"generated": now, "tasks": [], "count": 0}
        cards = ratecards.load(conn)
        cards_rev = ratecards.revision(root, conn=conn)
        sources = build_index._transcripts(root, conn)
        families = children.discover(root, conn)
        took = durations(tasks)
        ops = {row[0]: {"id": row[0], "title": row[1], "status": row[2]} for row in conn.execute("SELECT id, intent, status FROM ops")}
        phase = {}
        for op, kind, data in conn.execute("SELECT op, kind, data FROM events WHERE kind IN ('phase-enter','phase-advance') ORDER BY id"):
            try:
                d = json.loads(data or "{}")
                phase[op] = d.get("phase") if kind == "phase-enter" else str(d.get("boundary") or "").split("->")[-1]
            except ValueError:
                continue
        plans = {}
        sessions_of = {}
        out = []
        for t in flying:
            claim_ts, doing_ts = cycle(t.get("history") or [])
            claim_ts = claim_ts or now
            sid = next((e["session"] for e in reversed(t.get("history") or []) if e["kind"] == "claim" and e.get("session")),
                       t.get("assigned_session"))
            sessions_of.setdefault(sid, []).append(t["id"])
            plan = plans.setdefault(t["op"], _plan(conn, t["op"]))
            entries = {e["id"]: e for e in plan.get("tasks") or []}
            groups = {g["id"]: g for g in plan.get("groups") or []}
            entry = entries.get(t["id"], {})
            group = groups.get(entry.get("group")) if entry else None
            state = lambda i: {"id": i, "title": (by_id.get(i) or {}).get("title") or i, "status": (by_id.get(i) or {}).get("status", "unknown")}
            prereqs = [state(i) for i in entry.get("after") or []]
            successors = [state(e["id"]) for e in plan.get("tasks") or [] if t["id"] in (e.get("after") or [])]
            same_group = sorted((e["id"] for e in plan.get("tasks") or [] if entry and e.get("group") == entry.get("group")),
                                key=lambda i: (by_id.get(i, {}).get("number") or 0, i))
            at = same_group.index(t["id"]) if t["id"] in same_group else -1
            stream_prev = state(same_group[at - 1]) if at > 0 else None
            stream_next = state(same_group[at + 1]) if 0 <= at < len(same_group) - 1 else None
            in_stream = [took[i] for i in same_group if i in took] if entry else []
            in_op = [took[x["id"]] for x in tasks if x["op"] == t["op"] and x["id"] in took]
            activity = _activity(root, conn, sid, sources, families, claim_ts, now, cards, cards_rev, t["id"])
            active_runs = [r for r in activity.pop("_runs") if r["status"] not in fanout.FINISHED_RUN]
            card = {
                "id": t["id"], "title": t["title"], "statement": t.get("description") or t["title"], "op": t["op"],
                "operation": ops.get(t["op"], {}), "phase": t.get("phase"), "op_phase": phase.get(t["op"]),
                "claimant": t.get("claimant"), "lease_until": t.get("lease_until"), "session": sid,
                "claimed_at": claim_ts, "doing_since": doing_ts, "running_s": now - claim_ts,
                "doing_s": now - (doing_ts or claim_ts), "contract": t.get("contract"),
                "workstream": {"id": group.get("id"), "title": group.get("title")} if group else None,
                "prerequisites": prereqs, "successors": successors, "stream_prev": stream_prev, "stream_next": stream_next,
                "milestones": _milestones(conn, t["id"], claim_ts),
                "progress": progress(now - claim_ts, active_runs, in_stream, in_op),
                **activity}
            out.append(card)
        from alpaca.posture import level as posture_level
        try:
            levels = posture_level.levels(conn, {c["session"] for c in out}, root=root)
        except Exception:
            levels = {}
        for card in out:
            card["level"] = levels.get(card["session"])
            card["shared_with"] = [i for i in sessions_of.get(card["session"], []) if i != card["id"]]
        out.sort(key=lambda c: -(c.get("last_activity") or c["claimed_at"] or 0))
        return {"generated": now, "tasks": out, "count": len(out)}
    finally:
        conn.close()


_AGENT_TAIL = {}


def agent_state(path):
    """'done' when an agent ended its turn, returned its structured result or its SubagentStop hook ran;
    else 'running'.

    Reads at most the last 64 KB, once per change of size or modification time."""
    st = os.stat(path)
    key = (os.path.realpath(path), st.st_size, st.st_mtime_ns)
    with _LOCK:
        if key in _AGENT_TAIL:
            return _AGENT_TAIL[key]
    state, last = "running", st.st_mtime
    with open(path, "rb") as fh:
        fh.seek(max(0, st.st_size - 65536))
        for line in fh.read().splitlines()[1 if st.st_size > 65536 else 0:]:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            last = _epoch(rec.get("timestamp")) or last
            attachment = rec.get("attachment") if isinstance(rec.get("attachment"), dict) else {}
            if rec.get("type") == "assistant":
                message = rec.get("message") if isinstance(rec.get("message"), dict) else {}
                state = "done" if message.get("stop_reason") == "end_turn" else "running"
            elif attachment.get("type") == "structured_output" or attachment.get("hookEvent") == "SubagentStop":
                state = "done"           # the agent returned its result, or its SubagentStop hook ran
    with _LOCK:
        _AGENT_TAIL[key] = (state, last)
    return state, last


def live_runs(root, conn, sid, recorded, launches, task_id=None):
    """Workflow runs still running: Claude Code writes a run record only when a run ends, so a live
    run is read from its agents' transcripts (subagents/workflows/<run>/agent-*.jsonl) and their
    metadata (description, workflowPhase). The planned agent count is not on disk until the end."""
    stem = fanout._stem_dir(conn, root, sid)
    bases = ([os.path.join(stem, "subagents", "workflows")] if stem else []) + \
            [os.path.join(transcripts.subagents_dir(root, sid), "workflows")]
    seen, out = set(), []
    for base in bases:
        if not os.path.isdir(base):
            continue
        for run in sorted(os.listdir(base)):
            folder = os.path.join(base, run)
            if run in seen or run in recorded or not run.startswith("wf_") or not os.path.isdir(folder):
                continue
            seen.add(run)
            agents = []
            for name in sorted(os.listdir(folder)):
                if not (name.startswith("agent-") and name.endswith(".jsonl")):
                    continue
                path = os.path.join(folder, name)
                meta = {}
                try:
                    with open(path[:-len(".jsonl")] + ".meta.json", encoding="utf-8") as fh:
                        meta = json.load(fh)
                except (OSError, ValueError):
                    pass
                try:
                    state, last = agent_state(path)
                except OSError:
                    continue
                agents.append({"id": name[len("agent-"):-len(".jsonl")], "state": state, "last": last,
                               "phase": str(meta.get("workflowPhase") or ""), "label": str(meta.get("description") or "")[:120]})
            if not agents:
                continue
            launch = launches.get(run, {})
            phases = {}
            for a in agents:
                p = phases.setdefault(a["phase"] or "unnamed", {"title": a["phase"] or "unnamed", "agents": 0, "done": 0, "failed": 0})
                p["agents"] += 1
                p["done"] += a["state"] == "done"
            mine = [a for a in agents if task_id and re.search(r"\b%s\b" % re.escape(task_id), a["label"])]
            out.append({"run": run, "name": launch.get("workflow") or run, "status": "running", "live": True,
                        "start": launch.get("ts"), "end": None, "agents": len(agents), "planned": None,
                        "done": sum(a["state"] == "done" for a in agents), "failed": 0,
                        "running": sum(a["state"] != "done" for a in agents), "phases": list(phases.values()),
                        "peak": None, "last": max(a["last"] for a in agents),
                        "task_agents": len(mine), "task_done": sum(a["state"] == "done" for a in mine)})
    return out


def _activity(root, conn, sid, sources, families, since, now, cards, cards_rev, task_id=None):
    """The claiming session's work since the claim: totals, workflow fan-out, the narrative and signals."""
    empty = {"available": False, "responses": 0, "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
             "total_tokens": 0, "cost_usd": 0.0, "tool_calls": 0, "subagents": 0, "workflows": [], "feed": [],
             "status": [], "prompts": [], "now": None, "mode": None, "focus": [], "tests": test_trend([]),
             "signals": {}, "last_activity": None, "feed_start": None, "_runs": []}
    path = sources.get(sid) if sid else None
    if not path or not os.path.isfile(path):
        return empty
    threads = [(None, burn.read(path, cards, cards_rev))]
    kids = families.get(sid) or {}
    for cid, entry in kids.items():
        if entry.get("path") and os.path.isfile(entry["path"]):
            threads.append((cid, burn.read(entry["path"], cards, cards_rev)))
    rows = [r for _, st in threads for r in st["responses"] if r["ts"] >= since]
    calls = [c for _, st in threads for c in st["calls"] if c["ts"] and c["ts"] >= since]
    started = [cid for cid, st in threads[1:] if st["responses"] and st["responses"][0]["ts"] >= since]
    links = fanout.child_links(root, conn, sid)
    usage = {}
    for cid, st in threads[1:]:
        priced = [r["cost"] for r in st["responses"] if r["cost"] is not None and r["cost"] >= 0]
        usage[cid] = {"tokens": sum(burn._tokens(r) for r in st["responses"]), "cost": sum(priced) if priced else None,
                      "responses": len(st["responses"])}
    runs = []
    for name, rpath in sorted(fanout.run_files(root, conn, sid).items()):
        record = fanout.load(rpath)
        if record:
            run = fanout.summarize(record, usage, links, now)
            if run["start"] and run["start"] >= since - 60:
                runs.append(run)
    st = tail(path)
    events = [e for e in st["events"] if e["ts"] and e["ts"] >= since]
    launches = {e["run"]: e for e in st["events"] if e["t"] == "result" and e.get("run")}
    recorded = {os.path.splitext(n)[0] for n in fanout.run_files(root, conn, sid)}
    live = [r for r in live_runs(root, conn, sid, recorded, launches, task_id) if now - r["last"] < LIVE_S]
    running = any(r["status"] not in fanout.FINISHED_RUN for r in runs) or bool(live)
    lines = feed(events, root, limit=None)
    for line in lines:
        if line["kind"] == "workflow":
            # The run this call started: the first run record that began within two minutes after it.
            run = next((r for r in sorted(runs, key=lambda r: r["start"]) if line["ts"] - 5 <= r["start"] <= line["ts"] + 120), None)
            if run:
                finished = sum(a["state"] in FINISHED for a in run["agents"])
                line["text"] = "Workflow %s: %d of %d agents finished (%s)" % (
                    run["name"], finished, max(run.get("agent_count") or 0, len(run["agents"])), run["status"])
            else:
                match = next((r for r in live if r.get("start") and abs(r["start"] - line["ts"]) < 120), None)
                if match:
                    line["text"] = "Workflow %s: %d of %d agents started so far finished (running)" % (
                        match["name"], match["done"], match["agents"])
    shown = lines[:FEED]
    texts = [e for e in events if e["t"] == "text"]
    signals = {}
    flags = []
    for thread, th in threads:
        for f in insights.loops([c for c in th["calls"] if c["ts"] and c["ts"] >= since], th["errors"]):
            flags.append(dict(f, thread=thread))
    signals["loops"] = flags[:5]
    bins = {}
    for r in (r for _, th in threads for r in th["responses"]):
        bins[int(r["ts"] // 900)] = bins.get(int(r["ts"] // 900), 0) + burn._tokens(r)
    signals["runaway"] = [dict(b, start=b["bucket"] * 900) for b in insights.runaway(bins) if b["bucket"] * 900 >= since - 900]
    friction = []
    for thread, th in threads:
        friction += insights.friction({k: e for k, e in th["errors"].items() if e["ts"] and e["ts"] >= since}, [h for h in th["hooks"] if h["ts"] and h["ts"] >= since], thread)
    signals["friction"] = friction[-5:]
    stamps = sorted(r["ts"] for r in rows)
    idle_s, gaps = insights.idle(stamps)
    signals["idle"] = {"idle_s": idle_s, "gaps": gaps, "quiet_s": now - stamps[-1] if stamps else None}
    priced = [r["cost"] for r in rows if r["cost"] is not None and r["cost"] >= 0]
    return {"available": True, "responses": len(rows),
            "tokens": {"input": sum(r["in"] for r in rows), "output": sum(r["out"] for r in rows),
                       "cache_read": sum(r["cr"] for r in rows), "cache_write": sum(r["cw"] for r in rows)},
            "total_tokens": sum(burn._tokens(r) for r in rows), "cost_usd": sum(priced), "tool_calls": len(calls),
            "subagents": len(started),
            "workflows": [{"run": r["run"], "name": r["name"], "status": r["status"], "start": r["start"], "end": r["end"],
                           "agents": len(r["agents"]), "planned": max(r.get("agent_count") or 0, len(r["agents"])),
                           "done": sum(a["state"] == "done" for a in r["agents"]),
                           "failed": sum(a["state"] in fanout.FAILED for a in r["agents"]),
                           "running": sum(a["state"] not in FINISHED for a in r["agents"]),
                           "phases": [{"title": p["title"], "agents": p["agents"],
                                       "done": sum(a["state"] == "done" for a in r["agents"] if a["phase"] == p["index"]),
                                       "failed": p["failed"]} for p in r["phases"]],
                           "peak": r["figures"]["peak"]} for r in runs] + live,
            "feed": shown, "status": [{"ts": e["ts"], "text": e["text"]} for e in reversed(texts)][:STATUS_LINES],
            "prompts": [{"ts": e["ts"], "text": e["text"]} for e in events if e["t"] == "prompt"][-5:][::-1],
            "now": texts[-1]["text"] if texts else None, "mode": mode(events, running), "focus": focus(events, root),
            "tests": test_trend(events), "signals": signals,
            "last_activity": max(stamps[-1] if stamps else 0, events[-1]["ts"] if events else 0) or None,
            "feed_start": min((l["off"] for l in shown if l.get("off") is not None), default=None),
            "feed_complete": len(lines) <= FEED and (st["start"] == 0 or (bool(events) and events[0] is not st["events"][0])),
            "_runs": runs}


def more(root, task_id, before):
    """Earlier activity lines for one in-flight task's session, read from before byte offset `before`."""
    from alpaca.analytics import build_index
    root = os.path.realpath(root)
    conn = db.connect_readonly(root)
    try:
        row = conn.execute("SELECT session FROM events WHERE ref=? AND kind='claim' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        sid = row[0] if row else None
        path = build_index._transcripts(root, conn).get(sid) if sid else None
        claim = conn.execute("SELECT ts FROM events WHERE ref=? AND kind='claim' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    finally:
        conn.close()
    if not path or not os.path.isfile(path):
        raise KeyError("no transcript for this task's session")
    events, start = earlier(path, int(before))
    since = _epoch(claim[0]) if claim else 0
    reached = start == 0 or any(e["ts"] and e["ts"] < since for e in events)
    events = [e for e in events if e["ts"] and e["ts"] >= since]
    return {"task": task_id, "feed": feed(events, root, limit=None), "start": start, "complete": reached}
