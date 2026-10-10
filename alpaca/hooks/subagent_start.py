"""SubagentStart: record that a subagent started, with its agent id and type.

The session runtime of the research engine reads the event as the start time of a job's agent:
the group check of a verifier pair (in-session-runtime.md section 6.3, item 5) and the check
that no agent started before its job was handed (K4) rest on hook-written start and stop times,
which the driving session does not control. The payload carries `agent_id` and `agent_type`
(the runtime's hook table: "Input to command is JSON with agent_id and agent_type"), and the
handler records whatever ids it finds, as the stop hook does.

Headless like the other capture hooks: nothing on stdout, exit 0 on any path.
"""
from alpaca.hooks import common

#: the ids a SubagentStart payload may carry, each with the camelCase spelling some callers use.
ID_KEYS = (("agent_id", "agentId"), ("agent_type", "agentType"),
           ("subagent_id", "subagentId"), ("tool_use_id", "toolUseId"))


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
        db.append_event(conn, session=sid, actor="alpaca", kind="subagent-start", data=data)
        return {"subagent": data}
    finally:
        conn.close()


@common.fail_open
def main():
    handle(common.read_stdin())


if __name__ == "__main__":
    main()
