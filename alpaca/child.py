"""The one launcher for a Python child that an Alpaca hook, verb or collector starts (t-070 round 2,
class C-A; the package by its location since t-428 and t-452).

The hooks run `bin/alpaca-python --isolated`, which is `python -I <tree>/alpaca/boot.py`: the
working folder and PYTHONPATH stay off `sys.path`, so a module an agent plants in the project never
runs in an engine process, and `alpaca` is loaded from the tree's own folder (alpaca/boot.py). A
child started as `[sys.executable, "-m", ...]` with `cwd=root` puts the root first on its path and
undoes that (check round 1, B-1). `-I -m` alone is not enough either: a path search then decides
which `alpaca` runs, and a venv's legacy `alpaca-root.pth` names one install root, so a child
started from another tree (a linked worktree) would import that tree's `alpaca`.

The rule:

1. the child runs with `-I`, through `alpaca/boot.py` of the package the parent imported, which
   loads that package by its location before the module runs, so no path entry decides which
   `alpaca` runs (t-428);
2. its import path is the parent's, without `''`, the working folder, the folders the caller names
   (the served root), the parent's PYTHONPATH entries, the parent's package root and every root a
   legacy `alpaca-root.pth` names, and every other Alpaca tree (`alpaca.boot.is_alpaca_tree`: a
   copied tree some code left on the path would run its other modules in the child; review of
   3295509b9, m2). Nothing else is dropped: a folder that also holds another `alpaca` (an
   alpaca-named distribution in site-packages) keeps its dependencies for the child, and its
   `alpaca` never runs (t-452; review of 97b59a438, m2);
3. its environment has no PYTHONPATH, and with a served root its `SQLITE_TMPDIR` is that root's
   folder (`alpaca.sqlite_tmpdir`, prepared with no link followed).

`python -m alpaca.child describe` prints what a child sees (its package, path, flags and SQLite
folder). The live server's import probe is `alpaca.import_probe`, kept out of this module because
this module is in the research engine's static import closure, where no file may import a module by
a computed name (test_research_session_engine.test_no_engine_file_loads_a_module_by_a_computed_name).
Stdlib only.
"""
import json
import os
import sys

from alpaca import boot

#: the start file of a tree, below its root (alpaca/boot.py)
BOOT = ("alpaca", "boot.py")
#: the name of the .pth file earlier bootstraps wrote into the venv's site-packages (legacy)
PTH_NAME = boot.LEGACY_PTH


def package_root():
    """The folder that holds the `alpaca` package this process imported."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def boot_path(root=None):
    """The start file that loads the package of `root` (default: this process's package root)."""
    return os.path.join(root or package_root(), *BOOT)


def _pth_roots():
    """The roots a legacy `alpaca-root.pth` names (`alpaca.boot.legacy_pth_roots`)."""
    return boot.legacy_pth_roots()


def _real(path):
    return os.path.realpath(path or ".")


def child_path(drop=()):
    """The import path a child gets (rule 2)."""
    gone = {_real(package_root())}
    try:
        gone.add(_real(os.getcwd()))
    except OSError:
        pass
    gone |= {_real(p) for p in drop if p}
    gone |= {_real(p) for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p}
    gone |= {_real(p) for p in _pth_roots()}
    out = []
    for entry in sys.path:
        if entry and entry not in out and _real(entry) not in gone and not boot.is_alpaca_tree(entry):
            out.append(entry)
    return out


def python_argv(module, *args, served_root=None, drop=()):
    """The argv that runs `python -m <module> <args...>` as a confined child (rules 1 and 2)."""
    path = child_path(drop=tuple(drop) + ((served_root,) if served_root else ()))
    return ([sys.executable, "-I", boot_path(), "--path", json.dumps(path), "-m", module]
            + [str(a) for a in args])


def child_env(served_root=None, base=None):
    """The environment of a confined child (rule 3): `base` (default os.environ) without PYTHONPATH;
    with `served_root`, `SQLITE_TMPDIR` is that root's folder when it can be prepared. A folder the
    operator chose (one that is not an Alpaca runtime folder) is kept."""
    env = dict(os.environ if base is None else base)
    env.pop("PYTHONPATH", None)
    if served_root:
        import alpaca
        inherited = env.get("SQLITE_TMPDIR")
        if not inherited or inherited.rstrip(os.sep).endswith(os.path.join(*alpaca.SQLITE_TMP)):
            fresh = {}
            if alpaca.sqlite_tmpdir(root=str(served_root), environ=fresh):
                env["SQLITE_TMPDIR"] = fresh["SQLITE_TMPDIR"]
    return env


def describe(root=None, **_):
    """What this process sees: its `alpaca`, its path and flags, its SQLite folder."""
    import alpaca
    return {"package": os.path.dirname(os.path.dirname(os.path.abspath(alpaca.__file__))),
            "path": list(sys.path), "isolated": sys.flags.isolated, "argv": list(sys.argv),
            "cwd": os.getcwd(), "sqlite_tmpdir": os.environ.get("SQLITE_TMPDIR"),
            "pythonpath": os.environ.get("PYTHONPATH")}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["describe"]:
        print(json.dumps(describe()))
        return 0
    sys.stderr.write("usage: python -m alpaca.child describe\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
