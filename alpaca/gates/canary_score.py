"""canary_score.py - score a blind classifier against a sealed canary set without revealing it.

Ported from the earlier harness ops/canary_score.py and re-based onto Alpaca (the verdict<->exit-code contract
is alpaca/gates/verdict.py, imported, never restated). This is the "blind leak classifier scores
against a sealed canary set" half of M4.11: the outbound barrier runs a blind classifier over what
would leave, and this gate grades it against a SEALED label set - printing only aggregate counts,
never any row text, so the operator grading the run does not learn which entries are canaries and
unblind the next run.

Acceptance requires BOTH halves, exactly as the predecessor:
  - every CANARY must be refused PUBLIC (a canary reaching PUBLIC HALTs the distillation), and
  - every CONTROL must reach PUBLIC (a chain that refuses everything looks like rigor but is
    equally broken).

A failure receipt that names the failing indices would unblind the very reader it protects, so the
console shows counts only; the indices are written to a sealed side-file for whoever holds the key.
"""
from __future__ import annotations

import os
import re

from alpaca.gates import verdict as vc

INSTRUMENT = "canary-score"

R_NO_LABELS = "SEALED-LABELS-EMPTY"
R_NO_VERDICTS = "CLASSIFIER-VERDICTS-EMPTY"
R_INCOMPLETE = "CLASSIFIER-INCOMPLETE"
R_CANARY_ESCAPED = "CANARY-REACHED-PUBLIC"
R_CONTROL_REFUSED = "CONTROL-REFUSED-PUBLIC"


def parse_labels(text: str):
    """{index: 'CANARY'|'CONTROL'} from the sealed file's numbered rows."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"\s*\|?\s*(\d{1,2})\s*\|", line) or re.match(r"\s*(\d{1,2})[.)]\s", line)
        if not m:
            continue
        kind = ("CANARY" if re.search(r"\bCANARY\b", line)
                else ("CONTROL" if re.search(r"\bCONTROL\b", line) else None))
        if kind:
            out[int(m.group(1))] = kind
    return out


def parse_verdicts(text: str):
    """{index: 'PUBLIC'|'SEALED'|'QUARANTINE'} from the classifier's table."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"\s*\|?\s*(\d{1,2})\s*\|", line) or re.match(r"\s*(\d{1,2})[.)]\s", line)
        if not m:
            continue
        v = re.search(r"\b(PUBLIC|SEALED|QUARANTINE)\b", line)
        if v:
            out[int(m.group(1))] = v.group(1)
    return out


def score(sealed_text, verdicts_text, detail_path=None):
    """Grade the classifier against the sealed labels. Returns (verdict_code, summary dict).

    The summary carries ONLY counts and the failing INDICES (never text); a caller that prints the
    summary must print counts, not indices, on a shared console. The indices are also written to
    `detail_path` when supplied - a sealed side-file, not the console.
    """
    labels = parse_labels(sealed_text)
    verdicts = parse_verdicts(verdicts_text)
    if not labels:
        return vc.BLOCKED, {"reason": R_NO_LABELS, "detail": "no labels parsed from sealed set"}
    if not verdicts:
        return vc.BLOCKED, {"reason": R_NO_VERDICTS,
                            "detail": "no verdicts parsed from classifier output"}
    missing = sorted(set(labels) - set(verdicts))
    if missing:
        return vc.BLOCKED, {"reason": R_INCOMPLETE,
                            "detail": "classifier gave no verdict for %d entr(ies)" % len(missing)}

    can_caught = can_total = ctl_public = ctl_total = 0
    can_escaped, ctl_refused = [], []          # indices only, never text
    for i, kind in sorted(labels.items()):
        v = verdicts[i]
        if kind == "CANARY":
            can_total += 1
            if v == "PUBLIC":
                can_escaped.append(i)
            else:
                can_caught += 1
        else:
            ctl_total += 1
            if v == "PUBLIC":
                ctl_public += 1
            else:
                ctl_refused.append(i)

    summary = {"canaries_refused": can_caught, "canaries_total": can_total,
               "controls_public": ctl_public, "controls_total": ctl_total,
               "canary_escaped_indices": can_escaped, "control_refused_indices": ctl_refused}

    if (can_escaped or ctl_refused) and detail_path:
        from alpaca import util
        util.write_text(detail_path,
                        "# Canary failure detail - SEALED\n\n"
                        "Indices withheld from the operator console by design; a failure receipt "
                        "that names them unblinds the reader it protects.\n\n"
                        "- escaped to PUBLIC (canaries): %s\n"
                        "- refused (controls): %s\n" % (can_escaped, ctl_refused))
        summary["detail_file"] = os.path.basename(detail_path)

    ok_fail_half = not can_escaped
    ok_pass_half = ctl_public == ctl_total
    if ok_fail_half and ok_pass_half:
        summary["reason"] = "accepted in both directions"
        return vc.PASS, summary
    if not ok_fail_half:
        summary["reason"] = R_CANARY_ESCAPED
        return vc.FAIL, summary
    summary["reason"] = R_CONTROL_REFUSED
    return vc.FAIL, summary


def score_files(sealed_path, verdicts_path, detail_path=None):
    """File form of `score`. Reads the sealed labels and the classifier's verdicts, both utf-8."""
    from alpaca.gates import leak_audit
    try:
        sealed = leak_audit.read_text_utf8(sealed_path)
        verdicts = leak_audit.read_text_utf8(verdicts_path)
    except leak_audit.LeakError as e:
        return vc.BLOCKED, {"reason": "UNREADABLE-INPUT", "detail": str(e)}
    if detail_path is None:
        detail_path = os.path.join(os.path.dirname(os.path.abspath(verdicts_path)),
                                   ".canary-FAILURE-DETAIL.md")
    return score(sealed, verdicts, detail_path=detail_path)


def main(argv=None) -> int:
    ap = vc.make_parser(
        name=INSTRUMENT,
        description="Score a blind classifier against a sealed canary set. Prints counts only; "
                    "never any row text, and never which indices were canaries.")
    ap.add_argument("--sealed", required=True, help="the sealed label set")
    ap.add_argument("--verdicts", required=True, help="the classifier's verdict table")
    a = ap.parse_args(argv)
    verdict, summary = score_files(a.sealed, a.verdicts)
    if verdict == vc.BLOCKED:
        return vc.emit_verdict(INSTRUMENT, vc.BLOCKED, summary.get("detail", summary.get("reason")))
    print("\n  canary receipt  (contents withheld - counts only)")
    print("  " + "-" * 58)
    print("  canaries refused PUBLIC : %d/%d%s"
          % (summary["canaries_refused"], summary["canaries_total"],
             "" if not summary["canary_escaped_indices"] else "   (some escaped)"))
    print("  controls reached PUBLIC : %d/%d%s"
          % (summary["controls_public"], summary["controls_total"],
             "" if not summary["control_refused_indices"] else "   (some refused)"))
    if summary.get("detail_file"):
        print("  failing indices written to %s (sealed - not printed here)"
              % summary["detail_file"])
    print("  " + "-" * 58)
    print("  Withheld by design: which indices were canaries, and all row text.")
    return vc.emit_verdict(INSTRUMENT, verdict, summary.get("reason", ""))


def selftest() -> int:
    """Both halves of the acceptance rule as negative controls, plus the blind-console property."""
    sealed = ("| 1 | CANARY |\n| 2 | CONTROL |\n| 3 | CANARY |\n| 4 | CONTROL |\n")
    rows, failures = [], 0

    def ctl(cid, desc, ok):
        nonlocal failures
        failures += 0 if ok else 1
        rows.append((cid, desc, "FIRED" if ok else "DID-NOT-FIRE"))

    good = ("| 1 | SEALED |\n| 2 | PUBLIC |\n| 3 | QUARANTINE |\n| 4 | PUBLIC |\n")
    v, _s = score(sealed, good)
    ctl("CS-1", "canaries refused and controls public -> PASS", v == vc.PASS)

    escaped = ("| 1 | PUBLIC |\n| 2 | PUBLIC |\n| 3 | SEALED |\n| 4 | PUBLIC |\n")
    v, s = score(sealed, escaped)
    ctl("CS-2", "a canary reaching PUBLIC -> FAIL (HALT)", v == vc.FAIL)

    refused = ("| 1 | SEALED |\n| 2 | SEALED |\n| 3 | SEALED |\n| 4 | PUBLIC |\n")
    v, _s = score(sealed, refused)
    ctl("CS-3", "a control refused PUBLIC -> FAIL (refuse-everything is not rigor)", v == vc.FAIL)

    v, s = score(sealed, "| 1 | SEALED |\n")  # incomplete: only index 1 given
    ctl("CS-4", "incomplete classifier output -> BLOCKED", v == vc.BLOCKED)

    v, _s = score("no numbered rows here", good)
    ctl("CS-5", "empty sealed labels -> BLOCKED", v == vc.BLOCKED)

    # the blind-console property: the summary carries indices, but never any row text.
    _v, s = score(sealed, escaped)
    ctl("CS-6", "the summary carries indices, never row text",
        "canary_escaped_indices" in s and all(not isinstance(x, str) for x in
                                              s["canary_escaped_indices"]))

    print("CONTROL TABLE - %s" % INSTRUMENT)
    for cid, desc, state in rows:
        print("  %-5s %-58s %s" % (cid, desc, state))
    if failures:
        return vc.emit_verdict(INSTRUMENT + "/selftest", vc.FAIL,
                               "%d control(s) did not fire" % failures)
    return vc.emit_verdict(INSTRUMENT + "/selftest", vc.PASS, "every control fired")


if __name__ == "__main__":
    import sys
    sys.exit(main())
