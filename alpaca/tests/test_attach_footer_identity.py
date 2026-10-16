"""The attach settings that keep Alpaca's name out of the attached repository (op-062 t-690 D2, t-689).

  * `attach.pr_footer: false` turns the generated section of the pull request body off; the body is then the approved
    body text and a gate summary that names no tool. The setting is part of the dry run's plan and of the approval's
    binding, and the first chat's `pr-says-agent` answer writes it.
  * `attach.commit_name` and `attach.commit_email` are the commit identity of the studio, set at attach and held by the
    record; every commit attach makes, the outgoing author check and the pull request text use it.
A local bare upstream and a fake GitHub API (`attachkit.FakeGitHub`); no network."""
import json
import os
import re
from pathlib import Path

import pytest

from alpaca import cli, project
from alpaca.attach import AttachError, approval, check, config, github, gitops, handoff, proven, repo, runbooks, scan
from alpaca.attach.receipts import AttachApprovalReceipt
from alpaca.gates import verdict as vc
from alpaca.tests.attachkit import (REPOSITORY, FakeGitHub, AttachedStudio, add_worktree, attached, commit_in,  # noqa: F401
                                    git, owner_approve, ready, remote_branches, set_attach, studio, upstream,
                                    with_term_list)
from alpaca.tests.onboardchat import chat  # noqa: F401
from alpaca.tests.test_attach_land_race import claimed, diff_of, wt

TASK = "t-001"
OWNER = ("Vi Nguyen", "vi@example.org")
OWNER_ENV = {"GIT_AUTHOR_NAME": OWNER[0], "GIT_AUTHOR_EMAIL": OWNER[1],
             "GIT_COMMITTER_NAME": OWNER[0], "GIT_COMMITTER_EMAIL": OWNER[1]}
BODY = "Remove the duplicated block.\n\nIt was written twice."


def attach_with(studio, upstream, **kw):
    return AttachedStudio(studio, upstream, repo.attach(str(studio), upstream.url, **kw))


def block_of(root):
    return project.load(str(root))["attach"]


def edit_block(root, **changes):
    block = dict(block_of(root))
    for key, value in changes.items():
        if value is None:
            block.pop(key, None)
        else:
            block[key] = value
    set_attach(root, block)


def no_trace(text):
    low = text.lower()
    for word in ("alpaca", "attach", ".alpaca", "localhost", TASK):
        assert word not in low, word


# ---- the settings in the block -------------------------------------------------------------------------------------------

def test_the_defaults_are_the_footer_on_and_the_alpaca_agent_identity(attached):
    cfg = config.load(str(attached.root))
    assert cfg.pr_footer is True and cfg.identity == ("Alpaca agent", "alpaca-agent@localhost")
    assert "pr_footer" not in block_of(attached.root) and "commit_name" not in block_of(attached.root)


@pytest.mark.parametrize("changes,text", [
    ({"pr_footer": "no"}, "attach.pr_footer must be true or false"),
    ({"commit_name": "Vi"}, "set together or not at all"),
    ({"commit_email": "vi@example.org"}, "set together or not at all"),
    ({"commit_name": "Vi <x>", "commit_email": "vi@example.org"}, "attach.commit_name must be a plain name"),
    ({"commit_name": "Vi\nN", "commit_email": "vi@example.org"}, "attach.commit_name must be a plain name"),
    ({"commit_name": "Vi", "commit_email": "not an email"}, "attach.commit_email must look like"),
    ({"commit_name": "Vi", "commit_email": "-x@example.org"}, "attach.commit_email must look like"),
])
def test_a_bad_footer_or_identity_setting_is_a_config_error(attached, changes, text):
    block = dict(block_of(attached.root), **changes)
    assert any(text in e for e in config.validate(block, str(attached.root)))


def test_a_footer_off_and_an_identity_are_valid_and_read_back(attached):
    edit_block(attached.root, pr_footer=False, commit_name=OWNER[0], commit_email=OWNER[1])
    cfg = config.load(str(attached.root))
    assert cfg.pr_footer is False and cfg.identity == OWNER


# ---- t-689: the identity ------------------------------------------------------------------------------------------------

def test_attach_sets_the_chosen_identity_in_the_clone_and_records_it(studio, upstream):
    att = attach_with(studio, upstream, commit_name=OWNER[0], commit_email=OWNER[1])
    assert block_of(studio)["commit_name"] == OWNER[0] and block_of(studio)["commit_email"] == OWNER[1]
    app = str(att.app)
    assert git(app, "config", "--local", "--get", "user.name") == OWNER[0]
    assert git(app, "config", "--local", "--get", "user.email") == OWNER[1]
    assert proven.init_facts(str(studio), att.cfg)["identity"] == OWNER
    assert proven.identity(str(studio), att.cfg) == OWNER


def test_the_unset_identity_is_the_default_one_in_the_clone_and_the_record(attached):
    app = str(attached.app)
    assert git(app, "config", "--local", "--get", "user.name") == "Alpaca agent"
    assert proven.init_facts(str(attached.root), attached.cfg)["identity"] == gitops.IDENTITY


def test_every_commit_attach_makes_carries_the_chosen_identity(studio, upstream, tmp_path):
    att = attach_with(studio, upstream, commit_name=OWNER[0], commit_email=OWNER[1])
    task, folder = claimed(att)
    head = runbooks.land(str(studio), task, diff_of(tmp_path, {"src/landed.py": "y = 1\n"}), message="land one")
    shown = wt(att, task, folder, "log", "-1", "--format=%an <%ae> | %cn <%ce>", head)
    assert shown == "{0} <{1}> | {0} <{1}>".format(*OWNER)


def test_gitops_commits_as_a_given_pair_and_as_the_default_for_true(attached):
    app = str(attached.app)
    assert gitops.git(app, "var", "GIT_COMMITTER_IDENT", identity=OWNER).startswith("Vi Nguyen <vi@example.org>")
    assert gitops.git(app, "var", "GIT_COMMITTER_IDENT", identity=True).startswith("Alpaca agent <alpaca-agent@localhost>")
    with pytest.raises(AttachError):
        gitops.run(app, "var", "GIT_COMMITTER_IDENT", identity=("only-a-name",))


def test_the_outgoing_scan_takes_the_chosen_identity_and_refuses_the_default_one(studio, upstream):
    att = attach_with(studio, upstream, commit_name=OWNER[0], commit_email=OWNER[1])
    with_term_list(att)
    wtree = add_worktree(att, TASK)
    base = git(att.app, "rev-parse", "origin/main")
    commit_in(wtree, {"src/new.py": "x = 1\n"}, "change", env=OWNER_ENV)
    now = scan.outgoing(str(studio), wtree, git(wtree, "rev-parse", "HEAD"), base, branch="alpaca/t-001", title="t", body="b")
    assert now.verdict == vc.PASS, now.reason
    commit_in(wtree, {"src/more.py": "x = 2\n"}, "default author")           # AGENT_ENV: Alpaca agent
    later = scan.outgoing(str(studio), wtree, git(wtree, "rev-parse", "HEAD"), base, branch="alpaca/t-001", title="t", body="b")
    assert later.refused and scan.R_IDENTITY in later.reasons


def test_an_identity_edited_in_project_yaml_after_attach_is_refused(attached):
    root = str(attached.root)
    edit_block(attached.root, commit_name=OWNER[0], commit_email=OWNER[1])
    with pytest.raises(AttachError, match="differ from the commit identity recorded when the project was attached"):
        proven.identity(root, config.load(root))
    with_term_list(attached)
    wtree = add_worktree(attached, TASK)
    commit_in(wtree, {"src/new.py": "x = 1\n"}, "change")
    result = scan.outgoing(root, wtree, git(wtree, "rev-parse", "HEAD"), git(attached.app, "rev-parse", "origin/main"),
                           branch="alpaca/t-001", title="t", body="b")
    assert result.refused and scan.R_IDENTITY in result.reasons


def test_the_verb_takes_the_identity_and_refuses_one_half_of_it(studio, upstream, capsys):
    assert cli.main(["attach", "init", upstream.url, "--commit-name", "Vi N"]) == cli.FAIL
    assert "set together or not at all" in capsys.readouterr().out and not (studio / "app").exists()
    assert cli.main(["attach", "init", upstream.url, "--commit-name", "Vi N", "--commit-email", "vi@example.org",
                     "--json"]) == cli.PASS
    out = json.loads(capsys.readouterr().out)
    assert out["receipt"]["commit_identity"] == "Vi N <vi@example.org>" and out["receipt"]["pr_footer"] is True


# ---- t-690 D2: the footer -----------------------------------------------------------------------------------------------

@pytest.fixture
def quiet(studio, upstream):
    """A studio attached with the footer off and an identity that names no tool."""
    return attach_with(studio, upstream, pr_footer=False, commit_name=OWNER[0], commit_email=OWNER[1])


def quiet_ready(quiet):
    with_term_list(quiet)
    from alpaca.tests.attachkit import FAST_GATES, set_gates, set_repository
    set_repository(quiet.root)
    set_gates(quiet.root, FAST_GATES)
    wtree = add_worktree(quiet, TASK)
    head = commit_in(wtree, {"src/feature.py": "VALUE = 1\n"}, "add a module", env=OWNER_ENV)
    ck = check.run(str(quiet.root), TASK)
    assert ck.verdict == "PASS", ck.reason
    from alpaca.tests.attachkit import Ready
    return Ready(quiet, TASK, wtree, head, ck)


def test_with_the_footer_off_the_body_is_the_approved_text_and_a_gate_summary_that_names_no_tool(quiet):
    r = quiet_ready(quiet)
    owner_approve(r)
    info = handoff.plan(r.root, TASK, body=BODY)
    assert info["body"].startswith(BODY.strip() + "\n\n")
    assert "- ok: pass / pass" in info["body"]
    no_trace(info["body"])
    assert r.head not in info["body"] and "Alpaca agent" not in info["body"]
    out = info["outgoing"]
    assert out["pr_footer"] is False and out["commit_identity"] == "Vi Nguyen <vi@example.org>"


def test_with_the_footer_off_and_no_body_text_the_body_is_the_gate_summary_alone(quiet):
    r = quiet_ready(quiet)
    owner_approve(r)
    body = handoff.plan(r.root, TASK)["body"]
    no_trace(body)
    assert body.splitlines()[0].startswith("Checks run") and "- ok: pass / pass" in body


def test_the_pull_request_that_is_opened_carries_the_dry_run_body_and_no_tool_name(quiet):
    r = quiet_ready(quiet)
    owner_approve(r)
    gh = FakeGitHub(upstream=quiet.upstream)
    info = handoff.plan(r.root, TASK, title="Remove the block", body=BODY)
    rec = handoff.execute(r.root, TASK, expect=r.head, title="Remove the block", body=BODY, api=gh)
    assert rec.status == "PUSHED"
    pr = gh.prs[1]
    assert pr["body"] == info["body"] and pr["title"] == "Remove the block"
    no_trace(pr["body"])
    shown = git(quiet.upstream.bare, "log", "-1", "--format=%an <%ae>", "refs/heads/alpaca/t-001")
    assert shown == "Vi Nguyen <vi@example.org>"


def test_the_footer_on_keeps_the_body_it_always_had(attached):
    r = ready(attached)
    owner_approve(r)
    info = handoff.plan(r.root, TASK, body="Why: because.")
    assert info["outgoing"]["pr_footer"] is True
    assert "Alpaca attach handoff" in info["body"] and "Alpaca agent <alpaca-agent@localhost>" in info["body"]


def test_a_custom_identity_with_the_footer_on_is_named_as_it_is(studio, upstream):
    att = attach_with(studio, upstream, commit_name=OWNER[0], commit_email=OWNER[1])
    r = quiet_ready(att)
    owner_approve(r)
    body = handoff.plan(r.root, TASK)["body"]
    assert "- commits are authored by Vi Nguyen <vi@example.org>" in body and "Alpaca agent" not in body


def test_turning_the_footer_back_on_between_approval_and_execute_is_refused(quiet):
    r = quiet_ready(quiet)
    owner_approve(r)
    handoff.plan(r.root, TASK, body=BODY)
    edit_block(quiet.root, pr_footer=None)
    gh = FakeGitHub(upstream=quiet.upstream)
    with pytest.raises(AttachError, match="approval of %s was given for another pr footer" % r.head[:12]):
        handoff.execute(r.root, TASK, expect=r.head, body=BODY, api=gh)
    assert remote_branches(quiet.upstream) == ["main"] and gh.calls == []


def test_the_dry_run_plan_itself_holds_the_footer_setting(quiet, monkeypatch):
    """With the approval check out of the way, the recomputed plan still differs from the reviewed dry run."""
    r = quiet_ready(quiet)
    owner_approve(r)
    handoff.plan(r.root, TASK, body=BODY)
    edit_block(quiet.root, pr_footer=None)
    monkeypatch.setattr(approval, "require", lambda *a, **k: type("R", (), {"attempt": "x", "approver": "owner"})())
    monkeypatch.setattr(handoff.approval, "require", approval.require)
    gh = FakeGitHub(upstream=quiet.upstream)
    with pytest.raises(AttachError, match="plan changed since the dry run \\(.*pr_footer"):
        handoff.execute(r.root, TASK, expect=r.head, body=BODY, api=gh)
    assert remote_branches(quiet.upstream) == ["main"]


def test_an_approval_records_the_footer_and_the_identity_it_was_given_for(quiet):
    r = quiet_ready(quiet)
    rec = owner_approve(r)
    assert rec.pr_footer is False and rec.commit_identity == "Vi Nguyen <vi@example.org>"
    current = approval.current(r.root, TASK, r.head)
    assert current.pr_footer is False


def test_an_approval_written_before_the_settings_existed_holds_the_defaults():
    old = {"source_sha": "a" * 40, "approver": "owner", "decision": "approve", "task": TASK, "repository": REPOSITORY,
           "url": "u", "branch": "alpaca/t-001", "base": "main", "gate_set": "g", "status": "APPROVED", "kind": "attach-approval",
           "schema": 1}
    rec = AttachApprovalReceipt(**old)
    assert rec.pr_footer is True and rec.commit_identity == "Alpaca agent <alpaca-agent@localhost>"


def test_the_verb_flag_turns_the_footer_off(studio, upstream, capsys):
    assert cli.main(["attach", "init", upstream.url, "--no-pr-footer", "--json"]) == cli.PASS
    assert json.loads(capsys.readouterr().out)["receipt"]["pr_footer"] is False
    assert block_of(studio)["pr_footer"] is False and "commit_name" not in block_of(studio)


# ---- the first chat card reaches both settings ---------------------------------------------------------------------------

def _card_for(chat, upstream, *answers):
    chat.look(upstream.url)
    chat.answers()
    for args in answers:
        rc, out = chat.run("onboard", "answer", *args, "--source", "agent")
        assert rc == 0, out
    cid, card = chat.propose()
    chat.say("yes")
    assert chat.run("onboard", "confirm", cid, "--quote", "yes")[0] == 0
    return cid, card


def test_the_pr_says_agent_answer_reaches_the_attach_block(chat, upstream):
    cid, card = _card_for(chat, upstream, ["pr-says-agent", "--option", "b"])
    inputs = json.load(open(os.path.join(chat.root, ".alpaca", "onboard", "cards", cid + ".json")))
    assert inputs["settings"]["pr_says_agent"] is False
    rc, out = chat.run("attach", "init", upstream.url, "--card", cid)
    assert rc == 0, out
    block = project.load(chat.root)["attach"]
    assert block["pr_footer"] is False and "commit_name" not in block


def test_without_that_answer_the_card_leaves_the_footer_on(chat, upstream):
    cid, card = _card_for(chat, upstream)
    rc, out = chat.run("attach", "init", upstream.url, "--card", cid)
    assert rc == 0, out
    assert "pr_footer" not in project.load(chat.root)["attach"]


def test_an_identity_the_owner_chose_on_the_card_reaches_the_block_and_the_clone(chat, upstream):
    cid, card = _card_for(chat, upstream, ["commit-identity", "--value", "Vi Nguyen <vi@example.org>"])
    assert "- Commit identity: Vi Nguyen <vi@example.org> (you chose it (my reading of your reply))" in " ".join(card.split("\n"))
    rc, out = chat.run("attach", "init", upstream.url, "--card", cid)
    assert rc == 0, out
    block = project.load(chat.root)["attach"]
    assert (block["commit_name"], block["commit_email"]) == OWNER
    assert git(os.path.join(chat.root, "app"), "config", "--local", "--get", "user.email") == OWNER[1]


def test_a_commit_identity_answer_that_is_not_name_and_email_is_refused(chat, upstream):
    chat.look(upstream.url)
    chat.answers()
    rc, out = chat.run("onboard", "answer", "commit-identity", "--value", "just a name", "--source", "agent")
    assert rc != 0 and "Name <name@host>" in out


def test_the_flags_that_name_the_settings_are_refused_with_a_card(chat, upstream):
    cid, card = _card_for(chat, upstream)
    for flags in (["--no-pr-footer"], ["--commit-name", "X"], ["--commit-email", "x@example.com"]):
        rc, out = chat.run("attach", "init", upstream.url, "--card", cid, *flags)
        assert rc == 1 and "ATTACH-CARD-FLAGS" in out, flags


# ---- the trace set the card shows is the trace set apply writes (op-062 t-691) ---------------------------------------------

def _shown(card):
    """The three trace lines of part 6 of a card, as (identity, footer on, prefix)."""
    lines = {m.group(1): m.group(2) for m in re.finditer(r"^- (Commit identity|Pull request footer|Branch prefix): (.*)$",
                                                          card, re.M)}
    ident = re.match(r"(.*) <(.*)> \(", lines["Commit identity"])
    return (ident.group(1), ident.group(2)), lines["Pull request footer"].startswith("on,"), lines["Branch prefix"].split(" ")[0]


def test_a_card_with_no_footprint_answer_shows_the_defaults_and_writes_them(chat, upstream):
    cid, card = _card_for(chat, upstream)
    assert _shown(card) == (("Alpaca agent", "alpaca-agent@localhost"), True, "alpaca/")
    rc, out = chat.run("attach", "init", upstream.url, "--card", cid)
    assert rc == 0, out
    block = project.load(chat.root)["attach"]
    assert block["branch_prefix"] == "alpaca/" and "pr_footer" not in block and "commit_name" not in block
    cfg = config.load(chat.root)
    assert (cfg.identity, cfg.pr_footer, cfg.branch_prefix) == (("Alpaca agent", "alpaca-agent@localhost"), True, "alpaca/")


def test_an_owner_who_wants_no_trace_gets_the_neutral_set_on_the_card_and_in_the_block_and_the_clone(chat, upstream):
    cid, card = _card_for(chat, upstream, ["files-in-repo", "--option", "a"])
    identity, footer, prefix = _shown(card)
    assert (identity, footer, prefix) == (("Maintenance bot", "maintenance@localhost"), False, "fix/")
    rc, out = chat.run("onboard", "apply", cid)
    assert rc == 0, out
    block = project.load(chat.root)["attach"]
    assert (block["commit_name"], block["commit_email"]) == identity
    assert block["pr_footer"] is False and block["branch_prefix"] == prefix
    app = os.path.join(chat.root, "app")
    assert git(app, "config", "--local", "--get", "user.email") == identity[1]
    assert git(app, "config", "--local", "--get", "user.name") == identity[0]
    cfg = config.load(chat.root)
    assert (cfg.identity, cfg.pr_footer, cfg.branch_prefix) == (identity, footer, prefix)


def test_what_the_owner_chose_wins_over_the_proposal_and_apply_writes_what_the_card_shows(chat, upstream):
    cid, card = _card_for(chat, upstream, ["files-in-repo", "--option", "a"], ["commit-identity", "--value", "Vi Nguyen <vi@example.org>"],
                          ["pr-says-agent", "--option", "a"], ["branch-prefix", "--value", "work/<ticket>"])
    identity, footer, prefix = _shown(card)
    assert (identity, footer, prefix) == (OWNER, True, "work/")
    rc, out = chat.run("onboard", "apply", cid)
    assert rc == 0, out
    cfg = config.load(chat.root)
    assert (cfg.identity, cfg.pr_footer, cfg.branch_prefix) == (identity, footer, prefix)


def test_a_footprint_yes_on_a_solo_take_over_attaches_with_the_defaults_and_says_the_in_place_mode_is_not_built(chat, upstream):
    cid, card = _card_for(chat, upstream, ["files-in-repo", "--option", "b"])
    flat = " ".join(card.split())
    assert "3. WHERE ALPACA SITS: attach mode" in flat and "is not built in this version, so I propose attach" in flat
    rc, out = chat.run("onboard", "apply", cid)
    assert rc == 0, out
    assert config.mode(chat.root) is not None and not os.path.exists(os.path.join(chat.root, "app", ".alpaca"))
    assert _shown(card) == (("Alpaca agent", "alpaca-agent@localhost"), True, "alpaca/")


def test_the_footprint_answer_is_checked_against_the_question_the_owner_was_asked(chat, upstream):
    """An owner answer to the footprint question needs its token in the reply, like every option answer."""
    out = chat.look(upstream.url)
    assert "[files-in-repo] May Alpaca files live in your repo?" in out
    num = re.search(r"(\d)\. \[files-in-repo\]", out).group(1)
    reply = chat.say("%sa. and keep it clean" % num)
    assert chat.run("onboard", "answer", "files-in-repo", "--option", "a", "--quote", reply)[0] == 0
    wrong = chat.say("no thanks")
    rc, text = chat.run("onboard", "answer", "files-in-repo", "--option", "b", "--quote", wrong)
    assert rc != 0 and "ONBOARD-OPTION-NOT-IN-REPLY" in text
