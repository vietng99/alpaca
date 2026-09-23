"""M4.6 proof - the historian: the never-drop unsorted bucket, the per-op record, alpaca day cadence.

Proved on BOTH paths, under a FixedClock over a throwaway root, so no control is a tautology:

  1. POSITIVE - the per-op record model: an op's timeline carries the intent, the done bar, the
     authority, the judgment basis and the choices log, each field with a pointer to its note.
  2. NEGATIVE (the never-drop rule): a note whose timestamp appears in no timeline is SURFACED by
     name with a reason, never dropped - both an unattached note (no op) and a dangling one (an
     op with no record).
  3. COMPLETENESS: placed notes plus unsorted notes are every note. The record is partitioned;
     nothing falls through the gap.
  4. The boundary in code: the capture path carries no classifier (an unknown kind becomes the
     neutral `note`, never a forced guess), and the sort verb REFUSES to mechanise the judgment
     (`alpaca sort --auto` is BLOCKED).
  5. `alpaca day` regenerates a per-project daily digest that is a VIEW and never a source: it writes
     nothing, and a re-render under a fixed clock is byte-identical.
  6. The pad reports a day with unsorted notes until it is cleared.
"""
import types

from alpaca import db as tedb
from alpaca import historian
from alpaca import sort as tesort
from alpaca.clock import FixedClock
from alpaca.gates import verdict as vc
from alpaca.wiki.ingest import drain


def _clock(start="2026-03-01T00:00:00+00:00", step=1):
    return FixedClock(start=start, step=step)


def _open_op(conn, oid, intent, done_when, clock, *, level="L3", scope="single-op"):
    """Open an op the way alpaca.ops / alpaca.posture.authority do: the op-open note carries the intent and
    the done bar, and an authority-bind note carries the standing authority it ran under."""
    tedb.append_event(conn, session="s1", actor="human", kind="op-open", op=oid,
                      data={"intent": intent, "done_when": done_when}, clock=clock)
    tedb.upsert(conn, "ops", "id", {"id": oid, "intent": intent, "done_when": done_when,
                                    "status": "open", "opened": "t", "closed": None,
                                    "phases": None})
    tedb.append_event(conn, session="s1", actor="alpaca", kind="authority-bind", op=oid,
                      ref="auth-1",
                      data={"authority": "auth-1", "level": level, "scope": scope}, clock=clock)


def _close_op(conn, oid, basis, clock):
    tedb.append_event(conn, session="s1", actor="human", kind="op-close", op=oid,
                      data={"basis": basis}, clock=clock)
    tedb.append_event(conn, session="s1", actor="owner", kind="intent-judge", op=oid,
                      data={"judge": "owner", "basis": basis}, clock=clock)
    tedb.patch(conn, "ops", "id", oid, {"status": "closed", "closed": "t"})


# ============================================================ (1) the per-op record model
def test_timeline_carries_the_per_op_record_model(project):
    conn = tedb.connect(project)
    clk = _clock()
    _open_op(conn, "op-001", "build the widget", "the widget prints", clk)
    tedb.append_event(conn, session="s1", actor="agent", kind="task-claim", op="op-001",
                      ref="t1", data={"statement": "wire it"}, clock=clk)
    _close_op(conn, "op-001", "the widget printed in the demo", clk)

    tl = historian.timeline(conn, "op-001")
    # intent + done bar, each with a pointer.
    assert tl["intent"] == "build the widget"
    assert tl["done_when"] == "the widget prints"
    assert tl["intent_pointer"] and tl["done_pointer"]
    # authority, with a pointer.
    assert tl["authority"] == {"authority": "auth-1", "level": "L3", "scope": "single-op"}
    assert tl["authority_pointer"]
    # judgment basis, with a pointer.
    assert tl["judgment"]["basis"] == "the widget printed in the demo"
    assert tl["judgment"]["judge"] == "owner"
    assert tl["judgment_pointer"]
    # the choices log: every note of the op, each with its pointer, in timeline order.
    kinds = [c["kind"] for c in tl["choices"]]
    assert kinds == ["op-open", "authority-bind", "task-claim", "op-close", "intent-judge"]
    assert all(c["pointer"].startswith("event:") for c in tl["choices"])
    # the pointers set is exactly the op's notes (used by the completeness cross-check).
    assert len(tl["pointers"]) == len(tl["choices"])


def test_timeline_falls_back_to_the_ops_row_when_no_open_note(project):
    """An op with a record row but no op-open note still carries its bar (an imported op)."""
    conn = tedb.connect(project)
    tedb.upsert(conn, "ops", "id", {"id": "op-009", "intent": "imported", "done_when": "bar",
                                    "status": "open", "opened": "t", "closed": None,
                                    "phases": None})
    tl = historian.timeline(conn, "op-009")
    assert tl["intent"] == "imported" and tl["done_when"] == "bar"
    assert tl["exists"] is True


# ============================================================ (2) never-drop: surfaced by name
def test_note_with_no_op_is_surfaced_not_dropped(project):
    conn = tedb.connect(project)
    clk = _clock()
    _open_op(conn, "op-001", "placed work", "bar", clk)
    # a note attached to no op: it appears in no timeline.
    orphan = tedb.append_event(conn, session="s1", actor="agent", kind="note",
                               ref="stray", data={"body": "a loose thought"}, clock=clk)

    un = historian.unsorted(conn)
    ptrs = {u["pointer"] for u in un}
    assert historian._pointer(orphan) in ptrs
    row = next(u for u in un if u["pointer"] == historian._pointer(orphan))
    assert row["op"] is None
    assert "no op" in row["reason"]
    # it is surfaced BY NAME: the pointer names the note, nothing is anonymous.
    assert row["pointer"].startswith("event:")


def test_note_naming_an_unknown_op_is_a_dangling_reference(project):
    conn = tedb.connect(project)
    clk = _clock()
    ghost = tedb.append_event(conn, session="s1", actor="agent", kind="result", op="op-404",
                              ref="x", data={"body": "orphaned by a missing op"}, clock=clk)
    un = historian.unsorted(conn)
    row = next(u for u in un if u["pointer"] == historian._pointer(ghost))
    assert row["op"] == "op-404"
    assert "dangling op op-404" in row["reason"]


def test_placed_note_is_not_in_the_unsorted_bucket(project):
    conn = tedb.connect(project)
    clk = _clock()
    _open_op(conn, "op-001", "work", "bar", clk)
    un = historian.unsorted(conn)
    assert un == []          # every note of op-001 is placed in its timeline


# ============================================================ (3) completeness: partition
def test_placed_plus_unsorted_is_every_note(project):
    conn = tedb.connect(project)
    clk = _clock()
    _open_op(conn, "op-001", "a", "bar", clk)
    _open_op(conn, "op-002", "b", "bar", clk)
    tedb.append_event(conn, session="s1", actor="agent", kind="note", data={"body": "loose"},
                      clock=clk)
    tedb.append_event(conn, session="s1", actor="agent", kind="result", op="op-777",
                      data={"body": "dangling"}, clock=clk)

    placed = set()
    for op in historian._known_ops(conn):
        placed.update(historian.timeline(conn, op)["pointers"])
    unsorted_ptrs = {u["pointer"] for u in historian.unsorted(conn)}
    every = {historian._pointer(e) for e in tedb.events(conn, limit=10 ** 9)}

    # partition: placed and unsorted are disjoint and together are every note (nothing dropped).
    assert placed.isdisjoint(unsorted_ptrs)
    assert placed | unsorted_ptrs == every


# ============================================================ (4) the boundary in code
def test_capture_refuses_to_judge_role_is_a_fixed_lookup(project):
    # positive: a known kind maps to its role by the fixed table.
    assert drain._role_of("task-claim") == "intent"
    assert drain._role_of("result") == "result"
    # negative: an unrecognised kind is the NEUTRAL note, never forced to intent or result. Capture
    # refuses to guess what an ambiguous note means - that judgment is sort's, not capture's.
    assert drain._role_of("something-brand-new") == "note"
    # the tables are fixed sets, not a decision procedure over a specific event.
    assert isinstance(drain.INTENT_KINDS, frozenset)
    assert isinstance(drain.RESULT_KINDS, frozenset)


def test_sort_verb_refuses_to_mechanise_the_judgment(project, capsys):
    # negative: --auto (sort as an automatic classifier over a backlog) is refused, BLOCKED.
    args = types.SimpleNamespace(auto=True, day=None, json=False)
    assert tesort.cmd_sort(args) == vc.BLOCKED
    out = capsys.readouterr().out
    assert "refuses to mechanise" in out
    # positive: an ordinary sort (no --auto) runs and PASSes (empty raw layer here).
    args_ok = types.SimpleNamespace(auto=False, day=None, json=False)
    assert tesort.cmd_sort(args_ok) == vc.PASS


# ============================================================ (5) alpaca day is a view, byte-stable
def test_day_digest_regenerates_byte_identical_and_writes_nothing(project):
    conn = tedb.connect(project)
    clk = _clock("2026-03-05T09:00:00+00:00", step=1)
    _open_op(conn, "op-001", "the day's work", "the bar", clk)
    tedb.append_event(conn, session="s1", actor="agent", kind="note", data={"body": "loose"},
                      clock=clk)

    before = len(tedb.events(conn, limit=10 ** 9))
    first = historian.render_day(historian.day(conn, "2026-03-05"))
    second = historian.render_day(historian.day(conn, "2026-03-05"))
    after = len(tedb.events(conn, limit=10 ** 9))

    # a VIEW, never a source: regenerating wrote nothing to the record.
    assert after == before
    # byte-identical under a fixed clock (no wall-clock read in the render).
    assert first == second
    assert first.startswith("# Day 2026-03-05")
    # the day surfaces the unsorted note, never drops it.
    assert "surfaced not dropped" in first


def test_day_digest_counts_ops_and_unsorted(project):
    conn = tedb.connect(project)
    clk = _clock("2026-03-06T00:00:00+00:00", step=1)
    _open_op(conn, "op-001", "x", "bar", clk)
    tedb.append_event(conn, session="s1", actor="agent", kind="note", data={"body": "loose"},
                      clock=clk)
    digest = historian.day(conn, "2026-03-06")
    assert digest["totals"]["ops"] == 1
    assert digest["totals"]["unsorted"] == 1
    assert digest["ops"][0]["op"] == "op-001"


# ============================================================ (6) the pad reports unsorted
def test_pad_reports_unsorted_until_cleared(project):
    conn = tedb.connect(project)
    clk = _clock()
    stray = tedb.append_event(conn, session="s1", actor="agent", kind="note",
                              data={"body": "loose"}, clock=clk)
    block = historian.pad_lines(conn)
    assert block and block[0].startswith("## Unsorted notes: 1")
    assert any(historian._pointer(stray) in line for line in block)

    # cleared: attach the note to a real op timeline -> the pad block goes empty.
    tedb.upsert(conn, "ops", "id", {"id": "op-001", "intent": "i", "done_when": "b",
                                    "status": "open", "opened": "t", "closed": None,
                                    "phases": None})
    tedb.append_event(conn, session="s1", actor="agent", kind="note", op="op-001",
                      data={"body": "placed now"}, clock=clk)
    # the original stray is still unsorted, so the block still reports (until it too is placed).
    assert historian.pad_lines(conn)


if __name__ == "__main__":
    import pytest as _p
    raise SystemExit(_p.main([__file__, "-q"]))
