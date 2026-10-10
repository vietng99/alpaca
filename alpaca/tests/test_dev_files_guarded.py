"""Guard: a shipped test that names a development-only path does so through alpaca/tests/devfiles.py
(op-008 t-072b).

Release check 08a13fc83f8b failed clean-pytest because shipped tests read tools/, packs/, design/
and docs/ files that a release candidate does not ship. The guard reads every shipped test file,
collects the repository paths it names (string literals, and the literal parts of os.path.join,
Path(...) and the / operator), and requires each one that is a development-only path (tracked
here, outside the release ship set) to be covered by a needs_dev_files or require_dev_files call
in the same file, naming that path or a folder above it.

It needs the development tree's git index to know which paths are development-only; a release tree
tracks only shipped files, so there it has nothing to check.
"""
import ast
import os
import re
import subprocess
from pathlib import Path

import pytest

from alpaca.tests import devfiles

REPO = devfiles.REPO
HELPERS = {"needs_dev_files", "require_dev_files", "names_as_data"}
PATHLIKE = re.compile(r"^[A-Za-z0-9_.\-]+(/[A-Za-z0-9_.\-]+)*/?$")


def _tracked(root):
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return [rel for rel in out.decode().split("\0") if rel]


def dev_only_index(root=REPO):
    """(files, folders): the tracked development-only files, and the folders that hold only such
    files. The memory class (.alpaca/ and the rest) is runtime state, not a development file."""
    tracked = _tracked(root)
    if tracked is None:
        return None
    memory = [e.strip("/") for e in devfiles.manifest.classes(root)["memory"]]
    files, shipped_dirs, dirs = set(), set(), set()
    for rel in tracked:
        if devfiles.is_memory(rel, memory):
            continue
        parents = [rel.rsplit("/", i)[0] for i in range(1, rel.count("/") + 1)]
        if devfiles.ships(rel, root):
            shipped_dirs.update(parents)
        else:
            files.add(rel)
            dirs.update(parents)
    return files, dirs - shipped_dirs


def _const(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _runs(parts):
    """Each run of literal parts of a path join, joined: join(REPO, "tools", "x.py") -> tools/x.py,
    join(REPO, "packs", "refpack", name) -> packs/refpack."""
    runs, run = [], []
    for part in list(parts) + [None]:
        value = _const(part) if part is not None else None
        if value is None:
            if run:
                runs.append("/".join(p for p in run if p))
            run = []
        else:
            run.append(value.strip("/"))
    return [r for r in runs if r]


def _div_parts(node):
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _div_parts(node.left) + [node.right]
    return [node]


def _scopes(tree):
    """{id(node): (enclosing class and function nodes, outermost first)}; a decorator belongs to
    the function or class it decorates."""
    out = {id(tree): ()}
    for node in ast.walk(tree):
        here = out[id(node)]
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            here = here + (node,)
        for child in ast.iter_child_nodes(node):
            out[id(child)] = here
    return out


def named_paths(tree):
    """([(path, line, scope)], [(path, scope)]): every repository-like path of two or more parts
    the module names, and the paths its helper calls cover, each with its scope (_scopes). A path
    is a literal with a separator ("packs/refpack"), or a run of literal parts of a join or a call:
    os.path.join(REPO, "tools", "x.py"), REPO / "design" / "notes.md", _read("design", "notes.md").
    Docstrings are left out. A single part ("design", "tools") is not counted: it is too often a
    phase name or a folder of a test's own scratch repository."""
    scopes = _scopes(tree)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and _const(body[0].value) is not None:
                docstrings.add(id(body[0].value))
    # REPO / "tools" / "x.py" is one path: the inner REPO / "tools" is not a second one.
    inner = {id(node.left) for node in ast.walk(tree)
             if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)}
    named, covered = [], []
    for node in ast.walk(tree):
        values = []
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in HELPERS:
                covered.extend((_const(a).strip("/"), scopes[id(node)]) for a in node.args if _const(a))
                continue
            values = _runs(([func.value] if name == "joinpath" else []) + list(node.args))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and id(node) not in inner:
            values = _runs(_div_parts(node))
        elif _const(node) is not None and id(node) not in docstrings:
            values = [node.value.strip("/")]
        for value in values:
            value = value[2:] if value.startswith("./") else value
            if "/" in value and PATHLIKE.match(value):
                named.append((value, node.lineno, scopes[id(node)]))
    return named, covered


def _covers(helper_scope, scope):
    """A helper call covers a path named in a function when it sits in that function (its body or
    its decorators), in a class around it, or at module level. A path named outside any function (a
    module or class constant) is used wherever the module likes, so any helper call covers it."""
    if not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in scope):
        return True
    return scope[:len(helper_scope)] == helper_scope


def unguarded(path, index):
    """The development-only paths the file at path names outside the helper (names_as_data
    covers a path a module names only as data)."""
    files, dirs = index
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    named, covered = named_paths(ast.parse(source, filename=path))
    out = []
    for rel, line, scope in named:
        if rel not in files and rel not in dirs:
            continue
        if any((rel == c or rel.startswith(c + "/") or c.startswith(rel + "/")) and _covers(where, scope)
               for c, where in covered):
            continue
        out.append("%s:%d names %s" % (os.path.relpath(path, REPO), line, rel))
    return out


def shipped_tests(root=REPO):
    base = os.path.join(root, "alpaca", "tests")
    for folder, dirs, names in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(names):
            if name.endswith(".py"):
                path = os.path.join(folder, name)
                if devfiles.ships(os.path.relpath(path, root), root):
                    yield path


def test_every_shipped_test_names_development_files_through_the_helper():
    index = dev_only_index()
    if index is None or not index[0]:
        pytest.skip("no git index of a development tree here: a release tree tracks only shipped files")
    found = [hit for path in shipped_tests() for hit in unguarded(path, index)]
    assert not found, ("shipped tests name development-only paths without alpaca/tests/devfiles.py "
                       "(needs_dev_files / require_dev_files):\n" + "\n".join(found))


def test_the_guard_sees_a_join_a_literal_and_the_helper(tmp_path):
    index = ({"lab/probe.py", "kits/demo/a.yaml"}, {"lab", "kits", "kits/demo"})
    bad = tmp_path / "test_bad.py"
    bad.write_text('import os\nREPO = "."\nTOOL = os.path.join(REPO, "lab", "probe.py")\n'
                   'PACK = "kits/demo/a.yaml"\nBENCH = REPO / "kits" / "demo"\n')
    assert [h.rsplit(" names ", 1)[1] for h in unguarded(str(bad), index)] == \
        ["lab/probe.py", "kits/demo/a.yaml", "kits/demo"]
    good = tmp_path / "test_good.py"
    good.write_text('"""Reads lab/probe.py."""\nimport os\n'
                    'from alpaca.tests.devfiles import needs_dev_files, require_dev_files\n'
                    'require_dev_files("kits/demo")\npytestmark = needs_dev_files("lab/probe.py")\n'
                    'REPO = "."\nTOOL = os.path.join(REPO, "lab", "probe.py")\n'
                    'PACK = "kits/demo/a.yaml"\nBENCH = REPO / "kits" / "demo"\n')
    assert unguarded(str(good), index) == []


def test_the_helper_refuses_a_shipped_path_skips_an_absent_file_and_fails_a_lost_one(tmp_path,
                                                                                  monkeypatch):
    with pytest.raises(ValueError, match="ships with the release"):
        devfiles.missing("alpaca/cli.py")
    assert devfiles.ships("MANIFEST.json") and not devfiles.ships("tools/probe.py")
    # a tree that does not carry the file and does not track it (a release): the test skips
    tree = tmp_path / "tree"
    (tree / "tools").mkdir(parents=True)
    (tree / "ALPACA-MANIFEST").write_text((Path(REPO) / "ALPACA-MANIFEST").read_text())
    monkeypatch.setattr(devfiles, "REPO", str(tree))
    mark = devfiles.needs_dev_files("tools/probe.py")
    assert mark.name == "skip" and "tools/probe.py" in mark.kwargs["reason"]
    with pytest.raises(pytest.skip.Exception):
        devfiles.require_dev_files("tools/probe.py")
    # present: the test runs
    (tree / "tools" / "probe.py").write_text("x = 1\n")
    assert devfiles.needs_dev_files("tools/probe.py").name == "dev_files"
    devfiles.require_dev_files("tools/probe.py")
    # tracked by the tree's git but gone from its work tree (a development tree lost it): it fails
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    subprocess.run(["git", "-C", str(tree), "add", "tools/probe.py"], check=True)
    (tree / "tools" / "probe.py").unlink()
    mark = devfiles.needs_dev_files("tools/probe.py")
    assert mark.name == "dev_files_lost" and "lost" in mark.kwargs["reason"]
    with pytest.raises(pytest.fail.Exception):
        devfiles.require_dev_files("tools/probe.py")
