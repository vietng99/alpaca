"""Messages table, verbs, and the native channel rule (M2.4).

A message is a durable, replayable record row: ts, from, to, kind, body, pointer. `post` appends
its write-ahead event first and inserts the row second, both inside one `db.transaction`, so the
event and the current-state row land together or not at all. `read` replays messages in the order
they were recorded (ascending id), optionally narrowed to one addressee (plus every broadcast) and
from a watermark.

Seven kinds: claim, handoff, question, result, blocker, decision, note.
Three `to` forms: an agent name, a task/row/op id, or the literal `broadcast`.

The native channel rule (`doctrine/leaves/file-as-truth.md`): a subagent return carries a POINTER to a board row or
a message id, never the content. A pointer, when given, must resolve, or the message is refused
before anything is written, so a dangling pointer never enters the record. A pointer resolves when
it is a `local:<relpath>` evidence file that exists on disk, a `remote:<ref>` opaque reference, a
message id (`msg:<n>` or a bare integer) already in this table, or a board row id present in
`tasks`, `rows`, or `ops`.

M2.4 Step 4 note: until M4.9 ships the board renderer, `board.json` neutralises a message body by
escaping the pipe and collapsing newlines. `render_cell` here does exactly that so the board can
call one place; when M4.9 lands, the renderer it ships supersedes this and this helper is retired.
"""
from alpaca import cli, db, paths, util

KINDS = ("claim", "handoff", "question", "result", "blocker", "decision", "note")

# The three shapes a `to` can take. `broadcast` is the literal fan-out address; an agent name and
# a task/row/op id are free-form, validated only as non-empty at the boundary.
BROADCAST = "broadcast"


class MessageError(ValueError):
    """A message that cannot be recorded: an empty from or to, an unknown kind, or a pointer that
    does not resolve. Raised before any event is appended, so a refused message leaves no trace."""


def _resolve_pointer(conn, pointer, root=None):
    """Return the canonical pointer to store, or None when none was given. Raise MessageError when
    a pointer is given but resolves to nothing (the reproduced defect this refuses)."""
    if pointer is None:
        return None
    p = str(pointer).strip()
    if not p:
        return None
    scheme = p.split(":", 1)[0] if ":" in p else ""
    if scheme in ("local", "remote", "file"):
        from alpaca.gates import contract
        try:
            contract.resolve_pointer(root or paths.root(), p)
        except contract.ContractError as e:
            raise MessageError("pointer does not resolve: %s (%s)" % (p, e)) from e
        return p
    if scheme == "msg" or p.isdigit():
        mid = p.split(":", 1)[1] if scheme == "msg" else p
        if not mid.isdigit() or not conn.execute(
                "SELECT 1 FROM messages WHERE id=?", (int(mid),)).fetchone():
            raise MessageError("pointer does not resolve: no message %s" % p)
        return "msg:%d" % int(mid)
    for table in ("tasks", "rows", "ops"):
        if conn.execute("SELECT 1 FROM %s WHERE id=?" % table, (p,)).fetchone():
            return p
    raise MessageError("pointer does not resolve: no board row %s" % p)


def _row(conn, mid):
    r = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    return dict(r) if r else None


def post(conn, frm, to, kind, body, pointer=None, *, session="cli", root=None):
    """Record one message durably and return the stored row as a dict.

    frm and to are non-empty (to is an agent, a task/row/op id, or the literal broadcast); kind is
    one of the seven; a pointer, when given, must resolve or the message is refused. The event and
    the row carry the same ts and land in one transaction.
    """
    if not (frm and str(frm).strip()):
        raise MessageError("a message needs a from")
    if not (to and str(to).strip()):
        raise MessageError("--to must name an agent, a task, or broadcast")
    if kind not in KINDS:
        raise MessageError("unknown kind %r; one of %s" % (kind, ", ".join(KINDS)))
    resolved = _resolve_pointer(conn, pointer, root)
    ts = util.now_iso()
    with db.transaction(conn):
        db.append_event(conn, session=session, actor=frm, kind="msg", ref=resolved,
                        data={"to": to, "kind": kind, "body": body},
                        conn_in_txn=True, clock=lambda: ts)
        cur = conn.execute(
            "INSERT INTO messages (ts,session,sender,to_,kind,body,ref) VALUES (?,?,?,?,?,?,?)",
            (ts, session, frm, to, kind, body, resolved))
        mid = cur.lastrowid
    return _row(conn, mid)


def read(conn, to=None, since=None):
    """Replay messages in recorded order (ascending id). `to` narrows to that addressee plus every
    broadcast; `since` is a watermark, an integer message id returning rows after it or an ISO
    timestamp string returning rows stamped at or after it."""
    where, params = [], []
    if to is not None:
        where.append("(to_=? OR to_=?)"); params += [to, BROADCAST]
    if since is not None:
        if isinstance(since, bool):
            raise MessageError("since must be a message id or a timestamp, not a bool")
        if isinstance(since, int):
            where.append("id > ?"); params.append(since)
        else:
            where.append("ts >= ?"); params.append(str(since))
    q = "SELECT * FROM messages"
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY id"
    return [dict(r) for r in conn.execute(q, params)]


def render_cell(body):
    """Neutralise a message body for board.json until M4.9 ships the renderer: escape the pipe and
    collapse every newline to a space so one field never breaks the row. M2.4 Step 4."""
    return (body or "").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def cmd_msg(args):
    """alpaca msg post|read, routed through this module (M2.4). Extends the M0 verb additively: the
    seven kinds and the pointer-resolution refusal are enforced here. The `msg` subparser is the
    one ops.py already registers; cli.main binds this handler last so it wins deterministically."""
    conn = db.connect(cli._root())
    sid = args.session or "cli"
    if args.msg_verb == "post":
        try:
            row = post(conn, args.sender, args.to, args.kind, args.body,
                       pointer=getattr(args, "ref", None), session=sid, root=cli._root())
        except MessageError as e:
            print("GATE alpaca-msg-post: FAIL (%s)" % e)
            return cli.FAIL
        print("posted to %s" % row["to_"])
        return cli.PASS
    if args.msg_verb == "read":
        for m in read(conn, to=getattr(args, "to", None), since=getattr(args, "since", None)):
            print("%s %s -> %s [%s] %s"
                  % (m["ts"], m["sender"], m["to_"], m["kind"], render_cell(m["body"])))
        return cli.PASS
    return cli.USAGE
