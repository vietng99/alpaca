"""SubagentStop: record that a subagent finished, then snapshot the session transcript.

The payload shape varies by caller and by version, so every id here is optional: whatever the
payload carries is recorded and a missing key is simply absent from the event. The snapshot right
after is the point of the hook: a subagent writes its own transcript beside the session one while
it runs, and that is the moment the file is complete and worth copying into the project.

Headless like the other capture hooks: nothing on stdout, exit 0 on any path.
"""
from alpaca.hooks import common

#: the ids a SubagentStop payload may carry, each with the camelCase spelling some callers use.
ID_KEYS = (("agent_id", "agentId"), ("subagent_id", "subagentId"),
           ("agent_type", "agentType"), ("agent_name", "agentName"),
           ("tool_use_id", "toolUseId"), ("task_id", "taskId"))


def ids(payload):
    """Whatever ids the payload carries, under their snake_case names. Missing keys are dropped."""
    out = {}
    for name, alt in ID_KEYS:
        value = payload.get(name)
        if value is None:
            value = payload.get(alt)
        if value not in (None, ""):
            out[name] = value
    return out


def handle(payload):
    from alpaca import db, paths
    root = paths.root(payload.get("cwd"))
    sid = common.session_of(payload)
    conn = db.connect(root)
    try:
        data = ids(payload)
        db.append_event(conn, session=sid, actor="alpaca", kind="subagent-stop", data=data)
        result = {"subagent": data}
        from alpaca import observability
        child = data.get('agent_id') or data.get('subagent_id')
        if child:
            observability.expect_source(root,str(child),payload.get('operator') or 'claude',
                                       locator=payload.get('agent_transcript_path'),native_id=str(child),
                                       parent_session=sid,capabilities={'child_inventory_complete':False})
        if observability.enabled(root):
            result['queued'] = True
            return result
        try:
            from alpaca import transcripts
            result["snapshot"] = transcripts.snapshot(root, sid, conn=conn)
        except Exception:
            if payload.get("_strict"):
                raise
        return result
    finally:
        conn.close()


@common.fail_open
def main():
    handle(common.read_stdin())


if __name__ == "__main__":
    main()
