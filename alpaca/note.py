"""alpaca note: the inbox for raw pieces.

An operator hands over pieces as they come: a pasted chat, an exported file, a line typed in a
hurry. Each piece is kept as it was given, in `input/notes/<UTC stamp>-<sha12>.md`: a small front
matter (`time`, `by`, `from`, the `sha256` of the body) and then the body byte for byte. A note is
never rewritten. The same bytes again are found by their sha256 and not written twice. The
interview (alpaca/interview.py) cites notes by their path, and `alpaca interview status` lists
the notes no answer cites yet.

    alpaca note add "<text>" | --file <path> | -   [--by <name>] [--json]
    alpaca note list [--json]

A new note records one `note` event (file, sha256, bytes). docs/interview.md is the reference.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sys
import tempfile

from alpaca import db, util

INSTRUMENT = "alpaca-note"
EVENT = "note"
NOTES = "input/notes"
PASS, FAIL, BLOCKED, USAGE = 0, 1, 2, 64


class NoteError(Exception):
    def __init__(self, message, code=FAIL):
        super().__init__(message)
        self.code = code


def notes_dir(root):
    return os.path.join(root, *NOTES.split("/"))


def utc_stamp(iso=None):
    """`20260924T030000Z` for the given ISO time (default: now), in UTC so names sort by time."""
    t = datetime.datetime.fromisoformat(iso or util.now_iso())
    if t.tzinfo is None:
        t = t.astimezone()
    return t.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _one_line(value):
    return " ".join(str(value).split())


def split(raw):
    """(front matter dict, body bytes) of a note's bytes, or None when it has no front matter."""
    if not raw.startswith(b"---\n"):
        return None
    end = raw.find(b"\n---\n", 3)
    if end < 0:
        return None
    meta = {}
    for line in raw[4:end].decode("utf-8", "replace").split("\n"):
        key, sep, value = line.partition(": ")
        if sep:
            meta[key.strip()] = value.strip()
    return meta, raw[end + 5:]


def _first_line(body):
    for line in body.decode("utf-8", "replace").splitlines():
        if line.strip():
            return line.strip()[:120]
    return ""


def notes(root):
    """Every note in time order: [{file, time, by, from, sha256, bytes, first_line}]. The file
    name starts with the UTC stamp, so the name order is the time order."""
    base = notes_dir(root)
    out = []
    if not os.path.isdir(base):
        return out
    for name in sorted(os.listdir(base)):
        path = os.path.join(base, name)
        if not name.endswith(".md") or name.startswith(".") or not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            parts = split(fh.read())
        meta, body = parts if parts else ({}, b"")
        out.append({"file": "%s/%s" % (NOTES, name), "time": meta.get("time", ""),
                    "by": meta.get("by", ""), "from": meta.get("from", ""),
                    "sha256": meta.get("sha256", ""), "bytes": len(body),
                    "first_line": _first_line(body)})
    return out


def find(root, sha):
    """The project-relative path of the note whose body has this sha256, or None."""
    for n in notes(root):
        if n["sha256"] == sha:
            return n["file"]
    return None


def _write_new(path, data):
    """Write `data` to `path` only when nothing is there: a temp file linked into place, so a
    note is either whole or absent and an existing file is never replaced."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        u = os.umask(0); os.umask(u)
        os.chmod(tmp, 0o666 & ~u)
        try:
            os.link(tmp, path)
        except FileExistsError:
            raise NoteError("%s already exists and is never rewritten" % os.path.basename(path))
    finally:
        os.unlink(tmp)


def add(root, body, origin, *, by="cli", session="cli"):
    """Keep `body` (bytes) as a new note. Returns {file, sha256, bytes, new}; `new` is False when
    a note with the same bytes is already there (nothing is written and no event recorded)."""
    if not body.strip():
        raise NoteError("the note is empty; nothing to keep")
    sha = hashlib.sha256(body).hexdigest()
    have = find(root, sha)
    if have:
        return {"file": have, "sha256": sha, "bytes": len(body), "new": False}
    # a kept note given again, front matter and all (`--file input/notes/<note>.md`), is that note
    parts = split(body)
    if parts and parts[0].get("sha256") == hashlib.sha256(parts[1]).hexdigest():
        have = find(root, parts[0]["sha256"])
        if have:
            return {"file": have, "sha256": parts[0]["sha256"], "bytes": len(parts[1]), "new": False}
    now = util.now_iso()
    rel = "%s/%s-%s.md" % (NOTES, utc_stamp(now), sha[:12])
    head = "---\ntime: %s\nby: %s\nfrom: %s\nsha256: %s\n---\n" % (
        now, _one_line(by) or "cli", _one_line(origin), sha)
    _write_new(os.path.join(root, *rel.split("/")), head.encode("utf-8") + body)
    conn = db.connect(root)
    db.append_event(conn, session=session, actor=INSTRUMENT, kind=EVENT,
                    data={"file": rel, "sha256": sha, "bytes": len(body), "from": _one_line(origin)})
    return {"file": rel, "sha256": sha, "bytes": len(body), "new": True}


# ------------------------------------------------------------------------------ the verb
def _body(args):
    """(bytes, origin) from the text argument, --file or stdin."""
    if args.file is not None:
        path = util.from_caller(args.file)
        try:
            with open(path, "rb") as fh:
                return fh.read(), "file:%s" % os.path.basename(args.file)
        except OSError as exc:
            raise NoteError("cannot read %s: %s" % (args.file, exc.strerror or exc))
    if args.text == "-":
        stream = getattr(sys.stdin, "buffer", None)
        return (stream.read() if stream is not None else sys.stdin.read().encode("utf-8")), "stdin"
    # os.fsencode gives back the bytes of the argument as typed, even when they are not UTF-8
    return os.fsencode(args.text), "text"


def _fail(args, code, message):
    from alpaca.gates import verdict
    name = "USAGE" if code == USAGE else verdict.name_of(code)
    if getattr(args, "json", False):
        print(json.dumps({"verdict": name, "reason": message}, indent=1))
    else:
        print("GATE %s: %s (%s)" % (INSTRUMENT, name, message))
    return code


def cmd_note(args):
    from alpaca import cli
    from alpaca.gates import verdict
    verb = getattr(args, "note_verb", None)
    root = cli._root()
    if verb == "add":
        if (args.text is None) == (args.file is None):
            return _fail(args, USAGE, "give the note as text, - for stdin, or --file <path> (one of them)")
        try:
            body, origin = _body(args)
            result = add(root, body, origin, by=args.by or args.session or "cli",
                         session=args.session or "cli")
        except NoteError as exc:
            return _fail(args, exc.code, str(exc))
        result["verdict"] = "PASS"
        if args.json:
            print(json.dumps(result, indent=1, sort_keys=True))
        else:
            if result["new"]:
                print("kept %s (%d bytes, sha256 %s)" % (result["file"], result["bytes"], result["sha256"][:12]))
            else:
                print("already have %s" % result["file"])
            print(verdict.gate_line(INSTRUMENT, PASS))
        return PASS
    if verb == "list":
        found = notes(root)
        if args.json:
            print(json.dumps({"verdict": "PASS", "notes": found}, indent=1, sort_keys=True))
        else:
            if not found:
                print("no notes yet (alpaca note add \"<text>\" | --file <path> | -)")
            for n in found:
                print("%s  %s  %s" % (n["file"], n["time"], n["first_line"]))
            print(verdict.gate_line(INSTRUMENT, PASS))
        return PASS
    print("usage: alpaca note add \"<text>\" | --file <path> | -  or  alpaca note list [--json]",
          file=sys.stderr)
    return USAGE


def _parser(sub):
    p = sub.add_parser("note", help="the inbox for raw pieces: keep a note as given, list the notes")
    v = p.add_subparsers(dest="note_verb")
    a = v.add_parser("add", help="keep one raw piece in input/notes/, byte for byte, never rewritten")
    a.add_argument("text", nargs="?", default=None, help="the note as text, or - to read stdin")
    a.add_argument("--file", default=None, help="keep the bytes of this file")
    a.add_argument("--by", default=None, help="who gave the note (default: the session)")
    a.add_argument("--json", action="store_true")
    ls = v.add_parser("list", help="the notes in time order with their first line")
    ls.add_argument("--json", action="store_true")


def _register():
    from alpaca import cli
    cli.command("note")(cmd_note)
    cli.register_parser("note", _parser)


_register()
