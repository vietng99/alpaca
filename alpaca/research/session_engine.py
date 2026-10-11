"""The engine's own files and the files an agent loads as context, for the session runtime
(design/research-engine/in-session-runtime.md sections 12.5 and 12.6).

With no jail an attempt agent runs with the session's rights, so it could write code that an
engine process loads later (the engine files, a hook script, a module planted on an import path,
a startup file) or text that a later agent receives as context (a settings file, a CLAUDE.md, the
auto-memory index, a hook data file). This module closes both classes with the same two tools:

  * the PIN: every file that decides or witnesses (the static import closure of the engine, its
    non-Python files, the hook scripts the settings name, the importable files of every import
    root) is recorded with its size, inode, mtime, ctime and sha256; any difference, a restored
    file included (its ctime has moved), is a change that ends the run, save a metadata-only
    change of a text source (a new uid, gid or mode with the same bytes, size, inode and mtime;
    section 24.1), which is noted and ends nothing;
  * the LEDGER: the files under a fixed list of roots that the runtime loads into an agent and
    that the driver session itself writes (the auto-memory folder first among them) are
    fingerprinted at the hand of each job and compared at its judge (a metadata-only change is
    noted, not a change, as for the pin), and a change is put in a writer class: `driver` (the session's own save: the job loses a try), `agent` or
    `unattributed` (the run ends).

It reads files and writes only the pin file and the ledger file it is asked for. It imports none
of the deciding modules (accept, gates, fingerprint, proofs).

One function loads modules by name, `preload`, because the runner has to import the whole static
closure of its deciding code before the first job; that is the only dynamic import in the engine
(the design's test 17 refuses any other).
"""
from __future__ import annotations

import ast
import compileall
import fnmatch
import glob
import hashlib
import importlib
import json
import os
import re
import shlex
import shutil
import stat
import sys
import time

from alpaca import util
from alpaca.research import pathscan

PIN_SCHEMA = "alpaca-engine-pin/1"
LEDGER_SCHEMA = "alpaca-context-ledger/1"

#: the hub web server and what only it reaches (12.5, item 1): not pinned, not preloaded, but
#: named by control_paths, so the guard still denies a write to them
ENGINE_CUT = ("alpaca.serve", "alpaca.weblogin", "alpaca.hub", "alpaca.hub_checklist",
              "alpaca.hub_publish")
#: modules the design names besides the research package and the hook modules (12.5, item 1)
NAMED_MODULES = ("alpaca.db", "alpaca.proof", "alpaca.pool", "alpaca.transcripts",
                 "alpaca.observability", "alpaca.analytics.metrics", "alpaca.formation.controller",
                 "alpaca.formation.shield")
#: the hook modules the research engine adds
NEW_HOOKS = ("alpaca.hooks.subagent_start", "alpaca.hooks.research_guard")
#: the extensions Python imports from a folder
_IMPORTABLE = (".py", ".pyc", ".so", ".pyd")
_PIN_EXT = _IMPORTABLE + (".pth",)
#: the hook data files that change on every call and carry no text (12.5, item 5)
TICK_PREFIX = ".autodrive-tick"
#: a regex for a dot file of the config dir that a hook script reads
_DOT_REF = re.compile(r"(?:\$CLAUDE_DIR|\$CONF|\$\{CLAUDE_CONFIG_DIR:-\$HOME/\.claude\}|"
                      r"\$\{CLAUDE_CONFIG_DIR\}|\$CLAUDE_CONFIG_DIR)/(\.[A-Za-z0-9_.-]+)")
#: whether `start` byte-compiles the venv's site-packages (the tests turn it off)
COMPILE_SITE_PACKAGES = True
#: whether preload() blocks the names the host lacks (the tests that share a process turn it off)
PRELOAD_BLOCK = True
#: the most paths one report of a change lists
MAX_CHANGED = 50
#: the most files a text-source folder lists
_MAX_FOLDER_FILES = 4096


# ================================================================ small helpers
def install_root() -> str:
    """The folder that holds the `alpaca` package this process runs: the project root in a real
    project, and the checkout in a test."""
    import alpaca
    return os.path.dirname(os.path.dirname(os.path.realpath(alpaca.__file__)))


def sha256_file(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stat_tuple(path: str) -> dict | None:
    """size, inode, mtime_ns and ctime_ns of a regular file, with its uid, gid and permission bits
    (`mode`, the low 12 bits), or None. The stat key reads the first four; the last three let a
    compare tell a permission change from a pure ctime move (t-360, section 24.1)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return {"size": st.st_size, "ino": st.st_ino, "mtime_ns": st.st_mtime_ns,
            "ctime_ns": st.st_ctime_ns, "uid": st.st_uid, "gid": st.st_gid,
            "mode": stat.S_IMODE(st.st_mode)}


def fingerprint_file(path: str, previous: dict | None = None, *, digest: bool = True) -> dict:
    """The six figures of a file; the sha256 is taken when the stat tuple differs from
    `previous` (a record of the same file), else carried over. With digest False only the stat
    pass is made (the check at every point of a run), and a changed file says so by its stat."""
    got = stat_tuple(path)
    if got is None:
        return {"present": False}
    if previous and previous.get("present") and all(previous.get(k) == got[k] for k in got) \
            and previous.get("sha256"):
        return dict(got, present=True, sha256=previous["sha256"])
    return dict(got, present=True, sha256=sha256_file(path) if digest else None)


def _stat_key(rec: dict) -> tuple:
    return (rec.get("present", False), rec.get("size"), rec.get("ino"), rec.get("mtime_ns"),
            rec.get("ctime_ns"))


#: the stat fields a permission repair changes (t-360, orchestrator ruling dec-464755c60ab4,
#: section 24.1)
PERMISSION_FIELDS = ("uid", "gid", "mode")


def _rights(rec: dict, uid: int, gids: set) -> int:
    """The rwx bits (4 read, 2 write, 1 execute) that `uid`, a member of `gids`, has on a file with
    the record's uid, gid and mode, as the kernel's mode check reads them (root reads all, and runs
    a file when any execute bit is set). ACLs are not read: an ACL change moves only the ctime and
    stays a change."""
    mode = rec["mode"]
    if uid == 0:
        return 4 | (1 if mode & 0o111 else 0)
    if rec["uid"] == uid:
        return (mode >> 6) & 7
    if rec["gid"] in gids:
        return (mode >> 3) & 7
    return mode & 7


def metadata_only(ra: dict, rb: dict, path: str) -> bool:
    """True when two records of one member file differ by a permission change alone (t-360,
    orchestrator ruling dec-464755c60ab4, section 24.1): both present; the same sha256 (both
    known), size, inode and mtime_ns; a different uid, gid or mode (each known on both sides);
    the file readable by this process's uid now (the new record's digest was read now, since a
    record whose uid, gid or mode moved is hashed again, and `os.access` says so); and the same
    execute right and set-id and sticky bits for this process's uid, so a change that lets the
    runtime run a file it could not run before is still a change. A ctime move with nothing else
    changed is no metadata-only change: an edit undone with the mtime set back moves only the
    ctime (12.5, R8), so it still ends the run."""
    if not (ra.get("present") and rb.get("present")):
        return False
    if any(ra.get(k) is None or ra.get(k) != rb.get(k) for k in ("sha256", "size", "ino",
                                                                 "mtime_ns")):
        return False
    if any(ra.get(k) is None or rb.get(k) is None for k in PERMISSION_FIELDS):
        return False                      # a record taken before t-360 round 4: no field to read
    if all(ra[k] == rb[k] for k in PERMISSION_FIELDS):
        return False                      # the ctime alone moved
    if (ra["mode"] & 0o7000) != (rb["mode"] & 0o7000):
        return False
    uid = os.getuid()
    gids = set(os.getgroups()) | {os.getgid()}
    if (_rights(ra, uid, gids) & 1) != (_rights(rb, uid, gids) & 1):
        return False
    if not _rights(rb, uid, gids) & 4:
        return False
    try:
        return os.access(path, os.R_OK)
    except (OSError, ValueError):
        return False


def _metadata_entry(path: str, ra: dict, rb: dict) -> dict:
    """The note of a metadata-only change: the path, the fields that moved, the old and new
    uid, gid and mode, and the unchanged sha256 (section 24.1)."""
    return {"path": path, "change": "metadata", "moved": _moved(ra, rb),
            "old": {k: ra.get(k) for k in PERMISSION_FIELDS},
            "new": {k: rb.get(k) for k in PERMISSION_FIELDS}, "sha256": rb.get("sha256")}


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def home() -> str:
    return os.path.realpath(os.path.expanduser("~"))


def config_dir() -> str:
    """$CLAUDE_CONFIG_DIR or ~/.claude, resolved once (a pooled account sets the variable)."""
    return _real(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(home(), ".claude"))


def _list_names(folder: str) -> list | None:
    try:
        return sorted(n for n in os.listdir(folder)
                      if n != "__pycache__" and not n.startswith(TICK_PREFIX))
    except OSError:
        return None


def _write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def read_json(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# ================================================================ the static import closure
def _module_file(root: str, name: str) -> tuple:
    """(file, is_package) of a dotted `alpaca.` module under root, or (None, False)."""
    rel = name.replace(".", os.sep)
    plain = os.path.join(root, rel + ".py")
    pkg = os.path.join(root, rel, "__init__.py")
    if os.path.isfile(plain):
        return plain, False
    if os.path.isfile(pkg):
        return pkg, True
    return None, False


def _imports_of(path: str, package: str) -> tuple:
    """(alpaca module names the file imports at any depth, other top-level names) from its
    source. A relative import is resolved against `package`, the file's own package."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            tree = ast.parse(fh.read(), filename=path)
    except (OSError, SyntaxError, ValueError):
        return set(), set()
    mods, ext = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                if top == "alpaca":
                    parts = a.name.split(".")
                    for i in range(1, len(parts) + 1):
                        mods.add(".".join(parts[:i]))
                else:
                    ext.add(top)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                pkg_parts = package.split(".") if package else []
                keep = len(pkg_parts) - (node.level - 1)
                if keep < 0:
                    continue
                base = ".".join(pkg_parts[:keep] + ([base] if base else []))
            top = base.split(".")[0] if base else ""
            if top == "alpaca" or (node.level and base):
                parts = base.split(".")
                for i in range(1, len(parts) + 1):
                    mods.add(".".join(parts[:i]))
                for a in node.names:
                    if a.name != "*":
                        mods.add("%s.%s" % (base, a.name))
            elif node.level and not base:
                for a in node.names:
                    if a.name != "*":
                        pkg = ".".join(package.split(".")[:len(package.split(".")) - (node.level - 1)])
                        mods.add("%s.%s" % (pkg, a.name) if pkg else a.name)
            elif top:
                ext.add(top)
    return mods, ext


def research_modules(root: str) -> list:
    out = []
    base = os.path.join(root, "alpaca", "research")
    for folder, dirs, names in os.walk(base):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for n in sorted(names):
            if n.endswith(".py"):
                rel = os.path.relpath(os.path.join(folder, n), root)
                mod = rel[:-3].replace(os.sep, ".")
                out.append(mod[:-9] if mod.endswith(".__init__") else mod)
    return sorted(out)


def hook_modules(root: str) -> list:
    """The hook modules a command of .claude/settings.json names with -m, plus the new ones."""
    names = set(NEW_HOOKS)
    data = read_json(os.path.join(root, ".claude", "settings.json")) or {}
    for entries in (data.get("hooks") or {}).values():
        for grp in entries or []:
            for h in (grp or {}).get("hooks") or []:
                m = re.search(r"-m\s+(alpaca\.hooks\.[A-Za-z0-9_]+)", str(h.get("command") or ""))
                if m:
                    names.add(m.group(1))
    return sorted(names)


def import_closure(root: str, roots: list | None = None) -> dict:
    """The static import closure of the engine, taken from the source and not from sys.modules,
    because the deciding code imports modules inside functions and such a module is in
    sys.modules only once the function has run (12.5, item 1). Returns {"files" (relative paths,
    sorted), "modules" (dotted names), "external" (the top-level non-alpaca names the files
    import)}. The walk parses each file with ast.parse, takes every Import and ImportFrom at any
    depth, and follows no edge into ENGINE_CUT."""
    root = os.path.realpath(root)
    todo = list(roots if roots is not None else
                (research_modules(root) + hook_modules(root) + list(NAMED_MODULES)))
    seen, files, external = set(), set(), set()
    while todo:
        name = todo.pop()
        if name in seen or not name.startswith("alpaca"):
            continue
        if any(name == c or name.startswith(c + ".") for c in ENGINE_CUT):
            continue
        seen.add(name)
        path, is_pkg = _module_file(root, name)
        if path is None:
            continue
        files.add(os.path.relpath(path, root))
        parts = name.split(".")
        for i in range(1, len(parts)):
            init = os.path.join(root, *parts[:i], "__init__.py")
            if os.path.isfile(init):
                files.add(os.path.relpath(init, root))
        # a package named as a root brings its own modules
        if is_pkg and name in ("alpaca.observability",):
            folder = os.path.dirname(path)
            for n in sorted(os.listdir(folder)):
                if n.endswith(".py") and n != "__init__.py":
                    todo.append("%s.%s" % (name, n[:-3]))
        package = name if is_pkg else ".".join(parts[:-1])
        mods, ext = _imports_of(path, package)
        external |= ext
        todo.extend(sorted(m for m in mods if m not in seen))
    modules = sorted(n for n in seen if _module_file(root, n)[0])
    return {"files": sorted(files), "modules": modules, "external": sorted(external)}


_CLOSURE_CACHE: dict = {}


def closure_of(root: str) -> dict:
    """import_closure(root), cached for this process by the root and the newest mtime of the
    engine's folders (an edit changes it)."""
    root = os.path.realpath(root)
    key = (root, _tree_signature(root))
    got = _CLOSURE_CACHE.get(key)
    if got is None:
        got = import_closure(root)
        _CLOSURE_CACHE.clear()
        _CLOSURE_CACHE[key] = got
    return got


def _tree_signature(root: str) -> tuple:
    newest, count = 0, 0
    for sub in ("alpaca/research", "alpaca/hooks"):
        for folder, dirs, names in os.walk(os.path.join(root, sub)):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for n in names:
                if n.endswith(".py"):
                    try:
                        newest = max(newest, os.stat(os.path.join(folder, n)).st_mtime_ns)
                    except OSError:
                        pass
                    count += 1
    return (newest, count)


# ================================================================ the engine set
def _site_packages() -> list:
    out = []
    for p in sys.path:
        if p and p.endswith("site-packages") and os.path.isdir(p):
            out.append(os.path.realpath(p))
    return sorted(set(out))


def _non_python_research(root: str) -> list:
    out = []
    base = os.path.join(root, "alpaca", "research")
    for folder, dirs, names in os.walk(base):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for n in sorted(names):
            if not n.endswith(".py"):
                out.append(os.path.join(folder, n))
    return out


def _glob_dir(folder: str, suffixes=None, prefix=None) -> list:
    out = []
    try:
        for n in sorted(os.listdir(folder)):
            full = os.path.join(folder, n)
            if os.path.isfile(full) and (suffixes is None or n.endswith(suffixes)) \
                    and (prefix is None or n.startswith(prefix)):
                out.append(full)
    except OSError:
        pass
    return out


def pyc_of(path: str) -> list:
    """The cached bytecode of a Python file: its .pyc files in the folder's __pycache__."""
    folder = os.path.join(os.path.dirname(path), "__pycache__")
    stem = os.path.basename(path)[:-3]
    out = []
    try:
        for n in sorted(os.listdir(folder)):
            if n.startswith(stem + ".") and n.endswith(".pyc"):
                out.append(os.path.join(folder, n))
    except OSError:
        pass
    return out


def engine_set(root: str | None = None, *, hook_scripts: list | None = None,
               with_pyc: bool = True) -> list:
    """The absolute paths of the files that decide or witness (12.5): the static import
    closure, the non-Python files of alpaca/research/, bin/*, the interpreter's startup files,
    the shield and formation files, the agent briefs, and the hook scripts. The .pyc of each
    Python file is in the set too, by stat only."""
    root = os.path.realpath(root or install_root())
    closure = closure_of(root)
    out = [os.path.join(root, rel) for rel in closure["files"]]
    out += _non_python_research(root)
    out += _glob_dir(os.path.join(root, "bin"))
    out.append(os.path.join(root, "alpaca", "boot.py"))    # starts every isolated process (t-428)
    venv = os.path.join(root, ".venv")
    cfg = os.path.join(venv, "pyvenv.cfg")
    if os.path.isfile(cfg):
        out.append(cfg)
    for sp in _site_packages():
        if sp.startswith(venv + os.sep):
            for n in ("sitecustomize.py", "usercustomize.py"):
                if os.path.isfile(os.path.join(sp, n)):
                    out.append(os.path.join(sp, n))
            out += _glob_dir(sp, ".pth")
    for folder, dirs, names in os.walk(os.path.join(root, ".alpaca", "toolchain", "sqlite")):
        out += [os.path.join(folder, n) for n in sorted(names)]
    out += [os.path.join(root, "formations", "shield.yaml")]
    out += _glob_dir(os.path.join(root, "formations"), ".md", "research-")
    out += _glob_dir(os.path.join(root, "agents"), ".md")
    out += list(hook_scripts or [])
    if with_pyc:
        for p in list(out):
            if p.endswith(".py"):
                out += pyc_of(p)
    return sorted({p for p in out if os.path.isfile(p)})


def _pyc_current(path: str) -> bool:
    """True when this interpreter's .pyc of `path` exists and its timestamp header matches the
    source, the test the import system itself applies, so compiling it again would change nothing
    but its stat."""
    import importlib.util
    try:
        pyc = importlib.util.cache_from_source(path)
        st = os.stat(path)
        with open(pyc, "rb") as fh:
            head = fh.read(16)
    except (OSError, NotImplementedError, ValueError):
        return False
    return (len(head) == 16 and head[:4] == importlib.util.MAGIC_NUMBER
            and int.from_bytes(head[4:8], "little") == 0
            and int.from_bytes(head[8:12], "little") == int(st.st_mtime) & 0xFFFFFFFF
            and int.from_bytes(head[12:16], "little") == st.st_size & 0xFFFFFFFF)


def compile_engine(root: str, files: list) -> None:
    """Byte-compile the engine's Python files (and, in a real run, the venv's site-packages) once,
    so a .pyc that appears later is somebody else's. A .pyc that is already current is left as it
    is: py_compile rewrites a file it compiles, and a rewrite changes the stat that the pin of a
    run already live in this tree holds, which ends that run interrupted."""
    import py_compile
    for p in files:
        if p.endswith(".py") and not _pyc_current(p):
            try:
                py_compile.compile(p, doraise=False, quiet=2,
                                   invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
            except Exception:  # noqa: BLE001 - a file that does not compile is pinned as it is
                pass
    if COMPILE_SITE_PACKAGES:
        for sp in _site_packages():
            try:
                compileall.compile_dir(sp, quiet=2, workers=0)
            except Exception:  # noqa: BLE001
                pass


# ================================================================ import roots
def _scan_root(path: str, project_root: str, names: set, digest: bool = True) -> dict:
    """The pinned listing of one import root. A root other than the project root lists every
    regular file under it that Python could load, with its six figures, and the sorted names of
    every folder that holds one. The project root lists the files Python could import from it
    (top level only) and each folder that holds an __init__.py or is named like a top-level
    name the closure walk found. A folder whose name is not a Python identifier is skipped in
    both modes: no import reaches a module through it, and `site` reads a `.pth` only at the top
    of a site folder, which is a root and never skipped (in-session-runtime.md section 30)."""
    files, folders = {}, {}
    if path == project_root:
        listing = _list_names(path) or []
        folders[path] = [n for n in listing if os.path.isfile(os.path.join(path, n))
                         and n.endswith(_PIN_EXT)]
        for n in listing:
            full = os.path.join(path, n)
            if os.path.isfile(full) and n.endswith(_PIN_EXT):
                files[full] = fingerprint_file(full, digest=digest)
            elif os.path.isdir(full) and not os.path.islink(full) and n.isidentifier():
                if os.path.isfile(os.path.join(full, "__init__.py")) or n in names:
                    folders[full] = [x for x in (_list_names(full) or [])
                                     if os.path.isfile(os.path.join(full, x))
                                     and x.endswith(_PIN_EXT)]
        return {"root": path, "project": True, "files": files, "folders": folders}
    for folder, dirs, fnames in os.walk(path, followlinks=False):
        # no import goes through another folder; the root's own name is never tested (t-443)
        dirs[:] = [d for d in dirs if d.isidentifier()]
        pycache = os.path.basename(folder) == "__pycache__"
        held = []
        for n in fnames:
            if n.endswith(_PIN_EXT):
                full = os.path.join(folder, n)
                files[full] = fingerprint_file(full, digest=digest)
                held.append(n)
        if held and not pycache:
            folders[folder] = sorted(held)
    return {"root": path, "project": False, "files": files, "folders": folders}


def import_roots(root: str | None = None, *, names: set | None = None, digest: bool = True) -> list:
    """The listing of every entry of sys.path of this process, resolved and once each (12.5,
    mechanism 3)."""
    project = os.path.realpath(root or install_root())
    names = names if names is not None else set(closure_of(project)["external"])
    out, seen = [], set()
    for entry in sys.path:
        full = os.path.realpath(entry or os.getcwd())
        if full in seen or not os.path.isdir(full):
            continue
        seen.add(full)
        out.append(_scan_root(full, project, names, digest))
    if project not in seen and os.path.isdir(project):
        out.append(_scan_root(project, project, names, digest))
    return out


def _roots_stat_digest(roots: list) -> str:
    parts = []
    for r in roots:
        for p in sorted(r["files"]):
            parts.append("%s %s" % (p, _stat_key(r["files"][p])))
        for f in sorted(r["folders"]):
            parts.append("%s/ %s" % (f, ",".join(r["folders"][f])))
    return sha256_text("\n".join(parts))


def _diff_roots(old: list, new: list) -> list:
    """The paths that differ between two import-root listings: [{path, change}]."""
    out = []
    olds = {r["root"]: r for r in old}
    news = {r["root"]: r for r in new}
    for root in sorted(set(olds) | set(news)):
        a, b = olds.get(root), news.get(root)
        if a is None or b is None:
            out.append({"path": root, "change": "added" if a is None else "removed"})
            continue
        for p in sorted(set(a["files"]) | set(b["files"])):
            fa, fb = a["files"].get(p), b["files"].get(p)
            if fa is None:
                out.append({"path": p, "change": "added"})
            elif fb is None:
                out.append({"path": p, "change": "removed"})
            elif _stat_key(fa) != _stat_key(fb):
                out.append({"path": p, "change": "touched"})
        for f in sorted(set(a["folders"]) | set(b["folders"])):
            if a["folders"].get(f) != b["folders"].get(f):
                if not any(c["path"].startswith(f + os.sep) for c in out):
                    out.append({"path": f, "change": "listing"})
    return out


# ================================================================ text sources
def settings_files(root: str, cfg: str) -> list:
    return [os.path.join(root, ".claude", "settings.json"),
            os.path.join(root, ".claude", "settings.local.json"),
            os.path.join(cfg, "settings.json")]


def hook_commands(root: str, cfg: str | None = None) -> list:
    """The command strings of the PostToolUse and PreToolUse entries of the three settings files:
    what K8 compares with the command that made a hook body."""
    cfg = cfg or config_dir()
    out = []
    for path in settings_files(root, cfg):
        data = read_json(path) or {}
        for event in ("PostToolUse", "PreToolUse"):
            for grp in (data.get("hooks") or {}).get(event) or []:
                for h in (grp or {}).get("hooks") or []:
                    cmd = h.get("command")
                    if isinstance(cmd, str) and cmd not in out:
                        out.append(cmd)
    return out


def all_hook_commands(root: str, cfg: str | None = None) -> list:
    cfg = cfg or config_dir()
    out = []
    for path in settings_files(root, cfg):
        data = read_json(path) or {}
        for entries in (data.get("hooks") or {}).values():
            for grp in entries or []:
                for h in (grp or {}).get("hooks") or []:
                    cmd = h.get("command")
                    if isinstance(cmd, str) and cmd not in out:
                        out.append(cmd)
    return out


def hook_scripts(root: str, cfg: str | None = None) -> list:
    """The hook scripts the settings name: a word of a command that, with $CLAUDE_PROJECT_DIR and
    ~ expanded, is an existing regular file outside the project, plus the regular files directly
    beside it when its folder holds at most 64 (12.5, item 4)."""
    root = os.path.realpath(root)
    out = []
    for cmd in all_hook_commands(root, cfg):
        try:
            words = shlex.split(cmd)
        except ValueError:
            words = cmd.split()
        for w in words:
            w = w.replace("$CLAUDE_PROJECT_DIR", root).replace("${CLAUDE_PROJECT_DIR}", root)
            w = os.path.expanduser(w)
            if not os.path.isabs(w) or not os.path.isfile(w):
                continue
            real = os.path.realpath(w)
            if real == root or real.startswith(root + os.sep):
                continue
            out.append(real)
            folder = os.path.dirname(real)
            names = _list_names(folder) or []
            if len(names) <= 64:
                # a dot file beside a script is a data file the hook reads, pinned by hook_data_files
                out += [os.path.join(folder, n) for n in names
                        if not n.startswith(".") and os.path.isfile(os.path.join(folder, n))]
    return sorted(set(out))


def hook_data_files(root: str, scripts: list, driver_session: str, cfg: str | None = None) -> list:
    """The files a PostToolUse or PreToolUse hook script turns into the text it prints, found
    from the scripts and not from a fixed list (12.5, item 5): every reference to a dot file of
    the config dir, listed with the driver's session id and with no suffix; the tick file is left
    out; the project's own posture files are listed as well."""
    cfg = cfg or config_dir()
    cmds = hook_commands(root, cfg)
    names = []
    for script in scripts:
        mentioned = False
        for cmd in cmds:
            if os.path.basename(script) in cmd:
                mentioned = True
        if not mentioned:
            continue
        try:
            with open(script, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for m in _DOT_REF.finditer(text):
            name = m.group(1).rstrip(".")
            if name.startswith(TICK_PREFIX):
                continue
            if name not in names:
                names.append(name)
    out = []
    for name in names:
        out.append(os.path.join(cfg, "%s.%s" % (name, driver_session)))
        out.append(os.path.join(cfg, name))
    proj = os.path.join(os.path.realpath(root), ".alpaca", "config")
    for name in (".autodrive-level", ".autodrive-goal"):
        out.append(os.path.join(proj, "%s.%s" % (name, driver_session)))
        out.append(os.path.join(proj, name))
    return out


def text_sources(root: str, driver_session: str, cfg: str | None = None, *,
                 scripts: list | None = None, previous: dict | None = None) -> dict:
    """The text sources of 12.5 item 5: {"shared": {path: record}, "driver": {path: record},
    "folders": {path: names}}. The files that carry the driver's session id are the driver-bound
    group; every other source is the shared group."""
    root = os.path.realpath(root)
    cfg = cfg or config_dir()
    scripts = hook_scripts(root, cfg) if scripts is None else scripts
    prev_shared = (previous or {}).get("shared") or {}
    prev_driver = (previous or {}).get("driver") or {}
    shared, driver, folders = {}, {}, {}
    for p in settings_files(root, cfg) + [os.path.join(root, "CLAUDE.md"),
                                         os.path.join(cfg, "CLAUDE.md")]:
        shared[p] = fingerprint_file(p, prev_shared.get(p))
    for agents in (os.path.join(root, ".claude", "agents"), os.path.join(cfg, "agents")):
        names = _list_names(agents)
        if names is None:
            folders[agents] = None
            continue
        folders[agents] = names
        n = 0
        for folder, dirs, fnames in os.walk(agents):
            for fn in sorted(fnames):
                n += 1
                if n > _MAX_FOLDER_FILES:
                    break
                full = os.path.join(folder, fn)
                shared[full] = fingerprint_file(full, prev_shared.get(full))
    for p in hook_data_files(root, scripts, driver_session, cfg):
        bound = driver_session in os.path.basename(p)
        target = driver if bound else shared
        prev = prev_driver if bound else prev_shared
        target[p] = fingerprint_file(p, prev.get(p))
    return {"shared": shared, "driver": driver, "folders": folders}


def _text_hash(src: dict) -> str:
    parts = []
    for group in ("shared", "driver"):
        for p in sorted(src[group]):
            rec = src[group][p]
            parts.append("%s %s" % (p, rec.get("sha256") if rec.get("present") else "absent"))
    for f in sorted(src["folders"]):
        parts.append("%s/ %s" % (f, ",".join(src["folders"][f]) if src["folders"][f] is not None
                                 else "absent"))
    return sha256_text("\n".join(parts))


def _diff_text(old: dict, new: dict, *, include_driver: bool, metadata: list | None = None) -> list:
    """The group S paths that differ. A metadata-only change of a file (`metadata_only`, section
    24.1) is no change: it goes to `metadata` when a list is given and is left out otherwise."""
    out = []
    groups = ("shared", "driver") if include_driver else ("shared",)
    for group in groups:
        a, b = old.get(group) or {}, new.get(group) or {}
        for p in sorted(set(a) | set(b)):
            ra, rb = a.get(p), b.get(p)
            if ra is None or rb is None:
                out.append({"path": p, "change": "added" if ra is None else "removed"})
            elif _stat_key(ra) != _stat_key(rb):
                if metadata_only(ra, rb, p):
                    if metadata is not None:
                        metadata.append(_metadata_entry(p, ra, rb))
                    continue
                out.append(dict({"path": p, "change": "touched"}, **_touched(ra, rb)))
    fa, fb = old.get("folders") or {}, new.get("folders") or {}
    for f in sorted(set(fa) | set(fb)):
        if fa.get(f) != fb.get(f):
            out.append({"path": f, "change": "listing"})
    return out


# ================================================================ the pin
def _pin_summary(pin: dict) -> dict:
    return {k: pin[k] for k in ("engine_sha256", "engine_stat_sha256", "engine_files",
                                "pin_file", "hook_scripts", "hook_commands", "import_roots",
                                "config_dir") if k in pin}


def take_pin(root: str, driver_session: str, pin_path: str, *, cfg: str | None = None,
             compile_files: bool = True, previous_text: dict | None = None) -> dict:
    """Compute the pin of the engine set, the import roots and the text sources, write the pin
    file and return its summary: the keys the research-driver event carries (12.5, mechanism 3)."""
    install = os.path.realpath(install_root())
    cfg = cfg or config_dir()
    scripts = hook_scripts(root, cfg)
    files = engine_set(install, hook_scripts=scripts)
    if compile_files:
        compile_engine(install, files)
        files = engine_set(install, hook_scripts=scripts)
    closure = closure_of(install)
    rec_files = {p: fingerprint_file(p) for p in files}
    folders = {}
    for p in files:
        d = os.path.dirname(p)
        if "__pycache__" in d:
            continue
        if d not in folders:
            folders[d] = _list_names(d) or []
    roots = import_roots(install, names=set(closure["external"]))
    text = text_sources(root, driver_session, cfg, scripts=scripts, previous=previous_text)
    pairs = sorted((p, r.get("sha256")) for p, r in rec_files.items())
    for r in roots:
        pairs += sorted((p, rec.get("sha256")) for p, rec in r["files"].items())
    engine_sha = sha256_text(json.dumps(pairs))
    stat_digest = sha256_text(json.dumps(sorted((p, _stat_key(r)) for p, r in rec_files.items()))
                              + json.dumps(sorted(folders.items())) + _roots_stat_digest(roots))
    absent = []
    pin = {"schema": PIN_SCHEMA, "install_root": install, "project_root": os.path.realpath(root),
           "driver_session": driver_session, "files": rec_files, "folders": folders,
           "import_roots": roots, "text": text, "absent_names": absent,
           "hook_scripts": scripts, "hook_commands": hook_commands(root, cfg),
           "config_dir": cfg, "engine_sha256": engine_sha, "engine_stat_sha256": stat_digest,
           "engine_files": len(rec_files), "shared_sha256": _text_hash(
               {"shared": text["shared"], "driver": {}, "folders": text["folders"]}),
           "text_sha256": _text_hash(text), "pin_file": pin_path}
    _write_json(pin_path, pin)
    summary = _pin_summary(pin)
    summary["import_roots"] = [r["root"] for r in roots]
    summary["pin_file"] = pin_path
    summary["text_sha256"] = pin["text_sha256"]
    summary["shared_sha256"] = pin["shared_sha256"]
    summary["config_base_sha256"] = pin["text_sha256"]
    summary["hook_data"] = {p: {"present": r.get("present", False), "sha256": r.get("sha256"),
                                "ctime_ns": r.get("ctime_ns")} for p, r in text["driver"].items()}
    return summary


def engine_unchanged(root: str, pin: dict | str, *, driver_session: str | None = None,
                     include_text: bool = True) -> dict:
    """The stat check of the pin run now: {"ok", "files", "sha256", "changed" (up to 50 paths
    that differ), "text_changed", "metadata"}. A file counts as changed when it is added, removed,
    or has a different size, inode, mtime or ctime, whatever its content now is, except that a
    metadata-only change of a text source (group S, `metadata_only`, section 24.1) is listed under
    `metadata` and is no change; the engine files, hook scripts and import roots keep the full
    rule. The driver-bound text sources are compared only when `driver_session` is the session
    the pin names."""
    if isinstance(pin, str):
        pin = read_json(pin) or {}
    if not pin:
        return {"ok": False, "files": 0, "sha256": None, "changed": [{"path": "(no pin)",
                                                                       "change": "removed"}],
                "text_changed": [], "metadata": []}
    changed = []
    for p, rec in sorted((pin.get("files") or {}).items()):
        now = stat_tuple(p)
        cur = dict(now, present=True) if now else {"present": False}
        if _stat_key(cur) != _stat_key(rec):
            changed.append({"path": p, "change": "touched" if now else "removed"})
    for d, names in sorted((pin.get("folders") or {}).items()):
        if (_list_names(d) or []) != names:
            known = set(pin.get("files") or {})
            now = _list_names(d) or []
            for n in sorted(set(now) - set(names)):
                if os.path.join(d, n) not in known:
                    changed.append({"path": os.path.join(d, n), "change": "added"})
    # the entries the pin recorded are rescanned, whatever this process's own sys.path holds, so
    # a verb process and the runner compare against the same list
    install = pin.get("install_root") or install_root()
    names = set(closure_of(install)["external"])
    roots = [_scan_root(r["root"], install, names, False) for r in pin.get("import_roots") or []
             if os.path.isdir(r["root"])]
    old_roots = [r for r in pin.get("import_roots") or [] if os.path.isdir(r["root"])]
    changed += _diff_roots(old_roots, roots)
    text_changed, metadata = [], []
    if include_text:
        cur = text_sources(pin.get("project_root") or install_root(),
                           pin.get("driver_session") or "", pin.get("config_dir"),
                           scripts=pin.get("hook_scripts"))
        bound = driver_session is not None and driver_session == pin.get("driver_session")
        text_changed = _diff_text(pin.get("text") or {}, cur, include_driver=bound,
                                  metadata=metadata)
        changed += text_changed
    seen, uniq = set(), []
    for c in changed:
        if c["path"] not in seen:
            seen.add(c["path"])
            uniq.append(c)
    return {"ok": not uniq, "files": len(pin.get("files") or {}),
            "sha256": pin.get("engine_sha256"), "changed": uniq[:MAX_CHANGED],
            "changed_count": len(uniq), "text_changed": text_changed,
            "metadata": metadata[:MAX_CHANGED]}


def config_hash(root: str, driver_session: str, cfg: str | None = None, *,
                scripts: list | None = None) -> str:
    """The K13 hash of the files that decide what text reaches a running agent or what the agent
    is: the settings files, the CLAUDE.md files, the agent folders and the hook data files."""
    text = text_sources(root, driver_session, cfg, scripts=scripts)
    return _text_hash(text)


# ================================================================ preload
def preload(root: str | None = None, *, block: bool | None = None) -> dict:
    """Import every module of the static closure by name and, for the names the closure imports
    that the host lacks, set sys.modules[name] = None so a later import raises ImportError
    whatever file appears on the path (12.5, mechanism 2). Returns {"loaded", "absent"}. This is
    the one dynamic import of the engine."""
    root = os.path.realpath(root or install_root())
    closure = closure_of(root)
    loaded, absent = 0, []
    for name in closure["modules"]:
        try:
            importlib.import_module(name)
            loaded += 1
        except Exception:  # noqa: BLE001 - a module that cannot import here is left to its caller
            continue
    do_block = PRELOAD_BLOCK if block is None else block
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    for name in closure["external"]:
        if name in sys.modules and sys.modules[name] is not None:
            continue
        try:
            importlib.import_module(name)
        except ImportError:
            absent.append(name)
            if do_block and name not in stdlib:
                sys.modules[name] = None
        except Exception:  # noqa: BLE001
            continue
    return {"loaded": loaded, "absent": sorted(absent)}


def safe_path() -> bool:
    """True when no script or working folder is put first on the path: the -P flag, which -I sets.
    Python 3.10, the oldest Python Alpaca runs on, has no -P and no `sys.flags.safe_path` (both came
    in 3.11), and there -I itself leaves the script's folder and the working folder off the path, so
    -I is what the flag reads (op-008 t-571)."""
    return bool(getattr(sys.flags, "safe_path", sys.flags.isolated))


def isolation_ok() -> bool:
    """True when this process is an isolated interpreter whose import path holds no folder an agent
    can write ahead of the package (12.5, mechanism 1): -I and -P are set, `alpaca/boot.py` of the
    install root loaded the package by its location (its mark names the install root and the
    package file is the install root's), and the install root is not on the path at all (t-428).
    A process that found `alpaca` through the install root on its path (the legacy
    `alpaca-root.pth` layout), or loaded it some other way, is refused."""
    if not (sys.flags.isolated and safe_path()):
        return False
    import alpaca
    from alpaca import boot
    install = os.path.realpath(install_root())
    mark = getattr(alpaca, boot.BOOT_MARK, None)
    if not mark or os.path.realpath(mark) != install:
        return False
    if os.path.realpath(getattr(alpaca, "__file__", "") or "") != os.path.join(install, "alpaca",
                                                                             "__init__.py"):
        return False
    return not any(os.path.realpath(p or ".") == install for p in sys.path)


# ================================================================ the control plane
#: text forms with no path meaning, matched only where they run something (section 7, rule S15)
CONTROL_NAMES = ("bin/alpaca", "alpaca research", "-m alpaca", "alpaca.db", "from alpaca",
                 "import alpaca", "sqlite3", "alpaca.hooks", "alpaca.research",
                 "alpaca.observability", "sudo")
#: the commands whose first word writes a path it names: K16's table, one source (t-354, 27.1)
WRITE_COMMANDS = pathscan.WRITE_WORDS
#: interpreters whose -c text or heredoc is code
INTERPRETERS = ("python", "python3", "python3.10", "python3.11", "python3.12", "python3.13",
                "python3.14", "node", "perl", "ruby", "bash", "sh", "zsh", "dash", "ksh")
#: git subcommands that write a repository
GIT_WRITES = ("add", "commit", "stash", "reset", "checkout", "config", "rebase", "merge", "push",
              "pull", "fetch", "clean", "rm", "mv", "restore", "switch", "cherry-pick", "revert",
              "tag", "branch", "am", "apply", "gc", "prune", "worktree", "submodule", "remote",
              "update-ref", "write-tree", "commit-tree", "hash-object", "init", "clone",
              "filter-branch", "reflog", "notes")


def control_paths(root: str, hook_scripts: list | None = None, cfg: str | None = None) -> dict:
    """The paths an agent may not name or write, resolved (section 7, 12.3 and 12.6):
    {"deny" (named for any use), "write" (named only when a call writes them), "read_deny"
    (a read is flagged: other agents' transcripts)}. The guard and rule S15 use this one
    table, so the scan and the guard cannot drift apart."""
    root = os.path.realpath(root)
    install = os.path.realpath(install_root())
    cfg = cfg or config_dir()
    hm = home()
    deny = [os.path.join(root, ".alpaca", n) for n in ("research", "pool", "transcripts", "config",
                                                       "verify")]
    deny.append(os.path.join(root, ".alpaca", "alpaca.db"))
    deny.append(os.path.join(root, "contracts", "research"))
    for base in {root, hm, cfg}:
        deny += [os.path.join(base, ".claude", "settings.json"),
                 os.path.join(base, ".claude", "settings.local.json"),
                 os.path.join(base, ".claude", "agents"), os.path.join(base, ".claude", "projects")]
    deny += [os.path.join(cfg, "settings.json"), os.path.join(cfg, "settings.local.json"),
             os.path.join(cfg, "agents"), os.path.join(cfg, "projects")]
    for r in context_root_paths(root, cfg, runtime="codex"):
        deny.append(r)
    deny += [os.path.join(root, "CLAUDE.md"), os.path.join(hm, "CLAUDE.md"),
             os.path.join(cfg, "CLAUDE.md")]
    for folder in (cfg, os.path.join(root, ".alpaca", "config")):
        try:
            for n in os.listdir(folder):
                if n.startswith(".autodrive-"):
                    deny.append(os.path.join(folder, n))
        except OSError:
            pass
    write = [os.path.join(install, "bin"), os.path.join(install, "setup"),
             os.path.join(install, ".venv")]
    for rel in closure_of(install)["files"]:
        write.append(os.path.join(install, rel))
    write += [os.path.join(install, "alpaca", "research"), os.path.join(install, "alpaca", "hooks")]
    for cut in ENGINE_CUT:
        write.append(os.path.join(install, cut.replace(".", os.sep) + ".py"))
    write += list(hook_scripts or [])
    return {"deny": sorted({_real(p) for p in deny}), "write": sorted({_real(p) for p in write}),
            "read_deny": sorted({os.path.join(cfg, "projects"), os.path.join(hm, ".claude", "projects"),
                                 os.path.join(root, ".claude", "projects")})}


def under(path: str, prefix: str) -> bool:
    path, prefix = os.path.normpath(path), os.path.normpath(prefix)
    return path == prefix or path.startswith(prefix.rstrip(os.sep) + os.sep)


# ================================================================ the context roots and the ledger
EXCLUDED_CONFIG = ("remote", "sessions", "file-history", "backups", "cache", ".ctx-band-state",
                   # t-351 F46 (section 23.5): the runtime's own state that another session of the
                   # account writes during a run and that no agent loads as instructions
                   "telemetry", "history.jsonl", "mcp-needs-auth-cache.json", "state", "todos",
                   "statsig", "debug", "paste-cache")
EXCLUDED_DOT_PREFIXES = (".autodrive-tick.", ".caveman-active", ".credentials.json", ".last-cleanup",
                         ".claude.json",
                         # t-360 FU-7 (section 24.3): the usage poller of a pooled home rewrites
                         # .soap-usage.json and its temp file every 3 minutes, and .oauth-token is a
                         # credential; neither is loaded as instructions (top level only)
                         ".soap-usage", ".oauth-token")
EXCLUDED_CODEX = ("packages", "tmp", ".tmp", "sessions", "app-server-control", "cache", "worktrees",
                  # t-360 FU-7 (section 24.3): per-thread lock files and .coordination.lock,
                  # created and rewritten at a thread resume; locks, never loaded
                  "thread-writer-locks")
#: the Codex folders where another thread adds and removes files (grow-only: a rewrite of a file
#: present on both sides is still a change); attachments/ holds the images pasted into one thread
#: (t-360 FU-7, section 24.3)
GROW_CODEX = ("shell_snapshots", "attachments")
EXCLUDED_CODEX_GLOBS = ("logs_", "state_", "thread_history_", "goals_", "queue_")
EXCLUDED_CODEX_FILES = ("models_cache.json", "session_index.jsonl",
                        # t-360 FU-7 (section 24.3): the Codex credential, rewritten by a token
                        # refresh; the Codex form of .credentials.json
                        "auth.json")
_SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_IMPORT = re.compile(r"(?<![A-Za-z0-9_.@])@([~./][^\s`'\"<>)]*|[A-Za-z0-9_][A-Za-z0-9_./-]*\.md)")
_ANCESTOR_FILES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "AGENTS.override.md", ".mcp.json")


def _ancestors(root: str) -> list:
    out, cur = [], os.path.realpath(root)
    while True:
        out.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            return out
        cur = parent


def context_root_paths(root: str, cfg: str, *, runtime: str = "claude") -> list:
    """The folders and files that are roots of the set (12.6, table of roots), as paths, for the
    guard's deny list: every root whole, the Codex roots included whatever the runtime."""
    out = [os.path.join(root, ".claude"), cfg, os.path.join(home(), ".claude"), "/etc/claude-code"]
    for p in _ancestors(root):
        out += [os.path.join(p, n) for n in _ANCESTOR_FILES] + [os.path.join(p, ".claude"),
                                                                  os.path.join(p, ".codex"),
                                                                  os.path.join(p, ".agents")]
    out += [os.environ.get("CODEX_HOME") or os.path.join(home(), ".codex"),
            os.path.join(home(), ".agents"), os.path.join(root, ".agents")]
    return [p for p in out if p]


def _settings_dirs(root: str, cfg: str) -> list:
    out = []
    for path in settings_files(root, cfg):
        data = read_json(path) or {}
        v = data.get("autoMemoryDirectory")
        if isinstance(v, str) and v:
            out.append(_real(v))
        for d in data.get("additionalDirectories") or []:
            if isinstance(d, str) and d:
                out.append(_real(d))
    return out


def context_roots(root: str, cfg: str, driver_session: str, settings: list | None = None, *,
                  runtime: str = "claude", extra: tuple = ()) -> list:
    """The roots of the set (12.6): [{"path", "kind" ("dir" or "file"), "n" (the root's number),
    "present"}]. The Codex roots only for a Codex driver; the roots are deduplicated by realpath."""
    root = os.path.realpath(root)
    cfg = _real(cfg)
    raw = [(1, os.path.join(root, ".claude"), "dir"), (2, cfg, "dir")]
    if os.path.join(home(), ".claude") != cfg:
        raw.append((3, os.path.join(home(), ".claude"), "dir"))
    for p in _ancestors(root):
        for n in _ANCESTOR_FILES:
            raw.append((4, os.path.join(p, n), "file"))
        raw.append((4, os.path.join(p, ".claude"), "dir"))
    raw.append((5, "/etc/claude-code", "dir"))
    for d in (settings if settings is not None else _settings_dirs(root, cfg)):
        raw.append((6, d, "dir"))
    if runtime == "codex":
        for p in _ancestors(root):
            raw.append((10, os.path.join(p, ".codex"), "dir"))
        raw.append((10, _real(os.environ.get("CODEX_HOME") or os.path.join(home(), ".codex")),
                    "dir"))
        raw += [(10, os.path.join(home(), ".agents"), "dir"), (10, os.path.join(root, ".agents"),
                                                                "dir")]
    for e in extra:
        raw.append((7, _real(e), "file"))
    out, seen = [], set()
    for n, p, kind in raw:
        real = _real(p)
        if real in seen:
            continue
        seen.add(real)
        out.append({"path": real, "kind": kind, "n": n,
                    "present": os.path.isdir(real) if kind == "dir" else os.path.isfile(real)})
    return out


def _slug_of(root: str, transcript: str | None) -> str:
    if transcript:
        parts = os.path.normpath(transcript).split(os.sep)
        if "projects" in parts:
            i = parts.index("projects")
            if i + 1 < len(parts):
                return parts[i + 1]
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(root))


def _is_config_home(path: str, cfg: str) -> bool:
    return path in (cfg, os.path.join(home(), ".claude"))


_NOT_PATH = "\\s'\"\\\\;&|<>()$`"
_SHELL_SOURCE = re.compile(r"(/[^%s]+/shell-snapshots/snapshot-[^%s/]+\.sh)"
                           % (_NOT_PATH, _NOT_PATH))


def _parent_pid(pid: int) -> int | None:
    """The parent of `pid` from /proc/<pid>/stat (the comm field may hold spaces and brackets, so
    the fields are read after the last `)`), or None."""
    try:
        with open("/proc/%d/stat" % pid, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        fields = text[text.rindex(")") + 1:].split()
        return int(fields[1])
    except (OSError, ValueError, IndexError):
        return None


def _cmdline_of(pid: int) -> str:
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            return fh.read(1 << 16).replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def find_driver_snapshot(pid: int | None = None) -> str | None:
    """The shell snapshot the driving session's own Bash shell sources (t-351 F46, section 23.5):
    walk the /proc/<pid>/stat ancestry from `pid` (this process when None), look in each command
    line for `shell-snapshots/snapshot-*.sh`, and return the realpath of the first one that lies
    under realpath(<current config dir>)/shell-snapshots and exists; else None. The file is never
    guessed from a modification time, and a process with no such ancestor (a detached runner)
    gets None."""
    try:
        folder = os.path.join(config_dir(), "shell-snapshots")
        cur = int(pid) if pid is not None else os.getpid()
    except (OSError, ValueError, TypeError):
        return None
    for _ in range(64):
        if cur is None or cur <= 1:
            return None
        for hit in _SHELL_SOURCE.findall(_cmdline_of(cur)):
            real = os.path.realpath(hit)
            if real != folder and under(real, folder) and os.path.isfile(real):
                return real
        cur = _parent_pid(cur)
    return None


def _excluded(rel: str, parts: list, *, config_home: bool, slug: str, driver_session: str,
              codex: bool, driver_rel: str | None = None) -> tuple:
    """(excluded, grow_only) for the entry `rel` (relative to its root). `driver_rel` is the path
    of the driver's own shell snapshot relative to this root (t-351 F46, section 23.5), or None."""
    first = parts[0]
    if first == "worktrees":
        return True, False
    if codex:
        if first in EXCLUDED_CODEX:
            return True, False
        if first in GROW_CODEX:
            return False, True
        if len(parts) == 1 and (first in EXCLUDED_CODEX_FILES
                                or any(first.startswith(g) and ".sqlite" in first
                                       for g in EXCLUDED_CODEX_GLOBS)):
            return True, False
        return False, False
    if not config_home:
        return False, False
    if first == "projects":
        if len(parts) >= 3 and parts[1] == slug and parts[2] == "memory":
            return False, False
        if len(parts) == 1 or (len(parts) == 2 and parts[1] == slug):
            return False, False
        return True, False
    if first in EXCLUDED_CONFIG:
        return True, False
    if first == "plugins" and (".trash" in parts[1:2] or fnmatch.fnmatchcase(parts[-1],
                                                                              "*.bak.port-*")):
        return True, False
    if first == "session-env":
        if len(parts) >= 2 and parts[1] == driver_session:
            return False, False
        if len(parts) == 1:
            return False, False
        return True, False
    if first == "shell-snapshots":
        if driver_rel is None:
            # no driver snapshot known: the folder is grow-only (a file added or removed is no
            # change, a rewrite of a file present on both sides is)
            return False, True
        # the driver's own snapshot is the one member; every other file is another session's
        return (len(parts) > 1 and rel != driver_rel), False
    if first.startswith(".") and len(parts) == 1:
        if any(first.startswith(p) for p in EXCLUDED_DOT_PREFIXES):
            return True, False
        m = _SESSION_ID.search(first)
        if m and driver_session not in first:
            return True, False
    return False, False


def _driver_rel(rootpath: str, driver_snapshot: str | None, config_home: bool) -> str | None:
    """The driver's snapshot relative to the root, when it lies in this root's shell-snapshots
    folder (t-351 F46, section 23.5), else None."""
    if not driver_snapshot or not config_home:
        return None
    folder = os.path.join(os.path.realpath(rootpath), "shell-snapshots")
    real = os.path.realpath(driver_snapshot)
    if not under(real, folder) or real == folder:
        return None
    return os.path.relpath(real, os.path.realpath(rootpath))


def _walk_root(rootpath: str, *, config_home: bool, slug: str, driver_session: str,
               codex: bool, prev: dict | None, driver_snapshot: str | None = None) -> tuple:
    """(files {path: record}, folders {path: names}, grow {paths of grow-only folders}) of one
    root folder. With a driver snapshot in this root's shell-snapshots folder, that file is the
    folder's only member and the folder is not grow-only; without one the folder is grow-only
    (t-351 F46, section 23.5)."""
    files, folders, grow = {}, {}, set()
    driver_rel = _driver_rel(rootpath, driver_snapshot, config_home)
    for folder, dirs, names in os.walk(rootpath, followlinks=False):
        rel_folder = os.path.relpath(folder, rootpath)
        keep = []
        listing = []
        for d in sorted(dirs):
            rel = d if rel_folder == "." else os.path.join(rel_folder, d)
            parts = rel.split(os.sep)
            ex, g = _excluded(rel, parts, config_home=config_home, slug=slug,
                              driver_session=driver_session, codex=codex, driver_rel=driver_rel)
            if ex:
                continue
            keep.append(d)
            listing.append(d)
            if g:
                grow.add(os.path.join(folder, d))
        dirs[:] = keep
        in_grow = any(under(folder, g) for g in grow)
        for n in sorted(names):
            rel = n if rel_folder == "." else os.path.join(rel_folder, n)
            parts = rel.split(os.sep)
            ex, g = _excluded(rel, parts, config_home=config_home, slug=slug,
                              driver_session=driver_session, codex=codex, driver_rel=driver_rel)
            if ex:
                continue
            listing.append(n)
            full = os.path.join(folder, n)
            if os.path.isfile(full) or os.path.islink(full):
                files[full] = fingerprint_file(full, (prev or {}).get(full))
                if os.path.islink(full):
                    target = os.path.realpath(full)
                    if os.path.isfile(target):
                        files[target] = fingerprint_file(target, (prev or {}).get(target))
        if not in_grow:
            folders[folder] = listing
    return files, folders, grow


def _imports_in(files: dict, known: set) -> list:
    """The files that member markdown files import with @path, followed as far as the runtime
    follows imports, resolved against the importing file."""
    found, todo = [], [p for p in files if p.endswith(".md")]
    seen = set(todo)
    while todo and len(found) < 256:
        p = todo.pop()
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                text = fh.read(1 << 20)
        except OSError:
            continue
        for m in _IMPORT.finditer(text):
            target = os.path.expanduser(m.group(1))
            if not os.path.isabs(target):
                target = os.path.join(os.path.dirname(p), target)
            target = os.path.realpath(target)
            if target in seen or not os.path.isfile(target):
                continue
            seen.add(target)
            found.append(target)
            if target.endswith(".md"):
                todo.append(target)
    return sorted(set(found) - known)


def _mcp_sha(root: str, cfg: str) -> str | None:
    """The sha256 of the JSON, with sorted keys, of the user state file's top-level mcpServers
    and projects[<root>].mcpServers (root 9)."""
    for path in (os.path.join(cfg, ".claude.json"), os.path.join(home(), ".claude.json")):
        data = read_json(path)
        if isinstance(data, dict):
            proj = ((data.get("projects") or {}).get(root) or {}).get("mcpServers")
            return sha256_text(json.dumps({"mcpServers": data.get("mcpServers"),
                                           "project": proj}, sort_keys=True))
    return None


def context_fingerprint(root: str, cfg: str, driver_session: str, *, runtime: str = "claude",
                        transcript: str | None = None, extra: tuple = (), previous: dict | None = None,
                        group_s: set | None = None, driver_snapshot: str | None = None) -> dict:
    """The fingerprint of group M (12.6): {"files" {path: record}, "folders", "roots" [{path,
    present}], "imports", "mcp_sha256", "extra", "sha256", "stat_sha256", "driver_session",
    "slug", "grow", "driver_snapshot"}. Group S files (the settings files, the CLAUDE.md files, the
    agent folders and the hook data files) are in the pin and left out here: a member is in one
    group only. `driver_snapshot` is the value `find_driver_snapshot` returned when the driver was
    registered (never looked for again); `grow` lists the grow-only folders, whose added or removed
    files `context_changes` skips (t-351 F46, section 23.5). The driver snapshot stays a member
    while it exists and a file created at its path is one; its removal is no change to
    `context_changes` only after a restart of the driver's CLI is on the record (t-360, section
    24.2)."""
    root = os.path.realpath(root)
    cfg = _real(cfg)
    slug = _slug_of(root, transcript)
    prev = (previous or {}).get("files") or {}
    group_s = group_s or set()
    files, folders, roots, grow = {}, {}, [], set()
    for r in context_roots(root, cfg, driver_session, runtime=runtime, extra=extra):
        roots.append({"path": r["path"], "present": r["present"], "n": r["n"]})
        if not r["present"]:
            continue
        if r["kind"] == "file":
            files[r["path"]] = fingerprint_file(r["path"], prev.get(r["path"]))
            continue
        f, fo, g = _walk_root(r["path"], config_home=_is_config_home(r["path"], cfg), slug=slug,
                              driver_session=driver_session, codex=r["n"] == 10,
                              prev=prev, driver_snapshot=driver_snapshot)
        files.update(f)
        folders.update(fo)
        grow |= g
    imports = _imports_in(files, set(files))
    for p in imports:
        files[p] = fingerprint_file(p, prev.get(p))
    for p in list(files):
        if p in group_s:
            del files[p]
    mcp = _mcp_sha(root, cfg)
    pairs = sorted((p, r.get("sha256") if r.get("present") else "absent") for p, r in files.items())
    pairs += [(f + "/", ",".join(n)) for f, n in sorted(folders.items())]
    pairs += [("mcp", mcp or "")]
    sha = sha256_text(json.dumps(pairs))
    stat_sha = sha256_text(json.dumps([(p, _stat_key(r)) for p, r in sorted(files.items())]
                                      + [(f, n) for f, n in sorted(folders.items())]))
    return {"schema": LEDGER_SCHEMA, "files": files, "folders": folders, "roots": roots,
            "imports": imports, "mcp_sha256": mcp, "extra": list(extra), "sha256": sha,
            "stat_sha256": stat_sha, "driver_session": driver_session, "slug": slug,
            "runtime": runtime, "transcript": transcript, "grow": sorted(grow),
            "driver_snapshot": driver_snapshot}


def take_ledger(folder: str, ledger: dict, *, tag: str | None = None) -> dict:
    """Write the ledger as <folder>/context-ledger.<sha12>.json (once per content) and return the
    event key: {ledger_file, sha256, stat_sha256, files (the sha256 and ctime of each), roots,
    imports, mcp_sha256, extra}."""
    # the file is named by the content and the driver it belongs to: two sessions with the same files
    # keep two ledgers, since the driver-bound members are compared for the ledger's own driver only
    ident = hashlib.sha256(("%s|%s" % (ledger["sha256"], ledger.get("driver_session"))).encode(
        "utf-8")).hexdigest()
    name = "context-ledger.%s.json" % ident[:12]
    path = os.path.join(folder, name)
    if not os.path.isfile(path):
        _write_json(path, ledger)
    return {"ledger_file": path, "sha256": ledger["sha256"], "stat_sha256": ledger["stat_sha256"],
            "files": {p: {"sha256": r.get("sha256"), "ctime_ns": r.get("ctime_ns")}
                      for p, r in sorted(ledger["files"].items())},
            "roots": [{"path": r["path"], "present": r["present"]} for r in ledger["roots"]],
            "imports": ledger["imports"], "mcp_sha256": ledger["mcp_sha256"],
            "extra": ledger["extra"]}


def load_ledger(event_context: dict | None) -> dict | None:
    if not event_context:
        return None
    return read_json(event_context.get("ledger_file") or "")


def _moved(ra: dict, rb: dict) -> list:
    """The stat fields that differ between two records of one file (t-360 F44, section 24.1): a
    diagnosis field of a `touched` change, read by no decision."""
    return [k for k in ("present", "size", "ino", "mtime_ns", "ctime_ns") + PERMISSION_FIELDS
            if ra.get(k) != rb.get(k)]


def _touched(ra: dict, rb: dict) -> dict:
    """The diagnosis fields of a `touched` change (t-360 F44, section 24.1): `moved`, and
    `content_equal` when both digests are known. No decision reads either; an end by a pass that
    moves only the ctime (a no-op chgrp, or an edit undone) names itself in the marker."""
    out = {"moved": _moved(ra, rb)}
    if ra.get("sha256") is not None and rb.get("sha256") is not None:
        out["content_equal"] = ra["sha256"] == rb["sha256"]
    return out


def context_changes(old: dict, new: dict, *, check_session: str | None = None,
                    driver_restarted: bool = False, metadata: list | None = None) -> list:
    """The group M paths that differ between a ledger and a fingerprint now: [{path, change,
    old_sha256, new_sha256}], a `touched` change also with `moved` and `content_equal` (t-360,
    section 24.1). The driver-bound members (session-env/<driver>/) are compared only when
    `check_session` is the ledger's own driver.

    The removal of the ledger's stored driver snapshot is no change only when `driver_restarted`
    is true: the record holds a restart of the driver's CLI process after the driver event
    (`jobs.driver_restarted`). The runtime removes the file when that process exits, the restart
    already ends the run RUN-DRIVER-GONE, and the resume re-finds the new process's snapshot. With
    no restart on the record the removal is `removed`, marked `driver_snapshot: True`: a live CLI
    whose snapshot is missing runs each later Bash call as a login shell, which reads the shell
    startup files live, and those files are in no root (t-360, section 24.2, check round 2 N1).
    A file created at the stored path again is a change either way.

    A metadata-only change of a member (`metadata_only`: same bytes, size, inode and mtime, a new
    uid, gid or mode, still readable and with the same execute right; orchestrator ruling
    dec-464755c60ab4, section 24.1) is no change: it goes to `metadata` when a list is given. A
    ctime move with nothing else changed stays `touched`."""
    out = []
    bound = check_session is not None and check_session == old.get("driver_session")
    a, b = old.get("files") or {}, new.get("files") or {}
    snap = old.get("driver_snapshot")
    snap = os.path.realpath(snap) if isinstance(snap, str) and snap else None
    if snap is not None and os.path.basename(os.path.dirname(snap)) != "shell-snapshots":
        snap = None                       # only a snapshot file is read this way, never a member
    # a file added or removed under a grow-only folder is no change (a rewrite of a file present
    # on both sides is); a ledger with no `grow` key is read under the fingerprint's own set
    grow = sorted(set(old.get("grow") or ()) | set(new.get("grow") or ()))
    for p in sorted(set(a) | set(b)):
        if not bound and "%ssession-env%s%s" % (os.sep, os.sep, old.get("driver_session")) in p:
            continue
        ra, rb = a.get(p), b.get(p)
        if (ra is None or rb is None) and any(under(p, g) for g in grow):
            continue
        if rb is None and ra is not None and snap is not None and os.path.realpath(p) == snap:
            if driver_restarted:
                # the driver's CLI restarted and removed it at exit; the restart ends the run and
                # the resume re-finds the new snapshot (t-360, section 24.2)
                continue
            # no restart on the record: the live CLI now runs a login shell (section 24.2)
            out.append({"path": p, "change": "removed", "old_sha256": ra.get("sha256"),
                        "new_sha256": None, "driver_snapshot": True})
            continue
        if ra is None or rb is None:
            out.append({"path": p, "change": "added" if ra is None else "removed",
                        "old_sha256": (ra or {}).get("sha256"), "new_sha256": (rb or {}).get("sha256")})
        elif _stat_key(ra) != _stat_key(rb):
            if metadata_only(ra, rb, p):
                if metadata is not None:
                    metadata.append(_metadata_entry(p, ra, rb))
                continue
            out.append(dict({"path": p, "change": "touched", "old_sha256": ra.get("sha256"),
                             "new_sha256": rb.get("sha256")}, **_touched(ra, rb)))
    fa, fb = old.get("folders") or {}, new.get("folders") or {}
    # with a stored driver snapshot its folder's listing holds only that member's name (every other
    # name is excluded before the listing), so it carries nothing the file record does not; the
    # file record decides, with or without a restart
    snap_folder = os.path.dirname(snap) if snap is not None else None
    for f in sorted(set(fa) | set(fb)):
        if not bound and "%ssession-env%s%s" % (os.sep, os.sep, old.get("driver_session")) in f:
            continue
        if snap_folder is not None and os.path.realpath(f) == snap_folder:
            continue
        if fa.get(f) != fb.get(f) and not any(c["path"].startswith(f + os.sep) for c in out):
            out.append({"path": f, "change": "listing", "old_sha256": None, "new_sha256": None})
    ra, rb = old.get("roots") or [], new.get("roots") or []
    present = lambda rs: {r["path"]: r["present"] for r in rs}  # noqa: E731
    pa, pb = present(ra), present(rb)
    for p in sorted(set(pa) | set(pb)):
        if pa.get(p) != pb.get(p) and not any(c["path"] == p or under(c["path"], p) for c in out):
            out.append({"path": p, "change": "root", "old_sha256": None, "new_sha256": None})
    if old.get("mcp_sha256") != new.get("mcp_sha256"):
        out.append({"path": "mcpServers", "change": "touched", "old_sha256": old.get("mcp_sha256"),
                    "new_sha256": new.get("mcp_sha256")})
    return out


def attachment_member(ledger: dict | None, pin: dict | None, path: str) -> bool:
    """K8 rule 3: is `path` a member of the job's set, the pin's group S files or the hand's group
    M ledger? A ledger alone lacks the two CLAUDE.md, a pin alone lacks the memory index; the
    paths are compared after realpath."""
    real = _real(path)
    s_files = set()
    for group in ("shared", "driver"):
        for p, rec in ((pin or {}).get("text") or {}).get(group, {}).items():
            if rec.get("present"):
                s_files.add(_real(p))
    m_files = {_real(p) for p, r in ((ledger or {}).get("files") or {}).items() if r.get("present")}
    return real in s_files or real in m_files


def tail_of(path: str, offset: int):
    """Read the pool file from `offset` to its end as parsed lines (bad lines skipped)."""
    out = []
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, int(offset or 0)))
            data = fh.read()
    except OSError:
        return out
    for raw in data.split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def context_writers(root: str, pool_files: list, paths_changed: list, *, offsets: dict | None = None,
                    transcript_calls: set | None = None) -> dict:
    """Put each changed path in a writer class (12.6, "Who wrote a change"): `agent` when a `pre`
    pool line with an agent id names the path or a folder that holds it, `driver` when only a
    line with no agent id does, `unattributed` when no line names it. A line also counts as an
    agent's when its tool_use_id is among `transcript_calls` (the cross-check of K12). `agent`
    wins over `driver`. `pool_files` is [(path, offset)]."""
    from alpaca.research import pathscan
    lines = []
    for path, off in pool_files:
        lines += tail_of(path, off)
    classes = {}
    targets = [os.path.normpath(p) for p in paths_changed]
    for line in lines:
        if line.get("phase") != "pre":
            continue
        agent = bool(line.get("agent_id")) or (transcript_calls and line.get("tool_use_id") in
                                               transcript_calls)
        tool, inp = line.get("tool"), line.get("input")
        named = []
        if isinstance(inp, dict):
            for k in ("file_path", "path", "notebook_path"):
                if isinstance(inp.get(k), str):
                    named.append(inp[k])
            cmd = inp.get("command")
            if tool in ("Bash", "bash") and isinstance(cmd, str):
                try:
                    parsed = pathscan.unwrap(pathscan.parse(cmd))
                    first = [c["argv"][0] for c in parsed["commands"] if c["argv"]]
                    if any(os.path.basename(a) not in pathscan.READ_ONLY for a in first) or \
                            any(op for c in parsed["commands"] for op, _t in c["redirects"]):
                        found, _h, _p = pathscan._walk(cmd, line.get("cwd") or root, None)
                        named += [p for p, placed, _r in found if placed]
                        named += [t for c in parsed["commands"] for _o, t in c["redirects"]]
                except Exception:  # noqa: BLE001
                    pass
        for n in named:
            n = os.path.normpath(n if os.path.isabs(n) else os.path.join(line.get("cwd") or root, n))
            for t in targets:
                if t == n or under(t, n):
                    prev = classes.get(t)
                    classes[t] = "agent" if (agent or prev == "agent") else "driver"
    return {t: classes.get(t, "unattributed") for t in targets}


# ================================================================ reading a call for the control plane
#: the commands that only read what they are given, besides pathscan.READ_ONLY, when their arguments
#: name no output (fix batch 2, F6; k-replay-findings.md G5)
_SED_IN_PLACE = re.compile(r"-[A-Za-z]*i[A-Za-z0-9._~-]*")
_SED_WRITE_CMD = re.compile(r"(?:^|[;{}\n]|/[gpiIemM0-9]*)\s*[wW]\s+\S")
_AWK_WRITES = re.compile(r"\bprint[f]?\b[^;}\n]*(?:>|\|)|\bsystem\s*\(|\bclose\s*\(|\|\s*getline")
_UNIQ_VALUE_OPTS = ("-f", "-s", "-w", "--skip-fields", "--skip-chars", "--check-chars")


def _sed_writes(args: list) -> bool:
    """True when a `sed` call edits a file in place or has a script that writes one."""
    for a in args:
        if a == "--in-place" or a.startswith("--in-place=") or _SED_IN_PLACE.fullmatch(a):
            return True
        if not a.startswith("-") and _SED_WRITE_CMD.search(a):
            return True
    return False


def reads_only(argv: list) -> bool:
    """True when a command only reads: a word of pathscan.READ_ONLY, or sed without -i or a write
    command, nl, cut, jq, sort without -o, uniq with at most an input argument, awk whose program
    prints to no file and runs no command. A shell redirect is read separately; anything else is
    not read-only, so every path it names counts as written."""
    from alpaca.research import pathscan
    if not argv:
        return True
    base = os.path.basename(argv[0])
    args = argv[1:]
    if base in pathscan.READ_ONLY:
        return True
    if base == "sed":
        return not _sed_writes(args)
    if base in ("nl", "cut", "jq"):
        return True
    if base == "sort":
        return not any(a in ("-o", "--output") or a.startswith("--output=") or a.startswith("-o")
                       or (re.fullmatch(r"-[A-Za-z]+", a) and "o" in a) for a in args)
    if base == "uniq":
        positional, skip = 0, False
        for a in args:
            if skip:
                skip = False
            elif a in _UNIQ_VALUE_OPTS:
                skip = True
            elif not a.startswith("-"):
                positional += 1
        return positional <= 1
    if base in ("awk", "gawk", "mawk"):
        if any(a == "-i" or a.startswith("--inplace") or a == "inplace" for a in args):
            return False
        return not any(_AWK_WRITES.search(a) for a in args)
    return False


#: words after the leading cd that may move the shell, run text as shell code or change how a
#: cd resolves; any of them makes _folders keep the call's start folder for every command
_MOVERS = frozenset(("cd", "pushd", "popd", "eval", "source", ".", "exec", "trap", "alias",
                     "unalias", "enable", "shopt", "set", "function", "builtin", "command",
                     "if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done",
                     "case", "esac", "select", "coproc"))
_MOVE_VARS = ("CDPATH", "BASH_ENV", "ENV", "OLDPWD", "PWD", "PROMPT_COMMAND")
_LEAD = re.compile(r"[ \t]*(?:mkdir[ \t]+-p[ \t]+(/[A-Za-z0-9._@+,=/-]*)[ \t]*&&[ \t]*)?"
                   r"cd[ \t]+(/[A-Za-z0-9._@+,=/-]*)[ \t]*&&")
#: a command word of the chain that the mover check can trust: a plain name or absolute path
_PLAIN_WORD = re.compile(r"^(?:/[A-Za-z0-9._+-]+)*/?[A-Za-z0-9_][A-Za-z0-9._+-]*$")
#: bash builtins and keywords (bash 5.x): a chain command named by one runs inside the shell and
#: may move it (mapfile -C and readarray -C run a callback string, for one), so only the few
#: below that cannot move it are trusted; any other chain command must be a program on PATH
_BASH_BUILTINS = frozenset((
    ".", ":", "[", "[[", "]]", "!", "{", "}", "alias", "bg", "bind", "break", "builtin", "caller",
    "case", "cd", "command", "compgen", "complete", "compopt", "continue", "coproc", "declare",
    "dirs", "disown", "do", "done", "echo", "elif", "else", "enable", "esac", "eval", "exec",
    "exit", "export", "false", "fc", "fg", "fi", "for", "function", "getopts", "hash", "help",
    "history", "if", "in", "jobs", "kill", "let", "local", "logout", "mapfile", "popd", "printf",
    "pushd", "pwd", "read", "readarray", "readonly", "return", "select", "set", "shift", "shopt",
    "source", "suspend", "test", "then", "time", "times", "trap", "true", "type", "typeset",
    "ulimit", "umask", "unalias", "unset", "until", "wait", "while"))
_SAFE_BUILTINS = frozenset(("echo", "true", "false", "pwd"))
#: programs that write only the files they name and make no link: a chain command after one of
#: these alone keeps the cd folder (_folders)
_NO_LINK = frozenset(("mkdir", "cat", "echo", "true", "false", "pwd", "grep", "head", "tail",
                      "sort", "wc", "tee", "sed", "ls", "diff", "cmp", "stat", "tr", "cut",
                      "uniq", "touch"))


def _skeleton(text: str) -> str | None:
    """The call text with every quoted string replaced by `Q` and every heredoc body dropped, so
    the shell's own operators and words can be read; a comment is dropped to its line end. None
    for a text it cannot read: an open quote or heredoc, a backslash-newline, a backquote, and
    any `$` outside single quotes or in the body of a heredoc whose end word is not quoted.
    Every expansion is refused because bash 5.3 runs `${ cmd; }` and `${| cmd; }` in the
    current shell, inside double quotes and heredoc bodies too, so a cd there moves the shell
    (confirm-final-r5.md); `${` is refused even inside single quotes, where an array subscript
    of a builtin may still expand it."""
    if "${" in text:
        return None
    # only the characters both readers agree on: bash keeps a carriage return or any other
    # control or non-ASCII character in a word, where pathscan may read it as a blank
    # (confirm-final-r6.md)
    if any(not (" " <= ch <= "~" or ch in "\t\n") for ch in text):
        return None
    out, i, n, pending = [], 0, len(text), []
    while i < n:
        c = text[i]
        if c == "\\":
            if i + 1 < n and text[i + 1] == "\n":
                return None
            out.append("Q")
            i += 2
            continue
        if c == "'":
            j = text.find("'", i + 1)
            if j < 0:
                return None
            out.append("Q")
            i = j + 1
            continue
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\":
                    j += 1
                elif text[j] in "`$":
                    return None
                j += 1
            if j >= n:
                return None
            out.append("Q")
            i = j + 1
            continue
        if c in "`$" or text.startswith("<(", i) or text.startswith(">(", i):
            return None
        if c == "#" and (not out or out[-1] in " \t\n;&|"):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("<<", i) and not text.startswith("<<<", i):
            m = re.match(r"<<-?\s*(['\"]?)([A-Za-z0-9_]+)\1", text[i:])
            if not m:
                return None
            pending.append((m.group(2), text[i + 2:i + 3] == "-", bool(m.group(1))))
            out.append("<<H")
            i += m.end()
            continue
        if c == "\n" and pending:
            out.append("\n")
            i += 1
            for word, strip, quoted in pending:
                while True:
                    j = text.find("\n", i)
                    line = text[i:] if j < 0 else text[i:j]
                    if (line.lstrip("\t") if strip else line) == word:
                        i = n if j < 0 else j + 1
                        break
                    if j < 0:
                        return None
                    if not quoted and any(ch in line for ch in "`$\\"):
                        return None             # the body of an unquoted heredoc is expanded
                    i = j + 1
                # an empty body and its end line, so the skeleton parses as the same commands
                out.append("H\n")
            pending = []
            continue
        out.append(c)
        i += 1
    return None if pending else "".join(out)


def _folders(text: str, parsed: dict, here: str | None) -> list:
    """The folders of `_folders_why`, without its notes."""
    return _folders_why(text, parsed, here)[0]


def _cd_folder_of(cmds: list) -> str | None:
    """The literal absolute folder of the cd that begins the call (`cd /abs`, or `mkdir -p /abs &&
    cd /abs`), as the shell reads it, or None. Used only to word a deny text (cd_chain_note)."""
    for k in (0, 1):
        if k < len(cmds):
            argv = [str(a) for a in cmds[k]["argv"]]
            if k == 1 and not (cmds[0]["argv"][:1] == ["mkdir"]):
                return None
            if len(argv) == 2 and argv[0] == "cd" and argv[1].startswith("/"):
                return os.path.normpath(argv[1])
    return None


def _word_of(name) -> str:
    """A program name worded for a deny text: itself when it is a plain short name."""
    base = os.path.basename(str(name))
    return base if re.fullmatch(r"[A-Za-z0-9._+-]{1,32}", base) else ""


def _folders_why(text: str, parsed: dict, here: str | None) -> tuple:
    """(folders, note): [frozenset of folders] per command of `parsed`
    (pathscan.unwrap(pathscan.parse(text))): the folders the shell may be in when that command
    runs, the call starting in `here`. `note` is {"folder": the literal folder of the leading cd
    or None, "why": [per command: None when the command is placed in the cd folder alone or has no
    cd, else the construct that made the reader keep the start folder for it]}; it words the deny
    text of the guard and changes nothing the reader accepts (t-351 F48).

    One form is followed (in-session-runtime.md section 7: a cd moves only inside one call): a
    call that begins `cd /abs &&`, or `mkdir -p /abs && cd /abs &&`, with a literal absolute
    folder. The `&&` chain after that cd, up to its first `;`, `||`, `&`, `|&` or line break,
    runs only when the cd succeeded; when nothing in the chain can move the shell (a cd, pushd,
    popd, eval, source, exec, a function or alias, a compound command or group, CDPATH, OLDPWD,
    ...), its commands run in that folder (its realpath). Every command after the chain may run
    after a failed cd or after a later move, so it is read in both the start folder and the cd
    folder, which keeps the start-folder reading the scan had before. Any other call, and a text
    the skeleton reader cannot read, keeps the start folder for every command (op-009 t-248;
    check-adversarial-r1.md: a cd follower that models less than the shell does opens holes)."""
    cmds = parsed.get("commands") or []
    start = [frozenset([here])] * len(cmds)
    has_cd = any(c["argv"][:1] == ["cd"] for c in cmds)
    cd_folder = _cd_folder_of(cmds) if has_cd else None

    def keep(why):
        """The start folder for every command, and the construct that made the reader keep it."""
        return start, {"folder": cd_folder, "why": [why] * len(cmds)}
    skel = _skeleton(text) if has_cd else None
    m = _LEAD.match(skel or "")
    if not m:
        if not has_cd:
            return keep(None)
        if skel is None:
            return keep("a `$` in the call" if "$" in text else "a backquote in the call"
                        if "`" in text else "a quote, heredoc or character the reader does not "
                        "follow")
        return keep("a cd that is not a plain `cd /folder &&` at the start of the call")
    lead = 2 if m.group(1) else 1
    if [c["argv"] for c in cmds[:lead]][-1:] != [["cd", m.group(2)]] or \
            (lead == 2 and cmds[0]["argv"][:1] != ["mkdir"]):
        return keep("a cd that is not a plain `cd /folder &&` at the start of the call")
    rest = skel[m.end():]
    cut = re.search(r"\|\||;|\n|\|&|(?<![&>])&(?![&>])", rest)
    chain = rest if cut is None else rest[:cut.start()]
    # an unquoted glob or tilde is expanded by bash to a name the scan cannot resolve, so a link
    # in the cd folder would be missed by the realpath check (confirm-final-r7.md)
    if re.search(r"[*?\[\]~]", chain):
        return keep("a glob or tilde in the chain")
    words = re.findall(r"[^\s;&|()<>{}]+|[(){}]", chain)
    if any(w in _MOVERS or w in "(){}" or w.endswith("()")
           or any(w.startswith(v + "=") or ("$" + v) in w for v in _MOVE_VARS) for w in words):
        mover = next((w for w in words if w in _MOVERS or w in "(){}" or w.endswith("()")
                      or any(w.startswith(v + "=") or ("$" + v) in w for v in _MOVE_VARS)), "")
        return keep("a second `cd`" if mover == "cd" else "a group, subshell or function"
                    if mover in "(){}" or mover.endswith("()") else "a `%s`" % mover
                    if mover in _MOVERS else "a variable that moves the shell")
    if any(part in (".", "..") for part in m.group(2).split("/")):
        return keep("a `.` or `..` in the cd folder")
    try:
        # the skeleton must read as the same commands as the real text, so the count of its
        # prefix is the count of the chain's commands in `cmds`
        if len(_parsed_commands(skel)) != len(cmds):
            return keep("text the reader does not follow")
        end = m.end() + (len(rest) if cut is None else cut.start())
        n_chain = len(_parsed_commands(skel[:end]))
    except Exception:  # noqa: BLE001 - a prefix the reader cannot parse keeps the start folder
        return keep("text the reader does not follow")
    if not lead <= n_chain <= len(cmds):
        return keep("text the reader does not follow")
    # the chain's commands as the shell reads them (quotes and escapes removed): a mover spelled
    # with a backslash, quotes or $'...', or a command word built from a variable, gives up
    for c in cmds[lead:n_chain]:
        argv = [str(a) for a in c["argv"]]
        if not argv or not _PLAIN_WORD.match(argv[0]) or \
                any(os.path.basename(a) in _MOVERS for a in argv) or \
                any(a.startswith(v + "=") for a in argv for v in _MOVE_VARS):
            return keep("a command word the reader cannot place")
        word = argv[0]
        if word in _BASH_BUILTINS:
            if word not in _SAFE_BUILTINS:
                return keep("a command word the reader cannot place")
        elif not (os.path.isabs(word) and os.access(word, os.X_OK)) and \
                not shutil.which(word):
            # a name that is no program on PATH may be a function of the shell's profile
            return keep("a command word the reader cannot place")
    folder = os.path.realpath(m.group(2))
    # control_hits resolves links that exist when it reads the call; a link that an earlier
    # command of the same chain makes (ln -s, cp -s, python3 os.symlink, mv of the folder) does
    # not exist yet, so a command is placed in the cd folder alone only while every chain
    # command before it is one that makes no link and moves nothing (confirm-final-r8.md)
    trusted = n_chain
    for k in range(lead, n_chain):
        if os.path.basename(str(cmds[k]["argv"][0])) not in _NO_LINK:
            trusted = k + 1             # its own writes run before any link it makes
            break
    folders = [frozenset([here]) if k < lead else frozenset([folder]) if k < trusted
               else frozenset([here, folder]) for k in range(len(cmds))]
    # the construct that put a command in the start folder as well: the program that came before
    # it in the chain (it may make a link), or the operator that ended the chain
    in_chain = ("a `%s` call" % _word_of(cmds[trusted - 1]["argv"][0])
                if _word_of(cmds[trusted - 1]["argv"][0]) else "a call that may make a link") \
        if lead < trusted <= n_chain else None
    ended = {"||": "a `||`", ";": "a `;`", "\n": "a line break", "|&": "a `|&`",
             "&": "a `&`"}.get(cut.group(0) if cut else "", "text after the chain")
    why = [None if k < trusted else in_chain if k < n_chain else ended for k in range(len(cmds))]
    return folders, {"folder": cd_folder, "why": why}


def _parsed_commands(text: str) -> list:
    from alpaca.research import pathscan
    return pathscan.unwrap(pathscan.parse(text)).get("commands") or []


def _write_words(cmd: dict) -> list:
    """The words one command writes: the target of each output redirect (not `&N` or /dev/null),
    and the arguments of a write command or of a `sed` that edits in place."""
    argv = cmd["argv"]
    base = os.path.basename(argv[0]) if argv else ""
    out = [target for op, target in cmd["redirects"]
           if ">" in op and not str(target).startswith("&") and target != "/dev/null"]
    if base in WRITE_COMMANDS or (base == "sed" and _sed_writes(argv[1:])):
        out += [a for a in argv[1:] if not a.startswith("-")]
    return out


# ---------------------------------------------------------------- t-354: read exactly, then deny
# In-session-runtime.md section 27.1 (rules U1 to U10). The reader first reads what bash makes
# certain (U2, U3, U10), then a write it still cannot place is a hit (U1), every word of a command
# that may write is a name it may write (U4), program text the call carries is read the same way
# (U5), and the shapes of U6 and U7 are read where the placement is exact.
#: the words that put the command after them in the same position (`do cp a b`, `then rm x`)
_LEAD_KEYWORDS = ("do", "then", "else", "elif", "!", "time", "{", "(", "${")
#: `$NAME` and `${NAME}`, the two forms U2 substitutes
_VAR_WORD = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
_URL_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>()]*")
#: a name token of U4: the characters a file name is written with in a command or in code
_NAME_TOKEN = re.compile(r"[A-Za-z0-9._~+@%/*?\[\]-]+")
_GLOB_CHARS = re.compile(r"[*?\[]")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: the parse flags under which an assignment may be scoped or rewritten: U2 reads none then
_SCOPING_FLAGS = ("subshell", "brace-group", "command-substitution", "process-substitution",
                  "eval")
#: commands that run text in the current shell, where they may set any variable
_SHELL_SETTERS = ("source", ".", "eval")


def _lead_stripped(argv: list) -> list:
    """`argv` without the keywords before its command (`do cp a b` is `cp a b`)."""
    argv = list(argv)
    while argv and argv[0] in _LEAD_KEYWORDS:
        argv = argv[1:]
    return argv


def _literal_assigns(cmds: list, flags) -> list:
    """U2: [{NAME: value} per command of `cmds`]: the names a command may read exactly, each set
    before it by a command that is only `NAME=value`, made once in the call, with a non-empty value
    that holds no expansion. A name that any other word of the call writes or names (`read NAME`,
    `export NAME=x`, `for NAME in`, `printf -v NAME`, `NAME=x cmd`) is left out, and none is
    read under a scoping flag (_SCOPING_FLAGS) or when a command runs text in this shell."""
    known = [dict() for _c in cmds]
    if set(flags or ()) & set(_SCOPING_FLAGS):
        return known
    sites, uses = {}, {}
    for k, cmd in enumerate(cmds):
        argv = cmd["argv"]
        if argv and argv[0] in _SHELL_SETTERS:
            return known
        lone = bool(cmd.get("assign")) and len(argv) == 1 and not (cmd.get("live") or [[]])[0]
        for i, w in enumerate(argv):
            if lone and i == 0:
                name, _eq, value = w.partition("=")
                sites.setdefault(name, []).append((k, value))
                continue
            for m in re.finditer(r"(?<![\w${])([A-Za-z_][A-Za-z0-9_]*)", str(w)):
                uses[m.group(1)] = uses.get(m.group(1), 0) + 1
    for name, got in sites.items():
        if len(got) != 1 or uses.get(name):
            continue
        k, value = got[0]
        if not value or _VAR_WORD.search(value) or "$" in value or "`" in value:
            continue
        for j in range(k + 1, len(cmds)):
            known[j][name] = value
    return known


def _expand_known(word: str, known: dict, folder: str | None) -> str:
    """U2: `word` with each `$NAME`/`${NAME}` of `known` put in, `$PWD`/`${PWD}` as `folder` when
    the reader knows the one folder, and `$HOME` as the home folder (the reader already reads `~`
    so, pathscan._place). Any other expansion is left as written."""
    def put(m):
        name = m.group(1) or m.group(2)
        if name in known:
            return known[name]
        if name == "PWD" and folder:
            return folder
        if name == "HOME":
            return os.path.expanduser("~")
        return m.group(0)
    return _VAR_WORD.sub(put, str(word))


_CD_FAILED = "cd: %s: "


def _cd_trusted(folder: str | None, words: list, call: dict) -> bool:
    """U3 with M2: True when the reader may take `folder`, reached by literal cds over `words`, as
    the folder the shell was in. The folder exists now and may be searched (a cd into it can
    succeed); or the call ran (it carries its recorded output) and that output holds no bash `cd:`
    failure for any of the words. The live guard's call has no output, so the guard reads the
    disk only."""
    if folder is None:
        return False
    if os.path.isdir(folder):
        return os.access(folder, os.X_OK) or isinstance(call.get("output"), str) and not any(
            (_CD_FAILED % w) in call["output"] for w in words)
    output = call.get("output")
    if not isinstance(output, str):
        return False
    return not any((_CD_FAILED % w) in output for w in words)


def _walk_folders(cmds: list, here: str | None, known: list, call: dict, flags) -> list:
    """U3: [(folder, trusted)] per command of `cmds`: the folder the shell is in when it runs, each
    literal `cd` followed with U2 put into its word (pathscan._cd), and whether the reader may
    take that folder alone (_cd_trusted). Under a subshell or brace-group flag a cd may be scoped
    to its group, so no folder past a cd is taken alone."""
    from alpaca.research import pathscan
    scoped = bool(set(flags or ()) & {"subshell", "brace-group"})
    out, wh, wp, words, moved = [], here, None, [], False
    for k, cmd in enumerate(cmds):
        out.append((wh, (not moved or (not scoped and _cd_trusted(wh, words, call)))
                    and wh is not None))
        argv = _lead_stripped(cmd["argv"])
        if argv and argv[0] == "cd":
            argv = [argv[0]] + [_expand_known(a, known[k], None) for a in argv[1:]]
            wh, wp, target, _placed = pathscan._cd(argv, wh, wp)
            # bash names the word as given in its `cd: WORD: ...` failure line
            words += [str(a) for a in argv[1:]] + [str(target)]
            moved = True
    return out


def _name_tokens(text: str) -> list:
    """U4: the name tokens of a word or of program text: runs of the characters a file name is
    written with, a URL, an option word (`-x`, `--opt`), a glob and a `$NAME` left out; an
    `--opt=value` gives `value`."""
    text = _URL_WORD.sub(" ", str(text))
    out = []
    for m in _NAME_TOKEN.finditer(text):
        tok, start = m.group(0), m.start()
        if (start > 0 and text[start - 1] == "$") or text[max(0, start - 2):start] == "${":
            continue
        if tok.startswith("-") or _GLOB_CHARS.search(tok):
            continue
        out.append(tok)
    return out


def _globbed(raw: str) -> list:
    """U7d: the entries on disk a glob write word names now, folder parts included."""
    out = []
    try:
        out += [os.path.normpath(m) for m in glob.glob(raw)]
        head, tail = os.path.split(raw)
        if _GLOB_CHARS.search(head):
            out += [os.path.normpath(os.path.join(m, tail)) for m in glob.glob(head)]
    except (OSError, ValueError, re.error):
        pass
    return out


def cd_chain_note(call: dict, *, here: str, hit_why: str, own: tuple = ()) -> str | None:
    """The sentence a deny text adds when the hit (`writes P` or `names P`) is a relative write
    that the cd reader placed in the start folder `here` because of a construct before it in the
    cd chain (t-351 F48), or None.
    It names the construct and the same file by its absolute path in the cd folder, which the
    reader does follow. Only a cd folder inside `own` (the folders the job may write: its scratch
    folder and write paths, never its read-only cwd; section 27.1, M3) is offered. A pure
    function of the call, `here` and the hit: the accepted set of `_folders` is not touched."""
    got = re.fullmatch(r"(?:writes|names) (.+)", str(hit_why))
    if not got:
        return None
    from alpaca.research import pathscan
    text = pathscan.shell_text(call)
    if text is None:
        return None
    try:
        parsed = pathscan.unwrap(pathscan.parse(text))
        folders, note = _folders_why(text, parsed, here)
    except Exception:  # noqa: BLE001 - no note
        return None
    cd = note["folder"]
    if not cd or not any(under(os.path.realpath(cd), os.path.realpath(o)) for o in own if o):
        return None
    hit = os.path.normpath(got.group(1))
    for k, cmd in enumerate(parsed.get("commands") or []):
        construct = note["why"][k]
        if not construct or here not in folders[k]:
            continue
        for word in _write_words(cmd):
            word = str(word)
            if os.path.isabs(word):
                continue
            raw, ok = pathscan._place(word, here, None)
            if ok and os.path.normpath(raw) == hit:
                return ("The relative name was read in %s, where the call starts, because %s came "
                        "before it in the cd chain; the same file by its absolute path is %s."
                        % (here, construct, os.path.normpath(os.path.join(cd, word))))
    return None


def _call_paths(call: dict, here: str | None) -> list:
    """[(path, placed, writes)] the paths a call names, resolved against `here` (the shell base of
    the call), and whether the call writes each: a Write, Edit or NotebookEdit call writes the
    path it names; a shell call writes the target of a redirect and the arguments of a write
    command, and names (any command word outside READ_ONLY) the paths it holds. A write target
    is placed in each folder its command may run in (_folders), so `cd S && cat > x.py` writes
    S/x.py (op-009 t-248); the words of echo, printf and the other pathscan.NAMES_NOTHING
    commands are read as before."""
    from alpaca.research import pathscan
    out = []
    kind = call.get("kind")
    if kind in ("read", "write", "other") and not pathscan.shell_text(call):
        for path, access, placed, _raw in pathscan._tool_paths(call, here):
            out.append((path, placed, kind == "write"))
        return out
    text = pathscan.shell_text(call)
    if text is None:
        return out
    try:
        parsed = pathscan.unwrap(pathscan.parse(text))
    except Exception:  # noqa: BLE001 - a text the reader cannot parse names nothing it can place
        return out
    found, _h, _p = pathscan._walk(text, here, None)
    placed_paths = [(p, placed) for p, placed, _raw in found]
    writing = set()
    named_by_non_read = False
    folders = _folders(text, parsed, here)

    def write(word, where):
        for folder in where:
            raw, ok = pathscan._place(str(word), folder, None)
            if ok:
                writing.add(os.path.normpath(raw))
    for k, cmd in enumerate(parsed["commands"]):
        argv = cmd["argv"]
        base = os.path.basename(argv[0]) if argv else ""
        for target in _write_words(cmd):
            write(target, folders[k])
        if base and not reads_only(argv):
            named_by_non_read = True
    for p, placed in placed_paths:
        w = placed and (os.path.normpath(p) in writing or named_by_non_read)
        out.append((p, placed, bool(w)))
    for p in writing:
        if not any(os.path.normpath(q) == p for q, _pl, _w in out):
            out.append((p, True, True))
    return out


#: how many substitutions inside substitutions the reader opens (U11); a deeper one is a hit
SUBSTITUTION_DEPTH = 4


def _group_end(text: str, i: int) -> int | None:
    """The index of the `)` that closes the group whose body starts at `i`, reading quotes and
    backslashes as bash does inside a substitution, or None when it does not close."""
    depth, quote = 1, None
    while i < len(text):
        c = text[i]
        if quote == "'":
            quote = None if c == "'" else quote
        elif c == "\\":
            i += 1
        elif quote == '"':
            quote = None if c == '"' else quote
        elif c in "'\"":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _substitution_at(text: str, i: int) -> tuple:
    """(body, kind, end) of the substitution that starts at `text[i]`, or (None, None, i + 1):
    kind is `command` for `$(...)` and a backquote pair, `process` for `<(...)` and `>(...)` and
    `arithmetic` for `$((...))`. A pair that does not close runs to the end of the text."""
    if text[i] == "`":
        k = i + 1
        while k < len(text) and text[k] != "`":
            k += 2 if text[k] == "\\" else 1
        return text[i + 1:k], "command", k + 1
    two = text[i:i + 2]
    if two not in ("$(", "<(", ">("):
        return None, None, i + 1
    if text.startswith("$((", i):
        end = _group_end(text, i + 3)
        if end is not None and text[end + 1:end + 2] == ")":
            return text[i + 3:end], "arithmetic", end + 2
    end = _group_end(text, i + 2)
    stop = len(text) if end is None else end
    return text[i + 2:stop], "command" if two == "$(" else "process", stop + 1


def _substitutions_in_text(text: str) -> list:
    """The command text of every substitution in a text bash expands as it would a double-quoted
    word (an unquoted heredoc body, an arithmetic expression): `$(...)` and backquotes."""
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "`" or text.startswith("$(", i):
            body, kind, i = _substitution_at(text, i)
            out += _substitutions_in_text(body) if kind == "arithmetic" else [body]
            continue
        i += 1
    return out


def substitution_bodies(call: dict, here: str | None) -> list:
    """U11 of section 27.1: [(text, folder)] for every command text the call's shell runs inside
    a substitution, each with a folder the enclosing command may run in. A `$(...)`, a backquote,
    a `<(...)` or a `>(...)` that starts at a live position of a word or redirect target (parse's
    `live` and `rlive`, so a quoted one is not read) and every `$(...)` or backquote in an
    unquoted heredoc body; an arithmetic `$((...))` gives the substitutions inside it. Each text
    is read by control_hits as a call of its own, so no substitution is dropped unread."""
    from alpaca.research import pathscan
    text = pathscan.shell_text(call)
    if text is None:
        return []
    try:
        parsed = pathscan.unwrap(pathscan.parse(text))
        folders = _folders(text, parsed, here)
    except Exception:  # noqa: BLE001 - control_hits has made it a hit (M1)
        return []
    out = []
    for k, cmd in enumerate(parsed.get("commands") or []):
        where = folders[k] if k < len(folders) else {here}
        words = list(zip(cmd["argv"], cmd.get("live") or [None] * len(cmd["argv"])))
        words += [(t, rl) for (op, t), rl in zip(cmd["redirects"], cmd.get("rlive")
                                                  or [None] * len(cmd["redirects"]))]
        texts = []
        for word, offsets in words:
            word = str(word)
            for off in offsets or ():
                if off >= len(word):
                    continue
                body, kind, _end = _substitution_at(word, off)
                if kind == "arithmetic":
                    texts += _substitutions_in_text(body)
                elif kind:
                    texts.append(body)
        for i in cmd["heredocs"]:
            doc = parsed["heredocs"][i]
            if not doc.get("quoted"):
                texts += _substitutions_in_text(doc.get("body") or "")
        out += [(t, folder) for t in texts for folder in sorted(where, key=str)]
    return out


def _is_process_substitution(word: str) -> bool:
    """True when the word is one `<(...)` or `>(...)` and nothing else: bash passes a pipe for
    it, so a write to it writes the pipe and the body is read as a call (U11)."""
    if word[:2] not in ("<(", ">("):
        return False
    return _group_end(word, 2) == len(word) - 1


def _holds_substitution(word: str) -> bool:
    """True when a written word holds a substitution of any kind, so the output of a command
    names the file (U1). A quoted one is read as one too: deny by default."""
    return any(word[i] == "`" or word[i:i + 2] in ("$(", "<(", ">(") for i in range(len(word)))


def _unread_entries(call: dict, here: str | None) -> list:
    """[(path, placed, kind, shown, program, rule)]: what a shell call may write that `_call_paths`
    does not read (in-session-runtime.md section 27.1). `kind` is `write` (a written path, placed
    or not: U1, U2, U4, U5, U7d), `read` (a path a read-only command names through a variable U2
    reads), `ancestor` (U7a), `newtop` (U7c) or `unseen` (U6, U7b: a folder `program` writes
    names into that the call does not show). `shown` is the word as written when U2 changed it;
    `rule` words the guard's deny text (`unplaced`, `words-here` for a name read from the words or
    code of a command in the start folder, else `path`). A text the parse refuses gives nothing
    here: control_hits reads it as a hit first (M1)."""
    from alpaca.research import pathscan
    text = pathscan.shell_text(call)
    if text is None:
        return []
    try:
        parsed = pathscan.unwrap(pathscan.parse(text))
    except Exception:  # noqa: BLE001 - control_hits has made it a hit (M1)
        return []
    flags = parsed.get("flags") or ()
    cmds = parsed.get("commands") or []
    out = []

    # the write words of each command, a literal for loop read once per word (U1, U2, U7)
    rolled = pathscan.unroll(parsed)
    if rolled.get("error"):
        rolled = parsed
    rfolders, rnote = _folders_why(text, rolled, here)
    rknown = _literal_assigns(rolled["commands"], flags)
    for k, cmd in enumerate(rolled["commands"]):
        argv = _lead_stripped(cmd["argv"])
        stripped = len(argv) != len(cmd["argv"])
        cmd = dict(cmd, argv=argv)
        where = rfolders[k] if k < len(rfolders) else {here}
        sure = k < len(rnote["why"]) and rnote["why"][k] is None
        targets = {str(t) for op, t in cmd["redirects"] if ">" in op}
        prog = os.path.basename(argv[0]) if argv else ""
        for word in _write_words(cmd):
            word = str(word)
            for folder in where:
                w2 = _expand_known(word, rknown[k], folder if len(where) == 1 else None)
                if _is_process_substitution(w2):
                    continue                    # a pipe: its body is read as a call (U11)
                raw, ok = pathscan._place(w2, folder, None)
                if not ok or _holds_substitution(w2):
                    out.append((word, False, "write", None, prog, "unplaced"))
                    continue
                path = os.path.normpath(raw)
                shown = word if w2 != word else None
                if shown or stripped or rolled is not parsed:
                    out.append((path, True, "write", shown, prog, "path"))
                if _GLOB_CHARS.search(w2):
                    out += [(m, True, "write", shown, prog, "path") for m in _globbed(raw)]
                if os.path.isabs(w2) or sure:
                    out.append((path, True, "ancestor", shown, prog, "path"))
                    if word not in targets:
                        out.append((path, True, "newtop", shown, prog, "path"))
                    if os.path.isdir(raw):
                        out.append((path, True, "unseen", shown, prog, "path"))

    # the words and the program text of each command (U2, U3, U4, U5, U6)
    folders = _folders(text, parsed, here)
    known = _literal_assigns(cmds, flags)
    walk = _walk_folders(cmds, here, known, call, flags)
    docs = {}
    piped = "|" in text.replace("||", "")
    for k, cmd in enumerate(cmds):
        argv = _lead_stripped(cmd["argv"])
        prog = os.path.basename(argv[0]) if argv else ""
        where = folders[k] if k < len(folders) else {here}
        wk, trusted = walk[k]
        places = {wk} if trusted else (set(where) | ({wk} - {None}))
        writes = bool(argv) and not reads_only(argv)
        for w in list(argv[1:]) + [str(t) for op, t in cmd["redirects"] if not op.endswith("<<<")]:
            if "$" not in w:
                continue
            for folder in places:
                w2 = _expand_known(w, known[k], folder if len(places) == 1 else None)
                if w2 == w or "$" in w2:
                    continue
                raw, ok = pathscan._place(w2, folder, None)
                if ok:
                    out.append((os.path.normpath(raw), True, "write" if writes else "read", w,
                                prog, "path"))
        if cmd["heredocs"]:
            body = " ".join(parsed["heredocs"][i].get("body") or "" for i in cmd["heredocs"])
            for op, t in cmd["redirects"]:
                if ">" in op and not str(t).startswith("&"):
                    for folder in places:
                        raw, ok = pathscan._place(_expand_known(str(t), known[k], folder),
                                                  folder, None)
                        if ok:
                            docs[os.path.normpath(raw)] = body
        if writes and not (cmd.get("assign") and len(argv) == 1) \
                and pathscan.rerun_args(argv) is None:
            texts = list(argv[1:])
            for w in argv[1:]:
                for folder in places:
                    raw, ok = pathscan._place(_expand_known(w, known[k], folder), folder, None)
                    if ok and os.path.normpath(raw) in docs:
                        texts.append(docs[os.path.normpath(raw)])
            if piped and k > 0:
                # words that may reach this command's stdin on a pipe (`echo x.py | xargs touch`)
                for before in cmds[:k]:
                    texts += [str(a) for a in before["argv"][1:]]
            if prog not in WRITE_COMMANDS:
                # a tee heredoc is data; any other command's heredoc or here-string is its input
                texts += [parsed["heredocs"][i].get("body") or "" for i in cmd["heredocs"]]
                texts += [str(t) for op, t in cmd["redirects"] if op.endswith("<<<")]
            for t in texts:
                for tok in _name_tokens(t):
                    for folder in places:
                        raw, ok = pathscan._place(tok, folder, None)
                        if not ok:
                            continue
                        rule = "words-here" if folder == here and not os.path.isabs(tok) \
                            and not tok.startswith("~") else "path"
                        out.append((os.path.normpath(raw), True, "write", None, prog, rule))
                        if "/" in tok and tok.strip("/"):
                            out.append((os.path.normpath(raw), True, "ancestor", None, prog,
                                        "path"))
        if prog in pathscan.UNSEEN_WRITERS:
            for folder in where:
                if folder:
                    out.append((folder, True, "unseen", None, prog, "path"))
            for w in argv[1:]:
                cands = [w] + _name_tokens(w)
                if w.startswith("-") and not w.startswith("--") and len(w) > 2:
                    cands.append(w[2:])         # a folder glued to its letter: `-C/abs`
                for c in cands:
                    if c.startswith("-"):
                        continue
                    for folder in where:
                        raw, ok = pathscan._place(_expand_known(c, known[k], None), folder, None)
                        if ok and os.path.isdir(raw):
                            out.append((os.path.normpath(raw), True, "unseen", None, prog,
                                        "path"))
    return out


def unparsed_hit(call: dict) -> str | None:
    """M1 of section 27.1: the reason a shell call's text cannot be read, or None. A text the parse
    refuses or reads with an `error` (its own or that of the text a shell wrapper runs) is a hit:
    the reader cannot say what it writes. A pure function of the call text."""
    from alpaca.research import pathscan
    text = pathscan.shell_text(call)
    if text is None:
        return None
    try:
        parsed = pathscan.parse(text)
        error = parsed.get("error") or pathscan.unwrap(parsed).get("error")
    except Exception as exc:  # noqa: BLE001 - the reason is the parse failure itself
        error = str(exc) or type(exc).__name__
    return "cannot be parsed (%s)" % error if error else None


def control_name_hit(call: dict) -> str | None:
    """The CONTROL_NAMES entry a call runs, or None: as the command word of a shell call, in the
    text of an interpreter's -c payload or its heredoc, or as the module of a -m argument; never
    in the content of a file a call writes, so a pack whose own code says `sqlite3` or
    `from alpaca` is not denied."""
    from alpaca.research import pathscan
    text = pathscan.shell_text(call)
    if text is None:
        return None
    try:
        parsed = pathscan.parse(text)
    except Exception:  # noqa: BLE001
        return None
    queue = [parsed]
    unwrapped = pathscan.unwrap(parsed)
    if unwrapped is not parsed:
        queue.append(unwrapped)
    for got in queue:
        for cmd in got["commands"]:
            argv = cmd["argv"]
            if not argv:
                continue
            if pathscan.rerun_args(argv) is not None:
                # the verifier's request client: the text it carries is a command the DRIVER runs
                # in the rerun jail, not one this shell runs
                continue
            base = os.path.basename(argv[0])
            if base in ("sudo", "sqlite3"):
                return base
            if base == "alpaca" or argv[0].endswith("bin/alpaca"):
                return "bin/alpaca"
            joined = " ".join(argv)
            if "alpaca research" in joined or "alpaca proof" in joined \
                    or " -m alpaca" in " " + joined:
                return "-m alpaca" if "-m alpaca" in joined else \
                    "alpaca proof" if "alpaca proof" in joined else "alpaca research"
            if base.startswith(INTERPRETERS) or base in INTERPRETERS:
                payloads = []
                for i, a in enumerate(argv):
                    if a in ("-c", "-lc", "-ic", "-lic", "-e") and i + 1 < len(argv):
                        payloads.append(argv[i + 1])
                for idx in cmd["heredocs"]:
                    payloads.append(got["heredocs"][idx]["body"])
                for body in payloads:
                    for name in ("from alpaca", "import alpaca", "alpaca.db", "alpaca.hooks",
                                 "alpaca.research", "alpaca.observability", "sqlite3", "sudo",
                                 "bin/alpaca", "-m alpaca"):
                        if name in body:
                            return name
    return None


#: the `alpaca research` verbs that drive a run (the driving session runs them; a subagent never does)
DRIVING_VERBS = ("start", "resume", "extend", "stop", "next-job", "submit")
_DRIVING_TEXT = re.compile(
    r"\balpaca\b[^\n]{0,160}?\b(?:research\W{1,8}(?:%s)|proof\W{1,8}verify)(?![\w-])"
    % "|".join(re.escape(v) for v in DRIVING_VERBS))
#: the verb words alone, for a text that runs the package through runpy (the name comes after the verb)
_VERB_WORDS = re.compile(
    r"\b(?:research\W{1,8}(?:%s)|proof\W{1,8}verify)(?![\w-])"
    % "|".join(re.escape(v) for v in DRIVING_VERBS))
_ALPACA_MODULES = ("alpaca", "alpaca.cli", "alpaca.__main__")
_SHELLS = ("bash", "sh", "zsh", "dash", "ksh")
#: a path-shaped run of a text the reader does not parse: word characters, `.`, `/` and `-`
_PATH_RUN = re.compile(r"[\w./-]+")


def _verb_after_entry(words: list) -> str | None:
    """The driving verb named by the words that follow an alpaca entry point: the global options
    are skipped (`--session` takes a value), then `research <verb>` or `proof verify`."""
    pos, skip = [], False
    for w in words:
        if skip:
            skip = False
            continue
        if len(w) >= 3 and "--session".startswith(w):
            # `--session` takes a value, and argparse accepts any unique prefix of it (`--s`,
            # `--sess`); `--session=X` carries its value and falls to the option skip below
            skip = True
            continue
        if w.startswith("-"):
            continue
        pos.append(w)
        if len(pos) == 2:
            break
    if len(pos) == 2 and ((pos[0] == "research" and pos[1] in DRIVING_VERBS)
                          or (pos[0] == "proof" and pos[1] == "verify")):
        return "alpaca %s %s" % (pos[0], pos[1])
    return None


def _is_main_file(word: str) -> bool:
    """`alpaca/__main__.py`, the package's entry file, run by its path."""
    return os.path.basename(word) == "__main__.py" \
        and os.path.basename(os.path.dirname(word)) == "alpaca"


def _names_entry(text: str) -> bool:
    """True when a path-shaped run of `text` names the driver's entry by the test `_driving_in`
    gives a command word: one of _ALPACA_MODULES, or a path whose normal form has the basename
    `alpaca` or is the entry file. `alpaca.research`, `alpaca/research` and `.alpaca/x` name a
    module or a folder of the package, not its entry (27.3)."""
    for m in _PATH_RUN.finditer(text):
        word = m.group(0)
        if word in _ALPACA_MODULES:
            return True
        norm = os.path.normpath(word)
        if os.path.basename(norm) == "alpaca" or _is_main_file(norm):
            return True
    return False


def _driving_in(parsed: dict, depth: int) -> str | None:
    from alpaca.research import pathscan
    for cmd in parsed["commands"]:
        argv = cmd["argv"]
        for i, a in enumerate(argv):
            entry = os.path.basename(a) == "alpaca" or _is_main_file(a) or (
                a == "-m" and i + 1 < len(argv) and argv[i + 1] in _ALPACA_MODULES)
            if entry:
                hit = _verb_after_entry(argv[i + 2:] if a == "-m" else argv[i + 1:])
                if hit:
                    return hit
        base = os.path.basename(argv[0]) if argv else ""
        if not (base in INTERPRETERS or base.startswith(INTERPRETERS)):
            continue
        bodies = [argv[i + 1] for i, a in enumerate(argv)
                  if a in ("-c", "-lc", "-ic", "-lic", "-e") and i + 1 < len(argv)]
        bodies += [parsed["heredocs"][k].get("body") or "" for k in cmd["heredocs"]]
        for body in bodies:
            if base in _SHELLS and depth < 4:
                try:
                    inner = pathscan.parse(body)
                except Exception:  # noqa: BLE001
                    inner = None
                hit = _driving_in(inner, depth + 1) if inner else None
                if hit:
                    return hit
            found = _DRIVING_TEXT.search(body)
            if found:
                return "alpaca " + " ".join(re.findall(r"[\w-]+", found.group(0))[-2:])
    return None


def driving_verb_hit(call: dict) -> str | None:
    """The driving verb a shell call runs, or None: `alpaca research start | resume | extend |
    stop | next-job | submit` and `alpaca proof verify`, through bin/alpaca, `python -m alpaca`
    or an `alpaca` entry point, as a command word, through a shell wrapper, or in the text of an
    interpreter's -c payload or heredoc. A subagent never drives a run (the guard denies the call
    by the agent id of the hook payload), and the driver's own calls are not read by this rule.
    A text the parse cannot read is still read by the whole-text rules below."""
    from alpaca.research import pathscan
    text = pathscan.shell_text(call)
    if text is None:
        return None
    try:
        parsed = pathscan.parse(text)
        hit = _driving_in(parsed, 0)
        if hit:
            return hit
        unwrapped = pathscan.unwrap(parsed)
        hit = _driving_in(unwrapped, 0) if unwrapped is not parsed else None
        if hit:
            return hit
    except Exception:  # noqa: BLE001 - fail-open
        pass
    # the whole command text, for the forms the parse does not open: a substitution, a backtick,
    # an eval, a pipe into a shell, a script written by the call, a variable that holds the entry
    # point. A name built at run time stays out of reach (R1 and R2 of in-session-runtime.md).
    found = _DRIVING_TEXT.search(text)
    if found:
        return "alpaca " + " ".join(re.findall(r"[\w-]+", found.group(0))[-2:])
    if re.search(r"\bxargs\b", text) and re.search(r"\b(?:research|proof)\b", text) \
            and _names_entry(text):
        # the verb arrives on the pipe: `echo start | xargs bin/alpaca research`. The package
        # counts only as a word that names the entry (27.3); the whole text is still read
        found = re.search(r"\b(?:%s|verify)\b" % "|".join(re.escape(v) for v in DRIVING_VERBS), text)
        if found:
            return "alpaca via xargs " + found.group(0)
    if "runpy" in text and re.search(r"run_(?:module|path)\(\W{0,3}[\w./]*alpaca", text):
        found = _VERB_WORDS.search(text)
        if found:
            return "alpaca " + " ".join(re.findall(r"[\w-]+", found.group(0))[-2:])
    return None


def git_write_hit(call: dict) -> str | None:
    """The git subcommand a shell call runs that writes a repository, or None."""
    from alpaca.research import pathscan
    text = pathscan.shell_text(call)
    if text is None:
        return None
    try:
        got = pathscan.unwrap(pathscan.parse(text))
    except Exception:  # noqa: BLE001
        return None
    for cmd in got["commands"]:
        argv = cmd["argv"]
        if argv and os.path.basename(argv[0]) == "git":
            rest = [a for a in argv[1:] if not a.startswith("-")]
            # skip the value words of -C and -c
            skip, words = False, []
            for a in argv[1:]:
                if skip:
                    skip = False
                    continue
                if a in ("-C", "-c", "--git-dir", "--work-tree"):
                    skip = True
                    continue
                if a.startswith("-"):
                    continue
                words.append(a)
            if words and words[0] in GIT_WRITES:
                if _git_listing(words):
                    continue                    # a listing reads
                return words[0]
    return None


def _git_listing(words: list) -> bool:
    """True when git words (subcommand first, options left out) are a listing form of a writing
    subcommand: `branch`, `tag`, `remote`, `config`, `worktree`, `notes`, `reflog` alone or with
    `list`, `show` or `get`, and `stash list` or `stash show`."""
    sub = words[1] if len(words) > 1 else None
    if words[0] in ("branch", "tag", "remote", "config", "worktree", "notes", "reflog") \
            and (sub is None or sub in ("list", "show", "get")):
        return True
    return words[0] == "stash" and sub in ("list", "show")


#: rule U8 of section 27.1: the git subcommands that only read. Any other subcommand of a git
#: call, outside the listing forms of _git_listing, is a hit: an allow list, which the strict
#: grammar permits (git appeared 67 times in 645 real job calls, all status, log, diff or show)
GIT_READS = ("status", "log", "diff", "show", "rev-parse", "ls-files", "ls-tree", "cat-file",
             "blame", "grep", "describe", "shortlog", "show-ref", "merge-base", "rev-list",
             "name-rev", "version", "help")
#: git options before the subcommand that take the next word as their value
_GIT_VALUE_OPTIONS = ("-C", "-c", "--git-dir", "--work-tree", "--namespace")
#: git options that set configuration at call time, which can define an alias or a command
_GIT_CONFIG_OPTIONS = ("-c", "--config-env", "--exec-path")


def git_unread_hit(call: dict) -> str | None:
    """U8: the reason a git call is outside the read list, or None. In a command that is not
    read-only, the first word that is git (`git`, `/usr/bin/git`, also behind `env`, `timeout`,
    `xargs` or any other runner) starts a git call; an option of _GIT_CONFIG_OPTIONS, or a
    subcommand outside GIT_READS and the listing forms, is a hit."""
    from alpaca.research import pathscan
    text = pathscan.shell_text(call)
    if text is None:
        return None
    try:
        got = pathscan.unwrap(pathscan.parse(text))
    except Exception:  # noqa: BLE001 - control_hits reads a parse failure first (M1)
        return None
    for cmd in got["commands"]:
        argv = _lead_stripped(cmd["argv"])
        if not argv or reads_only(argv):
            continue
        at = next((i for i, w in enumerate(argv) if os.path.basename(str(w)) == "git"), None)
        if at is None:
            continue
        rest = [str(a) for a in argv[at + 1:]]
        j = 0
        while j < len(rest) and rest[j].startswith("-"):
            if rest[j].partition("=")[0] in _GIT_CONFIG_OPTIONS:
                return "git %s sets configuration at call time, which the reader cannot read" % (
                    rest[j].partition("=")[0])
            j += 2 if rest[j] in _GIT_VALUE_OPTIONS else 1
        if j >= len(rest):
            continue
        words = [w for w in rest[j:] if not w.startswith("-")]
        if rest[j] in GIT_READS or _git_listing(words):
            continue
        return "git %s is outside the git read list" % rest[j]
    return None


def background_hit(call: dict) -> str | None:
    """The form of a call that starts a process outliving the call (K9): a background flag on a
    tool, nohup, setsid, disown, coproc, systemd-run, screen or tmux as a command word, or a
    trailing background &."""
    from alpaca.research import pathscan
    if call.get("kind") == "denied":
        return None                       # a call the guard refused never ran (section 18.1)
    inp = call.get("input")
    raw = call.get("raw_input") if isinstance(call.get("raw_input"), dict) else (
        inp if isinstance(inp, dict) else {})
    if raw.get("run_in_background") is True:
        return "run_in_background"
    text = pathscan.shell_text(call)
    if text is None:
        return None
    try:
        got = pathscan.unwrap(pathscan.parse(text))
    except Exception:  # noqa: BLE001
        return None
    if "background" in got["flags"]:
        return "a trailing &"
    for cmd in got["commands"]:
        argv = cmd["argv"]
        if argv and os.path.basename(argv[0]) in ("nohup", "setsid", "disown", "coproc",
                                                  "systemd-run", "screen", "tmux"):
            return os.path.basename(argv[0])
    return None


_IMPORTABLE = (".py", ".pyc", ".so", ".pth", ".pyd")


def root_module_write(path: str, install: str | None = None) -> bool:
    """True when `path` would put an importable file on the engine's import path: a module or
    extension directly in the install root, or a file of a new top-level folder there (a package
    an isolated start with the root on its path would find; 12.5, mechanism 3)."""
    install = os.path.realpath(install or install_root())
    rel = os.path.relpath(os.path.normpath(path), install)
    if rel == "." or rel.startswith(".."):
        return False
    parts = rel.split(os.sep)
    if len(parts) == 1:
        return parts[0].endswith(_IMPORTABLE)
    return not os.path.exists(os.path.join(install, parts[0])) and parts[-1].endswith(_IMPORTABLE)


def control_hits(calls: list, *, here: str, root: str, hook_scripts: list | None = None,
                 cfg: str | None = None, allowed: tuple = (), reset_cwd: bool = True,
                 plane: dict | None = None, exact_allowed: tuple = ()) -> list:
    """[{"line", "what", "why", "rule"}]: every call that reaches the control plane (rule S15, K15,
    the guard and the verifier breach scan share this reader). A name of CONTROL_NAMES where it
    runs something, a git command that writes a repository, a path of control_paths()["deny"]
    named for any use, a path of ["write"] named by a call that writes it, and a read under
    another agent's transcripts, unless the name or path lies under `allowed` (the stream's own
    cwd, read_paths, write_paths) or is one of `exact_allowed` (the persisted outputs of the
    agent's own tool results, each allowed as that exact file: in-session-runtime.md section
    18.1a). Since t-354 (section 27.1) the reader also denies by default what it cannot read: a
    text it cannot parse (M1), a git call outside the read list (U8), a write it cannot place
    (U1), a name in the words or program text of a command that may write (U4, U5), a folder a
    program writes unseen names into (U6), and a written folder that holds the control plane, a
    folder destination, a new top-level name or a glob (U7), after the exact readings of U2, U3
    and U10. The command text of every substitution is read as a call of its own, in its
    command's folders (U11). A hit through a U2 substitution names the word as written. `rule` words the guard's
    deny text: `unplaced`, `words-here`, `parse` or `path` (any other hit). A call of kind
    `denied` (one the guard refused) ran nothing and is skipped. Each call starts in `here` when
    reset_cwd."""
    plane = plane or control_paths(root, hook_scripts, cfg)
    install = os.path.realpath(install_root())
    allowed = tuple(os.path.normpath(a) for a in allowed if a)
    allowed_real = tuple(os.path.realpath(a) for a in allowed)
    exact = {os.path.normpath(e) for e in exact_allowed or () if e}
    entries_of_plane = list(plane["deny"]) + list(plane["write"])

    def one(call, base, depth):
        """(why, rule) of one shell call read from `base`, its substitution bodies
        included (U11): each body is read as a call of its own in each folder its
        command may run in, and a body nested deeper than SUBSTITUTION_DEPTH is a hit."""
        why, rule, shown = None, "path", None
        name = control_name_hit(call)
        if name:
            why = "runs %s" % name
        git = git_write_hit(call) if not why else None
        if git:
            why = "git %s writes a repository" % git
        if not why:
            why = unparsed_hit(call)
            rule = "parse" if why else rule
        if not why:
            why = git_unread_hit(call)
        if not why:
            entries = [(path, placed, "write" if writes else "read", None, None, "path")
                       for path, placed, writes in _call_paths(call, base)]
            entries += _unread_entries(call, base)
            for path, placed, kind, shown, prog, rule in entries:
                if not placed:
                    if kind == "write":
                        why = "writes %s, a path the reader cannot place" % path
                        rule = "unplaced"
                        break
                    continue
                # realpath also resolves the folders of a path that does not exist yet, so a
                # write through a symlink in an allowed folder is read where it lands (op-009
                # t-248: the cd chain places writes in the cd folder, where such a link may sit)
                p = os.path.realpath(path)
                np = os.path.normpath(path)
                if exact and (p in exact or np in exact):
                    continue
                if any(under(p, a) or under(p, r) for a, r in zip(allowed, allowed_real)):
                    continue
                if kind == "newtop":
                    # U7c: a new top-level name in the install root imports as a package
                    if os.path.dirname(p) == install and not os.path.lexists(p) \
                            and _IDENTIFIER.fullmatch(os.path.basename(p)):
                        why = "writes %s, a new top-level name on the engine's import path" % path
                        break
                    continue
                if kind == "ancestor":
                    # U7a: a written folder that holds a control-plane entry, or the install root
                    if p == install or any(under(e, p) and e != p for e in entries_of_plane):
                        why = "writes %s, a folder that holds the control plane" % path
                        break
                    continue
                if kind == "unseen":
                    # U6, U7b: a program that writes names the call does not show, in this folder
                    if p == install or any(under(p, e) or os.path.dirname(e) == p
                                           for e in entries_of_plane):
                        why = "runs %s in %s, where the reader cannot see what it writes" % (
                            prog or "a program", path)
                        break
                    continue
                writes = kind == "write"
                # a path written under an allowed folder that resolves outside it is read only
                # where it resolves; any other path is read both ways, as before
                both = (p,) if any(under(np, a) for a in allowed) else (p, np)
                if any(under(x, d) for x in both for d in plane["deny"]):
                    why = "names %s" % path
                    break
                if writes and (any(under(x, w) for x in both for w in plane["write"])
                               or root_module_write(p)):
                    why = "writes %s" % path
                    break
                if not writes and any(under(p, r) for r in plane["read_deny"]) \
                        and "/tool-results/" not in p:
                    why = "reads %s" % path
                    break
            else:
                shown, rule = None, "path"
            if why and shown:
                why = "%s (written `%s` in the call)" % (why, shown)
        if why:
            return why, rule
        for sub, folder in substitution_bodies(call, base):
            if depth >= SUBSTITUTION_DEPTH:
                return ("holds substitutions nested deeper than %d" % SUBSTITUTION_DEPTH,
                        "parse")
            inner = dict(call, input=sub, paths=[], text="")
            why, rule = one(inner, folder, depth + 1)
            if why and depth:
                return why, rule
            if why:
                # the deny text names the outermost substitution, as written in the call
                shown = " ".join(sub.split())
                shown = shown if len(shown) <= 80 else shown[:77] + "..."
                return "%s (in the substitution `%s`)" % (why, shown), rule
        return None, "path"

    out = []
    cur = here
    for pos, call in enumerate(calls or [], 1):
        if call.get("kind") == "denied":
            continue
        base = here if reset_cwd else cur
        why, rule = one(call, base, 0)
        if why:
            out.append({"line": pos, "what": call.get("tool"), "why": why, "rule": rule})
    return out
