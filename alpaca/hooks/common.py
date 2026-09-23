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
  analytics can show hook cost per session.
"""
import json, os, sys, functools, select, signal, time
from alpaca import paths, util

STDIN_DEADLINE_S = 2.0                     # how long read_stdin waits for a wedged caller
HOOK_DEADLINE_S = float(os.environ.get("ALPACA_HOOK_DEADLINE_S") or 8)  # hard ceiling per hook
MAX_STDIN_BYTES = 4 * 1024 * 1024          # cap the read so an oversized flood cannot blow memory or time

_STDIN_STASH = {}                          # last payload parsed, for the cost recorder in fail_open


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

def emit_context(event_name: str, text: str) -> None:
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": text}}) + "\n")
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
