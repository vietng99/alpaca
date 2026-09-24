"""A record carried over from an earlier harness keeps every event and row byte for byte.

An earlier harness wrote the same events and rows tables, but mixed its own tag into each content
hash. These tests build such a record (by running this package with the earlier tags in place),
then check that `alpaca.lineage.record` makes it verify here without touching one stored hash,
that a new event chains onto the carried head, and that the boundary cannot be moved or faked.
"""
import json

import pytest

from alpaca import db, lineage, migrate
from alpaca.checklist import bridge, gate, obligation_hash, synthesis
from alpaca.gates import chain_check
from alpaca.gates import verdict as vc

OLD_EVENT = "earlier-event/v1"
OLD_ROW = "earlier-obligation/v1"


def _earlier(monkeypatch):
    """Write with the earlier harness's tags until the returned undo is called."""
    monkeypatch.setattr(db, "EVENT_TAG", OLD_EVENT)
    monkeypatch.setattr(obligation_hash, "TAG", OLD_ROW)
    return monkeypatch.undo


def _carried(tmp_path, monkeypatch, n=5):
    """A record whose n events were all written by the earlier harness. Returns (conn, hashes)."""
    undo = _earlier(monkeypatch)
    conn = db.connect(str(tmp_path / "rec"))
    for i in range(n):
        db.append_event(conn, session="s", actor="a", kind="beat", ref="r%d" % i, data={"i": i})
    undo()
    hashes = [(r["id"], r["content_hash"], r["hash"])
              for r in conn.execute("SELECT id, content_hash, hash FROM events ORDER BY id")]
    return conn, hashes


# --------------------------------------------------------------------------- the problem
def test_an_earlier_record_does_not_verify_without_a_lineage(tmp_path, monkeypatch):
    conn, _h = _carried(tmp_path, monkeypatch)
    ok, reason = db.verify_chain(conn)
    assert not ok and "content drift at event 1" in reason
    assert chain_check.verify_chain(conn)[0] == vc.FAIL


# --------------------------------------------------------------------------- the lineage
def test_a_recorded_lineage_verifies_and_keeps_every_hash(tmp_path, monkeypatch):
    conn, before = _carried(tmp_path, monkeypatch)
    got = lineage.record(conn, event_tag=OLD_EVENT, obligation_tag=OLD_ROW, source="earlier",
                         session="migrate", actor="migrate")
    assert got["through_event"] == 5 and got["through_hash"] == before[-1][2]
    ok, reason = db.verify_chain(conn)
    assert ok, reason
    after = [(r["id"], r["content_hash"], r["hash"])
             for r in conn.execute("SELECT id, content_hash, hash FROM events WHERE id<=5 ORDER BY id")]
    assert after == before
    # the adoption itself is the first event under this harness's tag, chained on the old head
    ev = db.last_event(conn)
    assert ev["id"] == 6 and ev["kind"] == lineage.EVENT_KIND and ev["prev_hash"] == before[-1][2]
    assert json.loads(ev["data"])["through_hash"] == before[-1][2]
    assert chain_check.verify_chain(conn)[0] == vc.PASS


def test_new_events_chain_onto_the_carried_head(tmp_path, monkeypatch):
    conn, _b = _carried(tmp_path, monkeypatch)
    lineage.record(conn, event_tag=OLD_EVENT, obligation_tag=OLD_ROW, source="earlier",
                   session="migrate", actor="migrate")
    for i in range(3):
        db.append_event(conn, session="s2", actor="a", kind="beat", ref="n%d" % i, data={"n": i})
    assert db.verify_chain(conn)[0]
    assert chain_check.verify_chain(conn)[0] == vc.PASS


def test_the_head_anchor_uses_the_carried_tags(tmp_path, monkeypatch):
    conn, _b = _carried(tmp_path, monkeypatch)
    lineage.record(conn, event_tag=OLD_EVENT, obligation_tag=OLD_ROW, source="earlier",
                   session="migrate", actor="migrate")
    head = migrate._effective_head(conn)
    assert head["count"] == 6 and head["head"] == db.last_event(conn)["hash"]


def test_recording_is_idempotent_and_refuses_a_second_different_lineage(tmp_path, monkeypatch):
    conn, _b = _carried(tmp_path, monkeypatch)
    first = lineage.record(conn, event_tag=OLD_EVENT, obligation_tag=OLD_ROW, source="earlier",
                           session="m", actor="m")
    again = lineage.record(conn, event_tag=OLD_EVENT, obligation_tag=OLD_ROW, source="earlier",
                           session="m", actor="m")
    assert again == first
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 6
    with pytest.raises(lineage.LineageError):
        lineage.record(conn, event_tag="other-event/v1", obligation_tag=OLD_ROW, source="x",
                       session="m", actor="m")


def test_recording_refuses_events_that_do_not_hash_under_the_named_tag(tmp_path, monkeypatch):
    conn, _b = _carried(tmp_path, monkeypatch)
    with pytest.raises(lineage.LineageError):
        lineage.record(conn, event_tag="wrong-event/v1", obligation_tag=OLD_ROW, source="x",
                       session="m", actor="m")
    assert lineage.read(conn) is None


def test_recording_refuses_an_empty_record_and_a_bad_tag(tmp_path):
    conn = db.connect(str(tmp_path / "empty"))
    with pytest.raises(lineage.LineageError):
        lineage.record(conn, event_tag=OLD_EVENT, obligation_tag=OLD_ROW, source="x",
                       session="m", actor="m")
    with pytest.raises(lineage.LineageError):
        lineage.record(conn, event_tag="Bad Tag\n", obligation_tag=OLD_ROW, source="x",
                       session="m", actor="m")


# --------------------------------------------------------------------------- tamper
def test_an_edited_carried_event_is_still_caught(tmp_path, monkeypatch):
    conn, _b = _carried(tmp_path, monkeypatch)
    lineage.record(conn, event_tag=OLD_EVENT, obligation_tag=OLD_ROW, source="earlier",
                   session="m", actor="m")
    conn.execute("UPDATE events SET actor='TAMPER' WHERE id=3")
    conn.commit()
    ok, reason = db.verify_chain(conn)
    assert not ok and "event 3" in reason
    assert chain_check.verify_chain(conn)[0] == vc.FAIL


def test_moving_the_boundary_over_a_new_event_fails(tmp_path, monkeypatch):
    conn, _b = _carried(tmp_path, monkeypatch)
    lineage.record(conn, event_tag=OLD_EVENT, obligation_tag=OLD_ROW, source="earlier",
                   session="m", actor="m")
    db.append_event(conn, session="s2", actor="a", kind="beat", ref="n", data={})
    lin = json.loads(db.meta_get(conn, lineage.META_KEY))
    last = db.last_event(conn)
    lin.update(through_event=last["id"], through_hash=last["hash"])
    db.meta_set(conn, lineage.META_KEY, json.dumps(lin))
    ok, _reason = db.verify_chain(conn)
    assert not ok
    assert chain_check.verify_chain(conn)[0] == vc.FAIL


def test_a_boundary_hash_that_does_not_match_fails(tmp_path, monkeypatch):
    conn, _b = _carried(tmp_path, monkeypatch)
    lineage.record(conn, event_tag=OLD_EVENT, obligation_tag=OLD_ROW, source="earlier",
                   session="m", actor="m")
    lin = json.loads(db.meta_get(conn, lineage.META_KEY))
    lin["through_hash"] = "0" * 64
    db.meta_set(conn, lineage.META_KEY, json.dumps(lin))
    ok, reason = db.verify_chain(conn)
    assert not ok and "lineage" in reason
    assert chain_check.verify_chain(conn)[0] == vc.FAIL


def test_a_boundary_past_the_last_event_fails(tmp_path, monkeypatch):
    conn, _b = _carried(tmp_path, monkeypatch)
    lineage.record(conn, event_tag=OLD_EVENT, obligation_tag=OLD_ROW, source="earlier",
                   session="m", actor="m")
    lin = json.loads(db.meta_get(conn, lineage.META_KEY))
    lin["through_event"] = 999
    db.meta_set(conn, lineage.META_KEY, json.dumps(lin))
    assert not db.verify_chain(conn)[0]


def test_an_unreadable_lineage_fails_closed(tmp_path, monkeypatch):
    conn, _b = _carried(tmp_path, monkeypatch)
    db.meta_set(conn, lineage.META_KEY, "{not json")
    ok, reason = db.verify_chain(conn)
    assert not ok and "lineage" in reason
    assert chain_check.verify_chain(conn)[0] == vc.FAIL


def test_a_record_with_no_lineage_is_unchanged(tmp_path):
    conn = db.connect(str(tmp_path / "plain"))
    for i in range(3):
        db.append_event(conn, session="s", actor="a", kind="beat", ref="r%d" % i, data={})
    assert lineage.read(conn) is None
    assert db.verify_chain(conn) == (True, "3 events")


# --------------------------------------------------------------------------- obligation rows
_SPEC = gate.SPEC_TEXT
_MANIFEST = gate._anchorize(_SPEC)


def _gate_rows(conn, root):
    import os
    wdir = os.path.join(root, "wiring")
    os.makedirs(wdir, exist_ok=True)
    with open(os.path.join(wdir, "caller.py"), "w", encoding="utf-8") as fh:
        fh.write("from alpaca.checklist import gate\ngate.verify\n")
    for rid, step, stmt in (("cover-plan", "s1", "intake artifacts reviewed against this plan"),
                            ("row-model", "s2", "model authored and compiled from this plan"),
                            ("row-bench", "s3", "bench measurement recorded on the rig")):
        rel = "proof-%s.txt" % rid
        with open(os.path.join(root, rel), "w", encoding="utf-8") as fh:
            fh.write("artifact for the work reported\nrows: %s\n" % rid)
        row = {"id": rid, "kind": "item", "op": "op-1", "phase": "build", "step": step,
               "statement": stmt, "proof": "local:%s" % rel, "where_": "", "how": "", "when_": "",
               "why": "", "status": "open", "tag": "Specced", "supersedes": None}
        row["content_hash"] = gate._row_content_hash(row)
        row["prev_hash"] = chain_check.GENESIS
        db.upsert(conn, "rows", "id", row)
        gate.verdict_row.discharge(conn, rid, row["content_hash"], "probe", vc.PASS,
                                   ["local:%s" % rel], "L2", "s1")
    return [wdir]


def _gate(conn, root, wiring):
    return gate.verify(conn, "op-1", "build", spec=_SPEC, step_manifest=_MANIFEST,
                       wiring_roots=wiring, root=root, write_receipt=False)


def test_the_checklist_gate_passes_on_carried_rows(tmp_path, monkeypatch):
    root = str(tmp_path / "g")
    undo = _earlier(monkeypatch)
    conn = db.connect(root)
    wiring = _gate_rows(conn, root)
    undo()
    assert _gate(conn, root, wiring).verdict != vc.PASS          # carried hashes, no lineage yet
    lineage.record(conn, event_tag=OLD_EVENT, obligation_tag=OLD_ROW, source="earlier",
                   session="m", actor="m")
    res = _gate(conn, root, wiring)
    assert res.verdict == vc.PASS, res


def test_a_carried_row_edited_in_place_still_halts_on_drift(tmp_path, monkeypatch):
    root = str(tmp_path / "g")
    undo = _earlier(monkeypatch)
    conn = db.connect(root)
    wiring = _gate_rows(conn, root)
    undo()
    lineage.record(conn, event_tag=OLD_EVENT, obligation_tag=OLD_ROW, source="earlier",
                   session="m", actor="m")
    conn.execute("UPDATE rows SET statement='a later hand edit of the frozen row' WHERE id=?",
                 ("row-bench",))
    conn.commit()
    res = _gate(conn, root, wiring)
    assert res.verdict == vc.FAIL and res.token == gate.R_HALT_ON_DRIFT


def test_a_row_added_after_the_boundary_cannot_use_the_earlier_tag(tmp_path, monkeypatch):
    conn, _b = _carried(tmp_path, monkeypatch)
    lineage.record(conn, event_tag=OLD_EVENT, obligation_tag=OLD_ROW, source="earlier",
                   session="m", actor="m")
    lin = lineage.read(conn)
    row = {"id": "late", "kind": "item", "op": "op-1", "phase": "build", "step": "s1",
           "statement": "a row written after the carry", "proof": "", "where_": "", "how": "",
           "when_": "", "why": "", "status": "open", "tag": "Specced", "supersedes": None}
    row["content_hash"] = obligation_hash.content_hash(row, OLD_ROW)
    db.upsert(conn, "rows", "id", row)
    numbers = lineage.row_numbers(conn)
    assert not lineage.row_frozen(row, lin, numbers.get("late"))
    row["content_hash"] = obligation_hash.content_hash(row)
    assert lineage.row_frozen(row, lin, numbers.get("late"))


def test_the_bridge_accepts_a_carried_row_it_derives_again(tmp_path, monkeypatch):
    spec = tmp_path / "acceptance.md"
    spec.write_text("| item | need |\n|---|---|\n| adder8 | timing closure report |\n")
    from alpaca.checklist import artifact
    parsed = artifact.parse(str(spec), "item")
    model = {"model_id": "build", "steps": [{"key": "timing",
             "obligation": "Verify timing closure for {item} using {artifact}"}]}
    undo = _earlier(monkeypatch)
    rows = synthesis.derive(model, parsed, op="flow", session="one", operator="codex")
    conn = db.connect(str(tmp_path / "b"))
    assert bridge.apply(conn, rows)["verdict"] == vc.PASS
    undo()
    lineage.record(conn, event_tag=OLD_EVENT, obligation_tag=OLD_ROW, source="earlier",
                   session="m", actor="m")
    again = synthesis.derive(model, parsed, op="flow", session="one", operator="codex")
    assert bridge.apply(conn, again)["verdict"] == vc.PASS
