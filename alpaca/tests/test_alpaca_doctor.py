from pathlib import Path

from alpaca import doctor
from alpaca.tests.test_doctor import _full


def test_doctor_checks_codex_entrypoint_and_mcp_config(project):
    _full(project)
    before = {item["name"]: item for item in doctor.checks(project)}
    assert "codex" in before, "doctor must check both supported operators"
    assert before["codex"]["level"] == "ok"
    (Path(project) / "AGENTS.md").unlink()
    after = {item["name"]: item for item in doctor.checks(project)}
    assert after["codex"]["level"] == "error"


def test_doctor_requires_precompact_hook(project):
    import json
    _full(project)
    settings = Path(project) / ".claude/settings.json"
    data = json.loads(settings.read_text())
    data["hooks"].pop("PreCompact")
    settings.write_text(json.dumps(data))
    hooks = next(item for item in doctor.checks(project) if item["name"] == "hooks")
    assert hooks["level"] == "error"
