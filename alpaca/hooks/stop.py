"""Stop: record the turn end, re-render the pad, push data.json on change, refresh analytics every
10th stop, and snapshot the transcript last.

The transcript copy goes last on purpose: it is the one step whose cost follows the size of a file
this hook does not own, and the hard hook deadline is a BaseException. Anything placed after it
could be cut off before it ran. See the comment at the call site.

M3.4: at the full-autodrive level the Stop hook refuses a top-level stop while a done marker is
genuinely absent (spec 5.6:415), and NOT while a thread is halted -- a halt is a bounded terminal
state, already surfaced by the stuck report, so forcing the run onward past it would be the
unbounded loop the retry bound exists to prevent. The refusal is a `{"decision": "block"}` line
on stdout; the hook still exits 0 (fail-open)."""
import json
import sys

from alpaca.hooks import common

EVERY = 10


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
    from alpaca import db, pad, paths
    from alpaca.analytics import build_index
    root = paths.root(payload.get("cwd"))
    sid = common.session_of(payload)
    conn = db.connect(root)
    db.append_event(conn, session=sid, actor="alpaca", kind="turn-end", data={})
    pad.write(root)
    # M3.4: refuse a top-level stop only while a done marker is genuinely absent (and not while a
    # thread is halted). stop_hook_active guards against re-blocking an already-active stop loop.
    blocked = False
    result = {}
    if not payload.get("stop_hook_active"):
        try:
            from alpaca.posture import loops
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
    if not blocked and not payload.get("stop_hook_active"):
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
        from alpaca import export
        export.write_if_changed(root)
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
        try:
            build_index.collect(root, only=sid)
            build_index.build(root)
        except Exception as exc:
            db.append_event(conn, session=sid, actor="alpaca", kind="analytics-build-failed",
                            data={"error": "%s: %s" % (type(exc).__name__, exc)})
            if payload.get("_strict"):
                raise
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
        from alpaca import transcripts
        transcripts.snapshot(root, sid, conn=conn)
    except BaseException:
        pass
    conn.close()
    return result

@common.fail_open
def main():
    result = handle(common.read_stdin())
    if result.get("decision"):
        sys.stdout.write(json.dumps(result) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
