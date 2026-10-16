"""The look of the first chat: read the repository the owner names, before asking anything (op-062 t-656).

`look(root, source, scratch=...)` makes a read-only clone in a folder OUTSIDE the studio, surveys it
with git and file reads, senses its gates, and tries only the gates that run no code from the
repository. It writes nothing under the studio and records nothing; `alpaca/onboard.py` records the
returned dict as the `onboard-look` event. No program the repository wrote is run (probes:
`alpaca/tests/test_onboard_attacks.py`, `test_onboard_look.py`, `test_onboard_nowrite.py`):

  * git runs through `alpaca.attach.gitops` (hooks off, no prompt, no system or global config);
  * a local folder is never entered by git: it is cloned with `--no-local` and its own `.git/config` is
    read as bytes, so a hostile `core.fsmonitor` or `core.hooksPath` is reported and never run;
  * a gate is tried here only when `classify_gate` says it parses and runs nothing the repository
    wrote (the py-syntax check, a compiler in `-fsyntax-only` mode). CMake, make, npm, pytest and
    every script run only after the owner's yes.

Every string that comes from the repository (author names, branch names, file names) is cleaned to
plain ASCII and cut to a short length before it is kept, so the card never carries a control
character or a stray line of someone else's text.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

from alpaca.attach import AttachError, gitops

CLONE_TIMEOUT = 300
TRIAL_TIMEOUT = 60
MAX_FILES = 200000
READ_LIMIT = 200_000
MARKER = ".alpaca-look"

#: files that say how a repository is built, in the order they are listed
BUILD_NAMES = ("CMakeLists.txt", "CMakePresets.json", "Makefile", "package.json", "pyproject.toml", "setup.py",
               "setup.cfg", "pytest.ini", "tox.ini", "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "meson.build")
LANGUAGES = {
    ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++", ".hpp": "C++", ".cxx": "C++", ".py": "Python", ".js": "JavaScript",
    ".mjs": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript", ".jsx": "JavaScript", ".go": "Go", ".rs": "Rust",
    ".java": "Java", ".kt": "Kotlin", ".rb": "Ruby", ".php": "PHP", ".cs": "C#", ".swift": "Swift", ".sh": "Shell",
    ".s": "Assembly", ".lua": "Lua", ".scala": "Scala", ".dart": "Dart", ".vue": "Vue",
}
CI_PATTERNS = (r"^\.github/workflows/[^/]+\.ya?ml$", r"^\.gitlab-ci\.ya?ml$", r"^Jenkinsfile$", r"^\.circleci/config\.yml$",
               r"^azure-pipelines\.ya?ml$", r"^\.travis\.yml$", r"^bitbucket-pipelines\.yml$")
HOOK_PATTERNS = (r"^\.husky/[^/]+$", r"^\.pre-commit-config\.ya?ml$", r"^lefthook\.ya?ml$", r"^\.githooks/[^/]+$")
#: files that show an Alpaca install already lives in the repository (op-062 t-691): the repo answers the footprint question
ALPACA_FILE_PATTERNS = (r"^ALPACA-MANIFEST$", r"^\.alpaca/", r"^\.claude/skills/alpaca-")
AGENT_FILES = ("CLAUDE.md", "AGENTS.md", ".cursorrules", "GEMINI.md", ".github/copilot-instructions.md")
CODEOWNER_FILES = ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS")
CONTRIBUTING_FILES = ("CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md")
TEST_PATTERN = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*\.py$|_test\.(py|go|c|cc|cpp|js|ts)$|\.test\.(js|ts)$")
CROSS_COMPILER = re.compile(r"\b((?:arm|aarch64|riscv\d*|avr|xtensa|mips\w*|msp430|powerpc\w*|sparc\w*)"
                            r"(?:-[a-z0-9_]+){1,3}-(?:gcc|g\+\+|clang))\b")
CROSS_PREFIX = re.compile(r"(?:TOOLCHAIN_PREFIX|CROSS_COMPILE|CROSS_PREFIX)\s*[=( ]\s*[\"']?((?:arm|aarch64|riscv\d*|avr|xtensa|mips\w*|msp430|powerpc\w*)"
                          r"(?:-[a-z0-9_]+){1,3})-")
BRANCH_RULE = re.compile(r"\b([a-z][a-z0-9-]{2,30}/)(?=<|\{|\[|\*|[A-Z]+-\d|\d)")
HOST_TOOLS = ("git", "cmake", "ninja", "make", "gcc", "clang", "python3", "node", "npm", "cargo", "go", "pytest")

#: the gate every Python repository gets when it has no test gate: the files parse (probe gate A)
PY_SYNTAX = ["python3", "-B", "-I", "-c",
             "import ast,os,subprocess;"
             "fs=[f for f in subprocess.check_output(['git','ls-files','-z','*.py'],text=True).split('\\0') if f];"
             "fs=[f for f in fs if os.path.isfile(f) and not os.path.islink(f) and os.path.getsize(f)<=2000000];"
             "[ast.parse(open(f,encoding='utf-8').read(),f) for f in fs];print('parsed',len(fs),'files')"]
_COMPILERS = ("clang", "gcc", "cc", "clang++", "g++", "c++")
#: what a compiler in -fsyntax-only mode may be given (an allow list: anything else is "runs-repo-code")
_PLAIN_FLAGS = ("-fsyntax-only", "-w", "-pedantic", "-pedantic-errors")
_RE_STD = re.compile(r"^-std=[A-Za-z0-9+:.]+$")
_RE_MACRO = re.compile(r"^-[DU][A-Za-z_][A-Za-z0-9_]*(=[A-Za-z0-9_.+-]*)?$")
_RE_WARN = re.compile(r"^-W[a-z0-9+=-]+$")        # no comma: -Wp, -Wa and -Wl pass options to other programs
_RE_RELPATH = re.compile(r"^[A-Za-z0-9_.+/-]+$")
_SOURCE_EXT = (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh")
_SENSITIVE_KEYS = {"fsmonitor": "core.fsmonitor", "hookspath": "core.hooksPath", "sshcommand": "core.sshCommand",
                   "packobjectshook": "uploadpack.packObjectsHook", "pager": "core.pager", "editor": "core.editor"}


class LookError(Exception):
    """A look that stops. `code` is a LOOK-* token; `str(exc)` is one line safe to print."""

    def __init__(self, code, detail):
        super().__init__("%s (%s)" % (code, detail))
        self.code, self.detail = code, detail


# ---------------------------------------------------------------------------------------- cleaning

def clean(text, cap=60):
    """Plain ASCII, one line, at most `cap` characters: text from a repository made safe to keep."""
    out = "".join(c if 32 <= ord(c) < 127 else "?" if ord(c) > 127 else " " for c in str(text))
    out = " ".join(out.split())
    return out if len(out) <= cap else out[:cap - 3].rstrip() + "..."


# ----------------------------------------------------------------------------------------- scratch

def check_scratch(root, scratch) -> str:
    """The real path of `scratch`, refused when it lies inside the studio or is not absolute."""
    if not scratch or not os.path.isabs(str(scratch)):
        raise LookError("LOOK-SCRATCH", "the scratch folder must be an absolute path outside the studio")
    real = os.path.realpath(str(scratch))
    studio = os.path.realpath(str(root))
    if real == studio or real.startswith(studio + os.sep) or studio.startswith(real + os.sep):
        raise LookError("LOOK-SCRATCH-INSIDE", "the scratch folder %s is inside or around the studio; use the session "
                        "scratchpad" % real)
    return real


def clone_dir(scratch):
    return os.path.join(scratch, "clone")


def remove_scratch_clone(scratch) -> bool:
    """Remove the clone this tool made (and only that: the marker proves it). True when something went."""
    path = clone_dir(scratch)
    if os.path.lexists(path) and os.path.exists(os.path.join(scratch, MARKER)) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
        try:
            os.unlink(os.path.join(scratch, MARKER))
        except OSError:
            pass
        try:
            os.rmdir(scratch)
        except OSError:
            pass
        return True
    return False


# ------------------------------------------------------------------------------------ the source

def _owner_config_findings(path):
    """Findings in a local folder's own `.git/config`, read as bytes (git never runs there)."""
    out = []
    cfg = os.path.join(path, ".git", "config")
    try:
        if os.path.islink(cfg) or not os.path.isfile(cfg):
            return out
        with open(cfg, "rb") as fh:
            text = fh.read(1 << 20).decode("utf-8", "replace")
    except OSError:
        return out
    for number, line in enumerate(text.splitlines(), 1):
        m = re.match(r"\s*([A-Za-z][A-Za-z0-9-]*)\s*=\s*(.*)$", line)
        if m and m.group(1).lower() in _SENSITIVE_KEYS:
            out.append({"token": _SENSITIVE_KEYS[m.group(1).lower()], "file": ".git/config", "line": number,
                        "detail": "%s is set in the folder's own git config; I never run git inside it" % _SENSITIVE_KEYS[m.group(1).lower()]})
    return out


def _local_findings(path):
    from alpaca.gates import git_containment
    findings = [{"token": t, "file": clean(os.path.relpath(f, path), 80) if f else "", "line": ln, "detail": clean(d, 120)}
                for (t, f, ln, d) in git_containment.scan(path)]
    return findings + _owner_config_findings(path)


def _owner_origin(path):
    """The `origin` url of a local folder, read from its `.git/config` as bytes; None when absent."""
    cfg = os.path.join(path, ".git", "config")
    try:
        if os.path.islink(cfg) or not os.path.isfile(cfg):
            return None
        with open(cfg, "rb") as fh:
            text = fh.read(1 << 20).decode("utf-8", "replace")
    except OSError:
        return None
    m = re.search(r'\[remote "origin"\][^\[]*?\burl\s*=\s*(\S+)', text, re.S)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------------------- survey

def _git(clone, *args, timeout=60):
    return gitops.git(clone, *args, timeout=timeout)


def _days(now, then):
    try:
        a = datetime.date.fromisoformat(now)
        b = datetime.date.fromisoformat(then)
    except ValueError:
        return None
    return max(0, (a - b).days)


def _files(clone):
    out = gitops.git(clone, "ls-files", "-z", binary=True)
    names = [n.decode("utf-8", "replace") for n in out.split(b"\0") if n]
    return names[:MAX_FILES]


def _depth_key(path):
    """Order for example files: sources before headers, real files before __init__.py, near the root first."""
    name = os.path.basename(path)
    return (name.endswith((".h", ".hpp")), name == "__init__.py", path.count("/"), path)


def _read_small(clone, rel):
    """Text of a regular file inside the clone (no link), or ''."""
    path = os.path.join(clone, rel)
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return ""
        with open(path, "rb") as fh:
            return fh.read(READ_LIMIT).decode("utf-8", "replace")
    except OSError:
        return ""


def survey(clone, base, *, now) -> dict:
    """Facts of the clone: files, languages, build files, history and surroundings. Reads only."""
    files = _files(clone)
    vendor = []
    for d in ("Drivers", "vendor", "third_party", "third-party", "external", "extern", "node_modules"):
        n = sum(1 for f in files if f.startswith(d + "/"))
        if n:
            vendor.append({"dir": d, "files": n})
    vendor_prefix = tuple(v["dir"] + "/" for v in vendor)
    own_list = [f for f in files if not f.startswith(vendor_prefix)] if vendor_prefix else files
    langs = {}
    for f in own_list:
        name = LANGUAGES.get(os.path.splitext(f)[1].lower())
        if name:
            langs.setdefault(name, []).append(f)
    languages = [{"name": n, "files": len(v), "examples": [clean(x, 80) for x in sorted(v, key=_depth_key)[:2]]}
                 for n, v in sorted(langs.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:3]]
    tracked = {"files": len(files), "vendor": vendor, "own": len(own_list), "languages": languages,
               "python_files": len(langs.get("Python", []))}
    builds = [f for f in files if os.path.basename(f) in BUILD_NAMES or (f.endswith(".cmake") and f.count("/") <= 2)]
    builds = [clean(b, 80) for b in sorted(builds, key=lambda b: (b.count("/"), BUILD_NAMES.index(os.path.basename(b))
                                                                    if os.path.basename(b) in BUILD_NAMES else 99, b))[:8]]
    fileset = set(files)

    def match(patterns):
        pats = [re.compile(p) for p in patterns]
        return [clean(f, 80) for f in files if any(p.search(f) for p in pats)][:5]

    contributing = next((c for c in CONTRIBUTING_FILES if c in fileset), None)
    prefix, prefix_at = None, None
    if contributing:
        for number, line in enumerate(_read_small(clone, contributing).splitlines(), 1):
            if "branch" in line.lower():
                m = BRANCH_RULE.search(line)
                if m:
                    prefix, prefix_at = m.group(1), "repo:%s:%d" % (contributing, number)
                    break
    around = {"ci": match(CI_PATTERNS), "hooks": match(HOOK_PATTERNS),
              "agent_files": [c for c in AGENT_FILES if c in fileset],
              "alpaca_files": match(ALPACA_FILE_PATTERNS),
              "codeowners": [c for c in CODEOWNER_FILES if c in fileset],
              "contributing": contributing, "contributing_branch_prefix": prefix, "contributing_branch_rule_at": prefix_at,
              "gitmodules": ".gitmodules" in fileset,
              "tests": [clean(f, 80) for f in own_list if TEST_PATTERN.search(f)][:5]}
    # ---- history on the base branch, then every branch within the recent window
    # the date comes first and the separator is a control character, so a name with a tab or a colon in it
    # cannot shift a field
    log = _git(clone, "log", base, "--format=%ad%x1f%an", "--date=short")
    rows = [r.split("\x1f", 1) for r in log.splitlines() if "\x1f" in r]
    counts = {}
    for _date, name in rows:
        counts[clean(name, 40)] = counts.get(clean(name, 40), 0) + 1
    authors = [{"name": n, "commits": c} for n, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    last = rows[0][0] if rows else None
    first = rows[-1][0] if rows else None
    since = (datetime.date.fromisoformat(now) - datetime.timedelta(days=90)).isoformat()
    recent = {}
    for line in _git(clone, "log", "--all", "--since=%s" % since, "--format=%ad%x1f%an", "--date=short").splitlines():
        if "\x1f" in line:
            date, name = line.split("\x1f", 1)
            name = clean(name, 40)
            recent[name] = max(recent.get(name, ""), date)
    recent_authors = [{"name": n, "last": d} for n, d in sorted(recent.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)]
    tips = []
    refs = _git(clone, "for-each-ref", "--sort=-committerdate",
                "--format=%(committerdate:short)%x1f%(refname:short)%x1f%(authorname)", "refs/remotes")
    for line in refs.splitlines():
        parts = line.split("\x1f", 2)
        if len(parts) != 3:
            continue
        date, ref, author = parts
        if ref.endswith("/HEAD") or ref == "origin" or ref.endswith("/" + base):
            continue
        tips.append({"ref": clean(ref, 60), "author": clean(author, 40), "date": date})
        if len(tips) == 5:
            break
    history = {"base": clean(base, 60), "commits": len(rows), "first": first, "last": last,
               "days_still": _days(now, last) if last else None, "authors": authors,
               "recent_authors": recent_authors, "tips": tips}
    return {"tracked": tracked, "build_files": builds, "history": history, "around": around, "files": files}


def host_tools(names) -> dict:
    return {n: shutil.which(n) for n in names}


def _host(clone, builds, files):
    wanted = []
    for rel in builds:
        text = _read_small(clone, rel)
        for tool in CROSS_COMPILER.findall(text) + [p + "-gcc" for p in CROSS_PREFIX.findall(text)]:
            if tool not in wanted:
                wanted.append(tool)
    tools = host_tools(list(HOST_TOOLS) + wanted)
    # the owner's own name from their own git configuration: the clean environment (no GIT_DIR and the like) without
    # the two variables that switch that configuration off; the folder is the scratch folder, not the repository
    env = gitops.clean_env()
    for name in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"):
        env.pop(name, None)
    try:
        user = subprocess.run(["git", "config", "user.name"], cwd=os.path.dirname(clone), capture_output=True, text=True,
                              timeout=10, stdin=subprocess.DEVNULL, env=env).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        user = ""
    return {"tools": {k: ("found" if v else None) for k, v in tools.items()}, "wanted": wanted[:3],
            "git_user": clean(user, 60) or None}


# -------------------------------------------------------------------------------------- the gates

def _inside(path, folder):
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def _safe_path(clone=None, root=None):
    """The PATH with only the absolute folders that lie outside the clone and the studio (an empty or relative
    entry would name the clone itself, where a trial runs)."""
    keep = [f for f in os.environ.get("PATH", "").split(os.pathsep)
            if f and os.path.isabs(f) and not any(x and _inside(f, x) for x in (clone, root))]
    return os.pathsep.join(keep)


def resolve_tool(name, clone=None, root=None):
    """The absolute path of the program `name` (a bare name) on the PATH, looking only at absolute folders that lie
    outside the clone and the studio, or None. A name with a slash is never resolved: it would name a file of the
    repository."""
    if not name or os.sep in name or name.startswith("-") or name in (".", ".."):
        return None
    for folder in _safe_path(clone, root).split(os.pathsep):
        if not folder:
            continue
        cand = os.path.join(folder, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            if any(x and _inside(cand, x) for x in (clone, root)):
                continue
            return cand
    return None


def _repo_path_ok(rel, clone, *, want_file):
    """A path a compiler may be given: relative, plain characters, no `..`, inside the clone, and for a file a regular
    file that is not a link."""
    if not rel or rel.startswith(("/", "-", "@", "~")) or not _RE_RELPATH.match(rel) or ".." in rel.split("/"):
        return False
    if clone is None:
        return True
    full = os.path.join(clone, rel)
    if not _inside(full, clone):
        return False
    if want_file:
        return os.path.isfile(full) and not os.path.islink(full)
    return True


def _compiler_parse_only(argv, clone):
    """True when `argv` is a bare compiler name and arguments from the allow list: -fsyntax-only, -std=, -I, -D, -U, a
    -W word without a comma, -w, -pedantic, -x c or c++, and source files of the clone. Nothing that names a plug-in,
    a specs or config file, an output or dependency file, a linker or an @file is on the list."""
    args = argv[1:]
    sources = 0
    i = 0
    while i < len(args):
        a = args[i]
        if a in _PLAIN_FLAGS or _RE_STD.match(a) or _RE_MACRO.match(a) or _RE_WARN.match(a):
            pass
        elif a in ("-xc", "-xc++"):
            pass
        elif a == "-x" and i + 1 < len(args) and args[i + 1] in ("c", "c++"):
            i += 1
        elif a == "-I" and i + 1 < len(args) and _repo_path_ok(args[i + 1], clone, want_file=False):
            i += 1
        elif a.startswith("-I") and len(a) > 2 and _repo_path_ok(a[2:], clone, want_file=False):
            pass
        elif not a.startswith("-") and a.endswith(_SOURCE_EXT) and _repo_path_ok(a, clone, want_file=True):
            sources += 1
        else:
            return False
        i += 1
    return sources > 0 and "-fsyntax-only" in args


def classify_gate(argv, clone=None, root=None) -> str:
    """"parse" when running `argv` executes nothing the repository wrote, else "runs-repo-code". An allow list: the
    built-in py-syntax check, `git diff --check`, and a compiler named by a bare program name with only the
    arguments of `_compiler_parse_only`. When `clone` and `root` are given the program must also resolve to a file
    outside both and the paths must lie in the clone."""
    argv = [str(a) for a in (argv or [])]
    if argv == PY_SYNTAX or argv == ["git", "diff", "--check"]:
        return "parse" if resolve_tool(argv[0], clone, root) else "runs-repo-code"
    if argv and argv[0] in _COMPILERS and _compiler_parse_only(argv, clone):
        return "parse" if resolve_tool(argv[0], clone, root) else "runs-repo-code"
    return "runs-repo-code"


def _limits():
    """Set in the child before a trial runs: memory, processor time and written file size are capped, and the kernel stops
    a trial that passes a cap. The allow list is the first guard; this is the second."""
    import resource
    for which, value in ((resource.RLIMIT_AS, 2 << 30), (resource.RLIMIT_CPU, TRIAL_TIMEOUT + 10),
                         (resource.RLIMIT_FSIZE, 0), (resource.RLIMIT_CORE, 0)):
        try:
            resource.setrlimit(which, (value, value))
        except (ValueError, OSError):
            pass


def trial_env(home, clone=None, root=None) -> dict:
    """The environment of a trial, built from a fixed list and not by taking the caller's environment and removing
    names: a variable nobody thought of (COMPILER_PATH, GCC_EXEC_PREFIX, LD_PRELOAD, PYTHONPATH, GIT_EXEC_PATH and the
    like) would reach a program that runs before the owner's yes. Only these names are set: PATH (absolute folders
    outside the clone and the studio), LC_ALL, LANG, HOME and TMPDIR (an empty folder made for this run, outside the
    clone) and the git switches that keep git from reading any configuration."""
    return {"PATH": _safe_path(clone, root), "LC_ALL": "C", "LANG": "C", "HOME": home, "TMPDIR": home,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}


def trial(clone, gate, *, timeout=TRIAL_TIMEOUT, root=None) -> dict:
    """Run one inert gate in the clone: {"exit", "seconds", "tail"}. A timeout reports exit -1. The program is
    resolved here again (outside the clone and the studio) and the run has memory, time and file-size limits; a gate
    that is not inert is not run (exit 126)."""
    start = time.monotonic()
    argv = [str(a) for a in gate["command"]]
    if classify_gate(argv, clone, root) != "parse":
        return {"exit": 126, "seconds": 0.0, "tail": "not run: this command is not one that runs nothing of the repository"}
    argv[0] = resolve_tool(argv[0], clone, root)
    home = tempfile.mkdtemp(prefix="trial-home-", dir=os.path.dirname(os.path.abspath(clone)))
    env = trial_env(home, clone, root)
    try:
        done = subprocess.run(argv, cwd=clone, env=env, capture_output=True, text=True, errors="replace",
                              timeout=timeout, stdin=subprocess.DEVNULL, preexec_fn=_limits, start_new_session=True)
        code, tail = done.returncode, (done.stdout + done.stderr)
    except subprocess.TimeoutExpired:
        code, tail = -1, "ran out of time after %ds" % timeout
    except OSError as exc:
        code, tail = 127, str(exc)
    finally:
        shutil.rmtree(home, ignore_errors=True)
    seconds = round(time.monotonic() - start, 2)
    last = [x for x in tail.splitlines() if x.strip()]
    return {"exit": code, "seconds": seconds, "tail": clean(last[-1] if last else "", 120)}


def _kind_of(name):
    return {"build": "build", "test": "test", "lint": "lint", "cmake-configure": "configure",
            "py-syntax": "parse"}.get(name, "check")


def sense_gates(clone, survey_data, *, trial_timeout=TRIAL_TIMEOUT, root=None) -> list:
    """The gates of the clone (t-647's sensing, plus py-syntax), classified and the inert ones tried."""
    from alpaca.attach import sense
    try:
        gates, traces = sense.sense_gates(clone, "repo")
    except AttachError:
        gates, traces = [], {}
    out = []
    for g in gates:
        trace = traces.get(g["name"], "")
        out.append({"name": g["name"], "command": list(g["command"]), "timeout": g.get("timeout", 600),
                    "kind": _kind_of(g["name"]), "evidence": "repo:" + trace.split("/", 1)[1] if "/" in trace else "repo:" + trace})
    pyfiles = [f for f in survey_data["files"] if f.endswith(".py") and not f.startswith("Drivers/")]
    if pyfiles and not any(g["kind"] == "test" for g in out):
        out.insert(0, {"name": "py-syntax", "command": list(PY_SYNTAX), "timeout": 120, "kind": "parse",
                       "evidence": "repo:" + clean(sorted(pyfiles, key=_depth_key)[0], 80)})
    for g in out:
        g["classify"] = classify_gate(g["command"], clone, root)
        g["trial"] = trial(clone, g, timeout=trial_timeout, root=root) if g["classify"] == "parse" else None
    return out


# ------------------------------------------------------------------------------------- sources

def sources(payload) -> list:
    """The fixed sources [1] to [6] of the card, from the look (design 5.1)."""
    tracked, hist, around = payload.get("tracked") or {}, payload.get("history") or {}, payload.get("around") or {}
    builds = payload.get("build_files") or []
    ex = []
    for lang in tracked.get("languages") or []:
        ex.extend(lang.get("examples") or [])
    vend = (tracked.get("vendor") or [{}])[0].get("dir")
    count = "cmd: git ls-files | wc -l = %s" % tracked.get("files")
    if vend:
        count += "; git ls-files %s | wc -l = %s" % (vend, tracked["vendor"][0]["files"])
    found = [x for key in ("ci", "hooks", "codeowners", "agent_files", "alpaca_files", "tests") for x in (around.get(key) or [])]
    if around.get("contributing"):
        found.append(around["contributing"])
    scan = "look scan of CI, hook, test, CODEOWNERS, CONTRIBUTING and agent files: " + (
        "found " + ", ".join(found[:8]) if found else "none found")
    return [
        {"n": 1, "text": (", ".join("repo:" + b for b in builds[:5]) + (" and %d more" % (len(builds) - 5) if len(builds) > 5 else ""))
         if builds else "none found (looked for CMakeLists.txt, "
                                                                                   "Makefile, package.json, pyproject.toml, Cargo.toml, go.mod)"},
        {"n": 2, "text": "cmd: git ls-files, grouped by file extension" + ("; examples " + ", ".join("repo:" + e for e in ex[:4]) if ex else "")},
        {"n": 3, "text": count},
        {"n": 4, "text": "cmd: git log %s --format=%%an%%x09%%ad --date=short (%d authors, %d commits)"
                         % (hist.get("base"), len(hist.get("authors") or []), hist.get("commits") or 0)},
        {"n": 5, "text": "cmd: git for-each-ref --sort=-committerdate refs/remotes"},
        {"n": 6, "text": scan},
    ]


def digest(payload) -> str:
    body = {k: v for k, v in payload.items() if k != "digest"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------------------ look

def _auth_failure(text):
    low = text.lower()
    return any(k in low for k in ("authentication failed", "could not read username", "permission denied", "terminal prompts disabled",
                                  "repository not found", "403", "401", "access denied"))


def look(root, source, *, scratch, now, clone_timeout=CLONE_TIMEOUT, trial_timeout=TRIAL_TIMEOUT) -> dict:
    """Clone `source` read-only into `scratch`/clone and survey it. Raises LookError. Writes nothing
    under the studio `root`."""
    scratch = check_scratch(root, scratch)
    if not isinstance(source, str) or not source.strip():
        raise LookError("LOOK-SOURCE", "name a git URL or a folder path")
    try:
        display = gitops.validate_source(source.strip(), root)
    except AttachError as exc:
        raise LookError("LOOK-SOURCE", str(exc)) from None
    kind = "path" if os.path.isdir(display) else "url"
    findings, upstream = [], display
    if kind == "path":
        findings = _local_findings(display)
        origin = _owner_origin(display)
        if origin and not gitops.has_userinfo(origin) and gitops.split_source(origin)[0] != "path":
            upstream = origin
    dest = clone_dir(scratch)
    os.makedirs(scratch, exist_ok=True)
    marker = os.path.join(scratch, MARKER)
    if os.path.lexists(dest):
        if not os.path.exists(marker):
            raise LookError("LOOK-SCRATCH-USED", "%s already exists and was not made by a look; use an empty scratch folder" % dest)
        shutil.rmtree(dest, ignore_errors=True)
    try:
        gitops.remote(scratch, "clone", "--no-local", "--template=", "--origin", "origin", "--", display, dest,
                      url=display, timeout=clone_timeout)
    except AttachError as exc:
        shutil.rmtree(dest, ignore_errors=True)
        if _auth_failure(str(exc)):
            raise LookError("LOOK-AUTH", "git could not read %s without a login; run `gh auth login` in your own terminal "
                            "or give a local folder (never paste a token here)" % gitops.scrub(display)) from None
        raise LookError("LOOK-CLONE", str(exc)) from None
    with open(marker, "w", encoding="utf-8") as fh:
        fh.write("made by alpaca onboard look; the clone beside it may be deleted\n")
    if kind == "path":
        # a clone of a local folder carries its branches but not the remote-tracking branches that folder
        # holds (the work other people pushed); fetch those too, as refs of their own, so the history is whole
        gitops.git(dest, "fetch", "--no-tags", "-q", "origin", "+refs/remotes/*:refs/remotes/folder/*", check=False,
                   timeout=clone_timeout)
    head = gitops.git(dest, "rev-parse", "--verify", "-q", "HEAD", check=False)
    if not head:
        raise LookError("LOOK-EMPTY", "the repository has no commits, so there is nothing to read")
    branch = gitops.git(dest, "symbolic-ref", "-q", "--short", "HEAD", check=False) or "HEAD"
    if kind == "path":
        # the folder may be on a feature branch; the base of the work is the branch its origin calls HEAD
        pointed = gitops.git(dest, "symbolic-ref", "-q", "refs/remotes/folder/origin/HEAD", check=False)
        wanted = pointed[len("refs/remotes/folder/origin/"):] if pointed.startswith("refs/remotes/folder/origin/") else ""
        if wanted and wanted != branch and gitops.git(dest, "rev-parse", "--verify", "-q",
                                                       "refs/remotes/origin/%s" % wanted, check=False):
            gitops.git(dest, "checkout", "-q", "-B", wanted, "refs/remotes/origin/%s" % wanted, check=False)
            branch = gitops.git(dest, "symbolic-ref", "-q", "--short", "HEAD", check=False) or branch
    data = survey(dest, branch, now=now)
    meta = gitops.git(dest, "log", "-1", "--format=%cd%x09%s", "--date=short")
    date, _, subject = meta.partition("\t")
    gates = sense_gates(dest, data, trial_timeout=trial_timeout, root=root)
    payload = {
        "schema": 1, "now": now, "source": gitops.scrub(display), "source_kind": kind,
        "upstream": gitops.scrub(upstream), "repository": gitops.repository_of(upstream),
        "scratch": scratch, "clone": dest,
        "head": {"branch": clean(branch, 60), "sha": head, "date": date, "subject": clean(subject, 80)},
        "git_safe": not findings, "git_findings": findings,
        "tracked": data["tracked"], "build_files": data["build_files"], "history": data["history"],
        "around": data["around"], "host": _host(dest, data["build_files"], data["files"]), "gates": gates,
    }
    payload["sources"] = sources(payload)
    payload["digest"] = digest(payload)
    return payload
