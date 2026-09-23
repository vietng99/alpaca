"""M2.18 proof - `alpaca export` writes one seven-key data.json from the record, and `alpaca deploy` refuses.

The record is the only truth; `data.json` is a projection rendered from `.alpaca/alpaca.db` and never
hand-edited. This module proves two of the four Done-when clauses (the other two covered the
static board page, which is retired; the cockpit reads the same payload at `/board/data.json`):

  (1) `alpaca export` writes ONE `data.json` whose top level carries all seven keys
      (`board`, `messages`, `pulse`, `gates`, `wiki`, `runs`, `metrics`) and which parses through
      `json.loads`. The seven sections are folded from the record only: no transcript file is
      needed, and content put on the record (an op's board rows, a posted message) shows up in
      the rendered payload. `metrics` and `pulse.now.stages` come from the domain profile
      (alpaca/profile.py) and are empty without one.

  (4) `alpaca deploy` exits 3 (PAUSED-FOR-DECISION) with a named reason while no human decision row
      for the deploy is on the record. Publishing outside the box is an irreversible external
      action; until the review card lands (M3.5) the refusal is unconditional, and it never
      auto-advances.
"""
import json
import os

import pytest

from alpaca import clock, cli, db, export, profile, util


class Fake(profile.Profile):
    """A stand-in domain profile: each keyword replaces one hook."""

    def __init__(self, **hooks):
        self.__dict__.update(hooks)

KEYS = {"board", "messages", "pulse", "gates", "wiki", "runs", "metrics"}


def _row(conn, rid, *, op, phase, statement="an obligation to fold on the board"):
    db.upsert(conn, "rows", "id", {
        "id": rid, "kind": "item", "op": op, "phase": phase, "step": "s1",
        "statement": statement, "proof": "local:spec.md", "where_": "", "how": "",
        "when_": "", "why": "", "session": None, "operator": None, "status": "open",
        "tag": "Specced", "content_hash": util.sha256_hex("row/" + rid),
        "prev_hash": None, "supersedes": None, "superseded_by": None})


@pytest.fixture
def proj(project):
    """An onboarded project carrying an op, two obligation rows and a message, under a fixed
    clock so every render is byte-stable but for the generation stamp."""
    from alpaca import messages
    util.set_clock(clock.FixedClock("2026-03-01T00:00:00+00:00", step=0))
    cli.main(["init"])
    conn = db.connect(project)
    db.meta_set(conn, "onboarded", util.now_iso())
    db.upsert(conn, "ops", "id", {
        "id": "op-001", "intent": "ship the board", "done_when": "board green",
        "status": "open", "opened": util.now_iso(), "closed": None, "phases": None})
    _row(conn, "r-1", op="op-001", phase="build")
    _row(conn, "r-2", op="op-001", phase="verify")
    messages.post(conn, "orch", "w1", "handoff", "take r-1")
    yield project, conn
    conn.close()
    util.set_clock(None)


# ----------------------------------------------------- Done-when (1): the seven-key payload
def test_render_carries_the_seven_keys_and_parses(proj):
    root, conn = proj
    text = export.render(root)
    d = json.loads(text)                       # parses through json.loads
    assert KEYS <= set(d.keys())               # all seven top-level keys present


def test_alpaca_export_writes_exactly_one_data_json_at_the_root(proj):
    root, conn = proj
    code = cli.main(["export"])
    assert code == 0                           # PASS
    p = os.path.join(root, "data.json")
    assert os.path.isfile(p), "alpaca export writes data.json into the root"
    # exactly ONE data.json anywhere under the root.
    hits = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f == "data.json":
                hits.append(os.path.join(dirpath, f))
    assert hits == [p], hits


def test_written_data_json_parses_and_has_seven_keys(proj):
    root, conn = proj
    cli.main(["export"])
    d = json.loads(util.read_text(os.path.join(root, "data.json")))
    assert KEYS <= set(d.keys())
    assert set(KEYS) == (KEYS & set(d.keys()))


def test_sections_are_folded_from_the_record_only(proj):
    root, conn = proj
    # nothing to do with transcripts: render straight from the DB and see the record's content.
    d = json.loads(export.render(root))
    assert "r-1" in json.dumps(d["board"]), "board folds the op's obligation rows"
    assert "r-2" in json.dumps(d["board"])
    assert "take r-1" in json.dumps(d["messages"]), "messages fold the posted message"
    # pulse/gates/wiki/runs are record-derived shapes, never a store.
    assert isinstance(d["pulse"], dict)
    assert isinstance(d["gates"], list)
    assert isinstance(d["wiki"], dict)
    assert isinstance(d["runs"], list)
    assert d["metrics"] == {}, "no profile, no domain numbers"


def test_render_is_byte_stable_modulo_the_generation_stamp(proj):
    root, conn = proj
    a = export.render(root)
    b = export.render(root)
    # under a fixed clock the two renders are identical; the only volatile line is the stamp.
    assert a == b


# ------------------------------------------------------- Done-when (4): the deploy barrier
def _deploy_decision_rows(conn):
    """Every recorded decision whose kind or body names a deploy/publish. None should exist."""
    out = []
    for r in db.rows(conn, "decisions", "1=1"):
        blob = ("%s %s" % (r.get("kind") or "", r.get("body") or "")).lower()
        if "deploy" in blob or "publish" in blob:
            out.append(r)
    return out


def test_alpaca_deploy_exits_3_paused(proj):
    root, conn = proj
    code = cli.main(["deploy"])
    assert code == 3, "deploy is PAUSED-FOR-DECISION, never auto-advanced"


def test_alpaca_deploy_names_a_reason(proj, capsys):
    root, conn = proj
    cli.main(["deploy"])
    out = capsys.readouterr()
    blob = (out.out + out.err).lower()
    assert "deploy" in blob, "the refusal names the boundary"
    # the reason names WHY it paused: a human decision is required and none is on the record.
    assert ("human" in blob) or ("decision" in blob) or ("review" in blob)


def test_alpaca_deploy_refuses_with_no_decision_row_on_the_record(proj):
    root, conn = proj
    # precondition: no human decision row for the deploy exists.
    assert _deploy_decision_rows(conn) == []
    code = cli.main(["deploy"])
    assert code == 3
    # and alpaca deploy did not conjure one: the refusal is a pause, not a self-granted go.
    conn2 = db.connect(root)
    assert _deploy_decision_rows(conn2) == []
    conn2.close()


def test_metrics_come_from_the_profile_fold_of_the_record(proj, monkeypatch):
    root, conn = proj
    seen = []

    def metrics(c):
        seen.append(c.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        return {"runs": [{"design": "d1", "cells": 570}], "reference": {}, "attempts": []}
    monkeypatch.setattr(profile, "load", lambda r: Fake(metrics=metrics))
    d = json.loads(export.render(root))
    assert d["metrics"]["runs"] == [{"design": "d1", "cells": 570}]
    assert seen and seen[0] > 0, "the fold is handed the record connection"


# ------------------------------------------------- op-006: `pulse.now`, where the project stands
def _worker(conn, sid, level="L5", beats=2, tool="Bash"):
    db.upsert(conn, "sessions", "sid", {"sid": sid, "started": util.now_iso(), "level": level,
                                        "beats": beats})
    db.meta_set(conn, "operator:%s" % sid, "claude")
    for n in range(beats):
        db.append_event(conn, session=sid, actor="agent", kind="heartbeat",
                        data={"tool": tool, "ref": "step %d" % n})


def _probe(conn, sid):
    db.upsert(conn, "sessions", "sid", {"sid": sid, "started": util.now_iso(),
                                        "ended": util.now_iso(), "beats": 0})
    db.append_event(conn, session=sid, actor="agent", kind="session-start")
    db.append_event(conn, session=sid, actor="agent", kind="session-end")


def test_now_rides_inside_pulse_and_leaves_the_top_level_alone(proj):
    root, conn = proj
    d = json.loads(export.render(root))
    assert set(d) - {"generated"} == KEYS, "no eighth top-level key"
    assert "now" in d["pulse"]
    assert set(d["pulse"]["now"]) == {"as_of", "next_action", "level", "ops", "tasks", "stages",
                                      "sessions", "recent", "noise"}


def test_now_carries_the_record_clock_the_level_and_the_pad_line(proj):
    root, conn = proj
    from alpaca import pad
    _worker(conn, "aaaaaaaa-1111")
    now = json.loads(export.render(root))["pulse"]["now"]
    assert now["as_of"] == db.last_event(conn)["ts"], "as_of is the newest event, not the clock"
    assert now["level"] == "L2", "the level in force behind the newest working session"
    assert now["next_action"] == pad.next_action(root, conn=conn, expire=False)


def test_now_reports_ops_tasks_and_which_tasks_are_sealed(proj):
    root, conn = proj
    db.upsert(conn, "tasks", "id", {"id": "t-001", "op": "op-001", "statement": "a first task",
                                    "status": "doing", "claimant": "w1"})
    db.upsert(conn, "tasks", "id", {"id": "t-002", "op": "op-001", "statement": "a second task",
                                    "status": "done", "proof": "local:p.md"})
    db.append_event(conn, session="w", actor="agent", kind="proof-report", ref="t-002",
                    data={"path": ".alpaca/proofs/op-001/t-002.md"})
    now = json.loads(export.render(root))["pulse"]["now"]
    op = [o for o in now["ops"] if o["id"] == "op-001"][0]
    assert set(op) == {"id", "title", "status", "done_when", "tasks_total", "tasks_done",
                       "tasks_doing", "tasks_open", "rows_total", "rows_done", "rows_blocked"}
    assert op["title"] == "ship the board" and op["status"] == "open"
    assert (op["tasks_total"], op["tasks_done"], op["tasks_doing"]) == (2, 1, 1)
    assert (op["rows_total"], op["rows_done"]) == (2, 0), "the two obligation rows fold in"
    ids = [t["id"] for t in now["tasks"]]
    assert ids == ["t-001", "t-002"], "doing before done"
    sealed = {t["id"]: t["sealed"] for t in now["tasks"]}
    assert sealed == {"t-001": False, "t-002": True}
    assert now["tasks"][0]["claimant"] == "w1"


def test_now_splits_the_sessions_and_counts_the_probes_without_listing_them(proj):
    root, conn = proj
    _worker(conn, "aaaaaaaa-1111")
    for n in range(9):
        _probe(conn, "probe-%d" % n)
    db.append_event(conn, session="instrument", actor="instrument", kind="run",
                    data={"gate": "g", "verdict": "PASS"})
    now = json.loads(export.render(root))["pulse"]["now"]
    work = now["sessions"]["work"]
    assert [s["sid"] for s in work] == ["aaaaaaaa-1111"]
    assert set(work[0]) == {"sid", "operator", "started", "ended", "last_beat", "beats", "turns",
                            "level", "active", "last_tool", "last_ref", "claims",
                            "subagent_stops"}
    assert work[0]["active"] is True, "it beat at the record's own newest instant"
    assert now["sessions"]["probes"]["count"] == 9
    service = {s["sid"]: s["events"] for s in now["sessions"]["service"]}
    assert set(service) == {"instrument", "cli"}, "the record's own writer ids, never a sitting"
    assert service["instrument"] == 1
    assert "probe-0" not in json.dumps(now["sessions"]["work"])


def test_now_keeps_at_most_twelve_working_sessions(proj):
    root, conn = proj
    util.set_clock(clock.FixedClock("2026-03-01T00:00:00+00:00", step=60))
    for n in range(20):
        _worker(conn, "session-%02d" % n, beats=1)
    now = json.loads(export.render(root))["pulse"]["now"]
    assert len(now["sessions"]["work"]) == 12
    assert now["sessions"]["work"][0]["sid"] == "session-19", "newest activity first"


def test_now_stages_are_the_profile_stage_rows(proj, monkeypatch):
    root, conn = proj
    assert json.loads(export.render(root))["pulse"]["now"]["stages"] == []
    row = {"stage": "lint", "verdict": "PASS", "reason": "lint acceptance passed",
           "ts": "2026-03-01T00:00:00+00:00", "receipt_id": "r-PASS"}
    monkeypatch.setattr(profile, "load", lambda r: Fake(stage_rows=lambda c: [row, "not a row"]))
    stages = json.loads(export.render(root))["pulse"]["now"]["stages"]
    assert stages == [row]


def test_now_recent_is_filtered_summarised_and_the_noise_is_counted(proj):
    root, conn = proj
    _worker(conn, "aaaaaaaa-1111", beats=6)
    for n in range(3):
        _probe(conn, "probe-%d" % n)
    db.append_event(conn, session="aaaaaaaa-1111", actor="agent", kind="task-move", ref="t-004",
                    data={"from": "doing", "to": "done"})
    now = json.loads(export.render(root))["pulse"]["now"]
    kinds = {e["kind"] for e in now["recent"]}
    assert "heartbeat" not in kinds and "transcript-snapshot" not in kinds
    assert not [e for e in now["recent"] if e["session"].startswith("probe-")]
    move = [e for e in now["recent"] if e["kind"] == "task-move"][0]
    assert move["summary"] == "t-004 doing -> done"
    assert move["session_class"] == "work"
    assert set(move) == {"id", "ts", "session", "session_class", "kind", "ref", "summary"}
    assert now["noise"] == {"heartbeats": 6, "probe_sessions": 3, "snapshots": 0}


def test_rendering_data_json_never_writes_to_the_record(proj):
    """A projection renders the record and never mutates it, so a read cannot expire a lease."""
    root, conn = proj
    db.upsert(conn, "tasks", "id", {"id": "t-001", "op": "op-001", "statement": "a held task",
                                    "status": "doing", "claimant": "w1",
                                    "lease_until": "2020-01-01T00:00:00+00:00"})
    before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    export.render(root)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
