"""M1.13 proof - verdict rows, the discharge fold, drift, and reconcile.

Asserts the Done-when on BOTH the positive and the negative path:

  * discharge authors a VERDICT ROW (an event), not a status-column flip, and the fold reads
    the row as discharged.
  * a verdict bound to a STALE content_hash HALTs with the drift reason (negative), while a
    verdict bound to the current content passes (positive).
  * fold precedence, latest-wins: a later FAILED verdict reopens a discharged row.
  * a waiver REQUIRES a reason and a level; with both it folds to waived.
  * the pad cannot claim more rows back than are discharged or waived: reconcile reports the
    over-claim (dangerous direction) and a single discharge cannot back two claimed rows.
  * `alpaca task move ... --proof` authors a verdict row for a checklist obligation row, and keeps
    working unchanged for an ordinary tracker task.
"""
import os

import pytest

from alpaca import cli, db
from alpaca.checklist import Halt, artifact, step_model, synthesis, supersession, verdict_row
from alpaca.checklist import reconcile as rec
from alpaca.gates import verdict as vc
from alpaca.tests import proofkit

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODELS = os.path.join(REPO, "step-models")

CLEAN = """# Sample spec

## Acceptance

| item  | statement                          | oracle class | proof kind |
| ----- | ---------------------------------- | ------------ | ---------- |
| AC-01 | the parser reads a clean table     | unit         | test       |
| AC-02 | residue outside the table is BLOCK | unit         | test       |
| AC-03 | keys are unique inside the table   | unit         | test       |

## Open questions

None.
"""


def _synth(project):
    p = os.path.join(project, "spec.md")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(CLEAN)
    art = artifact.parse(p, "item")
    model = step_model.load(os.path.join(MODELS, "requirement.json"))
    return synthesis.synthesize(model, art)


def _persist(conn, row):
    """Land one synthesized row into the rows table (the mapping M2.1 will own; here it is a
    test fixture so the DB-backed discharge fold has real frozen rows to bind to)."""
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


def _setup(project):
    cli.main(["init"])
    return db.connect(project)


# --------------------------------------------------------------- discharge / fold
def test_discharge_authors_a_verdict_row_and_folds_discharged(project):
    conn = _setup(project)
    rows = _synth(project)
    r = _persist(conn, rows[0])
    assert verdict_row.status_fold(conn, r["id"]) == verdict_row.OPEN
    ev = verdict_row.discharge(conn, r["id"], r["content_hash"], instrument="probe",
                               verdict=vc.PASS, evidence=["local:spec.md"], level="L2",
                               session="s1")
    assert ev["kind"] == verdict_row.KIND
    # it is an appended event, not a status-column flip on the row.
    verdicts = [e for e in db.events(conn, kind=verdict_row.KIND)]
    assert len(verdicts) == 1
    assert verdicts[0]["data"]["binds"] == {"row_id": r["id"], "content_hash": r["content_hash"]}
    assert verdict_row.status_fold(conn, r["id"]) == verdict_row.DISCHARGED


def test_fold_precedence_a_later_failed_verdict_reopens(project):
    conn = _setup(project)
    r = _persist(conn, _synth(project)[0])
    verdict_row.discharge(conn, r["id"], r["content_hash"], "probe", vc.PASS, [], "L2", "s1")
    assert verdict_row.status_fold(conn, r["id"]) == verdict_row.DISCHARGED
    verdict_row.discharge(conn, r["id"], r["content_hash"], "probe", vc.FAIL, [], "L2", "s1")
    assert verdict_row.status_fold(conn, r["id"]) == verdict_row.FAILED


# --------------------------------------------------------------- drift (stale content_hash)
def test_verdict_bound_to_stale_content_hash_halts_with_drift(project):
    conn = _setup(project)
    rows = _synth(project)
    original = rows[0]
    _persist(conn, original)
    # correct the obligation: a NEW row citing supersedes (never an edit of the frozen original).
    store = [original]
    corrected = dict(original)
    corrected["id"] = original["id"] + ".fix"
    corrected["statement"] = "a corrected obligation statement here now"
    corrected["supersedes"] = original["id"]
    corrected.pop("content_hash", None)
    corrected.pop("prev_hash", None)
    store = supersession.supersede(store, original["id"], corrected)
    _persist(conn, store[-1])
    # a verdict citing the ORIGINAL (now stale) content_hash HALTs with the drift reason.
    with pytest.raises(Halt) as ex:
        verdict_row.discharge(conn, original["id"], original["content_hash"], "probe",
                              vc.PASS, [], "L2", "s1")
    assert ex.value.verdict == vc.BLOCKED
    assert ex.value.code == verdict_row.R_DRIFT
    # a verdict bound to the CURRENT content of the superseding row passes (positive path).
    head = store[-1]
    verdict_row.discharge(conn, head["id"], head["content_hash"], "probe", vc.PASS, [], "L2", "s1")
    assert verdict_row.status_fold(conn, head["id"]) == verdict_row.DISCHARGED


def test_discharge_of_an_unknown_row_halts(project):
    conn = _setup(project)
    with pytest.raises(Halt) as ex:
        verdict_row.discharge(conn, "no-such-row", "deadbeef", "probe", vc.PASS, [], "L2", "s1")
    assert ex.value.code == verdict_row.R_NO_SUCH_ROW


# --------------------------------------------------------------- waiver
def test_waiver_requires_a_reason_and_a_level(project):
    conn = _setup(project)
    r = _persist(conn, _synth(project)[0])
    with pytest.raises(Halt) as ex1:
        verdict_row.waive(conn, r["id"], r["content_hash"], reason="", level="L2", session="s1")
    assert ex1.value.code == verdict_row.R_WAIVER_NO_REASON
    with pytest.raises(Halt) as ex2:
        verdict_row.waive(conn, r["id"], r["content_hash"], reason="not applicable here",
                          level=None, session="s1")
    assert ex2.value.code == verdict_row.R_WAIVER_NO_LEVEL
    # with both, it folds to waived.
    verdict_row.waive(conn, r["id"], r["content_hash"], reason="out of scope this milestone",
                      level="L2", session="s1")
    assert verdict_row.status_fold(conn, r["id"]) == verdict_row.WAIVED_STATUS


# --------------------------------------------------------------- reconcile / over-claim
def test_pad_cannot_claim_more_than_are_discharged_or_waived(project):
    conn = _setup(project)
    rows = _synth(project)
    a, b, c = rows[0], rows[1], rows[2]
    for r in (a, b, c):
        _persist(conn, r)
    verdict_row.discharge(conn, a["id"], a["content_hash"], "probe", vc.PASS, [], "L2", "s1")
    verdict_row.waive(conn, b["id"], b["content_hash"], reason="deferred", level="L2", session="s1")
    # c is NOT discharged or waived. A pad claiming all three back is one over-claim.
    block, info = rec.reconcile(conn, [a["id"], b["id"], c["id"]])
    assert block is None
    assert info["discharged_count"] == 2
    over = {o["id"] for o in info["over_claims"]}
    assert over == {c["id"]}, "only the un-discharged row is an over-claim"


def test_one_discharge_cannot_back_two_claims(project):
    conn = _setup(project)
    a = _persist(conn, _synth(project)[0])
    verdict_row.discharge(conn, a["id"], a["content_hash"], "probe", vc.PASS, [], "L2", "s1")
    # the pad claims the SAME discharged row twice: injectivity leaves exactly one over-claim.
    block, info = rec.reconcile(conn, [a["id"], a["id"]])
    assert block is None
    assert len(info["over_claims"]) == 1


def test_empty_pad_is_blocked(project):
    conn = _setup(project)
    _persist(conn, _synth(project)[0])
    block, info = rec.reconcile(conn, [])
    assert block is not None and block[0] == rec.R_EMPTY_PAD


# --------------------------------------------------------------- alpaca task move wiring
def test_task_move_authors_a_verdict_row_for_a_checklist_row(project):
    conn = _setup(project)
    r = _persist(conn, _synth(project)[0])
    # done needs a proof, exactly as for an ordinary task.
    assert cli.main(["task", "move", r["id"], "done"]) == cli.FAIL
    # E4: done over the CLI needs the SEALED proof report, not a bare pointer.
    ptr = proofkit.seal_for(project, r["id"])
    assert cli.main(["task", "move", r["id"], "done", "--proof", ptr,
                     "--level", "L2"]) == cli.PASS
    assert verdict_row.status_fold(conn, r["id"]) == verdict_row.DISCHARGED
    # it authored a verdict row (an event), and did not create a tasks-table row.
    assert len(db.events(conn, kind=verdict_row.KIND)) == 1
    assert db.rows(conn, "tasks", "id=?", (r["id"],)) == []


def test_task_move_still_works_for_an_ordinary_task(project):
    conn = _setup(project)
    cli.main(["op", "new", "x"])
    cli.main(["task", "add", "--title", "task", "op-001", "write the verb", "--phase", "build"])
    assert cli.main(["task", "move", "t-001", "done",
                     "--proof", proofkit.seal_for(project, "t-001")]) == cli.PASS
    assert db.rows(conn, "tasks", "id=?", ("t-001",))[0]["status"] == "done"
    # an ordinary task move authors NO verdict row.
    assert len(db.events(conn, kind=verdict_row.KIND)) == 0
