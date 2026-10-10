"""The verification driver: open, briefs, independence, both slots per round through
launcher.launch, verdict records, run, status, require_verified (design t-089, verifier.md
section 10).

One verification is the whole two-agent check of one sealed proof, `vf-<task>-<seal event>`:

    open      extract the claims, scan them and the sealed patch, pin the runtime binary and
              record `research-verify-open`
    round 0   both slots, each alone, on trees of their own; every claim in scope
    fold      agreed claims settle; disputed ones go to the next round (rounds 1 to 5)
    cap       at round 5 extend once to round 10 or rule, in code, with the reason recorded
    end       `research-verify-end`: verified, refuted, unverified or superseded

The driver writes every event; a verifier writes only its requests and its verdict, which the
driver serves (`serve`) and checks (`verdicts.check_verdict`). Code under test runs only in a
rerun jail the driver starts for one request. `proof.done_gate` reads the end through
`verify_gate.check`; the champion merge waits on `require_verified`.

What this build leans on that other op-009 tasks build: the headless launcher (t-098) through
the `launch` callable, its `runtime_in_use`, `resolve_binary` and `tool_calls` (each read here
with a stand-in until t-098 lands), the gate re-run `gates.verify_rerun` (t-093; a `--gate`
request is refused while it is a stub) and the engine's tree cutter, isolation.build_head_clone,
fresh_tree and git_env (t-091). The driver cuts every tree through them once they are built; until
then a stand-in cuts the trees the same way from a host git folder kept outside the try folder, so
no try folder holds a `.git` (_cut).
"""

from __future__ import annotations

import builtins
import concurrent.futures
import contextlib
import errno
import fcntl
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time

from alpaca import db, proof, util
from alpaca.gates import verdict as vc
from alpaca.research import (claims, events as rev, isolation, pathscan, rerun, verdicts,
                             verify_gate)
from alpaca.research.errors import ResearchError
from alpaca.research.types import JailSpec

#: section 5.3, item 4: what a verifier's own shell may run. The request client first, then the
#: read commands, then cd, pwd, echo and printf, then the read-only text tools of
#: pathscan.READ_FORMS (sed, sort and uniq only in the forms pathscan.read_form lets through; t-173
#: fix batch 3, F16). A `for` loop over written words is read as its body (pathscan.unroll).
#: Default-deny: anything else voids the pass.
#: In-session-runtime.md section 19 (t-228) makes it a pathscan.Grammar: the same names (so the launch
#: spec's `bash_allow` does not change), carrying the command table of 19.5 and the form of the
#: submission, which `pathscan.shell_allowed` reads as the strict grammar of the verifier's shell.
SHELL_ALLOW = pathscan.Grammar(("python3 packet/bin/rerun",) + pathscan.READ_COMMANDS +
                               ("cd", "pwd", "echo", "printf") + pathscan.READ_FORMS)
BRIEF_SCHEMA = "alpaca-verify-brief/1"
#: section 5.1: the only keys a first-pass brief may hold.
FIRST_KEYS = ("schema", "verification", "task", "slot", "round", "runtime", "claims", "seal",
              "evidence", "contract", "engine", "audit", "tree", "base", "out", "requests",
              "receipts", "rerun", "deadline_s", "refusal", "rules")
LATER_KEYS = FIRST_KEYS + ("disputed", "other_out")
TOOLS = ("Read", "Bash")
OTHER_OUT_CAP = 1 << 20
SERVE_POLL_S = 0.02
DEFAULT_TIMEOUT_S = 1800
ACTOR = "verifier"
WITHHELD = "<withheld>"

_fopen = builtins.open         # the module defines its own open() (verifier.md section 10.2)
_OPEN = rev.VERIFY_OPEN
_ASSIGN = rev.VERIFY_ASSIGN
_REFUSED = rev.VERIFY_REFUSED
_REVIEW = rev.VERIFY_REVIEW
_APPLY_FAILURE = rev.VERIFY_APPLY_FAILURE
_VERDICT = rev.PROOF_VERDICT
_ROUND = rev.VERIFY_ROUND
_EXTEND = rev.PROOF_EXTEND
_RULING = rev.PROOF_RULING
_END = rev.VERIFY_END
_LAUNCH_END = rev.LAUNCH_END

#: the refusal texts a relaunch brief carries (section 5.3, item 2): fixed per code and check,
#: naming no path, address or command the breach named and quoting nothing the verifier wrote.
_REFUSAL_TEXT = {
    "VERIFY-SCHEMA": "the verdict failed schema check %s",
    "VERIFY-RECEIPT": "the verdict failed receipt check %s",
    "VERIFY-BREACH": "tool call %s broke the %s rule of the verifier's jail",
    "VERIFY-PACKET-CHANGED": "packet/ changed during the pass",
    "VERIFY-NO-VERDICT": "the pass ended with no submitted verdict",
    "VERIFY-LOST": "the pass was lost before its verdict could be read",
}


class Stopped(Exception):
    """A driver stop recorded on the record (section 6.7)."""

    def __init__(self, code, event=None):
        super().__init__(code)
        self.code, self.event = code, event


# --------------------------------------------------------------------------- names and folders
def verification_id(task: str, seal_event: int) -> str:
    """"vf-<task>-<seal event>"; a restarted driver finds its own verification from it."""
    return verify_gate.verification_id(task, seal_event)


def _vf_dir(root, task, vf):
    return verify_gate.verification_dir(root, task, vf)


def _try_dir(root, task, vf, slot, rnd, try_):
    return os.path.join(_vf_dir(root, task, vf), slot, "r%d" % rnd, "t%d" % try_)


def _rel(root, path):
    return proof._rel(root, path)


def _session_of(vf, slot, rnd, try_):
    return "vs-%s-%s-r%d-t%d" % (vf, slot, rnd, try_)


def _key_of(vf, slot, rnd, try_):
    return "%s/%s/r%d/t%d" % (vf, slot, rnd, try_)


def _launch_id(key):
    """The launch id of a try's key: paths.launch_id's form (every "/" turned into "-") when that
    is a safe name, else a slug of it with a hash of the key as suffix. A checklist row id is in
    scope by policy (section 1) and holds a `.`, which paths.check_name refuses; the slug keeps
    its launch home inside `homes/` and distinct from every other try's."""
    from alpaca.research import paths as rpaths
    plain = str(key).replace("/", "-")
    if rpaths.NAME.fullmatch(plain):
        return rpaths.launch_id(key)
    tag = rerun.sha256_bytes(str(key))[:12]
    slug = re.sub(r"[^a-z0-9-]+", "-", plain.lower()).strip("-")[:64 - len(tag) - 1].strip("-")
    return rpaths.check_name("%s-%s" % (slug, tag) if slug else tag)


def _task_of(conn, vf):
    ev = verdicts.open_event(conn, vf)
    return ev["data"].get("task") if ev else None


def _dir_sha(path) -> str:
    """The sha256 of the sorted list of relative paths and file sha256 under `path`."""
    lines = []
    if os.path.isdir(path):
        for base, dirs, files in os.walk(path, followlinks=False):
            dirs.sort()
            for name in sorted(files):
                full = os.path.join(base, name)
                rel = os.path.relpath(full, path).replace(os.sep, "/")
                if os.path.islink(full):
                    lines.append("%s\0link:%s" % (rel, os.readlink(full)))
                    continue
                try:
                    lines.append("%s\0%s" % (rel, rerun.sha256_file(full)))
                except OSError:
                    lines.append("%s\0unreadable" % rel)
    return rerun.sha256_bytes("\n".join(sorted(lines)))


def _emit(conn, kind, task, op, session, *, actor=ACTOR, code=None, reason=None, data=None,
          run=None):
    return rev.emit(conn, kind, run=run, ref=task, op=op, session=session, actor=actor, code=code,
                    reason=reason, data=data)


def _problem(code, detail, *, check=None, rule=None, call=None, claim=None):
    return {"code": code, "check": check, "rule": rule, "call": call, "claim": claim,
            "detail": detail}


def _sealed_error(reason):
    """VERIFY-TREE for a sealed patch that does not apply or a kept or sealed file that no
    longer hashes to its sealed value: its problem carries the rule SEALED_RULE, which
    verify_gate reads to print the re-seal steps (section 8.4)."""
    return verdicts.error("VERIFY-TREE", reason, problems=[
        _problem("VERIFY-TREE", reason, rule=verify_gate.SEALED_RULE)])


def _rule_of(exc):
    """The rule of a VerifyError's first dict problem, or None."""
    for p in exc.problems or ():
        if isinstance(p, dict):
            return p.get("rule")
    return None


# --------------------------------------------------------------------------- runtime
def _markers(environ):
    env = os.environ if environ is None else environ
    claude = bool(env.get("CLAUDECODE") or env.get("CLAUDE_CODE_SESSION_ID"))
    codex = bool(env.get("CODEX_THREAD_ID") or env.get("CODEX_SANDBOX"))
    return claude, codex


def _runtime_in_use(environ=None):
    """The runtime whose marker the environment carries, or None for no marker or markers of
    both runtimes (section 5.5). The runtime table is the launcher's (t-098); until its
    `runtime_in_use` is built, the same markers are read here."""
    from alpaca.research import launcher
    try:
        return launcher.runtime_in_use(environ)
    except NotImplementedError:
        claude, codex = _markers(environ)
        if claude and codex:
            return None
        return "claude" if claude else ("codex" if codex else None)


def _resolve_binary(runtime):
    """The real path of the runtime's binary (section 5.5): `launcher.resolve_binary`, or until
    t-098 builds it a lookup of the runtime's name on PATH, never through a shell. A runtime
    the launcher cannot resolve (its LaunchError) stops with VERIFY-RUNTIME-MISMATCH."""
    from alpaca.research import launcher
    from alpaca.research.errors import LaunchError
    try:
        found = launcher.resolve_binary(runtime)
    except NotImplementedError:
        found = shutil.which(runtime)
    except LaunchError:
        found = None
    if not found:
        raise verdicts.error("VERIFY-RUNTIME-MISMATCH", "no %s binary is found" % runtime)
    return os.path.realpath(found)


def _tool_calls(stream, runtime, keep_ids: bool = False) -> list:
    """The tool calls of a captured stream: `launcher.tool_calls`, or until t-098 builds it the
    reader below of the two stream formats. `keep_ids` keeps each call's id, which a session
    launch needs to tell a call the guard denied from one that ran (jobs.ran_calls)."""
    from alpaca.research import launcher
    try:
        return launcher.tool_calls(stream, runtime, keep_ids=keep_ids)
    except NotImplementedError:
        return _read_stream(stream)


_READ_TOOLS = ("Read", "Grep", "Glob", "LS")
_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
_WEB_TOOLS = ("WebFetch", "WebSearch")


def _read_stream(path) -> list:
    """[{tool, kind, input, paths, ts, text, output}] from a Claude stream-json or a Codex JSON
    stream. A stand-in for `launcher.tool_calls` (t-098), which owns the reader."""
    calls, by_id = [], {}
    try:
        with _fopen(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        if rec.get("type") == "assistant":
            for b in msg.get("content") or []:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                name = str(b.get("name") or "?")
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                if name == "Bash":
                    kind, value, pths = "shell", str(inp.get("command") or ""), []
                elif name in _READ_TOOLS:
                    kind, value, pths = "read", inp, []
                elif name in _WRITE_TOOLS:
                    kind, value = "write", inp
                    pths = [p for p in (inp.get("file_path"), inp.get("notebook_path")) if p]
                elif name in _WEB_TOOLS:
                    kind, value, pths = "web", inp, []
                else:
                    kind, value, pths = "other", inp, []
                call = {"tool": name, "kind": kind, "input": value, "paths": pths,
                        "ts": rec.get("timestamp"), "text": None, "output": None}
                calls.append(call)
                if b.get("id"):
                    by_id[b["id"]] = call
        elif rec.get("type") == "user":
            for b in msg.get("content") or []:
                if not isinstance(b, dict) or b.get("type") != "tool_result":
                    continue
                call = by_id.get(b.get("tool_use_id"))
                if call is None:
                    continue
                content = b.get("content")
                if isinstance(content, list):
                    content = "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
                call["output"] = content if isinstance(content, str) else None
        elif rec.get("type") in ("item.started", "item.completed"):
            item = rec.get("item") if isinstance(rec.get("item"), dict) else {}
            ident = item.get("id")
            if item.get("type") == "command_execution":
                call = by_id.get(ident)
                if call is None:
                    call = {"tool": "exec", "kind": "shell", "input": str(item.get("command") or ""),
                            "paths": [], "ts": None, "text": None, "output": None}
                    calls.append(call)
                    if ident:
                        by_id[ident] = call
                if item.get("aggregated_output") is not None and rec["type"] == "item.completed":
                    call["output"] = str(item.get("aggregated_output"))
            elif item.get("type") == "file_change" and rec["type"] == "item.completed":
                pths = [c.get("path") for c in item.get("changes") or [] if isinstance(c, dict)]
                calls.append({"tool": "file_change", "kind": "write", "input": item, "paths": pths,
                              "ts": None, "text": None, "output": None})
            elif item.get("type") == "web_search" and rec["type"] == "item.completed":
                calls.append({"tool": "web_search", "kind": "web", "input": item, "paths": [],
                              "ts": None, "text": None, "output": None})
    return calls


#: injection points of the `proof verify` verb for tests: "verb_launch" stands in for the session
#: launch callable the verb uses (in-session-runtime.md section 2.2); None in a real project
_hooks = {"verb_launch": None}


def _pin(runtime, binary=None, session_mode=False):
    if session_mode:
        # a session run spawns its verifiers as subagents of the driving session: there is no
        # runtime binary to pin, and two None pins agree (in-session-runtime.md section 14)
        return None, None
    path = binary or _resolve_binary(runtime)
    path = os.path.realpath(path)
    if not os.path.isfile(path):
        raise verdicts.error("VERIFY-RUNTIME-MISMATCH", "the runtime binary %s is gone" % path)
    return path, rerun.sha256_file(path)


# --------------------------------------------------------------------------- the lock
@contextlib.contextmanager
def _locked(root, task, vf):
    folder = _vf_dir(root, task, vf)
    os.makedirs(folder, exist_ok=True)
    fd = os.open(os.path.join(folder, "lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise verdicts.error("VERIFY-BUSY", "another driver holds the lock of %s" % vf)
            raise
        yield fd
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- git helpers
#: pins this module adds after isolation.GIT_HARDEN on its own calls: a byte-for-byte checkout
#: and no automatic gc or maintenance in the middle of a cut (isolation.py adds the same kind)
_GIT_PINS = ("-c", "core.autocrlf=false", "-c", "gc.auto=0", "-c", "maintenance.auto=false")
_GIT_AUTHOR = {"GIT_AUTHOR_NAME": "alpaca", "GIT_AUTHOR_EMAIL": "alpaca@localhost",
               "GIT_COMMITTER_NAME": "alpaca", "GIT_COMMITTER_EMAIL": "alpaca@localhost"}


def _git_env(extra=None) -> dict:
    """The environment of every git call this module makes, built as isolation.git_env builds
    its base (architecture.md section 3.1): the runner's environment with every inherited GIT_*
    variable removed, then GIT_CONFIG_NOSYSTEM=1, GIT_CONFIG_GLOBAL=/dev/null and no terminal
    prompt, so neither the system nor the operator's global git config reaches a cut, a checkout
    or an apply; GIT_NO_REPLACE_OBJECTS=1, so a planted refs/replace/ entry in an agent-writable
    clone cannot swap the objects a cut reads. `extra` adds the host git folder of one tree or
    the author of a commit."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_TERMINAL_PROMPT="0",
               GIT_NO_REPLACE_OBJECTS="1")
    env.update(extra or {})
    return env


def _git(cwd, *args, check=True, binary=False, env=None):
    """One git call with isolation.GIT_HARDEN on its command line and the _git_env environment."""
    done = subprocess.run(("git",) + tuple(isolation.GIT_HARDEN) + _GIT_PINS + args, cwd=cwd,
                          capture_output=True, env=_git_env(env), timeout=900, text=not binary,
                          stdin=subprocess.DEVNULL)
    if check and done.returncode != 0:
        err = done.stderr if isinstance(done.stderr, str) else done.stderr.decode("utf-8", "replace")
        raise RuntimeError("git %s failed: %s" % (args[0], " ".join(err.split())[:300]))
    return done


def _isolation_built() -> bool:
    """True once t-091 has built isolation.build_head_clone, fresh_tree and git_env: the driver
    then cuts every tree through them (verifier.md section 5.6, item 1; architecture.md section
    3.1). While any of them is a stub, the stand-in cutter below cuts the trees the same way."""
    return not any(_stub(fn) for fn in (isolation.build_head_clone, isolation.fresh_tree,
                                        isolation.git_env))


def _git_home(folder) -> str:
    """verify_gate.try_git_home: where the stand-in keeps the git folders of a try's trees and
    where every cut writes `mounts.json`, beside the try folder and never inside it. The
    verifier's jail reads the try folder alone, so it reaches no git folder; a rerun jail
    reaches one only through the binds `mounts.json` names."""
    return verify_gate.try_git_home(folder)


def _resolve_commit(clone_git, commit) -> str:
    got = _git(None, "rev-parse", "--verify", "%s^{commit}" % (commit or "HEAD"),
               env={"GIT_DIR": clone_git})
    return got.stdout.strip()


def _stand_in_head_clone(root, head, folder) -> str:
    """The stripped clone of a task outside a run while isolation.build_head_clone is a stub
    (verifier.md section 5.6, item 1; architecture.md section 3.1): `git archive` of the sealed
    `head` with `:(exclude).alpaca`, `git init -b main` and one root commit, so no object
    reachable from a tree cut from it holds the record, a kept review or a proof report.
    Returns the root commit."""
    if os.path.lexists(folder):
        shutil.rmtree(folder)
    os.makedirs(folder)
    tar = folder.rstrip(os.sep) + ".tar"
    try:
        _git(root, "archive", "--format=tar", "-o", tar, head, "--", ".", ":(exclude).alpaca")
        got = subprocess.run(["tar", "-xf", tar, "-C", folder], capture_output=True, text=True)
        if got.returncode != 0:
            raise RuntimeError("tar failed: %s" % got.stderr.strip()[:200])
    finally:
        with contextlib.suppress(OSError):
            os.remove(tar)
    _git(folder, "init", "-q", "-b", "main")
    _git(folder, "add", "-A", "-f", ".")
    _git(folder, "commit", "-q", "--allow-empty", "--no-verify", "-m", "sealed head %s" % head,
         env=_GIT_AUTHOR)
    return _resolve_commit(os.path.join(folder, ".git"), "HEAD")


def _clone_tree(folder, commit):
    """The git tree id of `commit` in the head clone at `folder`, or None when the clone does
    not hold it. Replace refs are off (_git_env), so the id is the tree a cut reads."""
    if not commit or not os.path.isdir(os.path.join(folder, ".git")):
        return None
    got = _git(None, "rev-parse", "--verify", "%s^{tree}" % commit, check=False,
               env={"GIT_DIR": os.path.join(folder, ".git")})
    return got.stdout.strip() if got.returncode == 0 and got.stdout.strip() else None


def _build_head_clone(root, task, vf, head):
    """Build the stripped clone of a task outside a run at `<verification>/clone`, fresh: any
    folder already there goes first, with the clone.done an earlier build of this module wrote,
    since `.alpaca/` is agent-writable (section 13, item 8) and nothing read from it before the
    build is trusted. Through isolation.build_head_clone once t-091 has built it, else with the
    stand-in. Returns (CloneInfo, the tree id of its base commit)."""
    from alpaca.research.types import CloneInfo
    folder = os.path.join(_vf_dir(root, task, vf), "clone")
    for p in (folder, os.path.join(_vf_dir(root, task, vf), "clone.done")):
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        elif os.path.lexists(p):
            os.remove(p)
    if _isolation_built():
        info = isolation.build_head_clone(root, head=head, path=folder, stamp=util.now_iso())
    else:
        base = _stand_in_head_clone(root, head, folder)
        info = CloneInfo(path=folder, line="verify", branch="main", base_commit=base, head=base)
    tree = _clone_tree(folder, info.base_commit)
    if tree is None:
        raise RuntimeError("the head clone holds no base commit")
    return info, tree


def _head_clone(root, task, vf, tree):
    """The stripped clone every try's tree/ and base/ are cut from, for a task outside a run.
    `tree` is the open event's tree block: open built the clone fresh and recorded its base
    commit (`clone_base`) and that commit's tree id (`clone_tree`). The clone on disk is used
    only while its recorded base commit still holds that tree; otherwise it is built again, and
    a rebuild whose tree differs stops the driver with VERIFY-TREE and SEALED_RULE, so no try is
    cut from a tree other than the sealed head's. When the open could not build the clone, its
    event records `clone_tree` as null, and every cut builds the clone fresh from the sealed
    head and trusts nothing already in the folder. An open event with no `clone_tree` key at all
    was not written by this driver and stops with VERIFY-TREE. Returns a CloneInfo."""
    from alpaca.research.types import CloneInfo
    if "clone_tree" in tree and tree["clone_tree"] is None:
        return _build_head_clone(root, task, vf, tree.get("head"))[0]
    want, base = tree.get("clone_tree"), tree.get("clone_base")
    if not want:
        raise _sealed_error("the open event records no head clone tree")
    folder = os.path.join(_vf_dir(root, task, vf), "clone")
    if _clone_tree(folder, base) == want:
        return CloneInfo(path=folder, line="verify", branch="main", base_commit=base, head=base)
    info, got = _build_head_clone(root, task, vf, tree.get("head"))
    if got != want:
        raise _sealed_error("the head clone no longer holds the sealed head's tree (%s, recorded "
                            "%s)" % (got, want))
    return info


def _open_clone(root, run_id, clone_path, line):
    """The CloneInfo of an engine clone a tree is cut from: isolation.open_clone once t-091 has
    built it (the config and registry checks included), else the path as it is."""
    from alpaca.research.types import CloneInfo
    if _isolation_built() and not _stub(isolation.open_clone):
        info = isolation.open_clone(root, run_id, line=line)
        if info is None or os.path.realpath(info.path) != os.path.realpath(clone_path):
            raise verdicts.error("VERIFY-TREE", "the clone %s is not the run's %s clone"
                                 % (_rel(root, clone_path), line))
        return info
    if not os.path.isdir(os.path.join(clone_path, ".git")):
        raise verdicts.error("VERIFY-TREE", "the clone %s is missing" % _rel(root, clone_path))
    return CloneInfo(path=clone_path, line=line, branch="", base_commit="", head="")


def _cut(root, clone, dest, commit=None) -> dict:
    """A fresh tree of `clone` at `commit` (its head by default) at `dest`, so a write during a
    re-run reaches only this try's tree. Returns the cut: {"path", "commit", "env" (the host git
    environment of the tree, for the apply step), "jail" (what a rerun jail with this tree as
    its cwd binds for git: "ro", "rw", "git_hide", "admins" and "env")}.

    Once t-091 has built it, isolation.fresh_tree cuts the tree with its admin folder (its `.git`
    file names that folder), and the rerun jail binds the clone's `.git` read-only with
    `.git/worktrees/` hidden and the admin folder writable (verifier.md section 5.6, item 4).
    Until then the stand-in cuts it as fresh_tree does, from a host git folder of its own (a HEAD
    holding the commit, refs/ and objects/ with the clone's objects as its one alternate) filled
    with `git read-tree -u --reset`, but keeps that folder under _git_home, outside the try
    folder, so `dest` holds no `.git` at all; the rerun jail binds the host folder writable and
    names it in GIT_DIR."""
    clone_git = os.path.join(clone.path, ".git")
    commit = _resolve_commit(clone_git, commit or clone.head or None)
    hide = (clone_git,) if os.path.isdir(os.path.join(clone_git, "worktrees")) else ()
    if _isolation_built():
        for p in (dest, dest + ".git-host", dest + ".git-admin"):
            if os.path.lexists(p):
                shutil.rmtree(p)
        wt = isolation.fresh_tree(root, clone, commit=commit, path=dest, admin=True)
        host_env = {k: v for k, v in isolation.git_env(clone, worktree=wt).items()
                    if k.startswith("GIT_")}
        return {"path": dest, "commit": commit, "env": host_env,
                "jail": {"ro": [clone_git], "rw": [], "git_hide": list(hide),
                         "admins": [wt.admin_dir] if wt.admin_dir else [], "env": {}}}
    host = os.path.join(_git_home(os.path.dirname(dest)), os.path.basename(dest))
    for p in (dest, host):
        if os.path.lexists(p):
            shutil.rmtree(p)
    os.makedirs(os.path.join(host, "objects", "info"))
    os.makedirs(os.path.join(host, "refs"))
    util.write_text(os.path.join(host, "objects", "info", "alternates"),
                    os.path.join(clone_git, "objects") + "\n")
    util.write_text(os.path.join(host, "HEAD"), commit + "\n")
    os.makedirs(dest)
    env = {"GIT_DIR": host, "GIT_WORK_TREE": dest, "GIT_INDEX_FILE": os.path.join(host, "index")}
    _git(dest, "read-tree", "-u", "--reset", commit, env=env)
    return {"path": dest, "commit": commit, "env": env,
            "jail": {"ro": [clone_git], "rw": [host], "git_hide": list(hide), "admins": [],
                     "env": {"GIT_DIR": host, "GIT_WORK_TREE": dest}}}


def _write_mounts(folder, cuts: dict) -> None:
    """Keep what each tree's rerun jail binds for git in `<git home>/mounts.json`."""
    home = _git_home(folder)
    os.makedirs(home, exist_ok=True)
    _write_json(os.path.join(home, "mounts.json"),
                {name: cut["jail"] for name, cut in cuts.items()})


def _mounts(folder, cwd_name) -> dict:
    """The git binds of the rerun jail whose cwd is the try's `cwd_name` tree; {} when none."""
    try:
        with _fopen(os.path.join(_git_home(folder), "mounts.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    got = data.get(cwd_name) if isinstance(data, dict) else None
    return got if isinstance(got, dict) else {}


def _lay(src_dir, dest_dir) -> list:
    """Copy every file of `src_dir` over its path in `dest_dir`; [{path, sha256}]."""
    out = []
    if not os.path.isdir(src_dir):
        return out
    for base, dirs, files in os.walk(src_dir, followlinks=False):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(base, name)
            if os.path.islink(full):
                continue
            rel = os.path.relpath(full, src_dir)
            target = os.path.join(dest_dir, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if os.path.islink(target) or os.path.isdir(target):
                if os.path.isdir(target) and not os.path.islink(target):
                    shutil.rmtree(target)
                else:
                    os.remove(target)
            shutil.copyfile(full, target)
            out.append({"path": rel.replace(os.sep, "/"), "sha256": rerun.sha256_file(target)})
    return out


# --------------------------------------------------------------------------- open
def _open_events(conn, task, vf):
    return verify_gate.events(conn, _OPEN, task, verification=vf)


def _refuse(conn, task, op, session, vf, code, problems, *, slot=None, rnd=None, try_=None,
            worker=None, vsession=None, run=None, extra=None):
    data = {"verification": vf, "slot": slot, "round": rnd, "try": try_, "worker": worker,
            "session": vsession, "problems": problems, "verdict_sha256": None,
            "launch_end": None, "out_sha256": None, "refuted": []}
    data.update(extra or {})
    # section 7 records a refusal with code=FAIL; a budget hold is a pause, not a failure
    # (section 10.4 gives VERIFY-HOLD exit 3), so its row carries PAUSED
    band = vc.PAUSED if code == "VERIFY-HOLD" else vc.FAIL
    return _emit(conn, _REFUSED, task, op, session, code=band, reason=code, data=data, run=run)


def _stop(conn, task, op, session, vf, code, detail, *, run=None, slot=None, rule=None):
    ev = _refuse(conn, task, op, session, vf, code, [_problem(code, detail, rule=rule)], slot=None,
                 run=run)
    raise Stopped(code, ev)


def _scope_label(conn, root, task, seal_ev):
    sc = verify_gate.scope(conn, root, task, seal_ev)
    if sc is None:
        return "request"
    return sc


def open(conn, root, task, *, runtime, driver, session, binary=None, session_mode=False) -> dict:
    """Open the verification of the task's latest seal, or return the open event already on the
    record (section 6.8). Before the open: recover the tries earlier verifications of the task
    left with no outcome (section 6.10), refuse a task over REVERIFY_MAX (VERIFY-EXHAUSTED), and
    refuse a seal with no tree (VERIFY-TREE, records nothing). After it, the open-time refusals
    VERIFY-NO-CLAIMS, VERIFY-TOO-MANY-CLAIMS, VERIFY-STEERING and VERIFY-NO-COMMAND stop the
    driver (raise Stopped), as do a kept patch that no longer hashes to its sealed value and a
    head clone that cannot be built (VERIFY-TREE). Returns the open event's data with its `event` id."""
    seal_ev = proof.latest_seal(conn, task)
    if seal_ev is None:
        raise verdicts.error("VERIFY-NOT-SEALED", "%s has no proof-report event" % task)
    vf = verification_id(task, seal_ev["id"])
    op = seal_ev.get("op")
    existing = _open_events(conn, task, vf)
    if existing:
        data = dict(existing[0]["data"], event=existing[0]["id"])
        path = os.path.join(_vf_dir(root, task, vf), "claims.json")
        if not os.path.isfile(path) or rerun.sha256_file(path) != data.get("claims_sha256"):
            _stop(conn, task, op, session, vf, "VERIFY-CLAIMS-CHANGED",
                  "claims.json no longer hashes to the open event's value", run=data.get("run"))
        return data
    recovered = _recover_earlier(conn, root, task, before=seal_ev["id"], session=session,
                                 strict=True)
    pol = verify_gate.policy(root)
    runs = verify_gate.refuted_runs(conn, task, before=seal_ev["id"])
    earlier = verify_gate.earlier(conn, task, before=seal_ev["id"])
    if len(runs) > pol["reverify_max"]:
        _refuse(conn, task, op, session, vf, "VERIFY-EXHAUSTED",
                [_problem("VERIFY-EXHAUSTED", "%d earlier verifications count, over the bound of %d"
                          % (len(runs), pol["reverify_max"]))], extra={"earlier": earlier})
        raise verdicts.error("VERIFY-EXHAUSTED", "%s has %d counted earlier verifications, over "
                             "the bound of %d" % (task, len(runs), pol["reverify_max"]))
    sdata = seal_ev["data"] if isinstance(seal_ev.get("data"), dict) else {}
    research = verify_gate.research_of(conn, task)
    role = verify_gate.role_of(conn, task)
    run_id = research.get("run") if isinstance(research, dict) else None
    engine_block, tree = None, None
    if role in ("attempt", "repair"):
        src = claims.record_source(conn, task)
        if src is None or (role == "repair" and src["data"].get("outcome") != "fixed") \
                or not isinstance(src["data"].get("diff"), dict):
            raise verdicts.error("VERIFY-TREE", "%s has no record source to cut its trees from"
                                 % task)
        d = src["data"]
        frozen = os.path.join(_contract_dir(root, run_id), "files") if run_id else None
        engine_block = {"run": run_id, "attempt": task, "forked_from": d.get("forked_from"),
                        "diff_sha256": d["diff"].get("sha256"),
                        "frozen_sha256": _dir_sha(frozen) if frozen and os.path.isdir(frozen) else None}
    else:
        tree = sdata.get("tree") if isinstance(sdata.get("tree"), dict) else None
        if tree is None:
            raise verdicts.error("VERIFY-TREE", "seal event %s records no tree; seal it again with "
                                 "alpaca proof seal %s --verify" % (seal_ev["id"], task))
    try:
        cl = claims.extract(conn, root, task, seal_ev)
    except (ValueError, OSError) as exc:
        raise verdicts.error("VERIFY-TREE", str(exc))
    written = claims.write(root, vf, cl)
    pin, pin_sha = _pin(runtime, binary, session_mode)
    patch_text = None
    folder = _vf_dir(root, task, vf)
    if tree is not None:
        kept = os.path.join(root, str(tree.get("patch") or "").replace("/", os.sep))
        if os.path.isfile(kept) and rerun.sha256_file(kept) == tree.get("patch_sha256"):
            shutil.copyfile(kept, os.path.join(folder, "tree.patch"))
            with _fopen(kept, "rb") as fh:
                patch_text = fh.read().decode("utf-8", "replace")
    head_clone, clone_failed = None, None
    if tree is not None:
        # the head clone is built here, fresh, and its base commit and tree id go on the open
        # event: every later cut checks the clone against them (_head_clone), so a clone or a
        # clone.done planted in the agent-writable verification folder is never trusted. A
        # clone that cannot be built (a full disk, a git error) records the open with both
        # values null and then stops the driver with VERIFY-TREE (section 6.7: a tree that
        # cannot be cut stops after the open event); a resume clears that cause, and every cut
        # of that verification builds its clone fresh (_head_clone)
        try:
            info, clone_tree = _build_head_clone(root, task, vf, tree.get("head"))
            head_clone = {"clone_base": info.base_commit, "clone_tree": clone_tree}
        except (OSError, RuntimeError, subprocess.SubprocessError, ResearchError) as exc:
            head_clone = {"clone_base": None, "clone_tree": None}
            clone_failed = str(exc)
    steer = claims.steering(cl, patch_text if role is None or role == "report" else None)
    audit_event = None
    if role == "attempt":
        src = claims.record_source(conn, task)
        audit_event = src["data"].get("audit_event") if src else None
    data = {"verification": vf, "task": task, "seal_event": seal_ev["id"],
            "report_sha256": sdata.get("sha256"), "scope": _scope_label(conn, root, task, seal_ev),
            "policy": {k: pol[k] for k in ("scope", "ops", "reverify_max", "error")},
            "runtime": runtime, "driver": driver,
            "claims_path": _rel(root, written["json"]), "claims_sha256": written["sha256"],
            "claims": len(cl["claims"]), "claim_ids": [c["id"] for c in cl["claims"]],
            "skipped": [{"index": s.get("index"), "code": s.get("code"), "text": s.get("text")}
                        for s in cl["skipped"]],
            "contracts": cl["contracts"], "steering_exempt": steer["exempt"],
            "binary": pin, "binary_sha256": pin_sha, "audit_event": audit_event,
            "tree": (dict({"head": tree.get("head"), "patch_sha256": tree.get("patch_sha256")},
                          **head_clone) if tree else None),
            "engine": engine_block, "cap": verdicts.ROUND_CAP, "earlier": earlier,
            "session_mode": bool(session_mode),
            "recovered": recovered,
            "reverify": {"count": len(runs), "max": pol["reverify_max"]}, "role": role}
    ev = _emit(conn, _OPEN, task, op, session, data=data, run=run_id)
    data["event"] = ev["id"]
    # open-time refusals: the cause lives in the sealed report or the sealed tree
    if not cl["claims"]:
        _stop(conn, task, op, session, vf, "VERIFY-NO-CLAIMS", "the extractor found no claim",
              run=run_id)
    if len(cl["claims"]) > verdicts.MAX_CLAIMS:
        _stop(conn, task, op, session, vf, "VERIFY-TOO-MANY-CLAIMS",
              "%d claims, over %d; split the report" % (len(cl["claims"]), verdicts.MAX_CLAIMS),
              run=run_id)
    if steer["hits"]:
        h = steer["hits"][0]
        where = "claim %s" % h["claim"] if h.get("claim") else "%s line %s" % (h["file"], h["line"])
        _stop(conn, task, op, session, vf, "VERIFY-STEERING",
              "%s holds \"%s\"" % (where, h["phrase"]), run=run_id)
    blocks = cl.get("blocks") or []
    if not blocks:
        _stop(conn, task, op, session, vf, "VERIFY-NO-COMMAND",
              "the How to reproduce section holds no fenced command block", run=run_id)
    if pathscan.reads_only(blocks, gates=role in ("attempt", "repair")):
        _stop(conn, task, op, session, vf, "VERIFY-NO-COMMAND",
              "the How to reproduce section only reads or prints files", run=run_id)
    loose = next((c for c in cl["claims"] if not claims.bound(c)), None)
    if loose is not None:
        # every claim names what a receipt of the verifier's own try checks (t-296, design 25.2);
        # a claim that names none could be supported on any receipt, so nothing is launched
        _stop(conn, task, op, session, vf, "VERIFY-NO-COMMAND",
              "claim %s binds no command, pointer or diff; re-seal the report so it is rendered "
              "by the current code" % loose.get("id"), run=run_id)
    if tree is not None and patch_text is None:
        _stop(conn, task, op, session, vf, "VERIFY-TREE",
              "the kept tree.patch no longer hashes to its sealed value", run=run_id,
              rule=verify_gate.SEALED_RULE)
    if clone_failed is not None:
        # no SEALED_RULE: the cause is on the host, not in the seal, so a resume can clear it
        _stop(conn, task, op, session, vf, "VERIFY-TREE",
              "a fresh tree could not be cut: %s" % clone_failed, run=run_id)
    return data


def _contract_dir(root, run):
    from alpaca.research import paths as rpaths
    return rpaths.contract_dir(root, run)


# --------------------------------------------------------------------------- independence
def check_independence(conn, task, *, worker, session, record_session) -> None:
    """Refuse a verifier whose worker id or session belongs to the work under test (section
    5.4): record `research-verify-refused` with VERIFY-SELF before anything is launched, and
    raise VerifyError."""
    ids = verify_gate.work_identities(conn, task)
    if worker not in ids["workers"] and session not in ids["sessions"]:
        return
    vf, _, slot = str(worker).partition("/")
    seal_ev = proof.latest_seal(conn, task)
    op = seal_ev.get("op") if seal_ev else None
    _refuse(conn, task, op, record_session, vf, "VERIFY-SELF",
            [_problem("VERIFY-SELF", "the verifier %s (session %s) belongs to the work under test"
                      % (worker, session))], worker=worker, vsession=session)
    raise verdicts.error("VERIFY-SELF", "the verifier is the implementer of %s" % task)


def first_pass(conn, verification: str, slot: str) -> bool:
    """True while the slot holds no counted, non-synthetic verdict in this verification."""
    for ev in verify_gate.events(conn, _VERDICT, verification=verification):
        if ev["data"].get("verifier") == slot and not ev["data"].get("synthetic"):
            return False
    return True


# --------------------------------------------------------------------------- the brief
def _formation_role(root, formation, role):
    from alpaca.formation import manifest
    last = None
    for base in (root, os.path.dirname(os.path.dirname(os.path.abspath(proof.__file__)))):
        try:
            m = manifest.registry(base).get(formation)
            m.role(role)
            return base, m
        except Exception as exc:
            last = exc
    raise verdicts.error("VERIFY-BRIEF-NOT-BLIND", "formation %s has no role %s (%s)"
                         % (formation, role, last))


def role_tools(root, formation="builder-verifier", role="verifier") -> list:
    """The tools of the role's archetype (agents/<archetype>.md), which for the blind verifier
    role gives Read and Bash and no Write or Edit (agents/red.md:8)."""
    base, m = _formation_role(root, formation, role)
    arche = m.role(role).get("archetype")
    for folder in (root, base, os.path.dirname(os.path.dirname(os.path.abspath(proof.__file__)))):
        path = os.path.join(folder, "agents", "%s.md" % arche)
        if os.path.isfile(path):
            for line in util.read_text(path).splitlines():
                if line.startswith("tools:"):
                    return [t.strip() for t in line.split(":", 1)[1].split(",") if t.strip()]
    return list(TOOLS)


def _prompt_text():
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts", "verify.md")
    return util.read_text(here)


def render_prompt(brief_path="packet/brief.json") -> str:
    """The verifier's prompt: prompts/verify.md with the brief's path."""
    return _prompt_text().replace("{brief}", brief_path)


def _export_event(conn, eid):
    r = conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
    if r is None:
        return None
    from alpaca import lineage
    try:
        lin = lineage.read(conn)
    except Exception:
        lin = None
    return {"id": r["id"], "ts": r["ts"], "session": r["session"], "actor": r["actor"],
            "kind": r["kind"], "op": r["op"], "ref": r["ref"], "data": r["data"],
            "content_hash": r["content_hash"], "prev_hash": r["prev_hash"], "hash": r["hash"],
            "tag": lineage.event_tag(lin, r["id"]) or db.EVENT_TAG}


def _place_evidence(conn, root, seal_data, cl, packet):
    """Copy every sealed evidence item the claims name into the packet, each checked against its
    sealed value (VERIFY-TREE otherwise). Returns the brief's `evidence` value."""
    placed = []
    seen = set()
    for c in cl["claims"]:
        for s in c.get("sealed") or []:
            copy = s.get("copy")
            if not copy or copy in seen:
                continue
            seen.add(copy)
            ptr = s.get("pointer")
            target = os.path.join(os.path.dirname(packet), copy)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if copy == "packet/diff.patch":
                continue
            if ptr.startswith("event:"):
                row = _export_event(conn, int(ptr.split(":", 1)[1]))
                if row is None or row["hash"] != s.get("sha256"):
                    raise _sealed_error("evidence %s no longer carries its sealed chain hash"
                                        % ptr)
                util.write_text(target, json.dumps(row, indent=1, sort_keys=True) + "\n")
            else:
                item = next((e for e in seal_data.get("evidence") or []
                             if isinstance(e, dict) and e.get("pointer") == ptr), {})
                sources = []
                if item.get("kept"):
                    sources.append(os.path.join(root, str(item["kept"]).replace("/", os.sep)))
                src = proof._evidence_file(root, item.get("kind"), ptr)
                if src:
                    sources.append(src)
                chosen = None
                for cand in sources:
                    if os.path.isfile(cand) and not os.path.islink(cand) \
                            and rerun.sha256_file(cand) == s.get("sha256"):
                        chosen = cand
                        break
                if chosen is None:
                    raise _sealed_error("evidence %s no longer hashes to its sealed value" % ptr)
                shutil.copyfile(chosen, target)
            placed.append({"pointer": ptr, "copy": copy})
    return {"copies": placed}


def _cut_trees(conn, root, task, vf, open_data, folder, packet):
    """tree/ and base/ of one try (section 5.6, item 1), plus the packet's diff and frozen files
    for an attempt or repair task. Returns the brief's `seal`-side facts."""
    tree_dir, base_dir = os.path.join(folder, "tree"), os.path.join(folder, "base")
    role = open_data.get("role")
    out = {"base_differs": None, "engine": None}
    try:
        if role in ("attempt", "repair"):
            src = claims.record_source(conn, task)
            d = src["data"]
            eng = open_data.get("engine") or {}
            run_id = eng.get("run")
            from alpaca.research import paths as rpaths
            if role == "repair" and d.get("clone"):
                clone = d["clone"] if os.path.isabs(d["clone"]) else os.path.join(root, d["clone"])
                line = os.path.basename(os.path.normpath(clone))
            else:
                research = verify_gate.research_of(conn, task) or {}
                line = research.get("repair_task") or "champion"
                clone = rpaths.clone_dir(root, run_id, line)
            clone = _open_clone(root, run_id, clone, line)
            diff = d.get("diff") or {}
            dpath = diff.get("path") or ""
            dfull = dpath if os.path.isabs(dpath) else os.path.join(root, dpath)
            if not os.path.isfile(dfull) or rerun.sha256_file(dfull) != diff.get("sha256") \
                    or diff.get("sha256") != eng.get("diff_sha256"):
                raise verdicts.error("VERIFY-TREE", "the recorded diff no longer hashes to its "
                                     "recorded value")
            shutil.copyfile(dfull, os.path.join(packet, "diff.patch"))
            frozen_src = os.path.join(_contract_dir(root, run_id), "files") if run_id else None
            frozen = os.path.join(packet, "frozen")
            os.makedirs(frozen, exist_ok=True)
            if frozen_src and os.path.isdir(frozen_src):
                _lay(frozen_src, frozen)
            cjson = os.path.join(_contract_dir(root, run_id), "contract.json") if run_id else None
            if cjson and os.path.isfile(cjson):
                shutil.copyfile(cjson, os.path.join(packet, "research-contract.json"))
            cuts = {"tree": _cut(root, clone, tree_dir, d.get("forked_from")),
                    "base": _cut(root, clone, base_dir, d.get("forked_from"))}
            _write_mounts(folder, cuts)
            _lay(frozen, base_dir)
            for top in (tree_dir, base_dir):
                bad = _escaping_link(top)
                if bad is not None:
                    raise verdicts.error("VERIFY-TREE", "the cut tree holds a link that leaves it: "
                                         "%s -> %s" % (os.path.relpath(bad[0], top).replace(os.sep, "/"),
                                                       bad[1]))
            stages = _stages(conn, root, task, vf)
            out["engine"] = {"diff": "packet/diff.patch", "diff_sha256": diff.get("sha256"),
                             "empty": bool(diff.get("empty")), "forked_from": d.get("forked_from"),
                             "contract": "packet/research-contract.json"
                             if os.path.isfile(os.path.join(packet, "research-contract.json")) else None,
                             "frozen": "packet/frozen", "run": run_id,
                             "stages": stages,
                             # the receipt id of each gate item on each side, which a `--gate`
                             # request with no id of its own is given (rerun.gate_ids)
                             "gate_receipts": rerun.gate_ids(stages)}
            return out
        tree = open_data.get("tree") or {}
        clone = _head_clone(root, task, vf, tree)
        cuts = {"tree": _cut(root, clone, tree_dir), "base": _cut(root, clone, base_dir)}
        _write_mounts(folder, cuts)
        patch = os.path.join(_vf_dir(root, task, vf), "tree.patch")
        if not os.path.isfile(patch) or rerun.sha256_file(patch) != tree.get("patch_sha256"):
            raise _sealed_error("the kept tree.patch no longer hashes to its sealed value")
        shutil.copyfile(patch, os.path.join(packet, "tree.patch"))
        if os.path.getsize(patch) > 0:
            got = _git(tree_dir, "apply", "--binary", "--whitespace=nowarn", patch, check=False,
                       env=cuts["tree"]["env"])
            if got.returncode != 0:
                raise _sealed_error("the sealed tree.patch does not apply")
        out["base_differs"] = os.path.getsize(patch) > 0
        if role == "report":
            _report_files(conn, root, task, tree_dir)
        for top in (tree_dir, base_dir):
            bad = _escaping_link(top)
            if bad is not None:
                raise _sealed_error("the sealed tree holds a link that leaves it: %s -> %s"
                                    % (os.path.relpath(bad[0], top).replace(os.sep, "/"), bad[1]))
        return out
    except verdicts.VerifyError:
        raise
    except (OSError, RuntimeError, subprocess.SubprocessError, ResearchError) as exc:
        raise verdicts.error("VERIFY-TREE", "a fresh tree could not be cut: %s" % exc)


def _escaping_link(top):
    """(path, target) of the first symbolic link under `top` (its `.git` left out) whose target
    is absolute or resolves outside `top`, else None. A verifier reading through such a link
    from its own shell reaches a path the lexical scan places inside its try folder, such as the
    other slot's receipts (section 5.3, item 4), so a tree that holds one is not handed out."""
    real_top = os.path.realpath(top)
    for here, dirs, files in os.walk(top):
        if here == top:
            dirs[:] = [d for d in dirs if d != ".git"]
            files = [f for f in files if f != ".git"]
        for name in sorted(dirs) + sorted(files):
            full = os.path.join(here, name)
            if not os.path.islink(full):
                continue
            target = os.readlink(full)
            real = os.path.realpath(full)
            if os.path.isabs(target) or not (real == real_top
                                             or real.startswith(real_top.rstrip(os.sep) + os.sep)):
                return full, target
    return None


def _drop_escaping_links(folder) -> list:
    """Remove every link under the try's `tree/` and `base/` whose target is absolute or leaves
    that tree, and return what was removed as [(path relative to the try folder, target)]. A
    re-run in `tree/` or `base/` runs the jail with that tree writable (a `bin/rerun --shell
    'ln -s ...'` makes a link there), and a verifier's own shell reading through such a link
    (`grep -R`) reaches another slot's folder by a path the lexical scan places inside its own
    (in-session-runtime.md section 18.7). The driver calls this after every re-run and every
    apply, before the answer is served, so no link of that kind outlives the call that made it."""
    gone = []
    for sub in ("tree", "base"):
        top = os.path.join(folder, sub)
        for _ in range(100000):
            bad = _escaping_link(top)
            if bad is None:
                break
            full, target = bad
            with contextlib.suppress(OSError):
                os.unlink(full)
            gone.append((os.path.relpath(full, folder).replace(os.sep, "/"), target))
            if os.path.lexists(full):
                break
    return gone


def _stages(conn, root, task, vf) -> list:
    """The chain items a `--gate` request may name: the stages the task's `research-gate` events
    record and the gate blocks of its claims. `gates.verify_rerun` checks the frozen contract
    itself (architecture.md section 3.3)."""
    out = {e["data"].get("stage") for e in verify_gate.events(conn, rev.GATE, task)
           if e["data"].get("stage")}
    try:
        with _fopen(os.path.join(_vf_dir(root, task, vf), "claims.json"), encoding="utf-8") as fh:
            cl = json.load(fh)
        for c in cl.get("claims") or []:
            for entry in c.get("commands") or []:
                if "gate" in entry:
                    out.add(entry["gate"])
    except (OSError, ValueError):
        pass
    return sorted(out)


def _report_files(conn, root, task, tree_dir):
    """The run report task's sealed report files, copied into tree/ at their own paths. A file
    the seal kept no copy of because of proof's size caps (an events.json over
    proof.KEPT_FILE_CAP, t-289), or bound by hash alone because the dashboard's browser does not
    serve it (`proof.IN_PLACE_REASONS`, op008/followups review B-2), is copied from its live path
    instead; either way the copy in tree/ must hash to the sealed value."""
    seal_ev = proof.latest_seal(conn, task)
    for item in (seal_ev["data"].get("evidence") or []) if seal_ev else []:
        if not isinstance(item, dict) or item.get("kind") != "local":
            continue
        body = str(item.get("pointer")).split(":", 1)[1].strip()
        if not body.startswith(".alpaca/reports/research/"):
            continue
        if item.get("kept"):
            source = os.path.join(root, str(item["kept"]).replace("/", os.sep))
            what = "the kept copy of %s" % body
        elif item.get("kept_reason") in proof.IN_PLACE_REASONS:
            # bound by hash with no copy (over the caps, or a file the browser does not serve,
            # op008/followups review B-2): read from the tree and checked against the sealed hash
            source = os.path.join(root, body.replace("/", os.sep))
            what = "%s (kept no copy: %s)" % (body, "over cap" if item["kept_reason"] == proof.OVER_CAP
                                               else "hash only")
        else:
            raise _sealed_error("the kept copy of %s no longer hashes to its sealed value" % body)
        if not os.path.isfile(source) or os.path.islink(source):
            raise _sealed_error("%s no longer hashes to its sealed value" % what)
        target = os.path.join(tree_dir, body)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(source, target)
        # hash the copy, not the source: the live file may change between a check and a copy
        if rerun.sha256_file(target) != item.get("sha256"):
            with contextlib.suppress(OSError):
                os.unlink(target)
            raise _sealed_error("%s no longer hashes to its sealed value" % what)


def _scope_claims(conn, root, task, vf, rnd):
    path = os.path.join(_vf_dir(root, task, vf), "claims.json")
    with _fopen(path, encoding="utf-8") as fh:
        cl = json.load(fh)
    scope = verdicts.round_scope(conn, vf, rnd)
    return cl, [c for c in cl["claims"] if c["id"] in scope]


def _refusal_key(conn, vf, slot, rnd, forbidden):
    """The problems of this slot's own refused tries in this round, in the fixed form."""
    rows = [ev for ev in verify_gate.events(conn, _REFUSED, verification=vf)
            if ev["data"].get("slot") == slot and ev["data"].get("round") == rnd]
    if not rows:
        return None
    out = []
    for p in rows[-1]["data"].get("problems") or []:
        if not isinstance(p, dict):
            continue
        code = p.get("code") or rows[-1]["data"].get("code")
        text = _REFUSAL_TEXT.get(code, "the try was refused with %s" % code)
        if code == "VERIFY-BREACH":
            text = text % (p.get("call"), p.get("rule"))
        elif "%s" in text:
            text = text % (p.get("check"),)
        if p.get("claim"):
            text += " on claim %s" % p.get("claim")
        out.append({"code": code, "check": p.get("check"), "rule": p.get("rule"),
                    "call": p.get("call"), "claim": p.get("claim"), "text": text})
    return withhold(out, forbidden)


def _latest_entries(conn, vf, slot):
    """{claim id: (entry, verdict event)} of the slot's latest counted, non-synthetic entries."""
    out = {}
    for ev in verify_gate.events(conn, _VERDICT, verification=vf):
        d = ev["data"]
        if d.get("verifier") != slot or d.get("synthetic"):
            continue
        for c in d.get("claims") or []:
            if isinstance(c, dict):
                out[c.get("id")] = (c, ev)
    return out


def build_brief(conn, root, verification, slot, round, try_: int, *, formation="builder-verifier",
                role="verifier") -> dict:
    """Cut the try's trees, fill its packet and write `packet/brief.json` (section 5.1): a
    FIRST_KEYS brief for a slot in its first pass, whatever the round; a later-round brief with
    `disputed` and `other_out` for a slot past it (section 5.7). The narrative sections go into
    the payload under `narrative` and `manifest.payload_for` drops them for the blind role.
    Returns the brief."""
    from alpaca.formation import manifest
    task = _task_of(conn, verification)
    open_ev = verdicts.open_event(conn, verification)
    od = open_ev["data"]
    folder = _try_dir(root, task, verification, slot, round, try_)
    packet = os.path.join(folder, "packet")
    for sub in ("packet", "packet/bin", "out", "requests", "receipts", "launch"):
        os.makedirs(os.path.join(folder, sub), exist_ok=True)
    cpath = os.path.join(_vf_dir(root, task, verification), "claims.json")
    if not os.path.isfile(cpath) or rerun.sha256_file(cpath) != od.get("claims_sha256"):
        raise verdicts.error("VERIFY-CLAIMS-CHANGED", "claims.json no longer hashes to the open "
                             "event's value")
    cl, scope = _scope_claims(conn, root, task, verification, round)
    seal_ev = verify_gate.event_by_id(conn, od.get("seal_event"))
    sdata = seal_ev["data"] if seal_ev else {}
    facts = _cut_trees(conn, root, task, verification, od, folder, packet)
    evidence = _place_evidence(conn, root, sdata, cl, packet)
    shutil.copyfile(os.path.join(os.path.dirname(os.path.abspath(__file__)), "rerun.py"),
                    os.path.join(packet, "bin", "rerun"))
    util.write_text(os.path.join(packet, "claims.json"),
                    json.dumps({"verification": verification, "round": round, "claims": scope},
                               indent=1, sort_keys=True) + "\n")
    withheld = {s.get("index") for s in cl.get("skipped") or []
                if s.get("code") == "VERIFY-EVIDENCE-FORBIDDEN"}
    seal_items = [{"pointer": e.get("pointer"), "sha256": e.get("sha256"), "bytes": e.get("bytes")}
                  for pos, e in enumerate(sdata.get("evidence") or [], 1)
                  if isinstance(e, dict) and pos not in withheld and e.get("kind") != "remote"]
    tree = sdata.get("tree") if isinstance(sdata.get("tree"), dict) else None
    seal_key = {"event": od.get("seal_event"), "report_sha256": od.get("report_sha256"),
                "tree": ({"head": tree.get("head"), "patch": "packet/tree.patch",
                          "patch_sha256": tree.get("patch_sha256"), "bytes": tree.get("bytes")}
                         if tree else None),
                "base_differs": facts.get("base_differs"),
                "steering_exempt": od.get("steering_exempt") or [],
                "evidence": seal_items}
    contract = claims.contract_view(conn, task, events=od.get("contracts") or [])
    rules = render_prompt()
    brief = {"schema": BRIEF_SCHEMA, "verification": verification, "task": task, "slot": slot,
             "round": round, "runtime": od.get("runtime"), "claims": scope, "seal": seal_key,
             "evidence": evidence, "contract": contract,
             "tree": "tree", "base": "base", "out": "out", "requests": "requests",
             "receipts": "receipts", "rerun": "packet/bin/rerun",
             "deadline_s": None, "rules": {"text": rules, "sha256": rerun.sha256_bytes(rules)}}
    if facts.get("engine"):
        brief["engine"] = facts["engine"]
    if od.get("audit_event") is not None:
        # the audit event, with packet/audit.json and packet/audit.log (sections 5.1 and 9.7)
        from alpaca.research import audit
        brief["audit"] = audit._for_verifier(conn, root, od.get("audit_event"), packet)
    fp = first_pass(conn, verification, slot)
    if not fp:
        other = "v2" if slot == "v1" else "v1"
        mine, theirs = _latest_entries(conn, verification, slot), _latest_entries(conn, verification, other)
        disputed = {}
        other_dir = os.path.join(packet, "other_out")
        for c in scope:
            this = mine.get(c["id"])
            that = theirs.get(c["id"])
            disputed[c["id"]] = {"this": _shown(this, None), "other": _shown(that, other)}
            if that:
                _copy_other(root, task, verification, other, that, other_dir)
        brief["disputed"] = disputed
        brief["other_out"] = "packet/other_out"
    # the narrative the blind role never receives
    report = {}
    try:
        text = util.read_text(os.path.join(root, str(sdata.get("path") or "")))
        found = proof.split_sections(text)
        report = {k: found.get(k) for k in ("What I did", "How I did it", "Where",
                                             "Deviations and issues", proof.APPENDIX)}
    except OSError:
        report = {}
    base, m = _formation_role(root, formation, role)
    payload = manifest.payload_for(m, role, dict(brief, narrative=report))
    return payload


def _shown(entry_ev, other):
    """A last counted entry as a later-round brief shows it: the verdict, reason, evidence with
    its receipt fields, failure scenario, conditions and note, and no path of the verification
    folders (a copy under packet/other_out/ for the other slot's receipts)."""
    if not entry_ev:
        return None
    entry, ev = entry_ev
    items = []
    for e in entry.get("evidence") or []:
        item = {k: e.get(k) for k in ("receipt", "kind", "sha256", "key", "result")}
        if other and e.get("receipt"):
            item["copy"] = "packet/other_out/%s-r%s-%s.json" % (other, ev["data"].get("round"),
                                                              e["receipt"])
        items.append(item)
    return {"verdict": entry.get("verdict"), "effective": entry.get("effective"),
            "reason": entry.get("reason"), "evidence": items,
            "failure_scenario": entry.get("failure_scenario"),
            "conditions": entry.get("conditions") or [], "note": entry.get("note"),
            "round": ev["data"].get("round")}


def _copy_other(root, task, vf, other, entry_ev, dest):
    """Copies of the other slot's receipts and outputs a disputed entry cites, cut at 1 MiB,
    with every path of this task's verification folders withheld."""
    entry, ev = entry_ev
    d = ev["data"]
    folder = _try_dir(root, task, vf, other, d.get("round"), d.get("try"))
    os.makedirs(dest, exist_ok=True)
    prefix_rel = ".alpaca/verify/%s/" % verify_gate.slug(task)
    prefix_abs = os.path.join(root, ".alpaca", "verify", verify_gate.slug(task)) + os.sep
    for item in entry.get("evidence") or []:
        rid = item.get("receipt")
        if not rid:
            continue
        for suffix in (".json", ".out"):
            src = os.path.join(folder, "receipts", rid + suffix)
            if not os.path.isfile(src) or os.path.islink(src):
                continue
            with _fopen(src, "rb") as fh:
                data = fh.read(OTHER_OUT_CAP)
            text = data.decode("utf-8", "replace").replace(prefix_abs, WITHHELD + "/") \
                .replace(prefix_rel, WITHHELD + "/")
            util.write_text(os.path.join(dest, "%s-r%s-%s%s" % (other, d.get("round"), rid, suffix)),
                            text)


# --------------------------------------------------------------------------- blindness
_HEX = re.compile(r"[0-9a-f]{64}")


def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _walk_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_strings(v)
    elif obj is not None:
        yield str(obj)


def forbidden_tokens(conn, root, verification, slot, brief: dict) -> dict:
    """{"tokens", "ids", "hashes", "events", "traces", "trace_sizes"}: what no brief or packet
    file of `slot` may hold (section 5.3, item 2): this task's verification folder prefix, the
    ids and sha256 of its kept reviews, the `event:<id>` and `events/<id>.json` of its
    verification events, the other slot's launch homes, and the sha256 of every receipt, output
    and submitted verdict of the other slot, less the values the slot's own inputs carry by
    design. Every earlier verification of the task (section 5.2) adds its id to `ids`, matched
    where no digit follows, and the hashes of verify_gate.earlier_traces to both `hashes` and
    `traces`; the shared-value rule never removes those, and assert_blind also looks for them
    in the try's trees."""
    task = brief.get("task") or _task_of(conn, verification)
    other = "v2" if slot == "v1" else "v1"
    prefix = ".alpaca/verify/%s/" % verify_gate.slug(task)
    tokens = [prefix, os.path.join(root, ".alpaca", "verify", verify_gate.slug(task)) + os.sep]
    hashes = set()
    for ev in verify_gate.events(conn, _REVIEW, task):
        if ev["data"].get("review"):
            tokens.append(str(ev["data"]["review"]))
        if ev["data"].get("sha256"):
            tokens.append(str(ev["data"]["sha256"]))
    rows = conn.execute("SELECT id, kind FROM events WHERE ref=? AND (kind LIKE 'research-verify-%' "
                        "OR kind LIKE 'research-proof-%')", (task,)).fetchall()
    event_ids = [r["id"] for r in rows]
    for ev in verify_gate.events(conn, _ASSIGN, verification=verification):
        if ev["data"].get("slot") == other and ev["data"].get("home"):
            tokens.append(str(ev["data"]["home"]))
    other_dir = os.path.join(_vf_dir(root, task, verification), other)
    for base, dirs, files in os.walk(other_dir):
        if os.path.basename(base) != "receipts":
            continue
        for name in files:
            full = os.path.join(base, name)
            if not os.path.islink(full):
                hashes.add(rerun.sha256_file(full))
    # shared values: the slot's own inputs, fixed before the verification opened
    shared = set()
    for key in ("seal", "claims", "contract", "engine", "audit"):
        for s in _walk_strings(brief.get(key)):
            shared.update(_HEX.findall(s))
    try_packets = []
    if isinstance(brief.get("_packet"), str):
        try_packets.append(brief["_packet"])
    for pk in try_packets:
        for name in ("evidence", "events", "frozen", "tree.patch", "diff.patch", "audit.json",
                     "audit.log"):
            full = os.path.join(pk, name)
            files = []
            if os.path.isdir(full):
                for b, _d, fs in os.walk(full):
                    files.extend(os.path.join(b, f) for f in fs)
            elif os.path.isfile(full):
                files.append(full)
            for f in files:
                if os.path.islink(f):
                    continue
                shared.add(rerun.sha256_file(f))
                with _fopen(f, "rb") as fh:
                    shared.update(_HEX.findall(fh.read().decode("utf-8", "replace")))
    disputed = brief.get("disputed") if isinstance(brief.get("disputed"), dict) else {}
    for cid, pair in disputed.items():
        if not isinstance(pair, dict):
            continue
        for s in _walk_strings(pair.get("this")):
            shared.update(_HEX.findall(s))
        for s in _walk_strings(pair.get("other")):
            shared.update(_HEX.findall(s))
        that = pair.get("other") or {}
        for item in that.get("evidence") or []:
            rid = item.get("receipt")
            if not rid:
                continue
            for d in _other_try_dirs(root, task, verification, other):
                for suffix in (".json", ".out"):
                    f = os.path.join(d, "receipts", rid + suffix)
                    if os.path.isfile(f):
                        shared.add(rerun.sha256_file(f))
    open_ev = verdicts.open_event(conn, verification)
    seal = (open_ev["data"].get("seal_event") if open_ev else None)
    if not isinstance(seal, int):
        seal = (brief.get("seal") or {}).get("event") if isinstance(brief.get("seal"), dict) else None
    traces = verify_gate.earlier_traces(conn, root, task, before=seal) if isinstance(seal, int) \
        else {"ids": [], "hashes": [], "sizes": []}
    return {"tokens": sorted(set(tokens)), "ids": list(traces["ids"]),
            "hashes": sorted((hashes - shared) | set(traces["hashes"])),
            "events": sorted(event_ids), "traces": list(traces["hashes"]),
            "trace_sizes": list(traces["sizes"])}


def _other_try_dirs(root, task, vf, other):
    base = os.path.join(_vf_dir(root, task, vf), other)
    out = []
    if os.path.isdir(base):
        for r in sorted(os.listdir(base)):
            for t in sorted(os.listdir(os.path.join(base, r))) if os.path.isdir(os.path.join(base, r)) else []:
                out.append(os.path.join(base, r, t))
    return out


def _id_pattern(vid):
    return re.escape(vid) + r"(?![0-9])"


def _hit(text, forbidden) -> str | None:
    s = str(text)
    for tok in forbidden.get("tokens") or ():
        if tok and tok in s:
            return tok
    for vid in forbidden.get("ids") or ():
        if vid and re.search(_id_pattern(vid), s):
            return vid
    for eid in forbidden.get("events") or ():
        if re.search(r"event:%d(?![0-9])" % eid, s) or ("events/%d.json" % eid) in s:
            return "event:%d" % eid
    for h in forbidden.get("hashes") or ():
        if h and h in s:
            return h
    return None


def _scrub(text, forbidden) -> str:
    s = str(text)
    for tok in sorted(forbidden.get("tokens") or (), key=len, reverse=True):
        if tok:
            s = s.replace(tok, WITHHELD)
    for vid in sorted(forbidden.get("ids") or (), key=len, reverse=True):
        if vid:
            s = re.sub(_id_pattern(vid), WITHHELD, s)
    for eid in forbidden.get("events") or ():
        s = re.sub(r"event:%d(?![0-9])" % eid, WITHHELD, s)
        s = s.replace("events/%d.json" % eid, WITHHELD)
    for h in forbidden.get("hashes") or ():
        if h:
            s = s.replace(h, WITHHELD)
    return s


def withhold(refusal: list, forbidden: dict) -> list:
    """The refusal key with every forbidden token and sha256 replaced by "<withheld>"."""
    out = []
    for item in refusal or []:
        if isinstance(item, dict):
            out.append({k: (_scrub(v, forbidden) if isinstance(v, str) else v)
                        for k, v in item.items()})
        elif isinstance(item, str):
            out.append(_scrub(item, forbidden))
        else:
            out.append(item)
    return out


def assert_blind(brief: dict, *, first_pass: bool, forbidden: dict, packet: str | None = None,
                 trees: tuple = ()) -> None:
    """Refuse a brief that is not blind (section 5.3, item 2) with VERIFY-BRIEF-NOT-BLIND: a key
    outside FIRST_KEYS on a first pass (outside FIRST_KEYS, `disputed` and `other_out` later), a
    `disputed` entry on which the slot holds no counted verdict, or a string of the brief, a file
    name or a file content under `packet` that holds a forbidden token or sha256, or a packet
    file that hashes to a forbidden sha256, or a file of `trees` (the try's tree/ and base/) that
    is a byte copy of a trace an earlier verification left (`forbidden["traces"]`, section 5.2)."""
    problems = []
    allowed = FIRST_KEYS if first_pass else LATER_KEYS
    extra = sorted(k for k in brief if k not in allowed and not k.startswith("_"))
    if extra:
        problems.append("keys outside the %s brief: %s" % ("first-pass" if first_pass else
                                                           "later-round", ", ".join(extra)))
    disputed = brief.get("disputed")
    if isinstance(disputed, dict):
        for cid, pair in disputed.items():
            this = pair.get("this") if isinstance(pair, dict) else None
            if not this or this.get("synthetic"):
                problems.append("disputed claim %s has no counted verdict of this slot" % cid)
    for s in _walk_strings({k: v for k, v in brief.items() if not k.startswith("_")}):
        tok = _hit(s, forbidden)
        if tok:
            problems.append("the brief holds a forbidden value")
            break
    if packet and os.path.isdir(packet):
        hashes = set(forbidden.get("hashes") or ())
        for base, dirs, files in os.walk(packet):
            for name in files:
                full = os.path.join(base, name)
                rel = os.path.relpath(full, packet)
                if _hit(rel, forbidden):
                    problems.append("packet file name %s holds a forbidden value" % _scrub(rel, forbidden))
                    continue
                if os.path.islink(full):
                    continue
                sha = rerun.sha256_file(full)
                if sha in hashes:
                    problems.append("packet file %s hashes to a forbidden value" % _scrub(rel, forbidden))
                    continue
                with _fopen(full, "rb") as fh:
                    text = fh.read().decode("utf-8", "replace")
                if _hit(text, forbidden):
                    problems.append("packet file %s holds a forbidden value" % _scrub(rel, forbidden))
    traces = set(forbidden.get("traces") or ())
    sizes = set(forbidden.get("trace_sizes") or ())
    for tree in trees or ():
        if not traces:
            break
        for full, size in verify_gate._files(tree):
            if size in sizes and verify_gate._sha256_file(full) in traces:
                problems.append("%s file %s is a copy of an earlier verification's trace" % (
                    os.path.basename(tree), _scrub(os.path.relpath(full, tree), forbidden)))
    if problems:
        raise verdicts.error("VERIFY-BRIEF-NOT-BLIND", "; ".join(problems),
                             [_problem("VERIFY-BRIEF-NOT-BLIND", p) for p in problems])


# --------------------------------------------------------------------------- serving requests
def _rerun_prefix(spec: JailSpec) -> list:
    """The rerun jail (section 5.6, item 4): the gate kind of jail, its own network namespace,
    no credential, only the cwd tree writable, ended with its command."""
    from alpaca.research import jail
    ok, why = jail.available()
    if not ok:
        raise RuntimeError("the rerun jail is not available: %s" % why)
    return jail.argv(spec) + ["--"]


def _jail_env(cwd, git_env=None, tag=None) -> dict:
    """procs.gate_environment for a rerun jail (an allowlist: no provider key, proxy, GIT_* or
    ALPACA_* of the operator's), HOME and TMPDIR the jail's own /tmp, then no system or global git
    config and no replace refs, the tree's git folder when its cut names one, and the launch tag
    procs.kill_tree finds a process that left the jail's process group by."""
    from alpaca.research import procs
    env = procs.gate_environment({"PWD": cwd}, home="/tmp")
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_TERMINAL_PROMPT="0",
               GIT_NO_REPLACE_OBJECTS="1")
    env.update(git_env or {})
    if tag:
        env[procs.TAG_VAR] = tag
    return env


def _run_jailed(folder, cwd_name, command, *, shell, timeout, tag=None):
    """(rc, timed_out, output bytes, jail argv) of one command in a rerun jail, run with
    procs.run, the engine's one process runner (architecture.md section 2.2): a new session, the
    output to a log beside the try folder, every descendant killed on a timeout or a stop_all.
    The jail binds the packet read-only and the cwd tree writable, plus the git binds the tree's
    cut named (_mounts): the clone's `.git` read-only with `.git/worktrees/` hidden, and the
    tree's own admin or host git folder writable (verifier.md section 5.6, item 4)."""
    from alpaca.research import procs
    packet = os.path.join(folder, "packet")
    cwd = os.path.join(folder, cwd_name)
    if cwd_name == "packet":
        spec = JailSpec(kind="gate", cwd=packet, ro=(packet,))
        git = {}
    else:
        git = _mounts(folder, cwd_name)
        spec = JailSpec(kind="gate", cwd=cwd, ro=(packet,) + tuple(git.get("ro") or ()),
                        rw=(cwd,) + tuple(git.get("rw") or ()),
                        git_hide=tuple(git.get("git_hide") or ()),
                        admins=tuple((a, True) for a in git.get("admins") or ()))
    prefix = _rerun_prefix(spec)
    argv = prefix + (["bash", "-c", command] if shell else list(command))
    logs = os.path.join(_git_home(folder), "runs")
    os.makedirs(logs, exist_ok=True)
    fd, log = tempfile.mkstemp(prefix="rerun-", suffix=".log", dir=logs)
    os.close(fd)
    try:
        res = procs.run(argv, cwd=cwd, env=_jail_env(cwd, git.get("env"), tag),
                        timeout_s=max(1.0, float(timeout)), log=log, tag=tag)
        with _fopen(log, "rb") as fh:
            out = fh.read()
    finally:
        with contextlib.suppress(OSError):
            os.remove(log)
    shown = [a.replace(folder, "<try>") for a in prefix]
    return res.rc, bool(res.timed_out or res.stopped), out, shown


def _read_request(requests, name):
    """One request file, read with O_NOFOLLOW and a realpath check (architecture section 6.1,
    step 8): a link or a path outside `requests/` is refused."""
    full = os.path.join(requests, name)
    if os.path.realpath(full) != os.path.join(os.path.realpath(requests), name):
        raise ValueError("the request resolves outside requests/")
    fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as fh:
        data = fh.read(4 << 20)
    body = json.loads(data.decode("utf-8"))
    if not isinstance(body, dict) or not isinstance(body.get("argv"), list):
        raise ValueError("a request needs argv")
    return body


def _write_json(path, obj):
    data = (json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=True) + "\n").encode()
    part = path + ".part"
    with _fopen(part, "wb") as fh:
        fh.write(data)
    os.replace(part, path)
    return rerun.sha256_bytes(data)


def _sealed_value(brief, pointer):
    for c in brief.get("claims") or []:
        for s in c.get("sealed") or []:
            if s.get("pointer") == pointer:
                return s.get("sha256")
    for e in (brief.get("seal") or {}).get("evidence") or []:
        if e.get("pointer") == pointer:
            return e.get("sha256")
    return None


def serve(conn, root, verification, slot, round, try_: int, *, trees: dict, contract=None, stop,
          ledger: dict | None = None, deadline: float | None = None, brief: dict | None = None,
          task: str | None = None) -> dict:
    """Answer the try's requests (section 4.1), one at a time in the order of `n`, until `stop`
    is set; then serve a `--submit -` request still pending and return {receipt id: sha256}.
    `ledger` collects every served request, the kept receipt hashes and the submitted body for
    the checks of section 4.3. Runs in a thread of the driver's process and writes no event
    (a `--gate` request opens its own connection)."""
    folder = trees.get("folder")
    requests = os.path.join(folder, "requests")
    receipts = os.path.join(folder, "receipts")
    os.makedirs(receipts, exist_ok=True)
    led = ledger if ledger is not None else {}
    led.setdefault("requests", {})
    led.setdefault("receipts", {})
    led.setdefault("verdict_sha256", None)
    led.setdefault("verdict_body", None)
    brief = brief or {}
    base_info = {"schema": rerun.RECEIPT_SCHEMA, "verification": verification, "slot": slot,
                 "round": round, "try": try_}
    rerun_cap = getattr(contract, "verifier_rerun_timeout_s", None)

    def answer(n, line, rc, extra):
        _write_json(os.path.join(receipts, "%d.answer" % n), {"line": line, "rc": rc})
        led["requests"][n] = dict(extra, line=line, rc=rc)

    def handle(n, body):
        argv = [str(a) for a in body.get("argv") or []]
        base = {"argv": argv}
        try:
            req = rerun.parse_request(argv, (brief.get("engine") or {}).get("stages") or [])
        except ValueError as exc:
            return answer(n, rerun.answer_line(None, None, None, refused=str(exc)), 2,
                          dict(base, kind="refused"))
        kind = req["kind"]
        if kind == "submit":
            text = body.get("body")
            if not isinstance(text, str):
                return answer(n, rerun.answer_line(None, None, None, refused="no body"), 2,
                              dict(base, kind="refused"))
            data = text.encode("utf-8")
            path = os.path.join(receipts, "verdict.json")
            part = path + ".part"
            with _fopen(part, "wb") as fh:
                fh.write(data)
            os.replace(part, path)
            sha = rerun.sha256_bytes(data)
            led["verdict_sha256"], led["verdict_body"] = sha, text
            return answer(n, rerun.answer_line("submit", None, sha), 0,
                          dict(base, kind="submit", id=None, sha256=sha, body=text))
        rid = req["id"]
        if rid in led["receipts"] or os.path.exists(os.path.join(receipts, "%s.json" % rid)):
            return answer(n, rerun.answer_line(None, None, None, refused="receipt id %s is used "
                                               "already in this try" % rid), 2,
                          dict(base, kind="refused"))
        rec = dict(base_info, id=rid)
        rc = 0
        if kind in ("rerun", "apply"):
            if kind == "apply":
                if not os.path.isfile(os.path.join(folder, "packet", "diff.patch")):
                    return answer(n, rerun.answer_line(None, None, None, refused="this packet "
                                                       "holds no diff"), 2, dict(base, kind="refused"))
                cmd = list(rerun.APPLY_ARGV) + ["../packet/diff.patch"]
                cwd_name, shell, text = "tree", False, shlex.join(cmd)
            elif req["shell"] is not None:
                cmd, cwd_name, shell, text = req["shell"], req["cwd"], True, req["shell"]
            else:
                cmd, cwd_name, shell, text = req["argv"], req["cwd"], False, shlex.join(req["argv"])
            if callable(deadline):
                left = deadline()
            else:
                left = (deadline - time.monotonic()) if deadline else DEFAULT_TIMEOUT_S
            limit = min(x for x in (req["timeout"], left, rerun_cap) if x is not None)
            started = util.now_iso()
            try:
                code, timed_out, out, shown = _run_jailed(
                    folder, cwd_name, cmd, shell=shell, timeout=limit,
                    tag="%s/rerun" % _key_of(verification, slot, round, try_))
            except (RuntimeError, OSError) as exc:
                return answer(n, rerun.answer_line(None, None, None, refused=str(exc)), 2,
                              dict(base, kind="refused"))
            dropped = _drop_escaping_links(folder)
            if dropped:
                # a re-run that made a link leaving its tree is not a re-run the verifier may rest
                # a claim on: the link is gone, the output says so and the exit code is not zero
                out += ("".join("bin/rerun: removed a link that leaves the tree: %s -> %s\n" % pair
                                for pair in dropped)).encode("utf-8", "replace")
                if code == 0:
                    code = 1
            with _fopen(os.path.join(receipts, "%s.out" % rid), "wb") as fh:
                fh.write(out)
            rec.update(kind="rerun", command=text, argv=cmd if not shell else None,
                       cwd=cwd_name, rc=code, timed_out=timed_out, out="receipts/%s.out" % rid,
                       out_sha256=rerun.sha256_bytes(out), bytes=len(out), started=started,
                       ended=util.now_iso(), jail=shown)
            if dropped:
                rec["links_removed"] = [list(pair) for pair in dropped]
            if kind == "apply":
                rec["argv"] = cmd
                overlay = []
                if code == 0:
                    overlay = _lay(os.path.join(folder, "packet", "frozen"),
                                   os.path.join(folder, "tree"))
                rec["overlay"] = overlay
            rc = code if isinstance(code, int) else 124
        elif kind == "gate":
            if not isinstance(brief.get("engine"), dict):
                return answer(n, rerun.answer_line(None, None, None, refused="--gate is refused "
                                                   "outside a run"), 2, dict(base, kind="refused"))
            stages = brief["engine"].get("stages") or []
            if req["stage"] not in stages:
                return answer(n, rerun.answer_line(None, None, None, refused="stage %s is not a "
                                                   "stage of the frozen contract" % req["stage"]), 2,
                              dict(base, kind="refused"))
            try:
                from alpaca.research import gates
                c2 = db.connect(root)
                try:
                    got = gates.verify_rerun(c2, root, run=brief["engine"].get("run"),
                                             task=task, contract=contract, request=req,
                                             trees=trees, session=_session_of(
                                                 verification, slot, round, try_))
                finally:
                    c2.close()
            except NotImplementedError:
                return answer(n, rerun.answer_line(None, None, None, refused="gate re-runs are not "
                                                   "built yet (gates.verify_rerun)"), 2,
                              dict(base, kind="refused"))
            except Exception as exc:
                return answer(n, rerun.answer_line(None, None, None, refused="the gate re-run "
                                                   "failed: %s" % type(exc).__name__), 2,
                              dict(base, kind="refused"))
            got = dict(got or {})
            rec.update(kind="gate", stage=req["stage"], side=req["side"], rc=got.get("rc"),
                       verdict=got.get("verdict"), tests=got.get("tests"), log=got.get("log"),
                       log_sha256=got.get("log_sha256"), input_sha256=got.get("input_sha256"),
                       lock_wait_s=got.get("lock_wait_s"), event=got.get("event"))
            rc = got.get("rc") if isinstance(got.get("rc"), int) else 1
        elif kind == "hash":
            path = os.path.join(folder, req["path"])
            if os.path.islink(path) or not os.path.isfile(path):
                return answer(n, rerun.answer_line(None, None, None, refused="no file %s"
                                                   % req["path"]), 2, dict(base, kind="refused"))
            sealed = _sealed_value(brief, req["pointer"])
            if not sealed:
                # a pointer no claim and no seal item names has nothing to match: a receipt for it
                # could only say match false and refuse the claim at C12 (t-296, design 25.5)
                return answer(n, rerun.answer_line(None, None, None, refused="no sealed value for "
                                                   "%s; name a pointer of a claim or of the seal"
                                                   % req["pointer"]), 2, dict(base, kind="refused"))
            sha = rerun.sha256_file(path)
            rec.update(kind="rehash", path=req["path"], pointer=req["pointer"], sha256=sha,
                       bytes=os.path.getsize(path), sealed_sha256=sealed,
                       match=bool(sealed) and sha == sealed)
        elif kind == "event":
            path = os.path.join(folder, req["path"])
            try:
                with _fopen(path, encoding="utf-8") as fh:
                    row = json.load(fh)
            except (OSError, ValueError):
                return answer(n, rerun.answer_line(None, None, None, refused="no event export %s"
                                                   % req["path"]), 2, dict(base, kind="refused"))
            content, h = rerun.event_hashes(row)
            sealed = _sealed_value(brief, "event:%s" % row.get("id"))
            rec.update(kind="event", path=req["path"], event_id=row.get("id"), tag=row.get("tag"),
                       content_hash=content, hash=h, prev_hash=row.get("prev_hash"),
                       match=bool(sealed) and h == sealed)
        sha = _write_json(os.path.join(receipts, "%s.json" % rid), rec)
        led["receipts"][rid] = sha
        return answer(n, rerun.answer_line(kind, rid, sha), rc,
                      dict(base, kind=kind, id=rid, sha256=sha))

    def pending():
        out = []
        try:
            names = os.listdir(requests)
        except OSError:
            return out
        for name in names:
            if name.endswith(".json") and name[:-5].isdigit():
                n = int(name[:-5])
                if n not in led["requests"]:
                    out.append(n)
        return sorted(out)

    while True:
        stopping = stop.is_set()
        for n in pending():
            try:
                body = _read_request(requests, "%d.json" % n)
            except (OSError, ValueError) as exc:
                if stopping:
                    continue
                answer(n, rerun.answer_line(None, None, None, refused="unreadable request (%s)"
                                            % type(exc).__name__), 2, {"argv": None, "kind": "refused"})
                continue
            if stopping and [str(a) for a in body.get("argv") or []][:2] != ["--submit", "-"]:
                continue
            handle(n, body)
        if stopping:
            break
        time.sleep(SERVE_POLL_S)
    return dict(led["receipts"])


# --------------------------------------------------------------------------- judging a try
def _serve_port(root):
    try:
        with _fopen(os.path.join(root, ".alpaca", "serve.json"), encoding="utf-8") as fh:
            port = json.load(fh).get("port")
        return (int(port),) if port else ()
    except (OSError, ValueError, TypeError):
        return ()


def _out_sha(folder):
    lines = []
    for sub in ("out", "requests", "receipts"):
        d = os.path.join(folder, sub)
        lines.append("%s:%s" % (sub, _dir_sha(d)))
    return rerun.sha256_bytes("\n".join(lines))


#: the words of the prompt for each rule the guard enforces and the judge names (in-session-runtime.md
#: section 19.8.1), keyed by the rule's name: each rule of the strict grammar (`glob`, `expansion`,
#: `backquote`, `brace`, `tilde`, `backslash`, `paren`, `bang`, `comment`, `character`, `background`,
#: `redirect`, `heredoc`, `unreadable`, `too-long`, `program`, `command-path`, `command-word`,
#: `assignment`, `reserved`, `option`, `operand`, `folder`, `loop`, `cd`, `path`, `dotdot`, `link`,
#: `write`, `client`, `sed`, `test`) and the rules `address`, `tool` and `deny-cap` of section 18.1. Each
#: value is a sentence of prompts/verify.md, word for word (alpaca/tests/test_research_prompt_rules.py
#: pins it), with the rule in one sentence and one example of the allowed form, so the guard says only
#: what the prompt says. `RULE_EXAMPLES` names the allowed call of each rule; a test runs each one
#: through the guard (t-296, design in-session-runtime.md 25.6).
_GATE_WORDS = ("A gate re-run needs no id of your own: `python3 packet/bin/rerun --gate <item id> "
               "--side tree|base` takes the derived id that the brief's `engine.gate_receipts` "
               "lists.")
_EXPANSION_WORDS = (
    "Only `$NAME` of a `for` loop around it and `$?` in `echo` or `printf` are allowed, and a "
    "pattern that holds a `$` goes in single quotes (`grep -n 'x$' FILE`). To print a line range, "
    "run `grep -n -A 45 'pattern' FILE`, or `grep -n` first and then `sed -n 'A,Bp' FILE` with the "
    "numbers written out. " + _GATE_WORDS)
RULE_WORDS = {
    "glob": (
        "No globs outside quotes: list the folder first (`ls packet/evidence`), then write each "
        "name out, or loop over the names (`for f in 01-a.log 02-b.log; do cat "
        "packet/evidence/$f; done`). To search many files, give grep the folder and a pattern in "
        "quotes (`grep -r -H --include='h-*.json' '\"match\"' receipts`)."),
    "expansion": _EXPANSION_WORDS,
    "backquote": _EXPANSION_WORDS,
    "brace": ("Write each word out (`cat a.txt b.txt`, not `cat {a,b}.txt`), and run commands one "
              "after another, not in `{ ...; }`."),
    "tilde": ("Name paths in your try folder (`cat packet/brief.json`); `~` is your home, which is "
              "outside it."),
    "backslash": "Put the word in single quotes (`grep 'a\\|b' FILE`).",
    "paren": ("Run commands one after another, not in `( ... )`. To compare two outputs, write "
              "each to `out/` first and compare the files (`tr ',' '\\n' < packet/a.json > "
              "out/a.txt`, then `diff out/a.txt out/b.txt`)."),
    "bang": "`!` is refused outside quotes; invert a match with `grep -v 'x' packet/brief.json`.",
    "comment": "Leave comments out of the call (`grep -n 'x' packet/brief.json`).",
    "character": ("Write the call with plain characters, quotes, blanks and the joins `|`, `&&`, "
                  "`||`, `;` (`grep -n 'x' packet/brief.json | head -5`)."),
    "background": "Run every command in the foreground (`wc -l packet/brief.json`).",
    "redirect": ("Read input with `<` from a file in your try folder, write output with `>` or "
                 "`>>` into `out/` or to `/dev/null`, and join streams with `2>&1` (`grep -n 'x' "
                 "packet/brief.json > out/a.txt 2>&1`)."),
    "heredoc": ("A heredoc is only for the submission: `python3 packet/bin/rerun --submit - "
                "<<'VERDICT'`."),
    "unreadable": "Close every quote, loop and heredoc (`for f in a.txt b.txt; do cat out/$f; done`).",
    "too-long": ("At most 256 commands in one call, a loop counted once for each word: split the "
                 "work over more calls (`wc -l packet/brief.json`)."),
    "program": ("Run only the listed programs. Code under test runs only through `python3 "
                "packet/bin/rerun`."),
    "command-path": ("Write a program by its bare name: `cat packet/brief.json`, not `/usr/bin/cat "
                     "packet/brief.json` or `'cat' packet/brief.json`."),
    "command-word": ("Write a program by its bare name: `cat packet/brief.json`, not `/usr/bin/cat "
                     "packet/brief.json` or `'cat' packet/brief.json`."),
    "assignment": ("`NAME=value` is refused: write the value into each command (`grep -n 'v1' "
                   "packet/brief.json`)."),
    "reserved": ("No `if`, `while`, `until`, `case`, `time`, `!` or function: run commands one "
                 "after another, and use `&&` or `||` for a condition (`test -s packet/brief.json "
                 "&& cat packet/brief.json`)."),
    "option": ("A program takes only the options of its row in the table above, each written in "
               "full (`grep -r --include='*.py' 'x' tree`, not `--inc`); the refusal lists the "
               "row."),
    "operand": ("A program takes only the operands its row in the table above names (`uniq` takes "
                "at most one file, as in `uniq -c out/a.txt`, `grep` a pattern unless `-e` is "
                "given); the refusal lists them."),
    "folder": ("Only `ls`, `stat`, `file`, `test`, `grep -r` and `rg` take a folder, and `diff` "
               "takes no `-r`; name the files. To compare two folders, list both (`ls -R tree/x > "
               "out/t.txt`, `ls -R base/x > out/b.txt`), compare the lists with `diff`, then "
               "compare the files both hold with `cmp` or `sha256sum`."),
    "loop": ("A loop is `for NAME in WORD ...; do COMMANDS; done`, NAME lowercase letters and "
             "digits (`for f in a.txt b.txt; do wc -l out/$f; done`), over plain words written in "
             "the call, with no loop or `cd` inside and nothing after `done` but `;`, `&&`, `||` or "
             "the end."),
    "cd": ("One `cd` per call, first, to your try folder as the brief gives it (`cd <try folder> "
           "&& ls tree`); name every other folder from there."),
    "path": ("Name only paths in your try folder: start the call with the `cd` to it and name "
             "`packet/...` or `tree/...` from there (`cd <try folder> && test -e "
             "packet/brief.json`)."),
    "dotdot": ("Write the path from your try folder without `..` (`cat packet/brief.json`, not "
               "`cat tree/../packet/brief.json`)."),
    "link": ("This path leads through a link out of your try folder; list the folder (`ls -l "
             "tree`) and name the file itself."),
    "write": ("Write only into `out/` or to `/dev/null` (`grep -n 'x' packet/brief.json > "
              "out/a.txt`)."),
    "client": ("`python3` runs only the request client, `python3 packet/bin/rerun ...`; code under "
               "test runs through it."),
    "sed": ("`sed` only prints: a `w`, `W`, `r`, `R` or `e` command and an `s///w` or `s///e` flag "
            "are refused. Write the script in single quotes (`sed -n '10,40p' FILE`)."),
    "test": ("`test` takes one check of two or three words: a file operator such as `-e`, `-f`, "
             "`-d` or `-s` and one path in your try folder (`test -e packet/brief.json`, the form "
             "for `-a` too), `-n` or `-z` and one word, or two words around one comparison (`test "
             "packet/brief.json -nt out/a.txt`); join two checks with `&&` or `||`, never `-a` or "
             "`-o` (`test -e packet/brief.json && test -s packet/brief.json`)."),
    "address": ("Do not name a local address or port as a network target; read the copy in your "
                "packet instead (`cat packet/brief.json`)."),
    "tool": ("Use only the tools your job lists; a call of any other tool is refused, and that "
             "pass is void. In Bash a read is `cd <try folder> && cat packet/brief.json`."),
    "deny-cap": ("After 64 refused calls in one pass, the next refused call voids the pass: send "
                 "the rewrite the refusal gives (`cd <try folder> && ls packet`), not the same call "
                 "again."),
}
#: one full allowed call for each rule of RULE_WORDS, written backquoted, word for word, in that
#: rule's words (t-296, design in-session-runtime.md 25.6). Placeholders: `<try folder>` in a leading
#: `cd` only, `<id>`, `<item id>` and `FILE`. alpaca/tests/test_research_prompt_rules.py runs each
#: one through the guard (`role_rules`, after `cd <try folder> &&`, placeholders filled, a heredoc
#: closed with one body line) and checks that it stands in its rule's words; no import-time check,
#: so a drift fails a test and never the guard hook.
RULE_EXAMPLES = {
    "glob": "ls packet/evidence",
    "expansion": "grep -n 'x$' FILE",
    "backquote": "grep -n 'x$' FILE",
    "brace": "cat a.txt b.txt",
    "tilde": "cat packet/brief.json",
    "backslash": "grep 'a\\|b' FILE",
    "paren": "diff out/a.txt out/b.txt",
    "bang": "grep -v 'x' packet/brief.json",
    "comment": "grep -n 'x' packet/brief.json",
    "character": "grep -n 'x' packet/brief.json | head -5",
    "background": "wc -l packet/brief.json",
    "redirect": "grep -n 'x' packet/brief.json > out/a.txt 2>&1",
    "heredoc": "python3 packet/bin/rerun --submit - <<'VERDICT'",
    "unreadable": "for f in a.txt b.txt; do cat out/$f; done",
    "too-long": "wc -l packet/brief.json",
    "program": "python3 packet/bin/rerun",
    "command-path": "cat packet/brief.json",
    "command-word": "cat packet/brief.json",
    "assignment": "grep -n 'v1' packet/brief.json",
    "reserved": "test -s packet/brief.json && cat packet/brief.json",
    "option": "grep -r --include='*.py' 'x' tree",
    "operand": "uniq -c out/a.txt",
    "folder": "ls -R tree/x > out/t.txt",
    "loop": "for f in a.txt b.txt; do wc -l out/$f; done",
    "cd": "cd <try folder> && ls tree",
    "path": "cd <try folder> && test -e packet/brief.json",
    "dotdot": "cat packet/brief.json",
    "link": "ls -l tree",
    "write": "grep -n 'x' packet/brief.json > out/a.txt",
    "client": "python3 packet/bin/rerun ...",
    "sed": "sed -n '10,40p' FILE",
    "test": "test -e packet/brief.json",
    "address": "cat packet/brief.json",
    "tool": "cd <try folder> && cat packet/brief.json",
    "deny-cap": "cd <try folder> && ls packet",
}
#: the names section 18 gave a refusal that the grammar names differently (the write tool, a tool
#: the job is not given): a hit with no grammar rule is read by rule_keys as in section 18, then
#: through this table
_KEYS_18 = {"write-outside-out": "write", "write-unplaced": "write", "program-off-list": "program"}


def rule_keys(kind: str, why: str, rule: str | None = None) -> list:
    """The names of a table of rule words a refusal belongs to: `rule` is the grammar's rule when
    the hit holds one (section 19.9.1: the strict grammar names its own), else `kind` is the scan
    that named the call (`path`, `address` or `allow-list`) and `why` the allow list's text, read
    as in section 18, which the auditor keeps (verifier.md section 5.3)."""
    if rule:
        return [rule]
    if kind in ("path", "address"):
        return [kind]
    why = str(why or "")
    if why.startswith("unreadable:"):
        return ["unreadable"]
    if why.startswith("uses "):
        got = [w.strip() for w in why[5:].split(",") if w.strip() in pathscan.FLAGS]
        return got or ["program-off-list"]
    if why.startswith("runs ") and why.endswith("which is not on the allow list"):
        return ["program-off-list"]
    if why.startswith("writes ") and "which the scan cannot place" in why:
        return ["write-unplaced"]
    if why == "a write outside out/" or (why.startswith("writes ") and why.endswith("outside out/")):
        return ["write-outside-out"]
    if why == "a tool the verifier is not given":
        return ["tool"]
    for first in ("sed", "sort", "uniq"):
        if why.startswith(first):
            return [first]
    if why.endswith("runs a program"):
        return ["option-runs"]
    if why.endswith("writes a file"):
        return ["option-writes"]
    if why.endswith("follows links"):
        return ["option-follows"]
    if why.endswith("from a file"):
        return ["option-names"]
    return ["program-off-list"]


def _verifier_keys(hit: dict) -> list:
    """The keys of RULE_WORDS for one hit of the verifier's rules: the grammar's rule, or the
    section 18 name of a hit that has none (a tool the job is not given, a write by the write tool)
    taken through _KEYS_18."""
    got = []
    for k in rule_keys("allow-list", hit.get("why"), hit.get("rule")):
        k = _KEYS_18.get(k, k)
        got.append(k if k in RULE_WORDS else "program")
    return got


def role_rules(root, folder, calls, *, other_homes=(), shell_cwd=None, extra_allowed=(),
               exact_allowed=(), runtime_tools=()) -> list:
    """The verifier's call rules over a list of calls, one source for the after-pass judge (the
    whole stream) and the PreToolUse guard (one call; in-session-runtime.md section 18.1): [{"line",
    "code", "detail", "rule" (`path`, `address` or `allow-list`), "keys" (RULE_WORDS names),
    "why"}]. The rules are the path rule (pathscan.outside, exact), the address rule
    (pathscan.addresses) and the allow list (pathscan.shell_allowed with SHELL_ALLOW, which is the
    strict grammar of in-session-runtime.md section 19, so a hit holds the grammar's `rule`). For a
    session launch every call starts in `shell_cwd` and the shell is not kept between calls, so a
    list of one call gives the same answer for that call as the whole stream does. A call of kind
    `denied` is skipped by every scan. `extra_allowed` are paths allowed as folders (the prompt
    file); `exact_allowed` are files allowed as exactly that file (the persisted outputs)."""
    out = []
    allowed = [folder] + [p for p in extra_allowed if p]
    watched = (os.path.realpath(root), os.path.abspath(root), os.path.expanduser("~")) + \
        tuple(h for h in other_homes if h)
    for hit in pathscan.outside(calls, cwd=folder, allowed=allowed, ignore_prefixes=(),
                                watched=watched, reset_cwd=shell_cwd, exact=True,
                                exact_allowed=tuple(exact_allowed or ())):
        out.append({"line": hit["line"], "code": "VERIFY-BREACH", "rule": "path",
                    "detail": "call %s reached %s" % (hit["line"], hit["path"]),
                    "why": "reached %s" % hit["path"], "keys": ["path"]})
    names = tuple(n for n in {socket.gethostname(), socket.getfqdn()} if n)
    for hit in pathscan.addresses(calls, host_names=names, ports=_serve_port(root)):
        out.append({"line": hit["line"], "code": "VERIFY-BREACH", "rule": "address",
                    "detail": "call %s named %s" % (hit["line"], hit["target"]),
                    "why": "named %s" % hit["target"], "keys": ["address"]})
    exact = tuple(p for p in extra_allowed if p) + tuple(exact_allowed or ())
    for hit in pathscan.shell_allowed(calls, allow=SHELL_ALLOW, try_folder=folder,
                                      client=os.path.join(folder, "packet", "bin", "rerun"),
                                      reset_cwd=shell_cwd, runtime_tools=runtime_tools,
                                      exact_allowed=exact):
        out.append({"line": hit["line"], "code": "VERIFY-BREACH", "rule": "allow-list",
                    "detail": "call %s: %s (%s)" % (hit["line"], hit["command"], hit["why"]),
                    "why": hit["why"] if hit.get("rule") else "%s (%s)" % (hit["command"],
                                                                         hit["why"]),
                    "keys": _verifier_keys(hit)})
    return out


def _breaches(root, folder, calls, other_homes, *, shell_cwd=None, extra_allowed=(),
              runtime_tools=(), hook_scripts=None, config_dir=None, exact_allowed=()):
    """The post-pass breach check of section 5.3, item 4. For a session launch (in-session-runtime.md
    section 7) every call starts in the driving session's folder (`shell_cwd`) and the shell is
    not kept between calls; the prompt file and the persisted outputs the agent's own tool results
    announced are extra allowed paths; the runtime's own tools are not off-list calls; and a call
    that reaches the control plane (the record, the hooks, the engine, a text source) is a hit.
    The call rules are `role_rules`, the same function the guard runs before a call; `calls` are
    the calls that ran (jobs.ran_calls)."""
    problems = []
    allowed = [folder] + [p for p in extra_allowed if p]
    for hit in role_rules(root, folder, calls, other_homes=other_homes, shell_cwd=shell_cwd,
                          extra_allowed=extra_allowed, exact_allowed=exact_allowed,
                          runtime_tools=runtime_tools):
        problems.append(_problem(hit["code"], hit["detail"], rule=hit["rule"], call=hit["line"]))
    if shell_cwd is not None:
        from alpaca.research import session_engine
        for hit in session_engine.control_hits(calls, here=shell_cwd, root=root,
                                               hook_scripts=hook_scripts, cfg=config_dir,
                                               allowed=tuple(allowed),
                                               exact_allowed=tuple(exact_allowed or ())):
            problems.append(_problem("VERIFY-BREACH", "call %s %s" % (hit["line"], hit["why"]),
                                     rule="control-plane", call=hit["line"]))
    return problems


def _abs(root, path):
    if not path:
        return None
    return path if os.path.isabs(path) else os.path.join(root, path)


def _served_submissions(ledger) -> list:
    """The bodies of the submissions the serve thread answered, in the order served."""
    reqs = (ledger or {}).get("requests", {})
    return [reqs[n].get("body") for n in sorted(reqs)
            if reqs[n].get("kind") == "submit"]


def _judge(conn, root, ctx, result, ledger, *, live=True):
    """Read one try's outcome from its launch end and captured stream (section 6.5). Returns
    ("verdict", normalized, facts) or ("refused", code, problems, refuted, facts) or ("held",)."""
    res = dict(result or {})
    folder = ctx["folder"]
    facts = {"launch_end": None, "stream_sha256": None, "verdict_sha256": None}
    if ctx.get("error") is not None:
        return ("refused", "VERIFY-NO-VERDICT",
                [_problem("VERIFY-NO-VERDICT", "the launch failed to start: %s" % ctx["error"])],
                [], facts)
    if res.get("held"):
        return ("held",)
    le = verify_gate.event_by_id(conn, res.get("launch_end"))
    if le is None or le["kind"] != _LAUNCH_END:
        # a submission the serve thread already answered is on the record even when the launch
        # has no end (a session job the judge refused or that expired, which is what a driver that
        # saw a refuting verdict and filed a claim built to fail produces): the try is lost, and
        # the refutations it submitted stay as dropped ones, so LOST_MAX counts it (section 6.5;
        # in-session-runtime.md section 12.1, A37)
        served = _served_submissions(ledger)
        if served:
            dropped = verdicts.refuted_in(served[-1], [c["id"] for c in ctx["scope"]])
            return ("refused", "VERIFY-LOST",
                    [_problem("VERIFY-LOST", "the launch recorded no end, and a submission had "
                              "been served")], dropped, facts)
        code = "VERIFY-NO-VERDICT" if live else "VERIFY-LOST"
        return ("refused", code, [_problem(code, "the launch recorded no end")], [], facts)
    facts["launch_end"] = le["id"]
    stream = _abs(root, res.get("stream") or le["data"].get("stream"))
    want = le["data"].get("stream_sha256")
    facts["stream_sha256"] = want
    if not stream or not os.path.isfile(stream) or rerun.sha256_file(stream) != want:
        # the stream cannot be read, but a submission the driver served still names its
        # refutations: they stay on the record as dropped ones
        kept = [r.get("body") for r in (ledger or {}).get("requests", {}).values()
                if r.get("kind") == "submit"]
        dropped = verdicts.refuted_in(kept[-1], [c["id"] for c in ctx["scope"]]) if kept else []
        return ("refused", "VERIFY-LOST",
                [_problem("VERIFY-LOST", "the captured stream no longer hashes to its launch end")],
                dropped, facts)
    session = le["data"].get("mode") == "session"
    calls = _tool_calls(stream, ctx["runtime"], keep_ids=session)
    jid = le["data"].get("job") or ""
    if session:
        # a call the guard denied never ran: the scans below read the calls that ran, each denied
        # call stands in its place as a stub (jobs.ran_calls, in-session-runtime.md section 18.1)
        from alpaca.research import jobs
        calls = jobs.ran_calls(conn, jid, le["data"].get("agent_id"), calls)
    scope_ids = [c["id"] for c in ctx["scope"]]
    items = (ctx["brief"].get("engine") or {}).get("stages") or [] \
        if isinstance(ctx.get("brief"), dict) else []
    invs = verdicts.stream_invocations(calls, items)
    sub = verdicts.last_submission(calls)
    refuted = verdicts.refuted_in(sub["heredoc"]["body"], scope_ids) if sub and sub.get("heredoc") else []
    # a served submission the stream no longer holds
    for n, req in sorted((ledger or {}).get("requests", {}).items()):
        if req.get("kind") != "submit":
            continue
        found = False
        for i in invs:
            if i["args"] != ["--submit", "-"]:
                continue
            shown = verdicts.answer_of(i, ledger)
            if req.get("line") in (i.get("answers") or []) \
                    or (shown is None and i["call"] == i["calls"]):
                found = True
                break
        if not found:
            dropped = verdicts.refuted_in(req.get("body"), scope_ids)
            return ("refused", "VERIFY-LOST",
                    [_problem("VERIFY-LOST", "served submission %s has no call in the stream" % n,
                              check="C14")], dropped, facts)
    if _dir_sha(os.path.join(folder, "packet")) != ctx["packet_sha256"]:
        return ("refused", "VERIFY-PACKET-CHANGED",
                [_problem("VERIFY-PACKET-CHANGED", "packet/ no longer hashes to the assign "
                          "event's packet_sha256")], refuted, facts)
    extra, exact, shell, tools = (), (), None, ()
    scripts, cfg = None, None
    if session:
        from alpaca.research import agent_transcript, session_runtime
        launch_ev = verify_gate.event_by_id(conn, le["data"].get("launch_event"))
        ld = (launch_ev or {}).get("data") or {}
        shell = ld.get("shell_cwd") or folder
        extra = (ld.get("prompt"),)
        tools = session_runtime.RUNTIME_TOOLS
        from alpaca.research import jobs
        jev = jobs.find_job_event(conn, jid)
        drv = jobs.newest_driver(conn, jev["data"]["scope"]) if jev else None
        dd = (drv or {}).get("data") or {}
        scripts, cfg = dd.get("hook_scripts"), dd.get("config_dir")
        # the persisted outputs of the agent's own tool results: exact files, in the form Claude
        # Code writes (section 18.1a), read from the driving session's tool-results folder
        src = jobs.source_transcript_of(conn, jev["data"]["scope"], jid) if jev else None
        exact = tuple(agent_transcript.persisted_outputs(
            agent_transcript.read_records(stream), agent_transcript.results_dir_of(src)))
    breach = _breaches(root, folder, calls, ctx.get("other_homes") or [], shell_cwd=shell,
                       extra_allowed=extra, runtime_tools=tools, hook_scripts=scripts,
                       config_dir=cfg, exact_allowed=exact)
    if breach:
        return ("refused", "VERIFY-BREACH", breach, refuted, facts)
    if sub is None:
        return ("refused", "VERIFY-NO-VERDICT",
                [_problem("VERIFY-NO-VERDICT", "the stream holds no bin/rerun --submit - call")],
                [], facts)
    raw = None
    copy = os.path.join(folder, "receipts", "verdict.json")
    if os.path.isfile(copy) and not os.path.islink(copy):
        try:
            with _fopen(copy, encoding="utf-8") as fh:
                raw = json.load(fh)
        except ValueError:
            raw = None
        facts["verdict_sha256"] = rerun.sha256_file(copy)
    brief = dict(ctx["brief"], **{"try": ctx["try"]})
    try:
        norm = verdicts.check_verdict(brief, raw if raw is not None else {}, receipts_dir=os.path.join(folder, "receipts"),
                                      stream_calls=calls, served=ledger or {},
                                      commands=ctx["scope"])
    except verdicts.VerifyError as exc:
        code = exc.code
        if not live and all(isinstance(p, dict) and p.get("check") in ("C4", "C10", "C13", "C14")
                            for p in exc.problems):
            code = "VERIFY-LOST"
        return ("refused", code, exc.problems, refuted, facts)
    return ("verdict", norm, facts)


# --------------------------------------------------------------------------- records
def _seal_facts(conn, vf):
    od = verdicts.open_event(conn, vf)["data"]
    return od


def _review_of(conn, root, task, vf, slot, rnd, try_, norm, facts, claims_txt):
    """Keep the counted verdict write-once through reviews.submit (section 4.6)."""
    from alpaca.runlog import reviews
    with _fopen(claims_txt, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    line_of = {}
    for i, line in enumerate(lines, 1):
        if "\t" in line:
            line_of.setdefault(line.split("\t", 1)[0], i)
    findings = []
    for c in norm["claims"]:
        ln = line_of.get(c["id"])
        if ln is None:
            continue
        sev = {"refuted": "error", "cannot-verify": "warning"}.get(c["effective"], "info")
        meaning = ("%s: %s" % (c["effective"], c.get("note") or ""))[:1000]
        findings.append({"line": ln, "end_line": ln, "severity": sev,
                         "quote": lines[ln - 1][:400], "meaning": meaning,
                         "check": str(c.get("failure_scenario") or "")[:600]})
    outcome = {"PASS": "matches-verdict", "FAIL": "disagrees-with-verdict"}.get(
        norm["slot_verdict"], "inconclusive")
    review = {"task": task, "log_sha256": rerun.sha256_file(claims_txt), "reviewer": slot,
              "summary": norm["summary"], "outcome": outcome, "findings": findings}
    return review


def record_verdict(conn, root, verification, slot, round, normalized: dict, *, launch: dict) -> dict:
    """Keep the counted verdict as a checked review, then append `research-proof-verdict` with the
    per-claim verdicts and each receipt as a `local:` pointer with its sha256 (section 4.6), and
    `research-verify-apply-failure` when the slot's apply receipt failed. `launch` carries the
    try, its session and worker, the launch end, the stream sha256 and the driver session.
    Returns the verdict event."""
    from alpaca.runlog import reviews
    task = _task_of(conn, verification)
    od = _seal_facts(conn, verification)
    try_ = launch["try"]
    folder = _try_dir(root, task, verification, slot, round, try_)
    rel_try = _rel(root, folder)
    claims_out = []
    for c in normalized["claims"]:
        ev_items = []
        for e in c.get("evidence") or []:
            ptr = e.get("pointer") or ""
            if ptr.startswith("local:"):
                ptr = "local:%s/%s" % (rel_try, ptr[len("local:"):])
            ev_items.append({"receipt": e.get("receipt"), "kind": e.get("kind"), "pointer": ptr,
                             "sha256": e.get("sha256"), "key": e.get("key"),
                             "result": e.get("result")})
        claims_out.append({"id": c["id"], "verdict": c["verdict"], "effective": c["effective"],
                           "reason": c.get("reason"), "evidence": ev_items,
                           "failure_scenario": c.get("failure_scenario"),
                           "conditions": c.get("conditions") or [], "note": c.get("note")})
    data = {"verification": verification, "task": task, "seal_event": od.get("seal_event"),
            "report_sha256": od.get("report_sha256"), "verifier": slot,
            "worker": launch.get("worker"), "session": launch.get("session"),
            "runtime": launch.get("runtime"), "round": round, "try": try_,
            "first_pass": launch.get("first_pass"),
            "conditional": bool(normalized.get("conditional")), "synthetic": False,
            "apply_failure": bool(normalized.get("apply_failed")),
            "audit_event": od.get("audit_event"), "review": None,
            "launch_end": launch.get("launch_end"), "stream_sha256": launch.get("stream_sha256"),
            "verdict_sha256": normalized.get("verdict_sha256"), "claims": claims_out,
            "slot_verdict": normalized["slot_verdict"]}
    claims_txt = os.path.join(_vf_dir(root, task, verification), "claims.txt")
    review = _review_of(conn, root, task, verification, slot, round, try_, normalized,
                        launch, claims_txt)
    context = {"verification": verification, "slot": slot, "round": round, "try": try_,
               "launch_end": launch.get("launch_end"), "stream_sha256": launch.get("stream_sha256"),
               "verdict_sha256": normalized.get("verdict_sha256"),
               "receipts": normalized.get("receipts") or {}, "verdict": data}
    kept = reviews.submit(root, task, review, log=claims_txt, folder=verify_gate.kept_dir(root, task),
                          kind=_REVIEW, ref_key="task", session=launch.get("driver_session"),
                          actor=ACTOR, op=_op(conn, task), context=context)
    rev_ev = verify_gate.events(conn, _REVIEW, task)[-1]
    data["review"] = {"id": kept["review"], "path": kept["path"], "sha256": rev_ev["data"].get("sha256")}
    return _emit_verdict(conn, root, task, od, data, launch.get("driver_session"),
                         normalized.get("apply_receipt"), folder)


def _emit_verdict(conn, root, task, od, data, session, apply_rec, folder):
    op = _op(conn, task)
    run = (od.get("engine") or {}).get("run")
    ev = _emit(conn, _VERDICT, task, op, session, code=verdicts.band(data["slot_verdict"]),
               data={k: v for k, v in data.items() if k != "slot_verdict"}, run=run)
    if data.get("apply_failure") and apply_rec:
        path = os.path.join(folder, "receipts", "apply.json")
        _emit(conn, _APPLY_FAILURE, task, op, session, code=vc.FAIL, run=run,
              data={"verification": data["verification"], "slot": data["verifier"],
                    "round": data["round"], "rc": apply_rec.get("rc"),
                    "receipt": _rel(root, path),
                    "receipt_sha256": apply_rec.get("_sha256") or (rerun.sha256_file(path)
                                                                   if os.path.isfile(path) else None),
                    "diff_sha256": (od.get("engine") or {}).get("diff_sha256")})
    return ev


def _op(conn, task):
    return verify_gate._op_of(conn, task)


def _synthetic(conn, root, task, vf, slot, rnd, scope, session, last_event):
    od = _seal_facts(conn, vf)
    data = {"verification": vf, "task": task, "seal_event": od.get("seal_event"),
            "report_sha256": od.get("report_sha256"), "verifier": slot, "worker": None,
            "session": None, "runtime": od.get("runtime"), "round": rnd, "try": None,
            "first_pass": first_pass(conn, vf, slot), "conditional": False, "synthetic": True,
            "apply_failure": False, "audit_event": od.get("audit_event"), "review": None,
            "launch_end": None, "stream_sha256": None, "verdict_sha256": None,
            "claims": [{"id": cid, "verdict": "cannot-verify", "effective": "cannot-verify",
                        "reason": "infrastructure",
                        "evidence": [{"receipt": None, "kind": "event",
                                      "pointer": "event:%s" % last_event if last_event else None,
                                      "sha256": None, "key": None, "result": None}],
                        "failure_scenario": None, "conditions": []} for cid in scope]}
    return _emit(conn, _VERDICT, task, _op(conn, task), session, code=vc.BLOCKED, data=data,
                 run=(od.get("engine") or {}).get("run"))


# --------------------------------------------------------------------------- recovery
def _assign_of(conn, vf, slot, rnd, try_):
    for ev in verify_gate.events(conn, _ASSIGN, verification=vf):
        d = ev["data"]
        if d.get("slot") == slot and d.get("round") == rnd and d.get("try") == try_:
            return ev
    return None


def _has_outcome(conn, vf, slot, rnd, try_):
    for kind in (_VERDICT, _REFUSED):
        for ev in verify_gate.events(conn, kind, verification=vf):
            d = ev["data"]
            if (d.get("verifier") or d.get("slot")) == slot and d.get("round") == rnd \
                    and d.get("try") == try_:
                return True
    return False


def _last_try_event(conn, vf, slot, rnd):
    """The event a synthetic verdict points at (section 6.8): the `research-launch-end` of this
    slot's last try in this round, else that try's refusal, else its assign event. None when the
    slot has no try in the round."""
    last = None
    for ev in verify_gate.events(conn, _ASSIGN, verification=vf):
        d = ev["data"]
        if d.get("slot") == slot and d.get("round") == rnd and (
                last is None or (d.get("try") or 0) >= (last["data"].get("try") or 0)):
            last = ev
    if last is None:
        return None
    try_ = last["data"].get("try")
    end = _launch_end_for(conn, _key_of(vf, slot, rnd, try_ or 0))
    if end is not None:
        return end
    for ev in reversed(verify_gate.events(conn, _REFUSED, verification=vf)):
        d = ev["data"]
        if d.get("slot") == slot and d.get("round") == rnd and d.get("try") == try_:
            return ev["id"]
    return last["id"]


def _launch_end_for(conn, key):
    row = conn.execute("SELECT id FROM events WHERE kind=? AND json_extract(data, '$.key')=? "
                       "ORDER BY id DESC LIMIT 1", (_LAUNCH_END, key)).fetchone()
    return row["id"] if row else None


def recover_try(conn, root, verification, slot, round, try_: int, *, session=None) -> dict:
    """A try with an assign event and no outcome (section 6.8): with no recorded launch end it is
    VERIFY-LOST; with one, its verdict is taken from its captured stream alone under C4, C10,
    C13 and C14, and it is VERIFY-LOST when those do not hold. A kept review with no verdict
    event gets that event from the kept file. Returns {"event", "code"}."""
    task = _task_of(conn, verification)
    od = _seal_facts(conn, verification)
    session = session or "proof-verify"
    a = _assign_of(conn, verification, slot, round, try_)
    folder = _try_dir(root, task, verification, slot, round, try_)
    if a is None or _has_outcome(conn, verification, slot, round, try_):
        return {"event": None, "code": None}
    # a kept review whose verdict event was never appended
    for rv in verify_gate.events(conn, _REVIEW, task):
        ctx = verify_gate.kept_context(root, rv["data"].get("path"))
        if ctx.get("verification") == verification and ctx.get("slot") == slot \
                and ctx.get("round") == round and ctx.get("try") == try_ \
                and verify_gate.kept_intact(root, rv["data"].get("path"), rv["data"].get("sha256")):
            data = dict(ctx.get("verdict") or {})
            data["review"] = {"id": rv["data"].get("review"), "path": rv["data"].get("path"),
                              "sha256": rv["data"].get("sha256")}
            ev = _emit_verdict(conn, root, task, od, data, session, None, folder)
            return {"event": ev["id"], "code": None}
    key = _key_of(verification, slot, round, try_)
    le = _launch_end_for(conn, key)
    ctx = _ctx_from_disk(conn, root, task, verification, slot, round, try_, a, od)
    if le is None:
        ev = _refuse(conn, task, _op(conn, task), session, verification, "VERIFY-LOST",
                     [_problem("VERIFY-LOST", "the try has no recorded launch end")], slot=slot,
                     rnd=round, try_=try_, worker=a["data"].get("worker"),
                     vsession=a["data"].get("session"),
                     extra={"out_sha256": _out_sha(folder)}, run=(od.get("engine") or {}).get("run"))
        return {"event": ev["id"], "code": "VERIFY-LOST"}
    le_ev = verify_gate.event_by_id(conn, le)
    result = {"launch_end": le, "stream": le_ev["data"].get("stream")}
    got = _judge(conn, root, ctx, result, {}, live=False)
    return _record_outcome(conn, root, task, verification, slot, round, try_, ctx, got, session)


def _ctx_from_disk(conn, root, task, vf, slot, rnd, try_, assign, od):
    folder = _try_dir(root, task, vf, slot, rnd, try_)
    brief = {}
    with contextlib.suppress(OSError, ValueError):
        with _fopen(os.path.join(folder, "packet", "brief.json"), encoding="utf-8") as fh:
            brief = json.load(fh)
    return {"folder": folder, "brief": brief, "scope": brief.get("claims") or [],
            "try": try_, "runtime": od.get("runtime"),
            "packet_sha256": assign["data"].get("packet_sha256"),
            "worker": assign["data"].get("worker"), "session": assign["data"].get("session"),
            "first_pass": assign["data"].get("first_pass"), "error": None,
            "other_homes": _other_homes(conn, vf, slot)}


def _record_outcome(conn, root, task, vf, slot, rnd, try_, ctx, got, session):
    od = _seal_facts(conn, vf)
    run = (od.get("engine") or {}).get("run")
    if got[0] == "verdict":
        norm, facts = got[1], got[2]
        ev = record_verdict(conn, root, vf, slot, rnd, norm, launch={
            "try": try_, "worker": ctx.get("worker"), "session": ctx.get("session"),
            "runtime": ctx.get("runtime"), "first_pass": ctx.get("first_pass"),
            "launch_end": facts["launch_end"], "stream_sha256": facts["stream_sha256"],
            "driver_session": session})
        return {"event": ev["id"], "code": None}
    if got[0] == "held":
        ev = _refuse(conn, task, _op(conn, task), session, vf, "VERIFY-HOLD",
                     [_problem("VERIFY-HOLD", "the launch was held")], slot=slot, rnd=rnd,
                     try_=try_, worker=ctx.get("worker"), vsession=ctx.get("session"), run=run)
        return {"event": ev["id"], "code": "VERIFY-HOLD"}
    _, code, problems, refuted, facts = got
    extra = {"verdict_sha256": facts.get("verdict_sha256"), "launch_end": facts.get("launch_end"),
             "refuted": refuted}
    if code == "VERIFY-LOST":
        extra["out_sha256"] = _out_sha(ctx["folder"])
    ev = _refuse(conn, task, _op(conn, task), session, vf, code, problems, slot=slot, rnd=rnd,
                 try_=try_, worker=ctx.get("worker"), vsession=ctx.get("session"), extra=extra,
                 run=run)
    return {"event": ev["id"], "code": code}


def _pending_tries(conn, vf):
    out = []
    for ev in verify_gate.events(conn, _ASSIGN, verification=vf):
        d = ev["data"]
        if not _has_outcome(conn, vf, d.get("slot"), d.get("round"), d.get("try")):
            out.append((d.get("slot"), d.get("round"), d.get("try")))
    return out


def _recover_earlier(conn, root, task, *, before, session, strict) -> list:
    """Bring the task's verifications on older seals up to date (section 6.10): each try with no
    outcome is recovered once its lock is free. With `strict`, a held lock over such a try stops
    the open with VERIFY-BUSY."""
    out = []
    for op_ev in verify_gate.events(conn, _OPEN, task):
        d = op_ev["data"]
        if not isinstance(d.get("seal_event"), int) or d["seal_event"] >= before:
            continue
        vf = d.get("verification")
        tries = _pending_tries(conn, vf)
        if not tries:
            continue
        try:
            with _locked(root, task, vf):
                for slot, rnd, t in tries:
                    got = recover_try(conn, root, vf, slot, rnd, t, session=session)
                    if got.get("event"):
                        out.append({"verification": vf, "slot": slot, "round": rnd, "try": t,
                                    "event": got["event"]})
        except verdicts.VerifyError as exc:
            if exc.code == "VERIFY-BUSY" and strict:
                raise verdicts.error("VERIFY-BUSY", "another process holds the lock of %s, which "
                                     "has tries with no outcome: %s" % (vf, ", ".join(
                                         "%s/r%s/t%s" % t for t in tries)))
            if exc.code != "VERIFY-BUSY":
                raise
    return out


# --------------------------------------------------------------------------- the round loop
def _state_result(conn, root, task, **over) -> dict:
    st = verify_gate.state(conn, root, task)
    out = dict(st)
    out.update(over)
    return out


def _end(conn, root, task, vf, session, driver, *, superseded=False):
    od = _seal_facts(conn, vf)
    seal_ev = verify_gate.event_by_id(conn, od.get("seal_event"))
    sdata = seal_ev["data"] if seal_ev else {}
    tree = sdata.get("tree") if isinstance(sdata.get("tree"), dict) else None
    got = verdicts.outcome(conn, vf)
    word = "superseded" if superseded else got["outcome"]
    rounds = verify_gate.events(conn, _ROUND, task, verification=vf)
    facts = verify_gate.verification_facts(conn, task, vf)
    band = {"verified": vc.PASS, "refuted": vc.FAIL}.get(word, vc.BLOCKED)
    pol = verify_gate.policy(root)
    runs = verify_gate.refuted_runs(conn, task, before=od.get("seal_event"))
    data = {"verification": vf, "task": task, "seal_event": od.get("seal_event"),
            "report_sha256": od.get("report_sha256"),
            "tree_patch_sha256": tree.get("patch_sha256") if tree else None, "outcome": word,
            "rounds": rounds[-1]["data"].get("round") if rounds else 0,
            "cap": verify_gate.HARD_CAP if verify_gate.events(conn, _EXTEND, task, verification=vf)
            else verdicts.ROUND_CAP,
            "extended": bool(verify_gate.events(conn, _EXTEND, task, verification=vf)),
            "ruled": bool(verify_gate.events(conn, _RULING, task, verification=vf)),
            "supported": got["supported"], "refuted": got["refuted"],
            "unverified": got["unverified"],
            "lost": facts["lost"], "dropped": facts["dropped"], "failed": facts["failed"],
            "earlier": verify_gate.earlier(conn, task, before=od.get("seal_event")),
            "reverify": {"count": len(runs), "max": pol["reverify_max"]},
            "verdict_events": [e["id"] for e in verify_gate.events(conn, _VERDICT, task, verification=vf)],
            "round_events": [e["id"] for e in rounds],
            "ruling_event": next((e["id"] for e in verify_gate.events(conn, _RULING, task,
                                                                      verification=vf)), None),
            "by": driver}
    ev = _emit(conn, _END, task, _op(conn, task), session, code=band, data=data,
               run=(od.get("engine") or {}).get("run"))
    _cleanup_trees(root, task, vf)
    return ev


def _cleanup_trees(root, task, vf):
    """After the end, every tree/ and base/ of the verification goes, with their git folders
    and the head clone; the rest stays as evidence. First the driver keeps, beside each try,
    which out/ files are copies of the try's own inputs (verify_gate.input_copies), which a
    later verification's trace check reads once the trees are gone."""
    base = _vf_dir(root, task, vf)
    for slot in verdicts.SLOTS:
        for d in _other_try_dirs(root, task, vf, slot):
            home = _git_home(d)
            copies = verify_gate.input_copies(d)
            os.makedirs(home, exist_ok=True)
            _write_json(os.path.join(home, verify_gate.INPUT_COPIES), copies)
            for sub in ("tree", "base"):
                for p in (os.path.join(d, sub), os.path.join(d, sub + ".git-host"),
                          os.path.join(d, sub + ".git-admin"), os.path.join(home, sub)):
                    shutil.rmtree(p, ignore_errors=True)
            shutil.rmtree(os.path.join(home, "runs"), ignore_errors=True)
    shutil.rmtree(os.path.join(base, "clone"), ignore_errors=True)


def _history(conn, task, vf):
    out = []
    ext_after = {e["data"].get("round") for e in verify_gate.events(conn, _EXTEND, task, verification=vf)}
    for ev in verify_gate.events(conn, _ROUND, task, verification=vf):
        d = ev["data"]
        rnd = d.get("round")
        unrun = False
        got = verdicts.round_verdicts(conn, vf, rnd)
        for slot, vev in got.items():
            for c in vev["data"].get("claims") or []:
                if c.get("id") in (d.get("disputed") or []) and any(
                        isinstance(k, dict) and not k.get("ran") for k in c.get("conditions") or []):
                    unrun = True
        out.append({"round": rnd, "scope": d.get("scope") or [], "disputed": d.get("disputed") or [],
                    "unrun": unrun, "extended": rnd in ext_after})
    return out


def _tries_used(conn, vf, slot, rnd):
    n = 0
    for ev in verify_gate.events(conn, _ASSIGN, verification=vf):
        d = ev["data"]
        if d.get("slot") == slot and d.get("round") == rnd:
            held = any(r["data"].get("code") == "VERIFY-HOLD" and r["data"].get("slot") == slot
                       and r["data"].get("round") == rnd and r["data"].get("try") == d.get("try")
                       for r in verify_gate.events(conn, _REFUSED, verification=vf))
            if not held:
                n += 1
    return n


def _next_try(root, task, vf, slot, rnd):
    base = os.path.join(_vf_dir(root, task, vf), slot, "r%d" % rnd)
    if not os.path.isdir(base):
        return 0
    nums = [int(n[1:]) for n in os.listdir(base) if n.startswith("t") and n[1:].isdigit()]
    return max(nums) + 1 if nums else 0


def _prepare(conn, root, task, vf, od, slot, rnd, *, runtime, timeout_s, session, formation, role):
    """Build one try: the brief, the blindness and independence checks, the assign event.
    Returns the try context, or raises Stopped."""
    try_ = _next_try(root, task, vf, slot, rnd)
    folder = _try_dir(root, task, vf, slot, rnd, try_)
    run_id = (od.get("engine") or {}).get("run")
    op = _op(conn, task)
    # the pinned runtime binary
    pin = od.get("binary")
    if pin is not None or not od.get("session_mode"):
        if not pin or not os.path.isfile(pin) or rerun.sha256_file(pin) != od.get("binary_sha256"):
            _stop(conn, task, op, session, vf, "VERIFY-RUNTIME-MISMATCH",
                  "the runtime binary pinned at open is gone or changed", run=run_id)
    try:
        brief = build_brief(conn, root, vf, slot, rnd, try_, formation=formation, role=role)
    except verdicts.VerifyError as exc:
        _stop(conn, task, op, session, vf, exc.code, exc.reason, run=run_id, rule=_rule_of(exc))
    brief["deadline_s"] = timeout_s
    fp = first_pass(conn, vf, slot)
    probe = dict(brief, _packet=os.path.join(folder, "packet"))
    forbidden = forbidden_tokens(conn, root, vf, slot, probe)
    refusal = _refusal_key(conn, vf, slot, rnd, forbidden)
    if refusal:
        brief["refusal"] = refusal
    util.write_text(os.path.join(folder, "packet", "brief.json"),
                    json.dumps(brief, indent=1, sort_keys=True) + "\n")
    try:
        assert_blind(brief, first_pass=fp, forbidden=forbidden, packet=os.path.join(folder, "packet"),
                     trees=(os.path.join(folder, "tree"), os.path.join(folder, "base")))
    except verdicts.VerifyError as exc:
        ev = _refuse(conn, task, op, session, vf, exc.code, exc.problems, slot=None, rnd=rnd,
                     try_=try_, run=run_id)
        raise Stopped(exc.code, ev)
    worker = "%s/%s" % (vf, slot)
    vsession = _session_of(vf, slot, rnd, try_)
    try:
        check_independence(conn, task, worker=worker, session=vsession, record_session=session)
    except verdicts.VerifyError as exc:
        raise Stopped(exc.code)
    from alpaca.research import paths as rpaths
    key = _key_of(vf, slot, rnd, try_)
    home = rpaths.launch_home(root, run_id, _launch_id(key))
    tools = role_tools(root, formation, role)
    write_paths = [os.path.join(folder, "out"), os.path.join(folder, "requests")]
    packet_sha = _dir_sha(os.path.join(folder, "packet"))
    brief_sha = rerun.sha256_file(os.path.join(folder, "packet", "brief.json"))
    assign = {
        "verification": vf, "slot": slot, "round": rnd, "try": try_, "folder": _rel(root, folder),
        "first_pass": fp, "worker": worker, "session": vsession, "runtime": runtime,
        "binary": od.get("binary"), "binary_sha256": od.get("binary_sha256"),
        "formation": formation, "role": role, "tools": tools, "bash_allow": list(SHELL_ALLOW),
        "read_paths": [folder], "write_paths": write_paths, "home": home,
        "brief_sha256": brief_sha, "packet_sha256": packet_sha, "timeout_s": timeout_s}
    spec = {"role": "verifier", "runtime": runtime, "session": vsession, "workdir": folder,
            "cwd": folder, "prompt": render_prompt(), "timeout_s": timeout_s, "key": key,
            "run": run_id, "attempt": task, "read_paths": [folder], "write_paths": write_paths,
            "tools": tools, "bash_allow": list(SHELL_ALLOW), "binary": od.get("binary"),
            "binary_sha256": od.get("binary_sha256"), "evidence_dir": os.path.join(folder, "launch"),
            "try_n": try_, "round": rnd, "op": op, "group": "%s/r%d" % (vf, rnd),
            "group_size": 2, "first_pass": fp, "verifier_slot": slot}
    return {"slot": slot, "try": try_, "folder": folder, "brief": brief, "scope": brief["claims"],
            "runtime": runtime, "packet_sha256": packet_sha, "worker": worker, "session": vsession,
            "first_pass": fp, "spec": spec, "assign": assign, "error": None,
            "other_homes": [], "timeout_s": timeout_s, "op": op, "run": run_id,
            "session_mode": bool(od.get("session_mode"))}


def _assign(conn, task, vf, ctx, session):
    """Record `research-verify-assign` for a prepared try; called once every try of the batch
    is prepared, so a driver stop in one slot's preparation leaves no try without an outcome."""
    ev = _emit(conn, _ASSIGN, task, ctx["op"], session, run=ctx["run"], data=ctx["assign"])
    ctx["assign_event"] = ev["id"]
    return ctx


def _other_homes(conn, vf, slot):
    """The launch homes of the other slot's tries so far, which this slot's calls may not
    reach (section 5.3, item 4)."""
    other = "v2" if slot == "v1" else "v1"
    return [e["data"].get("home") for e in verify_gate.events(conn, _ASSIGN, verification=vf)
            if e["data"].get("slot") == other and e["data"].get("home")]


def _halting(launch) -> bool:
    """True when the launch callable says its run is ending (session_runtime.launch_for)."""
    fn = getattr(launch, "stopping", None)
    try:
        return bool(fn()) if callable(fn) else False
    except Exception:  # noqa: BLE001
        return False


def _launch_one(root, vf, rnd, ctx, launch, contract):
    stop = threading.Event()
    ledger = {}
    deadline = time.monotonic() + (ctx["timeout_s"] or DEFAULT_TIMEOUT_S)
    if ctx.get("session_mode"):
        # the job's clock starts at its hand, which may be up to 1800 seconds after the issue: the
        # serve thread's re-runs are capped by the seconds the job has left (section 6.4)
        from alpaca.research import session_runtime
        deadline = session_runtime.serve_left(root, None, ctx["spec"]["key"])
    task = ctx["brief"].get("task")
    th = threading.Thread(target=serve, args=(None, root, vf, ctx["slot"], rnd, ctx["try"]),
                          kwargs={"trees": {"folder": ctx["folder"],
                                            "tree": os.path.join(ctx["folder"], "tree"),
                                            "base": os.path.join(ctx["folder"], "base")},
                                  "contract": contract, "stop": stop, "ledger": ledger,
                                  "deadline": deadline, "brief": ctx["brief"], "task": task},
                          daemon=True)
    th.start()
    result, error = None, None
    try:
        result = launch(ctx["spec"], root=root)
    except Exception as exc:          # a launch that failed to start is a failed try
        error = "%s: %s" % (type(exc).__name__, exc)
    finally:
        stop.set()
        th.join()
    if hasattr(result, "__dataclass_fields__"):
        import dataclasses
        result = dataclasses.asdict(result)
    ctx = dict(ctx, error=error)
    return ctx, result, ledger


def _run_round(conn, root, task, vf, od, rnd, *, runtime, launch, retries, timeout_s, session,
               formation, role, contract):
    """Both slots of one round, in parallel, relaunched within the retry count; a slot whose tries
    run out gets a synthetic verdict. Returns None, "paused", or raises Stopped."""
    scope = verdicts.round_scope(conn, vf, rnd)
    for slot, r, t in _pending_tries(conn, vf):
        if r == rnd:
            recover_try(conn, root, vf, slot, r, t, session=session)
    while True:
        if _halting(launch):
            return "halted"
        have = verdicts.round_verdicts(conn, vf, rnd)
        pending = [s for s in verdicts.SLOTS if s not in have]
        if not pending:
            return None
        batch = []
        for slot in pending:
            if _tries_used(conn, vf, slot, rnd) >= retries + 1:
                _synthetic(conn, root, task, vf, slot, rnd, scope, session,
                           _last_try_event(conn, vf, slot, rnd))
                continue
            # no budget pre-check: a verifier launch is never held by the budget check (t-297,
            # in-session-runtime.md section 21); the PAUSED refusal in _record_outcome stays for
            # a launcher that answers `held` all the same
            batch.append(_prepare(conn, root, task, vf, od, slot, rnd, runtime=runtime,
                                  timeout_s=timeout_s, session=session, formation=formation,
                                  role=role))
        if not batch:
            continue
        for ctx in batch:
            ctx["spec"]["group_size"] = len(batch)
            _assign(conn, task, vf, ctx, session)
        for ctx in batch:
            ctx["other_homes"] = _other_homes(conn, vf, ctx["slot"])
        held = False
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(batch))
        try:
            # both slots are launched before either verdict is read (FR-055); each result is
            # recorded as it is collected, in slot order
            futs = [pool.submit(_launch_one, root, vf, rnd, ctx, launch, contract) for ctx in batch]
            for f in futs:
                ctx, result, ledger = f.result()
                if _halting(launch):
                    # the run is ending and the stop expired this try: it is recovered as a lost
                    # try when the run resumes, not judged and not counted now
                    return "halted"
                got = _judge(conn, root, ctx, result, ledger)
                out = _record_outcome(conn, root, task, vf, ctx["slot"], rnd, ctx["try"], ctx,
                                      got, session)
                if out.get("code") == "VERIFY-HOLD":
                    held = True
        finally:
            pool.shutdown(wait=True)
        if held:
            return "paused"


def run(conn, root, task, *, runtime, driver, session, launch=None, retries=None, timeout_s=None,
        formation="builder-verifier", role="verifier", binary=None, contract=None,
        session_mode=False, on_open=None) -> dict:
    """Run or resume the verification of the task's latest seal to its end (sections 6.1 to
    6.10). Returns {"state", "outcome", "code", ...}, spelled and valued as
    `verify_gate.state` reads them right after the call; state `paused` for a launch held on a
    budget HOLD, which the record does not say. The refusals that stop before the open event
    and record nothing (VERIFY-NOT-SEALED, VERIFY-BUSY, a seal with no tree) raise VerifyError;
    VERIFY-EXHAUSTED is recorded and returned as state `exhausted`.

    `launch` is for tests and the loop runner; None means `session_runtime.launch`, which turns each
    verifier launch into a job the driving session spawns a subagent for. A caller that imports
    this package and passes a launch callable of its own can write the launch ends and verdicts
    it likes, as a direct record write can, and no gate can tell them apart (verifier.md section
    13, item 8). `session_mode` opens the verification with no runtime binary pin (a session run
    has none; two None pins agree). `on_open(vf, open data)` is called once, after `open` has
    returned, so every refusal that records nothing (VERIFY-NOT-SEALED, VERIFY-BUSY, a seal with
    no tree) still comes first (in-session-runtime.md section 2.2).
    (The keyword is session_mode and not `session`, which already names the record session.)"""
    if launch is None:
        from alpaca.research import session_runtime
        launch = session_runtime.launch
    pol = verify_gate.policy(root)
    retries = pol["retries"] if retries is None else int(retries)
    timeout_s = pol["timeout_s"] if timeout_s is None else int(timeout_s)
    seal_ev = proof.latest_seal(conn, task)
    if seal_ev is None:
        raise verdicts.error("VERIFY-NOT-SEALED", "%s has no proof-report event" % task)
    vf = verification_id(task, seal_ev["id"])
    if _open_events(conn, task, vf):
        # a resume: the open recorded its recovery; later runs recover what is left (6.10)
        _recover_earlier(conn, root, task, before=seal_ev["id"], session=session, strict=False)
    with _locked(root, task, vf):
        if verify_gate.events(conn, _END, task, verification=vf):
            return _state_result(conn, root, task)
        facts = verify_gate.verification_facts(conn, task, vf)
        if facts["stop"] is not None and facts["stop"]["data"].get("code") in verify_gate.SEAL_STOPS:
            return _state_result(conn, root, task)
        try:
            od = open(conn, root, task, runtime=runtime, driver=driver, session=session,
                      binary=binary, session_mode=session_mode)
        except verdicts.VerifyError as exc:
            if exc.code == "VERIFY-EXHAUSTED":
                return _state_result(conn, root, task, state="exhausted", code=exc.code)
            raise
        except Stopped:
            return _state_result(conn, root, task)
        if on_open is not None:
            on_open(vf, od)
        try:
            return _loop(conn, root, task, vf, od, runtime=runtime, driver=driver,
                         session=session, launch=launch, retries=retries, timeout_s=timeout_s,
                         formation=formation, role=role, contract=contract)
        except Stopped:
            return _state_result(conn, root, task)


def _loop(conn, root, task, vf, od, *, runtime, driver, session, launch, retries, timeout_s,
          formation, role, contract):
    actor = "loop-runner" if str(driver).startswith("loop-runner") else "proof-verify"
    run_id = (od.get("engine") or {}).get("run")
    op = _op(conn, task)
    while True:
        rounds = verify_gate.events(conn, _ROUND, task, verification=vf)
        rnd = rounds[-1]["data"].get("round") + 1 if rounds else 0
        extended = bool(verify_gate.events(conn, _EXTEND, task, verification=vf))
        cap = verify_gate.HARD_CAP if extended else verdicts.ROUND_CAP
        if rounds:
            last = rounds[-1]["data"]
            if not last.get("disputed"):
                _end(conn, root, task, vf, session, driver)
                return _state_result(conn, root, task)
            if last.get("round") >= cap:
                decision, reason = verdicts.decide_at_cap(_history(conn, task, vf))
                if decision == "extend" and not extended:
                    _emit(conn, _EXTEND, task, op, session, actor=actor, run=run_id, data={
                        "verification": vf, "task": task, "round": last.get("round"),
                        "rounds_from": verdicts.ROUND_CAP, "rounds_to": verdicts.HARD_CAP,
                        "reason": reason,
                        "basis": [{"round": h["round"], "disputed": len(h["disputed"]),
                                   "unrun": h["unrun"]} for h in _history(conn, task, vf)]})
                    continue
                if not verify_gate.events(conn, _RULING, task, verification=vf):
                    ruling = verdicts.rule_claims(conn, vf)
                    _emit(conn, _RULING, task, op, session, actor=actor, run=run_id, data={
                        "verification": vf, "task": task, "report_sha256": od.get("report_sha256"),
                        "round": last.get("round"), "ruling": ruling, "reason": reason,
                        "by": driver})
                _end(conn, root, task, vf, session, driver)
                return _state_result(conn, root, task)
        latest = proof.latest_seal(conn, task)
        if latest is None or latest["id"] != od.get("seal_event"):
            _end(conn, root, task, vf, session, driver, superseded=True)
            return _state_result(conn, root, task)
        got = _run_round(conn, root, task, vf, od, rnd, runtime=runtime, launch=launch,
                         retries=retries, timeout_s=timeout_s, session=session,
                         formation=formation, role=role, contract=contract)
        if got == "halted":
            return _state_result(conn, root, task, state="halted", outcome=None, code=None)
        if got == "paused":
            return _state_result(conn, root, task, state="paused", outcome=None, code=None)
        fold = verdicts.fold_round(conn, vf, rnd)
        _emit(conn, _ROUND, task, op, session, run=run_id, data={
            "verification": vf, "round": rnd, "scope": fold["scope"], "settled": fold["settled"],
            "disputed": fold["disputed"], "verdict_events": fold["verdict_events"]})


# --------------------------------------------------------------------------- status and merge
def status(conn, root, task) -> dict:
    """The state, each claim with both slots' latest effective verdicts, every lost or failed
    try, dropped refutation and driver stop, and the earlier verifications (section 10.3).
    Writes nothing."""
    st = verify_gate.state(conn, root, task)
    out = dict(st)
    vf = st.get("verification")
    rows = []
    if vf and verdicts.open_event(conn, vf):
        ids = verdicts.claim_ids(conn, vf)
        latest = {s: {} for s in verdicts.SLOTS}
        for ev in verify_gate.events(conn, _VERDICT, verification=vf):
            for c in ev["data"].get("claims") or []:
                latest[ev["data"].get("verifier")][c.get("id")] = c.get("effective")
        for cid in ids:
            rows.append({"id": cid, "v1": latest["v1"].get(cid), "v2": latest["v2"].get(cid)})
        stops = [ev["data"] for ev in verify_gate.events(conn, _REFUSED, verification=vf)
                 if ev["data"].get("code") in verify_gate.STOP_CODES]
        out["stops"] = [{"code": s.get("code"), "problems": s.get("problems")} for s in stops]
    out["claim_rows"] = rows
    seal_id = st.get("seal_event")
    if seal_id:
        out["reverify"] = {"count": len(verify_gate.refuted_runs(conn, task, before=seal_id)),
                           "max": verify_gate.policy(root)["reverify_max"]}
    return out


def require_verified(conn, root, task, *, diff_sha256: str) -> None:
    """Refuse the champion merge (section 8.6) unless the attempt's latest seal ended verified,
    `diff_sha256` equals the diff both verifiers applied, and no verification of the attempt
    ended refuted or unverified or is listed by `refuted_runs`."""
    seal_ev = proof.latest_seal(conn, task)
    if seal_ev is None:
        raise verdicts.error("VERIFY-NOT-SEALED", "%s has no seal" % task)
    ok, problems = verify_gate.check(conn, root, task, seal_ev)
    vf = verification_id(task, seal_ev["id"])
    ends = verify_gate.events(conn, _END, task, verification=vf)
    if not ends or ends[-1]["data"].get("outcome") != "verified" or not ok:
        raise verdicts.VerifyError("VERIFY-UNVERIFIED", problems[0] if problems else
                                   "%s has no verified end" % task, verdict=vc.FAIL, problems=problems)
    od = verdicts.open_event(conn, vf)["data"]
    if (od.get("engine") or {}).get("diff_sha256") != diff_sha256:
        raise verdicts.VerifyError("VERIFY-UNVERIFIED", "the diff to merge is not the diff both "
                                   "verifiers applied", verdict=vc.FAIL)
    for ev in verify_gate.events(conn, _END, task):
        if ev["data"].get("outcome") in ("refuted", "unverified"):
            raise verdicts.VerifyError("VERIFY-UNVERIFIED", "%s ended %s" % (
                ev["data"].get("verification"), ev["data"].get("outcome")), verdict=vc.FAIL)
    runs = verify_gate.refuted_runs(conn, task, before=seal_ev["id"] + 1)
    runs = [r for r in runs if r["verification"] != vf]
    if runs:
        raise verdicts.VerifyError("VERIFY-UNVERIFIED", "%s is counted (%s)" % (
            runs[0]["verification"], ", ".join(runs[0]["why"])), verdict=vc.FAIL)


# --------------------------------------------------------------------------- the CLI
def _stub(fn) -> bool:
    return str(getattr(fn, "__doc__", "") or "").startswith("Stub:")


def cli(args, conn, root) -> int:
    """`alpaca proof verify <id> [--status] [--runtime claude|codex] [--json]` (section 10.3)."""
    ident = args.id
    as_json = bool(getattr(args, "json", False))
    sid = getattr(args, "session", None) or "proof-verify"

    def emit_line(word, text, payload=None):
        if as_json:
            # section 10.3: one object with the same keys on every path, null where unknown
            print(json.dumps(payload or {"verdict": word, "verification": None, "state": None,
                                         "outcome": None, "round": None, "claims": None},
                             sort_keys=True))
        print("GATE alpaca-proof-verify: %s (%s)" % (word, text))

    if getattr(args, "status", False):
        st = status(conn, root, ident)
        payload = {"verdict": "PASS", "verification": st.get("verification"),
                   "state": st.get("state"), "outcome": st.get("outcome"),
                   "round": st.get("round"), "claims": st.get("claim_rows")}
        if as_json:
            print(json.dumps(payload, sort_keys=True))
            return vc.PASS
        print("state: %s%s" % (st["state"], " (%s)" % st["code"] if st.get("code") else ""))
        print("scope: %s" % st.get("scope"))
        if st.get("verification"):
            print("verification: %s, round %s of %s" % (st["verification"], st.get("round"),
                                                         st.get("cap")))
        for row in st.get("claim_rows") or []:
            print("  %s  v1 %s  v2 %s" % (row["id"], row["v1"] or "-", row["v2"] or "-"))
        for key in ("lost", "failed", "dropped"):
            for t in st.get(key) or []:
                print("  %s try %s (event %s)" % (key, verify_gate.try_name(t),
                                                   t.get("refused_event")))
        for s in st.get("stops") or []:
            print("  stopped with %s" % s.get("code"))
        for e in st.get("earlier") or []:
            print("  earlier %s: %s, refuted %s, counted %s (%s)" % (
                e["verification"], e.get("outcome"), ", ".join(e.get("refuted") or []) or "-",
                e.get("counted"), ", ".join(e.get("why") or []) or "-"))
        if st.get("reverify"):
            print("  reverify: %d of %d" % (st["reverify"]["count"], st["reverify"]["max"]))
        return vc.PASS
    marker = _runtime_in_use(None)
    claude, codex = _markers(None)
    wanted = getattr(args, "runtime", None)
    hook = _hooks.get("verb_launch")
    if marker is None:
        # With neither marker the verb refuses even when --runtime is passed, because a
        # verification with no session has nobody to take its jobs; with the markers of both
        # runtimes set, --runtime names which of the two this session is
        # (in-session-runtime.md section 2.2, step 2). A test that installs a stand-in launch
        # keeps today's rule.
        if (not wanted) or (not (claude and codex) and hook is None):
            emit_line("BLOCKED", "VERIFY-RUNTIME-UNKNOWN: no runtime marker%s; pass --runtime"
                      % (" (markers of both runtimes are set)" if claude and codex else ""))
            return vc.BLOCKED
        runtime = wanted
    else:
        if wanted and wanted != marker:
            emit_line("BLOCKED", "VERIFY-RUNTIME-MISMATCH: this session runs %s, not %s"
                      % (marker, wanted))
            return vc.BLOCKED
        runtime = marker
    if hook is not None:
        return _verb_run(args, conn, root, ident, runtime, sid, hook, emit_line, session_mode=False)
    from alpaca.research import errors as rerrors, session_runtime
    if _stub(session_runtime.launch):
        emit_line("BLOCKED", "the session runtime is not built in this tree")
        return vc.BLOCKED
    try:
        from alpaca.posture import level as posture_level
        lv = int(posture_level.in_force(conn, sid, root=root))
    except Exception:  # noqa: BLE001 - an unreadable level refuses the verb
        lv = None
    try:
        session_runtime.drive_checks(conn, root, sid, level=lv, runtime=runtime)
    except rerrors.ResearchRefusal as exc:
        emit_line(vc.name_of(exc.code), "%s: %s" % (exc.reason, exc.detail))
        return exc.code
    return _verb_run(args, conn, root, ident, runtime, sid, session_runtime.launch, emit_line,
                     session_mode=True, level=lv)


def _verb_run(args, conn, root, ident, runtime, sid, launch, emit_line, *, session_mode, level=None):
    """Run the verification with `launch`: the session runtime's callable (the verb process is the
    judge of the verification and the session that typed it is the driver) or, in a test, a
    stand-in with the runtime binary pinned as before."""
    on_open = None
    if session_mode:
        from alpaca.research import session_runtime

        def on_open(vf, od):
            session_runtime.register_verification(conn, root, vf, sid, level=level, runtime=runtime,
                                                  op=(proof.latest_seal(conn, ident) or {}).get("op"))
        session_runtime._VERB_STOP.clear()
        old = {}
        import signal

        def interrupt(_sig, _frame):
            session_runtime._VERB_STOP.set()
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    old[sig] = signal.signal(sig, interrupt)
                except (ValueError, OSError):
                    pass
    try:
        got = run(conn, root, ident, runtime=runtime, driver="proof-verify", session=sid,
                  launch=launch, session_mode=session_mode, on_open=on_open)
    except verdicts.VerifyError as exc:
        emit_line(vc.name_of(exc.verdict), "%s: %s" % (exc.code, exc.reason))
        if exc.code == "VERIFY-TREE" and "records no tree" in exc.reason:
            print("  1. alpaca proof seal %s --verify" % ident)
            print("  2. alpaca proof verify %s" % ident)
        return exc.verdict
    except BaseException as exc:  # noqa: BLE001 - the verb's own interrupt
        if session_mode and type(exc).__name__ == "VerbInterrupted":
            emit_line("BLOCKED", "interrupted: the open jobs expired; run the verb again to resume")
            return vc.BLOCKED
        raise
    finally:
        if session_mode:
            import signal
            from alpaca.research import jobs, session_runtime
            for sig, h in old.items():
                try:
                    signal.signal(sig, h)
                except (ValueError, OSError):
                    pass
            try:
                vf = verification_id(ident, proof.latest_seal(conn, ident)["id"])
                jobs.expire_open(conn, root, vf, "stopped", session=sid)
                session_runtime.unmark_live(root, sid, vf)
            except Exception:  # noqa: BLE001
                pass
    word = got.get("state")
    code = {"verified": vc.PASS, "refuted": vc.FAIL, "unverified": vc.BLOCKED,
            "superseded": vc.BLOCKED, "paused": vc.PAUSED, "exhausted": vc.BLOCKED}.get(word)
    if word == "stopped":
        code = verdicts.EXITS.get(got.get("code"), vc.FAIL)
    if code is None:
        code = vc.BLOCKED
    payload = {"verdict": vc.name_of(code), "verification": got.get("verification"),
               "state": word, "outcome": got.get("outcome"), "round": got.get("round"),
               "claims": got.get("claims")}
    emit_line(vc.name_of(code), "%s %s in round %s%s" % (
        got.get("verification"), got.get("outcome") or word, got.get("round"),
        " (%s)" % got["code"] if got.get("code") else ""), payload)
    return code
