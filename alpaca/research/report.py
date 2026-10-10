"""The run report, and the ship hold for a finished run (operator-surface.md section 4.3.6).

`build` reads the events table alone, and within it only the run's events: the research events
whose ref is the run or one of its tasks, the task events of those tasks and the events of the
run's ship card, minus research-report and research-status (state._run_events, the same rows
proofs.export_report_events writes for the report task's replay). It never reads the tasks
table, a file under `root`, project.yaml, another run's events or the clock: times are event
`ts` values, verifier counts come from the tasks' own verification events, and hold and grant
decision ids from the `decision` key of research-grant and research-hold-lifted data. So the same
run events give the same bytes, and proofs.replay_report, which inserts the exported rows into an
empty database and calls build(conn, <its output folder>, run), rebuilds the sealed files.

`render` turns the report into pure ASCII Markdown; `write` opens the ship hold first when the
run is a finished real run with something to ship (an improve run whose champion differs from
its round 0 commit, a repair run with a resolved repair task; the rule runner.ship_rule applies
at finalize), then writes report.md and report.json to .alpaca/reports/research/<run>/ (or
out_dir inside the root) and records research-report. Files that hash as the newest
research-report already says are not rewritten and no second event is recorded.

Design: design/research-engine/operator-surface.md sections 4.3.6 and 4.7, architecture.md
sections 3.9 and 4.8 (steps 4 to 6)."""

from __future__ import annotations

import hashlib
import json
import os

from alpaca.research import events, paths, state

#: the report's Markdown and JSON as written; replay_report writes the JSON the same way
_JSON = dict(indent=1, sort_keys=True, ensure_ascii=True)


# ------------------------------------------------------------------ helpers
def _ascii(text) -> str:
    """Text with every non-ASCII character escaped, so the report is ASCII whatever the record
    holds."""
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def _of(evs: list, kind: str) -> list:
    return [ev for ev in evs if ev["kind"] == kind]


def _tally(values) -> dict:
    out: dict = {}
    for value in values:
        key = "unknown" if value is None else str(value)
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def _first_metric(metrics, mid):
    if not isinstance(metrics, dict):
        return None
    value = metrics.get(mid)
    return value.get("median") if isinstance(value, dict) else value


def _met(value, target, direction):
    if value is None or target is None:
        return None
    try:
        value, target = float(value), float(target)
    except (TypeError, ValueError):
        return None
    return value <= target if direction == "lower" else value >= target


def _ship_wanted(conn, run: str, fold: dict) -> bool:
    """The ship rule of operator-surface section 4.3.6: a real run, finished, with no ship hold
    at all yet, and something to ship."""
    if fold.get("control") or fold.get("state") != "finished":
        return False
    for ev in state._events_of(conn, events.HOLD, run):
        if ev["data"].get("gate") == "ship":
            return False
    if fold.get("mode") == "repair":
        return any(item.get("resolved") for item in fold.get("repair") or [])
    champ = (fold.get("champion") or {}).get("commit")
    start = state._run_start(conn, run)
    base = (fold.get("baseline") or {}).get("commit") or \
        ((start or {}).get("data") or {}).get("base_commit")
    return bool(champ) and champ != base


def _ship_reason(run: str, fold: dict) -> str:
    if fold.get("mode") == "repair":
        parts = []
        for item in fold.get("repair") or []:
            if item.get("resolved"):
                fix = item.get("fix") or {}
                parts.append("%s on %s at %s" % (item.get("task"),
                                                 "%s/%s" % (paths.research_branch(run),
                                                            item.get("task")),
                                                 fix.get("commit")))
        return "%s resolved %d repair task(s): %s; adopt the fixes together or one by one " \
               "outside the run" % (run, len(parts), "; ".join(parts))
    champ = fold.get("champion") or {}
    base = (fold.get("baseline") or {}).get("commit")
    return "%s holds the champion %s (round %s, attempt %s) against the round 0 commit %s; " \
           "nothing is pushed until the owner moves this card" % (
               paths.research_branch(run), champ.get("commit"), champ.get("round"),
               champ.get("attempt"), base)


def _jobs_summary(evs: list):
    """What the session runtime recorded (in-session-runtime.md sections 3.3 and 7): the jobs by
    outcome, the refused and late ones, the agent ids, the drivers and the driver audit rows.
    None for a run with no job event, so a report of a run that used no jobs is unchanged."""
    results = _of(evs, events.JOB_RESULT)
    refused = _of(evs, events.JOB_REFUSED)
    expired = _of(evs, events.JOB_EXPIRED)
    late = _of(evs, events.JOB_LATE)
    drivers = _of(evs, events.DRIVER)
    audits = _of(evs, events.DRIVER_AUDIT)
    if not (results or refused or expired or late or drivers or audits):
        return None
    agents = sorted({str(ev["data"].get("agent_id")) for ev in results + refused + late
                     if ev["data"].get("agent_id")})
    out = {
        "judged": {"accepted": len(results), "refused": len(refused), "expired": len(expired),
                   "late": len(late)},
        "outcomes": _tally(ev["data"].get("outcome") for ev in results),
        "refusals": _tally(((ev["data"].get("problems") or [{}])[0] or {}).get("check")
                           for ev in refused),
        "agent_ids": agents,
        "drivers": [{"event": ev["id"], "session": ev["data"].get("session") or ev.get("session"),
                     "cause": ev["data"].get("cause"), "runtime": ev["data"].get("runtime"),
                     "witness": ev["data"].get("witness"), "level": ev["data"].get("level")}
                    for ev in drivers],
        "driver_audit": [dict({"event": ev["id"], "session": ev["data"].get("session"),
                               "findings": len(ev["data"].get("findings") or [])},
                              **({"beyond": (ev["data"].get("beyond") or {}).get("count")}
                                 if isinstance(ev["data"].get("beyond"), dict) else {}),
                              **({"subagent_driving": (ev["data"].get("subagent_driving")
                                                       or {}).get("count")}
                                 if isinstance(ev["data"].get("subagent_driving"), dict) else {}))
                         for ev in audits]}
    strays = sorted({str(ev["data"].get("agent_id")) for ev in late
                     if ev["data"].get("stray") and ev["data"].get("agent_id")})
    if strays:
        # agents of the driver priced after the judge for a job it had already decided (R5)
        out["strays"] = strays
    return out


# ------------------------------------------------------------------ build
def build(conn, root, run_id) -> dict:
    """The report of `run_id` from the run's events alone (see the module docstring); `root` is
    not read. Raises KeyError for an unknown run."""
    fold = state.fold(conn, run_id)
    start = state._run_start(conn, run_id)
    sd = start["data"]
    adds = state._run_tasks(conn, run_id)
    tasks = [a["ref"] for a in adds]
    evs = state._run_events(conn, run_id, set(tasks))
    finals = [ev for ev in _of(evs, events.FINALIZE) if ev["ref"] == run_id]
    fin = finals[-1] if finals else None
    report = {
        "run": {"id": run_id, "op": start.get("op"), "mode": sd.get("mode"),
                "runbook": sd.get("runbook"), "contract_sha256": sd.get("contract_sha256"),
                "intent": sd.get("intent"), "override_diff": sd.get("override_diff") or [],
                "level": sd.get("level"), "via": sd.get("via"), "control": sd.get("control"),
                "research_branch": sd.get("research_branch"), "base_commit": sd.get("base_commit"),
                "k": sd.get("k"), "n": sd.get("n"), "started_at": start["ts"],
                "ended_at": fin["ts"] if fin else None},
        "end": {"state": fold.get("state"),
                "reason": fin["data"].get("reason") if fin else None,
                "code": fin["data"].get("code") if fin else None,
                "finalize_event": fin["id"] if fin else None},
        "attempts": fold.get("counts") or {},
        "control_failed": bool(fold.get("control_failed")),
    }
    targets = {t.get("metric"): t.get("value") for t in sd.get("targets") or []
               if isinstance(t, dict)}
    metric_rows = []
    if sd.get("mode") == "repair":
        items = fold.get("repair") or []
        report["repair"] = items
        report["resolved_rate"] = {"resolved": sum(1 for i in items if i.get("resolved")),
                                   "of": len(items)}
    else:
        champ = fold.get("champion") or {}
        base = fold.get("baseline") or {}
        for m in sd.get("metrics") or []:
            mid = m.get("id")
            cval = _first_metric(champ.get("metrics"), mid)
            metric_rows.append({"id": mid, "unit": m.get("unit"), "direction": m.get("direction"),
                                "baseline": _first_metric(base.get("metrics"), mid),
                                "champion": cval, "target": targets.get(mid),
                                "met": _met(cval, targets.get(mid), m.get("direction"))})
        first = (sd.get("metrics") or [{}])[0].get("id") if sd.get("metrics") else None
        report["metrics"] = metric_rows
        report["champion"] = {"commit": champ.get("commit"), "round": champ.get("round"),
                              "attempt": champ.get("attempt")}
        report["baseline"] = {"commit": base.get("commit")}
        report["rounds"] = [
            {"round": ev["data"].get("round"), "winner": ev["data"].get("winner"),
             "champion_commit": ev["data"].get("champion_commit"),
             "champion_value": _first_metric(ev["data"].get("champion_metrics"), first),
             "accepted": ev["data"].get("accepted"),
             "targets_met": ev["data"].get("targets_met"), "event": ev["id"]}
            for ev in _of(evs, events.ROUND) if not ev["data"].get("repair_task")]
        report["lineage"] = [
            {"round": ev["data"].get("round"), "attempt": ev["data"].get("attempt"),
             "commit": ev["data"].get("commit"), "parent": ev["data"].get("parent"),
             "event": ev["id"]}
            for ev in _of(evs, events.MERGE)
            if ev["data"].get("research_branch") == paths.research_branch(run_id)]
    tamper = _of(evs, events.TAMPER)
    report["verification"] = {
        "verify_end": _tally(ev["data"].get("outcome") for ev in _of(evs, events.VERIFY_END)),
        "proof_verdicts": _tally(ev["data"].get("verdict")
                                 for ev in _of(evs, events.PROOF_VERDICT)),
        "rulings": _tally(ev["data"].get("ruling") for ev in _of(evs, events.PROOF_RULING)),
        "audit": _tally(ev["data"].get("verdict") for ev in _of(evs, events.AUDIT_VERDICT)),
        "tamper": {"checks": len(tamper),
                   "changed": sum(1 for ev in tamper if ev["data"].get("changed"))}}
    jobs = _jobs_summary(evs)
    if jobs is not None:
        report["jobs"] = jobs
    lifts = {}
    for ev in _of(evs, events.HOLD_LIFTED):
        lifts.setdefault(ev["data"].get("hold"), ev)
    cards = {}
    for ev in evs:
        if ev["kind"] in ("review-card", "review-move"):
            cid = ev["data"].get("id")
            cards[cid] = dict(cards.get(cid) or {}, **ev["data"])
    hold_rows, ship = [], None
    for ev in _of(evs, events.HOLD):
        d = ev["data"]
        lift = lifts.get(ev["id"])
        row = {"hold": ev["id"], "gate": d.get("gate"), "code": d.get("code"),
               "ref": d.get("ref"), "at": ev["ts"], "detail": d.get("detail"),
               "card": d.get("card"), "question": d.get("question"),
               "lifted": None if lift is None else {
                   "event": lift["id"], "decision": lift["data"].get("decision"),
                   "answer": lift["data"].get("answer"), "at": lift["ts"]}}
        hold_rows.append(row)
        if d.get("gate") == "ship":
            card = cards.get(d.get("card")) or {}
            ship = {"hold": ev["id"], "card": d.get("card"), "subject": card.get("subject"),
                    "status": card.get("status"), "decision": card.get("decision")}
    report["holds"] = hold_rows
    report["grants"] = [
        {"event": ev["id"], "gate": ev["data"].get("gate"), "hold": ev["data"].get("hold"),
         "decision": ev["data"].get("decision"), "terms": ev["data"].get("terms"),
         "confirmed": ev["data"].get("confirmed"), "at": ev["ts"]}
        for ev in _of(evs, events.GRANT)]
    budget = dict(fold.get("budget") or {})
    budget.pop("posture", None)
    report["budget"] = budget
    report["ship"] = ship
    report["reproduce"] = [
        "bin/alpaca research report %s" % run_id,
        "python3 -m alpaca.research.proofs replay-report --run %s --events "
        ".alpaca/reports/research/%s/events.json --out <folder>" % (run_id, run_id)]
    proofs = set()
    for ev in evs:
        if ev["ref"] not in tasks:
            continue
        if ev["kind"] == "task-move":
            ptr = ev["data"].get("proof")
            if isinstance(ptr, str) and ptr:
                proofs.add(ptr[len("local:"):] if ptr.startswith("local:") else ptr)
        elif ev["kind"] == "proof-report":
            ptr = ev["data"].get("path")
            if isinstance(ptr, str) and ptr:
                proofs.add(ptr)
    report["evidence"] = {
        "run_start": start["id"], "finalize": fin["id"] if fin else None,
        "holds": [h["hold"] for h in hold_rows], "grants": [g["event"] for g in report["grants"]],
        "tasks": tasks, "proofs": sorted(proofs)}
    report["through_event"] = max([ev["id"] for ev in evs] + [start["id"]])
    return report


# ------------------------------------------------------------------ render
def _cap(value) -> str:
    """A run cap for the report: "no cap" when none is set (a run is uncapped by default, t-297)."""
    return "no cap" if value is None else _cell(value)


def _cell(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=True)
    return str(value).replace("|", "/").replace("\n", " ")


def _table(head, rows) -> list:
    out = ["| %s |" % " | ".join(head), "|%s|" % "|".join("---" for _ in head)]
    for row in rows:
        out.append("| %s |" % " | ".join(_cell(v) for v in row))
    return out


def _audit_line(a: dict) -> str:
    """One driver audit row of the report: its findings, the agents beyond the jobs, and the
    driving verbs a subagent tried (the guard denies them, the audit counts them)."""
    return "- driver audit %s: %s finding(s) for %s%s" % (
        a.get("event"), a.get("findings"), _cell(a.get("session")),
        ("; %s agent(s) beyond the jobs" % a["beyond"] if a.get("beyond") is not None else "")
        + ("; %s subagent call(s) of a driving verb" % a["subagent_driving"]
           if a.get("subagent_driving") is not None else ""))


def render(report: dict) -> str:
    """The report as pure ASCII Markdown: the run, how it ended, the metrics (baseline, champion
    and target) with the rounds and the champion lineage, or the repair items and the resolved
    rate; attempts by outcome; verifier, audit and tamper counts; holds and grants with their
    decision ids; the budget; the ship card; how to reproduce; the evidence list."""
    run, end = report["run"], report["end"]
    out = ["# Research run %s" % run["id"], "",
           "## The run", ""]
    for key in ("mode", "runbook", "op", "contract_sha256", "intent", "level", "via", "control",
                "research_branch", "base_commit", "k", "n", "started_at", "ended_at"):
        out.append("- %s: %s" % (key, _cell(run.get(key))))
    diff = run.get("override_diff") or []
    if diff:
        out += ["", "Override diff:", ""]
        out += _table(("field", "pack", "task", "direction"),
                      [(d.get("field"), d.get("pack"), d.get("task"), d.get("direction"))
                       for d in diff])
    else:
        out.append("- override diff: none")
    out += ["", "## How it ended", "",
            "- state: %s" % _cell(end.get("state")),
            "- reason: %s" % _cell(end.get("reason")),
            "- code: %s" % _cell(end.get("code")),
            "- finalize event: %s" % _cell(end.get("finalize_event"))]
    if "repair" in report:
        rate = report.get("resolved_rate") or {}
        out += ["", "## Repair tasks", "",
                "Resolved %s of %s." % (rate.get("resolved"), rate.get("of")), ""]
        out += _table(("task", "case", "base", "base pass", "reproducer", "outcome", "fix",
                       "resolved"),
                      [(i.get("task"), i.get("case"), i.get("base_commit"), i.get("base_pass"),
                        i.get("reproducer_proved"), i.get("outcome"),
                        "%s at %s on %s/%s" % ((i.get("fix") or {}).get("attempt"),
                                               (i.get("fix") or {}).get("commit"),
                                               run.get("research_branch"), i.get("task"))
                        if i.get("fix") else None, i.get("resolved"))
                       for i in report["repair"]])
    else:
        out += ["", "## Metrics", ""]
        out += _table(("metric", "unit", "direction", "baseline", "champion", "target", "met"),
                      [(m.get("id"), m.get("unit"), m.get("direction"), m.get("baseline"),
                        m.get("champion"), m.get("target"), m.get("met"))
                       for m in report.get("metrics") or []])
        champ = report.get("champion") or {}
        out += ["", "Champion %s (round %s, attempt %s); round 0 commit %s." % (
            _cell(champ.get("commit")), _cell(champ.get("round")), _cell(champ.get("attempt")),
            _cell((report.get("baseline") or {}).get("commit"))), "", "## Rounds", ""]
        out += _table(("round", "winner", "champion value", "accepted", "targets met", "event"),
                      [(r.get("round"), r.get("winner"), r.get("champion_value"),
                        r.get("accepted"), r.get("targets_met"), r.get("event"))
                       for r in report.get("rounds") or []])
        out += ["", "## Champion lineage", ""]
        out += _table(("round", "attempt", "commit", "parent", "event"),
                      [(m.get("round"), m.get("attempt"), m.get("commit"), m.get("parent"),
                        m.get("event")) for m in report.get("lineage") or []])
    out += ["", "## Attempts by outcome", ""]
    out += _table(("outcome", "count"), sorted((report.get("attempts") or {}).items()))
    ver = report.get("verification") or {}
    out += ["", "## Verifier, audit and tamper", "",
            "- verify end: %s" % _cell(ver.get("verify_end")),
            "- proof verdicts: %s" % _cell(ver.get("proof_verdicts")),
            "- rulings: %s" % _cell(ver.get("rulings")),
            "- audit: %s" % _cell(ver.get("audit")),
            "- tamper checks: %s" % _cell(ver.get("tamper")),
            "- control failed: %s" % _cell(report.get("control_failed"))]
    jobs = report.get("jobs")
    if jobs:
        out += ["", "## Agent jobs", "",
                "- jobs judged: %s" % _cell(jobs.get("judged")),
                "- outcomes: %s" % _cell(jobs.get("outcomes")),
                "- refusals: %s" % _cell(jobs.get("refusals")),
                "- agents: %d (%s)" % (len(jobs.get("agent_ids") or []),
                                       ", ".join((jobs.get("agent_ids") or [])[:12]) or "none")]
        if jobs.get("strays"):
            out.append("- stray agents priced after the judge: %d (%s)" % (
                len(jobs["strays"]), ", ".join(jobs["strays"][:12])))
        out += ["- driver %s: %s, %s, witness %s, cause %s" % (
            d.get("event"), _cell(d.get("session")), _cell(d.get("runtime")),
            _cell(d.get("witness")), _cell(d.get("cause"))) for d in jobs.get("drivers") or []]
        out += [_audit_line(a) for a in jobs.get("driver_audit") or []]
    out += ["", "## Holds and grants", ""]
    out += _table(("hold", "gate", "code", "at", "detail", "lifted by", "answer"),
                  [(h.get("hold"), h.get("gate"), h.get("code"), h.get("at"), h.get("detail"),
                    (h.get("lifted") or {}).get("decision"), (h.get("lifted") or {}).get("answer"))
                   for h in report.get("holds") or []])
    out.append("")
    out += _table(("grant event", "gate", "hold", "decision", "terms", "at"),
                  [(g.get("event"), g.get("gate"), g.get("hold"), g.get("decision"),
                    g.get("terms"), g.get("at")) for g in report.get("grants") or []])
    bud = report.get("budget") or {}
    out += ["", "## Budget", "",
            "- dollars: %s spent of %s" % (_cell(bud.get("usd_spent")), _cap(bud.get("usd_cap"))),
            "- tokens: %s spent of %s" % (_cell(bud.get("tokens_spent")),
                                          _cap(bud.get("tokens_cap"))),
            "- wall seconds: %s of %s" % (_cell(bud.get("wall_s_spent")),
                                          _cap(bud.get("wall_cap"))),
            "- launches with unpriced spend: %s" % _cell(bud.get("unpriced")),
            "", "## Ship card", ""]
    ship = report.get("ship")
    if ship:
        out.append("- hold %s, card %s (%s): %s%s" % (
            ship.get("hold"), ship.get("card"), _cell(ship.get("subject")),
            _cell(ship.get("status")),
            ", decision: %s" % ship["decision"] if ship.get("decision") else ""))
    else:
        out.append("- none")
    out += ["", "## How to reproduce", ""]
    out += ["- `%s`" % cmd for cmd in report.get("reproduce") or []]
    evid = report.get("evidence") or {}
    out += ["", "## Evidence", "",
            "- event:%s - the run start" % evid.get("run_start")]
    if evid.get("finalize"):
        out.append("- event:%s - the finalize" % evid["finalize"])
    for h in evid.get("holds") or []:
        out.append("- event:%s - a hold" % h)
    for g in evid.get("grants") or []:
        out.append("- event:%s - an owner grant" % g)
    if evid.get("tasks"):
        out.append("- tasks: %s" % ", ".join(evid["tasks"]))
    for p in evid.get("proofs") or []:
        out.append("- local:%s - a proof report" % p)
    out += ["", "Built from the run's events through event %s." % report.get("through_event"), ""]
    return _ascii("\n".join(out))


# ------------------------------------------------------------------ write
def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: str):
    try:
        with open(path, "rb") as fh:
            return _sha(fh.read())
    except OSError:
        return None


def _write_bytes(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def _out_dir(root: str, run_id: str, out_dir) -> str:
    if out_dir is None:
        return paths.reports_dir(root, run_id)
    real_root = os.path.realpath(root)
    target = os.path.realpath(out_dir if os.path.isabs(out_dir) else os.path.join(root, out_dir))
    if target != real_root and not target.startswith(real_root + os.sep):
        raise ValueError("the report folder %s is outside the project root" % out_dir)
    return target


def write(conn, root, run_id, out_dir=None, *, session: str | None = None) -> dict:
    """Open the ship hold when the run is a finished real run with something to ship and has no
    ship hold yet (holds.open_hold, gate ship, code RUN-SHIP-DECISION, card subject
    research:<run>:champion or research:<run>:repair), then build and render the report and write
    report.md and report.json to .alpaca/reports/research/<run>/ (or out_dir, which must lie in
    the root), and record research-report {path, sha256, json_path, json_sha256, through_event,
    state}. When the two files would hash as the newest research-report says for the same
    paths, nothing is rewritten that already holds those bytes and no second event is recorded.
    `session` is the writer's session (the runner's research-<run> by default). Returns {path,
    sha256, json_path, json_sha256, through_event, new, event, hold}. Raises KeyError for an
    unknown run and ValueError for an out_dir outside the root."""
    from alpaca.research import holds
    root = os.path.realpath(root)
    session = session or events.runner_session(run_id)
    fold = state.fold(conn, run_id)
    hold = None
    if _ship_wanted(conn, run_id, fold):
        hold = holds.open_hold(conn, root, ref=run_id, gate="ship",
                               code=holds.HOLD_CODES["ship"], detail=_ship_reason(run_id, fold),
                               op=fold.get("op"), bound=None, session=session)
    built = build(conn, root, run_id)
    md = render(built).encode("ascii")
    js = (json.dumps(built, **_JSON) + "\n").encode("ascii")
    folder = _out_dir(root, run_id, out_dir)
    md_path, js_path = os.path.join(folder, "report.md"), os.path.join(folder, "report.json")
    rel = lambda p: os.path.relpath(p, root).replace(os.sep, "/")  # noqa: E731
    got = {"path": rel(md_path), "sha256": _sha(md), "json_path": rel(js_path),
           "json_sha256": _sha(js), "through_event": built["through_event"]}
    newest = state._events_of(conn, events.REPORT, run_id)
    newest = newest[-1] if newest else None
    same = newest is not None and all(newest["data"].get(k) == got[k] for k in
                                      ("path", "sha256", "json_path", "json_sha256"))
    for path, data in ((md_path, md), (js_path, js)):
        if _file_sha(path) != _sha(data):
            _write_bytes(path, data)
    event = newest["id"] if same else None
    if not same:
        ev = events.emit(conn, events.REPORT, run=run_id, ref=run_id, op=fold.get("op"),
                         session=session, data=dict(got, state=built["end"]["state"]))
        event = ev["id"]
    return dict(got, new=not same, event=event, hold=hold["id"] if hold else None)

