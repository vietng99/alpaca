"""op-008 t-066 stage s2-admission: an admitted fact keeps its anchor and its edge id, and a rebuild
from the record meets it again (design/rune2-memory/architecture.md 4.3, 7.6, 10.2).

* A re-rendered note keeps the admitted edge (same edge id, new block) while the quote stands.
* `wiki recover` replays `wiki-candidate`, `wiki-admission` and `wiki-candidate-withdrawn` in record
  id order, capturing each event before the act that follows it, with the Writer clock at the act's
  `ts`; it runs no check again. The rebuilt store has an equal `accepted_digest`.
* Record-only checks skip a consistency failure and count it; a changed verdict is `order-drift`
  (novel against matches or duplicate) or `stale`.
Real record, real drain, real store. Two tests inject a fault and say so where they do.
"""
import json
import os
import sys
from contextlib import closing

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import (CONFIG_A, CONFIG_B, T09, T10, T11, admit, append, appender, at,  # noqa: E402
                            candidate_rows, capture, claims, corroborators, digest_of, doc_like, edges,
                            events, llm_edges, note, proposal, propose, rebuild_store, reset_vault,
                            row_of, set_config, sockets_blocked, store, unknown_time, wiki_events,
                            withdraw)

from alpaca.wiki import intake, recovery, service              # noqa: E402
from alpaca.wiki.config import Config                           # noqa: E402
from alpaca.wiki.determinism import normalize_text              # noqa: E402
from alpaca.wiki.ingest import drain                            # noqa: E402
from alpaca.wiki.ingest.absorb import Absorber                  # noqa: E402
from alpaca.wiki.store.db import DB                             # noqa: E402

BLOCKED = "Task t-9 is blocked on the review."


def rerender(project, eid, edit):
    """Absorb an edited copy of an event's note under the same doc id, as a later renderer would."""
    event = next(e for e in events(project) if e["id"] == eid)
    doc_id, text = drain._note_doc(dict(event, data=json.loads(event["data"])))
    new = edit(text)
    assert new != text
    # Merge of op-008 into main, review B4: the shrink guard is waived for a smaller event note only
    # when the caller asks; the owner's re-render (`drain.capture(rerender_notes=True)`, `wiki
    # recover --rerender-notes`) passes this waiver, so the later renderer here passes it too.
    with closing(Absorber(Config.for_project(str(project)))) as absorber:
        result = absorber.absorb_text(doc_id, new, kind="raw", path=doc_id,
                                      waive_reason=drain.RERENDER_WAIVER)
    return doc_id, new, result


def admitted_fact(project, body=BLOCKED, quote="Task t-9 is blocked", ts=T10):
    eid = note(project, body, ts)
    cid = propose(project, proposal(project, eid, quote)).candidate_id
    assert admit(project, cid).status == "applied"
    return eid, cid


# ------------------------------------------------------------------ re-render
def test_an_admitted_fact_survives_a_rerender_of_its_note_with_the_same_edge_id(project):
    eid, cid = admitted_fact(project)
    (before,) = llm_edges(project)
    doc_id, new, result = rerender(project, eid, lambda t: t.replace(BLOCKED, BLOCKED + " It is still open."))
    assert result["retired_blocks"] >= 1 and result["retracted_edges"] == 0
    (after,) = llm_edges(project)
    assert after["edge_id"] == before["edge_id"] and after["status"] == "active"
    assert after["source_block_id"] != before["source_block_id"]
    with store(project) as conn:
        block = conn.execute("SELECT status,text FROM blocks WHERE block_id=?", (after["source_block_id"],)).fetchone()
        old = conn.execute("SELECT status FROM blocks WHERE block_id=?", (before["source_block_id"],)).fetchone()
    assert (block["status"], old["status"]) == ("active", "superseded")
    assert normalize_text(after["source_quote"]) in normalize_text(block["text"])
    assert corroborators(project, after["edge_id"]) == [after["source_block_id"]]
    assert after["corroboration_count"] == 1 and row_of(project, cid)["status"] == "admitted"
    assert [c["status"] for c in claims(project, after["edge_id"])] == ["live"]


def test_a_rerender_that_drops_the_quoted_span_retracts_the_edge(project):
    eid, cid = admitted_fact(project)
    (before,) = llm_edges(project)
    result = rerender(project, eid, lambda t: t.replace(BLOCKED, "Nothing is said about it now."))[2]
    assert result["retracted_edges"] == 1
    (after,) = llm_edges(project)
    assert after["edge_id"] == before["edge_id"] and after["status"] == "retracted"


def test_retire_documents_passes_the_successor_so_an_edge_follows_its_quote(project):
    """A projection replaced by another document hands that document to `retire_block`: the edge
    re-anchors to the block there that holds its quote and keeps its id; an edge whose quote the
    successor does not hold is retracted, as before."""
    text = "The kestrel queue drains nightly at two.\n"
    for name in ("old", "new"):
        open("%s/%s.md" % (project, name), "w").write(text)
        service.ingest(project, "%s/%s.md" % (project, name), doc_id="wiki/%s.md" % name)
    with closing(Absorber(Config.for_project(str(project)))) as absorber:
        old = absorber.db.conn.execute("SELECT * FROM blocks WHERE doc_id='wiki/old.md' AND status='active'").fetchone()
        with absorber.writer.transaction():
            absorber.writer.upsert_node("kestrel queue")
            absorber.writer.upsert_node("nightly")
            for edge_id, quote in (("e_follow", "The kestrel queue drains nightly"),
                                   ("e_lost", "The kestrel queue drains hourly")):
                absorber.writer.upsert_edge({
                    "edge_id": edge_id, "subj_node": "kestrel queue", "predicate": "related_to",
                    "obj_node": "nightly", "obj_datatype": "node", "source_block_id": old["block_id"],
                    "source_doc_id": "wiki/old.md", "source_quote": quote,
                    "extractor": "deterministic", "valid_from": T10})
        stats = absorber.retire_documents(["wiki/old.md"], superseded_by="wiki/new.md")
        assert stats["reanchored"] == 1 and stats["retracted"] == 1
        kept = absorber.db.conn.execute("SELECT * FROM edges WHERE edge_id='e_follow'").fetchone()
        gone = absorber.db.conn.execute("SELECT * FROM edges WHERE edge_id='e_lost'").fetchone()
        assert (kept["status"], kept["source_doc_id"]) == ("active", "wiki/new.md")
        assert gone["status"] == "retracted"


# ------------------------------------------------------------------ rebuild from the record
def scenario(project):
    """Novel, corroborated, superseding and withdrawn admissions, and a refused proposal."""
    a = note(project, BLOCKED, T10)
    b = note(project, "Task t-9 is blocked again on the review.", T10)
    c = note(project, "Task t-9 is released since the review.", T11)
    one = propose(project, proposal(project, a, "Task t-9 is blocked")).candidate_id
    admit(project, one)
    two = propose(project, proposal(project, b, "Task t-9 is blocked again")).candidate_id
    admit(project, two)
    three = propose(project, proposal(project, c, "Task t-9 is released", obj="released")).candidate_id
    admit(project, three)
    gone = propose(project, proposal(project, a, "Task t-9 is blocked on the review",
                                     obj="review")).candidate_id
    refused = propose(project, proposal(project, a, "Task t-9 is not in this note"))
    assert refused.status == "refused"
    withdraw(project, gone)
    return one, two, three, gone


def state(project):
    return (digest_of(project), candidate_rows(project),
            [(e["edge_id"], e["status"], e["valid_until"], e["corroboration_count"]) for e in edges(project)])


def test_a_rebuild_from_the_record_gives_an_equal_accepted_digest(project):
    scenario(project)
    before = state(project)
    assert len(llm_edges(project)) == 2
    report = rebuild_store(project)
    cand = report["candidates"]
    assert cand["admissions_applied"] == 3 and cand["order_drift"] == [] and cand["stale"] == []
    assert cand["skipped"] == {} and cand["candidates_written"] == 5
    assert state(project) == before
    assert report["status"] == "ok"


def test_a_withdrawal_replays_and_restores_the_prior(project):
    one, two, three, gone = scenario(project)
    withdraw(project, three)
    live = state(project)
    report = rebuild_store(project)
    assert report["candidates"]["withdrawals_applied"] == 2
    assert state(project) == live
    assert row_of(project, three)["status"] == "retracted" and row_of(project, gone)["status"] == "withdrawn"
    assert [e["status"] for e in llm_edges(project)].count("active") == 1


def test_an_admission_made_while_a_note_lagged_captures_it_first_and_the_rebuild_finds_the_same_verdict(project):
    """The record rule's note was appended but not captured when the agent fact was proposed (so the
    advisory verdict was `novel`). `admit` lands it first, finds the rule's edge and corroborates; the
    rebuild captures that event before the admission act and finds the same verdict, no drift."""
    result = note(project, "Task t-9 is sourced from dec-38940e1fb35f in this result.", T10)
    receipt = propose(project, proposal(project, result, "Task t-9 is sourced from dec-38940e1fb35f",
                                        predicate="sourced_from", obj="dec-38940e1fb35f", datatype="node"))
    assert receipt.verdict == "novel"
    contract = at(project, "task-contract", {"source": "dec-38940e1fb35f", "done_bar": ["x"]}, T11)
    done = admit(project, receipt.candidate_id)                 # lands the contract's note first
    assert done.effects["verdict"] == "matches" and done.effects["effect"] == "corroborated"
    (event,) = wiki_events(project, "wiki-admission")
    assert event["data"]["verdict"] == "matches" and event["id"] > contract
    live = digest_of(project)
    report = rebuild_store(project)
    assert report["candidates"]["order_drift"] == [] and report["candidates"]["stale"] == []
    assert digest_of(project) == live


def test_a_replay_that_finds_matches_where_novel_was_recorded_reports_order_drift(project):
    eid, cid = admitted_fact(project)
    other = note(project, "Task t-9 is blocked once more.", T09)
    reset_vault(project)
    capture(project)
    # The same fact is already in the store, read from `other`, when the admission replays: a fault
    # injected by a direct write, standing for a source that reached the store ahead of the act.
    with closing(Absorber(Config.for_project(str(project)))) as absorber:
        block = absorber.db.conn.execute(
            "SELECT * FROM blocks WHERE doc_id LIKE ? AND status='active'", (doc_like(other),)).fetchone()
        with absorber.writer.transaction():
            absorber.writer.upsert_node("t-9")
            absorber.writer.upsert_edge({
                "edge_id": "e_seed", "subj_node": "t-9", "predicate": "status", "obj_literal": "blocked",
                "obj_datatype": "string", "source_block_id": block["block_id"], "source_doc_id": block["doc_id"],
                "source_quote": "Task t-9 is blocked", "extractor": "deterministic", "valid_from": T09,
                "source_created_at": T09})
        report = recovery.replay_candidates(str(project), absorber=absorber)
        seeded = absorber.db.conn.execute("SELECT corroboration_count FROM edges WHERE edge_id='e_seed'").fetchone()
    admission = wiki_events(project, "wiki-admission")[0]["id"]
    assert report["order_drift"] == [{"candidate_id": cid, "event": admission,
                                      "recorded": "novel", "found": "matches"}]
    assert report["stale"] == [] and report["admissions_applied"] == 1
    assert seeded[0] == 2 and not llm_edges(project)
    row = row_of(project, cid)
    assert (row["status"], row["admission_effect"]) == ("admitted", "corroborated")


def test_a_replay_whose_recorded_prior_no_longer_resolves_marks_the_candidate_stale(project, monkeypatch):
    old = note(project, BLOCKED, T10)
    new = note(project, "Task t-9 is released since the review.", T11)
    admit(project, propose(project, proposal(project, old, "Task t-9 is blocked")).candidate_id)
    cid = propose(project, proposal(project, new, "Task t-9 is released", obj="released")).candidate_id
    admit(project, cid)
    with store(project) as conn:
        prior = dict(conn.execute("SELECT * FROM edges WHERE obj_literal='blocked'").fetchone())
    db = DB(Config.for_project(str(project)))
    try:
        ident = intake.fact_identity(db, prior)
        assert intake.resolve_fact_identity(db, ident) == [prior["edge_id"]]
        assert intake.resolve_fact_identity(db, dict(ident, obj_key="lit:string:other")) == []
        assert intake.resolve_fact_identity(db, dict(ident, anchor_source_ref="event:999")) == []
    finally:
        db.close()
    # fault injection: the replay store holds no edge for the recorded identity
    monkeypatch.setattr(intake, "resolve_fact_identity", lambda db, ident: [])
    report = rebuild_store(project)["candidates"]
    assert [(s["candidate_id"], s["reason"]) for s in report["stale"]] == [(cid, "prior-unresolved")]
    assert row_of(project, cid)["status"] == "stale"
    assert [e["obj_literal"] for e in llm_edges(project)] == ["blocked"]


def test_a_fact_identity_of_unknown_time_matches_without_the_capture_proxy(project):
    eid = note(project, BLOCKED, T10)
    unknown_time(project, eid, T09)
    admit(project, propose(project, proposal(project, eid, "Task t-9 is blocked")).candidate_id)
    (edge,) = llm_edges(project)
    db = DB(Config.for_project(str(project)))
    try:
        ident = intake.fact_identity(db, edge)
        assert ident["valid_from"] is None and ident["anchor_source_ref"] == "event:%d" % eid
        assert intake.resolve_fact_identity(db, ident) == [edge["edge_id"]]
    finally:
        db.close()


def test_an_admission_whose_anchor_no_longer_resolves_marks_the_candidate_stale(project):
    eid = note(project, BLOCKED, T10)
    cid = propose(project, proposal(project, eid, "Task t-9 is blocked")).candidate_id
    appender(project, "s-admin")("wiki-admission", {
        "candidate_id": cid, "session": "s-admin", "result": "applied", "reason": None,
        "verdict": "novel", "entailer": "structural", "effect": "edge-written", "lesson": None,
        "knowledge": None, "edge": {"anchor": {"source_ref": "event:%d" % eid,
                                               "quote": "A sentence the note never held"}}})
    report = rebuild_store(project)["candidates"]
    assert [(s["candidate_id"], s["reason"]) for s in report["stale"]] == [(cid, "anchor-unresolved")]
    assert row_of(project, cid)["status"] == "stale" and not llm_edges(project)


# ------------------------------------------------------------------ record-only checks
def test_a_failing_act_is_skipped_and_counted(project):
    eid = note(project, BLOCKED, T10)
    cid = propose(project, proposal(project, eid, "Task t-9 is blocked")).candidate_id
    row = {"candidate_id": cid, "result": "applied", "reason": None, "verdict": "novel",
           "entailer": "structural", "effect": "edge-written", "edge": None, "lesson": None,
           "knowledge": None}
    # a wiki-admission with no earlier wiki-candidate for its id
    appender(project, "s-admin")("wiki-admission", dict(row, candidate_id="c_never", session="s-admin"))
    # a self-admission: the admitting session is the proposer
    appender(project, "s-agent")("wiki-admission", dict(row, session="s-agent"))
    # another actor: the record accepts any kind from any actor (consistency, not authentication)
    append(project, "wiki-admission", dict(row, session="s-forger"), session="s-forger", actor="agent")
    # a withdrawal by a session that is neither the proposer nor the owner
    appender(project, "s-stranger")("wiki-candidate-withdrawn", {
        "candidate_id": cid, "reason": "x", "undone": {}, "by": None, "status": "withdrawn"})
    report = rebuild_store(project)["candidates"]
    assert report["skipped"] == {"no-earlier-candidate": 1, "self-admission": 1, "actor": 1,
                                 "not-proposer-or-owner": 1}
    assert report["admissions_applied"] == 0 and report["stale"] == []
    assert row_of(project, cid)["status"] == "checked" and not llm_edges(project)


def test_a_rebuild_under_configuration_b_writes_the_same_candidate_rows(project):
    """No check runs again on replay, so candidates checked under A are written alike under B."""
    set_config(project, CONFIG_A)
    scenario(project)
    before = (candidate_rows(project), digest_of(project))
    set_config(project, CONFIG_B)
    report = rebuild_store(project)
    assert report["candidates"]["stale"] == [] and report["candidates"]["order_drift"] == []
    assert (candidate_rows(project), digest_of(project)) == before


def test_the_replay_runs_with_sockets_blocked(project):
    scenario(project)
    live = digest_of(project)
    with sockets_blocked():
        report = rebuild_store(project)
    assert report["candidates"]["admissions_applied"] == 3 and digest_of(project) == live
