"""The headless agent launcher per runtime: runtime detection and binary resolution, per-launch
homes, argv and environment, the jail and its egress proxy, timeouts and cleanup through procs,
transcripts and usage into the record.

Design: design/research-engine/architecture.md sections 3.6 and 6 (FR-019, FR-020, FR-029,
FR-042), with the mount table of section 5.2 and the event shapes of section 8.3. Every launch
runs in a bubblewrap agent jail with its own network namespace; the runtime reaches its provider
only through the launch's egress proxy (egress.py) and the forwarder inside the jail
(jail_forward.py). The runtime gets a launch home of its own (HOME, config home, message folder,
read-only bin/), an environment built from an allowlist, the prompt on stdin, and a timeout;
procs.run kills every descendant at a timeout or a stop, and the launcher sweeps the launch's tag
once more when the runtime has ended. The runtime's stdin and stdout are sockets of a stdio relay
that runs on the host, outside the jail's PID namespace (_RELAY): no process in the jail holds a
file of out/ open, and a /proc/<pid>/fd link to a socket cannot be reopened, so no agent can
seek over, truncate or rewrite its stream or its prompt. The stream, the prompt and the egress
log are kept as evidence; the transcript and its subagent files are staged by bytes into a folder of the home no
jail mounts and copied from there into the record with transcripts.snapshot before the home is
removed. The credential values the launch handed the runtime are replaced in the stream, the staged
transcript, the final message and the files the agent changed under its write paths. That redaction
is best effort: it catches only exact copies of a value. A value the agent encoded (base64, hex,
split or compressed), a changed file with over _READ_MAX bytes of data, and git objects (zlib
compressed, outside the write paths) keep it. In the write paths a value is overwritten in place
with a mask of its own length and the walk reads at most _REDACT_TREE_MAX bytes, so the host never
writes a sparse file out; research-launch-end names the changed files it left unchecked. The design
accepts that the runtime's own login is reachable in the jail (architecture.md section 6.5,
FR-019). Only that login is: when the jail gets a credentials file, it is a copy cut down to the
runtime's login (_runtime_login), never the operator's file with its MCP server tokens.

Every read of a path an agent could write (the home, the config home, msg/) is symlink-safe: the
final component is opened with O_NOFOLLOW and its realpath must stay inside the folder it was
expected in; anything else makes the launch lost (research-infra class lost), never a read
through a link. A link anywhere in the transcript's subagent tree, a file named for the Codex
thread in the sessions folder that is not a regular file (a FIFO would block the read forever,
after the timeout), a stream over _STREAM_DISK_MAX bytes, an egress log that reached
egress._LOG_MAX bytes, or a normal end with no transcript, is lost too.
"""
from __future__ import annotations

import dataclasses
import errno
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import sys
import time
import uuid
from collections.abc import Mapping

from alpaca import db, util
from alpaca import paths as alpaca_paths
from alpaca.gates import verdict as vc
from alpaca.research import agent_transcript as at
from alpaca.research import contract as contract_mod
from alpaca.research import egress, events, jail, paths, procs, spend
from alpaca.research import types as rtypes
from alpaca.research.errors import LaunchError
from alpaca.research.types import JailSpec, LaunchHome, LaunchResult, LaunchSpec


RUNTIMES = ("claude", "codex", "control")

_CLAUDE_MARKERS = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID")
_CODEX_MARKERS = ("CODEX_THREAD_ID", "CODEX_SANDBOX")
_CONFIG_NAMES = {"claude": "claude", "codex": "codex", "control": "control"}
# the one list (in-session-runtime.md section 26); FeSens's list
# (design/research-engine/study-fesens.md:660-668) plus Task, Agent and Skill
_DISALLOWED_TOOLS = at.FORBIDDEN_TOOLS
_CLAUDE_FLAGS_ENV = (("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1"), ("DISABLE_TELEMETRY", "1"),
                     ("DISABLE_ERROR_REPORTING", "1"), ("DISABLE_AUTOUPDATER", "1"))
_CREDENTIAL_VARS = {"claude": ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"),
                    "codex": ("OPENAI_API_KEY", "CODEX_API_KEY")}
_BASE_URL_VARS = {"claude": "ANTHROPIC_BASE_URL", "codex": "OPENAI_BASE_URL"}
_CREDENTIAL_FILES = {"claude": ("CLAUDE_CONFIG_DIR", ".claude", ".credentials.json"),
                     "codex": ("CODEX_HOME", ".codex", "auth.json")}
# the top-level keys of the operator's credentials file that hold the runtime's own login, and of
# them the ones at least one of which must hold a value (the Claude file also holds mcpOAuth, the
# operator's MCP server tokens, and a trustedDeviceToken: neither enters a jail)
_CREDENTIAL_KEEP = {"claude": ("claudeAiOauth",),
                    "codex": ("auth_mode", "OPENAI_API_KEY", "tokens", "last_refresh")}
_CREDENTIAL_NEED = {"claude": ("claudeAiOauth",), "codex": ("OPENAI_API_KEY", "tokens")}
_PROXY_NAMES = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy",
                "all_proxy")
_NO_PROXY_NAMES = ("NO_PROXY", "no_proxy")
# section 6.3; the names are this Codex version's (probe-jail.txt:8-68)
_CODEX_FEATURES_OFF = ("memories", "apps", "browser_use", "browser_use_external",
                       "browser_use_full_cdp_access", "computer_use", "hooks", "image_generation",
                       "plugins", "remote_plugin", "multi_agent", "skill_search",
                       "skill_mcp_dependency_install", "tool_suggest", "shell_snapshot",
                       "worktrees")
_CODEX_TOP = (("approval_policy", "never"), ("sandbox_mode", "danger-full-access"),
              ("web_search", "disabled"))
_FEATURES_TIMEOUT_S = 60
_TAIL_BYTES = 65536
_READ_MAX = 8 * 1024 * 1024          # the most bytes read from an agent-written message file
_STREAM_MAX = 64 * 1024 * 1024       # the most bytes of a stream the collect step reads
_REDACT_BLOCK = 1024 * 1024          # the block the redacting copy reads at a time
_STREAM_HEAD = 8 * 1024 * 1024       # of which from the start of the stream
_FINAL_MAX = 65536                   # the most characters of a final message kept in an event
_SCAN_MAX = 20000                    # the most entries walked under one agent folder
_TRANSCRIPT_MAX = 256 * 1024 * 1024  # the most bytes of transcripts one launch's stage copies
_REDACT_TREE_MAX = 256 * 1024 * 1024  # the most bytes the redaction of the write paths reads
_MISSED_KEEP = 100                   # the most unchecked paths named in research-launch-end
_STREAM_DISK_MAX = 1024 * 1024 * 1024  # the most bytes of a stream the relay writes on the host
_REDACTED = b"[REDACTED-CREDENTIAL]"
_SECRET_KEY = re.compile(r"token|key|secret|password", re.IGNORECASE)
_SESSION_NS = uuid.UUID("0b4c7a52-6d0e-4f0e-9c7e-2f5d8c1a9b31")   # the Claude --session-id space
_CHAMPION = re.compile(r"^(?:[a-z0-9-]+-)?r[0-9]+-champion$")
_SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_QUOTA = re.compile(r"rate[ _-]?limit|usage[ _-]?limit|quota|too many requests|\b429\b|"
                    r"limit reached|resource_exhausted", re.IGNORECASE)
_AUTH = re.compile(r"authentication[ _-]?error|invalid[ _-]?(?:x-)?api[ _-]?key|\b401\b|"
                   r"unauthori[sz]ed|not logged in|/login|oauth token (?:has )?expired|"
                   r"token (?:has )?expired|login required|invalid bearer|"
                   r"permission[ _-]?denied.*credential", re.IGNORECASE)
_QUOTA_BASE_S, _QUOTA_MAX_S = 300, 3600


class _Broken(Exception):
    """A host read of an agent path that is a link, leaves its folder, or is larger than the
    host copies (_TRANSCRIPT_MAX, _SCAN_MAX): the launch is lost."""


# ================================================================ runtime and binary

def runtime_in_use(environ: dict | None = None) -> str | None:
    """t-089's rule (verifier.md section 5.5): claude when CLAUDECODE or CLAUDE_CODE_SESSION_ID is
    set, codex when CODEX_THREAD_ID or CODEX_SANDBOX is set, and None when markers of both
    runtimes are set or none is, since a process started from one runtime's shell inherits its
    markers and no precedence tells which runtime is in use. These are the markers the release
    approval reads (alpaca/release/cli.py:14)."""
    env = os.environ if environ is None else environ
    claude = any(env.get(name) for name in _CLAUDE_MARKERS)
    codex = any(env.get(name) for name in _CODEX_MARKERS)
    if claude and not codex:
        return "claude"
    if codex and not claude:
        return "codex"
    return None


def detect_runtime(environ: dict | None = None, *, root: str | None = None) -> str:
    """runtime_in_use(environ), else the first of project.yaml `operators` (project.yaml:58-60)
    whose binary resolves.

    root defaults to alpaca.paths.root(). Only the agent runtimes claude and codex are taken
    from `operators`. Raises LaunchError when neither rule gives a runtime."""
    found = runtime_in_use(environ)
    if found:
        return found
    from alpaca import project
    try:
        base = root if root is not None else alpaca_paths.root()
        operators = (project.load(base) or {}).get("operators") or []
    except Exception:            # an unreadable project.yaml names no operator
        operators = []
    for name in operators:
        if name not in ("claude", "codex"):
            continue
        try:
            resolve_binary(name, environ)
        except LaunchError:
            continue
        return name
    raise LaunchError("no runtime marker is set and no operator of project.yaml resolves "
                      "(operators: %s)" % (", ".join(str(o) for o in operators) or "none"))


def _clean_path(env) -> str:
    """The PATH of env with every relative or empty entry left out, in order, once each."""
    out = []
    for entry in str(env.get("PATH") or jail.GATE_PATH).split(os.pathsep):
        if entry and os.path.isabs(entry) and entry not in out:
            out.append(entry)
    return os.pathsep.join(out)


def resolve_binary(runtime: str, environ: dict | None = None) -> str:
    """shutil.which on a clean PATH, then realpath; never through a shell, so the operator's shell
    functions (.alpaca/proofs/op-009/t-087.logs/runtime-homes.txt:67-74) are not used. t-089's
    driver calls it once at a verification's open and pins the result (section 6.1, step 3).

    The clean PATH is environ's PATH with its relative and empty entries left out, so a file in
    the current folder never answers. The control runtime resolves python3, which runs the
    control agent script. Raises LaunchError for an unknown runtime or no executable file."""
    if runtime not in RUNTIMES:
        raise LaunchError("unknown runtime %r" % (runtime,))
    env = os.environ if environ is None else environ
    name = "python3" if runtime == "control" else runtime
    found = shutil.which(name, path=_clean_path(env))
    if not found:
        raise LaunchError("runtime %s: %s is not on the PATH" % (runtime, name))
    real = os.path.realpath(found)
    try:
        st = os.stat(real)
    except OSError as exc:
        raise LaunchError("runtime %s: %s: %s" % (runtime, real, exc))
    if not stat.S_ISREG(st.st_mode) or not os.access(real, os.X_OK):
        raise LaunchError("runtime %s: %s is not an executable file" % (runtime, real))
    return real


def _operator_home(env) -> str:
    home = env.get("HOME")
    if not home:
        try:
            import pwd
            home = pwd.getpwuid(os.getuid()).pw_dir
        except (ImportError, KeyError):
            home = os.path.expanduser("~")
    return home


def _config_homes(env) -> list:
    """The operator's home and every runtime config home, as realpaths."""
    home = _operator_home(env)
    out = [home, os.path.join(home, ".claude"), os.path.join(home, ".codex")]
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        if env.get(name):
            out.append(env[name])
    return [os.path.realpath(p) for p in out]


def _overlaps(a: str, b: str) -> bool:
    """a equals b, contains it or lies inside it."""
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep) or b.startswith(a.rstrip(os.sep) + os.sep)


def install_dir(runtime: str, binary: str, environ: dict | None = None) -> str:
    """What the jail mounts read-only for the runtime: for Claude the resolved binary file itself
    (a single file under versions/, .alpaca/proofs/op-009/t-087.logs/runtime-homes.txt:73); for
    Codex its versioned release folder, the parent of the binary's bin/
    (.alpaca/proofs/op-009/t-087.logs/runtime-homes.txt:74). Raises LaunchError when the result is
    the operator's home, a runtime config home ($CLAUDE_CONFIG_DIR, ~/.claude, $CODEX_HOME,
    ~/.codex) or an ancestor of one; the Codex release folder lies inside ~/.codex, which is
    allowed, since only that one version's folder is mounted, never its parent.

    The control runtime mounts its interpreter file, as Claude does. A Codex binary that is not
    in a bin/ folder mounts its own folder."""
    if runtime not in RUNTIMES:
        raise LaunchError("unknown runtime %r" % (runtime,))
    env = os.environ if environ is None else environ
    real = os.path.realpath(binary)
    if runtime == "codex":
        folder = os.path.dirname(real)
        result = os.path.dirname(folder) if os.path.basename(folder) == "bin" else folder
    else:
        result = real
    for home in _config_homes(env):
        if result == home or home.startswith(result.rstrip(os.sep) + os.sep):
            raise LaunchError("install folder %s is %s or holds it" % (result, home))
    return result


def _runtime_version(runtime: str, binary: str) -> str | None:
    """The version the install path names: Claude keeps each version as a file named by it, Codex
    a release folder named <version>-<target>."""
    if runtime == "claude":
        name = os.path.basename(binary)
    elif runtime == "codex":
        name = os.path.basename(install_dir("codex", binary, {"HOME": "/nonexistent-home"}))
    else:
        return None
    found = re.match(r"^([0-9]+\.[0-9]+\.[0-9]+[0-9A-Za-z.+]*)", name)
    return found.group(1).rstrip(".-") if found else None


# ================================================================ the launch spec and home

def _as_spec(spec) -> LaunchSpec:
    """A LaunchSpec, or one built from a mapping of its fields (lists become tuples)."""
    if isinstance(spec, LaunchSpec):
        return spec
    if not isinstance(spec, Mapping):
        raise LaunchError("a launch spec is a LaunchSpec or a mapping, not %s"
                          % type(spec).__name__)
    known = {f.name for f in dataclasses.fields(LaunchSpec)}
    unknown = sorted(set(spec) - known)
    if unknown:
        raise LaunchError("unknown launch spec keys: %s" % ", ".join(unknown))
    data = dict(spec)
    for name in ("read_paths", "write_paths", "tools", "bash_allow"):
        if name in data and data[name] is not None:
            data[name] = tuple(data[name])
    try:
        return LaunchSpec(**data)
    except TypeError as exc:
        raise LaunchError("launch spec: %s" % exc)


def _credential_file(runtime: str, env) -> str | None:
    """The realpath of the operator's credentials file, when no credential variable is set
    (section 6.5). prepare_home reads it once; no jail binds it (_runtime_login)."""
    if any(env.get(name) for name in _CREDENTIAL_VARS.get(runtime, ())):
        return None
    where = _CREDENTIAL_FILES.get(runtime)
    if where is None:
        return None
    var, default, name = where
    folder = env.get(var) or os.path.join(_operator_home(env), default)
    path = os.path.join(folder, name)
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return os.path.realpath(path)


def _runtime_login(runtime: str, path: str) -> bytes:
    """The runtime's own login out of the operator's credentials file at path: a JSON object of
    only its _CREDENTIAL_KEEP keys. LaunchError (the launch is start-failed) when the file is not
    a regular file, is over _READ_MAX bytes, does not parse as a JSON object, or holds none of
    the _CREDENTIAL_NEED keys. No message names a value of the file.

    Check round 2 of t-098 (adversarial B1): the operator's Claude file also holds mcpOAuth, the
    access tokens of every MCP server the operator has authorized, and a trustedDeviceToken. The
    runtime needs neither to call its provider, so a jail that bound the whole file handed an
    agent credentials beyond what the runtime needs (FR-019). This departs from architecture.md
    section 6.5, which binds the operator's file itself and calls it "the runtime's own login and
    nothing more"."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise LaunchError("cannot read the %s credentials file %s: %s"
                          % (runtime, path, exc.strerror))
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise LaunchError("the %s credentials file %s is not a regular file" % (runtime, path))
        raw = fh.read(_READ_MAX + 1)
    if len(raw) > _READ_MAX:
        raise LaunchError("the %s credentials file %s is over %d bytes"
                          % (runtime, path, _READ_MAX))
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise LaunchError("the %s credentials file %s is not a JSON object" % (runtime, path))
    if not any(data.get(name) for name in _CREDENTIAL_NEED.get(runtime, ())):
        raise LaunchError("the %s credentials file %s holds no %s login (%s)"
                          % (runtime, path, runtime, " or ".join(_CREDENTIAL_NEED[runtime])))
    keep = {name: data[name] for name in _CREDENTIAL_KEEP[runtime] if name in data}
    return (json.dumps(keep, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _claude_settings(spec: LaunchSpec) -> dict:
    """launch-settings.json: the role's tools as allow rules, except that a bash_allow list turns
    the bare Bash rule into one Bash(<cmd>:*) rule per entry (section 6.2)."""
    tools = [str(t) for t in spec.tools]
    if spec.bash_allow:
        allow = [t for t in tools if t != "Bash"] + ["Bash(%s:*)" % c for c in spec.bash_allow]
    else:
        allow = tools
    return {"permissions": {"allow": allow, "deny": list(at.FORBIDDEN_TOOLS),
                            "defaultMode": "dontAsk"},
            "disableAllHooks": True, "sandbox": {"enabled": False}}


def _codex_config() -> str:
    lines = ['%s = "%s"' % (k, v) for k, v in _CODEX_TOP]
    lines.append("allow_login_shell = false")
    lines.append("")
    lines.append("[features]")
    lines += ["%s = false" % name for name in _CODEX_FEATURES_OFF]
    return "\n".join(lines) + "\n"


def _write(path: str, data: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(path, mode)


def _copy_file_in(src: str, dst: str, mode: int) -> None:
    """Copy a regular file, never through a link, into the launch home."""
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise LaunchError("not a regular file: %s" % src)
        data = fh.read()
    _write(dst, data, mode)


def _rmtree(path: str) -> None:
    """Remove a folder without following links (shutil.rmtree removes a link, never its target)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _msg_dir(home: LaunchHome) -> str:
    return os.path.join(home.root, "msg")


def _bin_dir(home: LaunchHome) -> str:
    return os.path.join(home.root, "bin")


def prepare_home(root: str, run: str, spec: LaunchSpec, *,
                 environ: dict | None = None) -> LaunchHome:
    """Make paths.launch_home(root, run, launch) fresh (section 6.1, step 4): home/ (HOME), the
    runtime's config home (claude/, codex/, or control/), msg/ (the runtime's own output file),
    bin/ (jail_forward.py; proposal.schema.json when the spec has an output schema; for runtime
    control, control_agent.py and the frozen mutate script; for Claude, launch-settings.json,
    kept out of the agent-writable config home so no agent can replace it) and out/ (the
    stream, prompt and egress log, never mounted). Each folder has mode 0700.

    No process in the jail holds a file of out/ open either: the stdio relay (_RELAY) holds the
    stream and the prompt on the host and gives the jail sockets, which a /proc/<pid>/fd link
    cannot reopen. So bytes already in the stream cannot be changed or taken back, and the
    prompt cannot be rewritten. What does not hold: a process in the jail that holds the
    runtime's stdout (the runtime itself, the forwarder, a child that inherits the descriptor)
    can still add lines to the stream, so a stream line proves its place in the stream, not
    which process wrote it.

    Nothing of the operator's config is copied: the Claude config home holds at most the empty
    file the credential copy is bound over, the Codex home only config.toml (and that file). When no
    credential variable is set, the runtime's own login is taken out of the operator's credentials
    file (_runtime_login: for Claude the claudeAiOauth object, for Codex auth_mode, OPENAI_API_KEY,
    tokens and last_refresh; never the MCP tokens or anything else the file holds) and written, mode
    0400, to cred/<name>, a folder no jail mounts (LaunchHome.credential_file). The jail binds that
    copy read-only over an empty file of the same name in the config home, so a token refresh in the
    jail can neither write the copy nor rotate the operator's file. A credentials file that
    does not parse, or holds no login, raises LaunchError."""
    spec = _as_spec(spec)
    env = os.environ if environ is None else environ
    if spec.runtime not in RUNTIMES:
        raise LaunchError("unknown runtime %r" % (spec.runtime,))
    launch = paths.launch_id(spec.key)
    source = _credential_file(spec.runtime, env)
    login = _runtime_login(spec.runtime, source) if source else None   # before any folder
    base = paths.launch_home(root, run, launch)
    _rmtree(base)
    os.makedirs(os.path.dirname(base), exist_ok=True)
    os.mkdir(base, 0o700)
    os.chmod(base, 0o700)
    config_name = _CONFIG_NAMES[spec.runtime]
    for sub in ("home", config_name, "msg", "bin", "out"):
        os.mkdir(os.path.join(base, sub), 0o700)
        os.chmod(os.path.join(base, sub), 0o700)
    here = os.path.dirname(os.path.abspath(__file__))
    bin_dir = os.path.join(base, "bin")
    _copy_file_in(os.path.join(here, "jail_forward.py"),
                  os.path.join(bin_dir, "jail_forward.py"), 0o444)
    if spec.output_schema is not None:
        _write(os.path.join(bin_dir, "proposal.schema.json"),
               json.dumps(spec.output_schema, sort_keys=True, indent=2).encode("utf-8"), 0o444)
    if spec.runtime == "control":
        from alpaca.research import controls
        _copy_file_in(controls.script_path(), os.path.join(bin_dir, "control_agent.py"), 0o444)
        if spec.control_mutate:
            if not os.path.isabs(spec.control_mutate):
                raise LaunchError("control_mutate must be absolute: %r" % spec.control_mutate)
            name = os.path.basename(spec.control_mutate)
            if not _SAFE_FILE.fullmatch(name):
                raise LaunchError("control_mutate has an unsafe file name: %r" % name)
            _copy_file_in(spec.control_mutate, os.path.join(bin_dir, name), 0o555)
    config = os.path.join(base, config_name)
    if spec.runtime == "claude":
        # in bin/, which the jail binds read-only: the config home is the agent's to write, so a
        # settings file there could be replaced during the session (check round 4, m3)
        _write(os.path.join(bin_dir, "launch-settings.json"),
               json.dumps(_claude_settings(spec), sort_keys=True, indent=2).encode("utf-8"),
               0o444)
    elif spec.runtime == "codex":
        _write(os.path.join(config, "config.toml"), _codex_config().encode("utf-8"), 0o644)
    credential = None
    if login is not None:
        name = _CREDENTIAL_FILES[spec.runtime][2]
        cred_dir = os.path.join(base, "cred")
        os.mkdir(cred_dir, 0o700)
        os.chmod(cred_dir, 0o700)
        credential = os.path.join(cred_dir, name)
        _write(credential, login, 0o400)
        _write(os.path.join(config, name), b"", 0o600)
    return LaunchHome(launch_id=launch, root=base, home=os.path.join(base, "home"),
                      config=config, out=os.path.join(base, "out"),
                      credential_file=credential)


# ================================================================ argv and environment

def _claude_session_uuid(spec: LaunchSpec) -> str:
    """The --session-id of a Claude launch: a UUID derived from the session and the key, so the
    argv stays a pure function of the spec and the collect step finds <uuid>.jsonl again."""
    return str(uuid.uuid5(_SESSION_NS, "%s\n%s" % (spec.session, spec.key)))


def build_argv(spec: LaunchSpec, home: LaunchHome, binary: str) -> list[str]:
    """The runtime's argv of sections 6.2 to 6.4, without the prompt: the prompt goes on stdin.
    Pure. Every path in it is absolute."""
    spec = _as_spec(spec)
    for value in (spec.cwd, binary, home.root):
        if not os.path.isabs(str(value)):
            raise LaunchError("launch paths must be absolute: %r" % (value,))
    if spec.runtime == "claude":
        argv = [binary, "-p", "--output-format", "stream-json", "--verbose",
                "--session-id", _claude_session_uuid(spec),
                "--setting-sources", "",
                "--settings", os.path.join(_bin_dir(home), "launch-settings.json"),
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--tools", ",".join(str(t) for t in spec.tools),
                "--disallowedTools", ",".join(_DISALLOWED_TOOLS),
                "--permission-mode", "dontAsk",
                "--disable-slash-commands",
                "--no-chrome"]
        if spec.model:
            argv += ["--model", str(spec.model)]
        if spec.effort:
            argv += ["--effort", str(spec.effort)]
        if spec.max_usd is not None:
            argv += ["--max-budget-usd", "%.4f" % max(0.0, float(spec.max_usd))]
        if spec.output_schema is not None and spec.role == "proposer":
            argv += ["--json-schema", json.dumps(spec.output_schema, sort_keys=True,
                                                 separators=(",", ":"))]
        return argv
    if spec.runtime == "codex":
        argv = [binary, "exec", "--json",
                "--output-last-message", os.path.join(_msg_dir(home), "last-message.txt"),
                "--cd", spec.cwd,
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "--strict-config",
                "--ignore-rules"]
        if spec.model:
            argv += ["--model", str(spec.model)]
        if spec.effort:
            argv += ["--config", 'model_reasoning_effort="%s"' % spec.effort]
        if spec.output_schema is not None and spec.role == "proposer":
            argv += ["--output-schema", os.path.join(_bin_dir(home), "proposal.schema.json")]
        argv.append("-")
        return argv
    if spec.runtime == "control":
        if spec.control not in ("noop", "random"):
            raise LaunchError("a control launch needs control noop or random, not %r"
                              % (spec.control,))
        from alpaca.research import controls
        mutate = (os.path.join(_bin_dir(home), os.path.basename(spec.control_mutate))
                  if spec.control_mutate else None)
        return controls.agent_argv(binary, os.path.join(_bin_dir(home), "control_agent.py"),
                                   spec.control, role=spec.role,
                                   seed=int(spec.control_seed or 0), mutate=mutate)
    raise LaunchError("unknown runtime %r" % (spec.runtime,))


def build_env(spec: LaunchSpec, home: LaunchHome, environ: dict | None = None) -> dict:
    """The agent environment of section 6.5, built from an allowlist, never from the operator's
    (FR-019, SC-023). Pure: it reads environ (default os.environ) and nothing else changes.

    PATH is jail.GATE_PATH plus, for Codex, its install bin folder; HOME the launch home; TMPDIR
    the jail's /tmp; PWD the launch cwd, an absolute path (gap map F03); LANG, LC_ALL and TZ the
    operator's when set (LANG C.UTF-8 otherwise); TERM, NO_COLOR, SHELL fixed; USER and LOGNAME
    the operator's; every proxy variable the in-jail forwarder and NO_PROXY empty; SSL_CERT_FILE
    and SSL_CERT_DIR as the realpaths the jail binds; the runtime's config variables; one
    credential variable of the runtime and its base URL, when set; the git variables; and
    ALPACA_RESEARCH_LAUNCH, the tag procs uses to find stray processes (_tag: the launch id and
    a hash of the home, so it is unique on the machine). Nothing else passes: no
    ALPACA_* session variable, no CLAUDECODE or CLAUDE_CODE_SESSION_ID, no forge token, no SSH
    agent, no other runtime's key."""
    spec = _as_spec(spec)
    env_in = os.environ if environ is None else environ
    if not os.path.isabs(str(spec.cwd)):
        raise LaunchError("the launch cwd must be absolute: %r" % (spec.cwd,))
    folders = jail.GATE_PATH.split(":")
    if spec.runtime == "codex" and spec.binary:
        extra = os.path.dirname(os.path.realpath(spec.binary))
        if extra not in folders:
            folders.append(extra)
    env = {"PATH": ":".join(folders), "HOME": home.home, "TMPDIR": "/tmp", "PWD": spec.cwd,
           "TERM": "dumb", "NO_COLOR": "1", "SHELL": "/bin/sh"}
    for name in ("LANG", "LC_ALL", "TZ"):
        if env_in.get(name):
            env[name] = env_in[name]
    env.setdefault("LANG", "C.UTF-8")
    user = env_in.get("USER") or env_in.get("LOGNAME")
    if not user:
        try:
            import pwd
            user = pwd.getpwuid(os.getuid()).pw_name
        except (ImportError, KeyError):
            user = None
    if user:
        env["USER"] = env_in.get("USER") or user
        env["LOGNAME"] = env_in.get("LOGNAME") or user
    proxy = "http://127.0.0.1:%d" % jail.EGRESS_PORT
    for name in _PROXY_NAMES:
        env[name] = proxy
    for name in _NO_PROXY_NAMES:
        env[name] = ""
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        if env_in.get(name):
            env[name] = os.path.realpath(env_in[name])
    if spec.runtime == "claude":
        env["CLAUDE_CONFIG_DIR"] = home.config
        env.update(dict(_CLAUDE_FLAGS_ENV))
    elif spec.runtime == "codex":
        env["CODEX_HOME"] = home.config
    for name in _CREDENTIAL_VARS.get(spec.runtime, ()):
        if env_in.get(name):
            env[name] = env_in[name]
            break
    base_var = _BASE_URL_VARS.get(spec.runtime)
    if base_var and env_in.get(base_var):
        env[base_var] = env_in[base_var]
    if spec.git_objects:
        env["GIT_OBJECT_DIRECTORY"] = spec.git_objects
    if spec.git_alternates:
        env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = spec.git_alternates
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env[procs.TAG_VAR] = _tag(home)
    return env


def _tag(home: LaunchHome) -> str:
    """The process tag of one launch: `<launch id>-<first 12 hex of sha256(realpath(home.root))>`.

    procs.kill_tree and _sweep SIGKILL every process of this user that carries the tag, so it
    must be unique on the machine, not only in one project's record. The launch id alone
    (paths.launch_id, `<task>-<role>-t<try>`) is the same in every project, run and test root,
    and one launch's sweep would kill another launch's live agent that shares its key. The home
    lies at paths.launch_home(root, run, launch), so its realpath differs across projects, runs
    and launches. This departs from architecture.md sections 6.1 step 7, 6.5 and 6.6, which give
    the tag as the bare launch id."""
    digest = hashlib.sha256(os.path.realpath(home.root).encode("utf-8")).hexdigest()[:12]
    return "%s-%s" % (home.launch_id, digest)


# ================================================================ paths and the jail

def _inside(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def _rel_parts(path: str, base: str) -> list | None:
    """The components of path below base (both realpaths), or None when it is not below."""
    if not path.startswith(base.rstrip(os.sep) + os.sep):
        return None
    return [p for p in os.path.relpath(path, base).split(os.sep) if p]


def _subject_folders(root: str, run: str | None) -> list:
    """(realpath, depth, kind) of each per-subject folder base of section 6.1, step 3: a path is
    admitted when it lies at least `depth` components below one of them."""
    real_root = os.path.realpath(root)
    out = [(os.path.join(real_root, ".alpaca", "verify"), 2, "verify")]
    if run is not None:
        run_real = os.path.realpath(paths.run_dir(root, run))
        for sub in ("attempts", "audit", "repair", "clones"):
            out.append((os.path.join(run_real, sub), 1, sub))
        out.append((os.path.realpath(paths.worktrees_dir(root, run)), 1, "worktrees"))
    return out


def _host_git(kind: str, parts: list) -> bool:
    """True when the path is, holds or lies inside one of the engine's host git folders (section
    3.1, section 8.4): a `<tree>.git-host/` beside any tree, or `git/host/` of an attempt folder,
    which holds it (the attempt folder itself and its `git/`). No jail mounts them, so no agent
    can write the index and objects host git reads."""
    if any(p.endswith(".git-host") for p in parts):
        return True
    return kind == "attempts" and (parts[1:3] == ["git", "host"] or parts[1:] in ([], ["git"]))


def _admitted(real: str, root: str, spec: LaunchSpec, *, admin: bool = False) -> tuple | None:
    """(kind, parts) of the per-subject folder real lies in, or None. A host git folder is never
    admitted, and a `<tree>.git-admin` folder only when admin is set (the git_admin check)."""
    for base, depth, kind in _subject_folders(root, spec.run):
        parts = _rel_parts(real, base)
        if parts is None or len(parts) < depth:
            continue
        if _host_git(kind, parts):
            return None
        if not admin and any(p.endswith(".git-admin") for p in parts):
            return None
        subject = parts[0]
        if spec.attempt and kind in ("attempts", "audit") and subject != spec.attempt:
            return None
        if spec.attempt and kind == "worktrees":
            tree = re.sub(r"\.git-admin$", "", subject) if admin else subject
            if tree != spec.attempt and not _CHAMPION.fullmatch(tree):
                return None
        return kind, parts
    return None


def _contract_read_index(path: str, contract) -> int | None:
    if contract is None:
        return None
    declared = tuple(getattr(contract, "read_paths", ()) or ())
    real = tuple(getattr(contract, "read_real", ()) or ())
    for i, value in enumerate(declared):
        if path == value:
            return i
    for i, value in enumerate(real):
        if path == value or os.path.realpath(path) == value:
            return i
    return None


def check_paths(root: str, spec: LaunchSpec, contract) -> None:
    """Section 6.1, step 3: raises LaunchError for a contract read path that no longer resolves
    to its frozen realpath, and for any other spec path outside the per-subject folders listed
    there.

    Every path the spec names must be absolute (gap map F03). A contract read path must also pass
    contract.check_read_paths (no socket, FIFO or device, no writable folder). Every other path of
    workdir, cwd, read_paths, write_paths, git_objects, git_alternates and evidence_dir must lie
    inside attempts/<task>/, audit/<attempt>/, repair/<task>/ or clones/<line>/ of the run
    folder, one tree of the run's worktrees folder, or .alpaca/verify/<task>/<verification>/;
    when the spec names its attempt, an attempts/, audit/ or worktrees/ entry must be that
    attempt's (a champion checkout excepted). The engine's host git folders (every
    `<tree>.git-host/` and an attempt's `git/host/`, and the attempt folder or `git/` that holds
    one) are never a spec path, and a `<tree>.git-admin/` folder is one only as git_admin
    (section 3.1). git_admin and git_common come together: the admin
    folder is an attempt's git/admin/ or a <tree>.git-admin folder in those folders, the common
    folder a clone's .git whose worktrees/ is an empty folder. A contract of None has no read
    paths."""
    spec = _as_spec(spec)
    named = [("workdir", spec.workdir), ("cwd", spec.cwd)]
    named += [("read path", p) for p in spec.read_paths]
    named += [("write path", p) for p in spec.write_paths]
    for label, value in (("git_objects", spec.git_objects), ("git_alternates", spec.git_alternates),
                         ("evidence_dir", spec.evidence_dir), ("git_admin", spec.git_admin),
                         ("git_common", spec.git_common), ("control_mutate", spec.control_mutate),
                         ("binary", spec.binary)):
        if value is not None:
            named.append((label, value))
    for label, value in named:
        if not isinstance(value, str) or not os.path.isabs(value):
            raise LaunchError("%s is not an absolute path: %r" % (label, value))
    used_real = []
    for label, value in named:
        if label in ("git_admin", "git_common", "control_mutate", "binary"):
            continue
        index = _contract_read_index(value, contract) if label == "read path" else None
        if index is not None:
            frozen = tuple(contract.read_real)[index]
            if os.path.realpath(value) != frozen:
                raise LaunchError("read path %s now resolves to %s, not its frozen %s"
                                  % (value, os.path.realpath(value), frozen))
            used_real.append(frozen)
            continue
        if _admitted(os.path.realpath(value), root, spec) is None:
            raise LaunchError("%s %s lies outside the launch's per-subject folders"
                              % (label, value))
    if used_real:
        findings = list(contract_mod.check_read_paths(contract) or [])
        bad = [f for f in findings if str(f).split(": ", 1)[0] in used_real]
        if bad:
            raise LaunchError("read path refused: %s" % "; ".join(str(f) for f in bad))
    if (spec.git_admin is None) != (spec.git_common is None):
        raise LaunchError("git_admin and git_common are set together or not at all")
    if spec.git_admin is not None:
        admin = os.path.realpath(spec.git_admin)
        where = _admitted(admin, root, spec, admin=True)
        ok = False
        if where is not None:
            kind, parts = where
            if kind == "attempts" and parts[1:] == ["git", "admin"]:
                ok = True
            elif parts[-1].endswith(".git-admin") and kind in ("attempts", "audit", "repair",
                                                              "clones", "worktrees", "verify"):
                ok = True
        if not ok:
            raise LaunchError("git_admin %s is not an admin folder the engine makes" % admin)
        common = os.path.realpath(spec.git_common)
        where = _admitted(common, root, spec)
        if (os.path.basename(common) != ".git" or where is None
                or where[0] not in ("clones", "verify")
                or (where[0] == "clones" and where[1] != [where[1][0], ".git"])):
            raise LaunchError("git_common %s is not a clone's .git" % common)
        registry = os.path.join(common, "worktrees")
        try:
            st = os.lstat(registry)
        except OSError:
            raise LaunchError("git_common %s has no worktrees/ folder" % common)
        if not stat.S_ISDIR(st.st_mode) or os.listdir(registry):
            raise LaunchError("git_common %s: worktrees/ is not an empty folder" % common)


def _project_root_of(home: LaunchHome) -> str:
    """The project root a launch home lies in (paths.launch_home puts it under
    <root>/.alpaca/research/)."""
    marker = os.sep + os.path.join(".alpaca", "research") + os.sep
    cut = home.root.find(marker)
    if cut <= 0:
        raise LaunchError("launch home %s is not under a project's .alpaca/research/" % home.root)
    return home.root[:cut]


def _forbidden(root: str, run: str | None, environ) -> list:
    """The forbidden paths of section 3.5, rule (2), as realpaths."""
    real_root = os.path.realpath(root)
    out = [real_root] + [os.path.join(real_root, n) for n in contract_mod.FORBIDDEN_READ]
    if run is not None:
        out += [os.path.realpath(paths.run_dir(root, run)),
                os.path.realpath(paths.worktrees_dir(root, run))]
    try:
        out.append(os.path.realpath(paths.machine_lock_dir()))
    except Exception:
        pass
    out += _config_homes(environ)
    if environ.get("XDG_RUNTIME_DIR"):
        out.append(os.path.realpath(environ["XDG_RUNTIME_DIR"]))
    out.append("/run/user/%d" % os.getuid())
    return out


def _ssl_binds(root: str, run: str | None, environ) -> list:
    """The realpaths SSL_CERT_FILE and SSL_CERT_DIR name, refused inside a forbidden path."""
    out = []
    forbidden = _forbidden(root, run, environ)
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        value = environ.get(name)
        if not value:
            continue
        real = os.path.realpath(value)
        for bad in forbidden:
            if _overlaps(real, bad):
                raise LaunchError("%s=%s resolves to %s, which overlaps the forbidden %s"
                                  % (name, value, real, bad))
        if os.path.exists(real):
            out.append(real)
    return out


def jail_spec(spec: LaunchSpec, home: LaunchHome, *, contract, install: str,
              egress_dir: str) -> JailSpec:
    """The agent jail of spec by the mapping of section 6.1, step 7: rw, ro, overlays and
    egress from the spec and the home; git_hide and admins from spec.git_admin and
    spec.git_common, the admin folder and clone `.git` the engine recorded for the launch's one
    tree, never from a `.git` or `commondir` file, which the implementer can rewrite between two
    tries. A spec without git_admin, a verifier's or an auditor's, gets no `.git` at all. Reads
    and writes nothing.

    ro: the workdir unless it is a write path or lies inside one, each read path (a contract
    read path at its frozen realpath), the clone's .git, the install folder, the home's bin/,
    and the realpaths SSL_CERT_FILE and SSL_CERT_DIR name (LaunchError when one overlaps a
    forbidden path). rw: the write paths, git_objects, and the home's home/, config home and
    msg/. overlays: the credential file over its name in the config home."""
    spec = _as_spec(spec)
    writes = [str(p) for p in spec.write_paths]
    ro: list = []
    rw: list = []
    git_hide: list = []

    def add(bucket, value):
        if value and value not in bucket:
            bucket.append(value)

    workdir_written = any(_inside(spec.workdir, w) for w in writes)
    if not workdir_written:
        add(ro, spec.workdir)
    for path in spec.read_paths:
        index = _contract_read_index(path, contract)
        target = tuple(contract.read_real)[index] if index is not None else path
        add(ro, target)
        if os.path.basename(target) == ".git" and os.path.isdir(os.path.join(target, "worktrees")):
            add(git_hide, target)
    admins = []
    if spec.git_admin is not None:
        add(ro, spec.git_common)
        add(git_hide, spec.git_common)
        admins.append((spec.git_admin, spec.workdir in writes))
    add(ro, install)
    add(ro, _bin_dir(home))
    for real in _ssl_binds(_project_root_of(home), spec.run, os.environ):
        add(ro, real)
    for path in writes:
        add(rw, path)
    add(rw, spec.git_objects)
    for path in (home.home, home.config, _msg_dir(home)):
        add(rw, path)
    overlays = ()
    if home.credential_file:
        overlays = ((home.credential_file,
                     os.path.join(home.config, os.path.basename(home.credential_file))),)
    return JailSpec(kind="agent", cwd=spec.cwd, ro=tuple(ro), rw=tuple(rw),
                    git_hide=tuple(git_hide), admins=tuple(admins), overlays=overlays,
                    egress=egress_dir)


# ================================================================ reading what the agent left

def _read_inside(path: str, base: str, limit: int = _READ_MAX) -> bytes | None:
    """The bytes of path, which must lie inside base: None when it does not exist; _Broken when
    it is a link, not a regular file, or resolves outside base."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise _Broken("%s is a link or not a regular file" % path)
    if not _inside(os.path.realpath(path), os.path.realpath(base)):
        raise _Broken("%s resolves outside %s" % (path, base))
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise _Broken("%s: %s" % (path, exc))
    with os.fdopen(fd, "rb") as fh:
        return fh.read(limit)


def _real_folder(path: str, base: str) -> bool:
    """True when path is a folder, not a link, inside base; False when it is missing; _Broken
    otherwise."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise _Broken("%s is a link or not a folder" % path)
    if not _inside(os.path.realpath(path), os.path.realpath(base)):
        raise _Broken("%s resolves outside %s" % (path, base))
    return True


def _events_of(text: str) -> list:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _read_text(path: str, limit: int = _STREAM_MAX) -> str:
    """The text of a stream file, at most limit bytes: a larger file gives its first
    min(_STREAM_HEAD, limit / 8) bytes and the rest of limit from its end (the start holds the
    init and thread events, the end the result and its usage), each cut at a line break, so an
    agent that floods its stream cannot make the host read it whole."""
    first = min(_STREAM_HEAD, limit // 8)
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(0)
            if size <= limit:
                return fh.read().decode("utf-8", "replace")
            head = fh.read(first)
            fh.seek(size - (limit - first))
            tail = fh.read(limit - first)
    except OSError:
        return ""
    head = head[:head.rfind(b"\n") + 1]
    tail = tail[tail.find(b"\n") + 1:]
    return (head + tail).decode("utf-8", "replace")


def _secrets(runtime: str, home: LaunchHome, environ) -> tuple:
    """The credential values this launch hands the runtime (section 6.5): the one credential
    variable build_env passes, and the string values of the bound login copy (cred/<name>,
    never the operator's whole file) under a
    key that names a token, key, secret or password, or of 24 characters or more. The launcher
    redacts them from what outlives the launch."""
    found = []
    for name in _CREDENTIAL_VARS.get(runtime, ()):
        if environ.get(name):
            found.append(str(environ[name]))
            break
    if home.credential_file:
        try:
            with open(home.credential_file, "rb") as fh:
                data = json.loads(fh.read(_READ_MAX).decode("utf-8", "replace"))
        except (OSError, ValueError):
            data = None

        def walk(value, key=""):
            if isinstance(value, dict):
                for k, v in value.items():
                    walk(v, str(k))
            elif isinstance(value, list):
                for v in value:
                    walk(v, key)
            elif isinstance(value, str) and (len(value) >= 24 or (
                    len(value) >= 8 and _SECRET_KEY.search(key))):
                found.append(value)
        walk(data)
    out = []
    for value in sorted(set(v for v in found if len(v) >= 8), key=len, reverse=True):
        out.append(value.encode("utf-8"))
    return tuple(out)


def _copy_redacted(src_fh, dst: str, secrets: tuple, *, block: int = _REDACT_BLOCK,
                   max_bytes: int | None = None) -> bool:
    """Copy an open file into a new file dst (never through a link) in blocks of `block` bytes,
    replacing each secret value; True when one was replaced. With max_bytes, reading more than
    max_bytes bytes of the source raises _Broken (the stage's cap, check round 2 of t-098,
    adversarial B2).

    The host holds at most one block plus the carry, never a whole line: an agent that writes
    one line of gigabytes with no line break cannot make the runner read it whole (check round
    1 of t-098, adversarial B2). The last len(longest secret) - 1 bytes of each block are carried
    into the next one unless a match starts before them, so a value that spans two blocks is
    still replaced. The matches are found left to right, the longest value first at each place,
    the same as one pass over the whole file."""
    hit = False
    keep = max((len(v) for v in secrets), default=1) - 1
    pattern = (re.compile(b"|".join(re.escape(v) for v in sorted(secrets, key=len, reverse=True)))
               if secrets else None)
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as out:
        carry = b""
        total = 0
        while True:
            data = src_fh.read(block)
            total += len(data)
            if max_bytes is not None and total > max_bytes:
                raise _Broken("more than %d bytes to copy into %s" % (max_bytes, dst))
            last = not data
            buf = carry + data
            if pattern is None:
                out.write(buf)
                carry = b""
            else:
                safe = len(buf) if last else len(buf) - keep
                pos = 0
                for m in pattern.finditer(buf):
                    if m.start() >= safe:
                        break
                    out.write(buf[pos:m.start()])
                    out.write(_REDACTED)
                    pos = m.end()
                    hit = True
                end = max(pos, safe)
                out.write(buf[pos:end])
                carry = buf[end:]
            if last:
                break
    return hit


def _redact_file(path: str, secrets: tuple) -> bool:
    """Replace the secret values in a host-only file (the launch's out/ stream) in place."""
    if not secrets:
        return False
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    tmp = path + ".redact"
    _rmtree(tmp)
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            return False
        hit = _copy_redacted(fh, tmp, secrets)
    if hit:
        os.replace(tmp, path)
    else:
        os.unlink(tmp)
    return hit


def _mask(length: int) -> bytes:
    """The bytes written over one secret value of `length` bytes in a file under a write path:
    as long as the value, so the file keeps its size and its holes. `[REDACTED-CREDENTIAL]`
    padded with `*` when the value is at least that long (every real login is), else `*`s."""
    if length >= len(_REDACTED):
        return _REDACTED + b"*" * (length - len(_REDACTED))
    return b"*" * length


def _data_extents(fd: int, size: int, budget: int) -> list | None:
    """The (start, end) byte ranges of an open regular file that hold data, up to size, its
    holes left out (lseek SEEK_DATA and SEEK_HOLE). A hole reads as zero bytes, so it holds no
    secret value, and a sparse file of gigabytes costs the host no read. The whole file is one
    range when the filesystem cannot tell. None when the ranges add up to more than budget
    bytes: the file is not read at all."""
    extents, pos, total = [], 0, 0
    while pos < size:
        try:
            start = os.lseek(fd, pos, os.SEEK_DATA)
        except OSError as exc:
            if exc.errno == errno.ENXIO:          # no data at or after pos
                break
            extents, total = [(0, size)], size
            break
        if start >= size:
            break
        try:
            end = min(os.lseek(fd, start, os.SEEK_HOLE), size)
        except OSError:
            end = size
        if end <= start:
            end = size
        extents.append((start, end))
        total += end - start
        if total > budget:
            return None
        pos = end
    return None if total > budget else extents


def _mask_file(path: str, st: os.stat_result, pattern, budget: int) -> tuple:
    """Overwrite each secret value in one regular file in place, never through a link:
    (hit, bytes read), hit None when the file's data is over budget and it was not read. Only
    the data ranges are read (_data_extents), each match is overwritten with os.pwrite and a
    mask of its own length (_mask), and nothing else is written, so a sparse file stays sparse.
    A file the agent made read-only gets its owner write bit for the write and its mode back.
    OSError when the file is not the one the walk saw or cannot be written."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode) or (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
            raise OSError("%s changed during the redaction walk" % path)
        extents = _data_extents(fd, fst.st_size, budget)
        if extents is None:
            return None, 0
        found, used = [], 0
        for start, end in extents:
            data = b""
            while len(data) < end - start:
                chunk = os.pread(fd, end - start - len(data), start + len(data))
                if not chunk:
                    break
                data += chunk
            used += len(data)
            found += [(start + m.start(), m.end() - m.start()) for m in pattern.finditer(data)]
        if not found:
            return False, used
        mode = stat.S_IMODE(fst.st_mode)
        if not mode & stat.S_IWUSR:
            os.fchmod(fd, mode | stat.S_IWUSR)
        try:
            wfd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                if os.fstat(wfd).st_ino != fst.st_ino:
                    raise OSError("%s changed during the redaction walk" % path)
                for offset, length in found:
                    os.pwrite(wfd, _mask(length), offset)
            finally:
                os.close(wfd)
        finally:
            if not mode & stat.S_IWUSR:
                os.fchmod(fd, mode)
        return True, used
    finally:
        os.close(fd)


def _redact_tree(folder: str, secrets: tuple, since: float) -> tuple:
    """Overwrite the secret values in the regular files under a write path that changed during
    the launch (inode change time at or after since: an agent can set a file's mtime back, not
    its ctime), never through a link. Returns (redacted, missed, stopped): the paths, relative
    to folder, of the files where a value was overwritten; of the changed files (and folders
    it could not list, with a trailing /) it did not check; and whether the walk stopped at
    _SCAN_MAX entries.

    This runs after the runtime has ended, outside the launch's timeout, so the host's work is
    bounded (check round 4 of t-098, adversarial B1: a rewrite of the whole file turned 300
    sparse files of 8 MiB, which cost the agent no disk, into 2.5 GB written on the host). The
    walk counts folders and files against _SCAN_MAX entries; only a file's data ranges are
    read, never its holes; a file with more than _READ_MAX bytes of data is not read, and all
    reads count against one total of _REDACT_TREE_MAX bytes, past which a file is not read; and
    a match is overwritten in place with a mask of its length, so the host writes only the
    masks. Every changed file left unchecked is listed in missed, and research-launch-end names
    them."""
    if not secrets:
        return [], [], False
    pattern = re.compile(b"|".join(re.escape(v) for v in sorted(secrets, key=len, reverse=True)))
    hits, missed, budget, seen = [], [], _REDACT_TREE_MAX, 0

    def unlisted(exc):
        missed.append(os.path.relpath(exc.filename, folder) + "/")

    for base, dirs, names in os.walk(folder, onerror=unlisted, followlinks=False):
        seen += len(dirs)
        if seen > _SCAN_MAX:
            return hits, missed, True
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(base, d))]
        for name in names:
            seen += 1
            if seen > _SCAN_MAX:
                return hits, missed, True
            path = os.path.join(base, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_ctime < since:
                continue
            rel = os.path.relpath(path, folder)
            try:
                hit, used = _mask_file(path, st, pattern, min(budget, _READ_MAX))
            except OSError:
                missed.append(rel)
                continue
            budget -= used
            if hit is None:
                missed.append(rel)
            elif hit:
                hits.append(rel)
    return hits, missed, False


def _stage_copy(src: str, dst: str, base: str, secrets: tuple, limit: int) -> int:
    """Copy one agent-written file into the stage by its bytes and return how many it read:
    opened with O_NOFOLLOW, a regular file whose realpath lies inside base and whose size is at
    most limit bytes, else _Broken. The size is checked on the open file before a byte is read,
    and the copy stops at limit bytes should the file grow, so a sparse file of gigabytes (which
    costs the agent no disk) is refused at once instead of written out whole on the host."""
    if not _inside(os.path.realpath(src), os.path.realpath(base)):
        raise _Broken("%s resolves outside %s" % (src, base))
    try:
        fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise _Broken("%s: %s" % (src, exc))
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise _Broken("%s is not a regular file" % src)
        if st.st_size > limit:
            raise _Broken("%s is %d bytes, over the %d bytes left of the stage's %d"
                          % (src, st.st_size, limit, _TRANSCRIPT_MAX))
        _copy_redacted(fh, dst, secrets, max_bytes=limit)
        return fh.tell()


def _stage_transcript(src: str, home: LaunchHome, secrets: tuple) -> str:
    """Copy the transcript and the subagent transcripts beside it into <home>/stage/, a folder
    no jail mounts, and return the staged transcript: transcripts.snapshot then reads only the
    stage (section 6.1, step 8). The sibling folder <stem>/ and every folder below
    <stem>/subagents/ must be a real folder inside the home, and every file there a regular
    file: a link anywhere makes the launch lost (_Broken), since transcripts._subagent_pairs
    checks each file against the realpath of a base folder that could itself be a link. Only
    the agent-*.jsonl files the snapshot reads are copied, each by its bytes with the secret
    values replaced.

    The copy runs after the runtime has ended, outside the launch's timeout, so it is bounded:
    the transcript and every subagent file count against one total of _TRANSCRIPT_MAX bytes,
    and the folders and files under subagents/ against _SCAN_MAX entries. Past either the launch
    is lost (_Broken), and nothing larger is written on the host."""
    stage = os.path.join(home.root, "stage")
    _rmtree(stage)
    os.mkdir(stage, 0o700)
    name = os.path.basename(src)
    staged = os.path.join(stage, name)
    left = _TRANSCRIPT_MAX - _stage_copy(src, staged, home.root, secrets, _TRANSCRIPT_MAX)
    stem = os.path.splitext(name)[0]
    side = os.path.join(os.path.dirname(src), stem)
    if not _real_folder(side, home.root):
        return staged
    sub = os.path.join(side, "subagents")
    if not _real_folder(sub, home.root):
        return staged
    seen = 0
    for folder, dirs, names in os.walk(sub, followlinks=False):
        seen += len(dirs) + len(names)
        if seen > _SCAN_MAX:
            raise _Broken("more than %d entries under %s" % (_SCAN_MAX, sub))
        for d in dirs:
            _real_folder(os.path.join(folder, d), home.root)
        target = os.path.join(stage, stem, os.path.relpath(folder, side))
        os.makedirs(target, 0o700, exist_ok=True)
        for leaf in names:
            path = os.path.join(folder, leaf)
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                raise _Broken("%s is a link or not a regular file" % path)
            if leaf.startswith("agent-") and leaf.endswith(".jsonl"):
                left -= _stage_copy(path, os.path.join(target, leaf), home.root, secrets, left)
    return staged


def _tail(path: str, limit: int = _TAIL_BYTES) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _claude_transcript(home: LaunchHome, spec: LaunchSpec) -> str | None:
    projects = os.path.join(home.config, "projects")
    if not _real_folder(home.config, home.root) or not _real_folder(projects, home.root):
        return None
    name = _claude_session_uuid(spec) + ".jsonl"
    found = []
    for entry in sorted(os.listdir(projects)):
        folder = os.path.join(projects, entry)
        st = os.lstat(folder)
        if stat.S_ISLNK(st.st_mode):
            if os.path.lexists(os.path.join(folder, name)):
                raise _Broken("%s is a link" % folder)
            continue
        if not stat.S_ISDIR(st.st_mode):
            continue
        candidate = os.path.join(folder, name)
        if os.path.lexists(candidate):
            found.append(candidate)
    if len(found) > 1:
        raise _Broken("%d transcripts named %s" % (len(found), name))
    if not found:
        return None
    _read_inside(found[0], home.root, limit=1)
    return found[0]


def _codex_thread(stream_text: str) -> str | None:
    for event in _events_of(stream_text):
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            return event["thread_id"]
    return None


def _check_sessions(sessions: str, thread: str) -> None:
    """Walk the agent-writable Codex sessions folder before transcripts.discover_codex opens
    anything in it (check round 3, adversarial B1). discover_codex skips a link but opens every
    other file whose name ends in <thread>.jsonl with a plain open(), which waits forever on a
    FIFO the agent planted there: every process of the jail is dead by now, so no writer comes,
    and the collect step runs after procs.run returned, outside the launch's timeout. So here
    every name that ends in <thread>.jsonl is lstat()ed, and one that is not a regular file (a
    FIFO, a socket, a device, a link) makes the launch lost (_Broken); the folders and files
    walked count against _SCAN_MAX. The walk does not follow links. No process of the launch is
    alive when this runs (the jail's PID namespace ended and _sweep ran), so nothing can swap a
    checked file before discover_codex opens it."""
    suffix = thread + ".jsonl"
    seen = 0
    for folder, dirs, names in os.walk(sessions, followlinks=False):
        seen += len(dirs) + len(names)
        if seen > _SCAN_MAX:
            raise _Broken("more than %d entries under %s" % (_SCAN_MAX, sessions))
        for leaf in names:
            if not leaf.endswith(suffix):
                continue
            path = os.path.join(folder, leaf)
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode):
                raise _Broken("%s is a link or not a regular file" % path)


def _codex_transcript(home: LaunchHome, spec: LaunchSpec, stream_text: str) -> str | None:
    from alpaca import transcripts
    sessions = os.path.join(home.config, "sessions")
    if not _real_folder(home.config, home.root) or not _real_folder(sessions, home.root):
        return None
    thread = _codex_thread(stream_text)
    if not thread:
        return None
    _check_sessions(sessions, thread)
    try:
        found = transcripts.discover_codex(spec.cwd, thread, storage_root=sessions)
    except ValueError as exc:
        raise _Broken("codex transcript: %s" % exc)
    if not found:
        return None
    _read_inside(found, home.root, limit=1)
    return found


def _final_message(runtime: str, home: LaunchHome, stream_text: str) -> str | None:
    if runtime == "codex":
        data = _read_inside(os.path.join(_msg_dir(home), "last-message.txt"), _msg_dir(home))
        return data.decode("utf-8", "replace") if data is not None else None
    final = None
    for event in _events_of(stream_text):
        if event.get("type") == "result":
            if event.get("structured_output") is not None:
                final = json.dumps(event["structured_output"], sort_keys=True)
            elif isinstance(event.get("result"), str):
                final = event["result"]
    return final


def _stream_usage(runtime: str, stream_text: str) -> dict:
    """Tokens, the reported cost and the model the runtime's own events state."""
    tokens = None
    cost = None
    model = None
    parts: dict = {}
    for event in _events_of(stream_text):
        kind = event.get("type")
        if kind == "system" and isinstance(event.get("model"), str):
            model = event["model"]
        if kind == "result":
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            got = {k: usage.get(k) for k in ("input_tokens", "output_tokens",
                                              "cache_creation_input_tokens",
                                              "cache_read_input_tokens")}
            if all(isinstance(v, int) for v in (got["input_tokens"], got["output_tokens"])):
                parts = {k: v for k, v in got.items() if isinstance(v, int)}
                tokens = sum(parts.values())
            if isinstance(event.get("total_cost_usd"), (int, float)):
                cost = float(event["total_cost_usd"])
        if kind == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
            if isinstance(usage.get("input_tokens"), int) and isinstance(
                    usage.get("output_tokens"), int):
                parts["input_tokens"] = parts.get("input_tokens", 0) + usage["input_tokens"]
                parts["output_tokens"] = parts.get("output_tokens", 0) + usage["output_tokens"]
                if isinstance(usage.get("cached_input_tokens"), int):
                    parts["cache_read_input_tokens"] = parts.get(
                        "cache_read_input_tokens", 0) + usage["cached_input_tokens"]
                tokens = parts["input_tokens"] + parts["output_tokens"]
    return {"tokens": tokens, "cost_usd": cost, "model": model, "parts": parts}


def _usage(root: str, spec: LaunchSpec, stream_path: str, stream_text: str,
           have_transcript: bool) -> dict:
    """Tokens and cost: the larger of what metrics.analyze measures over the snapshotted
    transcript and what the runtime's own result or turn.completed events in the stream state,
    each figure on its own, and both in `sources`. The transcript lives in the agent-writable
    config home, so an agent could make it state less than it spent (check round 4 of t-098,
    adversarial m2); the stream is kept on the host by the relay. When the transcript measured
    nothing, the stream's usage is imported with analytics.usage.ingest so the session's
    analytics see it. Never fails the launch."""
    found = _stream_usage(spec.runtime, stream_text)
    tokens, cost = None, None
    if have_transcript:
        try:
            from alpaca.analytics import metrics
            analysis = metrics.analyze(root, spec.session)
            total = ((analysis.get("summary") or {}).get("tokens") or {}).get("total_tokens")
            if isinstance(total, int) and total > 0:
                tokens = total
                costs = analysis.get("cost") or {}
                for key in ("reported_usd", "estimated_usd"):
                    if isinstance(costs.get(key), (int, float)):
                        cost = float(costs[key])
                        break
        except Exception:
            tokens, cost = None, None
    sources = {"transcript": {"tokens": tokens, "cost_usd": cost},
               "stream": {"tokens": found["tokens"], "cost_usd": found["cost_usd"]}}
    if tokens is None and found["tokens"] is not None:
        tokens = found["tokens"]
        parts = found["parts"]
        sample = {"sample_id": "%s-stream" % paths.launch_id(spec.key),
                  "provider": "anthropic" if spec.runtime == "claude" else "openai",
                  "model": found["model"] or "unknown",
                  "tokens": {"input": parts.get("input_tokens", 0),
                             "output": parts.get("output_tokens", 0),
                             "cache_read": parts.get("cache_read_input_tokens")}}
        if found["cost_usd"] is not None:
            sample["reported_cost_usd"] = found["cost_usd"]
            sample["currency"] = "USD"
        try:
            from alpaca.analytics import usage
            usage_file = os.path.join(os.path.dirname(stream_path), "usage.json")
            with open(usage_file, "w", encoding="utf-8") as fh:
                json.dump({"samples": [sample]}, fh, sort_keys=True)
            usage.ingest(root, spec.session, usage_file)
        except Exception:
            pass
    if found["tokens"] is not None and found["tokens"] > tokens:
        tokens = found["tokens"]
    if found["cost_usd"] is not None and (cost is None or found["cost_usd"] > cost):
        cost = found["cost_usd"]
    return {"tokens": tokens, "cost_usd": cost, "model": found["model"], "sources": sources}


# ================================================================ failure classes

def _error_texts(events_list: list) -> list:
    out = []
    for event in events_list:
        kind = event.get("type")
        if kind == "result" and event.get("is_error"):
            out.append(str(event.get("result") or event.get("subtype") or "error"))
            for err in event.get("errors") or ():
                out.append(str(err))
        elif kind in ("error", "turn.failed"):
            err = event.get("error")
            if isinstance(err, dict):
                out.append(str(err.get("message") or err))
            out.append(str(event.get("message") or ""))
        elif kind == "assistant" and event.get("error"):
            out.append(str(event.get("error")))
    return [t for t in out if t]


def _final_ok(events_list: list) -> bool:
    for event in events_list:
        kind = event.get("type")
        if kind == "result" and not event.get("is_error"):
            return True
        if kind == "turn.completed":
            return True
    return False


def classify_failure(result: LaunchResult, stream_tail: str) -> str | None:
    """None, or one of "start-failed", "quota", "auth", "crash" (FR-042 classes).

    A timeout, a stop, a hold or a lost launch is not classified here (the first two are the
    launch's own verdict). No stream event at all is start-failed, whatever the exit code (the
    runtime or the jail exited before its first event, section 6.7): a runtime that exits 0
    having printed nothing produced no usable result. Then a run with exit 0 and no error event
    is None. Otherwise a rate or usage-limit refusal in the runtime's error events (a Claude
    result with is_error, a Codex error or turn.failed) is quota; an authentication refusal in
    the error text is auth; a non-zero exit with no final result is crash. The error text is the
    error events, else the stream's lines that are not JSON (stderr). Quota needs an error event:
    a rate-limit line on stderr alone, which any process holding the runtime's stdout can print,
    is crash, so an agent cannot make the loop wait out a quota backoff by printing one (check
    round 2 of t-098, adversarial m1). Every runtime prints JSON events, the control agent
    included: its final message is read from a result event."""
    if result.timed_out or result.outcome in ("timeout", "killed", "held", "lost"):
        return None
    evs = _events_of(stream_tail or "")
    if not any("type" in e for e in evs):
        return "start-failed"
    errors = _error_texts(evs)
    final_ok = _final_ok(evs)
    if result.rc == 0 and not errors:
        return None
    if result.rc not in (0, None) and final_ok and not errors:
        return None
    text = "\n".join(errors) or "\n".join(
        line for line in (stream_tail or "").splitlines() if not line.strip().startswith("{"))
    if errors and _QUOTA.search(text):
        return "quota"
    if _AUTH.search(text):
        return "auth"
    return "crash"


def _reset_at(stream_tail: str) -> str | None:
    """The reset time a quota refusal states, as an ISO time: an epoch after "|" (Claude's usage
    limit text), a number of seconds or minutes to wait, or an ISO time in the text."""
    text = "\n".join(_error_texts(_events_of(stream_tail or ""))) or stream_tail or ""
    found = re.search(r"limit[^|\n]*\|\s*([0-9]{9,11})\b", text, re.IGNORECASE)
    if found:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(found.group(1))))
    found = re.search(r"(?:in|after)\s+([0-9]{1,6})\s*(seconds?|secs?|s|minutes?|mins?|m)\b",
                      text, re.IGNORECASE)
    if found:
        amount = int(found.group(1))
        if found.group(2).lower().startswith("m"):
            amount *= 60
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + amount))
    found = re.search(r"reset[^0-9\n]{0,40}([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+(?:Z|[+-][0-9:]+)?)",
                      text, re.IGNORECASE)
    if found:
        return found.group(1)
    return None


def _wait_until(reset_at: str | None, try_n: int) -> str:
    if reset_at:
        return reset_at
    wait = min(_QUOTA_BASE_S * (2 ** max(0, int(try_n) - 1)), _QUOTA_MAX_S)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + wait))


# ================================================================ tool calls

_CLAUDE_KINDS = {"Read": "read", "Glob": "read", "Grep": "read", "LS": "read",
                 "NotebookRead": "read", "Write": "write", "Edit": "write", "MultiEdit": "write",
                 "NotebookEdit": "write", "Bash": "shell", "BashOutput": "shell",
                 "KillShell": "shell", "KillBash": "shell", "WebFetch": "web", "WebSearch": "web"}
_PATCH_PATH = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$",
                         re.MULTILINE)


def _patch_paths(text: str) -> list:
    out = []
    flat = text.replace("\\\\n", "\n").replace("\\n", "\n")
    for found in _PATCH_PATH.finditer(flat):
        path = (found.group(1) or found.group(2) or "").strip().rstrip("\\").strip().strip('"')
        if path and path not in out:
            out.append(path)
    return out


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), (str, list)):
                    parts.append(_content_text(item["content"]))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    if content is None:
        return ""
    return json.dumps(content, sort_keys=True)


def _claude_calls(evs: list) -> list:
    calls, by_id = [], {}
    text: list = []
    for event in evs:
        kind = event.get("type")
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        content = message.get("content")
        if kind == "assistant" and isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    text.append(item["text"])
                elif item.get("type") == "tool_use":
                    name = str(item.get("name") or "")
                    raw = item.get("input") if isinstance(item.get("input"), dict) else {}
                    call_kind = _CLAUDE_KINDS.get(name, "other")
                    if call_kind == "shell":
                        value = raw.get("command") if isinstance(raw.get("command"), str) else ""
                        call_paths = _patch_paths(value) if "*** Begin Patch" in value else []
                    else:
                        value = raw
                        call_paths = [str(raw[k]) for k in ("file_path", "notebook_path", "path")
                                      if isinstance(raw.get(k), str)]
                    call = {"tool": name, "kind": call_kind, "input": value, "paths": call_paths,
                            "ts": event.get("timestamp"), "text": "\n".join(text), "output": None,
                            "id": item.get("id"), "raw_input": raw, "error": None}
                    text = []
                    calls.append(call)
                    if item.get("id"):
                        by_id[item["id"]] = call
        elif kind == "user" and isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    call = by_id.get(item.get("tool_use_id"))
                    if call is not None and call["output"] is None:
                        call["output"] = _content_text(item.get("content"))
                        call["error"] = bool(item.get("is_error"))
    return calls


def _codex_patch_form(command: str) -> bool:
    """U9 (t-354, in-session-runtime.md section 27.1): True when a Codex command_execution is a
    patch by its form: the text starts with `*** Begin Patch`, or it parses (a `bash -lc` wrapper
    opened) as the one command `apply_patch` with its patch as a heredoc or an argument. A text
    that only holds the word (a comment, an echo, a chain around it) is a shell call, read by the
    shell reader of the guard and of S15."""
    if command.lstrip().startswith("*** Begin Patch"):
        return True
    from alpaca.research import pathscan
    try:
        cmds = pathscan.unwrap(pathscan.parse(command)).get("commands") or []
    except Exception:  # noqa: BLE001 - not a patch form: the shell reader reads it (M1)
        return False
    return len(cmds) == 1 and cmds[0]["argv"][:1] == ["apply_patch"] \
        and all(op.startswith("<<") for op, _t in cmds[0]["redirects"])


def _codex_item_call(item: dict, ts) -> dict | None:
    kind = item.get("type")
    if kind == "command_execution":
        command = str(item.get("command") or "")
        if _codex_patch_form(command):
            return {"tool": "apply_patch", "kind": "write", "input": command,
                    "paths": _patch_paths(command), "ts": ts, "text": "",
                    "output": item.get("aggregated_output")}
        return {"tool": "command_execution", "kind": "shell", "input": command, "paths": [],
                "ts": ts, "text": "", "output": item.get("aggregated_output")}
    if kind == "file_change":
        changes = item.get("changes") if isinstance(item.get("changes"), list) else []
        found = [str(c.get("path")) for c in changes if isinstance(c, dict) and c.get("path")]
        return {"tool": "file_change", "kind": "write", "input": changes, "paths": found,
                "ts": ts, "text": "", "output": item.get("status")}
    if kind == "web_search":
        return {"tool": "web_search", "kind": "web", "input": item.get("query"), "paths": [],
                "ts": ts, "text": "", "output": None}
    if kind == "mcp_tool_call":
        return {"tool": "%s.%s" % (item.get("server"), item.get("tool")), "kind": "other",
                "input": item.get("arguments"), "paths": [], "ts": ts, "text": "",
                "output": item.get("result")}
    return None


def _codex_calls(evs: list) -> list:
    """codex exec --json events, and the rollout records of a saved Codex transcript."""
    calls, by_id = [], {}
    text: list = []
    for event in evs:
        kind = event.get("type")
        ts = event.get("timestamp")
        if kind in ("item.started", "item.updated", "item.completed") and isinstance(
                event.get("item"), dict):
            item = event["item"]
            if item.get("type") == "agent_message" and kind == "item.completed":
                if isinstance(item.get("text"), str):
                    text.append(item["text"])
                continue
            ident = item.get("id")
            if ident in by_id:
                call = by_id[ident]
                fresh = _codex_item_call(item, ts)
                if fresh is not None:
                    if fresh["output"] not in (None, ""):
                        call["output"] = fresh["output"]
                    if fresh["paths"] and not call["paths"]:
                        call["paths"] = fresh["paths"]
                continue
            call = _codex_item_call(item, ts)
            if call is None:
                continue
            call["text"] = "\n".join(text)
            text = []
            call["id"] = ident
            calls.append(call)
            if ident is not None:
                by_id[ident] = call
        elif kind == "response_item" and isinstance(event.get("payload"), dict):
            payload = event["payload"]
            ptype = payload.get("type")
            if ptype == "message" and payload.get("role") == "assistant":
                text.append(_content_text(payload.get("content")))
            elif ptype in ("function_call", "custom_tool_call", "local_shell_call"):
                name = str(payload.get("name") or ptype)
                raw = payload.get("arguments", payload.get("input", payload.get("action")))
                raw_text = raw if isinstance(raw, str) else json.dumps(raw, sort_keys=True)
                command = raw_text
                parsed = raw if isinstance(raw, dict) else None
                if isinstance(raw, str):
                    try:
                        parsed = json.loads(raw)
                    except ValueError:
                        parsed = None
                workdir = None
                if isinstance(parsed, dict):
                    cmd = parsed.get("cmd", parsed.get("command"))
                    if isinstance(cmd, list):
                        # an argv list, quoted so a shell reader sees its words
                        # (`["bash", "-lc", "sed -i x f"]`)
                        command = shlex.join(str(c) for c in cmd)
                    elif isinstance(cmd, str):
                        command = cmd
                    # the folder the command runs in (exec_command's workdir, a local shell
                    # call's working_directory): the audit reads its relative paths there
                    folder = parsed.get("workdir", parsed.get("working_directory"))
                    workdir = folder if isinstance(folder, str) and folder else None
                if "apply_patch" in name or "*** Begin Patch" in raw_text:
                    call = {"tool": name, "kind": "write", "input": command,
                            "paths": _patch_paths(command)}
                elif name in ("exec", "exec_command", "shell", "local_shell_call",
                              "container.exec"):
                    call = {"tool": name, "kind": "shell", "input": command, "paths": []}
                elif name in ("web_search", "web.run"):
                    call = {"tool": name, "kind": "web", "input": raw, "paths": []}
                else:
                    call = {"tool": name, "kind": "other", "input": raw, "paths": []}
                call.update({"ts": ts, "text": "\n".join(text), "output": None,
                             "id": payload.get("call_id")})
                if workdir is not None:
                    call["workdir"] = workdir
                text = []
                calls.append(call)
                if payload.get("call_id"):
                    by_id[payload["call_id"]] = call
            elif ptype in ("function_call_output", "custom_tool_call_output"):
                call = by_id.get(payload.get("call_id"))
                if call is not None and call["output"] is None:
                    out = payload.get("output")
                    call["output"] = out if isinstance(out, str) else _content_text(out)
            elif ptype == "web_search_call":
                calls.append({"tool": "web_search", "kind": "web",
                              "input": payload.get("action"), "paths": [], "ts": ts,
                              "text": "\n".join(text), "output": None, "id": payload.get("id")})
                text = []
    return calls


def tool_calls(transcript: str, runtime: str, keep_ids: bool = False) -> list:
    """t-089's reader (verifier.md section 12): one {tool, kind, input, paths, ts, text, output}
    per tool call of a Claude stream-json or Codex JSON stream, read from the launch's captured
    stream; kind is read, write, shell, web or other.

    transcript is the stream file's path, or the stream text itself when no file is there.
    Calls come in the order their input appears. A shell call's input is its full command text
    (heredoc bodies included); a write's paths are its targets (for Codex apply_patch, also
    inside an exec call, and file_change items, the paths the patch headers or changes name);
    text is the agent's own message text since the call before; output is the call's result as
    the stream carries it, or None. Lines that are not JSON are skipped.

    What a call proves: bytes already in the stream cannot be changed or taken back (the stdio
    relay, _RELAY), but any process in the jail that holds the runtime's stdout can append a
    line, so a call read here shows its place in the stream, not that the runtime made it. A
    caller that needs the call's author checks it against the transcript or a receipt.

    A Claude call also carries `raw_input` (the tool's input object as written) and `error` (the
    result's `is_error`, None when the call has no result). `keep_ids` keeps each call's `id`, which
    the judge needs to tell a call the guard denied from one that ran (jobs.ran_calls)."""
    if runtime not in RUNTIMES:
        raise ValueError("unknown runtime %r" % (runtime,))
    text = transcript
    if isinstance(transcript, str) and "\n" not in transcript and os.path.isfile(transcript):
        text = _read_text(transcript)
    evs = _events_of(text or "")
    calls = _codex_calls(evs) if runtime == "codex" else _claude_calls(evs)
    if not keep_ids:
        for call in calls:
            call.pop("id", None)
    return calls


# ================================================================ record helpers

def _find_event(conn, kind: str, key: str, extra: str = "", params: tuple = ()):
    row = conn.execute(
        "SELECT id, data FROM events WHERE kind=? AND json_extract(data, '$.key')=? %s "
        "ORDER BY id DESC LIMIT 1" % extra, (kind, key) + tuple(params)).fetchone()
    if row is None:
        return None
    return {"id": row[0], "data": json.loads(row[1])}


def _sha256_file(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def _result(spec: LaunchSpec, **fields) -> dict:
    base = {"session": spec.session, "rc": None, "timed_out": False, "held": False,
            "launch_id": paths.launch_id(spec.key), "key": spec.key, "role": spec.role,
            "try_n": spec.try_n, "outcome": "ok", "attempt": spec.attempt}
    base.update(fields)
    base["killed"] = tuple(base.get("killed") or ())
    return rtypes.to_dict(LaunchResult(**base))


def _final_of(d: dict) -> str | None:
    """The final message of a research-launch-end: the event keeps its first _FINAL_MAX
    characters, and a longer one is read back from final-message.txt beside the stream when its
    sha256 is the recorded one."""
    final = d.get("final_message")
    if not d.get("final_message_truncated") or not d.get("stream"):
        return final
    path = os.path.join(os.path.dirname(d["stream"]), "final-message.txt")
    try:
        with open(path, "rb") as fh:
            data = fh.read(_STREAM_MAX)
    except OSError:
        return final
    if hashlib.sha256(data).hexdigest() != d.get("final_message_sha256"):
        return final
    return data.decode("utf-8", "replace")


def _from_end(spec: LaunchSpec, end: dict) -> dict:
    """The result a recorded research-launch-end stands for (a resumed step)."""
    d = end["data"]
    return _result(spec, session=d.get("session", spec.session), rc=d.get("rc"),
                   timed_out=bool(d.get("timed_out")), outcome=d.get("outcome", "ok"),
                   home=d.get("home") or "", duration_s=float(d.get("duration_s") or 0.0),
                   stream=d.get("stream") or "", stream_sha256=d.get("stream_sha256"),
                   launch_event=d.get("launch_event"), launch_end=end["id"],
                   infra=d.get("infra"), reason=d.get("reason") or "",
                   transcript=d.get("transcript"), transcript_sha256=d.get("transcript_sha256"),
                   final_message=_final_of(d), egress_log=d.get("egress_log"),
                   egress_sha256=d.get("egress_sha256"), tokens=d.get("tokens"),
                   cost_usd=d.get("cost_usd"), priced=bool(d.get("priced")),
                   reset_at=d.get("reset_at"), killed=tuple(d.get("killed_pids") or ()))


def _ref(spec: LaunchSpec) -> str:
    return spec.attempt or spec.run or spec.key


def _record_infra(conn, spec: LaunchSpec, cls: str, detail: str, *, wait_until=None,
                  in_txn: bool = False) -> dict:
    return events.emit(conn, events.INFRA, run=spec.run, ref=_ref(spec), op=spec.op,
                       session=spec.session, code=vc.BLOCKED,
                       data={"attempt": spec.attempt, "role": spec.role, "try": spec.try_n,
                             "class": cls, "detail": str(detail)[:2000],
                             "wait_until": wait_until, "key": spec.key,
                             "launch": paths.launch_id(spec.key)},
                       in_txn=in_txn)


def _evidence_dir(root: str, spec: LaunchSpec, launch: str) -> str:
    """spec.evidence_dir, else attempts/<attempt>/launches/<launch>/ of the run (section 6.1,
    step 9). A launch with neither has no evidence folder the design names: LaunchError."""
    if spec.evidence_dir:
        return spec.evidence_dir
    if spec.run is not None and spec.attempt:
        return os.path.join(paths.attempt_dir(root, spec.run, spec.attempt), "launches", launch)
    raise LaunchError("a launch needs evidence_dir, or a run and an attempt for "
                      "attempts/<attempt>/launches/<launch>/")


def _copy_out(src: str, dst: str, *, exclude: tuple = ()) -> list:
    """Copy out/ into the evidence folder by bytes: each entry opened with O_NOFOLLOW, a link or
    a special file skipped, never followed. A top-level file named in `exclude` is left as the
    evidence folder has it (the prompt, copied there before the run). Returns the relative paths
    skipped."""
    skipped = []
    os.makedirs(dst, exist_ok=True)
    for folder, dirs, names in os.walk(src, followlinks=False):
        rel = os.path.relpath(folder, src)
        target = dst if rel == "." else os.path.join(dst, rel)
        keep = []
        for name in dirs:
            if os.path.islink(os.path.join(folder, name)):
                skipped.append(os.path.normpath(os.path.join(rel, name)))
            else:
                keep.append(name)
                os.makedirs(os.path.join(target, name), exist_ok=True)
        dirs[:] = keep
        for name in names:
            if rel == "." and name in exclude:
                continue
            path = os.path.join(folder, name)
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            except OSError:
                skipped.append(os.path.normpath(os.path.join(rel, name)))
                continue
            with os.fdopen(fd, "rb") as fh:
                if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                    skipped.append(os.path.normpath(os.path.join(rel, name)))
                    continue
                out = os.path.join(target, name)
                if os.path.lexists(out):
                    os.unlink(out)
                ofd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
                with os.fdopen(ofd, "wb") as oh:
                    shutil.copyfileobj(fh, oh, 1 << 20)      # in chunks, never whole
    return skipped


# The host side of the jail's stdin and stdout (check round 3 of t-098, B1). procs.run opens the
# prompt and the stream as files and hands them to its child as fds 0, 1 and 2. When that child
# is bwrap, every process in the jail holds those files, and any of them can reopen a file on its
# host mount through /proc/<pid>/fd/0 or /proc/<pid>/fd/1 and seek over, truncate or rewrite it.
# So procs.run starts this relay instead, with the host's python in isolated mode (-I -S, no
# import from the agent's working directory). It keeps the two files on the host, outside the
# jail's PID namespace, and gives bwrap one end of a socket pair for stdin and one for stdout and
# stderr: open() of a socket through /proc/<pid>/fd fails with ENXIO, and the jail's end of each
# pair is shut down in the other direction. A thread copies the prompt into the stdin socket and
# half-closes it; the main loop copies the stdout socket into the stream file, in order, until
# the socket ends, or for at most `grace` seconds once bwrap has exited (a jail's processes die
# with its PID namespace, so the end comes at once; the grace only bounds a process that holds
# the socket outside a jail). It writes at most `cap` + 1 bytes to the stream file and drains
# the rest unwritten, so the runtime never blocks on a full socket and the file on the host stays
# bounded; a stream file over `cap` bytes makes the launch lost (_STREAM_DISK_MAX, check round
# 3, adversarial m1). The relay exits with bwrap's code, or dies of bwrap's signal.
# bwrap's --die-with-parent now watches the relay, not the runner, so the relay asks the kernel
# for SIGKILL when the runner ends (PR_SET_PDEATHSIG) and exits at once when the runner is
# already gone: a SIGKILL of the runner still ends every agent jail (architecture.md 10.2).
_RELAY_GRACE_S = 1.0
_RELAY = r"""
import ctypes, os, select, signal, socket, subprocess, sys, threading, time

# Die with the runner (section 10.2): bwrap's --die-with-parent watches this relay, so the relay
# must watch the runner. PR_SET_PDEATHSIG (1) sends SIGKILL when the parent ends; a runner that
# ended before the prctl call is seen by the parent pid check, which closes the race.
try:
    _prctl = ctypes.CDLL(None, use_errno=True).prctl
    _armed = _prctl(1, signal.SIGKILL, 0, 0, 0) == 0
except (OSError, AttributeError):
    _armed = False
if not _armed or os.getppid() != int(sys.argv[3]):
    os.write(2, b"stdio relay: cannot tie this relay to its runner\n")
    os._exit(126)

CHUNK = 65536
GRACE_S = float(sys.argv[1])
CAP = int(sys.argv[2])
kept = 0
cmd = sys.argv[sys.argv.index("--") + 1:]
out_host, out_jail = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
in_host, in_jail = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
out_jail.shutdown(socket.SHUT_RD)
in_jail.shutdown(socket.SHUT_WR)


def put(data):
    global kept
    if kept > CAP:
        return                     # past the cap: drained, not written
    data = data[:CAP + 1 - kept]
    kept += len(data)
    view = memoryview(data)
    while view:
        view = view[os.write(1, view):]


def feed():
    try:
        while True:
            data = os.read(0, CHUNK)
            if not data:
                break
            in_host.sendall(data)
    except OSError:
        pass
    try:
        in_host.shutdown(socket.SHUT_WR)
    except OSError:
        pass


try:
    child = subprocess.Popen(cmd, stdin=in_jail.fileno(), stdout=out_jail.fileno(),
                             stderr=out_jail.fileno(), close_fds=True)
except OSError as exc:
    put(("stdio relay: cannot start %r: %s\n" % (cmd[:1], exc)).encode("utf-8", "replace"))
    os._exit(127)
in_jail.close()
out_jail.close()
threading.Thread(target=feed, daemon=True).start()
rc, deadline = None, None
while True:
    if rc is None:
        rc = child.poll()
        if rc is not None:
            deadline = time.monotonic() + GRACE_S
    now = time.monotonic()
    if deadline is not None and now >= deadline:
        break
    wait = 0.1 if deadline is None else min(0.1, deadline - now)
    if select.select([out_host], [], [], wait)[0]:
        data = out_host.recv(CHUNK)
        if not data:
            break
        put(data)
out_host.close()
if rc is None:
    rc = child.wait()
if rc < 0:
    try:
        signal.signal(-rc, signal.SIG_DFL)
    except (OSError, ValueError):
        pass
    os.kill(os.getpid(), -rc)
    time.sleep(1)
os._exit(rc & 0xFF)
"""


def _relay_argv() -> list:
    """The argv prefix that runs the jail under the stdio relay (_RELAY): the host's own
    interpreter, by absolute path, in isolated mode. The caller appends the bwrap argv and
    starts it from this process with procs.run: the relay checks that its parent is this
    process's pid, and it dies with this process (PR_SET_PDEATHSIG), so bwrap, which dies with
    the relay (--die-with-parent), dies with the runner too. The kernel sends that signal when
    the thread that started the relay ends; procs.run waits for its child in that thread, so the
    thread outlives the relay unless the runner dies. The relay writes at most
    _STREAM_DISK_MAX + 1 bytes of the stream, read here when the argv is built."""
    python = sys.executable
    if not python or not os.path.isabs(python):
        raise LaunchError("no absolute path to this interpreter for the stdio relay: %r"
                          % (python,))
    return [python, "-I", "-S", "-c", _RELAY, repr(_RELAY_GRACE_S), str(int(_STREAM_DISK_MAX)),
            str(os.getpid()), "--"]


def _sweep(tag: str) -> tuple:
    """After the runtime ended: kill every process that still carries the launch tag (a child
    that double-forked and left its process group, gap map F13), with its tree and group. `tag`
    is _tag(home), unique on the machine, so no other launch's process is found. procs has no
    public scan of the tagged processes (procs.py:133), so this reads its private _tagged."""
    killed: set = set()
    for _round in range(3):
        left = procs._tagged(tag)
        if not left:
            break
        for pid in left:
            killed.update(procs.kill_tree(pid, tag=tag))
        time.sleep(0.05)
    return tuple(sorted(killed))


def _check_binary(spec: LaunchSpec, environ) -> tuple:
    """(binary, sha256, pinned): the pinned file hashed again (section 6.1, step 3), or a fresh
    resolve_binary."""
    if spec.binary is not None:
        if not spec.binary_sha256:
            raise LaunchError("a pinned binary needs binary_sha256")
        try:
            st = os.stat(spec.binary)
        except OSError as exc:
            raise LaunchError("pinned binary %s: %s" % (spec.binary, exc))
        if not stat.S_ISREG(st.st_mode):
            raise LaunchError("pinned binary %s is not a regular file" % spec.binary)
        got = _sha256_file(spec.binary)
        if got != spec.binary_sha256:
            raise LaunchError("pinned binary %s hashes to %s, not the pinned %s"
                              % (spec.binary, got, spec.binary_sha256))
        return os.path.realpath(spec.binary), got, True
    binary = resolve_binary(spec.runtime, environ)
    return binary, _sha256_file(binary), False


def _codex_features(spec: LaunchSpec, home: LaunchHome, binary: str, install: str,
                    root: str) -> None:
    """Section 6.3: `codex features list` under the launch's CODEX_HOME, in a jail with no
    network; every feature config.toml turns off must be listed as false."""
    probe = JailSpec(kind="probe", cwd=home.home, ro=(install,),
                     rw=(home.home, home.config))
    log = os.path.join(home.out, "codex-features.txt")
    env = build_env(spec, home)
    result = procs.run(jail.argv(probe) + ["--", binary, "features", "list"], cwd=home.home,
                       env=env, timeout_s=_FEATURES_TIMEOUT_S, log=log,
                       tag=_tag(home), registry=paths.pids_dir(root, spec.run))
    if result.rc != 0:
        raise LaunchError("codex features list exited %s (%s)"
                          % (result.rc, _tail(log, 400).strip()[-200:]))
    state = {}
    for line in _read_text(log).splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1] in ("true", "false"):
            state[parts[0]] = parts[-1] == "true"
    wrong = [name for name in _CODEX_FEATURES_OFF if state.get(name) is not False]
    if wrong:
        raise LaunchError("codex features not off under the launch's CODEX_HOME: %s"
                          % ", ".join(wrong))


# ================================================================ the guard against a headless agent

#: the environment variable that, with TEST_SWITCH_TOKEN as its value, lets a test start a fake
#: agent runtime. Only a test sets it (the `lab` fixture of test_research_launcher.py); a real
#: project root is never under a temp prefix, so no switch makes a real project start one
TEST_SWITCH_ENV = "ALPACA_RESEARCH_TEST_HEADLESS"
TEST_SWITCH_TOKEN = "headless-agent-for-tests-only"


class HeadlessRefused(LaunchError):
    """A headless agent start the engine refuses (reason code RUN-HEADLESS-REFUSED): the research
    engine runs only inside the driving Claude Code or Codex session, which spawns every agent as
    its own subagent (in-session-runtime.md section 11; decision dec-423725d295b8)."""
    code = "RUN-HEADLESS-REFUSED"

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail)


def refuse_headless(spec, conn, root: str) -> None:
    """Raise HeadlessRefused for a spec whose runtime is claude or codex unless both hold: the test
    switch is on, and the project root lies under a temp prefix (the folders workspace_guard
    refuses as a project root). Independent of the switch, it refuses whenever the spec's run
    recorded `runtime_mode: session`. A control spec is a script, not an agent, and is not
    refused. Every refusal is appended to .alpaca/research/headless-refusals.log."""
    spec = _as_spec(spec)
    if spec.runtime not in ("claude", "codex"):
        return
    from alpaca.gates import workspace_guard
    why = []
    if os.environ.get(TEST_SWITCH_ENV) != TEST_SWITCH_TOKEN:
        why.append("the test switch is off")
    else:
        real = os.path.realpath(root)
        if workspace_guard._under_any(real, workspace_guard.default_tmp_prefixes()) is None:
            why.append("the project root %s is not under a temp prefix" % real)
    if spec.run is not None and conn is not None:
        try:
            row = conn.execute("SELECT data FROM events WHERE kind=? AND ref=? ORDER BY id LIMIT 1",
                               (events.RUN_START, spec.run)).fetchone()
            if row is not None and json.loads(row["data"] or "{}").get("runtime_mode") == "session":
                why.append("run %s is a session run" % spec.run)
        except (ValueError, TypeError, AttributeError):
            pass
    if not why:
        return
    detail = "%s: a headless %s agent is refused (%s)" % (HeadlessRefused.code, spec.runtime,
                                                          "; ".join(why))
    try:
        log = os.path.join(paths.research_dir(root), "headless-refusals.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("%s %s key=%s\n" % (util.now_iso(), detail, spec.key))
    except OSError:
        pass
    raise HeadlessRefused(detail)


# ================================================================ launch

def launch(spec, *, conn=None, root: str | None = None) -> dict:
    """One agent process. spec is a LaunchSpec or a mapping of its fields. root defaults to
    alpaca.paths.root(spec["workdir"]) (alpaca/paths.py:19) and conn to db.connect(root); the
    loop runner passes both. Checks the budget (a HOLD returns outcome held and records nothing);
    checks the paths and, when spec.binary is set, the binary pin; records research-launch;
    starts the egress proxy (egress.start); runs the jail through procs.run with the timeout;
    snapshots the transcript; books the spend and records research-launch-end in one
    transaction; stops the proxy. Returns types.to_dict(LaunchResult), whose keys include
    t-089's launch_event, session, rc, timed_out, infra, reason, transcript, transcript_sha256,
    held, home, launch_end, stream and stream_sha256. Keyed on spec["key"]: a recorded
    research-launch-end for the key returns its result without launching (section 6.1).
    The contract that check_paths, jail_spec and egress.allowed_hosts take: when spec["run"] is
    set, launch reads it once with contract.read_frozen(root, spec["run"]) (section 3.5), which
    does not re-hash the frozen files; when spec["run"] is None (a verification outside a run) the
    contract is None, and the three helpers treat None as no contract read paths, no read
    realpaths and no extra egress hosts, so every spec path must pass the per-subject folder rule
    of section 6.1, step 3. A ResearchRefusal from read_frozen (RUN-CONTRACT-CHANGED) is raised to
    the caller, not recorded: the runner stops the run, and no launch event or home is left.

    A launch that cannot be prepared (a path, the binary pin, the home and the operator's
    credentials file, the Codex feature check, the egress proxy) records research-infra class
    start-failed and no research-launch, and returns outcome start-failed. A classified failure
    after the run (start-failed, quota, auth, crash) records research-launch-end with band
    BLOCKED and research-infra in the same transaction; a quota failure's research-infra carries
    wait_until. A collect step that meets a link or transcripts over _TRANSCRIPT_MAX bytes, a
    file in the Codex sessions folder named for the thread that is not a regular file, a stream
    over _STREAM_DISK_MAX bytes, or a Claude or Codex run that ended normally with no transcript,
    records research-infra class lost and no research-launch-end. The proxy is started just before research-launch is
    recorded, so a proxy failure is a preparation failure. A
    dispatch.spawn refusal (the op's spawn budget) records its dispatch-spawn-refused event and
    research-infra class start-failed in the transaction that would have recorded
    research-launch, records no research-launch, and returns outcome start-failed.

    Every process of the launch carries ALPACA_RESEARCH_LAUNCH=_tag(home), recorded as `tag` in
    research-launch; cleanup kills by that tag, which no other launch on the machine shares."""
    spec = _as_spec(spec)
    if spec.runtime not in RUNTIMES:
        raise LaunchError("unknown runtime %r" % (spec.runtime,))
    if root is None:
        root = alpaca_paths.root(spec.workdir)
    own = conn is None
    if own:
        conn = db.connect(root)
    try:
        refuse_headless(spec, conn, root)
        return _launch(spec, conn, root)
    finally:
        if own:
            conn.close()


def _launch(spec: LaunchSpec, conn, root: str) -> dict:
    launch_id = paths.launch_id(spec.key)
    ended = _find_event(conn, events.LAUNCH_END, spec.key)
    if ended is not None:
        return _from_end(spec, ended)
    started = _find_event(conn, events.LAUNCH, spec.key)
    if started is not None:
        lost = _find_event(conn, events.INFRA, spec.key,
                           "AND json_extract(data, '$.class')=?", ("lost",))
        if lost is None:
            _record_infra(conn, spec, "lost", "research-launch %d has no end: found on resume"
                          % started["id"])
        return _result(spec, outcome="lost", infra="lost", launch_event=started["id"],
                       reason="the launch was recorded with no end (lost)")
    check = spend.check(conn, root, spec.run, role=spec.role)
    if getattr(check, "posture", "GO") == "HOLD":
        return _result(spec, held=True, outcome="held", reason=str(check.reason))
    max_usd = spec.max_usd
    if (max_usd is None and spec.run is not None and spec.runtime == "claude"
            and getattr(check, "usd_cap", None) is not None
            and spec.role not in spend.NEVER_HELD_ROLES):   # a verifier reserves no share (t-297)
        max_usd = max(0.0, float(check.usd_cap) - float(check.usd_spent or 0.0))
    environ = os.environ
    home = None
    proxy = None
    sock_dir = None
    try:
        try:
            contract = (contract_mod.read_frozen(root, spec.run) if spec.run is not None
                        else None)
            check_paths(root, spec, contract)
            if jail.binary() is None:
                raise LaunchError("bwrap is not on the PATH: no launch runs outside a jail")
            binary, binary_sha, pinned = _check_binary(spec, environ)
            install = install_dir(spec.runtime, binary, environ)
            home = prepare_home(root, spec.run, spec, environ=environ)
            tag = _tag(home)
            run_spec = dataclasses.replace(spec, binary=binary, binary_sha256=binary_sha,
                                           max_usd=max_usd)
            if spec.runtime == "codex":
                _codex_features(run_spec, home, binary, install, root)
            sock_dir = paths.egress_dir(root, spec.run, launch_id)
            jspec = jail_spec(run_spec, home, contract=contract, install=install,
                              egress_dir=sock_dir)
            argv = build_argv(run_spec, home, binary)
            relay = _relay_argv()
            env = build_env(run_spec, home)
            allow = egress.allowed_hosts(spec.runtime, dict(environ), contract)
            evidence = _evidence_dir(root, spec, launch_id)
            os.makedirs(evidence, exist_ok=True)
            prompt_out = os.path.join(home.out, "prompt.txt")
            with open(prompt_out, "wb") as fh:
                fh.write(str(spec.prompt).encode("utf-8"))
            shutil.copyfile(prompt_out, os.path.join(evidence, "prompt.txt"))
            proxy = egress.start(sock_dir, allow=allow,
                                 log=os.path.join(home.out, "egress.jsonl"),
                                 upstream=environ.get("HTTPS_PROXY") or environ.get("https_proxy"))
        except (LaunchError, ValueError, OSError) as exc:
            _record_infra(conn, spec, "start-failed", str(exc))
            if home is not None:
                _rmtree(home.root)
            return _result(spec, outcome="start-failed", infra="start-failed",
                           reason=str(exc)[:2000])
        # the stdio relay runs bwrap, so no file of out/ is open in the jail (_RELAY)
        full = relay + (jail.argv(jspec)
                        + ["--", "python3", os.path.join(_bin_dir(home), "jail_forward.py"),
                           str(jail.EGRESS_PORT), "%s/egress.sock" % jail.EGRESS_MOUNT, "--"]
                        + argv)
        prompt_sha = _sha256_file(prompt_out)
        now = util.now_iso()
        refused = None
        with db.transaction(conn):
            if spec.worker and spec.op:
                # the spawn first: a refusal (over the op's spawn budget) records its own
                # dispatch-spawn-refused event in this transaction (section 6.1, step 6), and
                # research-infra start-failed with it, and no research-launch or sessions row
                from alpaca.formation import dispatch
                try:
                    dispatch.spawn(conn, spec.op, spec.worker, role=spec.role,
                                   session=spec.session)
                except dispatch.DispatchError as exc:
                    refused = "dispatch.spawn refused: %s" % exc
                    _record_infra(conn, spec, "start-failed", refused, in_txn=True)
            if refused is None:
                launch_event = events.emit(
                    conn, events.LAUNCH, run=spec.run, ref=_ref(spec), op=spec.op,
                    session=spec.session, in_txn=True,
                    data={"key": spec.key, "round": spec.round, "slot": spec.slot,
                          "role": spec.role, "attempt": spec.attempt, "worker": spec.worker,
                          "session": spec.session, "try": spec.try_n, "launch": launch_id,
                          "runtime": spec.runtime,
                          "runtime_version": _runtime_version(spec.runtime, binary),
                          "binary": binary, "binary_sha256": binary_sha, "pinned": pinned,
                          "model": spec.model, "effort": spec.effort, "jail": jail.version(),
                          "cwd": spec.cwd, "read_paths": list(spec.read_paths),
                          "write_paths": list(spec.write_paths),
                          "bash_allow": list(spec.bash_allow),
                          "prompt": os.path.join(evidence, "prompt.txt"),
                          "prompt_sha256": prompt_sha, "env_keys": sorted(env),
                          "egress_allow": list(allow), "timeout_s": spec.timeout_s,
                          "max_usd": max_usd, "home": home.root, "tag": tag})
                db.upsert(conn, "sessions", "sid", {"sid": spec.session, "started": now,
                                                    "cwd": spec.cwd})
        if refused is not None:
            _rmtree(home.root)
            return _result(spec, outcome="start-failed", infra="start-failed",
                           reason=refused[:2000])
        stream_out = os.path.join(home.out, "stream.jsonl")
        started_at = time.monotonic()
        wall_start = time.time() - 0.05          # the file clock may lag by a tick
        run_error = None
        try:
            proc = procs.run(full, cwd=spec.cwd, env=env, timeout_s=spec.timeout_s,
                             log=stream_out, stdin=prompt_out, tag=tag,
                             registry=paths.pids_dir(root, spec.run))
        except OSError as exc:
            proc, run_error = None, str(exc)
        finally:
            proxy.close()
            proxy_refused, proxy_dropped, proxy = proxy.refused, proxy.dropped, None
        swept = _sweep(tag)
        return _collect(conn, root, spec, home, launch_event, proc, run_error, swept,
                        stream_out, evidence, proxy_refused,
                        round(time.monotonic() - started_at, 3),
                        secrets=_secrets(spec.runtime, home, environ), since=wall_start,
                        prompt_sha=prompt_sha, egress_dropped=proxy_dropped)
    finally:
        if proxy is not None:
            proxy.close()
        if sock_dir is not None:
            _rmtree(sock_dir)


def _classify_tail(text: str) -> str:
    """The last _TAIL_BYTES of the stream text for classify_failure, led by the last typed
    event line before it when that part holds none, so a flood of lines that are not JSON after
    the runtime's events never reads as a runtime that printed no event."""
    tail = text[-_TAIL_BYTES:]
    if any("type" in e for e in _events_of(tail)):
        return tail
    for line in reversed(text[:-_TAIL_BYTES].splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "type" in obj:
            return line + "\n" + tail
    return tail


def _collect(conn, root, spec, home, launch_event, proc, run_error, swept, stream_out,
             evidence, refused, duration, *, secrets: tuple = (), since: float = 0.0,
             prompt_sha: str | None = None, egress_dropped: int = 0) -> dict:
    """Section 6.1, steps 8 and 9. The credential values the launch handed the runtime are
    replaced in the stream, the staged transcript, the final message and the files the agent
    changed under its write paths before any of them outlives the home. Best effort: only exact
    copies of a value are replaced, never an encoded one (base64, hex, split, compressed), a
    changed file with over _READ_MAX bytes of data, or a git object; in the write paths each value is
    overwritten in place with a mask of its length, and the walk reads at most
    _REDACT_TREE_MAX bytes in all (_redact_tree). research-launch-end names the changed files
    left unchecked in `not_redacted`. A Claude or Codex run
    that ended normally with no transcript, or whose transcript tree holds a link, is lost:
    research-infra class lost and the sessions row's end in one transaction, and no
    research-launch-end. So is a launch whose evidence prompt.txt no longer hashes to the
    prompt_sha256 research-launch recorded: out/prompt.txt is never copied over it, and one
    whose egress log reached egress._LOG_MAX bytes (`egress_dropped`, the requests the proxy
    left out of it)."""
    from alpaca import transcripts
    redacted = []
    if _redact_file(stream_out, secrets):
        redacted.append("stream.jsonl")
    missed, unwalked = [], []
    for folder in spec.write_paths:
        hits, left, stopped = _redact_tree(folder, secrets, since)
        redacted += [os.path.join(folder, rel) for rel in hits]
        missed += [os.path.join(folder, rel) for rel in left]
        if stopped:
            unwalked.append(folder)
    not_redacted = {"count": len(missed), "paths": missed[:_MISSED_KEEP],
                    "walk_stopped": unwalked}
    stream_text = _read_text(stream_out)
    tail = _classify_tail(stream_text)
    killed = tuple(sorted(set(proc.killed if proc else ()) | set(swept)))
    if proc is None:
        outcome, rc, timed_out = "start-failed", None, False
    elif proc.timed_out:
        outcome, rc, timed_out = "timeout", None, True
    elif proc.stopped:
        outcome, rc, timed_out = "killed", None, False
    else:
        outcome, rc, timed_out = "ok", proc.rc, False
    probe = LaunchResult(session=spec.session, rc=rc, timed_out=timed_out, held=False,
                         launch_id=home.launch_id, key=spec.key, role=spec.role,
                         try_n=spec.try_n, outcome=outcome)
    infra = "start-failed" if proc is None else classify_failure(probe, tail)
    if infra:
        outcome = infra
    reason = run_error or ""
    reset_at = _reset_at(tail) if infra == "quota" else None
    staged, final, broken = None, None, None
    try:
        if os.path.exists(stream_out) and os.path.getsize(stream_out) > _STREAM_DISK_MAX:
            raise _Broken("the stream is over %d bytes: the relay kept only the first ones"
                          % _STREAM_DISK_MAX)
        if egress_dropped:
            raise _Broken("the egress log reached %d bytes: %d requests are not in it"
                          % (egress._LOG_MAX, egress_dropped))
        transcript_src = None
        if spec.runtime == "claude":
            transcript_src = _claude_transcript(home, spec)
        elif spec.runtime == "codex":
            transcript_src = _codex_transcript(home, spec, stream_text)
        if spec.runtime in ("claude", "codex") and transcript_src is None and outcome == "ok":
            raise _Broken("no transcript")
        if transcript_src is not None:
            staged = _stage_transcript(transcript_src, home, secrets)
        final = _final_message(spec.runtime, home, stream_text)
    except (_Broken, OSError) as exc:
        broken = str(exc)
    if final is not None and secrets:
        for value in secrets:
            text = value.decode("utf-8", "replace")
            if text in final:
                final = final.replace(text, _REDACTED.decode("ascii"))
                redacted.append("final message")
    truncated = final is not None and len(final) > _FINAL_MAX
    if truncated:
        _write(os.path.join(home.out, "final-message.txt"), final.encode("utf-8"), 0o644)
    skipped = _copy_out(home.out, evidence, exclude=("prompt.txt",))
    if (broken is None and prompt_sha is not None
            and _sha256_file(os.path.join(evidence, "prompt.txt")) != prompt_sha):
        broken = "the evidence prompt.txt no longer hashes to the recorded prompt_sha256"
    stream = os.path.join(evidence, "stream.jsonl")
    egress_log = os.path.join(evidence, "egress.jsonl")
    if broken is not None:
        detail = "collect refused: %s" % broken
        if redacted:
            detail += "; credential redacted in: %s" % ", ".join(redacted)
        if missed or unwalked:
            detail += "; changed files not checked for the credential: %d%s" % (
                len(missed), " (walk stopped in %s)" % ", ".join(unwalked) if unwalked else "")
        with db.transaction(conn):
            _record_infra(conn, spec, "lost", detail, in_txn=True)
            db.patch(conn, "sessions", "sid", spec.session, {"ended": util.now_iso()})
        _rmtree(home.root)
        return _result(spec, outcome="lost", infra="lost", launch_event=launch_event["id"],
                       reason=detail, home=home.root, stream=stream,
                       stream_sha256=_sha256_file(stream), killed=killed, duration_s=duration)
    transcript, transcript_sha = None, None
    if staged is not None:
        db.patch(conn, "sessions", "sid", spec.session, {"transcript": staged})
        snap = transcripts.snapshot(root, spec.session, conn=conn, event=True)
        transcript = os.path.join(root, snap["path"])
        transcript_sha = snap.get("sha256")
        db.patch(conn, "sessions", "sid", spec.session, {"transcript": transcript})
    used = _usage(root, spec, stream, stream_text, transcript is not None)
    stream_sha = _sha256_file(stream)
    egress_sha = _sha256_file(egress_log) if os.path.exists(egress_log) else None
    band = {"ok": vc.PASS, "timeout": vc.FAIL, "killed": vc.FAIL}.get(outcome, vc.BLOCKED)
    if skipped:
        reason = (reason + "; " if reason else "") + "skipped in out/: " + ", ".join(skipped)
    if infra and not reason:
        reason = "; ".join(_error_texts(_events_of(tail)))[:2000] or infra
    tokens, cost = used["tokens"], used["cost_usd"]
    data = {"key": spec.key, "attempt": spec.attempt, "role": spec.role,
            "session": spec.session, "try": spec.try_n, "launch": home.launch_id,
            "outcome": outcome, "rc": rc, "timed_out": timed_out, "duration_s": duration,
            "stream": stream, "stream_sha256": stream_sha, "transcript": transcript,
            "transcript_sha256": transcript_sha, "egress_log": egress_log if egress_sha else None,
            "egress_sha256": egress_sha, "egress_refused": int(refused), "tokens": tokens,
            "cost_usd": cost, "priced": cost is not None, "usage_sources": used["sources"],
            "killed_pids": list(killed),
            "model": used["model"], "home": home.root,
            "final_message": final[:_FINAL_MAX] if final is not None else None,
            "final_message_sha256": (hashlib.sha256(final.encode("utf-8")).hexdigest()
                                     if final is not None else None),
            "final_message_truncated": truncated, "redacted": redacted,
            "not_redacted": not_redacted,
            "infra": infra, "reason": reason, "reset_at": reset_at,
            "launch_event": launch_event["id"]}
    with db.transaction(conn):
        end = events.emit(conn, events.LAUNCH_END, run=spec.run, ref=_ref(spec), op=spec.op,
                          session=spec.session, code=band, data=data, in_txn=True)
        if infra:
            _record_infra(conn, spec, infra, reason or infra, in_txn=True,
                          wait_until=_wait_until(reset_at, spec.try_n)
                          if infra == "quota" else None)
        spend.book(conn, spec.run, spec.attempt, cost_usd=cost, tokens=tokens,
                   session=spec.session, in_txn=True)
        db.patch(conn, "sessions", "sid", spec.session, {"ended": util.now_iso()})
    _rmtree(home.root)
    return _from_end(spec, {"id": end["id"], "data": data})
