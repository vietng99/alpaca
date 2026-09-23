"""Work provenance must follow recorded transitions and safe linked report text."""
import hashlib
import json

import pytest

from alpaca import db, hub, hub_checklist


@pytest.fixture
def record(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    conn = db.connect(root)
    yield root, conn
    conn.close()


def event(conn, kind, ref, stamp, session="finishing-session", **data):
    return db.append_event(conn, session=session, actor="reviewer", kind=kind,
                           ref=ref, data=data, clock=lambda: stamp)


def task(conn, ident="t-023", **fields):
    db.upsert(conn, "tasks", "id", {"id": ident, "statement": "Document measured results",
              "op": "op-001", "status": "done", "claimant": "different-claimant", **fields})


def put(root, rel, value):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    return path


def test_task_completion_uses_transition_session_and_survives_sort_changes(record):
    root, conn = record
    task(conn, created="2026-09-20T09:00:00Z", updated="2026-09-21T10:00:00Z", why="Explain the measured outcome")
    event(conn, "task-add", "t-023", "2026-09-20T09:00:00Z", session="starting-session")
    done = event(conn, "task-move", "t-023", "2026-09-21T10:00:00Z", **{"from": "doing", "to": "done"})
    item = hub.overview(root)["tasks"][0]
    assert item["number"] == 23
    assert item["created_at"] == "2026-09-20T09:00:00Z"
    assert item["updated_at"] == item["completed_at"] == "2026-09-21T10:00:00Z"
    assert item["completed_session"] == "finishing-session"
    assert item["completed_actor"] == "reviewer"
    assert item["completion_event_id"] == done["id"]
    assert item["purpose"] == "Explain the measured outcome"
    assert item["source"] is None and item["ref"] == "t-023"
    task(conn, "t-024", status="doing")
    assert next(t for t in hub.overview(root)["tasks"] if t["id"] == "t-023")["number"] == 23


def test_reopened_task_has_no_current_completion_until_done_again(record):
    root, conn = record
    task(conn)
    first = event(conn, "task-move", "t-023", "2026-09-20T10:00:00Z", to="done")
    event(conn, "task-move", "t-023", "2026-09-21T10:00:00Z", session="reopening-session", to="open")
    task(conn, status="open")
    item = hub.overview(root)["tasks"][0]
    assert item["completed_at"] is None and item["completed_session"] is None
    assert item["last_completion"]["event_id"] == first["id"]
    assert [entry["to"] for entry in item["history"]] == ["done", "open"]
    event(conn, "task-move", "t-023", "2026-09-22T10:00:00Z", session="second-finish", to="done")
    task(conn)
    item = hub.overview(root)["tasks"][0]
    assert item["completed_at"] == "2026-09-22T10:00:00Z"
    assert item["completed_session"] == "second-finish"


def test_missing_completion_evidence_is_explicit_and_overview_is_read_only(record):
    root, conn = record
    task(conn)
    before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    item = hub.overview(root)["tasks"][0]
    assert all(item[field] is None for field in ("created_at", "updated_at", "completed_at", "completed_session", "completed_actor", "completion_event_id", "report"))
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
    assert not (root / ".alpaca/flow").exists()


def test_linked_proof_has_bounded_sections_and_historical_seal_only(record):
    root, conn = record
    rel = ".alpaca/proofs/op-001/t-023.md"
    path = put(root, rel, "# Measurement report\n\n## What I did\nMeasured actual results.\n\n## How I did it\nRan the recorded checker.\n\n## Result\n" + "Observed values. " * 500 + "\n\n## Evidence\nprivate-attachment-marker\n")
    task(conn, proof="local:" + rel)
    event(conn, "proof-report", "t-023", "2026-09-21T09:00:00Z", session="sealing-session", path=rel, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    item = hub.overview(root)["tasks"][0]
    report = item["report"]
    assert report["path"] == rel
    assert report["what"] == "Measured actual results."
    assert report["how"] == "Ran the recorded checker."
    assert report["result"].startswith("Observed values.")
    assert sum(len(report[key] or "") for key in ("what", "how", "result")) <= 4000
    assert report["truncated"] is True
    assert "private-attachment-marker" not in json.dumps(report)
    assert report["sealed"] is True and report["sealed_session"] == "sealing-session"
    assert report["verification"] == "not_checked" and report["verified_at"] is None
    path.write_text(path.read_text().replace("Measured actual", "Updated actual"))
    assert hub.overview(root)["tasks"][0]["report"]["verification"] == "not_checked"


@pytest.mark.parametrize("rel", [".alpaca/private.md", ".alpaca/proofs/t-023.evidence/copy.md", "docs/escape.md", "../outside.md", ".git/private.md"])
def test_proof_summary_rejects_private_and_escaping_paths(record, tmp_path, rel):
    root, conn = record
    if rel == "docs/escape.md":
        external = put(tmp_path, "outside.md", "## What I did\nsecret-marker\n")
        (root / "docs").mkdir()
        (root / rel).symlink_to(external)
    else:
        put(root, rel, "## What I did\nsecret-marker\n")
    task(conn, proof="local:" + rel)
    item = hub.overview(root)["tasks"][0]
    assert item["report"] is None
    assert "secret-marker" not in json.dumps(item)


def test_report_headings_in_code_or_comments_do_not_invent_missing_sections(record):
    root, conn = record
    rel = "custom/task-report.md"
    put(root, rel, "# Report\n<!--\n## How I did it\nHidden method\n-->\n```markdown\n## Result\nSample result\n```\n## What I did\nRecorded work.\n")
    task(conn, proof="local:" + rel)
    report = hub.overview(root)["tasks"][0]["report"]
    assert report["what"] == "Recorded work."
    assert report["how"] is None and report["result"] is None
    assert report["sealed"] is False
    assert hub.document(root, report["path"])["text"].startswith("# Report")


def test_report_method_preserves_recorded_commands_without_treating_code_as_headings(record):
    root, conn = record
    rel = "custom/task-report.md"
    put(root, rel, "## How I did it\nRan the recorded check:\n```sh\npython -m pytest\n## Result\n```\n## Result\nAll checks passed.\n")
    task(conn, proof="local:" + rel)
    report = hub.overview(root)["tasks"][0]["report"]
    assert "python -m pytest" in report["how"]
    assert report["result"] == "All checks passed."


def test_internal_symlink_to_private_report_is_not_read(record):
    root, conn = record
    path = put(root, ".alpaca/session/private.md", "## What I did\nprivate-marker\n")
    (root / "docs").mkdir()
    (root / "docs/report.md").symlink_to(path)
    task(conn, proof="local:docs/report.md")
    assert hub.overview(root)["tasks"][0]["report"] is None


def test_done_state_does_not_reuse_completion_before_a_later_claim(record):
    root, conn = record
    task(conn)
    event(conn, "task-move", "t-023", "2026-09-20T10:00:00Z", to="done")
    event(conn, "claim", "t-023", "2026-09-21T10:00:00Z", session="reopened-worker", worker="other")
    item = hub.overview(root)["tasks"][0]
    assert item["completed_at"] is None
    assert item["last_completion"]["at"] == "2026-09-20T10:00:00Z"


def test_acceptance_completion_binds_recorded_verdict_and_stale_checks(record):
    """The provenance a profile's acceptance hook attaches (work_record.acceptance_provenance):
    a recorded PASS observation bound to its verdict row is a completion while the current check
    passes, and only a recorded completion once the check goes stale."""
    from alpaca import work_record
    root, conn = record
    rid, row_id = "a" * 32, "discharge.ac-09-abcd"
    observation = {"row_id": row_id, "item": "AC-09", "stage": "sim", "step": "discharge",
                   "verdict": "PASS", "reason": "Three tests passed", "receipt_id": rid}
    done = event(conn, "verdict", row_id, "2026-09-20T10:00:00Z", session="acceptance-session",
                 binds={"row_id": row_id}, verdict_name="PASS", reason="Three tests passed")
    verdicts = work_record.verdict_index(conn)
    item = work_record.acceptance_provenance("AC-09", observation, "PASS", verdicts)
    assert item["number"] == 9 and item["ref"] == row_id
    assert item["completed_at"] == "2026-09-20T10:00:00Z"
    assert item["completed_session"] == "acceptance-session"
    assert item["completion_event_id"] == done["id"]
    item = work_record.acceptance_provenance("AC-09", observation, "BLOCKED", verdicts)
    assert item["completed_at"] is None and item["completed_session"] is None
    assert item["recorded_completed_at"] == "2026-09-20T10:00:00Z"
    assert item["recorded_completed_session"] == "acceptance-session"
    changed = dict(observation, reason="another reason")
    assert work_record.acceptance_provenance("AC-09", changed, "PASS", verdicts)["completed_at"] is None


def test_generated_checklist_shares_numbered_work_provenance_and_report_sections(record):
    root, conn = record
    rel = ".alpaca/proofs/op-001/t-023.md"
    put(root, rel, "# Report\n## What I did\nMeasured results.\n## How I did it\nUsed checker\noutput.\n## Result\nThree tests passed.\n")
    task(conn, proof="local:" + rel)
    event(conn, "task-move", "t-023", "2026-09-21T10:00:00Z", to="done")
    output = "\n".join(hub_checklist.render(conn, []))
    assert "23. t-023" in output
    assert "2026-09-21T10:00:00Z" in output and "finishing-session" in output
    assert "Measured results." in output and "Used checker output." in output and "Three tests passed." in output
    assert "[Work report and proof](.alpaca/proofs/op-001/t-023.md)" in output
    assert "checker\\noutput" not in output


def test_stage_provenance_reports_receipt_time_and_session_without_guessing(record, monkeypatch):
    root, conn = record
    rid = "b" * 32
    monkeypatch.setattr(hub, "_checked", lambda root: ([
        {"stage": "sim", "verdict": "BLOCKED", "reason": "Inputs changed", "receipt_id": rid,
         "recorded_status": "PASS", "recorded_at": 1700.25, "recorded_session": "flow-worker"},
        {"stage": "lint", "verdict": "BLOCKED"}], None, "now"))
    stages = hub.overview(root)["stages"]
    assert stages[0]["recorded_at"] == 1700.25 and stages[0]["recorded_session"] == "flow-worker"
    assert stages[0]["status"] == "BLOCKED" and stages[0]["recorded_status"] == "PASS"
    assert stages[0]["receipt_id"] == rid
    assert stages[1]["recorded_at"] is None and stages[1]["recorded_session"] is None


def test_checklist_passing_outcome_includes_recorded_method_and_actual_session(record):
    root, conn = record
    cards = [{"row_id": step + ".ac-09-abcd", "column": "done", "reason": None,
              "proof": "local:flow/spec/adder8-runbook-acceptance.md:20",
              "verdict": {"ts": "2026-09-22T01:00:00Z", "session": "flow-session", "reason": reason}}
             for step, reason in [("state", "one stated claim of 5 words: Simulation passes all three tests"),
                                  ("oracle", "oracle class 'dynamic' is in project.yaml oracle_classes"),
                                  ("proof-kind", "proof kind 'test' is one of test, run, review"),
                                  ("discharge", "observed: 3 of 3 tests passed"),
                                  ("no-drift", "receipt fingerprint matches current inputs")]]
    output = "\n".join(hub_checklist.render(conn, cards))
    assert "9. AC-09: Simulation passes all three tests" in output
    assert "Completed at (recorded): 2026-09-22T01:00:00Z" in output
    assert "Completion session (recorded): flow-session" in output
    assert "dynamic" in output and "proof kind 'test'" in output
    assert "observed: 3 of 3 tests passed" in output


def test_assigned_session_comes_from_matching_claim_worker_and_not_worker_name(record):
    root, conn = record
    task(conn, status="doing", claimant="timing-worker", lease_until="2099-01-01T00:00:00Z")
    event(conn, "claim", "t-023", "2026-09-22T01:00:00Z", session="session-actual", worker="timing-worker")
    item = hub.overview(root)["tasks"][0]
    assert item["claimant"] == "timing-worker"
    assert item["assigned_session"] == "session-actual"
    output = "\n".join(hub_checklist.render(conn, []))
    assert "Assigned worker: timing-worker" in output
    assert "Assigned session: session-actual" in output
    assert "Assigned session: timing-worker" not in output


@pytest.mark.parametrize("claim_worker", [None, "different-worker"])
def test_assigned_session_is_unknown_when_claim_worker_is_missing_or_mismatched(record, claim_worker):
    root, conn = record
    task(conn, status="doing", claimant="timing-worker")
    event(conn, "claim", "t-023", "2026-09-22T01:00:00Z", session="different-session", worker=claim_worker)
    assert hub.overview(root)["tasks"][0]["assigned_session"] is None


def test_assigned_session_is_unknown_without_a_claim_event(record):
    root, conn = record
    task(conn, status="doing", claimant="timing-worker")
    assert hub.overview(root)["tasks"][0]["assigned_session"] is None


@pytest.mark.parametrize("kind,data", [("claim-release", {}), ("lease-expired", {}),
                                      ("task-move", {"to": "open"}), ("task-move", {"to": "done"})])
def test_assigned_session_does_not_reuse_released_expired_or_reopened_claim(record, kind, data):
    root, conn = record
    task(conn, status="doing", claimant="timing-worker", lease_until="2099-01-01T00:00:00Z")
    event(conn, "claim", "t-023", "2026-09-20T01:00:00Z", session="old-session", worker="timing-worker")
    event(conn, kind, "t-023", "2026-09-21T01:00:00Z", **data)
    event(conn, "task-move", "t-023", "2026-09-22T01:00:00Z", to="doing")
    assert hub.overview(root)["tasks"][0]["assigned_session"] is None


def test_assigned_session_uses_latest_claim_and_current_renewed_lease(record):
    root, conn = record
    task(conn, status="doing", claimant="timing-worker", lease_until="2099-01-01T00:00:00Z")
    event(conn, "claim", "t-023", "2026-09-20T01:00:00Z", session="old-session", worker="timing-worker")
    event(conn, "claim-release", "t-023", "2026-09-21T01:00:00Z")
    event(conn, "claim", "t-023", "2026-09-22T01:00:00Z", session="new-session", worker="timing-worker", lease_until="2000-01-01T00:00:00Z")
    assert hub.overview(root)["tasks"][0]["assigned_session"] == "new-session"
    task(conn, status="doing", claimant="timing-worker", lease_until="2000-01-01T00:00:00Z")
    assert hub.overview(root)["tasks"][0]["assigned_session"] is None


def test_currently_open_task_does_not_advertise_stale_assigned_session(record):
    root, conn = record
    task(conn, status="open", claimant="timing-worker", lease_until="2099-01-01T00:00:00Z")
    event(conn, "claim", "t-023", "2026-09-20T01:00:00Z", session="old-session", worker="timing-worker")
    assert hub.overview(root)["tasks"][0]["assigned_session"] is None


def test_checklist_marks_retained_report_on_reopened_task_as_historical(record):
    root, conn = record
    rel = ".alpaca/proofs/op-001/t-023.md"
    put(root, rel, "## What I did\nMeasured results.\n## Result\nThree tests passed.\n")
    task(conn, status="open", claimant=None, proof="local:" + rel)
    event(conn, "task-move", "t-023", "2026-09-20T01:00:00Z", to="done")
    event(conn, "task-move", "t-023", "2026-09-21T01:00:00Z", to="open")
    output = "\n".join(hub_checklist.render(conn, []))
    assert "Historical report" in output
    assert "earlier completion" in output
    assert "Completed at: Not recorded" in output
    assert output.index("Historical report") < output.index("Measured results.")


def test_short_titles_keep_the_full_statement_as_description(record, monkeypatch):
    root, conn = record
    task(conn, title="Record results")
    task(conn, "t-024")
    items = {t["id"]: t for t in hub.overview(root)["tasks"]}
    assert items["t-023"]["title"] == "Record results"
    assert items["t-023"]["description"] == "Document measured results"
    assert items["t-024"]["title"] == "Document measured results" and items["t-024"]["description"] is None
    # the short display names a profile's acceptance hook reads: absent reads as {}, blank
    # names are dropped
    assert hub._checklist_titles(root) == {}
    put(root, hub.CHECKLIST_TITLES, json.dumps({"AC-09": "simulation passes", "AC-10": "  "}))
    assert hub._checklist_titles(root) == {"AC-09": "simulation passes"}
