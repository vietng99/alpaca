"""Compact project-scoped index of main-conversation analytics.

The server supplies its classified session list. A subagent is folded under its parent session
(alpaca.analytics.children): it is never a row of its own, its measurements are added to the
parent's `family` figures and to the totals. Client cost snapshots are deliberately not summed
across overlapping scopes.

A project has more work sessions than metrics keeps whole analyses for (metrics.MAX_CACHE),
so index() keeps its own small row per session: only the pieces it reads, keyed on
metrics.analysis_key. An unchanged session is never analysed again; a changed transcript or a
newly recorded rate card changes the key and recomputes that row.
"""
from collections import OrderedDict
import copy
import json
import threading

from alpaca.analytics import detail, metrics, ratecards

# Keep the measured project inventory across complete rebuilds: 773 parents and 2741 children,
# then 1333 parents and 9314 children (10604 rows, 19.6 MB of JSON) at t-680, when an 8192-row
# bound evicted every row before its reuse. The byte bound is the real one; the row bound only
# stops a flood of tiny rows.
MAX_ROWS = 65536
MAX_ROW_BYTES = 64 * 1024 * 1024
_ROWS = OrderedDict()
_ROW_BYTES = 0
_ROWS_LOCK = threading.RLock()
_MODEL_KEYS = ("model", "provider", "responses", "costed_responses", "unpriced")
_UNKNOWN = "unknown session"     # metrics.analyze's KeyError for a conversation the record does not know


def _analysis_row(root, sid):
    """The pieces of metrics.analyze(root, sid) that index() needs, cached per analysis key.

    A conversation the record does not know (metrics.analyze raises KeyError('unknown session'):
    no session row, no transcript, no pool file) is cached too, as a row of None that raises the
    same KeyError: the key covers each of those inputs, so it moves when one appears. Most
    children of the live record are such files, and an uncached miss paid the source revision
    twice and the failed analysis on every index run (t-680)."""
    global _ROW_BYTES
    key = metrics.analysis_key(root, sid)
    identity = key[:3]
    with _ROWS_LOCK:
        cached = _ROWS.get(identity)
        if cached is not None and cached[0] == key:
            _ROWS.move_to_end(identity)
            if cached[1] is None:
                raise KeyError(_UNKNOWN)
            return copy.deepcopy(cached[1])
    try:
        analysis = metrics.analyze(root, sid)
    except KeyError as error:
        if error.args != (_UNKNOWN,):
            raise
        analysis = None
    if analysis is None:
        row = None
    else:
        row = {"summary": analysis["summary"], "coverage": analysis["coverage"], "cost": analysis["cost"],
               "operator": analysis["session"].get("operator"),
               "models": [{name: model.get(name) for name in _MODEL_KEYS} for model in analysis.get("models") or []]}
    size = len(json.dumps([key, row], ensure_ascii=True).encode("utf-8"))
    with _ROWS_LOCK:
        # Replace a conversation revision directly, without scanning every key.
        prior = _ROWS.pop(identity, None)
        if prior is not None:
            _ROW_BYTES -= prior[2]
        if size <= MAX_ROW_BYTES:
            _ROWS[identity] = (key, copy.deepcopy(row), size)
            _ROW_BYTES += size
        while _ROWS and (len(_ROWS) > MAX_ROWS or _ROW_BYTES > MAX_ROW_BYTES):
            _ROW_BYTES -= _ROWS.popitem(last=False)[1][2]
    if row is None:
        raise KeyError(_UNKNOWN)
    return row


def _add(total, value):
    return value if total is None else total if value is None else total + value


def _child_row(root, cid, entry, cards, cards_rev):
    """(summary, models) for one subagent: its own analysis when the record or a project copy names
    its transcript, else the board's light reader over the file the family scan found."""
    try:
        analysis = _analysis_row(root, cid)
        return analysis["summary"], analysis["models"]
    except (KeyError, ValueError, OSError):
        pass
    if not entry.get("path"):
        return None, []
    from alpaca.analytics import burn
    try:
        light = burn.summary(entry["path"], cards, cards_rev)
    except OSError:
        return None, []
    return {"responses": light["responses"], "costed_responses": light["costed_responses"],
            "usage_responses": light["responses"], "estimated_cost_usd": light["estimated_cost_usd"],
            "tokens": {"total_tokens": light["total_tokens"]}}, []


def _families(root):
    from alpaca import db
    from alpaca.analytics import children
    conn = db.connect_readonly(root)
    try:
        return children.discover(root, conn), ratecards.load(conn), ratecards.revision(root, conn=conn)
    finally:
        conn.close()


def index(root, sessions, families=None, *, yield_control=None):
    # One connection, one obs_source read and one rate-card read for the whole run (t-577).
    with detail.revision_scope(root), detail.revision_batch(root):
        return _index(root, sessions, families, yield_control=yield_control)


def _index(root, sessions, families, *, yield_control):
    """One row per work session. t-143: a subagent is never a row of its own; each parent row
    carries `subagents` (its children's measurements) and `family` (main plus children), and the
    project totals cover main conversations and every readable subagent."""
    rows = []
    analysed = []
    totals = {"measured_sessions": 0, "total_tokens": None, "responses": 0,
              "estimated_cost_usd": None, "costed_responses": 0, "partial_sessions": 0,
              "listed_sessions": 0, "unavailable_sessions": 0, "complete_sessions": 0,
              "main_total_tokens": None, "main_estimated_cost_usd": None,
              "children_total_tokens": None, "children_estimated_cost_usd": None,
              "subagents": 0, "subagents_measured": 0}
    try:
        found, cards, cards_rev = _families(root) if families is None else (families, {}, "")
    except Exception:
        found, cards, cards_rev = {}, {}, ""
    folded = {child for kids in found.values() for child in kids}
    for session in sessions:
        if session.get("class", "work") != "work" or session.get("sid") in folded:
            continue
        if yield_control is not None:
            yield_control()
        totals["listed_sessions"] += 1
        row = {key: session.get(key) for key in ("sid", "title", "first", "last", "operator")}
        try:
            analysis = _analysis_row(root, session["sid"])
            analysed.append((session["sid"], {"models": analysis["models"]}))
            row.update({key: analysis[key] for key in ("summary", "coverage", "cost")})
            summary = analysis["summary"]
            row["operator"] = analysis["operator"] or row["operator"]
            totals["responses"] += summary["responses"]
            totals["costed_responses"] += summary["costed_responses"]
            total = (summary["tokens"] or {}).get("total_tokens")
            if total is not None and summary["usage_responses"]:
                totals["measured_sessions"] += 1
                totals["total_tokens"] = (totals["total_tokens"] or 0) + total
            cost = summary["estimated_cost_usd"]
            if cost is not None:
                totals["estimated_cost_usd"] = (totals["estimated_cost_usd"] or 0) + cost
            if analysis["coverage"].get("state") == "unavailable":
                totals["unavailable_sessions"] += 1
            if analysis["coverage"].get("complete"):
                totals["complete_sessions"] += 1
            if not analysis["coverage"].get("complete"):
                totals["partial_sessions"] += 1
            totals["main_total_tokens"] = _add(totals["main_total_tokens"], total if summary["usage_responses"] else None)
            totals["main_estimated_cost_usd"] = _add(totals["main_estimated_cost_usd"], cost)
            main_tokens, main_cost, main_responses = (total if summary["usage_responses"] else None), cost, summary["responses"]
            main_costed = summary["costed_responses"]
        except (KeyError, ValueError, OSError):
            totals["partial_sessions"] += 1
            totals["unavailable_sessions"] += 1
            row.update(coverage={"state": "unavailable"}, error="No readable analytics source for this session.")
            main_tokens = main_cost = None
            main_responses = main_costed = 0
        kids = found.get(session["sid"]) or {}
        sub = {"count": len(kids), "measured": 0, "unreadable": 0,
               "workflow": sum(entry.get("kind") == "workflow" for entry in kids.values()),
               "subagent": sum(entry.get("kind") != "workflow" for entry in kids.values()),
               "total_tokens": None, "estimated_cost_usd": None, "responses": 0, "costed_responses": 0}
        for cid, entry in sorted(kids.items()):
            if yield_control is not None:
                yield_control()
            summary, models = _child_row(root, cid, entry, cards, cards_rev)
            if summary is None:
                sub["unreadable"] += 1
                continue
            sub["measured"] += 1
            if models:
                analysed.append((cid, {"models": models}))
            ctotal = (summary.get("tokens") or {}).get("total_tokens")
            sub["total_tokens"] = _add(sub["total_tokens"], ctotal if summary.get("usage_responses") else None)
            sub["estimated_cost_usd"] = _add(sub["estimated_cost_usd"], summary.get("estimated_cost_usd"))
            sub["responses"] += summary.get("responses") or 0
            sub["costed_responses"] += summary.get("costed_responses") or 0
        row["subagents"] = sub
        row["family"] = {"total_tokens": _add(main_tokens, sub["total_tokens"]),
                         "estimated_cost_usd": _add(main_cost, sub["estimated_cost_usd"]),
                         "responses": main_responses + sub["responses"],
                         "costed_responses": main_costed + sub["costed_responses"]}
        totals["subagents"] += sub["count"]
        totals["subagents_measured"] += sub["measured"]
        totals["children_total_tokens"] = _add(totals["children_total_tokens"], sub["total_tokens"])
        totals["children_estimated_cost_usd"] = _add(totals["children_estimated_cost_usd"], sub["estimated_cost_usd"])
        totals["total_tokens"] = _add(totals["total_tokens"], sub["total_tokens"])
        totals["estimated_cost_usd"] = _add(totals["estimated_cost_usd"], sub["estimated_cost_usd"])
        totals["responses"] += sub["responses"]
        totals["costed_responses"] += sub["costed_responses"]
        rows.append(row)
    # Pricing gaps: models that answered but have no rate card, each with the command that
    # records one, and a count of responses unpriced for any other reason.
    gaps = ratecards.tally(analysed)
    totals["unpriced_models"] = gaps["unpriced_models"]
    totals["unpriced_other"] = gaps["unpriced_other"]
    totals["incomplete_project"] = bool(totals["partial_sessions"])
    totals["denominator"] = ("listed work sessions; totals cover available main-conversation measurements "
                             "plus every readable subagent of those sessions")
    return {"sessions": rows, "totals": totals,
            "scope": "one row per main conversation; each row and the totals add its subagents"}


def family(root, sid):
    """Analyze discovered child files separately before adding non-overlapping totals."""
    parent = metrics.analyze(root, sid)
    rows = [{"id": None, "title": "Main conversation", **{key: parent[key] for key in ("summary", "coverage", "cost")}}]
    for child in parent["children"]:
        item = metrics.analyze(root, sid, child=child["id"])
        rows.append({"id": child["id"], "title": item["session"]["title"],
                     **{key: item[key] for key in ("summary", "coverage", "cost")}})
    amounts = [row["summary"]["estimated_cost_usd"] for row in rows if row["summary"]["estimated_cost_usd"] is not None]
    tokens = [(row["summary"]["tokens"] or {}).get("total_tokens") for row in rows]
    return {"sid": sid, "conversations": rows,
            "totals": {"estimated_cost_usd": sum(amounts) if amounts else None,
                       "total_tokens": sum(t for t in tokens if t is not None) if any(t is not None for t in tokens) else None,
                       "responses": sum(row["summary"]["responses"] for row in rows),
                       "costed_responses": sum(row["summary"]["costed_responses"] for row in rows)},
            "complete": all(row["coverage"].get("usage_total_is_exact") and
                            row["summary"]["responses"] == row["summary"]["costed_responses"] for row in rows)
                        and not parent["coverage"].get("children_truncated")
                        and parent["coverage"].get("session_complete", False)
                        and parent["coverage"].get("child_coverage", {}).get("state") == "complete",
            "scope": "Main conversation plus discovered child transcripts; client cost snapshots are not summed."}
