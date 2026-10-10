"""The run budget check before each launch and the spend booking after it (FR-041).

A research run is uncapped by default (owner decision dec-36f614298507, in-session-runtime.md
section 21, t-297): a cap exists only when the run's contract sets it on purpose (a runbook, its
pack defaults file or a task override) or the owner grants one at a terminal. check() answers GO
or HOLD before a launch: HOLD only when a run cap is reached (US dollars, tokens, or wall time),
or when a launch of the run left spend nobody priced while a dollar or token cap is in force,
since such a cap cannot be checked against it (operator-surface.md section 9). The project
budget posture (alpaca/resilience/budget.py:101-104) never holds a research launch: the project
allowance in project.yaml was not set for research runs; research spend still books to it and
check() reports the posture. A verifier launch is never held: its spend is booked and counted,
and the next launch of another role meets the cap. The caps are read at
every call, never cached: each is the matching term of the newest terminal-confirmed budget
`research-grant` on the run that gives it a value, else the frozen contract's value as the
run-start event holds it (architecture.md sections 3.8 and 4.6).

book() books a priced launch with budget.spend, ref the attempt task (or the run for a launch
that serves no task), inside the transaction the launcher opened for `research-launch-end`, so a
launch is booked exactly when its end is recorded.

Launches in flight (FR-041, section 4.6): K slots and the verifier pool launch at once, and each
launch's own check reads only booked spend, so without more K launches could each spend the whole
remainder. reserve() therefore takes the check and the launch's dollar share in one step under a
process lock (one runner per run, section 4.2): the share is what is left of the cap after the
booked spend and every other launch in flight, divided over the launches that may still start
beside it, and the caller passes it to the launch as `max_usd` (a Claude launch's
--max-budget-usd). check() counts every launch in flight against the cap: a research-launch of
the run with no research-launch-end and no research-infra for its key reserves its recorded
`max_usd`, and a share reserve() handed out and not yet on the record reserves itself. A
research-launch whose key ended `lost` (research-infra class lost, no end) spent what nobody
priced, so totals() counts it unpriced. Unpriced spend counts until the owner's next
terminal-confirmed budget grant, which is how the owner lifts that hold (operator-surface
section 9).

Design: design/research-engine/architecture.md sections 3.8 and 4.6."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import json
import threading

from alpaca.research.types import SpendCheck

GO, HOLD = "GO", "HOLD"

#: runtimes whose launches spend nothing: a control launch reserves nothing and is never unpriced
_FREE_RUNTIMES = ("control",)
#: launches one slot may have in flight at once: its own agent and the two verifiers of one pool
#: worker (section 4.7: K slot threads and K verifier workers)
_PER_SLOT = 3
#: launch roles the budget check never holds (t-297, in-session-runtime.md section 21): a held
#: verifier leaves a sealed proof half verified; its spend is still booked and counted
NEVER_HELD_ROLES = ("verifier",)
#: a dollar share below this is no room for a launch (--max-budget-usd prints 4 decimals)
_MIN_SHARE_USD = 0.0001

_LOCK = threading.RLock()
#: run -> {launch key: (usd share, thread ident)}: shares handed out and not yet on the record
_PENDING = {}


def _rows(conn, sql, params=()) -> list:
    out = []
    for row in conn.execute(sql, params).fetchall():
        data = row["data"] if "data" in row.keys() else None
        try:
            parsed = json.loads(data) if data else {}
        except (TypeError, ValueError):
            parsed = {}
        kind = row["kind"] if "kind" in row.keys() else None
        out.append({"id": row["id"], "ts": row["ts"], "ref": row["ref"], "kind": kind,
                    "data": parsed})
    return out


def _run_start(conn, run: str) -> dict | None:
    got = _rows(conn, "SELECT id, ts, ref, data FROM events WHERE kind=? AND ref=? ORDER BY id "
                      "LIMIT 1", ("research-run-start", run))
    return got[0] if got else None


def _run_refs(conn, run: str) -> set:
    """The run id and every task the run added (task-add events whose research.run is the run)."""
    refs = {run}
    for row in conn.execute("SELECT ref FROM events WHERE kind='task-add' AND "
                            "json_extract(data, '$.research.run')=?", (run,)).fetchall():
        if row["ref"]:
            refs.add(row["ref"])
    return refs


def _grant_caps(conn, run: str) -> dict:
    """{usd, tokens, wall}: for each term, the value of the newest terminal-confirmed budget
    research-grant on the run that gives it a non-null value."""
    caps = {"usd": None, "tokens": None, "wall": None}
    grants = _rows(conn, "SELECT id, ts, ref, data FROM events WHERE kind=? AND ref=? ORDER BY id",
                   ("research-grant", run))
    for ev in grants:
        d = ev["data"]
        if d.get("gate") != "budget" or d.get("confirmed") != "terminal":
            continue
        terms = d.get("terms") if isinstance(d.get("terms"), dict) else {}
        for key in caps:
            value = terms.get(key)
            if value is not None:
                caps[key] = value
    return caps


def _parse_ts(text):
    try:
        got = datetime.datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    if got.tzinfo is None:
        got = got.replace(tzinfo=datetime.timezone.utc)
    return got


def wall_seconds(conn, run: str, *, now=None) -> float:
    """The seconds the run's runner processes have been alive: each `research-runner` event to
    the first `research-finalize` after it, or to `now` when none follows (time parked at a hold,
    between a finalize and the next start, does not count). `now` is a datetime; None reads the
    clock (alpaca.util.now_iso)."""
    from alpaca import util
    if now is None:
        now = _parse_ts(util.now_iso())
    rows = _rows(conn, "SELECT id, ts, ref, kind, data FROM events WHERE ref=? AND kind IN "
                       "('research-runner', 'research-finalize') ORDER BY id", (run,))
    total, open_at = 0.0, None
    for ev in rows:
        kind_is_runner = ev["kind"] == "research-runner"
        at = _parse_ts(ev["ts"])
        if at is None:
            continue
        if kind_is_runner:
            if open_at is None:
                open_at = at
        elif open_at is not None:
            total += max(0.0, (at - open_at).total_seconds())
            open_at = None
    if open_at is not None and now is not None:
        total += max(0.0, (now - open_at).total_seconds())
    return round(total, 3)


def _launch_ends(conn, run: str) -> list:
    return _rows(conn, "SELECT id, ts, ref, data FROM events WHERE kind='research-launch-end' AND "
                       "json_extract(data, '$.run')=? ORDER BY id", (run,))


def _control_launch(conn, launch_event) -> bool:
    if not isinstance(launch_event, int):
        return False
    row = conn.execute("SELECT data FROM events WHERE id=? AND kind='research-launch'",
                       (launch_event,)).fetchone()
    if row is None:
        return False
    try:
        return json.loads(row["data"] or "{}").get("runtime") == "control"
    except (TypeError, ValueError):
        return False


def _launches(conn, run: str) -> list:
    return _rows(conn, "SELECT id, ts, ref, data FROM events WHERE kind='research-launch' AND "
                       "json_extract(data, '$.run')=? ORDER BY id", (run,))


def _infra(conn, run: str) -> list:
    return _rows(conn, "SELECT id, ts, ref, data FROM events WHERE kind='research-infra' AND "
                       "json_extract(data, '$.run')=? ORDER BY id", (run,))


def _last_budget_grant(conn, run: str) -> int:
    """The id of the newest terminal-confirmed budget research-grant on the run, else 0."""
    top = 0
    for ev in _rows(conn, "SELECT id, ts, ref, data FROM events WHERE kind=? AND ref=? ORDER BY id",
                    ("research-grant", run)):
        d = ev["data"]
        if d.get("gate") == "budget" and d.get("confirmed") == "terminal":
            top = ev["id"]
    return top


def in_flight(conn, run: str) -> dict:
    """launch key -> the dollars it reserves, for every research-launch of the run with no
    research-launch-end and no research-infra for its key: its recorded `max_usd` (0 when it has
    none, and for a control launch, which spends nothing)."""
    closed = {ev["data"].get("key") for ev in _launch_ends(conn, run)}
    closed |= {ev["data"].get("key") for ev in _infra(conn, run)}
    out = {}
    for ev in _launches(conn, run):
        d = ev["data"]
        key = d.get("key")
        if not key or key in closed:
            continue
        usd = 0.0
        if d.get("runtime") not in _FREE_RUNTIMES:
            try:
                usd = max(0.0, float(d.get("max_usd") or 0.0))
            except (TypeError, ValueError):
                usd = 0.0
        out[key] = usd
    return out


def _reserved(conn, run: str) -> tuple:
    """(dollars reserved, launches reserving) by the launches of the run in flight: on the record
    (in_flight) and handed out by reserve() and not yet recorded, except the share this thread
    holds (the launch this thread is about to start checks against the others only)."""
    flying = in_flight(conn, run)
    usd = sum(flying.values())
    n = sum(1 for v in flying.values() if v > 0)
    me = threading.get_ident()
    with _LOCK:
        for key, (share, owner) in (_PENDING.get(run) or {}).items():
            if key in flying or owner == me:
                continue
            usd += share
            n += 1
    return round(usd, 6), n


def totals(conn, run: str) -> dict:
    """{usd_spent, tokens_spent, unpriced}: the booked dollars of the run and its tasks, the tokens
    of every launch end of the run, and how many launches of the run left spend unpriced since the
    owner's newest terminal-confirmed budget grant: an end with `priced` false that used tokens,
    or whose tokens nobody counted, and a launch that ended `lost` (a research-infra class lost
    and no research-launch-end: it ran, and what it spent was never read). A control launch
    spends nothing and is never unpriced."""
    refs = _run_refs(conn, run)
    usd = 0.0
    for row in conn.execute("SELECT ref, data FROM events WHERE kind='budget-spend'").fetchall():
        if row["ref"] not in refs:
            continue
        try:
            usd += float(json.loads(row["data"] or "{}").get("amount") or 0.0)
        except (TypeError, ValueError):
            continue
    tokens, unpriced = 0, 0
    granted = _last_budget_grant(conn, run)
    ends = _launch_ends(conn, run)
    for ev in ends:
        d = ev["data"]
        n = d.get("tokens")
        if isinstance(n, (int, float)) and not isinstance(n, bool):
            tokens += int(n)
        if d.get("priced") is False and (n is None or n) and ev["id"] > granted \
                and not _control_launch(conn, d.get("launch_event")):
            unpriced += 1
    ended = {ev["data"].get("key") for ev in ends}
    # a refused or late job of a session run that was priced from its transcript spent money the
    # judge read: its tokens count, and its key is no longer unpriced (in-session-runtime.md
    # section 9, "Priced without an end"); it never has a launch end, so it is counted once here
    priced = set()
    for ev in _rows(conn, "SELECT id, ts, ref, kind, data FROM events WHERE kind IN "
                          "('research-job-refused', 'research-job-late') AND "
                          "json_extract(data, '$.run')=? ORDER BY id", (run,)):
        d = ev["data"]
        if d.get("priced") is True and d.get("key"):
            n = d.get("tokens")
            if isinstance(n, (int, float)) and not isinstance(n, bool):
                tokens += int(n)
            if not d.get("stray"):
                # a stray agent's row adds its tokens; it does not price the job's own key, whose
                # agent may be unread
                priced.add(d["key"])
    lost = {}
    for ev in _infra(conn, run):
        if ev["data"].get("class") == "lost" and ev["data"].get("key"):
            lost.setdefault(ev["data"]["key"], ev["id"])
    for ev in _launches(conn, run):
        key = ev["data"].get("key")
        if key in lost and key not in ended and key not in priced and lost[key] > granted \
                and ev["data"].get("runtime") not in _FREE_RUNTIMES:
            unpriced += 1
            ended.add(key)                      # one count per key
    return {"usd_spent": round(usd, 6), "tokens_spent": int(tokens), "unpriced": unpriced}


def caps(conn, run: str) -> dict:
    """{usd, tokens, wall}: the caps check() reads, each the owner's newest raise when there is
    one, else the frozen contract's value from the run-start event's `budget`."""
    start = _run_start(conn, run)
    frozen = (start["data"].get("budget") if start else None) or {}
    granted = _grant_caps(conn, run)
    return {key: granted[key] if granted[key] is not None else frozen.get(key)
            for key in ("usd", "tokens", "wall")}


def check(conn, root: str, run: str | None, *, role: str) -> SpendCheck:
    """GO, or HOLD with the reason: a run cap reached (usd, tokens, or wall: the seconds the run's
    runner processes have been alive, from each research-runner event to its research-finalize or
    now, so time parked at a hold does not count), or unpriced spend in the run while a dollar or
    token cap is in force (operator-surface section 9). A run with no cap set on purpose is never
    held (t-297, in-session-runtime.md section 21): its unpriced launches are counted and
    reported, not held on. The project budget posture is read and reported as
    `project_posture` and never holds a research launch; with run None (a launch outside a run)
    the answer is GO. A launch of a role in NEVER_HELD_ROLES (the verifier) is always GO; the
    totals and caps are still read for it.
    The caps are read at every call, never cached: the dollar cap is the `usd` term of the
    newest research-grant on the run with gate "budget", confirmed "terminal" and a non-null
    `usd`, else the frozen contract's budget_usd (None when the contract sets none); the token
    cap likewise from the `tokens` term, else budget_tokens; the wall cap likewise from the
    `wall` term, else budget_wall_s. The grant's terms are {usd, tokens, wall} (operator-surface
    section 4.3.7). So the caps a `grant RUN budget --usd X --tokens T --wall S` sets are the caps
    the restarted runner checks and the caps the view shows (operator-surface section 7.3)."""
    from alpaca.resilience import budget
    project_posture = budget.posture(conn, root=root)          # reported only, never held on
    if run is None:
        return SpendCheck(posture=GO, reason="", usd_spent=0.0, tokens_spent=0,
                          usd_cap=None, tokens_cap=None, wall_s_spent=0.0, wall_cap_s=None,
                          unpriced=0, project_posture=project_posture)
    got = totals(conn, run)
    cap = caps(conn, run)
    wall = wall_seconds(conn, run)
    reserved, flying = _reserved(conn, run)
    reasons = []
    usd_cap = None if cap["usd"] is None else float(cap["usd"])
    tokens_cap = None if cap["tokens"] is None else int(cap["tokens"])
    wall_cap = None if cap["wall"] is None else int(cap["wall"])
    if usd_cap is not None and got["usd_spent"] >= usd_cap:
        reasons.append("the dollar cap is reached: %.4f of %.4f spent" % (got["usd_spent"], usd_cap))
    elif usd_cap is not None and got["usd_spent"] + reserved >= usd_cap:
        reasons.append("the dollar cap is committed: %.4f spent and %.4f reserved by %d launch(es) "
                       "in flight, of %.4f" % (got["usd_spent"], reserved, flying, usd_cap))
    if tokens_cap is not None and got["tokens_spent"] >= tokens_cap:
        reasons.append("the token cap is reached: %d of %d spent" % (got["tokens_spent"], tokens_cap))
    if wall_cap is not None and wall >= wall_cap:
        reasons.append("the wall-time cap is reached: %d of %d seconds" % (int(wall), wall_cap))
    if got["unpriced"] and (usd_cap is not None or tokens_cap is not None):
        reasons.append("%d launch(es) of the run left spend nobody priced" % got["unpriced"])
    if role in NEVER_HELD_ROLES:
        reasons = []
    return SpendCheck(posture=HOLD if reasons else GO, reason="; ".join(reasons),
                      usd_spent=got["usd_spent"], tokens_spent=got["tokens_spent"],
                      usd_cap=usd_cap, tokens_cap=tokens_cap, wall_s_spent=wall,
                      wall_cap_s=wall_cap, unpriced=got["unpriced"],
                      project_posture=project_posture)


@contextlib.contextmanager
def reserve(conn, root: str, run: str | None, key: str, *, role: str, runtime: str | None,
            slots: int):
    """Yields (SpendCheck, share): the check of one launch and, when it is GO under a dollar cap,
    the dollars reserved for it, taken together under the process lock, so two launches that
    start at once never both read the same remainder. The share is (cap - spent - reserved by
    the other launches in flight) / (the launches that may still start beside it: _PER_SLOT *
    slots minus those in flight), so the launches in flight never together pass the cap; below
    _MIN_SHARE_USD the check is HOLD (the cap is committed). The share is None outside a run,
    with no dollar cap, for a control runtime, which spends nothing, and for a role in
    NEVER_HELD_ROLES, which is never held. It stays reserved until
    the block ends; by then the launch's research-launch (its `max_usd`) holds it until its end."""
    if run is None:
        yield check(conn, root, None, role=role), None
        return
    me = threading.get_ident()
    with _LOCK:
        got = check(conn, root, run, role=role)
        share = None
        if got.posture == GO and got.usd_cap is not None and runtime not in _FREE_RUNTIMES \
                and role not in NEVER_HELD_ROLES:
            reserved, flying = _reserved(conn, run)
            free = float(got.usd_cap) - float(got.usd_spent) - reserved
            room = max(1, _PER_SLOT * max(1, int(slots)) - flying)
            share = round(free / room, 6)
            if share < _MIN_SHARE_USD:
                got = dataclasses.replace(got, posture=HOLD, reason=(
                    "the dollar cap is committed: %.4f spent and %.4f reserved by %d launch(es) "
                    "in flight, of %.4f" % (got.usd_spent, reserved, flying, got.usd_cap)))
                share = None
            else:
                _PENDING.setdefault(run, {})[key] = (share, me)
    try:
        yield got, share
    finally:
        with _LOCK:
            mine = _PENDING.get(run) or {}
            if key in mine and mine[key][1] == me:
                del mine[key]
            if not mine:
                _PENDING.pop(run, None)


def book(conn, run: str, task: str | None, *, cost_usd: float | None, tokens: int | None,
         session: str, in_txn: bool = True) -> None:
    """budget.spend(conn, cost_usd, ref=<task, or the run id for a launch that serves no task>)
    (alpaca/resilience/budget.py:78-82), the ref operator-surface section 8.1 uses. The launcher
    calls it inside the transaction that records research-launch-end, so a launch is booked
    exactly when its end is recorded and a resumed step never books twice. An unpriced launch books
    nothing and is counted as unpriced."""
    if cost_usd is None:
        return None
    from alpaca import db
    from alpaca.resilience import budget
    ref = task or run
    if in_txn:
        with db.transaction(conn):
            budget.spend(conn, float(cost_usd), session=session, actor="research-runner", ref=ref)
    else:
        budget.spend(conn, float(cost_usd), session=session, actor="research-runner", ref=ref)
    return None
