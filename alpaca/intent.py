"""Intent record and the done-bar (M3.6).

What it holds to: the intent record is the done-bar; the intent label and the done_when are
split, who authors each is recorded, and a close records its judgment basis
(`doctrine/leaves/intent-is-the-done-bar.md`; the autodrive level decides which boundaries run on
their own, `doctrine/leaves/level-gated-advance.md`).

The split this module makes structural:

  * The LABEL names the work ("de-sign the checklist engine"). The BAR (done_when) says when the
    work is done ("the acceptance suite is green"). They are different fields with different
    authors: the owner authors the bar; the agent may draft it and must have it trace to an owner
    artifact.
  * A recorded bar is IMMUTABLE. `edit` always refuses: a change is a NEW intent citing the old
    (record(..., supersedes=<old id>)), so the record keeps both and the change is traceable.
  * An agent can neither loosen a recorded bar (supersede one) nor author a fresh lax one at or
    above L5 without an owner artifact (a `pointer`) to trace to. The owner needs no pointer: the
    owner is the artifact.
  * An op cannot open without a label AND a bar whose author is recorded. `next_from_queue` turns
    a queue line into an intent record (label + bar + the queue file as the owner artifact), and
    `alpaca op new --from-intent` opens the op from it. The queue stays the only source an unattended
    loop opens the next op from.
  * Op close records who judged the bar met and on what basis: `judge` appends a JUDGE_KIND event
    and refuses an empty basis.

Everything lives on the append-only record: INTENT_KIND events are the intents, JUDGE_KIND events
the judgments, BIND_KIND events plus a meta row bind an intent to the op it opened. There is no
intent file. `alpaca` is the sole writer, exactly as everywhere else.

HONEST LIMITS: `author` and `author_role` are self-asserted tokens, exactly as `actor` is in the
decisions and questions ledgers. "owner" here means the caller said owner, never a proof the human
owner authored the bar. Binding a bar to a person needs an external identity in front of `record`.
The `pointer` is the owner artifact the bar traces to; this module checks it is present, not that
it is genuine.
"""
from __future__ import annotations

import json
import os

from alpaca import db, paths, util
from alpaca.gates import verdict as vc
from alpaca.phase import defaults

#: the record channels. INTENT_KIND is the intent record; JUDGE_KIND records who judged the bar
#: met and on what basis; BIND_KIND binds an intent to the op it opened.
INTENT_KIND = "intent-record"
JUDGE_KIND = "intent-judge"
BIND_KIND = "intent-bind"

#: the author role that must trace to an owner artifact when it loosens a bar or authors one high.
AGENT_ROLE = "agent"
OWNER_ROLE = "owner"

#: the level at or above which an agent-authored bar is "lax" without an owner artifact to trace to
#: (at L5 the boundary is automatic, so a bar authored there needs the owner behind it). Below it a drafted bar is a review-card matter, not a refusal here.
LAX_LEVEL = 5

#: the owner artifact a queue-derived intent traces to: the queue file the owner wrote.
QUEUE_POINTER = "local:intents/queue.md"

# reason tokens -- controls bind to these exact strings (grep ONE token, not prose).
R_LABEL_EMPTY = "INTENT-LABEL-EMPTY"
R_BAR_EMPTY = "INTENT-DONE-BAR-EMPTY"
R_AUTHOR_EMPTY = "INTENT-BAR-AUTHOR-MISSING"
R_AGENT_LAX_BAR_NO_OWNER = "INTENT-AGENT-LAX-BAR-NO-OWNER-ARTIFACT"
R_AGENT_LOOSEN_NO_OWNER = "INTENT-AGENT-LOOSEN-NO-OWNER-ARTIFACT"
R_BAR_IMMUTABLE = "INTENT-BAR-IMMUTABLE"
R_SUPERSEDE_UNKNOWN = "INTENT-SUPERSEDES-UNKNOWN"
R_JUDGE_NO_BASIS = "INTENT-JUDGE-NO-BASIS"
R_NO_INTENT = "INTENT-NONE"
R_QUEUE_NO_OPEN = "INTENT-QUEUE-NO-OPEN-LINE"
R_QUEUE_NO_BAR = "INTENT-QUEUE-LINE-NO-BAR"
R_QUEUE_ABSENT = "INTENT-QUEUE-ABSENT"

#: how a queue line separates its label from its bar. Case-insensitive; a hyphenated spelling is
#: accepted too, so the same note reads naturally to a human editing the queue.
_QUEUE_SEPARATORS = (" - done when ", " - done-when ")


class IntentRefusal(Exception):
    """A refusal carrying its integer verdict-band code and its EXACT reason token, exactly as
    alpaca.decisions.DecisionRefusal and alpaca.posture.authority.AuthorityRefusal do. The verdict numbers
    are referenced from alpaca.gates.verdict, never restated here."""

    def __init__(self, verdict_code: int, reason: str, detail: str = ""):
        super().__init__("%s %s: %s" % (vc.name_of(verdict_code), reason, detail))
        if verdict_code not in vc.VERDICT_BAND:
            raise ValueError("a refusal needs a verdict-band code, got %r" % (verdict_code,))
        self.verdict = verdict_code
        self.reason = reason
        self.detail = detail


def _clean(value) -> str:
    return "" if value is None else str(value).strip()


# --------------------------------------------------------------------------- the record channel
def _intent_events(conn):
    return [e for e in db.events(conn, kind=INTENT_KIND, limit=1000000)]


def get(conn, intent_id):
    """The intent record addressed by id (the latest carrying that id), or None."""
    iid = _clean(intent_id)
    if not iid:
        return None
    hit = None
    for e in _intent_events(conn):
        if (e["data"] or {}).get("id") == iid:
            hit = e["data"]
    return hit


def all_intents(conn):
    """Every recorded intent, oldest first (append order)."""
    return [e["data"] for e in _intent_events(conn)]


def _intent_id(label, done_when, author, pointer, supersedes) -> str:
    """A stable, content-derived id (never the clock), so a re-run is reproducible and a distinct
    intent -- any field, the supersede link included, that differs -- is a distinct id."""
    digest = util.sha256_hex(util.canonical_json({
        "label": label, "done_when": done_when, "author": author,
        "pointer": pointer, "supersedes": supersedes}))
    return "int-%s" % digest[:12]


# --------------------------------------------------------------------------- record the intent
def record(conn, label, done_when, author, pointer="", *, author_role=OWNER_ROLE, level=None,
           session="cli", supersedes=None) -> dict:
    """Append one intent record: a label (names the work) and a bar (done_when) whose author is
    recorded. Refuses a missing label, bar or author. An agent that loosens a recorded bar (passes
    `supersedes`) or authors a fresh bar at or above L5 must pass an owner artifact `pointer` to
    trace to; the owner needs none. A recorded bar is never edited in place: a change is a new
    intent citing the old via `supersedes`. Returns the intent record.
    """
    label = _clean(label)
    done_when = _clean(done_when)
    author = _clean(author)
    pointer = _clean(pointer)
    role = (_clean(author_role) or OWNER_ROLE).lower()
    supersedes = _clean(supersedes)

    if not label:
        raise IntentRefusal(vc.BLOCKED, R_LABEL_EMPTY, "an intent needs a label that names the work")
    if not done_when:
        raise IntentRefusal(vc.BLOCKED, R_BAR_EMPTY,
                            "an intent needs a done bar that says when the work is done")
    if not author:
        raise IntentRefusal(vc.BLOCKED, R_AUTHOR_EMPTY,
                            "the bar's author must be recorded; an unattributed bar is refused")

    if role == AGENT_ROLE:
        if supersedes and not pointer:
            raise IntentRefusal(
                vc.BLOCKED, R_AGENT_LOOSEN_NO_OWNER,
                "an agent loosening a recorded bar (%s) needs an owner artifact to trace to; a "
                "bare agent change to a recorded bar is refused" % supersedes)
        if level is not None and defaults.level_num(level) >= LAX_LEVEL and not pointer:
            raise IntentRefusal(
                vc.BLOCKED, R_AGENT_LAX_BAR_NO_OWNER,
                "an agent authoring a fresh bar at or above L%d needs an owner artifact to trace "
                "to; refusing a lax agent-authored bar with no owner behind it" % LAX_LEVEL)

    if supersedes and get(conn, supersedes) is None:
        raise IntentRefusal(vc.BLOCKED, R_SUPERSEDE_UNKNOWN,
                            "a change cites an intent that is not on the record: %s" % supersedes)

    # the owner is the artifact; an agent draft that traces to an owner artifact is confirmed by
    # that artifact. Recorded so a reader sees whether a human owner stood behind the bar.
    confirmed = (role == OWNER_ROLE) or bool(pointer)
    iid = _intent_id(label, done_when, author, pointer, supersedes)
    rec = {"id": iid, "label": label, "done_when": done_when, "author": author,
           "author_role": role, "pointer": pointer, "level": ("" if level is None else str(level)),
           "confirmed": confirmed, "supersedes": supersedes}
    with db.transaction(conn):
        db.append_event(conn, session=session, actor=author or role, kind=INTENT_KIND,
                        ref=iid, data=dict(rec), conn_in_txn=True)
    return rec


def edit(conn, intent_id, **_fields):
    """Never edits a recorded bar. A recorded bar is immutable: a change is a NEW intent citing the
    old via record(..., supersedes=<id>). This function exists only to make the refusal explicit at
    the one call a caller might reach for."""
    raise IntentRefusal(
        vc.BLOCKED, R_BAR_IMMUTABLE,
        "a recorded bar (%s) is immutable; a change is a new intent citing the old "
        "(record(..., supersedes=%r))" % (intent_id, intent_id))


# --------------------------------------------------------------------------- judge the bar
def judge(conn, op, basis, *, judge=OWNER_ROLE, session="cli") -> dict:
    """Record who judged the bar met and on what basis (op close). Refuses an empty basis. The
    judgment cites the intent bound to the op, when there is one, so a closed op says both what it
    committed to and who declared it met."""
    basis_s = _clean(basis)
    if not basis_s:
        raise IntentRefusal(vc.BLOCKED, R_JUDGE_NO_BASIS,
                            "a judgment records the basis on which the bar was judged met; a bare "
                            "close with no basis is refused")
    iid = for_op(conn, op)
    data = {"op": op, "judge": _clean(judge) or OWNER_ROLE, "basis": basis_s, "intent": iid}
    db.append_event(conn, session=session, actor=data["judge"], kind=JUDGE_KIND, op=op,
                    ref=iid, data=dict(data))
    return data


# --------------------------------------------------------------------------- bind to the op
def op_meta_key(op) -> str:
    return "intent:op:%s" % op


def bind_op(conn, op, intent_id, *, session="cli", actor="alpaca") -> dict:
    """Bind the intent an op opened from onto the op forever: a BIND_KIND event on the append-only
    chain and a meta row `for_op` reads back."""
    iid = _clean(intent_id)
    with db.transaction(conn):
        db.append_event(conn, session=session, actor=actor, kind=BIND_KIND, op=op,
                        ref=iid, data={"intent": iid}, conn_in_txn=True)
        db.meta_set(conn, op_meta_key(op), iid)
    return {"op": op, "intent": iid}


def for_op(conn, op):
    """The intent id an op opened from, read back from the record, or None if never bound."""
    raw = db.meta_get(conn, op_meta_key(op))
    return raw or None


# --------------------------------------------------------------------------- the intent queue
def _queue_path(root) -> str:
    return os.path.join(root, "intents", "queue.md")


def _split_label_bar(body):
    """Split a queue line body into (label, bar). The bar is everything after the separator (a
    trailing `, proof local:...` stays part of the bar). Returns ('', ...) shapes verbatim so the
    caller decides what an empty half means."""
    low = body.lower()
    for sep in _QUEUE_SEPARATORS:
        i = low.find(sep)
        if i >= 0:
            return body[:i].strip(), body[i + len(sep):].strip()
    return body.strip(), ""


def parse_queue(text):
    """Parse the intent queue into a list of line dicts: {checked, label, done_when, raw}. Only
    checkbox lines (`- [ ]` / `- [x]`) are intent lines; headings and blanks are skipped."""
    import re
    line_re = re.compile(r"^\s*-\s*\[( |x|X)\]\s*(.+?)\s*$")
    out = []
    for raw in (text or "").splitlines():
        m = line_re.match(raw)
        if not m:
            continue
        checked = m.group(1).lower() == "x"
        label, bar = _split_label_bar(m.group(2))
        out.append({"checked": checked, "label": label, "done_when": bar, "raw": raw})
    return out


def next_open_line(root):
    """The first unchecked intent line in the queue, or None. Raises IntentRefusal(BLOCKED) when
    the queue file is absent."""
    path = _queue_path(root)
    if not os.path.isfile(path):
        raise IntentRefusal(vc.BLOCKED, R_QUEUE_ABSENT, "no intent queue at %s" % path)
    for line in parse_queue(util.read_text(path)):
        if not line["checked"]:
            return line
    return None


def next_from_queue(conn, *, root=None, session="cli") -> dict:
    """Pick the next unchecked queue line and record it as an intent (the owner authored the queue,
    so the queue file is the owner artifact the bar traces to). Refuses when the queue has no open
    line, or the open line carries no bar. The queue stays the only source an unattended loop opens
    the next op from."""
    root = root or paths.root()
    line = next_open_line(root)
    if line is None:
        raise IntentRefusal(vc.BLOCKED, R_QUEUE_NO_OPEN,
                            "the intent queue has no open line to open an op from")
    if not line["label"]:
        raise IntentRefusal(vc.BLOCKED, R_QUEUE_NO_BAR, "a queue line needs a label")
    if not line["done_when"]:
        raise IntentRefusal(
            vc.BLOCKED, R_QUEUE_NO_BAR,
            "the queue line %r carries no done bar; an op cannot open without one" % line["label"])
    return record(conn, line["label"], line["done_when"], OWNER_ROLE, pointer=QUEUE_POINTER,
                  author_role=OWNER_ROLE, session=session)
