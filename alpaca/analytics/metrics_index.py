"""Compact project-scoped index of main-conversation analytics.

The server supplies its classified session list. Children stay separate and
client cost snapshots are deliberately not summed across overlapping scopes.

A project has more work sessions than metrics keeps whole analyses for (metrics.MAX_CACHE),
so index() keeps its own small row per session: only the pieces it reads, keyed on
metrics.analysis_key. An unchanged session is never analysed again; a changed transcript or a
newly recorded rate card changes the key and recomputes that row.
"""
from collections import OrderedDict
import copy
import threading

from alpaca.analytics import metrics, ratecards

MAX_ROWS = 2048
_ROWS = OrderedDict()
_ROWS_LOCK = threading.RLock()
_MODEL_KEYS = ("model", "provider", "responses", "costed_responses", "unpriced")


def _analysis_row(root, sid):
    """The pieces of metrics.analyze(root, sid) that index() needs, cached per analysis key."""
    key = metrics.analysis_key(root, sid)
    with _ROWS_LOCK:
        if key in _ROWS:
            _ROWS.move_to_end(key)
            return copy.deepcopy(_ROWS[key])
    analysis = metrics.analyze(root, sid)
    row = {"summary": analysis["summary"], "coverage": analysis["coverage"], "cost": analysis["cost"],
           "operator": analysis["session"].get("operator"),
           "models": [{name: model.get(name) for name in _MODEL_KEYS} for model in analysis.get("models") or []]}
    with _ROWS_LOCK:
        # One row per conversation: a new revision replaces the old one.
        for stale in [candidate for candidate in _ROWS if candidate[:3] == key[:3]]:
            del _ROWS[stale]
        _ROWS[key] = copy.deepcopy(row)
        while len(_ROWS) > MAX_ROWS:
            _ROWS.popitem(last=False)
    return row


def index(root, sessions):
    rows = []
    analysed = []
    totals = {"measured_sessions": 0, "total_tokens": None, "responses": 0,
              "estimated_cost_usd": None, "costed_responses": 0, "partial_sessions": 0,
              "listed_sessions": 0, "unavailable_sessions": 0, "complete_sessions": 0}
    for session in sessions:
        if session.get("class", "work") != "work":
            continue
        totals["listed_sessions"] += 1
        row = {key: session.get(key) for key in ("sid", "title", "first", "operator")}
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
        except (KeyError, ValueError, OSError):
            totals["partial_sessions"] += 1
            totals["unavailable_sessions"] += 1
            row.update(coverage={"state": "unavailable"}, error="No readable analytics source for this session.")
        rows.append(row)
    # Pricing gaps: models that answered but have no rate card, each with the command that
    # records one, and a count of responses unpriced for any other reason.
    gaps = ratecards.tally(analysed)
    totals["unpriced_models"] = gaps["unpriced_models"]
    totals["unpriced_other"] = gaps["unpriced_other"]
    totals["incomplete_project"] = bool(totals["partial_sessions"])
    totals["denominator"] = "listed work sessions; totals cover available main-conversation measurements"
    return {"sessions": rows, "totals": totals, "scope": "main conversations only"}


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
