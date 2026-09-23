"""M2.12 proof: the domain compartment and private-routing filter, applied once at the data layer.

Ported in spirit from the upstream Rune-2 suite (tests/test_r1_domain_compartment.py,
test_domain_filter.py, test_privacy_ingest.py, test_private_projection.py, test_private_routing.py).
The upstream tests drive the ingest absorber and the project projection (M2.14 / M2.15, not vendored
here); the same properties are proved directly against the vendored compartment surface, on BOTH a
positive and a negative path.

Done-when property proved here: a row with a missing or null label FOLDS TO PRIVATE (fail-closed),
and the domain predicate is composed once as a subquery so no caller re-implements it.
"""
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# Earlier-collected wiki tests (test_assertion_log, test_bitemporal_asof) register a stand-in
# alpaca.wiki.engine[.compartment] via sys.modules.setdefault - the M2.8 pattern from before compartment
# was vendored. Evict any such stand-in so THIS module binds the real, vendored compartment surface
# (the stub carries neither domain_filter nor is_pushable nor filter).
_eng = sys.modules.get("alpaca.wiki.engine")
if _eng is not None and list(getattr(_eng, "__path__", [None])) == []:
    del sys.modules["alpaca.wiki.engine"]
_comp = sys.modules.get("alpaca.wiki.engine.compartment")
if _comp is not None and not hasattr(_comp, "domain_filter"):
    del sys.modules["alpaca.wiki.engine.compartment"]

from alpaca.wiki.clock import FixedClock
from alpaca.wiki.config import Config
from alpaca.wiki.determinism import sha256_hex
from alpaca.wiki.engine import compartment as comp
from alpaca.wiki.store.db import DB
from alpaca.wiki.store.ledger import Ledger
from alpaca.wiki.store.write import Writer

T0 = "2020-01-01T00:00:00+00:00"
_CANDIDATE_SQL = "SELECT block_id, domain, text FROM blocks WHERE status='active' ORDER BY block_id"


# --------------------------------------------------------------------- os.5 domain compartment


class TestDomainCompartment(unittest.TestCase):
    def setUp(self):
        self.vault = Path(tempfile.mkdtemp(prefix="tewiki_comp_"))
        self.addCleanup(shutil.rmtree, self.vault, ignore_errors=True)
        self.cfg = Config.for_vault(self.vault)
        self.db = DB(self.cfg)
        self.db.pour()
        self.addCleanup(self.db.conn.close)
        w = Writer(self.db, Ledger(self.cfg.ledger_path, vault_dir=self.cfg.vault_dir),
                   clock=FixedClock(start=T0, step=1))
        docs = [("career", "career.md", "Dat works at Acme as a staff engineer."),
                ("personal", "pho.md", "Cong thuc nau pho bo cua me toi."),
                ("general", "notes.md", "A general note about the weather today.")]
        with w.transaction():
            for domain, path, text in docs:
                w.upsert_doc(domain, f"raw/{path}", "raw", sha256_hex(text), domain=domain, private=0)
                w.upsert_block({"block_id": f"{domain}#0", "block_content_id": f"{domain}c0",
                                "occurrence_index": 0, "doc_id": domain, "ordinal": 0,
                                "text": text, "domain": domain})

    def test_personal_query_excludes_career_rows(self):
        # NEGATIVE: a personal query must surface ZERO career-domain rows.
        rows = comp.run_compartmented(self.db, _CANDIDATE_SQL, {}, domain="personal")
        domains = {r["domain"] for r in rows}
        self.assertNotIn("career", domains)
        self.assertIn("personal", domains)

    def test_all_domains_widens(self):
        # POSITIVE: --all-domains is the sole widener.
        rows = comp.run_compartmented(self.db, _CANDIDATE_SQL, {}, domain="personal",
                                      all_domains=True)
        domains = {r["domain"] for r in rows}
        self.assertIn("career", domains)
        self.assertIn("personal", domains)

    def test_filter_composes_as_subquery_once(self):
        wrapped, params = comp.domain_filter(_CANDIDATE_SQL, "career", all_domains=False)
        self.assertIn(":__domain", wrapped)
        self.assertEqual(params, {"__domain": "career"})
        # all_domains widens; no-domain fails closed to general (never an open scan).
        self.assertEqual(comp.domain_filter(_CANDIDATE_SQL, "career", all_domains=True),
                         (_CANDIDATE_SQL, {}))
        default_sql, default_params = comp.domain_filter(_CANDIDATE_SQL, None)
        self.assertIn(":__domain", default_sql)
        self.assertEqual(default_params, {"__domain": "general"})


# --------------------------------------------------------------------- os.6 private write/push routing


class TestPrivateRouting(unittest.TestCase):
    def test_private_outside_partition_raises(self):
        for private, path in ((True, "wiki/career/salary.md"), (1, "raw/diary.md"),
                              ("true", "notes.md")):
            with self.subTest(path=path):
                with self.assertRaises(comp.PrivatePathViolation):
                    comp.assert_private_path(private, path)

    def test_private_inside_partition_ok(self):
        comp.assert_private_path(True, "wiki/private/salary.md")
        comp.assert_private_path(1, "wiki/private/sub/diary.md")

    def test_traversal_and_absolute_tricks_are_refused(self):
        for path in ("wiki/private/../career/salary.md", "/tmp/wiki/private/salary.md",
                     "other/wiki/private/salary.md", "wiki/private-not/salary.md"):
            with self.subTest(path=path):
                with self.assertRaises(comp.PrivatePathViolation):
                    comp.assert_private_path(True, path)

    def test_unknown_flags_are_private_on_write(self):
        for private in (None, -1, 2, "private", "", object()):
            with self.subTest(private=private):
                with self.assertRaises(comp.PrivatePathViolation):
                    comp.assert_private_path(private, "raw/notes.md")

    def test_push_barrier_is_fail_closed(self):
        self.assertFalse(comp.is_pushable(None, 1))          # null private -> not pushable
        self.assertFalse(comp.is_pushable(1, 1))             # explicit private -> not pushable
        self.assertFalse(comp.is_pushable(0, 0))             # unscanned public -> not pushable
        self.assertTrue(comp.is_pushable(0, 1))              # explicit public + scanned -> pushable

    def test_scan_push_payload_flags_private_partition(self):
        payload = ["README.md", "wiki/private/salary.md", "wiki/career/cv.md"]
        self.assertEqual(comp.scan_push_payload(payload), ["wiki/private/salary.md"])


# --------------------------------------------------------------------- compartment.filter fold-to-private


class TestAudienceFilter(unittest.TestCase):
    def test_null_or_missing_label_folds_to_private(self):
        # NEGATIVE: a row whose visibility label is missing or null folds to private and is dropped
        # from a public audience.
        rows = [
            {"id": "a"},                                   # no label at all
            {"id": "b", "label": None},                    # explicit null label
            {"id": "c", "label": "public", "privacy_scanned": 1},  # explicit, scanned public
        ]
        public = comp.filter(rows, "public")
        self.assertEqual([r["id"] for r in public], ["c"], "only the explicit scanned-public row")

    def test_unscanned_public_is_excluded(self):
        rows = [{"id": "u", "label": "public"}, {"id": "u2", "label": "public", "privacy_scanned": 0}]
        self.assertEqual(comp.filter(rows, "public"), [], "an unscanned public label is not public")

    def test_private_audience_returns_every_row(self):
        # POSITIVE: a non-public audience (local/private/all) returns every row unchanged.
        rows = [{"id": "a"}, {"id": "b", "label": None}, {"id": "c", "label": "public",
                                                          "privacy_scanned": 1}]
        self.assertEqual(len(comp.filter(rows, "private")), 3)
        self.assertEqual(len(comp.filter(rows, "local")), 3)

    def test_derives_from_private_flag_when_no_label(self):
        rows = [{"id": "a", "private": 0, "privacy_scanned": 1},   # explicit public
                {"id": "b", "private": 1, "privacy_scanned": 1}]   # explicit private
        self.assertEqual([r["id"] for r in comp.filter(rows, "public")], ["a"])

    def test_normalize_label_fail_closed(self):
        self.assertEqual(comp.normalize_label(None), comp.PRIVATE)
        self.assertEqual(comp.normalize_label("weird"), comp.PRIVATE)
        self.assertEqual(comp.normalize_label("public"), comp.PUBLIC)


if __name__ == "__main__":
    unittest.main()
