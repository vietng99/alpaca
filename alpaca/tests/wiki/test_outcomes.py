"""op-008 t-062: query outcomes (FR-3; design/rune2-memory/architecture.md 5.1, 5.2, 6.1).

`outcome.classify_outcome` decides every outcome, first match wins, in the order of the 6.1 table.
Through the real service (no mock): grounded, historical, empty, abstained, partial, refused,
unavailable and the three `unsupported` reasons. A knowledge-time pin and an explicit valid time are
read from engine/plan.classify, so "as known at <date>" and a date in the question text give the
same refusals as the arguments, through both `query` and `context`. `conflict` and `stale` are
checked at the classifier, and `stale` also through the service (t-065): an edge-backed answer whose
anchor block no longer resolves. A scoped source the record names as missing is `unavailable` even
when the record holds only events (t-065, W-04).
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import append, capture, ingest, set_config, store  # noqa: E402

from alpaca.wiki import outcome as outcomes            # noqa: E402
from alpaca.wiki.api import EVAL_CLASS, Filters, Outcome  # noqa: E402

FUTURE = "2030-01-01T00:00:00+00:00"
FACT = "The widget cache uses SQLite because deployments run offline."
QUESTION = "widget cache SQLite offline"


def _base(**kw):
    args = dict(precheck=None, precheck_reason=None, door_verdict="abstained", served_ids=("b",),
                excluded_count=0, retrieved_count=1, cited_current=True, stale_only=False,
                scope_gaps=False, valid_time_unknown=False)
    args.update(kw)
    return outcomes.classify_outcome(**args)


def _undated(project):
    """FACT as an ingested wiki document: it has no source time (an event note has one since t-065)."""
    ingest(project, FACT + "\n", "wiki/widget-cache.md")


def _captured(project):
    eid = append(project, "result", {"body": FACT}, op="op-900", ref="t-900")
    capture(project)
    return eid


# ------------------------------------------------------------------ the 6.1 order, at the classifier
def test_every_outcome_is_reachable_in_the_architecture_order():
    assert _base(precheck="unsupported", precheck_reason="r") == (Outcome.UNSUPPORTED, "r")
    assert _base(valid_time_unknown=True) == (Outcome.UNSUPPORTED, "valid-time-unknown")
    assert _base(precheck="unavailable", precheck_reason="store-locked") == (
        Outcome.UNAVAILABLE, "store-locked")
    assert _base(served_ids=(), excluded_count=1) == (Outcome.REFUSED, "not-servable")
    assert _base(stale_only=True) == (Outcome.STALE, "anchor-unresolved")
    assert _base(door_verdict="conflict") == (Outcome.CONFLICT, None)
    assert _base(door_verdict="grounded", cited_current=False) == (Outcome.HISTORICAL, "not-current")
    assert _base(door_verdict="grounded") == (Outcome.GROUNDED, None)
    assert _base(scope_gaps=True) == (Outcome.PARTIAL, "scope-capture-gaps")
    assert _base(retrieved_count=0) == (Outcome.EMPTY, None)
    assert _base() == (Outcome.ABSTAINED, None)
    assert set(EVAL_CLASS) == {o.value for o in Outcome}


def test_first_match_wins():
    # unsupported beats valid-time-unknown's sibling rows and every door verdict
    assert _base(precheck="unsupported", precheck_reason="k", valid_time_unknown=True,
                 door_verdict="grounded")[0] is Outcome.UNSUPPORTED
    assert _base(valid_time_unknown=True, precheck="unavailable")[1] == "valid-time-unknown"
    assert _base(precheck="unavailable", served_ids=(), excluded_count=1)[0] is Outcome.UNAVAILABLE
    assert _base(served_ids=(), excluded_count=1, door_verdict="conflict")[0] is Outcome.REFUSED
    assert _base(stale_only=True, door_verdict="grounded")[0] is Outcome.STALE
    assert _base(door_verdict="conflict", scope_gaps=True)[0] is Outcome.CONFLICT
    assert _base(door_verdict="grounded", scope_gaps=True)[0] is Outcome.GROUNDED
    assert _base(scope_gaps=True, retrieved_count=0)[0] is Outcome.PARTIAL
    # a served block next to an excluded one is not a refusal
    assert _base(excluded_count=1)[0] is Outcome.ABSTAINED


def test_precheck_reads_filters_store_and_scope():
    # t-065: structured filters are read by the door; only a value that cannot apply is refused
    assert outcomes.precheck(filters=Filters(op="op-1")) == (None, None)
    assert outcomes.precheck(filters=Filters(fact_kind="guess")) == ("unsupported", "filter-invalid")
    assert outcomes.precheck(filters=Filters(task="")) == ("unsupported", "filter-invalid")
    assert outcomes.precheck(filters=Filters()) == (None, None)
    assert outcomes.precheck(store_locked=True) == ("unavailable", "store-locked")
    assert outcomes.precheck(missing_transcripts_in_scope=("s",)) == (
        "unavailable", "scoped-source-missing")


# ------------------------------------------------------------------ through the real service
def test_grounded_empty_and_abstained(project):
    from alpaca.wiki import service
    _captured(project)
    result = service.query(project, QUESTION)
    assert (result["outcome"], result["verdict"], result["reason"]) == ("grounded", "grounded", None)
    assert FACT in result["answer_text"]
    assert service.query(project, "zebra quantum nebula")["outcome"] == "empty"
    # retrieves the captured block, which the door then refuses (unmet SUBCLAIM_COVERAGE)
    abstained = service.query(project, "widget deployments marmalade harpsichord")
    assert abstained["extra"]["retrieved"] >= 1
    assert (abstained["outcome"], abstained["verdict"]) == ("abstained", "abstained")
    assert abstained["answer_text"] is None


def test_knowledge_pin_from_argument_is_served_since_t065(project):
    from alpaca.wiki import service
    _captured(project)
    result = service.query(project, QUESTION, knowledge_as_of=FUTURE)
    assert (result["outcome"], result["reason"]) == ("grounded", None)
    assert FACT in result["answer_text"]
    assert result["currency_stamp"]["txn_axis"] == FUTURE


def test_knowledge_pin_from_question_text_is_read_in_query_and_context(project):
    """"as known at <date>" pins K like the argument; the extra words of the question are not in the
    note, so the door abstains, but the read is served, never refused for its time pin."""
    from alpaca.wiki import service
    _captured(project)
    question = QUESTION + " as known at 2030-01-01"
    result = service.query(project, question)
    assert result["outcome"] != "unsupported" and result["reason"] != "knowledge-time-read-not-supported"
    assert result["currency_stamp"]["txn_axis"].startswith("2030-01-01")
    text = service.context(project, question)
    assert "unsupported" not in text


def test_valid_time_unknown_from_argument(project):
    from alpaca.wiki import service
    _undated(project)
    result = service.query(project, QUESTION, as_of=FUTURE)
    assert (result["outcome"], result["reason"]) == ("unsupported", "valid-time-unknown")
    assert result["answer_text"] is None and result["sources"] == []
    assert FACT not in json.dumps(result)


def test_valid_time_unknown_from_date_in_question_text_query_and_context(project):
    from alpaca.wiki import service
    _undated(project)
    question = QUESTION + " 2030-01-01"
    result = service.query(project, question)
    assert (result["outcome"], result["reason"]) == ("unsupported", "valid-time-unknown")
    text = service.context(project, question)
    assert "Outcome: unsupported (valid-time-unknown)" in text
    assert FACT not in text


def _ada(project, *, with_source_time: bool):
    """Two dated wiki sources of one single-cardinality fact: the later one ends the earlier one's
    valid window (a world change). Capture clock 2019, before both."""
    from alpaca.wiki.clock import FixedClock
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import Absorber
    set_config(project, "wiki_providers:\n  meta:\n    retrieval_profile: full\n")
    cfg = Config.for_project(project)
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    absorber = Absorber(cfg, clock=FixedClock(start="2019-01-01T00:00:00+00:00", step=0))
    try:
        for doc, obj, day in (("wiki/ada-2020.md", "Acme", "2020-01-01"),
                              ("wiki/ada-2021.md", "Globex", "2021-06-01")):
            absorber.absorb_text(doc, "[[Ada]] works at [[%s]] @%s\n" % (obj, day), kind="wiki",
                                 source_created_at=(day + "T00:00:00+00:00")
                                 if with_source_time else None)
    finally:
        absorber.close()


def test_historical_when_nothing_cited_is_current(project):
    from alpaca.wiki import service
    _ada(project, with_source_time=True)
    past = service.query(project, "Ada works at", as_of="2020-06-01T00:00:00+00:00")
    assert (past["outcome"], past["reason"]) == ("historical", "not-current")
    assert "acme" in past["answer_text"]
    now = service.query(project, "Ada works at")
    assert (now["outcome"], now["verdict"]) == ("grounded", "grounded")
    assert "globex" in now["answer_text"]


def test_known_source_time_is_not_valid_time_unknown_but_null_is(project):
    from alpaca.wiki import service
    _ada(project, with_source_time=False)
    result = service.query(project, "Ada works at", as_of="2020-06-01T00:00:00+00:00")
    assert (result["outcome"], result["reason"]) == ("unsupported", "valid-time-unknown")
    assert result["answer_text"] is None


def test_valid_time_unknown_on_a_conflict_withholds_the_conflict_note(project):
    """A conflict whose cited items all lack a source time, asked at an explicit T, is
    unsupported (valid-time-unknown): the answer text is withheld, and so is the conflict note
    that names the winning fact."""
    from alpaca.wiki import service
    from alpaca.wiki.clock import FixedClock
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import Absorber
    set_config(project, "wiki_providers:\n  meta:\n    retrieval_profile: full\n")
    cfg = Config.for_project(project)
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    absorber = Absorber(cfg, clock=FixedClock(start="2019-01-01T00:00:00+00:00", step=0))
    try:
        for doc, text in (("wiki/ada.md", "[[Ada]] works at [[Acme]]\n"),
                          ("wiki/bob.md", "[[Bob]] works at [[Hooli]]\n"),
                          ("wiki/bob2.md", "[[Bob]] works at [[Acme]]\n")):
            absorber.absorb_text(doc, text, kind="wiki", source_created_at=None)
    finally:
        absorber.close()
    now = service.query(project, "Where does Ada currently work")
    assert now["verdict"] == "conflict" and now["extra"]["conflict_note"], \
        "setup: the door rules a conflict and names a winner"
    dated = service.query(project, "Where does Ada currently work",
                          as_of="2025-01-01T00:00:00+00:00")
    assert dated["verdict"] == "conflict"
    assert (dated["outcome"], dated["reason"]) == ("unsupported", "valid-time-unknown")
    assert dated["answer_text"] is None
    assert dated["extra"]["conflict_note"] is None
    assert "works_at" not in json.dumps(dated)


def test_a_filter_value_that_cannot_apply_is_unsupported(project):
    from alpaca.wiki import service
    _captured(project)
    result = service.query(project, QUESTION, filters=Filters(fact_kind="guess"))
    assert (result["outcome"], result["reason"]) == ("unsupported", "filter-invalid")
    assert service.query(project, QUESTION, filters=Filters(op="op-900"))["outcome"] == "grounded"
    assert service.query(project, QUESTION, filters=Filters())["outcome"] == "grounded"


def test_locked_store_is_unavailable_and_runs_no_door(project):
    from alpaca.wiki import service
    from alpaca.wiki.store.db import REBUILD_LOCK_NAME
    _captured(project)
    lock = Path(project, ".alpaca", "wiki", REBUILD_LOCK_NAME)
    lock.write_text("rebuild-token-held-elsewhere", encoding="ascii")
    try:
        result = service.query(project, QUESTION)
        scoped = service.query(project, QUESTION + " op-900")
    finally:
        lock.unlink()
    assert (result["outcome"], result["reason"]) == ("unavailable", "store-locked")
    assert result["verdict"] is None and result["answer_text"] is None
    # The locked store cannot be read, so the capture report says so instead of calling every
    # captured event missing and pointing at `wiki recover`.
    for locked in (result, scoped):
        assert locked["capture"]["capture_state"] == "unavailable-store-locked"
        assert locked["capture"]["missing_events"] is None
        assert locked["capture"]["captured_events"] is None
        assert locked["capture"]["recovery"] is None
        assert locked["coverage"]["project_missing_events"] is None
        assert list(locked["coverage"]["missing_event_ids_in_scope"]) == []
    assert scoped["coverage"]["scope"]["op"] == "op-900"
    readable = service.query(project, QUESTION + " op-900")
    assert readable["capture"]["capture_state"] == "read"
    assert readable["capture"]["missing_events"] == 0 and readable["capture"]["recovery"] is None


def test_partial_when_abstaining_with_capture_gaps_in_scope(project):
    from alpaca.wiki import service
    _captured(project)
    gap = append(project, "result", {"body": "uncaptured op-900 result"}, op="op-900", ref="t-900")
    result = service.query(project, "op-900 zebra quantum nebula")
    assert (result["outcome"], result["reason"]) == ("partial", "scope-capture-gaps")
    assert result["coverage"]["scope"]["op"] == "op-900"
    assert result["coverage"]["missing_event_ids_in_scope"] == [gap]
    assert result["coverage"]["project_missing_events"] == 1
    # a project-wide gap outside the question's scope never makes an abstention partial
    other = service.query(project, "op-777 zebra quantum nebula")
    assert other["outcome"] in ("empty", "abstained")
    assert other["coverage"]["missing_event_ids_in_scope"] == []


def test_scoped_missing_transcript_is_unavailable(project):
    from alpaca import observability
    from alpaca.wiki import service
    _captured(project)
    observability.expect_source(project, "s-gone", "claude", locator=str(Path(project, "gone.jsonl")))
    result = service.query(project, "what did s-gone do")
    assert (result["outcome"], result["reason"]) == ("unavailable", "scoped-source-missing")
    assert result["coverage"]["missing_transcripts_in_scope"] == ["s-gone"]


def test_refused_when_every_match_is_private(project):
    from alpaca.wiki import service
    secret = "The orchid ledger vault password rotates every Thursday."
    ingest(project, secret, "wiki/private/orchid.md")
    result = service.query(project, "orchid ledger vault password")
    assert (result["outcome"], result["reason"]) == ("refused", "not-servable")
    assert "thursday" not in json.dumps(result).lower()
    assert result["answer_text"] is None and result["sources"] == []
    text = service.context(project, "orchid ledger vault password")
    assert "Outcome: refused (not-servable)" in text
    assert "Thursday" not in text
    # the door's own Answer carries only a count of the removed matches, not their block ids
    from alpaca.wiki.config import Config
    from alpaca.wiki.engine.answer import answer as door
    raw = door(Config.for_project(project), "orchid ledger vault password")
    assert raw.extra["excluded"] >= 1 and "excluded_ids" not in raw.extra
    assert "wiki/private" not in json.dumps(raw.to_dict())


def test_every_query_on_a_reachable_store_records_its_outcome(project):
    """One answer_outcomes row per query whose store is reachable: through the door (grounded) and
    before it (unsupported, scoped-source-missing). A locked store takes no write."""
    from alpaca import observability
    from alpaca.wiki import service
    from alpaca.wiki.store.db import REBUILD_LOCK_NAME
    _captured(project)
    service.query(project, QUESTION)
    service.query(project, QUESTION, filters=Filters(fact_kind="guess"))
    observability.expect_source(project, "s-gone", "claude", locator=str(Path(project, "gone.jsonl")))
    assert service.query(project, "what did s-gone do")["reason"] == "scoped-source-missing"
    lock = Path(project, ".alpaca", "wiki", REBUILD_LOCK_NAME)
    lock.write_text("rebuild-token-held-elsewhere", encoding="ascii")
    try:
        assert service.query(project, QUESTION)["reason"] == "store-locked"
    finally:
        lock.unlink()
    with store(project) as conn:
        rows = conn.execute("SELECT outcome, reason, answer_id FROM answer_outcomes "
                            "ORDER BY id").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("grounded", None),
                                            ("unsupported", "filter-invalid"),
                                            ("unavailable", "scoped-source-missing")]
    assert rows[0][2] and rows[1][2] is None and rows[2][2] is None


# ------------------------------------------------------------------ t-065: W-04, record-derived scope
def _snapshot(project, *children, session="s-parent"):
    """A transcript-snapshot event as the hooks write it: `registry_children` lists each child
    transcript with the mode the snapshot found it in."""
    return append(project, "transcript-snapshot", {
        "bytes": 10, "generation": 1, "schema": 1, "source_missing": False, "subagents": 0,
        "registry_children": [{"mode": mode, "path": ".te/transcripts/%s.jsonl" % sid,
                               "retained": mode != "missing", "session": sid, "source_id": "x"}
                              for sid, mode in children]}, session=session)


MISSING_QUESTION = "What did subagent a-gone report when it finished its work?"


def test_a_session_the_record_reports_missing_is_unavailable(project):
    """The evaluation record holds only events, so no session or registration row exists: the
    snapshot event is the only place the record says the child transcript is missing."""
    from alpaca.wiki import service
    _captured(project)
    append(project, "subagent-stop", {"agent_id": "a-gone"}, session="s-parent")
    _snapshot(project, ("a-gone", "missing"), ("a-here", "copy"))
    capture(project)
    result = service.query(project, MISSING_QUESTION)
    assert (result["outcome"], result["reason"]) == ("unavailable", "scoped-source-missing")
    assert result["coverage"]["missing_transcripts_in_scope"] == ["a-gone"]
    assert result["answer_text"] is None and result["sources"] == []
    # a child the snapshot retained is not missing; a question that names no missing session is unchanged
    assert service.query(project, "What did subagent a-here report?")["outcome"] != "unavailable"
    assert service.query(project, QUESTION)["outcome"] == "grounded"
    capture_report = service.status(project)["capture"]
    assert capture_report["missing_transcripts"] == 1
    assert capture_report["missing_transcript_sessions"] == ["a-gone"]
    text = service.context(project, MISSING_QUESTION)
    assert "Outcome: unavailable (scoped-source-missing)" in text and "No supported answer" in text


def test_a_later_report_of_the_record_clears_a_missing_session(project):
    from alpaca.wiki import service
    _captured(project)
    _snapshot(project, ("a-gone", "missing"))
    assert service.query(project, MISSING_QUESTION)["outcome"] == "unavailable"
    _snapshot(project, ("a-gone", "copy"))                       # a later snapshot retained it
    assert service.query(project, MISSING_QUESTION)["outcome"] != "unavailable"
    assert service.status(project)["capture"]["missing_transcripts"] == 0
    # a wiki-recovery-incomplete event reports the same fact as the snapshot
    append(project, "wiki-recovery-incomplete", {"ingested": 1, "missing_transcripts": [
        {"error": "registered transcript is unavailable: a-lost", "session": "a-lost",
         "source": "/gone/agent-a-lost.jsonl"}]}, session="cli")
    result = service.query(project, "What did subagent a-lost report?")
    assert (result["outcome"], result["reason"]) == ("unavailable", "scoped-source-missing")


def test_a_session_whose_transcript_the_store_holds_is_not_missing(project):
    from alpaca.wiki import service
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import Absorber
    _captured(project)
    _snapshot(project, ("a-gone", "missing"))
    cfg = Config.for_project(project)
    absorber = Absorber(cfg)
    try:
        absorber.absorb_text("raw/transcripts/x/transcript-a-gone.md",
                             "role: transcript\nop: session\nsession: a-gone\n\nThe skiff left at dawn.\n",
                             kind="raw")
    finally:
        absorber.close()
    assert service.query(project, MISSING_QUESTION)["outcome"] != "unavailable"
    assert service.status(project)["capture"]["missing_transcripts"] == 0


def test_stale_when_the_only_support_is_an_edge_whose_anchor_does_not_resolve(project):
    """A question that reaches only the earlier block follows the earlier edge's `superseded_by` to
    its active successor and cites the successor's block (engine/loop.py `_follow_active_successor`).
    When that block is no longer active the successor has no anchor: the answer is `stale` with no
    text, never `historical` or `grounded`. Setup rows: the earlier edge is superseded by hand and
    the successor's block retired without retracting its edge. The Writer retracts both together,
    so this is the corrupt state Z6 guards against."""
    from alpaca.wiki import service
    _ada(project, with_source_time=True)
    with store(project) as conn:
        old, new = (r[0] for r in conn.execute(
            "SELECT edge_id FROM edges WHERE obj_node IN ('acme','globex') ORDER BY obj_node"))
        block = conn.execute("SELECT source_block_id FROM edges WHERE edge_id=?", (new,)).fetchone()[0]
        conn.execute("UPDATE edges SET status='invalidated', superseded_at='2021-06-01T00:00:00+00:00', "
                     "superseded_by_edge_id=? WHERE edge_id=?", (new, old))
        conn.execute("UPDATE blocks SET status='superseded' WHERE block_id=?", (block,))
    result = service.query(project, "Ada works at")
    assert (result["outcome"], result["reason"]) == ("stale", "anchor-unresolved")
    assert result["answer_text"] is None and result["sources"] == []
    assert "Globex" not in json.dumps(result)
    text = service.context(project, "Ada works at")
    assert "Outcome: stale (anchor-unresolved)" in text and "Globex" not in text
