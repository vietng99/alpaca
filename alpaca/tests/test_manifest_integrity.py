"""Manifest integrity: unsafe modes, the files an onboarded instance owns, and release modes.

  * setup/gen_manifest.py records 0644 or 0755 (the one bit git keeps), so a checkout under umask
    0002 still verifies. Other-write, setuid, setgid and sticky bits on a mechanism file are drift
    all the same: a checkout never makes them, so they are someone's change.
  * `alpaca onboard` writes project.yaml and intents/queue.md, and `alpaca upgrade` keeps both
    (alpaca/upgrade.py PRESERVE). On an onboarded instance (project.yaml no longer carries the
    template's `template: true`), verify accepts a content change there and still reports every
    other change.
  * setup/package_candidate.py refuses a selected tree with an unsafe mode.
  * setup/make_release.py writes 0644 or 0755 tar members, whatever the checkout's umask gave.

Every tree is a throwaway under pytest's tmp dir.
"""
import importlib.util
import os
import subprocess
import sys
import tarfile

import pytest

from alpaca.tests.conftest import REPO

SETUP = os.path.join(REPO, "setup")
GEN = os.path.join(SETUP, "gen_manifest.py")


def _load(basename, modname):
    spec = importlib.util.spec_from_file_location(modname, os.path.join(SETUP, basename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _w(root, rel, body, mode=0o644):
    path = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(path, mode)
    return path


TEMPLATE_YAML = "# template\nname: Alpaca\nproject_id: alpaca-template\ntemplate: true\nwhat: before onboarding\n"
ONBOARDED_YAML = "name: demo\nproject_id: 0123456789ab\nwhat: a demo project\npeople: []\n"


def _tree(root):
    """A small tree shaped like the distribution: the template project.yaml and intent queue are
    mechanism paths, as ALPACA-MANIFEST lists them."""
    _w(root, "ALPACA-MANIFEST", "[mechanism]\nalpaca/\nintents/\nproject.yaml\nrun.sh\n[memory]\n.alpaca/\n")
    _w(root, "alpaca/mod.py", "value = 1\n")
    _w(root, "run.sh", "#!/bin/sh\necho ok\n", mode=0o755)
    _w(root, "project.yaml", TEMPLATE_YAML)
    _w(root, "intents/queue.md", "# Intent queue\n\n- [ ] (nothing yet)\n")


def _gen(root, *args):
    return subprocess.run([sys.executable, GEN, *args, "--root", str(root)],
                          capture_output=True, text=True, encoding="utf-8")


def _written(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    _tree(root)
    res = _gen(root, "--write")
    assert res.returncode == 0, res.stdout
    return root


def _onboard(root):
    """What `alpaca onboard` does to the two files (alpaca/onboard.py)."""
    _w(root, "project.yaml", ONBOARDED_YAML)
    _w(root, "intents/queue.md", "# Intent queue - demo\n\n- [ ] first task\n")


# ---- unsafe modes --------------------------------------------------------------------------------

@pytest.mark.parametrize("rel,mode,why", [
    ("alpaca/mod.py", 0o666, "other-write"),
    ("alpaca/mod.py", 0o646, "other-write"),
    ("run.sh", 0o4755, "setuid"),
    ("run.sh", 0o2755, "setgid"),
    ("alpaca/mod.py", 0o1644, "sticky"),
])
def test_an_unsafe_mode_on_a_mechanism_file_is_drift(tmp_path, rel, mode, why):
    root = _written(tmp_path)
    os.chmod(root / rel, mode)
    res = _gen(root, "--verify")
    assert res.returncode == 1, res.stdout
    assert "mode/type changed: %s" % rel in res.stdout and why in res.stdout, res.stdout


def test_group_write_alone_is_still_not_drift(tmp_path):
    root = _written(tmp_path)
    os.chmod(root / "alpaca" / "mod.py", 0o664)
    os.chmod(root / "run.sh", 0o775)
    res = _gen(root, "--verify")
    assert res.returncode == 0, res.stdout


def test_write_refuses_to_record_an_unsafe_mode(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    _tree(root)
    os.chmod(root / "alpaca" / "mod.py", 0o666)
    res = _gen(root, "--write")
    assert res.returncode == 2 and "alpaca/mod.py" in res.stdout, res.stdout
    assert not (root / "MANIFEST.json").exists()


def test_restore_modes_clears_unsafe_bits(tmp_path):
    root = _written(tmp_path)
    os.chmod(root / "alpaca" / "mod.py", 0o666)
    os.chmod(root / "run.sh", 0o4775)
    assert _gen(root, "--restore-modes").returncode == 0
    assert os.stat(root / "alpaca" / "mod.py").st_mode & 0o7777 == 0o664
    assert os.stat(root / "run.sh").st_mode & 0o7777 == 0o775
    assert _gen(root, "--verify").returncode == 0


def test_selftest_covers_the_unsafe_modes():
    res = subprocess.run([sys.executable, GEN, "--selftest"], capture_output=True, text=True,
                         encoding="utf-8")
    assert res.returncode == 0, res.stdout
    assert "other-write" in res.stdout and "setuid" in res.stdout


def test_package_candidate_refuses_an_unsafe_mode(tmp_path):
    pc = _load("package_candidate.py", "alpaca_pc_integrity")
    assert pc.mode_ok(0o664, 0o644) and pc.mode_ok(0o775, 0o755)
    assert not pc.mode_ok(0o755, 0o644) and not pc.mode_ok(0o644, 0o755)
    for unsafe in (0o666, 0o646, 0o4755, 0o2755, 0o1644):
        assert not pc.mode_ok(unsafe, 0o755 if unsafe & 0o100 else 0o644), oct(unsafe)


# ---- the files an onboarded instance owns --------------------------------------------------------

def test_the_instance_owned_paths_are_the_ones_upgrade_keeps():
    from alpaca import onboard, upgrade
    gen = _load("gen_manifest.py", "alpaca_gen_integrity")
    assert set(gen.INSTANCE_OWNED) == set(upgrade.PRESERVE)
    for rel in ("project.yaml", "intents/queue.md"):          # what onboarding writes
        assert rel.split("/")[0] in gen.INSTANCE_OWNED
        assert rel in onboard.DONE_WHEN_0


def test_an_onboarded_instance_verifies(tmp_path):
    root = _written(tmp_path)
    _onboard(root)
    _w(root, "intents/later.md", "- [ ] another intent\n")      # the owner adds intents
    res = _gen(root, "--verify")
    assert res.returncode == 0, res.stdout
    assert "OK -- tree matches manifest" in res.stdout
    for rel in ("project.yaml", "intents/queue.md", "intents/later.md"):
        assert "instance-owned: %s" % rel in res.stdout, res.stdout


def test_the_template_tree_still_reports_a_changed_project_yaml(tmp_path):
    root = _written(tmp_path)
    _w(root, "project.yaml", TEMPLATE_YAML.replace("before onboarding", "edited"))
    _w(root, "intents/queue.md", "# edited\n")
    res = _gen(root, "--verify")
    assert res.returncode == 1, res.stdout
    assert "~ changed: project.yaml" in res.stdout and "~ changed: intents/queue.md" in res.stdout


def test_an_onboarded_instance_still_reports_mechanism_drift(tmp_path):
    root = _written(tmp_path)
    _onboard(root)
    _w(root, "alpaca/mod.py", "value = 2\n")
    _w(root, "alpaca/extra.py", "x = 1\n")
    os.chmod(root / "run.sh", 0o644)
    res = _gen(root, "--verify")
    assert res.returncode == 1, res.stdout
    assert "~ changed: alpaca/mod.py" in res.stdout
    assert "+ on disk, not in manifest: alpaca/extra.py" in res.stdout
    assert "mode/type changed: run.sh" in res.stdout


def test_an_onboarded_instance_still_reports_a_bad_mode_or_a_missing_instance_file(tmp_path):
    root = _written(tmp_path)
    _onboard(root)
    os.chmod(root / "project.yaml", 0o666)
    os.remove(root / "intents" / "queue.md")
    res = _gen(root, "--verify")
    assert res.returncode == 1, res.stdout
    assert "mode/type changed: project.yaml" in res.stdout
    assert "- in manifest, not on disk: intents/queue.md" in res.stdout


def test_a_tampered_manifest_digest_still_fails_on_an_instance(tmp_path):
    import json
    root = _written(tmp_path)
    _onboard(root)
    lock = root / "MANIFEST.json"
    data = json.loads(lock.read_text(encoding="utf-8"))
    data["tree_digest"] = "0" * 64
    lock.write_text(json.dumps(data), encoding="utf-8")
    res = _gen(root, "--verify")
    assert res.returncode == 1 and "tree_digest MISMATCH" in res.stdout, res.stdout


# ---- release modes -------------------------------------------------------------------------------

def _git(cwd, *args, umask=None):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "init.defaultBranch=main", *args], cwd=str(cwd),
                          capture_output=True, text=True, check=True,
                          preexec_fn=(lambda: os.umask(umask)) if umask is not None else None)


def test_make_release_writes_0644_or_0755_members_whatever_the_checkout_umask(tmp_path):
    mk = _load("make_release.py", "alpaca_release_integrity")
    src = tmp_path / "src"
    src.mkdir()
    _tree(src)
    _w(src, "CLAUDE.md", "# boot\n")
    _git(src, "init", "-q")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "tree")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(src), str(clone), umask=0o002)
    assert os.stat(clone / "alpaca" / "mod.py").st_mode & 0o777 == 0o664
    assert os.stat(clone / "run.sh").st_mode & 0o777 == 0o775
    os.chmod(clone / "CLAUDE.md", 0o666)                     # not even other-write travels
    out = tmp_path / "rel.tar.gz"
    mk.build(str(clone), str(out))
    with tarfile.open(out, mode="r:gz") as tar:
        modes = {m.name: m.mode for m in tar.getmembers()}
    assert modes["alpaca/mod.py"] == 0o644 and modes["CLAUDE.md"] == 0o644
    assert modes["run.sh"] == 0o755
    assert set(modes.values()) <= {0o644, 0o755}, modes
