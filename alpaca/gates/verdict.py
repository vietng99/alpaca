"""THE verdict <-> exit-code contract. Defined ONCE, here. Import it; never restate it.

Ported from the earlier harness gates/verdict.py and adapted for Alpaca: the four verdicts and the
harness band are plain module-level integers (0 PASS, 1 FAIL, 2 BLOCKED, 3 PAUSED, and the
reserved harness band 64..67), not an enum, matching the M1 interface block. There is one
definition of these numbers in this tree and it is this file; any other module that writes
the numbers next to the verdict names is restating the contract, not referencing it, and
`rc_conformance.py` reports that as a defect.

Five slots, not four: the four verdicts do not cover "you invoked me wrong" / "my own input
is missing" / "I crashed". A harness error is not a verdict about the subject, so it lives in
a reserved band (64..67) and never squats a verdict-band code.

`emit_verdict` is the sanctioned way for an instrument to end: it prints the one canonical
line AND writes a `run` row into the record, so every instrument execution is an event, not
just stdout (M1.3 Step 4).
"""
from __future__ import annotations

import argparse
import os
import sys

# ---- THE MAPPING. This is the only place these numbers are written next to the names. ----
PASS, FAIL, BLOCKED, PAUSED = 0, 1, 2, 3

# Reserved harness band: not a verdict about the subject. Kept clear of 126/127 and 128+N.
USAGE, INTERNAL, SELFTEST, ABSENT = 64, 67, 66, 65

VERDICT_BAND = frozenset({PASS, FAIL, BLOCKED, PAUSED})
HARNESS_BAND = frozenset({USAGE, INTERNAL, SELFTEST, ABSENT})

# Canonical verdict word for each code. The single source for a name shown in the record.
_NAME = {PASS: "PASS", FAIL: "FAIL", BLOCKED: "BLOCKED", PAUSED: "PAUSED-FOR-DECISION"}
_HARNESS_NAME = {USAGE: "USAGE", ABSENT: "ABSENT", SELFTEST: "SELFTEST", INTERNAL: "INTERNAL"}


class ContractError(Exception):
    """Base for every refusal this module makes. Never swallow one into a PASS."""


def name_of(code: int) -> str:
    """The canonical word for a code; a bare number for anything outside both bands."""
    if code in _NAME:
        return _NAME[code]
    if code in _HARNESS_NAME:
        return _HARNESS_NAME[code]
    return "CODE-%d" % code


def contract_map() -> dict:
    """The running verdict->code map, keyed by member name. Ground truth for the census."""
    return {"PASS": PASS, "FAIL": FAIL, "BLOCKED": BLOCKED, "PAUSED": PAUSED}


def gate_line(gate: str, code: int) -> str:
    """The one canonical result line. Callers print this; they do not compose their own."""
    if not gate or not str(gate).strip():
        raise ContractError("a result line needs a boundary name")
    return "GATE %s: %s" % (gate, name_of(code))


# ----------------------------------------------- CLI parser routed to the reserved band
class _BandParser(argparse.ArgumentParser):
    """An ArgumentParser whose usage error and --help terminate in the reserved harness band
    (USAGE=64), never on a verdict-band code. Stock argparse exits 2 on a usage error (collides
    with BLOCKED) and 0 on --help (collides with PASS); both launder a non-adjudication event
    into a verdict a caller decoding 0..3 will mis-read. Every boundary builds its parser through
    make_parser(), so the routing is automatic and identical everywhere."""

    _harness_name = ""

    def error(self, message):
        where = self._harness_name or self.prog
        sys.stderr.write("HARNESS-ERROR %s [USAGE]: %s\n" % (where, message))
        raise SystemExit(USAGE)

    def exit(self, status=0, message=None):
        if message:
            sys.stderr.write(message)
        where = self._harness_name or self.prog
        reason = "help requested" if not status else "arguments refused"
        sys.stderr.write("HARNESS-ERROR %s [USAGE]: %s\n" % (where, reason))
        raise SystemExit(USAGE)


def make_parser(*args, **kwargs):
    """THE constructor for a CLI boundary's argument parser. A drop-in for
    argparse.ArgumentParser(...) whose usage error and --help route into the reserved harness
    band instead of squatting BLOCKED (2) / PASS (0). Pass name=... to label the line; it
    defaults to the parser's prog. Sub-parsers inherit this class automatically."""
    name = kwargs.pop("name", None)
    parser = _BandParser(*args, **kwargs)
    if name:
        parser._harness_name = name
    return parser


# ------------------------------------------------------------------ census (the run row)
def _record_run(gate: str, code: int, reason: str, evidence) -> None:
    """Best-effort: append a `run` event so every instrument execution is in the record, not
    only on stdout. A record that cannot be reached (no root, read-only disk) never turns an
    instrument's own verdict into a crash, so any failure here is swallowed."""
    try:
        from alpaca import db, paths
        root = paths.root()
        # A test suite names its own checkout here so instrument runs it drives (in process or in
        # a subprocess) never write a stray record into the repository under test.
        skip = os.environ.get("ALPACA_RECORD_SKIP_ROOT")
        if skip and os.path.realpath(root) == os.path.realpath(skip):
            print("alpaca: run row for %s not recorded: ALPACA_RECORD_SKIP_ROOT names this root (%s)" % (gate, root),
                  file=sys.stderr)
            return
        conn = db.connect(root)
        try:
            db.append_event(
                conn,
                session=os.environ.get("ALPACA_SESSION_ID", "instrument"),
                actor="instrument",
                kind="run",
                ref=gate,
                data={"gate": gate, "code": code, "verdict": name_of(code),
                      "reason": reason, "evidence": list(evidence or [])})
        finally:
            conn.close()
    except Exception:
        pass


def emit_verdict(gate: str, code: int, reason: str, evidence: list | None = None) -> int:
    """Print the one canonical line, write a `run` row into the record, and return the code.

    This is the sanctioned way for a boundary to report. It is a function, not a table, so the
    census write happens at every site automatically instead of being hand-copied.
    """
    if code not in VERDICT_BAND and code not in HARNESS_BAND:
        raise ContractError("emit_verdict got a code outside both bands: %r" % (code,))
    _record_run(gate, code, reason, evidence)
    print(gate_line(gate, code))
    if reason:
        print("  reason: %s" % reason)
    for ev in (evidence or []):
        print("  evidence: %s" % ev)
    return code


# ---------------------------------------------------------------------------- selftest
def selftest() -> int:
    controls = []

    def _check(cid, ok, detail):
        controls.append((cid, "FIRED" if ok else "DID-NOT-FIRE", detail))
        return 0 if ok else 1

    failures = 0
    failures += _check("V-01", (PASS, FAIL, BLOCKED, PAUSED) == (0, 1, 2, 3),
                       "the four verdict codes are 0,1,2,3")
    failures += _check("V-02", not (VERDICT_BAND & HARNESS_BAND),
                       "verdict band and harness band are disjoint")
    failures += _check("V-03", name_of(PAUSED) == "PAUSED-FOR-DECISION",
                       "code 3 names PAUSED-FOR-DECISION, not a default")
    try:
        gate_line("", PASS)
        failures += _check("V-04", False, "gate_line refused an unnamed boundary")
    except ContractError:
        failures += _check("V-04", True, "gate_line refused an unnamed boundary")
    try:
        emit_verdict("ctl", 99, "out of band")
        failures += _check("V-05", False, "emit_verdict refused an out-of-band code")
    except ContractError:
        failures += _check("V-05", True, "emit_verdict refused an out-of-band code")

    print("CONTROL TABLE -- verdict-contract/1")
    for cid, state, detail in controls:
        print("  %-5s %-12s %s" % (cid, state, detail))
    if failures:
        return emit_verdict("verdict-contract-selftest", FAIL,
                            "%d control(s) did not fire" % failures)
    return emit_verdict("verdict-contract-selftest", PASS, "every control fired")


def main(argv=None) -> int:
    ap = make_parser(
        name="verdict-contract",
        description="The one verdict<->exit-code contract (import it; do not restate it)")
    ap.add_argument("--selftest", action="store_true", help="run the contract's own controls")
    ap.add_argument("--show", action="store_true", help="print the contract as data")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.show:
        import json
        print(json.dumps({"verdicts": contract_map(),
                          "harness_band": {_HARNESS_NAME[c]: c for c in sorted(HARNESS_BAND)}},
                         indent=2, sort_keys=True))
        return PASS
    ap.print_help()
    return USAGE


if __name__ == "__main__":
    sys.exit(main())
