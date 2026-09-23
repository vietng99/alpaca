"""RESUME.md: rendered from the record every time. Never hand-edited."""
import os
from alpaca import db, ops, project, sessions_view, token, util
from alpaca import render as _render                # aliased: this module defines its own `render`

def next_action(root, *, conn=None, expire=True) -> str:
    conn = db.connect(root) if conn is None else conn
    # A domain profile (alpaca/profile.py) may name the next step first, e.g. a job to poll or a
    # stage to run. Without a profile, or with nothing to say, the generic line follows.
    from alpaca import profile
    line = profile.load(root).next_action(root)
    if isinstance(line, str) and line:
        return line
    if db.meta_get(conn, "onboarded") is None:
        return "run onboarding: bin/alpaca onboard --name <project> --who <name:role,...> --what <one line> [--task ...]"
    # expire=False is how a read-only caller (the data.json fold) asks for the same line without
    # the lease sweep: a projection renders the record, it never writes to it.
    if expire:
        ops.expire_leases(conn)
    t = ops.first_open_task(conn)
    if t:
        # M4.9: the task statement is externally sourced; neutralise it at the render boundary so
        # a hostile value cannot break the line (_render.cell is identity on a benign value).
        return "%s [%s] %s (phase %s)" % (t["id"], t["status"], _render.cell(t["statement"]),
                                          t["phase"] or "-")
    o = ops.current_op(conn)
    if o:
        return "%s has no open tasks: add one (bin/alpaca task add %s \"...\" --title \"...\") or close it (bin/alpaca op close %s --basis ...)" % (o["id"], o["id"], o["id"])
    return "no open op: pick one from intents/queue.md and run bin/alpaca op new \"<intent>\" --done-when \"...\""

def render(root) -> str:
    conn = db.connect(root)
    ops.expire_leases(conn)
    cfg = project.load(root)
    name = cfg.get("name") or "(not onboarded)"
    # op-006: the header counts sittings that did something, not every id the desktop app opened
    # for a second to run one local command. sessions_view decides which is which, once, for the
    # pad, data.json and the analytics fold together.
    view = sessions_view.classify(conn)
    now_ts = sessions_view.record_now(conn)
    work = sessions_view.of_class(view, sessions_view.WORK)
    probes = sessions_view.probe_window(view)
    level_default = cfg.get("default_level", "L2")
    lines = ["# RESUME - %s" % name, ""]
    lines.append("updated: %s | events: %d | sessions: %d working, %d probes" % (
        util.now_iso(), conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        len(work), probes["count"]))
    lead = sessions_view.leader(view)
    if lead:
        s = view[lead]
        lines.append("last session: %s started %s, last beat %s, level %s" % (
            lead[:8], s["started"] or "-", s["last_beat"] or "-", s["level"] or level_default))
    # M4.3: the fork-state sentinel, one line so a human reading RESUME.md sees where this copy
    # stands (fresh, a cloned but unrun second machine, or onboarded here). Guarded so a detection
    # failure never turns a RESUME write into a failure.
    try:
        from alpaca import adopt
        st = adopt.detect(root, conn)
        lines.append("fork-state: %s (%s)" % (st, adopt.describe(st)))
    except Exception:
        pass
    # Who is at the keys right now, and on what. The window is measured against the record's own
    # newest event, never the wall clock, so two renders of one record agree.
    claims = sessions_view.claims_by_session(conn, now_ts)
    active = [sid for sid in work if sessions_view.is_active(view[sid], now_ts)]
    lines += ["", "## Active now", ""]
    for sid in active:
        s = view[sid]
        held = claims.get(sid) or []
        lines.append("- %s %s %s  beats %d  last %s %s  holds %s" % (
            sid[:8], _render.cell(s["operator"] or "-"), s["level"] or level_default, s["beats"],
            _render.cell(s["last_tool"] or "-"), _render.cell((s["last_ref"] or "-")[:48]),
            ", ".join(held) or "(no claim)"))
    if not active:
        lines.append("(no session beat in the last %d minutes of record time)"
                     % (sessions_view.ACTIVE_WINDOW_S // 60))
    lines += ["", "## Next action", "", next_action(root, conn=conn), ""]
    block = token.pad_lines(conn, root)
    if block:
        lines += block + [""]
    # M4.5: unresolved collisions surface on the pad with their reference, since the predecessor
    # evidence shows a collision logged and never resolved. Guarded so a resolve-render failure
    # never turns a RESUME write into a failure.
    try:
        from alpaca import resolve
        resolve_block = resolve.pad_lines(conn)
        if resolve_block:
            lines += resolve_block + [""]
    except Exception:
        pass
    # M3.4: halted threads (bounded retries reached) surface in the pad with their stuck-report
    # pointers. Guarded so a halt-render failure never turns a RESUME write into a failure.
    try:
        from alpaca.posture import loops
        halt_block = loops.pad_lines(conn, root=root)
        if halt_block:
            lines += halt_block + [""]
    except Exception:
        pass
    # How far each live op has got, in one line: its tracker tasks and its obligation rows.
    # Guarded so a board fold failure never turns a RESUME write into a failure.
    try:
        from alpaca import board
        cards = board.view(conn)["cards"]
    except Exception:
        cards = []
    progress = sessions_view.op_progress(conn, cards=cards, now_ts=now_ts)
    lines += ["## Progress", ""]
    for p in progress:
        lines.append("%-16s %-7s tasks %d/%d  rows %d/%d (blocked %d)" % (
            p["id"], p["status"], p["tasks_done"], p["tasks_total"],
            p["rows_done"], p["rows_total"], p["rows_blocked"]))
    if not progress:
        lines.append("(no op open, and none closed in the last day of record time)")
    lines.append("")
    o = ops.current_op(conn)
    lines += ["## Current op", ""]
    if o:
        # M4.9: the intent and done_when are externally sourced; both cross _render.cell.
        lines.append("%s  %s" % (o["id"], _render.cell(o["intent"])))
        lines.append("done_when: %s" % (_render.cell(o["done_when"]) if o["done_when"] else "(not stated)"))
        # M2.7: the pad names the current op and its resume cursor; the op index (alpaca status
        # --json) carries every other op, so resuming a second op never rewrites the pad.
        try:
            from alpaca import opindex
            cur = opindex.cursor(conn, o["id"])
            lines.append("state: %s | cursor: %s" % (opindex.state(conn, o["id"]), cur or "(all rows done)"))
        except Exception:
            pass
    else:
        lines.append("(none open)")
    lines += ["", "## Open tasks", ""]
    tasks = db.rows(conn, "tasks", "status IN ('open','doing','blocked') ORDER BY id")
    # M4.9: the statement and the claimant token are externally sourced; both cross _render.cell.
    lines += ["- %s [%s] %s%s" % (t["id"], t["status"], _render.cell(t["statement"]),
              (" (claimed by %s)" % _render.cell(t["claimant"])) if t["claimant"] else "")
              for t in tasks] or ["(none)"]
    lines += ["", "## Recent events", ""]
    # The kinds that repeat on their own, and a probe's open/close pair, are counted on one
    # closing line instead of taking all eight slots.
    recent = sessions_view.recent_events(conn, view, limit=8)
    for e in reversed(recent):
        lines.append("- %s %s %s %s" % (e["ts"], e["kind"], _render.cell(e["op"] or ""),
                                        _render.cell(e["ref"] or "")))
    if recent:
        noise = sessions_view.noise_since(conn, view, recent[-1]["id"])
        lines.append("(+ %d heartbeats, %d probe sessions since the first line above)"
                     % (noise["heartbeats"], noise["probe_sessions"]))
    # A shipped template (project.yaml `template: true`) is not onboarded either: its name is the
    # distribution's, not this project's, until `alpaca onboard` writes the project's own answers.
    if db.meta_get(conn, "onboarded") is None or cfg.get("template") is True:
        lines += ["", "This project is not onboarded. Run: bin/alpaca onboard ..."]
    return "\n".join(lines) + "\n"

def render_checklist(root) -> str:
    """CHECKLIST.md: the obligation rows as a projection, grouped by op and phase, each with its
    folded status (never a stored column). Rendered from the record every time; never
    hand-edited. M2.17 registers it with the freshness gate; this renderer is its trusted
    source. The generation stamp is the one volatile line the gate excludes."""
    conn = db.connect(root)
    ops.expire_leases(conn)
    cfg = project.load(root)
    from alpaca import board
    from alpaca.checklist import verdict_row
    v = board.view(conn)
    name = cfg.get("name") or "(not onboarded)"
    lines = ["# CHECKLIST - %s" % name, ""]
    lines.append("updated: %s | rows: %d" % (util.now_iso(), len(v["cards"])))
    from alpaca.hub_checklist import render as readable_checklist
    lines += readable_checklist(conn, v["cards"])
    lines += ["", "columns: " + "  ".join("%s=%d" % (c, v["counts"][c]) for c in v["columns"])]
    # deterministic order: op, then phase, then row id. A superseded row never shows (board.view
    # already returns heads only).
    cards = sorted(v["cards"], key=lambda c: (str(c.get("op") or ""),
                                              str(c.get("phase") or ""), str(c["row_id"])))
    op_seen = object()
    last_op = op_seen
    last_phase = op_seen
    for c in cards:
        op = c.get("op")
        phase = c.get("phase")
        if op != last_op:
            lines += ["", "## op %s" % (op or "-")]
            last_op = op
            last_phase = op_seen
        if phase != last_phase:
            lines += ["", "### phase %s" % (phase or "-")]
            last_phase = phase
        # M4.9: the reason and the claimant token are externally sourced; both cross _render.cell.
        tail = (" (%s)" % _render.cell(c["reason"])) if c["reason"] else ""
        who = (" @%s" % _render.cell(c["claimant"])) if c["claimant"] else ""
        lines.append("- [%-7s] %s %s%s%s" % (c["column"], c["row_id"],
                     c["tag"] or "-", who, tail))
    if not cards:
        lines += ["", "(no obligation rows)"]
    return "\n".join(lines) + "\n"


def write_checklist(root) -> str:
    p = os.path.join(root, "CHECKLIST.md")
    util.write_text(p, render_checklist(root))
    return p


def write(root) -> str:
    p = os.path.join(root, "RESUME.md")
    util.write_text(p, render(root))
    write_checklist(root)
    # M2.3: board.json is a projection too, rendered from the record on every status write.
    # Guarded so a board render never turns a RESUME write into a failure.
    try:
        from alpaca import board
        board.write_json(root)
    except Exception:
        pass
    return p
