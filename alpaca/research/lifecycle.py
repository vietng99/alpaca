"""The attempt plan, the close plan and the round plan of section 7, each a formation.controller
plan: the attempt lifecycle state machine.

Every step is one step of a formation.controller plan (alpaca/formation/controller.py:104-169):
the controller records one `controller-step` per step run, which is also how a restarted runner
skips the steps that already passed. A step returns PASS once its work is done, whatever the
attempt's verdict, so a rejected or broken attempt still reaches its audit, notes, record, proof
and close. A step whose precondition no longer holds (an earlier step set the verdict, or there
is no worktree or no launched agent) records no domain event and returns PASS. A step that is
not idempotent looks for its earlier result on the record first (architecture.md section 10.4),
so a killed run resumes without losing or duplicating an attempt.

Plans (architecture.md section 7.3):
  round plan, id <run>:r<N> (or <run>:repair:<i>:r<N>): open, champion, slots, rank, candidates,
    merge, summary, handoff;
  attempt plan, id <task>: claim, open-window, propose, check-proposer, isolate, implement,
    check-implementer, capture, purge, gates, judge, audit, notes, close-window, record,
    teardown, seal (round 0: claim, open-window, isolate, capture, purge, gates, judge,
    close-window, record, teardown, seal);
  close plan, id <task>:close: verify, close.

Agent launches go through session_runtime.launch (a job the driving session spawns a subagent for)
unless a test sets _hooks["launch"]; the verifiers' launches go through the same callable unless
_hooks["verify_launch"] is set.

Design: design/research-engine/architecture.md sections 3.8, 4.3, 7 and 10."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import queue
import threading
import time

from alpaca import claims as leases
from alpaca import db, util
from alpaca.gates import verdict as vc
from alpaca.research import events, paths
from alpaca.research.errors import IsolationError
from alpaca.research.types import RunContext

ACTOR = events.ACTOR
_LEASE_MINUTES = 60
_BARRIER_POLL_S = 2.0            # the record step polls the round barrier this often (4.3)
_JOIN_SLICE_S = 0.2              # the runner's main thread waits on its threads in slices this long
_DEFAULT_TIMEOUTS = {"proposer": 1200, "implementer": 1800, "notes": 240}   # t-088's defaults
_RECORD_INDEX = 14               # the `record` step's place in the attempt plan (0-based)

#: injection points for tests and control runs: "launch" stands in for session_runtime.launch,
#: "verify_launch" is the launch verifier.run gets, "audit_launch" the one audit.run gets, and
#: "sleep" is what a quota wait sleeps with. Every one of them is called through reserving(), so
#: the run's spend check and the launch's dollar share are taken in one step (spend.reserve).
_hooks = {"launch": None, "verify_launch": None, "audit_launch": None, "sleep": time.sleep}

#: run -> the runner's VerifierPool, which the round's `handoff` step feeds; kept here rather
#: than on the shared RunContext type (section 12.2), which does not declare it
_POOLS = {}
_QUOTA_BASE_S, _QUOTA_MAX_S = 300, 3600        # section 6.7: the doubling fallback of a quota wait

ATTEMPT_STEPS = ("claim", "open-window", "propose", "check-proposer", "isolate", "implement",
                 "check-implementer", "capture", "purge", "gates", "judge", "audit", "notes",
                 "close-window", "record", "teardown", "seal")
BASELINE_STEPS = ("claim", "open-window", "isolate", "capture", "purge", "gates", "judge",
                  "close-window", "record", "teardown", "seal")
ROUND_STEPS = ("open", "champion", "slots", "rank", "candidates", "merge", "summary", "handoff")
CLOSE_STEPS = ("verify", "close")


# ======================================================================== record reads
def _parse(row) -> dict:
    try:
        data = json.loads(row["data"] or "{}")
    except (TypeError, ValueError):
        data = {}
    return {"id": row["id"], "ts": row["ts"], "kind": row["kind"], "ref": row["ref"],
            "op": row["op"], "session": row["session"], "data": data}


#: the research-worktree actions that cut a tree (in-session-runtime.md section 28.1)
_CUT_ACTIONS = ("create", "reclaim", "rebuild")


def _cut_number(data: dict) -> int:
    """The cut of a research-worktree event: its `cut` field, else read from its path
    (paths.cut_of; an event written before t-357 has no field), else 1."""
    c = data.get("cut")
    if isinstance(c, int) and not isinstance(c, bool) and c >= 1:
        return c
    got = paths.cut_of(os.path.basename(str(data.get("path") or "").rstrip("/")))
    return got[1] if got else 1


def _events(conn, kind: str, ref: str | None = None) -> list:
    if ref is None:
        rows = conn.execute("SELECT * FROM events WHERE kind=? ORDER BY id", (kind,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM events WHERE kind=? AND ref=? ORDER BY id",
                            (kind, ref)).fetchall()
    return [_parse(r) for r in rows]


def _research(conn, task: str) -> dict:
    row = conn.execute("SELECT data FROM events WHERE kind='task-add' AND ref=? ORDER BY id LIMIT 1",
                       (task,)).fetchone()
    if row is None:
        return {}
    try:
        got = json.loads(row["data"] or "{}").get("research")
    except (TypeError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


#: Task statuses the runner treats as settled: it neither claims nor works such a task. op-057 t-616:
#: a deferred task is parked by the owner, so a resumed run leaves it alone. Not types.TERMINAL_STATES,
#: which stays done/blocked (a deferred task is not an end state of a run).
_SETTLED = ("done", "blocked", "deferred")


def _status(conn, task: str) -> str | None:
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()
    return row["status"] if row else None


def run_keys(conn, run: str) -> dict:
    """key -> task id of every task the run added (its task-add `research.key`)."""
    out = {}
    for row in conn.execute("SELECT ref, data FROM events WHERE kind='task-add' AND "
                            "json_extract(data, '$.research.run')=? ORDER BY id", (run,)).fetchall():
        try:
            key = json.loads(row["data"] or "{}")["research"].get("key")
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
        if key and key not in out:
            out[key] = row["ref"]
    return out


def _round_open(conn, run: str, round_n: int, line: str | None) -> dict | None:
    for ev in reversed(_events(conn, events.ROUND_OPEN, run)):
        d = ev["data"]
        if d.get("round") == round_n and (d.get("repair_task") or None) == (line or None):
            return ev
    return None


def _round_done(conn, run: str, round_n: int, line: str | None) -> dict | None:
    for ev in reversed(_events(conn, events.ROUND, run)):
        d = ev["data"]
        if d.get("round") == round_n and (d.get("repair_task") or None) == (line or None):
            return ev
    return None


def _rel(root: str, path: str | None) -> str | None:
    if not path:
        return path
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    except ValueError:
        return path
    return path if rel.startswith("..") else rel.replace(os.sep, "/")


def _abs(root: str, path: str | None) -> str | None:
    if not path:
        return path
    return path if os.path.isabs(path) else os.path.join(root, path)


def _sha_file(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def _emit(conn, ctx: RunContext, kind: str, ref: str, *, code=None, reason=None, data=None,
          in_txn=False) -> dict:
    return events.emit(conn, kind, run=ctx.run, ref=ref, op=ctx.op, session=ctx.session,
                       code=code, reason=reason, data=data, in_txn=in_txn)


def _stub(fn) -> bool:
    """True while fn is still the scaffold's stub (its task has not built it)."""
    return str(getattr(fn, "__doc__", "") or "").startswith("Stub:")


def _gate_path(ctx: RunContext) -> str:
    from alpaca.research import procs
    return procs.gate_path(tuple(ctx.contract.tools_real))


def _budget_hold(conn, ctx: RunContext, detail: str) -> dict:
    from alpaca.research import holds
    return holds.open_hold(conn, ctx.root, ref=ctx.run, gate="budget", code="RUN-BUDGET-HOLD",
                           detail=detail or "a launch was held by the budget check", op=ctx.op,
                           bound=None, session=ctx.session)


def _line(research: dict) -> str:
    return research.get("repair_task") or "champion"


def set_pool(run: str, pool) -> None:
    """Register (or with None drop) the run's verifier pool for the `handoff` step."""
    if pool is None:
        _POOLS.pop(run, None)
    else:
        _POOLS[run] = pool


#: the roles a control run's control script stands in for (section 6.4)
CONTROL_ROLES = ("proposer", "implementer", "notes")


def _frozen_mutate(ctx: RunContext) -> str | None:
    """The absolute path of the frozen copy of research.controls.mutate (a script in the runbook
    folder) under the run's contract/files/, which the launcher copies into a random control
    launch's bin/; None when the contract names none or the freeze holds no copy (the control
    agent then refuses to mutate)."""
    name = getattr(ctx.contract, "controls_mutate", None)
    if not name:
        return None
    from alpaca.research import gates
    try:
        folder = gates._runbook_folder(ctx.contract)
    except ValueError:
        return None
    base = os.path.join(paths.contract_dir(ctx.root, ctx.run), "files")
    path = os.path.normpath(os.path.join(base, folder, str(name)))
    if os.path.commonpath([path, base]) != base or not os.path.isfile(path) \
            or os.path.islink(path):
        return None
    return path


def _control(conn, ctx: RunContext):
    """The control kind of the run (`noop`, `random` or None), from its research-run-start."""
    row = conn.execute("SELECT data FROM events WHERE kind=? AND ref=? ORDER BY id LIMIT 1",
                       (events.RUN_START, ctx.run)).fetchone()
    try:
        return json.loads(row["data"] or "{}").get("control") if row else None
    except (TypeError, ValueError):
        return None


def _join(thread) -> None:
    """Wait for `thread` in slices of _JOIN_SLICE_S. Python runs a signal handler only in the main
    thread, between bytecodes. When the kernel hands the runner's SIGTERM to another thread (a
    slot, a pool worker), a plain join leaves the main thread in a lock wait the signal never
    interrupts, so the stop would wait for every thread to end on its own (a gate or an agent
    runs on for its whole timeout). The slices let the handler run within _JOIN_SLICE_S."""
    while thread.is_alive():
        thread.join(_JOIN_SLICE_S)


def quota_wait_until(stated, fallback, *, now=None):
    """The instant a quota retry waits until (FR-042, gap map H21, section 6.7). The reset the
    runtime's error event states (LaunchResult.reset_at: an ISO time, or a whole number of
    seconds) is the value of the provider's rate-limit reset, so it is folded with
    resilience.reset_wait.parse as that header (`ratelimit-reset` for a time, `Retry-After` for
    seconds). When the runtime states none, or one reset_wait.parse cannot read (its fallback
    path), the wait is `fallback`: the research-infra event's wait_until, which the launcher sets
    to 300 seconds doubled per try, at most 3600. Returns an ISO 8601 time, or `fallback`."""
    from alpaca.resilience import reset_wait
    text = str(stated).strip() if stated is not None else ""
    headers = {}
    if text:
        headers = {"Retry-After": text} if text.isdigit() else {"ratelimit-reset": text}
    got = reset_wait.parse(headers, now=now)
    if got.get("reset_at") is not None:
        return got["reset_at"].astimezone(datetime.timezone.utc).isoformat(timespec="seconds")
    return fallback


def _doubling(n: int) -> str:
    """now + 300 seconds doubled per quota failure before this one, at most 3600 (section 6.7)."""
    wait = min(_QUOTA_BASE_S * (2 ** max(0, int(n) - 1)), _QUOTA_MAX_S)
    at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=wait)
    return at.isoformat(timespec="seconds")


def _spec_get(spec, name, default=None):
    if isinstance(spec, dict):
        return spec.get(name, default)
    return getattr(spec, name, default)


def _spec_with(spec, **fields):
    if isinstance(spec, dict):
        return dict(spec, **fields)
    import dataclasses
    return dataclasses.replace(spec, **fields)


def _held(spec, reason: str) -> dict:
    """The LaunchResult of a launch the budget check held (the launcher's own shape)."""
    from alpaca.research import launcher
    try:
        return launcher._result(launcher._as_spec(spec), held=True, outcome="held",
                                reason=str(reason))
    except Exception:  # noqa: BLE001 - a spec the launcher cannot read still gets a held result
        return {"held": True, "outcome": "held", "reason": str(reason),
                "key": _spec_get(spec, "key"), "role": _spec_get(spec, "role"),
                "try_n": _spec_get(spec, "try_n"), "session": _spec_get(spec, "session")}


def _replayed(conn, key: str) -> bool:
    """True when the record already has this launch key's research-launch (a resumed step
    reads its end, or the launcher records it lost): the launcher launches nothing for it."""
    return conn.execute("SELECT 1 FROM events WHERE kind IN ('research-launch', "
                        "'research-launch-end') AND json_extract(data, '$.key')=? LIMIT 1",
                        (key,)).fetchone() is not None


def reserving(ctx: RunContext, fn):
    """fn, a launch callable (launcher.launch or a test's stand-in), behind the run's spend
    reservation (spend.reserve, FR-041, section 4.6): the check and the launch's dollar share are
    taken in one step, a HOLD returns the launcher's held result without launching, and the
    share goes into the spec as `max_usd` (a Claude launch's --max-budget-usd), so the launches of
    the run in flight never together pass its dollar cap. A launch key the record already holds
    is a resumed step reading its result and goes straight to fn."""
    def launch(spec, **kw):
        from alpaca.research import spend
        run, key = _spec_get(spec, "run"), _spec_get(spec, "key")
        if run is None or not key:
            return fn(spec, **kw)
        conn = kw.get("conn")
        own = conn is None
        if own:
            conn = db.connect(ctx.root)
        try:
            if _replayed(conn, key):
                return fn(spec, **kw)
            with spend.reserve(conn, ctx.root, run, key, role=_spec_get(spec, "role") or "",
                               runtime=_spec_get(spec, "runtime"),
                               slots=int(ctx.contract.slots)) as (got, share):
                if got.posture != "GO":
                    return _held(spec, got.reason)
                if share is not None and _spec_get(spec, "max_usd") is None:
                    spec = _spec_with(spec, max_usd=share)
                return fn(spec, **kw)
        finally:
            if own:
                conn.close()
    launch.stopping = getattr(fn, "stopping", None)
    return launch


# ======================================================================== the round barrier
class RoundBarrier:
    """The barrier of section 4.3: the `record` step of each slot waits until every slot of the
    round has launched its implementer, reached a verdict before `implement`, or ended its plan.
    Arrival is read from the record (so a resumed round does not wait for arrivals that already
    happened) and from the threads that ended their plan (arrive(), called in a finally)."""

    def __init__(self, tasks):
        self.tasks = list(tasks)
        self.ended = set()
        self.cond = threading.Condition()

    def arrive(self, task: str) -> None:
        with self.cond:
            self.ended.add(task)
            self.cond.notify_all()

    def wake(self) -> None:
        with self.cond:
            self.cond.notify_all()

    def wait(self, conn, ctx: RunContext, me: str) -> bool:
        """True once every other slot arrived; False when the stop flag ended the wait."""
        while True:
            pending = [t for t in self.tasks if t != me and t not in self.ended
                       and not arrived(conn, ctx, t)]
            if not pending:
                return True
            if ctx.stop is not None and ctx.stop.is_set():
                return False
            with self.cond:
                self.cond.wait(_BARRIER_POLL_S)


def arrived(conn, ctx: RunContext, task: str) -> bool:
    """A slot's arrival on the record (section 4.3, 10.4): an implementer research-launch, a
    verdict set before `implement`, its research-attempt, or a non-PASS controller-step at or
    before `record`."""
    for ev in _events(conn, events.LAUNCH, task):
        if ev["data"].get("role") == "implementer":
            return True
    if _events(conn, events.ATTEMPT, task):
        return True
    for ev in _events(conn, events.PROPOSAL, task):
        if ev["data"].get("valid") is False:
            return True
    for ev in _events(conn, events.TAMPER, task):
        if ev["data"].get("point") in ("open", "after-proposer") \
                and ev["data"].get("verdict") not in (None, "PASS"):
            return True
    for ev in _events(conn, events.WORKTREE, task):
        if ev["data"].get("action") == "create" and ev["data"].get("error"):
            return True
    infra = [ev for ev in _events(conn, events.INFRA, task) if ev["data"].get("role") == "proposer"]
    if len(infra) > int(ctx.contract.infra_retries):
        return True
    for row in conn.execute("SELECT data FROM events WHERE kind='controller-step' AND ref LIKE ?",
                            (task + ":%",)).fetchall():
        try:
            d = json.loads(row["data"] or "{}")
        except (TypeError, ValueError):
            continue
        if d.get("plan") == task and d.get("verdict") != vc.PASS \
                and d.get("index", 99) <= _RECORD_INDEX:
            return True
    return False


# ======================================================================== the attempt steps
class _Attempt:
    """The steps of one attempt task's plan. Every method takes the controller's connection."""

    def __init__(self, ctx: RunContext, task: str):
        self.ctx = ctx
        self.task = task
        self._research = None

    # ---------------------------------------------------------------- facts
    def research(self, conn) -> dict:
        if self._research is None:
            self._research = _research(conn, self.task)
        return self._research

    @property
    def root(self):
        return self.ctx.root

    @property
    def run(self):
        return self.ctx.run

    @property
    def contract(self):
        return self.ctx.contract

    def round(self, conn) -> int:
        return int(self.research(conn).get("round") or 0)

    def slot(self, conn) -> int:
        return int(self.research(conn).get("slot") or 0)

    def baseline(self, conn) -> bool:
        return bool(self.research(conn).get("baseline"))

    def category(self, conn):
        return self.research(conn).get("category")

    def attempt_dir(self) -> str:
        return paths.attempt_dir(self.root, self.run, self.task)

    def opened(self, conn) -> dict:
        """The round's research-round-open data (its champion commit)."""
        r = self.research(conn)
        ev = _round_open(conn, self.run, int(r.get("round") or 0), r.get("repair_task"))
        if ev is None:
            raise RuntimeError("no research-round-open holds %s" % self.task)
        return ev["data"]

    def champion(self, conn):
        from alpaca.research import isolation
        opened = self.opened(conn)
        r = self.research(conn)
        path = paths.champion_dir(self.root, self.run, int(r.get("round") or 0), _line(r))
        return isolation.tree_info(path, commit=opened["champion_commit"],
                                   label="r%d-champion" % int(r.get("round") or 0), admin=True,
                                   read_only=True)

    def verdict(self, conn) -> dict:
        from alpaca.research import state
        return state.attempt_verdict(conn, self.task)

    def verdict_set(self, conn) -> bool:
        return self.verdict(conn)["verdict"] is not None

    def launched(self, conn, role: str | None = None) -> bool:
        for ev in _events(conn, events.LAUNCH, self.task):
            if role is None and ev["data"].get("role") in ("proposer", "implementer"):
                return True
            if role is not None and ev["data"].get("role") == role:
                return True
        return False

    def tamper(self, conn, point: str) -> dict | None:
        got = [ev for ev in _events(conn, events.TAMPER, self.task)
               if ev["data"].get("point") == point]
        return got[-1] if got else None

    def worktree(self, conn):
        """The WorktreeInfo of the newest live research-worktree event (create, reclaim or
        rebuild with no error and no destroy after it), built from the event's keys, never from
        the tree's own `.git` file; None when there is none."""
        from alpaca.research.types import WorktreeInfo
        current = None
        for ev in _events(conn, events.WORKTREE, self.task):
            d = ev["data"]
            if d.get("action") in ("create", "reclaim", "rebuild") and not d.get("error"):
                current = d
            elif d.get("action") == "destroy":
                current = None
        if current is None:
            return None
        return WorktreeInfo(path=_abs(self.root, current["path"]), label=self.task,
                            commit=current.get("commit") or "",
                            admin_dir=_abs(self.root, current.get("admin_dir")),
                            private_objects=_abs(self.root, current.get("private_objects")),
                            index=_abs(self.root, current.get("index")),
                            host_git=_abs(self.root, current.get("host_git")))

    def _cut_events(self, conn) -> list:
        """The task's research-worktree events of a cut (action create, reclaim or rebuild with no
        error), oldest first (in-session-runtime.md section 28.1)."""
        return [ev for ev in _events(conn, events.WORKTREE, self.task)
                if ev["data"].get("action") in _CUT_ACTIONS and not ev["data"].get("error")]

    def next_cut(self, conn) -> int:
        """1 + the highest cut of the task's cut events, 1 when there is none: every recorded cut
        gets a new name, never given to another try (section 28.1). The cut is the event's `cut`
        field or, for an event written before t-357, read from its path (paths.cut_of)."""
        cuts = [_cut_number(ev["data"]) for ev in self._cut_events(conn)]
        return 1 + max(cuts) if cuts else 1

    def _cut_event_of(self, conn, path: str):
        """The newest cut event whose path is `path`, or None."""
        want = os.path.abspath(path or "")
        got = None
        for ev in self._cut_events(conn):
            if os.path.abspath(_abs(self.root, ev["data"].get("path")) or "") == want:
                got = ev
        return got

    def _reap_cut(self, conn, ev) -> dict:
        """jobs.reap of the processes left under the cut of `ev` (its tree and its admin and host
        folders) that started after the cut's own event (section 28.2). A best effort; the event
        time unknown, or any error, reaps nothing."""
        from alpaca.research import jobs
        empty = {"killed": [], "spared": [], "left": []}
        if ev is None:
            return empty
        d = ev["data"]
        since = jobs.parse_time(ev.get("ts"))
        if since is None:
            return empty
        folders = [_abs(self.root, d.get(k)) for k in ("path", "admin_dir", "host_git")]
        driver = None
        try:
            drv = jobs.newest_driver(conn, self.run)
            driver = ((drv or {}).get("data") or {}).get("pid")
        except Exception:  # noqa: BLE001 - no driver event: no driver pid to protect by number
            driver = None
        return jobs.reap(folders, since, bases=(paths.worktrees_dir(self.root, self.run),
                                                paths.attempt_dir(self.root, self.run, self.task)),
                         driver_pid=driver)

    def diffs(self, conn) -> list:
        return _events(conn, events.DIFF, self.task)

    def capture_of(self, ev: dict):
        from alpaca.research.types import DiffCapture
        d = ev["data"]
        return DiffCapture(forked_from=d.get("forked_from"), head=d.get("head"),
                           tree=d.get("tree") or "", path=_abs(self.root, d.get("path")),
                           sha256=d.get("sha256"), bytes=int(d.get("bytes") or 0),
                           paths=tuple(d.get("paths") or ()), stamp=d.get("stamp") or "",
                           message=d.get("message") or "", empty=bool(d.get("empty")),
                           gitlinks=tuple(d.get("gitlinks") or ()))

    # ---------------------------------------------------------------- fingerprints
    def take_point(self, conn, point: str, *, worktree=None):
        from alpaca.research import fingerprint
        return fingerprint.take(self.root, self.run, self.task, point, contract=self.contract,
                                worktree=worktree, clone=self.ctx.clone,
                                champion=self.champion(conn), path_env=_gate_path(self.ctx))

    def record_point(self, conn, point: str, fp, *, against=None, check=None) -> dict:
        """research-tamper for one point: compared with `against` (the open point) when given,
        with the allowlist check `check` folded in; PASS or FAIL."""
        from alpaca.research import fingerprint
        from alpaca.research.types import TamperVerdict
        if against is not None:
            got = fingerprint.compare(against, fp)
        else:
            errs = tuple(fp.errors) + tuple(
                "%s: %s" % (k, v) for k, v in fp.entries.items()
                if isinstance(v, str) and v.startswith("error: ") and k not in fp.errors)
            got = TamperVerdict(point=point, ok=not errs, errors=errs)
        ok = got.ok and (check is None or check.ok)
        affected = []
        if not got.ok and got.changed:
            try:
                since = self.tamper(conn, "open")
                affected = fingerprint.affected(conn, self.run, got.changed[0],
                                                since_event=since["id"] if since else 0)
            except Exception:  # noqa: BLE001 - the flag list is advisory; the verdict stands
                affected = []
        data = {"point": point, "changed": list(got.changed), "missing": list(got.missing),
                "outside": list(check.outside if check else got.outside),
                "protected": list(check.protected if check else got.protected),
                "nested": list(check.nested if check else got.nested),
                "errors": list(got.errors) + (list(check.errors) if check else []),
                "affected": [t for t in affected if t != self.task],
                "path": fp.path, "sha256": _sha_file(fp.path), "ok": ok}
        return _emit(conn, self.ctx, events.TAMPER, self.task, code=vc.PASS if ok else vc.FAIL,
                     data=data)

    def open_point(self):
        from alpaca.research import fingerprint
        path = os.path.join(self.attempt_dir(), "fingerprints", "open.json")
        return fingerprint._load_point(path)

    def point_step(self, conn, point: str, *, worktree=None, check=None) -> int:
        if self.tamper(conn, point) is not None:
            return vc.PASS
        fp = self.take_point(conn, point, worktree=worktree)
        try:
            base = self.open_point()
        except (OSError, ValueError, KeyError):
            base = None
        if base is None:
            self.record_point(conn, point, fp, check=check)
        else:
            self.record_point(conn, point, fp, against=base, check=check)
        return vc.PASS

    # ---------------------------------------------------------------- launches
    def spec(self, conn, role: str, try_n: int, *, cwd: str, workdir: str, prompt: str,
             write_paths=(), read_paths=(), git_admin=None, git_objects=None) -> dict:
        from alpaca.research.types import LaunchSpec, to_dict
        key = paths.launch_key(self.task, role, try_n)
        launch_id = paths.launch_id(key)
        timeout = _DEFAULT_TIMEOUTS.get(role, 1200)
        effort = None
        for agent in tuple(self.contract.agents or ()):
            if agent.role == role:
                timeout = agent.timeout_s or timeout
                effort = agent.effort
        tools = ()
        try:
            from alpaca.research import verifier
            tools = tuple(verifier.role_tools(self.root, "research-improve", role))
        except Exception:  # noqa: BLE001 - no tools: the issue refuses the job (design 26)
            tools = ()
        clone_git = os.path.join(self.ctx.clone.path, ".git") if self.ctx.clone else None
        # a control run's proposer, implementer and notes roles run the control script; every
        # other launch (a repair task's reproducer roles, the audit, the verifiers) keeps the
        # run's runtime, so a control run tests the same trust path (section 6.4, t-102)
        control = _control(conn, self.ctx) if role in CONTROL_ROLES else None
        spec = LaunchSpec(role=role, runtime="control" if control else self.ctx.runtime,
                          session=paths.launch_session(self.run, launch_id), workdir=workdir,
                          cwd=cwd, prompt=prompt, timeout_s=int(timeout), key=key, run=self.run,
                          attempt=self.task, read_paths=tuple(read_paths),
                          write_paths=tuple(write_paths), tools=tools, op=self.ctx.op,
                          worker="%s-%s-s%d" % (self.run, role, self.slot(conn)), try_n=try_n,
                          round=self.round(conn), slot=self.slot(conn), effort=effort,
                          git_objects=git_objects,
                          git_alternates=os.path.join(clone_git, "objects")
                          if git_objects and clone_git else None,
                          git_admin=git_admin, git_common=clone_git if git_admin else None,
                          control=control,
                          # controls.seed plus the slot, so the K controls of a round differ
                          control_seed=int(self.contract.controls_seed) + self.slot(conn)
                          if control else None,
                          control_mutate=_frozen_mutate(self.ctx)
                          if control == "random" else None)
        return to_dict(spec)

    def run_agent(self, conn, role: str, build, *, first_try: int = 1) -> tuple:
        """Launch `role` for this attempt, retrying an infrastructure failure up to the pack's
        count (FR-042): ("ok", result) for an ended launch (ok or timeout), ("held", result) on a
        budget HOLD, ("stopped", None) when the stop flag is set, ("spent", class) when every try
        failed. Each try has its own key <task>/<role>/t<try>; a key with a recorded end or
        infrastructure failure is not launched again, and a quota failure waits until the
        instant its research-infra event names before the next try. first_try numbers the
        first try (1); a caller that launches the role again for a new purpose (a repair task's
        second proof of a changed reproducer) starts past the keys of the earlier one, so it
        never replays their recorded end."""
        retries = max(0, int(self.contract.infra_retries))
        last_class, wait_until = None, None
        quotas = 0
        try_n = int(first_try) - 1
        while True:
            try_n += 1
            key = paths.launch_key(self.task, role, try_n)
            ended = [ev for ev in _events(conn, events.LAUNCH_END, self.task)
                     if ev["data"].get("key") == key]
            failed = [ev for ev in _events(conn, events.INFRA, self.task)
                      if ev["data"].get("key") == key]
            if ended and ended[-1]["data"].get("outcome") in ("ok", "timeout"):
                return "ok", self.launch(conn, build(try_n))
            if ended or failed:
                last = (failed or ended)[-1]["data"]
                last_class = last.get("class") or last.get("outcome")
                wait_until = None
                if last_class == "quota":
                    quotas += 1
                    wait_until = quota_wait_until(last.get("reset_at"),
                                                  last.get("wait_until") or _doubling(quotas))
                continue
            if try_n - int(first_try) + 1 > retries + 1:
                return "spent", last_class
            if self.ctx.stop is not None and self.ctx.stop.is_set():
                return "stopped", None
            if wait_until:
                if not self.wait(wait_until):
                    return "stopped", None
            result = self.launch(conn, build(try_n))
            if result.get("held"):
                return "held", result
            outcome = result.get("outcome")
            if outcome in ("ok", "timeout"):
                return "ok", result
            last_class = result.get("infra") or outcome
            wait_until = None
            if last_class == "quota":
                quotas += 1
                wait_until = quota_wait_until(result.get("reset_at"),
                                              self.infra_wait(conn, key) or _doubling(quotas))

    def infra_wait(self, conn, key: str):
        for ev in reversed(_events(conn, events.INFRA, self.task)):
            if ev["data"].get("key") == key:
                return ev["data"].get("wait_until")
        return None

    def wait(self, instant: str) -> bool:
        """Sleep until `instant` (ISO 8601), in slices, while the stop flag is not set."""
        try:
            at = datetime.datetime.fromisoformat(str(instant).replace("Z", "+00:00"))
        except ValueError:
            return True
        if at.tzinfo is None:
            at = at.replace(tzinfo=datetime.timezone.utc)
        sleep = _hooks.get("sleep") or time.sleep
        while True:
            left = (at - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
            if left <= 0:
                return True
            if self.ctx.stop is not None and self.ctx.stop.is_set():
                return False
            sleep(min(left, 1.0))

    def launch(self, conn, spec: dict) -> dict:
        fn = _hooks.get("launch")
        if fn is None:
            from alpaca.research import session_runtime
            fn = session_runtime.launch_for(self.ctx)
        return reserving(self.ctx, fn)(spec, conn=conn, root=self.root)

    # ---------------------------------------------------------------- prompts
    def notes_text(self, conn) -> str | None:
        from alpaca import messages
        lines = []
        for msg in messages.read(conn, to=self.run):
            if msg.get("kind") == "note" and msg.get("sender") == ACTOR:
                lines.append("- %s" % " ".join(str(msg.get("body") or "").split()))
        return "\n".join(lines) if lines else None

    def lessons_text(self) -> str | None:
        name = self.contract.lessons_file
        if not name:
            return None
        base = os.path.join(paths.contract_dir(self.root, self.run), "files")
        for cand in (os.path.join(base, os.path.dirname(self.contract.runbook), name),
                     os.path.join(base, name)):
            if os.path.isfile(cand) and not os.path.islink(cand):
                with open(cand, encoding="utf-8", errors="replace") as fh:
                    return fh.read()
        return None

    def repair_texts(self, conn, *, proposal_text: str | None = None,
                     implementer: bool = False) -> dict:
        """A fix attempt's repair texts (section 6.8): the case's bug report from the frozen
        contract copy and repair.hints (the suspect files, the chain and their coupled groups;
        for the implementer also the groups of the proposal's `suspects` and `files` and its
        `evidence` chain), each as untrusted text. {} for an improve attempt."""
        from alpaca.research import repair
        from alpaca.research.types import Proposal
        line = self.research(conn).get("repair_task")
        if not line:
            return {}
        item = repair._item(self.ctx, conn, line)
        proposal = None
        if implementer and proposal_text:
            try:
                fields = json.loads(proposal_text)
            except (TypeError, ValueError, RecursionError):
                fields = None
            if isinstance(fields, dict):
                proposal = Proposal(attempt=self.task, category=self.category(conn) or "",
                                    fields=fields, sha256="", path="")
        got = repair.hints(self.ctx, line, proposal=proposal)
        return {"bug_report": repair.bug_report_text(self.ctx, item),
                "hints": repair.hints_text(got)}

    def prompt(self, conn, role: str, cwd: str, untrusted: dict) -> str:
        from alpaca.research import prompting
        context = {"run": self.run, "round": self.round(conn), "slot": self.slot(conn),
                   "cwd": cwd, "attempt": self.task}
        return prompting.render(role, contract=self.contract, context=context,
                                untrusted={k: v for k, v in untrusted.items() if v is not None})

    # ---------------------------------------------------------------- steps
    def claim(self, conn) -> int:
        got = leases.take(conn, self.task, self.run, _LEASE_MINUTES, session=self.ctx.session,
                          reason="research attempt")
        if got.get("status") != leases.CLAIMED:
            raise RuntimeError("%s is held by %s" % (self.task, got.get("worker")))
        return vc.PASS

    def open_window(self, conn) -> int:
        if self.tamper(conn, "open") is not None:
            return vc.PASS
        fp = self.take_point(conn, "open")
        self.record_point(conn, "open", fp)
        return vc.PASS

    def propose(self, conn) -> int:
        from alpaca.research import proposal as proposal_mod
        if self.verdict_set(conn) or _events(conn, events.PROPOSAL, self.task):
            return vc.PASS
        champ = self.champion(conn)
        untrusted = {"notes": self.notes_text(conn), "lessons": self.lessons_text()}
        untrusted.update(self.repair_texts(conn))
        text = self.prompt(conn, "proposer", champ.path, untrusted)

        def build(try_n):
            return self.spec(conn, "proposer", try_n, cwd=champ.path, workdir=champ.path,
                             prompt=text, git_admin=champ.admin_dir)
        how, result = self.run_agent(conn, "proposer", build)
        if how == "held":
            _budget_hold(conn, self.ctx, result.get("reason"))
            return vc.PAUSED
        if how == "stopped":
            return vc.PAUSED
        if how == "spent":
            return vc.PASS
        path = os.path.join(self.attempt_dir(), "proposal.json")
        category = self.category(conn) if tuple(self.contract.categories or ()) else None
        try:
            got = proposal_mod.parse(result.get("final_message") or "", self.contract,
                                     attempt=self.task, category=category, path=path)
        except ValueError as exc:
            findings = exc.args[1] if len(exc.args) > 1 else [
                {"code": "PROPOSAL-UNREADABLE", "where": "", "message": str(exc)}]
            _emit(conn, self.ctx, events.PROPOSAL, self.task, code=vc.FAIL,
                  data={"valid": False, "problems": findings, "category": category,
                        "path": _rel(self.root, path), "sha256": _sha_file(path),
                        "outcome": result.get("outcome")})
            return vc.PASS
        _emit(conn, self.ctx, events.PROPOSAL, self.task, code=vc.PASS,
              data={"valid": True, "problems": [], "category": category,
                    "path": _rel(self.root, path), "sha256": got.sha256,
                    "outcome": result.get("outcome")})
        return vc.PASS

    def check_proposer(self, conn) -> int:
        if not self.launched(conn, "proposer"):
            return vc.PASS
        return self.point_step(conn, "after-proposer")

    def isolate(self, conn) -> int:
        from alpaca.research import isolation
        if self.verdict_set(conn):
            return vc.PASS
        live = self.worktree(conn)
        commit = self.opened(conn)["champion_commit"]
        if live is not None:
            if os.path.isdir(live.path) and not os.path.islink(live.path):
                return vc.PASS
            # the live tree's folder is gone: the next cut, never the same name (section 28.1)
            wt = isolation.create_worktree(self.root, self.run, self.task, self.ctx.clone,
                                           commit=live.commit or commit, cut=self.next_cut(conn))
            self.record_tree(conn, "reclaim", wt, retired=_rel(self.root, live.path))
            return vc.PASS
        cut = self.next_cut(conn)
        try:
            wt = isolation.create_worktree(self.root, self.run, self.task, self.ctx.clone,
                                           commit=commit, cut=cut)
        except IsolationError as exc:
            _emit(conn, self.ctx, events.WORKTREE, self.task, reason=exc.reason,
                  data={"action": "create", "path": _rel(self.root, paths.worktree_dir(
                      self.root, self.run, self.task, cut)), "commit": commit, "head": commit,
                      "cut": cut, "error": exc.detail or exc.reason})
            return vc.PASS
        self.record_tree(conn, "create", wt)
        return vc.PASS

    def record_tree(self, conn, action: str, wt, **extra) -> dict:
        data = {"action": action, "path": _rel(self.root, wt.path),
                "admin_dir": _rel(self.root, wt.admin_dir), "index": _rel(self.root, wt.index),
                "host_git": _rel(self.root, wt.host_git),
                "private_objects": _rel(self.root, wt.private_objects), "commit": wt.commit,
                "head": extra.pop("head", wt.commit), "error": None}
        cut = paths.cut_of(os.path.basename(os.path.abspath(wt.path or "")))
        if cut is not None:
            data["cut"] = cut[1]
        data.update(extra)
        return _emit(conn, self.ctx, events.WORKTREE, self.task, data=data)

    def _reclaim_tree(self, conn, wt, after_launch: int):
        """Cut the next tree of the attempt (in-session-runtime.md section 28.2): the processes
        the dead cut left are reaped as a best effort (jobs.reap), the next cut is made at its own
        name and the old cut stays in place until teardown, so a dead try that is still writing
        reaches neither the new tree nor this step. The event keeps action `reclaim` with
        `after_launch` and adds `retired` (the old path), `reaped` and `spared`; then a `reopen`
        point is compared with the open point (section 10.4). Every reclaim takes its own point.
        Returns the new WorktreeInfo."""
        from alpaca.research import isolation
        got = self._reap_cut(conn, self._cut_event_of(conn, wt.path))
        old = wt
        wt = isolation.create_worktree(self.root, self.run, self.task, self.ctx.clone,
                                       commit=wt.commit, cut=self.next_cut(conn))
        extra = {"after_launch": after_launch, "retired": _rel(self.root, old.path),
                 "reaped": list(got.get("killed") or []),
                 "spared": [{"pid": x.get("pid"), "why": x.get("why")}
                            for x in got.get("spared") or []]}
        if got.get("error"):
            extra["reap_error"] = str(got["error"])[:200]
        self.record_tree(conn, "reclaim", wt, **extra)
        fp = self.take_point(conn, "reopen", worktree=wt)
        try:
            base = self.open_point()
        except (OSError, ValueError, KeyError):
            base = None
        self.record_point(conn, "reopen", fp, against=base)
        return wt

    def _reclaimed_after(self, conn, launch_id: int) -> bool:
        return any(ev["data"].get("action") == "reclaim"
                   and ev["data"].get("after_launch") == launch_id
                   for ev in _events(conn, events.WORKTREE, self.task))

    def _reclaim_killed(self, conn, wt, killed):
        """The implement step's reclaim (section 10, rule 1): the next tree is cut (section 28.2)
        when no reclaim has `after_launch` equal to the id of the NEWEST killed implementer
        launch. A reclaim of an older killed launch does not cover a later one."""
        if not killed:
            return wt
        newest = max(k["id"] for k in killed)
        if self._reclaimed_after(conn, newest):
            return wt
        # killed mid-launch: the next tree is cut first, then a reopen point is compared with
        # the open point (section 10.4)
        return self._reclaim_tree(conn, wt, newest)

    def _try_replayed(self, conn, try_n: int) -> bool:
        """True when try `try_n` of the implementer has a recorded end that run_agent replays."""
        key = paths.launch_key(self.task, "implementer", try_n)
        return any(ev["data"].get("key") == key
                   and ev["data"].get("outcome") in ("ok", "timeout")
                   for ev in _events(conn, events.LAUNCH_END, self.task))

    def _reclaim_before_try(self, conn, wt):
        """Before an implementer try after the first (section 10, rule 2): cut the next tree
        (section 28.2) unless the newest implementer launch already has a reclaim after it (the
        step's own, after a resume). The first try of a step makes no extra tree."""
        launches = [ev for ev in _events(conn, events.LAUNCH, self.task)
                    if ev["data"].get("role") == "implementer"]
        if not launches:
            return wt
        newest = max(ev["id"] for ev in launches)
        if self._reclaimed_after(conn, newest):
            return wt
        return self._reclaim_tree(conn, wt, newest)

    def implement(self, conn) -> int:
        if self.verdict_set(conn):
            return vc.PASS
        wt = self.worktree(conn)
        if wt is None:
            return vc.PASS
        for ev in _events(conn, events.LAUNCH_END, self.task):
            if ev["data"].get("role") == "implementer" \
                    and ev["data"].get("outcome") in ("ok", "timeout"):
                return vc.PASS
        ends = {ev["data"].get("key") for ev in _events(conn, events.LAUNCH_END, self.task)}
        # an infra row of class lost that names a job (a session launch whose job expired or was
        # refused) counts as killed: a subagent may still run it, so the worktree is reclaimed
        # when the step runs again after a resume (in-session-runtime.md section 10)
        lost = {ev["data"].get("key") for ev in _events(conn, events.INFRA, self.task)
                if not (ev["data"].get("class") == "lost" and ev["data"].get("job"))}
        killed = [ev for ev in _events(conn, events.LAUNCH, self.task)
                  if ev["data"].get("role") == "implementer"
                  and ev["data"].get("key") not in ends and ev["data"].get("key") not in lost]
        wt = self._reclaim_killed(conn, wt, killed)
        props = _events(conn, events.PROPOSAL, self.task)
        proposal_text = None
        if props and props[-1]["data"].get("path"):
            full = _abs(self.root, props[-1]["data"]["path"])
            try:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    proposal_text = fh.read()
            except OSError:
                proposal_text = None
        untrusted = {"proposal": proposal_text or "(none)"}
        untrusted.update(self.repair_texts(conn, proposal_text=proposal_text, implementer=True))

        tree = {"wt": wt}
        texts = {}

        def build(try_n):
            # every try after the first starts on its own new cut with its own reopen point
            # (section 10, rule 2; section 28.2); a try whose end is recorded is only replayed,
            # so the tree that holds its diff is never touched
            if try_n > 1 and not self._try_replayed(conn, try_n):
                tree["wt"] = self._reclaim_before_try(conn, tree["wt"])
            w = tree["wt"]
            # the prompt names the tree this try runs in, rendered once per tree (section 28.1)
            if w.path not in texts:
                texts[w.path] = self.prompt(conn, "implementer", w.path, untrusted)
            return self.spec(conn, "implementer", try_n, cwd=w.path, workdir=w.path,
                             prompt=texts[w.path], write_paths=(w.path,), git_admin=w.admin_dir,
                             git_objects=w.private_objects)
        how, result = self.run_agent(conn, "implementer", build)
        if how == "held":
            _budget_hold(conn, self.ctx, result.get("reason"))
            return vc.PAUSED
        if how == "stopped":
            return vc.PAUSED
        return vc.PASS

    def check_implementer(self, conn) -> int:
        from alpaca.research import fingerprint
        if not self.launched(conn, "implementer") or self.tamper(conn, "after-implementer"):
            return vc.PASS
        wt = self.worktree(conn)
        check = None
        if wt is not None:
            check = fingerprint.check_paths(self.ctx.clone, wt, forked_from=wt.commit,
                                            editable=tuple(self.contract.editable),
                                            protected=tuple(self.contract.protected))
            check = self.attributed(conn, check, wt)
        return self.point_step(conn, "after-implementer", worktree=wt, check=check)

    def attributed(self, conn, check, wt):
        """The attribution rule of a session run (in-session-runtime.md section 5.3): every path
        the worktree changed that no write call of the accepted launch's transcript names and no
        shell call mentions is an error of the check, so the verdict is FAIL and the attempt is
        broken with class tamper. It is the part that catches a driver that edited the live
        worktree itself; a straggler of a dead try writes into its own dead cut, which no later
        step reads (section 28.1), and a call made only of reads names nothing (section 28.4)."""
        import dataclasses
        ends = [ev for ev in _events(conn, events.LAUNCH_END, self.task)
                if ev["data"].get("role") == "implementer" and ev["data"].get("mode") == "session"
                and ev["data"].get("outcome") in ("ok", "timeout")]
        if not ends or not check.changed:
            return check
        d = ends[-1]["data"]
        path = d.get("transcript")
        if not path or not os.path.isfile(path):
            return check
        from alpaca.research import agent_transcript, session_runtime
        calls = agent_transcript.calls(agent_transcript.read_records(path))
        shell = None
        for ev in _events(conn, events.LAUNCH, self.task):
            if ev["data"].get("key") == d.get("key"):
                shell = ev["data"].get("shell_cwd")
        stray = session_runtime.unattributed(calls, list(check.changed), wt.path, reset_cwd=shell)
        if not stray:
            return check
        errs = tuple(check.errors) + tuple(
            "%s changed by no call of the implementer's transcript" % p for p in stray)
        return dataclasses.replace(check, ok=False, errors=errs)

    def verdict_event_id(self, conn):
        """The id of the event that set the attempt's provisional verdict, for an empty capture's
        `reason`."""
        v = self.verdict(conn)
        if v["verdict"] is None:
            return None
        kinds = (events.PROPOSAL, events.TAMPER, events.WORKTREE, events.INFRA)
        for row in conn.execute("SELECT id, kind, data FROM events WHERE ref=? AND kind IN (?,?,?,?) "
                                "ORDER BY id", (self.task,) + kinds).fetchall():
            d = json.loads(row["data"] or "{}")
            if (row["kind"] == events.PROPOSAL and d.get("valid") is False) \
                    or (row["kind"] == events.TAMPER and d.get("verdict") not in (None, "PASS")) \
                    or (row["kind"] == events.WORKTREE and d.get("error")) \
                    or row["kind"] == events.INFRA:
                return row["id"]
        return None

    def capture(self, conn) -> int:
        from alpaca.research import fingerprint, isolation
        if self.diffs(conn):
            return vc.PASS
        wt = self.worktree(conn)
        forked = self.opened(conn)["champion_commit"]
        out_dir = self.attempt_dir()
        os.makedirs(out_dir, exist_ok=True)
        if wt is None or self.baseline(conn):
            path = os.path.join(out_dir, "attempt.diff")
            with open(path, "wb"):
                pass
            reason = "baseline" if self.baseline(conn) and wt is not None \
                else self.verdict_event_id(conn)
            _emit(conn, self.ctx, events.DIFF, self.task, code=vc.PASS,
                  data={"forked_from": forked, "head": forked, "tree": None,
                        "path": _rel(self.root, path), "sha256": hashlib.sha256(b"").hexdigest(),
                        "bytes": 0, "paths": [], "gitlinks": [], "outside": [], "protected": [],
                        "nested": [], "empty": True, "reason": reason, "error": None,
                        "stamp": None, "message": None})
            return vc.PASS
        message = "research attempt %s of %s" % (self.task, self.run)
        stamp = paths.stamp()
        try:
            cap = isolation.capture(self.ctx.clone, wt, forked_from=wt.commit, message=message,
                                    stamp=stamp, out_dir=out_dir)
        except IsolationError as exc:
            _emit(conn, self.ctx, events.DIFF, self.task, code=vc.FAIL,
                  data={"forked_from": wt.commit, "head": None, "path": None, "sha256": None,
                        "bytes": 0, "paths": [], "gitlinks": [], "outside": [], "protected": [],
                        "nested": [], "empty": False, "reason": None,
                        "error": "%s: %s" % (exc.reason, exc.detail)})
            return vc.PASS
        check = fingerprint.check_diff(cap, editable=tuple(self.contract.editable),
                                       protected=tuple(self.contract.protected))
        _emit(conn, self.ctx, events.DIFF, self.task, code=vc.PASS if check.ok else vc.FAIL,
              data={"forked_from": cap.forked_from, "head": cap.head, "tree": cap.tree,
                    "path": _rel(self.root, cap.path), "sha256": cap.sha256, "bytes": cap.bytes,
                    "paths": list(cap.paths), "gitlinks": list(cap.gitlinks),
                    "outside": list(check.outside), "protected": list(check.protected),
                    "nested": list(check.nested), "empty": cap.empty, "reason": None,
                    "error": None, "stamp": cap.stamp, "message": cap.message})
        return vc.PASS

    def head(self, conn) -> str | None:
        diffs = self.diffs(conn)
        return diffs[0]["data"].get("head") if diffs else None

    def ensure_tree(self, conn):
        """The live worktree, rebuilt from the recorded diff when a resume finds it missing
        (`research-worktree` action rebuild, section 10.5)."""
        from alpaca.research import isolation
        wt = self.worktree(conn)
        if wt is None or (os.path.isdir(wt.path) and not os.path.islink(wt.path)):
            return wt
        diffs = self.diffs(conn)
        if not diffs or diffs[0]["data"].get("error"):
            return None
        cap = self.capture_of(diffs[0])
        try:
            got = isolation.rebuild(self.root, self.run, self.task, self.ctx.clone, recorded=cap,
                                    message=cap.message or "research attempt %s of %s"
                                    % (self.task, self.run), stamp=cap.stamp or paths.stamp(),
                                    cut=self.next_cut(conn))
        except IsolationError as exc:
            _emit(conn, self.ctx, events.WORKTREE, self.task, reason=exc.reason,
                  data={"action": "rebuild", "path": _rel(self.root, wt.path),
                        "commit": wt.commit, "head": cap.head, "error": exc.detail or exc.reason})
            return None
        self.record_tree(conn, "rebuild", got, head=cap.head)
        _emit(conn, self.ctx, events.PURGE, self.task, code=vc.PASS,
              data={"phase": "pre-eval", "removed": [], "kept": [], "new": [], "changed": [],
                    "nested": [], "ok": True, "rebuilt": True})
        return got

    def purge(self, conn) -> int:
        from alpaca.research import isolation
        if self.verdict_set(conn) or self.worktree(conn) is None:
            return vc.PASS
        if any(ev["data"].get("phase") == "pre-eval" for ev in _events(conn, events.PURGE, self.task)):
            return vc.PASS
        wt = self.ensure_tree(conn)
        if wt is None:
            return vc.PASS
        if any(ev["data"].get("phase") == "pre-eval"
               for ev in _events(conn, events.PURGE, self.task)):
            return vc.PASS          # ensure_tree rebuilt it: a fresh cut, recorded as its purge
        head = self.head(conn) or wt.commit
        try:
            got = isolation.purge_ignored(self.ctx.clone, wt, head=head,
                                          keep=tuple(self.contract.keep))
        except IsolationError as exc:
            _emit(conn, self.ctx, events.PURGE, self.task, code=vc.FAIL,
                  data={"phase": "pre-eval", "removed": [], "kept": [], "new": [], "changed": [],
                        "nested": [], "ok": False, "error": "%s: %s" % (exc.reason, exc.detail)})
            return vc.PASS
        _emit(conn, self.ctx, events.PURGE, self.task, code=vc.PASS if got.ok else vc.FAIL,
              data={"phase": "pre-eval", "removed": list(got.removed), "kept": list(got.kept),
                    "new": list(got.new), "changed": list(got.changed),
                    "nested": list(got.nested), "ok": got.ok})
        return vc.PASS

    def stopping(self) -> bool:
        return self.ctx.stop is not None and self.ctx.stop.is_set()

    def repair_baseline(self, conn):
        """The PassToPass of the repair task this fix attempt serves: the `pass_to_pass` key of
        its research-baseline (types.to_dict(PassToPass), recorded by the repair plan's
        base-measure step next to the event's own `repair_task`, `ok`, `min_tests` and `code`,
        section 8.3 row research-baseline)."""
        from alpaca.research.types import PassToPass
        line = self.research(conn).get("repair_task")
        got = _events(conn, events.BASELINE, line)
        if not got:
            raise RuntimeError("the repair task %s has no research-baseline" % line)
        d = got[-1]["data"].get("pass_to_pass") or {}
        return PassToPass(baseline=tuple(d.get("baseline") or ()),
                          base_pass=tuple(d.get("base_pass") or ()),
                          attempt_pass=tuple(d.get("attempt_pass") or ()),
                          lost=tuple(d.get("lost") or ()), ok=bool(d.get("ok")),
                          base_event=d.get("base_event"), attempt_event=d.get("attempt_event"))

    def repair_results(self, conn) -> tuple:
        """(FailToPass, PassToPass) of this fix attempt, rebuilt from its newest attempt-side
        research-gate events of the fail-to-pass and pass-to-pass items (gates.py records them
        with their `fail_to_pass` and `pass_to_pass` data)."""
        from alpaca.research.types import FailToPass, PassToPass
        f2p = FailToPass(tests=(), base={}, attempt={}, weak=(), ok=False)
        p2p = PassToPass(baseline=(), base_pass=(), attempt_pass=(), lost=(), ok=False)
        for ev in _events(conn, events.GATE, self.task):
            d = ev["data"]
            if d.get("side") != "attempt" or d.get("stopped"):
                continue
            stage = str(d.get("stage") or "")
            if stage.startswith("fail-to-pass:") and isinstance(d.get("fail_to_pass"), dict):
                got = d["fail_to_pass"]
                f2p = FailToPass(tests=tuple(d.get("tests_named") or ()),
                                 base=dict(got.get("base") or {}),
                                 attempt=dict(got.get("attempt") or {}),
                                 weak=tuple(d.get("weak") or ()), ok=bool(got.get("ok")),
                                 attempt_event=ev["id"])
            elif stage == "pass-to-pass" and isinstance(d.get("pass_to_pass"), dict):
                got = d["pass_to_pass"]
                p2p = PassToPass(baseline=tuple(got.get("baseline") or ()),
                                 base_pass=tuple(got.get("base_pass") or ()),
                                 attempt_pass=tuple(got.get("attempt_pass") or ()),
                                 lost=tuple(got.get("lost") or ()), ok=bool(got.get("ok")),
                                 attempt_event=ev["id"])
        return f2p, p2p

    def base_tree(self, conn):
        """A fix attempt's base tree (section 7.4): a fresh tree of the line's clone at
        repair.case_base under attempts/<task>/base, a leftover of a killed runner removed
        first. The gate jail overlays the frozen copies on it as on every tree it runs
        (gates._overlays), so none are copied in here."""
        from alpaca.research import isolation, repair
        line = self.research(conn).get("repair_task")
        commit = repair.case_base(conn, self.ctx, line)
        path = os.path.join(self.attempt_dir(), "base")
        old = isolation.tree_info(path, commit=commit, label="base", admin=False)
        if any(p and os.path.lexists(p) for p in (old.path, old.host_git)):
            isolation.destroy_worktree(self.ctx.clone, old)
        return isolation.fresh_tree(self.root, self.ctx.clone, commit=commit, path=path)

    def gates(self, conn) -> int:
        from alpaca.research import gates, isolation
        if self.verdict_set(conn) or self.worktree(conn) is None:
            return vc.PASS
        if self.stopping():
            return vc.PAUSED
        wt = self.ensure_tree(conn)
        if wt is None:
            return vc.PASS
        line = self.research(conn).get("repair_task")
        extra, base = {}, None
        if line:
            # a repair fix attempt (rows 55 and 56): fail-to-pass and pass-to-pass over the
            # base tree at the case base and the attempt tree, against the task's base set
            baseline = self.repair_baseline(conn)
            base = self.base_tree(conn)
            extra = {"repair_case": _research(conn, line).get("case"), "base": base,
                     "baseline": baseline}
        try:
            gates.run_chain(conn, self.root, run=self.run, task=self.task,
                            contract=self.contract, wt=wt, mode=self.contract.mode,
                            session=self.ctx.session, **extra)
        finally:
            if base is not None:
                isolation.destroy_worktree(self.ctx.clone, base)
        if self.stopping():
            # a stop signal while the chain ran: the gate it killed is recorded stopped, with no
            # verdict, and this step records no PASS, so the resumed step runs it again (EC-016)
            return vc.PAUSED
        if self.tamper(conn, "after-eval") is None:
            self.point_step(conn, "after-eval", worktree=wt)
        if not any(ev["data"].get("phase") == "post-eval"
                   for ev in _events(conn, events.PURGE, self.task)):
            pre = [ev for ev in _events(conn, events.PURGE, self.task)
                   if ev["data"].get("phase") == "pre-eval"]
            before = tuple(pre[-1]["data"].get("kept") or ()) if pre else ()
            try:
                got = isolation.post_eval(self.ctx.clone, wt, head=self.head(conn) or wt.commit,
                                          keep=tuple(self.contract.keep),
                                          eval_outputs=tuple(self.contract.outputs),
                                          before=before)
                data = {"phase": "post-eval", "removed": list(got.removed),
                        "kept": list(got.kept), "new": list(got.new),
                        "changed": list(got.changed), "nested": list(got.nested), "ok": got.ok}
            except IsolationError as exc:
                data = {"phase": "post-eval", "removed": [], "kept": [], "new": [],
                        "changed": [], "nested": [], "ok": False,
                        "error": "%s: %s" % (exc.reason, exc.detail)}
            _emit(conn, self.ctx, events.PURGE, self.task,
                  code=vc.PASS if data["ok"] else vc.FAIL, data=data)
        return vc.PASS

    def diff_check(self, conn) -> dict | None:
        """The FR-018 check (section 10.6): exactly one research-diff whose file, the zero-byte
        file of an empty capture included, still has the recorded sha256. None when it holds,
        else the research-diff-refused data (without `step`)."""
        diffs = self.diffs(conn)
        key = self.research(conn).get("key") or self.task
        if len(diffs) != 1:
            return {"attempt": key, "task": self.task, "found": len(diffs),
                    "diff_event": None, "recorded_sha256": None, "actual_sha256": None}
        d = diffs[0]["data"]
        actual = _sha_file(_abs(self.root, d.get("path"))) if d.get("path") else None
        if d.get("error") or actual is None or actual != d.get("sha256"):
            return {"attempt": key, "task": self.task, "found": 1, "diff_event": diffs[0]["id"],
                    "recorded_sha256": d.get("sha256"), "actual_sha256": actual}
        return None

    def refuse_diff(self, conn, step: str) -> bool:
        if _events(conn, events.DIFF_REFUSED, self.task):
            return True
        bad = self.diff_check(conn)
        if bad is None:
            return False
        _emit(conn, self.ctx, events.DIFF_REFUSED, self.task, code=vc.FAIL,
              data=dict(bad, step=step))
        return True

    def metrics(self, conn) -> dict:
        """metric id -> MetricValue of this attempt, from its newest research-gate event per
        metric item on the attempt side."""
        from alpaca.research.types import MetricValue
        out = {}
        for ev in _events(conn, events.GATE, self.task):
            d = ev["data"]
            stage = str(d.get("stage") or "")
            if not stage.startswith("metric:") or d.get("side") not in (None, "attempt") \
                    or d.get("stopped"):
                continue
            m = d.get("metric") if isinstance(d.get("metric"), dict) else {}
            mid = stage[len("metric:"):]
            out[mid] = MetricValue(metric=mid, median=m.get("median"),
                                   repeats=tuple(m.get("values") or ()),
                                   n_ok=int(m.get("n_ok") or 0), spread=m.get("spread"),
                                   untested=bool(m.get("untested")))
        return out

    def champion_values(self, conn) -> dict:
        """The start-of-round champion's recorded values (the champion_metrics of the line's
        newest research-round before this round), as MetricValue; {} for round 0."""
        return champion_metrics(conn, self.ctx, self.round(conn),
                                self.research(conn).get("repair_task"))

    def judge(self, conn) -> int:
        from alpaca.research import accept
        from alpaca.research.types import to_dict
        if _events(conn, events.DIFF_REFUSED, self.task) or self.verdict_set(conn):
            return vc.PASS
        if _events(conn, events.ACCEPT, self.task):
            return vc.PASS
        if self.refuse_diff(conn, "judge"):
            return vc.PASS
        if self.research(conn).get("repair_task"):
            # row 56: the repair rule, against the repair task's base pass-to-pass set
            from alpaca.research import repair
            from alpaca.research.types import AcceptDecision
            problem = repair.fix_problem(conn, self.ctx, self.task)
            if problem is not None:
                # FR-005, EC-018: no fix counts without a reproducer proved before it started
                decision = AcceptDecision(accepted=False, outcome="not-fixed", phase="repair",
                                          reason="not fixed: " + problem)
            else:
                f2p, p2p = self.repair_results(conn)
                decision = accept.repair(f2p, p2p, self.repair_baseline(conn))
        elif self.contract.mode == "improve":
            decision = accept.improve(self.champion_values(conn), self.metrics(conn),
                                      self.contract)
        else:
            raise RuntimeError("%s is a repair attempt with no repair task" % self.task)
        _emit(conn, self.ctx, events.ACCEPT, self.task,
              code=vc.PASS if decision.accepted else vc.FAIL, data=to_dict(decision))
        return vc.PASS

    def audit(self, conn) -> int:
        from alpaca.research import audit
        if not self.launched(conn):
            return vc.PASS
        if self.tamper(conn, "after-audit") is not None:
            return vc.PASS
        if not _stub(audit.run):
            from alpaca.research import session_runtime
            timeout = None
            for agent in tuple(self.contract.agents or ()):
                if agent.role == "auditor" and agent.timeout_s:
                    timeout = int(agent.timeout_s)
            fn = _hooks.get("audit_launch") or _hooks.get("launch") \
                or session_runtime.launch_for(self.ctx)
            got = audit.run(conn, self.root, self.task, runtime=self.ctx.runtime,
                            session=self.ctx.session, launch=reserving(self.ctx, fn),
                            retries=int(self.contract.infra_retries), timeout_s=timeout)
            if isinstance(got, dict) and got.get("state") == "halted":
                return vc.PAUSED         # the run is ending: the audit resumes where it was
            if isinstance(got, dict) and got.get("state") == "paused":
                _budget_hold(conn, self.ctx, got.get("reason") or "an audit launch was held")
                return vc.PAUSED
        return self.point_step(conn, "after-audit", worktree=self.worktree(conn))

    def notes(self, conn) -> int:
        from alpaca import messages
        if not self.launched(conn) or self.tamper(conn, "after-notes") is not None:
            return vc.PASS
        noted = conn.execute("SELECT 1 FROM messages WHERE kind='note' AND ref=?",
                             (self.task,)).fetchone()
        if noted is None:
            folder = os.path.join(self.attempt_dir(), "notes")
            os.makedirs(folder, exist_ok=True)
            v = self.verdict(conn)
            verdict_text = "verdict %s, outcome %s%s" % (
                v["verdict"], v["outcome"],
                ", class %s" % v["broken_class"] if v["broken_class"] else "")
            props = _events(conn, events.PROPOSAL, self.task)
            proposal_text = None
            if props and props[-1]["data"].get("path"):
                try:
                    with open(_abs(self.root, props[-1]["data"]["path"]), encoding="utf-8",
                              errors="replace") as fh:
                        proposal_text = fh.read()
                except OSError:
                    proposal_text = None
            for name, body in (("proposal.json", proposal_text or ""),
                               ("verdict.txt", verdict_text + "\n")):
                util.write_text(os.path.join(folder, name), body)
            diffs = self.diffs(conn)
            if diffs and diffs[0]["data"].get("path"):
                src = _abs(self.root, diffs[0]["data"]["path"])
                try:
                    with open(src, "rb") as fh, open(os.path.join(folder, "attempt.diff"), "wb") as out:
                        out.write(fh.read())
                except OSError:
                    pass
            text = self.prompt(conn, "notes", folder, {"proposal": proposal_text,
                                                       "verdict": verdict_text})

            def build(try_n):
                return self.spec(conn, "notes", try_n, cwd=folder, workdir=folder, prompt=text)
            how, result = self.run_agent(conn, "notes", build)
            if how == "held":
                _budget_hold(conn, self.ctx, result.get("reason"))
                return vc.PAUSED
            if how == "stopped":
                return vc.PAUSED
            if how == "ok":
                line = " ".join(str(result.get("final_message") or "").split())
                line = line[:int(self.contract.note_chars or 300)] or "(no note)"
                messages.post(conn, ACTOR, self.run, "note", line, pointer=self.task,
                              session=self.ctx.session, root=self.root)
        return self.point_step(conn, "after-notes", worktree=self.worktree(conn))

    def close_window(self, conn) -> int:
        return self.point_step(conn, "close", worktree=self.worktree(conn))

    def record(self, conn) -> int:
        if _events(conn, events.ATTEMPT, self.task):
            return vc.PASS
        if not self.baseline(conn) and self.ctx.barrier is not None:
            if not self.ctx.barrier.wait(conn, self.ctx, self.task):
                return vc.PAUSED
        self.refuse_diff(conn, "record")
        code, data = attempt_record(conn, self.ctx, self.task)
        _emit(conn, self.ctx, events.ATTEMPT, self.task, code=code, data=data)
        return vc.PASS

    def teardown(self, conn) -> int:
        """Retire every dead cut of the attempt, then destroy the live one (in-session-runtime.md
        section 28.3): each dead cut is reaped, removed with its admin and host folders where it
        can be (isolation.retire) and recorded as `research-worktree` action `retire` with the
        paths `left` to the sweep (or the `refused` reason); then the live cut is destroyed and
        recorded `destroy`.
        The readers of the live tree ignore `retire`."""
        from alpaca.research import isolation
        from alpaca.research.types import WorktreeInfo
        wt = self.worktree(conn)
        if wt is None:
            return vc.PASS
        live = os.path.abspath(wt.path)
        done = {os.path.abspath(_abs(self.root, ev["data"].get("path")) or "")
                for ev in _events(conn, events.WORKTREE, self.task)
                if ev["data"].get("action") == "retire"}
        for ev in self._cut_events(conn):
            d = ev["data"]
            path = os.path.abspath(_abs(self.root, d.get("path")) or "")
            if path == live or path in done:
                continue
            done.add(path)
            dead = WorktreeInfo(path=path, label=self.task, commit=d.get("commit") or "",
                                admin_dir=_abs(self.root, d.get("admin_dir")),
                                private_objects=_abs(self.root, d.get("private_objects")),
                                index=_abs(self.root, d.get("index")),
                                host_git=_abs(self.root, d.get("host_git")))
            got = self._reap_cut(conn, self._cut_event_of(conn, path))
            # no `error` key: a WORKTREE event with one reads as the attempt's broken verdict
            # (verdict_event_id); a cut that cannot be removed now is `left` to the sweep
            refused, left = None, []
            try:
                existed, left = isolation.retire(self.root, self.run, self.task, self.ctx.clone,
                                                 recorded=dead)
            except IsolationError as exc:
                existed, refused = None, exc.reason
            _emit(conn, self.ctx, events.WORKTREE, self.task, data={
                "action": "retire", "path": _rel(self.root, path), "cut": _cut_number(d),
                "existed": existed, "left": [_rel(self.root, p) for p in left],
                "refused": refused, "reaped": list(got.get("killed") or [])})
        existed = any(p and os.path.lexists(p) for p in (wt.path, wt.admin_dir, wt.host_git))
        if existed:
            isolation.destroy_worktree(self.ctx.clone, wt)
        self.record_tree(conn, "destroy", wt, existed=existed)
        return vc.PASS

    def seal(self, conn) -> int:
        from alpaca.research import proofs
        proofs.seal(conn, self.root, self.task, session=self.ctx.session)
        return vc.PASS

    # ---------------------------------------------------------------- the close plan
    def verify(self, conn) -> int:
        if _status(conn, self.task) in _SETTLED:
            return vc.PASS
        return verify_task(conn, self.ctx, self.task)

    def close(self, conn) -> int:
        from alpaca.research import proofs, verify_gate
        if _status(conn, self.task) in _SETTLED:
            return vc.PASS
        st = verify_gate.state(conn, self.root, self.task)
        outcome = st.get("outcome") or st.get("state")
        if outcome == "verified":
            return proofs.close(conn, self.root, self.task, session=self.ctx.session)
        if outcome in ("refuted", "unverified"):
            return proofs.close(conn, self.root, self.task, session=self.ctx.session,
                                to="blocked", reason="proof %s" % outcome)
        raise RuntimeError("%s has no verification end to close on (state %s)"
                           % (self.task, st.get("state")))


def _metric_dict(values: dict) -> dict:
    return {mid: {"median": m.median, "repeats": list(m.repeats), "n_ok": m.n_ok,
                  "spread": m.spread, "untested": m.untested} for mid, m in values.items()}


def attempt_record(conn, ctx: RunContext, task: str, *, outcome: str | None = None,
                   reason: str | None = None) -> tuple:
    """(code, data) of the task's research-attempt (section 8.3): its key, round, slot, mode,
    outcome, broken class, reason, category, forked_from, head, diff (never null: an attempt
    with no research-diff gets the zero-byte attempt.diff of an empty capture), the gates (the
    newest attempt-side result of each chain item; one the stop signal killed is listed with
    `stopped` true), the tamper points, the accept and audit events, the metrics, the champion's
    metrics, the launches, tokens, cost, priced and the Alpaca version. The `record` step and the
    stop handler (row 57, which passes outcome `abandoned` and reason `stopped`) both build it
    here, so no attempt is recorded without its gates and verdicts (FR-039)."""
    from alpaca import version
    from alpaca.research.types import VERDICT_OF
    a = _Attempt(ctx, task)
    v = a.verdict(conn)
    research = a.research(conn)
    if outcome is not None:
        broken_class = None
        why = reason
    else:
        outcome = v["outcome"] or "abandoned"
        broken_class, why = v["broken_class"], v["reason"]
    diffs = a.diffs(conn)
    dd = diffs[0]["data"] if diffs else {}
    if dd:
        diff = {"path": dd.get("path"), "sha256": dd.get("sha256"),
                "bytes": dd.get("bytes") or 0, "empty": bool(dd.get("empty"))}
    else:
        folder = a.attempt_dir()
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "attempt.diff")
        if not os.path.lexists(path):
            with open(path, "wb"):
                pass
        size = os.path.getsize(path) if os.path.isfile(path) else 0
        diff = {"path": _rel(ctx.root, path), "sha256": _sha_file(path), "bytes": size,
                "empty": size == 0}
    gates_, where = [], {}
    for ev in _events(conn, events.GATE, task):
        d = ev["data"]
        if d.get("side") not in (None, "attempt"):
            continue
        entry = {"stage": d.get("stage"), "verdict": d.get("verdict")}
        if d.get("stopped"):
            entry["stopped"] = True
            if d.get("stage") in where:          # an earlier real result stands
                continue
        if d.get("stage") in where:
            gates_[where[d.get("stage")]] = entry
        else:
            where[d.get("stage")] = len(gates_)
            gates_.append(entry)
    tampers = _events(conn, events.TAMPER, task)
    accepts = _events(conn, events.ACCEPT, task)
    audits = _events(conn, events.AUDIT_VERDICT, task)
    ends = _events(conn, events.LAUNCH_END, task)
    tokens = sum(int(e["data"].get("tokens") or 0) for e in ends)
    costs = [e["data"].get("cost_usd") for e in ends]
    priced = all(e["data"].get("priced") is not False for e in ends)
    forked = dd.get("forked_from") or a.opened(conn)["champion_commit"]
    data = {"key": research.get("key"), "round": research.get("round"),
            "slot": research.get("slot"), "mode": research.get("mode"), "outcome": outcome,
            "broken_class": broken_class, "reason": why, "category": research.get("category"),
            "forked_from": forked, "head": dd.get("head") or forked, "diff": diff,
            "gates": gates_,
            "tamper": {"verdict": "FAIL" if any(t["data"].get("verdict") not in (None, "PASS")
                                                for t in tampers) else "PASS",
                       "points": [t["data"].get("point") for t in tampers]},
            "accept_event": accepts[-1]["id"] if accepts else None,
            "audit_event": audits[-1]["id"] if audits else None,
            "metrics": _metric_dict(a.metrics(conn)),
            "champion_metrics": _metric_dict(a.champion_values(conn)),
            "launches": len(_events(conn, events.LAUNCH, task)), "tokens": tokens,
            "cost_usd": round(sum(float(c) for c in costs if c is not None), 6),
            "priced": priced, "alpaca_version": version.string()}
    word = VERDICT_OF.get(outcome)
    code = vc.PASS if word in ("accepted", "baseline") else \
        vc.BLOCKED if word == "abandoned" else vc.FAIL
    return code, data


def champion_metrics(conn, ctx: RunContext, round_n: int, line: str | None) -> dict:
    """The champion's recorded values at the start of round `round_n` of the line: the
    champion_metrics of the line's newest research-round before it, as MetricValue."""
    from alpaca.research.types import MetricValue
    best = None
    for ev in _events(conn, events.ROUND, ctx.run):
        d = ev["data"]
        if (d.get("repair_task") or None) == (line or None) and int(d.get("round", -1)) < round_n:
            best = d
    out = {}
    for mid, m in ((best or {}).get("champion_metrics") or {}).items():
        if isinstance(m, dict):
            out[mid] = MetricValue(metric=mid, median=m.get("median"),
                                   repeats=tuple(m.get("repeats") or ()),
                                   n_ok=int(m.get("n_ok") or 0), spread=m.get("spread"),
                                   untested=bool(m.get("untested")))
    return out


def verify_task(conn, ctx: RunContext, task: str) -> int:
    """Run or resume the verification of the task's latest seal (verifier.run) and handle its
    result (section 7.3): an end goes on (PASS); a first VERIFY-RUNTIME-MISMATCH stop re-seals
    and verifies again; any other stop, and `exhausted`, moves the task to blocked with
    "verification stopped: <code>" (PASS); `paused` opens the run's budget hold (PAUSED)."""
    from alpaca import proof
    from alpaca.research import proofs, verifier
    from alpaca.research import session_runtime
    launch = reserving(ctx, _hooks.get("verify_launch") or session_runtime.launch_for(ctx))
    for _ in range(3):
        # the pack's retry count and verifier timeout bound every verifier try (FR-034, FR-042)
        got = verifier.run(conn, ctx.root, task, runtime=ctx.runtime,
                           driver="loop-runner:%s" % ctx.run, session=ctx.session,
                           launch=launch, contract=ctx.contract,
                           retries=int(ctx.contract.infra_retries),
                           timeout_s=int(ctx.contract.verifier_timeout_s),
                           session_mode=bool(getattr(ctx, "pinned", False)))
        state = got.get("state")
        if state == "halted":
            return vc.PAUSED         # the run is ending: the verification resumes where it was
        if state == "paused":
            _budget_hold(conn, ctx, "a verifier launch was held by the budget check")
            return vc.PAUSED
        if state in ("verified", "refuted", "unverified", "superseded"):
            return vc.PASS
        code = got.get("code")
        if state == "stopped" and code == "VERIFY-RUNTIME-MISMATCH":
            stops = [ev for ev in _events(conn, events.VERIFY_REFUSED, task)
                     if ev["data"].get("code") == "VERIFY-RUNTIME-MISMATCH"]
            if len(stops) <= 1:
                proof.seal(conn, ctx.root, task, session=ctx.session)
                continue
        if state in ("stopped", "exhausted"):
            proofs.close(conn, ctx.root, task, session=ctx.session, to="blocked",
                         reason="verification stopped: %s" % (code or "VERIFY-EXHAUSTED"))
            return vc.PASS
        raise RuntimeError("the verification of %s ended in state %r" % (task, state))
    raise RuntimeError("the verification of %s did not settle" % task)


# ======================================================================== the round steps
class _Round:
    def __init__(self, ctx: RunContext, round_n: int, repair_task: str | None):
        self.ctx = ctx
        self.round_n = int(round_n)
        self.line = repair_task

    @property
    def run(self):
        return self.ctx.run

    def keys(self, conn) -> list:
        """The attempt keys of this round, fixed by (run, round, slot) before any slot starts."""
        if self.round_n == 0 and self.line is None:
            return [paths.attempt_key(self.run, 0, 0)]
        index = None
        if self.line is not None:
            index = _research(conn, self.line).get("index")
        return [paths.attempt_key(self.run, self.round_n, s, index)
                for s in range(int(self.ctx.contract.slots))]

    def tasks(self, conn) -> list:
        have = run_keys(conn, self.run)
        return [have[k] for k in self.keys(conn) if k in have]

    def open(self, conn) -> int:
        from alpaca import batch, ops, taskcontract
        from alpaca.research import isolation
        have = run_keys(conn, self.run)
        contract = self.ctx.contract
        categories = tuple(contract.categories or ())
        for slot, key in enumerate(self.keys(conn)):
            if key in have:
                continue
            category = categories[slot % len(categories)].id if categories else None
            research = {"run": self.run, "role": "attempt", "attempt": key, "key": key,
                        "baseline": self.round_n == 0, "round": self.round_n, "slot": slot,
                        "mode": contract.mode, "category": category, "repair_task": self.line}
            title = "%s r%d s%d %s attempt" % (self.run, self.round_n, slot, contract.mode)
            statement = ("Research run %s, round %d, slot %d: %s" % (
                self.run, self.round_n, slot,
                "measure the untouched champion (the baseline)" if self.round_n == 0 else
                "one attempt: a proposal, its implementation, the gates, the accept rule, the "
                "audit and the notes, recorded as one attempt event"))
            tid = ops.add_task(conn, self.ctx.op, statement, title[:60],
                               phase=batch.ladder_phase(conn, self.ctx.op, "build"),
                               why="one attempt is one task (FR-039)", session=self.ctx.session,
                               actor=ACTOR, extra={"research": research},
                               mapping={"group": self.run, "after": [], "resources": []})
            have[key] = tid
        latest = taskcontract.latest(conn)
        opened = _round_open(conn, self.run, self.round_n, self.line)
        clone = isolation.open_clone(self.ctx.root, self.run, line=self.line or "champion")
        if clone is None:
            raise RuntimeError("the clone of line %s is missing" % (self.line or "champion"))
        self.ctx.clone = clone
        commit = opened["data"]["champion_commit"] if opened else clone.head
        for key in self.keys(conn):
            tid = have[key]
            if tid in latest:
                continue
            taskcontract.record(conn, tid, _attempt_contract(
                self.ctx, key, commit, _research(conn, tid).get("category")),
                session=self.ctx.session, actor=ACTOR)
        if opened is None:
            _emit(conn, self.ctx, events.ROUND_OPEN, self.run,
                  data={"round": self.round_n, "repair_task": self.line,
                        "champion_commit": commit, "keys": self.keys(conn),
                        "tasks": [have[k] for k in self.keys(conn)]})
        return vc.PASS

    def champion_commit(self, conn) -> str:
        ev = _round_open(conn, self.run, self.round_n, self.line)
        return ev["data"]["champion_commit"]

    def champion(self, conn) -> int:
        from alpaca.research import isolation
        if self.ctx.clone is None or self.ctx.clone.line != (self.line or "champion"):
            self.ctx.clone = isolation.open_clone(self.ctx.root, self.run,
                                                  line=self.line or "champion")
        isolation.champion_checkout(self.ctx.root, self.run, self.round_n, self.ctx.clone,
                                    commit=self.champion_commit(conn))
        return vc.PASS

    def slots(self, conn) -> int:
        from alpaca.formation import controller
        tasks = self.tasks(conn)
        barrier = RoundBarrier(tasks)
        self.ctx.barrier = barrier
        codes = {}

        def work(task):
            code = vc.BLOCKED
            try:
                own = db.connect(self.ctx.root)
                try:
                    code = controller.run(own, attempt_plan(self.ctx, task), root=self.ctx.root,
                                          session=self.ctx.session,
                                          tmp_prefixes=self.ctx.tmp_prefixes)
                finally:
                    own.close()
            finally:
                codes[task] = code
                barrier.arrive(task)
        threads = [threading.Thread(target=work, args=(t,), name="slot-%s" % t, daemon=True)
                   for t in tasks]
        for th in threads:
            th.start()
        for th in threads:
            _join(th)
        got = [codes.get(t, vc.BLOCKED) for t in tasks]
        if any(c == vc.PAUSED for c in got):
            return vc.PAUSED
        if any(c != vc.PASS for c in got):
            return vc.BLOCKED
        return vc.PASS

    def ranking(self, conn):
        """(winner, ranked, reason) of accept.pick_winner over the round's attempts."""
        from alpaca.research import accept, state
        from alpaca.research.types import AcceptDecision, Candidate
        cands = []
        for task in self.tasks(conn):
            a = _Attempt(self.ctx, task)
            acc = _events(conn, events.ACCEPT, task)
            v = state.attempt_verdict(conn, task)
            if acc and v["verdict"] == "accepted":
                d = acc[-1]["data"]
                dec = AcceptDecision(accepted=bool(d.get("accepted")), outcome=d.get("outcome"),
                                     phase=d.get("phase"), reason=d.get("reason") or "",
                                     score=d.get("score"), champion_score=d.get("champion_score"),
                                     untested=bool(d.get("untested")),
                                     repeats_failed=bool(d.get("repeats_failed")),
                                     margin=float(d.get("margin") or 0.0))
            else:
                dec = AcceptDecision(accepted=False, outcome=v["outcome"] or "abandoned",
                                     phase="strict", reason=v["reason"] or "not accepted")
            r = a.research(conn)
            cands.append(Candidate(task=task, round=self.round_n, slot=int(r.get("slot") or 0),
                                   metrics=a.metrics(conn), decision=dec))
        if not cands:
            return None, [], "no attempt"
        return accept.pick_winner(cands, self.ctx.contract)

    def accepted(self, conn) -> list:
        winner, ranked, _reason = self.ranking(conn)
        from alpaca.research import state
        out = []
        for r in ranked:
            if state.attempt_verdict(conn, r.task)["verdict"] == "accepted":
                out.append(r.task)
        return out

    def rank(self, conn) -> int:
        self.ranking(conn)          # pure: recomputed by candidates, merge and summary
        return vc.PASS

    def candidates(self, conn) -> int:
        for task in self.accepted(conn):
            if _status(conn, task) in _SETTLED:
                continue
            code = verify_task(conn, self.ctx, task)
            if code != vc.PASS:
                return code
        return vc.PASS

    def merged(self, conn) -> dict | None:
        for ev in reversed(_events(conn, events.MERGE, self.run)):
            d = ev["data"]
            if d.get("round") == self.round_n and \
                    (d.get("repair_task") or None) == (self.line or None):
                return ev
        return None

    def merge(self, conn) -> int:
        from alpaca.research import isolation, verdicts, verifier
        if self.merged(conn) is not None:
            return vc.PASS
        parent = self.champion_commit(conn)
        for task in self.accepted(conn):
            a = _Attempt(self.ctx, task)
            diffs = a.diffs(conn)
            if len(diffs) != 1:
                continue
            cap = a.capture_of(diffs[0])
            try:
                verifier.require_verified(conn, self.ctx.root, task, diff_sha256=cap.sha256)
            except verdicts.VerifyError:
                continue
            head = isolation.merge_champion(self.ctx.clone, branch=self.ctx.clone.branch,
                                            parent=parent, capture=cap, message=cap.message,
                                            stamp=cap.stamp)
            _emit(conn, self.ctx, events.MERGE, self.run,
                  data={"round": self.round_n, "repair_task": self.line, "attempt": task,
                        "commit": head, "parent": parent,
                        "research_branch": self.ctx.clone.branch})
            self.ctx.clone = isolation.open_clone(self.ctx.root, self.run,
                                                  line=self.line or "champion")
            return vc.PASS
        return vc.PASS

    def summary(self, conn) -> int:
        from alpaca.posture import loops
        from alpaca.research import accept, isolation, state
        if _round_done(conn, self.run, self.round_n, self.line) is not None:
            return vc.PASS
        winner, ranked, reason = self.ranking(conn)
        merged = self.merged(conn)
        clone = isolation.open_clone(self.ctx.root, self.run, line=self.line or "champion")
        champ = {mid: {"median": m.median, "repeats": list(m.repeats), "n_ok": m.n_ok,
                       "spread": m.spread, "untested": m.untested}
                 for mid, m in champion_metrics(conn, self.ctx, self.round_n, self.line).items()}
        won = None
        if self.round_n == 0:
            tasks = self.tasks(conn)
            if tasks and state.attempt_verdict(conn, tasks[0])["verdict"] == "baseline":
                won = tasks[0]
        elif merged is not None:
            won = merged["data"].get("attempt")
        if won is not None:
            att = _events(conn, events.ATTEMPT, won)
            if att:
                champ = att[-1]["data"].get("metrics") or {}
        values = {mid: m.get("median") for mid, m in champ.items() if isinstance(m, dict)}
        met = bool(values) and all(v is not None for v in values.values()) \
            and bool(tuple(self.ctx.contract.targets)) \
            and accept.targets_met(values, self.ctx.contract)
        from alpaca.research import verify_gate
        rows = []
        for r in ranked:
            st = verify_gate.state(conn, self.ctx.root, r.task)
            rows.append({"attempt": r.task, "rank": r.rank, "key": [str(k) for k in r.key],
                         "accepted": state.attempt_verdict(conn, r.task)["verdict"] == "accepted",
                         "verify": st.get("outcome") or st.get("state")})
        accepted = sum(1 for row in rows if row["accepted"])
        with db.transaction(conn):
            _emit(conn, self.ctx, events.ROUND, self.run, code=vc.PASS, in_txn=True,
                  data={"round": self.round_n, "repair_task": self.line,
                        "winner": merged["data"].get("attempt") if merged else None,
                        "ranking": rows, "reason": reason,
                        "champion_commit": clone.head if clone else None,
                        "champion_metrics": champ if (won is not None or self.round_n > 0)
                        else {}, "accepted": accepted, "targets_met": met})
            if self.round_n > 0 and not met:
                loops.attempt(conn, self.run, "research-extension", verdict="targets-unmet",
                              detail=("%s r%d" % (self.line, self.round_n)) if self.line
                              else "r%d" % self.round_n, session=self.ctx.session, actor=ACTOR)
        return vc.PASS

    def handoff(self, conn) -> int:
        from alpaca.research import isolation
        path = paths.champion_dir(self.ctx.root, self.run, self.round_n, self.line or "champion")
        if os.path.lexists(path) and self.ctx.clone is not None:
            wt = isolation.tree_info(path, commit=self.champion_commit(conn),
                                     label="r%d-champion" % self.round_n, admin=True,
                                     read_only=True)
            isolation.destroy_worktree(self.ctx.clone, wt)
        pool = _POOLS.get(self.run)
        for task in self.tasks(conn):
            if _status(conn, task) in _SETTLED:
                continue
            if pool is not None:
                pool.put(task)
            else:
                code = run_close(conn, self.ctx, task)
                if code == vc.PAUSED:
                    return vc.PAUSED
        return vc.PASS


def _attempt_contract(ctx: RunContext, key: str, commit: str, category=None) -> dict:
    """The task-contract of an attempt task (architecture.md section 8.2)."""
    from alpaca.research import verify_gate
    return {"input": ["the champion commit %s" % commit,
                      "the frozen contract %s" % ctx.contract.sha256,
                      "the category %s" % (category or "none"),
                      "the attempt key %s" % key],
            "expected": ["the attempt diff and its research-attempt event"],
            "done_bar": ["the gates, tamper checks, accept rule, audit and notes ran and are "
                         "recorded", verify_gate.SELF_LINES[1]],
            "fail_cases": ["an attempt recorded without its gates and verdicts (FR-039)"]}


def run_close(conn, ctx: RunContext, task: str) -> int:
    from alpaca.formation import controller
    return controller.run(conn, close_plan(ctx, task), root=ctx.root, session=ctx.session,
                          tmp_prefixes=ctx.tmp_prefixes)


# ======================================================================== the verifier pool
class VerifierPool:
    """K worker threads that run close plans (verify, close) while the next round works
    (section 4.3). Each worker opens its own record connection. A close plan that returns
    PAUSED stops the pool from taking more work; stop() ends the workers once the queue they
    took is done, and drain() waits for every queued plan."""

    def __init__(self, ctx: RunContext, workers: int):
        self.ctx = ctx
        self.q = queue.Queue()
        self.seen = set()
        self.lock = threading.Lock()
        self.paused = threading.Event()
        self.closed = False
        self.codes = {}
        self.threads = [threading.Thread(target=self._work, name="verify-%d" % i, daemon=True)
                        for i in range(max(1, int(workers)))]
        for th in self.threads:
            th.start()

    def put(self, task: str) -> None:
        with self.lock:
            if task in self.seen or self.closed:
                return
            self.seen.add(task)
        self.q.put(task)

    def _work(self):
        while True:
            task = self.q.get()
            try:
                if task is None:
                    return
                if self.paused.is_set() or (self.ctx.stop is not None and self.ctx.stop.is_set()):
                    continue
                code = vc.BLOCKED
                try:
                    own = db.connect(self.ctx.root)
                    try:
                        code = run_close(own, self.ctx, task)
                    finally:
                        own.close()
                finally:
                    self.codes[task] = code
                    if code == vc.PAUSED:
                        self.paused.set()
            finally:
                self.q.task_done()

    def drain(self) -> None:
        """Queue.join in slices, for the reason _join gives."""
        with self.q.all_tasks_done:
            while self.q.unfinished_tasks:
                self.q.all_tasks_done.wait(_JOIN_SLICE_S)

    def stop(self, *, drain: bool = True) -> None:
        if drain:
            self.drain()
        with self.lock:
            self.closed = True
        for _ in self.threads:
            self.q.put(None)
        for th in self.threads:          # a running verification ends on its own launch timeouts
            _join(th)


# ======================================================================== the plans
def round_plan(ctx: RunContext, round_n: int, *, repair_task: str | None = None) -> dict:
    """Plan id "<run>:r<N>" for an improve round (round 0 included), or "<run>:repair:<i>:r<N>"
    for fix round N of repair task i (its key <run>:repair:<i>), so the controller's resume key
    (plan id, step name) is never shared between repair tasks. repair_task is None in an improve
    run; round_n 0 exists only there."""
    if repair_task is None:
        plan_id = "%s:r%d" % (ctx.run, int(round_n))
    else:
        from alpaca.research import state
        conn = db.connect(ctx.root)
        try:
            index = state._research(conn, repair_task).get("index")
        finally:
            conn.close()
        plan_id = "%s:repair:%s:r%d" % (ctx.run, index, int(round_n))
    r = _Round(ctx, round_n, repair_task)
    steps = {"open": r.open, "champion": r.champion, "slots": r.slots, "rank": r.rank,
             "candidates": r.candidates, "merge": r.merge, "summary": r.summary,
             "handoff": r.handoff}
    return _checked(ctx, {"id": plan_id, "steps": [{"name": n, "run": steps[n]}
                                                    for n in ROUND_STEPS]})


def attempt_plan(ctx: RunContext, task: str) -> dict:
    """Plan id <task>: claim, open-window, propose, check-proposer, isolate, implement,
    check-implementer, capture, purge, gates, judge, audit, notes, close-window, record,
    teardown, seal; the round 0 baseline has no propose, check, implement, audit or notes step
    (section 7.4)."""
    a = _Attempt(ctx, task)
    conn = db.connect(ctx.root)
    try:
        baseline = bool(_research(conn, task).get("baseline"))
    finally:
        conn.close()
    steps = {"claim": a.claim, "open-window": a.open_window, "propose": a.propose,
             "check-proposer": a.check_proposer, "isolate": a.isolate, "implement": a.implement,
             "check-implementer": a.check_implementer, "capture": a.capture, "purge": a.purge,
             "gates": a.gates, "judge": a.judge, "audit": a.audit, "notes": a.notes,
             "close-window": a.close_window, "record": a.record, "teardown": a.teardown,
             "seal": a.seal}
    names = BASELINE_STEPS if baseline else ATTEMPT_STEPS
    return _checked(ctx, {"id": task, "steps": [{"name": n, "run": steps[n]} for n in names]})


def close_plan(ctx: RunContext, task: str) -> dict:
    """Plan id <task>:close: verify (verifier.run, and what section 7.3 says to do with each
    result), then close (the task moves to done through the done gate after a verified end, or
    to blocked with "proof refuted" or "proof unverified")."""
    a = _Attempt(ctx, task)
    return _checked(ctx, {"id": "%s:close" % task, "steps": [{"name": "verify", "run": a.verify},
                                                              {"name": "close", "run": a.close}]})


def _checked(ctx: RunContext, plan: dict) -> dict:
    """A plan whose every step starts with the stat pass of the engine pin (in-session-runtime.md
    section 12.5) when the run is a session run; a test run's plan comes back as it is."""
    if getattr(ctx, "pinned", False):
        from alpaca.research import session_runtime
        return session_runtime.engine_checked(ctx, plan)
    return plan
