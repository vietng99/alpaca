"""PostToolUse: one heartbeat event per tool call. Headless, no output."""
import hashlib
from alpaca.hooks import common

def brief(tool_name, tool_input):
    ti = tool_input or {}
    if tool_name == "Bash":
        cmd = str(ti.get("command") or "")
        first = cmd.split()[0] if cmd.split() else ""
        digest = hashlib.sha256(cmd.encode("utf-8")).hexdigest()[:12]
        return "%s sha256:%s" % (first, digest)
    for k in ("file_path", "path", "notebook_path", "command", "url", "pattern", "query", "skill", "description"):
        if ti.get(k):
            return str(ti[k])[:200]
    return ""

def handle(payload):
    from alpaca import db, paths, util
    root = paths.root(payload.get("cwd"))
    sid = common.session_of(payload)
    conn = db.connect(root)
    tool = payload.get("tool_name") or "?"
    # M3.1: keep the recorded level in force reconciled with the /autodrive skill state.
    try:
        from alpaca.posture import level as posture_level
        posture_level.mirror(conn, sid, root=root)
    except Exception:
        if payload.get("_strict"):
            raise
    # M4.10: stamp the honest project this call attributes to, derived from the target rather
    # than the shell cwd. A command or a URL is display and attributes nothing (where_ null).
    try:
        from alpaca import where
        attributed = where.attribute(payload)
    except Exception:
        attributed = None
    with db.transaction(conn):
        db.append_event(conn, session=sid, actor="agent", kind="heartbeat",
                        data={"tool": tool, "ref": brief(tool, payload.get("tool_input"))},
                        where=attributed)
        now = util.now_iso()
        if not db.rows(conn, "sessions", "sid=?", (sid,)):
            db.upsert(conn, "sessions", "sid", {"sid": sid, "started": now, "last_beat": now, "beats": 0})
        conn.execute("UPDATE sessions SET beats=beats+1, last_beat=? WHERE sid=?", (now, sid))
    # E6: the heartbeat above keeps its shape; the pool keeps the whole call beside the record,
    # so a tool input and a tool response survive the session that produced them. Guarded on its
    # own: a pool that cannot be written never costs the record its heartbeat.
    try:
        from alpaca import pool
        pool.record(root, sid, "post", payload)
    except Exception as exc:
        common.record_failure(payload,"post_tool_pool",exc)
        if payload.get("_strict"):
            raise
    conn.close()
    return {"heartbeat": True}

@common.fail_open
def main():
    handle(common.read_stdin())


if __name__ == "__main__":
    main()
