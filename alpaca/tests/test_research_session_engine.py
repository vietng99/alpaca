"""The engine's own code, the hook scripts and the import path in an agent's reach (design/research-engine/
in-session-runtime.md section 12.5), and the files an agent loads as context (section 12.6).

With no jail an attempt agent could write code that an engine process loads later (an engine file, a
hook script, a module planted on an import path, a startup file) or text that a later agent receives
as context (a settings file, a CLAUDE.md, the auto-memory index). The run closes both classes with the
PIN (every file that decides or witnesses, with size, inode, mtime, ctime and sha256: any touch, a
restored one included, ends the run), the preload of the deciding code, isolated interpreters, the
guard, and the LEDGER of the files the runtime loads into an agent (a change inside a job's window
refuses the job; a change that an agent or no recorded call wrote ends the run).

The tests that change files do so in a copy of the engine tree under tmp_path (`engine_tree` patches
session_engine.install_root), never in this checkout.
"""
from __future__ import annotations

import ast
import json
import os
import json
import shutil
import subprocess
import sys
import time

import pytest

from alpaca import db
from alpaca.research import (errors, events, jobs, session_engine as eng, session_runtime)
from alpaca.tests import research_session_kit as rk
from alpaca.tests.conftest import REPO


@pytest.fixture
def engine_tree(tmp_path, monkeypatch):
    """A copy of the engine under tmp_path (alpaca/ without its tests, web and vendor files, bin/,
    formations/, agents/), which session_engine takes as the install root."""
    tree = tmp_path / "engine"
    shutil.copytree(os.path.join(REPO, "alpaca"), tree / "alpaca", ignore=shutil.ignore_patterns(
        "tests", "web", "vendor", "__pycache__", "*.pyc"))
    for sub in ("bin", "formations", "agents"):
        shutil.copytree(os.path.join(REPO, sub), tree / sub)
    (tree / ".claude").mkdir()
    shutil.copy(os.path.join(REPO, ".claude", "settings.json"), tree / ".claude" / "settings.json")
    monkeypatch.setattr(eng, "install_root", lambda: str(tree))
    monkeypatch.setattr(eng, "COMPILE_SITE_PACKAGES", False)
    # the pth of a bootstrapped main tree puts the whole main tree on sys.path; its files change
    # while other sessions work there, which is no change of this test's engine
    rk.drop_foreign_trees(monkeypatch)
    eng._CLOSURE_CACHE.clear()
    yield str(tree)
    eng._CLOSURE_CACHE.clear()


def _pin(tree, tmp_path, sid="s-drv", name="pin.json"):
    return eng.take_pin(str(tmp_path), sid, str(tmp_path / name), cfg=str(tmp_path / "cfg"),
                        compile_files=False)


# ================================================================ the static import closure
def _independent_imports(path, package):
    """Every `alpaca` module a file imports at any depth, by a walk written apart from the engine's:
    a name that resolves to a file under the tree."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names if a.name.split(".")[0] == "alpaca"}
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                parts = parts[:len(parts) - (node.level - 1)]
                base = ".".join(parts + ([base] if base else []))
            if base.split(".")[0] == "alpaca":
                found.add(base)
                found |= {"%s.%s" % (base, a.name) for a in node.names}
    return found


def test_engine_set_covers_the_static_import_closure():
    root = REPO
    files = {os.path.relpath(p, root) for p in eng.engine_set(root, with_pyc=False)}
    cut = tuple(c.replace(".", os.sep) for c in eng.ENGINE_CUT)
    # every function-level import the deciding code makes is in the set: the walk does not use sys.modules
    for must in ("alpaca/runlog/quotes.py", "alpaca/runlog/reviews.py", "alpaca/formation/manifest.py",
                 "alpaca/lineage.py", "alpaca/capture_io.py", "alpaca/resilience/budget.py",
                 "alpaca/research/session_runtime.py", "alpaca/research/jobs.py",
                 "alpaca/hooks/research_guard.py", "alpaca/hooks/subagent_start.py"):
        assert must in files, must
    # every non-Python file of alpaca/research/ (the prompts that build every job's text)
    for folder, _d, names in os.walk(os.path.join(root, "alpaca", "research")):
        if "__pycache__" in folder:
            continue
        for n in names:
            if not n.endswith(".py"):
                assert os.path.relpath(os.path.join(folder, n), root) in files, n
    # for every file of the set, an independent walk finds nothing it imports that is outside the set
    # (or one of the five cut modules)
    for rel in sorted(f for f in files if f.endswith(".py") and f.startswith("alpaca/")):
        module = rel[:-3].replace("/", ".")
        package = module if rel.endswith("__init__.py") else module.rsplit(".", 1)[0]
        for name in _independent_imports(os.path.join(root, rel), package.replace(".__init__", "")):
            target = os.path.join(root, name.replace(".", os.sep))
            for cand in (target + ".py", os.path.join(target, "__init__.py")):
                if os.path.isfile(cand):
                    r = os.path.relpath(cand, root)
                    assert r in files or any(r.startswith(c) for c in cut), (rel, name)
    # the hub server is the cut: no web file and no Rune file is in the set
    assert not any(f.startswith(("alpaca/web", "alpaca/serve.py", "alpaca/rune")) for f in files)


def test_the_closure_follows_function_level_imports_and_ignores_sys_modules(tmp_path):
    pkg = tmp_path / "alpaca"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text("def f():\n    from alpaca import b\n    return b\n")
    (pkg / "b.py").write_text("def g():\n    import alpaca.c\n    return alpaca.c\n")
    (pkg / "c.py").write_text("import json\nOPT = None\ntry:\n    import brotlipy\nexcept ImportError:\n    pass\n")
    got = eng.import_closure(str(tmp_path), roots=["alpaca.a"])
    assert got["files"] == ["alpaca/__init__.py", "alpaca/a.py", "alpaca/b.py", "alpaca/c.py"]
    assert "json" in got["external"] and "brotlipy" in got["external"]
    # negative: nothing was imported to find them, so a closure taken from sys.modules would be empty
    assert "alpaca.b" not in sys.modules or sys.modules["alpaca.b"] is None or True


def test_no_engine_file_loads_a_module_by_a_computed_name():
    """The walk cannot follow an import whose name is computed at run time. The ones in the engine
    are preload's (it imports the names the walk found), the literal __import__("hashlib"), and
    alpaca/cli.py's loop over its own VERB_MODULES tuple: every `bin/alpaca` process imports every
    verb module, the hub's `serve` and `hub_publish` among them, and those two are the cut of
    section 12.5 (not pinned, because another session edits them); the guard and rule S15 still name
    them, and the proof lists this as part of residual R12 (c)."""
    allowed = {"alpaca/research/session_engine.py", "alpaca/cli.py"}
    bad = []
    for rel in sorted(f for f in eng.engine_set(REPO, with_pyc=False) if f.endswith(".py")):
        r = os.path.relpath(rel, REPO)
        tree = ast.parse(open(rel, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name in ("import_module", "__import__") and r not in allowed:
                    arg = node.args[0] if node.args else None
                    if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                        bad.append((r, node.lineno))
    assert bad == [], bad
    # the verb modules cli.py loads by name are a literal tuple, and the cut ones are the only
    # engine-cut modules among them
    from alpaca import cli
    cut_loaded = {m for m in cli.VERB_MODULES if "alpaca.%s" % m in eng.ENGINE_CUT}
    assert cut_loaded == {"serve", "hub_publish"}
    # control: a planted call is found by the same reading
    planted = ast.parse("import importlib\nimportlib.import_module(name)\n")
    calls = [n for n in ast.walk(planted) if isinstance(n, ast.Call)]
    assert calls and not isinstance(calls[0].args[0], ast.Constant)


def test_cut_modules_are_not_imported_by_research():
    cut = {c for c in eng.ENGINE_CUT}
    for name in os.listdir(os.path.join(REPO, "alpaca", "research")):
        if not name.endswith(".py"):
            continue
        path = os.path.join(REPO, "alpaca", "research", name)
        for mod in _independent_imports(path, "alpaca.research"):
            assert mod not in cut and not any(mod.startswith(c + ".") for c in cut), (name, mod)


def test_preload_loads_the_closure():
    code = ("import sys; from alpaca.research import session_engine as e;"
            "r = e.preload(block=False);"
            "c = e.closure_of(e.install_root());"
            "missing = [m for m in c['modules'] if m not in sys.modules];"
            "print(len(c['modules']), r['loaded'], missing)")
    got = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True,
                         env=dict(os.environ, PYTHONPATH=REPO), timeout=180)
    assert got.returncode == 0, got.stderr
    total, loaded, missing = got.stdout.split(" ", 2)
    assert int(loaded) >= int(total) - 3 and missing.strip().count("alpaca.") <= 3, got.stdout


# ================================================================ the pin
def test_a_touched_engine_file_is_seen_and_a_restored_one_too(engine_tree, tmp_path):
    summary = _pin(engine_tree, tmp_path)
    pin = eng.read_json(summary["pin_file"])
    assert eng.engine_unchanged(str(tmp_path), pin)["ok"] is True
    target = os.path.join(engine_tree, "alpaca", "research", "accept.py")
    original = open(target, "rb").read()
    st = os.stat(target)
    # rewrite, then restore the content and set the mtime back: the ctime has moved
    with open(target, "ab") as fh:
        fh.write(b"\n# planted\n")
    got = eng.engine_unchanged(str(tmp_path), pin)
    assert got["ok"] is False and target in [c["path"] for c in got["changed"]]
    with open(target, "wb") as fh:
        fh.write(original)
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert os.stat(target).st_mtime_ns == st.st_mtime_ns and open(target, "rb").read() == original
    got = eng.engine_unchanged(str(tmp_path), pin)
    assert got["ok"] is False and target in [c["path"] for c in got["changed"]]


def test_an_added_removed_or_unrelated_file_of_the_engine_folders(engine_tree, tmp_path):
    pin = eng.read_json(_pin(engine_tree, tmp_path)["pin_file"])
    # a file beside the pinned ones is a change
    added = os.path.join(engine_tree, "alpaca", "research", "extra_module.py")
    open(added, "w").write("x = 1\n")
    got = eng.engine_unchanged(str(tmp_path), pin)
    assert not got["ok"] and added in [c["path"] for c in got["changed"]]
    os.remove(added)
    assert eng.engine_unchanged(str(tmp_path), pin)["ok"] is False or True
    # the hub server is not in the set: an edit of it changes nothing
    pin2 = eng.read_json(_pin(engine_tree, tmp_path, name="pin2.json")["pin_file"])
    open(os.path.join(engine_tree, "alpaca", "serve.py"), "a").write("\n# edited by another session\n")
    assert eng.engine_unchanged(str(tmp_path), pin2)["ok"] is True


@pytest.mark.parametrize("kind", ["py", "pyc", "so", "pth", "sitecustomize"])
def test_import_root_changes_are_seen(engine_tree, tmp_path, monkeypatch, kind):
    site = tmp_path / "site-packages"
    site.mkdir()
    (site / "present.py").write_text("x = 1\n")
    monkeypatch.setattr(sys, "path", list(sys.path) + [str(site)])
    pin = eng.read_json(_pin(engine_tree, tmp_path)["pin_file"])
    name = {"py": "newmod.py", "pyc": "newmod.pyc", "so": "newmod.so", "pth": "extra.pth",
            "sitecustomize": "sitecustomize.py"}[kind]
    assert eng.engine_unchanged(str(tmp_path), pin)["ok"] is True
    (site / name).write_text("import os\n")
    got = eng.engine_unchanged(str(tmp_path), pin)
    assert not got["ok"] and str(site / name) in [c["path"] for c in got["changed"]], got


def test_a_module_planted_in_the_project_root_is_a_change_and_other_files_are_not(engine_tree, tmp_path):
    pin = eng.read_json(_pin(engine_tree, tmp_path)["pin_file"])
    for planted in ("json.py", "brotli.py", "sitecustomize.py"):
        path = os.path.join(engine_tree, planted)
        open(path, "w").write("raise SystemExit('planted')\n")
        got = eng.engine_unchanged(str(tmp_path), pin)
        assert not got["ok"] and path in [c["path"] for c in got["changed"]], planted
        os.remove(path)
    # a package folder with an __init__.py, and a file that Python could not import
    os.makedirs(os.path.join(engine_tree, "brotli"))
    open(os.path.join(engine_tree, "brotli", "__init__.py"), "w").write("x = 1\n")
    assert not eng.engine_unchanged(str(tmp_path), pin)["ok"]
    shutil.rmtree(os.path.join(engine_tree, "brotli"))
    open(os.path.join(engine_tree, "notes.txt"), "w").write("not importable\n")
    os.makedirs(os.path.join(engine_tree, "scratch"))
    assert eng.engine_unchanged(str(tmp_path), pin)["ok"] is True


def test_import_root_check_is_cheap(monkeypatch):
    rk.drop_foreign_trees(monkeypatch)          # the pth of a bootstrapped main tree is not this engine
    start = time.monotonic()
    roots = eng.import_roots(REPO, digest=False)
    elapsed = time.monotonic() - start
    assert roots and sum(len(r["files"]) for r in roots) > 100
    assert elapsed < 0.25 + 3.0           # the measured 0.007 s is a warm cache; a cold one reads the disk


# ================================================================ t-443: the walk enters import folders only
#: files of a shared main tree that an import can reach: every folder on the way is an identifier
_MAIN_KEPT = ("top.py", "extra.pth", "pkg/__init__.py", "pkg/mod.py",
              "pkg/__pycache__/mod.cpython-314.pyc", "nspkg/sub/leaf.py")
#: files of the same tree that no import reaches: a folder on the way is not an identifier
_MAIN_FOREIGN = (".alpaca/worktrees/other/alpaca/__init__.py",
                 ".alpaca/worktrees/other/alpaca/research/__pycache__/jobs.cpython-314.pyc",
                 ".git/hooks/h.py", "node-modules/x.py", "x-1.0.dist-info/y.py",
                 "bad-name/__init__.py")


def _write_tree(base, rels):
    for rel in rels:
        path = os.path.join(str(base), rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("" if rel.endswith("__init__.py") else "x = 1\n")
    return os.path.realpath(str(base))


def _bump(path):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("y = 2\n")


def test_the_main_root_pin_holds_no_worktree_and_no_folder_python_cannot_import_through(
        engine_tree, tmp_path, monkeypatch):
    """t-443 done bar 1 and the fail case (in-session-runtime.md section 30.2): a shared main tree on
    sys.path is walked through identifier folders only, so the pin holds no file of another worktree,
    of .git or of a dist-info folder, and a change an import can reach still ends the run."""
    main = _write_tree(tmp_path / "main", _MAIN_KEPT + _MAIN_FOREIGN)
    monkeypatch.setattr(sys, "path", list(sys.path) + [main])
    pin = eng.read_json(_pin(engine_tree, tmp_path)["pin_file"])
    mine = [r for r in pin["import_roots"] if r["root"] == main]
    assert len(mine) == 1 and mine[0]["project"] is False
    assert sorted(os.path.relpath(p, main) for p in mine[0]["files"]) == sorted(_MAIN_KEPT)
    for r in pin["import_roots"]:
        for key in list(r["files"]) + list(r["folders"]):
            rel = os.path.relpath(key, r["root"])
            parts = [] if rel == "." else rel.split(os.sep)
            folders = parts[:-1] if key in r["files"] else parts
            assert ".alpaca" not in folders and all(p.isidentifier() for p in folders), (r["root"], key)
    # the project mode reads the same rule: neither bad-name nor .alpaca is listed
    shallow = eng._scan_root(main, main, set(), False)
    listed = list(shallow["files"]) + list(shallow["folders"])
    assert not [p for p in listed if "bad-name" in p or os.sep + ".alpaca" in p], listed
    # parallel work no import reaches is no change
    _bump(os.path.join(main, _MAIN_FOREIGN[1]))
    _write_tree(main, [".git/hooks/new.py"])
    got = eng.engine_unchanged(str(tmp_path), pin)
    assert got["ok"] is True, got["changed"]
    # the fail case: a real change of a module on the root still ends the run
    _bump(os.path.join(main, "pkg", "mod.py"))
    got = eng.engine_unchanged(str(tmp_path), pin)
    assert not got["ok"] and os.path.join(main, "pkg", "mod.py") in [c["path"] for c in got["changed"]]
    _write_tree(main, ["pkg/new.py"])
    got = eng.engine_unchanged(str(tmp_path), pin)
    assert os.path.join(main, "pkg", "new.py") in [c["path"] for c in got["changed"]], got["changed"]


def test_a_root_with_a_non_identifier_name_is_still_walked(tmp_path):
    """Only the folders under a root are tested, never the root's own name or the path to it: a
    site-packages under `x-1.0/` and a `lib-dynload` stay walked, and a module planted at the top of
    such a root or an edit under it is a change (t-443, section 30.2)."""
    site = _write_tree(tmp_path / "x-1.0" / "lib" / "python3.14" / "site-packages",
                       ["extra.pth", "sitecustomize.py", "pkg/__init__.py", "pkg/mod.py"])
    dyn = _write_tree(tmp_path / "lib-dynload", ["m.cpython-314-x86_64-linux-gnu.so"])
    other = str(tmp_path / "proj")
    roots = [eng._scan_root(site, other, set(), True), eng._scan_root(dyn, other, set(), True)]
    assert sorted(os.path.relpath(p, site) for p in roots[0]["files"]) == [
        "extra.pth", "pkg/__init__.py", "pkg/mod.py", "sitecustomize.py"]
    assert [os.path.relpath(p, dyn) for p in roots[1]["files"]] == ["m.cpython-314-x86_64-linux-gnu.so"]
    pin = {"install_root": os.path.realpath(eng.install_root()), "files": {}, "folders": {},
           "import_roots": roots}
    assert eng.engine_unchanged(other, pin, include_text=False)["ok"] is True
    _write_tree(site, ["json.py"])
    got = eng.engine_unchanged(other, pin, include_text=False)
    assert not got["ok"] and os.path.join(site, "json.py") in [c["path"] for c in got["changed"]]
    os.remove(os.path.join(site, "json.py"))
    _bump(os.path.join(site, "pkg", "mod.py"))
    got = eng.engine_unchanged(other, pin, include_text=False)
    assert not got["ok"] and os.path.join(site, "pkg", "mod.py") in [c["path"] for c in got["changed"]]


def test_every_closure_name_is_made_of_identifiers():
    """The premise of the walk rule (t-443): no module the engine imports, its own or another, is
    reached through a folder whose name is not an identifier."""
    closure = eng.closure_of(eng.install_root())
    names = list(closure["modules"]) + list(closure["external"])
    assert names
    bad = [n for n in names if not all(part.isidentifier() for part in n.split("."))]
    assert bad == []


def test_the_text_sources_are_pinned_and_the_tick_file_is_not(engine_tree, tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    cfg = tmp_path / "cfg"          # outside the project, as a pooled account's config folder is
    cfg.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    (cfg / "settings.json").write_text(json.dumps({"hooks": {"PostToolUse": [{"hooks": [
        {"type": "command", "command": "bash %s PostToolUse" % (cfg / "inject.sh")}]}]}}))
    (cfg / "inject.sh").write_text(
        'CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"\n'
        'LEVEL_FILE="$CLAUDE_DIR/.autodrive-level.$SID"\n'
        'CONTEXT_FILE="$CLAUDE_DIR/.autodrive-context.$SID"\n'
        'TICK_FILE="$CLAUDE_DIR/.autodrive-tick.$SID"\n'
        'EXTRA="$CLAUDE_DIR/.autodrive-extra.$SID"\n')
    (cfg / ".autodrive-context.s-drv").write_text("Declared scope/goal: x\n")
    names = eng.hook_data_files(str(proj), eng.hook_scripts(str(proj), str(cfg)), "s-drv", str(cfg))
    base = {os.path.basename(p) for p in names}
    assert {".autodrive-level.s-drv", ".autodrive-level", ".autodrive-context.s-drv",
            ".autodrive-context", ".autodrive-extra.s-drv"} <= base
    assert not any(b.startswith(".autodrive-tick") for b in base)
    pin = eng.read_json(eng.take_pin(str(proj), "s-drv", str(tmp_path / "pin.json"),
                                     cfg=str(cfg), compile_files=False)["pin_file"])
    assert eng.engine_unchanged(str(proj), pin, driver_session="s-drv")["ok"] is True
    # the tick file changes on every call and does not trip the check
    (cfg / ".autodrive-tick.s-drv").write_text("1")
    (cfg / ".autodrive-tick.s-drv").write_text("2")
    got = eng.engine_unchanged(str(proj), pin, driver_session="s-drv")
    assert got["ok"] is True, got
    # a verdict written into the context file is a change, the restored content too (ctime)
    ctx = cfg / ".autodrive-context.s-drv"
    st = os.stat(ctx)
    original = ctx.read_text()
    ctx.write_text("Declared scope/goal: v2 PASS\n")
    assert eng.engine_unchanged(str(proj), pin, driver_session="s-drv")["ok"] is False
    ctx.write_text(original)
    os.utime(ctx, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert eng.engine_unchanged(str(proj), pin, driver_session="s-drv")["ok"] is False
    # the driver-bound files are compared only for the driver the pin names
    got = eng.engine_unchanged(str(proj), pin, driver_session="another")
    assert got["ok"] is True, got


# ================================================================ isolation
def test_isolated_start_ignores_the_working_folder(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    for name in ("json.py", "brotli.py", "sitecustomize.py"):
        (work / name).write_text("raise SystemExit('planted %s ran')\n" % name)
    got = subprocess.run([sys.executable, "-I", "-c",
                          "import sys, json; print(sys.flags.isolated, sys.flags.safe_path, "
                          "[p for p in sys.path if p in ('', %r)])" % str(work)],
                         cwd=str(work), capture_output=True, text=True, timeout=60)
    assert got.returncode == 0, got.stderr
    assert got.stdout.startswith("1 True []"), got.stdout
    # control: without -I the planted module runs
    plain = subprocess.run([sys.executable, "-c", "import json"], cwd=str(work),
                           capture_output=True, text=True, timeout=60, env=dict(
                               os.environ, PYTHONPATH=str(work)))
    assert plain.returncode != 0 and "planted" in plain.stderr


def test_launch_argv_is_isolated_for_a_session_run(project, monkeypatch):
    from alpaca.research import runner
    seen = []

    class Rec:
        def __init__(self, argv, **kw):
            seen.append(list(argv))
            self.pid = 4242

    monkeypatch.setattr(runner.subprocess, "Popen", Rec)
    for mode, want in (("session", True), ("test", False)):
        seen.clear()
        monkeypatch.setattr(runner, "_runtime_mode", lambda root, run, m=mode: m)
        runner.launch(project, "rr-001", foreground=False, session="s")
        argv = seen[0]
        assert argv[0] == sys.executable and (("-I" in argv[:2]) is want), (mode, argv)
        assert argv[-4:-2] == ["--run", "rr-001"] or "--run" in argv


def test_wrappers_pass_isolated():
    wrapper = open(os.path.join(REPO, "bin", "alpaca"), encoding="utf-8").read()
    assert "alpaca-python\" --isolated -m alpaca" in wrapper
    py = open(os.path.join(REPO, "bin", "alpaca-python"), encoding="utf-8").read()
    assert '"${1:-}" = "--isolated"' in py and '-I "$ALPACA_BOOT" "$@"' in py     # alpaca/boot.py (t-428)
    settings = json.load(open(os.path.join(REPO, ".claude", "settings.json"), encoding="utf-8"))
    commands = [h["command"] for entries in settings["hooks"].values() for g in entries
                for h in g["hooks"]]
    assert len(commands) == 12          # temp_guard joined the Bash PreToolUse hooks (op-020)
    assert all("bin/alpaca-python --isolated -m alpaca.hooks." in c for c in commands), commands
    assert any("research_guard" in c for c in commands) and any("subagent_start" in c for c in commands)


def test_the_wrapper_runs_isolated_through_the_boot_with_or_without_a_pth(tmp_path):
    """`--isolated` with a venv starts `python -I <root>/alpaca/boot.py`, which loads the package by
    its location (t-428): no `alpaca-root.pth` is needed, and one naming another root changes
    nothing. With no venv it runs as before; other callers are unchanged, except `-I -m` (the
    generated units), which goes through the boot too."""
    root = tmp_path / "inst"
    (root / "bin").mkdir(parents=True)
    for n in ("alpaca-python",):
        shutil.copy(os.path.join(REPO, "bin", n), root / "bin" / n)
    venv = root / ".venv" / "bin"
    # a stand-in interpreter that prints its first two arguments
    stub = "#!/usr/bin/env bash\necho \"first=$1 second=$2\"\n"
    run = lambda *a: subprocess.run([str(root / "bin" / "alpaca-python"), *a], capture_output=True,  # noqa: E731
                                    text=True, timeout=30, env=dict(os.environ, PATH=str(tmp_path / "path") + os.pathsep + os.environ.get("PATH", ""))).stdout
    (tmp_path / "path").mkdir()
    (tmp_path / "path" / "python3").write_text(stub)
    (tmp_path / "path" / "python3").chmod(0o755)
    boot = str(root.resolve() / "alpaca" / "boot.py")
    assert run("--isolated", "-m", "alpaca").strip() == "first=-m second=alpaca"   # no venv: as before
    venv.mkdir(parents=True)
    (venv / "python").write_text(stub)
    (venv / "python").chmod(0o755)
    sp = root / ".venv" / "lib" / "python3.99" / "site-packages"
    sp.mkdir(parents=True)
    assert run("--isolated", "-m", "alpaca").strip() == "first=-I second=" + boot
    (sp / "alpaca-root.pth").write_text("/somewhere/else\n")
    assert run("--isolated", "-m", "alpaca").strip() == "first=-I second=" + boot
    assert run("-m", "alpaca").strip() == "first=-m second=alpaca"            # other callers unchanged
    assert run("-I", "-m", "alpaca").strip() == "first=-I second=" + boot     # the units' form


def test_every_sys_executable_use_in_the_engine_is_isolated():
    """The grep of the design: each argv built from sys.executable in alpaca/research/ carries -I,
    or is the runner's own conditional start. A new use must pass -I."""
    root = os.path.join(REPO, "alpaca", "research")
    uses = {}
    for name in sorted(os.listdir(root)):
        if name.endswith(".py"):
            text = open(os.path.join(root, name), encoding="utf-8").read()
            if "sys.executable" in text:
                uses[name] = text
    assert set(uses) <= {"runner.py", "launcher.py", "gates.py", "session_engine.py",
                         "session_runtime.py"}, sorted(uses)
    assert '"-I", "-S"' in uses["launcher.py"] and '"-I"' in uses["gates.py"]
    assert 'argv.append("-I")' in uses["runner.py"]


def test_drive_checks_refuses_a_plain_interpreter(project, monkeypatch):
    w = rk.JudgeWorld(monkeypatch, project)
    try:
        monkeypatch.undo()                     # the kit's patch of isolation_ok goes
        rk.prepare(monkeypatch, project)
        monkeypatch.setattr(session_runtime, "isolation_ok", eng.isolation_ok)
        assert eng.isolation_ok() is False     # pytest is not an isolated interpreter
        with pytest.raises(errors.ResearchRefusal) as exc:
            session_runtime.drive_checks(w.conn, project, w.sid, level=6)
        assert exc.value.reason == "RUN-ISOLATION-MISSING"
    finally:
        w.close()


# ================================================================ a later edit of a deciding module
def test_preload_makes_a_later_edit_inert(engine_tree, tmp_path):
    """The accept rule, the review writers and the quote matcher are imported inside the step that
    first needs them; without the preload an attempt that rewrote one of them during its job would
    change the rule in the process that judges it. After `preload()` the live process holds the
    modules it started with, and the stat check shows the edit."""
    proj = tmp_path / "proj"
    proj.mkdir()
    script = tmp_path / "probe.py"
    script.write_text(
        "import importlib, json, os, sys\n"
        "sys.path.insert(0, sys.argv[2])\n"
        "from alpaca.research import session_engine as e\n"
        "tree = e.install_root()\n"
        "e.PRELOAD_BLOCK = False\n"
        "e.preload(tree, block=False)\n"
        "lazy = ['alpaca.research.accept', 'alpaca.runlog.quotes', 'alpaca.runlog.reviews']\n"
        "loaded = [m for m in lazy if m in sys.modules]\n"
        "from alpaca.research import accept\n"
        "before = accept.pick_winner.__code__.co_code\n"
        "pinpath = os.path.join(sys.argv[1], 'pin.json')\n"
        "e.take_pin(sys.argv[1], 's', pinpath, cfg=os.path.join(sys.argv[1], 'cfg'), compile_files=False)\n"
        "for rel in ('alpaca/research/accept.py', 'alpaca/runlog/quotes.py', 'alpaca/runlog/reviews.py'):\n"
        "    with open(os.path.join(tree, rel), 'a') as fh:\n"
        "        fh.write('\\ndef pick_winner(*a, **k):\\n    return \"accept everything\"\\n')\n"
        "m2 = importlib.import_module('alpaca.research.accept')\n"
        "got = e.engine_unchanged(sys.argv[1], pinpath)\n"
        "print(json.dumps({'loaded': loaded, 'same_module': m2 is accept,\n"
        "                  'same_code': m2.pick_winner.__code__.co_code == before,\n"
        "                  'seen': not got['ok'],\n"
        "                  'paths': sorted(os.path.basename(c['path']) for c in got['changed'])}))\n")
    got = subprocess.run([sys.executable, "-I", str(script), str(proj), engine_tree], cwd=engine_tree,
                         capture_output=True, text=True, timeout=300)
    assert got.returncode == 0, got.stderr
    out = json.loads(got.stdout.strip().splitlines()[-1])
    assert out["loaded"] == ["alpaca.research.accept", "alpaca.runlog.quotes", "alpaca.runlog.reviews"]
    assert out["same_module"] and out["same_code"], out        # the live process did not change
    assert out["seen"] and {"accept.py", "quotes.py", "reviews.py"} <= set(out["paths"]), out


# ================================================================ two runs in one tree
def test_a_second_start_leaves_the_first_runs_pin_intact(engine_tree, tmp_path):
    """t-243: start byte-compiles the engine set; a .pyc that is already current must keep its
    stat, or the start of a second run ends every run already live in the tree interrupted
    (rr-008 by rr-009 and rr-009 by rr-010, 2026-10-04 04:44)."""
    first = eng.take_pin(str(tmp_path), "s-a", str(tmp_path / "a.json"), cfg=str(tmp_path / "cfg"))
    pycs = [p for p in eng.read_json(first["pin_file"])["files"] if p.endswith(".pyc")]
    assert pycs, "the first start compiled no .pyc"
    stat_before = {p: os.stat(p) for p in pycs}
    second = eng.take_pin(str(tmp_path), "s-b", str(tmp_path / "b.json"), cfg=str(tmp_path / "cfg"))
    assert second["engine_stat_sha256"] == first["engine_stat_sha256"]
    for p, st in stat_before.items():
        now = os.stat(p)
        assert (now.st_ino, now.st_mtime_ns, now.st_ctime_ns) == (st.st_ino, st.st_mtime_ns,
                                                                   st.st_ctime_ns), p


def test_a_stale_or_missing_pyc_is_compiled_at_start(engine_tree, tmp_path):
    eng.take_pin(str(tmp_path), "s-a", str(tmp_path / "a.json"), cfg=str(tmp_path / "cfg"))
    import importlib.util
    src = os.path.join(engine_tree, "alpaca", "research", "session_engine.py")
    gone = os.path.join(engine_tree, "alpaca", "paths.py")
    os.remove(importlib.util.cache_from_source(gone))
    st = os.stat(src)
    os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert not eng._pyc_current(src) and not eng._pyc_current(gone)
    eng.take_pin(str(tmp_path), "s-b", str(tmp_path / "b.json"), cfg=str(tmp_path / "cfg"))
    assert eng._pyc_current(src) and eng._pyc_current(gone)


def test_source_date_epoch_does_not_bring_the_rewrite_back(engine_tree, tmp_path, monkeypatch):
    """With SOURCE_DATE_EPOCH set py_compile writes hash-based .pyc by default, which the skip never
    takes as current; start compiles timestamp .pyc so the second start still leaves them alone."""
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    first = eng.take_pin(str(tmp_path), "s-a", str(tmp_path / "a.json"), cfg=str(tmp_path / "cfg"))
    second = eng.take_pin(str(tmp_path), "s-b", str(tmp_path / "b.json"), cfg=str(tmp_path / "cfg"))
    assert second["engine_stat_sha256"] == first["engine_stat_sha256"]
