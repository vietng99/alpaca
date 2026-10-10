"""op-008 t-066 stage s2-admission: `admit` and `withdraw_candidate` for facts (design/rune2-memory/
architecture.md 5.1, 7.4, 7.6; FR-33; OB-05; dec-38940e1fb35f Q11, Q12).

A candidate never corroborates or places anything before admission (Z9). Admission re-runs K-1 to K-5
on the current store, places the edge by the verdict of `reconcile.classify` with the valid time of
architecture.md 5.1, writes the support claim `candidate:<id>`, and appends the `wiki-admission`
event with the full effect before the commit. Withdrawal undoes only that net effect. Real record,
real drain, real store: the only monkeypatch (a capture that lands nothing) says so where it is used.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import (T09, T10, T11, admit, at, capture, claims, corroborators,  # noqa: E402
                            doc_like, edges, events, llm_edges, note, proposal, propose, row_of,
                            store, unknown_time, wiki_events, withdraw)

from alpaca.wiki import intake, service                       # noqa: E402
from alpaca.wiki.ingest import reconcile                      # noqa: E402

BLOCKED = "Task t-9 is blocked on the review."


# ------------------------------------------------------------------ who may admit
def test_no_self_admission_and_no_job_admission(project):
    eid = note(project, BLOCKED)
    cid = propose(project, proposal(project, eid, "Task t-9 is blocked")).candidate_id
    own = admit(project, cid, session="s-agent")
    assert (own.status, own.reason) == ("refused", "self-admission")
    job = admit(project, cid, session="job:rune2-consolidate")
    assert (job.status, job.reason) == ("refused", "job-cannot-admit")
    assert row_of(project, cid)["status"] == "checked" and not llm_edges(project)
    # a refusal is recorded, with its reason, and writes nothing else
    refused = [e["data"] for e in wiki_events(project, "wiki-admission")]
    assert [(d["result"], d["reason"]) for d in refused] == [
        ("refused", "self-admission"), ("refused", "job-cannot-admit")]
    assert admit(project, "c_unknown").reason == "unknown-candidate"


def test_a_summary_is_never_admitted_and_an_observation_needs_its_gate_metadata(project):
    eid = note(project, BLOCKED)
    a = {"source_ref": "event:%d" % eid, "quote": "Task t-9 is blocked",
         "hash": next(e for e in events(project) if e["id"] == eid)["hash"]}
    summary = propose(project, {"kind": "summary", "text": "Task t-9 is blocked.",
                                "sentence_anchors": [[0]], "anchors": [a]})
    assert summary.status == "checked"
    refused = admit(project, summary.candidate_id)
    assert (refused.status, refused.reason) == ("refused", "summary-not-admitted")
    assert row_of(project, summary.candidate_id)["status"] == "checked"
    obs = propose(project, {"kind": "observation", "text": "Task t-9 is blocked.",
                            "sentence_anchors": [[0]], "anchors": [a]})
    # the observation doors are built (t-067): without gate metadata there is no door
    refused_obs = admit(project, obs.candidate_id)
    assert (refused_obs.status, refused_obs.reason) == ("refused", "observation-body-missing")


# ------------------------------------------------------------------ corroboration (Z9)
def test_a_matches_candidate_corroborates_only_at_admission(project):
    first = note(project, BLOCKED, T10)
    second = note(project, "Task t-9 is blocked again on the review.", T11)
    one = propose(project, proposal(project, first, "Task t-9 is blocked"))
    admit(project, one.candidate_id)
    (edge,) = llm_edges(project)
    assert edge["corroboration_count"] == 1
    two = propose(project, proposal(project, second, "Task t-9 is blocked again"))
    assert two.verdict == "matches" and two.prior_edge_id == edge["edge_id"]
    # the proposal changed nothing, and withdrawing it before admission changes nothing either
    assert edges(project, edge_id=edge["edge_id"])[0]["corroboration_count"] == 1
    assert withdraw(project, two.candidate_id).status == "applied"
    assert edges(project, edge_id=edge["edge_id"])[0]["corroboration_count"] == 1
    assert len(claims(project, edge["edge_id"])) == 1
    # a candidate that is withdrawn is not admitted afterwards
    assert admit(project, two.candidate_id).reason == "not-checked"
    # the same fact from the second note, proposed again with another quote, corroborates at admission
    three = propose(project, proposal(project, second, "Task t-9 is blocked again on the review"))
    receipt = admit(project, three.candidate_id)
    assert (receipt.status, receipt.effects["effect"]) == ("applied", "corroborated")
    after = edges(project, edge_id=edge["edge_id"])[0]
    assert after["corroboration_count"] == 2 and len(corroborators(project, edge["edge_id"])) == 2
    assert len(llm_edges(project)) == 1
    # withdrawing it removes the corroboration it wrote and its claim only
    assert withdraw(project, three.candidate_id).effects["undone"]["corroboration"] == "removed"
    back = edges(project, edge_id=edge["edge_id"])[0]
    assert back["corroboration_count"] == 1 and back["status"] == "active"
    assert {c["claim_ref"]: c["status"] for c in claims(project, edge["edge_id"])} == {
        "candidate:" + one.candidate_id: "live", "candidate:" + three.candidate_id: "withdrawn"}
    assert row_of(project, three.candidate_id)["status"] == "retracted"


def test_a_claim_only_admission_keeps_the_row_a_record_rule_added(project):
    """The rule edge of a task-contract already corroborates from the header block. An agent fact
    anchored in the same block has the same edge id (a `duplicate` verdict): it writes a claim and no
    row, and its withdrawal leaves the rule's row, claim and edge as they were."""
    eid = at(project, "task-contract", {"source": "dec-38940e1fb35f", "done_bar": ["x"]}, T10)
    capture(project)
    (rule,) = [e for e in edges(project, predicate="sourced_from")]
    before = (rule["corroboration_count"], corroborators(project, rule["edge_id"]),
              claims(project, rule["edge_id"]))
    quote = "task: t-9\nsource: dec-38940e1fb35f"
    receipt = propose(project, proposal(project, eid, quote, predicate="sourced_from",
                                        obj="dec-38940e1fb35f", datatype="node"))
    assert receipt.status == "checked", receipt.checks
    done = admit(project, receipt.candidate_id)
    assert (done.status, done.effects["effect"]) == ("applied", "claim-only")
    assert not llm_edges(project)                         # no agent edge: the rule's edge is the edge
    assert edges(project, edge_id=rule["edge_id"])[0]["corroboration_count"] == before[0]
    assert corroborators(project, rule["edge_id"]) == before[1]
    mine = [c for c in claims(project, rule["edge_id"]) if c["origin"] == "candidate"]
    assert [(c["status"], c["fact_kind"], c["candidate_id"]) for c in mine] == [
        ("live", "derived-hypothesis", receipt.candidate_id)]
    out = withdraw(project, receipt.candidate_id)
    assert out.effects["undone"] == {"claim": "withdrawn", "edge_id": rule["edge_id"]}
    rule_after = edges(project, edge_id=rule["edge_id"])[0]
    assert rule_after["status"] == "active" and rule_after["extractor"] == "deterministic"
    assert corroborators(project, rule["edge_id"]) == before[1]
    live = [c for c in claims(project, rule["edge_id"]) if c["status"] == "live"]
    assert [c["origin"] for c in live] == ["record-rule"]


# ------------------------------------------------------------------ withdrawing a written edge
def contract_proposal(project, eid, quote="Task t-9 is sourced from dec-38940e1fb35f"):
    return proposal(project, eid, quote, predicate="sourced_from", obj="dec-38940e1fb35f",
                    datatype="node")


def test_withdrawing_a_novel_edge_reanchors_to_a_record_rule_instead_of_retracting_it(project):
    eid = note(project, "Task t-9 is sourced from dec-38940e1fb35f in this result.", T10)
    one = propose(project, contract_proposal(project, eid, "Task t-9 is sourced from dec-38940e1fb35f"))
    assert admit(project, one.candidate_id).effects["effect"] == "edge-written"
    (edge,) = llm_edges(project)
    first_block = edge["source_block_id"]
    # a record rule now reads the same fact from a later task-contract: it supports the agent's edge
    rule = at(project, "task-contract", {"source": "dec-38940e1fb35f", "done_bar": ["x"]}, T11)
    capture(project, {rule})
    assert {c["origin"] for c in claims(project, edge["edge_id"])} == {"candidate", "record-rule"}
    assert len(corroborators(project, edge["edge_id"])) == 2
    out = withdraw(project, one.candidate_id)
    assert out.effects["undone"]["edge"] == "reanchored" and out.effects["undone"]["corroboration"] == "removed"
    (kept,) = edges(project, edge_id=edge["edge_id"])
    assert kept["status"] == "active" and kept["source_block_id"] != first_block
    assert corroborators(project, kept["edge_id"]) == [kept["source_block_id"]]
    assert kept["corroboration_count"] == 1
    with store(project) as conn:
        text = conn.execute("SELECT text FROM blocks WHERE block_id=?", (kept["source_block_id"],)
                            ).fetchone()[0]
    assert "source: dec-38940e1fb35f" in text and intake.normalize_text(kept["source_quote"]) in \
        intake.normalize_text(text)


def test_withdrawing_a_novel_edge_with_no_other_claim_retracts_it(project):
    eid = note(project, BLOCKED, T10)
    cid = propose(project, proposal(project, eid, "Task t-9 is blocked")).candidate_id
    admit(project, cid)
    (edge,) = llm_edges(project)
    out = withdraw(project, cid)
    assert out.status == "applied" and out.effects["status"] == "retracted"
    assert out.effects["undone"]["edge"] == "retracted"
    (gone,) = edges(project, edge_id=edge["edge_id"])
    assert (gone["status"], gone["retraction_reason"]) == ("retracted", "candidate-withdrawn")
    assert [c["status"] for c in claims(project, edge["edge_id"])] == ["withdrawn"]
    assert row_of(project, cid)["status"] == "retracted"
    (event,) = wiki_events(project, "wiki-candidate-withdrawn")
    assert event["data"]["undone"]["edge"] == "retracted" and event["data"]["reason"] == "wrong"
    # nothing was deleted, and a second withdrawal is a duplicate
    assert withdraw(project, cid).status == "duplicate"
    # the identical proposal after withdrawal is still a duplicate (Q12: a withdrawn row matches)
    assert propose(project, proposal(project, eid, "Task t-9 is blocked")).status == "duplicate"


# ------------------------------------------------------------------ contradicting a prior
def test_a_contradicting_candidate_supersedes_as_a_correction_and_withdrawal_restores(project):
    old = note(project, "Task t-9 is blocked on the review.", T10)
    new = note(project, "Task t-9 is released since the review.", T10)        # the same instant
    one = propose(project, proposal(project, old, "Task t-9 is blocked"))
    admit(project, one.candidate_id)
    (prior,) = llm_edges(project)
    two = propose(project, proposal(project, new, "Task t-9 is released", obj="released"))
    assert two.verdict == "contradicts" and two.prior_edge_id == prior["edge_id"]
    receipt = admit(project, two.candidate_id)
    assert receipt.effects["effect"] == "superseded-prior"
    after = {e["edge_id"]: e for e in edges(project)}
    successor = after[receipt.effects["edge_id"]]
    assert after[prior["edge_id"]]["status"] == "invalidated"          # a correction
    assert after[prior["edge_id"]]["superseded_by_edge_id"] == successor["edge_id"]
    assert after[prior["edge_id"]]["valid_until"] is None and successor["supersedes_edge_id"] == prior["edge_id"]
    undone = withdraw(project, two.candidate_id, session="s-agent").effects["undone"]
    assert undone["edge"] == "retracted" and undone["prior"] == prior["edge_id"]
    restored = edges(project, edge_id=prior["edge_id"])[0]
    assert (restored["status"], restored["valid_until"], restored["superseded_at"],
            restored["superseded_by_edge_id"]) == ("active", None, None, None)
    assert edges(project, edge_id=successor["edge_id"])[0]["status"] == "retracted"


def test_a_contradicting_candidate_with_a_later_valid_from_is_a_world_change(project):
    old = note(project, "Task t-9 is blocked on the review.", T10)
    new = note(project, "Task t-9 is released since the review.", T11)
    admit(project, propose(project, proposal(project, old, "Task t-9 is blocked")).candidate_id)
    (prior,) = llm_edges(project)
    two = propose(project, proposal(project, new, "Task t-9 is released", obj="released"))
    receipt = admit(project, two.candidate_id)
    assert receipt.effects["effect"] == "superseded-prior"
    ended = edges(project, edge_id=prior["edge_id"])[0]
    assert (ended["status"], ended["valid_until"], ended["superseded_at"]) == ("active", T11, None)
    event = wiki_events(project, "wiki-admission")[-1]["data"]["edge"]
    assert event["world_change"] is True and event["prior"]["valid_until"] is None
    withdraw(project, two.candidate_id)
    back = edges(project, edge_id=prior["edge_id"])[0]
    assert (back["status"], back["valid_until"]) == ("active", None)


# ------------------------------------------------------------------ valid time (ARCH 5.1, OB-05)
def test_an_explicit_valid_from_is_used_and_the_time_is_known(project):
    eid = note(project, "Since 2026-02-01 task t-9 is blocked on the review.", T10)
    cid = propose(project, proposal(project, eid, "Since 2026-02-01 task t-9 is blocked",
                                    valid_from="2026-02-01")).candidate_id
    admit(project, cid)
    (edge,) = llm_edges(project)
    assert edge["valid_from"] == edge["source_created_at"] == "2026-02-01T00:00:00+00:00"


def test_unknown_time_fact_gets_the_block_time_and_no_source_time(project):
    eid = note(project, BLOCKED, T10)
    unknown_time(project, eid)
    cid = propose(project, proposal(project, eid, "Task t-9 is blocked")).candidate_id
    admit(project, cid)
    (edge,) = llm_edges(project)
    assert edge["valid_from"] == T09 and edge["source_created_at"] is None
    # an explicit-T ask whose cited items all have a null source time cannot place them in time
    answer = service.query(project, "Task t-9 is blocked on the review.", as_of="2030-01-01T00:00:00+00:00")
    assert (answer["outcome"], answer["reason"]) == ("unsupported", "valid-time-unknown")


def test_unknown_time_contradicts_is_refused_and_leaves_the_prior_window(project):
    prior_note = note(project, "Task t-9 is blocked on the review.", T10)
    admit(project, propose(project, proposal(project, prior_note, "Task t-9 is blocked")).candidate_id)
    (prior,) = llm_edges(project)
    undated = note(project, "Task t-9 is released since the review.", T10, session="s-other")
    unknown_time(project, undated, T09)
    cid = propose(project, proposal(project, undated, "Task t-9 is released", obj="released")).candidate_id
    receipt = admit(project, cid)
    assert (receipt.status, receipt.reason) == ("refused", "valid-time-required")
    assert row_of(project, cid)["status"] == "checked"
    assert len(llm_edges(project)) == 1
    same = edges(project, edge_id=prior["edge_id"])[0]
    assert (same["status"], same["valid_until"], same["superseded_at"]) == ("active", None, None)


def _status_move(project, to, ts, frm="open"):
    return at(project, "task-move", {"from": frm, "to": to, "proof": None, "reason": None}, ts,
              ref="t-9", actor="human")


def test_record_fact_does_not_end_unknown_time_fact_window(project):
    """OB-05: an admitted agent fact of unknown time, then a later task-move on the same subject and
    predicate: the verdict is a correction (the unknown time never orders history) and no valid_until
    is written on the agent edge. Two record-fact statuses of known time still give a world change."""
    eid = note(project, BLOCKED, T10)
    unknown_time(project, eid)
    admit(project, propose(project, proposal(project, eid, "Task t-9 is blocked")).candidate_id)
    (agent,) = llm_edges(project)
    assert agent["source_created_at"] is None
    move = _status_move(project, "done", T11)
    capture(project, {move})
    after = edges(project, edge_id=agent["edge_id"])[0]
    assert after["valid_until"] is None                              # no window written
    assert after["status"] == "invalidated" and after["superseded_at"] is not None   # a correction
    (rule,) = [e for e in edges(project, predicate="status") if e["extractor"] == "deterministic"]
    assert rule["reconcile_verdict"] == "contradicts" and rule["supersedes_edge_id"] == agent["edge_id"]
    # known times still give a world change
    a, b = _status_move(project, "doing", "2026-04-01T10:00:00+00:00"), None
    b = _status_move(project, "review", "2026-04-01T11:00:00+00:00", frm="doing")
    with store(project) as conn:
        conn.execute("UPDATE edges SET subj_node=subj_node")        # no-op: keep the connection honest
    # (these two moves are on t-9 as well; the earlier agent edge is invalidated, so they order alone)
    capture(project, {a, b})
    by_obj = {e["obj_node"]: e for e in edges(project, predicate="status") if e["obj_node"]}
    assert by_obj["status:doing"]["valid_until"] == by_obj["status:review"]["valid_from"]


def test_unknown_time_fact_gets_no_backfilled_window(project):
    """OB-05: statuses at 10:00 and 11:00 and an unknown-time agent fact whose proxy is 09:00: no
    valid_until is back-filled and its admission is refused with `valid-time-required`."""
    _status_move(project, "doing", T10)
    _status_move(project, "done", T11, frm="doing")
    capture(project)
    eid = note(project, BLOCKED, T10, session="s-other")
    unknown_time(project, eid, T09)
    cid = propose(project, proposal(project, eid, "Task t-9 is blocked")).candidate_id
    receipt = admit(project, cid)
    assert (receipt.status, receipt.reason) == ("refused", "valid-time-required")
    assert not llm_edges(project)
    # the classifier itself: an unknown time picks no containing prior, back-fills no window
    cand = {"subj_node": "t-9", "predicate": "status", "obj_node": "status:blocked",
            "obj_literal": None, "obj_datatype": "node", "valid_from": T09, "source_created_at": None,
            "extractor": "llm", "source_block_id": "none"}
    from alpaca.wiki.config import Config
    from alpaca.wiki.store.db import DB
    db = DB(Config.for_project(str(project)))
    try:
        verdict = reconcile.classify(db, cand, "e_new")
    finally:
        db.close()
    assert cand.get("valid_until") is None and verdict.world_change is False
    # with a known time the same ask is back-filled from the later edge, as before
    cand2 = dict(cand, valid_from=T09, source_created_at=T09)
    db = DB(Config.for_project(str(project)))
    try:
        reconcile.classify(db, cand2, "e_new2")
    finally:
        db.close()
    assert cand2.get("valid_until") == T10


# ------------------------------------------------------------------ the door, the cite, the record
def test_the_admitted_edge_cites_its_source_block_and_only_the_door_wrote_it(project):
    eid = note(project, BLOCKED, T10)
    cid = propose(project, proposal(project, eid, "Task t-9 is blocked")).candidate_id
    assert not llm_edges(project) and not claims(project)             # a proposal writes no edge, no claim
    receipt = admit(project, cid)
    (edge,) = llm_edges(project)
    assert (edge["extractor"], edge["extractor_version"], edge["status"]) == (
        "llm", "agent-candidate/1", "active")
    with store(project) as conn:
        block = conn.execute("SELECT * FROM blocks WHERE block_id=?", (edge["source_block_id"],)).fetchone()
        origin = conn.execute("SELECT source_doc_id FROM edges WHERE edge_id=?", (edge["edge_id"],)).fetchone()
    assert block["status"] == "active" and origin[0] == block["doc_id"]
    assert intake.normalize_text(edge["source_quote"]) in intake.normalize_text(block["text"])
    (claim,) = claims(project, edge["edge_id"])
    assert (claim["origin"], claim["candidate_id"], claim["fact_kind"], claim["status"],
            claim["source_ref"]) == ("candidate", cid, "derived-hypothesis", "live", "event:%d" % eid)
    assert claim["claim_ref"] == "candidate:" + cid
    row = row_of(project, cid)
    assert (row["status"], row["admission_ref"], row["admission_effect"], row["admission_event_id"]) == (
        "admitted", edge["edge_id"], "edge-written", receipt.record_event_id)
    # the event holds the full effect, and no fact kind is user-statement (Q11)
    (event,) = wiki_events(project, "wiki-admission")
    d = event["data"]
    assert event["actor"] == "alpaca-wiki" and event["session"] == "s-admin"
    assert (d["result"], d["effect"], d["verdict"], d["session"]) == ("applied", "edge-written", "novel", "s-admin")
    assert d["edge"]["edge_id"] == edge["edge_id"] and d["edge"]["valid_from"] == edge["valid_from"]
    assert d["edge"]["anchor"]["source_ref"] == "event:%d" % eid
    assert d["edge"]["anchor"]["quote_sha256"] == intake.quote_sha256("Task t-9 is blocked")
    assert d["edge"]["claim"] == {"claim_ref": "candidate:" + cid, "fact_kind": "derived-hypothesis"}
    assert d["entailer"] and d["lesson"] is None and d["knowledge"] is None
    assert "user-statement" not in {c["fact_kind"] for c in claims(project)}
    # admitting twice is a duplicate and writes nothing more
    again = admit(project, cid)
    assert again.status == "duplicate" and len(llm_edges(project)) == 1 and len(claims(project)) == 1


def test_the_only_source_text_that_writes_an_llm_edge_is_the_admission_door():
    import re
    from pathlib import Path
    root = Path(service.__file__).parent
    writers = {path.relative_to(root).as_posix() for path in root.rglob("*.py")
               if "agent-candidate/1" in path.read_text(encoding="utf-8")}
    assert writers == {"store/write.py"}, writers


def test_admission_runs_k1_to_k5_again_on_the_current_store(project):
    eid = note(project, BLOCKED, T10)
    cid = propose(project, proposal(project, eid, "Task t-9 is blocked")).candidate_id
    with store(project) as conn:                  # the source turns private between propose and admit
        conn.execute("UPDATE docs SET private=1 WHERE doc_id LIKE ?", (doc_like(eid),))
    receipt = admit(project, cid)
    assert (receipt.status, receipt.reason) == ("refused", "private-anchor")
    assert receipt.effects["check"] == "K-4" and row_of(project, cid)["status"] == "checked"
    assert not llm_edges(project) and not claims(project)


def test_admit_lands_the_events_it_has_not_captured_first(project):
    eid = note(project, BLOCKED, T10)
    cid = propose(project, proposal(project, eid, "Task t-9 is blocked")).candidate_id
    lag = at(project, "result", {"body": "A later result that the store has not seen."}, T11)
    with store(project) as conn:
        assert not conn.execute("SELECT 1 FROM docs WHERE doc_id LIKE ?", (doc_like(lag),)).fetchone()
    assert admit(project, cid).status == "applied"
    with store(project) as conn:
        assert conn.execute("SELECT 1 FROM docs WHERE doc_id LIKE ?", (doc_like(lag),)).fetchone()


def test_admit_is_unavailable_when_capture_is_behind(project, monkeypatch):
    eid = note(project, BLOCKED, T10)
    cid = propose(project, proposal(project, eid, "Task t-9 is blocked")).candidate_id
    lag = at(project, "result", {"body": "A later result that the store has not seen."}, T11)
    # fault injection: a capture that lands nothing, as one cut short by its deadline would
    monkeypatch.setattr(service, "capture", lambda *a, **k: {})
    receipt = admit(project, cid)
    assert (receipt.status, receipt.reason) == ("unavailable", "capture-behind")
    assert receipt.effects["first_missing_event"] == lag and receipt.record_event_id is None
    assert row_of(project, cid)["status"] == "checked" and not llm_edges(project)
    assert not wiki_events(project, "wiki-admission")


# ------------------------------------------------------------------ who may withdraw
def test_only_the_proposer_or_the_owner_withdraws_and_a_refused_row_has_nothing_to_withdraw(project):
    eid = note(project, BLOCKED, T10)
    cid = propose(project, proposal(project, eid, "Task t-9 is blocked")).candidate_id
    stranger = withdraw(project, cid, session="s-stranger")
    assert (stranger.status, stranger.reason) == ("refused", "not-proposer-or-owner")
    assert row_of(project, cid)["status"] == "checked"
    owner = withdraw(project, cid, session="s-stranger", by="owner")
    assert owner.status == "applied" and row_of(project, cid)["status"] == "withdrawn"
    with pytest.raises(ValueError):
        withdraw(project, cid, by="root")
    # a refused proposal is retried unchanged (Q12), so withdrawing it is refused, not applied
    bad = propose(project, proposal(project, eid, "Task t-9 is not in the note"))
    assert bad.status == "refused"
    nothing = withdraw(project, bad.candidate_id)
    assert (nothing.status, nothing.reason) == ("refused", "nothing-to-withdraw")
    assert withdraw(project, "c_unknown").reason == "unknown-candidate"


def test_ingest_records_the_session_in_source_versions(project):
    from native_helpers import ingest
    page = project + "/doc.md"
    open(page, "w").write("The kestrel queue drains nightly.\n")
    service.ingest(project, page, doc_id="wiki/doc.md", session="s-ingester")
    with store(project) as conn:
        row = conn.execute("SELECT session,operator FROM source_versions WHERE doc_id='wiki/doc.md'").fetchone()
    assert row["session"] == "s-ingester"


def test_the_cli_verbs_admit_and_withdraw_candidate(project, capsys):
    """`wiki admit` and `wiki withdraw-candidate` bind the record appender (actor `alpaca-wiki`) to
    the caller's session; both need `--session`, and `ingest` passes it on (FR-4)."""
    from alpaca import cli
    eid = note(project, BLOCKED, T10)
    cid = propose(project, proposal(project, eid, "Task t-9 is blocked")).candidate_id
    capsys.readouterr()
    assert cli.main(["wiki", "admit", cid]) == 1                       # no --session
    assert "needs --session" in capsys.readouterr().err
    assert cli.main(["--session", "s-agent", "wiki", "admit", cid]) == 2      # self-admission: blocked
    assert json.loads(capsys.readouterr().out)["reason"] == "self-admission"
    assert cli.main(["--session", "s-admin", "wiki", "admit", cid]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "applied" and out["effects"]["effect"] == "edge-written"
    assert cli.main(["--session", "s-agent", "wiki", "withdraw-candidate", cid, "--reason", "cli"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "applied" and out["effects"]["status"] == "retracted"
    assert {e["actor"] for e in events(project) if e["kind"].startswith("wiki-")} == {"alpaca-wiki"}
    page = project + "/cli.md"
    open(page, "w").write("The kestrel queue drains nightly.\n")
    assert cli.main(["--session", "s-ingester", "wiki", "ingest", page, "--doc-id", "wiki/cli.md"]) == 0
    with store(project) as conn:
        assert conn.execute("SELECT session FROM source_versions WHERE doc_id='wiki/cli.md'"
                            ).fetchone()[0] == "s-ingester"
