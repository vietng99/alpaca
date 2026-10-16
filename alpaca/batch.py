"""The start-of-op question batch and the start gate (op-019 t-200, design sections 3.2 and 3.3).

An autonomous run (L5 or L6) must not start while the owner has an open choice. Every op that
`alpaca op new` opens gets a batch: one item for each of the six big-choice classes of the brief
(D2: runtime, credentials, sandbox, spend, external, deleting; the data file is
templates/interview/intent-slots.yaml) and one item for every question sentence of the plan
(`batch open <op> --plan F`, ids q1, q2, ...). The batch lives in the record as `batch-item`
events, the last event of an item wins.

An item is `open`, `proposed` (the agent pre-filled a proposal; still unanswered) or `answered`.
Only the owner answers: `batch answer` goes through ownerword.human_write, so an agent proposal is
never counted as an owner answer. `--yes` and `--all-yes` record the proposal text exactly as the
latest `batch show` printed it (the owner says yes to what the owner was shown) and are refused
when the item was proposed again after that ask. A batch item id repeats across ops, so an answer
counts only from the session that recorded the batch's latest owner-ask, typed after that ask and
before that session's next owner-ask for another op.

THE START GATE (`task_gate`, called through alpaca/intentgate.py from task claim and task move).
For an op that has a batch, at level 5 or 6, a claim or a move to doing, review or done is refused
until `check` passes once (BATCH-UNANSWERED naming each item, BRIEF-UNSIGNED when the op has no
signed brief). The pass is recorded as `batch-cleared` and the gate stays open after that: the
batch is a start condition, not a standing stop. An item added after `batch-cleared` is late: at
level 6 the agent answers it from the brief and runbook with `batch derive` (a derived choice with
its reason; the claim is refused until it does), at level 5 it raises an `out-of-brief` review card.
Every gate keys on "the op has a batch" (design D-1): an op opened before this landed, or by a
research run through ops.open_op, has none and is not gated.

On an op with a batch `ops.add_task` also needs a phase of the ladder (`require_phase`), because the
self-check binds at phase transitions and a task with no phase would escape it (design D-19).

HONEST LIMIT: an owner word proves the text sits in the transcript Claude Code writes for the
asking session, not who typed it (alpaca/ownerword.py). `derive` is the agent's own word and is
listed as such: `owner_items` returns owner answers only.
"""
from __future__ import annotations

import json
import re

from alpaca import db, ownerword
from alpaca.intentgate import IntentGateRefusal

ITEM_KIND = "batch-item"
CLEARED_KIND = "batch-cleared"
CLASS_SET = "intent"

OPEN, PROPOSED, ANSWERED = "open", "proposed", "answered"
MIN_LEVEL = 5            # the gate starts here (L5 and L6 are the autonomous levels)
DERIVE_LEVEL = 6         # an agent may derive a late item only at this level
LADDER = ("requirement", "design", "build", "verify", "release")

R_UNANSWERED = "BATCH-UNANSWERED"
R_BRIEF = "BRIEF-UNSIGNED"
R_LATE = "BATCH-LATE-ITEM"
R_PHASE_REQUIRED = "TASK-PHASE-REQUIRED"
R_PHASE_UNKNOWN = "TASK-PHASE-UNKNOWN"


class BatchError(ValueError):
    """A refused batch verb. `reason` is the reason token, the message names what is wrong."""

    def __init__(self, reason, detail):
        super().__init__("%s: %s" % (reason, detail))
        self.reason, self.detail = reason, detail


def ask_subject(op):
    return "batch:%s" % op


# ------------------------------------------------------------------ the record
def _data(row):
    try:
        data = json.loads(row["data"])
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def has_batch(conn, op) -> bool:
    """True when `op` has a batch: every gate of design 3.3 keys on this (D-1)."""
    return conn.execute("SELECT 1 FROM events WHERE kind=? AND op=? LIMIT 1",
                        (ITEM_KIND, op)).fetchone() is not None


def items(conn, op) -> dict:
    """The items of `op`, id to its newest state dict, in the order they were first added. Each
    state carries `event`, the id of the event that wrote it."""
    out = {}
    for row in conn.execute("SELECT * FROM events WHERE kind=? AND op=? ORDER BY id",
                            (ITEM_KIND, op)):
        data = _data(row)
        name = data.get("item")
        if not name:
            continue
        state = dict(data, event=row["id"], ts=row["ts"])
        out[name] = state
    return out


def cleared_event(conn, op):
    """The `batch-cleared` event row of `op`, or None."""
    return conn.execute("SELECT * FROM events WHERE kind=? AND op=? ORDER BY id LIMIT 1",
                        (CLEARED_KIND, op)).fetchone()


def _write(conn, session, actor, op, state):
    return db.append_event(conn, session=session, actor=actor, kind=ITEM_KIND, op=op,
                           ref=state["item"], data=state)


def _plain(state):
    """The item state without the keys the reader adds."""
    return {k: v for k, v in state.items() if k not in ("event", "ts")}


# ------------------------------------------------------------------ the six classes and the plan
def class_items():
    """[(id, fills, becomes)] of the six big-choice classes, from the data file."""
    from alpaca import interview
    slots = interview._read_slots(interview.set_file(CLASS_SET), "intent-slots.yaml")
    return [(s["id"], s["fills"], s["becomes"]) for s in slots]


_MARKER = re.compile(r"^\s*(?:>+\s*|[-*+]\s+|\d+[.)]\s+)+")
_CLOSERS = "\"')]*_`"
_SPLIT = re.compile(r"(?<=[.?!])\s+")


def extract_questions(text) -> list:
    """Every sentence of `text` that ends with `?`, anywhere in a line. A line is split on `. `,
    `? ` and `! `; list markers, quote marks and emphasis marks at the ends are trimmed. A
    question that repeats is listed once."""
    out, seen = [], set()
    for line in (text or "").splitlines():
        line = _MARKER.sub("", line).strip()
        for sentence in _SPLIT.split(line):
            sentence = sentence.strip()
            core = sentence.rstrip(_CLOSERS + " ")
            if not core.endswith("?"):
                continue
            sentence = sentence.strip(_CLOSERS + " ").strip()
            if len(sentence) < 2:
                continue
            key = _norm(sentence)
            if key not in seen:
                seen.add(key)
                out.append(sentence)
    return out


def _norm(text):
    return " ".join((text or "").split()).casefold()


# ------------------------------------------------------------------ open and propose
def open_batch(conn, op, *, session, plan_text=None, questions=(), actor="alpaca") -> list:
    """Add the six class items (when not there yet) and one item per question of the plan and of
    `questions` (when the text is not there yet) to the batch of `op`. Returns the ids added. An
    item added after `batch-cleared` is marked late."""
    if not db.rows(conn, "ops", "id=?", (op,)):
        raise BatchError("BATCH-OP-UNKNOWN", "no op %s" % op)
    have = items(conn, op)
    late = cleared_event(conn, op) is not None
    added = []
    for name, fills, becomes in class_items():
        if name not in have:
            _write(conn, session, actor, op, {"item": name, "kind": "class", "state": OPEN,
                                              "question": " ".join(fills.split()),
                                              "becomes": becomes, "late": late})
            added.append(name)
    asked = {_norm(s["question"]) for s in items(conn, op).values()}
    numbers = [int(n[1:]) for n in items(conn, op) if re.fullmatch(r"q\d+", n)]
    nxt = max(numbers or [0]) + 1
    texts = extract_questions(plan_text or "") + [" ".join(str(q).split()) for q in questions
                                                  if str(q).strip()]
    for text in texts:
        if _norm(text) in asked:
            continue
        asked.add(_norm(text))
        name = "q%d" % nxt
        nxt += 1
        _write(conn, session, actor, op, {"item": name, "kind": "question", "state": OPEN,
                                          "question": text, "becomes": "the owner's answer",
                                          "late": late})
        added.append(name)
    return added


def propose(conn, op, item, text, *, session, ref=None):
    """Record the agent's proposal for `item`: the item is `proposed`, still unanswered."""
    state = _need_item(conn, op, item)
    if state["state"] == ANSWERED:
        raise BatchError("BATCH-ITEM-ANSWERED", "%s of %s is already answered" % (item, op))
    text = " ".join(str(text or "").split())
    if not text:
        raise BatchError("BATCH-TEXT-EMPTY", "a proposal needs --text")
    new = dict(_plain(state), state=PROPOSED, proposal=text, proposed_by=session)
    if ref:
        new["proposal_ref"] = str(ref)
    return _write(conn, session, ownerword.session_actor(session), op, new)


def _need_item(conn, op, item):
    state = items(conn, op).get(item)
    if state is None:
        raise BatchError("BATCH-ITEM-UNKNOWN", "%s has no batch item %s" % (op, item))
    return state


# ------------------------------------------------------------------ show (the owner-ask)
FORMS = ("Reply in one message, one phrase for each item:\n"
         "  <id> yes          accepts the proposal shown for that item\n"
         "  <id> no           declines the proposal shown (the item stays open)\n"
         "  <id>: <answer>    answers the item in your own words\n"
         "  all yes           the whole message; accepts every proposal shown")


def _readback(name, text, root, op=None):
    """The sandbox item's answer or proposal read back to the owner (alpaca/sandbox.py `readback`), as
    indented lines, or []. Only with a project root and only for the `sandbox` item. The reading is a
    proposal with the `alpaca sandbox set` command that confirms it; it is never enforced by itself."""
    if root is None or name != "sandbox" or not text:
        return []
    from alpaca import sandbox
    return ["  " + line for line in sandbox.readback(str(text), root, op).splitlines()]


def render(conn, op, root=None) -> str:
    """The text the owner is shown for the batch of `op`. With `root`, the `sandbox` item is followed
    by the reading the write guard makes of its answer or proposal."""
    state = items(conn, op)
    waiting = [s for s in state.values() if s["state"] != ANSWERED]
    lines = ["Batch of %s: %d of %d items wait for your answer before an autonomous run starts."
             % (op, len(waiting), len(state)), ""]
    for name, s in state.items():
        lines.append("[%s] %s" % (name, s["question"]))
        if s["state"] == ANSWERED:
            by = "derived by the agent" if s.get("source") == "derived" else "your answer"
            lines.append("    ANSWERED (%s): %s" % (by, s.get("answer")))
            lines += _readback(name, s.get("answer"), root, op)
        elif s.get("proposal"):
            lines.append("    PROPOSAL (agent, not an answer): %s" % s["proposal"])
            lines += _readback(name, s["proposal"], root, op)
        else:
            lines.append("    NO PROPOSAL: there is nothing to say yes to; answer with `%s: <answer>`"
                         % name)
        lines.append("")
    if waiting and any(not s.get("proposal") for s in waiting):
        lines.append("`all yes` is not available: some items have no proposal.")
        lines.append("")
    lines.append(FORMS)
    return "\n".join(lines)


def show(conn, op, *, session, root=None):
    """Record the owner-ask for the batch of `op` and return (text, the item states). The ask keeps
    each waiting item's proposal as printed, so `--yes` records what the owner was shown."""
    if not has_batch(conn, op):
        raise BatchError("BATCH-NONE", "%s has no batch; open one with `alpaca batch open %s`"
                         % (op, op))
    ownerword.require_real_session(session)
    state = items(conn, op)
    text = render(conn, op, root)
    shown = {n: {"proposal": s.get("proposal"), "proposal_event": s["event"] if s.get("proposal")
                 else None, "state": s["state"]} for n, s in state.items()}
    ownerword.record_ask(conn, session, ask_subject(op), text, op=op,
                         extra={"batch": op, "items": shown})
    return text, state


# ------------------------------------------------------------------ answer
def _ask_op(conn, row):
    """The op an owner-ask is about: its own op, else the op of the task its subject names, else the
    op the subject is. None when the subject names no op (a free-text ask): such an ask does not
    close a batch window (design 3.2: only an ask for another op does)."""
    if row["op"]:
        return row["op"]
    subject = str(row["ref"] or "")
    t = conn.execute("SELECT op FROM tasks WHERE id=?", (subject,)).fetchone()
    if t and t["op"]:
        return t["op"]
    m = re.search(r"\bop-\d+\b", subject)
    return m.group(0) if m else None


def _window(conn, op):
    """(ask row, its data, bound session, since, until) of the latest batch owner-ask of `op`."""
    ask = ownerword.latest_ask(conn, ask_subject(op))
    if ask is None:
        raise ownerword.OwnerWordRefusal(ownerword.R_UNBOUND, "the batch of %s was never shown to "
                                         "the owner; run `alpaca batch show %s` first" % (op, op))
    until = None
    for row in conn.execute("SELECT * FROM events WHERE kind=? AND session=? AND id>? ORDER BY id",
                            (ownerword.ASK_KIND, ask["session"], ask["id"])):
        other = _ask_op(conn, row)
        if other is not None and other != op:
            until = row["ts"]
            break
    return ask, _data(ask), ask["session"], ownerword.ask_floor(ask), until


def _printed(conn, op, name, ask, shown):
    """The proposal of `name` as the ask printed it, or a refusal when there is none or the item was
    proposed again after the ask."""
    entry = (shown.get("items") or {}).get(name)
    if entry is None:
        raise ownerword.OwnerWordRefusal(ownerword.R_UNBOUND, "%s was not in the batch the owner was "
                                         "shown; run `alpaca batch show %s` again" % (name, op))
    if not entry.get("proposal"):
        raise ownerword.OwnerWordRefusal(ownerword.R_UNBOUND, "%s had no proposal when the owner was "
                                         "shown the batch; answer it with `%s: <answer>`" % (name, name))
    now = items(conn, op).get(name) or {}
    if now.get("state") != PROPOSED or now.get("proposal") != entry["proposal"] \
            or now.get("event", 0) > ask["id"]:
        raise ownerword.OwnerWordRefusal(ownerword.R_UNBOUND, "%s was proposed again or declined "
                                         "after the owner was shown the batch; run `alpaca batch "
                                         "show %s` again" % (name, op))
    return entry["proposal"]


def answer(conn, op, item, *, session, by, quote, root, kind, text=None, environ=None,
           isatty=None, read=None):
    """Record the owner's answer to one item (kind `yes`, `no` or `text`). Goes through
    ownerword.human_write; returns the event. A `no` to an item that had a proposal declines the
    proposal and leaves the item open; with no proposal shown it is the answer."""
    state = _need_item(conn, op, item)
    if state["state"] == ANSWERED:
        raise BatchError("BATCH-ITEM-ANSWERED", "%s of %s is already answered" % (item, op))
    ask, shown, bound, since, until = _window(conn, op)
    entry = (shown.get("items") or {}).get(item)
    if entry is None:
        raise ownerword.OwnerWordRefusal(ownerword.R_UNBOUND, "%s was not in the batch the owner was "
                                         "shown; run `alpaca batch show %s` again" % (item, op))
    recorded = None
    if kind == "yes":
        recorded = _printed(conn, op, item, ask, shown)
    elif kind == "text":
        recorded = str(text or "").strip()
        if not recorded:
            raise BatchError("BATCH-TEXT-EMPTY", "an answer needs text")
    word = ownerword.human_write(
        conn, session, by, subject=item, answer=recorded if kind == "text" else kind,
        quote=quote, root=root, form="id", since=since, until=until, bound_session=bound,
        others=list(items(conn, op)), strict_ids=True, environ=environ, isatty=isatty, read=read)
    base = _plain(state)
    if kind == "no" and entry.get("proposal"):
        new = dict(base, state=OPEN, rejected=entry["proposal"], owner_word=word)
        new.pop("proposal", None)
        return _write(conn, session, str(by).strip(), op, new)
    if kind == "no":
        recorded = "no"
    new = dict(base, state=ANSWERED, answer=recorded, answer_kind=kind, source="owner",
               owner_word=word)
    return _write(conn, session, str(by).strip(), op, new)


def answer_all_yes(conn, op, *, session, by, quote, root, environ=None, isatty=None, read=None):
    """The whole-batch form: the owner's first message after the ask is `all yes` (or `yes all`,
    `yes to all`, `all ok`). Records every waiting item's printed proposal; refused as a whole when
    one waiting item had no proposal or was proposed again after the ask."""
    ask, shown, bound, since, until = _window(conn, op)
    state = items(conn, op)
    waiting = [n for n, s in state.items() if s["state"] != ANSWERED]
    texts = {n: _printed(conn, op, n, ask, shown) for n in waiting}
    word = ownerword.human_write(
        conn, session, by, subject=ask_subject(op), answer="yes", quote=quote, root=root,
        form="batch", since=since, until=until, bound_session=bound, environ=environ,
        isatty=isatty, read=read)
    out = []
    for n in waiting:
        new = dict(_plain(state[n]), state=ANSWERED, answer=texts[n], answer_kind="yes",
                   source="owner", owner_word=word)
        out.append(_write(conn, session, str(by).strip(), op, new))
    return out


def answer_reply(conn, op, *, session, by, quote, root, environ=None, isatty=None, read=None):
    """Answer every waiting item the owner's one reply addresses in the id form. Returns the ids
    answered and the ids that stay open; raises when the reply answers none."""
    names = list(items(conn, op))
    done, refused = [], []
    for n in names:
        if items(conn, op)[n]["state"] == ANSWERED:
            continue
        read_back = ownerword.read_answer(quote, n, names, strict=True)
        if read_back is None:
            continue
        kind, text = read_back
        try:
            answer(conn, op, n, session=session, by=by, quote=quote, root=root, kind=kind,
                   text=text, environ=environ, isatty=isatty, read=read)
            done.append(n)
        except (ownerword.OwnerWordRefusal, BatchError) as exc:
            refused.append("%s (%s)" % (n, exc))
    if not done:
        raise ownerword.OwnerWordRefusal(ownerword.R_UNBOUND, "the message answers no waiting item%s"
                                         % ("; " + ", ".join(refused) if refused else ""))
    still = [n for n in names if items(conn, op)[n]["state"] != ANSWERED]
    return done, still


# ------------------------------------------------------------------ check
def check(conn, op, *, root=None) -> list:
    """The problems that keep an autonomous run of `op` from starting: [(reason token, detail)],
    empty when the batch passes. An item the agent derived counts as answered here (a derived
    item exists only after the start); `owner_items` leaves it out."""
    out = []
    waiting = [n for n, s in items(conn, op).items() if s["state"] != ANSWERED]
    if waiting:
        out.append((R_UNANSWERED, "%d batch item(s) of %s have no owner answer: %s"
                    % (len(waiting), op, ", ".join(waiting))))
    from alpaca import brief
    if not brief.signed_events(conn, op):
        out.append((R_BRIEF, "%s has no signed brief; run `alpaca brief present %s <file>` and "
                    "have the owner sign it" % (op, op)))
    return out


# ------------------------------------------------------------------ derive (a late item, level 6)
def derive(conn, op, item, text, reason, *, session, root=None):
    """Answer a late item as a derived choice: the agent's own word, with its reason. Only an item
    added after `batch-cleared`, and only at level 6; an item of the start batch needs the owner.
    Writes the batch-item answer and one derived-choice event (data kind "batch-derive") in one
    transaction, so `selfcheck derived` and the pad count list it."""
    from alpaca import intentgate
    from alpaca import selfcheck
    state = _need_item(conn, op, item)
    if not state.get("late"):
        raise BatchError("BATCH-NOT-LATE", "%s of %s was in the batch before the run started; only "
                         "the owner answers it" % (item, op))
    if state["state"] == ANSWERED:
        raise BatchError("BATCH-ITEM-ANSWERED", "%s of %s is already answered" % (item, op))
    lvl = intentgate.level(conn, session, root=root)
    if lvl < DERIVE_LEVEL:
        raise BatchError("BATCH-DERIVE-LEVEL", "below level 6 a late item is the owner's: a review "
                         "card of kind out-of-brief carries it")
    text = " ".join(str(text or "").split())
    reason = " ".join(str(reason or "").split())
    if not text:
        raise BatchError("BATCH-TEXT-EMPTY", "a derived answer needs --text")
    if not 1 <= len(reason) <= 200:
        raise BatchError("BATCH-REASON", "a derived answer needs --reason of 1 to 200 characters")
    actor = ownerword.session_actor(session)
    with db.transaction(conn):
        state = _need_item(conn, op, item)
        if state["state"] == ANSWERED:
            raise BatchError("BATCH-ITEM-ANSWERED", "%s of %s is already answered" % (item, op))
        new = dict(_plain(state), state=ANSWERED, answer=text, answer_kind="text", source="derived",
                   reason=reason)
        ev = _write(conn, session, actor, op, new)
        db.append_event(conn, session=session, actor=actor, kind=selfcheck.DERIVED_KIND, op=op,
                        ref="%s:%s" % (op, item),
                        data={"op": op, "kind": "batch-derive", "item": item,
                              "question": state.get("question"), "answer": text,
                              "reason": reason, "level": lvl, "batch_event": ev["id"]},
                        conn_in_txn=True)
    return ev


# ------------------------------------------------------------------ the providers of intentgate
def owner_items(conn, op, *, root=None) -> list:
    """The answered items the owner answered, as plain dicts for the self-check packet."""
    if not has_batch(conn, op):
        return []
    out = []
    for name, s in items(conn, op).items():
        if s["state"] == ANSWERED and s.get("source") == "owner":
            word = s.get("owner_word") or {}
            out.append({"kind": "batch-answer", "op": op, "id": name, "question": s["question"],
                        "answer": s.get("answer"), "quote": word.get("quote"),
                        "owner_word": word})
    return out


def _late_open(conn, op):
    cleared = cleared_event(conn, op)
    if cleared is None:
        return []
    return [n for n, s in items(conn, op).items() if s.get("late") and s["state"] != ANSWERED]


def task_gate(conn, task, action, *, level, session, root=None):
    """The start gate (design 3.3). See the module docstring."""
    op = task.get("op")
    if not op or level < MIN_LEVEL or not has_batch(conn, op):
        return
    if cleared_event(conn, op) is None:
        problems = check(conn, op, root=root)
        if problems:
            raise IntentGateRefusal(problems[0][0], "; ".join(
                "%s %s" % (r, d) for r, d in problems) + " (level %d, %s of %s)"
                % (level, action, task.get("id")))
        db.append_event(conn, session=session or "cli", actor="alpaca", kind=CLEARED_KIND, op=op,
                        ref=op, data={"op": op, "level": level, "action": action,
                                      "task": task.get("id")})
        return
    late = _late_open(conn, op)
    if not late:
        return
    if level >= DERIVE_LEVEL:
        raise IntentGateRefusal(R_LATE, "%s has late batch item(s) %s added after the run started; "
                                "answer each from the brief and runbook with `alpaca batch derive "
                                "%s <item> --text T --reason R`" % (op, ", ".join(late), op))
    from alpaca import review
    for name in late:
        review.card(conn, review.OUT_OF_BRIEF, "%s:%s" % (op, name),
                    "batch item %s of %s was added after the run started: %s"
                    % (name, op, items(conn, op)[name]["question"]), op=op,
                    actor="alpaca", session=session)


def ladder_phase(conn, op, phase):
    """`phase` when `op` has a batch, else None: the phase a caller that has no phase of its own (the
    research engine) files its tasks under, so the D-19 rule stays whole on a batch op and an op
    without a batch keeps its tasks phaseless."""
    return phase if has_batch(conn, op) else None


def require_phase(conn, op, phase):
    """On an op with a batch a task needs a phase of the ladder (design D-19), for every caller:
    `task add`, intake and the research engine alike. Raises BatchError (a ValueError)."""
    if not has_batch(conn, op):
        return
    if phase is None or not str(phase).strip():
        raise BatchError(R_PHASE_REQUIRED, "a task of %s needs --phase, one of %s"
                         % (op, ", ".join(LADDER)))
    if str(phase).strip() not in LADDER:
        raise BatchError(R_PHASE_UNKNOWN, "%r is not a phase; use one of %s"
                         % (phase, ", ".join(LADDER)))


# ------------------------------------------------------------------ CLI: alpaca batch
def _cmd_batch(args):
    from alpaca import cli, paths
    root = cli._root()
    conn = db.connect(root)
    sid = args.session or "cli"
    verb = getattr(args, "batch_verb", None)
    try:
        if verb == "open":
            plan = None
            if args.plan:
                with open(args.plan, encoding="utf-8", errors="replace") as fh:
                    plan = fh.read(1024 * 1024)
            added = open_batch(conn, args.op, session=sid, plan_text=plan,
                               questions=args.question or [], actor=ownerword.session_actor(sid))
            print("%s batch: %d item(s) added%s" % (args.op, len(added),
                                                   (": " + ", ".join(added)) if added else ""))
        elif verb == "propose":
            ev = propose(conn, args.op, args.item, args.text, session=sid, ref=args.ref)
            print("%s %s proposed (event %s); still unanswered until the owner answers"
                  % (args.op, args.item, ev["id"]))
        elif verb == "show":
            text, state = show(conn, args.op, session=sid, root=root)
            if args.json:
                print(json.dumps({"verdict": "PASS", "op": args.op, "items": [
                    {"id": n, "question": s["question"], "state": s["state"],
                     "proposal": s.get("proposal"), "answer": s.get("answer")}
                    for n, s in state.items()]}, indent=1, ensure_ascii=True))
            else:
                print(text)
        elif verb == "answer":
            kw = dict(session=sid, by=args.by, quote=args.quote, root=root)
            before = items(conn, args.op).get("sandbox", {}).get("event")
            if not args.by:
                raise BatchError("BATCH-BY", "give --by owner and the owner's message with --quote")
            if args.all_yes:
                evs = answer_all_yes(conn, args.op, **kw)
                print("%s: %d item(s) answered yes to the proposals shown" % (args.op, len(evs)))
            elif args.reply:
                done, still = answer_reply(conn, args.op, **kw)
                print("%s: answered %s; still open: %s" % (args.op, ", ".join(done),
                                                           ", ".join(still) or "none"))
            else:
                if not args.item:
                    raise BatchError("BATCH-ITEM-UNKNOWN", "name the item, or use --all-yes or --reply")
                kind = "yes" if args.yes else "no" if args.no else "text" if args.text else None
                if kind is None:
                    raise BatchError("BATCH-ANSWER", "give --yes, --no or --text T")
                ev = answer(conn, args.op, args.item, kind=kind, text=args.text, **kw)
                print("%s %s: %s" % (args.op, args.item, items(conn, args.op)[args.item]["state"]))
            sb = items(conn, args.op).get("sandbox") or {}
            if sb.get("state") == ANSWERED and sb.get("event") != before:
                from alpaca import sandbox
                print("sandbox answer recorded; the write guard's proposal from it (NOT ENFORCED until the "
                      "owner confirms a path list with `alpaca sandbox set`):")
                print(sandbox.readback(str(sb.get("answer") or ""), root, args.op))
        elif verb == "check":
            problems = check(conn, args.op, root=root)
            if problems:
                print("GATE alpaca-batch-check: FAIL (%s)" % "; ".join("%s %s" % p for p in problems))
                return cli.FAIL
            print("GATE alpaca-batch-check: PASS (every item of %s is answered and the brief is "
                  "signed)" % args.op)
        elif verb == "derive":
            ev = derive(conn, args.op, args.item, args.text, args.reason, session=sid, root=root)
            print("%s %s derived (event %s): %s" % (args.op, args.item, ev["id"], args.reason))
        else:
            return cli.USAGE
    except ownerword.OwnerWordRefusal as exc:
        print("GATE alpaca-batch: FAIL (%s: %s)" % (exc.reason, exc.detail))
        return cli.FAIL
    except (ValueError, OSError) as exc:
        print("GATE alpaca-batch: FAIL (%s)" % exc)
        return cli.FAIL
    return cli.PASS


def _parser(sub):
    b = sub.add_parser("batch", help="the start-of-op question batch: open, propose, show, answer, "
                                     "check, derive")
    bv = b.add_subparsers(dest="batch_verb")
    p = bv.add_parser("open", help="add the six class items and the plan's questions to an op's batch")
    p.add_argument("op"); p.add_argument("--plan", help="a plan file: every sentence ending with ? is an item")
    p.add_argument("--question", action="append", help="one more question; repeatable")
    p = bv.add_parser("propose", help="record the agent's proposal for an item (not an answer)")
    p.add_argument("op"); p.add_argument("item"); p.add_argument("--text", required=True)
    p.add_argument("--from", dest="ref", help="where the proposal comes from")
    p = bv.add_parser("show", help="print the batch to the owner and record the owner-ask")
    p.add_argument("op"); p.add_argument("--json", action="store_true")
    p = bv.add_parser("answer", help="record the owner's answer (owner words)")
    p.add_argument("op"); p.add_argument("item", nargs="?")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--yes", action="store_true"); g.add_argument("--no", action="store_true")
    g.add_argument("--all-yes", action="store_true", help="the whole message is `all yes`")
    g.add_argument("--reply", action="store_true", help="answer every item the one message addresses")
    p.add_argument("--text", help="the answer in the owner's words (with `<id>: <text>` as the quote)")
    p.add_argument("--by", help="a human identity (owner, human)")
    p.add_argument("--quote", help="the owner's whole message")
    p = bv.add_parser("check", help="PASS when every item is answered and the op has a signed brief")
    p.add_argument("op")
    p = bv.add_parser("derive", help="answer a late item from the brief and runbook (level 6)")
    p.add_argument("op"); p.add_argument("item")
    p.add_argument("--text", required=True); p.add_argument("--reason", required=True)


def _register():
    from alpaca import cli
    cli.command("batch")(_cmd_batch)
    cli.register_parser("batch", _parser)


_register()
