"""op-008 t-067 stage s1-withdraw: withdrawal propagation (design/rune2-memory/architecture.md 7.6, 9.3,
10.2; FR-17, FR-22; OB-04, OB-11).

Withdrawing a source is an exclusion, not a purge: the record keeps the event, nothing is deleted, and
every record derived from the source leaves every serve path. The seven steps of `withdraw_source`
(the exclusion and the versions, the claims, the blocks, the edges anchored there, the priors those
edges ended, the dependent candidates and the lessons and priors of the admitted ones, the expected
event sets) each have a test here. Real record, real drain, real store; the one hand-built input (an
admission payload carrying a lesson and a prior, which the observation door of a later stage will
write) says so where it is used.
"""
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import (T09, T10, T11, admit, appender, append, at, capture, claims,  # noqa: E402
                            corroborators, digest_of, edges, events, ingest, llm_edges, note, proposal,
                            propose, rebuild_store, row_of, store, vault, wiki_events, withdraw)

from alpaca.wiki import recovery, service, withdrawal            # noqa: E402

BLOCKED = "Task t-9 is blocked on the review."


def withdraw_source(project, ref, session="s-owner", reason="wrong source"):
    return service.withdraw_source(project, ref, session=session, record=appender(project, session),
                                   reason=reason)


def move(project, to, ts, frm="open", task="t-9"):
    data = {"from": frm, "to": to, "proof": None, "reason": None}
    return at(project, "task-move", data, ts, op="op-8", ref=task, actor="human")


def status_edges(project, **where):
    return {e["obj_node"]: e for e in edges(project, predicate="status", **where)}


CONTRACT = {"input": ["x"], "expected": ["y"], "done_bar": ["z"], "fail_cases": ["w"], "stage": "build",
            "source": "dec-abc"}


def stop_capture(project, session="s-src"):
    """The capture the Stop hook makes for an open session (hooks/stop.py `capture_open_session`); the
    SessionEnd and PreCompact hooks make the same call with transcripts."""
    return service.capture(project, sessions=[session], session_events=True, uncaptured_only=True,
                           transcripts=False, deadline_s=None)


def supersede_events(project, prior, successor):
    """The `supersede_edge` events of the store that name this prior and this successor and were not
    reverted: one for each time the successor ended or invalidated the prior."""
    with store(project) as conn:
        rows = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM ingest_event WHERE op='supersede_edge' ORDER BY seq")]
    return [p for p in rows if p.get("prior") == prior and p.get("successor") == successor
            and not p.get("reverted")]


# ------------------------------------------------------------------ OB-04: the expected event sets
def test_withdrawn_event_note_is_not_missing(project):
    """OB-04: an event whose `event:<id>` has an exclusions row leaves the expected set of status,
    coverage and the admit capture step, and status counts it as withdrawn."""
    kept = at(project, "result", {"body": "The lantern rota moved to Tuesday."}, T09, op="op-8")
    gone = at(project, "result", {"body": "The lantern rota moved again."}, T10, op="op-8")
    later = at(project, "result", {"body": "A later result."}, T11, op="op-8")
    capture(project, {kept, later})
    before = service.status(project)["capture"]
    assert before["missing_event_ids"] == [gone] and before["withdrawn_events"] == 0
    assert before["missing_events"] == 1
    receipt = withdraw_source(project, "event:%d" % gone)
    assert (receipt.status, receipt.reason) == ("applied", None)
    after = service.status(project)["capture"]
    assert after["missing_events"] == 0 and after["missing_event_ids"] == []
    assert after["withdrawn_events"] == 1 and after["source_events"] == before["source_events"] - 1
    # a captured note that is withdrawn afterwards is counted as withdrawn, not as captured
    assert withdraw_source(project, "event:%d" % kept).status == "applied"
    again = service.status(project)["capture"]
    assert again["withdrawn_events"] == 2 and again["missing_events"] == 0
    assert again["captured_events"] == 1                       # only `later` is a captured source
    # a scoped question names no missing event, and stays an abstention
    answer = service.query(project, "What did op-8 say about the zebra crossing?")
    assert answer["coverage"]["missing_event_ids_in_scope"] == []
    assert answer["outcome"] == "abstained"
    # the admit capture step has nothing to land for an event that was withdrawn
    assert service._uncaptured_events(project, service.Config.for_project(project)) == []
    # A source copy withdrawn by its own ref: the event note it was made from falls with it and
    # leaves the expected sets too, with no exclusions row of its own; it stays out after the next
    # capture of its session (check round 4 invariants B1).
    contract = at(project, "task-contract", CONTRACT, T11, op="op-8", ref="t-9")
    capture(project, {contract})
    assert service.status(project)["capture"]["captured_events"] == 2
    contract_note = doc_of(project, "event:%d" % contract)
    assert withdraw_source(project, "contract:t-9").status == "applied"
    with store(project) as conn:
        assert "event:%d" % contract not in {r[0] for r in conn.execute("SELECT source_ref FROM exclusions")}
    for _ in ("withdrawn", "after the next capture"):
        assert active_blocks_of(project, [contract_note]) == 0     # status leaves a withdrawn event out
        copied = service.status(project)["capture"]
        assert (copied["withdrawn_events"], copied["missing_events"], copied["captured_events"]) == (3, 0, 1)
        answer = service.query(project, "What did op-8 say about the zebra crossing?")
        assert answer["coverage"]["missing_event_ids_in_scope"] == []
        assert answer["outcome"] == "abstained"
        assert service._uncaptured_events(project, service.Config.for_project(project)) == []
        stop_capture(project, "s-native")


def test_a_withdrawn_event_is_not_captured_again(project):
    eid = at(project, "result", {"body": "The lantern rota moved to Tuesday."}, T09, op="op-8")
    capture(project, {eid})
    assert withdraw_source(project, "event:%d" % eid).status == "applied"
    result = capture(project, {eid})
    assert result["skipped"] == 1 and result["ingested"] == 0
    with store(project) as conn:
        assert conn.execute("SELECT COUNT(*) FROM blocks WHERE status='active' AND doc_id LIKE ?",
                            ("raw/%%/%06d-%%" % eid,)).fetchone()[0] == 0
        assert [r["status"] for r in conn.execute(
            "SELECT status FROM source_versions WHERE source_ref=?", ("event:%d" % eid,))] == ["withdrawn"]


# ------------------------------------------------------------------ OB-11: priors come back
def test_withdrawn_successor_restores_prior_window(project):
    """OB-11: moves doing, done, review; withdrawing the `done` note retracts its edge and ends `doing`
    at the `review` edge's valid_from, not at the withdrawn event's ts. With only doing and done,
    `doing` is current again. A correction-style supersede returns its prior to `active`."""
    t1, t2, t3 = ("2026-03-01T%02d:00:00+00:00" % h for h in (10, 11, 12))
    move(project, "doing", t1)
    done = move(project, "done", t2, frm="doing")
    move(project, "review", t3, frm="done")
    capture(project)
    before = status_edges(project)
    assert before["status:doing"]["valid_until"] == t2 and before["status:done"]["valid_until"] == t3
    receipt = withdraw_source(project, "event:%d" % done)
    assert receipt.status == "applied" and receipt.effects["edges_retracted"] >= 1
    now = status_edges(project)
    assert now["status:done"]["status"] == "retracted"
    assert now["status:done"]["retraction_reason"] == "source-withdrawn"
    assert (now["status:doing"]["status"], now["status:doing"]["valid_until"]) == ("active", t3)
    assert now["status:review"]["status"] == "active" and now["status:review"]["valid_until"] is None


def test_withdrawing_the_last_move_makes_the_one_before_current_again(project):
    t1, t2 = "2026-03-01T10:00:00+00:00", "2026-03-01T11:00:00+00:00"
    move(project, "doing", t1)
    done = move(project, "done", t2, frm="doing")
    capture(project)
    assert {e["obj_node"]: e["valid_until"] for e in edges(project, predicate="status")}["status:doing"] == t2
    withdraw_source(project, "event:%d" % done)
    now = {e["obj_node"]: e for e in edges(project, predicate="status")}
    assert (now["status:doing"]["status"], now["status:doing"]["valid_until"]) == ("active", None)
    assert now["status:done"]["status"] == "retracted"


def test_a_correction_style_supersede_returns_its_prior_to_active(project):
    """Two moves stamped alike: the earlier edge is invalidated by the later one as a same-instant
    correction. Withdrawing the later note gives it back `active`, with no `superseded_at`."""
    t = "2026-03-01T10:00:00+00:00"
    move(project, "doing", t)
    done = move(project, "done", t, frm="doing")
    capture(project)
    invalid = {e["obj_node"]: e for e in edges(project, predicate="status")}["status:doing"]
    assert invalid["status"] == "invalidated" and invalid["superseded_at"] is not None
    withdraw_source(project, "event:%d" % done)
    back = {e["obj_node"]: e for e in edges(project, predicate="status")}["status:doing"]
    assert (back["status"], back["superseded_at"], back["superseded_by_edge_id"]) == ("active", None, None)


def test_supersede_records_the_prior_as_it_was_before(project):
    t1, t2 = "2026-03-01T10:00:00+00:00", "2026-03-01T11:00:00+00:00"
    move(project, "doing", t1)
    move(project, "done", t2, frm="doing")
    capture(project)
    with store(project) as conn:
        payloads = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM ingest_event WHERE op='supersede_edge' ORDER BY seq")]
    (made,) = [p for p in payloads if p.get("world_change")]
    assert made["prior_before"] == {"status": "active", "valid_until": None, "superseded_at": None}


# ------------------------------------------------------------------ the derived records
def admitted_fact(project, body=BLOCKED, quote="Task t-9 is blocked", ts=T10, **kw):
    eid = note(project, body, ts)
    cid = propose(project, proposal(project, eid, quote, **kw)).candidate_id
    assert admit(project, cid).status == "applied"
    return eid, cid


def test_withdrawing_a_source_retracts_dependent_edges_candidates_and_claims(project):
    eid, admitted = admitted_fact(project)
    second = note(project, "Task t-9 is blocked again, on the review.", T11)
    pending = propose(project, proposal(project, second, "Task t-9 is blocked again")).candidate_id
    other = note(project, "Task t-10 is open for work.", T11)
    unrelated = propose(project, proposal(project, other, "Task t-10 is open", subject="t-10",
                                          obj="open")).candidate_id
    (edge,) = llm_edges(project)
    receipt = withdraw_source(project, "event:%d" % eid)
    assert receipt.status == "applied"
    states = {d["candidate_id"]: (d["from"], d["to"]) for d in receipt.effects["dependents"]}
    assert states == {admitted: ("admitted", "retracted")}
    assert row_of(project, admitted)["status"] == "retracted"
    assert row_of(project, unrelated)["status"] == "checked"
    # the second candidate cites another note, which stays: it is still a checked candidate
    assert row_of(project, pending)["status"] == "checked"
    now = edges(project, edge_id=edge["edge_id"])[0]
    assert (now["status"], now["retraction_reason"]) == ("retracted", "source-withdrawn")
    assert {c["status"] for c in claims(project, edge["edge_id"])} == {"withdrawn"}
    assert corroborators(project, edge["edge_id"]) == [] and now["corroboration_count"] == 0
    with store(project) as conn:
        assert [r[0] for r in conn.execute("SELECT source_ref FROM exclusions")] == ["event:%d" % eid]


def test_a_checked_candidate_of_a_withdrawn_source_becomes_stale(project):
    eid = note(project, BLOCKED, T10)
    cid = propose(project, proposal(project, eid, "Task t-9 is blocked")).candidate_id
    receipt = withdraw_source(project, "event:%d" % eid)
    assert [(d["from"], d["to"]) for d in receipt.effects["dependents"]] == [("checked", "stale")]
    assert row_of(project, cid)["status"] == "stale"
    assert admit(project, cid).reason == "not-checked"


def test_nothing_of_a_withdrawn_source_is_served_on_any_surface(project):
    """A scan of every string field the candidates view, the answer, the context pack and the dry run
    return finds none of the withdrawn quotes, bodies or edge text."""
    eid, cid = admitted_fact(project, body="Task t-9 is blocked on the zebra crossing review.",
                             quote="Task t-9 is blocked on the zebra crossing")
    note(project, "The lantern rota moved to Tuesday.", T11)
    assert withdraw_source(project, "event:%d" % eid).status == "applied"
    surfaces = []

    def strings(value):
        if isinstance(value, str):
            surfaces.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                strings(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                strings(item)

    for view in service.candidates(project):
        strings(view.to_dict())
    answer = service.query(project, "Is task t-9 blocked?")
    strings({k: v for k, v in answer.items() if k != "question"})
    surfaces.append(service.context(project, "Is task t-9 blocked?"))
    for row in service.record_facts_preview(project, from_event=eid, through_event=eid):
        strings(vars(row) if hasattr(row, "__dict__") else row)
    strings([e["source_quote"] for e in edges(project) if e["status"] == "active"])
    assert "zebra crossing" not in "\n".join(surfaces)
    # the control: the scan reads real surfaces, and a source that was not withdrawn is still served
    assert surfaces and "lantern" in service.context(project, "When does the lantern rota move?")


def test_an_edge_moves_to_a_servable_claim_and_never_to_the_withdrawn_one(project):
    """A fact both notes carry stays: it moves to the live claim of the note that stays. Its
    corroboration row on the withdrawn block is removed and the count follows."""
    first, one = admitted_fact(project, ts=T10)
    second = note(project, "Task t-9 is blocked again on the review.", T11)
    two = propose(project, proposal(project, second, "Task t-9 is blocked again")).candidate_id
    assert admit(project, two).status == "applied"
    (edge,) = llm_edges(project)
    assert edge["source_block_id"].startswith("raw/") and corroborators(project, edge["edge_id"]) \
        and len(corroborators(project, edge["edge_id"])) == 2
    withdraw_source(project, "event:%d" % first)
    (now,) = llm_edges(project)
    assert now["edge_id"] == edge["edge_id"] and now["status"] == "active"
    with store(project) as conn:
        doc = conn.execute("SELECT source_doc_id FROM edges WHERE edge_id=?", (edge["edge_id"],)).fetchone()[0]
    assert doc != edge["source_doc_id"] and doc.startswith("raw/") and ("%06d" % second) in doc
    assert len(corroborators(project, edge["edge_id"])) == 1 and now["corroboration_count"] == 1
    assert row_of(project, one)["status"] == "retracted" and row_of(project, two)["status"] == "admitted"
    live = [c for c in claims(project, edge["edge_id"]) if c["status"] == "live"]
    assert [c["candidate_id"] for c in live] == [two]


def test_a_private_claim_is_not_a_place_to_move_to(project):
    """The block that takes over must itself be servable: an edge whose only other claim is on a
    quarantined (private) capture is retracted, not moved there."""
    first, one = admitted_fact(project, ts=T10)
    second = note(project, "Task t-9 is blocked again on the review.", T11)
    two = propose(project, proposal(project, second, "Task t-9 is blocked again")).candidate_id
    assert admit(project, two).status == "applied"
    with store(project) as conn:
        conn.execute("UPDATE docs SET private=1 WHERE doc_id LIKE ?", ("raw/%%/%06d-%%" % second,))
    withdraw_source(project, "event:%d" % first)
    (now,) = llm_edges(project)
    assert now["status"] == "retracted"


# ------------------------------------------------------------------ the gates (alpaca/lessons.py, knowledge.py)
def test_a_lesson_withdraw_row_is_honored_and_a_rebuild_replays_the_lesson():
    from alpaca.lessons import LessonsStore
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store_ = LessonsStore(conn)
    block = {"id": "L-x", "label": "x", "description": "d", "value": "v", "char_limit": 400,
             "read_only": True, "provenance": "p", "admission_probe_sig": "s", "governs": ["g"],
             "prescribes": "pp"}
    assert store_.replay_admitted(block) is True and store_.replay_admitted(block) is False
    assert [b["id"] for b in store_._retained()] == ["L-x"]
    assert store_.withdraw("L-x", reason="candidate-withdrawn") is True
    assert store_._retained() == [] and store_.withdraw("L-x") is False
    # the log keeps every row; a lesson admitted again after its withdrawal is retained again
    assert [r["event"] for r in conn.execute("SELECT event FROM lessons_log ORDER BY seq")] == [
        "lesson-admit", "lesson-withdraw"]
    block2 = dict(block, value="v2")
    store_.replay_admitted(block2)
    assert [b["value"] for b in store_._retained()] == ["v2"]


def test_decay_takes_a_reason_and_a_recorded_prior_is_replayed_without_the_gate():
    from alpaca.knowledge import KnowledgeStore, RejectedWrite
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ks = KnowledgeStore(conn)
    row = {"claim_id": "OP-the-queue", "scope": "OP", "claim": "the queue drains nightly",
           "stamp": "ASSERTED", "blind_class": "QUARANTINE"}
    assert ks.replay_admitted(row) is True and ks.replay_admitted(row) is False
    with pytest.raises(RejectedWrite):
        ks.replay_admitted(dict(row, claim="another claim under the same id"))
    ks.decay("OP", "OP-the-queue", reason="candidate-withdrawn")
    assert conn.execute("SELECT decay_state FROM knowledge_facts").fetchone()[0] == "DECAYED"
    assert conn.execute("SELECT detail FROM knowledge_log WHERE event='decay'").fetchone()[0] \
        == "candidate-withdrawn -> DECAYED"
    ks.reaffirm("OP", "OP-the-queue")
    ks.decay("OP", "OP-the-queue")                                         # the retention default
    assert conn.execute("SELECT detail FROM knowledge_log WHERE event='decay' ORDER BY seq DESC"
                        ).fetchone()[0] == "unreaffirmed -> DECAYED"


def test_undo_derived_withdraws_the_lesson_and_decays_the_prior(project):
    """An admitted observation puts a lesson and a prior into the gates. The observation door arrives
    in a later stage, so the admission payload is built here by hand, in the shape of
    interfaces.py.txt section 3 (`lesson`: the block, `knowledge`: the prior row)."""
    from alpaca.knowledge import KnowledgeStore
    from alpaca.lessons import LessonsStore
    block = {"id": "L-wait", "label": "wait", "description": "d", "value": "v", "char_limit": 400,
             "read_only": True, "provenance": "p", "admission_probe_sig": "s", "governs": ["g"],
             "prescribes": "pp"}
    prior = {"claim_id": "OP-wait", "scope": "OP", "claim": "the queue drains nightly",
             "stamp": "ASSERTED", "blind_class": "QUARANTINE"}
    vault(project).mkdir(parents=True, exist_ok=True)
    with store(project) as conn:
        conn.row_factory = sqlite3.Row
        LessonsStore(conn).replay_admitted(block)
        KnowledgeStore(conn).replay_admitted(prior)
        done = withdrawal.undo_derived(conn, {"lesson": block, "knowledge": prior})
        assert done == {"lesson": "L-wait", "prior": "OP-wait"}
        assert LessonsStore(conn)._retained() == []
        assert conn.execute("SELECT decay_state FROM knowledge_facts").fetchone()[0] == "DECAYED"
    # a fact has neither
    with store(project) as conn:
        assert withdrawal.undo_derived(conn, {"lesson": None, "knowledge": None}) == {}


# ------------------------------------------------------------------ absorb does not revive
def test_absorb_does_not_revive_an_excluded_document(project, tmp_path):
    page = "The kestrel queue drains nightly at two.\n"
    ingest(project, page, "wiki/synthetic/kestrel.md")
    with store(project) as conn:
        assert conn.execute("SELECT COUNT(*) FROM blocks WHERE status='active'").fetchone()[0] >= 1
    # the evaluation fixtures name an ingested document `wiki:<doc-id>`: the same source
    receipt = withdraw_source(project, "wiki:wiki/synthetic/kestrel.md")
    assert receipt.status == "applied" and receipt.effects["source_ref"] == "document:wiki/synthetic/kestrel.md"
    # an ingested document is its own source, so nothing is renamed (the evaluation fixtures' form)
    assert "requested_ref" not in receipt.effects
    (event,) = wiki_events(project, "wiki-withdrawal")
    assert event["data"]["source_ref"] == "document:wiki/synthetic/kestrel.md"
    assert "requested_ref" not in event["data"]
    with store(project) as conn:
        assert conn.execute("SELECT COUNT(*) FROM blocks WHERE doc_id='wiki/synthetic/kestrel.md' "
                            "AND status='active'").fetchone()[0] == 0
    for text in (page, page + "A second paragraph.\n"):
        result = ingest(project, text, "wiki/synthetic/kestrel.md")
        assert (result["skipped"], result["reason"]) == (True, "excluded")
    with store(project) as conn:
        assert conn.execute("SELECT COUNT(*) FROM blocks WHERE doc_id='wiki/synthetic/kestrel.md' "
                            "AND status='active'").fetchone()[0] == 0
    answer = service.query(project, "When does the kestrel queue drain?")
    assert "kestrel" not in json.dumps({k: answer[k] for k in ("answer_text", "citations", "sources")})
    assert withdraw_source(project, "document:wiki/synthetic/kestrel.md").status == "duplicate"


def test_an_unknown_source_and_a_missing_reason_are_refused(project):
    ingest(project, "The kestrel queue drains nightly.\n", "wiki/synthetic/kestrel.md")
    unknown = withdraw_source(project, "document:wiki/nowhere.md")
    assert (unknown.status, unknown.reason) == ("refused", "unknown-source")
    assert not wiki_events(project, "wiki-withdrawal")
    with pytest.raises(ValueError):
        withdraw_source(project, "event:012")
    with pytest.raises(ValueError):
        withdraw_source(project, "kestrel", reason="x")
    with pytest.raises(ValueError):
        service.withdraw_source(project, "document:wiki/synthetic/kestrel.md", session="s",
                                record=appender(project, "s"), reason="  ")


def test_the_withdrawal_is_recorded_before_the_commit_and_is_not_a_source(project):
    eid = note(project, BLOCKED, T10)
    receipt = withdraw_source(project, "event:%d" % eid, reason="not for the wiki")
    (event,) = wiki_events(project, "wiki-withdrawal")
    assert event["id"] == receipt.record_event_id and event["actor"] == "alpaca-wiki"
    assert event["data"]["source_ref"] == "event:%d" % eid and event["data"]["reason"] == "not for the wiki"
    # the withdrawal event is not a source, so it is not a missing note either
    status = service.status(project)["capture"]
    assert status["missing_events"] == 0


# ------------------------------------------------------------------ rebuild
def test_a_rebuild_replays_a_withdrawal_to_an_equal_digest(project):
    t1, t2, t3 = ("2026-03-01T%02d:00:00+00:00" % h for h in (10, 11, 12))
    move(project, "doing", t1)
    done = move(project, "done", t2, frm="doing")
    move(project, "review", t3, frm="done")
    eid, cid = admitted_fact(project, ts=T10)
    pending = propose(project, proposal(project, note(project, "Task t-10 is open.", T11),
                                        "Task t-10 is open", subject="t-10", obj="open")).candidate_id
    capture(project)
    withdraw_source(project, "event:%d" % done)
    withdraw_source(project, "event:%d" % eid)
    live = digest_of(project)
    live_edges = [(e["subj_node"], e["predicate"], e["obj_node"] or e["obj_literal"], e["status"],
                   e["valid_until"]) for e in edges(project)]
    report = rebuild_store(project)["candidates"]
    assert report["source_withdrawals_applied"] == 2 and report["stale"] == []
    assert digest_of(project) == live
    assert [(e["subj_node"], e["predicate"], e["obj_node"] or e["obj_literal"], e["status"],
             e["valid_until"]) for e in edges(project)] == live_edges
    assert row_of(project, cid)["status"] == "retracted" and row_of(project, pending)["status"] == "checked"
    # the capture loop of the rebuild did not bring a withdrawn note back
    with store(project) as conn:
        assert conn.execute("SELECT COUNT(*) FROM blocks WHERE status='active' AND (doc_id LIKE ? OR "
                            "doc_id LIKE ?)", ("raw/%%/%06d-%%" % done, "raw/%%/%06d-%%" % eid)
                            ).fetchone()[0] == 0


def test_withdraw_source_runs_with_sockets_blocked(project):
    from native_helpers import sockets_blocked
    eid, cid = admitted_fact(project)
    with sockets_blocked():
        receipt = withdraw_source(project, "event:%d" % eid)
        report = rebuild_store(project)
    assert receipt.status == "applied" and report["candidates"]["source_withdrawals_applied"] == 1


# ------------------------------------------------------------------ OB-11 after check round 1: windows
def windows(project, task="t-9"):
    """The active status edges of a task as (object, valid_from, valid_until), in time order."""
    return sorted((e["obj_node"] or e["obj_literal"], e["valid_from"], e["valid_until"])
                  for e in edges(project, predicate="status", subj_node=task, status="active"))


def current_at(project, ts, task="t-9"):
    """The statuses the readers serve as current at `ts`: their own as-of clause, never a copy of it."""
    from alpaca.wiki.store.asof import AsOf, asof_where_edges
    clause, params = asof_where_edges(AsOf(T=ts), "edges")
    with store(project) as conn:
        return sorted(r[0] for r in conn.execute(
            "SELECT COALESCE(obj_node, obj_literal) FROM edges WHERE subj_node=:subj "
            "AND predicate='status' AND " + clause,
            dict(params, subj=task)))


def hours(*hs):
    return tuple("2026-03-01T%02d:00:00+00:00" % h for h in hs)


def test_withdrawing_the_middle_of_a_flip_flop_ends_the_first_blocked_at_the_second(project):
    """Moves blocked, doing, blocked, done; the `doing` note is withdrawn. A store that never held it
    has blocked 10:00 to 12:00, blocked 12:00 to 13:00 and done from 13:00: the restored `blocked`
    is ended by the next live edge although that edge has the same object (OB-11), so no
    reader sees two current statuses."""
    t10, t11, t12, t13 = hours(10, 11, 12, 13)
    move(project, "blocked", t10)
    doing = move(project, "doing", t11, frm="blocked")
    move(project, "blocked", t12, frm="doing")
    move(project, "done", t13, frm="blocked")
    capture(project)
    assert current_at(project, "2026-03-01T11:30:00+00:00") == ["status:doing"]
    assert withdraw_source(project, "event:%d" % doing).status == "applied"
    assert windows(project) == [("status:blocked", t10, t12), ("status:blocked", t12, t13),
                                ("status:done", t13, None)]
    for hour, held in ((10, "blocked"), (11, "blocked"), (12, "blocked"), (14, "done")):
        assert current_at(project, "2026-03-01T%02d:30:00+00:00" % hour) == ["status:" + held]


def test_a_window_the_back_fill_closed_opens_again_when_its_edge_is_withdrawn(project):
    """`reconcile.classify` gives an edge that arrives after a later one that later edge's `valid_from`
    as its own `valid_until`, with no event that names the later edge. Moves doing, done, review with
    the `done` note captured last: withdrawing `review` gives `done` its open window back, and a
    rebuild from the record, which lands the notes in order, reaches the same state (OB-11)."""
    t10, t11, t12 = hours(10, 11, 12)
    doing = move(project, "doing", t10)
    done = move(project, "done", t11, frm="doing")
    review = move(project, "review", t12, frm="done")
    capture(project, {doing, review})
    capture(project, {done})
    assert windows(project) == [("status:doing", t10, t11), ("status:done", t11, t12),
                                ("status:review", t12, None)]
    receipt = withdraw_source(project, "event:%d" % review)
    assert receipt.status == "applied" and receipt.effects["priors_restored"] == 1
    assert windows(project) == [("status:doing", t10, t11), ("status:done", t11, None)]
    assert current_at(project, "2026-03-01T14:00:00+00:00") == ["status:done"]
    live = digest_of(project)
    rebuild_store(project)
    assert windows(project) == [("status:doing", t10, t11), ("status:done", t11, None)]
    assert digest_of(project) == live


def test_a_reopened_window_is_ended_again_by_the_next_live_edge(project):
    """Withdrawing the middle of three notes captured out of order: the window the withdrawn edge
    had closed is reopened and then ended by the edge that follows, as insertion order would have."""
    t10, t11, t12, t13 = hours(10, 11, 12, 13)
    a = move(project, "open", t10, frm="new")
    b = move(project, "doing", t11, frm="open")
    c = move(project, "review", t12, frm="doing")
    d = move(project, "done", t13, frm="review")
    capture(project, {a, c, d})
    capture(project, {b})
    assert windows(project) == [("status:doing", t11, t12), ("status:done", t13, None),
                                ("status:open", t10, t11), ("status:review", t12, t13)]
    withdraw_source(project, "event:%d" % c)
    assert windows(project) == [("status:doing", t11, t13), ("status:done", t13, None),
                                ("status:open", t10, t11)]
    assert current_at(project, "2026-03-01T12:30:00+00:00") == ["status:doing"]


def test_an_agent_fact_window_the_back_fill_closed_opens_again_when_its_edge_is_withdrawn(project):
    """The same for an agent fact: "done since 2026-03-01" is admitted after the record moves, so it is
    ended at the `review` move of 2026-03-02; withdrawing that note gives the fact its open window."""
    d1, d3 = "2026-02-28T10:00:00+00:00", "2026-03-02T10:00:00+00:00"
    doing = move(project, "doing", d1)
    review = move(project, "review", d3, frm="doing")
    capture(project, {doing, review})
    eid = note(project, "Since 2026-03-01 task t-9 is done with the review.", "2026-03-03T10:00:00+00:00")
    cid = propose(project, proposal(project, eid, "Since 2026-03-01 task t-9 is done", obj="done",
                                    valid_from="2026-03-01")).candidate_id
    assert admit(project, cid).status == "applied"
    (agent,) = llm_edges(project)
    assert (agent["obj_node"] or agent["obj_literal"], agent["valid_until"]) == ("done", d3)
    withdraw_source(project, "event:%d" % review)
    (after,) = [e for e in llm_edges(project) if e["status"] == "active"]
    assert after["valid_until"] is None
    assert current_at(project, "2026-03-04T00:00:00+00:00") == ["done"]    # the fact is a literal
    live = digest_of(project)
    rebuild_store(project)
    assert digest_of(project) == live


def test_a_restored_prior_ends_at_the_next_live_edge_not_where_its_snapshot_said(project):
    """The prior comes back with the end its supersede event recorded, which a later edge may since
    have cut short. Notes review (06:00) and doing (13:00) land first, then blocked (08:00) and open
    (11:00); withdrawing blocked gives `review` back its end from before blocked, 13:00, and `open`
    starts at 11:00: review ends there, as in a store that never held blocked."""
    t6, t8, t11, t13 = ("2026-03-01T%02d:00:00+00:00" % h for h in (6, 8, 11, 13))
    review = move(project, "review", t6)
    blocked = move(project, "blocked", t8, frm="review")
    opened = move(project, "open", t11, frm="blocked")
    doing = move(project, "doing", t13, frm="open")
    capture(project, {review, doing})
    capture(project, {blocked, opened})
    assert windows(project) == [("status:blocked", t8, t11), ("status:doing", t13, None),
                                ("status:open", t11, t13), ("status:review", t6, t8)]
    withdraw_source(project, "event:%d" % blocked)
    assert windows(project) == [("status:doing", t13, None), ("status:open", t11, t13),
                                ("status:review", t6, t11)]
    assert current_at(project, "2026-03-01T09:00:00+00:00") == ["status:review"]


def test_an_end_that_only_a_withdrawn_edge_made_is_given_up_when_the_edge_after_it_goes_too(project):
    """x (10:00), y (11:00), z (12:00); z lands first, so x and y are given ends from it at insert.
    Withdrawing z and then y leaves x with the end z gave it through y's snapshot, which no live edge
    makes: x is open again."""
    t10, t11, t12 = hours(10, 11, 12)
    x = move(project, "open", t10)
    y = move(project, "doing", t11, frm="open")
    z = move(project, "done", t12, frm="doing")
    capture(project, {z})
    capture(project, {x, y})
    assert windows(project) == [("status:doing", t11, t12), ("status:done", t12, None),
                                ("status:open", t10, t11)]
    withdraw_source(project, "event:%d" % z)
    assert windows(project) == [("status:doing", t11, None), ("status:open", t10, t11)]
    withdraw_source(project, "event:%d" % y)
    assert windows(project) == [("status:open", t10, None)]
    assert current_at(project, "2026-03-01T13:00:00+00:00") == ["status:open"]


def test_reverting_a_world_change_leaves_a_later_correction_in_force(project):
    """doing and review are stamped alike (a same-instant correction: review invalidates doing) and
    land after `open` (21:00), which had already ended doing's window. Withdrawing `open` gives
    doing back its open window and keeps it invalidated: it is not current again beside review."""
    t7, t21 = "2026-03-01T07:00:00+00:00", "2026-03-01T21:00:00+00:00"
    doing = move(project, "doing", t7)
    review = move(project, "review", t7, frm="doing")
    opened = move(project, "open", t21, frm="review")
    capture(project, {doing, opened})
    capture(project, {review})
    before = status_edges(project)
    assert before["status:doing"]["status"] == "invalidated" and before["status:review"]["status"] == "active"
    withdraw_source(project, "event:%d" % opened)
    now = status_edges(project)
    after = {k: (e["status"], e["valid_until"]) for k, e in now.items()}
    assert after["status:doing"] == ("invalidated", None)
    assert after["status:review"] == ("active", None)
    assert current_at(project, "2026-03-01T22:00:00+00:00") == ["status:review"]
    # the revert undid the world change and nothing else: doing was never made current again and corrected a
    # second time, so what review's correction recorded (who, and when the store stopped believing doing) stands
    kept = ("superseded_by_edge_id", "superseded_at")
    assert [now["status:doing"][k] for k in kept] == [before["status:doing"][k] for k in kept]
    assert now["status:doing"]["superseded_by_edge_id"] == now["status:review"]["edge_id"]
    assert len(supersede_events(project, now["status:doing"]["edge_id"], now["status:review"]["edge_id"])) == 1


def test_withdrawing_the_middle_of_three_same_instant_moves_leaves_the_last_one_current(project):
    """Moves doing, open and blocked are stamped alike: each corrects the one before. Withdrawing
    `open` gives `doing` back as active, and it is corrected again by `blocked`, the move that came
    after it, as in a store that never held `open`: no reader sees two statuses."""
    t = "2026-03-01T16:00:00+00:00"
    doing = move(project, "doing", t)
    opened = move(project, "open", t, frm="doing")
    blocked = move(project, "blocked", t, frm="open")
    capture(project)
    states = {e["obj_node"]: e["status"] for e in edges(project, predicate="status")}
    assert states == {"status:doing": "invalidated", "status:open": "invalidated",
                      "status:blocked": "active"}
    withdraw_source(project, "event:%d" % opened)
    after = {e["obj_node"]: e["status"] for e in edges(project, predicate="status")}
    assert after == {"status:doing": "invalidated", "status:open": "retracted", "status:blocked": "active"}
    assert current_at(project, "2026-03-01T17:00:00+00:00") == ["status:blocked"]
    live = digest_of(project)
    rebuild_store(project)
    assert digest_of(project) == live and current_at(project, "2026-03-01T17:00:00+00:00") == ["status:blocked"]
    # withdrawing the last of the three gives the middle one back, and the first stays corrected by it
    withdraw_source(project, "event:%d" % blocked)
    final = {e["obj_node"]: e["status"] for e in edges(project, predicate="status")}
    assert final == {"status:doing": "active", "status:open": "retracted", "status:blocked": "retracted"}
    assert current_at(project, "2026-03-01T17:00:00+00:00") == ["status:doing"]


def test_withdrawing_a_candidate_gives_back_the_window_its_edge_had_closed(project):
    """The same for a withdrawn candidate: an admitted agent fact "review since 2026-03-02" lands first,
    and a record move to `doing` on 2026-03-01 lands after it, so its window ends at the fact's start.
    Withdrawing the candidate retracts the fact and `doing` is open again."""
    eid = note(project, "Since 2026-03-02 task t-9 is in review.", "2026-03-03T10:00:00+00:00")
    cid = propose(project, proposal(project, eid, "Since 2026-03-02 task t-9 is in review", obj="review",
                                    valid_from="2026-03-02")).candidate_id
    assert admit(project, cid).status == "applied"
    doing = move(project, "doing", "2026-03-01T10:00:00+00:00")
    capture(project, {doing})
    before = {(e["obj_node"] or e["obj_literal"]): e["valid_until"] for e in edges(project, predicate="status")}
    assert before["status:doing"] == "2026-03-02T00:00:00+00:00"
    assert withdraw(project, cid).status == "applied"
    after = {(e["obj_node"] or e["obj_literal"]): (e["status"], e["valid_until"])
             for e in edges(project, predicate="status")}
    assert after["review"][0] == "retracted" and after["status:doing"] == ("active", None)
    assert current_at(project, "2026-03-05T00:00:00+00:00") == ["status:doing"]


def agent_fact(project, subject, word, since, note_ts):
    """An admitted agent fact "Since <date> task <subject> is <word>."; returns (event id, candidate id)."""
    eid = note(project, "Since %s task %s is %s." % (since, subject, word), note_ts)
    cid = propose(project, proposal(project, eid, "Since %s task %s is %s" % (since, subject, word),
                                    subject=subject, obj=word, valid_from=since)).candidate_id
    assert admit(project, cid).status == "applied"
    return eid, cid


def test_withdrawing_a_prior_after_a_restore_gives_back_priors_before_settling_any_window(project):
    """doing 10:00, open 13:00, and doing and open both at 16:00 (a later move to the held state starts its own
    edge); the doing was recorded first but lands last, so the open at 16:00 invalidates it. Withdrawing that open
    gives back `doing` at 16:00 and `open` at 13:00 ends there, whichever of the two the restore meets
    first: every prior is back before a window is settled."""
    t10, t13, t16 = hours(10, 13, 16)
    a = move(project, "doing", t10)
    b = move(project, "open", t13, frm="doing")
    d = move(project, "doing", t16, frm="open")           # recorded first, so the later `open` wins the tie
    c = move(project, "open", t16, frm="doing")
    capture(project, {a, b, c})
    capture(project, {d})
    states = {(e["obj_node"], e["valid_from"]): e["status"] for e in edges(project, predicate="status")}
    assert states[("status:doing", t16)] == "invalidated"
    withdraw_source(project, "event:%d" % c)
    assert windows(project) == [("status:doing", t10, t13), ("status:doing", t16, None), ("status:open", t13, t16)]
    assert current_at(project, "2026-03-01T17:00:00+00:00") == ["status:doing"]


def test_a_withdrawn_candidate_leaves_no_edge_to_come_back_when_the_edge_that_replaced_it_goes(project):
    """Two agent facts of one date: the later one corrects the earlier, which is `invalidated`. Withdrawing
    the earlier fact's candidate retracts its edge, so that withdrawing the later fact's note afterwards
    does not give it back as a current value."""
    a_event, a_cand = agent_fact(project, "t-1", "amber", "2026-03-05", "2026-03-09T01:00:00+00:00")
    b_event, b_cand = agent_fact(project, "t-1", "birch", "2026-03-05", "2026-03-09T02:00:00+00:00")
    by_word = {e["obj_literal"]: e for e in llm_edges(project)}
    assert by_word["amber"]["status"] == "invalidated" and by_word["birch"]["status"] == "active"
    assert withdraw(project, a_cand).status == "applied"
    assert {e["obj_literal"]: e["status"] for e in llm_edges(project)} == {"amber": "retracted", "birch": "active"}
    withdraw_source(project, "event:%d" % b_event)
    assert {e["obj_literal"]: e["status"] for e in llm_edges(project)} == {"amber": "retracted", "birch": "retracted"}
    assert current_at(project, "2026-03-06T00:00:00+00:00", task="t-1") == []


def test_a_window_settled_again_keeps_the_lineage_its_successor_was_admitted_over(project):
    """birch (03-03), a move to open (03-03 18:00), amber and cedar (03-05, cedar corrects amber). Withdrawing
    the move extends birch to cedar, the next live edge: that must not change what cedar was admitted
    over: the column `supersedes_edge_id` of cedar still names amber, which readers of the lineage follow
    and which a withdrawal of cedar's candidate reads back."""
    agent_fact(project, "t-1", "birch", "2026-03-03", "2026-03-09T01:00:00+00:00")
    mv = move(project, "open", "2026-03-03T18:00:00+00:00", task="t-1")
    capture(project, {mv})
    agent_fact(project, "t-1", "amber", "2026-03-05", "2026-03-09T02:00:00+00:00")
    _, cedar = agent_fact(project, "t-1", "cedar", "2026-03-05", "2026-03-09T03:00:00+00:00")
    by_word = {e["obj_literal"]: e for e in llm_edges(project)}
    assert by_word["cedar"]["supersedes_edge_id"] == by_word["amber"]["edge_id"]
    amber_id = by_word["amber"]["edge_id"]
    withdraw_source(project, "event:%d" % mv)
    by_word = {e["obj_literal"]: e for e in llm_edges(project)}
    assert by_word["birch"]["valid_until"] == "2026-03-05T00:00:00+00:00"
    assert by_word["cedar"]["supersedes_edge_id"] == amber_id
    assert withdraw(project, cedar).status == "applied"
    after = {e["obj_literal"]: e["status"] for e in llm_edges(project)}
    assert after == {"birch": "active", "amber": "active", "cedar": "retracted"}
    assert current_at(project, "2026-03-06T00:00:00+00:00", task="t-1") == ["amber"]


def test_a_refinement_never_ends_the_edge_it_refines_when_a_neighbour_is_withdrawn(project):
    """"blocked" (03-02) and its refinement "blocked on the review" (03-03) are both active; a move to open
    on 03-05 ends the refinement. Withdrawing the move leaves both open, as before it: a store that never
    held the move has the same two open edges, so the refinement does not end its prior."""
    agent_fact(project, "t-1", "blocked", "2026-03-02", "2026-03-09T01:00:00+00:00")
    agent_fact(project, "t-1", "blocked on the review", "2026-03-03", "2026-03-09T02:00:00+00:00")
    mv = move(project, "open", "2026-03-05T10:00:00+00:00", task="t-1")
    capture(project, {mv})
    withdraw_source(project, "event:%d" % mv)
    ends = {e["obj_literal"]: e["valid_until"] for e in llm_edges(project) if e["status"] == "active"}
    assert ends == {"blocked": None, "blocked on the review": None}


# ------------------------------------------------------------------ check round 2 (invariants B2): one instant, a refinement
def fact_rows(project, task="t-1"):
    """(word, status, valid_from day, valid_until, the word of the edge that corrected it) of every agent
    status fact of a task, sorted: what a withdrawal may change and what it must leave as it was."""
    rows = [e for e in llm_edges(project) if e["subj_node"] == task and e["predicate"] == "status"]
    word = {e["edge_id"]: e["obj_literal"] for e in rows}
    return sorted((e["obj_literal"], e["status"], e["valid_from"][:10], e["valid_until"],
                   word.get(e["superseded_by_edge_id"])) for e in rows)


def without(rows, *words):
    return [r for r in rows if r[0] not in words]


@pytest.mark.parametrize("verb", ["withdraw_source", "withdraw_candidate"])
def test_a_same_instant_refinement_keeps_both_edges_when_a_neighbour_is_withdrawn(project, verb):
    """"blocked" and its refinement "blocked on the review" start on the same day, and
    `reconcile.classify` keeps both active. "open" on a later day ends one of them. Withdrawing "open",
    by its note or by its candidate, leaves the pair as a store that never held "open" has it: both
    active and open, neither corrected by the other (the one-survivor rule of the restore is for
    values a withdrawal gives back, and a refinement is not a second value)."""
    agent_fact(project, "t-1", "blocked", "2026-03-02", "2026-03-09T01:00:00+00:00")
    agent_fact(project, "t-1", "blocked on the review", "2026-03-02", "2026-03-09T02:00:00+00:00")
    verdicts = {e["obj_literal"]: e["reconcile_verdict"] for e in llm_edges(project)}
    assert verdicts == {"blocked": "novel", "blocked on the review": "refines"}
    never_held = fact_rows(project)
    assert never_held == [("blocked", "active", "2026-03-02", None, None),
                          ("blocked on the review", "active", "2026-03-02", None, None)]
    eid, cid = agent_fact(project, "t-1", "open", "2026-03-05", "2026-03-09T03:00:00+00:00")
    assert [r for r in fact_rows(project) if r[3] == "2026-03-05T00:00:00+00:00"], fact_rows(project)
    if verb == "withdraw_source":
        receipt = withdraw_source(project, "event:%d" % eid)
    else:
        receipt = withdraw(project, cid)
    assert receipt.status == "applied", receipt
    after = fact_rows(project)
    assert without(after, "open") == never_held
    assert [r[:2] for r in after if r[0] == "open"] == [("open", "retracted")]
    assert current_at(project, "2026-03-06T00:00:00+00:00", task="t-1") == ["blocked", "blocked on the review"]


def test_a_prior_given_back_beside_its_same_instant_refinement_stays_active(project):
    """An owner supersede invalidates "blocked", whose same-instant refinement "blocked on the review" stays
    active; reverting the supersede gives "blocked" back. The refinement stands later in the record, so
    the one-survivor rule would correct "blocked" by it; a refinement is kin, not a rival value, and both
    are active again, as before the supersede."""
    agent_fact(project, "t-1", "blocked", "2026-03-02", "2026-03-09T01:00:00+00:00")
    agent_fact(project, "t-1", "blocked on the review", "2026-03-02", "2026-03-09T02:00:00+00:00")
    before = fact_rows(project)
    blocked = next(e for e in llm_edges(project) if e["obj_literal"] == "blocked")
    made = service.correct(project, blocked["edge_id"], "supersede", session="s-owner", by="owner",
                           record=appender(project, "s-owner"), reason="it was open",
                           replacement={"obj": "open", "obj_datatype": "string"})
    assert made.status == "applied", made
    assert ("blocked", "invalidated") in [r[:2] for r in fact_rows(project)]
    back = service.correct(project, made.effects["replacement_edge_id"], "revert", session="s-owner",
                           by="owner", record=appender(project, "s-owner"), reason="blocked was right")
    assert back.status == "applied", back
    assert fact_rows(project) == before
    assert current_at(project, "2026-03-03T00:00:00+00:00", task="t-1") == ["blocked", "blocked on the review"]


def test_a_withdrawal_never_corrects_an_edge_it_did_not_give_back(project):
    """"blocked", its refinement "blocked on the review" and "open", all on one day: "open" corrects one of
    the pair (the prior `reconcile.classify` picks) and the other stays active beside it, as the reconcile
    rule leaves them. "closed" on a later day ends one of the active edges. Withdrawing "closed" undoes that
    end and nothing else: every edge is as it was before "closed" was admitted, also the active edge that
    stands earlier in the record than "open" and that "closed" never touched."""
    agent_fact(project, "t-1", "blocked", "2026-03-02", "2026-03-09T01:00:00+00:00")
    agent_fact(project, "t-1", "blocked on the review", "2026-03-02", "2026-03-09T02:00:00+00:00")
    agent_fact(project, "t-1", "open", "2026-03-02", "2026-03-09T03:00:00+00:00")
    before = fact_rows(project)
    assert sorted(r[1] for r in before) == ["active", "active", "invalidated"], before
    _, cid = agent_fact(project, "t-1", "closed", "2026-03-05", "2026-03-09T04:00:00+00:00")
    ended = [r for r in fact_rows(project) if r[3] == "2026-03-05T00:00:00+00:00"]
    assert len(ended) == 1 and ended[0][1] == "active", fact_rows(project)
    assert withdraw(project, cid).status == "applied"
    assert without(fact_rows(project), "closed") == before


def test_a_prior_a_withdrawn_candidate_gives_back_is_corrected_by_the_later_value_of_its_instant(project):
    """"open", then "blocked" (which corrects it) and "blocked on the review" (which refines "blocked"), all
    on one day. Withdrawing the candidate of "blocked" gives "open" back; the refinement, admitted after
    "open", is the value of that instant in a store that never held "blocked", and corrects "open" there.
    The prior the candidate withdrawal gives back takes part in the one-survivor rule like a prior that a
    source withdrawal gives back, so "open" is corrected by it and only one value is current."""
    agent_fact(project, "t-1", "open", "2026-03-02", "2026-03-09T01:00:00+00:00")
    _, blocked = agent_fact(project, "t-1", "blocked", "2026-03-02", "2026-03-09T02:00:00+00:00")
    agent_fact(project, "t-1", "blocked on the review", "2026-03-02", "2026-03-09T03:00:00+00:00")
    assert {e["obj_literal"]: e["status"] for e in llm_edges(project)} == {
        "open": "invalidated", "blocked": "active", "blocked on the review": "active"}
    assert withdraw(project, blocked).status == "applied"
    assert without(fact_rows(project), "blocked") == [
        ("blocked on the review", "active", "2026-03-02", None, None),
        ("open", "invalidated", "2026-03-02", None, "blocked on the review")]
    assert current_at(project, "2026-03-03T00:00:00+00:00", task="t-1") == ["blocked on the review"]


@pytest.mark.parametrize("verb", ["withdraw_source", "withdraw_candidate"])
@pytest.mark.parametrize("day, gone, partner", [
    ("2026-03-02", "blocked on the review", "blocked"),        # "open" met the refinement
    ("2026-03-03", "blocked", "blocked on the review")])       # "open" met the edge it refines
def test_a_correction_that_met_a_withdrawn_edge_passes_to_its_same_instant_refinement_partner(
        project, verb, day, gone, partner):
    """"blocked" and its refinement "blocked on the review" start at one instant, and "open" arrives after
    them at that instant: `reconcile.classify` takes one of the pair as the prior (the smaller edge id;
    the two days give one case each) and corrects it, and the other stays active. Withdrawing the one it
    corrected leaves a store that never held it, where "open" meets the partner and corrects it: one
    value, "open", is current."""
    agent_fact(project, "t-1", "blocked", day, "2026-03-09T01:00:00+00:00")
    agent_fact(project, "t-1", "blocked on the review", day, "2026-03-09T02:00:00+00:00")
    agent_fact(project, "t-1", "open", day, "2026-03-09T03:00:00+00:00")
    rows = {e["obj_literal"]: e for e in llm_edges(project)}
    assert (rows[gone]["status"], rows[partner]["status"]) == ("invalidated", "active"), fact_rows(project)
    event = next(e["id"] for e in events(project) if gone + "." in (json.loads(e["data"]).get("body") or ""))
    cand = next(c for c in service.candidates(project) if c.status == "admitted"
                and c.body["fact"]["obj"] == gone)
    receipt = (withdraw_source(project, "event:%d" % event) if verb == "withdraw_source"
               else withdraw(project, cand.candidate_id))
    assert receipt.status == "applied", receipt
    assert without(fact_rows(project), gone) == sorted([
        (partner, "invalidated", day, None, "open"), ("open", "active", day, None, None)])
    later = day[:-2] + "%02d" % (int(day[-2:]) + 1) + "T00:00:00+00:00"
    assert current_at(project, later, task="t-1") == ["open"]


def test_a_successor_that_refines_the_prior_given_back_leaves_both_active(project):
    """"open", then "cedar" (which corrects it) and "open again" (which corrects "cedar"), all at one instant.
    Withdrawing "cedar" gives "open" back; the next edge of the chain, "open again", holds the value of
    "open", so in a store that never held "cedar" it refines "open" and both stay active: the restore
    does not correct "open" by it."""
    agent_fact(project, "t-1", "open", "2026-03-02", "2026-03-09T01:00:00+00:00")
    _, cedar = agent_fact(project, "t-1", "cedar", "2026-03-02", "2026-03-09T02:00:00+00:00")
    agent_fact(project, "t-1", "open again", "2026-03-02", "2026-03-09T03:00:00+00:00")
    assert {e["obj_literal"]: e["status"] for e in llm_edges(project)} == {
        "open": "invalidated", "cedar": "invalidated", "open again": "active"}
    assert withdraw(project, cedar).status == "applied"
    assert without(fact_rows(project), "cedar") == [("open", "active", "2026-03-02", None, None),
                                                    ("open again", "active", "2026-03-02", None, None)]
    assert current_at(project, "2026-03-03T00:00:00+00:00", task="t-1") == ["open", "open again"]


def test_a_refinement_whose_refined_edge_is_withdrawn_ends_the_earlier_window(project):
    """A move to doing on 03-01, then the fact "open" on 03-02, which ends doing there, and "open again" on
    03-02, which refines "open". Withdrawing "open" gives doing its window back; "open again" no longer
    refines anything here, and a store that never held "open" has it end doing on 03-02 (it is a
    contradiction of doing there). A refinement never ends the edge it refines, but it ends others."""
    mv = move(project, "doing", "2026-03-01T00:00:00+00:00", task="t-1")
    capture(project, {mv})
    _, opened = agent_fact(project, "t-1", "open", "2026-03-02", "2026-03-09T01:00:00+00:00")
    agent_fact(project, "t-1", "open again", "2026-03-02", "2026-03-09T02:00:00+00:00")
    assert {e["obj_literal"]: e["reconcile_verdict"] for e in llm_edges(project)} == {
        "open": "contradicts", "open again": "refines"}
    assert withdraw(project, opened).status == "applied"
    assert windows(project, task="t-1") == [("open again", "2026-03-02T00:00:00+00:00", None),
                                            ("status:doing", "2026-03-01T00:00:00+00:00",
                                             "2026-03-02T00:00:00+00:00")]
    assert current_at(project, "2026-03-02T12:00:00+00:00", task="t-1") == ["open again"]


def test_two_refinements_of_a_withdrawn_edge_meet_each_other_as_in_a_store_that_never_held_it(project):
    """"blocked", then "blocked on the review by bob" and "blocked on the review", all on one day for task t-2:
    `reconcile.classify` takes "blocked" as the prior of each, so both refine it. Withdrawing "blocked" leaves
    the two without the edge they refined; in a store that never held it, "blocked on the review" arrives
    after "blocked on the review by bob", does not hold its value, and corrects it."""
    _, blocked = agent_fact(project, "t-2", "blocked", "2026-03-02", "2026-03-09T01:00:00+00:00")
    agent_fact(project, "t-2", "blocked on the review by bob", "2026-03-02", "2026-03-09T02:00:00+00:00")
    agent_fact(project, "t-2", "blocked on the review", "2026-03-02", "2026-03-09T03:00:00+00:00")
    rows = {e["edge_id"]: e for e in llm_edges(project)}
    word = {k: e["obj_literal"] for k, e in rows.items()}
    assert sorted((e["obj_literal"], e["reconcile_verdict"], word.get(e["supersedes_edge_id"]))
                  for e in rows.values() if e["obj_literal"] != "blocked") == [
        ("blocked on the review", "refines", "blocked"), ("blocked on the review by bob", "refines", "blocked")]
    assert withdraw(project, blocked).status == "applied"
    assert without(fact_rows(project, task="t-2"), "blocked") == [
        ("blocked on the review", "active", "2026-03-02", None, None),
        ("blocked on the review by bob", "invalidated", "2026-03-02", None, "blocked on the review")]
    assert current_at(project, "2026-03-03T00:00:00+00:00", task="t-2") == ["blocked on the review"]


def test_a_refinement_that_stood_before_its_prior_arrived_still_ends_its_window(project):
    """"blocked on the review" on 03-02 is admitted first, then "blocked" on 03-01: `reconcile.classify`
    back-fills the window of "blocked" to 03-02, the start of the edge already there, whatever its value.
    "cedar" on 03-02 then corrects "blocked on the review". Withdrawing "cedar" gives "blocked on the review"
    back, and "blocked" keeps its end: a store that never held "cedar" back-filled it the same way, because
    there the refinement stood in the store before "blocked" arrived."""
    agent_fact(project, "t-1", "blocked on the review", "2026-03-02", "2026-03-09T01:00:00+00:00")
    agent_fact(project, "t-1", "blocked", "2026-03-01", "2026-03-09T02:00:00+00:00")
    _, cedar = agent_fact(project, "t-1", "cedar", "2026-03-02", "2026-03-09T03:00:00+00:00")
    assert ("blocked", "active", "2026-03-01", "2026-03-02T00:00:00+00:00", None) in fact_rows(project)
    assert withdraw(project, cedar).status == "applied"
    assert without(fact_rows(project), "cedar") == [
        ("blocked", "active", "2026-03-01", "2026-03-02T00:00:00+00:00", None),
        ("blocked on the review", "active", "2026-03-02", None, None)]
    assert current_at(project, "2026-03-02T12:00:00+00:00", task="t-1") == ["blocked on the review"]


def test_a_later_value_placed_against_a_withdrawn_edge_refines_the_prior_given_back(project):
    """"open" on 03-01, "blocked" on 03-01 (which corrects it) and "open again" on 03-02, placed as a
    contradiction of "blocked", whose window it ends. Withdrawing "blocked" gives "open" back; in a store
    that never held "blocked", "open again" meets "open" and refines it, so "open" stays open and both are
    current from 03-02: the rule that ends a window applies the refines rule to the edge it meets now."""
    agent_fact(project, "t-1", "open", "2026-03-01", "2026-03-09T01:00:00+00:00")
    _, blocked = agent_fact(project, "t-1", "blocked", "2026-03-01", "2026-03-09T02:00:00+00:00")
    agent_fact(project, "t-1", "open again", "2026-03-02", "2026-03-09T03:00:00+00:00")
    assert {e["obj_literal"]: e["reconcile_verdict"] for e in llm_edges(project)}["open again"] == "contradicts"
    assert withdraw(project, blocked).status == "applied"
    assert windows(project, task="t-1") == [("open", "2026-03-01T00:00:00+00:00", None),
                                            ("open again", "2026-03-02T00:00:00+00:00", None)]
    assert current_at(project, "2026-03-03T00:00:00+00:00", task="t-1") == ["open", "open again"]


def test_an_edge_a_same_instant_correction_invalidated_still_ends_the_window_it_ended(project):
    """"blocked" on 03-01; moves to review and to doing at 03-02 00:00, review later in the record and
    captured first, so doing lands corrected by it; then "blocked on the review" on 03-02, which corrects
    review. Withdrawing the review note gives doing back, corrected now by "blocked on the review", and
    reopens "blocked", whose window review had ended. A store that never held review has doing end
    "blocked" at 03-02 before "blocked on the review" corrects doing: an invalidated edge still ends the
    window it ended when it arrived, although the value that corrected it refines "blocked"."""
    agent_fact(project, "t-1", "blocked", "2026-03-01", "2026-03-09T01:00:00+00:00")
    doing = move(project, "doing", "2026-03-02T00:00:00+00:00", task="t-1")
    review = move(project, "review", "2026-03-02T00:00:00+00:00", task="t-1")
    capture(project, {review})
    capture(project, {doing})
    agent_fact(project, "t-1", "blocked on the review", "2026-03-02", "2026-03-09T02:00:00+00:00")
    states = {(e["obj_node"] or e["obj_literal"]): e["status"] for e in edges(project, predicate="status")}
    assert states == {"blocked": "active", "status:doing": "invalidated", "status:review": "invalidated",
                      "blocked on the review": "active"}, states
    withdraw_source(project, "event:%d" % review)
    assert windows(project, task="t-1") == [
        ("blocked", "2026-03-01T00:00:00+00:00", "2026-03-02T00:00:00+00:00"),
        ("blocked on the review", "2026-03-02T00:00:00+00:00", None)]
    assert current_at(project, "2026-03-02T12:00:00+00:00", task="t-1") == ["blocked on the review"]


@pytest.mark.parametrize("first", ["the source", "the candidate"])
def test_an_edge_moved_onto_a_candidate_claim_goes_when_that_candidate_is_withdrawn(project, first):
    """Two notes say "Since 2026-03-02 task t-1 is blocked"; the first admitted fact writes the edge and the
    second only corroborates it. Withdrawing the first note moves the edge onto the second candidate's
    claim, its last support; withdrawing that candidate then retracts the edge, as it would an edge it
    wrote, and nothing is served, as in a store that never held either. The other order ends the same."""
    one_event, one = agent_fact(project, "t-1", "blocked", "2026-03-02", "2026-03-09T01:00:00+00:00")
    eid = note(project, "Since 2026-03-02 task t-1 is blocked.", "2026-03-09T02:00:00+00:00")
    two = propose(project, proposal(project, eid, "Since 2026-03-02 task t-1 is blocked", subject="t-1",
                                    obj="blocked", valid_from="2026-03-02")).candidate_id
    made = admit(project, two)
    assert (made.status, made.effects["effect"]) == ("applied", "corroborated"), made
    (edge,) = llm_edges(project)
    if first == "the source":
        assert withdraw_source(project, "event:%d" % one_event).status == "applied"
        (moved,) = llm_edges(project)
        assert moved["status"] == "active" and moved["source_doc_id"] != edge["source_doc_id"]
        assert [c["claim_ref"] for c in claims(project, edge["edge_id"]) if c["status"] == "live"] == [
            "candidate:" + two]
        out = withdraw(project, two)
        assert (out.status, out.effects["undone"].get("edge")) == ("applied", "retracted"), out
    else:
        assert withdraw(project, two).status == "applied"
        assert withdraw_source(project, "event:%d" % one_event).status == "applied"
    (gone,) = llm_edges(project)
    assert gone["status"] == "retracted"
    assert current_at(project, "2026-03-03T00:00:00+00:00", task="t-1") == []


def test_a_chain_of_same_date_corrections_stays_one_chain_when_its_links_are_withdrawn(project):
    """Agent facts cedar, fjord, birch and delta of one date, admitted in that order: each corrects the one
    before. Withdrawing fjord and then delta leaves cedar corrected by birch, the fact admitted after it,
    and birch current: the corrections stay one chain, so the two never come back together."""
    _, cedar = agent_fact(project, "t-1", "cedar", "2026-03-06", "2026-03-09T01:00:00+00:00")
    _, fjord = agent_fact(project, "t-1", "fjord", "2026-03-06", "2026-03-09T02:00:00+00:00")
    _, birch = agent_fact(project, "t-1", "birch", "2026-03-06", "2026-03-09T03:00:00+00:00")
    _, delta = agent_fact(project, "t-1", "delta", "2026-03-06", "2026-03-09T04:00:00+00:00")
    assert {e["obj_literal"]: e["status"] for e in llm_edges(project)} == {
        "cedar": "invalidated", "fjord": "invalidated", "birch": "invalidated", "delta": "active"}
    assert withdraw(project, fjord).status == "applied"
    by_word = {e["obj_literal"]: e for e in llm_edges(project)}
    assert {w: e["status"] for w, e in by_word.items()} == {
        "cedar": "invalidated", "fjord": "retracted", "birch": "invalidated", "delta": "active"}
    # cedar is corrected by birch, the edge that stood after fjord in the chain, as in a store that never held
    # fjord (birch corrected cedar there, and delta corrected birch), and not by delta, the last one
    assert by_word["cedar"]["superseded_by_edge_id"] == by_word["birch"]["edge_id"]
    assert by_word["birch"]["superseded_by_edge_id"] == by_word["delta"]["edge_id"]
    assert withdraw(project, delta).status == "applied"
    assert {e["obj_literal"]: e["status"] for e in llm_edges(project)} == {
        "cedar": "invalidated", "fjord": "retracted", "birch": "active", "delta": "retracted"}
    assert current_at(project, "2026-03-07T00:00:00+00:00", task="t-1") == ["birch"]


def test_priors_that_come_back_together_at_one_instant_leave_the_one_the_record_puts_last(project):
    """Three moves of one task stamped alike, recorded x, y, z: z lands first and the other two, which stand
    earlier in the record, are corrected by it as they arrive. Withdrawing z gives both back, and only
    one can be current: y, the later of the two in the record, as in a store that never held z."""
    t = "2026-03-01T16:00:00+00:00"
    x = move(project, "doing", t)
    y = move(project, "done", t, frm="doing")
    z = move(project, "review", t, frm="done")
    capture(project, {z})
    capture(project, {x})
    capture(project, {y})
    states = {e["obj_node"]: e["status"] for e in edges(project, predicate="status")}
    assert states == {"status:doing": "invalidated", "status:done": "invalidated", "status:review": "active"}
    withdraw_source(project, "event:%d" % z)
    after = {e["obj_node"]: e["status"] for e in edges(project, predicate="status")}
    assert after == {"status:doing": "invalidated", "status:done": "active", "status:review": "retracted"}
    assert current_at(project, "2026-03-01T17:00:00+00:00") == ["status:done"]


def test_the_corrections_of_one_instant_stay_one_chain_when_a_move_in_the_middle_is_withdrawn(project):
    """Moves doing, open, blocked and review are stamped alike: each corrects the one before. Withdrawing
    `open` gives `doing` back and corrects it by `blocked`, the move that stood after `open`, as in a store
    that never held `open` (blocked corrected doing there), and not by `review`, the last one: the chain
    doing, blocked, review stays one chain."""
    t = "2026-03-01T16:00:00+00:00"
    move(project, "doing", t)
    opened = move(project, "open", t, frm="doing")
    move(project, "blocked", t, frm="open")
    move(project, "review", t, frm="blocked")
    capture(project)
    withdraw_source(project, "event:%d" % opened)
    now = status_edges(project)
    assert {k: e["status"] for k, e in now.items()} == {
        "status:doing": "invalidated", "status:open": "retracted", "status:blocked": "invalidated",
        "status:review": "active"}
    assert now["status:doing"]["superseded_by_edge_id"] == now["status:blocked"]["edge_id"]
    assert now["status:blocked"]["superseded_by_edge_id"] == now["status:review"]["edge_id"]
    assert current_at(project, "2026-03-01T17:00:00+00:00") == ["status:review"]


def test_a_multi_valued_key_keeps_every_edge_when_one_of_them_is_withdrawn(project):
    """`sourced_from` is multi-valued: a task is sourced from several decisions at once, all of them current.
    Three admitted facts of one date; withdrawing one leaves the other two active and open, because the
    rules that settle windows and leave one survivor are for single-valued keys."""
    cands = {}
    for i, decision in enumerate(("dec-aaaaaaaaaaaa", "dec-bbbbbbbbbbbb", "dec-cccccccccccc")):
        quote = "Since 2026-03-05 task t-1 is sourced from %s" % decision
        eid = note(project, quote + ".", "2026-03-09T0%d:00:00+00:00" % (i + 1))
        receipt = propose(project, proposal(project, eid, quote, subject="t-1", predicate="sourced_from",
                                            obj=decision, datatype="node", valid_from="2026-03-05"))
        assert receipt.status == "checked", receipt.checks
        assert admit(project, receipt.candidate_id).status == "applied"
        cands[decision] = receipt.candidate_id

    def held():
        return {e["obj_node"]: (e["status"], e["valid_until"]) for e in llm_edges(project)
                if e["predicate"] == "sourced_from"}
    assert held() == {d: ("active", None) for d in cands}
    assert withdraw(project, cands["dec-cccccccccccc"]).status == "applied"
    assert held() == {"dec-aaaaaaaaaaaa": ("active", None), "dec-bbbbbbbbbbbb": ("active", None),
                      "dec-cccccccccccc": ("retracted", None)}


def test_a_window_closed_by_another_live_edge_stays_closed_when_a_neighbour_is_withdrawn(project):
    """The reopening is for an end that only the withdrawn edge made. Two later notes start at the
    same instant; withdrawing one of them leaves the other still ending the earlier edge there."""
    t10, t11 = hours(10, 11)
    doing = move(project, "doing", t10)
    a = move(project, "done", t11, frm="doing")
    b = move(project, "review", t11, frm="doing")
    capture(project, {doing, a, b})
    before = [w for w in windows(project) if w[0] == "status:doing"]
    assert before == [("status:doing", t10, t11)]
    withdraw_source(project, "event:%d" % b)
    assert [w for w in windows(project) if w[0] == "status:doing"] == [("status:doing", t10, t11)]


# ------------------------------------------------------------------ a withdrawn transcript
TRANSCRIPT_TEXT = "The kestrel queue drains nightly at two."


def register_transcript(project, sid="s-t", text=TRANSCRIPT_TEXT):
    """A session with a registered transcript file and one record event, as `wiki recover` meets it."""
    from pathlib import Path
    from alpaca import db as record
    folder = Path(project, ".alpaca", "transcripts")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (sid + ".jsonl")
    path.write_text(json.dumps({"type": "user", "timestamp": "2026-09-01T10:00:00Z",
                                "message": {"content": text}}) + "\n", encoding="utf-8")
    conn = record.connect(str(project))
    try:
        record.upsert(conn, "sessions", "sid", {"sid": sid, "started": "2026-09-01T00:00:00+00:00",
                                                "transcript": str(path)})
    finally:
        conn.close()
    append(project, "result", {"body": "The lantern rota moved to Tuesday."}, session=sid)
    return path


def transcript_blocks(project):
    with store(project) as conn:
        return conn.execute("SELECT COUNT(*) FROM blocks WHERE status='active' "
                            "AND doc_id LIKE 'raw/transcripts/%'").fetchone()[0]


def test_a_withdrawn_transcript_does_not_stop_wiki_recover(project):
    """`wiki recover` verifies the active blocks of every captured transcript. A withdrawn transcript
    keeps its projection file and has no active block, so it is verified by its bytes only, as a
    withdrawn event is, on the live store and on a store rebuilt from the record."""
    register_transcript(project)
    first = recovery.run(project, session="s-recover")
    assert first["status"] == "ok" and first["verified_transcripts"] == 1
    assert withdraw_source(project, "transcript:s-t").status == "applied"
    assert transcript_blocks(project) == 0
    again = recovery.run(project, session="s-recover")
    assert again["status"] == "ok" and again["verified_transcripts"] == 0
    assert again["withdrawn_transcripts"] == ["s-t"]
    rebuilt = rebuild_store(project)
    assert rebuilt["status"] == "ok" and rebuilt["withdrawn_transcripts"] == ["s-t"]
    assert transcript_blocks(project) == 0
    assert "kestrel" not in service.context(project, "When does the kestrel queue drain?").lower()
    # a transcript that was not withdrawn is still verified
    register_transcript(project, sid="s-u", text="The osprey queue drains nightly at four.")
    other = recovery.run(project, session="s-recover")
    assert other["verified_transcripts"] == 1 and other["withdrawn_transcripts"] == ["s-t"]


def test_a_transcript_captured_before_source_versions_existed_can_be_withdrawn(project):
    """A transcript of the live store has no `source_versions` row (it was captured before schema 4),
    and `servable` finds it by its header. The withdrawal verb has to as well: it was refused as an
    unknown source while the transcript stayed served."""
    register_transcript(project)
    recovery.run(project, session="s-recover")
    with store(project) as conn:
        conn.execute("DROP TRIGGER IF EXISTS no_delete_source_versions")
        conn.execute("DELETE FROM source_versions WHERE source_ref LIKE 'transcript:%'")
        assert conn.execute("SELECT COUNT(*) FROM source_versions WHERE source_ref LIKE 'transcript:%'"
                            ).fetchone()[0] == 0
    assert transcript_blocks(project) >= 1
    assert "kestrel" in service.context(project, "When does the kestrel queue drain?").lower()
    receipt = withdraw_source(project, "transcript:s-t")
    assert (receipt.status, receipt.reason) == ("applied", None) and receipt.effects["blocks_retired"] >= 1
    assert transcript_blocks(project) == 0
    assert "kestrel" not in service.context(project, "When does the kestrel queue drain?").lower()
    answer = service.query(project, "When does the kestrel queue drain?")
    assert "kestrel" not in json.dumps({k: answer[k] for k in ("answer_text", "citations", "sources")}).lower()
    assert withdraw_source(project, "transcript:s-t").status == "duplicate"
    assert withdraw_source(project, "transcript:s-nowhere").reason == "unknown-source"


def test_a_header_lookup_never_takes_a_source_whose_name_only_contains_the_ref(project):
    """The lookup narrows by the name in the header and then compares the whole identity: withdrawing
    `transcript:s-t` leaves the transcript of the session `s-t2` served."""
    register_transcript(project, sid="s-t")
    register_transcript(project, sid="s-t2", text="The osprey queue drains nightly at four.")
    recovery.run(project, session="s-recover")
    with store(project) as conn:
        conn.execute("DROP TRIGGER IF EXISTS no_delete_source_versions")
        conn.execute("DELETE FROM source_versions WHERE source_ref LIKE 'transcript:%'")
    assert withdraw_source(project, "transcript:s-t").status == "applied"
    with store(project) as conn:
        left = conn.execute("SELECT COUNT(*) FROM blocks WHERE status='active' AND doc_id LIKE ?",
                            ("raw/transcripts/%transcript-s-t2-%",)).fetchone()[0]
        gone = conn.execute("SELECT COUNT(*) FROM blocks WHERE status='active' AND doc_id LIKE ? "
                            "AND doc_id NOT LIKE ?", ("raw/transcripts/%transcript-s-t-%",
                                                      "raw/transcripts/%transcript-s-t2-%")).fetchone()[0]
    assert left >= 1 and gone == 0
    assert "osprey" in service.context(project, "When does the osprey queue drain?").lower()


def test_a_source_copy_captured_before_source_versions_existed_can_be_withdrawn(project):
    """The header lookup is for every ref kind: a proof page copy with no `source_versions` row is found
    by its `source_ref:` line."""
    import hashlib
    from pathlib import Path
    from alpaca.wiki.ingest import drain
    text = "# Proof t-1\n\n## At a glance\n\nThe ember queue stalled because the cinder lock stayed held.\n"
    rel = ".alpaca/proofs/op-1/t-1.md"
    path = Path(project, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    eid = append(project, "proof-report", {"path": rel, "sha256": hashlib.sha256(text.encode()).hexdigest(),
                                           "bytes": len(text.encode()), "sections": ["At a glance"],
                                           "evidence": []}, session="s-src", op="op-1", ref="t-1")
    drain.capture(str(project), events(project, {eid}))
    with store(project) as conn:
        conn.execute("DROP TRIGGER IF EXISTS no_delete_source_versions")
        conn.execute("DELETE FROM source_versions WHERE source_ref=?", ("proof:" + rel,))
        copies = conn.execute("SELECT COUNT(*) FROM blocks WHERE status='active' "
                              "AND doc_id LIKE 'raw/sources/proof/%'").fetchone()[0]
    assert copies >= 1
    receipt = withdraw_source(project, "proof:" + rel)
    assert (receipt.status, receipt.reason) == ("applied", None) and receipt.effects["blocks_retired"] >= 1
    with store(project) as conn:
        assert conn.execute("SELECT COUNT(*) FROM blocks WHERE status='active' "
                            "AND doc_id LIKE 'raw/sources/proof/%'").fetchone()[0] == 0
    assert "cinder lock" not in service.context(project, "Why did the ember queue stall?")


# ------------------------------------------------------------------ a document ref names its source
# Check round 3 invariants B1: `wiki withdraw document:<doc id>` of a captured note answered
# `applied` with an exclusion under the document ref, which names no source the servable predicate,
# the expected sets, the absorb skip or the replay look for (the store names that note `event:N`), so
# the note stayed servable on a knowledge-time pinned read and the next capture served it again.
def doc_of(project, ref):
    """The document that the store's `source_versions` row of this source names."""
    with store(project) as conn:
        return conn.execute("SELECT doc_id FROM source_versions WHERE source_ref=? ORDER BY version_seq",
                            (ref,)).fetchone()[0]


def appender_at(project, ts, session="s-owner"):
    """The CLI's appender with the record clock at `ts`. The withdrawal stamps the store with its
    record event's time, so a withdrawal recorded at `ts` lies after a knowledge time taken now."""
    from alpaca import db as record
    from alpaca.wiki.clock import FixedClock

    def append_wiki(kind, data):
        conn = record.connect(str(project))
        try:
            return record.append_event(conn, session=session, actor="alpaca-wiki", kind=kind,
                                       data=data, clock=FixedClock(ts))["id"]
        finally:
            conn.close()
    return append_wiki


def active_blocks_of(project, doc_ids):
    with store(project) as conn:
        return sum(conn.execute("SELECT COUNT(*) FROM blocks WHERE status='active' AND doc_id=?",
                                (d,)).fetchone()[0] for d in doc_ids)


def test_a_document_ref_of_a_captured_note_withdraws_the_event_it_stands_for(project):
    """`document:<doc id>` and `wiki:<doc id>` of a captured event note withdraw `event:N`: the record
    event and the receipt name it, its versions and claims are withdrawn, its rule edge and the
    admitted fact on it are retracted, the candidate follows, status counts the event as withdrawn, a
    read pinned to a knowledge time before the withdrawal serves none of it, the next capture of
    every event lands none of the notes again, and a rebuild from the record gives an equal digest."""
    from datetime import datetime, timedelta, timezone
    from alpaca.wiki.clock import now_iso
    from alpaca.wiki.servable import servable_ref
    rota = at(project, "result", {"body": "The lantern rota moved to Tuesday."}, T09, op="op-8")
    done = move(project, "done", T10)
    fact, cid = admitted_fact(project, body="Task t-10 is blocked on the review.",
                              quote="Task t-10 is blocked", ts=T11, subject="t-10")
    capture(project)
    question = "When did the lantern rota move?"
    assert "Tuesday" in json.dumps(service.query(project, question))
    assert {e["subj_node"]: e["status"] for e in edges(project, predicate="status")} == \
        {"t-9": "active", "t-10": "active"}
    docs = {n: doc_of(project, "event:%d" % n) for n in (rota, done, fact)}
    k_before = now_iso()
    later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    # the control: a read pinned to this knowledge time sees the note before any withdrawal
    assert "Tuesday" in json.dumps(service.query(project, question, knowledge_as_of=k_before))
    receipts = [service.withdraw_source(project, ref, session="s-owner", reason="wrong source",
                                        record=appender_at(project, later))
                for ref in ("document:" + docs[rota], "wiki:" + docs[done], "document:" + docs[fact])]
    assert [(r.status, r.reason, r.effects["source_ref"], r.effects["requested_ref"])
            for r in receipts] == [("applied", None, "event:%d" % n, "document:" + docs[n])
                                   for n in (rota, done, fact)]
    assert [r.effects["claims_withdrawn"] for r in receipts] == [0, 1, 1]
    assert [(d["candidate_id"], d["from"], d["to"]) for d in receipts[2].effects["dependents"]] == \
        [(cid, "admitted", "retracted")]
    assert [(e["data"]["source_ref"], e["data"]["requested_ref"], e["data"]["dependents"])
            for e in wiki_events(project, "wiki-withdrawal")] == [
        ("event:%d" % rota, "document:" + docs[rota], []), ("event:%d" % done, "document:" + docs[done], []),
        ("event:%d" % fact, "document:" + docs[fact], [cid])]
    with store(project) as conn:
        assert [r[0] for r in conn.execute("SELECT source_ref FROM exclusions ORDER BY rowid")] == \
            ["event:%d" % n for n in (rota, done, fact)]
        for n in (rota, done, fact):
            assert not servable_ref(conn, "event:%d" % n)
            assert {r[0] for r in conn.execute("SELECT status FROM source_versions WHERE source_ref=?",
                                               ("event:%d" % n,))} == {"withdrawn"}
            assert {r[0] for r in conn.execute("SELECT status FROM edge_origin WHERE source_ref=?",
                                               ("event:%d" % n,))} <= {"withdrawn"}
    assert {e["subj_node"]: (e["status"], e["retraction_reason"])
            for e in edges(project, predicate="status")} == \
        {"t-9": ("retracted", "source-withdrawn"), "t-10": ("retracted", "source-withdrawn")}
    assert row_of(project, cid)["status"] == "retracted"
    status = service.status(project)["capture"]
    assert status["withdrawn_events"] == 3 and status["missing_event_ids"] == []
    assert service._uncaptured_events(project, service.Config.for_project(project)) == []

    def served():
        return [service.query(project, question), service.query(project, question, knowledge_as_of=k_before),
                service.context(project, question)]

    assert active_blocks_of(project, docs.values()) == 0
    assert "Tuesday" not in json.dumps(served())
    # the next capture of every event, as the Stop hook or the admit capture step would make it
    capture(project)
    assert active_blocks_of(project, docs.values()) == 0
    assert [e["status"] for e in edges(project, predicate="status")] == ["retracted", "retracted"]
    assert "Tuesday" not in json.dumps(served())
    # the same source asked for again, by either name, is a duplicate
    again = withdraw_source(project, "document:" + docs[rota])
    assert (again.status, again.effects) == ("duplicate", {"source_ref": "event:%d" % rota,
                                                           "requested_ref": "document:" + docs[rota]})
    assert withdraw_source(project, "event:%d" % done).status == "duplicate"
    # wiki recover from the record
    live = digest_of(project)
    report = rebuild_store(project)["candidates"]
    assert report["source_withdrawals_applied"] == 3 and report["skipped"] == {}
    assert digest_of(project) == live and active_blocks_of(project, docs.values()) == 0
    assert "Tuesday" not in json.dumps([service.query(project, question), service.context(project, question)])
    assert row_of(project, cid)["status"] == "retracted"


def test_a_document_ref_of_a_transcript_withdraws_the_transcript(project):
    """The document of a captured transcript stands for `transcript:<session>`: the receipt names it,
    the transcript is not served, and the next capture of the session and `wiki recover`, on the live
    store and on a rebuild, leave it withdrawn."""
    from alpaca.wiki.servable import servable_ref
    register_transcript(project)
    recovery.run(project, session="s-recover")
    doc = doc_of(project, "transcript:s-t")
    question = "When does the kestrel queue drain?"
    assert transcript_blocks(project) >= 1 and "kestrel" in service.context(project, question).lower()
    receipt = withdraw_source(project, "document:" + doc)
    assert (receipt.status, receipt.reason, receipt.effects["source_ref"], receipt.effects["requested_ref"]) \
        == ("applied", None, "transcript:s-t", "document:" + doc)
    with store(project) as conn:
        assert not servable_ref(conn, "transcript:s-t")
        assert [r[0] for r in conn.execute("SELECT source_ref FROM exclusions")] == ["transcript:s-t"]
    assert transcript_blocks(project) == 0 and "kestrel" not in service.context(project, question).lower()
    assert withdraw_source(project, "document:" + doc).status == "duplicate"
    again = recovery.run(project, session="s-recover")
    assert again["status"] == "ok" and again["withdrawn_transcripts"] == ["s-t"]
    assert transcript_blocks(project) == 0 and "kestrel" not in service.context(project, question).lower()
    rebuilt = rebuild_store(project)
    assert rebuilt["status"] == "ok" and rebuilt["withdrawn_transcripts"] == ["s-t"]
    assert transcript_blocks(project) == 0 and "kestrel" not in service.context(project, question).lower()


def test_a_document_ref_of_a_source_copy_withdraws_the_source_it_copies(project):
    """The document of a proof page copy stands for `proof:<path>`: the receipt names it, the copy and
    the event it was made from fall together (`servable._source_copy_links`), and neither the next
    capture of the event nor a rebuild serves the page again."""
    import hashlib
    from pathlib import Path
    from alpaca.wiki.ingest import drain
    from alpaca.wiki.servable import servable_ref
    text = "# Proof t-1\n\n## At a glance\n\nThe ember queue stalled because the cinder lock stayed held.\n"
    rel = ".alpaca/proofs/op-1/t-1.md"
    path = Path(project, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    eid = append(project, "proof-report", {"path": rel, "sha256": hashlib.sha256(text.encode()).hexdigest(),
                                           "bytes": len(text.encode()), "sections": ["At a glance"],
                                           "evidence": []}, session="s-src", op="op-1", ref="t-1")
    drain.capture(str(project), events(project, {eid}))
    copy = doc_of(project, "proof:" + rel)
    question = "Why did the ember queue stall?"
    assert copy.startswith("raw/sources/proof/") and "cinder lock" in service.context(project, question)
    receipt = withdraw_source(project, "document:" + copy)
    assert (receipt.status, receipt.reason, receipt.effects["source_ref"], receipt.effects["requested_ref"]) \
        == ("applied", None, "proof:" + rel, "document:" + copy)
    assert receipt.effects["refs"] == sorted(["event:%d" % eid, "proof:" + rel])
    with store(project) as conn:
        assert not servable_ref(conn, "proof:" + rel) and not servable_ref(conn, "event:%d" % eid)
    assert active_blocks_of(project, [copy]) == 0 and "cinder lock" not in service.context(project, question)
    drain.capture(str(project), events(project, {eid}))
    assert active_blocks_of(project, [copy]) == 0 and "cinder lock" not in service.context(project, question)
    assert withdraw_source(project, "document:" + copy).status == "duplicate"
    rebuilt = rebuild_store(project)
    assert rebuilt["status"] == "ok" and rebuilt["candidates"]["source_withdrawals_applied"] == 1
    assert active_blocks_of(project, [copy]) == 0 and "cinder lock" not in service.context(project, question)


# ------------------------------------------------------------------ what falls with a withdrawal
# Check round 4 invariants B1: a source withdrawn by the ref of its Alpaca source copy (or the document
# ref of the copy) took the event note the copy was made from out of every serve path, but the absorb
# skip and the expected sets read the exclusions row of the named source only. So status counted the
# note missing, a scoped question answered `partial`, and the next Stop, SessionEnd or PreCompact capture
# landed the note again with its record-rule edge active and a live claim on the withdrawn event. Every
# reader now reads one set, `servable.excluded_source_refs`.
DECISION = {"id": "dec-abc123def456", "kind": "design", "context": "Which store holds facts?",
            "options": ["a ledger", "a table"], "choice": "keep the quokka table",
            "consequence": "Replay rebuilds the quokka table.", "pointer": "", "ts": T10}
PROOF_TEXT = "# Proof t-1\n\n## At a glance\n\nThe ember queue stalled because the cinder lock stayed held.\n"
PROOF_REL = ".alpaca/proofs/op-1/t-1.md"
#: kind -> (the copy's source ref, the predicate of the event's record rule, a question scoped to the event)
COPIES = {"contract": ("contract:t-9", "in_op", "Which op is task t-9 in?"),
          "decision": ("decision:dec-abc123def456", "choice", "Which store holds facts for op-1?"),
          "proof": ("proof:" + PROOF_REL, "proves", "Why did the ember queue stall in op-1?")}


def write_proof(project):
    from pathlib import Path
    path = Path(project, PROOF_REL)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PROOF_TEXT.encode("utf-8"))


def source_event(project, kind, *, ts=T10, op=None, file=True):
    """One record event of a source kind (a task contract, a decision, a proof report), not captured."""
    import hashlib
    if kind == "contract":
        return at(project, "task-contract", CONTRACT, ts, session="s-src", op=op or "op-2", ref="t-9")
    if kind == "decision":
        return at(project, "decision", DECISION, ts, session="s-src", op=op or "op-1", ref=DECISION["id"])
    if file:
        write_proof(project)
    raw = PROOF_TEXT.encode("utf-8")
    return at(project, "proof-report", {"path": PROOF_REL, "sha256": hashlib.sha256(raw).hexdigest(),
                                        "bytes": len(raw), "sections": ["At a glance"], "evidence": []},
              ts, session="s-src", op=op or "op-1", ref="t-1")


def live_rule_edges(project, eid):
    """The active edges that hold a live claim on `event:<eid>`, by predicate."""
    with store(project) as conn:
        return sorted(r[0] for r in conn.execute(
            "SELECT e.predicate FROM edges e JOIN edge_origin o ON o.edge_id=e.edge_id "
            "WHERE e.status='active' AND o.status='live' AND o.source_ref=?", ("event:%d" % eid,)))


def in_op_at(project, ts, task="t-9"):
    """The ops the readers serve as current for a task at `ts`, by their own as-of clause."""
    from alpaca.wiki.store.asof import AsOf, asof_where_edges
    clause, params = asof_where_edges(AsOf(T=ts), "edges")
    with store(project) as conn:
        return sorted(r[0] for r in conn.execute(
            "SELECT COALESCE(obj_node, obj_literal) FROM edges WHERE subj_node=:subj "
            "AND predicate='in_op' AND " + clause, dict(params, subj=task)))


@pytest.mark.parametrize("form", ["the copy's ref", "the copy's document ref"])
@pytest.mark.parametrize("kind", sorted(COPIES))
def test_a_withdrawn_source_copy_takes_its_event_note_out_for_every_reader(project, kind, form):
    """A task contract, decision or proof copy withdrawn by its own ref or its document ref: the note it
    was made from is withdrawn for status, the expected sets and a scoped question, and neither the next
    Stop-hook capture, nor `wiki recover`, nor a rebuild from the record lands it again or brings back
    its rule edge; for the contract, the task's op before it stays the current one."""
    from alpaca.wiki.servable import servable_ref
    ref, predicate, question = COPIES[kind]
    if kind == "contract":
        at(project, "task-add", {"title": "Build the thing"}, T09, session="s-src", op="op-1", ref="t-9")
    eid = source_event(project, kind)
    capture(project)
    note, copy = doc_of(project, "event:%d" % eid), doc_of(project, ref)
    assert copy.startswith("raw/sources/") and active_blocks_of(project, [note, copy]) > 0
    assert predicate in live_rule_edges(project, eid)
    asked = ref if form == "the copy's ref" else "document:" + copy
    receipt = withdraw_source(project, asked)
    assert (receipt.status, receipt.effects["source_ref"]) == ("applied", ref)
    assert sorted(receipt.effects["refs"]) == sorted([ref, "event:%d" % eid])
    with store(project) as conn:
        assert [r[0] for r in conn.execute("SELECT source_ref FROM exclusions")] == [ref]

    def withdrawn_everywhere(stage, recovered=()):
        status = service.status(project)["capture"]
        assert status["withdrawn_events"] == 1, stage
        assert status["missing_events"] == len(recovered), stage       # a recovery event not landed yet
        assert set(service._uncaptured_events(project, service.Config.for_project(project))) == set(recovered)
        answer = service.query(project, question)
        assert answer["outcome"] != "partial" and answer["coverage"]["missing_event_ids_in_scope"] == [], stage
        assert active_blocks_of(project, [note, copy]) == 0, stage
        assert live_rule_edges(project, eid) == [], stage
        with store(project) as conn:
            assert not servable_ref(conn, "event:%d" % eid) and not servable_ref(conn, ref), stage
        if kind == "contract":
            assert in_op_at(project, "2026-03-01T11:00:00+00:00") == ["op-1"], stage
            door = service.query(project, "task t-9 op")
            assert door["outcome"] == "grounded" and "op-1" in door["answer_text"], stage

    withdrawn_everywhere("withdrawn")
    stop_capture(project)
    withdrawn_everywhere("after the next Stop-hook capture")
    report = recovery.run(project, session="s-recover")
    assert report["status"] == "ok" and report["candidates"]["skipped"] == {"already-withdrawn": 1}
    withdrawn_everywhere("after wiki recover", recovered=[report["recovery_event"]])
    capture(project)                                    # every event of the record, the recovery's too
    withdrawn_everywhere("after a capture of every event")
    live = digest_of(project)
    rebuilt = rebuild_store(project)
    assert rebuilt["status"] == "ok" and rebuilt["candidates"]["source_withdrawals_applied"] == 1
    assert digest_of(project) == live
    withdrawn_everywhere("after a rebuild from the record", recovered=[rebuilt["recovery_event"]])


@pytest.mark.parametrize("kind", sorted(COPIES))
def test_an_event_withdrawal_keeps_its_source_copy_out_of_the_next_capture(project, kind):
    """The mirror case: `event:N` of a source kind withdrawn, its copy is not landed again by the next
    Stop-hook capture or by a rebuild; the page is not served."""
    ref, _, _ = COPIES[kind]
    eid = source_event(project, kind)
    capture(project)
    copy = doc_of(project, ref)
    assert active_blocks_of(project, [copy]) > 0
    assert withdraw_source(project, "event:%d" % eid).status == "applied"
    with store(project) as conn:
        assert [r[0] for r in conn.execute("SELECT source_ref FROM exclusions")] == ["event:%d" % eid]
    assert active_blocks_of(project, [copy]) == 0
    stop_capture(project)
    capture(project)
    assert active_blocks_of(project, [copy]) == 0
    status = service.status(project)["capture"]
    assert (status["withdrawn_events"], status["missing_events"]) == (1, 0)
    assert rebuild_store(project)["status"] == "ok" and active_blocks_of(project, [copy]) == 0


@pytest.mark.parametrize("versions", ["the note has a source_versions row", "a note captured before them"])
def test_a_source_copy_that_lands_after_its_event_was_withdrawn_is_not_landed(project, versions):
    """The proof file is missing at the first capture (a source gap), the event is withdrawn, then the file
    appears: the next capture renders the copy but does not land it, as the servable predicate would hide
    it once landed: by the origin_hash it shares with the note's version, or, for a note captured before
    `source_versions` existed, by the copy link of its doc id. Nothing of it is served."""
    eid = source_event(project, "proof", file=False)
    gaps = capture(project)["source_gaps"]
    assert [(g["event"], g["reason"]) for g in gaps] == [(eid, "proof-file-missing")]
    if versions == "a note captured before them":
        with store(project) as conn:
            conn.execute("DROP TRIGGER IF EXISTS no_delete_source_versions")
            conn.execute("DELETE FROM source_versions WHERE source_ref=?", ("event:%d" % eid,))
    assert withdraw_source(project, "event:%d" % eid).status == "applied"
    write_proof(project)
    stop_capture(project)
    with store(project) as conn:
        assert conn.execute("SELECT COUNT(*) FROM docs WHERE doc_id LIKE 'raw/sources/%'").fetchone()[0] == 0
    assert "cinder lock" not in service.context(project, "Why did the ember queue stall?")
    status = service.status(project)["capture"]
    assert (status["withdrawn_events"], status["missing_events"]) == (1, 0)


def test_withdrawing_the_newer_of_two_contracts_keeps_the_note_of_the_older_one(project):
    """A contract rendered twice: withdrawing the newer event takes out the contract copy (every version)
    and that event's note, not the older event's note, whose op is current again, and the next capture
    lands nothing of the newer one. The contract asked for by its own ref is withdrawn as a whole,
    the older note with it: the duplicate check reads the row of the source asked for."""
    from alpaca.wiki.servable import excluded_source_refs, servable_ref
    older = source_event(project, "contract", ts=T10, op="op-2")
    newer = source_event(project, "contract", ts=T11, op="op-3")
    capture(project)
    assert in_op_at(project, "2026-03-01T12:00:00+00:00") == ["op-3"]
    assert withdraw_source(project, "event:%d" % newer).status == "applied"
    with store(project) as conn:
        assert excluded_source_refs(conn) == {"event:%d" % newer, "contract:t-9"}
        assert servable_ref(conn, "event:%d" % older)
    for _ in ("withdrawn", "after the next capture"):
        assert in_op_at(project, "2026-03-01T12:00:00+00:00") == ["op-2"]
        assert active_blocks_of(project, [doc_of(project, "event:%d" % older)]) == 2
        assert active_blocks_of(project, [doc_of(project, "event:%d" % newer)]) == 0
        with store(project) as conn:
            assert conn.execute("SELECT COUNT(*) FROM blocks WHERE status='active' "
                                "AND doc_id LIKE 'raw/sources/%'").fetchone()[0] == 0
        status = service.status(project)["capture"]
        assert (status["withdrawn_events"], status["missing_events"], status["captured_events"]) == (1, 0, 1)
        stop_capture(project)
    whole = withdraw_source(project, "contract:t-9")
    assert whole.status == "applied" and "event:%d" % older in whole.effects["refs"]
    stop_capture(project)
    assert in_op_at(project, "2026-03-01T12:00:00+00:00") == []
    status = service.status(project)["capture"]
    assert (status["withdrawn_events"], status["missing_events"], status["captured_events"]) == (2, 0, 0)


def test_an_anchor_that_fell_with_a_withdrawn_copy_is_refused_as_excluded(project):
    """K-4 names why an anchor is not servable by the same set: the note of a withdrawn contract copy is
    an `excluded-anchor`, not a private one."""
    eid = source_event(project, "contract")
    capture(project)
    body = proposal(project, eid, "stage: build", subject="t-9", predicate="status", obj="build")
    assert withdraw_source(project, "contract:t-9").status == "applied"
    refused = propose(project, body)
    assert refused.status == "refused"
    assert {c.check: c.reason for c in refused.checks if not c.passed}["K-4"] == "excluded-anchor"
