"""PreCompact: drain the session into the wiki before the transcript is compacted away.

A compaction is a boundary just like a session end: the events and the transcript so far must land
in the wiki with no human step, so nothing is lost when the context window is rewritten. The drain
is idempotent (its notes are byte-stable and absorb's hashgate skips unchanged blocks), so running
it here AND again at SessionEnd double-counts nothing.

This hook is fail-open under the M1.5 contract (alpaca.hooks.common.fail_open): any error prints
nothing and exits 0, within a bounded wall-clock deadline. Capture may never block a session.
"""
from alpaca.hooks import common


def handle(payload):
    from alpaca import db, pad, paths
    from alpaca.wiki.ingest import drain
    root = paths.root(payload.get("cwd"))
    sid = common.session_of(payload)
    conn = db.connect(root)
    db.append_event(conn, session=sid, actor="alpaca", kind=payload.get("event_kind") or "pre-compact",
                    data={"trigger": payload.get("trigger") or payload.get("reason"), "note": payload.get("note") or ""})
    from alpaca import observability
    if observability.enabled(root):
        conn.close()
        return {'queued':True,'consumer':'wiki,snapshots,projection'}
    # E1: a compaction rewrites the context, so this is a boundary the transcript copy must cross.
    # The snapshot is recorded here, unlike the per-turn one on Stop.
    try:
        from alpaca import transcripts
        transcripts.snapshot(root, sid, conn=conn, event=True)
    except Exception:
        if payload.get("_strict"):
            raise
    try:
        summary = drain.run(root, sid)
        pad.write(root)
        return {"drain": summary}
    except Exception as e:
        db.append_event(conn, session=sid, actor="alpaca", kind="drain-failed",
                        data={"where": "pre_compact", "error": "%s: %s" % (type(e).__name__, e)})
        if payload.get("_strict"):
            raise
        return {"error": str(e)}


@common.fail_open
def main():
    handle(common.read_stdin())


if __name__ == "__main__":
    main()
