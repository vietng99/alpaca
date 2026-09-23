"""SessionEnd: close the session row, render the pad, rebuild analytics/index.html."""
from alpaca.hooks import common

def handle(payload):
    from alpaca import db, pad, paths, util
    from alpaca.analytics import build_index
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
    from alpaca import observability
    if observability.enabled(root):
        conn.close()
        return {'ended':now,'queued':True}
    pad.write(root)
    # E1: the last chance to copy the transcript while the source is still there. Recorded, so the
    # record carries the path, the size and the sha256 of what the project now holds.
    try:
        from alpaca import transcripts
        transcripts.snapshot(root, sid, conn=conn, event=True)
    except Exception:
        if payload.get("_strict"):
            raise
    # M2.14: land this session's events and transcript into the wiki with no human step. Idempotent
    # (absorb's hashgate skips unchanged notes), so a PreCompact drain earlier this session is not
    # double-counted. Isolated in its own try so a wiki hiccup never blocks the session close.
    # A domain profile (alpaca/profile.py `refresh`) then refreshes its own projections; the empty
    # profile does nothing.
    try:
        from alpaca.wiki.ingest import drain
        from alpaca import profile
        drain.run(root, sid)
        profile.load(root).refresh(root, sid)
    except Exception as e:
        db.append_event(conn, session=sid, actor="alpaca", kind="drain-failed", data={"where": "session_end", "error": "%s: %s" % (type(e).__name__, e)})
        if payload.get("_strict"):
            raise
    try:
        build_index.build(root)
    except Exception as e:
        db.append_event(conn, session=sid, actor="alpaca", kind="analytics-build-failed", data={"error": "%s: %s" % (type(e).__name__, e)})
        if payload.get("_strict"):
            raise
    return {"ended": now}

@common.fail_open
def main():
    handle(common.read_stdin())


if __name__ == "__main__":
    main()
