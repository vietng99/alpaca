"""Merge of op-008 into main (2026-10-06), review blocker B4: a changed note format never rewrites the
stored notes of a live store unless the owner asks for it.

Note format 2 renders almost every old event note differently. The merged capture rewrote a stored
note in place whenever a new rendering of the same event differed, and the absorber waived the
shrink guard for it, so any `wiki recover`, `wiki context --refresh` or `wiki query --refresh` would
have rewritten about 109,000 live notes (hours of write-lock churn), and the SessionStart banner told
every agent to pass `--refresh`.

Now a capture keeps the stored note of the same event as it is (no write, no shrink waiver) and
reports it in `kept_renderings`; the re-render is the owner's job, `wiki recover --rerender-notes`
(`rerender_notes=True` on `drain.capture`, `drain.run` and `recovery.run`), which rewrites the
notes in place with the shrink guard waived for event notes as op-008 did. New events and new notes
land as before; a note another event left at a reused id still goes to `-render-<sha>` (main
cea450f95). The banner no longer names `--refresh`.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import append, events, store, vault  # noqa: E402

from alpaca import cli, operator                          # noqa: E402
from alpaca.wiki import recovery                          # noqa: E402
from alpaca.wiki.config import Config                     # noqa: E402
from alpaca.wiki.ingest import drain                      # noqa: E402
from alpaca.wiki.ingest.absorb import Absorber            # noqa: E402
from alpaca.wiki.ingest.shrink import BodyShrinkRejected  # noqa: E402

LATER = "\nrendered-by: a later note format\n"


def _render_with(monkeypatch, change):
    """Fault injection: a later renderer (a format change) words every event note differently."""
    original = drain._note_doc

    def render(ev, prefixes=()):
        doc_id, text = original(ev, prefixes=prefixes)
        return doc_id, change(text)
    monkeypatch.setattr(drain, "_note_doc", render)
    return original


def _landed(project, n=2, session="s-fmt"):
    ids = [append(project, "result", {"body": "the lantern rota note %d" % i}, session=session)
           for i in range(n)]
    result = drain.capture(str(project), events(project, set(ids)))
    assert result["ingested"] == n
    return ids, list(result["docs"])


def _state(project, doc_ids):
    """What a rewrite would change: each note's stored hash, its vault bytes, its active blocks."""
    with store(project) as conn:
        rows = {d: (conn.execute("SELECT content_sha256 FROM docs WHERE doc_id=?", (d,)).fetchone()[0],
                    (vault(project) / d).read_bytes(),
                    [tuple(r) for r in conn.execute(
                        "SELECT block_id,block_sha256 FROM blocks WHERE doc_id=? AND status='active' "
                        "ORDER BY ordinal", (d,))])
                for d in doc_ids}
        renders = conn.execute("SELECT count(*) FROM docs WHERE doc_id LIKE '%-render-%'").fetchone()[0]
        versions = conn.execute("SELECT count(*) FROM source_versions WHERE doc_id IN (%s)"
                                % ",".join("?" * len(doc_ids)), list(doc_ids)).fetchone()[0]
    return rows, renders, versions


def test_a_capture_keeps_the_stored_note_of_the_same_event_when_its_rendering_changed(project, monkeypatch):
    ids, docs = _landed(project)
    before = _state(project, docs)
    _render_with(monkeypatch, lambda text: text + LATER)
    result = drain.capture(str(project), events(project, set(ids)))
    assert _state(project, docs) == before                 # not rewritten, not revised, no new version
    assert result["kept_renderings"] == 2 and result["ingested"] == 0
    assert result["docs"] == docs and result["notes"] == 2
    # a new event still lands, in the new format
    new = append(project, "result", {"body": "a later lantern note"}, session="s-fmt")
    landed = drain.capture(str(project), events(project, {new}))
    assert landed["ingested"] == 1 and landed["kept_renderings"] == 0
    assert (vault(project) / landed["docs"][0]).read_text().endswith(LATER)


def test_recover_and_refresh_keep_the_stored_notes_by_default(project, monkeypatch, capsys):
    ids, docs = _landed(project)
    before = _state(project, docs)
    _render_with(monkeypatch, lambda text: text + LATER)
    report = recovery.run(str(project), session="s-rec")
    assert report["status"] == "ok" and report["kept_renderings"] == 2
    assert report["verified_events"] >= 2                  # verified as they are stored
    assert _state(project, docs) == before
    assert cli.main(["wiki", "context", "lantern rota", "--refresh"]) == 0
    assert cli.main(["wiki", "query", "lantern rota", "--refresh"]) == 0
    assert cli.main(["wiki", "recover"]) == 0
    capsys.readouterr()
    assert _state(project, docs) == before


def test_the_owner_rerender_job_rewrites_the_notes_in_place(project, monkeypatch, capsys):
    ids, docs = _landed(project)
    _render_with(monkeypatch, lambda text: text + LATER)
    assert cli.main(["wiki", "recover", "--rerender-notes"]) == 0
    capsys.readouterr()
    after = _state(project, docs)
    rows, renders, _ = after
    assert renders == 0                                     # in place, at the same doc ids
    assert all(raw.decode().endswith(LATER) for _, raw, _ in rows.values())
    again = recovery.run(str(project), session="s-rec", rerender_notes=True)
    assert again["status"] == "ok" and again["kept_renderings"] == 0
    assert _state(project, docs) == after                   # a second run rewrites nothing


def test_the_shrink_guard_holds_for_an_event_note_unless_the_owner_asks(project, monkeypatch):
    """A smaller rendering of a stored note: a capture keeps the note, the absorber refuses it with
    no waiver, and only the owner's re-render lands it, the guard waived as a format change."""
    original = _render_with(monkeypatch, lambda text: text + "\nlegacy renderer metadata line" * 40)
    ids, docs = _landed(project, n=1)
    monkeypatch.setattr(drain, "_note_doc", original)
    before = _state(project, docs)
    assert drain.capture(str(project), events(project, set(ids)))["kept_renderings"] == 1
    assert _state(project, docs) == before
    doc_id, text = drain._note_doc(_normalized(project, ids[0]))
    assert doc_id == docs[0]
    absorber = Absorber(Config.for_project(str(project)))
    try:
        with pytest.raises(BodyShrinkRejected):
            absorber.absorb_text(doc_id, text, kind="raw", path=doc_id)
    finally:
        absorber.close()
    assert _state(project, docs) == before
    landed = drain.capture(str(project), events(project, set(ids)), rerender_notes=True)
    assert landed["ingested"] == 1 and landed["docs"] == docs
    assert (vault(project) / doc_id).read_text() == text


def _normalized(project, event_id):
    from alpaca import db as record
    conn = record.connect_readonly(str(project))
    try:
        return drain.normalize_events(conn, events(project, {event_id}))[0]
    finally:
        conn.close()


def test_a_reused_id_still_goes_to_its_render_revision_by_default(project, monkeypatch):
    """main cea450f95 stays: another event's note at the id is kept and the new event's note lands
    beside it at `-render-<sha>`; nothing about that path needs the opt-in."""
    ids, docs = _landed(project, n=1)
    ev = _normalized(project, ids[0])
    other = dict(ev, ts="2020-01-01T00:00:00+00:00", data={"body": "another event that held this id"})
    _, other_text = drain._note_doc(other)
    absorber = Absorber(Config.for_project(str(project)))
    try:
        absorber.absorb_text(docs[0], other_text, kind="raw", path=docs[0],
                             waive_reason="test: seed another event's note")
    finally:
        absorber.close()
    (vault(project) / docs[0]).write_text(other_text)
    result = drain.capture(str(project), events(project, set(ids)))
    assert result["kept_renderings"] == 0 and "-render-" in result["docs"][0]
    assert (vault(project) / docs[0]).read_text() == other_text


def test_the_session_start_banner_does_not_tell_agents_to_refresh(project):
    text = operator.run("start", project, "s-banner", operator="codex")["context"]
    assert "WIKI EVIDENCE" in text and "bin/alpaca wiki context" in text
    assert "--refresh" not in text
