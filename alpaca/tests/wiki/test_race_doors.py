"""op-008 t-070 fix plan RC1: every write door takes the store's write lock first and reads its
preconditions inside the transaction (review findings C-1, B-3, M-adv-6/M-inv-5, m-adv-5, merge m8).

The rig injects a competing commit exactly at the boundary a door used to leave open: right before the
door's write transaction takes the lock (`Writer.transaction` patched to run the competitor first, on
its own connection), or, for the consolidation step, right after the step read its checkpoint, and
for schema creation, in the middle of the first creator's schema script. Afterwards the live store
must equal a rebuild from the record (accepted digest, candidate rows, active blocks), nothing
withdrawn may be active, and each act lands once. On 82434c15a every case here failed (the door acted
on what it had read before the competitor committed) except two orders that already held there,
kept as coverage: an admission meeting the withdrawal of its anchor, and a withdrawal meeting an
admission (`.alpaca/proofs/op-008/t-070.logs/rc13/red-base-82434c15a.log`).

Real record, real drain, real store; the injection is the only fault and is named where it is made.
"""
import contextlib
import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import (T09, T10, admit, append, appender, at, capture, ingest,  # noqa: E402
                            note, proposal, propose, row_of, store, wiki_events, withdraw)
from convergence_helpers import assert_converged, shadow, state  # noqa: E402

from alpaca.wiki import consolidate, portable, service             # noqa: E402
from alpaca.wiki.config import Config                              # noqa: E402
from alpaca.wiki.store.write import Writer                         # noqa: E402

BLOCKED = "Task t-9 is blocked on the review."


@contextlib.contextmanager
def at_lock_boundary(monkeypatch, competitor):
    """Run `competitor()` once, on its own connections, right before the next `Writer.transaction`
    takes the store's write lock: the moment a door that read its preconditions earlier would act on
    them. Yields a dict that holds the competitor's result."""
    real = Writer.transaction
    state = {"fired": False, "result": None}

    @contextlib.contextmanager
    def boundary(self):
        if not state["fired"]:
            state["fired"] = True
            state["result"] = competitor()
        with real(self):
            yield self

    monkeypatch.setattr(Writer, "transaction", boundary)
    try:
        yield state
    finally:
        monkeypatch.setattr(Writer, "transaction", real)
    assert state["fired"], "the door under test never opened a write transaction"


def owner_withdraws(project, ref):
    return lambda: service.withdraw_source(project, ref, session="s-owner",
                                           record=appender(project, "s-owner"), reason="race")


def applied(project, kind):
    return [e["data"] for e in wiki_events(project, kind) if e["data"].get("result", "applied") == "applied"]


# ------------------------------------------------------------------ absorb
def test_absorb_meets_a_withdrawal_at_its_lock(project, monkeypatch):
    """C-1 through the rig: the withdrawal of the event commits after the absorb door read the
    exclusions and before it took the lock; the note must not land active."""
    note(project, "The lantern rota moved to Tuesday.", T09)
    gone = at(project, "result", {"body": "The beacon rota moved to Friday."}, T10)
    with at_lock_boundary(monkeypatch, owner_withdraws(project, "event:%d" % gone)) as race:
        capture(project, {gone})
    assert race["result"].status == "applied", race["result"]
    assert_converged(project)


def test_capture_meets_another_event_note_at_its_lock(project, monkeypatch):
    """Event identity (`drain._same_event`, merge m8) is a precondition too: the drain decided the
    note lands at its plain id (nothing held there), then another event's note landed at that id
    before the absorb took the lock (the shape of the 2026-09-30 restore). The decision is asked
    again under the lock, so the other note stays as it is and this one lands at its `-render-`
    revision."""
    from alpaca import db as record
    from alpaca.wiki.determinism import sha256_hex
    from alpaca.wiki.ingest import drain
    from alpaca.wiki.ingest.absorb import Absorber
    from native_helpers import events, vault
    note(project, "The lantern rota moved to Tuesday.", T09)        # the store exists
    eid = append(project, "result", {"body": "the heron rota moved to Monday"}, session="s-m8")
    conn = record.connect_readonly(project)
    try:
        ev = drain.normalize_events(conn, events(project, {eid}))[0]
    finally:
        conn.close()
    doc_id, _text = drain._note_doc(ev)
    other = drain._note_doc(dict(ev, ts="2020-01-01T00:00:00+00:00",
                                 data={"body": "another event that held this id"}))[1]

    def seed():
        absorber = Absorber(Config.for_project(project))
        try:
            absorber.absorb_text(doc_id, other, kind="raw", path=doc_id)
        finally:
            absorber.close()
        (vault(project) / doc_id).write_text(other)

    with at_lock_boundary(monkeypatch, seed):
        result = drain.capture(project, events(project, {eid}))
    assert "-render-" in result["docs"][0], result
    with store(project) as conn:
        held = conn.execute("SELECT content_sha256 FROM docs WHERE doc_id=?", (doc_id,)).fetchone()[0]
    assert held == sha256_hex(other) and (vault(project) / doc_id).read_text() == other


# ------------------------------------------------------------------ propose
def test_two_proposals_of_one_candidate_give_one_row_and_one_proposer(project, monkeypatch):
    """adv-r1-m3 through the rig: a second session proposes the same candidate at the boundary of
    the first; exactly one non-duplicate `wiki-candidate` event, and its session is the row's
    proposer in the live store and in the rebuild."""
    eid = note(project, BLOCKED)
    body = proposal(project, eid, "Task t-9 is blocked")
    with at_lock_boundary(monkeypatch, lambda: propose(project, body, session="s-other")):
        first = propose(project, body, session="s-agent")
    made = [e for e in wiki_events(project, "wiki-candidate") if e["data"]["status"] != "duplicate"]
    assert len(made) == 1, [e["data"]["status"] for e in made]
    assert row_of(project, first.candidate_id)["proposer"] == made[0]["session"]
    assert_converged(project)


# ------------------------------------------------------------------ admit
@pytest.mark.parametrize("rival", ["admit", "withdraw-candidate", "withdraw-source"])
def test_admit_meets_a_rival_act_at_its_lock(project, monkeypatch, rival):
    """B-3 and its kin: another admission of the candidate, its withdrawal by the proposer, or the
    withdrawal of its anchor commits after `admit` read the row as `checked` and before the lock.
    One admission at most is applied, a withdrawn candidate is never admitted, and the store equals
    its rebuild."""
    eid = note(project, BLOCKED)
    cid = propose(project, proposal(project, eid, "Task t-9 is blocked")).candidate_id
    rivals = {"admit": lambda: admit(project, cid, session="s-admin-1"),
              "withdraw-candidate": lambda: withdraw(project, cid),
              "withdraw-source": owner_withdraws(project, "event:%d" % eid)}
    with at_lock_boundary(monkeypatch, rivals[rival]) as race:
        outer = admit(project, cid, session="s-admin-2")
    assert race["result"].status == "applied", race["result"]
    admissions = applied(project, "wiki-admission")
    if rival == "admit":
        assert len(admissions) == 1 and outer.status != "applied", outer
    else:
        assert admissions == [] and outer.status == "refused", outer
        assert row_of(project, cid)["status"] in ("withdrawn", "stale")
    assert_converged(project)


# ------------------------------------------------------------------ withdraw and retract
def test_two_withdrawals_of_one_source_apply_once(project, monkeypatch):
    eid = note(project, BLOCKED)
    with at_lock_boundary(monkeypatch, owner_withdraws(project, "event:%d" % eid)) as race:
        outer = owner_withdraws(project, "event:%d" % eid)()
    assert race["result"].status == "applied"
    assert outer.status == "duplicate", outer
    assert len(applied(project, "wiki-withdrawal")) == 1
    assert_converged(project)


def test_two_retractions_of_one_admitted_candidate_undo_once(project, monkeypatch):
    """Retract: the owner and the proposer withdraw one admitted candidate at once; its net effect is
    undone once and one `wiki-candidate-withdrawn` event is applied."""
    eid = note(project, BLOCKED)
    cid = propose(project, proposal(project, eid, "Task t-9 is blocked")).candidate_id
    assert admit(project, cid).status == "applied"
    with at_lock_boundary(monkeypatch, lambda: withdraw(project, cid, session="s-owner", by="owner")):
        outer = withdraw(project, cid)
    assert outer.status == "duplicate", outer
    assert len(wiki_events(project, "wiki-candidate-withdrawn")) == 1
    assert row_of(project, cid)["status"] == "retracted"
    assert_converged(project)


def test_a_withdrawal_meets_an_admission_at_its_lock(project, monkeypatch):
    """The other order: the candidate is admitted after `withdraw_source` read the store and before
    its lock; step 6 retracts the admitted candidate."""
    eid = note(project, BLOCKED)
    cid = propose(project, proposal(project, eid, "Task t-9 is blocked")).candidate_id
    with at_lock_boundary(monkeypatch, lambda: admit(project, cid)) as race:
        outer = owner_withdraws(project, "event:%d" % eid)()
    assert race["result"].status == "applied" and outer.status == "applied", outer
    assert row_of(project, cid)["status"] == "retracted"
    assert_converged(project)


# ------------------------------------------------------------------ consolidate
def test_two_consolidation_steps_never_try_one_item_twice(project, monkeypatch):
    """M-adv-6 / M-inv-5: a second step starts after the first read its checkpoint and failure state.
    Each item is tried by one step only, so the attempts the failure events record are the tries
    made, and three attempts stay the cap."""
    err = "OSError: disk %d busy"
    a = at(project, "drain-failed", {"error": err % 1, "where": "pre_compact"}, T09, session="s-f1")
    b = at(project, "drain-failed", {"error": err % 2, "where": "pre_compact"}, T10, session="s-f2")
    assert (a, b)                                    # not captured: the item fails anchor-unresolved
    real = service.propose
    tries = []
    state = {"rival": None}

    def counting(root, body, **kw):
        tries.append(body["text"])
        if state["rival"] is None:
            state["rival"] = "running"
            state["rival"] = consolidate.run_step(project, deadline_s=30)
        return real(root, body, **kw)

    monkeypatch.setattr(service, "propose", counting)
    for _ in range(4):
        consolidate.run_step(project, deadline_s=30)
    monkeypatch.setattr(service, "propose", real)
    recorded = [e["data"] for e in wiki_events(project, "rune2-consolidate-failure")]
    assert len(tries) == len(recorded) <= consolidate.MAX_ATTEMPTS, (tries, recorded)
    assert [d["attempts"] for d in recorded] == list(range(1, len(recorded) + 1)), recorded
    assert_converged(project, catch_up=True)       # the failure events stay uncaptured on purpose


# ------------------------------------------------------------------ import
def test_two_imports_into_one_empty_store_land_once(project, monkeypatch):
    """Two imports of one export into the same empty store: the one that claims the store first
    imports, the other is refused `store-not-empty`, and the result is a byte-identical round trip."""
    eid = note(project, BLOCKED)
    cid = propose(project, proposal(project, eid, "Task t-9 is blocked")).candidate_id
    assert admit(project, cid).status == "applied"
    ingest(project, "The kestrel queue drains nightly at two.\n", "wiki/notes/kestrel.md", name="k.md")
    export = Path(project).parent / "race-export.jsonl"
    portable.export_store(project, export)
    target = shadow(project, "race-import")
    with at_lock_boundary(monkeypatch, lambda: portable.import_store(target, export)) as race:
        outer = portable.import_store(target, export)
    statuses = sorted([race["result"]["status"], outer["status"]])
    assert statuses == ["ok", "refused"], (race["result"], outer)
    assert "store-not-empty" in (race["result"].get("reason"), outer.get("reason"))
    again = Path(project).parent / "race-export-2.jsonl"
    portable.export_store(target, again)
    assert again.read_bytes() == export.read_bytes()
    # a `wiki ingest` document is not in the record, so the imported store, not a rebuild, is what
    # the live store must equal here
    assert state(target)["digest"] == state(project)["digest"]


# ------------------------------------------------------------------ schema init
def test_a_fresh_store_is_created_under_the_lock(project):
    """m-adv-5: a second process opens a fresh store while the first is in the middle of its schema
    script (paused right before `blocks` is created). Status reads and a capture must not fail with
    `no such table`, and the store ends complete."""
    from alpaca.wiki.store.db import DB
    cfg = Config.for_project(project)
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    paused, errors = threading.Event(), []

    def creator():
        db = DB(cfg)
        released = threading.Event()

        def trace(sql):
            if "CREATE TABLE IF NOT EXISTS blocks" in sql and not paused.is_set():
                paused.set()
                released.wait(1.5)                   # the injection: hold the creator mid-schema
        db.conn.set_trace_callback(trace)
        try:
            db.pour()
        except Exception as exc:                     # noqa: BLE001
            errors.append(("creator", repr(exc)))
        finally:
            db.close()

    thread = threading.Thread(target=creator)
    thread.start()
    try:
        assert paused.wait(20), "the creator never reached the blocks table"
        eid = append(project, "result", {"body": "The heron rota moved to Monday."})
        try:
            service.status(project)
            capture(project, {eid})
        except Exception as exc:                     # noqa: BLE001
            errors.append(("second", repr(exc)))
    finally:
        thread.join(30)
    assert errors == []
    with store(project) as conn:
        assert conn.execute("SELECT COUNT(*) FROM blocks WHERE status='active'").fetchone()[0] >= 1
    assert_converged(project)
