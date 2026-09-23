"""The controller sequencer with crash-only replay from events (M3.8).

Ported from the earlier harness `ops/controller.py` campaign SEQUENCER, MINUS the two-human phase
sign-off, and adapted to Alpaca: the state store is the
Alpaca record itself, not a private `war-log.jsonl`.

WHY A PROGRAM, NOT A PROMPT. The thing that decides WHEN each step runs is a program, not an agent
reading doctrine. The controller:

  1. enforces WORKSPACE ISOLATION first (`alpaca.gates.workspace_guard`, ported in M1.4): a foreign or
     scratch/tmp root BLOCKS before any step runs -- the same "no step on a bad workspace" floor the
     The earlier harness controller opened with;
  2. reads a declared, ordered STEP PLAN (each step has a unique name and a callable);
  3. reads the append-only RECORD -- the SOLE state store -- to recover, CRASH-ONLY, which steps
     already reached PASS, so a re-run resumes at the first non-done step and never re-executes a
     completed one (`completed_steps`, keyed on the plan id + step name, exactly as the earlier harness
     controller keyed resume on the war-log);
  4. for each not-done step, runs it, reads its verdict-band code through the M1.3 contract (never a
     scraped label), reflects the result into the record as one `controller-step` event, and ADVANCES
     on PASS or HALTS fail-closed on anything else.

Every step is reflected into the record (Step 2): one hash-chained `controller-step` event per run,
so the resume above reads the same store a peeking session reads, and a controller killed mid-plan
replays from those events to the same completed set.

THE TWO-HUMAN SIGN-OFF IS REMOVED (the plan's adaptation of ADAPT row 84). Alpaca carries phase-boundary
decisions as recorded human decision rows elsewhere (M3.5 review cards, the ship boundary); this
sequencer therefore never blocks on two typed signer lines and never auto-signs -- there is no
`phase_boundary`/`signoff` path here at all.

A STEP is a callable. `{"name": str, "run": callable(conn) -> int}` where the callable returns a
verdict-band code (PASS advances, anything else halts). A step that raises is an inability to run and
BLOCKS fail-closed -- surfaced through the contract, never an escaping traceback.
"""
from __future__ import annotations

from alpaca import db, paths
from alpaca.gates import verdict as vc
from alpaca.gates import workspace_guard

#: the event kind each step result lands as. The plan id + step name are the resume key.
STEP_KIND = "controller-step"
#: a plan or workspace precondition that refused before any step ran.
BLOCK_KIND = "controller-blocked"

DEFAULT_PLAN_ID = "campaign"


class PlanError(Exception):
    """A malformed or empty plan -- never guessed around, always surfaced as a clean BLOCKED."""


def _plan_id(plan) -> str:
    return str(plan.get("id") or plan.get("op") or DEFAULT_PLAN_ID)


def _load_plan(plan) -> list:
    """Read and validate the ordered step plan. An empty plan, a step with no name, a duplicate
    name (resume keys on the name), or a step with no callable `run` raises PlanError."""
    if not isinstance(plan, dict):
        raise PlanError("a plan is a dict with a 'steps' list")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise PlanError("plan carries no non-empty 'steps' list")
    seen, out = set(), []
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            raise PlanError("step %d is not a mapping" % i)
        name = s.get("name")
        if not name or not isinstance(name, str):
            raise PlanError("step %d has no string 'name'" % i)
        if name in seen:
            raise PlanError("duplicate step name %r (resume keys on the name, so it must be unique)"
                            % name)
        seen.add(name)
        run = s.get("run")
        if not callable(run):
            raise PlanError("step %r has no callable 'run'" % name)
        out.append({"name": name, "run": run})
    return out


def completed_steps(conn, plan_id) -> set:
    """The set of step names that reached PASS for `plan_id`, read CRASH-ONLY from the record (the
    SOLE state store). Resume keys on it, so a re-run skips exactly these."""
    done = set()
    for e in db.events(conn, kind=STEP_KIND, limit=10 ** 9):
        d = e["data"]
        if d.get("plan") == str(plan_id) and d.get("verdict") == vc.PASS \
                and isinstance(d.get("step"), str):
            done.add(d["step"])
    return done


def _record(conn, plan_id, name, index, code, *, session, clock=None, detail=None):
    data = {"plan": str(plan_id), "step": name, "index": index, "verdict": int(code),
            "verdict_name": vc.name_of(code)}
    if detail is not None:
        data["detail"] = str(detail)
    return db.append_event(conn, session=session, actor="controller", kind=STEP_KIND,
                           ref="%s:%s" % (plan_id, name), data=data, clock=clock)


def run(conn, plan, *, root=None, session="controller", clock=None, tmp_prefixes=None) -> int:
    """Drive `plan` to completion or the first non-PASS halt, resuming crash-only from the record.

    Returns a verdict-band code, verdict-preserving at the campaign boundary:
      * PASS    -- every step reached PASS.
      * BLOCKED -- the workspace precondition refused, the plan is unusable, or a step could not be
                   run (it raised) -- block on inability to run, never a guessed pass.
      * FAIL / PAUSED / other -- a step returned that band; the campaign surfaces it and halts, later
                   steps unrun. A non-PASS is never recorded as PASS, so resume never launders it.

    The workspace guard runs FIRST: a foreign or scratch/tmp `root` BLOCKS before any step runs.
    Completed steps are read from the record and skipped, so a re-run never re-executes them.
    `tmp_prefixes` flows to the guard so a test can isolate the containment property from the
    no-tmp property with an empty set (a pytest root lives under /tmp), exactly as the M1.15 doors do.
    """
    # (1) workspace isolation precondition -- FIRST, before any step.
    try:
        base = root if root is not None else paths.root()
    except Exception as e:
        base = None
        findings = [("ROOT-ABSENT", str(root), str(e))]
    else:
        findings = workspace_guard.check(base, [], tmp_prefixes=tmp_prefixes)
    if findings:
        db.append_event(conn, session=session, actor="controller", kind=BLOCK_KIND,
                        data={"plan": _plan_id(plan), "reason": "workspace",
                              "findings": [list(f) for f in findings]}, clock=clock)
        return vc.BLOCKED

    # (2) the declared plan.
    try:
        steps = _load_plan(plan)
    except PlanError as e:
        db.append_event(conn, session=session, actor="controller", kind=BLOCK_KIND,
                        data={"plan": _plan_id(plan), "reason": "plan", "detail": str(e)},
                        clock=clock)
        return vc.BLOCKED

    plan_id = _plan_id(plan)
    # (3) crash-only resume: which steps already reached PASS.
    done = completed_steps(conn, plan_id)

    # (4) sequence: run first-non-done, reflect into the record, advance on PASS / halt otherwise.
    for idx, step in enumerate(steps):
        name = step["name"]
        if name in done:
            continue
        try:
            code = step["run"](conn)
        except BaseException as e:
            # an inability to RUN the step is a precondition failure, recorded non-PASS (so resume
            # re-runs it, never laundering it to PASS) and halted fail-closed -- never a traceback.
            _record(conn, plan_id, name, idx, vc.BLOCKED, session=session, clock=clock,
                    detail="step raised: %s: %s" % (type(e).__name__, e))
            return vc.BLOCKED
        try:
            code = int(code)
        except (TypeError, ValueError):
            _record(conn, plan_id, name, idx, vc.BLOCKED, session=session, clock=clock,
                    detail="step returned a non-code %r" % (code,))
            return vc.BLOCKED
        _record(conn, plan_id, name, idx, code, session=session, clock=clock)
        if code == vc.PASS:
            continue
        return code
    return vc.PASS
