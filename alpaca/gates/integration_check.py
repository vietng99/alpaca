"""integration_check.py - the cross-instrument INTEGRATION / seam gate (M1.17 Step 2).

Ported from the earlier harness gates/integration_check.py and re-based onto Alpaca. The earlier harness drove a signing
lifecycle (open a row, two operators sign, the row reaches SIGNED). Alpaca has no signing; the
equivalent end-to-end seam is the DISCHARGE flow (spec section 6 REBUILD row 43: sign rows became
verdict rows). So this gate replaces the signing flow with a discharge flow and drives it across
several instruments in a THROWAWAY tempdir, each hop hard-asserted:

    parse an acceptance artifact  (alpaca.checklist.artifact)
      -> synthesize obligation rows      (alpaca.checklist.synthesis)
      -> land them in the rows table      (alpaca.db)
      -> discharge a PASS verdict row     (alpaca.checklist.verdict_row)      => status folds discharged
      -> derive the maturity tag          (alpaca.gates.honest_tag_oracle)    => a static-check PASS is Built
      -> discharge a stale-hash verdict                                    => HALTs (drift), the seam refuses
      -> discharge a later FAIL           (alpaca.checklist.verdict_row)      => status folds failed, tag drops

Every unit self-verifies in isolation with its own selftest; none of those prove the units still
fit TOGETHER once wired end-to-end through the record. This gate is that missing check. It authors
nothing durable in the tree it is handed: it builds its own throwaway project (its own ALPACA-MANIFEST
and .alpaca/alpaca.db under a tempdir) and removes it, so a run never mutates the operator's real state.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from collections import namedtuple

from alpaca import db
from alpaca.checklist import Halt, artifact, synthesis, verdict_row
from alpaca.gates import honest_tag_oracle as oracle
from alpaca.gates import verdict as vc

INSTRUMENT = "integration-check"

Result = namedtuple("Result", "verdict reasons info")

# a small, known-clean acceptance artifact (the same shape the row tests use).
_SPEC = """# Sample spec

## Acceptance

| item  | statement                          | oracle class | proof kind |
| ----- | ---------------------------------- | ------------ | ---------- |
| AC-01 | the parser reads a clean table     | unit         | test       |
| AC-02 | residue outside the table is BLOCK | unit         | test       |
| AC-03 | keys are unique inside the table   | unit         | test       |

## Open questions

None.
"""

# a single generic step whose obligation is above the statement floor after substitution. No
# `consumes`, so it applies to every item (the parsed artifact carries no kind).
_STEP_MODEL = {
    "model_id": "integration-fixture",
    "steps": [
        {"key": "intake-review", "ordinal": 1, "name": "intake",
         "obligation": "the obligation {item} in {artifact} is reviewed against the acceptance "
                       "table before the next boundary opens",
         "report_back": {"where": "the host the review ran on", "how": "the review command",
                         "when": "before the next boundary"}},
    ],
}


class _Controls:
    """One row per asserted hop, generated from an executed run (never asserted in prose)."""

    def __init__(self):
        self.rows = []
        self.failures = 0

    def check(self, cid, desc, ok, observed):
        self.rows.append((cid, desc, "FIRED" if ok else "DID-NOT-FIRE", observed))
        if not ok:
            self.failures += 1
        return ok


def _persist(conn, rows):
    dbcols = ("id", "kind", "op", "phase", "step", "statement", "proof", "where_", "how",
              "when_", "why", "session", "operator", "status", "tag", "content_hash",
              "prev_hash", "supersedes")
    with db.transaction(conn):
        for r in rows:
            dbrow = {c: None for c in dbcols}
            dbrow.update({
                "id": r["id"], "kind": r["kind"], "op": r.get("op"), "phase": r.get("phase"),
                "step": r.get("step"), "statement": r.get("statement"), "proof": r.get("proof"),
                "where_": r.get("where"), "how": r.get("how"), "when_": r.get("when"),
                "why": r.get("why"), "session": r.get("session"), "operator": r.get("operator"),
                "status": r.get("status"), "tag": r.get("tag"),
                "content_hash": r.get("content_hash"), "prev_hash": r.get("prev_hash"),
                "supersedes": r.get("supersedes"),
            })
            db.upsert(conn, "rows", "id", dbrow)


def _run_flow(ctl, root):
    """Drive the discharge seam end to end inside `root` (a throwaway project). Each hop is a
    hard assertion; a broken hop is a control that did not fire."""
    conn = db.connect(root)
    try:
        # H-1 parse + synthesize -> one row per (step, item) = 3 rows.
        spec = os.path.join(root, "spec.md")
        with open(spec, "w", encoding="utf-8") as fh:
            fh.write(_SPEC)
        art = artifact.parse(spec, "item")
        rows = synthesis.synthesize(_STEP_MODEL, art, op="op-int", session="sess-int",
                                    operator="op-int")
        ctl.check("I-1", "artifact.parse + synthesize derive one row per (step, item)",
                  len(rows) == 3, "rows=%d" % len(rows))
        target = rows[0]

        # H-2 land the rows in the record's rows table; re-read holds all three.
        _persist(conn, rows)
        held = {r["id"] for r in db.rows(conn, "rows", "1=1")}
        ctl.check("I-2", "the synthesized rows land in the rows table",
                  set(r["id"] for r in rows).issubset(held), "held=%d" % len(held))

        # H-3 discharge a PASS verdict bound to the frozen content -> status folds discharged.
        ev = os.path.join(root, "build.log")
        with open(ev, "w", encoding="utf-8") as fh:
            fh.write("genuine static-check evidence\n")
        verdict_row.discharge(conn, target["id"], target["content_hash"],
                              instrument="static-check", verdict=vc.PASS,
                              evidence=["local:build.log"], level="L2", session="s1")
        ctl.check("I-3", "a PASS verdict row folds the obligation to discharged",
                  verdict_row.status_fold(conn, target["id"]) == verdict_row.DISCHARGED,
                  "fold=%s" % verdict_row.status_fold(conn, target["id"]))

        # H-4 the tag oracle derives Built from that static-check PASS with a genuine pointer.
        tag = oracle.derive(conn, target["id"], session="s1")
        ctl.check("I-4", "the tag oracle derives Built from a static-check PASS + genuine pointer",
                  tag == oracle.BUILT, "tag=%s" % tag)

        # H-5 (negative) a verdict binding a STALE content hash HALTs (drift), the seam refuses.
        drift_fired = False
        try:
            verdict_row.discharge(conn, target["id"], "stale-hash-does-not-match",
                                  instrument="static-check", verdict=vc.PASS, evidence=[],
                                  level="L2", session="s1")
        except Halt as h:
            drift_fired = h.verdict == vc.BLOCKED and h.code == verdict_row.R_DRIFT
        ctl.check("I-5", "a verdict binding a stale content hash HALTs (drift), never certifies",
                  drift_fired, "halt=%s" % drift_fired)

        # H-6 (negative) a later FAIL verdict reopens the row and the tag drops to the floor.
        verdict_row.discharge(conn, target["id"], target["content_hash"],
                              instrument="static-check", verdict=vc.FAIL,
                              evidence=["local:build.log"], level="L2", session="s1")
        reopened = verdict_row.status_fold(conn, target["id"]) == verdict_row.FAILED
        dropped = db.rows(conn, "rows", "id=?", (target["id"],))[0]["tag"] == oracle.SPECCED
        ctl.check("I-6", "a later FAIL reopens the row and the derived tag drops to the floor",
                  reopened and dropped, "reopened=%s dropped=%s" % (reopened, dropped))
    finally:
        conn.close()


def audit() -> Result:
    """Run the seam flows in a fresh throwaway project and fold the controls to a verdict. Authors
    nothing durable; the tempdir (its own record and ALPACA-MANIFEST) is removed at the end."""
    base = tempfile.mkdtemp(prefix="integration-check-")
    root = os.path.join(base, "proj")
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "ALPACA-MANIFEST"), "w", encoding="utf-8") as fh:
        fh.write("alpaca\n")
    os.makedirs(os.path.join(root, "style"), exist_ok=True)
    with open(os.path.join(root, "style", "banned.txt"), "w", encoding="utf-8") as fh:
        fh.write("")
    # point root discovery at the throwaway project so the tag oracle resolves pointers there.
    saved = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = root
    ctl = _Controls()
    try:
        _run_flow(ctl, root)
    except Exception as e:                                   # a hop raised instead of asserting
        ctl.check("I-X", "the flow raised instead of asserting", False,
                  "%s: %s" % (type(e).__name__, str(e)[:80]))
    finally:
        if saved is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = saved
        shutil.rmtree(base, ignore_errors=True)

    info = {"controls": len(ctl.rows), "failures": ctl.failures, "table": ctl.rows}
    if not ctl.rows:
        return Result(vc.BLOCKED, ["INTEGRATION-NO-FLOW: no hop ran; nothing was integrated"], info)
    if ctl.failures:
        reasons = ["INTEGRATION-HOP-BROKE: %s (%s)" % (desc, obs)
                   for _cid, desc, state, obs in ctl.rows if state == "DID-NOT-FIRE"]
        return Result(vc.FAIL, reasons, info)
    return Result(vc.PASS, [], info)


def check(root) -> int:
    """The uniform instrument entry. The passed root is NOT touched: the flow runs entirely in a
    throwaway project of its own, so a boot-check can run this without mutating the real tree."""
    return audit().verdict


# ------------------------------------------------------------------------- CLI boundary
def main(argv=None) -> int:
    ap = vc.make_parser(
        name=INSTRUMENT,
        description="Cross-instrument seam gate: a real discharge flow across artifact, "
                    "synthesis, verdict rows and the tag oracle in a throwaway project.")
    ap.add_argument("--selftest", action="store_true",
                    help="run the cross-instrument flow and emit the control table")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    res = audit()
    for cid, desc, state, obs in res.info.get("table", []):
        print("  %-5s %-58s %-12s %s" % (cid, desc, state, obs))
    return vc.emit_verdict(INSTRUMENT, res.verdict,
                           res.reasons[0] if res.reasons else "the discharge seam holds end to end")


def selftest() -> int:
    res = audit()
    print("CONTROL TABLE - %s (every row from an executed cross-instrument run)" % INSTRUMENT)
    for cid, desc, state, obs in res.info.get("table", []):
        print("  %-5s %-58s %-12s %s" % (cid, desc, state, obs))
    print("  %d control(s), %d did not fire" % (res.info.get("controls", 0),
                                                res.info.get("failures", 0)))
    return vc.emit_verdict(INSTRUMENT + "-selftest", res.verdict,
                           res.reasons[0] if res.reasons else "every control fired")


if __name__ == "__main__":
    import sys
    sys.exit(main())
