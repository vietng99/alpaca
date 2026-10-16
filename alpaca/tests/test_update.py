"""alpaca update (op-061 t-646). No network: the release repository is a local git repository at a
file:// URL, and every install runs under a scratch HOME, PATH, --prefix and --bin-dir.

The Done bar 2 cases come first: already newest, newer found, offline, downgrade refused, pending
journal refused. The fail case (a failed install leaves the old release and the current link
unchanged) runs the real install.sh against a tampered release.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys

import pytest

from alpaca import cli, new, releasetags, update, upgrade

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GIT_ENV = dict(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t",
               GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


# ---- fixtures ---------------------------------------------------------------------------------
def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=dict(os.environ, **GIT_ENV))


def fake_repo(tmp_path, tags, annotated=(), name="pub"):
    """A local repository with the given tags; returns a file:// URL."""
    work = tmp_path / name
    work.mkdir()
    git("init", "-q", str(work))
    (work / "f").write_text("x")
    git("add", "f", cwd=work)
    git("commit", "-qm", "x", cwd=work)
    for tag in tags:
        git("tag", *(["-a", "-m", tag] if tag in annotated else []), tag, cwd=work)
    return "file://" + str(work)


def write_tree(base, version, channel="release", template=False):
    base.mkdir(parents=True, exist_ok=True)
    (base / "alpaca").mkdir(exist_ok=True)
    (base / "bin").mkdir(exist_ok=True)
    (base / "ALPACA-MANIFEST").write_text("[mechanism]\nalpaca/\nbin/\n")
    (base / "alpaca" / "__init__.py").write_text('VERSION = "%s"\n' % version)
    (base / "alpaca" / "_channel.py").write_text('CHANNEL = "%s"\n' % channel)
    (base / "project.yaml").write_text("template: true\n" if template else "name: Proj\nproject_id: proj\n")
    (base / "bin" / "alpaca").write_text("#!/bin/sh\nexit 0\n")
    (base / "bin" / "alpaca").chmod(0o755)
    return base


def project(tmp_path, version="1.3.0", channel="release", name="proj"):
    return write_tree(tmp_path / name, version, channel)


def make_prefix(tmp_path, version="1.3.0"):
    """A prefix with one installed release and the current link; returns (prefix, bin_dir)."""
    prefix, bin_dir = tmp_path / "prefix", tmp_path / "launcher-bin"
    write_tree(prefix / "releases" / "old", version, template=True)
    os.symlink("releases/old", prefix / "current")
    bin_dir.mkdir()
    return prefix, bin_dir


def listing(base):
    out = []
    for dirpath, dirs, files in os.walk(base):
        dirs.sort()
        out += [os.path.relpath(os.path.join(dirpath, n), base) for n in sorted(dirs + files)]
    return out


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    pathbin = tmp_path / "pathbin"
    pathbin.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "share"))
    monkeypatch.setenv("PATH", os.pathsep.join([str(pathbin), os.path.dirname(shutil.which("git")), "/usr/bin", "/bin"]))
    monkeypatch.setenv("ALPACA_CALLER_CWD", str(tmp_path))
    for key in ("ALPACA_UPDATE_REPO", "ALPACA_UPDATE_ACTIVE", "ALPACA_PYTHON", "ALPACA_ROOT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(update, "_interactive", lambda: True)
    monkeypatch.setattr(update, "_ask", lambda question: pytest.fail("asked: " + question))
    execs = []
    monkeypatch.setattr(update, "_exec", lambda path, argv, env: execs.append((path, argv, env)))
    monkeypatch.setattr(update, "_INSTALL_EXTRA", ["--no-bootstrap"])
    return execs


@pytest.fixture
def execs(scratch):
    return scratch


class Calls:
    """Records every command update runs and answers it like install.sh, git and the project would."""

    def __init__(self, monkeypatch):
        self.log = []
        self.rc = {}
        self.install_version = None      # what the fake installer writes, instead of the tag's version
        self.doctor_out = None           # what the fake doctor prints, instead of its own verdict line
        monkeypatch.setattr(update, "_run", self)

    def argvs(self):
        return [c["argv"] for c in self.log]

    def __call__(self, argv, cwd=None, env=None, timeout=None, capture=None):
        argv = [str(a) for a in argv]
        self.log.append(dict(argv=argv, cwd=cwd, env=env, timeout=timeout))
        if argv[0] == "git" and "clone" in argv:
            tag = argv[argv.index("--branch") + 1]
            write_tree(__import__("pathlib").Path(argv[-1]), tag[1:], template=True)
            return self.rc.get("clone", 0)
        if argv[0] == "bash" and argv[1].endswith("install.sh"):
            if self.rc.get("install", 0):
                return self.rc["install"]
            tree, prefix = argv[argv.index("--from") + 1], argv[argv.index("--prefix") + 1]
            version = self.install_version or releasetags.tree_version(tree)
            dest = __import__("pathlib").Path(prefix) / "releases" / ("rel-" + version)
            write_tree(dest, version, template=True)
            tmp = os.path.join(prefix, ".link")
            os.symlink("releases/" + dest.name, tmp)
            os.replace(tmp, os.path.join(prefix, "current"))
            return 0
        if argv[-1] == "--version":
            return self.rc.get("version", 0)
        if argv[0] == "bash" and argv[1].endswith("bootstrap.sh"):
            return self.rc.get("bootstrap-check" if argv[-1] == "--check" else "bootstrap", 0)
        if argv[-1] == "doctor":
            rc = self.rc.get("doctor", 0)
            text = self.doctor_out if self.doctor_out is not None else (
                "GATE alpaca-doctor: %s\n" % {0: "PASS", 1: "WARN"}.get(rc, "FAIL"))
            if capture is not None:
                capture.append(text)
            return rc
        pytest.fail("unexpected command: %r" % argv)


@pytest.fixture
def calls(monkeypatch):
    return Calls(monkeypatch)


def go(capsys, *argv):
    rc = cli.main(["update", *argv])
    out = capsys.readouterr()
    return rc, out.out + out.err


def in_project(monkeypatch, proj):
    monkeypatch.setenv("ALPACA_CALLER_CWD", str(proj))


# ---- Done bar 2: already newest, newer found, offline, downgrade, pending journal ---------------
def test_already_newest(tmp_path, monkeypatch, capsys, calls, execs):
    proj = project(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    repo = fake_repo(tmp_path, ["v1.2.3", "v1.3.0"])
    rc, out = go(capsys, "--repo", repo)
    assert rc == 0 and "already at the newest release v1.3.0" in out, out
    assert "GATE alpaca-update: PASS" in out
    assert calls.log == [] and execs == []
    # --check says the same and writes nothing
    before = listing(tmp_path)
    rc, out = go(capsys, "--check", "--repo", repo)
    assert rc == 0 and "already at the newest release v1.3.0" in out and "GATE alpaca-update-check: PASS" in out
    assert listing(tmp_path) == before


def test_newer_found_is_reported_by_check_and_changes_nothing(tmp_path, monkeypatch, capsys, calls, execs):
    proj = project(tmp_path, "1.3.0")
    prefix, bin_dir = make_prefix(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    repo = fake_repo(tmp_path, ["v1.3.0", "v1.3.1"], annotated=("v1.3.1",))
    before = listing(tmp_path)
    rc, out = go(capsys, "--check", "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir))
    assert rc == 0, out
    assert "project %s is at 1.3.0" % proj in out
    assert "installed release 1.3.0" in out and str(prefix) in out
    assert "newest release v1.3.1 at " + repo in out
    assert "v1.3.1 is newer than 1.3.0; run: alpaca update" in out and "GATE alpaca-update-check: PASS" in out
    assert listing(tmp_path) == before and calls.log == [] and execs == []


def test_newer_found_installs_then_hands_off_to_the_new_release(tmp_path, monkeypatch, capsys, calls, execs):
    proj = project(tmp_path, "1.3.0")
    prefix, bin_dir = make_prefix(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    repo = fake_repo(tmp_path, ["v1.3.0", "v1.3.1"], annotated=("v1.3.1",))
    rc, out = go(capsys, "--yes", "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir))
    assert rc == 0 or execs, out        # _exec is faked, so the hand-off returns here
    argvs = calls.argvs()
    clone, install, version = argvs
    assert clone[:7] == ["git", "-c", "credential.helper=", "clone", "-q", "--depth", "1"]
    assert clone[clone.index("--branch") + 1] == "v1.3.1" and clone[-2] == repo
    stage_tree = clone[-1]
    assert install[:3] == ["bash", stage_tree + "/install.sh", "--from"] and install[3] == stage_tree
    assert install[install.index("--prefix") + 1] == str(prefix) and install[install.index("--bin-dir") + 1] == str(bin_dir)
    assert "--force" not in install and "--no-bootstrap" in install
    new_release = os.path.realpath(prefix / "current")
    assert new_release.endswith("rel-1.3.1") and version == [new_release + "/bin/alpaca", "--version"]
    (path, argv, env), = execs
    assert path == new_release + "/bin/alpaca"
    assert argv[:2] == ["alpaca", "update"] and argv[argv.index("--target") + 1] == str(proj)
    assert argv[argv.index("--handoff") + 1] == "v1.3.1" and "--yes" in argv
    assert argv[argv.index("--prefix") + 1] == str(prefix) and argv[argv.index("--repo") + 1] == repo
    assert "ALPACA_UPDATE_ACTIVE" not in env
    assert "handing off to " + new_release in out
    assert not [n for n in os.listdir(prefix) if n.startswith(".update.") and n != ".update.lock"]


def test_offline_refuses_and_writes_nothing(tmp_path, monkeypatch, capsys, calls, execs):
    proj = project(tmp_path, "1.3.0")
    prefix, bin_dir = make_prefix(tmp_path)
    in_project(monkeypatch, proj)
    before = listing(tmp_path)
    for flags in ([], ["--check"]):
        rc, out = go(capsys, *flags, "--repo", "file://" + str(tmp_path / "missing"), "--prefix", str(prefix),
                     "--bin-dir", str(bin_dir), "--yes")
        assert rc == 2 and "cannot read release tags from file://" in out and "nothing was changed" in out, out
        assert "GATE alpaca-update" in out and "BLOCKED" in out
    assert listing(tmp_path) == before and calls.log == [] and execs == []


def test_a_hung_git_is_a_refusal_not_a_hang(tmp_path, monkeypatch, capsys, calls):
    proj = project(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    fake = tmp_path / "pathbin" / "git"
    fake.write_text("#!/bin/sh\nexec sleep 30\n")
    fake.chmod(0o755)
    monkeypatch.setattr(update, "LS_REMOTE_TIMEOUT", 1)
    rc, out = go(capsys, "--check", "--repo", "file://" + str(tmp_path / "pub"))
    assert rc == 2 and "timed out after 1s" in out, out


def test_no_git_is_a_refusal(tmp_path, monkeypatch, capsys, calls):
    proj = project(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    monkeypatch.setenv("PATH", str(tmp_path / "pathbin"))
    rc, out = go(capsys, "--check", "--repo", "file:///x")
    assert rc == 2 and "git is not installed" in out


def test_downgrade_is_refused(tmp_path, monkeypatch, capsys, calls, execs):
    proj = project(tmp_path, "1.4.0")
    prefix, bin_dir = make_prefix(tmp_path, "1.4.0")
    in_project(monkeypatch, proj)
    repo = fake_repo(tmp_path, ["v1.3.0"])
    for flags in ([], ["--check"]):
        rc, out = go(capsys, *flags, "--yes", "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir))
        assert rc == 2 and "the newest release v1.3.0 is older than 1.4.0; no downgrade" in out, out
    assert calls.log == [] and execs == []


def test_an_installed_release_newer_than_the_newest_tag_is_refused(tmp_path, monkeypatch, capsys, calls):
    proj = project(tmp_path, "1.3.0")
    prefix, bin_dir = make_prefix(tmp_path, "1.9.0")
    in_project(monkeypatch, proj)
    repo = fake_repo(tmp_path, ["v1.4.0"])
    rc, out = go(capsys, "--yes", "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir))
    assert rc == 2 and "the installed release 1.9.0 is newer than the newest tag v1.4.0" in out, out
    assert calls.log == []


@pytest.mark.parametrize("flags", [[], ["--check"], ["--yes"]])
def test_a_pending_upgrade_journal_is_refused_before_any_network_call(tmp_path, monkeypatch, capsys, calls, execs, flags):
    proj = project(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    upgrade._write_journal(str(proj), {"phase": "stage", "mechanism": []})
    rc, out = go(capsys, *flags, "--repo", "file://" + str(tmp_path / "missing"))
    assert rc == 2 and "pending upgrade journal (phase stage)" in out and "nothing was changed" in out, out
    assert "cannot read release tags" not in out          # refused before the network
    assert upgrade.pending(str(proj)) and calls.log == [] and execs == []


def test_a_pending_journal_survives_the_cli_start_when_run_inside_the_project(tmp_path, monkeypatch, capsys, calls):
    """cli.main recovers a crashed upgrade at every start; the update verb must report it instead."""
    proj = project(tmp_path, "1.3.0")
    monkeypatch.chdir(proj)
    monkeypatch.delenv("ALPACA_CALLER_CWD")
    upgrade._write_journal(str(proj), {"phase": "begin", "mechanism": []})
    rc, out = go(capsys, "--check", "--repo", "file://" + str(tmp_path / "missing"))
    assert rc == 2 and "pending upgrade journal" in out and upgrade.pending(str(proj))
    # argv=None reads sys.argv, the same way
    monkeypatch.setattr(sys, "argv", ["alpaca", "--session", "s", "update", "--check", "--repo", "file:///none"])
    assert cli.main() == 2 and upgrade.pending(str(proj))
    capsys.readouterr()


# ---- the fail case: a failed install leaves the old release and the current link ----------------
@pytest.fixture(scope="module")
def release_tree(tmp_path_factory):
    """A release-shaped tree built the way alpaca new builds its copy (the test_new.py recipe)."""
    root = tmp_path_factory.mktemp("rel") / "tree"
    new.copy_tree(REPO, str(root))
    new.apply_release_overlays(REPO, str(root))
    assert new._gen_manifest(str(root)).write(str(root)) == 0
    assert releasetags.tree_channel(str(root)) == "release"
    return root


def digest_of(base):
    h = hashlib.sha256()
    for dirpath, dirs, files in os.walk(base):
        dirs.sort()
        for n in sorted(files):
            p = os.path.join(dirpath, n)
            h.update(os.path.relpath(p, base).encode())
            h.update(os.readlink(p).encode() if os.path.islink(p) else open(p, "rb").read())
    return h.hexdigest()


def real_install(release_tree, prefix, bin_dir, monkeypatch):
    monkeypatch.setenv("ALPACA_PYTHON", sys.executable)
    res = subprocess.run(["bash", os.path.join(REPO, "install.sh"), "--from", str(release_tree), "--prefix", str(prefix),
                          "--bin-dir", str(bin_dir), "--no-bootstrap", "--python", sys.executable],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr


def tagged_release_repo(tmp_path, release_tree, version, tag, tamper):
    """A git repository holding a copy of the release tree at `version`, tagged `tag`; with `tamper`
    one byte is added to alpaca/cli.py after the manifest was written."""
    tree = tmp_path / "bad"
    shutil.copytree(release_tree, tree, symlinks=True)
    init = tree / "alpaca" / "__init__.py"
    init.write_text(init.read_text().replace('VERSION = "%s"' % releasetags.tree_version(str(release_tree)),
                                             'VERSION = "%s"' % version))
    assert new._gen_manifest(str(tree)).write(str(tree)) == 0
    if tamper:
        with open(tree / "alpaca" / "cli.py", "ab") as fh:
            fh.write(b"#")
    git("init", "-q", str(tree))
    git("add", "-A", "-f", cwd=tree)
    git("commit", "-qm", "release", cwd=tree)
    git("tag", tag, cwd=tree)
    return "file://" + str(tree)


def test_a_failed_install_leaves_the_old_release_and_the_current_link(tmp_path, monkeypatch, capfd, release_tree, execs):
    prefix, bin_dir = tmp_path / "share", tmp_path / "bin"
    real_install(release_tree, prefix, bin_dir, monkeypatch)
    proj = project(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    link = os.readlink(prefix / "current")
    releases = sorted(os.listdir(prefix / "releases"))
    launcher = (bin_dir / "alpaca").read_bytes()
    old = digest_of(prefix / "releases" / releases[0])
    repo = tagged_release_repo(tmp_path, release_tree, "9.9.9", "v9.9.9", tamper=True)
    rc, out = go(capfd, "--yes", "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir))
    assert rc == 1, out
    assert "installing v9.9.9 failed" in out and "the current release is unchanged" in out
    assert "does not match its MANIFEST.json" in out
    assert os.readlink(prefix / "current") == link
    assert sorted(os.listdir(prefix / "releases")) == releases
    assert (bin_dir / "alpaca").read_bytes() == launcher
    assert digest_of(prefix / "releases" / releases[0]) == old
    assert not [n for n in os.listdir(prefix) if n.startswith((".update.", ".stage.")) and n != ".update.lock"]
    assert execs == []


def test_a_good_tag_installs_with_the_real_installer_and_moves_current(tmp_path, monkeypatch, capsys, release_tree, execs):
    prefix, bin_dir = tmp_path / "share", tmp_path / "bin"
    real_install(release_tree, prefix, bin_dir, monkeypatch)
    proj = project(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    old_link = os.readlink(prefix / "current")
    repo = tagged_release_repo(tmp_path, release_tree, "9.9.9", "v9.9.9", tamper=False)
    # the installed release has no .venv here, so the post-install `--version` runs on system python3
    monkeypatch.setattr(update, "_run", lambda argv, cwd=None, env=None, timeout=None, capture=None: (
        0 if str(argv[-1]) == "--version" else _real_run(argv, cwd, env, timeout)))
    rc, out = go(capsys, "--yes", "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir))
    assert os.readlink(prefix / "current") != old_link, out
    assert releasetags.tree_version(str(prefix / "current")) == "9.9.9"
    assert len(os.listdir(prefix / "releases")) == 2
    (path, argv, env), = execs
    assert argv[argv.index("--handoff") + 1] == "v9.9.9"


def _real_run(argv, cwd, env, timeout):
    return subprocess.run([str(a) for a in argv], cwd=cwd, env=env or update._child_env(), stdin=subprocess.DEVNULL,
                          timeout=timeout, capture_output=True).returncode


def test_an_unusable_release_restores_the_current_link(tmp_path, monkeypatch, capsys, calls, execs):
    proj = project(tmp_path, "1.3.0")
    prefix, bin_dir = make_prefix(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    repo = fake_repo(tmp_path, ["v1.3.1"])
    # the installer puts a release of another version in place
    calls.install_version = "1.3.9"
    rc, out = go(capsys, "--yes", "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir))
    assert rc == 1 and "is not usable (it is version 1.3.9, not 1.3.1); current and the launcher are back as they were (current -> releases/old)" in out, out
    assert os.readlink(prefix / "current") == "releases/old" and execs == []
    # a release whose own --version fails
    calls.install_version = None
    calls.rc["version"] = 1
    rc, out = go(capsys, "--yes", "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir))
    assert rc == 1 and "alpaca --version failed in it" in out and os.readlink(prefix / "current") == "releases/old"
    assert execs == []


def test_a_tag_whose_tree_holds_another_version_installs_nothing(tmp_path, monkeypatch, capsys, calls, execs):
    proj = project(tmp_path, "1.3.0")
    prefix, bin_dir = make_prefix(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    repo = fake_repo(tmp_path, ["v1.3.1"])
    original = calls.__call__

    def clone_then_lie(argv, cwd=None, env=None, timeout=None, capture=None):
        rc = original(argv, cwd, env, timeout)
        if "clone" in argv:
            write_tree(__import__("pathlib").Path(argv[-1]), "1.3.7", template=True)
        return rc
    monkeypatch.setattr(update, "_run", clone_then_lie)
    rc, out = go(capsys, "--yes", "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir))
    assert rc == 1 and "the tag v1.3.1 holds version 1.3.7" in out and "unchanged" in out
    assert not any("install.sh" in " ".join(c["argv"]) for c in calls.log)


def test_a_failed_clone_or_installer_leaves_current_unchanged(tmp_path, monkeypatch, capsys, calls, execs):
    proj = project(tmp_path, "1.3.0")
    prefix, bin_dir = make_prefix(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    repo = fake_repo(tmp_path, ["v1.3.1"])
    for step in ("clone", "install"):
        calls.rc[step] = 3
        rc, out = go(capsys, "--yes", "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir))
        assert rc == 1 and "the current release is unchanged (current -> releases/old)" in out, out
        calls.rc.clear()
    assert os.readlink(prefix / "current") == "releases/old" and execs == []


def _fake_installer_that_changes_both(calls, monkeypatch, bin_dir, then):
    """The fake installer moves current and rewrites the launcher, as a real one does, then `then(rc)`."""
    original = calls.__call__

    def run(argv, cwd=None, env=None, timeout=None, capture=None):
        rc = original(argv, cwd, env, timeout, capture)
        if argv[0] == "bash" and argv[1].endswith("install.sh") and rc == 0:
            (bin_dir / "alpaca").write_text("#!/bin/sh\n# a launcher the new release wrote\n")
            (bin_dir / "alpaca").chmod(0o755)
            return then(rc)
        return rc
    monkeypatch.setattr(update, "_run", run)


@pytest.mark.parametrize("had_launcher", [True, False])
def test_an_installer_that_moved_current_and_the_launcher_before_failing_is_undone(tmp_path, monkeypatch, capsys, calls, had_launcher):
    proj = project(tmp_path, "1.3.0")
    prefix, bin_dir = make_prefix(tmp_path, "1.3.0")
    before = fake_launcher(bin_dir, prefix) if had_launcher else None
    in_project(monkeypatch, proj)
    repo = fake_repo(tmp_path, ["v1.3.1"])
    _fake_installer_that_changes_both(calls, monkeypatch, bin_dir, lambda rc: 9)
    rc, out = go(capsys, "--yes", "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir))
    assert rc == 1 and "installing v1.3.1 failed (exit 9); current and the launcher are back as they were (current -> releases/old)" in out, out
    assert os.readlink(prefix / "current") == "releases/old"
    if had_launcher:
        assert (bin_dir / "alpaca").read_bytes() == before and (bin_dir / "alpaca").stat().st_mode & 0o777 == 0o755
    else:
        assert not (bin_dir / "alpaca").exists()
    assert not (bin_dir / "alpaca.restore").exists()


def test_an_interrupt_during_the_install_puts_current_and_the_launcher_back(tmp_path, monkeypatch, capsys, calls):
    proj = project(tmp_path, "1.3.0")
    prefix, bin_dir = make_prefix(tmp_path, "1.3.0")
    before = fake_launcher(bin_dir, prefix)
    in_project(monkeypatch, proj)
    repo = fake_repo(tmp_path, ["v1.3.1"])

    def interrupt(rc):
        raise KeyboardInterrupt
    _fake_installer_that_changes_both(calls, monkeypatch, bin_dir, interrupt)
    rc = cli.main(["update", "--yes", "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir)])
    out = capsys.readouterr()
    text = out.out + out.err
    # t-673: the interrupt is a FAIL verdict that names the state, not a traceback
    assert rc == 1 and "GATE alpaca-update: FAIL" in text and "interrupted" in text, text
    assert "Traceback" not in text
    assert "current and the launcher are back as they were (current -> releases/old)" in text
    assert os.readlink(prefix / "current") == "releases/old"
    assert (bin_dir / "alpaca").read_bytes() == before
    assert not [n for n in os.listdir(prefix) if n.startswith(".update.") and n != ".update.lock"]


def test_a_release_that_is_not_usable_gets_the_old_launcher_back_too(tmp_path, monkeypatch, capsys, calls):
    proj = project(tmp_path, "1.3.0")
    prefix, bin_dir = make_prefix(tmp_path, "1.3.0")
    before = fake_launcher(bin_dir, prefix)
    in_project(monkeypatch, proj)
    repo = fake_repo(tmp_path, ["v1.3.1"])
    calls.rc["version"] = 1
    _fake_installer_that_changes_both(calls, monkeypatch, bin_dir, lambda rc: rc)
    rc, out = go(capsys, "--yes", "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir))
    assert rc == 1 and "alpaca --version failed in it" in out and "current and the launcher are back as they were" in out, out
    assert os.readlink(prefix / "current") == "releases/old"
    assert (bin_dir / "alpaca").read_bytes() == before


def test_a_release_whose_venv_is_not_ready_is_not_current_after_the_update(tmp_path, monkeypatch, capsys, calls):
    """--version answers before any package loads; bootstrap.sh --check is what sees a half built .venv."""
    proj = project(tmp_path, "1.3.0")
    prefix, bin_dir = make_prefix(tmp_path, "1.3.0")
    before = fake_launcher(bin_dir, prefix)
    in_project(monkeypatch, proj)
    repo = fake_repo(tmp_path, ["v1.3.1"])
    monkeypatch.setattr(update, "_INSTALL_EXTRA", [])
    calls.rc["bootstrap-check"] = 1
    _fake_installer_that_changes_both(calls, monkeypatch, bin_dir, lambda rc: rc)
    rc, out = go(capsys, "--yes", "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir))
    assert rc == 1 and "its .venv is not ready: bootstrap.sh --check exit 1" in out, out
    assert os.readlink(prefix / "current") == "releases/old"
    assert (bin_dir / "alpaca").read_bytes() == before
    # with --no-bootstrap in the install arguments there is no venv to check
    monkeypatch.setattr(update, "_INSTALL_EXTRA", ["--no-bootstrap"])
    rc, out = go(capsys, "--yes", "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir))
    assert "installed v1.3.1 at" in out, out


def test_a_restore_that_fails_names_the_command_that_fixes_it(tmp_path, monkeypatch, capsys, calls):
    proj = project(tmp_path, "1.3.0")
    prefix, bin_dir = make_prefix(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    repo = fake_repo(tmp_path, ["v1.3.1"])
    calls.rc["version"] = 1

    def no_restore(prefix_, old):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(update, "_restore_current", no_restore)
    rc, out = go(capsys, "--yes", "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir))
    assert rc == 1, out                                      # a FAIL verdict, not an internal error (67)
    assert "could not be restored" in out and "restore it with: ln -sfn releases/old %s/current" % prefix in out, out


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write to a read-only folder")
def test_the_real_installer_moves_nothing_when_the_launcher_cannot_be_written(tmp_path, monkeypatch, release_tree):
    prefix, bin_dir = tmp_path / "share", tmp_path / "bin"
    real_install(release_tree, prefix, bin_dir, monkeypatch)
    link, launcher = os.readlink(prefix / "current"), (bin_dir / "alpaca").read_bytes()
    second = tmp_path / "second"
    shutil.copytree(release_tree, second, symlinks=True)
    init = second / "alpaca" / "__init__.py"
    init.write_text(init.read_text().replace('VERSION = "%s"' % releasetags.tree_version(str(release_tree)), 'VERSION = "9.9.9"'))
    assert new._gen_manifest(str(second)).write(str(second)) == 0
    bin_dir.chmod(0o555)
    try:
        res = subprocess.run(["bash", os.path.join(REPO, "install.sh"), "--from", str(second), "--prefix", str(prefix),
                              "--bin-dir", str(bin_dir), "--no-bootstrap", "--python", sys.executable],
                             capture_output=True, text=True)
    finally:
        bin_dir.chmod(0o755)
    assert res.returncode != 0, res.stdout + res.stderr
    assert os.readlink(prefix / "current") == link and (bin_dir / "alpaca").read_bytes() == launcher
    assert sorted(os.listdir(bin_dir)) == ["alpaca"]
    assert not [n for n in os.listdir(prefix) if n.startswith(".stage.")]


def test_the_real_installer_removes_a_venv_it_cannot_make_work_and_moves_nothing(tmp_path, monkeypatch, release_tree):
    """A half built .venv (pip failed after the folder was made) must not let a retry make the release current."""
    prefix, bin_dir = tmp_path / "share", tmp_path / "bin"
    real_install(release_tree, prefix, bin_dir, monkeypatch)
    link, launcher = os.readlink(prefix / "current"), (bin_dir / "alpaca").read_bytes()
    second = tmp_path / "second"
    shutil.copytree(release_tree, second, symlinks=True)
    init = second / "alpaca" / "__init__.py"
    init.write_text(init.read_text().replace('VERSION = "%s"' % releasetags.tree_version(str(release_tree)), 'VERSION = "9.9.9"'))
    assert new._gen_manifest(str(second)).write(str(second)) == 0
    args = ["bash", os.path.join(REPO, "install.sh"), "--from", str(second), "--prefix", str(prefix),
            "--bin-dir", str(bin_dir), "--python", sys.executable]
    first = subprocess.run(args + ["--no-bootstrap"], capture_output=True, text=True)
    assert first.returncode == 0, first.stdout + first.stderr
    (prefix / "current").unlink()
    os.symlink(link, prefix / "current")                       # as if the first run had stopped before the link moved
    new_release = [r for r in os.listdir(prefix / "releases") if "releases/" + r != link][0]
    venv = prefix / "releases" / new_release / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text("#!/bin/sh\nexit 1\n")
    (venv / "python").chmod(0o755)
    res = subprocess.run(args, capture_output=True, text=True)
    assert res.returncode != 0 and "bootstrap of" in res.stderr, res.stdout + res.stderr
    assert not (prefix / "releases" / new_release / ".venv").exists()
    assert os.readlink(prefix / "current") == link and (bin_dir / "alpaca").read_bytes() == launcher
    assert sorted(os.listdir(bin_dir)) == ["alpaca"]


# ---- refusals ---------------------------------------------------------------------------------
def test_dev_template_and_installed_trees_are_not_projects(tmp_path, monkeypatch, capsys, calls, execs):
    prefix, bin_dir = make_prefix(tmp_path, "1.3.0")
    repo = fake_repo(tmp_path, ["v1.4.0"])
    common = ["--yes", "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir)]
    dev = project(tmp_path, "1.3.0", channel="dev", name="dev")
    rc, out = go(capsys, "--target", str(dev), *common)
    assert rc == 2 and "is a development tree (channel dev)" in out, out
    rc, out = go(capsys, "--target", str(prefix / "releases" / "old"), *common)
    assert rc == 2 and "is an installed release, not a project" in out, out
    inside = write_tree(prefix / "releases" / "plain", "1.3.0")          # no template line, still under releases/
    rc, out = go(capsys, "--target", str(inside), *common)
    assert rc == 2 and "is an installed release, not a project" in out, out
    rc, out = go(capsys, "--target", str(tmp_path / "nowhere"), *common)
    assert rc == 2 and "has no ALPACA-MANIFEST" in out
    assert calls.log == [] and execs == []


def test_without_a_terminal_and_without_yes_nothing_is_asked_or_written(tmp_path, monkeypatch, capsys, calls, execs):
    proj = project(tmp_path, "1.3.0")
    prefix, bin_dir = make_prefix(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    monkeypatch.setattr(update, "_interactive", lambda: False)
    repo = fake_repo(tmp_path, ["v1.4.0"])
    before = listing(tmp_path)
    rc, out = go(capsys, "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir))
    assert rc == 2 and "no terminal to ask; pass --yes" in out, out
    assert listing(tmp_path) == before and calls.log == []
    rc, out = go(capsys, "--check", "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir))
    assert rc == 0 and "run: alpaca update" in out


def test_declining_the_install_changes_nothing(tmp_path, monkeypatch, capsys, calls, execs):
    proj = project(tmp_path, "1.3.0")
    prefix, bin_dir = make_prefix(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    asked = []
    monkeypatch.setattr(update, "_ask", lambda q: asked.append(q) or False)
    repo = fake_repo(tmp_path, ["v1.4.0"])
    rc, out = go(capsys, "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir))
    assert rc == 2 and "declined; nothing changed" in out and len(asked) == 1 and "Install v1.4.0" in asked[0]
    assert calls.log == [] and execs == []


def test_no_install_found_is_a_refusal_naming_prefix_and_check_still_reports(tmp_path, monkeypatch, capsys, calls):
    proj = project(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    repo = fake_repo(tmp_path, ["v1.4.0"])
    before = listing(tmp_path)
    rc, out = go(capsys, "--yes", "--repo", repo)
    assert rc == 2 and "no installed Alpaca found" in out and "--prefix" in out, out
    assert listing(tmp_path) == before                        # no prefix was created anywhere
    rc, out = go(capsys, "--check", "--repo", repo)
    assert rc == 0 and "installed release unknown" in out and "run: alpaca update" in out
    assert listing(tmp_path) == before


def test_nested_update_insecure_scheme_and_held_lock_are_refused(tmp_path, monkeypatch, capsys, calls, execs):
    import fcntl
    proj = project(tmp_path, "1.3.0")
    prefix, bin_dir = make_prefix(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    repo = fake_repo(tmp_path, ["v1.4.0"])
    common = ["--yes", "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir)]
    monkeypatch.setenv("ALPACA_UPDATE_ACTIVE", "1")
    rc, out = go(capsys, *common)
    assert rc == 2 and "already running in a parent process" in out
    monkeypatch.delenv("ALPACA_UPDATE_ACTIVE")
    for url in ("http://example.invalid/r.git", "git://example.invalid/r.git"):
        rc, out = go(capsys, "--yes", "--repo", url)
        assert rc == 2 and "use https, ssh or a local path" in out
    fd = os.open(prefix / ".update.lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        rc, out = go(capsys, *common)
        assert rc == 2 and "another alpaca update holds " in out, out
    finally:
        os.close(fd)
    assert calls.log == [] and execs == []


def test_a_repo_with_no_release_tag_is_refused(tmp_path, monkeypatch, capsys, calls):
    proj = project(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    repo = fake_repo(tmp_path, ["v1.4", "nightly", "v1.5.0-rc1"])
    rc, out = go(capsys, "--check", "--repo", repo)
    assert rc == 2 and "has no release tag (vX.Y.Z)" in out


def test_the_repo_comes_from_the_environment_when_not_given(tmp_path, monkeypatch, capsys, calls):
    proj = project(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    monkeypatch.setenv("ALPACA_UPDATE_REPO", fake_repo(tmp_path, ["v1.3.0"]))
    rc, out = go(capsys)
    assert rc == 0 and "already at the newest release v1.3.0" in out


# ---- the project, the install, the paths --------------------------------------------------------
def test_a_relative_target_is_read_from_the_folder_the_command_ran_in(tmp_path, monkeypatch):
    proj = project(tmp_path, "1.3.0", name="proj")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(REPO)                       # bin/alpaca-python runs Python in the install root
    monkeypatch.setenv("ALPACA_CALLER_CWD", str(tmp_path))
    assert update.find_project("proj") == (str(proj), True)
    monkeypatch.setenv("ALPACA_CALLER_CWD", str(elsewhere))
    with pytest.raises(update._Refuse, match="has no ALPACA-MANIFEST"):
        update.find_project("proj")


def test_the_project_is_found_from_the_callers_folder_not_the_install_root(tmp_path, monkeypatch):
    proj = project(tmp_path, "1.3.0")
    (proj / "sub").mkdir()
    monkeypatch.setenv("ALPACA_CALLER_CWD", str(proj / "sub"))
    assert update.find_project(None) == (os.path.realpath(proj), False)
    monkeypatch.setenv("ALPACA_CALLER_CWD", str(tmp_path / "home"))
    assert update.find_project(None) == (None, False)


def test_the_launcher_tells_update_where_the_install_is(release_tree, tmp_path, monkeypatch):
    prefix, bin_dir = tmp_path / "it's a prefix", tmp_path / "b'in dir"
    real_install(release_tree, prefix, bin_dir, monkeypatch)
    text = (bin_dir / "alpaca").read_text()
    assert update.LAUNCHER_MARK in text and "ALPACA_BIN_DIR=" in text
    assert update._launcher_values(str(bin_dir / "alpaca")) == (str(os.path.realpath(prefix)), str(os.path.realpath(bin_dir)))
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "alpaca").write_text("#!/bin/sh\necho mine\n")
    assert update._launcher_values(str(foreign / "alpaca")) is None
    monkeypatch.setenv("PATH", os.pathsep.join([str(foreign), str(bin_dir), os.environ["PATH"]]))
    assert update.find_install() == (os.path.realpath(prefix), os.path.realpath(bin_dir))
    assert update.find_install(str(prefix)) == (os.path.realpath(prefix), os.path.realpath(bin_dir))
    # a prefix nothing points at needs --bin-dir; one with no current release is no install
    other = tmp_path / "other"
    (other / "releases").mkdir(parents=True)
    with pytest.raises(update._Refuse, match="pass --bin-dir"):
        update.find_install(str(other))
    with pytest.raises(update._Refuse, match="has no current release"):
        update.find_install(str(other), str(bin_dir))
    monkeypatch.setenv("PATH", str(foreign))
    with pytest.raises(update._Refuse, match="no installed Alpaca found"):
        update.find_install()
    assert not (tmp_path / "home" / "share").exists()


def test_the_launcher_runs_update_from_the_installed_release_even_inside_a_project(release_tree, tmp_path, monkeypatch):
    prefix, bin_dir = tmp_path / "share", tmp_path / "bin"
    real_install(release_tree, prefix, bin_dir, monkeypatch)
    venv = prefix / "current" / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(sys.executable)
    proj = write_tree(tmp_path / "work" / "p", releasetags.tree_version(str(release_tree)))
    (proj / "bin" / "alpaca").write_text("#!/bin/sh\necho project-cli \"$@\"\n")
    (proj / "bin" / "alpaca").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "ALPACA_ROOT", "ALPACA_CALLER_CWD")}
    status = subprocess.run([str(bin_dir / "alpaca"), "status"], cwd=proj, capture_output=True, text=True, env=env)
    assert status.stdout.strip() == "project-cli status"
    repo = fake_repo(tmp_path, [f"v{releasetags.tree_version(str(release_tree))}"])
    out = subprocess.run([str(bin_dir / "alpaca"), "update", "--check", "--repo", repo], cwd=proj,
                         capture_output=True, text=True, env=env)
    assert "project-cli" not in out.stdout and out.returncode == 0, out.stdout + out.stderr
    assert "GATE alpaca-update-check: PASS" in out.stdout
    assert "(prefix %s, launcher folder %s)" % (os.path.realpath(prefix), os.path.realpath(bin_dir)) in out.stdout
    assert not (prefix / "current" / ".alpaca").exists()      # no record inside the installed release


def test_outside_a_project_update_installs_and_stops(tmp_path, monkeypatch, capsys, calls, execs):
    prefix, bin_dir = make_prefix(tmp_path, "1.3.0")
    work = tmp_path / "work"
    work.mkdir()
    in_project(monkeypatch, work)
    repo = fake_repo(tmp_path, ["v1.3.1"])
    rc, out = go(capsys, "--yes", "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir))
    assert rc == 0 and "no project here; the installed release is 1.3.0" in out, out
    assert "installed v1.3.1; run alpaca update inside each project" in out
    assert os.readlink(prefix / "current") == "releases/rel-1.3.1" and execs == []
    rc, out = go(capsys, "--yes", "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir))
    assert rc == 0 and "already at the newest release v1.3.1" in out


def test_children_get_a_clean_environment_and_no_stdin(tmp_path, monkeypatch, capsys, calls, execs):
    proj = project(tmp_path, "1.3.0")
    prefix, bin_dir = make_prefix(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    for key in ("ALPACA_ROOT", "VIRTUAL_ENV", "PYTHONPATH", "CLAUDE_PROJECT_DIR"):
        monkeypatch.setenv(key, "x")
    repo = fake_repo(tmp_path, ["v1.3.1"])
    go(capsys, "--yes", "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir))
    assert len(calls.log) == 3
    for call in calls.log:
        for key in ("ALPACA_ROOT", "VIRTUAL_ENV", "PYTHONPATH", "CLAUDE_PROJECT_DIR", "ALPACA_CALLER_CWD"):
            assert key not in call["env"], (call["argv"], key)
        assert call["env"]["ALPACA_UPDATE_ACTIVE"] == "1" and call["env"]["GIT_TERMINAL_PROMPT"] == "0"
    (path, argv, env), = execs
    for key in ("ALPACA_ROOT", "VIRTUAL_ENV", "PYTHONPATH", "CLAUDE_PROJECT_DIR", "ALPACA_CALLER_CWD"):
        assert key not in env


def test_the_real_runner_gives_a_child_no_stdin_and_reports_failure_codes(monkeypatch, tmp_path):
    check = "import os,sys;sys.exit(0 if os.path.samestat(os.fstat(0), os.stat('/dev/null')) else 5)"
    assert update._run([sys.executable, "-c", check]) == 0
    assert update._run([sys.executable, "-c", "import sys; sys.exit(7)"]) == 7
    assert update._run([str(tmp_path / "no-such-command")]) == 127
    assert update._run(["sleep", "5"], timeout=1) == 124


# ---- phase 2: inside the newly installed release ------------------------------------------------
def release_dir(prefix, version):
    rel = write_tree(prefix / "releases" / ("rel-" + version), version, template=True)
    (rel / "alpaca" / "extra.py").write_text("NEW = 1\n")
    tmp = prefix / ".link"
    os.symlink("releases/" + rel.name, tmp)
    os.replace(tmp, prefix / "current")
    return rel


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def project_with_local_files(tmp_path):
    proj = project(tmp_path, "1.3.0")
    (proj / "alpaca" / "shipped.py").write_text("SHIPPED = 1\n")
    (proj / "alpaca" / "edited.py").write_text("EDITED = 'by the owner'\n")
    (proj / "alpaca" / "added.py").write_text("ADDED = 1\n")
    (proj / "MANIFEST.json").write_text(json.dumps({"files": {
        "alpaca/__init__.py": sha(proj / "alpaca" / "__init__.py"), "alpaca/_channel.py": sha(proj / "alpaca" / "_channel.py"),
        "alpaca/shipped.py": sha(proj / "alpaca" / "shipped.py"), "alpaca/edited.py": "0" * 64}}))
    return proj


class Phase2:
    def __init__(self, tmp_path, monkeypatch, capsys, calls):
        self.proj = project_with_local_files(tmp_path)
        self.prefix, self.bin_dir = make_prefix(tmp_path, "1.3.0")
        self.rel = release_dir(self.prefix, "1.3.1")
        self.repo = fake_repo(tmp_path, ["v1.3.1"])
        self.events = []
        self.capsys, self.calls = capsys, calls
        in_project(monkeypatch, self.proj)
        monkeypatch.setattr(update, "INSTALL_ROOT", str(self.rel))
        monkeypatch.setattr(update, "RUNNING_VERSION", "1.3.1")
        real_print = upgrade.print_plan

        def printed(plan):
            self.events.append("plan")
            return real_print(plan)

        def applied(root, source):
            self.events.append("apply")
            assert source == str(self.rel)
            (self.proj / "alpaca" / "__init__.py").write_text('VERSION = "1.3.1"\n')
        monkeypatch.setattr(upgrade, "print_plan", printed)
        monkeypatch.setattr(upgrade, "apply", applied)
        orig = calls.__call__

        def logged(argv, cwd=None, env=None, timeout=None, capture=None):
            self.events.append(os.path.basename(str(argv[1] if str(argv[0]) == "bash" else argv[-1])))
            return orig(argv, cwd, env, timeout, capture)
        monkeypatch.setattr(update, "_run", logged)

    def run(self, *flags, handoff=False):
        base = ["--repo", self.repo, "--prefix", str(self.prefix), "--bin-dir", str(self.bin_dir)]
        if handoff:
            base += ["--target", str(self.proj), "--handoff", "v1.3.1"]
        return go(self.capsys, *base, *flags)


def test_phase_two_shows_the_plan_saves_local_files_applies_and_proves(tmp_path, monkeypatch, capsys, calls, execs):
    p = Phase2(tmp_path, monkeypatch, capsys, calls)
    (p.proj / ".venv" / "bin").mkdir(parents=True)
    (p.proj / ".venv" / "bin" / "python").write_text("")
    rc, out = p.run("--yes")
    assert rc == 0, out
    assert p.events == ["plan", "apply", "bootstrap.sh", "doctor"], p.events
    assert "alpaca update: plan: refresh=" in out and "changed=" in out
    assert "local file, saved before apply: alpaca/edited.py" in out and "local file, saved before apply: alpaca/added.py" in out
    saved = list((p.proj / ".alpaca" / "update").iterdir())
    assert len(saved) == 1
    assert (saved[0] / "alpaca" / "edited.py").read_text() == "EDITED = 'by the owner'\n"
    assert (saved[0] / "alpaca" / "added.py").read_text() == "ADDED = 1\n"
    assert not (saved[0] / "alpaca" / "shipped.py").exists()           # a shipped file is not a local file
    assert "saved 2 local file(s) to .alpaca/update/" in out
    assert "updated %s from 1.3.0 to 1.3.1; doctor passed" % p.proj in out and "GATE alpaca-update: PASS" in out
    assert [c["argv"][-1] for c in calls.log] == ["--check", "doctor"] and execs == []
    assert calls.log[0]["cwd"] == str(p.proj)


def test_phase_two_by_handoff_does_not_read_the_network_again(tmp_path, monkeypatch, capsys, calls, execs):
    p = Phase2(tmp_path, monkeypatch, capsys, calls)
    monkeypatch.setattr(update, "_tags", lambda repo: pytest.fail("the hand-off read the network"))
    rc, out = p.run("--yes", handoff=True)
    assert rc == 0 and p.events[:2] == ["plan", "apply"], out


def test_phase_two_skips_the_venv_check_for_a_project_without_a_venv(tmp_path, monkeypatch, capsys, calls):
    p = Phase2(tmp_path, monkeypatch, capsys, calls)
    rc, out = p.run("--yes")
    assert rc == 0 and "the project has no .venv; venv check skipped" in out
    assert "bootstrap.sh" not in p.events


def test_phase_two_runs_bootstrap_once_when_the_venv_check_fails(tmp_path, monkeypatch, capsys, calls):
    p = Phase2(tmp_path, monkeypatch, capsys, calls)
    (p.proj / ".venv" / "bin").mkdir(parents=True)
    (p.proj / ".venv" / "bin" / "python").write_text("")
    calls.rc["bootstrap-check"] = 1
    rc, out = p.run("--yes")
    assert rc == 0 and p.events.count("bootstrap.sh") == 2
    calls.rc["bootstrap"] = 1
    (p.proj / "alpaca" / "__init__.py").write_text('VERSION = "1.3.0"\n')
    rc, out = p.run("--yes")
    assert rc == 1 and "the project's .venv does not match v1.3.1; run bash setup/bootstrap.sh in " in out


def test_phase_two_doctor_error_fails_and_a_warning_passes(tmp_path, monkeypatch, capsys, calls):
    p = Phase2(tmp_path, monkeypatch, capsys, calls)
    calls.rc["doctor"] = 2
    rc, out = p.run("--yes")
    assert rc == 1 and "applied; alpaca doctor exited 2" in out and "GATE alpaca-update: FAIL" in out
    calls.rc["doctor"] = 1
    (p.proj / "alpaca" / "__init__.py").write_text('VERSION = "1.3.0"\n')
    rc, out = p.run("--yes")
    assert rc == 0 and "doctor passed with warnings" in out
    # exit 1 is also what an uncaught exception gives: without doctor's own WARN verdict it is a failure
    calls.doctor_out = "Traceback (most recent call last):\nKeyError: 'x'\n"
    (p.proj / "alpaca" / "__init__.py").write_text('VERSION = "1.3.0"\n')
    rc, out = p.run("--yes")
    assert rc == 1 and "alpaca doctor exited 1 without a WARN verdict" in out, out


def test_phase_two_an_apply_that_changed_nothing_is_a_failure(tmp_path, monkeypatch, capsys, calls):
    p = Phase2(tmp_path, monkeypatch, capsys, calls)
    monkeypatch.setattr(upgrade, "apply", lambda root, source: None)
    rc, out = p.run("--yes")
    assert rc == 1 and "the project still says 1.3.0 after the upgrade" in out
    assert [c for c in calls.log if c["argv"][-1] == "doctor"] == []


def test_phase_two_a_crashed_apply_names_both_states(tmp_path, monkeypatch, capsys, calls):
    p = Phase2(tmp_path, monkeypatch, capsys, calls)

    def boom(root, source):
        raise RuntimeError("disk full")
    monkeypatch.setattr(upgrade, "apply", boom)
    rc, out = p.run("--yes")
    assert rc == 1 and "upgrade failed: RuntimeError: disk full; v1.3.1 is installed; the journal under .alpaca/upgrade/" in out
    assert "run alpaca update again" in out
    monkeypatch.setattr(upgrade, "apply", lambda root, source: (_ for _ in ()).throw(upgrade.UpgradeLocked("busy")))
    rc, out = p.run("--yes")
    assert rc == 2 and "UpgradeLocked: busy" in out


def test_phase_two_a_no_at_the_plan_leaves_the_project_alone(tmp_path, monkeypatch, capsys, calls):
    p = Phase2(tmp_path, monkeypatch, capsys, calls)
    asked = []
    monkeypatch.setattr(update, "_ask", lambda q: asked.append(q) or False)
    rc, out = p.run()
    assert rc == 2 and asked == ["Apply this plan to %s?" % p.proj], asked
    assert "declined; v1.3.1 is installed and current, the project is unchanged; run alpaca update again to finish" in out
    assert p.events == ["plan"] and not (p.proj / ".alpaca" / "update").exists()


def test_phase_two_without_a_terminal_and_without_yes_is_refused_before_the_apply(tmp_path, monkeypatch, capsys, calls):
    p = Phase2(tmp_path, monkeypatch, capsys, calls)
    monkeypatch.setattr(update, "_interactive", lambda: False)
    rc, out = p.run()
    assert rc == 2 and "no terminal to ask; pass --yes" in out and "apply" not in p.events


def test_the_handoff_guard_never_execs_again(tmp_path, monkeypatch, capsys, calls, execs):
    p = Phase2(tmp_path, monkeypatch, capsys, calls)
    monkeypatch.setattr(update, "INSTALL_ROOT", REPO)                       # not the current release
    rc, out = p.run("--yes", handoff=True)
    assert rc == 1 and "it needs the current release at " in out and "holding 1.3.1" in out and "not retrying" in out
    monkeypatch.setattr(update, "INSTALL_ROOT", str(p.rel))
    monkeypatch.setattr(update, "RUNNING_VERSION", "1.3.0")                   # right place, wrong code
    rc, out = p.run("--yes", handoff=True)
    assert rc == 1 and "not retrying" in out
    assert execs == [] and p.events == []


def test_phase_two_finishes_as_already_newest_when_the_project_has_caught_up(tmp_path, monkeypatch, capsys, calls):
    p = Phase2(tmp_path, monkeypatch, capsys, calls)
    (p.proj / "alpaca" / "__init__.py").write_text('VERSION = "1.3.1"\n')
    rc, out = p.run("--yes", handoff=True)
    assert rc == 0 and "already at the newest release v1.3.1" in out and p.events == []


def test_an_installed_newest_release_skips_the_install_and_applies_in_process(tmp_path, monkeypatch, capsys, calls, execs):
    """Second project, current already newest: no clone, no installer, no hand-off (R6)."""
    p = Phase2(tmp_path, monkeypatch, capsys, calls)
    rc, out = p.run("--yes")
    assert rc == 0 and "release v1.3.1 is already installed" in out and p.events[:2] == ["plan", "apply"]
    assert not any(c["argv"][0] in ("git",) or "install.sh" in " ".join(c["argv"]) for c in calls.log) and execs == []


def test_a_running_copy_that_is_not_current_hands_off_without_installing(tmp_path, monkeypatch, capsys, calls, execs):
    p = Phase2(tmp_path, monkeypatch, capsys, calls)
    monkeypatch.setattr(update, "INSTALL_ROOT", REPO)
    rc, out = p.run("--yes")
    (path, argv, env), = execs
    assert path == str(p.rel) + "/bin/alpaca" and "--handoff" in argv and calls.log == []


# ---- the prefix and the launcher folder must be one install (round 1, B4) -----------------------
def fake_launcher(folder, home, bin_dir=None, marked=True):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "alpaca").write_text("#!/bin/sh\n%sALPACA_HOME='%s'\nALPACA_BIN_DIR='%s'\n" % (
        (update.LAUNCHER_MARK + "\n") if marked else "", home, bin_dir or folder))
    (folder / "alpaca").chmod(0o755)
    return (folder / "alpaca").read_bytes()


def two_installs(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    pa, ba = make_prefix(a, "1.3.0")
    pb, bb = make_prefix(b, "1.3.0")
    return (pa, ba, fake_launcher(ba, pa)), (pb, bb, fake_launcher(bb, pb))


def test_a_prefix_paired_with_another_installs_launcher_folder_is_refused(tmp_path):
    (pa, ba, la), (pb, bb, lb) = two_installs(tmp_path)
    assert update.find_install(str(pa), str(ba)) == (str(pa), str(ba))          # a matching pair passes
    for prefix, folder, who in ((pb, ba, "A"), (pa, bb, "B")):                  # the launcher folder of the other install
        with pytest.raises(update._Refuse, match="is the launcher of .*, not .*; pass --bin-dir with the launcher folder of") as e:
            update.find_install(str(prefix), str(folder))
        assert e.value.code == update.BLOCKED and "nothing was changed" in e.value.reason
    # a file there that install.sh did not write
    other = tmp_path / "C"
    fake_launcher(other, pa, marked=False)
    with pytest.raises(update._Refuse, match="was not written by install.sh; pass --bin-dir"):
        update.find_install(str(pa), str(other))
    # an empty folder is fine: install.sh writes a new launcher there and touches no other
    empty = tmp_path / "D"
    empty.mkdir()
    assert update.find_install(str(pa), str(empty)) == (str(pa), str(empty))
    assert (ba / "alpaca").read_bytes() == la and (bb / "alpaca").read_bytes() == lb


@pytest.mark.parametrize("flags", [[], ["--check"]])
def test_the_launchers_own_bin_dir_with_another_prefix_installs_nothing(tmp_path, monkeypatch, capsys, calls, execs, flags):
    """alpaca update --prefix B through launcher A passes A's --bin-dir too (install.sh); B's installer would rewrite A's launcher."""
    (pa, ba, la), (pb, bb, lb) = two_installs(tmp_path)
    proj = project(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    repo = fake_repo(tmp_path, ["v1.3.1"])
    before = listing(tmp_path)
    rc, out = go(capsys, *flags, "--yes", "--repo", repo, "--prefix", str(pb), "--bin-dir", str(ba))
    assert rc == 2 and "is the launcher of" in out and "pass --bin-dir with the launcher folder of %s" % pb in out, out
    assert calls.log == [] and execs == [] and listing(tmp_path) == before
    assert (ba / "alpaca").read_bytes() == la and (bb / "alpaca").read_bytes() == lb


def test_a_hand_edited_bin_dir_line_is_caught_by_the_same_check(tmp_path, monkeypatch, capsys, calls):
    (pa, ba, la), (pb, bb, lb) = two_installs(tmp_path)
    fake_launcher(ba, pa, bin_dir=bb)                         # A's launcher says its folder is B's
    monkeypatch.setenv("PATH", os.pathsep.join([str(ba), os.environ["PATH"]]))
    with pytest.raises(update._Refuse, match="is the launcher of"):
        update.find_install(None, str(bb))
    proj = project(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    repo = fake_repo(tmp_path, ["v1.3.1"])
    rc, out = go(capsys, "--yes", "--repo", repo, "--bin-dir", str(bb))
    assert rc == 2 and "is the launcher of" in out and calls.log == []


def test_through_the_real_launcher_a_foreign_prefix_is_named_and_both_launchers_stay(release_tree, tmp_path, monkeypatch):
    a, b = tmp_path / "shareA", tmp_path / "shareB"
    ba, bb = tmp_path / "binA", tmp_path / "binB"
    real_install(release_tree, a, ba, monkeypatch)
    real_install(release_tree, b, bb, monkeypatch)
    for prefix in (a,):
        venv = prefix / "current" / ".venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python").symlink_to(sys.executable)
    la, lb = (ba / "alpaca").read_bytes(), (bb / "alpaca").read_bytes()
    version = releasetags.tree_version(str(release_tree))
    major, minor, patch = (int(x) for x in version.split("."))
    repo = fake_repo(tmp_path, ["v%d.%d.%d" % (major, minor, patch + 1)])
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "ALPACA_ROOT", "ALPACA_CALLER_CWD")}
    work = tmp_path / "work"
    work.mkdir()
    out = subprocess.run([str(ba / "alpaca"), "update", "--check", "--repo", repo, "--prefix", str(b)], cwd=work,
                         capture_output=True, text=True, env=env)
    assert out.returncode == 2, out.stdout + out.stderr
    assert "binA/alpaca is the launcher of %s, not %s" % (os.path.realpath(a), os.path.realpath(b)) in out.stdout
    assert "pass --bin-dir with the launcher folder of %s" % os.path.realpath(b) in out.stdout
    assert (ba / "alpaca").read_bytes() == la and (bb / "alpaca").read_bytes() == lb


# ---- other refusals and errors named by round 1 --------------------------------------------------
def test_an_upgrade_journal_that_cannot_be_read_is_refused_and_left_alone(tmp_path, monkeypatch, capsys, calls, execs):
    proj = project(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    journal = proj / ".alpaca" / "upgrade" / upgrade.JOURNAL
    journal.parent.mkdir(parents=True)
    journal.write_bytes(b"")                                  # a torn write
    assert not upgrade.pending(str(proj))
    for flags in ([], ["--check"], ["--yes"]):
        rc, out = go(capsys, *flags, "--repo", "file://" + str(tmp_path / "missing"))
        assert rc == 2 and "upgrade journal file that cannot be read" in out and "nothing was changed" in out, out
        assert "cannot read release tags" not in out
    assert journal.read_bytes() == b"" and calls.log == [] and execs == []


@pytest.mark.parametrize("repo", ["--upload-pack=touch /nonexistent/x", "-oProxyCommand=x"])
def test_a_repo_that_looks_like_an_option_is_refused_before_anything_runs(tmp_path, monkeypatch, capsys, calls, execs, repo):
    proj = project(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    for flags in ([], ["--check"]):
        rc, out = go(capsys, *flags, "--yes", "--repo=" + repo)
        assert rc == 2 and "a repository address cannot start with '-'" in out, out
    monkeypatch.setenv("ALPACA_UPDATE_REPO", repo)
    rc, out = go(capsys, "--check")
    assert rc == 2 and "cannot start with '-'" in out and calls.log == [] and execs == []


def test_the_repo_address_follows_a_double_dash_in_the_clone(tmp_path, monkeypatch, capsys, calls):
    proj = project(tmp_path, "1.3.0")
    prefix, bin_dir = make_prefix(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    repo = fake_repo(tmp_path, ["v1.3.1"])
    go(capsys, "--yes", "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir))
    clone = calls.log[0]["argv"]
    assert clone[-3:-1] == ["--", repo] and clone[clone.index("--branch") + 1] == "v1.3.1"


def test_a_prefix_that_cannot_be_written_is_a_refusal_not_an_internal_error(tmp_path, monkeypatch, capsys, calls, execs):
    if os.geteuid() == 0:
        pytest.skip("root can write to a read-only folder")
    proj = project(tmp_path, "1.3.0")
    prefix, bin_dir = make_prefix(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    repo = fake_repo(tmp_path, ["v1.3.1"])
    prefix.chmod(0o555)
    try:
        rc, out = go(capsys, "--yes", "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir))
    finally:
        prefix.chmod(0o755)
    assert rc == 2 and "cannot write" in out and "nothing was changed" in out, out
    assert calls.log == [] and execs == [] and os.readlink(prefix / "current") == "releases/old"


def test_a_hand_off_that_cannot_exec_is_a_failure_that_names_the_state(tmp_path, monkeypatch, capsys, calls, execs):
    proj = project(tmp_path, "1.3.0")
    prefix, bin_dir = make_prefix(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    repo = fake_repo(tmp_path, ["v1.3.1"])

    def cannot_exec(path, argv, env):
        raise OSError(8, "Exec format error")
    monkeypatch.setattr(update, "_exec", cannot_exec)
    rc, out = go(capsys, "--yes", "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir))
    assert rc == 1 and "could not hand off to" in out and "v1.3.1 is installed and current, the project is unchanged" in out, out


def test_phase_two_saves_a_claude_md_with_an_edit_inside_its_boot_block(tmp_path, monkeypatch, capsys, calls):
    from alpaca import manifest
    p = Phase2(tmp_path, monkeypatch, capsys, calls)
    for base in (p.proj, p.rel):
        (base / "ALPACA-MANIFEST").write_text("[mechanism]\nalpaca/\nbin/\nCLAUDE.md\n")
    block = lambda body: "%s\n%s\n%s\n" % (manifest.BOOT_BEGIN, body, manifest.BOOT_END)
    (p.rel / "CLAUDE.md").write_text(block("boot of the new release"))
    (p.proj / "CLAUDE.md").write_text("# my rules\n" + block("boot of the old release\nand a line I added inside it"))
    mine = (p.proj / "CLAUDE.md").read_bytes()
    rc, out = p.run("--yes")
    assert rc == 0, out
    assert "local edit in the boot block will be replaced by the release's block: CLAUDE.md" in out
    assert "local file, saved before apply: CLAUDE.md" in out
    saved = list((p.proj / ".alpaca" / "update").iterdir())
    assert len(saved) == 1 and (saved[0] / "CLAUDE.md").read_bytes() == mine


# ---- hardening (t-673, the nine minor findings of the t-646 adversarial round 2) ---------------
def hold_project_lock(proj):
    """Take the project's lock on a file descriptor of our own, as another process would hold it."""
    import fcntl
    os.makedirs(os.path.join(str(proj), ".alpaca"), exist_ok=True)
    fd = os.open(upgrade.lock_path(str(proj)), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def within(seconds, fn):
    """fn() in a thread, waited for at most `seconds`: (thread, {"value"|"error": ...})."""
    import threading
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:      # noqa: BLE001 - handed to the test
            box["error"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    return t, box


def other_release(tmp_path, release_tree, version, name="second"):
    """A copy of the release tree at another version, with its MANIFEST.json written again."""
    tree = tmp_path / name
    shutil.copytree(release_tree, tree, symlinks=True)
    init = tree / "alpaca" / "__init__.py"
    init.write_text(init.read_text().replace('VERSION = "%s"' % releasetags.tree_version(str(release_tree)),
                                             'VERSION = "%s"' % version))
    assert new._gen_manifest(str(tree)).write(str(tree)) == 0
    return tree


def install_from(tree, prefix, bin_dir, env=None, preexec_fn=None):
    return subprocess.run(["bash", os.path.join(REPO, "install.sh"), "--from", str(tree), "--prefix", str(prefix),
                           "--bin-dir", str(bin_dir), "--no-bootstrap", "--python", sys.executable],
                          capture_output=True, text=True, env=env, preexec_fn=preexec_fn)


# minor 1: a project lock around plan and apply
def test_a_second_update_of_one_project_is_refused_while_the_first_holds_its_lock(tmp_path, monkeypatch, capsys, calls, execs):
    p = Phase2(tmp_path, monkeypatch, capsys, calls)
    fd = hold_project_lock(p.proj)
    try:
        rc, out = p.run("--yes")
    finally:
        os.close(fd)
    assert rc == 2 and "is running" in out and "nothing was changed" in out, out
    assert "GATE alpaca-update: BLOCKED" in out
    assert p.events == [] and not (p.proj / ".alpaca" / "update").exists()
    assert not [c for c in calls.log if c["argv"][-1] == "doctor"] and execs == []
    # with the lock free the run goes through; it holds the lock for the apply and not for the checks
    seen = []
    monkeypatch.setattr(upgrade, "apply", lambda root, source: (seen.append(("apply", upgrade.busy(root))),
                        (p.proj / "alpaca" / "__init__.py").write_text('VERSION = "1.3.1"\n'))[0])
    orig = update._run

    def logged(argv, cwd=None, env=None, timeout=None, capture=None):
        seen.append((str(argv[-1]), upgrade.busy(str(p.proj))))
        return orig(argv, cwd, env, timeout, capture)
    monkeypatch.setattr(update, "_run", logged)
    rc, out = p.run("--yes")
    assert rc == 0, out
    assert seen[0] == ("apply", True) and ("doctor", False) in seen, seen
    assert not upgrade.busy(str(p.proj))


def test_a_cli_start_leaves_the_journal_of_a_running_upgrade_alone_and_update_says_wait(tmp_path, monkeypatch, capsys, calls):
    proj = project(tmp_path, "1.3.0")
    monkeypatch.chdir(proj)
    monkeypatch.delenv("ALPACA_CALLER_CWD")
    upgrade._write_journal(str(proj), {"phase": "stage", "mechanism": []})
    journal = open(upgrade._journal_path(str(proj)), "rb").read()
    fd = hold_project_lock(proj)
    try:
        cli.main(["--session", "s", "status"])           # every other verb recovers at start-up
        capsys.readouterr()
        assert open(upgrade._journal_path(str(proj)), "rb").read() == journal
        rc, out = go(capsys, "--check", "--repo", "file://" + str(tmp_path / "missing"))
        assert rc == 2 and "is running now" in out and "recover it first" not in out, out
    finally:
        os.close(fd)
    rc, out = go(capsys, "--check", "--repo", "file://" + str(tmp_path / "missing"))
    assert rc == 2 and "pending upgrade journal" in out and "recover it first" in out    # nobody holds it now


# minor 2: an abbreviated --session never skips the journal refusal
def test_an_abbreviated_session_option_never_runs_the_start_up_recovery(tmp_path, monkeypatch, capsys, calls):
    proj = project(tmp_path, "1.3.0")
    monkeypatch.chdir(proj)
    monkeypatch.delenv("ALPACA_CALLER_CWD")
    upgrade._write_journal(str(proj), {"phase": "promote", "mechanism": []})
    journal = open(upgrade._journal_path(str(proj)), "rb").read()
    assert cli.build_parser()[0].allow_abbrev is False
    for argv in (["--sess", "s", "update", "--check", "--repo", "file:///none"], ["--sess", "s", "status"],
                 ["--s", "s", "update", "--check"], ["--sess", "update"]):
        assert cli.main(argv) == 64, argv
        capsys.readouterr()
        assert open(upgrade._journal_path(str(proj)), "rb").read() == journal, argv
    # the full option still works, and update still refuses on the journal
    assert cli.main(["--session", "s", "update", "--check", "--repo", "file:///none"]) == 2
    assert "pending upgrade journal" in capsys.readouterr().out
    assert open(upgrade._journal_path(str(proj)), "rb").read() == journal
    # a verb that is not update still recovers at start-up, and a subcommand keeps its own abbreviations
    assert cli.main(["--session", "s", "status"]) in (0, 1, 2, 67)
    capsys.readouterr()
    assert not upgrade.pending(str(proj))


# minor 3: a named pipe under a refreshed path does not hang the plan
def test_phase_two_with_a_named_pipe_in_a_refreshed_folder_fails_before_the_apply(tmp_path, monkeypatch, capsys, calls):
    p = Phase2(tmp_path, monkeypatch, capsys, calls)
    pipe = str(p.proj / "alpaca" / "pipe")
    os.mkfifo(pipe)
    try:
        t, box = within(20, lambda: p.run("--yes"))
        assert not t.is_alive(), "alpaca update is stuck opening the named pipe"
    finally:
        try:
            os.close(os.open(pipe, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass
    rc, out = box["value"]
    assert rc == 1 and "could not save your local files" in out and "named pipe" in out and "nothing was applied" in out, out
    assert p.events == ["plan"] and not upgrade.pending(str(p.proj))


# minor 6: an undo puts a symlinked launcher back as the link it was
def test_an_undo_puts_a_symlinked_launcher_back_as_the_same_link(tmp_path, monkeypatch, capsys, calls):
    proj = project(tmp_path, "1.3.0")
    prefix, bin_dir = make_prefix(tmp_path, "1.3.0")
    real = tmp_path / "real-launcher-folder"
    target_bytes = fake_launcher(real, prefix, bin_dir)
    os.symlink(str(real / "alpaca"), bin_dir / "alpaca")
    in_project(monkeypatch, proj)
    repo = fake_repo(tmp_path, ["v1.3.1"])
    original = calls.__call__

    def run(argv, cwd=None, env=None, timeout=None, capture=None):
        rc = original(argv, cwd, env, timeout, capture)
        if argv[0] == "bash" and argv[1].endswith("install.sh") and rc == 0:
            os.unlink(bin_dir / "alpaca")                        # install.sh's mv -f replaces the link itself
            (bin_dir / "alpaca").write_text("#!/bin/sh\n# a launcher the new release wrote\n")
            (bin_dir / "alpaca").chmod(0o755)
        return rc
    monkeypatch.setattr(update, "_run", run)
    calls.rc["version"] = 1
    rc, out = go(capsys, "--yes", "--repo", repo, "--prefix", str(prefix), "--bin-dir", str(bin_dir))
    assert rc == 1 and "alpaca --version failed in it" in out and "back as they were" in out, out
    assert os.path.islink(bin_dir / "alpaca") and os.readlink(bin_dir / "alpaca") == str(real / "alpaca")
    assert (real / "alpaca").read_bytes() == target_bytes
    assert os.readlink(prefix / "current") == "releases/old" and not (bin_dir / "alpaca.restore").exists()


def test_the_launcher_reader_and_restorer_keep_a_link_even_when_it_dangles(tmp_path):
    bin_dir = tmp_path / "b"
    bin_dir.mkdir()
    assert update._read_launcher(str(bin_dir)) is None
    os.symlink(str(tmp_path / "gone"), bin_dir / "alpaca")
    saved = update._read_launcher(str(bin_dir))
    assert saved == ("link", str(tmp_path / "gone"))
    os.unlink(bin_dir / "alpaca")
    (bin_dir / "alpaca").write_text("x")
    update._restore_launcher(str(bin_dir), saved)
    assert os.path.islink(bin_dir / "alpaca") and os.readlink(bin_dir / "alpaca") == str(tmp_path / "gone")
    update._restore_launcher(str(bin_dir), None)
    assert not os.path.lexists(bin_dir / "alpaca")


# minor 7: an interrupt is a FAIL verdict (see test_an_interrupt_during_the_install_puts_current_and_the_launcher_back),
# install.sh cleans up after itself
def test_an_interrupt_during_the_apply_names_the_journal_and_leaves_no_traceback(tmp_path, monkeypatch, capsys, calls):
    p = Phase2(tmp_path, monkeypatch, capsys, calls)

    def interrupted(root, source):
        raise KeyboardInterrupt
    monkeypatch.setattr(upgrade, "apply", interrupted)
    rc, out = p.run("--yes")
    assert rc == 1 and "interrupted during the upgrade" in out and "recovered on the project's next start" in out, out
    assert "Traceback" not in out and not upgrade.busy(str(p.proj))
    # anywhere else it is a plain FAIL too
    monkeypatch.setattr(upgrade, "plan", interrupted)
    rc, out = p.run("--yes")
    assert rc == 1 and "interrupted" in out and "Traceback" not in out


def test_the_real_installer_cleans_up_after_a_signal(tmp_path, monkeypatch, release_tree):
    import signal
    prefix, bin_dir = tmp_path / "share", tmp_path / "bin"
    real_install(release_tree, prefix, bin_dir, monkeypatch)
    old, launcher = os.readlink(prefix / "current"), (bin_dir / "alpaca").read_bytes()
    second = other_release(tmp_path, release_tree, "9.9.9")
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "mv").write_text('#!/bin/bash\ncase "$*" in\n  *alpaca.new*) kill -INT $PPID; sleep 1; exit 0 ;;\nesac\n'
                             'exec /bin/mv "$@"\n')
    (shim / "mv").chmod(0o755)
    env = dict(os.environ, PATH=str(shim) + os.pathsep + os.environ["PATH"])
    # Ctrl-C reaches install.sh after `current` moved and before the launcher rename
    res = install_from(second, prefix, bin_dir, env=env, preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    assert res.returncode == 130, res.stdout + res.stderr
    assert os.readlink(prefix / "current") == old and (bin_dir / "alpaca").read_bytes() == launcher
    assert not [n for n in os.listdir(prefix) if n.startswith(".stage.")]
    assert sorted(os.listdir(bin_dir)) == ["alpaca"]


def test_a_kill_signal_ends_an_update_like_ctrl_c_and_the_handlers_are_restored(tmp_path, monkeypatch, capsys):
    import signal
    before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP))
    monkeypatch.setattr(update, "_update", lambda args: os.kill(os.getpid(), signal.SIGTERM))
    rc = cli.main(["update", "--check"])
    out = capsys.readouterr().out
    assert rc == 1 and "GATE alpaca-update-check: FAIL" in out and "interrupted" in out, out
    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP)) == before


def test_the_runner_forwards_an_interrupt_to_the_child_and_waits_for_its_cleanup(tmp_path):
    import _thread
    import signal
    import threading
    import time
    ready, marker = tmp_path / "ready", tmp_path / "cleaned"
    script = ("sleep 30 & pid=$!\ntrap 'echo cleaned > %s; kill $pid; exit 130' INT\n: > %s\nwait\n" % (marker, ready))
    started = time.time()
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)   # a parent that ignored SIGINT is undone
    main_id = threading.main_thread().ident

    def press_ctrl_c():
        for _ in range(200):
            if ready.exists():
                break
            time.sleep(0.05)
        time.sleep(0.2)
        signal.pthread_kill(main_id, signal.SIGINT)
    threading.Thread(target=press_ctrl_c, daemon=True).start()
    try:
        with pytest.raises(KeyboardInterrupt):
            update._run(["bash", "-c", script])
    finally:
        signal.signal(signal.SIGINT, previous)
    assert marker.read_text() == "cleaned\n"                  # the child ran its own cleanup before _run let go
    assert time.time() - started < 10


# minor 8: an existing release folder is checked again before it is made current
def test_the_real_installer_rechecks_a_release_folder_that_is_already_there(tmp_path, monkeypatch, release_tree):
    prefix, bin_dir = tmp_path / "share", tmp_path / "bin"
    real_install(release_tree, prefix, bin_dir, monkeypatch)
    first = os.path.realpath(prefix / "current")
    second = other_release(tmp_path, release_tree, "9.9.9")
    assert install_from(second, prefix, bin_dir).returncode == 0
    assert os.path.realpath(prefix / "current") != first                  # `first` is a leftover folder now
    gen = os.path.join(first, "setup", "gen_manifest.py")
    # what a used release folder holds besides its files (a .venv, the interpreter's caches) is not drift
    os.makedirs(os.path.join(first, ".venv", "bin"))
    os.makedirs(os.path.join(first, "alpaca", "__pycache__"))
    with open(os.path.join(first, "alpaca", "__pycache__", "x.cpython-314.pyc"), "wb") as fh:
        fh.write(b"x")
    res = install_from(release_tree, prefix, bin_dir)
    assert res.returncode == 0 and "is already installed" in res.stdout and "replacing" not in res.stdout, res.stdout
    assert os.path.realpath(prefix / "current") == first
    assert install_from(second, prefix, bin_dir).returncode == 0                  # `first` is a leftover again
    assert os.path.realpath(prefix / "current") != first
    with open(os.path.join(first, "docs", "operators.md"), "a") as fh:
        fh.write("CHANGED-AFTER-INSTALL\n")
    with open(os.path.join(first, "alpaca", "planted.py"), "w") as fh:
        fh.write("PLANTED = 1\n")
    assert subprocess.run([sys.executable, gen, "--root", first, "--verify"], capture_output=True).returncode != 0
    res = install_from(release_tree, prefix, bin_dir)
    assert res.returncode == 0 and "replacing it" in res.stdout, res.stdout + res.stderr
    assert os.path.realpath(prefix / "current") == first
    assert subprocess.run([sys.executable, gen, "--root", first, "--verify"], capture_output=True).returncode == 0
    assert "CHANGED-AFTER-INSTALL" not in open(os.path.join(first, "docs", "operators.md")).read()
    assert not os.path.exists(os.path.join(first, "alpaca", "planted.py"))
    assert not [n for n in os.listdir(prefix) if n.startswith(".stage.")]
    # a release folder that is current and no longer matches is not replaced under a running install
    with open(os.path.join(first, "alpaca", "planted.py"), "w") as fh:
        fh.write("PLANTED = 1\n")
    link, launcher = os.readlink(prefix / "current"), (bin_dir / "alpaca").read_bytes()
    res = install_from(release_tree, prefix, bin_dir)
    assert res.returncode == 1 and "the current release folder" in res.stderr and "does not match its MANIFEST.json" in res.stderr
    assert os.readlink(prefix / "current") == link and (bin_dir / "alpaca").read_bytes() == launcher
    assert os.path.exists(os.path.join(first, "alpaca", "planted.py"))
    assert not [n for n in os.listdir(prefix) if n.startswith(".stage.")]


# minor 8, releases/ moved to another disk and linked: the current folder is still recognised
def test_the_installer_tells_the_current_release_through_a_linked_releases_folder(tmp_path, monkeypatch, release_tree):
    prefix, bin_dir = tmp_path / "share", tmp_path / "bin"
    disk2 = tmp_path / "disk2" / "releases"
    disk2.mkdir(parents=True)
    prefix.mkdir()
    os.symlink(disk2, prefix / "releases")
    real_install(release_tree, prefix, bin_dir, monkeypatch)
    first = os.path.realpath(prefix / "current")
    assert os.path.dirname(first) == str(disk2)                         # the release really sits behind the link
    second = other_release(tmp_path, release_tree, "9.9.9")
    assert install_from(second, prefix, bin_dir).returncode == 0
    gen = os.path.join(first, "setup", "gen_manifest.py")
    # a damaged folder that is not current is replaced, as with a plain releases folder
    with open(os.path.join(first, "alpaca", "planted.py"), "w") as fh:
        fh.write("PLANTED = 1\n")
    res = install_from(release_tree, prefix, bin_dir)
    assert res.returncode == 0 and "replacing it" in res.stdout, res.stdout + res.stderr
    assert os.path.realpath(prefix / "current") == first
    assert subprocess.run([sys.executable, gen, "--root", first, "--verify"], capture_output=True).returncode == 0
    assert not os.path.exists(os.path.join(first, "alpaca", "planted.py"))
    # a damaged folder that is current is refused and kept
    with open(os.path.join(first, "alpaca", "planted.py"), "w") as fh:
        fh.write("PLANTED = 1\n")
    link, launcher = os.readlink(prefix / "current"), (bin_dir / "alpaca").read_bytes()
    res = install_from(release_tree, prefix, bin_dir)
    assert res.returncode == 1 and "the current release folder" in res.stderr and "does not match its MANIFEST.json" in res.stderr, res.stdout + res.stderr
    assert "replacing" not in res.stdout
    assert os.readlink(prefix / "current") == link and (bin_dir / "alpaca").read_bytes() == launcher
    assert os.path.exists(os.path.join(first, "alpaca", "planted.py"))
    assert os.path.exists(os.path.join(first, "bin", "alpaca"))
    assert not [n for n in os.listdir(prefix) if n.startswith(".stage.")]


# minor 9: a relative --repo is read from the folder the command ran in
def test_a_relative_repo_is_read_from_the_folder_the_command_ran_in(tmp_path, monkeypatch, capsys, calls):
    proj = project(tmp_path, "1.3.0")
    in_project(monkeypatch, proj)
    callers = tmp_path / "callers"
    callers.mkdir()
    pub = fake_repo(callers, ["v1.3.0", "v1.3.1"], name="pub")
    path = pub[len("file://"):]
    monkeypatch.chdir(REPO)                       # bin/alpaca-python runs Python in the install root
    monkeypatch.setenv("ALPACA_CALLER_CWD", str(callers))
    for form in ("pub", "./pub"):
        rc, out = go(capsys, "--check", "--repo", form)
        assert rc == 0 and "newest release v1.3.1 at %s" % path in out, (form, out)
    monkeypatch.setenv("ALPACA_UPDATE_REPO", "pub")
    rc, out = go(capsys, "--check")
    assert rc == 0 and "newest release v1.3.1 at %s" % path in out, out
    # addresses git reads as a URL or as ssh are never joined to a folder
    for same in ("https://x.example/y.git", "ssh://h/o/r.git", "git@h.example:o/r.git", "h:path/r.git", "file:///abs/r"):
        assert update._repo_address(same) == same
    assert update._repo_address("./a:b") == str(callers / "a:b")
    assert update._repo_address("/abs/r") == "/abs/r"
    assert update._repo_address("~/r") == os.path.expanduser("~/r")
