"""PreToolUse: one pool line before the call runs. Prints nothing, decides nothing.

A PreToolUse hook can block a tool call by writing a decision on stdout. This one never writes on
stdout and never exits non-zero, so it cannot change what the session does: it only captures the
tool name and the whole tool input, which is the half of a call the PostToolUse heartbeat does not
keep. The fail-open wrapper carries the same bounded deadline as every other hook.

This is the only hook that runs before every single tool call, so its cost is paid over and over
in one turn. It stays off sqlite completely: `alpaca.pool` reaches nothing but `alpaca.paths` and
`alpaca.util`, and `record_cost=False` drops the wrapper's own duration record, which was the one
thing on this path that opened the record. What the hook costs is a process start and one append.
"""
from alpaca.hooks import common


def handle(payload):
    from alpaca import paths, pool
    root = paths.root(payload.get("cwd"))
    sid = common.session_of(payload)
    pool.record(root, sid, "pre", payload)
    return {"pool": True}


@common.fail_open(record_cost=False)
def main():
    handle(common.read_stdin())


if __name__ == "__main__":
    main()
