"""Fold per-session JSON (from transcripts) + the record into one ASCII single-file app."""
import glob, json, os, re
import copy
from collections import OrderedDict
import threading
from alpaca import db, determinism, paths, project, render, sessions_view, util
from alpaca.analytics import parse_session as ps
from alpaca.analytics import usage

_PARSE_CACHE = OrderedDict()
_PARSE_LOCK = threading.RLock()

def _sessions_dir(root):
    return os.path.join(paths.runtime_dir(root), "analytics", "sessions")

def _record_summary(conn, sid):
    q = lambda kind: conn.execute("SELECT COUNT(*) FROM events WHERE session=? AND kind=?", (sid, kind)).fetchone()[0]
    row = db.rows(conn, "sessions", "sid=?", (sid,))
    hook_rows = [item for item in conn.execute("SELECT key,value FROM meta WHERE key LIKE 'hook-cost:%'")
                 if item["key"].startswith("hook-cost:" + sid + ":")]
    hook_ms = 0.0; hook_runs = 0
    for item in hook_rows:
        try:
            value = json.loads(item["value"])
            hook_ms += float(value["total_ms"]); hook_runs += int(value["runs"])
        except (KeyError, TypeError, ValueError):
            continue
    return {"events": conn.execute("SELECT COUNT(*) FROM events WHERE session=?", (sid,)).fetchone()[0],
            "heartbeats": q("heartbeat"), "tasks_moved": q("task-move"), "ops_opened": q("op-open"),
            "ops_closed": q("op-close"), "msgs": q("msg"), "level": row[0]["level"] if row else None,
            "last_beat": row[0]["last_beat"] if row else None, "hook_runtime_ms": round(hook_ms, 1),
            "hook_runs": hook_runs}

def _transcripts(root, conn):
    # Only explicitly registered paths and project-local copies. Historical
    # working directories and account-wide Claude storage are never scanned.
    local = {}
    for p in glob.glob(os.path.join(paths.transcript_dir(root), "*.jsonl")):
        if not os.path.realpath(p).startswith(os.path.realpath(root) + os.sep):
            continue
        local[os.path.splitext(os.path.basename(p))[0]] = p
    seen = {}
    for r in db.rows(conn, "sessions", "transcript IS NOT NULL AND transcript != ''"):
        path = r["transcript"]
        if not os.path.isabs(path):
            path = os.path.join(root, path)
        # E1: the registered path lives in the operator's own storage, which is cleared after
        # thirty days. When it is gone and alpaca.transcripts has a copy, read the copy: a session
        # whose source expired keeps its prompts, tokens and tool calls instead of falling back
        # to the record-only summary.
        if not os.path.isfile(path) and r["sid"] in local:
            path = local[r["sid"]]
        seen[r["sid"]] = path
    from alpaca import transcripts
    for row in db.rows(conn, "sessions"):
        if row['sid'] not in seen:
            registered = transcripts.registered(conn, row['sid'])
            if registered:
                seen[row['sid']] = registered if os.path.isabs(registered) else os.path.join(root, registered)
    for sid, p in local.items():
        seen.setdefault(sid, p)
    return seen


def _record_only(conn, sid):
    row = db.rows(conn, "sessions", "sid=?", (sid,))
    session = row[0] if row else {}
    events = db.events(conn, session=sid, limit=1_000_000)
    beats = [e for e in events if e["kind"] == "heartbeat"]
    tools = {}
    for e in beats:
        name = e.get("data", {}).get("tool") or "reported action"
        tools[name] = tools.get(name, 0) + 1
    return {"sid": sid, "title": "%s session (record only)" %
            (db.meta_get(conn, "operator:%s" % sid) or "operator"),
            "first": session.get("started"), "last": session.get("ended") or session.get("last_beat"),
            "span_s": 0, "turns": sum(e["kind"] == "turn-end" for e in events),
            "tool_calls": len(beats), "human_prompts": [], "tools": tools,
            "files": {}, "models": {}, "hooks": {}, "hourly": {}, "subagents": 0,
            "transcript": None, "usage_available": False, "capture": "explicit-record",
            "tokens": {name: None for name in
                       ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")},
            "cost_notional_usd": None, "cost_reported_usd": None, "cost_available": False,
            "cost_estimate_available": False, "cost_provenance": None}


def _imported_usage(root, sid, d, conn):
    # The canonical analysis owns precedence and keeps reported charges separate.
    from alpaca.analytics import metrics
    analysis = metrics.analyze(root, sid)
    value = analysis['imported_usage']
    d['imported_usage'] = value
    d['cost_reported_usd'] = value['reported_cost_usd']
    d['cost_available'] = value['cost_complete']
    if value['samples']:
        d['usage_samples'] = value['samples']
        d['cost_provenance'] = 'reported-provider-usage' if value['cost_complete'] else d.get('cost_provenance')
    return d


def _codex_session(root, sid, conn):
    """Use the visible-record adapter for explicitly registered Codex transcripts."""
    from datetime import datetime
    from alpaca.analytics.detail import detail
    captured = detail(root, sid, limit=1, include_rollup=True)
    session, rollup = captured["session"], captured["rollup"]
    value = _record_only(conn, sid)
    value.update(rollup)
    value.update(title=session["title"], turns=session["turns"], tool_calls=session["tool_calls"],
                 subagents=len(captured["children"]), conversation_coverage=captured["coverage"],
                 _adapter="codex-visible-v2")
    try:
        value["span_s"] = max(0, (datetime.fromisoformat(rollup["last"]) - datetime.fromisoformat(rollup["first"])).total_seconds())
    except (TypeError, ValueError):
        value["span_s"] = 0
    return value


def collect(root, force=False, only=None):
    """Read classified sessions without migrations, cache files, or other writes."""
    from alpaca.analytics import metrics
    conn = db.connect_readonly(root)
    try:
        result = []
        sources = _transcripts(root, conn)
        view = sessions_view.classify(conn)
        sessions = {r["sid"] for r in db.rows(conn, "sessions")} | set(sources)
        for sid in sorted(sessions):
            if only is not None and sid != only:
                continue
            tpath = sources.get(sid)
            available = bool(tpath and os.path.isfile(tpath))
            d = _record_only(conn, sid)
            analysis = metrics.analyze(root, sid)
            src = analysis['revision']
            key = (os.path.realpath(root), sid, json.dumps(src, sort_keys=True))
            if available:
                try:
                    with _PARSE_LOCK:
                        parsed = None if force else copy.deepcopy(_PARSE_CACHE.get(key))
                    if parsed is None:
                        parsed = ps.parse(tpath, include_children=False)
                        with _PARSE_LOCK:
                            for stale in [item for item in _PARSE_CACHE if item[:2] == key[:2]]:
                                del _PARSE_CACHE[stale]
                            if len(json.dumps(parsed)) <= 1024 * 1024:
                                _PARSE_CACHE[key] = copy.deepcopy(parsed)
                            while len(_PARSE_CACHE) > 32:
                                _PARSE_CACHE.popitem(last=False)
                    d.update(parsed)
                except (OSError, ValueError, TypeError) as exc:
                    d['parse_error'] = '%s: %s' % (type(exc).__name__, exc)
            d['sid'] = sid
            d['capture'] = 'registered-transcript' if available else 'explicit-record'
            d['_src'] = src
            summary, coverage = analysis['summary'], analysis['coverage']
            measured = summary['tokens'] or {}
            d['tokens'] = {old: measured.get(new) for old, new in {
                'input': 'input_tokens', 'output': 'output_tokens', 'cache_read': 'cached_input_tokens',
                'cache_write_5m': 'cache_creation_5m_tokens', 'cache_write_1h': 'cache_creation_1h_tokens'}.items()}
            d['usage_available'] = bool(coverage['usage_total_is_exact'] and all(value is not None for value in d['tokens'].values()))
            d['usage_incomplete'] = not d['usage_available']
            d['visible_assistant_messages'] = analysis['session']['turns']
            d['coverage'] = coverage
            d['conversation_coverage'] = coverage
            if available:
                d['turns'], d['tool_calls'] = summary['responses'], summary['tool_calls']
            d['cost_notional_usd'] = summary['estimated_cost_usd']
            d['cost_estimate_available'] = bool(coverage['usage_total_is_exact'] and summary['responses'] and summary['costed_responses'] == summary['responses'])
            d['cost_provenance'] = 'rate_card_estimate' if d['cost_estimate_available'] else None
            imported = analysis['imported_usage']
            d['imported_usage'] = imported
            d['cost_reported_usd'] = imported['reported_cost_usd']
            d['cost_available'] = imported['cost_complete']
            if imported['samples']:
                d['usage_samples'] = imported['samples']
                if coverage['usage_method'] == 'explicit-usage-samples':
                    d['capture'] = 'explicit-usage'
                if imported['cost_complete']:
                    d['cost_provenance'] = 'reported-provider-usage'
            if imported['partial']:
                d['usage_import_error'] = 'invalid or incomplete explicit usage events'
            d['record'] = _record_summary(conn, sid)
            d['class'] = sessions_view.class_of(view, sid)
            if d['class'] == sessions_view.PROBE and (d.get('turns') or d.get('tool_calls')):
                d['class'] = sessions_view.WORK
            result.append(d)
        return result
    finally:
        conn.close()

def _fold(root, sessions):
    conn = db.connect_readonly(root); cfg = project.load(root)
    # op-006: a probe is counted, never listed. It beat nothing, ended no turn and spent nothing,
    # so it contributes to no total either: sixty of them made every figure on the page read as
    # an average over sessions that never happened.
    listed = [s for s in sessions if s.get("class", sessions_view.WORK) != sessions_view.PROBE]
    work = [s for s in sessions if s.get("class", sessions_view.WORK) == sessions_view.WORK]
    dropped = [s for s in sessions if s.get("class", sessions_view.WORK) == sessions_view.PROBE]
    stamps = sorted(str(s[k]) for s in dropped for k in ("first", "last") if s.get(k))
    probes = {"count": len(dropped), "first": stamps[0] if stamps else None,
              "last": stamps[-1] if stamps else None}
    complete_usage = bool(work) and all(s.get("usage_available", False) for s in work)
    complete_estimate = bool(work) and all(s.get("cost_estimate_available", False) for s in work)
    incomplete = sum(not session.get('coverage', {}).get('complete', False) for session in work)
    tot = {"sessions": len(work), "probe_sessions": probes["count"],
           "listed_sessions": len(work), "partial_sessions": incomplete,
           "measured_sessions": sum(session.get('tokens', {}).get('output') is not None for session in work),
           "incomplete_project": bool(incomplete),
           "usage_scope": "available main-conversation measurements; imported samples never replace native measurements",
           "turns": sum(s["turns"] for s in work),
           "tool_calls": sum(s["tool_calls"] for s in work), "prompts": sum(len(s["human_prompts"]) for s in work),
           "output_tokens": (sum(s["tokens"]["output"] or 0 for s in work)
                             if complete_usage else None),
           "cost_notional_usd": (round(sum(s["cost_notional_usd"] or 0 for s in work), 2)
                                 if complete_estimate else None),
           "cost_reported_usd": None,
           "cost_available": False,
           "reported_cost_scope": "Per-session imports are separate; cross-session overlap has not been reconciled.",
           "usage_available": complete_usage,
           "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
           "ops": {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM ops GROUP BY status")},
           "tasks": {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM tasks GROUP BY status")}}
    daily = {}
    tools = {}
    for s in work:
        for hk, v in s["hourly"].items():
            day = hk[:10]; d = daily.setdefault(day, {"turns": 0, "tools": 0, "prompts": 0, "sessions": set()})
            d["turns"] += v["turns"]; d["tools"] += v["tools"]; d["prompts"] += v["prompts"]; d["sessions"].add(s["sid"])
        for k, v in s["tools"].items():
            tools[k] = tools.get(k, 0) + v
    daily = [{"day": k, "turns": v["turns"], "tools": v["tools"], "prompts": v["prompts"], "sessions": len(v["sessions"])}
             for k, v in sorted(daily.items())]
    # M2.2: "generated" reads util.now_iso, so a clock installed with util.set_clock (a FixedClock
    # under test) makes the fold reproducible. The two orderings below carry a total tie-break
    # (name, sid) via determinism.stable_rank so dict/hash-seed order never leaks into the output.
    # M4.9: the project name is externally sourced and lands on an HTML page surface. It crosses
    # render.html_text, which HTML-escapes its markup characters and renders any non-ASCII as an
    # entity, so the page carries no live markup from the name and stays pure ASCII.
    conn.close()
    return {"project": {"name": render.html_text(cfg.get("name") or "(not onboarded)"),
                        "generated": util.now_iso(), **tot},
            "daily": daily,
            "probes": probes,
            "tools": determinism.stable_rank(({"name": k, "n": v} for k, v in tools.items()),
                                             key=lambda x: (-x["n"], x["name"]))[:20],
            "sessions": determinism.stable_rank(listed,
                                                key=lambda s: (s.get("first") or "", s.get("sid") or ""))[::-1]}

def build(root, force=False):
    sessions = collect(root, force=force)
    data = _fold(root, sessions)
    tpl = util.read_text(os.path.join(os.path.dirname(__file__), "app.html"))
    payload = json.dumps(data, ensure_ascii=True).replace("<", "\\u003c")
    html = tpl.replace("__DATA__", payload)
    # M4.9: the whole-page final ASCII pass is shared with alpaca serve through render.ascii_entities,
    # so the phone-safe ASCII rule holds in one place.
    html = render.ascii_entities(html)
    out = os.path.join(root, "analytics", "index.html")
    util.write_text(out, html)
    return out
