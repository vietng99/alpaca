"""The research view's JSON, read through a read-only connection (operator-surface.md section 7.2).

`runs(root)` returns the run list and `run(root, run_id)` one run in full (None for an unknown
run). Both open the record with db.connect_readonly inside one read transaction, as the hub's
overview does (alpaca/hub.py `_record`), and never db.connect: the view writes nothing. Both call
state.fold for the headline, so the view, `research status` and the report agree; the view passes
`root`, so budget.posture is set from the project's budget state.

A run object is the fold headline plus `attempts`, `rounds`, `lineage`, `chart`, `repair`,
`holds`, `grants`, `budget` and `sources`; `sources` maps every field name to its record source
(operator-surface.md section 7.3), and the page shows it as the column header's title.

A missing value is never turned into a number: an attempt with no median for the charted metric
is listed in the chart's `missing` strip and never in `points`, a round with no champion value
breaks the champion line, and the y range spans only recorded values (EC-025, section 7.5).

Design: design/research-engine/operator-surface.md sections 7.2 to 7.10 and 8.1."""

from __future__ import annotations

import contextlib
import json
import math

from alpaca import db
from alpaca.research import events, paths, state, verify_gate
from alpaca.research.types import INFRA_CLASSES

SCHEMA = "research-view/1"
MAX_ATTEMPTS = 2000          # section 7.2: above this the run carries attempts_total
ENDED = ("finished", "stopped", "failed")
CONTROL_LABELS = {"noop": "control (no-op)", "no-op": "control (no-op)",
                  "random": "control (random)"}

#: every field of the run object and its record source (operator-surface.md section 7.3). Each
#: names an event kind or a table; the project budget posture also names the project.yaml keys
#: budget.state reads, the view's one source outside the record (principle 2).
SOURCES = {
    "run": "research-run-start ref",
    "mode": "research-run-start data.mode",
    "runbook": "research-run-start data.runbook",
    "contract_sha256": "research-run-start data.contract_sha256",
    "intent": "research-run-start data.intent",
    "override_diff": "research-run-start data.override_diff",
    "slots_k": "research-run-start data.k",
    "metrics": "research-run-start data.metrics",
    "targets": "research-run-start data.targets",
    "control": "research-run-start data.control",
    "via": "research-run-start data.via",
    "level": "research-run-start data.level",
    "started_at": "research-run-start event ts",
    "research_branch": "research-run-start data.research_branch",
    "base_commit": "research-run-start data.base_commit",
    "op": "research-run-start op column, else the tasks table op of the run's attempt tasks",
    "control_failed": "research-finalize data.control_failed; while the run lives, "
                      "research-accept accepted on an attempt task of a control run",
    "baseline": "research-round round 0 data.champion_metrics and champion_commit, else "
                "research-run-start data.base_commit (state.fold)",
    "rounds_n": "newest research-extend data.rounds_to, else research-run-start data.n",
    "state": "state.fold over research-run-start, research-attempt, research-round, "
             "research-stop, research-resume, research-finalize, research-hold and "
             "research-hold-lifted data.answer",
    "round": "research-round-open data.round, else the highest round of the run's "
             "research-launch and research-attempt events",
    "runner": "newest research-runner data.pid, host, cause; the runner.lock probe",
    "attempt.task": "task-add data.research.run",
    "attempt.round": "task-add data.research.round",
    "attempt.slot": "task-add data.research.slot",
    "attempt.category": "task-add data.research.category",
    "attempt.status": "tasks table status, claimant",
    "attempt.proof": "tasks table proof; newest proof-report data.path and sha256",
    "attempt.outcome": "research-attempt data.outcome, broken_class, reason "
                       "(state.attempt_verdict), then research-verify-end; while the task is "
                       "doing, its newest research-gate data.stage",
    "attempt.infra": "research-infra data.class, try, role, detail",
    "attempt.accepted": "research-accept data.accepted, else research-round data.ranking[].accepted",
    "attempt.winner": "research-round data.winner",
    "attempt.metrics": "research-attempt data.metrics.<id>.median, repeats, n_ok, spread",
    "attempt.gates": "research-gate data.stage, verdict, rc, offline, input_sha256",
    "attempt.tamper": "research-tamper data.point, verdict, changed, missing",
    "attempt.audit": "research-audit-verdict data.verdict, flags",
    "attempt.verifiers": "research-proof-verdict data.verifier, runtime, round, verdict, "
                           "report_sha256, claims",
    "attempt.ruling": "research-proof-ruling, research-proof-extend",
    "attempt.verify": "verify_gate.state over proof-report, research-verify-open, "
                      "research-proof-verdict, research-verify-refused, research-verify-end",
    "attempt.lineage": "research-attempt data.forked_from, head, diff.sha256, alpaca_version",
    "attempt.cost": "research-attempt data.tokens, cost_usd, priced",
    "rounds": "research-round data.round, winner, champion_commit, champion_metrics, accepted, "
              "ranking, reason, targets_met",
    "merge": "research-merge data.round, attempt, commit, research_branch",
    "lineage": "research-round champions in round order, research-merge, and each winner's "
               "research-attempt data.forked_from",
    "chart": "research-attempt data.metrics, research-round data.champion_metrics, "
             "research-run-start data.metrics and targets",
    "holds": "research-hold, research-hold-lifted data.decision, answer; the hold card's "
             "review-card and review-move data.status, decision",
    "grants": "research-grant and the decision event it names",
    "budget": "research-attempt cost_usd, tokens and priced; research-launch-end; "
              "research-run-start data.budget raised by the newest budget research-grant",
    "budget.posture": "budget-spend events; project.yaml resource_class, budget.totals, "
                      "budget.reserve",
    "repair": "state.fold repair items: task-add, task-move, research-prepare, "
              "research-reproducer, research-baseline, research-repair-result, research-merge; "
              "verify_gate.state; the fix attempts' research-attempt and research-infra",
    "finalize": "research-finalize data.state, reason, summary, log, env_snapshot",
    "stop": "research-stop data.reason, final; event session and ts",
    "report": "newest research-report data.path, sha256",
    "last_activity": "the newest of the run's events (research-run-start and the research-* "
                     "and task events of the run and its tasks) by id and ts (state.fold)",
}


# ------------------------------------------------------------------ the read transaction
@contextlib.contextmanager
def _record(root):
    """A read-only connection inside one read transaction (alpaca/hub.py `_record`)."""
    conn = db.connect_readonly(str(root))
    try:
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def _check_id(run_id) -> str:
    if not isinstance(run_id, str) or not paths.RUN_ID.fullmatch(run_id):
        raise ValueError("run must be a research run id such as rr-001")
    return run_id


def _head(conn) -> tuple:
    row = conn.execute("SELECT id, ts FROM events ORDER BY id DESC LIMIT 1").fetchone()
    return (row["ts"], row["id"]) if row is not None else (None, 0)


def runs(root) -> dict:
    """{"schema", "as_of", "through_event", "runs"}: every recorded run, newest first, each the
    fold headline with the compact chart of its first metric and its open holds."""
    with _record(root) as conn:
        as_of, through = _head(conn)
        out = []
        for rid in state.run_ids(conn):
            try:
                out.append(_build(conn, str(root), rid, full=False))
            except KeyError:
                continue
        return {"schema": SCHEMA, "as_of": as_of, "through_event": through, "runs": out,
                "sources": dict(SOURCES)}


def run(root, run_id) -> dict | None:
    """{"schema", "as_of", "through_event", "run"} for one run, or None for an unknown run.
    Raises ValueError for a run id that does not match the run id pattern."""
    rid = _check_id(run_id)
    with _record(root) as conn:
        as_of, through = _head(conn)
        try:
            body = _build(conn, str(root), rid, full=True)
        except KeyError:
            return None
        return {"schema": SCHEMA, "as_of": as_of, "through_event": through, "run": body}


# ------------------------------------------------------------------ small reads
def _parse(row) -> dict:
    try:
        data = json.loads(row["data"] or "{}")
    except (TypeError, ValueError):
        data = {}
    return {"id": row["id"], "ts": row["ts"], "session": row["session"], "actor": row["actor"],
            "kind": row["kind"], "op": row["op"], "ref": row["ref"],
            "data": data if isinstance(data, dict) else {}}


def _evs(conn, kind, ref=None) -> list:
    if ref is None:
        rows = conn.execute("SELECT * FROM events WHERE kind=? ORDER BY id", (kind,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM events WHERE kind=? AND ref=? ORDER BY id",
                            (kind, ref)).fetchall()
    return [_parse(r) for r in rows]


def _of_run(conn, kind, run) -> list:
    """Events of one kind whose ref is the run or whose data.run is the run."""
    rows = conn.execute("SELECT * FROM events WHERE kind=? AND (ref=? OR "
                        "json_extract(data, '$.run')=?) ORDER BY id", (kind, run, run)).fetchall()
    return [_parse(r) for r in rows]


def _number(value):
    """value as a float when it is a finite recorded number, else None (never 0 for missing)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _median(entry):
    return _number(entry.get("median") if isinstance(entry, dict) else entry)


def _tasks(conn, ids) -> dict:
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for row in conn.execute("SELECT id, op, status, claimant, proof, title FROM tasks "
                                "WHERE id IN (%s)" % ",".join("?" * len(part)), part):
            out[row["id"]] = dict(row)
    return out


# ------------------------------------------------------------------ one attempt
def _gates(conn, task) -> list:
    """The newest attempt-side result of each gate item, in first-run order."""
    order, newest = [], {}
    for ev in _evs(conn, events.GATE, task):
        d = ev["data"]
        if d.get("side") not in (None, "attempt"):
            continue
        stage = d.get("stage")
        if stage not in newest:
            order.append(stage)
        if d.get("stopped") and stage in newest:
            continue
        newest[stage] = {"stage": stage, "verdict": d.get("verdict"), "rc": d.get("rc"),
                         "offline": d.get("offline"), "input_sha256": d.get("input_sha256"),
                         "stopped": bool(d.get("stopped")), "timed_out": bool(d.get("timed_out")),
                         "event": ev["id"]}
    return [newest[s] for s in order]


_WORST = {"PASS": 0, "PAUSED": 1, "FAIL": 2, "BLOCKED": 3}


def _tamper(conn, task) -> dict:
    """The worst verdict over the attempt's fingerprint points. No point recorded gives verdict
    None ("no fingerprint recorded", never PASS); a missing source counts as broken (EC-007)."""
    points, worst, changed, missing = [], None, [], []
    for ev in _evs(conn, events.TAMPER, task):
        d = ev["data"]
        word = d.get("verdict") or "BLOCKED"
        if d.get("missing") and word == "PASS":
            word = "FAIL"
        points.append({"point": d.get("point"), "verdict": word, "event": ev["id"],
                       "changed": list(d.get("changed") or []),
                       "missing": list(d.get("missing") or [])})
        for p in d.get("changed") or []:
            if p not in changed:
                changed.append(p)
        for p in d.get("missing") or []:
            if p not in missing:
                missing.append(p)
        if worst is None or _WORST.get(word, 3) > _WORST.get(worst, 3):
            worst = word
    return {"verdict": worst, "points": points, "changed": changed, "missing": missing,
            "broken": worst is not None and worst != "PASS"}


def _audit(conn, task) -> dict | None:
    got = _evs(conn, events.AUDIT_VERDICT, task)
    if not got:
        return None
    d = got[-1]["data"]
    return {"verdict": d.get("verdict"), "flags": list(d.get("flags") or []),
            "voided": bool(d.get("voided")), "event": got[-1]["id"]}


def verify_word(st: dict, status: str | None, ruled: bool = False) -> str:
    """The word section 7.7 shows for a verify state: the state as it is, except a running
    verification with a dispute ("disputed (round r of cap)"), a stopped or exhausted one with its
    code (", blocked" once the runner blocked the task, ", sealing again" at a first runtime
    mismatch), and a ruled refutation ("ruled: refuted")."""
    word = st.get("state") or "not-sealed"
    if word == "running" and (st.get("round") or 0) >= 1 and st.get("disputed"):
        return "disputed (round %s of %s)" % (st.get("round"), st.get("cap") or 5)
    if word in ("stopped", "exhausted"):
        code = st.get("code") or ("VERIFY-EXHAUSTED" if word == "exhausted" else None)
        text = "%s: %s" % (word, code) if code else word
        if status == "blocked":
            return text + ", blocked"
        if word == "stopped" and code == "VERIFY-RUNTIME-MISMATCH":
            return text + ", sealing again"
        return text
    if word in ("refuted", "unverified") and ruled:
        return "ruled: %s" % word
    return word


def _verify(conn, root, task, status) -> dict:
    try:
        st = verify_gate.state(conn, root, task)
    except Exception as exc:  # noqa: BLE001 - an unreadable state shows as such, never verified
        return {"state": None, "word": "verify state unreadable (%s)" % type(exc).__name__,
                "code": None, "lost": [], "round": None, "cap": None, "disputed": [],
                "verifiers": [], "ruling": None, "extended": False}
    vf = st.get("verification")
    verdicts, ruling, extended = [], None, False
    if vf:
        for ev in verify_gate.events(conn, events.PROOF_VERDICT, task, verification=vf):
            d = ev["data"]
            verdicts.append({"verifier": d.get("verifier"), "runtime": d.get("runtime"),
                             "round": d.get("round"), "verdict": d.get("verdict"),
                             "report_sha256": d.get("report_sha256"),
                             "claims": d.get("claims"),
                             "synthetic": bool(d.get("synthetic")), "event": ev["id"]})
        rulings = verify_gate.events(conn, events.PROOF_RULING, task, verification=vf)
        if rulings:
            ruling = {"event": rulings[-1]["id"], "round": rulings[-1]["data"].get("round"),
                      "reason": rulings[-1]["data"].get("reason")}
        extended = bool(verify_gate.events(conn, events.PROOF_EXTEND, task, verification=vf))
    return {"state": st.get("state"), "word": verify_word(st, status, ruled=ruling is not None),
            "code": st.get("code"), "lost": [verify_gate.try_name(t) for t in st.get("lost") or []],
            "round": st.get("round"), "cap": st.get("cap"),
            "disputed": list(st.get("disputed") or []), "verifiers": verdicts, "ruling": ruling,
            "extended": extended, "command": "alpaca proof verify %s --status" % task}


def _proof(conn, task, pointer) -> dict | None:
    row = conn.execute("SELECT id, data FROM events WHERE kind='proof-report' AND ref=? "
                       "ORDER BY id DESC LIMIT 1", (task,)).fetchone()
    if row is None:
        return None
    try:
        d = json.loads(row["data"] or "{}")
    except (TypeError, ValueError):
        d = {}
    path = d.get("path") or (str(pointer)[len("local:"):] if str(pointer or "").startswith(
        "local:") else None)
    return {"path": path, "sha256": d.get("sha256"), "pointer": pointer, "event": row["id"]}


def _attempt(conn, root, add, task_row, round_winners, ranking_accepted, stages) -> dict:
    task = add["ref"]
    r = add["data"].get("research") or {}
    status = (task_row or {}).get("status")
    v = state.attempt_verdict(conn, task)
    recorded = _evs(conn, events.ATTEMPT, task)
    ad = recorded[-1]["data"] if recorded else {}
    running = None
    if v["event"] is None and status == "doing":
        gate_evs = _evs(conn, events.GATE, task)
        stage = gate_evs[-1]["data"].get("stage") if gate_evs else None
        running = stage or state.attempt_state(conn, task)
    accepts = _evs(conn, events.ACCEPT, task)
    if accepts:
        accepted = bool(accepts[-1]["data"].get("accepted"))
    else:
        accepted = ranking_accepted.get(task)
    metrics = {}
    for mid, m in (ad.get("metrics") or {}).items():
        if not isinstance(m, dict):
            m = {"median": m}
        reps = [x for x in (_number(y) for y in (m.get("repeats") or [])) if x is not None]
        metrics[mid] = {"median": _median(m), "repeats": reps, "n_ok": m.get("n_ok"),
                        "spread": m.get("spread"), "untested": bool(m.get("untested"))}
    gates = _gates(conn, task)
    first_fail = next((g["stage"] for g in gates if g["verdict"] not in ("PASS", None)
                       and not g["stopped"]), None)
    seen = {g["stage"] for g in gates}
    for s in stages:
        if s not in seen:
            gates.append({"stage": s, "verdict": None, "not_run": True})
    gates.sort(key=lambda g: stages.index(g["stage"]) if g["stage"] in stages else len(stages))
    infra = [{"class": e["data"].get("class"), "try": e["data"].get("try"),
              "role": e["data"].get("role"), "detail": e["data"].get("detail"), "event": e["id"]}
             for e in _evs(conn, events.INFRA, task)]
    if v["event"] is None:
        outcome_word = ("running (stage %s)" % running) if running else "no outcome recorded"
    else:
        outcome_word = v["outcome"] or "no outcome recorded"
    diff = ad.get("diff") if isinstance(ad.get("diff"), dict) else {}
    return {
        "task": task, "title": (task_row or {}).get("title"), "round": r.get("round"),
        "slot": r.get("slot"), "category": r.get("category"), "baseline": bool(r.get("baseline")),
        "repair_task": r.get("repair_task"), "status": status,
        "claimant": (task_row or {}).get("claimant"),
        "outcome": v["outcome"], "verdict": v["verdict"], "broken_class": v["broken_class"],
        "reason": v["reason"], "recorded": v["event"] is not None, "running": running,
        "outcome_word": outcome_word, "infra": infra,
        "infra_failure": bool(v["outcome"] == "abandoned" and v["reason"] in INFRA_CLASSES),
        "accepted": accepted, "winner": task in round_winners,
        "metrics": metrics, "gates": gates, "first_failing_gate": first_fail,
        "tamper": _tamper(conn, task), "audit": _audit(conn, task),
        "verify": _verify(conn, root, task, status),
        "proof": _proof(conn, task, (task_row or {}).get("proof")),
        "forked_from": ad.get("forked_from"), "head": ad.get("head"),
        "diff_sha256": diff.get("sha256"), "alpaca_version": ad.get("alpaca_version"),
        "tokens": ad.get("tokens") if "tokens" in ad else None,
        "cost_usd": ad.get("cost_usd") if "cost_usd" in ad else None,
        "priced": ad.get("priced") if "priced" in ad else None,
    }


# ------------------------------------------------------------------ rounds, chart, lineage
def _rounds(conn, run, mode) -> list:
    merges = {}
    for ev in _of_run(conn, events.MERGE, run):
        d = ev["data"]
        merges[(d.get("round"), d.get("repair_task") or None)] = {
            "event": ev["id"], "attempt": d.get("attempt"), "commit": d.get("commit"),
            "parent": d.get("parent"), "research_branch": d.get("research_branch")}
    out = []
    for ev in _of_run(conn, events.ROUND, run):
        d = ev["data"]
        key = (d.get("round"), d.get("repair_task") or None)
        champ = {}
        for mid, m in (d.get("champion_metrics") or {}).items():
            champ[mid] = _median(m)
        out.append({"round": d.get("round"), "repair_task": d.get("repair_task"),
                    "winner": d.get("winner"), "champion_commit": d.get("champion_commit"),
                    "champion_metrics": champ, "accepted": d.get("accepted"),
                    "ranking": list(d.get("ranking") or []), "reason": d.get("reason"),
                    "targets_met": d.get("targets_met"), "merge": merges.get(key),
                    "event": ev["id"], "ts": ev["ts"]})
    out.sort(key=lambda x: (x["repair_task"] or "", x["round"] if isinstance(x["round"], int)
                            else -1), reverse=True)
    return out


def _met(value, target, direction):
    if value is None or target is None:
        return None
    return value >= target if direction == "higher" else value <= target


def _y_range(values: list):
    """The y range of section 7.5: only recorded values, padded by 5 percent; it never includes 0
    unless a recorded value is 0. None when there is no value."""
    if not values:
        return None
    lo, hi = min(values), max(values)
    span = hi - lo
    pad = span * 0.05 if span > 0 else (abs(hi) * 0.05 or 1.0)
    y0, y1 = lo - pad, hi + pad
    if lo > 0 and y0 <= 0:
        y0 = lo - min(pad, lo * 0.5)
    if hi < 0 and y1 >= 0:
        y1 = hi + min(pad, -hi * 0.5)
    return {"min": y0, "max": y1}


def _chart(metrics_decl, targets, attempts, rounds, mode) -> dict:
    """One chart per declared metric (section 7.5). x: attempts by round, round 0 first, slots in
    order. `points` holds only attempts with a recorded median; the others are in `missing` with
    their reason. The champion line (improve runs only) is split into `champion_segments` at a
    round whose champion value is missing; it never drops to 0."""
    order = sorted(attempts, key=lambda a: (a["round"] if isinstance(a["round"], int) else -1,
                                            a["slot"] if isinstance(a["slot"], int) else 0))
    bands, x_of = [], {}
    for x, a in enumerate(order):
        x_of[a["task"]] = x
        if bands and bands[-1]["round"] == a["round"]:
            bands[-1]["x1"] = x
            bands[-1]["n"] += 1
        else:
            bands.append({"round": a["round"], "x0": x, "x1": x, "n": 1})
    line_rounds = sorted((r for r in rounds if not r["repair_task"]),
                         key=lambda r: r["round"] if isinstance(r["round"], int) else -1)
    out = []
    for m in metrics_decl or []:
        mid = m.get("id")
        points, missing = [], []
        for a in order:
            mv = a["metrics"].get(mid) or {}
            med = mv.get("median")
            if med is None:
                missing.append({"x": x_of[a["task"]], "task": a["task"], "round": a["round"],
                                "slot": a["slot"], "outcome": a["outcome"],
                                "reason": a["reason"] or a["outcome_word"] or "no metric"})
                continue
            reps = mv.get("repeats") or []
            points.append({"x": x_of[a["task"]], "task": a["task"], "round": a["round"],
                           "slot": a["slot"], "value": med, "outcome": a["outcome"],
                           "verdict": a["verdict"], "winner": a["winner"],
                           "untested": bool(mv.get("untested")),
                           "lo": min(reps) if len(reps) > 1 else None,
                           "hi": max(reps) if len(reps) > 1 else None,
                           "n_ok": mv.get("n_ok"), "repeats": len(reps)})
        champion, segments = [], []
        if mode != "repair":
            current = []
            for r in line_rounds:
                val = r["champion_metrics"].get(mid)
                champion.append({"round": r["round"], "value": val})
                if val is None:
                    if current:
                        segments.append(current)
                    current = []
                else:
                    current.append({"round": r["round"], "value": val})
            if current:
                segments.append(current)
        tvals = [_number(t.get("value")) for t in (targets or []) if t.get("metric") == mid]
        tvals = [t for t in tvals if t is not None]
        met = []
        for r in line_rounds:
            val = r["champion_metrics"].get(mid)
            met.append({"round": r["round"],
                        "met": None if not tvals else _met(val, tvals[0], m.get("direction"))})
        values = [p["value"] for p in points] + [c["value"] for c in champion
                                                  if c["value"] is not None] + tvals
        out.append({"metric": mid, "unit": m.get("unit"), "direction": m.get("direction"),
                    "kind": m.get("kind"), "points": points, "missing": missing,
                    "champion": champion, "champion_segments": segments,
                    "targets": [{"value": t} for t in tvals], "targets_met": met,
                    "y": _y_range(values),
                    "caption": ("%d attempt%s no metric and %s not plotted"
                                % (len(missing), " has" if len(missing) == 1 else "s have",
                                   "is" if len(missing) == 1 else "are")) if missing else None})
    return {"x_count": len(order), "bands": bands, "metrics": out,
            "targets_metrics": len({t.get("metric") for t in (targets or [])})}


def _lineage(rounds, attempts, start_data, mode) -> list:
    """The champion chain of section 7.6, round 0 first: each round whose winner became the
    champion, with the change of each metric against the previous champion and a lineage break
    when the winner's forked_from is not the previous champion commit (FR-003)."""
    if mode == "repair":
        return []
    by_task = {a["task"]: a for a in attempts}
    nodes, prev = [], None
    for r in sorted((r for r in rounds if not r["repair_task"]),
                    key=lambda r: r["round"] if isinstance(r["round"], int) else -1):
        if r["round"] == 0:
            commit = r["champion_commit"] or start_data.get("base_commit")
            winner = r["winner"]
        elif r["merge"] is not None:
            commit = r["merge"]["commit"] or r["champion_commit"]
            winner = r["merge"]["attempt"] or r["winner"]
        else:
            continue
        change = {}
        for mid, val in r["champion_metrics"].items():
            before = (prev or {}).get("metrics", {}).get(mid) if prev else None
            if prev is None:
                change[mid] = None
            elif val is None or before is None:
                change[mid] = "not comparable"
            else:
                change[mid] = val - before
        forked = (by_task.get(winner) or {}).get("forked_from") if winner else None
        brk = bool(prev is not None and r["round"] != 0 and forked != prev["commit"])
        nodes.append({"round": r["round"], "commit": commit, "winner": winner,
                      "metrics": dict(r["champion_metrics"]), "change": change,
                      "merge_event": (r["merge"] or {}).get("event"), "forked_from": forked,
                      "previous_commit": prev["commit"] if prev else None, "lineage_break": brk})
        prev = nodes[-1]
    return nodes


# ------------------------------------------------------------------ repair runs (section 7.8)
def _repair(conn, root, items, attempts, run_state) -> dict:
    if items is None:
        return None
    ended = run_state in ENDED
    rows, buckets = [], {"resolved": [], "error": [], "unresolved": [], "pending": []}
    for item in items:
        fixes = [a for a in attempts if a["repair_task"] == item["task"]]
        fixes.sort(key=lambda a: (a["round"] if isinstance(a["round"], int) else -1,
                                  a["slot"] if isinstance(a["slot"], int) else 0))
        live = [a for a in fixes if not a["recorded"] and a["status"] not in ("done", "blocked")]
        last = fixes[-1] if fixes else None
        outcome, st = item.get("outcome"), item.get("state")
        if item.get("resolved"):
            bucket = "resolved"
        elif outcome != "fixed" and not live and last is not None and last["recorded"] \
                and last["outcome"] == "abandoned" and last["reason"] in INFRA_CLASSES:
            bucket = "error"
        elif (outcome is not None and outcome != "fixed") or (outcome == "fixed" and st == "blocked") \
                or (outcome is None and ended):
            bucket = "unresolved"
        else:
            bucket = "pending"
        fix = item.get("fix") or {}
        best = next((a for a in fixes if a["task"] == fix.get("attempt")), None) or \
            next((a for a in reversed(fixes) if a["recorded"]), None)
        # every repair task carries its own verify state, whatever its bucket: an error or
        # unresolved task was never verified, and the page shows the state the record holds
        verify = _verify(conn, root, item["task"], st)
        infra = [i for a in fixes for i in a["infra"]]
        row = dict(item, bucket=bucket, verify=verify,
                   best_attempt=best["task"] if best else None,
                   best_gates=best["gates"] if best else [],
                   best_first_failing_gate=best["first_failing_gate"] if best else None,
                   fix_attempts=[a["task"] for a in fixes],
                   infra_classes=sorted({i["class"] for i in infra if i.get("class")}))
        rows.append(row)
        buckets[bucket].append(item["task"])
    total = len(rows)
    resolved = len(buckets["resolved"])
    rate = None if not total else {
        "resolved": resolved, "total": total,
        "percent": round(100.0 * resolved / total), "so_far": bool(buckets["pending"])}
    return {"items": rows, "buckets": buckets, "rate": rate}


# ------------------------------------------------------------------ the run object
def _card(conn, card_id) -> dict | None:
    """The hold card's folded state from its review-card and review-move events (the fold
    review._state and state._open_holds use: the card event, then each move merged on top), with
    the id of the move event. None when the hold names no card or no card event is recorded."""
    if not card_id:
        return None
    rows = conn.execute("SELECT * FROM events WHERE kind IN ('review-card', 'review-move') "
                        "AND ref=? ORDER BY id", (str(card_id),)).fetchall()
    rec, move = None, None
    for ev in (_parse(r) for r in rows):
        if ev["kind"] == "review-card":
            rec = dict(ev["data"])
        elif rec is not None:
            rec.update(ev["data"])
            move = ev
    if rec is None:
        return None
    return {"id": str(card_id), "status": rec.get("status"),
            "decision": rec.get("decision") or None,
            "moved_by": rec.get("moved_by"), "moved_at": rec.get("moved_at"),
            "move_event": move["id"] if move else None}


def _holds(conn, run) -> list:
    """Every hold of the run, newest first. `status` is "lifted" once research-hold-lifted is
    recorded, "card moved" for a ship hold whose card the owner moved before holds.settle_ship
    records the lift (operator-surface.md:1221-1223), else "open"."""
    lifts = {}
    for ev in _of_run(conn, events.HOLD_LIFTED, run):
        lifts[ev["data"].get("hold")] = {"event": ev["id"], "ts": ev["ts"],
                                         "decision": ev["data"].get("decision"),
                                         "answer": ev["data"].get("answer")}
    out = []
    for ev in _of_run(conn, events.HOLD, run):
        d = ev["data"]
        lifted = lifts.get(ev["id"])
        card = _card(conn, d.get("card"))
        card_status = card["status"] if card else None
        if lifted is not None:
            status = "lifted"
        elif d.get("gate") == "ship" and card_status not in (None, "open"):
            status = "card moved"
        else:
            status = "open"
        out.append({"id": ev["id"], "ts": ev["ts"], "gate": d.get("gate"), "code": d.get("code"),
                    "detail": d.get("detail"), "card": d.get("card"),
                    "card_status": card_status,
                    "card_decision": card["decision"] if card else None,
                    "card_move_event": card["move_event"] if card else None,
                    "question": d.get("question"), "lifted": lifted, "status": status})
    return sorted(out, key=lambda h: h["id"], reverse=True)


def _grants(conn, run) -> list:
    out = []
    for ev in _evs(conn, events.GRANT):
        d = ev["data"]
        if ev["ref"] != run and d.get("ref") != run and d.get("run") != run:
            continue
        dec = d.get("decision")
        found = conn.execute("SELECT id FROM decisions WHERE id=?", (dec,)).fetchone() \
            if dec else None
        out.append({"event": ev["id"], "ts": ev["ts"], "gate": d.get("gate"), "hold": d.get("hold"),
                    "decision": dec, "decision_recorded": found is not None,
                    "terms": d.get("terms"), "note": d.get("note"),
                    "confirmed": d.get("confirmed"), "by": d.get("by")})
    return out


def _newest(conn, kind, run, keys) -> dict | None:
    got = _of_run(conn, kind, run)
    if not got:
        return None
    ev = got[-1]
    out = {k: ev["data"].get(k) for k in keys}
    out.update(event=ev["id"], ts=ev["ts"], session=ev["session"])
    return out


def _runner(conn, root, run) -> dict | None:
    got = _newest(conn, events.RUNNER, run, ("pid", "host", "cause", "foreground"))
    alive = False
    try:
        from alpaca.research import runner
        alive = runner.is_alive(root, run)
    except Exception:  # noqa: BLE001 - a probe that cannot run reads as not alive
        alive = False
    if got is None and not alive:
        return None
    return dict(got or {}, alive=alive)


def _build(conn, root, run_id, *, full) -> dict:
    head = state.fold(conn, run_id, root=root)
    start = _evs(conn, events.RUN_START, run_id)[0]
    sd = start["data"]
    adds = state._run_tasks(conn, run_id)
    attempt_adds = [a for a in adds if (a["data"].get("research") or {}).get("role") == "attempt"]
    rows = _tasks(conn, [a["ref"] for a in adds])
    rounds = _rounds(conn, run_id, sd.get("mode"))
    winners = {r["winner"] for r in rounds if r["winner"]}
    ranking_accepted = {}
    for r in rounds:
        for item in r["ranking"]:
            if isinstance(item, dict) and item.get("attempt"):
                ranking_accepted.setdefault(item["attempt"], bool(item.get("accepted")))
    stages = []
    for ev in _of_run(conn, events.GATE, run_id):
        d = ev["data"]
        if d.get("side") in (None, "attempt") and d.get("stage") not in stages:
            stages.append(d.get("stage"))
    attempts = [_attempt(conn, root, a, rows.get(a["ref"]), winners, ranking_accepted, stages)
                for a in attempt_adds]
    op = head.get("op") or next((rows[a["ref"]]["op"] for a in adds if a["ref"] in rows
                                 and rows[a["ref"]].get("op")), None)
    extends = _of_run(conn, events.EXTEND, run_id)
    rounds_n = extends[-1]["data"].get("rounds_to") if extends and \
        extends[-1]["data"].get("rounds_to") is not None else sd.get("n")
    current = head.get("round")
    if current is None:
        seen = [a["round"] for a in attempts if isinstance(a["round"], int)]
        seen += [e["data"].get("round") for e in _of_run(conn, events.LAUNCH, run_id)
                 if isinstance(e["data"].get("round"), int)]
        current = max(seen) if seen else None
    finalize = _newest(conn, events.FINALIZE, run_id,
                       ("state", "reason", "summary", "log", "env_snapshot", "forensics",
                        "control_failed", "verdict", "code"))
    if finalize is not None and not full:
        finalize.pop("summary", None)
    control_failed = bool(head.get("control_failed") or (finalize or {}).get("control_failed"))
    control = sd.get("control")
    chart = _chart(sd.get("metrics"), sd.get("targets"), attempts, rounds, sd.get("mode"))
    repair = _repair(conn, root, head.get("repair"), attempts, head.get("state"))
    stops = [{"event": e["id"], "ts": e["ts"], "session": e["session"],
              "reason": e["data"].get("reason"), "final": bool(e["data"].get("final")),
              "decision": e["data"].get("decision")} for e in _of_run(conn, events.STOP, run_id)]
    report = _newest(conn, events.REPORT, run_id, ("path", "sha256", "json_path", "state"))
    running = [{"task": a["task"], "round": a["round"], "slot": a["slot"],
                "stage": a["running"]} for a in attempts if a["running"]]
    body = dict(head)
    body.update({
        "op": op, "rounds_n": rounds_n, "rounds_n_start": sd.get("n"), "round": current,
        "control": control, "control_label": CONTROL_LABELS.get(str(control), "control (%s)"
                                                                % control) if control else None,
        "control_failed": control_failed,
        "start": {k: sd.get(k) for k in ("mode", "runbook", "contract_sha256", "intent",
                                         "override_diff", "k", "n", "metrics", "targets",
                                         "control", "via", "level", "research_branch",
                                         "base_commit", "budget")},
        "metrics": list(sd.get("metrics") or []),
        "research_branch": sd.get("research_branch"),
        "running_slots": running,
        "finalize": finalize, "stops": stops, "report": report,
        "runner": _runner(conn, root, run_id),
        "holds": _holds(conn, run_id), "open_holds": head.get("holds") or [],
        "grants": _grants(conn, run_id),
        "repair": repair,
        "repair_items": head.get("repair"),
        "rounds": rounds,
        "lineage": _lineage(rounds, attempts, sd, sd.get("mode")),
        "chart": chart,
        "attempt_count": len(attempts),
        "verify_counts": _count(a["verify"]["state"] or "unknown" for a in attempts),
        "tamper_catches": sum(1 for a in attempts if a["tamper"]["broken"]),
        "audit_flags": sum(1 for a in attempts if a["audit"] and (a["audit"]["flags"]
                                                                  or a["audit"]["verdict"]
                                                                  not in ("PASS", None))),
        "sources": dict(SOURCES),
    })
    if full:
        ordered = sorted(attempts, key=lambda a: (-(a["round"] if isinstance(a["round"], int)
                                                    else -1),
                                                  a["slot"] if isinstance(a["slot"], int) else 0))
        body["attempts_total"] = len(ordered)
        body["attempts"] = ordered[:MAX_ATTEMPTS]
    else:
        body.pop("sources")
        body["attempts"] = None
        body["chart"] = dict(chart, metrics=chart["metrics"][:1])
    return body


def _count(words) -> dict:
    out = {}
    for w in words:
        out[w] = out.get(w, 0) + 1
    return dict(sorted(out.items()))
