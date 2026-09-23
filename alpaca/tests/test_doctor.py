import json, os, shutil
from alpaca import cli, doctor, project as proj
from alpaca.tests.conftest import REPO


def _full(project):
    from pathlib import Path
    for rel in doctor._manifest_paths(REPO)["mechanism"]:
        if rel == "project.yaml":
            continue
        dst = Path(project) / rel
        if rel.endswith("/"):
            dst.mkdir(parents=True, exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src = Path(REPO) / rel
            dst.write_bytes(src.read_bytes() if src.is_file() else b"fixture\n")
    for rel in ("CLAUDE.md", "bin/alpaca", ".claude/settings.json"):
        src = os.path.join(REPO, rel)
        dst = os.path.join(project, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy(src, dst)
    os.chmod(os.path.join(project, "bin", "alpaca"), 0o755)
    for d in ("alpaca", "contracts", "intents", "design", "docs", "tests"):
        os.makedirs(os.path.join(project, d), exist_ok=True)
    with open(os.path.join(project, "pytest.ini"), "w", encoding="utf-8") as fh:
        fh.write("[pytest]\n")


def test_doctor_warns_before_onboarding(project, capsys):
    _full(project)
    cli.main(["init"])
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "GATE alpaca-doctor: WARN" in out and "project.yaml" in out


def test_doctor_passes_after_onboarding(project, capsys):
    _full(project)
    cli.main(["init"])
    cli.main(["onboard", "--name", "d", "--who", "a:owner", "--what", "w"])
    os.makedirs(doctor.paths.transcript_dir(project), exist_ok=True)
    assert cli.main(["doctor"]) == 0
    assert "GATE alpaca-doctor: PASS" in capsys.readouterr().out


def test_doctor_fails_on_a_project_yaml_missing_a_required_key(project, capsys):
    # M1.8 Done-when: alpaca doctor FAILs a project.yaml missing any required schema key.
    _full(project)
    cli.main(["init"])
    cli.main(["onboard", "--name", "d", "--who", "a:owner", "--what", "w"])
    os.makedirs(doctor.paths.transcript_dir(project), exist_ok=True)
    assert cli.main(["doctor"]) == 0  # a freshly onboarded project.yaml is schema-complete
    capsys.readouterr()
    cfg = proj.load(project)
    del cfg["fingerprint"]  # drop one required key
    proj.save(project, cfg)
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 2  # error level -> BLOCKED exit
    assert "GATE alpaca-doctor: FAIL" in out
    assert "project.yaml" in out and "fingerprint" in out


def test_doctor_warns_on_missing_record(project, capsys):
    _full(project)
    assert not os.path.isfile(doctor.paths.db_path(project))
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "GATE alpaca-doctor: WARN" in out
    assert "WARN" in out and "no record yet" in out


def test_doctor_errors_on_missing_hook(project, capsys):
    _full(project)
    cli.main(["init"])
    settings_path = os.path.join(project, ".claude", "settings.json")
    with open(settings_path, encoding="utf-8") as fh:
        s = json.load(fh)
    del s["hooks"]["SessionEnd"]
    with open(settings_path, "w", encoding="utf-8") as fh:
        json.dump(s, fh)
    assert cli.main(["doctor"]) == 2
    assert "SessionEnd" in capsys.readouterr().out
