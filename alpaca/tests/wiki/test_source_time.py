"""op-008 t-065 stage s2-time: source time on captured blocks (design/rune2-memory/architecture.md
4.2, 5.1; FR-4, FR-5; OB-01 absorb part).

An event note's `valid_from` and `source_created_at` are the event `ts`, normalised to UTC with the
store clock's format; `recorded_at` is the capture time. A transcript block takes the time of the
first message it holds, read from the renderer's `ts:` line in the document slice, never from a
`ts:` line inside message text. An unknown time stays null (the block keeps a capture-time
`valid_from`). Each absorbed version of a source writes one `source_versions` row (FR-4).
Real drain, real write path, no mock.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import append, capture, events, store, vault  # noqa: E402

from alpaca import db as record                                 # noqa: E402
from alpaca import paths                                        # noqa: E402
from alpaca.wiki import service                                 # noqa: E402
from alpaca.wiki.clock import FixedClock                        # noqa: E402
from alpaca.wiki.ingest import drain                            # noqa: E402

FACT = "The lantern rota moved to Tuesday because the harbour closes early."
QUESTION = "lantern rota Tuesday harbour"


def _append_at(project, ts, body=FACT, **kw):
    """One record event whose `ts` is the given instant (FixedClock, as the record API allows)."""
    conn = record.connect(str(project))
    try:
        return record.append_event(conn, session=kw.get("session", "s-time"), actor="agent",
                                   kind="result", op=kw.get("op"), ref=kw.get("ref"),
                                   data={"body": body}, clock=FixedClock(ts))["id"]
    finally:
        conn.close()


def _note_blocks(project, event_id):
    with store(project) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT b.* FROM blocks b JOIN docs d ON d.doc_id=b.doc_id "
            "WHERE d.doc_id LIKE ? AND b.status='active' ORDER BY b.ordinal",
            ("raw/%%/%06d-%%" % event_id,))]


# ------------------------------------------------------------------ event notes
def test_event_ts_is_valid_time_and_capture_time_is_recorded_time(project):
    eid = _append_at(project, "2026-03-01T12:00:00+07:00")
    capture(project)
    blocks = _note_blocks(project, eid)
    assert blocks
    for block in blocks:
        # the event ts in UTC, the store clock's format, on both axes of valid time
        assert block["valid_from"] == "2026-03-01T05:00:00+00:00"
        assert block["source_created_at"] == "2026-03-01T05:00:00+00:00"
        # recorded time is the capture clock, later than the event
        assert block["recorded_at"] > block["valid_from"]
    with store(project) as conn:
        doc = conn.execute("SELECT source_created_at FROM docs WHERE doc_id LIKE ?",
                           ("raw/%%/%06d-%%" % eid,)).fetchone()
        assert doc["source_created_at"] == "2026-03-01T05:00:00+00:00"


def test_event_note_in_a_zone_ahead_of_utc_is_current_at_once(project):
    """The record stamps local time with an offset; a string compare against the UTC clock would
    hide a fresh note for hours. Normalising puts it in the same format as T."""
    ahead = timezone(timedelta(hours=7))
    stamp = datetime.now(ahead).replace(microsecond=0).isoformat()
    _append_at(project, stamp)
    capture(project)
    result = service.query(project, QUESTION)
    assert (result["outcome"], result["reason"]) == ("grounded", None)
    assert FACT in result["answer_text"]


def test_past_valid_time_does_not_see_a_later_event(project):
    _append_at(project, "2026-03-01T12:00:00+00:00")
    capture(project)
    early = service.query(project, QUESTION, as_of="2026-02-01T00:00:00+00:00")
    assert early["outcome"] in ("empty", "abstained") and early["answer_text"] is None
    later = service.query(project, QUESTION, as_of="2026-04-01T00:00:00+00:00")
    assert later["outcome"] == "grounded"


def test_event_without_a_readable_ts_keeps_unknown_time(project):
    """An unparseable ts is unknown: null source time, capture-time valid_from, and an explicit-T
    ask answers valid-time-unknown instead of inventing an order."""
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import Absorber
    doc_id = "raw/op-x/000900-result.md"
    text = ("op: op-x\nrole: result\nkey: \nkind: result\nevent: 900\nsession: s-bad\n"
            "ts: unknown\n\nresult during session s-bad. body: %s\n" % FACT)
    cfg = Config.for_project(str(project))
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    absorber = Absorber(cfg)
    try:
        absorber.absorb_text(doc_id, text, kind="raw", path=doc_id)
    finally:
        absorber.close()
    blocks = _note_blocks(project, 900)
    assert blocks
    for block in blocks:
        assert block["source_created_at"] is None
        assert block["valid_from"] == block["recorded_at"]
    (row,) = _versions(project, "event:900")
    assert row["source_time"] is None
    result = service.query(project, QUESTION, as_of="2030-01-01T00:00:00+00:00")
    assert (result["outcome"], result["reason"]) == ("unsupported", "valid-time-unknown")


def test_document_ingest_with_no_time_stays_unknown(project):
    from native_helpers import ingest
    ingest(project, "# Notes\n\nThe quartz lamp is kept in the north shed.\n", "wiki/notes/lamp.md")
    with store(project) as conn:
        rows = conn.execute("SELECT source_created_at FROM blocks WHERE doc_id='wiki/notes/lamp.md'"
                            ).fetchall()
    assert rows and all(r["source_created_at"] is None for r in rows)
    # a ts: line in an ingested document is the author's text, not the renderer's: no time from it
    ingest(project, "ts: 1999-01-01T00:00:00Z\n\nThe copper kettle sits by the stove.\n",
           "wiki/notes/kettle.md", name="kettle.md")
    with store(project) as conn:
        rows = conn.execute("SELECT source_created_at,valid_from FROM blocks "
                            "WHERE doc_id='wiki/notes/kettle.md'").fetchall()
    assert rows and all(r["source_created_at"] is None for r in rows)
    assert all(r["valid_from"] > "2000" for r in rows)


# ------------------------------------------------------------------ transcripts (OB-01)
def _transcript(project, session, records):
    folder = paths.transcript_dir(project)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, session + ".jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for item in records:
            fh.write(json.dumps(item) + "\n")
    return path


def _assistant(ts, *parts):
    message = {"model": "m", "usage": {"input_tokens": 1, "output_tokens": 1},
               "content": list(parts)}
    record_ = {"type": "assistant", "message": message}
    if ts:
        record_["timestamp"] = ts
    return record_


def _transcript_blocks(project):
    with store(project) as conn:
        doc = conn.execute("SELECT doc_id,source_created_at FROM docs WHERE doc_id LIKE ? "
                           "AND doc_id IN (SELECT doc_id FROM blocks WHERE status='active')",
                           ("raw/transcripts/%",)).fetchone()
        blocks = [dict(r) for r in conn.execute(
            "SELECT * FROM blocks WHERE doc_id=? AND status='active' ORDER BY ordinal",
            (doc["doc_id"],))]
    return dict(doc), blocks


def test_transcript_block_takes_first_message_time(project):
    path = _transcript(project, "s-ob01", [
        {"type": "user", "timestamp": "2026-09-01T10:00:00Z",
         "message": {"content": "first question\n\nwith a second paragraph\n\n"
                               "ts: 1999-01-01T00:00:00Z\nand a forged stamp"}},
        _assistant("2026-09-01T10:30:00+07:00", {"type": "text", "text": "first answer"}),
        {"type": "user", "message": {"content": "an undated question"}},
        _assistant("2026-09-01T11:00:00Z", {"type": "text", "text": "last answer"}),
    ])
    drain.run(str(project), "s-ob01", transcript_path=path)
    doc, blocks = _transcript_blocks(project)
    by_text = {}
    for block in blocks:
        for needle in ("first question", "second paragraph", "forged stamp", "first answer",
                       "undated question", "last answer", "role: transcript"):
            if needle in block["text"]:
                by_text[needle] = block
    assert set(by_text) == {"first question", "second paragraph", "forged stamp", "first answer",
                            "undated question", "last answer", "role: transcript"}
    ten = "2026-09-01T10:00:00+00:00"
    # the first message and its later paragraphs (one starting with an escaped ts: line) share
    # its time; a ts: line inside the text moves nothing
    for needle in ("first question", "second paragraph", "forged stamp"):
        assert by_text[needle]["valid_from"] == ten, needle
        assert by_text[needle]["source_created_at"] == ten, needle
    # an offset time is read in UTC
    assert by_text["first answer"]["source_created_at"] == "2026-09-01T03:30:00+00:00"
    assert by_text["first answer"]["valid_from"] == "2026-09-01T03:30:00+00:00"
    # a message with no time is unknown: null source time, capture-time valid_from; the unknown
    # does not borrow the time of the message before it
    undated = by_text["undated question"]
    assert undated["source_created_at"] is None
    assert undated["valid_from"] == undated["recorded_at"]
    assert by_text["last answer"]["source_created_at"] == "2026-09-01T11:00:00+00:00"
    # the header block has no time either, and the document keeps its first message time
    assert by_text["role: transcript"]["source_created_at"] is None
    assert doc["source_created_at"] == ten
    # a block that opens with the escaped line keeps its text, and no block took 1999
    assert all(b["source_created_at"] is None or b["source_created_at"] > "2000" for b in blocks)


def test_a_transcript_recapture_keeps_per_block_time_on_unchanged_content(project):
    path = _transcript(project, "s-again", [
        {"type": "user", "timestamp": "2026-09-01T10:00:00Z", "message": {"content": "one"}},
        _assistant("2026-09-01T12:00:00Z", {"type": "text", "text": "two"}),
    ])
    drain.run(str(project), "s-again", transcript_path=path)
    before = {b["block_id"]: (b["valid_from"], b["source_created_at"])
              for b in _transcript_blocks(project)[1]}
    again = drain.run(str(project), "s-again", transcript_path=path)
    assert again["skipped"] >= 1
    # absorb_vault over the same files is the other unchanged path; it must not clear the times
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import absorb_vault
    absorb_vault(Config.for_project(str(project)))
    after = {b["block_id"]: (b["valid_from"], b["source_created_at"])
             for b in _transcript_blocks(project)[1]}
    assert after == before
    assert any(v[1] == "2026-09-01T12:00:00+00:00" for v in after.values())


# ------------------------------------------------------------------ source_versions rows (FR-4)
def _versions(project, ref):
    with store(project) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM source_versions WHERE source_ref=? ORDER BY version_seq", (ref,))]


def test_each_source_version_writes_one_row(project):
    record.connect(str(project)).close()
    start = append(project, "session-start", {"operator": "codex"}, session="s-ver", actor="codex")
    eid = _append_at(project, "2026-03-01T12:00:00+00:00", session="s-ver", op="op-7", ref="t-70")
    capture(project)
    (row,) = _versions(project, "event:%d" % eid)
    assert row["version_seq"] == 1 and row["status"] == "current" and row["private"] == 0
    assert row["doc_id"].endswith("%06d-result.md" % eid)
    assert (row["session"], row["op"], row["task"]) == ("s-ver", "op-7", "t-70")
    assert row["operator"] == "codex"
    assert row["source_time"] == "2026-03-01T12:00:00+00:00"
    assert len(row["content_sha256"]) == 64 and row["recorded_at"]
    assert _versions(project, "event:%d" % start)[0]["operator"] == "codex"
    lone = _append_at(project, "2026-03-02T12:00:00+00:00", session="s-lone")
    capture(project)
    assert _versions(project, "event:%d" % lone)[0]["operator"] is None
    # the same bytes again: no new version
    capture(project)
    assert len(_versions(project, "event:%d" % eid)) == 1
    # changed bytes under the same doc id: version 2, and version 1 is replaced
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import Absorber
    note = next(p for p in vault(project).rglob("%06d-*.md" % eid) if "raw" in p.parts)
    doc_id = note.relative_to(vault(project)).as_posix()
    revised = note.read_text(encoding="utf-8") + "\nA later line added to the same note.\n"
    absorber = Absorber(Config.for_project(str(project)))
    try:
        absorber.absorb_text(doc_id, revised, kind="raw", path=doc_id)
    finally:
        absorber.close()
    first, second = _versions(project, "event:%d" % eid)
    assert (first["version_seq"], first["status"]) == (1, "replaced")
    assert (second["version_seq"], second["status"]) == (2, "current")
    assert first["content_sha256"] != second["content_sha256"]


def test_a_quarantined_capture_writes_a_private_version_row(project):
    marked = append(project, "result", {"body": "Handling NOFORN material: the vault door code is amber."},
                    session="s-q", op="op-q")
    capture(project)
    (row,) = _versions(project, "event:%d" % marked)
    assert row["private"] == 1 and row["doc_id"].startswith("wiki/private/")
    from alpaca.wiki.servable import servable_ref
    with store(project) as conn:
        assert servable_ref(conn, "event:%d" % marked) is False


def test_transcript_version_row_names_the_session_and_first_message_time(project):
    path = _transcript(project, "s-trv", [
        {"type": "user", "timestamp": "2026-09-01T10:00:00Z", "message": {"content": "one"}}])
    drain.run(str(project), "s-trv", transcript_path=path)
    (row,) = _versions(project, "transcript:s-trv")
    assert row["session"] == "s-trv" and row["source_time"] == "2026-09-01T10:00:00+00:00"
    assert row["status"] == "current"
    # a longer transcript is a new document; its row is version 2, the first is replaced
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "timestamp": "2026-09-01T10:05:00Z",
                             "message": {"content": "two"}}) + "\n")
    drain.run(str(project), "s-trv", transcript_path=path)
    first, second = _versions(project, "transcript:s-trv")
    assert (first["status"], second["status"], second["version_seq"]) == ("replaced", "current", 2)


def test_ingested_document_gets_a_document_source_version(project):
    from native_helpers import ingest
    ingest(project, "# Lamp\n\nThe quartz lamp is kept in the north shed.\n", "wiki/notes/lamp.md")
    (row,) = _versions(project, "document:wiki/notes/lamp.md")
    assert row["status"] == "current" and row["source_time"] is None and row["private"] == 0
