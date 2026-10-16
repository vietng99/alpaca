"""attach init and new --attach (AC-01, AC-18) with the pre-mortem attacks of t-647 (build plan 6.1)."""
import json
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from alpaca import cli, db, project
from alpaca.attach import AttachError, config, gitops, repo
from alpaca.tests.conftest import owner_pty
from alpaca.tests.onboardchat import chat  # noqa: F401
from alpaca.tests.attachkit import (DEFAULT_FILES, attached, git, make_studio, make_upstream,  # noqa: F401
                                    studio, upstream)


def events(root, kind):
    return [e for e in db.events(db.connect(str(root))) if e["kind"] == kind]


def test_attach_clones_into_app_ignores_it_and_writes_the_block_from_sensing(attached):
    app = attached.app
    assert (app / ".git").is_dir() and (app / "Makefile").is_file()
    block = project.load(str(attached.root))["attach"]
    assert block["repo"] == "app" and block["upstream"] == "origin" and block["base"] == "main"
    assert block["branch_prefix"] == "alpaca/" and block["url"] == attached.upstream.url
    assert [g["name"] for g in block["gates"]] == ["build", "test"]
    assert block["gates"][1]["command"] == ["make", "test"]
    assert project.load(str(attached.root))["paths"]["product_tree"] == "app"
    # ignored by the studio repository without touching its .gitignore (P2)
    assert git(attached.root, "check-ignore", "-q", "app/x", check=False) == ""
    assert subprocess.run(["git", "-C", str(attached.root), "check-ignore", "-q", "app/x"]).returncode == 0
    assert subprocess.run(["git", "-C", str(attached.root), "check-ignore", "-q", ".alpaca/attach/worktrees/x"]).returncode == 0
    assert "app" not in (attached.root / ".gitignore").read_text()
    # the event on op-0 carries the traces and no secret
    ev = events(attached.root, "attach-init")[0]
    assert ev["op"] == "op-0" and ev["data"]["traces"] == {"build": "app/Makefile", "test": "app/Makefile"}


def test_app_worktree_is_pristine_and_on_the_upstream_base(attached):
    assert git(attached.app, "status", "--porcelain", "--ignored") == ""
    assert git(attached.app, "rev-parse", "HEAD") == attached.upstream.sha()
    assert git(attached.app, "symbolic-ref", "--short", "HEAD") == "main"


def test_attach_twice_is_refused(attached, upstream):
    with pytest.raises(AttachError, match="already attached"):
        repo.attach(str(attached.root), upstream.url, repo="other")
    assert not (attached.root / "other").exists()


def test_attach_needs_an_onboarded_studio(project, upstream):
    with pytest.raises(AttachError, match="not onboarded"):
        repo.attach(str(project), upstream.url)


@pytest.mark.parametrize("url", ["--upload-pack=touch PWNED", "-oProxyCommand=x", "ext::sh -c touch% PWNED"])
def test_option_like_and_helper_sources_are_refused(studio, tmp_path, url):
    with pytest.raises(AttachError):
        repo.attach(str(studio), url)
    assert not (studio / "app").exists() and not list(tmp_path.rglob("PWNED")) and not list(studio.rglob("PWNED"))


def test_url_with_user_info_is_refused_and_the_secret_never_prints(studio, upstream, capsys):
    url = "https://deploy:SECRET@example.org/team/app.git"
    with pytest.raises(AttachError) as exc:
        repo.attach(str(studio), url)
    assert "SECRET" not in str(exc.value) and "example.org/team/app.git" in str(exc.value)
    rc = cli.main(["attach", "init", "file://u:SECRET@" + upstream.url])
    out = capsys.readouterr()
    assert rc == cli.FAIL and "SECRET" not in out.out + out.err
    for text in (json.dumps([e["data"] for e in db.events(db.connect(str(studio)))], default=str),
                 (studio / "project.yaml").read_text()):
        assert "SECRET" not in text


def test_a_local_clone_shares_nothing_with_its_source(attached):
    objects = attached.app / ".git" / "objects"
    assert not (objects / "info" / "alternates").exists()
    assert all(p.stat().st_nlink == 1 for p in objects.rglob("*") if p.is_file())
    assert git(attached.app, "rev-parse", "--is-shallow-repository") == "false"
    assert git(attached.app, "remote", "get-url", "origin") == attached.upstream.url


def test_a_source_inside_the_studio_is_refused(studio):
    inner = make_upstream(studio, "inner")
    with pytest.raises(AttachError, match="inside the studio"):
        repo.attach(str(studio), inner.url)


def test_unreachable_https_returns_without_hanging(studio):
    import time
    started = time.time()
    with pytest.raises(AttachError):
        repo.attach(str(studio), "https://127.0.0.1:9/private.git")
    assert time.time() - started < 30
    assert not (studio / "app").exists()
    assert config.raw(str(studio)) is config.MISSING


def test_repo_with_alpaca_manifest_is_refused_and_removed(studio, tmp_path):
    up = make_upstream(tmp_path, "alp", files={"ALPACA-MANIFEST": "x\n", "Makefile": "test:\n\techo\n"})
    with pytest.raises(AttachError, match="ALPACA-MANIFEST"):
        repo.attach(str(studio), up.url)
    assert not (studio / "app").exists()


def test_empty_gates_are_refused_and_nothing_is_written(studio, tmp_path):
    up = make_upstream(tmp_path, "bare", files={"notes.txt": "nothing to sense\n"})
    exclude_before = (studio / ".git" / "info" / "exclude").read_text()
    n_before = len(db.events(db.connect(str(studio))))
    with pytest.raises(AttachError, match="no attach gates"):
        repo.attach(str(studio), up.url)
    assert not (studio / "app").exists()
    assert not (studio / ".alpaca" / "attach").exists()
    assert (studio / ".git" / "info" / "exclude").read_text() == exclude_before
    assert config.raw(str(studio)) is config.MISSING
    assert len(db.events(db.connect(str(studio)))) == n_before


def test_gates_file_replaces_sensing(studio, tmp_path):
    up = make_upstream(tmp_path, "g", files={"notes.txt": "x\n"})
    gates = [{"name": "py-syntax", "command": ["python3", "-c", "print(1)"], "timeout": 120}]
    cfg = repo.attach(str(studio), up.url, gates=gates)
    assert [g["name"] for g in cfg.gates] == ["py-syntax"] and cfg.gates[0]["timeout"] == 120
    bad = [{"name": "BAD NAME", "command": ["x"]}]
    up2 = make_upstream(tmp_path, "g2")
    with pytest.raises(AttachError):
        repo.attach(str(studio), up2.url, repo="app2", gates=bad)


def test_a_gitignore_that_unignores_alpaca_state_is_refused(studio, upstream):
    (studio / ".gitignore").write_text("!.alpaca/**\n", encoding="utf-8")
    before = (studio / ".git" / "info" / "exclude").read_text()
    with pytest.raises(AttachError, match="does not ignore"):
        repo.attach(str(studio), upstream.url)
    assert (studio / ".git" / "info" / "exclude").read_text() == before
    assert not (studio / "app").exists()


def test_the_push_trap_identity_and_state_are_set(attached):
    app = attached.app
    assert git(app, "config", "--local", "remote.origin.pushurl") == repo.PUSH_TRAP
    assert git(app, "config", "--local", "user.email") == "alpaca-agent@localhost"
    assert git(app, "push", "origin", "main", check=False) == ""
    done = subprocess.run(["git", "-C", str(app), "push", "origin", "main"], capture_output=True, text=True)
    assert done.returncode != 0
    state = config.read_state(str(attached.root))
    assert state["config_digest"] == gitops.config_digest(str(app)) and state["base_sha_at_attach"] == attached.upstream.sha()
    assert state["fetch_url"] == attached.upstream.url and state["repo"] == "app"
    assert stat.S_IMODE(os.stat(config.state_dir(str(attached.root)) / "state.json").st_mode) == 0o600


def _live_hooks(app):
    folder = app / ".git" / "hooks"
    return [p.name for p in folder.iterdir() if not p.name.endswith(".sample")] if folder.is_dir() else []


def test_no_hook_is_installed_in_the_clone(attached):
    assert _live_hooks(attached.app) == []


def test_a_template_folder_in_the_environment_installs_no_hook_in_the_clone(studio, upstream, tmp_path, monkeypatch):
    tpl = tmp_path / "tpl"
    (tpl / "hooks").mkdir(parents=True)
    hook = tpl / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho leaked\n")
    hook.chmod(0o755)
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(tpl))
    # control: plain git with the same variable does copy the hook, so the test can see it
    git(tmp_path, "clone", "-q", upstream.url, str(tmp_path / "control"), env={"GIT_TEMPLATE_DIR": str(tpl)})
    assert (tmp_path / "control" / ".git" / "hooks" / "pre-commit").exists()
    repo.attach(str(studio), upstream.url)
    assert _live_hooks(studio / "app") == []


def test_answers_file_is_recorded_on_op_0(studio, upstream, tmp_path):
    answers = tmp_path / "answers.md"
    answers.write_text("goal: ship the thing\n", encoding="utf-8")
    repo.attach(str(studio), upstream.url, answers=str(answers))
    rec = events(studio, "attach-init")[0]["data"]["answers"]
    assert rec["path"] == str(answers) and rec["text"] == "goal: ship the thing\n" and len(rec["sha256"]) == 64


def test_term_file_is_written_only_when_the_barrier_names_it(studio, upstream):
    cfg = project.load(str(studio))
    cfg["barrier"] = {"terms": ".alpaca/sealed-terms.txt"}
    project.save(str(studio), cfg)
    repo.attach(str(studio), upstream.url)
    text = (studio / ".alpaca" / "sealed-terms.txt").read_text()
    assert str(studio.resolve()) in text and "re: \\bop-[0-9]{3,}\\b" in text


def test_the_base_follows_the_upstream_head_and_can_be_named(studio, tmp_path):
    up = make_upstream(tmp_path, "dev", branch="develop")
    cfg = repo.attach(str(studio), up.url)
    assert cfg.base == "develop"
    assert git(studio / "app", "symbolic-ref", "--short", "HEAD") == "develop"


def test_a_named_base_that_does_not_exist_is_refused(studio, upstream):
    with pytest.raises(AttachError, match="no branch nope"):
        repo.attach(str(studio), upstream.url, base="nope")
    assert not (studio / "app").exists()


def test_a_named_base_other_than_the_head_branch_is_checked_out(studio, upstream):
    upstream.branch_off("release", {"R.md": "r\n"})
    cfg = repo.attach(str(studio), upstream.url, base="release")
    assert cfg.base == "release" and git(studio / "app", "symbolic-ref", "--short", "HEAD") == "release"
    assert git(studio / "app", "status", "--porcelain") == ""


def test_attach_verb_prints_a_gate_line_and_json(studio, upstream, capsys):
    rc = cli.main(["attach", "init", upstream.url, "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["verdict"] == "PASS" and out["receipt"]["repo"] == "app" and out["receipt"]["gates"] == ["build", "test"]
    rc = cli.main(["attach", "init", upstream.url])
    text = capsys.readouterr().out
    assert rc == cli.FAIL and "GATE alpaca-attach-init: FAIL (this project is already attached" in text


# ---- alpaca new --attach (AC-01) -------------------------------------------------------------------

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NEW_FLAGS = ["--who", "tester:owner", "--yes", "--no-bootstrap", "--no-serve"]


def _new(*args):
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with owner_pty(), redirect_stdout(buf):       # the copy's own commands inherit standard input: the owner's terminal
        rc = cli.main(["new", *args])
    return rc, buf.getvalue()


def _verify(root):
    return subprocess.run([sys.executable, os.path.join(root, "setup", "gen_manifest.py"), "--root", str(root),
                           "--verify"], capture_output=True, text=True)


@pytest.fixture(scope="module")
def made(tmp_path_factory):
    """One `alpaca new --attach` run shared by the tests that only look at its result."""
    base = tmp_path_factory.mktemp("newattach")
    up = make_upstream(base)
    target = base / "work" / "studio"
    # a module fixture runs before the per-test cleanup of the session variables; the owner's own terminal has none
    from alpaca import ownerword
    with pytest.MonkeyPatch.context() as mp:
        for name in (*cli.ENV_SESSION_VARS, *ownerword.AGENT_MARKERS):
            mp.delenv(name, raising=False)
        mp.setattr(ownerword, "_stdin_is_tty", lambda: True)      # the autouse patch of a test does not reach a module fixture
        rc, out = _new("--name", "Demo App", "--dir", str(target), "--attach", up.url, *NEW_FLAGS)
    return {"rc": rc, "out": out, "target": target, "up": up}


def test_new_attach_makes_a_studio_with_the_clone_ignored_and_the_block_written(made):
    target = made["target"]
    assert made["rc"] == 0, made["out"]
    assert "doctor 0 ERROR line(s)" in made["out"]
    assert (target / "app" / ".git").is_dir() and (target / "app" / "Makefile").is_file()
    block = yaml.safe_load((target / "project.yaml").read_text())["attach"]
    assert block["repo"] == "app" and block["url"] == made["up"].url and [g["name"] for g in block["gates"]] == ["build", "test"]
    assert subprocess.run(["git", "-C", str(target), "check-ignore", "-q", "app/README.md"]).returncode == 0
    assert git(target / "app", "status", "--porcelain", "--ignored") == ""
    assert git(target / "app", "rev-parse", "HEAD") == made["up"].sha()


def test_new_attach_leaves_the_shipped_gitignore_and_the_manifest_verify_green(made):
    target = made["target"]
    assert (target / ".gitignore").read_bytes() == Path(REPO, "templates", "release", "gitignore").read_bytes()
    done = _verify(target)
    assert done.returncode == 0 and "VERIFY: OK" in done.stdout, done.stdout + done.stderr
    # no studio file in the clone and no clone file in the studio history
    assert not (target / "app" / "ALPACA-MANIFEST").exists()
    assert "app/" not in git(target, "ls-files")


def test_new_attach_doctor_names_the_mode_and_every_row_is_ok(made):
    done = subprocess.run([sys.executable, "-m", "alpaca", "doctor"], cwd=made["target"], capture_output=True, text=True)
    rows = {line.split()[1]: line.split()[0] for line in done.stdout.splitlines() if line.startswith(("OK", "WARN", "ERROR"))}
    for name in ("attach-config", "attach-repo", "attach-upstream", "attach-git-config", "attach-worktrees", "attach-push-checks"):
        assert rows.get(name) == "OK", done.stdout
    assert "attach mode on" in done.stdout


def test_new_attach_with_an_option_like_source_fails_before_any_clone(tmp_path):
    target = tmp_path / "studio"
    rc, out = _new("--name", "X", "--dir", str(target), "--attach=--upload-pack=touch PWNED", *NEW_FLAGS)
    assert rc == cli.FAIL and "GATE alpaca-new: FAIL" in out
    assert not (target / "app").exists() and not list(tmp_path.rglob("PWNED"))


def test_new_without_attach_writes_no_attach_key(tmp_path):
    target = tmp_path / "plain"
    rc, out = _new("--name", "Plain", "--dir", str(target), *NEW_FLAGS)
    assert rc == 0, out
    assert "attach" not in yaml.safe_load((target / "project.yaml").read_text())
    assert not (target / "app").exists() and not (target / ".alpaca" / "attach").exists()


def test_a_linked_state_folder_is_refused_and_nothing_lands_outside(studio, upstream, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (studio / ".alpaca" / "attach").symlink_to(outside)
    with pytest.raises(AttachError, match="is a link"):
        repo.attach(str(studio), upstream.url)
    assert list(outside.iterdir()) == [] and not (studio / "app").exists()


def test_a_linked_repo_folder_is_refused(studio, upstream, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (studio / "app").symlink_to(outside)
    with pytest.raises(AttachError, match="not a real folder"):
        repo.attach(str(studio), upstream.url)
    assert list(outside.iterdir()) == [] and (studio / "app").is_symlink()


def test_the_event_says_whether_the_gates_were_sensed_or_supplied(studio, tmp_path, capsys):
    up = make_upstream(tmp_path, "src")
    repo.attach(str(studio), up.url)
    assert events(studio, "attach-init")[0]["data"]["gates_source"] == "sensed from the repository files"


def test_new_attach_reads_a_relative_gates_file_from_the_callers_folder(tmp_path, monkeypatch):
    up = make_upstream(tmp_path, "plain", files={"notes.txt": "x\n"})
    caller = tmp_path / "caller"
    caller.mkdir()
    (caller / "gates.json").write_text(json.dumps([{"name": "py-syntax", "command": ["python3", "-c", "print(1)"], "timeout": 120}]))
    monkeypatch.setenv("ALPACA_CALLER_CWD", str(caller))
    rc, out = _new("--name", "G", "--dir", "studio", "--attach", up.url, "--attach-gates-file", "gates.json", *NEW_FLAGS)
    assert rc == 0, out
    studio_dir = caller / "studio"
    block = yaml.safe_load((studio_dir / "project.yaml").read_text())["attach"]
    assert [g["name"] for g in block["gates"]] == ["py-syntax"]
    data = [e["data"] for e in db.events(db.connect(str(studio_dir))) if e["kind"] == "attach-init"][0]
    assert data["gates_source"].startswith("gates file ") and "sha256" in data["gates_source"]
    assert data["traces"] == {"py-syntax": "gates-file"}


def test_new_attach_without_gates_and_nothing_to_sense_fails_and_says_how_to_fix_it(tmp_path):
    up = make_upstream(tmp_path, "plain", files={"notes.txt": "x\n"})
    target = tmp_path / "studio"
    rc, out = _new("--name", "G", "--dir", str(target), "--attach", up.url, *NEW_FLAGS)
    assert rc == cli.FAIL and "no attach gates sensed" in out and "--gates-file" in out
    assert not (target / "app").exists()


def test_a_plain_onboarding_writes_no_attach_key(project):
    assert cli.main(["init"]) == 0
    assert cli.main(["onboard", "--name", "d", "--who", "a:owner", "--what", "w"]) == 0
    with open(os.path.join(project, "project.yaml"), encoding="utf-8") as fh:
        assert "attach" not in yaml.safe_load(fh)


# ---- round 1 of the adversarial check: nothing lands outside the studio, the identity and the clone stay clean -----------

def _snap(root):
    """Files of the studio (not .git, not the record), the exclude file and project.yaml."""
    root = Path(root)
    skip = ("alpaca.db", "alpaca.db-wal", "alpaca.db-shm")
    files = sorted(str(p.relative_to(root)) for p in root.rglob("*")
                   if not str(p.relative_to(root)).startswith(".git/") and p.name not in skip)
    excl = root / ".git" / "info" / "exclude"
    return files, excl.read_text() if excl.exists() else None, (root / "project.yaml").read_text()


def test_an_exclude_file_that_is_a_link_is_not_written_through(studio, upstream, tmp_path):
    outside = tmp_path / "outside-exclude"
    outside.write_text("# outside\n")
    excl = studio / ".git" / "info" / "exclude"
    excl.parent.mkdir(parents=True, exist_ok=True)
    if excl.exists():
        excl.unlink()
    excl.symlink_to(outside)
    before = _snap(studio)
    with pytest.raises(AttachError, match="is a link"):
        repo.attach(str(studio), upstream.url)
    assert outside.read_text() == "# outside\n" and excl.is_symlink()
    assert _snap(studio) == before and events(studio, "attach-init") == []


def test_an_info_folder_that_is_a_link_is_not_written_through(studio, upstream, tmp_path):
    outside = tmp_path / "outside-info"
    outside.mkdir()
    info = studio / ".git" / "info"
    shutil.rmtree(info, ignore_errors=True)
    info.symlink_to(outside)
    with pytest.raises(AttachError, match="is a link"):
        repo.attach(str(studio), upstream.url)
    assert list(outside.iterdir()) == [] and not (studio / "app").exists()


def test_a_dangling_exclude_link_is_not_followed_to_create_a_file(studio, upstream, tmp_path):
    target = tmp_path / "does-not-exist-yet"
    excl = studio / ".git" / "info" / "exclude"
    excl.parent.mkdir(parents=True, exist_ok=True)
    if excl.exists():
        excl.unlink()
    excl.symlink_to(target)
    with pytest.raises(AttachError, match="is a link"):
        repo.attach(str(studio), upstream.url)
    assert not target.exists()


def test_the_rollback_does_not_write_through_an_exclude_link_made_during_the_attach(studio, upstream, tmp_path, monkeypatch):
    """The clone step fails after the exclude lines were written; a link now stands where the file was."""
    outside = tmp_path / "outside-late"
    outside.write_text("# outside\n")

    def fail_after_swap(*a, **k):
        excl = studio / ".git" / "info" / "exclude"
        excl.unlink()
        excl.symlink_to(outside)
        raise AttachError("simulated clone failure")
    monkeypatch.setattr(repo, "clone", fail_after_swap)
    with pytest.raises(AttachError, match="simulated clone failure"):
        repo.attach(str(studio), upstream.url)
    assert outside.read_text() == "# outside\n"


def test_terms_below_a_linked_folder_are_not_written_outside(studio, upstream, tmp_path):
    outside = tmp_path / "outside-terms"
    outside.mkdir()
    (studio / "seal").symlink_to(outside)
    cfg = project.load(str(studio))
    cfg["barrier"] = {"terms": "seal/terms.txt"}
    project.save(str(studio), cfg)
    with pytest.raises(AttachError, match="barrier.terms"):
        repo.attach(str(studio), upstream.url)
    assert list(outside.iterdir()) == [] and not (studio / "app").exists()
    assert config.raw(str(studio)) is config.MISSING and not (studio / ".alpaca" / "attach" / "state.json").exists()


def test_a_terms_file_that_is_a_dangling_link_is_refused_and_not_created_through_the_link(studio, upstream, tmp_path):
    target = tmp_path / "terms-target"
    (studio / ".alpaca").mkdir(exist_ok=True)
    (studio / ".alpaca" / "sealed-terms.txt").symlink_to(target)
    cfg = project.load(str(studio))
    cfg["barrier"] = {"terms": ".alpaca/sealed-terms.txt"}
    project.save(str(studio), cfg)
    with pytest.raises(AttachError, match="barrier.terms"):
        repo.attach(str(studio), upstream.url)
    assert not target.exists() and not (studio / "app").exists()


def test_a_terms_file_inside_the_clone_is_refused(studio, upstream):
    cfg = project.load(str(studio))
    cfg["barrier"] = {"terms": "app/terms.txt"}
    project.save(str(studio), cfg)
    with pytest.raises(AttachError, match="inside the attached clone"):
        repo.attach(str(studio), upstream.url)
    assert not (studio / "app").exists()


def test_a_studio_that_is_a_linked_worktree_is_refused_and_left_as_it_was(tmp_path, monkeypatch, upstream):
    from alpaca.tests.attachkit import make_linked_worktree_studio
    root = make_linked_worktree_studio(tmp_path, monkeypatch)
    before = _snap(root)
    with pytest.raises(AttachError, match="linked git worktree"):
        repo.attach(str(root), upstream.url)
    assert _snap(root) == before and not (root / "app").exists()
    # the exclude file of the common git folder, outside the studio, was not touched either
    common = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    excl = common / "info" / "exclude"
    assert not excl.exists() or "/app/" not in excl.read_text()


def test_a_studio_inside_another_repository_is_refused(project, upstream):
    root = make_studio(project, git_init=False)
    git(Path(root).parent, "init", "-q", "-b", "main")
    assert gitops.studio_kind(str(root)) == "borrowed"
    with pytest.raises(AttachError, match="inside another repository"):
        repo.attach(str(root), upstream.url)
    assert not (root / "app").exists()


def test_a_studio_with_no_git_around_it_still_attaches_and_has_nothing_to_ignore(project, upstream):
    root = make_studio(project, git_init=False)
    assert gitops.studio_kind(str(root)) == "none"
    repo.attach(str(root), upstream.url)
    assert (root / "app" / ".git").is_dir() and not (root / ".git").exists()


@pytest.mark.parametrize("name", ["input", "openspec", "Input", "specs", "notes"])
def test_a_folder_alpaca_writes_into_is_not_a_clone_target(studio, upstream, name):
    before = _snap(studio)
    with pytest.raises(AttachError, match="owns or writes into"):
        repo.attach(str(studio), upstream.url, repo=name)
    assert _snap(studio) == before


def test_the_attach_verb_refuses_input_as_the_clone_folder(studio, upstream, capsys):
    os.chdir(studio)
    rc = cli.main(["attach", "init", upstream.url, "--repo", "input"])
    assert rc == cli.FAIL and not (studio / "input").exists()
    assert "GATE alpaca-attach-init: FAIL" in capsys.readouterr().out


def test_an_alpaca_note_does_not_land_in_the_clone(attached):
    os.chdir(attached.studio)
    assert cli.main(["note", "add", "a raw note"]) == 0
    assert git(attached.app, "status", "--porcelain", "--untracked-files=all") == ""


def test_the_folder_is_claimed_before_anything_is_written(studio, upstream, monkeypatch):
    """Another run creates app/ between this run's existence check and its claim: this run refuses and
    leaves the other run's folder and the exclude file alone."""
    before = _snap(studio)
    real = gitops.studio_kind

    def other_run_wins(path):
        kind = real(path)
        (studio / "app").mkdir(exist_ok=True)
        (studio / "app" / "other-run").write_text("x\n")
        return kind
    monkeypatch.setattr(gitops, "studio_kind", other_run_wins)
    with pytest.raises(AttachError, match="already exists"):
        repo.attach(str(studio), upstream.url)
    assert (studio / "app" / "other-run").read_text() == "x\n"
    assert _snap(studio)[1] == before[1]
    assert not (studio / ".alpaca" / "attach" / "state.json").exists()


def _attach_in_process(root, url, queue):
    os.chdir(root)
    try:
        repo.attach(root, url)
        queue.put("ok")
    except AttachError as exc:
        queue.put("refused: %s" % exc)
    except Exception as exc:                            # noqa: BLE001 - reported to the parent
        queue.put("crash: %r" % exc)


def test_two_attach_runs_started_together_leave_one_consistent_studio(studio, tmp_path):
    import multiprocessing
    ups = [make_upstream(tmp_path, "race-one"),
           make_upstream(tmp_path, "race-two", files={"Makefile": "test:\n\ttrue\n", "TWO": "2\n"})]
    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    procs = [ctx.Process(target=_attach_in_process, args=(str(studio), u.url, queue)) for u in ups]
    for p in procs:
        p.start()
    for p in procs:
        p.join(180)
    results = sorted(queue.get(timeout=10) for _ in procs)
    assert not [r for r in results if r.startswith("crash")], results
    assert len(events(studio, "attach-init")) <= 1, results
    block = config.raw(str(studio))
    if block is not config.MISSING:
        assert (studio / "app" / ".git").is_dir(), results
        assert git(studio / "app", "remote", "get-url", "origin") == block["url"], results
        assert (studio / ".alpaca" / "attach" / "state.json").exists(), results
        assert "/app/" in (studio / ".git" / "info" / "exclude").read_text(), results
    assert any(r == "ok" for r in results) == (block is not config.MISSING), results


# ---- round 2 of the adversarial check: one attach at a time, a late failure undoes only its own writes -----------------

def _excl(root):
    p = Path(root) / ".git" / "info" / "exclude"
    return p.read_text() if p.exists() else ""


def _doctor_rows(root):
    from alpaca import doctor
    return {r["name"]: r for r in doctor.checks(str(root))}


def test_a_held_studio_lock_refuses_the_attach_and_leaves_the_studio_alone(studio, upstream):
    import fcntl
    before = _snap(studio)
    fd = os.open(str(studio / ".alpaca"), os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(AttachError, match="another alpaca attach init is running"):
            repo.attach(str(studio), upstream.url)
    finally:
        os.close(fd)
    assert _snap(studio) == before and not (studio / "app").exists()
    repo.attach(str(studio), upstream.url)                    # the lock is gone with the holder
    assert config.raw(str(studio)) is not config.MISSING


def test_the_lock_dies_with_a_killed_holder(studio, upstream):
    """A holder killed with SIGKILL cannot run a clean-up; the kernel drops its lock, so no stale lock stays."""
    import signal
    import time
    code = ("import fcntl, os, sys\nfd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\nprint('held', flush=True)\ntime.sleep(600)\n")
    holder = subprocess.Popen([sys.executable, "-c", "import time\n" + code, str(studio / ".alpaca")],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(AttachError, match="another alpaca attach init is running"):
            repo.attach(str(studio), upstream.url)
        holder.send_signal(signal.SIGKILL)
        holder.wait(30)
        repo.attach(str(studio), upstream.url)
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.wait()
    assert config.raw(str(studio)) is not config.MISSING


def test_the_lock_leaves_no_file_behind_and_is_released_after_a_refusal(studio, tmp_path):
    bare = make_upstream(tmp_path, "nogate", files={"README.md": "no gate file here\n"})
    before = _snap(studio)
    with pytest.raises(AttachError, match="no attach gates"):
        repo.attach(str(studio), bare.url)
    assert _snap(studio) == before
    with pytest.raises(AttachError, match="no attach gates"):                # a second run is not blocked by the first
        repo.attach(str(studio), bare.url)


def test_a_second_run_with_another_repo_name_inside_the_first_is_refused_and_the_first_finishes(studio, tmp_path, monkeypatch):
    """B (--repo app) starts inside A's sensing (--repo web). B is refused at the lock; A attaches; there is
    one attach-init event and the only clone is the one project.yaml names."""
    from alpaca.attach import sense
    up_b, up_a = make_upstream(tmp_path, "bee"), make_upstream(tmp_path, "aye")
    real, refused = sense.sense_gates, []

    def interleave(repo_dir, repo_name):
        if repo_name == "web" and not refused:
            try:
                repo.attach(str(studio), up_b.url)
            except AttachError as exc:
                refused.append(str(exc))
        return real(repo_dir, repo_name)

    monkeypatch.setattr(sense, "sense_gates", interleave)
    repo.attach(str(studio), up_a.url, repo="web")
    assert len(refused) == 1 and "another alpaca attach init is running" in refused[0], refused
    block = config.raw(str(studio))
    assert block["repo"] == "web" and len(events(studio, "attach-init")) == 1
    assert sorted(p.name for p in Path(studio).iterdir() if (p / ".git").is_dir()) == ["web"]
    assert "/web/" in _excl(studio) and "/app/" not in _excl(studio)
    assert _doctor_rows(studio)["attach-repo"]["level"] == "ok"


def test_a_refused_first_run_does_not_undo_anything_of_a_second_run_that_was_refused_at_the_lock(studio, tmp_path, monkeypatch):
    """A (--repo web, an upstream with no gate file) is refused late. B (--repo app) starts inside A's sensing
    and is refused at the lock. Afterwards the studio is as it was before A started."""
    from alpaca.attach import sense
    up_b = make_upstream(tmp_path, "bee")
    up_a = make_upstream(tmp_path, "aye", files={"README.md": "no gate file here\n"})
    before = _snap(studio)
    real, refused = sense.sense_gates, []

    def interleave(repo_dir, repo_name):
        if not refused:
            try:
                repo.attach(str(studio), up_b.url)
            except AttachError as exc:
                refused.append(str(exc))
        return real(repo_dir, repo_name)

    monkeypatch.setattr(sense, "sense_gates", interleave)
    with pytest.raises(AttachError, match="no attach gates"):
        repo.attach(str(studio), up_a.url, repo="web")
    assert refused and "another alpaca attach init is running" in refused[0]
    assert _snap(studio) == before and not (studio / "web").exists() and not (studio / "app").exists()


def _attach_named(root, url, name, queue):
    os.chdir(root)
    try:
        repo.attach(root, url, repo=name)
        queue.put("ok")
    except AttachError as exc:
        queue.put("refused: %s" % exc)
    except Exception as exc:                            # noqa: BLE001 - reported to the parent
        queue.put("crash: %r" % exc)


@pytest.mark.parametrize("round_", range(3))
def test_two_processes_with_different_repo_names_leave_one_consistent_studio(studio, tmp_path, round_):
    import multiprocessing
    ups = [make_upstream(tmp_path, "pa"), make_upstream(tmp_path, "pb", files={"Makefile": "test:\n\ttrue\n", "TWO": "2\n"})]
    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    procs = [ctx.Process(target=_attach_named, args=(str(studio), u.url, n, queue)) for u, n in zip(ups, ("app", "web"))]
    for p in procs:
        p.start()
    for p in procs:
        p.join(180)
    results = sorted(queue.get(timeout=10) for _ in procs)
    assert not [r for r in results if r.startswith("crash")], results
    assert len(events(studio, "attach-init")) == 1, results
    block = config.raw(str(studio))
    assert results.count("ok") == 1 and sum(r.startswith("refused") for r in results) == 1, results
    assert [r for r in results if r.startswith("refused")][0].count("running") + \
        [r for r in results if r.startswith("refused")][0].count("already") >= 1, results
    assert "gitignore" not in " ".join(results)
    assert sorted(p.name for p in Path(studio).iterdir() if (p / ".git").is_dir()) == [block["repo"]], results
    assert "/%s/" % block["repo"] in _excl(studio)
    assert (studio / ".alpaca" / "attach" / "state.json").exists()
    assert _doctor_rows(studio)["attach-repo"]["level"] == "ok"


def test_an_attach_that_fails_late_removes_the_terms_file_and_its_folders(studio, upstream, monkeypatch):
    doc = project.load(str(studio))
    doc["barrier"] = dict(doc.get("barrier") or {}, terms="seal/deep/terms.txt")
    project.save(str(studio), doc)
    before = _snap(studio)
    for stop in (RuntimeError("record write failed"), KeyboardInterrupt()):
        def boom(*a, _stop=stop, **k):
            raise _stop
        monkeypatch.setattr(repo, "_commit", boom)
        with pytest.raises(type(stop)):
            repo.attach(str(studio), upstream.url)
        assert _snap(studio) == before, sorted(set(_snap(studio)[0]) ^ set(before[0]))
    assert not (studio / "seal").exists()


def test_a_terms_folder_that_was_there_before_stays(studio, upstream, monkeypatch):
    (studio / "seal").mkdir()
    doc = project.load(str(studio))
    doc["barrier"] = dict(doc.get("barrier") or {}, terms="seal/terms.txt")
    project.save(str(studio), doc)
    monkeypatch.setattr(repo, "_commit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        repo.attach(str(studio), upstream.url)
    assert (studio / "seal").is_dir() and not (studio / "seal" / "terms.txt").exists()


def test_a_refused_run_keeps_a_state_file_it_did_not_write(studio, tmp_path):
    sd = studio / ".alpaca" / "attach"
    sd.mkdir(parents=True)
    (sd / "state.json").write_text('{"earlier": true}\n')
    up = make_upstream(tmp_path, "nogates", files={"README.md": "x\n"})
    with pytest.raises(AttachError):
        repo.attach(str(studio), up.url)
    assert (sd / "state.json").read_text() == '{"earlier": true}\n'


def test_a_failing_run_puts_an_earlier_state_file_back_as_it_was(studio, upstream, monkeypatch):
    sd = studio / ".alpaca" / "attach"
    sd.mkdir(parents=True)
    (sd / "state.json").write_text('{"earlier": true}\n')
    monkeypatch.setattr(repo, "_commit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        repo.attach(str(studio), upstream.url)
    assert (sd / "state.json").read_text() == '{"earlier": true}\n'
    assert sorted(p.name for p in sd.iterdir()) == ["state.json"]


def test_the_rollback_does_not_write_through_a_link_planted_at_a_guessable_temp_name(studio, upstream, tmp_path, monkeypatch):
    """R1 (check-adversarial-r3.md): the rollback put state.json back through a temp file named
    state.json.alpaca-<pid>.tmp. A link planted at that name carried the earlier bytes out of the studio."""
    sd = studio / ".alpaca" / "attach"
    sd.mkdir(parents=True)
    (sd / "state.json").write_text('{"earlier": true}\n')
    outside = tmp_path / "outside-r1.txt"
    outside.write_text("outside\n")
    for guess in ("state.json.alpaca-%d.tmp" % os.getpid(), "state.json.tmp", ".tmp-state.json"):
        os.symlink(outside, sd / guess)
    monkeypatch.setattr(repo, "_commit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("late")))
    with pytest.raises(RuntimeError):
        repo.attach(str(studio), upstream.url)
    state = sd / "state.json"
    assert outside.read_text() == "outside\n"
    assert not state.is_symlink() and state.read_text() == '{"earlier": true}\n'
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    assert all(p.is_symlink() for p in sd.iterdir() if p.name != "state.json")
    assert not [p for p in sd.iterdir() if p.name.startswith(".tmp-") and not p.is_symlink()]


def test_an_exclude_temp_name_that_is_a_planted_link_does_not_stop_the_attach(studio, upstream, tmp_path):
    info = studio / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside-excl.txt"
    outside.write_text("outside\n")
    os.symlink(outside, info / ("exclude.alpaca-%d.tmp" % os.getpid()))
    repo.attach(str(studio), upstream.url)
    assert outside.read_text() == "outside\n"
    assert "/app/" in (info / "exclude").read_text()


def test_a_state_file_that_is_a_link_is_refused(studio, upstream, tmp_path):
    sd = studio / ".alpaca" / "attach"
    sd.mkdir(parents=True)
    outside = tmp_path / "outside-state"
    outside.write_text("keep\n")
    os.symlink(outside, sd / "state.json")
    with pytest.raises(AttachError, match="state.json is a link"):
        repo.attach(str(studio), upstream.url)
    assert outside.read_text() == "keep\n" and not (studio / "app").exists()


def test_an_exclude_file_hard_linked_to_a_file_outside_is_not_written_through(studio, upstream, tmp_path):
    outside = tmp_path / "outside-exclude"
    outside.write_text("# outside\n")
    excl = studio / ".git" / "info" / "exclude"
    excl.parent.mkdir(parents=True, exist_ok=True)
    if excl.exists():
        excl.unlink()
    os.link(outside, excl)
    repo.attach(str(studio), upstream.url)
    assert outside.read_text() == "# outside\n"
    assert "/app/" in excl.read_text() and "# outside" in excl.read_text()
    assert not list((studio / ".git" / "info").glob("*.tmp"))


def test_a_clone_with_an_alpaca_manifest_in_a_subfolder_is_refused_and_removed(studio, tmp_path):
    up = make_upstream(tmp_path, "nest", files={"Makefile": "test:\n\ttrue\n", "tools/kit/ALPACA-MANIFEST": "# vendored\n"})
    before = _snap(studio)
    with pytest.raises(AttachError, match=r"ALPACA-MANIFEST at tools/kit/"):
        repo.attach(str(studio), up.url)
    assert not (studio / "app").exists() and _snap(studio) == before


@pytest.mark.parametrize("env", [{}, {"GIT_LITERAL_PATHSPECS": "1"}, {"GIT_NOGLOB_PATHSPECS": "1"}])
def test_the_nested_manifest_check_does_not_depend_on_pathspec_variables(attached, monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    sub = attached.app / "a" / "b"
    sub.mkdir(parents=True)
    (sub / "ALPACA-MANIFEST").write_text("x\n")
    git(attached.app, "add", "-A")
    git(attached.app, "commit", "-q", "-m", "vendor a kit")
    problems = gitops.clone_problems(str(attached.app), "app/")
    assert any("ALPACA-MANIFEST at a/b/" in p for p in problems), problems


@pytest.mark.parametrize("where", ["xdg-attributes", "home-attributes"])
def test_a_global_attributes_file_does_not_change_the_checkout(studio, upstream, tmp_path, monkeypatch, where):
    base = tmp_path / ("xdg" if where == "xdg-attributes" else "home" / Path(".config"))
    (base / "git").mkdir(parents=True)
    (base / "git" / "attributes").write_text("* text eol=crlf\n")
    if where == "xdg-attributes":
        monkeypatch.setenv("XDG_CONFIG_HOME", str(base))
    else:
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    repo.attach(str(studio), upstream.url)
    want = subprocess.run(["git", "-C", str(upstream.bare), "cat-file", "blob", "main:Makefile"], capture_output=True,
                          check=True, env={"PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1",
                                           "GIT_CONFIG_GLOBAL": os.devnull, "HOME": "/nonexistent"}).stdout
    assert (studio / "app" / "Makefile").read_bytes() == want and b"\r" not in want


def _new_proc(tmp_path, *extra, env_extra=None):
    env = {k: v for k, v in os.environ.items()
           if k not in ("ALPACA_ROOT", "CLAUDE_PROJECT_DIR", "ALPACA_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDECODE",
                        "CODEX_THREAD_ID", "CODEX_SANDBOX")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", PYTHONPATH=REPO,
               ALPACA_WORKSPACES=str(tmp_path / "ws.json"), ALPACA_RECORD_SKIP_ROOT=REPO,
               GIT_AUTHOR_NAME="a", GIT_AUTHOR_EMAIL="a@x", GIT_COMMITTER_NAME="a", GIT_COMMITTER_EMAIL="a@x")
    env.update(env_extra or {})
    # the owner's terminal: standard input is a pseudo-terminal (a child with no terminal is not the owner's, ownerword.owner_terminal)
    master, slave = os.openpty()
    try:
        return subprocess.run([sys.executable, "-m", "alpaca", "new", "--name", "studio-x", "--dir", str(tmp_path / "sx"),
                               "--yes", "--no-bootstrap", "--no-serve", "--who", "ada:owner", "--what", "w", *extra],
                              cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=600, stdin=slave)
    finally:
        os.close(master)
        os.close(slave)


def test_new_attach_under_an_inherited_git_dir_still_gives_the_studio_its_own_git_and_ignores_app(tmp_path):
    up = make_upstream(tmp_path, "up")
    caller = tmp_path / "caller"
    caller.mkdir()
    git(caller, "init", "-q")
    before = sorted(os.listdir(caller / ".git"))
    res = _new_proc(tmp_path, "--attach", up.url, env_extra={"GIT_DIR": str(caller / ".git")})
    sx = tmp_path / "sx"
    detail = (res.stdout + res.stderr).strip().splitlines()[-4:]
    assert res.returncode == 0, detail
    assert (sx / ".git").is_dir() and "/app/" in _excl(sx), detail
    assert sorted(os.listdir(caller / ".git")) == before


def test_new_attach_without_git_in_the_studio_is_refused_with_the_reason(tmp_path):
    up = make_upstream(tmp_path, "up")
    res = _new_proc(tmp_path, "--attach", up.url, "--no-git")
    assert res.returncode != 0 and "needs a studio with a git repository of its own" in res.stdout, res.stdout[-400:]
    assert not (tmp_path / "sx" / "app").exists()


@pytest.mark.parametrize("case", ["info-is-a-file", "terms-parent-is-a-file"])
def test_a_studio_shape_attach_cannot_use_ends_as_a_refusal_with_the_gate_line_and_json(studio, upstream, capsys, case):
    if case == "info-is-a-file":
        info = studio / ".git" / "info"
        if info.is_dir():
            for p in info.iterdir():
                p.unlink()
            info.rmdir()
        info.write_text("not a folder\n")
    else:
        (studio / "seal").write_text("a plain file\n")
        doc = project.load(str(studio))
        doc["barrier"] = dict(doc.get("barrier") or {}, terms="seal/terms.txt")
        project.save(str(studio), doc)
    before = _snap(studio)
    capsys.readouterr()
    code = cli.main(["attach", "init", upstream.url, "--json"])
    out = capsys.readouterr()
    parsed = json.loads(out.out.strip().splitlines()[-1])
    assert code == 1 and parsed.get("verdict") == "FAIL", (code, out.out[-200:], out.err[-300:])
    assert _snap(studio) == before and not (studio / "app").exists()



# ---- fix round 4 (check-adversarial-r4.md S1 to S4): option-like source, unbounded answers file, unreadable files

NOT_ROOT = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads a mode 000 file")


@pytest.mark.parametrize("source", ["-h", "--help", " -h"])
def test_new_attach_with_an_option_like_source_is_refused_before_anything_is_copied(tmp_path, source):
    """S1: `new --attach=-h` ran `attach init -h`, whose parser printed help and exited 0, and `new` said ok."""
    res = _new_proc(tmp_path, "--attach=%s" % source)
    assert res.returncode != 0, (res.stdout + res.stderr)[-400:]
    assert "starts with '-'" in res.stdout and "alpaca new: ok" not in res.stdout, res.stdout[-400:]
    assert not (tmp_path / "sx").exists()


def test_the_child_command_new_builds_carries_the_source_behind_a_double_dash(tmp_path, monkeypatch):
    """S1, second guard: even when the early refusal is not reached, the source is last and behind `--`."""
    from alpaca import new as new_mod
    calls = []

    def fake_run(args, cwd, env, label, check=True):
        calls.append((label, list(args)))
        return "" if "doctor" in label else "x"

    monkeypatch.setattr(new_mod, "_run", fake_run)
    monkeypatch.setattr(new_mod, "copy_tree", lambda src, target: os.makedirs(target, exist_ok=True) or 0)
    monkeypatch.setattr(new_mod, "apply_release_overlays", lambda src, target: [])
    monkeypatch.setattr(new_mod, "fill_readme", lambda target, name: True)
    monkeypatch.setattr(new_mod, "_gen_manifest", lambda target: type("G", (), {"write": lambda self, t: 0})())
    monkeypatch.setattr("alpaca.attach.gitops.studio_kind", lambda target: "own")
    monkeypatch.setattr(new_mod, "hub_url", lambda *a, **k: None)
    rc = cli.main(["new", "--name", "x", "--dir", str(tmp_path / "t"), "--who", "a:owner", "--yes", "--no-bootstrap",
                   "--no-serve", "--attach", "git@example.org:o/r.git", "--attach-base=-x"])
    argv = [a for label, a in calls if label.startswith("attach")][0]
    assert rc == 0 and argv[-2:] == ["--", "git@example.org:o/r.git"] and "--base=-x" in argv, argv


def test_attach_init_reads_a_source_after_a_double_dash_as_a_source_and_refuses_it(studio, capsys):
    capsys.readouterr()
    code = cli.main(["attach", "init", "--json", "--", "-h"])
    out = capsys.readouterr().out
    assert code == 1 and json.loads(out.strip().splitlines()[-1])["verdict"] == "FAIL" and "starts with '-'" in out
    assert not (studio / "app").exists()


def test_an_answers_file_with_no_end_is_refused_by_its_size_limit_without_filling_memory(studio, upstream):
    """S2: path.read_bytes() on /dev/zero grew until MemoryError (under the limit set here, or the OOM killer without it)."""
    import resource

    def limit():
        resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
    env = dict(os.environ, PYTHONPATH=REPO)
    done = subprocess.run([sys.executable, "-m", "alpaca", "attach", "init", upstream.url, "--answers", "/dev/zero"],
                          cwd=str(studio), env=env, capture_output=True, text=True, timeout=300, preexec_fn=limit)
    text = done.stdout + done.stderr
    # a device is not a regular file: it is refused before a byte is read (t-656; it was the size limit before)
    assert "not a regular file" in text and "Traceback" not in text and "MemoryError" not in text, text[-300:]
    assert not (studio / "app").exists()


def test_an_answers_file_exactly_at_the_limit_is_read_and_one_byte_over_is_refused(tmp_path):
    ok, big = tmp_path / "ok.txt", tmp_path / "big.txt"
    ok.write_bytes(b"a" * repo.MAX_ANSWERS_BYTES)
    big.write_bytes(b"a" * (repo.MAX_ANSWERS_BYTES + 1))
    assert len(repo._read_answers(str(ok))["text"]) == repo.MAX_ANSWERS_BYTES
    with pytest.raises(AttachError, match="larger than"):
        repo._read_answers(str(big))


@NOT_ROOT
def test_a_state_file_that_cannot_be_read_refuses_the_attach_and_stays_as_it_was(studio, upstream):
    """S4: the file was read as 'absent' and the rollback of a later failure deleted it."""
    sd = studio / ".alpaca" / "attach"
    sd.mkdir(parents=True)
    st = sd / "state.json"
    st.write_text('{"earlier": true}\n')
    os.chmod(st, 0)
    try:
        with pytest.raises(AttachError, match="cannot be read"):
            repo.attach(str(studio), upstream.url)
        assert os.path.lexists(st) and not (studio / "app").exists()
    finally:
        os.chmod(st, 0o600)
    assert st.read_text() == '{"earlier": true}\n'


@NOT_ROOT
def test_an_exclude_file_that_cannot_be_read_refuses_the_attach_and_keeps_the_owners_lines(studio, upstream):
    """S4 sibling: a mode 000 .git/info/exclude was read as 'absent', rewritten with mode 000 and then deleted by the rollback."""
    ex = studio / ".git" / "info" / "exclude"
    ex.parent.mkdir(parents=True, exist_ok=True)
    ex.write_text("/secret-local/\n")
    os.chmod(ex, 0)
    try:
        with pytest.raises(AttachError, match="exclude exists but cannot be read"):
            repo.attach(str(studio), upstream.url)
        assert os.path.lexists(ex) and not (studio / "app").exists()
    finally:
        os.chmod(ex, 0o644)
    assert ex.read_text() == "/secret-local/\n"


@NOT_ROOT
def test_ensure_ignored_alone_also_refuses_an_unreadable_exclude_file(studio):
    ex = studio / ".git" / "info" / "exclude"
    ex.write_text("/secret-local/\n")
    os.chmod(ex, 0)
    try:
        with pytest.raises(AttachError, match="cannot be read"):
            repo.ensure_ignored(str(studio), "app")
    finally:
        os.chmod(ex, 0o644)
    assert ex.read_text() == "/secret-local/\n"


def test_a_missing_exclude_file_is_put_back_by_deleting_it_and_a_present_one_by_its_bytes(studio, upstream, monkeypatch):
    ex = studio / ".git" / "info" / "exclude"
    if ex.exists():
        ex.unlink()
    monkeypatch.setattr(repo, "_commit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("late")))
    with pytest.raises(RuntimeError):
        repo.attach(str(studio), upstream.url)
    assert not ex.exists()
    ex.write_text("/mine/\n")
    with pytest.raises(RuntimeError):
        repo.attach(str(studio), upstream.url)
    assert ex.read_text() == "/mine/\n" and not (studio / "app").exists()


# ---- fix round 6: T1 (a failed record COMMIT), T2 (an empty source), T3 (a failure inside the ignore proof) ----

class _CommitFails:
    """A record connection whose COMMIT fails the way a full disk makes it fail: the error comes out of
    COMMIT itself, after project.yaml was already replaced inside the transaction."""

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)

    def execute(self, sql, *args, **kw):
        if str(sql).strip().upper() == "COMMIT":
            raise sqlite3.OperationalError("disk I/O error")
        return self._conn.execute(sql, *args, **kw)


def test_a_record_commit_that_fails_puts_project_yaml_back_and_a_second_attach_works(studio, upstream, monkeypatch):
    """T1 (check-adversarial-r5.md): project.save runs inside the transaction, before
    the COMMIT. A failed COMMIT rolled the record back and undo() removed app/ and state.json, but left
    attach: in project.yaml, so attach init said 'already attached' and doctor said 'run attach init again'."""
    yaml_before = (studio / "project.yaml").read_bytes()
    real = db.connect
    monkeypatch.setattr(db, "connect", lambda root, *a, **k: _CommitFails(real(root, *a, **k)))
    with pytest.raises(AttachError, match="record"):
        repo.attach(str(studio), upstream.url)
    monkeypatch.setattr(db, "connect", real)
    assert (studio / "project.yaml").read_bytes() == yaml_before
    assert "attach" not in yaml.safe_load((studio / "project.yaml").read_text())
    assert not (studio / "app").exists() and not (studio / ".alpaca" / "attach").exists()
    assert events(studio, "attach-init") == []
    cfg = repo.attach(str(studio), upstream.url)                    # the studio is not stuck
    assert cfg.repo == "app" and len(events(studio, "attach-init")) == 1


def test_a_record_commit_that_fails_in_the_verb_prints_a_refusal_not_an_internal_error(studio, upstream, monkeypatch, capsys):
    real = db.connect
    monkeypatch.setattr(db, "connect", lambda root, *a, **k: _CommitFails(real(root, *a, **k)))
    capsys.readouterr()
    code = cli.main(["attach", "init", "--json", upstream.url])
    out = capsys.readouterr().out
    assert code == 1 and "internal" not in out and json.loads(out.strip().splitlines()[-1])["verdict"] == "FAIL", out


def test_a_failure_after_the_record_is_written_but_before_it_is_committed_also_puts_project_yaml_back(studio, upstream, monkeypatch):
    yaml_before = (studio / "project.yaml").read_bytes()
    real_save = project.save

    def save_then_die(root, doc):
        real_save(root, doc)
        raise KeyboardInterrupt()

    monkeypatch.setattr(project, "save", save_then_die)
    with pytest.raises(KeyboardInterrupt):
        repo.attach(str(studio), upstream.url)
    assert (studio / "project.yaml").read_bytes() == yaml_before
    assert events(studio, "attach-init") == [] and not (studio / "app").exists()


@pytest.mark.parametrize("form", ["--attach=", "--attach= ", "--attach=\t"])
def test_new_attach_with_an_empty_or_blank_source_is_refused_before_any_copy(tmp_path, form):
    """T2: `--attach "$URL"` with URL unset gives an empty value; it is falsy, so every `if args.attach`
    in cmd_new skipped the attach and `new` printed ok with a plain studio."""
    target = tmp_path / "studio"
    rc, out = _new("--name", "X", "--dir", str(target), form, *NEW_FLAGS)
    assert rc == cli.FAIL and "GATE alpaca-new: FAIL" in out and "source is empty" in out, out
    assert "alpaca new: ok" not in out and not target.exists()


@pytest.mark.parametrize("opt", ["--attach-base=", "--attach-base= ", "--attach-gates-file=", "--attach-gates-file= "])
def test_new_attach_options_with_an_empty_value_are_refused_not_dropped(tmp_path, opt):
    """Siblings of T2: an empty --attach-base or --attach-gates-file was dropped by the same truth test."""
    target = tmp_path / "studio"
    up = make_upstream(tmp_path)
    rc, out = _new("--name", "X", "--dir", str(target), "--attach", up.url, opt, *NEW_FLAGS)
    assert rc == cli.FAIL and "alpaca new: ok" not in out and not target.exists(), out


@pytest.mark.parametrize("opt", ["--attach-base=main", "--attach-gates-file=g.json"])
def test_new_attach_options_without_attach_are_refused_not_ignored(tmp_path, opt):
    target = tmp_path / "studio"
    rc, out = _new("--name", "X", "--dir", str(target), opt, *NEW_FLAGS)
    assert rc == cli.FAIL and "need --attach SOURCE" in out and not target.exists(), out


@pytest.mark.parametrize("field", ["base", "repo", "upstream", "branch_prefix"])
@pytest.mark.parametrize("value", ["", "  "])
def test_attach_init_refuses_an_empty_option_instead_of_taking_the_default(studio, upstream, field, value):
    """Siblings of T2 in `attach init`: `base or DEFAULTS` took the default for --base ''."""
    with pytest.raises(AttachError):
        repo.attach(str(studio), upstream.url, **{field: value})
    assert "attach" not in yaml.safe_load((studio / "project.yaml").read_text()) and not (studio / "app").exists()


@pytest.mark.parametrize("source", ["", "  ", "\t"])
def test_attach_init_refuses_an_empty_source(studio, source):
    with pytest.raises(AttachError, match="empty"):
        repo.attach(str(studio), source)
    assert not (studio / "app").exists()


def _exclude_bytes(root):
    p = Path(root) / ".git" / "info" / "exclude"
    return p.read_bytes() if p.exists() else None


@pytest.mark.parametrize("stop", [AttachError("git check-ignore ran out of time after 120s"), KeyboardInterrupt(),
                                  RuntimeError("boom")])
def test_a_failure_inside_the_ignore_proof_puts_the_exclude_file_back(studio, upstream, monkeypatch, stop):
    """T3: the exclude lines were written before the proof ran; a raise from `git check-ignore` (a
    timeout, Ctrl-C) skipped the restore because only a non-zero exit restored it."""
    before = _exclude_bytes(studio)
    real = gitops.run

    def run(repo_dir, *args, **kw):
        if args and args[0] == "check-ignore":
            raise stop
        return real(repo_dir, *args, **kw)

    monkeypatch.setattr(gitops, "run", run)
    with pytest.raises(type(stop)):
        repo.attach(str(studio), upstream.url)
    assert _exclude_bytes(studio) == before and not (studio / "app").exists()


def test_ensure_ignored_alone_puts_the_exclude_file_back_when_the_proof_raises(studio, monkeypatch):
    ex = studio / ".git" / "info" / "exclude"
    ex.write_text("/mine/\n")
    real = gitops.run

    def run(repo_dir, *args, **kw):
        if args and args[0] == "check-ignore":
            raise KeyboardInterrupt()
        return real(repo_dir, *args, **kw)

    monkeypatch.setattr(gitops, "run", run)
    with pytest.raises(KeyboardInterrupt):
        repo.ensure_ignored(str(studio), "app")
    assert ex.read_text() == "/mine/\n"


# ----------------------------------------------------------------------------- t-675 (t-647 follow-ups 2 and 3)

def test_a_studio_path_that_is_a_link_is_refused_with_the_link_reason_and_nothing_is_written(studio, upstream, tmp_path):
    """Follow-up 2: the refusal said 'linked git worktree or a folder inside another repository'."""
    link = tmp_path / "studio-link"
    link.symlink_to(studio)
    assert gitops.studio_kind(str(link)) == "link"
    before = _snap(studio)
    with pytest.raises(AttachError) as caught:
        repo.attach(str(link), upstream.url)
    assert "studio path is a link" in str(caught.value) and "real path" in str(caught.value)
    assert "linked git worktree" not in str(caught.value)
    assert _snap(studio) == before and not (studio / "app").exists()
    with pytest.raises(AttachError, match="studio path is a link"):
        repo.ensure_ignored(str(link), "app")


def test_undo_runs_to_the_end_when_project_yaml_became_a_link_after_the_save(studio, upstream, tmp_path, monkeypatch, capsys):
    """Follow-up 3: undo() read project.yaml outside any try; a link there ended it before the state
    file, the state folder and the made files were cleaned."""
    real_save = project.save
    outside = tmp_path / "outside.yaml"

    def save_then_link_then_die(root, doc):
        real_save(root, doc)
        outside.write_bytes((studio / "project.yaml").read_bytes())
        (studio / "project.yaml").unlink()
        (studio / "project.yaml").symlink_to(outside)
        raise KeyboardInterrupt()

    monkeypatch.setattr(project, "save", save_then_link_then_die)
    with pytest.raises(KeyboardInterrupt):                        # not an AttachError out of undo()
        repo.attach(str(studio), upstream.url)
    out = capsys.readouterr().out
    assert "project.yaml could not be put back" in out
    assert not (studio / "app").exists()
    assert not (studio / ".alpaca" / "attach").exists(), "state folder left behind"
    assert (studio / "project.yaml").is_symlink() and "attach" in yaml.safe_load(outside.read_text())   # left for the owner, never followed


def test_undo_runs_to_the_end_when_state_json_became_a_link(studio, upstream, tmp_path, monkeypatch):
    real_save = project.save
    outside = tmp_path / "outside-state.json"
    outside.write_text("{}")

    def save_then_link_state_then_die(root, doc):
        real_save(root, doc)
        state = studio / ".alpaca" / "attach" / "state.json"
        state.unlink()
        state.symlink_to(outside)
        raise KeyboardInterrupt()

    monkeypatch.setattr(project, "save", save_then_link_state_then_die)
    with pytest.raises(KeyboardInterrupt):
        repo.attach(str(studio), upstream.url)
    assert outside.read_text() == "{}"
    assert not (studio / "app").exists() and not (studio / ".alpaca" / "attach").exists()
    assert "attach" not in yaml.safe_load((studio / "project.yaml").read_text())


# --------------------------------------------------------------------- t-675 fix round 1 (undo() is a list of independent steps)

class _BrokenStdout:
    """A stdout whose writes fail like a pipe whose reader is gone."""

    def write(self, s):
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        raise BrokenPipeError(32, "Broken pipe")


def _die_in_commit(monkeypatch, damage):
    """Make the run stop in `_commit` (after record_started is set) with a KeyboardInterrupt, after `damage()`."""
    def boom(*a, **k):
        damage()
        raise KeyboardInterrupt()
    monkeypatch.setattr(repo, "_commit", boom)


def test_undo_runs_to_the_end_when_its_warn_line_cannot_be_printed(studio, upstream, tmp_path, monkeypatch):
    """A07: the WARN print sat between the project.yaml step and the state.json step; a stdout write error
    (a closed pipe) ended undo() there and replaced the interrupt that started the rollback."""
    real_save = project.save
    outside = tmp_path / "outside.yaml"

    def save_then_link_then_die(root, doc):
        real_save(root, doc)
        outside.write_bytes((studio / "project.yaml").read_bytes())
        (studio / "project.yaml").unlink()
        (studio / "project.yaml").symlink_to(outside)
        monkeypatch.setattr(sys, "stdout", _BrokenStdout())
        raise KeyboardInterrupt()

    monkeypatch.setattr(project, "save", save_then_link_then_die)
    with pytest.raises(KeyboardInterrupt):                        # the original interrupt, not a BrokenPipeError
        repo.attach(str(studio), upstream.url)
    monkeypatch.setattr(sys, "stdout", sys.__stdout__)
    assert not (studio / "app").exists() and not (studio / ".alpaca" / "attach").exists()


def test_undo_never_deletes_state_json_outside_the_studio_through_a_swapped_state_folder(studio, upstream, tmp_path,
                                                                                         monkeypatch, capsys):
    """A12: `.alpaca/attach` swapped for a link during the run; undo() unlinked state.json through it."""
    outside = tmp_path / "owner-folder"

    def damage():
        shutil.move(str(studio / ".alpaca" / "attach"), str(tmp_path / "moved-state"))
        outside.mkdir()
        (outside / "state.json").write_text("owner state\n")
        (outside / "keep.txt").write_text("keep\n")
        (studio / ".alpaca" / "attach").symlink_to(outside)

    _die_in_commit(monkeypatch, damage)
    with pytest.raises(KeyboardInterrupt):
        repo.attach(str(studio), upstream.url)
    assert (outside / "state.json").read_text() == "owner state\n" and (outside / "keep.txt").read_text() == "keep\n"
    assert not (studio / "app").exists()
    assert (studio / ".alpaca" / "attach").is_symlink()           # left for the owner, never followed
    assert "WARN" in capsys.readouterr().out


def test_undo_never_removes_the_state_folder_outside_the_studio_through_a_swapped_dot_alpaca(studio, upstream,
                                                                                             tmp_path, monkeypatch):
    """Same class as A12, one level up: `.alpaca` itself swapped for a link; the rmtree of the state folder
    must not follow it."""
    outside = tmp_path / "owner-alpaca"

    def damage():
        shutil.move(str(studio / ".alpaca"), str(tmp_path / "moved-alpaca"))
        (outside / "attach").mkdir(parents=True)
        (outside / "attach" / "state.json").write_text("owner state\n")
        (studio / ".alpaca").symlink_to(outside)

    _die_in_commit(monkeypatch, damage)
    with pytest.raises(KeyboardInterrupt):
        repo.attach(str(studio), upstream.url)
    assert (outside / "attach" / "state.json").read_text() == "owner state\n"


def test_undo_never_deletes_made_files_outside_the_studio_through_a_swapped_folder(studio, upstream, tmp_path, monkeypatch):
    """Same class as A12 for `_undo_made`: the terms folder this run made is swapped for a link."""
    doc = project.load(str(studio))
    doc["barrier"] = dict(doc.get("barrier") or {}, terms="seal/deep/terms.txt")
    project.save(str(studio), doc)
    outside = tmp_path / "owner-seal"

    def damage():
        shutil.move(str(studio / "seal"), str(tmp_path / "moved-seal"))
        (outside / "deep").mkdir(parents=True)
        (outside / "deep" / "terms.txt").write_text("owner terms\n")
        (studio / "seal").symlink_to(outside)

    _die_in_commit(monkeypatch, damage)
    with pytest.raises(KeyboardInterrupt):
        repo.attach(str(studio), upstream.url)
    assert (outside / "deep" / "terms.txt").read_text() == "owner terms\n"
    assert not (studio / "app").exists() and not (studio / ".alpaca" / "attach").exists()


@pytest.mark.parametrize("raised, comes_out", [(RuntimeError, KeyboardInterrupt), (KeyboardInterrupt, KeyboardInterrupt),
                                               (SystemExit, SystemExit)])
def test_a_step_of_undo_that_raises_anything_does_not_stop_the_next_steps(studio, upstream, monkeypatch, raised,
                                                                          comes_out):
    """Class check: each undo() step runs on its own, whatever it raises (not only the exception types
    the first fix named). An interrupt or exit raised by a step is held until the last step has run and
    then comes out in place of the error that started the rollback (t-675 round 2, B01)."""
    def bad(*a, **k):
        raise raised()

    monkeypatch.setattr(repo, "_remove_clone", bad)
    monkeypatch.setattr(repo, "_restore_exclude", bad)
    _die_in_commit(monkeypatch, lambda: None)
    with pytest.raises(comes_out):
        repo.attach(str(studio), upstream.url)
    assert not (studio / ".alpaca" / "attach").exists()
    assert "attach" not in yaml.safe_load((studio / "project.yaml").read_text())


def test_a_second_ctrl_c_inside_an_undo_step_still_leaves_the_state_cleanup_done(studio, upstream, monkeypatch):
    """B01: a KeyboardInterrupt raised while the clone is being removed ended undo() before the exclude,
    project.yaml, state.json and state-folder steps."""
    real_remove = repo._remove_clone
    ran = []

    def second_ctrl_c(root, name, created):
        real_remove(root, name, created)
        raise KeyboardInterrupt()

    real_made = repo._undo_made
    monkeypatch.setattr(repo, "_remove_clone", second_ctrl_c)
    monkeypatch.setattr(repo, "_undo_made", lambda *a, **k: (ran.append("made"), real_made(*a, **k))[1])
    _die_in_commit(monkeypatch, lambda: None)
    with pytest.raises(KeyboardInterrupt):
        repo.attach(str(studio), upstream.url)
    assert ran == ["made"]
    assert not (studio / "app").exists() and not (studio / ".alpaca" / "attach").exists()
    assert "attach" not in yaml.safe_load((studio / "project.yaml").read_text())


def test_a_real_sigint_during_undo_waits_until_every_step_has_run(studio, upstream, monkeypatch):
    """B01, the window between two steps: a real SIGINT sent from inside a step (the step itself returns)
    is held by the run's SIGINT hold (no signal mask) and comes out only after the last step, and the
    caller's handler is put back."""
    real_remove = repo._remove_clone
    real_made = repo._undo_made
    seen = []

    def remove_then_signal(root, name, created):
        real_remove(root, name, created)
        os.kill(os.getpid(), signal.SIGINT)
        seen.append("signalled")

    def last_step(*a, **k):
        seen.append("last step")
        return real_made(*a, **k)

    monkeypatch.setattr(repo, "_remove_clone", remove_then_signal)
    monkeypatch.setattr(repo, "_undo_made", last_step)
    _die_in_commit(monkeypatch, lambda: None)
    try:
        with pytest.raises(KeyboardInterrupt):
            repo.attach(str(studio), upstream.url)
        assert seen == ["signalled", "last step"]
        assert not (studio / "app").exists() and not (studio / ".alpaca" / "attach").exists()
        assert "attach" not in yaml.safe_load((studio / "project.yaml").read_text())
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
        assert signal.SIGINT not in signal.pthread_sigmask(signal.SIG_BLOCK, set())
    finally:
        signal.signal(signal.SIGINT, signal.default_int_handler)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})


def test_undo_in_a_thread_other_than_the_main_one_still_runs_every_step(studio, upstream, monkeypatch):
    """Off the main thread no SIGINT hold is installed (Python runs signal handlers in the main thread only);
    a step that raises an interrupt is still held by step()."""
    import threading
    real_remove = repo._remove_clone

    def second_ctrl_c(root, name, created):
        real_remove(root, name, created)
        raise KeyboardInterrupt()

    monkeypatch.setattr(repo, "_remove_clone", second_ctrl_c)
    _die_in_commit(monkeypatch, lambda: None)
    out = []

    def run():
        try:
            repo.attach(str(studio), upstream.url)
        except BaseException as exc:                 # noqa: BLE001 - the test reads what came out
            out.append(exc)

    t = threading.Thread(target=run)
    t.start()
    t.join(60)
    assert len(out) == 1 and isinstance(out[0], KeyboardInterrupt)
    assert not (studio / ".alpaca" / "attach").exists()
    assert "attach" not in yaml.safe_load((studio / "project.yaml").read_text())


def test_undo_never_removes_a_folder_outside_the_studio_when_the_clone_folder_became_a_link(studio, upstream, tmp_path,
                                                                                            monkeypatch):
    """Sibling of A12 (the clone step): `app` swapped for a link to an owner folder; rollback removes the link only."""
    outside = tmp_path / "owner-app"

    def damage():
        shutil.move(str(studio / "app"), str(tmp_path / "moved-app"))
        outside.mkdir()
        (outside / "keep.txt").write_text("keep\n")
        (studio / "app").symlink_to(outside)

    _die_in_commit(monkeypatch, damage)
    with pytest.raises(KeyboardInterrupt):
        repo.attach(str(studio), upstream.url)
    assert (outside / "keep.txt").read_text() == "keep\n"
    assert not (studio / ".alpaca" / "attach").exists()


# ---- attach init for the first chat (t-656; R17, AC-18) ---------------------------------------------------

def _out(capsys):
    return capsys.readouterr().out


def test_ac18_attach_init_attaches_a_repo_to_a_fresh_studio_and_refuses_a_second_attach(studio, upstream, capsys):
    """AC-18 in one test: attach, then the same verb again."""
    before_events = len(db.events(db.connect(str(studio)), limit=100000))
    assert cli.main(["attach", "init", upstream.url]) == 0
    capsys.readouterr()
    app = studio / "app"
    assert (app / ".git").is_dir() and os.path.realpath(app).startswith(os.path.realpath(studio))
    assert git(app, "rev-parse", "HEAD") == upstream.sha("main")
    assert git(studio, "check-ignore", "-q", "app/x", check=False) == ""
    cfg = project.load(str(studio))
    assert set(cfg["attach"]) >= {"repo", "url", "upstream", "base", "branch_prefix", "gates"}
    assert all(isinstance(g["command"], list) for g in cfg["attach"]["gates"])
    assert cfg["existing_repo"] is True and cfg["paths"]["product_tree"] == "app"
    ev = events(studio, "attach-init")
    assert len(ev) == 1 and ev[0]["data"]["base_sha"] == upstream.sha("main") and ev[0]["op"] == "op-0"
    yaml_bytes = (studio / "project.yaml").read_bytes()
    head, count = git(app, "rev-parse", "HEAD"), len(db.events(db.connect(str(studio)), limit=100000))
    assert count > before_events
    rc = cli.main(["attach", "init", upstream.url])
    text = _out(capsys)
    assert rc == cli.FAIL and "GATE alpaca-attach-init: FAIL (this project is already attached" in text and "ATTACH-ALREADY" in text
    assert (studio / "project.yaml").read_bytes() == yaml_bytes and git(app, "rev-parse", "HEAD") == head
    assert len(db.events(db.connect(str(studio)), limit=100000)) == count


def test_attach_init_refuses_an_existing_app_folder_and_an_empty_gate_list_with_their_tokens(studio, tmp_path, capsys):
    bare = make_upstream(tmp_path, name="nogates", files={"README.md": "x\n"})
    (studio / "app").mkdir()
    (studio / "app" / "keep.txt").write_text("mine")
    assert cli.main(["attach", "init", bare.url]) == cli.FAIL
    assert "ATTACH-APP-EXISTS" in _out(capsys) and (studio / "app" / "keep.txt").read_text() == "mine"
    shutil.rmtree(studio / "app")
    assert cli.main(["attach", "init", bare.url]) == cli.FAIL
    assert "ATTACH-NO-GATES" in _out(capsys) and not (studio / "app").exists() and "attach" not in project.load(str(studio))


def _confirmed_card(chat, upstream, tmp_path, reply="yes"):
    cid, card = chat.walk_to_card(upstream.url)
    chat.say(reply)
    assert chat.run("onboard", "confirm", cid, "--quote", reply)[0] == 0
    return cid


def test_attach_init_with_a_card_takes_the_block_from_the_card_and_refuses_flags(chat, upstream, tmp_path, capsys):
    from alpaca.tests.onboardchat import chat as _c  # noqa: F401
    cid = _confirmed_card(chat, upstream, tmp_path)
    for flags in (["--base", "dev"], ["--gates-file", str(tmp_path / "g.json")], ["--branch-prefix", "zzz/"],
                  ["--upstream", "up"], ["--repo", "code"]):
        rc, out = chat.run("attach", "init", upstream.url, "--card", cid, *flags)
        assert rc == 1 and "ATTACH-CARD-FLAGS" in out, flags
    rc, out = chat.run("attach", "init", "/some/other/place", "--card", cid)
    assert rc == 1 and "ATTACH-CARD-SOURCE" in out
    assert not (Path(chat.root) / "app").exists() and "attach" not in project.load(chat.root)
    rc, out = chat.run("attach", "init", upstream.url, "--card", cid)
    assert rc == 0, out
    inputs = json.load(open(os.path.join(chat.root, ".alpaca", "onboard", "cards", cid + ".json")))
    block = project.load(chat.root)["attach"]
    st = inputs["settings"]
    assert block["url"] == st["source"] and block["base"] == st["base"] and block["branch_prefix"] == st["branch_prefix"]
    assert [(g["name"], g["command"]) for g in block["gates"]] == [(g["name"], g["command"]) for g in st["gates"]]
    ev = events(chat.root, "attach-init")[-1]["data"]
    assert ev["card"] == cid and ev["gates_source"] == "confirmed card %s" % cid


def test_attach_init_with_a_card_the_owner_did_not_confirm_is_refused(chat, upstream):
    cid, card = chat.walk_to_card(upstream.url)
    rc, out = chat.run("attach", "init", upstream.url, "--card", cid)
    assert rc == 1 and "ATTACH-CARD-NOT-CONFIRMED" in out and not (Path(chat.root) / "app").exists()
    rc, out = chat.run("attach", "init", upstream.url, "--card", "card-deadbeef")
    assert rc == 1 and "ATTACH-CARD-NOT-CONFIRMED" in out


def test_attach_init_with_a_card_whose_copy_changed_is_refused(chat, upstream, tmp_path):
    cid = _confirmed_card(chat, upstream, tmp_path)
    with open(os.path.join(chat.root, ".alpaca", "onboard", "cards", cid + ".md"), "a") as fh:
        fh.write("tampered\n")
    rc, out = chat.run("attach", "init", upstream.url, "--card", cid)
    assert rc == 1 and "ATTACH-CARD-CHANGED" in out and not (Path(chat.root) / "app").exists()


def test_attach_init_with_a_native_card_is_refused(chat):
    opening = chat.say("a new project please")
    chat.run("onboard", "answer", "opening", "--value", opening, "--quote", opening)
    reply = chat.say("a notes page. 2a")
    for args in (["goal", "--value", "a notes page"], ["stack", "--option", "a"]):
        chat.run("onboard", "answer", *args, "--quote", reply)
    cid, card = chat.propose()
    chat.say("yes")
    assert chat.run("onboard", "confirm", cid, "--quote", "yes")[0] == 0
    rc, out = chat.run("attach", "init", "/tmp", "--card", cid)
    assert rc == 1 and "ATTACH-CARD-MODE" in out


def test_attach_init_with_a_reference_uses_it_and_leaves_no_alternates(studio, upstream, tmp_path):
    ref = tmp_path / "reference"
    git(tmp_path, "clone", "-q", "--no-local", upstream.url, str(ref))
    cfg = repo.attach(str(studio), upstream.url, reference=str(ref))
    app = studio / cfg.repo
    assert not (app / ".git" / "objects" / "info" / "alternates").exists()
    assert git(app, "rev-parse", "--is-shallow-repository") == "false"
    assert git(app, "rev-parse", "HEAD") == upstream.sha("main") and git(app, "fsck", "--no-progress") is not None
    assert str(ref) not in (app / ".git" / "config").read_text()


def test_attach_init_scrubs_user_info_from_the_record_the_block_and_the_output(studio, upstream, capsys):
    rc = cli.main(["attach", "init", "file://u:SECRET@127.0.0.1" + upstream.url])
    out = _out(capsys)
    assert rc == cli.FAIL and "SECRET" not in out
    assert "SECRET" not in (studio / "project.yaml").read_text()
    assert not any("SECRET" in json.dumps(e["data"]) for e in db.events(db.connect(str(studio)), limit=100000))


def test_a_plain_studio_gets_no_attach_key_from_any_onboarding_path(chat, tmp_path):
    from alpaca.tests.onboardchat import Chat  # noqa: F401
    opening = chat.say("a new project please")
    chat.run("onboard", "answer", "opening", "--value", opening, "--quote", opening)
    reply = chat.say("a notes page. 2a")
    for args in (["goal", "--value", "a notes page"], ["stack", "--option", "a"]):
        chat.run("onboard", "answer", *args, "--quote", reply)
    cid, card = chat.propose()
    chat.say("yes")
    chat.run("onboard", "confirm", cid, "--quote", "yes")
    assert chat.run("onboard", "apply", cid)[0] == 0
    assert "attach" not in project.load(chat.root)


def _not_a_file(tmp_path, kind):
    """(path, keep): a path of `kind` that is not a plain file; `keep` is "pipe" when the child's standard input must be
    a pipe, or an object to close after the run."""
    if kind == "fifo":
        p = tmp_path / "pipe"
        os.mkfifo(p)
        return str(p), None
    if kind == "link-to-fifo":
        f = tmp_path / "pipe"
        os.mkfifo(f)
        p = tmp_path / "link"
        os.symlink(f, p)
        return str(p), None
    if kind == "dev-zero":
        return "/dev/zero", None
    if kind == "dir":
        return str(tmp_path), None
    if kind == "dev-stdin":
        return "/dev/stdin", None
    if kind == "proc-fd0-pipe":
        return "/proc/self/fd/0", "pipe"
    if kind == "socket":
        import socket
        p = str(tmp_path / "sock")
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(p)
        return p, sock
    if kind == "sparse-huge":
        p = tmp_path / "huge"
        with open(p, "wb") as fh:
            fh.truncate(repo.MAX_ANSWERS_BYTES * 64)
        return str(p), None
    raise AssertionError(kind)


NOT_A_FILE = ["fifo", "link-to-fifo", "dev-zero", "dir", "dev-stdin", "proc-fd0-pipe", "socket", "sparse-huge"]


@pytest.mark.parametrize("flag", ["--answers", "--gates-file"])
@pytest.mark.parametrize("kind", NOT_A_FILE)
def test_a_path_that_is_not_a_plain_file_is_refused_at_once_and_frees_the_lock(studio, upstream, tmp_path, flag, kind):
    """t-647 follow-up 1: opening a FIFO with no writer blocked the attach (and its lock) for good. Every kind that is
    not a plain file is refused at once, by the reader (never by the setup gate, so the case tests the reader)."""
    path, keep = _not_a_file(tmp_path, kind)
    env = dict(os.environ, PYTHONPATH=REPO)
    stdin = subprocess.PIPE if keep == "pipe" else subprocess.DEVNULL
    try:
        done = subprocess.run([sys.executable, "-m", "alpaca", "attach", "init", upstream.url, flag, path],
                              cwd=str(studio), env=env, stdin=stdin, capture_output=True, text=True, timeout=30)
    finally:
        if keep not in (None, "pipe"):
            keep.close()
    text = done.stdout + done.stderr
    assert "ATTACH-NEEDS-CARD" not in text and "ONBOARD-FIRST-CHAT-OPEN" not in text, text[-300:]
    assert done.returncode == 1 and any(s in text for s in ("is not a regular file", "cannot be read", "larger than")), \
        text[-300:]
    assert not (studio / "app").exists()
    # the lock was not left held: a plain attach right after works
    assert cli.main(["attach", "init", upstream.url]) == 0


@pytest.mark.parametrize("flag", ["--answers", "--gates-file"])
@pytest.mark.parametrize("kind", NOT_A_FILE)
def test_a_path_that_is_not_a_plain_file_is_refused_by_the_reader_before_the_setup_gate(project, upstream, tmp_path,
                                                                                         flag, kind):
    """The same refusal when the caller is one the setup gate refuses (an agent call, markers set, no terminal, in a
    studio whose first chat is due): the file argument is checked first, so the reader's text comes out at once and
    no `app/` is made (frozen attack B6, run with no terminal on standard input)."""
    from alpaca.tests.onboardchat import make_due_studio
    make_due_studio(project)
    path, keep = _not_a_file(tmp_path, kind)
    env = dict(os.environ, PYTHONPATH=REPO, CLAUDECODE="1", CLAUDE_CODE_SESSION_ID="chat-b6")
    env.pop("ALPACA_SESSION_ID", None)
    stdin = subprocess.PIPE if keep == "pipe" else subprocess.DEVNULL
    try:
        done = subprocess.run([sys.executable, "-m", "alpaca", "attach", "init", upstream.url, flag, path],
                              cwd=str(project), env=env, stdin=stdin, capture_output=True, text=True, timeout=30)
    finally:
        if keep not in (None, "pipe"):
            keep.close()
    text = done.stdout + done.stderr
    assert "ATTACH-NEEDS-CARD" not in text and "ONBOARD-FIRST-CHAT-OPEN" not in text, text[-300:]
    assert done.returncode == 1 and any(s in text for s in ("is not a regular file", "cannot be read", "larger than")), \
        text[-300:]
    assert not os.path.exists(os.path.join(str(project), "app"))
    # control: a plain file is not refused by the reader, so the setup gate refuses this caller
    plain = tmp_path / "plain.json"
    plain.write_text("[]", encoding="utf-8")
    done = subprocess.run([sys.executable, "-m", "alpaca", "attach", "init", upstream.url, flag, str(plain)],
                          cwd=str(project), env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=30)
    assert "ATTACH-NEEDS-CARD" in done.stdout + done.stderr, (done.stdout + done.stderr)[-300:]


def test_a_folder_given_as_a_file_is_refused(tmp_path):
    with pytest.raises(AttachError, match="not a regular file"):
        repo._read_answers(str(tmp_path))
    with pytest.raises(AttachError, match="cannot be read"):
        repo._read_answers(str(tmp_path / "missing"))
