"""UserPromptSubmit: level + next action every Nth prompt; banned-list injection on the re-arm cadence.

A re-arm point (first prompt, every Nth, after compaction, danger band) injects the full
rule text; any other prompt injects only a short reminder line. See alpaca/hooks/common.py.
"""
from alpaca.hooks import common

EVERY = 5


def handle(payload):
    from alpaca import db, pad, paths, project
    root = paths.root(payload.get("cwd"))
    sid = common.session_of(payload)
    conn = db.connect(root)
    cfg = project.load(root)
    key = "prompts:%s" % sid
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?, '1') "
        "ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT)",
        (key,))
    conn.commit()
    n = int(db.meta_get(conn, key))
    # M3.1: mirror a mid-session /autodrive level change into the record as a decision row.
    try:
        from alpaca.posture import level as posture_level
        posture_level.mirror(conn, sid, root=root)
    except Exception:
        if payload.get("_strict"):
            raise
    band = common.prompt_band(payload)
    parts = []
    if n % EVERY == 0:
        from alpaca.posture import level as posture_level
        level = "L%d" % posture_level.in_force(conn, sid, root=root)
        parts.append("[alpaca | level %s | next: %s]" % (level, pad.next_action(root)))
    if common.should_rearm(root, sid, band):
        full = common.ban_rules(root, True)
        if full:
            parts.append(full)
        # M3.11: the model-side tiers (context-dependent words, response patterns) are injected
        # ONLY here, on the M1.1 re-arm cadence, never on a steady-state prompt.
        try:
            from alpaca.gates import literal_guard
            ms = literal_guard.model_side_text(root)
            if ms:
                parts.append(ms)
        except Exception:
            if payload.get("_strict"):
                raise
        db.append_event(conn, session=sid, actor="alpaca", kind="style-rearm",
                        data={"prompt": n, "band": band})
    else:
        reminder = common.ban_rules(root, False)
        if reminder:
            parts.append(reminder)
    # t-076: a session that edits files while holding no task claim is invisible on the cockpit's
    # parallel-sessions panel. Guarded: a failed check never costs the prompt its other context.
    try:
        nudge = claim_nudge(conn, sid)
        if nudge:
            parts.append(nudge)
    except Exception:
        if payload.get("_strict"):
            raise
    db.meta_set(conn, "band:%s" % sid, band)
    return {"context": "\n".join(parts)}


EDIT_TOOLS = ("Edit", "Write", "NotebookEdit")


def claim_nudge(conn, sid):
    """One reminder line when `sid` holds no live claim and edited a file after its last claim
    event or task move; None otherwise. A session that only reads or talks is left alone."""
    from alpaca import sessions_view
    if sessions_view.claims_by_session(conn, sessions_view.record_now(conn)).get(sid):
        return None
    marks = "','".join(EDIT_TOOLS)
    edit = conn.execute(
        "SELECT MAX(id) FROM events WHERE session=? AND kind='heartbeat' AND json_valid(data) "
        "AND json_extract(data,'$.tool') IN ('%s')" % marks, (sid,)).fetchone()[0]
    if not edit:
        return None
    settled = conn.execute(
        "SELECT COALESCE(MAX(id),0) FROM events WHERE session=? AND kind IN ('claim','claim-release','task-move')",
        (sid,)).fetchone()[0]
    if edit <= settled:
        return None
    return ("[alpaca claim] this session edited files but holds no task claim, so the cockpit shows it "
            "as unclaimed. Record the work: bin/alpaca task add <op> \"<statement>\" --title \"<short name>\", "
            "then bin/alpaca task claim <id> --by <worker> and bin/alpaca task move <id> doing.")


@common.fail_open
def main():
    result = handle(common.read_stdin())
    if result["context"]:
        common.emit_context("UserPromptSubmit", result["context"])


if __name__ == "__main__":
    main()
