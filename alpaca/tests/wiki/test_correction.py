"""op-008 t-067 stage s1-withdraw: the owner correction verb (design/rune2-memory/architecture.md 5.1,
10.2; FR-21; OB-14; dec-38940e1fb35f Q5).

An owner correction supersedes a fact (it was wrong), records a world change at a stated time, retracts
a fact, or reverts a successor. It is resolved to a fact identity before the `wiki-correction` event is
appended, the replacement is anchored on the correction's own note, and a rebuild from the record gives
the same accepted digest. Anyone but the owner is refused (`agent-correction-use-propose`) and that
event is not a source. Real record, real drain, real store; the one monkeypatch (a renderer that words
a header line differently, to re-render a note) says so where it is used.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import (T09, T10, T11, appender, at, capture, claims, digest_of, edges,  # noqa: E402
                            events, rebuild_store, sockets_blocked, store, wiki_events)

from alpaca.wiki import intake, service                          # noqa: E402
from alpaca.wiki.ingest import drain                             # noqa: E402

T12 = "2026-03-01T12:00:00+00:00"


def move(project, to, ts, frm="open", task="t-9"):
    data = {"from": frm, "to": to, "proof": None, "reason": None}
    return at(project, "task-move", data, ts, op="op-8", ref=task, actor="human")


def two_moves(project, task="t-9"):
    first = move(project, "doing", T09, task=task)
    second = move(project, "done", T10, frm="doing", task=task)
    return first, second


def status(project, task="t-9", **where):
    return {e["obj_node"]: e for e in edges(project, predicate="status", subj_node=task, **where)}


def correct(project, target, action, *, by="owner", session="s-owner", reason="the record is wrong",
            **kw):
    return service.correct(project, target, action, session=session,
                           record=appender(project, session), reason=reason, by=by, **kw)


def new_node(obj, datatype="node"):
    return {"obj": obj, "obj_datatype": datatype}


def digest_and_edges(project):
    return digest_of(project), [(e["subj_node"], e["predicate"], e["obj_node"] or e["obj_literal"],
                                 e["status"], e["valid_from"], e["valid_until"]) for e in edges(project)]


# ------------------------------------------------------------------ the four actions
def test_an_owner_correction_supersedes_retracts_or_reverts(project):
    two_moves(project, "t-1")
    two_moves(project, "t-2")
    capture(project)
    done = status(project, "t-1")["status:done"]
    # supersede: the fact was wrong; the replacement is current, the target is invalidated
    made = correct(project, done["edge_id"], "supersede", replacement=new_node("status:blocked"))
    assert (made.status, made.reason) == ("applied", None), made
    now = status(project, "t-1")
    assert now["status:done"]["status"] == "invalidated"
    assert now["status:done"]["superseded_by_edge_id"] == made.effects["replacement_edge_id"]
    replacement = now["status:blocked"]
    assert replacement["status"] == "active" and replacement["valid_until"] is None
    assert replacement["edge_id"] == made.effects["replacement_edge_id"]
    # the replacement quotes the correction's own note, which is a source with an `actor:` line
    note_ref = "event:%d" % made.record_event_id
    (claim,) = [c for c in claims(project, replacement["edge_id"]) if c["status"] == "live"]
    assert (claim["source_ref"], claim["fact_kind"], claim["source_event_id"]) == (
        note_ref, "record-fact", made.record_event_id)
    with store(project) as conn:
        doc = "\n\n".join(r[0] for r in conn.execute(
            "SELECT text FROM blocks WHERE doc_id=? AND status='active' ORDER BY ordinal",
            (replacement["source_doc_id"],)))
        block = conn.execute("SELECT text FROM blocks WHERE block_id=?",
                             (replacement["source_block_id"],)).fetchone()[0]
    assert "actor: owner" in doc and "action: supersede" in doc
    assert replacement["source_quote"] in block and replacement["source_quote"] == "t-1 status status:blocked"
    # revert the successor: it is retracted and the target is given back, active, as before
    back = correct(project, replacement["edge_id"], "revert", reason="the first record was right")
    assert back.status == "applied", back
    now = status(project, "t-1")
    assert now["status:blocked"]["status"] == "retracted"
    assert (now["status:done"]["status"], now["status:done"]["superseded_at"],
            now["status:done"]["superseded_by_edge_id"]) == ("active", None, None)
    # retract: only the target moves
    other = status(project, "t-2")
    gone = correct(project, other["status:done"]["edge_id"], "retract", reason="never happened")
    assert gone.status == "applied" and gone.effects["replacement_edge_id"] is None
    after = status(project, "t-2")
    assert (after["status:done"]["status"], after["status:done"]["retraction_reason"]) == (
        "retracted", "owner-correction")
    assert after["status:doing"]["valid_until"] == other["status:doing"]["valid_until"]
    with store(project) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM corrections ORDER BY record_event_id")]
    assert [(r["action"], r["status"], r["requested_by"]) for r in rows] == [
        ("supersede", "applied", "s-owner"), ("revert", "applied", "s-owner"),
        ("retract", "applied", "s-owner")]
    assert rows[0]["target_edge_id"] == done["edge_id"] and rows[0]["target_quote_sha256"] == ""
    assert json.loads(rows[0]["replacement_json"]) == {
        "subject": "t-1", "predicate": "status", "obj": "status:blocked", "obj_datatype": "node"}


def test_a_target_may_be_named_by_its_fact_identity(project):
    two_moves(project)
    capture(project)
    done = status(project)["status:done"]
    with store(project) as conn:
        identity = intake.fact_identity(conn, done)
    made = correct(project, identity, "retract")
    assert made.status == "applied" and made.effects["target_edge_id"] == done["edge_id"]
    (event,) = wiki_events(project, "wiki-correction")
    assert event["data"]["target"]["anchor_source_ref"].startswith("event:")
    assert event["data"]["target_edge_id"] == done["edge_id"] and event["data"]["result"] == "applied"


def test_a_correction_that_names_no_single_fact_changes_nothing(project):
    two_moves(project)
    capture(project)
    before = digest_and_edges(project)
    unknown = correct(project, "e_" + "0" * 32, "retract")
    assert (unknown.status, unknown.reason) == ("refused", "unknown-target")
    done = status(project)["status:done"]
    again = correct(project, done["edge_id"], "retract")
    assert again.status == "applied"
    repeated = correct(project, done["edge_id"], "retract")
    # the edge is retracted now, so the same name resolves to nothing
    assert (repeated.status, repeated.reason) == ("refused", "unknown-target")
    nothing = correct(project, status(project)["status:doing"]["edge_id"], "revert")
    assert (nothing.status, nothing.reason) == ("refused", "nothing-to-revert")
    with pytest.raises(ValueError):
        correct(project, done["edge_id"], "erase")
    with pytest.raises(ValueError):
        correct(project, done["edge_id"], "retract", reason=" ")
    assert before[0] != digest_of(project)           # only the one retraction moved the digest


def test_the_same_correction_twice_is_one_row(project):
    two_moves(project)
    capture(project)
    done = status(project)["status:done"]
    first = correct(project, done["edge_id"], "supersede", replacement=new_node("status:blocked"))
    assert first.status == "applied"
    target = wiki_events(project, "wiki-correction")[0]["data"]["target"]
    second = correct(project, target, "supersede", replacement=new_node("status:blocked"))
    assert (second.status, second.reason) == ("duplicate", "already-applied")
    with store(project) as conn:
        assert conn.execute("SELECT COUNT(*) FROM corrections").fetchone()[0] == 1


# ------------------------------------------------------------------ who may correct
def test_an_agent_correction_is_refused_and_its_event_is_not_a_source(project):
    """An agent that thinks a fact is wrong proposes a fact (`wiki propose`); it does not correct. The
    attempt is recorded, changes nothing, is not rendered as a note and is not counted as missing."""
    two_moves(project)
    capture(project)
    done = status(project)["status:done"]
    before = (digest_and_edges(project), service.status(project)["capture"])
    refused = correct(project, done["edge_id"], "retract", by=None, session="s-agent")
    assert (refused.status, refused.reason) == ("refused", "agent-correction-use-propose")
    assert refused.record_event_id is not None
    (event,) = wiki_events(project, "wiki-correction")
    assert event["data"]["result"] == "refused" and event["data"]["actor"] == "agent"
    assert event["data"]["refusal"] == "agent-correction-use-propose"
    assert (digest_and_edges(project), service.status(project)["capture"]) == before
    result = capture(project, {event["id"]})
    assert result["notes"] == 0                                   # not rendered
    with store(project) as conn:
        assert conn.execute("SELECT COUNT(*) FROM docs WHERE doc_id LIKE ?",
                            ("raw/%%/%06d-%%" % event["id"],)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM corrections").fetchone()[0] == 0
    answer = service.query(project, "What is the status of op-8 task t-9?")
    assert answer["coverage"]["missing_event_ids_in_scope"] == []
    assert status(project)["status:done"]["status"] == "active"


# ------------------------------------------------------------------ time (architecture.md 5.1)
def test_a_world_change_without_effective_at_is_refused_and_changes_nothing(project):
    two_moves(project)
    capture(project)
    done = status(project)["status:done"]
    before = digest_and_edges(project)
    refused = correct(project, done["edge_id"], "world-change", replacement=new_node("status:archived"))
    assert (refused.status, refused.reason) == ("refused", "valid-time-required")
    assert digest_and_edges(project) == before
    (event,) = wiki_events(project, "wiki-correction")
    assert event["data"]["result"] == "refused" and event["data"]["actor"] == "owner"
    for bad, code in (("not a time", "valid-time-unreadable"), (T09, "effective-at-not-after-valid-from")):
        assert correct(project, done["edge_id"], "world-change", effective_at=bad,
                       replacement=new_node("status:archived")).reason == code
    assert digest_and_edges(project) == before
    with store(project) as conn:
        assert conn.execute("SELECT COUNT(*) FROM corrections").fetchone()[0] == 0


def test_a_world_change_ends_the_prior_at_effective_at_and_a_supersede_keeps_its_valid_from(project):
    """Neither takes the Writer clock: the prior's window ends at `effective_at` and the replacement
    starts there; a supersede gives the replacement the prior's `valid_from`."""
    two_moves(project, "t-1")
    two_moves(project, "t-2")
    capture(project)
    world = correct(project, status(project, "t-1")["status:done"]["edge_id"], "world-change",
                    effective_at="2026-03-01T19:00:00+07:00", replacement=new_node("status:archived"))
    assert world.status == "applied", world
    now = status(project, "t-1")
    assert (now["status:done"]["status"], now["status:done"]["valid_until"]) == (
        "active", "2026-03-01T12:00:00+00:00")
    assert now["status:archived"]["valid_from"] == "2026-03-01T12:00:00+00:00"
    assert now["status:archived"]["source_created_at"] == now["status:archived"]["valid_from"]
    assert now["status:done"]["superseded_at"] is None            # a world change, not a correction
    done2 = status(project, "t-2")["status:done"]
    fix = correct(project, done2["edge_id"], "supersede", replacement=new_node("status:blocked"))
    assert fix.status == "applied"
    again = status(project, "t-2")
    assert again["status:blocked"]["valid_from"] == done2["valid_from"] == T10
    assert again["status:done"]["valid_until"] is None and again["status:done"]["status"] == "invalidated"
    # the clocks of both writes are the correction events' own stamps, not the effective or valid times
    assert again["status:done"]["superseded_at"] is not None
    assert again["status:done"]["superseded_at"] != again["status:blocked"]["valid_from"]


def test_a_replacement_must_be_readable_and_only_a_replacing_action_takes_one(project):
    two_moves(project)
    capture(project)
    done = status(project)["status:done"]
    assert correct(project, done["edge_id"], "supersede").reason == "replacement-required"
    assert correct(project, done["edge_id"], "retract", replacement=new_node("x")).reason \
        == "replacement-not-allowed"
    assert correct(project, done["edge_id"], "retract", effective_at=T11).reason \
        == "effective-at-only-for-world-change"
    assert correct(project, done["edge_id"], "supersede",
                   replacement=new_node("status:a\nb")).reason == "replacement-unreadable"
    with pytest.raises(ValueError):
        correct(project, done["edge_id"], "supersede", replacement={"obj": "x", "extra": 1})
    assert status(project)["status:done"]["status"] == "active"


# ------------------------------------------------------------------ OB-14: a note re-render
def reworded(monkeypatch):
    """Fault injection: a renderer that words the `from:` line of a task-move note differently, as a
    later format change would (the line is inside the quoted span of the status fact)."""
    original = drain._note_doc

    def render(ev, prefixes=()):
        doc_id, text = original(ev, prefixes=prefixes)
        return doc_id, text.replace("\nfrom: ", "\nfrom: re-") if ev.get("kind") == "task-move" else text

    monkeypatch.setattr(drain, "_note_doc", render)


def correction_scenario(project):
    ids = {}
    for task in ("t-1", "t-2", "t-3"):
        ids[task] = two_moves(project, task)
    capture(project)
    retract = correct(project, status(project, "t-1")["status:done"]["edge_id"], "retract")
    fix = correct(project, status(project, "t-2")["status:done"]["edge_id"], "supersede",
                  replacement=new_node("status:blocked"))
    world = correct(project, status(project, "t-3")["status:done"]["edge_id"], "world-change",
                    effective_at=T11, replacement=new_node("status:archived"))
    assert [r.status for r in (retract, fix, world)] == ["applied"] * 3
    return ids


def test_correction_survives_note_rerender(project, monkeypatch):
    """OB-14: retract, supersede and world-change corrections on task-move status facts, then a
    re-render of their event notes, live and through a rebuild. The retracted fact stays retracted, the
    replacement stays active and is not invalidated by the re-emitted fact, no correction turns
    stale, and accepted_digest is equal."""
    ids = correction_scenario(project)
    before = digest_of(project)
    reworded(monkeypatch)
    moves = {i for pair in ids.values() for i in pair}
    # Merge of op-008 into main, review B4: a capture keeps a stored note in its old rendering; the
    # re-render is the owner's job (`wiki recover --rerender-notes`), which this test pins.
    assert drain.capture(str(project), events(project, moves))["kept_renderings"] == len(moves)
    assert digest_of(project) == before
    result = drain.capture(str(project), events(project, moves), rerender_notes=True)
    assert result["rules"]["reanchored"] >= 3                     # the facts followed their notes
    # the retracted fact is one retracted edge, on the new quote, and no active copy came back
    all_one = [e for e in edges(project, predicate="status", subj_node="t-1")]
    assert [(e["obj_node"], e["status"]) for e in all_one if e["obj_node"] == "status:done"] \
        == [("status:done", "retracted")]
    assert "from: re-" in [e for e in all_one if e["obj_node"] == "status:done"][0]["source_quote"]
    # the replacement is active and the target stays invalidated, with no second done edge
    two = {}
    for e in edges(project, predicate="status", subj_node="t-2"):
        two.setdefault(e["obj_node"], []).append(e["status"])
    assert two["status:blocked"] == ["active"] and two["status:done"] == ["invalidated"]
    three = {e["obj_node"]: (e["status"], e["valid_until"]) for e in edges(project, predicate="status",
                                                                         subj_node="t-3")}
    assert three["status:done"] == ("active", T11) and three["status:archived"] == ("active", None)
    with store(project) as conn:
        assert [r[0] for r in conn.execute("SELECT status FROM corrections")] == ["applied"] * 3
    live = digest_of(project)
    assert live != before                                  # the notes did change (a new quote hash)
    report = rebuild_store(project)["candidates"]
    assert report["corrections_applied"] == 3 and report["stale"] == []
    assert digest_of(project) == live
    with store(project) as conn:
        assert [r[0] for r in conn.execute("SELECT status FROM corrections")] == ["applied"] * 3


def test_a_rebuild_replays_every_action_to_an_equal_digest(project):
    for task in ("t-1", "t-2", "t-3", "t-4"):
        two_moves(project, task)
    capture(project)
    correct(project, status(project, "t-1")["status:done"]["edge_id"], "supersede",
            replacement=new_node("status:blocked"))
    correct(project, status(project, "t-2")["status:done"]["edge_id"], "world-change",
            effective_at=T11, replacement=new_node("status:archived"))
    correct(project, status(project, "t-3")["status:done"]["edge_id"], "retract")
    made = correct(project, status(project, "t-4")["status:done"]["edge_id"], "supersede",
                   replacement=new_node("status:blocked"))
    assert correct(project, made.effects["replacement_edge_id"], "revert").status == "applied"
    refused = correct(project, status(project, "t-1")["status:blocked"]["edge_id"], "retract",
                      by=None, session="s-agent")
    assert refused.status == "refused"
    live = digest_and_edges(project)
    report = rebuild_store(project)["candidates"]
    assert report["corrections_applied"] == 5 and report["stale"] == []
    assert report["skipped"] == {"not-applied": 1}
    assert digest_and_edges(project) == live


def test_a_correction_whose_target_is_gone_on_replay_is_marked_stale(project):
    """A correction resolves by identity: when the rebuilt store holds no such edge (here the note was
    withdrawn first, in the record before the correction ran in a different order), it is `stale`."""
    two_moves(project)
    capture(project)
    done = status(project)["status:done"]
    assert correct(project, done["edge_id"], "retract").status == "applied"
    # a second correction the record holds for the same fact, as if the fact had never been placed
    with store(project) as conn:
        row = dict(conn.execute("SELECT * FROM corrections").fetchone())
    ident = {"subj_node": row["target_subj"], "predicate": row["target_predicate"],
             "obj_key": "node:status:nowhere", "valid_from": row["target_valid_from"],
             "anchor_source_ref": row["target_source_ref"], "anchor_quote_sha256": ""}
    appender(project, "s-owner")("wiki-correction", {
        "actor": "owner", "session": "s-owner", "target": ident, "target_edge_id": None,
        "action": "retract", "reason": "x", "replacement": None, "effective_at": None,
        "result": "applied"})
    report = rebuild_store(project)["candidates"]
    assert report["corrections_applied"] == 1 and len(report["stale"]) == 1
    assert report["stale"][0]["reason"] == "unknown-target"
    with store(project) as conn:
        assert sorted(r[0] for r in conn.execute("SELECT status FROM corrections")) == ["applied", "stale"]


def test_correct_runs_with_sockets_blocked(project):
    two_moves(project)
    capture(project)
    done = status(project)["status:done"]
    with sockets_blocked():
        made = correct(project, done["edge_id"], "supersede", replacement=new_node("status:blocked"))
        report = rebuild_store(project)
    assert made.status == "applied" and report["candidates"]["corrections_applied"] == 1


def test_the_cli_verbs_correct_and_withdraw(project, capsys):
    """`wiki correct` and `wiki withdraw` bind the record appender (actor `alpaca-wiki`) to the
    caller's session and need `--session`; `correct` without `--by owner` is refused (exit 2)."""
    from alpaca import cli
    first, second = two_moves(project)
    capture(project)
    done = status(project)["status:done"]["edge_id"]
    capsys.readouterr()
    assert cli.main(["wiki", "correct", done, "--action", "retract", "--reason", "x"]) == 1
    assert "needs --session" in capsys.readouterr().err
    assert cli.main(["--session", "s-agent", "wiki", "correct", done, "--action", "retract",
                     "--reason", "x"]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "agent-correction-use-propose"
    assert cli.main(["--session", "s-owner", "wiki", "correct", done, "--action", "supersede",
                     "--reason", "x", "--by", "owner", "--obj", "status:blocked",
                     "--obj-datatype", "node"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "applied" and out["effects"]["action"] == "supersede"
    assert cli.main(["--session", "s-owner", "wiki", "withdraw", "event:%d" % first,
                     "--reason", "wrong note"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "applied" and out["effects"]["source_ref"] == "event:%d" % first
    assert cli.main(["--session", "s-owner", "wiki", "withdraw", "event:%d" % first,
                     "--reason", "wrong note"]) == 0                        # a duplicate is not a failure
    assert json.loads(capsys.readouterr().out)["status"] == "duplicate"
    assert {e["actor"] for e in events(project) if e["kind"].startswith("wiki-")} == {"alpaca-wiki"}


# ------------------------------------------------------------------ after check round 1: what a replacement may be
def windows(project, task="t-9"):
    """The active status edges of a task as (object, valid_from, valid_until), in time order."""
    return sorted((e["obj_node"] or e["obj_literal"], e["valid_from"], e["valid_until"])
                  for e in edges(project, predicate="status", subj_node=task, status="active"))


def current_at(project, ts, task="t-9"):
    """The statuses the readers serve as current at `ts`, through their own as-of clause."""
    from alpaca.wiki.store.asof import AsOf, asof_where_edges
    clause, params = asof_where_edges(AsOf(T=ts), "edges")
    with store(project) as conn:
        return sorted(r[0] for r in conn.execute(
            "SELECT obj_node FROM edges WHERE subj_node=:subj AND predicate='status' AND " + clause,
            dict(params, subj=task)))


def three_moves(project):
    for to, ts, frm in (("doing", T09, "open"), ("done", T10, "doing"), ("review", T11, "done")):
        move(project, to, ts, frm=frm)
    capture(project)


def test_a_replacement_with_an_unknown_predicate_is_refused_before_it_is_recorded_as_applied(project):
    """A predicate that is not in `predicates` cannot be written. The correction used to be recorded as
    applied and then to fail on a foreign key, which left an act in the record that no rebuild could
    apply. Now it is refused first, and the record says so."""
    two_moves(project)
    capture(project)
    done = status(project)["status:done"]
    before = digest_and_edges(project)
    for action, extra in (("supersede", {}), ("world-change", {"effective_at": T11})):
        refused = correct(project, done["edge_id"], action, replacement={
            "predicate": "statuss", "obj": "blocked", "obj_datatype": "string"}, **extra)
        assert (refused.status, refused.reason) == ("refused", "unknown-predicate"), action
    assert digest_and_edges(project) == before
    assert [(e["data"]["result"], e["data"]["refusal"]) for e in wiki_events(project, "wiki-correction")] \
        == [("refused", "unknown-predicate")] * 2
    with store(project) as conn:
        assert conn.execute("SELECT COUNT(*) FROM corrections").fetchone()[0] == 0
    report = rebuild_store(project)
    assert report["status"] == "ok" and report["candidates"]["corrections_applied"] == 0
    assert digest_and_edges(project) == before


def poisoned_correction(project, edge, predicate="statuss"):
    """What an earlier build left in the record: an applied owner correction whose replacement names a
    predicate the store does not have. Appended as the verb would have appended it."""
    with store(project) as conn:
        identity = intake.fact_identity(conn, edge)
    identity["anchor_quote_sha256"] = ""                      # an event anchor, as `correct` stores it
    replacement = {"subject": edge["subj_node"], "predicate": predicate, "obj": "blocked",
                   "obj_datatype": "string"}
    return appender(project, "s-owner")("wiki-correction", {
        "actor": "owner", "session": "s-owner", "target": identity, "target_edge_id": edge["edge_id"],
        "action": "supersede", "reason": "typo", "replacement": replacement, "effective_at": None,
        "result": "applied", "correction_id": "k_poison", "statement": "t-1 %s blocked" % predicate})


def test_a_recorded_correction_that_cannot_be_applied_replays_as_stale_and_the_rebuild_goes_on(project):
    two_moves(project, "t-1")
    two_moves(project, "t-2")
    capture(project)
    poison = poisoned_correction(project, status(project, "t-1")["status:done"])
    # the live store has not processed the act an earlier build left: the doors answer
    # store-behind-record until `wiki recover` (t-070 round 2 C-E), which keeps it as stale
    held = correct(project, status(project, "t-2")["status:done"]["edge_id"], "supersede",
                   replacement=new_node("status:blocked"))
    assert (held.status, held.reason) == ("unavailable", "store-behind-record")
    from alpaca.wiki import recovery
    assert recovery.replay(project)["candidates"]["stale"][0]["event"] == poison
    fine = correct(project, status(project, "t-2")["status:done"]["edge_id"], "supersede",
                   replacement=new_node("status:blocked"))
    assert fine.status == "applied"
    report = rebuild_store(project)
    assert report["status"] == "ok"
    done = report["candidates"]
    assert done["corrections_applied"] == 1
    assert [(s["event"], s["reason"]) for s in done["stale"]] == [(poison, "unknown-predicate")]
    assert status(project, "t-1")["status:done"]["status"] == "active"
    assert status(project, "t-2")["status:blocked"]["status"] == "active"
    with store(project) as conn:
        assert sorted(r[0] for r in conn.execute("SELECT status FROM corrections")) == ["applied", "stale"]
    assert rebuild_store(project)["status"] == "ok"           # and again, as often as the record is read


def test_a_recorded_correction_whose_write_fails_replays_as_stale_and_changes_nothing(project, monkeypatch):
    """Fault injection: the write of one replacement raises the foreign key error the poisoned record
    used to cause. The rebuild keeps that act as a stale correction, writes nothing of it, and applies
    the others."""
    import sqlite3
    from alpaca.wiki.store.write import Writer
    two_moves(project, "t-1")
    two_moves(project, "t-2")
    capture(project)
    first = correct(project, status(project, "t-1")["status:done"]["edge_id"], "supersede",
                    replacement=new_node("status:blocked"))
    second = correct(project, status(project, "t-2")["status:done"]["edge_id"], "supersede",
                     replacement=new_node("status:blocked"))
    assert (first.status, second.status) == ("applied", "applied")
    real = Writer.upsert_edge

    def failing(self, e):
        if e.get("extractor_version") == "owner-correction/1" and e.get("subj_node") == "t-1":
            raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")
        return real(self, e)

    monkeypatch.setattr(Writer, "upsert_edge", failing)
    report = rebuild_store(project)
    assert report["status"] == "ok"
    done = report["candidates"]
    assert done["corrections_applied"] == 1
    assert [(s["event"], s["reason"]) for s in done["stale"]] == [
        (first.record_event_id, "apply-failed:IntegrityError")]
    one = status(project, "t-1")
    assert one["status:done"]["status"] == "active" and "status:blocked" not in one
    assert status(project, "t-2")["status:blocked"]["status"] == "active"
    with store(project) as conn:
        assert sorted(r[0] for r in conn.execute("SELECT status FROM corrections")) == ["applied", "stale"]


def test_a_supersede_of_a_fact_that_a_later_edge_ended_keeps_its_window(project):
    """The replacement takes the target's window. Moves doing, done, review; the `done` fact is
    corrected to blocked: blocked holds from 10:00 to 11:00, where `review` starts, and no reader sees
    two current statuses. A revert gives `done` back with its window."""
    three_moves(project)
    done = status(project)["status:done"]
    assert (done["valid_from"], done["valid_until"]) == (T10, T11)
    made = correct(project, done["edge_id"], "supersede", replacement=new_node("status:blocked"))
    assert made.status == "applied", made
    assert windows(project) == [("status:blocked", T10, T11), ("status:doing", T09, T10),
                                ("status:review", T11, None)]
    assert current_at(project, "2026-03-01T10:30:00+00:00") == ["status:blocked"]
    assert current_at(project, "2026-03-01T12:30:00+00:00") == ["status:review"]
    live = digest_of(project)
    assert rebuild_store(project)["candidates"]["corrections_applied"] == 1
    assert digest_of(project) == live
    back = correct(project, made.effects["replacement_edge_id"], "revert", reason="done was right")
    assert back.status == "applied", back
    assert windows(project) == [("status:doing", T09, T10), ("status:done", T10, T11),
                                ("status:review", T11, None)]
    assert current_at(project, "2026-03-01T12:30:00+00:00") == ["status:review"]


def test_a_world_change_of_a_fact_that_a_later_edge_ended_ends_the_replacement_where_the_target_ended(project):
    """Moves doing, done, review; the world changed to `open` at 09:30, inside the window of `doing`
    (09:00 to 10:00): `doing` ends at 09:30 and `open` holds from 09:30 to 10:00. A change at or after
    the end of the target's window is refused: the target was not the fact in force then."""
    three_moves(project)
    doing = status(project)["status:doing"]
    assert (doing["valid_from"], doing["valid_until"]) == (T09, T10)
    for late in (T10, T11, "2026-03-01T13:00:00+00:00"):
        refused = correct(project, doing["edge_id"], "world-change", effective_at=late,
                          replacement=new_node("status:open"))
        assert (refused.status, refused.reason) == ("refused", "effective-at-not-before-valid-until")
    half = "2026-03-01T09:30:00+00:00"
    made = correct(project, doing["edge_id"], "world-change", effective_at=half,
                   replacement=new_node("status:open"))
    assert made.status == "applied", made
    assert windows(project) == [("status:doing", T09, half), ("status:done", T10, T11),
                                ("status:open", half, T10), ("status:review", T11, None)]
    assert current_at(project, "2026-03-01T09:45:00+00:00") == ["status:open"]
    assert current_at(project, "2026-03-01T10:30:00+00:00") == ["status:done"]
    live = digest_of(project)
    assert rebuild_store(project)["candidates"]["corrections_applied"] == 1
    assert digest_of(project) == live
    back = correct(project, made.effects["replacement_edge_id"], "revert", reason="no change then")
    assert back.status == "applied"
    assert windows(project) == [("status:doing", T09, T10), ("status:done", T10, T11),
                                ("status:review", T11, None)]


# ------------------------------------------------------------------ check round 2: record first, in one transaction
T1030 = "2026-03-01T10:30:00+00:00"


def other_session_done(project):
    """Another session's move of t-9 from doing to done at 10:00, appended to the record and not captured."""
    return at(project, "task-move", {"from": "doing", "to": "done", "proof": None, "reason": None}, T10,
              op="op-8", ref="t-9", actor="human", session="s-other")


def correction_rows(project):
    with store(project) as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT correction_id, action, status, record_event_id FROM corrections ORDER BY correction_id")]


def test_a_correction_meets_the_store_a_replay_meets_and_is_refused_before_it_is_recorded(project):
    """Check round 2 (invariants B1, reproduction 1): doing at 09:00 is captured; another session's move to
    done at 10:00 is in the record and not captured; the owner records a world change of doing at 10:30.
    `correct` lands every uncaptured event first, as `admit` does, so it plans against the store a replay
    meets at this place in the record: doing ended at 10:00, so the change at 10:30 is refused before
    anything is recorded as applied. The record says refused, no corrections row is written, the event is
    no source, the statement is served nowhere, a retry is refused again (not a duplicate), and a rebuild
    gives the same accepted digest."""
    move(project, "doing", T09)
    capture(project)
    doing = status(project)["status:doing"]
    other_session_done(project)
    first = correct(project, doing["edge_id"], "world-change", effective_at=T1030,
                    replacement=new_node("status:blocked"))
    assert (first.status, first.reason) == ("refused", "effective-at-not-before-valid-until"), first
    (event,) = wiki_events(project, "wiki-correction")
    assert (event["data"]["result"], event["data"]["refusal"]) == ("refused", "effective-at-not-before-valid-until")
    assert not drain.renders_as_source(event["kind"], event["data"])
    assert correction_rows(project) == []
    assert status(project)["status:doing"]["valid_until"] == T10      # the move landed first
    with store(project) as conn:
        assert conn.execute("SELECT COUNT(*) FROM blocks WHERE status='active' AND text LIKE ?",
                            ("%statement: t-9 status status:blocked%",)).fetchone()[0] == 0
    assert "status:blocked" not in service.context(project, "Which status did the owner give task t-9, blocked?")
    retry = correct(project, doing["edge_id"], "world-change", effective_at=T1030,
                    replacement=new_node("status:blocked"))
    assert (retry.status, retry.reason) == ("refused", "effective-at-not-before-valid-until")
    live = digest_of(project)
    assert rebuild_store(project)["candidates"]["corrections_applied"] == 0
    assert digest_of(project) == live


def test_a_source_event_appended_after_the_capture_step_makes_the_correction_unavailable(project, monkeypatch):
    """Fault injection: another session appends its move right after the capture step of `correct` (the
    monkeypatch runs the real step, then appends the move). Once the store's write lock is held, `correct`
    finds that event uncaptured and answers `unavailable` `capture-behind`, recording nothing and writing
    nothing; sent again, the move lands first and the change is refused, as a replay would refuse it."""
    move(project, "doing", T09)
    capture(project)
    doing = status(project)["status:doing"]
    before = digest_and_edges(project)
    real = service._capture_before_admission
    appended = []

    def then_another_session_appends(root, cfg):
        out = real(root, cfg)
        if not appended:
            appended.append(other_session_done(project))
        return out

    monkeypatch.setattr(service, "_capture_before_admission", then_another_session_appends)
    late = correct(project, doing["edge_id"], "world-change", effective_at=T1030,
                   replacement=new_node("status:blocked"))
    assert (late.status, late.reason, late.record_event_id) == ("unavailable", "capture-behind", None), late
    assert late.effects["first_missing_event"] == appended[0]
    assert wiki_events(project, "wiki-correction") == [] and correction_rows(project) == []
    assert digest_and_edges(project) == before
    monkeypatch.undo()
    again = correct(project, doing["edge_id"], "world-change", effective_at=T1030,
                    replacement=new_node("status:blocked"))
    assert (again.status, again.reason) == ("refused", "effective-at-not-before-valid-until")


def test_the_plan_the_note_and_the_apply_hold_one_write_lock_of_the_store(project, monkeypatch):
    """No other capture can land between the plan and the apply: `correct` plans, appends, lands its note
    and applies in one transaction of the store. Probed by a second connection that asks for the write
    lock without waiting, at the plan, at the capture of the note and at the apply: each time it is
    refused, `database is locked`."""
    import sqlite3
    from alpaca.wiki.config import Config
    from alpaca.wiki.store.write import Writer
    two_moves(project)
    capture(project)
    done = status(project)["status:done"]
    db_path = str(Config.for_project(project).db_path)
    seen = []

    def probe(where):
        other = sqlite3.connect(db_path, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
            seen.append((where, "free"))
        except sqlite3.OperationalError as exc:
            seen.append((where, "locked" if "locked" in str(exc) else str(exc)))
        finally:
            other.close()

    def wrap(owner, name, where):
        real = getattr(owner, name)

        def probed(*args, **kw):
            probe(where)
            return real(*args, **kw)
        monkeypatch.setattr(owner, name, probed)

    wrap(Writer, "plan_correction", "plan")
    wrap(drain, "capture", "note")
    wrap(Writer, "apply_correction", "apply")
    made = correct(project, done["edge_id"], "supersede", replacement=new_node("status:blocked"))
    assert made.status == "applied", made
    assert [w for w, _ in seen][:3] == ["plan", "note", "apply"], seen
    assert {state for _, state in seen} == {"locked"}, seen


def test_a_stale_row_is_not_a_duplicate_and_the_same_correction_applies_once_its_plan_holds(project):
    """Check round 2 (invariants B1): the record holds an applied world change of doing at 10:30 that an
    earlier build recorded and that cannot apply (done started at 10:00), so a rebuild keeps it as a
    `stale` row. Once the done note is withdrawn the same correction (the same id) holds, and sent again it
    is applied: a stale row is not a duplicate. The row becomes `applied` with the new event, and a rebuild
    gives the same accepted digest."""
    from alpaca.wiki.clock import to_clock_format
    from alpaca.wiki.store.write import Writer
    _, second = two_moves(project)
    capture(project)
    doing = status(project)["status:doing"]
    with store(project) as conn:
        identity = intake.fact_identity(conn, doing)
    identity["anchor_quote_sha256"] = ""                      # an event anchor, as `correct` stores it
    half = to_clock_format(T1030)
    replacement = {"subject": "t-9", "predicate": "status", "obj": "status:blocked", "obj_datatype": "node"}
    cid = Writer.correction_id(identity, "world-change", replacement, half)
    appender(project, "s-owner")("wiki-correction", {
        "actor": "owner", "session": "s-owner", "target": identity, "target_edge_id": doing["edge_id"],
        "action": "world-change", "reason": "as an earlier build recorded it", "replacement": replacement,
        "effective_at": half, "result": "applied", "correction_id": cid,
        "statement": "t-9 status status:blocked"})
    report = rebuild_store(project)["candidates"]
    assert [s["reason"] for s in report["stale"]] == ["effective-at-not-before-valid-until"]
    assert [(r[0], r[2]) for r in correction_rows(project)] == [(cid, "stale")]
    gone = service.withdraw_source(project, "event:%d" % second, session="s-owner",
                                   record=appender(project, "s-owner"), reason="no such move")
    assert gone.status == "applied"
    made = correct(project, doing["edge_id"], "world-change", effective_at=T1030,
                   replacement=new_node("status:blocked"))
    assert (made.status, made.effects["correction_id"]) == ("applied", cid), made
    assert correction_rows(project) == [(cid, "world-change", "applied", made.record_event_id)]
    assert windows(project) == [("status:blocked", half, None), ("status:doing", T09, half)]
    live = digest_of(project)
    report = rebuild_store(project)["candidates"]
    assert report["corrections_applied"] == 1 and len(report["stale"]) == 1
    assert digest_of(project) == live


def test_a_note_that_does_not_land_leaves_the_store_unchanged_and_the_answer_unavailable(project, monkeypatch):
    """Check round 2 (invariants B1, reproduction 2). Fault injection: the capture of the correction's own
    note lands nothing (the monkeypatch makes `service.capture`, which the earlier build used for the note,
    and the drain capture that `correct` now runs in its transaction defer it). The act is in the record,
    which is append-only; the store takes none of it, no `stale` row is written, and the answer is
    `unavailable` with the reason. A replay (`wiki recover`) lands the note and applies the act."""
    two_moves(project)
    capture(project)
    done = status(project)["status:done"]
    before = digest_and_edges(project)
    real_drain = drain.capture

    def nothing(events):
        return {"deferred": {"through_event": 0, "remaining": len(events)}, "notes": 0, "ingested": 0,
                "skipped": 0, "docs": [], "transcripts": [], "missing_transcripts": []}

    def drain_lands_nothing(root, events, **kw):
        return nothing(events) if kw.get("absorber") is not None else real_drain(root, events, **kw)

    monkeypatch.setattr(drain, "capture", drain_lands_nothing)
    monkeypatch.setattr(service, "capture", lambda root, **kw: nothing(kw.get("events") or ()))
    made = correct(project, done["edge_id"], "supersede", replacement=new_node("status:blocked"))
    assert (made.status, made.reason) == ("unavailable", "note-not-landed"), made
    (event,) = wiki_events(project, "wiki-correction")
    assert made.record_event_id == event["id"] and event["data"]["result"] == "applied"
    assert correction_rows(project) == []
    assert digest_and_edges(project) == before
    monkeypatch.undo()
    report = rebuild_store(project)["candidates"]
    assert report["corrections_applied"] == 1 and report["stale"] == []
    assert status(project)["status:blocked"]["status"] == "active"


def test_an_identical_correction_that_commits_first_makes_the_second_a_duplicate(project, monkeypatch):
    """Two owners send the same correction at once; the other one commits while this one lands what the
    store is behind on (the monkeypatch runs the other call to its end inside the capture step of this
    one). This one then holds the store, finds the applied row and answers `duplicate`: it records no
    second applied event, so no second source note, and the store has one row."""
    two_moves(project)
    capture(project)
    done = status(project)["status:done"]
    real = service._capture_before_admission
    other = []

    def the_other_owner_commits_first(root, cfg):
        out = real(root, cfg)
        if not other:
            other.append(None)
            other[0] = correct(project, done["edge_id"], "supersede", replacement=new_node("status:blocked"),
                               session="s-owner-2")
        return out

    monkeypatch.setattr(service, "_capture_before_admission", the_other_owner_commits_first)
    mine = correct(project, done["edge_id"], "supersede", replacement=new_node("status:blocked"))
    assert other[0].status == "applied"
    assert (mine.status, mine.reason, mine.record_event_id) == ("duplicate", "already-applied", None), mine
    assert [e["data"]["result"] for e in wiki_events(project, "wiki-correction")] == ["applied"]
    assert [r[2] for r in correction_rows(project)] == ["applied"]
    with store(project) as conn:
        assert conn.execute("SELECT COUNT(*) FROM blocks WHERE status='active' AND text LIKE ?",
                            ("%statement: t-9 status status:blocked%",)).fetchone()[0] == 1
