"""SessionEnd: close the session row, publish the hub tile when enabled, start the disk sweep, render the pad, rebuild analytics/index.html."""
from alpaca.hooks import common

#: Half the 60 s hook ceiling in .claude/settings.json; the hard deadline of fail_open binds first.
CAPTURE_CAP_S = 30.0
#: The consolidation step (default off, `consolidate_after_capture`) gets at most this long, and only
#: when at least `CONSOLIDATE_MIN_S` of the hook's time is left.
CONSOLIDATE_CAP_S = 10.0
CONSOLIDATE_MIN_S = 2.0

def handle(payload):
    from alpaca import db, paths, util
    root = paths.root(payload.get("cwd"))
    sid = common.session_of(payload)
    conn = db.connect(root)
    with db.transaction(conn):
        db.append_event(conn, session=sid, actor="alpaca", kind="session-end", data={"reason": payload.get("reason")})
        row = db.rows(conn, "sessions", "sid=?", (sid,))
        now = util.now_iso()
        if row:
            db.patch(conn, "sessions", "sid", sid, {"ended": now})
        else:
            db.upsert(conn, "sessions", "sid", {"sid": sid, "started": now, "ended": now})
    _publish_hub_tile(root, conn, sid, payload)
    _start_disk_sweep(root, sid, payload)
    from alpaca import capture_runner, observability
    if observability.enabled(root):
        conn.close()
        capture_runner.note_absent_collector(root, sid, "session_end")
        return {'ended':now,'queued':True}
    # The pad, the transcript copy, the profile refresh, the consolidation step and the analytics
    # build each run as a step of the one runner (`common.run_step`, t-070 RC5): in the hook, a child
    # process stopped at its share of the deadline, as the capture is (A-4).
    common.run_step(payload, "session_end", "pad", share=0.3)
    # E1: the last chance to copy the transcript while the source is still there. Recorded, so the
    # record carries the path, the size and the sha256 of what the project now holds.
    try:
        common.run_step(payload, "session_end", "snapshot", share=0.3, session=sid, event=True)
    except Exception:
        if payload.get("_strict"):
            raise
    # M2.14: land this session's events and transcript into the wiki with no human step, through
    # service.capture (architecture.md 2) run by the one capture runner (`common.capture_step`,
    # t-070 RC4), which stops it from outside at the hook's deadline. `uncaptured_only` leaves out the
    # events the Stop hook and an earlier PreCompact already landed, so the budget goes to the new
    # events first and then the transcript; what a hook lands twice (a concurrent Stop) is skipped by
    # absorb's hashgate (OB-18). A missing transcript keeps the event notes; a deadline appends
    # `capture-deferred`, a failure or a transcript failure `drain-failed` (FR-8). Isolated in its own
    # try so a wiki hiccup never blocks the session close.
    # A domain profile (alpaca/profile.py `refresh`) then refreshes its own projections; the empty
    # profile does nothing.
    # While this project's `collect watch` collector runs it lands the session's events and the
    # transcript (the snapshot above keeps the copy it reads), so the capture call is skipped: on a
    # live-size store it read the echo index inside the hook deadline and landed nothing (review B2,
    # `stop.capture_open_session`). The profile refresh and the consolidation step run either way.
    try:
        from alpaca import profile
        if not observability.collector_alive(root):
            common.capture_step(payload, "session_end", cap_s=CAPTURE_CAP_S, share=0.7)
        if profile.load(root) is not profile.EMPTY:
            # a failed or stopped refresh appends `drain-failed` {where: session_end}
            common.run_step(payload, "session_end", "profile", share=0.5, session=sid)
        # FR-18: one bounded consolidation step, only when the project turned it on and half the
        # hook's time remains. It proposes candidates and admits nothing (architecture.md 9.2).
        budget = common.capture_budget(CONSOLIDATE_CAP_S, share=0.5)
        if budget >= CONSOLIDATE_MIN_S and _consolidate_on(root):
            import time
            common.run_step(payload, "session_end", "consolidate", share=0.5, session=sid,
                            where="session_end_consolidate", deadline_at=time.monotonic() + budget)
    except (common.CaptureFailed, common.StepFailed):
        raise                                   # `_strict` only; the runner recorded it
    except Exception as e:
        # the error text may quote what failed: the one screen first (t-070 round 2 C-C, A-9)
        db.append_event(conn, session=sid, actor="alpaca", kind="drain-failed", data={"where": "session_end", "error": capture_runner._describe(e)})
        if payload.get("_strict"):
            raise
    # a failed or stopped build appends `analytics-build-failed` (`common.run_step`)
    common.run_step(payload, "session_end", "analytics", share=0.9)
    return {"ended": now}


def _consolidate_on(root):
    """Whether the project turned the consolidation step on (`consolidate_after_capture`, default
    off), read here so a SessionEnd with it off starts no step. A setting that cannot be read is
    left to the step, which records it as `drain-failed`."""
    try:
        from alpaca.wiki.config import Config
        return bool(Config.for_project(root, resolve=False).consolidate_after_capture)
    except Exception:
        return True

def _start_disk_sweep(root, sid, payload):
    """Start a detached disk sweep (alpaca/disk.py) when project.yaml declares a disk: block and
    one is due. The ending session's own scratch stays for `ended_grace_hours` after the end, so a
    `claude --resume` within that window finds its files; a `.keep` file keeps it longer."""
    try:
        from alpaca import disk
        disk.spawn_sweep(root, sid, "end")
    except Exception:
        if payload.get("_strict"):
            raise


def _publish_hub_tile(root, conn, sid, payload=None):
    """Publish this workspace's hub tile when project.yaml says hub.enabled: true (alpaca/hub_publish.py).
    data.json is refreshed from the record first, so the tile shows the state this session ended
    with (the session row is already closed) rather than the last Stop hook's. Fail-open: a publish
    error is recorded as a hub-publish-failed event and never breaks the hook; with hub publishing
    off nothing happens at all. The publish runs as a step of the one runner (`common.run_step`,
    t-070 RC5), which records a failed or stopped publish as `hub-publish-failed`."""
    try:
        from alpaca import hub_publish
        if not hub_publish.enabled(root):
            return
        common.run_step(dict(payload or {}, cwd=root, session_id=sid, _strict=False),
                        "session_end", "hub", share=0.3)
    except Exception as e:
        try:
            from alpaca import capture_runner, db
            db.append_event(conn, session=sid, actor="alpaca", kind="hub-publish-failed",
                            data={"error": capture_runner._describe(e)})
        except Exception:
            pass


@common.fail_open
def main():
    handle(common.read_stdin())


if __name__ == "__main__":
    main()
