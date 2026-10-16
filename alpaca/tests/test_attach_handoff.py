"""`alpaca attach handoff`: the dry run, the push and the pull request (op-062 t-686, AC-09; build plan 6.8, P6, P7, P13 to P19).

A local bare upstream and a fake GitHub API (`attachkit.FakeGitHub`); no network. Every refusal test ends by asserting
that nothing reached the upstream: its branches are still `["main"]`."""
import base64
import json
import stat
import sys
from pathlib import Path

import pytest

from alpaca import cli, db
from alpaca.attach import AttachError, approval, check, config, github, gitops, handoff, proven
from alpaca.release.receipts import ApprovalReceipt, record_receipt
from alpaca.tests.attachkit import (REPOSITORY, FakeGitHub, agent_approve, attached, commit_in, git, owner_approve,  # noqa: F401
                                    project_yaml, ready, record_decision, remote_branches, set_attach, set_gates,
                                    set_repository, studio, upstream, write_delegation)

TASK = "t-001"
BRANCH = "alpaca/t-001"


def untouched(r):
    """Nothing reached the upstream: its only branch is main."""
    assert remote_branches(r.att.upstream) == ["main"]


@pytest.fixture
def r(attached):
    return ready(attached)


@pytest.fixture
def gh(r):
    return FakeGitHub(upstream=r.att.upstream)


def approved(r):
    owner_approve(r)
    return r


def reviewed(r, **kw):
    """Owner approval and a dry run: what a person does before `--execute`."""
    approved(r)
    return handoff.plan(r.root, TASK, **kw)


def go(r, gh, **kw):
    return handoff.execute(r.root, TASK, expect=kw.pop("expect", r.head), api=gh, **kw)


def receipts(root, kind="attach-handoff"):
    conn = db.connect(str(root))
    try:
        return [e for e in db.events(conn, limit=10 ** 9) if e["kind"] == "release-" + kind]
    finally:
        conn.close()


def install_hook(att, body, tmp_path=None):
    path = Path(att.app) / ".git" / "hooks" / "pre-push"
    path.parent.mkdir(exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


# ---- AC-09: no approval of the exact sha, no --expect ---------------------------------------------------------------------

def test_a_handoff_without_an_approval_pushes_nothing(r, gh):
    """AC-09."""
    with pytest.raises(AttachError, match="no current approval"):
        handoff.plan(r.root, TASK)
    with pytest.raises(AttachError, match="no current approval"):
        go(r, gh)
    untouched(r)
    assert gh.calls == [] and receipts(r.root) == []


def test_an_approval_of_another_sha_does_not_cover_this_head(r, gh):
    """AC-09: the approval is of exactly one commit."""
    old = r.head
    owner_approve(r)
    commit_in(r.wt, {"src/more.py": "x = 2\n"}, "one more change")
    new_ck = check.run(r.root, TASK)
    assert new_ck.verdict == "PASS" and new_ck.head != old
    with pytest.raises(AttachError, match="no current approval of %s" % new_ck.head[:12]):
        handoff.plan(r.root, TASK)
    with pytest.raises(AttachError, match="no current approval"):
        handoff.execute(r.root, TASK, expect=new_ck.head, api=gh)
    untouched(r)


def test_a_push_without_expect_or_with_another_sha_is_refused(r, gh):
    """AC-09."""
    reviewed(r)
    for expect in (None, "", "abc", r.head[:12], "f" * 40, r.head.upper()):
        with pytest.raises(AttachError):
            handoff.execute(r.root, TASK, expect=expect, api=gh)
    untouched(r)
    assert gh.calls == []


def test_the_verb_refuses_execute_without_expect(r, gh, capsys, monkeypatch):
    """AC-09 through the CLI."""
    monkeypatch.setattr(github, "default_api", gh)
    reviewed(r)
    assert cli.main(["attach", "handoff", TASK, "--execute"]) == cli.BLOCKED
    assert "--expect <sha> is required" in capsys.readouterr().out
    assert cli.main(["attach", "handoff", TASK, "--expect", r.head]) == cli.BLOCKED
    assert "belongs with --execute" in capsys.readouterr().out
    untouched(r)


def test_a_push_needs_a_reviewed_dry_run(r, gh):
    approved(r)
    with pytest.raises(AttachError, match="no dry run"):
        go(r, gh)
    untouched(r)


# ---- the dry run ----------------------------------------------------------------------------------------------------------

def test_the_dry_run_lists_the_handoff_and_pushes_nothing(r, gh, capsys):
    approved(r)
    assert cli.main(["attach", "handoff", TASK]) == cli.PASS
    out = capsys.readouterr().out
    for text in ("alpaca/t-001", "add a module", "A src/feature.py", "ok: pass / pass", "merge preview: no conflicts",
                 "0 commit(s) behind", r.head[:12], "approval: owner", "pre-push hook: none in the repository", "GATE alpaca-attach-handoff: PASS",
                 "--execute --expect " + r.head):
        assert text in out, text
    untouched(r)
    rows = receipts(r.root)
    assert len(rows) == 1 and rows[0]["data"]["status"] == "DRY_RUN"
    assert gh.calls == []


def test_the_plan_holds_every_field_the_push_must_agree_on(r):
    info = reviewed(r, title="A good title", body="Why: because.")
    out = info["outgoing"]
    assert (out["repository"], out["url"], out["branch"], out["head"], out["base"]) == (
        REPOSITORY, r.att.upstream.url, BRANCH, r.head, "main")
    assert out["preview_base"] == r.ck.preview_base == r.att.upstream.sha() and out["behind"] == 0 and out["conflicts"] == []
    assert out["commits_total"] == 1 and out["files"] == ["A src/feature.py"] and out["title"] == "A good title"
    assert out["gates"] == [{"name": "ok", "head": True, "preview": True, "red_at_base": False}]
    assert out["gate_set"] == config.load(r.root).gate_set_digest() and out["approval"]["approver"] == "owner"
    assert "Why: because." in info["body"] and "Alpaca agent <alpaca-agent@localhost>" in info["body"]
    assert len(out["body_sha256"]) == 64


def test_the_dry_run_counts_the_commits_the_branch_is_behind(r):
    r.att.upstream.commit({"other.txt": "x\n"}, "somebody else's work")
    ck = check.run(r.root, TASK)
    assert ck.verdict == "PASS"
    out = reviewed(r)["outgoing"]
    assert out["behind"] == 1 and out["preview_base"] == r.att.upstream.sha()


def test_a_branch_with_no_commit_of_its_own_has_nothing_to_hand_off(attached):
    from alpaca.tests.attachkit import add_worktree, with_term_list
    with_term_list(attached)
    set_repository(attached.root)
    set_gates(attached.root, [{"name": "ok", "command": ["python3", "-B", "-c", "pass"], "timeout": 60}])
    wt = add_worktree(attached, TASK)
    head = git(wt, "rev-parse", "HEAD")
    check.run(str(attached.root), TASK)
    approval.approve(str(attached.root), TASK, head, approver="owner")
    with pytest.raises(AttachError, match="nothing to hand off"):
        handoff.plan(str(attached.root), TASK)


# ---- the push -------------------------------------------------------------------------------------------------------------

def test_the_approved_branch_is_pushed_and_its_pull_request_opened(r, gh):
    reviewed(r, title="Add the feature")
    rec = go(r, gh, title="Add the feature")
    assert rec.status == "PUSHED" and rec.pr_number == 1 and rec.pr_url == "https://github.com/acme/demo/pull/1"
    assert remote_branches(r.att.upstream) == [BRANCH, "main"]
    assert git(r.att.upstream.bare, "rev-parse", "refs/heads/" + BRANCH) == r.head
    pr = gh.prs[1]
    assert (pr["head"]["ref"], pr["base"]["ref"], pr["title"]) == (BRANCH, "main", "Add the feature")
    assert "Alpaca agent <alpaca-agent@localhost>" in pr["body"] and r.head in pr["body"]
    assert [c[0] for c in gh.calls] == ["GET", "POST"]
    assert receipts(r.root)[-1]["data"]["status"] == "PUSHED"
    assert handoff.handoffs(r.root, TASK, ("PUSHED",))[0][1].outgoing["head"] == r.head


def test_the_verb_pushes_and_prints_the_pull_request(r, gh, capsys, monkeypatch):
    monkeypatch.setattr(github, "default_api", gh)
    reviewed(r)
    assert cli.main(["attach", "handoff", TASK, "--execute", "--expect", r.head, "--json"]) == cli.PASS
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "PASS" and out["receipt"]["pr_url"].endswith("/pull/1") and out["receipt"]["head"] == r.head
    assert remote_branches(r.att.upstream) == [BRANCH, "main"]


def test_the_push_goes_to_the_recorded_url_and_never_through_the_remote_name(r, gh, monkeypatch):
    """P6: remote.origin.pushurl is the trap; the handoff names the URL itself, with a lease on a branch that must not exist."""
    app = str(r.att.app)
    assert git(app, "config", "--local", "--get", "remote.origin.pushurl") == "alpaca-handoff-only:refused"
    assert gitops.run(app, "push", "origin", "%s:refs/heads/x" % r.head).rc != 0               # the trap holds
    calls = []
    real = gitops.remote

    def spy(repo, *args, **kw):
        calls.append(args)
        return real(repo, *args, **kw)

    monkeypatch.setattr(gitops, "remote", spy)
    reviewed(r)
    go(r, gh)
    push = [a for a in calls if a[0] == "push"]
    assert len(push) == 1
    args = push[0]
    assert "origin" not in args and r.att.upstream.url in args and "--no-verify" in args
    assert "--force-with-lease=refs/heads/%s:" % BRANCH in args and "%s:refs/heads/%s" % (r.head, BRANCH) in args
    assert remote_branches(r.att.upstream) == [BRANCH, "main"]


def test_a_branch_that_appears_upstream_between_the_check_and_the_push_is_not_overwritten(r, gh, monkeypatch):
    """P14: the lease expects the ref not to exist."""
    reviewed(r)
    real = gitops.remote
    seen = {}

    def racing(repo, *args, **kw):
        if args and args[0] == "push":
            # an ancestor of the head: a plain push would fast-forward it, only the lease refuses
            git(r.att.upstream.bare, "branch", BRANCH, "main")
            seen["sha"] = git(r.att.upstream.bare, "rev-parse", "refs/heads/" + BRANCH)
        return real(repo, *args, **kw)

    monkeypatch.setattr(gitops, "remote", racing)
    with pytest.raises(AttachError, match="git push failed"):
        go(r, gh)
    assert git(r.att.upstream.bare, "rev-parse", "refs/heads/" + BRANCH) == seen["sha"] != r.head
    assert gh.calls == [] and not handoff.handoffs(r.root, TASK, ("PUSHED", "PUSHED_NO_PR"))


def test_an_existing_remote_branch_is_not_overwritten(r, gh):
    """AC-09 / P14: the branch is already there, so a handoff is a new decision."""
    reviewed(r)
    other = r.att.upstream.branch_off(BRANCH, {"theirs.txt": "x\n"}, "somebody's branch")
    with pytest.raises(AttachError, match="already exists upstream"):
        go(r, gh)
    with pytest.raises(AttachError, match="already exists upstream"):
        handoff.plan(r.root, TASK)
    assert git(r.att.upstream.bare, "rev-parse", "refs/heads/" + BRANCH) == other and gh.calls == []


def test_a_second_handoff_of_a_pushed_task_is_refused(r, gh):
    reviewed(r)
    go(r, gh)
    with pytest.raises(AttachError, match="already exists upstream"):
        handoff.plan(r.root, TASK)
    with pytest.raises(AttachError, match="already exists upstream"):
        go(r, gh)
    assert len(gh.calls_of("POST")) == 1


def test_a_branch_that_moved_after_the_dry_run_pushes_nothing(r, gh):
    """PM t-654.2."""
    reviewed(r)
    commit_in(r.wt, {"src/late.py": "x = 3\n"}, "a late change")
    with pytest.raises(AttachError, match="check of %s judged" % TASK):
        go(r, gh)
    untouched(r)


def test_a_late_change_that_is_checked_and_approved_still_needs_a_new_dry_run(r, gh):
    reviewed(r)
    new = commit_in(r.wt, {"src/late.py": "x = 3\n"}, "a late change")
    check.run(r.root, TASK)
    approval.approve(r.root, TASK, new, approver="owner")
    with pytest.raises(AttachError, match="latest dry run of %s is of %s" % (TASK, r.head[:12])):
        handoff.execute(r.root, TASK, expect=new, api=gh)
    untouched(r)
    handoff.plan(r.root, TASK)
    assert handoff.execute(r.root, TASK, expect=new, api=gh).outgoing["head"] == new


def test_a_revocation_after_the_dry_run_blocks_the_push(r, gh):
    reviewed(r)
    approval.approve(r.root, TASK, r.head, approver="owner", decision="revoke")
    with pytest.raises(AttachError, match="no current approval"):
        go(r, gh)
    untouched(r)


def test_a_plan_that_changed_since_the_dry_run_is_refused(r, gh, tmp_path):
    reviewed(r, title="First title")
    with pytest.raises(AttachError, match=r"plan changed since the dry run \(.*title"):
        go(r, gh, title="Another title")
    body = tmp_path / "body.md"
    body.write_text("A different body.\n")
    with pytest.raises(AttachError, match="body_sha256"):
        go(r, gh, title="First title", body_file=str(body))
    untouched(r)
    assert go(r, gh, title="First title").status == "PUSHED"


def test_the_body_file_is_the_top_of_the_body_and_its_digest_is_pinned(r, gh, tmp_path):
    body = tmp_path / "body.md"
    body.write_text("Context from the owner.\n")
    reviewed(r, body_file=str(body))
    go(r, gh, body_file=str(body))
    assert gh.prs[1]["body"].startswith("Context from the owner.\n")
    assert (tmp_path / "body.md").read_text() == "Context from the owner.\n"


def test_a_body_file_that_is_not_a_plain_file_is_refused(r, tmp_path):
    approved(r)
    link = tmp_path / "link.md"
    link.symlink_to(tmp_path)
    for path in (tmp_path / "missing.md", link, tmp_path):
        with pytest.raises(AttachError, match="body file"):
            handoff.plan(r.root, TASK, body_file=str(path))


def test_an_untitled_handoff_takes_the_subject_of_the_newest_commit(r):
    assert reviewed(r)["outgoing"]["title"] == "add a module"
    with pytest.raises(AttachError, match="title"):
        handoff.plan(r.root, TASK, title="two\nlines")


# ---- what the check must say ----------------------------------------------------------------------------------------------

def test_a_handoff_needs_a_check_of_this_head(attached):
    from alpaca.tests.attachkit import add_worktree, with_term_list
    with_term_list(attached)
    set_repository(attached.root)
    set_gates(attached.root, [{"name": "ok", "command": ["python3", "-B", "-c", "pass"], "timeout": 60}])
    wt = add_worktree(attached, TASK)
    head = commit_in(wt, {"a.py": "x = 1\n"}, "a change")
    approval.approve(str(attached.root), TASK, head, approver="owner")
    with pytest.raises(AttachError, match="has not been checked"):
        handoff.plan(str(attached.root), TASK)


def test_a_failed_check_cannot_be_handed_off(attached):
    from alpaca.tests.attachkit import add_worktree, with_term_list
    with_term_list(attached)
    set_repository(attached.root)
    gate = {"name": "test", "command": ["python3", "-B", "-c", "import os, sys; sys.exit(1 if os.path.exists('broken') else 0)"],
            "timeout": 60}
    set_gates(attached.root, [gate])
    wt = add_worktree(attached, TASK)
    head = commit_in(wt, {"broken": "x\n"}, "break it")
    assert check.run(str(attached.root), TASK).verdict == "FAIL"
    approval.approve(str(attached.root), TASK, head, approver="owner")
    with pytest.raises(AttachError, match="did not pass"):
        handoff.plan(str(attached.root), TASK)
    assert remote_branches(attached.upstream) == ["main"]


def test_the_upstream_base_that_moved_since_the_check_refuses(r, gh):
    """P17 / PM t-649.6."""
    approved(r)
    r.att.upstream.commit({"other.txt": "x\n"}, "somebody else's work")
    with pytest.raises(AttachError, match="moved since the check"):
        handoff.plan(r.root, TASK)
    untouched(r)
    check.run(r.root, TASK)                                          # the remedy the message names
    assert handoff.plan(r.root, TASK)["outgoing"]["behind"] == 1


def test_a_base_that_moves_after_the_dry_run_refuses_the_push(r, gh):
    reviewed(r)
    r.att.upstream.commit({"other.txt": "x\n"}, "somebody else's work")
    with pytest.raises(AttachError, match="moved since the check"):
        go(r, gh)
    untouched(r)


def test_a_gate_set_changed_since_the_check_refuses(r):
    """PM t-649.5."""
    set_gates(r.root, [{"name": "ok", "command": ["python3", "-B", "-c", "import sys; sys.exit(0)"], "timeout": 99}])
    approval.approve(r.root, TASK, r.head, approver="owner")
    with pytest.raises(AttachError, match="gates changed since the check"):
        handoff.plan(r.root, TASK)
    untouched(r)


def test_an_unclean_work_folder_refuses(r):
    approved(r)
    (Path(r.wt) / "scratch.txt").write_text("x")
    with pytest.raises(AttachError, match="uncommitted changes"):
        handoff.plan(r.root, TASK)


# ---- red at base (AC-06, P16) ---------------------------------------------------------------------------------------------

def red_after_the_approval(r):
    """An agent approval of a green head, then upstream breaks the gate: the new check marks the preview base red."""
    gate = {"name": "test", "command": ["python3", "-B", "-c", "import os, sys; sys.exit(1 if os.path.exists('broken') else 0)"],
            "timeout": 60}
    set_gates(r.root, [gate])
    r.ck = check.run(r.root, TASK)
    agent_approve(r)
    r.att.upstream.commit({"broken": "red upstream\n"}, "upstream breaks the test")
    r.ck = check.run(r.root, TASK)
    assert r.ck.verdict == "PASS" and r.ck.needs_owner_review and r.ck.red_at_preview_base == ["test"]


def test_red_at_base_with_an_agent_approval_refuses_and_with_an_owner_approval_passes(r, gh):
    red_after_the_approval(r)
    with pytest.raises(AttachError, match="must be the owner's"):
        handoff.plan(r.root, TASK)
    untouched(r)
    owner_approve(r)
    out = handoff.plan(r.root, TASK)["outgoing"]
    assert out["needs_owner_review"] and out["red_at_base"] == ["test"] and out["gates"][0]["red_at_base"]
    rec = go(r, gh)
    assert rec.status == "PUSHED" and "red at base: test" in gh.prs[1]["body"]


# ---- what an approval is bound to -----------------------------------------------------------------------------------------

def test_an_approval_with_the_remote_url_changed_refuses(r, tmp_path):
    """PM t-654.1."""
    reviewed(r)
    set_attach(r.root, dict(project_yaml(r.root)["attach"], url=str(tmp_path / "another.git")))
    with pytest.raises(AttachError, match="differs from the URL recorded"):
        handoff.plan(r.root, TASK)
    untouched(r)


def test_a_changed_git_config_of_the_clone_refuses(r, gh):
    """P8: an insteadOf rule would send the push elsewhere."""
    reviewed(r)
    git(r.att.app, "config", "--local", "url.%s.insteadOf" % (r.att.upstream.url + "-elsewhere"), r.att.upstream.url)
    with pytest.raises(AttachError, match="git config changed since attach"):
        go(r, gh)
    untouched(r)


def test_an_owner_approval_that_does_not_cover_pr_refuses(r):
    owner_approve(r, actions="push")
    with pytest.raises(AttachError, match="does not cover pr"):
        handoff.plan(r.root, TASK)


def test_a_release_approval_of_the_same_sha_does_not_authorize_a_handoff(r, gh):
    """PM t-654.1."""
    record_receipt(r.root, "cli", ApprovalReceipt(r.head, "owner", "approve", "0" * 40, "", "", "APPROVED"))
    with pytest.raises(AttachError, match="no current approval"):
        handoff.plan(r.root, TASK)
    untouched(r)


def test_a_delegation_without_the_line_or_for_another_repository_refuses(r):
    record_decision(r.root)
    for line in ("no line here", "attach-delegation: dec-049934c5931f other/repo push,pr"):
        with pytest.raises(AttachError):
            agent_approve(r, delegation=write_delegation(r.root, line))
    assert approval.current(r.root, TASK, r.head) is None
    with pytest.raises(AttachError, match="no current approval"):
        handoff.plan(r.root, TASK)


def test_an_agent_approval_whose_delegation_was_withdrawn_refuses(r, gh):
    agent_approve(r)
    handoff.plan(r.root, TASK)
    (Path(r.root) / "docs" / "delegation.md").write_text("# nothing\n")
    with pytest.raises(AttachError, match="no longer holds"):
        go(r, gh)
    untouched(r)


def test_an_agent_with_a_delegation_can_hand_off(r, gh):
    agent_approve(r)
    handoff.plan(r.root, TASK)
    assert go(r, gh).status == "PUSHED"
    assert handoff.plan.__name__ == "plan"


def test_a_project_without_a_repository_name_cannot_open_a_pull_request(r):
    approved(r)
    block = dict(project_yaml(r.root)["attach"])
    block.pop("repository")
    set_attach(r.root, block)
    with pytest.raises(AttachError, match="attach.repository"):
        handoff.plan(r.root, TASK)


# ---- the repository's own pre-push hook (P7) ------------------------------------------------------------------------------

def hook_dump(tmp_path):
    out = tmp_path / "hook"
    return out, ('env > "%s.env"\ncat > "%s.stdin"\necho "$@" > "%s.args"\necho ran >> "%s.count"\nexit 0\n'
                 % (out, out, out, out))


def test_the_token_never_reaches_the_repositorys_pre_push_hook(r, gh, tmp_path, monkeypatch):
    """PM X2: the hook sees an environment without the credential, in any spelling."""
    token = "sentinel-token-5f3a9c"
    for name in ("ALPACA_ATTACH_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "PUBLIC_REPO_TOKEN"):
        monkeypatch.setenv(name, token)
    out, body = hook_dump(tmp_path)
    install_hook(r.att, body)
    info = reviewed(r)
    assert info["hook"] == "hooks/pre-push"
    go(r, gh)
    env = (tmp_path / "hook.env").read_text()
    assert "PATH=" in env and "HOME=" in env                                                 # the dump is real
    for secret in (token, base64.b64encode(token.encode()).decode(), base64.b64encode(("x-access-token:" + token).encode()).decode()):
        assert secret not in env
    assert "ALPACA_ATTACH_TOKEN" not in env and "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env
    assert (tmp_path / "hook.stdin").read_text() == "refs/heads/%s %s refs/heads/%s %s\n" % (BRANCH, r.head, BRANCH, "0" * 40)
    assert (tmp_path / "hook.args").read_text().split() == ["origin", r.att.upstream.url]
    assert (tmp_path / "hook.count").read_text() == "ran\n"                                 # git's own run is off: once only
    assert __import__("os").environ["ALPACA_ATTACH_TOKEN"] == token                           # and the process still has it


def test_a_pre_push_hook_that_exits_nonzero_refuses_the_push(r, gh):
    install_hook(r.att, 'echo "lint failed: line 3" >&2\nexit 1\n')
    reviewed(r)
    with pytest.raises(AttachError, match=r"pre-push hook refused the push \(exit 1\): lint failed: line 3"):
        go(r, gh)
    untouched(r)
    assert gh.calls == [] and not handoff.handoffs(r.root, TASK, ("PUSHED", "PUSHED_NO_PR"))


def test_a_pre_push_hook_that_hangs_is_killed_and_refuses(r, gh, monkeypatch):
    install_hook(r.att, "sleep 30\n")
    reviewed(r)
    monkeypatch.setattr(handoff, "HOOK_TIMEOUT", 1)
    with pytest.raises(AttachError, match="ran out of time"):
        go(r, gh)
    untouched(r)


def test_a_hook_that_is_not_executable_is_not_run(r, gh):
    path = install_hook(r.att, "exit 1\n")
    path.chmod(0o644)
    assert handoff.hook_found(str(r.att.app), r.wt) is None
    reviewed(r)
    assert go(r, gh).status == "PUSHED"


def test_core_hooks_path_of_the_clone_decides_where_the_hook_is(r):
    """A husky-style relative path is relative to the work folder, as git runs hooks."""
    app = str(r.att.app)
    folder = Path(r.wt)
    (folder / ".husky").mkdir()
    hook = folder / ".husky" / "pre-push"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)
    assert handoff.hook_found(app, str(folder)) is None
    git(app, "config", "--local", "core.hooksPath", ".husky")
    assert handoff.hook_found(app, str(folder)) == str(hook)
    git(app, "config", "--local", "core.hooksPath", str(folder / ".husky"))
    assert handoff.hook_found(app, str(folder)) == str(hook)


def test_the_runner_never_runs_a_hook_of_its_own_on_the_push(r, gh, tmp_path):
    """The git push itself runs with hooks off: a hook that would fail on its second call does not."""
    out = tmp_path / "count"
    install_hook(r.att, 'echo x >> "%s"\n[ "$(wc -l < "%s")" -le 1 ] || exit 1\nexit 0\n' % (out, out))
    reviewed(r)
    assert go(r, gh).status == "PUSHED"
    assert out.read_text() == "x\n"


# ---- the pull request step ----------------------------------------------------------------------------------------------

def test_a_pull_request_that_fails_after_the_push_is_retried_without_a_second_push(r, gh, monkeypatch):
    reviewed(r)
    gh.fail[("POST", "/repos/acme/demo/pulls")] = "try later"
    with pytest.raises(AttachError, match="pushed but the pull request step failed"):
        go(r, gh)
    assert remote_branches(r.att.upstream) == [BRANCH, "main"]
    assert [x[1].status for x in handoff.handoffs(r.root, TASK)] == ["DRY_RUN", "PUSHED_NO_PR"]
    pushes = []
    real = gitops.remote
    monkeypatch.setattr(gitops, "remote", lambda repo, *a, **kw: (pushes.append(a), real(repo, *a, **kw))[1])
    rec = go(r, gh)
    assert rec.status == "PUSHED" and rec.pr_number == 1
    assert [a for a in pushes if a[0] == "push"] == []


def test_an_open_pull_request_for_the_same_head_is_reused(r, gh):
    reviewed(r)
    gh.fail[("POST", "/repos/acme/demo/pulls")] = "timed out after it was created"
    with pytest.raises(AttachError):
        go(r, gh)
    gh.prs[7] = {"number": 7, "html_url": "https://github.com/acme/demo/pull/7", "state": "open", "merged_at": None,
                 "merge_commit_sha": None, "head": {"ref": BRANCH, "sha": r.head}, "base": {"ref": "main"}}
    posts = len(gh.calls_of("POST"))
    rec = go(r, gh)
    assert rec.pr_number == 7 and len(gh.calls_of("POST")) == posts


def test_an_open_pull_request_at_another_head_is_not_reused(r, gh):
    reviewed(r)
    gh.fail[("POST", "/repos/acme/demo/pulls")] = "down"
    with pytest.raises(AttachError):
        go(r, gh)
    gh.prs[3] = {"number": 3, "html_url": "x", "state": "open", "merged_at": None, "merge_commit_sha": None,
                 "head": {"ref": BRANCH, "sha": "a" * 40}, "base": {"ref": "main"}}
    with pytest.raises(AttachError, match="another head or base"):
        go(r, gh)


def test_a_revocation_between_the_push_and_the_pull_request_leaves_the_branch_and_opens_nothing(r, gh, monkeypatch):
    reviewed(r)
    real = gitops.remote

    def revoke_after_push(repo, *args, **kw):
        done = real(repo, *args, **kw)
        if args and args[0] == "push":
            approval.approve(r.root, TASK, r.head, approver="owner", decision="revoke")
        return done

    monkeypatch.setattr(gitops, "remote", revoke_after_push)
    with pytest.raises(AttachError, match="pushed but the pull request step failed: no current approval"):
        go(r, gh)
    assert gh.calls_of("POST") == []
    assert handoff.handoffs(r.root, TASK)[-1][1].status == "PUSHED_NO_PR"


def test_a_missing_token_is_found_before_anything_is_pushed(r, monkeypatch):
    reviewed(r)
    monkeypatch.delenv("ALPACA_ATTACH_TOKEN", raising=False)
    monkeypatch.setattr(gitops, "token", lambda: None)
    with pytest.raises(AttachError, match="no GitHub token"):
        handoff.execute(r.root, TASK, expect=r.head)
    untouched(r)
