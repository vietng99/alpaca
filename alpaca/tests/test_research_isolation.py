"""t-091: attempt worktrees and purge (alpaca/research/isolation.py).

Done when (the contract of t-091, design/research-engine/architecture.md sections 3.1, 5.3, 10.5):

  * a planted ignored build output is deleted before the eval (purge_ignored), a kept path stays,
    and after the eval a new ignored path, a changed file or a nested repository marks the tree
    not ok (post_eval);
  * two concurrent attempts never share a worktree: each has its own tree, admin folder, host git
    folder and private store, and a `gitdir` and `locked` file one attempt plants naming the
    other's paths change nothing;
  * a crashed attempt's worktree is reclaimed on resume from the recorded paths only;
  * a repair attempt's worktree holds no commit after its base: a search for the fix commit from
    inside the worktree finds nothing (gap map H14);
  * a repair attempt's worktree path and branch name carry no bug or fix identifier (H14);
  * git in attempt worktrees runs with commit signing off, so no git command waits on a signing
    prompt (F03).

Fail case: the eval reads a file the attempt agent could plant outside the tracked diff. Besides
ignored outputs, the places git never lists are covered: the root `.git` (put back to the
engine's bytes before the eval, listed in post_eval's changed after it), folders that hold no
tracked path, FIFOs and sockets, and entries inside a gitlink folder (removed before the eval,
flagged after it), tracked bytes a `.gitattributes` clean conversion kept out of the diff (every
tracked file is rewritten from head before the eval), and the mode, user.* xattrs, POSIX ACLs
and mtimes of tracked files and folders (reset before the eval).

Each control has a positive and a negative case. Every write goes to tmp_path; no test touches the
network or starts an agent runtime: "agent git" below is plain git run in the tree as the jailed
implementer runs it (its private object store, the clone's objects as the alternate).
"""
from __future__ import annotations

import dataclasses
import fnmatch
import hashlib
import os
import shutil
import socket
import stat
import subprocess
import threading
import time

import pytest

from alpaca import runbook
from alpaca.research import isolation as iso
from alpaca.research import paths
from alpaca.research.errors import IsolationError
from alpaca.research.types import Contract, DiffCapture, WorktreeInfo

STAMP = "20260930t061500z"
RUN = "rr-001"
FIX_TEXT = "the one line that fixes BUG-4711"


# ---------------------------------------------------------------- helpers

def _env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Dev",
               GIT_AUTHOR_EMAIL="dev@example.invalid", GIT_COMMITTER_NAME="Dev",
               GIT_COMMITTER_EMAIL="dev@example.invalid")
    env.update(extra)
    return env


def _g(cwd, *args, env=None, check=True):
    res = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd,
                         env=env or _env(), capture_output=True, timeout=60)
    if check and res.returncode != 0:
        raise AssertionError("git %s: %s" % (args, res.stderr.decode()))
    return res.stdout.decode("utf-8", "replace").strip()


def _write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if mode:
        os.chmod(path, mode)


def _contract(base, **over):
    values = {f.name: () for f in dataclasses.fields(Contract)}
    values.update(repo_path=".", repo_commit=base, hidden=("secret/",),
                  holdout_paths=("bench/holdout/",), keep=(), outputs=())
    values.update(over)
    return Contract(**values)


def _strict(path, rules):
    """The strict matcher of runbook-format-3.md section 14.1 as a test fake until t-096 lands:
    every rule read from the root, case kept, a folder rule admits that folder and below."""
    for rule in rules:
        if rule.endswith("/"):
            if path == rule[:-1] or path.startswith(rule):
                return rule
        elif fnmatch.fnmatchcase(path, rule) and path.count("/") == rule.count("/"):
            return rule
    return None


@pytest.fixture(autouse=True)
def matcher(monkeypatch):
    if not hasattr(runbook, "path_admitted"):
        monkeypatch.setattr(runbook, "path_admitted", _strict, raising=False)


@pytest.fixture
def repo(tmp_path):
    """A target project: history, a tag, a remote, held-out and hidden data, the reproducer store,
    a tracked `.alpaca/` and memory-class files, a gitlink, a symlink, an executable, an
    export-ignore file; then a fix commit after the base."""
    root = str(tmp_path / "proj")
    os.makedirs(root)
    _g(root, "init", "-q", "-b", "main")
    _write(os.path.join(root, "src/app.py"), "def f():\n    return 1\n")
    _write(os.path.join(root, "src/run.sh"), "#!/bin/sh\necho run\n", mode=0o755)
    _write(os.path.join(root, ".gitignore"), "build/\n*.log\nout/\ncache/\n")
    _write(os.path.join(root, "bench/holdout/case1.txt"), "held-out case one\n")
    _write(os.path.join(root, "bench/runbook.yaml"), "holdout: [bench/holdout/]\n")
    _write(os.path.join(root, "secret/hidden.txt"), "hidden data\n")
    _write(os.path.join(root, "contracts/research/rb/case1/run.sh"), "#!/bin/sh\nexit 1\n")
    _write(os.path.join(root, ".alpaca/state.json"), "{}\n")
    _write(os.path.join(root, "RESUME.md"), "pad\n")
    _write(os.path.join(root, "ALPACA-MANIFEST"), "[mechanism]\nsrc/\n[memory]\n.alpaca/\nRESUME.md\n")
    _write(os.path.join(root, "docs/internal.md"), "kept although export-ignore\n")
    _write(os.path.join(root, ".gitattributes"), "docs/internal.md export-ignore\n")
    os.symlink("app.py", os.path.join(root, "src/link.py"))
    _g(root, "add", "-A")
    _g(root, "update-index", "--add", "--cacheinfo",
       "160000,1111111111111111111111111111111111111111,vendor/lib")
    _g(root, "commit", "-qm", "base")
    base = _g(root, "rev-parse", "HEAD")
    _write(os.path.join(root, "src/app.py"), "def f():\n    return 2  # %s\n" % FIX_TEXT)
    _write(os.path.join(root, "src/fix_only_helper.py"), "HELP = 1\n")
    _g(root, "add", "-A")
    _g(root, "commit", "-qm", "Fix BUG-4711")
    fix = _g(root, "rev-parse", "HEAD")
    _g(root, "tag", "v1")
    _g(root, "remote", "add", "origin", "https://example.invalid/repo.git")
    return {"root": root, "base": base, "fix": fix}


@pytest.fixture
def clone(repo):
    return iso.build_clone(repo["root"], RUN, _contract(repo["base"]), stamp=STAMP)


def _agent(wt, clone, *args, env=None, check=True):
    """git as the jailed implementer runs it: in the tree, through its `.git` file, with its
    private object store and the clone's objects as the alternate."""
    extra = {"GIT_ALTERNATE_OBJECT_DIRECTORIES": os.path.join(clone.path, ".git", "objects")}
    if wt.private_objects:
        extra["GIT_OBJECT_DIRECTORY"] = wt.private_objects
    return _g(wt.path, *args, env=env or _env(**extra), check=check)


def _tree_files(path):
    out = []
    for folder, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            rel = os.path.relpath(os.path.join(folder, name), path)
            if rel != ".git":
                out.append(rel)
    return sorted(out)


def _marker_program(tmp_path, name="evil.sh"):
    mark = str(tmp_path / ("RAN-" + name))
    prog = str(tmp_path / name)
    _write(prog, "#!/bin/sh\necho ran >> %s\ncat\n" % mark, mode=0o755)
    return prog, mark


# ---------------------------------------------------------------- the stripped clone

def test_the_clone_holds_the_base_tree_minus_held_out_hidden_store_and_memory(repo, clone):
    files = _g(clone.path, "ls-tree", "-r", "--name-only", clone.base_commit).splitlines()
    assert "src/app.py" in files and "bench/runbook.yaml" in files
    assert "docs/internal.md" in files            # export-ignore changes nothing: no git archive
    for gone in ("bench/holdout/case1.txt", "secret/hidden.txt",
                 "contracts/research/rb/case1/run.sh", ".alpaca/state.json", "RESUME.md",
                 "vendor/lib"):
        assert gone not in files, gone
    modes = {line.split("\t")[1]: line.split()[0] for line in
             _g(clone.path, "ls-tree", "-r", clone.base_commit).splitlines()}
    assert modes["src/run.sh"] == "100755" and modes["src/link.py"] == "120000"
    assert "160000" not in modes.values()
    # one root commit, no tags, no remotes, no fix; main and the research branch at the base
    assert _g(clone.path, "rev-list", "--all", "--count") == "1"
    assert _g(clone.path, "tag", "-l") == "" and _g(clone.path, "remote") == ""
    assert _g(clone.path, "rev-parse", "main") == clone.base_commit
    assert _g(clone.path, "rev-parse", "research/%s" % RUN) == clone.base_commit
    assert clone.branch == "research/%s" % RUN and clone.head == clone.base_commit
    assert os.listdir(os.path.join(clone.path, ".git", "worktrees")) == []
    cfg = _g(clone.path, "config", "--file", ".git/config", "--list")
    assert "commit.gpgsign=false" in cfg and "hookspath" not in cfg.lower()
    # the working folder of the clone holds only .git: trees are cut from its objects
    assert os.listdir(clone.path) == [".git"]
    # negative: the source still holds all of it
    src = _g(repo["root"], "ls-tree", "-r", "--name-only", repo["base"]).splitlines()
    assert "bench/holdout/case1.txt" in src and "RESUME.md" in src


def test_the_clone_is_deterministic_and_open_clone_reads_it(repo, clone):
    again = iso.build_clone(repo["root"], "rr-002", _contract(repo["base"]), stamp=STAMP)
    assert again.base_commit == clone.base_commit
    later = iso.build_clone(repo["root"], "rr-003", _contract(repo["base"]),
                            stamp="20261001t000000z")
    assert later.base_commit != clone.base_commit            # the date is part of the commit
    assert iso.open_clone(repo["root"], RUN) == clone
    assert iso.open_clone(repo["root"], "rr-009") is None
    with pytest.raises(IsolationError) as exc:
        iso.build_clone(repo["root"], RUN, _contract(repo["base"]), stamp=STAMP)
    assert exc.value.reason == "ISOLATION-PATH-TAKEN"


@pytest.mark.parametrize("key,value", [("extensions.worktreeConfig", "true"),
                                       ("include.path", "/tmp/elsewhere"),
                                       ("includeIf.gitdir:/x/.path", "/tmp/y")])
def test_open_clone_refuses_a_config_that_would_read_an_agent_file(repo, clone, key, value):
    assert iso.open_clone(repo["root"], RUN) is not None           # positive first
    _g(clone.path, "config", "--file", ".git/config", key, value)
    with pytest.raises(IsolationError) as exc:
        iso.open_clone(repo["root"], RUN)
    assert exc.value.reason == "ISOLATION-CONFIG"


def test_open_clone_refuses_a_registered_worktree_or_a_linked_registry(repo, clone, tmp_path):
    reg = os.path.join(clone.path, ".git", "worktrees")
    os.makedirs(os.path.join(reg, "planted"))
    with pytest.raises(IsolationError) as exc:
        iso.open_clone(repo["root"], RUN)
    assert exc.value.reason == "ISOLATION-REGISTRY"
    os.rmdir(os.path.join(reg, "planted"))
    os.rmdir(reg)
    os.symlink(str(tmp_path), reg)
    with pytest.raises(IsolationError) as exc:
        iso.open_clone(repo["root"], RUN)
    assert exc.value.reason == "ISOLATION-REGISTRY"


def test_the_clone_proof_refuses_a_fix_term_in_the_base(repo):
    # negative control of the barrier lane: a fix term the base content carries refuses the build
    contract = _contract(repo["base"])
    with pytest.raises(IsolationError) as exc:
        iso.build_repair_repo(repo["root"], RUN, contract, repair_task="t-50",
                              base_commit=repo["fix"], fix_terms=(repo["fix"], FIX_TEXT[-20:]),
                              stamp=STAMP)
    assert exc.value.reason == "ISOLATION-BARRIER"
    assert not os.path.lexists(paths.clone_dir(repo["root"], RUN, "t-50"))
    ok = iso.build_repair_repo(repo["root"], RUN, contract, repair_task="t-50",
                               base_commit=repo["base"], fix_terms=(repo["fix"], FIX_TEXT[-20:]),
                               stamp=STAMP)
    assert ok.base_commit == ok.head


# ---------------------------------------------------------------- git_env

def test_git_env_pins_the_engine_folders_and_drops_inherited_git_variables(clone, repo,
                                                                          monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/planted")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    wt = iso.create_worktree(repo["root"], RUN, "t-1", clone, commit=clone.base_commit)
    env = iso.git_env(clone, worktree=wt)
    assert env["GIT_DIR"] == wt.host_git and env["GIT_WORK_TREE"] == wt.path
    assert env["GIT_COMMON_DIR"] == os.path.join(clone.path, ".git")
    assert env["GIT_INDEX_FILE"] == wt.index and wt.index.startswith(wt.host_git)
    assert env["GIT_OBJECT_DIRECTORY"] == os.path.join(wt.host_git, "objects")
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null" and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert "GIT_CONFIG_COUNT" not in env
    assert wt.admin_dir not in env.values() and wt.private_objects not in env.values()
    assert iso.git_env(clone)["GIT_DIR"] == os.path.join(clone.path, ".git")
    assert iso.git_env(clone, worktree=wt, index="/x/i")["GIT_INDEX_FILE"] == "/x/i"
    with pytest.raises(IsolationError):
        iso.git_env(clone, worktree=dataclasses.replace(wt, index=None))
    assert "diff.ignoreSubmodules=all" in iso.GIT_HARDEN
    assert "commit.gpgsign=false" in iso.GIT_HARDEN


# ---------------------------------------------------------------- done bar 1: purge

def test_a_planted_ignored_build_output_is_deleted_before_the_eval(repo, clone, tmp_path):
    wt = iso.create_worktree(repo["root"], RUN, "t-2", clone, commit=clone.base_commit)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keepme.txt").write_text("host file\n")
    _write(os.path.join(wt.path, "src/app.py"), "def f():\n    return 3\n")   # the tracked edit
    _write(os.path.join(wt.path, "build/cache.bin"), "planted answer\n")      # planted output
    _write(os.path.join(wt.path, "run.log"), "planted log\n")
    _write(os.path.join(wt.path, "out/keep.txt"), "kept by the keep list\n")
    os.symlink(str(outside), os.path.join(wt.path, "build", "outside"))     # an ignored link
    # the agent force-adds one output to its own admin index and marks it skip-worktree
    _agent(wt, clone, "add", "-f", "build/cache.bin")
    cap = iso.capture(clone, wt, forked_from=clone.base_commit, message="attempt t-2",
                      stamp=STAMP, out_dir=str(tmp_path / "ev"))
    assert cap.paths == ("src/app.py",)                   # the planted outputs are not captured
    res = iso.purge_ignored(clone, wt, head=cap.head, keep=("out/",))
    assert res.phase == "pre-eval" and res.ok
    # the files git lists as ignored, then the folder they leave that holds no tracked path
    assert set(res.removed) == {"build/cache.bin", "run.log", "build/outside", "build"}
    assert res.kept == ("out/keep.txt",)
    assert not os.path.lexists(os.path.join(wt.path, "build/cache.bin"))
    assert not os.path.lexists(os.path.join(wt.path, "build"))
    assert not os.path.lexists(os.path.join(wt.path, "build", "outside"))
    assert (outside / "keepme.txt").read_text() == "host file\n"   # the link was not followed
    assert os.path.isfile(os.path.join(wt.path, "out/keep.txt"))
    with open(os.path.join(wt.path, "src/app.py")) as fh:
        assert "return 3" in fh.read()                             # tracked work untouched
    # the fail case: an eval stand-in that reads the planted answer finds nothing to read
    assert _eval_reads(wt.path, "build/cache.bin") is None
    # negative: without the purge the eval would have read it
    _write(os.path.join(wt.path, "build/cache.bin"), "planted answer\n")
    assert _eval_reads(wt.path, "build/cache.bin") == "planted answer\n"


def _eval_reads(tree, rel):
    try:
        with open(os.path.join(tree, rel)) as fh:
            return fh.read()
    except OSError:
        return None


def test_post_eval_flags_new_ignored_changed_and_untracked_paths(repo, clone, tmp_path):
    wt = iso.create_worktree(repo["root"], RUN, "t-3", clone, commit=clone.base_commit)
    _write(os.path.join(wt.path, "src/app.py"), "def f():\n    return 4\n")
    cap = iso.capture(clone, wt, forked_from=clone.base_commit, message="m", stamp=STAMP,
                      out_dir=str(tmp_path / "ev"))
    pre = iso.purge_ignored(clone, wt, head=cap.head, keep=("out/",))
    clean = iso.post_eval(clone, wt, head=cap.head, keep=("out/",), eval_outputs=("build/",),
                          before=pre.kept)
    assert clean.ok and clean.phase == "post-eval" and not clean.new and not clean.changed
    _write(os.path.join(wt.path, "build/report.json"), "{}\n")      # a declared eval output
    assert iso.post_eval(clone, wt, head=cap.head, keep=("out/",), eval_outputs=("build/",),
                         before=pre.kept).ok
    _write(os.path.join(wt.path, "run.log"), "eval wrote this\n")   # undeclared ignored output
    res = iso.post_eval(clone, wt, head=cap.head, keep=("out/",), eval_outputs=("build/",),
                        before=pre.kept)
    assert not res.ok and res.new == ("run.log",)
    os.unlink(os.path.join(wt.path, "run.log"))
    _write(os.path.join(wt.path, "src/app.py"), "eval rewrote the attempt\n")
    _write(os.path.join(wt.path, "src/new.py"), "x = 1\n")
    res = iso.post_eval(clone, wt, head=cap.head, keep=("out/",), eval_outputs=("build/",),
                        before=pre.kept)
    assert not res.ok and res.changed == ("src/app.py", "src/new.py")


def test_an_agent_commit_hides_nothing_from_post_eval(repo, clone, tmp_path):
    # the compare names the captured head, never the admin HEAD an agent commit moves
    wt = iso.create_worktree(repo["root"], RUN, "t-4", clone, commit=clone.base_commit)
    cap = iso.capture(clone, wt, forked_from=clone.base_commit, message="m", stamp=STAMP,
                      out_dir=str(tmp_path / "ev"))
    assert cap.empty and cap.bytes == 0
    assert cap.sha256 == hashlib.sha256(b"").hexdigest() and cap.head == clone.base_commit
    assert os.path.getsize(cap.path) == 0
    _write(os.path.join(wt.path, "src/app.py"), "committed by the eval\n")
    _agent(wt, clone, "commit", "-qam", "hide it")
    assert _agent(wt, clone, "status", "--porcelain") == ""           # git status is fooled
    res = iso.post_eval(clone, wt, head=cap.head, keep=(), eval_outputs=(), before=())
    assert not res.ok and res.changed == ("src/app.py",)             # the engine is not


# ---------------------------------------------------------------- planted git state

def test_planted_git_state_runs_no_program_and_capture_records_the_tree_bytes(repo, clone,
                                                                              tmp_path):
    wt = iso.create_worktree(repo["root"], RUN, "t-6", clone, commit=clone.base_commit)
    prog, mark = _marker_program(tmp_path)
    _write(os.path.join(wt.path, "src/app.py"), "def f():\n    return 5\n")
    _agent(wt, clone, "commit", "-qam", "agent commit")
    _write(os.path.join(wt.path, "bench/runbook.yaml"), "edited behind a skip-worktree mark\n")
    _agent(wt, clone, "update-index", "--skip-worktree", "bench/runbook.yaml")
    _write(os.path.join(wt.path, "build/cache.bin"), "planted\n")
    _agent(wt, clone, "add", "-f", "build/cache.bin")
    _write(os.path.join(wt.path, ".gitattributes"), "*.py filter=x diff=x\n")
    hostile = ("[core]\n\tfsmonitor = %s\n[filter \"x\"]\n\tclean = %s\n\tsmudge = %s\n"
               "[diff \"x\"]\n\ttextconv = %s\n" % (prog, prog, prog, prog))
    for name in ("config", "config.worktree"):
        _write(os.path.join(wt.admin_dir, name), hostile)
    _write(os.path.join(wt.path, "src/app.py"), "def f():\n    return 6\n")
    real = _g(wt.path, "hash-object", "src/app.py", env=_env())
    loose = os.path.join(wt.private_objects, real[:2], real[2:])
    os.makedirs(os.path.dirname(loose), exist_ok=True)
    import zlib
    body = b"FORGED\n"
    with open(loose, "wb") as fh:
        fh.write(zlib.compress(b"blob %d\0" % len(body) + body))
    with open(os.path.join(wt.admin_dir, "HEAD"), "w") as fh:
        fh.write("garbage\n")                                        # a corrupted admin HEAD
    with open(os.path.join(wt.admin_dir, "commondir"), "w") as fh:
        fh.write(str(tmp_path / "nowhere") + "\n")                   # a hostile commondir
    cap = iso.capture(clone, wt, forked_from=clone.base_commit, message="m", stamp=STAMP,
                      out_dir=str(tmp_path / "ev"))
    assert set(cap.paths) == {".gitattributes", "bench/runbook.yaml", "src/app.py"}
    assert _g(clone.path, "cat-file", "blob", "%s:src/app.py" % cap.head,
              env=_env(GIT_OBJECT_DIRECTORY=os.path.join(wt.host_git, "objects"),
                       GIT_ALTERNATE_OBJECT_DIRECTORIES=os.path.join(clone.path, ".git",
                                                                     "objects"))) \
        == "def f():\n    return 6"
    with open(cap.path, "rb") as fh:
        diff = fh.read()
    assert b"+    return 6" in diff and b"FORGED" not in diff
    res = iso.purge_ignored(clone, wt, head=cap.head, keep=())
    assert "build/cache.bin" in res.removed                          # the force-added output
    assert iso.post_eval(clone, wt, head=cap.head, keep=(), eval_outputs=(), before=()).ok
    assert not os.path.exists(mark)                                  # no planted program ran
    # negative: the admin folder as GIT_DIR (the route the design drops) fails on its HEAD
    bad = subprocess.run(["git", "status"], cwd=wt.path, env=_env(GIT_DIR=wt.admin_dir),
                         capture_output=True, timeout=30)
    assert bad.returncode != 0


def test_nested_repositories_are_listed_flagged_and_run_nothing(repo, clone, tmp_path,
                                                               monkeypatch):
    wt = iso.create_worktree(repo["root"], RUN, "t-7", clone, commit=clone.base_commit)
    assert iso.nested_git(wt) == ()
    prog, mark = _marker_program(tmp_path)
    sub = os.path.join(wt.path, "sub")
    os.makedirs(sub)
    _g(sub, "init", "-q", "-b", "main")
    _write(os.path.join(sub, ".gitattributes"), "x filter=evil\n")
    _write(os.path.join(sub, "x"), "data\n")
    _g(sub, "add", ".gitattributes", "x")
    _g(sub, "commit", "-qm", "sub")
    _g(sub, "config", "filter.evil.clean", prog)
    os.utime(os.path.join(sub, "x"), (978307200, 978307200))        # stat no longer matches
    assert iso.nested_git(wt) == ("sub/.git",)
    _write(os.path.join(wt.path, "src/app.py"), "def f():\n    return 7\n")
    cap = iso.capture(clone, wt, forked_from=clone.base_commit, message="m", stamp=STAMP,
                      out_dir=str(tmp_path / "ev"))
    # the nested repository is listed (check_diff marks the attempt broken on it) but never
    # staged: git reads neither its gitdir nor its HEAD, so no SHA of it reaches the diff, and
    # the rest of the tree is still captured (check-adversarial-r3 B1 and m1)
    assert cap.gitlinks == ("sub",) and cap.paths == ("src/app.py",)
    with open(cap.path, "rb") as fh:
        diff = fh.read()
    assert b"160000" not in diff and b"Subproject" not in diff and b"+    return 7" in diff
    assert not os.path.exists(mark)
    # post_eval runs no git at all on a tree that holds a nested repository
    calls = []
    monkeypatch.setattr(iso, "_git", lambda *a, **k: calls.append(a) or b"")
    res = iso.post_eval(clone, wt, head=cap.head, keep=(), eval_outputs=(), before=())
    assert not res.ok and res.nested == ("sub/.git",) and calls == []
    monkeypatch.undo()
    # a `.git` symlink is flagged without being followed
    import shutil
    shutil.rmtree(os.path.join(sub, ".git"))
    os.symlink(str(tmp_path), os.path.join(sub, ".git"))
    os.symlink(str(tmp_path), os.path.join(wt.path, "linkdir"))
    assert iso.nested_git(wt) == ("sub/.git",)
    assert not os.path.exists(mark)


# ---------------------------------------------------------------- done bar 2: concurrency

def test_two_concurrent_attempts_never_share_a_worktree(repo, clone):
    made, errors = {}, []

    def cut(task):
        try:
            made[task] = iso.create_worktree(repo["root"], RUN, task, clone,
                                             commit=clone.base_commit)
        except Exception as exc:              # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=cut, args=(t,)) for t in ("t-10", "t-11")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    a, b = made["t-10"], made["t-11"]
    for field in ("path", "admin_dir", "host_git", "index", "private_objects"):
        assert getattr(a, field) != getattr(b, field), field
        assert not getattr(a, field).startswith(getattr(b, field) + os.sep)
    for wt in (a, b):
        with open(os.path.join(wt.path, ".git")) as fh:
            assert fh.read().strip() == "gitdir: %s" % wt.admin_dir
        assert _tree_files(wt.path) == _tree_files(a.path)
    assert os.listdir(os.path.join(clone.path, ".git", "worktrees")) == []
    # an edit and a commit in A never show in B, and neither writes the clone
    refs = _g(clone.path, "for-each-ref")
    _write(os.path.join(a.path, "src/app.py"), "only in A\n")
    _agent(a, clone, "commit", "-qam", "A's work")
    with open(os.path.join(b.path, "src/app.py")) as fh:
        assert "only in A" not in fh.read()
    assert _agent(b, clone, "log", "--all", "--oneline").count("\n") == 0
    assert _g(clone.path, "for-each-ref") == refs


def test_a_planted_gitdir_naming_another_attempt_changes_nothing(repo, clone, tmp_path):
    a = iso.create_worktree(repo["root"], RUN, "t-12", clone, commit=clone.base_commit)
    b_path = paths.worktree_dir(repo["root"], RUN, "t-13")
    b_base = os.path.join(paths.attempt_dir(repo["root"], RUN, "t-13"), "base")

    def plant(target):
        with open(os.path.join(a.admin_dir, "gitdir"), "w") as fh:
            fh.write(os.path.join(target, ".git") + "\n")
        with open(os.path.join(a.admin_dir, "locked"), "w") as fh:
            fh.write("x\n")

    # each plant names one of B's paths and is followed by the cut of that path
    plant(b_path)
    b = iso.create_worktree(repo["root"], RUN, "t-13", clone, commit=clone.base_commit)
    plant(b_base)
    base = iso.fresh_tree(repo["root"], clone, commit=clone.base_commit, path=b_base)
    champ = iso.champion_checkout(repo["root"], RUN, 1, clone, commit=clone.base_commit)
    assert os.path.isfile(os.path.join(b.path, "src/app.py"))
    assert os.path.isfile(os.path.join(base.path, "src/app.py"))
    assert os.path.isfile(os.path.join(champ.path, "src/app.py"))
    assert os.listdir(os.path.join(clone.path, ".git", "worktrees")) == []
    _write(os.path.join(b.path, "src/app.py"), "B's change\n")
    cap = iso.capture(clone, b, forked_from=clone.base_commit, message="b", stamp=STAMP,
                      out_dir=str(tmp_path / "evb"))
    iso.destroy_worktree(clone, b)
    again = iso.rebuild(repo["root"], RUN, "t-13", clone, recorded=cap, message="b", stamp=STAMP)
    with open(os.path.join(again.path, "src/app.py")) as fh:
        assert fh.read() == "B's change\n"
    assert iso.open_clone(repo["root"], RUN) is not None
    # negative: `git worktree add` (the route the design drops) is blocked by such a registry
    reg = os.path.join(clone.path, ".git", "worktrees", "x")
    os.makedirs(reg)
    with open(os.path.join(reg, "gitdir"), "w") as fh:
        fh.write(os.path.join(tmp_path, "y", ".git") + "\n")
    with open(os.path.join(reg, "locked"), "w") as fh:
        fh.write("x\n")
    res = subprocess.run(["git", "-C", clone.path, "worktree", "add", "--detach",
                          str(tmp_path / "y"), clone.base_commit], env=_env(),
                         capture_output=True)
    assert res.returncode != 0
    with pytest.raises(IsolationError) as exc:
        iso.open_clone(repo["root"], RUN)
    assert exc.value.reason == "ISOLATION-REGISTRY"


def test_create_refuses_a_link_at_its_path_and_a_containment_finding(repo, clone, tmp_path):
    path = paths.worktree_dir(repo["root"], RUN, "t-14")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.symlink(str(tmp_path), path)
    with pytest.raises(IsolationError) as exc:
        iso.create_worktree(repo["root"], RUN, "t-14", clone, commit=clone.base_commit)
    assert exc.value.reason == "ISOLATION-PATH-TAKEN"
    assert os.path.islink(path)
    os.unlink(path)
    _g(clone.path, "config", "--file", ".git/config", "core.hooksPath", "/planted/hooks")
    with pytest.raises(IsolationError) as exc:
        iso.create_worktree(repo["root"], RUN, "t-14", clone, commit=clone.base_commit)
    assert exc.value.reason == "ISOLATION-CONTAINMENT"
    assert not os.path.lexists(path)
    _g(clone.path, "config", "--file", ".git/config", "--unset", "core.hooksPath")
    assert iso.create_worktree(repo["root"], RUN, "t-14", clone, commit=clone.base_commit)


# ---------------------------------------------------------------- done bar 3: reclaim

def _recorded(event):
    """The WorktreeInfo the lifecycle builds from a research-worktree create event."""
    return WorktreeInfo(path=event["path"], label=event["task"], commit=event["commit"],
                        admin_dir=event["admin_dir"], private_objects=event["private_objects"],
                        index=event["index"], host_git=event["host_git"])


def test_a_crashed_attempts_worktree_is_reclaimed_on_resume(repo, clone, tmp_path):
    wt = iso.create_worktree(repo["root"], RUN, "t-20", clone, commit=clone.base_commit)
    event = dict(dataclasses.asdict(wt), task="t-20")                 # what the record holds
    _write(os.path.join(wt.path, "src/app.py"), "half-done work of a killed implementer\n")
    _agent(wt, clone, "commit", "-qam", "partial")
    outside = tmp_path / "host-data"
    outside.mkdir()
    (outside / "f.txt").write_text("keep\n")
    os.symlink(str(outside), os.path.join(wt.path, "link"))
    os.chmod(os.path.join(wt.path, "src"), 0o500)                   # a folder made read-only
    with open(os.path.join(wt.path, ".git"), "w") as fh:            # a hostile .git file
        fh.write("gitdir: %s\n" % outside)
    # the runner died here; on resume the lifecycle reclaims from the recorded paths
    assert iso.reclaim(repo["root"], RUN, "t-20", clone, recorded=_recorded(event)) is True
    for p in (wt.path, wt.admin_dir, wt.host_git):
        assert not os.path.lexists(p)
    assert os.path.isdir(wt.private_objects)                        # evidence stays
    assert (outside / "f.txt").read_text() == "keep\n"               # never followed
    assert iso.reclaim(repo["root"], RUN, "t-20", clone, recorded=_recorded(event)) is False
    fresh = iso.create_worktree(repo["root"], RUN, "t-20", clone, commit=clone.base_commit)
    with open(os.path.join(fresh.path, "src/app.py")) as fh:
        assert "half-done" not in fh.read()


def test_reclaim_refuses_recorded_folders_the_engine_did_not_make(repo, clone):
    a = iso.create_worktree(repo["root"], RUN, "t-21", clone, commit=clone.base_commit)
    b = iso.create_worktree(repo["root"], RUN, "t-22", clone, commit=clone.base_commit)
    forged = dataclasses.replace(a, admin_dir=b.admin_dir)
    with pytest.raises(IsolationError):
        iso.reclaim(repo["root"], RUN, "t-21", clone, recorded=forged)
    with pytest.raises(IsolationError):
        iso.reclaim(repo["root"], RUN, "t-21", clone, recorded=b)     # another attempt's path
    assert os.path.isdir(b.admin_dir) and os.path.isdir(b.path) and os.path.isdir(a.path)


def test_a_leftover_of_a_killed_create_is_cut_again(repo, clone):
    wt = iso.create_worktree(repo["root"], RUN, "t-23", clone, commit=clone.base_commit)
    _write(os.path.join(wt.path, "stray.txt"), "left by a killed create\n")
    again = iso.create_worktree(repo["root"], RUN, "t-23", clone, commit=clone.base_commit)
    assert again == wt and not os.path.exists(os.path.join(wt.path, "stray.txt"))


# ---------------------------------------------------------------- done bars 4 and 5: repair

def test_a_repair_worktree_holds_no_commit_after_its_base(repo, tmp_path):
    root, fix = repo["root"], repo["fix"]
    # negative control: in the source the search finds the fix, so the search can find it
    assert _g(root, "cat-file", "-t", fix) == "commit"
    assert FIX_TEXT in _g(root, "log", "--all", "-p")
    rc = iso.build_repair_repo(root, RUN, _contract(repo["base"]), repair_task="t-30",
                               base_commit=repo["base"], fix_terms=(fix, "src/fix_only_helper.py"),
                               stamp=STAMP)
    wt = iso.create_worktree(root, RUN, "t-31", rc, commit=rc.base_commit)
    # the search, from inside the worktree, as the implementer's git sees it
    assert _agent(wt, rc, "cat-file", "-t", fix, check=False) == ""
    assert _agent(wt, rc, "rev-list", "--all").splitlines() == [rc.base_commit]
    assert FIX_TEXT not in _agent(wt, rc, "log", "--all", "-p")
    every = _agent(wt, rc, "cat-file", "--batch-all-objects", "--batch")
    assert FIX_TEXT not in every and "fix_only_helper" not in every and fix not in every
    assert _agent(wt, rc, "tag", "-l") == "" and _agent(wt, rc, "remote") == ""
    assert "Fix BUG-4711" not in _agent(wt, rc, "log", "--all", "--format=%s")
    assert "fix_only_helper" not in "\n".join(_tree_files(wt.path))
    with open(os.path.join(wt.path, "src/app.py")) as fh:
        assert "return 1" in fh.read()


def test_repair_paths_and_branches_carry_no_bug_or_fix_identifier(repo):
    root, fix = repo["root"], repo["fix"]
    rc = iso.build_repair_repo(root, RUN, _contract(repo["base"]), repair_task="t-32",
                               base_commit=repo["base"], fix_terms=(fix,), stamp=STAMP)
    wt = iso.create_worktree(root, RUN, "t-33", rc, commit=rc.base_commit)
    names = [rc.path, rc.branch, wt.path, wt.admin_dir, wt.host_git, wt.private_objects,
             wt.index, _g(rc.path, "for-each-ref", "--format=%(refname)")]
    for text in names:
        rel = text.replace(root, "")
        for ident in (fix, fix[:7], "BUG-4711", "4711", "fix"):
            assert ident.lower() not in rel.lower(), (ident, rel)
    assert rc.branch == "research/%s/t-32" % RUN
    # negative: a line that names the bug is refused before anything is made
    for bad in ("fix-bug-4711", "BUG-4711", "t-32-fix", fix[:7]):
        with pytest.raises(ValueError):
            iso.build_repair_repo(root, RUN, _contract(repo["base"]), repair_task=bad,
                                  base_commit=repo["base"], fix_terms=(), stamp=STAMP)
    assert sorted(os.listdir(os.path.dirname(rc.path))) == ["t-32", "t-32.barrier.json"]
    # and an attempt worktree is named by a task id only, whatever the caller passes
    # (check-contract-r1-resume minor 2)
    for bad in ("fix-bug-4711", "t-33-fix", "bug-4711"):
        with pytest.raises(ValueError):
            iso.create_worktree(root, RUN, bad, rc, commit=rc.base_commit)
        assert not os.path.lexists(paths.worktree_dir(root, RUN, bad))


def test_commit_prepared_commits_the_stage_output_minus_ignored_and_proves_it(repo, tmp_path):
    root = repo["root"]
    rc = iso.build_repair_repo(root, RUN, _contract(repo["base"]), repair_task="t-34",
                               base_commit=repo["base"], fix_terms=(repo["fix"],), stamp=STAMP)
    tree = iso.fresh_tree(root, rc, commit=rc.base_commit, path=str(tmp_path / "prep"))
    assert not os.path.lexists(os.path.join(tree.path, ".git"))       # admin False: no .git
    _write(os.path.join(tree.path, "src/generated.py"), "GEN = 1\n")
    _write(os.path.join(tree.path, "build/residue.o"), "ignored\n")
    cap = iso.commit_prepared(rc, tree, parent=rc.base_commit, message="prepare", stamp=STAMP,
                              out_dir=str(tmp_path / "ev"))
    assert cap.paths == ("src/generated.py",) and os.path.basename(cap.path) == "prepare.diff"
    assert _g(rc.path, "rev-parse", rc.branch) == cap.head
    assert _g(rc.path, "rev-parse", "main") == rc.base_commit
    opened = iso.open_clone(root, RUN, line="t-34")
    assert opened.base_commit == rc.base_commit and opened.head == cap.head
    # negative: a prepare output that carries a fix term is refused and the branch stays
    tree2 = iso.fresh_tree(root, rc, commit=cap.head, path=str(tmp_path / "prep2"))
    _write(os.path.join(tree2.path, "src/leak.txt"), "see %s\n" % repo["fix"])
    with pytest.raises(IsolationError) as exc:
        iso.commit_prepared(rc, tree2, parent=cap.head, message="prepare", stamp=STAMP,
                            out_dir=str(tmp_path / "ev2"))
    assert exc.value.reason == "ISOLATION-BARRIER"
    assert _g(rc.path, "rev-parse", rc.branch) == cap.head
    # an unchanged tree: an empty capture and no commit
    tree3 = iso.fresh_tree(root, rc, commit=cap.head, path=str(tmp_path / "prep3"))
    empty = iso.commit_prepared(rc, tree3, parent=cap.head, message="prepare", stamp=STAMP,
                                out_dir=str(tmp_path / "ev3"))
    assert empty.empty and empty.head == cap.head


# ---------------------------------------------------------------- done bar 6: signing off

def test_git_in_attempt_worktrees_runs_with_signing_off(repo, clone, tmp_path, monkeypatch):
    prog, mark = _marker_program(tmp_path, "sign.sh")
    hostile = str(tmp_path / "operator.gitconfig")
    _write(hostile, "[commit]\n\tgpgsign = true\n[tag]\n\tgpgsign = true\n"
                    "\tforceSignAnnotated = true\n[gpg]\n\tprogram = %s\n" % prog)
    # negative control: a plain repository under that config does call the signing program
    plain = str(tmp_path / "plain")
    os.makedirs(plain)
    _g(plain, "init", "-q")
    _write(os.path.join(plain, "a"), "a\n")
    _g(plain, "add", "a")
    _g(plain, "commit", "-qm", "x", env=_env(GIT_CONFIG_GLOBAL=hostile), check=False)
    assert os.path.exists(mark)
    os.unlink(mark)
    # and an annotated tag there calls it too, with tag.gpgsign off in the repository config,
    # through tag.forceSignAnnotated alone (check-adversarial-r3 m2)
    _g(plain, "config", "tag.gpgsign", "false")
    _g(plain, "commit", "-qm", "x", "--no-gpg-sign", env=_env(GIT_CONFIG_GLOBAL=hostile))
    _g(plain, "tag", "-m", "t", "v9", env=_env(GIT_CONFIG_GLOBAL=hostile), check=False)
    assert os.path.exists(mark)
    os.unlink(mark)
    # the engine's own git makes commits only with `git commit-tree`, which does not read
    # commit.gpgsign, so this part shows the engine's capture and merge still run and write no
    # signature under the operator's signing config; it does not exercise the GIT_HARDEN pin,
    # which is a last layer only (its presence is asserted in the git_env test above)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", hostile)
    wt = iso.create_worktree(repo["root"], RUN, "t-40", clone, commit=clone.base_commit)
    _write(os.path.join(wt.path, "src/app.py"), "signed? no\n")
    started = time.monotonic()
    cap = iso.capture(clone, wt, forked_from=clone.base_commit, message="m", stamp=STAMP,
                      out_dir=str(tmp_path / "ev"))
    iso.merge_champion(clone, branch=clone.branch, parent=clone.base_commit, capture=cap,
                       message="m", stamp=STAMP)
    assert "gpgsig" not in _g(clone.path, "cat-file", "commit", cap.head)
    # the agent's git in the worktree reads the operator's signing config as its global one
    extra = dict(GIT_CONFIG_GLOBAL=hostile, GIT_OBJECT_DIRECTORY=wt.private_objects,
                 GIT_ALTERNATE_OBJECT_DIRECTORIES=os.path.join(clone.path, ".git", "objects"))
    res = subprocess.run(["git", "commit", "-qam", "agent"], cwd=wt.path, env=_env(**extra),
                         capture_output=True, timeout=30)
    assert res.returncode == 0, res.stderr
    assert "gpgsig" not in _g(wt.path, "cat-file", "commit", "HEAD", env=_env(**extra))
    for tag in (["tag", "-m", "t", "v9"], ["tag", "-a", "-m", "t", "v10"]):
        res = subprocess.run(["git", *tag], cwd=wt.path, env=_env(**extra), capture_output=True,
                             timeout=30)
        assert res.returncode == 0, res.stderr
    assert not os.path.exists(mark) and time.monotonic() - started < 30


# ---------------------------------------------------------------- capture, merge, rebuild

def test_merge_reproduces_the_recorded_head_and_is_idempotent(repo, clone, tmp_path):
    wt = iso.create_worktree(repo["root"], RUN, "t-50", clone, commit=clone.base_commit)
    _write(os.path.join(wt.path, "src/app.py"), "def f():\n    return 50\n")
    with open(os.path.join(wt.path, "src/blob.bin"), "wb") as fh:
        fh.write(bytes(range(256)))
    cap = iso.capture(clone, wt, forked_from=clone.base_commit, message="attempt t-50",
                      stamp=STAMP, out_dir=str(tmp_path / "ev"))
    assert set(cap.paths) == {"src/app.py", "src/blob.bin"}
    with open(cap.path, "rb") as fh:
        assert hashlib.sha256(fh.read()).hexdigest() == cap.sha256
    assert _g(clone.path, "cat-file", "-t", cap.head, check=False) == ""   # not in the clone yet
    head = iso.merge_champion(clone, branch=clone.branch, parent=clone.base_commit,
                              capture=cap, message="attempt t-50", stamp=STAMP)
    assert head == cap.head == _g(clone.path, "rev-parse", clone.branch)
    assert _g(clone.path, "rev-parse", "main") == clone.base_commit
    assert iso.merge_champion(clone, branch=clone.branch, parent=clone.base_commit,
                              capture=cap, message="attempt t-50", stamp=STAMP) == head
    # negative: a changed diff file, or another message, does not give the recorded head
    other = dataclasses.replace(cap, head="0" * 40)
    with pytest.raises(IsolationError) as exc:
        iso.merge_champion(clone, branch="research/other", parent=clone.base_commit,
                           capture=other, message="attempt t-50", stamp=STAMP)
    assert exc.value.reason == "ISOLATION-REBUILD-MISMATCH"
    with open(cap.path, "ab") as fh:
        fh.write(b"\n")
    with pytest.raises(IsolationError) as exc:
        iso.merge_champion(clone, branch="research/other", parent=clone.base_commit,
                           capture=cap, message="attempt t-50", stamp=STAMP)
    assert exc.value.reason == "ISOLATION-REBUILD-MISMATCH"


def test_rebuild_gives_the_recorded_head_or_refuses(repo, clone, tmp_path):
    wt = iso.create_worktree(repo["root"], RUN, "t-51", clone, commit=clone.base_commit)
    _write(os.path.join(wt.path, "src/app.py"), "def f():\n    return 51\n")
    os.unlink(os.path.join(wt.path, "src/run.sh"))
    cap = iso.capture(clone, wt, forked_from=clone.base_commit, message="attempt t-51",
                      stamp=STAMP, out_dir=str(tmp_path / "ev"))
    iso.destroy_worktree(clone, wt)
    assert not os.path.lexists(wt.path)
    again = iso.rebuild(repo["root"], RUN, "t-51", clone, recorded=cap, message="attempt t-51",
                        stamp=STAMP)
    assert not os.path.exists(os.path.join(again.path, "src/run.sh"))
    iso.destroy_worktree(clone, again)
    with pytest.raises(IsolationError) as exc:
        iso.rebuild(repo["root"], RUN, "t-51", clone, recorded=cap, message="another message",
                    stamp=STAMP)
    assert exc.value.reason == "ISOLATION-REBUILD-MISMATCH"
    empty = DiffCapture(forked_from=clone.base_commit, head=clone.base_commit, tree="",
                        path=str(tmp_path / "empty.diff"), sha256=hashlib.sha256(b"").hexdigest(),
                        bytes=0, paths=(), stamp=STAMP, message="m", empty=True)
    open(empty.path, "wb").close()
    iso.destroy_worktree(clone, again)
    assert iso.rebuild(repo["root"], RUN, "t-51", clone, recorded=empty, message="m",
                       stamp=STAMP).commit == clone.base_commit


# ---------------------------------------------------------------- trees

def test_champion_checkout_is_reused_when_clean_and_cut_again_when_not(repo, clone):
    first = iso.champion_checkout(repo["root"], RUN, 2, clone, commit=clone.base_commit)
    assert first.read_only and first.label == "r2-champion"
    assert first.admin_dir == first.path + ".git-admin"
    assert first == iso.tree_info(first.path, commit=clone.base_commit, label="r2-champion",
                                  admin=True, read_only=True)
    marker = os.stat(os.path.join(first.path, "src/app.py")).st_ino
    again = iso.champion_checkout(repo["root"], RUN, 2, clone, commit=clone.base_commit)
    assert again == first and os.stat(os.path.join(first.path, "src/app.py")).st_ino == marker
    _write(os.path.join(first.path, "src/app.py"), "dirty\n")
    third = iso.champion_checkout(repo["root"], RUN, 2, clone, commit=clone.base_commit)
    with open(os.path.join(third.path, "src/app.py")) as fh:
        assert "return 1" in fh.read()
    # an ignored leftover, an empty folder or a changed root `.git` is not clean either
    for plant in ("build/stale.bin", "answers/42/", ".git"):
        target = os.path.join(third.path, plant)
        if plant == ".git":
            with open(target, "a") as fh:
                fh.write("ANSWER=42\n")
        elif plant.endswith("/"):
            os.makedirs(target)
        else:
            _write(target, "42\n")
        again = iso.champion_checkout(repo["root"], RUN, 2, clone, commit=clone.base_commit)
        assert again == first
        if plant == ".git":
            with open(target) as fh:
                assert fh.read() == "gitdir: %s\n" % first.admin_dir
        else:
            assert not os.path.lexists(target.rstrip("/")), plant
    # the proposer's git works read-only in it through its admin folder
    assert _g(third.path, "status", "--porcelain",
              env=_env(GIT_ALTERNATE_OBJECT_DIRECTORIES=os.path.join(clone.path, ".git",
                                                                     "objects"))) == ""


def test_fresh_tree_overlays_and_refusals(repo, clone, tmp_path):
    frozen = tmp_path / "frozen.yaml"
    frozen.write_text("frozen copy\n")
    tree = iso.fresh_tree(repo["root"], clone, commit=clone.base_commit,
                          path=str(tmp_path / "t" / "base"),
                          overlays=((str(frozen), "bench/runbook.yaml"),))
    with open(os.path.join(tree.path, "bench/runbook.yaml")) as fh:
        assert fh.read() == "frozen copy\n"
    assert tree.admin_dir is None and tree.host_git == tree.path + ".git-host"
    with pytest.raises(IsolationError) as exc:
        iso.fresh_tree(repo["root"], clone, commit=clone.base_commit, path=tree.path)
    assert exc.value.reason == "ISOLATION-PATH-TAKEN"
    with pytest.raises(IsolationError) as exc:
        iso.fresh_tree(repo["root"], clone, commit=clone.base_commit,
                       path=str(tmp_path / "t" / "other"),
                       overlays=((str(frozen), "../escape.yaml"),))
    assert exc.value.reason == "ISOLATION-CONTAINMENT"
    assert not os.path.exists(str(tmp_path / "t" / "escape.yaml"))
    assert not os.path.lexists(str(tmp_path / "t" / "other"))


def test_build_head_clone_for_a_verification(repo, tmp_path):
    path = os.path.join(repo["root"], ".alpaca", "verify", "t-60", "v1", "clone")
    hc = iso.build_head_clone(repo["root"], head=repo["fix"], path=path, stamp=STAMP)
    assert hc.line == "verify" and hc.branch == "main" and hc.base_commit == hc.head
    files = _g(hc.path, "ls-tree", "-r", "--name-only", hc.base_commit).splitlines()
    assert "src/fix_only_helper.py" in files and ".alpaca/state.json" not in files
    assert _g(hc.path, "rev-list", "--all", "--count") == "1"
    assert os.listdir(os.path.join(hc.path, ".git", "worktrees")) == []


# ---------------------------------------------------------------- sweep

def test_sweep_removes_only_sealed_old_attempts_of_dead_runs(repo, clone):
    root = repo["root"]
    done = iso.create_worktree(root, RUN, "t-70", clone, commit=clone.base_commit)
    open_ = iso.create_worktree(root, RUN, "t-71", clone, commit=clone.base_commit)
    homes = os.path.join(paths.run_dir(root, RUN), "homes")
    for name in ("t-70-implementer-t1", "t-71-implementer-t1", "vf-t-70-15890-v1-r0-t1"):
        os.makedirs(os.path.join(homes, name))
    old = time.time() - 7200
    # an attempt is aged by its attempt dir and its host git folder, which no jail mounts
    for wt in (done, open_):
        for p in (os.path.dirname(os.path.dirname(wt.host_git)), wt.host_git):
            os.utime(p, (old, old))
    # attempt code in a gate stage sets the tree's and the home's mtime 100 years ahead
    # (check-adversarial-resume-r1 m3): the sealed attempt of a dead run is still swept
    ahead = time.time() + 100 * 365 * 86400
    for p in (done.path, open_.path, *[os.path.join(homes, n) for n in os.listdir(homes)]):
        os.utime(p, (ahead, ahead))

    def sealed(_root, _run, task):
        return task == "t-70"

    dead = lambda _root, _run: False                                  # noqa: E731
    live = lambda _root, _run: True                                   # noqa: E731
    assert iso.sweep(root, None, sealed=sealed, dry_run=False, alive=live) == []
    plan = iso.sweep(root, None, sealed=sealed, alive=dead)
    assert os.path.exists(done.path)                                   # dry run by default
    assert set(plan) == {done.path, done.admin_dir, done.host_git,
                         os.path.join(homes, "t-70-implementer-t1"),
                         os.path.join(homes, "vf-t-70-15890-v1-r0-t1")}
    assert iso.sweep(root, RUN, sealed=sealed, dry_run=False, alive=dead, grace_s=10 ** 6) == []
    # negative: an engine write to the host git folder makes the attempt young again
    now = time.time()
    os.utime(done.host_git, (now, now))
    assert iso.sweep(root, RUN, sealed=sealed, alive=dead) == []
    os.utime(done.host_git, (old, old))
    removed = iso.sweep(root, RUN, sealed=sealed, dry_run=False, alive=dead)
    assert set(removed) == set(plan)
    assert not os.path.lexists(done.path) and os.path.isdir(done.private_objects)
    assert os.path.isdir(open_.path) and os.path.isdir(os.path.join(homes, "t-71-implementer-t1"))


def test_destroy_refuses_an_admin_folder_it_did_not_make(repo, clone, tmp_path):
    wt = iso.create_worktree(repo["root"], RUN, "t-80", clone, commit=clone.base_commit)
    victim = tmp_path / "victim"
    victim.mkdir()
    with pytest.raises(IsolationError):
        iso.destroy_worktree(clone, dataclasses.replace(wt, admin_dir=str(victim)))
    with pytest.raises(IsolationError):
        iso.destroy_worktree(clone, dataclasses.replace(wt, host_git=str(victim)))
    assert victim.is_dir() and os.path.isdir(wt.path)
    iso.destroy_worktree(clone, wt)
    assert not os.path.lexists(wt.path) and not os.path.lexists(wt.admin_dir)
    assert stat.S_ISDIR(os.lstat(wt.private_objects).st_mode)


# ---------------------------------------------------------------- the fail case: what git never lists

def _plant_root_dotgit(wt, how):
    dotgit = os.path.join(wt.path, ".git")
    if how == "folder":
        os.unlink(dotgit)
        _write(os.path.join(dotgit, "answer.txt"), "42\n")
    elif how == "extra-bytes":
        with open(dotgit, "a") as fh:
            fh.write("ANSWER=42\n")
    elif how == "same-length":
        with open(dotgit, "rb") as fh:
            body = fh.read()
        with open(dotgit, "wb") as fh:
            fh.write(body[:-3] + b"42\n")
    elif how == "symlink":
        os.unlink(dotgit)
        os.symlink(os.path.join(wt.admin_dir, "HEAD"), dotgit)
    else:
        raise AssertionError(how)


@pytest.mark.parametrize("how", ["folder", "extra-bytes", "same-length", "symlink"])
def test_a_planted_root_dotgit_is_put_back_before_the_eval_and_flagged_after(
        repo, clone, tmp_path, how):
    wt = iso.create_worktree(repo["root"], RUN, "t-90", clone, commit=clone.base_commit)
    engine = "gitdir: %s\n" % wt.admin_dir
    assert iso.nested_git(wt) == ()
    # the implementer plants data in the root `.git`, which git never lists
    _plant_root_dotgit(wt, how)
    _write(os.path.join(wt.path, "src/app.py"), "def f():\n    return 90\n")
    # the design's rule: nested_git lists `.git` entries below the root only (architecture.md
    # section 3.1, types.py PurgeResult.nested), so check_paths still runs its git listings
    assert iso.nested_git(wt) == () and not iso._root_dotgit_ok(wt)
    cap = iso.capture(clone, wt, forked_from=clone.base_commit, message="m", stamp=STAMP,
                      out_dir=str(tmp_path / "ev"))
    assert cap.paths == ("src/app.py",)                       # the plant is not in the diff
    iso.purge_ignored(clone, wt, head=cap.head, keep=())
    dotgit = os.path.join(wt.path, ".git")
    st = os.lstat(dotgit)
    assert stat.S_ISREG(st.st_mode) and st.st_nlink == 1
    with open(dotgit) as fh:
        assert fh.read() == engine                             # the engine's bytes, and only them
    assert _eval_reads(wt.path, ".git/answer.txt") is None     # the eval finds nothing planted
    assert os.path.isfile(os.path.join(wt.admin_dir, "HEAD"))  # a link target was not touched
    assert iso._root_dotgit_ok(wt)
    assert iso.post_eval(clone, wt, head=cap.head, keep=(), eval_outputs=(), before=()).ok
    # attempt code in a gate stage plants it again during the eval: post_eval lists the root
    # `.git` in changed, and its git compare, whose GIT_DIR is pinned, still runs
    _plant_root_dotgit(wt, how)
    res = iso.post_eval(clone, wt, head=cap.head, keep=(), eval_outputs=(), before=())
    assert not res.ok and res.changed == (".git",) and res.nested == ()
    # and it still sees an edit beside the plant
    _write(os.path.join(wt.path, "src/app.py"), "eval rewrote it\n")
    res = iso.post_eval(clone, wt, head=cap.head, keep=(), eval_outputs=(), before=())
    assert not res.ok and res.changed == (".git", "src/app.py")


def test_a_tree_without_an_admin_folder_flags_any_root_dotgit(repo, clone, tmp_path):
    tree = iso.fresh_tree(repo["root"], clone, commit=clone.base_commit,
                          path=str(tmp_path / "base"))
    assert iso.post_eval(clone, tree, head=clone.base_commit, keep=(), eval_outputs=(),
                         before=()).ok
    _write(os.path.join(tree.path, ".git"), "gitdir: /elsewhere\n")
    assert iso.nested_git(tree) == ()                      # below the root only
    res = iso.post_eval(clone, tree, head=clone.base_commit, keep=(), eval_outputs=(), before=())
    assert not res.ok and res.changed == (".git",)
    iso.purge_ignored(clone, tree, head=clone.base_commit, keep=())
    assert not os.path.lexists(os.path.join(tree.path, ".git"))
    assert iso.post_eval(clone, tree, head=clone.base_commit, keep=(), eval_outputs=(),
                         before=()).ok


def test_folders_git_never_lists_are_removed_before_the_eval_and_flagged_after(repo, clone,
                                                                              tmp_path, request):
    wt = iso.create_worktree(repo["root"], RUN, "t-91", clone, commit=clone.base_commit)
    os.makedirs(os.path.join(wt.path, "answers", "42"))              # untracked and empty
    _write(os.path.join(wt.path, "build", "43", "o.bin"), "x\n")     # an ignored file in a folder
    os.makedirs(os.path.join(wt.path, "src", "41"))                   # empty, in a tracked folder
    os.makedirs(os.path.join(wt.path, "out", "sub"))                  # admitted by keep
    _write(os.path.join(wt.path, "cache", "k.txt"), "kept\n")         # a kept file
    os.makedirs(os.path.join(wt.path, "cache", "40"))                 # beside it, not kept
    locked = os.path.join(wt.path, "hidden", "44")
    os.makedirs(locked)
    os.chmod(os.path.join(wt.path, "hidden"), 0o000)                   # made unreadable
    request.addfinalizer(lambda: os.path.isdir(os.path.join(wt.path, "hidden"))
                         and os.chmod(os.path.join(wt.path, "hidden"), 0o700))
    os.unlink(os.path.join(wt.path, "bench", "runbook.yaml"))         # empties a tracked folder
    cap = iso.capture(clone, wt, forked_from=clone.base_commit, message="m", stamp=STAMP,
                      out_dir=str(tmp_path / "ev"))
    assert cap.paths == ("bench/runbook.yaml",)                      # no folder is in the diff
    keep = ("out/", "cache/k.txt")
    res = iso.purge_ignored(clone, wt, head=cap.head, keep=keep)
    assert set(res.removed) == {"answers", "answers/42", "build", "build/43", "build/43/o.bin",
                                "src/41", "cache/40", "hidden", "hidden/44", "bench"}
    assert res.kept == ("cache/k.txt",)
    for gone in ("answers", "build", "src/41", "cache/40", "hidden", "bench"):
        assert not os.path.lexists(os.path.join(wt.path, gone)), gone
    assert os.path.isdir(os.path.join(wt.path, "out", "sub"))
    assert os.path.isfile(os.path.join(wt.path, "cache", "k.txt"))
    assert os.path.isfile(os.path.join(wt.path, "src", "app.py"))
    ok = iso.post_eval(clone, wt, head=cap.head, keep=keep, eval_outputs=("tmp/",),
                       before=res.kept)
    assert ok.ok and ok.new == ()
    # the eval leaves empty folders: an undeclared one is new, a declared one is not
    os.makedirs(os.path.join(wt.path, "answers", "42"))
    os.makedirs(os.path.join(wt.path, "tmp", "t"))
    _write(os.path.join(wt.path, "docs", "x.log"), "eval log\n")    # beside it, a file
    os.makedirs(os.path.join(wt.path, "docs", "39"))
    res = iso.post_eval(clone, wt, head=cap.head, keep=keep, eval_outputs=("tmp/",),
                        before=res.kept)
    assert not res.ok and res.new == ("answers", "docs/39", "docs/x.log")


def _acl_names(path):
    try:
        return [n for n in os.listxattr(path, follow_symlinks=False)
                if n.startswith("system.posix_acl")]
    except OSError:
        return []


def test_purge_gives_tracked_files_their_git_metadata_and_drops_xattrs_acls_and_mtimes(
        repo, clone, tmp_path):
    wt = iso.create_worktree(repo["root"], RUN, "t-92", clone, commit=clone.base_commit)
    app = os.path.join(wt.path, "src", "app.py")
    run = os.path.join(wt.path, "src", "run.sh")
    src = os.path.join(wt.path, "src")
    try:
        os.setxattr(app, "user.answer", b"42")
        xattrs = True
    except OSError:
        xattrs = False
    # a POSIX ACL the file owner may set: named entries that chmod leaves in place
    acls = subprocess.run(["setfacl", "-m", "u:nobody:r", app], capture_output=True).returncode \
        == 0 if shutil.which("setfacl") else False
    if acls:
        subprocess.run(["setfacl", "-d", "-m", "u:nobody:r", src], check=True)
        assert _acl_names(app) and _acl_names(src)
    _write(os.path.join(src, "planted.log"), "an ignored output\n")
    os.chmod(app, 0o602)
    os.chmod(src, 0o511)                                # the purge must open it to write
    # data in mtimes: a file, a tracked folder that holds only a folder, and the root
    _write(os.path.join(wt.path, "pkg", "sub", "mod.py"), "X = 1\n")
    os.utime(app, (4711, 4711))
    os.utime(os.path.join(wt.path, "pkg"), (4712, 4712))
    os.utime(wt.path, (4713, 4713))
    cap = iso.capture(clone, wt, forked_from=clone.base_commit, message="m", stamp=STAMP,
                      out_dir=str(tmp_path / "ev"))
    assert cap.paths == ("pkg/sub/mod.py",)             # no mode, xattr, ACL or mtime in the diff
    started = time.time() - 1
    res = iso.purge_ignored(clone, wt, head=cap.head, keep=())
    assert res.removed == ("src/planted.log",)
    assert stat.S_IMODE(os.stat(app).st_mode) == 0o644
    assert stat.S_IMODE(os.stat(run).st_mode) == 0o755
    assert stat.S_IMODE(os.stat(src).st_mode) == 0o755
    if xattrs:
        assert "user.answer" not in os.listxattr(app)
    if acls:
        assert _acl_names(app) == [] and _acl_names(src) == []
    stamps = {os.lstat(p).st_mtime for p in (app, run, src, wt.path,
                                             os.path.join(wt.path, "pkg"))}
    assert len(stamps) == 1 and stamps.pop() >= started      # one engine time, no planted one


def _read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


@pytest.mark.parametrize("how", ["ident", "text-eol", "text-auto-in-target"])
def test_tracked_bytes_the_eval_reads_are_heads_whatever_a_clean_conversion_hid(
        repo, clone, tmp_path, how):
    # `git add -A` applies the tree's own .gitattributes clean conversions, so the recorded blob
    # can differ from the bytes left in the tree (check-contract-r2 finding 1, check-adversarial-r2
    # B2): the purge rewrites every tracked file from head
    if how == "text-auto-in-target":
        # the target's own attributes already normalize: nothing needs to enter the diff
        root = repo["root"]
        _g(root, "checkout", "-q", "-b", "auto", repo["base"])
        _write(os.path.join(root, ".gitattributes"), "* text=auto\n")
        _g(root, "commit", "-qam", "normalize")
        base = _g(root, "rev-parse", "HEAD")
        clone = iso.build_clone(root, "rr-005", _contract(base), stamp=STAMP)
        run = "rr-005"
    else:
        run = RUN
    wt = iso.create_worktree(repo["root"], run, "t-94", clone, commit=clone.base_commit)
    if how == "ident":
        _write(os.path.join(wt.path, ".gitattributes"), "*.txt ident\n")
        rel, planted, secret = "src/answer.txt", b"$Id: THE-ANSWER-IS-42 $\n", b"ANSWER-IS-42"
    elif how == "text-eol":
        _write(os.path.join(wt.path, ".gitattributes"), "*.txt text eol=lf\n")
        rel, planted, secret = "src/data.txt", b"a\r\nb\nc\r\nd\n", b"\r"
    else:
        rel, planted, secret = "src/app.py", b"x = 2\r\ny = 3\n", b"\r"
    with open(os.path.join(wt.path, rel), "wb") as fh:
        fh.write(planted)
    cap = iso.capture(clone, wt, forked_from=clone.base_commit, message="m", stamp=STAMP,
                      out_dir=str(tmp_path / "ev"))
    assert rel in cap.paths
    assert secret not in _read_bytes(cap.path)                 # the diff does not carry it
    assert _read_bytes(os.path.join(wt.path, rel)) == planted   # the tree still did
    iso.purge_ignored(clone, wt, head=cap.head, keep=())
    got = _read_bytes(os.path.join(wt.path, rel))
    assert secret not in got                                    # the eval cannot read it now
    # the bytes are what a fresh cut of head gives, so they depend on head alone: head made
    # again in the clone from the recorded diff, as the merge and a verifier's tree make it
    assert iso.merge_champion(clone, branch=clone.branch, parent=clone.base_commit, capture=cap,
                              message="m", stamp=STAMP) == cap.head
    cut = iso.fresh_tree(repo["root"], clone, commit=cap.head, path=str(tmp_path / "cut"))
    assert got == _read_bytes(os.path.join(cut.path, rel))
    assert iso.post_eval(clone, wt, head=cap.head, keep=(), eval_outputs=(), before=()).ok
    # a tree rebuilt from the recorded diff holds the same bytes (`git apply` alone would leave
    # an `ident` unexpanded)
    iso.destroy_worktree(clone, wt)
    again = iso.rebuild(repo["root"], run, "t-94", clone, recorded=cap, message="m", stamp=STAMP)
    assert _read_bytes(os.path.join(again.path, rel)) == got


def _bind_socket(folder, name):
    here = os.getcwd()
    os.chdir(folder)                        # a relative bind keeps under the 108-byte limit
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(name)
    finally:
        os.chdir(here)
    return sock


def test_fifos_and_sockets_git_never_lists_are_removed_before_the_eval_and_flagged_after(
        repo, clone, tmp_path):
    wt = iso.create_worktree(repo["root"], RUN, "t-95", clone, commit=clone.base_commit)
    src = os.path.join(wt.path, "src")
    os.mkfifo(os.path.join(wt.path, "ANSWER-42"))                 # in the root, a tracked folder
    os.mkfifo(os.path.join(src, "ANSWER-src-7"))                  # in a tracked folder
    sock = _bind_socket(src, "SOCK-ANSWER-9")
    os.makedirs(os.path.join(wt.path, "out"))
    os.mkfifo(os.path.join(wt.path, "out", "kept-fifo"))          # admitted by keep
    try:
        cap = iso.capture(clone, wt, forked_from=clone.base_commit, message="m", stamp=STAMP,
                          out_dir=str(tmp_path / "ev"))
        assert cap.empty                                  # git never lists a FIFO or a socket
        # a file that appears after the capture is in no head either: head's entries only stay
        _write(os.path.join(src, "late-ANSWER.txt"), "written after the capture\n")
        res = iso.purge_ignored(clone, wt, head=cap.head, keep=("out/",))
    finally:
        sock.close()
    # a FIFO in a kept folder goes too: keep admits folders and regular files only
    # (check-adversarial-resume-r1 m2)
    assert set(res.removed) == {"ANSWER-42", "src/ANSWER-src-7", "src/SOCK-ANSWER-9",
                                "src/late-ANSWER.txt", "out/kept-fifo"}
    assert "ANSWER-42" not in os.listdir(wt.path)
    assert sorted(os.listdir(src)) == ["app.py", "link.py", "run.sh"]   # head's entries only
    assert os.listdir(os.path.join(wt.path, "out")) == []             # the kept folder stays
    assert iso.post_eval(clone, wt, head=cap.head, keep=("out/",), eval_outputs=("tmp/",),
                         before=res.kept).ok
    # the eval makes them again: post_eval names each one git never lists
    os.mkfifo(os.path.join(wt.path, "ANSWER-43"))
    os.mkfifo(os.path.join(src, "ANSWER-src-8"))
    os.makedirs(os.path.join(wt.path, "tmp"))
    os.mkfifo(os.path.join(wt.path, "tmp", "declared"))            # a declared eval output
    res = iso.post_eval(clone, wt, head=cap.head, keep=("out/",), eval_outputs=("tmp/",),
                        before=res.kept)
    assert not res.ok and res.new == ("ANSWER-43", "src/ANSWER-src-8") and res.changed == ()


def test_entries_inside_a_gitlink_folder_are_removed_before_the_eval_and_flagged_after(
        repo, clone, tmp_path):
    wt = iso.create_worktree(repo["root"], RUN, "t-96", clone, commit=clone.base_commit)
    sub = os.path.join(wt.path, "vendor", "sub")
    os.makedirs(sub)
    _g(sub, "init", "-q", "-b", "main")
    _write(os.path.join(sub, "answer.txt"), "42\n")
    _g(sub, "add", "-A")
    _g(sub, "commit", "-qm", "nested")
    cap = iso.capture(clone, wt, forked_from=clone.base_commit, message="m", stamp=STAMP,
                      out_dir=str(tmp_path / "ev"))
    assert cap.gitlinks == ("vendor/sub",)             # the nested repository is one gitlink
    assert cap.empty                                   # listed, never staged
    # a head that holds a gitlink at that folder (made here with the engine's own git; capture
    # never stages one), so the purge meets a gitlink folder of head with entries below it
    env = iso.git_env(clone, worktree=wt, index=str(tmp_path / "gl-index"))
    iso._git(["read-tree", clone.base_commit], env=env, cwd=wt.path)
    iso._git(["update-index", "--add", "--cacheinfo",
              "160000,%s,vendor/sub" % ("1" * 40)], env=env, cwd=wt.path)
    tree = iso._text(iso._git(["write-tree"], env=env, cwd=wt.path))
    head = iso._text(iso._git(["commit-tree", tree, "-p", clone.base_commit, "-m", "gl"],
                              env=iso._commit_env(env, STAMP), cwd=wt.path))
    res = iso.purge_ignored(clone, wt, head=head, keep=())
    assert {"vendor/sub/.git", "vendor/sub/answer.txt"} <= set(res.removed)
    assert os.listdir(sub) == []
    assert _eval_reads(wt.path, "vendor/sub/answer.txt") is None
    assert iso.post_eval(clone, wt, head=head, keep=(), eval_outputs=(), before=()).ok
    # git lists nothing below a gitlink folder, so post_eval names it itself
    _write(os.path.join(sub, "answer.txt"), "43\n")
    res = iso.post_eval(clone, wt, head=head, keep=(), eval_outputs=(), before=())
    assert not res.ok and res.new == ("vendor/sub/answer.txt",) and res.changed == ()


def test_a_hostile_dotgit_file_changes_nothing_for_capture_purge_and_post_eval(repo, clone,
                                                                                tmp_path):
    wt = iso.create_worktree(repo["root"], RUN, "t-93", clone, commit=clone.base_commit)
    prog, mark = _marker_program(tmp_path)
    evil = tmp_path / "evil-gitdir"
    os.makedirs(evil)
    _write(str(evil / "HEAD"), "garbage\n")
    _write(str(evil / "config"), "[core]\n\tfsmonitor = %s\n" % prog)
    _write(str(evil / "commondir"), str(tmp_path) + "\n")
    with open(os.path.join(wt.path, ".git"), "w") as fh:
        fh.write("gitdir: %s\n" % evil)
    _write(os.path.join(wt.path, "src/app.py"), "def f():\n    return 9\n")
    _write(os.path.join(wt.path, "build/x.bin"), "planted\n")
    cap = iso.capture(clone, wt, forked_from=clone.base_commit, message="m", stamp=STAMP,
                      out_dir=str(tmp_path / "ev"))
    assert cap.paths == ("src/app.py",)
    res = iso.purge_ignored(clone, wt, head=cap.head, keep=())
    assert res.removed == ("build", "build/x.bin")
    assert iso.post_eval(clone, wt, head=cap.head, keep=(), eval_outputs=(), before=()).ok
    assert not os.path.exists(mark)
    # negative: git run through that `.git` file does read the planted folder
    bad = subprocess.run(["git", "status"], cwd=str(tmp_path / "ev"), env=_env(
        GIT_DIR=str(evil), GIT_WORK_TREE=wt.path), capture_output=True, timeout=30)
    assert bad.returncode != 0


def test_build_clone_refuses_a_config_with_worktreeconfig(repo, monkeypatch):
    monkeypatch.setattr(iso, "_CLONE_CONFIG",
                        iso._CLONE_CONFIG + (("extensions.worktreeConfig", "true"),))
    with pytest.raises(IsolationError) as exc:
        iso.build_clone(repo["root"], "rr-077", _contract(repo["base"]), stamp=STAMP)
    assert exc.value.reason == "ISOLATION-CONFIG"
    assert not os.path.lexists(paths.clone_dir(repo["root"], "rr-077"))
    monkeypatch.undo()
    assert iso.build_clone(repo["root"], "rr-077", _contract(repo["base"]), stamp=STAMP)


# ---------------------------------------------------------------- files host git would block on

def _bounded(call, fifos, limit=20.0):
    """Run call() in a thread and wait at most limit seconds. When it is still running, open every
    FIFO of fifos for writing (which unblocks a reader) and fail: the call waited on a FIFO.
    Returns ("ok", value) or ("err", exception) and the seconds it took."""
    out = {}

    def run():
        try:
            out["ok"] = call()
        except BaseException as exc:            # the test inspects it
            out["err"] = exc

    started = time.monotonic()
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(limit)
    took = time.monotonic() - started
    if worker.is_alive():
        for fifo in fifos:
            try:
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
        worker.join(30)
        pytest.fail("the call waited on a planted FIFO for %.0f s" % took)
    kind = "ok" if "ok" in out else "err"
    return kind, out[kind], took


def _blocks(argv, env, cwd, fifos, limit=3):
    """Negative control: True when plain git, given the engine's environment, waits on a FIFO."""
    try:
        subprocess.run(argv, env=env, cwd=cwd, capture_output=True, timeout=limit)
    except subprocess.TimeoutExpired:
        return True
    finally:
        for fifo in fifos:
            try:
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
    return False


@pytest.mark.parametrize("plant", ["src/.gitignore", "newdir/.gitignore", "src/.gitattributes",
                                   ".gitmodules"])
def test_a_planted_fifo_git_opens_ends_capture_purge_and_post_eval_without_a_wait(
        repo, clone, tmp_path, plant):
    wt = iso.create_worktree(repo["root"], RUN, "t-97", clone, commit=clone.base_commit)
    fifo = os.path.join(wt.path, plant)
    os.makedirs(os.path.dirname(fifo), exist_ok=True)
    os.mkfifo(fifo)
    if plant.endswith(".gitignore"):
        # negative control: host git with the engine's environment, run on this tree with no walk
        # first, does wait on the FIFO (check-adversarial-r3 B1); its own index, so the killed
        # git leaves no lock in the engine's
        env = iso.git_env(clone, worktree=wt, index=str(tmp_path / "neg-index"))
        assert _blocks(["git", *iso.GIT_HARDEN, "add", "-A"], env, wt.path, [fifo])
    # capture: ISOLATION-CONTAINMENT naming the FIFO, at once, not after the time limit
    kind, got, took = _bounded(lambda: iso.capture(clone, wt, forked_from=clone.base_commit,
                                                   message="m", stamp=STAMP,
                                                   out_dir=str(tmp_path / "ev")), [fifo])
    assert kind == "err" and isinstance(got, IsolationError), got
    assert got.reason == "ISOLATION-CONTAINMENT" and plant in str(got)
    assert "timed out" not in str(got) and took < 10
    # post_eval (attempt code in a gate stage planted it): not ok, the FIFO in `new`, even when
    # keep and eval_outputs admit its folder, and no git runs
    for admit in ((), (os.path.dirname(plant) + "/",) if "/" in plant else (plant,)):
        kind, res, took = _bounded(lambda: iso.post_eval(
            clone, wt, head=clone.base_commit, keep=admit, eval_outputs=admit, before=()), [fifo])
        assert kind == "ok" and not res.ok and res.new == (plant,) and took < 10
    # purge: removes it before any git call, then runs; a tracked path comes back from head
    kind, res, took = _bounded(lambda: iso.purge_ignored(clone, wt, head=clone.base_commit,
                                                         keep=()), [fifo])
    assert kind == "ok" and plant in res.removed and took < 10
    assert not os.path.lexists(fifo) or stat.S_ISREG(os.lstat(fifo).st_mode)
    assert iso.post_eval(clone, wt, head=clone.base_commit, keep=(), eval_outputs=(),
                         before=()).ok


def test_purge_refuses_a_kept_fifo_git_opens_and_the_champion_is_cut_again(repo, clone,
                                                                           tmp_path):
    wt = iso.create_worktree(repo["root"], RUN, "t-98", clone, commit=clone.base_commit)
    os.makedirs(os.path.join(wt.path, "out"))
    fifo = os.path.join(wt.path, "out", ".gitignore")
    os.mkfifo(fifo)
    kind, got, _took = _bounded(lambda: iso.purge_ignored(clone, wt, head=clone.base_commit,
                                                          keep=("out/",)), [fifo])
    assert kind == "err" and isinstance(got, IsolationError)
    assert got.reason == "ISOLATION-CONTAINMENT" and "out/.gitignore" in str(got)
    assert stat.S_ISFIFO(os.lstat(fifo).st_mode)             # a kept path is never removed
    # the champion's clean check is the same compare: a FIFO there means a fresh cut, no wait
    champ = iso.champion_checkout(repo["root"], RUN, 4, clone, commit=clone.base_commit)
    planted = os.path.join(champ.path, "src", ".gitignore")
    os.mkfifo(planted)
    kind, again, _took = _bounded(lambda: iso.champion_checkout(
        repo["root"], RUN, 4, clone, commit=clone.base_commit), [planted])
    assert kind == "ok" and again == champ and not os.path.lexists(planted)


def test_a_nested_gitdir_with_a_fifo_head_is_listed_and_never_read(repo, clone, tmp_path):
    wt = iso.create_worktree(repo["root"], RUN, "t-99", clone, commit=clone.base_commit)
    # the implementer writes a gitdir in its private store (writable in its jail) whose HEAD is
    # a FIFO, and a `vend/.git` file naming it (check-adversarial-r3 B1, route 2)
    fake = os.path.join(wt.private_objects, "fake")
    os.makedirs(os.path.join(fake, "objects"))
    os.makedirs(os.path.join(fake, "refs"))
    head_fifo = os.path.join(fake, "HEAD")
    os.mkfifo(head_fifo)
    _write(os.path.join(wt.path, "vend", ".git"), "gitdir: %s\n" % fake)
    _write(os.path.join(wt.path, "vend", "c.txt"), "c\n")
    _write(os.path.join(wt.path, "src/app.py"), "def f():\n    return 99\n")
    env = iso.git_env(clone, worktree=wt, index=str(tmp_path / "neg-index"))
    assert _blocks(["git", *iso.GIT_HARDEN, "add", "-A"], env, wt.path, [head_fifo])
    kind, cap, took = _bounded(lambda: iso.capture(clone, wt, forked_from=clone.base_commit,
                                                   message="m", stamp=STAMP,
                                                   out_dir=str(tmp_path / "ev")), [head_fifo])
    assert kind == "ok", cap
    assert cap.gitlinks == ("vend",) and cap.paths == ("src/app.py",) and took < 10
    with open(cap.path, "rb") as fh:
        assert b"Subproject" not in fh.read()
    kind, res, took = _bounded(lambda: iso.post_eval(clone, wt, head=cap.head, keep=(),
                                                     eval_outputs=(), before=()), [head_fifo])
    assert kind == "ok" and not res.ok and res.nested == ("vend/.git",) and took < 10
    kind, res, took = _bounded(lambda: iso.purge_ignored(clone, wt, head=cap.head, keep=()),
                               [head_fifo])
    assert kind == "ok" and {"vend", "vend/.git"} & set(res.removed) and took < 10
    assert not os.path.lexists(os.path.join(wt.path, "vend", ".git"))
    assert stat.S_ISFIFO(os.lstat(head_fifo).st_mode)        # never opened, never removed


def test_a_nested_gitdir_naming_the_project_puts_no_fix_sha_in_the_diff(repo, tmp_path):
    root, fix = repo["root"], repo["fix"]
    rc = iso.build_repair_repo(root, RUN, _contract(repo["base"]), repair_task="t-36",
                               base_commit=repo["base"], fix_terms=(fix,), stamp=STAMP)
    wt = iso.create_worktree(root, RUN, "t-37", rc, commit=rc.base_commit)
    _write(os.path.join(wt.path, "vend", ".git"), "gitdir: %s\n" % os.path.join(root, ".git"))
    # negative control: plain `git add -A` with the engine's environment stages the project's
    # HEAD, the fix commit, as a gitlink (check-adversarial-r3 m1)
    probe = iso.git_env(rc, worktree=wt, index=str(tmp_path / "probe-index"))
    iso._git(["read-tree", rc.base_commit], env=probe, cwd=wt.path)
    subprocess.run(["git", *iso.GIT_HARDEN, "add", "-A"], env=probe, cwd=wt.path,
                   capture_output=True, timeout=30)
    staged = subprocess.run(["git", "ls-files", "-s"], env=probe, cwd=wt.path,
                            capture_output=True, timeout=30).stdout.decode()
    assert fix in staged
    cap = iso.capture(rc, wt, forked_from=rc.base_commit, message="m", stamp=STAMP,
                      out_dir=str(tmp_path / "ev"))
    assert cap.gitlinks == ("vend",)
    with open(cap.path, "rb") as fh:
        body = fh.read()
    assert fix.encode() not in body and fix[:12].encode() not in body


def test_every_engine_git_call_ends_at_its_time_limit(tmp_path, monkeypatch):
    fifo = str(tmp_path / "blocks")
    os.mkfifo(fifo)
    plain = str(tmp_path / "plain.txt")
    _write(plain, "x\n")
    env = iso._base_env()
    # negative control: the same call on a regular file returns at once
    assert len(iso._text(iso._git(["hash-object", plain], env=env, timeout=5))) == 40
    monkeypatch.setattr(iso, "_GIT_TIMEOUT_S", 2)
    kind, got, took = _bounded(lambda: iso._git(["hash-object", fifo], env=env), [fifo])
    assert kind == "err" and isinstance(got, IsolationError), got
    assert got.reason == "ISOLATION-CONTAINMENT" and "timed out" in str(got)
    assert 1.5 < took < 10


def test_a_repair_line_champion_is_labelled_by_its_round(repo):
    root = repo["root"]
    rc = iso.build_repair_repo(root, RUN, _contract(repo["base"]), repair_task="t-38",
                               base_commit=repo["base"], fix_terms=(repo["fix"],), stamp=STAMP)
    champ = iso.champion_checkout(root, RUN, 3, rc, commit=rc.base_commit)
    assert champ.label == "r3-champion"                      # architecture.md 3.1, types.py
    # the same WorktreeInfo fingerprint.take_attempt builds for this tree (architecture.md:720)
    assert champ == iso.tree_info(champ.path, commit=rc.base_commit, label="r3-champion",
                                  admin=True, read_only=True)
    assert os.path.basename(champ.path) == "t-38-r3-champion"
    assert iso.champion_checkout(root, RUN, 3, rc, commit=rc.base_commit) == champ


# ---------------------------------------------------------------- deep chains, locked folders

def _chain(top, depth, leaf=None):
    """mkdir a chain of `depth` folders named d under top, each made relative to its parent's
    descriptor (the path passes PATH_MAX); with leaf, write that file at the bottom. Returns
    nothing: the chain is only reachable a descriptor at a time."""
    os.makedirs(top, exist_ok=True)
    fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(depth):
            os.mkdir("d", dir_fd=fd)
            nxt = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = nxt
        if leaf:
            out = os.open(leaf, os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=fd)
            os.write(out, b"planted answer\n")
            os.close(out)
    finally:
        os.close(fd)


def _reads_leaf(top, depth, leaf):
    """The eval stand-in: walk down the chain a descriptor at a time and read leaf; None when any
    step is missing."""
    try:
        fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return None
    try:
        for _ in range(depth):
            nxt = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = nxt
        got = os.open(leaf, os.O_RDONLY, dir_fd=fd)
        try:
            return os.read(got, 100).decode()
        finally:
            os.close(got)
    except OSError:
        return None
    finally:
        os.close(fd)


def test_a_folder_chain_past_path_max_ends_in_a_result_never_a_recursion_error(repo, clone,
                                                                               tmp_path):
    # check-adversarial-resume-r1 B2: a 5000-deep chain under a tracked folder, with a planted
    # answer at the bottom that git cannot reach (git skips a path past PATH_MAX with a warning)
    wt = iso.create_worktree(repo["root"], RUN, "t-120", clone, commit=clone.base_commit)
    top = os.path.join(wt.path, "src")
    _chain(top, 5000, "answer.txt")
    assert _reads_leaf(top, 5000, "answer.txt") == "planted answer\n"
    # negative control: host git lists nothing of it, so only the walk can see it
    env = iso.git_env(clone, worktree=wt, index=str(tmp_path / "neg-index"))
    iso._git(["read-tree", clone.base_commit], env=env, cwd=wt.path)
    listed = subprocess.run(["git", *iso.GIT_HARDEN, "ls-files", "--others", "-z"], env=env,
                            cwd=wt.path, capture_output=True, timeout=60).stdout
    assert b"answer.txt" not in listed and b"src/d" not in listed
    # capture refuses it with the module's one error
    with pytest.raises(IsolationError) as exc:
        iso.capture(clone, wt, forked_from=clone.base_commit, message="m", stamp=STAMP,
                    out_dir=str(tmp_path / "ev"))
    assert exc.value.reason == "ISOLATION-CONTAINMENT" and "path limit" in str(exc.value)
    # post_eval (a gate stage built it): not ok, the chain in `new`, whatever keep admits
    res = iso.post_eval(clone, wt, head=clone.base_commit, keep=("src/",), eval_outputs=("src/",),
                        before=())
    assert not res.ok and len(res.new) == 1 and res.new[0].startswith("src/d/d/")
    # purge: nothing of the chain is left for the eval, and the tracked files are
    res = iso.purge_ignored(clone, wt, head=clone.base_commit, keep=())
    assert res.ok and "src/d" in res.removed
    assert not os.path.lexists(os.path.join(top, "d"))
    assert _reads_leaf(top, 5000, "answer.txt") is None
    assert os.path.isfile(os.path.join(top, "app.py"))
    assert iso.post_eval(clone, wt, head=clone.base_commit, keep=(), eval_outputs=(),
                         before=()).ok
    # the same chain below the path limit (1000 deep, 2 KB) is walked, not refused: purge
    # removes it and post_eval flags its top folder, all without a RecursionError
    _chain(os.path.join(wt.path, "gen"), 1000)
    res = iso.post_eval(clone, wt, head=clone.base_commit, keep=(), eval_outputs=(), before=())
    assert not res.ok and res.new == ("gen",)
    res = iso.purge_ignored(clone, wt, head=clone.base_commit, keep=())
    assert res.ok and "gen" in res.removed and not os.path.lexists(os.path.join(wt.path, "gen"))


def test_a_read_only_ignored_folder_does_not_keep_a_planted_output(repo, clone, tmp_path):
    # check-adversarial-resume-r1 B3: the agent takes the write bits off the ignored folders and
    # every bit off the planted file; the purge still deletes it before the eval
    wt = iso.create_worktree(repo["root"], RUN, "t-121", clone, commit=clone.base_commit)
    planted = os.path.join(wt.path, "build", "deep", "answer.bin")
    _write(planted, "planted answer\n")
    os.chmod(planted, 0)
    os.chmod(os.path.join(wt.path, "build", "deep"), 0o555)
    os.chmod(os.path.join(wt.path, "build"), 0o555)
    # negative control: a plain delete of the planted file is refused
    with pytest.raises(PermissionError):
        os.unlink(planted)
    cap = iso.capture(clone, wt, forked_from=clone.base_commit, message="m", stamp=STAMP,
                      out_dir=str(tmp_path / "ev"))
    assert cap.empty
    res = iso.purge_ignored(clone, wt, head=cap.head, keep=())
    assert res.ok and "build/deep/answer.bin" in res.removed
    assert not os.path.lexists(os.path.join(wt.path, "build"))


def test_a_kept_symlink_or_special_entry_is_removed_before_the_eval(repo, clone, tmp_path):
    # check-adversarial-resume-r1 m2: keep admits regular files and folders only; a kept symlink
    # would let the eval read outside the tree
    wt = iso.create_worktree(repo["root"], RUN, "t-122", clone, commit=clone.base_commit)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "answer.txt").write_text("host answer\n")
    os.symlink(str(outside), os.path.join(wt.path, "out"))                  # kept name, a link
    _write(os.path.join(wt.path, "cache", "k.txt"), "kept\n")                # a kept file
    os.symlink(str(outside / "answer.txt"), os.path.join(wt.path, "cache", "a.txt"))
    os.mkfifo(os.path.join(wt.path, "cache", "pipe"))
    keep = ("out/", "cache/")
    # negative control: through the kept link the eval would read the host file
    assert _eval_reads(wt.path, "out/answer.txt") == "host answer\n"
    res = iso.purge_ignored(clone, wt, head=clone.base_commit, keep=keep)
    assert res.ok and res.kept == ("cache/k.txt",)
    assert {"out", "cache/a.txt", "cache/pipe"} <= set(res.removed)
    assert _eval_reads(wt.path, "out/answer.txt") is None
    assert _eval_reads(wt.path, "cache/a.txt") is None
    assert not os.path.lexists(os.path.join(wt.path, "cache", "pipe"))
    assert _eval_reads(wt.path, "cache/k.txt") == "kept\n"
    assert (outside / "answer.txt").read_text() == "host answer\n"          # not followed


def test_an_os_error_in_a_tree_call_is_an_isolation_error(repo, clone, tmp_path, monkeypatch):
    # check-adversarial-resume-r1 B2 and B3: whatever the file system raises, the lifecycle gets
    # the module's one error, with the cause chained
    wt = iso.create_worktree(repo["root"], RUN, "t-123", clone, commit=clone.base_commit)
    _write(os.path.join(wt.path, "build", "x.bin"), "x\n")
    real = iso._rmtree

    def refuse(path):
        if "build" in path:
            raise PermissionError(13, "refused", path)
        return real(path)

    monkeypatch.setattr(iso, "_rmtree", refuse)
    with pytest.raises(IsolationError) as exc:
        iso.purge_ignored(clone, wt, head=clone.base_commit, keep=())
    assert exc.value.reason == "ISOLATION-CONTAINMENT"
    assert isinstance(exc.value.__cause__, PermissionError)
    monkeypatch.setattr(iso, "_rmtree", real)

    def deep(_wt):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(iso, "_hazards", deep)
    for call in (lambda: iso.post_eval(clone, wt, head=clone.base_commit, keep=(),
                                       eval_outputs=(), before=()),
                 lambda: iso.capture(clone, wt, forked_from=clone.base_commit, message="m",
                                     stamp=STAMP, out_dir=str(tmp_path / "ev"))):
        with pytest.raises(IsolationError) as exc:
            call()
        assert exc.value.reason == "ISOLATION-CONTAINMENT"
        assert isinstance(exc.value.__cause__, RecursionError)
