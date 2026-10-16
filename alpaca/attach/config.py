"""The `attach:` block of project.yaml and the files attach keeps (build plan sections 4 and 5).

    attach:
      repo: app                                  # the clone, one folder of the studio root
      url: https://github.com/example-org/QTRM64    # where it came from, no user info
      repository: example-org/QTRM64                # owner/name for a pull request, or null
      upstream: origin
      base: main
      branch_prefix: alpaca/
      pr_footer: true                            # false: the pull request body names no tool (op-062 t-690)
      commit_name: Alpaca agent                  # the commit identity the owner chose; both or neither (t-689)
      commit_email: alpaca-agent@localhost
      gates:
        - {name: test, command: [make, test], timeout: 600}

The mode is the presence of a valid mapping with a string `repo` (P1): `None`, `{}`, `[]` and `"app"`
give no mode, and a present but invalid block is a doctor error, never a silent mode. `mode()` never
raises. Importing this module reads nothing, so a project without the block pays nothing for it.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from alpaca.attach import AttachError, DEFAULT_IDENTITY, STATE_DIR, safefs

MISSING = object()
DEFAULTS = {"repo": "app", "upstream": "origin", "base": "main", "branch_prefix": "alpaca/"}
KEYS = ("repo", "url", "repository", "upstream", "base", "branch_prefix", "gates", "pr_footer", "commit_name",
        "commit_email")
GATE_KEYS = ("name", "command", "timeout")
DEFAULT_TIMEOUT = 600
MAX_GATES = 20

_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*")
_UPSTREAM = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_BASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]*")
_PREFIX = re.compile(r"[a-z][a-z0-9-]{2,30}/")
_PREFIX_REFUSED = ("refs/", "heads/", "remotes/", "tags/")
_GATE_NAME = re.compile(r"[a-z][a-z0-9-]*")
_COMMIT_NAME = re.compile(r"[^\s<>@\\][^<>\\\x00-\x1f\x7f]{0,98}[^\s<>\\]|[^\s<>@\\]")
_COMMIT_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9][A-Za-z0-9.-]{0,120}")
#: folders Alpaca verbs write into at run time (`alpaca note` keeps input/notes/, `alpaca spec init --kit openspec`
#: writes openspec/, spec-kit writes specs/, the hub reads runbooks, reports, design, docs and templates) and that
#: are not in ALPACA-MANIFEST. A clone placed there would receive Alpaca files, so attach.repo may not name them.
RESERVED_REPO_NAMES = ("input", "openspec", "specs", "notes", "runbooks", "reports", "design", "docs", "templates")
#: a gate may not take the name of a release check, so a receipt never holds two results under one name
_RESERVED_GATES = ("reproducibility", "leak-scan", "fresh-clone", "preflight-build")


@dataclass(frozen=True)
class AttachConfig:
    repo: str
    url: str
    repository: object
    upstream: str
    base: str
    branch_prefix: str
    gates: tuple
    #: the generated section of the pull request body (op-062 t-690): on by default; off, the body is the owner's
    #: approved text plus a short gate summary that names no tool
    pr_footer: bool = True
    #: the commit identity of every commit attach makes (op-062 t-689); the default is DEFAULT_IDENTITY
    commit_name: str = DEFAULT_IDENTITY[0]
    commit_email: str = DEFAULT_IDENTITY[1]

    @property
    def identity(self) -> tuple:
        """(name, email) every work-branch commit, the preview commit and the outgoing author check use."""
        return (self.commit_name, self.commit_email)

    def repo_path(self, root) -> Path:
        """The real path of the attached clone (the studio root resolved, then the folder)."""
        return Path(os.path.realpath(root)) / self.repo

    def gate_set_digest(self) -> str:
        """sha256 over each gate's definition (command, timeout, interpreter and host)."""
        from alpaca.release import checks
        from alpaca.release.receipts import digest
        return digest([checks.definition(dict(g)) for g in self.gates])


# ------------------------------------------------------------------------------------ reading

def raw(root):
    """The `attach` value of project.yaml as written, or MISSING when the key is absent."""
    from alpaca import project
    cfg = project.load(root)
    if not isinstance(cfg, dict):
        return MISSING
    return cfg.get("attach", MISSING)


def _manifest_top_names(root) -> set:
    names = set()
    try:
        with open(os.path.join(str(root), "ALPACA-MANIFEST"), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return names
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        names.add(line.split("/", 1)[0])
    return names


def _bad_url(url) -> str | None:
    from alpaca.attach import gitops
    if not isinstance(url, str) or not url.strip():
        return "attach.url must be a non-empty string"
    if any(ord(c) < 32 or ord(c) == 127 for c in url):
        return "attach.url holds a control character"
    if url.startswith("-"):
        return "attach.url starts with '-'"
    if gitops.has_userinfo(url):
        return "attach.url carries user info; remove it (%s)" % gitops.scrub(url)
    return None


def _bad_base(value) -> bool:
    return (not isinstance(value, str) or not _BASE.fullmatch(value) or any(x in value for x in ("..", "//", ".lock"))
            or value.endswith(("/", ".")))


def validate(block, root) -> list:
    """Every problem of an `attach:` value, one line each. [] means valid."""
    if not isinstance(block, dict):
        return ["attach must be a mapping with a repo"]
    errs = ["attach.%s is not a known key" % k for k in sorted(str(k) for k in set(block) - set(KEYS))]
    repo = block.get("repo", DEFAULTS["repo"])
    if not isinstance(repo, str) or not _REPO.fullmatch(repo):
        errs.append("attach.repo must be one folder name of the studio root (letters, digits, - and _)")
    elif repo in _manifest_top_names(root) or repo.lower() in RESERVED_REPO_NAMES:
        errs.append("attach.repo %s is a folder the Alpaca install owns or writes into" % repo)
    else:
        from alpaca.release import config as rconfig
        try:
            rconfig.contained(root, repo)
        except ValueError as exc:
            errs.append("attach.repo %s is not a real folder inside the studio (%s)" % (repo, exc))
    if "url" not in block:
        errs.append("attach.url is required")
    else:
        bad = _bad_url(block["url"])
        if bad:
            errs.append(bad)
    repository = block.get("repository")
    if repository is not None and (not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository)):
        errs.append("attach.repository must be owner/name or null")
    upstream = block.get("upstream", DEFAULTS["upstream"])
    if not isinstance(upstream, str) or not _UPSTREAM.fullmatch(upstream):
        errs.append("attach.upstream is not a valid remote name")
    base = block.get("base", DEFAULTS["base"])
    if _bad_base(base):
        errs.append("attach.base is not a valid branch name")
    prefix = block.get("branch_prefix", DEFAULTS["branch_prefix"])
    if not isinstance(prefix, str) or not _PREFIX.fullmatch(prefix) or prefix in _PREFIX_REFUSED:
        errs.append("attach.branch_prefix must look like alpaca/ (a lowercase word and a slash)")
    elif isinstance(base, str) and (base.startswith(prefix) or prefix == base + "/"):
        errs.append("attach.branch_prefix %s collides with the base branch %s" % (prefix, base))
    errs += _identity_errors(block)
    errs += _gate_errors(block.get("gates"))
    return errs


def _identity_errors(block) -> list:
    errs = []
    if "pr_footer" in block and not isinstance(block["pr_footer"], bool):
        errs.append("attach.pr_footer must be true or false")
    name, email = block.get("commit_name"), block.get("commit_email")
    if (name is None) != (email is None):
        errs.append("attach.commit_name and attach.commit_email are set together or not at all")
    if name is not None and (not isinstance(name, str) or not _COMMIT_NAME.fullmatch(name)):
        errs.append("attach.commit_name must be a plain name of 1 to 100 characters (no < > @ or control character)")
    if email is not None and (not isinstance(email, str) or not _COMMIT_EMAIL.fullmatch(email)
                              or email.startswith("-") or ".." in email):
        errs.append("attach.commit_email must look like name@host")
    return errs


def _gate_errors(gates) -> list:
    if not isinstance(gates, list) or not gates:
        return ["no attach gates: attach.gates needs at least one gate"]
    if len(gates) > MAX_GATES:
        return ["attach.gates holds %d gates; the limit is %d" % (len(gates), MAX_GATES)]
    from alpaca.release import config as rconfig
    errs, names = [], set()
    for i, gate in enumerate(gates):
        label = "attach.gates[%d]" % i
        if not isinstance(gate, dict):
            errs.append("%s is not a mapping" % label)
            continue
        errs += ["%s.%s is not a known key" % (label, k) for k in sorted(str(k) for k in set(gate) - set(GATE_KEYS))]
        name = gate.get("name")
        if not isinstance(name, str) or not _GATE_NAME.fullmatch(name):
            errs.append("%s.name must be lowercase letters, digits and -" % label)
        elif name in names:
            errs.append("%s.name %s is used twice" % (label, name))
        elif name in _RESERVED_GATES:
            errs.append("%s.name %s is the name of a release check" % (label, name))
        else:
            names.add(name)
        try:
            rconfig.argv(gate.get("command"))
        except ValueError:
            errs.append("%s.command must be a non-empty argv list of strings" % label)
        timeout = gate.get("timeout", DEFAULT_TIMEOUT)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 7200:
            errs.append("%s.timeout must be 1..7200 seconds" % label)
    return errs


def config_errors(root) -> list:
    """[] when the block is absent or valid; else the problems, one line each."""
    block = raw(root)
    if block is MISSING:
        return []
    return validate(block, root)


def _build(block) -> AttachConfig:
    gates = tuple({"name": g["name"], "command": list(g["command"]), "timeout": g.get("timeout", DEFAULT_TIMEOUT)}
                  for g in block["gates"])
    return AttachConfig(repo=block.get("repo", DEFAULTS["repo"]), url=block["url"],
                        repository=block.get("repository"), upstream=block.get("upstream", DEFAULTS["upstream"]),
                        base=block.get("base", DEFAULTS["base"]),
                        branch_prefix=block.get("branch_prefix", DEFAULTS["branch_prefix"]), gates=gates,
                        pr_footer=block.get("pr_footer", True),
                        commit_name=block.get("commit_name", DEFAULT_IDENTITY[0]),
                        commit_email=block.get("commit_email", DEFAULT_IDENTITY[1]))


def load(root) -> AttachConfig:
    """The strict reader for verbs that act: raises AttachError with the first problem."""
    block = raw(root)
    if block is MISSING:
        raise AttachError("this project is not attached: project.yaml has no attach: block")
    errs = validate(block, root)
    if errs:
        raise AttachError(errs[0])
    return _build(block)


def mode(root):
    """The AttachConfig when the project is in attach mode, else None. Never raises (P1)."""
    try:
        block = raw(root)
        if not isinstance(block, dict) or not isinstance(block.get("repo", DEFAULTS["repo"]), str):
            return None
        if "repo" not in block or validate(block, root):
            return None
        return _build(block)
    except Exception:
        return None


# ------------------------------------------------------------------------------------ state files

def state_dir(root) -> Path:
    return Path(str(root)) / STATE_DIR


def state_dir_problem(root):
    """Why attach's state folder cannot be trusted, or None. A link at `.alpaca` or `.alpaca/attach`
    would make every state write land outside the studio."""
    real_root = os.path.realpath(str(root))
    for rel in (".alpaca", STATE_DIR):
        path = os.path.join(str(root), rel)
        if os.path.islink(path):
            return "%s is a link; attach keeps its files only inside the studio" % rel
    here = os.path.realpath(str(state_dir(root)))
    if here != real_root and not here.startswith(real_root + os.sep):
        return "%s resolves outside the studio" % STATE_DIR
    return None


def read_state(root):
    """`.alpaca/attach/state.json` as a dict, or None when absent or unreadable. For display and for the
    "studio moved" note only: no decision reads it (any process in the studio can write it); decisions use
    `alpaca.attach.proven`. `test_attach_read_guard.py` enforces that."""
    return _json_dict(root, state_dir(root) / "state.json")


def _json_dict(root, path):
    """A JSON object read from `path` inside the studio with no link in the way, else None."""
    data = safefs.read_bytes(root, path)
    if data is None:
        return None
    try:
        value = json.loads(data.decode("utf-8"))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def guard_view(root):
    """(real path of the attached repo, branch prefix) for the git guard, when this studio is attached: the
    `attach:` block of project.yaml is a mapping, or a state.json file is there (its content is not read, so a
    damaged or forged file still switches the guard on, and deleting it cannot switch the guard off). Tolerant of
    a bad block: the defaults fill what it cannot read."""
    block = raw(root)
    if not isinstance(block, dict) and not os.path.lexists(os.path.join(str(state_dir(root)), "state.json")):
        return None
    block = block if isinstance(block, dict) else {}
    repo = block.get("repo") if isinstance(block.get("repo"), str) else DEFAULTS["repo"]
    prefix = block.get("branch_prefix") if isinstance(block.get("branch_prefix"), str) else DEFAULTS["branch_prefix"]
    if not repo or repo in (".", "..") or "/" in repo:
        repo = DEFAULTS["repo"]
    return os.path.join(os.path.realpath(str(root)), repo), prefix


_TASK = re.compile(r"t-[0-9]{1,6}")


def read_branch_file(root, task):
    """`.alpaca/attach/branches/<task>.json` as a dict, or None. REPORT ONLY (`attach status`, doctor): the
    file is output of t-648 and any process in the studio can write it. A decision uses `task_branch`."""
    if not isinstance(task, str) or not _TASK.fullmatch(task):
        return None
    return _json_dict(root, state_dir(root) / "branches" / ("%s.json" % task))


def task_branch(root, task):
    """The proved facts of a task's work branch (the `attach-branch` event and the derived folder), or None.
    See `alpaca.attach.proven`; the shape is the one `branches/<task>.json` has."""
    from alpaca.attach import proven
    return proven.task_branch(root, task)


def task_checkout(root, task):
    """The task's work folder as a Path when its branch is proved and git agrees, else None."""
    from alpaca.attach import proven
    return proven.task_checkout(root, task)


def _studio_of(path):
    """The studio root a state path belongs to: the part before the LAST `.alpaca/attach` pair of its
    folders (last, so a studio that itself sits under another project's `.alpaca` is found), or None."""
    parts = Path(path).parts
    pair = tuple(STATE_DIR.split("/"))
    for i in range(len(parts) - len(pair), -1, -1):
        if i > 0 and parts[i:i + len(pair)] == pair:
            return Path(*parts[:i])
    return None


def write_json_atomic(path, payload, *, root=None) -> None:
    """Write JSON, mode 0600, parent folders made, through `safefs.write_bytes`: a link anywhere in the
    folders below the studio root, or at the file, is refused (AttachError) and nothing is written.
    `root` is the studio; without it the studio is the part of `path` before its last
    `.alpaca/attach`, and a path with no such part is refused instead of written unchecked."""
    if root is None:
        root = _studio_of(path)
        if root is None:
            raise AttachError("%s is not inside a studio's %s; attach writes only inside the studio"
                              % (Path(path).name, STATE_DIR))
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    safefs.write_bytes(root, path, data, mode=0o600)
