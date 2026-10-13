"""alpaca aar: the after-action report of an op, one file per op. The report verbs:

  aar new <op>    writes .alpaca/aar/<op>.md: the Timeline filled from the record (every row ends in
                  the event it came from) and the other sections empty, each with a TODO(agent): line.
  aar check <op>  names every part that is missing, empty or short, an action item without owner,
                  due, size or done-when, a claim with no pointer, a line the report grammar does not
                  read, and a report that changed after it was sealed.
  aar seal <op>   runs the same content rules and SEAL_HOOKS (an accepted review of these exact
                  bytes), then records the sha256 of the bytes it checked.
The owed verbs (t-622, section "owed" below): aar owe, aar owed, aar waive; op close is refused
while an op owes a report. The review verbs (t-624, section "review" below): aar review, accept,
rework and lesson; none of them seals.

Decisions, so a later reader does not have to guess:
- The report has a closed grammar, so the checker never reads a line differently from a markdown
  viewer. A line in any other form is refused with its line number and a hint of what to write.
  Every live line is one of: blank; a one-line comment `<!-- ... -->` at the left margin; a ###
  to ###### heading or a rule (--- after a blank line) at the left margin, which hold no claim; a
  list item at the left margin; a line inside that item (a nested item, text indented to the
  item's text, or a plain line right under a paragraph line); a paragraph line at the left margin.
  Fenced code counts as nothing. The header holds only the # title and the facts aar new writes
  (- op:, - intent:, - done when:, - close basis:, - timeline:), one line each. The Timeline holds
  only one-line items ending in (event:N) for an event of this op; an op with no events (imported)
  keeps an empty Timeline legal. Action items holds GFM tables: a header row, a --- row with as
  many cells, then one row per action up to a blank line. A `## ` heading that is not one of the
  eight sections is refused.
- Refused markup, each named by its line number: tabs, invisible characters (format characters,
  the Hangul fillers and the blank braille cell), raw HTML and comments other than a one-line
  comment, character references, links, images and link definitions (`](`, `]:`), task-list boxes,
  block quotes, # and ## inside a section, setext underlines, indented fences and backtick fences
  with a backtick in their info string. A backslash before `<`, `&` or `]` keeps the character as
  text. Accepted false refusals, each with a message that says what to write instead: inline code
  holding `<x>`, `&name;`, `](` or `]:`; a tab outside fenced code; a fence indented by any space;
  ordered siblings or a paragraph indented 1-3 spaces; a UTF-8 BOM; an emoji ZWJ sequence; a due
  like "next week" or a size like "big"; a pointer with a `_` outside backticks.
- A claim is every list item (top or nested, each on its own) and every paragraph in Impact, Five
  whys, Contributing factors, What went well and Lessons. It needs a visible pointer in the proof
  grammar (event:, local:, receipt:, job:, remote:); `event:` and `local:` must resolve, the others
  need a body only. A pointer whose body has a `_` that is not between two letters or digits must sit
  in backticks. The Action items table and At a glance are not claims.
- A value "says something" when it has a letter or digit besides its pointers and outside struck
  text (~~...~~), and is not a placeholder word (PLACEHOLDERS, or a word starting with todo). A why
  is a top-level ordered item that says something; a claim must say something. due must be a real
  date YYYY-MM-DD; size must be XS to XXL, small,
  medium, large, or a number of hours, days or weeks. At a glance needs 40 visible characters
  besides its pointers, as a proof summary does. A section holding TODO(agent): is one "empty"
  problem. Headings match exactly and each appears once. `\\|` (GFM) is the one pipe escape in the
  table.
- Every value taken from the record is put on one line, cleared of invisible characters, and has
  `\\ < & [ ]` backslash-escaped before it is cut to length, so a draft always reads under the
  grammar (aar new refuses to write one that does not). The reader refuses the line breaks
  str.splitlines() honours and CommonMark does not.
- The aar-new and aar-seal events carry `format` 2: these rules. A later tightening bumps it;
  op-057 t-629 narrowed the due to a date before any report was sealed on the record, so the format
  stayed 2. op-057 t-640 made Carried over required while an item is open without a bump: a sealed
  report is not judged again by check (its sealed bytes keep their seal) and the row grammar did not
  change.
- seal = content rules + SEAL_HOOKS over the bytes it hashes. check = content rules + the compare
  with the latest seal. "Unchecked" means "fails the content rules now"; there is no aar-check event.
- `aar new` refuses over a sealed report, even with --force. An open op may be drafted.
- seal files each action item as a task (alpaca/aaritems.py) in the same transaction as the seal
  event. Carried over holds one one-line row per action item filed from another op's report whose
  task is not done (aaritems.carried, the set aar new writes), each citing that task's task-add
  event; check of an unsealed or edited report, review and seal refuse a report that leaves the
  section out or drops a row while such an item is open; with nothing open the section may be
  absent. The check of the sealed bytes does not apply this rule.
- An op id matches a message body at a token boundary, so OP-001 and op-001-t621 are accepted misses.
"""
import bisect
import datetime
import hashlib
import json
import os
import re
import tempfile
import unicodedata

from alpaca import db, historian, paths, proof, util

KIND_NEW = "aar-new"        # data: {"format": 2, "path", "events": n, "through": max id or None}
KIND_SEAL = "aar-seal"      # data: {"format": 2, "path", "sha256", "bytes", "sections", "actions", "claims"}
TODO_MARK = proof.TODO_MARK
FORMAT = 2                  # 2: the closed report grammar
GLANCE, TIMELINE, IMPACT, WHYS = "At a glance", "Timeline", "Impact", "Five whys"
FACTORS, WELL, ACTIONS, LESSONS = "Contributing factors", "What went well", "Action items", "Lessons"
SECTIONS = (GLANCE, TIMELINE, IMPACT, WHYS, FACTORS, WELL, ACTIONS, LESSONS)   # draft order
AUTHORED = (GLANCE, IMPACT, WHYS, FACTORS, WELL, ACTIONS, LESSONS)            # must be filled
CLAIMED = (IMPACT, WHYS, FACTORS, WELL, LESSONS)                              # items need a pointer
MIN_WHYS = 3
COLUMNS = ("action", "owner", "due", "size", "done_when", "type")
NEEDED = ("owner", "due", "size", "done_when")
PLACEHOLDERS = ("tbd", "tba", "tbc", "none", "n/a", "na", "nil", "null", "unknown", "pending", "later",
                "soon", "see above", "see below", "as above", "same as above", "ditto")
HEADER_FACTS = ("op", "intent", "done when", "close basis", "timeline")
_DUE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_SIZE = re.compile(r"(?i)xs|s|m|l|xl|xxl|small|medium|large|\d+(?:\.\d+)? ?[hdw]")
_TASKBOX = re.compile(r"\[[ xX]\](?: |$)")
GLANCE_CHARS = proof.SUMMARY_CHARS[0]
TIMELINE_KINDS = ("op-open", "task-add", "task-move", "op-close")              # plus matched "msg"
#: pre-seal checks a later task joins (t-624 review accept); empty now. Each is
#: fn(conn, root, op, data: bytes, sha256: str) -> [problem, ...]
SEAL_HOOKS = []     # hooks only read; filing runs after every hook, inside seal's transaction
_ODD_BREAKS = re.compile("[\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")
_ROW_END = re.compile(r"\(event:(\d+)\)[ \t]*$")
_MARK = re.compile(r"^([-*+]|(\d{1,9})[.)])( *)(.*)$")
_HEADLIKE = re.compile(r"^ {0,3}#{1,6}(?: |$)")
_QUOTE = re.compile(r"^ {0,3}>")
_BREAK = re.compile(r"^ {0,3}([-*_])(?: *\1){2,} *$")
_UNDERLINE = re.compile(r"^ {0,3}(?:=+|-+) *$")
_COMMENT_LINE = re.compile(r"^<!--(?!-?>)(?:(?!--).)*-->[ ]*$")
_UNESCAPED = r"(?<!\\)(?:\\\\)*"
_MARKUP = ((re.compile(_UNESCAPED + r"<[A-Za-z/!?]"), "holds HTML"),
           (re.compile(_UNESCAPED + r"&(?:[A-Za-z][A-Za-z0-9]{0,31}|#[0-9]{1,7}|#[xX][0-9A-Fa-f]{1,6});"),
            "holds a character reference"),
           (re.compile(_UNESCAPED + r"\][(:]"), "holds a link, an image or a link definition"))
_UNSEEN = "\u115f\u1160\u3164\uffa0\u2800"     # letters and symbols that render as nothing
_ESCAPE = re.compile(r"([\\<&\[\]])")
_POINTER = re.compile(r"(?<![\w-])(local|event|receipt|job|remote):([^\s`()\[\]<>,;*~\\]+)")
_RULE = re.compile(r":?-+:?")
_STRUCK = re.compile(r"(?<!~)(~~?)(?=[^\s~])(?:(?!\1).)*?(?<=[^\s~])\1(?!~)")    # GFM strikethrough
_PIPE = re.compile(r"(?<!\\)\|")
# op-057 t-623: Carried over is a Timeline-shaped list of one-line rows; op-057 t-640: it is required
# while an item filed by another op's report is still open (_carried_problems), absent otherwise
CARRIED = "Carried over"
OPTIONAL = (CARRIED,)             # known sections outside SECTIONS: required only by their own rule
ROWS_ONLY = (TIMELINE, CARRIED)   # one-line items ending in (event:N), nothing else


# --------------------------------------------------------------------------- paths and the op
def aar_dir(root):
    return os.path.join(paths.runtime_dir(root), "aar")


def report_path(root, op):
    return os.path.join(aar_dir(root), "%s.md" % op)


def _rel(root, path):
    return os.path.relpath(path, root).replace(os.sep, "/")


def op_row(conn, op):
    """The ops row for `op`, or a Refusal. Every entry point calls it before a path is built."""
    rows = db.rows(conn, "ops", "id=?", (op,)) if op else []
    if not rows:
        raise proof.Refusal(["no op %r on the record" % (op,)])
    return rows[0]


def _unsafe(root, path):
    """True when the report path is a link, sits under a linked aar directory or leaves the root."""
    return (os.path.islink(path) or os.path.islink(aar_dir(root))
            or not proof._inside_root(root, path))


def _flat(text, cap=300):
    """One line of plain text for a value taken from the record: no invisible characters, and
    every character that could open markup backslash-escaped, so the draft reads as it is."""
    s = " ".join(str(text if text is not None else "").split()).replace("\u2014", "-")
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf" and ch not in _UNSEEN)
    s = _ESCAPE.sub(r"\\\1", s)[:cap]
    if (len(s) - len(s.rstrip("\\"))) % 2:
        s = s[:-1]
    return s or "-"


def _data(e):
    return e.get("data") if isinstance(e.get("data"), dict) else {}


# --------------------------------------------------------------------------- the draft
def select(conn, op):
    """The events of `op`'s Timeline, (ts, id) order: the whitelisted op kinds, plus messages that
    cite the op or one of its tasks as their ref or name the op at a token boundary in the body."""
    tasks = {t["id"] for t in db.rows(conn, "tasks", "op=?", (op,))}
    named = re.compile(r"(?<![\w-])%s(?![\w-])" % re.escape(op))
    out = []
    for e in db.events(conn, limit=10 ** 9):
        if e["kind"] in TIMELINE_KINDS and e.get("op") == op:
            out.append(e)
        elif e["kind"] == "msg":
            body = _data(e).get("body")
            if (e.get("ref") == op or e.get("ref") in tasks
                    or (isinstance(body, str) and named.search(body))):
                out.append(e)
    out.sort(key=lambda e: (e.get("ts") or "", e["id"]))
    return out


def _row_text(e):
    d, kind = _data(e), e["kind"]
    if kind == "op-open":
        return "opened: %s; done when: %s" % (_flat(d.get("intent")), _flat(d.get("done_when")))
    if kind == "task-add":
        return _flat(d.get("title") or d.get("statement"))
    if kind == "task-move":
        text = "%s -> %s" % (_flat(d.get("from")), _flat(d.get("to")))
        if d.get("reason"):
            text += "; reason: %s" % _flat(d.get("reason"))
        if d.get("proof"):
            text += "; proof: %s" % _flat(d.get("proof"))
        return text
    if kind == "op-close":
        return "closed; basis: %s" % _flat(d.get("basis"))
    return "%s to %s (%s): %s" % (_flat(e.get("actor")), _flat(d.get("to")), _flat(d.get("kind")),
                                  _flat(d.get("body")))


def render(conn, root, op):
    """(draft text, [event ids on the Timeline]). Pure: reads the record, writes nothing."""
    row = op_row(conn, op)
    tl = historian.timeline(conn, op)
    shown = select(conn, op)

    def fact(label, value, pointer):
        return "- %s: %s (%s)" % (label, _flat(value), pointer or "ops row")

    head = ["# After-action report: %s" % op, "",
            "- op: %s (%s)" % (op, _flat(row.get("status"))),
            fact("intent", tl["intent"], tl["intent_pointer"]),
            fact("done when", tl["done_when"], tl["done_pointer"])]
    if tl["judgment"]:
        head.append(fact("close basis", tl["judgment"].get("basis"), tl["judgment_pointer"]))
    head.append("- timeline: %d event(s) from the record%s" % (
        len(shown), (", through event:%d" % max(e["id"] for e in shown)) if shown else ""))
    lines = head + ["", "## %s" % GLANCE, "",
                    "%s what the op set out to do, what happened and what changes now, in a few "
                    "sentences." % TODO_MARK, "", "## %s" % TIMELINE, ""]
    for e in shown:
        lines.append("- %s %s %s %s (event:%d)" % (_flat(e.get("ts"), 40), e["kind"],
                                                   _flat(e.get("ref") or "-", 60),
                                                   _row_text(e), e["id"]))
    todo = {IMPACT: "one list item per effect, each with a pointer (event:N, local:path, remote:ref).",
            WHYS: "a numbered list of at least three whys, each with a pointer.",
            FACTORS: "one list item per factor, each with a pointer.",
            WELL: "one list item per thing that worked, each with a pointer.",
            LESSONS: "one list item per lesson, each with a pointer."}
    for name in (IMPACT, WHYS, FACTORS, WELL, ACTIONS, LESSONS):
        lines += ["", "## %s" % name, ""]
        if name == ACTIONS:
            lines += ["| action | owner | due | size | done-when | type |",
                      "| --- | --- | --- | --- | --- | --- |", "",
                      "%s one row per action; owner, due, size and done-when are required."
                      % TODO_MARK]
        else:
            lines.append("%s %s" % (TODO_MARK, todo[name]))
    lines += _carried_lines(conn, op)
    return "\n".join(lines) + "\n", [e["id"] for e in shown]


def latest_seal(conn, op):
    """The latest aar-seal event of `op`, or None."""
    hits = [e for e in db.events(conn, kind=KIND_SEAL, limit=10 ** 9) if e.get("ref") == op]
    return hits[-1] if hits else None


def _latest_seals(conn):
    """{op: its latest aar-seal event}, read once for every op."""
    out = {}
    for e in db.events(conn, kind=KIND_SEAL, limit=10 ** 9):
        out[e.get("ref")] = e
    return out


def new(conn, root, op, *, force=False, session=None):
    row = op_row(conn, op)
    op = row["id"]
    path = report_path(root, op)
    rel = _rel(root, path)
    if _unsafe(root, path):
        raise proof.Refusal(["%s is outside the project root or a link; aar new writes only "
                             "inside .alpaca/aar" % rel])
    if os.path.lexists(aar_dir(root)) and not os.path.isdir(aar_dir(root)):
        raise proof.Refusal(["%s exists and is not a directory" % _rel(root, aar_dir(root))])
    if os.path.lexists(path) and not os.path.isfile(path):
        raise proof.Refusal(["%s exists and is not a regular file" % rel])
    sealed = latest_seal(conn, op)
    if sealed and os.path.lexists(path):
        raise proof.Refusal(["%s is sealed (event:%d); edit it and run alpaca aar seal %s again "
                             "instead of a new draft" % (rel, sealed["id"], op)])
    existed = os.path.lexists(path)
    if existed and not force:
        raise proof.Refusal(["%s already exists; pass --force to overwrite it" % rel])
    text, shown = render(conn, root, op)
    bad = _draft_problems(text)
    if bad:
        raise proof.Refusal(bad)
    util.write_text(path, text)
    db.append_event(conn, session=session or "cli", actor="agent", kind=KIND_NEW, op=op, ref=op,
                    data={"format": FORMAT, "path": rel, "events": len(shown),
                          "through": max(shown) if shown else None})
    return {"path": path, "rel": rel, "op": op, "events": len(shown),
            "through": max(shown) if shown else None,
            "open": row.get("status") != "closed", "overwrote": existed}


# --------------------------------------------------------------------------- reading it back
def load(root, op):
    """{"path", "rel", "data", "text", "problems"}. A file problem is a problem, never a raise;
    `data` and `text` are None when one stopped the read."""
    path = report_path(root, op)
    rel = _rel(root, path)
    got = {"path": path, "rel": rel, "data": None, "text": None, "problems": []}
    if not os.path.lexists(path):
        got["problems"].append("no report at %s; run alpaca aar new %s" % (rel, op))
        return got
    if _unsafe(root, path):
        got["problems"].append("%s is a link or outside the project root; it is not read" % rel)
        return got
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        got["problems"].append("%s cannot be read (%s)" % (rel, exc.strerror or exc))
        return got
    try:
        got["text"] = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        got["problems"].append("%s is not UTF-8 text (byte %d)" % (rel, exc.start))
        return got
    got["data"] = data
    return got


def parse(text):
    """{"header": [lines], "sections": [{"name", "line", "body": [(lineno, line, inside)]}],
    "problems": [...]}. Sections keep file order and repeats; a repeat is a problem."""
    text = str(text or "")
    out = {"header": [], "sections": [], "problems": []}
    for m in _ODD_BREAKS.finditer(text):
        out["problems"].append(
            "line %d holds a line break other than a newline (U+%04X); replace it with a newline "
            "or a space" % (text.count("\n", 0, m.start()) + 1, ord(m.group())))
    if out["problems"]:
        return out
    cur = None
    for n, (line, inside) in enumerate(proof.fence_scan(text), 1):
        m = None if inside else proof._HEADING.match(line)
        if m:
            cur = {"name": m.group(1).strip(), "line": n, "body": []}
            out["sections"].append(cur)
        elif cur is None:
            out["header"].append((n, line, inside))
        else:
            cur["body"].append((n, line, inside))
    for name in SECTIONS + OPTIONAL:
        hits = [s["line"] for s in out["sections"] if s["name"] == name]
        if len(hits) > 1:
            out["problems"].append("section '%s' appears %d times (lines %s); keep one"
                                   % (name, len(hits), ", ".join(str(n) for n in hits)))
    return out


def _cells(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.replace("\\|", "|").strip() for c in re.split(r"(?<!\\)\|", s)]


def _delim(line):
    """True for a GFM table delimiter row: | --- | :-: |."""
    return (bool(_PIPE.search(line)) and all(_RULE.fullmatch(c) for c in _cells(line))
            or bool(re.fullmatch(r" *(?::-+:?|-+:) *", line)))


def _block(line):
    """The block `line` starts at an indent of 0-3 spaces ("heading", "quote", "table", "break",
    "underline", "item"), or None for plain text."""
    s = line.lstrip(" ")
    if len(line) - len(s) > 3:
        return None
    if _HEADLIKE.match(s):
        return "heading"
    if _QUOTE.match(s):
        return "quote"
    if _delim(s):
        return "table"
    if _BREAK.match(s):
        return "break"
    if _UNDERLINE.match(s):
        return "underline"
    m = _MARK.match(s)
    if m and (m.group(3) or not m.group(4)):
        return "item"
    return None


_SAY = {"heading": "is a heading inside a list item, or a level 1 or 2 heading; use ### at the "
                   "left margin for a sub-heading",
        "quote": "is a block quote",
        "table": "is a table row; only Action items holds a table, under its header and --- row",
        "break": "is a rule inside a list item or right under a paragraph; put a blank line "
                 "above it",
        "underline": "is an underline (=== or ---) or an empty list item",
        "item": "is a list item that does not start at the left margin or under its item's text",
        None: "is indented outside a list item; start it at the left margin or indent it under "
              "its item"}


def _odd(line):
    """Why `line` holds text a markdown viewer may hide, or None."""
    if "\t" in line:
        return "holds a tab; use spaces"
    for ch in line:
        if unicodedata.category(ch) == "Cf" or ch in _UNSEEN:
            return "holds an invisible character (U+%04X); delete it" % ord(ch)
    for rx, why in _MARKUP:
        if rx.search(line):
            return "%s, which a markdown viewer may hide; write plain text (a fenced block for code)" % why
    return None


def _shape(name, body):
    """(units, rows, problems) of one section body; `name` None is the header.

    The closed grammar. Every live line is one of: blank; a one-line comment at the left margin;
    a ### to ###### heading or a rule (---) at the left margin, which hold no claim; a list item
    at the left margin; a line inside that item (a nested item, or text indented to the text of
    the item it belongs to, or a plain line right under a paragraph line); a paragraph line at
    the left margin. Action items holds GFM tables instead (a header row, a --- row with as many
    cells, then rows up to a blank line), the Timeline holds one-line items only, and the header
    may carry its # title. Any other line is a problem, so the checker never reads a line
    differently from a markdown viewer. Each list item, nested or not, and each paragraph is a
    unit: {"line", "top", "ordered", "text", "col"}. rows: [(lineno, line, header)]."""
    where = "the header" if name is None else "section '%s'" % name
    units, rows, problems = [], [], []
    stack, para, head = [], None, None
    skip, opener = None, True

    def bad(why):
        problems.append("line %d in %s %s" % (n, where, why))
    for k, (n, line, inside) in enumerate(body):
        if inside:
            m = proof._FENCE.match(line)
            if m and (line[:1] in " \t" or opener and m.group(1)[0] == "`" and "`" in m.group(2)):
                bad("is a code fence that is indented or has a backtick in its info string")
            stack, para, head = [], None, None
            opener = False
            continue
        opener = True
        if not line.strip(" "):
            para = head = None
            continue
        if _COMMENT_LINE.match(line):
            stack, para, head = [], None, None
            continue
        why = _odd(line)
        if why:
            bad(why)
            stack, para, head = [], None, None
            continue
        ind = len(line) - len(line.lstrip(" "))
        if name == ACTIONS:
            kind = _block(line)
            if head is not None and n == skip:
                continue
            if head is not None:
                if ind or kind:
                    bad(_SAY[kind] if kind else "is an indented table row; start it at the left margin")
                    head = None
                    continue
                rows.append((n, line, head))
                continue
            nxt = body[k + 1] if k + 1 < len(body) else None
            if (not ind and not kind and _PIPE.search(line) and nxt and not nxt[2]
                    and not nxt[1].startswith(" ") and _delim(nxt[1])
                    and len(_cells(nxt[1])) == len(_cells(line))):
                head = [c.strip().lower().replace("-", "_").replace(" ", "_") for c in _cells(line)]
                skip = nxt[0]
                for col in ("action",) + NEEDED:
                    if col not in head:
                        problems.append("the Action items table has no '%s' column"
                                        % col.replace("_", "-"))
                continue
            bad("is not part of the action table (a header row, a --- row with as many cells, "
                "then one row per action)")
            continue
        # inside a list item: the deepest open item whose text column the line reaches
        home = next((u for u in reversed(stack) if ind >= u["col"]), None)
        if stack and name in ROWS_ONLY and (home or para and (ind > 3 or _block(line) is None)):
            bad("continues a %s row; each row is one line" % name)
            continue
        if home is not None:
            rel = line[home["col"]:]
            kind = _block(rel)
            if kind is None or len(rel) - len(rel.lstrip(" ")) > 3:
                (para or home)["text"] += "\n" + line
                if para is None:        # a new paragraph of `home`, or an indented code line
                    stack = stack[:stack.index(home) + 1]
                    para = home if kind is None and len(rel) - len(rel.lstrip(" ")) <= 3 else None
                continue
            if kind != "item":
                bad(_SAY[kind])
                continue
            m = _MARK.match(rel.lstrip(" "))
            if not m.group(4):
                bad("is a list item with no text")
            elif len(m.group(3)) > 4:
                bad("has more than four spaces after its list marker")
            elif _TASKBOX.match(m.group(4)):
                bad("is a task-list item (a checkbox); write it as a plain list item")
            elif para is home and m.group(2) and int(m.group(2)) != 1:
                bad("is a numbered line right under a paragraph line, so it joins that paragraph; "
                    "put a blank line before it or start the list at 1")
            else:
                sub = {"line": n, "top": False, "item": True, "ordered": m.group(2) is not None,
                       "text": m.group(4),
                       "col": home["col"] + len(rel) - len(rel.lstrip(" ")) + len(m.group(1))
                       + len(m.group(3))}
                stack = stack[:stack.index(home) + 1] + [sub]
                units.append(sub)
                para = sub
            continue
        kind = _block(line)
        if para is not None and (ind > 3 or kind is None):
            if name in ROWS_ONLY:
                bad("continues a %s row; each row is one line" % name)
            else:
                para["text"] += "\n" + line         # a plain or lazy continuation line
            continue
        stack = []
        if ind:
            bad(_SAY[kind] if kind else _SAY[None])
            para = None
            continue
        if name is None and line.startswith("# "):
            para = None
            continue
        if kind == "heading" and name not in ROWS_ONLY + (None,) and re.match(r"#{3,6}( |$)", line):
            para = None
            continue
        if kind == "break" and para is None and name not in ROWS_ONLY + (None,):
            continue
        if kind == "item":
            m = _MARK.match(line)
            if not m.group(4):
                bad("is a list item with no text")
            elif len(m.group(3)) > 4:
                bad("has more than four spaces after its list marker")
            elif _TASKBOX.match(m.group(4)):
                bad("is a task-list item (a checkbox); write it as a plain list item")
            elif para is not None and not para["item"] and m.group(2) and int(m.group(2)) != 1:
                bad("is a numbered line right under a paragraph line, so it joins that paragraph; "
                    "put a blank line before it or start the list at 1")
            else:
                para = {"line": n, "top": True, "item": True, "ordered": m.group(2) is not None,
                        "text": m.group(4), "col": len(m.group(1)) + len(m.group(3))}
                stack = [para]
                units.append(para)
                continue
            para = None
            continue
        if kind:
            bad(_SAY[kind])
            para = None
            continue
        if name in ROWS_ONLY:
            bad("is not a %s row; the %s holds only the rows aar new wrote" % (name, name))
            para = None
            continue
        para = {"line": n, "top": True, "item": False, "ordered": False, "text": line, "col": 0}
        units.append(para)
    return units, rows, problems


def _spans(text):
    """[(start, end)] of the code spans in `text`: a backtick run up to the next run as long."""
    out, runs, by_len = [], list(re.finditer(r"`+", text)), {}
    for k, run in enumerate(runs):
        by_len.setdefault(len(run.group()), []).append(k)
    i = 0
    while i < len(runs):
        same = by_len[len(runs[i].group())]
        at = bisect.bisect_right(same, i)
        if at == len(same):
            i += 1
            continue
        j = same[at]
        out.append((runs[i].start(), runs[j].end()))
        i = j + 1
    return out


def _bare(text):
    return _POINTER.sub(" ", text or "")


def _says(text):
    """True when `text` says something besides its pointers: a letter or digit outside struck text
    (~~...~~) that is not a placeholder word (TBD, none, n/a, TODO ...)."""
    bare = _STRUCK.sub(" ", _bare(text))
    if TODO_MARK in (text or "") or not any(ch.isalnum() for ch in bare):
        return False
    word = bare.strip(" \n*_~`#>.,;:!?()[]{}|+-\\'\"").lower()
    return not (word in PLACEHOLDERS or re.match(r"todo\b", word))


def _due(value):
    """A due cell names a real date, YYYY-MM-DD."""
    value = value.strip(" *_")
    if not _DUE.fullmatch(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _size(value):
    return bool(_SIZE.fullmatch(value.strip(" *_")))


def _items(rows):
    out = []
    for n, line, head in rows:
        item = {c: "" for c in COLUMNS}
        for key, value in zip(head, _cells(line)):
            if key in item:
                item[key] = value
        item["line"] = n
        item["missing"] = [c for c in ("action",) + NEEDED if not _says(item[c])
                           or c == "due" and not _due(item[c]) or c == "size" and not _size(item[c])]
        out.append(item)
    return out


def parse_actions(text):
    """The Action items table as data: one dict per row with the COLUMNS keys, "line" (1-based
    file line) and "missing" (the required cells that are empty). Only rows of a table a markdown
    viewer renders are returned. t-623 reads it, from a sealed report."""
    parsed = parse(text)
    sec = next((s for s in parsed["sections"] if s["name"] == ACTIONS), None)
    if sec is None or parsed["problems"]:
        return []
    return _items(_shape(ACTIONS, sec["body"])[1])


def _header(body):
    """The problems of the header: only the # title, blank lines and the facts aar new writes."""
    units, _rows, problems = _shape(None, body)
    facts = re.compile(r"(?:%s): " % "|".join(re.escape(f) for f in HEADER_FACTS))
    for u in units:
        if not (u["top"] and u["item"] and not u["ordered"] and "\n" not in u["text"]
                and facts.match(u["text"])):
            problems.append("line %d in the header is not one of the facts aar new writes (- %s:); "
                            "move it into a section" % (u["line"], ":, - ".join(HEADER_FACTS)))
    return problems


def _draft_problems(text):
    """The problems of a draft `aar new` is about to write: its header and Timeline must read as
    they will in a viewer, whatever the record text holds."""
    parsed = parse(text)
    problems = list(parsed["problems"]) + _header(parsed["header"])
    for s in parsed["sections"]:
        if s["name"] in ROWS_ONLY:
            problems += _shape(s["name"], s["body"])[2]
    return problems


# --------------------------------------------------------------------------- the content rules
def _judge(conn, root, op, path, text, sealed=False):
    """(problems, info) for the content rules R0-R6."""
    parsed = parse(text)
    problems = list(parsed["problems"])
    info = {"sections": [s["name"] for s in parsed["sections"]], "claims": 0}
    if any("line break other than" in p for p in problems):
        return problems, info
    dupes = {n for n in SECTIONS + OPTIONAL if sum(1 for s in parsed["sections"] if s["name"] == n) > 1}
    problems += _header(parsed["header"])
    sec, shape = {}, {}
    for s in parsed["sections"]:
        if s["name"] not in SECTIONS + OPTIONAL:
            problems.append("line %d: '## %s' is not a section of this report; use ### for a "
                            "sub-heading" % (s["line"], s["name"]))
            problems += _shape(s["name"], s["body"])[2]
    for name in (GLANCE, TIMELINE) + AUTHORED[1:]:
        found = [s for s in parsed["sections"] if s["name"] == name]
        if name in dupes:
            continue
        if not found:
            near = next((s["name"] for s in parsed["sections"]
                         if s["name"].lower() == name.lower()), None)
            problems.append("section '%s' is missing%s" % (
                name, "; found '%s'" % near if near else ""))
            continue
        sec[name] = found[0]
        shape[name] = _shape(name, found[0]["body"])
    empty = set()
    for name in AUTHORED:
        if name not in sec:
            continue
        units, rows, bad = shape[name]
        raw = "\n".join(line for _n, line, _i in sec[name]["body"])
        if TODO_MARK not in raw:
            problems.extend(bad)
            if bad or (rows if name == ACTIONS else [u for u in units if _says(u["text"])]):
                continue
        problems.append("section '%s' is empty or still holds %s" % (name, TODO_MARK))
        empty.add(name)
    if GLANCE in sec and GLANCE not in empty:
        seen = sum(1 for u in shape[GLANCE][0] for ch in _bare(u["text"])
                   if unicodedata.category(ch)[0] in "LNPS")
        if seen < GLANCE_CHARS:
            problems.append("section 'At a glance' holds %d visible character(s) besides its "
                            "pointers; at least %d are needed" % (seen, GLANCE_CHARS))
    if WHYS in sec and WHYS not in empty:
        why = sum(1 for u in shape[WHYS][0] if u["top"] and u["ordered"] and _says(u["text"]))
        if why < MIN_WHYS:
            bullet = next((u["line"] for u in shape[WHYS][0] if u["top"] and u["item"]
                           and not u["ordered"]), None)
            problems.append("section 'Five whys' holds %d why(s); at least %d are needed%s"
                            % (why, MIN_WHYS, "; line %d is a bullet, number each why 1. 2. 3."
                               % bullet if bullet else ""))
    if ACTIONS in sec and ACTIONS not in empty:
        for item in _items(shape[ACTIONS][1]):
            for col in item["missing"]:
                problems.append("action item on line %d has no %s%s" % (
                    item["line"], col.replace("_", "-"), " (a date YYYY-MM-DD)" if col == "due" else ""))
    for name in CLAIMED:
        if name not in sec or name in empty:
            continue
        for u in shape[name][0]:
            n, claim = u["line"], u["text"]
            info["claims"] += 1
            if not _says(claim):
                problems.append("claim on line %d in '%s' says nothing besides a placeholder or "
                                "its pointer" % (n, name))
            found = [(k, v.rstrip(".")) for k, v in _POINTER.findall(claim)]
            spans = _spans(claim)
            for m in _POINTER.finditer(claim):
                if (re.search(r"(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])", m.group(2))
                        and not any(a <= m.start() < b for a, b in spans)):
                    problems.append("claim on line %d in '%s': %s may read differently in a "
                                    "viewer; put it in backticks" % (n, name, m.group(0)))
            if not found:
                problems.append("claim on line %d in '%s' carries no pointer (event:N, local:path, "
                                "receipt:, job:, remote:)" % (n, name))
            for kind, rest in found:
                if kind in ("event", "local") or not rest:
                    item = {"pointer": "%s:%s" % (kind, rest), "kind": kind, "note": None}
                    try:
                        proof.resolve_evidence(conn, root, item, report=path)
                    except proof.Refusal as exc:
                        problems.extend("claim on line %d in '%s': %s" % (n, name, p)
                                        for p in exc.problems)
    if TIMELINE in sec:
        problems.extend(shape[TIMELINE][2])
        known = {e["id"] for e in select(conn, op)}
        cited = 0
        for u in shape[TIMELINE][0]:
            m = _ROW_END.search(u["text"])
            if not m or int(m.group(1)) not in known:
                problems.append("timeline row on line %d cites no event of %s" % (u["line"], op))
            else:
                cited += 1
        if known and not cited:     # an imported op has no events, so its empty Timeline is legal
            problems.append("section 'Timeline' holds no row that cites an event of %s" % op)
    problems += _carried_problems(conn, op, parsed, sealed)
    return problems, info


def content_problems(conn, root, op, path, text, sealed=False):
    return _judge(conn, root, op, path, text, sealed)[0]


def _is_sealed(ev, data):
    """True when `data` are the bytes the seal event `ev` hashed: their check keeps the old rules."""
    return bool(ev) and data is not None and hashlib.sha256(data).hexdigest() == _data(ev).get("sha256")


# --------------------------------------------------------------------------- check and seal
def check(conn, root, op):
    """The verdict on the report as it is now: {"op", "path", "rel", "sha256", "sealed", "changed",
    "actions", "problems"}. Raises Refusal only for an unknown op."""
    row = op_row(conn, op)
    op = row["id"]
    got = load(root, op)
    ev = latest_seal(conn, op)
    problems = list(got["problems"]) or content_problems(conn, root, op, got["path"], got["text"],
                                                         sealed=_is_sealed(ev, got["data"]))
    sha = hashlib.sha256(got["data"]).hexdigest() if got["data"] is not None else None
    changed = False
    if ev:
        if got["data"] is None:
            if not got["problems"]:
                problems.append("the sealed report %s is gone (sealed at event:%d)" % (got["rel"], ev["id"]))
            else:
                problems.append("the sealed report %s cannot be read (sealed at event:%d)"
                                % (got["rel"], ev["id"]))
        elif sha != _data(ev).get("sha256"):
            changed = True
            problems.append("%s changed since it was sealed (event:%d); re-seal it with alpaca "
                            "aar seal %s" % (got["rel"], ev["id"], op))
    items = parse_actions(got["text"]) if got["text"] is not None else []
    return {"op": op, "path": got["path"], "rel": got["rel"], "sha256": sha, "sealed": ev,
            "changed": changed, "actions": len(items), "problems": problems}


def seal(conn, root, op, *, session=None, into=None):
    """Check the report, then record the sha256 of the bytes that were checked. Never reads the
    seal state, so an edited sealed report that is valid again re-seals (the latest seal wins).
    Files every action item in the same transaction as the seal event (aaritems.file_items); call
    it outside a transaction."""
    if conn.in_transaction:
        raise RuntimeError("aar seal opens its own transaction; call it outside one")
    row = op_row(conn, op)
    op = row["id"]
    got = load(root, op)
    info = None
    if got["problems"]:
        problems = list(got["problems"])
    else:
        problems, info = _judge(conn, root, op, got["path"], got["text"])
    if problems:
        raise proof.Refusal(problems)
    mark = got["path"] + ".seal.json"
    if os.path.lexists(mark) and (os.path.islink(mark) or not os.path.isfile(mark)):
        raise proof.Refusal(["%s exists and is not a regular file" % _rel(root, mark)])
    sha = hashlib.sha256(got["data"]).hexdigest()
    for hook in list(SEAL_HOOKS):
        problems.extend(hook(conn, root, op, got["data"], sha) or [])
    if problems:
        raise proof.Refusal(problems)
    payload = {"format": FORMAT, "path": got["rel"], "sha256": sha, "bytes": len(got["data"]),
               "sections": info["sections"], "actions": len(parse_actions(got["text"])),
               "claims": info["claims"]}
    from alpaca import aaritems
    try:
        with db.transaction(conn):
            # another op's seal may have filed items since _judge; the rule holds inside the lock
            late = _carried_problems(conn, op, parse(got["text"]))
            if late:
                raise proof.Refusal(late)
            payload["filed"] = aaritems.file_items(conn, root, op, got["text"], into=into,
                                                   session=session or "cli")
            db.append_event(conn, session=session or "cli", actor="agent", kind=KIND_SEAL, op=op,
                            ref=op, data=payload, conn_in_txn=True)
    except ValueError as exc:
        raise proof.Refusal(["the action items could not be filed, so %s is not sealed: %s"
                             % (got["rel"], exc)]) from exc
    util.write_text(mark, json.dumps(payload, indent=1, sort_keys=True) + "\n")
    return payload


# --------------------------------------------------------------------------- owed: triggers, owe, waive (t-622)
KIND_OWE = "aar-owe"        # data: {"format": 2, "reason": one line, "event": id when it ties one}
KIND_WAIVE = "aar-waive"    # data: {"format": 2, "through": id, "triggers": [ids], "quote", "by", "owner_word"}
REOPEN_TO = ("open", "doing", "blocked", "deferred")
SHARED_SESSIONS = ("cli", "alpaca")     # fallback session ids many callers share: never attributed
OWE_REASON_CAP = 300
NO_GO = "NO-GO"
FAIL = "release check FAIL"


def _one_line(text, cap=120):
    """Terminal text for a value taken from the record: one line, capped."""
    return _plain(text)[:cap] or "-"


def _plain(text):
    """One line of printable text: whitespace runs collapse to one space and every other control
    character (ESC among them) is dropped, so a stored reason cannot drive a terminal."""
    text = "".join(c for c in str(text if text is not None else "") if c.isprintable() or c.isspace())
    return " ".join(text.split())


def _reopens(conn):
    """A task moved from done back to open, doing, blocked or deferred. done -> done is not one."""
    marks = ",".join("?" * len(REOPEN_TO))
    rows = conn.execute(
        "SELECT id, ts, op, ref, json_extract(data,'$.to') AS dest FROM events WHERE kind='task-move' "
        "AND json_valid(data) AND json_type(data)='object' AND json_extract(data,'$.from')='done' "
        "AND json_extract(data,'$.to') IN (%s) ORDER BY id" % marks, REOPEN_TO).fetchall()
    return [{"id": r["id"], "ts": r["ts"], "op": r["op"], "trigger": "reopen",
             "text": "%s moved from done to %s" % (_one_line(r["ref"], 60), r["dest"])} for r in rows]


def _owes(conn):
    rows = conn.execute(
        "SELECT id, ts, op, json_extract(data,'$.reason') AS reason, json_extract(data,'$.event') AS event "
        "FROM events WHERE kind=? AND json_valid(data) ORDER BY id", (KIND_OWE,)).fetchall()
    out = []
    for r in rows:
        tie = r["event"] if isinstance(r["event"], int) and not isinstance(r["event"], bool) else None
        text = _one_line(r["reason"])
        out.append({"id": r["id"], "ts": r["ts"], "op": r["op"], "trigger": "aar owe", "event": tie,
                    "text": "ties event:%d: %s" % (tie, text) if tie is not None else text})
    return out


def _no_gos(conn, root):
    """A NO-GO decision saved by a review flow of the profile, with the session that decided it. It
    goes to the op in the event's op column, else (triggers) to the one op of the tasks that session
    held in doing at that event; else it is unattributed and blocks the ops open when it was
    recorded (owed). A takeover or renew re-saves the stored decision; the copy is the same fact
    (key kind, ref, decided_at), the first id wins and the later ids are kept in `copies`."""
    from alpaca import profile
    # profile.load answers EMPTY for a profile that did not load; a gate must fail closed (strict).
    kinds = profile.listed(profile.strict(root, "events"), "review_flow_kinds")
    if not kinds:
        return []
    marks = ",".join("?" * len(kinds))
    rows = conn.execute(
        "SELECT id, ts, kind, op, ref, session, json_extract(data,'$.decision.decided_at') AS at FROM events "
        "WHERE kind IN (%s) AND json_valid(data) AND json_extract(data,'$.decision.value')='NO-GO' "
        "ORDER BY id" % marks, tuple(kinds)).fetchall()
    out, seen = [], {}
    for r in rows:
        key = (r["kind"], r["ref"], str(r["at"]))
        if key in seen:
            seen[key]["copies"].append(r["id"])
            continue
        seen[key] = {"id": r["id"], "ts": r["ts"], "op": r["op"], "trigger": NO_GO, "copies": [],
                     "session": r["session"], "text": "NO-GO on %s" % _one_line(r["ref"], 60)}
        out.append(seen[key])
    return out


def _holders(conn, ids):
    """{event id: {session: {task, ...}}}: who held which task at each event, by one replay of
    claim, claim-release, lease-expired and task-move events, oldest first. A claim or a
    move to doing holds the task for that session; any other move or an end event frees it. A hold
    lasts until an end event is on the record: a lease that lapsed with no lease-expired row still
    counts, because the record is read as written. The event `session` column is used on both sides."""
    rows = conn.execute(
        "SELECT id, kind, session, ref, CASE WHEN json_valid(data) THEN json_extract(data,'$.to') END "
        "AS dest FROM events WHERE kind IN ('claim','claim-release','lease-expired','task-move') "
        "ORDER BY id").fetchall()
    out, held, i = {}, {}, 0
    for fid in sorted(ids):
        while i < len(rows) and rows[i]["id"] < fid:
            r = rows[i]
            i += 1
            if r["kind"] == "claim" or (r["kind"] == "task-move" and r["dest"] == "doing"):
                held[r["ref"]] = r["session"]
            else:
                held.pop(r["ref"], None)
        snap = {}
        for task, sess in held.items():
            snap.setdefault(sess, set()).add(task)
        out[fid] = snap
    return out


def _release_fails(conn):
    """A release check FAIL receipt (kind release-check; the other receipts are not triggers). The
    event has no op and `triggers` attributes it. The same receipt recorded again (key ref, sha256)
    is one fact; the later ids are kept in `copies`."""
    rows = conn.execute(
        "SELECT id, ts, session, ref, json_extract(data,'$.sha256') AS sha FROM events WHERE "
        "kind='release-check' AND json_valid(data) AND json_extract(data,'$.status')='FAIL' "
        "ORDER BY id").fetchall()
    out, seen = [], {}
    for r in rows:
        key = (r["ref"], str(r["sha"]))
        if key in seen:
            seen[key]["copies"].append(r["id"])
            continue
        seen[key] = {"id": r["id"], "ts": r["ts"], "op": None, "trigger": FAIL, "copies": [],
                     "session": r["session"], "text": "attempt %s" % _one_line(r["ref"], 40)}
        out.append(seen[key])
    return out


def _open_at(conn, op, event):
    """True when `op` was open at event `event`: its newest op-open event lies before it and no
    op-close event for it lies between the two. Event ids, never ts text."""
    opened = conn.execute("SELECT MAX(id) FROM events WHERE kind='op-open' AND op=? AND id<?",
                          (op, event)).fetchone()[0]
    if opened is None:
        return False
    return conn.execute("SELECT 1 FROM events WHERE kind='op-close' AND op=? AND id>? AND id<?",
                        (op, opened, event)).fetchone() is None


def _attribute(conn, found):
    """Tie each NO-GO with no op column and each release check FAIL to the one op of the tasks its
    session held at that event (a claim or a move to doing holds a task; _holders). A shared
    session, no task held, or tasks in two ops leave it unattributed, named by its session. A task of
    a standing op (it never closes) or of an op that was not open at the event is not counted."""
    todo = [t for t in found if t["op"] is None and t["trigger"] in (NO_GO, FAIL)]
    if not todo:
        return
    held = _holders(conn, [t["id"] for t in todo])
    task_op = {r["id"]: r["op"] for r in conn.execute("SELECT id, op FROM tasks")}
    standing = _standing_ops(conn)
    for t in todo:
        who = t.get("session") or ""
        ops = set()
        if who and who not in SHARED_SESSIONS:
            ops = {task_op.get(x) for x in held[t["id"]].get(who, ())} - {None}
            ops = {o for o in ops if o not in standing and _open_at(conn, o, t["id"])}
        if len(ops) == 1:
            t["op"] = ops.pop()
        else:
            t["text"] += ", session %s" % _one_line(who, 40)


def _standing_ops(conn):
    """The ids of the standing ops (ops.STANDING_KINDS), found by their marker events."""
    from alpaca import ops as _ops
    return {r["op"] for r in conn.execute(_ops._STANDING_OPS + " GROUP BY op")}


def triggers(conn, root):
    """Every trigger fact on the record, oldest first: {id, ts, op or None, trigger, text}, plus
    `session` and `copies` on a NO-GO or release check FAIL and `event` on an aar owe row that ties
    one. Derived each time; nothing is stored."""
    found = _reopens(conn) + _owes(conn) + _no_gos(conn, root) + _release_fails(conn)
    _attribute(conn, found)
    return sorted(found, key=lambda t: t["id"])


def _waive_cuts(conn):
    """{op: cut} of the owner waivers on the record, read once for every op: a row counts only when
    the verb could have written it (a human identity with an owner word), and never past its own id
    (a waiver never answers a later trigger)."""
    from alpaca import ownerword
    cuts = {}
    for r in conn.execute("SELECT id, op, actor, json_extract(data,'$.through') AS t, "
                          "json_extract(data,'$.by') AS by, json_extract(data,'$.owner_word.path') AS path "
                          "FROM events WHERE kind=? AND json_valid(data)", (KIND_WAIVE,)):
        if (isinstance(r["t"], int) and r["actor"] in ownerword.HUMAN_IDENTITIES
                and r["by"] in ownerword.HUMAN_IDENTITIES and r["path"] in ("session", "terminal")):
            cuts[r["op"]] = max(cuts.get(r["op"], 0), min(r["t"], r["id"] - 1))
    return cuts


def _open_since(conn):
    """{open op: id of its first op-open event}. An open op with no op-open event counts as open
    since the start (0), so it is never left out. Event ids, never ts text (t-622)."""
    return {r["id"]: r["since"] or 0 for r in conn.execute(
        "SELECT o.id, (SELECT MIN(e.id) FROM events e WHERE e.kind='op-open' AND e.op=o.id) AS since "
        "FROM ops o WHERE o.status='open'")}


def _ties(conn, found):
    """{event id: aar-owe row}: the first `aar owe --event` row naming each unattributed NO-GO or
    release check FAIL. A row counts only when its event is the id of such a trigger, is older than
    the row, and the row's op was open when the trigger was recorded; any other aar-owe row is a
    plain owe of its op."""
    loose = {t["id"] for t in found if t["op"] is None and t["trigger"] in (NO_GO, FAIL)}
    ties = {}
    since = None
    for t in found:
        e = t.get("event")
        if t["trigger"] != "aar owe" or e not in loose or e >= t["id"] or e in ties:
            continue
        if since is None:
            since = {r["id"]: r["since"] or 0 for r in conn.execute(
                "SELECT o.id, (SELECT MIN(e.id) FROM events e WHERE e.kind='op-open' AND e.op=o.id) "
                "AS since FROM ops o")}
        if t["op"] in since and since[t["op"]] < e:
            ties[e] = t
    return ties


def _cut(root, op, ev, waived):
    """(cut, note) of `op` from its latest aar-seal event `ev` (or None) and the waiver cuts
    `waived` ({op: cut}): the triggers with an id up to `cut` are answered. The seal answers while
    it holds (the report on disk still hashes to it); an owner waive answers up to its `through`."""
    cut, note = 0, ""
    if ev:
        got = load(root, op)
        if (got["data"] is not None and not got["problems"]
                and hashlib.sha256(got["data"]).hexdigest() == _data(ev).get("sha256")):
            cut = ev["id"]
        else:
            note = "the sealed report changed or is gone since event:%d" % ev["id"]
    return max(cut, waived.get(op, 0)), note


def _seal_cut(conn, root, op):
    """(cut, note) of `op`, read from the record; see `_cut`."""
    return _cut(root, op, latest_seal(conn, op), _waive_cuts(conn))


def owed(conn, root, op=None):
    """({op: [trigger, ...]}, [unattributed trigger, ...]): the triggers recorded after the op's
    last sealed report or owner waive. Ops with nothing left are not in the dict. A NO-GO or release
    check FAIL no op is tied to is in the second list, with `blocks` (the open ops that were open
    when it was recorded and have not waived it); it is also in the list of each of those ops with
    `held` set. It is answered for that op the way an attributed one is, by the op's sealed report or
    the owner's waive (each up to its own event id); `aar owe <op> --event` moves it to the op it
    belongs to instead. One recorded while no op was open blocks nothing, and a reopen with no op is
    listed and blocks nothing."""
    by_op, loose = {}, []
    found = triggers(conn, root)
    ties = _ties(conn, found)
    for t in found:
        if t["op"] is None:
            if t["id"] not in ties:
                loose.append(t)
        elif op is None or t["op"] == op:
            by_op.setdefault(t["op"], []).append(t)
    out, cuts, reads = {}, {}, []

    def cut(x):
        if not reads:
            reads.append((_latest_seals(conn), _waive_cuts(conn)))
        if x not in cuts:
            cuts[x] = _cut(root, x, reads[0][0].get(x), reads[0][1])[0]
        return cuts[x]

    for name, items in sorted(by_op.items()):
        due = [t for t in items if t["id"] > cut(name)]
        if due:
            out[name] = due
    if loose:
        since = _open_since(conn)
        for t in loose:
            t["blocks"] = [x for x in sorted(since) if t["trigger"] in (NO_GO, FAIL) and since[x] < t["id"]
                           and t["id"] > cut(x)]
            for x in t["blocks"]:
                if op is None or x == op:
                    out.setdefault(x, []).append(dict(t, held=True))
        for items in out.values():
            items.sort(key=lambda t: t["id"])
    return out, loose


def describe(t):
    return "%s event:%d (%s%s)" % (t["trigger"], t["id"], t["text"],
                                   "; no op is tied to it" if t.get("held") else "")


def gate_text(conn, root, op, due):
    """The text inside `BLOCKED (...)` of op close, so the wording lives with the rule."""
    note = _seal_cut(conn, root, op)[1]
    held = [t for t in due if t.get("held")]
    text = "AAR owed for %d trigger(s): %s%s" % (len(due), "; ".join(describe(t) for t in due),
                                                 "; " + note if note else "")
    text += ("; seal the report with alpaca aar seal %s, or the owner waives it with alpaca aar "
             "waive %s --by owner --quote ..." % (op, op))
    if held:
        many = len(held) > 1
        text += ("; %s tied to no op: alpaca aar owe <op> --event <id> moves it to the op it belongs "
                 "to (that op then owes the report)"
                 % (", ".join("event:%d" % t["id"] for t in held) + (" are" if many else " is")))
    return text


def answered(conn, root, op):
    """op-057 t-625: what the hub shows beside the owed list, read through the rule op close uses:
    {"seal": the latest aar-seal id or None, "holds": the report on disk still hashes to that seal,
    "note": _seal_cut's note ("" when none), "report": the report's path from the root when it is a
    regular file inside the root, else None}. Reads only; `op` comes from the ops table."""
    ev = latest_seal(conn, op)
    note = _seal_cut(conn, root, op)[1]
    path = report_path(root, op)
    on_disk = os.path.isfile(path) and not _unsafe(root, path)
    return {"seal": ev["id"] if ev else None, "holds": bool(ev) and not note, "note": note,
            "report": _rel(root, path) if on_disk else None}


def owe(conn, op, reason, *, session=None, event=None, root=None):
    """Record that `op` owes an AAR. A closed op is accepted: it is listed and blocks nothing. With
    `event`, tie that unattributed NO-GO or release check FAIL to `op`, which must have been open
    when it was recorded (and be open now, or closed since): `op` then owes the report for it and it stops blocking
    every other op. The check and the write share one transaction, so two sessions cannot both tie
    one trigger."""
    op = op_row(conn, op)["id"]
    if event is None:
        text = _plain(reason)
        if not _says(text) or len(text) < 4:
            raise proof.Refusal(["--reason must say in one line why %s owes an AAR" % op])
        if len(text) > OWE_REASON_CAP:
            raise proof.Refusal(["--reason is %d characters; at most %d" % (len(text), OWE_REASON_CAP)])
        return db.append_event(conn, session=session or "cli", actor="agent", kind=KIND_OWE, op=op,
                               ref=op, data={"format": FORMAT, "reason": text})
    with db.transaction(conn):
        target = _tie_target(conn, root, op, event)
        text = _plain(reason) if reason else describe(target)[:OWE_REASON_CAP]
        if not _says(text) or len(text) < 4:
            raise proof.Refusal(["--reason must say in one line why %s owes an AAR" % op])
        if len(text) > OWE_REASON_CAP:
            raise proof.Refusal(["--reason is %d characters; at most %d" % (len(text), OWE_REASON_CAP)])
        return db.append_event(conn, session=session or "cli", actor="agent", kind=KIND_OWE, op=op,
                               ref=op, data={"format": FORMAT, "reason": text, "event": event},
                               conn_in_txn=True)


def _tie_target(conn, root, op, event):
    """The unattributed trigger `event` that `op` may take, or a Refusal that says why not."""
    found = triggers(conn, root)
    mine = [t for t in found if t["id"] == event]
    if not mine:
        for t in found:
            if event in t.get("copies", ()):
                raise proof.Refusal(["event:%d is a copy of event:%d; pass --event %d"
                                     % (event, t["id"], t["id"])])
        raise proof.Refusal(["event:%d is not a NO-GO or release check FAIL on the record" % event])
    t = mine[0]
    if t["trigger"] not in (NO_GO, FAIL):
        raise proof.Refusal(["event:%d is a %s, not a NO-GO or release check FAIL" % (event, t["trigger"])])
    if t["op"] is not None:
        raise proof.Refusal(["event:%d already goes to %s" % (event, _one_line(t["op"], 40))])
    prior = _ties(conn, found).get(event)
    if prior:
        raise proof.Refusal(["event:%d is already tied to %s by event:%d"
                             % (event, _one_line(prior["op"], 40), prior["id"])])
    status = conn.execute("SELECT status FROM ops WHERE id=?", (op,)).fetchone()["status"]
    if op in _standing_ops(conn):
        raise proof.Refusal(["%s is a standing op and never closes; tie event:%d to the op the work "
                             "belongs to" % (op, event)])
    # A closed op takes a trigger only when it was open as the trigger was recorded (a release check
    # that failed before the gate existed, under an op closed since): the report is then owed by the
    # op the work belonged to. Any other closed op would only clear the trigger from the open ops.
    if status != "open" and not _open_at(conn, op, event):
        raise proof.Refusal(["%s is %s and was not open at event:%d; a trigger is tied only to an op "
                             "that is open or was open when it was recorded" % (op, status, event)])
    since = _open_since(conn)
    if status == "open" and since[op] >= event:
        standing = _standing_ops(conn)
        can = [x for x in sorted(since) if since[x] < event and x not in standing]
        raise proof.Refusal(["%s opened after event:%d was recorded; it can go to %s"
                             % (op, event, ", ".join(can) or "no op")])
    return t


def waive(conn, root, op, by, quote, *, session=None, environ=None, isatty=None, read=None):
    """The owner waives the AAR `op` owes, through its newest trigger. Every refusal comes before
    the write. Only the owner's own words from this session count (ownerword.human_write)."""
    from alpaca import ownerword
    op = op_row(conn, op)["id"]
    due = owed(conn, root, op)[0].get(op)
    if not due:
        raise proof.Refusal(["%s owes no AAR; there is nothing to waive" % op])
    newest = max(due, key=lambda t: t["id"])
    # The id form, as batch answer and brief sign: the owner's whole message is "<op>: waive".
    word = ownerword.human_write(conn, session, by, subject=op, answer="waive", quote=quote, root=root,
                                 since=max(t["ts"] for t in due), environ=environ, isatty=isatty,
                                 read=read)
    stored = (quote or "").strip()
    # human_write reads only the stretch from the op id to a comma, so words before it ("do not
    # type op-1: waive") would pass; a waiver removes a duty, so the whole message must be the answer.
    if word.get("path") == "session" and stored != "%s: waive" % op:
        raise proof.Refusal(["the owner's message must be exactly '%s: waive'" % op])
    if word.get("transcript"):
        used = conn.execute(
            "SELECT id FROM events WHERE kind=? AND json_valid(data) AND "
            "json_extract(data,'$.owner_word.transcript')=? AND json_extract(data,'$.owner_word.line')=? "
            "ORDER BY id LIMIT 1", (KIND_WAIVE, word.get("transcript"), word.get("line"))).fetchone()
        if used:
            raise proof.Refusal(["that owner message already waived event:%d; a new waiver needs a "
                                 "new message" % used["id"]])
    by = str(by).strip()
    return db.append_event(conn, session=session or "cli", actor=by, kind=KIND_WAIVE, op=op, ref=op,
                           data={"format": FORMAT, "through": newest["id"],
                                 "triggers": [t["id"] for t in due], "quote": stored, "by": by,
                                 "owner_word": word})


def _cmd_owed(args, conn, root, sid):
    from alpaca import cli, ownerword, profile
    verb = args.aar_verb
    gate = "GATE alpaca-aar-%s" % verb
    try:
        if verb == "owe":
            ev = owe(conn, args.op, args.reason, session=sid, event=args.event, root=root)
            print("%s: PASS (%s owes an AAR, event:%d%s)"
                  % (gate, ev["op"], ev["id"], ", ties event:%d" % args.event if args.event else ""))
            return cli.PASS
        if verb == "waive":
            ev = waive(conn, root, args.op, args.by, args.quote, session=sid)
            d = json.loads(ev["data"]) if isinstance(ev["data"], str) else ev["data"]
            print("%s: PASS (%s: %d trigger(s) through event:%d waived by %s, event:%d)"
                  % (gate, ev["op"], len(d["triggers"]), d["through"], d["by"], ev["id"]))
            return cli.PASS
        if args.op:
            op_row(conn, args.op)
        due, loose = owed(conn, root, args.op)
    except proof.Refusal as exc:
        print("%s: FAIL (%s)" % (gate, "; ".join(exc.problems)))
        return cli.FAIL
    except ownerword.OwnerWordRefusal as exc:
        print("%s: FAIL (%s: %s)" % (gate, exc.reason, exc.detail))
        return cli.FAIL
    except profile.ProfileError as exc:
        print("%s: FAIL (the review-flow NO-GOs cannot be read: %s)" % (gate, exc))
        return cli.FAIL
    status = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM ops")}
    for name, items in due.items():
        closed = status.get(name) == "closed"       # an event may name an op with no row
        print("%s%s owes an AAR:" % (name, " (closed)" if closed else ""))
        for t in items:
            print("  - %s" % describe(t))
    if not args.op and loose:
        print("unattributed (no op can be tied to these; each op it blocks is cleared by its own sealed "
              "report or the owner's waive, or alpaca aar owe <op> --event <id> ties it to the op it "
              "belongs to):")
        for t in loose:
            print("  - %s; %s" % (describe(t), "blocks " + ", ".join(t["blocks"]) if t["blocks"] else
                                  "blocks no open op (none was open when it was recorded, or each has "
                                  "sealed its report, waived it or closed)"))
    if not due and (args.op or not loose):
        print("no AAR owed")
    return cli.PASS


def _owed_parser(av):
    o = av.add_parser("owe", help="record that an op owes an after-action report, and why")
    o.add_argument("op")
    o.add_argument("--reason", help="one line: why the op owes a report (optional with --event)")
    o.add_argument("--event", type=int, help="the id of a NO-GO or release check FAIL no op is tied to: "
                   "it goes to this op, which then owes the report")
    d = av.add_parser("owed", help="list the triggers each op has not answered, with event ids")
    d.add_argument("op", nargs="?")
    w = av.add_parser("waive", help="the owner waives the report an op owes")
    w.add_argument("op")
    w.add_argument("--by", help="a human identity: owner or human")
    w.add_argument("--quote", help="the owner's whole message, exactly '<op>: waive', from this session; one message waives one op")


# --------------------------------------------------------------------------- Carried over
def _carried_row(r):
    return "- %s from the %s AAR, due %s, owner %s, size %s: %s (event:%d)" % (
        _flat(r["task"], 20), _flat(r["source"], 40), _flat(r["due"], 40), _flat(r["owner"], 60),
        _flat(r["size"], 20), _flat(r["action"], 200), r["event"])


def _carried_lines(conn, op):
    """The Carried over section of a draft: one row per still-open action item filed from another
    op's report, each ending in the task-add event of its task. Nothing to carry, nothing written."""
    from alpaca import aaritems
    rows = aaritems.carried(conn, op)
    if not rows:
        return []
    return ["", "## %s" % CARRIED, ""] + [_carried_row(r) for r in rows]


def _missing_row(r):
    """The refusal line for a missing row. aar check and seal print it raw, so it goes through
    _plain: a control character in a stored action cannot reach the terminal. No cap, the line
    carries the row to paste; presence is matched on (event, task), not on the action text."""
    return _plain("carried item %s from the %s AAR is still open and has no row; add: %s" % (
        _flat(r["task"], 20), _flat(r["source"], 40), _carried_row(r)))


def _carried_problems(conn, op, parsed, sealed=False):
    """The problems of a Carried over section: each row cites the task-add event of an action item
    filed from another op's report and names that task. The section is required while an item filed
    from another op's report is not done (aaritems.carried) and every such item needs its row; with
    nothing open it may be absent. `sealed` (the bytes a seal hashed) skips only that open-item rule,
    never the row checks."""
    from alpaca import aaritems
    found = [s for s in parsed["sections"] if s["name"] == CARRIED]
    if len(found) > 1:
        return []                   # parse already reports the duplicate
    wanted = [] if sealed else aaritems.carried(conn, op)
    if not found:
        if not wanted:
            return []
        near = [s["name"] for s in parsed["sections"] if s["name"].lower() == CARRIED.lower()]
        problems = ["section '%s' is missing%s: %d action item(s) filed by other ops' after-action "
                    "reports are still open; aar check, review and seal need one row for each"
                    % (CARRIED, "; found '%s'" % _flat(near[0], 40) if near else "", len(wanted))]
        return problems + [_missing_row(r) for r in wanted]
    units, _rows, problems = _shape(CARRIED, found[0]["body"])
    problems = list(problems)
    known = aaritems.cited(conn, op)
    present = set()
    for u in units:
        m = _ROW_END.search(u["text"])
        if not m or int(m.group(1)) not in known:
            problems.append("carried row on line %d cites no action item filed by another op's "
                            "after-action report" % u["line"])
            continue
        task = known[int(m.group(1))]
        if u["text"].split(" ", 1)[0] != task:
            problems.append("carried row on line %d names %s but cites the task-add of %s (event:%s)"
                            % (u["line"], u["text"].split(" ", 1)[0][:40], task, m.group(1)))
            continue
        present.add((int(m.group(1)), task))
    return problems + [_missing_row(r) for r in wanted if (r["event"], r["task"]) not in present]


# --------------------------------------------------------------------------- review, decision, lessons
# A fresh subagent reviews the report against REVIEW_CHECKS and returns a checked review file. Only
# the session that asked for the review records accept or rework (as in alpaca/runlog/reviewflow.py);
# the decision binds the report's sha256, and seal needs the latest decision for the exact bytes to
# be accept (review_gate, registered in SEAL_HOOKS below). Session names are attribution, not
# authentication. Reports sealed before this gate keep their seal. `aar lesson` hands one Lessons
# item to lessons.write, whose probe decides.
KIND_REVIEW = "aar-review"      # data: {"format": 1, "path", "sha256", "parent", "reviewer", "packet"}
KIND_DECISION = "aar-decision"  # data: {"format": 1, "decision", "sha256", "packet", "reviewer", "review",
#                                        "review_sha256", "reason"}
KIND_LESSON = "aar-lesson"      # data: {"format": 1, "line", "sha256", "admitted", "lesson", "control",
#                                        "reason", "probe"}
REVIEW_FORMAT = 1
DECISIONS = ("accept", "rework")
RESULTS = ("pass", "fail")
REVIEW_VERBS = ("review", "accept", "rework", "lesson")
REVIEW_BYTES = 1 << 20          # a review or probe file larger than this is refused
REVIEW_DEPTH = 8                # a review nested deeper than this (objects and lists) is refused
LINE_CAP = 400                  # the longest line the review verbs print
_TOKEN = re.compile(r"[A-Za-z0-9._:-]{1,80}")   # a reviewer id, a probe id or tag, a --governs tag
REVIEW_CHECKS = ("whys-chain", "root-cause", "pointers", "actions", "g1-placeholders",
                 "g2-math-pointers", "g3-header-facts", "g4-foreign-pointers", "g5-bare-whys",
                 "g6-padded-glance", "g7-struck-pointers")
CHECK_TEXT = {
    "whys-chain": "each why answers the one before it, and none is circular or restates an earlier why",
    "root-cause": "the last why names a process or system cause, not a person or a one-off slip",
    "pointers": "every Timeline and Impact claim has a pointer that resolves (an event id, a message "
                "id or a file) to something that says what the claim says",
    "actions": "every action item is concrete: a real owner, a due, a size and a done-when a reader "
               "can test",
    "g1-placeholders": "no owner or done-when that only fills the cell (nobody, someone, x, done, same)",
    "g2-math-pointers": "no pointer that sits only inside math ($...$), where a viewer hides it",
    "g3-header-facts": "no header fact line (intent, done when, close basis) carrying a claim with no pointer",
    "g4-foreign-pointers": "no pointer to another op's event, and no event:N inside a URL",
    "g5-bare-whys": "no why that says only Why? beside its pointer",
    "g6-padded-glance": "At a glance says what happened in words, not filler or punctuation to reach 40 characters",
    "g7-struck-pointers": "no pointer that is struck through (~~...~~)",
}
_REVIEW_BRIEF = (
    "Review the after-action report %(rel)s (sha256 %(sha12)s) of %(op)s as reviewer %(reviewer)s. "
    "Read the whole report and open what its pointers name before you judge. Judge every check in "
    "`checks` against the report. Write one JSON file shaped like `template` in your scratch folder: "
    "for each check a result (pass or fail), a note of 4-600 characters and a quote of 4-400 "
    "characters copied exactly from the report line it names. Do not edit the report, do not run "
    "alpaca aar accept, rework or seal, and do not read an earlier review. Return the file path, a "
    "short summary and what stays uncertain. Use only the template's keys you need, never the same "
    "key twice in one object, and no deeper nesting than the template. "
    "The skill is .claude/skills/alpaca-aar/SKILL.md.")


def review_dir(root, op):
    return os.path.join(aar_dir(root), "%s.review" % op)


def _review_dir_problems(root, op):
    """The reasons the review folder cannot be used (a list, never a raise)."""
    adir, rdir = aar_dir(root), review_dir(root, op)
    if (os.path.islink(adir) or os.path.lexists(adir) and not os.path.isdir(adir)
            or os.path.islink(rdir) or os.path.lexists(rdir) and not os.path.isdir(rdir)
            or not proof._inside_root(root, os.path.realpath(rdir))):
        return ["%s is a link or not a directory; aar review keeps its files only inside .alpaca/aar"
                % _rel(root, rdir)]
    return []


def _write_bytes(path, data):
    """util.write_text for bytes: a temp file in the folder, then a replace."""
    if os.path.lexists(path) and (os.path.islink(path) or not os.path.isfile(path)):
        raise proof.Refusal(["%s exists and is not a regular file" % os.path.basename(path)])
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    u = os.umask(0)
    os.umask(u)
    os.chmod(tmp, 0o666 & ~u)
    os.replace(tmp, path)


def latest_review(conn, op):
    """The latest aar-review event (packet) of `op`, or None."""
    hits = [e for e in db.events(conn, kind=KIND_REVIEW, limit=10 ** 9) if e.get("ref") == op]
    return hits[-1] if hits else None


def decisions(conn, op):
    """Every aar-decision event of `op`, in record order."""
    return [e for e in db.events(conn, kind=KIND_DECISION, limit=10 ** 9) if e.get("ref") == op]


def review_gate(conn, root, op, data, sha):
    """Seal hook: the latest review decision for these exact bytes must be accept. Reads only
    record events; writes nothing and never raises."""
    mine = [e for e in decisions(conn, op) if _data(e).get("sha256") == sha]
    if mine and _data(mine[-1]).get("decision") == "accept":
        return []
    if mine:
        return ["the latest review decision for this report is rework (event:%d): %s; fix the "
                "report, then run alpaca aar review %s again"
                % (mine[-1]["id"], _flat(_data(mine[-1]).get("reason"), 200), op)]
    return ["no accepted review for the report as it is now (sha256 %s); run alpaca aar review %s "
            "--reviewer <id>, have a fresh subagent review it, then alpaca aar accept %s --review "
            "<file>" % (sha[:12], op, op)]


SEAL_HOOKS.append(review_gate)


def review(conn, root, op, *, reviewer, session=None):
    """Write the packet a fresh subagent answers and record it; {"op", "rel", "sha256", "event",
    "packet", "reviewer"}. The report must pass the content rules first."""
    row = op_row(conn, op)
    op = row["id"]
    got = load(root, op)
    problems = list(got["problems"]) or content_problems(
        conn, root, op, got["path"], got["text"], sealed=_is_sealed(latest_seal(conn, op), got["data"]))
    if problems:
        raise proof.Refusal(problems)
    parent = session or "cli"
    reviewer = (reviewer or "").strip()
    if not _TOKEN.fullmatch(reviewer) or reviewer in (parent, "cli"):
        raise proof.Refusal(["the reviewer id must be 1-80 letters, digits or . _ : - naming the reviewer "
                             "subagent, and differ from your session (%s) and from cli" % _shown(parent)])
    problems = _review_dir_problems(root, op)
    if problems:
        raise proof.Refusal(problems)
    sha = hashlib.sha256(got["data"]).hexdigest()
    folder = _rel(root, review_dir(root, op))
    with db.transaction(conn):
        ev = db.append_event(conn, session=parent, actor="agent", kind=KIND_REVIEW, op=op, ref=op,
                             data={"format": REVIEW_FORMAT, "path": got["rel"], "sha256": sha,
                                   "parent": parent, "reviewer": reviewer, "packet": folder})
        packet = "%s/packet-%d.json" % (folder, ev["id"])
        doc = {"format": REVIEW_FORMAT, "op": op, "report": got["rel"], "report_sha256": sha,
               "packet": ev["id"], "parent": parent, "reviewer": reviewer, "checks": dict(CHECK_TEXT),
               "brief": _REVIEW_BRIEF % {"rel": got["rel"], "sha12": sha[:12], "op": op, "reviewer": reviewer},
               "template": {"op": op, "report_sha256": sha, "reviewer": reviewer,
                            "checks": {c: {"result": "pass or fail", "note": "",
                                           "quote": {"line": 0, "text": ""}} for c in REVIEW_CHECKS},
                            "findings": []}}
        try:
            util.write_text(os.path.join(root, packet), json.dumps(doc, indent=1, sort_keys=True) + "\n")
        except OSError as exc:
            raise proof.Refusal(["the packet %s cannot be written (%s)"
                                 % (packet, _shown(exc.strerror or str(exc)))])
    return {"op": op, "rel": got["rel"], "sha256": sha, "event": ev["id"], "packet": packet,
            "reviewer": reviewer}


def _quote_problems(what, quote, lines):
    """The problems of one {"line", "text"} quote against the report's lines."""
    if not isinstance(quote, dict):
        return ["%s: the quote must be {\"line\": N, \"text\": ...}" % what]
    n, q = quote.get("line"), quote.get("text")
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= len(lines):
        return ["%s: the quote line must be a line number of the report (1-%d)" % (what, len(lines))]
    if not isinstance(q, str) or not 4 <= len(q.strip()) <= 400 or q.strip() not in lines[n - 1]:
        return ["%s: the quote is not on line %d of the report" % (what, n)]
    return []


def _note_ok(note):
    return isinstance(note, str) and 4 <= len(note.strip()) <= 600


# --- untrusted input: review files, probe files and command-line text
class _Repeated(ValueError):
    """A key given twice in one JSON object."""


def _pairs(pairs):
    """json object_pairs_hook: a key given twice is refused, so what is checked is what a person who
    opens the kept file reads (json.loads would keep the last one silently)."""
    if len({k for k, _ in pairs}) != len(pairs):
        raise _Repeated()
    return dict(pairs)


def _too_deep(value, cap):
    """True when decoded JSON nests more than `cap` objects and lists. Walked with a list, never by
    recursion, so no depth the decoder accepts can overflow the stack here."""
    todo = [(value, 1)]
    while todo:
        v, d = todo.pop()
        if isinstance(v, dict):
            v = list(v.values())
        if isinstance(v, list):
            if d > cap:
                return True
            todo.extend((x, d + 1) for x in v)
    return False


def _json_value(text, cap):
    """(value, why) of one untrusted JSON text; why says why it is refused, or is None."""
    try:
        value = json.loads(text, object_pairs_hook=_pairs)
    except _Repeated:
        return None, "repeats a key inside one object"
    except (ValueError, RecursionError):
        return None, "cannot be read as JSON"
    if _too_deep(value, cap):
        return None, "nests deeper than %d levels" % cap
    return value, None


def _shown(value, cap=80):
    """A value this command did not write (a review field, a path, a session id, a lesson's reason) as
    one printable line of at most `cap` characters. A value that is not a str is named by its type, so
    repr and str never run on it."""
    if not isinstance(value, str):
        return "<%s>" % type(value).__name__
    s = " ".join("".join(ch if ch.isprintable() else " " for ch in value[:cap * 4]).split())
    return (s if len(s) <= cap else s[:cap - 3] + "...") or "-"


def _say(text, lead="", cap=LINE_CAP):
    """Print one line of the review verbs. Every line goes through here, so nothing inside it can start
    a second line, run past `cap` or fail to encode."""
    print(lead + _shown(text, cap))


def _arg_problem(flag, value, cap, lines=False):
    """Why a command-line string cannot be used, or None. It must be UTF-8 text (argv bytes that are not
    arrive as lone surrogates), hold no control character (a line break or tab only when `lines`) and
    fit in `cap` characters."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return "%s is not UTF-8 text" % flag
    if any(unicodedata.category(ch) == "Cc" and not (lines and ch in "\n\t") for ch in value):
        return "%s holds a control character" % flag
    if len(value) > cap:
        return "%s is longer than %d characters" % (flag, cap)
    return None


def _read_capped(path, what):
    """(bytes, problem) of a regular file read to at most REVIEW_BYTES. The bound holds on the bytes
    read, so a file that grows after a stat is still refused."""
    shown = "%s %s" % (what, _shown(path, 100))
    if not os.path.isfile(path) or os.path.islink(path):
        return None, "%s is not a regular file" % shown
    try:
        with open(path, "rb") as fh:
            raw = fh.read(REVIEW_BYTES + 1)
    except OSError as exc:
        return None, "%s cannot be read (%s)" % (shown, _shown(exc.strerror or str(exc)))
    if len(raw) > REVIEW_BYTES:
        return None, "%s is larger than %d bytes" % (shown, REVIEW_BYTES)
    return raw, None


def _same_bytes(path, raw):
    """True when `path` is a regular file that holds exactly `raw`."""
    if not os.path.isfile(path) or os.path.islink(path):
        return False
    with open(path, "rb") as fh:
        return fh.read(len(raw) + 1) == raw


def _probe_ok(obj):
    """One probe instance: exactly id, tag, outcome and action; id and tag are tokens, outcome is PASS
    or FAIL, and action is 1-2000 characters."""
    return (isinstance(obj, dict) and set(obj) == {"id", "tag", "outcome", "action"}
            and obj["outcome"] in ("PASS", "FAIL")
            and all(isinstance(obj[k], str) and _TOKEN.fullmatch(obj[k]) for k in ("id", "tag"))
            and isinstance(obj["action"], str) and 1 <= len(obj["action"].strip()) <= 2000)


def _read_review(path, op, pkt, text):
    """(raw bytes, doc, problems) of a reviewer's review file. Never raises; the bytes and the dict
    come back only when there is no problem."""
    d, ident = _data(pkt), "packet event:%s" % pkt["id"]
    raw, why = _read_capped(path, "review file")
    if why:
        return None, None, [why]
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, None, ["review file %s is not UTF-8" % _shown(path, 100)]
    doc, why = _json_value(body, REVIEW_DEPTH)
    if why:
        return None, None, ["review file %s %s" % (_shown(path, 100), why)]
    if not isinstance(doc, dict):
        return None, None, ["review file %s is not a JSON object" % _shown(path, 100)]
    problems = []
    if doc.get("op") != op:
        problems.append("the review names op %s, not %s" % (_shown(doc.get("op")), op))
    if doc.get("report_sha256") != d.get("sha256"):
        problems.append("the review is of sha256 %s; %s is of %s"
                        % (_shown(doc.get("report_sha256"))[:12], ident, str(d.get("sha256"))[:12]))
    if doc.get("reviewer") != d.get("reviewer"):
        problems.append("the review is signed by %s; %s named reviewer %s"
                        % (_shown(doc.get("reviewer")), ident, _shown(d.get("reviewer"))))
    lines = text.split("\n")
    checks = doc.get("checks")
    if not isinstance(checks, dict):
        problems.append("the review holds no checks object")
        checks = {}
    for c in REVIEW_CHECKS:
        if c not in checks:
            problems.append("check %s is missing from the review" % c)
    for c, v in checks.items():
        if c not in REVIEW_CHECKS:
            problems.append("check %s is not a review check" % _shown(c))
        elif not isinstance(v, dict):
            problems.append("check %s: the entry must be an object" % c)
        else:
            if v.get("result") not in RESULTS:
                problems.append("check %s: result must be pass or fail, not %s" % (c, _shown(v.get("result"))))
            if not _note_ok(v.get("note")):
                problems.append("check %s: the note must be 4-600 characters" % c)
            problems += _quote_problems("check %s" % c, v.get("quote"), lines)
    findings = doc.get("findings", [])
    if not isinstance(findings, list) or len(findings) > 50:
        problems.append("findings must be a list of at most 50 items")
        findings = []
    for i, f in enumerate(findings, 1):
        if not isinstance(f, dict) or f.get("check") not in REVIEW_CHECKS:
            problems.append("finding %d: it must be an object naming a review check" % i)
            continue
        if not _note_ok(f.get("note")):
            problems.append("finding %d: the note must be 4-600 characters" % i)
        problems += _quote_problems("finding %d" % i, f.get("quote"), lines)
    return (None, None, problems) if problems else (raw, doc, [])


def decide(conn, root, op, decision, *, session=None, review=None, reason=None):
    """Record accept or rework for the latest packet; the data plus "event" and "rel". Only the
    session that asked for the review decides. All problems come back in one Refusal."""
    row = op_row(conn, op)
    op = row["id"]
    pkt = latest_review(conn, op)
    if pkt is None:
        raise proof.Refusal(["no review packet for %s; run alpaca aar review %s --reviewer <id> first"
                             % (op, op)])
    sid, d = session or "cli", _data(pkt)
    if sid == d.get("reviewer"):
        raise proof.Refusal(["session %s is the reviewer of packet event:%d; the reviewer never "
                             "records accept or rework" % (_shown(sid), pkt["id"])])
    if sid != d.get("parent"):
        raise proof.Refusal(["only the session that asked for the review (%s) records accept or "
                             "rework; this is session %s" % (_shown(d.get("parent")), _shown(sid))])
    kept = raw = None
    if decision == "accept":
        later = [e for e in decisions(conn, op) if e["id"] > pkt["id"]
                 and _data(e).get("decision") == "rework"]
        if later:
            raise proof.Refusal(["packet event:%d was sent back for rework at event:%d; run alpaca "
                                 "aar review %s again for a fresh review" % (pkt["id"], later[0]["id"], op)])
        got = load(root, op)
        if got["problems"]:
            raise proof.Refusal(got["problems"])
        now = hashlib.sha256(got["data"]).hexdigest()
        if now != d.get("sha256"):
            raise proof.Refusal(["the report changed since packet event:%d (sha256 %s then, %s now); "
                                 "run alpaca aar review %s again" % (pkt["id"], str(d.get("sha256"))[:12],
                                                                    now[:12], op)])
        raw, doc, problems = _read_review(util.from_caller(review) if review else "", op, pkt, got["text"])
        if not problems:
            problems = ["check %s failed in the review; record alpaca aar rework %s --reason <why> "
                        "instead" % (c, op) for c, v in doc["checks"].items() if v["result"] == "fail"]
        if not problems:
            problems = _review_dir_problems(root, op)
        if problems:
            raise proof.Refusal(problems)
        kept = "%s/review-%s.json" % (_rel(root, review_dir(root, op)), hashlib.sha256(raw).hexdigest()[:12])
        full = os.path.join(root, kept)
        if not _same_bytes(full, raw):
            _write_bytes(full, raw)
        data = {"format": REVIEW_FORMAT, "decision": "accept", "sha256": now, "packet": pkt["id"],
                "reviewer": d.get("reviewer"), "review": kept,
                "review_sha256": hashlib.sha256(raw).hexdigest(), "reason": None}
    else:
        reason = (reason or "").strip()
        if not 4 <= len(reason) <= 2000 or not _says(reason):
            raise proof.Refusal(["the reason must say what to fix in 4-2000 characters"])
        why = _arg_problem("--reason", reason, 2000, lines=True)
        if why:
            raise proof.Refusal([why])
        data = {"format": REVIEW_FORMAT, "decision": "rework", "sha256": d.get("sha256"),
                "packet": pkt["id"], "reviewer": d.get("reviewer"), "review": None,
                "review_sha256": None, "reason": reason}
    ev = db.append_event(conn, session=sid, actor="agent", kind=KIND_DECISION, op=op, ref=op, data=data)
    return dict(data, event=ev["id"], rel=kept)


def accept(conn, root, op, review, *, session=None):
    return decide(conn, root, op, "accept", session=session, review=review)


def rework(conn, root, op, reason, *, session=None):
    return decide(conn, root, op, "rework", session=session, reason=reason)


def lesson_line(text, n):
    """The Lessons unit (a list item or paragraph, nested or not) whose first line is file line
    `n`, as _shape returns it, or None."""
    parsed = parse(text)
    sec = next((s for s in parsed["sections"] if s["name"] == LESSONS), None)
    if sec is None or parsed["problems"] or not isinstance(n, int) or n < 1:
        return None
    return next((u for u in _shape(LESSONS, sec["body"])[0] if u["line"] == n), None)


def _lesson_value(text):
    """A Lessons item as the lesson's value: pointers and the escapes of the report taken out."""
    s = re.sub(r"\(\s*\)|`\s*`", " ", _POINTER.sub(" ", text or ""))
    return " ".join(re.sub(r"\\([\\<&\[\]|])", r"\1", s).split())


def _probe_items(path):
    """The held-out instances of a probe file (JSON lines), or a Refusal naming the bad line."""
    raw, why = _read_capped(path, "probe file")
    if why:
        raise proof.Refusal([why])
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise proof.Refusal(["probe file %s is not UTF-8" % _shown(path, 100)])
    items, problems = [], []
    for n, line in enumerate(body.split("\n"), 1):
        if not line.strip():
            continue
        obj, why = _json_value(line, 1)
        if _probe_ok(obj):
            items.append(obj)
        else:
            problems.append("probe file %s line %d is not a JSON object with exactly id, tag, outcome "
                            "(PASS or FAIL) and action%s" % (_shown(path, 100), n, " (%s)" % why if why else ""))
    if not problems and not items:
        problems.append("probe file %s holds no instance" % _shown(path, 100))
    if problems:
        raise proof.Refusal(problems)
    return items


def _lesson_event(conn, session, op, n, sha, admitted, lesson, control, reason, probe):
    db.append_event(conn, session=session or "cli", actor="agent", kind=KIND_LESSON, op=op, ref=op,
                    data={"format": REVIEW_FORMAT, "line": n, "sha256": sha, "admitted": admitted,
                          "lesson": lesson, "control": control,
                          "reason": _shown(reason, 600) if reason is not None else None,
                          "probe": probe})


def propose_lesson(conn, root, op, n, probe, *, label, governs, prescribes, description=None,
                   session=None):
    """Hand Lessons line `n` of an accepted report to lessons.write. A RejectedLesson is recorded
    and raised again, never swallowed."""
    row = op_row(conn, op)
    op = row["id"]
    got = load(root, op)
    if got["problems"]:
        raise proof.Refusal(got["problems"])
    sha = hashlib.sha256(got["data"]).hexdigest()
    gate = review_gate(conn, root, op, got["data"], sha)
    if gate:
        raise proof.Refusal(["propose a lesson only from an accepted report: " + gate[0]])
    u = lesson_line(got["text"], n)
    if u is None:
        raise proof.Refusal(["line %s is not the first line of an item in Lessons of %s" % (n, got["rel"])])
    value = _lesson_value(u["text"])
    if not value:
        raise proof.Refusal(["line %d holds only pointers" % n])
    items = _probe_items(probe)
    bad = [f for f, ok in (("--label", (label or "").strip()), ("--prescribes", (prescribes or "").strip()),
                           ("--governs", governs)) if not ok]
    if bad:
        raise proof.Refusal(["%s must not be empty" % f for f in bad])
    problems = [p for p in (_arg_problem("--label", label, 120), _arg_problem("--prescribes", prescribes, 300),
                            _arg_problem("--description", description or "", 600)) if p]
    problems += ["--governs tag %s must be 1-80 letters, digits or . _ : -" % _shown(g)
                 for g in governs if not _TOKEN.fullmatch(g)]
    if not problems:
        try:
            re.compile(prescribes, re.I)
        except (re.error, RecursionError, OverflowError, ValueError) as exc:
            problems.append("--prescribes is not a pattern Python can compile (%s)" % _shown(str(exc), 120))
    if problems:
        raise proof.Refusal(problems)
    from alpaca import lessons
    lesson_id = "L-aar-%s-%s" % (op, hashlib.sha256(value.encode("utf-8")).hexdigest()[:12])
    lesson = {"label": label, "value": value, "provenance": "%s:%d" % (got["rel"], n),
              "description": description or "a lesson from the after-action report of %s, line %d" % (op, n),
              "governs": governs, "prescribes": prescribes, "run_id": "%s:%d" % (op, n),
              "proposer": "aar", "lesson_id": lesson_id, "roots": [root]}
    rejected = None
    with db.transaction(conn):
        try:
            block = lessons.write(conn, lesson, items)
        except lessons.RejectedLesson as exc:
            rejected = exc
            _lesson_event(conn, session, op, n, sha, False, None, exc.control, exc.reason, len(items))
        else:
            _lesson_event(conn, session, op, n, sha, True, block["id"], None, None, len(items))
    if rejected is not None:
        raise rejected
    return {"block": block, "instances": len(items), "value": value,
            "provenance": lesson["provenance"], "rel": got["rel"]}


# --------------------------------------------------------------------------- CLI: alpaca aar
def _cmd_aar(args):
    from alpaca import cli
    root = cli._root()
    conn = db.connect(root)
    sid = args.session or "cli"
    verb = getattr(args, "aar_verb", None)
    if verb is None:
        print("usage: alpaca aar new|check|seal|review|accept|rework|lesson|owe|owed|waive <op>")
        return cli.USAGE
    if verb in ("owe", "owed", "waive"):
        return _cmd_owed(args, conn, root, sid)
    if verb in REVIEW_VERBS:
        return _cmd_review(args, conn, root, sid, verb)
    gate = "GATE alpaca-aar-%s" % verb
    try:
        if verb == "new":
            made = new(conn, root, args.op, force=args.force, session=sid)
            print("new   PASS  wrote %s (%d timeline row(s))" % (made["rel"], made["events"]))
            print("            next: fill every section, then alpaca aar check %s" % made["op"])
            if made["open"]:
                print("            note: %s is still open; the timeline stops at event:%s"
                      % (made["op"], made["through"]))
            return cli.PASS
        if verb == "check":
            res = check(conn, root, args.op)
            problems = res["problems"]
            if not problems:
                ev = res["sealed"]
                print("%s: PASS (%s, %d action item(s), %s)" % (
                    gate, res["rel"], res["actions"],
                    "sealed at event:%d" % ev["id"] if ev else "not sealed"))
                return cli.PASS
        else:
            from alpaca import aaritems
            out = seal(conn, root, args.op, session=sid, into=args.into)
            print("%s: PASS (%s sealed, sha256 %s)" % (gate, out["path"], out["sha256"][:12]))
            for line in aaritems.seal_lines(out.get("filed") or {}):
                print("            " + line)
            return cli.PASS
    except proof.Refusal as exc:
        problems = exc.problems
        if verb == "new":
            print("%s: FAIL (%s)" % (gate, "; ".join(problems)))
            return cli.FAIL
    print("%s: FAIL (%d problem(s))" % (gate, len(problems)))
    for p in problems:
        print("  - %s" % _plain(p))       # a problem may quote report or record text
    return cli.FAIL


def _parser(sub):
    a = sub.add_parser("aar", help="the after-action report of an op: new, check, review, accept, rework, seal, lesson, owe, owed, waive")
    av = a.add_subparsers(dest="aar_verb")
    n = av.add_parser("new", help="write the draft with the op's timeline filled from the record")
    n.add_argument("op")
    n.add_argument("--force", action="store_true",
                   help="overwrite an unsealed draft (a sealed report is never overwritten)")
    c = av.add_parser("check", help="name every part that is missing, empty or has no pointer, "
                                    "and a change since the seal")
    c.add_argument("op")
    s = av.add_parser("seal", help="check the report and its accepted review, then record its hash on the record")
    s.add_argument("op")
    s.add_argument("--into", help="the open op the new action items go into (default: the standing "
                                  "After-action items op, opened at the first seal that needs it; when its plan is "
                                  "full the seal refuses and asks for --into)")
    _owed_parser(av)
    _review_parser(av)


def _review_parser(av):
    r = av.add_parser("review", help="write the review packet a fresh subagent answers")
    r.add_argument("op")
    r.add_argument("--reviewer", required=True,
                   help="the id the reviewer subagent signs its review with: 1-80 letters, digits or . _ : -, "
                        "different from your session")
    a = av.add_parser("accept", help="record that the review passed every check (the main session only)")
    a.add_argument("op")
    a.add_argument("--review", required=True,
                   help="the reviewer's review file (JSON); a relative path is read from the folder "
                        "you run the command in")
    w = av.add_parser("rework", help="send the report back with a reason (the main session only)")
    w.add_argument("op")
    w.add_argument("--reason", required=True, help="what to fix, 4-2000 characters")
    ls = av.add_parser("lesson", help="propose one Lessons item to lessons.write, whose probe decides")
    ls.add_argument("op")
    ls.add_argument("--line", type=int, required=True, help="the file line where the Lessons item starts")
    ls.add_argument("--probe", required=True,
                    help="held-out instances as JSON lines, each exactly {id, tag, outcome PASS or FAIL, action}; "
                         "id and tag 1-80 letters, digits or . _ : -, action 1-2000 characters")
    ls.add_argument("--label", required=True, help="a short name for the lesson, at most 120 characters")
    ls.add_argument("--governs", required=True, help="the probe tags the lesson governs, comma separated")
    ls.add_argument("--prescribes", required=True,
                    help="the regex an action that follows the lesson matches, at most 300 characters")
    ls.add_argument("--description", help="one sentence, at most 600 characters; default names the report and the line")


def _cmd_review(args, conn, root, sid, verb):
    """review, accept, rework and lesson. None of them seals."""
    from alpaca import cli, lessons
    gate = "GATE alpaca-aar-%s" % verb
    try:
        try:
            if verb == "review":
                out = review(conn, root, args.op, reviewer=args.reviewer, session=sid)
                _say("%s: PASS (packet event:%d for %s, sha256 %s, reviewer %s)"
                     % (gate, out["event"], out["rel"], out["sha256"][:12], out["reviewer"]))
                _say("packet: %s" % out["packet"], " " * 12)
                _say("next: give the packet to a fresh subagent; then alpaca aar accept %s "
                     "--review <file> or alpaca aar rework %s --reason <why>" % (out["op"], out["op"]), " " * 12)
            elif verb == "accept":
                out = accept(conn, root, args.op, args.review, session=sid)
                _say("%s: PASS (accept at event:%d for sha256 %s; review kept at %s)"
                     % (gate, out["event"], out["sha256"][:12], out["rel"]))
                _say("next: alpaca aar seal %s" % args.op, " " * 12)
            elif verb == "rework":
                out = rework(conn, root, args.op, args.reason, session=sid)
                _say("%s: PASS (rework at event:%d for sha256 %s)" % (gate, out["event"], out["sha256"][:12]))
                _say("next: fix the report, alpaca aar check %s, then alpaca aar review %s again"
                     % (args.op, args.op), " " * 12)
            else:
                governs = [g.strip() for g in args.governs.split(",") if g.strip()]
                out = propose_lesson(conn, root, args.op, args.line, util.from_caller(args.probe),
                                     label=args.label, governs=governs, prescribes=args.prescribes,
                                     description=args.description, session=sid)
                _say("%s: PASS (admitted %s from %s:%d, %d probe instance(s))"
                     % (gate, out["block"]["id"], out["rel"], args.line, out["instances"]))
            return cli.PASS
        except proof.Refusal as exc:
            problems = exc.problems
        except lessons.RejectedLesson as exc:
            _say("%s: FAIL (lessons.write refused the lesson, control %s)" % (gate, exc.control or "-"))
            _say(exc.control or "-", "  - control: ")
            _say(exc.reason, "  - reason: ", 280)
            return cli.FAIL
        _say("%s: FAIL (%d problem(s))" % (gate, len(problems)))
        for p in problems:
            _say(p, "  - ")
        return cli.FAIL
    finally:
        conn.close()


def _register():
    from alpaca import cli
    cli.command("aar")(_cmd_aar)
    cli.register_parser("aar", _parser)


_register()
