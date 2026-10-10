"""Owner orders: a standing instruction the owner gave, kept in the record until the owner lifts it
(op-019 t-210, design design/intent-fidelity/design.md section 4, brief P6 and O1).

An order the agent only remembers is an order the next session loses (brief R4). So an order is a
record row: an `owner-order` event with id `ord-N`, the op, the owner's quoted words, the door it
binds (a boundary of the phase ladder or `any`) and `owner_word`, the pointer to the transcript line
that proves the owner typed it. Only the owner closes it: `order close` goes through
ownerword.human_write in the id form, so an agent cannot close an order without the owner's message.

An open order applies at every level, L6 included:
  * door link `owner-orders` is PAUSED while an open order of the op binds the boundary or `any`;
  * `task_gate` refuses claim and move of a task whose phase comes after the order's boundary, for
    ops that have a batch (design D-1);
  * `msg post --kind handoff` and `order check-note` refuse a note that does not name every open
    order of the op (ORDER-DROPPED), so a handoff cannot drop an order;
  * the pad lists the open orders in the op's resume entry.

Record shape (all appended through db.append_event, never edited):
  owner-order        ref ord-N, op; data {id, op, quote, door, by, owner_word, closed?, lifted_by?}
  owner-order-close  ref ord-N, op; data {id, by, owner_word}
  derived-choice     op; data {kind: "not-order"|"order-lifted", op, quote, reason, order?}
                     (other writers: oob choice, selfcheck submit, batch derive with kind
                     "batch-derive"; accounting reads only "not-order")

HONEST LIMIT: the pointer proves the owner typed the words, not that the words are an order for this
op. The agent reads the message; `not-order` records that reading as a derived choice for the owner to
read, and the self-check packet (design 5.1) is what looks for an order no row records.
"""
from __future__ import annotations

import json
import os
import re

from alpaca import db, ownerword
from alpaca.gates import verdict as vc

ORDER_KIND = "owner-order"
CLOSE_KIND = "owner-order-close"
DERIVED_KIND = "derived-choice"
BATCH_ITEM_KIND = "batch-item"

LINK_NAME = "owner-orders"
ANY = "any"
BOUNDARIES = ("requirement->design", "design->build", "build->verify", "verify->release")
LADDER = ("requirement", "design", "build", "verify", "release")
#: assent words an owner close may use, beside the shared assent list (design section 4)
CLOSE_WORDS = ("close", "lift", "done", "resume")
MIN_WORDS = 3
NOTE_MARKERS = ("landing", "handoff")

R_OPEN = "ORDER-OPEN"
R_DROPPED = "ORDER-DROPPED"


class OrderError(ValueError):
    """An order write that cannot be made: an unknown op, order or door, or a closed order."""


def _data(raw):
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------------ reading the record
def all_orders(conn, op=None) -> list:
    """Every order as a dict {id, op, quote, door, owner_word, event, ts, status, closed_by,
    lifted_by}, oldest first. `status` is `open` until a close event or a lifted-by names it."""
    q, p = "SELECT * FROM events WHERE kind=?", [ORDER_KIND]
    if op:
        q += " AND op=?"
        p.append(op)
    out = []
    for row in conn.execute(q + " ORDER BY id", p):
        d = _data(row["data"])
        out.append({"id": d.get("id") or row["ref"], "op": row["op"], "quote": d.get("quote") or "",
                    "door": d.get("door") or ANY, "owner_word": d.get("owner_word"),
                    "event": row["id"], "ts": row["ts"], "by": d.get("by"),
                    "status": "closed" if d.get("closed") else "open",
                    "closed_by": None, "lifted_by": d.get("lifted_by")})
    by_id = {o["id"]: o for o in out}
    for row in conn.execute("SELECT * FROM events WHERE kind=? ORDER BY id", (CLOSE_KIND,)):
        d = _data(row["data"])
        o = by_id.get(d.get("id") or row["ref"])
        if o is not None:
            o["status"] = "closed"
            o["closed_by"] = d.get("owner_word")
    return out


def open_orders(conn, op=None) -> list:
    return [o for o in all_orders(conn, op) if o["status"] == "open"]


def _order(conn, ident):
    for o in all_orders(conn):
        if o["id"] == ident:
            return o
    return None


def _op_row(conn, op):
    rows = db.rows(conn, "ops", "id=?", (op,))
    if not rows:
        raise OrderError("no op %s" % op)
    return rows[0]


def _op_opened(conn, op):
    """When the op opened: its op-new event, else the ops row. An order typed before it is not this
    op's order."""
    row = conn.execute("SELECT ts FROM events WHERE kind='op-new' AND ref=? ORDER BY id LIMIT 1",
                       (op,)).fetchone()
    if row:
        return row["ts"]
    return dict(_op_row(conn, op)).get("opened")


# ------------------------------------------------------------------ writing
def _next_id(conn):
    n = conn.execute("SELECT COUNT(*) FROM events WHERE kind=?", (ORDER_KIND,)).fetchone()[0]
    return "ord-%d" % (n + 1)


def add(conn, session, op, quote, door, by, *, lifted_by=None, root=None, environ=None,
        isatty=None, read=None) -> dict:
    """Record an owner order. `quote` goes through human_write in the plain quote form: a substring
    of one owner message of the session, at least three words, typed after the op opened. With
    `lifted_by` (a whole later owner message) the order is recorded closed and the reading is a
    derived-choice event. Returns the order dict."""
    if door != ANY and door not in BOUNDARIES:
        raise OrderError("--door is one of: %s" % ", ".join(BOUNDARIES + (ANY,)))
    _op_row(conn, op)
    root = root or _root()
    word = ownerword.human_write(conn, session, by, subject=op, answer="order", quote=quote,
                                 root=root, form="quote", since=_op_opened(conn, op),
                                 min_words=MIN_WORDS, environ=environ, isatty=isatty, read=read)
    # On the terminal path (no agent session) human_write's word carries the confirmed subject as
    # its quote, not the owner's words: the order stores the caller's own quote, held to the same
    # minimum, and keeps the terminal confirmation as the pointer.
    stored = (quote or "").strip()
    if len(stored.split()) < MIN_WORDS:
        raise OrderError("the quote has %d words; at least %d are needed"
                         % (len(stored.split()), MIN_WORDS))
    lifted = None
    if lifted_by is not None:
        lifted = _lifting_word(conn, session, word, lifted_by, root, environ)
    ident = _next_id(conn)
    data = {"id": ident, "op": op, "quote": stored, "door": door, "by": str(by).strip(),
            "owner_word": word}
    if lifted is not None:
        data["closed"] = True
        data["lifted_by"] = lifted
    ev = db.append_event(conn, session=session, actor=str(by).strip(), kind=ORDER_KIND, op=op,
                         ref=ident, data=data)
    if lifted is not None:
        db.append_event(conn, session=session, actor=ownerword.session_actor(session, environ),
                        kind=DERIVED_KIND, op=op, ref=ident,
                        data={"kind": "order-lifted", "op": op, "order": ident,
                              "quote": word["quote"], "lifted_by": lifted,
                              "reason": "the owner's later message lifts this order; the agent "
                                        "read it so and recorded the order closed"})
    return {"id": ident, "event": ev["id"], "status": "closed" if lifted else "open", **data}


def _lifting_word(conn, session, order_word, lifted_by, root, environ):
    """The owner_word of `lifted_by`: the whole text of an owner message typed after the order."""
    needle = (lifted_by or "").strip()
    path, messages = ownerword.session_messages(conn, session, root, environ)
    for m in messages:
        if m["text"].strip() == needle and ownerword._after(m, order_word["timestamp"], None):
            return {"path": "session", "transcript": path, "line": m["line"],
                    "timestamp": m["timestamp"], "quote": needle}
    raise ownerword.OwnerWordRefusal(
        ownerword.R_NOT_FOUND, "no owner message of session %s, typed after the order, is exactly "
        "that text" % session)


def close(conn, session, ident, by, quote, *, root=None, environ=None, isatty=None, read=None):
    """Close an order with the owner's words (`ord-N` plus a close word). An agent cannot close an
    order: human_write refuses any message that is not the owner's, typed after the order."""
    o = _order(conn, ident)
    if o is None:
        raise OrderError("no order %s" % ident)
    if o["status"] != "open":
        raise OrderError("%s is already closed" % ident)
    word = ownerword.human_write(conn, session, by, subject=ident, answer="yes", quote=quote,
                                 root=root or _root(), since=o["ts"], extra_yes=CLOSE_WORDS,
                                 environ=environ, isatty=isatty, read=read)
    return db.append_event(conn, session=session, actor=str(by).strip(), kind=CLOSE_KIND,
                           op=o["op"], ref=ident, data={"id": ident, "by": str(by).strip(),
                                                        "owner_word": word})


def not_order(conn, session, op, quote, reason, *, environ=None):
    """The agent's reading that an owner message is no order for `op`, recorded as a derived choice
    so the owner can read it. No owner words: it is the agent's reading, and says so."""
    _op_row(conn, op)
    if not (quote or "").strip() or not (reason or "").strip():
        raise OrderError("not-order needs --quote and --reason")
    return db.append_event(conn, session=session, actor=ownerword.session_actor(session, environ),
                           kind=DERIVED_KIND, op=op, ref=op,
                           data={"kind": "not-order", "op": op, "quote": quote.strip(),
                                 "reason": reason.strip()})


def _root():
    from alpaca import paths
    return paths.root()


# ------------------------------------------------------------------ accounting for messages
def _not_orders(conn, op):
    out = []
    for row in conn.execute("SELECT data FROM events WHERE kind=? AND op=? ORDER BY id",
                            (DERIVED_KIND, op)):
        d = _data(row["data"])
        if d.get("kind") == "not-order":
            out.append(d.get("quote") or "")
    return out


def accounts_for(conn, op, message_text) -> bool:
    """True when an order of `op` (open or closed) quotes inside `message_text`, or a not-order
    record names the message. The self-check calls this for each owner message it finds."""
    text = (message_text or "").strip()
    if not text:
        return False
    for o in all_orders(conn, op):
        if o["quote"] and o["quote"] in text:
            return True
    for quote in _not_orders(conn, op):
        # a not-order names a message only by its whole text or a substring of at least MIN_WORDS
        # words; a short or message-spanning quote must not clear every owner message
        if quote and len(quote.split()) >= MIN_WORDS and quote in text:
            return True
        if quote and quote == text:
            return True
    return False


# ------------------------------------------------------------------ the door and the task verbs
def _binds(order, boundary):
    return order["door"] == ANY or order["door"] == boundary


def door_link(conn, op, boundary, *, level, session, root):
    """The `owner-orders` link: PAUSED while an open order of the op binds this boundary or `any`,
    at every level (an owner order is the one stop L6 keeps). An op with no order has no link."""
    orders = all_orders(conn, op)
    if not orders:
        return None
    if any(o["status"] == "open" and _binds(o, boundary) for o in orders):
        return (LINK_NAME, vc.PAUSED)
    return (LINK_NAME, vc.PASS)


def _has_batch(conn, op):
    return conn.execute("SELECT 1 FROM events WHERE kind=? AND op=? LIMIT 1",
                        (BATCH_ITEM_KIND, op)).fetchone() is not None


def _after_boundary(phase, boundary):
    """A task whose phase is at or past the phase the boundary enters. A task with no phase, or a
    phase off the ladder, counts as after every boundary: nothing says it is before the stop."""
    if boundary == ANY or phase not in LADDER:
        return True
    entered = boundary.split("->", 1)[1]
    return LADDER.index(phase) >= LADDER.index(entered)


def task_gate(conn, task, action, *, level, session, root):
    """Refuse claim and move to doing, review or done of a task that comes after an open order's
    boundary (every task for `any`). Only ops with a batch are gated (design D-1)."""
    op = task.get("op")
    if not op or not _has_batch(conn, op):
        return
    from alpaca.intentgate import IntentGateRefusal
    for o in open_orders(conn, op):
        if _after_boundary(task.get("phase"), o["door"]):
            raise IntentGateRefusal(
                R_OPEN, "%s is open for %s (door %s): \"%s\". Only the owner lifts it: "
                "`alpaca order close %s --by owner --quote ...`"
                % (o["id"], op, o["door"], o["quote"], o["id"]))


def owner_items(conn, op, *, root=None) -> list:
    """The op's orders as plain dicts, for the self-check packet."""
    return [{"kind": "owner-order", "id": o["id"], "op": o["op"], "quote": o["quote"],
             "door": o["door"], "status": o["status"], "owner_word": o["owner_word"]}
            for o in all_orders(conn, op)]


# ------------------------------------------------------------------ the handoff check
def _names(text, order):
    pat = r"(?<![\w-])%s(?![\w-])" % re.escape(order["id"])
    return bool(re.search(pat, text, re.I)) or (order["quote"] and order["quote"] in text)


def missing_orders(conn, text, ops=None) -> list:
    """The open orders `text` fails to name, for the ops given (None: every op with open orders)."""
    out = []
    for o in open_orders(conn):
        if ops is not None and o["op"] not in ops:
            continue
        if not _names(text or "", o):
            out.append(o)
    return out


def dropped_message(missing, what="note") -> str:
    return "%s: the %s does not name open owner order%s %s" % (
        R_DROPPED, what, "" if len(missing) == 1 else "s",
        "; ".join("%s (%s): \"%s\"" % (o["id"], o["op"], o["quote"]) for o in missing))


def resolve_ops(conn, *refs) -> set:
    """The ops the refs name: an op id, a task id or a row id. Empty when none resolves."""
    found = set()
    for ref in refs:
        ref = str(ref or "").strip()
        if not ref:
            continue
        if conn.execute("SELECT 1 FROM ops WHERE id=?", (ref,)).fetchone():
            found.add(ref)
            continue
        for table in ("tasks", "rows"):
            r = conn.execute("SELECT op FROM %s WHERE id=?" % table, (ref,)).fetchone()
            if r and r["op"]:
                found.add(r["op"])
    return found


def check_handoff(conn, to, pointer, body) -> list:
    """Orders a handoff body drops. The op comes from `to` or the pointer; a handoff that resolves
    to no op is checked against every op with open orders."""
    ops = resolve_ops(conn, to, pointer)
    return missing_orders(conn, body, ops or None)


def check_note_text(conn, text, op=None) -> list:
    return missing_orders(conn, text, {op} if op else None)


def check_note(conn, path, op=None) -> list:
    """Orders the landing or handoff note at `path` fails to name."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return check_note_text(conn, fh.read(), op)


def is_note_file(path) -> bool:
    name = os.path.basename(str(path or "")).lower()
    return any(m in name for m in NOTE_MARKERS)


# ------------------------------------------------------------------ CLI: alpaca order
def _cmd_order(args):
    from alpaca import cli
    root = cli._root()
    conn = db.connect(root)
    sid = args.session or "cli"
    verb = getattr(args, "order_verb", None)
    try:
        if verb == "add":
            o = add(conn, sid, args.op, args.quote, args.door, args.by,
                    lifted_by=args.lifted_by, root=root)
            print("%s %s for %s (door %s): \"%s\"" % (o["id"], o["status"], args.op, args.door,
                                                       o["quote"]))
        elif verb == "close":
            close(conn, sid, args.order, args.by, args.quote, root=root)
            print("%s closed" % args.order)
        elif verb == "not-order":
            not_order(conn, sid, args.op, args.quote, args.reason)
            print("recorded: the message is no order for %s (derived choice)" % args.op)
        elif verb == "list":
            rows = open_orders(conn, args.op) if args.open else all_orders(conn, args.op)
            for o in rows:
                print("%s [%s] %s door %s: \"%s\"" % (o["id"], o["status"], o["op"], o["door"],
                                                       o["quote"]))
            if not rows:
                print("no orders")
        elif verb == "check-note":
            try:
                missing = check_note(conn, args.file, args.op)
            except OSError as exc:
                print("GATE alpaca-order: FAIL (cannot read %s: %s)" % (args.file, exc))
                return cli.FAIL
            if missing:
                print("GATE alpaca-order-check-note: FAIL (%s)" % dropped_message(missing, "note"))
                return cli.FAIL
            print("GATE alpaca-order-check-note: PASS (%s names every open order)" % args.file)
        else:
            return cli.USAGE
    except ownerword.OwnerWordRefusal as exc:
        print("GATE alpaca-order: FAIL (%s: %s)" % (exc.reason, exc.detail))
        return cli.FAIL
    except OrderError as exc:
        print("GATE alpaca-order: FAIL (%s)" % exc)
        return cli.FAIL
    return cli.PASS


def _parser(sub):
    o = sub.add_parser("order", help="owner orders: record, close and list the owner's standing orders")
    ov = o.add_subparsers(dest="order_verb")
    a = ov.add_parser("add", help="record an owner order from the owner's words")
    a.add_argument("op")
    a.add_argument("--quote", required=True, help="the owner's words (a substring of one message)")
    a.add_argument("--door", required=True, help="a boundary (%s) or any" % ", ".join(BOUNDARIES))
    a.add_argument("--by", help="a human identity (owner, human)")
    a.add_argument("--lifted-by", dest="lifted_by",
                   help="a whole later owner message that lifts the order: recorded closed")
    c = ov.add_parser("close", help="close an order with the owner's words (ord-N plus a close word)")
    c.add_argument("order")
    c.add_argument("--by", help="a human identity (owner, human)")
    c.add_argument("--quote", help="the owner's message, e.g. `ord-1 resume`")
    n = ov.add_parser("not-order", help="record the reading that an owner message is no order")
    n.add_argument("op")
    n.add_argument("--quote", required=True); n.add_argument("--reason", required=True)
    ls = ov.add_parser("list", help="list the orders")
    ls.add_argument("--op"); ls.add_argument("--open", action="store_true")
    k = ov.add_parser("check-note", help="fail when a landing or handoff note drops an open order")
    k.add_argument("file"); k.add_argument("--op")


def _register():
    from alpaca import cli
    cli.command("order")(_cmd_order)
    cli.register_parser("orders", _parser)


_register()
