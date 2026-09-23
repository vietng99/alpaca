"""Verdict rows and the discharge fold (M1.13).

A discharge is not a status-column flip; it is a machine-authored VERDICT ROW appended to
the record (the events table), binding a verdict to the exact frozen content of one
obligation row. This is section 6 REBUILD row 43, sign_row -> verdict rows: the earlier harness sign
fields and signature guards are gone, the freeze witness the sign row carried is kept.

A verdict CITES the obligation's `content_hash`. If that hash is stale (the obligation was
superseded or otherwise drifted since the instrument ran) the discharge HALTs with the
drift reason rather than certifying a claim the current row never made. This is the same
cite-and-freeze the supersession port keeps, read from the discharge side.

Interface (M1.13):

    discharge(conn, row_id, content_hash, instrument, verdict, evidence, level, session)
        author a PASS / FAIL / BLOCKED verdict row bound to (row_id, content_hash).
    waive(conn, row_id, content_hash, reason, level, session)
        author a waiver row; a waiver REQUIRES a reason AND a level.
    status_fold(conn, row_id) -> open | discharged | failed | waived | blocked
        the row's current state, folded from its verdict rows LATEST-WINS, so a later
        failed verdict reopens a discharged row.

The fold is derived from events, never from a stored status word, so the column can never
be a claim that outlives its evidence.
"""
from __future__ import annotations

from alpaca import db
from alpaca.checklist import Halt, supersession
from alpaca.gates import verdict as vc

KIND = "verdict"

#: the verdict value a waiver row carries in place of an integer verdict-band code.
WAIVED = "waived"

# reason tokens
R_NO_SUCH_ROW = "VERDICT-NO-SUCH-ROW"
R_DRIFT = "VERDICT-BINDS-STALE-CONTENT"
R_CODE_UNKNOWN = "VERDICT-CODE-UNKNOWN"
R_WAIVER_NO_REASON = "WAIVER-REQUIRES-REASON"
R_WAIVER_NO_LEVEL = "WAIVER-REQUIRES-LEVEL"

# the folded status strings status_fold returns
OPEN = "open"
DISCHARGED = "discharged"
FAILED = "failed"
WAIVED_STATUS = "waived"
BLOCKED_STATUS = "blocked"


def _all_rows(conn) -> list:
    return db.rows(conn, "rows", "1=1")


def _bind_or_halt(conn, row_id, content_hash) -> dict:
    """Resolve the authoritative row for `row_id` and refuse a stale binding.

    The authoritative row is the head of the supersession chain (`supersession.head`, the
    pure citation-forward walk over the DB rows). A verdict must cite that row's current
    `content_hash`; a mismatch is a stale binding and HALTs with the drift reason."""
    rows = _all_rows(conn)
    current = supersession.head(rows, row_id)
    if current is None:
        raise Halt(vc.BLOCKED, R_NO_SUCH_ROW, "no obligation row with id=%r" % row_id)
    frozen = current.get("content_hash")
    if content_hash != frozen:
        raise Halt(
            vc.BLOCKED, R_DRIFT,
            "a verdict binds %r to content_hash %s, but the authoritative row %r is frozen at "
            "%s: the obligation drifted or was superseded, so this verdict is stale. A correction "
            "is a new row citing supersedes; re-run the instrument against the current content."
            % (row_id, str(content_hash)[:16], current.get("id"), str(frozen)[:16]))
    return current


def _author(conn, row_id, current, instrument, verdict_value, evidence, level, session,
            reason=None) -> dict:
    data = {
        "kind": KIND,
        "binds": {"row_id": row_id, "content_hash": current.get("content_hash")},
        "verdict": verdict_value,
        "verdict_name": WAIVED.upper() if verdict_value == WAIVED else vc.name_of(verdict_value),
        "instrument": instrument,
        "evidence": list(evidence or []),
        "level": level,
        "session": session,
    }
    if reason is not None:
        data["reason"] = reason
    ev = db.append_event(conn, session=session or "instrument",
                         actor=instrument or "instrument", kind=KIND,
                         op=current.get("op"), ref=row_id, data=data)
    _recompute_tag(conn, row_id, session)
    return ev


def _recompute_tag(conn, row_id, session) -> None:
    """Recompute the row's derived `tag` column from its verdict rows (M1.16 Step 3).

    A verdict row is the only thing that changes the evidence behind a tag, so the tag oracle
    is re-run here on every discharge and waiver: the column is a fold of the record, never a
    claim that outlives its evidence. Imported inside the function because the oracle imports
    this module (the fold and the derivation are two sides of one relation). The recompute never
    turns a valid discharge into a crash: a tag it cannot derive is left as it was."""
    from alpaca.gates import honest_tag_oracle
    try:
        honest_tag_oracle.derive(conn, row_id, session=session)
    except Exception:
        pass


def discharge(conn, row_id, content_hash, instrument, verdict, evidence, level, session,
              reason=None) -> dict:
    """Author a verdict row bound to (row_id, content_hash). `verdict` is a verdict-band code
    (PASS / FAIL / BLOCKED / PAUSED). A verdict citing a stale content hash HALTs (drift); an
    unknown code is refused rather than laundered. An optional `reason` rides on the verdict
    row so the board can show why a row is blocked. Returns the appended event row."""
    if verdict not in vc.VERDICT_BAND:
        raise Halt(vc.BLOCKED, R_CODE_UNKNOWN,
                   "%r is not a verdict-band code" % (verdict,))
    current = _bind_or_halt(conn, row_id, content_hash)
    return _author(conn, row_id, current, instrument, verdict, evidence, level, session,
                   reason=reason)


def waive(conn, row_id, content_hash, reason, level, session) -> dict:
    """Author a waiver row bound to (row_id, content_hash). A waiver is a deliberate decision to
    not discharge, so it REQUIRES a reason AND a level; either missing is refused. The same
    stale-binding drift check applies. Returns the appended event row."""
    if not reason or not str(reason).strip():
        raise Halt(vc.BLOCKED, R_WAIVER_NO_REASON,
                   "a waiver must carry a reason; a bare waiver is a silent skip")
    if level is None or not str(level).strip():
        raise Halt(vc.BLOCKED, R_WAIVER_NO_LEVEL,
                   "a waiver must carry the level it was taken at")
    current = _bind_or_halt(conn, row_id, content_hash)
    return _author(conn, row_id, current, "waiver", WAIVED, [], level, session, reason=reason)


def _verdict_events(conn, row_id) -> list:
    """Every verdict row bound to `row_id`, in append order (oldest first)."""
    out = []
    for e in db.events(conn, kind=KIND, limit=10 ** 9):
        binds = e["data"].get("binds") or {}
        if binds.get("row_id") == row_id:
            out.append(e)
    return out


def verdict_index(conn) -> dict:
    """Every verdict row grouped by the obligation it binds, in append order. One scan of the
    event log, so a view over many rows folds them without re-reading the log per row."""
    out = {}
    for e in db.events(conn, kind=KIND, limit=10 ** 9):
        rid = (e["data"].get("binds") or {}).get("row_id")
        if rid is not None:
            out.setdefault(rid, []).append(e)
    return out


def status_fold(conn, row_id, index=None) -> str:
    """Fold a row's verdict rows into its current state, LATEST-WINS.

    open        no verdict row binds this obligation yet.
    discharged  the latest verdict is PASS.
    failed      the latest verdict is FAIL (a later failed verdict reopens a discharge).
    waived      the latest verdict is a waiver.
    blocked     the latest verdict is BLOCKED (or any other non-pass band).
    """
    evs = index.get(row_id, []) if index is not None else _verdict_events(conn, row_id)
    if not evs:
        return OPEN
    latest = evs[-1]["data"].get("verdict")
    if latest == WAIVED:
        return WAIVED_STATUS
    if latest == vc.PASS:
        return DISCHARGED
    if latest == vc.FAIL:
        return FAILED
    return BLOCKED_STATUS
