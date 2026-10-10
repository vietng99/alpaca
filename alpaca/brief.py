"""The signed brief of an op (op-019 t-201, design section 3.1).

An op's brief is the file the owner signs before an autonomous run starts. `alpaca brief present
<op> <file>` copies the file to `.alpaca/briefs/<op>/<sha256>.md`, records an `owner-ask` and prints
the file. `alpaca brief sign <op> <file> --by owner --quote Q` records `brief-signed` through
ownerword.human_write (subject `<sha12>`, answer yes: the owner replies `<sha12> signed`).
`alpaca brief sign <op> <file> --decision <dec-id>` binds an owner-proved decision whose body names
the file's sha256 (how op-019 binds dec-ab31c8b6fd4b). `alpaca brief show <op>` lists what was
signed and reports `changed` when the live file no longer has the signed bytes.

The signed COPY, never the live file, is what an owner-quote and the self-check read, so a later
edit of the file changes nothing that was signed (design D-16). The copy's bytes are re-hashed on
every read; a copy that no longer matches its name is not a signed brief.

HONEST LIMIT: the copy is a file under `.alpaca/`; the same Unix user can rewrite it, and the
re-hash then fails the read rather than prevents the edit.
"""
from __future__ import annotations

import hashlib
import json
import os

from alpaca import db, ownerword, paths

SIGNED_KIND = "brief-signed"
MAX_BYTES = 1024 * 1024


def copy_path(root, op, sha256):
    return os.path.join(paths.runtime_dir(root), "briefs", op, "%s.md" % sha256)


def _rel(root, file):
    full = file if os.path.isabs(file) else os.path.join(root, file)
    return os.path.relpath(os.path.normpath(full), root)


def _read(root, file):
    full = file if os.path.isabs(file) else os.path.join(root, file)
    try:
        with open(full, "rb") as fh:
            data = fh.read(MAX_BYTES + 1)
    except OSError as exc:
        raise ValueError("cannot read %s: %s" % (file, exc))
    if len(data) > MAX_BYTES:
        raise ValueError("%s is larger than %d bytes" % (file, MAX_BYTES))
    return data


def _store_copy(root, op, data):
    sha = hashlib.sha256(data).hexdigest()
    path = copy_path(root, op, sha)
    if not os.path.isfile(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
    return sha, path


def signed_events(conn, op=None):
    """Every brief-signed event as a dict (data merged with id and ts), oldest first."""
    q, p = "SELECT * FROM events WHERE kind=?", [SIGNED_KIND]
    if op:
        q += " AND op=?"
        p.append(op)
    out = []
    for row in conn.execute(q + " ORDER BY id", p):
        try:
            data = json.loads(row["data"])
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            out.append(dict(data, event_id=row["id"], ts=row["ts"], op=row["op"]))
    return out


def signed_text(conn, root, file):
    """(text of the newest signed copy of repo file `file`, its sha256), or None when the file was
    never signed or the copy no longer matches its sha256."""
    rel = _rel(root, file)
    for ev in reversed(signed_events(conn)):
        if ev.get("file") != rel:
            continue
        path = os.path.join(root, ev.get("copy") or "")
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        if hashlib.sha256(data).hexdigest() != ev.get("sha256"):
            continue
        return data.decode("utf-8", errors="replace"), ev["sha256"]
    return None


def _op_exists(conn, op):
    if not db.rows(conn, "ops", "id=?", (op,)):
        raise ValueError("no op %s" % op)


def present(conn, root, session, op, file):
    """Copy the brief, record the owner-ask and return (sha256, text printed)."""
    _op_exists(conn, op)
    ownerword.require_real_session(session)
    data = _read(root, file)
    sha, _ = _store_copy(root, op, data)
    text = data.decode("utf-8", errors="replace")
    ask = ("Brief of %s (sha256 %s). To sign it reply: %s signed" % (op, sha, sha[:12]))
    ownerword.record_ask(conn, session, "brief:%s:%s" % (op, sha[:12]), ask, op=op,
                         extra={"file": _rel(root, file), "sha256": sha})
    return sha, "%s\n\n%s" % (text, ask)


def _record_signed(conn, session, actor, op, root, file, sha, path, extra):
    data = {"op": op, "file": _rel(root, file), "sha256": sha, "copy": os.path.relpath(path, root)}
    data.update(extra)
    return db.append_event(conn, session=session, actor=actor, kind=SIGNED_KIND, op=op,
                           ref=sha[:12], data=data)


def sign_with_quote(conn, root, session, op, file, by, quote):
    _op_exists(conn, op)
    data = _read(root, file)
    sha, path = _store_copy(root, op, data)
    subject = "brief:%s:%s" % (op, sha[:12])
    since = ownerword.ask_ts(conn, subject)
    if since is None:
        raise ownerword.OwnerWordRefusal(ownerword.R_UNBOUND, "this brief was never presented; run "
                                         "`alpaca brief present %s <file>` first" % op)
    word = ownerword.human_write(conn, session, by, subject=sha[:12], answer="yes", quote=quote,
                                 root=root, since=since)
    return _record_signed(conn, session, str(by).strip(), op, root, file, sha, path,
                          {"owner_word": word})


def sign_with_decision(conn, root, session, op, file, dec):
    _op_exists(conn, op)
    data = _read(root, file)
    sha, path = _store_copy(root, op, data)
    ownerword.ensure_grandfathered(conn, root)
    if not ownerword.decision_is_owner(conn, dec, root):
        raise ownerword.OwnerWordRefusal(ownerword.R_NOT_FOUND, "%s is not an owner-proved "
                                         "decision" % dec)
    row, body = ownerword._decision_event(conn, dec)
    text = json.dumps(body, ensure_ascii=False)
    if sha not in text:
        raise ValueError("decision %s does not name the sha256 of %s (%s)" % (dec, file, sha))
    return _record_signed(conn, session, ownerword.session_actor(session), op, root, file, sha,
                          path, {"decision": dec})


def show(conn, root, op):
    """One line per signed brief of `op`: file, sha12, how it was signed, `changed` when the live
    file differs from the signed copy."""
    lines = []
    for ev in signed_events(conn, op):
        live = None
        try:
            live = hashlib.sha256(_read(root, ev["file"])).hexdigest()
        except ValueError:
            pass
        state = "missing" if live is None else ("unchanged" if live == ev["sha256"] else "changed")
        how = ("decision " + ev["decision"]) if ev.get("decision") else "owner word"
        lines.append("%s %s signed by %s; live file %s" % (ev["file"], ev["sha256"][:12], how, state))
    return lines


# ------------------------------------------------------------------ CLI: alpaca brief
def _cmd_brief(args):
    from alpaca import cli
    root = cli._root()
    conn = db.connect(root)
    sid = args.session or "cli"
    verb = getattr(args, "brief_verb", None)
    try:
        if verb == "present":
            _sha, text = present(conn, root, sid, args.op, args.file)
            print(text)
        elif verb == "sign":
            if args.decision:
                ev = sign_with_decision(conn, root, sid, args.op, args.file, args.decision)
            elif args.by:
                ev = sign_with_quote(conn, root, sid, args.op, args.file, args.by, args.quote)
            else:
                print("GATE alpaca-brief: FAIL (give --by owner --quote Q, or --decision <dec-id>)")
                return cli.FAIL
            print("%s brief signed: %s (event %s)" % (args.op, args.file,
                                                      ev["id"]))
        elif verb == "show":
            for line in show(conn, root, args.op) or ["%s has no signed brief" % args.op]:
                print(line)
        else:
            return cli.USAGE
    except ownerword.OwnerWordRefusal as exc:
        print("GATE alpaca-brief: FAIL (%s: %s)" % (exc.reason, exc.detail))
        return cli.FAIL
    except ValueError as exc:
        print("GATE alpaca-brief: FAIL (%s)" % exc)
        return cli.FAIL
    return cli.PASS


def _parser(sub):
    b = sub.add_parser("brief", help="present, sign and show the signed brief of an op")
    bv = b.add_subparsers(dest="brief_verb")
    p = bv.add_parser("present", help="copy the brief, record the owner-ask, print the file")
    p.add_argument("op"); p.add_argument("file")
    s = bv.add_parser("sign", help="record the owner's signature of the brief")
    s.add_argument("op"); s.add_argument("file")
    s.add_argument("--by", help="a human identity (owner, human); needs --quote")
    s.add_argument("--quote", help="the owner's whole message, `<sha12> signed`")
    s.add_argument("--decision", help="an owner-proved decision whose body names the file's sha256")
    w = bv.add_parser("show", help="list the signed briefs of an op")
    w.add_argument("op")


def _register():
    from alpaca import cli
    cli.command("brief")(_cmd_brief)
    cli.register_parser("brief", _parser)


_register()
