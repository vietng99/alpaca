"""The small checklist gate (M1.14): does the record discharge every required obligation?

Ported from the earlier harness gates/checklist-gate.py + gates/checklist_store.py and DE-SIGNED for
Alpaca. The earlier harness gate asserted ONE universal claim over a signed markdown store: every step the
spec declares is carried by a countersigned checklist row whose proof resolves and supports
its own row, in a store whose chain and pin agree, in a tree where something calls the gate.

Alpaca keeps the ITEM half of that claim and drops the human-signature machinery. The dropped
cases (see the commit body): the roster, capture-binding, signer-identity, countersign-
monotonicity, pre-auth-row and break-glass controls, plus the detached-MAC / countersigned-
row anchor ORIGIN authentication (Alpaca authority is tracked by supersession, not signatures),
the store pin (the events hash chain is the freeze witness), and the DUPLICATE-ROW-ID /
ROW-UNKNOWN-FIELD controls (the `rows` table primary key and fixed column set make both
structurally impossible in the store). DUPLICATE-ROW-ID stays enforced upstream by the bridge.

What `verify(conn, op, phase)` keeps and proves, over the record (alpaca/db.py):

  * the required-row UNIVERSE is derived from the spec's `step <id>: <text> [never-waive]`
    lines merged with the owner-anchored step manifest; the spec may not declare a universe
    NARROWER than the anchor, the anchor's text GOVERNS, and an EMPTY universe is BLOCKED,
    never a vacuously-true pass;
  * the item half of `check_rows`: each obligation row's statement clears the field floor and
    is row-specific, its proof resolves to a real non-empty artifact that NAMES the row, and a
    row claiming a step shares a subject-matter token with the step the spec declares;
  * coverage from the discharge fold, NOT from the row's own `status`/`tag` column: a required
    step is covered only by a row whose verdict rows fold to `discharged` (a hand-set status
    column, or a planted receipt, changes nothing). A `never-waive` step covered only by a
    waiver FAILs; a waivable step covered by a waiver PAUSES;
  * wiring: something in the tree calls this gate, or it is not wired to anything;
  * receipts keyed by a header hash over (op, phase): a run whose anchored universe SHRANK
    since a recorded receipt is BLOCKED;
  * a mid-run mutation of the record is BLOCKED; and a PASS can never sit on a LAUNDERED record
    -- the events hash chain is verified first (chain_check), and a frozen obligation row edited
    in place (its recomputed content hash no longer matches its frozen column) HALTs on drift.

Refusals surface as a verdict-band code from alpaca.gates.verdict, never a private copy.
"""
from __future__ import annotations

import os
import re
import sys
import unicodedata

from alpaca import db, paths, util
from alpaca.checklist import Halt, verdict_row
from alpaca.gates import chain_check, contract, verdict as vc

INSTRUMENT = "checklist-gate"

# ---- reason tokens a control binds to (kept subset of the earlier harness tokens) --------------
R_SPEC_NOT_SUPPLIED = "SPEC-NOT-SUPPLIED"
R_SPEC_NO_STEPS = "SPEC-DECLARES-NO-STEPS"
R_STEP_ANCHOR_ABSENT = "STEP-ANCHOR-ABSENT"
R_STEP_ANCHOR_IS_SPEC = "STEP-ANCHOR-IS-THE-SPEC"
R_STEP_UNIVERSE_NARROWED = "STEP-UNIVERSE-NARROWED"
R_STEP_TEXT_DIVERGES = "STEP-TEXT-DIVERGES-FROM-ANCHOR"
R_EMPTY_REQUIRED = "EMPTY-REQUIRED-STEP-SET"
R_EMPTY_STORE = "EMPTY-STORE-NO-WITNESS"
R_GATE_NOT_WIRED = "GATE-NOT-WIRED"
R_STEP_NOT_COVERED = "STEP-NOT-COVERED"
R_FIELD_CONTENTLESS = "ROW-FIELD-CONTENTLESS"
R_NOT_ROW_SPECIFIC = "ROW-CONTENT-NOT-ROW-SPECIFIC"
R_UNRELATED = "ROW-STATEMENT-UNRELATED-TO-SPEC"
R_PROOF_NO_SUPPORT = "PROOF-DOES-NOT-SUPPORT-ROW"
R_PROOF_EMPTY = "PROOF-EMPTY-ARTIFACT"
R_PROOF_NOT_A_FILE = "PROOF-POINTER-NOT-A-FILE"
R_PROOF_ABSENT = "PROOF-POINTER-ABSENT"
R_STORE_MUTATED = "STORE-MUTATED-DURING-EVALUATION"
R_HALT_ON_DRIFT = "HALT-ON-DRIFT"
R_CHAIN_LAUNDERED = "RECORD-CHAIN-LAUNDERED"
R_NEVER_WAIVE = "NEVER-WAIVE-STEP-WAIVED"
R_WAIVER_USED = "WAIVER-USED"
R_UNIVERSE_SHRANK = "STEP-UNIVERSE-SHRANK-SINCE-RECEIPT"

RECEIPT_KIND = "gate-receipt"

#: A spec artifact declares the step universe with lines of the form
#:     step <id>: <what the step must produce>            [never-waive]
SPEC_STEP_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?step\s+([A-Za-z0-9][A-Za-z0-9._-]{0,63})\s*:\s*(\S.*?)\s*$",
    re.IGNORECASE)
NEVER_WAIVE_MARK = "[never-waive]"

#: Generative content floor for the load-bearing statement (matches synthesis' own floor).
MIN_ALNUM = 12
MIN_WORDS = 3
SUPPORT_TOKEN_LEN = 4

# --- naming: a marker line the author fills, with a liberal whole-token mention fallback ---
_MARKER_RE = re.compile(
    r"^[ \t]*(?:[-*+>][ \t]*)?(?:checklist[-_]rows?|row[-_]ids?|rows?)[ \t]*[:=][ \t]*"
    r"(?P<ids>.+?)[ \t]*$", re.IGNORECASE | re.MULTILINE | re.UNICODE)
_MARKER_SPLIT = re.compile(r"[,;\s]+", re.UNICODE)
_ID_LEFT = r"(?<![\w-])(?<!\w\.)(?<!\w/)"
_ID_RIGHT = r"(?![\w-])(?!\.\w)(?!/\w)"
TIER_MARKER = "marker"
TIER_MENTION = "mention"


class GateResult:
    """A gate outcome: a verdict-band code, its reasons (the first names the token), and info."""

    def __init__(self, verdict, reasons, info=None):
        self.verdict = verdict
        self.reasons = list(reasons)
        self.info = info or {}

    @property
    def token(self):
        head = self.reasons[0] if self.reasons else ""
        return head.split(":", 1)[0].strip()

    def __repr__(self):
        return "GateResult(%s, %r)" % (vc.name_of(self.verdict), self.reasons)


# ---------------------------------------------------------------------- text primitives
def _normalise(value) -> str:
    text = unicodedata.normalize("NFKC", str(value))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return " ".join(text.split())


def _alnum_count(value) -> int:
    return sum(1 for ch in _normalise(value) if ch.isalnum())


def _words(value) -> list:
    return [w for w in _normalise(value).split(" ") if len(w) >= 2]


def _support_tokens(value) -> set:
    text = _normalise(value).casefold()
    return set(t for t in re.split(r"[^0-9a-z]+", text) if len(t) >= SUPPORT_TOKEN_LEN)


def marker_rows(text):
    """The set of row ids declared by the mark's marker line(s), or None when it carries none."""
    if not isinstance(text, str):
        return None
    found, saw = set(), False
    for match in _MARKER_RE.finditer(text):
        saw = True
        for tok in _MARKER_SPLIT.split(match.group("ids").strip()):
            tok = tok.strip().strip(".,;")
            if tok:
                found.add(tok)
    return found if saw else None


def name_tier(text, rid):
    """How `text` names row `rid`: TIER_MARKER (authoritative), TIER_MENTION (liberal), or None."""
    if not isinstance(text, str) or not isinstance(rid, str) or not rid:
        return None
    declared = marker_rows(text)
    if declared is not None:
        return TIER_MARKER if rid in declared else None
    if text.strip() == rid:
        return TIER_MARKER
    pat = re.compile(_ID_LEFT + re.escape(rid) + _ID_RIGHT, re.UNICODE)
    return TIER_MENTION if pat.search(text) else None


# ---------------------------------------------------------------------- the step universe
def _steps_text(spec) -> str:
    if spec is None:
        return ""
    if isinstance(spec, (list, tuple)):
        return "\n".join(str(x) for x in spec)
    return str(spec)


def _parse_steps(text, label):
    """-> (ordered {step: {text, never_waive}}, order). Empty is BLOCKED, never a pass."""
    universe, order = {}, []
    for line in text.split("\n"):
        m = SPEC_STEP_RE.match(line)
        if not m:
            continue
        step, tail = m.group(1), m.group(2)
        never = NEVER_WAIVE_MARK in tail.casefold()
        statement = tail.replace(NEVER_WAIVE_MARK, "").replace(NEVER_WAIVE_MARK.upper(), "").strip()
        if not statement:
            raise Halt(vc.BLOCKED, R_SPEC_NO_STEPS, "step %s in %s declares no work" % (step, label))
        if step in universe:
            if universe[step]["text"] != statement:
                raise Halt(vc.BLOCKED, R_SPEC_NO_STEPS,
                           "step %s is declared with two different statements in %s" % (step, label))
            continue
        universe[step] = {"text": statement, "never_waive": never}
        order.append(step)
    if not universe:
        raise Halt(vc.BLOCKED, R_SPEC_NO_STEPS,
                   "no step declaration matched in %s; an empty universe would make the gate's "
                   "universal claim vacuously true" % label)
    return dict((s, universe[s]) for s in order), order


def required_universe(spec, step_manifest):
    """The merged, anchor-governed required-step universe -> (universe, order, anchored_order).

    The spec-derived universe is producer-writable, so the owner anchor is not optional: with no
    manifest the run BLOCKS (STEP-ANCHOR-ABSENT). The spec may not declare a universe narrower
    than the anchor (STEP-UNIVERSE-NARROWED); the anchor's step text governs (STEP-TEXT-DIVERGES);
    an anchor byte-identical to the spec is self-referential (STEP-ANCHOR-IS-THE-SPEC). Anchor
    text and never-waive posture win on the anchored steps; spec-only extra steps are retained."""
    spec_text = _steps_text(spec)
    if not spec_text.strip():
        raise Halt(vc.BLOCKED, R_SPEC_NOT_SUPPLIED,
                   "the required-step universe is derived from the spec; with none supplied there "
                   "is no universe and nothing to assert")
    universe, order = _parse_steps(spec_text, "spec")
    if step_manifest is None:
        raise Halt(vc.BLOCKED, R_STEP_ANCHOR_ABSENT,
                   "no step manifest was supplied, so the universe would be derived only from the "
                   "producer-writable spec and could have been narrowed at invocation; the owner "
                   "anchor is not optional")
    manifest_text = _steps_text(step_manifest)
    if manifest_text.strip() == spec_text.strip():
        raise Halt(vc.BLOCKED, R_STEP_ANCHOR_IS_SPEC,
                   "the step manifest is byte-identical to the spec: a self-referential anchor "
                   "cannot constrain the spec it is a copy of")
    anchored, anchored_order = _parse_steps(manifest_text, "step manifest")
    missing = [s for s in anchored_order if s not in universe]
    if missing:
        raise Halt(vc.BLOCKED, R_STEP_UNIVERSE_NARROWED,
                   "the spec declares %s, NARROWER than the anchor %s: steps %s are anchored but "
                   "absent from the spec, so they would escape the gate"
                   % (order, anchored_order, missing))
    for s in anchored_order:
        if _normalise(universe[s]["text"]) != _normalise(anchored[s]["text"]):
            raise Halt(vc.BLOCKED, R_STEP_TEXT_DIVERGES,
                       "step %s carries different work text in the spec than in the owner anchor; "
                       "the anchored statement governs" % s)
    merged = dict(universe)
    for s in anchored_order:
        merged[s] = {"text": anchored[s]["text"],
                     "never_waive": anchored[s]["never_waive"] or universe[s]["never_waive"]}
    if not merged:
        raise Halt(vc.BLOCKED, R_EMPTY_REQUIRED, "the required-step set is empty")
    return dict((s, merged[s]) for s in order), order, list(anchored_order)


# ---------------------------------------------------------------------- rows / proofs
def _row_content_hash(dbrow) -> str:
    """The freeze witness for a stored obligation row, over its own load-bearing columns.
    Recomputed here so an in-place edit (which does not touch the frozen `content_hash` column)
    is caught as drift."""
    from alpaca.checklist.obligation_hash import content_hash
    return content_hash(dbrow)


def _resolve_proof(root, pointer):
    """(kind, path) for a proof pointer. `local:<relpath>` optionally carries a trailing
    `:<line>` (synthesis writes `local:<art>:<line>`), which is stripped. Returns (None, None)
    for a remote pointer, which is recorded but never resolvable to a pass here."""
    if not isinstance(pointer, str) or ":" not in pointer:
        return "malformed", None
    scheme, _, rest = pointer.partition(":")
    if scheme == "remote":
        return "remote", None
    if scheme != "local":
        return "malformed", None
    rel = rest
    tail = rel.rsplit(":", 1)
    if len(tail) == 2 and tail[1].isdigit():
        rel = tail[0]
    rel = rel.strip()
    return "local", os.path.join(root, rel.replace("/", os.sep))


def check_rows_item_half(rows, universe, root):
    """The item half of check_rows: field floor, row-specificity, proof-names-row, and a row's
    statement sharing a subject-matter token with the step it claims. Fail-fast with an exact
    token, in a fixed order, so a control binds. Raises Halt(FAIL, ...) on the first defect."""
    seen_statement, seen_proof = {}, {}
    for row in rows:
        rid = row["id"]
        stmt = row.get("statement") or ""
        if _alnum_count(stmt) < MIN_ALNUM or len(_words(stmt)) < MIN_WORDS:
            raise Halt(vc.FAIL, R_FIELD_CONTENTLESS,
                       "row %s statement normalises to %d alnum / %d word(s), below the floor of "
                       "%d / %d: %.40r" % (rid, _alnum_count(stmt), len(_words(stmt)),
                                           MIN_ALNUM, MIN_WORDS, stmt))
        skey = _normalise(stmt).casefold()
        if skey in seen_statement:
            raise Halt(vc.FAIL, R_NOT_ROW_SPECIFIC,
                       "rows %s and %s carry the same statement; a report that is not row-specific "
                       "reports on no row" % (seen_statement[skey], rid))
        seen_statement[skey] = rid
        pkey = _normalise(row.get("proof") or "").casefold()
        if pkey in seen_proof:
            raise Halt(vc.FAIL, R_NOT_ROW_SPECIFIC,
                       "rows %s and %s cite the same proof pointer" % (seen_proof[pkey], rid))
        seen_proof[pkey] = rid
        kind, target = _resolve_proof(root, row.get("proof") or "")
        if kind == "remote":
            continue                          # recorded, and never resolvable to a pass here
        if kind != "local" or target is None:
            raise Halt(vc.FAIL, R_PROOF_NO_SUPPORT,
                       "row %s carries a malformed proof pointer %r" % (rid, row.get("proof")))
        if os.path.isdir(target):
            raise Halt(vc.FAIL, R_PROOF_NOT_A_FILE,
                       "row %s proof resolves to %s, a directory that merely exists" % (rid, target))
        if not os.path.exists(target):
            raise Halt(vc.FAIL, R_PROOF_ABSENT,
                       "row %s proof %r is no longer on disk" % (rid, row.get("proof")))
        if not os.path.isfile(target):
            raise Halt(vc.FAIL, R_PROOF_NOT_A_FILE, "row %s proof %s is not a file" % (rid, target))
        with open(target, "rb") as fh:
            raw = fh.read()
        if not raw.strip():
            raise Halt(vc.FAIL, R_PROOF_EMPTY, "row %s proof %r is empty" % (rid, row.get("proof")))
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise Halt(vc.FAIL, R_PROOF_NO_SUPPORT, "row %s proof %r is not decodable"
                       % (rid, row.get("proof")))
        if name_tier(text, rid) is None:
            raise Halt(vc.FAIL, R_PROOF_NO_SUPPORT,
                       "row %s cites %r, which does not name row %s: a pointer that resolves is not "
                       "a pointer that supports (add `rows: %s` to the proof artifact)"
                       % (rid, row.get("proof"), rid, rid))
        step = str(row.get("step"))
        if step in universe:
            spec_tokens = _support_tokens(universe[step]["text"])
            if spec_tokens and not (spec_tokens & _support_tokens(stmt)):
                raise Halt(vc.FAIL, R_UNRELATED,
                           "row %s claims step %s but shares no subject-matter token with the step "
                           "the spec declares" % (rid, step))


def check_coverage(conn, rows, universe):
    """Fold coverage from the discharge rows, NOT from the stored status column. Returns
    (verdict_code, findings). BLOCKED on an empty universe or an empty store; FAIL on an
    uncovered required step or a never-waive step covered only by a waiver; PAUSED on a
    waivable step covered by a waiver; PASS when every required step is discharged."""
    findings = []
    if not universe:
        return vc.BLOCKED, [(vc.BLOCKED, R_EMPTY_REQUIRED,
                             "no required step to assert; an empty universe is never a pass")]
    if not rows:
        return vc.BLOCKED, [(vc.BLOCKED, R_EMPTY_STORE,
                             "the store carries no obligation row for this op/phase")]
    by_step = {}
    for row in rows:
        by_step.setdefault(str(row.get("step")), []).append(row)
    paused = False
    for step, meta in universe.items():
        covering = by_step.get(step, [])
        states = {r["id"]: verdict_row.status_fold(conn, r["id"]) for r in covering}
        if verdict_row.DISCHARGED in states.values():
            continue
        if verdict_row.WAIVED_STATUS in states.values():
            if meta["never_waive"]:
                findings.append((vc.FAIL, R_NEVER_WAIVE,
                                 "step %s is declared [never-waive] but is covered only by a "
                                 "waiver" % step))
            else:
                paused = True
            continue
        findings.append((vc.FAIL, R_STEP_NOT_COVERED,
                         "required step %s is covered by no discharged obligation row (a hand-set "
                         "status or a planted receipt is not a discharge)" % step))
    codes = [f[0] for f in findings]
    if codes:
        return contract.worst(codes), findings
    if paused:
        return vc.PAUSED, [(vc.PAUSED, R_WAIVER_USED,
                            "a required step is covered by a waiver; a used waiver is a pause, "
                            "never a pass")]
    return vc.PASS, []


# ---------------------------------------------------------------------- wiring / receipts
_WIRING_SKIP = {"__pycache__", ".git", "node_modules"}


def find_gate_callers(roots, token=INSTRUMENT):
    """Re-derive from CONTENT which .py files under `roots` invoke this gate. The gate's own
    module file is excluded so the check can never satisfy itself."""
    own = os.path.abspath(__file__)
    witnesses = []
    for root in roots or []:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _WIRING_SKIP]
            for name in sorted(filenames):
                if not name.endswith(".py"):
                    continue
                path = os.path.abspath(os.path.join(dirpath, name))
                if path == own:
                    continue
                try:
                    with open(path, encoding="utf-8") as fh:
                        text = fh.read()
                except (OSError, UnicodeDecodeError):
                    continue
                if token in text or "gate.verify" in text:
                    witnesses.append(path)
    return witnesses


def _header_hash(op, phase) -> str:
    return util.sha256_hex("checklist-gate/receipt/v1\n%s\n%s" % (op, phase))


def _prior_receipts(conn, header_hash):
    out = []
    for e in db.events(conn, kind=RECEIPT_KIND, limit=10 ** 9):
        if e["data"].get("header_hash") == header_hash:
            out.append(e["data"])
    return out


def _events_head(conn) -> str:
    ev = db.last_event(conn)
    return ev["hash"] if ev else chain_check.GENESIS


# ---------------------------------------------------------------------- the gate
def verify(conn, op, phase, *, spec=None, step_manifest=None, wiring_roots=None, root=None,
           now=None, write_receipt=True, mutate_hook=None) -> GateResult:
    """Adjudicate whether the record discharges every required obligation for (op, phase).

    `spec` and `step_manifest` are the step-line text (or a list of lines) the required universe
    is derived from. `wiring_roots` is the set of trees scanned for a caller of this gate;
    default is `[root]`. Returns a GateResult carrying a verdict-band code."""
    root = root or paths.root()
    info = {"gate": INSTRUMENT, "op": op, "phase": phase}
    try:
        # stage A: the record must not be laundered. A verify PASS can never sit on a broken
        # chain, so the events chain is re-derived first; a real break BLOCKS.
        chain_code, chain_reasons, _cf = chain_check.verify_chain(conn)
        if chain_code == vc.FAIL:
            raise Halt(vc.BLOCKED, R_CHAIN_LAUNDERED,
                       "the events hash chain does not re-derive (%s); no verdict can stand on a "
                       "laundered record" % (chain_reasons[0] if chain_reasons else "break"))

        head_before = _events_head(conn)

        # stage B: the universe the gate will assert over.
        universe, order, anchored_order = required_universe(spec, step_manifest)
        info["required_steps"] = order
        info["anchored_steps"] = anchored_order
        anchored = anchored_order

        # stage C: is this gate wired to anything at all?
        roots = wiring_roots if wiring_roots is not None else [root]
        callers = find_gate_callers(roots)
        info["gate_callers"] = callers
        if not callers:
            raise Halt(vc.BLOCKED, R_GATE_NOT_WIRED,
                       "nothing under %s invokes this gate; an artifact no transition executes "
                       "cannot block anything" % ", ".join(roots))

        # stage D: the obligation rows in scope.
        rows = [r for r in db.rows(conn, "rows", "phase=?", (phase,))
                if (op is None or r.get("op") == op) and r.get("kind") == "item"]
        info["rows_in_scope"] = len(rows)

        # a frozen row edited in place is drift: its recomputed content hash no longer matches
        # its frozen column. A PASS can never sit on drifted content.
        for row in rows:
            if row.get("content_hash") and _row_content_hash(row) != row["content_hash"]:
                raise Halt(vc.FAIL, R_HALT_ON_DRIFT,
                           "row %s was edited in place after it was frozen; a correction is a NEW "
                           "row citing supersedes, never an edit of the frozen original" % row["id"])

        # stage E: the item half of check_rows, then coverage from the discharge fold.
        check_rows_item_half(rows, universe, root)

        # a mid-run mutation of the record: the caller's hook models a concurrent write between
        # the gate's read and its verdict.
        if mutate_hook is not None:
            mutate_hook()
        if _events_head(conn) != head_before:
            raise Halt(vc.BLOCKED, R_STORE_MUTATED,
                       "the record changed between the gate's read and its verdict; nothing read "
                       "before that change can be reported on")

        cov_code, cov_findings = check_coverage(conn, rows, universe)

        # stage F: receipts keyed by the (op, phase) header hash. A universe that SHRANK since a
        # recorded receipt is BLOCKED.
        header_hash = _header_hash(op, phase)
        for prior in _prior_receipts(conn, header_hash):
            prior_anchor = set(prior.get("anchored_steps") or [])
            if not prior_anchor.issubset(set(anchored)):
                raise Halt(vc.BLOCKED, R_UNIVERSE_SHRANK,
                           "a receipt recorded anchored steps %s; this run anchors only %s -- the "
                           "owner universe shrank between runs"
                           % (sorted(prior_anchor), anchored))

        if cov_code == vc.PASS:
            result = GateResult(vc.PASS, ["OK"], info)
        else:
            result = GateResult(cov_code, ["%s: %s" % (c, d) for _v, c, d in cov_findings], info)

    except Halt as h:
        result = GateResult(h.verdict, ["%s: %s" % (h.code, h.detail)], info)

    # write a receipt into the record (best-effort; alpaca is the sole writer via append_event).
    if write_receipt:
        try:
            db.append_event(conn, session="instrument", actor=INSTRUMENT, kind=RECEIPT_KIND,
                            op=op, ref=phase,
                            data={"header_hash": _header_hash(op, phase),
                                  "anchored_steps": info.get("anchored_steps", []),
                                  "verdict": vc.name_of(result.verdict),
                                  "reasons": result.reasons})
        except Exception:
            pass
    return result


# ---------------------------------------------------------------------- selftest
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
R_SCRATCH_INSIDE = "SELFTEST-SCRATCH-INSIDE-DELIVERABLE"
R_TABLE_TOO_THIN = "CONTROL-TABLE-TOO-THIN"
#: A control table thinner than this cannot be evidence: a too-thin selftest is BLOCKED,
#: never a pass. This is the checklist_store self-refusal, kept.
CONTROL_TABLE_FLOOR = 12


def refuse_thin_table(control_count, floor=CONTROL_TABLE_FLOOR):
    """A control table thinner than the floor cannot be evidence: it is BLOCKED, never a pass.
    This is the checklist_store self-refusal, kept."""
    if control_count < floor:
        return vc.BLOCKED, R_TABLE_TOO_THIN
    return vc.PASS, "OK"


def refuse_scratch_inside_tree(base, tree_root=REPO_ROOT):
    """A selftest may not write its scratch INSIDE the shipped tree. A base that resolves inside
    `tree_root` FAILs with SELFTEST-SCRATCH-INSIDE-DELIVERABLE before a byte is written. This is
    the checklist-gate self-refusal, kept."""
    base_real = os.path.normcase(os.path.realpath(os.path.abspath(base)))
    tree_real = os.path.normcase(os.path.realpath(os.path.abspath(tree_root)))
    if base_real == tree_real or base_real.startswith(tree_real + os.sep):
        return vc.FAIL, R_SCRATCH_INSIDE
    return vc.PASS, "OK"


SPEC_TEXT = ("step s1: intake artifacts reviewed against this plan\n"
             "step s2: model authored and compiled from this plan [never-waive]\n"
             "step s3: bench measurement recorded on the rig\n")


def _anchorize(spec_text):
    """A manifest with the SAME step universe as `spec_text` but a distinct preamble, so it is
    not byte-identical to the spec it constrains (STEP-ANCHOR-IS-THE-SPEC never fires legit)."""
    steps = [ln for ln in spec_text.split("\n") if SPEC_STEP_RE.match(ln)]
    return "# owner-anchored step manifest (out of band)\n" + "\n".join(steps) + "\n"


_DEFAULT_ROWS = (
    ("cover-plan", "s1", "intake artifacts reviewed against this plan", True),
    ("row-model", "s2", "model authored and compiled from this plan", True),
    ("row-bench", "s3", "bench measurement recorded on the rig", True),
)


def _mkrow(conn, root, rid, step, statement, *, phase="build", op="op-sel", proof_names=True,
           discharge=True, waive=False):
    proof_rel = "proof-%s.txt" % rid
    body = "artifact for the work reported\n"
    if proof_names:
        body += "rows: %s\n" % rid
    util.write_text(os.path.join(root, proof_rel), body)
    row = {"id": rid, "kind": "item", "op": op, "phase": phase, "step": step,
           "statement": statement, "proof": "local:%s" % proof_rel, "where_": "", "how": "",
           "when_": "", "why": "", "status": "open", "tag": "Specced", "supersedes": None}
    row["content_hash"] = _row_content_hash(row)
    row["prev_hash"] = chain_check.GENESIS
    db.upsert(conn, "rows", "id", row)
    if waive:
        verdict_row.waive(conn, rid, row["content_hash"], reason="deferred", level="L2",
                          session="s1")
    elif discharge:
        verdict_row.discharge(conn, rid, row["content_hash"], "probe", vc.PASS,
                              ["local:%s" % proof_rel], "L2", "s1")
    return row


def _wiring(root):
    wdir = os.path.join(root, "wiring")
    util.write_text(os.path.join(wdir, "transition.py"), "from alpaca.checklist import gate\n"
                    "gate.verify\n")
    return [wdir]


def selftest(workdir=None) -> int:
    """Run the gate's controls against throwaway records. Every control seeds the exact shape it
    binds to; a control table thinner than the floor is BLOCKED (not a pass), and a scratch dir
    inside the shipped tree FAILs before a byte is written (the two self-refusals)."""
    base = workdir or __import__("tempfile").mkdtemp(prefix="checklist-gate-selftest-")
    scratch_v, scratch_r = refuse_scratch_inside_tree(base)
    if scratch_v != vc.PASS:
        print("%s: refusing to write scratch %s inside the shipped tree %s"
              % (scratch_r, base, REPO_ROOT))
        print(vc.gate_line("%s-selftest" % INSTRUMENT, vc.FAIL))
        return vc.SELFTEST
    if not os.path.isdir(base):
        os.makedirs(base)
    rows = []

    def _c(cid, desc, ok, detail=""):
        rows.append((cid, desc, bool(ok), detail))

    def _fresh(stem):
        d = os.path.join(base, stem)
        n = 2
        while os.path.exists(d):
            d = os.path.join(base, "%s.%d" % (stem, n))
            n += 1
        os.makedirs(d)
        conn = db.connect(d)
        return d, conn

    def _valid(stem, rowspec=_DEFAULT_ROWS, **over):
        d, conn = _fresh(stem)
        for rid, step, stmt, disc in rowspec:
            _mkrow(conn, d, rid, step, stmt, discharge=disc)
        kw = dict(spec=SPEC_TEXT, step_manifest=_anchorize(SPEC_TEXT), wiring_roots=_wiring(d),
                  root=d, write_receipt=False)
        kw.update(over)
        return d, conn, kw

    # POS-01: a complete, discharged deployment PASSES.
    _d, conn, kw = _valid("pristine")
    _c("POS-01", "a complete, discharged deployment PASSES", verify(conn, "op-sel", "build",
       **kw).verdict == vc.PASS)

    # universe refusals (pure).
    try:
        required_universe(None, "x")
        _c("G-SPEC-0", "no spec is BLOCKED", False)
    except Halt as h:
        _c("G-SPEC-0", "no spec is BLOCKED", h.verdict == vc.BLOCKED and h.code == R_SPEC_NOT_SUPPLIED)
    try:
        required_universe("# no steps here\n", "x")
        _c("G-04c", "a spec with no step lines is BLOCKED", False)
    except Halt as h:
        _c("G-04c", "a spec with no step lines is BLOCKED", h.code == R_SPEC_NO_STEPS)
    try:
        required_universe(SPEC_TEXT, None)
        _c("GA-0", "a missing step manifest is BLOCKED", False)
    except Halt as h:
        _c("GA-0", "a missing step manifest is BLOCKED", h.code == R_STEP_ANCHOR_ABSENT)
    try:
        required_universe(SPEC_TEXT, SPEC_TEXT)
        _c("GA-6", "a manifest byte-identical to the spec is BLOCKED", False)
    except Halt as h:
        _c("GA-6", "a manifest byte-identical to the spec is BLOCKED", h.code == R_STEP_ANCHOR_IS_SPEC)
    try:
        required_universe("step s1: intake artifacts reviewed against this plan\n",
                          _anchorize(SPEC_TEXT))
        _c("GA-NARROW", "a spec narrower than the anchor is BLOCKED", False)
    except Halt as h:
        _c("GA-NARROW", "a spec narrower than the anchor is BLOCKED", h.code == R_STEP_UNIVERSE_NARROWED)
    try:
        hollow = SPEC_TEXT.replace("model authored and compiled from this plan", "did the thing")
        required_universe(hollow, _anchorize(SPEC_TEXT))
        _c("GA-7", "a hollowed spec statement diverging from the anchor is BLOCKED", False)
    except Halt as h:
        _c("GA-7", "a hollowed spec statement diverging from the anchor is BLOCKED",
           h.code == R_STEP_TEXT_DIVERGES)

    # empty required-row universe is BLOCKED (the done-when / DLG-01).
    _d, conn = _fresh("empty-universe")
    code, findings = check_coverage(conn, [{"id": "x", "step": "s1"}], {})
    _c("DLG-01", "an empty required-step set is BLOCKED",
       code == vc.BLOCKED and findings[0][1] == R_EMPTY_REQUIRED)

    # wiring: nothing calls the gate.
    _d, conn, kw = _valid("no-caller", wiring_roots=[os.path.join(base, "empty-wiring")])
    os.makedirs(kw["wiring_roots"][0], exist_ok=True)
    _c("G-01", "nothing invokes the gate -> GATE-NOT-WIRED",
       verify(conn, "op-sel", "build", **kw).token == R_GATE_NOT_WIRED)

    # empty store.
    _d, conn, kw = _valid("empty-store", rowspec=())
    _c("G-04b", "no obligation row in scope -> EMPTY-STORE-NO-WITNESS",
       verify(conn, "op-sel", "build", **kw).token == R_EMPTY_STORE)

    # a required step with no discharged row (a hand-set status changes nothing).
    _d, conn = _fresh("step-not-covered")
    _mkrow(conn, _d, "cover-plan", "s1", "intake artifacts reviewed against this plan")
    r = verify(conn, "op-sel", "build", spec=SPEC_TEXT, step_manifest=_anchorize(SPEC_TEXT),
               wiring_roots=_wiring(_d), root=_d, write_receipt=False)
    _c("G-04", "a step with no discharged row -> STEP-NOT-COVERED",
       r.verdict == vc.FAIL and r.token == R_STEP_NOT_COVERED)

    # field floor: a null-ish statement.
    _d, conn = _fresh("nullish")
    _mkrow(conn, _d, "cover-plan", "s1", "n/a\u200b")
    try:
        check_rows_item_half(db.rows(conn, "rows", "1=1"), {"s1": {"text": "intake", "never_waive": False}}, _d)
        _c("G-06", "a null-ish statement -> ROW-FIELD-CONTENTLESS", False)
    except Halt as h:
        _c("G-06", "a null-ish statement -> ROW-FIELD-CONTENTLESS", h.code == R_FIELD_CONTENTLESS)

    # proof does not name the row.
    _d, conn = _fresh("decoy")
    _mkrow(conn, _d, "cover-plan", "s1", "intake artifacts reviewed against this plan",
           proof_names=False)
    try:
        check_rows_item_half(db.rows(conn, "rows", "1=1"),
                             {"s1": {"text": "intake artifacts reviewed", "never_waive": False}}, _d)
        _c("G-07", "a proof that never names its row -> PROOF-DOES-NOT-SUPPORT-ROW", False)
    except Halt as h:
        _c("G-07", "a proof that never names its row -> PROOF-DOES-NOT-SUPPORT-ROW",
           h.code == R_PROOF_NO_SUPPORT)

    # drift: a discharged row edited in place.
    _d, conn = _fresh("drift")
    row = _mkrow(conn, _d, "cover-plan", "s1", "intake artifacts reviewed against this plan")
    conn.execute("UPDATE rows SET statement='a later edited statement here now' WHERE id=?",
                 (row["id"],))
    conn.commit()
    r = verify(conn, "op-sel", "build",
               spec="step s1: intake artifacts reviewed against this plan\n",
               step_manifest=_anchorize("step s1: intake artifacts reviewed against this plan\n"),
               wiring_roots=_wiring(_d), root=_d, write_receipt=False)
    _c("G-17", "a discharged row edited in place -> HALT-ON-DRIFT", r.token == R_HALT_ON_DRIFT)

    # a never-waive step covered only by a waiver.
    _d, conn = _fresh("never-waive")
    _mkrow(conn, _d, "cover-plan", "s1", "intake artifacts reviewed against this plan")
    _mkrow(conn, _d, "row-model", "s2", "model authored and compiled from this plan", waive=True)
    _mkrow(conn, _d, "row-bench", "s3", "bench measurement recorded on the rig")
    r = verify(conn, "op-sel", "build", spec=SPEC_TEXT, step_manifest=_anchorize(SPEC_TEXT),
               wiring_roots=_wiring(_d), root=_d, write_receipt=False)
    _c("G-27", "a [never-waive] step covered only by a waiver -> NEVER-WAIVE-STEP-WAIVED",
       r.verdict == vc.FAIL and r.token == R_NEVER_WAIVE)

    # a waivable step covered by a waiver PAUSES.
    _d, conn = _fresh("waiver-used")
    _mkrow(conn, _d, "cover-plan", "s1", "intake artifacts reviewed against this plan")
    _mkrow(conn, _d, "row-model", "s2", "model authored and compiled from this plan")
    _mkrow(conn, _d, "row-bench", "s3", "bench measurement recorded on the rig", waive=True)
    r = verify(conn, "op-sel", "build", spec=SPEC_TEXT.replace(" [never-waive]", ""),
               step_manifest=_anchorize(SPEC_TEXT.replace(" [never-waive]", "")),
               wiring_roots=_wiring(_d), root=_d, write_receipt=False)
    _c("G-28", "a waivable step covered by a waiver -> PAUSED",
       r.verdict == vc.PAUSED and r.token == R_WAIVER_USED)

    # mid-run mutation.
    _d, conn, kw = _valid("mutated")

    def _mutate():
        db.append_event(conn, session="x", actor="ghost", kind="beat", ref="injected", data={})
    r = verify(conn, "op-sel", "build", mutate_hook=_mutate, **kw)
    _c("G-16", "the record mutated mid-run -> STORE-MUTATED-DURING-EVALUATION",
       r.token == R_STORE_MUTATED)

    # a laundered record: the events chain broken -> a PASS cannot stand.
    _d, conn, kw = _valid("laundered")
    conn.execute("UPDATE events SET actor='TAMPER' WHERE id=(SELECT id FROM events ORDER BY id "
                 "LIMIT 1 OFFSET 1)")
    conn.commit()
    _c("LAUNDERED", "a verify PASS cannot sit on a laundered record -> RECORD-CHAIN-LAUNDERED",
       verify(conn, "op-sel", "build", **kw).token == R_CHAIN_LAUNDERED)

    failures = [cid for cid, _d2, ok, _x in rows if not ok]
    print("CONTROL TABLE -- %s/1 (every row from an executed run)" % INSTRUMENT)
    for cid, desc, ok, _detail in rows:
        print("  %-12s %-12s %s" % (cid, "FIRED" if ok else "DID-NOT-FIRE", desc))
    # a control table too thin to be evidence is BLOCKED, never a pass (the second self-refusal).
    if refuse_thin_table(len(rows))[0] != vc.PASS:
        print("  %s: %d control(s) is below the floor of %d; a too-thin selftest is BLOCKED"
              % (R_TABLE_TOO_THIN, len(rows), CONTROL_TABLE_FLOOR))
        print("  %d control(s), %d did not fire" % (len(rows), len(failures)))
        print(vc.gate_line("%s-selftest" % INSTRUMENT, vc.BLOCKED))
        return vc.SELFTEST
    print("  %d control(s), %d did not fire" % (len(rows), len(failures)))
    code = vc.FAIL if failures else vc.PASS
    print(vc.gate_line("%s-selftest" % INSTRUMENT, code))
    return vc.SELFTEST if failures else vc.PASS


def main(argv=None) -> int:
    ap = vc.make_parser(name=INSTRUMENT,
                        description="the small checklist gate: does the record discharge every "
                                    "required obligation for an op/phase?")
    ap.add_argument("--selftest", action="store_true", help="run the gate's own controls")
    ap.add_argument("--workdir", default=None, help="selftest scratch (fresh; never deleted)")
    ap.add_argument("--op")
    ap.add_argument("--phase")
    ap.add_argument("--spec", help="the spec artifact the step universe derives from")
    ap.add_argument("--step-manifest", help="the owner-anchored step manifest")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest(a.workdir)
    missing = [f for f, v in (("--op", a.op), ("--phase", a.phase), ("--spec", a.spec),
                              ("--step-manifest", a.step_manifest)) if not v]
    if missing:
        ap.error("these are not optional: %s" % " ".join(missing))
    conn = db.connect(paths.root())
    try:
        spec = util.read_text(a.spec)
        manifest = util.read_text(a.step_manifest)
        result = verify(conn, a.op, a.phase, spec=spec, step_manifest=manifest)
    finally:
        conn.close()
    return vc.emit_verdict(INSTRUMENT, result.verdict, result.reasons[0] if result.reasons else "")


if __name__ == "__main__":
    sys.exit(main())
