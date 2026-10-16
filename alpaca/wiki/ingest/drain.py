"""ingest.drain (Alpaca-native, no upstream): the session drain that feeds the vendored write path.

The drain reads one session's record events and its transcript out of the Alpaca store and lands them
as raw markdown notes in the wiki vault, then hands each note to the vendored write path
(ingest.absorb). It is the SOURCE in front of absorb, never a second write door.

Capture is DUMB (M2.14 Step 3): the drain assigns a note's role by a fixed mechanical lookup over
the event kind and NEVER judges the pairing of an intent with its result - that is `alpaca sort`'s job.
Re-running the drain over the same session is a no-op: the note bytes are identical, so absorb's
hashgate skips every unchanged block (idempotent), and the disk write is skipped when unchanged.

The note carries its op, role and pairing key in a plain first paragraph (`key: value` lines) so
`alpaca sort` can read them back off the ingested block without a second schema. No wikilinks are
emitted, so a captured note mints no wikilink edges of its own. The record rules
(ingest/record_extract.py, run by absorb) fill the graph from the header lines instead.

Note format 2 (design/rune2-memory/architecture.md 8.1): the first paragraph is `op:`, `role:`,
`key:`, `kind:`, `event:`, `session:`, `operator:`, `task:` (the event `ref`), one line per field
the kind's record rules read (`RULE_FIELDS`, subject first), the `data-` prefixed copy of any data
field named like a header key, and `ts:` last. The `_summary` paragraph stays the second block.

Capture honesty (architecture.md 9.5, FR-8): a transcript that is missing or unreadable never drops
the session's event notes; a document the marking scanner trips on is routed under `wiki/private/`
with a `capture-quarantined` record event instead of aborting the capture; a deadline stops the
capture between documents and reports what is left (`deferred`). Alpaca sources (FR-9, proof
reports, task contracts, decision pages) come from `alpaca_sources`.
"""
from __future__ import annotations

import os
import json
import re
import stat
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from alpaca import db as tedb
from alpaca import paths

# A note's role is a MECHANICAL lookup on the event kind, not a judgment (Step 3: capture is dumb).
# Everything the session emitted is landed; only the intent/result split is pre-labelled here, and
# even that is a fixed table, never a decision about a specific event.
INTENT_KINDS = frozenset({
    "intent", "task-add", "task-claim", "claim", "handoff", "question",
    "phase-enter", "row-open", "op-open", "op-new",
})
RESULT_KINDS = frozenset({
    "result", "task-done", "row-done", "run", "verdict", "phase-gate", "op-close",
})

#: Record events Rune2 itself appends about its own work (architecture.md 7.5, 10.2). They are never
#: rendered as a source, so a candidate, an admission, a withdrawal or a quarantine can never be
#: cited as evidence for the claim it is about (FR-20, FR-33).
NOT_SOURCES = frozenset({
    "wiki-candidate", "wiki-admission", "wiki-candidate-withdrawn", "wiki-withdrawal",
    "capture-quarantined", "rune2-consolidate-failure", "transcript-version",
})

#: The record event a capture appends when it lands a transcript version (t-430, owner decision
#: dec-f812470497fb): ref `transcript:<session>`, data {content_sha256, source_bytes, source_sha256,
#: capture_head, held_before}. A replay at a cutoff before the record head reads each transcript at
#: the version the newest of them at or below the cutoff names (`recovery._transcript_versions_at`).
TRANSCRIPT_VERSION_KIND = "transcript-version"
#: The synthetic writer session of those events (`sessions_view.SERVICE_IDS`): the event is about
#: the transcript's session (its ref), so a subagent with no event of its own is not made a session.
TRANSCRIPT_VERSION_WRITER = "wiki-capture"


def renders_as_source(kind: str, data: dict | None) -> bool:
    """The one "is this event a source" predicate (architecture.md 9.3): the note renderer and the
    expected event sets of `status` and `coverage` use it. A `wiki-correction` is a source only when
    it was applied by the owner; every kind in `NOT_SOURCES` is not."""
    if kind in NOT_SOURCES:
        return False
    if kind == "wiki-correction":
        data = data or {}
        return data.get("result") == "applied" and data.get("actor") == "owner"
    return True


def wiki_vault_dir(root: str) -> str:
    """The wiki vault for an Alpaca project lives under the runtime dir, beside alpaca.db."""
    return os.path.join(paths.runtime_dir(root), "wiki")


def _slug(value, fallback: str = "none") -> str:
    out = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "")).strip("-")
    return out or fallback


def _role_of(kind: str, data=None) -> str:
    data = data or {}
    if kind == "msg":
        kind = data.get("kind")
    elif kind == "task-move" and data.get("to") == "done":
        kind = "task-done"
    if kind in INTENT_KINDS:
        return "intent"
    if kind in RESULT_KINDS:
        return "result"
    return "note"


def _pair_key(ev: dict) -> str:
    if ev.get("_pair_key"):
        return ev["_pair_key"]
    data = ev.get("data") or {}
    for candidate in (ev.get("ref"), data.get("row"), data.get("task"),
                      data.get("id"), data.get("op"), ev.get("op")):
        if candidate:
            return str(candidate)
    return ""


#: The token only `screen` passes to `Screened` (t-070 round 2 C-C, I-4).
_SCREEN_TOKEN = object()


class Screened(str):
    """A text the one secret screen produced (`screen`): a fixed point of it. Only `screen` makes
    one: the class refuses a call without the module's private token, so a caller cannot mark text
    it did not screen (t-070 round 2 C-C, I-4). The write door (`Absorber.absorb_text`) stores a
    Screened text as it is and screens any other text itself, so every document passes the screen
    exactly once, whichever path brought it (t-070 RC2). A Screened text that is sliced, joined,
    copied or pickled is a plain `str` again, which the door screens."""
    __slots__ = ()

    def __new__(cls, text, token=None):
        if token is not _SCREEN_TOKEN:
            raise TypeError("only drain.screen makes a Screened text")
        return super().__new__(cls, text)

    def __reduce__(self):
        return str, (str(self),)


def screen(text: str) -> Screened:
    """The secret screen of every text Rune2 retains (FR-26, HS-SCO-05; dec-804f2708474c Q-A;
    t-070 RC2): the one screen of Alpaca (`alpaca.analytics.detail.screen`) over the whole text, so
    an AWS key, a PEM private key, a Slack token, a JWT, a bearer or API key and a plain phrasing such
    as "the PIN is X" are stored as `[redacted]`, and a mask written in brackets is spaced out of a
    wikilink. It applies one pass of every class until the text stops changing, so
    screen(screen(x)) == screen(x); its result is marked `Screened`, and screening a Screened text
    again returns it as it is, with no pass.

    The doors call it once per text: the write door (`Absorber.absorb_text`) for a document a caller
    did not screen, the drain's renderers once each (`_note_doc`, `_transcript_doc`,
    `alpaca_sources.render`), so the bytes the drain writes to the vault are the bytes the door
    stores (recovery.verify_documents), `service.ingest` and the import (`portable`) once before
    their vault copy, and candidate intake (`intake.parse_proposal`) and the write verbs of
    `service` on the free text they record."""
    if type(text) is Screened:
        return text
    from alpaca.analytics import detail
    return Screened(detail.screen(text), _SCREEN_TOKEN)


def _summary(ev: dict, *, redact: bool = True) -> str:
    """A plain one-line rendering of the event. Deliberately free of wikilinks so a note mints no
    wikilink edges; the record rules read the header lines, and sort writes its own assertions. A
    Rune2 briefing pasted into a value (a message that hands one on, for one) is left out of it
    with a line that says so (`briefing.strip_generated`, FR-20), and so is one inside a string of a
    list or dict value, which renders as JSON (a briefing line given as a decision option): its
    string keeps what is left of it. Generated text is never landed as source text, so it can never
    anchor, support or corroborate. A list or dict value is redacted before it renders, so a secret
    under a secret-named key is masked whole (FR-26), and so is each of its strings, since the JSON
    it renders as escapes the quotes a phrasing is read by; `_note_doc` screens the note's text once.
    With `redact` false nothing is redacted: the rendering the marking scanner reads
    (`note_placement`)."""
    from alpaca.analytics import detail
    from alpaca.wiki.briefing import strip_generated
    data = ev.get("data") or {}
    bits = ["%s during session %s" % (ev.get("kind", "event"), ev.get("session", ""))]
    for k in ("reason", "statement", "title", "body", "verdict", "note", "detail", "error",
              "context", "options", "choice", "consequence", "pointer", "proof", "from", "to"):
        v = data.get(k)
        if v:
            value = (json.dumps(detail._redact(v) if redact else v, ensure_ascii=True)
                     if isinstance(v, (dict, list)) else str(v))
            bits.append(k + ": " + strip_generated(value).replace("\n", " ").strip())
    return ". ".join(b for b in bits if b).replace("[[", "[ [").replace("]]", "] ]")


def observed_prefixes(root) -> tuple:
    """Kind prefixes whose event data the wiki keeps verbatim as untrusted JSON: the domain
    profile's observation kinds (alpaca/profile.py `events`). Empty without a profile."""
    from alpaca import profile
    try:
        return tuple(profile.listed(profile.load(root).events(), "observation_prefixes"))
    except Exception:
        return ()


#: Header keys of a note. A data field with one of these names renders as `data-<name>`, so each
#: key appears once and `projection_identity` and `alpaca sort` read the true value (OB-08).
HEADER_KEYS = frozenset({"op", "role", "key", "kind", "event", "session", "operator", "task",
                         "ts", "source_ref"})

#: The event data fields the record rules of architecture.md 8.1 read, per event kind, in render
#: order with the subject first (a `task:` subject comes from the event `ref`, a `session:` subject
#: from the header). A later rule that reads another field adds it here and nowhere else. A field
#: absent from the event data renders no line.
RULE_FIELDS = {
    "task-contract": ("source",),
    "task-move": ("from", "to", "proof"),
    "decision": ("id", "choice"),
    "proof-report": ("path", "sha256"),
    "drain-failed": ("where", "error"),
    "verdict": ("verdict", "verdict_name"),
    "run": ("gate", "verdict", "code"),
    "msg": ("to",),
    # An owner correction renders as a source only when applied (`renders_as_source`); the `actor:`
    # line says who applied it, and `action` which kind of correction it is (architecture.md 9.3, 10.2).
    "wiki-correction": ("actor", "action"),
}
#: Kinds whose `ref` column is not a task id, so no `task:` line is rendered for them.
_REF_NOT_TASK = frozenset({"decision"})

_WHITESPACE = re.compile(r"\s+")
_LINE_BREAK = re.compile("(\r\n|[\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029])")


def _one_line(value, *, cut: bool = False) -> str:
    """A value flattened to one line: no line break survives, so a value cannot add a header line.
    `[[` is neutralised as in `_summary`, so a note never carries a wikilink. With `cut`, a Rune2
    briefing in the value is left out first, while its line breaks still stand
    (`briefing.strip_generated`, FR-20): the event data a header line renders."""
    if value is None:
        return ""
    text = (json.dumps(value, sort_keys=True, ensure_ascii=True)
            if isinstance(value, (dict, list)) else str(value))
    if cut:
        from alpaca.wiki.briefing import strip_generated
        text = strip_generated(text)
    return _WHITESPACE.sub(" ", text).strip().replace("[[", "[ [").replace("]]", "] ]")


#: The start of a header value written in its escaped form (`header_field`).
ESCAPED_PREFIX = 'json:"'


def header_field(value, *, cut: bool = False) -> str:
    """One header value, rendered so that reading the line back gives exactly the text it was made
    from (`header_value`; t-070 round 2 C-B, A-9). A value `_one_line` keeps as it is (one line, no
    run of whitespace, no leading or trailing space, no `[[` or `]]`) and that does not start with
    `ESCAPED_PREFIX` renders as before, so the headers of the live record render the same bytes (a
    read-only probe over its 134,570 source events found no other value,
    .alpaca/proofs/op-008/t-070.logs/r2/header-probe-cbe.out). Any other value renders as `json:`
    and its JSON string with every non-ASCII character and every square bracket escaped: one line,
    no wikilink, and a session id such as `x\\noperator: claude` cannot add a header line or read
    back as another id. `cut` leaves a Rune2 briefing out first (`briefing.strip_generated`, FR-20).
    An empty value renders as the empty string, and the caller leaves its line out."""
    if value is None:
        return ""
    text = (json.dumps(value, sort_keys=True, ensure_ascii=True)
            if isinstance(value, (dict, list)) else str(value))
    if cut:
        from alpaca.wiki.briefing import strip_generated
        text = strip_generated(text)
    if not text:
        return ""
    if _one_line(text) == text and not text.startswith(ESCAPED_PREFIX):
        return text
    return "json:" + json.dumps(text, ensure_ascii=True).replace("[", "\\u005b").replace("]", "\\u005d")


def header_value(raw: str) -> str:
    """The text a header value was rendered from (`header_field`): an escaped value is decoded, any
    other value is returned as it is."""
    if raw.startswith(ESCAPED_PREFIX) and raw.endswith('"') and len(raw) > len(ESCAPED_PREFIX):
        try:
            decoded = json.loads(raw[len("json:"):])
        except ValueError:
            return raw
        if isinstance(decoded, str):
            return decoded
    return raw


def header_fields(text) -> dict:
    """The `key: value` lines of a document's first paragraph, each value read back with
    `header_value`; a key that appears twice keeps its last value."""
    meta = {}
    for line in (text or "").split("\n\n", 1)[0].splitlines():
        key, separator, value = line.partition(":")
        if separator:
            meta[key] = header_value(value.strip())
    return meta


def _escape_ts(text: str) -> str:
    """A `ts:` line inside message, input or result text gets a leading space, so only the renderer
    writes a `ts:` line at column 0 (OB-01). Splits on every line break `str.splitlines` knows."""
    if "ts:" not in text:
        return text
    parts = _LINE_BREAK.split(text)
    for index in range(0, len(parts), 2):
        if parts[index].startswith("ts:"):
            parts[index] = " " + parts[index]
    return "".join(parts)


def _note_header(ev: dict, op, role: str, seq: int) -> list:
    data = ev.get("data") or {}
    kind = ev.get("kind") or ""
    # every value goes through the one field encoder (`header_field`, t-070 round 2 C-B): reading
    # the header back gives each value exactly, and no value can add a line
    header = [
        "op: %s" % header_field(op, cut=True),
        "role: %s" % role,
        "key: %s" % header_field(_pair_key(ev), cut=True),
        "kind: %s" % header_field(kind),
        "event: %s" % seq,
    ]
    for name, value in (("session", ev.get("session")),
                        ("operator", ev.get("_operator") or data.get("operator")),
                        ("task", None if kind in _REF_NOT_TASK else ev.get("ref"))):
        flat = header_field(value, cut=True)
        if flat:
            header.append("%s: %s" % (name, flat))
    rendered = set()
    # every value a header line renders from the event is cut like the note's body (a briefing line
    # given as a decision's choice, for one); kind, role, event and ts are ids Alpaca writes itself
    for field in RULE_FIELDS.get(kind, ()):
        flat = header_field(data.get(field), cut=True)
        if flat:
            name = "data-" + field if field in HEADER_KEYS else field
            header.append("%s: %s" % (name, flat))
            rendered.add(field)
    for field in sorted(k for k in data if k in HEADER_KEYS and k not in rendered):
        flat = header_field(data[field], cut=True)
        if flat:
            header.append("data-%s: %s" % (field, flat))
    header.append("ts: %s" % header_field(ev.get("ts")))
    return header


def _note_text(ev: dict, prefixes=(), *, redact: bool = True):
    """(doc_id, text) of one event note before the screen. With `redact` a list or dict value of
    the summary and an observation's JSON are redacted by key; without it nothing is redacted."""
    from alpaca.analytics import detail
    op = ev.get("op") or (ev.get("data") or {}).get("op") or "unassigned"
    role = _role_of(ev.get("kind"), ev.get("data"))
    seq = int(ev.get("id") or 0)
    text = "\n".join(_note_header(ev, op, role, seq)) + "\n\n" + _summary(ev, redact=redact) + "\n"
    if prefixes and (ev.get('kind') or '').startswith(tuple(prefixes)):
        # A profile observation: preserve its receipt/job/source pointers. JSON is untrusted
        # observation data, never instructions and never an automatically admitted lesson. A Rune2
        # briefing inside one of its strings is left out of that string (FR-20).
        from alpaca.wiki.briefing import strip_generated
        data = ev.get('data') or {}
        text += '\nUntrusted observation data:\n\n```json\n' + strip_generated(json.dumps(
            detail._redact(data) if redact else data, sort_keys=True, ensure_ascii=True)) + '\n```\n'
    return "raw/%s/%06d-%s.md" % (_slug(op), seq, role), text


def _note_doc(ev: dict, prefixes=()):
    """(doc_id, text) of one event note as it is stored. The whole text is screened (`screen`,
    FR-26): the summary, the header values a rule field or a `data-` line renders (a decision's
    choice, a drain failure's error) and an observation's JSON, which is redacted by key first.
    `recovery.verify_events` renders through here, so what it compares is what was stored."""
    doc_id, text = _note_text(ev, prefixes)
    return doc_id, screen(text)


def note_placement(ev: dict, prefixes=()):
    """(doc_id where the note lives, marking rule_id or None, the stored text, the unscreened text)
    of one event note. The marking scanner (`placement`, architecture.md 9.5) reads the note as it
    renders before any redaction, as it did before the screen existed: the screen may mask the word
    a marking rule reads ("SECRET: REL TO ..."), and it may add privacy, never take it away.
    The record-fact listing of `service` places a note here, and `recovery.verify_events` through
    `stored_note`; `capture` reads the same unscreened rendering (its `raw`), so all three agree
    where the scanner puts a note. Where the note then lands in a given store (a source held
    private, a changed rendering's revision) is `_landed_target`."""
    doc_id, text = _note_doc(ev, prefixes)
    raw = _note_text(ev, prefixes, redact=False)[1]
    target, rule_id = placement(doc_id, raw)
    return target, rule_id, text, raw


def record_head(conn) -> int:
    """The id of the record's last event as `conn` reads it (0 for an empty record)."""
    return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])


def session_operator(conn, session, through) -> str | None:
    """The operator a session declared at its first `session-start` event at or below record event
    `through`, or None (FR-6). `through` is the record head the projection covers: a replay's cutoff,
    or the head a capture read with its events (`capture`); None reads the whole record. So a store
    at head N is rendered from record events at or below N only, and a declaration appended later
    never changes a rebuild at an earlier cutoff (t-070 round 2 C-B, B-3). A declaration that arrives
    after the session's events re-renders their notes when it is captured (`capture`)."""
    if not session:
        return None
    row = conn.execute("SELECT data FROM events WHERE session=? AND kind='session-start' "
                       "AND (? IS NULL OR id<=?) ORDER BY id LIMIT 1",
                       (session, through, through)).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    except ValueError:
        return None
    operator = data.get("operator") if isinstance(data, dict) else None
    return str(operator) if operator else None


def normalize_events(conn, events, through=None):
    """Resolve native message task/op references from earlier immutable source events, and each
    session's operator as declared at or below record event `through` (`session_operator`).

    Never consult mutable current task state: a later replay at the same cutoff must
    give the same operation and pairing key. The original event dictionaries stay intact.
    """
    normalized = []
    operators = {}
    for source in events:
        event = dict(source)
        data = event.get("data") or {}
        if isinstance(data, str):
            data = json.loads(data)
        event["data"] = data
        event["op"] = event.get("op") or data.get("op")
        session = event.get("session")
        if session not in operators:
            operators[session] = session_operator(conn, session, through)
        event["_operator"] = operators[session]
        if event.get("kind") == "msg":
            for target in (event.get("ref"), data.get("to")):
                if not target:
                    continue
                prior = conn.execute(
                    "SELECT op FROM events WHERE ref=? AND id<=? AND op IS NOT NULL "
                    "AND op<>'' ORDER BY id LIMIT 1", (str(target), event["id"]),
                ).fetchone()
                if prior:
                    event["op"] = event.get("op") or prior["op"]
                    event["_pair_key"] = str(target)
                    break
        normalized.append(event)
    return normalized


class TranscriptUnavailable(OSError):
    """A registered transcript has no readable native or preserved source."""


class TranscriptMalformed(OSError):
    """A transcript file that is there and readable holds a record that does not parse (or a
    retained copy's `capture-malformed-record` marker). A torn record in the middle of a file never
    reads clean again, so the collector hands such a session to its own snapshot job instead of
    retrying the whole batch for ever (t-684, `consumers.MALFORMED_AFTER_FAILURES`)."""


class TranscriptVersionUnavailable(OSError):
    """The transcript version a record event names (its byte count and sha256) is no longer the start
    of the transcript file: the file is shorter, or its first bytes were rewritten. Nothing of the
    file is read as that version (t-070 round 2 C-B, B-4)."""

    reason = "transcript-version-unavailable"


def _transcript_path(root: str, session: str, conn, override: str | None) -> str | None:
    from alpaca import transcripts
    registered = override or transcripts.registered(conn, session)
    if registered:
        candidate = registered if os.path.isabs(registered) else os.path.join(root, registered)
        if os.path.isfile(candidate):
            return candidate
    candidate = transcripts.local_path(root, session)
    if os.path.isfile(candidate):
        return candidate
    if registered:
        raise TranscriptUnavailable("registered transcript is unavailable: " + session)
    return None


def _visible_transcript(session, path, changed=None, version=None):
    """Stream complete source records through the existing privacy/shape normalizer.

    `version` is `(source_bytes, source_sha256)`, a transcript version a record event names (a
    withdrawal, t-070 round 2 C-B, B-4): only the first `source_bytes` bytes of the file are read, and
    only when their sha256 is `source_sha256`; otherwise `TranscriptVersionUnavailable` is raised
    and nothing is read as that version.

    UI pagination budgets do not apply to durable capture: each value is kept whole. A message
    text, title or string input or result is kept as read: the one screen reads it in place, as part
    of the whole projection (`_transcript_doc`), and nothing else redacts it (t-070 RC2: the base
    capture class redacted each value, and the drain threw that copy away and redacted the value
    again before the projection was screened). A list or dict input or result renders as JSON,
    whose escaping hides the quotes a phrasing is read by (`API_KEY="..."`), so it is redacted by
    key and by value as it is read (`detail._redact`). Private reasoning is dropped by normalize.
    When `changed` is a list, every list or dict the redactor changed is appended to it as it was
    read (as JSON), so the marking scanner can read what the redaction masked (`note_placement`).
    """
    import hashlib
    from alpaca.analytics import detail

    def kept(value):
        if value is None or isinstance(value, str):
            return value, False
        out = detail._redact(value)
        if changed is not None and out != value:
            changed.append(json.dumps(value, sort_keys=True, ensure_ascii=True))
        return out, False

    class Capture(detail._Capture):
        _bound = staticmethod(kept)

        def rows(self, path, kind, **kwargs):
            digest = hashlib.sha256()
            source_id = hashlib.sha256((self.sid + ":transcript").encode()).hexdigest()[:24]
            with open(path, "rb") as whole:
                stream = whole
                if version is not None:
                    import io
                    size, sha = int(version[0]), str(version[1])
                    prefix = whole.read(size)
                    if len(prefix) != size or hashlib.sha256(prefix).hexdigest() != sha:
                        raise TranscriptVersionUnavailable(
                            "the transcript bytes a record event names are gone or were rewritten: "
                            + session)
                    stream = io.BytesIO(prefix)
                offset = 0
                for number, raw in enumerate(stream, 1):
                    digest.update(raw)
                    source = {"id": source_id, "kind": kind, "line": number, "offset": offset}
                    offset += len(raw)
                    if not raw.strip():
                        continue
                    try:
                        record = json.loads(raw)
                    except (ValueError, UnicodeError, RecursionError) as exc:
                        raise TranscriptMalformed(
                            "transcript contains an incomplete or malformed record") from exc
                    if not isinstance(record, dict) or record.get("type") == "capture-malformed-record":
                        raise TranscriptMalformed("transcript contains a malformed record")
                    yield record, source
            self.source_sha256 = digest.hexdigest()
            self.source_bytes = offset

    capture = Capture(session, "registered-transcript", detail._Budget())
    capture.transcript(path)
    capture.title = kept(capture.title)[0]
    return capture


def _transcript_doc(session: str, tp: str, operator: str | None = None, *, changed=None,
                    version=None):
    """The transcript projection of one session as it is stored: its header (with the session
    `operator:`, FR-6), then each message with a `ts:` line holding its source time (OB-01), the
    whole text screened once (`screen`). Any `ts:` line inside message, input or result text is
    escaped, so only this renderer writes one at column 0. A Rune2 briefing the session was served
    (a session-start context, a `wiki briefing` result) is left out of the title, message, input or
    result that holds it, with a line that says so (`briefing.strip_generated`, FR-20); an input or
    result that is a list or dict renders as JSON, and one of its strings that holds a briefing keeps
    what is left of it. When `changed` is a list, it gets what the marking scanner reads beside the
    stored text (`capture`), as before t-070 RC2: each list or dict the redaction changed, as read;
    each title, message, input or result string the screen changed, as read; and each other line
    the screen changed, as it was before it (the whole projection when the screen joined lines).
    `version` cuts the file to a version a record event names (`_visible_transcript`)."""
    from alpaca.wiki.briefing import strip_generated
    try:
        visible = _visible_transcript(session, tp, changed, version)
    except (FileNotFoundError, PermissionError, IsADirectoryError) as exc:
        raise TranscriptUnavailable("registered transcript is unreadable: " + session) from exc
    # Transcript identity is session-based, independent of the current event batch/op.
    # each value through the one field encoder (`header_field`): a session id holding a line break
    # cannot add a header line (t-070 round 2 C-B, A-9)
    lines = ["role: transcript", "op: session", "session: %s" % header_field(session)]
    if operator and header_field(operator):
        lines.append("operator: %s" % header_field(operator))
    # The source is named by its portable identity, the transcript's source ref, and its version by
    # the bytes read (`source_sha256`, `source_bytes`), never by the path it was read from: a path
    # names the machine, the operator's home folder and user name, and which copy happened to be
    # found, so the projection's text, content_sha256 and doc id would change with the project's
    # location and an import at another root would refuse every transcript (t-417, A-10).
    lines.extend(["trust: untrusted source", "source: " + header_field("transcript:" + session),
                  "source_sha256: " + visible.source_sha256,
                  "source_bytes: " + str(visible.source_bytes), ""])
    spans, row = [], [sum(entry.count("\n") + 1 for entry in lines)]

    def add(entries, raw=None):
        """Append entries; a string value as read (`raw`) is remembered with the physical lines of
        its last entry, which renders it."""
        for entry in entries:
            first = row[0]
            row[0] += entry.count("\n") + 1
            lines.append(entry)
        if raw is not None:
            spans.append((first, row[0], raw))

    if visible.title:
        title = visible.title
        add(["# " + strip_generated(str(title)).replace("\n", " ").strip()],
            title if isinstance(title, str) else None)
        add([""])
    for message in visible.messages:
        text = message.get("text") or ""
        stamp = _one_line(message.get("ts"))
        pending = ["ts: " + stamp] if stamp else []
        if text:
            add(["", message["role"] + ":"] + pending)
            add([_escape_ts(strip_generated(text))], text)
            pending = []
        for tool in message["tools"]:
            add(["", "tool: " + _one_line(tool["name"])] + pending)
            pending = []
            for field in ("input", "result"):
                value = tool.get(field)
                if value is not None:
                    rendered = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=True)
                    add([field + ":"])
                    add([_escape_ts(strip_generated(rendered))], value if isinstance(value, str) else None)
    # Keep immutable source projections for audit, retiring old versions from search. The one
    # screen reads the whole projection once (t-070 RC2); the write door stores it as it is.
    import hashlib
    joined = "\n".join(lines).replace("[[", "[ [").replace("]]", "] ]") + "\n"
    text = screen(joined)
    if changed is not None and text != joined:
        before, after = joined.split("\n"), text.split("\n")
        if len(before) != len(after):
            changed.append(joined)              # the screen joined lines: the whole projection
        else:
            moved = {i for i, (b, a) in enumerate(zip(before, after)) if b != a}
            owned = set()
            for first, stop, raw in spans:
                held = moved.intersection(range(first, stop))
                if held:
                    changed.append(raw)
                    owned |= held
            changed.extend(before[i] for i in sorted(moved - owned))
    version = hashlib.sha256(text.encode("utf-8")).hexdigest()
    identity = hashlib.sha256(session.encode("utf-8")).hexdigest()[:16]
    doc_id = "raw/transcripts/%s/transcript-%s-summary-%s.md" % (identity, _slug(session), version)
    return doc_id, text


def _versioned_doc(root, session, path, operator, changed, version):
    """`_transcript_doc` at the version a record event names. `version` is None (the file as it
    is), a `(source_bytes, source_sha256)` pair, or a list of `(path, source_bytes, source_sha256)`
    alternatives tried in order, a path None meaning the transcript's own resolved `path` and a
    relative one the project root's (t-430: the native file cut at the snapshot's `source_bytes`,
    then the snapshot's copy cut at its `bytes`). The first alternative whose bytes are there lands;
    when none is, `TranscriptVersionUnavailable` is raised and nothing of it lands."""
    if not isinstance(version, list):
        return _transcript_doc(session, path, operator, changed=changed, version=version)
    failure = None
    for where, size, sha in version:
        candidate = path if where is None else (where if os.path.isabs(where) else os.path.join(root, where))
        attempt = []
        try:
            out = _transcript_doc(session, candidate, operator, changed=attempt, version=(size, sha))
        except (TranscriptVersionUnavailable, TranscriptUnavailable) as exc:
            failure = exc
            continue
        changed.extend(attempt)
        return out
    raise TranscriptVersionUnavailable(
        "no transcript version a record event names at the cutoff is there: " + session) from failure


def projection_identity(text):
    """One source, regardless of renderer version or operation label: an Alpaca source by its
    `source_ref:` header (checked first, FR-9), else one event note or one session transcript."""
    meta = header_fields(text)
    if meta.get("source_ref"):
        return meta["source_ref"]
    if meta.get("event"):
        return "event:" + meta["event"]
    if meta.get("role") == "transcript" and meta.get("session"):
        return "transcript:" + meta["session"]
    return None


def placement(doc_id: str, text: str):
    """(doc_id, rule_id) where the document must live: a document the marking scanner trips on is
    routed under `wiki/private/` (architecture.md 9.5), where it lands `private=1`; rule_id names
    the marking rule, None when nothing tripped. Pure: the scanner reads the text only."""
    from alpaca.wiki.engine.compartment import is_private_path
    from alpaca.wiki.ingest import firewall
    result = firewall.scan(text)
    if result.tripped and not is_private_path(doc_id):
        return "wiki/private/" + doc_id, result.rule_id
    return doc_id, (result.rule_id if result.tripped else None)


def _recorded_quarantines(root) -> set:
    """doc_ids the record already holds a `capture-quarantined` event for."""
    conn = tedb.connect_readonly(root)
    try:
        out = set()
        for row in conn.execute("SELECT data FROM events WHERE kind='capture-quarantined'"):
            try:
                out.add(json.loads(row[0]).get("doc_id"))
            except (ValueError, AttributeError):
                pass
        return out
    finally:
        conn.close()


def _record_quarantine(root, session, doc_id, rule_id, recorded: set) -> None:
    """Append the `capture-quarantined` lifecycle event (actor alpaca-wiki) once per document."""
    if doc_id in recorded:
        return
    conn = tedb.connect(root)
    try:
        tedb.append_event(conn, session=session or "wiki", actor="alpaca-wiki",
                          kind="capture-quarantined", data={"doc_id": doc_id, "rule_id": rule_id})
    finally:
        conn.close()
    recorded.add(doc_id)


def _header_value(text, key):
    """The value of one line of a document's first paragraph, or None."""
    match = re.search(r"^%s: (.+)$" % re.escape(key), (text or "").split("\n\n", 1)[0], re.M)
    return header_value(match[1]) if match else None


def _event_no(text):
    match = re.search(r"^event: (\d+)$", (text or "").split("\n\n", 1)[0], re.M)
    return int(match[1]) if match else None


#: What a transcript costs to land, in seconds, from the size of its native file: a base for the
#: render and the first blocks and a rate per byte (the rendered text is bounded, so the rate is
#: small). Measured on a copy of the evaluation store, 40,505 active blocks, with the development-only
#: probe t_transcript_cost.py kept with the t-065 execution notes: 73 KB to 1.7 MB
#: took 1.5 to 2 s, 15 MB took 5.3 s. Only a hook, which knows when it is cut, asks for it.
TRANSCRIPT_BASE_S = 1.5
TRANSCRIPT_S_PER_BYTE = 0.4e-6


def transcript_cost_s(path) -> float:
    try:
        return TRANSCRIPT_BASE_S + TRANSCRIPT_S_PER_BYTE * os.path.getsize(path)
    except OSError:
        return TRANSCRIPT_BASE_S


#: A capture of at most this many documents asks the store only for the first blocks that can hold
#: their sources; a larger one reads them all once.
NARROW_SCAN_LIMIT = 8


def _previous_projections(conn, texts, sessions, *, full: bool = False) -> dict:
    """`{identity: [(doc_id, event number)...]}` of the active first blocks of raw documents, for
    the sources the documents about to land belong to (`texts`, and the transcripts of
    `sessions`). A small batch matches the header line of each source in SQL, so the cost does not
    grow with the store; the match is a superset (`event: 7` also finds `event: 71`) and the
    identity read below decides, so the entries for the batch's own identities are those of a scan
    of every raw document. With `full` every raw document is read, whatever the batch: the
    inventory a caller shares across captures (`capture(projection_index=...)`) must hold every
    identity, not only the first batch's."""
    wanted = {}
    for text in texts:
        identity = projection_identity(text)
        if identity:
            wanted[identity] = None
    for session in sessions:
        wanted["transcript:" + session] = None
    sql = ("SELECT b.doc_id,b.text FROM blocks b JOIN docs d ON d.doc_id=b.doc_id "
           "WHERE d.kind='raw' AND b.status='active' AND b.ordinal=0")
    plain = all(re.fullmatch(r"[A-Za-z0-9._:-]+", session) for session in sessions)
    if not full and len(wanted) <= NARROW_SCAN_LIMIT and plain:  # a session id the header may flatten scans all
        terms, params = [], []
        for identity in wanted:
            kind, _, name = identity.partition(":")
            if kind == "event":
                terms.append("instr(b.text, ?) > 0")
                params.append("event: " + name)
            elif kind == "transcript":
                terms.append("(instr(b.text, 'role: transcript') > 0 AND instr(b.text, ?) > 0)")
                params.append("session: " + name)
            else:
                terms.append("instr(b.text, ?) > 0")
                params.append("source_ref: " + identity)
        if not terms:
            return {}
        rows = conn.execute(sql + " AND (" + " OR ".join(terms) + ")", params)
    else:
        rows = conn.execute(sql)
    previous = {}
    for row in rows:
        identity = projection_identity(row["text"])
        if identity:
            previous.setdefault(identity, []).append((row["doc_id"], _event_no(row["text"])))
    return previous


def capture(root, events, *, sessions=(), clock=None, transcript_paths=None, absorber=None,
            deadline=None, skip_notes=frozenset(), hard_deadline=None, record_time=False,
            projection_index=None, min_documents=0, rerender_notes=False, progress=None,
            through=None, transcript_versions=None, record_versions=False):
    """Shared bounded event-batch capture for drains, collector, hooks and recovery.

    Documents land in this order: event notes, Alpaca sources (FR-9), session transcripts. A
    transcript that is missing or unreadable is reported in `missing_transcripts`, never fails the
    capture, so the event notes survive it (FR-8). `deadline` is a `time.monotonic()` value: when it
    passes, the capture stops between documents and `deferred` names `through_event` (the last event
    note landed) and `remaining` (documents left); the caller records `capture-deferred`.
    `min_documents` documents are taken before `deadline` is first checked, so a caller that retries
    a deferred capture (the collector) always gets on, however long the work before the first
    document took; the hooks keep the default 0 and stay within their ceilings.
    `skip_notes` are event ids whose note is already held: they render their Alpaca source only.
    `hard_deadline` (a `time.monotonic()` value, only a hook passes one) is the moment its process
    is cut: a transcript whose estimated cost (`transcript_cost_s`) passes it is deferred without
    being rendered, and a document still being absorbed then is rolled back between two blocks and
    reported as deferred, so the cut leaves a `capture-deferred` to record and no half-landed document.
    `record_time` (a rebuild from the record, architecture.md 10.2, OB-19): each document lands with
    the Writer clock at the time the record gives it, the `ts` of its event, and a transcript the
    `ts` of the last event of its session in the batch, so knowledge time comes from the record
    and not from the moment of the rebuild.
    `rerender_notes` (default False): an event note the store already holds for the same event in
    another rendering is kept as stored and counted in `kept_renderings`; with True it is rewritten
    in place, the owner's re-render job (`wiki recover --rerender-notes`, review B4 of the merge).
    `progress` (the capture runner, t-070 RC4): called as `progress(through_event=, done=, total=)`
    once before the first document and after each one, so a capture stopped from outside still names
    the last event note it landed and how many documents were left.
    `through` is the record head the capture covers (t-070 round 2 C-B, B-3): each note and
    transcript is rendered from record events at or below it (the session's operator,
    `session_operator`). A replay passes its cutoff; otherwise it is the head the capture reads. When
    the batch holds a session's first `session-start` and it declares an operator after the session
    already had events, the notes of those earlier events that the store holds in their rendering
    with no operator are re-rendered in place, and the session's transcript is landed again when its
    stored projection has no operator line (`_late_declarations`), so the live store gives what a
    rebuild at the same head gives; a note in an older rendering stays as it is (`kept_renderings`).
    `transcript_versions` maps a session to the `(source_bytes, source_sha256)` of the transcript
    version a record event names (a withdrawal the replay applies next, B-4): only that version is
    read, and when it is gone the transcript is reported missing with
    `transcript-version-unavailable` and nothing of it lands. A replay at a cutoff before the record
    head (t-430) maps a session to a list of `(path, source_bytes, source_sha256)` alternatives, the
    version the record names at the cutoff (`recovery._transcript_versions_at`: the newest
    `transcript-version` event, or best-effort a `transcript-snapshot`; `_versioned_doc`), or to
    False when the record says the store held none at the cutoff: then no transcript of it lands.
    `record_versions` (the live capture, `service.capture`, and `wiki recover`, never a replay that
    rebuilds or imports): each transcript version that lands, or that the hashgate skips after the
    session's newest recorded version, gets one `transcript-version` record event inside its
    landing's store write lock, just before the store commit (`_VersionLanding`), so a rebuild at a
    later cutoff reads exactly that version and the events follow the store's landing order (t-430).
    No record is opened, or created, when the root has none. `summary["versions_recorded"]` counts
    them.
    A document lands in this order (t-070 round 2 C-E, I-3, A-6): the landing decision under the
    write lock, which refuses a withdrawn source; then its vault bytes, written atomically
    (`Absorber.absorb_text(before_commit=...)`); then the store commit. A crash leaves at worst a
    vault file with no row, which the next landing rewrites with the same bytes, never a row without
    its file and never new bytes of a withdrawn source.
    """
    from alpaca import transcripts
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest import alpaca_sources, record_extract
    from alpaca.wiki.ingest.absorb import Absorber, DeadlineExceeded, LandingMoved

    cfg = Config.for_project(root)
    vault = str(cfg.vault_dir)
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    conn = tedb.connect_readonly(root)
    try:
        if through is None:
            through = record_head(conn)
        events = normalize_events(conn, events, through)
        prefixes = observed_prefixes(root)
        docs, gaps = [], []
        for event in events:
            if int(event.get("id") or 0) in skip_notes:
                continue
            if not renders_as_source(event.get("kind"), event.get("data")):
                continue
            doc_id, text = _note_doc(event, prefixes=prefixes)
            docs.append({"kind": "event", "doc_id": doc_id, "text": text,
                         "raw": _note_text(event, prefixes, redact=False)[1],
                         "session": event.get("session"), "event": int(event.get("id") or 0),
                         "origin_hash": event.get("hash")})
        for event in events:
            if not renders_as_source(event.get("kind"), event.get("data")):
                continue
            source = alpaca_sources.render(str(root), event)
            if isinstance(source, alpaca_sources.Gap):
                gaps.append({"event": source.event_id, "source_ref": source.source_ref,
                             "reason": source.reason})
            elif source is not None:
                docs.append({"kind": "source", "doc_id": source.doc_id, "text": source.text,
                             "raw": source.raw, "session": event.get("session"),
                             "event": source.event_id, "origin_hash": event.get("hash")})
        # a declaration that comes after the session's events (B-3, option b of dec-8c95ba97c4ac)
        declared = _late_declarations(conn, events, through)
        late_notes, late_sessions = [], []
        if declared:
            batch = {doc["event"]: doc for doc in docs if doc["kind"] == "event"}
            for session, decl in declared.items():
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM events WHERE session=? AND id<? ORDER BY id", (session, decl))]
                for event in normalize_events(conn, rows, through):
                    if not renders_as_source(event.get("kind"), event.get("data")):
                        continue
                    number = int(event.get("id") or 0)
                    bare = screen(_note_text(dict(event, _operator=None), prefixes)[1])
                    from alpaca.wiki.determinism import sha256_hex
                    if number in batch:
                        batch[number]["plain_sha"] = sha256_hex(bare)
                    elif number not in skip_notes:
                        doc_id, text = _note_doc(event, prefixes=prefixes)
                        late_notes.append({"kind": "event", "doc_id": doc_id, "text": text,
                                           "raw": _note_text(event, prefixes, redact=False)[1],
                                           "session": session, "event": number,
                                           "origin_hash": event.get("hash"),
                                           "plain_sha": sha256_hex(bare)})
                if session not in sessions:
                    late_sessions.append(session)
        operators, pending, missing = {}, deque(), []
        for session in dict.fromkeys(list(sessions) + late_sessions):
            if (transcript_versions or {}).get(session) is False:
                continue                     # the record names no version at the cutoff (t-430)
            override = (transcript_paths or {}).get(session)
            try:
                path = _transcript_path(str(root), session, conn, override)
            except TranscriptUnavailable as exc:
                if session in late_sessions:
                    continue                 # asked for by a late declaration only: not missing
                locator = override or transcripts.registered(conn, session)
                missing.append({"session": session, "source": locator or None, "error": str(exc)})
                continue
            if path:
                operators[session] = session_operator(conn, session, through)
                pending.append((session, path))
    finally:
        conn.close()
    late_pending = [entry for entry in pending if entry[0] in late_sessions]
    pending = deque(entry for entry in pending if entry[0] not in late_sessions)
    if not docs and not pending and not late_notes and not late_pending:
        # Nothing to land: no absorber is opened, so a replay whose transcripts are all current
        # (the collector's projection cache) loads no wiki index (main 3a2f32481/df6229ddf).
        return {"notes": 0, "ingested": 0, "skipped": 0, "docs": [], "transcripts": [],
                "through_event": max((event["id"] for event in events), default=0),
                "missing_transcripts": missing, "source_gaps": gaps, "quarantined": [],
                "stale_sources": [], "deferred": None, "rules": record_extract.new_report(),
                "kept_renderings": 0, "versions_recorded": 0}
    owns_absorber = absorber is None
    if owns_absorber:
        absorber = Absorber(cfg, **({"clock": clock} if clock is not None else {}))
    elif absorber.cfg.vault_dir.resolve() != cfg.vault_dir.resolve():
        raise ValueError("recovery absorber belongs to another vault")
    lent_abort = absorber.abort_at
    absorber.abort_at = hard_deadline
    lent_clock = absorber.writer.clock
    times = _record_times(events) if record_time else None
    summary = {"notes": 0, "ingested": 0, "skipped": 0, "docs": [], "transcripts": [],
               "through_event": max((event["id"] for event in events), default=0),
               "missing_transcripts": missing, "source_gaps": gaps, "quarantined": [],
               "stale_sources": [], "deferred": None, "rules": record_extract.new_report(),
               "kept_renderings": 0, "versions_recorded": 0}
    versions_conn = None
    try:
        # the late declaration's work, now that the store can be read: a note it re-renders is one
        # the store holds in its rendering with no operator; a transcript, one whose stored
        # projection has no operator line
        docs.extend(doc for doc in late_notes if _held_plain(absorber.db.conn, doc))
        for session, path in late_pending:
            if _transcript_without_operator(absorber.db.conn, session):
                pending.append((session, path))
        # Only active first blocks count. Old immutable files remain evidence, not
        # competing searchable versions of the same source.
        if projection_index is not None and "previous" in projection_index:
            # One inventory shared by the captures of one batch (the collector's snapshot jobs share
            # an absorber; main df6229ddf/532399468). The loop below keeps it current in place, and
            # the owner clears it when another connection commits to the store.
            previous = projection_index["previous"]
        else:
            previous = _previous_projections(
                absorber.db.conn, [doc["text"] for doc in docs], [session for session, _ in pending],
                full=projection_index is not None)
            if projection_index is not None:
                projection_index["previous"] = previous
        recorded = None
        total = len(docs) + len(pending)
        done = 0
        last_event = 0
        queue = deque(docs)

        def report():
            if progress is not None:
                progress(through_event=last_event, done=done, total=total)
        report()
        while queue or pending:
            if done:
                report()
            if deadline is not None and done >= min_documents and time.monotonic() >= deadline:
                summary["deferred"] = {"through_event": last_event, "remaining": total - done}
                break
            if queue:
                doc = queue.popleft()
            else:
                if hard_deadline is not None and (
                        time.monotonic() + transcript_cost_s(pending[0][1]) > hard_deadline):
                    summary["deferred"] = {"through_event": last_event, "remaining": total - done}
                    break                        # it cannot land before the cut: defer it whole, now
                session, path = pending.popleft()
                changed = []
                try:
                    doc_id, text = _versioned_doc(
                        str(root), session, path, operators.get(session), changed,
                        (transcript_versions or {}).get(session))
                except OSError as exc:
                    entry = {"session": session, "source": path,
                             "error": "%s: %s" % (type(exc).__name__, exc)}
                    if isinstance(exc, TranscriptVersionUnavailable):
                        entry["reason"] = exc.reason
                    elif isinstance(exc, TranscriptMalformed):
                        entry["malformed"] = True        # there and readable, but torn (t-684)
                    missing.append(entry)
                    done += 1
                    continue
                # the marking scanner reads the projection with each value the redaction changed
                # appended as it was read (`_visible_transcript`)
                doc = {"kind": "transcript", "doc_id": doc_id, "text": text,
                       "raw": "\n\n".join([text] + changed) if changed else None,
                       "session": session, "event": None,
                       "origin_hash": _header_value(text, "source_sha256")}
            doc_id, text = doc["doc_id"], doc["text"]
            identity = projection_identity(text)
            if doc["kind"] == "source" and any(
                    n is not None and n > doc["event"] for _, n in previous.get(identity, ())):
                summary["stale_sources"].append(doc_id)        # a newer version already landed
                done += 1
                continue
            # the marking scanner reads the document before the screen (`note_placement`)
            unscreened = doc.get("raw")
            decided_at = absorber.data_version()       # the landing below is decided at this version
            target, rule_id = placement(doc_id, text if unscreened is None else unscreened)
            quarantined = bool(rule_id) and target != doc_id
            # Sticky private classification (main 3a2f32481) and the `-render-<sha>` revision of a
            # note whose id another event held (main cea450f95), recorded under the doc_id the note
            # lands at.
            doc_id = _landed_target(absorber.db.conn, vault, doc_id, target, rule_id, text, previous,
                                    doc.get("origin_hash"))
            waive = None
            plain = doc.get("plain_sha")
            if doc["kind"] == "event" and _rendered_differently(absorber.db.conn, doc_id, text):
                # The store holds this event's note at doc_id in another rendering (a note format
                # change, the secret screen): `_landed_target` sent another event's note elsewhere.
                # By default the stored note stays as it is, untouched and counted in
                # `kept_renderings`: re-rendering a live store's notes is the owner's job
                # (`wiki recover --rerender-notes`), never a side effect of a capture, a recover or
                # a --refresh (merge of op-008 into main, review B4: about 109,000 live notes).
                # With `rerender_notes` the note is rewritten in place and the absorber treats a
                # smaller body as a change of format (the shrink guard waived for it, as op-008 did).
                # A note the store holds in its rendering with no operator, whose session declared
                # one late, is re-rendered in place (`_late_declarations`).
                if not rerender_notes and not _holds_sha(absorber.db.conn, doc_id, plain):
                    summary["kept_renderings"] += 1
                    summary["notes"] += 1
                    summary["skipped"] += 1
                    summary["docs"].append(doc_id)
                    last_event = max(last_event, doc["event"])
                    done += 1
                    continue
                waive = RERENDER_WAIVER
            if quarantined:
                if recorded is None:
                    recorded = _recorded_quarantines(root)
                _record_quarantine(root, doc["session"], doc_id, rule_id, recorded)
                summary["quarantined"].append({"doc_id": doc_id, "rule_id": rule_id,
                                               "session": doc["session"]})
            fpath = os.path.join(vault, doc_id)
            if times is not None:
                stamp = times["event"].get(doc["event"]) or times["session"].get(doc["session"])
                absorber.writer.clock = (lambda: stamp) if stamp else lent_clock

            def landing(conn, landed=doc_id, rendered=doc["doc_id"], target=target, rule_id=rule_id,
                        text=text, identity=identity, origin=doc.get("origin_hash"), kind=doc["kind"],
                        plain=plain):
                """The landing decision above, asked again under the write lock (t-070 RC1): the
                document still lands at `landed` (the event identity of `_same_event`, the sticky
                private partition), and a note of the same event in another rendering is still not
                there to keep (`_rendered_differently`)."""
                where = _landed_target(conn, vault, rendered, target, rule_id, text,
                                       {identity: previous.get(identity, [])}, origin, pin=False)
                return where == landed and not (
                    kind == "event" and not rerender_notes and not _holds_sha(conn, landed, plain)
                    and _rendered_differently(conn, landed, text))

            # t-430: a transcript version is recorded inside the store's write critical section of
            # its landing (`absorb_text` `under_lock` and `before_commit`), so the record's version
            # events of a session follow the store's own landing order, and `held_before` is what the
            # store held under that lock (review T-1, T-3)
            versioned = None
            if record_versions and (identity or "").startswith("transcript:"):
                if versions_conn is None and os.path.isfile(paths.db_path(root)):
                    versions_conn = tedb.connect(root)       # never creates a record (review T-4)
                if versions_conn is not None:
                    versioned = _VersionLanding(versions_conn, doc["session"], identity, text,
                                                through, record_time)

            from alpaca.wiki import faults
            faults.boundary("drain-land")
            try:
                with _projection_target(vault, doc_id) as parent_fd:
                    def publish(conn, fpath=fpath, text=text, parent_fd=parent_fd,
                                versioned=versioned):
                        """The vault bytes, after the landing decision and before the store commit
                        (t-070 round 2 C-E): never written for a withdrawn source. Then the
                        transcript version's record event, still under the store's write lock."""
                        _write_if_changed(fpath, text, parent_fd=parent_fd)
                        faults.boundary("drain-vault-written")
                        if versioned is not None:
                            versioned.record()
                    result = absorber.absorb_text(doc_id, text, kind="raw", path=doc_id,
                                                  origin_hash=doc.get("origin_hash"),
                                                  unscreened=unscreened, waive_reason=waive,
                                                  landing=landing, landing_version=decided_at,
                                                  before_commit=publish,
                                                  under_lock=None if versioned is None
                                                  else versioned.read)
            except DeadlineExceeded:
                summary["deferred"] = {"through_event": last_event, "remaining": total - done}
                break
            except LandingMoved:
                # another connection changed what is stored at the id between the decision and the
                # lock: decide again (bounded; the store only moves forward)
                doc["moved"] = doc.get("moved", 0) + 1
                if doc["moved"] > 3:
                    raise
                queue.appendleft(doc)
                continue
            old = [item for item, _ in previous.get(identity, []) if item != doc_id]
            if old:
                absorber.retire_documents(old, reason="source-projection-replaced", superseded_by=doc_id)
                previous[identity] = [entry for entry in previous[identity] if entry[0] not in old]
            if identity:
                previous.setdefault(identity, []).append((doc_id, _event_no(text)))
            if (identity or "").startswith("transcript:"):
                from alpaca.wiki.determinism import sha256_hex
                meta = header_fields(text)
                summary["transcripts"].append({
                    "doc_id": doc_id, "content_sha256": sha256_hex(text),
                    "session": meta["session"], "source": meta["source"],
                    "source_sha256": meta["source_sha256"], "source_bytes": int(meta["source_bytes"]),
                    # a withdrawn source lands nothing and writes no vault bytes (C-E, A-6)
                    "excluded": result.get("reason") == "excluded",
                })
                if versioned is not None and versioned.appended:
                    summary["versions_recorded"] += 1
            if doc["kind"] == "event":
                last_event = max(last_event, doc["event"])
            record_extract.add_report(summary["rules"], result.get("rules"))
            summary["notes"] += 1
            summary["docs"].append(doc_id)
            summary["skipped" if result.get("skipped") else "ingested"] += 1
            done += 1
    finally:
        if versions_conn is not None:
            versions_conn.close()
        absorber.abort_at = lent_abort
        absorber.writer.clock = lent_clock
        if owns_absorber:
            absorber.close()
    return summary


class _VersionLanding:
    """One transcript landing's `transcript-version` event (t-430), in two steps of the store's
    write critical section (`Absorber.absorb_text`): `read` first, under the lock and before any
    write, reads what the store held of the source (`held_before`: a `current` version;
    `ingested`: the current version is not this text); `record`, in `before_commit`, appends the
    event (`_record_transcript_version`). Because both run while the landing holds the store's
    write lock, two captures of one session record their versions in the order the store landed
    them (review T-1), and `held_before` is the store's state at the landing, not at the capture's
    start (T-3). A rebuild (`record_time`) records `held_before` None: an empty store cannot say what
    the store it replaces held. The version is read from the rendered text's header, as the file
    was read: no extra file read."""

    def __init__(self, conn, session, identity, text, through, record_time):
        from alpaca.wiki.determinism import sha256_hex
        meta = header_fields(text)
        self.conn, self.session, self.identity = conn, session, identity
        self.landed = {"content_sha256": sha256_hex(text), "source_bytes": int(meta["source_bytes"]),
                       "source_sha256": meta["source_sha256"]}
        self.through, self.record_time = through, record_time
        self.held_before, self.ingested, self.appended = None, True, False

    def read(self, store_conn):
        row = store_conn.execute("SELECT content_sha256 FROM source_versions WHERE source_ref=? "
                                 "AND status='current' ORDER BY version_seq DESC LIMIT 1",
                                 (self.identity,)).fetchone()
        self.held_before = row is not None
        self.ingested = row is None or row[0] != self.landed["content_sha256"]
        self.appended = False

    def record(self):
        self.appended = _record_transcript_version(
            self.conn, self.session, self.landed, self.through,
            None if self.record_time else self.held_before, ingested=self.ingested)


def _record_transcript_version(conn, session, landed: dict, through, held_before, *,
                               ingested: bool) -> bool:
    """Append the `transcript-version` event of a transcript version a capture landed (t-430,
    dec-f812470497fb), unless the session's newest such event already names it; returns True when
    it appended one. `landed` is the capture's summary entry (its `content_sha256`, `source_bytes`
    and `source_sha256`, computed as the file was read: no extra read); `through` the record head
    the capture rendered from; `held_before` whether the store held a version of the transcript
    before this landing (None: unknown, a rebuild from an empty store). A landing the hashgate
    skipped (`ingested` False) is recorded only after an earlier event of the session: a session
    captured before t-430 is not recorded until it lands a new version, so it stays best-effort
    (no burst of events for old sessions on a `wiki recover`). The check and the append are one
    record transaction, so captures racing on one version leave one event; the caller holds the
    store's write lock of the landing (`_VersionLanding`), so versions are recorded in the store's
    landing order. The data is hashes,
    byte counts and ids, and goes through the one secret screen like every record payload."""
    ref = "transcript:" + str(session)
    version = {"content_sha256": str(landed["content_sha256"]),
               "source_bytes": int(landed["source_bytes"]),
               "source_sha256": str(landed["source_sha256"])}
    with tedb.transaction(conn):
        row = conn.execute("SELECT data FROM events WHERE ref=? AND kind=? ORDER BY id DESC LIMIT 1",
                           (ref, TRANSCRIPT_VERSION_KIND)).fetchone()
        if row is None and not ingested:
            return False
        if row is not None:
            try:
                last = json.loads(row[0])
            except (TypeError, ValueError):
                last = {}
            if isinstance(last, dict) and all(last.get(k) == v for k, v in version.items()):
                return False
        data = {key: (str(screen(value)) if isinstance(value, str) else value)
                for key, value in dict(version, capture_head=int(through or 0),
                                       held_before=held_before).items()}
        tedb.append_event(conn, session=TRANSCRIPT_VERSION_WRITER, actor="alpaca-wiki",
                          kind=TRANSCRIPT_VERSION_KIND, ref=ref, data=data, conn_in_txn=True)
    return True


def _late_declarations(conn, events, through) -> dict:
    """`{session: event id}` of each `session-start` in `events` that is its session's first at or
    below `through`, declares an operator, and comes after an earlier event of the same session: a
    declaration made late, after the session already wrote (FR-6; t-070 round 2 C-B, B-3)."""
    out = {}
    for event in events:
        if event.get("kind") != "session-start" or not event.get("session"):
            continue
        data = event.get("data") or {}
        if not (isinstance(data, dict) and data.get("operator")):
            continue
        session, number = event["session"], int(event.get("id") or 0)
        first = conn.execute("SELECT MIN(id) FROM events WHERE session=? AND kind='session-start' "
                             "AND id<=?", (session, through)).fetchone()[0]
        if first != number:
            continue
        if conn.execute("SELECT 1 FROM events WHERE session=? AND id<? LIMIT 1",
                        (session, number)).fetchone():
            out[session] = number
    return out


def _holds_sha(conn, doc_id, sha) -> bool:
    """Whether the store holds the document `doc_id` with content sha256 `sha` (False for no sha)."""
    if not sha:
        return False
    row = conn.execute("SELECT content_sha256 FROM docs WHERE doc_id=?", (doc_id,)).fetchone()
    return row is not None and row[0] == sha


def _held_plain(conn, doc) -> bool:
    """Whether the store holds a late-declared session's earlier note (`capture`) in its rendering
    with no operator, at its own id or under the private partition."""
    from alpaca.wiki.engine.compartment import PRIVATE_PARTITION
    return any(_holds_sha(conn, doc_id, doc["plain_sha"])
               for doc_id in (doc["doc_id"], PRIVATE_PARTITION + "/" + doc["doc_id"]))


def _transcript_without_operator(conn, session) -> bool:
    """Whether the store holds an active projection of `session`'s transcript with no `operator:`
    line in its header."""
    for entries in _previous_projections(conn, [], [session]).values():
        for doc_id, _ in entries:
            row = conn.execute("SELECT text FROM blocks WHERE doc_id=? AND ordinal=0 AND "
                               "status='active'", (doc_id,)).fetchone()
            if row is not None and _header_value(row[0], "operator") is None:
                return True
    return False


def _sticky_private(conn, doc_id, previous) -> bool:
    """True when the store (`conn`) already holds this source as private: the document at `doc_id`
    was marked private by a scan (`private` and `privacy_scanned`), or an earlier projection of the
    same source (`previous`, `(doc_id, event number)` pairs) lives under the private partition. The
    drain then keeps the new rendering private too (main 3a2f32481), before the write door sees it."""
    from alpaca.wiki.engine.compartment import is_private_path
    prior = conn.execute(
        "SELECT private,privacy_scanned FROM docs WHERE doc_id=?", (doc_id,)).fetchone()
    if prior is not None and prior["private"] and prior["privacy_scanned"]:
        return True
    return any(is_private_path(old) for old, _ in previous)


def _same_event(conn, doc_id, text, origin_hash=None, vault=None) -> bool:
    """Whether the note the store (`conn`) holds at `doc_id` was rendered from the same record event
    as `text`, so that the two differ only by renderer (a format change, the secret screen), rather
    than from another event that took the same id (the 2026-09-30 record restore reused ids for
    other events). The record row hash the stored note was made from decides when both are known
    (`source_versions.origin_hash`, written since schema 4). Else the stored note's own header, its
    `event:`, `kind:` and `ts:` lines (every note format has them), must match the new rendering's,
    and so must its body (`_same_body`): a new event at a reused id with the same kind in the same
    second is another event, and its note lands at its `-render-` revision (merge review m8, t-070).
    On the live store this keeps every one of the 114,297 notes without an origin hash whose header
    matches (`.alpaca/proofs/op-008/t-070.logs/rc13/m8-live-probe.log`)."""
    import sqlite3
    if origin_hash:
        try:
            row = conn.execute(
                "SELECT v.origin_hash FROM source_versions v JOIN docs d ON d.doc_id=v.doc_id "
                "AND d.content_sha256=v.content_sha256 WHERE v.doc_id=? AND v.origin_hash IS NOT NULL "
                "ORDER BY v.version_seq DESC LIMIT 1", (doc_id,)).fetchone()
        except sqlite3.OperationalError:              # a store from before source_versions
            row = None
        if row is not None:
            return row[0] == origin_hash
    stored = conn.execute(
        "SELECT text FROM blocks WHERE doc_id=? AND ordinal=0 "
        "ORDER BY (status='active') DESC, recorded_at DESC LIMIT 1", (doc_id,)).fetchone()
    if stored is None:
        return False
    if not all(_header_value(text, key) is not None
               and _header_value(stored[0], key) == _header_value(text, key)
               for key in ("event", "kind", "ts")):
        return False
    held = _stored_text(conn, doc_id, vault)
    return held is None or _same_body(held, text)


def _stored_text(conn, doc_id, vault=None) -> str | None:
    """The text of the note the store holds at `doc_id`: its vault file when that file hashes to the
    document's stored `content_sha256` (the bytes the store took), else its active blocks in order
    joined as `segment` split them. None when neither is there (a retired note whose file is gone):
    the header alone then decides, as before t-070."""
    from alpaca.wiki.determinism import sha256_hex
    held = conn.execute("SELECT content_sha256 FROM docs WHERE doc_id=?", (doc_id,)).fetchone()
    if vault is not None and held is not None:
        try:
            raw = Path(vault, doc_id).read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            raw = None
        if raw is not None and sha256_hex(raw) == held[0]:
            return raw
    rows = conn.execute("SELECT text FROM blocks WHERE doc_id=? AND status='active' ORDER BY ordinal",
                        (doc_id,)).fetchall()
    return "\n\n".join(r[0] for r in rows) if rows else None


def _same_body(stored: str, text: str) -> bool:
    """Whether a stored note and a new rendering, whose headers name the same event, kind and second,
    carry one event's body. Both bodies go through the screen with their whitespace folded (as
    `segment` keeps it); then they are equal, or one is the other extended at a field or line
    boundary, either the summary line alone (the rest equal) or the whole body: an older renderer
    listed fewer fields (the 16 such live notes of the m8 probe) or wrote more metadata lines (the
    longer legacy note of `test_drain.py`). Another event's body is not an extension of this one's,
    unless one of the two has no field at all and the other only adds fields after the same kind and
    session (a recorded limit: then the note is kept, as before t-070)."""
    def body(value):
        parts = value.split("\n\n", 1)
        return " ".join(screen(parts[1]).split()) if len(parts) == 2 else ""

    def extends(a, b):
        short, long = sorted((a, b), key=len)
        return long.startswith(short + ". ") or long.startswith(short + " ")

    old, new = body(stored), body(text)
    if old == new:
        return True
    old_line, _, old_rest = old.partition(" Untrusted observation data:")
    new_line, _, new_rest = new.partition(" Untrusted observation data:")
    return (old_rest == new_rest and extends(old_line, new_line)) or extends(old, new)


#: The shrink-guard waiver of an event note the owner re-renders in place (`rerender_notes`).
RERENDER_WAIVER = "captured-note-rerender"


def _rendered_differently(conn, doc_id, text) -> bool:
    """Whether the store (`conn`) holds a document at `doc_id` whose content is not `text` as the
    write door would store it (screened). For an event note at the doc_id `_landed_target` gave,
    that is the same event's note in another rendering: a capture keeps it unless the owner asked
    for the re-render (`capture(rerender_notes=True)`)."""
    from alpaca.wiki.determinism import sha256_hex
    held = conn.execute("SELECT content_sha256 FROM docs WHERE doc_id=?", (doc_id,)).fetchone()
    if held is None or held[0] == sha256_hex(text):
        return False
    return held[0] != sha256_hex(screen(text))


def _revision_target(conn, vault, doc_id, text, origin_hash=None, *, pin: bool = True) -> str:
    """Where an event note lands. When the store (`conn`) holds another event's note at `doc_id`
    (a record id reused after the 2026-09-30 record restore; main cea450f95, decision D3 of the
    op-008 merge), the new rendering goes to `<stem>-render-<sha256>.md` beside it, so the other
    event's note stays as it was (it is retired from search as a replaced projection, never
    overwritten) and the shrink guard is never waived for it. A new rendering of the same event (a
    renderer revision, the secret screen) keeps the doc_id: `capture` leaves the stored note as it
    is unless the owner re-renders (`rerender_notes`), and then it lands in place, where its facts
    follow it to the new blocks (OB-14, OB-17) and the shrink guard is waived as a format change.
    With `pin` the old path is pinned first, so a symlink or special file put in its place still
    refuses the capture rather than being stepped around by the new name; a reader that only
    looks for the note (recovery) passes False."""
    from alpaca.wiki.determinism import sha256_hex
    digest = sha256_hex(text)
    held = conn.execute("SELECT content_sha256 FROM docs WHERE doc_id=?", (doc_id,)).fetchone()
    if held is None or held["content_sha256"] == digest or _same_event(conn, doc_id, text, origin_hash,
                                                                        vault=vault):
        return doc_id
    if pin:
        with _projection_target(vault, doc_id):
            pass
    name = Path(doc_id)
    return (name.parent / (name.stem + "-render-" + digest + name.suffix)).as_posix()


def _landed_target(conn, vault, doc_id, target, rule_id, text, previous, origin_hash=None, *,
                   pin: bool = True) -> str:
    """The doc_id a document rendered at `doc_id` lands at in the store `conn`, after the marking
    scanner placed it at `target` (`placement`, rule_id None when nothing tripped): under the
    private partition when the store holds its source as private (`_sticky_private`), and, for an
    event note, at its `-render-<sha>` revision when the store holds another event's note at that
    path (`_revision_target`; `origin_hash` is the record row hash the note is made from). `previous`
    is the inventory of `_previous_projections`. `capture` lands by
    this rule and `stored_note` finds a note by it, so recovery verifies where capture wrote."""
    from alpaca.wiki.engine.compartment import PRIVATE_PARTITION, is_private_path
    identity = projection_identity(text)
    if not rule_id and not is_private_path(doc_id) and _sticky_private(
            conn, doc_id, previous.get(identity, ())):
        # No rule tripped, so no `capture-quarantined` is recorded; privacy is added, never
        # taken away.
        target = PRIVATE_PARTITION + "/" + doc_id
    if (identity or "").startswith("event:"):
        target = _revision_target(conn, vault, target, text, origin_hash, pin=pin)
    return target


def stored_note(conn, vault, ev: dict, prefixes=(), previous=None, rerender_notes=False):
    """(doc_id, stored text) of one event note as capture landed it in the store `conn`: placed by
    the marking scanner (`note_placement`), then by `_landed_target`. `previous` is a full
    `_previous_projections` inventory of the store, read once by a caller that places many notes
    (`recovery.verify_events`). A note capture kept in an earlier rendering (`rerender_notes`
    False, `capture`) is the text of its vault file, so recovery verifies that the file, the
    document and its blocks agree; an unreadable file gives the new rendering, which then fails
    verification."""
    doc_id, text = _note_doc(ev, prefixes)
    target, rule_id = placement(doc_id, _note_text(ev, prefixes, redact=False)[1])
    if previous is None:
        previous = _previous_projections(conn, [text], [], full=True)
    landed = _landed_target(conn, vault, doc_id, target, rule_id, text, previous, ev.get("hash"),
                            pin=False)
    if not rerender_notes and _rendered_differently(conn, landed, text):
        try:
            return landed, Path(vault, landed).read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return landed, text
    return landed, text


def _record_times(events) -> dict:
    """The record time of each document a capture of `events` lands (`capture(record_time=True)`):
    an event note or Alpaca source at its event's `ts`, a transcript at the latest `ts` among the
    events of its session, in the store clock's format. An unreadable `ts` gives no time, and the
    document then lands at the capture's own clock."""
    from alpaca.wiki.clock import to_clock_format
    out = {"event": {}, "session": {}}
    for event in events:
        stamp = to_clock_format(event.get("ts"))
        if stamp is None:
            continue
        out["event"][int(event.get("id") or 0)] = stamp
        session = event.get("session")
        if stamp > out["session"].get(session, ""):
            out["session"][session] = stamp
    return out


@contextmanager
def _projection_target(vault, doc_id):
    """Pin the destination before ingest; reject symlinks in every path component."""
    from alpaca.wiki.store.db import open_vault_directory
    key = Path(doc_id)
    if key.is_absolute() or ".." in key.parts or not key.parts:
        raise ValueError("invalid projection path")
    _, directory = open_vault_directory(Path(vault))
    try:
        for component in key.parts[:-1]:
            try:
                os.mkdir(component, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        try:
            info = os.stat(key.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("non-regular projection file refused")
        yield directory
    finally:
        os.close(directory)


def _write_if_changed(path: str, text: str, *, parent_fd=None) -> bool:
    """Atomically publish accepted bytes through a pinned no-follow directory."""
    if parent_fd is None:
        with _projection_target(str(Path(path).parent), Path(path).name) as directory:
            return _write_if_changed(path, text, parent_fd=directory)
    data = text.encode("utf-8")
    name = Path(path).name
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    except FileNotFoundError:
        pass
    else:
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("non-regular projection file refused")
            if stream.read() == data:
                return False
    import uuid
    temporary = ".capture-" + uuid.uuid4().hex
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent_fd)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
    return True


def run(root: str, session: str, *, clock=None, transcript_path: str | None = None,
        through_event: int | None = None, absorber=None, deadline=None,
        record_time: bool = False, rerender_notes: bool = False, transcript_version=None,
        record_versions: bool = False) -> dict:
    """Land one session's events and transcript into the wiki, idempotently.

    Returns a summary: how many notes were landed, how many were freshly ingested versus skipped by
    the hashgate, and the doc ids. A second call over the same session ingests nothing new. A
    transcript that is missing is listed in `missing_transcripts`; the event notes still land.
    Recovery may lend an absorber to reuse its index across sessions; the caller owns it, and a
    rebuild passes `record_time` (`capture`); the owner's re-render passes `rerender_notes`; a
    replay at a cutoff before the record head passes the `transcript_version` the record names
    there (`capture` `transcript_versions`, t-430); `wiki recover` passes `record_versions`.
    """
    conn = tedb.connect_readonly(root)
    try:
        query = "SELECT * FROM events WHERE session=?"
        params = [session]
        if through_event is not None:
            query += " AND id<=?"
            params.append(through_event)
        events = [dict(row) for row in conn.execute(query + " ORDER BY id", params)]
        # the record head this capture covers, read with its events (B-3)
        through = through_event if through_event is not None else record_head(conn)
    finally:
        conn.close()
    summary = capture(root, events, sessions=[session], clock=clock,
                      transcript_paths={session: transcript_path}, absorber=absorber,
                      deadline=deadline, record_time=record_time, rerender_notes=rerender_notes,
                      through=through, record_versions=record_versions,
                      transcript_versions=None if transcript_version is None else {session: transcript_version})
    summary["session"] = session
    return summary
