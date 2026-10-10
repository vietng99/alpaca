"""t-428 and t-452: every process an Alpaca hook, verb, unit or child start runs imports the `alpaca`
of its own tree, by the package's location, and nothing else from the install root; a child keeps
every dependency folder of its parent.

The layout these tests pin: `bin/alpaca-python --isolated` (and `-I -m`, the form the generated units
use) starts `python -I <tree>/alpaca/boot.py`, and `alpaca.child.python_argv` starts
`python -I <package>/alpaca/boot.py --path <json>`. The boot loads `alpaca` from
`<tree>/alpaca/__init__.py` into `sys.modules` before it runs the module, so no path search decides
which `alpaca` runs and no path entry has to name the install root or be dropped for holding another
`alpaca`. The venv carries no `alpaca-root.pth`; a legacy one (the live venv's, which names the main
checkout) is taken off the path of every process the boot starts.

Before (main at f15a17101):

- t-428: the venv's `alpaca-root.pth` puts the whole install root at the end of `sys.path`, so a
  module at the root is importable in every isolated process; a linked worktree, whose `.venv` is a
  link to the main venv, imports the main checkout's `alpaca` under `-I`, and its
  `bin/alpaca-python --isolated` falls back to a plain interpreter
  (.alpaca/rune2-development/execution/t-070/notes-r2-ca.md, open issues).
- t-452: `alpaca/child.py` `_holds_alpaca` drops every path entry that holds an importable `alpaca`,
  so a site folder that also holds an alpaca-named distribution is dropped with its dependencies
  (.alpaca/proofs/op-008/t-070.logs/land/review-97b59a438.md m2, probe r01 case F).
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]          # the tree that holds this `alpaca` package
BOOT = REPO / "alpaca" / "boot.py"

#: the probe each fake tree's package carries: which tree's `alpaca` ran, the flags, the path, and
#: which of the names given on the command line imported
PROBE = r'''
import json, os, sys
import alpaca
out = {"tree": getattr(alpaca, "TREE", None), "file": alpaca.__file__,
       "isolated": sys.flags.isolated, "path": list(sys.path), "imports": {}}
for name in sys.argv[1:]:
    try:
        __import__(name)
        out["imports"][name] = "imported"
    except ImportError:
        out["imports"][name] = "missing"
print(json.dumps(out))
'''


def _venv(where):
    """A real venv with no pip (the venv module with the base interpreter, about a tenth of a second)."""
    base = getattr(sys, "_base_executable", None) or sys.executable
    subprocess.run([base, "-m", "venv", "--without-pip", str(where)], check=True, capture_output=True,
                   timeout=120)
    site = next(where.glob("lib/python*/site-packages"))
    return site


def _tree(root, name):
    """An install tree laid out as a checkout: bin/alpaca-python and this tree's boot, and a small
    `alpaca` package that says which tree it is."""
    (root / "bin").mkdir(parents=True)
    shutil.copy2(REPO / "bin" / "alpaca-python", root / "bin" / "alpaca-python")
    (root / "alpaca").mkdir()
    if BOOT.is_file():
        shutil.copy2(BOOT, root / "alpaca" / "boot.py")
    (root / "alpaca" / "__init__.py").write_text("TREE = %r\n" % name)
    (root / "alpaca" / "probe.py").write_text(PROBE)
    return root


def _run(argv, cwd):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("PYTHON") and k not in ("ALPACA_ROOT", "VIRTUAL_ENV")}
    p = subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, (argv, p.stdout[-400:], p.stderr[-1200:])
    return json.loads(p.stdout.strip().splitlines()[-1])


def _real(paths):
    return [os.path.realpath(p or ".") for p in paths]


FORMS = {"hook (--isolated -m)": ["--isolated", "-m"], "unit (-I -m)": ["-I", "-m"]}


@pytest.mark.parametrize("form", sorted(FORMS))
def test_a_linked_worktree_imports_its_own_alpaca(tmp_path, form):
    """A linked worktree's `.venv` is a link to the main checkout's venv, whose legacy
    `alpaca-root.pth` names the main checkout (the live layout). The worktree's own wrapper still
    starts an isolated interpreter that runs the worktree's `alpaca`, with neither tree on the path."""
    main = _tree(tmp_path / "main", "main")
    site = _venv(main / ".venv")
    (site / "alpaca-root.pth").write_text(str(main) + "\n")
    wt = _tree(tmp_path / "wt", "worktree")
    (wt / ".venv").symlink_to(main / ".venv", target_is_directory=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    got = _run([str(wt / "bin" / "alpaca-python")] + FORMS[form] + ["alpaca.probe"], elsewhere)
    assert got["tree"] == "worktree", got
    assert got["file"] == str(wt / "alpaca" / "__init__.py"), got
    assert got["isolated"] == 1, got
    path = _real(got["path"])
    assert os.path.realpath(str(main)) not in path and os.path.realpath(str(wt)) not in path, path


@pytest.mark.parametrize("layout", ["legacy alpaca-root.pth", "no .pth"])
@pytest.mark.parametrize("form", sorted(FORMS))
def test_nothing_but_the_package_is_imported_from_the_install_root(tmp_path, form, layout):
    """A module at the install root and a stray `alpaca` (and module) in the working folder are never
    imported by a process the wrapper starts, with the live venv's legacy `.pth` (which names the
    root) or with none (a venv bootstrapped from now on)."""
    root = _tree(tmp_path / "inst", "own")
    site = _venv(root / ".venv")
    if layout == "legacy alpaca-root.pth":
        (site / "alpaca-root.pth").write_text(str(root) + "\n")
    (root / "stray_root_module_t428.py").write_text("X = 1\n")
    work = tmp_path / "work"
    (work / "alpaca").mkdir(parents=True)
    (work / "alpaca" / "__init__.py").write_text("TREE = 'stray in the working folder'\n")
    (work / "alpaca" / "probe.py").write_text(PROBE)
    (work / "stray_cwd_module_t428.py").write_text("X = 1\n")
    got = _run([str(root / "bin" / "alpaca-python")] + FORMS[form]
               + ["alpaca.probe", "stray_root_module_t428", "stray_cwd_module_t428", "json"], work)
    assert got["tree"] == "own" and got["file"] == str(root / "alpaca" / "__init__.py"), got
    assert got["isolated"] == 1, got
    assert got["imports"] == {"stray_root_module_t428": "missing", "stray_cwd_module_t428": "missing",
                              "json": "imported"}, got
    path = _real(got["path"])
    assert os.path.realpath(str(root)) not in path and os.path.realpath(str(work)) not in path, path


def test_a_site_folder_holding_another_alpaca_keeps_its_dependencies_for_the_wrapper(tmp_path):
    """t-452 at the wrapper: the venv's site-packages holds an alpaca-named distribution (PyPI's
    alpaca-py installs a top-level `alpaca`) and a dependency. The hook runs the tree's own `alpaca`,
    the distribution's code never runs, and the dependency still imports."""
    root = _tree(tmp_path / "inst", "own")
    site = _venv(root / ".venv")
    (site / "alpaca-root.pth").write_text(str(root) + "\n")
    marker = tmp_path / "DIST-ALPACA-RAN.txt"
    (site / "alpaca").mkdir()
    (site / "alpaca" / "__init__.py").write_text(
        "open(%r, 'a').write('ran\\n')\nTREE = 'alpaca-named distribution'\n" % str(marker))
    (site / "needed_dep_t452.py").write_text("X = 1\n")
    got = _run([str(root / "bin" / "alpaca-python"), "--isolated", "-m", "alpaca.probe",
                "needed_dep_t452"], tmp_path)
    assert got["tree"] == "own", got
    assert got["imports"] == {"needed_dep_t452": "imported"}, got
    assert not marker.exists()


# ---------------------------------------------------------------------------- the child launcher
def _child_report(argv, cwd, env=None):
    p = subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, (p.stdout[-400:], p.stderr[-1200:])
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_the_child_gets_its_parents_package_and_no_install_root_on_its_path(tmp_path, monkeypatch):
    """The child runs the parent's package, loaded by its location, and neither the package root nor
    a root a legacy `alpaca-root.pth` names (the main checkout, seen from a worktree) is on its path."""
    from alpaca import child
    legacy = tmp_path / "main-checkout"
    legacy.mkdir()
    monkeypatch.setattr(sys, "path", list(sys.path) + [str(legacy)])
    monkeypatch.setattr(child, "_pth_roots", lambda: [str(legacy)])
    got = _child_report(child.python_argv("alpaca.tests.test_package_import_path", "probe"), tmp_path,
                        child.child_env())
    assert got["file"] == str(REPO / "alpaca" / "__init__.py"), got
    assert got["isolated"] == 1, got
    path = _real(got["path"])
    assert os.path.realpath(str(REPO)) not in path and os.path.realpath(str(legacy)) not in path, path


def test_a_site_folder_holding_another_alpaca_keeps_its_dependencies_for_the_child(tmp_path, monkeypatch):
    """t-452 at the launcher (probe r01 case F): a site-packages-like folder on the parent's path holds
    an alpaca-named distribution and a dependency. The child keeps the folder, so the dependency
    imports; its `alpaca` is still the parent's, and the distribution's code never runs."""
    from alpaca import child
    site = tmp_path / "site-packages"
    (site / "alpaca").mkdir(parents=True)
    marker = tmp_path / "DIST-ALPACA-RAN.txt"
    (site / "alpaca" / "__init__.py").write_text("open(%r, 'a').write('ran\\n')\n" % str(marker))
    (site / "alpaca" / "tests").mkdir()
    (site / "alpaca" / "tests" / "__init__.py").write_text("open(%r, 'a').write('ran\\n')\n" % str(marker))
    (site / "needed_dep_t452.py").write_text("X = 1\n")
    monkeypatch.setattr(sys, "path", [sys.path[0], str(site)] + list(sys.path[1:]))
    argv = child.python_argv("alpaca.tests.test_package_import_path", "probe", "needed_dep_t452")
    got = _child_report(argv, tmp_path, child.child_env())
    assert got["imports"] == {"needed_dep_t452": "imported"}, got
    assert got["file"] == str(REPO / "alpaca" / "__init__.py"), got
    assert os.path.realpath(str(site)) in _real(got["path"]), got
    assert not marker.exists(), marker.read_text()


def test_an_engine_process_the_boot_starts_passes_the_isolation_check(tmp_path):
    """The research engine's start check (RUN-ISOLATION-MISSING) accepts the process the wrapper
    starts, which has no install root on its path, and refuses one whose path holds the root (the
    legacy `.pth` layout run by hand) or that is not isolated."""
    script = tmp_path / "check.py"
    script.write_text("from alpaca.research import session_engine as e; print(e.isolation_ok())\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}

    def ok(argv):
        p = subprocess.run(argv, cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120)
        assert p.returncode == 0, p.stderr[-1200:]
        return p.stdout.strip().splitlines()[-1]

    assert ok([sys.executable, "-I", str(BOOT), str(script)]) == "True"
    # the legacy layout by hand: the install root on the path (first, so this tree's package is the
    # one found even where the venv's own legacy .pth names another checkout)
    legacy = "import sys, runpy; sys.path.insert(0, %r); runpy.run_path(%r)" % (str(REPO), str(script))
    assert ok([sys.executable, "-I", "-c", legacy]) == "False"
    assert ok([sys.executable, "-c", legacy]) == "False"


# ---------------------------------------------------------------------------- review of 3295509b9
def _stray_tree(where, marker, layout):
    """A copied Alpaca tree (as the in-process wiki mutation oracle left one on `sys.path` in t-070)
    whose `yaml.py` and `alpaca` append to `marker` when they run."""
    code = "open(%r, 'a').write(__name__ + ' ran\\n')\n" % str(marker)
    (where / "alpaca").mkdir(parents=True)
    (where / "alpaca" / "__init__.py").write_text(code)
    (where / "yaml.py").write_text(code)
    if layout == "boot":
        (where / "alpaca" / "boot.py").write_text(code)
    elif layout == "manifest":
        (where / "ALPACA-MANIFEST").write_text("[mechanism]\nalpaca/\n")
    return where


@pytest.mark.parametrize("layout", ["boot", "manifest"])
def test_a_stray_alpaca_tree_on_the_parents_path_runs_nothing_in_the_child(tmp_path, monkeypatch, layout):
    """Review of 3295509b9, m2 (probe section 9): a stray Alpaca tree first on the parent's path
    reached the child, and its `yaml.py` ran there. An entry that is an Alpaca tree (it holds
    `alpaca/boot.py`, or an `alpaca` package beside ALPACA-MANIFEST) is dropped
    from the child's path; the child still imports the real `yaml`."""
    from alpaca import child
    marker = tmp_path / "STRAY-TREE-RAN.txt"
    tree = _stray_tree(tmp_path / "straytree", marker, layout)
    monkeypatch.setattr(sys, "path", [str(tree)] + list(sys.path))
    argv = child.python_argv("alpaca.tests.test_package_import_path", "probe", "yaml")
    got = _child_report(argv, tmp_path, child.child_env())
    assert not marker.exists(), marker.read_text()
    assert got["imports"] == {"yaml": "imported"}, got
    assert os.path.realpath(str(tree)) not in _real(got["path"]), got
    assert got["file"] == str(REPO / "alpaca" / "__init__.py"), got


def test_the_isolation_check_needs_the_package_the_boot_loaded(tmp_path):
    """Review of 3295509b9, m4: `isolation_ok` also checks that `alpaca/boot.py` loaded `alpaca`
    from the install root. An isolated process that loaded the package some other way, with the
    root off its path, is refused."""
    script = tmp_path / "check.py"
    script.write_text("from alpaca.research import session_engine as e; print(e.isolation_ok())\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    by_hand = ("import sys, importlib.util, runpy; r = %r; "
               "sys.path[:] = [p for p in sys.path if p != r]; "
               "s = importlib.util.spec_from_file_location('alpaca', r + '/alpaca/__init__.py', "
               "submodule_search_locations=[r + '/alpaca']); m = importlib.util.module_from_spec(s); "
               "sys.modules['alpaca'] = m; s.loader.exec_module(m); runpy.run_path(%r)"
               ) % (str(REPO), str(script))
    p = subprocess.run([sys.executable, "-I", "-c", by_hand], cwd=str(tmp_path), env=env,
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr[-1200:]
    assert p.stdout.strip().splitlines()[-1] == "False", p.stdout
    p = subprocess.run([sys.executable, "-I", str(BOOT), str(script)], cwd=str(tmp_path), env=env,
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0 and p.stdout.strip().splitlines()[-1] == "True", (p.stdout, p.stderr[-800:])


def test_no_module_starts_a_multiprocessing_child():
    """Review of 3295509b9, m1: a multiprocessing `spawn` or `forkserver` child of a process the boot
    started gets the parent's path, which holds no `alpaca`, so it would fail, or import an
    alpaca-named distribution from site-packages. No product module starts one; a child starts
    through `alpaca.child.python_argv` (alpaca/boot.py says so)."""
    import ast
    found = []
    for path in sorted((REPO / "alpaca").rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        if rel.startswith("alpaca/tests/"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [node.module + "." + a.name for a in node.names]
            for name in names:
                if name.split(".")[0] == "multiprocessing" or name.endswith("ProcessPoolExecutor"):
                    found.append("%s:%d %s" % (rel, node.lineno, name))
    assert not found, found
    boot = BOOT.read_text(encoding="utf-8")
    assert "multiprocessing" in boot and "alpaca.child.python_argv" in boot


def test_the_isolation_texts_describe_the_boot():
    """Review of 3295509b9, m3: the RUN-ISOLATION-MISSING message and the capture runner's docstring
    no longer describe the `.pth` layout."""
    runtime = (REPO / "alpaca" / "research" / "session_runtime.py").read_text(encoding="utf-8")
    assert "holds the project root after the standard library" not in runtime
    assert "alpaca/boot.py" in runtime
    runner = (REPO / "alpaca" / "capture_runner.py").read_text(encoding="utf-8")
    assert "`.pth` slot" not in runner and "alpaca/boot.py" in runner


# ---------------------------------------------------------------------------- setup and doctor
def test_bootstrap_writes_no_root_pth_and_removes_a_legacy_one():
    """setup/bootstrap.sh no longer writes `alpaca-root.pth` (the whole install root on every path);
    it removes the one an earlier bootstrap wrote, and --check reports it."""
    text = (REPO / "setup" / "bootstrap.sh").read_text(encoding="utf-8")
    writes = [line for line in text.splitlines() if not line.lstrip().startswith("#")
              and re.search(r'>>?\s*"?[^\s&"]*(alpaca-root\.pth|ALPACA_BOOT_PTH)', line)]
    assert not writes, writes
    assert 'rm -f -- "$ALPACA_BOOT_PTH"' in text and "ALPACA_BOOT_PTH=" in text


def test_doctor_reports_a_legacy_root_pth(project):
    """`alpaca doctor` warns while the venv still holds a legacy `alpaca-root.pth` and says how to
    remove it; with none, the check is ok."""
    from alpaca import doctor
    site = Path(project, ".venv", "lib", "python3.99", "site-packages")
    site.mkdir(parents=True)

    def check():
        rows = [c for c in doctor.checks(project) if c["name"] == "research-isolation"]
        assert len(rows) == 1, rows
        return rows[0]["level"], rows[0]["detail"]

    level, why = check()
    assert level == "ok", why
    (site / "alpaca-root.pth").write_text(project + "\n")
    level, why = check()
    assert level == "warn" and "alpaca-root.pth" in why and "bootstrap" in why, why


if __name__ == "__main__" and sys.argv[1:2] == ["probe"]:
    # run in a child by the launcher tests: which `alpaca` ran, and which named modules imported
    import alpaca
    out = {"file": alpaca.__file__, "isolated": sys.flags.isolated, "path": list(sys.path),
           "imports": {}}
    for name in sys.argv[2:]:
        try:
            __import__(name)
            out["imports"][name] = "imported"
        except ImportError:
            out["imports"][name] = "missing"
    print(json.dumps(out))
