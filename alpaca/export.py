"""data.json: the static-page payload, rendered from the record (M2.18).

`alpaca export` writes ONE `data.json` whose top level carries the seven keys the cockpit reads
(`board`, `messages`, `pulse`, `gates`, `wiki`, `runs`, `metrics`; served live at
`/board/data.json`), plus a volatile `generated` stamp. `metrics` and `pulse.now.stages` come
from the domain profile (alpaca/profile.py `metrics` and `stage_rows`) and are empty without one. Every section is folded from the record only:
`render(root)` opens
the DB and reads it, and needs nothing on disk, so a single canonical projection is produced
under a fixed clock. The store is the only truth; this file renders a projection and never a
store, so `data.json` is never hand-edited and the projection-freshness gate (M2.17) proves it
matches a fresh render.

The seam M2.17 registered is `render(root) -> str` (the exact `data.json` bytes) and
`write(root)`; both are preserved here so the freshness registration is untouched. Q14's rule
is push-data-not-rebuild: the page is deployed once and `data.json` is pushed on change from a
card-move or the Stop hook (`alpaca/hooks/stop.py`), never re-rendered into a new page.

`alpaca deploy` is the deploy barrier. Publishing outside the box is an irreversible external
action, so it is a recorded human decision and never auto-advanced: `alpaca deploy` PAUSES (exit 3)
with a named reason while no human decision row for the deploy is on the record. M3.5 wires the
pause to a real review card (`alpaca/review.py`): `alpaca deploy` raises the card and STILL exits 3, so
the card is what a human moves and the deploy never self-advances.
"""
from __future__ import annotations

import json
import os

from alpaca import board, db, profile, sessions_view, util
from alpaca import render as _render                # aliased: this module defines its own `render`

#: how many entries the `now` fold keeps of each unbounded list, so a long run cannot grow
#: data.json without bound.
NOW_SESSIONS = 12
NOW_RECENT = 25
#: how many non-heartbeat events the record-pulse timeline keeps, newest first. Held at the
#: same size as NOW_RECENT so the two timelines on the board agree; a long run cannot grow
#: data.json without bound because the list is capped here.
PULSE_RECENT = 25


def _root_of(conn):
    """The project root behind an open connection: `<root>/.alpaca/alpaca.db`. `payload(conn)` is
    called with a connection alone (the live server does), so the root is recovered from the
    connection rather than threaded through every caller."""
    try:
        for r in conn.execute("PRAGMA database_list"):
            if r[1] == "main" and r[2]:
                return os.path.dirname(os.path.dirname(os.path.abspath(r[2])))
    except Exception:
        pass
    return None


def _next_action(root) -> str:
    """The line the pad prints under Next action, for the payload. Read-only: the lease sweep is
    the pad's, so rendering data.json never writes to the record. Empty when it cannot be read."""
    if not root:
        return ""
    try:
        from alpaca import pad
        return pad.next_action(root, expire=False)
    except Exception:
        return ""


def _level_of(conn, sid, root):
    """The autodrive level in force for one session, as the band name. Read from the record, with
    the on-disk default-level behind it; None when it cannot be read."""
    try:
        from alpaca.posture import level as _level
        return "L%d" % _level.in_force(conn, sid, root=root)
    except Exception:
        return None


def _now(conn, cards, root) -> dict:
    """`pulse.now`: where the project stands, folded from the record alone.

    The owner's question is "where are we": which sessions are live, what each holds, how far
    every op has got, what the profile last said per stage, and what happened that was not a
    heartbeat. Every list is bounded and every string crosses the JSON boundary in `alpaca/render.py`.
    """
    view = sessions_view.classify(conn)
    as_of = sessions_view.record_now(conn)
    prof = profile.load(root) if root else profile.for_conn(conn)
    claims = sessions_view.claims_by_session(conn, as_of)
    work = sessions_view.of_class(view, sessions_view.WORK)
    sealed = {r[0] for r in conn.execute(
        "SELECT DISTINCT ref FROM events WHERE kind='proof-report' AND ref IS NOT NULL")}
    order = {"doing": 0, "open": 1, "blocked": 2, "done": 3}
    tasks = sorted(db.rows(conn, "tasks", "1=1 ORDER BY id"),
                   key=lambda t: (order.get(t["status"], 4), str(t["id"])))
    recent = sessions_view.recent_events(conn, view, limit=NOW_RECENT)
    sessions = []
    for sid in work[:NOW_SESSIONS]:
        s = view[sid]
        sessions.append({"sid": _render.for_json(sid), "operator": _render.for_json(s["operator"]),
                         "started": s["started"], "ended": s["ended"], "last_beat": s["last_beat"],
                         "beats": s["beats"], "turns": s["turns"], "level": s["level"],
                         "active": sessions_view.is_active(s, as_of),
                         "last_tool": _render.for_json(s["last_tool"]),
                         "last_ref": _render.for_json(s["last_ref"]),
                         "claims": [_render.for_json(t) for t in claims.get(sid, [])],
                         "subagent_stops": s["subagent_stops"]})
    return {
        "as_of": as_of,
        "next_action": _render.for_json(_next_action(root)),
        "level": _level_of(conn, sessions_view.leader(view), root) if work else None,
        "ops": [{k: _render.for_json(v) for k, v in op.items()}
                for op in sessions_view.op_progress(conn, cards=cards, now_ts=as_of)],
        "tasks": [{"id": t["id"], "op": t["op"], "status": t["status"],
                   "statement": _render.for_json(t["statement"]),
                   "claimant": _render.for_json(t["claimant"]),
                   "proof": _render.for_json(t["proof"]), "sealed": t["id"] in sealed}
                  for t in tasks],
        "stages": [{k: _render.for_json(v) for k, v in stage.items()}
                   for stage in _rows(prof.stage_rows(conn)) if isinstance(stage, dict)],
        "sessions": {"work": sessions, "probes": sessions_view.probe_window(view),
                     "service": [{"sid": _render.for_json(sid), "events": view[sid]["events"]}
                                 for sid in sessions_view.of_class(view, sessions_view.SERVICE)]},
        "recent": [{"id": e["id"], "ts": e["ts"], "session": _render.for_json(e["session"]),
                    "session_class": sessions_view.class_of(view, e["session"]),
                    "kind": e["kind"], "ref": _render.for_json(e["ref"]),
                    "summary": _render.for_json(sessions_view.summary_of(e, prof))}
                   for e in recent],
        "noise": sessions_view.noise_since(conn, view, recent[-1]["id"] if recent else None),
    }


def _messages(conn) -> list:
    # M4.9: the sender, to and body are externally sourced. data.json is a JSON surface, so they
    # cross _render.for_json (identity): json.dumps neutralises, json.loads returns them byte for
    # byte, so a parsed message field equals the original value.
    out = []
    for m in db.rows(conn, "messages", "1=1 ORDER BY id"):
        out.append({"id": m.get("id"), "ts": m.get("ts"),
                    "sender": _render.for_json(m.get("sender")),
                    "to": _render.for_json(m.get("to_")), "kind": m.get("kind"),
                    "body": _render.for_json(m.get("body")), "ref": _render.for_json(m.get("ref"))})
    return out


def _runs(conn) -> list:
    out = []
    for r in db.rows(conn, "run", "1=1 ORDER BY id"):
        out.append({"id": r.get("id"), "ts": r.get("ts"), "gate": r.get("gate"),
                    "code": r.get("code"), "verdict": r.get("verdict"),
                    "reason": _render.for_json(r.get("reason"))})
    return out


def _pulse(conn, cards=None, root=None) -> dict:
    """The liveness fold from the record. It reuses the M0 analytics fold's shape (the ops- and
    tasks-by-status maps of `alpaca.analytics.build_index._fold`) so the page and the analytics view
    speak the same counts, but reads them straight from the record rather than from any
    transcript, so `render` needs nothing on disk. Nothing here is stored; this is a projection.
    """
    ops = {r["status"]: r["n"]
           for r in conn.execute("SELECT status, COUNT(*) n FROM ops GROUP BY status")}
    tasks = {r["status"]: r["n"]
             for r in conn.execute("SELECT status, COUNT(*) n FROM tasks GROUP BY status")}
    # The latest record events, heartbeats left out: what happened last, without opening the log.
    recent = [{"id": r["id"], "ts": r["ts"], "kind": r["kind"], "op": r["op"],
               "ref": _render.for_json(r["ref"]), "session": _render.for_json(r["session"])}
              for r in conn.execute("SELECT id, ts, kind, op, ref, session FROM events "
                                    "WHERE kind != 'heartbeat' ORDER BY id DESC LIMIT ?",
                                    (PULSE_RECENT,))]
    open_tasks = [{"id": t["id"], "op": t["op"], "phase": t["phase"], "status": t["status"],
                   "statement": _render.for_json(t["statement"]), "claimant": _render.for_json(t["claimant"])}
                  for t in db.rows(conn, "tasks", "status != 'done' ORDER BY id")]
    return {
        "recent": recent,
        "open_tasks": open_tasks,
        "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
        "ops": ops,
        "tasks": tasks,
        "ops_open": ops.get("open", 0),
        # op-006: where the project stands right now. It rides inside `pulse` rather than beside
        # it, so the seven-key top level the page binds to is untouched.
        "now": _now(conn, cards, root),
    }


def _gates(conn) -> list:
    """The instrument census, latest-first, as a compact list. M2.18 shapes the full view."""
    rows = db.rows(conn, "run", "1=1 ORDER BY id DESC LIMIT 50")
    return [{"gate": r.get("gate"), "code": r.get("code"), "verdict": r.get("verdict"),
             "ts": r.get("ts")} for r in rows]


def _wiki(conn) -> dict:
    """A minimal wiki summary: the page count by kind. M2.16 wires `alpaca wiki`; M2.18 renders it."""
    counts = {}
    for r in db.rows(conn, "page", "1=1"):
        k = r.get("kind") or "page"
        counts[k] = counts.get(k, 0) + 1
    return {"pages": sum(counts.values()), "by_kind": counts}


def _rows(value) -> list:
    """A profile answer that should be a list, or [] (alpaca/profile.py guards a loaded profile;
    this keeps a raw one honest too)."""
    return list(value) if isinstance(value, (list, tuple)) else []


def _mapping(value) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def payload(conn, root=None) -> dict:
    """The seven-key data.json payload, plus the volatile generation stamp. Rendered from the
    record only; nothing here is stored. The board is folded once and handed to the pulse, so the
    op progress inside `pulse.now` costs no second scan."""
    bview = board.view(conn)
    root = root if root is not None else _root_of(conn)
    return {
        "generated": util.now_iso(),
        "board": bview,
        "messages": _messages(conn),
        "pulse": _pulse(conn, cards=bview["cards"], root=root),
        "gates": _gates(conn),
        "wiki": _wiki(conn),
        "runs": _runs(conn),
        "metrics": _mapping(profile.load(root).metrics(conn)),
    }


def render(root) -> str:
    """Render `data.json` as canonical, stable-ordered JSON with a trailing newline. Byte-stable
    under a fixed clock save for the `generated` stamp, which M2.17 declares volatile."""
    conn = db.connect(root)
    return json.dumps(payload(conn, root=root), indent=1, sort_keys=True) + "\n"


def write(root) -> str:
    """Write `data.json` into the root as a projection."""
    p = os.path.join(root, "data.json")
    util.write_text(p, render(root))
    return p


def write_if_changed(root) -> str | None:
    """Push-data-not-rebuild (Q14): write `data.json` only when the record's fresh render differs
    from what is on disk, modulo the declared volatile stamp lines. Returns the path when it
    wrote, else None. The Stop hook calls this so a turn that changed nothing writes nothing.
    """
    from alpaca import freshness
    p = os.path.join(root, "data.json")
    want = render(root)
    if os.path.isfile(p):
        try:
            disk = util.read_text(p)
        except OSError:
            disk = None
        if disk is not None and freshness.content_hash(disk) == freshness.content_hash(want):
            return None
    util.write_text(p, want)
    return p


# ------------------------------------------------------------------ alpaca export / alpaca deploy
#: the deploy boundary name and the canonical reason its pause leads with, so a caller greps one
#: token. Publishing outside the box is an irreversible external action (a human decision), so
#: `alpaca deploy` never auto-advances; the review card a human moves lands in M3.5.
DEPLOY_GATE = "alpaca-deploy"
DEPLOY_TOKEN = "DEPLOY-NEEDS-HUMAN-DECISION"


def deploy_reason(conn) -> str:
    """The named reason `alpaca deploy` pauses: a human decision is required to publish outside the
    box and none is on the record. Reads the record; asserts the absence it reports."""
    have = 0
    for r in db.rows(conn, "decisions", "1=1"):
        blob = ("%s %s" % (r.get("kind") or "", r.get("body") or "")).lower()
        if "deploy" in blob or "publish" in blob:
            have += 1
    return ("%s: deploy publishes outside the box, an irreversible external action that stays a "
            "human decision; no human decision row for the deploy is on the record (%d found), so "
            "this pauses for review rather than advancing" % (DEPLOY_TOKEN, have))


def cmd_export(args):
    """`alpaca export`: render and write ONE `data.json` from the record."""
    from alpaca.gates import verdict as vc
    from alpaca import paths
    root = paths.root()
    p = write(root)
    return vc.emit_verdict("alpaca-export", vc.PASS, "wrote data.json from the record",
                           evidence=[p])


def cmd_deploy(args):
    """`alpaca deploy`: the deploy barrier. Exit 3 PAUSED with a named reason.

    M3.5 wiring (Step 4): the pause is now carried by a REAL review card rather than an
    unconditional refusal. Publishing outside the box stays a human decision, so `alpaca deploy` STILL
    exits 3 (PAUSED-FOR-DECISION) -- the card is what a human moves, and until then the deploy does
    not advance. Raising the card is idempotent (review dedupes on the open subject), so repeated
    deploys reuse the one open card and never a decisions-table row is conjured.
    """
    from alpaca.gates import verdict as vc
    from alpaca import paths
    root = paths.root()
    conn = db.connect(root)
    try:
        from alpaca import review
        review.ship_card(conn, "deploy", deploy_reason(conn), session=args.session or "cli")
    except Exception:
        pass                                # the card is the surface; the pause itself is the gate
    return vc.emit_verdict(DEPLOY_GATE, vc.PAUSED, deploy_reason(conn))


def _parser(sub):
    sub.add_parser("export", help="render one data.json from the record (a projection, never a store)")
    sub.add_parser("deploy", help="publish outside the box: a human decision, so this PAUSES (exit 3)")


def _register():
    from alpaca import cli
    cli.command("export")(cmd_export)
    cli.command("deploy")(cmd_deploy)
    cli.register_parser("export", _parser)


_register()
