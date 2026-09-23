"""The tag oracle and the derived `tag` column (M1.16).

Ported from the earlier harness gates/honest_tag_oracle.py and re-based onto the Alpaca record. The earlier harness
oracle read a doctrine leaf's Status LINE, took the highest maturity RUNG it ASSERTED, and
refused the leaf when the rung's min evidence count exceeded the genuine pointers the line
could show (a tag-to-EVIDENCE check, against the sibling's tag-to-tag check). Alpaca has no
self-asserted status line: the maturity tag of an obligation row is DERIVED here from the
evidence its verdict rows carry, never typed by hand ("Tags are derived, never typed":
`doctrine/leaves/test-or-UNTESTED.md`, `doctrine/leaves/verification-tags.md`).

  derive(conn, row_id, *, session=None) -> "Specced" | "Built" | "Verified"

    Fold the row's verdict rows (M1.13) into the highest maturity tag their evidence GENUINELY
    earns, RECOMPUTE the row's `tag` column from that fold, and return the derived tag. The
    counter points at the verdict rows and their pointers (M1.16 Step 2), so the column is never
    a claim that outlives its evidence (Step 3). Because the tag is derived, a hand-typed tag
    that OUT-RANKS the derived one is an over-claim: it is overwritten and the refused attempt
    is written to the record as an event (Step 1). A row absent from the store HALTs (absence
    blocks). Deriving Verified REFUSES without a behavioral probe (Step 4).

Adaptations from the earlier harness, per the plan (M1.16):

  * The rung ladder is re-read as the Alpaca tag ladder Specced < Built < Verified, each tier
    keyed to (evidence class, min genuine pointers). Specced is the floor: it owes no
    execution, so every extant row derives at least Specced.
  * The evidence class of a verdict row is resolved from its instrument through an OVERRIDABLE
    registry (G1: the registry is DATA, read from `project.yaml` when present, else the
    built-in defaults), never hard-coded per instrument.
  * The genuine-pointer counter keeps the G5 discipline in Alpaca terms: a pointer counts only
    when it resolves through `contract.resolve_pointer` (existence-checked, no `..`/absolute
    escape), is NON-whitespace, and is DISTINCT by canonical identity (one physical file cited
    twice counts once). A remote pointer is opaque and counts by its opaque body.
  * The advisory/enforce floor-file machinery, the doctrine-leaf enumerator, the status-vocab
    coupling and the symlink-escape controls of the leaf tree are dropped: there is no leaf
    tree here, only rows and their verdict rows.
"""
from __future__ import annotations

import os
from collections import OrderedDict

from alpaca import db, paths, project
from alpaca.checklist import Halt, verdict_row
from alpaca.gates import contract
from alpaca.gates import verdict as vc

INSTRUMENT = "honest-tag-oracle"

#: the event kind an over-claim overwrite is recorded under.
KIND = "tag"

# the three derived maturity tags.
SPECCED = "Specced"
BUILT = "Built"
VERIFIED = "Verified"

# evidence classes a verdict row can supply.
DESIGN = "design"
STATIC_CHECK = "static-check"
BEHAVIORAL_PROBE = "behavioral-probe"

# reason tokens controls bind to.
R_NO_SUCH_ROW = "TAG-NO-SUCH-ROW"
R_TYPED_OVER_EVIDENCE = "TAG-TYPED-OVER-EVIDENCE"

# The tag ladder, highest tier first: tag -> (required evidence class, min genuine pointers).
# Specced is the floor (min 0): it owes no machine-resolvable evidence, so every extant row
# earns it. Built needs one genuine static-check pointer; Verified needs one genuine behavioral
# probe pointer (refuse Verified without a behavioral probe). DATA, overridable.
DEFAULT_TAG_LADDER = OrderedDict([
    (VERIFIED, (BEHAVIORAL_PROBE, 1)),
    (BUILT, (STATIC_CHECK, 1)),
    (SPECCED, (DESIGN, 0)),
])

# instrument name -> the evidence class it supplies. DATA, overridable (G1). An instrument the
# registry does not name supplies no maturity class (design floor only), never a guessed tier.
DEFAULT_INSTRUMENT_CLASS = OrderedDict([
    ("behavioral-probe", BEHAVIORAL_PROBE),
    ("behavioral", BEHAVIORAL_PROBE),
    ("probe", BEHAVIORAL_PROBE),
    ("verify-probe", BEHAVIORAL_PROBE),
    ("static-check", STATIC_CHECK),
    ("static", STATIC_CHECK),
    ("lint", STATIC_CHECK),
    ("typecheck", STATIC_CHECK),
    ("build-check", STATIC_CHECK),
])

# rank of a tag on the ladder; a higher rank is a stronger claim. An unknown word ranks below
# the floor, so a garbage hand-typed tag is overwritten but is not itself counted an over-claim.
_RANK = {SPECCED: 0, BUILT: 1, VERIFIED: 2}


def _rank(tag) -> int:
    return _RANK.get(tag, -1)


def load_vocab(root=None) -> tuple:
    """Return (tag_ladder, instrument_class), read from `project.yaml` when it carries a
    `tag_oracle` block, else the built-in defaults (G1: the registry is DATA, not code).

    The optional block shape, both keys optional:

        tag_oracle:
          ladder:            {Verified: [behavioral-probe, 1], Built: [static-check, 1], ...}
          instrument_class:  {my-probe: behavioral-probe, my-lint: static-check}

    A partial block layers over the defaults so a project can register one new instrument
    without restating the whole ladder. An absent file or block yields the defaults unchanged.
    """
    ladder = OrderedDict(DEFAULT_TAG_LADDER)
    inst = OrderedDict(DEFAULT_INSTRUMENT_CLASS)
    if root is None:
        return ladder, inst
    try:
        cfg = project.load(root)
    except Exception:
        return ladder, inst
    block = (cfg or {}).get("tag_oracle") or {}
    for tag, spec in (block.get("ladder") or {}).items():
        try:
            cls, mn = spec[0], int(spec[1])
        except (TypeError, ValueError, IndexError):
            continue
        ladder[tag] = (cls, mn)
    for name, cls in (block.get("instrument_class") or {}).items():
        inst[name] = cls
    return ladder, inst


# --------------------------------------------------------- the genuine-pointer counter (G5)
def _identity(resolved: str):
    """The canonical identity of a resolved pointer, or None when it is not genuine corpus.

    A local pointer resolves to an absolute path: its identity is the physical file's
    (st_dev, st_ino) that `os.path.samefile` compares (so one file cited twice, or by two
    names, counts once), and a 0-byte or whitespace-only file is a placeholder, not corpus. A
    remote pointer resolves to an opaque body: its identity is that body string.
    """
    if os.path.isfile(resolved):
        try:
            with open(resolved, "rb") as fh:
                raw = fh.read()
        except OSError:
            return None
        if not raw.strip():                    # 0-byte or whitespace-only -> placeholder
            return None
        try:
            st = os.stat(resolved)
        except OSError:
            return None
        return ("inode", st.st_dev, st.st_ino)
    return ("opaque", resolved)


def _genuine_count(pointers, root) -> int:
    """The number of DISTINCT GENUINE evidence pointers in a verdict row's evidence list.

    Every candidate is resolved through contract.resolve_pointer (existence-checked, no `..`
    or absolute escape); an unresolvable or non-genuine pointer is dropped, never counted."""
    seen = set()
    for ptr in pointers or []:
        try:
            resolved = contract.resolve_pointer(root, ptr)
        except contract.ContractError:
            continue
        key = _identity(resolved)
        if key is None or key in seen:
            continue
        seen.add(key)
    return len(seen)


def _class_counts(conn, row_id, inst_class, root) -> dict:
    """evidence class -> the genuine pointer count its EFFECTIVE PASS verdict supplies.

    A row's verdict rows are folded per instrument, latest-wins (the same precedence the
    discharge fold uses): only the instrument's LATEST verdict is effective, so a later FAIL
    withdraws the evidence a PASS supplied. Only an effective PASS from a registered instrument
    contributes, and it contributes its class's genuine pointer count."""
    latest = OrderedDict()
    for e in verdict_row._verdict_events(conn, row_id):        # oldest first
        latest[e["data"].get("instrument")] = e               # later overwrites -> latest wins
    counts = {}
    for inst, e in latest.items():
        if e["data"].get("verdict") != vc.PASS:
            continue
        cls = inst_class.get(inst)
        if cls is None:
            continue
        n = _genuine_count(e["data"].get("evidence"), root)
        counts[cls] = max(counts.get(cls, 0), n)
    return counts


def _honest_tag(counts, ladder) -> str:
    """The highest tag the evidence genuinely earns. Walk the ladder high tier first; the first
    tier whose class shows at least its min genuine pointers wins. The min-0 tier is the floor,
    reached when no higher tier is earned, so an extant row is never below Specced."""
    floor = SPECCED
    for tag, (cls, mn) in ladder.items():
        if mn <= 0:
            floor = tag
            continue
        if counts.get(cls, 0) >= mn:
            return tag
    return floor


def assess(conn, row_id, root=None) -> dict:
    """Fold `row_id` into its honest tag WITHOUT writing anything. Returns a dict with the
    derived tag, the row's currently stored tag, the per-class genuine counts, and whether the
    stored tag is an over-claim (a typed tag out-ranking the evidence). HALTs when the row is
    absent from the store (absence blocks)."""
    stored = db.rows(conn, "rows", "id=?", (row_id,))
    if not stored:
        raise Halt(vc.BLOCKED, R_NO_SUCH_ROW, "no obligation row with id=%r" % row_id)
    if root is None:
        database = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "")
        if database and os.path.basename(os.path.dirname(database)) == ".alpaca":
            root = os.path.dirname(os.path.dirname(database))
        else:
            root = paths.root()
    ladder, inst_class = load_vocab(root)
    counts = _class_counts(conn, row_id, inst_class, root)
    honest = _honest_tag(counts, ladder)
    current = stored[0].get("tag")
    return {
        "row_id": row_id,
        "op": stored[0].get("op"),
        "derived": honest,
        "stored": current,
        "counts": counts,
        "over_claim": _rank(current) > _rank(honest),
    }


def derive(conn, row_id, *, session=None) -> str:
    """Derive the row's maturity tag from its verdict rows, recompute the `tag` column from that
    fold, and return the derived tag.

    A hand-typed tag that out-ranks the evidence is an over-claim: it is overwritten by the
    derivation and the refused attempt is written to the record as a `tag` event (Step 1). An
    under-claim (a low stored tag the evidence out-ranks) is promoted silently. The recompute
    runs whenever the discharge fold changes, so the column never outlives its evidence (Step
    3). HALTs when the row is absent (absence blocks)."""
    a = assess(conn, row_id)
    with db.transaction(conn):
        if a["over_claim"]:
            db.append_event(
                conn, session=session or "instrument", actor=INSTRUMENT, kind=KIND,
                op=a["op"], ref=row_id,
                data={"kind": KIND, "row_id": row_id, "derived": a["derived"],
                      "overwrote": a["stored"], "reason": R_TYPED_OVER_EVIDENCE,
                      "counts": a["counts"],
                      "detail": "a tag is derived, never typed: the hand-written tag %r "
                                "out-ranks the evidence, which earns only %r"
                                % (a["stored"], a["derived"])},
                conn_in_txn=True)
        db.patch(conn, "rows", "id", row_id, {"tag": a["derived"]})
    return a["derived"]


def recompute(conn, row_id, *, session=None) -> str:
    """Alias for derive read from the fold side (M1.13). Kept so the discharge fold reads as a
    recompute, not a query, while there is one implementation."""
    return derive(conn, row_id, session=session)


# ------------------------------------------------------------------------- CLI boundary
def main(argv=None) -> int:
    ap = vc.make_parser(
        name=INSTRUMENT,
        description="Derive an obligation row's maturity tag from its verdict rows")
    ap.add_argument("--row", help="the obligation row id to derive a tag for")
    ap.add_argument("--selftest", action="store_true", help="run the oracle's own controls")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not a.row:
        ap.print_help()
        return vc.USAGE
    conn = db.connect(paths.root())
    try:
        result = assess(conn, a.row)
    except Halt as h:
        conn.close()
        return vc.emit_verdict(INSTRUMENT, h.verdict, "%s: %s" % (h.code, h.detail))
    derive(conn, a.row)
    conn.close()
    # a derived tag is a report, not an adjudication of the subject; the refusal of a typed
    # over-claim is the one FAIL this boundary can return.
    if result["over_claim"]:
        return vc.emit_verdict(INSTRUMENT, vc.FAIL,
                               "%s: typed %r overwritten by derived %r"
                               % (R_TYPED_OVER_EVIDENCE, result["stored"], result["derived"]),
                               evidence=["row:%s" % a.row])
    return vc.emit_verdict(INSTRUMENT, vc.PASS, "tag=%s" % result["derived"],
                           evidence=["row:%s" % a.row])


def selftest() -> int:
    """In-memory controls: the ladder is monotone, the floor is free, Verified needs a probe,
    and an over-claim out-ranks its evidence. No subject tree is touched."""
    controls = []

    def _check(cid, ok, detail):
        controls.append((cid, "FIRED" if ok else "DID-NOT-FIRE", detail))
        return 0 if ok else 1

    ladder = OrderedDict(DEFAULT_TAG_LADDER)
    failures = 0
    failures += _check("T-01", _honest_tag({}, ladder) == SPECCED,
                       "no evidence folds to the floor Specced")
    failures += _check("T-02", _honest_tag({STATIC_CHECK: 1}, ladder) == BUILT,
                       "one static-check pointer earns Built")
    failures += _check("T-03", _honest_tag({BEHAVIORAL_PROBE: 1}, ladder) == VERIFIED,
                       "one behavioral-probe pointer earns Verified")
    failures += _check("T-04", _honest_tag({STATIC_CHECK: 9}, ladder) == BUILT,
                       "static-check alone never reaches Verified")
    failures += _check("T-05", _rank(VERIFIED) > _rank(BUILT) > _rank(SPECCED),
                       "the ladder is strictly monotone")
    print("CONTROL TABLE -- honest-tag-oracle")
    for cid, state, detail in controls:
        print("  %-5s %-12s %s" % (cid, state, detail))
    if failures:
        return vc.emit_verdict(INSTRUMENT + "-selftest", vc.FAIL,
                               "%d control(s) did not fire" % failures)
    return vc.emit_verdict(INSTRUMENT + "-selftest", vc.PASS, "every control fired")


if __name__ == "__main__":
    import sys
    sys.exit(main())
