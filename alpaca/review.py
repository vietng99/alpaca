"""Review cards, the review budget, and curation by exception (M3.5).

Sources: spec 5.7:466-470 (the review card), Q18 (spec:188), the 7.4 table (spec:775-786), rule 8
(spec:816-817); absorb-gap AG-M15 (a human-review budget with a conservation law and curation by
exception). Alpaca-native (no vendoring); the model is built here against the M2/M3 pieces.

A review card is raised when, and only when, something needs a HUMAN decision. A card is not a
routine notification: it exists BY EXCEPTION. The four card classes the plan names are the four
things that stay human decisions at every level (7.4, rule 8):

  * `ship`               -- the ship / deploy boundary: publishing outside the box.
  * `irreversible-external` -- an irreversible external action (push a shared remote, spend money).
  * `human-owned-write`  -- a write to the authority surface below L5 (floor.py hands this over).
  * `owner-pause`        -- an owner-only question is open (questions.py hands this over).

Three instruments, all over the record (alpaca is the sole writer):

  * `card(conn, kind, subject, reason)` raises one open card, or returns the existing OPEN card
    for the same (kind, subject). Card creation NEVER consults a budget: a card is raised because
    something needs a decision, and capping attention must never lose one (Step 2). Dedupe on the
    open (kind, subject) is how each class "produces exactly one card".
  * `queue(conn, budget)` returns `{"surfaced", "deferred", "all"}` over the OPEN cards. The
    budget caps ATTENTION only: it decides which cards are surfaced now and which are deferred, and
    it obeys a conservation law -- `len(surfaced) + len(deferred) == len(all)` -- so an over-budget
    queue defers rather than drops. The budget lives on the queue, never on `card` (Step 2, 3).
  * `move(conn, card_id, decision, actor)` resolves a card. ONLY A HUMAN IDENTITY may move a card
    (Step 1): an agent move is refused FAIL, exactly as the questions ledger refuses an owner-class
    self-decision. A moved card leaves the open queue (it no longer needs a decision).

The current state is the fold of the card / move events, last-appended-wins per id, the same fold
the questions ledger uses. There is no separate board column a hand-edit could set.

HONEST LIMITS: `actor` is a SELF-ASSERTED token, exactly as in alpaca.questions and alpaca.decisions:
"owner"/"human" here means the caller SAID a human, never a proof a human authored the move.
Binding a move to a person needs an external identity in front of `move`. "Surfaced" is the
caller's job (the pad, the page); this module guarantees only that a card cannot be moved by a
non-human token and that the budget never drops a card.
"""
from __future__ import annotations

from collections import OrderedDict

from alpaca import db, util
from alpaca.gates import verdict as vc

KIND_CARD = "review-card"          # the record channel that raises a card
KIND_MOVE = "review-move"          # the record channel that resolves a card

#: the four card classes (7.4, rule 8). Each is one of the four things that stays a human decision
#: at every autodrive level; a card of an unnamed kind is refused (the floor is a floor).
SHIP = "ship"
IRREVERSIBLE = "irreversible-external"
HUMAN_OWNED_WRITE = "human-owned-write"
OWNER_PAUSE = "owner-pause"
CARD_KINDS = frozenset({SHIP, IRREVERSIBLE, HUMAN_OWNED_WRITE, OWNER_PAUSE})

#: a card's status. It is either open (needs a decision) or moved (a human decided it).
OPEN = "open"
MOVED = "moved"

#: the identities that count as a human at the move boundary. Self-asserted (see HONEST LIMITS);
#: an agent token ("agent", "harness", "instrument", "cli") is not among them and cannot move.
HUMAN_IDENTITIES = frozenset({"owner", "human"})

# reason tokens -- controls bind to these exact strings, never to prose.
R_KIND_UNKNOWN = "REVIEW-KIND-UNKNOWN"
R_SUBJECT_EMPTY = "REVIEW-SUBJECT-EMPTY"
R_NOT_FOUND = "REVIEW-CARD-NOT-FOUND"
R_ALREADY_MOVED = "REVIEW-CARD-ALREADY-MOVED"
R_DECISION_EMPTY = "REVIEW-DECISION-EMPTY"
R_AGENT_MOVE = "REVIEW-MOVE-NOT-A-HUMAN"


class ReviewRefusal(Exception):
    """A refusal carrying its integer verdict-band code and its EXACT reason token, exactly as
    alpaca.questions.QuestionRefusal does. The verdict numbers are referenced from alpaca.gates.verdict,
    never restated here. Controls bind to `.verdict` and `.reason`; `.detail` is the human pointer.
    """

    def __init__(self, verdict_code: int, reason: str, detail: str = ""):
        super().__init__("%s %s: %s" % (vc.name_of(verdict_code), reason, detail))
        if verdict_code not in vc.VERDICT_BAND:
            raise ValueError("a refusal needs a verdict-band code, got %r" % (verdict_code,))
        self.verdict = verdict_code
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------- record read / fold
def _events(conn) -> list:
    """Every card / move event in append order (oldest first), data already decoded."""
    out = []
    for kind in (KIND_CARD, KIND_MOVE):
        out.extend(db.events(conn, kind=kind, limit=10 ** 9))
    out.sort(key=lambda e: e["id"])
    return out


def _state(conn) -> "OrderedDict[str, dict]":
    """Fold the card / move events to current state: {id -> latest record}, last-appended wins.

    A missing / empty ledger is an EMPTY state, never an error: a clean op that needed no decision
    has banked no card, which is exactly zero cards, not a verification over an empty population.
    A move event updates the card it names; an out-of-order move with no card is ignored.
    """
    st = OrderedDict()
    for e in _events(conn):
        rec = dict(e["data"])
        cid = rec.get("id")
        if cid is None:
            continue
        if e["kind"] == KIND_CARD:
            st[cid] = rec
        else:  # KIND_MOVE: merge the resolution onto the existing card
            base = st.get(cid)
            if base is None:
                continue
            merged = dict(base)
            merged.update(rec)
            st[cid] = merged
    return st


def _next_id(state) -> str:
    return "rc-%03d" % (len(state) + 1)


def _append(conn, kind, rec, *, actor, session) -> dict:
    """Append one hash-chained review event inside its own transaction. alpaca is the sole writer; the
    event and nothing else land together -- the fold over the events IS the current state."""
    with db.transaction(conn):
        db.append_event(conn, session=session or "instrument", actor=actor or "instrument",
                        kind=kind, op=rec.get("op"), ref=rec["id"], data=dict(rec),
                        conn_in_txn=True)
    return rec


# --------------------------------------------------------------------- card / open / all
def all_cards(conn) -> list:
    """Every card, in id order, at its current folded state (open or moved)."""
    return sorted(_state(conn).values(), key=lambda r: str(r.get("id")))


def open_cards(conn) -> list:
    """The OPEN cards -- the queue: exactly the cards that still need a decision (curate by
    exception). A moved card has left the queue. Ordered by (created, id) so the surface is stable.
    """
    opens = [r for r in _state(conn).values() if r.get("status") == OPEN]
    return sorted(opens, key=lambda r: (str(r.get("created") or ""), str(r.get("id"))))


def _open_for(state, kind, subject) -> dict | None:
    for r in state.values():
        if r.get("status") == OPEN and r.get("kind") == kind and r.get("subject") == subject:
            return r
    return None


def card(conn, kind, subject, reason, *, op=None, actor="agent", session=None) -> dict:
    """Raise one open review card, or return the existing OPEN card for the same (kind, subject).

    NEVER consults a budget: a card is raised because something needs a human decision, and capping
    attention (the queue's job) must lose no card. Dedupe on the open (kind, subject) is how each
    class produces exactly one card and a repeat request does not pile up duplicates.
    """
    if kind not in CARD_KINDS:
        raise ReviewRefusal(vc.BLOCKED, R_KIND_UNKNOWN,
                            "%r is not a review card class; use one of %s"
                            % (kind, ", ".join(sorted(CARD_KINDS))))
    subject = "" if subject is None else str(subject)
    if not subject.strip():
        raise ReviewRefusal(vc.BLOCKED, R_SUBJECT_EMPTY, "a review card needs a subject")
    subject = subject.strip()
    state = _state(conn)
    existing = _open_for(state, kind, subject)
    if existing is not None:
        return existing                                    # curate by exception: one open card
    cid = _next_id(state)
    rec = {
        "id": cid,
        "kind": kind,
        "subject": subject,
        "reason": "" if reason is None else str(reason),
        "op": op,
        "status": OPEN,
        "decision": "",
        "moved_by": "",
        "created": util.now_iso(),
    }
    return _append(conn, KIND_CARD, rec, actor=actor, session=session)


# --------------------------------------------------------------------- the queue + budget
def queue(conn, budget=None) -> dict:
    """Surface the open cards under a budget, deferring the rest. Conservation law (Step 2):
    `len(surfaced) + len(deferred) == len(all)`, so an over-budget queue DEFERS rather than DROPS.

    `budget` caps ATTENTION only: the first `budget` open cards (oldest first) are surfaced and the
    rest deferred. A `budget` of None (or negative) is unbounded: all surfaced, none deferred. The
    budget lives here, never on `card`, so nothing is lost by capping attention.
    """
    allc = open_cards(conn)
    if budget is None or budget < 0:
        return {"surfaced": list(allc), "deferred": [], "all": list(allc)}
    n = min(int(budget), len(allc))
    return {"surfaced": allc[:n], "deferred": allc[n:], "all": list(allc)}


# --------------------------------------------------------------------- move (human-only)
def is_human(actor) -> bool:
    """True iff `actor` is a human identity (self-asserted; see HONEST LIMITS)."""
    return str(actor or "").strip() in HUMAN_IDENTITIES


def move(conn, card_id, decision, actor, *, session=None) -> dict:
    """Resolve a review card. ONLY A HUMAN IDENTITY may move it (Step 1): an agent move is refused
    FAIL, exactly as the questions ledger refuses an owner-class self-decision. An unknown card, an
    already-moved card, or an empty decision is refused. A moved card leaves the open queue.
    """
    if not is_human(actor):
        raise ReviewRefusal(
            vc.FAIL, R_AGENT_MOVE,
            "a review card is a human decision; %r is not a human identity (%s) and may not move "
            "it -- surface it and await a human" % (actor, ", ".join(sorted(HUMAN_IDENTITIES))))
    rec = _state(conn).get(str(card_id))
    if rec is None:
        raise ReviewRefusal(vc.BLOCKED, R_NOT_FOUND, "no review card with id=%r" % card_id)
    if rec.get("status") == MOVED:
        raise ReviewRefusal(vc.BLOCKED, R_ALREADY_MOVED,
                            "%s is already moved; a new decision is a new card" % card_id)
    if decision is None or not str(decision).strip():
        raise ReviewRefusal(vc.BLOCKED, R_DECISION_EMPTY,
                            "%s: a move carries a non-empty decision" % card_id)
    new = dict(rec)
    new.update(status=MOVED, decision=str(decision).strip(), moved_by=str(actor).strip(),
               moved_at=util.now_iso())
    return _append(conn, KIND_MOVE, new, actor=actor, session=session)


# --------------------------------------------------------------------- bridges to the sources
def sync_owner_pauses(conn, phase=None, *, session=None) -> list:
    """Raise one owner-pause card per OPEN owner-owed question (the owner-only pause class). The
    subject is the question id, so the dedupe keeps it one card per question. Returns the cards
    raised or already standing. This is the questions -> review bridge (Step 4)."""
    from alpaca import questions
    made = []
    for spec in questions.owner_pause_cards(conn, phase=phase):
        made.append(card(conn, OWNER_PAUSE, spec["subject"], spec.get("reason", ""),
                         actor="instrument", session=session))
    return made


def ship_card(conn, subject, reason, *, op=None, session=None) -> dict:
    """The ship / deploy boundary card: publishing outside the box is a human decision (Step 4)."""
    return card(conn, SHIP, subject, reason, op=op, actor="instrument", session=session)


# --------------------------------------------------------------------- CLI: alpaca review list|move
def _cmd_review(args):
    from alpaca import cli
    conn = db.connect(cli._root())
    sid = args.session or "cli"
    if args.review_verb == "list":
        q = queue(conn, budget=args.budget)
        if getattr(args, "json", False):
            import json
            print(json.dumps({
                "surfaced": q["surfaced"], "deferred": q["deferred"],
                "counts": {"surfaced": len(q["surfaced"]), "deferred": len(q["deferred"]),
                           "all": len(q["all"])}}, indent=1, sort_keys=True))
        else:
            print("# review queue (surfaced=%d deferred=%d all=%d)"
                  % (len(q["surfaced"]), len(q["deferred"]), len(q["all"])))
            for c in q["surfaced"]:
                print("  [surfaced] %s %s %s" % (c["id"], c["kind"], c["subject"]))
            for c in q["deferred"]:
                print("  [deferred] %s %s %s" % (c["id"], c["kind"], c["subject"]))
        return vc.PASS
    if args.review_verb == "move":
        try:
            rec = move(conn, args.card_id, args.decision, args.by, session=sid)
        except ReviewRefusal as e:
            return vc.emit_verdict("alpaca-review-move", e.verdict, "%s: %s" % (e.reason, e.detail))
        return vc.emit_verdict("alpaca-review-move", vc.PASS,
                               "%s moved (%s) by %s" % (rec["id"], rec["decision"], rec["moved_by"]),
                               evidence=[rec["id"]])
    print("GATE alpaca-review: BLOCKED (unknown review verb; use list or move)")
    return vc.BLOCKED


def _parser(sub):
    r = sub.add_parser("review", help="review cards: the human-decision queue (curate by exception)")
    rv = r.add_subparsers(dest="review_verb")
    lst = rv.add_parser("list", help="the review queue (surfaced under a budget, deferred not dropped)")
    lst.add_argument("--budget", type=int, default=None,
                     help="cap attention: surface this many, defer the rest (never drops a card)")
    lst.add_argument("--json", action="store_true")
    mv = rv.add_parser("move", help="move a card (only a human identity may move it)")
    mv.add_argument("card_id", help="the review card id")
    mv.add_argument("decision", help="the decision recorded on the card")
    mv.add_argument("--by", required=True,
                    help="the mover (a human identity: %s)" % ", ".join(sorted(HUMAN_IDENTITIES)))


def _register():
    from alpaca import cli
    cli.command("review")(_cmd_review)
    cli.register_parser("review", _parser)


_register()
