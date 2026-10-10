"""Run and attempt state folds: run_ids, fold (the run headline the verbs, the report and the view
share), attempt_state and attempt_verdict (operator-surface.md section 8.1).

Every fold here reads the events table alone, and within it only the run's events: the research
events whose ref is the run or one of its tasks, the task events of those tasks and the events of
the run's ship card (operator-surface.md section 4.3.6). A task's status is the one its newest
status event gives (task-add open, task-move its `to`, claim and heartbeat doing, claim-release
and lease-expired open), never the tasks table, and no time is read from the clock, so a report
built over an exported copy of the rows (proofs.replay_report) gives the same bytes. With `root`
given, fold also sets budget.posture from the project's budget state (the view and `status`).

Design: design/research-engine/operator-surface.md sections 7.9 and 8.1, architecture.md sections
3.8 and 7.1."""

from __future__ import annotations

import json

from alpaca.research import events

# the attempt plan's steps and the lifecycle state each PASS reaches (architecture.md section 7.2)
_STEP_STATE = (("claim", "claimed"), ("open-window", "window-open"), ("propose", "proposed"),
               ("check-proposer", "proposal-checked"), ("isolate", "isolated"),
               ("implement", "implemented"), ("check-implementer", "checked"),
               ("capture", "captured"), ("purge", "purged"), ("gates", "gated"),
               ("judge", "judged"), ("audit", "audited"), ("notes", "noted"),
               ("close-window", "window-closed"), ("record", "recorded"),
               ("teardown", "torn-down"), ("seal", "sealed"))
_IN_FLIGHT = {"propose": ("proposer", "proposing"), "implement": ("implementer", "implementing")}
_STATUS_KINDS = ("task-add", "task-move", "claim", "heartbeat", "claim-release", "lease-expired")
_DEFAULT_RETRIES = 2


# ------------------------------------------------------------------ record reads
def _parse(row) -> dict:
    try:
        data = json.loads(row["data"] or "{}")
    except (TypeError, ValueError):
        data = {}
    return {"id": row["id"], "ts": row["ts"], "session": row["session"], "actor": row["actor"],
            "kind": row["kind"], "op": row["op"], "ref": row["ref"], "data": data}


def _events_of(conn, kind: str | None = None, ref: str | None = None) -> list:
    """Events in id order, data parsed, narrowed by kind and ref when given."""
    sql, params, where = "SELECT * FROM events", [], []
    if kind is not None:
        where.append("kind=?")
        params.append(kind)
    if ref is not None:
        where.append("ref=?")
        params.append(ref)
    if where:
        sql += " WHERE " + " AND ".join(where)
    return [_parse(r) for r in conn.execute(sql + " ORDER BY id", params).fetchall()]


def _run_start(conn, run: str) -> dict | None:
    got = _events_of(conn, events.RUN_START, run)
    return got[0] if got else None


def _run_tasks(conn, run: str) -> list:
    """The task-add events of the run's tasks, in add order."""
    rows = conn.execute("SELECT * FROM events WHERE kind='task-add' AND "
                        "json_extract(data, '$.research.run')=? ORDER BY id", (run,)).fetchall()
    return [_parse(r) for r in rows]


def _research(conn, task: str) -> dict:
    """The task-add event's `research` object of a run task, else {}."""
    row = conn.execute("SELECT data FROM events WHERE kind='task-add' AND ref=? ORDER BY id "
                       "LIMIT 1", (task,)).fetchone()
    if row is None:
        return {}
    try:
        got = json.loads(row["data"] or "{}").get("research")
    except (TypeError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _task_status(conn, task: str) -> str | None:
    """The status the task's newest status event gives, or None for no task-add."""
    status = None
    for ev in conn.execute("SELECT kind, data FROM events WHERE ref=? AND kind IN (%s) ORDER BY id"
                           % ",".join("?" * len(_STATUS_KINDS)),
                           (task,) + _STATUS_KINDS).fetchall():
        kind = ev["kind"]
        if kind == "task-add":
            status = "open"
        elif kind == "task-move":
            try:
                status = json.loads(ev["data"] or "{}").get("to") or status
            except (TypeError, ValueError):
                pass
        elif kind in ("claim", "heartbeat"):
            status = "doing"
        else:
            status = "open"
    return status


def _retries(conn, run: str | None) -> int:
    """research.runtime.retries of the run's frozen block (engine default 2)."""
    if not run:
        return _DEFAULT_RETRIES
    start = _run_start(conn, run)
    block = (start["data"].get("block") if start else None) or {}
    runtime = block.get("runtime") if isinstance(block, dict) else None
    value = runtime.get("retries") if isinstance(runtime, dict) else None
    try:
        return _DEFAULT_RETRIES if value is None else int(value)
    except (TypeError, ValueError):
        return _DEFAULT_RETRIES


# ------------------------------------------------------------------ run ids
def run_ids(conn) -> list[str]:
    """Every run with a research-run-start event, newest first."""
    rows = conn.execute("SELECT ref FROM events WHERE kind=? ORDER BY id DESC",
                        (events.RUN_START,)).fetchall()
    out = []
    for row in rows:
        if row["ref"] and row["ref"] not in out:
            out.append(row["ref"])
    return out


# ------------------------------------------------------------------ the attempt verdict
def _infra_spent(infra: list, retries: int) -> dict:
    """role -> the class of the last failed try, for each role whose failed tries passed the
    retry count (1 + retries tries in all)."""
    per_role: dict = {}
    for ev in infra:
        per_role.setdefault(ev["data"].get("role"), []).append(ev)
    return {role: evs[-1]["data"].get("class") for role, evs in per_role.items()
            if len(evs) > retries}


def _set(verdict: dict, value: str, outcome: str, broken_class=None, reason=None) -> None:
    verdict.update(verdict=value, outcome=outcome, broken_class=broken_class, reason=reason)


def attempt_verdict(conn, task: str) -> dict:
    """{verdict, outcome, broken_class, reason} as the record holds them now: the provisional
    verdict from the step events before research-attempt (research-proposal, research-tamper,
    research-worktree, research-diff, research-purge, research-gate, research-diff-refused,
    research-accept, the audit events, research-infra), the recorded one after it, and, after
    research-verify-end, the verify outcome (section 7.1)."""
    out = {"verdict": None, "outcome": None, "broken_class": None, "reason": None, "event": None}
    recorded = _events_of(conn, events.ATTEMPT, task)
    if recorded:
        d = recorded[-1]["data"]
        out.update(verdict=_verdict_of(d.get("outcome")),
                   outcome=d.get("outcome"), broken_class=d.get("broken_class"),
                   reason=d.get("reason"), event=recorded[-1]["id"])
    else:
        _provisional(conn, task, out)
    ends = _events_of(conn, events.VERIFY_END, task)
    if recorded and ends:
        outcome = ends[-1]["data"].get("outcome")
        if outcome == "refuted":
            out.update(verdict="broken", outcome="broken", broken_class="proof-refuted",
                       reason="proof-refuted")
        elif outcome == "unverified":
            out.update(verdict="rejected", outcome="untested", broken_class=None,
                       reason="proof-unverified")
    if recorded and not ends:
        stops = [e for e in _events_of(conn, events.VERIFY_REFUSED, task)
                 if e["data"].get("code")]
        moved = [e for e in _events_of(conn, "task-move", task) if e["data"].get("to") == "blocked"]
        if stops and moved and "verification stopped" in str(moved[-1]["data"].get("reason")):
            out.update(verdict="rejected", outcome="untested", broken_class=None,
                       reason=stops[-1]["data"].get("code"))
    return out


def accept_taken(conn, task: str) -> bool:
    """True when the accept rule took the attempt: a research-accept event of the task says
    accepted, or its research-attempt event records an improvement or a fix. A later verification
    that ends `refuted` or `unverified`, or an audit that blocks, rewrites attempt_verdict but
    not this fact. A control run reads it so a trust path that catches the attempt after the
    accept rule took it does not hide the broken gate or accept rule (FR-044)."""
    for ev in _events_of(conn, events.ACCEPT, task):
        if ev["data"].get("accepted") is True:
            return True
    for ev in _events_of(conn, events.ATTEMPT, task):
        if ev["data"].get("outcome") in ("improvement", "fixed"):
            return True
    return False


def _verdict_of(outcome):
    from alpaca.research.types import VERDICT_OF
    return VERDICT_OF.get(outcome)


def _provisional(conn, task: str, out: dict) -> None:
    research = _research(conn, task)
    retries = _retries(conn, research.get("run"))
    kinds = (events.PROPOSAL, events.TAMPER, events.WORKTREE, events.DIFF, events.PURGE,
             events.GATE, events.DIFF_REFUSED, events.ACCEPT, events.AUDIT_VERDICT, events.INFRA)
    rows = conn.execute("SELECT * FROM events WHERE ref=? AND kind IN (%s) ORDER BY id"
                        % ",".join("?" * len(kinds)), (task,) + kinds).fetchall()
    infra = []
    for row in rows:
        ev = _parse(row)
        kind, d = ev["kind"], ev["data"]
        unset = out["verdict"] is None
        if kind == events.PROPOSAL and d.get("valid") is False and unset:
            _set(out, "broken", "broken", "schema", "the proposal fails its shape")
        elif kind == events.TAMPER and d.get("verdict") not in (None, "PASS"):
            cls = "allowlist" if (d.get("outside") or d.get("protected") or d.get("nested")) \
                and d.get("point") == "after-implementer" and not d.get("changed") \
                and not d.get("missing") else "tamper"
            if unset or out["verdict"] != "broken":
                _set(out, "broken", "broken", cls, "fingerprint point %s" % d.get("point"))
        elif kind == events.WORKTREE and d.get("action") == "create" and d.get("error") and unset:
            _set(out, "broken", "broken", "diff", "the worktree could not be cut: %s"
                 % d.get("code"))
        elif kind == events.DIFF and unset:
            if d.get("error"):
                _set(out, "broken", "broken", "diff", "the diff could not be captured")
            elif d.get("outside") or d.get("protected") or d.get("nested"):
                _set(out, "broken", "broken", "allowlist", "the captured diff leaves the allowlist")
        elif kind == events.PURGE and d.get("verdict") not in (None, "PASS") and unset:
            _set(out, "broken", "broken", "purge", "the %s purge check failed" % d.get("phase"))
        elif kind == events.GATE and d.get("side") in (None, "attempt") and unset:
            if d.get("stopped"):
                # the runner's stop signal killed this gate: no verdict (EC-016); the resumed
                # `gates` step runs the item again
                continue
            if d.get("timed_out"):
                _set(out, "broken", "broken", "timeout", "gate %s timed out" % d.get("stage"))
            elif d.get("verdict") == "BLOCKED":
                _set(out, "broken", "broken", "gate-blocked", "gate %s could not run"
                     % d.get("stage"))
            elif d.get("verdict") == "FAIL":
                _set(out, "rejected", "gate-failed", None, "gate %s failed" % d.get("stage"))
        elif kind == events.DIFF_REFUSED:
            if unset or out["verdict"] != "broken":
                _set(out, "broken", "broken", "diff", "the FR-018 diff check refused the attempt")
        elif kind == events.ACCEPT and unset:
            outcome = d.get("outcome")
            _set(out, _verdict_of(outcome) or ("accepted" if d.get("accepted") else "rejected"),
                 outcome, None, d.get("reason"))
        elif kind == events.AUDIT_VERDICT:
            word = d.get("verdict")
            if word == "FAIL":
                _set(out, "broken", "broken", "audit", "the audit found a problem")
            elif word == "BLOCKED" and d.get("voided"):
                _set(out, "broken", "broken", "audit-tamper", "the audit was voided")
            elif word == "BLOCKED" and out["verdict"] != "broken":
                _set(out, "abandoned", "abandoned", None, "audit-blocked")
        elif kind == events.INFRA:
            infra.append(ev)
            spent = _infra_spent(infra, retries)
            role = d.get("role")
            if role in spent and (unset or role == "notes") and out["verdict"] != "broken":
                _set(out, "abandoned", "abandoned", None, spent[role])


# ------------------------------------------------------------------ the attempt state
def _passed_steps(conn, plan: str) -> list:
    rows = conn.execute("SELECT data FROM events WHERE kind='controller-step' AND ref LIKE ? "
                        "ORDER BY id", (plan + ":%",)).fetchall()
    out = []
    for row in rows:
        try:
            d = json.loads(row["data"] or "{}")
        except (TypeError, ValueError):
            continue
        if d.get("plan") == plan and d.get("verdict") == 0 and d.get("step") not in out:
            out.append(d.get("step"))
    return out


def attempt_state(conn, task: str) -> str:
    """The lifecycle state of section 7.1: the state the last passed step of the attempt plan
    reached, `proposing` or `implementing` while that launch has a start and no end, then the
    verification states of the latest seal, `merged` for a round winner, and `done` or `blocked`
    from the task's status events."""
    status = _task_status(conn, task)
    if status in ("done", "blocked"):
        return status
    passed = _passed_steps(conn, task)
    state = "planned"
    last = None
    for step, reached in _STEP_STATE:
        if step in passed:
            state, last = reached, step
    names = [s for s, _ in _STEP_STATE]
    nxt = names[names.index(last) + 1] if last in names and last != names[-1] else \
        (names[0] if last is None else None)
    if nxt in _IN_FLIGHT and state != "planned":
        role, flight = _IN_FLIGHT[nxt]
        starts = [e for e in _events_of(conn, events.LAUNCH, task) if e["data"].get("role") == role]
        ends = {e["data"].get("key") for e in _events_of(conn, events.LAUNCH_END, task)}
        if starts and starts[-1]["data"].get("key") not in ends:
            return flight
    if state == "planned" and status == "doing":
        state = "claimed"
    if state != "sealed":
        return state
    merged = [e for e in _events_of(conn, events.MERGE) if e["data"].get("attempt") == task]
    seal = conn.execute("SELECT id FROM events WHERE kind='proof-report' AND ref=? ORDER BY id DESC "
                        "LIMIT 1", (task,)).fetchone()
    if seal is not None:
        from alpaca.research import verifier
        vf = verifier.verification_id(task, seal["id"])
        ends = [e for e in _events_of(conn, events.VERIFY_END, task)
                if e["data"].get("verification") == vf]
        opens = [e for e in _events_of(conn, events.VERIFY_OPEN, task)
                 if e["data"].get("verification") == vf]
        if ends:
            outcome = ends[-1]["data"].get("outcome")
            if outcome == "verified":
                return "merged" if merged else "verified"
            if outcome in ("refuted", "unverified"):
                return outcome
        elif opens:
            return "verifying"
    return "merged" if merged else "sealed"


# ------------------------------------------------------------------ the run fold
def _run_events(conn, run: str, tasks: set) -> list:
    """The run's events (operator-surface section 4.3.6), minus research-report and
    research-status, in id order."""
    refs = {run} | set(tasks)
    out = []
    # Fetch only this run's references instead of materializing the whole
    # project history once per run. Chunk below SQLite's parameter limit.
    names = list(refs)
    for start in range(0, len(names), 500):
        part = names[start:start + 500]
        rows = conn.execute("SELECT * FROM events WHERE ref IN (%s) "
                            "AND kind NOT IN (?, ?) ORDER BY id"
                            % ",".join("?" * len(part)),
                            part + [events.REPORT, events.STATUS])
        out.extend(_parse(row) for row in rows)
    # Ship-card references are separate from the run/task IDs. Retain the
    # exact subject-prefix rule and avoid duplicating a directly owned row.
    for row in conn.execute("SELECT * FROM events WHERE kind IN "
                            "('review-card','review-move') ORDER BY id"):
        if row["ref"] in refs:
            continue
        ev = _parse(row)
        subject = str(ev["data"].get("subject") or "")
        if subject.startswith("research:%s:" % run):
            out.append(ev)
    return sorted(out, key=lambda ev: ev["id"])


def _metric_medians(metrics) -> dict:
    out = {}
    for mid, value in (metrics or {}).items():
        out[mid] = value.get("median") if isinstance(value, dict) else value
    return out


def _state_of(conn, run: str, evs: list, holds: list) -> str:
    """The state of operator-surface section 7.9, the first rule that holds."""
    resume_mark = 0
    for ev in evs:
        if ev["kind"] in (events.RESUME, events.RUNNER) and ev["ref"] == run:
            resume_mark = max(resume_mark, ev["id"])
    stops = [ev for ev in evs if ev["kind"] == events.STOP and ev["ref"] == run]
    finals = [ev for ev in evs if ev["kind"] == events.FINALIZE and ev["ref"] == run]
    lifts = [ev for ev in evs if ev["kind"] == events.HOLD_LIFTED and ev["ref"] == run]
    for ev in stops:
        if ev["data"].get("final") and ev["id"] > resume_mark:
            return "stopped"
    if finals and finals[-1]["id"] > resume_mark:
        fin = finals[-1]
        state = fin["data"].get("state")
        if state in ("halted", "paused"):
            later = [ev for ev in lifts if ev["id"] > fin["id"]]
            if later and later[-1]["data"].get("answer") != "stop":
                return "resumable"
        return state
    gates = {h["gate"] for h in holds}
    if "extension" in gates:
        return "halted"
    if "budget" in gates:
        return "paused"
    if any(ev["id"] > resume_mark for ev in stops):
        return "stopping"
    if not any(ev["kind"] == events.ATTEMPT for ev in evs):
        return "starting"
    return "running"


def _open_holds(evs: list, run: str) -> list:
    """The run's holds with no lift, from the run's events alone (a ship hold whose card event
    in the run's events is moved counts as answered)."""
    lifted = {ev["data"].get("hold") for ev in evs if ev["kind"] == events.HOLD_LIFTED}
    cards = {}
    for ev in evs:
        if ev["kind"] in ("review-card", "review-move"):
            cards[ev["data"].get("id")] = ev["data"].get("status")
    out = []
    for ev in evs:
        if ev["kind"] != events.HOLD or ev["id"] in lifted:
            continue
        d = ev["data"]
        if d.get("gate") == "ship" and cards.get(d.get("card")) not in (None, "open"):
            continue
        out.append({"id": ev["id"], "ref": d.get("ref"), "gate": d.get("gate"),
                    "code": d.get("code"), "detail": d.get("detail"), "card": d.get("card"),
                    "question": d.get("question"), "ts": ev["ts"]})
    return sorted(out, key=lambda h: h["id"], reverse=True)


def _budget(conn, run: str, evs: list, root: str | None) -> dict:
    from alpaca.research import spend
    got = spend.totals(conn, run)
    cap = spend.caps(conn, run)
    last = spend._parse_ts(evs[-1]["ts"]) if evs else None
    posture = None
    if root is not None:
        from alpaca.resilience import budget
        try:
            posture = budget.state(conn, root=root).get("posture")
        except Exception:  # noqa: BLE001 - an unreadable project.yaml shows no posture
            posture = None
    return {"usd_spent": got["usd_spent"], "tokens_spent": got["tokens_spent"],
            "wall_s_spent": spend.wall_seconds(conn, run, now=last),
            "usd_cap": cap["usd"], "tokens_cap": cap["tokens"], "wall_cap": cap["wall"],
            "unpriced": got["unpriced"], "posture": posture}


def _repair_items(conn, run: str, adds: list) -> list:
    out = []
    for add in adds:
        research = add["data"].get("research") or {}
        if research.get("role") != "repair":
            continue
        task = add["ref"]
        prep = _events_of(conn, events.PREPARE, task)
        repro = _events_of(conn, events.REPRODUCER, task)
        base = _events_of(conn, events.BASELINE, task)
        result = _events_of(conn, events.REPAIR_RESULT, task)
        status = _task_status(conn, task)
        outcome = result[-1]["data"].get("outcome") if result else None
        fix = None
        if outcome == "fixed":
            d = result[-1]["data"]
            fix = {"attempt": d.get("fix_attempt"), "commit": d.get("head"),
                   "round": (_research(conn, d.get("fix_attempt")) or {}).get("round")
                   if d.get("fix_attempt") else None}
        blocked_reason = None
        if status == "blocked":
            moves = [e for e in _events_of(conn, "task-move", task) if e["data"].get("to") == "blocked"]
            blocked_reason = moves[-1]["data"].get("reason") if moves else None
        base_commit = None
        if prep and prep[-1]["data"].get("ok"):
            base_commit = prep[-1]["data"].get("head")
        pts = (base[-1]["data"].get("pass_to_pass") or {}) if base else {}
        out.append({"task": task, "index": research.get("index"), "case": research.get("case"),
                    "state": status, "base_commit": base_commit, "prepared": bool(prep),
                    "reproducer_proved": bool(repro and repro[-1]["data"].get("proved")),
                    "base_pass": len(pts.get("base_pass") or ()) if base else None,
                    "outcome": outcome, "fix": fix,
                    "resolved": outcome == "fixed" and status == "done",
                    "blocked_reason": blocked_reason})
    out.sort(key=lambda item: (item["index"] is None, item["index"]))
    return out


def fold(conn, run: str, root: str | None = None) -> dict:
    """The keys of operator-surface section 8.1. An improve run has one line: `champion` is the
    newest research-merge of research/<run> (else the base) and `baseline` the round 0
    research-round. A repair run has no round 0 and one line per repair task, so its `champion`
    and `baseline` are None and the fold adds `repair`, one item per repair task in index
    order: {task, index, case, state (the task's status), base_commit (the prepared commit of
    its research-prepare, else its clone's root commit), prepared (bool), reproducer_proved,
    base_pass (the count in its research-baseline), outcome (its research-repair-result outcome,
    or None while it runs), fix ({attempt, commit, round} of the verified fix merged into
    research/<run>/<task>, or None), resolved (outcome `fixed` and the task done), blocked_reason
    (the reason of the newest task-move of the task to blocked while it is blocked, else None)}. `counts`
    covers the fix attempts of every repair task, and `round` is the current fix round of the
    repair task in progress. Raises KeyError for an unknown run."""
    start = _run_start(conn, run)
    if start is None:
        raise KeyError(run)
    sd = start["data"]
    adds = _run_tasks(conn, run)
    tasks = {a["ref"] for a in adds}
    evs = _run_events(conn, run, tasks)
    holds = _open_holds(evs, run)
    mode = sd.get("mode")
    counts: dict = {}
    control_failed = False
    for add in adds:
        research = add["data"].get("research") or {}
        if research.get("role") != "attempt":
            continue
        task = add["ref"]
        if sd.get("control") and not research.get("baseline") and accept_taken(conn, task):
            control_failed = True
        got = attempt_verdict(conn, task)
        if got["event"] is None:
            if _task_status(conn, task) == "doing":
                counts["running"] = counts.get("running", 0) + 1
            continue
        key = got["outcome"] or "unknown"
        if got["reason"] in ("proof-refuted", "proof-unverified"):
            key = got["reason"]
        counts[key] = counts.get(key, 0) + 1
    counts.setdefault("running", 0)
    rounds = [ev for ev in evs if ev["kind"] == events.ROUND]
    opens = [ev for ev in evs if ev["kind"] == events.ROUND_OPEN]
    current = opens[-1]["data"].get("round") if opens else None
    champion = baseline = None
    repair = None
    if mode == "repair":
        repair = _repair_items(conn, run, adds)
    else:
        base0 = [ev for ev in rounds if ev["data"].get("round") == 0
                 and not ev["data"].get("repair_task")]
        if base0:
            b = base0[-1]["data"]
            baseline = {"commit": b.get("champion_commit"),
                        "metrics": _metric_medians(b.get("champion_metrics"))}
        merges = [ev for ev in evs if ev["kind"] == events.MERGE
                  and ev["data"].get("research_branch") == "research/%s" % run]
        if merges:
            m = merges[-1]["data"]
            att = _events_of(conn, events.ATTEMPT, m.get("attempt"))
            metrics = _metric_medians(att[-1]["data"].get("metrics")) if att else {}
            champion = {"commit": m.get("commit"), "round": m.get("round"),
                        "attempt": m.get("attempt"), "metrics": metrics}
        else:
            champion = {"commit": sd.get("base_commit"), "round": 0, "attempt": None,
                        "metrics": (baseline or {}).get("metrics") or {}}
    last = evs[-1] if evs else start
    # a session run (in-session-runtime.md section 3.4): who drives it and how many jobs are out
    from alpaca.research import jobs as jobs_mod
    drv = jobs_mod.newest_driver(conn, run)
    driver = None if drv is None else {"session": drv["data"].get("session"),
                                       "runtime": drv["data"].get("runtime"),
                                       "cause": drv["data"].get("cause"), "event": drv["id"]}
    return {"run": run, "op": start.get("op"), "mode": mode, "runbook": sd.get("runbook"),
            "control": sd.get("control"), "state": _state_of(conn, run, evs, holds),
            "round": current, "rounds_n": sd.get("n"), "slots_k": sd.get("k"),
            "counts": dict(sorted(counts.items())), "champion": champion, "baseline": baseline,
            "targets": sd.get("targets"), "holds": holds,
            "budget": _budget(conn, run, evs, root), "level": sd.get("level"),
            "via": sd.get("via"), "started_at": start["ts"], "last_event_id": last["id"],
            "last_event_at": last["ts"], "control_failed": control_failed, "repair": repair,
            "driver": driver, "jobs": jobs_mod.counts(conn, run) if drv is not None else None,
            "runtime_mode": sd.get("runtime_mode")}
