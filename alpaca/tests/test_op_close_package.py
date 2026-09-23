"""M4.8 proof: the op-close package.

Every claim in the Done-when is driven on a POSITIVE and a NEGATIVE path against a throwaway git
work repo carrying an Alpaca record:

  * closing an op emits a version-control bundle, its digest and an explicit restore line;
  * the digest binds the bundle bytes: re-hashing the bundle on disk equals the returned sha256,
    and tampering one byte breaks both the digest match and the bundle's own verify (the restore
    of a tampered bundle fails rather than reconstructing a different tree);
  * the restore line, executed by the test, reconstructs the tree into a FRESH directory whose
    git HEAD equals the original repo's HEAD commit;
  * the package carries the op's record slice: its rows, its verdict rows, its decisions and its
    wiki op page, each written as a file the README points at;
  * the README is GENERATED (branch, commit, bundle name, digest, restore command), never typed;
  * the close conditions are unchanged: package.close is evidence of a close, and refuses on an
    unknown op and on a root that is not a version-controlled tree.
"""
import hashlib
import os
import subprocess

import pytest

from alpaca import db, package, util
from alpaca.checklist import verdict_row
from alpaca.clock import FixedClock
from alpaca.gates import verdict as vc
from alpaca.tests.conftest import REPO


@pytest.fixture(autouse=True)
def _fixed_clock():
    util.set_clock(FixedClock("2026-01-01T00:00:00+00:00"))
    yield
    util.set_clock(None)


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          encoding="utf-8")


def _init_repo(root):
    assert _git(root, "init", "-b", "main").returncode == 0
    assert _git(root, "config", "user.email", "t@e.test").returncode == 0
    assert _git(root, "config", "user.name", "alpaca test").returncode == 0
    import shutil
    shutil.copy(os.path.join(REPO, "ALPACA-MANIFEST"), os.path.join(str(root), "ALPACA-MANIFEST"))
    with open(os.path.join(str(root), "hello.txt"), "w", encoding="utf-8") as fh:
        fh.write("hello op\n")
    assert _git(root, "add", "-A").returncode == 0
    assert _git(root, "commit", "-m", "seed the tree").returncode == 0
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _seed_record(conn, op):
    """One op with a real record slice: an obligation row, a verdict row bound to it, and one
    decision event attributed to the op."""
    now = util.now_iso()
    with db.transaction(conn):
        db.append_event(conn, session="s1", actor="human", kind="op-open", op=op,
                        data={"intent": "ship the thing", "done_when": "the bar is met"},
                        conn_in_txn=True)
        db.upsert(conn, "ops", "id", {"id": op, "intent": "ship the thing",
                                      "done_when": "the bar is met", "status": "open",
                                      "opened": now, "closed": None, "phases": None})
    db.upsert(conn, "rows", "id", {
        "id": "r-1", "kind": "item", "op": op, "phase": "build", "statement": "do the work",
        "status": "open", "content_hash": "hash-r-1"})
    verdict_row.discharge(conn, "r-1", "hash-r-1", instrument="probe", verdict=vc.PASS,
                          evidence=["local:hello.txt"], level="L2", session="s1")
    db.append_event(conn, session="s1", actor="agent", kind="decision", op=op, ref="dec-1",
                    data={"kind": "go", "choice": "proceed", "pointer": "journal:1"})


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    head = _init_repo(root)
    conn = db.connect(str(root))
    _seed_record(conn, "op-1")
    return {"root": str(root), "conn": conn, "head": head}


def test_close_emits_bundle_digest_and_restore(repo, tmp_path):
    out = str(tmp_path / "pkg")
    res = package.close(repo["conn"], "op-1", out)

    # the three declared keys
    assert set(("bundle", "sha256", "restore_cmd")) <= set(res)
    assert os.path.isfile(res["bundle"])

    # the digest BINDS the bundle bytes
    on_disk = hashlib.sha256(open(res["bundle"], "rb").read()).hexdigest()
    assert res["sha256"] == on_disk

    # the record slice: rows, verdict rows, decisions, and the wiki op page
    files = res["files"]
    for key in ("rows", "verdicts", "decisions", "op_page"):
        assert os.path.isfile(files[key]), key
    import json
    rows = json.loads(util.read_text(files["rows"]))
    assert any(r["id"] == "r-1" for r in rows)
    verdicts = json.loads(util.read_text(files["verdicts"]))
    assert any(v["data"]["binds"]["row_id"] == "r-1" for v in verdicts)
    decisions = json.loads(util.read_text(files["decisions"]))
    assert any(d["ref"] == "dec-1" for d in decisions)
    page = util.read_text(files["op_page"])
    assert "op-1" in page and "ship the thing" in page

    # the README is generated and names branch, commit, bundle, digest and the restore command
    readme = util.read_text(res["readme"])
    assert "main" in readme
    assert repo["head"] in readme
    assert os.path.basename(res["bundle"]) in readme
    assert res["sha256"] in readme
    assert res["restore_cmd"] in readme

    # the restore line reconstructs the tree into a FRESH directory whose HEAD == the original
    r = subprocess.run(res["restore_cmd"], shell=True, capture_output=True, text=True,
                       encoding="utf-8")
    assert r.returncode == 0, r.stderr
    restored = res["restore_dir"]
    assert os.path.isdir(restored)
    got = _git(restored, "rev-parse", "HEAD").stdout.strip()
    assert got == repo["head"]


def test_tampered_bundle_breaks_digest_and_restore(repo, tmp_path):
    out = str(tmp_path / "pkg")
    res = package.close(repo["conn"], "op-1", out)
    data = bytearray(open(res["bundle"], "rb").read())
    data[-1] ^= 0xFF                                   # flip a byte in the pack trailer
    with open(res["bundle"], "wb") as fh:
        fh.write(bytes(data))
    assert hashlib.sha256(bytes(data)).hexdigest() != res["sha256"]
    r = subprocess.run(res["restore_cmd"], shell=True, capture_output=True, text=True,
                       encoding="utf-8")
    assert r.returncode != 0                           # a corrupt bundle refuses to reconstruct


def test_unknown_op_refused(repo, tmp_path):
    with pytest.raises(package.PackageError):
        package.close(repo["conn"], "op-404", str(tmp_path / "pkg"))


def test_non_repo_root_refused(tmp_path):
    root = tmp_path / "bare"
    root.mkdir()
    import shutil
    shutil.copy(os.path.join(REPO, "ALPACA-MANIFEST"), os.path.join(str(root), "ALPACA-MANIFEST"))
    conn = db.connect(str(root))
    _seed_record(conn, "op-1")
    with pytest.raises(package.PackageError):
        package.close(conn, "op-1", str(tmp_path / "pkg"))
