"""The board: a kanban view derived purely over obligation rows (M2.3).

The board is a VIEW, never a store (`doctrine/leaves/board-is-truth.md`). Every column is DERIVED, latest-wins,
from two sources and nothing else:

  * the row's folded status (`verdict_row.status_fold`, itself a fold of the row's verdict
    rows), and
  * the live claims on the row.

The mapping, exactly as the spec states it:

    open                      -> todo
    open with a live claim    -> doing
    discharged or waived      -> done
    blocked or failed         -> blocked (with the reason from the latest verdict row)

Because the column is derived, a hand-written `status` column on the row is IGNORED: the
view re-reads the verdict rows, so a column can never be a claim that outlives its evidence.

A card move is one `alpaca` verb = one event (spec: "nobody edits the board by hand; the web is
read-only"). `move(conn, row_id, column, actor, reason)` translates a target column into the
single record event that MAKES the derived column that value:

    doing    -> a claim event                       (board.claim)
    todo     -> a claim-release event               (board.release)
    done     -> a PASS verdict row                  (verdict_row.discharge)
    blocked  -> a BLOCKED verdict row + its reason  (verdict_row author)

Claims here are the board's minimal, event-derived notion (a claim event with a lease, ended
by a release event or by lease expiry). M2.5 formalizes claims/leases/takeover keyed on the
worker; it depends on this task and writes the same write-ahead claim event, so the board
keeps deriving `doing` whether the claim came from the board or from M2.5.

Interface (M2.3), consumed by the export (M2.18) and the review card (M3.5):

    view(conn, op=None) -> dict
    move(conn, row_id, column, actor, reason) -> event
"""
from __future__ import annotations

import datetime
import json
import os

from alpaca import db, render, util
from alpaca.checklist import Halt, supersession, verdict_row
from alpaca.gates import verdict as vc

INSTRUMENT = "alpaca-board"

# The four columns. These strings are the whole column vocabulary; the derivation never
# invents another, and a move to anything else is refused.
TODO = "todo"
DOING = "doing"
DONE = "done"
BLOCKED = "blocked"
COLUMNS = (TODO, DOING, DONE, BLOCKED)

# The board's claim events. A claim opens a live claim on a row; a release ends it. M2.5
# writes these same kinds (write-ahead), so both paths feed one derivation.
CLAIM_KIND = "claim"
RELEASE_KIND = "claim-release"

#: the default lease a board claim carries, in minutes, when the caller names none.
DEFAULT_LEASE_MINUTES = 60

# reason tokens controls bind to.
R_UNKNOWN_COLUMN = "BOARD-UNKNOWN-COLUMN"
R_NO_SUCH_ROW = "BOARD-NO-SUCH-ROW"


# --------------------------------------------------------------------- rows and claims
def _all_rows(conn) -> list:
    return db.rows(conn, "rows", "1=1")


def _head(conn, row_id) -> dict:
    """The authoritative (head-of-supersession) obligation row for `row_id`, or HALT when the id
    was never committed (absence blocks)."""
    current = supersession.head(_all_rows(conn), row_id)
    if current is None:
        raise Halt(vc.BLOCKED, R_NO_SUCH_ROW, "no obligation row with id=%r" % row_id)
    return current


def _lease_until(minutes) -> str:
    """An ISO instant `minutes` from the clock's now. Uses `util.now_iso`, so a FixedClock
    installed under test drives the lease deterministically."""
    base = datetime.datetime.fromisoformat(util.now_iso())
    return (base + datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds")


def live_claims(conn, now=None) -> dict:
    """row_id -> {worker, lease_until, since} for every row that currently holds a LIVE claim.

    A row's claim is the LATEST of its claim / claim-release events (latest-wins); it is live
    only when that latest event is a claim whose lease has not passed `now`. A released claim,
    or one whose lease_until is at or before now, is not live and the row falls back to todo."""
    now = now or util.now_iso()
    latest = {}
    for e in db.events(conn, limit=10 ** 9):            # oldest first; later overwrites
        if e["kind"] not in (CLAIM_KIND, RELEASE_KIND):
            continue
        rid = e["ref"]
        if rid is None:
            continue
        latest[rid] = e
    out = {}
    for rid, e in latest.items():
        if e["kind"] != CLAIM_KIND:
            continue
        lu = e["data"].get("lease_until")
        if lu is not None and lu <= now:
            continue
        out[rid] = {"worker": e["data"].get("worker"), "lease_until": lu, "since": e["ts"]}
    return out


def claim(conn, row_id, worker, session, *, minutes=DEFAULT_LEASE_MINUTES, reason=None) -> dict:
    """Open a live claim on `row_id` for `worker`, one event. The lease expires after `minutes`;
    an expired lease returns the row to todo. HALTs when the row is absent."""
    _head(conn, row_id)
    return db.append_event(
        conn, session=session or worker, actor=worker, kind=CLAIM_KIND,
        op=_head(conn, row_id).get("op"), ref=row_id,
        data={"worker": worker, "lease_until": _lease_until(minutes), "reason": reason})


def release(conn, row_id, worker, session, *, reason=None) -> dict:
    """End any live claim on `row_id`, one event, returning the row to todo. HALTs when the row
    is absent."""
    head = _head(conn, row_id)
    return db.append_event(
        conn, session=session or worker, actor=worker, kind=RELEASE_KIND,
        op=head.get("op"), ref=row_id, data={"worker": worker, "reason": reason})


# --------------------------------------------------------------------- the derivation
def _latest_verdict(conn, row_id, index=None):
    evs = index.get(row_id, []) if index is not None else verdict_row._verdict_events(conn, row_id)
    return evs[-1] if evs else None


def _verdict_summary(ev):
    """The latest verdict row as the card shows it: who discharged the row, when, why, and the
    evidence pointers. A done card carries it too, so a reader sees what was observed and not
    only that something passed. Externally sourced strings cross the JSON render boundary."""
    if not ev:
        return None
    d = ev["data"]
    return {"name": d.get("verdict_name"), "instrument": render.for_json(d.get("instrument")),
            "reason": render.for_json(d.get("reason")),
            "evidence": [render.for_json(p) for p in (d.get("evidence") or [])],
            "level": d.get("level"), "session": render.for_json(ev.get("session")), "ts": ev.get("ts")}


def _column_and_reason(conn, row_id, live, index=None) -> tuple:
    """Derive one row's (column, reason). The status fold is authoritative; the stored `status`
    column on the row is never read here, so a hand-written value cannot move a card."""
    status = verdict_row.status_fold(conn, row_id, index)
    if status in (verdict_row.DISCHARGED, verdict_row.WAIVED_STATUS):
        return DONE, None
    if status in (verdict_row.BLOCKED_STATUS, verdict_row.FAILED):
        ev = _latest_verdict(conn, row_id, index)
        reason = ev["data"].get("reason") if ev else None
        return BLOCKED, reason
    # status is open: a live claim makes it doing, otherwise todo.
    if row_id in live:
        return DOING, None
    return TODO, None


def card_for(conn, row, live, index=None) -> dict:
    """One kanban card. Carries the derived column plus the row's tag (M1.16, the derived
    column), proof, where, why (the decision-page pointer M2.6 fills) and the claimant."""
    rid = row.get("id")
    column, reason = _column_and_reason(conn, rid, live, index)
    held = live.get(rid) or {}
    # M4.9: the externally sourced string fields (statement, proof, where, why, the claimant
    # token, the blocked reason) cross the render boundary on their way onto board.json. board.json
    # is a JSON surface, so the boundary is render.for_json (identity): json.dumps neutralises and
    # json.loads returns the value byte for byte, so the parsed field equals the original.
    return {
        "row_id": rid,
        "op": render.for_json(row.get("op")),
        "phase": row.get("phase"),
        "step": row.get("step"),
        "statement": render.for_json(row.get("statement")),
        "column": column,
        "status": verdict_row.status_fold(conn, rid, index),
        "verdict": _verdict_summary(_latest_verdict(conn, rid, index)),
        "tag": row.get("tag"),
        "proof": render.for_json(row.get("proof")),
        "where": render.for_json(row.get("where_")),
        "why": render.for_json(row.get("why")),
        "claimant": render.for_json(held.get("worker")),
        "reason": render.for_json(reason),
    }


def _heads_only(rows) -> list:
    """The head of every supersession chain: a row that no other row supersedes. A superseded
    original stays in the store as history but never shows on the board."""
    superseded = {r.get("supersedes") for r in rows if r.get("supersedes")}
    return [r for r in rows if r.get("id") not in superseded]


def view(conn, op=None) -> dict:
    """The kanban board as a pure derivation over obligation rows.

    `op=None` is the project board over every op; an `op` filters to that op's board. The
    return carries the flat card list, the cards bucketed by column, and one swimlane per
    phase (each swimlane again bucketed by column). Nothing here is stored as a column."""
    live = live_claims(conn)
    rows = _heads_only(_all_rows(conn))
    rows = [r for r in rows if (r.get("kind") or "item") != verdict_row.KIND]
    if op is not None:
        rows = [r for r in rows if r.get("op") == op]
    rows = sorted(rows, key=lambda r: str(r.get("id")))
    index = verdict_row.verdict_index(conn)
    cards = [card_for(conn, r, live, index) for r in rows]

    def _bucket(subset) -> dict:
        b = {c: [] for c in COLUMNS}
        for card in subset:
            b[card["column"]].append(card)
        return b

    phases = sorted({(c["phase"] or "") for c in cards})
    swimlanes = [{"phase": ph or None, "by_column": _bucket([c for c in cards if (c["phase"] or "") == ph])}
                 for ph in phases]
    return {
        "op": op,
        "columns": list(COLUMNS),
        "ops": sorted({c["op"] for c in cards if c["op"] is not None}),
        "cards": cards,
        "by_column": _bucket(cards),
        "counts": {c: len(v) for c, v in _bucket(cards).items()},
        "swimlanes": swimlanes,
    }


# --------------------------------------------------------------------- the card move
def move(conn, row_id, column, actor, reason=None, *, proof=None, level=None, session=None,
         minutes=DEFAULT_LEASE_MINUTES) -> dict:
    """Move a card to `column`, as one event that makes the derived column that value.

    doing   claims the row for `actor`; todo releases the claim; done authors a PASS verdict
    row; blocked authors a BLOCKED verdict row carrying `reason`. done and blocked bind to the
    row's current content_hash and HALT on drift or an absent row. An unknown column is refused.
    """
    if column not in COLUMNS:
        raise Halt(vc.BLOCKED, R_UNKNOWN_COLUMN,
                   "%r is not a board column; use one of %s" % (column, ", ".join(COLUMNS)))
    session = session or actor
    if column == DOING:
        return claim(conn, row_id, actor, session, minutes=minutes, reason=reason)
    if column == TODO:
        return release(conn, row_id, actor, session, reason=reason)
    head = _head(conn, row_id)
    content_hash = head.get("content_hash")
    evidence = [proof] if proof else []
    if column == DONE:
        return verdict_row.discharge(conn, row_id, content_hash, INSTRUMENT, vc.PASS,
                                     evidence, level, session)
    # column == BLOCKED: author a BLOCKED verdict that carries the reason for the card.
    current = verdict_row._bind_or_halt(conn, row_id, content_hash)
    return verdict_row._author(conn, row_id, current, INSTRUMENT, vc.BLOCKED, evidence,
                               level, session, reason=reason)


# --------------------------------------------------------------------- review cards (M3.5)
def review_cards(conn, budget=None) -> dict:
    """The review queue over the record, surfaced under a `budget` (M3.5). The board consumes the
    review card here: a card is a human decision owed, kept apart from the derived kanban columns.
    Delegates to `alpaca.review.queue`, so the conservation law (surfaced + deferred == all) holds and
    no card is dropped by capping attention. Never modifies the derived board view."""
    from alpaca import review
    return review.queue(conn, budget=budget)


# --------------------------------------------------------------------- board.json projection
def render_json(conn, *, op=None, generated=None) -> str:
    """Render the board as a projection: canonical, stable-ordered JSON. Rendered from the
    record, never hand-edited (the store is the only truth). M2.17 registers this projection
    with the freshness gate; this task only renders it."""
    v = view(conn, op)
    v["generated"] = generated or util.now_iso()
    return json.dumps(v, indent=1, sort_keys=True)


def write_json(root) -> str:
    """Write `board.json` (the project board) into the root as a projection."""
    conn = db.connect(root)
    p = os.path.join(root, "board.json")
    util.write_text(p, render_json(conn) + "\n")
    return p


# --------------------------------------------------------------------- text render
def render_text(v) -> str:
    lines = ["# board%s" % ("" if v["op"] is None else " for %s" % v["op"])]
    lines.append("columns: " + "  ".join("%s=%d" % (c, v["counts"][c]) for c in v["columns"]))
    for lane in v["swimlanes"]:
        lines.append("")
        lines.append("## phase %s" % (lane["phase"] or "-"))
        for col in v["columns"]:
            for card in lane["by_column"][col]:
                tail = (" (%s)" % card["reason"]) if card["reason"] else ""
                who = (" @%s" % card["claimant"]) if card["claimant"] else ""
                lines.append("  [%-7s] %s %s%s%s" % (col, card["row_id"],
                             card["tag"] or "-", who, tail))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------- CLI boundary
def _cmd_board(args):
    from alpaca import cli
    conn = db.connect(cli._root())
    sid = args.session or "cli"
    if args.board_verb == "show":
        v = view(conn, args.op)
        if args.json:
            print(render_json(conn, op=args.op))
        else:
            print(render_text(v), end="")
        try:
            write_json(cli._root())
        except Exception:
            pass
        return vc.PASS
    if args.board_verb == "move":
        # E4: a MANUAL done move over the CLI carries the same sealed proof report a task does.
        # board.move() itself is untouched, so script discharge keeps working as it does today.
        if args.column == DONE:
            from alpaca import proof
            ok, problems = proof.done_gate(conn, cli._root(), args.row, args.proof)
            if not ok:
                proof.refuse_done("alpaca-board-move", args.row, problems)
                return vc.FAIL
        try:
            move(conn, args.row, args.column, args.by or "cli", reason=args.reason,
                 proof=args.proof, level=args.level, session=sid)
        except Halt as h:
            return vc.emit_verdict("alpaca-board-move", h.verdict, "%s: %s" % (h.code, h.detail))
        return vc.emit_verdict("alpaca-board-move", vc.PASS, "%s -> %s" % (args.row, args.column),
                               evidence=[args.row])
    print("GATE alpaca-board: BLOCKED (unknown board verb; use show or move)")
    return vc.BLOCKED


def _parser(sub):
    b = sub.add_parser("board")
    bv = b.add_subparsers(dest="board_verb")
    sh = bv.add_parser("show")
    sh.add_argument("--op")
    sh.add_argument("--json", action="store_true")
    mv = bv.add_parser("move")
    mv.add_argument("row")
    mv.add_argument("column", choices=list(COLUMNS))
    mv.add_argument("--by")
    mv.add_argument("--reason")
    mv.add_argument("--proof")
    mv.add_argument("--level")


def _register():
    from alpaca import cli
    cli.command("board")(_cmd_board)
    cli.register_parser("board", _parser)


_register()
