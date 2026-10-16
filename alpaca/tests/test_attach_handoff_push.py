"""The push of `alpaca attach handoff --execute` carries the approved refspec and nothing else (op-062 t-686, fix round 1, B1).

The adversarial check found that `push.followTags` in the clone's config sent an unapproved tag ref and an unscanned tag
object. The class is "the push trusts the clone": its config, its refs, its hooks and a window between the last look and the
push. The push now runs from a bare folder made for it (no refs, no config of the clone, no hooks) that holds a checked copy
of exactly the scanned objects (fix round 2, B3, replaced round 1's `objects/info/alternates` line naming the clone's objects;
`test_attach_push_store.py`), names exactly one refspec, carries the off value of every setting that adds a ref,
an object or a program, and is checked afterwards (the push report and the upstream's whole ref list before and after).

Each test plants its key in the one window the config digest cannot see, between the last look and the push, so the push
itself is what is under test. A local bare upstream and a fake GitHub API; no network."""
import os
import stat
from pathlib import Path

import pytest

from alpaca import barrier
from alpaca.attach import AttachError, gitops, handoff, safefs, scan
from alpaca.tests.attachkit import (FakeGitHub, attached, git, owner_approve, ready, remote_branches, studio,  # noqa: F401
                                    upstream)

TASK = "t-001"
BRANCH = "alpaca/t-001"
REF = "refs/heads/" + BRANCH
TAGGER = {"GIT_COMMITTER_NAME": "Alpaca agent", "GIT_COMMITTER_EMAIL": "alpaca-agent@localhost"}


@pytest.fixture
def r(attached):
    return ready(attached)


@pytest.fixture
def gh(r):
    return FakeGitHub(upstream=r.att.upstream)


def reviewed(r):
    owner_approve(r)
    return handoff.plan(r.root, TASK)


def go(r, gh):
    return handoff.execute(r.root, TASK, expect=r.head, api=gh)


def only_the_branch(r):
    """The bare upstream's refs are the branch and main, and no object of it is a tag."""
    names = sorted(git(r.att.upstream.bare, "for-each-ref", "--format=%(refname)").split())
    assert names == [REF, "refs/heads/main"], names
    assert "tag" not in object_types(r.att.upstream.bare).values()


def object_types(bare) -> dict:
    out = git(bare, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)")
    return dict(line.split(" ") for line in out.splitlines() if line.strip())


def at_the_push(monkeypatch, plant):
    """Run `plant()` in the window between the last config look and the push: when execute asks the upstream for the
    branch the second time (the first is `_build`'s, the second is the line before the push)."""
    real = handoff._remote_sha
    seen = []

    def racing(app, url, name):
        if name == REF:
            seen.append(name)
            if len(seen) == 2:
                plant()
        return real(app, url, name)

    monkeypatch.setattr(handoff, "_remote_sha", racing)


def plant_follow_tags(r):
    git(r.att.app, "config", "--local", "push.followTags", "true")
    git(r.att.app, "tag", "-a", "-m", "tag message", "v-studio", r.head, env=TAGGER)


def push_folders(r) -> list:
    return safefs.list_names(r.root, handoff.PUSH_DIR)


# ---- the class: whatever the clone holds, the push carries the approved refspec -----------------------------------------

def test_push_follow_tags_planted_before_the_push_sends_no_tag(r, gh, monkeypatch):
    reviewed(r)
    at_the_push(monkeypatch, lambda: plant_follow_tags(r))
    assert go(r, gh).status == "PUSHED"
    assert git(r.att.app, "config", "--local", "--get", "push.followTags") == "true"        # it was planted
    assert git(r.att.app, "tag") == "v-studio"
    only_the_branch(r)


def test_a_tag_made_by_the_repositorys_own_pre_push_hook_is_not_pushed(r, gh):
    """Without a config key the tag is not even sent by followTags; the push names one refspec."""
    path = Path(r.att.app) / ".git" / "hooks" / "pre-push"
    path.parent.mkdir(exist_ok=True)
    path.write_text('#!/bin/sh\ngit -c user.name=h -c user.email=h@example.org -C "%s" tag -a -m hook v-hook %s\n'
                    % (r.att.app, r.head))
    path.chmod(0o755)
    reviewed(r)
    assert go(r, gh).status == "PUSHED"
    assert git(r.att.app, "tag") == "v-hook"
    only_the_branch(r)


def test_a_hook_that_plants_the_key_is_stopped_by_the_last_look(r, gh):
    path = Path(r.att.app) / ".git" / "hooks" / "pre-push"
    path.parent.mkdir(exist_ok=True)
    path.write_text('#!/bin/sh\ngit -C "%s" config --local push.followTags true\n' % r.att.app)
    path.chmod(0o755)
    reviewed(r)
    with pytest.raises(AttachError, match="git config changed since attach"):
        go(r, gh)
    assert remote_branches(r.att.upstream) == ["main"] and gh.calls == []
    assert push_folders(r) == []


def test_push_options_in_the_clones_config_are_not_sent(r, gh, monkeypatch, tmp_path):
    bare = r.att.upstream.bare
    git(bare, "config", "receive.advertisePushOptions", "true")
    log = tmp_path / "options.log"
    hook = Path(bare) / "hooks" / "pre-receive"
    hook.write_text('#!/bin/sh\necho "count=${GIT_PUSH_OPTION_COUNT:-0}" >> "%s"\n' % log)
    hook.chmod(0o755)
    reviewed(r)
    at_the_push(monkeypatch, lambda: git(r.att.app, "config", "--local", "push.pushOption", "evil=1"))
    assert go(r, gh).status == "PUSHED"
    assert log.read_text() == "count=0\n"


def test_a_signed_push_asked_for_by_the_clones_config_runs_no_program(r, gh, monkeypatch, tmp_path):
    marker = tmp_path / "gpg-ran"
    program = tmp_path / "gpg"
    program.write_text('#!/bin/sh\ntouch "%s"\nexit 1\n' % marker)
    program.chmod(0o755)

    def plant():
        git(r.att.app, "config", "--local", "push.gpgSign", "true")
        git(r.att.app, "config", "--local", "gpg.program", str(program))

    reviewed(r)
    at_the_push(monkeypatch, plant)
    assert go(r, gh).status == "PUSHED"
    assert not marker.exists()
    only_the_branch(r)


def test_a_config_that_names_the_remote_and_a_mirror_refspec_sends_nothing_more(r, gh, monkeypatch):
    def plant():
        git(r.att.app, "config", "--local", "remote.origin.mirror", "true")
        git(r.att.app, "config", "--local", "remote.origin.push", "+refs/*:refs/*")
        git(r.att.app, "config", "--local", "push.default", "matching")
        git(r.att.app, "tag", "-a", "-m", "m", "v-x", r.head, env=TAGGER)

    reviewed(r)
    at_the_push(monkeypatch, plant)
    assert go(r, gh).status == "PUSHED"
    only_the_branch(r)


# ---- the shape of the push --------------------------------------------------------------------------------------------

def test_the_push_names_one_refspec_with_every_off_switch_and_runs_from_its_own_folder(r, gh, monkeypatch):
    reviewed(r)
    seen = {}
    real = gitops.remote

    def spy(repo, *args, **kw):
        if args and args[0] == "push":
            seen.update(repo=str(repo), args=args, kw=kw)
            folder = Path(repo)
            seen["config"] = (folder / "config").read_text()
            seen["refs"] = [p for p in (folder / "refs").rglob("*") if p.is_file()]
            seen["packed"] = (folder / "packed-refs").exists()
            seen["hooks"] = (folder / "hooks").exists()
            seen["alternates"] = (folder / "objects" / "info" / "alternates").exists()
            seen["files"] = sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())
        return real(repo, *args, **kw)

    monkeypatch.setattr(gitops, "remote", spy)
    go(r, gh)
    args = seen["args"]
    studio_root = os.path.realpath(str(r.root))
    assert seen["repo"].startswith(os.path.join(studio_root, handoff.PUSH_DIR) + os.sep)
    assert not seen["repo"].startswith(os.path.realpath(str(r.att.app)))
    assert args[0] == "push" and args.count("--") == 1
    for flag in ("--porcelain", "--no-verify", "--no-follow-tags", "--no-signed", "--recurse-submodules=no",
                 "--force-with-lease=%s:" % REF, "--no-thin"):
        assert flag in args, flag
    assert args[args.index("--") + 1:] == (r.att.upstream.url, "%s:%s" % (r.head, REF))     # the url and one refspec
    assert tuple(seen["kw"]["overrides"]) == handoff.PUSH_OFF and "push.followTags=false" in handoff.PUSH_OFF
    # fix round 2 (B3): no commit-graph, multi-pack-index, bitmap or sparse walk, and no shallow file
    for off in ("core.commitGraph=false", "core.multiPackIndex=false", "pack.useBitmaps=false", "pack.useSparse=false"):
        assert off in handoff.PUSH_OFF, off
    assert seen["kw"]["env_extra"] == {"GIT_SHALLOW_FILE": scan.NO_SHALLOW}
    # the folder it ran in held nothing of the clone but a checked copy of the objects: no link to the clone's object folder
    assert "[push" not in seen["config"] and "[remote" not in seen["config"] and "[submodule" not in seen["config"]
    assert seen["refs"] == [] and not seen["packed"] and not seen["hooks"] and not seen["alternates"]
    assert all(f in ("HEAD", "config") or f.startswith("objects/pack/pack-") for f in seen["files"]), seen["files"]
    assert push_folders(r) == []                                                           # taken back afterwards


def test_the_push_folder_is_made_after_the_repositorys_pre_push_hook_has_run(r, gh, tmp_path):
    out = tmp_path / "listing"
    path = Path(r.att.app) / ".git" / "hooks" / "pre-push"
    path.parent.mkdir(exist_ok=True)
    path.write_text('#!/bin/sh\nls -A "%s" > "%s" 2>&1\nexit 0\n' % (Path(r.root) / handoff.PUSH_DIR, out))
    path.chmod(0o755)
    reviewed(r)
    go(r, gh)
    assert "No such file" in out.read_text() or out.read_text().strip() == ""


def test_the_push_folder_is_removed_when_the_push_fails(r, gh, monkeypatch):
    reviewed(r)
    real = handoff._remote_sha
    seen = []

    def racing(app, url, name):
        if name == REF:
            seen.append(name)
            if len(seen) == 2:                                                      # an ancestor: only the lease refuses
                git(r.att.upstream.bare, "branch", BRANCH, "main")
        return real(app, url, name)

    monkeypatch.setattr(handoff, "_remote_sha", racing)
    with pytest.raises(AttachError):
        go(r, gh)
    assert push_folders(r) == [] and gh.calls == []


def test_the_clone_object_folder_must_be_the_clones_own(r, gh, tmp_path):
    reviewed(r)
    other = tmp_path / "objects-elsewhere"
    other.mkdir()
    objects = Path(r.att.app) / ".git" / "objects"
    moved = Path(r.att.app) / ".git" / "objects-moved"
    objects.rename(moved)
    objects.symlink_to(moved)
    try:
        with pytest.raises(AttachError):
            go(r, gh)
    finally:
        objects.unlink()
        moved.rename(objects)
    assert remote_branches(r.att.upstream) == ["main"] and push_folders(r) == []


def test_what_enters_the_upstream_is_what_the_scan_read(r, gh):
    """Every object the push added to the upstream is an object the scan listed as entering (commits, trees, blobs)."""
    before = set(object_types(r.att.upstream.bare))
    reviewed(r)
    go(r, gh)
    added = set(object_types(r.att.upstream.bare)) - before
    base = git(r.att.app, "rev-parse", r.ck.preview_base)
    scanned = {sha for sha, _p in barrier.objects_entering(str(r.att.app), r.head, base)}
    assert added and added == scanned


# ---- the check after the push ------------------------------------------------------------------------------------------

def server_hook(r, body):
    hook = Path(r.att.upstream.bare) / "hooks" / "post-receive"
    hook.write_text("#!/bin/sh\n" + body)
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)


def test_an_extra_ref_that_appears_with_the_push_is_reported_and_no_pull_request_is_opened(r, gh):
    """The upstream creates a tag when the branch arrives (a server hook stands for a ref the push should not cause)."""
    server_hook(r, 'git update-ref refs/tags/server-made "$(git rev-parse refs/heads/main)"\n')
    reviewed(r)
    with pytest.raises(AttachError, match="differ from the approved push in 1 name.*refs/tags/server-made") as seen:
        go(r, gh)
    assert "no pull request was opened" in str(seen.value) and "PUSHED_UNVERIFIED" in str(seen.value)
    assert gh.calls == []
    last = handoff.handoffs(r.root, TASK)[-1][1]
    assert last.status == handoff.PUSHED_UNVERIFIED and "refs/tags/server-made" in last.error
    assert push_folders(r) == []
    # a second run does not open the pull request over it, and a merge has nothing to merge
    with pytest.raises(AttachError, match="not shown to be only the approved branch"):
        go(r, gh)
    with pytest.raises(AttachError, match="not shown to be only the approved branch"):
        handoff.plan(r.root, TASK)
    assert gh.calls == []
    with pytest.raises(AttachError, match="no pull request"):
        handoff.merge(r.root, TASK, expect=r.head, execute=True, api=gh)


def test_a_ref_the_upstream_moves_during_the_push_is_reported_too(r, gh):
    server_hook(r, 'git update-ref refs/heads/other "$(git rev-parse refs/heads/main~1)"\n')
    reviewed(r)
    with pytest.raises(AttachError, match="refs/heads/other"):
        go(r, gh)
    assert handoff.handoffs(r.root, TASK)[-1][1].status == handoff.PUSHED_UNVERIFIED and gh.calls == []


def test_a_clean_push_changes_only_the_branch(r, gh):
    before = git(r.att.upstream.bare, "for-each-ref", "--format=%(refname) %(objectname)")
    reviewed(r)
    assert go(r, gh).status == "PUSHED"
    after = git(r.att.upstream.bare, "for-each-ref", "--format=%(refname) %(objectname)")
    assert set(after.splitlines()) - set(before.splitlines()) == {"%s %s" % (REF, r.head)}


def test_the_push_report_must_be_one_new_branch_at_the_approved_sha():
    head, ref = "a" * 40, REF
    good = "To /x\n*\t%s:%s\t[new branch]\nDone\n" % (head, ref)
    assert handoff._porcelain_problems(good, head, ref) == []
    assert handoff._porcelain_problems("To /x\nDone\n", head, ref)                          # no ref line
    assert handoff._porcelain_problems(good + "*\trefs/tags/x:refs/tags/x\t[new tag]\n", head, ref)       # a second ref
    assert handoff._porcelain_problems("To /x\n!\t%s:%s\t[rejected]\nDone\n" % (head, ref), head, ref)     # not new
    assert handoff._porcelain_problems("To /x\n*\t%s:refs/heads/other\t[new branch]\n" % head, head, ref)   # another name
    assert handoff._porcelain_problems("To /x\n*\t%s:%s\t[new branch]\n" % ("b" * 40, ref), head, ref)      # another sha


def test_the_ref_comparison_names_every_added_changed_or_removed_ref():
    head = "a" * 40
    before = {"HEAD": "1" * 40, "refs/heads/main": "1" * 40, "refs/tags/t": "2" * 40}
    expected = dict(before, **{REF: head})
    assert handoff._ref_changes(before, expected, REF, head) == []
    assert handoff._ref_changes(before, dict(expected, **{"refs/tags/new": "3" * 40}), REF, head) == ["refs/tags/new"]
    assert handoff._ref_changes(before, dict(expected, **{"refs/tags/t": "4" * 40}), REF, head) == ["refs/tags/t"]
    assert handoff._ref_changes(before, {k: v for k, v in expected.items() if k != "refs/tags/t"}, REF, head) == ["refs/tags/t"]
    assert handoff._ref_changes(before, before, REF, head) == [REF]                         # the branch did not arrive


def test_an_upstream_ref_list_that_does_not_parse_refuses_before_the_push(r, gh, monkeypatch):
    reviewed(r)
    real = gitops.remote

    def garbled(repo, *args, **kw):
        if args and args[0] == "ls-remote" and len(args) == 3:                            # ls-remote -- <url>, every ref
            return "this is not a ref list"
        return real(repo, *args, **kw)

    monkeypatch.setattr(gitops, "remote", garbled)
    with pytest.raises(AttachError, match="ref list could not be read"):
        go(r, gh)
    assert remote_branches(r.att.upstream) == ["main"] and push_folders(r) == []
