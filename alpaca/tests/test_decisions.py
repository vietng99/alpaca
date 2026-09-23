"""M2.6 -- decisions table, decision pages, and the `why` resolution.

Proof test for the Done-when (decisions, the row schema `why`, a gate skip or level change as
one decision event, the decision page):

  * every row's `why` resolves to a decision page that EXISTS and carries all four fields
    (context, options, choice, consequence);
  * an unresolvable `why` (absent, empty, or pointing at no page) BLOCKs the design door;
  * an owner decision (intent, done_when, go, ship, level change) carries its verbatim
    journal pointer -- recorded without one it is refused;
  * a gate skip or a level change is ONE decision event (Q15: no separate override ledger);
  * `synthesis.why_shape` refuses a `why` that is prose rather than a decision-page pointer.

Every control asserts the POSITIVE and the NEGATIVE path, so none is a tautological refuser.
"""
import os

import pytest

from alpaca import cli, db, decisions, util
from alpaca.checklist import synthesis
from alpaca.gates import verdict as vc

OPTS = ["ship as-is", "revert the change", "patch forward"]
OWNER_KINDS = ["intent", "done_when", "go", "ship", "level_change"]


def _init(project):
    cli.main(["init"])
    return db.connect(project)


def _persist_row(conn, rid, *, op, phase, why, kind="item"):
    db.upsert(conn, "rows", "id", {
        "id": rid, "kind": kind, "op": op, "phase": phase, "step": "s1",
        "statement": "the row carries a checkable claim here", "proof": "local:x:1",
        "where_": "", "how": "", "when_": "", "why": why, "session": "cli",
        "operator": "agent", "status": "open", "tag": "Specced",
        "content_hash": "h", "prev_hash": "g", "supersedes": None, "superseded_by": None})


# --------------------------------------------------------------- the four page fields
def test_record_writes_a_page_with_the_four_fields(project):
    conn = _init(project)
    dec = decisions.record(conn, "design", "why we chose the additive path", OPTS,
                           "patch forward", "a follow-up row is owed next phase")
    path = decisions.page_path(project, dec["id"])
    assert os.path.isfile(path), "the decision page is written under .alpaca/wiki/decisions/"
    body = util.read_text(path)
    for needle in ("why we chose the additive path", "patch forward",
                   "a follow-up row is owed next phase", "revert the change"):
        assert needle in body
    page = decisions.resolve(conn, "decision:%s" % dec["id"])
    assert page["context"] and page["options"] and page["choice"] and page["consequence"]


def test_record_refuses_a_missing_required_field(project):
    conn = _init(project)
    with pytest.raises(decisions.DecisionRefusal) as ei:
        decisions.record(conn, "design", "ctx", OPTS, "", "consequence")  # empty choice
    assert ei.value.verdict == vc.BLOCKED
    # positive: the same call with a choice records fine
    dec = decisions.record(conn, "design", "ctx", OPTS, "patch forward", "consequence")
    assert dec["choice"] == "patch forward"


# ------------------------------------------------------------------- resolve()
def test_resolve_positive_and_unresolvable(project):
    conn = _init(project)
    dec = decisions.record(conn, "design", "ctx", OPTS, "patch forward", "cons")
    page = decisions.resolve(conn, decisions.why_pointer(dec))
    assert page["choice"] == "patch forward"
    with pytest.raises(decisions.DecisionRefusal) as ei:
        decisions.resolve(conn, "decision:dec-doesnotexist")
    assert ei.value.verdict == vc.BLOCKED
    with pytest.raises(decisions.DecisionRefusal):
        decisions.resolve(conn, "")            # an empty why never resolves


# --------------------------------------------------------- the design door why check
def test_unresolvable_why_blocks_the_design_door(project):
    conn = _init(project)
    _persist_row(conn, "r-1", op="op-001", phase="design", why="decision:dec-missing")
    assert decisions.design_door_why_code(conn, "op-001", "design") == vc.BLOCKED
    dec = decisions.record(conn, "design", "ctx", OPTS, "patch forward", "cons")
    _persist_row(conn, "r-1", op="op-001", phase="design", why=decisions.why_pointer(dec))
    assert decisions.design_door_why_code(conn, "op-001", "design") == vc.PASS


def test_empty_why_blocks_the_design_door(project):
    conn = _init(project)
    _persist_row(conn, "r-2", op="op-001", phase="design", why="")
    assert decisions.design_door_why_code(conn, "op-001", "design") == vc.BLOCKED


# ------------------------------------------------------------ owner-decision pointer
@pytest.mark.parametrize("kind", OWNER_KINDS)
def test_owner_decision_requires_verbatim_pointer(project, kind):
    conn = _init(project)
    with pytest.raises(decisions.DecisionRefusal) as ei:
        decisions.record(conn, kind, "ctx", ["a", "b"], "a", "cons", "")
    assert ei.value.verdict == vc.BLOCKED
    dec = decisions.record(conn, kind, "ctx", ["a", "b"], "a", "cons",
                           "journal:2026-09-16T00:00:00+00:00#L42")
    assert dec["pointer"].startswith("journal:")


def test_non_owner_decision_does_not_need_a_pointer(project):
    conn = _init(project)
    dec = decisions.record(conn, "design", "ctx", OPTS, "patch forward", "cons")
    assert dec["pointer"] == ""


# ----------------------------------------- one decision event, no separate override ledger
def test_gate_skip_is_one_decision_event_no_override_ledger(project):
    conn = _init(project)
    before = len(db.events(conn, kind="decision"))
    decisions.record(conn, "gate_skip", "skip the flaky fuzz gate this pass once",
                     ["run the gate", "skip the gate"], "skip the gate",
                     "the fuzz gate is not run this pass", "journal:2026-09-16#skip")
    after = db.events(conn, kind="decision")
    assert len(after) == before + 1                 # exactly one decision event
    assert db.events(conn, kind="override") == []   # Q15: no separate override ledger


def test_level_change_is_one_decision_event(project):
    conn = _init(project)
    decisions.record(conn, "level_change", "raise the op to L5 for the fan-out",
                     ["L4", "L5"], "L5", "boundaries auto-advance from here",
                     "journal:2026-09-16#level")
    assert len(db.events(conn, kind="decision")) == 1
    assert db.events(conn, kind="override") == []


# --------------------------------------------------------- synthesis why-shape check
def test_why_shape_refuses_prose_accepts_pointer(project):
    good = [{"id": "r-1", "why": "decision:dec-abc123"}]
    assert synthesis.why_shape(good) == []
    bad = [{"id": "r-2", "why": "because I felt like it"}]
    findings = synthesis.why_shape(bad)
    assert findings and findings[0][0] == vc.FAIL
    # an empty why is not a SHAPE defect: the design door's resolution check owns that
    assert synthesis.why_shape([{"id": "r-3", "why": ""}]) == []


# --------------------------------------------------------------------- alpaca decide CLI
def test_alpaca_decide_records_a_decision(project):
    code = cli.main(["decide", "--kind", "design", "--context", "the additive path",
                     "--option", "a", "--option", "b", "--choice", "a",
                     "--consequence", "a follow-up row is owed"])
    assert code == vc.PASS
    conn = db.connect(project)
    assert len(db.events(conn, kind="decision")) == 1


def test_alpaca_decide_owner_kind_without_pointer_is_blocked(project):
    code = cli.main(["decide", "--kind", "ship", "--context", "ship it",
                     "--option", "a", "--choice", "a", "--consequence", "done"])
    assert code == vc.BLOCKED
