"""Stop: record the turn end, re-render the pad, push data.json on change, refresh analytics every
10th stop, and snapshot the transcript last.

The transcript copy goes last on purpose: it is the one step whose cost follows the size of a file
this hook does not own, and the hard hook deadline is a BaseException. Anything placed after it
could be cut off before it ran. See the comment at the call site.

M3.4: at the full-autodrive level the Stop hook refuses a top-level stop while a done marker is
genuinely absent, and NOT while a thread is halted -- a halt is a bounded terminal
state, already surfaced by the stuck report, so forcing the run onward past it would be the
unbounded loop the retry bound exists to prevent. The refusal is a `{"decision": "block"}` line
on stdout; the hook still exits 0 (fail-open).

A stop caused by the account usage limit is never refused, at any level: the limit ends the turn
to protect the run, the harness parks the session and continues it after the reset, and the
resumed turn hands off from the last checkpoint. Refusing that stop only retries against a closed
limit and breaks the resume. The hook records a `limit-stop` event instead."""
import json
import re
import sys

from alpaca.hooks import common

EVERY = 10
#: Half the 20 s Stop ceiling in .claude/settings.json (OB-18); the hard deadline of fail_open binds first.
CAPTURE_CAP_S = 10.0

#: the synthetic final reply a harness writes when the account hits its usage limit.
LIMIT_REPLY = re.compile(r"\AYou.ve hit your .*limit|\AClaude AI usage limit reached")
#: how much of the transcript tail to read; the limit record is the last turn-end entry.
TAIL_BYTES = 256 * 1024


def limit_stop(payload):
    """Whether this stop is the account usage limit ending the turn. Structural checks only: the
    synthetic limit text as the whole final reply, or the transcript's last turn-end record
    carrying the limit error (Claude Code `"error": "rate_limit"` / 429 on a synthetic API-error
    message, Codex `usage_limit_exceeded`). A later user turn clears it. Never raises."""
    reply = payload.get("last_assistant_message")
    if isinstance(reply, str) and LIMIT_REPLY.match(reply):
        return True
    tp = payload.get("transcript_path") or payload.get("transcriptPath")
    if not tp:
        return False
    try:
        with open(tp, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - TAIL_BYTES))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except (OSError, TypeError, ValueError):
        return False
    hit = False
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        kind = obj.get("type")
        if kind == "user":
            hit = False
        elif kind == "assistant":
            hit = bool(obj.get("isApiErrorMessage")) and (
                obj.get("error") == "rate_limit" or obj.get("apiErrorStatus") == 429)
        elif kind == "event_msg" and isinstance(obj.get("payload"), dict):
            event = obj["payload"]
            if event.get("type") in ("task_started", "user_message"):
                hit = False
            error = event.get("error") if isinstance(event.get("error"), dict) else event
            if error.get("codex_error_info") == "usage_limit_exceeded":
                hit = True
    return hit


def _reply_text(payload, root, sid):
    """The agent's final reply for the style lint. Prefer an inline `reply` field; otherwise read
    the last assistant text blocks from the transcript named by the payload. Never raises."""
    inline = payload.get("reply") or payload.get("last_assistant")
    if inline:
        return str(inline)
    import os
    tp = payload.get("transcript_path") or payload.get("transcriptPath")
    if not tp or not os.path.isfile(tp):
        return ""
    last = ""
    try:
        with open(tp, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                content = (obj.get("message") or {}).get("content")
                if isinstance(content, str):
                    last = content
                elif isinstance(content, list):
                    parts = [b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text"]
                    if any(parts):
                        last = "\n".join(p for p in parts if p)
    except OSError:
        return ""
    return last


def handle(payload):
    from alpaca import db, paths
    root = paths.root(payload.get("cwd"))
    sid = common.session_of(payload)
    conn = db.connect(root)
    db.append_event(conn, session=sid, actor="alpaca", kind="turn-end", data={})
    # The pad, the data push, the analytics build and the transcript copy each run as a step of the
    # one runner (`common.run_step`, t-070 RC5): in the hook, a child process stopped at its share of
    # the deadline, so a call blocked in the kernel cannot hold the turn's end (A-4).
    common.run_step(payload, "stop", "pad", share=0.4)
    # A usage-limit stop is never refused (see the module docstring): record it and let it end.
    limited = limit_stop(payload)
    if limited:
        db.append_event(conn, session=sid, actor="alpaca", kind="limit-stop", data={})
    # M3.4: refuse a top-level stop only while a done marker is genuinely absent (and not while a
    # thread is halted). stop_hook_active guards against re-blocking an already-active stop loop.
    blocked = False
    result = {}
    if not limited and not payload.get("stop_hook_active"):
        try:
            from alpaca.posture import level as posture_level, loops
            # an owner's `/autodrive off` made since the last tool call reaches the record first
            posture_level.mirror(conn, sid, root=root)
            refuse, reason = loops.stop_should_refuse(conn, sid, root=root)
            if refuse:
                result.update(decision="block", reason=reason)
                blocked = True
        except Exception:
            if payload.get("_strict"):
                raise
    # M3.11: the Stop hook runs the ONE lint over the final reply. A hit blocks once (the
    # stop_hook_active guard caps it at one rewrite per turn) with the matches and a rewrite
    # instruction. No rule configured, or no reply to read, is a no-op. At most one block line.
    if not blocked and not limited and not payload.get("stop_hook_active"):
        try:
            from alpaca.gates import literal_guard
            rules = literal_guard.load_rules(root)
            reply = _reply_text(payload, root, sid)
            matches = literal_guard.lint(reply, rules) if reply else []
            if matches:
                result.update(decision="block", reason=literal_guard.rewrite_instruction(matches))
        except Exception:
            if payload.get("_strict"):
                raise
    from alpaca import observability
    if observability.enabled(root):
        conn.close()
        result['queued'] = True
        return result
    # M2.18: push data, not a rebuild (Q14). data.json is a projection of the record; the page is
    # deployed once and this pushes the payload only when the turn changed it. Guarded on its own
    # so a render failure never touches the pad or the analytics refresh below; the handler is
    # fail-open regardless (M1.5).
    try:
        common.run_step(payload, "stop", "export", share=0.4)
    except Exception:
        if payload.get("_strict"):
            raise
    # Analytics is a session view; it refreshes every tenth stop, at session end and on an
    # explicit build. A failed build is recorded, never hidden (M1.5 keeps the hook fail-open).
    key = "stops:%s" % sid
    with db.transaction(conn):
        n = int(db.meta_get(conn, key, "0")) + 1
        db.meta_set(conn, key, str(n))
    if n % EVERY == 0:
        # a failed or stopped build appends `analytics-build-failed` (`common.run_step`)
        common.run_step(payload, "stop", "analytics", share=0.6, session=sid)
    # E1: keep the local copy of the registered transcript current -- LAST, after the result is
    # computed and everything else has run. A transcript is JSONL and grows by appending, so a turn
    # usually costs one tail append, but the copy is the one step here whose cost follows the size
    # of a file this hook does not own. The hard hook deadline raises a BaseException, which an
    # `except Exception` above would not catch, so a slow copy placed earlier would take the stop
    # refusal, the style lint, the data push and the analytics refresh down with it. Here there is
    # nothing left to lose: whatever this raises, the already-computed result is what comes back.
    # No event on this path: a Stop happens every turn and the boundary snapshots (PreCompact,
    # SessionEnd) carry the recorded fact.
    try:
        common.run_step(payload, "stop", "snapshot", share=0.6, session=sid)
    except BaseException:
        pass
    conn.close()
    return result

def capture_open_session(payload, hook="stop"):
    """OB-18: capture the open session's uncaptured event notes (FR-8), no transcript. Runs from
    `main` after the decision line is flushed, never inside `handle` (so `session stop`, which calls
    only `handle`, captures nothing), within half the 20 s hook ceiling, through the one capture
    runner (`common.capture_step`, t-070 RC4): a capture still running at the hook's deadline is
    stopped from outside and appends `capture-deferred`, as a capture its budget cut short does (A-6);
    a failure appends `drain-failed`; neither changes the hook's output. With observability on, the
    collector owns capture, as in the SessionEnd and PreCompact hooks; when no collector is alive
    then, the step notes it (`capture_runner.note_absent_collector`, A-8).

    The same holds while this project's `collect watch` collector is running
    (`observability.collector_alive`), with or without the `enabled` flag: the collector lands every
    event after its checkpoint, so the step is skipped, nothing is written and nothing is appended.
    On a live-size store the step read the echo index (about 30 s) on every turn end, so the 8 s hook
    deadline cut it each time after it had held the turn and competed for the wiki write lock
    (merge of op-008 into main, review B2). With no live collector it captures as before.

    The FR-7 checkpoint verb runs this same step for an open Codex session, which has no Stop hook
    (OA-4, `pre_compact.checkpoint`); `hook` names the boundary in the events it appends. Returns the
    capture report, or None when nothing was captured here."""
    from alpaca import capture_runner, observability, paths
    root = paths.root(payload.get("cwd"))
    if observability.enabled(root):
        capture_runner.note_absent_collector(root, common.session_of(payload), hook)
        return None
    if observability.collector_alive(root):
        return None
    result = common.capture_step(payload, hook, cap_s=CAPTURE_CAP_S, transcripts=False)
    return result.value if result.status == "ok" else None


@common.fail_open
def main():
    payload = common.read_stdin()
    result = handle(payload)
    if result.get("decision"):
        sys.stdout.write(json.dumps(result) + "\n")
        sys.stdout.flush()
    capture_open_session(payload)


if __name__ == "__main__":
    main()
