"""Session insights for the Sessions cost board (t-145 to t-153): signs that work is going off track.

Every function here is pure: it takes the parsed transcripts (alpaca.analytics.burn.read) and rows
of the record, and returns plain data the board shows. The rules, stated once:

- Loops (t-146), per conversation (a main thread or one subagent):
  R1 the same normalized tool call (same tool, same command or arguments) REPEAT or more times
     within WINDOW consecutive model responses; edits are left to R2.
  R2 the same file edited (Edit, Write, MultiEdit, NotebookEdit) EDITS or more times within WINDOW
     consecutive responses.
  R3 a test command (pytest, npm test, go test, cargo test, jest, vitest, unittest, make test)
     failing CYCLES or more times in a row with an edit between each failure and no passing run.
- Wasted spend (t-147): unfinished agents of a killed workflow run, failed agents, the earlier
  attempts of a retried agent (cost x (attempt - 1) / attempt, assuming equal attempts), and the
  spend of a task cycle that ended in done and was later moved out of done (reopened).
- Off-task spend (t-145): a response is "unclaimed" when its session holds no task; "off-task" when
  it holds a task and the response edits, writes or runs a command on files inside the project
  none of which match the held tasks' paths; otherwise "claimed". Task paths are the file and
  directory names in the task's title, statement, where and contract. A target matches a task path
  when it is the same path, lies under it, or shares its file name when the task names a bare file;
  a command naming the task id matches too; files under .alpaca/ (record, proofs) and the tools a
  command runs (.venv/, bin/, node_modules/, .git/) are neutral.
  A session holds a task from its claim (or a move to doing) until the task moves out of doing,
  its lease expires or another session claims it.
- Cost per outcome (t-148): the spend of every response a holding session (or its subagents) made
  while it held the task, split evenly across the tasks it held at that moment, from the claim to
  the move to done. Outliers: more than OUTLIER x the median of done tasks in the same operation.
- Idle and cache (t-149): idle time is every gap of more than IDLE_S between requests of a session
  (subagents included); a cache re-write is the cache_creation of a response that follows a gap of
  more than IDLE_S in its own conversation (HOUR_S when the conversation writes 1-hour cache);
  owner wait is the time from a session's last response to the owner's next typed prompt.
- Runaway (t-150): a 15-minute bucket whose tokens are RUNAWAY x the session's median bucket and at
  least RUNAWAY_FLOOR; an agent whose tokens per active minute are RUNAWAY x the median agent of its
  session and at least AGENT_FLOOR per minute.
- Collisions (t-151): two sessions (subagents count toward their parent) that edited the same file
  within COLLIDE_S of each other.
- Friction (t-152) and hook overhead (t-153): see friction() and hooks().
"""
from __future__ import annotations

import os
import re
import statistics

WINDOW, REPEAT, EDITS, CYCLES = 20, 5, 8, 3
IDLE_S, HOUR_S = 300, 3600
RUNAWAY, RUNAWAY_FLOOR, AGENT_FLOOR = 5.0, 10_000_000, 1_000_000
COLLIDE_S = 900
OUTLIER = 2.0
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"}
TEST_RE = re.compile(r"\b(pytest|py\.test|npm (?:run )?test|pnpm test|yarn test|go test|cargo test|jest|vitest|"
                     r"unittest|make test|tox)\b")
PATH_RE = re.compile(r"(?<![\w@:$])((?:\.{0,2}/)?(?:[\w.-]+/)+[\w.-]*|[\w-]+\.(?:py|js|mjs|ts|css|html|md|json|"
                     r"ya?ml|toml|sh|txt|sv|v|go|rs|sql))")
TASK_ID = re.compile(r"\bt-\d{3,}\b")


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


# ---- t-146 loops ----------------------------------------------------------------------------

def _most_in_window(positions, window):
    """The largest number of positions inside any span of `window` consecutive responses, and where."""
    best, at, i = 0, 0, 0
    for j in range(len(positions)):
        while positions[j] - positions[i] >= window:
            i += 1
        if j - i + 1 > best:
            best, at = j - i + 1, i
    return best, at


def loops(calls, errors, window=WINDOW, repeat=REPEAT, edits=EDITS, cycles=CYCLES):
    """The loop rules that fired in one conversation, each with the repeated call and its count."""
    flags = []
    by_key, by_file = {}, {}
    for c in calls:
        if c["name"] in EDIT_TOOLS and c.get("path"):
            by_file.setdefault(c["path"], []).append(c)
        else:
            by_key.setdefault(c["key"], []).append(c)
    for rule, groups, limit, label in (("R1", by_key, repeat, "same call repeated"),
                                       ("R2", by_file, edits, "same file edited")):
        for key, items in groups.items():
            count, at = _most_in_window([c["r"] for c in items], window)
            if count >= limit:
                flags.append({"rule": rule, "label": label, "call": key, "count": count,
                              "within": window, "ts": items[at]["ts"], "line": items[at]["line"]})
    streak, best, since_edit, first = {}, {}, {}, {}
    for c in calls:
        if c["name"] in EDIT_TOOLS:
            for key in since_edit:
                since_edit[key] = True
            continue
        if not c.get("cmd") or not TEST_RE.search(c["cmd"]):
            continue
        key = c["key"]
        failed = c["id"] in errors if c["id"] else False
        if not failed:
            streak[key] = 0
            since_edit[key] = False
            continue
        if streak.get(key, 0) == 0 or since_edit.get(key):
            streak[key] = streak.get(key, 0) + 1
            if streak[key] == 1:
                first[key] = c
        since_edit[key] = False
        if streak[key] > best.get(key, 0):
            best[key] = streak[key]
    for key, count in best.items():
        if count >= cycles:
            flags.append({"rule": "R3", "label": "test fail, edit, fail", "call": key, "count": count,
                          "within": None, "ts": first[key]["ts"], "line": first[key]["line"]})
    return sorted(flags, key=lambda f: (-f["count"], f["rule"], f["call"]))


# ---- task holds (t-145, t-148, t-147 reopened) --------------------------------------------

def holds(events):
    """[(task, session, start, end)] from claim, task-move and lease-expired rows, in time order.

    `events` are dicts with ts (epoch), kind, session, ref and data."""
    open_, out = {}, []
    for e in sorted(events, key=lambda e: (e["ts"], e.get("id", 0))):
        task, sid, kind, data = e.get("ref"), e.get("session"), e.get("kind"), e.get("data") or {}
        if not task:
            continue
        if kind == "claim" or (kind == "task-move" and data.get("to") == "doing"):
            holder = open_.get(task)
            if holder and holder[0] != sid:
                out.append((task, holder[0], holder[1], e["ts"]))
                holder = None
            if not holder and sid:
                open_[task] = (sid, e["ts"])
        elif kind == "task-move" or kind == "lease-expired":
            holder = open_.pop(task, None)
            if holder:
                out.append((task, holder[0], holder[1], e["ts"]))
    for task, (sid, start) in open_.items():
        out.append((task, sid, start, float("inf")))
    return sorted(out, key=lambda h: (h[2], h[0]))


def cycles(events):
    """Per task, its done cycles: [{task, start, done, reopened_at}] from task-move rows.

    A cycle starts at the first claim or move to doing after the previous done and ends at a move
    to done; `reopened_at` is the time of a later move out of done, when there is one."""
    out, start, current = [], {}, {}
    for e in sorted(events, key=lambda e: (e["ts"], e.get("id", 0))):
        task, kind, data = e.get("ref"), e.get("kind"), e.get("data") or {}
        if not task:
            continue
        if kind == "claim" or (kind == "task-move" and data.get("to") == "doing"):
            start.setdefault(task, e["ts"])
        if kind == "task-move" and data.get("to") == "done":
            cycle = {"task": task, "start": start.pop(task, e["ts"]), "done": e["ts"], "reopened_at": None,
                     "session": e.get("session")}
            out.append(cycle)
            current[task] = cycle
        elif kind == "task-move" and data.get("from") == "done" and data.get("to") != "done" and task in current:
            current.pop(task)["reopened_at"] = e["ts"]
    return out


def task_paths(task, contract_texts=()):
    """The file and directory names a task names, relative and without ./ ."""
    text = " ".join([str(task.get(k) or "") for k in ("title", "statement", "where_", "why")] + list(contract_texts))
    out = set()
    for match in PATH_RE.findall(text):
        path = match.strip("./") if match.startswith("./") else match.rstrip(".,;:)")
        if path and not path.startswith(("http", "www.")) and len(path) > 2:
            out.add(path.rstrip("/"))
    return out


def targets(call, root):
    """Project files one call edits, writes or names in a command, relative to the project root."""
    raw = [call["path"]] if call.get("path") else []
    if call.get("cmd"):
        raw += [m for m in PATH_RE.findall(call["cmd"]) if not m.startswith("-")]
    out = set()
    root = os.path.realpath(root) if root else ""
    for item in raw:
        item = item.strip("'\"")
        if not item:
            continue
        if os.path.isabs(item):
            if not root or not (item == root or item.startswith(root + os.sep)):
                continue
            item = os.path.relpath(item, root)
        item = os.path.normpath(item)
        if item.startswith("..") or item in (".", ""):
            continue
        # A worktree path names the same project file.
        item = re.sub(r"^\.claude/worktrees/[^/]+/", "", item)
        out.add(item)
    return out


NEUTRAL = (".alpaca", ".venv", "bin", "node_modules", ".git")


def matches(target, paths, task_ids=(), command=""):
    """True when a target belongs to a held task, False when it does not, None when it is neutral:
    the record and proofs (.alpaca/), and tools a command runs (.venv/, bin/, node_modules/, .git/)."""
    if target.split("/", 1)[0] in NEUTRAL:
        return None
    for task in task_ids:
        if task in target or (command and task in command):
            return True
    for p in paths:
        if target == p or target.startswith(p + "/") or p.startswith(target + "/"):
            return True
        if "/" not in p and os.path.basename(target) == p:
            return True
    return False


# ---- t-149 idle, cache re-writes, owner wait --------------------------------------------------

def idle(stamps, gap=IDLE_S):
    """(idle seconds, number of gaps) over one session's request times."""
    stamps = sorted(stamps)
    gaps = [b - a for a, b in zip(stamps, stamps[1:]) if b - a > gap]
    return sum(gaps), len(gaps)


def cache_rewrites(responses, gap=IDLE_S, hour=HOUR_S):
    """Cache re-writes after an idle gap in one conversation: [{ts, idle_s, tokens, cost}]."""
    out, hourly, prev = [], False, None
    for r in sorted(responses, key=lambda r: r["ts"]):
        limit = hour if hourly else gap
        if prev is not None and r["ts"] - prev > limit and r["cw"] > 0:
            out.append({"ts": r["ts"], "idle_s": r["ts"] - prev, "tokens": r["cw"],
                        "cost": r.get("cwc") if isinstance(r.get("cwc"), (int, float)) else None})
        if isinstance(r.get("cw1"), int) and r["cw1"] > 0:
            hourly = True
        prev = r["ts"]
    return out


def owner_wait(response_stamps, prompts):
    """(seconds, count): from the last response before each typed prompt to that prompt."""
    stamps = sorted(response_stamps)
    total, count, i, last_prompt = 0.0, 0, 0, None
    for p in sorted(prompts):
        while i < len(stamps) and stamps[i] < p:
            i += 1
        before = stamps[i - 1] if i else None
        if before is not None and (last_prompt is None or before > last_prompt):
            total += p - before
            count += 1
        last_prompt = p
    return total, count


# ---- t-150 runaway ----------------------------------------------------------------------------

def runaway(bins, factor=RUNAWAY, floor=RUNAWAY_FLOOR):
    """Buckets burning far above the session's own median bucket: [{bucket, tokens, median, ratio}]."""
    values = [v for v in bins.values() if v > 0]
    med = _median(values)
    if not med:
        return []
    return [{"bucket": b, "tokens": v, "median": med, "ratio": v / med}
            for b, v in sorted(bins.items()) if v >= factor * med and v >= floor]


def runaway_agents(rates, factor=RUNAWAY, floor=AGENT_FLOOR):
    """Agents whose tokens per active minute are far above the session's median agent."""
    med = _median(list(rates.values()))
    if not med:
        return []
    return sorted(({"agent": a, "rate": v, "median": med, "ratio": v / med}
                   for a, v in rates.items() if v >= factor * med and v >= floor), key=lambda x: -x["rate"])


# ---- t-151 collisions -------------------------------------------------------------------------

def collisions(edits, within=COLLIDE_S):
    """Files two sessions edited within `within` seconds of each other.

    `edits` are (session, ts, path). Returns [{path, a, b, start, end, a_edits, b_edits}]."""
    by_path = {}
    for sid, ts, path in edits:
        by_path.setdefault(path, {}).setdefault(sid, []).append(ts)
    out = []
    for path, sessions in by_path.items():
        sids = sorted(sessions)
        for i, a in enumerate(sids):
            for b in sids[i + 1:]:
                ta, tb = sorted(sessions[a]), sorted(sessions[b])
                close_a = [t for t in ta if any(abs(t - u) <= within for u in tb)]
                close_b = [u for u in tb if any(abs(t - u) <= within for t in ta)]
                if close_a and close_b:
                    out.append({"path": path, "a": a, "b": b, "start": min(close_a + close_b),
                                "end": max(close_a + close_b), "a_edits": len(close_a), "b_edits": len(close_b)})
    return sorted(out, key=lambda c: (c["start"], c["path"]))


# ---- t-152 friction ---------------------------------------------------------------------------

FRICTION = (
    ("git_guard", "Git guard denial", re.compile(r"Alpaca git guard")),
    ("hook_denial", "Other PreToolUse hook denial", re.compile(r"PreToolUse:\w+ hook (?:error|denied)|hook denied", re.I)),
    ("permission", "Permission denied", re.compile(r"doesn't want to proceed|Permission to use \S+ (?:has been|was) denied|denied this tool", re.I)),
)
STYLE = re.compile(r"\[alpaca style\]|banned wording", re.I)


def friction(errors, hooks, thread=None):
    """Friction items from one conversation's failed tool results and hook messages."""
    out = []
    for ident, e in errors.items():
        for kind, label, pattern in FRICTION:
            if pattern.search(e["text"]):
                out.append({"kind": kind, "label": label, "ts": e["ts"], "line": e["line"], "thread": thread,
                            "text": e["text"][:200]})
                break
    for h in hooks:
        if h["kind"] in ("block", "error") and STYLE.search(h.get("text") or ""):
            out.append({"kind": "style", "label": "Style hook rejection", "ts": h["ts"], "line": h["line"],
                        "thread": thread, "text": (h.get("text") or "")[:200]})
    return out


# ---- t-153 hook overhead ----------------------------------------------------------------------

HOOK_NAME = re.compile(r"alpaca\.hooks\.(\w+)")


def hook_name(command):
    """The Alpaca hook a command runs, or None for a hook that is not Alpaca's."""
    match = HOOK_NAME.search(command or "")
    if match:
        return match.group(1)
    if re.search(r"\balpaca(?:-python)?\b|te-python", command or ""):
        return "alpaca"
    return None


def hooks(runs):
    """{hook: {runs, ms, max_ms, failures}} for Alpaca hooks from one session's hook records."""
    out = {}
    for h in runs:
        name = hook_name(h.get("cmd"))
        if not name:
            continue
        row = out.setdefault(name, {"runs": 0, "ms": 0.0, "max_ms": 0.0, "failures": 0})
        if h["kind"] == "run" and h.get("ms") is not None:
            row["runs"] += 1
            row["ms"] += h["ms"]
            row["max_ms"] = max(row["max_ms"], h["ms"])
        elif h["kind"] in ("error", "cancelled") or (h["kind"] == "block" and not STYLE.search(h.get("text") or "")):
            row["failures"] += 1
            if h.get("ms") is not None:
                row["ms"] += h["ms"]
    return out
