"""op-008 t-068: summary candidates from the live session agent (requirements.md FR-20, FR-33;
design/rune2-memory/architecture.md 7.2 K-3, K-5, 7.4, 7.5; implementation-plan.md t-068 Tests and
OB-13).

A summary is taken in by `propose` as a cited candidate and is never admitted: it is never a
grounded Answer, a source document or corroboration. A later proposal whose anchor is briefing
text is refused (K-5, K-2), wherever that text sits: named as a briefing, quoted under the note it
cites, handed on in a record message, pasted into a sealed proof report or copied without its
marker line; capture leaves a briefing out of the notes it lands, and nothing it says corroborates
a fact (FR-20, HS-MM-11). A summary sentence that names a person absent from its anchors is refused
with `field-not-in-span` though the structural entailer calls it supported, however the name is
written (OB-13, run on a summary here; the observation run is
T/test_intake_checks.py::test_observation_with_invented_name_is_refused).
Real record, real drain, real store; no model and no network.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import (T09, T10, T11, appender, at, capture, digest_of, event_hash,  # noqa: E402
                            events, store)

from alpaca.wiki import briefing as briefings, intake, service   # noqa: E402

ANCHOR = ("The capture of session c29 failed because the store was locked during the SessionEnd "
          "hook.")
SUMMARY = "The capture of session c29 failed during the SessionEnd hook."


def note(project, body=ANCHOR, ts=T10, ref="t-9"):
    eid = at(project, "result", {"body": body}, ts, op="op-9", ref=ref)
    capture(project, {eid})
    return eid


def anchor(project, eid, quote=ANCHOR):
    return {"source_ref": "event:%d" % eid, "quote": quote, "hash": event_hash(project, eid)}


def summary_body(project, eid, text=SUMMARY, **more):
    body = {"kind": "summary", "anchors": [anchor(project, eid)], "sentence_anchors": [[0]],
            "text": text}
    body.update(more)
    return body


def propose(project, body, session="s-agent"):
    return service.propose(project, body, session=session, record=appender(project, session))


def failed(receipt):
    return {c.check: c.reason for c in receipt.checks if not c.passed}


def table_rows(project, table):
    with store(project) as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                              (table,)).fetchone()
        return [dict(r) for r in conn.execute("SELECT * FROM %s" % table)] if exists else []


# ------------------------------------------------------------------ intake, never admission
def test_a_summary_is_a_checked_candidate_and_never_admitted(project):
    eid = note(project)
    digest = digest_of(project)
    receipt = propose(project, summary_body(project, eid))
    assert receipt.status == "checked" and failed(receipt) == {}
    assert receipt.verdict is None and receipt.prior_edge_id is None
    refused = service.admit(project, receipt.candidate_id, session="s-admin",
                            record=appender(project, "s-admin"))
    assert (refused.status, refused.reason) == ("refused", "summary-not-admitted")
    (event,) = [e for e in events(project) if e["kind"] == "wiki-admission"]
    assert json.loads(event["data"])["result"] == "refused"
    (row,) = [r for r in table_rows(project, "candidates")
              if r["candidate_id"] == receipt.candidate_id]
    assert row["status"] == "checked" and row["admission_ref"] is None
    assert not [e for e in table_rows(project, "edges") if e["extractor"] == "llm"]
    assert not [c for c in table_rows(project, "edge_origin") if c["candidate_id"]]
    assert not table_rows(project, "lessons_log") and not table_rows(project, "knowledge_facts")
    assert digest_of(project) == digest                  # no accepted state moved


def test_a_summary_is_never_a_grounded_answer_nor_a_source(project):
    eid = note(project)
    receipt = propose(project, summary_body(project, eid))
    capture(project)                                      # the wiki-candidate event included
    with store(project) as conn:
        texts = [r[0] for r in conn.execute("SELECT text FROM blocks")]
        notes = [r[0] for r in conn.execute("SELECT text FROM blocks WHERE ordinal=0")]
    assert not any(SUMMARY in t for t in texts)           # never absorbed as a source document
    candidate_event = next(e["id"] for e in events(project) if e["kind"] == "wiki-candidate")
    assert not any(("event: %d\n" % candidate_event) in n + "\n" for n in notes)
    result = service.query(project, SUMMARY)
    assert result["citations"]                            # answered from the note it cites
    for citation in result["citations"]:
        assert citation["source_block_id"].startswith("raw/")
    for source in result["sources"]:
        assert source["source_ref"] == "event:%d" % eid and SUMMARY not in source["quote"]
    assert SUMMARY not in (result["answer_text"] or "")
    assert receipt.candidate_id not in json.dumps(result)


def test_a_summary_is_never_corroboration(project):
    at(project, "task-move", {"from": "open", "to": "doing", "proof": None, "reason": None}, T09,
       op="op-9", ref="t-9")
    move = at(project, "task-move", {"from": "doing", "to": "blocked", "proof": None,
                                     "reason": ANCHOR}, T11, op="op-9", ref="t-9")
    capture(project)
    before = (table_rows(project, "edges"), table_rows(project, "edge_corroborations"),
              table_rows(project, "edge_origin"))
    assert [e for e in before[0] if e["predicate"] == "status"]
    receipt = propose(project, summary_body(project, move))
    assert receipt.status == "checked"
    service.admit(project, receipt.candidate_id, session="s-admin",
                  record=appender(project, "s-admin"))
    capture(project)
    after = (table_rows(project, "edges"), table_rows(project, "edge_corroborations"),
             table_rows(project, "edge_origin"))
    assert after == before


def test_a_withdrawn_summary_leaves_the_briefing(project):
    eid = note(project)
    receipt = propose(project, summary_body(project, eid, scope={"task": "t-9"}))
    assert SUMMARY in service.briefing(project, task="t-9").text
    out = service.withdraw_candidate(project, receipt.candidate_id, session="s-agent",
                                     record=appender(project, "s-agent"), reason="superseded")
    assert out.status == "applied"
    assert SUMMARY not in service.briefing(project, task="t-9").text


# ------------------------------------------------------------------ briefing text is never an anchor
def test_a_later_proposal_anchored_on_briefing_text_is_refused(project):
    move = at(project, "task-move", {"from": "open", "to": "blocked", "proof": None,
                                     "reason": ANCHOR}, T10, op="op-9", ref="t-9")
    capture(project)
    brief = service.briefing(project, task="t-9")
    line = next(x for x in brief.text.splitlines() if x.startswith("- blockers"))
    for kind_body in ({"kind": "summary", "text": SUMMARY, "sentence_anchors": [[0]]},
                      {"kind": "fact", "fact": {"subject": "t-9", "predicate": "status",
                                                "obj": "blocked", "obj_datatype": "string"}}):
        # the briefing named as a source: not an independent kind (K-5), and nothing to resolve
        named = dict(kind_body, anchors=[{"source_ref": "briefing:task:t-9", "quote": line}])
        checks = failed(propose(project, named))
        assert checks.get("K-5") == "not-independent" and checks.get("K-1") == "anchor-unresolved"
        # the briefing's own wording under the note it cites resolves in no source (K-2)
        for quote in (line, brief.marker):
            copied = dict(kind_body, anchors=[anchor(project, move, quote)])
            assert failed(propose(project, copied)).get("K-2") == "quote-not-in-span"
    # what a briefing hands an agent to anchor on is the source's own span, which passes
    (item,) = [i for i in brief.items if i.section == "blockers"]
    (source,) = item.sources
    assert source.source_ref == "event:%d" % move and source.event_hash == event_hash(project, move)
    quoted = {"kind": "summary", "text": "The store was locked during the SessionEnd hook.",
              "sentence_anchors": [[0]],
              "anchors": [{"source_ref": source.source_ref, "quote": source.quote,
                           "hash": source.event_hash}]}
    assert propose(project, quoted).status == "checked"
    # nor can the briefing come back in as a document
    page = os.path.join(project, "fed-back.md")
    with open(page, "w", encoding="utf-8") as fh:
        fh.write(brief.text)
    try:
        service.ingest(project, page, doc_id="wiki/fed-back.md")
        raise AssertionError("a briefing was ingested")
    except ValueError as exc:
        assert "not a source document" in str(exc)
    assert briefings.MARKER_PATTERN.search(brief.text)
    assert "briefing" not in intake.INDEPENDENT_KINDS


# ------------------------------------------------------------------ briefing text fed back
DECISION = "dec-0a1b2c3d4e5f"


def blocked_task(project):
    """t-9: added, a decision, a contract sourced from it, then blocked; an admitted fact about
    it ("t-9 related to nightly backup"), so its briefing holds a source, record facts and an
    admitted item. Returns the briefing at 2,500 characters and the event ids."""
    ids = {"add": at(project, "task-add", {"title": "Restore the kestrel queue backup",
                                           "statement": "Restore it."}, T09, op="op-9", ref="t-9"),
           "decision": at(project, "decision", {"id": DECISION,
                                                "choice": "Restore t-9 from the nightly backup."},
                          T09, op="op-9", ref=DECISION),
           "contract": at(project, "task-contract", {"done_bar": ["the kestrel queue drains"],
                                                     "source": DECISION}, T09, op="op-9", ref="t-9"),
           "move": at(project, "task-move", {"from": "open", "to": "blocked", "proof": None,
                                             "reason": "The backup volume is offline."}, T10,
                      op="op-9", ref="t-9")}
    capture(project)
    fact = propose(project, {"kind": "fact", "fact": {"subject": "t-9", "predicate": "related_to",
                                                      "obj": "nightly backup",
                                                      "obj_datatype": "string"},
                             "anchors": [anchor(project, ids["decision"],
                                                "Restore t-9 from the nightly backup")]})
    assert fact.status == "checked"
    done = service.admit(project, fact.candidate_id, session="s-admin",
                         record=appender(project, "s-admin"))
    assert done.status == "applied"
    brief = service.briefing(project, task="t-9")
    assert "- state [admitted]: t-9 related to nightly backup" in brief.text
    return brief, ids


def llm_support(project):
    """The corroboration state of the agent facts: (edge, count) pairs, corroborator rows, claims."""
    with store(project) as conn:
        edges = [tuple(r) for r in conn.execute(
            "SELECT edge_id, corroboration_count FROM edges WHERE extractor='llm' ORDER BY edge_id")]
        rows = conn.execute("SELECT COUNT(*) FROM edge_corroborations").fetchone()[0]
        claims = conn.execute("SELECT COUNT(*) FROM edge_origin").fetchone()[0]
    return edges, rows, claims


def blocks_of(project, eid):
    with store(project) as conn:
        return [r[0] for r in conn.execute(
            "SELECT b.text FROM blocks b WHERE b.status='active' AND b.doc_id LIKE ? "
            "AND b.doc_id NOT LIKE 'raw/sources/%'", ("raw/%%/%06d-%%" % eid,))]


def test_a_briefing_handed_on_in_a_message_is_never_support(project):
    """Check round 1 (invariants B1, adversarial B3): a briefing posted with `bin/alpaca msg post`
    is a record event, captured as a note. Capture leaves the briefing out of that note, so no
    quote of its wording resolves there, no fact anchored on it is admitted, no corroboration
    count moves, and a summary's unsupported clause cannot be laundered into an admitted fact or a
    grounded answer through it."""
    import io
    from contextlib import redirect_stdout
    from alpaca import cli, db as record
    brief, ids = blocked_task(project)

    def post(text):
        with redirect_stdout(io.StringIO()):
            assert cli.main(["--session", "s-lead", "msg", "post", text, "--from", "s-lead",
                             "--to", "broadcast"]) == 0
        conn = record.connect_readonly(project)
        try:
            mid = conn.execute("SELECT id FROM events WHERE kind='msg' ORDER BY id DESC").fetchone()[0]
        finally:
            conn.close()
        capture(project, {mid})
        return mid

    mid = post("Handing over t-9.\n" + brief.text + "\nAsk me about the volume.")
    held = " ".join(blocks_of(project, mid))
    assert "Handing over t-9." in held and "Ask me about the volume." in held
    assert "[Rune2 briefing task:t-9 left out at capture: generated text is not a source]" in held
    assert not briefings.holds_generated(held) and "related to nightly backup" not in held
    before = llm_support(project)
    status_line = next(i.text for i in brief.items if "status is blocked" in i.text)
    for body in ({"kind": "fact", "fact": {"subject": "t-9", "predicate": "related_to",
                                           "obj": "nightly backup", "obj_datatype": "string"},
                  "anchors": [anchor(project, mid, "t-9 related to nightly backup")]},
                 {"kind": "summary", "text": status_line + ".", "sentence_anchors": [[0]],
                  "anchors": [anchor(project, mid, status_line)]},
                 {"kind": "summary", "text": "Rune2 briefing task:t-9 is evidence-only.",
                  "sentence_anchors": [[0]], "anchors": [anchor(project, mid, brief.marker)]}):
        receipt = propose(project, body, session="s-other")
        assert receipt.status == "refused" and failed(receipt).get("K-2") == "quote-not-in-span"
        if "Rune2 briefing" in body["anchors"][0]["quote"]:
            # a quote that is itself briefing text is refused as not independent too (K-5)
            assert failed(receipt).get("K-5") == "not-independent"
    assert llm_support(project) == before                 # the corroboration count is unchanged
    # a summary's clause no source holds ("shard") is shown in a briefing, which is posted on
    said = "Task t-9 is related to the quarry backup volume, the owner says."
    source = note(project, said, T11)
    clause = "Task t-9 is related to the quarry backup volume shard."
    summ = propose(project, {"kind": "summary", "text": clause, "sentence_anchors": [[0]],
                             "scope": {"task": "t-9"}, "anchors": [anchor(project, source, said)]},
                   session="s-agent4")
    assert summ.status == "checked" and clause in service.briefing(project, task="t-9").text
    mid2 = post(service.briefing(project, task="t-9").text)
    laundered = propose(project, {"kind": "fact", "fact": {
        "subject": "t-9", "predicate": "related_to", "obj": "quarry backup volume shard",
        "obj_datatype": "string"}, "anchors": [anchor(project, mid2, clause.rstrip("."))]},
        session="s-agent6")
    assert laundered.status == "refused"
    assert llm_support(project) == before
    result = service.query(project, "What is t-9 related to?")
    assert all(s["source_ref"] != "event:%d" % mid2 for s in result["sources"])
    assert "shard" not in (result["answer_text"] or "")


def test_briefing_lines_copied_without_their_marker_are_never_support(project):
    """Intake refuses an anchor whose quote or block holds a briefing line even when no marker line
    came with it (K-5 `not-independent`), at proposal and so again at admission (OB-15)."""
    brief, ids = blocked_task(project)
    status_line = next(x for x in brief.text.splitlines() if "status is blocked" in x)
    # flattened onto one line inside other text, as a hand copy may be
    eid = at(project, "result", {"body": "Note for the next session " + status_line}, T11,
             op="op-9", ref="t-9")
    capture(project, {eid})
    assert any(status_line in b for b in blocks_of(project, eid))
    quote = status_line.split("]: ", 1)[1].rsplit(" (", 1)[0]          # the generated wording
    for body in ({"kind": "summary", "text": quote + ".", "sentence_anchors": [[0]],
                  "anchors": [anchor(project, eid, quote)]},
                 {"kind": "fact", "fact": {"subject": "t-9", "predicate": "status", "obj": "blocked",
                                           "obj_datatype": "string"},
                  "anchors": [anchor(project, eid, quote)]}):
        receipt = propose(project, body, session="s-other")
        assert failed(receipt).get("K-5") == "not-independent", failed(receipt)
        assert [c.detail for c in receipt.checks if c.check == "K-5"] == ["anchor[0] briefing-text"]
    # an item line pasted on a line of its own is left out at capture
    eid2 = at(project, "result", {"body": "Notes:\n" + status_line + "\nThe volume is mounted."},
              T11, op="op-9", ref="t-9")
    capture(project, {eid2})
    held = " ".join(blocks_of(project, eid2))
    assert "status is blocked" not in held and "The volume is mounted." in held
    # an item of the JSON form cut short, so capture cannot decode and cut it: intake still knows it
    fragment = '{"section": "state", "text": "%s", "label": "record-fact"' % quote
    eid3 = at(project, "result", {"body": "Copied: " + fragment}, T11, op="op-9", ref="t-9")
    capture(project, {eid3})
    assert any(quote in b for b in blocks_of(project, eid3))
    receipt = propose(project, {"kind": "summary", "text": quote + ".", "sentence_anchors": [[0]],
                                "anchors": [anchor(project, eid3, quote)]}, session="s-other")
    assert failed(receipt).get("K-5") == "not-independent", failed(receipt)
    # check round 2: the line under another list marker, a number, a letter, a table pipe or none
    # (adversarial fu-adv-1) is cut on a line of its own and refused inline; and in the escaped
    # forms capture cannot decode (a JSON string cut short, a Python string), after a quote mark or
    # an escaped line break, with or without its list marker, it is refused (invariants B2)
    head = status_line[2:]                         # "state [record-fact]: t-9 status is ..."
    shapes = ["+ " + head, "1. " + head, "2) " + head, "b) " + head, head, "| 3 | %s |" % head,
              "|" + head, "- - " + head]
    for shape in shapes:
        own = at(project, "result", {"body": "Notes:\n" + shape + "\nThe volume is mounted."},
                 T11, op="op-9", ref="t-9")
        capture(project, {own})
        held = " ".join(blocks_of(project, own))
        assert "status is blocked" not in held and "The volume is mounted." in held, (shape, held)
    escaped = ['["%s' % head, '{"text": "Notes:\\n%s' % status_line, '{"text": "Notes:\\n%s' % head,
               '["Notes:\\n%s' % head.replace(" ", "\\t", 1), repr(["Notes:\n" + status_line]),
               repr(["Notes:\n" + head])]
    for shape in shapes + escaped:
        inline = at(project, "result", {"body": "Note for the next session " + shape}, T11,
                    op="op-9", ref="t-9")
        capture(project, {inline})
        assert any(quote in b for b in blocks_of(project, inline)), shape
        receipt = propose(project, {"kind": "summary", "text": quote + ".",
                                    "sentence_anchors": [[0]],
                                    "anchors": [anchor(project, inline, quote)]}, session="s-other")
        assert failed(receipt).get("K-5") == "not-independent", (shape, failed(receipt))
        assert [c.detail for c in receipt.checks if c.check == "K-5"] == ["anchor[0] briefing-text"]


def test_a_briefing_line_given_as_a_decision_option_is_never_support(project):
    """Check round 2, invariants B2 (FR-20, HS-MM-11): a decision's options are a list, which the
    event note renders as JSON, so an item line given as an option (`bin/alpaca decide --option`)
    sits inside a JSON string there, after a quote mark. Capture cuts it out of that string and out
    of the decision's source copy, so the copied wording resolves in neither: a summary and an
    observation anchored on it are refused, nothing is admitted, and no briefing lists the
    generated sentence as `admitted`."""
    import io
    from contextlib import redirect_stdout
    from alpaca import cli
    brief, ids = blocked_task(project)
    status_line = next(x for x in brief.text.splitlines() if "status is blocked" in x)
    quote = status_line.split("]: ", 1)[1].rsplit(" (", 1)[0]          # the generated wording
    with redirect_stdout(io.StringIO()):
        assert cli.main(["--session", "s-lead", "decide", "--kind", "plan", "--context",
                         "What the briefing said.", "--option", status_line, "--option", "stop",
                         "--choice", "Keep the plan.", "--consequence", "Nothing moves."]) == 0
    decided = max(e["id"] for e in events(project) if e["kind"] == "decision")
    in_op = at(project, "decision", {"id": "dec-0d0d0d0d0d0d", "choice": "Keep the plan.",
                                     "options": [status_line, "stop"]}, T11, op="op-9",
               ref="dec-0d0d0d0d0d0d")
    capture(project)
    ident = next(e["ref"] for e in events(project) if e["id"] == decided)
    for eid, ref in ((decided, "decision:%s" % ident), (in_op, "decision:dec-0d0d0d0d0d0d")):
        with store(project) as conn:
            held = "\n".join(r[0] for r in conn.execute(
                "SELECT text FROM blocks WHERE status='active' AND doc_id LIKE ?",
                ("raw/%%/%06d-%%" % eid,)))
        assert "stop" in held and "status is blocked" not in held, held
        assert not briefings.holds_generated(held), held
        for source in ("event:%d" % eid, ref):
            cited = {"source_ref": source, "quote": quote}
            if source.startswith("event:"):
                cited["hash"] = event_hash(project, eid)
            for body in ({"kind": "summary", "text": quote + ".", "sentence_anchors": [[0]],
                          "anchors": [cited]},
                         {"kind": "observation", "text": "t-9 status is blocked.",
                          "sentence_anchors": [[0]], "scope": {"op": "op-9"},
                          "observation": {"falsifiability":
                                          "observe: a later move of t-9 shows another status"},
                          "anchors": [cited]}):
                receipt = propose(project, body, session="s-other")
                assert receipt.status == "refused", (source, body["kind"], failed(receipt))
                assert service.admit(project, receipt.candidate_id, session="s-admin",
                                     record=appender(project, "s-admin")).status != "applied"
    for subject in ({"task": "t-9"}, {"op": "op-9"}):
        b = service.briefing(project, max_chars=16000, **subject)
        assert not [i for i in b.items if i.label == "admitted" and "status is blocked" in i.text]


def test_a_briefing_pasted_into_a_sealed_proof_report_is_never_support(project):
    """A proof report is an independent source (K-5); a briefing pasted into its evidence is left
    out of the captured copy, so the generated lines resolve nowhere while the report's own text
    still anchors."""
    import hashlib
    brief, ids = blocked_task(project)
    rel = ".alpaca/proofs/op-9/t-9.md"
    os.makedirs(os.path.join(project, ".alpaca", "proofs", "op-9"), exist_ok=True)
    report = ("# t-9 proof\n\n## At a glance\n\nThe queue is blocked on the backup volume.\n\n"
              "## Evidence\n\n`bin/alpaca wiki briefing --task t-9` printed:\n\n" + brief.text + "\n")
    with open(os.path.join(project, rel), "w", encoding="utf-8") as fh:
        fh.write(report)
    at(project, "proof-report", {"path": rel, "sha256": hashlib.sha256(report.encode()).hexdigest()},
       T11, op="op-9", ref="t-9")
    capture(project)
    with store(project) as conn:
        copy = " ".join(r[0] for r in conn.execute(
            "SELECT text FROM blocks WHERE status='active' AND doc_id LIKE 'raw/sources/proof/%'"))
    assert "The queue is blocked on the backup volume." in copy
    assert "left out at capture" in copy and not briefings.holds_generated(copy)
    status_line = next(i.text for i in brief.items if "status is blocked" in i.text)
    refused = propose(project, {"kind": "summary", "text": status_line + ".",
                                "sentence_anchors": [[0]], "scope": {"task": "t-9"},
                                "anchors": [{"source_ref": "proof:" + rel, "quote": status_line}]},
                      session="s-agent3")
    assert refused.status == "refused" and failed(refused).get("K-2") == "quote-not-in-span"
    own = propose(project, {"kind": "summary", "text": "The queue is blocked on the backup volume.",
                            "sentence_anchors": [[0]],
                            "anchors": [{"source_ref": "proof:" + rel,
                                         "quote": "The queue is blocked on the backup volume."}]},
                  session="s-agent3")
    assert own.status == "checked"


def test_ingest_refuses_briefing_text_however_it_is_wrapped(project):
    brief, _ = blocked_task(project)
    head, rest = brief.text.split("\n", 1)
    cut = head.index(" built on demand")
    for text in (head[:cut] + "\n" + head[cut + 1:] + "\n" + rest,     # the marker line wrapped
                 "Notes from the last session:\n" + rest):              # the lines without it
        page = os.path.join(project, "fed-back.md")
        with open(page, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        try:
            service.ingest(project, page, doc_id="wiki/fed-back.md")
            raise AssertionError("briefing text was ingested")
        except ValueError as exc:
            assert "not a source document" in str(exc)


# ------------------------------------------------------------------ OB-13 on a summary
def test_summary_with_invented_name_is_refused(project):
    """OB-13 run on a summary: every number, identifier-like token and capitalized word that does
    not start a sentence sits in one of that sentence's anchors, though the structural entailer
    passes 80% token coverage and calls both invented sentences supported."""
    eid = note(project)
    invented = "The capture failed because Remy locked the store during the SessionEnd hook."
    hindsight = "The capture failed because Hindsight locked the store during the SessionEnd hook."
    from alpaca.wiki.config import Config
    entailer = Config.for_project(project).providers().entailer
    assert entailer.entail(invented, ANCHOR)[0] == "supported"
    assert entailer.entail(hindsight, ANCHOR)[0] == "supported"
    for text in (invented, hindsight):
        receipt = propose(project, summary_body(project, eid, text=text))
        assert receipt.status == "refused" and failed(receipt) == {"K-3": "field-not-in-span"}
        assert [c.detail for c in receipt.checks if c.check == "K-3"] == ["sentence:0"]
    # however the name is written: typographic quotes, guillemets, after a zero-width space, or
    # glued to a word by a dash other than "-" (check round 1, adversarial B1)
    for name in ("\u201cViet\u201d", "\u2018Hindsight\u2019", "\u00abViet\u00bb", "\u200bViet",
                 "store\u2014Viet"):
        text = "The capture failed because %s locked the store during the SessionEnd hook." % name
        receipt = propose(project, summary_body(project, eid, text=text))
        assert receipt.status == "refused" and failed(receipt) == {"K-3": "field-not-in-span"}, name
    # a name the anchor holds, in typographic quotes, is read without them and passes
    quoted = "The capture of session c29 failed during the \u2018SessionEnd\u2019 hook."
    assert propose(project, summary_body(project, eid, text=quoted)).status == "checked"
    # the second sentence of a two-sentence summary is checked against its own anchors
    two = summary_body(project, eid, text="%s %s" % (SUMMARY, invented),
                       sentence_anchors=[[0], [0]])
    receipt = propose(project, two)
    assert failed(receipt) == {"K-3": "field-not-in-span"}
    assert [c.detail for c in receipt.checks if c.check == "K-3"] == ["sentence:1"]
    # the anchor's own sentence passes, and a summary whose tokens all sit in its anchor
    assert propose(project, summary_body(project, eid, text=ANCHOR)).status == "checked"
    assert propose(project, summary_body(project, eid)).status == "checked"


def test_summary_limits_and_sentence_anchors(project):
    eid = note(project)
    long = " ".join([SUMMARY] * 40)
    receipt = propose(project, summary_body(project, eid, text=long,
                                            sentence_anchors=[[0]] * 40))
    assert failed(receipt).get("K-6") == "limit" and len(long) > intake.SUMMARY_CHARS
    unanchored = summary_body(project, eid, text="%s %s" % (SUMMARY, SUMMARY), sentence_anchors=[[0]])
    assert failed(propose(project, unanchored)) == {"K-3": "sentence-without-anchor"}
    transcript = summary_body(project, eid)
    transcript["anchors"] = [{"source_ref": "transcript:s-native", "quote": ANCHOR}]
    assert failed(propose(project, transcript)).get("K-5") == "not-independent"
