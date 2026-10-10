"""op-008 t-069 stage s2-portable: the lifecycle converges (design/rune2-memory/architecture.md 7.6,
9.2, 9.3, 10.2; FR-17, FR-18, FR-22; contract done bars 1, 4 and 5).

Duplicate ingestion, an interrupted capture, an interrupted job step, a changed source, retractions
and retries each end in the store one clean pass builds: no missing event, no stale or withdrawn
evidence served, the same accepted digest as a rebuild from the record, and downstream reads (the
answer, the context pack, the briefing, the export) that follow the change. Withdrawal (a retrieval
exclusion) and a historical retraction are told apart by what stays served. Real record, real drain,
real store, the real job; the one injected fault (a process killed between the record append and the
store commit of a proposal) says so where it is used.
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import (T09, T10, T11, admit, append, at, capture, digest_of,  # noqa: E402
                            edges, events, ingest, llm_edges, note, proposal, propose,
                            rebuild_store, reset_vault, store, wiki_events)

from alpaca import db as record                                  # noqa: E402
from alpaca.wiki import consolidate, portable, recovery, service  # noqa: E402

ERR = "PrivatePathViolation: document privacy classification/path mismatch"


def move(project, to, ts, frm="open", task="t-9"):
    return at(project, "task-move", {"from": frm, "to": to, "proof": None, "reason": None}, ts,
              op="op-8", ref=task, actor="human")


def appender_at(project, ts, session="s-owner"):
    """The CLI's appender with the record clock at `ts`: the act stamps the store with its record
    event's time, so it lies after a knowledge time taken now (as T/test_withdrawal_propagation.py)."""
    from alpaca.wiki.clock import FixedClock

    def append_wiki(kind, data):
        conn = record.connect(str(project))
        try:
            return record.append_event(conn, session=session, actor="alpaca-wiki", kind=kind,
                                       data=data, clock=FixedClock(ts))["id"]
        finally:
            conn.close()
    return append_wiki


def missing(project) -> int:
    """How many record events that render as a source the store holds no note for (status)."""
    return service.status(project)["capture"]["missing_events"]


def versions(project) -> list:
    with store(project) as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT source_ref, status FROM source_versions ORDER BY source_ref, version_seq")]


def served(project, question) -> str:
    answer = {k: v for k, v in service.query(project, question).items() if k != "question"}
    return json.dumps([answer, service.context(project, question)])


def same_after_rebuild(project) -> None:
    """The store a single clean pass builds from the record: an equal accepted digest. The live store
    first lands what the hooks would (a `wiki-recovered` event of a catch-up is a source too)."""
    capture(project)
    live = digest_of(project)
    assert rebuild_store(project)["status"] == "ok"
    assert digest_of(project) == live


# ------------------------------------------------------------------ duplicate ingestion
def test_duplicate_ingestion_lands_each_source_once(project):
    """The same events captured by the drain, the capture door and `wiki recover`, and the same
    document ingested twice: one version per source, no corroboration counted twice, one note per
    event, and the accepted digest of one pass."""
    move(project, "doing", T09)
    move(project, "done", T10, frm="doing")
    note(project, "The lantern rota moved to Tuesday.", T11)
    ingest(project, "The kestrel queue drains nightly at two.\n", "wiki/notes/kestrel.md")
    capture(project)
    once = (digest_of(project), versions(project),
            [(e["edge_id"], e["corroboration_count"]) for e in edges(project)])
    capture(project)
    service.capture(project, sessions=["s-native"], session_events=True, deadline_s=None)
    service.capture(project, sessions=["s-native"], session_events=True, uncaptured_only=True,
                    deadline_s=None)
    ingest(project, "The kestrel queue drains nightly at two.\n", "wiki/notes/kestrel.md")
    assert recovery.run(project, session="s-recover")["ingested"] == 0
    assert (digest_of(project), versions(project),
            [(e["edge_id"], e["corroboration_count"]) for e in edges(project)]) == once
    assert {status for _, status in versions(project)} == {"current"}
    with store(project) as conn:
        notes = conn.execute("SELECT COUNT(*) FROM docs WHERE doc_id LIKE 'raw/%'").fetchone()[0]
    assert notes == len([e for e in events(project) if e["kind"] in ("task-move", "result")])
    # the `wiki-recovered` event the catch-up appended is a source too: the next capture lands it
    assert missing(project) == 1
    capture(project)
    assert missing(project) == 0


# ------------------------------------------------------------------ interrupted capture
def test_an_interrupted_capture_resumes_to_the_store_one_pass_builds(project):
    """A capture cut at its deadline lands nothing more and says what is left; the next capture of
    what is missing lands the rest once; status shows no missing event and a rebuild is equal."""
    ids = [move(project, "doing", T09), move(project, "done", T10, frm="doing"),
           note(project, "The lantern rota moved to Tuesday.", T11)]
    capture(project)
    later = [at(project, "result", {"body": "The heron gateway retried twice."}, T11),
             at(project, "result", {"body": "The osprey relay restarted."}, T11)]
    cut = service.capture(project, sessions=["s-native"], session_events=True, uncaptured_only=True,
                          deadline_s=0)
    assert cut["deferred"] == {"through_event": 0, "remaining": len(later)} and cut["notes"] == 0
    assert missing(project) == len(later)
    jobs = service.status(project)["jobs"][consolidate.CONSUMER]
    assert jobs["remaining"] == len(events(project))           # the job has read nothing yet
    done = service.capture(project, sessions=["s-native"], session_events=True, uncaptured_only=True,
                           deadline_s=None)
    assert done["deferred"] is None and done["notes"] == len(later)
    assert missing(project) == 0 and ids
    same_after_rebuild(project)


# ------------------------------------------------------------------ interrupted job (FR-18)
def test_a_killed_job_step_is_caught_up_and_proposes_nothing_twice(project, monkeypatch):
    """Restart and catch-up: a consolidation step killed after its proposal reached the record and
    before the store committed it (injected: the store write raises SystemExit) leaves the event and
    no row, and no checkpoint. `wiki recover` lands the row from the record; the next step, which
    starts again at the checkpoint, proposes nothing twice; `wiki status` then shows the queue empty
    and nothing failed."""
    from alpaca.wiki.store.write import Writer
    a = at(project, "drain-failed", {"error": ERR, "where": "pre_compact"}, T10, session="s-fail-1")
    b = at(project, "drain-failed", {"error": ERR, "where": "pre_compact"}, T11, session="s-fail-2")
    capture(project, {a, b})
    with monkeypatch.context() as m:
        def killed(self, row, anchors):
            raise SystemExit("killed between the record append and the store commit")
        m.setattr(Writer, "write_candidate", killed)
        with pytest.raises(SystemExit):
            consolidate.run_step(project, deadline_s=30)
    (event,) = wiki_events(project, "wiki-candidate")
    cid = event["data"]["candidate_id"]
    assert service.candidates(project) == []
    queue = service.status(project)["jobs"][consolidate.CONSUMER]
    assert queue["checkpoint_event_id"] == 0 and queue["remaining"] == len(events(project))
    report = recovery.run(project, session="s-recover")
    assert report["candidates"]["candidates_written"] == 1
    assert report["jobs"][consolidate.CONSUMER]["checkpoint_event_id"] == 0
    assert [v.candidate_id for v in service.candidates(project)] == [cid]
    step = consolidate.run_step(project, deadline_s=30)
    assert step.proposed == () and step.failed == ()
    assert len(wiki_events(project, "wiki-candidate")) == 1          # proposed once, in the record
    queue = service.status(project)["jobs"][consolidate.CONSUMER]
    assert queue["remaining"] == 0 and queue["failed"] == []
    assert queue["checkpoint_event_id"] == step.checkpoint_event_id == queue["head"]
    same_after_rebuild(project)
    assert [v.candidate_id for v in service.candidates(project)] == [cid]


# ------------------------------------------------------------------ a changed source
def test_a_changed_source_serves_only_its_current_version(project):
    """A document changed after it was captured: its new text is served and the old is not, a read
    pinned to a knowledge time before the change still sees the old text (the replaced version stays
    as history, architecture.md 5.2), and an export and import hold only the current version."""
    from alpaca.wiki.clock import FixedClock
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import Absorber
    question = "Which port does the osprey relay run on?"
    cfg = Config.for_project(project)
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    absorber = Absorber(cfg, clock=FixedClock(start=T09))
    try:
        absorber.absorb_text("wiki/notes/osprey.md", "The osprey relay runs on port one.\n",
                             kind="wiki", path="wiki/notes/osprey.md")
        eid = note(project, "A note so the record has an event.", T09)
        assert "port one" in served(project, question)
        absorber.writer.clock = lambda: T11
        absorber.absorb_text("wiki/notes/osprey.md", "The osprey relay moved to port two.\n",
                             kind="wiki", path="wiki/notes/osprey.md")
    finally:
        absorber.close()
    Path(cfg.vault_dir, "wiki", "notes").mkdir(parents=True, exist_ok=True)
    Path(cfg.vault_dir, "wiki", "notes", "osprey.md").write_text(
        "The osprey relay moved to port two.\n", encoding="utf-8")
    before_change = T10
    now = served(project, question)
    assert "port two" in now and "port one" not in now
    assert ("document:wiki/notes/osprey.md", "replaced") in versions(project)
    pinned = service.query(project, question, knowledge_as_of=before_change)
    assert "port one" in json.dumps(pinned.get("citations"))
    path = Path(project, "out.jsonl")
    portable.export_store(project, path)
    assert "port one" not in path.read_text(encoding="utf-8")
    reset_vault(project)
    report = portable.import_store(project, path)
    assert report["refused"] == [] and versions(project) == [
        ("document:wiki/notes/osprey.md", "current"), ("event:%d" % eid, "current")]
    after = served(project, question)
    assert "port two" in after and "port one" not in after


# ------------------------------------------------------------------ retractions (FR-22)
def test_exclusion_and_historical_retraction_differ_and_downstream_reads_follow(project):
    """A withdrawn source is served nowhere, at no knowledge time; a retracted fact is not served while
    the note it was read from still is; a superseded fact stays visible before the correction. The
    briefing, the context and the export follow each act, and a rebuild from the record is equal."""
    from datetime import datetime, timedelta, timezone
    from alpaca.wiki.clock import now_iso
    from alpaca.wiki.store.asof import AsOf
    move(project, "doing", T09, task="t-9")
    move(project, "done", T10, frm="doing", task="t-9")
    move(project, "doing", T09, task="t-8")
    gone = note(project, "The lantern rota moved to Tuesday.", T10)
    capture(project)
    status = {(e["subj_node"], e["obj_node"]): e for e in edges(project, predicate="status")}
    k_before = now_iso()                      # after the capture, before the acts below
    later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    k_after = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec="seconds")
    owner = {"session": "s-owner", "record": appender_at(project, later), "by": "owner"}
    fixed = service.correct(project, status[("t-9", "status:done")]["edge_id"], "supersede",
                            reason="it was blocked", replacement={"obj": "status:blocked",
                                                                 "obj_datatype": "node"}, **owner)
    assert fixed.status == "applied"
    retracted = service.correct(project, status[("t-8", "status:doing")]["edge_id"], "retract",
                                reason="never started", **owner)
    assert retracted.status == "applied"
    assert service.withdraw_source(project, "event:%d" % gone, session="s-owner",
                                   record=appender_at(project, later),
                                   reason="wrong source").status == "applied"

    def believed(k):
        """The status facts a read pinned to knowledge time `k` reads (the as-of clause)."""
        asof = AsOf(k_after, K=k)
        with store(project) as conn:
            return sorted((r[0], r[1]) for r in conn.execute(
                "SELECT subj_node, obj_node FROM edges WHERE predicate='status' AND " + asof.clause(),
                asof.params()))

    # history: before the acts the superseded fact is still believed; the retracted one is not,
    # at any knowledge time; after them the replacement is
    assert ("t-9", "status:done") in believed(k_before) and ("t-8", "status:doing") not in believed(k_before)
    assert ("t-9", "status:done") not in believed(k_after) and ("t-9", "status:blocked") in believed(k_after)
    # downstream: the briefing and the context state the corrected status, not the old one
    brief = service.briefing(project, task="t-9")
    assert "t-9 status is blocked" in brief.text and "t-9 status is done" not in brief.text
    assert "status is doing" not in service.briefing(project, task="t-8").text
    # the exclusion: served nowhere, also when pinned to a knowledge time before it
    assert "Tuesday" not in served(project, "When did the lantern rota move?")
    assert "Tuesday" not in json.dumps(service.query(project, "When did the lantern rota move?",
                                                     knowledge_as_of=T11))
    # the retraction: the fact is gone from current reads, the note it was read from is served
    now = {(e["subj_node"], e["obj_node"]): e["status"] for e in edges(project, predicate="status")}
    assert now[("t-8", "status:doing")] == "retracted" and now[("t-9", "status:done")] == "invalidated"
    assert now[("t-9", "status:blocked")] == "active"
    assert "t-8" in served(project, "What happened to task t-8?")
    # the export carries the exclusion and both acts, and no text of the withdrawn note
    path = Path(project, "out.jsonl")
    portable.export_store(project, path)
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert {r["action"] for r in lines if r["kind"] == "correction"} == {"supersede", "retract"}
    assert [r["source_ref"] for r in lines if r["kind"] == "exclusion"] == ["event:%d" % gone]
    assert "Tuesday" not in path.read_text(encoding="utf-8")
    same_after_rebuild(project)
    assert "t-9 status is blocked" in service.briefing(project, task="t-9").text


# ------------------------------------------------------------------ retries
def test_a_retried_proposal_admission_and_capture_converge(project):
    """A proposal refused while its note was not captured yet, sent again unchanged after the capture,
    is checked; a second admission is a duplicate; three capture boundaries land each event once.
    One candidate row, one active agent fact, and a rebuild equal to it."""
    eid = append(project, "result", {"body": "Task t-9 is blocked on the review."}, op="op-8", ref="t-9")
    body = proposal(project, eid, "Task t-9 is blocked")
    first = propose(project, body)
    assert first.status == "refused"
    for _ in range(3):                     # Stop, PreCompact and SessionEnd each capture the session
        service.capture(project, sessions=["s-native"], session_events=True, uncaptured_only=True,
                        transcripts=False, deadline_s=None)
    second = propose(project, body)
    assert second.status == "checked" and second.candidate_id == first.candidate_id
    assert admit(project, second.candidate_id).status == "applied"
    assert admit(project, second.candidate_id).status == "duplicate"
    assert [e["status"] for e in llm_edges(project)] == ["active"]
    assert versions(project) == [("event:%d" % eid, "current")] and missing(project) == 0
    with store(project) as conn:
        assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1
    same_after_rebuild(project)
    assert [e["status"] for e in llm_edges(project)] == ["active"]


def test_a_rebuilt_store_answers_like_the_live_one(project):
    """Convergence on the read side: the live store and its rebuild serve the same current answer
    to each question, with no missing event."""
    move(project, "doing", T09)
    move(project, "done", T10, frm="doing")
    note(project, "The lantern rota moved to Tuesday.", T11)
    capture(project)
    questions = ("When did the lantern rota move?", "What is the status of t-9?")
    live = [service.query(project, q) for q in questions]
    rebuild_store(project)
    again = [service.query(project, q) for q in questions]
    assert [(r["outcome"], r["answer_text"]) for r in again] == [
        (r["outcome"], r["answer_text"]) for r in live]
    capture(project)                       # the `wiki-recovered` event of the rebuild, as a hook would
    assert missing(project) == 0
    conn = record.connect_readonly(str(project))
    try:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] >= 3
    finally:
        conn.close()
