"""How each workflow run in a session fanned out (t-143).

A Claude Code workflow run leaves a run record beside the session transcript,
`<stem>/workflows/wf_<run>.json`: runId, workflowName, status, startTime (ms), durationMs,
agentCount, defaultModel, phases [{title, detail}] and workflowProgress, whose entries of type
"workflow_agent" carry index, label, phaseIndex, phaseTitle, agentId, model, state, queuedAt,
startedAt, lastProgressAt, durationMs, attempt, tokens and toolCalls. Each agent's own transcript
sits under `<stem>/subagents/workflows/<run>/agent-<agentId>.jsonl`.

`alpaca.transcripts.snapshot` keeps a copy of every run record under
`.alpaca/transcripts/<sid>/workflows/`; the registered source is read first and the project copy
when the source is gone. Nothing here writes.

Per run this measures: agents, peak concurrent agents (the fan-out width), agents per phase, the
median queue wait (startedAt - queuedAt), the median and longest agent duration, agent tokens and
estimated cost (joined from each agent's transcript by agentId where it is readable, else the run
record's own `tokens`, which has no token kinds and so no price), failures, unfinished agents and
retries (attempt > 1).
"""
from __future__ import annotations

import glob
import json
import os
import statistics
import threading
from pathlib import Path

from alpaca import transcripts

FAILED = {"error", "failed", "killed", "cancelled", "canceled"}
FINISHED_RUN = {"completed", "killed", "failed", "error", "cancelled", "canceled"}
MAX_RUN_BYTES = 8 * 1024 * 1024

_RUNS = {}
_LOCK = threading.RLock()


def _ms(value):
    """Milliseconds as a float from an int, a float or a numeric string; None otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _stem_dir(conn, root, sid):
    try:
        registered = transcripts.registered(conn, sid)
    except Exception:
        registered = ""
    if not registered:
        return None
    if not os.path.isabs(registered):
        registered = os.path.join(root, registered)
    return os.path.join(os.path.dirname(registered), Path(registered).stem)


def run_files(root, conn, sid):
    """{run file name: path}: the registered source's run records, else the project copies."""
    found = {}
    stem = _stem_dir(conn, root, sid)
    for base in ([os.path.join(stem, "workflows")] if stem else []) + [transcripts.workflows_dir(root, sid)]:
        if not os.path.isdir(base) or os.path.islink(base):
            continue
        for path in sorted(glob.glob(os.path.join(base, "wf_*.json"))):
            if not os.path.islink(path):
                found.setdefault(os.path.basename(path), path)
    return found


def metas(root, conn, sid):
    """{agent id: metadata} from every agent-<id>.meta.json beside the session (source, then copy)."""
    out = {}
    stem = _stem_dir(conn, root, sid)
    for base in ([os.path.join(stem, "subagents")] if stem else []) + [transcripts.subagents_dir(root, sid)]:
        if not os.path.isdir(base) or os.path.islink(base):
            continue
        for path in glob.glob(os.path.join(base, "**", "agent-*.meta.json"), recursive=True):
            ident = os.path.basename(path)[len("agent-"):-len(".meta.json")]
            if ident in out:
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    value = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(value, dict):
                out[ident] = value
    return out


def load(path):
    """One run record, parsed once per change of size or modification time."""
    st = os.stat(path)
    key = os.path.realpath(path)
    with _LOCK:
        hit = _RUNS.get(key)
        if hit and hit[0] == (st.st_size, st.st_mtime_ns):
            return hit[1]
    if st.st_size > MAX_RUN_BYTES:
        return None
    with open(path, encoding="utf-8") as fh:
        try:
            value = json.load(fh)
        except ValueError:
            return None
    value = value if isinstance(value, dict) else None
    with _LOCK:
        _RUNS[key] = ((st.st_size, st.st_mtime_ns), value)
    return value


def peak(intervals):
    """The most intervals open at one instant; an interval ending where another starts does not overlap it."""
    points = []
    for a, b in intervals:
        if a is None or b is None:
            continue
        points.append((a, 1))
        points.append((max(a, b), -1))
    best = now = 0
    for _, step in sorted(points, key=lambda p: (p[0], p[1])):
        now += step
        best = max(best, now)
    return best


def pack(intervals):
    """A slot per interval so that no two intervals in one slot overlap (greedy by start)."""
    order = sorted(range(len(intervals)), key=lambda i: (intervals[i][0], intervals[i][1]))
    ends, slots = [], [0] * len(intervals)
    for i in order:
        a, b = intervals[i]
        slot = next((k for k, e in enumerate(ends) if e <= a), None)
        if slot is None:
            ends.append(b)
            slot = len(ends) - 1
        ends[slot] = b
        slots[i] = slot
    return slots, len(ends)


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def _family(model):
    from alpaca.analytics.burn import family_of
    return family_of(model)


def summarize(record, usage=None, links=None, now=None):
    """The fan-out of one run record.

    `usage` maps an agent id to {tokens, cost, responses} measured from its transcript; an agent
    missing there falls back to the record's `tokens`. `links` maps an agent id to the child
    identifier the session detail opens."""
    usage, links = usage or {}, links or {}
    start = (_ms(record.get("startTime")) or 0) / 1000.0 or None
    run_ms = _ms(record.get("durationMs"))
    status = str(record.get("status") or "unknown")
    progress = record.get("workflowProgress") if isinstance(record.get("workflowProgress"), list) else []
    phase_titles = {}
    for i, ph in enumerate(record.get("phases") or [], 1):
        if isinstance(ph, dict):
            phase_titles[i] = {"title": str(ph.get("title") or "Phase %d" % i), "detail": str(ph.get("detail") or "")}
    for entry in progress:
        if isinstance(entry, dict) and entry.get("type") == "workflow_phase" and isinstance(entry.get("index"), int):
            phase_titles.setdefault(entry["index"], {"title": str(entry.get("title") or ""), "detail": ""})
            if entry.get("title"):
                phase_titles[entry["index"]]["title"] = str(entry["title"])
    end = start + run_ms / 1000.0 if start and run_ms is not None else None
    finished = status in FINISHED_RUN
    agents = []
    for entry in progress:
        if not isinstance(entry, dict) or entry.get("type") != "workflow_agent":
            continue
        queued = _ms(entry.get("queuedAt"))
        started = _ms(entry.get("startedAt"))
        dur = _ms(entry.get("durationMs"))
        last = _ms(entry.get("lastProgressAt"))
        queued = queued / 1000.0 if queued else None
        started = started / 1000.0 if started else None
        state = str(entry.get("state") or "unknown")
        if started is not None and dur is not None:
            stop = started + dur / 1000.0
        elif last:
            stop = last / 1000.0
        else:
            stop = end if finished else now
        ident = str(entry.get("agentId") or "")
        measured = usage.get(ident)
        attempt = entry.get("attempt") if isinstance(entry.get("attempt"), int) else 1
        agents.append({
            "i": entry.get("index"), "label": str(entry.get("label") or "")[:120],
            "phase": entry.get("phaseIndex") if isinstance(entry.get("phaseIndex"), int) else 0,
            "id": ident, "model": str(entry.get("model") or record.get("defaultModel") or "unknown")[:80],
            "state": state, "q": queued, "s": started, "e": stop, "dur": dur / 1000.0 if dur is not None else None,
            "attempt": attempt, "tools": entry.get("toolCalls") if isinstance(entry.get("toolCalls"), int) else None,
            "last_tool": str(entry.get("lastToolName") or "")[:60],
            "tokens": measured["tokens"] if measured else (entry.get("tokens") if isinstance(entry.get("tokens"), int) else None),
            "cost": measured["cost"] if measured else None,
            "src": "transcript" if measured else ("record" if isinstance(entry.get("tokens"), int) else None),
            "unfinished": state not in FAILED and state != "done" and finished,
            "child": links.get(ident)})
        agents[-1]["fam"] = _family(agents[-1]["model"])
    spans = [(a["s"], a["e"]) for a in agents if a["s"] is not None and a["e"] is not None]
    if end is None and spans:
        end = max(b for _, b in spans)
    slots, rows = pack([(a["s"] if a["s"] is not None else (a["q"] or start or 0),
                         a["e"] if a["e"] is not None else (a["s"] or a["q"] or start or 0)) for a in agents])
    for a, slot in zip(agents, slots):
        a["slot"] = slot
    phases = []
    for index in sorted(set(phase_titles) | {a["phase"] for a in agents if a["phase"]}):
        mine = [a for a in agents if a["phase"] == index]
        times = [t for a in mine for t in (a["s"], a["e"]) if t is not None]
        info = phase_titles.get(index, {"title": "Phase %d" % index, "detail": ""})
        phases.append({"index": index, "title": info["title"], "detail": info["detail"], "agents": len(mine),
                       "start": min(times) if times else None, "end": max(times) if times else None,
                       "failed": sum(a["state"] in FAILED for a in mine), "retries": sum(a["attempt"] > 1 for a in mine),
                       "peak": peak([(a["s"], a["e"]) for a in mine])})
    waits = [a["s"] - a["q"] for a in agents if a["s"] is not None and a["q"] is not None]
    durs = [a["dur"] for a in agents if a["dur"] is not None]
    tokens = [a["tokens"] for a in agents if a["tokens"] is not None]
    costs = [a["cost"] for a in agents if a["cost"] is not None]
    models = {}
    for a in agents:
        models[a["model"]] = models.get(a["model"], 0) + 1
    return {"run": str(record.get("runId") or "")[:80], "name": str(record.get("workflowName") or record.get("runId") or "workflow")[:120],
            "status": status, "start": start, "end": end, "model": str(record.get("defaultModel") or ""),
            "agent_count": record.get("agentCount") if isinstance(record.get("agentCount"), int) else len(agents),
            "agents": agents, "rows": rows, "phases": phases, "models": models,
            "figures": {"agents": len(agents), "peak": peak(spans),
                        "queue_median_s": _median(waits), "queue_longest_s": max(waits) if waits else None,
                        "duration_median_s": _median(durs), "duration_longest_s": max(durs) if durs else None,
                        "tokens": sum(tokens) if tokens else None, "cost_usd": sum(costs) if costs else None,
                        "joined": sum(a["src"] == "transcript" for a in agents),
                        "from_record": sum(a["src"] == "record" for a in agents),
                        "failed": sum(a["state"] in FAILED for a in agents),
                        "unfinished": sum(a["unfinished"] for a in agents),
                        "retries": sum(a["attempt"] > 1 for a in agents),
                        "extra_attempts": sum(max(0, a["attempt"] - 1) for a in agents),
                        "record_tokens": record.get("totalTokens") if isinstance(record.get("totalTokens"), int) else None}}


def child_links(root, conn, sid, strict=False):
    """{agent id: child identifier} for the children the session detail can open.

    A listing that cannot be read is {}; with `strict` it raises instead, so a caller that keeps
    the result can tell a failure from a session with no children."""
    from alpaca.analytics import detail
    try:
        registered = transcripts.registered(conn, sid)
        if registered and not os.path.isabs(registered):
            registered = os.path.join(root, registered)
        found, _cut = detail._children(root, sid, registered or None)
    except Exception:
        if strict:
            raise
        return {}
    out = {}
    for ident, path in found.items():
        name = Path(path).name
        agent = name[len("agent-"):-len(".jsonl")] if name.startswith("agent-") else Path(path).stem
        out.setdefault(agent, ident)
    return out
