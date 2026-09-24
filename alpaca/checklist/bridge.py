"""The bridge (R1): land synthesized obligation rows in the record's `rows` store.

Ported from the earlier harness gates/checklist_bridge.py and adapted for Alpaca.

WHY THIS EXISTS
---------------
`synthesis.synthesize` derives one obligation row per (step, item) but does not write it
anywhere; on the earlier harness production path the derived rows were a DEAD LETTER, emitted to a
file that nothing consumed. This module is the missing consumer: it lands every derived row
in the `rows` table, then RE-DERIVES coverage from the store on disk so a PASS is
conditional on the store actually carrying every derived obligation with matching content.

Adaptations from the earlier harness, per the plan (M1.11):

  * The SIGN / signature fields never exist and there is no signature guard; an Alpaca row
    carries the generic schema only.
  * The war-log file becomes the record's `events` table: the batch is landed through
    `db.transaction` with a single `bridge` event, so the event and the rows commit
    together or not at all (write-ahead, one transaction per mutation).
  * Cite-and-freeze is kept: a row already in the store with the SAME id but DIFFERENT
    content is a drift, and a frozen row is never edited in place. The drift is BLOCKED and
    surfaced as a supersession correction the caller must author, never a silent overwrite.

CONTENT-AWARENESS
-----------------
`synthesis.row_id` digests only (step, artifact-path, item-key, artifact-sha), NOT the
obligation text or context. So a re-derivation with corrected wording yields the SAME row
id with DIFFERENT content; an id-only skip would silently leave the store holding stale
content while PASSing. The bridge therefore compares the CONTENT hash: it recomputes the
incoming row's content_hash from its real fields (never trusting the row's self-declared
`content_hash`), and compares it against the store row's recorded content_hash. Idempotent
when they match; BLOCKED when they drift.

The bridge is crash-only re-runnable: a re-run over the same rows appends nothing, writes
no event, and PASSes with `appended == 0`.
"""
from __future__ import annotations

from alpaca import db
from alpaca.checklist import synthesis
from alpaca.gates import contract, verdict

INSTRUMENT = "checklist-bridge"

#: the fields an incoming row must carry to be landed and content-hashed. These are exactly
#: the content fields synthesis hashes, so a row missing one cannot be silently landed with
#: a degraded content hash.
_REQUIRED = synthesis._CONTENT_FIELDS

#: how a synthesized row's keys map onto the `rows` table columns. `where`/`when` are SQL
#: keywords, stored as `where_`/`when_`; `item` and `artifact` are folded into the row id
#: and its content hash, so they are not separate columns.
_DB_COLUMNS = ("id", "kind", "op", "phase", "step", "statement", "proof", "where_", "how",
               "when_", "why", "session", "operator", "status", "tag", "content_hash",
               "prev_hash", "supersedes")


def _finding(code: str, detail: str, vcode: int) -> dict:
    return {"verdict": vcode, "code": code, "detail": detail}


def _result(vcode: int, findings: list, *, appended: int, derived: int,
            store_row_count: int) -> dict:
    return {"verdict": vcode, "findings": findings, "appended": appended,
            "derived": derived, "committed": vcode == verdict.PASS,
            "store_row_count": store_row_count}


def _db_row(row: dict, content_hash: str) -> dict:
    """Project a synthesized row onto the `rows` table columns, using the recomputed
    content hash rather than the row's self-declared one."""
    return {"id": row["id"], "kind": row["kind"], "op": row.get("op"),
            "phase": row.get("phase"), "step": row.get("step"),
            "statement": row.get("statement"), "proof": row.get("proof"),
            "where_": row.get("where", ""), "how": row.get("how", ""),
            "when_": row.get("when", ""), "why": row.get("why", ""),
            "session": row.get("session"), "operator": row.get("operator"),
            "status": row.get("status", "open"), "tag": row.get("tag"),
            "content_hash": content_hash, "prev_hash": row.get("prev_hash"),
            "supersedes": row.get("supersedes")}


def apply(conn, rows, *, session=None, actor="bridge", op=None) -> dict:
    """Land every derived row in the `rows` store, idempotently and content-aware.

    Returns {"verdict", "findings", "appended", "derived", "committed", "store_row_count"}.
    Nothing is written unless the whole batch is clean: an empty batch, a malformed row, a
    duplicated id, or a content drift on an existing id refuses the WHOLE batch (atomic), so
    a defective batch never leaves a partial store. On a clean batch the absent rows are
    landed through `db.transaction` with a single `bridge` event.
    """
    store_count = conn.execute("SELECT COUNT(*) FROM rows").fetchone()[0]

    if not rows:
        return _result(verdict.BLOCKED,
                       [_finding("BRIDGE-ROWS-EMPTY",
                                 "no derived row to land; an empty derivation is never a pass",
                                 verdict.BLOCKED)],
                       appended=0, derived=0, store_row_count=store_count)

    findings = []
    incoming = []            # (row_id, expected_content_hash, row)
    seen = {}
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            findings.append(_finding("BRIDGE-ROW-MALFORMED",
                                     "row %d is not an object" % i, verdict.FAIL))
            continue
        missing = [k for k in _REQUIRED if k not in row]
        if missing:
            findings.append(_finding("BRIDGE-ROW-MALFORMED",
                                     "row %d is missing %r" % (i, missing), verdict.FAIL))
            continue
        rid = row["id"]
        if rid in seen:
            findings.append(_finding("BRIDGE-DUPLICATE-ROW-ID",
                                     "row id %r appears more than once; the derivation is not a "
                                     "partition" % rid, verdict.FAIL))
            continue
        seen[rid] = i
        # recompute the content hash from the row's real fields; never trust the field.
        incoming.append((rid, synthesis._content_hash(row), row))

    if findings:
        return _result(contract.worst([f["verdict"] for f in findings]), findings,
                       appended=0, derived=len(rows), store_row_count=store_count)

    # classify against the store BEFORE any write (no partial batch on a drift).
    to_append = []
    from alpaca import lineage
    lin = lineage.read(conn)
    numbers = lineage.row_numbers(conn) if lin else {}

    def _stored_matches(stored_hash, rid, row, expected):
        # a row carried from an earlier harness keeps the hash that harness froze it with.
        if stored_hash == expected:
            return True
        tag = lineage.obligation_tag(lin, numbers.get(rid))
        return tag is not None and stored_hash == synthesis._content_hash(row, tag)

    for rid, expected, row in incoming:
        existing = db.rows(conn, "rows", "id=?", (rid,))
        if existing:
            stored_hash = existing[0]["content_hash"]
            if not _stored_matches(stored_hash, rid, row, expected):
                findings.append(_finding(
                    "BRIDGE-CONTENT-DRIFT",
                    "store row %s is present but its content differs from this derivation "
                    "(stored %s, derived %s); a frozen row is never edited in place -- surface "
                    "the correction as a supersession, never a silent pass"
                    % (rid, (stored_hash or "?")[:12], expected[:12]), verdict.BLOCKED))
            # else: present with matching content -> idempotent, nothing to do.
        else:
            to_append.append((rid, expected, row))

    if findings:
        # a drift refuses the whole batch; nothing lands.
        return _result(contract.worst([f["verdict"] for f in findings]), findings,
                       appended=0, derived=len(incoming), store_row_count=store_count)

    appended = 0
    if to_append:
        with db.transaction(conn):
            db.append_event(conn, session=session or "bridge", actor=actor, kind="bridge",
                            op=op, ref=INSTRUMENT,
                            data={"instrument": INSTRUMENT, "appended": len(to_append),
                                  "ids": [rid for rid, _e, _r in to_append]},
                            conn_in_txn=True)
            for rid, expected, row in to_append:
                dbr = _db_row(row, expected)
                conn.execute(
                    "INSERT INTO rows (%s) VALUES (%s)"
                    % (",".join(_DB_COLUMNS), ",".join("?" * len(_DB_COLUMNS))),
                    [dbr[c] for c in _DB_COLUMNS])
            appended = len(to_append)

    # RE-DERIVE coverage from the store on disk: every derived row must be present AND carry
    # matching content. Never trust the append return value.
    store_count = conn.execute("SELECT COUNT(*) FROM rows").fetchone()[0]
    for rid, expected, row in incoming:
        existing = db.rows(conn, "rows", "id=?", (rid,))
        if not existing:
            findings.append(_finding("BRIDGE-STORE-MISSING-DERIVED-ROW",
                                     "the store does not carry derived row %s after the bridge" % rid,
                                     verdict.FAIL))
        elif not _stored_matches(existing[0]["content_hash"], rid, row, expected):
            findings.append(_finding("BRIDGE-CONTENT-DRIFT",
                                     "store row %s content differs from the derivation after the "
                                     "bridge" % rid, verdict.FAIL))

    if findings:
        return _result(contract.worst([f["verdict"] for f in findings]), findings,
                       appended=appended, derived=len(incoming), store_row_count=store_count)

    return _result(verdict.PASS, [], appended=appended, derived=len(incoming),
                   store_row_count=store_count)
