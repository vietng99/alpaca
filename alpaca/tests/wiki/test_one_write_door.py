"""M2.9 one write door - a raw mutating conn.execute lives only behind store/write.py.

Ported from rune2/tests/test_apply_writedoor.py (its TestWriteDoorGuard teeth). The apply-path and
write-barrier suites in the upstream files ported for this task (test_apply_writedoor.py's
TestApplyPath, test_pkt1_door.py, test_pkt1_barrier.py, test_rebuild_write_barrier.py) ride on
apply.py / engine.compartment / engine.ledger / the full Writer path, none of which M2.9 vendors;
they are carried by M2.12's proof. What M2.9 vendors and proves here is the write-door GUARD:
guards.check_one_write_door watches the SQL VERB, not the method name.

Positive path: the real alpaca/wiki tree keeps its mutations behind the door and the named derived
writers, so check_one_write_door() is clean. Negative path: a NEW module holding a raw INSERT,
UPDATE or DELETE outside the door is caught, while a read-only sibling is not.
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from alpaca.wiki.guards import KNOWN_WRITERS, WRITE_DOOR, check_one_write_door


def _write(root: Path, rel: str, source: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


class _Fixture:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="write_door_"))

    def add(self, rel: str, source: str) -> "_Fixture":
        _write(self.root, rel, source)
        return self

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class TestWriteDoorRealTree(unittest.TestCase):
    def test_real_wiki_tree_is_clean(self):
        ok, violations = check_one_write_door()
        self.assertTrue(ok, msg=f"a raw mutation leaked outside the write door: {violations}")

    def test_the_one_door_is_named(self):
        self.assertEqual(WRITE_DOOR, {"store/write.py"})


class TestWriteDoorNegativePaths(unittest.TestCase):
    def setUp(self):
        self.fx = _Fixture()

    def tearDown(self):
        self.fx.close()

    def test_rogue_insert_is_flagged_and_reader_is_not(self):
        self.fx.add("engine/rogue.py",
                    "def bad(conn):\n"
                    "    conn.execute(\"INSERT INTO t(x) VALUES (1)\")\n"
                    "    conn.commit()\n")
        self.fx.add("engine/reader.py",
                    "def ok(conn):\n"
                    "    return conn.execute(\"SELECT 1 FROM t\").fetchone()\n")
        ok, violations = check_one_write_door(self.fx.root)
        self.assertFalse(ok)
        self.assertTrue(any("rogue" in v for v in violations),
                        "the rogue raw-INSERT module must be flagged")
        self.assertFalse(any("reader" in v for v in violations),
                         "a read-only module must NOT be flagged")

    def test_update_and_delete_are_flagged(self):
        self.fx.add("engine/upd.py",
                    "def bad(conn):\n    conn.execute(\"UPDATE t SET x=1\")\n")
        self.fx.add("engine/dele.py",
                    "def bad(conn):\n    conn.executescript(\"DELETE FROM t\")\n")
        ok, violations = check_one_write_door(self.fx.root)
        self.assertFalse(ok)
        self.assertTrue(any("upd" in v for v in violations), violations)
        self.assertTrue(any("dele" in v for v in violations), violations)

    def test_guard_watches_the_verb_not_the_method_name(self):
        # a method NAMED like a writer that only SELECTs is clean; a plain execute holding a
        # mutating verb is caught. The guard keys on the SQL verb, never the Python method name.
        self.fx.add("engine/named.py",
                    "class Writer:\n"
                    "    def upsert(self, conn):\n"
                    "        return conn.execute(\"SELECT 1\").fetchone()\n")
        ok, violations = check_one_write_door(self.fx.root)
        self.assertTrue(ok, msg=f"a SELECT-only 'upsert' was wrongly flagged: {violations}")

    def test_whole_module_path_not_a_prefix(self):
        # a same-prefix but distinct file (store/writer.py) must NOT inherit the door's exemption.
        self.fx.add("store/writer.py",
                    "def bad(conn):\n    conn.execute(\"INSERT INTO t VALUES (1)\")\n")
        ok, violations = check_one_write_door(self.fx.root)
        self.assertFalse(ok, msg="store/writer.py rode in on the store/write.py exemption")
        self.assertTrue(any("writer.py" in v for v in violations), violations)


class TestWriteDoorPositivePaths(unittest.TestCase):
    def setUp(self):
        self.fx = _Fixture()

    def tearDown(self):
        self.fx.close()

    def test_the_door_may_mutate(self):
        self.fx.add("store/write.py",
                    "def upsert(conn):\n    conn.execute(\"INSERT INTO t VALUES (1)\")\n")
        ok, violations = check_one_write_door(self.fx.root)
        self.assertTrue(ok, msg=violations)

    def test_named_derived_writers_are_carved_out(self):
        # store/db.py (schema pour + FTS rebuild + backfill) is a named derived-data writer, not
        # the semantic door; it is carved out BY NAME, never by silence.
        self.assertIn("store/db.py", KNOWN_WRITERS)
        self.fx.add("store/db.py",
                    "def pour(conn):\n    conn.execute(\"UPDATE docs SET private=1\")\n")
        ok, violations = check_one_write_door(self.fx.root)
        self.assertTrue(ok, msg=violations)


if __name__ == "__main__":
    unittest.main()
