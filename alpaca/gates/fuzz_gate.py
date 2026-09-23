"""A standing PROPERTY / FUZZ gate for the record's append-only `events` table (alpaca/db.py).

Ported from the earlier harness gates/fuzz_gate.py and adapted for Alpaca: the subject is the record's
`events` table verified by alpaca.db.verify_chain (and chain_check), not log_db's sqlite store.

WHAT THIS IS
    A generative safety check. It builds an HONEST record through the real append path
    (db.append_event), snapshots the committed rows, applies ONE out-of-band tamper drawn from
    a menu (raw sqlite, the substrate carries no append-only triggers), runs a short sequence of
    honest appends, then asserts the single load-bearing SAFETY INVARIANT the record exists to
    keep:

        A verify() that returns PASS may NEVER sit on top of an INTERNALLY INCONSISTENT record.

    Per round, after the tamper:
        * verify() stays refused (chain break / content drift)  -> the tamper was detected. OK.
        * verify() PASS and the events table is empty            -> ERASURE-ALLOWED (the disclosed
                                                                    same-writer total-erasure limit).
        * verify() PASS and the record is still internally consistent (chain_check re-derives it
          clean) even though a committed row changed                -> REWRITE-ALLOWED. This is the
          declared detect-only limit of a substrate with no external head anchor (a wholesale
          re-chain, or a tail truncation, re-chains and verifies), NOT a laundering bug.
        * verify() PASS while chain_check finds the record INCONSISTENT -> LAUNDERING. The store
          certified a record that does not re-derive. This is the defect the gate exists to catch;
          it FAILs, with the seed + tamper printed so the finding reproduces.

    A gate that cannot pass is as broken as one that cannot fail: the no-tamper round must land
    PASS-CLEAN, and a synthetic laundering (a deliberately broken verify over a tampered store)
    must land LAUNDERING, or the gate is not verifying anything.

CONTRACT (imported, never restated): alpaca.gates.verdict owns the verdict<->code map.
"""
from __future__ import annotations

import random
import sys
import tempfile

from alpaca import db, util
from alpaca.gates import chain_check, verdict as vc

INSTRUMENT = "fuzz-gate"

_GENESIS = util.sha256_hex(b"")


# ---------------------------------------------------------------------- snapshot
def _snapshot(conn) -> dict:
    """{content_hash: (id, canonical content fields)} over every committed event. A preserved
    content_hash proves preserved content; the content tuple is kept too so a same-hash content
    edit (a collision attempt) is still caught."""
    snap = {}
    for r in conn.execute("SELECT * FROM events ORDER BY id"):
        snap[r["content_hash"]] = (r["id"], r["ts"], r["session"], r["actor"], r["kind"],
                                   r["op"], r["ref"], r["data"])
    return snap


def _build_honest_record(conn, rng) -> None:
    """A fully honest, verify()-clean record built through the real append path."""
    for i in range(rng.randint(3, 6)):
        db.append_event(conn, session="s-%d" % (i % 2), actor="worker",
                        kind=rng.choice(["beat", "note", "run"]), ref="r%d" % i,
                        data={"i": i, "payload": "honest work %d" % i})


# ---------------------------------------------------------------------- tamper menu
def tamper_none(conn, rng) -> str:
    return "no-tamper"


def tamper_edit_no_rechain(conn, rng) -> str:
    """Edit one committed row's field WITHOUT re-chaining -> content drift / chain break."""
    ids = [r[0] for r in conn.execute("SELECT id FROM events ORDER BY id").fetchall()]
    tid = ids[rng.randrange(len(ids))]
    conn.execute("UPDATE events SET actor=actor||'_TAMPER' WHERE id=?", (tid,))
    conn.commit()
    return "edit-no-rechain(id=%d)" % tid


def tamper_delete_middle(conn, rng) -> str:
    """Delete a NON-tail row -> the following row's prev_hash no longer matches -> chain break."""
    ids = [r[0] for r in conn.execute("SELECT id FROM events ORDER BY id").fetchall()]
    tid = ids[rng.randrange(0, len(ids) - 1)]
    conn.execute("DELETE FROM events WHERE id=?", (tid,))
    conn.commit()
    return "delete-middle(id=%d)" % tid


def tamper_insert_forged_middle(conn, rng) -> str:
    """Insert a correctly-shaped row after some row without fixing the successor's prev_hash."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id")]
    at = rng.randrange(0, len(rows) - 1)
    prev = rows[at]
    fields = {"ts": util.now_iso(), "session": "ghost", "actor": "ghost", "kind": "beat",
              "op": None, "ref": "forged", "data": util.canonical_json({"forged": True})}
    ch = util.sha256_hex("alpaca-event/v1\n" + util.canonical_json(fields))
    h = util.sha256_hex(prev["hash"] + "\n" + ch)
    conn.execute(
        "INSERT INTO events (ts,session,actor,kind,op,ref,data,content_hash,prev_hash,hash) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (fields["ts"], fields["session"], fields["actor"], fields["kind"], fields["op"],
         fields["ref"], fields["data"], ch, prev["hash"], h))
    conn.commit()
    return "insert-forged-middle(after id=%d)" % prev["id"]


def tamper_wipe_table(conn, rng) -> str:
    """Empty the whole events table -> total erasure (the disclosed same-writer limit)."""
    conn.execute("DELETE FROM events")
    conn.commit()
    return "wipe-table(events)"


_MENU = {
    "none": tamper_none,
    "edit-no-rechain": tamper_edit_no_rechain,
    "delete-middle": tamper_delete_middle,
    "insert-forged-middle": tamper_insert_forged_middle,
    "wipe-table": tamper_wipe_table,
}


# ---------------------------------------------------------------------- classifier
def classify(conn, pre_snap, verify_fn=None) -> tuple:
    """-> (outcome, reasons, violations). verify_fn(conn) -> (ok, msg) defaults to
    db.verify_chain; the selftest injects a broken one to prove the gate can fail."""
    verify_fn = verify_fn or db.verify_chain
    try:
        ok, msg = verify_fn(conn)
    except Exception as exc:                       # an un-adjudicable record is BLOCKED, never a pass
        return "BLOCKED", ["verify-raised: %s: %s" % (type(exc).__name__, exc)], None
    if not ok:
        return "BLOCKED", [str(msg)], None
    # verify() said PASS. Is the record still internally consistent, independently?
    consistent = chain_check.verify_chain(conn)[0] == vc.PASS
    empty = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    post = _snapshot(conn)
    violations = []
    for ch, content in pre_snap.items():
        if ch not in post:
            violations.append("content_hash %s MISSING while verify()=PASS" % ch[:16])
        elif post[ch][1:] != content[1:]:
            violations.append("content_hash %s CONTENT-CHANGED while verify()=PASS" % ch[:16])
    if not violations:
        return "PASS-CLEAN", [str(msg)], None
    if empty:
        return "ERASURE-ALLOWED", [str(msg)], None
    if consistent:
        # a committed row changed/vanished but the record re-derives clean: the disclosed
        # detect-only limit of an anchor-free substrate, not a laundering bug.
        return "REWRITE-ALLOWED", [str(msg)], None
    return "LAUNDERING", [str(msg)], violations


def _round(seed, tamper_name, verify_fn=None) -> dict:
    rng = random.Random(seed)
    base = tempfile.mkdtemp(prefix="fuzz-gate-")
    conn = db.connect(base)
    try:
        _build_honest_record(conn, rng)
        pre = _snapshot(conn)
        desc = _MENU[tamper_name](conn, rng)
        outcome, reasons, violations = classify(conn, pre, verify_fn=verify_fn)
        return {"seed": seed, "tamper": desc, "tamper_name": tamper_name,
                "outcome": outcome, "reasons": reasons, "violations": violations}
    finally:
        conn.close()


# a verify that lies: always PASS. The synthetic regression the gate must catch.
def _broken_verify(conn):
    return True, "broken verify: always PASS"


# ---------------------------------------------------------------------- selftest battery
# (cid, description, tamper, acceptable-outcomes, verify_fn). LAUNDERING is acceptable for no
# honest control; F-06 asserts it explicitly against a broken verify to prove the gate can fail.
_CONTROLS = [
    ("F-01", "positive control: no tamper -> verify PASS on an intact record",
     "none", {"PASS-CLEAN"}, None),
    ("F-02", "edit a committed row field without re-chaining -> detected",
     "edit-no-rechain", {"BLOCKED"}, None),
    ("F-03", "delete a non-tail row (chain break) -> detected",
     "delete-middle", {"BLOCKED"}, None),
    ("F-04", "insert a forged row mid-chain -> detected",
     "insert-forged-middle", {"BLOCKED"}, None),
    ("F-05", "wipe the whole events table -> ERASURE-ALLOWED (disclosed limit)",
     "wipe-table", {"ERASURE-ALLOWED"}, None),
    ("F-06", "synthetic laundering: a broken verify over a tampered store -> LAUNDERING",
     "edit-no-rechain", {"LAUNDERING"}, _broken_verify),
]


def selftest() -> int:
    rows = []
    launderings = []
    base_seed = 0xF022
    for i, (cid, desc, tamper, acceptable, verify_fn) in enumerate(_CONTROLS):
        res = _round(base_seed + i * 101, tamper, verify_fn=verify_fn)
        actual = res["outcome"]
        # LAUNDERING is a real finding for every control EXCEPT the one that asks for it.
        if actual == "LAUNDERING" and "LAUNDERING" not in acceptable:
            launderings.append(res)
        fired = actual in acceptable
        rows.append((cid, desc, fired, "want %s | got %s | tamper=%s"
                     % ("/".join(sorted(acceptable)), actual, res["tamper"])))
    failures = [cid for cid, _d, ok, _x in rows if not ok]
    print("CONTROL TABLE -- %s-selftest/1 (one record, one tamper, the safety invariant)"
          % INSTRUMENT)
    for cid, desc, ok, observed in rows:
        print("  %-5s %-12s %s | %s" % (cid, "FIRED" if ok else "DID-NOT-FIRE", desc, observed))
    print("  %d control(s), %d did not fire, %d laundering finding(s)"
          % (len(rows), len(failures), len(launderings)))
    code = vc.FAIL if (failures or launderings) else vc.PASS
    print(vc.gate_line("%s-selftest" % INSTRUMENT, code))
    return vc.SELFTEST if (failures or launderings) else vc.PASS


def run_fuzz(n, seed) -> int:
    """N random rounds from a master seed. A LAUNDERING finding fails the gate; the seed and
    tamper are printed so it reproduces."""
    master = random.Random(seed)
    names = list(_MENU.keys())
    counts = {}
    launderings = []
    for _ in range(n):
        res = _round(master.getrandbits(63), master.choice(names))
        counts[res["outcome"]] = counts.get(res["outcome"], 0) + 1
        if res["outcome"] == "LAUNDERING":
            launderings.append(res)
    print("%s: %d round(s), seed=%d, outcomes=%s" % (INSTRUMENT, n, seed, sorted(counts.items())))
    for res in launderings:
        print("  LAUNDERING seed=%d tamper=%s violations=%s"
              % (res["seed"], res["tamper"], res["violations"]))
    code = vc.FAIL if launderings else vc.PASS
    return vc.emit_verdict(INSTRUMENT, code,
                           "%d laundering finding(s)" % len(launderings) if launderings
                           else "no laundering across %d round(s)" % n)


def main(argv=None) -> int:
    ap = vc.make_parser(name=INSTRUMENT,
                        description="property/fuzz gate: a verify() PASS may never sit on an "
                                    "internally inconsistent record")
    ap.add_argument("--selftest", action="store_true", help="the fixed deterministic battery")
    ap.add_argument("--fuzz", type=int, default=None, metavar="N", help="N random rounds")
    ap.add_argument("--seed", type=int, default=0, help="master seed for --fuzz")
    a = ap.parse_args(argv)
    if a.fuzz is not None:
        if a.fuzz < 1:
            ap.error("fuzz N must be >= 1")
        return run_fuzz(a.fuzz, a.seed)
    return selftest()


if __name__ == "__main__":
    sys.exit(main())
