"""alpaca/releasetags.py: the one release-tag parser (op-061 t-646). No network: ls-remote reads a local repository."""
import os
import subprocess

import pytest

from alpaca import releasetags as rt

GIT_ENV = dict(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t",
               GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=dict(os.environ, **GIT_ENV))


def fake_repo(tmp_path, tags, annotated=(), name="pub"):
    work = tmp_path / name
    work.mkdir()
    git("init", "-q", str(work))
    (work / "f").write_text("x")
    git("add", "f", cwd=work)
    git("commit", "-qm", "x", cwd=work)
    for tag in tags:
        git("tag", *(["-a", "-m", tag] if tag in annotated else []), tag, cwd=work)
    return "file://" + str(work)


@pytest.mark.parametrize("name, want", [
    ("v1.3.0", (1, 3, 0)), ("v0.0.1", (0, 0, 1)), ("v10.20.30", (10, 20, 30)),
    ("v1.3", None), ("v1.3.0-rc1", None), ("1.3.0", None), ("v1.3.0.1", None), ("v01.3.0", None),
    ("v1.3.x", None), ("main", None), ("", None), (None, None), ("v1.3.0\n", None),
])
def test_parse_tag(name, want):
    assert rt.parse_tag(name) == want


@pytest.mark.parametrize("text, want", [("1.3.0", (1, 3, 0)), ("1.10.0", (1, 10, 0)), ("v1.3.0", None),
                                         ("1.3", None), ("1.3.0.dev0", None), (None, None)])
def test_parse_version(text, want):
    assert rt.parse_version(text) == want


def test_tag_of_round_trips():
    assert rt.tag_of((1, 3, 0)) == "v1.3.0" and rt.parse_tag(rt.tag_of((12, 0, 7))) == (12, 0, 7)


SHA = "a" * 40
LS = "".join("%s\t%s\n" % (SHA, ref) for ref in (
    "refs/tags/v1.9.0", "refs/tags/v1.10.0", "refs/tags/v1.10.0^{}", "refs/tags/v1.3.0", "refs/tags/v1.3.0",
    "refs/tags/v2.0.0-rc1", "refs/tags/1.3.5", "refs/tags/nightly", "refs/heads/v9.9.9", "refs/tags/v1.3"))


def test_tags_from_ls_remote_keeps_release_tags_only_sorted_by_version_not_text():
    assert rt.tags_from_ls_remote(LS) == ["v1.3.0", "v1.9.0", "v1.10.0"]
    assert rt.tags_from_ls_remote("") == [] and rt.tags_from_ls_remote(None) == []
    assert rt.tags_from_ls_remote("garbage\nno tab here\n") == []


def test_newest_puts_v1_10_above_v1_9():
    assert rt.newest(["v1.9.0", "v1.10.0", "v1.2.0"]) == "v1.10.0"
    assert rt.newest([]) is None and rt.newest(["main", "v1.3"]) is None


def test_previous_is_strictly_below():
    tags = ["v1.2.0", "v1.3.0", "v1.10.0"]
    assert rt.previous(tags, "1.10.0") == "v1.3.0"
    assert rt.previous(tags, "1.3.0") == "v1.2.0"      # an equal tag is excluded
    assert rt.previous(tags, "1.2.0") is None
    assert rt.previous(tags, "1.11.0") == "v1.10.0"
    assert rt.previous(tags, "1.4.0.dev0") is None
    assert rt.previous(tags, (1, 3, 1)) == "v1.3.0"


def test_tree_version_and_channel(tmp_path):
    (tmp_path / "alpaca").mkdir()
    assert rt.tree_version(tmp_path) is None and rt.tree_channel(tmp_path) is None
    (tmp_path / "alpaca" / "__init__.py").write_text('"""doc"""\nVERSION = "1.3.0"\n')
    (tmp_path / "alpaca" / "_channel.py").write_text('# note\nCHANNEL = "release"\n')
    assert rt.tree_version(tmp_path) == "1.3.0" and rt.tree_channel(tmp_path) == "release"
    (tmp_path / "alpaca" / "__init__.py").write_text('VERSION = "1.3"\n')
    assert rt.tree_version(tmp_path) is None


def test_git_env_turns_every_prompt_off_and_keeps_the_base():
    env = rt.git_env({"KEEP": "1"})
    assert env["KEEP"] == "1" and env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == "true" and env["SSH_ASKPASS"] == "true"
    assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]
    assert "PATH" in rt.git_env()


def test_ls_remote_reads_a_local_repository_with_an_annotated_tag(tmp_path):
    repo = fake_repo(tmp_path, ["v1.3.0", "v1.10.0", "v1.9.0", "nightly", "v1.4.0-rc1"], annotated=("v1.10.0",))
    assert rt.ls_remote(repo) == ["v1.3.0", "v1.9.0", "v1.10.0"]


def test_ls_remote_of_a_missing_path_is_unreachable_with_a_reason(tmp_path):
    with pytest.raises(rt.Unreachable, match="git ls-remote .*missing failed: "):
        rt.ls_remote("file://" + str(tmp_path / "missing"))


def test_ls_remote_without_git_and_on_timeout():
    def no_git(*a, **k):
        raise FileNotFoundError("git")

    def hangs(*a, **k):
        raise subprocess.TimeoutExpired("git", 3)

    with pytest.raises(rt.Unreachable, match="git is not installed"):
        rt.ls_remote("x", run=no_git)
    with pytest.raises(rt.Unreachable, match="timed out after 3s"):
        rt.ls_remote("x", timeout=3, run=hangs)


def test_ls_remote_runs_git_without_prompts_or_stdin():
    seen = {}

    def run(argv, **kw):
        seen.update(argv=argv, **kw)
        return subprocess.CompletedProcess(argv, 0, stdout="%s\trefs/tags/v1.0.0\n" % SHA, stderr="")

    assert rt.ls_remote("https://example.invalid/r.git", run=run) == ["v1.0.0"]
    assert seen["argv"][:4] == ["git", "-c", "credential.helper=", "ls-remote"]
    assert seen["stdin"] == subprocess.DEVNULL and seen["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_ls_remote_puts_the_address_after_a_double_dash():
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")
    assert rt.ls_remote("--upload-pack=x", run=run) == []
    assert seen[0][-2:] == ["--", "--upload-pack=x"]
