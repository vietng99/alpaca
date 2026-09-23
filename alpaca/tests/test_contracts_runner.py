"""M1.8 - contracts/ runner (alpaca/contracts_runner.py).

The runner executes every executable under contracts/<phase>/ under the verdict
contract and folds the exit codes with worst() (BLOCKED > FAIL > PAUSED > PASS). An
empty or absent phase directory folds to BLOCKED (a vacuous universal is never a pass).
A file that is not executable is not a contract and is skipped. A contract that exits
outside the verdict band broke the contract and cannot adjudicate, so it folds as BLOCKED.
"""
import os
import stat

from alpaca import contracts_runner
from alpaca.gates import verdict


def _script(dirpath, name, exit_code, executable=True):
    os.makedirs(dirpath, exist_ok=True)
    p = os.path.join(dirpath, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexit %d\n" % exit_code)
    if executable:
        st = os.stat(p)
        os.chmod(p, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


def test_a_single_passing_contract_passes(tmp_path):
    root = str(tmp_path)
    _script(os.path.join(root, "contracts", "build"), "ok.sh", 0)
    assert contracts_runner.run(root, "build") == verdict.PASS


def test_a_failing_contract_folds_to_fail(tmp_path):
    root = str(tmp_path)
    d = os.path.join(root, "contracts", "build")
    _script(d, "ok.sh", 0)
    _script(d, "bad.sh", 1)
    assert contracts_runner.run(root, "build") == verdict.FAIL


def test_blocked_dominates_fail(tmp_path):
    root = str(tmp_path)
    d = os.path.join(root, "contracts", "verify")
    _script(d, "bad.sh", 1)
    _script(d, "blocked.sh", 2)
    assert contracts_runner.run(root, "verify") == verdict.BLOCKED


def test_paused_folds_when_no_worse_verdict(tmp_path):
    root = str(tmp_path)
    d = os.path.join(root, "contracts", "release")
    _script(d, "ok.sh", 0)
    _script(d, "paused.sh", 3)
    assert contracts_runner.run(root, "release") == verdict.PAUSED


def test_an_empty_phase_directory_is_blocked(tmp_path):
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "contracts", "build"))
    assert contracts_runner.run(root, "build") == verdict.BLOCKED


def test_an_absent_phase_directory_is_blocked(tmp_path):
    root = str(tmp_path)
    assert contracts_runner.run(root, "nope") == verdict.BLOCKED


def test_a_non_executable_file_is_not_a_contract(tmp_path):
    root = str(tmp_path)
    d = os.path.join(root, "contracts", "build")
    _script(d, "ok.sh", 0)
    _script(d, "not-exec.sh", 1, executable=False)
    assert contracts_runner.run(root, "build") == verdict.PASS


def test_an_out_of_band_exit_folds_to_blocked(tmp_path):
    root = str(tmp_path)
    d = os.path.join(root, "contracts", "build")
    _script(d, "weird.sh", 5)
    assert contracts_runner.run(root, "build") == verdict.BLOCKED
