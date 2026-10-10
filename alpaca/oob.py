"""The L5 out-of-brief card (op-019 t-202, design section 6): at L5 a choice nothing the owner signed
covers stops for the owner; at L6 it is a derived choice and nothing stops (brief D1).

Two doors raise a card of kind `out-of-brief` (alpaca.review):

  * a door self-check submitted at L5 (alpaca.selfcheck.submit) raises one card per finding, subject
    `<op>:<boundary>:<selfcheck event id>:<n>`. The card pauses that door and the transition behind it
    (alpaca.selfcheck.check), nothing else.
  * `alpaca choice <task> --text T --outside` between doors raises one card with subject the task id.
    The task waits: `task_gate` refuses `task claim` and `task move` to doing, review or done while the
    card is open, and after the owner rejects it until `choice <task> --withdraw <card>` records that
    the agent took the choice back. Other tasks go on.

`--covered-by REF` records the agent's claimed basis (an owner-quote source, a decision id or a batch
item id) as a `choice` event; `owner_items` lists it in the next self-check packet's owner.json, where
the checker re-reads it. A rejected card goes into owner.json too, so a deliverable that still makes the
rejected choice is a mismatch at the next self-check.

Only the owner moves a card, with the owner's own words (alpaca.review.move, alpaca.ownerword). An agent
that moves, answers or dismisses a card is refused there; this module never moves one.

HONEST LIMIT: `--covered-by` is a claim, not a proof. The record cannot tell that REF covers the choice;
the next door self-check reads it against the brief and finds a mismatch when it does not.
"""
from __future__ import annotations

from alpaca import db, ownerword, review
from alpaca.gates import verdict as vc

CHOICE_KIND = "choice"
DERIVED_KIND = "derived-choice"

R_OPEN = "OUT-OF-BRIEF-CARD-OPEN"
R_CHOICE = "OUT-OF-BRIEF-CHOICE"

ACCEPT, REJECT = "accept", "reject"


class ChoiceError(ValueError):
    """A refused `alpaca choice` call; the message names what is wrong."""


# ------------------------------------------------------------------ the cards
def cards(conn) -> list:
    """Every out-of-brief card at its current state, in id order."""
    return [c for c in review.all_cards(conn) if c.get("kind") == review.OUT_OF_BRIEF]


def door_parts(subject):
    """(op, boundary, selfcheck event id, finding index) of a door card subject, else None (a task
    card's subject is the task id)."""
    from alpaca.phase import defaults
    parts = str(subject or "").split(":")
    if len(parts) != 4 or parts[1] not in defaults.BOUNDARY_PHASE:
        return None
    try:
        return parts[0], parts[1], int(parts[2]), int(parts[3])
    except ValueError:
        return None


def door_cards(conn, op, boundary) -> list:
    """The door cards of (op, boundary), from any self-check, as (card, event id, index) triples."""
    out = []
    for c in cards(conn):
        d = door_parts(c.get("subject"))
        if d and d[0] == op and d[1] == boundary:
            out.append((c, d[2], d[3]))
    return out


def open_door_cards(conn, op, boundary) -> list:
    return [c for c, _e, _k in door_cards(conn, op, boundary) if c.get("status") == review.OPEN]


def rejected_door_findings(conn, op, boundary) -> set:
    """{(selfcheck event id, finding index)} of the door cards the owner rejected."""
    return {(e, k) for c, e, k in door_cards(conn, op, boundary)
            if c.get("status") == review.MOVED and c.get("decision") == REJECT}


def task_cards(conn, task_id) -> list:
    """The cards raised by `choice <task> --outside`: subject the task id."""
    return [c for c in cards(conn) if c.get("subject") == task_id and not door_parts(c.get("subject"))]


def choice_events(conn, task_id=None, op=None) -> list:
    """The `choice` events (covered-by claims, outside choices, withdrawals), oldest first, as dicts."""
    q, p = "SELECT id, ts, op, ref, data FROM events WHERE kind=?", [CHOICE_KIND]
    if task_id:
        q += " AND ref=?"
        p.append(task_id)
    if op:
        q += " AND op=?"
        p.append(op)
    import json
    out = []
    for row in conn.execute(q + " ORDER BY id", p):
        try:
            data = json.loads(row["data"]) if row["data"] else {}
        except ValueError:
            data = {}
        out.append(dict(data, event_id=row["id"], ts=row["ts"], op=row["op"], task=row["ref"]))
    return out


def _withdrawn(conn, task_id) -> set:
    return {e.get("card") for e in choice_events(conn, task_id) if e.get("basis") == "withdraw"}


# ------------------------------------------------------------------ raising
def raise_door_card(conn, op, boundary, event_id, index, finding, *, session=None) -> dict:
    """One card for one finding of the self-check submitted at L5: the deliverable quote and what it
    misses. Called by alpaca.selfcheck.submit once the selfcheck event has its id."""
    where = finding.get("path") or finding.get("item") or "?"
    reason = "%s finding in %s: %r. It misses: %s" % (finding.get("kind"), where,
                                                       str(finding.get("quote"))[:200],
                                                       finding.get("reason"))
    return review.card(conn, review.OUT_OF_BRIEF,
                       "%s:%s:%d:%d" % (op, boundary, event_id, index), reason, op=op,
                       actor=ownerword.session_actor(session), session=session)


def _task_row(conn, task_id):
    rows = db.rows(conn, "tasks", "id=?", (task_id,))
    if not rows:
        raise ChoiceError("no task %s" % task_id)
    return rows[0]


def choice(conn, session, task_id, *, text=None, covered_by=None, outside=False, withdraw=None,
           level=None, root=None) -> dict:
    """Record one choice of `task_id`. Exactly one of `covered_by`, `outside`, `withdraw`.

    covered_by: a `choice` event naming the claimed basis; no card. outside: at L5 one out-of-brief
    card (subject the task id) and a `choice` event; at L6 a derived-choice event and no card (brief
    D1). withdraw (a card id): the agent took the rejected choice back; only a card of this task that
    the owner rejected can be withdrawn, and it frees the task. Returns {"event", "card"}."""
    task = _task_row(conn, task_id)
    op = task["op"]
    if sum(bool(x) for x in (covered_by, outside, withdraw)) != 1:
        raise ChoiceError("choose exactly one of --covered-by, --outside, --withdraw")
    actor = ownerword.session_actor(session)
    if withdraw:
        card = next((c for c in task_cards(conn, task_id) if c["id"] == withdraw), None)
        if card is None:
            raise ChoiceError("%s is not an out-of-brief card of %s" % (withdraw, task_id))
        if card.get("status") != review.MOVED or card.get("decision") != REJECT:
            raise ChoiceError("%s is not rejected (it is %s): only the owner's no is withdrawn from"
                              % (withdraw, card.get("decision") or card.get("status")))
        ev = db.append_event(conn, session=session, actor=actor, kind=CHOICE_KIND, op=op,
                             ref=task_id, data={"task": task_id, "op": op, "basis": "withdraw",
                                                "card": withdraw, "text": (text or "").strip()})
        return {"event": ev, "card": card}
    if not (text or "").strip():
        raise ChoiceError("--text is required: say what was chosen")
    text = text.strip()
    if covered_by:
        ev = db.append_event(conn, session=session, actor=actor, kind=CHOICE_KIND, op=op,
                             ref=task_id, data={"task": task_id, "op": op, "basis": "covered-by",
                                                "covered_by": str(covered_by).strip(), "text": text})
        return {"event": ev, "card": None}
    if level is None:
        from alpaca import intentgate
        level = intentgate.level(conn, session, root=root)
    if level == 5:
        prior = next((c for c in task_cards(conn, task_id) if c.get("status") == review.OPEN), None)
        if prior is not None and (prior.get("reason") or "") != text:
            # review.card hands back the open card of the same subject; the owner's answer to the
            # text he saw must not also clear a different choice he never saw
            raise ChoiceError("%s already waits on open card %s for a different choice (%r); the "
                              "owner answers it first, then record this choice"
                              % (task_id, prior["id"], prior.get("reason")))
        card = review.card(conn, review.OUT_OF_BRIEF, task_id, text, op=op, actor=actor,
                           session=session)
        ev = db.append_event(conn, session=session, actor=actor, kind=CHOICE_KIND, op=op,
                             ref=task_id, data={"task": task_id, "op": op, "basis": "outside",
                                                "card": card["id"], "text": text, "level": level})
        return {"event": ev, "card": card}
    ev = db.append_event(conn, session=session, actor=actor, kind=DERIVED_KIND, op=op,
                         ref="%s:%s" % (op, task_id),
                         data={"op": op, "task": task_id, "kind": "choice", "reason": text,
                               "quote": text, "level": level})
    return {"event": ev, "card": None}


# ------------------------------------------------------------------ the provider (intentgate)
def task_gate(conn, task, action, *, level, session, root):
    """At L5 refuse `task claim` and `task move` to doing, review or done while an out-of-brief card
    names the task: open (the owner has not answered), or rejected and not yet withdrawn (the agent
    still owes the owner the choice taken back). Other tasks go on. Not asked at L6 (brief D1)."""
    if level != 5:
        return
    from alpaca.intentgate import IntentGateRefusal
    gone = _withdrawn(conn, task.get("id"))
    for c in task_cards(conn, task.get("id")):
        if c.get("status") == review.OPEN:
            raise IntentGateRefusal(R_OPEN, "%s of %s waits for the owner: card %s is open (%s); "
                                    "the owner answers with `alpaca review move %s --accept|--reject"
                                    % (action, task.get("id"), c["id"], c.get("reason"), c["id"]))
        if c.get("decision") == REJECT and c["id"] not in gone:
            raise IntentGateRefusal(R_OPEN, "%s of %s waits: the owner rejected card %s (%s); take "
                                    "the choice back, then `alpaca choice %s --withdraw %s`"
                                    % (action, task.get("id"), c["id"], c.get("reason"),
                                       task.get("id"), c["id"]))


def owner_items(conn, op, *, root=None) -> list:
    """For the self-check packet's owner.json: the owner's rejected cards of the op and the agent's
    covered-by claims, so a deliverable that still makes a rejected choice is a mismatch and a claimed
    basis is read against the brief."""
    out = []
    for c in cards(conn):
        if c.get("op") == op and c.get("status") == review.MOVED and c.get("decision") == REJECT:
            word = c.get("owner_word") or {}
            out.append({"id": "card:%s" % c["id"], "kind": "rejected-choice", "card": c["id"],
                        "subject": c.get("subject"), "text": c.get("reason") or "",
                        "decision": REJECT, "owner_quote": word.get("quote") or ""})
    for e in choice_events(conn, op=op):
        if e.get("basis") == "covered-by":
            out.append({"id": "choice:%s" % e["event_id"], "kind": "covered-by-claim",
                        "task": e["task"], "text": e.get("text") or "",
                        "covered_by": e.get("covered_by") or ""})
    return out


# ------------------------------------------------------------------ CLI: alpaca choice
def _cmd_choice(args):
    from alpaca import cli
    root = cli._root()
    conn = db.connect(root)
    sid = args.session or "cli"
    try:
        res = choice(conn, sid, args.task, text=args.text, covered_by=args.covered_by,
                     outside=args.outside, withdraw=args.withdraw, root=root)
    except ChoiceError as exc:
        print("GATE alpaca-choice: FAIL (%s: %s)" % (R_CHOICE, exc))
        return cli.FAIL
    card = res["card"]
    if args.withdraw:
        print("%s: choice withdrawn (card %s)" % (args.task, args.withdraw))
    elif args.covered_by:
        print("%s: choice recorded as covered by %s; the next self-check reads it"
              % (args.task, args.covered_by))
    elif card is not None:
        print("GATE alpaca-choice: PAUSED-FOR-DECISION (%s waits for the owner: card %s)"
              % (args.task, card["id"]))
        return vc.PAUSED
    else:
        print("%s: out-of-brief choice recorded as a derived choice (L6: nothing stops for the "
              "owner; `alpaca selfcheck derived` lists it)" % args.task)
    return cli.PASS


def _parser(sub):
    c = sub.add_parser("choice", help="record a choice of a task: covered by the brief, outside it, "
                                      "or taken back")
    c.add_argument("task")
    c.add_argument("--text", help="what was chosen")
    how = c.add_mutually_exclusive_group(required=True)
    how.add_argument("--covered-by", dest="covered_by",
                     help="the basis: an owner-quote source, a decision id or a batch item id")
    how.add_argument("--outside", action="store_true",
                     help="nothing signed covers it: L5 raises an owner card, L6 a derived choice")
    how.add_argument("--withdraw", metavar="CARD",
                     help="the owner rejected this card; the choice is taken back")


def _register():
    from alpaca import cli
    cli.command("choice")(_cmd_choice)
    cli.register_parser("oob", _parser)


_register()
