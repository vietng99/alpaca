"""Portable shipment checks over synthetic trees, with no project record or network."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name):
    spec = importlib.util.spec_from_file_location("shipment_test_" + name,
                                                REPO / "setup" / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fixture(tmp_path):
    root = tmp_path / "source project"
    root.mkdir()
    (root / "setup").mkdir()
    for name in ("ship.py", "gen_manifest.py", "package_candidate.py", "shipment_policy.py"):
        shutil.copyfile(REPO / "setup" / name, root / "setup" / name)
    (root / "alpaca").mkdir()
    (root / "alpaca" / "__init__.py").write_text("VERSION = 'fixture'\n")
    (root / "alpaca" / "__main__.py").write_text("print('alpaca fixture')\n")
    (root / "project.yaml").write_text("name: null\nproject_id: null\npeople: []\ncwd_history: []\n")
    (root / "intents").mkdir()
    (root / "intents" / "queue.md").write_text("# Intent queue\n\nNo open tasks.\n")
    (root / "ALPACA-MANIFEST").write_text(
        "[mechanism]\nALPACA-MANIFEST\nsetup/\nalpaca/\nproject.yaml\nintents/\n"
        "[memory]\n.alpaca/\nRESUME.md\nanalytics/\n")
    return root


def test_identity_and_root_ignore_ambient_session(tmp_path, monkeypatch):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "ALPACA-MANIFEST").write_text("[mechanism]\n")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(foreign))
    gen = _load("gen_manifest")
    package = _load("package_candidate")
    assert Path(gen.discover_root()) == REPO
    assert gen._product_name(foreign) == "Alpaca"
    assert package.EXPECTED_PRODUCT == gen._product_name(foreign)


def test_package_reproducible_clean_and_extracts_with_spaces(tmp_path):
    ship = _load("ship")
    root = _fixture(tmp_path)
    (root / ".alpaca").mkdir()
    (root / ".alpaca" / "alpaca.db").write_text("private runtime")
    (root / "alpaca" / "__pycache__").mkdir()
    (root / "alpaca" / "__pycache__" / "junk.pyc").write_bytes(b"cache")
    first, second = tmp_path / "first.tar.gz", tmp_path / "second.tar.gz"
    ship.build(root, first)
    ship.build(root, second)
    assert first.read_bytes() == second.read_bytes()
    manifest, content = ship.verify(first)
    assert manifest["product"] == "Alpaca"
    assert not any(".alpaca" in n or "__pycache__" in n for n in content)
    assert not (root / "MANIFEST.json").exists(), "building must not mutate source"
    target = tmp_path / "different folder with spaces"
    ship.extract(first, target)
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(root), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, str(target / "setup/gen_manifest.py"), "--verify"],
                            cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    result = subprocess.run([sys.executable, "-m", "alpaca"], cwd=target,
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0 and "alpaca fixture" in result.stdout


@pytest.mark.parametrize("bad", ["name: inherited-project\n", "project_id: old-id\n",
                                 "people:\n- name: previous-owner\n",
                                 "cwd_history:\n- /old/project\n"])
def test_refuse_inherited_project_before_archive_write(tmp_path, bad):
    ship = _load("ship")
    root = _fixture(tmp_path)
    (root / "project.yaml").write_text(bad)
    archive = tmp_path / "must-not-exist.tar.gz"
    with pytest.raises(ship.ShipmentError, match="project identity"):
        ship.build(root, archive)
    assert not archive.exists()
    assert not Path(str(archive) + ".sha256").exists()


def test_distribution_template_accepted_but_marker_cannot_hide_foreign_identity(tmp_path):
    ship = _load("ship")
    root = _fixture(tmp_path)
    template = ("name: " + "Alpaca" + "\nproject_id: alpaca-template\n"
                "template: true\npeople: []\ncwd_history: []\n")
    (root / "project.yaml").write_text(template)
    ship.build(root, tmp_path / "template.tar.gz")
    (root / "project.yaml").write_text(template.replace("alpaca-template", "old-instance-id"))
    with pytest.raises(ship.ShipmentError, match="project identity"):
        ship.build(root, tmp_path / "bad.tar.gz")


def test_nested_runtime_memory_excluded_from_mechanism_directory(tmp_path):
    ship = _load("ship")
    root = _fixture(tmp_path)
    (root / "flow" / "out").mkdir(parents=True)
    (root / "flow" / "README.md").write_text("Flow sources\n")
    (root / "flow" / "out" / "secret.log").write_text("runtime only")
    with (root / "ALPACA-MANIFEST").open("a") as stream:
        stream.write("[mechanism]\nflow/\n[memory]\nflow/out/\n")
    archive = tmp_path / "nested.tar.gz"
    ship.build(root, archive)
    _, content = ship.verify(archive)
    assert "flow/README.md" in content
    assert "flow/out/secret.log" not in content


@pytest.mark.parametrize("bad", [".claude/worktrees/", ".alpaca/", "nuclear/"])
def test_manifest_cannot_ship_forbidden_paths(tmp_path, bad):
    ship = _load("ship")
    root = _fixture(tmp_path)
    target = root / bad / "secret.txt"
    target.parent.mkdir(parents=True)
    target.write_text("must stay local")
    with (root / "ALPACA-MANIFEST").open("a") as stream:
        stream.write("[mechanism]\n" + bad + "\n")
    with pytest.raises(ship.ShipmentError, match="forbidden"):
        ship.build(root, tmp_path / "bad.tar.gz")


def test_missing_mechanism_and_symlink_are_rejected(tmp_path):
    ship = _load("ship")
    root = _fixture(tmp_path)
    (root / "alpaca" / "outside.py").symlink_to(tmp_path / "outside.py")
    with pytest.raises(ship.ShipmentError, match="symlink"):
        ship.build(root, tmp_path / "bad.tar.gz")
    (root / "alpaca" / "outside.py").unlink()
    (root / "project.yaml").unlink()
    with pytest.raises(ship.ShipmentError, match="missing"):
        ship.build(root, tmp_path / "bad.tar.gz")


def test_receipt_mismatch_and_overwrite_refused(tmp_path):
    ship = _load("ship")
    root = _fixture(tmp_path)
    archive = tmp_path / "package.tar.gz"
    ship.build(root, archive)
    with pytest.raises(ship.ShipmentError, match="exists"):
        ship.build(root, archive)
    Path(str(archive) + ".sha256").write_text("0" * 64 + "  package.tar.gz\n")
    with pytest.raises(ship.ShipmentError, match="receipt"):
        ship.verify(archive)


def test_bootstrap_help_does_not_create_environment(tmp_path):
    target = tmp_path / "bootstrap path with spaces"
    (target / "setup").mkdir(parents=True)
    shutil.copyfile(REPO / "setup/bootstrap.sh", target / "setup/bootstrap.sh")
    result = subprocess.run(["bash", str(target / "setup/bootstrap.sh"), "--help"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert ".venv" in result.stdout
    assert not (target / ".venv").exists()


def test_bootstrap_rejects_system_python_disguised_as_local_environment(tmp_path):
    target = tmp_path / "bootstrap check"
    (target / "setup").mkdir(parents=True)
    shutil.copyfile(REPO / "setup/bootstrap.sh", target / "setup/bootstrap.sh")
    (target / "ALPACA-MANIFEST").write_text("[mechanism]\n")
    (target / "requirements.txt").write_text("pytest==9.1.1\n")
    (target / ".venv/bin").mkdir(parents=True)
    fake = target / ".venv/bin/python"
    fake.write_text('#!/bin/sh\nexec "' + sys.executable + '" "$@"\n')
    fake.chmod(0o755)
    result = subprocess.run(["bash", str(target / "setup/bootstrap.sh"), "--check"],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "local .venv" in result.stderr
