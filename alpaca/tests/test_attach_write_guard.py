"""The write guard: nothing in alpaca/attach/ writes to disk except alpaca/attach/safefs.py (op-062 t-647).

Fix rounds 1 to 3 each closed one writer that followed a link out of the studio and then missed the
next one (a plain open, a makedirs, a temp file at a guessable name). The rule now has one owner,
safefs, and this test fails the suite the day a second owner appears.

What it proves: no Python call by name that creates, replaces or makes a file or folder, and no
open for writing, exists in a file under alpaca/attach/ other than safefs.py. What it does not
prove: os.sendfile, mmap, ctypes or a shell redirect inside a child process are not seen, and the
folder a child process (git clone, git worktree add) writes into is a review item: its path must come
from safefs.make_dir (see its docstring). Deletes (unlink, rmdir, rmtree) are not writes and pass.
The record (alpaca/db.py) and project.yaml (alpaca/project.py) are written by modules every verb
shares, not by attach; the promise covers the files attach itself writes.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALLOWED = ("alpaca/attach/safefs.py",)
WRITE_CALLS = {"replace", "rename", "makedirs", "mkdir", "symlink", "link", "truncate", "mkfifo", "mkstemp",
               "mkdtemp", "NamedTemporaryFile", "write_text", "write_bytes", "touch", "copy", "copy2",
               "copyfile", "copytree", "move"}
WRITE_FLAGS = {"O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND"}
READ_MODE_CHARS = set("rbtU")
MODULES_OF_WRITES = {"os", "shutil", "tempfile"}


def _call_name(func):
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _is_safefs(func):
    """`safefs.<name>(...)`: the one module allowed to write (its own file is skipped by the scan)."""
    return isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "safefs"


def _flag_names(node):
    """The set of flag names an os.open flags expression is built from, or None when it is not built only
    from names, attributes, `|` and getattr(os, "NAME", <int>) (that form counts as os.NAME)."""
    if isinstance(node, ast.Name):
        return {node.id} if node.id.startswith("O_") else None
    if isinstance(node, ast.Attribute):
        return {node.attr} if node.attr.startswith("O_") else None
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return set()
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left, right = _flag_names(node.left), _flag_names(node.right)
        return None if left is None or right is None else left | right
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
            and len(node.args) == 3 and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str) and isinstance(node.args[2], ast.Constant)
            and isinstance(node.args[2].value, int)):
        return {node.args[1].value}
    return None


def scan_source(source, label):
    """`label:line what` for every write found in `source`."""
    hits = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in MODULES_OF_WRITES:
            for alias in node.names:
                if alias.name in WRITE_CALLS or alias.name == "open":
                    hits.append("%s:%d from %s import %s" % (label, node.lineno, node.module, alias.name))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname and alias.name in MODULES_OF_WRITES:
                    hits.append("%s:%d import %s as %s" % (label, node.lineno, alias.name, alias.asname))
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            is_os_open = (isinstance(node.func, ast.Attribute) and name == "open"
                          and isinstance(node.func.value, ast.Name) and node.func.value.id == "os")
            if is_os_open:
                flags = node.args[1] if len(node.args) > 1 else next(
                    (k.value for k in node.keywords if k.arg == "flags"), None)
                names = None if flags is None else _flag_names(flags)
                if names is None or names & WRITE_FLAGS:
                    hits.append("%s:%d os.open" % (label, node.lineno))
            elif name in ("open", "fdopen"):
                mode = node.args[1] if len(node.args) > 1 else next(
                    (k.value for k in node.keywords if k.arg == "mode"), None)
                if mode is not None and not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)
                                             and set(mode.value) <= READ_MODE_CHARS):
                    hits.append("%s:%d %s" % (label, node.lineno, name))
                elif name == "fdopen" and mode is None:
                    hits.append("%s:%d fdopen" % (label, node.lineno))
            elif name in WRITE_CALLS and not _is_safefs(node.func):
                hits.append("%s:%d %s" % (label, node.lineno, name))
    return sorted(hits, key=lambda h: (h.split(":")[0], int(h.split(":")[1].split()[0])))


def attach_files():
    base = ROOT / "alpaca" / "attach"
    return sorted(p for p in base.rglob("*.py") if p.relative_to(ROOT).as_posix() not in ALLOWED)


def test_no_module_in_alpaca_attach_writes_except_safefs():
    hits = []
    for path in attach_files():
        label = path.relative_to(ROOT).as_posix()
        hits += scan_source(path.read_text(encoding="utf-8"), label)
    assert not hits, ("alpaca/attach/ writes outside safefs.py (use safefs.write_bytes, make_dir or open_lock):\n  "
                      + "\n  ".join(hits))


def test_the_allow_list_is_exactly_safefs_and_it_exists():
    assert ALLOWED == ("alpaca/attach/safefs.py",)
    assert (ROOT / ALLOWED[0]).is_file()


def test_every_module_of_the_package_is_scanned():
    names = {p.name for p in attach_files()}
    assert {"config.py", "repo.py", "gitops.py", "sense.py", "health.py", "cli.py", "__init__.py",
            "sealkeep.py", "applay.py"} <= names
    assert "safefs.py" not in names


FLAGGED = {
    "open mode w": 'open(p, "w")',
    "open mode keyword": 'open(p, mode="ab")',
    "open mode variable": "open(p, m)",
    "os.open write": "os.open(p, os.O_WRONLY)",
    "os.open write with getattr": 'os.open(p, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))',
    "os.open flags variable": "os.open(p, flags)",
    "os.open no flags": "os.open(p)",
    "write_text": 'Path(p).write_text("x")',
    "write_bytes": 'Path(p).write_bytes(b"x")',
    "os.replace": "os.replace(a, b)",
    "from os import replace": "from os import replace",
    "tempfile.mkstemp": "tempfile.mkstemp()",
    "os.mkdir": "os.mkdir(p)",
    "makedirs": "os.makedirs(p, exist_ok=True)",
    "mkdir method": "Path(p).mkdir(parents=True)",
    "fdopen": 'os.fdopen(fd, "w")',
    "fdopen no mode": "os.fdopen(fd)",
    "os alias": "import os as o",
    "shutil.copy": "shutil.copy(a, b)",
    "os.symlink": "os.symlink(a, b)",
    "O_CREAT read flags": "os.open(p, os.O_RDONLY | os.O_CREAT)",
    "a bare write_bytes": 'write_bytes(root, p, b"x")',
    "another receiver named like safefs": 'self.safefs.write_bytes(p, b"x")',
    "write_text on a path": 'path.write_text("x")',
}
CLEAN = {
    "open default": "open(p)",
    "open rb": 'open(p, "rb")',
    "open encoding": 'open(p, encoding="utf-8")',
    "os.open read": "os.open(p, os.O_RDONLY | os.O_NOFOLLOW)",
    "the studio lock open": ('os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))'),
    "unlink": "os.unlink(p)",
    "rmdir": "os.rmdir(p)",
    "rmtree": "shutil.rmtree(p, ignore_errors=True)",
    "flock": "fcntl.flock(fd, fcntl.LOCK_EX)",
    "plain import os": "import os",
    "safefs write": 'safefs.write_bytes(root, p, b"x", mode=0o600)',
    "safefs make_dir": "safefs.make_dir(root, p, exclusive=True)",
}


def test_the_checker_flags_every_write_shape():
    missed = [name for name, src in FLAGGED.items() if not scan_source(src, "snippet")]
    assert not missed, "the guard does not flag: %s" % missed


def test_the_checker_passes_every_read_shape():
    wrong = {name: scan_source(src, "snippet") for name, src in CLEAN.items() if scan_source(src, "snippet")}
    assert not wrong, "the guard flags a read: %s" % wrong


# ---- t-648 fix round 1 (B1): no bare delete in the modules that delete records ------------------------------------------

#: modules whose deletes of studio files all go through `safefs.remove` (a bare os.unlink follows a link at a
#: folder of the path and deletes a file outside the studio). Fix round 2 (U1): `shutil.rmtree` of a path string
#: that `safefs.make_dir` returned earlier follows a link a swap put at a folder of the path since, so a folder
#: goes through `safefs.remove_tree` and a swapped link through `safefs.drop_link`.
DELETE_CHECKED = ("alpaca/attach/worktree.py", "alpaca/attach/baseline.py", "alpaca/attach/proven.py")
BARE_DELETES = {"unlink", "remove", "rmdir", "removedirs", "rmtree"}


def scan_deletes(source, label):
    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in BARE_DELETES \
                and not _is_safefs(node.func):
            hits.append("%s:%d %s" % (label, node.lineno, node.func.attr))
        elif isinstance(node, ast.ImportFrom) and node.module in MODULES_OF_WRITES:
            hits += ["%s:%d from %s import %s" % (label, node.lineno, node.module, a.name)
                     for a in node.names if a.name in BARE_DELETES]
    return hits


def test_records_are_deleted_through_safefs_only():
    hits = []
    for rel in DELETE_CHECKED:
        hits += scan_deletes((ROOT / rel).read_text(encoding="utf-8"), rel)
    assert not hits, "a bare delete in a checked module (use safefs.remove):\n  " + "\n  ".join(hits)


def test_the_delete_checker_flags_a_bare_unlink_and_passes_safefs_remove():
    assert scan_deletes("os.unlink(p)", "s") and scan_deletes("Path(p).unlink()", "s") and scan_deletes("os.remove(p)", "s")
    assert scan_deletes("from os import unlink", "s")
    assert not scan_deletes("safefs.remove(root, p)", "s") and not scan_deletes("safefs.remove_tree(root, p)", "s")
    assert scan_deletes("shutil.rmtree(p, ignore_errors=True)", "s") and scan_deletes("from shutil import rmtree", "s")
    assert not scan_deletes("safefs.drop_link(root, p)", "s")
