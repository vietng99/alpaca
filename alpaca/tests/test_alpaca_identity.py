import json

from alpaca import adopt, db, project


def test_template_receives_fresh_local_identity_without_rewriting_template(tmp_path):
    roots = [tmp_path / "one", tmp_path / "two"]
    for root in roots:
        root.mkdir()
        (root / "project.yaml").write_text(
            "name: Alpaca\nproject_id: alpaca-template\ntemplate: true\n"
        )
    initialize = getattr(project, "ensure_instance", None)
    assert callable(initialize), "Alpaca needs project-local instance initialization"
    first = initialize(roots[0])
    second = initialize(roots[1])
    assert first["project_id"] != second["project_id"]
    assert initialize(roots[0]) == first
    assert project.load(roots[0])["project_id"] == first["project_id"]
    assert "alpaca-template" in (roots[0] / "project.yaml").read_text()
    assert json.loads((roots[0] / ".alpaca/instance.json").read_text()) == first


def test_shipped_template_is_not_misclassified_as_another_machine(tmp_path):
    (tmp_path / "project.yaml").write_text(
        "name: Alpaca\nproject_id: alpaca-template\ntemplate: true\n"
    )
    assert adopt.detect(tmp_path, db.connect(str(tmp_path))) == adopt.FRESH


def test_instrument_records_explicit_operator_session(project, monkeypatch):
    from alpaca.gates import verdict
    monkeypatch.setenv("ALPACA_SESSION_ID", "codex-proof")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "foreign-session")
    verdict.emit_verdict("operator-proof", verdict.PASS, "fixture evidence")
    event = db.events(db.connect(project), kind="run")[-1]
    assert event["session"] == "codex-proof"


TEMPLATE = "name: Alpaca\nproject_id: alpaca-template\ntemplate: true\n"


def test_a_template_resume_reads_as_not_onboarded(tmp_path):
    """A shipped template carries the distribution's name, not the project's: the pad keeps the
    onboarding line and the not-onboarded note until `alpaca onboard` writes the project's answers."""
    from alpaca import pad
    (tmp_path / "ALPACA-MANIFEST").write_text("[mechanism]\nalpaca/\n[memory]\n.alpaca/\n")
    (tmp_path / "project.yaml").write_text(TEMPLATE)
    text = pad.render(str(tmp_path))
    assert "run onboarding:" in text
    assert "This project is not onboarded" in text


def test_a_template_with_a_profile_names_the_profile_step_first(tmp_path, monkeypatch):
    from alpaca import pad, profile

    class Runbook(profile.Profile):
        def next_action(self, root):
            return "stage-a [BLOCKED]: no current evidence. Next: run stage-a"
    monkeypatch.setattr(profile, "load", lambda root: Runbook())
    (tmp_path / "ALPACA-MANIFEST").write_text("[mechanism]\nalpaca/\n[memory]\n.alpaca/\n")
    (tmp_path / "project.yaml").write_text(TEMPLATE + "profile: domain.profile\n")
    text = pad.render(str(tmp_path))
    assert "Next: run stage-a" in text
    assert "run onboarding:" not in text
    assert "This project is not onboarded" in text


def test_doctor_reads_a_template_as_not_onboarded_and_a_project_as_onboarded(project):
    from pathlib import Path
    from alpaca import doctor
    import yaml
    cfg = {"name": "shipped", "project_id": "alpaca-template", "template": True, "tier": "public",
           "commands": {k: "true" for k in ("build", "test", "lint", "run")},
           "paths": {"product_tree": ".", "artifacts": "docs", "spec_dir": "design"},
           "phases": ["build"], "oracle_classes": ["static"], "forbidden_literals": [],
           "resource_class": "small", "boundary_rules": {"root_only": True},
           "non_adoptions": {"why": "none"},
           "fingerprint": {"command": "git rev-parse HEAD", "expected": "x"}}
    path = Path(project, "project.yaml")
    path.write_text(yaml.safe_dump(cfg))
    row = {f["name"]: f for f in doctor.checks(project)}["project.yaml"]
    assert row["level"] == "warn" and "not onboarded" in row["detail"]
    assert "onboarded as" not in row["detail"]
    cfg.pop("template")
    path.write_text(yaml.safe_dump(cfg))
    row = {f["name"]: f for f in doctor.checks(project)}["project.yaml"]
    assert row["level"] == "ok" and row["detail"] == "onboarded as shipped"
