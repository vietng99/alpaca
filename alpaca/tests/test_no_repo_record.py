"""op-014 finding F9: a test run never writes a record into the repository under test.

An instrument appends a `run` row to the record of the project its working directory resolves
to. The suite names its own checkout in ALPACA_RECORD_SKIP_ROOT (alpaca/tests/conftest.py), which a
subprocess inherits, so an instrument driven from the repository root leaves no `.alpaca/` here,
while the same instrument run in another project still records there.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SNIPPET = "from alpaca.gates import verdict; verdict.emit_verdict('f9-probe', 0, 'probe')"


def _run(cwd, env):
    env = dict(env, PYTHONPATH=REPO)
    return subprocess.run([sys.executable, "-c", SNIPPET], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=60)


def test_an_instrument_run_from_the_repo_root_writes_no_record_here():
    db = os.path.join(REPO, ".alpaca", "alpaca.db")
    before = os.path.getmtime(db) if os.path.exists(db) else None
    r = _run(REPO, os.environ)
    assert r.returncode == 0, r.stdout + r.stderr
    after = os.path.getmtime(db) if os.path.exists(db) else None
    assert after == before, "the instrument wrote into the repository's own record"


def test_the_same_instrument_still_records_in_another_project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    with open(os.path.join(REPO, "ALPACA-MANIFEST"), "rb") as src:
        (root / "ALPACA-MANIFEST").write_bytes(src.read())
    r = _run(str(root), os.environ)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (root / ".alpaca" / "alpaca.db").exists(), "the skip must only cover the repository itself"
