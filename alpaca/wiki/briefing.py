"""Task and op briefings (design/rune2-memory/architecture.md 3.1, 6.2, 7.2 K-5; requirements.md
FR-19, FR-20; interfaces.py.txt `Briefing`).

A briefing is the cited state of one task or op. It is built on demand from the Rune2 store each
time it is asked for and never stored (EX-22): nothing here writes the store or the record, and no
generated text is kept anywhere. Its items come only from what the store already holds:

| label                   | what                         | read from                              |
| source                  | an event note, a contract    | the active blocks of a servable source |
| record-fact             | a fact a record rule wrote   | a current `deterministic` edge         |
| admitted                | an admitted fact or lesson   | a current `llm` edge, an admitted one  |
| agent-summary-candidate | a summary the agent proposed | a `checked` summary candidate (FR-33)  |

Every item cites its sources (`SourceRef`: the source_ref and quote an agent can copy into a
proposal). A source that fails the servable predicate (servable.py: withdrawn, private, quarantined,
outside the compartment), by any name the store gives it (`withdrawal.source_of`), gives no item: a
summary or admitted observation with any such anchor or context entry is dropped rather than
labelled (architecture.md 6.2); a fact is shown only while its anchor block passes, and a fact whose
object names such a source (a withdrawn proof, a withdrawn decision) is left out and counted in the
gaps, so no briefing states a withdrawn source as current (requirements.md HS-MM-04). An edge ties
one thing to another (a decision to a task by `sourced_from`, a task to an op by `in_op`) only while
the block it was read from passes too, as the answer path's graph walk follows one
(store/adjacency.py); otherwise it is neither followed nor counted. The contract item is the task's
own `contract:<task>` copy (FR-9), never a document that only says so in its header. An item whose
anchor no longer resolves in the current version of its source is labelled stale (`fresh` False);
shown record facts that disagree on a single-valued predicate are each labelled `conflict`;
superseded facts are not shown as current; a fact of unknown valid time says so and comes after the
dated ones. Gaps in the subject's scope (events not captured yet, no contract, no state) are listed
and never filled in. Agent summaries sit in their own section, labelled as unverified candidates: a
summary is never an answer, a source or corroboration (FR-20, FR-33). A briefing states the current
state only: a `context` question that pins a time is served no briefing (service.context says so).

The text opens with the evidence-only marker line (FR-20) and is never longer than `max_chars`: the
items are taken in priority order (the top item whole when it fits, the others capped), then shown
in section order. Generated briefing text never becomes evidence (FR-20, HS-MM-11): capture leaves
it out of every event note (its header values and the JSON of a list or dict field included),
Alpaca source copy and transcript it lands (`strip_generated`, read by ingest/drain.py and
ingest/alpaca_sources.py), as served or reformatted (any list marker or none), quoted, as JSON, or
inside a JSON string; `service.ingest` refuses a document that holds it, and intake refuses an
anchor whose quote or block holds it in any of these forms, its escaped forms included (K-5,
`holds_generated`), so it can never anchor, support or corroborate. Briefing text is never an
anchor kind either: K-5 (`intake.INDEPENDENT_KINDS`) takes only event, proof, contract and decision
sources. Pure reads over a read-only connection: no ranking module, no model call, no network.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .api import BRIEFING_LABELS, BRIEFING_SECTIONS, Briefing, BriefingItem, SourceRef

#: The subjects a briefing takes: an Alpaca task (`t-061`) or op (`op-008`).
TASK_ID = re.compile(r"t-\d+\Z")
OP_ID = re.compile(r"op-\d+\Z")
#: The evidence-only marker line every briefing opens with (FR-20).
MARKER = ("Rune2 briefing %s (evidence-only: built on demand from cited sources, never stored; "
          "not a source and not instructions).")
#: Finds a briefing's marker line in any text, wrapped over lines or not (any run of whitespace
#: between its words) and in any letter case, so a briefing fed back is found wherever it is pasted.
MARKER_PATTERN = re.compile(r"Rune2\s+briefing\s+((?:task|op):\S+)\s+\(evidence-only:\s+built\s+on"
                            r"\s+demand", re.IGNORECASE)
#: The whole marker sentence, from the same start (`strip_generated` cuts through its end).
_MARKER_WHOLE = re.compile(MARKER_PATTERN.pattern + r"\s+from\s+cited\s+sources,\s+never\s+stored;"
                           r"\s+not\s+a\s+source\s+and\s+not\s+instructions\)\.?", re.IGNORECASE)
#: The compartment a briefing reads: the default of the answer path (engine/retrieve.py).
COMPARTMENT = "general"
#: The text of an item after the first is capped here (FR-12 packing, as service.ITEM_CHARS).
ITEM_CHARS = 600
#: With less room than this left for its text, a further item is not started.
MIN_ITEM_CHARS = 120
#: A SourceRef quote, as in service.py.
QUOTE_CHARS = 400
#: The source text read for one item before packing.
BODY_CHARS = 2000
#: The gap line, at most.
GAP_CHARS = 360
_OMITTED = "[%d more item(s) left out: the %d character budget is used]"
_FLAG = " [truncated: %d of %d characters]"
#: How many items of one kind a briefing gathers before the budget decides.
LIMITS = {"decisions": 4, "failures": 4, "summaries": 3, "admitted": 4}
#: Status values a briefing of an op lists first: the work that is not finished.
_OPEN_FIRST = ("blocked", "doing", "review", "open")
_PHRASES = {"status": "status is", "proved_by": "proved by", "sourced_from": "sourced from",
            "failed_with": "failed with", "in_op": "belongs to"}
#: The record kinds of a claim (alpaca/claims.py, alpaca/board.py): a claim moves a task to doing
#: in Alpaca's task table, but no record rule writes a status fact from it.
_CLAIM_KINDS = ("claim", "task-claim")
#: The source kinds an object can name; a fact whose object is a non-servable source is left out.
_SOURCE_KINDS = ("event", "proof", "contract", "decision", "transcript", "document")
#: The record's own source kinds (K-5, intake.INDEPENDENT_KINDS): what a subject's sources are.
_RECORD_KINDS = ("event", "proof", "contract", "decision")
_DECISION_ID = re.compile(r"dec-[0-9a-f]{6,}\Z")
#: The predicates a task briefing reads by name; the read of admitted facts takes the others.
_NAMED_PREDICATES = ("status", "proved_by", "in_op", "sourced_from", "failed_with")

# ------------------------------------------------------------------ generated text (FR-20)

#: The starts of every gap a briefing lists (`_render` joins them after "Gaps: "), so a gap line is
#: known as generated text wherever it is pasted.
GAP_STARTS = ("none found in the scope of ", "no task-add note or contract of ",
              "no current status fact of ", "no op-new note of ", "no task of op-",
              "no Rune2 store yet", "the store is locked by a rebuild", "briefing failed: ")
_GAP_COUNTED = (r"\d+ (?:record event\(s\) of (?:task|op) |transcript\(s\) in scope unavailable|"
                r"record fact\(s\) of )")
_GAP_HEAD = r"Gaps:[ \t]+(?:%s|%s)" % ("|".join(re.escape(g) for g in GAP_STARTS), _GAP_COUNTED)
#: An item's head without its list marker, `<section> [<label>(, stale)(, conflict)]:`. Its section
#: and label words make it a briefing's whatever comes before it: the `- ` it is served with,
#: another bullet, a number, a table pipe, the quote mark of a JSON string, or nothing.
_ITEM_HEAD = (r"(?:%s)[ \t]+\[(?:%s)(?:,[ \t]*stale)?(?:,[ \t]*conflict)?\]:"
              % ("|".join(re.escape(x) for x in BRIEFING_SECTIONS),
                 "|".join(re.escape(x) for x in BRIEFING_LABELS)))
_OMITTED_HEAD = r"\[\d+ more item\(s\) left out: the \d+ character budget is used\]"
#: What may come before a pasted line: one letter that numbers it (`a.`, `b)`), then any run of
#: characters that are not letters: indentation, a quote mark (`> `), a list marker (`-`, `*`, `+`,
#: a bullet), a number (`1.`, `2)`), a table pipe, or the quote mark or bracket that opens a JSON or
#: Python string or list.
_PREFIX = r"(?:[A-Za-z][.)])?(?:[^\w\n]|\d)*"
#: A line of a briefing other than its marker line: an item, the left-out count or the gap line.
_LINE_HEAD = re.compile(_PREFIX + r"(?:%s|%s|%s)" % (_ITEM_HEAD, _OMITTED_HEAD, _GAP_HEAD),
                        re.IGNORECASE)
_GAP_LINE = re.compile(_PREFIX + _GAP_HEAD, re.IGNORECASE)
#: The same heads anywhere in a text (an event note renders a value on one line; a JSON string
#: holds an item line after a quote mark or an escaped line break): an item head that no letter
#: runs into, the left-out count, the gap line.
_INLINE = re.compile(r"(?<![^\W\d])(?:%s)|%s|%s" % (_ITEM_HEAD, _OMITTED_HEAD, _GAP_HEAD),
                     re.IGNORECASE)
#: A briefing in its JSON form (`wiki briefing --json`): its marker key, and an item's label key.
_JSON_MARKER = re.compile(r'"marker"\s*:\s*"Rune2\s+briefing', re.IGNORECASE)
_JSON_LABEL = re.compile(r'"label"\s*:\s*"(?:%s)"' % "|".join(re.escape(x) for x in BRIEFING_LABELS),
                         re.IGNORECASE)
_JSON_SECTION = re.compile(r'"section"\s*:\s*"(?:%s)"'
                           % "|".join(re.escape(x) for x in BRIEFING_SECTIONS), re.IGNORECASE)
#: One pass that tells whether a text can hold any of the above (most captured text cannot); a
#: label key inside a JSON string reads `"label\"`.
_QUICK = re.compile(r'briefing|gaps:|left out: the|\]:|"label\\?"', re.IGNORECASE)
_QUICK_LINES = re.compile(r"gaps:|left out: the|\]:", re.IGNORECASE)
#: A JSON string on one line: from its quote mark through the closing one, or to the end of the
#: line when it is not closed. The match never fails once started, so the text is read once, left
#: to right, however many escapes it holds.
_JSON_STRING = re.compile(r'"(?:[^"\\\n]|\\.)*"?')
#: A backslash escape of a JSON string or a Python literal: a code point, or one character (an
#: escaped line break, tab, quote mark or backslash).
_ESCAPE = re.compile(r"\\(?:u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2})|(.))", re.DOTALL)
_ESCAPED = {"n": "\n", "r": "\r", "t": "\t", "f": "\f", "v": "\v", "b": " ", "a": " ", "0": " "}
#: How many levels of escaping `holds_generated` reads through (a JSON string inside a JSON string
#: inside a third).
_ESCAPE_LEVELS = 3
#: How many opening braces before a JSON marker key are tried as the start of its object.
_JSON_TRIES = 8
#: The subject a cut names in its line: a task or op id, nothing longer.
_SUBJECT = re.compile(r"(?:task|op):[A-Za-z0-9._-]{1,40}", re.IGNORECASE)
_LEFT_OUT = "[Rune2 briefing %s left out at capture: generated text is not a source]"
_LINES_LEFT_OUT = "[Rune2 briefing lines left out at capture: generated text is not a source]"


def holds_generated(text) -> bool:
    """True when `text` holds generated briefing text: a marker line (wrapped or not, in any case),
    an item head (after any list marker, number, table pipe or quote mark, or none), the left-out
    count or a gap line, at a line start or inside other text, or an item of the JSON form. The
    text is read as it stands and through up to `_ESCAPE_LEVELS` levels of backslash escapes, so an
    item line inside a JSON string (after `\\n`, as `\\u2022`, inside a string inside a string) is
    found too. Intake refuses an anchor whose quote or block holds it (K-5) and `service.ingest` a
    document that holds it."""
    if not text:
        return False
    view = str(text)
    for _ in range(_ESCAPE_LEVELS + 1):
        if _holds(view):
            return True
        if "\\" not in view:
            return False
        view, before = _unescaped(view), view
        if view == before:
            return False
    return False


def _holds(text: str) -> bool:
    return bool(MARKER_PATTERN.search(text) or _INLINE.search(text)
                or (_JSON_LABEL.search(text) and _JSON_SECTION.search(text)))


def _unescaped(text: str) -> str:
    """`text` with one level of backslash escapes read (a JSON string, a Python literal): the view in
    which an item line inside a JSON string starts a line again."""
    def one(found):
        if found.group(1):
            return chr(int(found.group(1), 16))
        if found.group(2):
            return chr(int(found.group(2), 16))
        return _ESCAPED.get(found.group(3), found.group(3))
    return _ESCAPE.sub(one, text)


def strip_generated(text):
    """`text` with every briefing pasted into it left out, as capture lands it (FR-20: generated
    briefings are never absorbed as source documents): a briefing's JSON object, or an item object
    of that form (`_json_spans`); a briefing inside a JSON string, as the string's value reads, the
    string kept with what is left of it, so the JSON around it stays whole (`_cut_strings`: the JSON
    capture renders for a list or dict field, a briefing line given as a list element); from a
    marker line (wrapped or not, anywhere on its line) to the end of that line, and on through its
    gap line when one comes before the next blank line; and any other run of briefing lines (an
    item under any list marker or none, the left-out count, a gap line). Each cut is replaced by one
    line that says so. Text with no briefing comes back unchanged, storable text stays so; pure."""
    if not text or not isinstance(text, str) or not _QUICK.search(text):
        return text
    text = _cut_spans(text, _json_spans(text))
    text = _cut_strings(text)
    text = _cut_spans(text, _marker_spans(text))
    return _strip_lines(text)


def _cut_spans(text: str, spans: list) -> str:
    """`text` with each span (start, end, subject) replaced by the line that says it was left out;
    a span that starts inside an earlier one is part of it."""
    if not spans:
        return text
    out, pos = [], 0
    for start, end, subject in sorted(spans):
        if start < pos:
            continue
        out.append(text[pos:start])
        if subject is None:
            out.append(_LINES_LEFT_OUT)
        else:
            named = _SUBJECT.match(subject)
            out.append(_LEFT_OUT % (named.group(0) if named else "?"))
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _cut_strings(text: str) -> str:
    """Each JSON string in `text` whose value holds a briefing (an item line at its start or after an
    escaped line break, a marker line, a briefing's JSON) replaced by the JSON string of what
    `strip_generated` makes of that value (`_json_string`). A string that is not closed or does not
    decode is kept as it stands, and the line rules and intake (`holds_generated`) still see it."""
    if '"' not in text:
        return text

    def one(found):
        literal = found.group(0)
        if len(literal) < 2 or not literal.endswith('"') or not _QUICK.search(literal):
            return literal
        try:
            value = json.loads(literal)
        except (ValueError, RecursionError):
            return literal
        if not isinstance(value, str):
            return literal
        cut = strip_generated(value)
        return literal if cut == value else _json_string(cut, literal)
    return _JSON_STRING.sub(one, text)


def _json_spans(text: str) -> list:
    """(start, end, subject) of each briefing in its JSON form (`wiki briefing --json`): the whole
    JSON object whose `marker` key holds the marker line, and any item object of that form (its
    `section` and `label` keys a briefing's own) that stands outside one, each found by decoding
    from the nearest `{` before its key (subject None for an item). An object that does not decode
    whole, or nests too deep to decode, is left to the line rules, and intake still refuses an
    anchor on it (`holds_generated`)."""
    spans = []
    decoder = json.JSONDecoder()
    keys = sorted([m.start() for m in _JSON_MARKER.finditer(text)]
                  + [m.start() for m in _JSON_LABEL.finditer(text)])
    for at in keys:
        if spans and spans[-1][0] <= at < spans[-1][1]:
            continue                                   # inside an object already cut
        brace = text.rfind("{", 0, at)
        for _ in range(_JSON_TRIES):
            if brace < 0:
                break
            try:
                obj, end = decoder.raw_decode(text, brace)
            except (ValueError, RecursionError):        # a value nested past the decoder's depth
                obj, end = None, -1
            if isinstance(obj, dict) and end > at and _briefing_json(obj):
                subject = str(obj.get("subject") or "?") if "marker" in obj else None
                spans.append((brace, end, subject))
                break
            brace = text.rfind("{", 0, brace)
    return spans


def _briefing_json(obj: dict) -> bool:
    """True for a briefing's JSON object (its marker key holds the marker line) or one of its item
    objects (a section and a label of a briefing)."""
    if "marker" in obj and MARKER_PATTERN.search(str(obj.get("marker") or "")):
        return True
    return (str(obj.get("section") or "").lower() in BRIEFING_SECTIONS
            and str(obj.get("label") or "").lower() in BRIEFING_LABELS)


def _marker_spans(text: str) -> list:
    """(start, end, subject) of each briefing that opens with a marker line in `text`; `end` stops
    before the line break that ends the briefing's last line."""
    spans = []
    for found in MARKER_PATTERN.finditer(text):
        whole = _MARKER_WHOLE.match(text, found.start())
        end = whole.end() if whole else found.end()
        line_end = text.find("\n", end)
        end = len(text) if line_end < 0 else line_end
        lines = []
        cursor = end
        while cursor < len(text):
            nxt = text.find("\n", cursor + 1)
            line = text[cursor + 1: len(text) if nxt < 0 else nxt]
            if not line.strip():
                break
            lines.append((line, len(text) if nxt < 0 else nxt))
            cursor = len(text) if nxt < 0 else nxt
        # through the gap line when one closes the briefing before the next blank line, however
        # its items were reformatted; else the marker line alone, and `_strip_lines` takes any
        # item, left-out or gap line that follows
        gap = next((n for n, (line, _) in enumerate(lines) if _GAP_LINE.match(line)), None)
        if gap is not None:
            end = lines[gap][1]
        spans.append((found.start(), end, found.group(1)))
    return spans


def _strip_lines(text: str) -> str:
    """Runs of briefing lines that no marker line opens, each replaced by one line."""
    if not _QUICK_LINES.search(text):
        return text
    parts = text.split("\n")
    out, cut = [], False
    for line in parts:
        if _LINE_HEAD.match(line):
            if not cut:
                out.append(_LINES_LEFT_OUT)
            cut = True
            continue
        cut = False
        out.append(line)
    return "\n".join(out)


def subject_of(*, task=None, op=None) -> tuple[str, str]:
    """('task', id) or ('op', id). ValueError unless exactly one well-formed id is given."""
    if (task is None) == (op is None):
        raise ValueError("a briefing names exactly one task (t-NNN) or one op (op-NNN)")
    value, pattern, kind = (task, TASK_ID, "task") if task is not None else (op, OP_ID, "op")
    if not isinstance(value, str) or not pattern.match(value.strip()):
        raise ValueError("%s must look like %s" % (kind, "t-NNN" if kind == "task" else "op-NNN"))
    return kind, value.strip()


def marker(kind: str, ident: str) -> str:
    return MARKER % ("%s:%s" % (kind, ident))


@dataclass
class _Item:
    section: str
    label: str
    text: str
    sources: list
    rank: int                    # packing priority, lowest first
    order: tuple = ()            # deterministic tie-break
    fresh: bool = True
    conflict: bool = False
    extra: dict = field(default_factory=dict)


# ------------------------------------------------------------------ store reads

def _conn(store):
    return getattr(store, "conn", store)


def _has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def _flat(text) -> str:
    return " ".join(str(text or "").split())


def _int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _order(*parts) -> tuple:
    """A sort key of strings (numbers zero-padded, nested tuples flattened), so items of different
    kinds in one rank compare, deterministically."""
    out: list = []
    for part in parts:
        if isinstance(part, tuple):
            out.extend(_order(*part))
        else:
            out.append("%020d" % (part + 10 ** 18) if isinstance(part, int) else str(part))
    return tuple(out)


def _header(text: str) -> dict:
    out = {}
    for line in (text or "").split("\n\n", 1)[0].splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            out[key.strip()] = value.strip()
    return out


def _header_rows(store, key: str, value: str) -> list:
    """The active header blocks (block 0) that hold the line `key: value`, whole line and exact
    value as store/filters.py matches it, whether or not they pass the servable predicate."""
    line = "\n%s: %s\n" % (key, " ".join(value.split()))
    return [dict(r) for r in _conn(store).execute(
        "SELECT h.block_id, h.doc_id, h.ordinal, h.block_content_id, h.text, h.source_created_at, "
        "d.path FROM blocks h JOIN docs d ON d.doc_id=h.doc_id WHERE h.ordinal=0 AND "
        "h.status='active' AND instr(char(10)||h.text||char(10), ?)>0 ORDER BY h.doc_id", (line,))]


def _docs_with(store, key: str, value: str) -> dict:
    """The documents of `_header_rows`, kept only when the header block passes the servable
    predicate: doc_id -> {doc_id, source_ref, header, block, path, event}."""
    from .servable import servable, source_refs_for_docs
    rows = _header_rows(store, key, value)
    keep = servable(store, [r["block_id"] for r in rows], compartment=COMPARTMENT)
    rows = [r for r in rows if r["block_id"] in keep]
    refs = source_refs_for_docs(store, [r["doc_id"] for r in rows])
    out = {}
    for r in rows:
        header = _header(r["text"])
        out[r["doc_id"]] = {"doc_id": r["doc_id"], "source_ref": refs.get(r["doc_id"]),
                            "header": header, "block": r, "path": r["path"],
                            "event": _int(header.get("event"))}
    return out


def _body(store, doc: dict) -> tuple:
    """(text, first block): the document after its header (active blocks from ordinal 1), each
    block after the last part of its heading (`Done bar: ...`), flattened and cut to BODY_CHARS,
    and the block a SourceRef quotes. A document without a body is quoted by its header block."""
    rows = [dict(r) for r in _conn(store).execute(
        "SELECT block_id, doc_id, ordinal, block_content_id, text, source_created_at, heading_path "
        "FROM blocks WHERE doc_id=? AND ordinal>=1 AND status='active' ORDER BY ordinal",
        (doc["doc_id"],))]
    if not rows:
        return _flat(doc["block"]["text"])[:BODY_CHARS], doc["block"]
    parts = []
    for r in rows:
        leaf = (r.get("heading_path") or "").split(" / ")[-1].strip()
        parts.append("%s: %s" % (leaf, r["text"]) if leaf else r["text"])
    return _flat(" ".join(parts))[:BODY_CHARS], rows[0]


def _source(doc: dict, block: dict, quote: str | None = None) -> SourceRef:
    """The SourceRef of a block: the cited span is `quote` when given (an edge's anchor quote, a
    candidate's anchor), else the start of the block, both exact substrings of it."""
    text = quote if quote is not None else (block.get("text") or "").strip()
    header = doc.get("header") or {}
    return SourceRef(source_ref=doc["source_ref"], quote=text[:QUOTE_CHARS],
                     source_block_id=block.get("block_id") or "",
                     block_content_id=block.get("block_content_id") or "",
                     doc_id=doc["doc_id"], path=doc.get("path") or "",
                     source_time=block.get("source_created_at"),
                     operator=header.get("operator") or None, session=header.get("session") or None)


def _doc_of_block(store, block_id: str) -> tuple:
    """(doc, block) for an edge's anchor block, or (None, None) when it fails the servable predicate
    or is gone. `doc` has the shape `_docs_with` returns."""
    from .servable import block_servable, source_refs_for_docs
    conn = _conn(store)
    if not block_id or not block_servable(store, block_id, compartment=COMPARTMENT):
        return None, None
    row = conn.execute(
        "SELECT b.block_id, b.doc_id, b.ordinal, b.block_content_id, b.text, b.source_created_at, "
        "b.status, d.path FROM blocks b JOIN docs d ON d.doc_id=b.doc_id WHERE b.block_id=?",
        (block_id,)).fetchone()
    if row is None:
        return None, None
    row = dict(row)
    head = conn.execute("SELECT text FROM blocks WHERE doc_id=? AND ordinal=0 ORDER BY "
                        "status='active' DESC LIMIT 1", (row["doc_id"],)).fetchone()
    header = _header(head[0] if head else "")
    doc = {"doc_id": row["doc_id"], "source_ref": source_refs_for_docs(store, [row["doc_id"]]).get(
        row["doc_id"]), "header": header, "block": row, "path": row["path"],
        "event": _int(header.get("event"))}
    return doc, row


def _named_servable(store, ref: str) -> bool:
    """`servable_ref` of `ref` and of the source the store names it by (`withdrawal.source_of`: a
    `document:<doc_id>` ref of a captured note is that note's `event:N`), so a source withdrawn or
    private under one of its names is so under all of them (t-067 alias matrix)."""
    from .servable import servable_ref
    from .withdrawal import source_of
    return servable_ref(store, ref) and servable_ref(store, source_of(store, ref))


def _object_source(edge: dict) -> str | None:
    """The source a fact's object names, as a source_ref: a decision (`sourced_from`, `dec-...`) or
    a ref of a source kind (`proof:<path>` for `proved_by`); None for any other object."""
    obj = (edge.get("obj_node") or edge.get("obj_literal") or "").strip()
    if not obj:
        return None
    if _DECISION_ID.match(obj):
        return "decision:" + obj
    kind, sep, rest = obj.partition(":")
    return obj if sep and rest and kind in _SOURCE_KINDS else None


def _current_edges(store, where: str, args: dict) -> list:
    """The edges current at latest knowledge (store/asof.py, the one currency clause plus shelf
    life) that meet `where`, oldest valid time first, those of unknown time last (`_by_time`)."""
    from .clock import now_iso
    from .store.asof import AsOf, asof_where_edges
    clause, params = asof_where_edges(AsOf(T=now_iso()), "e")
    return _by_time([dict(r) for r in _conn(store).execute(
        "SELECT e.* FROM edges e WHERE %s AND %s ORDER BY e.valid_from, e.edge_id"
        % (where, clause), dict(params, **args))])


def _single(store, predicate: str) -> bool:
    row = _conn(store).execute("SELECT cardinality FROM predicates WHERE predicate=?",
                               (predicate,)).fetchone()
    return bool(row) and row[0] == "single"


def _object(edge: dict) -> str:
    obj = edge.get("obj_node") or edge.get("obj_literal") or ""
    if edge.get("obj_node") and edge["predicate"] in ("status", "failed_with") and ":" in obj:
        obj = obj.split(":", 1)[1]
    return obj


def _fact_text(edge: dict) -> str:
    text = "%s %s %s" % (edge["subj_node"], _PHRASES.get(edge["predicate"],
                                                         edge["predicate"].replace("_", " ")),
                         _object(edge))
    if edge["predicate"] == "status" or not _dated(edge):
        text += _since(edge)
    return text


def _fact_items(store, edges: list, *, section: str, rank: int, label: str | None = None,
                body: bool = False, dropped: list | None = None) -> list:
    """One item per current edge whose anchor block passes the servable predicate and whose object,
    when it names a source (a proof, a decision), names one that passes it too: a fact that would
    state a withdrawn or private source as current is left out and added to `dropped` (HS-MM-04).
    A record rule's edge is a `record-fact`, an admitted agent fact (`llm`) is `admitted`. The anchor
    block must be active to be fresh; shown single-valued edges of one subject that disagree are
    each a conflict (a hidden fact flags nothing). With `body`, the anchor note's text follows the
    fact (what failed, for one)."""
    shown = []
    for e in edges:
        doc, block = _doc_of_block(store, e["source_block_id"])
        if doc is None:
            continue
        named = _object_source(e)
        if named is not None and not _named_servable(store, named):
            if dropped is not None:
                dropped.append(e["edge_id"])
            continue
        shown.append((e, doc, block))
    by_key: dict = {}
    for e, _, _ in shown:
        if _single(store, e["predicate"]):
            by_key.setdefault((e["subj_node"], e["predicate"]), set()).add(_object(e))
    out = []
    for e, doc, block in shown:
        text = _fact_text(e)
        if body:
            note, _ = _body(store, doc)
            if note:
                text = "%s: %s" % (text, note)
        kind = label or ("admitted" if e["extractor"] == "llm" else "record-fact")
        out.append(_Item(section, kind, text, [_source(doc, block, e["source_quote"])], rank,
                         order=_when(e),
                         fresh=block["status"] == "active",
                         conflict=len(by_key.get((e["subj_node"], e["predicate"]), ())) > 1,
                         extra={"edge": e}))
    return out


def _note_item(store, doc: dict, *, section: str, rank: int) -> _Item:
    """A `source` item of one note. A decision note leads with its id (its `id:` header line),
    which the note's text does not repeat."""
    text, block = _body(store, doc)
    ident = doc["header"].get("id") if doc["header"].get("kind") == "decision" else None
    return _Item(section, "source", "%s: %s" % (ident, text) if ident else text,
                 [_source(doc, block)], rank, order=_order(doc["event"] or 0, doc["doc_id"]))


# ------------------------------------------------------------------ candidates (FR-33)

def _candidate_items(store, *, kind: str, status: str, belongs, rank: int, limit: int) -> list:
    """Items for candidates of `kind` in `status` that belong to the subject (`belongs(row, refs)`),
    newest first. A candidate whose body was withheld at intake, any of whose anchors has no quote,
    or any of whose anchors or context entries names a source that now fails `servable_ref` under
    any of its names (`_named_servable`; the agent wrote it after reading that source), is dropped,
    not labelled (architecture.md 6.2): its text never reaches a briefing. It is fresh when every
    anchor still resolves in the current version of its source (K-1 and K-2 again, read-only)."""
    from .intake import resolve_anchor
    conn = _conn(store)
    if not _has_table(conn, "candidates"):
        return []
    out = []
    for row in conn.execute("SELECT * FROM candidates WHERE kind=? AND status=? "
                            "ORDER BY record_event_id DESC, candidate_id", (kind, status)):
        row = dict(row)
        if row["body_json"] is None:
            continue
        cited = [dict(a) for a in conn.execute(
            "SELECT * FROM candidate_anchors WHERE candidate_id=? AND ordinal>=0 ORDER BY ordinal",
            (row["candidate_id"],))]
        anchors = [a for a in cited if a["role"] == "anchor"]
        if (not anchors or any(a["quote"] is None for a in anchors)
                or any(not _named_servable(store, a["source_ref"]) for a in cited)):
            continue
        body = json.loads(row["body_json"])
        refs = [a["source_ref"] for a in anchors]
        if not belongs(body, refs):
            continue
        sources, fresh = [], True
        for a in anchors:
            block = resolve_anchor(store, a["source_ref"], a["quote"])
            fresh = fresh and block is not None
            doc = {"doc_id": (block or {}).get("doc_id") or a["doc_id"] or "",
                   "source_ref": a["source_ref"], "header": {}, "path": ""}
            sources.append(_source(doc, block or {"block_content_id": a["block_content_id"]},
                                   a["quote"]))
        text = _flat(body.get("text") or "")
        if kind == "summary":
            text = "proposed by %s, not verified as an answer: %s" % (row["proposer"], text)
        section = ("agent-summaries" if kind == "summary" else
                   "failures" if row["admission_effect"] == "lesson-admitted" else "state")
        label = "agent-summary-candidate" if kind == "summary" else "admitted"
        out.append(_Item(section, label, text, sources, rank,
                         order=_order(-row["record_event_id"], row["candidate_id"]), fresh=fresh))
        if len(out) >= limit:
            break
    return out


def _belongs(kind: str, ident: str, refs: set):
    """The membership rule of a candidate: its scope names the subject, or one of its anchors is a
    source of the subject."""
    def test(body: dict, anchor_refs: list) -> bool:
        scope = body.get("scope") or {}
        return scope.get(kind) == ident or any(r in refs for r in anchor_refs)
    return test


# ------------------------------------------------------------------ subjects

def _task_items(store, task: str) -> tuple:
    """(items, gaps) of a task briefing: the task-add note and the contract source, the current
    status and proof facts and the latest move (its reason; a move into `blocked` is a blocker),
    admitted facts and observations, the decisions the contract is sourced from or that name the
    task, failing verdicts and admitted lessons, and the agent summaries about the task."""
    docs = _docs_with(store, "task", task)
    notes = sorted(docs.values(), key=lambda d: (d["event"] or 0, d["doc_id"]))
    kinds = lambda *names: [d for d in notes if d["header"].get("kind") in names]  # noqa: E731
    items, gaps, dropped = [], [], []
    adds = [d for d in kinds("task-add") if (d["source_ref"] or "").startswith("event:")]
    if adds:
        items.append(_note_item(store, adds[-1], section="contract", rank=1))
    # the task's own contract copy (FR-9, `contract:<task>`), never a document whose header only
    # says `role: contract`: a page cannot stand in for the record's contract (one authority)
    contracts = [d for d in notes if d["header"].get("role") == "contract"
                 and d["source_ref"] == "contract:%s" % task]
    if contracts:
        items.append(_note_item(store, contracts[-1], section="contract", rank=5))
    if not adds and not contracts:
        gaps.append("no task-add note or contract of %s is captured" % task)
    status = _fact_items(store, _current_edges(
        store, "e.subj_node=:s AND e.predicate='status'", {"s": task}), section="state", rank=2,
        dropped=dropped)
    items.extend(status)
    if not status:
        # none shown: a status fact read from a withheld note is not a fact this briefing has
        gaps.append("no current status fact of %s" % task)
    # the latest event that changed the task's state: a move, or a claim (which no record rule
    # reads); a move into `blocked` is a blocker
    moves = [d for d in kinds("task-move", *_CLAIM_KINDS)
             if (d["source_ref"] or "").startswith("event:")]
    if moves:
        last = moves[-1]
        blocked = (last["header"].get("kind") == "task-move"
                   and last["header"].get("to") == "blocked")
        items.append(_note_item(store, last, section="blockers" if blocked else "state", rank=3))
    items.extend(_fact_items(store, _current_edges(
        store, "e.subj_node=:s AND e.predicate='proved_by'", {"s": task}), section="state", rank=4,
        dropped=dropped))
    # admitted facts of the predicates not read by name above and below, so none is listed twice
    named = ",".join("'%s'" % p for p in _NAMED_PREDICATES)
    items.extend(_fact_items(store, _latest(_current_edges(
        store, "e.subj_node=:s AND e.extractor='llm' AND e.predicate NOT IN (%s)" % named,
        {"s": task}), LIMITS["admitted"]), section="state", rank=4, dropped=dropped))
    items.extend(_fact_items(store, _current_edges(
        store, "e.subj_node=:s AND e.predicate='in_op'", {"s": task}), section="contract", rank=9,
        dropped=dropped))
    refs = {d["source_ref"] for d in notes
            if (d["source_ref"] or "").partition(":")[0] in _RECORD_KINDS}
    belongs = _belongs("task", task, refs)
    items.extend(_candidate_items(store, kind="summary", status="checked", belongs=belongs, rank=6,
                                  limit=LIMITS["summaries"]))
    items.extend(_candidate_items(store, kind="observation", status="admitted", belongs=belongs,
                                  rank=8, limit=LIMITS["admitted"]))
    items.extend(_decision_items(store, task, _current_edges(
        store, "e.subj_node=:s AND e.predicate='sourced_from'", {"s": task}), dropped))
    failing = _current_edges(store, "e.subj_node=:s AND e.predicate='failed_with'", {"s": task})
    items.extend(_fact_items(store, _latest(failing, LIMITS["failures"]), section="failures",
                             rank=8, body=True, dropped=dropped))
    if dropped:
        gaps.append(_dropped_gap("task", task, dropped))
    return items, gaps


def _dropped_gap(kind: str, ident: str, dropped: list) -> str:
    return ("%d record fact(s) of %s %s left out: each names a withdrawn or private source"
            % (len(set(dropped)), kind, ident))


def _withheld_decision(store, ident: str) -> bool:
    """True when the record's decision `ident` is captured as an event note and no note of it
    passes the servable predicate (withdrawn, private or quarantined)."""
    from .servable import servable, source_refs_for_docs
    rows = [r for r in _header_rows(store, "id", ident) if _header(r["text"]).get("kind") == "decision"]
    refs = source_refs_for_docs(store, [r["doc_id"] for r in rows])
    rows = [r for r in rows if (refs.get(r["doc_id"]) or "").startswith("event:")]
    return bool(rows) and not servable(store, [r["block_id"] for r in rows], compartment=COMPARTMENT)


def _decision_items(store, task: str, sourced: list, dropped: list) -> list:
    """The decisions a task's contract is sourced from (the decision note when it is captured and
    servable; the record fact only when no note of it is captured at all, never when its note or
    copy is withdrawn or private), then decision notes that name the task, newest first. A
    `sourced_from` edge links the two only while the block it was read from (the contract's note or
    copy) passes the servable predicate, as the answer path's graph walk follows an edge
    (store/adjacency.py): a decision that only a withdrawn, private or quarantined contract ties to
    the task is not listed, nor counted in the gaps."""
    from .servable import servable
    out, seen = [], set()
    grounded = servable(store, [e["source_block_id"] for e in sourced], compartment=COMPARTMENT)
    for edge in sourced:
        if edge["source_block_id"] not in grounded:
            continue
        ident = edge.get("obj_node") or ""
        notes = sorted(_docs_with(store, "id", ident).values(),
                       key=lambda d: (d["event"] or 0, d["doc_id"]))
        notes = [d for d in notes if d["header"].get("kind") == "decision"
                 and (d["source_ref"] or "").startswith("event:")]
        if notes:
            out.append(_note_item(store, notes[-1], section="decisions", rank=7))
            seen.add(notes[-1]["doc_id"])
        elif _withheld_decision(store, ident):
            dropped.append(edge["edge_id"])     # captured, but every note of it is withheld
        else:
            out.extend(_fact_items(store, [edge], section="decisions", rank=7, dropped=dropped))
    named = re.compile(r"(?<![\w-])%s(?![\w-])" % re.escape(task))
    notes = {d["doc_id"]: d for d in _docs_with(store, "kind", "decision").values()
             if d["doc_id"] not in seen and (d["source_ref"] or "").startswith("event:")}
    hits = {r[0] for r in _conn(store).execute(
        "SELECT DISTINCT doc_id FROM blocks WHERE doc_id IN (SELECT value FROM json_each(?)) "
        "AND ordinal>=1 AND status='active' AND instr(text, ?)>0",
        (json.dumps(sorted(notes)), task))}
    for doc in sorted((notes[d] for d in hits), key=lambda d: (-(d["event"] or 0), d["doc_id"])):
        if len(out) >= LIMITS["decisions"]:
            break
        text, _ = _body(store, doc)
        if named.search(text):
            out.append(_note_item(store, doc, section="decisions", rank=7))
    return out[:LIMITS["decisions"]]


def _op_items(store, op: str) -> tuple:
    """(items, gaps) of an op briefing: the op's opening notes, the current status of each of its
    tasks (unfinished work first), the latest move of each blocked task, the op's decisions and
    the failing verdicts of its tasks, newest first, and the summaries and admitted observations
    about the op."""
    docs = _docs_with(store, "op", op)
    notes = sorted(docs.values(), key=lambda d: (d["event"] or 0, d["doc_id"]))
    items, gaps, dropped = [], [], []
    opening = [d for d in notes if d["header"].get("kind") in ("op-new", "op-open")
               and (d["source_ref"] or "").startswith("event:")]
    for doc in opening[-2:]:
        items.append(_note_item(store, doc, section="contract", rank=1))
    # the op's tasks (a decision's in_op fact names the op too, and is not a task), each by an
    # in_op edge whose own block passes the servable predicate: a membership read only from a
    # withheld task-add note or contract lists no task
    from .servable import servable
    members = [e for e in _current_edges(store, "e.predicate='in_op' AND e.obj_node=:o", {"o": op})
               if TASK_ID.match(e["subj_node"])]
    grounded = servable(store, [e["source_block_id"] for e in members], compartment=COMPARTMENT)
    members = [e for e in members if e["source_block_id"] in grounded]
    tasks = sorted({e["subj_node"] for e in members}, key=_task_key)
    if not opening:
        gaps.append("no op-new note of %s is captured" % op)
    if not tasks:
        gaps.append("no task of %s has a current in_op fact" % op)
    among = {"tasks": json.dumps(tasks)}
    status = _current_edges(store, "e.predicate='status' AND e.subj_node IN "
                                   "(SELECT value FROM json_each(:tasks))", among)
    facts = _fact_items(store, status, section="state", rank=2, dropped=dropped)
    for item in facts:
        # unfinished work first; a finished task ranks below the op's decisions and failures
        value = _object(item.extra["edge"])
        unfinished = value in _OPEN_FIRST
        item.rank = 2 if unfinished else 8
        item.order = _order(_OPEN_FIRST.index(value) if unfinished else len(_OPEN_FIRST),
                            _task_key(item.extra["edge"]["subj_node"]))
    # a task with no status fact yet (added, or only claimed) is listed by its membership
    known = {i.extra["edge"]["subj_node"] for i in facts}
    first = {}
    for e in members:
        if e["subj_node"] not in known:
            first.setdefault(e["subj_node"], e)
    pending = _fact_items(store, list(first.values()), section="state", rank=2,
                          dropped=dropped)
    for item in pending:
        item.text += " (no status fact yet)"
        item.order = _order(len(_OPEN_FIRST), _task_key(item.extra["edge"]["subj_node"]))
    items.extend(facts + pending)
    blocked = {i.extra["edge"]["subj_node"] for i in facts if _object(i.extra["edge"]) == "blocked"}
    last_move: dict = {}
    for doc in notes:
        if doc["header"].get("kind") == "task-move" and doc["header"].get("task") in blocked \
                and (doc["source_ref"] or "").startswith("event:"):
            last_move[doc["header"]["task"]] = doc
    for task in sorted(last_move, key=_task_key):
        items.append(_note_item(store, last_move[task], section="blockers", rank=3))
    decisions = [d for d in notes if d["header"].get("kind") == "decision"
                 and (d["source_ref"] or "").startswith("event:")]
    for doc in decisions[::-1][:LIMITS["decisions"]]:
        item = _note_item(store, doc, section="decisions", rank=4)
        item.order = _order(-(doc["event"] or 0), doc["doc_id"])
        items.append(item)
    failing = _current_edges(store, "e.predicate='failed_with' AND e.subj_node IN "
                                    "(SELECT value FROM json_each(:tasks))", among)
    items.extend(_fact_items(store, _latest(failing, LIMITS["failures"]), section="failures",
                             rank=5, body=True, dropped=dropped))
    refs = {d["source_ref"] for d in notes
            if (d["source_ref"] or "").partition(":")[0] in _RECORD_KINDS}
    belongs = _belongs("op", op, refs)
    items.extend(_candidate_items(store, kind="summary", status="checked", belongs=belongs, rank=6,
                                  limit=LIMITS["summaries"]))
    items.extend(_candidate_items(store, kind="observation", status="admitted", belongs=belongs,
                                  rank=7, limit=LIMITS["admitted"]))
    if dropped:
        gaps.append(_dropped_gap("op", op, dropped))
    return items, gaps


def _task_key(task: str):
    number = _int(task.split("-", 1)[-1]) if "-" in task else None
    return (number if number is not None else 10 ** 9, task)


# ------------------------------------------------------------------ packing and text

def _coverage_gaps(kind: str, ident: str, coverage) -> list:
    if coverage is None:
        return []
    get = (coverage.get if isinstance(coverage, dict)
           else lambda name, default=None: getattr(coverage, name, default))
    out = []
    missing = list(get("missing_event_ids_in_scope", ()) or ())
    if missing:
        shown = ", ".join(str(n) for n in missing[:8])
        more = ", ..." if len(missing) > 8 else ""
        out.append("%d record event(s) of %s %s not captured yet (ids %s%s); run bin/alpaca wiki "
                   "recover" % (len(missing), kind, ident, shown, more))
    transcripts = list(get("missing_transcripts_in_scope", ()) or ())
    if transcripts:
        out.append("%d transcript(s) in scope unavailable" % len(transcripts))
    return out


def _line(item: _Item, text: str) -> str:
    flags = [item.label] + (["stale"] if not item.fresh else []) + (
        ["conflict"] if item.conflict else [])
    refs = ", ".join(dict.fromkeys(s.source_ref for s in item.sources))
    return "- %s [%s]: %s (%s)" % (item.section, ", ".join(flags), text, refs)


def _cut(text: str, space: int) -> str | None:
    """`text` whole when it fits in `space` characters, else cut with a flag that says so, or None
    when no text is left once the flag fits."""
    if len(text) <= space:
        return text
    keep = space - len(_FLAG % (space, len(text)))
    if keep < 1:
        return None
    return text[:keep] + _FLAG % (keep, len(text))


def _pack(items: list, room: int) -> tuple:
    """(chosen, left_out). Items are taken in priority order (rank, then order): the first is kept
    whole when it fits and cut with a flag otherwise; the text of every other item is capped at
    ITEM_CHARS; an item that does not fit is cut while MIN_ITEM_CHARS of text fit, else left out
    and counted, and a later, shorter item may still fit. The lines chosen, newlines included,
    fit in `room`."""
    chosen, left_out = [], 0
    for item in sorted(items, key=lambda i: (i.rank, i.order)):
        frame = len(_line(item, "")) + 1
        cap = len(item.text) if not chosen else min(len(item.text), ITEM_CHARS)
        limit = min(cap, room - frame)
        shown = _cut(item.text, limit) if limit >= min(len(item.text), MIN_ITEM_CHARS) else None
        if shown is None:
            left_out += 1
            continue
        room -= frame + len(shown)
        chosen.append((item, shown))
    return chosen, left_out


def build(store, kind: str, ident: str, *, max_chars: int = 2500, coverage=None) -> Briefing:
    """The briefing of task or op `ident` from a read-only store connection (or an object with
    `.conn`). `coverage` (an api.Coverage or its dict) gives the uncaptured events and transcripts
    in the subject's scope, listed as gaps. The text fits in `max_chars`."""
    if kind not in ("task", "op"):
        raise ValueError("a briefing subject is a task or an op")
    if not hasattr(store, "conn"):
        # servable.py keeps its unservable set on an object with `.conn` for as long as the store
        # does not change, so the many checks of one briefing compute it once
        from types import SimpleNamespace
        store = SimpleNamespace(conn=store)
    head = marker(kind, ident)
    items, gaps = _task_items(store, ident) if kind == "task" else _op_items(store, ident)
    gaps = _coverage_gaps(kind, ident, coverage) + gaps
    return _render(kind, ident, head, items, gaps, max_chars)


def unavailable(kind: str, ident: str, reason: str, max_chars: int = 2500) -> Briefing:
    """A briefing with no item, when the store cannot be read: the reason is its one gap."""
    return _render(kind, ident, marker(kind, ident), [], [reason], max_chars)


def _render(kind: str, ident: str, head: str, items: list, gaps: list, max_chars: int) -> Briefing:
    gap_line = "Gaps: " + ("; ".join(gaps) if gaps
                           else "none found in the scope of %s %s" % (kind, ident))
    reserve = len(_OMITTED % (len(items), max_chars)) + 1 if items else 0
    # the gap line is cut with "..." at GAP_CHARS, or sooner when the budget leaves less room after
    # the marker line (and the left-out line), so a cut never goes unflagged
    cap = min(GAP_CHARS, max_chars - len(head) - 1 - reserve)
    gap_line = gap_line if len(gap_line) <= cap else gap_line[:max(cap - 3, 0)] + "..."
    room = max_chars - len(head) - 1 - len(gap_line) - 1 - reserve
    chosen, left_out = _pack(items, room) if room > 0 else ([], len(items))
    shown = sorted(chosen, key=lambda pair: (BRIEFING_SECTIONS.index(pair[0].section),
                                             pair[0].rank, pair[0].order))
    lines = [head] + [_line(item, text) for item, text in shown]
    if left_out:
        lines.append(_OMITTED % (left_out, max_chars))
    lines.append(gap_line)
    text = "\n".join(lines)[:max_chars]
    out_items = tuple(BriefingItem(section=item.section, text=shown_text,
                                   sources=tuple(item.sources), fresh=item.fresh,
                                   conflict=item.conflict, label=item.label)
                      for item, shown_text in shown)
    truncated = bool(left_out) or any(t != item.text for item, t in shown)
    return Briefing(subject="%s:%s" % (kind, ident), marker=head, items=out_items,
                    gaps=tuple(gaps), truncated=truncated, text=text, max_chars=max_chars)


#: A UTF-16 surrogate code point. A JSON string can spell one alone with an escape (`\ud83d`, the
#: first half of an emoji a truncated output cut off); decoded, it is no character UTF-8 encodes.
_SURROGATE = re.compile(r"[\ud800-\udfff]")


def _json_string(value: str, like: str) -> str:
    """`value` written back as a JSON string in the style of `like`, the string `_cut_strings` cut
    it from: non-ASCII characters escaped when `like` holds none, kept as they are otherwise. A
    surrogate code point is escaped either way (in storable text it can only come from an escape),
    so the JSON value is the same and the text stays storable. Written raw, it made the store write
    of the note raise UnicodeEncodeError, which stopped the capture batch, the Stop hook's capture
    and `wiki recover` (check round 3, minor m-inv-6)."""
    text = json.dumps(value, ensure_ascii=like.isascii())
    return _SURROGATE.sub(lambda found: "\\u%04x" % ord(found.group(0)), text)


# ------------------------------------------------------------------ valid time (check round 4)

#: Where an item of unknown valid time sorts in its rank: after every other item, whose order
#: begins with an ISO valid time (a fact) or a zero-padded number (a note, a candidate).
_UNDATED = "~"


def _dated(edge: dict) -> bool:
    """Whether a fact has a known valid time (`reconcile.time_known`, OB-05). An admitted agent fact
    (extractor `llm`) whose anchor block has no source time has none: its `valid_from` is only the
    capture time of that block (architecture.md 5.1; a note captured before t-065 has no source
    time), so a briefing never states it as a time and never orders history by it (requirements.md
    FR-5: an unknown time stays null)."""
    from .ingest.reconcile import time_known
    return time_known(edge)


def _since(edge: dict) -> str:
    """The time label of a fact: since when a dated status holds; for a fact of unknown valid time,
    whatever its predicate, that its valid time is unknown."""
    return " (since %s)" % edge["valid_from"] if _dated(edge) else " (valid time unknown)"


def _by_time(edges: list) -> list:
    """`edges`, oldest valid time first, with those of unknown valid time moved after the dated
    ones, in the order they were recorded (admitted), then by edge id: their proxy `valid_from`
    places none of them among the dated ones."""
    undated = sorted((e for e in edges if not _dated(e)),
                     key=lambda e: (e["recorded_at"], e["edge_id"]))
    return [e for e in edges if _dated(e)] + undated


def _when(edge: dict) -> tuple:
    """The order of a fact's item in its rank: a dated fact by its valid time, one of unknown valid
    time after every other item (`_UNDATED`), by the time it was recorded."""
    if _dated(edge):
        return _order(edge["valid_from"], edge["edge_id"])
    return _order(_UNDATED, edge["recorded_at"], edge["edge_id"])


def _latest(edges: list, limit: int) -> list:
    """The `limit` latest of `edges` (ordered by `_by_time`): the latest dated ones, then, for a
    place no dated fact takes, those of unknown valid time last recorded, which cannot be shown to
    be later than any dated one. The order of `edges` is kept."""
    dated = [e for e in edges if _dated(e)][-limit:] if limit > 0 else []
    room = limit - len(dated)
    return dated + ([e for e in edges if not _dated(e)][-room:] if room > 0 else [])


__all__ = ["build", "unavailable", "subject_of", "marker", "MARKER_PATTERN", "TASK_ID", "OP_ID",
           "holds_generated", "strip_generated", "GAP_STARTS"]
