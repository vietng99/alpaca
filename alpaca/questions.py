"""The questions ledger and the HITL decision gate (M1.18).

Ported from the earlier harness `gates/autonomy.py` (the bank-and-continue autonomy model, s3/s4/s5)
and de-signed for Alpaca: the append-only `questions.jsonl` becomes the Alpaca record itself. Every
bank and every answer is a hash-chained `question` event in the `events` table (alpaca is the sole
writer; `db.transaction` + `db.append_event(..., conn_in_txn=True)` land the event). The
current state of a question is the LAST event carrying its id, exactly as the jsonl fold was
last-record-wins. A bank ALWAYS lands `status=open, decided_by=""`, so a decision can never be
laundered in through `bank`.

The model this enforces (autonomy-model.md s3):

  * research                          -> the harness may research-and-decide (full autodrive
                                         only; a lower level raises more and decides less, so it
                                         banks). This is the class ROUTED TO THE HARNESS.
  * owner-only | flow-break |         -> OWNER-ONLY. The machine banks them and waits, EVEN at
    fundamental                          the full autodrive level; it may never self-decide them.
  * never full-halt                   -> a banked (research-decidable) question that stops the
                                         whole run is the forbidden pattern; independent work
                                         must keep flowing (bank-and-continue).
  * a banked owner-owed question that is silently dropped (never resolved, never surfaced)
                                      -> a phase may not sign out while it is open.

The class table is DATA: the routing of each class name is read from
`project.yaml` under the additive key `question_classes` (a map of class-name -> "owner" |
"harness"). When that key is absent the table below is the default, and the routing is NOT
hardcoded past that default -- a project that declares the key overrides it. (project.yaml did
not yet carry the key when this landed; see the module note and the task summary.)

HONEST LIMITS (carried from the port, do not soften): this ledger is COORDINATION + TRACING,
ADVISORY. `decided_by` is a SELF-ASSERTED token: "owner" here means "the caller said owner",
never a proof that the human owner authored the decision. Binding a decision to a person needs
an external identity that plugs in front of `answer()`. Append-only is enforced by the record's
hash chain, not claimed as non-forgeability. "Surfaced" (the pad / a review card) is the
caller's job; this module guarantees only that a phase cannot CLOSE, and a dependent boundary
cannot advance below the threshold, while an owner-owed question is still open.
"""
from __future__ import annotations

from collections import OrderedDict

from alpaca import db, paths, project, util
from alpaca.gates import verdict as vc
from alpaca.phase import defaults

KIND = "question"
SCHEMA_ID = "questions-ledger/1"

# --- the routes a class maps to (autonomy-model.md s3). DATA, defaulted here, read from yaml. -
ROUTE_OWNER = "owner"       # owner-only: banked and owner-decided, never machine-decided.
ROUTE_HARNESS = "harness"   # research: the machine may research-and-decide at full autodrive.
ROUTES = frozenset({ROUTE_OWNER, ROUTE_HARNESS})

#: The default class table (M1.18 Step 3): the four classes the plan names and their routes.
#: research routes to the harness; the three owner classes route to the owner. A project
#: overrides this by declaring `question_classes` in project.yaml; absence falls back here.
#: `stuck` (M3.4) is additive and owner-owed: a bounded loop that reaches its retry bound banks a
#: `stuck` question, and a stuck thread is a human decision, never machine-decided even at full
#: autodrive. A project still overrides the whole table via `question_classes`.
DEFAULT_CLASS_TABLE = OrderedDict((
    ("research", ROUTE_HARNESS),
    ("owner-only", ROUTE_OWNER),
    ("flow-break", ROUTE_OWNER),
    ("fundamental", ROUTE_OWNER),
    ("stuck", ROUTE_OWNER),
))
PROJECT_KEY = "question_classes"

#: the deciders an answer may name. "owner" is the human; "harness" is the machine (research).
DECIDERS = frozenset({"owner", ROUTE_HARNESS})

#: the full-autodrive level at which the harness may research-and-decide a research question.
L_RESEARCH_DECIDE = 6

#: a door hitting an owner-only OPEN question owed in its phase PAUSES below this level; at or
#: above it the door does not pause on the banked owner question (bank-and-continue). M1.18
#: Step 4: "a door that hits an owner-only open question at a level below L4 returns PAUSED".
OWNER_QUESTION_PAUSE_BELOW = 4

# reason tokens -- controls bind to these exact strings.
R_CLASS_UNKNOWN = "QUESTIONS-CLASS-UNKNOWN"
R_TEXT_EMPTY = "QUESTIONS-TEXT-EMPTY"
R_ID_DUPLICATE = "QUESTIONS-ID-DUPLICATE"
R_NOT_FOUND = "QUESTIONS-NOT-FOUND"
R_ALREADY_RESOLVED = "QUESTIONS-ALREADY-RESOLVED"
R_DECIDER_UNKNOWN = "QUESTIONS-DECIDER-UNKNOWN"
R_ANSWER_EMPTY = "QUESTIONS-ANSWER-EMPTY"
R_OWNER_SELF_DECIDED = "QUESTIONS-OWNER-CLASS-SELF-DECIDED"
R_HALTED_DECIDABLE = "QUESTIONS-HALTED-DECIDABLE-FORBIDDEN"
R_BANKED_DROPPED = "QUESTIONS-BANKED-DROPPED"


class QuestionRefusal(Exception):
    """A refusal carrying its integer verdict-band code and its EXACT reason token.

    The verdict numbers are referenced from `alpaca.gates.verdict`, never restated here. Controls
    bind to `.verdict` and `.reason`; `.detail` is the human pointer."""

    def __init__(self, verdict_code: int, reason: str, detail: str = ""):
        super().__init__("%s %s: %s" % (vc.name_of(verdict_code), reason, detail))
        if verdict_code not in vc.VERDICT_BAND:
            raise ValueError("a refusal needs a verdict-band code, got %r" % (verdict_code,))
        self.verdict = verdict_code
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------- the class table
def class_table(conn, root=None) -> "OrderedDict[str, str]":
    """The class-name -> route map. Read from `project.yaml` (`question_classes`) when present
    and well-formed; otherwise the DEFAULT_CLASS_TABLE. A malformed or absent key falls back to
    the default rather than crashing a caller, so the ledger works before the key is added."""
    try:
        root = root or paths.root()
        cfg = project.load(root)
        raw = cfg.get(PROJECT_KEY)
    except Exception:
        raw = None
    if isinstance(raw, dict) and raw:
        out = OrderedDict()
        for name, route in raw.items():
            r = str(route)
            out[str(name)] = r if r in ROUTES else ROUTE_OWNER   # unknown route -> owner-safe
        return out
    return OrderedDict(DEFAULT_CLASS_TABLE)


def route(conn, cls, root=None) -> str:
    """The route of a class, or a fail-closed refusal for a class the table does not name."""
    tbl = class_table(conn, root=root)
    if cls not in tbl:
        raise QuestionRefusal(vc.BLOCKED, R_CLASS_UNKNOWN,
                              "%r not in the class table %s" % (cls, sorted(tbl)))
    return tbl[cls]


# --------------------------------------------------------------------- record read / fold
def _events(conn) -> list:
    """Every `question` event in append order (oldest first), data already decoded."""
    return db.events(conn, kind=KIND, limit=10 ** 9)


def _state(conn) -> "OrderedDict[str, dict]":
    """Fold the question events to current state: {id -> latest record}, last-appended wins.

    A missing / empty ledger is an EMPTY state, never an error: a run that has banked no
    question yet is legitimate, distinct from a verification claim over an empty population."""
    st = OrderedDict()
    for e in _events(conn):
        rec = dict(e["data"])
        qid = rec.get("id")
        if qid is None:
            continue
        st[qid] = rec
    return st


def _next_id(state) -> str:
    return "q-%03d" % (len(state) + 1)


def _append(conn, rec, *, actor, session) -> dict:
    """Append one hash-chained `question` event inside its own transaction. alpaca is the sole
    writer; the event and nothing else land together (there is no separate current-state row
    to keep in step -- the fold over the events IS the current state)."""
    with db.transaction(conn):
        db.append_event(conn, session=session or "instrument", actor=actor or "instrument",
                        kind=KIND, op=rec.get("op"), ref=rec["id"], data=dict(rec),
                        conn_in_txn=True)
    return rec


# --------------------------------------------------------------------- s4 API: bank / open / answer
def bank(conn, text, cls, *, phase="", blocks=None, op=None, qid=None,
         actor="agent", session=None, raised_at=None, **ignored) -> dict:
    """Append a banked question. A bank ALWAYS lands `status=open, decided_by=""`: any `status`
    or `decided_by` passed in is ignored (`**ignored`), so a decision cannot be laundered in.

    `text` is the question; `cls` is its class (must be in the class table); `phase` scopes it
    (the door over that phase is the one it can pause); `blocks` are the work/row ids it blocks
    (bank-and-continue uses them to partition runnable from waiting). Returns the banked record.
    """
    text = "" if text is None else str(text)
    if not text.strip():
        raise QuestionRefusal(vc.BLOCKED, R_TEXT_EMPTY, "a banked question needs a non-empty text")
    rt = route(conn, cls)                                  # refuses an unknown class, fail-closed
    if not isinstance(blocks, (list, tuple, set)) and blocks is not None:
        raise QuestionRefusal(vc.BLOCKED, "QUESTIONS-BLOCKS-NOT-LIST", type(blocks).__name__)
    blocks = [str(b) for b in (blocks or [])]
    state = _state(conn)
    qid = str(qid) if qid else _next_id(state)
    if qid in state:
        raise QuestionRefusal(vc.BLOCKED, R_ID_DUPLICATE, qid)
    rec = {
        "id": qid,
        "text": text,
        "class": cls,
        "route": rt,
        "phase": str(phase or ""),
        "blocks": blocks,
        "op": op,
        "status": "open",           # a bank is ALWAYS open ...
        "answer": "",
        "decided_by": "",           # ... and ALWAYS undecided.
        "raised_at": raised_at or util.now_iso(),
    }
    return _append(conn, rec, actor=actor, session=session)


def open(conn, cls=None) -> list:
    """The open questions, optionally filtered to one class. (The name is the M1.18 interface;
    this module reads and writes only through `alpaca.db`, never the builtin `open`.)"""
    out = [r for r in _state(conn).values() if r.get("status") == "open"]
    if cls is not None:
        out = [r for r in out if r.get("class") == cls]
    return out


def answer(conn, qid, text, actor, *, session=None) -> dict:
    """Append the resolution of a banked question.

    The owner-only guard lives HERE (autonomy s5.2): a question whose class ROUTES to the owner
    may be resolved ONLY by `actor="owner"`. A machine self-decision (`actor="harness"`) of such
    a question is REFUSED (FAIL) -- those classes are owner-only even at the full autodrive
    level. A resolved question is not re-resolved; a correction is a NEW banked question.
    """
    if actor not in DECIDERS:
        raise QuestionRefusal(vc.BLOCKED, R_DECIDER_UNKNOWN,
                              "%r not in %s" % (actor, sorted(DECIDERS)))
    rec = _state(conn).get(str(qid))
    if rec is None:
        raise QuestionRefusal(vc.BLOCKED, R_NOT_FOUND, "no banked question with id=%r" % qid)
    if rec.get("status") == "resolved":
        raise QuestionRefusal(vc.BLOCKED, R_ALREADY_RESOLVED,
                              "%s is resolved; a correction is a NEW banked question" % qid)
    if rec.get("route") == ROUTE_OWNER and actor != "owner":
        raise QuestionRefusal(
            vc.FAIL, R_OWNER_SELF_DECIDED,
            "question %s is class %r (owner-only, even at full autodrive); it may not be decided "
            "by %r -- bank it and await the owner" % (qid, rec.get("class"), actor))
    if text is None or not str(text).strip():
        raise QuestionRefusal(vc.BLOCKED, R_ANSWER_EMPTY,
                              "%s: a resolution must carry a non-empty answer" % qid)
    new = dict(rec)
    new.update(status="resolved", answer=str(text), decided_by=actor,
               resolved_at=util.now_iso())
    return _append(conn, new, actor=actor, session=session)


def decide_or_bank(conn, text, cls, level, *, phase="", blocks=None, op=None,
                   answer_text=None, session=None) -> dict:
    """The level-calibrated router (autonomy s1/s3).

      * owner-class question -> ALWAYS banked (owner-only; never self-decided, even at full
        autodrive).
      * research + level >= L_RESEARCH_DECIDE -> banked, then resolved by the harness, the
        decision documented in `answer`.
      * research + lower level -> banked (raise more, decide less).
    Returns the resulting (latest) record.
    """
    rt = route(conn, cls)                                 # refuses an unknown class
    rec = bank(conn, text, cls, phase=phase, blocks=blocks, op=op, session=session)
    if rt == ROUTE_OWNER:
        return rec                                        # owner-only: bank, never decide
    if defaults.level_num(level) >= L_RESEARCH_DECIDE:
        ans = answer_text or "harness research decision (documented in the record)"
        return answer(conn, rec["id"], ans, ROUTE_HARNESS, session=session)
    return rec                                            # lower level: bank


# --------------------------------------------------------------------- s4/s5 helpers
def open_owner_owed(conn, phase=None) -> list:
    """The OPEN owner-owed questions (classes that route to the owner), optionally scoped to a
    phase. These must reach the owner and may not be silently dropped; scoping by phase is how a
    banked owner question blocks ONLY the boundary that depends on it."""
    out = []
    for r in _state(conn).values():
        if r.get("status") != "open":
            continue
        if r.get("route") != ROUTE_OWNER:
            continue
        if phase is not None and (r.get("phase") or "") != phase:
            continue
        out.append(r)
    return out


def owner_pause_cards(conn, phase=None):
    """M3.5 bridge: the (subject, reason) for one owner-only pause review card per OPEN owner-owed
    question, optionally scoped to a phase. An open owner-owed question is a human decision owed
    (autonomy s5), which is exactly the owner-only-pause card class; `alpaca.review.sync_owner_pauses`
    consumes this. The subject is the question id so the review dedupe keeps it one card each. This
    is additive: questions owns the fold, review owns the card.
    """
    out = []
    for r in open_owner_owed(conn, phase):
        out.append({
            "subject": "question:%s" % r.get("id"),
            "reason": "owner-only question %s (%s) is open: %s"
                      % (r.get("id"), r.get("class"), r.get("text")),
            "op": r.get("op"),
        })
    return out


def independent_work(conn, all_work):
    """(runnable, waiting): the work ids NOT blocked by any OPEN question can proceed now; the
    rest wait. Bank-and-continue as a set operation -- throughput is bounded only by real
    dependencies, never by the mere existence of a banked question."""
    blocked = set()
    for q in open(conn):
        blocked |= set(str(b) for b in q.get("blocks", []))
    allw = set(str(w) for w in all_work)
    return (allw - blocked, allw & blocked)


def assert_bank_and_continue(conn, all_work, proceeded):
    """The NEVER-FULL-HALT guard (autonomy s5.1).

    The forbidden pattern: a research-DECIDABLE question is banked and the run then HALTS THE
    WHOLE RUN, advancing none of the work that was never blocked by it. Raises (FAIL) when
    runnable independent work existed, a research (harness-route) question sat banked, and NONE
    of that independent work was advanced. Returns (runnable, waiting) when the run flowed.
    """
    runnable, waiting = independent_work(conn, all_work)
    proceeded = set(str(w) for w in proceeded)
    decidable_open = [q for q in open(conn) if q.get("route") == ROUTE_HARNESS]
    if decidable_open and runnable and not (proceeded & runnable):
        raise QuestionRefusal(
            vc.FAIL, R_HALTED_DECIDABLE,
            "the run advanced NO independent work (runnable=%s) while %d research-decidable "
            "question(s) sat banked; a single banked question must not full-halt the run "
            "(bank-and-continue)" % (sorted(runnable), len(decidable_open)))
    return runnable, waiting


def phase_signout(conn, phase) -> int:
    """A phase cannot sign out while an owner-owed question raised in it is still open.

    Returns PASS when the phase is clear; raises (BLOCKED) otherwise. This is the guard against
    a banked owner-owed question silently dropped -- never resolved, never surfaced (s5.3)."""
    owed = open_owner_owed(conn, phase)
    if owed:
        ids = ", ".join("%s(%s)" % (r["id"], r["class"]) for r in owed)
        raise QuestionRefusal(
            vc.BLOCKED, R_BANKED_DROPPED,
            "phase %r cannot sign out: %d owner-owed question(s) still open and unresolved -- %s"
            % (phase, len(owed), ids))
    return vc.PASS


# --------------------------------------------------------------------- the HITL decision gate
def decision_gate(gate, *, decision_required, decided) -> int:
    """The hitl-decision-gate (`doctrine/leaves/hitl-decision-gate.md`): opt-in per gate row. A gate row marked
    decision-required and not yet decided returns PAUSED (exit 3, PAUSED-FOR-DECISION); a decided
    row, or one not marked decision-required, returns PASS. `gate` is the boundary name for the
    caller's message; the verdict is the contract's own PAUSED / PASS code, never a private one."""
    if decision_required and not decided:
        return vc.PAUSED
    return vc.PASS


def door_question_code(conn, phase, level) -> int:
    """The additive owner-only-question door gate (M1.18 Step 4).

    PAUSED when an owner-only OPEN question is owed in `phase` and the level in force is below
    OWNER_QUESTION_PAUSE_BELOW; PASS otherwise. At or above the threshold the door does not pause
    on the banked owner question -- bank-and-continue: the door composition still decides advance
    or human-go on its own links.
    """
    if defaults.level_num(level) >= OWNER_QUESTION_PAUSE_BELOW:
        return vc.PASS
    return vc.PAUSED if open_owner_owed(conn, phase) else vc.PASS


# --------------------------------------------------------------------- CLI: alpaca ask / alpaca answer
def _split(s):
    if not s:
        return []
    return [t for t in (x.strip() for x in str(s).split(",")) if t]


def _cmd_ask(args):
    from alpaca import cli
    conn = db.connect(cli._root())
    sid = args.session or "cli"
    try:
        rec = bank(conn, args.text, args.cls, phase=args.phase or "", blocks=_split(args.blocks),
                   op=args.op, session=sid)
    except QuestionRefusal as e:
        return vc.emit_verdict("alpaca-ask", e.verdict, "%s: %s" % (e.reason, e.detail))
    required = (rec["route"] == ROUTE_OWNER) or bool(getattr(args, "decision_required", False))
    code = decision_gate("alpaca-ask:%s" % rec["id"], decision_required=required, decided=False)
    tail = "owner decision owed" if code == vc.PAUSED else "routed to the %s" % rec["route"]
    return vc.emit_verdict(
        "alpaca-ask", code,
        "%s banked (class %s -> %s); %s" % (rec["id"], rec["class"], rec["route"], tail),
        evidence=[rec["id"]])


def _cmd_answer(args):
    from alpaca import cli
    conn = db.connect(cli._root())
    sid = args.session or "cli"
    try:
        rec = answer(conn, args.id, args.text, args.by, session=sid)
    except QuestionRefusal as e:
        return vc.emit_verdict("alpaca-answer", e.verdict, "%s: %s" % (e.reason, e.detail))
    return vc.emit_verdict("alpaca-answer", vc.PASS,
                           "%s resolved by %s" % (rec["id"], args.by), evidence=[rec["id"]])


def _parser(sub):
    ask = sub.add_parser("ask", help="bank a question (owner-only exits 3 PAUSED-FOR-DECISION)")
    ask.add_argument("text", help="the question")
    ask.add_argument("--class", dest="cls", required=True,
                     help="the question class (validated against the project class table)")
    ask.add_argument("--phase", default="", help="the phase the question is raised in")
    ask.add_argument("--blocks", default="", help="comma-separated work/row ids it blocks")
    ask.add_argument("--op", default=None, help="the op the question belongs to")
    ask.add_argument("--decision-required", dest="decision_required", action="store_true",
                     help="opt this gate row in as decision-required (PAUSES even off-owner)")
    ans = sub.add_parser("answer", help="resolve a banked question (owner-classes: --by owner)")
    ans.add_argument("id", help="the question id")
    ans.add_argument("text", help="the answer")
    ans.add_argument("--by", required=True, choices=sorted(DECIDERS),
                     help="the decider: owner, or harness for a research question")


def _register():
    from alpaca import cli
    cli.command("ask")(_cmd_ask)
    cli.command("answer")(_cmd_answer)
    cli.register_parser("questions", _parser)


_register()
