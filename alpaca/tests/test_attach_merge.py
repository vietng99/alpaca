"""`alpaca attach merge`: the pull request of an approved handoff, merged only under a delegation that names merge,
with the head sha pinned in the call (op-062 t-686; build plan 6.8, P16, P17)."""
import pytest

from alpaca import cli, db
from alpaca.attach import AttachError, approval, github, gitops, handoff
from alpaca.tests.attachkit import (FakeGitHub, agent_approve, attached, git, owner_approve, ready, record_decision,  # noqa: F401
                                    remote_branches, studio, upstream, write_delegation)

TASK = "t-001"


@pytest.fixture
def r(attached):
    return ready(attached)


@pytest.fixture
def gh(r):
    return FakeGitHub(upstream=r.att.upstream)


def pushed(r, gh, *, agent=True, actions=None):
    """A handoff that went through: approved (by an agent under a delegation that names merge, unless told),
    reviewed, pushed, with its pull request open."""
    if agent:
        agent_approve(r, actions=actions)
    else:
        owner_approve(r, actions=actions)
    handoff.plan(r.root, TASK)
    return handoff.execute(r.root, TASK, expect=r.head, api=gh)


def merge(r, gh, **kw):
    kw.setdefault("expect", r.head)
    kw.setdefault("execute", True)
    return handoff.merge(r.root, TASK, api=gh, **kw)


def test_a_merge_pins_the_head_sha_and_the_method(r, gh):
    pushed(r, gh)
    rec = merge(r, gh, method="squash")
    put = gh.calls_of("PUT")
    assert len(put) == 1 and put[0][1] == "/repos/acme/demo/pulls/1/merge"
    assert put[0][2] == {"sha": r.head, "merge_method": "squash"}
    assert (rec.status, rec.method, rec.pr_number, rec.head, rec.merge_sha) == ("MERGED", "squash", 1, r.head, "f" * 40)
    assert handoff.merges(r.root, TASK)[0].attempt == rec.attempt


def test_the_default_method_is_merge(r, gh):
    pushed(r, gh)
    merge(r, gh)
    assert gh.calls_of("PUT")[0][2]["merge_method"] == "merge"


def test_a_dry_run_merges_nothing(r, gh):
    pushed(r, gh)
    info = handoff.merge(r.root, TASK, api=gh)
    assert info["pr_number"] == 1 and info["head"] == r.head and info["method"] == "merge"
    assert gh.calls_of("PUT") == [] and handoff.merges(r.root, TASK) == []


def test_a_merge_needs_expect_and_the_right_one(r, gh):
    pushed(r, gh)
    for expect in (None, "", r.head[:12], "a" * 40):
        with pytest.raises(AttachError):
            merge(r, gh, expect=expect)
    assert gh.calls_of("PUT") == []


def test_an_owner_approval_does_not_cover_merge_unless_the_owner_typed_it(r, gh):
    pushed(r, gh, agent=False)
    with pytest.raises(AttachError, match="does not cover merge"):
        merge(r, gh)
    owner_approve(r, actions="push,pr,merge")
    assert merge(r, gh).status == "MERGED"


def test_a_delegation_without_merge_cannot_merge(r, gh):
    record_decision(r.root)
    write_delegation(r.root, "attach-delegation: dec-049934c5931f acme/demo push,pr")
    agent_approve(r, delegation="docs/delegation.md#Attach delegation")
    handoff.plan(r.root, TASK)
    handoff.execute(r.root, TASK, expect=r.head, api=gh)
    with pytest.raises(AttachError, match="does not cover merge"):
        merge(r, gh)
    assert gh.calls_of("PUT") == []


def test_a_merge_after_the_approval_was_revoked_refuses(r, gh):
    pushed(r, gh)
    approval.approve(r.root, TASK, r.head, approver="agent", decision="revoke")
    with pytest.raises(AttachError, match="no current approval"):
        merge(r, gh)
    assert gh.calls_of("PUT") == []


def test_a_task_that_was_never_handed_off_has_nothing_to_merge(r, gh):
    agent_approve(r)
    with pytest.raises(AttachError, match="no pull request"):
        merge(r, gh)


def test_a_pull_request_at_another_head_is_not_merged(r, gh):
    pushed(r, gh)
    gh.prs[1]["head"]["sha"] = "a" * 40
    with pytest.raises(AttachError, match="not at the approved head"):
        merge(r, gh)
    assert gh.calls_of("PUT") == []


def test_a_pull_request_into_another_base_is_not_merged(r, gh):
    pushed(r, gh)
    gh.prs[1]["base"]["ref"] = "release"
    with pytest.raises(AttachError, match="is not alpaca/t-001 into main"):
        merge(r, gh)


def test_the_head_is_pinned_so_github_refuses_a_branch_that_moved_meanwhile(r, gh, monkeypatch):
    """The read said the head was right; before the call it moved. The pinned sha is what GitHub compares (409)."""
    pushed(r, gh)
    real = github.GitHub.get_pr

    def stale_read(self, number):
        view = real(self, number)
        gh.prs[1]["head"]["sha"] = "b" * 40                       # somebody pushed to the branch right after the read
        return view

    monkeypatch.setattr(github.GitHub, "get_pr", stale_read)
    with pytest.raises(AttachError, match="409"):
        merge(r, gh)
    assert handoff.merges(r.root, TASK) == []
    assert gh.prs[1]["state"] == "open"


def test_a_base_that_moved_since_the_check_is_not_merged_into(r, gh):
    """P17: merge re-checks what the handoff checked."""
    pushed(r, gh)
    r.att.upstream.commit({"other.txt": "x\n"}, "somebody else's work")
    with pytest.raises(AttachError, match="moved since the check"):
        merge(r, gh)
    assert gh.calls_of("PUT") == []


def test_a_closed_or_merged_pull_request_is_not_merged_again(r, gh):
    pushed(r, gh)
    gh.close(1)
    with pytest.raises(AttachError, match="is closed, not open"):
        merge(r, gh)
    gh.prs[1].update(state="open")
    assert merge(r, gh).status == "MERGED"
    with pytest.raises(AttachError, match="already merged"):
        merge(r, gh)
    assert len(gh.calls_of("PUT")) == 1


def test_an_unknown_method_is_refused(r, gh):
    pushed(r, gh)
    with pytest.raises(AttachError, match="merge method"):
        merge(r, gh, method="fast-forward")


def test_the_merge_verb_prints_the_plan_and_merges_only_with_execute(r, gh, capsys, monkeypatch):
    monkeypatch.setattr(github, "default_api", gh)
    pushed(r, gh)
    assert cli.main(["attach", "merge", TASK]) == cli.PASS
    out = capsys.readouterr().out
    assert "pull request #1" in out and "--execute --expect %s" % r.head in out and "GATE alpaca-attach-merge: PASS" in out
    assert gh.calls_of("PUT") == []
    assert cli.main(["attach", "merge", TASK, "--execute"]) == cli.BLOCKED
    assert "--expect <sha> is required" in capsys.readouterr().out
    assert cli.main(["attach", "merge", TASK, "--execute", "--expect", r.head, "--method", "rebase", "--json"]) == cli.PASS
    assert gh.calls_of("PUT")[0][2]["merge_method"] == "rebase"
