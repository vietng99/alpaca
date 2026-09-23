"""M1.14 proof - the chain check, the fuzz gate, and the monotonicity guard.

Ported from the earlier harness `--selftest` control tables for gates/warlog_chain_check.py,
gates/fuzz_gate.py and gates/monotonicity_check.py, adapted to run against the record's
`events` table (alpaca/db.py). Each instrument's own selftest is exercised as one case, and both
the positive and the negative path of its load-bearing property are asserted directly.

The load-bearing property the fuzz gate and the chain check share is the M1.14 done-when: a
verify() PASS may never sit on a LAUNDERED (internally inconsistent) record.
"""
import os

import pytest

from alpaca import db
from alpaca.gates import chain_check, fuzz_gate, monotonicity
from alpaca.gates import verdict as vc


def _conn(tmp_path, stem="rec"):
    return db.connect(str(tmp_path / stem))


# --------------------------------------------------------------- chain_check
def test_chain_check_selftest_all_controls_fire(tmp_path):
    # the instrument's own control table runs green (harness band, not a subject verdict).
    assert chain_check.selftest() == vc.PASS


def test_chain_check_pass_on_a_clean_chain(tmp_path):
    conn = _conn(tmp_path)
    for i in range(4):
        db.append_event(conn, session="s", actor="a", kind="beat", ref="r%d" % i, data={"i": i})
    code, reasons, facts = chain_check.verify_chain(conn)
    assert code == vc.PASS
    assert facts["rows"] == 4
    assert any(x.startswith(chain_check.R_CHAIN_HOLDS) for x in reasons)


def test_chain_check_empty_population_is_blocked(tmp_path):
    conn = _conn(tmp_path)
    code, reasons, _f = chain_check.verify_chain(conn)
    assert code == vc.BLOCKED
    assert any(x.startswith(chain_check.R_POPULATION_EMPTY) for x in reasons)


def test_chain_check_fails_on_an_edited_row(tmp_path):
    conn = _conn(tmp_path)
    for i in range(4):
        db.append_event(conn, session="s", actor="a", kind="beat", ref="r%d" % i, data={"i": i})
    conn.execute("UPDATE events SET actor='TAMPER' WHERE id=(SELECT id FROM events ORDER BY id "
                 "LIMIT 1 OFFSET 1)")
    conn.commit()
    code, reasons, _f = chain_check.verify_chain(conn)
    assert code == vc.FAIL
    assert any(x.startswith((chain_check.R_CONTENT_DRIFT, chain_check.R_CHAIN_BREAK))
               for x in reasons)


def test_chain_check_wholesale_rewrite_is_the_declared_limit(tmp_path):
    # the honest limit, executed: a consistent wholesale re-chain verifies clean, and the scope
    # note rides every verdict so no reader mistakes detect-only for tamper-proof.
    conn = _conn(tmp_path)
    for i in range(3):
        db.append_event(conn, session="s", actor="a", kind="beat", ref="r%d" % i, data={"i": i})
    code, reasons, _f = chain_check.verify_chain(conn)
    assert code == vc.PASS
    assert any(x.startswith("scope: DETECT-ONLY") for x in reasons)


# --------------------------------------------------------------- fuzz_gate
def test_fuzz_gate_selftest_all_controls_fire():
    assert fuzz_gate.selftest() == vc.PASS


def test_fuzz_gate_positive_no_tamper_is_pass_clean():
    res = fuzz_gate._round(1, "none")
    assert res["outcome"] == "PASS-CLEAN"


def test_fuzz_gate_detects_an_in_place_edit():
    res = fuzz_gate._round(2, "edit-no-rechain")
    assert res["outcome"] == "BLOCKED"


def test_fuzz_gate_detects_a_middle_deletion():
    res = fuzz_gate._round(3, "delete-middle")
    assert res["outcome"] == "BLOCKED"


def test_fuzz_gate_wipe_is_erasure_allowed():
    res = fuzz_gate._round(4, "wipe-table")
    assert res["outcome"] == "ERASURE-ALLOWED"


def test_fuzz_gate_a_broken_verify_over_a_tamper_is_laundering():
    # the negative path proving the gate can fail: a verify that lies (always PASS) over a
    # tampered record is caught as LAUNDERING -- a PASS may never sit on a laundered record.
    res = fuzz_gate._round(5, "edit-no-rechain", verify_fn=fuzz_gate._broken_verify)
    assert res["outcome"] == "LAUNDERING"
    assert res["violations"]


def test_fuzz_gate_random_rounds_find_no_laundering():
    # a short random sweep over the real substrate must never launder.
    assert fuzz_gate.run_fuzz(20, seed=7) == vc.PASS


# --------------------------------------------------------------- monotonicity
def test_monotonicity_selftest_all_controls_fire():
    assert monotonicity.selftest() == vc.PASS


def test_monotonicity_shrink_is_blocked_growth_is_ok():
    cur = {"g": 5}
    assert monotonicity.check({"g": 6}, cur) == vc.BLOCKED
    assert any(v.startswith(monotonicity.SHRINK) for v in monotonicity.diff({"g": 6}, cur))
    assert monotonicity.check({"g": 5}, cur) == vc.PASS      # equal holds
    assert monotonicity.check({"g": 4}, cur) == vc.PASS      # growth is fine


def test_monotonicity_a_vanished_gate_blocks():
    assert monotonicity.check({"ghost": 1}, {"other": 3}) == vc.BLOCKED
    assert any(v.startswith(monotonicity.MISSING)
               for v in monotonicity.diff({"ghost": 1}, {"other": 3}))


def test_monotonicity_baseline_floor_may_only_rise():
    frozen = {"checklist-gate": 18}
    with pytest.raises(monotonicity.MonotonicityError) as ex1:
        monotonicity.apply_baseline_floor({"checklist-gate": 1}, frozen)
    assert ex1.value.token == monotonicity.BASELINE_FLOOR
    with pytest.raises(monotonicity.MonotonicityError) as ex2:
        monotonicity.apply_baseline_floor({}, frozen)          # empty may not erase the floor
    assert ex2.value.token == monotonicity.BASELINE_FLOOR
    eff, raises = monotonicity.apply_baseline_floor({"checklist-gate": 30}, frozen)
    assert eff["checklist-gate"] == 30 and raises


def test_monotonicity_count_from_a_real_tally_line():
    import sys
    got = monotonicity.count_controls(
        {"g": [sys.executable, "-c", "print('  7 control(s), 0 did not fire')"]})
    assert got == {"g": 7}
    with pytest.raises(monotonicity.MonotonicityError) as ex:
        monotonicity.count_controls({"g": [sys.executable, "-c", "print('no tally at all')"]})
    assert ex.value.token == monotonicity.COUNT_UNPARSEABLE
