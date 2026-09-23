"""proportionality.py - THE non-adoption tripwire.

D9/D10 (spec) fix the runtime: Claude Code sessions, the Workflow tool, hooks and skills,
with MCP as the one plug point for anything external. Orchestration frameworks and a
long-running network service are deliberately NOT adopted. This instrument keeps those
non-adoptions honest: it FAILs when a forbidden import or a forbidden name appears anywhere
under `alpaca/`, and PASSes on a tree that stays within the sanctioned runtime.

The forbidden list is DATA, read from `project.yaml` (the `non_adoptions` block), never from
code. The one live exception is `alpaca serve` (P-006): a read-only dashboard that binds 127.0.0.1
only, the single sanctioned local reader. That exception is listed explicitly in the same block
with its justification and is scoped to its own file and its own server modules, so any OTHER
file that reaches for a server module is still a FAIL.

Two passes, so neither can hide a reference from the other:
  * AST over every `*.py`: static imports (`import x`, `from x import y`), the submodule form
    (`from pkg import sub` matching `pkg.sub`), and dynamic import by name
    (`importlib.import_module("x")`, `__import__("x")`).
  * Text over every readable file: a whole-token match, so a string reference on a doc surface
    (a docstring, a markdown note, the HTML shell) is caught too.

It NEVER imports or executes a candidate: a planted module must not buy code execution inside
the tripwire, so every check is AST-only or plain text.
"""
from __future__ import annotations

import ast
import os
import re
import sys

from alpaca.gates import verdict as _v

INSTRUMENT = "proportionality"

# "tests" is skipped: the harness self-tests ride under alpaca/tests/ (P-002) and legitimately name
# the non-adopted runtimes as violating fixtures. The tripwire binds production code under alpaca/,
# never the self-tests, so the whole test tree is out of the scan.
_SKIP_DIRS = frozenset({"__pycache__", ".git", ".venv", "venv", ".mypy_cache",
                        ".pytest_cache", ".ruff_cache", "node_modules", ".tox", ".eggs",
                        "tests"})

# Files the text pass reads. Anything not here is treated as opaque and skipped, so a binary
# blob never crashes the scan; a doc surface (.md, .html, .txt) is read so a string reference
# in prose is still caught.
_TEXT_EXT = frozenset({".py", ".md", ".rst", ".txt", ".html", ".htm", ".cfg", ".ini",
                       ".toml", ".yaml", ".yml", ".json", ".sh", ".csv", ".tsv"})


# --------------------------------------------------------------------- project.yaml intake
def load_non_adoptions(root):
    """Return (forbidden, sanctioned) read from `project.yaml`. Forbidden is a list of names;
    sanctioned is a list of {path, names, why}. Both default to empty when the block is absent,
    so the tripwire is inert rather than crashing on a project that never declared any."""
    from alpaca import project
    doc = project.load(root) or {}
    block = doc.get("non_adoptions") or {}
    forbidden = list(block.get("forbidden") or [])
    sanctioned = list(block.get("sanctioned") or [])
    return forbidden, sanctioned


def _sanctioned_map(root, sanctioned):
    """{relpath (posix): set(names)} from the sanctioned list. A relpath with an empty name set
    is exempt for every forbidden name; a non-empty set exempts only those names, so an
    unlisted server module in a sanctioned file is still reported."""
    out = {}
    for entry in (sanctioned or []):
        if not isinstance(entry, dict):
            continue
        rel = str(entry.get("path") or "").replace("\\", "/").strip()
        if not rel:
            continue
        names = entry.get("names")
        out[rel] = set(names) if names else set()
    return out


# ------------------------------------------------------------------------- matching helpers
def _matches(name, ref):
    """A forbidden `name` matches an imported dotted `ref` when they are equal or `ref` is a
    submodule of `name`. It does NOT match a parent: importing a top package alone is not
    importing a forbidden submodule of it."""
    return ref == name or ref.startswith(name + ".")


def _import_refs(tree):
    """Yield (lineno, ref) dotted-module references for every static and dynamic import in a
    parsed module. `from pkg import sub` yields both `pkg` and `pkg.sub` so the submodule form
    of a forbidden name is caught. Relative imports (level > 0) are internal and skipped."""
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name:
                    yield (n.lineno, a.name)
        elif isinstance(n, ast.ImportFrom):
            if (n.level or 0) > 0:
                continue
            mod = n.module or ""
            if not mod:
                continue
            yield (n.lineno, mod)
            for a in n.names:
                if a.name and a.name != "*":
                    yield (n.lineno, "%s.%s" % (mod, a.name))
        elif isinstance(n, ast.Call):
            f = n.func
            is_dyn = (isinstance(f, ast.Name) and f.id == "__import__") or \
                     (isinstance(f, ast.Attribute) and f.attr == "import_module")
            if is_dyn and n.args:
                a0 = n.args[0]
                if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                    yield (n.lineno, a0.value)


def _text_pattern(name):
    """A whole-token, case-insensitive matcher for `name`. Boundaries on both sides keep a
    short name out of a longer word that merely contains it, and keep a dotted name out of a
    deeper dotted path, while a bare mention in prose still matches."""
    return re.compile(r"(?<![\w.])" + re.escape(name) + r"(?![\w.])", re.IGNORECASE)


# ----------------------------------------------------------------------------- the scan
def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def scan(root, forbidden, sanctioned=None):
    """Return {relpath: [(name, kind, lineno), ...]} for every forbidden reference under
    `alpaca/`. `kind` is "import" (AST) or "text". A reference exempted by the sanctioned map for
    its file is dropped. One tuple per (file, name, kind, lineno)."""
    root = os.path.abspath(root)
    tree_root = os.path.join(root, "alpaca")
    exempt = _sanctioned_map(root, sanctioned)
    names = [str(x) for x in (forbidden or []) if str(x).strip()]
    patterns = [(nm, _text_pattern(nm)) for nm in names]
    found = {}

    def _add(rel, name, kind, lineno):
        allowed = exempt.get(rel)
        if allowed is not None and (not allowed or name in allowed):
            return
        found.setdefault(rel, []).append((name, kind, lineno))

    if not os.path.isdir(tree_root):
        return found
    for dirpath, dirnames, filenames in os.walk(tree_root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in sorted(filenames):
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            ext = os.path.splitext(fn)[1].lower()
            text = _read(path) if ext in _TEXT_EXT else None
            if text is None:
                continue
            seen = set()
            if ext == ".py":
                try:
                    parsed = ast.parse(text)
                except SyntaxError:
                    parsed = None
                if parsed is not None:
                    for lineno, ref in _import_refs(parsed):
                        for nm in names:
                            if _matches(nm, ref):
                                key = (nm, "import", lineno)
                                if key not in seen:
                                    seen.add(key)
                                    _add(rel, nm, "import", lineno)
            for nm, pat in patterns:
                for m in pat.finditer(text):
                    lineno = text.count("\n", 0, m.start()) + 1
                    key = (nm, "text", lineno)
                    if key not in seen:
                        seen.add(key)
                        _add(rel, nm, "text", lineno)
    return found


def check(root, forbidden, sanctioned=None):
    """Verdict code for the `alpaca/` tree against a forbidden list: FAIL on any un-sanctioned
    reference, PASS otherwise. This is the interface the boot-check and the regression suite
    consume; it takes the list as an argument so the caller owns where the data came from."""
    return _v.FAIL if scan(root, forbidden, sanctioned) else _v.PASS


# ------------------------------------------------------------------------------- reporting
def _report(found):
    for rel in sorted(found):
        for name, kind, lineno in found[rel]:
            print("  FORBIDDEN %-16s %s:%d  (%s)" % (name, rel, lineno, kind))


def main(argv=None) -> int:
    ap = _v.make_parser(
        name=INSTRUMENT,
        description="non-adoption tripwire: FAIL when a forbidden import or name appears under "
                    "alpaca/ (the list is data in project.yaml, never code)")
    ap.add_argument("--root", default=None, help="project root (default: discovered)")
    a = ap.parse_args(argv)

    if a.root:
        root = os.path.abspath(a.root)
    else:
        from alpaca import paths
        root = paths.root()

    forbidden, sanctioned = load_non_adoptions(root)
    found = scan(root, forbidden, sanctioned)
    print("  proportionality -- %d forbidden name(s), %d sanctioned file(s)"
          % (len(forbidden), len(sanctioned)))
    if found:
        _report(found)
        n = sum(len(v) for v in found.values())
        return _v.emit_verdict(INSTRUMENT, _v.FAIL,
                               "%d forbidden reference(s) across %d file(s)"
                               % (n, len(found)))
    return _v.emit_verdict(INSTRUMENT, _v.PASS,
                           "no forbidden imports or names under alpaca/")


if __name__ == "__main__":
    sys.exit(main())
