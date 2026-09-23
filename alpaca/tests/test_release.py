"""op-006 -- the standalone release: no contamination, reproducible bytes, and it drops in and boots.

Drives setup/make_release.py through its real functions over the live REPO (a git repo, so the
tracked-set boundary is exercised for real) into throwaway tmp paths. The live .alpaca/ is never touched:
every build writes under pytest's tmp dirs and every deploy lands in a disposable tree.

Every open() passes encoding="utf-8"; every subprocess passes text=True, encoding="utf-8". No em
dash anywhere; the source is derived from alpaca.tests.conftest.REPO (from __file__), never written as a
literal path or folder name.
"""
import importlib.util
import os
import subprocess
import sys
import tarfile

import pytest

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
                 "plugin/alpaca", "skills/", "setup/make_release.py", "contracts/", "alpaca/cli.py")


def _members(archive):
    with tarfile.open(archive, mode="r:gz") as tar:
        return [m.name for m in tar.getmembers()]


def _build(tmp_path, *, keep_identity=False):
    out = os.path.join(str(tmp_path), "rel.tar.gz")
    digest, count, receipt = MK.build(REPO, out, keep_identity=keep_identity)
    return out, digest, count, receipt


def test_release_carries_no_memory_or_runtime_contamination(tmp_path):
    out, _digest, count, _receipt = _build(tmp_path)
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


def test_release_is_reproducible(tmp_path):
    a, sa, _c, _r = _build(tmp_path / "a")
    b, sb, _c2, _r2 = _build(tmp_path / "b")
    assert sa == sb, "a rebuild from the same tree is not byte-for-byte equal"
    with open(a, "rb") as fa, open(b, "rb") as fb:
        assert fa.read() == fb.read()


def test_release_verify_accepts_its_own_build_and_cross_checks_the_receipt(tmp_path):
    out, digest, _c, receipt = _build(tmp_path)
    count, identity, cross = MK.verify(out)
    assert count > 0
    assert identity is False        # template: no project.yaml
    assert "matches" in cross       # the co-located .sha256 attests these bytes
    assert receipt.endswith(".sha256")
    with open(receipt, encoding="utf-8") as fh:
        assert fh.read().split()[0] == digest


def test_keep_identity_carries_project_yaml(tmp_path):
    out, _d, _c, _r = _build(tmp_path, keep_identity=True)
    names = _members(out)
    assert "project.yaml" in names
    count, identity, _cross = MK.verify(out)
    assert identity is True


def test_release_drops_in_and_boots_under_a_new_name(tmp_path):
    out, _d, _c, _r = _build(tmp_path / "pkg")
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
