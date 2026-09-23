"""M1.3 - the verdict contract spine.

Proof test for alpaca/gates/verdict.py, alpaca/gates/contract.py, alpaca/gates/rc_conformance.py.

Done-when (from the plan): rc_conformance exits FAIL on a fixture module that restates
the verdict map or uses raw argparse at a CLI boundary, and PASS on alpaca/gates/; plus a
worst() precedence table (BLOCKED beats FAIL beats PAUSED beats PASS); plus emit_verdict
writes a run row into the record.
"""
import hashlib
import os

import pytest

from alpaca import db
from alpaca.gates import contract, rc_conformance, verdict

GATES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "alpaca", "gates")


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# --------------------------------------------------------------- verdict codes
def test_verdict_codes_are_the_one_contract():
    assert (verdict.PASS, verdict.FAIL, verdict.BLOCKED, verdict.PAUSED) == (0, 1, 2, 3)
    assert (verdict.USAGE, verdict.INTERNAL, verdict.SELFTEST, verdict.ABSENT) == (64, 67, 66, 65)
    # the verdict band and the harness band never overlap: 2 must not mean BLOCKED and usage.
    assert not (verdict.VERDICT_BAND & verdict.HARNESS_BAND)


# --------------------------------------------------------------- worst() table
def test_worst_precedence_table():
    P, F, B, Pa = verdict.PASS, verdict.FAIL, verdict.BLOCKED, verdict.PAUSED
    assert contract.worst([P]) == P
    assert contract.worst([P, F]) == F            # FAIL beats PASS
    assert contract.worst([Pa, P]) == Pa          # PAUSED beats PASS
    assert contract.worst([F, Pa]) == F           # FAIL beats PAUSED
    assert contract.worst([F, B]) == B            # BLOCKED beats FAIL
    assert contract.worst([P, F, B, Pa]) == B     # BLOCKED tops the table
    assert contract.worst([]) == B                # an empty verdict set is never a pass


# --------------------------------------------------------------- contract prims
def test_canonical_is_key_ordered_and_stable():
    a = contract.canonical({"b": 1, "a": [3, 2, 1]})
    b = contract.canonical({"a": [3, 2, 1], "b": 1})
    assert a == b
    assert isinstance(a, str)


def test_sha256_bytes_hashes_the_raw_file(tmp_path):
    p = tmp_path / "blob.bin"
    p.write_bytes(b"the exact bytes")
    assert contract.sha256_bytes(str(p)) == hashlib.sha256(b"the exact bytes").hexdigest()


def test_resolve_pointer_local_and_remote(tmp_path):
    (tmp_path / "proof.txt").write_text("ok", encoding="utf-8")
    got = contract.resolve_pointer(str(tmp_path), "local:proof.txt")
    assert os.path.isfile(got)
    assert contract.resolve_pointer(str(tmp_path), "remote:opaque-ref-1") == "opaque-ref-1"
    with pytest.raises(Exception):
        contract.resolve_pointer(str(tmp_path), "local:does-not-exist.txt")
    with pytest.raises(Exception):
        contract.resolve_pointer(str(tmp_path), "local:../escape.txt")


# --------------------------------------------------------------- rc_conformance
def test_rc_conformance_fails_a_restated_verdict_map(tmp_path):
    f = tmp_path / "restate.py"
    # A divergent restatement of the one map (PASS and FAIL swapped).
    _write(str(f), "PASS, FAIL, BLOCKED, PAUSED = 1, 0, 2, 3\n")
    defects = rc_conformance.analyze(str(f), force_boundary=True)
    assert any(rule.startswith("RC-CONTRACT") for rule, _, _ in defects), defects


def test_rc_conformance_fails_raw_argparse_at_a_boundary(tmp_path):
    f = tmp_path / "raw.py"
    _write(str(f),
           "import argparse\n"
           "if __name__ == '__main__':\n"
           "    ap = argparse.ArgumentParser()\n"
           "    ap.parse_args()\n")
    defects = rc_conformance.analyze(str(f))
    assert any(rule == "RC-RAW-ARGPARSE" for rule, _, _ in defects), defects


def test_rc_conformance_passes_on_alpaca_gates():
    defect_files, checked = rc_conformance.scan_tree(GATES_DIR)
    assert checked >= 2, "expected verdict.py and rc_conformance.py as boundaries, got %d" % checked
    assert defect_files == {}, defect_files


def test_rc_conformance_check_paths_returns_verdict_codes(tmp_path):
    clean = tmp_path / "clean.py"
    _write(str(clean),
           "from alpaca.gates import verdict\n"
           "if __name__ == '__main__':\n"
           "    raise SystemExit(verdict.emit_verdict('clean', verdict.PASS, 'ok'))\n")
    assert rc_conformance.check_paths([str(clean)]) == verdict.PASS
    bad = tmp_path / "bad.py"
    _write(str(bad), "PASS, FAIL, BLOCKED, PAUSED = 1, 0, 2, 3\n")
    assert rc_conformance.check_paths([str(bad)]) == verdict.FAIL


# --------------------------------------------------------------- census (run row)
def test_emit_verdict_writes_a_run_row(project):
    code = verdict.emit_verdict("demo-gate", verdict.PASS, "all clear", ["local:proof.txt"])
    assert code == verdict.PASS
    conn = db.connect(project)
    runs = db.events(conn, kind="run")
    assert runs, "emit_verdict wrote no run row into the record"
    last = runs[-1]
    assert last["data"]["gate"] == "demo-gate"
    assert last["data"]["code"] == verdict.PASS
    assert last["data"]["reason"] == "all clear"
    assert last["data"]["evidence"] == ["local:proof.txt"]


def test_emit_verdict_records_a_fail_too(project):
    assert verdict.emit_verdict("demo-gate", verdict.FAIL, "a real defect") == verdict.FAIL
    conn = db.connect(project)
    runs = db.events(conn, kind="run")
    assert any(r["data"]["code"] == verdict.FAIL for r in runs)
