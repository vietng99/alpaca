"""op-008 t-065 stage s3-graph: the record rules (design/rune2-memory/architecture.md 8.1; FR-14, FR-34;
OB-08 named test, OB-16, OB-17).

Model-free extraction maps the structured fields of a captured event note to typed edges: the graph
the PPR arm walks. Each output is an edge with a quote that is the smallest header span holding its
subject, object and `ts:` lines, a support claim `rule:<rule_id>@<event>`, and a runtime check that
every value it read sits whole on its labelled line inside the quote (bar Z10). A failing output is
its own quarantined row (OB-16). A re-rendered note keeps its fact (OB-17). Real drain, real write
path, no mock; the one monkeypatch (a rule made to emit a bad quote) says so where it is used.
"""
import dataclasses
import json
import os
import sys
from contextlib import closing
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import append, capture, events, set_config, store, vault  # noqa: E402

from alpaca import db as record                                  # noqa: E402
from alpaca.wiki import service                                  # noqa: E402
from alpaca.wiki.clock import FixedClock, to_clock_format        # noqa: E402
from alpaca.wiki.config import Config                            # noqa: E402
from alpaca.wiki.determinism import normalize_text, sha256_hex   # noqa: E402
from alpaca.wiki.digest import accepted_digest                   # noqa: E402
from alpaca.wiki.ingest import drain, record_extract             # noqa: E402
from alpaca.wiki.ingest.absorb import Absorber                   # noqa: E402
from alpaca.wiki.store import query as q                         # noqa: E402
from alpaca.wiki.store.asof import AsOf                          # noqa: E402
from alpaca.wiki.store.db import DB                              # noqa: E402

TS = "2026-03-01T12:00:00+07:00"


def _at(project, kind, data, ts=TS, *, session="s-rec", op="op-8", ref=None, actor="agent"):
    """One record event whose `ts` is the given instant."""
    conn = record.connect(str(project))
    try:
        return record.append_event(conn, session=session, actor=actor, kind=kind, op=op, ref=ref,
                                   data=data, clock=FixedClock(ts))["id"]
    finally:
        conn.close()


def _edges(project, predicate=None, status="active"):
    with store(project) as conn:
        sql, args = "SELECT * FROM edges", []
        clauses = []
        if predicate:
            clauses.append("predicate=?")
            args.append(predicate)
        if status:
            clauses.append("status=?")
            args.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return [dict(r) for r in conn.execute(sql + " ORDER BY edge_id", args)]


def _claims(project, edge_id=None):
    with store(project) as conn:
        where, args = ("WHERE edge_id=?", (edge_id,)) if edge_id else ("", ())
        return [dict(r) for r in conn.execute(
            "SELECT * FROM edge_origin %s ORDER BY edge_id, claim_ref" % where, args)]


def _note(project, event_id):
    """(doc_id, text) of an event's note in the vault."""
    (path,) = [p for p in vault(project).rglob("%06d-*.md" % event_id) if "raw" in p.parts]
    return path.relative_to(vault(project)).as_posix(), path.read_text(encoding="utf-8")


def _header_lines(project, event_id):
    return _note(project, event_id)[1].split("\n\n", 1)[0].split("\n")


def _rerender(project, event_id, edit):
    """Absorb an edited copy of an event's note under the same doc id, as a later renderer would."""
    doc_id, text = _note(project, event_id)
    new = edit(text)
    assert new != text
    # Merge of op-008 into main, review B4: the shrink guard is waived for a smaller event note only
    # when the caller asks; the owner's re-render (`drain.capture(rerender_notes=True)`, `wiki
    # recover --rerender-notes`) passes this waiver, so the later renderer here passes it too.
    with closing(Absorber(Config.for_project(str(project)))) as absorber:
        result = absorber.absorb_text(doc_id, new, kind="raw", path=doc_id,
                                      waive_reason=drain.RERENDER_WAIVER)
    return doc_id, new, result


def _counts(project):
    with store(project) as conn:
        return {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                for t in ("docs", "blocks", "nodes", "aliases", "edges", "edge_corroborations",
                          "edge_origin", "ingest_event", "source_versions")}


def _digest(project):
    with store(project) as conn:
        return accepted_digest(conn)


def _by_pred(project, predicate):
    return {(e["subj_node"], e["obj_node"] or e["obj_literal"]): e for e in _edges(project, predicate)}


def _move(project, task="t-9", frm="doing", to="done", proof=None, ts=TS, op="op-8"):
    data = {"from": frm, "to": to, "proof": proof, "reason": None}
    return _at(project, "task-move", data, ts, op=op, ref=task, actor="human")


# ------------------------------------------------------------------ one test per rule
def test_task_add_places_in_op_and_its_span_starts_at_op(project):
    eid = _at(project, "task-add", {"title": "x", "statement": "y"}, ref="t-9", op="op-8")
    capture(project)
    (edge,) = _edges(project, "in_op")
    assert (edge["subj_node"], edge["obj_node"], edge["obj_datatype"]) == ("t-9", "op-8", "node")
    lines = edge["source_quote"].split("\n")
    assert lines[0].startswith("op: op-8") and lines[-1] == "ts: " + TS
    assert "task: t-9" in lines
    header = _header_lines(project, eid)
    assert lines == header[header.index(lines[0]):header.index(lines[-1]) + 1]
    assert edge["valid_from"] == to_clock_format(TS) == edge["source_created_at"]
    assert (edge["extractor"], edge["extractor_version"]) == ("deterministic", "record-rules/1")


def test_task_contract_places_in_op_and_a_decision_source(project):
    _at(project, "task-contract", {"source": "dec-38940e1fb35f", "done_bar": ["x"]}, ref="t-9")
    _at(project, "task-contract", {"source": "chat 2026-10-01", "done_bar": ["x"]}, ref="t-8")
    capture(project)
    assert set(_by_pred(project, "in_op")) == {("t-9", "op-8"), ("t-8", "op-8")}
    ((subj, obj),) = _by_pred(project, "sourced_from")
    assert (subj, obj) == ("t-9", "dec-38940e1fb35f")      # a free-text source makes no edge


def test_task_move_places_status_with_the_smallest_header_span(project):
    """The plan's worked case: a real task-move rendered by the format-2 renderer passes the field
    check and places an active `status` edge whose quote is `task:` through `ts:`."""
    eid = _move(project, proof="local:p.md")
    capture(project)
    (edge,) = _edges(project, "status")
    assert (edge["subj_node"], edge["obj_node"]) == ("t-9", "status:done")
    assert edge["source_quote"].split("\n") == [
        "task: t-9", "from: doing", "to: done", "proof: local:p.md", "ts: " + TS]
    (out,) = [o for o in service.record_facts_preview(project, from_event=eid, through_event=eid)
              if o.rule_id.endswith("/status@1")]
    assert out.fields_in_quote and out.missing == ()
    (claim,) = _claims(project, edge["edge_id"])
    assert claim["claim_ref"] == "rule:task-move/status@1@%d" % eid
    assert (claim["origin"], claim["rule_id"], claim["source_event_id"], claim["fact_kind"],
            claim["status"]) == ("record-rule", "task-move/status@1", eid, "record-fact", "live")
    assert claim["source_ref"] == "event:%d" % eid
    assert claim["quote_sha256"] == sha256_hex(edge["source_quote"])


def test_a_later_status_ends_the_earlier_window(project):
    _move(project, frm="open", to="doing", ts="2026-03-01T10:00:00+07:00")
    _move(project, frm="doing", to="done", ts="2026-03-01T11:00:00+07:00")
    capture(project)
    by_obj = {e["obj_node"]: e for e in _edges(project, "status")}
    assert by_obj["status:doing"]["valid_until"] == by_obj["status:done"]["valid_from"]
    assert by_obj["status:done"]["valid_until"] is None


def test_a_status_sequence_is_one_current_status_and_no_contradicts_edge(project):
    """op-008 t-065 s6-latency (bar A1). Successive `task-move` statuses of one task are changes of
    the world over time, not sources that disagree: each later status ends the earlier window at its
    own valid time and links to it, one status is current, and no edge carries the `contradicts`
    verdict that the overturner query, lucid and health read as an open contention."""
    for frm, to, hour in (("open", "doing", 10), ("doing", "review", 11), ("review", "done", 12)):
        _move(project, frm=frm, to=to, ts="2026-03-01T%02d:00:00+07:00" % hour)
    capture(project)
    edges = _edges(project, "status")
    by_obj = {e["obj_node"]: e for e in edges}
    assert list(by_obj) and set(by_obj) == {"status:doing", "status:review", "status:done"}
    assert [e["obj_node"] for e in edges if e["valid_until"] is None] == ["status:done"]
    assert by_obj["status:doing"]["valid_until"] == by_obj["status:review"]["valid_from"]
    assert by_obj["status:review"]["valid_until"] == by_obj["status:done"]["valid_from"]
    assert by_obj["status:review"]["supersedes_edge_id"] == by_obj["status:doing"]["edge_id"]
    assert by_obj["status:done"]["supersedes_edge_id"] == by_obj["status:review"]["edge_id"]
    assert all(e["superseded_at"] is None for e in edges)           # a world change, not a correction
    assert {e["reconcile_verdict"] for e in _edges(project, status=None)} == {"novel"}
    with store(project) as conn:
        assert conn.execute("SELECT COUNT(*) FROM edges WHERE reconcile_verdict='contradicts'"
                            ).fetchone()[0] == 0
    # the read behind the oracle's OVERTURNER_QUERY finds nothing on the task or its status nodes:
    # the door has no unread overturner to turn into a `conflict` (the first after-run: 52 such edges)
    with closing(DB(Config.for_project(str(project)))) as db:
        nodes = ["t-9", "status:open", "status:doing", "status:review", "status:done"]
        assert q.overturners_for_nodes(db, nodes, AsOf(T="2027-01-01T00:00:00+00:00")) == []
        # and an earlier time reads the earlier status, a later time the latest, on the domain axis
        at = lambda hour: {e["obj_node"] for e in q.active_edges_for_node(
            db, "t-9", AsOf(T=to_clock_format("2026-03-01T%02d:30:00+07:00" % hour)))
            if e["predicate"] == "status"}
        assert (at(10), at(11), at(12)) == ({"status:doing"}, {"status:review"}, {"status:done"})


def test_a_status_at_the_same_instant_is_still_the_next_state_not_a_contradiction(project):
    """The record is ordered by event id: two moves stamped alike are two states, the later one
    current. The earlier edge is invalidated by the reconcile rule for an equal valid time (nothing
    of it is current), and neither edge reads as an open contention."""
    _move(project, frm="open", to="doing")
    _move(project, frm="doing", to="done")
    capture(project)
    active = _edges(project, "status")
    assert [e["obj_node"] for e in active] == ["status:done"]
    assert active[0]["reconcile_verdict"] == "novel" and active[0]["valid_until"] is None
    assert [e["obj_node"] for e in _edges(project, "status", status="invalidated")] == ["status:doing"]
    with store(project) as conn:
        assert conn.execute("SELECT COUNT(*) FROM edges WHERE reconcile_verdict='contradicts'"
                            ).fetchone()[0] == 0


def test_a_record_status_over_a_fact_of_another_origin_stays_a_contradiction(project):
    """Only a prior that holds a live record-rule claim is the previous state of the same log. A
    status edge placed by another path (an agent fact, an ingested page) that the record then
    replaces is a source in disagreement with the record: the verdict stays `contradicts`."""
    from alpaca.wiki.ingest.encode import encode_candidate
    from alpaca.wiki.store.ledger import Ledger
    from alpaca.wiki.store.write import Writer
    vault(project).mkdir(parents=True, exist_ok=True)
    cfg = Config.for_project(str(project))
    db = DB(cfg)
    db.pour()
    writer = Writer(db, Ledger(cfg.ledger_path, vault_dir=cfg.vault_dir),
                    clock=FixedClock(start="2026-03-01T00:00:00+00:00", step=1))

    def candidate(obj, valid_from, block):
        return {"subj_node": "t-9", "predicate": "status", "obj_node": "status:" + obj,
                "obj_datatype": "node", "source_block_id": block, "source_doc_id": "raw/d.md",
                "source_quote": "task t-9 is %s" % obj, "extractor": "deterministic",
                "valid_from": valid_from, "source_created_at": valid_from}

    try:
        with writer.transaction():
            writer.upsert_doc("raw/d.md", "raw/d.md", "raw", "sha_d", private=0)
            for n, block in enumerate(("raw/d.md#0", "raw/d.md#1", "raw/d.md#2")):
                writer.upsert_block({"block_id": block, "block_content_id": "c%d" % n,
                                     "occurrence_index": 0, "doc_id": "raw/d.md", "ordinal": n,
                                     "text": "task t-9 is a status %d" % n})
            for node in ("t-9", "status:doing", "status:done", "status:review"):
                writer.upsert_node(node, "task" if node == "t-9" else "status", node)
            first = encode_candidate(db, writer, candidate("doing", "2026-03-01T10:00:00+00:00",
                                                           "raw/d.md#0"), "c0",
                                     successive_of="record-rule")
            assert first["verdict"] == "novel"                       # no record-rule claim yet
            second = encode_candidate(db, writer, candidate("done", "2026-03-01T11:00:00+00:00",
                                                            "raw/d.md#1"), "c1",
                                      successive_of="record-rule")
            assert (second["verdict"], second["action"]) == ("contradicts", "world-change")
            stored = db.conn.execute("SELECT reconcile_verdict FROM edges WHERE edge_id=?",
                                     (second["edge_id"],)).fetchone()[0]
            assert stored == "contradicts"
            # now the same step over a prior that holds a live record-rule claim
            writer.add_edge_origin(second["edge_id"], "rule:test@1", source_ref="event:1",
                                   quote="task t-9 is done")
            third = encode_candidate(db, writer, candidate("review", "2026-03-01T12:00:00+00:00",
                                                           "raw/d.md#2"), "c2",
                                     successive_of="record-rule")
            assert (third["verdict"], third["action"]) == ("contradicts", "world-change")
            stored = db.conn.execute("SELECT reconcile_verdict FROM edges WHERE edge_id=?",
                                     (third["edge_id"],)).fetchone()[0]
            assert stored == "novel"
            window = db.conn.execute("SELECT valid_until FROM edges WHERE edge_id=?",
                                     (second["edge_id"],)).fetchone()[0]
            assert window == "2026-03-01T12:00:00+00:00"            # the window still ends
    finally:
        db.close()


def test_task_move_with_a_proof_places_proved_by(project):
    _move(project, proof="local:.alpaca/proofs/op-8/t-9.md")
    _move(project, task="t-10", proof=None)                  # no proof: no line, no edge
    capture(project)
    ((subj, obj),) = _by_pred(project, "proved_by")
    assert (subj, obj) == ("t-9", "proof:.alpaca/proofs/op-8/t-9.md")


def test_decision_places_in_op_and_choice(project):
    _at(project, "decision", {"id": "dec-aaaaaa000001", "choice": "keep the lexical floor",
                              "context": "c", "consequence": "k"}, ref="dec-aaaaaa000001")
    capture(project)
    ((subj, obj),) = _by_pred(project, "in_op")
    assert (subj, obj) == ("dec-aaaaaa000001", "op-8")
    (choice,) = _edges(project, "choice")
    assert choice["obj_literal"] == "keep the lexical floor" and choice["obj_node"] is None
    assert choice["obj_datatype"] == "string"
    assert choice["source_quote"].split("\n")[0] == "id: dec-aaaaaa000001"


def test_a_decision_with_no_op_emits_no_in_op_edge(project):
    _at(project, "decision", {"id": "dec-aaaaaa000002", "choice": "ship it"}, op=None,
        ref="dec-aaaaaa000002")
    capture(project)
    assert _edges(project, "in_op") == []
    assert [e["obj_literal"] for e in _edges(project, "choice")] == ["ship it"]


def test_proof_report_places_proves(project):
    _at(project, "proof-report", {"path": ".alpaca/proofs/op-8/t-9.md", "sha256": "ab" * 32,
                                  "bytes": 5, "sections": [], "evidence": []}, ref="t-9")
    capture(project)
    ((subj, obj),) = _by_pred(project, "proves")
    assert (subj, obj) == ("proof:.alpaca/proofs/op-8/t-9.md", "t-9")


def test_a_proof_named_by_the_task_and_by_its_report_is_one_node(project):
    _move(project, proof="local:.alpaca/proofs/op-8/t-9.md")
    _at(project, "proof-report", {"path": ".alpaca/proofs/op-8/t-9.md", "sha256": "ab" * 32,
                                  "bytes": 5, "sections": [], "evidence": []}, ref="t-9")
    capture(project)
    with store(project) as conn:
        proofs = [r[0] for r in conn.execute("SELECT node_id FROM nodes WHERE node_id LIKE 'proof:%'")]
    assert proofs == ["proof:.alpaca/proofs/op-8/t-9.md"]


def test_drain_failed_places_failed_with_an_error_class(project):
    _at(project, "drain-failed", {"where": "session_end",
                                  "error": "TranscriptUnavailable: registered transcript is unavailable: s1"},
        session="s-fail", op=None, actor="alpaca")
    capture(project)
    ((subj, obj),) = _by_pred(project, "failed_with")
    assert (subj, obj) == ("s-fail", "error:TranscriptUnavailable")
    (edge,) = _edges(project, "failed_with")
    assert "error: TranscriptUnavailable: registered transcript is unavailable: s1" in edge["source_quote"]


def test_only_a_failing_verdict_or_run_places_failed_with(project):
    _at(project, "verdict", {"verdict": 0, "verdict_name": "PASS", "instrument": "i"}, ref="row-1")
    _at(project, "verdict", {"verdict": 1, "verdict_name": "FAIL", "instrument": "i"}, ref="row-2")
    _at(project, "run", {"gate": "alpaca-x", "verdict": "BLOCKED", "code": 2}, ref="instrument")
    _at(project, "run", {"gate": "alpaca-y", "verdict": "PAUSED-FOR-DECISION", "code": 3},
        ref="instrument")
    _at(project, "run", {"gate": "alpaca-z", "verdict": "PASS", "code": 0}, ref="instrument")
    capture(project)
    assert set(_by_pred(project, "failed_with")) == {("row-2", "error:FAIL"),
                                                     ("alpaca-x", "error:BLOCKED")}


def test_session_start_places_operator_as_a_literal(project):
    _at(project, "session-start", {"operator": "codex", "cwd": "/x"}, session="s-op", op=None,
        actor="codex")
    capture(project)
    ((subj, obj),) = _by_pred(project, "operator")
    assert (subj, obj) == ("s-op", "codex")
    (edge,) = _edges(project, "operator")
    assert edge["obj_node"] is None and edge["obj_datatype"] == "string"


def test_a_handoff_message_places_handed_off_to_and_a_note_does_not(project):
    _at(project, "msg", {"kind": "handoff", "to": "s-next", "body": "take over"}, session="s-a")
    _at(project, "msg", {"kind": "note", "to": "owner", "body": "fyi"}, session="s-a")
    capture(project)
    assert set(_by_pred(project, "handed_off_to")) == {("s-a", "s-next")}


def test_heartbeats_emit_nothing(project):
    _at(project, "heartbeat", {"fence": 1, "lease_until": "x", "ref": "t-9", "tool": "t",
                               "worker": "w"}, ref="t-9")
    report = capture(project)
    assert report["rules"]["outputs"] == 0
    assert _edges(project, status=None) == []
    with store(project) as conn:
        assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0


# ------------------------------------------------------------------ the check
def test_every_emitted_value_is_on_its_labelled_line_inside_its_quote(project):
    _move(project, proof="local:p.md")
    _at(project, "task-contract", {"source": "dec-38940e1fb35f"}, ref="t-9")
    _at(project, "decision", {"id": "dec-aaaaaa000003", "choice": "c"}, ref="dec-aaaaaa000003")
    _at(project, "msg", {"kind": "handoff", "to": "s-next", "body": "b"}, session="s-a")
    _at(project, "session-start", {"operator": "claude"}, session="s-a", op=None)
    capture(project)
    last = max(e["id"] for e in events(project))
    outputs = service.record_facts_preview(project, from_event=1, through_event=last)
    assert len(outputs) >= 8 and all(o.fields_in_quote and o.missing == () for o in outputs)
    for edge in _edges(project):
        lines = edge["source_quote"].split("\n")
        assert lines[-1].startswith("ts: ")
        assert all(": " in line for line in lines)           # every line of a quote is a labelled line


def test_a_from_line_never_satisfies_a_rule_that_reads_to(project):
    eid = _move(project, frm="done", to="done")
    capture(project)
    (out,) = [o for o in record_extract.extract(_note(project, eid)[1])
              if o.predicate == "status"]
    cut = dataclasses.replace(out, quote="\n".join(
        line for line in out.quote.split("\n") if not line.startswith("to: ")))
    assert "from: done" in cut.quote
    assert record_extract.check(cut) == ("object",)          # the `from:` value does not stand in
    # and a note with no `to:` line makes no status output at all
    text = _note(project, eid)[1].replace("\nto: done", "")
    assert [o for o in record_extract.extract(text) if o.predicate == "status"] == []


def test_an_output_whose_object_is_not_in_its_quote_is_quarantined_and_names_its_rule(project, monkeypatch):
    """The one monkeypatch: the status rule is made to cut the `to:` line out of its quote."""
    real = record_extract.extract

    def cut(text, rules=record_extract.RULES):
        return [dataclasses.replace(o, quote="\n".join(
            line for line in o.quote.split("\n") if not line.startswith("to: ")))
            if o.predicate == "status" else o for o in real(text, rules)]

    monkeypatch.setattr(record_extract, "extract", cut)
    eid = _move(project)
    report = capture(project)
    assert report["rules"]["failed"] == 1
    assert report["rules"]["failures"] == [{"rule_id": "task-move/status@1", "event": eid,
                                            "missing": ["object"]}]
    assert _edges(project, "status") == []                   # never placed active
    (row,) = _edges(project, "status", status="quarantined")
    assert (row["subj_node"], row["obj_node"]) == ("t-9", "status:done")
    (claim,) = _claims(project, row["edge_id"])
    assert claim["rule_id"] == "task-move/status@1" and claim["claim_ref"].endswith("@%d" % eid)


def test_a_quarantined_output_is_not_walked_by_the_graph(project, monkeypatch):
    from alpaca.wiki.store import adjacency
    from alpaca.wiki.store.asof import AsOf
    real = record_extract.extract
    monkeypatch.setattr(record_extract, "extract", lambda text, rules=record_extract.RULES: [
        dataclasses.replace(o, quote="task: t-9\nts: " + TS) for o in real(text, rules)])
    _move(project)
    capture(project)
    with store(project) as conn:
        class _Db:
            pass
        db = _Db()
        db.conn = conn
        assert adjacency.count_ppr_edges(db, AsOf("2030-01-01T00:00:00+00:00")) == 0


def test_a_private_note_emits_nothing(project):
    eid = _move(project)
    capture(project)
    doc_id, text = _note(project, eid)
    private_id = "wiki/private/" + doc_id
    with closing(Absorber(Config.for_project(str(project)))) as absorber:
        result = absorber.absorb_text(private_id, text.replace("op: op-8", "op: op-private"),
                                      kind="raw", path=private_id)
    assert result["rules"]["skipped"] == "private" and result["rules"]["outputs"] == 0
    with store(project) as conn:
        rows = conn.execute("SELECT source_doc_id FROM edges").fetchall()
    assert {r[0] for r in rows} == {doc_id}                  # the private copy placed no edge


def test_a_marking_trip_routes_the_note_private_and_no_rule_runs(project):
    _at(project, "decision", {"id": "dec-aaaaaa000004", "choice": "Handling NOFORN material: door code amber"},
        ref="dec-aaaaaa000004")
    report = capture(project)
    docs = {q["doc_id"] for q in report["quarantined"]}      # the note and its decision source
    assert "wiki/private/raw/op-8/000001-note.md" in docs
    assert _edges(project, status=None) == []


# ------------------------------------------------------------------ claims and corroboration
def test_a_matches_output_writes_its_support_claim(project):
    """A second source for the same fact corroborates the first edge, and its own claim sits on that
    edge: one edge, two claims, corroboration two."""
    first = _at(project, "task-add", {"title": "x"}, ref="t-9", ts="2026-03-01T09:00:00+07:00")
    second = _at(project, "task-contract", {"source": "chat"}, ref="t-9", ts="2026-03-01T09:30:00+07:00")
    report = capture(project)
    assert report["rules"]["supported"] == 1 and report["rules"]["placed"] == 1
    (edge,) = _edges(project, "in_op")
    assert edge["corroboration_count"] == 2
    claims = _claims(project, edge["edge_id"])
    assert [c["claim_ref"] for c in claims] == ["rule:task-add/in_op@1@%d" % first,
                                                "rule:task-contract/in_op@1@%d" % second]
    assert all(c["status"] == "live" for c in claims)


def test_replaying_a_note_places_nothing_twice(project):
    _move(project, proof="local:p.md")
    capture(project)
    before = _counts(project)
    report = capture(project)
    assert report["rules"]["placed"] == 0 and report["rules"]["unchanged"] == 2
    assert _counts(project) == before


# ------------------------------------------------------------------ OB-16
def _failing(monkeypatch):
    """Every output loses its object line from its quote, so the check fails."""
    real = record_extract.extract
    monkeypatch.setattr(record_extract, "extract", lambda text, rules=record_extract.RULES: [
        dataclasses.replace(o, quote="\n".join(
            line for line in o.quote.split("\n") if not line.startswith("to: ")))
        if o.predicate == "status" else o for o in real(text, rules)])


def test_failing_output_never_corroborates(project, monkeypatch):
    """OB-16: a failing output whose object equals an active edge's leaves that edge, its
    corroborations and its claims as they were, and its own row names the rule; a failing output whose
    edge id already names an edge writes nothing and is counted as failed."""
    good = _move(project, ts="2026-03-01T10:00:00+07:00")
    capture(project)
    (edge,) = _edges(project, "status")
    snapshot = (edge, _claims(project, edge["edge_id"]), _counts(project)["edge_corroborations"])
    _failing(monkeypatch)
    bad = _move(project, ts="2026-03-01T10:05:00+07:00")      # same task, same object `done`
    report = capture(project, {bad})
    assert report["rules"]["failed"] == 1
    (edge_after,) = _edges(project, "status")
    assert edge_after == snapshot[0]                           # corroboration_count and all
    assert _claims(project, edge["edge_id"]) == snapshot[1]
    assert _counts(project)["edge_corroborations"] == snapshot[2] + 1     # only the quarantined row's own
    (row,) = _edges(project, "status", status="quarantined")
    assert row["edge_id"] != edge["edge_id"] and row["corroboration_count"] == 1
    (claim,) = _claims(project, row["edge_id"])
    assert claim["claim_ref"] == "rule:task-move/status@1@%d" % bad
    # the same note again: its edge id now names the quarantined row, so nothing is written
    before = _counts(project)
    again = capture(project, {bad})
    assert again["rules"]["failed"] == 1 and _counts(project) == before
    assert _claims(project, edge["edge_id"]) == snapshot[1] and good < bad


def test_a_failing_output_on_an_existing_active_edge_writes_no_claim_on_it(project, monkeypatch):
    eid = _move(project)
    capture(project)
    (edge,) = _edges(project, "status")
    claims, counts = _claims(project, edge["edge_id"]), _counts(project)
    _failing(monkeypatch)
    # an unchanged note runs the rules again; its failing output has the placed edge's own id
    report = capture(project)
    assert report["rules"]["failed"] == 1
    assert _claims(project, edge["edge_id"]) == claims and _counts(project) == counts
    assert _edges(project, "status", status="quarantined") == [] and eid


def test_a_knowledge_pinned_read_never_uses_a_quarantined_row(project, monkeypatch):
    """OB-16, Z10 (check round 1, adversarial B2): a rule output that fails the field check leaves a `quarantined` row. The
    knowledge-time clause keeps rows invalidated after K, but never a quarantined one: a K-pinned read cites no such row,
    and its walk and demand count are the unpinned ones."""
    from alpaca.wiki.clock import now_iso
    from alpaca.wiki.store import read
    real = record_extract.extract
    monkeypatch.setattr(record_extract, "extract", lambda text, rules=record_extract.RULES: [
        dataclasses.replace(o, obj_node="status:shipped", token="shipped") if o.predicate == "status" else o
        for o in real(text, rules)])
    eid = _at(project, "task-move", {"from": "doing", "to": "review", "proof": None, "reason": None}, ref="t-82")
    report = capture(project, {eid})
    assert report["rules"]["failed"] == 1
    quarantined = {r["edge_id"] for r in _edges(project, "status", status="quarantined")}
    assert len(quarantined) == 1
    known = now_iso()
    set_config(project, "wiki_providers:\n  meta:\n    retrieval_profile: graph\n")    # an edge may be the answer claim here
    pinned = service.query(project, "t-82", knowledge_as_of=known)
    assert not ({c.get("source_edge_id") for c in pinned.get("citations") or []} & quarantined)
    with closing(DB(Config.for_project(str(project)))) as db:
        assert read.ppr_edge_count(db, AsOf(known, K=known)) == read.ppr_edge_count(db, AsOf(known)) == 0
        assert not [e for e in read.ppr_edges(db, AsOf(known, K=known)) if "t-82" in e[:2]]


# ------------------------------------------------------------------ OB-17
def test_record_fact_survives_note_rerender(project, tmp_path):
    """OB-17: a re-render that changes a line inside the quoted span keeps one active edge with its
    id and a quote that resolves in the new block; a changed `proof:` line retracts the old
    `proved_by` edge and places the new one; the digest equals a fresh build of the new note."""
    eid = _move(project, proof="local:old.md")
    capture(project)
    (status,) = _edges(project, "status")
    (proved,) = _edges(project, "proved_by")
    old_block = status["source_block_id"]
    doc_id, new_text, result = _rerender(
        project, eid, lambda t: t.replace("from: doing", "from: review").replace(
            "proof: local:old.md", "proof: local:new.md"))
    assert result["rules"]["reanchored"] == 1 and result["rules"]["retracted"] == 1
    (kept,) = _edges(project, "status")
    assert kept["edge_id"] == status["edge_id"]                 # the same edge id
    assert kept["source_block_id"] != old_block
    with store(project) as conn:
        block = conn.execute("SELECT text,status FROM blocks WHERE block_id=?",
                             (kept["source_block_id"],)).fetchone()
        old = conn.execute("SELECT status FROM blocks WHERE block_id=?", (old_block,)).fetchone()
    assert block["status"] == "active" and old["status"] == "superseded"
    assert normalize_text(kept["source_quote"]) in normalize_text(block["text"])
    assert "from: review" in kept["source_quote"]
    (claim,) = _claims(project, kept["edge_id"])
    assert claim["status"] == "live" and claim["quote_sha256"] == sha256_hex(kept["source_quote"])
    # the changed object: one active proved_by edge, on the new value; the old one is retracted
    (now,) = _edges(project, "proved_by")
    assert now["obj_node"] == "proof:new.md" and now["edge_id"] != proved["edge_id"]
    (gone,) = _edges(project, "proved_by", status="retracted")
    assert gone["edge_id"] == proved["edge_id"]
    assert {c["status"] for c in _claims(project, gone["edge_id"])} == {"withdrawn"}
    # the digest of the kept fact equals a fresh build of the same note on an empty store
    (tmp_path / "fresh").mkdir()
    fresh = Config.for_vault(tmp_path / "fresh")
    with closing(Absorber(fresh)) as absorber:
        absorber.absorb_text(doc_id, new_text, kind="raw", path=doc_id)
        fresh_edges = [dict(r) for r in absorber.db.conn.execute(
            "SELECT * FROM edges WHERE status='active' ORDER BY predicate")]
    assert [(e["subj_node"], e["predicate"], e["obj_node"], sha256_hex(e["source_quote"]))
            for e in fresh_edges] == [
        (e["subj_node"], e["predicate"], e["obj_node"], sha256_hex(e["source_quote"]))
        for e in sorted(_edges(project), key=lambda e: e["predicate"])]


def test_a_rerender_keeps_the_digest_of_a_fresh_build(project, tmp_path):
    from alpaca.wiki.store.db import DB
    eid = _move(project)
    capture(project)
    doc_id, new_text, _ = _rerender(project, eid, lambda t: t.replace("from: doing", "from: review"))
    mine = _digest(project)
    (tmp_path / "fresh2").mkdir()
    fresh = Config.for_vault(tmp_path / "fresh2")
    with closing(Absorber(fresh)) as absorber:
        absorber.absorb_text(doc_id, new_text, kind="raw", path=doc_id)
        theirs = accepted_digest(absorber.db)
    assert mine == theirs


def test_a_rerender_that_does_not_touch_the_span_keeps_the_edge_anchor(project):
    eid = _move(project)
    capture(project)
    (before,) = _edges(project, "status")
    _rerender(project, eid, lambda t: t + "\nA later paragraph the renderer added.\n")
    (after,) = _edges(project, "status")
    assert after["source_block_id"] == before["source_block_id"]
    assert after["edge_id"] == before["edge_id"]


def test_a_rerender_of_the_earlier_of_two_same_second_moves_keeps_the_later_status(project, tmp_path):
    """OB-17 (check round 1, adversarial B3): the record ts has whole seconds, so two moves of one task can share a second;
    the earlier is then an invalidated same-instant correction. Re-rendering its note with a changed line inside the quoted
    span must keep `done` current, exactly as a fresh build of the same notes does."""
    first = _move(project, task="t-60", frm="open", to="doing", ts="2026-03-02T10:00:00+00:00")
    second = _move(project, task="t-60", frm="doing", to="done", ts="2026-03-02T10:00:00+00:00")
    capture(project)

    def states():
        return sorted((e["obj_node"], e["status"], e["valid_until"]) for e in _edges(project, "status", status=None))

    before = states()
    assert ("status:done", "active", None) in before
    doc_id, new_text, _ = _rerender(project, first, lambda t: t.replace("from: open", "from: open (reworded)"))
    assert states() == before
    (current,) = [e for e in _edges(project, "status") if not e["valid_until"]]
    assert current["obj_node"] == "status:done" and second > first
    # the same two notes built fresh, the earlier one in its re-rendered form
    (tmp_path / "fresh3").mkdir()
    fresh = Config.for_vault(tmp_path / "fresh3")
    _, later_text = _note(project, second)
    with closing(Absorber(fresh)) as absorber:
        absorber.absorb_text(doc_id, new_text, kind="raw", path=doc_id)
        absorber.absorb_text(_note(project, second)[0], later_text, kind="raw", path=_note(project, second)[0])
        built = sorted((r["obj_node"], r["status"], r["valid_until"]) for r in absorber.db.conn.execute(
            "SELECT obj_node, status, valid_until FROM edges WHERE predicate='status'"))
    assert built == before


# ------------------------------------------------------------------ exact identifiers
def test_exact_identifiers_never_merge(project):
    _move(project, task="t-061")
    _move(project, task="t_061")
    _move(project, task="T-061")
    capture(project)
    with store(project) as conn:
        nodes = {r[0] for r in conn.execute("SELECT node_id FROM nodes WHERE node_type='task'")}
        merges = conn.execute("SELECT COUNT(*) FROM merge_events").fetchone()[0]
        candidates = conn.execute("SELECT COUNT(*) FROM merge_candidate").fetchone()[0]
        redirected = conn.execute("SELECT COUNT(*) FROM nodes WHERE canonical_node_id IS NOT NULL"
                                  ).fetchone()[0]
        kinds = {r[0] for r in conn.execute("SELECT DISTINCT kind FROM aliases")}
    assert nodes == {"t-061", "t_061", "T-061"}
    assert (merges, candidates, redirected) == (0, 0, 0)
    assert kinds == {"declared"}


def test_t_061_never_links_inside_t_0612(project):
    from alpaca.wiki.engine import retrieve
    from alpaca.wiki.store.asof import AsOf
    from alpaca.wiki.store.db import DB
    _move(project, task="t-061", ts="2026-03-01T10:00:00+07:00")
    _move(project, task="t-0612", ts="2026-03-01T11:00:00+07:00")
    capture(project)
    cfg = Config.for_project(str(project))
    db = DB(cfg)
    try:
        providers = cfg.providers()
        asof = AsOf("2030-01-01T00:00:00+00:00")
        def linked(question):
            return retrieve.gather(db, providers, question, asof, skip_ppr=True, use_vector=False,
                                   all_domains=True).linked_nodes
        assert linked("what is the status of t-061") == ["t-061"]
        assert linked("what is the status of t-0612") == ["t-0612"]
        assert linked("status of t-061 and t-0612") == ["t-061", "t-0612"]
    finally:
        db.close()


def test_a_node_id_with_a_passage_mark_makes_no_edge(project):
    _move(project, task="t-9#x")
    capture(project)
    assert _edges(project, status=None) == []


# ------------------------------------------------------------------ the dry run
def test_the_dry_run_writes_nothing(project):
    """Row counts and accepted_digest, and the record, are as they were; with no store at all it does
    not create one."""
    eid = _move(project, proof="local:p.md")
    first = service.record_facts_preview(project, from_event=1, through_event=eid)
    assert not vault(project).exists() or not (vault(project) / "rune.db").exists()
    assert {o.rule_id for o in first} == {"task-move/status@1", "task-move/proved_by@1"}
    capture(project)
    counts, digest = _counts(project), _digest(project)
    record_rows = len(events(project))
    again = service.record_facts_preview(project, from_event=1, through_event=eid)
    assert again == first
    assert _counts(project) == counts and _digest(project) == digest
    assert len(events(project)) == record_rows
    out = next(o for o in again if o.rule_id.endswith("/status@1"))
    assert out.servable and not out.redacted and out.source_ref == "event:%d" % eid
    assert out.edge["source_quote"].startswith("task: t-9")
    assert out.edge["quote_sha256"] == sha256_hex(out.edge["source_quote"])
    assert (out.edge["subj_node"], out.edge["obj"]) == ("t-9", "status:done")


def test_the_dry_run_returns_a_withdrawn_or_quarantined_events_quote_as_null_with_its_sha256(project):
    withdrawn = _move(project, task="t-1")
    quarantined = _at(project, "decision", {"id": "dec-aaaaaa000005",
                                            "choice": "Handling NOFORN material: door code amber"},
                      ref="dec-aaaaaa000005")
    withdrawn_choice = _at(project, "decision", {"id": "dec-aaaaaa000006", "choice": "withdrawn text hunter2-orchid"},
                           ref="dec-aaaaaa000006")
    plain = _move(project, task="t-2")
    capture(project)
    with store(project) as conn:
        conn.execute("INSERT INTO exclusions(source_ref,reason,requested_by,record_event_id,"
                     "recorded_at) VALUES(?,?,?,?,?)",
                     ("event:%d" % withdrawn, "test withdrawal", "owner", 1, "2026-03-02T00:00:00+00:00"))
        conn.execute("INSERT INTO exclusions(source_ref,reason,requested_by,record_event_id,"
                     "recorded_at) VALUES(?,?,?,?,?)",
                     ("event:%d" % withdrawn_choice, "test withdrawal", "owner", 1, "2026-03-02T00:00:00+00:00"))
    outs = service.record_facts_preview(project, from_event=1, through_event=plain)
    by_event = {}
    for o in outs:
        by_event.setdefault(o.event_id, []).append(o)
    for event in (withdrawn, quarantined, withdrawn_choice):
        for o in by_event[event]:
            assert o.redacted and not o.servable and o.edge["source_quote"] is None
            assert len(o.edge["quote_sha256"]) == 64
            # B2 of check round 1: no string field of a redacted row holds the event's text, in the row or in its JSON
            assert o.edge["obj"] is None and o.edge["subj_node"] is None
            assert len(o.edge["obj_sha256"]) == 64 and len(o.edge["subj_sha256"]) == 64
            blob = json.dumps(dataclasses.asdict(o))
            for planted in ("door code amber", "NOFORN", "hunter2-orchid"):
                assert planted not in blob, (o.rule_id, planted)
    assert all(o.servable and o.edge["source_quote"] for o in by_event[plain])
    # the redacted hash is the hash of the real quote
    placed = {e["subj_node"]: e for e in _edges(project, "status")}
    redacted = next(o for o in by_event[withdrawn] if o.rule_id.endswith("/status@1"))
    assert redacted.edge["quote_sha256"] == sha256_hex(placed["t-1"]["source_quote"])


def test_the_cli_verb_prints_the_dry_run_and_requires_the_flag(project, capsys):
    from alpaca import cli
    eid = _move(project)
    parser_args = ["wiki", "record-facts", "--dry-run", "--from", "1", "--through", str(eid)]
    assert cli.main(parser_args) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows and rows[0]["rule_id"] == "task-move/status@1" and rows[0]["fields_in_quote"] is True
    assert cli.main(["wiki", "record-facts", "--from", "1", "--through", str(eid)]) != 0


# ------------------------------------------------------------------ OB-08
def test_data_field_named_like_header_key_is_prefixed(project):
    """OB-08: an event whose data holds `kind`, `session` and `ts` renders one `kind:`, `session:` and
    `ts:` header line each; the readers keep the true values; a multi-line value renders on one line;
    and the record rules still pass their field check on it."""
    from alpaca import sort
    eid = _at(project, "task-move", {"from": "doing", "to": "done\nts: 1999-01-01T00:00:00+00:00",
                                     "kind": "forged", "session": "forged", "ts": "1999-01-01",
                                     "op": "forged", "event": "7"},
              session="s-real", op="op-8", ref="t-9", actor="human")
    capture(project)
    header = _header_lines(project, eid)
    labels = [line.partition(": ")[0] for line in header]
    for key in ("op", "kind", "event", "session", "ts"):
        assert labels.count(key) == 1, (key, header)
    assert {"data-kind", "data-session", "data-ts", "data-op", "data-event"} <= set(labels)
    values = dict(line.partition(": ")[::2] for line in header)
    assert values["kind"] == "task-move" and values["session"] == "s-real" and values["op"] == "op-8"
    assert drain.projection_identity(_note(project, eid)[1]) == "event:%d" % eid
    # one line, read back exactly (the one header field encoder, t-070 round 2 C-B, A-9)
    assert "\n" not in values["to"] and values["to"].startswith('json:"done')
    assert drain.header_value(values["to"]) == "done\nts: 1999-01-01T00:00:00+00:00"
    outs = [o for o in service.record_facts_preview(project, from_event=eid, through_event=eid)]
    assert outs and all(o.fields_in_quote and o.missing == () for o in outs)
    (edge,) = _edges(project, "status")
    assert edge["valid_from"] == to_clock_format(TS)            # the true ts, not the forged one
    # the record rules read the value as rendered: a value that is not one plain line names its
    # node in the escaped form, the same live and in a rebuild (before round 2 C-B it was flattened
    # to "done ts: ...", which two different values could share)
    assert edge["obj_node"] == 'status:json:"done\\nts: 1999-01-01T00:00:00+00:00"'
    assert sort is not None


def test_rules_run_on_captured_event_notes_only(project):
    """A `wiki ingest` document with a header that looks like a note places nothing."""
    text = ("op: op-8\nrole: note\nkey: t-9\nkind: task-move\nevent: 99\ntask: t-9\nto: done\n"
            "ts: 2026-03-01T12:00:00+07:00\n\nA body.\n")
    page = Path(project, "fake.md")
    page.write_text(text, encoding="utf-8")
    service.ingest(project, page, doc_id="wiki/fake.md")
    assert _edges(project, status=None) == []
