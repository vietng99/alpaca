"""The resolve log and the resolve pass: serial-with-resolve, made a record (M4.5).

Sources: spec 5.4:345-346 (serial-with-resolve), 5.7:460-462 (claims detect the collision);
absorb-gap AG-M23 (a named artifact with typed events and a pass that consumes it), AG-A4
(the claim and the first beat land before the first mutation; a hard lock is refused, and why).
The doctrine leaf is `doctrine/leaves/serial-with-resolve.md`: the code is the mechanism, the leaf
is the reason, so the two never drift.

The named artifact is the RESOLVE LOG. It is not a file: it lives on the same append-only,
hash-chained record as everything else, under one event kind (`resolve-log`), typed by a `kind`
field in the event data. The three typed kinds are:

    claim               a live claim was present when a second writer peeked. Written by the
                        claim path (alpaca/claims.py) so the log shows the claim a second session
                        saw in the gap, rather than nothing.
    concurrent-detected two writers landed on one reference. This is the collision, recorded, for
                        the resolve pass to consume. It is never a resolution.
    resolved            the resolve pass reconstructed the divergence against a snapshot and
                        recorded the outcome. Only the pass writes this kind.

Serial-with-resolve, not a hard lock. A claim is advisory: it is a note on the record, taken
before the first mutation, not an operating-system lock over the reference. A hard lock is refused
because a held lock outlives the process that took it (a crashed worker leaves the reference
wedged with no live holder to break it), it cannot be reconstructed from the append-only record
after a restart, and it stops honest work rather than recording a collision to resolve. The
softness is a decision on the record (the leaf), not an omission: two writers are allowed to
collide, the collision is recorded, and the resolve pass reconstructs it afterwards.

The resolve pass (`pass_`) reads every `concurrent-detected` entry that no `resolved` entry yet
answers, and for each one reconstructs the divergence AGAINST THE SNAPSHOT: the base value the two
writers diverged from. With a snapshot it can see what each writer changed and record a `resolved`
entry that keeps both divergences visible (nothing is dropped in silence). WITHOUT a snapshot it
cannot know the common ancestor, so a merge would be a blind clobber; it refuses to merge and
SURFACES the collision instead. No collision is ever silently merged. The unresolved count is a
read over the record (`unresolved`), surfaced by `alpaca doctor` and on the pad, so a collision that
was logged and never resolved cannot hide.
"""
from __future__ import annotations

from alpaca import db

#: the one event kind the resolve log rides on; the typed kind lives in the event data.
LOG_KIND = "resolve-log"

#: the three typed kinds carried in the event data. `claim` and `concurrent-detected` are written
#: by the claim path on a collision; `resolved` is written only by the resolve pass.
CLAIM = "claim"
CONCURRENT = "concurrent-detected"
RESOLVED = "resolved"
KINDS = (CLAIM, CONCURRENT, RESOLVED)

#: how a resolved entry names the outcome. Serial: the writers are ordered, the later one is the
#: winner, and every writer's divergence from the base is kept so nothing is merged in silence.
RESOLUTION = "serial-with-resolve"


class ResolveError(Exception):
    """A resolve-log operation that cannot proceed (an unknown typed kind, chiefly)."""


def log(conn, kind, ref, detail, *, session="alpaca", actor="alpaca", now=None):
    """Append one typed entry to the resolve log. `kind` must be one of KINDS; an unknown kind is
    refused rather than laundered into the record. `detail` is any JSON-able payload. `now` stamps
    the entry from an injected clock (a FixedClock under test); None uses the process clock."""
    if kind not in KINDS:
        raise ResolveError(
            "unknown resolve log kind %r; expected one of %s" % (kind, ", ".join(KINDS)))
    clock = (lambda: now) if now is not None else None
    data = {"kind": kind, "ref": ref, "detail": detail}
    ev = dict(db.append_event(
        conn, session=session, actor=actor, kind=LOG_KIND, ref=ref, data=data, clock=clock))
    ev["data"] = data   # the raw row carries data as a JSON string; hand back the parsed dict
    return ev


def entries(conn, *, kind=None, ref=None):
    """Every resolve-log entry, oldest first, optionally filtered by typed `kind` and/or `ref`."""
    out = []
    for e in db.events(conn, kind=LOG_KIND, limit=10 ** 9):
        d = e["data"]
        if kind is not None and d.get("kind") != kind:
            continue
        if ref is not None and e["ref"] != ref:
            continue
        out.append(e)
    return out


def _detail_of(e):
    d = e["data"].get("detail")
    return d if isinstance(d, dict) else {}


def _writers_of(detail):
    """The writers a concurrent-detected entry names, as [{worker, value}, ...]. A `writers` list
    is used verbatim; otherwise an incumbent/challenger pair is folded into the same shape."""
    ws = detail.get("writers")
    if isinstance(ws, list):
        return ws
    out = []
    if detail.get("incumbent") is not None:
        out.append({"worker": detail.get("incumbent"), "value": detail.get("incumbent_value")})
    if detail.get("challenger") is not None:
        out.append({"worker": detail.get("challenger"), "value": detail.get("challenger_value")})
    return out


def _resolved_sources(conn):
    """The set of concurrent-detected event ids that a `resolved` entry already answers."""
    out = set()
    for e in entries(conn, kind=RESOLVED):
        src = _detail_of(e).get("source")
        if src is not None:
            out.add(src)
    return out


def unresolved(conn):
    """Every `concurrent-detected` entry with no matching `resolved` entry, oldest first. This is
    the read the pad and `alpaca doctor` project the unresolved count from; it never writes."""
    answered = _resolved_sources(conn)
    return [e for e in entries(conn, kind=CONCURRENT) if e["id"] not in answered]


def unresolved_count(conn):
    """How many collisions are logged and not yet resolved."""
    return len(unresolved(conn))


def _base_for(ref, detail, snapshots):
    """The snapshot (base) the two writers diverged from, or None when none is available. A
    `snapshots` mapping ref->base wins; otherwise a `base` carried in the entry detail is used."""
    if snapshots and ref in snapshots:
        return snapshots[ref]
    return detail.get("base")


def pass_(conn, snapshots=None, *, now=None):
    """The resolve pass. Walk every unresolved `concurrent-detected` entry and, for each, try to
    reconstruct the divergence against the snapshot:

      * snapshot available -> reconstruct what each writer changed from the base and record a
        `resolved` entry (serial: the later writer wins, every writer's divergence is kept, so the
        collision is resolved on the record, never merged in silence).
      * no snapshot -> refuse to merge. The collision is SURFACED (returned, and reported by the
        pad and the doctor), not written as resolved. A blind merge is never performed.

    Idempotent: a second pass writes nothing new for a collision already resolved, and re-surfaces
    an unrecoverable one without writing. Returns {resolved, unrecoverable, unresolved}."""
    resolved, unrecoverable = [], []
    for e in unresolved(conn):
        ref = e["ref"]
        detail = _detail_of(e)
        writers = _writers_of(detail)
        base = _base_for(ref, detail, snapshots)
        if base is None:
            unrecoverable.append({"event": e["id"], "ref": ref, "writers": writers,
                                  "reason": "no-snapshot"})
            continue
        divergence = [{"worker": w.get("worker"), "base": base, "value": w.get("value"),
                       "changed": w.get("value") != base} for w in writers]
        winner = writers[-1].get("worker") if writers else None
        detail_out = {"ref": ref, "source": e["id"], "base": base, "writers": writers,
                      "divergence": divergence, "resolution": RESOLUTION, "winner": winner}
        ev = log(conn, RESOLVED, ref, detail_out, now=now)
        resolved.append({"event": ev["id"], "ref": ref, "detail": detail_out})
    return {"resolved": resolved, "unrecoverable": unrecoverable,
            "unresolved": len(unrecoverable)}


def pad_lines(conn):
    """The pad block naming every unresolved collision, empty when there is none. Shared shape with
    the other pad blocks so the operator reads one surface. Reports the count Step 4 requires."""
    un = unresolved(conn)
    if not un:
        return []
    lines = ["## Unresolved collisions: %d" % len(un), "",
             "A concurrent write was detected and not yet resolved. Run the resolve pass; a "
             "collision with no snapshot is surfaced, never merged.", ""]
    for e in un:
        ws = ", ".join(str(w.get("worker")) for w in _writers_of(_detail_of(e))) or "-"
        lines.append("- ref %s writers %s (detected %s)" % (e["ref"], ws, e["ts"]))
    return lines
