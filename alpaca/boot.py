"""Start a module or a script with this tree's `alpaca` package, loaded by its location (t-428, t-452).

Every Python process an Alpaca hook, verb, unit or child start runs is

    python -I <tree>/alpaca/boot.py [--path <json list>] (-m <module> | <script>) [args...]

`bin/alpaca-python --isolated` and `bin/alpaca-python -I -m` (the hooks, `bin/alpaca`, the generated
units) start it with the tree the wrapper sits in; `alpaca.child.python_argv` starts it with the
parent's package and the parent's import path in `--path`.

`-I` keeps the working folder, the script's folder, PYTHONPATH and the user site off `sys.path`.
This file then loads `alpaca` from `<tree>/alpaca/__init__.py` into `sys.modules` before anything
imports it, with the package folder as its only submodule location. So no search of `sys.path`
decides which `alpaca` runs: a stray `alpaca` anywhere on the path (an alpaca-named distribution in
site-packages, a copied tree some code left on the path) is never imported, and no path entry has to
be dropped for holding one, so every dependency folder stays. The install root itself is not on the
path, so nothing else in it is importable: a root that a legacy `alpaca-root.pth` names (earlier
bootstraps wrote one; a linked worktree's venv names the main checkout) is taken off the path, as
is this tree and every other Alpaca tree on it (`is_alpaca_tree`: a copied tree some code left on
the path would otherwise run its other modules, its `yaml.py` say, in the child; review of
3295509b9, m2). The loaded package carries `BOOT_MARK` (this tree), which the research engine's
isolation check reads.

Limits, by design:

- a Python child starts through `alpaca.child.python_argv`, never through multiprocessing: a
  `spawn` or `forkserver` child gets this process's path, which holds no `alpaca`, so it would fail
  to import the package, or import an alpaca-named distribution from site-packages (review m1;
  `test_no_module_starts_a_multiprocessing_child` pins that no module starts one);
- `importlib.reload(alpaca)` searches the path and finds no `alpaca`; nothing calls it.

Stdlib only, and nothing here runs on import: `import alpaca.boot` is a no-op.
"""
import os
import sys

#: the name of the .pth file earlier bootstraps wrote into the venv's site-packages
LEGACY_PTH = "alpaca-root.pth"
#: the attribute `load_package` sets on the package: the tree it loaded it from
BOOT_MARK = "__alpaca_boot_root__"
USAGE = "usage: python -I <tree>/alpaca/boot.py [--path JSON] (-m MODULE | SCRIPT) [ARGS...]\n"


def tree_root():
    """The tree this file belongs to: the folder that holds `alpaca/boot.py`."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def legacy_pth_roots():
    """The roots a legacy `alpaca-root.pth` in this interpreter's site folders names."""
    import site
    roots = []
    try:
        folders = list(site.getsitepackages())
    except Exception:  # noqa: BLE001 - an interpreter without site folders has no .pth
        folders = []
    for folder in folders:
        try:
            with open(os.path.join(folder, LEGACY_PTH), encoding="utf-8") as fh:
                line = fh.readline().strip()
        except OSError:
            continue
        if line:
            roots.append(line)
    return roots


def _real(path):
    return os.path.realpath(path or ".")


def is_alpaca_tree(entry):
    """True when the path entry is an Alpaca tree: it holds `alpaca/boot.py`, or an `alpaca` package
    beside ALPACA-MANIFEST (a tree from before the boot). A site folder that holds an alpaca-named
    distribution is neither, so its dependencies stay (t-452)."""
    try:
        folder = _real(entry)
        if os.path.isfile(os.path.join(folder, "alpaca", "boot.py")):
            return True
        return (os.path.isfile(os.path.join(folder, "alpaca", "__init__.py"))
                and os.path.isfile(os.path.join(folder, "ALPACA-MANIFEST")))
    except (OSError, ValueError):
        return False


def import_path(path, root):
    """`path` without `''`, `root`, every legacy `.pth` root and every other Alpaca tree."""
    gone = {_real(root)} | {_real(r) for r in legacy_pth_roots()}
    return [str(p) for p in path if p and _real(str(p)) not in gone and not is_alpaca_tree(str(p))]


def load_package(root):
    """Load `<root>/alpaca` as the `alpaca` package into `sys.modules` (as the import system would,
    with the package folder as its only submodule location) and return it."""
    import importlib.util
    folder = os.path.join(root, "alpaca")
    spec = importlib.util.spec_from_file_location(
        "alpaca", os.path.join(folder, "__init__.py"), submodule_search_locations=[folder])
    if spec is None or spec.loader is None:
        raise ImportError("no alpaca package at %s" % folder, name="alpaca")
    module = importlib.util.module_from_spec(spec)
    sys.modules["alpaca"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop("alpaca", None)
        raise
    setattr(module, BOOT_MARK, root)
    return module


def main(argv):
    args = list(argv)
    path = sys.path
    if args[:1] == ["--path"] and len(args) >= 2:
        import json
        path = json.loads(args[1])
        args = args[2:]
    if args[:1] == ["-m"] and len(args) >= 2:
        module, script, rest = args[1], None, args[2:]
    elif args and not args[0].startswith("-"):
        module, script, rest = None, args[0], args[1:]
    else:
        sys.stderr.write(USAGE)
        return 2
    root = tree_root()
    sys.path[:] = import_path(path, root)
    load_package(root)
    import runpy
    if module is not None:
        sys.argv[:] = [sys.argv[0]] + rest
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    else:
        sys.argv[:] = [script] + rest
        runpy.run_path(script, run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
