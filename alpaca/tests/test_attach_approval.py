"""`alpaca attach approve`: the approval of one exact head commit (op-062 t-686, R8; build plan P15, P16)."""
import hashlib
import json
from pathlib import Path

import pytest

from alpaca import cli, db
from alpaca.attach import AttachError, approval, check, config, proven
from alpaca.attach.receipts import AttachApprovalReceipt
from alpaca.release.receipts import ApprovalReceipt, load_receipt, record_receipt
from alpaca.tests.attachkit import (REPOSITORY, FakeGitHub, agent_approve, attached, commit_in, git, owner_approve,  # noqa: F401
                                    project_yaml, ready, record_decision, set_attach, set_gates, studio, upstream,
                                    write_delegation)

TASK = "t-001"


def events(root, kind):
    conn = db.connect(str(root))
    try:
        return [e for e in db.events(conn, limit=10 ** 9) if e["kind"] == kind]
    finally:
        conn.close()


@pytest.fixture
def r(attached):
    return ready(attached)


# ---- the receipt -------------------------------------------------------------------------------------------------------

def test_an_owner_approval_is_an_attach_receipt_bound_to_the_project(r):
    path, rec = approval.approve(r.root, TASK, r.head, approver="owner")
    assert isinstance(rec, AttachApprovalReceipt) and rec.kind == "attach-approval" and rec.status == "APPROVED"
    assert (rec.source_sha, rec.task, rec.approver, rec.decision) == (r.head, TASK, "owner", "approve")
    assert rec.repository == REPOSITORY and rec.url == r.att.upstream.url and rec.branch == "alpaca/t-001"
    assert rec.base == "main" and rec.gate_set == config.load(r.root).gate_set_digest()
    assert rec.actions == ["push", "pr"] and rec.delegation == "" and rec.dec_id == ""
    assert Path(path).name.endswith("-attach-approval.json")
    assert [e["kind"] for e in events(r.root, "release-attach-approval")] != []
    assert events(r.root, "release-approval") == []                       # release's own kind is not written
    assert approval.current(r.root, TASK, r.head).attempt == rec.attempt


def test_the_receipt_file_loads_only_through_its_own_class(r):
    path, rec = approval.approve(r.root, TASK, r.head, approver="owner")
    rel = Path(path).relative_to(Path(r.root).resolve()).as_posix()
    assert load_receipt(r.root, rel, AttachApprovalReceipt).source_sha == r.head
    with pytest.raises(ValueError):
        load_receipt(r.root, rel, ApprovalReceipt)


def test_only_the_exact_head_can_be_approved(r):
    with pytest.raises(AttachError, match="not the head"):
        approval.approve(r.root, TASK, "a" * 40, approver="owner")
    with pytest.raises(AttachError, match="40-character"):
        approval.approve(r.root, TASK, r.head[:12], approver="owner")
    with pytest.raises(AttachError, match="40-character"):
        approval.approve(r.root, TASK, r.head.upper(), approver="owner")
    with pytest.raises(AttachError, match="not a task id"):
        approval.approve(r.root, "../t-001", r.head, approver="owner")
    with pytest.raises(AttachError, match="no work branch"):
        approval.approve(r.root, "t-009", r.head, approver="owner")
    assert events(r.root, "release-attach-approval") == []


def test_a_head_that_moved_after_the_approval_is_not_approved(r):
    owner_approve(r)
    newer = commit_in(r.wt, {"src/more.py": "x = 2\n"}, "one more change")
    assert approval.current(r.root, TASK, newer) is None
    with pytest.raises(AttachError, match="no current approval"):
        approval.require(r.root, TASK, newer, action="push")
    assert approval.require(r.root, TASK, r.head, action="push").source_sha == r.head       # the old sha still has its own


def test_an_approval_of_another_task_does_not_carry_over(attached):
    a = ready(attached, "t-001")
    owner_approve(a)
    assert approval.current(a.root, "t-002", a.head) is None


# ---- agent approval and the delegation (P16) -------------------------------------------------------------------------

def test_an_agent_approval_needs_a_delegation(r):
    with pytest.raises(AttachError, match="PATH#Section"):
        approval.approve(r.root, TASK, r.head, approver="agent")
    with pytest.raises(AttachError, match="PATH#Section"):
        approval.approve(r.root, TASK, r.head, approver="agent", delegation="docs/delegation.md")
    record_decision(r.root)
    ref = write_delegation(r.root)
    rec = approval.approve(r.root, TASK, r.head, approver="agent", delegation=ref)[1]
    assert rec.approver == "agent" and rec.dec_id == "dec-049934c5931f" and rec.actions == ["push", "pr", "merge"]
    path = Path(r.root) / "docs" / "delegation.md"
    assert rec.delegation == ref and rec.delegation_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("line, why", [
    (None, None),
    ("nothing here", "holds no 'attach-delegation"),
    ("attach-delegation: dec-049934c5931f other/repo push,pr,merge", "names other/repo"),
    ("attach-delegation: dec-049934c5931f acme/demo", "does not read"),
    ("attach-delegation: dec-049934c5931f acme/demo push,deploy", "does not read"),
    ("attach-delegation: dec-1 acme/demo push", "does not read"),
    ("attach-delegation: dec-aaaaaaaaaaaa acme/demo push", "holds no decision dec-aaaaaaaaaaaa"),
    ("attach-delegation: dec-049934c5931f acme/demo push\nattach-delegation: dec-049934c5931f acme/demo merge", "more than one"),
])
def test_the_delegation_line_is_checked(r, line, why):
    record_decision(r.root)
    ref = write_delegation(r.root, line)
    if why is None:
        assert approval.approve(r.root, TASK, r.head, approver="agent", delegation=ref)[1].approver == "agent"
        return
    with pytest.raises(AttachError, match=why):
        approval.approve(r.root, TASK, r.head, approver="agent", delegation=ref)
    assert events(r.root, "release-attach-approval") == []


def test_a_line_outside_the_named_section_does_not_count(r):
    record_decision(r.root)
    path = Path(r.root) / "docs" / "d.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("# A\n\n## Mine\n\nnothing\n\n## Other\n\nattach-delegation: dec-049934c5931f acme/demo push,pr\n")
    with pytest.raises(AttachError, match="holds no 'attach-delegation"):
        approval.approve(r.root, TASK, r.head, approver="agent", delegation="docs/d.md#Mine")
    with pytest.raises(AttachError, match="no section"):
        approval.approve(r.root, TASK, r.head, approver="agent", delegation="docs/d.md#Absent")
    assert approval.approve(r.root, TASK, r.head, approver="agent", delegation="docs/d.md#other")[1].actions == ["push", "pr"]


def test_a_delegation_file_must_be_a_project_file_without_links(r, tmp_path):
    record_decision(r.root)
    outside = tmp_path / "outside.md"
    outside.write_text("## S\n\nattach-delegation: dec-049934c5931f acme/demo push\n")
    (Path(r.root) / "link.md").symlink_to(outside)
    for ref in ("link.md#S", "../outside.md#S", "%s#S" % outside, "docs/missing.md#S"):
        with pytest.raises(AttachError):
            approval.approve(r.root, TASK, r.head, approver="agent", delegation=ref)


def test_an_owner_approval_takes_no_delegation(r):
    record_decision(r.root)
    with pytest.raises(AttachError, match="typed, not delegated"):
        approval.approve(r.root, TASK, r.head, approver="owner", delegation=write_delegation(r.root))


def test_the_actions_of_an_agent_approval_are_at_most_the_delegations(r):
    record_decision(r.root)
    ref = write_delegation(r.root, "attach-delegation: dec-049934c5931f acme/demo push,pr")
    with pytest.raises(AttachError, match="does not allow merge"):
        approval.approve(r.root, TASK, r.head, approver="agent", delegation=ref, actions="push,merge")
    rec = approval.approve(r.root, TASK, r.head, approver="agent", delegation=ref, actions="push")[1]
    assert rec.actions == ["push"]
    with pytest.raises(AttachError, match="does not cover pr"):
        approval.require(r.root, TASK, r.head, action=("push", "pr"))


def test_the_delegation_is_read_again_at_every_use(r):
    rec = agent_approve(r)
    assert approval.require(r.root, TASK, r.head, action="merge").attempt == rec.attempt
    path = Path(r.root) / "docs" / "delegation.md"
    text = path.read_text()
    path.write_text(text.replace("push,pr,merge", "push,pr"))                 # the owner withdraws merge
    with pytest.raises(AttachError, match="no longer covers merge"):
        approval.require(r.root, TASK, r.head, action="merge")
    assert approval.require(r.root, TASK, r.head, action="push")
    path.write_text(text.replace("acme/demo", "other/repo"))
    with pytest.raises(AttachError, match="no longer covers"):
        approval.require(r.root, TASK, r.head, action="push")
    path.write_text("# gone\n")
    with pytest.raises(AttachError, match="no longer holds"):
        approval.require(r.root, TASK, r.head, action="push")


def test_a_merge_is_covered_by_a_delegation_that_names_it_or_by_an_owner_who_types_it(r):
    owner_approve(r)
    with pytest.raises(AttachError, match="does not cover merge"):
        approval.require(r.root, TASK, r.head, action="merge")
    owner_approve(r, actions="push,pr,merge")
    assert approval.require(r.root, TASK, r.head, action="merge").approver == "owner"


# ---- red at base (P16) ------------------------------------------------------------------------------------------------

def test_a_head_red_at_base_can_be_approved_by_the_owner_only(attached):
    attached.upstream.commit({"broken": "red\n"}, "upstream is red")
    gate = {"name": "test", "command": ["python3", "-B", "-c", "import os, sys; sys.exit(1 if os.path.exists('broken') else 0)"],
            "timeout": 60}
    r = ready(attached, gates=[gate])
    assert r.ck.needs_owner_review and r.ck.red_at_base == ["test"]
    record_decision(r.root)
    ref = write_delegation(r.root)
    with pytest.raises(AttachError, match="only an owner approval"):
        approval.approve(r.root, TASK, r.head, approver="agent", delegation=ref)
    assert approval.approve(r.root, TASK, r.head, approver="owner")[1].approver == "owner"


def test_require_refuses_an_agent_approval_when_the_owner_is_needed(r):
    agent_approve(r)
    assert approval.require(r.root, TASK, r.head, action="push").approver == "agent"
    with pytest.raises(AttachError, match="must be the owner's"):
        approval.require(r.root, TASK, r.head, action="push", needs_owner=True)
    owner_approve(r)
    assert approval.require(r.root, TASK, r.head, action="push", needs_owner=True).approver == "owner"


# ---- revocation and the record ------------------------------------------------------------------------------------------

def test_a_revocation_wins_and_a_later_approval_wins_over_it(r):
    owner_approve(r)
    approval.approve(r.root, TASK, r.head, approver="owner", decision="revoke")
    assert approval.current(r.root, TASK, r.head) is None
    with pytest.raises(AttachError, match="no current approval"):
        approval.require(r.root, TASK, r.head, action="push")
    again = owner_approve(r)
    assert approval.current(r.root, TASK, r.head).attempt == again.attempt


def test_an_agent_may_revoke_without_a_delegation_and_a_stale_sha_can_be_revoked(r):
    owner_approve(r)
    old = r.head
    commit_in(r.wt, {"src/more.py": "x = 2\n"}, "one more change")
    rec = approval.approve(r.root, TASK, old, approver="agent", decision="revoke")[1]
    assert rec.status == "REVOKED" and rec.actions == []
    assert approval.current(r.root, TASK, old) is None
    with pytest.raises(AttachError, match="not a commit"):
        approval.approve(r.root, TASK, "b" * 40, approver="agent", decision="revoke")


def test_a_release_approval_of_the_same_sha_authorizes_nothing_here(r):
    record_receipt(r.root, "cli", ApprovalReceipt(r.head, "owner", "approve", "0" * 40, "", "", "APPROVED"))
    assert events(r.root, "release-approval") != []
    assert approval.current(r.root, TASK, r.head) is None
    with pytest.raises(AttachError, match="no current approval"):
        approval.require(r.root, TASK, r.head, action="push")


def test_an_attach_approval_is_not_a_release_approval(r):
    from alpaca.release import approval as release_approval
    owner_approve(r)
    assert release_approval.current(r.root, r.head) is None


def test_an_edited_receipt_file_refuses_every_approval(r):
    path = owner_approve(r) and sorted((Path(r.root) / ".alpaca" / "releases" / "receipts").glob("*-attach-approval.json"))[0]
    data = json.loads(path.read_text())
    data["actions"] = ["push", "pr", "merge"]
    path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n")
    with pytest.raises(AttachError, match="missing or damaged"):
        approval.current(r.root, TASK, r.head)
    with pytest.raises(AttachError, match="missing or damaged"):
        approval.require(r.root, TASK, r.head, action="push")


def test_a_deleted_receipt_file_refuses_and_does_not_bring_an_older_approval_back(r):
    owner_approve(r)
    approval.approve(r.root, TASK, r.head, approver="owner", decision="revoke")
    revoke = sorted((Path(r.root) / ".alpaca" / "releases" / "receipts").glob("*-attach-approval.json"),
                    key=lambda p: json.loads(p.read_text())["created_at"])[-1]
    revoke.unlink()
    with pytest.raises(AttachError, match="missing or damaged"):
        approval.current(r.root, TASK, r.head)


# ---- what an approval is bound to ---------------------------------------------------------------------------------------

def test_an_approval_is_void_when_the_gate_set_changes(r):
    owner_approve(r)
    set_gates(r.root, [{"name": "ok", "command": ["python3", "-B", "-c", "import sys; sys.exit(0)"], "timeout": 61}])
    with pytest.raises(AttachError, match="another gate set"):
        approval.require(r.root, TASK, r.head, action="push")


def test_an_approval_is_void_when_the_base_branch_or_the_repository_changes(r):
    owner_approve(r)
    block = dict(project_yaml(r.root)["attach"])
    set_attach(r.root, dict(block, repository="other/demo"))
    with pytest.raises(AttachError, match="another repository"):
        approval.require(r.root, TASK, r.head, action="push")
    set_attach(r.root, dict(block, base="develop"))
    with pytest.raises(AttachError, match="another base"):
        approval.require(r.root, TASK, r.head, action="push")
    set_attach(r.root, block)
    assert approval.require(r.root, TASK, r.head, action="push")


def test_an_approval_is_void_when_the_upstream_url_changes(r, monkeypatch):
    owner_approve(r)
    monkeypatch.setattr(proven, "fetch_url", lambda root, cfg: "https://github.com/someone/else")
    with pytest.raises(AttachError, match="another url"):
        approval.require(r.root, TASK, r.head, action="push")


def test_a_changed_url_in_project_yaml_refuses_before_the_receipt_is_compared(r, tmp_path):
    owner_approve(r)
    block = dict(project_yaml(r.root)["attach"])
    set_attach(r.root, dict(block, url=str(tmp_path / "elsewhere.git")))
    with pytest.raises(AttachError, match="differs from the URL recorded"):
        approval.require(r.root, TASK, r.head, action="push")


# ---- the verb ---------------------------------------------------------------------------------------------------------

def run(*argv):
    return cli.main(["attach", "approve", *argv])


def test_the_owner_verb_refuses_outside_a_terminal(r, capsys):
    code = run(TASK, r.head, "--by", "owner")
    out = capsys.readouterr().out
    assert code == cli.BLOCKED and "GATE alpaca-attach-approve: BLOCKED" in out and "interactive terminal" in out
    assert events(r.root, "release-attach-approval") == []


def test_the_owner_verb_records_once_the_owner_typed_the_sha(r, capsys, monkeypatch):
    seen = []
    monkeypatch.setattr("alpaca.release.cli.owner_confirmation", lambda sha: seen.append(sha))
    assert run(TASK, r.head, "--by", "owner", "--json") == cli.PASS
    out = json.loads(capsys.readouterr().out)
    assert seen == [r.head] and out["verdict"] == "PASS" and out["task"] == TASK
    assert out["receipt"]["status"] == "APPROVED" and out["receipt"]["actions"] == ["push", "pr"]


def test_the_agent_verb_needs_its_delegation_and_records_under_it(r, capsys):
    assert run(TASK, r.head, "--by", "agent") == cli.BLOCKED
    assert "PATH#Section" in capsys.readouterr().out
    record_decision(r.root)
    ref = write_delegation(r.root)
    assert run(TASK, r.head, "--by", "agent", "--delegation", ref) == cli.PASS
    assert "covers: push, pr, merge" in capsys.readouterr().out
    assert run(TASK, r.head, "--by", "agent", "--revoke") == cli.PASS
    assert approval.current(r.root, TASK, r.head) is None


def test_the_verb_is_registered_and_the_default_attach_group_lists_it(capsys):
    from alpaca.attach import cli as attach_cli
    assert "approval" in attach_cli.MODULES
    assert "approve" in approval.SUBVERBS
