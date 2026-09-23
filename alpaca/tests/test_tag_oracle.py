"""M1.16 proof - the tag oracle and the derived `tag` column.

The maturity tag of an obligation row is DERIVED from the evidence its verdict rows carry,
never typed by hand (spec:290 the `tag` column, spec:329 the verify-phase default, rule 1
spec:804 "No unbuilt work is ever tagged Verified. Tags are derived, never typed."). The
evidence counter points at the row's verdict rows and their pointers (M1.16 Step 2).

Asserts the Done-when on BOTH the positive and the negative path:

  * a row with a design pointer only derives Specced (the floor: it owes no execution).
  * a static-check PASS with a genuine pointer derives Built.
  * a behavioral probe PASS with a genuine pointer derives Verified.
  * Verified is REFUSED without a behavioral probe: a static-check PASS alone stays Built.
  * a typed tag with no evidence is refused: a hand-written tag that OUT-RANKS the derived
    tag is overwritten by derivation, and the refused attempt is recorded as an event.
  * the tag is recomputed on every fold, so a later FAIL reopen drops the column back to the
    floor: the column never outlives its evidence.
  * an under-claim (a low stored tag the evidence out-ranks) is promoted silently, no event.
  * deriving an unknown row HALTs (a self-refusal, absence blocks).
"""
import os

import pytest

from alpaca import db
from alpaca.checklist import Halt, artifact, step_model, supersession, synthesis, verdict_row
from alpaca.gates import honest_tag_oracle as oracle
from alpaca.gates import verdict as vc

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
    from alpaca import cli
    cli.main(["init"])
    return db.connect(project)


def _write(project, rel, body="genuine evidence\n"):
    p = os.path.join(project, rel)
    os.makedirs(os.path.dirname(p) or project, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(body)
    return "local:%s" % rel


def _stored_tag(conn, row_id):
    return db.rows(conn, "rows", "id=?", (row_id,))[0]["tag"]


def _tag_events(conn):
    return [e for e in db.events(conn, kind=oracle.KIND, limit=10 ** 9)]


# ---------------------------------------------------------------- Specced (the floor)
def test_a_row_with_a_design_pointer_only_derives_specced(project):
    conn = _setup(project)
    r = _persist(conn, _synth(project)[0])
    # no discharge yet: the row carries only its design pointer (its `proof`).
    assert oracle.derive(conn, r["id"]) == oracle.SPECCED
    assert _stored_tag(conn, r["id"]) == oracle.SPECCED


# ---------------------------------------------------------------- Built (static-check PASS)
def test_a_static_check_pass_derives_built(project):
    conn = _setup(project)
    r = _persist(conn, _synth(project)[0])
    ev = _write(project, "build.log")
    verdict_row.discharge(conn, r["id"], r["content_hash"], instrument="static-check",
                          verdict=vc.PASS, evidence=[ev], level="L2", session="s1")
    assert oracle.derive(conn, r["id"]) == oracle.BUILT
    # recompute-on-fold already wrote the column, without a second call.
    assert _stored_tag(conn, r["id"]) == oracle.BUILT


# ---------------------------------------------------------------- Verified (behavioral probe)
def test_a_behavioral_probe_pass_derives_verified(project):
    conn = _setup(project)
    r = _persist(conn, _synth(project)[0])
    ev = _write(project, "contracts/verify/probe.log")
    verdict_row.discharge(conn, r["id"], r["content_hash"], instrument="behavioral-probe",
                          verdict=vc.PASS, evidence=[ev], level="L2", session="s1")
    assert oracle.derive(conn, r["id"]) == oracle.VERIFIED
    assert _stored_tag(conn, r["id"]) == oracle.VERIFIED


# ---------------------------------------------------------------- refuse Verified w/o a probe
def test_verified_is_refused_without_a_behavioral_probe(project):
    conn = _setup(project)
    r = _persist(conn, _synth(project)[0])
    ev = _write(project, "build.log")
    verdict_row.discharge(conn, r["id"], r["content_hash"], instrument="static-check",
                          verdict=vc.PASS, evidence=[ev], level="L2", session="s1")
    # a static-check PASS reaches Built, never Verified (spec:329, the verify-phase default).
    assert oracle.derive(conn, r["id"]) == oracle.BUILT
    # hand-type Verified into the column: derivation OVERWRITES it back to Built ...
    before = len(_tag_events(conn))
    db.patch(conn, "rows", "id", r["id"], {"tag": oracle.VERIFIED})
    assert oracle.derive(conn, r["id"]) == oracle.BUILT
    assert _stored_tag(conn, r["id"]) == oracle.BUILT
    # ... and the refused typed claim is an event on the record.
    evs = _tag_events(conn)
    assert len(evs) == before + 1
    last = evs[-1]["data"]
    assert last["overwrote"] == oracle.VERIFIED and last["derived"] == oracle.BUILT
    assert last["reason"] == oracle.R_TYPED_OVER_EVIDENCE


# ---------------------------------------------------------------- typed tag, zero evidence
def test_a_typed_tag_with_no_evidence_is_refused(project):
    conn = _setup(project)
    r = _persist(conn, _synth(project)[0])
    db.patch(conn, "rows", "id", r["id"], {"tag": oracle.VERIFIED})   # a bare hand-typed claim
    before = len(_tag_events(conn))
    assert oracle.derive(conn, r["id"]) == oracle.SPECCED             # no evidence -> the floor
    assert _stored_tag(conn, r["id"]) == oracle.SPECCED
    evs = _tag_events(conn)
    assert len(evs) == before + 1
    assert evs[-1]["data"]["overwrote"] == oracle.VERIFIED


# ---------------------------------------------------------------- under-claim promotes silently
def test_an_under_claim_is_promoted_without_an_event(project):
    conn = _setup(project)
    r = _persist(conn, _synth(project)[0])
    ev = _write(project, "contracts/verify/probe.log")
    before = len(_tag_events(conn))
    # the row still reads Specced (synthesis default) but the evidence supports Verified.
    verdict_row.discharge(conn, r["id"], r["content_hash"], instrument="behavioral-probe",
                          verdict=vc.PASS, evidence=[ev], level="L2", session="s1")
    assert oracle.derive(conn, r["id"]) == oracle.VERIFIED
    assert len(_tag_events(conn)) == before, "a promotion is not a refused attempt"


# ---------------------------------------------------------------- recompute on fold
def test_the_column_never_outlives_its_evidence(project):
    conn = _setup(project)
    r = _persist(conn, _synth(project)[0])
    ev = _write(project, "contracts/verify/probe.log")
    verdict_row.discharge(conn, r["id"], r["content_hash"], instrument="behavioral-probe",
                          verdict=vc.PASS, evidence=[ev], level="L2", session="s1")
    assert _stored_tag(conn, r["id"]) == oracle.VERIFIED
    # a later FAIL verdict reopens the row; the fold recompute drops the tag back to the floor.
    verdict_row.discharge(conn, r["id"], r["content_hash"], instrument="behavioral-probe",
                          verdict=vc.FAIL, evidence=[ev], level="L2", session="s1")
    assert verdict_row.status_fold(conn, r["id"]) == verdict_row.FAILED
    assert _stored_tag(conn, r["id"]) == oracle.SPECCED


# ---------------------------------------------------------------- a whitespace pointer is not genuine
def test_a_whitespace_pointer_does_not_earn_a_tag(project):
    conn = _setup(project)
    r = _persist(conn, _synth(project)[0])
    ev = _write(project, "empty.log", body="   \n")   # whitespace-only placeholder, not corpus
    verdict_row.discharge(conn, r["id"], r["content_hash"], instrument="static-check",
                          verdict=vc.PASS, evidence=[ev], level="L2", session="s1")
    assert oracle.derive(conn, r["id"]) == oracle.SPECCED


# ---------------------------------------------------------------- self-refusal (absence blocks)
def test_deriving_an_unknown_row_halts(project):
    conn = _setup(project)
    with pytest.raises(Halt) as ex:
        oracle.derive(conn, "no-such-row")
    assert ex.value.verdict == vc.BLOCKED
    assert ex.value.code == oracle.R_NO_SUCH_ROW
