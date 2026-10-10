"""PreCompact: drain the session into the wiki before the transcript is compacted away.

A compaction is a boundary just like a session end: the events and the transcript so far must land
in the wiki with no human step, so nothing is lost when the context window is rewritten. The hook
reaches the drain only through service.capture (run by the one capture runner, `common.capture_step`), for the events no earlier capture landed (the
Stop hook's, a previous PreCompact's) and then the transcript. The capture is idempotent (its
notes are byte-stable and absorb's hashgate skips unchanged blocks), so running it here AND again
at SessionEnd double-counts nothing, and a Stop running at the same time lands each event once.

This hook is fail-open under the M1.5 contract (alpaca.hooks.common.fail_open): any error prints
nothing and exits 0, within a bounded wall-clock deadline. Capture may never block a session.
"""
from alpaca.hooks import common

#: Half the 20 s hook ceiling in .claude/settings.json; the hard deadline of fail_open binds first.
CAPTURE_CAP_S = 10.0


def handle(payload):
    from alpaca import db, paths
    root = paths.root(payload.get("cwd"))
    sid = common.session_of(payload)
    conn = db.connect(root)
    db.append_event(conn, session=sid, actor="alpaca", kind=payload.get("event_kind") or "pre-compact",
                    data={"trigger": payload.get("trigger") or payload.get("reason"), "note": payload.get("note") or ""})
    from alpaca import capture_runner, observability
    if observability.enabled(root):
        conn.close()
        capture_runner.note_absent_collector(root, sid, "pre_compact")
        return {'queued':True,'consumer':'wiki,snapshots,projection'}
    # E1: a compaction rewrites the context, so this is a boundary the transcript copy must cross.
    # The snapshot is recorded here, unlike the per-turn one on Stop. It and the pad run as steps of
    # the one runner (`common.run_step`, t-070 RC5), stopped at their share of the hook's deadline.
    try:
        common.run_step(payload, "pre_compact", "snapshot", share=0.3, session=sid, event=True)
    except Exception:
        if payload.get("_strict"):
            raise
    if observability.collector_alive(root):
        # A running `collect watch` collector lands this session's events and transcript (the
        # snapshot above keeps the copy it reads); capturing here too would read the echo index
        # inside the hook deadline on a live-size store and land nothing (review B2,
        # `stop.capture_open_session`). Without a live collector the capture runs as before.
        conn.close()
        common.run_step(payload, "pre_compact", "pad", share=0.9)
        return {"drain": None}
    conn.close()
    # The capture runs through the one capture runner (t-070 RC4): stopped from outside at the hook's
    # deadline, and every way it ends is recorded (`capture-deferred` or `drain-failed`).
    result = common.capture_step(payload, "pre_compact", cap_s=CAPTURE_CAP_S, share=0.7)
    if result.status == "failed":
        return {"error": result.error}
    common.run_step(payload, "pre_compact", "pad", share=0.9)
    if result.status != "ok":
        return {"drain": None, "status": result.status}
    return {"drain": result.value}


def checkpoint(payload):
    """The FR-7 checkpoint verb (`session checkpoint`, alpaca/operator.py). Codex runs no hooks, so
    an open Codex session is captured here the way an open Claude session is at each Stop (OA-4):
    the `session-checkpoint` event (with its note) and the recorded transcript snapshot as this hook
    records them, then the Stop hook's own capture step, `stop.capture_open_session`: the session's
    uncaptured event notes, the checkpoint's own included, and no transcript, which lands at `session
    end` as a Claude transcript waits for SessionEnd or PreCompact. Every capture goes through
    `service.capture`, so a checkpoint, a Stop and a SessionEnd of one session land each event once.
    A failed capture appends `drain-failed` (where `checkpoint`) and, as the verb passes `_strict`,
    raises, so the verb exits nonzero. With observability on, or while the project's watch collector
    runs (`stop.capture_open_session`), the collector owns capture."""
    from alpaca import db, observability, pad, paths
    from alpaca.hooks import stop
    root = paths.root(payload.get("cwd"))
    sid = common.session_of(payload)
    conn = db.connect(root)
    try:
        db.append_event(conn, session=sid, actor="alpaca", kind="session-checkpoint",
                        data={"trigger": payload.get("trigger") or payload.get("reason"),
                              "note": payload.get("note") or ""})
        if observability.enabled(root):
            from alpaca import capture_runner
            capture_runner.note_absent_collector(root, sid, "checkpoint")
            return {'queued': True, 'consumer': 'wiki,snapshots,projection'}
        try:
            from alpaca import transcripts
            transcripts.snapshot(root, sid, conn=conn, event=True)
        except Exception:
            if payload.get("_strict"):
                raise
    finally:
        conn.close()
    summary = stop.capture_open_session(payload, hook="checkpoint")
    pad.write(root)
    return {"drain": summary}


@common.fail_open
def main():
    handle(common.read_stdin())


if __name__ == "__main__":
    main()
