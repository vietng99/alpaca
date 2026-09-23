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
import json
import os
from pathlib import Path
import re

from alpaca import db, profile, util, work_record


_HEX = re.compile(r"^[a-f0-9]{32}$")
_SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
         ".mypy_cache", ".cache", "dist", "build", "cache", "caches"}
_REPORT_ROOTS = ("docs", "reports", "runbooks", "specs", ".alpaca/proofs",
                 ".alpaca/reports", ".alpaca/reviews", ".alpaca/wiki/decisions",
                 ".alpaca/wiki/pages", ".alpaca/wiki/reports")
_FAILURE_SQL = """(kind LIKE '%failed%' OR kind LIKE '%error%' OR
    CASE WHEN json_valid(data) THEN
      upper(COALESCE(json_extract(data,'$.verdict_name'),json_extract(data,'$.verdict'),''))
      IN ('FAIL','BLOCKED','INTERRUPTED','ERROR') OR json_extract(data,'$.error') IS NOT NULL
    ELSE 1 END)"""
#: the kinds a session writes without doing work of its own. A domain profile adds its own
#: side-effect kinds (alpaca/profile.py `events`).
_SIDE_EFFECT_KINDS = ('session-start', 'session-end', 'init', 'transcript-snapshot', 'style-rearm',
                      'verdict', 'run', 'bridge')


def _work_sql(root):
    """The work filter of the history feed and its parameters after the id watermark."""
    extra = profile.listed(profile.load(root).events(), "side_effect_kinds")
    kinds = list(_SIDE_EFFECT_KINDS) + [str(k) for k in extra if str(k) not in _SIDE_EFFECT_KINDS]
    sql = """(%s OR (kind NOT IN ('heartbeat','turn-end','transcript-snapshot','style-rearm')
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


def history(root, before=None, limit=50, query="", kind="work", session="", ref=""):
    """Newest first, with a snapshot watermark carried inside the event-ID cursor."""
    root, limit = Path(root).resolve(), _page_size(limit, 50, 200)
    with _record(root) as conn:
        newest = conn.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0] if conn else 0
        edge, upper = _cursor(before, newest)
        clauses, params = ["id <= ?"], [upper]
        if kind in ("work", "progress"):
            work_sql, work_kinds = _work_sql(root)
            clauses.append(work_sql)
            params.append(upper)
            params.extend(work_kinds)
            if kind == "progress":
                # Unbound instrument diagnostics are not task or run progress.
                # Preserve them (including failures) in work/all history.
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
        where = " AND ".join(clauses)
        total = conn.execute("SELECT COUNT(*) FROM events WHERE " + where, params).fetchone()[0] if conn else 0
        rows = _rows(conn, "SELECT * FROM events WHERE " + where + " AND id < ? ORDER BY id DESC LIMIT ?",
                     params + [edge, limit + 1])
    more, rows = len(rows) > limit, rows[:limit]
    cursor = None
    if more:
        payload = json.dumps({"before": rows[-1]["id"], "upper": upper}, separators=(",", ":"))
        cursor = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return {"items": [_event(r) for r in rows], "total": total, "next_before": cursor,
            "has_more": more, "watermark": upper}


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


def _document_allowed(root, path):
    """Catalog paths cannot alias private runtime state or evidence attachments."""
    if path is None or path.suffix.lower() not in (".md", ".html", ".htm"):
        return False
    parts = path.relative_to(root).parts
    if any(part.startswith(".") for part in parts[1:]):
        return False
    if any(part.endswith((".evidence", ".kept")) for part in parts):
        return False
    if parts[0].startswith("."):
        return parts[0] == ".alpaca" and len(parts) > 2 and parts[1] in ("proofs", "reports", "reviews", "wiki")
    return parts[0] not in _SKIP


def document(root, path):
    """Read a catalog-authorized report, bounded independently from listing limits."""
    root = Path(root).resolve()
    target = _contained(root, path)
    if not _document_allowed(root, target) or not target.is_file():
        raise FileNotFoundError("No readable report at this path")
    tools = str(path).startswith(tuple(t + "/" for t in _tool_roots(root)))
    allowed = any(candidate == target for candidate in _document_paths(root, tools=tools))
    if not allowed:
        with _record(root) as conn:
            allowed = target.relative_to(root).as_posix() in _proof_refs(root, conn)
    if not allowed:
        raise FileNotFoundError("No readable report at this path")
    limit = 512 * 1024
    with target.open(encoding="utf-8", errors="replace") as handle:
        text = handle.read(limit + 1)
    return {"path": target.relative_to(root).as_posix(), "text": text[:limit],
            "size": target.stat().st_size, "truncated": len(text) > limit}


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
                if _document_allowed(root, path) and path not in seen:
                    seen.add(path)
                    yield path
    if not tools:
        for path in sorted(root.glob("*.md")):
            safe = _contained(root, path)
            if _document_allowed(root, safe) and safe not in seen:
                yield safe


def _document_text(path):
    with path.open(encoding="utf-8", errors="replace") as handle:
        text = handle.read(16384)
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
    if ext not in ("", "all", "md", "html", "htm"):
        raise ValueError("unsupported document extension")
    with _record(root) as conn:
        refs = _proof_refs(root, conn)
    paths = set(_document_paths(root, tools=category == "tools"))
    runbook_roots = tuple(r + "/" for r in ("runbooks", "specs") + _profile_documents(root))
    if category != "tools":
        paths.update(path for rel in refs if (path := _contained(root, rel)) is not None)
    items = []
    for path in paths:
        if not _document_allowed(root, path):
            continue
        rel = path.relative_to(root).as_posix()
        suffix = path.suffix.lower().lstrip(".")
        if suffix not in ("md", "html", "htm") or (ext not in ("", "all") and suffix != ext and not (ext == "html" and suffix == "htm")):
            continue
        bucket = ("tools" if category == "tools" else "proofs" if rel.startswith(".alpaca/proofs/") or rel in refs
                  else "decisions" if "decisions" in path.parts else "runbooks" if rel.startswith(runbook_roots) else "reports")
        if category not in ("reports", "all", "tools") and category != bucket:
            continue
        try:
            stat = path.stat()
            title, summary = _document_text(path)
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
            with path.open(encoding="utf-8", errors="replace") as handle:
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


def overview(root):
    root = Path(root).resolve()
    checked, check_error, stamp = _checked(root)
    run_data = runs(root)
    with _record(root) as conn:
        cap, cap_cutoff = _capture(root, conn)
        resolved_ops = profile.listed(cap, "resolved_ops")
        error_kinds = profile.listed(cap, "error_kinds")
        tasks = _tasks(root, conn)
        ops = [{"id": row["id"], "title": row["intent"], "status": row["status"], "done_when": row["done_when"],
                "tasks_total": sum(t["op"] == row["id"] for t in tasks),
                "tasks_done": sum(t["op"] == row["id"] and t["status"] == "done" for t in tasks)}
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
        items = _acceptance(root, conn, checked, stamp)
        newest = _rows(conn, "SELECT ts,id FROM events ORDER BY id DESC LIMIT 1")
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
    config = _contained(root, "project.yaml")
    if config is not None and config.is_file():
        try:
            import yaml
            name = (yaml.safe_load(config.read_text()) or {}).get("name") or name
        except (OSError, ValueError, yaml.YAMLError):
            pass
    cap_status = str(cap_block.get("status") or "ok")
    return {"project": {"name": name}, "as_of": newest[0]["ts"] if newest else None,
            "profile": profile.web_meta(root),
            "tasks": tasks, "ops": ops, "items": items, "stages": stages,
            "observability": capture_health(root),
            "acceptance": {"status": ("PASS" if stages and all(s["status"] == "PASS" for s in stages)
                                      else "BLOCKED" if stages else "unavailable")},
            "capture": {"status": "error" if errors or cap_status == "error" else cap_status,
                        "profile": cap_block, "errors": errors, "wiki_recovery": recovery},
            "messages": messages, "active_jobs": active,
            "counts": {"tasks": len(tasks), "items": len(items), "items_pass": sum(i.get("status") == "PASS" for i in items),
                       "items_blocked": sum(i.get("status") == "BLOCKED" for i in items),
                       "items_fail": sum(i.get("status") == "FAIL" for i in items), "active_jobs": len(active)}}
