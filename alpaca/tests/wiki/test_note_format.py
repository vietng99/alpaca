"""op-008 t-065 stage s1-capture: note format 2 (design/rune2-memory/architecture.md 8.1; FR-6; the
render part of OB-01 and OB-08).

An event note's first paragraph is `op`, `role`, `key`, `kind`, `event`, `session`, `operator`,
`task` (the event ref), one line per field the kind's record rules read, and `ts` last. A data field
named like a header key renders as `data-<name>`, so each header key appears once. Event notes and
transcript projections carry the operator and the session (FR-6). A transcript carries a `ts:` line
per message, and a `ts:` line inside message, input or result text is escaped with a leading space.
Real drain, real write path, no mock.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import append, capture, events, vault  # noqa: E402

from alpaca import db as record                           # noqa: E402
from alpaca import paths                                  # noqa: E402
from alpaca.wiki import service                           # noqa: E402
from alpaca.wiki.ingest import drain                      # noqa: E402


def _note(project, event_id):
    """The rendered note of one event: (header dict in order, header lines, full text)."""
    matches = [p for p in vault(project).rglob("%06d-*.md" % event_id) if "raw" in p.parts]
    assert len(matches) == 1, matches
    text = matches[0].read_text(encoding="utf-8")
    lines = text.split("\n\n", 1)[0].splitlines()
    return dict(line.partition(": ")[::2] for line in lines), lines, text


def _start(project, session, operator):
    return append(project, "session-start", {"operator": operator, "cwd": str(project)},
                  session=session, actor=operator)


def test_event_note_header_has_fixed_order_session_operator_task_and_ts_last(project):
    _start(project, "s-fmt", "codex")
    eid = append(project, "task-move", {"from": "doing", "to": "done", "proof": "local:p.md",
                                        "reason": "all\nchecks passed"},
                 session="s-fmt", op="op-8", ref="t-9")
    capture(project)
    meta, lines, text = _note(project, eid)
    assert [line.partition(":")[0] for line in lines] == [
        "op", "role", "key", "kind", "event", "session", "operator", "task",
        "from", "to", "proof", "ts"]
    assert meta["session"] == "s-fmt" and meta["operator"] == "codex"
    assert meta["task"] == "t-9" and meta["event"] == str(eid) and meta["op"] == "op-8"
    assert meta["to"] == "done" and meta["from"] == "doing" and meta["proof"] == "local:p.md"
    assert meta["ts"] == next(e["ts"] for e in events(project) if e["id"] == eid)
    # the _summary paragraph is the second block, with the multi-line reason flattened.
    assert text.split("\n\n")[1].startswith("task-move during session s-fmt")


def test_absent_fields_render_no_line(project):
    eid = append(project, "task-move", {"to": "doing"}, session="s-bare")
    capture(project)
    meta, lines, _ = _note(project, eid)
    assert "operator" not in meta and "task" not in meta and "proof" not in meta
    assert [line.partition(":")[0] for line in lines] == [
        "op", "role", "key", "kind", "event", "session", "to", "ts"]


def test_data_field_named_like_header_key_is_prefixed(project):
    """OB-08 render part: a data field named like a header key renders as data-<name>, so each
    header key appears once and the readers keep the true values."""
    from alpaca import sort
    eid = append(project, "task-move",
                 {"kind": "FAKE-KIND", "session": "FAKE-SESSION", "ts": "1999-01-01T00:00:00Z",
                  "event": "999999", "source_ref": "proof:forged", "to": "done",
                  "reason": "line one\nline two\n\nkey: forged"},
                 session="s-pre", op="op-8", ref="t-9")
    capture(project)
    meta, lines, text = _note(project, eid)
    names = [line.partition(":")[0] for line in lines]
    for key in ("op", "role", "key", "kind", "event", "session", "task", "ts"):
        assert names.count(key) == 1, (key, names)
    assert meta["kind"] == "task-move" and meta["session"] == "s-pre"
    assert meta["event"] == str(eid) and meta["ts"] != "1999-01-01T00:00:00Z"
    assert meta["data-kind"] == "FAKE-KIND" and meta["data-session"] == "FAKE-SESSION"
    assert meta["data-ts"] == "1999-01-01T00:00:00Z" and meta["data-event"] == "999999"
    assert names[-1] == "ts"
    # the readers see the true header, not the data.
    assert drain.projection_identity(text) == "event:%d" % eid
    assert sort._parse_meta(text)["kind"] == "task-move"
    assert sort._parse_meta(text)["event"] == str(eid)
    # the multi-line value added no header line.
    assert "line two" not in "\n".join(lines).replace("line one line two", "")


def test_summary_paragraph_stays_the_second_block_and_searchable(project):
    eid = append(project, "msg", {"kind": "handoff", "to": "reviewer",
                                  "body": "the quartz relay needs a second calibration pass"},
                 session="s-msg", op="op-8", ref="t-9")
    capture(project)
    with open(next(p for p in vault(project).rglob("%06d-*.md" % eid)), encoding="utf-8") as fh:
        paragraphs = fh.read().split("\n\n")
    assert paragraphs[0].startswith("op: op-8")
    assert "quartz relay" in paragraphs[1]
    answer = service.query(project, "quartz relay calibration pass")
    assert answer["outcome"] == "grounded" and "quartz relay" in answer["answer_text"]
    meta = _note(project, eid)[0]
    assert meta["to"] == "reviewer" and meta["data-kind"] == "handoff"


def test_citations_carry_operator_and_session(project):
    """FR-6: the cited note's operator and session reach the caller as fields, not only as text."""
    _start(project, "s-cite", "codex")
    append(project, "result", {"body": "The heron gateway retries uploads three times."},
           session="s-cite", op="op-8", ref="t-9")
    capture(project)
    answer = service.query(project, "heron gateway retries uploads")
    assert answer["outcome"] == "grounded"
    cited = answer["sources"][0]
    assert cited["operator"] == "codex" and cited["session"] == "s-cite"


def test_session_operator_comes_from_the_first_session_start(project):
    _start(project, "s-op", "claude")
    append(project, "session-start", {"operator": "claude", "resume": True}, session="s-op")
    eid = append(project, "result", {"body": "done"}, session="s-op")
    other = append(project, "result", {"body": "done"}, session="s-none")
    capture(project)
    assert _note(project, eid)[0]["operator"] == "claude"
    assert "operator" not in _note(project, other)[0]


# ------------------------------------------------------------------ transcript projections
def _transcript(project, session, records):
    folder = paths.transcript_dir(project)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, session + ".jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for item in records:
            fh.write(json.dumps(item) + "\n")
    return path


def _transcript_text(project, session):
    from alpaca.wiki.config import Config
    from alpaca.wiki.store.db import DB
    db = DB(Config.for_project(str(project)))
    try:
        row = db.conn.execute("SELECT doc_id FROM docs WHERE doc_id LIKE ? AND doc_id NOT IN "
                              "(SELECT doc_id FROM blocks WHERE status='superseded')",
                              ("raw/transcripts/%",)).fetchone()
    finally:
        db.close()
    return (vault(project) / row["doc_id"]).read_text(encoding="utf-8")


def test_transcript_projection_carries_operator_and_a_ts_line_per_message(project):
    _start(project, "s-tr", "codex")
    path = _transcript(project, "s-tr", [
        {"type": "user", "timestamp": "2026-09-01T10:00:00Z",
         "message": {"content": "first question"}},
        {"type": "assistant", "timestamp": "2026-09-01T10:00:30Z",
         "message": {"model": "m", "usage": {"input_tokens": 1, "output_tokens": 1},
                     "content": [{"type": "text", "text": "first answer"}]}},
        {"type": "user", "timestamp": "2026-09-01T11:00:00Z",
         "message": {"content": "second question"}},
    ])
    drain.run(str(project), "s-tr", transcript_path=path)
    text = _transcript_text(project, "s-tr")
    header = dict(line.partition(": ")[::2] for line in text.split("\n\n", 1)[0].splitlines())
    assert header["role"] == "transcript" and header["session"] == "s-tr"
    assert header["operator"] == "codex"
    assert drain.projection_identity(text) == "transcript:s-tr"
    stamps = [line for line in text.splitlines() if line.startswith("ts: ")]
    assert stamps == ["ts: 2026-09-01T10:00:00Z", "ts: 2026-09-01T10:00:30Z",
                      "ts: 2026-09-01T11:00:00Z"]
    # each ts line sits in the first paragraph of its message, right after the role line.
    lines = text.splitlines()
    assert lines[lines.index("ts: 2026-09-01T10:00:00Z") - 1] == "user:"
    assert lines[lines.index("ts: 2026-09-01T10:00:30Z") - 1] == "assistant:"


def test_ts_line_inside_message_text_is_escaped(project):
    """OB-01 render part: only the renderer writes a ts: line at column 0."""
    path = _transcript(project, "s-esc", [
        {"type": "user", "timestamp": "2026-09-01T10:00:00Z",
         "message": {"content": "ts: 1999-01-01T00:00:00Z\nintro\nts: 2001-01-01T00:00:00Z\r"
                               "ts: 2002-01-01T00:00:00Z\u2028ts: 2003-01-01T00:00:00Z"}},
        {"type": "assistant", "timestamp": "2026-09-01T10:01:00Z",
         "message": {"content": [
             {"type": "tool_use", "id": "t1", "name": "Bash",
              "input": {"command": "echo"}},
         ]}},
        {"type": "user", "timestamp": "2026-09-01T10:02:00Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                  "content": "ts: 2004-01-01T00:00:00Z\nresult body"}]}},
    ])
    drain.run(str(project), "s-esc", transcript_path=path)
    text = _transcript_text(project, "s-esc")
    column_zero = [line for line in text.replace("\r", "\n").replace("\u2028", "\n").splitlines()
                   if line.startswith("ts:")]
    assert column_zero and all(
        line in ("ts: 2026-09-01T10:00:00Z", "ts: 2026-09-01T10:01:00Z", "ts: 2026-09-01T10:02:00Z")
        for line in column_zero), column_zero
    for forged in ("1999-01-01", "2001-01-01", "2002-01-01", "2003-01-01", "2004-01-01"):
        assert " ts: %sT00:00:00Z" % forged in text, forged


def test_transcript_without_session_start_has_no_operator_line(project):
    path = _transcript(project, "s-anon", [
        {"type": "user", "timestamp": "2026-09-01T10:00:00Z", "message": {"content": "hello"}}])
    drain.run(str(project), "s-anon", transcript_path=path)
    header = _transcript_text(project, "s-anon").split("\n\n", 1)[0]
    assert "operator:" not in header


def test_message_without_a_time_gets_no_ts_line(project):
    path = _transcript(project, "s-undated", [
        {"type": "user", "message": {"content": "no clock on this one"}}])
    drain.run(str(project), "s-undated", transcript_path=path)
    text = _transcript_text(project, "s-undated")
    assert "no clock on this one" in text
    assert not [line for line in text.splitlines() if line.startswith("ts:")]


def test_recovery_verifies_format_2_notes(project):
    from alpaca.wiki import recovery
    _start(project, "s-rec", "codex")
    append(project, "task-move", {"to": "done", "proof": "local:p.md"}, session="s-rec",
           op="op-8", ref="t-9")
    report = recovery.run(str(project), session="s-rec")
    assert report["status"] == "ok" and report["verified_events"] >= 2
    again = recovery.run(str(project), session="s-rec")
    assert again["status"] == "ok"


# ------------------------------------------------------------------ t-070 round 2 C-B (A-9 header)
#: What a generated header value is made of: every line break `str.splitlines` knows, tabs and space
#: runs, wikilink brackets, header-shaped tails, the escaped form's own prefix, quotes, backslashes
#: and a few letters and non-ASCII characters.
_PARTS = ("\n", "\r", "\r\n", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " ",
          "\t", " ", "  ", "[[", "]]", "[", "]", "operator: claude", "\nsession: s-forged",
          "\nts: 1999-01-01T00:00:00Z", "event: 9", 'json:"', '"', "\\", "\\n", "a", "Z", "7", "-",
          ":", ": ", "é", "中", "x" * 40)


def _fields(text) -> dict:
    """The header read back: `drain.header_fields`, or on a tree without it (the red run at
    8f8976b5a) the plain `key: value` reading every header reader used before it."""
    if hasattr(drain, "header_fields"):
        return drain.header_fields(text)
    out = {}
    for line in text.split("\n\n", 1)[0].splitlines():
        key, separator, value = line.partition(":")
        if separator:
            out[key] = value.strip()
    return out


def _value(raw):
    return drain.header_value(raw) if hasattr(drain, "header_value") else raw


def _generated_values(seed, n):
    import random
    rng = random.Random(seed)
    fixed = ["x\noperator: claude", 'json:"a"', 'json:"', "  ", " lead", "trail ", "a  b",
             "[[link]]", " ", "x" * 300, "\\u005b", "ok"]
    out = list(fixed)
    while len(out) < n:
        text = "".join(rng.choice(_PARTS) for _ in range(rng.randint(1, 12)))
        out.append(text[:300])
    return out


def test_every_header_field_round_trips():
    """A header is injective (t-070 round 2 C-B, A-9): for every field of the event note header and
    the transcript header, reading the rendered header back (`drain.header_fields`) gives exactly the
    value it was rendered from, whatever the value holds, and no value adds or hides a line. Pure
    rendering over generated values; the stored note (`_note_doc`, after the screen) keeps the same
    header lines."""
    values = _generated_values(70, 600)
    rule_kinds = sorted(drain.RULE_FIELDS)
    for index, value in enumerate(values):
        kind = rule_kinds[index % len(rule_kinds)]
        data = {field: "%s|%s" % (field, value) for field in drain.RULE_FIELDS[kind]}
        data.update({"op": value, "session": value + "|d", "operator": value + "|o"})
        ev = {"id": 100 + index, "kind": kind, "session": value, "op": "op|" + value,
              "ref": "ref|" + value, "ts": "2026-03-01T09:00:00+00:00", "data": data,
              "_operator": "op-name|" + value}
        _, text = drain._note_text(ev)
        lines = text.split("\n\n", 1)[0].splitlines()
        got = _fields(text)
        expect = {"op": "op|" + value, "role": "note", "key": "ref|" + value, "kind": kind,
                  "event": str(100 + index), "session": value, "operator": "op-name|" + value,
                  "task": None if kind in drain._REF_NOT_TASK else "ref|" + value,
                  "ts": "2026-03-01T09:00:00+00:00"}
        expect = {k: v for k, v in expect.items() if v is not None}
        for field in drain.RULE_FIELDS[kind]:
            expect["data-" + field if field in drain.HEADER_KEYS else field] = data[field]
        for field in ("op", "session", "operator"):
            if field not in drain.RULE_FIELDS[kind]:
                expect["data-" + field] = data[field]
        expect["role"] = drain._role_of(kind, data)       # the fixed role table, not a value
        assert got == expect, (index, repr(value), lines)
        assert len(lines) == len(expect), (index, repr(value), lines)
        assert drain.projection_identity(text) == "event:%d" % (100 + index)
        stored = drain._note_doc(ev)[1].split("\n\n", 1)[0].splitlines()
        assert [line.partition(":")[0] for line in stored] == [
            line.partition(":")[0] for line in lines], (index, repr(value))


def test_every_transcript_header_field_round_trips(tmp_path):
    """The transcript header: session, operator and source come back exactly, and the projection's
    identity is the session as given. The source is the portable identity `transcript:<session>`,
    never the path the transcript was read from (t-417)."""
    record = json.dumps({"type": "user", "timestamp": "2026-09-01T10:00:00Z",
                         "message": {"content": "hello"}}) + "\n"
    for index, value in enumerate(_generated_values(71, 120)):
        name = "t%d-%s.jsonl" % (index, value[:50].replace("/", "_").replace("\x00", "_"))
        path = tmp_path / name
        path.write_text(record, encoding="utf-8")
        doc_id, text = drain._transcript_doc(value, str(path), operator="op|" + value)
        got = _fields(text)
        assert got.get("session") == value and got["operator"] == "op|" + value, (index, repr(value))
        assert got["source"] == "transcript:" + value, (index, repr(value))
        assert str(tmp_path) not in text, (index, repr(value))
        assert got["role"] == "transcript" and got["source_bytes"] == str(len(record.encode()))
        assert drain.projection_identity(text) == "transcript:" + value
        assert len(text.split("\n\n", 1)[0].splitlines()) == 8, (index, repr(value))


def test_a_hostile_session_id_lands_and_forges_no_attribution(project):
    """b05 (A-9): a session id holding a newline and a header-shaped tail captures (no
    `drain-failed`), its note and its transcript name no operator, and the store reads the session
    back as the id itself."""
    hostile = "atk-x\noperator: claude"
    eid = append(project, "result", {"body": "The pelican crane brake was relined."},
                 session=hostile, op="op-8", ref="t-9")
    path = _transcript(project, "hostile", [
        {"type": "user", "timestamp": "2026-09-01T10:00:00Z", "message": {"content": "hi"}}])
    summary = drain.capture(str(project), events(project, {eid}), sessions=[hostile],
                            transcript_paths={hostile: path})
    assert summary["missing_transcripts"] == [] and summary["transcripts"], summary
    meta, lines, _ = _note(project, eid)
    assert "operator" not in meta and _value(meta["session"]) == hostile, lines
    assert summary["transcripts"][0]["session"] == hostile
    text = (vault(project) / summary["transcripts"][0]["doc_id"]).read_text(encoding="utf-8")
    assert "operator" not in _fields(text)
    from alpaca.wiki.config import Config
    assert hostile in service._store_scan(Config.for_project(str(project)))["transcript_sessions"]
