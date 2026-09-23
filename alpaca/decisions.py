"""Decisions: the table, the decision page, and the `why` resolution (M2.6).

Sources: spec 5.7:463-465 (decisions), the row schema `why` (spec:287), Q15 (spec:181, "a
gate skip or a level change is one decision event, no separate override ledger"), and the 5.9
page kinds (spec:513-514, the decision page).

The shape, de-signed for Alpaca:

  * `record(conn, kind, context, options, choice, consequence, pointer)` appends ONE
    hash-chained `decision` event (alpaca is the sole writer), lands the current-state row in the
    `decisions` table and the page in the `page` table, and writes the markdown page under
    `.alpaca/wiki/decisions/<id>.md`. The page is ADDRESSED BY ID, so a later correction is a new
    page (a new id) that cites the old one in its context, never an in-place edit.
  * `resolve(conn, why_pointer)` resolves a row's `why` to its decision page: it returns the
    four fields (context, options, choice, consequence) when the page exists AND carries them,
    and raises a `DecisionRefusal(BLOCKED, ...)` otherwise. An absent, empty, or dangling
    pointer never resolves.
  * `design_door_why_code(conn, op, phase)` is the design-door link (M2.6 Step 4): it folds
    the `why` of every `item` row in (op, phase) through `resolve`. Any unresolvable `why`
    BLOCKs the boundary; every row resolving is a PASS; an empty universe is a vacuous PASS
    (the door's own discharge link is what refuses an empty phase).

Owner decisions (Q, spec 5.7): intent, done_when, go, ship, level change are decisions that
carry the OWNER's verbatim journal pointer, so the record points back at what the owner
actually wrote, not a paraphrase. Recording one of these kinds without a non-empty `pointer`
is refused fail-closed. A gate skip and a level change are themselves decision events (Q15):
there is no separate override ledger, so the skip or the change lives on the board forever as
the one decision event this module appends.

HONEST LIMITS: `actor` is a self-asserted token, exactly as in the questions ledger; "owner"
here means the caller said owner, never a proof the human owner authored the pointer. Binding a
decision to a person needs an external identity in front of `record`. The page file is a
projection of the record; the `decision` event in the append-only chain is the truth.
"""
from __future__ import annotations

import os

from alpaca import db, paths, util
from alpaca.gates import verdict as vc

KIND = "decision"
PAGE_KIND = "decision"
SCHEMA_ID = "decision-page/1"

#: the pointer form a row's `why` takes: a decision-page reference addressed by id.
WHY_PREFIX = "decision:"

#: owner decisions that must carry the owner's verbatim journal pointer (spec 5.7:463-465).
OWNER_KINDS = frozenset({"intent", "done_when", "go", "ship", "level_change"})

# reason tokens -- controls bind to these exact strings.
R_KIND_EMPTY = "DECISION-KIND-EMPTY"
R_CONTEXT_EMPTY = "DECISION-CONTEXT-EMPTY"
R_OPTIONS_EMPTY = "DECISION-OPTIONS-EMPTY"
R_CHOICE_EMPTY = "DECISION-CHOICE-EMPTY"
R_CONSEQUENCE_EMPTY = "DECISION-CONSEQUENCE-EMPTY"
R_OWNER_NO_POINTER = "DECISION-OWNER-POINTER-MISSING"
R_WHY_EMPTY = "DECISION-WHY-EMPTY"
R_PAGE_ABSENT = "DECISION-PAGE-ABSENT"
R_PAGE_FILE_ABSENT = "DECISION-PAGE-FILE-ABSENT"
R_PAGE_INCOMPLETE = "DECISION-PAGE-INCOMPLETE"


class DecisionRefusal(Exception):
    """A refusal carrying its integer verdict-band code and its EXACT reason token. The verdict
    numbers are referenced from `alpaca.gates.verdict`, never restated here."""

    def __init__(self, verdict_code: int, reason: str, detail: str = ""):
        super().__init__("%s %s: %s" % (vc.name_of(verdict_code), reason, detail))
        if verdict_code not in vc.VERDICT_BAND:
            raise ValueError("a refusal needs a verdict-band code, got %r" % (verdict_code,))
        self.verdict = verdict_code
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------- ids and paths
def _decision_id(kind, context, options, choice, consequence, pointer) -> str:
    """A stable, content-derived id. Derived from the decision's own fields (never the clock),
    so a re-run is reproducible and a distinct decision (any field, the pointer included, that
    differs) is a distinct id that a correction can cite."""
    digest = util.sha256_hex(util.canonical_json({
        "kind": kind, "context": context, "options": list(options),
        "choice": choice, "consequence": consequence, "pointer": pointer}))
    return "dec-%s" % digest[:12]


def wiki_dir(root) -> str:
    return os.path.join(paths.runtime_dir(root), "wiki", "decisions")


def page_path(root, decision_id) -> str:
    return os.path.join(wiki_dir(root), "%s.md" % decision_id)


def why_pointer(dec) -> str:
    """The `why` pointer a row carries to cite a decision: `decision:<id>`."""
    return "%s%s" % (WHY_PREFIX, dec["id"] if isinstance(dec, dict) else dec)


def _extract_id(why) -> str:
    s = str(why or "").strip()
    if not s:
        return ""
    if s.startswith(WHY_PREFIX):
        return s[len(WHY_PREFIX):].strip()
    if s.startswith("local:"):
        base = os.path.basename(s[len("local:"):].strip())
        return base[:-3] if base.endswith(".md") else base
    return s


# --------------------------------------------------------------------------- the page body
def _render_page(rec) -> str:
    """The markdown decision page. Plain hyphens only (no em dash); the four fields are
    headed so a human and the resolve check both read them off the same page."""
    lines = ["# Decision %s" % rec["id"], "",
             "- kind: %s" % rec["kind"],
             "- pointer: %s" % (rec["pointer"] or "(none)"),
             "- recorded: %s" % rec["ts"], "",
             "## Context", rec["context"], "",
             "## Options"]
    for opt in rec["options"]:
        lines.append("- %s" % opt)
    lines += ["", "## Choice", rec["choice"], "",
              "## Consequence", rec["consequence"], ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- record
def _clean(value) -> str:
    return "" if value is None else str(value)


def record(conn, kind, context, options, choice, consequence, pointer="",
           *, actor="agent", session=None, root=None) -> dict:
    """Append one decision. Validates the four page fields and, for an owner kind, the verbatim
    journal pointer; then lands the event, the current-state row, the page row and the page
    file together (alpaca is the sole writer, one transaction). Returns the decision record."""
    kind = _clean(kind).strip()
    context = _clean(context).strip()
    choice = _clean(choice).strip()
    consequence = _clean(consequence).strip()
    pointer = _clean(pointer).strip()
    if not kind:
        raise DecisionRefusal(vc.BLOCKED, R_KIND_EMPTY, "a decision needs a kind")
    if not context:
        raise DecisionRefusal(vc.BLOCKED, R_CONTEXT_EMPTY, "a decision page needs a context")
    if isinstance(options, (str, bytes)) or not hasattr(options, "__iter__"):
        opts = []
    else:
        opts = [str(o).strip() for o in options if str(o).strip()]
    if not opts:
        raise DecisionRefusal(vc.BLOCKED, R_OPTIONS_EMPTY,
                              "a decision page needs at least one option")
    if not choice:
        raise DecisionRefusal(vc.BLOCKED, R_CHOICE_EMPTY, "a decision page needs a choice")
    if not consequence:
        raise DecisionRefusal(vc.BLOCKED, R_CONSEQUENCE_EMPTY,
                              "a decision page needs a consequence")
    if kind in OWNER_KINDS and not pointer:
        raise DecisionRefusal(
            vc.BLOCKED, R_OWNER_NO_POINTER,
            "an owner decision (%s) carries the owner's verbatim journal pointer; refusing a "
            "decision of an owner kind with no pointer" % kind)

    root = root or paths.root()
    did = _decision_id(kind, context, opts, choice, consequence, pointer)
    ts = util.now_iso()
    rec = {"id": did, "kind": kind, "context": context, "options": opts,
           "choice": choice, "consequence": consequence, "pointer": pointer, "ts": ts}
    page_md = _render_page(rec)

    with db.transaction(conn):
        db.append_event(conn, session=session or "cli", actor=actor or "agent",
                        kind=KIND, ref=did, data=dict(rec), conn_in_txn=True)
        conn.execute(
            "INSERT INTO decisions (ts, session, actor, kind, body, ref) VALUES (?,?,?,?,?,?)",
            (ts, session or "cli", actor or "agent", kind,
             util.canonical_json(rec), did))
        db.upsert(conn, "page", "id", {
            "id": did, "kind": PAGE_KIND, "title": "Decision %s (%s)" % (did, kind),
            "body": page_md, "pointer": pointer, "created": ts})
    util.write_text(page_path(root, did), page_md)
    return rec


# --------------------------------------------------------------------------- resolve
def _latest(conn, decision_id) -> dict | None:
    r = conn.execute(
        "SELECT body FROM decisions WHERE ref=? ORDER BY id DESC LIMIT 1",
        (decision_id,)).fetchone()
    if r is None:
        return None
    import json
    try:
        return json.loads(r["body"])
    except (TypeError, ValueError):
        return None


def resolve(conn, why_pointer, *, root=None) -> dict:
    """Resolve a row's `why` to its decision page, or refuse (BLOCKED) if it cannot.

    A page resolves only when it exists in the record AND its file is on disk AND it carries
    all four fields. An empty pointer, an unknown id, a missing file, or a page short a field
    each raise a DecisionRefusal(BLOCKED, ...) with the exact reason.
    """
    root = root or paths.root()
    did = _extract_id(why_pointer)
    if not did:
        raise DecisionRefusal(vc.BLOCKED, R_WHY_EMPTY,
                              "an empty `why` resolves to no decision page")
    rec = _latest(conn, did)
    if rec is None:
        raise DecisionRefusal(vc.BLOCKED, R_PAGE_ABSENT,
                              "no decision page addressed %r" % did)
    path = page_path(root, did)
    if not os.path.isfile(path):
        raise DecisionRefusal(vc.BLOCKED, R_PAGE_FILE_ABSENT,
                              "decision %s has no page file at %s" % (did, path))
    for field in ("context", "options", "choice", "consequence"):
        if not rec.get(field):
            raise DecisionRefusal(vc.BLOCKED, R_PAGE_INCOMPLETE,
                                  "decision %s page is missing %s" % (did, field))
    return rec


# --------------------------------------------------------------------- the design door link
def design_door_why_code(conn, op, phase) -> int:
    """Fold the `why` resolution of every `item` row in (op, phase) to one verdict (M2.6 Step 4).

    BLOCKED when any row's `why` does not resolve to a decision page carrying the four fields
    (an unfilled or dangling `why` included); PASS when every row resolves. An empty universe is
    a vacuous PASS: the design door's own discharge link is what refuses an empty phase, so this
    link does not double-signal it.
    """
    rows = [r for r in db.rows(conn, "rows", "phase=?", (phase,))
            if (op is None or r.get("op") == op) and r.get("kind") == "item"]
    for r in rows:
        try:
            resolve(conn, r.get("why"))
        except DecisionRefusal:
            return vc.BLOCKED
    return vc.PASS


# ------------------------------------------------------------------------- CLI: alpaca decide
def _cmd_decide(args):
    from alpaca import cli
    conn = db.connect(cli._root())
    sid = args.session or "cli"
    try:
        rec = record(conn, args.kind, args.context, args.option or [], args.choice,
                     args.consequence, args.pointer or "", actor=args.by, session=sid)
    except DecisionRefusal as e:
        return vc.emit_verdict("alpaca-decide", e.verdict, "%s: %s" % (e.reason, e.detail))
    return vc.emit_verdict(
        "alpaca-decide", vc.PASS,
        "%s recorded (kind %s); page at %s" % (rec["id"], rec["kind"],
                                               page_path(cli._root(), rec["id"])),
        evidence=[why_pointer(rec)])


def _parser(sub):
    d = sub.add_parser("decide", help="record a decision as a page (owner kinds carry a pointer)")
    d.add_argument("--kind", required=True,
                   help="the decision kind (owner kinds: %s)" % ", ".join(sorted(OWNER_KINDS)))
    d.add_argument("--context", required=True, help="what was being decided and why")
    d.add_argument("--option", action="append", default=[],
                   help="an option that was on the table (repeatable)")
    d.add_argument("--choice", required=True, help="the option chosen")
    d.add_argument("--consequence", required=True, help="what the choice commits to")
    d.add_argument("--pointer", default="",
                   help="the verbatim journal pointer (required for an owner decision)")
    d.add_argument("--by", default="agent", help="the decider token (self-asserted)")


def _register():
    from alpaca import cli
    cli.command("decide")(_cmd_decide)
    cli.register_parser("decisions", _parser)


_register()
