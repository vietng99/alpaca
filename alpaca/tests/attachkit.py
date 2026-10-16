"""Test support for attach mode (op-062): a local bare upstream, an onboarded studio, an attached one.

A helper module in the pattern of proofkit.py. A test file imports what it needs and names the fixtures
it uses in its own namespace, so nothing here runs for a test that does not ask for it:

    from alpaca.tests.attachkit import studio, upstream, attached      # noqa: F401  (pytest fixtures)

Import the three together: `attached` is built from `studio` and `upstream`, and pytest finds a fixture's
own dependencies only in the namespace of the test module.

Later tasks add helpers by appending new functions at the end of this file; they do not edit the ones
above. No network: the upstream is a bare repository in the test's temp folder, and a GitHub call is an
injected callable.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from alpaca import cli, project as project_mod
from alpaca.tests.conftest import REPO

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
           "GIT_AUTHOR_NAME": "Ada Author", "GIT_AUTHOR_EMAIL": "ada@example.org",
           "GIT_COMMITTER_NAME": "Ada Author", "GIT_COMMITTER_EMAIL": "ada@example.org", "LC_ALL": "C"}
_LOCAL = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
          "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS")


def git(path, *args, check=True, env=None, input=None):
    """`git -C path args` with a fixed identity and no host config; returns stdout without the final newline."""
    # an inherited GIT_TRACE* names a file git appends to: the helper's own `git status` would create it in the repository
    full = {k: v for k, v in os.environ.items() if k not in _LOCAL and not k.startswith("GIT_TRACE")}
    full.update(GIT_ENV)
    full.update(env or {})
    done = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, env=full, input=input)
    if check and done.returncode != 0:
        raise AssertionError("git %s failed in %s: %s" % (" ".join(args), path, done.stderr.strip()))
    return done.stdout[:-1] if done.stdout.endswith("\n") else done.stdout


DEFAULT_FILES = {
    "README.md": "# demo app\n",
    "Makefile": "build:\n\t@echo build\n\ntest:\n\t@echo test\n",
    "src/main.py": "print('hello')\n",
}


class Upstream:
    """A bare repository and a working clone used to add commits to it."""

    def __init__(self, bare, work, branch):
        self.bare, self.work, self.branch = Path(bare), Path(work), branch

    @property
    def url(self):
        return str(self.bare)

    def sha(self, ref=None):
        return git(self.bare, "rev-parse", ref or self.branch)

    def commit(self, files, message="change", author="Bo Builder", branch=None):
        """Write `files` ({path: text}) on the working clone, commit as `author`, push; returns the sha."""
        branch = branch or self.branch
        if git(self.work, "symbolic-ref", "--short", "HEAD") != branch:
            git(self.work, "checkout", "-q", branch)
        for rel, text in files.items():
            target = self.work / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        git(self.work, "add", "-A")
        env = {"GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": "%s@example.org" % author.split()[0].lower(),
               "GIT_COMMITTER_NAME": author, "GIT_COMMITTER_EMAIL": "%s@example.org" % author.split()[0].lower()}
        git(self.work, "commit", "-q", "-m", message, env=env)
        git(self.work, "push", "-q", "origin", branch)
        return git(self.work, "rev-parse", "HEAD")

    def branch_off(self, name, files, message="topic", author="Cy Coder"):
        """A second branch with one commit on top of the integration branch; returns its sha."""
        git(self.work, "checkout", "-q", "-B", name, self.branch)
        sha = self.commit(files, message, author, branch=name)
        git(self.work, "checkout", "-q", self.branch)
        return sha


def make_upstream(base, name="upstream", files=None, branch="main"):
    """A bare upstream with two commits by two authors on `branch`."""
    base = Path(base)
    bare, work = base / ("%s.git" % name), base / ("%s-work" % name)
    git(base, "init", "-q", "--bare", "-b", branch, str(bare))
    git(base, "clone", "-q", str(bare), str(work))
    git(work, "checkout", "-q", "-b", branch)
    up = Upstream(bare, work, branch)
    up.commit(files or DEFAULT_FILES, "first commit", "Ada Author")
    up.commit({"CHANGELOG.md": "- first\n"}, "second commit", "Bo Builder")
    return up


def make_studio(root, git_init=True, gitignore=True):
    """Turn the `project` fixture's root into an onboarded studio: a git repository with the shipped
    .gitignore (when `git_init`), the record, and project.yaml written by `alpaca onboard`."""
    root = Path(root)
    if git_init:
        git(root, "init", "-q")
        if gitignore:
            shutil.copy(os.path.join(REPO, "templates", "release", "gitignore"), root / ".gitignore")
    assert cli.main(["init"]) == 0
    assert cli.main(["onboard", "--name", "demo", "--who", "ada:owner", "--what", "a studio for tests"]) == 0
    return root


@pytest.fixture
def studio(project):
    """An onboarded studio: the `project` fixture's root, a git repository, the cwd inside it."""
    return make_studio(project)


@pytest.fixture
def upstream(tmp_path):
    return make_upstream(tmp_path)


@pytest.fixture
def attached(studio, upstream):
    """The studio with `upstream` attached at app/ (gates sensed from the Makefile)."""
    from alpaca.attach import repo
    cfg = repo.attach(str(studio), upstream.url)
    return AttachedStudio(studio, upstream, cfg)


class AttachedStudio:
    def __init__(self, root, upstream, cfg):
        self.root, self.studio, self.upstream, self.cfg = Path(root), Path(root), upstream, cfg

    @property
    def app(self):
        return self.root / self.cfg.repo


def project_yaml(root):
    return project_mod.load(str(root))


REMOVE = object()


def set_attach(root, value):
    """Set the `attach:` key of the studio's project.yaml to `value` as written (REMOVE deletes the key)."""
    cfg = project_mod.load(str(root))
    if value is REMOVE:
        cfg.pop("attach", None)
    else:
        cfg["attach"] = value
    project_mod.save(str(root), cfg)


def write_attach(root, **block):
    """Replace the `attach:` block of the studio's project.yaml with these keys."""
    set_attach(root, block)


def make_linked_worktree_studio(tmp_path, monkeypatch):
    """An onboarded studio whose `.git` is a gitfile: a linked worktree of another clone (how this
    development repository runs every task). The cwd is moved into it."""
    main = Path(tmp_path) / "main-clone"
    main.mkdir()
    git(main, "init", "-q", "-b", "main")
    (main / "README.md").write_text("x\n")
    git(main, "add", "-A")
    git(main, "commit", "-q", "-m", "m")
    root = Path(tmp_path) / "linked-studio"
    git(main, "worktree", "add", "-q", "-b", "studio", str(root))
    shutil.copy(os.path.join(REPO, "ALPACA-MANIFEST"), root / "ALPACA-MANIFEST")
    shutil.copy(os.path.join(REPO, "templates", "release", "gitignore"), root / ".gitignore")
    (root / "style").mkdir()
    (root / "style" / "banned.txt").write_text("")
    (root / ".alpaca" / "config").mkdir(parents=True)
    monkeypatch.chdir(root)
    assert cli.main(["init"]) == 0
    assert cli.main(["onboard", "--name", "demo", "--who", "ada:owner", "--what", "a studio for tests"]) == 0
    return root


# ---- t-651: task worktrees, commits by the attach identity, a term list (appended; nothing above is edited) ------

AGENT_ENV = {"GIT_AUTHOR_NAME": "Alpaca agent", "GIT_AUTHOR_EMAIL": "alpaca-agent@localhost",
             "GIT_COMMITTER_NAME": "Alpaca agent", "GIT_COMMITTER_EMAIL": "alpaca-agent@localhost"}


def add_worktree(attached, task="t-001"):
    """The work folder of `task` on its work branch `<prefix><task>`, made by t-648's real cut
    (`alpaca.attach.worktree.cut`: fetch, baseline, branch, `git worktree add`, the `attach-branch` event).
    Integration of t-648 and t-651 (op-062): the guard and the scan accept a task worktree only when
    `alpaca.attach.proven` proves it, so a folder made here by plain `git worktree add` is not one."""
    from alpaca.attach import worktree
    info = worktree.cut(str(attached.root), task, worker="kit", session="cli")
    return Path(info["worktree"])


def commit_in(path, files, message="agent change", env=None):
    """Write `files` ({path: text or bytes}) under `path`, commit them as the attach identity (or `env`);
    returns the new head sha."""
    for rel, data in files.items():
        target = Path(path) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            target.write_bytes(data)
        else:
            target.write_text(data, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", message, env=env or AGENT_ENV)
    return git(path, "rev-parse", "HEAD")


def with_term_list(attached, extra=(), text=None):
    """Point the studio's barrier at `.alpaca/sealed-terms.txt`, write the attach minimum there (as attach
    init does for a studio whose barrier names the file), then add `extra` lines. `text` replaces the file."""
    from alpaca.attach import repo as attach_repo
    cfg = project_mod.load(str(attached.root))
    cfg["barrier"] = dict(cfg.get("barrier") or {}, terms=".alpaca/sealed-terms.txt")
    project_mod.save(str(attached.root), cfg)
    path = attached.root / ".alpaca" / "sealed-terms.txt"
    if path.exists():
        path.unlink()
    attach_repo.write_terms(str(attached.root), repo=attached.cfg.repo)
    if text is not None:
        path.write_text(text, encoding="utf-8")
    elif extra:
        path.write_text(path.read_text(encoding="utf-8") + "\n".join(extra) + "\n", encoding="utf-8")
    return path


def hook_payload(command, cwd):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}


# ---- t-648: baseline and work branch ---------------------------------------------------------------------------------

def add_tasks(root, count=1):
    """Open one op and `count` tasks in the studio; returns the new task ids in order."""
    from alpaca import db
    assert cli.main(["op", "new", "attach work"]) == 0
    for i in range(count):
        assert cli.main(["task", "add", "--title", "task %d" % i, "op-001", "s", "--phase", "build"]) == 0
    conn = db.connect(str(root))
    try:
        return [t["id"] for t in db.rows(conn, "tasks")]
    finally:
        conn.close()


def set_gates(root, gates):
    """Replace the gates of the studio's `attach:` block (the rest of the block stays)."""
    block = dict(project_mod.load(str(root))["attach"])
    block["gates"] = gates
    set_attach(root, block)


def counting_gates(counter, names=("one", "two")):
    """Gates that append their name to the file `counter` each time they run (so a test can count runs)."""
    import sys
    code = "import sys; open(%r, 'a').write(sys.argv[1] + '\\n')" % str(counter)
    return [{"name": n, "command": [sys.executable, "-c", code, n], "timeout": 60} for n in names]


def break_remote(upstream):
    """Make the upstream unreachable (its folder moves away); returns a function that puts it back."""
    gone = Path(str(upstream.bare) + ".gone")
    os.rename(str(upstream.bare), str(gone))
    return lambda: os.rename(str(gone), str(upstream.bare))


def task_row(root, task):
    from alpaca import db
    conn = db.connect(str(root))
    try:
        return db.rows(conn, "tasks", "id=?", (task,))[0]
    finally:
        conn.close()


# ---- t-686: approval, handoff, merge and sync (appended; nothing above is edited) -----------------------------------

REPOSITORY = "acme/demo"
FAST_GATES = [{"name": "ok", "command": ["python3", "-B", "-c", "import sys; sys.exit(0)"], "timeout": 60}]


class FakeGitHub:
    """The GitHub API of one repository, held in memory, as the injected `api=` callable of attach's client.

    It answers the four calls of `alpaca.attach.github` (list, open, get, merge), keeps every call in `calls`, and
    refuses a merge whose pinned sha is not the pull request's head the way GitHub does (409). `upstream` (an
    `Upstream`) is where a new pull request reads its head sha. `fail` maps a (method, path prefix) to a message: the
    next call to it raises an AttachError once (a down API)."""

    def __init__(self, repository=REPOSITORY, upstream=None):
        self.repository, self.upstream = repository, upstream
        self.prs, self.calls, self.fail = {}, [], {}

    def _head(self, branch):
        return git(self.upstream.bare, "rev-parse", "refs/heads/" + branch)

    def __call__(self, method, path, payload=None):
        import re
        from alpaca.attach import AttachError
        self.calls.append((method, path, payload))
        for (m, prefix), message in list(self.fail.items()):
            if m == method and path.startswith(prefix):
                del self.fail[(m, prefix)]
                raise AttachError("GitHub %s %s answered 503: %s" % (method, prefix, message))
        base = "/repos/%s/pulls" % self.repository
        if method == "GET" and path.startswith(base + "?"):
            return [dict(p) for p in self.prs.values()]
        if method == "POST" and path == base:
            number = len(self.prs) + 1
            self.prs[number] = {"number": number, "html_url": "https://github.com/%s/pull/%d" % (self.repository, number),
                                "state": "open", "merged_at": None, "merge_commit_sha": None,
                                "head": {"ref": payload["head"], "sha": self._head(payload["head"])},
                                "base": {"ref": payload["base"]}, "title": payload["title"], "body": payload["body"]}
            return dict(self.prs[number])
        found = re.fullmatch(re.escape(base) + r"/(\d+)(/merge)?", path)
        if found and int(found.group(1)) in self.prs:
            pr = self.prs[int(found.group(1))]
            if method == "GET" and not found.group(2):
                return dict(pr)
            if method == "PUT" and found.group(2):
                if payload["sha"] != pr["head"]["sha"]:
                    raise AttachError("GitHub PUT %s answered 409: Head branch was modified" % path)
                pr.update(state="closed", merged_at="2026-10-15T00:00:00Z", merge_commit_sha="f" * 40)
                return {"merged": True, "sha": "f" * 40}
        raise AttachError("GitHub %s %s answered 404" % (method, path))

    def merge_outside(self, number, sha="e" * 40):
        """The upstream's own tool merged it (squash or rebase): merged_at is set, the head is not an ancestor."""
        self.prs[number].update(state="closed", merged_at="2026-10-15T00:00:00Z", merge_commit_sha=sha)

    def close(self, number):
        self.prs[number].update(state="closed", merged_at=None)

    def calls_of(self, method):
        return [c for c in self.calls if c[0] == method]


def set_repository(root, repository=REPOSITORY):
    """Put `repository` (owner/name) in the studio's `attach:` block."""
    block = dict(project_mod.load(str(root))["attach"])
    block["repository"] = repository
    set_attach(root, block)


class Ready:
    """A task checked and ready to approve: the studio, the work folder, the head and the check result."""

    def __init__(self, att, task, wt, head, ck):
        self.att, self.task, self.wt, self.head, self.ck = att, task, wt, head, ck
        self.root = str(att.root)


def ready(att, task="t-001", files=None, message="add a module", gates=None, terms=True):
    """A term list, `attach.repository`, fast gates, the work folder of `task` with one commit, and a passing check."""
    from alpaca.attach import check
    if terms:
        with_term_list(att)
    set_repository(att.root)
    set_gates(att.root, gates or FAST_GATES)
    wt = add_worktree(att, task)
    head = commit_in(wt, files or {"src/feature.py": "VALUE = 1\n"}, message)
    ck = check.run(str(att.root), task)
    assert ck.verdict == "PASS", ck.reason
    return Ready(att, task, wt, head, ck)


def record_decision(root, dec="dec-049934c5931f"):
    """A `decision` event for `dec` in the studio's record (what an agent delegation line must name)."""
    from alpaca import db
    conn = db.connect(str(root))
    try:
        db.append_event(conn, session="cli", actor="owner", kind="decision", ref=dec,
                        data={"id": dec, "kind": "ship", "choice": "the agent hands off for the trial"})
    finally:
        conn.close()
    return dec


def write_delegation(root, line=None, name="docs/delegation.md", section="Attach delegation"):
    """A delegation file with one section; returns the `PATH#Section` reference. `line` defaults to a valid line."""
    root = Path(root)
    line = "attach-delegation: dec-049934c5931f %s push,pr,merge" % REPOSITORY if line is None else line
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Delegations\n\nSomething before.\n\n## %s\n\nThe owner delegates:\n\n%s\n\n## Another\n\ntext\n"
                    % (section, line), encoding="utf-8")
    return "%s#%s" % (name, section)


def owner_approve(ready_, actions=None, sha=None):
    from alpaca.attach import approval
    return approval.approve(ready_.root, ready_.task, sha or ready_.head, approver="owner", actions=actions)[1]


def agent_approve(ready_, delegation=None, actions=None, sha=None):
    from alpaca.attach import approval
    if delegation is None:
        record_decision(ready_.root)
        delegation = write_delegation(ready_.root)
    return approval.approve(ready_.root, ready_.task, sha or ready_.head, approver="agent", delegation=delegation,
                            actions=actions)[1]


def remote_branches(upstream):
    """The branch names of the bare upstream."""
    return sorted(git(upstream.bare, "for-each-ref", "--format=%(refname:short)", "refs/heads").split())


def all_refs(path):
    """Every ref of a repository that holds a sha of its own (symbolic refs, such as refs/remotes/origin/HEAD,
    are left to `symrefs`), as {refname: sha}."""
    out = git(path, "for-each-ref", "--format=%(refname) %(objectname) %(symref)")
    found = {}
    for line in out.splitlines():
        parts = line.split(" ")
        if len(parts) == 2 or not parts[2]:
            found[parts[0]] = parts[1]
    return found


def symrefs(path):
    """The symbolic refs of a repository as {refname: target}."""
    out = git(path, "for-each-ref", "--format=%(refname) %(symref)")
    return dict(line.split(" ", 1) for line in out.splitlines() if line.count(" ") and line.split(" ", 1)[1])
