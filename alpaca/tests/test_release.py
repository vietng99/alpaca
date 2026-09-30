"""op-006 -- the standalone release: no contamination, reproducible bytes, and it drops in and boots.

Drives setup/make_release.py through its real functions over the live REPO (a git repo, so the
tracked-set boundary is exercised for real) into throwaway tmp paths. The live .alpaca/ is never touched:
every build writes under pytest's tmp dirs and every deploy lands in a disposable tree.

A private development repository tracks its memory class on purpose (the record travels with the
private repo), and make_release refuses to ship it. There the tests build from the development
source the release pipeline's dev gate captures (the tracked files minus the memory class and the
forbidden paths, committed into a fresh repository), and one test proves the refusal on the real
tree. A tree that tracks no memory-class path is built from as it is.

Every open() passes encoding="utf-8"; every subprocess passes text=True, encoding="utf-8". No em
dash anywhere; the source is derived from alpaca.tests.conftest.REPO (from __file__), never written as a
literal path or folder name.
"""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from alpaca import manifest
from alpaca.gates import verdict as vc
from alpaca.tests.conftest import REPO

SETUP = os.path.join(REPO, "setup")


def _load(basename, modname):
    spec = importlib.util.spec_from_file_location(modname, os.path.join(SETUP, basename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MK = _load("make_release.py", "alpaca_op006_make_release")

# Paths that must never appear in a release: the memory class, version control, byte caches, the
# rendered projections, the host-local settings, the worktrees dir and the output dir.
_FORBIDDEN_PREFIXES = (".alpaca/", "analytics/", ".git/", "dist/", ".claude/worktrees/")
_FORBIDDEN_EXACT = {"RESUME.md", "board.json", "data.json", ".claude/settings.local.json"}
# A file the harness cannot boot without, one per support tree the manifest does not enumerate.
_LOAD_BEARING = ("bin/alpaca", "ALPACA-MANIFEST", "CLAUDE.md", "MAP.md", "doctrine/CORE-CARD.md",
                 "plugin/alpaca", ".claude/skills/alpaca-from-notes/SKILL.md", "setup/make_release.py", "contracts/", "alpaca/cli.py")


def _tracked_memory():
    """The memory-class paths git tracks in REPO; empty everywhere but a private development tree."""
    tracked = subprocess.run(["git", "-C", REPO, "ls-files", "-z"], capture_output=True, text=True,
                             encoding="utf-8", check=True).stdout.split("\0")
    memory = [m.rstrip("/") for m in manifest.memory(REPO)]
    return [rel for rel in tracked if rel and any(rel == m or rel.startswith(m + "/") for m in memory)]


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    """The tree a release is built from: REPO, or on a private development tree the development
    source the release dev gate captures (alpaca/release/checks.py dev_files and init_repository)."""
    if not _tracked_memory():
        return REPO
    from alpaca.release.build import forbidden, is_memory
    from alpaca.release.checks import copy_files, init_repository
    tracked = subprocess.run(["git", "-C", REPO, "ls-files", "-z"], capture_output=True, text=True,
                             encoding="utf-8", check=True).stdout.split("\0")
    memory = [m.rstrip("/") for m in manifest.memory(REPO)]
    files = {rel: Path(REPO, rel) for rel in tracked
             if rel and not forbidden(rel) and not is_memory(rel, memory) and Path(REPO, rel).is_file()}
    target = tmp_path_factory.mktemp("release-source") / "dev-source"
    copy_files(files, target)
    init_repository(target)
    return str(target)


def _members(archive):
    with tarfile.open(archive, mode="r:gz") as tar:
        return [m.name for m in tar.getmembers()]


def _build(source, tmp_path, *, keep_identity=False):
    out = os.path.join(str(tmp_path), "rel.tar.gz")
    digest, count, receipt = MK.build(source, out, keep_identity=keep_identity)
    return out, digest, count, receipt


def test_a_tree_that_tracks_its_memory_class_is_refused(tmp_path):
    if not _tracked_memory():
        pytest.skip("this tree tracks no memory-class path")
    with pytest.raises(Exception, match="SHIPMENT-FORBIDDEN-PATH"):
        MK.build(REPO, os.path.join(str(tmp_path), "rel.tar.gz"))
    assert not os.path.exists(os.path.join(str(tmp_path), "rel.tar.gz"))


def test_release_carries_no_memory_or_runtime_contamination(source, tmp_path):
    out, _digest, count, _receipt = _build(source, tmp_path)
    names = _members(out)
    assert count == len(names)
    leaked = [n for n in names
              if n in _FORBIDDEN_EXACT
              or n.endswith(".pyc")
              or "__pycache__/" in n
              or any(n == p.rstrip("/") or n.startswith(p) for p in _FORBIDDEN_PREFIXES)]
    assert leaked == [], leaked
    # template mode re-authors identity, so project.yaml does not travel
    assert "project.yaml" not in names
    for need in _LOAD_BEARING:
        assert any(n == need or n.startswith(need) for n in names), "missing from release: %s" % need


def test_release_is_reproducible(source, tmp_path):
    a, sa, _c, _r = _build(source, tmp_path / "a")
    b, sb, _c2, _r2 = _build(source, tmp_path / "b")
    assert sa == sb, "a rebuild from the same tree is not byte-for-byte equal"
    with open(a, "rb") as fa, open(b, "rb") as fb:
        assert fa.read() == fb.read()


def test_release_verify_accepts_its_own_build_and_cross_checks_the_receipt(source, tmp_path):
    out, digest, _c, receipt = _build(source, tmp_path)
    count, identity, cross = MK.verify(out)
    assert count > 0
    assert identity is False        # template: no project.yaml
    assert "matches" in cross       # the co-located .sha256 attests these bytes
    assert receipt.endswith(".sha256")
    with open(receipt, encoding="utf-8") as fh:
        assert fh.read().split()[0] == digest


def test_keep_identity_carries_project_yaml(source, tmp_path):
    out, _d, _c, _r = _build(source, tmp_path, keep_identity=True)
    names = _members(out)
    assert "project.yaml" in names
    count, identity, _cross = MK.verify(out)
    assert identity is True


def test_release_drops_in_and_boots_under_a_new_name(source, tmp_path):
    out, _d, _c, _r = _build(source, tmp_path / "pkg")
    root = str(tmp_path / "acme-widget")     # a target that does not carry the source folder name
    cfg = str(tmp_path / "cfg")
    os.makedirs(root)
    os.makedirs(cfg)
    with tarfile.open(out, mode="r:gz") as tar:
        # filter="data" lands in 3.12; the harness supports 3.10+, so guard it by version.
        if sys.version_info >= (3, 12):
            tar.extractall(root, filter="data")
        else:
            tar.extractall(root)
    assert not os.path.exists(os.path.join(root, ".alpaca")), "the memory class never travels"
    assert not os.path.isfile(os.path.join(root, "project.yaml")), "identity is re-authored"

    def _alpaca(*args):
        env = {**os.environ, "CLAUDE_PROJECT_DIR": root, "CLAUDE_CONFIG_DIR": cfg,
               "PYTHONPATH": root, "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, "-m", "alpaca", *args], cwd=root, env=env,
                              capture_output=True, text=True, encoding="utf-8")

    assert _alpaca("init").returncode == vc.PASS
    r = _alpaca("onboard", "--name", "acme-widget", "--who", "dev:owner", "--what", "a widget cli")
    assert r.returncode == vc.PASS, r.stdout + r.stderr
    d = _alpaca("doctor")
    assert d.returncode in (vc.PASS, vc.FAIL), d.stdout + d.stderr   # WARN allowed, ERROR is not
    assert "ERROR" not in d.stdout
    v = _alpaca("verify")
    assert v.returncode == vc.PASS, v.stdout + v.stderr

    with open(os.path.join(root, "project.yaml"), encoding="utf-8") as fh:
        cfg_text = fh.read()
    assert not any(ord(c) == 0x2014 for c in cfg_text)   # no em dash in the written identity
    assert cfg_text.splitlines()[0].strip() == "name: acme-widget"
