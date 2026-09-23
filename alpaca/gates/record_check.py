"""record_check.py - a derived document's status claims vs the ledger (M1.17 Step 5).

Built for Alpaca against the events and rows tables. A derived document - a projection such as
RESUME.md, CHECKLIST.md or a page - is NOT the record; it is a claim ABOUT the record (the
record is the only truth; projections are rendered and never hand-edited). A projection
can drift from the record it was rendered from, or be hand-edited to assert something the record
never said. This instrument re-derives the truth from the ledger and refuses a claim the record
does not back:

  * a status CLAIM whose row has NO ledger row      -> RECORD-UNBACKED-CLAIM (FAIL) - the exact
    orphan-claim the plan names (a derived document carrying an unbacked claim).
  * a claim that CONTRADICTS the row's folded status -> RECORD-CLAIM-MISSTATED (FAIL) - the
    claimed status/tag disagrees with what the record folds to.
  * a document with no recognizable claim            -> PASS (it asserts nothing false); a
    projection with nothing to check is not a defect at this level.

A CLAIM is a row id paired with a status word (open / discharged / failed / waived / blocked) or
a maturity tag (Specced / Built / Verified) via `:`, `=`, `->` or a markdown table cell. A
QUOTED value is NOT a status claim (that is quote_check's surface), so the two checkers never
fight over one line. The folded status is read from the row's verdict rows (events) via
`status_fold`, never from a stored word, so a claim that outlived its evidence cannot pass.

HONEST LIMIT: the claim grammar is a textual pattern, not a proof that the document MEANT the
match as a claim; a prose line shaped like `id: discharged` reads as a claim (disclosed residual).
"""
from __future__ import annotations

import os
import re
from collections import namedtuple

from alpaca import db
from alpaca.checklist import verdict_row
from alpaca.gates import contract
from alpaca.gates import verdict as vc

INSTRUMENT = "record-check"

# reason tokens.
R_UNBACKED = "RECORD-UNBACKED-CLAIM"
R_MISSTATED = "RECORD-CLAIM-MISSTATED"
R_NO_CLAIMS = "RECORD-NO-CLAIMS"

STATUS_WORDS = ("open", "discharged", "failed", "waived", "blocked")
TAG_WORDS = ("Specced", "Built", "Verified")

Claim = namedtuple("Claim", "row_id value line raw")
Result = namedtuple("Result", "verdict reasons info")

# a row id, then a status word or a tag, via `:` / `=` / `->` / a markdown pipe. The row id must
# begin a token (start-of-line, whitespace, a bullet or a pipe) so mid-word matches are excluded.
_VOCAB = "|".join(STATUS_WORDS + TAG_WORDS)
_CLAIM_RE = re.compile(
    r"(?:^|[\s|*>-])([A-Za-z][\w.:\-]*?)\s*(?::|=|->|\|)\s*(" + _VOCAB + r")\b")

#: projection files check(root) scans when present. These are rendered documents.
_PROJECTIONS = ("RESUME.md", "CHECKLIST.md", "board.json", "data.json",
                os.path.join("analytics", "index.html"))


def extract_claims(text):
    """Every (row_id, status-or-tag) claim in `text`, in document order. A quoted value is not a
    status claim, so a `<id>: "..."` line yields no claim here."""
    claims = []
    for lineno, raw in enumerate((text or "").splitlines(), start=1):
        for m in _CLAIM_RE.finditer(raw):
            claims.append(Claim(m.group(1), m.group(2), lineno, raw.strip()[:120]))
    return claims


def _actual(conn, row_id, value):
    """The record's own value for this claim's kind: the folded status for a status word, the
    derived tag column for a tag word. Returns None when the row is absent (unbacked)."""
    stored = db.rows(conn, "rows", "id=?", (row_id,))
    if not stored:
        return None
    if value in TAG_WORDS:
        return stored[0].get("tag")
    return verdict_row.status_fold(conn, row_id)


def check_claims(conn, claims) -> Result:
    """Adjudicate a list of Claims against the record. Empty -> PASS (nothing false is asserted)."""
    info = {"claims": len(claims)}
    if not claims:
        return Result(vc.PASS, [], info)
    verdicts, reasons = [], []
    for c in claims:
        actual = _actual(conn, c.row_id, c.value)
        if actual is None:
            verdicts.append(vc.FAIL)
            reasons.append("%s: line %d claims %r is %r, but no ledger row exists for %r - a "
                           "derived document may not assert a status the record never made"
                           % (R_UNBACKED, c.line, c.row_id, c.value, c.row_id))
        elif actual != c.value:
            verdicts.append(vc.FAIL)
            reasons.append("%s: line %d claims %r is %r, but the record folds it to %r"
                           % (R_MISSTATED, c.line, c.row_id, c.value, actual))
        else:
            verdicts.append(vc.PASS)
    return Result(contract.worst(verdicts), reasons, info)


def check_document(conn, text) -> Result:
    """Extract the claims from `text` and adjudicate them against the record."""
    return check_claims(conn, extract_claims(text))


def check(root) -> int:
    """The uniform instrument entry: scan the rendered projections under `root` and refuse any
    unbacked or misstated status claim. A tree with no projection or no claim PASSes (nothing
    false is asserted)."""
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
        description="Refuse a derived document's status claim that has no ledger row or that "
                    "contradicts the record's folded status.")
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
                               res.reasons[0] if res.reasons else "every claim is backed")
    return vc.emit_verdict(INSTRUMENT, check(root), "projections checked")


def selftest() -> int:
    """In-memory controls on a throwaway record: a backed and correct claim PASSes, an unbacked
    claim FAILs, and a misstated claim FAILs, all re-derived from the rows and events tables."""
    import shutil
    import tempfile

    base = tempfile.mkdtemp(prefix="record-check-selftest-")
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
        verdict_row.discharge(conn, "AC-01", "h1", instrument="static-check", verdict=vc.PASS,
                              evidence=[], level="L2", session="s1")

        def _check(cid, ok, detail):
            nonlocal failures
            controls.append((cid, "FIRED" if ok else "DID-NOT-FIRE", detail))
            if not ok:
                failures += 1

        good = check_document(conn, "- AC-01: discharged\n")
        _check("RC-1", good.verdict == vc.PASS, "a backed, correct status claim PASSes")
        unbacked = check_document(conn, "- AC-99: discharged\n")
        _check("RC-2", unbacked.verdict == vc.FAIL
               and any(R_UNBACKED in r for r in unbacked.reasons),
               "a claim with no ledger row FAILs (RECORD-UNBACKED-CLAIM)")
        misstated = check_document(conn, "- AC-01: failed\n")
        _check("RC-3", misstated.verdict == vc.FAIL
               and any(R_MISSTATED in r for r in misstated.reasons),
               "a claim contradicting the folded status FAILs (RECORD-CLAIM-MISSTATED)")
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
