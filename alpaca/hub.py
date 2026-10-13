"""Project-contained, read-only views for the operations hub.

Receipt verdicts describe a historical attempt. Only the domain profile's read-only
check (alpaca/profile.py `check`) establishes current stage validity; a project without a
profile has no stages, runs or acceptance outcomes, and the hub hides those panels.
Catalog seals describe recorded sealing, not a fresh verification of every attachment.
No read opens the record through db.connect, which would run migrations, or runs
collection/projection commands.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from html import unescape
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import threading

from alpaca import db, paths, profile, util, work_record


_HEX = re.compile(r"^[a-f0-9]{32}$")
_SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
         ".mypy_cache", ".cache", "dist", "build", "cache", "caches"}
_REPORT_ROOTS = ("docs", "reports", "runbooks", "specs", ".alpaca/proofs",
                 ".alpaca/reports", ".alpaca/reviews", ".alpaca/wiki/decisions")
#: An event that reports a failure. `capture-no-collector` (the hooks' note that nobody captures,
#: t-070 round 2 C-D, I-5) carries an `error` text but is a state, not a failure; the same note
#: recorded earlier as `drain-failed` stays a failure, as it was read then.
_FAILURE_SQL = """(kind <> 'capture-no-collector' AND (kind LIKE '%failed%' OR kind LIKE '%error%' OR
    CASE WHEN json_valid(data) THEN
      upper(COALESCE(json_extract(data,'$.verdict_name'),json_extract(data,'$.verdict'),''))
      IN ('FAIL','BLOCKED','INTERRUPTED','ERROR') OR json_extract(data,'$.error') IS NOT NULL
    ELSE 1 END))"""
#: the kinds a session writes without doing work of its own. A domain profile adds its own
#: side-effect kinds (alpaca/profile.py `events`).
_SIDE_EFFECT_KINDS = ('session-start', 'session-end', 'init', 'transcript-snapshot', 'style-rearm',
                      'verdict', 'run', 'bridge')

#: subagent lifecycle kinds, kept out of the work and progress feeds.
_AGENT_KINDS = ('subagent-start', 'subagent-stop')
#: session bookkeeping and conversation kinds, kept out of the progress feed only.
_CHAT_KINDS = ('session-start', 'session-end', 'session-checkpoint', 'init', 'mission-snapshot',
               'authority-bind', 'msg', 'question', 'bridge', 'sensed', 'lineage')


def _work_sql(root):
    """The work filter of the history feed and its parameters after the id watermark."""
    extra = profile.listed(profile.load(root).events(), "side_effect_kinds")
    kinds = list(_SIDE_EFFECT_KINDS) + [str(k) for k in extra if str(k) not in _SIDE_EFFECT_KINDS]
    sql = """(%s OR (kind NOT IN ('heartbeat','turn-end','transcript-snapshot','style-rearm',
    'transcript-version')
    AND (kind NOT IN ('session-start','session-end','init') OR session IN
      (SELECT DISTINCT work.session FROM events work WHERE work.id <= ? AND work.kind NOT IN
       (%s)))))""" % (_FAILURE_SQL, ",".join("?" * len(kinds)))
    return sql, kinds


def _contained(root, value):
    """Return a resolved project path or None; never follow an escaping symlink."""
    if not isinstance(value, (str, Path)) or not str(value):
        return None
    path = Path(value)
    try:
        full = (path if path.is_absolute() else root / path).resolve()
        full.relative_to(root)
        return full
    except (ValueError, OSError, RuntimeError):
        return None


def _relative(root, value):
    path = _contained(root, value)
    return path.relative_to(root).as_posix() if path is not None else None


@contextmanager
def _record(root):
    conn = db.connect_readonly(root)
    try:
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def _rows(conn, query, params=()):
    return [dict(row) for row in conn.execute(query, params)] if conn is not None else []


def _json(value):
    try:
        data = json.loads(value)
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def _summary(kind, data, ref=None):
    for field in ("summary", "note", "reason", "error", "statement", "body", "intent"):
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:500]
    return kind.replace("-", " ") + (": " + str(ref) if ref else "")


def _phases(row):
    """The op's declared phase order; an op opened without phases has none."""
    try:
        value = json.loads(row.get("phases") or "[]")
    except (TypeError, ValueError):
        return []
    return [p for p in value if isinstance(p, str)] if isinstance(value, list) else []


def _op_states(conn):
    """Pin choice, short title, phase events and close per op, read from the record in event order.
    The newest pin or unpin wins, the newest title (op-open's, then op-title renames) wins, and the
    newest op-close gives the close time and basis; nothing is inferred from activity."""
    pins, titles, phase_events, closes = {}, {}, {}, {}
    for row in _rows(conn, "SELECT op,kind,ts,data FROM events WHERE kind IN "
                           "('op-pin','op-unpin','op-open','op-title','phase-enter','phase-advance',"
                           "'phase-door-closed','op-close') ORDER BY id"):
        if row["kind"] == "op-close":
            # the newest close of the op, with the basis its closer recorded (op close --basis)
            data = _json(row["data"])
            basis = data.get("basis") if isinstance(data, dict) else None
            closes[row["op"]] = {"at": row["ts"], "basis": basis if isinstance(basis, str) else None}
        elif row["kind"] in ("op-pin", "op-unpin"):
            pins[row["op"]] = row["kind"] == "op-pin"
        elif row["kind"] in ("op-open", "op-title"):
            data = _json(row["data"])
            if isinstance(data, dict) and isinstance(data.get("title"), str) and data["title"].strip():
                titles[row["op"]] = data["title"].strip()
        else:
            phase_events.setdefault(row["op"], []).append({"kind": row["kind"], "at": row["ts"], "data": _json(row["data"])})
    return pins, titles, phase_events, closes


def _phase_state(phases, events):
    """Opened and completed phases, the current phase, and the last door refusal since the last
    advance. A phase is complete only when a recorded advance leaves it."""
    opened, completed, door = set(), set(), None
    for event in events:
        data = event["data"] if isinstance(event["data"], dict) else {}
        if event["kind"] == "phase-enter" and isinstance(data.get("phase"), str):
            opened.add(data["phase"])
        elif event["kind"] == "phase-advance":
            source, _, target = str(data.get("boundary") or "").partition("->")
            completed.add(source); opened.update(p for p in (source, target) if p); door = None
        elif event["kind"] == "phase-door-closed":
            door = {"boundary": data.get("boundary"), "verdict": data.get("verdict"),
                    "reason": data.get("reason"), "at": event["at"]}
    order = phases + sorted(p for p in opened | completed if p not in phases)
    current = next((p for p in reversed(order) if p in opened and p not in completed), None)
    return {"opened": [p for p in order if p in opened], "completed": [p for p in order if p in completed],
            "current": current, "door": door}


def _event(row):
    data = _json(row["data"])
    return {**{k: row.get(k) for k in ("id", "ts", "kind", "actor", "session", "op", "ref")},
            "summary": _summary(row["kind"], data, row.get("ref")), "data": data}


def _page_size(value, default, maximum):
    try:
        return min(maximum, max(1, int(value)))
    except (TypeError, ValueError):
        return default


def _cursor(value, newest):
    if value is None or value == "":
        return newest + 1, newest
    try:
        if str(value).isdigit():
            before = int(value)
            return before, min(newest, before - 1)
        decoded = json.loads(base64.urlsafe_b64decode(str(value) + "=" * (-len(str(value)) % 4)))
        before, upper = decoded["before"], decoded["upper"]
        if type(before) is not int or type(upper) is not int or before < 1 or upper < 0:
            raise ValueError
        return before, upper
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise ValueError("invalid history cursor") from exc


#: the history counts kept per filter: {(day, hour): events} as of one watermark, so a read tops them up
#: with the events added since instead of counting the record again (the work filter's DISTINCT
#: subquery reads every event: 0.75 s on 216k, t-575 L9). HISTORY_KEEP filters are kept, newest used last.
HISTORY_KEEP = 32
_HISTORY_COUNTS: dict = {}
_HISTORY_LOCK = threading.Lock()
_HISTORY_KEY_LOCKS: dict = {}
#: the kinds the work filter counts only for a session that did work of its own
_SESSION_KINDS = ("session-start", "session-end", "init")


def _group_order(item):
    (day, hour), _ = item
    return (day or "", -1 if hour is None else hour)


def _history_flipped(conn, low, high, side_kinds):
    """True when an event added in (low, high] can change how an older event counts under the work
    filter: a session start, end or init event counts once its session has done work, so a session's
    first work event flips the ones it already had, and a new one needs its own session read."""
    marks = ",".join("?" * len(_SESSION_KINDS))
    if conn.execute("SELECT 1 FROM events WHERE id > ? AND id <= ? AND kind IN (%s) LIMIT 1" % marks,
                    (low, high) + _SESSION_KINDS).fetchone():
        return True
    sides = ",".join("?" * len(side_kinds))
    sessions = [r[0] for r in conn.execute(
        "SELECT DISTINCT session FROM events WHERE id > ? AND id <= ? AND kind NOT IN (%s) "
        "AND session IS NOT NULL" % sides, (low, high) + tuple(side_kinds))]
    for session in sessions:
        if conn.execute("SELECT 1 FROM events WHERE session = ? AND id <= ? AND kind IN (%s) LIMIT 1" % marks,
                        (session, low) + _SESSION_KINDS).fetchone() and not conn.execute(
                "SELECT 1 FROM events WHERE session = ? AND id <= ? AND kind NOT IN (%s) LIMIT 1" % sides,
                (session, low) + tuple(side_kinds)).fetchone():
            return True
    return False


def _history_groups(root, conn, filter_key, clauses, params, upper, side_kinds):
    """{(day, hour): matching events with id <= upper} for the filter `clauses` describe (everything
    but the day and the page). A kept count is topped up with the events in (kept watermark, upper]
    when none of them can change an older event's count (_history_flipped); the record's own event
    at the kept watermark must still hash the same, so a rebuilt record starts over. A read behind
    the kept watermark (an old page of a cursor) is counted afresh and kept for no one."""
    def count(extra, extra_params):
        return conn.execute("SELECT substr(ts,1,10), CAST(substr(ts,12,2) AS INTEGER), COUNT(*) FROM events "
                            "WHERE " + " AND ".join(clauses + extra) + " GROUP BY 1, 2",
                            list(params) + extra_params).fetchall()
    def marker(at):
        row = conn.execute("SELECT hash FROM events WHERE id = ?", (at,)).fetchone()
        return row[0] if row else None
    key = (str(root), filter_key, tuple(side_kinds or ()))
    with _HISTORY_LOCK:
        key_lock = _HISTORY_KEY_LOCKS.setdefault(key, threading.Lock())
    with key_lock:
        try:
            kept = _HISTORY_COUNTS.get(key)
            groups = None
            if kept is not None and kept[0] <= upper and kept[2] == marker(kept[0]):
                low = kept[0]
                if low == upper:
                    groups = kept[1]
                elif not (side_kinds and _history_flipped(conn, low, upper, side_kinds)):
                    groups = dict(kept[1])
                    for d, h, n in count(["id > ?"], [low]):
                        groups[(d, h)] = groups.get((d, h), 0) + n
            if groups is None:
                groups = {(d, h): n for d, h, n in count([], [])}
            if kept is None or kept[0] <= upper:
                with _HISTORY_LOCK:
                    _HISTORY_COUNTS.pop(key, None)
                    _HISTORY_COUNTS[key] = (upper, groups, marker(upper))
                    while len(_HISTORY_COUNTS) > HISTORY_KEEP:
                        old = next(iter(_HISTORY_COUNTS))
                        del _HISTORY_COUNTS[old]
                        _HISTORY_KEY_LOCKS.pop(old, None)
            return groups
        except BaseException:
            # a count that raised leaves nothing kept, so its lock goes too
            with _HISTORY_LOCK:
                if key not in _HISTORY_COUNTS:
                    _HISTORY_KEY_LOCKS.pop(key, None)
            raise


#: t-606: the most events one history page reads while folding runs of repeats (_fold_key); a longer
#: run goes on with the next page.
HISTORY_FOLD_SCAN = 5000


#: kinds whose every event is its own piece of work: never folded, however alike
_UNFOLDED = ("task-", "op-", "proof", "msg", "run", "receipt", "verdict", "phase-", "decision", "check",
             "review", "release")


def _fold_key(event):
    """The repeat key of an event, or None for one that never folds. A run of the same bookkeeping
    or failure kind from the same place (`where`/`hook`) is one row, whatever each one's message:
    one pre-compact or collector pass can append a hundred in the same second, and two failures
    retried in turn would otherwise split the run at every switch. The row shows the newest event;
    `variants` counts the distinct messages folded under it."""
    kind, data = event["kind"] or "", event["data"]
    if kind.startswith(_UNFOLDED):
        return None
    return json.dumps([kind, data.get("where") or data.get("hook")])


def _fold_into(items, event):
    """Fold `event` into the newest-first `items` when it repeats the last one's failure; returns
    True when folded. A folded item keeps its newest event and counts `repeat`, the oldest time
    `first_ts`, the distinct `sessions` and the distinct summaries `variants`."""
    key = _fold_key(event)
    if key is None or not items or items[-1].get("fold") != key:
        if key is not None:
            event.update(fold=key, repeat=1, first_ts=event["ts"], sessions=1, variants=1,
                         _sessions={event["session"]}, _variants={event["summary"]})
        return False
    last = items[-1]
    last["repeat"] += 1
    last["first_ts"] = event["ts"]
    last["_sessions"].add(event["session"])
    last["sessions"] = len(last["_sessions"])
    if len(last["_variants"]) < 100:        # a count past 100 says enough
        last["_variants"].add(event["summary"])
        last["variants"] = len(last["_variants"])
    return True


def history(root, before=None, limit=50, query="", kind="work", session="", ref="", day="", calendar=False):
    """Newest first, with a snapshot watermark carried inside the event-ID cursor.

    `day` (YYYY-MM-DD, the recorded local date) keeps one day. `calendar` adds `hours`: the
    matching events counted per recorded day and hour over the latest 14 days with any, for
    the Activity heatmap; it ignores `day` so the heatmap still shows every day."""
    if day and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(day)):
        raise ValueError("day must be YYYY-MM-DD")
    # One day is read whole (up to 1000 events) so its timeline is complete.
    root, limit = Path(root).resolve(), _page_size(limit, 50, 1000 if day else 200)
    with _record(root) as conn:
        newest = conn.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0] if conn else 0
        edge, upper = _cursor(before, newest)
        upper = min(upper, newest)      # a cursor from beyond the record counts what is there
        clauses, params = ["id <= ?"], [upper]
        work_kinds = ()
        if kind in ("work", "progress"):
            work_sql, work_kinds = _work_sql(root)
            clauses.append(work_sql)
            params.append(upper)
            params.extend(work_kinds)
            # A subagent starting or finishing is bookkeeping, not work.
            # A failure among them still shows; the all-events view keeps every one.
            clauses.append("(%s OR kind NOT IN (%s))" % (_FAILURE_SQL, ",".join("?" * len(_AGENT_KINDS))))
            params.extend(_AGENT_KINDS)
            if kind == "progress":
                # Progress is task, op, phase, proof and run movement: session bookkeeping, chat
                # traffic and unbound instrument diagnostics are left to the work and all views.
                clauses.append("(%s OR kind NOT IN (%s))" % (_FAILURE_SQL, ",".join("?" * len(_CHAT_KINDS))))
                params.extend(_CHAT_KINDS)
                clauses.append("NOT (kind = 'run' AND session = 'instrument' AND op IS NULL)")
        elif kind and kind != "all":
            clauses.append("kind = ?")
            params.append(kind)
        for field, value in (("session", session), ("ref", ref)):
            if value:
                clauses.append(field + " = ?")
                params.append(value)
        if query:
            clauses.append("instr(lower(kind||' '||actor||' '||COALESCE(ref,'')||' '||data),lower(?)) > 0")
            params.append(str(query))
        # The total and the calendar come from one count per (day, hour) of the filter, kept and
        # topped up with the events added since it was read (_history_groups).
        groups = _history_groups(root, conn, (kind, session, ref, str(query)), clauses, params, upper,
                                 work_kinds if kind in ("work", "progress") else None) if conn else {}
        hours = None
        if calendar and conn:
            keep = sorted({d for d, _ in groups})[-14:]
            hours = [{"day": d, "hour": h, "count": n} for (d, h), n in sorted(groups.items(), key=_group_order)
                     if d in keep]
        if day:
            clauses.append("substr(ts,1,10) = ?")
            params.append(str(day))
        where = " AND ".join(clauses)
        total = sum(n for (d, _), n in groups.items() if not day or d == str(day)) if conn else 0
        # Each read of the work filter costs about 0.1 s whatever its size, so a fold reads 1000 at a time.
        items, more, bound, scanned, size = [], False, edge, 0, max(limit + 1, 1000)
        while not more:
            batch = _rows(conn, "SELECT * FROM events WHERE " + where + " AND id < ? ORDER BY id DESC LIMIT ?",
                          params + [bound, size])
            for row in batch:
                event = _event(row)
                if not _fold_into(items, event):
                    if len(items) == limit:
                        more = True
                        break
                    items.append(event)
                bound, scanned = row["id"], scanned + 1
                if scanned >= HISTORY_FOLD_SCAN:
                    more = True     # a longer run continues on the next page
                    break
            if len(batch) < size:
                break
    cursor = None
    if more:
        payload = json.dumps({"before": bound, "upper": upper}, separators=(",", ":"))
        cursor = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    for item in items:
        item.pop("_sessions", None)
        item.pop("_variants", None)
    result = {"items": items, "total": total, "next_before": cursor,
              "has_more": more, "watermark": upper}
    if calendar:
        result["hours"] = hours or []
    return result


def _load(root, rel, errors=None):
    """One project-contained JSON metadata object, or None. Kept for profile hooks (runs,
    check, capture) that read their own metadata files the same bounded way."""
    path = _contained(root, rel)
    if path is None or not path.is_file():
        return None
    try:
        # These are metadata documents. Logs and transcripts never enter this reader.
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("metadata exceeds 8 MiB")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("metadata must be an object")
        return value
    except (OSError, ValueError) as exc:
        if errors is not None:
            errors.append({"path": path.relative_to(root).as_posix(), "error": str(exc)[:300]})
        return None


def runs(root):
    """Recorded runs for the Runs page, from the domain profile (alpaca/profile.py `runs`).
    Durable jobs and receipts, never inferred from process telemetry. Empty without a profile."""
    root = Path(root).resolve()
    data = profile.load(root).runs(root)
    data = data if isinstance(data, dict) else {}
    items = [i for i in data.get("items") or [] if isinstance(i, dict)] \
        if isinstance(data.get("items"), (list, tuple)) else []
    errors = [e for e in data.get("errors") or [] if isinstance(e, dict)] \
        if isinstance(data.get("errors"), (list, tuple)) else []
    total = data.get("total")
    return {"items": items, "total": total if isinstance(total, int) and not isinstance(total, bool) else len(items),
            "errors": errors}


def _tool_roots(root):
    """Tool installation directories: the profile's plus `tools`."""
    extra = profile.listed(profile.load(root).paths(), "tools")
    return tuple(str(p).strip("/") for p in extra) + ("tools",)


def _profile_documents(root):
    """Report roots the profile adds to the catalog; they list as runbooks."""
    return tuple(p.strip("/") for p in profile.listed(profile.load(root).paths(), "documents") if p.strip("/"))


def _proof_refs(root, conn):
    refs = {}
    for row in _rows(conn, "SELECT id,proof FROM tasks WHERE proof IS NOT NULL"):
        pointer = row["proof"]
        if pointer.startswith("local:"):
            rel = _relative(root, pointer[6:])
            if rel:
                refs[rel] = {"task": row["id"], "sealed": False}
    # Sealing is historical here: do not hash all retained evidence on every list read.
    for event in _rows(conn, "SELECT ref,data FROM events WHERE kind='proof-report' ORDER BY id"):
        rel = _relative(root, _json(event["data"]).get("path"))
        if rel:
            refs[rel] = {"task": event["ref"], "sealed": True}
    return refs


#: where the Library looks for runbooks besides the report roots; evidence copies are skipped.
_RUNBOOK_ROOTS = ("runbooks", "specs", "docs", "templates", "design")


def _is_runbook(path):
    """A runbook or a runbook guide: the file name says runbook (runbook.yaml, runbook-format.md)
    and it is not a copy kept as evidence."""
    return ("runbook" in path.name.lower() and path.suffix.lower() in (".md", ".yaml", ".yml")
            and "evidence" not in path.parts)


def _runbook_paths(root):
    for rel in _RUNBOOK_ROOTS + _profile_documents(root):
        base = _contained(root, rel)
        if base is None or not base.is_dir():
            continue
        for directory, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in _SKIP and not d.startswith(".") and d != "evidence"
                             and not d.endswith((".evidence", ".kept"))
                             and not (Path(directory) / d).is_symlink())
            yield from _folder_documents(root, directory, "runbooks")


def _parts_below(root, path):
    """`path.relative_to(root).parts` for a resolved `path` under the resolved `root`, by string:
    the catalog asks this for every file, and `PurePath.relative_to` walks `parents` each time
    (op-008 t-463, review minor M-1). ValueError when `path` is not below `root`, as before."""
    top, here = str(root).rstrip(os.sep) + os.sep, str(path)
    if not here.startswith(top):
        return Path(here).relative_to(root).parts      # the same answer or ValueError, the slow way
    return tuple(p for p in here[len(top):].split(os.sep) if p)


def _aar_reports(root, conn):
    """op-057 t-625: the AAR report path of every op on the record (alpaca/aar.py report_path); the
    reader opens these and no other file in .alpaca/aar."""
    from alpaca import aar
    return {Path(aar.report_path(str(root), r["id"])).relative_to(root).as_posix()
            for r in _rows(conn, "SELECT id FROM ops")}


def _document_allowed(root, path):
    """Catalog paths cannot alias private runtime state, the Rune2 vault (only its doors serve the
    withdrawn and quarantined captures it keeps as files, op-008 t-441) or evidence attachments."""
    if path is None or path.suffix.lower() not in (".md", ".html", ".htm") and not _is_runbook(path):
        return False
    if paths.in_vault(root, path):
        return False
    parts = _parts_below(root, path)
    if any(part.startswith(".") for part in parts[1:]):
        return False
    if any(part.endswith((".evidence", ".kept")) for part in parts):
        return False
    if parts[0].startswith("."):
        if parts[0] == ".alpaca" and len(parts) == 3 and parts[1] == "aar":
            return path.suffix.lower() == ".md"      # an op's AAR report; document() also needs the op on the record
        return parts[0] == ".alpaca" and len(parts) > 2 and parts[1] in ("proofs", "reports", "reviews", "wiki")
    return parts[0] not in _SKIP


def _open_document(root, path):
    """`path` opened as text for a catalog reader, or FileNotFoundError. The opened file's own path
    must still be a contained, allowed document (`paths.open_checked`), so a folder on the path
    that became a link after `_document_allowed` looked cannot lead the open into the vault or a
    private file (op-008 t-463, review R-3)."""
    top = str(root).rstrip(os.sep) + os.sep

    def allowed(real):                  # `real` is the kernel's path for the open file: no links
        return real.startswith(top) and _document_allowed(root, Path(real))
    fh = paths.open_checked(path, allowed)
    if fh is None:
        raise FileNotFoundError("No readable report at this path")
    return io.TextIOWrapper(fh, encoding="utf-8", errors="replace")


def document(root, path):
    """Read a catalog-authorized report, bounded independently from listing limits."""
    root = Path(root).resolve()
    target = _contained(root, path)
    if not _document_allowed(root, target) or not target.is_file():
        raise FileNotFoundError("No readable report at this path")
    tools = str(path).startswith(tuple(t + "/" for t in _tool_roots(root)))
    sig = _catalog_signature(root)
    allowed = target in _catalog_paths(root, "tools" if tools else "reports", sig)
    if not allowed and not tools:
        allowed = target in _catalog_paths(root, "runbooks", sig)
    if not allowed:
        with _record(root) as conn:
            rel = target.relative_to(root).as_posix()
            allowed = rel in _proof_refs(root, conn) or rel in _aar_reports(root, conn)
    if not allowed:
        raise FileNotFoundError("No readable report at this path")
    limit = 512 * 1024
    with _open_document(root, target) as handle:
        text = handle.read(limit + 1)
        size = os.fstat(handle.fileno()).st_size
    return {"path": target.relative_to(root).as_posix(), "text": text[:limit],
            "size": size, "truncated": len(text) > limit}


#: What one folder's files add to a walk, kept while the folder is unchanged (op-012 t-593): a walk
#: resolved 31k paths again for each new proof file (2.8 s on the live record) though one folder moved.
_FOLDER_MEMO = {}              # str(root) -> {(str(folder), kind): (key, [paths])}
_FOLDER_MEMO_MAX = 20000       # the live record has ~4,100 folders, two kinds each
_FOLDER_LOCK = threading.Lock()


def _folder_documents(root, directory, kind):
    """The allowed document paths among the files of `directory`, in name order, for a "docs" or a
    "runbooks" walk. Reused while the folder's `_stamp` and where each link among its files
    resolves are as they were; the listing, a retargeted link or a permission change moves the key.
    The stamp is taken before the folder is listed (t-593 review B1): a file written during the
    listing then moves the stamp past the stored one and the next read lists the folder again,
    where a stamp taken after the listing stored the old list under the new stamp."""
    stamp = _stamp(directory)
    try:
        with os.scandir(directory) as scan:
            entries = list(scan)
    except OSError:
        return []
    files = sorted(e.name for e in entries if not e.is_dir())
    key = (stamp, tuple(sorted((e.name, os.path.realpath(e.path)) for e in entries
                               if e.is_symlink() and not e.is_dir())))
    slot = (str(directory), kind)
    with _FOLDER_LOCK:
        mine = _FOLDER_MEMO.get(str(root), {})
        held = mine.get(slot)
        if held is not None and held[0] == key and stamp is not None:
            mine[slot] = mine.pop(slot)          # least recently used goes first (review N3)
            return held[1]
    found = []
    for name in files:
        path = _contained(root, Path(directory) / name)
        if kind == "docs":
            keep = _document_allowed(root, path) and path.suffix.lower() in (".md", ".html", ".htm")
        else:
            keep = path is not None and _is_runbook(path) and _document_allowed(root, path)
        if keep:
            found.append(path)
    with _FOLDER_LOCK:
        mine = _FOLDER_MEMO.setdefault(str(root), {})
        if slot not in mine and len(mine) >= _FOLDER_MEMO_MAX:
            for old in list(mine)[:len(mine) // 4]:      # the least recently used quarter, not the whole walk
                del mine[old]
        mine[slot] = (key, found)
        while len(_FOLDER_MEMO) > 8:
            _FOLDER_MEMO.pop(next(k for k in _FOLDER_MEMO if k != str(root)))
    return found


def _document_paths(root, tools=False):
    roots = _tool_roots(root) if tools else _REPORT_ROOTS + _profile_documents(root)
    seen = set()
    for rel in roots:
        base = _contained(root, rel)
        if base is None or not base.is_dir():
            continue
        for directory, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in _SKIP and not d.startswith(".")
                             and not d.endswith((".evidence", ".kept"))
                             and not (Path(directory) / d).is_symlink())
            for path in _folder_documents(root, directory, "docs"):
                if path not in seen:
                    seen.add(path)
                    yield path
    if not tools:
        for path in sorted(root.glob("*.md")):
            safe = _contained(root, path)
            if _document_allowed(root, safe) and safe not in seen:
                yield safe


#: The catalog walk is cached while the directories it reads are unchanged (op-012 t-582): a walk
#: resolves every file path (31k `Path.resolve` calls on the live record, 1.1 s) and the answer
#: only moves when a directory listing does. `_catalog_signature` is the cheap proof of that.
_CATALOG = {}                  # str(root) -> (signature, {"reports": [...], "tools": [...], "runbooks": [...]})
_CATALOG_LOCK = threading.Lock()
_TEXT_CACHE = {}               # str(root) -> {str(path): ((mtime_ns, ctime_ns, size, inode), title, summary)}
_TEXT_CACHE_MAX = 20000
_TEXT_LOCK = threading.Lock()


def _catalog_roots(root):
    """Every folder a catalog walk starts from, report, tool and runbook roots together."""
    rels = _REPORT_ROOTS + _profile_documents(root) + _tool_roots(root) + _RUNBOOK_ROOTS
    return tuple(dict.fromkeys(rels))


def _stamp(path):
    """A folder's change stamp: ctime too, because a permission change moves only ctime and the
    walk reads what the folder lets it list."""
    try:
        stat = os.stat(path)
        return stat.st_mtime_ns, stat.st_ctime_ns
    except OSError:
        return None


def _catalog_signature(root):
    """A hash of exactly what the catalog walks read: each root folder, the modification time of
    every folder the walks enter (the same pruning as `_document_paths`) and of the project root
    (its `*.md` files). A file added, removed or renamed changes its folder's time; an edit in
    place changes no listing, so the walk stays valid and `_cached_text` is keyed on the file.
    The walks store resolved paths, so every link entry of a folder is hashed with where it
    resolves now: a link (or a folder it passes through, outside every walked root) that is
    retargeted moves the signature."""
    parts = [_stamp(root)]

    def links(entries, only=None):
        return sorted((e.name, os.path.realpath(e.path)) for e in entries
                      if e.is_symlink() and (only is None or e.name.lower().endswith(only)))

    def enter(path):
        parts.append((path, _stamp(path)))
        try:
            with os.scandir(path) as scan:
                entries = list(scan)
        except OSError:
            return
        parts.append(links(entries))
        for sub in sorted(e.path for e in entries
                          if e.is_dir(follow_symlinks=False) and e.name not in _SKIP
                          and not e.name.startswith(".") and not e.name.endswith((".evidence", ".kept"))):
            enter(sub)
    try:
        with os.scandir(root) as scan:
            parts.append(links(list(scan), ".md"))      # the project root's own `*.md` links
    except OSError:
        pass
    for rel in _catalog_roots(root):
        base = _contained(root, rel)
        parts.append((rel, str(base)))
        if base is not None and base.is_dir():
            enter(str(base))
    return util.sha256_hex(util.canonical_json(parts))


def _catalog_paths(root, kind, sig):
    """The walked document paths of `kind` ("reports", "tools" or "runbooks"), from the cache while
    `sig` (a `_catalog_signature`) is unchanged. Callers still apply `_document_allowed` to every path."""
    walk = {"reports": lambda: _document_paths(root), "tools": lambda: _document_paths(root, tools=True),
            "runbooks": lambda: _runbook_paths(root)}[kind]
    key = str(root)
    with _CATALOG_LOCK:
        held = _CATALOG.get(key)
        if held is not None and held[0] == sig and kind in held[1]:
            return held[1][kind]
    found = list(walk())
    with _CATALOG_LOCK:
        held = _CATALOG.get(key)
        if held is None or held[0] != sig:
            held = _CATALOG[key] = (sig, {})
            if len(_CATALOG) > 8:
                _CATALOG.pop(next(iter(_CATALOG)))
        held[1][kind] = found
    return found


def _cached_text(root, path, stat):
    """`_document_text` for `path`, reused while its file is unchanged: modification and change
    time, size and inode (ctime moves on any write, even when mtime is put back)."""
    key = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
    with _TEXT_LOCK:
        held = _TEXT_CACHE.get(str(root), {}).get(str(path))
    if held is not None and held[0] == key:
        return held[1], held[2]
    title, summary = _document_text(root, path)
    # The bookkeeping runs under a lock: request threads evict other roots concurrently.
    with _TEXT_LOCK:
        mine = _TEXT_CACHE.setdefault(str(root), {})
        if len(mine) >= _TEXT_CACHE_MAX:
            mine.clear()                # only this project's titles; another root keeps its own
        while len(_TEXT_CACHE) > 8:
            _TEXT_CACHE.pop(next(k for k in _TEXT_CACHE if k != str(root)))
        mine[str(path)] = (key, title, summary)
    return title, summary


def _document_text(root, path):
    with _open_document(root, path) as handle:
        text = handle.read(16384)
    if path.suffix.lower() in (".yaml", ".yml"):
        # A runbook: its title or name key, else the folder it lives in; the summary names the
        # format and the goal when the file states them.
        keys = {}
        for line in text.splitlines():
            match = re.match(r"^(runbook|title|name|goal|summary):\s*(.+?)\s*(?:\s#.*)?$", line)
            if match and match.group(1) not in keys:
                keys[match.group(1)] = match.group(2).strip("'\"")
        title = keys.get("title") or keys.get("name") or "%s / %s" % (path.parent.name, path.name)
        summary = " / ".join(x for x in ("Runbook format " + keys["runbook"] if "runbook" in keys else "",
                                         keys.get("goal") or keys.get("summary") or "") if x)
        return title[:300], summary[:400]
    if path.suffix.lower() in (".html", ".htm"):
        match = re.search(r"<(?:title|h1)\b[^>]*>(.*?)</(?:title|h1)>", text, re.I | re.S)
        title = unescape(re.sub(r"<[^>]*>", "", match.group(1))).strip() if match else path.stem
        return title[:300], ""
    title = next((line.lstrip("# ").strip() for line in text.splitlines() if line.startswith("# ")), path.stem)
    summary = next((line.strip() for line in text.splitlines() if line.strip()
                    and not line.startswith(("#", "|", "---", "```", "<!--"))), "")
    return title[:300], summary[:400]


def documents(root, query="", category="reports", ext="", limit=100, offset=0):
    """Curated report catalog; dependency documentation requires category='tools'."""
    if category not in ("all", "reports", "proofs", "runbooks", "decisions", "tools"):
        raise ValueError("unknown document category")
    root = Path(root).resolve()
    limit, offset = _page_size(limit, 100, 200), max(0, int(offset))
    ext = str(ext).lower().lstrip(".")
    if ext == "markdown":
        ext = "md"
    if ext in ("yml",):
        ext = "yaml"
    if ext not in ("", "all", "md", "html", "htm", "yaml"):
        raise ValueError("unsupported document extension")
    with _record(root) as conn:
        refs = _proof_refs(root, conn)
    sig = _catalog_signature(root)
    paths = set(_catalog_paths(root, "tools" if category == "tools" else "reports", sig))
    runbook_roots = tuple(r + "/" for r in ("runbooks", "specs") + _profile_documents(root))
    if category != "tools":
        paths.update(path for rel in refs if (path := _contained(root, rel)) is not None)
        paths.update(_catalog_paths(root, "runbooks", sig))
    items = []
    for path in paths:
        if not _document_allowed(root, path):
            continue
        rel = path.relative_to(root).as_posix()
        suffix = path.suffix.lower().lstrip(".")
        if suffix == "yml":
            suffix = "yaml"
        if suffix not in ("md", "html", "htm", "yaml") or (ext not in ("", "all") and suffix != ext and not (ext == "html" and suffix == "htm")):
            continue
        bucket = ("tools" if category == "tools" else "proofs" if rel.startswith(".alpaca/proofs/") or rel in refs
                  else "decisions" if "decisions" in path.parts else "runbooks" if rel.startswith(runbook_roots) or _is_runbook(path) else "reports")
        if category not in ("reports", "all", "tools") and category != bucket:
            continue
        try:
            stat = path.stat()
            title, summary = _cached_text(root, path, stat)
        except OSError:
            continue
        work = refs.get(rel, {})
        if query and str(query).casefold() not in " ".join((rel, title, summary, work.get("task") or "")).casefold():
            continue
        items.append({"path": rel, "title": title, "category": bucket, "ext": suffix,
                      "size": stat.st_size, "mtime": stat.st_mtime, "summary": summary,
                      "task": work.get("task"), "sealed": work.get("sealed", False)})
    items.sort(key=lambda item: (-item["mtime"], item["path"]))
    total = len(items)
    return {"items": items[offset:offset + limit], "total": total,
            "next_offset": offset + limit if offset + limit < total else None, "has_more": offset + limit < total}


def _checked(root):
    """The profile's current, read-only stage check (alpaca/profile.py `check`).

    Returns (rows, error, checked_at). The check is a gate, so it is called strictly: a check
    that raises or answers with the wrong type reads as every profile stage BLOCKED with the
    reason, never as "no stages". Without a profile there are no rows and no error."""
    prof = profile.load(root)
    stamp = util.now_iso()
    try:
        rows = profile.strict(root, "check", root)
        return [dict(r) for r in rows if isinstance(r, dict)], None, stamp
    except Exception as exc:
        reason = ("Evidence check unavailable: %s" % exc).replace(str(root) + "/", "")
        stages = prof.stages()
        stages = stages if isinstance(stages, (list, tuple)) else ()
        return [{"stage": str(s), "verdict": "BLOCKED", "reason": reason} for s in stages], reason, stamp


def _tasks(root, conn):
    """Linked reports are catalog members, subject to the same private-path policy."""
    def read_report(pointer, seal):
        if not isinstance(pointer, str) or not pointer.startswith("local:"):
            return None
        path = _contained(root, pointer[6:])
        if not _document_allowed(root, path):
            return None
        try:
            if not path.is_file():
                return None
            with _open_document(root, path) as handle:
                content = handle.read(65537)
        except OSError:
            return None
        if seal and _contained(root, seal["data"].get("path")) != path:
            seal = None
        return work_record.report_sections(content[:65536], path.relative_to(root).as_posix(),
                                           seal, read_truncated=len(content) > 65536)
    return work_record.tasks(conn, read_report)


def _acceptance(root, conn, checked, stamp):
    """Acceptance outcomes from the profile (alpaca/profile.py `acceptance`); empty without one."""
    found = profile.load(root).acceptance(root, conn, checked, stamp)
    return [i for i in found if isinstance(i, dict)] if isinstance(found, (list, tuple)) else []


CHECKLIST_TITLES = "docs/checklist-titles.json"


def _checklist_titles(root):
    """Short display names for acceptance items, keyed by item id, for a profile's acceptance
    hook to use. The spec tables stay the source of the full statement; this file only names
    them. Absent reads as {}."""
    try:
        data = _json((Path(root) / CHECKLIST_TITLES).read_text(encoding="utf-8"))
    except OSError:
        return {}
    return {str(k): v.strip() for k, v in data.items() if isinstance(v, str) and v.strip()}


def acceptance_health(root):
    stages, error, checked_at = _checked(Path(root).resolve())
    return {"status": ("PASS" if stages and all(s["verdict"] == "PASS" for s in stages)
                       else "BLOCKED" if stages or error else "unavailable"), "checked_at": checked_at}


def _preservation_health(root, name):
    try:
        from importlib import import_module
        result = import_module("alpaca." + name).status(root)
        # Verification receipts describe a past check; never imply a current full rehash.
        return {key: value for key, value in result.items() if key in (
            "status", "verified_at", "ok", "completeness", "watermark", "verification_receipt")} | {
                "scope": "recorded_verification", "issues": len(result.get("issues", [])),
                "known_gaps": len(result.get("known_gaps", []))}
    except Exception as error:
        return {"status": "unavailable", "type": type(error).__name__, "scope": "recorded_verification"}


def capture_health(root):
    """Canonical collector facts. Unavailable is explicit during upgrades or failures."""
    try:
        from alpaca.observability import status
        facts = status(root)
        # The registry is private. HTTP carries identifiers, counters and watermarks only.
        sources = [{key: source[key] for key in (
            "source_id", "session", "parent_session", "provider", "kind", "required", "closed",
            "updated_at", "backlog_bytes") if key in source} for source in facts.get("sources", [])]
        consumers = [{key: consumer[key] for key in (
            "consumer", "event_id", "observation_id", "updated_at", "failures", "next_attempt")
            if key in consumer} for consumer in facts.get("consumers", [])]
        consumer_health = {row["consumer"]: {**row, "status": "error" if row.get("failures") else "checkpointed"}
                           for row in consumers}
        consumer_health.setdefault("wiki", {"status": "unavailable", "reason": "no verification checkpoint"})
        for name in ("artifacts", "backup"):
            consumer_health[name] = _preservation_health(root, name)
        return {**facts, "sources": sources, "consumers": consumers, "consumer_health": consumer_health,
                "collector": {key: facts.get("collector", {})[key] for key in (
                    "status", "heartbeat", "age_seconds", "liveness", "mode") if key in facts.get("collector", {})},
                "coverage": {key: value for key, value in facts.get("coverage", {}).items() if key != "sources"},
                "issues": [{key: issue[key] for key in (
                    "issue_id", "source_id", "code", "first_seen", "last_seen", "occurrences") if key in issue}
                           for issue in facts.get("issues", [])]}
    except Exception as error:
        return {"status": "unavailable", "collector": {"status": "unavailable"},
                "sources": [], "backlog": None, "consumers": {},
                "coverage": {"status": "unknown"},
                "issues": [{"kind": "health-read-failed", "type": type(error).__name__}]}


def capture_summary(root):
    """capture_health in counts (t-590), for the hub's 10 s vitals poll: the same status, collector
    and backlog, with coverage and issues counted (observability.summary never builds the source
    list). `issues.last_seen` feeds the health score and is not sent."""
    try:
        from alpaca.observability import summary
        facts = summary(root)
        return {**facts, "collector": {key: facts["collector"][key] for key in (
            "status", "heartbeat", "age_seconds", "liveness", "mode") if key in facts["collector"]}}
    except Exception as error:
        return {"status": "unavailable", "collector": {"status": "unavailable"}, "backlog": None,
                "coverage": {"status": "unknown"}, "issues": {"open": 0, "last_seen": []},
                "error": type(error).__name__}


def _capture(root, conn):
    """The profile's capture health (alpaca/profile.py `capture`), normalized. Empty without one."""
    try:
        cap = profile.load(root).capture(root, conn)
        cap = dict(cap) if isinstance(cap, dict) else {}
    except Exception as exc:
        cap = {"status": "error", "reason": "profile capture health unavailable: %s" % type(exc).__name__}
    cutoff = cap.get("cutoff", 0)
    if not isinstance(cutoff, int) or isinstance(cutoff, bool):
        cutoff = 0
    return cap, cutoff


def _research_states(conn):
    """op -> {"run", "state", "holds"} for every research run, for the roadmap's run cards. The
    rules of alpaca.research.state._state_of read from the run's own runner, resume, stop,
    finalize, attempt and hold events alone; the engine's full fold takes seconds per run on a busy
    record, too slow for a read every page makes. A ship hold counts as open until lifted."""
    from alpaca.research import events as rev
    kinds = (rev.RESUME, rev.RUNNER, rev.STOP, rev.FINALIZE, rev.ATTEMPT, rev.HOLD, rev.HOLD_LIFTED)
    by_run = {}
    for row in conn.execute("SELECT id, kind, ref, op, data FROM events WHERE kind IN (%s) ORDER BY id"
                            % ",".join("?" * len(kinds)), kinds):
        try:
            data = json.loads(row["data"] or "{}")
        except (TypeError, ValueError):
            data = {}
        # one op per run; attempt events carry the task as their ref, so the op groups them
        if row["op"]:
            by_run.setdefault(row["op"], []).append((row["id"], row["kind"], row["ref"], data if isinstance(data, dict) else {}))
    out = {}
    for op, evs in by_run.items():
        run = next((r for _, _, r, _ in evs if re.fullmatch(r"rr-\d+", str(r or ""))), None)
        if run is None:
            continue
        evs = [(i, k, r, d) for i, k, r, d in evs if k == rev.ATTEMPT or r == run]
        mark = max([i for i, k, _, _ in evs if k in (rev.RESUME, rev.RUNNER)] or [0])
        lifted = {d.get("hold") for _, k, _, d in evs if k == rev.HOLD_LIFTED}
        holds = sum(1 for i, k, _, _ in evs if k == rev.HOLD and i not in lifted)
        finals = [(i, d) for i, k, _, d in evs if k == rev.FINALIZE]
        if any(k == rev.STOP and d.get("final") and i > mark for i, k, _, d in evs):
            state = "stopped"
        elif finals and finals[-1][0] > mark:
            state = str(finals[-1][1].get("state") or "finished")
        elif holds:
            state = "held"
        elif any(k == rev.STOP and i > mark for i, k, _, _ in evs):
            state = "stopping"
        elif not any(k == rev.ATTEMPT for _, k, _, _ in evs):
            state = "starting"
        else:
            state = "running"
        out[op] = {"run": run, "state": state, "holds": holds}
    return out


def _page_capture(health):
    """Capture health for the overview page: the per-source registry (about 4,000 rows, a third of
    the payload on a busy project) is counted, not listed; /health.json and alpaca doctor keep the
    full list."""
    sources = health.get("sources")
    # t-591: the coverage child-session id lists (about 270 KB on a busy project, read by no page) are counted too
    coverage = health.get("coverage")
    if isinstance(coverage, dict):
        health = {**health, "coverage": {k: len(v) if k in ("missing_children", "unknown_children") and isinstance(v, list) else v
                                         for k, v in coverage.items()}}
    if not isinstance(sources, list):
        return health
    return {**health, "sources": [], "source_count": len(sources),
            "source_backlog_bytes": sum(s["backlog_bytes"] for s in sources if isinstance(s, dict) and isinstance(s.get("backlog_bytes"), (int, float)))}


#: How long a self-check packet or a research verification with no result yet counts as running.
#: Neither has a lease in the record, so an abandoned one stops counting after this (a check takes
#: minutes; the reviewflow lease of alpaca/runlog/reviewflow.py is 30 minutes too).
ARTHUR_OPEN_S = 30 * 60
#: The research-gate stages that run tests (alpaca/research gates): a FAIL here is a failed test run.
_TEST_STAGES = ("unit", "pass-to-pass", "fail-to-pass")
#: t-487: the event kinds that mark task or job activity. The newest one's time is `last_work`, from
#: which arthur.js tells how long nothing has moved (Arthur sleeps after 30 quiet minutes).
_WORK_KINDS = ("task-add", "task-move", "claim", "claim-release", "proof-scaffold", "proof-report", "run",
               "research-run-start", "research-attempt", "research-status")


def _epoch(stamp):
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(stamp)).timestamp()
    except (TypeError, ValueError):
        return None


def _max_id(conn, query, params=()):
    row = conn.execute(query, params).fetchone()
    return int(row[0] or 0) if row else 0


def _selfchecks_open(root, conn, open_ops, now):
    """Self-checks running: the newest packet of an open op's boundary that no `selfcheck` event
    names yet, built within ARTHUR_OPEN_S (`alpaca selfcheck brief` writes the packet, `submit`
    records the event; alpaca/selfcheck.py)."""
    from alpaca import selfcheck
    named = {d.get("packet") for d in (_json(r["data"]) for r in _rows(
        conn, "SELECT data FROM events WHERE kind=?", (selfcheck.KIND,)))}
    count = 0
    for op in open_ops:
        base = Path(paths.runtime_dir(str(root))) / "selfcheck" / op
        try:
            names = os.listdir(base)
        except OSError:
            continue
        newest = {}
        for name in names:
            m = re.fullmatch(r"(.+)-(\d+)", name)
            if m and int(m.group(2)) > newest.get(m.group(1), (0, ""))[0]:
                newest[m.group(1)] = (int(m.group(2)), name)
        for _, name in newest.values():
            try:
                packet = json.loads((base / name / "packet.json").read_text())
            except (OSError, ValueError):
                continue
            created = _epoch(packet.get("created")) if isinstance(packet, dict) else None
            if created is not None and now - created < ARTHUR_OPEN_S \
                    and (base / name).relative_to(root).as_posix() not in named:
                count += 1
    return count


def _arthur(root, conn, tasks, ops, research_runs):
    """What Arthur (alpaca/web/arthur.js) acts out, read from the record. Looping states are counts;
    one-shot reactions are the newest event id of each kind, so the page reacts when an id grows
    between two reads. Every value names its source; nothing is read from free text."""
    now = _epoch(util.now_iso()) or 0
    open_ops = {o["id"] for o in ops if o["status"] == "open"}
    live = [t for t in tasks if t.get("status") == "doing" and t.get("claimant")
            and (_epoch(t.get("lease_until")) or 0) > now]
    from alpaca import batch, brief, orders
    from alpaca.research import events as rev
    # waiting: the owner's open batch items (the newest batch-item event per item wins, batch.py),
    # the newest presented brief of an op with no brief-signed for its sha (brief.py), open orders
    items = {}
    for row in _rows(conn, "SELECT op,data FROM events WHERE kind=? ORDER BY id", (batch.ITEM_KIND,)):
        data = _json(row["data"])
        if row["op"] in open_ops and data.get("item"):
            items[(row["op"], data["item"])] = data.get("state")
    asked = {}
    for row in _rows(conn, "SELECT op,ref FROM events WHERE kind='owner-ask' AND ref LIKE 'brief:%' ORDER BY id"):
        if row["op"] in open_ops:
            asked[row["op"]] = str(row["ref"]).rsplit(":", 1)[-1]
    signed = {(r["op"], r["ref"]) for r in _rows(conn, "SELECT op,ref FROM events WHERE kind=?", (brief.SIGNED_KIND,))}
    # reading: a proof scaffold of a task in flight with no proof-report after it (proof.py); a log
    # review assigned under a live lease with no decision (runlog/reviewflow.py, the kinds a domain
    # profile names as review_flow_kinds); a research verification opened with no end yet
    last_proof = {}
    for row in _rows(conn, "SELECT kind,ref FROM events WHERE kind IN ('proof-scaffold','proof-report') ORDER BY id"):
        last_proof[row["ref"]] = row["kind"]
    flow_kinds = [str(k) for k in profile.listed(profile.load(root).events(), "review_flow_kinds")]
    reviews, no_go = 0, 0
    if flow_kinds:
        marks = ",".join("?" * len(flow_kinds))
        newest = {}
        for row in _rows(conn, "SELECT id,ref,data FROM events WHERE kind IN (%s) ORDER BY id" % marks, flow_kinds):
            newest[row["ref"]] = _json(row["data"])
        reviews = sum(1 for d in newest.values() if not d.get("decision") and not d.get("failure")
                      and isinstance(d.get("assignment"), dict)
                      and (d["assignment"].get("expires_at") or 0) > now)
        no_go = _max_id(conn, "SELECT MAX(id) FROM events WHERE kind IN (%s) AND json_valid(data) AND "
                              "json_extract(data,'$.decision.value')='NO-GO'" % marks, flow_kinds)
    verifying = {}
    for row in _rows(conn, "SELECT kind,ts,data FROM events WHERE kind IN (?,?) ORDER BY id",
                     (rev.VERIFY_OPEN, rev.VERIFY_END)):
        key = _json(row["data"]).get("verification")
        if row["kind"] == rev.VERIFY_OPEN:
            verifying[key] = _epoch(row["ts"]) or 0
        else:
            verifying.pop(key, None)
    reviews += sum(1 for at in verifying.values() if now - at < ARTHUR_OPEN_S)
    verdict = "CASE WHEN json_valid(data) THEN json_extract(data,'$.verdict') END"
    tests = " OR ".join("json_extract(data,'$.stage') LIKE '%s%%'" % s for s in _TEST_STAGES)
    count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
    # t-487: when each active research run started (the research-run-start event of its run id,
    # research/view.py "started_at"), for the long-run headphones; the newest task or job activity
    active_runs = {r.get("run") for r in research_runs.values() if r.get("state") in ("starting", "running", "stopping")}
    started = {}
    for row in _rows(conn, "SELECT ref,ts FROM events WHERE kind='research-run-start' ORDER BY id"):
        if row["ref"] in active_runs:
            started.setdefault(row["ref"], _epoch(row["ts"]))
    newest_work = _rows(conn, "SELECT ts FROM events WHERE id=(SELECT MAX(id) FROM events WHERE kind IN (%s))"
                        % ",".join("?" * len(_WORK_KINDS)), _WORK_KINDS)
    return {
        "waiting": {"questions": sum(1 for s in items.values() if s != batch.ANSWERED),
                    "briefs": sum(1 for op, sha in asked.items() if (op, sha) not in signed),
                    "orders": sum(1 for o in orders.open_orders(conn) if o["op"] in open_ops)},
        "thinking": {"design": sum(1 for t in live if t.get("phase") == "design"),
                     "selfchecks": _selfchecks_open(root, conn, open_ops, now)},
        "reading": {"proofs": sum(1 for t in live if last_proof.get(t["id"]) == "proof-scaffold"),
                    "reviews": reviews},
        "research": sum(1 for r in research_runs.values() if r.get("state") in ("starting", "running", "stopping")),
        # t-487: verify-phase tasks in flight (checklist), the epoch each active research run started
        # (headphones after 30 minutes), the epoch of the newest task or job activity (sleeping)
        "verify": sum(1 for t in live if t.get("phase") == "verify"),
        "research_started": sorted(at for at in started.values() if at is not None),
        "last_work": _epoch(newest_work[0]["ts"]) if newest_work else None,
        "sessions": int(count[0]) if count else 0,
        # one-shot reactions: the newest event id of each, 0 when the record has none
        "events": {
            "op_closed": _max_id(conn, "SELECT MAX(id) FROM events WHERE kind='op-close'"),
            # gates: the census row every instrument writes (gates/verdict.py emit_verdict) and the
            # door self-check (selfcheck.py); a closed phase door is a gate FAIL (phase-door-closed)
            "gate_pass": _max_id(conn, "SELECT MAX(id) FROM events WHERE kind IN ('run','selfcheck') AND %s='PASS'" % verdict),
            "gate_fail": _max_id(conn, "SELECT MAX(id) FROM events WHERE (kind IN ('run','selfcheck') AND %s='FAIL')"
                                       " OR kind='phase-door-closed'" % verdict),
            "no_go": no_go,
            "tests_failed": _max_id(conn, "SELECT MAX(id) FROM events WHERE kind=? AND %s='FAIL' AND (%s)"
                                          % (verdict, tests), (rev.GATE,)),
            # t-487: a proof sealed (proof.py writes proof-report on `alpaca proof seal`); a release
            # build that passed (release/receipts.py writes release-<kind> with the receipt status);
            # a guard or a gate that blocked: the research guard denied a call (hooks/research_guard.py,
            # research-guard-deny) or a gate's census row or self-check said BLOCKED (gates/verdict.py).
            # The git guard hook never opens the record; each deny appends a line to
            # .alpaca/git-guard/denials.jsonl, and git_blocked is that file's size, which grows per deny.
            "proof_sealed": _max_id(conn, "SELECT MAX(id) FROM events WHERE kind='proof-report'"),
            "release_built": _max_id(conn, "SELECT MAX(id) FROM events WHERE kind='release-build' AND "
                                           "json_valid(data) AND json_extract(data,'$.status')='PASS'"),
            "blocked": _max_id(conn, "SELECT MAX(id) FROM events WHERE kind=? OR (kind IN ('run','selfcheck') "
                                     "AND %s='BLOCKED')" % verdict, (rev.GUARD_DENY,)),
            "git_blocked": _file_size(os.path.join(root, ".alpaca", "git-guard", "denials.jsonl")),
        },
    }


def _file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _jobs_started(run_data, active):
    """The epoch each active job started (a profile run item's "started_at", alpaca/profile.py
    `runs`: an ISO stamp or epoch seconds), oldest first; Arthur drinks coffee past one hour."""
    out = []
    for item in run_data["items"]:
        if item.get("id") not in active:
            continue
        at = item.get("started_at")
        at = float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else _epoch(at) if at else None
        if at is not None:
            out.append(at / 1000 if at > 1e12 else at)
    return sorted(out)


def party_days(settings):
    """The days Arthur wears his party hat (t-487, brief 2e): project.yaml `studio: {party_days:
    ["YYYY-MM-DD", ...]}`, optional and empty by default. YAML reads an unquoted date as a date, so
    both forms count; anything that is not a real calendar day is left out."""
    from datetime import date
    studio = settings.get("studio") if isinstance(settings, dict) else None
    days = studio.get("party_days") if isinstance(studio, dict) else None
    out = []
    for day in days if isinstance(days, (list, tuple)) else ():
        text = day.isoformat() if isinstance(day, date) else str(day).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            try:
                date.fromisoformat(text)
            except ValueError:
                continue
            if text not in out:
                out.append(text)
    return sorted(out)


def _main_head(root):
    """{"head", "merge"} of the main branch (alpaca/gitview.py), or None outside a git checkout.
    `merge` is true when the head commit has more than one parent: a merge landed on main."""
    try:
        from alpaca import gitview
        line = gitview.git(str(root), "rev-list", "--parents", "-n", "1", gitview.main_branch(str(root)),
                           "--").split()
    except Exception:
        return None
    return {"head": line[0], "merge": len(line) > 2} if line else None


#: t-591: the task fields /hub/overview.json leaves out (two thirds of its size): the text a task
#: shows is read from /hub/tasks.json for the tasks a view opens.
TASK_TEXT = ("report", "history", "contract", "description", "purpose", "last_completion", "reason")
#: Task fields no page reads (completed_actor, completion_event_id) or that the page derives from the id
#: (number, ref).
_TASK_DROPPED = ("completed_actor", "completion_event_id", "number", "ref")


def text_rev(task):
    """A short hash of exactly the TASK_TEXT fields as the overview computed them. The page keeps a
    task's text by (id, text_rev), so any change to the text changes the rev, whatever event
    caused it (a contract, a re-sealed report, an op's done_when behind a purpose)."""
    return hashlib.sha1(json.dumps([task.get(k) for k in TASK_TEXT], sort_keys=True, default=str,
                                   separators=(",", ":")).encode("utf-8")).hexdigest()[:10]


def _epoch(value):
    """Seconds since the epoch of an ISO time, 0 when absent or unreadable (the page's Date.parse(x)||0)."""
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return 0.0


def _inline_text_ids(tasks):
    """The ids whose text the default views draw: the cockpit's assignment card, its three latest
    completions and every claimed (doing) task, and the overview page's two tasks in focus
    (cockpit.js cockpitTaskIds, hub.js overview()). They carry their text in the slim overview,
    so those tabs make one request."""
    # deferred work is not in focus: the deferral patches updated_at, so the task just parked would
    # otherwise be the newest (op-057 t-617).
    open_ = [t for t in tasks if t.get("status") not in ("done", "deferred", "cancelled", "canceled")]
    focus = sorted(open_, key=lambda t: (bool(t.get("claimant")), str(t.get("updated_at") or t["id"])), reverse=True)[:1]
    latest = sorted((t for t in tasks if t.get("status") == "done"),
                    key=lambda t: _epoch(t.get("completed_at")), reverse=True)[:3]
    watched = [t for t in open_ if t.get("status") == "doing" or t.get("claimant")][:2]
    return {t["id"] for t in focus + latest + watched + [t for t in tasks if t.get("status") == "doing"]}


def _present(value):
    return value is not None and value != [] and value != ""


def page_overview(full):
    """`overview()` as /hub/overview.json carries it: every task without its TASK_TEXT fields (but
    with their text_rev, and the text itself for _inline_text_ids), without keys that hold nothing
    (the page reads a missing key as empty), and no message list (/hub/tasks.json and
    /hub/messages.json serve those). The CLI and other callers keep the whole `overview()`."""
    tasks = full.get("tasks", [])
    inline = _inline_text_ids(tasks)
    slim = []
    for t in tasks:
        row = {k: v for k, v in t.items() if k not in TASK_TEXT and k not in _TASK_DROPPED and _present(v)}
        row["text_rev"] = t.get("text_rev") or text_rev(t)
        if t["id"] in inline:
            row["text"] = {k: t.get(k) for k in TASK_TEXT}
        slim.append(row)
    page = {**{k: v for k, v in full.items() if k != "messages"}, "tasks": slim}
    if isinstance(page.get("ops"), list):
        page["ops"] = [{k: v for k, v in op.items() if _present(v)} for op in page["ops"]]
    plan = page.get("roadmap")
    if isinstance(plan, dict) and isinstance(plan.get("operations"), list):
        page["roadmap"] = {**plan, "operations": [
            {**op, "groups": [{**g, "nodes": [{k: v for k, v in n.items() if _present(v)} for n in g.get("nodes") or []]}
                              for g in op.get("groups") or []]} if isinstance(op, dict) and "groups" in op else op
            for op in plan["operations"]]}
    return page


def overview(root):
    root = Path(root).resolve()
    checked, check_error, stamp = _checked(root)
    run_data = runs(root)
    with _record(root) as conn:
        cap, cap_cutoff = _capture(root, conn)
        resolved_ops = profile.listed(cap, "resolved_ops")
        error_kinds = profile.listed(cap, "error_kinds")
        tasks = _tasks(root, conn)
        from alpaca import taskplan
        roadmap = taskplan.project(conn, tasks, root=root, stages=checked)
        pins, titles, phase_events, closes = _op_states(conn)
        from alpaca.posture import level as posture_level
        # the autodrive level of every session that holds or worked a task, for the map and cards
        held = {t.get("assigned_session") for t in tasks} | {
            n.get("session") for op in (roadmap or {}).get("operations") or []
            for g in op.get("groups") or [] for n in g.get("nodes") or []}
        try:
            levels = posture_level.levels(conn, held, root=root)
        except Exception:
            levels = {}
        try:
            research_runs = _research_states(conn)
        except Exception:
            research_runs = {}
        ops = [{"id": row["id"], "title": row["intent"], "status": row["status"], "done_when": row["done_when"],
                "tasks_total": sum(t["op"] == row["id"] for t in tasks),
                "tasks_done": sum(t["op"] == row["id"] and t["status"] == "done" for t in tasks),
                "pinned": pins.get(row["id"], False), "short_title": titles.get(row["id"]), "phases": _phases(row),
                "phase_state": _phase_state(_phases(row), phase_events.get(row["id"], [])),
                "opened": row["opened"], "closed": row["closed"],
                "close_basis": (closes.get(row["id"]) or {}).get("basis") if row["status"] == "closed" else None}
               for row in _rows(conn, "SELECT * FROM ops ORDER BY opened DESC,id")]
        from alpaca import aarview
        try:
            aar_states, aar_marks = aarview.project(conn, root, ops)
        except Exception as exc:          # op-057 t-625: the page stays up and each op says the read failed
            aar_states, aar_marks = {op["id"]: {"error": aarview.failed("AAR state", exc)} for op in ops}, {}
        for op in ops:
            if op["id"] in aar_states:
                op["aar"] = aar_states[op["id"]]
        for t in tasks:
            if t["id"] in aar_marks:
                t["aar_overdue"] = aar_marks[t["id"]]
        messages = [{**{k: row[k] for k in ("id", "ts", "session", "sender", "kind", "body", "ref")}, "to": row["to_"]}
                    for row in _rows(conn, "SELECT * FROM messages ORDER BY id DESC")]
        recovered = _rows(conn, "SELECT * FROM events WHERE kind='wiki-recovered' ORDER BY id DESC LIMIT 1")
        recovery = _event(recovered[0])['data'] if recovered else {}
        wiki_cutoff = recovery.get('through_event', 0) if recovery.get('status') == 'ok' else 0
        if not isinstance(wiki_cutoff, int) or (recovered and wiki_cutoff >= recovered[0]['id']):
            wiki_cutoff = 0
        # Resolve only failures covered by successful capture. Filter before LIMIT
        # so a flood of old, resolved failures cannot conceal another broken collector.
        # A profile's capture covers its own ops' capture failures up to its cutoff and may name
        # extra error kinds; without a profile every capture failure stays visible.
        op_clause = ("COALESCE(op,'') NOT IN (%s) OR id>?" % ",".join("?" * len(resolved_ops))
                     if resolved_ops else "1")
        kind_clause = ("(kind IN (%s) AND id>?) OR" % ",".join("?" * len(error_kinds))
                       if error_kinds else "")
        params = [wiki_cutoff] + (resolved_ops + [cap_cutoff] if resolved_ops else []) \
            + (error_kinds + [cap_cutoff] if error_kinds else [])
        errors = [_event(row) for row in _rows(conn, """SELECT * FROM events WHERE
            (kind='drain-failed' AND (id>? OR NOT CASE WHEN json_valid(data) THEN
                COALESCE(json_extract(data,'$.component'),'')='wiki' OR
                COALESCE(json_extract(data,'$.where'),'')='wiki-recover' OR
                COALESCE(json_extract(data,'$.error'),'') LIKE 'LedgerLockTimeout:%%'
                ELSE 0 END)) OR
            (kind='capture-failed' AND (%s)) OR
            %s
            kind IN ('analytics-failed','analytics-build-failed')
            ORDER BY id DESC LIMIT 20""" % (op_clause, kind_clause), params)]
        errors = [{**{k: row[k] for k in ("id", "ts", "kind", "summary", "session", "ref")},
                   "recovery": row["data"].get("recovery")} for row in errors]
        # the hooks' note that no collector captures (A-8, `capture-no-collector`; t-070 round 3,
        # check round 2 I2-4): the newest one since the last whole recovery, beside the errors as a
        # state line. It never makes the capture status an error: nothing failed, nothing captures.
        states = [{**{k: row[k] for k in ("id", "ts", "kind", "summary", "session", "ref")},
                   "state": True, "recovery": "bin/alpaca collect watch"}
                  for row in (_event(r) for r in _rows(conn, """SELECT * FROM events WHERE
                      kind='capture-no-collector' AND id>? ORDER BY id DESC LIMIT 1""", [wiki_cutoff]))]
        items = _acceptance(root, conn, checked, stamp)
        newest = _rows(conn, "SELECT ts,id FROM events ORDER BY id DESC LIMIT 1")
        try:
            arthur = _arthur(root, conn, tasks, ops, research_runs)
        except Exception:
            # Arthur is decoration: a record too old for one of these reads leaves him on the
            # counts the page already has, never fails the overview every page reads
            arthur = None
    stages = [{"stage": result.get("stage"), "status": result.get("verdict"),
               "recorded_status": result.get("recorded_status"), "reason": result.get("reason"),
               "recorded_at": result.get("recorded_at"), "recorded_session": result.get("recorded_session"),
               "receipt_id": result.get("receipt_id"), "verified_at": stamp} for result in checked]
    extra_errors = cap.get("errors") if isinstance(cap.get("errors"), (list, tuple)) else ()
    errors.extend({"kind": "capture-health", "summary": str(e.get("error") or e.get("summary") or ""),
                   "ref": e.get("path") or e.get("ref")} for e in extra_errors if isinstance(e, dict))
    cap_block = {k: v for k, v in cap.items() if k not in ("resolved_ops", "error_kinds", "errors")}
    if check_error:
        errors.append({"kind": "evidence-check", "summary": check_error, "recovery": "alpaca doctor"})
    errors.extend({"kind": "run-metadata", "summary": e.get("error"), "ref": e.get("path")}
                  for e in run_data["errors"] if isinstance(e, dict))
    running_receipts = {stage.get("receipt_id") for stage in checked if stage.get("verdict") in ("RUNNING", "QUEUED")}
    active = [run["id"] for run in run_data["items"] if run.get("verdict") in ("RUNNING", "QUEUED")
              and any(stage.get("receipt_id") in running_receipts for stage in run.get("stages") or [])]
    name = root.name
    settings = {}
    config = _contained(root, "project.yaml")
    if config is not None and config.is_file():
        try:
            import yaml
            settings = yaml.safe_load(config.read_text()) or {}
            settings = settings if isinstance(settings, dict) else {}
            name = settings.get("name") or name
        except (OSError, ValueError, yaml.YAMLError):
            pass
    cap_status = str(cap_block.get("status") or "ok")
    try:
        from alpaca import version
        release = version.info(root)
    except Exception:
        release = None
    return {"project": {"name": name}, "alpaca": release, "as_of": newest[0]["ts"] if newest else None,
            "profile": profile.web_meta(root),
            "tasks": tasks, "ops": ops, "items": items, "stages": stages, "roadmap": roadmap,
            "observability": _page_capture(capture_health(root)),
            "acceptance": {"status": ("PASS" if stages and all(s["status"] == "PASS" for s in stages)
                                      else "BLOCKED" if stages else "unavailable")},
            "capture": {"status": "error" if errors or cap_status == "error" else cap_status,
                        "profile": cap_block, "errors": errors, "states": states,
                        "wiki_recovery": recovery},
            "messages": messages, "active_jobs": active, "levels": levels, "research_runs": research_runs,
            "arthur": dict(arthur, main=_main_head(root), jobs_started=_jobs_started(run_data, active),
                           party_days=party_days(settings)) if arthur is not None else None,
            "counts": {"tasks": len(tasks), "items": len(items), "items_pass": sum(i.get("status") == "PASS" for i in items),
                       "items_blocked": sum(i.get("status") == "BLOCKED" for i in items),
                       "items_fail": sum(i.get("status") == "FAIL" for i in items), "active_jobs": len(active)}}


# ------------------------------------------------------------------ the Records page
RECORD_SECTIONS = ("ops", "decisions", "proofs")
_TASK_ID = re.compile(r"t-\d+[a-z]?")
_OP_ID = re.compile(r"op-\d+")


def _proof_path(pointer):
    """The project path of a task's `local:` proof report under .alpaca/proofs, or None."""
    text = str(pointer or "").strip()
    if not text.startswith("local:"):
        return None
    rel = text[len("local:"):].strip()
    parts = Path(rel).parts
    if not rel.endswith(".md") or len(parts) < 3 or parts[:2] != (".alpaca", "proofs") or ".." in parts:
        return None
    return rel


def _decision(row):
    """A decision row with its body's recorded parts (context, options, choice, consequence,
    pointer) lifted out; a body that is not that JSON object stays as its text."""
    body = _json(row.get("body"))
    parts = body if isinstance(body, dict) else {}
    options = parts.get("options")
    return {"id": row["id"], "ts": row["ts"], "session": row["session"], "actor": row["actor"],
            "kind": row["kind"], "ref": row["ref"],
            "context": parts.get("context") if isinstance(parts.get("context"), str) else None,
            "options": [str(o) for o in options] if isinstance(options, list) else [],
            "choice": parts.get("choice") if isinstance(parts.get("choice"), str) else None,
            "consequence": parts.get("consequence") if isinstance(parts.get("consequence"), str) else None,
            "pointer": parts.get("pointer") if isinstance(parts.get("pointer"), str) else None,
            "text": None if parts else str(row.get("body") or "")}


def records(root, section="ops", query=None, limit=200, offset=0):
    """The official records, read-only from the record (connect_readonly): every operation with its
    open and close, the recorded decisions, and the sealed proofs of done tasks. `query` keeps the
    rows whose text holds it (case-insensitive); `limit` and `offset` page them. The counts name the
    size of every section, so the page shows them on its tabs."""
    section = section if section in RECORD_SECTIONS else "ops"
    limit = _page_size(limit, 200, 500)
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        offset = 0
    needle = str(query or "").strip().lower()
    with _record(root) as conn:
        pins, titles, _phases_events, closes = _op_states(conn)
        tasks = _rows(conn, "SELECT id,op,title,statement,status,proof,updated FROM tasks")
        ops = []
        for row in _rows(conn, "SELECT * FROM ops ORDER BY opened DESC,id"):
            mine = [t for t in tasks if t["op"] == row["id"]]
            ops.append({"id": row["id"], "title": row["intent"], "short_title": titles.get(row["id"]),
                        "status": row["status"], "done_when": row["done_when"], "opened": row["opened"],
                        "closed": row["closed"], "pinned": pins.get(row["id"], False),
                        "close_basis": (closes.get(row["id"]) or {}).get("basis") if row["status"] == "closed" else None,
                        "tasks_total": len(mine), "tasks_done": sum(t["status"] == "done" for t in mine)})
        decisions = [_decision(row) for row in
                     _rows(conn, "SELECT id,ts,session,actor,kind,body,ref FROM decisions ORDER BY ts DESC,rowid DESC")]
        proofs = sorted(({"task": t["id"], "op": t["op"], "title": t["title"] or t["statement"],
                          "completed": t["updated"], "pointer": t["proof"], "path": _proof_path(t["proof"])}
                         for t in tasks if t["status"] == "done" and t["proof"]),
                        key=lambda p: (str(p["completed"] or ""), p["task"]), reverse=True)
    rows = {"ops": ops, "decisions": decisions, "proofs": proofs}
    counts = {name: len(items) for name, items in rows.items()}
    items = rows[section]
    if needle:
        items = [item for item in items
                 if needle in " ".join(str(v) for v in item.values() if v is not None).lower()]
    page = items[offset:offset + limit]
    return {"section": section, "query": needle or None, "total": len(items), "counts": counts,
            "items": page, "next_offset": offset + limit if offset + limit < len(items) else None}


_WIKI_STATUS_CACHE: dict = {}
WIKI_STATUS_TTL_S = 60


def wiki_status(root, *, now=None):
    """What the project wiki holds and whether it serves, from alpaca.wiki.service.status (read-only:
    it creates no store). Only the summary the Records page shows leaves here: no provider settings,
    switches or paths. The status reads the whole store (10 s on a 60 GB vault, 2026-10-11), so one
    reading serves every request for WIKI_STATUS_TTL_S seconds."""
    import time
    from alpaca.wiki import service
    key, clock = str(Path(root).resolve()), time.monotonic() if now is None else now
    cached = _WIKI_STATUS_CACHE.get(key)
    if cached and clock - cached[0] < WIKI_STATUS_TTL_S:
        return cached[1]
    try:
        full = service.status(root)
    except Exception as exc:                                # a broken store is shown, not raised
        value = {"available": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    else:
        capture = full.get("capture") if isinstance(full.get("capture"), dict) else {}
        value = {"available": True, "serving": full.get("serving"), "corpus": full.get("corpus") or {},
                 "knowledge_time": full.get("knowledge_time"), "schema_version": full.get("schema_version"),
                 "quarantined_captures": len(full.get("quarantined_captures") or []),
                 "unmatched_proofs": len(full.get("unmatched_proofs") or []),
                 "capture": {k: capture.get(k) for k in ("state", "status", "captured", "expected", "missing")
                             if k in capture}}
    _WIKI_STATUS_CACHE[key] = (clock, value)
    return value


#: A briefing is served from the one built before while its inputs move, and rebuilt once in the
#: background (stale while revalidate, as the Version control view). A reader is served a build at
#: most WIKI_BRIEFING_MAX_AGE_S old, counted from when that build began reading its inputs; past
#: that the reader waits for a fresh build, like a first read, and a build that fails raises. So a
#: briefing on screen is at most that old (plus the time between the page's reads), however long the
#: record was quiet or a rebuild kept failing; while the record or the store moves it is at most one
#: rebuild (about 3 s) plus WIKI_BRIEFING_TTL_S behind. The collector writes the store every few
#: seconds, so the store files are part of the key only to start that rebuild, never to make a
#: reader wait. The age bound covers what lives outside the record (the transcripts it checks for).
WIKI_BRIEFING_TTL_S = 30
WIKI_BRIEFING_MAX_AGE_S = 60            # the TTL and the time one build may take
WIKI_BRIEFING_MIN_GAP_S = 10            # at most one background build per subject this often: the
                                        # collector moves the store every few seconds, and each build
                                        # reads the whole store
WIKI_BRIEFING_KEEP = 64
#: events that do not change a briefing: a heartbeat or a failed drain lands every few seconds, and
#: the lag of the coverage gaps they could move is the bound above. A transcript snapshot is not
#: here: the missing transcripts the briefing lists come from it (wiki/service.py).
_BRIEFING_QUIET_KINDS = ("heartbeat", "drain-failed", "turn-end", "style-rearm",
                         "transcript-version", "subagent-start", "subagent-stop")
_WIKI_BRIEFING_CACHE: dict = {}          # key -> (build began (monotonic), signature, value)
_WIKI_BRIEFING_BUILDS: dict = {}         # key -> the thread building it
_WIKI_BRIEFING_LOCK = threading.Lock()


def _wiki_briefing_sig(root):
    """What a briefing reads: the record's countable events, sessions with their transcripts and
    registered sources, and the wiki store files (size and nanosecond mtime of the store and its
    write-ahead log). None when unreadable."""
    from alpaca.wiki.config import Config
    try:
        conn = db.connect_readonly(root)
    except Exception:
        return None
    try:
        head = tuple(conn.execute(
            "SELECT COALESCE((SELECT id FROM events WHERE kind NOT IN (%s) ORDER BY id DESC LIMIT 1), 0),"
            " (SELECT COUNT(*) FROM sessions),"
            " (SELECT COUNT(*) FROM sessions WHERE transcript IS NOT NULL AND transcript <> ''),"
            " (SELECT COALESCE(MAX(id), 0) FROM decisions)"
            % ",".join("?" * len(_BRIEFING_QUIET_KINDS)), _BRIEFING_QUIET_KINDS).fetchone())
        try:
            head += (conn.execute("SELECT COUNT(*) FROM obs_source").fetchone()[0],)
        except Exception:                    # a record without the table
            head += (0,)
    except Exception:
        return None
    finally:
        conn.close()
    files = []
    store = Config.for_project(root).db_path
    for suffix in ("", "-wal"):
        try:
            info = os.stat(str(store) + suffix)
            files.append((info.st_size, info.st_mtime_ns))
        except OSError:
            files.append(None)
    return head + tuple(files)


def _build_wiki_briefing(root, key, op, task, max_chars, sig, outcome):
    """Build one briefing and keep it; `outcome` takes the error of a failed build for a reader
    that waited on it. The thread leaves _WIKI_BRIEFING_BUILDS whatever happens."""
    import time
    from alpaca.wiki import service
    began, built_at = time.monotonic(), time.time()
    try:
        brief = service.briefing(root, op=op, task=task, max_chars=max_chars)
        value = {"subject": op or task, "text": brief.text, "gaps": list(brief.gaps or []),
                 "truncated": bool(brief.truncated), "items": len(brief.items or []), "built_at": round(built_at, 3)}
        if sig is not None:
            with _WIKI_BRIEFING_LOCK:
                _WIKI_BRIEFING_CACHE.pop(key, None)
                _WIKI_BRIEFING_CACHE[key] = (began, sig, value)
                while len(_WIKI_BRIEFING_CACHE) > WIKI_BRIEFING_KEEP:
                    del _WIKI_BRIEFING_CACHE[next(iter(_WIKI_BRIEFING_CACHE))]
        outcome["value"] = value
    except BaseException as exc:
        outcome["error"] = exc
    finally:
        with _WIKI_BRIEFING_LOCK:
            _WIKI_BRIEFING_BUILDS.pop(key, None)


def wiki_briefing(root, *, op=None, task=None, max_chars=6000):
    """The wiki's cited briefing of one op or task (alpaca.wiki.service.briefing): built on demand
    from servable sources only, never stored, nothing written and no model called. The subject must
    be one op id or one task id. One build reads the whole store (2-3 s on a large one), so the
    first read of a subject waits for it and every later read answers at once with the last build,
    while one background build per subject catches up when its inputs moved (see above). Reads that
    arrive during a build share it."""
    import time
    op, task = (str(op).strip() if op else None), (str(task).strip() if task else None)
    if bool(op) == bool(task) or (op and not _OP_ID.fullmatch(op)) or (task and not _TASK_ID.fullmatch(task)):
        raise ValueError("name one op (op-12) or one task (t-345)")
    key, now = (str(Path(root).resolve()), op or task, max_chars), time.monotonic()
    sig = _wiki_briefing_sig(root)
    with _WIKI_BRIEFING_LOCK:
        cached = _WIKI_BRIEFING_CACHE.get(key)
        building = _WIKI_BRIEFING_BUILDS.get(key)
        current = cached and sig is not None and cached[1] == sig and now - cached[0] < WIKI_BRIEFING_TTL_S
        outcome = {}
        due = cached is None or now - cached[0] >= WIKI_BRIEFING_MIN_GAP_S
        if building is None and not current and due:
            building = threading.Thread(target=_build_wiki_briefing, daemon=True, name="wiki-briefing",
                                        args=(root, key, op, task, max_chars, sig, outcome))
            building.outcome = outcome
            _WIKI_BRIEFING_BUILDS[key] = building
            building.start()
    refreshing = False
    if cached and now - cached[0] < WIKI_BRIEFING_MAX_AGE_S:
        value, refreshing = cached[2], building is not None
    else:
        building.join()
        outcome = getattr(building, "outcome", None) or {}
        if "error" in outcome:
            raise outcome["error"]
        if "value" in outcome:
            value = outcome["value"]            # the build this reader waited on, even when it stored nothing
        else:
            with _WIKI_BRIEFING_LOCK:
                hit = _WIKI_BRIEFING_CACHE.get(key)
            value = hit[2]
    return dict(value, gaps=list(value["gaps"]), refreshing=refreshing)


# ------------------------------------------------------------------ the evidence graph (Records, Wiki, Open vault)
# One column per item type, in the owner's reading order (t-589): operations, tasks, decisions, sealed
# proofs, the evidence split by what it is, review cards, phase doors, sessions and messages, then the
# wiki's assertions, checked claims and the sources they quote. A link runs from the item that supports
# to the item it supports (evidence, proof, task, operation), whichever side its columns sit on. The
# record gives everything up to the messages; the wiki gives the rest (servable sources only).
GRAPH_COLUMNS = (("op", "Operations", "Ops"), ("task", "Tasks", "Tasks"), ("decision", "Decisions", "Decisions"),
                 ("proof", "Sealed proofs", "Proofs"), ("ev-event", "Evidence: record events", "Events"),
                 ("ev-log", "Evidence: logs and outputs", "Logs"), ("ev-code", "Evidence: code and tests", "Code"),
                 ("ev-file", "Evidence: documents and other files", "Docs"), ("review", "Review cards", "Reviews"),
                 ("phase", "Phase doors", "Doors"), ("session", "Sessions", "Sessions"),
                 ("message", "Messages", "Messages"), ("assertion", "Wiki assertions", "Assertions"),
                 ("claim", "Wiki claims", "Claims"), ("source", "Wiki sources", "Sources"))
#: the item kind graph_item reports for a column: the four evidence columns are one kind
GRAPH_KIND = {key: ("evidence" if key.startswith("ev-") else key) for key, _label, _short in GRAPH_COLUMNS}
GRAPH_TTL_S = 60
GRAPH_LABEL_CHARS = 140
GRAPH_MENTIONS = 20                     # the most items one message or review card links to
_GRAPH_CACHE: dict = {}
_MENTION = re.compile(r"(?<![\w-])(op-\d+|t-\d+[a-z]?)(?![\w-])")
_RAW_EVENT = re.compile(r"(?:^|/)raw/(op-\d+)/0*(\d+)-[a-z-]+\.md$")
_NUM = re.compile(r"\d+")
_SOURCE_ROOTS = ("alpaca/", "bin/", "setup/", "tools/", "te/", "plugin/", "templates/", "vendor/", "agents/",
                 "formations/")
_CODE_EXTS = (".py", ".js", ".mjs", ".sh", ".css", ".html", ".ts", ".sql")
_OUTPUT_EXTS = (".log", ".out", ".txt", ".json", ".jsonl", ".csv", ".tsv", ".xml")
_LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")
_DOOR_KINDS = {"phase-advance": "advanced", "phase-door-closed": "closed", "phase-pause": "paused"}


def _id_key(ident):
    """Sort key of an op or task id by its number (op-9 before op-10)."""
    found = _NUM.search(str(ident or ""))
    return (int(found.group()) if found else 1 << 30, str(ident or ""))


def _clip(text, limit=GRAPH_LABEL_CHARS):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def _decision_id(row):
    """A decision's graph id: its dec- reference, else its row id."""
    ref = str(row.get("ref") or "")
    return ref if ref.startswith("dec-") else "decision:%s" % row["id"]


def _evidence_column(pointer):
    """The evidence column of a cited pointer: a record event, a log or output, code or a test, or a
    document or other file (a remote ref among them)."""
    text = str(pointer or "")
    if text.startswith("event:"):
        return "ev-event"
    if not text.startswith("local:"):
        return "ev-file"
    path = _LINE_SUFFIX.sub("", text[len("local:"):].strip().split(" ")[0].split("#")[0])
    if path.startswith(_SOURCE_ROOTS) or path.endswith(_CODE_EXTS):
        return "ev-code"
    if ".logs/" in path or "/logs/" in path or path.endswith(_OUTPUT_EXTS):
        return "ev-log"
    return "ev-file"


def _proof_reports(conn):
    """The newest proof-report per task: {task: (event id, ts, data)}."""
    latest = {}
    for row in conn.execute("SELECT id, ts, ref, data FROM events WHERE kind='proof-report' ORDER BY id"):
        try:
            data = json.loads(row["data"] or "{}")
        except ValueError:
            continue
        if isinstance(data, dict) and row["ref"]:
            latest[row["ref"]] = (row["id"], row["ts"], data)
    return latest


def _graph_rows(nodes, links, count):
    """Order every column so its links cross as little as cheaply possible: columns are placed left to
    right, each entry by the mean height of its neighbours already placed (an entry with none keeps its
    insertion order, at the bottom). Returns the nodes in column order and the links re-indexed."""
    adjacent = [[] for _ in nodes]
    for a, b, _k in links:
        adjacent[a].append(b)
        adjacent[b].append(a)
    by_col, placed, order = {}, {}, []
    for i, n in enumerate(nodes):
        by_col.setdefault(n["c"], []).append(i)
    for ci in range(count):
        ids = by_col.get(ci, [])
        def weight(i):
            ys = [placed[j] for j in adjacent[i] if j in placed]
            return sum(ys) / len(ys) if ys else 2.0
        ranked = sorted(ids, key=lambda i: (weight(i), i))
        for row, i in enumerate(ranked):
            placed[i] = row / max(1, len(ranked) - 1)
        order.extend(ranked)
    remap = {old: new for new, old in enumerate(order)}
    return [nodes[i] for i in order], [[remap[a], remap[b], k] for a, b, k in links]


def _graph_build(root):
    """The graph payload and the server-side index the item reader uses."""
    from alpaca import review
    from alpaca.wiki import service
    columns = [key for key, _label, _short in GRAPH_COLUMNS]
    nodes, index, links, info = [], {}, [], {}

    def add(column, ident, label, **extra):
        if ident in index:
            return index[ident]
        index[ident] = len(nodes)
        node = {"id": ident, "c": columns.index(column)}
        if ident.split(":", 1)[-1] != label:                  # an evidence pointer or proof path is its own label
            node["label"] = _clip(label)
        nodes.append(dict(node, **extra))
        return index[ident]

    with _record(root) as conn:
        _pins, titles, _phase_events, _closes = _op_states(conn)
        ops = sorted(_rows(conn, "SELECT id, intent, status, opened, closed FROM ops"), key=lambda r: _id_key(r["id"]))
        tasks = sorted(_rows(conn, "SELECT id, op, title, statement, status, proof FROM tasks"),
                       key=lambda r: _id_key(r["id"]))
        added = {r["ref"]: (r["id"], r["ts"]) for r in conn.execute(
            "SELECT id, ts, ref FROM events WHERE kind='task-add' ORDER BY id")}
        reports = _proof_reports(conn)
        decisions, taken = [], set()
        for r in _rows(conn, "SELECT id, ts, session, actor, kind, body, ref FROM decisions ORDER BY ts, rowid"):
            ident = _decision_id(r)
            ident = ident if ident not in taken else "decision:%s" % r["id"]   # a reference recorded twice
            taken.add(ident)
            decisions.append(dict(_decision(r), id=ident))
        cards = review.all_cards(conn)
        doors = {}
        for r in _rows(conn, "SELECT id, ts, op, ref, kind, data FROM events WHERE kind IN "
                             "('phase-advance','phase-door-closed','phase-pause') AND op IS NOT NULL "
                             "AND ref IS NOT NULL ORDER BY id"):
            doors.setdefault((r["op"], r["ref"]), []).append(dict(r, data=_json(r["data"])))
        worked = {}
        for r in conn.execute("SELECT session, ref, ts FROM events WHERE kind IN ('task-add','task-move','proof-report') "
                              "AND session IS NOT NULL AND ref IS NOT NULL ORDER BY id"):
            worked.setdefault(r["session"], {}).setdefault(r["ref"], r["ts"])
        sessions = {r["sid"]: r for r in _rows(conn, "SELECT sid, started, ended, level, cwd FROM sessions")}
        messages = _rows(conn, "SELECT id, ts, session, sender, to_, kind, body, ref FROM messages ORDER BY id")
        claims = service.graph_claims(root)
        try:
            said = service.graph_assertion_list(root)
        except Exception as exc:                              # a broken store is shown, not raised
            said = {"available": False, "reason": "%s: %s" % (type(exc).__name__, exc), "assertions": []}
        refs = {}
        wanted = sorted({int(m.group(2)) for c in claims.get("claims") or [] for m in [_RAW_EVENT.search(c.get("source") or "")] if m})
        for start in range(0, len(wanted), 500):
            chunk = wanted[start:start + 500]
            for row in conn.execute("SELECT id, ref FROM events WHERE id IN (%s)" % ",".join("?" * len(chunk)), chunk):
                refs[row["id"]] = row["ref"]

    op_rank = {o["id"]: i for i, o in enumerate(ops)}
    tasks.sort(key=lambda t: (op_rank.get(t["op"], len(ops)), _id_key(t["id"])))
    proof_of, evidence_of = {}, {}
    for t in tasks:
        path = _proof_path(t["proof"]) if t["status"] == "done" else None
        if not path:
            continue
        proof_of[t["id"]] = path
        report = reports.get(t["id"])
        cited = report[2].get("evidence") if report and report[2].get("path") == path else None
        evidence_of[t["id"]] = [e for e in cited if isinstance(e, dict) and e.get("pointer")] if isinstance(cited, list) else []
    for o in ops:
        add("op", o["id"], titles.get(o["id"]) or o["intent"], s=o["status"])
    for t in tasks:
        add("task", t["id"], t["title"] or t["statement"], s=t["status"])
    named = {}
    for d in decisions:
        text = " ".join(str(d.get(k) or "") for k in ("choice", "context", "consequence", "pointer", "text"))
        named[d["id"]] = sorted(set(_MENTION.findall(text)), key=_id_key)
        add("decision", d["id"], d.get("choice") or d.get("text") or d["kind"], k=d["kind"])
    for t in tasks:
        if t["id"] in proof_of:
            report = reports.get(t["id"])
            add("proof", "proof:" + proof_of[t["id"]], proof_of[t["id"]], sealed=report[1] if report else None)
    for t in tasks:
        for e in evidence_of.get(t["id"], []):
            pointer = str(e["pointer"])
            add(_evidence_column(pointer), "ev:" + pointer, pointer, k=str(e.get("kind") or ""))
            info.setdefault("ev:" + pointer, []).append({"task": t["id"], "note": _clip(e.get("note"), 300)})
    for card in cards:
        state = card.get("status") or ("moved" if card.get("moved_by") else "open")
        add("review", str(card["id"]), "%s: %s" % (card.get("kind") or "card", card.get("subject") or card.get("reason") or ""),
            s=state, k=card.get("kind"))
        info[str(card["id"])] = card
    for (op, boundary), events in sorted(doors.items(), key=lambda kv: (_id_key(kv[0][0]), kv[1][0]["id"])):
        last = events[-1]
        ident = "door:%s:%s" % (op, boundary)
        add("phase", ident, "%s %s: %s" % (op, boundary, _DOOR_KINDS.get(last["kind"], last["kind"])),
            s=_DOOR_KINDS.get(last["kind"], last["kind"]))
        info[ident] = {"op": op, "boundary": boundary, "events": events}
    for sid, touched in sorted(worked.items(), key=lambda kv: min(kv[1].values())):
        row = sessions.get(sid) or {}
        ident = "session:" + sid
        add("session", ident, "session %s, started %s, %d task%s" % (sid[:8], str(row.get("started") or min(touched.values()))[:16],
                                                                    len(touched), "" if len(touched) == 1 else "s"),
            s="ended" if row.get("ended") else "open")
        info[ident] = {"sid": sid, "row": row, "tasks": list(touched)}
    for m in messages:
        to = str(m.get("to_") or "")
        ident = "msg:%s" % m["id"]
        add("message", ident, "%s to %s: %s" % (str(m.get("sender") or "")[:8], to if to == "all" else to[:8], m.get("body") or ""),
            k=m.get("kind"))
        info[ident] = m
    for a in said.get("assertions") or []:
        ident = "as:" + str(a["id"])
        add("assertion", ident, "%s %s %s" % (a.get("subject") or "", a.get("predicate") or "", a.get("object") or ""),
            k=a.get("predicate"))
        info[ident] = a
    claim_about = {}
    for c in claims.get("claims") or []:
        about = [n for n in c.get("about") or [] if n in index]
        found = _RAW_EVENT.search(c.get("source") or "")
        if found:
            about.append(found.group(1))
            ref = refs.get(int(found.group(2)))
            if ref:
                about.append(ref)
        claim_about[c["id"]] = [n for n in dict.fromkeys(about) if n in index]
        add("claim", "claim:" + c["id"], c.get("text"), s=c.get("verdict"))
        info["claim:" + c["id"]] = c
    quoted = {}
    for ident, source in [("as:" + str(a["id"]), a.get("source")) for a in said.get("assertions") or []] + \
                         [("claim:" + c["id"], c.get("source")) for c in claims.get("claims") or []]:
        if source:
            add("source", "src:" + source, source)
            quoted.setdefault("src:" + source, []).append(ident)

    seen = set()

    def link(a, b, kind):
        if a in index and b in index and a != b and (a, b, kind) not in seen:
            seen.add((a, b, kind))
            links.append([index[a], index[b], kind])
    for t in tasks:
        for e in evidence_of.get(t["id"], []):
            link("ev:" + str(e["pointer"]), "proof:" + proof_of[t["id"]], "cited by")
        if t["id"] in proof_of:
            link("proof:" + proof_of[t["id"]], t["id"], "proves")
        link(t["id"], t["op"], "part of")
    for d in decisions:
        for name in named[d["id"]]:
            link(name, d["id"], "named in")
    for card in cards:
        text = " ".join(str(card.get(k) or "") for k in ("subject", "reason", "decision"))
        for name in list(dict.fromkeys([card.get("op")] + _MENTION.findall(text)))[:GRAPH_MENTIONS]:
            link(str(card["id"]), name, "reviews")
    for op, boundary in doors:
        link("door:%s:%s" % (op, boundary), op, "door of")
    for sid, touched in worked.items():
        for task in touched:
            link("session:" + sid, task, "worked on")
    for m in messages:
        text = "%s %s" % (m.get("body") or "", m.get("ref") or "")
        for name in list(dict.fromkeys(_MENTION.findall(text)))[:GRAPH_MENTIONS]:
            link("msg:%s" % m["id"], name, "mentions")
    for a in said.get("assertions") or []:
        ident = "as:" + str(a["id"])
        link(a.get("subject"), ident, "subject of")
        if a.get("node"):
            link(a["node"], ident, "object of")
    for cid, about in claim_about.items():
        for name in about:
            link(name, "claim:" + cid, "source of")
    for src, quoting in quoted.items():
        for ident in quoting:
            link(src, ident, "quoted in")
    nodes, links = _graph_rows(nodes, links, len(columns))
    payload = {"columns": [{"key": k, "label": label, "short": short, "count": sum(1 for n in nodes if n["c"] == i)}
                           for i, (k, label, short) in enumerate(GRAPH_COLUMNS)],
               "nodes": nodes, "links": links,
               "wiki": {"available": bool(claims.get("available")), "reason": claims.get("reason"),
                        "hidden": claims.get("hidden", 0), "assertions": bool(said.get("available")),
                        "assertions_reason": said.get("reason"), "assertions_hidden": said.get("hidden", 0)}}
    extra = {"tasks": {t["id"]: t for t in tasks}, "ops": {o["id"]: o for o in ops}, "titles": titles,
             "added": added, "reports": {k: (v[0], v[1], v[2].get("path"), v[2].get("sha256")) for k, v in reports.items()},
             "decisions": {d["id"]: d for d in decisions}, "info": info, "quoted": quoted}
    return payload, extra


def _graph(root, now=None):
    import time
    with _record(root) as conn:
        stamp = conn.execute("SELECT max(id) FROM events").fetchone()[0]
    key, clock = str(Path(root).resolve()), time.monotonic() if now is None else now
    hit = _GRAPH_CACHE.get(key)
    if hit and hit[0] == stamp and clock - hit[1] < GRAPH_TTL_S:
        return hit[2], hit[3]
    payload, extra = _graph_build(root)
    _GRAPH_CACHE[key] = (stamp, clock, payload, extra)
    return payload, extra


def evidence_graph(root, *, now=None):
    """Every operation, task, decision, sealed proof, the evidence its proof report cites (by what it
    is), review card, phase door, session that worked a task and message, and the wiki's servable
    assertions, checked claims and their sources, as typed columns of nodes with links from the item
    that supports to the item supported (GRAPH_COLUMNS). Read-only; one build serves until the record
    moves or GRAPH_TTL_S passes."""
    return _graph(root, now)[0]


def graph_item(root, ident, *, now=None):
    """One graph node with the provenance of each of its links: the record event behind a task's
    operation, the proof report behind a proof and the notes it gives its evidence, a decision's
    recorded parts, a claim's verdict and question, and what the wiki asserts about the item with
    the quote it cites."""
    from alpaca.wiki import service
    payload, extra = _graph(root, now)
    ident = str(ident or "").strip()
    found = next((i for i, n in enumerate(payload["nodes"]) if n["id"] == ident), None)
    if found is None:
        raise ValueError("no graph item %r" % ident)
    node = payload["nodes"][found]
    kind = GRAPH_KIND[GRAPH_COLUMNS[node["c"]][0]]
    item = {"id": ident, "kind": kind, "column": GRAPH_COLUMNS[node["c"]][0], "label": node.get("label") or ident.split(":", 1)[-1], "facts": [], "wiki": None,
            "links": [{"id": payload["nodes"][b if a == found else a]["id"],
                       "dir": "out" if a == found else "in", "kind": k}
                      for a, b, k in payload["links"] if found in (a, b)]}
    facts = item["facts"]
    if kind == "task":
        t = extra["tasks"].get(ident) or {}
        facts += [["Title", t.get("title") or ""], ["Statement", t.get("statement") or ""],
                  ["Status", t.get("status") or ""], ["Operation", t.get("op") or ""]]
        if ident in extra["added"]:
            facts.append(["Recorded", "event %s at %s (task-add)" % extra["added"][ident]])
        if t.get("proof"):
            facts.append(["Proof", t["proof"]])
    elif kind == "op":
        o = extra["ops"].get(ident) or {}
        facts += [["Intent", o.get("intent") or ""], ["Status", o.get("status") or ""],
                  ["Opened", o.get("opened") or ""], ["Closed", o.get("closed") or ""]]
    elif kind == "proof":
        path = ident[len("proof:"):]
        task = next((t for t, (_e, _ts, p, _sha) in extra["reports"].items() if p == path), None)
        facts.append(["Report", path])
        if task:
            event, ts, _p, sha = extra["reports"][task]
            facts += [["Task", task], ["Sealed", "event %s at %s (proof-report)" % (event, ts)], ["sha256", sha or ""]]
        item["path"] = path
    elif kind == "evidence":
        facts.append(["Pointer", ident[len("ev:"):]])
        for cite in extra["info"].get(ident, [])[:40]:
            facts.append(["Cited for " + cite["task"], cite["note"] or "(no note)"])
    elif kind == "decision":
        d = extra["decisions"].get(ident) or {}
        facts += [[label, d.get(key) or ""] for label, key in (("Kind", "kind"), ("Choice", "choice"), ("Context", "context"),
                                                               ("Consequence", "consequence"), ("Recorded", "ts"))]
        if d.get("options"):
            facts.append(["Options", ", ".join(d["options"])])
    elif kind == "claim":
        c = extra["info"].get(ident) or {}
        facts += [["Claim", _clip(c.get("text"), 900)], ["Verdict", c.get("verdict") or ""],
                  ["Question", c.get("question") or ""], ["Answer", c.get("answer_verdict") or ""],
                  ["Source", c.get("source") or ""], ["Checked", c.get("created") or ""]]
    elif kind == "review":
        card = extra["info"].get(ident) or {}
        facts += [["Kind", card.get("kind")], ["State", card.get("status")], ["Operation", card.get("op")],
                  ["Subject", card.get("subject")], ["Reason", _clip(card.get("reason"), 1500)],
                  ["Decision", card.get("decision")], ["Moved by", card.get("moved_by")],
                  ["Moved", card.get("moved_at")], ["Created", card.get("created")]]
    elif kind == "phase":
        door = extra["info"].get(ident) or {}
        facts += [["Operation", door.get("op")], ["Boundary", door.get("boundary")]]
        for event in (door.get("events") or [])[-12:]:
            data = event.get("data") or {}
            gates = ", ".join("%s %s" % tuple(pair) for pair in data.get("links") or [] if isinstance(pair, list) and len(pair) == 2)
            facts.append(["Event %s" % event["id"], "%s at %s, level %s%s" % (
                _DOOR_KINDS.get(event["kind"], event["kind"]), event["ts"], data.get("level") or "?",
                (": " + gates) if gates else "")])
    elif kind == "session":
        about = extra["info"].get(ident) or {}
        row = about.get("row") or {}
        worked = about.get("tasks") or []
        facts += [["Session", about.get("sid")], ["Started", row.get("started")], ["Ended", row.get("ended")],
                  ["Level", row.get("level")], ["Folder", row.get("cwd")],
                  ["Tasks", "%d: %s%s" % (len(worked), ", ".join(worked[:40]), " ..." if len(worked) > 40 else "")]]
    elif kind == "message":
        m = extra["info"].get(ident) or {}
        facts += [["From", m.get("sender")], ["To", m.get("to_")], ["Kind", m.get("kind")], ["Sent", m.get("ts")],
                  ["Reference", m.get("ref")], ["Message", _clip(m.get("body"), 1500)]]
    elif kind == "assertion":
        a = extra["info"].get(ident) or {}
        facts += [["Subject", a.get("subject")], ["Predicate", a.get("predicate")], ["Object", a.get("object")],
                  ["Quote", a.get("quote")], ["Source", a.get("source")], ["Recorded", a.get("recorded")]]
    elif kind == "source":
        quoting = extra["quoted"].get(ident) or []
        facts += [["Document", ident[len("src:"):]],
                  ["Quoted by", "%d wiki item%s" % (len(quoting), "" if len(quoting) == 1 else "s")]]
    item["facts"] = [[k, str(v)] for k, v in facts if v not in (None, "")]
    if kind in ("task", "op", "proof", "decision"):
        try:
            item["wiki"] = service.graph_assertions(root, ident)
        except Exception as exc:                              # a broken store is shown, not raised
            item["wiki"] = {"available": False, "reason": "%s: %s" % (type(exc).__name__, exc), "assertions": []}
    return item
