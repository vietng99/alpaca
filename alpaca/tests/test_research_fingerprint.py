"""t-092: tamper fingerprints and the strict allowlist check (alpaca/research/fingerprint.py).

Design: design/research-engine/architecture.md sections 3.2, 7.2 (rows 3, 9-10, 18-19, 21, 23a,
26, 33, 36-39), 8.3 (research-tamper) and 9.3; spec FR-015, FR-016, EC-007, EC-047, EC-048,
EC-049, EC-057 and SC-008.

Done when (the t-092 contract):

  * tests catch an edited protected file, a binary shadowed earlier on PATH and a replaced binary;
  * tampering by a concurrent slot flags every affected slot;
  * every path an attempt changed is checked against the editable-path allowlist, and a change
    outside it marks the attempt broken with the paths listed (gap map F02);
  * fail case: no check passes when the fingerprint source is missing.

Each control has a positive case (an untouched or allowed state gives ok) and a negative case (the
tamper gives BROKEN and names what changed). Writes go to tmp paths only; nothing touches the
network or starts an agent runtime; the "agent" is plain git run the way an implementer's jail
runs it (the worktree's .git file, the private object store).

t-091 (isolation.nested_git, git_env, tree_info) and t-096 (runbook.path_admitted) are built in
parallel with this task. The fixture `impl` uses their real code when it is there and, while a
name is still the scaffold's stub (NotImplementedError) or missing, a reference copy written here
from the design text (architecture.md section 3.1; runbook-format-3.md sections 2.2 and 14.1), so
the same tests run before and after those merges."""
from __future__ import annotations

import fnmatch
import hashlib
import inspect
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time

import pytest

from alpaca import db, runbook, util
from alpaca.research import events, fingerprint, isolation, paths
from alpaca.research.types import (CloneInfo, Contract, DiffCapture, FingerprintPoint,
                                   WorktreeInfo)

RUN = "rr-001"
TASK = "t-005"
ID = ["-c", "user.name=Research", "-c", "user.email=research@localhost",
      "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]
DATE = {"GIT_AUTHOR_DATE": "2026-09-30T00:00:00Z", "GIT_COMMITTER_DATE": "2026-09-30T00:00:00Z"}


# ================================================================ reference copies (t-091, t-096)

def ref_git_env(clone, *, worktree=None, index=None):
    """architecture.md section 3.1, git_env."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    if worktree is None:
        env["GIT_DIR"] = os.path.join(clone.path, ".git")
        return env
    chosen = index or worktree.index
    if chosen is None:
        raise ValueError("git_env needs an index")
    env.update(GIT_DIR=worktree.host_git, GIT_COMMON_DIR=os.path.join(clone.path, ".git"),
               GIT_WORK_TREE=worktree.path, GIT_INDEX_FILE=chosen,
               GIT_OBJECT_DIRECTORY=os.path.join(worktree.host_git, "objects"),
               GIT_ALTERNATE_OBJECT_DIRECTORIES=os.path.join(clone.path, ".git", "objects"))
    return env


def ref_nested_git(wt):
    """architecture.md section 3.1, nested_git: every `.git` entry below the root, never
    following a link."""
    found = []
    pending = [""]
    while pending:
        rel = pending.pop()
        with os.scandir(os.path.join(wt.path, rel) if rel else wt.path) as listing:
            for item in listing:
                child = item.name if not rel else rel + "/" + item.name
                if item.name == ".git" and rel:
                    found.append(child)
                    continue
                if item.is_dir(follow_symlinks=False):
                    pending.append(child)
    return tuple(sorted(found))


def ref_tree_info(path, *, commit, label, admin, read_only=False):
    """architecture.md section 3.1, tree_info: the naming rule alone."""
    host = path + ".git-host"
    return WorktreeInfo(path=path, label=label, commit=commit,
                        admin_dir=(path + ".git-admin") if admin else None,
                        index=os.path.join(host, "index"), host_git=host, read_only=read_only)


def ref_path_admitted(path, rules):
    """runbook-format-3.md sections 2.2 and 14.1: case kept, every rule read from the root, `*`
    and `?` inside one part, a folder rule admits that folder at the root and everything under it
    (the grammar of barrier.path_is_protected, alpaca/barrier.py:128 and :143)."""
    p = path[2:] if path.startswith("./") else path
    parts = p.split("/")
    for rule in rules:
        r = rule[2:] if rule.startswith("./") else rule
        if r.endswith("/"):
            body = r.rstrip("/").split("/")
            if len(parts) >= len(body) and all(
                    fnmatch.fnmatchcase(a, b) for a, b in zip(parts, body)):
                return rule
        else:
            body = r.split("/")
            if len(parts) == len(body) and all(
                    fnmatch.fnmatchcase(a, b) for a, b in zip(parts, body)):
                return rule
    return None


def _is_stub(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except NotImplementedError:
        return True
    except Exception:
        return False
    return False


@pytest.fixture
def impl(monkeypatch, tmp_path):
    """The real t-091 and t-096 names when they are built, else the reference copies above."""
    probe = WorktreeInfo(path=str(tmp_path), label="probe", commit="0" * 40, admin_dir=None,
                         index=str(tmp_path / "index"), host_git=str(tmp_path))
    clone = CloneInfo(path=str(tmp_path), line="champion", branch="b", base_commit="0" * 40,
                      head="0" * 40)
    used = {}
    if _is_stub(isolation.git_env, clone, worktree=probe):
        monkeypatch.setattr(isolation, "git_env", ref_git_env)
        used["git_env"] = "reference"
    if _is_stub(isolation.nested_git, probe):
        monkeypatch.setattr(isolation, "nested_git", ref_nested_git)
        used["nested_git"] = "reference"
    if _is_stub(isolation.tree_info, str(tmp_path / "x"), commit="0" * 40, label="x", admin=True):
        monkeypatch.setattr(isolation, "tree_info", ref_tree_info)
        used["tree_info"] = "reference"
    if not hasattr(runbook, "path_admitted"):
        monkeypatch.setattr(runbook, "path_admitted", ref_path_admitted, raising=False)
        used["path_admitted"] = "reference"
    return used


# ================================================================ a small world

def git(cwd, *args, env=None, check=True):
    base = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    base.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, **DATE)
    base.update(env or {})
    proc = subprocess.run(["git", *ID, *args], cwd=cwd, env=base, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
    if check and proc.returncode != 0:
        raise AssertionError("git %s: %s" % (args, proc.stderr.decode()))
    return proc.stdout.decode().strip()


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if mode is not None:
        os.chmod(path, mode)


FILES = {
    "src/app.py": "print('v0')\n",
    "tests/test_app.py": "def test_app():\n    assert True\n",
    "eval/config.yaml": "threshold: 3\n",
    "pack/runbook.yaml": "runbook: 3\n",
    "README.md": "readme\n",
    ".gitignore": ".alpaca/\nbuild/\n__pycache__/\n",
}
PROTECTED = ("tests/", "eval/")
EDITABLE = ("src/",)
FROZEN_FILES = ("tests/test_app.py", "eval/config.yaml")


def frozen_manifest():
    """contract/files.json as t-096's contract._manifest builds it: "files/<path>" -> sha256,
    mode, size."""
    out = {}
    for rel in FROZEN_FILES:
        data = FILES[rel].encode()
        out["files/" + rel] = {"sha256": hashlib.sha256(data).hexdigest(), "mode": "100644",
                               "size": len(data)}
    return out


# the contract's files_sha256: t-096 hashes the manifest in its normalized form (contract._canon,
# util.canonical_json) and writes files.json in that same form
FILES_SHA256 = util.sha256_hex(util.canonical_json(frozen_manifest()))


def make_contract(**over) -> Contract:
    values = dict(
        runbook="pack/runbook.yaml", runbook_id="bench", runbook_sha256="0" * 64, pack=None,
        task=None, ref="contract:pack/runbook.yaml#-", mode="improve", repo_path=".",
        repo_ref="HEAD", repo_commit="0" * 40, stages=(), knobs={}, metrics=(), targets=(),
        accept_rule="champion", after_targets=None, editable=EDITABLE, protected=PROTECTED,
        hidden=(), tests=("tests/",), read_paths=(), read_real=(), keep=(), outputs=(),
        tools=(), tools_real=(), shared=(), gates=(), slots=2, rounds=3, budget_usd=1.0,
        budget_tokens=1000, budget_wall_s=None, disk_gib=1.0, proposal=(), categories=(),
        lessons_file=None, note_chars=400, noop_rounds=2, controls_random=False,
        controls_mutate=None, controls_syntax=None, controls_seed=0, holdout_stages=(),
        holdout_paths=(), repair=(), pass_to_pass=None, repair_prepare=None, coupled=(),
        agents=(), infra_retries=2, verifier_timeout_s=1800, verifier_rerun_timeout_s=1200,
        audit_hints=None, audit_assert_tokens=(), audit_build_files=(), audit_steering=(),
        egress_extra=(), block={}, override_diff=(), grants=(), reproducers={},
        pack_sha256="0" * 64, sha256="0" * 64, files_sha256=FILES_SHA256)
    values.update(over)
    return Contract(**values)


def cut(clone, path, commit, *, host_git, admin_dir=None, label="tree", private=None):
    """A tree cut as architecture.md section 3.1 cuts it: the engine's host git folder (HEAD and
    an object store with the clone's objects as its alternate), `git read-tree -u --reset` with
    the engine environment, and with an admin folder outside the clone's .git that the tree's
    .git file names."""
    os.makedirs(os.path.join(host_git, "objects", "info"), exist_ok=True)
    write(os.path.join(host_git, "objects", "info", "alternates"),
          os.path.join(clone.path, ".git", "objects") + "\n")
    write(os.path.join(host_git, "HEAD"), commit + "\n")
    os.makedirs(path, exist_ok=True)
    wt = WorktreeInfo(path=path, label=label, commit=commit, admin_dir=admin_dir,
                      private_objects=private, index=os.path.join(host_git, "index"),
                      host_git=host_git)
    subprocess.run(["git", *isolation.GIT_HARDEN, "read-tree", "-u", "--reset", commit],
                   cwd=path, env=ref_git_env(clone, worktree=wt), check=True)
    if admin_dir:
        os.makedirs(admin_dir, exist_ok=True)
        write(os.path.join(admin_dir, "HEAD"), commit + "\n")
        write(os.path.join(admin_dir, "commondir"), os.path.join(clone.path, ".git") + "\n")
        env = ref_git_env(clone)
        env.update(GIT_DIR=admin_dir, GIT_INDEX_FILE=os.path.join(admin_dir, "index"))
        subprocess.run(["git", "read-tree", commit], cwd=path, env=env, check=True)
        write(os.path.join(path, ".git"), "gitdir: %s\n" % admin_dir)
    if private:
        os.makedirs(private, exist_ok=True)
    return wt


class World:
    """A project root that is also the main checkout, a run's frozen contract, a stripped clone,
    the round's champion checkout, one attempt worktree, two tool folders, a shared git checkout
    and a read path."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        # a test's own copies and links go here, never beside the tool, shared and read folders:
        # tmp_path is in their ancestor chain, whose ctime a new entry there moves
        self.scratch = tmp_path / "scratch"
        os.makedirs(self.scratch)
        self.root = str(tmp_path / "proj")
        os.makedirs(self.root)
        for rel, text in FILES.items():
            write(os.path.join(self.root, rel), text)
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "base")
        # the frozen contract (section 8.4): contract.json, files.json (the manifest of t-096's
        # contract._manifest, written as contract.freeze writes it), files/<protected path>
        self.frozen = paths.contract_dir(self.root, RUN)
        write(os.path.join(self.frozen, "contract.json"), '{"frozen": true}\n')
        for rel in FROZEN_FILES:
            write(os.path.join(self.frozen, "files", rel), FILES[rel])
        write(os.path.join(self.frozen, "files.json"), util.canonical_json(frozen_manifest()))
        # the stripped clone: the tracked files, one commit, an empty worktree registry
        cpath = paths.clone_dir(self.root, RUN)
        os.makedirs(cpath)
        for rel, text in FILES.items():
            write(os.path.join(cpath, rel), text)
        git(cpath, "init", "-q", "-b", "main")
        git(cpath, "add", "-A")
        git(cpath, "commit", "-qm", "base")
        os.makedirs(os.path.join(cpath, ".git", "worktrees"), exist_ok=True)
        self.commit = git(cpath, "rev-parse", "HEAD")
        self.clone = CloneInfo(path=cpath, line="champion", branch="research/%s" % RUN,
                               base_commit=self.commit, head=self.commit)
        # the round's champion checkout, cut with an admin folder
        cdir = paths.champion_dir(self.root, RUN, 1)
        self.champion = cut(self.clone, cdir, self.commit, host_git=cdir + ".git-host",
                            admin_dir=cdir + ".git-admin", label="r1-champion")
        self.worktree = self.attempt_tree(TASK)
        # tools: mytool lives in bin2; bin1 comes first on the gate PATH
        self.bin1, self.bin2 = str(tmp_path / "bin1"), str(tmp_path / "bin2")
        os.makedirs(self.bin1)
        write(os.path.join(self.bin2, "mytool"), "#!/bin/sh\necho tool v1\n", 0o755)
        self.path_env = "%s:%s" % (self.bin1, self.bin2)
        # a shared git checkout and a read path outside the project
        self.shared = str(tmp_path / "shared")
        write(os.path.join(self.shared, "lib.txt"), "shared v1\n")
        git(self.shared, "init", "-q", "-b", "main")
        git(self.shared, "add", "-A")
        git(self.shared, "commit", "-qm", "shared")
        self.data = str(tmp_path / "data")
        write(os.path.join(self.data, "set", "rows.csv"), "a,b\n1,2\n")
        self.contract = make_contract(
            tools=("mytool",), tools_real=(os.path.realpath(os.path.join(self.bin2, "mytool")),),
            shared=(self.shared,), read_real=(self.data,))

    def attempt_tree(self, task):
        adir = paths.attempt_dir(self.root, RUN, task)
        return cut(self.clone, paths.worktree_dir(self.root, RUN, task), self.commit,
                   host_git=os.path.join(adir, "git", "host"),
                   admin_dir=os.path.join(adir, "git", "admin"), label=task,
                   private=os.path.join(adir, "git", "objects"))

    def take(self, point, *, task=TASK, worktree="default", champion="default", clone="default",
             contract=None):
        return fingerprint.take(
            self.root, RUN, task, point, contract=contract or self.contract,
            worktree=self.worktree if worktree == "default" else worktree,
            clone=self.clone if clone == "default" else clone,
            champion=self.champion if champion == "default" else champion,
            path_env=self.path_env)

    def agent(self, *args, wt=None, check=True):
        """git as the implementer runs it: the tree's .git file, the private object store."""
        wt = wt or self.worktree
        return git(wt.path, *args, check=check,
                   env={"GIT_OBJECT_DIRECTORY": wt.private_objects,
                        "GIT_ALTERNATE_OBJECT_DIRECTORIES": os.path.join(self.clone.path, ".git",
                                                                         "objects")})

    def check(self, wt=None):
        return fingerprint.check_paths(self.clone, wt or self.worktree, forked_from=self.commit,
                                       editable=EDITABLE, protected=PROTECTED)


@pytest.fixture
def world(tmp_path, impl):
    return World(tmp_path)


# ================================================================ the module's surface

DESIGN_SIGS = {
    "resolve_tools": "(names: 'tuple[str, ...]', path_env: 'str') -> 'dict[str, ToolPin]'",
    "take": ("(root: 'str', run: 'str', task: 'str', point: 'str', *, contract: 'Contract', "
             "worktree: 'WorktreeInfo | None', clone: 'CloneInfo', "
             "champion: 'WorktreeInfo | None', path_env: 'str') -> 'FingerprintPoint'"),
    "take_attempt": "(conn, root: 'str', attempt: 'str', point: 'str') -> 'FingerprintPoint'",
    "compare": "(before: 'FingerprintPoint', after: 'FingerprintPoint') -> 'TamperVerdict'",
    "allowlist_match": "(path: 'str', rules: 'tuple[str, ...]') -> 'str | None'",
    "check_paths": ("(clone: 'CloneInfo', wt: 'WorktreeInfo', *, forked_from: 'str', "
                    "editable: 'tuple[str, ...]', protected: 'tuple[str, ...]') -> "
                    "'TamperVerdict'"),
    "check_diff": ("(capture: 'DiffCapture', *, editable: 'tuple[str, ...]', "
                   "protected: 'tuple[str, ...]') -> 'TamperVerdict'"),
    "affected": "(conn, run: 'str', source_key: 'str', *, since_event: 'int') -> 'list[str]'",
}
# names this module adds beside the design's: none (the helpers are private)
ADDED: set = set()


def test_the_design_api_is_built_with_its_signatures():
    for name, sig in DESIGN_SIGS.items():
        fn = getattr(fingerprint, name)
        assert str(inspect.signature(fn)) == sig, name
        assert "Stub" not in (fn.__doc__ or ""), name
    public = {n for n, o in vars(fingerprint).items() if not n.startswith("_") and (
        (inspect.isfunction(o) and o.__module__ == fingerprint.__name__)
        or (n.isupper() and not inspect.ismodule(o)))}
    assert public == set(DESIGN_SIGS) | {"POINTS", "AUDIT_POINTS"} | ADDED, sorted(public)
    assert fingerprint.POINTS == ("open", "after-proposer", "after-implementer", "after-eval",
                                  "after-audit", "after-notes", "close", "reopen")
    assert fingerprint.AUDIT_POINTS == ("audit-before", "audit-after")
    # event kinds live in events.py only (architecture.md:302-304)
    assert not any(isinstance(v, str) and v.startswith("research-")
                   for n, v in vars(fingerprint).items() if n.isupper())
    # negative: a stub body would raise; none does
    with pytest.raises(ValueError):
        fingerprint.take("/nowhere", RUN, TASK, "not-a-point", contract=make_contract(),
                         worktree=None, clone=None, champion=None, path_env="")


def test_git_harden_holds_the_submodule_pin():
    assert "diff.ignoreSubmodules=all" in isolation.GIT_HARDEN
    assert "core.fsmonitor=false" in isolation.GIT_HARDEN


# ================================================================ points, compare

def test_an_untouched_attempt_keeps_every_digest(world):
    before = world.take("open", worktree=None)
    after = world.take("after-implementer")
    verdict = fingerprint.compare(before, after)
    assert verdict.ok, verdict
    assert not before.errors and not after.errors
    keys = set(after.entries)
    # every source of section 9.3 is there
    for prefix in ("contract:", "frozen:", "clone:", "champion:", "champion-admin:", "main:",
                   "shared:", "read:", "tool:"):
        assert any(k.startswith(prefix) for k in keys), prefix
    assert "contract:tests/test_app.py" in keys and "contract:eval/config.yaml" in keys
    assert "tool:mytool" in keys and "clone:.git/config" in keys
    assert "main:tests/test_app.py" in keys and "main:README.md" not in keys   # out of scope
    assert "champion-admin:index" not in keys and "champion-admin:HEAD" in keys


def test_the_point_is_saved_and_reads_back(world):
    fp = world.take("open", worktree=None)
    want = os.path.join(paths.attempt_dir(world.root, RUN, TASK), "fingerprints", "open.json")
    assert fp.path == want == fingerprint._point_path(world.root, RUN, TASK, "open")
    back = fingerprint._load_point(fp.path)
    assert back == fp
    with open(fp.path, encoding="utf-8") as fh:
        body = json.load(fh)
    assert body["entries"] == fp.entries and body["point"] == "open"
    # negative: an unknown point writes nothing
    with pytest.raises(ValueError):
        world.take("after-lunch")
    assert not os.path.exists(fingerprint._point_path(world.root, RUN, TASK, "after-lunch"))


def test_an_implementer_edit_to_an_editable_file_breaks_no_digest(world):
    before = world.take("after-proposer", worktree=None)
    write(os.path.join(world.worktree.path, "src", "app.py"), "print('v1')\n")
    write(os.path.join(world.worktree.path, "src", "new.py"), "x = 1\n")
    assert fingerprint.compare(before, world.take("after-implementer")).ok


def test_an_edited_protected_file_is_caught(world):
    before = world.take("after-implementer")
    write(os.path.join(world.worktree.path, "tests", "test_app.py"), "def test_app(): pass\n")
    verdict = fingerprint.compare(before, world.take("after-eval"))
    assert not verdict.ok and verdict.changed == ("contract:tests/test_app.py",)
    assert verdict.point == "after-eval"


def test_a_deleted_protected_file_and_a_mode_change_are_caught(world):
    before = world.take("after-implementer")
    os.unlink(os.path.join(world.worktree.path, "eval", "config.yaml"))
    os.chmod(os.path.join(world.worktree.path, "tests", "test_app.py"), 0o755)
    verdict = fingerprint.compare(before, world.take("after-eval"))
    assert not verdict.ok
    assert set(verdict.changed) == {"contract:eval/config.yaml", "contract:tests/test_app.py"}


def test_a_protected_file_swapped_for_a_link_or_a_fifo_is_caught_without_following(world):
    before = world.take("after-implementer")
    target = os.path.join(world.scratch, "same.py")
    write(target, FILES["tests/test_app.py"])                 # the same bytes, behind a link
    victim = os.path.join(world.worktree.path, "tests", "test_app.py")
    os.unlink(victim)
    os.symlink(target, victim)
    fifo = os.path.join(world.worktree.path, "eval", "config.yaml")
    os.unlink(fifo)
    os.mkfifo(fifo)
    box = {}
    thread = threading.Thread(target=lambda: box.setdefault("fp", world.take("after-eval")))
    thread.start()
    thread.join(20)
    assert not thread.is_alive(), "a FIFO in a contract path blocked the point"
    after = box["fp"]
    assert after.entries["contract:tests/test_app.py"] == "link " + target
    assert after.entries["contract:eval/config.yaml"].startswith("special fifo")
    assert not fingerprint.compare(before, after).ok


def test_a_protected_folder_swapped_for_a_link_to_a_copy_is_caught(world):
    before = world.take("after-implementer")
    copy = str(world.scratch / "copy-of-tests")
    shutil.copytree(os.path.join(world.worktree.path, "tests"), copy)   # the same bytes
    shutil.rmtree(os.path.join(world.worktree.path, "tests"))
    os.symlink(copy, os.path.join(world.worktree.path, "tests"))
    after = world.take("after-eval")
    assert after.entries["contract:tests/test_app.py"] == "link-in-path tests -> " + copy
    assert after.entries["contract:tests"] == "link " + copy     # the walk sees the link itself
    verdict = fingerprint.compare(before, after)
    assert not verdict.ok
    assert verdict.changed == ("contract:tests", "contract:tests/test_app.py")


def test_frozen_contract_files_are_a_source(world):
    before = world.take("open", worktree=None)
    write(os.path.join(world.frozen, "files", "eval", "config.yaml"), "threshold: 0\n")
    write(os.path.join(world.frozen, "contract.json"), '{"frozen": false}\n')
    after = world.take("after-proposer", worktree=None)
    verdict = fingerprint.compare(before, after)
    assert not verdict.ok
    # the edited copy no longer matches files.json: an error entry besides the two digests
    assert set(verdict.changed) == {"frozen", "frozen:files/eval/config.yaml",
                                    "frozen:contract.json"}
    assert "does not match the sha256 files.json lists" in after.entries["frozen"]
    # the contract paths still come from the parsed manifest
    assert "contract:eval/config.yaml" in after.entries
    # so a pair taken after the edit is BROKEN too, not two equal points
    assert not fingerprint.compare(after, world.take("after-implementer")).ok


def test_the_clone_refs_config_hooks_and_registry_are_a_source(world):
    before = world.take("open", worktree=None)
    gitdir = os.path.join(world.clone.path, ".git")
    git(world.clone.path, "update-ref", "refs/heads/planted", world.commit)
    write(os.path.join(gitdir, "hooks", "post-checkout"), "#!/bin/sh\n", 0o755)
    with open(os.path.join(gitdir, "config"), "a", encoding="utf-8") as fh:
        fh.write("[core]\n\tfsmonitor = /nonexistent/evil\n")
    write(os.path.join(gitdir, "worktrees", "planted", "gitdir"), "/elsewhere/.git\n")
    verdict = fingerprint.compare(before, world.take("after-proposer", worktree=None))
    assert not verdict.ok
    assert {"clone:.git/hooks/post-checkout", "clone:.git/config",
            "clone:.git/worktrees/planted/gitdir",
            "clone:.git/refs/heads/planted"} <= set(verdict.changed)


def test_the_champion_checkout_is_a_source(world):
    before = world.take("open", worktree=None)
    write(os.path.join(world.champion.path, "src", "app.py"), "print('tampered')\n")
    write(os.path.join(world.champion.admin_dir, "HEAD"), "0" * 40 + "\n")
    verdict = fingerprint.compare(before, world.take("after-proposer", worktree=None))
    assert not verdict.ok
    # the proposer's read tree is the champion: the edit shows under both of its keys
    assert set(verdict.changed) == {"champion:src/app.py", "champion-admin:HEAD"}


def test_the_champion_admin_index_is_left_out(world):
    before = world.take("open", worktree=None)
    env = ref_git_env(world.clone)
    env.update(GIT_DIR=world.champion.admin_dir,
               GIT_INDEX_FILE=os.path.join(world.champion.admin_dir, "index"))
    os.unlink(os.path.join(world.champion.admin_dir, "index"))
    subprocess.run(["git", "read-tree", world.commit], cwd=world.champion.path, env=env,
                   check=True)                         # a recut writes other index bytes
    assert fingerprint.compare(before, world.take("reopen", worktree=None)).ok


def test_an_edit_to_the_main_checkout_under_the_pack_scopes_is_caught(world):
    """EC-057: an edit of the main checkout outside the worktree marks the attempt broken."""
    before = world.take("after-implementer")
    write(os.path.join(world.root, "README.md"), "outside every scope\n")     # not a scope path
    git(world.root, "add", "README.md")
    git(world.root, "commit", "-qm", "another session's commit outside the scopes")
    assert fingerprint.compare(before, world.take("after-eval")).ok
    write(os.path.join(world.root, "eval", "config.yaml"), "threshold: 0\n")
    write(os.path.join(world.root, "src", "planted.py"), "x = 1\n")
    write(os.path.join(world.root, "pack", "extra.sh"), "exit 0\n")
    verdict = fingerprint.compare(before, world.take("after-eval"))
    assert not verdict.ok
    assert set(verdict.changed) == {"main:eval/config.yaml", "main:src/planted.py",
                                    "main:pack/extra.sh"}
    # the engine never resets the main checkout: the edit is still there
    with open(os.path.join(world.root, "eval", "config.yaml"), encoding="utf-8") as fh:
        assert fh.read() == "threshold: 0\n"


def test_a_shared_checkout_and_a_read_path_are_sources(world):
    before = world.take("open", worktree=None)
    write(os.path.join(world.shared, "lib.txt"), "shared v2\n")
    write(os.path.join(world.data, "set", "rows.csv"), "a,b\n9,9\n")
    verdict = fingerprint.compare(before, world.take("after-proposer", worktree=None))
    assert not verdict.ok
    assert set(verdict.changed) == {"shared:%s/lib.txt" % os.path.realpath(world.shared),
                                    "read:%s/set/rows.csv" % os.path.realpath(world.data)}


# ================================================================ tools (EC-048)

def test_resolve_tools_pins_the_realpath_with_its_mode(world):
    pins = fingerprint.resolve_tools(("mytool", "absent-tool", "a/b"), world.path_env)
    pin = pins["mytool"]
    assert pin.resolved == os.path.join(world.bin2, "mytool") and pin.error is None
    assert pin.mode == 0o755 and len(pin.sha256) == 64
    assert pins["absent-tool"].error and pins["absent-tool"].sha256 is None
    assert pins["a/b"].error == "not a bare command name"


def test_a_binary_shadowed_earlier_on_path_is_caught(world):
    before = world.take("after-implementer")
    # a copy with the same bytes, earlier on the PATH
    shutil.copy2(os.path.join(world.bin2, "mytool"), os.path.join(world.bin1, "mytool"))
    verdict = fingerprint.compare(before, world.take("after-eval"))
    assert not verdict.ok and verdict.changed == ("tool-path:" + world.bin1, "tool:mytool")


def test_a_binary_shadowed_by_a_link_to_itself_is_caught(world):
    before = world.take("after-implementer")
    os.symlink(os.path.join(world.bin2, "mytool"), os.path.join(world.bin1, "mytool"))
    verdict = fingerprint.compare(before, world.take("after-eval"))
    assert not verdict.ok and verdict.changed == ("tool-path:" + world.bin1, "tool:mytool")


def test_a_binary_replaced_in_place_is_caught(world):
    tool = os.path.join(world.bin2, "mytool")
    before = world.take("after-implementer")
    st = os.stat(tool)
    write(tool, "#!/bin/sh\necho tool v2\n", 0o755)          # the same size
    os.utime(tool, ns=(st.st_atime_ns, st.st_mtime_ns))      # and the old mtime restored
    after = world.take("after-eval")
    verdict = fingerprint.compare(before, after)
    assert not verdict.ok and verdict.changed == ("tool:mytool",)
    # the content hash is in the digest on its own, beside the stamp (mutation-contract-r1 M3b)
    with open(tool, "rb") as fh:
        new_sha = hashlib.sha256(fh.read()).hexdigest()
    sha_of = lambda fp: fp.entries["tool:mytool"].split()[0]      # noqa: E731
    assert sha_of(after) == "sha256=" + new_sha != sha_of(before)


def test_a_binary_whose_mode_changed_is_caught(world):
    before = world.take("after-implementer")
    os.chmod(os.path.join(world.bin2, "mytool"), 0o4755)
    after = world.take("after-eval")
    verdict = fingerprint.compare(before, after)
    assert not verdict.ok and verdict.changed == ("tool:mytool",)
    assert " mode=4755 " in after.entries["tool:mytool"]      # the mode itself, not only the ctime


# ================================================================ missing sources fail closed (EC-007)

MISSING = {
    "tool not on the gate PATH": lambda w: {"contract": make_contract(
        tools=("mytool", "no-such-tool"), shared=(w.shared,), read_real=(w.data,))},
    "frozen contract folder gone": lambda w: shutil.rmtree(w.frozen) or {},
    "frozen files folder gone": lambda w: shutil.rmtree(os.path.join(w.frozen, "files")) or {},
    "shared checkout gone": lambda w: shutil.rmtree(w.shared) or {},
    "read path gone": lambda w: shutil.rmtree(w.data) or {},
    "clone gone": lambda w: {"clone": None},
    "clone config gone": lambda w: os.unlink(os.path.join(w.clone.path, ".git", "config")) or {},
    "champion checkout gone": lambda w: {"champion": None, "worktree": w.worktree},
    "no tree to read the contract paths in": lambda w: {"champion": None, "worktree": None},
    "main checkout not a git checkout": lambda w: {"contract": make_contract(
        repo_path=str(w.scratch / "plain"), tools=("mytool",), shared=(w.shared,),
        read_real=(w.data,))},
    "unreadable tool": lambda w: os.chmod(os.path.join(w.bin2, "mytool"), 0o311) or {},
    "attempt worktree gone": lambda w: shutil.rmtree(w.worktree.path) or {"worktree": w.worktree},
    "contract.json gone": lambda w: os.unlink(os.path.join(w.frozen, "contract.json")) or {},
    "files.json gone": lambda w: os.unlink(os.path.join(w.frozen, "files.json")) or {},
}


@pytest.mark.parametrize("case", sorted(MISSING))
def test_a_missing_fingerprint_source_fails_closed(world, case):
    if case == "unreadable tool" and os.geteuid() == 0:
        pytest.skip("root reads a file without the read bit")
    os.makedirs(str(world.scratch / "plain"), exist_ok=True)
    clean = world.take("open", worktree=None)
    assert fingerprint.compare(clean, clean).ok                 # positive: nothing missing
    over = MISSING[case](world)
    after = world.take("after-proposer", **over)
    assert after.errors, case
    assert any(v.startswith("error: ") for v in after.entries.values())
    verdict = fingerprint.compare(clean, after)
    assert not verdict.ok and verdict.errors, case
    # the fail case: a point compared with itself still fails while its source is missing,
    # so no check passes because both sides agree that nothing could be read
    same = fingerprint.compare(after, after)
    assert not same.ok and same.errors and not same.changed, case


def test_compare_refuses_points_of_two_attempts(world):
    a = world.take("open", worktree=None)
    b = world.take("open", worktree=None, task="t-006")
    with pytest.raises(ValueError):
        fingerprint.compare(a, b)


def test_an_added_or_missing_entry_is_broken():
    base = dict(run=RUN, task=TASK, taken_at="now", errors=(), path="/x")
    before = FingerprintPoint(point="open", entries={"a": "1", "b": "2"}, **base)
    added = FingerprintPoint(point="close", entries={"a": "1", "b": "2", "c": "3"}, **base)
    gone = FingerprintPoint(point="close", entries={"a": "1"}, **base)
    assert fingerprint.compare(before, before).ok
    assert fingerprint.compare(before, added).changed == ("c",)
    assert not fingerprint.compare(before, added).ok
    v = fingerprint.compare(before, gone)
    assert v.missing == ("b",) and not v.ok


# ================================================================ concurrent slots (EC-047)

def _record_point(conn, fp, *, ok=True):
    """The research-tamper row the lifecycle records for a point: `path` and the sha256 of the
    point file as saved (architecture.md section 8.3)."""
    try:
        with open(fp.path, "rb") as fh:
            sha = hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        sha = "0" * 64
    return events.emit(conn, events.TAMPER, run=RUN, ref=fp.task, op=None,
                       session="research-%s" % RUN, code=0 if ok else 1,
                       data={"point": fp.point, "changed": [], "missing": [], "outside": [],
                             "protected": [], "nested": [], "affected": [], "path": fp.path,
                             "sha256": sha})["id"]


def _add_task(conn, task, *, role="attempt", run=RUN):
    """The task-add a research task gets first (section 8.2), with its `research` object."""
    return db.append_event(conn, session="research-" + run, actor="research-runner",
                           kind="task-add", op="op-001", ref=task,
                           data={"title": role, "research": {"run": run, "role": role,
                                                             "key": "k-" + task}})


def test_a_concurrent_slots_tamper_flags_every_affected_slot(world):
    conn = db.connect(world.root)
    for task in ("t-005", "t-006", "t-007", "t-008", "t-009", "t-010"):
        _add_task(conn, task)
    slot_b = world.attempt_tree("t-006")
    # slot A (t-005) and slot B (t-006) open their windows; C (t-007) opens and closes before
    a_open = world.take("open", worktree=None)
    _record_point(conn, a_open)
    b_open = world.take("open", task="t-006", worktree=None)
    _record_point(conn, b_open)
    c_open = world.take("open", task="t-007", worktree=None)
    _record_point(conn, c_open)
    _record_point(conn, world.take("close", task="t-007", worktree=None))
    # E (t-009) was recorded (research-attempt) before the change; F's point file is gone
    _record_point(conn, world.take("open", task="t-009", worktree=None))
    events.emit(conn, events.ATTEMPT, run=RUN, ref="t-009", op=None, session="research-" + RUN,
                code=0, data={"key": "rr-001:r1:s4"})
    f_open = world.take("open", task="t-010", worktree=None)
    _record_point(conn, f_open)
    os.unlink(f_open.path)
    # D (t-008) reads another line: its saved point has no tool key
    d_point = fingerprint._save(world.root, RUN, "t-008", "open", {"clone:.git/HEAD": "x"}, [])
    _record_point(conn, d_point)
    a_prop = world.take("after-proposer", worktree=None)
    since = _record_point(conn, a_prop)
    # another run's slot is never flagged
    events.emit(conn, events.TAMPER, run="rr-002", ref="t-011", op=None, session="research-rr-002",
                code=0, data={"point": "open", "path": a_prop.path})
    # slot A's implementer replaces the shared tool in place
    write(os.path.join(world.bin2, "mytool"), "#!/bin/sh\necho planted\n", 0o755)
    a_impl = world.take("after-implementer")
    va = fingerprint.compare(a_prop, a_impl)
    assert not va.ok and va.changed == ("tool:mytool",)
    # slot B, whose implementer did nothing, sees the same change at its own next point
    b_impl = world.take("after-implementer", task="t-006", worktree=slot_b)
    vb = fingerprint.compare(b_open, b_impl)
    assert not vb.ok and vb.changed == ("tool:mytool",)
    flagged = fingerprint.affected(conn, RUN, "tool:mytool", since_event=since)
    assert flagged == ["t-005", "t-006", "t-010"]      # A, B, and F (unreadable point: fail closed)
    # a folder stem works as a source key too; the closed C and recorded E are never flagged
    assert fingerprint.affected(conn, RUN, "shared:%s" % os.path.realpath(world.shared),
                                since_event=since) == ["t-005", "t-006", "t-010"]
    # a change seen before C closed flags C as well
    c_close_id = conn.execute("SELECT MAX(id) FROM events WHERE ref='t-007'").fetchone()[0]
    assert "t-007" in fingerprint.affected(conn, RUN, "tool:mytool", since_event=c_close_id - 1)
    assert "t-007" not in fingerprint.affected(conn, RUN, "tool:mytool", since_event=c_close_id)
    # negative: no tampered source, no key, nothing flagged but the fail-closed F
    assert fingerprint.affected(conn, RUN, "tool:not-declared", since_event=since) == ["t-010"]


# ================================================================ the allowlist check (FR-015, EC-049)

def test_allowlist_match_calls_runbook_path_admitted_when_called(monkeypatch):
    seen = []

    def spy(path, rules):
        seen.append((path, rules))
        return "src/" if path.startswith("src/") else None
    monkeypatch.setattr(runbook, "path_admitted", spy, raising=False)
    assert fingerprint.allowlist_match("src/a.py", ["src/"]) == "src/"
    assert fingerprint.allowlist_match("docs/a.md", ("src/",)) is None
    assert seen == [("src/a.py", ("src/",)), ("docs/a.md", ("src/",))]


def test_the_allowlist_is_strict(impl):
    rules = ("src/", "Makefile", "cfg/*.toml")
    assert fingerprint.allowlist_match("src/a.py", rules) == "src/"
    assert fingerprint.allowlist_match("src/deep/b.py", rules) == "src/"
    assert fingerprint.allowlist_match("Makefile", rules) == "Makefile"
    assert fingerprint.allowlist_match("cfg/a.toml", rules) == "cfg/*.toml"
    # a folder rule admits the folder itself at the root (runbook-format-3.md section 2.2 reads it
    # with barrier.path_is_protected's grammar, alpaca/barrier.py:143)
    assert fingerprint.allowlist_match("src", rules) == "src/"
    for outside in ("SRC/a.py", "lib/src/a.py", "sub/Makefile", "makefile", "cfg/x/a.toml",
                    "srcx/a.py", "lib/src"):
        assert fingerprint.allowlist_match(outside, rules) is None, outside


def test_editable_edits_pass_the_path_check(world):
    write(os.path.join(world.worktree.path, "src", "app.py"), "print('v1')\n")
    write(os.path.join(world.worktree.path, "src", "pkg", "new.py"), "x = 1\n")
    write(os.path.join(world.worktree.path, "build", "out.bin"), "ignored output\n")
    verdict = world.check()
    assert verdict.ok, verdict
    assert verdict.point == "after-implementer"


def test_a_change_outside_the_allowlist_breaks_the_attempt_with_the_paths(world):
    wt = world.worktree.path
    write(os.path.join(wt, "src", "app.py"), "print('v1')\n")          # allowed
    write(os.path.join(wt, "README.md"), "edited\n")                    # tracked, outside
    write(os.path.join(wt, "SRC", "evil.py"), "x = 1\n")                # a case variant
    write(os.path.join(wt, "lib", "src", "x.py"), "x = 1\n")            # a nested folder src/
    os.unlink(os.path.join(wt, "pack", "runbook.yaml"))                 # a deletion
    write(os.path.join(wt, "tests", "test_app.py"), "def test_app(): pass\n")   # protected
    verdict = world.check()
    assert not verdict.ok
    assert verdict.outside == ("README.md", "SRC/evil.py", "lib/src/x.py", "pack/runbook.yaml",
                               "tests/test_app.py")
    assert verdict.protected == ("tests/test_app.py",)
    assert verdict.nested == () and verdict.errors == ()


def test_check_paths_returns_changed(world):
    """in-session-runtime.md section 5.3: `changed` is the set the check already computed, so the
    judge can tell a changed path from an unattributed one; no decision reads it."""
    wt = world.worktree.path
    clean = world.check()
    assert clean.ok and clean.changed == ()
    write(os.path.join(wt, "src", "app.py"), "print('v1')\n")          # an edited file
    write(os.path.join(wt, "src", "pkg", "planted.py"), "x = 1\n")    # a planted file
    verdict = world.check()
    assert verdict.ok and verdict.outside == () and verdict.protected == ()
    assert set(verdict.changed) == {"src/app.py", "src/pkg/planted.py"}
    # the existing fields are what they were: a path outside the list still names itself in both
    write(os.path.join(wt, "README.md"), "edited\n")
    bad = world.check()
    assert not bad.ok and bad.outside == ("README.md",) and "README.md" in bad.changed


def test_a_protected_path_inside_the_allowlist_still_breaks(world):
    write(os.path.join(world.worktree.path, "src", "tests", "conftest.py"), "x = 1\n")
    verdict = fingerprint.check_paths(world.clone, world.worktree, forked_from=world.commit,
                                      editable=("src/",), protected=("tests/",))
    # barrier.path_is_protected hits a one-folder rule at any depth
    assert not verdict.ok and verdict.protected == ("src/tests/conftest.py",)
    assert verdict.outside == ()


def _plant_hostile_git(world):
    """What the implementer can write: its admin folder and the worktree. Returns the marker a
    planted program would create."""
    wt, admin = world.worktree.path, world.worktree.admin_dir
    mark = str(world.scratch / "PLANTED-PROGRAM-RAN")
    evil = os.path.join(wt, "evil.sh")
    write(evil, "#!/bin/sh\necho ran >> %s\ncat\n" % mark, 0o755)
    text = ('[core]\n\tfsmonitor = %s\n[filter "x"]\n\tclean = %s\n\tsmudge = %s\n'
            '[diff "x"]\n\ttextconv = %s\n' % (evil, evil, evil, evil))
    write(os.path.join(admin, "config.worktree"), text)
    write(os.path.join(admin, "config"), text)
    write(os.path.join(wt, ".gitattributes"), "* filter=x diff=x\n")
    return mark


def test_host_git_misses_nothing_an_agents_git_hides_and_runs_nothing_planted(world):
    wt = world.worktree.path
    # an agent commit of a non-editable edit moves the admin HEAD and index
    write(os.path.join(wt, "README.md"), "committed by the agent\n")
    world.agent("commit", "-qam", "agent commit")
    # a skip-worktree mark hides another edit from the admin index
    write(os.path.join(wt, "pack", "runbook.yaml"), "runbook: 99\n")
    world.agent("update-index", "--skip-worktree", "pack/runbook.yaml")
    # a force-added ignored output in the admin index
    write(os.path.join(wt, "build", "cache.bin"), "planted\n")
    world.agent("add", "-f", "build/cache.bin")
    mark = _plant_hostile_git(world)
    # a hostile commondir and a corrupted admin HEAD (the root .git file is the next test's)
    write(os.path.join(world.worktree.admin_dir, "commondir"), "/nonexistent\n")
    write(os.path.join(world.worktree.admin_dir, "HEAD"), "garbage\n")
    verdict = world.check()
    assert not os.path.exists(mark), "a planted program ran"
    assert not verdict.ok and not verdict.errors, verdict
    # the exact set: nothing hidden, and no unchanged path reported (the fresh
    # `git read-tree <forked_from>` itself is guarded by test_editable_edits_pass_the_path_check)
    assert verdict.outside == (".gitattributes", "README.md", "evil.sh", "pack/runbook.yaml")
    assert "build/cache.bin" not in verdict.outside       # ignored: the purge removes it
    assert "src/app.py" not in verdict.outside and "eval/config.yaml" not in verdict.outside


def test_a_rewritten_root_git_file_never_passes_and_runs_nothing_planted(world):
    """The agent rewrites the worktree's own .git file as well. architecture.md:451-453 does not
    say whether nested_git lists the root .git then: t-091's walk does (the verdict is BROKEN
    through `nested` and no git runs), the reference walk does not (host git runs with the
    engine's own git folder and lists the edit). Either way the attempt is not ok and nothing
    planted runs."""
    wt = world.worktree.path
    write(os.path.join(wt, "README.md"), "edited outside the allowlist\n")
    mark = _plant_hostile_git(world)
    write(os.path.join(wt, ".git"), "gitdir: /nonexistent/elsewhere\n")
    write(os.path.join(world.worktree.admin_dir, "commondir"), "/nonexistent\n")
    write(os.path.join(world.worktree.admin_dir, "HEAD"), "garbage\n")
    verdict = world.check()
    assert not os.path.exists(mark), "a planted program ran"
    assert not verdict.ok and not verdict.errors, verdict
    assert ".git" in verdict.nested or "README.md" in verdict.outside, verdict


def test_the_plain_git_status_would_have_missed_the_committed_edit(world):
    # control for the test above: the agent's own view shows nothing to flag
    write(os.path.join(world.worktree.path, "README.md"), "committed by the agent\n")
    world.agent("commit", "-qam", "agent commit")
    assert world.agent("status", "--porcelain") == ""
    assert "README.md" in world.check().outside


@pytest.mark.parametrize("kind", ["folder", "symlink"])
def test_a_nested_repository_is_flagged_without_running_git(world, monkeypatch, kind):
    wt = world.worktree.path
    mark = str(world.scratch / "NESTED-FILTER-RAN")
    write(os.path.join(wt, "src", "sub", "code.py"), "x = 1\n")
    if kind == "folder":
        git(os.path.join(wt, "src", "sub"), "init", "-q")
        evil = os.path.join(wt, "src", "sub", "evil.sh")
        write(evil, "#!/bin/sh\necho ran >> %s\ncat\n" % mark, 0o755)
        with open(os.path.join(wt, "src", "sub", ".git", "config"), "a") as fh:
            fh.write('[filter "x"]\n\tclean = %s\n' % evil)
        write(os.path.join(wt, "src", "sub", ".gitattributes"), "* filter=x\n")
    else:
        os.symlink(os.path.join(world.clone.path, ".git"), os.path.join(wt, "src", "sub", ".git"))
    calls = []
    real = fingerprint._git
    monkeypatch.setattr(fingerprint, "_git", lambda *a, **k: calls.append(a) or real(*a, **k))
    verdict = world.check()
    assert not verdict.ok and verdict.nested == ("src/sub/.git",)
    assert calls == [], "git ran against a tree that holds a nested repository"
    assert not os.path.exists(mark)
    # positive control: without the nested repo the same edit passes and git runs
    target = os.path.join(wt, "src", "sub", ".git")
    if os.path.islink(target):
        os.unlink(target)
    else:
        shutil.rmtree(target)
    assert world.check().ok and calls


def test_a_failing_git_or_walk_fails_closed(world, monkeypatch):
    bad = fingerprint.check_paths(world.clone, world.worktree, forked_from="f" * 40,
                                  editable=EDITABLE, protected=PROTECTED)
    assert not bad.ok and bad.errors and bad.errors[0].startswith("host git")

    def boom(wt):
        raise OSError("walk failed")
    monkeypatch.setattr(isolation, "nested_git", boom)
    walked = world.check()
    assert not walked.ok and walked.errors == ("nested-git walk: walk failed",)
    no_index = WorktreeInfo(path=world.worktree.path, label=TASK, commit=world.commit,
                            admin_dir=None, index=None, host_git=None)
    monkeypatch.setattr(isolation, "nested_git", lambda wt: ())
    assert not fingerprint.check_paths(world.clone, no_index, forked_from=world.commit,
                                       editable=EDITABLE, protected=PROTECTED).ok


# ================================================================ the captured diff (row 23a)

def _capture(paths_=(), gitlinks=()):
    return DiffCapture(forked_from="a" * 40, head="b" * 40, tree="c" * 40,
                       path="/nonexistent/attempt.diff", sha256="0" * 64, bytes=1,
                       paths=tuple(paths_), stamp="s", message="m", gitlinks=tuple(gitlinks))


def test_check_diff_is_pure_and_checks_every_captured_path(impl, monkeypatch):
    monkeypatch.setattr(fingerprint, "_git", lambda *a, **k: pytest.fail("check_diff ran git"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("check_diff ran a program"))
    ok = fingerprint.check_diff(_capture(["src/a.py", "src/b/c.py"]), editable=EDITABLE,
                                protected=PROTECTED)
    assert ok.ok and ok.point == "capture"
    bad = fingerprint.check_diff(_capture(["src/a.py", "README.md", "tests/t.py", "Src/x.py"]),
                                 editable=EDITABLE, protected=PROTECTED)
    assert not bad.ok
    assert bad.outside == ("README.md", "Src/x.py", "tests/t.py")
    assert bad.protected == ("tests/t.py",)
    # a path outside the allowlist alone, with no protected path and no gitlink, is BROKEN too
    only = fingerprint.check_diff(_capture(["src/a.py", "README.md"]), editable=EDITABLE,
                                  protected=PROTECTED)
    assert not only.ok and only.outside == ("README.md",)
    assert only.protected == () and only.nested == ()


def test_check_diff_breaks_on_any_gitlink_whatever_the_allowlist(impl):
    verdict = fingerprint.check_diff(_capture(["src/sub"], gitlinks=["src/sub"]),
                                     editable=EDITABLE, protected=PROTECTED)
    assert not verdict.ok and verdict.nested == ("src/sub",) and verdict.outside == ()


def test_check_diff_catches_a_path_check_paths_never_saw(world):
    # the capture's own paths are judged again, so a path the earlier check missed is caught
    capture = _capture(["src/app.py", "eval/config.yaml"])
    verdict = fingerprint.check_diff(capture, editable=EDITABLE, protected=PROTECTED)
    assert verdict.outside == ("eval/config.yaml",) and verdict.protected == ("eval/config.yaml",)


# ================================================================ take_attempt (the audit's pair)

def _attempt_record(world, *, worktree_events=("create",), relative=False):
    conn = db.connect(world.root)
    db.append_event(conn, session="research-" + RUN, actor="research-runner", kind="task-add",
                    op="op-001", ref=TASK, data={"title": "attempt", "research": {
                        "run": RUN, "role": "attempt", "key": "rr-001:r1:s0"}})
    events.emit(conn, events.ROUND_OPEN, run=RUN, ref=RUN, op="op-001",
                session="research-" + RUN,
                data={"round": 1, "repair_task": None, "champion_commit": world.commit,
                      "keys": ["rr-001:r1:s0"], "tasks": [TASK]})
    wt = world.worktree

    def shown(location):
        # isolation records the paths of a worktree relative to the project root
        return os.path.relpath(location, world.root) if relative and location else location

    for action in worktree_events:
        events.emit(conn, events.WORKTREE, run=RUN, ref=TASK, op="op-001",
                    session="research-" + RUN,
                    data={"action": action, "path": shown(wt.path),
                          "admin_dir": shown(wt.admin_dir), "index": shown(wt.index),
                          "host_git": shown(wt.host_git),
                          "private_objects": shown(wt.private_objects), "commit": wt.commit})
    return conn


@pytest.fixture
def audit_world(world, monkeypatch):
    from alpaca.research import contract as contract_mod
    monkeypatch.setattr(contract_mod, "read_frozen",
                        lambda root, run: world.contract if run == RUN else None)
    monkeypatch.setattr(isolation, "open_clone",
                        lambda root, run, line="champion": world.clone if line == "champion"
                        else None)
    monkeypatch.setattr(fingerprint.procs, "gate_path", lambda tools_real=(): world.path_env)
    return world


def test_take_attempt_brackets_the_audit_and_sees_an_edit(audit_world):
    world = audit_world
    conn = _attempt_record(world)
    before = fingerprint.take_attempt(conn, world.root, TASK, "audit-before")
    same = fingerprint.take_attempt(conn, world.root, TASK, "audit-after")
    assert not before.errors, before.errors
    assert fingerprint.compare(before, same).ok
    assert before.path.endswith(os.path.join("fingerprints", "audit-before.json"))
    assert before.entries == world.take("after-audit").entries   # the lifecycle's arguments
    write(os.path.join(world.worktree.path, "tests", "test_app.py"), "def test_app(): pass\n")
    after = fingerprint.take_attempt(conn, world.root, TASK, "audit-after")
    verdict = fingerprint.compare(before, after)
    assert not verdict.ok and verdict.changed == ("contract:tests/test_app.py",)
    assert after.path.endswith(os.path.join("fingerprints", "audit-after.json"))


def test_take_attempt_reads_a_worktree_recorded_relative_to_the_root_from_any_cwd(
        audit_world, monkeypatch):
    # check round s1 (design): the lifecycle's own worktree() makes the recorded paths absolute
    # against the project root, and take_attempt did not, so with the paths as isolation records
    # them every audit of a real run read a folder under the cwd that is not there and was voided
    world = audit_world
    conn = _attempt_record(world, relative=True)
    monkeypatch.chdir("/")
    point = fingerprint.take_attempt(conn, world.root, TASK, "audit-before")
    assert not point.errors, point.errors
    assert point.entries == world.take("after-audit").entries
    assert fingerprint.compare(point, fingerprint.take_attempt(
        conn, world.root, TASK, "audit-after")).ok


def test_take_attempt_takes_only_the_audit_points(audit_world):
    conn = _attempt_record(audit_world)
    for point in fingerprint.POINTS:
        with pytest.raises(ValueError):
            fingerprint.take_attempt(conn, audit_world.root, TASK, point)
    folder = os.path.join(paths.attempt_dir(audit_world.root, RUN, TASK), "fingerprints")
    assert not os.path.exists(folder) or not os.listdir(folder)


@pytest.mark.parametrize("events_", [(), ("create", "destroy"), ("create", "create-error")])
def test_take_attempt_without_a_live_worktree_fails_closed(audit_world, events_):
    world = audit_world
    conn = _attempt_record(world, worktree_events=tuple(e for e in events_ if e != "create-error"))
    if "create-error" in events_:
        events.emit(conn, events.WORKTREE, run=RUN, ref=TASK, op="op-001",
                    session="research-" + RUN, reason="ISOLATION-PATH-TAKEN",
                    data={"action": "create", "path": "/x", "error": "path taken"})
        # the first create is live; a later failed create does not replace it
        point = fingerprint.take_attempt(conn, world.root, TASK, "audit-before")
        assert not point.errors
        return
    point = fingerprint.take_attempt(conn, world.root, TASK, "audit-before")
    assert point.errors and point.entries["contract"].startswith("error: ")
    assert not fingerprint.compare(point, point).ok


def test_take_attempt_without_a_clone_champion_or_contract_fails_closed(audit_world, monkeypatch):
    world = audit_world
    conn = _attempt_record(world)
    monkeypatch.setattr(isolation, "open_clone", lambda root, run, line="champion": None)
    shutil.rmtree(world.champion.path)
    point = fingerprint.take_attempt(conn, world.root, TASK, "audit-before")
    keys = {e.split(":")[0] for e in point.errors}
    assert {"clone", "champion"} <= keys
    assert not fingerprint.compare(point, point).ok
    from alpaca.research import contract as contract_mod

    def gone(root, run):
        raise FileNotFoundError("contract.json")
    monkeypatch.setattr(contract_mod, "read_frozen", gone)
    point = fingerprint.take_attempt(conn, world.root, TASK, "audit-after")
    assert "frozen" in {e.split(":")[0] for e in point.errors}
    assert not fingerprint.compare(point, point).ok


def test_take_attempt_refuses_a_task_that_is_not_an_attempt(audit_world):
    conn = db.connect(audit_world.root)
    with pytest.raises(ValueError):
        fingerprint.take_attempt(conn, audit_world.root, "t-404", "audit-before")


# ================================================================ check round 1 (t-092 r1 findings)

def _pair_after(world, change, *, first="after-implementer", second="after-eval"):
    """Apply change, then take two points wholly after it: a source that went missing must not
    let the pair agree and pass."""
    change()
    a = world.take(first)
    b = world.take(second)
    return a, b, fingerprint.compare(a, b)


def test_a_vanished_attempt_worktree_is_an_error_not_absent_paths(world):
    """Fail case: the tree the contract paths are read in is gone (finding 1 of check-contract-r1)."""
    clean = world.take("after-implementer")
    assert not clean.errors and fingerprint.compare(clean, clean).ok     # positive control
    a, b, verdict = _pair_after(world, lambda: shutil.rmtree(world.worktree.path))
    assert b.errors and b.entries["contract"].startswith("error: ")
    assert "missing or not a folder" in b.entries["contract"]
    assert not any(v == "absent" for k, v in b.entries.items() if k.startswith("contract:"))
    assert not verdict.ok and verdict.errors
    assert not fingerprint.compare(b, b).ok


def test_a_worktree_path_that_does_not_exist_or_is_a_link_is_an_error(world):
    ghost = WorktreeInfo(path=str(world.scratch / "never-made"), label=TASK, commit=world.commit,
                         admin_dir=None, index=None, host_git=None)
    point = world.take("after-implementer", worktree=ghost)
    assert point.entries["contract"].startswith("error: ") and not fingerprint.compare(
        point, point).ok
    link = str(world.scratch / "tree-link")
    os.symlink(world.worktree.path, link)
    linked = WorktreeInfo(path=link, label=TASK, commit=world.commit, admin_dir=None, index=None,
                          host_git=None)
    point = world.take("after-implementer", worktree=linked)
    assert point.entries["contract"].startswith("error: ")


def test_a_file_planted_under_a_protected_folder_is_caught(world):
    """Finding 2 of check-design-r1: the contract paths are the protected rules walked in the
    tree, not only the frozen files."""
    before = world.take("after-implementer")
    write(os.path.join(world.worktree.path, "tests", "conftest.py"), "import os\n")
    verdict = fingerprint.compare(before, world.take("after-eval"))
    assert not verdict.ok and verdict.changed == ("contract:tests/conftest.py",)


def test_a_planted_file_at_any_depth_or_case_that_a_rule_hits_is_caught(world):
    contract = make_contract(tools=("mytool",), tools_real=world.contract.tools_real,
                             shared=(world.shared,), read_real=(world.data,),
                             hidden=("secret.txt",))
    before = world.take("after-implementer", contract=contract)
    write(os.path.join(world.worktree.path, "src", "tests", "test_deep.py"), "x = 1\n")
    write(os.path.join(world.worktree.path, "TESTS", "x.py"), "x = 1\n")
    write(os.path.join(world.worktree.path, "src", "secret.txt"), "planted\n")
    verdict = fingerprint.compare(before, world.take("after-eval", contract=contract))
    assert not verdict.ok
    assert verdict.changed == ("contract:TESTS/x.py", "contract:src/secret.txt",
                               "contract:src/tests/test_deep.py")


def test_the_walk_keys_run_on_from_the_champion_to_the_worktree(world):
    """Positive: an untouched tree gives the same contract keys in the champion checkout (open)
    and in the attempt worktree (after-implementer)."""
    opened = world.take("open", worktree=None)
    impl = world.take("after-implementer")
    keys = sorted(k for k in impl.entries if k.startswith("contract:"))
    assert keys == ["contract:eval/config.yaml", "contract:tests/test_app.py"]
    assert keys == sorted(k for k in opened.entries if k.startswith("contract:"))
    assert fingerprint.compare(opened, impl).ok


def test_python_caches_and_declared_outputs_are_left_to_the_purge(world):
    """A test run beside a protected test writes caches; a declared eval output under a protected
    folder is the post-eval check's. Neither breaks the attempt; an undeclared file does."""
    contract = make_contract(tools=("mytool",), tools_real=world.contract.tools_real,
                             shared=(world.shared,), read_real=(world.data,),
                             outputs=("eval/results/",), keep=("tests/.cache/",))
    before = world.take("after-implementer", contract=contract)
    wt = world.worktree.path
    write(os.path.join(wt, "tests", "__pycache__", "test_app.cpython-312.pyc"), "cache\n")
    write(os.path.join(wt, "tests", ".pytest_cache", "v", "lastfailed"), "{}\n")
    write(os.path.join(wt, "tests", "old.pyc"), "cache\n")
    write(os.path.join(wt, "eval", "results", "run.json"), "{}\n")
    write(os.path.join(wt, "tests", ".cache", "kept.bin"), "kept\n")
    assert fingerprint.compare(before, world.take("after-eval", contract=contract)).ok
    write(os.path.join(wt, "eval", "undeclared.json"), "{}\n")
    verdict = fingerprint.compare(before, world.take("after-audit", contract=contract))
    assert not verdict.ok and verdict.changed == ("contract:eval/undeclared.json",)


def _age_main(world):
    """Old files whose stat data the index holds (a non-racy index, as in a real checkout)."""
    old = 1_600_000_000
    for rel in FILES:
        os.utime(os.path.join(world.root, rel), (old, old))
    git(world.root, "update-index", "--refresh")


def _same_size_edit(path, text):
    st = os.stat(path)
    with open(path, encoding="utf-8") as fh:
        assert len(fh.read()) == len(text)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


EVIL_TEST = "def test_app():\n    assert 1==1\n"          # the size of FILES["tests/test_app.py"]


def _main_edit_assume_unchanged(world):
    write(os.path.join(world.root, "tests", "test_app.py"), "def test_app():\n    pass\n")
    git(world.root, "update-index", "--assume-unchanged", "tests/test_app.py")


def _main_edit_skip_worktree(world):
    write(os.path.join(world.root, "eval", "config.yaml"), "threshold: 0\n")
    git(world.root, "update-index", "--skip-worktree", "eval/config.yaml")


def _main_edit_checkstat_minimal(world):
    git(world.root, "config", "core.trustctime", "false")
    git(world.root, "config", "core.checkStat", "minimal")
    _same_size_edit(os.path.join(world.root, "tests", "test_app.py"), EVIL_TEST)


def _main_edit_after_refresh_same_second(world):
    p = os.path.join(world.root, "tests", "test_app.py")
    st = os.stat(p)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))         # moves the ctime only
    git(world.root, "update-index", "--refresh")             # the index takes the new ctime
    _same_size_edit(p, EVIL_TEST)                            # within the same second


def _main_new_file_in_info_exclude(world):
    write(os.path.join(world.root, "tests", "conftest.py"), "import os\n")
    with open(os.path.join(world.root, ".git", "info", "exclude"), "a", encoding="utf-8") as fh:
        fh.write("tests/conftest.py\n")


def _main_new_file_staged(world):
    # a staged new file is in the checkout's index, so an index-based "others" list skips it
    write(os.path.join(world.root, "tests", "staged.py"), "import os\n")
    git(world.root, "add", "tests/staged.py")


MAIN_HIDES = {
    "assume-unchanged": (_main_edit_assume_unchanged, "main:tests/test_app.py"),
    "staged new file": (_main_new_file_staged, "main:tests/staged.py"),
    "skip-worktree": (_main_edit_skip_worktree, "main:eval/config.yaml"),
    "checkStat minimal": (_main_edit_checkstat_minimal, "main:tests/test_app.py"),
    "refresh then same-second edit": (_main_edit_after_refresh_same_second,
                                      "main:tests/test_app.py"),
    "info/exclude": (_main_new_file_in_info_exclude, "main:tests/conftest.py"),
}


@pytest.mark.parametrize("case", sorted(MAIN_HIDES))
def test_the_main_checkouts_own_index_and_config_hide_no_edit(world, case):
    """Finding 3 of check-adversarial-r1: the checkout's index, stat cache, config and
    info/exclude are never trusted."""
    _age_main(world)
    time.sleep(1.1)                     # the index is not racy: git would trust its stat data
    before = world.take("after-implementer")
    hide, key = MAIN_HIDES[case]
    hide(world)
    verdict = fingerprint.compare(before, world.take("after-eval"))
    assert not verdict.ok and key in verdict.changed, (case, verdict)


def test_a_main_checkout_file_that_gitignore_ignores_is_not_a_source(world):
    before = world.take("after-implementer")
    write(os.path.join(world.root, "src", "__pycache__", "app.cpython-312.pyc"), "cache\n")
    write(os.path.join(world.root, "build", "out.bin"), "out\n")
    assert fingerprint.compare(before, world.take("after-eval")).ok


@pytest.mark.parametrize("how", ["assume-unchanged", "info/exclude"])
def test_a_shared_checkouts_own_index_hides_no_edit(world, how):
    before = world.take("open", worktree=None)
    if how == "assume-unchanged":
        write(os.path.join(world.shared, "lib.txt"), "shared EVIL\n")
        git(world.shared, "update-index", "--assume-unchanged", "lib.txt")
        keys = ("shared:%s/lib.txt" % os.path.realpath(world.shared),)
    else:
        write(os.path.join(world.shared, "planted.txt"), "x\n")
        with open(os.path.join(world.shared, ".git", "info", "exclude"), "a",
                  encoding="utf-8") as fh:
            fh.write("planted.txt\n")
        # the new file, and the folder it was planted in (its ctime moved)
        keys = ("shared:%s/." % os.path.realpath(world.shared),
                "shared:%s/planted.txt" % os.path.realpath(world.shared))
    verdict = fingerprint.compare(before, world.take("after-proposer", worktree=None))
    assert not verdict.ok and verdict.changed == keys


def _linked(world, target, name):
    link = str(world.scratch / name)
    os.symlink(target, link)
    return link


def test_a_shared_location_declared_through_a_link_is_read_behind_it(world):
    """Finding 4 of check-adversarial-r1: the location is resolved at every point."""
    link = _linked(world, world.shared, "shared-link")
    contract = make_contract(tools=("mytool",), tools_real=world.contract.tools_real,
                             shared=(link,), read_real=(world.data,))
    before = world.take("open", worktree=None, contract=contract)
    real = os.path.realpath(world.shared)
    assert before.entries["shared:" + link] == "resolves-to " + real
    assert "shared:%s/lib.txt" % real in before.entries
    write(os.path.join(world.shared, "lib.txt"), "shared EVIL v2\n")
    git(world.shared, "commit", "-qam", "evil")
    verdict = fingerprint.compare(before, world.take("after-proposer", worktree=None,
                                                     contract=contract))
    assert not verdict.ok and verdict.changed == ("shared:%s/lib.txt" % real,)


def test_a_link_retargeted_to_a_copy_is_a_change(world):
    copy = str(world.scratch / "shared-copy")
    shutil.copytree(world.shared, copy, symlinks=True)
    link = _linked(world, world.shared, "shared-link")
    contract = make_contract(tools=("mytool",), tools_real=world.contract.tools_real,
                             shared=(link,), read_real=(world.data,))
    before = world.take("open", worktree=None, contract=contract)
    os.unlink(link)
    os.symlink(copy, link)
    verdict = fingerprint.compare(before, world.take("after-proposer", worktree=None,
                                                     contract=contract))
    assert not verdict.ok and "shared:" + link in verdict.changed


@pytest.mark.parametrize("kind", ["shared", "read"])
def test_a_linked_location_whose_target_is_deleted_fails_closed(world, kind):
    target = world.shared if kind == "shared" else world.data
    link = _linked(world, target, kind + "-link")
    if kind == "shared":
        contract = make_contract(tools=("mytool",), tools_real=world.contract.tools_real,
                                 shared=(link,), read_real=(world.data,))
    else:
        contract = make_contract(tools=("mytool",), tools_real=world.contract.tools_real,
                                 shared=(world.shared,), read_paths=(link,), read_real=())
    before = world.take("open", worktree=None, contract=contract)
    assert not before.errors
    shutil.rmtree(target)
    after = world.take("after-proposer", worktree=None, contract=contract)
    assert after.entries["%s:%s" % (kind, link)].startswith("error: ")
    assert after.errors and not fingerprint.compare(before, after).ok
    assert not fingerprint.compare(after, after).ok


def _empty_frozen(world):
    for name in os.listdir(world.frozen):
        target = os.path.join(world.frozen, name)
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target)
        else:
            os.unlink(target)
    os.makedirs(os.path.join(world.frozen, "files"))


FROZEN_WIPES = {
    "contract folder emptied": _empty_frozen,
    "files folder emptied": lambda w: (shutil.rmtree(os.path.join(w.frozen, "files")),
                                       os.makedirs(os.path.join(w.frozen, "files"))),
    "contract.json gone": lambda w: os.unlink(os.path.join(w.frozen, "contract.json")),
    "files.json gone": lambda w: os.unlink(os.path.join(w.frozen, "files.json")),
    "files.json not an object": lambda w: write(os.path.join(w.frozen, "files.json"), "[]\n"),
    "one listed file gone": lambda w: os.unlink(os.path.join(w.frozen, "files", "eval",
                                                             "config.yaml")),
}


@pytest.mark.parametrize("case", sorted(FROZEN_WIPES))
def test_a_pair_taken_after_the_frozen_contract_was_wiped_is_broken(world, case):
    """Finding 5 of check-adversarial-r1: contract.json, files.json and the files it lists are
    required, so two points taken after a wipe never agree and pass."""
    _a, b, verdict = _pair_after(world, lambda: FROZEN_WIPES[case](world))
    assert b.errors and b.entries["frozen"].startswith("error: "), case
    assert not verdict.ok and verdict.errors, case


def test_a_late_tamper_row_still_flags_the_slot_whose_window_is_open(world):
    """Finding 6 of check-adversarial-r1: B's window opens at its task-add, so B is flagged even
    when its open point's research-tamper row lands after affected() runs."""
    conn = db.connect(world.root)
    for task in ("t-005", "t-006"):
        db.append_event(conn, session="research-" + RUN, actor="research-runner", kind="task-add",
                        op="op-001", ref=task, data={"title": "attempt", "research": {
                            "run": RUN, "role": "attempt", "key": "k-" + task}})
    db.append_event(conn, session="research-" + RUN, actor="research-runner", kind="task-add",
                    op="op-001", ref="t-099", data={"title": "report", "research": {
                        "run": RUN, "role": "report", "key": "k-report"}})
    db.append_event(conn, session="research-rr-002", actor="research-runner", kind="task-add",
                    op="op-002", ref="t-777", data={"title": "attempt", "research": {
                        "run": "rr-002", "role": "attempt", "key": "k-other"}})
    slot_b = world.attempt_tree("t-006")
    b_open = world.take("open", task="t-006", worktree=None)        # saved, no row yet
    a_prop = world.take("after-proposer", worktree=None)
    since = _record_point(conn, a_prop)
    tool = os.path.join(world.bin2, "mytool")
    with open(tool, encoding="utf-8") as fh:
        orig = fh.read()
    write(tool, "#!/bin/sh\necho planted\n", 0o755)
    assert not fingerprint.compare(a_prop, world.take("after-implementer")).ok
    write(tool, orig, 0o755)                                        # A puts it back
    flagged = fingerprint.affected(conn, RUN, "tool:mytool", since_event=since)
    assert flagged == ["t-005", "t-006"]         # not the report task, not another run's slot
    _record_point(conn, b_open)                                     # B's row lands late
    # and once B's row is there, B is still flagged: its saved point holds the key
    assert fingerprint.affected(conn, RUN, "tool:mytool", since_event=since) == ["t-005", "t-006"]
    # B's own compare also breaks: the tool's ctime moved although its bytes are back
    vb = fingerprint.compare(b_open, world.take("after-implementer", task="t-006",
                                                worktree=slot_b))
    assert not vb.ok and vb.changed == ("tool:mytool",)


def test_a_shadow_added_and_removed_between_points_is_caught(world):
    before = world.take("after-implementer")
    shadow = os.path.join(world.bin1, "mytool")
    write(shadow, "#!/bin/sh\necho evil\n", 0o755)          # a gate would run it here
    os.unlink(shadow)
    verdict = fingerprint.compare(before, world.take("after-eval"))
    assert not verdict.ok and verdict.changed == ("tool-path:" + world.bin1,)


def test_a_tool_written_and_restored_between_points_is_caught(world):
    tool = os.path.join(world.bin2, "mytool")
    before = world.take("after-implementer")
    st = os.stat(tool)
    with open(tool, encoding="utf-8") as fh:
        orig = fh.read()
    write(tool, "#!/bin/sh\necho evil!\n", 0o755)
    write(tool, orig, 0o755)
    os.utime(tool, ns=(st.st_atime_ns, st.st_mtime_ns))
    verdict = fingerprint.compare(before, world.take("after-eval"))
    assert not verdict.ok and verdict.changed == ("tool:mytool",)


def test_the_path_folders_are_keyed_only_when_tools_are_declared(world):
    point = world.take("open", worktree=None, contract=make_contract(
        shared=(world.shared,), read_real=(world.data,)))
    assert not any(k.startswith(("tool:", "tool-path:")) for k in point.entries)
    full = world.take("open", worktree=None)
    assert {"tool-path:" + world.bin1, "tool-path:" + world.bin2} <= set(full.entries)


def test_take_attempt_with_its_worktree_folder_gone_fails_closed(audit_world):
    """Finding 1 of check-contract-r1: a live research-worktree event whose folder is gone."""
    world = audit_world
    conn = _attempt_record(world)
    clean = fingerprint.take_attempt(conn, world.root, TASK, "audit-before")
    assert not clean.errors                                         # positive control
    shutil.rmtree(world.worktree.path)
    before = fingerprint.take_attempt(conn, world.root, TASK, "audit-before")
    after = fingerprint.take_attempt(conn, world.root, TASK, "audit-after")
    assert after.errors and after.entries["contract"].startswith("error: ")
    assert "missing or not a folder" in after.entries["contract"]
    assert not fingerprint.compare(before, after).ok
    assert not fingerprint.compare(after, after).ok


# ================================================================ check round 2 (t-092 r2 findings)

def _restore(path, text, st):
    """Put the bytes, the mode and the mtime of path back as st had them: only the ctime (which no
    unprivileged process can set) and, for a new file, the folder's ctime still show the write."""
    write(path, text)
    os.chmod(path, st.st_mode & 0o7777)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


def test_a_frozen_file_written_and_restored_between_points_is_caught(world):
    """a3 of check-adversarial-r2: the frozen grader a gate reads is edited during the eval, then
    its bytes, mode and mtime are put back."""
    p = os.path.join(world.frozen, "files", "eval", "config.yaml")
    before = world.take("after-implementer")
    st = os.stat(p)
    write(p, "threshold: 0\n")                      # a gate reading the frozen copy sees this
    _restore(p, FILES["eval/config.yaml"], st)
    assert os.stat(p).st_ctime_ns != st.st_ctime_ns
    after = world.take("after-eval")
    assert not after.errors                        # the bytes match files.json again
    verdict = fingerprint.compare(before, after)
    assert not verdict.ok and verdict.changed == ("frozen:files/eval/config.yaml",)


def test_a_file_planted_in_and_removed_from_the_frozen_folder_is_caught(world):
    before = world.take("after-implementer")
    planted = os.path.join(world.frozen, "files", "tests", "conftest.py")
    write(planted, "import os\n")
    os.unlink(planted)
    verdict = fingerprint.compare(before, world.take("after-eval"))
    assert not verdict.ok and verdict.changed == ("frozen:files/tests/",)


def test_a_shared_file_written_and_restored_between_points_is_caught(world):
    """a5 of check-adversarial-r2."""
    p = os.path.join(world.shared, "lib.txt")
    before = world.take("after-implementer")
    st = os.stat(p)
    write(p, "shared EV\n")
    _restore(p, "shared v1\n", st)
    verdict = fingerprint.compare(before, world.take("after-eval"))
    assert not verdict.ok
    assert verdict.changed == ("shared:%s/lib.txt" % os.path.realpath(world.shared),)


def test_a_file_the_shared_checkouts_gitignore_ignores_is_still_read(world):
    """a5b of check-adversarial-r2: a poisoned bytecode cache under an ignored folder of the
    shared checkout."""
    write(os.path.join(world.shared, ".gitignore"), "__pycache__/\n")
    git(world.shared, "add", "-A")
    git(world.shared, "commit", "-qm", "ignore caches")
    before = world.take("after-implementer")
    real = os.path.realpath(world.shared)
    assert "shared:%s/.gitignore" % real in before.entries
    assert not any(k.startswith("shared:%s/.git/" % real) for k in before.entries)
    write(os.path.join(world.shared, "__pycache__", "lib.cpython-312.pyc"), "poisoned bytecode")
    verdict = fingerprint.compare(before, world.take("after-eval"))
    assert not verdict.ok
    assert verdict.changed == ("shared:%s/." % real, "shared:%s/__pycache__/" % real,
                               "shared:%s/__pycache__/lib.cpython-312.pyc" % real)


def test_a_git_operation_in_the_shared_checkout_changes_no_digest(world):
    """Positive control: the checkout's own .git is not a source, so git writing its index or
    refs there (another session's `git status` or fetch) breaks no attempt."""
    before = world.take("after-implementer")
    git(world.shared, "update-index", "--refresh")
    git(world.shared, "update-ref", "refs/heads/other", git(world.shared, "rev-parse", "HEAD"))
    assert fingerprint.compare(before, world.take("after-eval")).ok


def test_a_file_planted_in_and_removed_from_a_read_path_is_caught(world):
    """a5c of check-adversarial-r2."""
    before = world.take("after-implementer")
    p = os.path.join(world.data, "set", "extra.csv")
    write(p, "evil\n")
    os.unlink(p)
    verdict = fingerprint.compare(before, world.take("after-eval"))
    assert not verdict.ok
    assert verdict.changed == ("read:%s/set/" % os.path.realpath(world.data),)


def test_a_read_path_that_is_one_file_is_stamped(world):
    single = str(world.scratch / "limits.txt")
    write(single, "10\n")
    contract = make_contract(tools=("mytool",), tools_real=world.contract.tools_real,
                             shared=(world.shared,), read_real=(single,))
    before = world.take("after-implementer", contract=contract)
    st = os.stat(single)
    write(single, "99\n")
    _restore(single, "10\n", st)
    verdict = fingerprint.compare(before, world.take("after-eval", contract=contract))
    assert not verdict.ok and verdict.changed == ("read:" + os.path.realpath(single),)


def _drop_from_manifest(world, key):
    mpath = os.path.join(world.frozen, "files.json")
    with open(mpath, encoding="utf-8") as fh:
        manifest = json.load(fh)
    del manifest[key]
    write(mpath, util.canonical_json(manifest))


def test_a_frozen_grader_edited_with_its_manifest_entry_dropped_fails_closed(world):
    """a9c of check-adversarial-r2: every slot that opens after the edit must see an error."""
    write(os.path.join(world.frozen, "files", "eval", "config.yaml"), "threshold: 0\n")
    _drop_from_manifest(world, "files/eval/config.yaml")
    a = world.take("open", worktree=None)
    b = world.take("after-implementer")
    c = world.take("after-eval")
    for point in (a, b, c):
        assert point.errors, point.point
        assert "does not hash to the contract's files_sha256" in point.entries["frozen"]
    assert not fingerprint.compare(a, b).ok and not fingerprint.compare(b, c).ok


def test_an_emptied_manifest_before_the_first_point_fails_closed(world):
    """a9b of check-adversarial-r2: files.json set to {} and files/ deleted before any point."""
    shutil.rmtree(os.path.join(world.frozen, "files"))
    write(os.path.join(world.frozen, "files.json"), "{}")
    a = world.take("open", worktree=None)
    b = world.take("after-proposer", worktree=None)
    assert a.errors and b.errors
    verdict = fingerprint.compare(a, b)
    assert not verdict.ok and verdict.errors


def test_the_manifest_is_checked_in_its_normalized_form_and_needs_a_hash(world):
    # positive: the same manifest in another json layout still hashes to files_sha256
    write(os.path.join(world.frozen, "files.json"), json.dumps(frozen_manifest(), indent=2))
    assert not world.take("open", worktree=None).errors
    # negative: a contract that names no files_sha256 is an error, never a pass
    contract = make_contract(tools=("mytool",), tools_real=world.contract.tools_real,
                             shared=(world.shared,), read_real=(world.data,), files_sha256="")
    point = world.take("open", worktree=None, contract=contract)
    assert point.errors and "names no files_sha256" in point.entries["frozen"]
    assert not fingerprint.compare(point, point).ok


def test_a_concurrent_plant_removed_again_still_flags_the_other_slot(world):
    """a10c of check-adversarial-r2: A plants a new file in the shared checkout; no earlier point
    holds its key, and the file is gone again before B's next point."""
    conn = db.connect(world.root)
    slot_b = world.attempt_tree("t-006")
    for task in ("t-005", "t-006"):
        db.append_event(conn, session="research-" + RUN, actor="research-runner", kind="task-add",
                        op="op-001", ref=task, data={"title": "attempt", "research": {
                            "run": RUN, "role": "attempt", "key": "k-" + task}})
    b_open = world.take("open", task="t-006", worktree=None)
    _record_point(conn, b_open)
    # D (t-008) reads another line: no shared, read or main source in its point
    _add_task(conn, "t-008")
    _record_point(conn, fingerprint._save(world.root, RUN, "t-008", "open",
                                          {"clone:.git/HEAD": "x"}, []))
    a_prop = world.take("after-proposer", worktree=None)
    since = _record_point(conn, a_prop)
    planted = os.path.join(world.shared, "conftest.py")
    write(planted, "import os; os._exit(0)\n")        # B's gates would load it here
    a_impl = world.take("after-implementer")
    real = os.path.realpath(world.shared)
    key = "shared:%s/conftest.py" % real
    va = fingerprint.compare(a_prop, a_impl)
    assert not va.ok and va.changed == ("shared:%s/." % real, key)
    os.unlink(planted)
    assert key not in b_open.entries                   # no earlier point holds the new key
    flagged = fingerprint.affected(conn, RUN, key, since_event=since)
    assert flagged == ["t-005", "t-006"], flagged       # B flagged; D, which never read it, not
    # and B's own pair sees the folder's ctime move although the file is gone
    vb = fingerprint.compare(b_open, world.take("after-eval", task="t-006", worktree=slot_b))
    assert not vb.ok and vb.changed == ("shared:%s/." % real,)


def test_affected_matches_a_new_key_on_the_source_it_belongs_to(world):
    """_key_in: a key no point holds is matched on its source, and only on it."""
    point = world.take("open", worktree=None)
    shared, data = os.path.realpath(world.shared), os.path.realpath(world.data)
    e = point.entries
    for key in ("shared:%s/new.py" % shared, "shared:%s/deep/new.py" % shared,
                "read:%s/set/new.csv" % data, "frozen:files/new.py", "main:tests/new.py",
                "clone:.git/refs/heads/new", "champion:tests/new.py",
                "champion-admin:new", "tool:mytool", "tool-path:" + world.bin1):
        assert fingerprint._key_in(e, key), key
    for key in ("shared:%s-other/new.py" % shared, "shared:/elsewhere/new.py",
                "read:%s/new.csv" % shared, "tool:other", "tool-path:/elsewhere",
                "contract:tests/new.py"):
        assert not fingerprint._key_in(e, key), key
    # a slot whose point shows only the error entry of the location, or reads it through a link
    assert fingerprint._key_in({"shared:/decl": "error: missing"}, "shared:/decl/x.py")
    assert fingerprint._key_in({"shared:/link": "resolves-to /real"}, "shared:/real/x.py")
    assert fingerprint._key_in({"main": "error: x"}, "main:tests/new.py")
    assert not fingerprint._key_in({"clone:.git/HEAD": "x"}, "main:tests/new.py")


# ================================================================ check round 3 (t-092 r3 findings)

BAD_NAME = b"x\xff.py"                     # not valid UTF-8: a lone surrogate in a Python str
BAD_SHOWN = "x\\xff.py"                   # how a verdict lists it (fingerprint._shown)


def _plant_bytes(folder, name=BAD_NAME, data=b"import os\n"):
    target = os.path.join(os.fsencode(folder), name)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as fh:
        fh.write(data)
    return target


def _recordable(verdict):
    """The verdict goes into a research-tamper row as the lifecycle records it (sqlite refuses a
    lone surrogate), and the saved points read back as the same keys."""
    from alpaca.research import types as rtypes
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (data TEXT)")
    conn.execute("INSERT INTO t VALUES (?)", (util.canonical_json(rtypes.to_dict(verdict)),))


def _reads_back(fp):
    with open(fp.path, "rb") as fh:
        raw = fh.read()
    assert all(b < 0x80 for b in raw)               # the point file is pure ASCII
    assert fingerprint._load_point(fp.path).entries == fp.entries


def test_a_name_that_is_not_utf8_in_the_worktree_gives_a_point_and_broken(world):
    """a8 of check-adversarial-r3: the implementer creates tests/x<0xff>.py in its own worktree."""
    before = world.take("after-proposer")
    _plant_bytes(os.path.join(world.worktree.path, "tests"))
    after = world.take("after-implementer")
    _reads_back(after)
    verdict = fingerprint.compare(before, after)
    assert not verdict.ok and verdict.changed == ("contract:tests/" + BAD_SHOWN,)
    _recordable(verdict)
    # the allowlist check sees the same name: outside the allowlist and protected, and recordable
    check = world.check()
    assert not check.ok and check.protected == ("tests/" + BAD_SHOWN,)
    assert check.outside == ("tests/" + BAD_SHOWN,)
    _recordable(check)
    # read back after a restart, the saved pair still compares the same
    again = fingerprint.compare(fingerprint._load_point(before.path),
                                fingerprint._load_point(after.path))
    assert again == verdict


def test_a_name_that_is_not_utf8_in_a_shared_checkout_gives_a_point_and_broken(world):
    """a8b of check-adversarial-r3."""
    before = world.take("after-implementer")
    _plant_bytes(world.shared)
    after = world.take("after-eval")
    _reads_back(after)
    verdict = fingerprint.compare(before, after)
    real = os.path.realpath(world.shared)
    assert not verdict.ok
    assert verdict.changed == ("shared:%s/." % real, "shared:%s/%s" % (real, BAD_SHOWN))
    _recordable(verdict)


def test_a_name_that_is_not_utf8_in_the_champion_or_main_checkout_is_read(world):
    """a12 of check-adversarial-r3: an honest repository with a Latin-1 file name gives points
    that compare clean, and an edit to that file is caught."""
    champ = _plant_bytes(os.path.join(world.champion.path, "src"), b"z\xe9.py")
    main = _plant_bytes(os.path.join(world.root, "tests"), b"z\xe9.py")
    first = world.take("open", worktree=None)
    second = world.take("after-proposer", worktree=None)
    assert not first.errors and not second.errors, (first.errors, second.errors)
    _reads_back(first)
    assert "champion:src/z\udce9.py" in first.entries and "main:tests/z\udce9.py" in first.entries
    assert fingerprint.compare(first, second).ok
    for target in (champ, main):
        with open(target, "ab") as fh:
            fh.write(b"# edited\n")
    verdict = fingerprint.compare(second, world.take("after-implementer", worktree=None))
    assert not verdict.ok
    assert {"champion:src/z\\xe9.py", "main:tests/z\\xe9.py"} <= set(verdict.changed)
    _recordable(verdict)


def test_take_attempt_with_a_name_that_is_not_utf8_gives_a_point_and_broken(audit_world):
    """a13 of check-adversarial-r3: audit.run gets a pair it can compare and void."""
    world = audit_world
    conn = _attempt_record(world)
    before = fingerprint.take_attempt(conn, world.root, TASK, "audit-before")
    _plant_bytes(os.path.join(world.worktree.path, "tests"))
    after = fingerprint.take_attempt(conn, world.root, TASK, "audit-after")
    _reads_back(after)
    verdict = fingerprint.compare(before, after)
    assert not verdict.ok and verdict.changed == ("contract:tests/" + BAD_SHOWN,)
    # a name like that present at both points compares clean
    assert fingerprint.compare(after, fingerprint.take_attempt(conn, world.root, TASK,
                                                               "audit-after")).ok


def test_a_self_ignoring_gitignore_planted_in_the_main_checkout_is_caught(world):
    """a7d of check-adversarial-r3: tests/.gitignore holding `*` hides itself and conftest.py
    from the listing of untracked paths; nothing purges the main checkout."""
    before = world.take("after-implementer")
    write(os.path.join(world.root, "tests", ".gitignore"), "*\n")
    write(os.path.join(world.root, "tests", "conftest.py"), "import os; os._exit(0)\n")
    verdict = fingerprint.compare(before, world.take("after-eval"))
    assert not verdict.ok and verdict.changed == ("main:tests/.gitignore",)


def test_a_cache_folders_own_gitignore_in_the_main_checkout_is_not_a_source(world):
    """pytest writes .pytest_cache/.gitignore holding `*`: a cache the walk leaves out."""
    before = world.take("after-implementer")
    write(os.path.join(world.root, "tests", ".pytest_cache", ".gitignore"), "*\n")
    write(os.path.join(world.root, "tests", ".pytest_cache", "v", "cache", "nodeids"), "[]\n")
    after = world.take("after-eval")
    assert not any(".pytest_cache" in k for k in after.entries)
    assert fingerprint.compare(before, after).ok


def test_a_clone_config_section_added_and_removed_between_points_is_caught(world):
    """a1c of check-adversarial-r3: a filter section another slot's host git could run."""
    cfg = os.path.join(world.clone.path, ".git", "config")
    with open(cfg, encoding="utf-8") as fh:
        text = fh.read()
    before = world.take("after-implementer")
    st = os.stat(cfg)
    write(cfg, text + '[filter "x"]\n\tclean = /tmp/evil\n')
    _restore(cfg, text, st)
    verdict = fingerprint.compare(before, world.take("after-eval"))
    assert not verdict.ok and verdict.changed == ("clone:.git/config",)
    # a file planted in and removed from a clone folder moves that folder's stamp
    before = world.take("after-eval")
    hook = os.path.join(world.clone.path, ".git", "hooks", "post-checkout")
    write(hook, "#!/bin/sh\n", 0o755)
    os.unlink(hook)
    verdict = fingerprint.compare(before, world.take("after-audit"))
    assert not verdict.ok and verdict.changed == ("clone:.git/hooks/.",)


def test_affected_never_flags_a_slot_for_another_slots_own_tree(world):
    """a11 of check-adversarial-r3: slot A edits its own protected test; honest slot B, whose
    point holds the same contract: key for its own tree, is not flagged."""
    conn = db.connect(world.root)
    _add_task(conn, "t-005")
    _add_task(conn, "t-006")
    slot_b = world.attempt_tree("t-006")
    a_before = world.take("after-proposer")
    _record_point(conn, a_before)
    b_before = world.take("after-proposer", task="t-006", worktree=slot_b)
    since = _record_point(conn, b_before)
    write(os.path.join(world.worktree.path, "tests", "test_app.py"), "def test_app(): pass\n")
    va = fingerprint.compare(a_before, world.take("after-implementer"))
    assert not va.ok and va.changed == ("contract:tests/test_app.py",)
    vb = fingerprint.compare(b_before, world.take("after-implementer", task="t-006",
                                                  worktree=slot_b))
    assert vb.ok
    assert fingerprint.affected(conn, RUN, "contract:tests/test_app.py", since_event=since) == []
    # a shared source key still flags both open slots
    assert fingerprint.affected(conn, RUN, "tool:mytool", since_event=since) == ["t-005", "t-006"]


# ================================================================ check round 1, cycle 2

def test_affected_returns_attempt_tasks_only(world):
    """check-design-r1 finding 1: a repair task (role repair) is not a slot. With or without a
    research-tamper row on its ref it is never returned, and neither is a task whose tamper row
    has no task-add."""
    conn = db.connect(world.root)
    _add_task(conn, "t-010", role="repair")        # has a tamper row below
    _add_task(conn, "t-011", role="report")
    _add_task(conn, "t-012")                       # an attempt task
    _add_task(conn, "t-014", role="repair")        # no tamper row: its window would never shut
    _add_task(conn, "t-015", run="rr-002")         # another run's attempt
    repair_point = world.take("after-reproducer-writer", task="t-010", worktree=None)
    _record_point(conn, repair_point)
    since = _record_point(conn, world.take("open", task="t-012", worktree=None))
    _record_point(conn, world.take("open", task="t-013", worktree=None))   # no task-add at all
    os.unlink(repair_point.path)                   # an unreadable point keeps a slot, not this
    write(os.path.join(world.bin2, "mytool"), "#!/bin/sh\necho planted\n", 0o755)
    for since_event in (0, since):
        assert fingerprint.affected(conn, RUN, "tool:mytool",
                                    since_event=since_event) == ["t-012"]
    # positive control: the same repair task given role attempt is a slot again
    _add_task(conn, "t-016")
    assert fingerprint.affected(conn, RUN, "tool:mytool", since_event=since) == [
        "t-012", "t-016"]


def test_affected_keeps_a_slot_whose_point_file_no_longer_matches_its_row(world):
    """a6b of check-adversarial-r1 (cycle 2): slot A's saved point is rewritten without its tool
    keys after its row was recorded; the row's sha256 no longer matches, so A is kept."""
    conn = db.connect(world.root)
    _add_task(conn, "t-005")
    _add_task(conn, "t-006")
    slot_b = world.attempt_tree("t-006")
    a_open = world.take("open", worktree=None)
    _record_point(conn, a_open)
    since = _record_point(conn, world.take("open", task="t-006", worktree=slot_b))
    with open(a_open.path, encoding="utf-8") as fh:
        data = json.load(fh)
    data["entries"] = {k: v for k, v in data["entries"].items()
                       if not k.startswith(("tool:", "tool-path:"))}
    with open(a_open.path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    write(os.path.join(world.bin2, "mytool"), "#!/bin/sh\necho planted\n", 0o755)
    assert fingerprint.affected(conn, RUN, "tool:mytool", since_event=since) == [
        "t-005", "t-006"]
    # negative control: a row with no sha256 cannot be checked, and the rewritten file then
    # drops A (so the kept A above comes from the sha256 check)
    conn.execute("UPDATE events SET data=json_remove(data, '$.sha256') WHERE ref='t-005' "
                 "AND kind=?", (events.TAMPER,))
    assert fingerprint.affected(conn, RUN, "tool:mytool", since_event=since) == ["t-006"]


needs_non_root = pytest.mark.skipif(
    os.geteuid() == 0, reason="as root every test folder is root-owned, which gets no ctime")


def _swap_back(folder, fill):
    """Rename folder away, let fill(folder) make a new one at the same path, remove it and put
    the original back: every inode below it is the original one again."""
    os.rename(folder, folder + ".orig")
    fill(folder)
    shutil.rmtree(folder)
    os.rename(folder + ".orig", folder)


@needs_non_root
def test_a_folder_above_a_tool_swapped_and_put_back_is_caught(world):
    """a3c of check-adversarial-r1 (cycle 2): the grandparent of the tool's realpath."""
    v1 = str(world.tmp / "opt" / "pkg" / "v1")
    real = os.path.join(v1, "mytool")
    write(real, "#!/bin/sh\necho tool v1\n", 0o755)
    contract = make_contract(tools=("mytool",), tools_real=(real,), shared=(world.shared,),
                             read_real=(world.data,))

    def take(point):
        return fingerprint.take(world.root, RUN, TASK, point, contract=contract,
                                worktree=world.worktree, clone=world.clone,
                                champion=world.champion, path_env=world.bin1 + ":" + v1)
    before = take("after-implementer")
    assert fingerprint.compare(before, take("after-implementer")).ok      # a quiet window is ok
    _swap_back(os.path.dirname(v1), lambda pkg: write(os.path.join(pkg, "v1", "mytool"),
                                                      "#!/bin/sh\necho evil\n", 0o755))
    after = take("after-eval")
    verdict = fingerprint.compare(before, after)
    assert not verdict.ok and "tool:mytool" in verdict.changed, verdict
    assert after.entries["tool:mytool"].split()[0] == before.entries["tool:mytool"].split()[0]


@needs_non_root
def test_a_folder_above_a_gate_path_folder_swapped_and_put_back_is_caught(world):
    """A shadow in a gate PATH folder that sits earlier than the tool, planted through a swap
    of the folder above it and gone again by the next point."""
    early = str(world.tmp / "early" / "sub" / "bin")
    os.makedirs(early)
    path_env = early + ":" + world.path_env

    def take(point):
        return fingerprint.take(world.root, RUN, TASK, point, contract=world.contract,
                                worktree=world.worktree, clone=world.clone,
                                champion=world.champion, path_env=path_env)
    before = take("after-implementer")
    _swap_back(os.path.dirname(early), lambda sub: write(os.path.join(sub, "bin", "mytool"),
                                                         "#!/bin/sh\necho evil\n", 0o755))
    verdict = fingerprint.compare(before, take("after-eval"))
    assert not verdict.ok and "tool-path:" + early in verdict.changed, verdict


@needs_non_root
def test_a_missing_gate_path_folder_made_and_removed_again_is_caught(world):
    missing = str(world.tmp / "later" / "bin")
    os.makedirs(os.path.dirname(missing))
    path_env = missing + ":" + world.path_env

    def take(point):
        return fingerprint.take(world.root, RUN, TASK, point, contract=world.contract,
                                worktree=world.worktree, clone=world.clone,
                                champion=world.champion, path_env=path_env)
    before = take("after-implementer")
    assert before.entries["tool-path:" + missing].startswith("absent up=")
    write(os.path.join(missing, "mytool"), "#!/bin/sh\necho evil\n", 0o755)
    shutil.rmtree(missing)
    verdict = fingerprint.compare(before, take("after-eval"))
    assert not verdict.ok and verdict.changed == ("tool-path:" + missing,), verdict


@needs_non_root
@pytest.mark.parametrize("kind", ["read", "shared"])
def test_a_folder_above_a_location_swapped_and_put_back_is_caught(world, kind):
    """a4e of check-adversarial-r1 (cycle 2): the parent of a read path (and of a shared
    checkout, the same code) renamed away, refilled with other rows, and put back."""
    parent = str(world.tmp / ("outer-" + kind) / "mid")    # not right in tmp_path (see scratch)
    location = os.path.join(parent, "set")
    write(os.path.join(location, "rows.csv"), "a,b\n1,2\n")
    over = {"read_real": (location,)} if kind == "read" else {"shared": (location,)}
    contract = make_contract(**dict(dict(tools=("mytool",), tools_real=world.contract.tools_real,
                                         shared=(world.shared,), read_real=(world.data,)),
                                    **over))
    before = world.take("after-implementer", contract=contract)
    assert fingerprint.compare(before, world.take("after-implementer", contract=contract)).ok
    _swap_back(parent, lambda p: write(os.path.join(p, "set", "rows.csv"), "a,b\n9,9\n"))
    verdict = fingerprint.compare(before, world.take("after-eval", contract=contract))
    real = os.path.realpath(location)
    assert not verdict.ok and verdict.changed == ("%s:%s/." % (kind, real),), verdict


@needs_non_root
def test_a_read_path_that_is_one_file_carries_its_ancestor_chain(world):
    parent = str(world.tmp / "limits" / "mid")
    single = os.path.join(parent, "limits.txt")
    write(single, "10\n")
    contract = make_contract(tools=("mytool",), tools_real=world.contract.tools_real,
                             shared=(world.shared,), read_real=(single,))
    before = world.take("after-implementer", contract=contract)
    _swap_back(parent, lambda p: write(os.path.join(p, "limits.txt"), "99\n"))
    verdict = fingerprint.compare(before, world.take("after-eval", contract=contract))
    assert not verdict.ok and verdict.changed == ("read:" + os.path.realpath(single),)


def test_the_ancestor_chain_stamps_ctime_only_where_a_non_root_process_can_rename(tmp_path):
    # a folder right inside a sticky folder: device and inode only; its parent is stamped
    sticky = tmp_path / "sticky"
    os.makedirs(sticky / "own" / "leaf")
    os.chmod(sticky, 0o1777)
    chain = fingerprint._ancestry(str(sticky / "own" / "leaf" / "x"))[len(" up="):].split(";")
    by = {entry.split(" ")[0]: entry for entry in chain}
    assert "ctime=" in by[str(sticky / "own" / "leaf")]
    assert "ctime=" not in by[str(sticky / "own")]            # its parent is sticky
    if os.geteuid() != 0:
        assert "ctime=" in by[str(sticky)]
    assert chain[-1].startswith("/ dev=") and "ctime=" not in chain[-1]
    # a missing ancestor is listed as absent; one that is a file is an error
    assert str(tmp_path / "gone") + " absent" in fingerprint._ancestry(str(tmp_path / "gone" / "x"))
    write(str(tmp_path / "file"), "x")
    with pytest.raises(fingerprint._SourceError):
        fingerprint._ancestry(str(tmp_path / "file" / "x"))


@pytest.mark.skipif(os.lstat("/usr").st_uid != 0 or os.lstat("/").st_uid != 0,
                    reason="needs a root-owned /usr and /")
def test_the_ancestor_chain_of_a_system_tool_has_no_ctime():
    """/usr and / are root-owned without a group or other write bit: only root can rename a
    folder below them, so a package install that adds a folder to /usr breaks no window."""
    chain = fingerprint._ancestry("/usr/bin/env")
    assert chain.startswith(" up=/usr/bin dev=") and "ctime=" not in chain


def test_the_engines_own_writes_in_the_run_folder_break_no_digest(world):
    """The folders above contract/ get no stamp (module text, `frozen:`): the launcher's homes/,
    the process registry pids/, runner.pid and a new run beside this one are normal work."""
    before = world.take("after-implementer")
    run_dir = paths.run_dir(world.root, RUN)
    os.makedirs(os.path.join(run_dir, "homes", "l-1"))
    os.makedirs(os.path.join(run_dir, "pids"))
    util.write_text(os.path.join(run_dir, "runner.pid"), "123\n")
    os.makedirs(paths.run_dir(world.root, "rr-009"))
    util.write_text(os.path.join(world.root, ".alpaca", "serve.json"), "{}\n")
    assert fingerprint.compare(before, world.take("after-eval")).ok


# ================================================================ check round 2 (second cycle)

def _cut_base_tree(world, other):
    """What fresh_tree cuts for another slot's gates step (architecture.md section 3.1, admin
    False): attempt_dir/base holding the tracked files, with no .git."""
    base = os.path.join(paths.attempt_dir(world.root, RUN, other), "base")
    for rel in list(FILES) + ["tests/fixtures/.gitignore"]:
        with open(os.path.join(world.root, rel), encoding="utf-8") as fh:
            write(os.path.join(base, rel), fh.read())
    return base


@pytest.mark.parametrize("ignore", ["template", "unignored"])
def test_another_slots_engine_trees_in_the_live_project_break_no_digest(world, ignore):
    """check-design-r2 B1: the target is the live project (repo_path "."), so the engine's run
    folder and attempt worktrees lie inside the main checkout. Another slot's base tree cut and
    removed, and another attempt worktree cut, during this slot's window are the engine's own
    work, not tampering, and so is a rewrite of tracked state under .alpaca/. "template" ignores
    .alpaca/ as the shipped .gitignore does and the target tracks a .gitignore under a protected
    folder (caught before the fix through the no-exclude .gitignore listing); "unignored"
    un-ignores .alpaca/ as this project's own .gitignore does (every scoped file of the base tree
    was a key before the fix)."""
    write(os.path.join(world.root, "tests", "fixtures", ".gitignore"), "*.tmp\n")
    # this project tracks state under .alpaca/ that sessions rewrite all the time (the record
    # snapshot); a tracked file there under a one-folder scope rule stands for it
    state = os.path.join(world.root, ".alpaca", "notes", "tests", "log.txt")
    write(state, "v1\n")
    if ignore == "unignored":
        write(os.path.join(world.root, ".gitignore"), "build/\n__pycache__/\n!.alpaca/**\n")
    git(world.root, "add", ".gitignore", "tests/fixtures/.gitignore")
    git(world.root, "add", "-f", ".alpaca/notes/tests/log.txt")
    git(world.root, "commit", "-qm", "fixtures")
    before = world.take("after-implementer")
    assert not any(k.startswith("main:.alpaca") for k in before.entries), sorted(before.entries)
    base = _cut_base_tree(world, "t-999")
    world.attempt_tree("t-006")
    write(state, "v2\n")
    during = world.take("after-eval")
    assert os.path.isfile(os.path.join(base, "tests", "fixtures", ".gitignore"))
    assert fingerprint.compare(before, during).ok, fingerprint.compare(before, during)
    shutil.rmtree(base)
    assert fingerprint.compare(before, world.take("after-audit")).ok
    # the same edit outside .alpaca/ is still caught: the drop is that one folder
    write(os.path.join(world.root, "tests", "fixtures", ".gitignore"), "*\n")
    verdict = fingerprint.compare(before, world.take("after-notes"))
    assert not verdict.ok and verdict.changed == ("main:tests/fixtures/.gitignore",)


def test_the_main_source_leaves_out_only_the_project_state_folder_inside_the_checkout(tmp_path):
    root = str(tmp_path / "proj")
    os.makedirs(root)
    assert fingerprint._engine_folders(root, root) == (".alpaca",)
    assert fingerprint._engine_folders(root, str(tmp_path)) == ("proj/.alpaca",)
    assert fingerprint._engine_folders(root, os.path.join(root, "sub")) == ()
    assert fingerprint._engine_folders(root, str(tmp_path / "other")) == ()


def test_a_planted_gitignore_named_alpaca_below_the_top_is_still_caught(world):
    """Only the project's own .alpaca/ at the top is dropped: a folder of that name inside a
    protected folder is target content like any other (the World's .gitignore ignores `.alpaca/`
    at any depth, so the planted self-ignoring .gitignore is the key that shows it)."""
    before = world.take("after-implementer")
    write(os.path.join(world.root, "tests", ".alpaca", ".gitignore"), "*\n")
    write(os.path.join(world.root, "tests", ".alpaca", "conftest.py"), "x = 1\n")
    verdict = fingerprint.compare(before, world.take("after-eval"))
    assert not verdict.ok and verdict.changed == ("main:tests/.alpaca/.gitignore",)


def _plant_self_ignoring(wt, folder="pack"):
    write(os.path.join(wt, folder, ".gitignore"), "*\n")


def test_a_tracked_file_hidden_by_a_planted_text_attribute_is_listed(world):
    """a8 of check-adversarial-r2 (second cycle): pack/.gitattributes `* text` makes git read a
    CRLF rewrite of pack/runbook.yaml as clean, and pack/.gitignore `*` hides both planted files.
    The attributes are read from forked_from, and every new ignore or attribute file is listed."""
    wt = world.worktree.path
    _plant_self_ignoring(wt)
    write(os.path.join(wt, "pack", ".gitattributes"), "* text\n")
    with open(os.path.join(wt, "pack", "runbook.yaml"), "wb") as fh:
        fh.write(b"runbook: 3\r\n")
    verdict = world.check()
    assert not verdict.ok
    assert verdict.outside == ("pack/.gitattributes", "pack/.gitignore", "pack/runbook.yaml")


def test_a_tracked_file_hidden_by_a_planted_encoding_attribute_is_listed(world):
    """a12 of check-adversarial-r2 (second cycle): a working-tree-encoding rule and the file
    rewritten in that encoding, so every byte on disk differs while git reads it as unchanged."""
    wt = world.worktree.path
    _plant_self_ignoring(wt)
    write(wt + "/pack/.gitattributes", "runbook.yaml working-tree-encoding=UTF-16LE\n")
    with open(os.path.join(wt, "pack", "runbook.yaml"), "wb") as fh:
        fh.write("runbook: 3\n".encode("utf-16-le"))
    verdict = world.check()
    assert not verdict.ok
    assert "pack/runbook.yaml" in verdict.outside


def test_a_new_file_hidden_by_a_planted_self_ignoring_gitignore_is_caught(world):
    """a7 of check-adversarial-r2 (second cycle): pack/.gitignore `*` hides pack/conftest.py and
    itself from `ls-files --exclude-standard`; the planted ignore file is a changed path."""
    wt = world.worktree.path
    _plant_self_ignoring(wt)
    write(os.path.join(wt, "pack", "conftest.py"), "import os\n")
    verdict = world.check()
    assert not verdict.ok and verdict.outside == ("pack/.gitignore",)


def test_a_self_ignoring_gitignore_inside_the_allowlist_passes_the_path_check(world):
    """The no-exclude listing adds paths, and each is judged against the allowlist as any other:
    an ignore or attribute file under src/ is an editable change."""
    wt = world.worktree.path
    _plant_self_ignoring(wt, "src")
    write(os.path.join(wt, "src", ".gitattributes"), "*.py text\n")
    assert world.check().ok


def test_a_pytest_cache_gitignore_in_the_worktree_is_left_out_of_the_path_check(world):
    """pytest writes .pytest_cache/.gitignore holding `*` when the implementer runs the tests: a
    cache folder, which the pre-eval purge owns."""
    wt = world.worktree.path
    write(os.path.join(wt, ".pytest_cache", ".gitignore"), "*\n")
    write(os.path.join(wt, ".pytest_cache", "v", "cache", "nodeids"), "[]\n")
    write(os.path.join(wt, "tests", ".pytest_cache", ".gitignore"), "*\n")
    assert world.check().ok


def test_compare_reads_an_error_entry_whose_errors_list_is_empty():
    """check-design-r2 m2: a point file read back with its errors list emptied but an error entry
    left is still BROKEN; a listed error is not shown twice."""
    base = dict(run=RUN, task=TASK, taken_at="now", path="/x")
    good = FingerprintPoint(point="open", entries={"a": "1"}, errors=(), **base)
    bad = FingerprintPoint(point="close", entries={"a": "1", "clone": "error: no clone"},
                           errors=(), **base)
    verdict = fingerprint.compare(bad, bad)
    assert not verdict.ok and verdict.errors == ("close clone: no clone",) * 2
    listed = FingerprintPoint(point="close", entries={"a": "1", "clone": "error: no clone"},
                              errors=("clone: no clone",), **base)
    assert fingerprint.compare(good, listed).errors == ("close clone: no clone",)
    assert fingerprint.compare(good, good).ok
