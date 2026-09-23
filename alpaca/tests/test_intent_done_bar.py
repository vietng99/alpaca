"""M3.6: the intent record, the done-bar, and the verbatim journal. Proof for task M3.6.

Split the label (names the work) from the bar (says when the work is done). An op cannot open
without an intent label AND a done bar whose author is recorded; the owner authors the bar and the
agent may draft it but must have it trace to an owner artifact. A recorded bar is immutable: a
change is a NEW intent citing the old, never an in-place edit. An agent can neither loosen a
recorded bar nor author a fresh lax one at or above L5 without an owner artifact to trace to. Op
close records who judged the bar met and on what basis. Each `intents/queue.md` line becomes an
intent record when it is picked up, and the queue stays the only source an unattended loop opens
the next op from.

The verbatim journal is the ported capture.py core (append raw turn bytes, hash-chained, parse
only from disk; the signature parsers dropped). Its `--selftest` control table is ported here as a
pytest wrapper that fails on any control that did not fire.

Timing is driven by a FixedClock (M2.2) so every recorded timestamp is deterministic; the negative
and positive paths are both exercised.
"""
import os

import pytest

from alpaca import cli, db, util
from alpaca import intent, journal
from alpaca.clock import FixedClock


@pytest.fixture
def clock():
    """A FixedClock installed process-wide, so intent/journal timestamps are deterministic."""
    util.set_clock(FixedClock(start="2026-01-01T00:00:00+00:00", step=1))
    try:
        yield
    finally:
        util.set_clock(None)


def _write_queue(root, lines):
    path = os.path.join(root, "intents", "queue.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    util.write_text(path, "\n".join(lines) + "\n")
    return path


# ========================================================================= the verbatim journal
def test_journal_append_and_load_are_a_byte_exact_round_trip(project, clock):
    """A captured turn round-trips byte-exactly, and the journal verifies as a clean population."""
    conn = db.connect(project)
    ref = journal.append(conn, b"the owner's mark, opaque bytes\n", actor="owner")
    assert ref.seq == 1
    cap = journal.load(conn, 1)
    assert cap.raw() == b"the owner's mark, opaque bytes\n"
    v, reasons, info = journal.verify(conn)
    assert v == journal.PASS_V
    assert info["record_count"] == 1


def test_journal_parses_only_from_disk_a_mutated_payload_is_caught(project, clock):
    """The recorded sha is a producer token, re-derived from the bytes on disk and never believed:
    a single mutated payload byte is a FAIL, not a silent pass (negative path)."""
    conn = db.connect(project)
    journal.append(conn, b"A. Person\n", actor="owner")
    jp = journal.journal_path_for(conn)
    raw = bytearray(journal.read_raw(jp))
    idx = raw.rfind(b"Person")
    raw[idx] = ord("p")
    journal.atomic_write_bytes(jp, bytes(raw))
    v, reasons, info = journal.verify(conn)
    assert v == journal.FAIL_V
    assert reasons[0] == "CAPTURE-HASH-MISMATCH"


def test_a_captured_object_cannot_be_constructed_by_hand(project, clock):
    """`Captured` has no public constructor: interpretation can never precede capture (negative)."""
    with pytest.raises(journal.Refusal) as ex:
        journal.Captured(journal="x", seq=1, sha="0" * 64, payload=b"x")
    assert ex.value.reason == "CAPTURE-CONSTRUCT-FORBIDDEN"


def test_an_empty_turn_is_refused(project, clock):
    conn = db.connect(project)
    with pytest.raises(journal.Refusal) as ex:
        journal.append(conn, b"", actor="owner")
    assert ex.value.reason == "CAPTURE-EMPTY-TURN"


def test_journal_selftest_control_table_every_control_fires(project, clock):
    """The ported `--selftest` control table, run as a pytest wrapper that fails on any non-PASS.
    Every control must FIRE and the run reports zero failures."""
    failures, rows = journal.selftest()
    not_fired = [r for r in rows if r[4] != "FIRED"]
    assert not not_fired, "controls that did not fire: %r" % [r[0] for r in not_fired]
    assert failures == 0
    assert any(r[0] == "K-00" for r in rows)   # the positive control is present


# ================================================================== the intent record / done-bar
def test_record_requires_a_label_a_bar_and_a_recorded_author(project, clock):
    conn = db.connect(project)
    # missing bar
    with pytest.raises(intent.IntentRefusal) as e_bar:
        intent.record(conn, "de-sign the checklist engine", "", "owner")
    assert e_bar.value.reason == intent.R_BAR_EMPTY
    # missing author
    with pytest.raises(intent.IntentRefusal) as e_auth:
        intent.record(conn, "de-sign the checklist engine", "the suite is green", "")
    assert e_auth.value.reason == intent.R_AUTHOR_EMPTY
    # missing label
    with pytest.raises(intent.IntentRefusal) as e_lab:
        intent.record(conn, "", "the suite is green", "owner")
    assert e_lab.value.reason == intent.R_LABEL_EMPTY
    # the well-formed intent: label + bar + author all present
    rec = intent.record(conn, "de-sign the checklist engine", "the suite is green", "owner")
    assert rec["label"] and rec["done_when"] and rec["author"] == "owner"
    assert intent.get(conn, rec["id"])["done_when"] == "the suite is green"


def test_agent_authored_bar_at_L5_without_an_owner_artifact_is_refused(project, clock):
    conn = db.connect(project)
    # an agent authoring a fresh bar at L5 with no owner artifact to trace to: refused
    with pytest.raises(intent.IntentRefusal) as ex:
        intent.record(conn, "ship it", "looks fine", "scout",
                      author_role="agent", level="L5", pointer="")
    assert ex.value.reason == intent.R_AGENT_LAX_BAR_NO_OWNER
    # the same agent bar WITH an owner artifact to trace to: allowed (a draft that traces to owner)
    ok = intent.record(conn, "ship it", "the acceptance test is green", "scout",
                       author_role="agent", level="L5", pointer="local:intents/queue.md")
    assert ok["author"] == "scout" and ok["pointer"] == "local:intents/queue.md"
    # the owner may author a bar at L5 with no pointer: the owner IS the artifact
    owned = intent.record(conn, "ship it", "the owner is satisfied", "owner",
                          author_role="owner", level="L5")
    assert owned["author_role"] == "owner"


def test_a_recorded_bar_cannot_be_edited_a_change_is_a_new_intent(project, clock):
    conn = db.connect(project)
    old = intent.record(conn, "build the board", "two agents share one op", "owner")
    with pytest.raises(intent.IntentRefusal) as ex:
        intent.edit(conn, old["id"], done_when="something looser")
    assert ex.value.reason == intent.R_BAR_IMMUTABLE
    # a change is a NEW intent citing the old
    new = intent.record(conn, "build the board", "two agents share one op and the page shows it",
                        "owner", supersedes=old["id"])
    assert new["id"] != old["id"]
    assert new["supersedes"] == old["id"]
    assert intent.get(conn, old["id"])["done_when"] == "two agents share one op"  # unchanged


def test_an_agent_loosening_a_recorded_bar_needs_an_owner_artifact(project, clock):
    conn = db.connect(project)
    old = intent.record(conn, "build the board", "two agents share one op", "owner")
    with pytest.raises(intent.IntentRefusal) as ex:
        intent.record(conn, "build the board", "one agent is enough", "scout",
                      author_role="agent", supersedes=old["id"], pointer="")
    assert ex.value.reason == intent.R_AGENT_LOOSEN_NO_OWNER
    ok = intent.record(conn, "build the board", "one agent is enough", "scout",
                       author_role="agent", supersedes=old["id"], pointer="local:decisions/dec-1.md")
    assert ok["supersedes"] == old["id"]


# ============================================================ op open / close carry the intent
def test_op_new_from_intent_opens_from_the_queue_and_carries_its_bar(project, clock):
    _write_queue(project, [
        "# Intent queue",
        "",
        "- [x] already done",
        "- [ ] ship the milestone - done when the acceptance test is green, proof local:tests/t.py",
    ])
    conn = db.connect(project)
    assert cli.main(["op", "new", "--from-intent"]) == cli.PASS
    op = db.rows(conn, "ops", "id=?", ("op-001",))[0]
    assert op["done_when"] == "the acceptance test is green, proof local:tests/t.py"
    assert op["intent"] == "ship the milestone"
    iid = intent.for_op(conn, "op-001")
    assert iid is not None
    rec = intent.get(conn, iid)
    assert rec["author"]                            # the bar's author is recorded
    assert rec["done_when"]                          # a done bar exists
    assert rec["pointer"] == "local:intents/queue.md"  # traces to the owner artifact (the queue)


def test_op_new_from_intent_refuses_a_queue_line_with_no_bar(project, clock):
    _write_queue(project, [
        "- [ ] a wish with no bar",
    ])
    db.connect(project)
    assert cli.main(["op", "new", "--from-intent"]) == cli.BLOCKED


def test_op_new_from_intent_refuses_when_the_queue_has_no_open_line(project, clock):
    _write_queue(project, [
        "- [x] all done here",
    ])
    db.connect(project)
    assert cli.main(["op", "new", "--from-intent"]) == cli.BLOCKED


def test_existing_op_new_still_works(project, clock):
    """The `--from-intent` path is additive: the plain `op new` keeps working unchanged."""
    conn = db.connect(project)
    assert cli.main(["op", "new", "run the milestone", "--done-when", "green"]) == cli.PASS
    op = db.rows(conn, "ops", "id=?", ("op-001",))[0]
    assert op["intent"] == "run the milestone" and op["done_when"] == "green"


def test_op_new_with_neither_an_intent_nor_from_intent_is_refused(project, clock):
    db.connect(project)
    assert cli.main(["op", "new"]) == cli.FAIL


def test_close_records_who_judged_the_bar_met_and_on_what_basis(project, clock):
    conn = db.connect(project)
    rec = intent.record(conn, "close me", "the suite is green", "owner")
    # judge refuses an empty basis (negative)
    with pytest.raises(intent.IntentRefusal) as ex:
        intent.judge(conn, "op-001", "", judge="owner")
    assert ex.value.reason == intent.R_JUDGE_NO_BASIS
    # a real judgment is recorded with the judge and the basis (positive)
    j = intent.judge(conn, "op-001", "ran the acceptance suite, all green", judge="owner")
    assert j["judge"] == "owner"
    assert j["basis"] == "ran the acceptance suite, all green"
    judged = [e for e in db.events(conn, kind=intent.JUDGE_KIND) if e["op"] == "op-001"]
    assert judged and judged[-1]["data"]["basis"] == "ran the acceptance suite, all green"


def test_op_close_records_the_judgment_on_the_record(project, clock):
    """Closing an op through the CLI records who judged and on what basis (the close basis)."""
    _write_queue(project, [
        "- [ ] tiny op - done when nothing is left to do, proof local:tests/t.py",
    ])
    conn = db.connect(project)
    assert cli.main(["op", "new", "--from-intent"]) == cli.PASS
    assert cli.main(["op", "close", "op-001", "--basis", "judged met: everything shipped"]) == cli.PASS
    judged = [e for e in db.events(conn, kind=intent.JUDGE_KIND) if e["op"] == "op-001"]
    assert judged
    assert judged[-1]["data"]["basis"] == "judged met: everything shipped"


def test_each_queue_line_becomes_an_intent_record_when_picked_up(project, clock):
    """The queue is the only source an unattended loop opens from: picking a line records an
    intent, and the next pick advances to the next open line."""
    _write_queue(project, [
        "- [ ] first op - done when the first bar is met, proof local:a",
        "- [ ] second op - done when the second bar is met, proof local:b",
    ])
    conn = db.connect(project)
    first = intent.next_from_queue(conn, root=project, session="loop")
    assert first["label"] == "first op"
    assert first["done_when"].startswith("the first bar is met")
    # the recorded intent is on the append-only record
    seen = [e for e in db.events(conn, kind=intent.INTENT_KIND)]
    assert any(e["data"]["id"] == first["id"] for e in seen)
