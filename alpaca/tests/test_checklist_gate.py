"""M1.14 proof - the small checklist gate, built to the ported earlier-harness control tables.

Step 1 of the plan: port the earlier harness checklist-gate and checklist_store `--selftest` control
tables FIRST (their in-module control tables at gates/checklist-gate.py:1734/1960 and
gates/checklist_store.py:1270/1391) and let them fail, then build gate.verify(conn, op, phase)
to them. Each control below is transcribed as one pytest case asserting the EXACT verdict and
reason on both the positive and the negative path, over the record (alpaca/db.py) instead of a
signed markdown store.

Kept: the required-row universe (spec step lines merged with the owner-anchored step manifest),
the item half of check_rows (field floor, row-specificity, proof-names-row, statement shares a
token with the step text), coverage from the discharge fold, wiring, receipts keyed by header
hash, the mid-run mutation check, drift on a frozen row, and the two self-refusals
(SELFTEST-SCRATCH-INSIDE-DELIVERABLE; a too-thin control table is BLOCKED, never a pass).

Dropped per the plan (and why): the roster, capture-binding, signer-identity,
countersign-monotonicity, pre-auth-row and break-glass controls (Alpaca carries no signatures); the
anchor ORIGIN authentication (detached-MAC / countersigned-row -- authority is tracked by
supersession); the store pin (the events hash chain is the freeze witness); and DUPLICATE-ROW-ID
/ ROW-UNKNOWN-FIELD (the rows-table primary key and fixed column set make both impossible in the
store; DUPLICATE-ROW-ID stays enforced upstream by the bridge).
"""
import os
import subprocess
import sys

import pytest

from alpaca import db
from alpaca.checklist import Halt, gate
from alpaca.gates import verdict as vc

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPEC = gate.SPEC_TEXT                              # s1, s2 [never-waive], s3
MANIFEST = gate._anchorize(SPEC)


def _fresh(tmp_path, stem):
    d = tmp_path / stem
    d.mkdir()
    return str(d), db.connect(str(d))


def _wiring(root):
    wdir = os.path.join(root, "wiring")
    os.makedirs(wdir, exist_ok=True)
    with open(os.path.join(wdir, "caller.py"), "w", encoding="utf-8") as fh:
        fh.write("from alpaca.checklist import gate\ngate.verify\n")
    return [wdir]


def _mkrow(conn, root, rid, step, stmt, *, op="op-1", phase="build", proof="name",
           discharge=True, waive=False, status="open", tag="Specced"):
    """Land one obligation row and (by default) discharge it. `proof` selects the proof shape:
    name (names the row), decoy (resolves but never names it), empty, dir, missing, remote."""
    rel = "proof-%s.txt" % rid
    target = os.path.join(root, rel)
    pointer = "local:%s" % rel
    if proof == "remote":
        pointer = "remote:rack:/artifacts/%s" % rid
    elif proof == "dir":
        os.makedirs(target, exist_ok=True)
    elif proof == "empty":
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("")
    elif proof != "missing":
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("artifact for the work reported\n" + ("rows: %s\n" % rid
                     if proof == "name" else "signed by hand, no id here\n"))
    row = {"id": rid, "kind": "item", "op": op, "phase": phase, "step": step, "statement": stmt,
           "proof": pointer, "where_": "", "how": "", "when_": "", "why": "", "status": status,
           "tag": tag, "supersedes": None}
    row["content_hash"] = gate._row_content_hash(row)
    row["prev_hash"] = gate.chain_check.GENESIS
    db.upsert(conn, "rows", "id", row)
    if waive:
        gate.verdict_row.waive(conn, rid, row["content_hash"], reason="deferred", level="L2",
                               session="s1")
    elif discharge:
        gate.verdict_row.discharge(conn, rid, row["content_hash"], "probe", vc.PASS,
                                   ["local:%s" % rel], "L2", "s1")
    return row


def _default(conn, root, *, spec=SPEC, waivable=False, **rows_over):
    """Build the three-row happy path (s1, s2, s3), all discharged."""
    _mkrow(conn, root, "cover-plan", "s1", "intake artifacts reviewed against this plan",
           **rows_over.get("cover-plan", {}))
    _mkrow(conn, root, "row-model", "s2", "model authored and compiled from this plan",
           **rows_over.get("row-model", {}))
    _mkrow(conn, root, "row-bench", "s3", "bench measurement recorded on the rig",
           **rows_over.get("row-bench", {}))


def _verify(conn, root, **over):
    kw = dict(spec=SPEC, step_manifest=MANIFEST, wiring_roots=_wiring(root), root=root,
              write_receipt=False)
    kw.update(over)
    return gate.verify(conn, "op-1", "build", **kw)


# ============================================================ POS + wiring
def test_pos_01_complete_discharged_deployment_passes(tmp_path):
    root, conn = _fresh(tmp_path, "pristine")
    _default(conn, root)
    assert _verify(conn, root).verdict == vc.PASS


def test_g_01_nothing_invokes_the_gate_is_not_wired(tmp_path):
    root, conn = _fresh(tmp_path, "orphan")
    _default(conn, root)
    empty = os.path.join(root, "no-callers")
    os.makedirs(empty)
    res = _verify(conn, root, wiring_roots=[empty])
    assert res.verdict == vc.BLOCKED and res.token == gate.R_GATE_NOT_WIRED


# ============================================================ label / planted status
def test_g_02_a_hand_set_status_column_is_not_a_discharge(tmp_path):
    root, conn = _fresh(tmp_path, "planted")
    # every row carries status='done', tag='Verified' by hand, but no verdict row backs it.
    over = {"discharge": False, "status": "done", "tag": "Verified"}
    _default(conn, root, **{"cover-plan": over, "row-model": over, "row-bench": over})
    res = _verify(conn, root)
    assert res.verdict == vc.FAIL and res.token == gate.R_STEP_NOT_COVERED


# ============================================================ vacuity
def test_g_04_rows_only_for_the_first_step_is_uncovered(tmp_path):
    root, conn = _fresh(tmp_path, "step1only")
    _mkrow(conn, root, "cover-plan", "s1", "intake artifacts reviewed against this plan")
    res = _verify(conn, root)
    assert res.verdict == vc.FAIL and res.token == gate.R_STEP_NOT_COVERED


def test_g_04b_an_empty_store_is_blocked(tmp_path):
    root, conn = _fresh(tmp_path, "emptystore")
    res = _verify(conn, root)
    assert res.verdict == vc.BLOCKED and res.token == gate.R_EMPTY_STORE


def test_g_04c_a_spec_with_no_step_lines_is_blocked():
    with pytest.raises(Halt) as ex:
        gate.required_universe("# a plan with no declarations\n", "x")
    assert ex.value.verdict == vc.BLOCKED and ex.value.code == gate.R_SPEC_NO_STEPS


def test_dlg_01_an_empty_required_step_set_is_blocked(tmp_path):
    # the done-when: an empty required-row universe is BLOCKED, never a vacuous pass.
    _root, conn = _fresh(tmp_path, "emptyuniverse")
    code, findings = gate.check_coverage(conn, [{"id": "x", "step": "s1"}], {})
    assert code == vc.BLOCKED and findings[0][1] == gate.R_EMPTY_REQUIRED


# ============================================================ owner-anchored universe
def test_spec_not_supplied_is_blocked():
    with pytest.raises(Halt) as ex:
        gate.required_universe(None, "x")
    assert ex.value.code == gate.R_SPEC_NOT_SUPPLIED


def test_ga_0_a_missing_step_manifest_is_blocked():
    with pytest.raises(Halt) as ex:
        gate.required_universe(SPEC, None)
    assert ex.value.code == gate.R_STEP_ANCHOR_ABSENT


def test_ga_6_a_manifest_identical_to_the_spec_is_blocked():
    with pytest.raises(Halt) as ex:
        gate.required_universe(SPEC, SPEC)
    assert ex.value.code == gate.R_STEP_ANCHOR_IS_SPEC


def test_ga_narrow_a_spec_narrower_than_the_anchor_is_blocked():
    narrow = "step s1: intake artifacts reviewed against this plan\n"
    with pytest.raises(Halt) as ex:
        gate.required_universe(narrow, MANIFEST)
    assert ex.value.code == gate.R_STEP_UNIVERSE_NARROWED


def test_ga_7_a_hollowed_spec_statement_diverges_from_the_anchor():
    hollow = SPEC.replace("model authored and compiled from this plan", "glanced at a dashboard")
    with pytest.raises(Halt) as ex:
        gate.required_universe(hollow, MANIFEST)
    assert ex.value.code == gate.R_STEP_TEXT_DIVERGES


def test_ga_15_a_spec_wider_than_the_anchor_is_enforced(tmp_path):
    # positive: the anchor governs its steps, and a spec-only extra step is retained and enforced.
    wide = SPEC + "step s4: supplementary bringup check recorded for this run\n"
    root, conn = _fresh(tmp_path, "wider")
    _default(conn, root)
    _mkrow(conn, root, "row-extra", "s4", "supplementary bringup check recorded for this run")
    assert _verify(conn, root, spec=wide, step_manifest=MANIFEST).verdict == vc.PASS
    # drop the extra row and the spec-only step is still enforced.
    root2, conn2 = _fresh(tmp_path, "wider-missing")
    _default(conn2, root2)
    res = _verify(conn2, root2, spec=wide, step_manifest=MANIFEST)
    assert res.verdict == vc.FAIL and res.token == gate.R_STEP_NOT_COVERED


# ============================================================ item half of check_rows
def _items(conn):
    return [r for r in db.rows(conn, "rows", "kind='item'")]


def _uni(**over):
    u = {"s1": {"text": "intake artifacts reviewed against this plan", "never_waive": False},
         "s2": {"text": "model authored and compiled from this plan", "never_waive": True},
         "s3": {"text": "bench measurement recorded on the rig", "never_waive": False}}
    u.update(over)
    return u


def test_g_06_a_null_ish_statement_is_contentless(tmp_path):
    root, conn = _fresh(tmp_path, "nullish")
    _mkrow(conn, root, "cover-plan", "s1", "n/a\u200b")
    with pytest.raises(Halt) as ex:
        gate.check_rows_item_half(_items(conn), _uni(), root)
    assert ex.value.verdict == vc.FAIL and ex.value.code == gate.R_FIELD_CONTENTLESS


def test_g_06b_a_duplicated_statement_is_not_row_specific(tmp_path):
    root, conn = _fresh(tmp_path, "dupstmt")
    _mkrow(conn, root, "cover-plan", "s1", "intake artifacts reviewed against this plan")
    _mkrow(conn, root, "row-model", "s2", "intake artifacts reviewed against this plan")
    with pytest.raises(Halt) as ex:
        gate.check_rows_item_half(_items(conn), _uni(), root)
    assert ex.value.code == gate.R_NOT_ROW_SPECIFIC


def test_g_06c_a_statement_unrelated_to_the_step(tmp_path):
    root, conn = _fresh(tmp_path, "unrelated")
    _mkrow(conn, root, "row-model", "s2", "everything went fine today thanks")
    with pytest.raises(Halt) as ex:
        gate.check_rows_item_half(_items(conn), _uni(), root)
    assert ex.value.code == gate.R_UNRELATED


def test_g_07_a_proof_that_never_names_its_row(tmp_path):
    root, conn = _fresh(tmp_path, "decoy")
    _mkrow(conn, root, "cover-plan", "s1", "intake artifacts reviewed against this plan",
           proof="decoy")
    with pytest.raises(Halt) as ex:
        gate.check_rows_item_half(_items(conn), _uni(), root)
    assert ex.value.code == gate.R_PROOF_NO_SUPPORT


def test_g_07b_an_empty_proof_artifact(tmp_path):
    root, conn = _fresh(tmp_path, "emptyproof")
    _mkrow(conn, root, "cover-plan", "s1", "intake artifacts reviewed against this plan",
           proof="empty")
    with pytest.raises(Halt) as ex:
        gate.check_rows_item_half(_items(conn), _uni(), root)
    assert ex.value.code == gate.R_PROOF_EMPTY


def test_g_07c_a_proof_that_is_a_directory(tmp_path):
    root, conn = _fresh(tmp_path, "dirproof")
    _mkrow(conn, root, "cover-plan", "s1", "intake artifacts reviewed against this plan",
           proof="dir")
    with pytest.raises(Halt) as ex:
        gate.check_rows_item_half(_items(conn), _uni(), root)
    assert ex.value.code == gate.R_PROOF_NOT_A_FILE


def test_g_18_a_proof_no_longer_on_disk(tmp_path):
    root, conn = _fresh(tmp_path, "missingproof")
    _mkrow(conn, root, "cover-plan", "s1", "intake artifacts reviewed against this plan",
           proof="missing")
    with pytest.raises(Halt) as ex:
        gate.check_rows_item_half(_items(conn), _uni(), root)
    assert ex.value.code == gate.R_PROOF_ABSENT


def test_item_half_positive_a_clean_row_set_passes(tmp_path):
    root, conn = _fresh(tmp_path, "cleanrows")
    _default(conn, root)
    # no Halt is raised on the clean happy path.
    gate.check_rows_item_half(_items(conn), _uni(), root)


# ============================================================ freeze / drift
def test_g_17_a_discharged_row_edited_in_place_halts_on_drift(tmp_path):
    root, conn = _fresh(tmp_path, "drift")
    _default(conn, root)
    conn.execute("UPDATE rows SET statement='a later hand edit of the frozen row' WHERE id=?",
                 ("row-bench",))
    conn.commit()
    res = _verify(conn, root)
    assert res.verdict == vc.FAIL and res.token == gate.R_HALT_ON_DRIFT


# ============================================================ mid-run mutation
def test_g_16_a_record_mutated_during_evaluation_is_blocked(tmp_path):
    root, conn = _fresh(tmp_path, "toctou")
    _default(conn, root)

    def _mutate():
        db.append_event(conn, session="x", actor="ghost", kind="beat", ref="injected", data={})
    res = _verify(conn, root, mutate_hook=_mutate)
    assert res.verdict == vc.BLOCKED and res.token == gate.R_STORE_MUTATED


# ============================================================ waivers
def test_g_27_a_never_waive_step_waived_fails(tmp_path):
    root, conn = _fresh(tmp_path, "neverwaive")
    _mkrow(conn, root, "cover-plan", "s1", "intake artifacts reviewed against this plan")
    _mkrow(conn, root, "row-model", "s2", "model authored and compiled from this plan", waive=True)
    _mkrow(conn, root, "row-bench", "s3", "bench measurement recorded on the rig")
    res = _verify(conn, root)
    assert res.verdict == vc.FAIL and res.token == gate.R_NEVER_WAIVE


def test_g_28_a_waivable_step_waived_pauses(tmp_path):
    root, conn = _fresh(tmp_path, "waiverused")
    waivable_spec = SPEC.replace(" [never-waive]", "")
    _mkrow(conn, root, "cover-plan", "s1", "intake artifacts reviewed against this plan")
    _mkrow(conn, root, "row-model", "s2", "model authored and compiled from this plan")
    _mkrow(conn, root, "row-bench", "s3", "bench measurement recorded on the rig", waive=True)
    res = _verify(conn, root, spec=waivable_spec, step_manifest=gate._anchorize(waivable_spec))
    assert res.verdict == vc.PAUSED and res.token == gate.R_WAIVER_USED


def test_g_25_a_waiver_outside_the_universe_covers_nothing(tmp_path):
    root, conn = _fresh(tmp_path, "straywaiver")
    _mkrow(conn, root, "cover-plan", "s1", "intake artifacts reviewed against this plan")
    _mkrow(conn, root, "row-model", "s2", "model authored and compiled from this plan")
    _mkrow(conn, root, "row-stray", "s9", "an unrelated step waived covering nothing", waive=True)
    res = _verify(conn, root)                        # s3 is still uncovered
    assert res.verdict == vc.FAIL and res.token == gate.R_STEP_NOT_COVERED


# ============================================================ receipts keyed by header hash
def test_ga_11_a_universe_that_shrank_since_a_receipt_is_blocked(tmp_path):
    root, conn = _fresh(tmp_path, "shrank")
    _default(conn, root)
    # first run records a receipt over the wide anchored universe (s1, s2, s3).
    first = gate.verify(conn, "op-1", "build", spec=SPEC, step_manifest=MANIFEST,
                        wiring_roots=_wiring(root), root=root, write_receipt=True)
    assert first.verdict == vc.PASS
    # a later run anchoring a NARROWER universe (drop s3) shrank the owner universe.
    narrow_manifest = gate._anchorize("step s1: intake artifacts reviewed against this plan\n"
                                      "step s2: model authored and compiled from this plan "
                                      "[never-waive]\n")
    res = gate.verify(conn, "op-1", "build", spec=SPEC, step_manifest=narrow_manifest,
                      wiring_roots=_wiring(root), root=root, write_receipt=False)
    assert res.verdict == vc.BLOCKED and res.token == gate.R_UNIVERSE_SHRANK


# ============================================================ laundered record
def test_a_verify_pass_cannot_sit_on_a_laundered_record(tmp_path):
    # the done-when: with the events hash chain broken, no verdict can be a PASS.
    root, conn = _fresh(tmp_path, "laundered")
    _default(conn, root)
    assert _verify(conn, root).verdict == vc.PASS                 # clean record: PASS
    conn.execute("UPDATE events SET actor='TAMPER' WHERE id=(SELECT id FROM events ORDER BY id "
                 "LIMIT 1 OFFSET 1)")
    conn.commit()
    res = _verify(conn, root)
    assert res.verdict == vc.BLOCKED and res.token == gate.R_CHAIN_LAUNDERED


# ============================================================ naming tiers
def test_name_tier_marker_is_authoritative():
    body = "rows: cover-plan\nI countersign the whole batch having read the evidence.\n"
    assert gate.name_tier(body, "cover-plan") == gate.TIER_MARKER
    assert gate.name_tier(body, "row-bench") is None      # prose cannot smuggle a row in


def test_name_tier_mention_fallback_can_still_fail():
    assert gate.name_tier("I sign row evidence having read it.", "evidence") == gate.TIER_MENTION
    assert gate.name_tier("I signed nothing in particular.", "evidence") is None
    # a dot bounds an id: evidence.log does not name evidence.
    assert gate.name_tier("I read evidence.log and found nothing", "evidence") is None


# ============================================================ the two self-refusals
def test_selftest_scratch_inside_the_shipped_tree_fails(tmp_path):
    inside = os.path.join(REPO, "alpaca", "checklist")
    v, token = gate.refuse_scratch_inside_tree(inside)
    assert v == vc.FAIL and token == gate.R_SCRATCH_INSIDE
    # a scratch dir outside the tree is accepted.
    assert gate.refuse_scratch_inside_tree(str(tmp_path))[0] == vc.PASS


def test_a_too_thin_control_table_is_blocked():
    v, token = gate.refuse_thin_table(3)
    assert v == vc.BLOCKED and token == gate.R_TABLE_TOO_THIN
    assert gate.refuse_thin_table(gate.CONTROL_TABLE_FLOOR)[0] == vc.PASS


# ============================================================ the gate's own selftest
def test_gate_selftest_runs_green_above_the_floor():
    # a --selftest is a statement about this instrument, so it exits in the harness band, never
    # a subject verdict; PASS (exit 0) means every control fired, per the uniform selftest contract boot-check reads.
    assert gate.selftest() == vc.PASS


# ============================================================ exit-code contract (CLI)
def _cli(project, args):
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = project
    env["PYTHONPATH"] = REPO + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "alpaca.checklist.gate"] + args,
                          capture_output=True, text=True, encoding="utf-8", cwd=project, env=env)


def test_g_20_a_usage_error_lands_in_the_harness_band(project):
    proc = _cli(project, [])                          # missing required arguments
    assert proc.returncode not in (vc.PASS, vc.FAIL, vc.BLOCKED, vc.PAUSED)


def test_g_20b_an_absent_store_still_emits_a_gate_line_and_blocks(project):
    spec = os.path.join(project, "spec.txt")
    manifest = os.path.join(project, "manifest.txt")
    with open(spec, "w", encoding="utf-8") as fh:
        fh.write(SPEC)
    with open(manifest, "w", encoding="utf-8") as fh:
        fh.write(MANIFEST)
    proc = _cli(project, ["--op", "op-x", "--phase", "build", "--spec", spec,
                          "--step-manifest", manifest])
    assert proc.returncode == vc.BLOCKED
    assert "GATE checklist-gate" in proc.stdout
