"""Shared hook plumbing. Every hook is fail-open: on any error print nothing, exit 0.

The failure and cost contract (M1.5) lives here so all five hooks inherit it:

- `read_stdin(timeout_s)` is non-blocking. It waits at most `timeout_s` for the caller
  to deliver a payload and reads at most `MAX_STDIN_BYTES`, so a wedged caller that
  opens the pipe and never closes it (or floods it) cannot stall a session. A well-formed
  body parses; an empty body is `{}`; a malformed body raises, and `fail_open` turns that
  into a clean exit 0.
- `fail_open(fn)` swallows every exception, prints nothing on failure, and exits 0. It
  also arms a hard wall-clock deadline (SIGALRM) so a handler that wedges past the
  deadline is cut off and still exits 0, and it records the handler's own duration so the
  analytics can show hook cost per session. A signal reaches the handler only between two
  bytecodes, and a call asleep in the kernel holds it off until the call returns (t-070 A-4:
  60 s), so the wiki capture does not rely on it: `capture_step` runs it in a child process
  that the capture runner stops from outside (alpaca/capture_runner.py). The hooks' other
  steps that touch files the hook does not bound (pad, data push, hub tile, transcript
  snapshot, analytics, profile refresh, consolidation) run the same way, through `run_step`
  (alpaca/hook_steps.py, t-070 RC5): a step stopped at its share of the deadline leaves its
  own failure record where it has one and turns the hook's outcome into `timeout`.
"""
import json, os, sys, functools, select, signal, time
from alpaca import paths, util

STDIN_DEADLINE_S = 2.0                     # how long read_stdin waits for a wedged caller
HOOK_DEADLINE_S = float(os.environ.get("ALPACA_HOOK_DEADLINE_S") or 8)  # hard ceiling per hook
MAX_STDIN_BYTES = 4 * 1024 * 1024          # cap the read so an oversized flood cannot blow memory or time

_STDIN_STASH = {}                          # last payload parsed, for the cost recorder in fail_open
_WINDOW = {}                               # end (time.monotonic) of the running hook's hard deadline
_STEP_FAULTS = []                          # (step, status, error type) of this hook run's steps (`run_step`)


class _Deadline(BaseException):
    """Raised by the SIGALRM handler when a hook body runs past the hard deadline.

    A BaseException, like KeyboardInterrupt: a handler's ordinary `except Exception` must not
    catch it and record the cut-off as its own domain error. fail_open catches it at the top."""


def _drain_stdin(timeout_s: float) -> str:
    """Read whatever is on stdin within timeout_s and up to MAX_STDIN_BYTES, then stop.

    Returns the decoded text (possibly partial or empty). Never blocks past timeout_s and
    never raises: a fd with no select support, a read error or a wedged writer all just end
    the drain with what was collected so far.
    """
    try:
        fd = sys.stdin.fileno()
    except (OSError, ValueError, AttributeError):
        try:
            return (sys.stdin.read(MAX_STDIN_BYTES) or "")
        except Exception:
            return ""
    chunks, total = [], 0
    deadline = time.monotonic() + timeout_s
    while total < MAX_STDIN_BYTES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            ready, _, _ = select.select([fd], [], [], remaining)
        except (OSError, ValueError):
            break
        if not ready:
            break
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:                       # EOF: the caller closed the pipe
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks).decode("utf-8", "replace")


def read_stdin(timeout_s: float = STDIN_DEADLINE_S) -> dict:
    """Parse the hook payload without letting a wedged caller stall the session.

    Empty input is `{}`. Malformed input raises (fail_open converts it to exit 0). The
    parsed payload is stashed so fail_open can attribute the recorded duration to a session.
    """
    raw = _drain_stdin(timeout_s)
    payload = json.loads(raw) if raw.strip() else {}
    if isinstance(payload, dict):
        _STDIN_STASH["payload"] = payload
    return payload

def session_of(payload: dict) -> str:
    return str(payload.get("session_id") or payload.get("sessionId") or "unknown")

def emit_context(event_name: str, text: str, user_message=None) -> None:
    """`text` goes to the agent's context; `user_message`, when given, is shown to the person
    (Claude Code's systemMessage)."""
    out = {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": text}}
    if user_message:
        out["systemMessage"] = user_message
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()

def autodrive_level(sid: str, root=None):
    """Read only project-local posture: .alpaca/config/.autodrive-level.<sid>."""
    for name in (".autodrive-level.%s" % sid, ".autodrive-level"):
        p = os.path.join(paths.config_dir(root), name)
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    v = fh.read().strip().upper()
            except OSError:
                continue
            if v.startswith("L") and v[1:2].isdigit():
                return v[:2]
    return None

REARM_EVERY = 5          # every Nth prompt re-arms the full rule text
DANGER_PCT = 85          # transcript context percentage that names the danger band
_FULL_PREFIX = "[alpaca style] do not use these words or phrases in prose (code, quotes, identifiers exempt): "
_REMINDER = "[alpaca style] avoid the banned words; run bin/alpaca status for the full list."


def ban_entries(root):
    """Preset parser from M0 Task 8: style/banned.txt plus any configured preset ban lists."""
    from alpaca import project
    cfg = project.load(root)
    entries = []
    p = os.path.join(root, "style", "banned.txt")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as fh:
            entries += [l.strip() for l in fh if l.strip() and not l.startswith("#")]
    for preset in (cfg.get("style", {}) or {}).get("presets", []) or []:
        pp = os.path.join(root, "style", "presets", "%s-ban-list.md" % preset)
        if not os.path.isfile(pp):
            continue
        section = ""
        with open(pp, encoding="utf-8") as fh:
            for l in fh:
                if l.startswith("## "):
                    section = l[3:].strip()
                    continue
                if not (section.startswith("1.") or section.startswith("2.")):
                    continue
                if l.startswith("- ") and not l.startswith(("- `", "- Avoid:", "- Instead:")):
                    entries.append(l[2:].strip())
    return entries


def ban_rules(root, full: bool) -> str:
    """The text to inject. full=True is the whole list; full=False is the short reminder.

    Returns "" when nothing is banned, so both paths stay silent on an empty style config.
    """
    entries = ban_entries(root)
    if not entries:
        return ""
    if full:
        return _FULL_PREFIX + "; ".join(entries[:600])
    return _REMINDER


def prompt_band(payload: dict) -> str:
    """Name the re-arm band for one prompt payload: compact, danger, or normal."""
    if payload.get("compacted") or payload.get("source") == "compact":
        return "compact"
    pct = payload.get("context_percent")
    try:
        if pct is not None and float(pct) >= DANGER_PCT:
            return "danger"
    except (TypeError, ValueError):
        pass
    return "normal"


def should_rearm(root, session, band) -> bool:
    """True when this prompt is a re-arm point: first prompt, every Nth, after
    compaction, or in the danger band. Read-only: the caller owns the counter."""
    from alpaca import db
    conn = db.connect(root)
    n = int(db.meta_get(conn, "prompts:%s" % session, "0") or "0")
    if n <= 1:
        return True
    if n % REARM_EVERY == 0:
        return True
    return band in ("compact", "danger")


def _arm_deadline(seconds: float) -> bool:
    """Arm a hard wall-clock deadline via SIGALRM. Returns True when armed. A hook that
    is not on the main thread, or a platform without setitimer, simply runs without the
    backstop; read_stdin's own timeout still bounds the common wedge (a stuck caller)."""
    try:
        signal.signal(signal.SIGALRM, _on_deadline)
        signal.setitimer(signal.ITIMER_REAL, max(0.01, float(seconds)))
        return True
    except (ValueError, OSError, AttributeError):
        return False


def _on_deadline(signum, frame):
    raise _Deadline()


def _disarm_deadline() -> None:
    try:
        signal.setitimer(signal.ITIMER_REAL, 0)
    except (ValueError, OSError, AttributeError):
        pass


def _record_duration(fn, start: float, outcome="ok", error_type=None) -> None:
    """Record this hook's own duration so the analytics can show hook cost per session.

    Best-effort and always swallowed: a hook that cannot reach the record still exits 0.
    The duration is kept in the `meta` table (current state, like the other per-session
    counters) rather than as an events row, so it never disturbs the event chain or the
    ordering that other hooks assert on.
    """
    try:
        payload = _STDIN_STASH.get("payload")
        if payload is None:
            return
        from alpaca import db
        root = paths.root(payload.get("cwd"))
        sid = session_of(payload)
        hook = getattr(fn, "__module__", "?").rsplit(".", 1)[-1]
        if hook == "__main__":
            # `python -m alpaca.hooks.stop` runs the module as __main__; its spec keeps the name.
            spec = getattr(sys.modules.get("__main__"), "__spec__", None)
            hook = (getattr(spec, "name", None) or "__main__").rsplit(".", 1)[-1]
        ms = round((time.monotonic() - start) * 1000.0, 1)
        from alpaca import observability
        if observability.enabled(root):
            record_outcome(payload, hook, outcome, ms, error_type)
            return
        conn = db.connect(root)
        try:
            with db.transaction(conn):
                conn.execute('INSERT INTO obs_hook_outcome (session,hook,outcome,duration_ms,recorded_at,error_type) VALUES (?,?,?,?,?,?)',
                             (sid,hook,outcome,ms,util.now_iso(),error_type))
                _update_cost(conn,sid,hook,ms)
        finally:
            conn.close()
    except Exception as exc:
        print("alpaca hook outcome unavailable: "+type(exc).__name__,file=sys.stderr)


def _update_cost(conn, sid, hook, ms):
    """Caller holds BEGIN IMMEDIATE; aggregate and outcome cannot diverge."""
    from alpaca import db
    key = "hook-cost:%s:%s" % (sid, hook)
    try:
        prior = json.loads(db.meta_get(conn,key) or '{}')
    except (TypeError,ValueError):
        prior = {}
    db.meta_set(conn,key,json.dumps({'hook':hook,'runs':int(prior.get('runs',0))+1,
        'total_ms':round(float(prior.get('total_ms',0))+ms,1),'last_ms':ms}))


def record_outcome(payload, hook, outcome, duration_ms=0, error_type=None):
    from alpaca.capture_io import append_json
    from uuid import uuid4
    from alpaca.pool import slug
    root = paths.root(payload.get('cwd'))
    sid = session_of(payload)
    append_json(os.path.join(root,'.alpaca','pool','hooks',slug(sid)+'.jsonl'),
                {'id':str(uuid4()),'type':'hook_outcome','session':sid,'hook':hook,'outcome':outcome,
                 'duration_ms':duration_ms,'error_type':error_type,'ts':util.now_iso()},root=root)


def record_failure(payload, hook, exc):
    try:
        record_outcome(payload,hook,'failed',error_type=type(exc).__name__)
    except Exception:
        # The supervisor journals this sanitized fallback when even the spool is unavailable.
        print('alpaca capture failed: '+hook+': '+type(exc).__name__,file=sys.stderr)


def capture_budget(cap_s: float, share: float = 1.0, margin_s: float = 1.0) -> float:
    """Seconds a hook may give to wiki capture (FR-8): at most `cap_s`, and at most `share` of what
    is left of the hook's hard deadline after `margin_s`, so the capture stops itself, records
    `capture-deferred` and returns before the hard deadline cuts the process. Never below half a
    second. Outside a `fail_open` hook (a test, a verb) only `cap_s` applies."""
    end = _WINDOW.get("end")
    if end is None:
        return float(cap_s)
    return max(0.5, min(float(cap_s), share * (end - time.monotonic() - margin_s)))


def hook_time_left(margin_s: float = 1.5):
    """Seconds until `margin_s` before the running hook's hard deadline, never below 0.1, or None
    outside a `fail_open` hook."""
    end = _WINDOW.get("end")
    return None if end is None else max(0.1, end - time.monotonic() - margin_s)


#: Seconds before the hook's hard deadline at which the capture runner stops the capture, which
#: leaves the hook time to record the outcome and finish (t-070 RC4).
CAPTURE_KILL_MARGIN_S = 1.0


class CaptureFailed(RuntimeError):
    """A capture run under `_strict` (the checkpoint verb) that failed; its outcome is recorded."""


def capture_step(payload, hook, *, cap_s: float, share: float = 1.0, transcripts: bool = True):
    """The capture of the Stop, PreCompact and SessionEnd hooks and the checkpoint verb (FR-8,
    OB-18), through the one capture runner (`alpaca.capture_runner`, t-070 RC4).

    The session's uncaptured events, then (with `transcripts`) its transcript, land through
    `service.capture` in a child process. Its cooperative budget is `capture_budget(cap_s, share)`
    (no document starts after it); it rolls a document back between two blocks `HARD_LEAD_S` before
    the runner's deadline; and the runner kills it `CAPTURE_KILL_MARGIN_S` before the hook's hard
    deadline (outside a hook: the budget plus `GRACE_S`), so a call that blocks in the kernel can no
    longer hold the hook past its deadline (A-4). Every way it ends leaves the same record outcome
    (`capture_runner.record`): `capture-deferred` {hook, through_event, remaining} when cut short,
    `drain-failed` {where, error} when it failed or a transcript could not be read (A-6). Returns
    the `capture_runner.Result`; under `_strict` a failed run raises `CaptureFailed`."""
    from alpaca import capture_runner, paths
    root = paths.root(payload.get("cwd"))
    sid = session_of(payload)
    now = time.monotonic()
    budget = capture_budget(cap_s, share)
    left = hook_time_left(CAPTURE_KILL_MARGIN_S)
    kill_s = left if left is not None else budget + capture_runner.GRACE_S
    kwargs = {"sessions": [sid], "session_events": True, "uncaptured_only": True,
              "transcripts": transcripts, "deadline_at": now + budget,
              "hard_at": now + max(0.1, kill_s - capture_runner.HARD_LEAD_S)}
    result = capture_runner.run_process(capture_runner.CAPTURE, kwargs, root=root, kill_s=kill_s,
                                        progress=True)
    capture_runner.record(root, sid, hook, result)
    if result.status == "failed" and payload.get("_strict"):
        raise CaptureFailed(result.error)
    return result


#: The hook steps `run_step` runs (alpaca/hook_steps.py): the child's target, and the record event a
#: failed or stopped step leaves, as that step's failure left before (None: the hook outcome only).
STEPS = {
    "pad": ("alpaca.hook_steps:pad", None),
    "export": ("alpaca.hook_steps:export", None),
    "hub": ("alpaca.hook_steps:hub", "hub-publish-failed"),
    "snapshot": ("alpaca.hook_steps:snapshot", None),
    "analytics": ("alpaca.hook_steps:analytics", "analytics-build-failed"),
    "profile": ("alpaca.hook_steps:profile", "drain-failed"),
    "consolidate": ("alpaca.hook_steps:consolidate", "drain-failed"),
}


class StepFailed(RuntimeError):
    """A step run in a child (`run_step`) that failed under `_strict`; its outcome is recorded."""


def run_step(payload, hook, name, *, share: float = 0.5, where: str | None = None, **kwargs):
    """Run one hook step (`STEPS`, alpaca/hook_steps.py) within an enforced budget (t-070 RC5).

    In a `fail_open` hook the step runs in a child process through the capture runner, which kills it
    at `share` of the time left before the hook's own kill margin (`CAPTURE_KILL_MARGIN_S`), so a call
    that blocks in the kernel there cannot hold the hook past its deadline (A-4); a `deadline_at`
    keyword is clipped to that budget. Outside a hook (the `session` verbs, a test) there is no
    deadline to enforce and the step runs in this process, as it did before.

    Every way a step ends is recorded the same way (`_record_step`): a step that failed or was stopped
    leaves the record event its failure left before (`analytics-build-failed`, `hub-publish-failed`,
    `drain-failed` {where}), and the hook's own outcome (`fail_open`) becomes `timeout` for a stopped
    step, `failed` for a failed step that has no event. Returns the `capture_runner.Result`; under
    `_strict` a failed step raises its exception (in this process) or `StepFailed`."""
    from alpaca import capture_runner, paths
    root = paths.root(payload.get("cwd"))
    target, _kind = STEPS[name]
    left = hook_time_left(CAPTURE_KILL_MARGIN_S)
    if left is None:
        from alpaca import hook_steps
        start = time.monotonic()
        call = dict(kwargs)
        if "deadline_at" in call:
            call["deadline_s"] = max(0.0, call.pop("deadline_at") - time.monotonic())
        try:
            value = getattr(hook_steps, name)(root, **call)
        except Exception as exc:
            result = capture_runner.Result("failed", error=capture_runner._describe(exc), exc=exc,
                                           seconds=time.monotonic() - start)
        else:
            result = capture_runner.Result("ok", value=value, seconds=time.monotonic() - start)
    else:
        kill_s = max(0.1, share * left)
        if "deadline_at" in kwargs:
            kwargs = dict(kwargs, deadline_at=min(kwargs["deadline_at"],
                                                  time.monotonic() + max(0.0, kill_s - capture_runner.HARD_LEAD_S)))
        result = capture_runner.run_process(target, kwargs, root=root, kill_s=kill_s)
    _record_step(root, session_of(payload), hook, name, result, where)
    if result.status == "failed" and payload.get("_strict"):
        raise result.exc if result.exc is not None else StepFailed(result.error)
    return result


def _record_step(root, sid, hook, name, result, where=None):
    """The one outcome record of a step (`run_step`): nothing for `ok`; else the step's own failure
    event, when it has one, and a fault the hook's outcome reports (`fail_open`). Never raises."""
    if result.status == "ok":
        return
    error = (result.error if result.status == "failed" else
             "StepOverrun: %s stopped at the hook deadline after %.1f s" % (name, result.seconds))
    _STEP_FAULTS.append((name, result.status, (error or "").split(":", 1)[0]))
    kind = STEPS[name][1]
    if kind is None:
        return
    data = {"where": where or hook, "error": error} if kind == "drain-failed" else {"error": error}
    try:
        from alpaca import db
        conn = db.connect(root)
        try:
            db.append_event(conn, session=sid, actor="alpaca", kind=kind, data=data)
        finally:
            conn.close()
    except Exception as exc:
        print("alpaca step outcome unavailable: " + type(exc).__name__, file=sys.stderr)


def _step_outcome(outcome, error_type):
    """The hook's outcome once its steps are counted: a step stopped at its budget makes an `ok` hook
    `timeout` (`StepOverrun:<steps>`), as the SIGALRM cut of that step did before; a failed step with
    no record event of its own makes it `failed` (`StepFailed:<step>:<type>`)."""
    if outcome != "ok" or not _STEP_FAULTS:
        return outcome, error_type
    stopped = [n for n, status, _ in _STEP_FAULTS if status != "failed"]
    if stopped:
        return "timeout", "StepOverrun:" + ",".join(stopped)
    silent = ["%s:%s" % (n, kind) for n, status, kind in _STEP_FAULTS if STEPS[n][1] is None]
    if silent:
        return "failed", "StepFailed:" + ",".join(silent)
    return outcome, error_type


def fail_open(fn=None, *, deadline_s: float | None = None, record_cost: bool = True):
    """Wrap a hook's main() so it prints nothing on failure and always exits 0, within a
    hard wall-clock deadline. Usable bare (`@fail_open`) or parametrised
    (`@fail_open(deadline_s=...)`). The wrapped hook records its own duration on the way out.

    `record_cost=False` drops that duration record for one hook. It is the only thing on the
    wrapper's path that opens the record, so a hook that otherwise never touches sqlite pays
    nothing for it. PreToolUse runs before every single tool call and takes it; every other hook
    keeps the default, so the analytics still show hook cost for the hooks that run once a turn.
    """
    def decorate(f):
        @functools.wraps(f)
        def wrapper():
            deadline = HOOK_DEADLINE_S if deadline_s is None else deadline_s
            start = time.monotonic()
            _STDIN_STASH.pop("payload", None)
            del _STEP_FAULTS[:]
            _WINDOW["end"] = start + deadline
            _arm_deadline(deadline)
            outcome, error_type = "ok", None
            try:
                return f()
            except BaseException as exc:
                outcome = "timeout" if isinstance(exc,_Deadline) else "failed"
                error_type = type(exc).__name__
                return None
            finally:
                _disarm_deadline()
                _WINDOW.pop("end", None)
                outcome, error_type = _step_outcome(outcome, error_type)
                # Outcome I/O is inside the same deadline budget, including fsync and
                # SQLite contention. A failed recorder must never wedge the caller.
                _arm_deadline(min(0.5,max(0.01,deadline-(time.monotonic()-start))))
                try:
                    if record_cost:
                        if outcome == "ok":
                            _record_duration(f, start)
                        else:
                            _record_duration(f, start, outcome, error_type)
                    elif outcome != "ok" and _STDIN_STASH.get("payload"):
                        record_outcome(_STDIN_STASH["payload"],getattr(f,"__module__","hook"),outcome,
                                       (time.monotonic()-start)*1000,error_type)
                except BaseException as exc:
                    print("alpaca hook outcome unavailable: "+type(exc).__name__,file=sys.stderr)
                finally:
                    _disarm_deadline()
                sys.exit(0)
        return wrapper
    return decorate(fn) if fn is not None else decorate
