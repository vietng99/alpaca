"""Standalone plugin support and project-local hook behavior."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugin/alpaca"


@pytest.fixture
def plugin_copy(tmp_path, monkeypatch):
    root = tmp_path / "project with spaces"
    root.mkdir()
    (root / "ALPACA-MANIFEST").write_text("[mechanism]\nplugin/alpaca/\n[memory]\n.alpaca/\n")
    shutil.copytree(PLUGIN, root / "plugin/alpaca", ignore=shutil.ignore_patterns("__pycache__"))
    (root / "bin").mkdir()
    (root / "bin/alpaca-python").write_text('#!/bin/sh\nexec "' + sys.executable + '" "$@"\n')
    (root / "bin/alpaca-python").chmod(0o755)
    (root / ".alpaca/config").mkdir(parents=True)
    monkeypatch.delenv("ALPACA_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    return root


def _run(root, hook, payload=None, *args):
    return subprocess.run(["bash", str(root / "plugin/alpaca/hooks" / hook), *args],
                          input=json.dumps(payload or {}), text=True, capture_output=True,
                          env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(REPO)})


@pytest.mark.parametrize("hook,flag,needle", [
    ("sam-inject.sh", ".sam-always", ""),
    ("adhd-always-on.sh", ".i-have-adhd-always", ""),
    ("html-safe-always-on.sh", ".html-safe-always", "ASCII"),
])
def test_opt_in_rules_resolve_inside_bundle(plugin_copy, hook, flag, needle):
    before = _run(plugin_copy, hook)
    assert before.returncode == 0 and not before.stdout
    (plugin_copy / ".alpaca/config" / flag).touch()
    after = _run(plugin_copy, hook)
    assert after.returncode == 0, after.stderr
    context = json.loads(after.stdout)["hookSpecificOutput"]["additionalContext"]
    assert len(context) > 100 and needle in context


def test_posture_is_session_local_and_reads_current_goal(plugin_copy, tmp_path, monkeypatch):
    directory = plugin_copy / ".alpaca/config"
    (directory / ".autodrive-level").write_text("L6\n")
    foreign = tmp_path / "account"
    foreign.mkdir()
    (foreign / ".autodrive-level.s-1").write_text("L6\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(foreign))
    before = _run(plugin_copy, "autodrive-inject.sh", {"session_id": "s-1"})
    assert before.returncode == 0 and not before.stdout
    (directory / ".autodrive-level.s_1").write_text("L4\n")
    (directory / ".autodrive-goal.s_1").write_text("Inspect synthesis report\n")
    after = _run(plugin_copy, "autodrive-inject.sh", {"session_id": "s_1"})
    assert after.returncode == 0, after.stderr
    output = json.loads(after.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "UserPromptSubmit"
    assert "L4" in output["additionalContext"] and "Inspect synthesis report" in output["additionalContext"]
    assert "yolo" not in after.stdout.lower()
    stop = _run(plugin_copy, "autodrive-stop.sh", {"session_id": "s_1"})
    assert stop.returncode == 0 and not stop.stdout


def test_missing_session_cannot_create_shared_posture(plugin_copy):
    before = sorted(p.name for p in (plugin_copy / ".alpaca/config").iterdir())
    result = _run(plugin_copy, "autodrive-set.sh", {}, "L6", "goal")
    assert result.returncode == 2
    assert sorted(p.name for p in (plugin_copy / ".alpaca/config").iterdir()) == before


@pytest.mark.parametrize("prior_level", ["L1", "L4"])
def test_set_and_off_update_the_active_session_record(plugin_copy, prior_level):
    from alpaca import operator, db
    operator.run("start", str(plugin_copy), "portable-session", operator="terminal")
    changed = _run(plugin_copy, "autodrive-set.sh", {}, "--session", "portable-session",
                   prior_level, "Inspect synthesis")
    assert changed.returncode == 0, changed.stdout + changed.stderr
    directory = plugin_copy / ".alpaca/config"
    assert (directory / ".autodrive-level.portable-session").read_text().strip() == prior_level
    assert (directory / ".autodrive-goal.portable-session").read_text().strip() == "Inspect synthesis"
    stopped = _run(plugin_copy, "autodrive-set.sh", {}, "--session", "portable-session", "OFF")
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert (directory / ".autodrive-level.portable-session").read_text().strip() == prior_level
    conn = db.connect(str(plugin_copy))
    try:
        row = db.rows(conn, "sessions", "sid=?", ("portable-session",))[0]
        assert row["level"] == prior_level
        assert row["ended"]
    finally:
        conn.close()


def test_skill_support_files_are_local():
    paths = ["sang/reviewer-brief.md", "capture/template.html",
             "nuclear/NUCLEAR-DOCTRINE-v3.6.md", "napalm/NAPALM-DOCTRINE-v1.1.md",
             "vsys/vsys-home/TRACE-CONVENTION.md", "vsys/vsys-home/tracer/PROTOCOL.md",
             "vsys/vsys-home/engine/gen_blueprint.py", "vsys/vsys-home/engine/blueprint-engine.html",
             "vsys/vsys-home/profiles/alpaca.json",
             "caveman/skills/caveman-compress/scripts/validate.py"]
    for relative in paths:
        assert (PLUGIN / "skills" / relative).is_file(), relative
    assert (PLUGIN / ".claude-plugin/plugin.json").is_file()


def test_compression_checker_detects_lost_code_without_editing_inputs(tmp_path):
    original, candidate = tmp_path / "source.md", tmp_path / "candidate.md"
    original.write_text("# Config\n\nUse `bin/alpaca` to inspect the state.\n")
    candidate.write_text("# Config\n\nInspect state.\n")
    helper = PLUGIN / "skills/caveman/skills/caveman-compress/scripts/check.py"
    before = original.read_bytes(), candidate.read_bytes()
    failed = subprocess.run([sys.executable, str(helper), str(original), str(candidate)],
                            capture_output=True, text=True)
    assert failed.returncode == 1 and "Inline code lost" in failed.stdout
    assert before == (original.read_bytes(), candidate.read_bytes())
    candidate.write_bytes(original.read_bytes())
    passed = subprocess.run([sys.executable, str(helper), str(original), str(candidate)],
                            capture_output=True, text=True)
    assert passed.returncode == 0, passed.stdout + passed.stderr


def test_blueprint_renders_without_external_runtime(tmp_path):
    data = tmp_path / "trace.json"
    data.write_text(json.dumps({"areas": [{"area": "flow", "nodes": [
        {"id": "cli", "label": "CLI", "file": "bin/alpaca", "evidence": "bin/alpaca:1",
         "state": "built", "what": "Runs the local harness"}], "edges": [], "coverage": []}],
        "critic": {"verdict": "BOUNDED", "misses": []}}))
    home = PLUGIN / "skills/vsys/vsys-home"
    result = subprocess.run([sys.executable, str(home / "engine/gen_blueprint.py"), str(data),
                             str(home / "profiles/alpaca.json"), str(tmp_path),
                             str(home / "engine/blueprint-engine.html")], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    html = (tmp_path / "system.html").read_bytes()
    assert html.isascii() and b"CLI" in html
    assert b'<meta charset="utf-8">' in html


def test_every_plugin_file_is_covered_by_bundle_hash():
    manifest = json.loads((PLUGIN / "BUNDLE-MANIFEST.json").read_text())
    rows = list(manifest.get("hooks", [])) + list(manifest.get("local_files", []))
    rows += [row for skill in manifest["skills"] for row in skill["files"]]
    measured = {}
    for row in rows:
        if row.get("vendored"):
            path = REPO / row["bundle_path"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]
            measured[path.resolve()] = row["sha256"]
    actual = {path.resolve() for path in PLUGIN.rglob("*") if path.is_file()
              and path.name != "BUNDLE-MANIFEST.json" and "__pycache__" not in path.parts
              and path.suffix != ".pyc"}
    assert actual == set(measured)
