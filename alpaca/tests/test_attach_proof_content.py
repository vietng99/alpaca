"""The sealed app diff is the work, whatever the attached clone's own state says (op-062 t-650, fix round 2).

Round 2 found the round 1 class again, on the content path: the seal and the verifier read state of the
attached clone that an agent can write (a replace ref, a graft file, `diff.*` keys, a shallow file, a link in
the kept folder), and a seal went through whose app.patch did not describe the work, or a verification ended
`verified` on a tree that was not the worktree. The closers are:

  * every attach git call runs with replace objects and the graft file off (`gitops.clean_env`);
  * the app diff pins its path prefixes, and the seal records the tree id of its staged index copy
    (`tree.app.tree`); the verifier hashes `tree/app` in Python after the apply and refuses on any difference;
  * a work branch that adds, moves or drops a submodule entry is refused at the seal;
  * one record read decides the base of a seal, and a kept folder reached through a link is refused.
"""
import hashlib
import json
import os
import stat
import subprocess

import pytest

from alpaca import proof
from alpaca.attach import gitops, proofhook
from alpaca.research import verifier
from alpaca.tests.attachkit import attached, git, make_upstream, studio, upstream  # noqa: F401  (fixtures)
from alpaca.tests.attachproofkit import commit_in, cut, prepare, record_branch  # noqa: F401
from alpaca.tests.test_attach_proof import refusal, scaffold_text, seal, task  # noqa: F401
from alpaca.tests.test_attach_verify import probe, run, sealed, world  # noqa: F401
from alpaca.tests.test_research_verifier import events_of


def common_dir(wt):
    got = git(wt, "rev-parse", "--git-common-dir")
    return got if os.path.isabs(got) else os.path.join(str(wt), got)


def plant_replace(wt, victim, tree_of):
    """`refs/replace/<victim>` as a plain file (git_guard denies `git replace`, not a file write): a commit
    holding the tree of `tree_of`."""
    fake = git(wt, "commit-tree", git(wt, "rev-parse", "%s^{tree}" % tree_of), "-m", "replacement")
    ref = os.path.join(common_dir(wt), "refs", "replace", victim)
    os.makedirs(os.path.dirname(ref), exist_ok=True)
    with open(ref, "w") as fh:
        fh.write(fake + "\n")
    return fake


def patch_text(root, payload):
    return open(os.path.join(root, payload["tree"]["app"]["patch"]), encoding="utf-8").read()


def tree_of_dir(top):
    """{relative path: ("file"/"link", content or target, executable)} of everything below `top`."""
    out = {}
    for here, dirs, files in os.walk(top):
        for name in dirs + files:
            path = os.path.join(here, name)
            rel = os.path.relpath(path, top)
            if os.path.islink(path):
                out[rel] = ("link", os.readlink(path), False)
            elif os.path.isfile(path):
                out[rel] = ("file", open(path, "rb").read(), bool(os.stat(path).st_mode & stat.S_IXUSR))
    return out


def snapshot_probe(seen):
    """A scripted verifier step that records `tree/app` and `base/app` of each try."""
    def before(folder):
        seen.append({side: tree_of_dir(os.path.join(folder, side, "app")) for side in ("tree", "base")})
    return lambda slot, rnd, try_: {"before_submit": before}


def refused_stop(root):
    return events_of(root, "research-verify-refused")[-1]["data"]["problems"][0]


# ------------------------------------------------------------------------ B1: replace objects and grafts
def test_clean_env_turns_replace_objects_and_grafts_off_whatever_the_caller_set(monkeypatch):
    monkeypatch.setenv("GIT_NO_REPLACE_OBJECTS", "0")
    monkeypatch.setenv("GIT_GRAFT_FILE", "/tmp/not-null")
    monkeypatch.setenv("GIT_REPLACE_REF_BASE", "refs/other/")
    env = gitops.clean_env()
    assert env["GIT_NO_REPLACE_OBJECTS"] == "1" and env["GIT_GRAFT_FILE"] == os.devnull
    assert "GIT_REPLACE_REF_BASE" not in env


def test_a_replace_ref_on_the_base_does_not_hide_committed_work_from_the_app_patch(task):
    att, root, wt = task
    base = git(wt, "rev-parse", "HEAD")
    commit_in(wt, {"src/secret_feature.py": "feature = 1\n"})
    plant_replace(wt, base, "HEAD")
    payload = seal(root, verify=True)
    assert payload["tree"]["app"]["base"] == base
    assert "diff --git a/src/secret_feature.py b/src/secret_feature.py" in patch_text(root, payload)
    assert payload["tree"]["app"]["bytes"] > 0


def test_a_replace_ref_on_the_base_does_not_make_the_verifier_lay_the_work_into_base_app(world):
    base = git(world.wt, "rev-parse", "HEAD")
    commit_in(world.wt, {"src/main.py": "print('changed')\n", "src/new.py": "x = 1\n"})
    plant_replace(world.wt, base, "HEAD")
    sealed(world)
    seen = []
    s, got = run(world, snapshot_probe(seen))
    assert got["outcome"] == "verified", got
    assert seen
    for facts in seen:
        assert facts["base"]["src/main.py"][1] == b"print('hello')\n" and "src/new.py" not in facts["base"]
        assert facts["tree"]["src/main.py"][1] == b"print('changed')\n" and facts["tree"]["src/new.py"][1] == b"x = 1\n"


def test_a_replace_ref_planted_on_the_base_after_the_seal_stops_the_verifier_from_reading_it(world):
    """The replacement is written between the seal and the verification: the verifier lays the real base."""
    base = git(world.wt, "rev-parse", "HEAD")
    commit_in(world.wt, {"src/main.py": "print('changed')\n"})
    sealed(world)
    plant_replace(world.wt, base, "HEAD")
    seen = []
    s, got = run(world, snapshot_probe(seen))
    assert got["outcome"] == "verified", got
    assert all(f["base"]["src/main.py"][1] == b"print('hello')\n" for f in seen)


def test_a_graft_file_does_not_change_the_commit_count_of_the_header(task):
    att, root, wt = task
    commit_in(wt, {"a.txt": "a\n"}, "one")
    tip = commit_in(wt, {"b.txt": "b\n"}, "two")
    assert "2 commit(s) ahead" in proofhook.header_line(root, "t-001")
    os.makedirs(os.path.join(common_dir(wt), "info"), exist_ok=True)
    with open(os.path.join(common_dir(wt), "info", "grafts"), "w") as fh:
        fh.write(tip + "\n")                                  # the tip with no parent: a count of 1 without the rule
    assert "2 commit(s) ahead" in proofhook.header_line(root, "t-001")


def test_a_shallow_file_written_into_the_clone_is_refused(task):
    att, root, wt = task
    commit_in(wt, {"a.txt": "a\n"})
    head = git(wt, "rev-parse", "HEAD")
    with open(os.path.join(common_dir(wt), "shallow"), "w") as fh:
        fh.write(head + "\n")
    assert "is shallow" in refusal(root, verify=True)
    assert "shallow" in proofhook.header_line(root, "t-001")


# ------------------------------------------------------- B3: the tested tree is the sealed tree
FORMAT_KEYS = [("diff.dstPrefix", "b/x/"), ("diff.srcPrefix", "q/"), ("diff.noprefix", "true"),
               ("diff.mnemonicPrefix", "true"), ("color.diff", "always"), ("color.ui", "always"),
               ("diff.relative", "true"), ("diff.context", "0"), ("diff.renames", "copies"),
               ("core.quotePath", "false"), ("diff.algorithm", "patience"), ("diff.indentHeuristic", "false"),
               ("diff.interHunkContext", "9"), ("diff.suppressBlankEmpty", "true")]


@pytest.mark.parametrize("key,value", FORMAT_KEYS)
def test_a_diff_key_of_the_clone_does_not_move_the_work_in_the_verified_tree(world, key, value):
    git(world.att.app, "config", key, value)
    commit_in(world.wt, {"src/main.py": "print('changed')\n"})
    (world.wt / "src" / "new.py").write_text("x = 1\n")
    (world.wt / "src" / "café.py").write_text("y = 2\n")
    payload = sealed(world)
    assert "diff --git a/src/main.py b/src/main.py" in patch_text(world.root, payload)
    assert "b/x/" not in patch_text(world.root, payload) and "\x1b[" not in patch_text(world.root, payload)
    seen = []
    s, got = run(world, snapshot_probe(seen))
    assert got["outcome"] == "verified", got
    assert seen[0]["tree"]["src/main.py"][1] == b"print('changed')\n"
    assert seen[0]["tree"]["src/new.py"][1] == b"x = 1\n" and seen[0]["tree"]["src/café.py"][1] == b"y = 2\n"
    assert "src/new.py" not in seen[0]["base"]


def test_the_seal_records_the_tree_id_of_the_staged_copy_and_the_open_event_carries_it(world):
    commit_in(world.wt, {"src/main.py": "print('changed')\n"})
    (world.wt / "src" / "new.py").write_text("x = 1\n")
    payload = sealed(world)
    app = payload["tree"]["app"]
    git(world.wt, "add", "-A")
    assert app["tree"] == git(world.wt, "write-tree")
    s, got = run(world)
    assert got["outcome"] == "verified"
    assert events_of(world.root, "research-verify-open")[0]["data"]["tree"]["app_tree"] == app["tree"]


def forge(monkeypatch, make):
    """Make the seal honest about its patch bytes (the hash matches) but not about the work: `make` gets
    (patch, tree id, worktree) and returns the patch and tree id to seal."""
    real = proofhook.take_app_patch

    def bad(root, task_):
        head, patch, base, tree_id = real(root, task_)
        patch2, tree2 = make(patch, tree_id)
        return head, patch2, base, tree2

    monkeypatch.setattr(proofhook, "take_app_patch", bad)


def test_a_patch_that_applies_somewhere_else_stops_with_verify_tree(world, monkeypatch):
    """The patch is well formed and applies, but the paths are not the worktree's: the tree id says so."""
    commit_in(world.wt, {"src/main.py": "print('changed')\n"})
    (world.wt / "src" / "new.py").write_text("x = 1\n")
    forge(monkeypatch, lambda patch, tree_id: (patch.replace(b" b/src/", b" b/x/src/"), tree_id))
    sealed(world)
    s, got = run(world)
    assert got["state"] == "stopped" and got["code"] == "VERIFY-TREE", got
    stop = refused_stop(world.root)
    assert "does not give the sealed tree" in stop["detail"] and not s.specs


def test_a_patch_with_an_extra_file_stops_with_verify_tree(world, monkeypatch):
    commit_in(world.wt, {"src/main.py": "print('changed')\n"})
    extra = (b"diff --git a/src/extra.py b/src/extra.py\nnew file mode 100644\n--- /dev/null\n+++ b/src/extra.py\n"
             b"@@ -0,0 +1 @@\n+z = 3\n")
    forge(monkeypatch, lambda patch, tree_id: (patch + extra, tree_id))
    sealed(world)
    s, got = run(world)
    assert got["state"] == "stopped" and got["code"] == "VERIFY-TREE", got
    assert "does not give the sealed tree" in refused_stop(world.root)["detail"]


def test_a_wrong_tree_id_in_the_seal_stops_with_verify_tree(world, monkeypatch):
    commit_in(world.wt, {"src/main.py": "print('changed')\n"})
    forge(monkeypatch, lambda patch, tree_id: (patch, "0" * len(tree_id)))
    sealed(world)
    s, got = run(world)
    assert got["state"] == "stopped" and got["code"] == "VERIFY-TREE", got


def test_a_seal_with_no_tree_id_stops_with_verify_tree(world, monkeypatch):
    commit_in(world.wt, {"src/main.py": "print('changed')\n"})
    forge(monkeypatch, lambda patch, tree_id: (patch, None))
    sealed(world)
    s, got = run(world)
    assert got["state"] == "stopped" and got["code"] == "VERIFY-TREE", got
    assert "no tree id" in refused_stop(world.root)["detail"]


def test_a_mode_change_a_link_a_delete_a_rename_and_binary_bytes_verify(world):
    wt = world.wt
    base_main = (wt / "src" / "main.py").read_text()
    os.chmod(wt / "src" / "main.py", 0o755)                              # mode only
    os.symlink("main.py", wt / "src" / "alias.py")                       # a new link
    (wt / "src" / "blob.bin").write_bytes(bytes(range(256)) * 40)        # binary
    (wt / "docs").mkdir()
    (wt / "docs" / "crlf.txt").write_bytes(b"one\r\ntwo\r\n")           # line endings kept as they are
    (wt / "docs" / "empty.txt").write_bytes(b"")
    git(wt, "mv", "README.md", "docs/README.md")                         # a rename
    git(wt, "rm", "-q", "Makefile")                                      # a delete
    payload = sealed(world)
    assert payload["tree"]["app"]["bytes"] > 0
    seen = []
    s, got = run(world, snapshot_probe(seen))
    assert got["outcome"] == "verified", got
    got_tree = seen[0]["tree"]
    assert got_tree["src/main.py"] == ("file", base_main.encode(), True)
    assert got_tree["src/alias.py"] == ("link", "main.py", False)
    assert got_tree["src/blob.bin"][1] == bytes(range(256)) * 40
    assert got_tree["docs/crlf.txt"][1] == b"one\r\ntwo\r\n" and got_tree["docs/empty.txt"][1] == b""
    assert "README.md" not in got_tree and got_tree["docs/README.md"][1] == b"# demo app\n"
    assert "Makefile" not in got_tree and "Makefile" in seen[0]["base"]


def test_a_text_attribute_with_crlf_conversion_in_the_attached_repo_still_verifies(world):
    """`* text eol=crlf` in the work: git add stores LF blobs, the patch carries LF, the tree holds LF."""
    commit_in(world.wt, {".gitattributes": "*.txt text eol=crlf\n", "notes.txt": "a\nb\n"})
    (world.wt / "more.txt").write_bytes(b"c\r\nd\r\n")
    sealed(world)
    seen = []
    s, got = run(world, snapshot_probe(seen))
    assert got["outcome"] == "verified", got


# ----------------------------------------------- the Python tree hash is git's tree hash (unit)
def python_tree_id(top, algo):
    flat = verifier._walk_flat(top, algo)
    return verifier._tree_id(flat, algo)


@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
def test_the_python_tree_id_equals_git_write_tree(tmp_path, fmt):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "--object-format=%s" % fmt)
    # names whose git order differs from a plain sort: "a" the folder sorts as "a/", after "a-b" and before "a.b"
    for rel, data in {"a-b": b"1", "a.b": b"2", "a/inner.txt": b"3", "a/deep/x.bin": bytes(range(256)),
                      "z": b"", "café/ü.txt": b"u", "run.sh": b"#!/bin/sh\n"}.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    os.chmod(repo / "run.sh", 0o755)
    os.symlink("a/inner.txt", repo / "link")
    os.symlink("/nonexistent/target", repo / "dangling")
    git(repo, "add", "-A")
    want = git(repo, "write-tree")
    # the .git folder is not part of the walk target: hash a copy of the folder without it
    copy = tmp_path / "copy"
    subprocess.run(["cp", "-a", str(repo), str(copy)], check=True)
    subprocess.run(["rm", "-rf", str(copy / ".git")], check=True)
    assert python_tree_id(str(copy), fmt) == want


def test_the_python_tree_id_of_an_empty_folder_is_the_empty_tree(tmp_path):
    assert python_tree_id(str(tmp_path), "sha1") == hashlib.sha1(b"tree 0\0").hexdigest()


def test_a_fifo_in_the_applied_tree_is_not_hashed_as_a_file(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(RuntimeError):
        verifier._walk_flat(str(tmp_path), "sha1")


# --------------------------------------------------------------------------- B2: submodule entries
def test_a_committed_gitlink_is_refused_at_the_seal(task):
    att, root, wt = task
    base = git(wt, "rev-parse", "HEAD")
    (wt / ".gitmodules").write_text('[submodule "vendor/lib"]\n\tpath = vendor/lib\n\turl = https://example.invalid/x.git\n')
    git(wt, "add", ".gitmodules")
    git(wt, "update-index", "--add", "--cacheinfo", "160000,%s,vendor/lib" % base)
    git(wt, "commit", "-q", "-m", "vendor a submodule")
    text = refusal(root, verify=True)
    assert "ATTACH-SUBMODULE" in text and "vendor/lib" in text


def test_an_embedded_repository_is_refused_at_the_seal(task):
    att, root, wt = task
    inner = wt / "vendor2"
    inner.mkdir()
    git(inner, "init", "-q")
    (inner / "payload.py").write_text("evil = 1\n")
    git(inner, "add", "-A")
    git(inner, "commit", "-q", "-m", "inner")
    assert "ATTACH-SUBMODULE" in refusal(root, verify=True)


def test_a_moved_gitlink_and_a_dropped_gitlink_are_refused(attached):
    sub = attached.upstream.work
    sha = git(sub, "rev-parse", "HEAD")
    git(sub, "update-index", "--add", "--cacheinfo", "160000,%s,vendor/lib" % sha)
    git(sub, "commit", "-q", "-m", "vendor")
    git(sub, "push", "-q", "origin", "main")
    gitops.git(attached.app, "fetch", "-q", "origin")
    gitops.git(attached.app, "merge", "-q", "--ff-only", "origin/main")
    root = prepare(attached)
    wt = cut(attached)
    git(wt, "update-index", "--cacheinfo", "160000,%s,vendor/lib" % ("1" * 40))   # moved to another commit
    git(wt, "commit", "-q", "-m", "move")
    assert "ATTACH-SUBMODULE" in refusal(root, verify=True)
    git(wt, "rm", "-q", "--cached", "vendor/lib")                                # and then dropped
    git(wt, "commit", "-q", "-m", "drop")
    assert "ATTACH-SUBMODULE" in refusal(root, verify=True)


def test_a_gitlink_that_is_already_in_the_base_seals_and_verifies(attached, tmp_path):
    from alpaca.tests.test_research_verifier import MAKE_LOG, Scripted, run_verify, seal as vseal, \
        stand_in_binary, write_report
    from alpaca import util
    sub = attached.upstream.work
    sha = git(sub, "rev-parse", "HEAD")
    git(sub, "update-index", "--add", "--cacheinfo", "160000,%s,vendor/lib" % sha)
    git(sub, "commit", "-q", "-m", "vendor")
    git(sub, "push", "-q", "origin", "main")
    gitops.git(attached.app, "fetch", "-q", "origin")
    gitops.git(attached.app, "merge", "-q", "--ff-only", "origin/main")
    util.write_text(os.path.join(str(attached.root), "make_log.py"), MAKE_LOG)
    root = prepare(attached)
    wt = cut(attached)
    commit_in(wt, {"src/main.py": "print('changed')\n"})
    write_report(root)
    payload = vseal(root)
    assert "vendor" not in open(os.path.join(root, payload["tree"]["app"]["patch"]), encoding="utf-8").read()
    got = run_verify(root, Scripted(root), binary=stand_in_binary(tmp_path))
    assert got["outcome"] == "verified", got


# ----------------------------------------------------------------- B5: one record read per seal
def test_a_recut_between_the_patch_and_the_block_is_refused(task, monkeypatch):
    att, root, wt = task
    commit_in(wt, {"src/a.py": "a = 1\n"})
    new_base = git(wt, "rev-parse", "HEAD")
    real = proofhook.seal_block

    def recut_then_block(root_, task_, conn=None):
        branch_file = os.path.join(root_, ".alpaca", "attach", "branches", "t-001.json")
        data = json.load(open(branch_file))
        data["base_sha"] = new_base
        with open(branch_file, "w") as fh:
            fh.write(json.dumps(data))
        record_branch(root_, task_, branch=data["branch"], worktree=wt, base_sha=new_base)
        return real(root_, task_, conn)

    monkeypatch.setattr(proofhook, "seal_block", recut_then_block)
    assert "changed during the seal" in refusal(root, verify=True)


# ------------------------------------------------------ B8b: a kept folder reached through a link
def kept_folder(root):
    from alpaca import db
    conn = db.connect(root)
    try:
        return proof.kept_dir(root, proof.subject(conn, "t-001"))
    finally:
        conn.close()


def kept_files(top):
    """What a seal keeps that landed below `top`: patches and evidence folders (the report itself is written
    by the test helper before the seal, and may sit there when the link is above the report)."""
    found = []
    for here, dirs, files in os.walk(top):
        found += [os.path.join(here, d) for d in dirs if d.endswith(".evidence")]
        found += [os.path.join(here, f) for f in files if f.endswith((".patch", ".part")) or f == "t-001.md.seal.json"]
    return found


def test_kept_folder_problem_accepts_a_real_folder_and_a_missing_one(task):
    att, root, wt = task
    assert proofhook.kept_folder_problem(root, kept_folder(root)) is None
    os.makedirs(kept_folder(root))
    assert proofhook.kept_folder_problem(root, kept_folder(root)) is None


@pytest.mark.parametrize("level", ["evidence", "op", "proofs"])
def test_a_linked_kept_folder_carries_nothing_outside_the_studio(task, tmp_path, level):
    att, root, wt = task
    commit_in(wt, {"src/a.py": "payload = 1\n"})
    kept = kept_folder(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = {"evidence": kept, "op": os.path.dirname(kept), "proofs": os.path.dirname(os.path.dirname(kept))}[level]
    if os.path.isdir(victim):
        os.rename(victim, victim + ".moved")
    os.makedirs(os.path.dirname(victim), exist_ok=True)
    os.symlink(str(outside), victim)
    assert "is a link" in refusal(root, verify=True)
    assert kept_files(outside) == []
    assert "is a link" in refusal(root)                              # a seal without verification too
    assert kept_files(outside) == []


def test_a_kept_folder_that_is_a_link_is_refused_for_a_gone_worktree_too(task, tmp_path):
    att, root, wt = task
    outside = tmp_path / "outside"
    outside.mkdir()
    kept = kept_folder(root)
    os.makedirs(os.path.dirname(kept), exist_ok=True)
    os.symlink(str(outside), kept)
    gitops.git(att.app, "worktree", "remove", "--force", str(wt))
    assert "is a link" in refusal(root)
    assert kept_files(outside) == []


# ----------------------------------------------------- siblings: other config scopes of the clone
def test_a_filter_in_the_worktree_scope_config_is_refused_and_does_not_run(task, tmp_path):
    att, root, wt = task
    marker = tmp_path / "PWNED"
    git(att.app, "config", "extensions.worktreeConfig", "true")
    git(wt, "config", "--worktree", "filter.x.clean", "touch %s; cat" % marker)
    (wt / ".gitattributes").write_text("* filter=x\n")
    (wt / "a.txt").write_text("a\n")
    assert "GIT-FILTER" in refusal(root, verify=True)
    assert not marker.exists()
    assert "git config runs a command" in proofhook.header_line(root, "t-001")
