"""op-008 t-067 stage s2-observe: the bounded consolidation job (design/rune2-memory/architecture.md
9.2, 9.3; FR-16, FR-18; OB-23).

A step reads at most 200 record events after its checkpoint, turns repeated failure signatures and
superseded decisions into observation candidates through `propose` under `job:rune2-consolidate`,
and never admits. The caller (`consolidate.run_step`, which the `wiki consolidate` verb, the SessionEnd
hook and the collector call) writes the `rune2-consolidate` checkpoint and one failure event per
tried item that failed. Real record, real drain, real store, the real `propose`; the only injected
fault is a clock that runs out (said where it is used).
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import (T09, T10, T11, at, capture, digest_of, events, sockets_blocked,  # noqa: E402
                            store, wiki_events)

from alpaca import db as record                                  # noqa: E402
from alpaca.wiki import consolidate, intake, service            # noqa: E402

ERR = "PrivatePathViolation: document privacy classification/path mismatch"


def failure(project, n, error=ERR, ts=T10, where="pre_compact", session=None):
    return at(project, "drain-failed", {"error": error % n if "%d" in error else error, "where": where},
              ts, session=session or "s-fail-%d" % n)


def decision(project, new, old, ts=T11):
    return at(project, "decision", {"id": new, "kind": "scope", "choice": "adopt the new design",
                                    "consequence": "none", "context": "Supersedes %s (older design)." % old,
                                    "options": "[]", "pointer": "p", "ts": ts}, ts)


def checkpoint(project):
    conn = record.connect_readonly(str(project))
    try:
        row = conn.execute("SELECT event_id FROM obs_consumer_checkpoint WHERE consumer=?",
                           (consolidate.CONSUMER,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def kinds(project, kind):
    return [e for e in wiki_events(project, kind)]


def candidates(project):
    return service.candidates(project)


def failure_events(project):
    return [e["data"] for e in kinds(project, "rune2-consolidate-failure")]


def hide(project, source_ref):
    with store(project) as conn:
        conn.execute("INSERT INTO exclusions(source_ref,reason,requested_by,record_event_id,recorded_at) "
                     "VALUES(?,?,?,?,?)", (source_ref, "test", "owner", 1, "x"))


# ------------------------------------------------------------------ what a step proposes
def test_repeated_failures_and_superseded_decisions_become_observation_candidates(project):
    a, b = failure(project, 1), failure(project, 2)
    d = decision(project, "dec-bbbb", "dec-aaaa")
    capture(project, {a, b, d})
    result = consolidate.run_step(project, deadline_s=30)
    assert len(result.proposed) == 2 and not result.failed
    views = {v.candidate_id: v for v in candidates(project)}
    assert set(views) == set(result.proposed)
    texts = {v.body["text"]: v for v in views.values()}
    failure_view = texts["error: " + ERR]
    assert failure_view.kind == "observation" and failure_view.status == "checked"
    assert failure_view.proposer == consolidate.SESSION and failure_view.proposer_kind == "job"
    assert [x["source_ref"] for x in failure_view.anchors] == ["event:%d" % a, "event:%d" % b]
    assert failure_view.body["observation"]["falsifiability"] == consolidate.FALSIFY_FAILURE
    sup = texts["dec-bbbb supersedes dec-aaaa."]
    assert sup.status == "checked" and [x["source_ref"] for x in sup.anchors] == ["event:%d" % d] * 2
    assert [x["quote"] for x in sup.anchors] == ["id: dec-bbbb", "Supersedes dec-aaaa"]
    assert result.checkpoint_event_id >= d and checkpoint(project) == result.checkpoint_event_id


def test_a_failure_that_happened_once_or_with_other_text_is_not_a_signature(project):
    a = failure(project, 1)
    b = failure(project, 2, error="OSError: disk full")
    capture(project, {a, b})
    result = consolidate.run_step(project, deadline_s=30)
    assert result.proposed == () and result.failed == () and not candidates(project)


def test_digits_do_not_split_a_signature_and_a_third_event_makes_no_second_candidate(project):
    e = [failure(project, n, error="Timeout after %d seconds on shard 7") for n in (3, 40, 500)]
    capture(project, set(e))
    first = consolidate.run_step(project, deadline_s=30)
    assert len(first.proposed) == 1
    (view,) = candidates(project)
    assert [x["source_ref"] for x in view.anchors] == ["event:%d" % e[0], "event:%d" % e[1]]
    later = failure(project, 600, error="Timeout after %d seconds on shard 7", ts=T11)
    capture(project, {later})
    second = consolidate.run_step(project, deadline_s=30)
    assert second.proposed == () and len(candidates(project)) == 1


def test_a_rerun_is_idempotent(project):
    a, b = failure(project, 1), failure(project, 2)
    d = decision(project, "dec-bbbb", "dec-aaaa")
    capture(project, {a, b, d})
    first = consolidate.run_step(project, deadline_s=30)
    rows = candidates(project)
    seen = len(events(project))
    # the checkpoint is lost: the same events are read again, and nothing new is proposed or recorded
    conn = record.connect(str(project))
    try:
        conn.execute("DELETE FROM obs_consumer_checkpoint WHERE consumer=?", (consolidate.CONSUMER,))
    finally:
        conn.close()
    second = consolidate.run_step(project, deadline_s=30)
    assert second.proposed == () and second.failed == ()
    assert candidates(project) == rows and len(first.proposed) == 2
    assert len(events(project)) == seen                       # no duplicate wiki-candidate events
    # the second read passes the candidate events the first step appended, and finds nothing in them
    assert checkpoint(project) == second.checkpoint_event_id >= first.checkpoint_event_id
    assert second.remaining == 0


def test_the_step_never_admits_and_writes_no_gate_row(project):
    capture(project, {failure(project, 1), failure(project, 2), decision(project, "dec-bbbb", "dec-aaaa")})
    before = digest_of(project)
    consolidate.run_step(project, deadline_s=30)
    assert not wiki_events(project, "wiki-admission")
    assert {v.status for v in candidates(project)} == {"checked"}
    with store(project) as conn:
        for table in ("lessons_log", "knowledge_facts"):
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                assert conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0] == 0
    assert digest_of(project) == before                        # a candidate is not accepted state
    # and a job session cannot admit what it proposed
    cid = candidates(project)[0].candidate_id
    from native_helpers import admit
    assert admit(project, cid, session=consolidate.SESSION).reason == "job-cannot-admit"


def test_a_step_stops_at_its_deadline_and_resumes_from_the_checkpoint(project):
    """The one injected fault: a clock that runs out after the first item."""
    a, b = failure(project, 1), failure(project, 2)
    d = decision(project, "dec-bbbb", "dec-aaaa")
    capture(project, {a, b, d})

    class Clock:
        def __init__(self):
            self.calls = 0

        def __call__(self):
            self.calls += 1
            return 0.0 if self.calls <= 2 else 100.0         # started, first item checked, then out of time

    job = consolidate.ConsolidateJob()
    cut = job.step(project, deadline_s=10, record=consolidate.appender(project), clock=Clock())
    assert len(cut.proposed) == 1 and cut.remaining > 0
    assert cut.checkpoint_event_id == d - 1                  # held just before the item it did not reach
    consolidate.run_step(project, deadline_s=30, job=job)    # the caller writes the checkpoint from a real step
    assert len(candidates(project)) == 2
    assert checkpoint(project) >= d


def test_a_step_reads_at_most_the_window_of_events(project, monkeypatch):
    monkeypatch.setattr(consolidate, "MAX_ITEMS", 3)
    first = [failure(project, 1), failure(project, 2)]
    pad = at(project, "result", {"body": "padding one"}, T10)
    pad2 = at(project, "result", {"body": "padding two"}, T10)
    late = [failure(project, 3, error="OSError: disk full"), failure(project, 4, error="OSError: disk full")]
    capture(project, set(first) | {pad, pad2} | set(late))
    one = consolidate.run_step(project, deadline_s=30)
    assert one.checkpoint_event_id == pad and len(one.proposed) == 1 and one.remaining > 0
    two = consolidate.run_step(project, deadline_s=30)
    assert len(two.proposed) == 1 and len(candidates(project)) == 2


# ------------------------------------------------------------------ the callers
def test_the_wiki_consolidate_verb_runs_one_step(project, capsys):
    from alpaca import cli
    capture(project, {failure(project, 1), failure(project, 2)})
    capsys.readouterr()
    assert cli.main(["wiki", "consolidate", "--deadline-s", "30"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["proposed"]) == 1 and out["failed"] == []
    assert checkpoint(project) == out["checkpoint_event_id"]
    assert candidates(project)[0].proposer == consolidate.SESSION


def setup_default_session(project):
    # a session that ends with a failure pair already in the record; consolidation is off by default
    a, b = failure(project, 1, session="s-hook"), failure(project, 2, session="s-hook")
    return a, b


def test_session_end_with_the_default_configuration_runs_no_job_step(project):
    from alpaca.hooks import session_end
    setup_default_session(project)
    session_end.handle({"session_id": "s-hook", "cwd": str(project), "reason": "exit"})
    assert checkpoint(project) is None
    assert not wiki_events(project, "wiki-candidate") and not wiki_events(project, "wiki-admission")
    assert not kinds(project, "rune2-consolidate-failure")


def test_session_end_runs_one_step_only_when_the_key_is_true(project):
    from alpaca.hooks import session_end
    from native_helpers import set_config
    set_config(project, "wiki_providers:\n  consolidate_after_capture: true\n")
    setup_default_session(project)
    session_end.handle({"session_id": "s-hook", "cwd": str(project), "reason": "exit", "_strict": True})
    assert checkpoint(project) is not None
    assert len(kinds(project, "wiki-candidate")) == 1 and not wiki_events(project, "wiki-admission")
    assert [v.status for v in candidates(project)] == ["checked"]


def test_the_collector_wiki_consumer_follows_the_same_key(project):
    from alpaca.observability import consumers
    from native_helpers import set_config
    setup_default_session(project)
    rows = [dict(e, data=json.loads(e["data"])) for e in events(project)]
    consumers._wiki(project, rows)
    assert checkpoint(project) is None and not wiki_events(project, "wiki-candidate")
    set_config(project, "wiki_providers:\n  consolidate_after_capture: true\n")
    consumers._wiki(project, rows)
    assert checkpoint(project) is not None and len(kinds(project, "wiki-candidate")) == 1
    assert not wiki_events(project, "wiki-admission")


def test_a_consolidation_failure_after_capture_never_fails_the_hook(project, monkeypatch):
    from alpaca.hooks import session_end
    from native_helpers import set_config
    set_config(project, "wiki_providers:\n  consolidate_after_capture: true\n")
    setup_default_session(project)

    def boom(*a, **k):
        raise RuntimeError("step blew up")
    monkeypatch.setattr(consolidate, "run_step", boom)
    session_end.handle({"session_id": "s-hook", "cwd": str(project), "reason": "exit", "_strict": True})
    errors = [e["data"] for e in kinds(project, "drain-failed")
              if e["data"]["where"] == "session_end_consolidate"]
    assert len(errors) == 1
    assert "step blew up" in errors[0]["error"]


def test_consolidation_runs_with_sockets_blocked(project):
    capture(project, {failure(project, 1), failure(project, 2)})
    with sockets_blocked():
        assert len(consolidate.run_step(project, deadline_s=30).proposed) == 1


# ------------------------------------------------------------------ OB-23
def test_failure_event_not_repeated_after_cap(project):
    """OB-23: an item that fails on every step, run for five steps, has exactly three failure events
    with attempts 1, 2 and 3; a non-retryable class has one over three steps; `wiki status` lists
    both as failed."""
    # a repeated failure whose notes are not captured yet (K-1 anchor-unresolved: retryable) ...
    pending = [failure(project, 1, error="LockTimeout: store busy on shard %d"),
               failure(project, 2, error="LockTimeout: store busy on shard %d")]
    # ... and one whose notes are captured but excluded (K-4 excluded-anchor: not retryable)
    hidden = [failure(project, 3, error="DiskFull: no space on volume %d"),
              failure(project, 4, error="DiskFull: no space on volume %d")]
    capture(project, set(hidden))
    for eid in hidden:
        hide(project, "event:%d" % eid)
    steps = [consolidate.run_step(project, deadline_s=30) for _ in range(5)]

    by_key = {}
    for e in failure_events(project):
        by_key.setdefault(e["key"], []).append(e)
    retry_key = next(k for k in by_key if "LockTimeout".casefold() not in k and len(by_key[k]) == 3)
    stuck = by_key[retry_key]
    assert [e["attempts"] for e in stuck] == [1, 2, 3] and {e["class"] for e in stuck} == {"anchor-unresolved"}
    (fatal_key,) = [k for k in by_key if k != retry_key]
    fatal = by_key[fatal_key]
    assert [(e["attempts"], e["class"]) for e in fatal] == [(1, "excluded-anchor")]
    assert len(failure_events(project)) == 4                 # three plus one, over five steps

    # per step: the entries tried carry attempted True, a capped or non-retryable one never does
    def entry(step, key):
        return next((f for f in step.failed if f["key"] == key), None)
    assert [(entry(s, retry_key)["attempts"], entry(s, retry_key)["retryable"], entry(s, retry_key)["attempted"])
            for s in steps] == [(1, True, True), (2, True, True), (3, False, True),
                                (3, False, False), (3, False, False)]
    assert [entry(s, fatal_key)["attempted"] for s in steps] == [True, False, False, False, False]
    assert entry(steps[0], fatal_key)["retryable"] is False

    # a capped item no longer holds the checkpoint: it moved past both items by the third step
    assert steps[2].checkpoint_event_id >= max(pending + hidden)
    assert steps[1].checkpoint_event_id < max(pending)

    listed = {f["key"]: f for f in service.status(project)["jobs"][consolidate.CONSUMER]["failed"]}
    assert set(listed) == {retry_key, fatal_key}
    assert (listed[retry_key]["class"], listed[retry_key]["attempts"], listed[retry_key]["retryable"]) == (
        "anchor-unresolved", 3, False)
    assert (listed[fatal_key]["class"], listed[fatal_key]["attempts"], listed[fatal_key]["retryable"]) == (
        "excluded-anchor", 1, False)
    assert service.status(project)["jobs"][consolidate.CONSUMER]["checkpoint_event_id"] == checkpoint(project)


def test_a_retryable_item_that_lands_later_is_proposed_and_no_longer_listed(project):
    pending = [failure(project, 1), failure(project, 2)]
    first = consolidate.run_step(project, deadline_s=30)
    assert [(f["class"], f["attempts"], f["retryable"]) for f in first.failed] == [("anchor-unresolved", 1, True)]
    assert len(service.status(project)["jobs"][consolidate.CONSUMER]["failed"]) == 1
    capture(project, set(pending))                           # the notes land
    second = consolidate.run_step(project, deadline_s=30)
    assert len(second.proposed) == 1 and second.failed == ()
    assert service.status(project)["jobs"][consolidate.CONSUMER]["failed"] == []
    assert len(failure_events(project)) == 1                 # the earlier failure stays in the record
    assert second.checkpoint_event_id >= max(pending)


def test_the_failure_event_is_not_a_source_and_status_stays_honest(project):
    failure(project, 1), failure(project, 2)
    consolidate.run_step(project, deadline_s=30)
    report = service.status(project)
    assert report["capture"]["missing_events"] and report["jobs"][consolidate.CONSUMER]["failed"]
    from alpaca.wiki.ingest import drain
    assert drain.renders_as_source("rune2-consolidate-failure", {}) is False
    # its own failure events are not among the events the job reads for signatures
    again = consolidate.run_step(project, deadline_s=30)
    assert [f["key"] for f in again.failed if f["attempted"]] == [f["key"] for f in again.failed]
