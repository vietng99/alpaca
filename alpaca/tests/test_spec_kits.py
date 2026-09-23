"""`alpaca spec`: spec-kit and OpenSpec installed from the vendored, pinned copies, offline.

Covers the pins (every vendored archive matches its sha256, every license is MIT-compatible and
travels with it), the spec-kit install (skills, templates, scripts, and a constitution that points
at Alpaca's rules), the one-kit-per-project refusal and its --force, the project.yaml record,
the refusal of a tampered or unsafe archive, the session-start PATH export, and one real OpenSpec
change (new, validate, archive, living spec updated) through bin/openspec in a copy of the tree.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile

import pytest
import yaml

from alpaca.tests.conftest import REPO

VENDOR = os.path.join(REPO, "vendor")
MIT_COMPATIBLE = {"MIT", "ISC", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "0BSD"}


def _pins():
    with open(os.path.join(VENDOR, "VENDOR.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _run_cli(argv, capsys):
    from alpaca import cli
    rc = cli.main(argv)
    out = capsys.readouterr().out
    return rc, json.loads(out) if out.strip().startswith("{") else out


# ------------------------------------------------------------------------------------ pins
def test_vendored_archives_match_their_pins():
    from alpaca import spec_kits
    assert spec_kits.verify() == []


def test_pins_name_repo_tag_commit_and_archive_digests():
    pins = _pins()
    for kit, repo in (("spec-kit", "https://github.com/github/spec-kit"),
                      ("openspec", "https://github.com/Fission-AI/OpenSpec")):
        doc = pins[kit]
        assert doc["upstream"] == repo
        assert doc["tag"].startswith("v") and doc["tag"].lstrip("v") == doc["version"]
        assert len(doc["commit"]) == 40 and int(doc["commit"], 16) >= 0
        assert len(doc["source_archive"]["sha256"]) == 64
        assert doc["license"] == "MIT"
        with open(os.path.join(VENDOR, doc["license_file"]), encoding="utf-8") as fh:
            assert "MIT License" in fh.read()
    assert len(pins["spec-kit"]["archive"]["sha256"]) == 64


def test_every_openspec_dependency_is_mit_compatible_and_carries_its_license():
    packages = _pins()["openspec"]["packages"]
    assert packages
    for pkg in packages:
        assert pkg["license"] in MIT_COMPATIBLE, pkg
        assert pkg["license_member"], "%s ships no license file" % pkg["name"]
        with tarfile.open(os.path.join(VENDOR, pkg["file"])) as tar:
            assert tar.getmember(pkg["license_member"]).isfile()


def test_npm_packages_are_the_published_registry_tarballs():
    """A package stored as published keeps the registry's sha512 integrity; one stored at another
    gzip level still holds the registry tarball's exact tar (tar_sha256)."""
    import base64
    import gzip
    for pkg in _pins()["openspec"]["packages"]:
        with open(os.path.join(VENDOR, pkg["file"]), "rb") as fh:
            data = fh.read()
        assert hashlib.sha256(gzip.decompress(data)).hexdigest() == pkg["tar_sha256"], pkg["name"]
        alg, want = pkg["integrity"].split("-", 1)
        if pkg["stored"] == "registry":
            assert base64.b64encode(hashlib.new(alg, data).digest()).decode() == want, pkg["name"]
        else:
            assert pkg["stored"].startswith("gzip level "), pkg
        assert pkg["registry_url"].startswith("https://registry.npmjs.org/")


def test_every_dependency_is_named_in_third_party_notices():
    with open(os.path.join(REPO, "THIRD-PARTY-NOTICES.md"), encoding="utf-8") as fh:
        notices = fh.read()
    pins = _pins()
    for kit in ("spec-kit", "openspec"):
        assert pins[kit]["upstream"] in notices
    for pkg in pins["openspec"]["packages"]:
        assert "| `%s` | %s | %s |" % (pkg["name"], pkg["version"], pkg["license"]) in notices, pkg


def test_vendor_holds_only_pinned_files():
    pins = _pins()
    known = {"VENDOR.json", pins["spec-kit"]["archive"]["path"], pins["spec-kit"]["license_file"],
             pins["spec-kit"]["constitution"]["replaced_by"], pins["openspec"]["license_file"]}
    known |= {p["file"] for p in pins["openspec"]["packages"]}
    found = set()
    for dp, dn, fn in os.walk(VENDOR):
        dn[:] = [d for d in dn if d != "__pycache__"]
        found |= {os.path.relpath(os.path.join(dp, f), VENDOR).replace(os.sep, "/") for f in fn}
    assert found == known


def test_verify_reports_a_changed_archive(tmp_path):
    from alpaca import spec_kits
    copy = tmp_path / "vendor"
    shutil.copytree(VENDOR, copy)
    target = copy / _pins()["spec-kit"]["archive"]["path"]
    data = bytearray(target.read_bytes())
    data[-1] ^= 0xFF
    target.write_bytes(bytes(data))
    problems = spec_kits.verify(str(copy))
    assert len(problems) == 1 and "does not match its pin" in problems[0]


# -------------------------------------------------------------------------------- spec-kit
def test_spec_kit_install_writes_skills_templates_and_alpaca_constitution(project, capsys):
    (open(os.path.join(project, "project.yaml"), "w", encoding="utf-8")
     .write("# keep this comment\nname: demo\ntemplate: true\nphases:\n  default:\n  - build\n"))
    rc, doc = _run_cli(["spec", "init", "--kit", "spec-kit"], capsys)
    assert rc == 0 and doc["verdict"] == "PASS" and doc["kit"] == "spec-kit"
    skills = os.path.join(project, ".claude", "skills")
    names = sorted(os.listdir(skills))
    for want in ("speckit-specify", "speckit-clarify", "speckit-plan", "speckit-tasks",
                 "speckit-implement", "speckit-constitution"):
        assert want in names
    for name in names:
        text = open(os.path.join(skills, name, "SKILL.md"), encoding="utf-8").read()
        assert text.startswith("---\n")
        front = yaml.safe_load(text.split("---\n", 2)[1])
        assert front["name"] == name and front["description"]
        assert front.get("user-invocable") is True
    for rel in (".specify/templates/spec-template.md", ".specify/templates/plan-template.md",
                ".specify/templates/tasks-template.md", ".specify/integration.json"):
        assert os.path.isfile(os.path.join(project, rel)), rel
    script = os.path.join(project, ".specify", "scripts", "bash", "create-new-feature.sh")
    assert os.access(script, os.X_OK)
    const = open(os.path.join(project, ".specify", "memory", "constitution.md"), encoding="utf-8").read()
    assert const == open(os.path.join(VENDOR, "spec-kit", "constitution.md"), encoding="utf-8").read()
    assert "`CLAUDE.md`" in const and "`doctrine/`" in const and "Alpaca's rules win" in const
    text = open(os.path.join(project, "project.yaml"), encoding="utf-8").read()
    assert text.startswith("# keep this comment\n")
    cfg = yaml.safe_load(text)
    assert cfg["phases"] == {"default": ["build"]} and cfg["template"] is True
    assert cfg["spec"]["kit"] == "spec-kit" and cfg["spec"]["version"] == _pins()["spec-kit"]["version"]
    assert cfg["spec"]["commit"] == _pins()["spec-kit"]["commit"]


def test_spec_kit_files_match_the_pinned_generated_output(project):
    from alpaca import spec_kits
    spec_kits.init(project, "spec-kit")
    pins = _pins()["spec-kit"]
    for rel, digest in pins["files"].items():
        if rel == pins["constitution"]["path"]:
            continue
        with open(os.path.join(project, rel), "rb") as fh:
            assert hashlib.sha256(fh.read()).hexdigest() == digest, rel


def test_reinstall_is_idempotent_and_an_edit_needs_force(project, capsys):
    from alpaca import spec_kits
    spec_kits.init(project, "spec-kit")
    again = spec_kits.init(project, "spec-kit")
    assert again["files"]["written"] == [] and again["files"]["unchanged"]
    const = os.path.join(project, ".specify", "memory", "constitution.md")
    with open(const, "a", encoding="utf-8") as fh:
        fh.write("local edit\n")
    rc, doc = _run_cli(["spec", "init", "--kit", "spec-kit"], capsys)
    assert rc == 2 and doc["verdict"] == "BLOCKED"
    assert doc["detail"] == [".specify/memory/constitution.md"]
    assert open(const, encoding="utf-8").read().endswith("local edit\n")
    rc, doc = _run_cli(["spec", "init", "--kit", "spec-kit", "--force"], capsys)
    assert rc == 0 and doc["files"]["replaced"] == [".specify/memory/constitution.md"]
    assert not open(const, encoding="utf-8").read().endswith("local edit\n")


# ------------------------------------------------------------------------ one kit per project
def test_openspec_is_refused_in_a_spec_kit_project(project, capsys):
    from alpaca import spec_kits
    spec_kits.init(project, "spec-kit")
    rc, doc = _run_cli(["spec", "init", "--kit", "openspec"], capsys)
    assert rc == 2 and doc["verdict"] == "BLOCKED"
    assert "project.yaml spec.kit: spec-kit" in doc["detail"] and ".specify" in doc["detail"]
    assert not os.path.exists(os.path.join(project, "openspec"))
    assert spec_kits.recorded(project)["kit"] == "spec-kit"


def test_spec_kit_is_refused_where_openspec_files_exist_unless_forced(project, capsys):
    from alpaca import spec_kits
    os.makedirs(os.path.join(project, "openspec", "specs"))
    open(os.path.join(project, "openspec", "config.yaml"), "w").write("schema: spec-driven\n")
    rc, doc = _run_cli(["spec", "init", "--kit", "spec-kit"], capsys)
    assert rc == 2 and "openspec/config.yaml" in doc["detail"]
    assert not os.path.exists(os.path.join(project, ".specify"))
    rc, doc = _run_cli(["spec", "init", "--kit", "spec-kit", "--force"], capsys)
    assert rc == 0
    rec = spec_kits.recorded(project)
    assert rec["kit"] == "spec-kit" and rec["also_present"] == "openspec"


def test_status_reports_recorded_and_detected_kits(project, capsys):
    from alpaca import spec_kits
    rc, doc = _run_cli(["spec", "status"], capsys)
    assert rc == 0 and doc["kit"] is None and doc["detected"] == {}
    spec_kits.init(project, "spec-kit")
    rc, doc = _run_cli(["spec", "status"], capsys)
    assert doc["kit"] == "spec-kit" and list(doc["detected"]) == ["spec-kit"]
    assert doc["pinned"]["openspec"]["version"] == _pins()["openspec"]["version"]


def test_unknown_kit_is_a_usage_error(project, capsys):
    from alpaca import cli
    assert cli.main(["spec", "init", "--kit", "other"]) == cli.USAGE


# ------------------------------------------------------------------------------ refusals
def _fake_vendor(tmp_path, members):
    """A vendor dir whose spec-kit archive holds `members` (name -> bytes), pinned correctly."""
    vd = tmp_path / "fake-vendor"
    shutil.copytree(VENDOR, vd)
    pins = _pins()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    path = vd / pins["spec-kit"]["archive"]["path"]
    path.write_bytes(buf.getvalue())
    pins["spec-kit"]["archive"]["sha256"] = hashlib.sha256(buf.getvalue()).hexdigest()
    (vd / "VENDOR.json").write_text(json.dumps(pins), encoding="utf-8")
    return str(vd)


def test_an_archive_member_outside_the_project_is_refused(project, tmp_path):
    from alpaca import spec_kits
    vd = _fake_vendor(tmp_path, {"../escaped.txt": b"x", ".specify/memory/constitution.md": b"c"})
    with pytest.raises(spec_kits.KitError):
        spec_kits.init(project, "spec-kit", vendor_dir=vd)
    assert not os.path.exists(os.path.join(os.path.dirname(project), "escaped.txt"))


def test_a_tampered_archive_is_refused_before_any_write(project, tmp_path, capsys):
    from alpaca import spec_kits
    vd = tmp_path / "vendor"
    shutil.copytree(VENDOR, vd)
    target = vd / _pins()["spec-kit"]["archive"]["path"]
    target.write_bytes(target.read_bytes() + b"\0")
    with pytest.raises(spec_kits.KitError) as exc:
        spec_kits.init(project, "spec-kit", vendor_dir=str(vd))
    assert "does not match its pin" in str(exc.value) and not exc.value.blocked
    assert not os.path.exists(os.path.join(project, ".specify"))


def test_record_keeps_comments_and_replaces_an_existing_block(tmp_path):
    from alpaca import spec_kits
    root = tmp_path
    (root / "project.yaml").write_text(
        "# head\nname: x\nspec:\n  kit: old\n  version: 0\nlist:\n- a\n# tail\n", encoding="utf-8")
    spec_kits.record(str(root), {"kit": "openspec", "version": "1"})
    text = (root / "project.yaml").read_text(encoding="utf-8")
    assert text.startswith("# head\n") and text.rstrip().endswith("# tail")
    assert yaml.safe_load(text) == {"name": "x", "spec": {"kit": "openspec", "version": "1"},
                                    "list": ["a"]}


# ---------------------------------------------------------------------- session-start PATH
def test_session_start_puts_bin_on_path_only_for_an_openspec_project(project, tmp_path, monkeypatch):
    from alpaca.hooks import session_start
    env_file = tmp_path / "claude-env.sh"
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))
    assert session_start.export_tool_path(project) is False
    assert not env_file.exists()
    os.makedirs(os.path.join(project, "bin"))
    launcher = os.path.join(project, "bin", "openspec")
    with open(launcher, "w") as fh:
        fh.write("#!/bin/sh\necho launched\n")
    os.chmod(launcher, 0o755)
    from alpaca import spec_kits
    spec_kits.record(project, {"kit": "openspec", "version": "x"})
    for _ in range(2):
        assert session_start.export_tool_path(project) is True
    text = env_file.read_text()
    assert text.count("export PATH=") == 1 and "OPENSPEC_TELEMETRY=0" in text
    got = subprocess.run(["bash", "-c", ". %s && openspec" % env_file], capture_output=True, text=True)
    assert got.stdout.strip() == "launched"


# ------------------------------------------------------------------- OpenSpec, end to end
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="OpenSpec runs on node; none on PATH")
def test_openspec_install_and_one_change_through_bin_openspec(tmp_path):
    from alpaca.tests.test_acceptance_m0 import _ignore
    proj = tmp_path / "os-proj"
    shutil.copytree(REPO, proj, ignore=_ignore)
    if sys.prefix != sys.base_prefix:
        os.symlink(sys.prefix, proj / ".venv")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ALPACA_", "CLAUDE_"))}
    env.update({"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "XDG_DATA_HOME": str(tmp_path / "xdgd"),
                "HOME": str(tmp_path / "home")})

    def run(*cmd, cwd=proj):
        return subprocess.run(list(cmd), cwd=cwd, env=env, capture_output=True, text=True,
                              timeout=300)

    p = run(sys.executable, "-m", "alpaca", "spec", "init", "--kit", "openspec")
    assert p.returncode == 0, p.stdout + p.stderr
    doc = json.loads(p.stdout)
    assert doc["kit"] == "openspec" and ".claude/commands/opsx/propose.md" in doc["files"]["written"]
    for rel in ("openspec/config.yaml", ".claude/skills/openspec-propose/SKILL.md",
                ".claude/commands/opsx/apply.md", ".claude/commands/opsx/archive.md"):
        assert (proj / rel).is_file(), rel
    osp = str(proj / "bin" / "openspec")
    p = run(osp, "--version")
    assert p.returncode == 0 and p.stdout.strip() == _pins()["openspec"]["version"], p.stderr
    p = run(osp, "new", "change", "add-greeting")
    assert p.returncode == 0, p.stdout + p.stderr
    change = proj / "openspec" / "changes" / "add-greeting"
    (change / "proposal.md").write_text(
        "## Why\n\nThe tool has no greeting, so a new user gets no sign that it started.\n\n"
        "## What Changes\n\n- Add a greeting printed at start.\n\n"
        "## Impact\n\n- Affected specs: greeting\n", encoding="utf-8")
    (change / "tasks.md").write_text("## 1. Greeting\n\n- [x] 1.1 Print the greeting at start\n",
                                     encoding="utf-8")
    (change / "specs" / "greeting").mkdir(parents=True)
    (change / "specs" / "greeting" / "spec.md").write_text(
        "## Purpose\n\nThe greeting line the tool prints when it starts, so a user sees it "
        "is running.\n\n"
        "## ADDED Requirements\n\n### Requirement: Start greeting\n\n"
        "The tool SHALL print one greeting line when it starts.\n\n"
        "#### Scenario: plain start\n\n- **WHEN** the user starts the tool with no arguments\n"
        "- **THEN** the first output line is the greeting\n", encoding="utf-8")
    p = run(osp, "validate", "add-greeting", "--strict")
    assert p.returncode == 0, p.stdout + p.stderr
    p = run(osp, "archive", "add-greeting", "--yes")
    assert p.returncode == 0, p.stdout + p.stderr
    living = proj / "openspec" / "specs" / "greeting" / "spec.md"
    text = living.read_text(encoding="utf-8")
    assert "The tool SHALL print one greeting line when it starts." in text
    assert "#### Scenario: plain start" in text
    assert not change.exists()
    assert any(d.name.endswith("add-greeting") for d in (proj / "openspec" / "changes" / "archive").iterdir())
    p = run(osp, "validate", "--specs", "--strict")
    assert p.returncode == 0, p.stdout + p.stderr
    # the launcher runs in the caller's cwd and the session hook exports bin/ on PATH
    env_file = tmp_path / "claude-env.sh"
    from alpaca.hooks import session_start
    old = os.environ.get("CLAUDE_ENV_FILE")
    os.environ["CLAUDE_ENV_FILE"] = str(env_file)
    try:
        assert session_start.export_tool_path(str(proj)) is True
    finally:
        if old is None:
            os.environ.pop("CLAUDE_ENV_FILE")
        else:
            os.environ["CLAUDE_ENV_FILE"] = old
    p = run("bash", "-c", ". %s && cd openspec && openspec list --specs" % env_file)
    assert p.returncode == 0 and "greeting" in p.stdout, p.stdout + p.stderr


# ------------------------------------------------------------- review fixes (M2, L1, L2, L4)
def _user_dirs(tmp_path, monkeypatch):
    """Stand-ins for the user's own config, data and home dirs, so a test sees any write there."""
    dirs = {"XDG_CONFIG_HOME": tmp_path / "user-config", "XDG_DATA_HOME": tmp_path / "user-data",
            "HOME": tmp_path / "user-home"}
    for name, path in dirs.items():
        path.mkdir()
        monkeypatch.setenv(name, str(path))
    return dirs


@pytest.mark.skipif(NODE is None, reason="OpenSpec runs on node; none on PATH")
def test_openspec_reinstall_keeps_an_edit_unless_forced(project, tmp_path, monkeypatch, capsys):
    from alpaca import spec_kits
    _user_dirs(tmp_path, monkeypatch)
    spec_kits.init(project, "openspec")
    again = spec_kits.init(project, "openspec")
    assert again["files"]["written"] == [] and again["files"]["replaced"] == []
    edited = [".claude/commands/opsx/apply.md", ".claude/skills/openspec-propose/SKILL.md"]
    for rel in edited:
        with open(os.path.join(project, rel), "a", encoding="utf-8") as fh:
            fh.write("local edit\n")
    rc, doc = _run_cli(["spec", "init", "--kit", "openspec"], capsys)
    assert rc == 2 and doc["verdict"] == "BLOCKED", doc
    assert doc["detail"] == sorted(edited)
    for rel in edited:
        assert open(os.path.join(project, rel), encoding="utf-8").read().endswith("local edit\n")
    rc, doc = _run_cli(["spec", "init", "--kit", "openspec", "--force"], capsys)
    assert rc == 0 and doc["files"]["replaced"] == sorted(edited), doc
    for rel in edited:
        assert not open(os.path.join(project, rel), encoding="utf-8").read().endswith("local edit\n")


@pytest.mark.skipif(NODE is None, reason="OpenSpec runs on node; none on PATH")
def test_openspec_install_leaves_the_user_config_alone(project, tmp_path, monkeypatch):
    """`openspec init` keeps a global config (profile, delivery, workflows). The install points it
    at a directory inside the project, so the user's own OpenSpec config is never written and
    never changes what the install generates."""
    from alpaca import spec_kits
    dirs = _user_dirs(tmp_path, monkeypatch)

    def user_files():
        return {str(p): p.read_bytes() for p in tmp_path.glob("user-*/**/*") if p.is_file()}

    spec_kits.init(project, "openspec")
    assert user_files() == {}, "the install wrote into the user's config, data or home dir"
    cfg = dirs["XDG_CONFIG_HOME"] / "openspec"
    cfg.mkdir()
    (cfg / "config.json").write_text('{"profile": "core", "delivery": "skills"}\n', encoding="utf-8")
    before = user_files()
    spec_kits.init(project, "openspec", force=True)
    assert user_files() == before
    # the user's delivery=skills did not reach the install: the /opsx commands are still there
    assert os.path.isfile(os.path.join(project, ".claude", "commands", "opsx", "apply.md"))


def _tiny_openspec_vendor(tmp_path):
    """A vendor dir holding one small npm-style package, pinned, for the runtime unpack tests."""
    vd = tmp_path / "tiny-vendor"
    (vd / "openspec" / "npm").mkdir(parents=True)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        data = b"console.log('tiny');\n"
        info = tarfile.TarInfo("package/bin/cli.js")
        info.size = len(data)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(data))
    tar_bytes = raw.getvalue()
    import gzip
    gz = gzip.compress(tar_bytes, mtime=0)
    (vd / "openspec" / "npm" / "tiny-1.0.0.tgz").write_bytes(gz)
    pins = {"openspec": {"version": "0.0.1", "entry": "node_modules/tiny/bin/cli.js",
                         "packages": [{"name": "tiny", "file": "openspec/npm/tiny-1.0.0.tgz",
                                       "sha256": hashlib.sha256(gz).hexdigest(),
                                       "tar_sha256": hashlib.sha256(tar_bytes).hexdigest(),
                                       "paths": ["node_modules/tiny"]}]}}
    (vd / "VENDOR.json").write_text(json.dumps(pins), encoding="utf-8")
    return str(vd), pins


def test_a_runtime_unpacked_by_another_caller_meanwhile_is_used(tmp_path, monkeypatch):
    """Two first runs at once: when the other caller puts a complete runtime in place between this
    caller's check and its rename, this caller uses that runtime and does not fail."""
    from alpaca import spec_kits
    vd, pins = _tiny_openspec_vendor(tmp_path)
    other = tmp_path / "other"
    ready = spec_kits.ensure_runtime(str(other), pins, vd)
    root = tmp_path / "proj"
    dest = spec_kits.runtime_dir(str(root), pins)
    real_replace = os.replace
    raced = []

    def racing_replace(src, dst, *a, **kw):
        if os.path.abspath(dst) == os.path.abspath(dest) and not raced:
            raced.append(src)
            shutil.copytree(ready, dest)
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", racing_replace)
    got = spec_kits.ensure_runtime(str(root), pins, vd)
    assert raced and got == dest
    assert os.path.isfile(os.path.join(dest, "node_modules", "tiny", "bin", "cli.js"))
    leftovers = [n for n in os.listdir(os.path.dirname(dest)) if n.startswith(".unpack-")]
    assert leftovers == []


def test_record_keeps_the_blank_lines_after_the_spec_block(tmp_path):
    from alpaca import spec_kits
    text = "name: x\nspec:\n  kit: old\n\n# next section\nlist:\n- a\n"
    (tmp_path / "project.yaml").write_text(text, encoding="utf-8")
    spec_kits.record(str(tmp_path), {"kit": "openspec"})
    assert (tmp_path / "project.yaml").read_text(encoding="utf-8") == \
        "name: x\nspec:\n  kit: openspec\n\n# next section\nlist:\n- a\n"


def test_a_malformed_project_yaml_is_a_fail_verdict_before_any_write(project, capsys):
    path = os.path.join(project, "project.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("name: [unclosed\n")
    rc, doc = _run_cli(["spec", "init", "--kit", "spec-kit"], capsys)
    assert rc == 1 and doc["verdict"] == "FAIL" and "project.yaml" in doc["reason"], doc
    assert not os.path.exists(os.path.join(project, ".specify"))
    assert open(path, encoding="utf-8").read() == "name: [unclosed\n"


def test_a_rerun_with_nothing_new_leaves_project_yaml_unchanged(project, monkeypatch):
    from alpaca import spec_kits, util
    stamps = iter(["2026-01-01T00:00:00+00:00", "2026-02-02T00:00:00+00:00"])
    monkeypatch.setattr(util, "now_iso", lambda: next(stamps))
    spec_kits.init(project, "spec-kit")
    path = os.path.join(project, "project.yaml")
    first = open(path, "rb").read()
    spec_kits.init(project, "spec-kit")
    assert open(path, "rb").read() == first
    assert spec_kits.recorded(project)["installed"] == "2026-01-01T00:00:00+00:00"
