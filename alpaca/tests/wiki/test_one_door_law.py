"""M2.9 R1 one-door law - the read door is a regression-tested property, not a claim.

Ported from rune2/tests/test_r1_one_door_law.py and rune2/tests/test_r1_import_guard.py,
narrowed to the mechanism M2.9 vendors: the AST import-graph guard (guards.check_one_door). The
upstream oracle-level one-door tests (the ambiguity verdict and the raw-tier fall-through gate)
ride on the engine vendored in M2.10 and later; they are carried by that task's proof, not here.

Positive path: the real alpaca/wiki tree has exactly one retrieval door, so check_one_door() is clean.
Negative path: a violating fixture that reaches the ranking surface - by relative import, by
absolute import, by a `from ..engine import <submodule>` form, and by dynamic import by name - is
caught for every guarded module, while a read-only sibling and the two declared doors are not.
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from alpaca.wiki.guards import ALLOWED, GUARDED_MODULES, check_one_door


def _write(root: Path, rel: str, source: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


class _Fixture:
    """A throwaway package tree the guard can be pointed at."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="one_door_"))

    def add(self, rel: str, source: str) -> "_Fixture":
        _write(self.root, rel, source)
        return self

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class TestReadDoorRealTree(unittest.TestCase):
    def test_real_wiki_tree_has_one_read_door(self):
        # mutation (drop a guarded module from the surface, or widen ALLOWED) would let a second
        # retrieval door in undetected; on the shipped tree the guard must be clean.
        ok, violations = check_one_door()
        self.assertTrue(ok, msg=f"a second retrieval door leaked into alpaca/wiki: {violations}")


class TestReadDoorNegativePaths(unittest.TestCase):
    def setUp(self):
        self.fx = _Fixture()

    def tearDown(self):
        self.fx.close()

    def test_relative_import_of_ranking_primitive_is_flagged(self):
        self.fx.add("engine/rogue.py",
                    "from ..store.fts import search_bm25\n\n"
                    "def bad(db):\n    return search_bm25(db, 'x', 10)\n")
        ok, violations = check_one_door(self.fx.root)
        self.assertFalse(ok)
        self.assertTrue(any("rogue" in v for v in violations), violations)

    def test_absolute_import_of_ranking_primitive_is_flagged(self):
        self.fx.add("engine/rogue.py", "import alpaca.wiki.engine.plan\n")
        ok, violations = check_one_door(self.fx.root)
        self.assertFalse(ok)
        self.assertTrue(any("rogue" in v for v in violations), violations)

    def test_from_package_submodule_form_is_flagged(self):
        # `from alpaca.wiki.engine import fuse` pulls the ranking submodule in by name.
        self.fx.add("engine/rogue.py", "from alpaca.wiki.engine import fuse\n")
        ok, violations = check_one_door(self.fx.root)
        self.assertFalse(ok)
        self.assertTrue(any("rogue" in v for v in violations), violations)

    def test_dynamic_import_by_name_is_flagged(self):
        # F1 scar: a dynamic importlib bypass must be caught too.
        self.fx.add("engine/rogue.py",
                    "import importlib\n\n"
                    "def bad():\n    return importlib.import_module('alpaca.wiki.store.vec')\n")
        ok, violations = check_one_door(self.fx.root)
        self.assertFalse(ok)
        self.assertTrue(any("rogue" in v for v in violations), violations)

    def test_every_guarded_module_is_caught(self):
        # the whole ranking surface is guarded, not one name: each module, imported by a rogue,
        # must be flagged. Deleting any entry from GUARDED_MODULES flips exactly one of these.
        for mod in GUARDED_MODULES:
            with self.subTest(module=mod):
                fx = _Fixture()
                try:
                    fx.add("engine/rogue.py", f"import alpaca.wiki.{mod}\n")
                    ok, violations = check_one_door(fx.root)
                    self.assertFalse(ok, msg=f"{mod} was not guarded")
                    self.assertTrue(any("rogue" in v for v in violations), violations)
                finally:
                    fx.close()


class TestReadDoorPositivePaths(unittest.TestCase):
    def setUp(self):
        self.fx = _Fixture()

    def tearDown(self):
        self.fx.close()

    def test_read_only_sibling_is_not_flagged(self):
        # store.query is a by-id lookup that ranks nothing - freely importable.
        self.fx.add("engine/reader.py",
                    "from ..store.query import by_id\n\n"
                    "def ok(db):\n    return by_id(db, 'n1')\n")
        ok, violations = check_one_door(self.fx.root)
        self.assertTrue(ok, msg=violations)

    def test_declared_doors_may_import_the_surface(self):
        # engine/retrieve.py (the door) and store/read.py (its aggregator) are the two ALLOWED
        # importers: they reach the ranking surface and stay clean.
        self.assertEqual(ALLOWED, {"engine/retrieve.py", "store/read.py"})
        self.fx.add("engine/retrieve.py", "from ..store.fts import search_bm25\n")
        self.fx.add("store/read.py",
                    "from .fts import search_bm25\nfrom .vec import knn_blocks\n")
        ok, violations = check_one_door(self.fx.root)
        self.assertTrue(ok, msg=violations)

    def test_whole_module_path_not_a_prefix(self):
        # a same-prefix but distinct module (engine.planner) must NOT be caught by engine.plan;
        # a same-named module rooted elsewhere MUST still be caught (cannot slip through).
        self.fx.add("engine/near.py", "from ..engine.planner import x\n")
        ok, violations = check_one_door(self.fx.root)
        self.assertTrue(ok, msg=f"engine.planner false-matched engine.plan: {violations}")

        fx2 = _Fixture()
        try:
            fx2.add("engine/rogue.py", "from otherpkg.store.fts import search_bm25\n")
            ok2, violations2 = check_one_door(fx2.root)
            self.assertFalse(ok2, msg="a same-named ranking module elsewhere slipped through")
        finally:
            fx2.close()


if __name__ == "__main__":
    unittest.main()
