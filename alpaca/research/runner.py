"""The loop runner: open_run (freeze, preflight, run start), launch, the loop over rounds, stop
rules, holds, signals, finalize and forensics. Run as `python3 -m alpaca.research.runner --run
<id>`.

One runner process per run, guarded by an exclusive flock on .alpaca/research/<run>/runner.lock,
taken with worker_launch.ReentryLock and held for the life of the process (detached or
--foreground); liveness is that lock (is_alive).
The runner writes under session research-<run> with actor research-runner and leases its tasks
with worker <run>. Each round is a formation.controller plan (lifecycle.round_plan), so a killed
runner started again resumes at the first step that did not pass, and every step that is not
idempotent finds its earlier result on the record first. The loop stops at the targets, or at N
rounds (or the owner's granted bound) with an `extension` hold, exit 3; a budget HOLD before a
launch opens a `budget` hold, exit 3; a stop finalizes `stopped`. Every exit, early exits and
errors included, records one research-finalize event with the run summary, and an early exit
keeps the runner log and the environment snapshot under forensics/ (FR-046).

In a session run (runtime_mode session, in-session-runtime.md) the runner is the judge of every
agent launch: the slot threads park in session_runtime.wait until the driving session's claim has
passed the checks of jobs.py. A thread watches the driver and the engine pin every 30 seconds and
ends the run `interrupted` (resumable) when the driver is gone or a pinned file was touched.

Design: design/research-engine/architecture.md sections 3.8, 4 and 10, and
design/research-engine/in-session-runtime.md sections 2, 10 and 12.5."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

from alpaca import claims as leases
from alpaca import db, util
from alpaca.gates import verdict as vc
from alpaca.research import errors, events, paths
from alpaca.research.types import Contract, Proposal, RunContext, SpendCheck  # noqa: F401

ACTOR = events.ACTOR
_REAP_S = 5.0                    # the orphan reaper's pass interval while the runner lives
_HEARTBEAT_S = 20 * 60           # the heartbeat renews the runner's leases this often (4.2)
_LEASE_MINUTES = 60
_LOCK_POLL_S = 0.25
_GIB = 1024 ** 3


# ======================================================================== small helpers
def _log(root: str, run: str, text: str) -> None:
    """One line into runner.log (the detached runner's stdout goes there too)."""
    try:
        path = paths.runner_log(root, run)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (util.now_iso(), text))
    except (OSError, ValueError):
        pass


def _start_ticks(pid: int):
    from alpaca.research import procs
    return procs._start_ticks(pid)


_PR_SET_CHILD_SUBREAPER, _PR_GET_CHILD_SUBREAPER = 36, 37      # linux/prctl.h


def _subreaper_flag():
    """This process's child-subreaper flag (prctl PR_GET_CHILD_SUBREAPER), or None."""
    import ctypes
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        value = ctypes.c_int(0)
        if libc.prctl(_PR_GET_CHILD_SUBREAPER, ctypes.byref(value), 0, 0, 0) != 0:
            return None
        return int(value.value)
    except (OSError, AttributeError, ctypes.ArgumentError):
        return None


def _set_subreaper(flag: int) -> None:
    import ctypes
    try:
        ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_CHILD_SUBREAPER, int(flag), 0, 0, 0)
    except (OSError, AttributeError, ctypes.ArgumentError):
        pass


def _events(conn, kind: str, ref: str | None = None) -> list:
    from alpaca.research import lifecycle
    return lifecycle._events(conn, kind, ref)


def _opened_by(hold: dict, repair_task: str, opens: list) -> bool:
    """True when the extension hold was opened by repair_task's `rounds` step: the repair tasks
    run one after another (architecture.md section 4.3), so that hold comes after the task's
    first research-round-open with no other repair task's round-open in between, and its detail
    names the task (repair._Plan.rounds writes "<task> reached <bound> fix rounds ...")."""
    mine = [o["id"] for o in opens if o["data"].get("repair_task") == repair_task]
    if not mine or hold["id"] < mine[0]:
        return False
    if any(mine[0] < o["id"] < hold["id"] and o["data"].get("repair_task") != repair_task
           for o in opens):
        return False
    return str(hold["data"].get("detail") or "").startswith("%s reached " % repair_task)


def rounds_bound(conn, run: str, contract, *, repair_task: str | None = None) -> int:
    """N, or the newest owner extension: a research-extend counts only when the extension
    hold it cites was lifted by that same decision with the answer "extension" (the
    research-hold-lifted the extend verb records with it, operator-surface section 4.5), so
    an extend with the hold still open, or citing a hold the owner lifted with "stop" or by
    another decision, raises nothing (FR-040). The loop's improve rounds read it with
    repair_task None. A repair task's fix rounds (repair.repair_plan's `rounds` step) pass
    their repair task: the bound is per task (architecture.md section 4.3, "the granted
    `rounds_to` becomes that repair task's bound"), so only an extension of a hold that task's
    own `rounds` step opened counts, and an extension granted to an earlier repair task leaves
    a later one at N."""
    n = int(contract.rounds)
    lifted = {}
    for ev in _events(conn, events.HOLD_LIFTED, run):
        try:
            lifted.setdefault(int(ev["data"].get("hold")), ev["data"])
        except (TypeError, ValueError):
            continue
    opens = _events(conn, events.ROUND_OPEN, run) if repair_task is not None else []
    extension = {h["id"] for h in _events(conn, events.HOLD, run)
                 if h["data"].get("gate") == "extension"
                 and (repair_task is None or _opened_by(h, repair_task, opens))}
    for ev in _events(conn, events.EXTEND, run):
        try:
            hold = int(ev["data"].get("hold"))
        except (TypeError, ValueError):
            continue
        lift = lifted.get(hold)
        if hold not in extension or lift is None:
            continue
        if lift.get("answer") != "extension" or not ev["data"].get("decision") or \
                lift.get("decision") != ev["data"].get("decision"):
            continue
        try:
            n = max(n, int(ev["data"].get("rounds_to")))
        except (TypeError, ValueError):
            continue
    return n


def _run_start(conn, run: str) -> dict | None:
    got = _events(conn, events.RUN_START, run)
    return got[0] if got else None


def _runtime_version(runtime: str | None, binary: str | None) -> str | None:
    """The runtime's version for research-run-start, read the way the launcher reads it for its
    research-launch-end (launcher._runtime_version: from the install path); None for the control
    runtime, a binary it cannot name, or one whose install folder it cannot read."""
    if not runtime or not binary:
        return None
    from alpaca.research import launcher
    try:
        return launcher._runtime_version(runtime, binary)
    except Exception:  # noqa: BLE001 - the version is a record field, never a start refusal
        return None


def _disk_free(root: str) -> int:
    """Free bytes where the attempt worktrees land (the parent of paths.worktrees_dir, section
    8.4: .alpaca/worktrees/research/), measured on the nearest folder of that path that
    exists."""
    where = os.path.dirname(paths.worktrees_dir(root, "rr-000"))
    while where and not os.path.isdir(where):
        parent = os.path.dirname(where)
        if parent == where:
            break
        where = parent
    return shutil.disk_usage(where or root).free


def _check_disk(root: str, gib: float) -> None:
    free = _disk_free(root)
    need = float(gib or 0) * _GIB
    if free < need:
        raise errors.refusal("RUN-DISK-LOW", "%.3f GiB free where the worktrees land "
                             "(.alpaca/worktrees/research/), under the declared %.3f GiB"
                             % (free / _GIB, float(gib)))


def _check_runtime(runtime: str | None, root: str) -> tuple:
    from alpaca.research import launcher
    try:
        name = runtime or launcher.detect_runtime(root=root)
        binary = launcher.resolve_binary(name)
    except Exception as exc:  # noqa: BLE001 - LaunchError or any failure to find a runtime
        raise errors.refusal("RUN-RUNTIME-MISSING", str(exc))
    return name, binary


def _check_session_runtime(runtime: str | None, root: str) -> tuple:
    """(runtime name, mode). `mode` is "session" for a real driver: the Claude Code or Codex session
    in use, read from its markers (in-session-runtime.md section 3.4); "test" for runtime
    `control`, whose agents are scripts run by the launcher or stand-ins a test installs. No
    runtime binary is looked up: the session spawns the agents itself. Raises RUN-RUNTIME-MISSING
    when no session is in use, or when `runtime` names the other one."""
    from alpaca.research import launcher
    if runtime == "control":
        return "control", "test"
    marker = launcher.runtime_in_use()
    if marker is None:
        raise errors.refusal("RUN-RUNTIME-MISSING", "no Claude Code or Codex session is in use: a "
                             "research run is driven by the session that spawns its agents")
    if runtime in ("claude", "codex") and runtime != marker:
        raise errors.refusal("RUN-RUNTIME-MISSING", "this session runs %s, not %s" % (marker, runtime))
    return marker, "session"


def _runtime_mode(root: str, run: str) -> str | None:
    """The runtime_mode the run-start event of `run` records (None for a run recorded before the
    session runtime, which is a headless run)."""
    conn = None
    try:
        conn = db.connect_readonly(root)
        row = conn.execute("SELECT data FROM events WHERE kind=? AND ref=? ORDER BY id LIMIT 1",
                           (events.RUN_START, run)).fetchone()
        return (json.loads(row["data"] or "{}") if row else {}).get("runtime_mode")
    except Exception:  # noqa: BLE001
        return None
    finally:
        if conn is not None:
            conn.close()


def _check_jail() -> str:
    from alpaca.research import jail
    ok, why = jail.available()
    if not ok:
        raise errors.refusal("RUN-JAIL-UNAVAILABLE", str(why))
    return str(why)


def _task_count(conn, op: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM tasks WHERE op=?", (op,)).fetchone()[0]


def plan_need(*, slots: int, mode: str | None, repairs: int, control: str | None,
              noop_rounds: int, rounds: int) -> int:
    """The tasks a run adds to its op's task plan (architecture.md section 4.1, step 5): the
    report task, then the baseline task and K attempts a round for improve and the controls, or
    per repair task its own task and K attempts a round. open_run and `alpaca research extend`
    both count with this one rule."""
    k = int(slots)
    if control:
        return 1 + 1 + k * int(noop_rounds)
    if mode == "repair":
        return 1 + int(repairs) * (1 + k * int(rounds))
    return 1 + 1 + k * int(rounds)


def _plan_need(contract, control: str | None, rounds: int | None = None) -> int:
    return plan_need(slots=contract.slots, mode=contract.mode, repairs=len(tuple(contract.repair)),
                     control=control, noop_rounds=contract.noop_rounds,
                     rounds=rounds if rounds is not None else contract.rounds)


def _allocate(root: str, conn) -> str:
    """rr-%03d, one past the highest id among research-run-start events and folders under
    .alpaca/research/, taken with an exclusive os.mkdir; a race moves to the next number."""
    top = 0
    for row in conn.execute("SELECT ref FROM events WHERE kind=?", (events.RUN_START,)).fetchall():
        ref = row["ref"] or ""
        if paths.RUN_ID.fullmatch(ref):
            top = max(top, int(ref[3:]))
    base = paths.research_dir(root)
    os.makedirs(base, exist_ok=True)
    for name in os.listdir(base):
        if paths.RUN_ID.fullmatch(name):
            top = max(top, int(name[3:]))
    n = top + 1
    while True:
        run = "rr-%03d" % n
        try:
            os.mkdir(paths.run_dir(root, run))
            return run
        except FileExistsError:
            n += 1


def _same_contract_runs(conn, contract_sha: str) -> list:
    out = []
    for ev in _events(conn, events.RUN_START):
        if ev["data"].get("contract_sha256") == contract_sha:
            out.append(ev)
    return out


def _hash_checks(conn, contract_sha: str, runbook: str, control: str | None) -> None:
    """Step 4a of section 4.1: the start verb's checks 2 to 4 over every run with the same
    contract hash, whatever its runbook path."""
    from alpaca.research import holds
    runs = _same_contract_runs(conn, contract_sha)
    for ev in runs:
        if holds.open_holds(conn, ev["ref"]):
            raise errors.refusal("RESEARCH-HOLD-OPEN", "run %s under the same contract has an "
                                 "open hold" % ev["ref"])
    if control is None:
        real = [ev for ev in runs if not ev["data"].get("control")]
        if real:
            newest = real[-1]
            stops = [g for g in _events(conn, events.GRANT, newest["ref"])
                     if g["data"].get("gate") == "stop"]
            if stops:
                reopens = [g for g in _events(conn, events.GRANT)
                           if g["data"].get("gate") == "reopen" and g["id"] > stops[-1]["id"]
                           and g["ref"] in (runbook, newest["data"].get("runbook"))]
                if not reopens:
                    raise errors.refusal("RESEARCH-RUNBOOK-STOPPED", "the owner stopped run %s "
                                         "under this contract and granted no reopen"
                                         % newest["ref"])
    for ev in runs:
        if bool(ev["data"].get("control")) != bool(control):
            continue
        if control is not None and ev["data"].get("control") != control:
            continue
        if not _events(conn, events.FINALIZE, ev["ref"]):
            raise errors.refusal("RESEARCH-CONTRACT-BUSY", "run %s under the same contract has "
                                 "no finalize" % ev["ref"])


def _git_tracked(where: str, pathspec: str) -> list | None:
    """The paths under pathspec that the git work tree at `where` tracks, or None when `where`
    is not in a git work tree. Raises RUN-STORE-TRACKED when git cannot answer (git missing, a
    timeout, an error other than "not a git repository"), so the check fails closed."""
    def git(*args):
        try:
            return subprocess.run(["git", "-C", where] + list(args), capture_output=True,
                                  timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            raise errors.refusal("RUN-STORE-TRACKED", "the reproducer store could not be "
                                 "checked: git did not run in %s (%s)" % (where, exc))
    inside = git("rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0:
        if b"not a git repository" in inside.stderr.lower():
            return None
        raise errors.refusal("RUN-STORE-TRACKED", "the reproducer store could not be checked: "
                             "git rev-parse in %s exited %d: %s" % (
                                 where, inside.returncode,
                                 inside.stderr.decode("utf-8", "replace").strip()[:300]))
    if inside.stdout.strip() != b"true":
        return None
    got = git("ls-files", "-z", "--full-name", "--", pathspec)
    if got.returncode != 0:
        raise errors.refusal("RUN-STORE-TRACKED", "the reproducer store could not be checked: "
                             "git ls-files in %s exited %d: %s" % (
                                 where, got.returncode,
                                 got.stderr.decode("utf-8", "replace").strip()[:300]))
    return [n.decode("utf-8", "replace") for n in got.stdout.split(b"\0") if n]


def _store_tracked(root: str, contract) -> None:
    """FR-006, EC-051: no path under the reproducer store may be tracked. The store is
    <root>/contracts/research (paths.reproducer_store), so it is checked in the git work tree of
    the project root, and also in the target repository (contract.repo_path) when that is a
    different work tree that holds the store path. A folder that is not in a git work tree has
    nothing tracked; any other git failure refuses."""
    store = paths.reproducer_store(root)
    repo = contract.repo_path if os.path.isabs(contract.repo_path) else \
        os.path.join(root, contract.repo_path)
    repo = os.path.realpath(repo)
    places = [(root, os.path.relpath(store, root))]
    rel = os.path.relpath(store, repo)
    if repo != os.path.realpath(root) and rel != ".." and not rel.startswith(".." + os.sep):
        places.append((repo, rel))
    for where, pathspec in places:
        names = _git_tracked(where, pathspec)
        if names:
            raise errors.refusal("RUN-STORE-TRACKED", "%d path(s) of the reproducer store are "
                                 "tracked by git in %s, for example %s"
                                 % (len(names), where, names[0]))


def _stub(fn) -> bool:
    return str(getattr(fn, "__doc__", "") or "").startswith("Stub:")


# ======================================================================== open_run
def open_run(root: str, *, runbook: str, task: str | None, op: str | None, control: str | None,
             via: str, level: str, session: str, runtime: str | None = None,
             model: str | None = None) -> dict:
    """Freeze the contract, run the preflight (section 4.1), open the run's op and group, add the
    report task and, in a repair run, the repair tasks, record research-run-start. level is the
    word L1..L6 the verb read with level.in_force (section 4.4); the runner only records it.
    `model` is the run's model override (`start --model`, an alias the shield policy maps): the
    run-start records it as `model`, and every agent job of the run names it (in-session-runtime.md
    section 4.3); None keeps the shield tier of each role.
    Returns the run-start data. Raises ResearchRefusal at the first refusal and records nothing
    for it: the start verb records the one research-refused event and any hold (section 4.1)."""
    from alpaca import batch, ops, taskplan, version
    from alpaca.formation import dispatch
    from alpaca.research import contract as contract_mod, isolation
    root = os.path.realpath(root)
    if control is not None and control not in ("noop", "random"):
        raise ValueError("control is noop, random or None, not %r" % (control,))
    # a control run records the real runtime too: its proposer, implementer and notes roles run the
    # control script (lifecycle.CONTROL_ROLES), but its audit and its two verifiers still run on
    # the run's runtime, so a control run tests the same trust path as a real run (architecture.md
    # section 6.4, FR-044)
    runtime_name, mode = _check_session_runtime(runtime, root)
    if model is not None:
        # an alias the shield policy does not map is refused before anything is recorded
        from alpaca.research import session_runtime
        model = session_runtime.check_model_override(root, model, runtime_name)
    if mode == "session":
        # the refusals every start of a session run shares, before anything is recorded: the
        # level rule, the driver's transcript, the shield map, the isolated
        # interpreter and the rate cards (session_runtime.drive_checks)
        from alpaca.research import session_runtime
        probe = db.connect(root)
        try:
            session_runtime.drive_checks(probe, root, session, level=int(str(level).lstrip("L"))
                                         if str(level).lstrip("L").isdigit() else None,
                                         runtime=runtime_name, model=model)
        finally:
            probe.close()
    binary = None
    jail_version = _check_jail()
    conn = db.connect(root)
    try:
        run = _allocate(root, conn)
        frozen, fields = contract_mod.freeze(conn, root, run, runbook=runbook, task=task,
                                             session=session)
        _hash_checks(conn, fields["contract_sha256"], runbook, control)
        if control is None:
            from alpaca.research import controls
            if not _stub(controls.burn_in):
                # {"state": "needed" | "done" | "failed", ...} (architecture.md section 3.9)
                got = controls.burn_in(conn, fields["contract_sha256"]) or {}
                st = got.get("state")
                if st != "done":
                    code = "RESEARCH-CONTROL-FAILED" if st == "failed" else \
                        "RESEARCH-CONTROL-REQUIRED"
                    raise errors.refusal(code, "the no-op burn-in under contract %s is %s%s"
                                         % (fields["contract_sha256"][:12], st or "not run",
                                            ": %s" % got["reason"] if got.get("reason") else ""))
        _store_tracked(root, frozen)
        _check_disk(root, frozen.disk_gib)
        # no start check on the project budget posture: the project allowance in project.yaml
        # was not set for research runs and never holds one (t-297, in-session-runtime.md
        # section 21); the run's own caps, when its contract sets them, are checked per launch
        need = _plan_need(frozen, control)
        have = _task_count(conn, op) if op else 0
        if have + need > taskplan.MAX_TASKS:
            raise errors.refusal("RUN-PLAN-CAP", "the run needs %d task(s) and the op holds %d, "
                                 "over the %d-task plan" % (need, have, taskplan.MAX_TASKS))
        stamp = paths.stamp()
        try:
            clone = isolation.build_clone(root, run, frozen, stamp=stamp)
        except errors.IsolationError as exc:
            raise errors.refusal("RUN-CLONE-FAILED", "%s: %s" % (exc.reason, exc.detail))
        session_id = events.runner_session(run)
        op_id = op
        if op_id is None:
            op_id = ops.open_op(conn, "research run %s: %s on %s" % (run, frozen.mode, runbook),
                                "%s finalized with its report task done" % run, phases=None,
                                session=session_id, actor=ACTOR)
        taskplan.group(conn, op_id, run, "Research run %s" % run, "research",
                       session=session_id, actor=ACTOR)
        k = int(frozen.slots)
        repair_n = len(tuple(frozen.repair)) if frozen.mode == "repair" else 0
        budget_n = 3 * k + 2 * repair_n
        if op is not None:
            budget_n += int(dispatch.declared_budget(conn, op_id) or dispatch.spawn_count(conn, op_id))
        dispatch.declare_budget(conn, op_id, budget_n, session=session_id)
        mapping = {"group": run, "after": [], "resources": []}
        data = {"mode": frozen.mode, "runbook": runbook, "runbook_sha256": fields["runbook_sha256"],
                "contract_sha256": fields["contract_sha256"],
                "files_sha256": fields["files_sha256"], "intent": fields["intent"],
                "pack_sha256": fields["pack_sha256"], "override_diff": fields["override_diff"],
                "k": k, "n": int(frozen.rounds),
                "metrics": [{"id": m.id, "unit": m.unit, "direction": m.direction,
                             "kind": m.kind, "repeats": m.repeats, "min_ok": m.min_ok,
                             "margin": m.margin} for m in frozen.metrics],
                "targets": [{"metric": t.metric, "value": t.value} for t in frozen.targets],
                "accept": {"rule": frozen.accept_rule, "after_targets": frozen.after_targets},
                "budget": {"usd": frozen.budget_usd, "tokens": frozen.budget_tokens,
                           "wall": frozen.budget_wall_s},
                "control": control, "via": via, "level": level, "session": session,
                "runtime": runtime_name, "runtime_mode": mode,
                "witness": "hook" if runtime_name == "claude" else "transcript",
                "driver": session if mode == "session" else None,
                "runtime_version": None, "binary": binary,
                "model": model, "jail": jail_version,
                "research_branch": paths.research_branch(run),
                "base_commit": clone.base_commit, "repo_commit": fields["base_commit"],
                "repo": {"path": frozen.repo_path, "ref": frozen.repo_ref},
                "clone_stamp": stamp, "alpaca_version": version.string(),
                "block": fields["block"], "stamps": fields["stamps"], "group": run}
        with db.transaction(conn):
            _hash_checks(conn, fields["contract_sha256"], runbook, control)
            report_task = ops.add_task(
                conn, op_id, "Research run %s: the run report, sealed from the run's events and "
                "verified" % run, "%s report" % run,
                phase=batch.ladder_phase(conn, op_id, "verify"),
                why="the run's report is one task (DD-03)", session=session_id, actor=ACTOR,
                extra={"research": {"run": run, "role": "report", "attempt": None,
                                    "key": "%s:report" % run, "mode": frozen.mode,
                                    "repair_task": None}}, mapping=mapping)
            repair_tasks = []
            for item in (frozen.repair if frozen.mode == "repair" else ()):
                # a repair task's research.repair_task names the task itself (section 8.2): the id
                # add_task allocates, which this BEGIN IMMEDIATE transaction keeps stable
                own_id = "t-%03d" % (conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] + 1)
                tid = ops.add_task(
                    conn, op_id, "Research run %s: repair case %d, reproducer first, then fix "
                    "attempts judged by the repair rule" % (run, item.index),
                    ("%s repair %d %s" % (run, item.index, item.case_id))[:60],
                    phase=batch.ladder_phase(conn, op_id, "build"),
                    why="one repair task per case (FR-005)", session=session_id, actor=ACTOR,
                    extra={"research": {"run": run, "role": "repair", "attempt": None,
                                        "key": "%s:repair:%d" % (run, item.index),
                                        "mode": "repair", "case": item.case_id,
                                        "index": item.index, "repair_task": own_id}},
                    mapping=mapping)
                if tid != own_id:
                    raise RuntimeError("add_task allocated %s, not the %s the repair task names"
                                       % (tid, own_id))
                repair_tasks.append(tid)
            data.update(report_task=report_task, repair_tasks=repair_tasks)
            ev = events.emit(conn, events.RUN_START, run=run, ref=run, op=op_id,
                             session=session_id, data=data, in_txn=True)
        from alpaca import taskcontract
        from alpaca.research import verify_gate
        taskcontract.record(conn, report_task, {
            "input": ["the run id %s" % run, "the frozen contract %s" % fields["contract_sha256"]],
            "expected": ["report.md, report.json and events.json under "
                         ".alpaca/reports/research/%s/, and the research-report event" % run],
            "done_bar": ["report.md and report.json rebuilt from events.json by the replay block "
                         "have the sealed sha256",
                         "the report names the run's final state and its finalize event",
                         "every hold and grant of the run is in the report with its decision id",
                         verify_gate.SELF_LINES[1]],
            "fail_cases": ["a report that states a value the run's events do not hold"]},
            session=session_id, actor=ACTOR)
        if mode == "session":
            # the pin of the engine, the K13 baseline, the first ledger and the guard's live marker
            from alpaca.research import session_runtime
            lv = int(str(level).lstrip("L") or 0) if str(level).lstrip("L").isdigit() else None
            session_runtime.register_driver(conn, root, run, session, cause="start", level=lv,
                                            runtime=runtime_name, run=run, op=op_id)
        return dict(json.loads(ev["data"]), run=run, op=op_id, event=ev["id"])
    finally:
        conn.close()


# ======================================================================== launch
def launch(root: str, run: str, *, foreground: bool, session: str) -> dict:
    """Start (or restart) the runner process; returns {"pid", "host", "log"}. Foreground runs
    main() in this process and returns after it exits, with "code"."""
    from alpaca import child as child_launch
    from alpaca.research import procs
    root = os.path.realpath(root)
    log = paths.runner_log(root, run)
    os.makedirs(os.path.dirname(log), exist_ok=True)
    if foreground:
        code = main(["--run", run, "--root", root, "--foreground"])
        return {"pid": os.getpid(), "host": socket.gethostname(), "log": log, "code": code}
    argv = [sys.executable]
    if _runtime_mode(root, run) == "session":
        argv.append("-I")       # isolated: no folder an agent can write is first on the path
        argv.append(child_launch.boot_path())  # this package, by its location (alpaca/boot.py, t-428)
    argv += ["-m", "alpaca.research.runner", "--run", run, "--root", root]
    with open(log, "ab") as out:
        child = subprocess.Popen(argv, cwd=root, env=procs.runner_environment(),
                                 stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                 start_new_session=True, close_fds=True)
    return {"pid": child.pid, "host": socket.gethostname(), "log": log}


# ======================================================================== liveness
LOCK_RETRY_S = 5.0                   # operator-surface section 8.1


def is_alive(root: str, run: str) -> bool:
    """True when a process on this host holds the run's runner lock: open runner.lock, try
    fcntl.flock(LOCK_SH | LOCK_NB), drop it at once; a refused take means alive, a missing file
    or a granted take means not running (section 4.2, operator-surface section 4.3.2). True for
    a --foreground runner, whose process holds the lock like the detached one."""
    path = paths.runner_lock(root, run)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                return True
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _read_pid(root: str, run: str) -> dict | None:
    try:
        with open(paths.runner_pid(root, run), encoding="utf-8") as fh:
            got = json.load(fh)
        return got if isinstance(got, dict) else None
    except (OSError, ValueError):
        return None


def request_stop(root: str, run: str) -> bool:
    """SIGTERM to the runner.pid process when is_alive, the pid file names this host and the
    pid's start time in /proc/<pid>/stat equals the recorded one; False otherwise."""
    if not is_alive(root, run):
        return False
    rec = _read_pid(root, run)
    if not rec or rec.get("host") != socket.gethostname():
        return False
    try:
        pid = int(rec.get("pid"))
    except (TypeError, ValueError):
        return False
    if rec.get("start_ticks") is None or _start_ticks(pid) != rec.get("start_ticks"):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


# ======================================================================== the runner process
class _Runner:
    """One runner process for one run: the lock, the loop, finalize."""

    def __init__(self, root: str, run: str, *, foreground: bool):
        self.root = os.path.realpath(root)
        self.run = paths.check_name(run)
        self.foreground = foreground
        self.session = events.runner_session(run)
        self.stop = threading.Event()
        self.lock = None
        self.conn = None
        self.ctx = None
        self.pool = None
        self.finalized = False
        self.runner_event = None
        self.env_snapshot = None
        self.contract = None
        self.start = None
        self.heart = None
        self.reaper = None
        self.old_handlers = {}
        self.old_umask = None
        self.watch = None
        self.watch_stop = threading.Event()
        self.snapshot_grace = {}           # jobs.snapshot_wait's state (t-360, section 24.2)
        self.exit = vc.FAIL

    # ---------------------------------------------------------------- the lock
    def take_lock(self) -> bool:
        """The exclusive runner lock, taken with worker_launch.ReentryLock (section 4.2), whose
        take is non-blocking: a refused take is retried every 0.25 s for LOCK_RETRY_S, since a
        status, resume or stop probe holds a shared lock for an instant."""
        from alpaca.resilience.worker_launch import LockHeldError, ReentryLock
        path = paths.runner_lock(self.root, self.run)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        deadline = time.monotonic() + LOCK_RETRY_S
        while True:
            lock = ReentryLock(path)
            try:
                self.lock = lock.acquire()
                return True
            except LockHeldError:
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(_LOCK_POLL_S)

    def drop_lock(self) -> None:
        if self.lock is not None:
            try:
                self.lock.release()
            finally:
                self.lock = None

    # ---------------------------------------------------------------- signals
    def _on_signal(self, signum, _frame):
        from alpaca.research import procs
        self.stop.set()
        _log(self.root, self.run, "signal %d: stopping" % signum)
        try:
            procs.stop_all()
        except Exception:  # noqa: BLE001 - a signal handler never raises
            pass
        if self.ctx is not None and self.ctx.barrier is not None:
            try:
                self.ctx.barrier.wake()
            except Exception:  # noqa: BLE001
                pass

    def install_signals(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            try:
                self.old_handlers[sig] = signal.signal(sig, self._on_signal)
            except (ValueError, OSError):
                pass

    def restore_signals(self) -> None:
        for sig, old in self.old_handlers.items():
            try:
                signal.signal(sig, old)
            except (ValueError, OSError, TypeError):
                pass

    # ---------------------------------------------------------------- start
    def write_pid(self) -> dict:
        pid = os.getpid()
        rec = {"pid": pid, "host": socket.gethostname(), "start_ticks": _start_ticks(pid),
               "foreground": self.foreground}
        path = paths.runner_pid(self.root, self.run)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, sort_keys=True)
        os.replace(tmp, path)
        return rec

    def snapshot_env(self, runtime_binary: str | None) -> str:
        """env/<stamp>.json: PATH, the runtime and tool binaries with their sha256, the bwrap
        version, the Alpaca version and the names (never values) of the environment variables."""
        from alpaca import version
        from alpaca.research import jail
        stamp = paths.stamp()
        folder = paths.env_dir(self.root, self.run)
        os.makedirs(folder, exist_ok=True)

        def sha(path):
            if not path or not os.path.isfile(path):
                return None
            import hashlib
            with open(path, "rb") as fh:
                return hashlib.sha256(fh.read()).hexdigest()
        tools = {}
        for p in tuple(getattr(self.contract, "tools_real", ()) or ()):
            tools[p] = sha(p)
        body = {"stamp": stamp, "path": os.environ.get("PATH", ""),
                "runtime": {"binary": runtime_binary, "sha256": sha(runtime_binary)},
                "tools": tools, "bwrap": jail.version(), "alpaca_version": version.string(),
                "python": sys.version.split()[0], "env_names": sorted(os.environ)}
        path = os.path.join(folder, "%s.json" % stamp)
        n = 1
        while os.path.exists(path):
            n += 1
            path = os.path.join(folder, "%s-%d.json" % (stamp, n))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(body, fh, indent=1, sort_keys=True)
        return path

    def record_runner(self, cause: str) -> dict:
        from alpaca import version
        pid = os.getpid()
        return events.emit(self.conn, events.RUNNER, run=self.run, ref=self.run,
                           op=self.start.get("op"), session=self.session,
                           data={"pid": pid, "host": socket.gethostname(),
                                 "start_time": _start_ticks(pid), "cause": cause,
                                 "foreground": self.foreground,
                                 "log": os.path.relpath(paths.runner_log(self.root, self.run),
                                                        self.root),
                                 "alpaca_version": version.string()})

    # ---------------------------------------------------------------- resume helpers
    def kill_survivors(self) -> list:
        from alpaca.research import procs
        folder = paths.pids_dir(self.root, self.run)
        killed = []
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            return killed
        for name in names:
            path = os.path.join(folder, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    rec = json.load(fh)
                pid = int(rec.get("pid"))
            except (OSError, ValueError, TypeError):
                rec, pid = None, None
            if pid and pid != os.getpid() and rec.get("start_ticks") is not None \
                    and _start_ticks(pid) == rec.get("start_ticks"):
                killed.extend(procs.kill_tree(pid, tag=rec.get("tag")))
            try:
                os.remove(path)
            except OSError:
                pass
        self.remove_egress()
        return killed

    def remove_egress(self) -> None:
        """Remove the egress socket folders of the run's launches that have no end (section 10.3,
        step 4): their proxies died with the killed runner."""
        from alpaca.research import spend
        open_keys = set(spend.in_flight(self.conn, self.run))
        for ev in _events(self.conn, events.LAUNCH):
            d = ev["data"]
            if d.get("run") != self.run or d.get("key") not in open_keys or not d.get("launch"):
                continue
            try:
                folder = paths.egress_dir(self.root, self.run, d["launch"])
            except Exception:  # noqa: BLE001 - a folder that cannot be named is left alone
                continue
            shutil.rmtree(folder, ignore_errors=True)

    def sweep(self) -> None:
        from alpaca.research import isolation

        def sealed(root, run, task):
            row = self.conn.execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()
            return bool(row and row["status"] in ("done", "blocked"))
        try:
            isolation.sweep(self.root, self.run, sealed=sealed, dry_run=False,
                            alive=lambda root, run: False)
        except Exception as exc:  # noqa: BLE001 - a failed sweep leaves evidence, never the run
            _log(self.root, self.run, "sweep failed: %s" % exc)

    def ensure_clone(self):
        """Section 10.3, step 6: the clone of the improve line must exist with its research
        branch at the newest research-merge commit (or the base). A missing or differing clone
        is rebuilt from the base and the recorded diffs of the merges, each checked against its
        recorded head, and recorded as research-infra class lost (FR-042); a rebuild that does
        not reproduce the recorded heads fails the run."""
        from alpaca.research import isolation, lifecycle
        base = self.start.get("base_commit")
        merges = [ev for ev in _events(self.conn, events.MERGE, self.run)
                  if not ev["data"].get("repair_task")]
        want = merges[-1]["data"].get("commit") if merges else base
        clone, why = None, None
        try:
            clone = isolation.open_clone(self.root, self.run)
        except errors.IsolationError as exc:
            why = "%s: %s" % (exc.reason, exc.detail)
        if clone is not None and clone.head == want and clone.base_commit == base:
            return clone
        why = why or ("the clone is missing" if clone is None else
                      "the research branch is at %s, not %s" % (clone.head[:12], (want or "")[:12]))
        dest = paths.clone_dir(self.root, self.run)
        if os.path.islink(dest):
            os.unlink(dest)                 # a link is removed, never followed
        elif os.path.lexists(dest):
            shutil.rmtree(dest, ignore_errors=True)
        clone = isolation.build_clone(self.root, self.run, self.contract,
                                      stamp=self.start.get("clone_stamp") or paths.stamp())
        if clone.base_commit != base:
            raise errors.refusal("RUN-CLONE-FAILED", "the rebuilt clone's base %s is not the "
                                 "recorded %s" % (clone.base_commit[:12], (base or "")[:12]))
        parent = base
        for ev in merges:
            d = ev["data"]
            att = lifecycle._Attempt(self.ctx_stub(clone), d.get("attempt"))
            diffs = att.diffs(self.conn)
            if len(diffs) != 1:
                raise errors.refusal("RUN-CLONE-FAILED", "merge %d has no one diff" % ev["id"])
            cap = att.capture_of(diffs[0])
            head = isolation.merge_champion(clone, branch=clone.branch, parent=parent,
                                            capture=cap, message=cap.message, stamp=cap.stamp)
            if head != d.get("commit"):
                raise errors.refusal("RUN-CLONE-FAILED", "the rebuilt merge %d gives %s, not %s"
                                     % (ev["id"], head[:12], str(d.get("commit"))[:12]))
            parent = head
        events.emit(self.conn, events.INFRA, run=self.run, ref=self.run, op=self.start.get("op"),
                    session=self.session, code=vc.BLOCKED,
                    data={"attempt": None, "role": None, "try": None, "class": "lost",
                          "detail": "the clone was rebuilt from its base and %d recorded "
                                    "merge(s): %s" % (len(merges), why), "wait_until": None})
        return isolation.open_clone(self.root, self.run)

    def session_mode(self) -> bool:
        return bool(self.start) and self.start.get("runtime_mode") == "session"

    def ctx_stub(self, clone):
        ctx = RunContext(root=self.root, run=self.run, op=self.start.get("op"),
                         session=self.session, contract=self.contract,
                         runtime=self.start.get("runtime"), clone=clone, stop=self.stop,
                         tmp_prefixes=_tmp_prefixes())
        ctx.pinned = self.session_mode()      # a session run's plans check the engine pin
        return ctx

    # ---------------------------------------------------------------- the driver and engine watch
    def marker_code(self) -> str | None:
        """RUN-DRIVER-GONE or RUN-ENGINE-CHANGED when a marker file of the run says so (a marker
        written against an older pin or an older driver is stale), else None."""
        if not self.session_mode() or self.conn is None:
            return None
        from alpaca.research import jobs, session_engine as eng
        drv = jobs.newest_driver(self.conn, self.run)
        gone = eng.read_json(os.path.join(paths.run_dir(self.root, self.run), "driver-gone.json"))
        if isinstance(gone, dict) and drv is not None and gone.get("driver_event") == drv["id"]:
            return "RUN-DRIVER-GONE"
        pin = jobs.newest_pin(self.conn, self.run, self.run)
        if jobs.read_marker(self.root, self.run, pin["id"] if pin else None) is not None:
            return "RUN-ENGINE-CHANGED"
        return None

    def start_watch(self) -> None:
        """The driver watch (in-session-runtime.md section 10): every WATCH_S seconds, is the
        driver gone, did a pinned file change, did a file the runtime loads into an agent change.
        Any of them expires every open job, sets the stop flag and ends the run `interrupted`
        (resumable). It keeps running through finalize, the report task's verification
        included."""
        from alpaca.research import jobs, session_runtime, session_engine as eng

        def look():
            while not self.watch_stop.wait(session_runtime.WATCH_S):
                try:
                    own = db.connect(self.root)
                except Exception:  # noqa: BLE001
                    continue
                try:
                    drv = jobs.newest_driver(own, self.run)
                    if drv is None:
                        continue
                    if session_runtime.driver_gone(own, self.root, self.run):
                        eng._write_json(os.path.join(paths.run_dir(self.root, self.run),
                                                     "driver-gone.json"),
                                        {"driver_event": drv["id"], "session": drv["data"].get("session"),
                                         "at": jobs._iso()})
                        jobs.expire_priced(own, self.root, self.run, "driver-gone", session=self.session)
                        self._halt()
                        continue
                    pin = jobs.newest_pin(own, self.run, self.run)
                    try:
                        jobs.check_pin(own, self.root, self.run, run=self.run,
                                       driver_session=drv["data"].get("session"), point="watch")
                    except jobs.Problem:
                        jobs.expire_priced(own, self.root, self.run, "engine", session=self.session)
                        self._halt()
                        continue
                    lev = jobs.current_ledger_event(own, self.run, self.run)
                    ledger = eng.load_ledger((lev["data"] or {}).get("context")) if lev else None
                    if ledger is not None:
                        _cur, diff = jobs.context_now(own, self.root, self.run, drv, ledger,
                                                      check_session=drv["data"].get("session"),
                                                      point="watch")
                        if diff:
                            classes = jobs.classify_changes(self.root, own, self.run, drv, diff, lev)
                            bad = [c for c in diff
                                   if classes.get(os.path.normpath(c["path"])) != "driver"]
                            if bad and jobs.snapshot_wait(bad, drv, self.snapshot_grace):
                                continue
                            if bad:
                                jobs.write_marker(self.root, self.run, kind="context", changed=diff,
                                                  pin_event=pin["id"] if pin else None,
                                                  ledger_event=lev["id"], point="watch")
                                jobs.expire_priced(own, self.root, self.run, "context",
                                                 session=self.session)
                                self._halt()
                except Exception as exc:  # noqa: BLE001 - a failed look is tried again
                    _log(self.root, self.run, "watch: %s: %s" % (type(exc).__name__, exc))
                finally:
                    own.close()
        self.watch = threading.Thread(target=look, name="watch-%s" % self.run, daemon=True)
        self.watch.start()

    def _halt(self) -> None:
        """Set the stop flag and wake the barrier, as a signal does, so the plans return PAUSED."""
        self.stop.set()
        if self.ctx is not None and self.ctx.barrier is not None:
            try:
                self.ctx.barrier.wake()
            except Exception:  # noqa: BLE001
                pass

    def take_leases(self) -> None:
        """Section 10.3, step 5: sweep lapsed leases, then take the report task, the repair task
        in progress (the first in index order not done or blocked, once its plan claimed it) and
        every task of the current round that is not done or blocked and was claimed. The sealed
        tasks of earlier rounds are taken by queue_earlier as it hands them to the pool."""
        from alpaca import ops
        ops.expire_leases(self.conn)
        report = self.start.get("report_task")
        if report and self.status(report) not in ("done", "blocked"):
            leases.take(self.conn, report, self.run, _LEASE_MINUTES, session=self.session,
                        reason="research run report")
        from alpaca.research import lifecycle
        for task in self.start.get("repair_tasks") or []:
            if self.status(task) in ("done", "blocked"):
                continue
            if lifecycle._events(self.conn, "claim", task):
                leases.take(self.conn, task, self.run, _LEASE_MINUTES, session=self.session,
                            reason="research repair task (resume)")
            break
        current = self.next_round()
        for opened in _events(self.conn, events.ROUND_OPEN, self.run):
            d = opened["data"]
            if d.get("round") != current or d.get("repair_task"):
                continue
            for task in d.get("tasks") or []:
                if self.status(task) not in ("done", "blocked") and \
                        lifecycle._events(self.conn, "claim", task):
                    leases.take(self.conn, task, self.run, _LEASE_MINUTES, session=self.session,
                                reason="research attempt (resume)")

    def status(self, task: str) -> str | None:
        row = self.conn.execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()
        return row["status"] if row else None

    def heartbeat(self) -> None:
        """Renew every lease the runner holds on a task still in doing, every 20 minutes."""
        def beat():
            while not self.stop.wait(_HEARTBEAT_S):
                try:
                    own = db.connect(self.root)
                except Exception:  # noqa: BLE001
                    continue
                try:
                    rows = own.execute("SELECT id FROM tasks WHERE status='doing' AND claimant=?",
                                       (self.run,)).fetchall()
                    for row in rows:
                        try:
                            leases.renew(own, row["id"], self.run, minutes=_LEASE_MINUTES,
                                         session=self.session)
                        except leases.StaleLease:
                            continue
                finally:
                    own.close()
        self.heart = threading.Thread(target=beat, name="heartbeat-%s" % self.run, daemon=True)
        self.heart.start()

    def reap_pass(self) -> list:
        """One pass of procs.reap_orphans while this process is a subreaper: the orphans it
        adopted are zombies nothing waits for. A child a Popen owns is never taken. The count is
        written to the runner log when it is not zero."""
        if _subreaper_flag() != 1:
            return []
        from alpaca.research import procs
        try:
            reaped = procs.reap_orphans()
        except Exception as exc:  # noqa: BLE001
            _log(self.root, self.run, "orphan reaper failed: %s" % exc)
            return []
        if reaped:
            _log(self.root, self.run, "reaped %d orphaned process(es): %s"
                 % (len(reaped), " ".join(str(p) for p in reaped[:20])))
        return reaped

    def start_reaper(self) -> None:
        """A daemon thread that runs reap_pass every _REAP_S seconds until the runner stops."""
        def loop():
            while not self.stop.wait(_REAP_S):
                self.reap_pass()
        self.reaper = threading.Thread(target=loop, name="reaper-%s" % self.run, daemon=True)
        self.reaper.start()

    def release_leases(self) -> None:
        rows = self.conn.execute("SELECT id FROM tasks WHERE status='doing' AND claimant=?",
                                 (self.run,)).fetchall()
        for row in rows:
            try:
                leases.release(self.conn, row["id"], self.run, session=self.session)
            except Exception:  # noqa: BLE001 - a lease that already passed needs no release
                pass

    # ---------------------------------------------------------------- the loop
    def stop_requested(self) -> bool:
        if self.stop.is_set():
            return True
        since = self.runner_event["id"] if self.runner_event else 0
        return any(ev["id"] > since for ev in _events(self.conn, events.STOP, self.run))

    def open_hold_gates(self) -> set:
        from alpaca.research import holds
        return {h["gate"] for h in holds.open_holds(self.conn, self.run)}

    def bound(self) -> int:
        """N, or the newest owner extension (rounds_bound below)."""
        return rounds_bound(self.conn, self.run, self.contract)

    def next_round(self) -> int:
        done = {ev["data"].get("round") for ev in _events(self.conn, events.ROUND, self.run)
                if not ev["data"].get("repair_task")}
        r = 0
        while r in done:
            r += 1
        return r

    def targets_met(self) -> bool:
        rounds = [ev for ev in _events(self.conn, events.ROUND, self.run)
                  if not ev["data"].get("repair_task")]
        return bool(rounds) and bool(rounds[-1]["data"].get("targets_met"))

    def held_state(self) -> tuple:
        """The finalize state a PAUSED plan ends in: the open hold's, or the stop's."""
        gates = self.open_hold_gates()
        if "extension" in gates:
            return "halted", None           # the extension hold carries RUN-ROUNDS-REACHED
        if "budget" in gates:
            return "paused", "RUN-BUDGET-HOLD"
        marked = self.marker_code()
        if marked:
            return "interrupted", marked
        if self.stop_requested():
            return self.stop_state(), None
        return "interrupted", None

    def stop_state(self) -> str:
        since = self.runner_event["id"] if self.runner_event else 0
        if any(ev["id"] > since for ev in _events(self.conn, events.STOP, self.run)):
            return "stopped"
        return "interrupted"

    def baseline_failed(self):
        """(state, reason, code) when the round 0 baseline's verdict is not `baseline` (section
        7.4), else None."""
        from alpaca.research import lifecycle, state as state_mod
        base_task = lifecycle.run_keys(self.conn, self.run).get(paths.attempt_key(self.run, 0, 0))
        if base_task is None:
            return None
        got = state_mod.attempt_verdict(self.conn, base_task)
        if got["verdict"] == "baseline":
            return None
        return ("failed", "the baseline %s ended %s (%s)" % (base_task, got["verdict"],
                                                              got["reason"]), "RUN-BASELINE-FAILED")

    def queue_earlier(self) -> None:
        """Section 10.3, step 8: the verifier pool takes every sealed task of earlier rounds
        whose close plan is not done."""
        from alpaca.research import lifecycle
        current = self.next_round()
        for key, task in lifecycle.run_keys(self.conn, self.run).items():
            r = lifecycle._research(self.conn, task)
            if r.get("role") != "attempt" or int(r.get("round") or 0) >= current:
                continue
            if self.status(task) in ("done", "blocked"):
                continue
            if self.conn.execute("SELECT 1 FROM events WHERE kind='proof-report' AND ref=?",
                                 (task,)).fetchone() is None:
                continue
            leases.take(self.conn, task, self.run, _LEASE_MINUTES, session=self.session)
            self.pool.put(task)

    def loop(self) -> tuple:
        """The loop of section 4.3; returns (state, reason, code)."""
        from alpaca.formation import controller
        from alpaca.research import holds, lifecycle, state as state_mod
        from alpaca.posture import loops
        ctx = self.ctx
        if self.contract.mode == "repair":
            from alpaca.research import repair
            for task in self.start.get("repair_tasks") or []:
                if self.status(task) in ("done", "blocked"):
                    continue
                if self.stop_requested():
                    return self.stop_state(), "a stop was requested", None
                if "budget" in self.open_hold_gates():
                    return "paused", "a budget hold is open", "RUN-BUDGET-HOLD"
                code = controller.run(self.conn, repair.repair_plan(ctx, task), root=self.root,
                                      session=self.session, tmp_prefixes=ctx.tmp_prefixes)
                if code == vc.PAUSED:
                    st, why = self.held_state()
                    return st, "the repair task %s paused" % task, why
                if code != vc.PASS:
                    return "failed", "a step of repair task %s could not run" % task, None
                self.pool.put(task)
            return "finished", "every repair task ran", None
        control = self.start.get("control")
        r = self.next_round()
        if r > 0:
            if self.stop_requested():
                return self.stop_state(), "a stop was requested", None
            failed = self.baseline_failed()
            if failed:
                return failed
        while True:
            if self.stop_requested():
                return self.stop_state(), "a stop was requested", None
            if "budget" in self.open_hold_gates():
                return "paused", "a budget hold is open", "RUN-BUDGET-HOLD"
            if r == 0:
                code = controller.run(self.conn, lifecycle.round_plan(ctx, 0), root=self.root,
                                      session=self.session, tmp_prefixes=ctx.tmp_prefixes)
                if code == vc.PAUSED:
                    st, why = self.held_state()
                    return st, "round 0 paused", why
                if code != vc.PASS:
                    return "failed", "a step of round 0 could not run", None
                if self.stop_requested():
                    # an interrupt is never read as a failed baseline (EC-016)
                    return self.stop_state(), "a stop was requested", None
                failed = self.baseline_failed()
                if failed:
                    self.pool.drain()
                    return failed
            elif control and r > int(self.contract.noop_rounds):
                return "finished", "the control run ran its %d rounds" % self.contract.noop_rounds, None
            elif self.targets_met() and (self.contract.after_targets or "stop") == "stop":
                return "finished", "the targets are met", None
            elif r > self.bound() and self.targets_met():
                return "finished", "the targets are met after %d rounds" % (r - 1), None
            elif r > self.bound():
                n = r - 1
                holds.open_hold(self.conn, self.root, ref=self.run, gate="extension",
                                code="RUN-ROUNDS-REACHED",
                                detail="%d round(s) of %d ran and the targets are not met; the "
                                       "run asks the owner for an extension" % (n, self.bound()),
                                op=self.start.get("op"),
                                bound=loops.count(self.conn, self.run, "research-extension"),
                                session=self.session)
                # the finalize event carries no code for halted (section 8.3); the extension hold
                # carries RUN-ROUNDS-REACHED
                return "halted", "round %d of %d done; targets not met; owner extension owed" % (
                    n, self.bound()), None
            elif "extension" in self.open_hold_gates():
                # an extension hold still open means the owner has not answered it: no round runs
                # past it, whatever extension an older hold's answer granted
                return "halted", "an extension hold is open before round %d" % r, None
            else:
                code = controller.run(self.conn, lifecycle.round_plan(ctx, r), root=self.root,
                                      session=self.session, tmp_prefixes=ctx.tmp_prefixes)
                if code == vc.PAUSED:
                    st, why = self.held_state()
                    return st, "round %d paused" % r, why
                if code != vc.PASS:
                    return "failed", "a step of round %d could not run" % r, None
            r += 1

    # ---------------------------------------------------------------- finalize
    def ship_rule(self, summary: dict) -> bool:
        if self.start.get("control"):
            return False
        if self.contract is not None and self.contract.mode == "repair":
            return any(item.get("resolved") for item in summary.get("repair") or [])
        champ = (summary.get("champion") or {}).get("commit")
        base = (summary.get("baseline") or {}).get("commit") or self.start.get("base_commit")
        return bool(champ) and champ != base

    def stop_cleanup(self) -> None:
        """Row 57 of section 7.2: an attempt that launched an agent and has no research-attempt
        records one with outcome abandoned and reason stopped; it, and every planned task never
        claimed, moves to blocked through proofs.close(to="blocked"). A repair run's repair tasks
        are planned tasks of the run too (section 4.8): one not done or blocked moves to blocked
        the same way, since a stopped run is not resumed. The report task is close_report's."""
        from alpaca.research import lifecycle, proofs
        reason = "research run %s stopped" % self.run
        for key, task in lifecycle.run_keys(self.conn, self.run).items():
            r = lifecycle._research(self.conn, task)
            role = r.get("role")
            if role not in ("attempt", "repair") or self.status(task) in ("done", "blocked"):
                continue
            if role == "repair":
                proofs.close(self.conn, self.root, task, session=self.session, to="blocked",
                             reason=reason)
                continue
            if _events(self.conn, events.ATTEMPT, task):
                continue
            if _events(self.conn, events.LAUNCH, task):
                # the same data the `record` step writes (gates, tamper, accept and audit
                # events, metrics, diff, totals), with outcome abandoned and reason stopped
                ctx = self.ctx or self.ctx_stub(None)
                code, data = lifecycle.attempt_record(self.conn, ctx, task, outcome="abandoned",
                                                      reason="stopped")
                events.emit(self.conn, events.ATTEMPT, run=self.run, ref=task,
                            op=self.start.get("op"), session=self.session, code=code, data=data)
            proofs.close(self.conn, self.root, task, session=self.session, to="blocked",
                         reason=reason)

    def forensics(self, stamp: str) -> str | None:
        folder = paths.forensics_dir(self.root, self.run, stamp)
        os.makedirs(folder, exist_ok=True)
        for src in (paths.runner_log(self.root, self.run), self.env_snapshot):
            if src and os.path.isfile(src):
                shutil.copy2(src, os.path.join(folder, os.path.basename(src)))
        return folder

    def finalize(self, state: str, reason: str, code: str | None) -> int:
        """Section 4.8, each part guarded so a failure in one does not skip the rest. Returns
        the runner's exit code (section 4.5)."""
        if self.finalized:
            return self.exit
        self.finalized = True
        from alpaca.research import state as state_mod
        _log(self.root, self.run, "finalize %s: %s" % (state, reason))
        sm = self.session_mode()
        if sm:
            # a run that does not drain (stopped, interrupted, driver gone) expires its open jobs
            # and refuses new issues; a draining finalize keeps them open, since the verifier jobs
            # of the last round still need the driver (in-session-runtime.md section 10)
            try:
                from alpaca.research import jobs, session_runtime
                if state in ("stopped", "interrupted"):
                    why = {"RUN-DRIVER-GONE": "driver-gone", "RUN-ENGINE-CHANGED": "engine"}.get(
                        code or self.marker_code(), "stopped")
                    session_runtime.set_closing(self.run, True)
                    own = db.connect(self.root)
                    try:
                        jobs.expire_priced(own, self.root, self.run, why, session=self.session)
                    finally:
                        own.close()
            except Exception as exc:  # noqa: BLE001
                _log(self.root, self.run, "expire: %s" % exc)
            if state == "interrupted" and not code:
                code = self.marker_code()
        # 1. the verifier pool: stop taking work and, unless stopping, drain it
        if self.pool is not None:
            try:
                self.pool.stop(drain=state not in ("stopped", "interrupted"))
            except Exception as exc:  # noqa: BLE001
                _log(self.root, self.run, "pool: %s" % exc)
            finally:
                from alpaca.research import lifecycle
                lifecycle.set_pool(self.run, None)
        if state == "stopped":
            try:
                self.stop_cleanup()
            except Exception as exc:  # noqa: BLE001
                _log(self.root, self.run, "stop cleanup: %s" % exc)
        # 2. holdout checks on the final champion (t-102)
        if state in ("finished", "halted") and self.contract is not None:
            try:
                from alpaca.research import holdout
                summary = state_mod.fold(self.conn, self.run)
                champ = (summary.get("champion") or {}).get("commit")
                if champ and champ != self.start.get("base_commit") and not _stub(holdout.run_holdout):
                    holdout.run_holdout(self.conn, self.root, self.run, champion=champ,
                                        contract=self.contract, session=self.session)
            except Exception as exc:  # noqa: BLE001
                _log(self.root, self.run, "holdout: %s" % exc)
        # 3. forensics on an early exit
        early = state in ("failed", "interrupted") or not any(
            (ev["data"].get("round") or 0) >= 1 for ev in _events(self.conn, events.ROUND, self.run))
        forensics = None
        if early:
            try:
                forensics = self.forensics(paths.stamp())
            except Exception as exc:  # noqa: BLE001
                _log(self.root, self.run, "forensics: %s" % exc)
        # 4. the finalize event, with the summary and the exit code
        try:
            summary = state_mod.fold(self.conn, self.run)
        except Exception as exc:  # noqa: BLE001
            summary = {"error": str(exc)}
        exit_code = {"finished": vc.PASS, "stopped": vc.PASS, "failed": vc.FAIL,
                     "interrupted": vc.BLOCKED, "halted": vc.PAUSED,
                     "paused": vc.PAUSED}.get(state, vc.FAIL)
        if state == "finished" and self.ship_rule(summary):
            exit_code = vc.PAUSED
        rel = lambda p: os.path.relpath(p, self.root) if p else None  # noqa: E731
        try:
            events.emit(self.conn, events.FINALIZE, run=self.run, ref=self.run,
                        op=self.start.get("op") if self.start else None, session=self.session,
                        code=exit_code, reason=code,
                        data={"state": state, "reason": reason, "summary": summary,
                              "log": rel(paths.runner_log(self.root, self.run)),
                              "env_snapshot": rel(self.env_snapshot),
                              "forensics": rel(forensics),
                              "control_failed": bool(summary.get("control_failed"))})
        except Exception as exc:  # noqa: BLE001
            _log(self.root, self.run, "finalize event: %s" % exc)
        if sm:
            # price the strays and late agents that stopped while the run drained, so the report
            # reads them (R5)
            try:
                from alpaca.research import jobs
                own = db.connect(self.root)
                try:
                    jobs.reconcile(own, self.root, self.run, session=self.session)
                finally:
                    own.close()
            except Exception as exc:  # noqa: BLE001
                _log(self.root, self.run, "reconcile: %s" % exc)
        # 5. the report (t-105), 6. the report task
        report_task = self.start.get("report_task") if self.start else None
        try:
            from alpaca.research import report
            if _stub(report.write):
                raise NotImplementedError("report.write is not built yet (t-105)")
            report.write(self.conn, self.root, self.run)
            self.close_report(state, report_task)
        except Exception as exc:  # noqa: BLE001
            _log(self.root, self.run, "report: %s" % exc)
        if sm:
            # the guard's live marker, the pin checks and the driver watch stayed in force through
            # close_report, whose verification of the report task issues two verifier jobs; now the
            # driver audit over the whole run, the last open jobs and the marker (section 10)
            try:
                from alpaca.research import jobs, session_runtime
                own = db.connect(self.root)
                try:
                    drv = jobs.newest_driver(own, self.run)
                    sid = (drv or {}).get("data", {}).get("session")
                    if sid:
                        session_runtime.driver_audit(own, self.root, sid, self.run)
                    jobs.expire_priced(own, self.root, self.run, "finalize", session=self.session)
                    # the strays that stopped since the last judge (a twin still running when its
                    # job was decided) are priced and booked now (R5)
                    jobs.reconcile(own, self.root, self.run, session=self.session)
                    if sid:
                        session_runtime.unmark_live(self.root, sid, self.run)
                finally:
                    own.close()
                session_runtime.set_closing(self.run, False)
            except Exception as exc:  # noqa: BLE001
                _log(self.root, self.run, "driver audit: %s" % exc)
            self.watch_stop.set()
        # 7. release the leases the runner still holds on tasks in doing
        try:
            self.release_leases()
        except Exception as exc:  # noqa: BLE001
            _log(self.root, self.run, "leases: %s" % exc)
        self.exit = exit_code
        return exit_code

    def close_report(self, state: str, report_task: str | None) -> None:
        from alpaca.research import lifecycle, proofs
        if not report_task:
            return
        if state == "finished":
            proofs.export_report_events(self.conn, self.root, self.run)
            proofs.seal(self.conn, self.root, report_task, session=self.session)
            lifecycle.verify_task(self.conn, self.ctx, report_task)
            from alpaca.research import verify_gate
            st = verify_gate.state(self.conn, self.root, report_task)
            if (st.get("outcome") or st.get("state")) == "verified":
                proofs.close(self.conn, self.root, report_task, session=self.session)
        elif state in ("stopped", "failed"):
            proofs.close(self.conn, self.root, report_task, session=self.session, to="blocked",
                         reason="research run %s %s" % (self.run, state))

    # ---------------------------------------------------------------- main
    def main(self) -> int:
        if not self.take_lock():
            sys.stdout.write("GATE alpaca-research-runner: FAIL (RESEARCH-RUN-ACTIVE: another "
                             "runner holds the lock of %s)\n" % self.run)
            return vc.FAIL
        self.old_umask = os.umask(0o022)
        self.exit = vc.FAIL
        try:
            return self._main()
        finally:
            self.stop.set()
            if self.heart is not None:
                self.heart.join(timeout=30)
            if getattr(self, "reaper", None) is not None:
                self.reaper.join(timeout=30)
            self.reap_pass()                  # the last pass, while the flag is still set
            self.restore_signals()
            if self.conn is not None:
                try:
                    self.conn.close()
                except Exception:  # noqa: BLE001
                    pass
            os.umask(self.old_umask)
            if getattr(self, "old_subreaper", None) == 0:
                # a --foreground runner gives its caller back the flag it had, so the caller's
                # later orphans go to init again and not to a process that never reaps them
                _set_subreaper(0)
            self.drop_lock()

    def _main(self) -> int:
        from alpaca.research import contract as contract_mod, lifecycle, procs
        self.conn = db.connect(self.root)
        self.start = (_run_start(self.conn, self.run) or {}).get("data")
        if not self.start:
            sys.stdout.write("GATE alpaca-research-runner: FAIL (RESEARCH-RUN-UNKNOWN: %s)\n"
                             % self.run)
            return vc.FAIL
        self.start = dict(self.start, op=_run_start(self.conn, self.run)["op"])
        self.write_pid()
        self.old_subreaper = _subreaper_flag()
        procs.become_subreaper()
        self.start_reaper()
        self.install_signals()
        cause = "resume" if _events(self.conn, events.RUNNER, self.run) else "start"
        self.runner_event = self.record_runner(cause)
        _log(self.root, self.run, "runner %s (pid %d)" % (cause, os.getpid()))
        state, reason, code = "failed", "the runner ended before its loop", None
        try:
            try:
                self.contract = contract_mod.load_frozen(self.conn, self.root, self.run)
            except errors.ResearchRefusal as exc:
                self.env_snapshot = self.snapshot_env(self.start.get("binary"))
                return self.finalize("failed", exc.detail or str(exc), exc.reason)
            self.env_snapshot = self.snapshot_env(self.start.get("binary"))
            try:
                _check_jail()
                if self.start.get("runtime") == "control":
                    _check_runtime("control", self.root)
                _check_disk(self.root, self.contract.disk_gib)
            except errors.ResearchRefusal as exc:
                return self.finalize("interrupted", exc.detail or str(exc), exc.reason)
            if self.session_mode():
                # the first thing a session run's runner does after the lock: the engine and context
                # checks against the pin of the newest pin-carrying event, then the preload of the
                # whole static closure of its deciding code (in-session-runtime.md section 12.5)
                from alpaca.research import jobs, session_runtime
                drv = jobs.newest_driver(self.conn, self.run)
                try:
                    session_runtime.engine_check(self.conn, self.root, self.run,
                                                 (drv or {}).get("data", {}).get("session") or "")
                except errors.ResearchRefusal as exc:
                    return self.finalize("interrupted", exc.detail or str(exc), exc.reason)
                session_runtime.preload(session_runtime.eng.install_root())
            # section 10.3: survivors (step 4) before leases (step 5)
            self.kill_survivors()
            if self.session_mode():
                # a new runner does not adopt the jobs of the old one: an agent of the old
                # session may still be writing in a worktree the resume is about to reclaim
                from alpaca.research import jobs
                jobs.expire_open(self.conn, self.root, self.run, "resume", session=self.session)
                try:
                    jobs.reconcile(self.conn, self.root, self.run, session=self.session)
                except Exception as exc:  # noqa: BLE001 - pricing a late agent never stops the start
                    _log(self.root, self.run, "reconcile: %s" % exc)
            self.take_leases()
            self.heartbeat()
            if self.session_mode():
                self.start_watch()
            self.sweep()
            clone = self.ensure_clone() if self.contract.mode == "improve" else None
            ctx = self.ctx_stub(clone)
            self.ctx = ctx
            self.pool = lifecycle.VerifierPool(ctx, int(self.contract.slots))
            lifecycle.set_pool(self.run, self.pool)
            self.queue_earlier()
            state, reason, code = self.loop()
        except errors.ResearchRefusal as exc:
            state, reason, code = "failed", exc.detail or str(exc), exc.reason
        except BaseException as exc:  # noqa: BLE001 - every exit records its finalize
            state, reason, code = "failed", "%s: %s" % (type(exc).__name__, exc), None
            if self.stop.is_set():
                state = self.stop_state()
            _log(self.root, self.run, "error: %s: %s" % (type(exc).__name__, exc))
        finally:
            got = self.finalize(state, reason, code)
        return got


#: the temp prefixes workspace_guard refuses for the runner's controller plans; None is the
#: guard's own list. Tests, whose projects live under the temp root, set it to ().
_TMP_PREFIXES = None


def _tmp_prefixes():
    return _TMP_PREFIXES


def main(argv: list[str] | None = None) -> int:
    """`python3 -m alpaca.research.runner --run <id> [--root <root>] [--foreground]`; exit 0, 1,
    2 or 3 (4.5). --foreground marks the research-runner event of a runner that launch(...,
    foreground=True) runs in its caller's process (section 4.2)."""
    from alpaca import paths as alpaca_paths
    parser = vc.make_parser(prog="alpaca.research.runner", name="alpaca-research-runner")
    parser.add_argument("--run", required=True)
    parser.add_argument("--root")
    parser.add_argument("--foreground", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else vc.USAGE
    try:
        paths.check_name(args.run)
        root = args.root or alpaca_paths.root()
    except Exception as exc:  # noqa: BLE001
        sys.stdout.write("GATE alpaca-research-runner: BLOCKED (%s)\n" % exc)
        return vc.BLOCKED
    return _Runner(root, args.run, foreground=bool(args.foreground)).main()


if __name__ == "__main__":
    raise SystemExit(main())
