"""One session classification, read from the record and shared by every surface that counts.

The desktop app opens a session in this project every few minutes to run one local command and
closes it again a second later. Such a session fires `session-start` and `session-end`, beats
nothing, ends no turn and carries no prompt. Sixty of them bury the five that did the work: the
pad header counts them, the analytics list scrolls past them, and the level printed beside "last
session" belongs to whichever one opened most recently.

So the split is decided ONCE, here, and the pad (`alpaca/pad.py`), the data.json payload
(`alpaca/export.py`) and the analytics fold (`alpaca/analytics/build_index.py`) all ask this module
rather than each counting for itself. Three classes:

  * `service` - the synthetic ids the record writes under when no agent holds the pen: a domain
    profile's runner (alpaca/profile.py `events`), a bare CLI call, the instrument census, and the
    fallback for an unattributed write.
  * `work` - a session that beat, ended a turn, carried a prompt, ran a turn-level hook, or
    authored any event beyond the lifecycle set. A Codex session that checkpoints explicitly and
    never beats lands here on its `session-checkpoint` rows.
  * `probe` - a sitting that opened and CLOSED having done none of that. A session still open is
    work until it closes: it may yet do something, and the record cannot say it will not.

Everything here is a fold of the record. The clock is the record's own newest event, never the
wall clock, so two renders of one record agree and the projection freshness gate (M2.17) stays
green while nothing happens.
"""
from __future__ import annotations

import datetime
import json

WORK, PROBE, SERVICE = "work", "probe", "service"

#: the synthetic session ids. They are a writer, not a sitting, so they are never counted as
#: sessions and never listed beside one.
SERVICE_IDS = frozenset(("observability-collector", "cli", "instrument", "unknown"))

#: the kinds a session opens and closes with. A session whose whole record is these did nothing
#: but open and close.
LIFECYCLE_KINDS = frozenset(("init", "session-start", "session-end", "transcript-snapshot",
                             "style-rearm"))

#: kinds a closing session writes as a side effect: SessionEnd refreshes the profile projections,
#: so a session that did nothing of its own can still author row verdicts, gate runs and receipt
#: replays. They say the record moved, not that this session worked. A domain profile adds its
#: own (alpaca/profile.py `events`).
SIDE_EFFECT_KINDS = frozenset(("verdict", "run", "bridge", "capture-failed", "drain-failed",
                               "lease-expired", "analytics-failed"))


def _policy(conn):
    """(service ids, side-effect kinds) with the profile's additions (alpaca/profile.py `events`)."""
    from alpaca import profile
    try:
        extra = profile.for_conn(conn).events()
    except Exception:
        extra = {}
    return (SERVICE_IDS | frozenset(profile.listed(extra, "service_sessions")),
            SIDE_EFFECT_KINDS | frozenset(profile.listed(extra, "side_effect_kinds")))

#: hooks that only fire for a session that took a turn or ran a tool. Their cost rows
#: (`meta.hook-cost:<sid>:<hook>`) are a work signal for a session whose events were pruned or
#: never landed; the two lifecycle hooks are NOT in this set, so a probe stays a probe.
TURN_HOOKS = frozenset(("stop", "post_tool", "user_prompt", "pre_compact", "subagent_stop"))

#: the kinds a recent-events list leaves out. They repeat every few seconds and assert nothing a
#: reader is looking for.
#: kinds that arrive in bursts from one session (a sync judges every row at once). Neighbours
#: of one such kind are shown as one line with a count.
BURST_KINDS = frozenset(("verdict", "bridge", "claim"))

NOISE_KINDS = frozenset(("heartbeat", "turn-end", "transcript-snapshot", "style-rearm", "run",
                         "subagent-stop"))

#: a work session reads as active when its last beat is this close to the newest event on the
#: record.
ACTIVE_WINDOW_S = 600

#: how far back a closed op still counts as progress, in seconds of record time.
CLOSED_WINDOW_S = 24 * 3600


# ------------------------------------------------------------------------------ record time
def parse_ts(value):
    """An ISO stamp as a datetime, or None when it is absent or unparseable."""
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def seconds_between(later, earlier):
    """`later` minus `earlier` in seconds, or None when either side is missing or the two cannot
    be compared (one carries an offset and the other does not)."""
    a, b = parse_ts(later), parse_ts(earlier)
    if a is None or b is None:
        return None
    if (a.tzinfo is None) != (b.tzinfo is None):
        return None
    return (a - b).total_seconds()


def later_of(a, b):
    """The later of two stamps, preferring whichever parses when the other does not."""
    pa, pb = parse_ts(a), parse_ts(b)
    if pa is None:
        return b
    if pb is None:
        return a
    if (pa.tzinfo is None) != (pb.tzinfo is None):
        return a
    return a if pa >= pb else b


def record_now(conn):
    """The record's own clock: the timestamp of its newest event, or None on an empty record."""
    r = conn.execute("SELECT ts FROM events ORDER BY id DESC LIMIT 1").fetchone()
    return r[0] if r else None


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _blob(text):
    try:
        out = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


# ------------------------------------------------------------------------ the classification
def classify(conn) -> dict:
    """sid -> {class, operator, started, ended, last_beat, beats, turns, prompts, level, events,
    last_tool, last_ref, subagent_stops} for every session the record knows.

    Five grouped queries, whatever the session count: this runs on every server tick, so nothing
    here is per-session.
    """
    service_ids, side_effects = _policy(conn)
    sessions = {r["sid"]: dict(r) for r in conn.execute("SELECT * FROM sessions")}
    kinds = {}
    for r in conn.execute("SELECT session, kind, COUNT(*) n FROM events GROUP BY session, kind"):
        kinds.setdefault(r[0], {})[r[1]] = r[2]
    meta = {}
    hooks = {}
    for r in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'operator:%' "
                          "OR key LIKE 'prompts:%' OR key LIKE 'hook-cost:%'"):
        key = r[0]
        if key.startswith("hook-cost:"):
            part = key.split(":")
            if len(part) >= 3:
                hooks.setdefault(part[1], set()).add(part[2])
        else:
            meta[key] = r[1]
    beats = {}
    for r in conn.execute(
            "SELECT e.session, e.ts, e.data FROM events e JOIN "
            "(SELECT session, MAX(id) mid FROM events WHERE kind='heartbeat' GROUP BY session) m "
            "ON e.id = m.mid"):
        beats[r[0]] = (r[1], _blob(r[2]))
    out = {}
    for sid in set(sessions) | set(kinds):
        row = sessions.get(sid) or {}
        kind = kinds.get(sid) or {}
        prompts = _int(meta.get("prompts:%s" % sid))
        ran = hooks.get(sid) or set()
        closed = bool(kind.get("session-end")) or bool(row.get("ended"))
        worked = bool(kind.get("heartbeat") or kind.get("turn-end") or prompts > 0
                      or (ran & TURN_HOOKS)
                      or any(name not in LIFECYCLE_KINDS and name not in side_effects
                             for name in kind))
        if sid in service_ids:
            klass = SERVICE
        elif worked or not closed:
            klass = WORK
        else:
            klass = PROBE
        beat_ts, beat_data = beats.get(sid, (None, {}))
        out[sid] = {
            "class": klass,
            "operator": meta.get("operator:%s" % sid),
            "started": row.get("started"),
            "ended": row.get("ended"),
            "last_beat": later_of(beat_ts, row.get("last_beat")),
            "beats": kind.get("heartbeat", 0),
            "turns": kind.get("turn-end", 0),
            "prompts": prompts,
            "level": row.get("level"),
            "events": sum(kind.values()),
            "last_tool": beat_data.get("tool"),
            "last_ref": beat_data.get("ref"),
            "subagent_stops": kind.get("subagent-stop", 0),
            # did this session do anything beyond opening and closing? A session that has not
            # closed yet is work either way, so this is what tells the two apart.
            "worked": worked,
        }
    return out


def class_of(view, sid) -> str:
    """The class of `sid`. A sid the record never named is service when it is a synthetic id and
    work otherwise: a transcript on disk under an unknown id is a sitting that did happen."""
    info = view.get(sid)
    if info:
        return info["class"]
    return SERVICE if sid in SERVICE_IDS else WORK


def last_activity(info):
    """The latest stamp a session carries: its last beat, else its end, else its start."""
    return later_of(later_of(info.get("last_beat"), info.get("ended")), info.get("started"))


def _rank(view, sid):
    stamp = last_activity(view[sid]) or ""
    return (str(stamp), str(sid))


def of_class(view, klass) -> list:
    """The sids of one class, newest activity first."""
    return sorted((sid for sid, info in view.items() if info["class"] == klass),
                  key=lambda sid: _rank(view, sid), reverse=True)


def leader(view):
    """The one work session a header should name: the most recently active session that has
    actually done something, else simply the most recently active. Without this a session the
    desktop app opened a second ago, and has not closed yet, would be the project's last session.
    """
    work = of_class(view, WORK)
    return next((sid for sid in work if view[sid]["worked"]), work[0] if work else None)


def is_active(info, now_ts=None, window_s=ACTIVE_WINDOW_S) -> bool:
    """Online liveness, or a historical query when an explicit as-of is supplied.

    Read views should omit now_ts. Deterministic saved projections may supply it.
    A closed session and a heartbeat over one second in the future are not live.
    """
    if info["class"] != WORK or info.get("ended"):
        return False
    if now_ts is None:
        now_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    gap = seconds_between(now_ts, info.get("last_beat"))
    return gap is not None and -1 <= gap <= window_s


def probe_window(view) -> dict:
    """{count, first, last} over the probe sessions: how many there were and the span they cover."""
    stamps = []
    count = 0
    for info in view.values():
        if info["class"] != PROBE:
            continue
        count += 1
        for key in ("started", "ended", "last_beat"):
            if info.get(key):
                stamps.append(str(info[key]))
    stamps.sort()
    return {"count": count, "first": stamps[0] if stamps else None,
            "last": stamps[-1] if stamps else None}


# ------------------------------------------------------------------------------- the claims
def claims_by_session(conn, now_ts=None) -> dict:
    """sid -> the row ids that session holds a live claim on, sorted.

    Latest claim / claim-release per row wins, the way `board.live_claims` folds it, but keyed on
    the session that authored the claim and expired against the record clock rather than the wall
    clock.
    """
    latest = {}
    for r in conn.execute("SELECT id, session, kind, ref, data FROM events "
                          "WHERE kind IN ('claim','claim-release') AND ref IS NOT NULL "
                          "ORDER BY id"):
        latest[r["ref"]] = r
    settled = {r[0] for r in conn.execute("SELECT id FROM tasks WHERE status='done'")}
    out = {}
    for ref, r in latest.items():
        if r["kind"] != "claim" or ref in settled:
            continue                                    # a finished task is nobody's to hold
        lease = _blob(r["data"]).get("lease_until")
        gap = seconds_between(now_ts, lease) if now_ts else None
        if gap is not None and gap >= 0:
            continue                                    # the lease ran out in record time
        out.setdefault(r["session"], []).append(ref)
    for ids in out.values():
        ids.sort()
    return out


# ------------------------------------------------------------------------------ op progress
def op_progress(conn, cards=None, now_ts=None) -> list:
    """One row per op: its tracker task counts and its obligation row counts.

    Open ops first, newest opened first, then the ops closed within the last day of record time,
    newest closed first. `cards` is `board.view(conn)["cards"]`; pass the caller's own so the
    board is folded once per render.
    """
    if cards is None:
        from alpaca import board
        cards = board.view(conn)["cards"]
    rows = {}
    for card in cards:
        slot = rows.setdefault(card.get("op"), {"total": 0, "done": 0, "blocked": 0})
        slot["total"] += 1
        if card.get("column") == "done":
            slot["done"] += 1
        elif card.get("column") == "blocked":
            slot["blocked"] += 1
    tasks = {}
    for r in conn.execute("SELECT op, status, COUNT(*) n FROM tasks GROUP BY op, status"):
        tasks.setdefault(r[0], {})[r[1]] = r[2]
    now_ts = now_ts if now_ts is not None else record_now(conn)
    out = []
    for r in conn.execute("SELECT id, intent, done_when, status, opened, closed FROM ops"):
        op = dict(r)
        if op["status"] != "open":
            gap = seconds_between(now_ts, op["closed"])
            if gap is None or gap > CLOSED_WINDOW_S:
                continue
        by_status = tasks.get(op["id"], {})
        row = rows.get(op["id"], {"total": 0, "done": 0, "blocked": 0})
        out.append({"id": op["id"], "title": op["intent"], "status": op["status"],
                    "done_when": op["done_when"],
                    "tasks_total": sum(by_status.values()),
                    "tasks_done": by_status.get("done", 0),
                    "tasks_doing": by_status.get("doing", 0),
                    "tasks_open": by_status.get("open", 0),
                    "rows_total": row["total"], "rows_done": row["done"],
                    "rows_blocked": row["blocked"],
                    "_sort": str(op["closed"] or op["opened"] or "")})
    out.sort(key=lambda o: (o["status"] == "open", o["_sort"], o["id"]), reverse=True)
    for o in out:
        o.pop("_sort")
    return out


# ---------------------------------------------------------------------------- recent events
def recent_events(conn, view, limit=25) -> list:
    """The newest `limit` events worth reading, newest first.

    Out: the kinds that repeat on their own (`NOISE_KINDS`), and the open / close pair of a probe
    session. Scans backwards in bounded pages so a run of probe lifecycle rows cannot starve the
    list.
    """
    probes = {sid for sid, info in view.items() if info["class"] == PROBE}
    placeholder = ",".join("?" * len(NOISE_KINDS))
    kinds = sorted(NOISE_KINDS)
    out, cursor = [], None
    page = max(limit * 4, 100)
    while len(out) < limit:
        q = "SELECT id, ts, session, kind, op, ref, data FROM events WHERE kind NOT IN (%s)" % placeholder
        params = list(kinds)
        if cursor is not None:
            q += " AND id < ?"
            params.append(cursor)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(page)
        rows = [dict(r) for r in conn.execute(q, params)]
        if not rows:
            break
        cursor = rows[-1]["id"]
        for r in rows:
            if r["session"] in probes and r["kind"] in ("session-start", "session-end"):
                continue
            if out and r["kind"] in BURST_KINDS and out[-1]["kind"] == r["kind"] \
                    and out[-1]["session"] == r["session"]:
                # A sync judges every row at once: forty `verdict` lines say less than one line
                # with a count. The newest event of the burst keeps the line; the rest are counted.
                burst = out[-1].setdefault("burst", [dict(out[-1])])   # a copy: no dict inside itself
                burst.append(r)
                continue
            out.append(r)
            if len(out) >= limit:
                break
    return out[:limit]


def _data_of(event) -> dict:
    raw = event.get("data")                 # a raw row carries text, db.events carries a dict
    return raw if isinstance(raw, dict) else _blob(raw)


def _verdict_word(data) -> str:
    """The verdict of an event as a word. A row verdict stores a numeric code beside
    `verdict_name`; a profile event stores the word itself."""
    name = data.get("verdict_name")
    if isinstance(name, str) and name:
        return name.upper()
    value = data.get("verdict")
    if isinstance(value, str):
        return value.upper()
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            from alpaca.gates import verdict as vc
            return vc.name_of(value)
        except Exception:
            return "CODE-%d" % value
    return ""


def _burst_line(events) -> str:
    """One line for a burst of neighbours of one kind: how many, and the verdicts they carried."""
    tally = {}
    for e in events:
        word = _verdict_word(_data_of(e)) or "recorded"
        tally[word] = tally.get(word, 0) + 1
    parts = ", ".join("%d %s" % (n, word) for word, n in sorted(tally.items()))
    noun = {"verdict": "acceptance rows judged", "bridge": "row batches landed",
            "claim": "claims"}.get(events[0].get("kind"), "events")
    return "%d %s: %s" % (len(events), noun, parts)


def summary_of(event, prof=None) -> str:
    """One short plain sentence for an event, from the event's own data. Empty when the kind
    carries nothing a reader would want in a line. A kind generic Alpaca does not know is offered to
    the domain profile `prof` (alpaca/profile.py `summarize`) when one is given."""
    if event.get("burst"):
        return _burst_line(event["burst"])
    data = _data_of(event)
    kind = event.get("kind")
    ref = event.get("ref") or ""
    if kind == "verdict":
        return ("%s %s: %s" % (ref, _verdict_word(data), data.get("reason") or ""))[:160]
    if kind == "claim":
        return "%s claimed by %s" % (ref, data.get("worker") or "?")
    if kind == "decision":
        return ("%s: %s" % (data.get("kind") or "decision", data.get("choice") or ""))[:160]
    if kind == "session-checkpoint":
        return str(data.get("note") or "")[:160]
    if kind == "bridge":
        return "%s acceptance row(s) landed" % data.get("appended")
    if kind in ("session-start", "session-end"):
        return str(data.get("operator") or data.get("reason") or "")[:80]
    if kind == "task-move":
        return "%s %s -> %s" % (ref, data.get("from") or "?", data.get("to") or "?")
    if kind == "proof-report":
        return str(data.get("path") or "")
    if kind == "msg":
        return str(data.get("body") or "")[:120]
    if kind == "task-add":
        return str(data.get("statement") or "")[:120]
    if kind in ("op-open", "op-close"):
        return str(data.get("intent") or data.get("basis") or "")[:120]
    if prof is not None:
        try:
            return str(prof.summarize(dict(event, data=data)) or "")[:160]
        except Exception:
            return ""
    return ""


def noise_since(conn, view, event_id) -> dict:
    """{heartbeats, probe_sessions, snapshots} counted from `event_id` forward. The three kinds a
    reader is not looking for, reported as one line instead of as hundreds."""
    if event_id is None:
        return {"heartbeats": 0, "probe_sessions": 0, "snapshots": 0}
    counts = {}
    for r in conn.execute("SELECT kind, COUNT(*) n FROM events WHERE id >= ? AND kind IN "
                          "('heartbeat','transcript-snapshot') GROUP BY kind", (event_id,)):
        counts[r[0]] = r[1]
    probes = {sid for sid, info in view.items() if info["class"] == PROBE}
    seen = {r[0] for r in conn.execute("SELECT DISTINCT session FROM events WHERE id >= ?",
                                       (event_id,))}
    return {"heartbeats": counts.get("heartbeat", 0),
            "probe_sessions": len(probes & seen),
            "snapshots": counts.get("transcript-snapshot", 0)}
