"""t-288 (F40): the run report task's events.json is written once and no later step rewrites
it (design/research-engine/in-session-runtime.md section 20, "Run report export and seal").

Done when (the contract of t-288):

  * a test reproduces the rr-013 order (research-report, the export and the report task's
    seal, research-verify-open, a second research-report, the finalize-time driver audit and
    status events) and shows that the copy the verification lays in tree/ (the kept copy, or
    the file at its path when the seal kept none because of proof.KEPT_FILE_CAP, as rr-013's
    9 MB export) still hashes to the sealed events.json after the fix; it fails before the fix,
    where the export taken again after the seal followed the newest research-report and
    rewrote the file.

Fail case: a fix that stops sealing events.json or drops events from the export the replay
block reads. The tests below assert that the seal still names events.json with its sha256,
that the export holds every run event through the sealed report's through_event, and that the
replay of the sealed export rebuilds the sealed report.md and report.json.

All data is synthetic: the fake runner of test_research_cli opens the run, and the run events
are written with events.emit. No test touches the network or launches an agent runtime.
"""
from __future__ import annotations

import hashlib
import json
import os

import pytest

from alpaca import proof
from alpaca.gates import verdict as vc
from alpaca.research import events, proofs, report, state, verifier
from alpaca.tests.test_research_cli import (_call, _conn, _finalize, _improve_run, _start,  # noqa: F401
                                            rec)

SESSION = "research-test"


def _sha(path) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _report_task(root, run) -> str:
    conn = _conn(root)
    try:
        start = state._events_of(conn, events.RUN_START, run)[0]
        return start["data"]["report_task"]
    finally:
        conn.close()


def _seal_item(root, task, name) -> dict:
    conn = _conn(root)
    try:
        seal = proof.latest_seal(conn, task)
    finally:
        conn.close()
    for item in seal["data"]["evidence"]:
        if str(item.get("pointer", "")).endswith("/" + name):
            return item
    raise AssertionError("the seal names no %s" % name)


def _verification_copy(root, item) -> str:
    """The file the verification copies into tree/ for a sealed report file: the seal's kept
    copy, or the file at its own path when the seal kept none for the size cap (t-289)."""
    if item.get("kept"):
        return os.path.join(root, item["kept"])
    assert item.get("kept_reason") == proof.OVER_CAP
    return os.path.join(root, item["pointer"].split(":", 1)[1])


def _close_report(root, run, task):
    """What runner.close_report does for a finished run, up to the verification: report.write
    (the finalize-time research-report, rr-013 event 101164), the export, the report task's
    seal (101165)."""
    conn = _conn(root)
    try:
        first = report.write(conn, root, run)
        exported = proofs.export_report_events(conn, root, run)
        sealed = proofs.seal(conn, root, task, session=SESSION)
    finally:
        conn.close()
    return first, exported, sealed


def _after_the_seal(root, capsys, run, task):
    """The rr-013 events after the seal: research-verify-open (101166), a second
    research-report from the report verb (101174), the driver audit (101170) and the status
    (101171, 101181)."""
    conn = _conn(root)
    try:
        events.emit(conn, events.VERIFY_OPEN, run=run, ref=task, op="op-001", session=SESSION,
                    data={"verification": "vf-%s-1" % task, "task": task, "role": "report"})
    finally:
        conn.close()
    _code, body = _call(capsys, "report", run)           # the verb's exit follows the fold
    assert body["report"]["new"] is True, body           # the report was rebuilt in place
    conn = _conn(root)
    try:
        events.emit(conn, events.DRIVER_AUDIT, run=run, ref=run, op="op-001", session=SESSION,
                    data={"findings": [], "beyond": {"count": 0}})
        events.emit(conn, events.STATUS, run=run, ref=run, op="op-001", session=SESSION,
                    code=vc.PASS, data={"state": "finished", "round": 1, "holds": []})
    finally:
        conn.close()
    return body


@pytest.mark.parametrize("over_cap", [False, True], ids=["kept-copy", "over-cap-like-rr-013"])
def test_the_rr013_order_leaves_the_sealed_events_json_as_sealed(rec, capsys, monkeypatch,
                                                                  over_cap):
    root = rec.root
    run = _improve_run(root, capsys, champion="b" * 40, control="noop")
    task = _report_task(root, run)
    if over_cap:
        # rr-013's export was 9383303 bytes, over the 8 MiB KEPT_FILE_CAP, so its seal kept no
        # copy (proof-report 101165: kept null, kept_reason "over cap"); a small cap gives the
        # synthetic export the same seal shape
        monkeypatch.setattr(proof, "KEPT_FILE_CAP", 4096)
    first, exported, sealed = _close_report(root, run, task)
    assert exported.get("frozen", False) is False and exported["rows"] > 0
    item = _seal_item(root, task, "events.json")
    assert item["sha256"] == exported["sha256"] and item["verified"] is True
    assert (item["kept"] is None) is over_cap
    second = _after_the_seal(root, capsys, run, task)
    assert second["report"]["through_event"] > first["through_event"]

    # the later step: the export taken again after the seal, once newer run events exist
    conn = _conn(root)
    try:
        again = proofs.export_report_events(conn, root, run)
    finally:
        conn.close()
    # the copy the verification lays in tree/ still hashes to the sealed events.json
    path = os.path.join(root, ".alpaca/reports/research/%s/events.json" % run)
    assert _sha(path) == item["sha256"]
    assert _sha(_verification_copy(root, item)) == item["sha256"]
    assert again["sha256"] == item["sha256"]
    assert again.get("frozen") is True and again.get("matches") is True
    assert again.get("seal_event") == sealed["event"]
    conn = _conn(root)
    try:
        detail = proof.verify_detail(conn, root, task)
    finally:
        conn.close()
    assert detail["ok"] is True, detail
    assert not [n for n in detail["notes"] if "events.json" in n]
    if not over_cap:
        tree = os.path.join(root, "tree")
        conn = _conn(root)
        try:
            verifier._report_files(conn, root, task, tree)
        finally:
            conn.close()
        laid = os.path.join(tree, ".alpaca/reports/research/%s/events.json" % run)
        assert _sha(laid) == item["sha256"]


def test_the_sealed_export_holds_the_run_events_and_replays_the_sealed_report(rec, capsys):
    """Fail case: the seal still names events.json, the export keeps every run event through
    the sealed report's through_event, and its replay rebuilds the sealed report files, after
    the report was rebuilt in place by a second research-report."""
    root = rec.root
    run = _improve_run(root, capsys, champion="b" * 40, control="noop")
    task = _report_task(root, run)
    first, _exported, _sealed = _close_report(root, run, task)
    _after_the_seal(root, capsys, run, task)
    conn = _conn(root)
    try:
        proofs.export_report_events(conn, root, run)
        tasks = {a["ref"] for a in state._run_tasks(conn, run)}
        want = [ev["id"] for ev in state._run_events(conn, run, tasks)
                if ev["id"] <= first["through_event"]]
    finally:
        conn.close()
    path = os.path.join(root, ".alpaca/reports/research/%s/events.json" % run)
    body = json.load(open(path, encoding="utf-8"))
    assert body["through_event"] == first["through_event"]
    assert [row["id"] for row in body["rows"]] == want and want
    md, js = _seal_item(root, task, "report.md"), _seal_item(root, task, "report.json")
    assert body["report_md_sha256"] == md["sha256"]
    assert body["report_json_sha256"] == js["sha256"]
    # the live report.md was rebuilt after the seal; the sealed one is the kept copy
    assert _sha(os.path.join(root, md["pointer"].split(":", 1)[1])) != md["sha256"]
    got = proofs.replay_report(path, run, os.path.join(root, "replay"))
    assert got["match"] is True
    assert got["md_sha256"] == md["sha256"] and got["json_sha256"] == js["sha256"]


def test_a_changed_export_after_the_seal_is_not_rewritten_to_look_sealed(rec, capsys,
                                                                         monkeypatch):
    """Negative: an events.json changed after the seal stays changed (the export writes
    nothing), so the verification refuses it rather than reading a fresh export."""
    root = rec.root
    run = _improve_run(root, capsys, champion="b" * 40, control="noop")
    task = _report_task(root, run)
    monkeypatch.setattr(proof, "KEPT_FILE_CAP", 4096)
    _close_report(root, run, task)
    item = _seal_item(root, task, "events.json")
    path = os.path.join(root, ".alpaca/reports/research/%s/events.json" % run)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(" ")
    changed = _sha(path)
    _after_the_seal(root, capsys, run, task)
    conn = _conn(root)
    try:
        again = proofs.export_report_events(conn, root, run)
        detail = proof.verify_detail(conn, root, task)
    finally:
        conn.close()
    assert _sha(path) == changed
    assert again["sha256"] == changed != item["sha256"]
    assert again.get("frozen") is True and again.get("matches") is False
    assert detail["ok"] is False
    assert any("events.json" in p for p in detail["problems"])


def test_before_the_seal_the_export_follows_the_newest_report(rec, capsys):
    """The rule's other side: until the report task's seal names it, the export is the run
    proof's working file and follows the newest research-report, so a resumed close_report
    seals an export of the report it seals."""
    root = rec.root
    run = _improve_run(root, capsys, champion="b" * 40, control="noop")
    conn = _conn(root)
    try:
        first = report.write(conn, root, run)
        one = proofs.export_report_events(conn, root, run)
        events.emit(conn, events.DRIVER_AUDIT, run=run, ref=run, op="op-001", session=SESSION,
                    data={"findings": [], "beyond": {"count": 0}})
        second = report.write(conn, root, run)
        two = proofs.export_report_events(conn, root, run)
    finally:
        conn.close()
    assert second["through_event"] > first["through_event"]
    assert one.get("frozen", False) is False and two.get("frozen", False) is False
    assert two["sha256"] != one["sha256"]
    body = json.load(open(two["path"], encoding="utf-8"))
    assert body["through_event"] == second["through_event"]
    assert body["report_md_sha256"] == second["sha256"]


def test_a_second_close_after_the_seal_leaves_a_coherent_seal(rec, capsys):
    """Check s1, case C: after the seal, later run events and a second research-report, a
    second close (report.write, the export, proofs.seal, as a resumed close_report runs them)
    must not pair the rebuilt report.md with the frozen export of the first report. The seal
    step leaves the frozen seal as it is, so the newest seal's events.json replays to the
    newest seal's report.md."""
    root = rec.root
    run = _improve_run(root, capsys, champion="b" * 40, control="noop")
    task = _report_task(root, run)
    first, _exported, sealed = _close_report(root, run, task)
    _after_the_seal(root, capsys, run, task)
    second, again, resealed = _close_report(root, run, task)
    assert second["through_event"] > first["through_event"]
    assert again.get("frozen") is True and again.get("matches") is True
    assert again.get("report_event") == first["event"]
    assert resealed.get("skipped") is True and resealed.get("frozen") is True
    assert resealed["event"] == sealed["event"]
    md, ev = _seal_item(root, task, "report.md"), _seal_item(root, task, "events.json")
    path = os.path.join(root, ".alpaca/reports/research/%s/events.json" % run)
    assert _sha(path) == ev["sha256"]
    got = proofs.replay_report(path, run, os.path.join(root, "replay-c"))
    assert got["md_sha256"] == md["sha256"]


def test_a_forged_export_sealed_early_does_not_freeze_the_export(rec, capsys):
    """Check s1, case D: an events.json placed before the run's first close and a seal of the
    report task that names it, made before report.write, freeze nothing. The sealed file is not
    the record's export for any research-report before that seal, so the close writes and seals
    the record's export: the newest report's through_event, the finalize row among the rows."""
    root = rec.root
    run = _improve_run(root, capsys, champion="b" * 40, control="noop")
    task = _report_task(root, run)
    path = os.path.join(root, ".alpaca/reports/research/%s/events.json" % run)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    forged = {"run": run, "through_event": 1, "report_md_sha256": "0" * 64,
              "report_json_sha256": "0" * 64, "rows": []}
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(forged, indent=1, sort_keys=True) + "\n")
    conn = _conn(root)
    try:
        early = proofs.seal(conn, root, task, session=SESSION)
    finally:
        conn.close()
    assert early.get("event") and early.get("skipped") is False
    assert _seal_item(root, task, "events.json")["sha256"] == _sha(path)
    rep, exported, sealed = _close_report(root, run, task)
    assert exported.get("frozen") is False and exported["rows"] > 0
    assert sealed.get("skipped") is False and sealed["event"] != early["event"]
    body = json.load(open(path, encoding="utf-8"))
    assert body["through_event"] == rep["through_event"]
    conn = _conn(root)
    try:
        fin = [e["id"] for e in state._events_of(conn, events.FINALIZE, run)]
    finally:
        conn.close()
    assert fin and fin[-1] in [row["id"] for row in body["rows"]]
    assert _seal_item(root, task, "events.json")["sha256"] == _sha(path) == exported["sha256"]


def test_a_mid_run_export_sealed_before_the_finalize_does_not_freeze_the_export(rec, capsys):
    """Check s2, case S2-5: the genuine export of a mid-run research-report (the report verb's,
    taken before the finalize) and a seal of the report task that names it, both made before
    the finalize, freeze nothing. Only a report that covers the run's finished finalize makes
    the run proof, so the close writes and seals the export through the finalize row."""
    root = rec.root
    run = _start(capsys, "--control", "noop")
    task = _report_task(root, run)
    conn = _conn(root)
    try:
        events.emit(conn, events.ROUND, run=run, ref=run, op="op-001", session=SESSION,
                    code=vc.PASS, data={"round": 0, "repair_task": None, "winner": None,
                                        "champion_commit": "b" * 40,
                                        "champion_metrics": {"value": {"median": 100}},
                                        "accepted": 0, "targets_met": False})
        mid = report.write(conn, root, run)
        early_export = proofs.export_report_events(conn, root, run)
        early = proofs.seal(conn, root, task, session=SESSION)
    finally:
        conn.close()
    assert early_export.get("frozen") is False
    assert early.get("skipped") is False
    assert _seal_item(root, task, "events.json")["sha256"] == early_export["sha256"]
    _finalize(root, run)                                 # a finished finalize after the seal
    rep, exported, sealed = _close_report(root, run, task)
    assert rep["through_event"] > mid["through_event"]
    assert exported.get("frozen") is False and exported["rows"] > 0
    assert sealed.get("skipped") is False and sealed["event"] != early["event"]
    path = os.path.join(root, ".alpaca/reports/research/%s/events.json" % run)
    body = json.load(open(path, encoding="utf-8"))
    assert body["through_event"] == rep["through_event"]
    conn = _conn(root)
    try:
        fin = [e["id"] for e in state._events_of(conn, events.FINALIZE, run)]
    finally:
        conn.close()
    assert fin and fin[-1] in [row["id"] for row in body["rows"]]
    assert _seal_item(root, task, "events.json")["sha256"] == _sha(path) == exported["sha256"]
    # the close's seal does freeze: a later export writes nothing and names the close's report
    conn = _conn(root)
    try:
        again = proofs.export_report_events(conn, root, run)
    finally:
        conn.close()
    assert again.get("frozen") is True and again.get("matches") is True
    assert again.get("report_event") == rep["event"]
