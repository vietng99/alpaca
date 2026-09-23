"""The record's `events` hash chain, verified. Detect-only, and honest about its scope.

Ported from the earlier harness gates/warlog_chain_check.py and adapted for Alpaca: the subject is the
record's append-only `events` table (alpaca/db.py), not a war-log JSONL file. The chain is the
one alpaca.db already writes: each row carries `content_hash` (a hash over the event's canonical
fields), `prev_hash` (the previous row's `hash`) and `hash = sha256(prev_hash + "\\n" +
content_hash)`, with a genesis of `sha256(b"")`. This instrument RE-DERIVES that chain over
the rows on disk and reports a verdict, a reason code and controls, so a chain nobody
verifies -- worse than no chain, because every reader assumes somebody checked it -- is
verified here.

WHAT IT PROVES, AND THE SMALLER THING THAT IS
    It proves the table is INTERNALLY consistent: every row's `content_hash` re-derives from
    its own fields, its `prev_hash` is the previous row's `hash`, and the first row chains
    from genesis. That catches an edit to any field, an inserted row, a deleted row and a
    reordering. It does NOT prove the table was not rewritten WHOLESALE: anyone who can write
    the DB can re-chain every row from genesis and it verifies clean. Detecting that needs an
    EXTERNAL head anchor this substrate does not carry. So: tamper-EVIDENT against edits, NOT
    tamper-PROOF. Every run says so, because a verdict quoted without its scope becomes a
    stronger claim than the instrument made.

Refusals surface as a verdict-band code from alpaca.gates.verdict (never a private copy).
"""
from __future__ import annotations

import sys

from alpaca import util
from alpaca.gates import verdict as vc

INSTRUMENT = "chain-check"

#: The chain's genesis: the hash of the empty byte string. Re-derived, never a literal, so a
#: reader cannot mistake a copied constant for a measurement. Shared with alpaca.db.
GENESIS = util.sha256_hex(b"")

# ---------------------------------------------------------------------- reason codes
R_CHAIN_HOLDS = "CHAIN-HOLDS"
R_CONTENT_DRIFT = "CHAIN-CONTENT-DRIFT"
R_CHAIN_BREAK = "CHAIN-BREAK"
R_HASH_DRIFT = "CHAIN-HASH-DRIFT"
R_GENESIS_WRONG = "CHAIN-GENESIS-MISMATCH"
R_POPULATION_EMPTY = "CHAIN-POPULATION-EMPTY"

SCOPE_NOTE = ("scope: DETECT-ONLY. This proves the events table is internally consistent, so "
              "an edited, inserted, deleted or reordered row is caught. It does NOT catch a "
              "WHOLESALE rewrite: anyone who can write the DB can re-chain it from genesis and "
              "it will verify. That needs an external head anchor this substrate does not have.")


def _content_hash(row) -> str:
    """The content hash alpaca.db writes, re-derived from the row's own canonical fields."""
    fields = {"ts": row["ts"], "session": row["session"], "actor": row["actor"],
              "kind": row["kind"], "op": row["op"], "ref": row["ref"], "data": row["data"]}
    return util.sha256_hex("alpaca-event/v1\n" + util.canonical_json(fields))


def verify_chain(conn) -> tuple:
    """Re-derive the events chain from the rows on disk. Returns (verdict_code, reasons, facts).

    BLOCKED on an empty table (an empty measured population is never a pass). FAIL on the first
    content drift, chain break, hash drift or wrong genesis. PASS when every row chains from
    genesis. The scope note rides every verdict, pass or fail alike.
    """
    facts = {"instrument": INSTRUMENT, "genesis": GENESIS}
    rows = list(conn.execute("SELECT * FROM events ORDER BY id"))
    facts["rows"] = len(rows)
    if not rows:
        return (vc.BLOCKED,
                ["%s: the events table carries no row; an empty measured population is BLOCKED, "
                 "never a pass" % R_POPULATION_EMPTY, SCOPE_NOTE], facts)

    prev_hash = GENESIS
    breaks = []
    for n, row in enumerate(rows, 1):
        if _content_hash(row) != row["content_hash"]:
            breaks.append("%s: row %d (id=%s) content re-derives to a different hash; a field "
                          "was edited in place" % (R_CONTENT_DRIFT, n, row["id"]))
            break
        if row["prev_hash"] != prev_hash:
            reason = ("%s: row %d prev_hash=%s, the chain expected %s (a row was inserted, "
                      "removed or reordered)"
                      % (R_CHAIN_BREAK, n, str(row["prev_hash"])[:16], str(prev_hash)[:16]))
            if n == 1:
                reason = ("%s: the first row's prev_hash is not the genesis %s"
                          % (R_GENESIS_WRONG, GENESIS[:16]))
            breaks.append(reason)
            break
        if util.sha256_hex(prev_hash + "\n" + row["content_hash"]) != row["hash"]:
            breaks.append("%s: row %d hash does not fold prev_hash + content_hash" %
                          (R_HASH_DRIFT, n))
            break
        prev_hash = row["hash"]

    facts["head"] = prev_hash
    facts["breaks"] = breaks
    if breaks:
        return vc.FAIL, breaks + [SCOPE_NOTE], facts
    return (vc.PASS,
            ["%s: %d row(s) chain from genesis; head %s" % (R_CHAIN_HOLDS, len(rows),
                                                            prev_hash[:16]), SCOPE_NOTE],
            facts)


# ---------------------------------------------------------------------- selftest
def _controls(conn):
    """Every control seeds a case against a real events table and asserts the exact outcome.
    Returns a list of (cid, description, ok, detail)."""
    from alpaca import db
    out = []

    def _c(cid, desc, ok, detail=""):
        out.append((cid, desc, bool(ok), detail))

    # able-to-BLOCK: an empty table is never a pass.
    v, r, _f = verify_chain(conn)
    _c("C-00", "an empty events table is BLOCKED, never a pass",
       v == vc.BLOCKED and any(x.startswith(R_POPULATION_EMPTY) for x in r), r[0])

    # able-to-PASS: a well-formed chain holds.
    for i in range(5):
        db.append_event(conn, session="s", actor="a", kind="beat", ref="r%d" % i,
                        data={"i": i})
    v, r, f = verify_chain(conn)
    _c("C-01", "a well-formed chain of appended events holds",
       v == vc.PASS and f["rows"] == 5 and not f["breaks"], r[0])

    # the head equals alpaca.db's own recomputation (two derivations, one chain).
    ok_db, _msg = db.verify_chain(conn)
    _c("C-02", "chain-check agrees with alpaca.db.verify_chain on a clean record", ok_db, str(_msg))

    # able-to-FAIL: edit one row's content field in place (no re-chain) -> content drift.
    conn.execute("UPDATE events SET actor='TAMPERED' WHERE id=(SELECT id FROM events "
                 "ORDER BY id LIMIT 1 OFFSET 2)")
    v, r, f = verify_chain(conn)
    _c("C-03", "editing a committed row in place breaks the chain",
       v == vc.FAIL and any(x.startswith((R_CONTENT_DRIFT, R_CHAIN_BREAK)) for x in r),
       r[0] if r else "")

    return out


def selftest(conn=None) -> int:
    """Run the controls against a throwaway record. A control that cannot fail is not a
    selftest, so each seeds both the passing and the failing shape of the chain."""
    own = conn is None
    if own:
        import tempfile
        from alpaca import db
        base = tempfile.mkdtemp(prefix="chain-check-selftest-")
        conn = db.connect(base)
    try:
        controls = _controls(conn)
    finally:
        if own:
            conn.close()
    failures = [cid for cid, _d, ok, _x in controls if not ok]
    print("CONTROL TABLE -- %s/1" % INSTRUMENT)
    for cid, desc, ok, detail in controls:
        print("  %-5s %-12s %s" % (cid, "FIRED" if ok else "DID-NOT-FIRE", desc))
    print("  %d control(s), %d did not fire" % (len(controls), len(failures)))
    code = vc.FAIL if failures else vc.PASS
    print(vc.gate_line("%s-selftest" % INSTRUMENT, code))
    return vc.SELFTEST if failures else vc.PASS


def main(argv=None) -> int:
    ap = vc.make_parser(name=INSTRUMENT,
                        description="verify the record's events hash chain (detect-only)")
    ap.add_argument("--selftest", action="store_true", help="run the chain-check controls")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    from alpaca import db, paths
    conn = db.connect(paths.root())
    try:
        code, reasons, _f = verify_chain(conn)
    finally:
        conn.close()
    return vc.emit_verdict(INSTRUMENT, code, reasons[0] if reasons else "", evidence=reasons[1:])


if __name__ == "__main__":
    sys.exit(main())
