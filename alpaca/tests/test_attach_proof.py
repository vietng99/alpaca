"""Proof reports see the attached repository (op-062 t-650, R6, AC-07; pre-mortem t-650.1, .4, .5, .6, T8).

The seal and the header of a proof read the task's linked worktree of the attached clone, not the studio.
A project without `attach:` keeps its header and its seal payload as they were.
"""
import hashlib
import json
import os
import shutil

import pytest

from alpaca import db, paths, proof, util
from alpaca.attach import config, proofhook
from alpaca.tests import proofkit
from alpaca.tests.attachkit import attached, git, studio, upstream  # noqa: F401  (fixtures)
from alpaca.tests.attachproofkit import commit_in, cut, latest_seal, prepare

HEADER_KEYS = {"path", "sha256", "bytes", "sections", "evidence"}


def seal(root, task="t-001", *, verify=False, evidence=None):
    conn = db.connect(root)
    try:
        path = proofkit.write_report(root, task, conn=conn, evidence=evidence)
        if verify:                  # a verification-scope seal needs a command the verifiers can re-run
            text = util.read_text(path)
            assert "## How to reproduce\n" in text
            util.write_text(path, text.replace("## How to reproduce\n", "## How to reproduce\n\n```\npython3 make_log.py > logs/run.txt 2>&1\n```\n", 1))
        return proof.seal(conn, root, task, session="builder", verify=verify)
    finally:
        conn.close()


def scaffold_text(root, task="t-001"):
    conn = db.connect(root)
    try:
        res = proof.scaffold(conn, root, task, force=True)
        return util.read_text(res["path"])
    finally:
        conn.close()


def refusal(root, task="t-001", **kw):
    with pytest.raises(proof.Refusal) as exc:
        seal(root, task, **kw)
    return " | ".join(exc.value.problems)


@pytest.fixture
def task(attached):
    root = prepare(attached)
    wt = cut(attached)
    return attached, root, wt


# ------------------------------------------------------------------------------------ the header
def test_header_carries_head_branch_dirty_count_base_and_commits_ahead(task):
    att, root, wt = task
    base = git(wt, "rev-parse", "HEAD")
    text = scaffold_text(root)
    assert "- app git: %s on alpaca/t-001, 0 dirty file(s), base %s, 0 commit(s) ahead" % (base[:12], base[:12]) in text
    head = commit_in(wt, {"src/main.py": "print('changed')\n"})
    (wt / "scratch.txt").write_text("x\n")
    text = scaffold_text(root)               # no cache: the new head and the dirty file show at once
    assert "- app git: %s on alpaca/t-001, 1 dirty file(s), base %s, 1 commit(s) ahead" % (head[:12], base[:12]) in text
    lines = text.splitlines()
    assert lines[lines.index([x for x in lines if x.startswith("- git: ")][0]) + 1].startswith("- app git: ")


def test_header_says_worktree_gone_when_the_folder_is_gone(task):
    att, root, wt = task
    shutil.move(str(wt), str(wt) + ".away")
    assert "- app git: worktree gone" in scaffold_text(root)


def test_a_studio_only_task_in_an_attach_project_prints_only_the_studio_line(attached):
    root = prepare(attached)
    text = scaffold_text(root)
    assert "- git: " in text and "app git" not in text


def test_a_project_without_attach_has_the_same_header_and_seal_payload(project):
    from alpaca import cli
    cli.main(["init"])
    cli.main(["op", "new", "add the shout verb", "--done-when", "the verb prints upper case"])
    cli.main(["task", "add", "--title", "task", "op-001", "write the shout verb", "--phase", "build"])
    text = scaffold_text(project)
    assert "app git" not in text
    payload = seal(project)
    assert set(payload) == HEADER_KEYS and "attach" not in payload
    assert proofhook.header_line(project, "t-001") is None and proofhook.seal_block(project, "t-001") is None
    assert proofhook.take_app_patch(project, "t-001") is None
    assert not proofhook.is_attach_path(project, os.path.join(project, "app", "x"))


# ------------------------------------------------------------------------------------ the seal
def test_seal_payload_carries_the_attach_block(task):
    att, root, wt = task
    base = git(wt, "rev-parse", "HEAD")
    head = commit_in(wt, {"src/main.py": "print('changed')\n"})
    (wt / "extra.txt").write_text("x\n")
    payload = seal(root)
    assert payload["attach"] == {"repo": "app", "branch": "alpaca/t-001", "head": head, "base": base, "dirty": 1}
    assert "tree" not in payload and "verify_scope" not in payload
    assert latest_seal(root)["data"]["attach"] == payload["attach"]
    assert json.load(open(payload["path"] + ".seal.json" if os.path.isabs(payload["path"])
                          else os.path.join(root, payload["path"] + ".seal.json")))["attach"] == payload["attach"]


def test_a_seal_with_the_worktree_gone_says_so(task):
    att, root, wt = task
    shutil.move(str(wt), str(wt) + ".away")
    payload = seal(root)
    assert payload["attach"]["head"] is None and payload["attach"]["worktree"] == "gone"
    # op-062 integration of t-650: `config.task_branch` was the raw reader of branches/<task>.json at t-650's
    # base; under t-648 it answers from the record and the work folder, so with the folder gone it is None.
    # `config.read_branch_file` is the raw reader this line meant, and it returns the same dict.
    assert payload["attach"]["base"] == config.read_branch_file(root, "t-001")["base_sha"]


def test_committed_branch_work_is_in_app_patch(task):
    att, root, wt = task
    base = git(wt, "rev-parse", "HEAD")
    head = commit_in(wt, {"src/main.py": "print('committed')\n"})
    (wt / "src" / "staged.py").write_text("staged = 1\n")
    git(wt, "add", "src/staged.py")
    (wt / "src" / "untracked.py").write_text("untracked = 1\n")
    payload = seal(root, verify=True)
    app = payload["tree"]["app"]
    assert app["head"] == head and app["base"] == base
    kept = os.path.join(root, app["patch"])
    # the product's diff is kept in the closed folder, not beside the served tree.patch (fix round 4, review K1;
    # a deviation from build-plan.md:620-626, which said "next to tree.patch")
    assert kept.endswith("t-001.evidence/.closed/app.patch"), kept
    patch = open(kept, encoding="utf-8").read()
    for name in ("src/main.py", "src/staged.py", "src/untracked.py"):
        assert "+++ b/%s" % name in patch, name
    assert "+print('committed')" in patch
    assert app["bytes"] == len(patch.encode()) and app["patch_sha256"] == hashlib.sha256(patch.encode()).hexdigest()
    assert payload["attach"]["head"] == head
    # the studio's own patch does not hold the app work (it is ignored there)
    assert "src/main.py" not in open(os.path.join(root, payload["tree"]["patch"]), encoding="utf-8").read()
    # a re-seal builds the kept folder again and the patch is still where the first seal recorded it
    again = seal(root, verify=True)
    assert os.path.isfile(kept) and again["tree"]["app"]["patch"] == app["patch"]
    assert open(kept, encoding="utf-8").read() == patch


def test_an_empty_app_diff_is_still_recorded_in_verification_scope(task):
    att, root, wt = task
    app = seal(root, verify=True)["tree"]["app"]
    assert app["bytes"] == 0 and os.path.getsize(os.path.join(root, app["patch"])) == 0


def test_verification_seal_is_refused_when_the_worktree_is_gone(task):
    """Contract fail case: a proof in attach mode must not seal while its app diff is missing."""
    att, root, wt = task
    commit_in(wt, {"src/main.py": "print('committed')\n"})
    shutil.move(str(wt), str(wt) + ".away")
    assert "ATTACH-WORKTREE" in refusal(root, verify=True)
    assert latest_seal(root) is None


def test_the_worktree_path_is_computed_not_read_from_the_branch_file(task, tmp_path):
    att, root, wt = task
    other = tmp_path / "elsewhere"
    other.mkdir()
    git(other, "init", "-q")
    path = os.path.join(root, ".alpaca", "attach", "branches", "t-001.json")
    data = json.load(open(path))
    data["worktree"] = str(other)
    open(path, "w").write(json.dumps(data))
    assert "elsewhere" not in (proofhook.header_line(root, "t-001") or "")
    assert seal(root)["attach"]["branch"] == "alpaca/t-001"


def test_a_plain_repository_in_place_of_the_worktree_is_refused(task):
    att, root, wt = task
    shutil.rmtree(wt)
    wt.mkdir()
    git(wt, "init", "-q")
    assert "not a linked worktree" in refusal(root)
    assert proofhook.header_line(root, "t-001").startswith("not read (")


def test_inherited_git_variables_cannot_aim_the_patch_at_the_studio(task, monkeypatch):
    att, root, wt = task
    commit_in(wt, {"src/main.py": "print('committed')\n"})
    monkeypatch.setenv("GIT_DIR", os.path.join(root, ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", root)
    monkeypatch.setenv("GIT_INDEX_FILE", os.path.join(root, ".git", "index"))
    got = proofhook.take_app_patch(root, "t-001")
    assert got is not None and b"src/main.py" in got[1]
    assert os.environ["GIT_DIR"] == os.path.join(root, ".git")     # the scrub is restored


# ---------------------------------------------------------------------- foreign config (PM t-650.5)
@pytest.mark.parametrize("lines", [
    ["[filter \"x\"]", "\tclean = touch PWNED; cat"],
    ["[filter \"x\"]", "\tsmudge = touch PWNED; cat"],
    ["[diff \"x\"]", "\ttextconv = touch PWNED; cat"],
    ["[core]", "\tfsmonitor = touch PWNED"],
])
def test_a_repo_config_that_runs_commands_refuses_the_seal_and_runs_nothing(task, lines):
    att, root, wt = task
    with open(os.path.join(att.app, ".git", "config"), "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    (wt / ".gitattributes").write_text("* filter=x diff=x\n")
    (wt / "new.txt").write_text("content\n")
    for verify in (True, False):
        assert "GIT-FILTER" in refusal(root, verify=verify)
    assert "not read (git config runs a command" in proofhook.header_line(root, "t-001")
    assert latest_seal(root) is None
    for where in (wt, att.app, root):
        assert not os.path.exists(os.path.join(str(where), "PWNED"))
    assert not os.path.exists("PWNED")


def test_take_app_patch_refuses_by_itself_before_git_add_runs_anything(task):
    att, root, wt = task
    git(att.app, "config", "filter.x.clean", "touch PWNED; cat")
    (wt / ".gitattributes").write_text("* filter=x\n")
    (wt / "new.txt").write_text("content\n")
    with pytest.raises(ValueError, match="GIT-FILTER"):
        proofhook.take_app_patch(root, "t-001")
    with pytest.raises(ValueError, match="GIT-FILTER"):
        proofhook.seal_block(root, "t-001")
    assert not os.path.exists(os.path.join(str(wt), "PWNED"))


def test_a_filter_set_through_an_include_is_found_too(task):
    att, root, wt = task
    inc = os.path.join(att.app, ".git", "extra.cfg")
    open(inc, "w").write("[filter \"x\"]\n\tclean = touch PWNED; cat\n")
    with open(os.path.join(att.app, ".git", "config"), "a", encoding="utf-8") as fh:
        fh.write("[include]\n\tpath = %s\n" % inc)
    (wt / ".gitattributes").write_text("* filter=x\n")
    assert "GIT-FILTER" in refusal(root, verify=True)
    assert not os.path.exists(os.path.join(str(wt), "PWNED"))


def test_a_harmless_fsmonitor_false_does_not_refuse(task):
    att, root, wt = task
    git(att.app, "config", "core.fsmonitor", "false")
    assert seal(root)["attach"]["head"]


# ---------------------------------------------------------------------------- evidence (T8, t-650.4)
def kept_item(payload, pointer_part):
    return [i for i in payload["evidence"] if pointer_part in i["pointer"]][0]


def test_dot_files_of_the_attached_repo_are_kept_but_closed(task):
    att, root, wt = task
    (att.app / ".github" / "flows").mkdir(parents=True)
    (att.app / ".github" / "flows" / "ci.yml").write_text("on: push\n")
    (wt / ".github" / "flows").mkdir(parents=True)
    (wt / ".github" / "flows" / "wt.yml").write_text("on: pull_request\n")
    payload = seal(root, evidence=[
        "local:app/.github/flows/ci.yml - the upstream CI",
        "local:.alpaca/attach/worktrees/t-001/.github/flows/wt.yml - the branch CI"])
    for part in ("ci.yml", "wt.yml"):
        item = kept_item(payload, part)
        assert item["kept"] and "/.closed/" in item["kept"], item
        assert os.path.isfile(os.path.join(root, item["kept"]))
        assert not paths.browser_serves(root, os.path.realpath(os.path.join(root, item["kept"])))


def test_credential_names_below_the_attached_repo_stay_refused(task):
    att, root, wt = task
    (att.app / ".husky").mkdir()
    (att.app / ".husky" / "pre-push").write_text("echo hi\n")
    (att.app / ".npmrc").write_text("//registry/:_authToken=abc\n")
    (att.app / ".env.local").write_text("KEY=1\n")
    payload = seal(root, evidence=[
        "local:app/.husky/pre-push - their hook",
        "local:app/.npmrc - registry config",
        "local:app/.git/config - the clone config"])
    assert kept_item(payload, "pre-push")["kept"]
    for part in (".npmrc", ".git/config"):
        item = kept_item(payload, part)
        assert item["kept"] is None and item["kept_reason"] == proof.HIDDEN_NOT_KEPT, item
    # a private runtime name is refused outright, as everywhere (the generic rule is not widened)
    assert "private runtime file" in refusal(root, evidence=["local:app/.env.local - env"])


def test_a_dot_file_in_the_studio_is_still_refused(task):
    att, root, wt = task
    util.write_text(os.path.join(root, "design", ".hidden.txt"), "x\n")
    payload = seal(root, evidence=["local:design/.hidden.txt - hidden"])
    assert payload["evidence"][0]["kept"] is None and payload["evidence"][0]["kept_reason"] == proof.HIDDEN_NOT_KEPT


def test_kept_copies_of_attached_repo_files_are_never_served(task):
    att, root, wt = task
    (att.app / "Core" / "Src").mkdir(parents=True)
    (att.app / "Core" / "Src" / "main.c").write_text("int main(void){return 0;}\n")
    payload = seal(root, evidence=["local:app/Core/Src/main.c - the entry point"])
    item = payload["evidence"][0]
    assert "/.closed/" in item["kept"]
    full = os.path.realpath(os.path.join(root, item["kept"]))
    assert not paths.browser_serves(root, full)
    served = [n for n in os.listdir(os.path.dirname(os.path.dirname(full))) if n.endswith("main.c")]
    assert served == []
    # the same file in the studio stays served, as before
    util.write_text(os.path.join(root, "notes.txt"), "plain\n")
    again = seal(root, evidence=["local:notes.txt - a studio file"])
    assert "/.closed/" not in again["evidence"][0]["kept"]


def test_proof_check_survives_the_worktree_directory_going_away(task):
    att, root, wt = task
    (wt / ".github" / "flows").mkdir(parents=True)
    (wt / ".github" / "flows" / "wt.yml").write_text("on: pull_request\n")
    (wt / "src" / "n.py").write_text("n = 1\n")
    seal(root, evidence=[
        "local:.alpaca/attach/worktrees/t-001/.github/flows/wt.yml - the branch CI",
        "local:.alpaca/attach/worktrees/t-001/src/n.py - new module"])
    shutil.move(str(wt), str(wt) + ".away")
    conn = db.connect(root)
    try:
        detail = proof.verify_detail(conn, root, "t-001")
    finally:
        conn.close()
    assert detail["problems"] == [], detail
    assert detail["ok"]


# ------------------------------------------------------------------------------- is_attach_path
def test_is_attach_path_and_the_dot_rule(task):
    att, root, wt = task
    cases = {
        os.path.join(root, "app", ".github", "x.yml"): (True, True),
        os.path.join(root, "app", "src", "a.py"): (True, True),
        os.path.join(root, "app", ".git", "config"): (True, False),
        os.path.join(root, "app", "sub", ".NPMRC"): (True, False),
        os.path.join(root, "app", ".env.production"): (True, False),
        os.path.join(root, ".alpaca", "attach", "worktrees", "t-001", ".husky", "h"): (True, True),
        os.path.join(root, ".alpaca", "attach", "worktrees", "t-001", ".git"): (True, False),
        os.path.join(root, ".alpaca", "attach", "state.json"): (False, False),
        os.path.join(root, ".alpaca", "attach", "worktrees"): (False, False),
        os.path.join(root, "design", ".x"): (False, False),
        os.path.join(root, "application", "a"): (False, False),
    }
    for path, (inside, dots) in cases.items():
        assert proofhook.is_attach_path(root, path) is inside, path
        assert proofhook.dot_part_allowed(root, path) is dots, path
