"""M1.15 proof -- phase gate by level, doors per phase, generic phase defaults.

Asserts the Done-when on BOTH the positive and the negative path, over the 7.4 human-decision
table rows M1 can reach (requirement -> design, design -> build, build -> verify, verify ->
release):

  * at L5 a boundary whose links all PASS advances automatically (an advance event lands);
  * at L2 the SAME boundary returns PAUSED and records a pending human decision;
  * a failing-link case with a forcing flag must NOT open the door (no flag opens a door past
    a failing link);

plus `phase_gate.enter` as a level comparison (level in force >= the phase's declared level and
the previous phase discharged, replacing the earlier harness grant check) and the ported `sign_out`
guards 0-3.
"""
import os

import pytest

from alpaca import cli, db
from alpaca.checklist import Halt, artifact, step_model, synthesis, verdict_row
from alpaca.gates import verdict as vc
from alpaca.phase import defaults, doors, phase_gate

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODELS = os.path.join(REPO, "step-models")

# workspace_guard's NO-TMP invariant would BLOCK a pytest tmp root (it lives under /tmp), so the
# door tests isolate the CONTAINMENT property from the tmp-root property exactly as
# workspace_guard's own selftest does: with the tmp-prefix set emptied.
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
    dbrow = {
        "id": row["id"], "kind": row["kind"], "op": row.get("op"), "phase": row.get("phase"),
        "step": row.get("step"), "statement": row.get("statement"), "proof": row.get("proof"),
        "where_": row.get("where"), "how": row.get("how"), "when_": row.get("when"),
        "why": row.get("why"), "session": row.get("session"), "operator": row.get("operator"),
        "status": row.get("status"), "tag": row.get("tag"),
        "content_hash": row.get("content_hash"), "prev_hash": row.get("prev_hash"),
        "supersedes": row.get("supersedes"),
    }
    db.upsert(conn, "rows", "id", dbrow)
    return row


def _rows_for_phase(conn, project, phase, op):
    """Synthesize obligation rows for `phase` from a real step model + acceptance spec, tagged
    with `op`, and land them in the record so the door has real frozen rows to fold over."""
    p = os.path.join(project, "spec-%s.md" % phase)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(SPEC)
    art = artifact.parse(p, "item")
    model = step_model.load(os.path.join(MODELS, "%s.json" % phase))
    rows = synthesis.synthesize(model, art, op=op)
    for r in rows:
        _persist(conn, r)
    return rows


def _discharge_all(conn, rows, level="L5"):
    for r in rows:
        verdict_row.discharge(conn, r["id"], r["content_hash"], instrument="probe",
                              verdict=vc.PASS, evidence=[], level=level, session="s1")


# ------------------------------------------------------------------ the three Done-when cases
def test_L5_boundary_with_all_links_pass_advances_automatically(project):
    conn = _setup(project)
    op = "op-001"
    rows = _rows_for_phase(conn, project, "requirement", op)
    _discharge_all(conn, rows)
    before = len(db.events(conn, kind="phase-advance", limit=10 ** 9))
    code = doors.run(conn, op, "requirement->design", level="L5", root=project,
                     tmp_prefixes=NOTMP, session="s1")
    assert code == vc.PASS
    advances = db.events(conn, kind="phase-advance", limit=10 ** 9)
    assert len(advances) == before + 1
    assert advances[-1]["data"]["boundary"] == "requirement->design"
    assert advances[-1]["data"]["mode"] == "auto"


def test_L2_same_boundary_pauses_and_records_a_pending_human_decision(project):
    conn = _setup(project)
    op = "op-001"
    rows = _rows_for_phase(conn, project, "requirement", op)
    _discharge_all(conn, rows)
    code = doors.run(conn, op, "requirement->design", level="L2", root=project,
                     tmp_prefixes=NOTMP, session="s1")
    assert code == vc.PAUSED
    pauses = db.events(conn, kind="phase-pause", limit=10 ** 9)
    assert pauses, "a pause must be recorded as an event"
    assert pauses[-1]["data"]["boundary"] == "requirement->design"
    assert pauses[-1]["data"]["pending"] == "human-go"


def test_no_forcing_flag_opens_a_door_past_a_failing_link(project):
    conn = _setup(project)
    op = "op-001"
    rows = _rows_for_phase(conn, project, "requirement", op)
    # discharge all but ONE: that leaves an undischarged (open) obligation, so the phase-defaults
    # link cannot PASS and the door must stay closed.
    _discharge_all(conn, rows[:-1])
    before = len(db.events(conn, kind="phase-advance", limit=10 ** 9))
    code = doors.run(conn, op, "requirement->design", level="L5", root=project,
                     tmp_prefixes=NOTMP, force=True, session="s1")
    assert code != vc.PASS, "a forcing flag must not open a door past a failing link"
    assert len(db.events(conn, kind="phase-advance", limit=10 ** 9)) == before, \
        "a closed door records no advance"
    closed = db.events(conn, kind="phase-door-closed", limit=10 ** 9)
    assert closed and closed[-1]["data"]["forced"] is True


# ------------------------------------------------------------------ every 7.4 M1 boundary
@pytest.mark.parametrize("boundary,phase,auto_level,human_level", [
    ("requirement->design", "requirement", "L5", "L2"),
    ("design->build", "design", "L5", "L2"),
    ("build->verify", "build", "L4", "L2"),
    ("verify->release", "verify", "L6", "L4"),
])
def test_every_reachable_boundary_advances_high_and_pauses_low(
        project, boundary, phase, auto_level, human_level):
    conn = _setup(project)
    op = "op-001"
    rows = _rows_for_phase(conn, project, phase, op)
    _discharge_all(conn, rows)
    assert doors.run(conn, op, boundary, level=auto_level, root=project,
                     tmp_prefixes=NOTMP, session="s1") == vc.PASS
    assert doors.run(conn, op, boundary, level=human_level, root=project,
                     tmp_prefixes=NOTMP, session="s1") == vc.PAUSED


# ------------------------------------------------------------------ enter as a level comparison
def test_enter_first_phase_needs_only_a_low_level(project):
    conn = _setup(project)
    res = phase_gate.enter(conn, "op-001", "requirement", "L1", session="s1")
    assert res["allowed"] is True


def test_enter_blocks_when_level_below_the_phases_declared_level(project):
    conn = _setup(project)
    op = "op-001"
    rows = _rows_for_phase(conn, project, "requirement", op)
    _discharge_all(conn, rows)
    with pytest.raises(Halt) as ex:
        phase_gate.enter(conn, op, "design", "L2", session="s1")
    assert ex.value.verdict == vc.BLOCKED


def test_enter_blocks_when_the_previous_phase_is_not_discharged(project):
    conn = _setup(project)
    op = "op-001"
    _rows_for_phase(conn, project, "requirement", op)  # synthesized but NOT discharged
    with pytest.raises(Halt) as ex:
        phase_gate.enter(conn, op, "design", "L5", session="s1")
    assert ex.value.verdict == vc.BLOCKED


def test_enter_allowed_when_level_meets_and_previous_discharged(project):
    conn = _setup(project)
    op = "op-001"
    rows = _rows_for_phase(conn, project, "requirement", op)
    _discharge_all(conn, rows)
    res = phase_gate.enter(conn, op, "design", "L5", session="s1")
    assert res["allowed"] is True
    enters = db.events(conn, kind="phase-enter", limit=10 ** 9)
    assert enters and enters[-1]["data"]["phase"] == "design"


# ------------------------------------------------------------------ sign_out guards 0-3
def _clean_state(**over):
    state = {
        "steps": [
            {"id": "s-1", "exit_evidence": "ev://s-1", "closes_metric": True,
             "sampled_from_ran": True},
            {"id": "s-2", "exit_evidence": "ev://s-2", "closes_metric": False},
        ],
        "questions": [{"id": "q1", "class": "research", "status": "resolved"}],
    }
    state.update(over)
    return state


def test_sign_out_clean_state_passes(project):
    conn = _setup(project)
    res = phase_gate.sign_out(conn, "op-001", "build", _clean_state(), session="s1")
    assert res["verdict"] == vc.PASS
    assert db.events(conn, kind="phase-signout", limit=10 ** 9)


def test_sign_out_missing_exit_evidence_is_rejected(project):
    conn = _setup(project)
    bad = _clean_state(steps=[
        {"id": "s-1", "exit_evidence": "", "closes_metric": True, "sampled_from_ran": True}])
    with pytest.raises(Halt) as ex:
        phase_gate.sign_out(conn, "op-001", "build", bad, session="s1")
    assert ex.value.verdict == vc.FAIL


def test_sign_out_open_flow_break_question_is_rejected(project):
    conn = _setup(project)
    bad = _clean_state(questions=[{"id": "q9", "class": "flow-break", "status": "open"}])
    with pytest.raises(Halt) as ex:
        phase_gate.sign_out(conn, "op-001", "build", bad, session="s1")
    assert ex.value.verdict == vc.FAIL


def test_sign_out_sampled_from_not_run_is_blocked(project):
    conn = _setup(project)
    bad = _clean_state(steps=[
        {"id": "s-1", "exit_evidence": "ev://s-1", "closes_metric": True,
         "sampled_from_ran": False}])
    with pytest.raises(Halt) as ex:
        phase_gate.sign_out(conn, "op-001", "build", bad, session="s1")
    assert ex.value.verdict == vc.BLOCKED


def test_sign_out_non_canonical_state_is_blocked(project):
    conn = _setup(project)

    class _Hiding(dict):
        def get(self, key, default=None):
            return [] if key == "steps" else dict.get(self, key, default)

    with pytest.raises(Halt) as ex:
        phase_gate.sign_out(conn, "op-001", "build", _Hiding(_clean_state(
            steps=[{"id": "s-1", "exit_evidence": "", "closes_metric": False}])), session="s1")
    # a subclass that hides its steps from an overridable read is refused, so the missing exit
    # evidence it tried to hide cannot slip past. Either the non-canonical block or the guard it
    # tried to dodge fires; both are correct and neither is a PASS.
    assert ex.value.verdict in (vc.BLOCKED, vc.FAIL)


# a skipped phase is recorded as skipped with a reason (Q9)
def test_skip_records_a_skip_event_with_a_reason(project):
    conn = _setup(project)
    phase_gate.skip(conn, "op-001", "verify", reason="no behavioral surface this op",
                    session="s1")
    skips = db.events(conn, kind="phase-skip", limit=10 ** 9)
    assert skips and skips[-1]["data"]["reason"] == "no behavioral surface this op"
