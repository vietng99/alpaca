"""ingest.record_extract - the record rules (design/rune2-memory/architecture.md 8.1; FR-14, FR-34).

Model-free extraction of typed facts from the structured fields of a captured event note. The
renderer (ingest/drain.py `_note_header`) writes one `label: value` line per field the rules read, so a
rule reads header lines and nothing else. Each output is one edge: subject, predicate, object (a node
or a literal), `valid_from` = the event `ts`, and a quote that is the smallest header span holding its
subject, object and `ts:` lines (from `op:` for `in_op`).

The runtime check (Z10) keeps an output honest: every value the rule read must sit on its own labelled
line, whole, inside the quote, and the object must be made of what the object line says. A pass goes
to `encode_candidate`. A failure is its own quarantined `upsert_edge` row that names its rule and
carries its claim on that row only (OB-16); it never goes through `encode_candidate`, whose `matches`
and `duplicate` branches would corroborate another edge, and an output whose edge id already names an
edge writes nothing and is counted as failed.

Every output writes a support claim `rule:<rule_id>@<event>` (`edge_origin`) on the edge that holds it,
whether the verdict placed the edge or only supported it. A re-rendered note re-anchors its live
claims to the new block before the stale blocks retire (OB-17), so a record fact survives a change of
format. A document marked private emits nothing. `extract` and `check` are pure (the dry run uses
them); `apply` writes through the one write door.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from ..clock import to_clock_format
from ..determinism import edge_id as make_edge_id, normalize_text, sha256_hex
from .encode import encode_candidate, obj_key
from ..store.textnorm import norm_surface

#: The rule set version. It is part of every rule id, so a changed rule names itself in the claims.
VERSION = 1
EXTRACTOR_VERSION = "record-rules/%d" % VERSION
#: The first five header lines of an event note, in order (ingest/drain.py `_note_header`).
_HEAD = ("op", "role", "key", "kind", "event")
_LABEL = re.compile(r"[a-z][a-z0-9_-]*")
#: Verdict and run outcomes that count as a failure for `failed_with`. A PASS, and a pause for an
#: owner decision, are not failures.
FAILING = frozenset({"FAIL", "BLOCKED"})


# ------------------------------------------------------------------ rule table
def _exact(value: str):
    """An exact identifier is its own node id. Empty, `unassigned` and ids holding `#` (the mark of a
    passage id in the PPR graph) make no node."""
    value = value.strip()
    if not value or value == "unassigned" or "#" in value:
        return None
    return value, value


def _op(value: str):
    return _exact(value)


def _status(value: str):
    made = _exact(value)
    return None if made is None else ("status:" + made[0], made[0])


def _decision(value: str):
    return _exact(value) if re.fullmatch(r"dec-[0-9a-f]{6,}", value.strip()) else None


def _proof(value: str):
    """A proof is one node whether a task names it (`local:<path>`) or a proof report does (`<path>`)."""
    made = _exact(value)
    if made is None:
        return None
    ref = made[0]
    if ref.startswith("local:"):
        return "proof:" + ref[len("local:"):], ref
    if re.match(r"[a-z]+:", ref):
        return ref, ref
    return "proof:" + ref, ref


def _error_class(value: str):
    """The leading class name of an error line, `TranscriptUnavailable: ...` or a bare name."""
    match = re.match(r"([A-Za-z_][\w.]*)(?::|$)", value.strip())
    return None if match is None else ("error:" + match[1], match[1])


def _failing(value: str):
    return ("error:" + value.strip(), value.strip()) if value.strip() in FAILING else None


@dataclass(frozen=True)
class Node:
    """An endpoint read from one header line: the label, the node type, and `make(value)` giving
    `(node id, the token of the value the id is made of)` or None for no output."""
    label: str
    type: str
    make: Callable[[str], tuple[str, str] | None] = _exact


@dataclass(frozen=True)
class Literal:
    """An object that is the flattened value of one header line."""
    label: str


@dataclass(frozen=True)
class Rule:
    kinds: tuple
    predicate: str
    subject: Node
    obj: Node | Literal
    #: Extra header lines the rule reads: (label, test of the value). They sit inside the quote too.
    conditions: tuple = ()

    def rule_id(self, kind: str) -> str:
        return "%s/%s@%d" % (kind, self.predicate, VERSION)


RULES = (
    Rule(("task-add", "task-contract"), "in_op", Node("task", "task"), Node("op", "op", _op)),
    Rule(("task-contract",), "sourced_from", Node("task", "task"), Node("source", "decision", _decision)),
    Rule(("task-move",), "status", Node("task", "task"), Node("to", "status", _status)),
    Rule(("task-move",), "proved_by", Node("task", "task"), Node("proof", "proof", _proof)),
    Rule(("decision",), "in_op", Node("id", "decision"), Node("op", "op", _op)),
    Rule(("decision",), "choice", Node("id", "decision"), Literal("choice")),
    Rule(("proof-report",), "proves", Node("path", "proof", _proof), Node("task", "task")),
    Rule(("drain-failed",), "failed_with", Node("session", "session"),
         Node("error", "error-class", _error_class)),
    Rule(("verdict",), "failed_with", Node("task", "ref"), Node("verdict_name", "error-class", _failing)),
    Rule(("run",), "failed_with", Node("gate", "gate"), Node("verdict", "error-class", _failing)),
    Rule(("session-start",), "operator", Node("session", "session"), Literal("operator")),
    Rule(("msg",), "handed_off_to", Node("session", "session"), Node("to", "target"),
         conditions=(("data-kind", lambda value: value == "handoff"),)),
)


# ------------------------------------------------------------------ outputs
@dataclass(frozen=True)
class Output:
    """One rule output: an edge and the lines it was read from."""
    rule_id: str
    event_id: int
    kind: str
    predicate: str
    subj_node: str
    subj_type: str
    obj_node: str | None
    obj_literal: str | None
    obj_type: str
    valid_from: str
    quote: str
    #: (part, label, value): each labelled line the output read, part one of subject, object,
    #: condition, valid time. `check` looks for every one of them whole inside the quote.
    reads: tuple
    #: The text of the object line the object id is made of (the whole value for a literal).
    token: str

    @property
    def claim_ref(self) -> str:
        return "rule:%s@%d" % (self.rule_id, self.event_id)

    @property
    def source_ref(self) -> str:
        return "event:%d" % self.event_id

    @property
    def obj_datatype(self) -> str:
        return "node" if self.obj_node is not None else "string"


@dataclass(frozen=True)
class RuleOutput:
    """What the dry run returns (interfaces.py.txt `RuleOutput`). `edge` holds subj_node, predicate,
    obj, valid_from, source_created_at and source_quote; the quote, subject and object are None, with their sha256 beside them
    and `redacted` true, when the event's source fails `servable_ref` (a withdrawn or quarantined
    capture, architecture.md 6.2)."""
    rule_id: str
    event_id: int
    edge: dict
    fields_in_quote: bool
    missing: tuple
    servable: bool = True
    redacted: bool = False
    source_ref: str = ""


# ------------------------------------------------------------------ the pure part
@dataclass
class _Header:
    lines: list
    index: dict            # label -> line number, for a label that appears once
    repeated: set
    values: dict           # label -> value

    def line(self, label: str):
        """(line number, value) of a label that appears exactly once, else None."""
        if label in self.repeated or label not in self.index:
            return None
        return self.index[label], self.values[label]


def _read_header(text: str) -> _Header | None:
    """The first paragraph of an event note, or None when the text is not one: an event note opens
    with `op`, `role`, `key`, `kind` and `event`, in that order."""
    lines = (text or "").split("\n\n", 1)[0].split("\n")
    parsed, index, values, repeated = [], {}, {}, set()
    for number, raw in enumerate(lines):
        label, separator, value = raw.partition(": ")
        if not separator or not _LABEL.fullmatch(label):
            parsed.append(None)
            continue
        parsed.append(label)
        if label in index:
            repeated.add(label)
        else:
            index[label], values[label] = number, value
    if parsed[:len(_HEAD)] != list(_HEAD) or not values["event"].isdigit():
        return None
    return _Header(lines, index, repeated, values)


def is_event_note(text: str) -> bool:
    """Whether the text is a captured event note (the renderer's header, ingest/drain.py)."""
    return _read_header(text) is not None


def extract(text: str, rules=RULES) -> list[Output]:
    """The rule outputs of one note, in rule order. Pure: it reads the header and nothing else. A
    rule emits nothing when a line it reads is absent or repeated, when the value makes no node, or
    when `ts` is not a readable time; `unassigned` is never a value."""
    header = _read_header(text)
    if header is None:
        return []
    kind = header.values["kind"]
    outputs = []
    for rule in rules:
        if kind in rule.kinds:
            output = _output(rule, kind, header)
            if output is not None:
                outputs.append(output)
    return outputs


def _output(rule: Rule, kind: str, header: _Header) -> Output | None:
    subject = header.line(rule.subject.label)
    obj = header.line(rule.obj.label)
    moment = header.line("ts")
    if subject is None or obj is None or moment is None:
        return None
    valid_from = to_clock_format(moment[1])
    if valid_from is None:
        return None
    made_subject = rule.subject.make(subject[1])
    if made_subject is None:
        return None
    reads = [("subject", rule.subject.label, subject[1]), ("object", rule.obj.label, obj[1])]
    numbers = [subject[0], obj[0], moment[0]]
    if isinstance(rule.obj, Literal):
        if not obj[1]:
            return None
        obj_node, obj_literal, token, obj_type = None, obj[1], obj[1], "string"
    else:
        made = rule.obj.make(obj[1])
        if made is None:
            return None
        obj_node, obj_literal, token, obj_type = made[0], None, made[1], rule.obj.type
    for label, holds in rule.conditions:
        found = header.line(label)
        if found is None or not holds(found[1]):
            return None
        reads.append(("condition", label, found[1]))
        numbers.append(found[0])
    reads.append(("valid time", "ts", moment[1]))
    quote = "\n".join(header.lines[min(numbers):max(numbers) + 1])
    return Output(
        rule_id=rule.rule_id(kind), event_id=int(header.values["event"]), kind=kind,
        predicate=rule.predicate, subj_node=made_subject[0], subj_type=rule.subject.type,
        obj_node=obj_node, obj_literal=obj_literal, obj_type=obj_type, valid_from=valid_from,
        quote=quote, reads=tuple(reads), token=token)


def check(output: Output) -> tuple:
    """The runtime field-in-quote check (architecture.md 8.1, bar Z10): what the quote lacks, among
    subject, object, condition and valid time. A value is present only as a whole labelled line
    `<label>: <value>` of the quote, so a `from:` line never stands for a `to:` value. The object
    must also be made of its line's text, and `valid_from` must be the time the `ts:` line gives."""
    lines = set(output.quote.split("\n"))
    missing = []
    for part, label, value in output.reads:
        if "%s: %s" % (label, value) not in lines and part not in missing:
            missing.append(part)
    object_value = next((v for p, _, v in output.reads if p == "object"), "")
    if output.token not in object_value and "object" not in missing:
        missing.append("object")
    stamp = next((v for p, _, v in output.reads if p == "valid time"), "")
    if to_clock_format(stamp) != output.valid_from and "valid time" not in missing:
        missing.append("valid time")
    return tuple(missing)


def candidate_edge(output: Output) -> dict:
    """The edge an output stands for, as `encode_candidate` and `upsert_edge` take it."""
    return {
        "subj_node": output.subj_node, "predicate": output.predicate,
        "obj_node": output.obj_node, "obj_literal": output.obj_literal,
        "obj_datatype": output.obj_datatype, "source_quote": output.quote,
        "extractor": "deterministic", "extractor_version": EXTRACTOR_VERSION, "confidence": 1.0,
        "valid_from": output.valid_from, "source_created_at": output.valid_from,
    }


# ------------------------------------------------------------------ the write part
def new_report() -> dict:
    return {"outputs": 0, "placed": 0, "supported": 0, "reanchored": 0, "unchanged": 0,
            "retracted": 0, "failed": 0, "failures": [], "skipped": None}


def add_report(total: dict, part: dict | None) -> dict:
    """Fold one note's report into a capture total (drain.capture)."""
    if part:
        for key in ("outputs", "placed", "supported", "reanchored", "unchanged", "retracted", "failed"):
            total[key] += part.get(key, 0)
        total["failures"].extend(part.get("failures", ()))
    return total


def _ensure_node(db, writer, node_id: str, node_type: str) -> None:
    """The node of an exact identifier. It is minted as written, with one `declared` alias (the id
    itself, and the bare class name for an error class), and never merged or matched near: an exact
    id that differs by one character is another node, and no merge candidate is surfaced."""
    if db.conn.execute("SELECT 1 FROM nodes WHERE node_id=?", (node_id,)).fetchone() is None:
        writer.upsert_node(node_id, node_type=node_type, display_name=node_id)
    surfaces = [node_id] + ([node_id[len("error:"):]] if node_id.startswith("error:") else [])
    for surface in surfaces:
        norm = norm_surface(surface)
        if norm and db.conn.execute(
                "SELECT 1 FROM aliases WHERE node_id=? AND norm_surface=? AND kind='declared'",
                (node_id, norm)).fetchone() is None:
            writer.add_alias(node_id, surface, norm, kind="declared", status="bound")


def apply(db, writer, *, doc_id: str, text: str, header_block: dict | None, private: bool,
          outputs=None) -> dict:
    """Run the rules on one note inside the caller's transaction and return a report. `header_block`
    is the active block that holds the note header (block_id, block_content_id, text). A private
    document runs no rule. `outputs` lets a caller (a test, a later rule version) give the outputs;
    by default they are `extract(text)`."""
    report = new_report()
    if private:
        report["skipped"] = "private"
        return report
    found = extract(text) if outputs is None else list(outputs)
    report["outputs"] = len(found)
    if not found:
        return report
    for output in found:
        if header_block is None:
            _failed(report, output, ("header-block",))
            continue
        missing = check(output)
        if not missing and normalize_text(output.quote) not in normalize_text(header_block["text"]):
            missing = ("quote",)
        if missing:
            _quarantine(db, writer, doc_id, header_block, output, missing, report)
        else:
            _place(db, writer, doc_id, header_block, output, report)
    return report


def _failed(report: dict, output: Output, missing: tuple) -> None:
    report["failed"] += 1
    report["failures"].append({"rule_id": output.rule_id, "event": output.event_id,
                               "missing": list(missing)})


def _quarantine(db, writer, doc_id, block, output: Output, missing: tuple, report: dict) -> None:
    """OB-16: a failing output is its own quarantined row, never `encode_candidate`. When its edge id
    already names an edge the output writes nothing (not an edge, a corroboration or a claim): it
    is only counted."""
    _failed(report, output, missing)
    edge = candidate_edge(output)
    edge_id = make_edge_id(edge["subj_node"], edge["predicate"], obj_key(edge), block["block_content_id"])
    if db.conn.execute("SELECT 1 FROM edges WHERE edge_id=?", (edge_id,)).fetchone():
        return
    _ensure_node(db, writer, output.subj_node, output.subj_type)
    if output.obj_node is not None:
        _ensure_node(db, writer, output.obj_node, output.obj_type)
    edge.update(edge_id=edge_id, source_block_id=block["block_id"], source_doc_id=doc_id,
                status="quarantined", reconcile_verdict=None)
    writer.upsert_edge(edge)
    writer.add_edge_origin(edge_id, output.claim_ref, rule_id=output.rule_id,
                           source_event_id=output.event_id, source_ref=output.source_ref,
                           quote=output.quote)


def _live_claims(db, output: Output) -> list:
    """The edges that hold a live claim of this rule output's note, same subject and predicate. An
    `invalidated` edge counts beside an active one: two moves of a task in the same second (the record
    ts has whole seconds) leave the earlier one invalidated as a same-instant correction, and a re-render of
    that note must follow its own edge instead of meeting the later move as a prior and overturning it.

    An edge that an applied owner correction retracted counts too (OB-14): the output is that same
    fact in its corrected state, so a re-render moves the retracted edge to the new block and quote,
    and writes no second, active copy that would bring the fact back or invalidate its replacement.
    The correction names the edge in `corrections.target_edge_id`."""
    return db.conn.execute(
        "SELECT e.edge_id, e.source_block_id, e.source_doc_id, e.source_quote, e.obj_node, "
        "e.obj_literal FROM edge_origin o JOIN edges e ON e.edge_id=o.edge_id "
        "WHERE o.claim_ref=? AND o.status='live' AND (e.status IN ('active', 'invalidated') "
        "OR (e.status='retracted' AND EXISTS (SELECT 1 FROM corrections c "
        "WHERE c.target_edge_id=e.edge_id AND c.status='applied'))) "
        "AND e.subj_node=? AND e.predicate=?",
        (output.claim_ref, output.subj_node, output.predicate)).fetchall()


def _holds(row, output: Output) -> bool:
    """Whether an edge row has the output's object."""
    if output.obj_node is not None:
        return row["obj_node"] == output.obj_node
    return row["obj_node"] is None and row["obj_literal"] == output.obj_literal


def _place(db, writer, doc_id, block, output: Output, report: dict) -> None:
    held = _live_claims(db, output)
    same = [row for row in held if _holds(row, output)]
    for row in held:
        if not _holds(row, output):
            # a re-render changed what this rule reads: the old object is retracted, its claim withdrawn
            writer.retract_edge(row["edge_id"], reason="record-rule-output-changed",
                                retracted_by="ingest")
            writer.withdraw_edge_origin(row["edge_id"], output.claim_ref)
            report["retracted"] += 1
    _ensure_node(db, writer, output.subj_node, output.subj_type)
    if output.obj_node is not None:
        _ensure_node(db, writer, output.obj_node, output.obj_type)
    if same:
        # OB-17: the claim is live on an active edge with this subject, predicate and object, so the
        # edge follows its note to the new block and quote instead of taking a `matches` bump.
        edge = same[0]
        moved = False
        if edge["source_doc_id"] == doc_id:
            moved = writer.rebind_edge(edge["edge_id"], block["block_id"], doc_id, output.quote)
        writer.move_corroboration(edge["edge_id"], doc_id, block["block_id"])
        if moved or db.conn.execute(
                "SELECT 1 FROM edge_origin WHERE edge_id=? AND claim_ref=? AND quote_sha256<>?",
                (edge["edge_id"], output.claim_ref, sha256_hex(output.quote))).fetchone():
            writer.set_edge_origin_quote(edge["edge_id"], output.claim_ref, output.quote)
        report["reanchored" if moved else "unchanged"] += 1
        _link(writer, output, block)
        return
    candidate = candidate_edge(output)
    candidate.update(source_block_id=block["block_id"], source_doc_id=doc_id)
    result = encode_candidate(db, writer, candidate, block["block_content_id"],
                              block_text=block["text"], successive_of="record-rule", rule_id=output.rule_id,
                              event_id=output.event_id)
    verdict = result["verdict"]
    holder = result["prior_edge_id"] if verdict == "matches" else result["edge_id"]
    writer.add_edge_origin(holder, output.claim_ref, rule_id=output.rule_id,
                           source_event_id=output.event_id, source_ref=output.source_ref,
                           quote=output.quote)
    if verdict in ("novel", "refines", "contradicts"):
        report["placed"] += 1
    elif verdict == "matches":
        report["supported"] += 1
    else:
        report["unchanged"] += 1
    if not result["quarantined"]:
        _link(writer, output, block)


def _link(writer, output: Output, block: dict) -> None:
    """The PPR bridges: the subject claims the header block, the object is mentioned in it. (The
    exact floor reads only `claim` and `definition`, so an op or a status node that many notes name
    does not flood it.)"""
    writer.add_node_block(output.subj_node, block["block_id"], role="claim")
    if output.obj_node is not None:
        writer.add_node_block(output.obj_node, block["block_id"], role="mention")
