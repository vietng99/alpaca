"""M4.6: the historian - sort cadence, the never-drop unsorted bucket, the per-op record, alpaca day.

The record (`.alpaca/alpaca.db` events) is the raw layer. Capture is DUMB: the drain
(`alpaca.wiki.ingest.drain`) lands every session event as a note with a fixed mechanical role label
and NEVER judges. `alpaca sort` is the ONE pass that judges (it pairs intents with results). The
historian is the COMPLETENESS layer over that record:

  * `timeline(conn, op)` builds the per-op record model: the op's intent, its done bar, the
    authority it ran under, the judgment basis its close recorded, and the choices log, each entry
    carrying a pointer back to the note it came from.
  * `unsorted(conn)` is the never-drop bucket: every raw note whose timestamp appears in no
    timeline is surfaced BY NAME with a reason, never dropped. A note with no op, or a note naming
    an op with no record, is unplaceable and lands here.
  * `day(conn, date)` regenerates a per-project daily digest. It is a VIEW over the record and
    never a source: it writes nothing, so a re-render under a fixed clock is byte-identical.

Sources: spec 5.9 (dumb capture then agentic sort), P-008 (`alpaca day` / `alpaca sort`); absorb-gap
AG-M24 (cadence, completeness surfacing, the never-drop unsorted bucket with a reason), AG-A1
(capture may never judge, the code-level refusal), AG-A2 (the per-op record model, the choices
log, the per-op cursor, provenance fields), AG-A18 (the day roll-up is a regenerable view).

Cadence (Step 2), stated here and in `doctrine/leaves/dumb-capture-agentic-sort.md` so the two
never drift: `alpaca sort` runs at session end and on demand, never as a background classifier. A day
whose notes are placed in no timeline is reported on the pad (see `pad_lines`) until it is cleared.
"""
from __future__ import annotations

from alpaca import db

# The cadence, as one string so the leaf, the pad block and the manual quote the same sentence.
CADENCE = ("sort at session end and on demand; a day with unsorted notes is reported on the pad "
           "until it is cleared")

# The event kinds the per-op record model reads its provenance fields off. Each is a fixed lookup,
# never a judgment about a specific event (the judgment is `alpaca sort`'s, and the owner's at close).
OP_OPEN = "op-open"            # carries the intent and the done bar (alpaca.ops)
AUTHORITY_BIND = "authority-bind"  # carries the authority id, level and scope (alpaca.posture.authority)
JUDGE = "intent-judge"        # carries who judged the bar met and on what basis (alpaca.intent)


def _events(conn):
    """Every event on the record, oldest first. The raw layer the historian reads; never a write."""
    return db.events(conn, limit=10 ** 9)


def _pointer(ev) -> str:
    """A stable pointer to a note on the record: its event id. Every surfaced note carries one, so
    nothing is ever referred to by position - a note is named, or it is not surfaced at all."""
    return "event:%s" % ev["id"]


def _known_ops(conn):
    """The set of op ids that have a record row. A timeline exists only for a known op; an event
    naming an op with no row is a dangling reference and is surfaced, never silently placed."""
    return {o["id"] for o in db.rows(conn, "ops", "1=1")}


def timeline(conn, op) -> dict:
    """The per-op record model for `op` (AG-A2). A pure derivation over the record: intent, done
    bar, authority, judgment basis and the choices log, each field with a pointer to its note.

    `choices` is the op's notes in timeline order (ts then id, deterministic under a fixed clock),
    each with its pointer. `pointers` is the flat set of note pointers this timeline places, which
    `unsorted` cross-checks against so nothing is dropped."""
    evs = [e for e in _events(conn) if (e.get("op") or "") == op]
    evs.sort(key=lambda e: (e.get("ts") or "", e["id"]))
    oprow = db.rows(conn, "ops", "id=?", (op,))

    intent = done_when = None
    intent_pointer = done_pointer = None
    authority = authority_pointer = None
    judgment = judgment_pointer = None
    choices = []
    for e in evs:
        data = e.get("data") or {}
        if e["kind"] == OP_OPEN and intent is None:
            intent = data.get("intent")
            done_when = data.get("done_when")
            intent_pointer = _pointer(e)
            done_pointer = _pointer(e)
        elif e["kind"] == AUTHORITY_BIND:
            authority = {"authority": data.get("authority"), "level": data.get("level"),
                         "scope": data.get("scope")}
            authority_pointer = _pointer(e)
        elif e["kind"] == JUDGE:
            judgment = {"judge": data.get("judge"), "basis": data.get("basis")}
            judgment_pointer = _pointer(e)
        choices.append({"pointer": _pointer(e), "ts": e.get("ts"), "kind": e["kind"],
                        "ref": e.get("ref"), "actor": e.get("actor")})

    # Fall back to the ops row for the intent and the done bar when no op-open note was captured, so
    # a timeline built from an imported op still carries its bar.
    if intent is None and oprow:
        intent = oprow[0].get("intent")
        done_when = oprow[0].get("done_when")

    cursor = None
    try:
        from alpaca import opindex
        cursor = opindex.cursor(conn, op)
    except Exception:
        cursor = None

    return {
        "op": op,
        "exists": bool(oprow),
        "intent": intent, "intent_pointer": intent_pointer,
        "done_when": done_when, "done_pointer": done_pointer,
        "authority": authority, "authority_pointer": authority_pointer,
        "judgment": judgment, "judgment_pointer": judgment_pointer,
        "choices": choices,
        "cursor": cursor,
        "pointers": [c["pointer"] for c in choices],
    }


def unsorted(conn, day=None) -> list:
    """The never-drop unsorted bucket (AG-M24). Every raw note whose timestamp appears in no
    timeline, surfaced by name with a reason. Optionally restricted to notes stamped on `day`
    (YYYY-MM-DD). Together with the timelines this partitions the record: placed notes plus
    unsorted notes are every note, so nothing is dropped.

    A note with no op is unplaceable ("no op"); a note naming an op with no record is a dangling
    reference ("dangling op <id>"). Either way it is returned, never removed."""
    placed = set()
    for op in _known_ops(conn):
        placed.update(timeline(conn, op)["pointers"])

    out = []
    for e in _events(conn):
        pointer = _pointer(e)
        if pointer in placed:
            continue
        if day and (e.get("ts") or "")[:10] != day:
            continue
        op = e.get("op") or ""
        if not op:
            reason = "no op: this note is on the record but attached to no op timeline"
        else:
            reason = "dangling op %s: this note names an op with no record" % op
        out.append({"pointer": pointer, "ts": e.get("ts"), "kind": e["kind"],
                    "session": e.get("session"), "op": op or None, "reason": reason})
    out.sort(key=lambda r: (r["ts"] or "", r["pointer"]))
    return out


def day(conn, date) -> dict:
    """The per-project daily digest for `date` (YYYY-MM-DD) (AG-A18). A pure VIEW over the record:
    it writes nothing, so it is a projection and never a source. Reports the op activity of the day
    and the never-drop unsorted bucket for the day. `render_day` turns it into byte-stable text."""
    evs = [e for e in _events(conn) if (e.get("ts") or "")[:10] == date]

    active = {}
    for e in evs:
        op = e.get("op") or ""
        if op:
            active[op] = active.get(op, 0) + 1

    un = unsorted(conn, day=date)
    ops_out = []
    for op in sorted(active):
        tl = timeline(conn, op)
        ops_out.append({
            "op": op,
            "intent": tl["intent"],
            "done_when": tl["done_when"],
            "events": active[op],
            "closed": bool(tl["judgment"]),
        })

    return {
        "date": date,
        "ops": ops_out,
        "unsorted": un,
        "totals": {"events": len(evs), "ops": len(ops_out), "unsorted": len(un)},
    }


def render_day(digest) -> str:
    """The daily digest as text. Reads only the digest (whose stamps come from the record's own
    clock), never the wall clock, so a re-render is byte-identical under a fixed clock. Every
    externally sourced value crosses `render.cell`, so a hostile note cannot break a line."""
    from alpaca import render as _render
    d = digest
    t = d["totals"]
    lines = ["# Day %s" % d["date"], ""]
    lines.append("ops: %d | events: %d | unsorted: %d" % (t["ops"], t["events"], t["unsorted"]))
    lines += ["", "## Ops", ""]
    if d["ops"]:
        for o in d["ops"]:
            state = "closed" if o["closed"] else "open"
            lines.append("- %s [%s] %s (%d events)" % (
                o["op"], state, _render.cell(o["intent"] if o["intent"] else "(no intent)"),
                o["events"]))
    else:
        lines.append("(no op activity)")
    lines += ["", "## Unsorted", ""]
    if d["unsorted"]:
        lines.append("%d note(s) placed in no timeline, surfaced not dropped:" % len(d["unsorted"]))
        for u in d["unsorted"]:
            lines.append("- %s %s %s: %s" % (
                u["pointer"], u["ts"] or "-", _render.cell(u["kind"] or "-"),
                _render.cell(u["reason"])))
    else:
        lines.append("(none: every note is placed in a timeline)")
    return "\n".join(lines) + "\n"


def pad_lines(conn, date=None) -> list:
    """The pad block naming every unsorted note, empty when there is none (Step 2). Shares the
    shape of the other pad blocks (`resolve.pad_lines`, `opindex`), so the operator reads one
    surface. Reported until the notes are cleared: an empty bucket returns no block."""
    un = unsorted(conn, day=date)
    if not un:
        return []
    from alpaca import render as _render
    lines = ["## Unsorted notes: %d" % len(un), "",
             "These notes are on the record but placed in no op timeline. They are surfaced, "
             "never dropped; sort or attach them to clear this.", ""]
    for u in un:
        lines.append("- %s %s (%s)" % (u["pointer"], _render.cell(u["reason"]), u["ts"] or "-"))
    return lines
