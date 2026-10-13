"""alpaca/healthscore.py - the hub's health score out of 1000 (t-590).

The bar under Arthur in the Studio sidebar reads it from /hub/vitals.json. `score` is pure: it takes
the facts the server already holds and the time, and names every lost point with its reason. Five
parts make the 1000:

  page data                   250  the projection state: fresh 250, starting 100, stale 50, else 0
  hub speed                   250  p95 handler time of the page's data reads over the last 5 min:
                                   <= 0.5 s gives 250, >= 5 s gives 0, linear between; no reads, 250
  capture collector           200  fresh and alive 200; fresh with unknown liveness (a one-off collect,
                                   nothing proves it keeps running) 90; stale, stopped, unknown or
                                   missing 0, whatever the liveness
  capture backlog and issues  150  backlog past 1 MB costs up to 100 (linear to 100 MB); each open
                                   issue seen in the last hour costs 10, at most 50
  server errors               150  each failing server component with a failure in the last 10 min
                                   costs 50; a client that left mid-response is not a server error

A page that is not fresh loses at least 150, a fresh collector of unknown liveness 110 and a stale,
stopped, unknown or missing one 200, so none of them can show the good band (900 and up);
test_healthscore.py checks every state, and the review's random probe finds none. Every lost
point is a loss with its reason, so the tooltip names each one. Inputs are clamped: a duration or
backlog that is not a number counts as the worst case, and a stamp in the future or one that
cannot be read counts as now (a fault that cannot be dated is no proof it is old). Missing child
sources are long-standing coverage gaps, not a fault of this hour: they are counted in a note and
not scored.
"""
from __future__ import annotations

import datetime as dt
import math

MAX = 1000
GOOD, ATTENTION = 900, 600
SPEED_WINDOW_S = 300
SPEED_FAST_S, SPEED_SLOW_S = 0.5, 5.0
ISSUE_WINDOW_S = 3600
FAILURE_WINDOW_S = 600
MB = 1000 * 1000
PAGE = {"fresh": 250, "starting": 100, "stale": 50}
#: Live.failures components and error types that mean the reader went away mid-response, not that the
#: server failed (serve.py CLIENT_GONE).
CLIENT_COMPONENTS = {"disconnect"}
CLIENT_ERRORS = {"BrokenPipeError", "ConnectionResetError", "ConnectionAbortedError"}


def band(value):
    return "good" if value >= GOOD else "attention" if value >= ATTENTION else "bad"


def p95(values):
    """Nearest-rank 95th percentile; None for no values."""
    values = sorted(values)
    return values[max(0, math.ceil(0.95 * len(values)) - 1)] if values else None


def _number(value):
    """A float for an int or float (inf kept), None for anything else: NaN, a string, a bool."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value):
        return None
    return float(value)


def _age(stamp, now):
    """Seconds since `stamp` (epoch seconds or ISO; a naive ISO is UTC, as observability._time
    reads it). A stamp in the future or one that cannot be read is 0: it counts as now."""
    at = _number(stamp)
    if at is None and isinstance(stamp, str):
        try:
            value = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            at = (value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)).timestamp()
        except ValueError:
            at = None
    if at is None or not math.isfinite(at):
        return 0.0
    return max(0.0, now - at)


def _part(key, label, most, losses):
    """A part from its losses [(points, reason)]: each is rounded and clamped to what is left, so the
    part never goes below 0 and the reasons add up to exactly the points lost."""
    left, kept = most, []
    for points, reason in losses:
        points = min(left, max(0, int(round(points))))
        if points:
            kept.append({"points": points, "reason": reason})
            left -= points
    return {"key": key, "label": label, "points": left, "max": most,
            "reasons": [loss["reason"] for loss in kept], "losses": kept}


def _page(page):
    state = (page or {}).get("state") if isinstance(page, dict) else None
    state = state if isinstance(state, str) and state else "unknown"
    detail = (page or {}).get("detail") if isinstance(page, dict) else None
    return _part("page", "page data", 250,
                 [(250 - PAGE.get(state, 0), "page data %s%s" % (state, ": " + detail if detail else ""))])


def _speed(samples, now, cap=None):
    recent = []
    for sample in samples or ():
        try:
            at, seconds = sample
        except (TypeError, ValueError):
            continue
        if _age(at, now) <= SPEED_WINDOW_S:
            seconds = _number(seconds)
            # a duration that is not a finite number counts as a read at the 0-point line; a
            # negative one as instant
            recent.append(SPEED_SLOW_S if seconds is None or not math.isfinite(seconds) else max(0.0, seconds))
    # Live keeps at most `cap` reads as a memory guard; a full ring younger than the window means the
    # p95 covers less than 5 min, and the reason says so.
    capped = bool(cap) and len(recent) >= cap
    p = p95(recent)
    if p is None or p <= SPEED_FAST_S:
        return _part("speed", "hub speed", 250, [])
    lost = 250 - 250 * max(0.0, SPEED_SLOW_S - min(p, SPEED_SLOW_S)) / (SPEED_SLOW_S - SPEED_FAST_S)
    return _part("speed", "hub speed", 250, [(lost, "p95 %.1f s over the last 5 min (%d reads%s)" % (
        min(p, 999.0), len(recent), ", the newest %d kept" % cap if capped else ""))])


def _collector(collector):
    collector = collector if isinstance(collector, dict) else {}
    status, liveness = collector.get("status"), collector.get("liveness")
    if status == "fresh" and liveness == "alive":
        return _part("collector", "capture collector", 200, [])
    if status == "fresh" and liveness == "unknown":
        return _part("collector", "capture collector", 200, [
            (110, "collector fresh, liveness unknown: a one-off collect, no watch process proves it keeps running")])
    age = _number(collector.get("age_seconds"))
    if status == "stale":
        known = age is not None and math.isfinite(age)
        why = "collector stale%s" % (": no heartbeat for %d min" % (age // 60) if known else "")
    elif status == "fresh":
        why = "collector fresh, liveness %s: its watch process is gone" % liveness
    else:
        why = "collector %s" % (status or "missing")
    return _part("collector", "capture collector", 200, [(200, why)])


def _capture(backlog, issues, opened, now):
    losses = []
    if backlog is not None:
        size = _number(backlog)
        size = math.inf if size is None else size
        if size > MB:
            # past 100 MB, or a backlog that is not a number, costs the full 100
            losses.append((100 * min(1.0, (size - MB) / (100 * MB - MB)),
                           "backlog %s" % ("%.1f MB" % (size / MB) if math.isfinite(size) else "unreadable")))
    seen = sum(1 for stamp in issues if _age(stamp, now) <= ISSUE_WINDOW_S)
    if seen:
        # The record keeps the newest 100 open issues' stamps (observability.summary); past that the
        # count reads as a floor.
        more = isinstance(opened, int) and opened > len(issues)
        losses.append((min(50, 10 * seen), "%d%s open issue%s seen in the last hour%s" % (
            seen, "+" if more and seen == len(issues) else "", "" if seen == 1 else "s",
            " (%d open in all)" % opened if more else "")))
    return _part("capture", "capture backlog and issues", 150, losses)


def _errors(failures, now):
    losses = []
    for component, entry in sorted((failures if isinstance(failures, dict) else {}).items()):
        last = entry.get("last") if isinstance(entry, dict) else None
        if not isinstance(last, dict) or component in CLIENT_COMPONENTS or last.get("type") in CLIENT_ERRORS:
            continue
        age = _age(last.get("at"), now)
        if age <= FAILURE_WINDOW_S:
            losses.append((50, "%s failed %d min ago (%s)" % (component, age // 60, last.get("type") or "error")))
    return _part("errors", "server errors", 150, losses)


def score(inputs, now):
    """{value, max, band, parts, notes} from {page, speed, speed_cap, collector, backlog, issues,
    issues_open, failures, missing_children}. `speed` is [(epoch, seconds)] and `speed_cap` the most
    Live keeps, `issues` the newest open issues' last_seen stamps and `issues_open` their full count,
    `failures` Live.failures; `now` is epoch seconds. Each part lists its losses: [{points, reason}]."""
    issues = inputs.get("issues")
    parts = [_page(inputs.get("page")), _speed(inputs.get("speed"), now, inputs.get("speed_cap")),
             _collector(inputs.get("collector")),
             _capture(inputs.get("backlog"), issues if isinstance(issues, (list, tuple)) else [],
                      inputs.get("issues_open"), now),
             _errors(inputs.get("failures"), now)]
    value = sum(p["points"] for p in parts)
    missing = inputs.get("missing_children")
    notes = (["%d missing child sources (long-standing coverage gaps)" % missing]
             if isinstance(missing, int) and missing > 0 else [])
    return {"value": value, "max": MAX, "band": band(value), "parts": parts, "notes": notes}
