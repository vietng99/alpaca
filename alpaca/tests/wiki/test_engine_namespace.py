"""M2.8 namespace separation (Alpaca-native, no upstream).

The engine carries its own clock and determinism. They are vendored UNDER alpaca.wiki
(alpaca/wiki/clock.py, alpaca/wiki/determinism.py), never alpaca/clock.py or alpaca/determinism.py, so the 21
vendored importers resolve inside alpaca.wiki unchanged. This module walks both trees by AST and
fails on a crossing import in EITHER direction:

  * no module under alpaca/wiki/ imports alpaca.clock or alpaca.determinism;
  * no module outside alpaca/wiki/ imports alpaca.wiki.clock or alpaca.wiki.determinism.

Relative imports are resolved to their absolute dotted names exactly as CPython does, so a
`from ..clock import` inside alpaca.wiki.store (which resolves to alpaca.wiki.clock) is correctly read as
in-namespace and NOT flagged.
"""
import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
WIKI = os.path.join(ROOT, "alpaca", "wiki")
# The self-tests ride under the harness package at alpaca/tests/ (P-002). They are not production
# code and are allowed to reach the internal wiki modules under test, so the production-only
# namespace guard skips the whole test tree.
TESTS = os.path.join(ROOT, "alpaca", "tests")

_INSIDE_FORBIDDEN = ("alpaca.clock", "alpaca.determinism")
_OUTSIDE_FORBIDDEN = ("alpaca.wiki.clock", "alpaca.wiki.determinism")


def _resolve(name, package, level):
    """CPython's importlib._bootstrap._resolve_name, inlined."""
    if level == 0:
        return name
    bits = package.rsplit(".", level - 1)
    base = bits[0]
    return "{}.{}".format(base, name) if name else base


def _package_of(path):
    """The dotted package that anchors a file's relative imports (its containing dir)."""
    rel = os.path.relpath(os.path.dirname(path), ROOT)
    return rel.replace(os.sep, ".")


def _imported_modules(source, package):
    """Every absolute dotted module name a source file imports (relative ones resolved)."""
    out = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve(node.module or "", package, node.level)
            out.append(base)
            for alias in node.names:
                out.append("{}.{}".format(base, alias.name) if base else alias.name)
    return out


def _hits(source, package, forbidden):
    hits = []
    for mod in _imported_modules(source, package):
        for bad in forbidden:
            if mod == bad or mod.startswith(bad + "."):
                hits.append(mod)
    return hits


def _py_files(base):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


class EngineNamespace(unittest.TestCase):
    def test_no_wiki_module_imports_alpaca_clock_or_alpaca_determinism(self):
        offenders = {}
        for path in _py_files(WIKI):
            with open(path, encoding="utf-8") as fh:
                hits = _hits(fh.read(), _package_of(path), _INSIDE_FORBIDDEN)
            if hits:
                offenders[os.path.relpath(path, ROOT)] = hits
        self.assertEqual(offenders, {},
                         "alpaca/wiki modules must never reach alpaca.clock / alpaca.determinism")

    def test_no_outside_module_imports_alpaca_wiki_clock_or_determinism(self):
        offenders = {}
        alpaca_root = os.path.join(ROOT, "alpaca")
        for path in _py_files(alpaca_root):
            if os.path.abspath(path).startswith(os.path.abspath(WIKI) + os.sep):
                continue
            if os.path.abspath(path).startswith(os.path.abspath(TESTS) + os.sep):
                continue
            with open(path, encoding="utf-8") as fh:
                hits = _hits(fh.read(), _package_of(path), _OUTSIDE_FORBIDDEN)
            if hits:
                offenders[os.path.relpath(path, ROOT)] = hits
        self.assertEqual(offenders, {},
                         "nothing outside alpaca/wiki may import alpaca.wiki.clock / alpaca.wiki.determinism")

    # ---- the walker itself must catch a real crossing (negative fixtures) -----------
    def test_walker_flags_an_inside_crossing(self):
        # An absolute reach from inside alpaca.wiki is a violation.
        self.assertTrue(_hits("from alpaca.clock import now_iso\n", "alpaca.wiki.store", _INSIDE_FORBIDDEN))
        self.assertTrue(_hits("import alpaca.determinism\n", "alpaca.wiki.store", _INSIDE_FORBIDDEN))

    def test_walker_does_not_flag_in_namespace_relative_import(self):
        # `from ..clock import` inside alpaca.wiki.store resolves to alpaca.wiki.clock - in namespace, OK.
        self.assertEqual([], _hits("from ..clock import Clock\n", "alpaca.wiki.store", _INSIDE_FORBIDDEN))
        self.assertEqual(
            [], _hits("from ..determinism import sha256_hex\n", "alpaca.wiki.store", _INSIDE_FORBIDDEN))

    def test_walker_flags_an_outside_crossing(self):
        self.assertTrue(_hits("import alpaca.wiki.clock\n", "alpaca.cli", _OUTSIDE_FORBIDDEN))
        self.assertTrue(
            _hits("from alpaca.wiki.determinism import canonical_json\n", "alpaca.export", _OUTSIDE_FORBIDDEN))


if __name__ == "__main__":
    unittest.main()
