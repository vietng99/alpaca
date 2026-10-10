"""The `alpaca research` verbs (operator-surface.md section 4): start, status, stop, extend, resume,
report, grant, contracts and template, and the two verbs of the session runtime, next-job and
submit (in-session-runtime.md section 3), with which the driving session hands the engine's agent
launches to subagents of its own and files their results.

Rules every sub-verb follows (section 4.2): the session is `args.session or "cli"`; the text form
ends in one `GATE alpaca-research-<verb>: <WORD>` line, `--json` prints one object whose top-level
keys are surface.TABLE["research"]'s; exit codes come from alpaca.gates.verdict. A refusal
carries a reason code of section 4.4; every refusal with exit 1 or 3 of start, stop, extend,
resume, report and grant is recorded as one `research-refused` event, after the hold it opened
in the same transaction when it opened one. Every verb that writes events first records the lift
of any ship hold whose card the owner has moved (holds.settle_ship).

No verb takes a field that sets the bar: metrics, targets, the accept rule, K, N, budgets and
paths come only from the frozen contract. An owner decision is typed by the owner at a terminal
outside any agent session (`grant`, owner.confirm_at_terminal); `extend` and `resume` at a budget
hold need that grant on the record (owner.authorized), and exit 3 without it.

Design: design/research-engine/operator-surface.md sections 4.1 to 4.7; architecture.md section
4.1 (start), 4.2 (liveness) and 4.8 (finalize and the report)."""

from __future__ import annotations

import json
import os
import shlex
import socket

from alpaca import cli, db, util
from alpaca.gates import verdict as vc
from alpaca.research import agent_marker, errors, events, paths

#: the sub-verbs, in the order of operator-surface section 4.3
VERBS = ("start", "status", "stop", "extend", "resume", "report", "grant", "contracts",
         "template", "next-job", "submit")
#: the sub-verbs whose refusals with exit 1 or 3 are recorded as research-refused (section 4.2)
RECORDED = ("start", "stop", "extend", "resume", "report", "grant")
#: the actor of the events a verb writes; a grant is the owner's
ACTOR = "operator"
_OWNER_CMD = "bin/alpaca research grant"
_FINISHED = ("finished", "stopped", "failed")


class _Refused(Exception):
    """A refusal a verb's core raises; the verb prints it and records it (section 4.2)."""

    def __init__(self, code: str, reason: str, *, exit: int | None = None, decision=None,
                 ref: str | None = None, record: bool = True):
        super().__init__("%s: %s" % (code, reason))
        self.code, self.reason, self.decision, self.ref = code, reason, decision, ref
        self.exit = exit if exit is not None else errors.CODES.get(code, vc.FAIL)
        self.record = record


# ------------------------------------------------------------------ output and refusals
def _hold_view(hold) -> dict | None:
    if not hold:
        return None
    return {"id": hold.get("id"), "ref": hold.get("ref"), "gate": hold.get("gate"),
            "code": hold.get("code"), "detail": hold.get("detail"), "card": hold.get("card"),
            "question": hold.get("question"), "ts": hold.get("ts")}


def _owner_command(hold) -> str:
    """The command the owner types to answer a hold, printed with it."""
    gate, ref = hold.get("gate"), str(hold.get("ref") or "")
    if gate == "ship":
        return "bin/alpaca review move %s \"<your decision>\" --by owner" % hold.get("card")
    if ref.startswith("contract:"):
        path, _, task = ref[len("contract:"):].rpartition("#")
        base = "%s --runbook %s%s" % (_OWNER_CMD, shlex.quote(path),
                                      "" if task in ("", "-") else " --task %s" % task)
        if gate == "loosen":
            return base + " loosen --set <PATH=VALUE> (each field as the hold names it)"
        return base + " budget"
    if gate == "extension":
        return "%s %s extend --rounds <M>" % (_OWNER_CMD, ref)
    if gate == "budget":
        return "%s %s budget --usd <X> --tokens <T>" % (_OWNER_CMD, ref)
    return "%s %s stop" % (_OWNER_CMD, ref)


def _hold_lines(hold) -> list:
    out = ["  hold %s %s since %s on %s:" % (hold.get("id"), hold.get("gate"), hold.get("ts"),
                                            hold.get("ref"))]
    for line in str(hold.get("detail") or "").splitlines():
        out.append("    %s" % line)
    out.append("  owner, in your own terminal: %s" % _owner_command(hold))
    return out


def _one_line(text) -> str:
    return " ".join(str(text or "").split())


def _dump(body) -> str:
    return json.dumps(body, sort_keys=True, ensure_ascii=True, default=str)


class _Verb:
    """One call of one sub-verb: the root, the session, the record and the output rules."""

    def __init__(self, args, verb):
        self.args, self.verb = args, verb
        self.json = bool(getattr(args, "json", False))
        self.root = os.path.realpath(cli._root())
        self.session = getattr(args, "session", None) or "cli"
        self.conn = None

    # the record
    def open(self) -> None:
        try:
            self.conn = db.connect(self.root)
        except Exception as exc:  # noqa: BLE001 - any failure to open the record is BLOCKED
            raise _Refused("RESEARCH-RECORD-UNREADABLE", "the record cannot be opened (%s: %s)"
                           % (type(exc).__name__, exc), record=False)

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass

    def settle(self) -> None:
        from alpaca.research import holds
        holds.settle_ship(self.conn, self.root, session=self.session)

    def run_op(self, run):
        if not run:
            return None
        row = self.conn.execute("SELECT op FROM events WHERE kind=? AND ref=? ORDER BY id LIMIT 1",
                                (events.RUN_START, run)).fetchone()
        return row["op"] if row else None

    def record_refusal(self, code: str, reason: str, *, ref: str, exit: int,
                       in_txn: bool = False) -> dict:
        ref = str(ref or "-")
        run = ref if paths.RUN_ID.fullmatch(ref) else None
        known = code in errors.CODES or bool(events.T089_REASON.fullmatch(code or ""))
        data = {"verb": self.verb, "ref": ref, "reason": _one_line(reason)}
        if not known:
            data["refusal"] = code
        return events.emit(self.conn, events.REFUSED, run=run, ref=ref, op=self.run_op(run),
                           session=self.session, actor=ACTOR, code=exit,
                           reason=code if known else None, data=data, in_txn=in_txn)

    # printing
    def done(self, code: int, payload: dict, lines=()) -> int:
        if self.json:
            body = {"verdict": vc.name_of(code)}
            body.update(payload)
            print(_dump(body))
        else:
            for line in lines:
                print(line)
            print(vc.gate_line("alpaca-research-%s" % self.verb, code))
        return code

    def refused(self, exc: _Refused, *, ref: str | None = None, extra=None) -> int:
        ref = exc.ref or ref or "-"
        if exc.record and self.verb in RECORDED and exc.exit in (vc.FAIL, vc.PAUSED) \
                and self.conn is not None:
            self.record_refusal(exc.code, exc.reason, ref=ref, exit=exc.exit)
        decision = _hold_view(exc.decision)
        if self.json:
            body = {"verdict": vc.name_of(exc.exit), "code": exc.code,
                    "reason": _one_line(exc.reason)}
            if decision is not None:
                body["decision"] = decision
            body.update(extra or {})
            print(_dump(body))
        else:
            print("research %s: %s" % (self.verb, exc.reason))
            if decision is not None:
                for line in _hold_lines(decision):
                    print(line)
            print("%s (%s: %s)" % (vc.gate_line("alpaca-research-%s" % self.verb, exc.exit),
                                   exc.code, _one_line(exc.reason)))
        return exc.exit

    def rel_path(self, path: str) -> str:
        """A path argument relative to the root, refused (USAGE) when it leaves the root."""
        full = os.path.realpath(path if os.path.isabs(path) else os.path.join(self.root, path))
        if full != self.root and not full.startswith(self.root + os.sep):
            raise _Usage("%s is outside the project root" % path)
        return os.path.relpath(full, self.root).replace(os.sep, "/")


class _Usage(Exception):
    """A usage error the parser cannot catch (exit 64)."""


# ------------------------------------------------------------------ record reads
def _rows(conn, kind: str, ref: str | None = None) -> list:
    sql, params = "SELECT id, ts, session, ref, op, data FROM events WHERE kind=?", [kind]
    if ref is not None:
        sql += " AND ref=?"
        params.append(ref)
    out = []
    for row in conn.execute(sql + " ORDER BY id", params).fetchall():
        try:
            data = json.loads(row["data"] or "{}")
        except (TypeError, ValueError):
            data = {}
        out.append({"id": row["id"], "ts": row["ts"], "session": row["session"],
                    "ref": row["ref"], "op": row["op"], "data": data})
    return out


def _run_start(conn, run):
    got = _rows(conn, events.RUN_START, run)
    return got[0] if got else None


def _runs_under(conn, runbook: str) -> list:
    """The run-start events whose runbook is `runbook`, oldest first."""
    return [ev for ev in _rows(conn, events.RUN_START) if ev["data"].get("runbook") == runbook]


def _finalized(conn, run: str) -> bool:
    return bool(_rows(conn, events.FINALIZE, run))


#: the text form of level_source (operator-surface.md section 4.3.2 example)
_LEVEL_WORDS = {"record": "set by the owner", "default": "project default"}


def _level(v: _Verb) -> tuple:
    """("L<n>", source) of the level in force for the session (posture.level.in_force), read
    only; source is "record" when a level decision of the session sets it, else "default"
    (operator-surface.md section 3.3, step 1). Raises on a level that cannot be read."""
    from alpaca.posture import level
    n = level.in_force(v.conn, v.session, root=v.root)
    if not (level.MIN_LEVEL <= int(n) <= level.MAX_LEVEL):
        raise ValueError("the level in force reads as %r" % (n,))
    src = "record" if level._latest_decision(v.conn, v.session) is not None else "default"
    return "L%d" % int(n), src


def _runner_info(v: _Verb, run: str) -> dict:
    """{runner, pid, host}: `alive` when a process on this host holds the runner lock (the
    non-blocking shared probe of runner.is_alive), `unknown` when the newest research-runner
    event names another host, else `not running`."""
    from alpaca.research import runner
    alive = runner.is_alive(v.root, run)
    got = _rows(v.conn, events.RUNNER, run)
    newest = got[-1]["data"] if got else {}
    host, pid = newest.get("host"), newest.get("pid")
    if alive:
        word = "alive"
    elif host and host != socket.gethostname():
        word = "unknown"
    else:
        word = "not running"
    return {"runner": word, "pid": pid, "host": host}


def _owner_stopped(conn, run: str) -> bool:
    """True when the owner stopped the run: a research-stop with final true newer than its newest
    research-resume, or a newest research-hold-lifted whose answer is "stop" (D11)."""
    resumes = _rows(conn, events.RESUME, run)
    mark = resumes[-1]["id"] if resumes else 0
    if any(ev["data"].get("final") and ev["id"] > mark for ev in _rows(conn, events.STOP, run)):
        return True
    lifts = _rows(conn, events.HOLD_LIFTED, run)
    return bool(lifts) and lifts[-1]["data"].get("answer") == "stop"


def _fold(v: _Verb, run: str) -> dict:
    from alpaca.research import state
    return state.fold(v.conn, run, v.root)


def _need(conn, run: str, start: dict, rounds: int) -> tuple:
    """(tasks the run needs with `rounds`, tasks the op holds that are not the run's), the count
    of architecture.md section 4.1 step 5, by the rule open_run applies (runner.plan_need)."""
    from alpaca.research import runner
    d = start["data"]
    block = d.get("block") if isinstance(d.get("block"), dict) else {}
    controls = block.get("controls") if isinstance(block.get("controls"), dict) else {}
    need = runner.plan_need(slots=int(d.get("k") or 0), mode=d.get("mode"),
                            repairs=len(d.get("repair_tasks") or []), control=d.get("control"),
                            noop_rounds=int(controls.get("noop_rounds", 0) or 0), rounds=rounds)
    op = start.get("op")
    have = conn.execute("SELECT COUNT(*) FROM tasks WHERE op=?", (op,)).fetchone()[0] if op else 0
    own = conn.execute("SELECT COUNT(*) FROM events WHERE kind='task-add' AND "
                       "json_extract(data, '$.research.run')=?", (run,)).fetchone()[0]
    return need, max(0, int(have) - int(own))


# ------------------------------------------------------------------ the cores
def _level_n(v: _Verb):
    """The level in force as a number, or None when it cannot be read."""
    try:
        return int(_level(v)[0].lstrip("L"))
    except Exception:  # noqa: BLE001
        return None


def _session_check(v: _Verb, run: str, *, restart: bool = True) -> None:
    """The refusals every restart of a session run shares (session_runtime.drive_checks); a test
    run keeps today's behavior and a run recorded before the session runtime is a headless run,
    refused with RUN-HEADLESS-REFUSED. Changes nothing."""
    from alpaca.research import runner, session_runtime
    mode = runner._runtime_mode(v.root, run)
    if mode == "test":
        return
    try:
        session_runtime.drive_checks(v.conn, v.root, v.session, level=_level_n(v), run=run,
                                     restart=restart)
    except errors.ResearchRefusal as exc:
        raise _Refused(exc.reason, exc.detail, exit=exc.code, ref=run)


def _check_model(v: _Verb, model: str, *, ref: str) -> str:
    """The alias of `--model`, when the shield policy maps it for the session's runtime (claude
    when none is in use); refused with RUN-MODEL-UNKNOWN otherwise (in-session-runtime.md section
    4.3). Changes nothing."""
    from alpaca.research import launcher, session_runtime
    try:
        return session_runtime.check_model_override(v.root, model, launcher.runtime_in_use())
    except errors.ResearchRefusal as exc:
        raise _Refused(exc.reason, exc.detail, exit=exc.code, ref=ref)


def _run_model_arg(v: _Verb, run: str, model) -> None:
    """`resume` and `extend` take `--model` only to repeat the run's model override: the model a
    run's jobs name is set once, by its start, and recorded on research-run-start. An unknown
    alias is RUN-MODEL-UNKNOWN; another model than the run's, or a run with no override, is
    RUN-MODEL-MISMATCH. Without the flag the run's override (or none) holds. Changes nothing."""
    from alpaca.research import session_runtime
    if model is None:
        return
    if _run_start(v.conn, run) is None:
        raise _Refused("RESEARCH-RUN-UNKNOWN", "no research-run-start names %s" % run, ref=run)
    _check_model(v, model, ref=run)
    have = session_runtime.run_model(v.conn, run)
    if have != str(model):
        raise _Refused("RUN-MODEL-MISMATCH", "%s; --model %s cannot change it, since a run's "
                       "model is set by `alpaca research start --model` and recorded on its start"
                       % ("run %s names %s for every agent job" % (run, have) if have else
                          "run %s has no model override (each role runs on its shield tier)" % run,
                          model), ref=run)


def _launch(v: _Verb, run: str, *, cause: str, decision, hold, driver_cause: str = "resume") -> dict:
    """Start the runner again under the run id and record research-resume (section 4.3.5). For a
    session run the driver is registered first (session_runtime.register_driver), so the runner
    that starts next finds the resuming session as its newest driver."""
    from alpaca.research import runner, session_runtime
    if runner._runtime_mode(v.root, run) == "session":
        start = _run_start(v.conn, run)
        session_runtime.register_driver(
            v.conn, v.root, run, v.session, cause=driver_cause, level=_level_n(v),
            runtime=start["data"].get("runtime") or "claude", run=run, op=v.run_op(run))
    got = runner.launch(v.root, run, foreground=False, session=v.session)
    ev = events.emit(v.conn, events.RESUME, run=run, ref=run, op=v.run_op(run),
                     session=v.session, actor=ACTOR,
                     data={"cause": cause, "decision": decision, "hold": hold,
                           "pid": got.get("pid"), "host": got.get("host")})
    return {"event": ev["id"], "pid": got.get("pid"), "host": got.get("host"),
            "log": got.get("log")}


def _do_stop(v: _Verb, run: str, *, reason: str, decision: str | None, held: list) -> dict:
    """Record research-stop (after lifting the answered holds with "stop"), then SIGTERM the
    runner when its lock is held on this host (section 4.3.3)."""
    from alpaca.research import holds, runner
    alive = runner.is_alive(v.root, run)
    pid = None
    if alive:
        rec = runner._read_pid(v.root, run) or {}
        if rec.get("host") == socket.gethostname():
            pid = rec.get("pid")
    with db.transaction(v.conn):
        for hold in held:
            holds.lift_hold(v.conn, v.root, hold=hold["id"], decision=decision, answer="stop",
                            session=v.session)
        ev = events.emit(v.conn, events.STOP, run=run, ref=run, op=v.run_op(run),
                         session=v.session, actor=ACTOR, in_txn=True,
                         data={"reason": reason or "", "final": bool(decision) or not alive,
                               "pid_signalled": pid, "decision": decision})
    if runner._runtime_mode(v.root, run) == "session":
        # every open job expires before the runner is signalled, so a result that arrives later
        # is priced and dropped (in-session-runtime.md section 3.4)
        from alpaca.research import jobs
        jobs.expire_priced(v.conn, v.root, run, "stopped", session=v.session)
    signalled = runner.request_stop(v.root, run) if alive else False
    return {"event": ev["id"], "signalled": signalled, "pid": pid}


def _do_extend(v: _Verb, run: str, *, decision_id: str | None, no_resume: bool) -> dict:
    """Section 4.3.4: the checks in order, then research-extend and the lift in one transaction,
    then the resume unless no_resume. rounds_from is runner.rounds_bound over the frozen contract
    (contract.read_frozen); a contract that no longer reads as frozen refuses the extend with
    read_frozen's RUN-CONTRACT-CHANGED, as the next launch would."""
    from alpaca import taskplan
    from alpaca.research import contract as contract_mod, holds, owner, runner
    start = _run_start(v.conn, run)
    if start is None:
        raise _Refused("RESEARCH-RUN-UNKNOWN", "no research-run-start names %s" % run, ref=run)
    ext = [h for h in holds.open_holds(v.conn, run) if h.get("gate") == "extension"]
    if not ext:
        raise _Refused("RESEARCH-RUN-NOT-HALTED", "%s has no open extension hold" % run, ref=run)
    if not no_resume:
        _session_check(v, run)           # a refusal here has changed nothing
    hold = ext[0]
    ok, decision = owner.authorized(v.conn, run, "extension", decision_id)
    if not ok:
        raise _Refused("RESEARCH-OWNER-GRANT-MISSING",
                       "no terminal-confirmed owner grant answers the extension hold %s of %s; "
                       "the run stays halted until the owner types the grant in a terminal of "
                       "their own" % (hold["id"], run), decision=hold, ref=run)
    grants = [g for g in _rows(v.conn, events.GRANT, run) if g["data"].get("decision") == decision]
    rounds_to = int(((grants[-1]["data"].get("terms") or {}) if grants else {}).get("rounds") or 0)
    try:
        frozen = contract_mod.read_frozen(v.root, run)
    except errors.ResearchRefusal as exc:
        raise _Refused(exc.reason, exc.detail or str(exc), ref=run)
    rounds_from = runner.rounds_bound(v.conn, run, frozen)
    if rounds_to <= rounds_from:
        raise _Refused("RESEARCH-GRANT-TERMS", "the grant %s names %d rounds, not above the "
                       "run's %d" % (decision, rounds_to, rounds_from), ref=run)
    need, other = _need(v.conn, run, start, rounds_to)
    if other + need > taskplan.MAX_TASKS:
        raise _Refused("RUN-PLAN-CAP", "%s needs %d task(s) at %d rounds and its op holds %d "
                       "other task(s), over the %d-task plan; the run stays halted"
                       % (run, need, rounds_to, other, taskplan.MAX_TASKS), ref=run)
    with db.transaction(v.conn):
        ev = events.emit(v.conn, events.EXTEND, run=run, ref=run, op=start["op"],
                         session=v.session, actor=ACTOR, in_txn=True,
                         data={"decision": decision, "hold": hold["id"],
                               "rounds_from": rounds_from, "rounds_to": rounds_to})
        holds.lift_hold(v.conn, v.root, hold=hold["id"], decision=decision, answer="extension",
                        session=v.session)
    out = {"event": ev["id"], "decision": decision, "hold": hold["id"],
           "rounds_from": rounds_from, "rounds_to": rounds_to, "resume": None}
    if not no_resume:
        out["resume"] = _launch(v, run, cause="extension", decision=decision, hold=hold["id"],
                                driver_cause="extend")
    return out


def _do_resume(v: _Verb, run: str, *, decision_id: str | None) -> dict:
    """Section 4.3.5: the cause (crash, budget, or the gate of a hold already lifted), the
    refusals, then the restart."""
    from alpaca.research import holds, owner, runner
    if _run_start(v.conn, run) is None:
        raise _Refused("RESEARCH-RUN-UNKNOWN", "no research-run-start names %s" % run, ref=run)
    fold = _fold(v, run)
    if fold.get("state") in _FINISHED or _owner_stopped(v.conn, run):
        raise _Refused("RESEARCH-RUN-FINISHED", "%s is %s; a finished, stopped or failed run is "
                       "not resumed" % (run, fold.get("state")), ref=run)
    if runner.is_alive(v.root, run):
        raise _Refused("RESEARCH-RUN-ACTIVE", "the loop runner of %s is alive; nothing to resume"
                       % run, ref=run)
    open_ = holds.open_holds(v.conn, run)
    ext = [h for h in open_ if h.get("gate") == "extension"]
    if ext:
        raise _Refused("RESEARCH-HOLD-OPEN", "%s is halted at its extension hold %s; the owner's "
                       "extension grant and `alpaca research extend %s` answer it"
                       % (run, ext[0]["id"], run), decision=ext[0], ref=run)
    _session_check(v, run)               # a refusal here has changed nothing
    bud = [h for h in open_ if h.get("gate") == "budget"]
    if bud:
        ok, decision = owner.authorized(v.conn, run, "budget", decision_id)
        if not ok:
            raise _Refused("RESEARCH-OWNER-GRANT-MISSING",
                           "no terminal-confirmed owner budget grant answers the budget hold %s "
                           "of %s; the run stays paused" % (bud[0]["id"], run),
                           decision=bud[0], ref=run)
        holds.lift_hold(v.conn, v.root, hold=bud[0]["id"], decision=decision, answer="budget",
                        session=v.session)
        cause, hold = "budget", bud[0]["id"]
    elif fold.get("state") == "resumable":
        lift = _rows(v.conn, events.HOLD_LIFTED, run)[-1]["data"]
        cause = lift.get("gate") if lift.get("gate") in ("extension", "budget") else "extension"
        decision, hold = lift.get("decision"), lift.get("hold")
    else:
        cause, decision, hold = "crash", None, None
    got = _launch(v, run, cause=cause, decision=decision, hold=hold)
    return dict(got, cause=cause, decision=decision, hold=hold)


# ------------------------------------------------------------------ start
def _loosen_detail(entries, runbook) -> str:
    lines = []
    sets = []
    for e in entries:
        new = util.canonical_json(e.get("new"))
        lines.append("%s: accepted %s, new %s"
                     % (e.get("field"), util.canonical_json(e.get("accepted")), new))
        sets.append("--set %s" % shlex.quote("%s=%s" % (e.get("field"), new)))
    ref = entries[0].get("ref") if entries else ""
    task = ref.rpartition("#")[2] if ref else "-"
    lines.append("owner, in your own terminal: %s --runbook %s%s loosen %s" % (
        _OWNER_CMD, shlex.quote(runbook), "" if task in ("", "-") else " --task %s" % task,
        " ".join(sets)))
    return "\n".join(lines)


def _start_refused(v: _Verb, exc, runbook: str, ref: str, op) -> int:
    """A refusal of runner.open_run: a loosened field opens a loosen hold on each contract ref
    the refusal names, a budget posture of HOLD a budget hold on the contract ref; then the one
    research-refused event, all in one transaction (sections 4.2 and 4.3.1)."""
    from alpaca.research import holds
    loosened = list(getattr(exc, "loosened", ()) or ())
    if exc.reason == "CONTRACT-LOOSEN-UNGRANTED" and loosened:
        by_ref: dict = {}
        for e in loosened:
            by_ref.setdefault(e.get("ref") or ref, []).append(e)
        opened = []
        with db.transaction(v.conn):
            for r, entries in by_ref.items():
                opened.append(holds.open_hold(v.conn, v.root, ref=r, gate="loosen",
                                              code=holds.HOLD_CODES["loosen"],
                                              detail=_loosen_detail(entries, runbook), op=op,
                                              bound=None, session=v.session,
                                              fields=[e.get("field") for e in entries
                                                      if e.get("field")],
                                              measured=getattr(exc, "measured", None)))
            v.record_refusal(exc.reason, exc.detail, ref=ref, exit=vc.PAUSED, in_txn=True)
        return v.refused(_Refused(exc.reason, "the contract loosens %d field(s) with no owner "
                                  "grant; %d loosen hold(s) opened" % (len(loosened), len(opened)),
                                  exit=vc.PAUSED, decision=opened[0], record=False), ref=ref)
    if exc.reason == "RUN-BUDGET-HOLD":
        # runner.open_run no longer raises this code (t-297, in-session-runtime.md section 21: the
        # start never asks the project budget posture); the branch stays for a runner that still
        # raises it, and test_research_cli pins its hold and the later lift on a passing start
        detail = "%s; the budget check holds the start of %s; the owner grants it, then start " \
                 "again" % (exc.detail, ref)
        with db.transaction(v.conn):
            hold = holds.open_hold(v.conn, v.root, ref=ref, gate="budget",
                                   code=holds.HOLD_CODES["budget"], detail=detail, op=op,
                                   bound=None, session=v.session)
            v.record_refusal(exc.reason, exc.detail, ref=ref, exit=vc.PAUSED, in_txn=True)
        return v.refused(_Refused(exc.reason, "the budget check holds the start; budget hold %s "
                                  "opened" % hold["id"], exit=vc.PAUSED, decision=hold, record=False),
                         ref=ref)
    return v.refused(_Refused(exc.reason, exc.detail or str(exc), exit=exc.code), ref=ref)


def _is_format3(path: str) -> bool:
    import yaml
    from alpaca import runbook as rb
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return False
    return rb.research_block(data) is not None


def _moot_or_still(v: _Verb, hold: dict, rel: str, task, cache: list) -> str | None:
    """Section 22 (t-306): None when `hold`, an open loosen hold with no owner grant, is moot
    (holds.moot_reason: it names fields, the re-check used the baseline the hold was measured
    against, and none of its fields loosens now); else the reason it stays open. Read-only: the
    start lifts a moot hold (holds.lift_moot) only after its other checks pass. The re-check is
    contract.loosen_recheck, read once per start (`cache`); a re-check that cannot run keeps the
    hold, so the check fails closed."""
    from alpaca.research import contract, holds
    if not holds.held_fields(hold):
        return "the hold names no field to re-check"
    if not cache:
        try:
            cache.append(contract.loosen_recheck(v.conn, v.root, rel, task))
        except Exception as exc:  # noqa: BLE001 - a re-check that cannot run lifts nothing
            cache.append({"error": "%s: %s" % (type(exc).__name__, _one_line(exc))[:300]})
    got = cache[0]
    if got.get("error"):
        return "the re-check of its fields could not run (%s)" % got["error"]
    return holds.moot_reason(v.conn, hold["id"], got)


def _start(v: _Verb) -> int:
    from alpaca.research import contract, holds, owner, runner, state
    a = v.args
    v.open()
    v.settle()
    try:
        level, _source = _level(v)
    except Exception as exc:  # noqa: BLE001 - an unreadable level is BLOCKED, as intake refuses
        raise _Refused("RESEARCH-LEVEL-UNREADABLE", "the level in force cannot be read (%s: %s); "
                       "nothing started" % (type(exc).__name__, exc), record=False)
    rel = v.rel_path(a.runbook)
    ref = contract.ref(rel, a.task)
    pack_ref = contract.ref(rel, None)
    model = getattr(a, "model", None)
    if model is not None:
        # an alias the shield policy does not map is refused before any other check
        _check_model(v, model, ref=ref)
    if not _is_format3(os.path.join(v.root, rel)):
        raise _Refused("RESEARCH-NOT-FORMAT-3", "%s is not a runbook: 3 file with a research "
                       "block" % rel, ref=ref)
    # check 2: an open hold on a run under the runbook, or on the contract refs
    for ev in _runs_under(v.conn, rel):
        for hold in holds.open_holds(v.conn, ev["ref"]):
            raise _Refused("RESEARCH-HOLD-OPEN", "run %s under %s has an open %s hold; the owner "
                           "decides first" % (ev["ref"], rel, hold["gate"]), decision=hold,
                           ref=ref)
    carried = []
    recheck, mooted = [], []
    for r in [ref] + ([pack_ref] if a.task else []):
        for hold in holds.open_holds(v.conn, r):
            if hold.get("gate") in ("loosen", "budget"):
                ok, decision = owner.authorized(v.conn, r, hold["gate"])
                if ok:
                    carried.append((hold, decision))
                    continue
            why = ""
            if hold.get("gate") == "loosen":
                # section 22 (t-306): a loosen hold whose named fields the contract no longer
                # loosens is lifted as moot, and the start goes on; freeze checks again
                still = _moot_or_still(v, hold, rel, a.task, recheck)
                if still is None:
                    mooted.append(hold["id"])     # lifted after checks 3 and 4
                    continue
                why = "; %s" % still
            raise _Refused("RESEARCH-HOLD-OPEN", "%s has an open %s hold%s; the owner decides "
                           "first" % (r, hold["gate"], why), decision=hold, ref=ref)
    # check 3: the owner's stop at a hold, until a reopen grant
    if a.control is None:
        stopped = owner._stopped_run(v.conn, rel)
        if stopped:
            raise _Refused("RESEARCH-RUNBOOK-STOPPED", "the owner stopped %s, the newest real run "
                           "under %s, at a hold; a new real run needs `%s --runbook %s reopen`"
                           % (stopped, rel, _OWNER_CMD, rel), ref=ref)
    # check 4: another live run of the same kind
    for ev in _runs_under(v.conn, rel):
        control = ev["data"].get("control")
        if (a.control is None and not control) or (a.control is not None and control == a.control):
            if not _finalized(v.conn, ev["ref"]):
                raise _Refused("RESEARCH-CONTRACT-BUSY", "run %s under %s has no finalize event"
                               % (ev["ref"], rel), ref=ref)
    # section 22: a moot loosen hold is lifted only now, after checks 3 and 4, so a start those
    # checks refuse leaves it open; lift_moot asks holds.moot_reason again before it writes
    for hid in mooted:
        holds.lift_moot(v.conn, v.root, hold=hid, recheck=recheck[0], session=v.session)
    try:
        started = runner.open_run(v.root, runbook=rel, task=a.task, op=a.op, control=a.control,
                                  via=a.via, level=level, session=v.session,
                                  **({"model": model} if model is not None else {}))
    except errors.ResearchRefusal as exc:
        return _start_refused(v, exc, rel, ref, a.op)
    run = started["run"]
    for hold, decision in carried:
        holds.lift_hold(v.conn, v.root, hold=hold["id"], decision=decision, answer=hold["gate"],
                        session=v.session)
    launched = runner.launch(v.root, run, foreground=bool(a.foreground), session=v.session)
    code = vc.PASS
    if a.foreground:
        code = launched.get("code")
        code = code if code in vc.VERDICT_BAND else vc.FAIL
    fold = state.fold(v.conn, run, v.root)
    open_ = holds.open_holds(v.conn, run)
    decision = _hold_view(open_[0]) if code == vc.PAUSED and open_ else None
    log = launched.get("log")
    log = os.path.relpath(log, v.root) if log else None
    budget = started.get("budget") or {}
    diff = started.get("override_diff") or []
    body = {"mode": started.get("mode"), "runbook": rel,
            "contract_sha256": started.get("contract_sha256"), "override_diff": diff,
            "level": level, "via": a.via, "control": a.control, "pid": launched.get("pid"),
            "log": log}
    if mooted:
        body["moot_lifted"] = mooted              # section 22: loosen holds lifted as moot
    if started.get("model"):
        body["model"] = started.get("model")      # the run's one model for every agent job
    diff_text = "; ".join("%s %s -> %s (%s)" % (d.get("field"), d.get("pack"), d.get("task"),
                                                d.get("direction")) for d in diff) or "none"
    caps = " / ".join("%s %s" % (budget[key], unit) for key, unit in
                      (("usd", "USD"), ("tokens", "tokens"), ("wall", "s wall"))
                      if budget.get(key) is not None) or "uncapped"
    control = ", control %s" % a.control if a.control else ""
    lines = ["research run %s started (%s, %s)%s" % (run, started.get("mode"), rel, control),
             "  contract sha256 %s, override diff: %s"
             % (str(started.get("contract_sha256") or "")[:12], diff_text),
             "  K=%s slots, N=%s rounds, budget %s, level %s, via %s"
             % (started.get("k"), started.get("n"), caps, level, a.via),
             "  runner pid %s, log %s" % (launched.get("pid"), log)]
    if started.get("model"):
        lines.append("  model %s for every agent job (tier override)" % started.get("model"))
    for hid in mooted:
        lines.append("  loosen hold %s lifted as moot: none of its fields loosens now" % hid)
    if decision is not None:
        lines += _hold_lines(decision)
    return v.done(code, {"run": run, "state": fold.get("state"), "decision": decision,
                         "start": body}, lines)


# ------------------------------------------------------------------ status
def _state_sha(parts) -> str:
    return util.sha256_hex(util.canonical_json(parts))


def _status_event(v: _Verb, ref: str, run, fold_state, round_n, hold_ids, sha, code) -> None:
    """research-status, written only when the session's last one for the same ref carries a
    different state_sha256 (section 4.3.2)."""
    row = v.conn.execute("SELECT data FROM events WHERE kind=? AND ref=? AND session=? ORDER BY id "
                         "DESC LIMIT 1", (events.STATUS, ref, v.session)).fetchone()
    if row is not None:
        try:
            if json.loads(row["data"] or "{}").get("state_sha256") == sha:
                return
        except (TypeError, ValueError):
            pass
    events.emit(v.conn, events.STATUS, run=run, ref=ref, op=v.run_op(run), session=v.session,
                actor=ACTOR, code=code,
                data={"state": fold_state, "round": round_n, "holds": hold_ids,
                      "state_sha256": sha} if run else
                {"run": None, "state": fold_state, "round": round_n, "holds": hold_ids,
                 "state_sha256": sha})


def _contract_holds(v: _Verb, runbook) -> list:
    from alpaca.research import holds
    if not runbook:
        return []
    prefix = "contract:%s#" % runbook
    return [h for h in holds.open_holds(v.conn) if str(h.get("ref") or "").startswith(prefix)]


def _metric_order(v: _Verb, run: str) -> list:
    """The metric ids in the order of the run-start `metrics` list, the order report.build
    reads (alpaca/research/report.py, `first`)."""
    start = _run_start(v.conn, run)
    listed = (start or {}).get("data", {}).get("metrics") or []
    return [m.get("id") for m in listed if isinstance(m, dict) and m.get("id")]


def _first_value(fold, which, order=()) -> tuple:
    """(id, value) of the first metric: the first id of the run-start list that `which` holds,
    or the first id in sorted order when the list names none of them."""
    metrics = (fold.get(which) or {}).get("metrics") or {}
    if not metrics:
        return None, None
    mid = next((m for m in order if m in metrics), None) or sorted(metrics)[0]
    return mid, metrics[mid]


def _headline(v: _Verb, run: str) -> dict:
    from alpaca.research import holds
    fold = _fold(v, run)
    info = _runner_info(v, run)
    live = holds.open_holds(v.conn, run)
    mid, value = _first_value(fold, "champion", _metric_order(v, run))
    return {"run": run, "mode": fold.get("mode"), "control": fold.get("control"),
            "state": fold.get("state"), "round": fold.get("round"),
            "rounds_n": fold.get("rounds_n"), "champion_metric": mid, "champion_value": value,
            "runner": info["runner"], "holds": [h["id"] for h in live],
            "_sha": [run, fold.get("state"), fold.get("round"), sorted(h["id"] for h in live),
                     (fold.get("champion") or {}).get("commit"), fold.get("counts")]}


def _status(v: _Verb) -> int:
    from alpaca.research import holds, state
    a = v.args
    v.open()
    v.settle()
    try:
        level, source = _level(v)
    except Exception:  # noqa: BLE001 - status still reads the runs
        # neither "record" nor "default": the skill's step 1 branches on those two words
        level, source = None, "unreadable"
    level_text = "level %s (%s)" % (level, _LEVEL_WORDS.get(source, source)) if level \
        else "level unreadable"
    if not a.run:
        rows = [_headline(v, run) for run in state.run_ids(v.conn)]
        sha = _state_sha([r.pop("_sha") for r in rows])
        all_holds = sorted({h for r in rows for h in r["holds"]})
        _status_event(v, "-", None, None, None, all_holds, sha, vc.PASS)
        lines = []
        for r in rows:
            lines.append("%s  %s%s  %s  round %s/%s  champion %s  runner %s  holds: %s" % (
                r["run"], r["mode"], "  control %s" % r["control"] if r["control"] else "",
                r["state"], "-" if r["round"] is None else r["round"], r["rounds_n"],
                "-" if r["champion_value"] is None else "%s %s" % (r["champion_value"],
                                                                   r["champion_metric"]),
                r["runner"], ", ".join(str(h) for h in r["holds"]) or "none"))
        if not rows:
            lines.append("no research runs on the record")
        lines.append(level_text)
        return v.done(vc.PASS, {"level": level, "level_source": source, "runs": rows}, lines)
    run = a.run
    if _run_start(v.conn, run) is None:
        raise _Refused("RESEARCH-RUN-UNKNOWN", "no research-run-start names %s" % run,
                       ref=run, record=False)
    fold = _fold(v, run)
    info = _runner_info(v, run)
    live = holds.open_holds(v.conn, run) + _contract_holds(v, fold.get("runbook"))
    failed = fold.get("state") == "failed" or bool(fold.get("control_failed"))
    code = vc.PAUSED if live else (vc.FAIL if failed else vc.PASS)
    sha = _state_sha([fold.get("state"), fold.get("round"), sorted(h["id"] for h in live),
                      (fold.get("champion") or {}).get("commit"), fold.get("counts")])
    _status_event(v, run, run, fold.get("state"), fold.get("round"),
                  sorted(h["id"] for h in live), sha, code)
    lines = _status_lines(fold, info, live, level_text, _metric_order(v, run))
    body = dict(fold, runner=info["runner"], pid=info["pid"], host=info["host"])
    if fold.get("driver"):
        body.update(_session_status(v, run, fold, info))
        lines += _session_status_lines(body)
    return v.done(code, {"level": level, "level_source": source, "run": body}, lines)


def _session_status(v: _Verb, run: str, fold: dict, info: dict) -> dict:
    """What `status` adds for a session run (in-session-runtime.md section 3.4): the driver and
    whether it is alive, the jobs out, the count of JOB-* refusals by code, the slots that are
    waiting, and the stat check of the engine pin and of the files the runtime loads into an
    agent, run now."""
    from alpaca.research import jobs, session_engine as eng, session_runtime
    drv = jobs.newest_driver(v.conn, run)
    alive = not session_runtime.driver_gone(v.conn, v.root, run) if drv else False
    out = {"driver": dict(fold["driver"], alive=alive, last_seen=None)}
    out["jobs"] = jobs.counts(v.conn, run)
    out["refusals"] = jobs.refusal_counts(v.conn, run)
    # R5: the driver's agent stops in the run's window beyond the jobs' (a stray names a job already
    # judged; the rest are spawns that are not jobs, at the owner's cost)
    beyond = jobs.extra_agents(v.conn, run)
    out["beyond"] = {"count": len(beyond),
                     "strays": [{"agent_id": b["agent_id"], "job": b["job"]}
                                for b in beyond if b["job"]],
                     "other": sum(1 for b in beyond if not b["job"])}
    out["waiting"] = [{"job": jid, "role": j["job"].get("role"), "state": j["state"]}
                      for jid, j in sorted(jobs.open_jobs(v.conn, run).items())]
    pin_ev = jobs.newest_pin(v.conn, run, run)
    pin = jobs.pin_of(pin_ev)
    if pin:
        got = eng.engine_unchanged(v.root, pin, driver_session=(drv or {}).get("data", {}).get("session"))
        out["engine"] = {"ok": got["ok"], "files": got["files"], "sha256": got["sha256"],
                         "changed": [c["path"] for c in got["changed"]],
                         "metadata": [c["path"] for c in got.get("metadata") or []]}
    lev = jobs.current_ledger_event(v.conn, run, run)
    ledger = eng.load_ledger((lev["data"] or {}).get("context")) if lev else None
    if ledger is not None and drv is not None:
        meta: list = []
        _cur, diff = jobs.context_now(v.conn, v.root, run, drv, ledger,
                                      check_session=(drv["data"] or {}).get("session"), metadata=meta)
        out["context"] = {"ok": not diff, "changes": [c["path"] for c in diff][:50],
                          "metadata": [c["path"] for c in meta][:50]}
    # the metadata-only changes the run's checks noted (t-360, section 24.1): they ended nothing
    noted = jobs.read_metadata_note(v.root, run)
    if noted:
        out["metadata_changes"] = [{k: m.get(k) for k in ("path", "group", "moved", "old", "new",
                                                          "point", "first_at")}
                                   for m in noted[:50]]
    return out


def _session_status_lines(body: dict) -> list:
    drv, jb = body.get("driver") or {}, body.get("jobs") or {}
    out = ["  driver %s (%s), %s; jobs issued %s, handed %s, claimed %s" % (
        drv.get("session"), drv.get("runtime"), "alive" if drv.get("alive") else "gone",
        jb.get("issued"), jb.get("handed"), jb.get("claimed"))]
    for w in body.get("waiting") or []:
        out.append("  waiting: %s (%s, %s)" % (w["job"], w["role"], w["state"]))
    by = body.get("beyond") or {}
    if by.get("count"):
        out.append("  agents beyond the jobs: %d (%d stray of a judged job, %d not a job)" % (
            by["count"], len(by.get("strays") or []), by.get("other") or 0))
    eng_ = body.get("engine") or {}
    if eng_:
        out.append("  engine %s, %s file(s)%s" % ("ok" if eng_.get("ok") else "CHANGED",
                                                  eng_.get("files"),
                                                  ": " + ", ".join(eng_.get("changed") or [])
                                                  if eng_.get("changed") else ""))
    meta = sorted({m["path"] for m in body.get("metadata_changes") or []}
                  | set((eng_ or {}).get("metadata") or [])
                  | set((body.get("context") or {}).get("metadata") or []))
    if meta:
        out.append("  metadata-only changes (owner, group or mode; no end): %s" % ", ".join(meta))
    return out


def _status_lines(fold, info, live, level_text="", order=()) -> list:
    counts = dict(fold.get("counts") or {})
    total = sum(counts.values())
    parts = ", ".join("%d %s" % (n, k) for k, n in sorted(counts.items()) if n)
    control = "  control %s" % fold["control"] if fold.get("control") else ""
    out = ["%s  %s%s  %s  round %s/%s  K=%s  run level %s  %s  via %s" % (
        fold["run"], fold.get("mode"), control,
        fold.get("state"), "-" if fold.get("round") is None else fold["round"],
        fold.get("rounds_n"), fold.get("slots_k"), fold.get("level"),
        level_text or "level unreadable", fold.get("via")),
        "  attempts %d%s" % (total, ": %s" % parts if parts else "")]
    if fold.get("mode") == "repair":
        items = fold.get("repair") or []
        out.append("  repair tasks %d, resolved %d" % (len(items),
                                                      sum(1 for i in items if i.get("resolved"))))
    else:
        mid, value = _first_value(fold, "champion", order)
        if mid is None:
            mid, base = _first_value(fold, "baseline", order)
        else:
            base = ((fold.get("baseline") or {}).get("metrics") or {}).get(mid)
        champ = fold.get("champion") or {}
        target = None
        for t in fold.get("targets") or []:
            if isinstance(t, dict) and t.get("metric") == mid:
                target = t.get("value")
        out.append("  champion %s %s (round %s, %s, commit %s); baseline %s; target %s"
                   % ("-" if value is None else value, mid or "", champ.get("round"),
                      champ.get("attempt"), str(champ.get("commit") or "")[:12],
                      "-" if base is None else base, "-" if target is None else target))
    bud = fold.get("budget") or {}
    cap_of = lambda key: "no cap" if bud.get(key) is None else bud.get(key)  # noqa: E731
    out.append("  budget %s of %s USD, %s of %s tokens%s; project posture %s (never holds a "
               "research run)" % (
                   bud.get("usd_spent"), cap_of("usd_cap"), bud.get("tokens_spent"),
                   cap_of("tokens_cap"),
                   ", %s launch(es) unpriced" % bud["unpriced"] if bud.get("unpriced") else "",
                   bud.get("posture")))
    if live:
        for hold in live:
            out += _hold_lines(hold)
    else:
        out.append("  holds: none")
    out.append("  last event %s (event %s); runner %s%s" % (
        fold.get("last_event_at"), fold.get("last_event_id"), info["runner"],
        " (pid %s)" % info["pid"] if info["runner"] == "alive" and info["pid"] else ""))
    return out


# ------------------------------------------------------------------ stop, extend, resume
def _stop(v: _Verb) -> int:
    from alpaca.research import holds, owner
    a = v.args
    run = a.run
    v.open()
    v.settle()
    if _run_start(v.conn, run) is None:
        raise _Refused("RESEARCH-RUN-UNKNOWN", "no research-run-start names %s" % run, ref=run)
    fold = _fold(v, run)
    if fold.get("state") in _FINISHED:
        raise _Refused("RESEARCH-RUN-FINISHED", "%s is already %s" % (run, fold.get("state")),
                       ref=run)
    held = [h for h in holds.open_holds(v.conn, run) if h.get("gate") in ("extension", "budget")]
    decision = None
    if held:
        try:
            got = owner.grant(v.conn, v.root, ref=run, gate="stop", terms={},
                              note=a.reason or "", session=v.session)
        except owner.OwnerRefusal as exc:
            if exc.reason == "RESEARCH-OWNER-NOT-TERMINAL":
                raise _Refused("RESEARCH-OWNER-GRANT-MISSING",
                               "%s waits at its open %s hold; stopping it answers the owner's "
                               "question, so the owner types `%s %s stop` in a terminal of "
                               "their own (%s)" % (run, held[0]["gate"], _OWNER_CMD, run,
                                                    exc.detail), decision=held[0], ref=run)
            raise _Refused(exc.reason, exc.detail, exit=exc.code, ref=run)
        decision = got["decision"]
    got = _do_stop(v, run, reason=a.reason, decision=decision, held=held)
    fold = _fold(v, run)
    lines = ["research run %s: stop recorded (event %s)%s" % (
        run, got["event"], ", runner signalled" if got["signalled"] else ""),
        "  state %s%s" % (fold.get("state"),
                          ", owner decision %s" % decision if decision else "")]
    # `decision` is the open hold that made a verb exit 3, or null (section 4.2); the owner's
    # decision id is in the text and on research-stop.
    return v.done(vc.PASS, {"run": run, "state": fold.get("state"), "decision": None}, lines)


def _repin(v: _Verb, run: str) -> int:
    """`--repin-engine`: the owner's record-only step (in-session-runtime.md section 3.4, item
    3). It makes none of the start checks, since the owner's terminal has no agent marker and no
    registered transcript, and starts no runner."""
    from alpaca.research import owner, session_runtime
    a = v.args
    try:
        got = session_runtime.repin(v.conn, v.root, run, session=v.session,
                                    context_paths=tuple(getattr(a, "context_path", None) or ()))
    except errors.ResearchRefusal as exc:
        raise _Refused(exc.reason, exc.detail, exit=exc.code, ref=run)
    except owner.OwnerRefusal as exc:
        raise _Refused(exc.reason, exc.detail, exit=exc.code, ref=run)
    if not got["recorded"]:
        return v.done(vc.PASS, {"run": run, "applied": False, "next": None, "changed": []},
                      ["research run %s: the engine and the files the runtime loads are as pinned; "
                       "nothing recorded" % run])
    nxt = "alpaca research resume %s" % run
    return v.done(vc.PASS, {"run": run, "applied": False, "next": nxt, "changed": got["changed"]},
                  ["research run %s: new pin %s recorded (%d change(s)); no runner started" % (
                      run, got["engine_sha256"][:12], len(got["changed"])),
                   "  next, in the driving session: %s" % nxt])


def _extend(v: _Verb) -> int:
    a = v.args
    v.open()
    v.settle()
    _run_model_arg(v, a.run, getattr(a, "model", None))
    if getattr(a, "repin_engine", False):
        return _repin(v, a.run)
    got = _do_extend(v, a.run, decision_id=a.decision, no_resume=bool(a.no_resume))
    fold = _fold(v, a.run)
    lines = ["research run %s extended from %s to %s rounds by decision %s (hold %s lifted)"
             % (a.run, got["rounds_from"], got["rounds_to"], got["decision"], got["hold"])]
    if got["resume"]:
        lines.append("  runner pid %s restarted (cause extension)" % got["resume"]["pid"])
    else:
        lines.append("  not resumed (--no-resume); `alpaca research resume %s` starts it" % a.run)
    return v.done(vc.PASS, {"run": a.run, "state": fold.get("state"), "decision": None}, lines)


def _resume(v: _Verb) -> int:
    a = v.args
    v.open()
    v.settle()
    _run_model_arg(v, a.run, getattr(a, "model", None))
    if getattr(a, "repin_engine", False):
        return _repin(v, a.run)
    got = _do_resume(v, a.run, decision_id=a.decision)
    fold = _fold(v, a.run)
    lines = ["research run %s resumed (cause %s%s), runner pid %s" % (
        a.run, got["cause"], ", decision %s" % got["decision"] if got["decision"] else "",
        got["pid"])]
    return v.done(vc.PASS, {"run": a.run, "state": fold.get("state"), "decision": None}, lines)


# ------------------------------------------------------------------ report
def _report(v: _Verb) -> int:
    from alpaca.research import holds, report
    a = v.args
    run = a.run
    v.open()
    v.settle()
    if _run_start(v.conn, run) is None:
        raise _Refused("RESEARCH-RUN-UNKNOWN", "no research-run-start names %s" % run, ref=run)
    out_dir = None
    if a.out:
        out_dir = os.path.join(v.root, v.rel_path(a.out))
    got = report.write(v.conn, v.root, run, out_dir, session=v.session)
    fold = _fold(v, run)
    open_ = holds.open_holds(v.conn, run)
    failed = fold.get("state") == "failed" or bool(fold.get("control_failed"))
    code = vc.FAIL if failed else (vc.PAUSED if open_ else vc.PASS)
    decision = _hold_view(open_[0]) if code == vc.PAUSED else None
    body = {k: got[k] for k in ("path", "sha256", "json_path", "json_sha256", "through_event",
                                "new")}
    lines = ["research report %s: %s (sha256 %s)" % (run, got["path"], got["sha256"][:12]),
             "  %s (sha256 %s), through event %s, %s" % (
                 got["json_path"], got["json_sha256"][:12], got["through_event"],
                 "recorded as event %s" % got["event"] if got["new"]
                 else "unchanged since event %s" % got["event"]),
             "  state %s" % fold.get("state")]
    if decision is not None:
        lines += _hold_lines(decision)
    return v.done(code, {"run": run, "state": fold.get("state"), "decision": decision,
                         "report": body}, lines)


# ------------------------------------------------------------------ grant
def _split_set(item: str) -> tuple:
    """PATH=VALUE: PATH runs to the first `=` outside brackets; a key in brackets runs to its
    closing bracket (section 4.3.7)."""
    depth = 0
    for i, ch in enumerate(item):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif ch == "=" and depth == 0:
            return item[:i], item[i + 1:]
    raise ValueError("%r has no = outside brackets" % item)


def _parse_sets(items) -> dict:
    import yaml
    from alpaca.research import owner
    out: dict = {}
    for item in items or []:
        try:
            path, text = _split_set(item)
            value = yaml.safe_load(text) if text.strip() else None
        except (ValueError, yaml.YAMLError) as exc:
            raise owner.OwnerRefusal(vc.FAIL, "RESEARCH-GRANT-TERMS",
                                     "--set %s cannot be read: %s" % (item, _one_line(exc)))
        if path in out:
            raise owner.OwnerRefusal(vc.FAIL, "RESEARCH-GRANT-TERMS",
                                     "--set names %s twice" % path)
        out[path] = value
    return {"set": out}


_GATE_OF = {"extend": "extension", "budget": "budget", "stop": "stop", "loosen": "loosen",
            "reopen": "reopen"}


def _runtime_mode_of(v: _Verb, run: str):
    from alpaca.research import runner
    return runner._runtime_mode(v.root, run)


def _grant(v: _Verb) -> int:
    from alpaca.research import contract, holds, owner
    a = v.args
    words = list(a.words or [])
    if a.runbook:
        if len(words) != 1 or words[0] not in ("loosen", "budget", "reopen"):
            raise _Usage("with --runbook, grant takes one of loosen, budget or reopen")
        rel = v.rel_path(a.runbook)
        if words[0] == "reopen" and a.task:
            raise _Usage("reopen takes no --task")
        ref = rel if words[0] == "reopen" else contract.ref(rel, a.task)
        run = None
    else:
        if len(words) != 2 or words[1] not in ("extend", "budget", "stop"):
            raise _Usage("grant RUN extend|budget|stop, or grant --runbook PATH "
                         "loosen|budget|reopen")
        run, ref = words[0], words[0]
        if a.task:
            raise _Usage("--task goes with --runbook")
    word = words[-1]
    gate = _GATE_OF[word]
    if a.rounds is not None and word != "extend":
        raise _Usage("--rounds goes with extend")
    if any(x is not None for x in (a.usd, a.tokens, a.wall)) and not (word == "budget"):
        raise _Usage("--usd, --tokens and --wall go with budget")
    if a.set and word != "loosen":
        raise _Usage("--set goes with loosen")
    if a.no_apply and word not in ("extend", "budget") or (a.no_apply and run is None):
        raise _Usage("--no-apply goes with RUN extend or RUN budget")
    v.open()
    v.settle()
    try:
        if gate == "extension":
            terms = {"rounds": a.rounds}
        elif gate == "budget":
            terms = {"usd": a.usd, "tokens": a.tokens, "wall": a.wall}
        elif gate == "loosen":
            terms = _parse_sets(a.set)
        else:
            terms = {}
        session_run = run is not None and word in ("extend", "budget") and \
            _runtime_mode_of(v, run) == "session"
        got = owner.grant(v.conn, v.root, ref=ref, gate=gate, terms=terms, note=a.note or "",
                          session=v.session, applied=not a.no_apply and not session_run)
    except owner.OwnerRefusal as exc:
        raise _Refused(exc.reason, exc.detail, exit=exc.code, ref=ref)
    lines = ["owner grant recorded: decision %s, event %s%s" % (
        got["decision"], got["event"], ", hold %s" % got["hold"] if got["hold"] else "")]
    body = {"decision": got["decision"], "event": got["event"], "hold": got["hold"],
            "applied": False}
    session_run = run is not None and word in ("extend", "budget") and \
        _runtime_mode_of(v, run) == "session"
    if session_run:
        # `grant` runs only at a terminal with no agent marker, so it is never the driver of a
        # session run and must not start a runner that has none: it records the grant and names the
        # verb the driving session types after it (in-session-runtime.md section 3.4)
        nxt = "alpaca research extend %s" % run if word == "extend" else \
            "alpaca research resume %s" % run
        body["next"] = nxt
        lines.append("  recorded only; in the driving session: %s" % nxt)
    elif run is not None and not a.no_apply:
        try:
            if gate == "extension":
                done = _do_extend(v, run, decision_id=got["decision"], no_resume=False)
                lines.append("  %s extended to %s rounds and resumed" % (run, done["rounds_to"]))
            elif gate == "budget":
                done = _do_resume(v, run, decision_id=got["decision"])
                lines.append("  %s resumed (cause %s)" % (run, done["cause"]))
            else:
                held = [h for h in holds.open_holds(v.conn, run)
                        if h.get("gate") in ("extension", "budget")]
                _do_stop(v, run, reason=a.note or "", decision=got["decision"], held=held)
                lines.append("  %s stopped" % run)
            body["applied"] = True
        except _Refused as exc:
            if not v.json:
                for line in lines:
                    print(line)
            return v.refused(exc, ref=run, extra={"grant": body})
    elif gate == "loosen":
        lines.append("  the next `alpaca research start` of %s may use the granted values" % ref)
    return v.done(vc.PASS, {"grant": body}, lines)


# ------------------------------------------------------------------ contracts, template
def _contracts(v: _Verb) -> int:
    from alpaca.research import catalog
    found = catalog.find(v.root)
    code = vc.BLOCKED if any(c.get("unreadable") for c in found) else vc.PASS
    lines = []
    for c in found:
        if c.get("unreadable"):
            lines.append("%s  cannot be read: %s" % (c["path"], c["error"]))
            continue
        burn = c.get("burn_in") or {}
        lines.append("%s  %s  %s  check %s  K=%s N=%s  contract %s  pack %s" % (
            c["path"], c.get("id"), c.get("mode"), c.get("check"), c.get("k"), c.get("n"),
            str(c.get("contract_sha256") or "-")[:12], str(c.get("pack_sha256") or "-")[:12]))
        lines.append("  burn-in %s, runs %s, holds %s%s%s" % (
            burn.get("state", "-"), ", ".join(c.get("runs") or []) or "none",
            ", ".join(str(h["hold"]) for h in c.get("holds") or []) or "none",
            ", reopen needed" if c.get("reopen_needed") else "",
            ", signed off at event %s" % c["signoff"] if c.get("signoff") else ""))
    if not found:
        lines.append("no format 3 runbooks are tracked in this project")
    return v.done(code, {"contracts": found}, lines)


def _template(v: _Verb) -> int:
    a = v.args
    rel = "templates/research/%s.yaml" % a.mode
    src = os.path.join(v.root, rel)
    try:
        with open(src, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise _Refused("RESEARCH-TEMPLATE-MISSING", "%s cannot be read: %s" % (rel, exc),
                       exit=vc.BLOCKED, record=False)
    if not a.out:
        if v.json:
            return v.done(vc.PASS, {"template": {"mode": a.mode, "path": rel}})
        print(text, end="" if text.endswith("\n") else "\n")
        return v.done(vc.PASS, {}, ())
    out = v.rel_path(a.out)
    target = os.path.join(v.root, out)
    if os.path.lexists(target):
        raise _Refused("RESEARCH-TEMPLATE-EXISTS", "%s exists; template never overwrites a file"
                       % out, record=False)
    os.makedirs(os.path.dirname(target) or v.root, exist_ok=True)
    with open(target, "x", encoding="utf-8") as fh:
        fh.write(text)
    return v.done(vc.PASS, {"template": {"mode": a.mode, "path": out}},
                  ["research template %s written to %s" % (a.mode, out)])



# ------------------------------------------------------------------ next-job and submit
def _job_refusal(exc, *, record: bool = False) -> "_Refused":
    from alpaca.research import jobs
    return _Refused(exc.code, exc.detail, exit=errors.CODES.get(exc.code, vc.FAIL), record=record)


def _judge_alive(v: _Verb, scope: str) -> bool:
    """Whether the process that owns the launch slots of the scope is alive: the runner of a run,
    the `alpaca proof verify` process of a verification (its pid is on its research-driver
    event)."""
    from alpaca.research import jobs, runner
    if paths.RUN_ID.fullmatch(scope):
        return runner.is_alive(v.root, scope)
    drv = jobs.newest_driver(v.conn, scope)
    pid = ((drv or {}).get("data") or {}).get("pid")
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    return True


def _hold_fix(v: _Verb, run: str) -> list:
    """The price-check commands that clear a budget hold whose cause is a result priced by no rate
    card: one for each model of `unpriced_models` in the run's research-job-result rows."""
    from alpaca.research import jobs
    out = []
    for ev in jobs.events_of(v.conn, run, (events.JOB_RESULT,)):
        for m in ev["data"].get("unpriced_models") or []:
            if isinstance(m, dict) and m.get("fix") and m["fix"] not in out:
                out.append(m["fix"])
    return out


def _next_job(v: _Verb) -> int:
    """`alpaca research next-job RUN [--wait S] [--limit N] [--json]`, `--verification VF`,
    `RUN --list`: hand out the jobs the runner has issued (in-session-runtime.md section 3.1)."""
    import time as _time
    from alpaca.research import holds, jobs, session_runtime
    a = v.args
    v.open()
    if bool(a.run) == bool(a.verification):
        raise _Usage("next-job takes RUN or --verification VF, not both and not neither")
    scope = a.run or a.verification
    run = a.run
    if run is not None and _run_start(v.conn, run) is None:
        raise _Refused("RESEARCH-RUN-UNKNOWN", "no research-run-start names %s" % run, ref=run,
                       record=False)
    drv = jobs.newest_driver(v.conn, scope)
    if drv is None:
        if run is None:
            raise _job_refusal(jobs.Problem("JOB-NOT-DRIVER", "verification %s has no driver" % scope))
    elif drv["data"].get("session") != v.session:
        raise _job_refusal(jobs.Problem("JOB-NOT-DRIVER", "%s is driven by session %s, not this one"
                                        % (scope, drv["data"].get("session"))))
    if a.list:
        rows = jobs.list_open(v.conn, scope)
        return v.done(vc.PASS, {"run": run, "open": rows, "jobs": []},
                      ["%d open job(s)" % len(rows)] + ["  %s  %s  %s  %s" % (
                          r["job"], r["role"], r["state"], r["agent_id"] or "-") for r in rows])
    wait_s = max(0.0, min(300.0, float(a.wait or 0)))
    limit = max(1, int(a.limit or 8))
    deadline = _time.monotonic() + wait_s
    while True:
        gone = run is not None and drv is not None and session_runtime.driver_gone(v.conn, v.root, run)
        if gone:
            # the driver is gone (a restart of its CLI process, t-360 section 24.2): hand nothing,
            # since the watch expires every open job at its next look; the ladder answers resume
            got = {"jobs": [], "context_changes": []}
        else:
            try:
                got = jobs.hand_off(v.conn, v.root, scope, v.session, limit=limit, run=run)
            except jobs.Problem as exc:
                raise _job_refusal(exc)
        state_word = None
        fold = _fold(v, run) if run else {}
        state_word = fold.get("state")
        all_holds = holds.open_holds(v.conn, run) if run else []
        # a ship hold is an owner gate about shipping the champion, opened when the run's report is
        # written; it does not pause the loop, so it never hides the jobs of the report task's
        # verification that come after it (in-session-runtime.md section 10). Every other hold wins
        # over every other action.
        open_holds = [h for h in all_holds if h.get("gate") != "ship"]
        ship_holds = [h for h in all_holds if h.get("gate") == "ship"]
        alive = _judge_alive(v, scope)
        counts = jobs.counts(v.conn, scope)
        any_open = bool(counts["issued"] + counts["handed"] + counts["claimed"])
        if open_holds:
            action = "hold"
        elif got["jobs"]:
            action = "spawn"
        elif run is not None and state_word in _FINAL_STATES and not alive and not any_open:
            action = "report"
        elif run is not None and not alive and state_word not in _FINAL_STATES:
            action = "resume"
        elif gone:
            action = "resume"
        elif run is None and not alive and not any_open:
            action = "report"
        else:
            action = "wait"
        if action != "wait" or _time.monotonic() >= deadline:
            break
        _time.sleep(0.5)
    expired = jobs.expired_since(v.conn, v.root, scope)
    hold = None
    shown = open_holds[0] if action == "hold" else (ship_holds[0] if action == "report"
                                                    and ship_holds else None)
    if shown is not None:
        hold = {"gate": shown.get("gate"), "code": shown.get("code"), "detail": shown.get("detail"),
                "fix": _hold_fix(v, run) if shown.get("gate") == "budget" else []}
    out_jobs = []
    for j in got["jobs"]:
        row = dict(j)
        if a.with_prompt and row.get("prompt_path"):
            try:
                with open(row["prompt_path"], encoding="utf-8") as fh:
                    row["prompt"] = fh.read()
            except OSError:
                row["prompt"] = None
        out_jobs.append(row)
    code = {"spawn": vc.PASS, "wait": vc.PASS, "report": vc.FAIL if state_word == "failed" else vc.PASS,
            "hold": vc.PAUSED, "resume": vc.BLOCKED}[action]
    payload = {"run": run, "state": state_word, "action": action,
               "runner": "alive" if alive else "gone",
               "driver": {"session": (drv or {}).get("data", {}).get("session"),
                          "alive": not session_runtime.driver_gone(v.conn, v.root, scope)
                          if drv else False},
               "open": counts, "expired": expired, "context_changes": got["context_changes"],
               "hold": hold, "jobs": out_jobs}
    lines = ["next-job %s: %s (%d job(s) to spawn, %d open)" % (
        scope, action, len(out_jobs), counts["issued"] + counts["handed"] + counts["claimed"])]
    for j in out_jobs:
        lines.append("  %s  %s  model %s" % (j["job"], j["role"], j["model"]))
    return v.done(code, payload, lines)


_FINAL_STATES = ("finished", "stopped", "failed")
#: how long submit waits for the claim of a group member's sibling before it answers
#: JOB-GROUP-WAIT (fix batch 3, F18)
GROUP_GRACE_S = 3.0


def _submit(v: _Verb) -> int:
    """`alpaca research submit JOB --nonce N --agent-id ID ...`: file the driver's claim that an
    agent finished a job and wait for the judge's answer (in-session-runtime.md section 3.2). The
    verb runs only the checks that need no witness and writes only the claim."""
    import time as _time
    from alpaca.research import jobs
    a = v.args
    v.open()
    usage = None
    if a.usage_json:
        try:
            with open(a.usage_json, encoding="utf-8") as fh:
                usage = json.load(fh)
        except (OSError, ValueError) as exc:
            raise _Usage("--usage-json %s cannot be read: %s" % (a.usage_json, exc))
    args = {"nonce": a.nonce, "agent_id": a.agent_id, "transcript": a.transcript, "final": a.final,
            "usage": usage, "tool": a.tool, "tool_use_id": a.tool_use_id,
            "diagnose": bool(getattr(a, "diagnose", False))}
    try:
        filed = jobs.claim(v.conn, v.root, a.job, args, caller_session=v.session,
                           in_subagent=False, session=v.session)
    except jobs.Problem as exc:
        raise _job_refusal(exc)
    scope, n = filed["scope"], filed["n"]
    wait_s = max(0.0, min(300.0, float(a.wait if a.wait is not None else 60)))
    deadline = _time.monotonic() + wait_s
    # the judge of a verifier group member waits for its sibling's claim: while the sibling has
    # none after a short grace (a submit of the sibling running beside this one), the verb answers
    # JOB-GROUP-WAIT at once, so a driver that submits finished jobs one by one goes on to the
    # sibling instead of waiting here (fix batch 3, F18)
    group_grace = _time.monotonic() + min(wait_s, GROUP_GRACE_S)
    reply = filed["reply"]
    while reply is None:
        reply = jobs.read_reply(v.root, scope, a.job, n)
        if reply is not None:
            break
        if not _judge_alive(v, scope):
            raise _job_refusal(jobs.Problem("JOB-PENDING", "the judge of %s is not running; resume the "
                                            "run" % scope))
        if _time.monotonic() >= group_grace:
            waiting = jobs.unclaimed_siblings(v.conn, scope, a.job, root=v.root)
            if waiting:
                reply = {"code": "JOB-GROUP-WAIT", "retriable": True, "waiting": waiting, "n": n,
                         "reason": "claim %d of %s is filed; the judge waits for the claim of its "
                         "sibling %s: submit the sibling when its agent finishes, then repeat this "
                         "call" % (n, a.job, ", ".join(waiting))}
                break
        if _time.monotonic() >= deadline:
            raise _job_refusal(jobs.Problem("JOB-PENDING", "the judge has not answered claim %d "
                                            "yet; repeat the call" % n))
        _time.sleep(0.25)
    if reply.get("accepted"):
        body = {k: v_ for k, v_ in reply.items() if k != "verdict"}
        lines = ["job %s accepted (agent %s, outcome %s, launch end %s)" % (
            a.job, reply.get("agent_id"), reply.get("outcome"), reply.get("launch_end"))]
        if reply.get("table"):
            lines += ["checks of the claim:"] + jobs.render_table(reply["table"])
        return v.done(vc.PASS, body, lines)
    band = errors.CODES.get(reply.get("code"), vc.FAIL)
    if reply.get("retriable"):
        band = vc.BLOCKED
    payload = {"code": reply.get("code"), "reason": reply.get("reason"), "job": a.job,
               "retriable": bool(reply.get("retriable"))}
    if reply.get("waiting"):
        payload["waiting"] = reply["waiting"]
    if reply.get("table"):
        payload["table"] = reply["table"]
    if v.json:
        print(_dump(dict(payload, verdict=vc.name_of(band))))
    else:
        print("research submit: %s" % reply.get("reason"))
        if reply.get("table"):
            print("checks of the claim:")
            for line in jobs.render_table(reply["table"]):
                print(line)
        print("%s (%s: %s)" % (vc.gate_line("alpaca-research-submit", band), reply.get("code"),
                               _one_line(reply.get("reason"))))
    return band


# ------------------------------------------------------------------ dispatch
#: the sub-verbs that drive a run: a shell that carries the subagent marker never runs them
DRIVING = ("start", "resume", "extend", "stop", "next-job", "submit")
_HANDLERS = {"start": _start, "status": _status, "stop": _stop, "extend": _extend,
             "resume": _resume, "report": _report, "grant": _grant, "contracts": _contracts,
             "template": _template, "next-job": _next_job, "submit": _submit}


@cli.command("research")
def command(args) -> int:
    """`alpaca research <verb>`: dispatch one sub-verb of operator-surface section 4.3."""
    verb = getattr(args, "research_verb", None)
    if verb not in _HANDLERS:
        print("usage: alpaca research {%s} ..." % ",".join(VERBS))
        return vc.USAGE
    v = _Verb(args, verb)
    try:
        marked = agent_marker.current() if verb in DRIVING else None
        if marked:
            # the guard put the marker in this shell: the call comes from a subagent (section 3.4)
            raise _Refused("RUN-DRIVER-IS-SUBAGENT", agent_marker.refusal_reason(
                "alpaca research %s" % verb, marked), record=False)
        return _HANDLERS[verb](v)
    except _Usage as exc:
        print("alpaca research %s: %s" % (verb, exc))
        return vc.USAGE
    except _Refused as exc:
        return v.refused(exc, ref=getattr(args, "run", None))
    finally:
        v.close()


def parser(sub):
    """cli.register_parser('research', parser): the nine sub-verbs under dest research_verb."""
    entry = sub.add_parser("research", help="the research engine: start, watch, stop, extend, "
                           "resume and report a research run; the owner's grant; the contracts "
                           "and the contract templates")
    verbs = entry.add_subparsers(dest="research_verb")
    p = verbs.add_parser("start", help="start a research run under a format 3 runbook")
    p.add_argument("--runbook", required=True, help="a runbook: 3 file, relative to the root")
    p.add_argument("--task", help="the task whose contract's research part overrides the pack")
    p.add_argument("--op", help="the op that holds the run's attempt tasks")
    p.add_argument("--control", choices=("noop", "random"), help="run a control agent")
    p.add_argument("--via", choices=("cli", "autoresearch"), default="cli",
                   help="what started the run (the skill passes autoresearch)")
    p.add_argument("--model", metavar="ALIAS",
                   help="one model alias of the shield policy (opus, sonnet, ...) for every agent "
                   "job of the run, recorded on its start; without it each role keeps its tier")
    p.add_argument("--foreground", action="store_true", help="run the loop runner in this process")
    p.add_argument("--json", action="store_true")
    p = verbs.add_parser("status", help="the runs, or one run, folded from the record")
    p.add_argument("run", nargs="?")
    p.add_argument("--json", action="store_true")
    p = verbs.add_parser("stop", help="stop a run (at an owner hold, the owner's terminal check)")
    p.add_argument("run")
    p.add_argument("--reason")
    p.add_argument("--json", action="store_true")
    p = verbs.add_parser("extend", help="lift an extension hold with the owner's grant and resume")
    p.add_argument("run")
    p.add_argument("--decision")
    p.add_argument("--no-resume", action="store_true")
    p.add_argument("--model", metavar="ALIAS",
                   help="the run's model override, repeated: refused unless the run's start "
                   "recorded this same model")
    p.add_argument("--repin-engine", action="store_true",
                   help="the owner's record-only step after a stop with RUN-ENGINE-CHANGED")
    p.add_argument("--context-path", action="append", metavar="PATH",
                   help="with --repin-engine: a file the runtime loaded that no root holds")
    p.add_argument("--json", action="store_true")
    p = verbs.add_parser("resume", help="start a halted, paused or killed run again")
    p.add_argument("run")
    p.add_argument("--decision")
    p.add_argument("--model", metavar="ALIAS",
                   help="the run's model override, repeated: refused unless the run's start "
                   "recorded this same model")
    p.add_argument("--repin-engine", action="store_true",
                   help="the owner's record-only step after a stop with RUN-ENGINE-CHANGED")
    p.add_argument("--context-path", action="append", metavar="PATH",
                   help="with --repin-engine: a file the runtime loaded that no root holds")
    p.add_argument("--json", action="store_true")
    p = verbs.add_parser("report", help="build the run report from the record")
    p.add_argument("run")
    p.add_argument("--out", help="a folder inside the root (default .alpaca/reports/research/RUN)")
    p.add_argument("--json", action="store_true")
    p = verbs.add_parser(
        "grant", help="the owner's grant, typed at a terminal outside any agent session",
        description="grant RUN extend --rounds M | grant RUN budget [--usd X] [--tokens T] "
                    "[--wall S] | grant RUN stop | grant --runbook PATH [--task TASK] loosen "
                    "--set FIELD=VALUE ... | grant --runbook PATH [--task TASK] budget | "
                    "grant --runbook PATH reopen")
    p.add_argument("words", nargs="+", metavar="[RUN] GATE")
    p.add_argument("--runbook")
    p.add_argument("--task")
    p.add_argument("--rounds", type=int)
    p.add_argument("--usd", type=float)
    p.add_argument("--tokens", type=int)
    p.add_argument("--wall", type=int)
    p.add_argument("--set", action="append", metavar="FIELD=VALUE")
    p.add_argument("--note")
    p.add_argument("--no-apply", action="store_true")
    p.add_argument("--json", action="store_true")
    p = verbs.add_parser("contracts", help="the format 3 runbooks with their hash and burn-in")
    p.add_argument("--json", action="store_true")
    p = verbs.add_parser("next-job", help="the driving session asks for the jobs the runner has "
                         "issued, to spawn one subagent for each")
    p.add_argument("run", nargs="?")
    p.add_argument("--verification", help="a verification outside a run (proof verify)")
    p.add_argument("--wait", type=float, default=0, help="wait up to this many seconds (at most "
                   "300) for a job to hand out")
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--list", action="store_true", help="list the open jobs, never a spawn prompt")
    p.add_argument("--with-prompt", action="store_true", help="add each job's prompt (a scripted "
                   "session)")
    p.add_argument("--json", action="store_true")
    p = verbs.add_parser("submit", help="the driving session files the claim that a subagent "
                         "finished a job")
    p.add_argument("job")
    p.add_argument("--nonce", required=True)
    p.add_argument("--agent-id", required=True)
    p.add_argument("--transcript")
    p.add_argument("--final")
    p.add_argument("--usage-json")
    p.add_argument("--tool")
    p.add_argument("--tool-use-id")
    p.add_argument("--diagnose", action="store_true",
                   help="evaluate every check of the judge, not only up to the first failure, and "
                   "print and record the table of results")
    p.add_argument("--wait", type=float, default=60)
    p.add_argument("--json", action="store_true")
    p = verbs.add_parser("template", help="the fill-in research contract template")
    p.add_argument("--mode", choices=("improve", "repair"), required=True)
    p.add_argument("--out")
    p.add_argument("--json", action="store_true")
    return entry


cli.register_parser("research", parser)
