"""ingest.alpaca_sources (Alpaca-native, no upstream): Alpaca's own records as Rune2 sources (FR-9).

A proof report, a task contract and a decision page become source documents when the drain
captures the event that sealed or wrote them (design/rune2-memory/architecture.md 8.3), so the
rationale and the failures they hold are retrievable without a transcript. Alpaca keeps authority:
this module only reads, never writes the record, a proof or a page.

| event kind      | source_ref           | text rendered from                                        |
| proof-report    | proof:<path>         | the report file, only when its bytes hash to the event's  |
| task-contract   | contract:<task>      | the event data (the newest event per task is current)     |
| decision        | decision:<id>        | the event data, laid out as the decision page             |

A Rune2 briefing pasted into a report or a decision's text is left out of the copy with a line that
says so (`briefing.strip_generated`, FR-20). Each document's first paragraph carries `source_ref:`
and `event:`. `drain.projection_identity`
reads `source_ref:` first, so a newer version of the same source retires the older one, while the
event's own note (identity `event:<id>`) stays active beside it. A source that cannot be rendered
is a gap, returned with its reason and never papered over: a proof whose bytes no longer hash to the
sealed value is not served.
"""
from __future__ import annotations

import hashlib
import os
from typing import NamedTuple

PROOF = "proof-report"
CONTRACT = "task-contract"
DECISION = "decision"
SOURCE_KINDS = frozenset({PROOF, CONTRACT, DECISION})

#: A proof report larger than this is not read into a note (a gap with its reason).
MAX_PROOF_BYTES = 4 * 1024 * 1024
_SLUG_CHARS = 60

#: The record names 58 early proof reports by `.te/proofs/<name>`, the folder the project used
#: before it was renamed; the same files sit under `.alpaca/proofs/<name>` (W-07).
LEGACY_PROOF_DIR = ".te/proofs/"
PROOF_DIR = ".alpaca/proofs/"


class Rendered(NamedTuple):
    doc_id: str
    text: str
    source_ref: str
    event_id: int
    #: the document as rendered before any redaction, which the marking scanner reads
    #: (`drain.note_placement`): the screen may mask the word a marking rule reads
    raw: str | None = None


class Gap(NamedTuple):
    event_id: int
    source_ref: str | None
    reason: str


def _helpers():
    from . import drain          # lazy: drain imports this module inside capture
    return drain._one_line, drain._escape_ts, drain._slug


def source_ref(event: dict) -> str | None:
    """The source_ref an event of a source kind names, or None when it names none."""
    data = event.get("data") or {}
    kind = event.get("kind")
    if kind == PROOF:
        path = data.get("path")
        return "proof:%s" % path if isinstance(path, str) and path.strip() else None
    if kind == CONTRACT:
        return "contract:%s" % event["ref"] if event.get("ref") else None
    if kind == DECISION:
        ident = data.get("id") or event.get("ref")
        return "decision:%s" % ident if ident else None
    return None


def _inside(root: str, path: str) -> str | None:
    """The real path of `path` under `root`, or None when it escapes the project root."""
    candidate = path if os.path.isabs(path) else os.path.join(root, path)
    real, base = os.path.realpath(candidate), os.path.realpath(root)
    try:
        return real if os.path.commonpath([real, base]) == base else None
    except ValueError:
        return None


def _read_proof(root: str, path: str) -> tuple[bytes | None, str]:
    """(bytes, "") of the proof file an event names, or (None, reason). A legacy `.te/proofs/`
    path whose file is not there is read from `.alpaca/proofs/` under the same name; the caller's
    sha256 check still decides whether the bytes are the sealed ones."""
    candidates = [path]
    clean = os.path.normpath(path).replace(os.sep, "/")
    if clean.startswith(LEGACY_PROOF_DIR):
        candidates.append(PROOF_DIR + clean[len(LEGACY_PROOF_DIR):])
    for candidate in candidates:
        real = _inside(root, candidate)
        if real is None:
            return None, "path-outside-root"
        try:
            if os.path.getsize(real) > MAX_PROOF_BYTES:
                return None, "proof-too-large"
            with open(real, "rb") as stream:
                return stream.read(), ""
        except OSError:
            continue
    return None, "proof-file-missing"


def unmatched(root: str, path: str, sealed: str) -> str | None:
    """Why the proof `path` sealed with `sealed` cannot be rendered as a source (the reason `render`
    gives its Gap: `proof-file-missing`, `path-outside-root`, `proof-too-large`, `sha256-mismatch`),
    or None when its bytes hash to the seal. `wiki status` lists these as unmatched proofs
    (architecture.md 9.3)."""
    raw, why = _read_proof(root, path)
    if raw is None:
        return why
    if not sealed or hashlib.sha256(raw).hexdigest() != sealed:
        return "sha256-mismatch"
    return None


def _bullets(title: str, items) -> list:
    if not items:
        return []
    if isinstance(items, str):
        items = [items]
    return ["", "## " + title] + ["- " + str(item).strip() for item in items if str(item).strip()]


def _contract_body(event: dict, data: dict) -> list:
    lines = ["# Task contract %s" % event["ref"]]
    for title, key in (("Input", "input"), ("Expected", "expected"), ("Done bar", "done_bar"),
                       ("Fail cases", "fail_cases")):
        lines.extend(_bullets(title, data.get(key)))
    for title, key in (("Stage", "stage"), ("Source", "source")):
        if data.get(key):
            lines.extend(["", "## " + title, str(data[key]).strip()])
    return lines


def _decision_body(ident: str, data: dict) -> list:
    """The layout of the decision page (alpaca/decisions.py `_render_page`), from the event data."""
    lines = ["# Decision %s" % ident, "",
             "- kind: %s" % (data.get("kind") or ""),
             "- pointer: %s" % (data.get("pointer") or "(none)"),
             "- recorded: %s" % (data.get("ts") or ""), "",
             "## Context", str(data.get("context") or ""), "", "## Options"]
    options = data.get("options") or []
    lines.extend("- %s" % option for option in ([options] if isinstance(options, str) else options))
    lines.extend(["", "## Choice", str(data.get("choice") or ""), "",
                  "## Consequence", str(data.get("consequence") or "")])
    return lines


def render(root: str, event: dict) -> Rendered | Gap | None:
    """The source document of one captured event, a Gap when it names a source it cannot render,
    or None for an event that is not a source kind."""
    kind = event.get("kind")
    if kind not in SOURCE_KINDS:
        return None
    one_line, escape_ts, slug = _helpers()
    seq = int(event.get("id") or 0)
    data = event.get("data") or {}
    ref = source_ref(event)
    if ref is None:
        return Gap(seq, None, "no-source-ref")
    header = ["source_ref: %s" % one_line(ref)]
    extra: list = []
    if kind == PROOF:
        role, name = "proof", os.path.basename(data["path"]) or "proof"
        raw, why = _read_proof(root, data["path"])
        if raw is None:
            return Gap(seq, ref, why)
        sealed = str(data.get("sha256") or "")
        if not sealed or hashlib.sha256(raw).hexdigest() != sealed:
            return Gap(seq, ref, "sha256-mismatch")
        extra = ["path: %s" % one_line(data["path"]), "sha256: %s" % sealed]
        body = raw.decode("utf-8", errors="replace").rstrip("\n").split("\n")
    elif kind == CONTRACT:
        role, name = "contract", str(event["ref"])
        body = _contract_body(event, data)
    else:
        role, name = "decision", ref.split(":", 1)[1]
        body = _decision_body(name, data)
    header.append("role: %s" % role)
    for label, value in (("op", event.get("op")), ("kind", kind), ("event", seq),
                         ("session", event.get("session")), ("operator", event.get("_operator")),
                         ("task", None if kind == DECISION else event.get("ref"))):
        flat = one_line(value)
        if flat:
            header.append("%s: %s" % (label, flat))
    header.extend(extra)
    header.append("ts: %s" % one_line(event.get("ts")))
    from alpaca.wiki.briefing import strip_generated
    from .drain import screen
    # a Rune2 briefing pasted into a proof report or a decision's text is left out with a line that
    # says so (FR-20): generated text is never landed as source text
    kept = strip_generated("\n".join(body))
    # the document as rendered, before any redaction (what the marking scanner reads), then screened
    # once as a whole, header values included (FR-26; t-070 RC2: the body was redacted, then the
    # whole document screened again)
    raw = "\n".join(header) + "\n\n" + escape_ts(kept).replace("[[", "[ [").replace("]]", "] ]") + "\n"
    text = screen(raw)
    doc_id = "raw/sources/%s/%06d-%s.md" % (role, seq, slug(name)[:_SLUG_CHARS])
    return Rendered(doc_id, text, ref, seq, raw)
