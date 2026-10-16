"""alpaca new, install.sh and its launcher, and the hub address shown at session start (op-011)."""
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from alpaca import cli, new
from alpaca.tests.devfiles import names_as_data

names_as_data("docs/superpowers")                # a development-only tree a new project must not get

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FLAGS = ["--who", "tester:owner", "--yes", "--no-bootstrap", "--no-serve"]


def _new(*args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["new", *args])
    return rc, buf.getvalue()


def _verify(root):
    return subprocess.run([sys.executable, os.path.join(root, "setup", "gen_manifest.py"),
                           "--root", str(root), "--verify"], capture_output=True, text=True)


@pytest.fixture(scope="module")
def release(tmp_path_factory):
    """A release-shaped tree built the way alpaca new builds its copy: the shipped file list, the
    release templates, and a MANIFEST.json of its own."""
    root = tmp_path_factory.mktemp("release") / "tree"
    new.copy_tree(REPO, str(root))
    new.apply_release_overlays(REPO, str(root))
    with redirect_stdout(io.StringIO()):
        assert new._gen_manifest(str(root)).write(str(root)) == 0
    return root


# ---- alpaca new ------------------------------------------------------------------------------

def test_new_makes_an_onboarded_copy_named_in_its_readme(tmp_path):
    target = tmp_path / "work" / "demo"
    rc, out = _new("--name", "Demo App", "--dir", str(target), *FLAGS)
    assert rc == 0, out
    assert "doctor 0 ERROR line(s)" in out
    readme = (target / "README.md").read_text(encoding="utf-8")
    assert "# Demo App\n" in readme and new.README_START in readme and new.README_END in readme
    assert "name: Demo App" in (target / "project.yaml").read_text(encoding="utf-8")
    assert (target / ".git").is_dir()
    # runtime state and development-only trees never travel
    for absent in ("RESUME.md", "analytics", "design", "docs/superpowers", ".venv"):
        assert not (target / absent).exists(), absent
    res = _verify(target)
    assert res.returncode == 0, res.stdout
    assert "instance-owned: README.md" in res.stdout


def test_new_from_a_project_it_made_starts_the_copy_from_the_release_templates(tmp_path, release):
    """A project made by alpaca new names no release.overlays; alpaca new run from it still puts the
    shipped templates in place, so the second copy onboards as a first contact (op-045 t-480)."""
    first = tmp_path / "first"
    rc = subprocess.run([sys.executable, "-m", "alpaca", "new", "--name", "First", "--dir", str(first),
                         *FLAGS, "--no-git"], cwd=str(release), capture_output=True, text=True)
    assert rc.returncode == 0, rc.stdout + rc.stderr
    second = tmp_path / "second"
    rc = subprocess.run([sys.executable, "-m", "alpaca", "new", "--name", "Second", "--dir", str(second),
                         *FLAGS, "--no-git"], cwd=str(first), capture_output=True, text=True)
    assert rc.returncode == 0, rc.stdout + rc.stderr
    assert "release templates in place: .gitignore, intents/queue.md, project.yaml" in rc.stdout
    text = (second / "project.yaml").read_text(encoding="utf-8")
    assert "name: Second" in text and "name: First" not in text


def test_new_refuses_a_directory_that_is_not_empty(tmp_path):
    target = tmp_path / "taken"
    target.mkdir()
    (target / "notes.txt").write_text("mine\n", encoding="utf-8")
    rc, out = _new("--name", "Taken", "--dir", str(target), *FLAGS)
    assert rc == cli.FAIL and "not empty" in out
    assert sorted(p.name for p in target.iterdir()) == ["notes.txt"]


def test_new_refuses_a_directory_inside_the_installation(tmp_path):
    rc, out = _new("--name", "Inside", "--dir", os.path.join(REPO, "scratch-new-test"), *FLAGS)
    assert rc == cli.FAIL and "inside this installation" in out
    assert not os.path.exists(os.path.join(REPO, "scratch-new-test"))


def test_new_needs_a_name_when_it_cannot_ask(tmp_path):
    rc, out = _new("--dir", str(tmp_path / "x"), *FLAGS)
    assert rc == cli.USAGE and "name is required" in out


def test_fill_readme_replaces_only_the_marked_block(tmp_path):
    (tmp_path / "README.md").write_text(
        "%s\n%s\n# Guide\nbody\n" % (new.README_START, new.README_END), encoding="utf-8")
    assert new.fill_readme(str(tmp_path), "Shop")
    text = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert text.startswith(new.README_START + "\n# Shop\n") and text.endswith("# Guide\nbody\n")
    (tmp_path / "README.md").write_text("# no markers\n", encoding="utf-8")
    assert not new.fill_readme(str(tmp_path), "Shop")


def test_slug():
    assert new.slug("Link Shortener!") == "link-shortener" and new.slug("  ") == "project"


def test_the_shipped_readme_carries_the_name_block():
    text = Path(REPO, "README.md").read_text(encoding="utf-8")
    assert text.index(new.README_START) < text.index(new.README_END)


# ---- install.sh and the launcher ---------------------------------------------------------------

def _install(*args, env=None):
    return subprocess.run(["bash", os.path.join(REPO, "install.sh"), *args],
                          capture_output=True, text=True, env=env)


def test_install_from_a_release_writes_one_release_and_one_launcher(release, tmp_path):
    prefix, bindir = tmp_path / "share", tmp_path / "bin"
    res = _install("--from", str(release), "--prefix", str(prefix), "--bin-dir", str(bindir),
                   "--no-bootstrap")
    assert res.returncode == 0, res.stdout + res.stderr
    digest = json.loads((release / "MANIFEST.json").read_text())["tree_digest"][:12]
    assert os.readlink(prefix / "current") == "releases/" + digest
    assert sorted(os.listdir(prefix / "releases")) == [digest]
    launcher = (bindir / "alpaca").read_text(encoding="utf-8")
    assert "written by Alpaca install.sh" in launcher and str(prefix) in launcher
    assert os.access(bindir / "alpaca", os.X_OK)
    assert sorted(os.listdir(tmp_path)) == ["bin", "share"]


def test_a_tampered_release_installs_nothing(release, tmp_path):
    bad = tmp_path / "bad"
    subprocess.run(["cp", "-a", str(release), str(bad)], check=True)
    with open(bad / "alpaca" / "cli.py", "a", encoding="utf-8") as fh:
        fh.write("# tampered\n")
    prefix, bindir = tmp_path / "share", tmp_path / "bin"
    res = _install("--from", str(bad), "--prefix", str(prefix), "--bin-dir", str(bindir),
                   "--no-bootstrap")
    assert res.returncode == 1 and "nothing was installed" in res.stderr
    assert os.listdir(prefix / "releases") == [] and not (prefix / "current").exists()
    assert not bindir.exists()


def test_an_archive_must_match_its_sha256(release, tmp_path):
    archive = tmp_path / "alpaca.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(release, arcname="alpaca")
    good = hashlib.sha256(archive.read_bytes()).hexdigest()
    prefix, bindir = tmp_path / "share", tmp_path / "bin"
    res = _install("--archive", str(archive), "--sha256", "0" * 64, "--prefix", str(prefix),
                   "--bin-dir", str(bindir), "--no-bootstrap")
    assert res.returncode == 1 and "expected " + "0" * 64 in res.stderr and not bindir.exists()
    res = _install("--archive", str(archive), "--sha256", good, "--prefix", str(prefix),
                   "--bin-dir", str(bindir), "--no-bootstrap")
    assert res.returncode == 0, res.stderr


def test_install_refuses_an_onboarded_project_and_a_foreign_launcher(tmp_path):
    # an onboarded project of its own: this tree is one in development and a release in a release
    # candidate (op-008 t-072b), so it cannot stand in for one
    onboarded = tmp_path / "onboarded"
    onboarded.mkdir()
    (onboarded / "ALPACA-MANIFEST").write_text("[mechanism]\nproject.yaml\n", encoding="utf-8")
    (onboarded / "project.yaml").write_text("name: Demo App\nproject_id: demo-app\n", encoding="utf-8")
    res = _install("--from", str(onboarded), "--prefix", str(tmp_path / "s"), "--bin-dir",
                   str(tmp_path / "b"), "--no-bootstrap")
    assert res.returncode == 1 and "not a release" in res.stderr
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "alpaca").write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    res = _install("--from", REPO, "--prefix", str(tmp_path / "s"), "--bin-dir", str(tmp_path / "b"))
    assert res.returncode == 1 and "was not written by this installer" in res.stderr
    assert (tmp_path / "b" / "alpaca").read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"


def test_the_launcher_runs_the_project_it_is_in_and_the_release_elsewhere(release, tmp_path):
    prefix, bindir = tmp_path / "share", tmp_path / "bin"
    assert _install("--from", str(release), "--prefix", str(prefix), "--bin-dir", str(bindir),
                    "--no-bootstrap").returncode == 0
    venv = prefix / "current" / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(sys.executable)
    proj = tmp_path / "proj" / "sub"
    proj.mkdir(parents=True)
    (tmp_path / "proj" / "ALPACA-MANIFEST").write_text("", encoding="utf-8")
    (tmp_path / "proj" / "bin").mkdir()
    fake = tmp_path / "proj" / "bin" / "alpaca"
    fake.write_text("#!/bin/sh\necho project-cli \"$@\"\n", encoding="utf-8")
    fake.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "ALPACA_ROOT")}
    inside = subprocess.run([str(bindir / "alpaca"), "status"], cwd=proj, capture_output=True,
                            text=True, env=env)
    assert inside.stdout.strip() == "project-cli status"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    out = subprocess.run([str(bindir / "alpaca"), "new", "--help"], cwd=elsewhere,
                         capture_output=True, text=True, env=env)
    assert out.returncode == 0 and "--no-bootstrap" in out.stdout, out.stderr


def test_the_launcher_holds_both_folders_as_literals_and_routes_update_to_the_release(release, tmp_path):
    """alpaca update reads the prefix and the launcher folder from the launcher; a quote or a space in
    either must come back as it was, and `update` must reach the installed release, not a project."""
    prefix, bindir = tmp_path / "it's a share", tmp_path / "b'in dir"
    assert _install("--from", str(release), "--prefix", str(prefix), "--bin-dir", str(bindir),
                    "--no-bootstrap").returncode == 0
    launcher = bindir / "alpaca"
    text = launcher.read_text(encoding="utf-8")
    assert re.search(r"^ALPACA_BIN_DIR='.*'$", text, re.M)
    assigns = "\n".join(line for line in text.splitlines() if line.startswith("ALPACA_"))
    shown = subprocess.run(["sh", "-c", assigns + '\nprintf "%s\\n%s" "$ALPACA_HOME" "$ALPACA_BIN_DIR"'],
                           capture_output=True, text=True)
    assert shown.stdout == "%s\n%s" % (prefix.resolve(), bindir.resolve()), shown.stderr
    route = [line for line in text.splitlines() if line.startswith('if [ "${1-}" = update ]')]
    assert len(route) == 1 and '"$ALPACA_HOME/current/bin/alpaca" update --prefix "$ALPACA_HOME"' in route[0]
    assert text.index(route[0]) < text.index("while :")       # before the project lookup


# ---- the hub address at session start ----------------------------------------------------------

def test_emit_context_shows_the_person_a_message_when_given(capsys):
    from alpaca.hooks import common
    common.emit_context("SessionStart", "ctx", user_message="Alpaca hub: http://127.0.0.1:1/")
    out = json.loads(capsys.readouterr().out)
    assert out["systemMessage"] == "Alpaca hub: http://127.0.0.1:1/"
    assert out["hookSpecificOutput"]["additionalContext"] == "ctx"
    common.emit_context("SessionStart", "ctx")
    assert "systemMessage" not in json.loads(capsys.readouterr().out)


def test_session_start_starts_the_hub_unless_the_payload_opts_out(project, monkeypatch):
    from alpaca import serve
    from alpaca.hooks import session_start
    calls = []
    monkeypatch.setattr(serve, "ensure_running", lambda root: calls.append(root) or "http://127.0.0.1:7/")
    res = session_start.handle({"session_id": "hub00000-0000", "cwd": project, "source": "startup"})
    assert res["hub_url"] == "http://127.0.0.1:7/" and "Live dashboard: http://127.0.0.1:7/" in res["context"]
    res = session_start.handle({"session_id": "hub00000-0001", "cwd": project, "dashboard": False})
    assert res["hub_url"] is None and len(calls) == 1


def test_new_leaves_the_first_chat_due_and_tells_the_owner_what_to_say_next(tmp_path):
    from alpaca import adopt, db
    target = tmp_path / "work" / "demo"
    rc, out = _new("--name", "Demo App", "--dir", str(target), *FLAGS)
    assert rc == 0, out
    assert "Next: cd %s && claude   (or codex)" % target in out
    assert 'Then tell it about your project: paste a git URL or a folder path, or say "start empty".' in out
    conn = db.connect(str(target))
    assert adopt.first_chat_state(conn) == "due"
    events = db.events(conn, kind="onboard")
    assert len(events) == 1 and events[0]["data"].get("provisional") is True
    assert "doctor 0 ERROR line(s)" in out


def test_doctor_warns_while_the_first_chat_is_due_and_says_nothing_for_a_project_without_the_mark(project, capsys):
    from alpaca import adopt, db
    cli.main(["init"])
    cli.main(["onboard", "--name", "d", "--who", "a:owner", "--what", "w"])
    capsys.readouterr()
    cli.main(["doctor"])
    import re
    assert not re.search(r"^\s*(OK|WARN|ERROR)\s+first-chat\b", capsys.readouterr().out, re.M)
    conn = db.connect(project)
    # a state set in the meta table alone (no event behind it) is named (op-062 t-656 fix round 3); the OK line for a
    # skip prints only when the onboard-skipped event exists
    for value, text in (("due", "WARN  first-chat"), ("confirmed", "setup confirmed but not applied"),
                        ("skipped", "marked skipped, but the record has no onboard-skipped event")):
        db.meta_set(conn, adopt.FIRST_CHAT, value)
        cli.main(["doctor"])
        out = capsys.readouterr().out
        assert text in out, (value, out)
    assert "OK    first-chat" not in out
    db.append_event(conn, session="cli", actor="owner", kind="onboard-skipped", op="op-0",
                    data={"owner_word": {"path": "terminal"}, "quote": "onboard-skip"})
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "OK    first-chat" in out and "marked skipped" not in out, out
