"""M2.17 proof - the projection freshness gate and the store-versus-projection ruling.

The record is the only truth. RESUME.md, CHECKLIST.md, board.json and data.json are
projections: rendered from `.alpaca/alpaca.db` and never hand-edited. This module proves both paths
of the Done-when:

  * POSITIVE: a set of projections rendered from the record right now passes the gate with no
    findings, and the freshness link inside a phase door is PASS.
  * NEGATIVE: a hand-edited byte on any of the four projections, or a projection left stale
    after the record moved on, is a finding that NAMES the file and the differing line, folds
    the gate to a non-PASS verdict, and CLOSES the phase door over that boundary.

It also proves the volatile declaration (the generation stamp) is honored in ONE place -
excluded from both the diff and the content hash - and that the ruling document states the
store is truth and a correction targets the assertion rather than the rendered page.
"""
import json
import os

import pytest

from alpaca import board, clock, db, decisions, export, freshness, pad, profile, util
from alpaca.gates import verdict as vc
from alpaca.phase import doors

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# --------------------------------------------------------------- fixtures / helpers
def _row(conn, rid, *, op, phase, statement="an obligation to fold on the board here"):
    db.upsert(conn, "rows", "id", {
        "id": rid, "kind": "item", "op": op, "phase": phase, "step": "s1",
        "statement": statement, "proof": "local:spec.md", "where_": "", "how": "",
        "when_": "", "why": "", "session": None, "operator": None, "status": "open",
        "tag": "Specced", "content_hash": util.sha256_hex("row/" + rid),
        "prev_hash": None, "supersedes": None, "superseded_by": None})


@pytest.fixture
def proj(project):
    """An onboarded project with an op, a task and two obligation rows, under a fixed clock so
    every render is byte-stable and the only wall-clock line is the generation stamp."""
    from alpaca import cli
    util.set_clock(clock.FixedClock("2026-02-01T00:00:00+00:00", step=0))
    cli.main(["init"])
    conn = db.connect(project)
    db.meta_set(conn, "onboarded", util.now_iso())
    db.upsert(conn, "ops", "id", {
        "id": "op-001", "intent": "ship the board", "done_when": "board green",
        "status": "open", "opened": util.now_iso(), "closed": None, "phases": None})
    db.upsert(conn, "tasks", "id", {
        "id": "t-001", "op": "op-001", "phase": "build", "statement": "do the thing well now",
        "status": "open", "proof": None, "where_": "", "why": "", "claimant": None,
        "lease_until": None, "created": util.now_iso(), "updated": util.now_iso()})
    _row(conn, "r-1", op="op-001", phase="build")
    _row(conn, "r-2", op="op-001", phase="verify")
    yield project, conn
    conn.close()
    util.set_clock(None)


def _materialize(root):
    """Render all four projections to disk from the record, exactly as the writers do."""
    pad.write(root)              # RESUME.md and board.json
    pad.write_checklist(root)    # CHECKLIST.md
    export.write(root)           # data.json


def _line_of(text, needle):
    for i, ln in enumerate(text.split("\n"), start=1):
        if needle in ln:
            return i, ln
    raise AssertionError("no line carrying %r" % needle)


# ------------------------------------------------------------------- POSITIVE path
def test_all_four_projections_materialize_and_pass_fresh(proj):
    root, conn = proj
    _materialize(root)
    for name in ("RESUME.md", "CHECKLIST.md", "board.json", "data.json"):
        assert os.path.isfile(os.path.join(root, name)), name
    findings = freshness.check(root)
    assert findings == [], findings
    assert freshness.verdict_of(findings) == vc.PASS


def test_absent_projection_is_never_a_finding(proj):
    root, conn = proj
    # nothing rendered to disk: a projection that does not exist cannot be hand-edited or stale.
    findings = freshness.check(root)
    assert findings == []
    assert freshness.verdict_of(findings) == vc.PASS


# ------------------------------------------------------------------- NEGATIVE path
def test_hand_edited_board_json_fails_and_names_file_and_line(proj):
    root, conn = proj
    _materialize(root)
    p = os.path.join(root, "board.json")
    disk = util.read_text(p)
    lineno, ln = _line_of(disk, '"row_id": "r-1"')
    edited = disk.replace(ln, ln.replace("r-1", "r-9"), 1)
    util.write_text(p, edited)

    findings = freshness.check(root)
    hits = [f for f in findings if f["file"] == "board.json"]
    assert hits, "a hand-edited board.json is a finding"
    assert hits[0]["line"] == lineno, "the finding names the differing line"
    assert freshness.TOKEN == hits[0]["token"]
    assert freshness.verdict_of(findings) != vc.PASS


def test_stale_resume_fails_after_the_record_moves(proj):
    root, conn = proj
    _materialize(root)
    # the record moves on: a new open task the rendered RESUME.md never saw.
    db.upsert(conn, "tasks", "id", {
        "id": "t-777", "op": "op-001", "phase": "build", "statement": "a brand new open task",
        "status": "open", "proof": None, "where_": "", "why": "", "claimant": None,
        "lease_until": None, "created": util.now_iso(), "updated": util.now_iso()})
    findings = freshness.check(root)
    hits = [f for f in findings if f["file"] == "RESUME.md"]
    assert hits, "a RESUME.md left behind by the record is stale"
    assert hits[0]["line"] >= 1
    assert freshness.verdict_of(findings) != vc.PASS


def test_stale_checklist_and_data_are_each_caught(proj):
    root, conn = proj
    _materialize(root)
    # a new obligation row moves the board, the checklist and data.json together.
    _row(conn, "r-3", op="op-001", phase="build", statement="a third obligation appears now")
    findings = freshness.check(root)
    named = {f["file"] for f in findings}
    assert "CHECKLIST.md" in named
    assert "data.json" in named
    assert "board.json" in named


# ---------------------------------------------------- the single volatile declaration
def test_generation_stamp_is_volatile_excluded_from_diff_and_hash(proj):
    root, conn = proj
    _materialize(root)
    p = os.path.join(root, "board.json")
    disk = util.read_text(p)
    lineno, ln = _line_of(disk, '"generated"')
    bumped = disk.replace(ln, ln.replace("2026-02-01", "2027-09-09"), 1)
    util.write_text(p, bumped)

    # the stamp differs on disk, yet the gate stays green: the line is declared volatile.
    findings = freshness.check(root)
    assert [f for f in findings if f["file"] == "board.json"] == []

    # and the volatile line is excluded from the content hash: only the stamp changed, so the
    # neutralized hash is identical; a non-volatile edit DOES move the hash.
    assert freshness.content_hash(disk) == freshness.content_hash(bumped)
    other = disk.replace("r-1", "r-9", 1)
    assert freshness.content_hash(disk) != freshness.content_hash(other)


def test_volatile_lines_are_declared_in_one_place(proj):
    root, conn = proj
    _materialize(root)
    p = os.path.join(root, "board.json")
    disk = util.read_text(p)
    lineno, ln = _line_of(disk, '"generated"')
    bumped = disk.replace(ln, ln.replace("2026-02-01", "2027-09-09"), 1)
    util.write_text(p, bumped)

    # with the declared set (the default) the stamp change is excluded.
    assert freshness.check(root, volatile_lines=freshness.VOLATILE_LINES) == []
    # remove the declaration and the very same stamp change is now a finding: the one place
    # that declares the volatile lines is what excludes them.
    with_none = freshness.check(root, volatile_lines=())
    assert [f for f in with_none if f["file"] == "board.json"], \
        "without the declared volatile set the stamp line is diffed like any other"


# ------------------------------------------------------------------- door wiring
def test_projection_freshness_is_a_link_in_the_phase_door(proj):
    root, conn = proj
    _materialize(root)
    lk = dict(doors.links(conn, "op-001", "build->verify", root=root))
    assert "projection-freshness" in lk, "the door composes the freshness link"
    assert lk["projection-freshness"] == vc.PASS, "fresh projections keep the link PASS"


def test_stale_projection_closes_the_phase_door(proj):
    root, conn = proj
    _materialize(root)
    # hand-edit board.json on a content line: the door over any boundary must close.
    p = os.path.join(root, "board.json")
    disk = util.read_text(p)
    _, ln = _line_of(disk, '"row_id": "r-1"')
    util.write_text(p, disk.replace(ln, ln.replace("r-1", "r-9"), 1))

    lk = dict(doors.links(conn, "op-001", "build->verify", root=root))
    assert lk["projection-freshness"] != vc.PASS
    code = doors.run(conn, "op-001", "build->verify", level="L5", root=root,
                     tmp_prefixes=frozenset(), session="s1")
    assert code != vc.PASS, "a door never opens over a stale projection"


# ------------------------------------------------------------------- the ruling
def test_ruling_document_states_the_store_is_truth(proj):
    root, conn = proj
    doc = os.path.join(REPO, "docs", "projection-ruling.md")
    assert os.path.isfile(doc), "the ruling document exists"
    body = util.read_text(doc).lower()
    assert "store is truth" in body or "store is the truth" in body
    assert "assertion" in body, "a correction targets the assertion"
    assert "rendered page" in body or "rendered projection" in body
    # pure hyphen: the ban on the em dash holds in every file this plan authors.
    assert "\u2014" not in util.read_text(doc)


def test_ruling_is_recorded_as_a_resolvable_decision_page(proj):
    root, conn = proj
    rec = freshness.record_ruling(conn, root=root)
    assert rec["kind"] == freshness.RULING_KIND
    got = decisions.resolve(conn, decisions.why_pointer(rec), root=root)
    assert got["choice"] == rec["choice"]
    assert "store" in got["choice"].lower() or "store" in got["context"].lower()


# ------------------------------------------------------------------- data.json shape
def test_data_json_carries_the_seven_top_level_keys(proj):
    root, conn = proj
    import json
    data = json.loads(export.render(root))
    for key in ("board", "messages", "pulse", "gates", "wiki", "runs", "metrics"):
        assert key in data, key


def test_profile_numbers_keep_the_projection_fresh_under_a_moving_clock(proj, monkeypatch):
    """The seventh key is folded from the record alone, so it cannot go stale by itself.

    A projection renders the record and nothing else. `metrics` is the profile's fold of receipt
    rows, so two renders taken at two different wall-clock instants must agree byte for byte
    outside the declared stamp, and the gate over a materialized data.json must stay green while
    only the clock moves.
    """
    root, conn = proj

    class Numbers(profile.Profile):
        def metrics(self, c):
            rows = [json.loads(r[0]) for r in c.execute(
                "SELECT data FROM events WHERE kind='domain-receipt' ORDER BY id")]
            return {"runs": [{"receipt_id": r["receipt_id"], **r["metrics"]} for r in rows]}
    monkeypatch.setattr(profile, "load", lambda r: Numbers())
    db.append_event(conn, session="runner", actor="runner", kind="domain-receipt",
                    op="domain-run", ref="metrics",
                    data={"stage": "metrics", "receipt_id": "a" * 32, "job_id": "b" * 32,
                          "verdict": "PASS", "reason": "metrics acceptance passed",
                          "started_at": 10.0, "finished_at": 22.5,
                          "metrics": {"design": "adder8", "synth_cells": 570,
                                      "design_area_um2": 6569.0, "wns_ns": 0.0}})
    _materialize(root)
    assert freshness.check(root) == []
    assert json.loads(util.read_text(os.path.join(root, "data.json")))["metrics"]["runs"][0]["synth_cells"] == 570
    util.set_clock(clock.FixedClock("2027-12-31T23:59:59+00:00", step=0))
    assert freshness.content_hash(export.render(root)) == \
        freshness.content_hash(util.read_text(os.path.join(root, "data.json")))
    assert [f for f in freshness.check(root) if f["file"] == "data.json"] == []
