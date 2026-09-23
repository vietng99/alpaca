"""M1.17 proof - record_check and quote_check against the events and rows tables (Step 5).

A derived document (a projection: RESUME.md, CHECKLIST.md, a page) is not the record; it is a
claim ABOUT the record. Two failures a projection can carry, both of which these instruments
re-derive from the events and rows tables rather than trust:

  record_check - a status CLAIM with no backing ledger row.
      * positive: a document that states a row's actual folded status PASSes.
      * negative (Done-when): a status claim whose row has NO ledger row FAILs
        (RECORD-UNBACKED-CLAIM) - the exact orphan-claim the plan names.
      * negative: a claim that CONTRADICTS the row's folded status FAILs (RECORD-CLAIM-MISSTATED).

  quote_check - a QUOTE that does not faithfully reproduce its source.
      * positive: a document that quotes a row's `statement` verbatim PASSes.
      * negative (Done-when): a MISQUOTE (altered words) FAILs (QUOTE-MISMATCH).
      * negative: a quote attributed to a row with no source row FAILs (QUOTE-NO-SOURCE).

The folded status is read from the verdict rows (events), never from a stored word, so the
check can never bless a claim that outlived its evidence.
"""
import os

import pytest

from alpaca import db
from alpaca.checklist import verdict_row
from alpaca.gates import verdict as vc
from alpaca.gates import record_check
from alpaca.gates import quote_check


def _setup(project):
    from alpaca import cli
    cli.main(["init"])
    return db.connect(project)


def _seed_row(conn, row_id, statement, content_hash):
    dbrow = {
        "id": row_id, "kind": "item", "op": "op-1", "phase": "verify", "step": "s",
        "statement": statement, "proof": "local:proof.txt", "where_": "host",
        "how": "derived", "when_": "2026-09-16T00:00:00+00:00", "why": None,
        "session": "sess", "operator": "op", "status": "open", "tag": "Specced",
        "content_hash": content_hash, "prev_hash": None, "supersedes": None,
    }
    db.upsert(conn, "rows", "id", dbrow)
    return dbrow


def _discharge(conn, row_id, content_hash, code=vc.PASS):
    p = os.path.join(os.getcwd(), "ev.txt")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("genuine evidence\n")
    verdict_row.discharge(conn, row_id, content_hash, instrument="static-check",
                          verdict=code, evidence=["local:ev.txt"], level="L2", session="s1")


# =========================================================== record_check
def test_record_check_backed_claim_passes(project):
    conn = _setup(project)
    _seed_row(conn, "AC-01", "the parser reads a clean table", "h-ac-01")
    _discharge(conn, "AC-01", "h-ac-01", vc.PASS)      # folds to 'discharged'
    doc = "# Projection\n\n- AC-01: discharged\n"
    res = record_check.check_document(conn, doc)
    assert res.verdict == vc.PASS, res.reasons


def test_record_check_unbacked_claim_fails(project):
    conn = _setup(project)
    _seed_row(conn, "AC-01", "the parser reads a clean table", "h-ac-01")
    _discharge(conn, "AC-01", "h-ac-01", vc.PASS)
    # AC-99 is claimed discharged but no ledger row exists for it: the orphan claim.
    doc = "# Projection\n\n- AC-01: discharged\n- AC-99: discharged\n"
    res = record_check.check_document(conn, doc)
    assert res.verdict == vc.FAIL
    assert any(record_check.R_UNBACKED in r and "AC-99" in r for r in res.reasons), res.reasons


def test_record_check_misstated_claim_fails(project):
    conn = _setup(project)
    _seed_row(conn, "AC-01", "the parser reads a clean table", "h-ac-01")
    _discharge(conn, "AC-01", "h-ac-01", vc.PASS)      # actually 'discharged'
    doc = "# Projection\n\n- AC-01: failed\n"           # the document says otherwise
    res = record_check.check_document(conn, doc)
    assert res.verdict == vc.FAIL
    assert any(record_check.R_MISSTATED in r and "AC-01" in r for r in res.reasons), res.reasons


def test_record_check_extract_claims_grammar(project):
    # the grammar recognises a status word after a row id, and ignores a quoted statement (that
    # is quote_check's job), so the two checkers never fight over one line.
    claims = record_check.extract_claims(
        "- AC-01: discharged\n"
        "| AC-02 | failed |\n"
        'AC-03: "a verbatim quote, not a status claim"\n'
        "just some prose with no claim\n")
    got = {(c.row_id, c.value) for c in claims}
    assert ("AC-01", "discharged") in got
    assert ("AC-02", "failed") in got
    assert not any(c.row_id == "AC-03" for c in claims)


# =========================================================== quote_check
def test_quote_check_faithful_quote_passes(project):
    conn = _setup(project)
    _seed_row(conn, "AC-01", "the parser reads a clean table", "h-ac-01")
    doc = 'Report: AC-01: "the parser reads a clean table"\n'
    res = quote_check.check_document(conn, doc)
    assert res.verdict == vc.PASS, res.reasons


def test_quote_check_misquote_fails(project):
    conn = _setup(project)
    _seed_row(conn, "AC-01", "the parser reads a clean table", "h-ac-01")
    doc = 'Report: AC-01: "the parser reads a DIRTY table"\n'   # altered word -> misquote
    res = quote_check.check_document(conn, doc)
    assert res.verdict == vc.FAIL
    assert any(quote_check.R_MISQUOTE in r and "AC-01" in r for r in res.reasons), res.reasons


def test_quote_check_no_source_fails(project):
    conn = _setup(project)
    _seed_row(conn, "AC-01", "the parser reads a clean table", "h-ac-01")
    doc = 'Report: AC-77: "some claim about a row that does not exist"\n'
    res = quote_check.check_document(conn, doc)
    assert res.verdict == vc.FAIL
    assert any(quote_check.R_NO_SOURCE in r and "AC-77" in r for r in res.reasons), res.reasons


def test_quote_check_extract_quotes_grammar(project):
    quotes = quote_check.extract_quotes(
        'AC-01: "verbatim one"\n'
        "- AC-02: discharged\n"                        # a status claim, not a quote
        'AC-03: "verbatim three"\n')
    got = {(q.row_id, q.text) for q in quotes}
    assert ("AC-01", "verbatim one") in got
    assert ("AC-03", "verbatim three") in got
    assert not any(q.row_id == "AC-02" for q in quotes)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
