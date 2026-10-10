"""op-008 t-066: `propose` and `candidates` (design/rune2-memory/architecture.md 7.1, 7.3; FR-33;
OB-21; dec-38940e1fb35f Q12).

An identical proposal whose row is not `refused` is a `duplicate`: its event holds only ids and quote
hashes and no row is written. A row refused for a reason that may have passed (an anchor not captured
yet) is checked again and rewritten, so the same JSON can be sent again unchanged. A refusal that
involves a non-servable anchor keeps the body as a hash only. Real record, real drain, real store.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import (append, appender, capture, event_hash, events, fact_event,  # noqa: E402
                            fact_proposal, reset_vault, store)

from alpaca.wiki import intake, service                       # noqa: E402
from alpaca.wiki.config import Config                          # noqa: E402
from alpaca.wiki.store.db import DB                            # noqa: E402
from alpaca.wiki.store.ledger import Ledger                    # noqa: E402
from alpaca.wiki.store.write import Writer                     # noqa: E402


def propose(project, body, session="s-agent"):
    return service.propose(project, body, session=session, record=appender(project, session))


def wiki_events(project):
    return [dict(e, data=json.loads(e["data"])) for e in events(project) if e["kind"] == "wiki-candidate"]


def tables(project):
    with store(project) as conn:
        return ([dict(r) for r in conn.execute("SELECT * FROM candidates ORDER BY candidate_id")],
                [dict(r) for r in conn.execute(
                    "SELECT * FROM candidate_anchors ORDER BY candidate_id, ordinal")])


def rebuild(project):
    """Replay every `wiki-candidate` event in record order into a fresh store, from the recorded
    fields alone (the replay of alpaca/wiki/recovery.py reuses `rows_from_event`)."""
    reset_vault(project)
    capture(project)
    cfg = Config.for_project(project)
    db = DB(cfg)
    try:
        db.pour()
        writer = Writer(db, Ledger(cfg.ledger_path, vault_dir=cfg.vault_dir))
        for e in wiki_events(project):
            made = intake.rows_from_event(e["data"], proposer=e["session"], operator=None,
                                          event_id=e["id"], ts=e["ts"])
            if made:
                with writer.transaction():
                    writer.write_candidate(*made)
    finally:
        db.close()


def test_propose_writes_the_event_then_the_rows(project):
    eid = fact_event(project)
    receipt = propose(project, fact_proposal(project, eid))
    assert receipt.status == "checked" and receipt.candidate_id.startswith("c_")
    (event,) = wiki_events(project)
    assert event["id"] == receipt.record_event_id and event["actor"] == "alpaca-wiki"
    assert event["session"] == "s-agent" and event["data"]["status"] == "checked"
    rows, anchors = tables(project)
    assert [r["candidate_id"] for r in rows] == [receipt.candidate_id]
    assert rows[0]["record_event_id"] == event["id"] and rows[0]["proposer"] == "s-agent"
    assert rows[0]["proposer_kind"] == "session" and rows[0]["created_at"] == event["ts"]
    assert json.loads(rows[0]["body_json"])["fact"]["predicate"] == "status"
    assert [(a["role"], a["source_ref"]) for a in anchors] == [("anchor", "event:%d" % eid)]
    assert anchors[0]["doc_id"] and anchors[0]["block_content_id"] and anchors[0]["block_sha256"]


def test_a_job_proposer_is_recorded_as_a_job(project):
    eid = fact_event(project)
    propose(project, fact_proposal(project, eid), session="job:rune2-consolidate")
    (view,) = service.candidates(project)
    assert (view.proposer, view.proposer_kind) == ("job:rune2-consolidate", "job")


def test_the_candidate_id_ignores_context_scope_and_whitespace(project):
    eid = fact_event(project)
    a = intake.parse_proposal(fact_proposal(project, eid))
    b = intake.parse_proposal(fact_proposal(project, eid, quote="Task t-066   is blocked",
                                            scope={"op": "op-8"}))
    # the quote differs by whitespace only; the scope is not identity
    assert intake.candidate_id(a) == intake.candidate_id(b)
    c = intake.parse_proposal(fact_proposal(project, eid, obj="blocked ", quote="Task t-066 is blocked on"))
    assert intake.candidate_id(c) != intake.candidate_id(a)


def test_an_identical_proposal_is_a_duplicate_and_writes_no_row(project):
    eid = fact_event(project)
    body = fact_proposal(project, eid)
    first = propose(project, body)
    before = tables(project)
    second = propose(project, body)
    assert second.status == "duplicate" and second.candidate_id == first.candidate_id
    assert second.record_event_id != first.record_event_id and second.checks == ()
    assert tables(project) == before
    assert wiki_events(project)[1]["data"]["status"] == "duplicate"


def test_identical_proposal_after_withdrawal_is_duplicate(project):
    # the withdraw-candidate verb arrives in a later stage; its effect on the row is set here
    eid = fact_event(project)
    body = fact_proposal(project, eid)
    first = propose(project, body)
    with store(project) as conn:
        conn.execute("UPDATE candidates SET status='withdrawn'")
    before = tables(project)
    second = propose(project, body)
    assert second.status == "duplicate" and second.candidate_id == first.candidate_id
    rows, anchors = tables(project)
    assert (rows, anchors) == before and len(rows) == 1 and rows[0]["status"] == "withdrawn"


def test_duplicate_event_holds_no_clear_quote(project):
    eid = fact_event(project)
    with store(project) as conn:
        conn.execute("UPDATE docs SET private=1")           # a quarantined capture lands private
    body = fact_proposal(project, eid)
    ingest_ctx = {"source_ref": "transcript:s-later", "quote": "a private aside about the roadmap"}
    body["context"] = [ingest_ctx]
    first = propose(project, body)
    assert first.status == "refused"
    assert [c.reason for c in first.checks if not c.passed] == ["private-anchor"]
    second = propose(project, body)             # refused rows are checked again (Q12), so also refused
    assert second.status == "refused"
    # an admitted-or-checked row is what makes a duplicate: move the row on, then send it again
    with store(project) as conn:
        conn.execute("UPDATE candidates SET status='checked'")
    third = propose(project, body)
    assert third.status == "duplicate"
    events_ = wiki_events(project)
    first_data, last = events_[0]["data"], events_[-1]["data"]
    for data in (e["data"] for e in events_):
        raw = json.dumps(data)
        assert "Task t-066 is blocked" not in raw and "a private aside" not in raw
    assert first_data["body"] is None and first_data["body_sha256"]
    assert set(last) == {"candidate_id", "kind", "status", "anchors"} and last["status"] == "duplicate"
    expected = [intake.quote_sha256(a["quote"]) for a in (*body["anchors"], *body["context"])]
    assert [a["quote_sha256"] for a in last["anchors"]] == expected
    assert [a["source_ref"] for a in last["anchors"]] == ["event:%d" % eid, "transcript:s-later"]
    assert all(set(a) == {"source_ref", "quote_sha256"} for a in last["anchors"])
    rows, anchors = tables(project)
    assert rows[0]["body_json"] is None and all(a["quote"] is None for a in anchors)
    # a rebuild writes the same rows
    before = tables(project)
    rebuild(project)
    after = tables(project)
    assert after[1] == before[1]
    assert {k: v for k, v in after[0][0].items() if k != "status"} == {
        k: v for k, v in before[0][0].items() if k != "status"}


def test_a_proposal_refused_for_a_transient_reason_can_be_sent_again_unchanged(project):
    eid = append(project, "result", {"body": "Task t-066 is blocked on the review."}, op="op-8",
                 ref="t-066", session="s-native")
    body = fact_proposal(project, eid)               # the event exists but its note is not captured yet
    first = propose(project, body)
    assert first.status == "refused" and not first.checks[0].passed
    capture(project)
    second = propose(project, body)                   # the same JSON, unchanged
    assert second.status == "checked" and second.candidate_id == first.candidate_id
    rows, anchors = tables(project)
    assert len(rows) == 1 and rows[0]["status"] == "checked" and rows[0]["record_event_id"] == second.record_event_id
    assert rows[0]["created_at"] == wiki_events(project)[0]["ts"]
    assert len(anchors) == 1 and anchors[0]["block_content_id"]
    third = propose(project, body)
    assert third.status == "duplicate"


def test_a_retry_with_fewer_context_entries_retires_the_old_ones(project):
    eid = append(project, "result", {"body": "Task t-066 is blocked on the review."}, op="op-8",
                 ref="t-066", session="s-native")
    body = fact_proposal(project, eid)
    body["context"] = [{"source_ref": "transcript:s-a", "quote": "one"},
                       {"source_ref": "transcript:s-a", "quote": "two"}]
    assert propose(project, body).status == "refused"
    capture(project)
    body["context"] = [{"source_ref": "transcript:s-a", "quote": "one"}]
    assert propose(project, body).status == "checked"
    (view,) = service.candidates(project)
    assert [a["role"] for a in view.anchors] == ["anchor", "context"]
    with store(project) as conn:
        assert conn.execute("SELECT COUNT(*) FROM candidate_anchors WHERE ordinal<0").fetchone()[0] == 1
    before = tables(project)
    rebuild(project)
    assert [a for a in tables(project)[1] if a["ordinal"] >= 0] == [a for a in before[1] if a["ordinal"] >= 0]


def test_candidates_filters_and_reports_freshness(project):
    eid = fact_event(project)
    one = propose(project, fact_proposal(project, eid))
    two = propose(project, fact_proposal(project, eid, quote="Task t-066 is blocked on the review"),
                  session="s-other")
    assert {v.candidate_id for v in service.candidates(project)} == {one.candidate_id, two.candidate_id}
    assert [v.candidate_id for v in service.candidates(project, session="s-other")] == [two.candidate_id]
    assert service.candidates(project, status="refused") == []
    assert len(service.candidates(project, kind="fact", status="checked")) == 2
    assert service.candidates(project, kind="summary") == []
    assert all(v.fresh for v in service.candidates(project))
    # the note is re-rendered without the quoted span: the anchor no longer resolves
    with store(project) as conn:
        conn.execute("UPDATE blocks SET status='superseded' WHERE doc_id LIKE '%000001-result.md' AND ordinal=1")
    assert not any(v.fresh for v in service.candidates(project))


def test_the_candidates_view_hides_what_the_servable_predicate_hides_now(project):
    eid = fact_event(project)
    propose(project, fact_proposal(project, eid))
    (view,) = service.candidates(project)
    assert view.body and view.anchors[0]["quote"] == "Task t-066 is blocked" and not view.body_redacted
    with store(project) as conn:        # a later exclusion
        conn.execute("INSERT INTO exclusions(source_ref,reason,requested_by,record_event_id,"
                     "recorded_at) VALUES('event:%d','x','owner',9,'x')" % eid)
    (view,) = service.candidates(project)
    assert view.body is None and view.body_redacted and view.body_sha256
    assert view.anchors[0]["quote"] is None and view.anchors[0]["redacted"] is True
    assert "Task t-066 is blocked" not in json.dumps(view.to_dict())


def test_candidates_without_a_store_is_empty(project):
    assert service.candidates(project) == []


def test_status_counts_candidates_by_status(project):
    eid = fact_event(project)
    propose(project, fact_proposal(project, eid))
    propose(project, fact_proposal(project, eid, obj="done"))
    assert service.status(project)["candidates"] == {"checked": 1, "refused": 1}


def test_the_cli_verbs_propose_and_candidates(project, tmp_path):
    eid = fact_event(project)
    path = tmp_path / "proposal.json"
    path.write_text(json.dumps(fact_proposal(project, eid)), encoding="utf-8")
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    env = dict(os.environ, PYTHONPATH=root)      # the project (cwd) holds the record, the worktree the code
    out = subprocess.run([sys.executable, "-m", "alpaca", "--session", "s-cli", "wiki", "propose",
                          "--file", str(path)], capture_output=True, text=True, cwd=project, env=env)
    assert out.returncode == 0, out.stderr
    receipt = json.loads(out.stdout)
    assert receipt["status"] == "checked" and len(receipt["checks"]) == 6, [c for c in receipt["checks"] if not c["passed"]]
    listed = subprocess.run([sys.executable, "-m", "alpaca", "wiki", "candidates", "--status", "checked"],
                            capture_output=True, text=True, cwd=project, env=env)
    assert [c["candidate_id"] for c in json.loads(listed.stdout)] == [receipt["candidate_id"]]
    last = [e for e in events(project) if e["kind"] == "wiki-candidate"][-1]
    assert (last["actor"], last["session"]) == ("alpaca-wiki", "s-cli")
    nosession = subprocess.run([sys.executable, "-m", "alpaca", "wiki", "propose", "--file", str(path)],
                               capture_output=True, text=True, cwd=project, env=env)
    assert nosession.returncode != 0 and "session" in nosession.stderr
