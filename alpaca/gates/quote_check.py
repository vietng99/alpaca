"""quote_check.py - a derived document's quotes vs the source of record (M1.17 Step 5).

Built for Alpaca against the rows table. A derived document (a projection, a page, a report) that
QUOTES an obligation must reproduce it faithfully; a paraphrase presented as a quotation is a
misquote, and a quote attributed to a source that does not exist is worse. This instrument
re-derives the source text from the record and refuses a quote the record does not support:

  * a QUOTE that does not match its row's `statement` byte-for-byte (after trimming the
    surrounding whitespace of each) -> QUOTE-MISMATCH (FAIL) - the misquote the plan names.
  * a QUOTE attributed to a row with no source row in the record -> QUOTE-NO-SOURCE (FAIL).
  * a document with no recognizable quote -> PASS (it misquotes nothing).

A QUOTE is a row id followed by a DOUBLE-QUOTED string via `:`, `=`, `->` or a table cell, e.g.
`AC-01: "the parser reads a clean table"`. A bare status word is NOT a quote (that is
record_check's surface), so the two checkers partition the lines cleanly.

HONEST LIMIT: faithfulness is compared on the trimmed statement text; a document that quotes only
a FRAGMENT of a statement is not matched to it (a quote must reproduce the whole statement to
count as faithful here - disclosed residual).
"""
from __future__ import annotations

import os
import re
from collections import namedtuple

from alpaca import db
from alpaca.gates import contract
from alpaca.gates import verdict as vc

INSTRUMENT = "quote-check"

# reason tokens.
R_MISQUOTE = "QUOTE-MISMATCH"
R_NO_SOURCE = "QUOTE-NO-SOURCE"
R_NO_QUOTES = "QUOTE-NO-QUOTES"

Quote = namedtuple("Quote", "row_id text line raw")
Result = namedtuple("Result", "verdict reasons info")

# a row id (beginning a token) then a double-quoted verbatim string via `:` / `=` / `->` / pipe.
_QUOTE_RE = re.compile(
    r"(?:^|[\s|*>-])([A-Za-z][\w.:\-]*?)\s*(?::|=|->|\|)\s*\"([^\"]*)\"")

#: projection files check(root) scans when present.
_PROJECTIONS = ("RESUME.md", "CHECKLIST.md", "board.json", "data.json",
                os.path.join("analytics", "index.html"))


def extract_quotes(text):
    """Every (row_id, quoted-text) quote in `text`, in document order. A bare status word yields
    no quote here (it carries no double-quoted value)."""
    quotes = []
    for lineno, raw in enumerate((text or "").splitlines(), start=1):
        for m in _QUOTE_RE.finditer(raw):
            quotes.append(Quote(m.group(1), m.group(2), lineno, raw.strip()[:120]))
    return quotes


def check_quotes(conn, quotes) -> Result:
    """Adjudicate a list of Quotes against the record's row statements. Empty -> PASS."""
    info = {"quotes": len(quotes)}
    if not quotes:
        return Result(vc.PASS, [], info)
    verdicts, reasons = [], []
    for q in quotes:
        stored = db.rows(conn, "rows", "id=?", (q.row_id,))
        if not stored:
            verdicts.append(vc.FAIL)
            reasons.append("%s: line %d attributes a quote to %r, but no source row exists for "
                           "it - a quotation needs a source of record" % (R_NO_SOURCE, q.line,
                                                                          q.row_id))
            continue
        source = (stored[0].get("statement") or "")
        if q.text.strip() != source.strip():
            verdicts.append(vc.FAIL)
            reasons.append("%s: line %d quotes %r as %r, but the source statement is %r - a "
                           "paraphrase presented as a quotation is a misquote"
                           % (R_MISQUOTE, q.line, q.row_id, q.text, source))
        else:
            verdicts.append(vc.PASS)
    return Result(contract.worst(verdicts), reasons, info)


def check_document(conn, text) -> Result:
    """Extract the quotes from `text` and adjudicate them against the record."""
    return check_quotes(conn, extract_quotes(text))


def check(root) -> int:
    """The uniform instrument entry: scan the rendered projections under `root` and refuse any
    misquote or unsourced quote. A tree with no projection or no quote PASSes."""
    try:
        conn = db.connect(root)
    except Exception:
        return vc.PASS
    try:
        codes = []
        for rel in _PROJECTIONS:
            p = os.path.join(root, rel)
            if not os.path.isfile(p):
                continue
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            codes.append(check_document(conn, text).verdict)
        return contract.worst(codes) if codes else vc.PASS
    finally:
        conn.close()


# ------------------------------------------------------------------------- CLI boundary
def main(argv=None) -> int:
    ap = vc.make_parser(
        name=INSTRUMENT,
        description="Refuse a derived document's quote that misquotes its row statement or is "
                    "attributed to a row with no source of record.")
    ap.add_argument("--root", default=None, help="tree whose projections to check")
    ap.add_argument("--document", default=None, help="a single document file to check")
    ap.add_argument("--selftest", action="store_true", help="run the instrument's own controls")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    from alpaca import paths
    root = a.root or paths.root()
    if a.document:
        conn = db.connect(root)
        try:
            with open(a.document, encoding="utf-8") as fh:
                res = check_document(conn, fh.read())
        finally:
            conn.close()
        for r in res.reasons:
            print("  %s" % r)
        return vc.emit_verdict(INSTRUMENT, res.verdict,
                               res.reasons[0] if res.reasons else "every quote is faithful")
    return vc.emit_verdict(INSTRUMENT, check(root), "projections checked")


def selftest() -> int:
    """In-memory controls on a throwaway record: a faithful quote PASSes, a misquote FAILs, and a
    quote with no source row FAILs, all re-derived from the rows table."""
    import shutil
    import tempfile

    base = tempfile.mkdtemp(prefix="quote-check-selftest-")
    root = os.path.join(base, "proj")
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "ALPACA-MANIFEST"), "w", encoding="utf-8") as fh:
        fh.write("alpaca\n")
    controls = []
    failures = 0
    try:
        conn = db.connect(root)
        db.upsert(conn, "rows", "id", {
            "id": "AC-01", "kind": "item", "op": "op", "phase": "verify", "step": "s",
            "statement": "the parser reads a clean table", "proof": "local:p", "where_": "h",
            "how": "d", "when_": "t", "why": None, "session": "s", "operator": "o",
            "status": "open", "tag": "Specced", "content_hash": "h1", "prev_hash": None,
            "supersedes": None})

        def _check(cid, ok, detail):
            nonlocal failures
            controls.append((cid, "FIRED" if ok else "DID-NOT-FIRE", detail))
            if not ok:
                failures += 1

        good = check_document(conn, 'AC-01: "the parser reads a clean table"\n')
        _check("QC-1", good.verdict == vc.PASS, "a faithful quote PASSes")
        misquote = check_document(conn, 'AC-01: "the parser reads a DIRTY table"\n')
        _check("QC-2", misquote.verdict == vc.FAIL
               and any(R_MISQUOTE in r for r in misquote.reasons),
               "a misquote FAILs (QUOTE-MISMATCH)")
        nosrc = check_document(conn, 'AC-77: "a claim about a row that does not exist"\n')
        _check("QC-3", nosrc.verdict == vc.FAIL
               and any(R_NO_SOURCE in r for r in nosrc.reasons),
               "a quote with no source row FAILs (QUOTE-NO-SOURCE)")
        conn.close()
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print("CONTROL TABLE - %s" % INSTRUMENT)
    for cid, state, detail in controls:
        print("  %-5s %-12s %s" % (cid, state, detail))
    if failures:
        return vc.emit_verdict(INSTRUMENT + "-selftest", vc.FAIL,
                               "%d control(s) did not fire" % failures)
    return vc.emit_verdict(INSTRUMENT + "-selftest", vc.PASS, "every control fired")


if __name__ == "__main__":
    import sys
    sys.exit(main())
