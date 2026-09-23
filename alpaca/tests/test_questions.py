"""M1.18 -- the questions ledger and the HITL decision gate.

Proves the bank-and-continue autonomy model ported from the earlier harness `gates/autonomy.py`,
now against the Alpaca record (the append-only `events` table) instead of a `questions.jsonl`:

  * bank-and-continue: a banked question NEVER full-halts the run; the independent (runnable)
    rows keep flowing while the one dependent row waits on the banked thread;
  * class routing: a research-class question is routed to the harness (the machine may
    research-and-decide at the full-autodrive level), the owner classes are owner-only and are
    banked even there;
  * the never-full-halt property: a banked owner-only question blocks ONLY the boundary that
    depends on it (the door over its own phase), and the run keeps working on other rows;
  * exit code 3 (PAUSED-FOR-DECISION) from a decision-required gate row;
  * a door that hits an owner-only open question below the owner-question threshold PAUSES.

Every control asserts the POSITIVE and the NEGATIVE path (the guard fires on the bad input AND
passes on the legitimate one), so none is a tautological refuser.
"""
import os
import stat

import pytest

from alpaca import cli, db, questions
from alpaca.checklist import Halt, artifact, step_model, synthesis, verdict_row
from alpaca.gates import verdict as vc
from alpaca.phase import doors

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODELS = os.path.join(REPO, "step-models")
NOTMP = frozenset()

SPEC = """# Sample spec

## Acceptance

| item  | statement                          | oracle class | proof kind |
| ----- | ---------------------------------- | ------------ | ---------- |
| AC-01 | the parser reads a clean table     | unit         | test       |
| AC-02 | residue outside the table is BLOCK | unit         | test       |
"""


def _setup(project):
    cli.main(["init"])
    return db.connect(project)


def _persist(conn, row):
    dbrow = {c: row.get(k) for c, k in (
        ("id", "id"), ("kind", "kind"), ("op", "op"), ("phase", "phase"), ("step", "step"),
        ("statement", "statement"), ("proof", "proof"), ("where_", "where"), ("how", "how"),
        ("when_", "when"), ("why", "why"), ("session", "session"), ("operator", "operator"),
        ("status", "status"), ("tag", "tag"), ("content_hash", "content_hash"),
        ("prev_hash", "prev_hash"), ("supersedes", "supersedes"))}
    db.upsert(conn, "rows", "id", dbrow)
    return row


def _discharged_phase(conn, project, model_name, op="op-001"):
    """Synthesize and discharge every obligation row for one phase, so a door over that phase
    folds to PASS on its defaults link (mirrors tests/test_doors.py)."""
    p = os.path.join(project, "spec.md")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(SPEC)
    art = artifact.parse(p, "item")
    model = step_model.load(os.path.join(MODELS, model_name))
    rows = synthesis.synthesize(model, art, op=op)
    for r in rows:
        _persist(conn, r)
        verdict_row.discharge(conn, r["id"], r["content_hash"], "probe", vc.PASS, [], "L5", "s1")
    return rows


def _script(dirpath, name, exit_code):
    os.makedirs(dirpath, exist_ok=True)
    p = os.path.join(dirpath, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexit %d\n" % exit_code)
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


# ---------------------------------------------------------------- the class table is data
def test_the_class_table_names_the_four_classes_and_their_routes(project):
    conn = _setup(project)
    tbl = questions.class_table(conn)
    # research routes to the harness; the three owner classes route to the owner.
    assert tbl["research"] == questions.ROUTE_HARNESS
    for c in ("owner-only", "flow-break", "fundamental"):
        assert tbl[c] == questions.ROUTE_OWNER
    assert questions.route(conn, "research") == questions.ROUTE_HARNESS
    assert questions.route(conn, "owner-only") == questions.ROUTE_OWNER


def test_an_unknown_class_is_refused_never_silently_accepted(project):
    conn = _setup(project)
    # NEGATIVE: an unrecognised class is fail-closed.
    with pytest.raises(questions.QuestionRefusal) as ex:
        questions.bank(conn, "what colour?", "whatever", phase="build")
    assert ex.value.reason == questions.R_CLASS_UNKNOWN and ex.value.verdict == vc.BLOCKED
    # POSITIVE: a declared class banks clean.
    rec = questions.bank(conn, "which pin?", "research", phase="build")
    assert rec["class"] == "research"


# ---------------------------------------------------------------- bank lands open+undecided
def test_a_bank_always_lands_open_and_undecided(project):
    conn = _setup(project)
    rec = questions.bank(conn, "which vendor?", "research", phase="build")
    assert rec["status"] == "open" and rec["decided_by"] == ""
    # the record round-trips: the folded current state is the banked open question.
    live = questions.open(conn)
    assert any(r["id"] == rec["id"] and r["status"] == "open" for r in live)
    # a decision cannot be laundered in through bank: status/decided_by on input are ignored.
    rec2 = questions.bank(conn, "sneaky", "research", phase="build",
                          status="resolved", decided_by="harness")
    assert rec2["status"] == "open" and rec2["decided_by"] == ""


# ---------------------------------------------------------------- class routing to the harness
def test_research_is_routed_to_the_harness_owner_classes_are_banked(project):
    conn = _setup(project)
    # research + full autodrive: the harness researches-and-decides.
    dec = questions.decide_or_bank(conn, "which lib pin?", "research", 6, phase="build")
    assert dec["status"] == "resolved" and dec["decided_by"] == questions.ROUTE_HARNESS
    # research + lower level: banked (raise more, decide less).
    low = questions.decide_or_bank(conn, "which lib pin?", "research", 2, phase="build")
    assert low["status"] == "open"
    # owner class at the full autodrive level: STILL banked, never self-decided.
    own = questions.decide_or_bank(conn, "ship it?", "owner-only", 6, phase="build")
    assert own["status"] == "open" and not own["decided_by"]


def test_an_owner_class_may_not_be_self_decided_by_the_harness(project):
    conn = _setup(project)
    q = questions.bank(conn, "which strategy?", "owner-only", phase="build")
    # NEGATIVE: the harness self-deciding an owner-only question is refused.
    with pytest.raises(questions.QuestionRefusal) as ex:
        questions.answer(conn, q["id"], "the machine just picks", "harness")
    assert ex.value.reason == questions.R_OWNER_SELF_DECIDED and ex.value.verdict == vc.FAIL
    # POSITIVE: the owner may decide the owner-only question ...
    r = questions.answer(conn, q["id"], "owner picks A", "owner")
    assert r["status"] == "resolved" and r["decided_by"] == "owner"
    # ... and the harness may decide a research question.
    rq = questions.bank(conn, "which pin?", "research", phase="build")
    r2 = questions.answer(conn, rq["id"], "1.2.3", "harness")
    assert r2["status"] == "resolved" and r2["decided_by"] == "harness"
    # a resolved question cannot be resolved again (a correction is a NEW banked question).
    with pytest.raises(questions.QuestionRefusal) as ex2:
        questions.answer(conn, q["id"], "owner picks B", "owner")
    assert ex2.value.reason == questions.R_ALREADY_RESOLVED


# ---------------------------------------------------------------- bank-and-continue partition
def test_bank_and_continue_partitions_runnable_from_waiting(project):
    conn = _setup(project)
    questions.bank(conn, "which vendor?", "research", phase="build", blocks=["W2"])
    runnable, waiting = questions.independent_work(conn, ["W1", "W2"])
    assert runnable == {"W1"} and waiting == {"W2"}
    # POSITIVE: W1 (independent) proceeded while W2 waited -> the run flowed correctly.
    r, w = questions.assert_bank_and_continue(conn, {"W1", "W2"}, {"W1"})
    assert r == {"W1"} and w == {"W2"}
    # NEGATIVE: a banked research-decidable question that full-halts the run is the forbidden
    # pattern -- no independent work advanced.
    with pytest.raises(questions.QuestionRefusal) as ex:
        questions.assert_bank_and_continue(conn, {"W1", "W2"}, set())
    assert ex.value.reason == questions.R_HALTED_DECIDABLE and ex.value.verdict == vc.FAIL


# ---------------------------------------------------------------- never-full-halt: scoping
def test_a_banked_owner_question_blocks_only_the_boundary_that_depends_on_it(project):
    conn = _setup(project)
    # a banked owner-only question raised in the build phase, blocking one build row.
    questions.bank(conn, "which vendor?", "owner-only", phase="build", blocks=["r-build-1"])
    # the run keeps working on other rows: only the blocked row waits.
    runnable, waiting = questions.independent_work(conn, ["r-build-1", "r-build-2", "r-req-1"])
    assert runnable == {"r-build-2", "r-req-1"} and waiting == {"r-build-1"}
    # it is owed in its OWN phase ...
    assert [q["phase"] for q in questions.open_owner_owed(conn, "build")] == ["build"]
    # ... and in NO other phase: it does not pause a boundary that does not depend on it.
    assert questions.open_owner_owed(conn, "requirement") == []
    assert questions.open_owner_owed(conn, "verify") == []


def test_phase_signout_blocks_on_a_dropped_owner_question_then_passes(project):
    conn = _setup(project)
    q = questions.bank(conn, "which vendor?", "flow-break", phase="build", blocks=["W9"])
    # NEGATIVE: a banked owner-owed question silently dropped -> the phase may not sign out.
    with pytest.raises(questions.QuestionRefusal) as ex:
        questions.phase_signout(conn, "build")
    assert ex.value.reason == questions.R_BANKED_DROPPED and ex.value.verdict == vc.BLOCKED
    # POSITIVE: once the owner resolves it, the phase signs out clean.
    questions.answer(conn, q["id"], "owner unblocks the path", "owner")
    assert questions.phase_signout(conn, "build") == vc.PASS


# ---------------------------------------------------------------- the decision-required gate
def test_a_decision_required_gate_row_pauses_others_pass():
    # opt-in per gate row: a decision-required-and-undecided gate row PAUSES.
    assert questions.decision_gate("g", decision_required=True, decided=False) == vc.PAUSED
    # NEGATIVE controls: a decided gate row, or one not marked decision-required, PASSes.
    assert questions.decision_gate("g", decision_required=True, decided=True) == vc.PASS
    assert questions.decision_gate("g", decision_required=False, decided=False) == vc.PASS


def test_alpaca_ask_owner_only_exits_paused_research_passes(project):
    assert cli.main(["init"]) == 0
    # alpaca ask of an owner-only (decision-required) question exits 3 PAUSED-FOR-DECISION.
    assert cli.main(["ask", "which vendor?", "--class", "owner-only", "--phase", "build"]) == vc.PAUSED
    # alpaca ask of a research question is routed to the harness and exits 0 PASS.
    assert cli.main(["ask", "which pin?", "--class", "research", "--phase", "build"]) == vc.PASS
    # the opt-in flag makes a non-owner gate row decision-required too.
    assert cli.main(["ask", "confirm scope?", "--class", "research", "--phase", "build",
                     "--decision-required"]) == vc.PAUSED
    # alpaca answer resolves the banked owner-only question and exits 0.
    conn = db.connect(project)
    owed = questions.open_owner_owed(conn, "build")
    assert owed, "the owner-only question should be owed in the build phase"
    assert cli.main(["answer", owed[0]["id"], "vendor X", "--by", "owner"]) == vc.PASS
    assert questions.open_owner_owed(db.connect(project), "build") == []


# ---------------------------------------------------------------- the door gate
def test_a_door_hitting_an_owner_only_open_question_below_the_threshold_pauses(project):
    conn = _setup(project)
    _discharged_phase(conn, project, "build.json")   # build->verify links all PASS
    # NEGATIVE control: with no owner question, build->verify at L3 auto-advances (PASS).
    code = doors.run(conn, "op-001", "build->verify", level="L3", root=project,
                     tmp_prefixes=NOTMP, session="s1")
    assert code == vc.PASS
    # bank an owner-only question in the build phase.
    questions.bank(conn, "which vendor?", "owner-only", phase="build", blocks=["r-build-1"])
    # below the owner-question threshold (L3 < L4) the same door PAUSES on the open question.
    code = doors.run(conn, "op-001", "build->verify", level="L3", root=project,
                     tmp_prefixes=NOTMP, session="s1")
    assert code == vc.PAUSED
    # POSITIVE: at or above the threshold the door does not pause on it (bank-and-continue).
    code = doors.run(conn, "op-001", "build->verify", level="L4", root=project,
                     tmp_prefixes=NOTMP, session="s1")
    assert code == vc.PASS
    # the door helper is scoped by phase: the build question does not pause a verify boundary.
    assert questions.door_question_code(conn, "build", "L2") == vc.PAUSED
    assert questions.door_question_code(conn, "verify", "L2") == vc.PASS


def test_the_owner_question_pause_never_opens_a_door_past_a_failing_link(project):
    conn = _setup(project)
    _discharged_phase(conn, project, "build.json")
    questions.bank(conn, "which vendor?", "owner-only", phase="build")
    # a failing project contract closes the door; a BLOCKED/FAIL link dominates the pause.
    _script(os.path.join(project, "contracts", "build"), "bad.sh", vc.FAIL)
    code = doors.run(conn, "op-001", "build->verify", level="L3", root=project,
                     tmp_prefixes=NOTMP, session="s1")
    assert code == vc.FAIL
