"""AC-10: the outgoing scan of an attached studio (op-062 t-651; pre-mortem t-651.4 and t-651.5).

A work branch is judged by what would really enter the upstream, bounded by the fetched base. Studio files
under any name, studio strings in any text that leaves, protected paths, a foreign identity and a missing or
empty term list all refuse. A refusal never repeats the term or the text that matched.
"""
import json
import os
import re
from pathlib import Path

import pytest

from alpaca import barrier
from alpaca.attach import gitops, scan
from alpaca.gates import verdict as vc
from alpaca.tests.attachkit import (AGENT_ENV, add_worktree, attached, commit_in, git, studio, upstream,  # noqa: F401
                                    with_term_list)

SEALED = "zebra-sealed-term"


@pytest.fixture
def ready(attached):
    """A task worktree on alpaca/t-001 and a term list with the attach minimum plus one studio term."""
    with_term_list(attached, extra=[SEALED])
    wt = add_worktree(attached, "t-001")
    ready_world = type("Ready", (), {})()
    ready_world.att, ready_world.wt, ready_world.studio, ready_world.app = attached, wt, attached.root, attached.app
    ready_world.base = git(attached.app, "rev-parse", "origin/main")
    return ready_world


def run(w, head=None, *, base=None, branch="alpaca/t-001", title="Fix the demo", body="Fixes the demo.\n"):
    head = head or git(w.wt, "rev-parse", "HEAD")
    return scan.outgoing(w.studio, w.wt, head, w.base if base is None else base, branch=branch, title=title, body=body)


def fresh_branch(w, name):
    git(w.wt, "checkout", "-q", "-B", name, w.base)


def test_a_clean_branch_passes_and_counts_what_enters(ready):
    commit_in(ready.wt, {"src/new.py": "print('new')\n"}, "Add a script for t-001")
    result = run(ready)
    assert result.verdict == vc.PASS and not result.refused and result.reasons == []
    assert result.commits_scanned == 1 and result.objects_scanned >= 3
    assert result.report["commits_scanned"] == 1 and result.report["verdict"] == "PASS"


@pytest.mark.parametrize("path", [".alpaca/x", "sub/.alpaca/y", "ALPACA-MANIFEST", "RESUME.md", "CHECKLIST.md",
                                  ".env"])
def test_protected_paths_refuse(ready, path):
    commit_in(ready.wt, {path: "x\n"}, "add it")
    result = run(ready)
    assert result.refused and barrier.R_PROTECTED in result.reasons, path


STUDIO_FILES = {"design/attach/codebase-map.md": "# map of the codebase\narea one is about parsing\n",
                "doctrine/CORE-CARD.md": "core card text that only the studio holds\n",
                "runbooks/runbook.yaml": "runbook: 1\nstages: [a, b]\n",
                "contracts/rules.md": "the rules of the house\n",
                ".alpaca/proofs/op-001/t-001.md": "a sealed proof report body\n"}


@pytest.mark.parametrize("src", sorted(STUDIO_FILES))
def test_studio_blobs_refuse_under_any_name(ready, src):
    target = ready.studio / src
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(STUDIO_FILES[src])
    commit_in(ready.wt, {"docs/notes.md": STUDIO_FILES[src]}, "document it")
    result = run(ready)
    assert result.refused and scan.R_STUDIO_BLOB in result.reasons


def test_a_file_the_studio_tracks_in_git_refuses_even_outside_the_named_folders(ready):
    (ready.studio / "NOTES-FOR-US.txt").write_text("private studio working notes, version two\n")
    git(ready.studio, "add", "NOTES-FOR-US.txt")
    git(ready.studio, "commit", "-q", "-m", "notes")
    commit_in(ready.wt, {"misc/readme-two.txt": "private studio working notes, version two\n"}, "copy")
    assert scan.R_STUDIO_BLOB in run(ready).reasons


def test_the_empty_file_is_not_a_studio_blob(ready):
    (ready.studio / "design").mkdir()
    (ready.studio / "design" / "empty.txt").write_text("")
    commit_in(ready.wt, {"pkg/__init__.py": ""}, "package marker")
    assert run(ready).verdict == vc.PASS


HOME = os.path.realpath(os.path.expanduser("~"))
#: (text, a branch name that carries it and is a legal ref name)
STRINGS = [("/var/tmp/claude-1002/-home-u-Proj/scratchpad/x", "alpaca//var/tmp/claude-1002/x"),
           ("session 192d7716-be95-4870-a09e-1780e634506b", "alpaca/192d7716-be95-4870-a09e-1780e634506b"),
           ("see op-062 t-654", "alpaca/op-062-fix"),
           (HOME, "alpaca" + HOME)]


def _arrange(w, where, text, branch_name, n):
    """Put `text` in one place a branch can carry it; return the keyword arguments for `run`."""
    fresh_branch(w, "alpaca/case-%d" % n)
    kwargs = {"branch": "alpaca/case-%d" % n}
    if where == "commit-message":
        commit_in(w.wt, {"src/ok.py": "x = %d\n" % n}, "change: %s" % text)
        return kwargs
    commit_in(w.wt, {"src/ok.py": ("# %s\n" % text) if where == "blob" else ("x = %d\n" % n)}, "change")
    if where == "pr-body":
        kwargs["body"] = "Notes: %s\n" % text
    elif where == "pr-title":
        kwargs["title"] = "Fix %s" % text
    elif where == "branch-name":
        kwargs["branch"] = branch_name           # the scan reads the name as given
    return kwargs


@pytest.mark.parametrize("where", ["commit-message", "blob", "pr-body", "pr-title", "branch-name"])
def test_studio_strings_refuse_everywhere_they_can_leave(ready, where):
    for n, (text, branch_name) in enumerate(STRINGS):
        result = run(ready, **_arrange(ready, where, text, branch_name, n))
        assert result.refused, (where, text)
        assert barrier.R_LEAK in result.reasons, (where, text)
        assert text not in json.dumps(result.report) and text not in result.reason


def test_a_task_id_and_ordinary_words_pass_everywhere(ready):
    commit_in(ready.wt, {"src/ok.py": "# fixes t-001\n"}, "Fix the parser for t-001")
    assert run(ready, title="Fix the parser (t-001)", body="Changes the parser; see t-001.\n").verdict == vc.PASS


def test_a_sealed_term_in_a_blob_fails_and_the_report_does_not_repeat_it(ready):
    commit_in(ready.wt, {"src/ok.py": "# the %s is here\n" % SEALED}, "change")
    result = run(ready)
    assert result.refused and result.verdict == vc.FAIL and barrier.R_LEAK in result.reasons
    assert SEALED not in json.dumps(result.report) and SEALED not in result.reason


def test_a_sealed_term_in_the_path_of_a_new_file_fails(ready):
    commit_in(ready.wt, {"src/%s.py" % SEALED: "x = 1\n"}, "change")
    assert run(ready).refused


# ---- pre-mortem t-651.4: bounded by the fetched base ---------------------------------------------------------------

def test_scan_is_bounded_by_the_upstream_base(ready):
    commit_in(ready.wt, {"src/new.py": "print('new')\n"}, "Add a script")
    head = git(ready.wt, "rev-parse", "HEAD")
    ready.att.upstream.commit({"docs/ref.md": "from a colleague\n"}, "colleague change", author="Real Person")
    git(ready.att.upstream.work, "-c", "user.email=real.person@corp.example.org", "commit", "-q", "--amend",
        "--no-edit", "--author", "Real Person <real.person@corp.example.org>",
        env={"GIT_COMMITTER_NAME": "Real Person", "GIT_COMMITTER_EMAIL": "real.person@corp.example.org"})
    git(ready.att.upstream.work, "push", "-q", "-f", "origin", "main")
    git(ready.app, "fetch", "-q", "origin")
    new_base = git(ready.app, "rev-parse", "origin/main")
    assert new_base != ready.base
    result = run(ready, head, base=new_base)
    assert result.verdict == vc.PASS and result.commits_scanned == 1
    # the same branch scanned against a base that is not an ancestor-aware cut (zero) would meet other people
    everything = barrier.commits_entering(str(ready.wt), head, "0" * 40)
    assert len(everything) > 1


def test_an_all_zero_or_unknown_base_is_refused_not_scanned(ready):
    commit_in(ready.wt, {"src/new.py": "x = 1\n"}, "change")
    for bad in ("0" * 40, "", "deadbeef" * 5, "not-a-sha"):
        result = run(ready, base=bad)
        assert result.refused and scan.R_BASE in result.reasons, bad


def test_a_file_the_repository_already_had_under_a_protected_name_does_not_block_a_branch(attached):
    attached.upstream.commit({"RESUME.md": "their own resume notes\n"}, "their resume file")
    git(attached.app, "fetch", "-q", "origin")
    with_term_list(attached)
    wt = add_worktree(attached, "t-001")
    commit_in(wt, {"src/new.py": "x = 1\n"}, "change")
    base = git(attached.app, "rev-parse", "origin/main")
    result = scan.outgoing(attached.root, wt, git(wt, "rev-parse", "HEAD"), base, branch="alpaca/t-001", title="t", body="b")
    assert result.verdict == vc.PASS, result.reason


# ---- identity ---------------------------------------------------------------------------------------------------------

def test_a_foreign_identity_refuses_unless_an_allow_rule_names_it(ready):
    commit_in(ready.wt, {"src/new.py": "x = 1\n"}, "change",
              env={"GIT_AUTHOR_NAME": "Ada Author", "GIT_AUTHOR_EMAIL": "ada@example.org",
                   "GIT_COMMITTER_NAME": "Ada Author", "GIT_COMMITTER_EMAIL": "ada@example.org"})
    result = run(ready)
    assert result.refused and scan.R_IDENTITY in result.reasons
    from alpaca import project
    cfg = project.load(str(ready.studio))
    cfg["barrier"] = dict(cfg["barrier"], allow=[r"ada@example\.org  # the test fixture author"])
    project.save(str(ready.studio), cfg)
    assert run(ready).verdict == vc.PASS


def test_the_attach_identity_passes_without_any_allow_rule(ready):
    commit_in(ready.wt, {"src/new.py": "x = 1\n"}, "change", env=AGENT_ENV)
    assert run(ready).verdict == vc.PASS


def test_the_attach_name_with_another_address_is_not_the_attach_identity(ready):
    commit_in(ready.wt, {"src/new.py": "x = 1\n"}, "change",
              env={"GIT_AUTHOR_NAME": "Alpaca agent", "GIT_AUTHOR_EMAIL": "someone@corp.example.net",
                   "GIT_COMMITTER_NAME": "Alpaca agent", "GIT_COMMITTER_EMAIL": "someone@corp.example.net"})
    assert scan.R_IDENTITY in run(ready).reasons


# ---- the term list ------------------------------------------------------------------------------------------------------

def test_an_empty_term_list_refuses(ready):
    commit_in(ready.wt, {"src/new.py": "x = 1\n"}, "change")
    (ready.studio / ".alpaca" / "sealed-terms.txt").write_text("")
    result = run(ready)
    assert result.refused and "term list" in result.reason
    assert barrier.R_TERMS_MISSING in result.reasons


def test_a_missing_term_list_file_refuses(ready):
    commit_in(ready.wt, {"src/new.py": "x = 1\n"}, "change")
    (ready.studio / ".alpaca" / "sealed-terms.txt").unlink()
    result = run(ready)
    assert result.refused and "term list" in result.reason


def test_a_studio_that_names_no_term_list_refuses(attached):
    wt = add_worktree(attached, "t-001")
    commit_in(wt, {"src/new.py": "x = 1\n"}, "change")
    base = git(attached.app, "rev-parse", "origin/main")
    result = scan.outgoing(attached.root, wt, git(wt, "rev-parse", "HEAD"), base, branch="alpaca/t-001", title="t", body="b")
    assert result.refused and "term list" in result.reason


def test_the_attach_minimum_is_added_to_a_list_that_has_only_its_own_terms(attached):
    with_term_list(attached, text="my-own-secret-term\n")
    tl = scan.term_list(attached.root)
    keys = {key for _term, key in tl.terms}
    assert "my-own-secret-term" in keys
    assert os.path.realpath(str(attached.root)).casefold() in keys
    assert len(tl.regexes) == len(scan.ATTACH_MINIMUM)
    wt = add_worktree(attached, "t-001")
    commit_in(wt, {"src/new.py": "# op-123\n"}, "change")
    base = git(attached.app, "rev-parse", "origin/main")
    result = scan.outgoing(attached.root, wt, git(wt, "rev-parse", "HEAD"), base, branch="alpaca/t-001", title="t", body="b")
    assert result.refused and barrier.R_LEAK in result.reasons


def test_text_problems_names_the_place_and_never_the_text(attached):
    with_term_list(attached)
    assert scan.text_problems(attached.root, "A plain description of t-001.", "pr-body") == []
    problems = scan.text_problems(attached.root, "path %s here" % attached.root, "pr-body")
    assert problems and "pr-body" in problems[0] and str(attached.root) not in problems[0]
    (attached.root / ".alpaca" / "sealed-terms.txt").write_text("")
    assert "cannot be checked" in scan.text_problems(attached.root, "x", "pr-title")[0]


def test_inherited_git_variables_cannot_redirect_the_scan_to_the_studio(ready, monkeypatch):
    commit_in(ready.wt, {"src/new.py": "x = 1\n"}, "change")
    monkeypatch.setenv("GIT_DIR", str(ready.studio / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(ready.studio))
    result = run(ready)
    assert result.verdict == vc.PASS and result.commits_scanned == 1


def test_a_branch_with_nothing_new_passes_with_zero_commits(ready):
    result = run(ready, head=ready.base)
    assert result.verdict == vc.PASS and result.commits_scanned == 0


def test_a_hardlinked_or_oversized_blob_is_not_silently_passed(ready):
    """A blob the barrier cannot read in full refuses (its own rule), so a large file cannot carry anything out."""
    big = b"A" * (leak_limit() + 10)
    commit_in(ready.wt, {"assets/big.bin": big}, "binary")
    result = run(ready)
    assert result.refused


def leak_limit():
    from alpaca.gates import leak_audit
    return leak_audit.TEXT_SCAN_MAX_BYTES


def test_the_studio_blob_set_reads_tracked_and_named_folders(ready):
    (ready.studio / "doctrine").mkdir(exist_ok=True)
    (ready.studio / "doctrine" / "leaf.md").write_text("a leaf\n")
    blobs = scan.studio_blob_set(ready.studio)
    assert git(ready.studio, "hash-object", "--no-filters", "doctrine/leaf.md") in blobs
    assert scan.EMPTY_BLOB not in blobs
    assert all(re.fullmatch(r"[0-9a-f]{40}", b) for b in blobs)


def test_a_work_folders_git_file_does_not_choose_what_the_scan_reads(ready, tmp_path):
    """Integration of t-648 and t-651 (op-062): git never runs in a task work folder by its own `.git` file
    (gitops.run refuses it). The scan reads the attached clone, which holds the work branch's commits, so a
    rewritten `.git` file changes nothing; a work folder of another studio is refused."""
    head = commit_in(ready.wt, {"src/new.py": "x = 1\n"}, "change")
    (ready.wt / ".git").write_text("gitdir: %s\n" % (ready.studio / ".git"))
    result = run(ready, head=head)
    assert result.verdict == vc.PASS and result.commits_scanned == 1
    elsewhere = tmp_path / "other" / ".alpaca" / "attach" / "worktrees" / "t-001"
    elsewhere.mkdir(parents=True)
    result = scan.outgoing(ready.studio, elsewhere, head, ready.base, branch="alpaca/t-001", title="t", body="b")
    assert result.refused and scan.R_BASE in result.reasons


# ---- the studio blob set covers the working tree (op-062 t-686 fix round 1, B2) -----------------------------------------

def commit_studio(studio):
    git(studio, "add", "-A")
    git(studio, "commit", "-q", "-m", "studio state")


#: files of a studio under names the shipped .gitignore keeps out of git, and files nothing tracks yet; each holds bytes
#: with no sealed term, so only the blob rule can refuse a copy
STUDIO_ONLY = {"untracked": "intents/queue.md", "ignored-resume": "RESUME.md", "ignored-runtime": ".alpaca/instance.json",
               "ignored-attach": ".alpaca/attach/notes/plan.json", "root-file": "STUDIO-SETUP-NOTES.txt",
               "dist": "dist/bundle.txt"}


@pytest.mark.parametrize("committed", [False, True], ids=["no-commit", "committed"])
@pytest.mark.parametrize("name", sorted(STUDIO_ONLY))
def test_a_studio_file_copied_under_a_neutral_name_is_refused_with_or_without_a_studio_commit(ready, name, committed):
    """B2: a studio made by `alpaca new` has no commit, and its shipped .gitignore keeps `.alpaca/` and RESUME.md out of git;
    `ls-tree HEAD` listed nothing of them, so a byte-for-byte copy went out under another name."""
    rel = STUDIO_ONLY[name]
    text = "studio-only bytes of %s, line one\nline two for the size\n" % name
    target = ready.studio / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    if committed:
        commit_studio(ready.studio)
    commit_in(ready.wt, {"docs/neutral-%s.txt" % name: text}, "document it")
    result = run(ready)
    assert result.refused and scan.R_STUDIO_BLOB in result.reasons, (name, committed, result.reasons)


@pytest.mark.parametrize("how", ["committed-then-removed", "staged-then-removed"])
def test_a_studio_file_that_is_gone_from_the_working_tree_still_counts_through_the_studios_git(ready, how):
    text = "bytes that only a removed studio file held, version one\nsecond line\n"
    target = ready.studio / "old-plan.txt"
    target.write_text(text)
    git(ready.studio, "add", "old-plan.txt")
    if how == "committed-then-removed":
        git(ready.studio, "commit", "-q", "-m", "plan")
        git(ready.studio, "rm", "-q", "old-plan.txt")
        git(ready.studio, "commit", "-q", "-m", "drop the plan")
    else:
        target.unlink()
    assert not target.exists()
    commit_in(ready.wt, {"docs/neutral.txt": text}, "document it")
    result = run(ready)
    assert result.refused and scan.R_STUDIO_BLOB in result.reasons


def test_a_branch_with_its_own_bytes_passes_beside_every_kind_of_studio_file(ready):
    for rel in STUDIO_ONLY.values():
        target = ready.studio / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("studio bytes of %s\n" % rel)
    commit_in(ready.wt, {"src/own.py": "OWN = 1\n"}, "own work")
    result = run(ready)
    assert result.verdict == vc.PASS, result.reason


@pytest.mark.parametrize("rel,refused", [
    (".alpaca/attach/runs/t-001/seed/src/x.txt", False), (".alpaca/attach/runs/_baseline/k/work/x.txt", False),
    (".alpaca/attach/stages/t-001/s1/out.txt", False), (".alpaca/attach/study/abc/src/x.txt", False),
    (".alpaca/attach/push/t-001-ab/x.txt", False), (".alpaca/worktrees/research/r-1/x.txt", False),
    (".alpaca/research/r-1/contract/files/src/x.txt", False), ("app/vendor/x.txt", False),
    (".alpaca/research/r-1/contract/files/@studio/research/x.txt", True), (".alpaca/research/r-1/x.txt", True),
    (".alpaca/attach/branches/x.txt", True), (".alpaca/other/x.txt", True), ("scratch/x.txt", True)])
def test_the_folders_that_hold_the_repositorys_own_bytes_do_not_count_as_studio_files(ready, rel, refused):
    """Copies of the repository's files in a gate clone, a seed, stage evidence, the study's reading copy, a research
    worktree, a frozen copy or the clone itself are the repository, not the studio: a handoff of those bytes passes."""
    text = "bytes of the repository itself, kept at %s\nsecond line\n" % rel
    target = ready.studio / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    commit_in(ready.wt, {"src/copy.txt": text}, "add the file")
    result = run(ready)
    assert result.refused == refused, (rel, result.reasons)
    assert bool({scan.R_STUDIO_BLOB, scan.R_PACK} & set(result.reasons)) == refused      # a frozen pack file is a pack


def test_the_studio_blob_set_is_sized_and_has_no_size_cap(ready):
    big = b"B" * ((16 << 20) + 5)
    (ready.studio / "big.bin").write_bytes(big)
    (ready.studio / "small.txt").write_text("small studio file\n")
    big_id, small_id = git(ready.studio, "hash-object", "--no-filters", "big.bin"), git(ready.studio, "hash-object", "--no-filters", "small.txt")
    assert {big_id, small_id} <= scan.studio_blob_set(ready.studio)
    only_big = scan.studio_blob_set(ready.studio, sizes={len(big)})
    assert big_id in only_big and small_id not in only_big
    assert small_id not in scan.studio_blob_set(ready.studio, sizes={3})


def test_the_studio_blob_set_hashes_an_odd_file_name_and_survives_an_unreadable_file(ready):
    (ready.studio / "line\nbreak.txt").write_text("a file whose name holds a newline\n")
    (ready.studio / "fine.txt").write_text("a readable file beside the others\n")
    locked = ready.studio / "locked.txt"
    locked.write_text("a file nobody can read\n")
    locked.chmod(0)
    try:
        blobs = scan.studio_blob_set(ready.studio)
    finally:
        locked.chmod(0o644)
    for rel in ("line\nbreak.txt", "fine.txt"):
        assert git(ready.studio, "hash-object", "--no-filters", "--", rel) in blobs
    if os.geteuid() != 0:                                              # root reads it anyway
        assert git(ready.studio, "hash-object", "--no-filters", "locked.txt") not in blobs


def test_the_studio_blob_set_does_not_follow_a_link(ready, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("bytes outside the studio\n")
    (ready.studio / "link.txt").symlink_to(outside)
    (ready.studio / "linked-dir").symlink_to(tmp_path)
    assert git(ready.studio, "hash-object", "--no-filters", str(outside)) not in scan.studio_blob_set(ready.studio)


def test_the_blob_sizes_of_a_cat_file_listing_are_read_or_refused():
    assert scan.blob_sizes("blob 12\ntree 80\ncommit 200\ntag 150\nblob 12\n", 5) == {12}
    for text, count in (("blob 12\n", 2), ("%s missing\n" % ("a" * 40), 1), ("blob x\n", 1), ("", 1)):
        with pytest.raises(scan.AttachError):
            scan.blob_sizes(text, count)
