"""The owner holds of operator-surface.md section 4.7: open, list, lift and settle. Names owned by
that section.

A hold is one open owner decision: `extension` (the loop runner at N rounds with targets unmet,
FR-040), `budget` (a launch the budget check held, FR-041), `loosen` (the start verb, when the
loader refuses a loosened field without a grant, FR-036) and `ship` (a finished real run with
something to ship, FR-056). Each gate has one reason code, HOLD_CODES; the code goes into the
`research-hold` event as events.emit's `reason`, band PAUSED. Every hold records a `loop-halt`
(loops.halt), so the pad lists it as an owed owner decision; `extension` banks the loops module's
`stuck` question, `budget` and `loosen` bank an `owner-only` question in their own words, and
each of the three raises an `owner-pause` card with subject `question:<qid>`; `ship` raises the
ship card and no question. A hold is lifted by `research-hold-lifted`, after a `loop-resume`
(lift_hold, settle_ship), and a loosen hold whose named fields no longer loosen is lifted as moot
by the start verb's re-check (lift_moot, t-306).

Design: design/research-engine/operator-surface.md section 4.7 and architecture.md section 3.8."""

from __future__ import annotations

import json

from alpaca import db
from alpaca.gates import verdict as vc
from alpaca.research import events, paths


KIND_HOLD = "research-hold"          # = events.HOLD


KIND_LIFT = "research-hold-lifted"   # = events.HOLD_LIFTED

_INSTRUMENT = {"extension": "research-extension", "budget": "research-budget",
               "loosen": "research-loosen", "ship": "research-ship"}
_ACTOR = events.ACTOR


def _rows(conn, kind: str, ref: str | None = None) -> list:
    sql = "SELECT id, ts, ref, op, data FROM events WHERE kind=?"
    params = [kind]
    if ref is not None:
        sql += " AND ref=?"
        params.append(ref)
    out = []
    for row in conn.execute(sql + " ORDER BY id", params).fetchall():
        try:
            data = json.loads(row["data"] or "{}")
        except (TypeError, ValueError):
            data = {}
        out.append({"id": row["id"], "ts": row["ts"], "ref": row["ref"], "op": row["op"],
                    "data": data})
    return out


def _lifts(conn) -> dict:
    """hold event id -> its research-hold-lifted event."""
    out = {}
    for ev in _rows(conn, KIND_LIFT):
        hold = ev["data"].get("hold")
        if isinstance(hold, int) and hold not in out:
            out[hold] = ev
    return out


def _card(conn, card_id):
    if not card_id:
        return None
    from alpaca import review
    return review._state(conn).get(str(card_id))


def _as_hold(ev: dict, lift: dict | None = None) -> dict:
    d = ev["data"]
    return {"id": ev["id"], "hold": ev["id"], "ts": ev["ts"], "ref": d.get("ref", ev["ref"]),
            "op": ev.get("op"), "run": d.get("run"), "gate": d.get("gate"),
            "code": d.get("code"), "detail": d.get("detail"), "question": d.get("question"),
            "card": d.get("card"), "halt_event": d.get("halt_event"), "bound": d.get("bound"),
            "fields": d.get("fields"), "measured": d.get("measured"),
            "lifted": None if lift is None else {"event": lift["id"],
                                                  "decision": lift["data"].get("decision"),
                                                  "answer": lift["data"].get("answer")}}


def _run_of(ref: str) -> str | None:
    return ref if isinstance(ref, str) and paths.RUN_ID.fullmatch(ref) else None


def _newest_halt(conn, ref: str) -> int | None:
    got = conn.execute("SELECT id FROM events WHERE kind='loop-halt' AND ref=? ORDER BY id DESC "
                       "LIMIT 1", (ref,)).fetchone()
    return got["id"] if got else None


def _newest_stuck_question(conn, ref: str) -> str | None:
    from alpaca import questions
    found = None
    for rec in questions._state(conn).values():
        if rec.get("status") == "open" and str(ref) in (rec.get("blocks") or ()) \
                and rec.get("class") == "stuck":
            found = rec.get("id")
    return found


def _ship_subject(conn, ref: str) -> str:
    start = conn.execute("SELECT data FROM events WHERE kind=? AND ref=? ORDER BY id LIMIT 1",
                         (events.RUN_START, ref)).fetchone()
    mode = None
    if start is not None:
        try:
            mode = json.loads(start["data"] or "{}").get("mode")
        except (TypeError, ValueError):
            mode = None
    return "research:%s:%s" % (ref, "repair" if mode == "repair" else "champion")


def open_hold(conn, root, *, ref, gate, code, detail, op, bound, session, fields=None,
              measured=None) -> dict:
    """Records research-hold through events.emit(..., code=vc.PAUSED, reason=code, data={ref,
    gate, detail, question, card, halt_event, bound}), so the hold's code lands as data.code and
    never in data. code is HOLD_CODES[gate]; any other value raises ValueError. A loosen hold
    also records `fields`, the field paths it names (t-306), when the caller passes them, and
    `measured` {baseline, pack_sha256}: the research-contract event id (or None) and the pack
    hash its loosening was measured against, when the caller passes them; moot_reason lifts it
    only on a re-check of that same baseline (and, with no baseline, that same pack).

    Idempotent while a hold for (ref, gate) is open: returns it. For gate ship it returns the
    run's ship hold whether open or answered, so one run gets one ship card. Otherwise it records
    what operator-surface section 4.7 opens with the gate (loops.halt with the gate's instrument
    and `bound`, the question and the owner-pause card, or the ship card) and the research-hold
    event, all in one transaction."""
    if gate not in HOLD_CODES or code != HOLD_CODES[gate]:
        raise ValueError("hold gate %r takes the code %r, not %r"
                         % (gate, HOLD_CODES.get(gate), code))
    ref = str(ref)
    detail = str(detail or gate)
    lifts = _lifts(conn)
    for ev in reversed(_rows(conn, KIND_HOLD, ref)):
        if ev["data"].get("gate") != gate:
            continue
        if gate == "ship":
            return _as_hold(ev, lifts.get(ev["id"]))
        if ev["id"] not in lifts:
            return _as_hold(ev)
    from alpaca import messages, questions, review
    from alpaca.posture import loops
    instrument = _INSTRUMENT[gate]
    question = None
    with db.transaction(conn):
        if gate == "ship":
            card = review.ship_card(conn, _ship_subject(conn, ref), detail, op=op,
                                    session=session)
            loops.halt(conn, ref, detail, instrument=instrument, session=session, actor=_ACTOR,
                       root=root, emit=False, bound=bound)
        elif gate == "extension":
            loops.halt(conn, ref, detail, instrument=instrument, session=session, actor=_ACTOR,
                       root=root, emit=True, bound=bound)
            question = _newest_stuck_question(conn, ref)
            if question is None:
                question = questions.bank(conn, "%s: %s" % (ref, detail), "stuck", blocks=[ref],
                                          op=op, actor=_ACTOR, session=session)["id"]
            card = review.card(conn, "owner-pause", "question:%s" % question, detail, op=op,
                               actor="instrument", session=session)
        else:
            loops.halt(conn, ref, detail, instrument=instrument, session=session, actor=_ACTOR,
                       root=root, emit=False, bound=bound)
            try:
                messages.post(conn, _ACTOR, "owner", "blocker", "%s hold on %s: %s"
                              % (gate, ref, detail), session=session, root=root)
            except Exception:  # noqa: BLE001 - the message is a courtesy; the hold stands without it
                pass
            question = questions.bank(conn, "%s hold on %s: %s" % (gate, ref, detail),
                                      "owner-only", blocks=[ref], op=op, actor=_ACTOR,
                                      session=session)["id"]
            card = review.card(conn, "owner-pause", "question:%s" % question, detail, op=op,
                               actor="instrument", session=session)
        ev = events.emit(conn, events.HOLD, run=_run_of(ref), ref=ref, op=op, session=session,
                         code=vc.PAUSED, reason=code, in_txn=True,
                         data=dict({"ref": ref, "gate": gate, "detail": detail,
                                    "question": question,
                                    "card": card.get("id") if isinstance(card, dict) else None,
                                    "halt_event": _newest_halt(conn, ref), "bound": bound},
                                   **({"fields": sorted({str(f) for f in fields})}
                                      if gate == "loosen" and fields else {}),
                                   **({"measured": {"baseline": measured.get("baseline"),
                                                    "pack_sha256": measured.get("pack_sha256")}}
                                      if gate == "loosen" and isinstance(measured, dict)
                                      else {})))
    return _as_hold({"id": ev["id"], "ts": ev["ts"], "ref": ref, "op": op,
                     "data": json.loads(ev["data"])})


HOLD_CODES = {"extension": "RUN-ROUNDS-REACHED", "budget": "RUN-BUDGET-HOLD",
              "loosen": "CONTRACT-LOOSEN-UNGRANTED", "ship": "RUN-SHIP-DECISION"}   # section 12.4


def open_holds(conn, ref=None) -> list:
    """Holds whose research-hold event has no research-hold-lifted event, except ship holds whose
    card is moved (answered, even before settle_ship records the lift). Newest first."""
    lifts = _lifts(conn)
    out = []
    for ev in _rows(conn, KIND_HOLD, None if ref is None else str(ref)):
        if ev["id"] in lifts:
            continue
        if ev["data"].get("gate") == "ship":
            card = _card(conn, ev["data"].get("card"))
            if card is not None and card.get("status") != "open":
                continue
        out.append(_as_hold(ev))
    out.sort(key=lambda h: h["id"], reverse=True)
    return out


def lift_hold(conn, root, *, hold, decision, answer, session) -> dict:
    """For extension, budget and loosen, in one transaction: loops.resume (records loop-resume
    with ref, decision, hold); questions.answer(qid, text, "owner") with a text that is the answer
    and the decision id; review.move(conn, card, text, "owner") when the hold's owner-pause card
    is still open, so the queue keeps no answered card; then research-hold-lifted
    {hold, decision, answer}. answer is "stop" when the owner's grant stops the run (D11), else
    the grant's gate. Called only after owner.authorized returned True for that decision. A ship
    hold is never lifted here (settle_ship); a hold already lifted is returned as it was lifted.
    Raises KeyError for an unknown hold."""
    rows = [ev for ev in _rows(conn, KIND_HOLD) if ev["id"] == hold]
    if not rows:
        raise KeyError("no research-hold event %r" % (hold,))
    ev = rows[0]
    d = ev["data"]
    if d.get("gate") == "ship":
        raise ValueError("a ship hold is lifted by the owner's card move (settle_ship)")
    lifted = _lifts(conn).get(ev["id"])
    if lifted is not None:
        return _as_hold(ev, lifted)
    from alpaca import questions, review
    from alpaca.posture import loops
    ref = d.get("ref", ev["ref"])
    text = "%s (decision %s)" % (answer, decision)
    with db.transaction(conn):
        loops.resume(conn, ref, decision=decision, hold=ev["id"], session=session)
        qid = d.get("question")
        if qid:
            rec = questions._state(conn).get(str(qid))
            if rec is not None and rec.get("status") == "open":
                questions.answer(conn, qid, text, "owner", session=session)
        card = _card(conn, d.get("card"))
        if card is not None and card.get("status") == "open":
            review.move(conn, card["id"], text, "owner", session=session)
        lift = events.emit(conn, events.HOLD_LIFTED, run=_run_of(ref), ref=ref, op=ev.get("op"),
                           session=session, in_txn=True,
                           data={"hold": ev["id"], "decision": decision, "answer": answer,
                                 "gate": d.get("gate")})
    return _as_hold(ev, {"id": lift["id"], "data": json.loads(lift["data"])})


def held_fields(hold: dict) -> list:
    """The field paths a loosen hold names: its recorded `fields` (holds opened since t-306) and
    every `<field>: accepted <value>, new <value>` line of its detail (every loosen hold the start
    verb opened, older ones included), sorted. [] when it names none, and the moot check then keeps
    the hold open. A detail line is split at every ": accepted " whose rest holds ", new ", and
    each split's field is named, so a file path that itself holds ": accepted " is never cut to a
    shorter name only (a name that is no field never loosens, and could make the hold look moot);
    an extra candidate can only keep a hold open, never lift one."""
    got = {str(f) for f in (hold.get("fields") or []) if isinstance(f, str) and f}
    for line in str(hold.get("detail") or "").splitlines():
        line = line.strip()
        at = line.find(_ACCEPTED)
        while at > 0:
            if _NEW in line[at + len(_ACCEPTED):]:
                got.add(line[:at])
            at = line.find(_ACCEPTED, at + 1)
    return sorted(got)


_ACCEPTED, _NEW = ": accepted ", ", new "


def _measured(conn, ev) -> tuple:
    """(recorded, baseline event id or None, pack_sha256 or None) the loosen hold `ev` was
    measured against: its `measured` data (holds opened since the t-306 fix); else, for an older
    hold, (False, the newest research-contract event of the hold's runbook path recorded before
    the hold (any runbook id; the freeze compared against the newest one of its id and path),
    None)."""
    d = ev["data"]
    got = d.get("measured")
    if isinstance(got, dict):
        return True, got.get("baseline"), got.get("pack_sha256")
    ref = str(d.get("ref", ev["ref"]) or "")
    path = ref[len("contract:"):].rpartition("#")[0] if ref.startswith("contract:") else ""
    newest = None
    for row in _rows(conn, _KIND_CONTRACT):
        if row["id"] >= ev["id"]:
            break
        if path and isinstance(row["data"], dict) and row["data"].get("runbook") == path:
            newest = row["id"]
    return False, newest, None


_KIND_CONTRACT = "research-contract"   # = contract.KIND_CONTRACT, read by _measured


def moot_reason(conn, hold, recheck) -> str | None:
    """None when the open loosen hold `hold` (an event id) is moot against `recheck`
    (contract.loosen_recheck of the contract the start would open now), else why it is not. Moot
    needs all of: the hold names at least one field (held_fields); the re-check covers the hold's
    ref; the re-check compared against the same research-contract event the hold was measured
    against (_measured), so a runbook id changed after the hold (no baseline, an empty pack diff)
    or a baseline landed since the hold never reads as moot; with no baseline on either side (a
    task-layer hold at a first block) the pack hash is the one the hold recorded, so the pack was
    not edited to take the loosening in; and no named field is loosened in any layer, where a
    loosened item that holds the field, or a loosened leaf inside a named item, counts
    (_overlaps). lift_moot
    asks it, so no caller lifts a hold this refuses; the start verb asks it at check 2 and lifts
    only after its other checks."""
    rows = [ev for ev in _rows(conn, KIND_HOLD) if ev["id"] == hold]
    if not rows:
        raise KeyError("no research-hold event %r" % (hold,))
    ev = rows[0]
    d = ev["data"]
    if d.get("gate") != "loosen":
        return "only a loosen hold lifts as moot, not a %r hold" % (d.get("gate"),)
    ref = d.get("ref", ev["ref"])
    fields = held_fields(_as_hold(ev))
    if not fields:
        return "loosen hold %s names no field; it is not moot" % ev["id"]
    refs = (recheck or {}).get("refs") or {}
    layer = refs.get(ref)
    if layer is None:
        return "the re-check does not cover %s; loosen hold %s is not moot" % (ref, ev["id"])
    recorded, baseline, pack = _measured(conn, ev)
    now = (recheck or {}).get("baseline")
    if now != baseline:
        return ("the re-check compared against baseline %s, not baseline %s that loosen hold %s "
                "was measured against (the runbook id or path changed, or a baseline landed "
                "since); it is not moot" % (now, baseline, ev["id"]))
    if now is None and not (recorded and pack and (recheck or {}).get("pack_sha256") == pack):
        return ("the re-check found no baseline for the runbook, and the pack is not the one "
                "loosen hold %s was measured against; it is not moot" % ev["id"])
    loosened = {f for lay in refs.values() if isinstance(lay, dict)
                for f in lay.get("loosened") or ()}
    hits = {f: sorted(l for l in loosened if _overlaps(f, l)) for f in fields}
    still = sorted(f for f, got in hits.items() if got)
    if still:
        via = sorted({l for f in still for l in hits[f] if l != f})
        return "loosen hold %s is not moot: %s still loosen%s%s" % (
            ev["id"], ", ".join(still), "s" if len(still) == 1 else "",
            " (loosened as %s)" % ", ".join(via) if via else "")
    return None


def _overlaps(field: str, other: str) -> bool:
    """True when one field path is the other or sits inside it. pack_diff reports a leaf under an
    item that entered or left as the item alone (contract.pack_diff skips leaves under `lone`
    items), so a held leaf research.metrics[value].target whose metric was renamed shows up only
    as research.metrics[value]; the other way, a held item can come back with a loosened leaf.
    Either way the held field still loosens (check round s2 of t-306, finding B2)."""
    if field == other:
        return True
    a, b = sorted((field, other), key=len)
    return b.startswith(a + ".") or b.startswith(a + "[")


MOOT = "moot"


def lift_moot(conn, root, *, hold, recheck, session) -> dict:
    """Lift an open loosen hold whose loosening is gone (in-session-runtime.md section 22,
    t-306). `recheck` is contract.loosen_recheck of the contract the start would open now. The
    hold is lifted only when moot_reason finds it moot (it names a field, the re-check covers its
    ref and used the baseline the hold was measured against, and no named field loosens in any
    layer); otherwise ValueError with that reason and nothing is recorded, so no caller can lift
    a hold whose field still loosens or that the re-check did not measure. The lift is
    lift_hold's, in one transaction: loops.resume (decision None), the hold's owner-only question
    answered and its owner-pause card moved with a text that says the re-check, not the owner,
    lifted it, then research-hold-lifted {hold, decision: None, answer: "moot", gate: "loosen",
    recheck: {ref, fields, loosened, changed, pack_sha256, contract_sha256, files_sha256,
    baseline}}. A hold already lifted is returned as it was lifted."""
    rows = [ev for ev in _rows(conn, KIND_HOLD) if ev["id"] == hold]
    if not rows:
        raise KeyError("no research-hold event %r" % (hold,))
    ev = rows[0]
    d = ev["data"]
    if d.get("gate") != "loosen":
        raise ValueError("only a loosen hold lifts as moot, not a %r hold" % (d.get("gate"),))
    lifted = _lifts(conn).get(ev["id"])
    if lifted is not None:
        return _as_hold(ev, lifted)
    why = moot_reason(conn, ev["id"], recheck)
    if why is not None:
        raise ValueError(why)
    ref = d.get("ref", ev["ref"])
    fields = held_fields(_as_hold(ev))
    layer = recheck["refs"][ref]
    named = set(fields)
    checked = {"ref": ref, "fields": fields, "loosened": list(layer.get("loosened") or ()),
               "changed": [c for c in layer.get("changed") or () if c.get("field") in named],
               "pack_sha256": recheck.get("pack_sha256"),
               "contract_sha256": recheck.get("contract_sha256"),
               "files_sha256": recheck.get("files_sha256"), "baseline": recheck.get("baseline")}
    text = ("moot: the start re-check of %s found none of %s loosened (pack %s); lifted by the "
            "start verb, not by an owner decision" % (ref, ", ".join(fields),
                                                      str(recheck.get("pack_sha256") or "")[:12]))
    from alpaca import questions, review
    from alpaca.posture import loops
    with db.transaction(conn):
        loops.resume(conn, ref, decision=None, hold=ev["id"], session=session)
        qid = d.get("question")
        if qid:
            rec = questions._state(conn).get(str(qid))
            if rec is not None and rec.get("status") == "open":
                questions.answer(conn, qid, text, "owner", session=session)
        card = _card(conn, d.get("card"))
        if card is not None and card.get("status") == "open":
            review.move(conn, card["id"], text, "owner", session=session)
        lift = events.emit(conn, events.HOLD_LIFTED, run=_run_of(ref), ref=ref, op=ev.get("op"),
                           session=session, in_txn=True,
                           data={"hold": ev["id"], "decision": None, "answer": MOOT,
                                 "gate": "loosen", "recheck": checked})
    return _as_hold(ev, {"id": lift["id"], "data": json.loads(lift["data"])})


def settle_ship(conn, root, *, session) -> list:
    """For each ship hold with no research-hold-lifted whose card the owner has moved, in one
    transaction: loops.resume (loop-resume with ref the run, decision "review-move:<event id>",
    hold) and research-hold-lifted {hold, decision, answer: the card's decision text}. Every
    research verb that writes events calls it first, so the ship halt is lifted by the first
    such verb after the owner's move. Returns the holds it settled."""
    from alpaca.posture import loops
    lifts = _lifts(conn)
    settled = []
    for ev in _rows(conn, KIND_HOLD):
        d = ev["data"]
        if d.get("gate") != "ship" or ev["id"] in lifts:
            continue
        card = _card(conn, d.get("card"))
        if card is None or card.get("status") == "open":
            continue
        move = conn.execute("SELECT id FROM events WHERE kind='review-move' AND ref=? ORDER BY id "
                            "DESC LIMIT 1", (card["id"],)).fetchone()
        decision = "review-move:%s" % (move["id"] if move else card["id"])
        ref = d.get("ref", ev["ref"])
        with db.transaction(conn):
            loops.resume(conn, ref, decision=decision, hold=ev["id"], session=session)
            lift = events.emit(conn, events.HOLD_LIFTED, run=_run_of(ref), ref=ref,
                               op=ev.get("op"), session=session, in_txn=True,
                               data={"hold": ev["id"], "decision": decision,
                                     "answer": card.get("decision") or "", "gate": "ship"})
        settled.append(_as_hold(ev, {"id": lift["id"], "data": json.loads(lift["data"])}))
    return settled
