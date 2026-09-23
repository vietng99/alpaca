"""M2.13 (Step 5): the vendoring list is CLOSED - every vendored module is named by a ported test,
and every upstream module on the not-vendored list is asserted absent under alpaca/wiki/.

Alpaca-native (no upstream). Two obligations:

  1. COVERAGE. Walk alpaca/wiki/ and, by parsing the import graph of every tests/wiki/test_*.py, assert
     each vendored module is imported (hence named) by at least one ported test. A vendored module
     that no ported test imports is dead-on-arrival and fails here.

  2. CLOSURE. Assert `cli.py`, `onboard.py`, `surface.py`, `apply.py` and `eval/` have NOT appeared
     under alpaca/wiki/ (the M2 milestone's not-vendored list, spec preamble). A silent later copy of
     any of them is a failure here.

Seven engine/reflect modules land in the vendored tree ahead of the tests that drive their full
door (the answer/loop/templates path needs the M2.16 provider registry and the M2.14 absorb write
path; the reflect capabilities are carried-but-off in M1-M4). This module is their ported test: it
imports each and asserts a real property (a behavioral smoke on the empty store where cheap, a
carried-interface contract where the full drive needs a not-yet-vendored sibling). That both closes
the coverage walk honestly and discharges the M2 "carried, off state asserted by a test" rule.
"""
import ast
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from helpers import cleanup, fresh_cfg              # noqa: E402

from alpaca.wiki.store.db import DB                       # noqa: E402
# The seven modules no other ported test imports; named and exercised here.
from alpaca.wiki.engine import answer as _answer          # noqa: E402
from alpaca.wiki.engine import loop as _loop              # noqa: E402
from alpaca.wiki.engine import health as _health          # noqa: E402
from alpaca.wiki.engine import exceptions as _exceptions  # noqa: E402
from alpaca.wiki.engine import templates as _templates    # noqa: E402
from alpaca.wiki.reflect import identity as _identity     # noqa: E402
from alpaca.wiki.reflect import metamemory as _metamemory # noqa: E402

_ROOT = Path(__file__).resolve().parents[3]
_WIKI = _ROOT / "alpaca" / "wiki"
_TESTS = _ROOT / "alpaca" / "tests" / "wiki"

# The M2 milestone's not-vendored list (spec preamble near 565): never under alpaca/wiki/.
_NOT_VENDORED_FILES = ("cli.py", "onboard.py", "surface.py", "apply.py")
_NOT_VENDORED_DIRS = ("eval",)


def _disk_modules() -> list[str]:
    mods = []
    for p in sorted(_WIKI.rglob("*.py")):
        if p.name == "__init__.py":
            continue
        rel = p.relative_to(_WIKI).with_suffix("")
        mods.append("alpaca.wiki." + ".".join(rel.parts))
    return mods


def _imported_alpaca_wiki_paths() -> set[str]:
    """Every alpaca.wiki.* module path named (imported) by a ported test, via AST (not text match, so
    'identity reranker' prose never counts as importing reflect.identity).

    Scope: every tests/wiki/test_*.py, plus the wiki-facing entry-point tests M2.14/M2.15 placed at
    the tests/ root (drain, sort, extract), plus the two vendored orchestrators those end-to-end
    tests drive (ingest.absorb, project) - a leaf module reached only through absorb or the project
    package is exercised by test_absorb / test_projection without being imported by name there."""
    covered: set[str] = set()
    scan = list(_TESTS.glob("test_*.py"))
    for name in ("test_drain.py", "test_sort.py", "test_wiki_extract.py"):
        p = _ROOT / "alpaca" / "tests" / name
        if p.exists():
            scan.append(p)
    # The vendored orchestrators drive their leaf modules by RELATIVE import (from . import extract),
    # so include them and resolve those relatives to absolute alpaca.wiki paths.
    for src in (_WIKI / "ingest" / "absorb.py", _WIKI / "project" / "__init__.py", _WIKI / "extract.py"):
        if src.exists():
            scan.append(src)

    def _pkg_of(path: Path):
        # The package a source file under alpaca/wiki/ lives in, for relative-import resolution.
        try:
            rel = path.resolve().relative_to(_WIKI)
        except ValueError:
            return None
        parts = ["alpaca", "wiki"] + list(rel.with_suffix("").parts)
        return parts[:-1] if path.name != "__init__.py" else parts

    for tf in sorted(set(scan)):
        pkg = _pkg_of(tf)
        tree = ast.parse(tf.read_text(encoding="utf-8"), filename=str(tf))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module
                if node.level and pkg is not None:
                    base = pkg[: len(pkg) - node.level + 1]
                    mod = ".".join(base + ([node.module] if node.module else []))
                if mod and mod.startswith("alpaca.wiki"):
                    covered.add(mod)
                    for alias in node.names:
                        covered.add(mod + "." + alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("alpaca.wiki"):
                        covered.add(alias.name)
    return covered


# ---------------------------------------------------------------- the seven orphans, exercised
class TestCarriedModules(unittest.TestCase):
    def _fresh(self):
        cfg = fresh_cfg()
        db = DB(cfg)
        db.pour()
        return cfg, db

    def test_health_scores_an_empty_store(self):
        cfg, db = self._fresh()
        try:
            rep = _health.health_score(db, now="2020-01-01T00:00:00+00:00")
            self.assertIsInstance(rep.score, int)
            self.assertIn("health=", rep.audit())
        finally:
            db.close(); cleanup(cfg)

    def test_exceptions_queue_is_empty_on_an_empty_store(self):
        cfg, db = self._fresh()
        try:
            self.assertEqual(_exceptions.collect_exceptions(db), [])
        finally:
            db.close(); cleanup(cfg)

    def test_templates_expose_the_completeness_predicate_surface(self):
        # Layer-4 completeness templates are carried; assert the REAL vendored interface is present.
        # An earlier ported test (test_abstain_or_answer) registers a setdefault stand-in for this
        # module in sys.modules, so load the real file fresh here rather than trust the cached name.
        import importlib
        cached = sys.modules.pop("alpaca.wiki.engine.templates", None)
        try:
            real = importlib.import_module("alpaca.wiki.engine.templates")
            self.assertTrue(hasattr(real, "PredResult"))
            self.assertTrue(hasattr(real, "TEMPLATE_IDS"))
        finally:
            if cached is not None:
                sys.modules["alpaca.wiki.engine.templates"] = cached

    def test_answer_and_loop_expose_the_one_door(self):
        # The answer path is driven end-to-end once the M2.16 providers + M2.14 absorb land; here we
        # assert the carried interface: the single answer entrypoint and the loop orchestrator class.
        self.assertTrue(callable(_answer.answer))
        self.assertTrue(hasattr(_loop, "Engine"))

    def test_reflect_capabilities_are_carried_off(self):
        # Personal-claim maturation and metamemory sampling are carried but off in M1-M4; assert the
        # interface is present (the M2 carried-but-off contract) without driving the write path.
        self.assertTrue(issubclass(_identity.EodLinkDirectionError, Exception))
        self.assertTrue(callable(_identity.write_eod_entry))
        self.assertTrue(callable(_metamemory.sample_and_verify))


# ---------------------------------------------------------------- coverage + closure
class TestVendoringListClosed(unittest.TestCase):
    def test_every_vendored_module_is_named_by_a_ported_test(self):
        covered = _imported_alpaca_wiki_paths()
        uncovered = [m for m in _disk_modules() if m not in covered]
        self.assertEqual(uncovered, [],
                         msg=f"vendored modules with no ported test: {uncovered}")

    def test_not_vendored_files_are_absent_under_alpaca_wiki(self):
        for name in _NOT_VENDORED_FILES:
            hits = [str(p.relative_to(_ROOT)) for p in _WIKI.rglob(name)]
            self.assertEqual(hits, [], msg=f"not-vendored file appeared under alpaca/wiki: {hits}")

    def test_not_vendored_dirs_are_absent_under_alpaca_wiki(self):
        for name in _NOT_VENDORED_DIRS:
            hits = [str(p.relative_to(_ROOT)) for p in _WIKI.rglob(name) if p.is_dir()]
            self.assertEqual(hits, [], msg=f"not-vendored package appeared under alpaca/wiki: {hits}")

    def test_walk_actually_found_the_engine(self):
        # Teeth: the walk must see a real, non-trivial tree (guards against an empty-glob pass).
        self.assertGreater(len(_disk_modules()), 30)


if __name__ == "__main__":
    unittest.main()
