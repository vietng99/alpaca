"""op-008 t-070 fix plan RC3: one transition function for candidate rows, used by the live doors,
recovery replay and import (alpaca/wiki/lifecycle.py), so replay never accepts what the live door
refuses; import validates the record before it lands anything; a new event at a reused record id
lands (review findings B-1, adv-r1-m2, adv-r1-m3, m-adv-4, merge m8).

Real record, real drain, real store. The hand-made inputs (a forged record event appended through the
record's own appender, a record row edited to break the chain, a stored note given another event's
header) say so where they are made.
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import (admit, append, appender, digest_of, events, ingest,  # noqa: E402
                            llm_edges, note, proposal, propose, row_of, store, vault, wiki_events,
                            withdraw)
from convergence_helpers import assert_converged, shadow  # noqa: E402

from alpaca.wiki import lifecycle, portable, recovery     # noqa: E402
from alpaca.wiki.config import Config                     # noqa: E402
from alpaca.wiki.ingest import drain                      # noqa: E402
from alpaca.wiki.ingest.absorb import Absorber            # noqa: E402

BLOCKED = "Task t-9 is blocked on the review."


# ------------------------------------------------------------------ the table
def test_the_table_and_the_writer_agree():
    """Every move of the table is in MOVES and every MOVES entry is in the table."""
    assert {(f, t) for _act, f, t, _where in lifecycle.TABLE} == set(lifecycle.MOVES)
    with pytest.raises(lifecycle.IllegalTransition):
        lifecycle.check_move("withdrawn", "admitted")


def test_the_writer_refuses_a_move_the_table_does_not_hold(project):
    eid = note(project, BLOCKED)
    cid = propose(project, proposal(project, eid, "Task t-9 is blocked")).candidate_id
    assert withdraw(project, cid).status == "applied"
    from alpaca.wiki.store.db import DB
    from alpaca.wiki.store.ledger import Ledger
    from alpaca.wiki.store.write import Writer
    cfg = Config.for_project(project)
    db = DB(cfg)
    try:
        writer = Writer(db, Ledger(cfg.ledger_path, vault_dir=cfg.vault_dir))
        with pytest.raises(lifecycle.IllegalTransition):
            with writer.transaction():
                writer.set_candidate_status(cid, "admitted")
    finally:
        db.close()
    assert row_of(project, cid)["status"] == "withdrawn"


# ------------------------------------------------------------------ replay asks what live asks
def test_replay_skips_an_admission_by_a_job_session(project):
    """adv-r1-m2: an applied `wiki-admission` from `job:` (a forged record event; the live door
    refuses a job) is skipped by the replay, so the rebuild holds no edge the live door would not."""
    eid = note(project, BLOCKED)
    cid = propose(project, proposal(project, eid, "Task t-9 is blocked")).candidate_id
    job = "job:rune2-consolidate"
    live = admit(project, cid, session=job)
    assert live.status == "refused" and live.reason == "job-cannot-admit"
    good = admit(project, cid, session="s-admin")           # the real admission, for its payload
    payload = next(e["data"] for e in wiki_events(project, "wiki-admission")
                   if e["data"]["result"] == "applied")
    assert good.status == "applied"
    other = propose(project, proposal(project, eid, "Task t-9 is blocked on the review"),
                    session="s-agent-2").candidate_id
    appender(project, job)("wiki-admission", dict(payload, candidate_id=other, session=job))
    assert row_of(project, other)["status"] == "checked"
    report = recovery.replay(shadow(project, "job-rebuild"))
    assert report["candidates"]["skipped"].get("job-cannot-admit") == 1, report["candidates"]
    assert_converged(project)


def test_replay_takes_the_proposer_from_the_row(project):
    """adv-r1-m3: a second non-duplicate `wiki-candidate` event for an id that already has a row (a
    forged event; the live door would answer duplicate) writes no row, so it does not make its session
    the proposer: an admission by the row's proposer is self-admission in replay as it is live."""
    eid = note(project, BLOCKED)
    first = propose(project, proposal(project, eid, "Task t-9 is blocked"), session="s-a")
    data = next(e["data"] for e in wiki_events(project, "wiki-candidate")
                if e["data"]["candidate_id"] == first.candidate_id)
    appender(project, "s-b")("wiki-candidate", data)        # the forged second proposal
    live = admit(project, first.candidate_id, session="s-a")
    assert live.status == "refused" and live.reason == "self-admission"
    payload = {"candidate_id": first.candidate_id, "session": "s-a", "result": "applied",
               "reason": None, "verdict": "novel", "entailer": None, "effect": "edge-written",
               "edge": None, "lesson": None, "knowledge": None}
    appender(project, "s-a")("wiki-admission", payload)    # forged: what live refused
    report = recovery.replay(shadow(project, "proposer-rebuild"))
    assert report["candidates"]["skipped"].get("self-admission") == 1, report["candidates"]
    assert llm_edges(project) == []
    assert_converged(project)


# ------------------------------------------------------------------ B-1 pinned
@pytest.mark.parametrize("how", ["withdraw-candidate", "owner-retract"])
def test_a_fact_proposed_again_over_a_dead_edge_is_refused_naming_it(project, how):
    """B-1: the edge of an admitted fact is retracted (its candidate withdrawn, or the owner's
    correction); the same fact proposed again from the same block (another quote) is refused at
    admission with the edge named, the candidate stays `checked`, and live equals rebuild."""
    from alpaca.wiki import service
    eid = note(project, BLOCKED)
    one = propose(project, proposal(project, eid, "Task t-9 is blocked")).candidate_id
    assert admit(project, one).status == "applied"
    (edge,) = llm_edges(project)
    if how == "withdraw-candidate":
        assert withdraw(project, one).status == "applied"
    else:
        assert service.correct(project, edge["edge_id"], "retract", session="s-owner",
                               record=appender(project, "s-owner"), reason="wrong",
                               by="owner").status == "applied"
    two = propose(project, proposal(project, eid, "Task t-9 is blocked on the review")).candidate_id
    out = admit(project, two)
    assert out.status == "refused" and out.reason == "edge-not-active", out
    assert out.effects["edge_id"] == edge["edge_id"] and out.effects["edge_status"] == "retracted"
    assert row_of(project, two)["status"] == "checked"
    assert [e["status"] for e in llm_edges(project)] == ["retracted"]
    assert_converged(project)


# ------------------------------------------------------------------ m-adv-4
def test_an_import_validates_the_record_before_it_lands_a_document(project):
    """m-adv-4: the record chain is checked before any document lands, so a refused import leaves the
    store empty and a retry on a sound record imports. The chain is broken by editing one record row
    of the target's copy (the injected fault)."""
    ingest(project, "The kestrel queue drains nightly at two.\n", "wiki/notes/kestrel.md", name="k.md")
    note(project, BLOCKED)
    export = Path(project).parent / "chain-export.jsonl"
    portable.export_store(project, export)
    target = shadow(project, "chain-import")
    conn = sqlite3.connect(os.path.join(target, ".alpaca", "alpaca.db"))
    conn.execute("DROP TRIGGER events_no_update")             # the fault: only on this copy
    conn.execute("UPDATE events SET data=? WHERE id=1", (json.dumps({"body": "edited"}),))
    conn.commit()
    conn.close()
    report = portable.import_store(target, export)
    assert report["status"] == "refused" and report["reason"].startswith("record-chain"), report
    assert recovery.store_is_empty(Config.for_project(target))
    target = shadow(project, "chain-import")                   # the sound record again
    again = portable.import_store(target, export)
    assert again["status"] == "ok", again


# ------------------------------------------------------------------ merge m8
def _strip_origin(project, doc_id):
    """A note captured before schema 4 has no origin hash (most live notes, 118,581 at t-070): the
    test setup clears it on the stored version rows of `doc_id`."""
    with store(project) as conn:
        conn.execute("UPDATE source_versions SET origin_hash=NULL WHERE doc_id=?", (doc_id,))


def _seed(project, doc_id, text):
    Config.for_project(str(project)).vault_dir.mkdir(parents=True, exist_ok=True)
    absorber = Absorber(Config.for_project(str(project)))
    try:
        absorber.absorb_text(doc_id, text, kind="raw", path=doc_id,
                             waive_reason="test: seed a stored note")
    finally:
        absorber.close()
    (vault(project) / doc_id).parent.mkdir(parents=True, exist_ok=True)
    (vault(project) / doc_id).write_text(text)


def _normalized(project, event_id):
    from alpaca import db as record
    conn = record.connect_readonly(str(project))
    try:
        return drain.normalize_events(conn, events(project, {event_id}))[0]
    finally:
        conn.close()


def test_a_new_event_at_a_reused_id_with_the_same_kind_and_second_lands(project):
    """merge m8: the stored note at the id was rendered from another event (the same kind, the same
    `ts` second, no origin hash on the stored side); the new event's note lands at its `-render-`
    revision and is not counted as kept."""
    eid = append(project, "result", {"body": "the heron rota moved to Monday"}, session="s-m8")
    ev = _normalized(project, eid)
    doc_id, _text = drain._note_doc(ev)
    other = dict(ev, data={"body": "another event that held this id before the restore"})
    _seed(project, doc_id, drain._note_doc(other)[1])          # same id, kind and ts; other body
    _strip_origin(project, doc_id)
    result = drain.capture(str(project), events(project, {eid}))
    assert result["kept_renderings"] == 0 and result["ingested"] == 1, result
    assert "-render-" in result["docs"][0]
    assert "heron" in (vault(project) / result["docs"][0]).read_text()
    # the rebuild has no other event at the id (the record no longer holds it), so the note lands at
    # the plain id there; what is accepted is the same
    live = digest_of(project)
    from convergence_helpers import rebuilt, state
    assert state(rebuilt(project))["digest"] == live


def test_the_same_event_in_an_older_rendering_is_still_kept(project):
    """The other side of m8 (review B4 holds): a stored note of the same event without an origin hash,
    in an older rendering whose summary line held fewer fields (the shape of the 16 live notes that
    differ in more than the screen, `.alpaca/proofs/op-008/t-070.logs/rc13/m8-live-probe.log`), is
    kept as stored."""
    eid = append(project, "task-move", {"from": "doing", "to": "done", "proof": "local:p.md"},
                 session="s-m8", ref="t-9", op="op-8")
    ev = _normalized(project, eid)
    doc_id, text = drain._note_doc(ev)
    head, body = text.split("\n\n", 1)
    older = head + "\n\n" + body.split(". proof: ")[0] + "\n"
    assert older != text
    _seed(project, doc_id, older)
    _strip_origin(project, doc_id)
    result = drain.capture(str(project), events(project, {eid}))
    assert result["kept_renderings"] == 1 and result["ingested"] == 0, result
    assert (vault(project) / doc_id).read_text() == older
