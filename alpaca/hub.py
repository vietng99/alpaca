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
import io
import json
import os
from pathlib import Path
import re

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
    """Pin choice, short title and phase events per op, read from the record in event order. The
    newest pin or unpin wins, and the newest title (op-open's, then op-title renames) wins; nothing
    is inferred from activity."""
    pins, titles, phase_events = {}, {}, {}
    for row in _rows(conn, "SELECT op,kind,ts,data FROM events WHERE kind IN "
                           "('op-pin','op-unpin','op-open','op-title','phase-enter','phase-advance',"
                           "'phase-door-closed') ORDER BY id"):
        if row["kind"] in ("op-pin", "op-unpin"):
            pins[row["op"]] = row["kind"] == "op-pin"
        elif row["kind"] in ("op-open", "op-title"):
            data = _json(row["data"])
            if isinstance(data, dict) and isinstance(data.get("title"), str) and data["title"].strip():
                titles[row["op"]] = data["title"].strip()
        else:
            phase_events.setdefault(row["op"], []).append({"kind": row["kind"], "at": row["ts"], "data": _json(row["data"])})
    return pins, titles, phase_events


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
        clauses, params = ["id <= ?"], [upper]
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
        hours = None
        if calendar and conn:
            base = " AND ".join(clauses)
            rows = conn.execute("SELECT substr(ts,1,10), CAST(substr(ts,12,2) AS INTEGER), COUNT(*) FROM events WHERE "
                                + base + " GROUP BY 1, 2", params).fetchall()
            keep = sorted({r[0] for r in rows})[-14:]
            hours = [{"day": d, "hour": h, "count": n} for d, h, n in rows if d in keep]
        if day:
            clauses.append("substr(ts,1,10) = ?")
            params.append(str(day))
        where = " AND ".join(clauses)
        total = conn.execute("SELECT COUNT(*) FROM events WHERE " + where, params).fetchone()[0] if conn else 0
        rows = _rows(conn, "SELECT * FROM events WHERE " + where + " AND id < ? ORDER BY id DESC LIMIT ?",
                     params + [edge, limit + 1])
    more, rows = len(rows) > limit, rows[:limit]
    cursor = None
    if more:
        payload = json.dumps({"before": rows[-1]["id"], "upper": upper}, separators=(",", ":"))
        cursor = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    result = {"items": [_event(r) for r in rows], "total": total, "next_before": cursor,
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
            for name in sorted(files):
                path = _contained(root, Path(directory) / name)
                if path is not None and _is_runbook(path) and _document_allowed(root, path):
                    yield path


def _parts_below(root, path):
    """`path.relative_to(root).parts` for a resolved `path` under the resolved `root`, by string:
    the catalog asks this for every file, and `PurePath.relative_to` walks `parents` each time
    (op-008 t-463, review minor M-1). ValueError when `path` is not below `root`, as before."""
    top, here = str(root).rstrip(os.sep) + os.sep, str(path)
    if not here.startswith(top):
        return Path(here).relative_to(root).parts      # the same answer or ValueError, the slow way
    return tuple(p for p in here[len(top):].split(os.sep) if p)


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
    allowed = any(candidate == target for candidate in _document_paths(root, tools=tools))
    if not allowed and not tools:
        allowed = any(candidate == target for candidate in _runbook_paths(root))
    if not allowed:
        with _record(root) as conn:
            allowed = target.relative_to(root).as_posix() in _proof_refs(root, conn)
    if not allowed:
        raise FileNotFoundError("No readable report at this path")
    limit = 512 * 1024
    with _open_document(root, target) as handle:
        text = handle.read(limit + 1)
        size = os.fstat(handle.fileno()).st_size
    return {"path": target.relative_to(root).as_posix(), "text": text[:limit],
            "size": size, "truncated": len(text) > limit}


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
            for name in sorted(files):
                path = _contained(root, Path(directory) / name)
                if _document_allowed(root, path) and path.suffix.lower() in (".md", ".html", ".htm") \
                        and path not in seen:
                    seen.add(path)
                    yield path
    if not tools:
        for path in sorted(root.glob("*.md")):
            safe = _contained(root, path)
            if _document_allowed(root, safe) and safe not in seen:
                yield safe


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
    paths = set(_document_paths(root, tools=category == "tools"))
    runbook_roots = tuple(r + "/" for r in ("runbooks", "specs") + _profile_documents(root))
    if category != "tools":
        paths.update(path for rel in refs if (path := _contained(root, rel)) is not None)
        paths.update(_runbook_paths(root))
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
            title, summary = _document_text(root, path)
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
        pins, titles, phase_events = _op_states(conn)
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
                "phase_state": _phase_state(_phases(row), phase_events.get(row["id"], []))}
               for row in _rows(conn, "SELECT * FROM ops ORDER BY opened DESC,id")]
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
