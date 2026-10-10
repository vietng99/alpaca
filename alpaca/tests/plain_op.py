"""A test helper (op-019 t-200): open an op without a question batch, as the research runner
does, for fixtures that add phaseless tasks."""


def plain_op(intent="intent", done_when="bar"):
    """Open an op the way the research runner does (ops.open_op): no question batch, so a research
    fixture adds phaseless tasks as the engine's own tests always did (op-019 t-200, D-19). The
    project root is the one the CLI would read."""
    from alpaca import cli, db, ops
    root = cli._root()
    conn = db.connect(root)
    try:
        return ops.open_op(conn, intent, done_when, session="cli", actor="human")
    finally:
        conn.close()
