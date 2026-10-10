"""op-008 t-066: the model-free checks K-1 to K-6 of candidate intake (design/rune2-memory/
architecture.md 7.2; FR-33, FR-20; bars Z2, Z10; OB-22).

Real drain, real store, real record: a proposal is checked against a captured event note, never a
mock. Each check passes and fails here; an invented detail is refused; transcript and `wiki ingest`
text never counts as support and goes in `context`; no candidate or claim gets `user-statement`.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import (FACT_TEXT, append, appender, capture, event_hash, events,  # noqa: E402
                            fact_event, fact_proposal, ingest, store)

from alpaca.wiki import intake, service                       # noqa: E402


def propose(project, body, session="s-agent"):
    return service.propose(project, body, session=session, record=appender(project, session))


def failed(receipt):
    return {c.check: c.reason for c in receipt.checks if not c.passed}


def candidate_event(project, receipt):
    return json.loads(next(e["data"] for e in events(project) if e["id"] == receipt.record_event_id))


def hide(project, source_ref, how):
    """Make a captured source unservable the way the record does: an exclusions row, or a private
    document (a quarantined capture lands private under wiki/private/, architecture.md 9.5)."""
    with store(project) as conn:
        if how == "excluded":
            conn.execute("INSERT INTO exclusions(source_ref,reason,requested_by,record_event_id,"
                         "recorded_at) VALUES(?,?,?,?,?)", (source_ref, "test", "owner", 1, "x"))
        else:
            conn.execute("UPDATE docs SET private=1 WHERE doc_id IN "
                         "(SELECT doc_id FROM source_versions WHERE source_ref=?)", (source_ref,))


# ------------------------------------------------------------------ the happy path and K-1, K-2
def test_a_proposal_that_cites_its_span_passes_all_six_checks(project):
    eid = fact_event(project)
    receipt = propose(project, fact_proposal(project, eid))
    assert receipt.status == "checked" and failed(receipt) == {}
    assert [c.check for c in receipt.checks] == ["K-1", "K-2", "K-3", "K-4", "K-5", "K-6"]
    assert receipt.verdict == "novel"


def test_k1_an_anchor_that_is_not_captured_is_unresolved(project):
    eid = fact_event(project)
    body = fact_proposal(project, eid)
    body["anchors"][0]["source_ref"] = "event:%d" % (eid + 40)
    assert failed(propose(project, body))["K-1"] == "anchor-unresolved"


def test_k1_an_event_hash_that_differs_from_the_record_row_is_unresolved(project):
    eid = fact_event(project)
    body = fact_proposal(project, eid)
    body["anchors"][0]["hash"] = "0" * 64
    receipt = propose(project, body)
    assert failed(receipt)["K-1"] == "anchor-unresolved"
    del body["anchors"][0]["hash"]
    assert failed(propose(project, body))["K-1"] == "anchor-unresolved"


def test_k2_a_quote_that_is_not_in_the_span_is_refused(project):
    eid = fact_event(project)
    receipt = propose(project, fact_proposal(project, eid, quote="Task t-066 is finished"))
    assert receipt.status == "refused" and failed(receipt)["K-2"] == "quote-not-in-span"


def test_resolve_anchor_returns_the_first_block_holding_the_normalized_quote(project):
    eid = fact_event(project)
    with store(project) as conn:
        assert intake.resolve_anchor(conn, "event:%d" % eid, "task t-066 is blocked") is None   # case counts
        found = intake.resolve_anchor(conn, "event:%d" % eid, "Task t-066   is blocked")
        assert found["ordinal"] == 1 and found["doc_id"].endswith("000001-result.md")
        assert intake.resolve_anchor(conn, "event:%d" % eid, "") is None
        assert intake.resolve_anchor(conn, "event:999", "Task") is None


# ------------------------------------------------------------------ K-3
def test_k3_an_invented_object_is_refused(project):
    eid = fact_event(project)
    receipt = propose(project, fact_proposal(project, eid, obj="abandoned"))
    assert receipt.status == "refused"
    assert failed(receipt) == {"K-3": "field-not-in-span"}
    assert [c.detail for c in receipt.checks if c.check == "K-3"] == ["field:object"]


def test_k3_an_invented_subject_or_date_is_refused(project):
    eid = fact_event(project)
    assert failed(propose(project, fact_proposal(project, eid, subject="t-099")))["K-3"] \
        == "field-not-in-span"
    receipt = propose(project, fact_proposal(project, eid, valid_from="2026-09-30"))
    assert failed(receipt) == {"K-3": "field-not-in-span"}
    assert propose(project, fact_proposal(
        project, eid, quote="Task t-066 is blocked on the review of dec-38940e1fb35f since 2026-10-02",
        valid_from="2026-10-02")).status == "checked"


def test_k3_a_short_name_does_not_match_inside_a_longer_one(project):
    eid = fact_event(project, "Task t-066 is blocked on the review of Vietnam since 2026-10-02.")
    receipt = propose(project, fact_proposal(project, eid, quote="Task t-066 is blocked on the review of Vietnam",
                                             obj="Remy"))
    assert failed(receipt) == {"K-3": "field-not-in-span"}


def test_k3_the_predicate_must_be_llm_allowed(project):
    eid = fact_event(project)
    receipt = propose(project, fact_proposal(project, eid, predicate="proved_by"))
    assert failed(receipt) == {"K-3": "predicate-not-allowed"}
    assert propose(project, fact_proposal(project, eid, predicate="no_such_predicate")).status == "refused"


def test_k3_an_ambiguous_entity_stays_ambiguous(project):
    eid = fact_event(project, "Dat reviewed the capture of session c29 on 2026-10-02.")
    with store(project) as conn:
        for node, name in (("dat-a", "Dat"), ("dat-b", "Dat")):
            conn.execute("INSERT INTO nodes(node_id,display_name,valid_from,recorded_at,status) "
                         "VALUES(?,?,?,?,'active')", (node, name, "2026", "2026"))
            conn.execute("INSERT INTO aliases(node_id,surface,norm_surface,kind,status) "
                         "VALUES(?,?,?,?,'bound')", (node, name, "dat", "exact"))
    receipt = propose(project, fact_proposal(project, eid, quote="Dat reviewed the capture of session c29",
                                             subject="Dat", obj="c29", predicate="failed_with"))
    assert failed(receipt) == {"K-3": "ambiguous-entity"}
    assert [c.detail for c in receipt.checks if c.check == "K-3"] == ["field:subject"]


def test_k3_the_entailer_must_support_the_claim(project):
    eid = fact_event(project)
    from alpaca.wiki.config import Config
    from alpaca.wiki.store.db import DB

    class Abstains:
        name = "stub-abstains"

        def entail(self, claim, source):
            return "unsupported", 0.5

    proposal = intake.parse_proposal(fact_proposal(project, eid))
    db = DB(Config.for_project(project))
    try:
        db.pour()
        ctx = intake.Context(store=db, event_hash=lambda n: event_hash(project, n), entailer=Abstains())
        checked = intake.check_proposal(proposal, ctx)
    finally:
        db.close()
    assert checked.status == "refused" and checked.entailer == "stub-abstains"
    assert [(c.check, c.reason) for c in checked.checks if not c.passed] == [
        ("K-3", "sentence-unsupported")]


def test_k3_observation_and_summary_sentences_need_anchors_the_entailer_supports(project):
    eid = fact_event(project, "The capture of session c29 failed because the store was locked.")
    anchor = {"source_ref": "event:%d" % eid, "quote": "The capture of session c29 failed because "
              "the store was locked.", "hash": event_hash(project, eid)}
    ok = {"kind": "observation", "anchors": [anchor], "sentence_anchors": [[0]],
          "text": "The capture of session c29 failed because the store was locked.",
          "observation": {"label": "locked-store"}}
    # a refused proposal can be sent again, so the refusals come first and the passing one last
    bad_slug = dict(ok, observation={"label": "Locked Store"})
    assert failed(propose(project, bad_slug)) == {"K-3": "label-not-slug"}
    assert failed(propose(project, dict(ok, sentence_anchors=[]))) == {"K-3": "sentence-without-anchor"}
    unsupported = dict(ok, text="Lunar tides unplugged the warehouse robots entirely.")
    assert failed(propose(project, unsupported)) == {"K-3": "sentence-unsupported"}
    assert propose(project, ok).status == "checked"
    summary = dict(ok, kind="summary")
    summary.pop("observation")
    assert propose(project, summary).status == "checked"


def test_observation_with_invented_name_is_refused(project):
    """OB-13: every number, identifier-like token and capitalized word that does not start a sentence
    sits in one of the sentence's anchors, though the structural entailer passes 80% token coverage."""
    anchor_text = ("The capture of session c29 failed because the store was locked during the "
                   "SessionEnd hook.")
    eid = fact_event(project, anchor_text)
    anchor = {"source_ref": "event:%d" % eid, "quote": anchor_text, "hash": event_hash(project, eid)}

    def body(text, **more):
        return dict({"kind": "observation", "anchors": [anchor], "sentence_anchors": [[0]],
                     "text": text}, **more)

    invented = "The capture failed because Remy locked the store during the SessionEnd hook."
    hindsight = "The capture failed because Hindsight locked the store during the SessionEnd hook."
    # the entailer calls both supported: that is the gap the token check closes
    from alpaca.wiki.config import Config
    entailer = Config.for_project(project).providers().entailer
    assert entailer.entail(invented, anchor_text)[0] == "supported"
    assert entailer.entail(hindsight, anchor_text)[0] == "supported"
    for text in (invented, hindsight):
        receipt = propose(project, body(text))
        assert receipt.status == "refused" and failed(receipt) == {"K-3": "field-not-in-span"}
        assert [c.detail for c in receipt.checks if c.check == "K-3"] == ["sentence:0"]
    # the same name inside typographic quotes or guillemets, after a zero-width space, or glued to
    # a word by a dash other than "-" (op-008 t-068 check round 1, adversarial B1)
    for name in ("\u201cViet\u201d", "\u2018Hindsight\u2019", "\u00abViet\u00bb", "\u200bViet",
                 "store\u2014Viet"):
        text = "The capture failed because %s locked the store during the SessionEnd hook." % name
        receipt = propose(project, body(text))
        assert receipt.status == "refused" and failed(receipt) == {"K-3": "field-not-in-span"}, name
    # a name the anchor holds, in typographic quotes, is read without them and passes
    quoted = "The capture of session c29 failed during the \u2018SessionEnd\u2019 hook."
    assert propose(project, body(quoted)).status == "checked"
    # an invented number, and an identifier with a hyphen, are refused too (by the entailer or the
    # token check, whichever sees them first)
    for text in ("The capture of session c30 failed because the store was locked during the SessionEnd hook.",
                 "The capture failed because the store was locked during the SessionEnd hook in t-9."):
        assert failed(propose(project, body(text))).get("K-3") in ("field-not-in-span", "sentence-unsupported")
    # the anchor's own sentence passes, and so does a sentence whose tokens all sit in the anchor
    assert propose(project, body(anchor_text)).status == "checked"
    assert propose(project, body("The capture of session c29 failed during the SessionEnd hook.")).status == "checked"
    # the token must be in an anchor the sentence cites, not in any anchor of the proposal
    other = fact_event(project, "Remy reviewed the store.", ref="t-067")
    two = {"source_ref": "event:%d" % other, "quote": "Remy reviewed the store.",
           "hash": event_hash(project, other)}
    stitched = {"kind": "observation", "anchors": [anchor, two], "sentence_anchors": [[0]], "text": invented}
    assert failed(propose(project, stitched)) == {"K-3": "field-not-in-span"}
    cited = dict(stitched, sentence_anchors=[[0, 1]])
    assert propose(project, cited).status == "checked"


def test_sentence_fields_names_numbers_identifiers_and_inner_capitals():
    assert intake.sentence_fields("The capture failed because Remy locked it.") == ["Remy"]
    assert intake.sentence_fields("Task t-9 took 3 retries on 2026-10-02 in a_b/c.") == [
        "t-9", "3", "2026-10-02", "a_b/c"]
    assert intake.sentence_fields("Remy's store opened.") == []        # a possessive, at the start
    assert intake.sentence_fields("See Remy's note.") == ["Remy"]


# ------------------------------------------------------------------ K-4 and the redaction of OB-21
@pytest.mark.parametrize("how,reason", [("excluded", "excluded-anchor"), ("private", "private-anchor")])
def test_k4_a_non_servable_anchor_is_refused_and_leaves_no_quote_or_body(project, how, reason):
    eid = fact_event(project)
    hide(project, "event:%d" % eid, how)
    receipt = propose(project, fact_proposal(project, eid))
    assert receipt.status == "refused" and failed(receipt)["K-4"] == reason
    data = candidate_event(project, receipt)
    text = json.dumps(data)
    assert "Task t-066 is blocked" not in text and data["body"] is None
    assert data["body_sha256"] and data["anchors"][0]["quote"] is None
    assert data["anchors"][0]["quote_sha256"]
    with store(project) as conn:
        row = conn.execute("SELECT * FROM candidates").fetchone()
        assert row["body_json"] is None and row["body_sha256"] == data["body_sha256"]
        anchor = conn.execute("SELECT * FROM candidate_anchors").fetchone()
        assert anchor["quote"] is None and anchor["quote_sha256"] == data["anchors"][0]["quote_sha256"]


def test_k4_an_anchor_outside_the_scope_is_refused(project):
    eid = fact_event(project)
    receipt = propose(project, fact_proposal(project, eid, scope={"op": "op-77"}))
    assert failed(receipt) == {"K-4": "out-of-scope"}
    assert propose(project, fact_proposal(project, eid, scope={"op": "op-8"})).status == "checked"


# ------------------------------------------------------------------ K-5 and context
def test_k5_a_wiki_ingest_anchor_is_not_independent(project):
    ingest(project, "The heron gateway retries uploads three times.", "wiki/notes/heron.md")
    body = {"kind": "fact", "fact": {"subject": "heron gateway", "predicate": "related_to",
                                     "obj": "uploads", "obj_datatype": "node"},
            "anchors": [{"source_ref": "document:wiki/notes/heron.md",
                         "quote": "The heron gateway retries uploads"}]}
    receipt = propose(project, body)
    assert receipt.status == "refused" and failed(receipt) == {"K-5": "not-independent"}


def test_k5_a_transcript_or_briefing_anchor_is_not_independent(project):
    eid = fact_event(project)
    for ref in ("transcript:s-native", "briefing:today", "msg:1"):
        body = fact_proposal(project, eid)
        body["anchors"][0]["source_ref"] = ref
        assert failed(propose(project, body))["K-5"] == "not-independent", ref


def test_the_same_quote_given_as_context_is_stored_and_never_satisfies_k3(project):
    eid = fact_event(project)
    ingest(project, "Task t-066 is blocked on nothing at all.", "wiki/notes/blocked.md")
    body = fact_proposal(project, eid, quote="Task t-066 is on hold")      # not in the event note
    body["context"] = [{"source_ref": "document:wiki/notes/blocked.md", "quote": "Task t-066 is blocked"}]
    receipt = propose(project, body)
    assert receipt.status == "refused" and failed(receipt)["K-2"] == "quote-not-in-span"
    body = fact_proposal(project, eid, quote="Task t-066 is blocked", obj="on nothing")
    body["context"] = [{"source_ref": "document:wiki/notes/blocked.md",
                        "quote": "Task t-066 is blocked on nothing"}]
    receipt = propose(project, body)
    assert failed(receipt) == {"K-3": "field-not-in-span"}
    view = [v for v in service.candidates(project) if v.candidate_id == receipt.candidate_id][0]
    context = [a for a in view.anchors if a["role"] == "context"]
    assert len(context) == 1 and context[0]["checked"] is False
    assert context[0]["quote"] == "Task t-066 is blocked on nothing"      # public, found: kept in clear


def test_candidates_label_every_context_entry_unchecked(project):
    eid = fact_event(project)
    ingest(project, "Note: t-066 is blocked.", "wiki/notes/n.md")
    body = fact_proposal(project, eid)
    body["context"] = [{"source_ref": "document:wiki/notes/n.md", "quote": "t-066 is blocked"}]
    receipt = propose(project, body)
    assert receipt.status == "checked"
    (view,) = service.candidates(project)
    assert [(a["role"], a["checked"]) for a in view.anchors] == [("anchor", True), ("context", False)]


# ------------------------------------------------------------------ OB-22
def test_context_quote_not_in_named_source_is_hashed(project):
    eid = fact_event(project)
    public = fact_event(project, "The public note says the queue drains nightly.", ref="t-070")
    secret = "The vault passphrase is hunter2."
    ingest(project, secret, "wiki/notes/secret.md")
    hide(project, "document:wiki/notes/secret.md", "private")          # a quarantined copy
    body = fact_proposal(project, eid)
    body["context"] = [
        # text copied from the quarantined source but labelled with a servable public source_ref
        {"source_ref": "event:%d" % public, "quote": secret, "hash": event_hash(project, public)},
        # the quarantined source under its own, now unservable, ref
        {"source_ref": "document:wiki/notes/secret.md", "quote": secret},
        # a quote found in the public note it names: kept in clear
        {"source_ref": "event:%d" % public, "quote": "the queue drains nightly",
         "hash": event_hash(project, public)},
        # a transcript that is not captured yet does not resolve
        {"source_ref": "transcript:s-later", "quote": "an uncaptured line"}]
    receipt = propose(project, body)
    assert receipt.status == "checked" and failed(receipt) == {}        # context never refuses
    sealed = json.dumps(candidate_event(project, receipt))
    with store(project) as conn:
        stored = json.dumps([dict(r) for r in conn.execute("SELECT * FROM candidate_anchors")])
    shown = json.dumps([v.to_dict() for v in service.candidates(project)])
    for where in (sealed, stored, shown):
        assert "hunter2" not in where and "an uncaptured line" not in where
        assert "the queue drains nightly" in where
    hashed = [a for a in candidate_event(project, receipt)["anchors"]
              if a["role"] == "context" and a["quote"] is None]
    assert len(hashed) == 3 and all(a["quote_sha256"] for a in hashed)


def test_context_from_a_withdrawn_ingest_document_leaves_no_quote(project):
    eid = fact_event(project)
    ingest(project, "The audit code is 5823.", "wiki/notes/audit.md")
    hide(project, "document:wiki/notes/audit.md", "excluded")
    body = fact_proposal(project, eid)
    body["context"] = [{"source_ref": "document:wiki/notes/audit.md", "quote": "The audit code is 5823"}]
    receipt = propose(project, body)
    assert receipt.status == "checked"
    assert "5823" not in json.dumps(candidate_event(project, receipt))
    assert "5823" not in json.dumps([v.to_dict() for v in service.candidates(project)])


# ------------------------------------------------------------------ K-6
def test_k6_limits(project):
    eid = fact_event(project)
    body = fact_proposal(project, eid)
    body["anchors"] = body["anchors"] * 9
    assert failed(propose(project, body)) == {"K-6": "limit"}
    body = {"kind": "observation", "anchors": [fact_proposal(project, eid)["anchors"][0]],
            "sentence_anchors": [[0]], "text": "Task t-066 is blocked. " * 20}
    assert failed(propose(project, body))["K-6"] == "limit"
    body = {"kind": "summary", "anchors": body["anchors"], "sentence_anchors": [[0]],
            "text": "Task t-066 is blocked. " * 100}
    assert failed(propose(project, body))["K-6"] == "limit"


def test_k6_pending_limit_is_per_proposer(project):
    eid = fact_event(project)
    propose(project, fact_proposal(project, eid, quote="Task t-066 is blocked", obj="blocked"))
    with store(project) as conn:
        proto = dict(conn.execute("SELECT * FROM candidates").fetchone())
        for n in range(intake.PENDING_LIMIT - 1):
            proto.update(candidate_id="c_fill%d" % n, body_sha256="x%d" % n)
            conn.execute("INSERT INTO candidates(%s) VALUES(%s)" % (
                ",".join(proto), ",".join("?" * len(proto))), tuple(proto.values()))
    other = fact_proposal(project, eid, quote="Task t-066 is blocked on the review", obj="blocked")
    assert propose(project, other).status == "refused"                    # 200 checked rows for s-agent
    assert propose(project, other, session="s-other").status == "checked"


# ------------------------------------------------------------------ the event, kinds, no user-statement
def test_the_event_carries_the_six_results_the_status_and_the_entailer_name(project):
    eid = fact_event(project)
    receipt = propose(project, fact_proposal(project, eid))
    data = candidate_event(project, receipt)
    assert [c["check"] for c in data["checks"]] == ["K-1", "K-2", "K-3", "K-4", "K-5", "K-6"]
    assert data["status"] == "checked" and data["entailer"] and data["candidate_id"] == receipt.candidate_id
    assert data["body"]["fact"]["obj"] == "blocked"
    last = events(project)[-1]
    assert last["kind"] == "wiki-candidate" and last["actor"] == "alpaca-wiki"


def test_a_proposed_facts_kind_is_derived_from_its_anchor_kinds(project):
    assert intake.derive_fact_kind(["proof:a.md", "proof:b.md"]) == "agent-experience"
    assert intake.derive_fact_kind(["proof:a.md", "event:3"]) == "derived-hypothesis"
    assert intake.derive_fact_kind(["decision:dec-1"]) == "derived-hypothesis"
    assert intake.derive_fact_kind(["transcript:s1"]) == "derived-hypothesis"
    eid = fact_event(project)
    receipt = propose(project, fact_proposal(project, eid))
    assert candidate_event(project, receipt)["body"]["fact_kind"] == "derived-hypothesis"
    (view,) = service.candidates(project)
    assert view.body["fact_kind"] == "derived-hypothesis"


def test_no_candidate_or_claim_gets_user_statement(project):
    eid = fact_event(project)
    ingest(project, "The owner said t-066 is blocked.", "wiki/notes/said.md")
    body = fact_proposal(project, eid)
    body["context"] = [{"source_ref": "document:wiki/notes/said.md", "quote": "The owner said"}]
    propose(project, body)
    propose(project, fact_proposal(project, eid, quote="Task t-066 is blocked on the review"))
    assert "user-statement" not in json.dumps([e["data"] for e in events(project)])
    assert "user-statement" not in json.dumps([v.to_dict() for v in service.candidates(project)])
    with store(project) as conn:
        assert "user-statement" not in json.dumps([dict(r) for r in conn.execute(
            "SELECT * FROM candidates")] + [dict(r) for r in conn.execute("SELECT * FROM candidate_anchors")])


def test_a_malformed_proposal_raises_and_leaves_no_event(project):
    eid = fact_event(project)
    before = len(events(project))
    for bad in ({"kind": "fact"}, {"kind": "poem", "anchors": []}, "not an object",
                dict(fact_proposal(project, eid), candidate="c_other"),
                {"kind": "observation", "anchors": [], "text": "  "}):
        with pytest.raises(ValueError):
            propose(project, bad)
    assert len(events(project)) == before


def test_the_intake_module_imports_no_model_or_network_module():
    import alpaca.wiki.intake as module
    text = open(module.__file__, encoding="utf-8").read()
    for word in ("import socket", "import urllib", "import requests", "import openai", "anthropic"):
        assert word not in text


def test_the_alpaca_predicates_an_agent_may_use_are_marked_llm_allowed(project):
    from alpaca.wiki.store.db import LLM_ALLOWED_ALPACA
    fact_event(project)
    with store(project) as conn:           # a store seeded before t-066 held them as deterministic
        conn.execute("UPDATE predicates SET extraction='deterministic' WHERE predicate='status'")
    with store(project) as conn:           # opening it again marks them
        marked = {r[0] for r in conn.execute(
            "SELECT predicate FROM predicates WHERE extraction='llm-allowed'")}
        record_only = {r[0] for r in conn.execute(
            "SELECT predicate FROM predicates WHERE predicate IN ('in_op','proved_by','proves','operator')"
            " AND extraction='deterministic'")}
    assert marked == set(LLM_ALLOWED_ALPACA) | {"related_to"}
    assert record_only == {"in_op", "proved_by", "proves", "operator"}


# ------------------------------------------------------------------ fix-r1 B1: one spelling of an event ref
@pytest.mark.parametrize("how,reason", [("excluded", "excluded-anchor"), ("private", "private-anchor")])
@pytest.mark.parametrize("spelling", ["event:0%d", "event:00%d", "event:%d٠"])
def test_k4_a_second_spelling_of_an_event_ref_leaves_no_quote(project, how, reason, spelling):
    eid = fact_event(project)
    hide(project, "event:%d" % eid, how)
    body = fact_proposal(project, eid)
    body["anchors"][0]["source_ref"] = spelling % eid
    body["context"] = [dict(body["anchors"][0])]
    receipt = propose(project, body)
    assert receipt.status == "refused"
    assert "Task t-066 is blocked" not in json.dumps(candidate_event(project, receipt))
    with store(project) as conn:
        assert all(r["quote"] is None for r in conn.execute("SELECT quote FROM candidate_anchors"))


def test_servable_ref_names_one_spelling_of_an_event_ref(project):
    from alpaca.wiki.servable import servable_ref
    eid = fact_event(project)
    with store(project) as conn:
        assert servable_ref(conn, "event:%d" % eid)
        assert not servable_ref(conn, "event:0%d" % eid)
        assert intake.current_version(conn, "event:0%d" % eid) is None


# ------------------------------------------------------------------ fix-r1 B2: the fields sit in one anchor
def _two_anchor_body(project, first_ev, second_ev, first_q, second_q, obj):
    body = fact_proposal(project, first_ev, subject="Heron dock", predicate="status", obj=obj, quote=first_q)
    body["anchors"].append({"source_ref": "event:%d" % second_ev, "quote": second_q,
                            "hash": event_hash(project, second_ev)})
    return body


def test_k3_a_fact_stitched_from_two_anchors_is_refused(project):
    a = fact_event(project, "The Heron dock south hatch code is 3816.")
    b = fact_event(project, "The Puffin dock south hatch code is 7052.")
    body = _two_anchor_body(project, a, b, "The Heron dock south hatch code is 3816",
                            "The Puffin dock south hatch code is 7052", "7052")
    receipt = propose(project, body)
    assert receipt.status == "refused" and failed(receipt)["K-3"] == "field-not-in-span"


def test_the_accepted_edge_sits_on_the_anchor_that_holds_every_field(project):
    a = fact_event(project, "The Puffin dock south hatch code is 7052.")
    b = fact_event(project, "The Heron dock south hatch code is 3816.")
    body = _two_anchor_body(project, a, b, "The Puffin dock south hatch code is 7052",
                            "The Heron dock south hatch code is 3816", "3816")
    receipt = propose(project, body)
    assert receipt.status == "checked", failed(receipt)
    out = service.admit(project, receipt.candidate_id, session="s-admin", record=appender(project, "s-admin"))
    assert out.status == "applied", (out.status, out.reason)
    with store(project) as conn:
        rows = conn.execute("SELECT source_quote FROM edges WHERE extractor='llm' AND status='active'").fetchall()
    assert [r[0] for r in rows] == ["The Heron dock south hatch code is 3816"]
