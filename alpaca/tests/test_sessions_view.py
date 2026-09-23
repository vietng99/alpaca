"""op-006 proof - one session classification, read from the record, used by every surface.

The complaint this answers: the analytics view "splits into way too many sub sessions" and
nothing says where the project is. Sixty-two of sixty-seven ids on the live record were the
desktop app opening a session for a second to run one local command; they flooded the session
list, the pad header and the pad's recent events.

Each control below asserts a POSITIVE and a NEGATIVE path, so none is a tautological pass:

  * a probe (open, close, nothing else) is a probe, and the same session with ONE heartbeat,
    ONE turn-end, one counted prompt or one event of any other kind is work;
  * the four synthetic writer ids are service whatever they carry;
  * active is measured against the record's own newest event, never the wall clock, so the same
    record classifies the same way an hour later;
  * claims, op progress, the recent filter and the noise counts each fold from the
    record and each report the absence honestly when the record holds none.
"""
import json

import pytest

from alpaca import clock, db, profile, sessions_view as sv, util


class Fake(profile.Profile):
    """A stand-in domain profile: each keyword replaces one hook."""

    def __init__(self, **hooks):
        self.__dict__.update(hooks)


@pytest.fixture
def conn(project):
    from alpaca import cli
    util.set_clock(clock.FixedClock("2026-05-01T00:00:00+00:00", step=0))
    cli.main(["init"])
    c = db.connect(project)
    yield c
    c.close()
    util.set_clock(None)


def _session(c, sid, **row):
    row.setdefault("started", util.now_iso())
    db.upsert(c, "sessions", "sid", dict(row, sid=sid))


def _event(c, sid, kind, **kw):
    return db.append_event(c, session=sid, actor="agent", kind=kind, **kw)


def _probe(c, sid):
    """Exactly what the desktop app leaves behind: a session row, an open and a close."""
    _session(c, sid, ended=util.now_iso(), beats=0)
    _event(c, sid, "session-start")
    _event(c, sid, "session-end")


# ------------------------------------------------------------------ probe versus work
def test_open_and_close_alone_is_a_probe(conn):
    _probe(conn, "probe-1")
    assert sv.classify(conn)["probe-1"]["class"] == sv.PROBE


@pytest.mark.parametrize("kind", ["heartbeat", "turn-end", "task-add", "session-checkpoint"])
def test_one_event_beyond_the_lifecycle_set_makes_it_work(conn, kind):
    _probe(conn, "s-1")
    assert sv.classify(conn)["s-1"]["class"] == sv.PROBE, "the same session starts as a probe"
    _event(conn, "s-1", kind)
    assert sv.classify(conn)["s-1"]["class"] == sv.WORK


def test_a_counted_prompt_alone_makes_it_work(conn):
    _probe(conn, "s-2")
    db.meta_set(conn, "prompts:s-2", "0")
    assert sv.classify(conn)["s-2"]["class"] == sv.PROBE, "zero prompts is still a probe"
    db.meta_set(conn, "prompts:s-2", "3")
    view = sv.classify(conn)
    assert view["s-2"]["class"] == sv.WORK and view["s-2"]["prompts"] == 3


def test_a_turn_level_hook_cost_makes_it_work_and_a_lifecycle_hook_does_not(conn):
    _probe(conn, "s-3")
    db.meta_set(conn, "hook-cost:s-3:session_start", json.dumps({"runs": 1, "total_ms": 4.0}))
    assert sv.classify(conn)["s-3"]["class"] == sv.PROBE, "opening and closing is not work"
    db.meta_set(conn, "hook-cost:s-3:stop", json.dumps({"runs": 1, "total_ms": 9.0}))
    assert sv.classify(conn)["s-3"]["class"] == sv.WORK


def test_a_transcript_snapshot_and_a_style_rearm_are_still_lifecycle(conn):
    _session(conn, "s-4", beats=0, ended=util.now_iso())
    _event(conn, "s-4", "session-start")
    _event(conn, "s-4", "transcript-snapshot")
    _event(conn, "s-4", "style-rearm")
    _event(conn, "s-4", "session-end")
    assert sv.classify(conn)["s-4"]["class"] == sv.PROBE


def test_a_session_that_has_not_closed_yet_is_work(conn):
    """It may yet do something. Only a sitting that opened and closed having done nothing was a
    probe, and the record can only say that once the session ended."""
    _session(conn, "s-5", beats=0)
    _event(conn, "s-5", "session-start")
    assert sv.classify(conn)["s-5"]["class"] == sv.WORK
    _session(conn, "s-5", beats=0, ended=util.now_iso())
    _event(conn, "s-5", "session-end")
    assert sv.classify(conn)["s-5"]["class"] == sv.PROBE


def test_the_synthetic_writer_ids_are_service(conn):
    for sid in sv.SERVICE_IDS:
        _event(conn, sid, "run", data={"gate": "g", "verdict": "PASS"})
    view = sv.classify(conn)
    for sid in sv.SERVICE_IDS:
        assert view[sid]["class"] == sv.SERVICE, sid
    # negative: an ordinary id carrying the same event is work, so the class comes from the id.
    _event(conn, "an-agent", "run", data={"gate": "g", "verdict": "PASS"})
    assert sv.classify(conn)["an-agent"]["class"] == sv.WORK


def test_class_of_answers_for_a_sid_the_record_never_named(conn):
    view = sv.classify(conn)
    assert sv.class_of(view, "never-seen") == sv.WORK, "a transcript on disk is a real sitting"
    assert sv.class_of(view, "cli") == sv.SERVICE


def test_a_profile_names_its_own_service_ids_and_side_effect_kinds(conn, monkeypatch):
    _event(conn, "domain-runner", "domain-receipt", data={"verdict": "PASS"})
    _probe(conn, "p-sync")
    _event(conn, "p-sync", "domain-receipt", data={"verdict": "PASS"})
    view = sv.classify(conn)
    assert view["domain-runner"]["class"] == sv.WORK and view["p-sync"]["class"] == sv.WORK
    monkeypatch.setattr(profile, "load", lambda root: Fake(events=lambda: {
        "service_sessions": ["domain-runner"], "side_effect_kinds": ["domain-receipt"]}))
    view = sv.classify(conn)
    assert view["domain-runner"]["class"] == sv.SERVICE
    assert view["p-sync"]["class"] == sv.PROBE


# ------------------------------------------------------------------ the reported fields
def test_the_fields_are_folded_from_the_record(conn):
    _session(conn, "w-1", level="L5", beats=0)
    db.meta_set(conn, "operator:w-1", "claude")
    db.meta_set(conn, "prompts:w-1", "2")
    for ref in ("a", "b"):
        _event(conn, "w-1", "heartbeat", data={"tool": "Bash", "ref": ref})
    _event(conn, "w-1", "turn-end")
    _event(conn, "w-1", "subagent-stop", data={"agent_id": "x"})
    info = sv.classify(conn)["w-1"]
    assert info["operator"] == "claude" and info["level"] == "L5"
    assert info["beats"] == 2 and info["turns"] == 1 and info["prompts"] == 2
    assert info["subagent_stops"] == 1 and info["events"] == 4
    assert info["last_tool"] == "Bash" and info["last_ref"] == "b", "the newest beat wins"


def test_probe_window_counts_and_spans_without_listing(conn):
    view = sv.classify(conn)
    assert sv.probe_window(view) == {"count": 0, "first": None, "last": None}
    util.set_clock(clock.FixedClock("2026-05-01T00:00:00+00:00", step=60))
    _probe(conn, "p-1")
    _probe(conn, "p-2")
    window = sv.probe_window(sv.classify(conn))
    assert window["count"] == 2
    assert window["first"] < window["last"]


# ------------------------------------------------------------------ the record clock
def test_active_is_measured_against_the_newest_event_not_the_wall_clock(conn):
    util.set_clock(clock.FixedClock("2026-05-01T00:00:00+00:00", step=0))
    _session(conn, "live", beats=0)
    _event(conn, "live", "heartbeat", data={"tool": "Read"})
    view, now = sv.classify(conn), sv.record_now(conn)
    assert sv.is_active(view["live"], now) is True
    # the wall clock runs on and nothing is appended: the record still reads the same way.
    util.set_clock(clock.FixedClock("2027-01-01T00:00:00+00:00", step=0))
    assert sv.is_active(sv.classify(conn)["live"], sv.record_now(conn)) is True
    # a newer event moves the record's own clock past the window, and only then is it idle.
    _event(conn, "other", "task-add")
    assert sv.is_active(sv.classify(conn)["live"], sv.record_now(conn)) is False


def test_a_session_that_never_beat_is_never_active(conn):
    _session(conn, "quiet", beats=0)
    _event(conn, "quiet", "task-add")
    view = sv.classify(conn)
    assert view["quiet"]["class"] == sv.WORK
    assert sv.is_active(view["quiet"], sv.record_now(conn)) is False


def test_work_sids_come_back_newest_activity_first(conn):
    util.set_clock(clock.FixedClock("2026-05-01T00:00:00+00:00", step=60))
    for sid in ("old", "mid", "new"):
        _session(conn, sid, beats=0)
        _event(conn, sid, "heartbeat", data={"tool": "Read"})
    assert sv.of_class(sv.classify(conn), sv.WORK) == ["new", "mid", "old"]


# ------------------------------------------------------------------ claims
def test_claims_are_keyed_on_the_authoring_session_and_expire_on_record_time(conn):
    util.set_clock(clock.FixedClock("2026-05-01T00:00:00+00:00", step=0))
    db.upsert(conn, "tasks", "id", {"id": "t-1", "op": "op-001", "statement": "s",
                                    "status": "doing"})
    _event(conn, "holder", "claim", ref="t-1",
           data={"worker": "w1", "lease_until": "2026-05-01T01:00:00+00:00"})
    now = sv.record_now(conn)
    assert sv.claims_by_session(conn, now) == {"holder": ["t-1"]}
    # a release by the same session drops it.
    _event(conn, "holder", "claim-release", ref="t-1", data={"worker": "w1"})
    assert sv.claims_by_session(conn, sv.record_now(conn)) == {}


def test_a_lease_that_ran_out_in_record_time_is_not_held(conn):
    util.set_clock(clock.FixedClock("2026-05-01T00:00:00+00:00", step=0))
    db.upsert(conn, "tasks", "id", {"id": "t-2", "op": "op-001", "statement": "s",
                                    "status": "doing"})
    _event(conn, "holder", "claim", ref="t-2",
           data={"worker": "w1", "lease_until": "2026-05-01T00:00:01+00:00"})
    assert sv.claims_by_session(conn, "2026-05-01T00:00:00+00:00") == {"holder": ["t-2"]}
    assert sv.claims_by_session(conn, "2026-05-01T09:00:00+00:00") == {}


def test_a_claim_on_a_finished_task_is_nobody_s_to_hold(conn):
    db.upsert(conn, "tasks", "id", {"id": "t-3", "op": "op-001", "statement": "s",
                                    "status": "doing"})
    _event(conn, "holder", "claim", ref="t-3", data={"worker": "w1", "lease_until": None})
    assert sv.claims_by_session(conn, sv.record_now(conn)) == {"holder": ["t-3"]}
    db.upsert(conn, "tasks", "id", {"id": "t-3", "op": "op-001", "statement": "s",
                                    "status": "done"})
    assert sv.claims_by_session(conn, sv.record_now(conn)) == {}


# ------------------------------------------------------------------ op progress
def _op(conn, oid, status="open", opened="2026-05-01T00:00:00+00:00", closed=None):
    db.upsert(conn, "ops", "id", {"id": oid, "intent": "do %s" % oid, "done_when": "done",
                                  "status": status, "opened": opened, "closed": closed,
                                  "phases": None})


def test_op_progress_counts_tasks_and_rows_per_op(conn):
    _op(conn, "op-001")
    for tid, status in (("t-1", "done"), ("t-2", "doing"), ("t-3", "open"), ("t-4", "blocked")):
        db.upsert(conn, "tasks", "id", {"id": tid, "op": "op-001", "statement": "s",
                                        "status": status})
    cards = [{"op": "op-001", "column": "done"}, {"op": "op-001", "column": "blocked"},
             {"op": "op-001", "column": "todo"}]
    row = sv.op_progress(conn, cards=cards, now_ts="2026-05-01T00:00:00+00:00")[0]
    assert row["id"] == "op-001" and row["status"] == "open" and row["title"] == "do op-001"
    assert (row["tasks_total"], row["tasks_done"], row["tasks_doing"], row["tasks_open"]) == (4, 1, 1, 1)
    assert (row["rows_total"], row["rows_done"], row["rows_blocked"]) == (3, 1, 1)


def test_open_ops_come_first_and_a_stale_closed_op_drops_off(conn):
    _op(conn, "op-001", opened="2026-05-01T00:00:00+00:00")
    _op(conn, "op-002", status="closed", opened="2026-04-01T00:00:00+00:00",
        closed="2026-05-01T09:00:00+00:00")
    _op(conn, "op-003", status="closed", opened="2026-01-01T00:00:00+00:00",
        closed="2026-02-01T00:00:00+00:00")
    got = [o["id"] for o in sv.op_progress(conn, cards=[], now_ts="2026-05-01T12:00:00+00:00")]
    assert got == ["op-001", "op-002"], "open first, then the op closed inside the day"
    assert "op-003" not in got, "an op closed long ago is not where the project is"


# ------------------------------------------------------------------ recent and noise
def test_recent_leaves_out_the_kinds_that_repeat_and_the_probe_lifecycle(conn):
    _probe(conn, "p-1")
    _session(conn, "w-1", beats=0)
    dropped = [_event(conn, "w-1", kind)["id"] for kind in
               ("heartbeat", "turn-end", "transcript-snapshot", "style-rearm", "subagent-stop")]
    dropped.append(_event(conn, "w-1", "run", data={"gate": "g", "verdict": "PASS"})["id"])
    kept = _event(conn, "w-1", "task-add", ref="t-1", data={"statement": "write the thing"})
    ids = [e["id"] for e in sv.recent_events(conn, sv.classify(conn), limit=8)]
    assert kept["id"] in ids, "the event a reader wants survives"
    assert not set(ids) & set(dropped), "the kinds that repeat are gone"
    assert not [e for e in sv.recent_events(conn, sv.classify(conn), limit=8)
                if e["session"] == "p-1"], "a probe's open and close are gone"
    # negative: a WORK session's own open and close are kept, so the filter is on the class.
    started = _event(conn, "w-1", "session-start")
    assert started["id"] in [e["id"] for e in sv.recent_events(conn, sv.classify(conn), limit=8)]


def test_recent_digs_past_a_long_run_of_probe_lifecycle_rows(conn):
    kept = _event(conn, "w-1", "task-add", ref="t-1", data={"statement": "s"})
    _session(conn, "w-1", beats=0)
    _event(conn, "w-1", "heartbeat")
    for n in range(60):
        _probe(conn, "p-%d" % n)
    ids = [e["id"] for e in sv.recent_events(conn, sv.classify(conn), limit=8)]
    assert kept["id"] in ids, "120 probe rows never starve the list"
    assert len(ids) <= 8


def test_noise_counts_the_three_kinds_from_one_event_forward(conn):
    first = _event(conn, "w-1", "task-add", ref="t-1", data={"statement": "s"})
    _session(conn, "w-1", beats=0)
    _event(conn, "w-1", "heartbeat")
    _event(conn, "w-1", "heartbeat")
    _event(conn, "w-1", "transcript-snapshot")
    _probe(conn, "p-1")
    view = sv.classify(conn)
    assert sv.noise_since(conn, view, first["id"]) == {"heartbeats": 2, "probe_sessions": 1,
                                                       "snapshots": 1}
    # negative: nothing counted before the anchor, and an absent anchor counts nothing.
    assert sv.noise_since(conn, view, None) == {"heartbeats": 0, "probe_sessions": 0,
                                                "snapshots": 0}


def test_summary_is_one_short_sentence_built_from_the_event(conn):
    move = _event(conn, "w-1", "task-move", ref="t-004",
                  data={"from": "doing", "to": "done", "proof": None})
    assert sv.summary_of(move) == "t-004 doing -> done"
    report = _event(conn, "w-1", "proof-report", ref="t-004",
                    data={"path": ".alpaca/proofs/op-001/t-004.md", "sha256": "a" * 64})
    assert sv.summary_of(report) == ".alpaca/proofs/op-001/t-004.md"
    receipt = _event(conn, "runner", "domain-receipt", ref="stage-a",
                     data={"stage": "stage-a", "verdict": "PASS", "reason": "acceptance passed"})
    assert sv.summary_of(receipt) == "", "a domain kind needs the profile to read it"
    fake = Fake(summarize=lambda e: "%s %s: %s" % (e["data"]["stage"], e["data"]["verdict"],
                                                   e["data"]["reason"]))
    assert sv.summary_of(receipt, fake) == "stage-a PASS: acceptance passed"
    note = _event(conn, "w-1", "msg", data={"to": "owner", "kind": "note", "body": "x" * 300})
    assert sv.summary_of(note) == "x" * 120
    assert sv.summary_of(_event(conn, "w-1", "claim", ref="t-1", data={"worker": "w"})) == "t-1 claimed by w"
    assert sv.summary_of(move, fake) == "t-004 doing -> done", "a known kind never asks the profile"
    row = _event(conn, "w-1", "verdict", ref="no-drift.ac-24", data={"verdict": "pass", "reason": "current"})
    assert sv.summary_of(row) == "no-drift.ac-24 PASS: current"
    # a kind with nothing a reader wants in a line stays empty
    assert sv.summary_of(_event(conn, "w-1", "authority-bind", data={"x": 1})) == ""


def test_a_burst_of_row_verdicts_reads_as_one_counted_line(conn):
    _event(conn, "w-1", "task-add", ref="t-1", data={"statement": "first"})
    for n in range(5):
        _event(conn, "w-1", "verdict", ref="row-%d" % n, data={"verdict": "PASS" if n else "BLOCKED"})
    view = sv.classify(conn)
    recent = sv.recent_events(conn, view, limit=25)
    kinds = [e["kind"] for e in recent]
    assert kinds.count("verdict") == 1, kinds
    line = sv.summary_of(next(e for e in recent if e["kind"] == "verdict"))
    assert line == "5 acceptance rows judged: 1 BLOCKED, 4 PASS"
    # a row verdict stores a numeric code beside its name: the name is what a reader gets
    coded = _event(conn, "w-2", "verdict", ref="row-c", data={"verdict": 2, "verdict_name": "BLOCKED", "reason": "no run"})
    assert sv.summary_of(coded) == "row-c BLOCKED: no run"
    # a single verdict keeps its own sentence
    _event(conn, "w-1", "task-add", ref="t-2", data={"statement": "second"})
    _event(conn, "w-1", "verdict", ref="row-9", data={"verdict": "FAIL", "reason": "drift"})
    recent = sv.recent_events(conn, sv.classify(conn), limit=25)
    assert sv.summary_of(recent[0]) == "row-9 FAIL: drift"


# ------------------------------------------------------------------ the shared reading
def test_the_pad_the_payload_and_the_fold_read_one_classification(project, conn):
    """The three surfaces do not each count for themselves: one module answers all of them."""
    import inspect
    from alpaca import export, pad
    from alpaca.analytics import build_index
    for module in (pad, export, build_index):
        assert "sessions_view" in inspect.getsource(module), module.__name__


def test_the_leader_is_the_session_that_actually_did_something(conn):
    util.set_clock(clock.FixedClock("2026-05-01T00:00:00+00:00", step=60))
    _session(conn, "real", beats=0)
    _event(conn, "real", "heartbeat", data={"tool": "Read"})
    # a session the desktop app opened a moment ago and has not closed: work, but it did nothing.
    _session(conn, "just-opened", beats=0)
    _event(conn, "just-opened", "session-start")
    view = sv.classify(conn)
    assert view["just-opened"]["class"] == sv.WORK and view["just-opened"]["worked"] is False
    assert sv.of_class(view, sv.WORK)[0] == "just-opened", "it is the most recent"
    assert sv.leader(view) == "real", "but the header names the one that did something"
    assert sv.leader(sv.classify(conn)) == "real"


def test_the_leader_is_none_on_a_record_with_no_working_session(conn):
    _probe(conn, "p-1")
    assert sv.leader(sv.classify(conn)) is None


def test_a_probe_that_only_refreshed_projections_on_its_way_out_stays_a_probe(conn):
    """SessionEnd refreshes the profile projections, so a session that did nothing can author row
    verdicts and gate runs. Those say the record moved, not that the session worked."""
    _probe(conn, "p-sync")
    _event(conn, "p-sync", "verdict", ref="row-1", data={"verdict": 2, "verdict_name": "BLOCKED"})
    _event(conn, "p-sync", "run", ref="flow:spec", data={"gate": "flow:spec", "verdict": "PASS"})
    assert sv.classify(conn)["p-sync"]["class"] == sv.PROBE
    # positive: one task move under the same id is work of its own
    _event(conn, "p-sync", "task-move", ref="t-1", data={"from": "open", "to": "doing"})
    assert sv.classify(conn)["p-sync"]["class"] == sv.WORK


def test_observability_collector_is_service_not_work(conn):
    _event(conn, "observability-collector", "capture-failed", data={"error": "test"})
    view = sv.classify(conn)
    assert sv.class_of(view, "observability-collector") == sv.SERVICE
    assert "observability-collector" not in sv.of_class(view, sv.WORK)
