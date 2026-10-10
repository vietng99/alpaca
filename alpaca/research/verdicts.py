"""Verdict checks, the effective verdict, the round fold, the cap rule, the ruling and the outcome
(design t-089, verifier.md sections 4 and 6). Pure, no launch.

A verifier gives a verdict per claim; code turns it into an effective verdict (section 4.4), a
slot verdict word (section 4.5), a fold of both slots per round (section 6.2), the cap rule
(section 6.3), the ruling (section 6.3) and the outcome of the whole verification (section 6.6).
`check_verdict` is the gate a submitted verdict passes before it counts (section 4.3, checks C1
to C14); it names every problem, not the first.
"""

from __future__ import annotations

import json
import os
import re

from alpaca.gates import verdict as vc
from alpaca.research import events as rev_events, pathscan, rerun

SLOTS = ("v1", "v2")
ROUND_CAP = 5            # re-check rounds after round 0 (FR-033)
EXTEND_ROUNDS = 5        # one extension adds at most 5 rounds (DD-09)
EXTENSIONS_MAX = 1       # at most one extension per verification
HARD_CAP = ROUND_CAP + EXTEND_ROUNDS * EXTENSIONS_MAX   # 10: a ruling is forced here
MAX_CLAIMS = 50
VERDICTS = ("supported", "refuted", "cannot-verify")
AGENT_REASONS = ("no-evidence", "not-reproducible", "environment", "out-of-reach", "apply-failure")
CODE_REASONS = ("conditional", "infrastructure")
RETRYABLE = ("conditional", "infrastructure")
OUTCOMES = ("verified", "refuted", "unverified", "superseded")
LOST_MAX = 2             # lost tries plus tries with a dropped refutation a verification survives (section 6.5)

VERDICT_SCHEMA = rerun.VERDICT_SCHEMA
CLAIM_ID = re.compile(r"^[a-z][0-9]{1,3}$")
COND_ID = re.compile(r"^k[0-9]{1,2}$")
STOCK = ("none", "n/a", "not applicable", "nothing", "no way", "it cannot be wrong")
EVIDENCE_BASE = 8        # evidence items per claim, plus the receipts C12 asks of it

#: section 10.4: the exit of each refusal code, from the verdict contract.
EXITS = {
    "VERIFY-NOT-SEALED": vc.BLOCKED, "VERIFY-NO-CLAIMS": vc.FAIL,
    "VERIFY-TOO-MANY-CLAIMS": vc.FAIL, "VERIFY-RUNTIME-UNKNOWN": vc.BLOCKED,
    "VERIFY-RUNTIME-MISMATCH": vc.BLOCKED, "VERIFY-BUSY": vc.BLOCKED,
    "VERIFY-EXHAUSTED": vc.BLOCKED, "VERIFY-CLAIMS-CHANGED": vc.BLOCKED, "VERIFY-TREE": vc.BLOCKED,
    "VERIFY-NO-COMMAND": vc.FAIL, "VERIFY-STEERING": vc.FAIL, "VERIFY-SELF": vc.FAIL,
    "VERIFY-BRIEF-NOT-BLIND": vc.FAIL, "VERIFY-SCHEMA": vc.FAIL, "VERIFY-RECEIPT": vc.FAIL,
    "VERIFY-BREACH": vc.FAIL, "VERIFY-PACKET-CHANGED": vc.FAIL, "VERIFY-HOLD": vc.PAUSED,
    # not in the section 10.4 table (a design gap): require_verified refuses the champion merge
    # with it (section 8.6), the exit of a refusal
    "VERIFY-UNVERIFIED": vc.FAIL,
}
#: the codes a failed try is refused with (section 6.5); `verify_gate.refuted_runs` counts them.
FAILED_CODES = ("VERIFY-SCHEMA", "VERIFY-RECEIPT", "VERIFY-BREACH", "VERIFY-PACKET-CHANGED",
                "VERIFY-NO-VERDICT")
#: the refusals that stop the driver (section 6.7); none of them is a try.
STOP_CODES = ("VERIFY-SELF", "VERIFY-BRIEF-NOT-BLIND", "VERIFY-TREE", "VERIFY-CLAIMS-CHANGED",
              "VERIFY-RUNTIME-MISMATCH", "VERIFY-STEERING", "VERIFY-NO-COMMAND",
              "VERIFY-NO-CLAIMS", "VERIFY-TOO-MANY-CLAIMS")
_SCHEMA_CHECKS = ("C1", "C2", "C3", "C5", "C6", "C7", "C8", "C9")

PROOF_VERDICT = rev_events.PROOF_VERDICT
VERIFY_ROUND = rev_events.VERIFY_ROUND
PROOF_EXTEND = rev_events.PROOF_EXTEND
PROOF_RULING = rev_events.PROOF_RULING
VERIFY_OPEN = rev_events.VERIFY_OPEN
VERIFY_REFUSED = rev_events.VERIFY_REFUSED
VERIFY_END = rev_events.VERIFY_END

_ANSWER = re.compile(r"rerun: (?:receipt ([a-z0-9][a-z0-9-]{0,40}) ([0-9a-f]{64})"
                     r"|verdict ([0-9a-f]{64})|refused (.*))")


class VerifyError(Exception):
    """A verification refusal: `code` the VERIFY-* code, `reason` the text, `verdict` the exit
    code (alpaca/gates/verdict.py), `problems` every problem found, each a dict {code, check,
    rule, call, claim, detail} or a string."""

    def __init__(self, code: str, reason: str, *, verdict: int, problems: list | None = None):
        self.code = str(code)
        self.reason = str(reason)
        self.verdict = int(verdict)
        self.problems = list(problems or [])
        super().__init__("%s: %s" % (self.code, self.reason))


def error(code: str, reason: str, problems=None) -> VerifyError:
    """The VerifyError for `code` with its exit from EXITS."""
    return VerifyError(code, reason, verdict=EXITS.get(code, vc.FAIL), problems=problems)


# --------------------------------------------------------------------------- effective verdicts
def effective(entry: dict, *, requires: str, apply_failed: bool) -> tuple:
    """(verdict, reason) a claim counts as (section 4.4): an apply failure refutes `x1` and turns
    every other supported claim that needs a re-run into cannot-verify `apply-failure`; a
    supported claim with a condition that did not run is cannot-verify `conditional`; else the
    verdict and reason as written."""
    verdict, reason = entry.get("verdict"), entry.get("reason")
    if apply_failed:
        if entry.get("id") == "x1":
            return "refuted", None
        if verdict == "supported" and requires == "rerun":
            return "cannot-verify", "apply-failure"
    if verdict == "supported" and any(
            isinstance(c, dict) and not c.get("ran") for c in entry.get("conditions") or ()):
        return "cannot-verify", "conditional"
    return verdict, reason


def slot_verdict(effectives: dict) -> str:
    """PASS when every claim is effectively supported, FAIL when any is refuted, else BLOCKED.
    `effectives` maps claim id to (verdict, reason)."""
    values = [v[0] if isinstance(v, (tuple, list)) else v for v in effectives.values()]
    if any(v == "refuted" for v in values):
        return "FAIL"
    if values and all(v == "supported" for v in values):
        return "PASS"
    return "BLOCKED"


def band(word: str) -> int:
    return {"PASS": vc.PASS, "FAIL": vc.FAIL, "BLOCKED": vc.BLOCKED}.get(word, vc.BLOCKED)


# --------------------------------------------------------------------------- stream reading
def stream_invocations(stream_calls: list, items=()) -> list:
    """Every request-client invocation in the stream, in order: {"call" (1-based position),
    "args", "rid" (the receipt id the arguments ask for, rerun.request_id; None for a submission,
    for arguments that do not parse and for a word the scan cannot know), "output" (the call's
    output or None), "answers" (the answer lines the output shows, in order), "heredoc"
    ({"quoted", "body"} or None), "index" (its place among the call's invocations),
    "refused_index" (its place among the call's invocations that name no receipt), "calls" (how
    many calls the stream has)}. Each call is read as the breach scan reads it: a `for` loop over
    written words is its body once for each word (pathscan.unroll), so a receipt made inside a
    loop has an invocation of its own (in-session-runtime.md section 18.5). A call of kind
    `denied` (a call the guard refused) printed nothing and was served nothing, and the scan
    skips it. `items` is the try's gate item list, for the id a `--gate` request derives."""
    out = []
    for pos, call in enumerate(stream_calls or [], 1):
        text = pathscan.shell_text(call)
        if text is None:
            continue
        got = pathscan.unwrap(pathscan.parse(text))
        rolled = pathscan.unroll(got)
        if not (rolled.get("error") and not got.get("error")):
            got = rolled
        output = call.get("output")
        if isinstance(output, list):
            output = "\n".join(str(o) for o in output)
        answers = []
        if isinstance(output, str):
            for line in output.splitlines():
                m = _ANSWER.search(line)
                if m:
                    answers.append(m.group(0).strip())
        k = r = 0
        for cmd in got["commands"]:
            args = pathscan.rerun_args(cmd["argv"])
            if args is None:
                continue
            live = list(cmd.get("live") or [None] * len(cmd["argv"]))[2:]
            doc = None
            if cmd["heredocs"]:
                h = got["heredocs"][cmd["heredocs"][-1]]
                doc = {"quoted": h["quoted"], "body": h["body"]}
            rid = rerun.request_id(args, items, live)
            out.append({"call": pos, "args": args, "rid": rid,
                        "output": output if isinstance(output, str) else None,
                        "answers": answers, "heredoc": doc, "index": k, "refused_index": r,
                        "calls": len(stream_calls)})
            k += 1
            if rid is None and args[:2] != ["--submit", "-"]:
                r += 1
    return out


def _hash_of(line: str) -> str:
    return str(line).split()[-1] if line else ""


def answer_of(inv: dict, served: dict | None = None) -> str | None:
    """The answer line of one invocation, found by its text in the lines its call printed
    (in-session-runtime.md section 18.5): for a receipt, the whole line `rerun: receipt <rid> <sha>`
    that names its id; for a submission, the call's `rerun: verdict` line; for a request that
    names no receipt (one the client refused), the `rerun: refused` line the invocation's place
    among the call's such invocations gives. When several lines of one call name the same id, or
    several show a verdict, the answer is the one whose hash equals the hash the driver's ledger
    kept for that id, or for the submission it served, and there is none when no line does:
    an `echo` of the same text beside the real answer cannot stand for it."""
    served = served or {}
    answers = inv.get("answers") or []
    if inv["args"][:2] == ["--submit", "-"]:
        lines = [a for a in answers if a.startswith("rerun: verdict ")]
        if len(lines) <= 1:
            return lines[0] if lines else None
        want = {served.get("verdict_sha256")} | {
            r.get("sha256") for r in (served.get("requests") or {}).values()
            if r.get("kind") == "submit"}
        hit = [ln for ln in lines if _hash_of(ln) in want]
        return hit[-1] if hit else None
    rid = inv.get("rid")
    if rid is not None:
        lines = [a for a in answers if a.startswith("rerun: receipt %s " % rid)]
        if len(lines) <= 1:
            return lines[0] if lines else None
        kept = (served.get("receipts") or {}).get(rid)
        hit = [ln for ln in lines if kept is not None and _hash_of(ln) == kept]
        return hit[-1] if hit else None
    lines = [a for a in answers if a.startswith("rerun: refused")]
    place = inv.get("refused_index", 0)
    return lines[place] if place < len(lines) else None


def last_submission(stream_calls: list) -> dict | None:
    """The last `bin/rerun --submit -` invocation in the stream, or None."""
    subs = [i for i in stream_invocations(stream_calls) if i["args"][:2] == ["--submit", "-"]]
    return subs[-1] if subs else None


def refuted_in(body, scope) -> list:
    """The claim ids of `scope` that a submitted body marks refuted (a dropped refutation when the
    submission does not count, section 6.5). An unreadable body marks none."""
    try:
        raw = json.loads(body) if isinstance(body, str) else body
    except ValueError:
        return []
    if not isinstance(raw, dict) or not isinstance(raw.get("claims"), list):
        return []
    want = set(scope or ())
    out = []
    for c in raw["claims"]:
        if isinstance(c, dict) and c.get("verdict") == "refuted" and c.get("id") in want \
                and c["id"] not in out:
            out.append(c["id"])
    return sorted(out)


def _strip_nl(text) -> str:
    text = str(text or "")
    return text[:-1] if text.endswith("\n") else text


def receipt_key(rec: dict) -> tuple:
    """The key of a receipt (section 4.1): command and cwd, stage and side, pointer, or event."""
    kind = rec.get("kind")
    if kind == "rerun":
        return ("rerun", " ".join(str(rec.get("command") or "").split()), rec.get("cwd"))
    if kind == "gate":
        return ("gate", rec.get("stage"), rec.get("side"))
    if kind == "rehash":
        return ("rehash", rec.get("pointer"))
    if kind == "event":
        return ("event", rec.get("event_id"))
    return (kind,)


def receipt_result(rec: dict) -> tuple:
    kind = rec.get("kind")
    if kind == "rerun":
        return (rec.get("rc"), bool(rec.get("timed_out")))
    if kind == "gate":
        return (rec.get("rc"), rec.get("verdict"))
    if kind == "rehash":
        return (rec.get("sha256"),)
    if kind == "event":
        return (rec.get("hash"),)
    return ()


# --------------------------------------------------------------------------- the checks
READ_TOPS = ("packet", "tree", "base")


def _read_ok(folder: str, rel: str, parts: list) -> bool:
    """True when `rel` is a `{"read": ...}` evidence path C4 accepts (in-session-runtime.md
    section 18.4): a regular file under packet/, tree/ or base/ of the try folder. It is refused
    when it is absolute, holds `..`, starts with any other folder (out/, receipts/,
    tree.git-admin/), passes through a link, resolves outside its own top folder or names no
    regular file."""
    if os.path.isabs(rel) or ".." in parts or len(parts) < 2 or parts[0] not in READ_TOPS \
            or "" in parts[:-1]:
        return False
    cur = folder
    for part in parts:
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            return False
    if not os.path.isfile(cur):
        return False
    top = os.path.realpath(os.path.join(folder, parts[0]))
    real = os.path.realpath(cur)
    return real.startswith(top.rstrip(os.sep) + os.sep)


def _text_ok(value, lo, hi) -> bool:
    return isinstance(value, str) and lo <= len(value.strip()) <= hi


def _norm(text) -> str:
    return " ".join(str(text or "").lower().split())


def check_verdict(brief: dict, raw: dict, *, receipts_dir: str, stream_calls: list, served: dict,
                  commands: list) -> dict:
    """The verdict as it will be recorded, or VerifyError naming every problem (section 4.3).

    `brief` is the try's brief (its `verification`, `slot`, `round`, `try`, `claims`, `audit`
    and `engine` keys are read); `raw` the driver's copy of the last submission, parsed;
    `receipts_dir` the try's `receipts/`; `stream_calls` the launch's tool calls
    (launcher.tool_calls); `served` the driver's ledger of the try ({"requests": {n: {...}},
    "verdict_sha256", "verdict_body"}, or {} on resume); `commands` the claims in scope with
    their `commands`, `pointers` and `requires` (C12). Returns {"claims", "summary",
    "apply_failed", "apply_receipt", "slot_verdict", "conditional", "verdict_sha256",
    "receipts"}."""
    problems = []

    def bad(check, detail, claim=None, rule=None, call=None):
        code = "VERIFY-SCHEMA" if check in _SCHEMA_CHECKS else "VERIFY-RECEIPT"
        problems.append({"code": code, "check": check, "rule": rule, "call": call,
                         "claim": claim, "detail": detail})

    served = served or {}
    ledger = served.get("requests") or {}
    scope = [c for c in (commands if commands is not None else brief.get("claims") or [])
             if isinstance(c, dict)]
    by_id = {c["id"]: c for c in scope}
    engine = brief.get("engine") if isinstance(brief.get("engine"), dict) else {}
    invs = stream_invocations(stream_calls, engine.get("stages") or [])

    # C1
    if not isinstance(raw, dict):
        bad("C1", "the verdict is not a JSON object")
        raise error("VERIFY-SCHEMA", "the verdict is not a JSON object", problems)
    for key, want in (("schema", VERDICT_SCHEMA), ("verification", brief.get("verification")),
                      ("slot", brief.get("slot")), ("round", brief.get("round"))):
        if raw.get(key) != want:
            bad("C1", "%s is %r, the brief says %r" % (key, raw.get(key), want))
    # C8 summary
    if not _text_ok(raw.get("summary"), 20, 2000):
        bad("C8", "summary must be 20 to 2000 characters")
    # C9
    audit = brief.get("audit") if isinstance(brief.get("audit"), dict) else None
    want_audit = audit.get("event") if audit else None
    if raw.get("audit_event") != want_audit:
        bad("C9", "audit_event is %r, the brief gave %r" % (raw.get("audit_event"), want_audit))
    # C2
    claims = raw.get("claims")
    if not isinstance(claims, list):
        bad("C2", "claims must be a list")
        claims = []
    seen = []
    for c in claims:
        cid = c.get("id") if isinstance(c, dict) else None
        if cid in seen:
            bad("C2", "claim %s appears twice" % cid, claim=cid)
        seen.append(cid)
    extra = [c for c in seen if c not in by_id]
    missing = [c for c in by_id if c not in seen]
    if extra:
        bad("C2", "claim ids outside this round's scope: %s" % ", ".join(map(str, extra)))
    if missing:
        bad("C2", "claim ids missing from the verdict: %s" % ", ".join(missing))

    # receipts of this try, as the driver wrote them (C4)
    receipts = {}

    def load_receipt(rid, claim):
        if rid in receipts:
            return receipts[rid]
        path = os.path.join(receipts_dir, "%s.json" % rid)
        rec = None
        if not rerun.ID.match(str(rid)):
            bad("C4", "receipt %r is not a receipt id" % rid, claim=claim)
        elif os.path.islink(path) or not os.path.isfile(path):
            bad("C4", "receipt %s is not in this try's receipts/" % rid, claim=claim)
        else:
            with open(path, "rb") as fh:
                data = fh.read()
            sha = rerun.sha256_bytes(data)
            try:
                rec = json.loads(data.decode("utf-8"))
            except ValueError:
                rec = None
            if not isinstance(rec, dict) or rec.get("schema") != rerun.RECEIPT_SCHEMA \
                    or rec.get("id") != rid:
                bad("C4", "receipt %s is not a well-formed receipt" % rid, claim=claim)
                rec = None
            else:
                for key in ("verification", "slot", "round", "try"):
                    if rec.get(key) != brief.get(key):
                        bad("C4", "receipt %s names %s %r, not this try's" % (rid, key, rec.get(key)),
                            claim=claim)
                # C10: a bin/rerun call of the pass, a loop's body included, asked for this id
                # and printed its answer line (in-session-runtime.md section 18.5)
                printed = [(i, answer_of(i, served)) for i in invs if i.get("rid") == rid]
                printed = [(i, ln) for i, ln in printed if ln is not None
                           and ln.startswith("rerun: receipt %s " % rid)]
                if not printed:
                    bad("C10", "receipt %s is not printed by a bin/rerun call in the stream" % rid,
                        claim=claim)
                else:
                    shown = printed[-1][1].split()[-1]
                    if shown != sha:
                        bad("C4", "receipt %s no longer hashes to the value the stream shows"
                            % rid, claim=claim)
                kept = served.get("receipts", {}).get(rid) if served else None
                if served and isinstance(served.get("receipts"), dict) and kept is None:
                    # the driver's ledger lists every receipt its serve thread wrote: a file in
                    # receipts/ that the ledger does not hold was not made by a request, whatever
                    # a printed line says (an `echo` beside a refused client call)
                    bad("C10", "receipt %s is not one the driver served in this try" % rid,
                        claim=claim)
                if kept is not None and kept != sha:
                    bad("C4", "receipt %s no longer hashes to the value the driver kept" % rid,
                        claim=claim)
                rec = dict(rec, _sha256=sha)
        receipts[rid] = rec
        return rec

    out_claims = []
    scenarios = {}
    apply_rec = None
    diff_in_packet = bool(isinstance(brief.get("engine"), dict) and brief["engine"].get("diff"))
    if diff_in_packet:
        apply_rec = load_receipt(rerun.APPLY_ID, None)
        if apply_rec is not None:
            want_argv = list(rerun.APPLY_ARGV) + ["../packet/diff.patch"]
            if apply_rec.get("kind") != "rerun" or apply_rec.get("argv") != want_argv \
                    or apply_rec.get("cwd") != "tree":
                bad("C11", "the apply receipt was not written for rerun apply --apply")
        else:
            bad("C11", "the packet holds diff.patch and no apply receipt exists")
    apply_failed = bool(apply_rec is not None and apply_rec.get("rc") not in (0,))

    for c in claims:
        if not isinstance(c, dict):
            bad("C3", "a claim entry is not an object")
            continue
        cid = c.get("id")
        spec = by_id.get(cid)
        verdict, reason = c.get("verdict"), c.get("reason")
        if verdict not in VERDICTS:
            bad("C3", "verdict must be one of %s" % ", ".join(VERDICTS), claim=cid)
        if verdict == "cannot-verify":
            if reason not in AGENT_REASONS:
                bad("C3", "reason must be one of %s" % ", ".join(AGENT_REASONS), claim=cid)
        elif reason is not None:
            bad("C3", "reason must be null unless the verdict is cannot-verify", claim=cid)
        # evidence
        ev = c.get("evidence")
        need = 0
        if spec:
            need = len(spec.get("commands") or []) + len(spec.get("pointers") or [])
        if not isinstance(ev, list) or not 1 <= len(ev) <= EVIDENCE_BASE + need:
            bad("C5", "evidence must list 1 to %d items" % (EVIDENCE_BASE + need), claim=cid)
            ev = ev if isinstance(ev, list) else []
        cited, enriched = [], []
        for item in ev:
            if not isinstance(item, dict) or (("receipt" in item and item.get("receipt") is not None)
                                              == ("read" in item and item.get("read") is not None)):
                bad("C5", "each evidence item names exactly one of receipt or read", claim=cid)
                continue
            if not _text_ok(item.get("observed"), 1, 400):
                bad("C5", "observed must be 1 to 400 characters", claim=cid)
            if item.get("receipt") is not None:
                rec = load_receipt(item["receipt"], cid)
                if rec is not None:
                    cited.append(rec)
                    enriched.append({"receipt": item["receipt"], "kind": rec.get("kind"),
                                     "pointer": "local:receipts/%s.json" % item["receipt"],
                                     "sha256": rec["_sha256"], "key": list(receipt_key(rec)),
                                     "result": list(receipt_result(rec)),
                                     "observed": item.get("observed")})
            else:
                rel = str(item.get("read")).replace("\\", "/")
                parts = rel.split("/")
                full = os.path.join(os.path.dirname(receipts_dir), *parts)
                if not _read_ok(os.path.dirname(receipts_dir), rel, parts):
                    bad("C4", "read %s names no file inside packet/, tree/ or base/" % rel,
                        claim=cid)
                else:
                    enriched.append({"receipt": None, "kind": "read", "pointer": "local:%s" % rel,
                                     "sha256": rerun.sha256_file(full), "key": ["read", rel],
                                     "result": [], "observed": item.get("observed")})
        # C5: kinds
        requires = (spec or {}).get("requires") or "any"
        kinds = [r.get("kind") for r in cited]
        if verdict == "supported":
            if requires == "rerun" and not any(k in ("rerun", "gate") for k in kinds):
                bad("C5", "a supported claim that needs a re-run cites no re-run receipt", claim=cid)
            elif requires == "rehash" and not any(k in ("rehash", "event") for k in kinds):
                bad("C5", "a supported claim that needs a re-hash cites no re-hash receipt", claim=cid)
            elif requires == "apply" and not any(r.get("id") == rerun.APPLY_ID for r in cited):
                bad("C5", "a supported claim about the diff cites no apply receipt", claim=cid)
            elif requires == "any" and not cited:
                bad("C5", "a supported claim cites no receipt", claim=cid)
            elif requires == "any" and not any(r.get("id") != rerun.APPLY_ID for r in cited):
                # `any` is left only in a claims.json written before t-296 (design 25.2): the
                # apply receipt is the diff claim's own, written before any claim is looked at,
                # so it backs no other claim by itself
                bad("C5", "a supported claim cites only the apply receipt, which is the diff "
                    "claim's own", claim=cid)
            elif requires not in ("rerun", "rehash", "apply", "any"):
                bad("C5", "a supported claim of an unbound class cannot be supported", claim=cid)
        elif verdict == "refuted" and not cited:
            bad("C5", "a refuted claim cites no receipt", claim=cid)
        # C6
        fs = c.get("failure_scenario")
        if not _text_ok(fs, 12, 400):
            bad("C6", "failure_scenario must be 12 to 400 characters", claim=cid)
        else:
            n = _norm(fs).rstrip(".")
            if spec and n == _norm(spec.get("text")).rstrip("."):
                bad("C6", "failure_scenario repeats the claim text", claim=cid)
            if n in STOCK:
                bad("C6", "failure_scenario is a stock answer", claim=cid)
            if n in scenarios:
                bad("C6", "claims %s and %s carry the same failure_scenario" % (scenarios[n], cid),
                    claim=cid)
            scenarios.setdefault(n, cid)
        # C7
        conds = c.get("conditions")
        if conds is None:
            conds = []
        if not isinstance(conds, list) or len(conds) > 5:
            bad("C7", "conditions must be a list of at most 5 items", claim=cid)
            conds = []
        if conds and verdict != "supported":
            bad("C7", "conditions are allowed only on a supported claim", claim=cid)
        for k in conds:
            if not isinstance(k, dict) or not COND_ID.match(str(k.get("id"))) \
                    or not _text_ok(k.get("check"), 8, 400) or not isinstance(k.get("ran"), bool):
                bad("C7", "a condition needs an id k<n>, a check of 8 to 400 characters and ran",
                    claim=cid)
                continue
            if k["ran"]:
                if not k.get("receipt") or not isinstance(k.get("passed"), bool):
                    bad("C7", "a condition that ran names its receipt and says passed", claim=cid)
                    continue
                rec = load_receipt(k["receipt"], cid)
                if verdict == "supported" and k.get("passed") is False:
                    bad("C7", "a supported claim holds a condition that ran and did not pass",
                        claim=cid)
            elif k.get("passed") not in (None,) or k.get("receipt") not in (None,):
                bad("C7", "a condition that did not run has no receipt and no passed value",
                    claim=cid)
        # C8
        if not _text_ok(c.get("note"), 8, 1000):
            bad("C8", "note must be 8 to 1000 characters", claim=cid)
        # C12
        if verdict == "supported" and spec:
            for entry in spec.get("commands") or []:
                if "gate" in entry:
                    ok = any(r.get("kind") == "gate" and r.get("stage") == entry["gate"]
                             and r.get("side") == entry.get("cwd") for r in cited)
                    if not ok:
                        bad("C12", "no gate receipt re-runs stage %s on %s"
                            % (entry["gate"], entry.get("cwd")), claim=cid)
                else:
                    want = ("rerun", " ".join(str(entry.get("text") or "").split()),
                            entry.get("cwd") or "tree")
                    if not any(receipt_key(r) == want for r in cited):
                        bad("C12", "no re-run receipt of a recorded command at cwd %s"
                            % (entry.get("cwd") or "tree"), claim=cid)
            for ptr in spec.get("pointers") or []:
                ok = False
                for r in cited:
                    if r.get("kind") == "rehash" and r.get("pointer") == ptr and r.get("match") is True:
                        ok = True
                    if r.get("kind") == "event" and ptr == "event:%s" % r.get("event_id") \
                            and r.get("match") is True:
                        ok = True
                if not ok:
                    bad("C12", "no matching re-hash of %s" % ptr, claim=cid)
            if any(r.get("match") is False for r in cited):
                bad("C12", "a supported claim cites a re-hash that did not match", claim=cid)
        eff = effective({"id": cid, "verdict": verdict, "reason": reason, "conditions": conds},
                        requires=requires, apply_failed=apply_failed)
        out_claims.append({"id": cid, "verdict": verdict, "effective": eff[0],
                           "reason": eff[1] if eff[0] == "cannot-verify" else None,
                           "given_reason": reason, "evidence": enriched,
                           "failure_scenario": fs, "conditions": conds, "note": c.get("note")})

    # C13: the verdict is the one the slot submitted last
    sub = None
    subs = [i for i in invs if i["args"][:2] == ["--submit", "-"]]
    if not subs:
        bad("C13", "the stream holds no bin/rerun --submit - call")
    else:
        sub = subs[-1]
        doc = sub.get("heredoc")
        if not doc or not doc.get("quoted"):
            bad("C13", "the last submission is not a heredoc with a quoted delimiter",
                call=sub["call"])
        copy_path = os.path.join(receipts_dir, "verdict.json")
        if os.path.islink(copy_path) or not os.path.isfile(copy_path):
            bad("C13", "the driver holds no copy of the submission")
        else:
            with open(copy_path, "rb") as fh:
                copy = fh.read()
            copy_text = copy.decode("utf-8", "replace")
            if doc and _strip_nl(copy_text) != _strip_nl(doc.get("body")):
                bad("C13", "the driver's copy differs from the heredoc body in the stream",
                    call=sub["call"])
            sha = rerun.sha256_bytes(copy)
            shown = answer_of(sub, served)
            if shown and shown.startswith("rerun: verdict "):
                if shown.split()[-1] != sha:
                    bad("C13", "the copy no longer hashes to the value the call printed",
                        call=sub["call"])
            elif served.get("verdict_sha256") is not None and served["verdict_sha256"] != sha:
                bad("C13", "the copy no longer hashes to the value the driver kept",
                    call=sub["call"])
            served_subs = [r for r in ledger.values() if r.get("kind") == "submit"]
            if ledger and not served_subs:
                bad("C13", "the driver never served the last submission", call=sub["call"])
            if served_subs and doc and _strip_nl(served_subs[-1].get("body")) != _strip_nl(doc.get("body")):
                bad("C13", "the served submission differs from the heredoc body in the stream",
                    call=sub["call"])

    # C14: every served request belongs to a call of the stream
    used = set()
    for n in sorted(ledger, key=lambda x: int(x)):
        req = ledger[n]
        match = None
        for k, inv in enumerate(invs):
            if k in used or inv["args"] != req.get("argv"):
                continue
            shown = answer_of(inv, served)
            last_call = inv["call"] == inv["calls"]
            # the served line is one of the lines the call printed, whichever invocation of the
            # call it was (a loop, a refusal); a call that ends the stream may show none
            if req.get("line") in (inv.get("answers") or []):
                match = k
                break
            if shown is None and last_call:
                match = k
                break
        if match is None:
            bad("C14", "served request %s has no bin/rerun call in the stream that made it" % n,
                call=None)
        else:
            used.add(match)

    if problems:
        code = problems[0]["code"]
        raise error(code, "; ".join("%s %s%s" % (p["check"], "claim %s: " % p["claim"]
                                                  if p["claim"] else "", p["detail"])
                                    for p in problems)[:2000], problems)
    effs = {c["id"]: (c["effective"], c["reason"]) for c in out_claims}
    return {"claims": out_claims, "summary": raw.get("summary"), "apply_failed": apply_failed,
            "apply_receipt": apply_rec, "slot_verdict": slot_verdict(effs),
            "conditional": any(c["reason"] == "conditional" for c in out_claims),
            "verdict_sha256": None if sub is None else rerun.sha256_file(
                os.path.join(receipts_dir, "verdict.json")),
            "receipts": {k: v["_sha256"] for k, v in receipts.items() if v}}


# --------------------------------------------------------------------------- record folds
def _rows(conn, kind, verification=None, ref=None):
    q, p = "SELECT id, ts, session, actor, ref, data FROM events WHERE kind=?", [kind]
    if ref is not None:
        q += " AND ref=?"
        p.append(ref)
    if verification is not None:
        q += " AND json_extract(data, '$.verification')=?"
        p.append(verification)
    out = []
    for r in conn.execute(q + " ORDER BY id", p):
        try:
            data = json.loads(r["data"])
        except (TypeError, ValueError):
            data = {}
        out.append({"id": r["id"], "ts": r["ts"], "session": r["session"], "actor": r["actor"],
                    "ref": r["ref"], "data": data if isinstance(data, dict) else {}})
    return out


def open_event(conn, verification):
    rows = _rows(conn, VERIFY_OPEN, verification)
    return rows[0] if rows else None


def claim_ids(conn, verification) -> list:
    ev = open_event(conn, verification)
    return list((ev or {}).get("data", {}).get("claim_ids") or [])


def round_verdicts(conn, verification, rnd) -> dict:
    """{slot: the latest research-proof-verdict of that slot in round `rnd`}."""
    out = {}
    for ev in _rows(conn, PROOF_VERDICT, verification):
        if ev["data"].get("round") == rnd:
            out[ev["data"].get("verifier")] = ev
    return out


def round_scope(conn, verification, rnd) -> list:
    """Round 0 covers every claim; round r covers round r-1's disputed claims (FR-033)."""
    if rnd == 0:
        return claim_ids(conn, verification)
    for ev in _rows(conn, VERIFY_ROUND, verification):
        if ev["data"].get("round") == rnd - 1:
            return list(ev["data"].get("disputed") or [])
    return []


def _entry_map(ev) -> dict:
    out = {}
    for c in (ev or {}).get("data", {}).get("claims") or []:
        if isinstance(c, dict):
            out[c.get("id")] = c
    return out


def fold_round(conn, verification: str, round: int) -> dict:
    """The comparison after one round (section 6.2): for each claim in the round's scope, both
    supported settles supported, both refuted settles refuted, both cannot-verify for reasons
    that are not retryable settles cannot-verify, anything else is disputed. Pure over the
    recorded verdict events. {"scope", "settled", "disputed", "verdict_events"}."""
    scope = round_scope(conn, verification, round)
    got = round_verdicts(conn, verification, round)
    a, b = _entry_map(got.get("v1")), _entry_map(got.get("v2"))
    settled, disputed = {}, []
    for cid in scope:
        x, y = a.get(cid), b.get(cid)
        if not x or not y:
            disputed.append(cid)
            continue
        ex, ey = x.get("effective"), y.get("effective")
        rx, ry = x.get("reason"), y.get("reason")
        if ex == ey == "supported":
            settled[cid] = "supported"
        elif ex == ey == "refuted":
            settled[cid] = "refuted"
        elif ex == ey == "cannot-verify" and rx not in RETRYABLE and ry not in RETRYABLE:
            settled[cid] = "cannot-verify"
        else:
            disputed.append(cid)
    return {"scope": scope, "settled": settled, "disputed": disputed,
            "verdict_events": [got[s]["id"] for s in SLOTS if s in got]}


def decide_at_cap(history: list) -> tuple:
    """("extend" | "rule", reason) at a cap round (section 6.3). `history` lists the rounds so
    far in order, each {"round", "scope", "disputed", "unrun" (a disputed claim carries an
    unrun condition on either side)}, and may carry {"extended": True} on the round an extension
    was recorded after. Extend once when the disputed set shrank in the cap round or a disputed
    claim carries an unrun condition; rule otherwise, and always at HARD_CAP."""
    rows = [h for h in history or [] if isinstance(h, dict)]
    if not rows:
        return "rule", "no round was recorded"
    last = rows[-1]
    rnd = int(last.get("round", 0))
    extended = sum(1 for h in rows if h.get("extended"))
    now = len(last.get("disputed") or [])
    if rnd >= HARD_CAP:
        return "rule", "round %d is the hard cap of %d rounds" % (rnd, HARD_CAP)
    if extended >= EXTENSIONS_MAX:
        return "rule", "the one extension is used, and %d claim(s) stay disputed" % now
    before = len(last.get("scope") or [])
    if now < before:
        return "extend", ("disputed claims fell from %d in round %d to %d in round %d"
                          % (before, rnd - 1, now, rnd))
    if last.get("unrun"):
        return "extend", "a disputed claim carries a condition that has not run"
    return "rule", ("disputed claims held at %d from round %d to round %d"
                    % (now, rnd - 1, rnd))


def _latest_counted(conn, verification, slot, cid):
    """The slot's latest counted, non-synthetic entry on claim `cid`, or None."""
    found = None
    for ev in _rows(conn, PROOF_VERDICT, verification):
        d = ev["data"]
        if d.get("verifier") != slot or d.get("synthetic"):
            continue
        entry = _entry_map(ev).get(cid)
        if entry is not None:
            found = entry
    return found


def rule_claims(conn, verification: str) -> list:
    """The ruling at the cap (section 6.3), one entry per claim still disputed after the last
    round: `refuted` when one side's latest verdict is refuted and the other side holds an
    agreeing re-run of one of the refuting receipts (same key, same result); `not-verified`
    otherwise, listing the receipts both sides cited that did not agree. A ruling never gives
    supported."""
    rounds = _rows(conn, VERIFY_ROUND, verification)
    disputed = list(rounds[-1]["data"].get("disputed") or []) if rounds else claim_ids(conn, verification)
    out = []
    for cid in disputed:
        a = _latest_counted(conn, verification, "v1", cid)
        b = _latest_counted(conn, verification, "v2", cid)
        ruled = None
        for side, other in ((a, b), (b, a)):
            if not side or side.get("effective") != "refuted" or not other:
                continue
            for r in side.get("evidence") or []:
                if not r.get("receipt"):
                    continue
                for q in other.get("evidence") or []:
                    if q.get("receipt") and q.get("key") == r.get("key") \
                            and q.get("result") == r.get("result"):
                        ruled = {"claim": cid, "ruling": "refuted",
                                 "receipts": [r.get("pointer"), q.get("pointer")],
                                 "reason": "both sides re-ran %s with the same result"
                                           % " ".join(str(x) for x in (r.get("key") or [])[:2])}
                        break
                if ruled:
                    break
            if ruled:
                break
        if ruled is None:
            # the receipts that did not agree: every receipt either side's latest counted item
            # on the claim cites (section 6.3); a side with no counted pass adds none
            cited = [r.get("pointer") for side in (a, b) if side
                     for r in side.get("evidence") or [] if r.get("receipt") and r.get("pointer")]
            if not a or not b:
                why = "a slot never made a counted pass on this claim"
            elif not cited:
                why = "no receipt was re-run by both sides"
            else:
                why = "no receipt was re-run by both sides with the same result"
            ruled = {"claim": cid, "ruling": "not-verified", "receipts": cited, "reason": why}
        out.append(ruled)
    return out


def lost_and_dropped(conn, verification) -> list:
    """The refused events of tries that are VERIFY-LOST or hold a dropped refutation."""
    out = []
    for ev in _rows(conn, VERIFY_REFUSED, verification):
        d = ev["data"]
        if d.get("slot") is None:
            continue
        if d.get("code") == "VERIFY-LOST" or d.get("refuted"):
            out.append(ev)
    return out


def outcome(conn, verification: str) -> dict:
    """The fold of the whole verification (section 6.6): refuted when any claim is settled or
    ruled refuted; else unverified when any claim is settled cannot-verify, ruled not-verified,
    still open, or the verification holds more than LOST_MAX lost tries and tries with a dropped
    refutation; else verified. {"outcome", "supported", "refuted", "unverified"}."""
    ids = claim_ids(conn, verification)
    final = {}
    for ev in _rows(conn, VERIFY_ROUND, verification):
        for cid, value in (ev["data"].get("settled") or {}).items():
            final[cid] = value
    for ev in _rows(conn, PROOF_RULING, verification):
        for r in ev["data"].get("ruling") or []:
            if isinstance(r, dict) and r.get("claim") not in final:
                final[r["claim"]] = r.get("ruling")
    supported = sorted(c for c in ids if final.get(c) == "supported")
    refuted = sorted(c for c in ids if final.get(c) == "refuted")
    unverified = sorted(c for c in ids if final.get(c) not in ("supported", "refuted"))
    over = len(lost_and_dropped(conn, verification)) > LOST_MAX
    if refuted:
        word = "refuted"
    elif unverified or over or not ids:
        word = "unverified"
    else:
        word = "verified"
    return {"outcome": word, "supported": supported, "refuted": refuted,
            "unverified": unverified}
