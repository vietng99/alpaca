"""Append-only supersession over Alpaca obligation rows (M1.13).

Ported from the earlier harness gates/supersession.py, MINUS the signature guard. The core
clause is kept whole: a row correction is never an edit. A committed row is FROZEN; to
correct it you APPEND a NEW row that CITES the superseded row's id through `supersedes`,
and the gate then treats the superseding row as authoritative for that id. The frozen
original stays in the store as history, never edited. Each committed row carries a freeze
witness (`content_hash`) folded into the chain, so editing a frozen row's content in
place is drift and HALTs on the next verify; appending a superseding row is not.

Adaptations for Alpaca, per the plan (M1.13 Step 3, "port supersession minus the signature
guard; keep cite-and-freeze"):

  * No signatures. The earlier harness refused an unsigned superseding row and required a sigkey mark
    bound to the row's exact content, a marks store and a distinct-signer check. That
    whole guard is gone. What this instrument tracks is AUTHORITY (which row is current),
    not authenticity (who wrote it). The one write path a correction takes is: it must
    CITE the superseded id, and it is frozen on commit.
  * The freeze witness is `synthesis`'s content hash, referenced not restated, so a
    superseding row and a synthesized obligation row are frozen exactly the same way and
    a drift on one is a drift on the other.
  * The earlier harness war-log file journal becomes a list of row dicts; in Alpaca the rows table is
    the store, and the DB-backed forward walk (`head`) reads the stored `content_hash`
    and `supersedes` columns without re-deriving the witness (a DB row does not carry the
    synthesis-time `item`/`artifact` fields). `verify_chain` is the in-memory check that
    RE-DERIVES each freeze witness to catch an in-place edit of a frozen row.

Refusals surface through `alpaca.checklist.Halt` with a verdict-band code; a drift is BLOCKED,
never laundered into a pass.
"""
from __future__ import annotations

from alpaca.checklist import Halt, synthesis
from alpaca.gates import verdict as vc

#: the genesis link, shared with synthesis so the two chain the same way.
GENESIS = synthesis.GENESIS

# reason tokens controls bind to
R_FROZEN_HALT = "SUPERSESSION-EDITS-FROZEN-HALT"
R_CHAIN_DRIFT = "SUPERSESSION-CHAIN-DRIFT"
R_NO_SUCH_ROW = "SUPERSESSION-NO-SUCH-ROW"
R_MISCITED = "SUPERSESSION-MISCITED"
R_ROW_MALFORMED = "SUPERSESSION-ROW-MALFORMED"


def freeze_hash(row: dict) -> str:
    """The freeze witness for a row: the content hash over exactly the load-bearing fields,
    computed by the ONE definition in `synthesis` (never restated here). The citation
    (`supersedes`) is one of those fields, so a freeze witness binds to what the row
    supersedes: moving the citation changes the witness and is detected as drift."""
    return synthesis._content_hash(row)


def is_frozen(row: dict) -> bool:
    """True iff `row` carries a self-consistent freeze witness: its `content_hash` equals the
    re-derived witness. A shape check on a detached row object, not proof it is on the store."""
    if not isinstance(row, dict) or not row.get("content_hash"):
        return False
    return row.get("content_hash") == freeze_hash(row)


def _commit(rows: list, new_row: dict) -> dict:
    """Freeze `new_row` onto the tail of `rows`: recompute its freeze witness and link it to
    the previous row's witness. Returns the frozen row. The chain fields the caller may have
    passed are dropped and recomputed, so a caller cannot forge the freeze anchor."""
    if not isinstance(new_row, dict):
        raise Halt(vc.BLOCKED, R_ROW_MALFORMED, "a row must be a dict, got %s"
                   % type(new_row).__name__)
    frozen = dict(new_row)
    frozen.pop("content_hash", None)
    frozen.pop("prev_hash", None)
    frozen["prev_hash"] = rows[-1]["content_hash"] if rows else GENESIS
    frozen["content_hash"] = freeze_hash(frozen)
    return frozen


def verify_chain(rows: list) -> list:
    """Recompute the whole in-memory chain. Returns the rows in order. HALTS on the FIRST sign
    of drift, and the token distinguishes the two realistic tampers: editing a FROZEN row's
    content in place is R_FROZEN_HALT; a broken link (a row inserted, removed or reordered) is
    R_CHAIN_DRIFT. An empty store is head=genesis, zero rows (never a HALT on its own)."""
    prev = GENESIS
    for idx, row in enumerate(rows):
        n = idx + 1
        if not isinstance(row, dict):
            raise Halt(vc.BLOCKED, R_CHAIN_DRIFT, "row %d is not a dict" % n)
        if row.get("content_hash") != freeze_hash(row):
            raise Halt(vc.BLOCKED, R_FROZEN_HALT,
                       "row %d (%r) was edited in place; a correction is a NEW row citing the "
                       "superseded id, never an edit of the frozen original"
                       % (n, row.get("id")))
        if row.get("prev_hash") != prev:
            raise Halt(vc.BLOCKED, R_CHAIN_DRIFT,
                       "row %d prev_hash=%s, chain expected %s (a row was inserted, removed or "
                       "reordered)" % (n, str(row.get("prev_hash"))[:16], str(prev)[:16]))
        prev = row["content_hash"]
    return list(rows)


def _index(rows: list) -> dict:
    by_id = {}
    for r in rows:
        rid = r.get("id")
        if rid is not None:
            by_id.setdefault(rid, r)          # first-writer wins as the id's ORIGINAL anchor
    return by_id


def head(rows: list, row_id):
    """The authoritative row for `row_id`: follow supersession citations forward to the head of
    the chain, WITHOUT re-deriving any freeze witness. Returns None when the id was never
    committed. This is the pure citation-graph walk shared by the DB-backed discharge fold and
    by `resolve`; it reads only `id` and `supersedes`, so it works on a DB row that no longer
    carries the synthesis-time fields."""
    by_id = _index(rows)
    if row_id not in by_id:
        return None
    current = by_id[row_id]
    seen = {current.get("id")}
    while True:
        successors = [r for r in rows if r.get("supersedes") == current.get("id")]
        if not successors:
            return current
        nxt = successors[-1]                   # the LAST-appended successor is authoritative
        if nxt.get("id") in seen:              # cycle guard, never loop
            return current
        seen.add(nxt.get("id"))
        current = nxt


def resolve(rows: list, row_id):
    """The LATEST authoritative row for `row_id`, after verifying the chain (HALT on drift
    before adjudicating). Raises R_NO_SUCH_ROW if the id was never committed. Superseded rows
    remain in the store, see `history`."""
    verify_chain(rows)
    current = head(rows, row_id)
    if current is None:
        raise Halt(vc.BLOCKED, R_NO_SUCH_ROW, "no row with id=%r has been committed" % row_id)
    return current


def history(rows: list, row_id) -> list:
    """The full supersession lineage for `row_id`, oldest first: the original and every row that
    transitively supersedes it. Proves the frozen originals are still on the store."""
    verify_chain(rows)
    by_id = _index(rows)
    if row_id not in by_id:
        raise Halt(vc.BLOCKED, R_NO_SUCH_ROW, "no row with id=%r" % row_id)
    lineage = [by_id[row_id]]
    seen = {by_id[row_id].get("id")}
    cur = by_id[row_id]
    while True:
        successors = [r for r in rows if r.get("supersedes") == cur.get("id")]
        if not successors:
            return lineage
        nxt = successors[-1]
        if nxt.get("id") in seen:
            return lineage
        seen.add(nxt.get("id"))
        lineage.append(nxt)
        cur = nxt


def append_row(rows: list, new_row: dict) -> list:
    """Commit a fresh original row to the store, freezing it. Returns a NEW list with the frozen
    row appended. The chain is verified first, so you never append onto drift."""
    verify_chain(rows)
    return list(rows) + [_commit(rows, new_row)]


def supersede(rows: list, old_row_id, new_row: dict) -> list:
    """Append `new_row` as the authoritative correction of `old_row_id`. Returns a NEW list with
    the frozen superseding row appended; the frozen original is NEVER touched.

    Guards, fail-closed, each an exact token a control binds to:
      1. the store verifies (HALT on drift)          -> R_FROZEN_HALT / R_CHAIN_DRIFT
      2. old_row_id must exist                        -> R_NO_SUCH_ROW
      3. new_row must CITE old_row_id via supersedes  -> R_MISCITED
      4. commit (freeze) and return the new store.

    The signature guard the earlier harness placed between (3) and (4) is dropped: cite-and-freeze is the
    whole contract now."""
    verify_chain(rows)                                            # (1)
    if old_row_id not in _index(rows):                           # (2)
        raise Halt(vc.BLOCKED, R_NO_SUCH_ROW,
                   "cannot supersede %r: it was never committed" % old_row_id)
    if not isinstance(new_row, dict):
        raise Halt(vc.BLOCKED, R_ROW_MALFORMED, "the superseding row must be a dict")
    cited = new_row.get("supersedes")
    if cited != old_row_id:                                       # (3)
        raise Halt(vc.BLOCKED, R_MISCITED,
                   "a supersession row must cite the superseded id: supersedes=%r, expected %r"
                   % (cited, old_row_id))
    return list(rows) + [_commit(rows, new_row)]                 # (4)
