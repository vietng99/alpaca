"""alpaca interview: the slot map a runbook needs, filled from the notes and the operator's answers.

Operators hand over raw pieces (alpaca/note.py keeps them in `input/notes/`) and rarely know all
that a runbook needs. The interview names what is needed as slots, each with the question family
that fills it: the default list is `templates/interview/slots.yaml`, and a project adds or replaces
slots in `input/interview/slots.yaml`. The skill `/alpaca-interview` asks the questions; this verb
keeps the state and says what is still open.

The state is an append-only log, `input/interview/log.jsonl`, one JSON line per change:
`{time, slot, state, value, source}` (plus `reason` for a waived slot). The latest line of a slot
is its state: `answered`, `default` (the operator accepted a proposed default), `waived` or `open`.
A slot with no line is open. `source` is a note path, a round and question (`round:2/q1`), or
`owner`.

    alpaca interview status [--json]
    alpaca interview set <slot> --answered|--default|--waived|--open --value <text> --source <ref> [--reason <text>]
    alpaca interview readback [--json]
    alpaca interview signoff --by <name> [--json]

`status` exits 0 only when no slot is open, so a script can gate on it. `signoff` refuses while a
slot is open, writes `input/interview/signed-<UTC stamp>-<sha12>.md` (the readback and the sha256
of the log at signing; sha12 is the start of that sha256) and records one `interview-signoff`
event. A later change to the log makes the sign-off stale. docs/interview.md is the reference.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sys

from alpaca import db, util

INSTRUMENT = "alpaca-interview"
EVENT = "interview-signoff"
PASS, FAIL, BLOCKED, USAGE = 0, 1, 2, 64
STATES = ("answered", "default", "waived", "open")
SETTLED = ("answered", "default", "waived")
DIR = "input/interview"
LOG = DIR + "/log.jsonl"
PROJECT_SLOTS = DIR + "/slots.yaml"
DEFAULT_SLOTS = "templates/interview/slots.yaml"
_SLOT_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_ROUND = re.compile(r"^round:[1-9][0-9]*/q[1-9][0-9]*$")


class InterviewError(Exception):
    def __init__(self, message, code=FAIL):
        super().__init__(message)
        self.code = code


def _path(root, rel):
    return os.path.join(root, *rel.split("/"))


def product_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ------------------------------------------------------------------------------ the slots
def _read_slots(path, label):
    import yaml
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        raise InterviewError("%s cannot be read: %s" % (label, exc), code=BLOCKED)
    items = data.get("slots") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise InterviewError("%s needs a `slots:` list" % label, code=BLOCKED)
    out, seen = [], set()
    for n, item in enumerate(items, 1):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) \
                or not _SLOT_ID.match(item["id"]):
            raise InterviewError("%s slot %d needs an `id` of lower-case letters, digits and hyphens"
                                 % (label, n), code=BLOCKED)
        if not isinstance(item.get("fills"), str) or not item["fills"].strip():
            raise InterviewError("%s slot %s needs `fills`: what fills it" % (label, item["id"]), code=BLOCKED)
        if item["id"] in seen:
            raise InterviewError("%s names the slot %s twice" % (label, item["id"]), code=BLOCKED)
        seen.add(item["id"])
        out.append({"id": item["id"], "fills": " ".join(item["fills"].split()),
                    "becomes": " ".join(str(item.get("becomes") or "").split())})
    return out


def slots(root):
    """The slot list: the defaults, with the project's slots replacing a default of the same id
    and new ids added after the defaults."""
    out = _read_slots(os.path.join(product_root(), *DEFAULT_SLOTS.split("/")), DEFAULT_SLOTS)
    for s in out:
        s["from"] = "default"
    project = _path(root, PROJECT_SLOTS)
    if os.path.isfile(project):
        index = {s["id"]: i for i, s in enumerate(out)}
        for s in _read_slots(project, PROJECT_SLOTS):
            s["from"] = "project"
            if s["id"] in index:
                out[index[s["id"]]] = s
            else:
                out.append(s)
    return out


def slot_ids(root):
    return [s["id"] for s in slots(root)]


# ------------------------------------------------------------------------------ the log
def log_bytes(root):
    try:
        with open(_path(root, LOG), "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return b""


def read_log(root):
    out = []
    for n, line in enumerate(log_bytes(root).decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            entry = None
        if not isinstance(entry, dict) or entry.get("state") not in STATES or not entry.get("slot"):
            raise InterviewError("line %d of %s is not an interview entry" % (n, LOG), code=BLOCKED)
        out.append(entry)
    return out


def is_note_source(source):
    return source.startswith("input/notes/") and source.endswith(".md") and "/../" not in source \
        and "\\" not in source and source.count("/") == 2


def source_kind(source):
    """"note", "round" or "owner"; None for any other shape."""
    if source == "owner":
        return "owner"
    if _ROUND.match(source):
        return "round"
    if is_note_source(source):
        return "note"
    return None


def append(root, slot, state, value, source, reason=None):
    """Append one line to the log after checking it. Returns the entry."""
    known = slot_ids(root)
    if slot not in known:
        raise InterviewError("unknown slot %r; the slots are: %s" % (slot, ", ".join(known)))
    if state not in STATES:
        raise InterviewError("unknown state %r; use %s" % (state, ", ".join(STATES)), code=USAGE)
    value = value or ""
    reason = (reason or "").strip()
    if state == "waived" and not reason:
        raise InterviewError("a waived slot needs --reason: why the runbook can go without it")
    if state in ("answered", "default") and not value.strip():
        raise InterviewError("an %s slot needs --value" % state)
    source = source or ""
    kind = source_kind(source)
    if kind is None:
        raise InterviewError("source %r is not a note path (input/notes/<file>.md), round:<n>/q<n> "
                             "or owner" % source)
    if kind == "note" and not os.path.isfile(_path(root, source)):
        raise InterviewError("source %s is not a note in this project (alpaca note list)" % source)
    entry = {"time": util.now_iso(), "slot": slot, "state": state, "value": value, "source": source}
    if reason:
        entry["reason"] = reason
    path = _path(root, LOG)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")
    return entry


# ------------------------------------------------------------------------------ the views
def signoff_state(root):
    """{state: needed|signed|stale, file, log_sha256}: the latest signed file against the log."""
    now = hashlib.sha256(log_bytes(root)).hexdigest()
    found = []
    for path in glob.glob(os.path.join(_path(root, DIR), "signed-*.md")):
        from alpaca import note
        with open(path, "rb") as fh:
            parts = note.split(fh.read())
        meta = parts[0] if parts else {}
        try:
            lines = int(meta.get("log_lines", "0"))
        except ValueError:
            lines = 0
        # the name starts with the UTC stamp; within one second the longer log is the later one
        found.append((os.path.basename(path)[:23], lines, os.path.basename(path), meta.get("log_sha256", "")))
    if not found:
        return {"state": "needed", "file": None, "log_sha256": None, "log_sha256_now": now}
    _stamp, _lines, name, signed = max(found)
    return {"state": "signed" if signed == now else "stale", "file": "%s/%s" % (DIR, name),
            "log_sha256": signed, "log_sha256_now": now}


def state(root):
    """needed, signed or stale, for alpaca start."""
    return signoff_state(root)["state"]


def view(root):
    """Every slot with its latest state, the open slots, the notes no line cites, the drift and
    the sign-off."""
    from alpaca import note
    all_slots = slots(root)
    entries = read_log(root)
    latest, first, last_settled = {}, {}, {}
    for e in entries:
        latest[e["slot"]] = e
        if e["state"] in SETTLED:
            first.setdefault(e["slot"], e)
            last_settled[e["slot"]] = e
    rows = []
    for s in all_slots:
        e = latest.get(s["id"])
        rows.append({"slot": s["id"], "fills": s["fills"], "becomes": s["becomes"], "from": s["from"],
                     "state": e["state"] if e else "open", "value": e.get("value", "") if e else "",
                     "source": e.get("source", "") if e else "", "reason": e.get("reason", "") if e else ""})
    drifted = []
    for s in all_slots:
        a = first.get(s["id"])
        later = [e for e in entries if e["slot"] == s["id"] and e["state"] in SETTLED and e is not a]
        if a and any(e.get("value", "") != a.get("value", "") for e in later):
            b = last_settled[s["id"]]
            drifted.append({"slot": s["id"], "first": a.get("value", ""), "first_source": a.get("source", ""),
                            "now": b.get("value", ""), "now_source": b.get("source", "")})
    cited = {e.get("source") for e in entries}
    settled = sum(1 for r in rows if r["state"] in SETTLED)
    return {"slots": rows, "settled": settled, "total": len(rows),
            "progress": "%d/%d slots settled" % (settled, len(rows)),
            "open": [r["slot"] for r in rows if r["state"] == "open"],
            "uncited_notes": [n["file"] for n in note.notes(root) if n["file"] not in cited],
            "drifted": drifted, "signoff": signoff_state(root), "log_lines": len(entries)}


def readback(root):
    """The five buckets: clear from the start (answered from a note), added during the interview
    (answered in a round or by the owner), filled by default, waived, and drifted."""
    v = view(root)
    buckets = {"clear": [], "added": [], "default": [], "waived": [], "drifted": v["drifted"]}
    for r in v["slots"]:
        item = {"slot": r["slot"], "value": r["value"], "source": r["source"]}
        if r["state"] == "answered":
            buckets["clear" if source_kind(r["source"]) == "note" else "added"].append(item)
        elif r["state"] == "default":
            buckets["default"].append(item)
        elif r["state"] == "waived":
            item["reason"] = r["reason"]
            buckets["waived"].append(item)
    return {"buckets": buckets, "open": v["open"], "progress": v["progress"],
            "uncited_notes": v["uncited_notes"], "log_lines": v["log_lines"],
            "log_sha256": v["signoff"]["log_sha256_now"]}


def _text(value):
    lines = str(value).strip().splitlines() or [""]
    return "\n  ".join([lines[0]] + [l.rstrip() for l in lines[1:]])


BUCKETS = (("clear", "Clear from the start", "Answered by the notes the operator gave."),
           ("added", "Added during the interview", "Answered in a question round or by the owner."),
           ("default", "Filled by default",
            "Proposed defaults the operator never raised: accepted in a round, or recorded because "
            "no one was there to answer."),
           ("waived", "Waived", "Left out on purpose, with the reason."),
           ("drifted", "Drifted", "The value changed after the slot was first settled."))


def render_readback(rb):
    out = ["# Interview readback", "",
           "%s. The log has %d line(s), sha256 %s." % (rb["progress"], rb["log_lines"], rb["log_sha256"])]
    if rb["open"]:
        out += ["", "Still open: %s." % ", ".join(rb["open"])]
    if rb["uncited_notes"]:
        out += ["", "Notes no answer cites: %s." % ", ".join(rb["uncited_notes"])]
    for key, title, what in BUCKETS:
        out += ["", "## %s" % title, "", what, ""]
        items = rb["buckets"][key]
        if not items:
            out.append("- (none)")
        for i in items:
            if key == "waived":
                out.append("- %s: %s (%s)" % (i["slot"], _text(i["reason"]), i["source"]))
            elif key == "drifted":
                out.append("- %s: first \"%s\" (%s), now \"%s\" (%s)" % (
                    i["slot"], _text(i["first"]), i["first_source"], _text(i["now"]), i["now_source"]))
            else:
                out.append("- %s: %s (%s)" % (i["slot"], _text(i["value"]), i["source"]))
    return "\n".join(out) + "\n"


def signoff(root, by, *, session="cli"):
    """Write the signed file and record the event. Returns {file, log_sha256, new}; `new` is False
    when the latest sign-off already covers this log (nothing is written)."""
    by = " ".join(str(by or "").split())
    if not by:
        raise InterviewError("signoff needs --by <name>: who approved the readback", code=USAGE)
    v = view(root)
    if v["open"]:
        raise InterviewError("%d slot(s) still open: %s. Settle each one (answered, default or "
                             "waived) before the sign-off" % (len(v["open"]), ", ".join(v["open"])))
    st = v["signoff"]
    if st["state"] == "signed":
        return {"file": st["file"], "log_sha256": st["log_sha256"], "new": False}
    rb = readback(root)
    sha = rb["log_sha256"]
    now = util.now_iso()
    from alpaca import note
    rel = "%s/signed-%s-%s.md" % (DIR, note.utc_stamp(now), sha[:12])
    head = "---\nsigned_by: %s\ntime: %s\nlog: %s\nlog_sha256: %s\nlog_lines: %d\n---\n" % (
        by, now, LOG, sha, rb["log_lines"])
    try:
        note._write_new(_path(root, rel), (head + render_readback(rb)).encode("utf-8"))
    except note.NoteError as exc:
        raise InterviewError(str(exc))
    conn = db.connect(root)
    db.append_event(conn, session=session, actor=INSTRUMENT, kind=EVENT,
                    data={"file": rel, "by": by, "log_sha256": sha, "log_lines": rb["log_lines"],
                          "settled": v["settled"], "total": v["total"]})
    return {"file": rel, "log_sha256": sha, "new": True}


# ------------------------------------------------------------------------------ the verb
def _short(value, width=60):
    text = " ".join(str(value).split())
    return text if len(text) <= width else text[:width - 3] + "..."


def _print_status(v):
    print("interview: %s" % v["progress"])
    width = max([len(r["slot"]) for r in v["slots"]] + [4])
    for r in v["slots"]:
        detail = r["reason"] if r["state"] == "waived" else r["value"]
        tail = "  (%s)" % r["source"] if r["source"] else ""
        print("  %-*s  %-8s  %s%s" % (width, r["slot"], r["state"], _short(detail) or "-", tail))
    print("open: %s" % (", ".join(v["open"]) or "none"))
    print("notes no answer cites: %s" % (", ".join(v["uncited_notes"]) or "none"))
    st = v["signoff"]
    if st["state"] == "needed":
        print("sign-off: needed (alpaca interview readback, then alpaca interview signoff --by <name>)")
    elif st["state"] == "signed":
        print("sign-off: signed (%s)" % st["file"])
    else:
        print("sign-off: stale (%s was signed; the log changed after it, so sign off again)" % st["file"])


def _fail(args, code, message):
    from alpaca.gates import verdict
    name = "USAGE" if code == USAGE else verdict.name_of(code)
    if getattr(args, "json", False):
        print(json.dumps({"verdict": name, "reason": message}, indent=1))
    else:
        print("GATE %s: %s (%s)" % (INSTRUMENT, name, message))
    return code


def cmd_interview(args):
    from alpaca import cli
    from alpaca.gates import verdict
    verb = getattr(args, "interview_verb", None)
    root = cli._root()
    try:
        if verb == "status":
            v = view(root)
            code = PASS if not v["open"] else FAIL
            if args.json:
                v.update(verdict=verdict.name_of(code), interview=v["signoff"]["state"])
                print(json.dumps(v, indent=1, sort_keys=True))
            else:
                _print_status(v)
                print(verdict.gate_line(INSTRUMENT, code))
            return code
        if verb == "set":
            if not args.state:
                return _fail(args, USAGE, "set needs one of --answered, --default, --waived, --open")
            entry = append(root, args.slot, args.state, args.value, args.source, args.reason)
            if args.json:
                print(json.dumps(dict(entry, verdict="PASS"), indent=1, sort_keys=True))
            else:
                print("%s: %s (%s)" % (entry["slot"], entry["state"], entry["source"]))
                print(verdict.gate_line(INSTRUMENT, PASS))
            return PASS
        if verb == "readback":
            rb = readback(root)
            if args.json:
                print(json.dumps(dict(rb, verdict="PASS"), indent=1, sort_keys=True))
            else:
                print(render_readback(rb), end="")
                print(verdict.gate_line(INSTRUMENT, PASS))
            return PASS
        if verb == "signoff":
            result = signoff(root, args.by, session=args.session or "cli")
            if args.json:
                print(json.dumps(dict(result, verdict="PASS"), indent=1, sort_keys=True))
            else:
                print(("signed %s" if result["new"] else "already signed: %s (the log has not changed)")
                      % result["file"])
                print(verdict.gate_line(INSTRUMENT, PASS))
            return PASS
    except InterviewError as exc:
        return _fail(args, exc.code, str(exc))
    print("usage: alpaca interview status|set|readback|signoff (see alpaca interview <verb> --help)",
          file=sys.stderr)
    return USAGE


def _parser(sub):
    p = sub.add_parser("interview", help="the slot map a runbook needs: status, set, readback, signoff")
    v = p.add_subparsers(dest="interview_verb")
    st = v.add_parser("status", help="each slot with its state, the open slots and the uncited notes; "
                                     "exit 0 only when no slot is open")
    st.add_argument("--json", action="store_true")
    s = v.add_parser("set", help="append one line to input/interview/log.jsonl")
    s.add_argument("slot")
    g = s.add_mutually_exclusive_group()
    for name in STATES:
        g.add_argument("--" + name, dest="state", action="store_const", const=name)
    s.add_argument("--value", default="", help="the answer, in the operator's words")
    s.add_argument("--source", default="", help="a note path (input/notes/<file>.md), round:<n>/q<n>, or owner")
    s.add_argument("--reason", default=None, help="why a waived slot can be left out")
    s.add_argument("--json", action="store_true")
    rb = v.add_parser("readback", help="the five buckets: clear, added, default, waived, drifted")
    rb.add_argument("--json", action="store_true")
    so = v.add_parser("signoff", help="write the signed readback with the sha256 of the log")
    so.add_argument("--by", default=None, help="who approved the readback")
    so.add_argument("--json", action="store_true")


def _register():
    from alpaca import cli
    cli.command("interview")(cmd_interview)
    cli.register_parser("interview", _parser)


_register()
