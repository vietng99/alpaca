"""The review workflow: an assigned reviewer, a checked review, then the main session's decision."""
import hashlib
import json

import pytest

from alpaca import db
from alpaca.runlog import quotes, reviewflow as rf, reviews

RID = "c" * 32
LOG = "step one started\nstep one finished: 12 checks passed\n"


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "project"
    (root / "runs" / "logs").mkdir(parents=True)
    db.connect(str(root)).close()
    (root / "runs" / "logs" / (RID + ".log")).write_text(LOG)
    record(root, result="PASS")
    return root


def record(root, **run):
    (root / "runs" / (RID + ".json")).write_text(json.dumps(run))


def subject(root):
    def read(ref):
        path = root / "runs" / (ref + ".json")
        return json.loads(path.read_text()) if path.is_file() else None
    return read


def flow(root, **over):
    kw = dict(log=lambda ref: quotes.log_path(root / "runs" / "logs", ref),
              folder=lambda ref: root / "runs" / "reviews" / ref, subject=subject(root),
              locks=root / "runs" / "locks", kind="run-review", mark_kind="run-review-mark",
              workflow_kind="run-review-flow", refs=lambda: [p.stem for p in (root / "runs").glob("*.json")])
    kw.update(over)
    return rf.Flow(**kw)


def payload(root, **over):
    raw = (root / "runs" / "logs" / (RID + ".log")).read_bytes()
    data = {"receipt": RID, "log_sha256": hashlib.sha256(raw).hexdigest(), "reviewer": "test reviewer",
            "outcome": "matches-verdict", "summary": "Step one finished at line 2 with 12 checks passed.",
            "quick_summary": "Step one passed: all 12 checks passed.",
            "next_check": "Nothing to check before advancing.", "primary_finding": "f1",
            "findings": [{"line": 2, "severity": "info", "quote": "12 checks passed",
                          "meaning": "Every check in step one passed."}]}
    data.update(over)
    return data


def assigned(root, f=None):
    f = f or flow(root)
    a = rf.assign(root, f, RID, parent_session="main", reviewer_session="worker")
    return a, rf.submit(root, f, RID, payload(root), assignment=a["id"], session="worker")


def test_assignment_is_exclusive_and_a_resubmit_returns_the_kept_review(root):
    f = flow(root)
    assert rf.state(root, f, RID)["review_state"] == "pending"
    with pytest.raises(ValueError, match="differ"):
        rf.assign(root, f, RID, parent_session="main", reviewer_session="main")
    a = rf.assign(root, f, RID, parent_session="main", reviewer_session="worker")
    assert "do not record GO or NO-GO" in a["brief"] and a["id"] in a["brief"]
    assert rf.state(root, f, RID)["review_state"] == "reviewing"
    with pytest.raises(ValueError, match="already assigned"):
        rf.assign(root, f, RID, parent_session="other", reviewer_session="other-worker")
    with pytest.raises(ValueError, match="assigned reviewer"):
        rf.submit(root, f, RID, payload(root), assignment=a["id"], session="main")
    first = rf.submit(root, f, RID, payload(root), assignment=a["id"], session="worker")
    again = rf.submit(root, f, RID, payload(root), assignment=a["id"], session="worker")
    assert first["review"] == again["review"]
    s = rf.state(root, f, RID)
    assert s["review_state"] == "reviewed" and s["review_id"] == first["review"] and s["decision"] is None
    kept = reviews.reviews(root, RID, folder=f.folder(RID), kind=f.kind, mark_kind=f.mark_kind)["reviews"]
    assert len(kept) == 1 and kept[0]["assignment"] == a["id"] and kept[0]["reviewer_session"] == "worker"
    assert kept[0]["quick_summary"] == "Step one passed: all 12 checks passed."


def test_only_the_main_session_decides_and_a_changed_log_clears_go(root):
    f = flow(root)
    _, result = assigned(root, f)
    with pytest.raises(ValueError, match="needs a main-session GO"):
        rf.require_go(root, f, RID)
    with pytest.raises(ValueError, match="main session may decide"):
        rf.decide(root, f, RID, result["review"], "GO", "Evidence supports advancing", session="worker")
    d = rf.decide(root, f, RID, result["review"], "GO", "Evidence supports advancing", session="main")
    assert d["value"] == "GO" and d["event"]
    assert rf.require_go(root, f, RID)["decision"]["value"] == "GO"
    (root / "runs" / "logs" / (RID + ".log")).write_text(LOG + "late output\n")
    s = rf.state(root, f, RID)
    assert s["review_state"] == "stale" and s["decision"] is None and s["previous_decision"]["value"] == "GO"
    with pytest.raises(ValueError, match="needs a main-session GO"):
        rf.require_go(root, f, RID)


def test_a_failed_run_never_gets_go_and_no_go_keeps_the_result(root):
    record(root, result="FAIL", reason="step one exited 2")
    f = flow(root)
    _, result = assigned(root, f)
    with pytest.raises(ValueError, match="recorded PASS"):
        rf.decide(root, f, RID, result["review"], "GO", "Try to advance anyway", session="main")
    with pytest.raises(ValueError, match="before rerunning"):
        rf.require_retry(root, f, RID)
    rf.decide(root, f, RID, result["review"], "NO-GO", "Investigate the exit before a retry", session="main")
    assert json.loads((root / "runs" / (RID + ".json")).read_text())["result"] == "FAIL"
    s = rf.require_retry(root, f, RID)
    assert s["decision"]["value"] == "NO-GO" and s["next_action"] == "Investigate; do not advance"
    with pytest.raises(ValueError, match="needs a main-session GO"):
        rf.require_go(root, f, RID)


def test_three_refusals_fail_the_assignment_and_a_replacement_is_explicit(root):
    f = flow(root)
    a = rf.assign(root, f, RID, parent_session="main", reviewer_session="worker")
    bad = payload(root)
    bad["findings"][0]["quote"] = "this never happened"
    for _ in range(3):
        with pytest.raises(ValueError, match="quote is not in lines"):
            rf.submit(root, f, RID, bad, assignment=a["id"], session="worker")
    s = rf.state(root, f, RID)
    assert s["review_state"] == "failed" and s["assignment"]["attempts"] == 3 and "refused 3 times" in s["reason"]
    with pytest.raises(ValueError, match="failed"):
        rf.submit(root, f, RID, payload(root), assignment=a["id"], session="worker")
    b = rf.assign(root, f, RID, parent_session="main", reviewer_session="replacement")
    assert b["fence"] == a["fence"] + 1
    with pytest.raises(ValueError, match="assigned reviewer"):
        rf.submit(root, f, RID, payload(root), assignment=a["id"], session="worker")
    assert rf.submit(root, f, RID, payload(root), assignment=b["id"], session="replacement")["verdict"] == "PASS"


def test_a_missing_log_is_a_failed_review_that_can_still_be_held(root):
    f = flow(root)
    assert [s["ref"] for s in rf.pending(root, f)] == [RID]
    (root / "runs" / "logs" / (RID + ".log")).unlink()
    s = rf.state(root, f, RID)
    assert s["review_state"] == "failed" and "Captured log unavailable" in s["reason"]
    a = rf.assign(root, f, RID, parent_session="main", reviewer_session="worker")
    assert a["log_sha256"] is None
    with pytest.raises(ValueError, match="review failed"):
        rf.decide(root, f, RID, "0" * 16, "GO", "Advance without a log", session="main")
    rf.decide(root, f, RID, None, "NO-GO", "Recover the log before any retry", session="main")
    s = rf.state(root, f, RID)
    assert s["review_state"] == "failed" and s["decision"]["value"] == "NO-GO"
    assert rf.pending(root, f) == []
    rf.require_retry(root, f, RID)


def test_a_disputed_finding_withdraws_go(root):
    f = flow(root)
    _, result = assigned(root, f)
    rf.decide(root, f, RID, result["review"], "GO", "All required evidence holds", session="main")
    reviews.mark(root, RID, result["review"], "f1", "disputed", folder=f.folder(RID), kind=f.mark_kind, by="eng")
    s = rf.state(root, f, RID)
    assert s["disputed"] == 1 and s["decision"] is None and "disputed" in s["next_action"]
    with pytest.raises(ValueError, match="needs a main-session GO"):
        rf.require_go(root, f, RID)


def test_a_changed_run_record_makes_the_review_stale(root):
    f = flow(root)
    _, result = assigned(root, f)
    rf.decide(root, f, RID, result["review"], "GO", "The run passed as reviewed", session="main")
    record(root, result="PASS", note="rewritten")
    s = rf.state(root, f, RID)
    assert s["review_state"] == "stale" and "run record changed" in s["reason"]
    with pytest.raises(ValueError):
        rf.require_go(root, f, RID)


def test_the_callers_go_check_and_validate_can_refuse(root):
    def not_current(ref, review):
        raise ValueError("a newer run replaced this one")

    def needs_next_check(ref, review):
        if not review.get("next_check"):
            raise ValueError("next_check is required here")

    f = flow(root, go_check=not_current, validate=needs_next_check)
    a = rf.assign(root, f, RID, parent_session="main", reviewer_session="worker")
    with pytest.raises(ValueError, match="next_check is required"):
        rf.submit(root, f, RID, payload(root, next_check=None), assignment=a["id"], session="worker")
    assert rf.state(root, f, RID)["assignment"]["attempts"] == 1
    result = rf.submit(root, f, RID, payload(root), assignment=a["id"], session="worker")
    with pytest.raises(ValueError, match="newer run"):
        rf.decide(root, f, RID, result["review"], "GO", "The run passed as reviewed", session="main")


def test_lease_expiry_renew_and_takeover(root, monkeypatch):
    f = flow(root)
    clock = [1000.0]
    monkeypatch.setattr(rf, "_now", lambda: clock[0])
    a = rf.assign(root, f, RID, parent_session="main", reviewer_session="worker", minutes=5)
    with pytest.raises(ValueError, match="assigned reviewer"):
        rf.renew(root, f, RID, a["id"], session="main")
    assert rf.renew(root, f, RID, a["id"], session="worker", minutes=10)["expires_at"] == 1600.0
    with pytest.raises(ValueError, match="only an expired"):
        rf.takeover(root, f, RID, session="new-main", reason="the main chat closed")
    clock[0] = 1700.0
    assert rf.state(root, f, RID)["review_state"] == "pending"
    with pytest.raises(ValueError, match="expired"):
        rf.submit(root, f, RID, payload(root), assignment=a["id"], session="worker")
    with pytest.raises(ValueError, match="reassigned, not taken over"):
        rf.takeover(root, f, RID, session="new-main", reason="the main chat closed")
    b = rf.assign(root, f, RID, parent_session="main", reviewer_session="worker")
    result = rf.submit(root, f, RID, payload(root), assignment=b["id"], session="worker")
    clock[0] += 31 * 60
    with pytest.raises(ValueError, match="reviewer cannot become"):
        rf.takeover(root, f, RID, session="worker", reason="the main chat closed")
    rf.takeover(root, f, RID, session="new-main", reason="the main chat closed")
    with pytest.raises(ValueError, match="main session may decide"):
        rf.decide(root, f, RID, result["review"], "GO", "Evidence holds", session="main")
    assert rf.decide(root, f, RID, result["review"], "GO", "Evidence holds", session="new-main")["value"] == "GO"


def test_waiting_not_applicable_and_unassigned_reviews(root):
    f = flow(root)
    record(root, result="RUNNING")
    assert rf.state(root, f, RID)["review_state"] == "waiting"
    with pytest.raises(ValueError, match="Wait"):
        rf.assign(root, f, RID, parent_session="main", reviewer_session="worker")
    assert rf.pending(root, f) == []
    record(root, result="PASS", reviewable=False)
    assert rf.state(root, f, RID)["review_state"] == "not-applicable"
    assert rf.require_go(root, f, RID)["review_state"] == "not-applicable"
    record(root, result="PASS")
    manual = reviews.submit(root, RID, payload(root), log=f.log, folder=f.folder(RID), kind=f.kind)
    s = rf.state(root, f, RID)
    assert s["review_state"] == "reviewed" and "assign an independent reviewer" in s["next_action"]
    assert [p["ref"] for p in rf.pending(root, f)] == [RID]
    with pytest.raises(ValueError, match="main session may decide"):
        rf.decide(root, f, RID, manual["review"], "GO", "A manual review is enough", session="main")
    with pytest.raises(ValueError, match="unknown receipt"):
        rf.state(root, f, "d" * 32)


def test_glance_reads_the_review_and_decision_without_inventing_one(root):
    f = flow(root)
    g = rf.glance(root, f, RID)
    assert g["result"] == "PASS" and g["why"] == "No review yet" and g["review"] == "pending"
    assert g["decision"] == "awaiting decision" and g["evidence"] is None
    _, result = assigned(root, f)
    rf.decide(root, f, RID, result["review"], "GO", "Checks passed as the log shows", session="main")
    g = rf.glance(root, f, RID)
    assert g["why"] == "Step one passed: all 12 checks passed." and g["review"] == "reviewed"
    assert g["decision"] == "GO" and g["evidence"] == {"line": 2, "end_line": 2}
    lines = rf.glance_lines(g)
    assert lines[:5] == ["AT A GLANCE", "Result: PASS", "Why: Step one passed: all 12 checks passed.",
                         "Review: reviewed", "Main decision: GO"]
    assert "Decision reason: Checks passed as the log shows" in lines and "Evidence: line 2" in lines
    assert "Next check: Nothing to check before advancing." in lines
    with pytest.raises(ValueError, match="no review"):
        rf.glance(root, f, RID, review="0" * 16)


def test_empty_logs_short_fields_and_context(root):
    f = flow(root)
    (root / "runs" / "logs" / (RID + ".log")).write_text("")
    empty = payload(root, findings=[], primary_finding=None, summary="The run printed nothing at all to its log.")
    kept = quotes.check(f.log(RID), empty, ref=RID)
    assert kept["findings"] == [] and "primary_finding" not in kept
    (root / "runs" / "logs" / (RID + ".log")).write_text(LOG)
    with pytest.raises(ValueError, match="list of 1 to"):
        quotes.check(f.log(RID), payload(root, findings=[]), ref=RID)
    with pytest.raises(ValueError, match="primary_finding"):
        quotes.check(f.log(RID), payload(root, primary_finding="f9"), ref=RID)
    with pytest.raises(ValueError, match="quick_summary"):
        quotes.check(f.log(RID), payload(root, quick_summary="too short"), ref=RID)
    with pytest.raises(ValueError, match="context may not replace"):
        reviews.submit(root, RID, payload(root), log=f.log, folder=f.folder(RID), kind=f.kind,
                       context={"outcome": "matches-verdict"})


def test_reviews_kept_in_the_same_second_list_newest_first(root, monkeypatch):
    f = flow(root)
    from alpaca import util
    monkeypatch.setattr(util, "now_iso", lambda: "2026-09-29T12:00:00+07:00")
    first = reviews.submit(root, RID, payload(root), log=f.log, folder=f.folder(RID), kind=f.kind)
    second = reviews.submit(root, RID, payload(root, reviewer="second reviewer"), log=f.log,
                            folder=f.folder(RID), kind=f.kind)
    kept = reviews.reviews(root, RID, folder=f.folder(RID), kind=f.kind, mark_kind=f.mark_kind)["reviews"]
    assert [r["id"] for r in kept] == [second["review"], first["review"]]


def test_a_run_with_no_log_advances_only_on_pass(root):
    f = flow(root)
    record(root, result="FAIL", reviewable=False)
    with pytest.raises(ValueError, match="advances only on PASS"):
        rf.require_go(root, f, RID)
    rf.require_retry(root, f, RID)
    record(root, result="PASS", reviewable=False)
    assert rf.require_go(root, f, RID)["review_state"] == "not-applicable"


def test_a_decision_binds_the_record_the_assignment_saw(root):
    f = flow(root)
    a, result = assigned(root, f)
    rf.decide(root, f, RID, result["review"], "GO", "The run passed as reviewed", session="main")
    data = rf._read(root, f, RID)
    data["assignment"]["subject_sha256"] = "0" * 64      # the assignment saw another record
    rf._save(root, f, RID, data, "main")
    assert rf.state(root, f, RID)["review_state"] == "stale"
    with pytest.raises(ValueError):
        rf.require_go(root, f, RID)


def test_leases_are_bounded_and_decisions_survive_other_sessions(root, monkeypatch):
    f = flow(root)
    clock = [1000.0]
    monkeypatch.setattr(rf, "_now", lambda: clock[0])
    a, result = assigned(root, f)
    for bad in (10 ** 9, 0, -5, "5", True):
        with pytest.raises(ValueError, match="1 to 240"):
            rf.renew(root, f, RID, a["id"], session="worker", minutes=bad)
    with pytest.raises(ValueError, match="cannot fail now"):
        rf.fail(root, f, RID, a["id"], "changed my mind", session="worker")
    rf.decide(root, f, RID, result["review"], "GO", "The run passed as reviewed", session="main")
    clock[0] += 31 * 60
    with pytest.raises(ValueError, match="already has a GO from main"):
        rf.assign(root, f, RID, parent_session="other", reviewer_session="other-worker")
    rf.takeover(root, f, RID, session="new-main", reason="the main chat closed")
    assert rf.state(root, f, RID)["decision"]["value"] == "GO"
    b = rf.assign(root, f, RID, parent_session="new-main", reviewer_session="worker-2")
    assert b["parent_session"] == "new-main" and rf.state(root, f, RID)["decision"] is None


def test_every_kind_of_refusal_counts_toward_the_limit(root):
    def broken(ref, review):
        raise TypeError("checker bug")

    f = flow(root, validate=broken)
    a = rf.assign(root, f, RID, parent_session="main", reviewer_session="worker")
    with pytest.raises(ValueError, match="TypeError: checker bug"):
        rf.submit(root, f, RID, payload(root), assignment=a["id"], session="worker")
    f = flow(root)
    for _ in range(2):
        with pytest.raises(ValueError, match="primary_finding"):
            rf.submit(root, f, RID, payload(root, primary_finding=["f1"]), assignment=a["id"], session="worker")
    s = rf.state(root, f, RID)
    assert s["assignment"]["attempts"] == 3 and s["review_state"] == "failed"
    with pytest.raises(ValueError, match="context may not"):
        reviews.submit(root, RID, payload(root, primary_finding=None), log=f.log, folder=f.folder(RID),
                       kind=f.kind, context={"primary_finding": "f99"})


def test_glance_never_presents_a_stale_review_as_current(root):
    f = flow(root)
    _, result = assigned(root, f)
    (root / "runs" / "logs" / (RID + ".log")).write_text(LOG + "late output\n")
    g = rf.glance(root, f, RID)
    assert g["review"] == "stale" and g["evidence"] is None and g["next_check"] == ""
    assert g["why"] == "The kept review or the captured log changed"
    old = rf.glance(root, f, RID, review=result["review"])
    assert old["review"] == "stale" and old["why"].startswith("The kept review")
