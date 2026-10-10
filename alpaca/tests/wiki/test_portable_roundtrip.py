"""op-008 t-069 stage s2-portable: the `rune2-portable/1` export and import of the accepted state
(design/rune2-memory/architecture.md 4, 6.2, 10.4; FR-4, FR-23; OB-12, OB-19).

An export names the record cutoff and carries the accepted state by portable identities only; an
import pours a fresh store from the record through that cutoff and accepts no row the record does not
back; the round trip gives the same bytes and the same accepted digest, also under another provider
configuration (module replacement). Text of a source that fails the servable predicate never leaves
in an export (6.2, OB-12). A store built from the record, by a rebuild or an import, says on every
K-pinned read that its knowledge time comes from the record (OB-19). Real record, real drain, real
store, real gates: nothing is mocked; the hand-made inputs (a tampered export, a later capture landed
private) say so where they are made.
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import (CONFIG_A, CONFIG_B, T09, T10, T11, admit, appender, at, capture,  # noqa: E402
                            digest_of, edges, events, ingest, note, proposal, propose,
                            rebuild_store, reset_vault, set_config, sockets_blocked, store, withdraw)

from alpaca.wiki import portable, service                       # noqa: E402
from alpaca.wiki.clock import FixedClock                        # noqa: E402
from alpaca.wiki.determinism import sha256_hex                  # noqa: E402

T12 = "2026-03-01T12:00:00+00:00"
LESSON = "Retry after backoff cleared the locked store during the capture."
PRIOR = "The nightly export finished once the ledger lock was released."
LESSON_GONE = "Retry after a pause cleared the busy index during the rebuild."
PRIOR_GONE = "The weekly digest stalled while the archive lock was held."
WITHDRAWN = "Task t-10 is blocked on the zebra crossing review."
DOCUMENT = "The kestrel queue drains nightly at two.\n"
FALSIFY = "observe: a later note shows the locked store error absent after the backoff"


# ------------------------------------------------------------------ helpers
def export(project, name, **kw) -> Path:
    path = Path(project, name)
    portable.export_store(project, path, **kw)
    return path


def rows(path) -> list:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def strings(value) -> list:
    """Every string anywhere inside a decoded export row."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in strings(item)]
    if isinstance(value, (list, tuple)):
        return [s for item in value for s in strings(item)]
    return []


def move(project, to, ts, frm="open", task="t-9"):
    return at(project, "task-move", {"from": frm, "to": to, "proof": None, "reason": None}, ts,
              op="op-8", ref=task, actor="human")


def correct(project, target, action, **kw):
    return service.correct(project, target, action, session="s-owner",
                           record=appender(project, "s-owner"), reason="the record is wrong",
                           by="owner", **kw)


def withdraw_source(project, ref):
    return service.withdraw_source(project, ref, session="s-owner",
                                   record=appender(project, "s-owner"), reason="wrong source")


def corpus(project):
    lines = [("p1", "PASS", "retry after backoff of two seconds"),
             ("p2", "PASS", "retry after backoff of five seconds"),
             ("f1", "FAIL", "retry immediately in a tight loop"),
             ("f2", "FAIL", "retry immediately without waiting")]
    path = Path(project, "probe.jsonl")
    path.write_text("".join(json.dumps({"id": i, "tag": "store-lock", "outcome": o, "action": a}) + "\n"
                            for i, o, a in lines), encoding="utf-8")
    return str(path)


def observation(project, eid, text, kind):
    row = next(e for e in events(project) if e["id"] == eid)
    body = ({"label": "backoff-clears-lock-%d" % eid, "governs": ["store-lock"],
             "prescribes": "retry after backoff"} if kind == "lesson" else {"falsifiability": FALSIFY})
    return {"kind": "observation", "text": text, "sentence_anchors": [[0]], "observation": body,
            "anchors": [{"source_ref": "event:%d" % eid, "quote": text, "hash": row["hash"]}]}


def admitted_observation(project, text, kind, ts=T10, session="s-agent"):
    """A note holding `text`, and the lesson or prior an admitted observation of it wrote."""
    eid = note(project, text, ts)
    receipt = propose(project, observation(project, eid, text, kind), session=session)
    assert receipt.status == "checked", receipt.checks
    done = admit(project, receipt.candidate_id, probe=corpus(project) if kind == "lesson" else None)
    assert done.status == "applied", done
    return eid, receipt.candidate_id


def admitted_fact(project, body, quote, subject, ts=T10):
    eid = note(project, body, ts)
    cid = propose(project, proposal(project, eid, quote, subject=subject)).candidate_id
    assert admit(project, cid).status == "applied"
    return eid, cid


def build_store(project) -> dict:
    """A live store holding every kind of accepted row: record facts, an owner correction, an
    admitted agent fact, a `wiki ingest` document, an admitted lesson and prior, a withdrawn lesson
    and prior, and a withdrawn source with the fact admitted from it, which the owner retracted
    before the withdrawal (so a correction names a fact of the withdrawn source)."""
    move(project, "doing", T09, task="t-1")
    move(project, "done", T10, frm="doing", task="t-1")
    move(project, "doing", T09, task="t-2")
    capture(project)
    done = next(e for e in edges(project, predicate="status", subj_node="t-1")
                if e["obj_node"] == "status:done")
    assert correct(project, done["edge_id"], "supersede",
                   replacement={"obj": "status:blocked", "obj_datatype": "node"}).status == "applied"
    admitted_fact(project, "Task t-9 is blocked on the review.", "Task t-9 is blocked", "t-9")
    ingest(project, DOCUMENT, "wiki/notes/kestrel.md", name="kestrel.md")
    admitted_observation(project, LESSON, "lesson")
    admitted_observation(project, PRIOR, "prior", ts=T11, session="s-agent-2")
    _, lesson_gone = admitted_observation(project, LESSON_GONE, "lesson", ts=T11, session="s-agent-3")
    _, prior_gone = admitted_observation(project, PRIOR_GONE, "prior", ts=T12, session="s-agent-4")
    assert withdraw(project, lesson_gone, session="s-agent-3").status == "applied"
    assert withdraw(project, prior_gone, session="s-agent-4").status == "applied"
    gone, gone_cid = admitted_fact(project, WITHDRAWN, "Task t-10 is blocked", "t-10", ts=T11)
    (fact,) = [e for e in edges(project, subj_node="t-10") if e["extractor"] == "llm"]
    assert correct(project, fact["edge_id"], "retract").status == "applied"
    assert withdraw_source(project, "event:%d" % gone).status == "applied"
    capture(project)
    return {"withdrawn_event": gone, "withdrawn_candidate": gone_cid}


# ------------------------------------------------------------------ the round trip (FR-23)
def test_export_import_export_is_byte_identical(project):
    """Export, import into a fresh store, export: the same bytes and the same accepted digest; every
    row is imported, none refused, unrestorable or added; no provider-local id is in the export."""
    build_store(project)
    live = digest_of(project)
    with store(project) as conn:
        local_ids = [r[0] for r in conn.execute("SELECT edge_id FROM edges")]
        local_ids += [r[0] for r in conn.execute("SELECT block_id FROM blocks")]
    first = export(project, "first.jsonl")
    manifest, *body = rows(first)
    assert manifest["format"] == "rune2-portable/1" and manifest["private"] is False
    assert manifest["cutoff"] == max(e["id"] for e in events(project))
    assert manifest["rows"] == {k: sum(1 for r in body if r["kind"] == k) for k in portable.KINDS}
    assert all(manifest["rows"][k] for k in portable.KINDS), manifest["rows"]
    text = first.read_text(encoding="utf-8")
    assert local_ids and not [i for i in local_ids if i in text]
    reset_vault(project)
    report = portable.import_store(project, first)
    assert report["status"] == "ok", report
    assert (report["refused"], report["unrestorable"], report["from_record"]) == ([], {}, {})
    assert report["imported"] == manifest["rows"] and report["documents_landed"] == 1
    assert digest_of(project) == live
    second = export(project, "second.jsonl")
    assert second.read_bytes() == first.read_bytes()
    # the document came back through the write door, with its vault copy, and is served again
    assert Path(project, ".alpaca", "wiki", "wiki", "notes", "kestrel.md").read_text(
        encoding="utf-8") == DOCUMENT
    assert "kestrel" in json.dumps(service.query(project, "When does the kestrel queue drain?"))


def test_the_export_carries_the_fr4_envelope_and_derived_record_fields(project):
    """FR-4 verify: a source row names the source, its version hash, what it was made from, the
    operator, session, op and task, its valid time and privacy class, with unknown fields null; a
    derived row names its kind, provider and version, the sources that support it and its admission
    status. The record cutoff and its hash name the record the rows belong to."""
    build_store(project)
    record_rows = {e["id"]: e for e in events(project)}
    manifest, *body = rows(export(project, "out.jsonl"))
    assert manifest["cutoff_hash"] == record_rows[manifest["cutoff"]]["hash"]
    note_row = next(r for r in body if r["kind"] == "source" and r["source_ref"].startswith("event:")
                    and record_rows[int(r["source_ref"][6:])]["kind"] == "result")
    event = record_rows[int(note_row["source_ref"][6:])]
    assert note_row["origin_hash"] == event["hash"] and len(note_row["content_sha256"]) == 64
    assert (note_row["session"], note_row["op"], note_row["task"], note_row["private"],
            note_row["status"]) == (event["session"], "op-8", "t-9", 0, "current")
    assert note_row["source_time"] == event["ts"] and "observed_at" not in note_row
    doc = next(r for r in body if r["kind"] == "source" and r["source_ref"].startswith("document:"))
    assert (doc["origin_hash"], doc["op"], doc["task"]) == (None, None, None)      # unknown stays null
    assert doc["observed_at"] and doc["text"] == DOCUMENT and doc["private"] == 0
    fact = next(r for r in body if r["kind"] == "edge" and r["extractor"] == "llm" and r["status"] == "active")
    assert fact["extractor_version"] == "agent-candidate/1" and fact["anchor"]["quote_sha256"]
    (claim,) = fact["claims"]
    assert claim["origin"] == "candidate" and claim["fact_kind"] == "derived-hypothesis"
    ruled = next(r for r in body if r["kind"] == "edge" and r["claims"]
                 and r["claims"][0]["claim_ref"].startswith("rule:"))
    assert ruled["claims"][0]["fact_kind"] == "record-fact" and ruled["claims"][0]["source_event_id"]
    admitted = next(r for r in body if r["kind"] == "candidate" and r["status"] == "admitted")
    assert admitted["admission_event_id"] > admitted["record_event_id"] and admitted["admission_effect"]


def test_a_tampered_export_imports_none_of_its_changes(project):
    """An added active `extractor='llm'` edge, an admitted edge whose object was edited and an invented
    record-fact row: the import holds none of them and counts each as refused; the accepted digest
    is the live one, since the record, not the export, is what the store is poured from."""
    build_store(project)
    live = digest_of(project)
    lines = rows(export(project, "clean.jsonl"))
    facts = [r for r in lines if r["kind"] == "edge" and not r["redacted"]]
    admitted = next(r for r in facts if r["extractor"] == "llm" and r["status"] == "active")
    ruled = next(r for r in facts if r["extractor"] == "deterministic" and r["status"] == "active"
                 and r["claims"] and all(c["claim_ref"].startswith("rule:") for c in r["claims"]))
    added = dict(admitted, subject="t-77", object="unblocked",
                 claims=[dict(admitted["claims"][0], claim_ref="candidate:c_invented",
                              candidate_id="c_invented")])
    edited = dict(admitted, object="unblocked")
    invented = dict(ruled, subject="t-99")
    tampered = [r for r in lines if r is not admitted] + [added, edited, invented]
    path = Path(project, "tampered.jsonl")
    path.write_text("".join(json.dumps(r) + "\n" for r in tampered), encoding="utf-8")
    reset_vault(project)
    report = portable.import_store(project, path)
    assert sorted(r["reason"] for r in report["refused"]) == [
        "no-applied-admission", "no-applied-admission", "rule-does-not-reproduce"]
    assert {r["kind"] for r in report["refused"]} == {"edge"}
    # the edited row is refused and the admission the record holds is poured from the record
    assert report["from_record"] == {"edge": 1} and report["unrestorable"] == {}
    held = edges(project)
    assert not [e for e in held if e["subj_node"] in ("t-77", "t-99")]
    assert not [e for e in held if (e["obj_literal"] or e["obj_node"]) == "unblocked"]
    assert [e["obj_literal"] for e in held if e["extractor"] == "llm" and e["status"] == "active"] \
        == [admitted["object"]]
    assert digest_of(project) == live


def test_a_withdrawn_source_and_withdrawn_lessons_and_priors_leave_no_text_in_an_export(project):
    """A withdrawn source's anchor quotes and its candidates' bodies are null with their sha256; a
    withdrawn lesson and a withdrawn prior are written without their text; a scan of every string of
    the export finds none of the withdrawn text, and does find the text that stays."""
    made = build_store(project)
    lines = rows(export(project, "out.jsonl"))
    gone_ref = "event:%d" % made["withdrawn_event"]
    (candidate,) = [r for r in lines if r["kind"] == "candidate"
                    and r["candidate_id"] == made["withdrawn_candidate"]]
    assert candidate["status"] == "retracted"
    assert (candidate["body"], candidate["redacted"]) == (None, True) and candidate["body_sha256"]
    assert [(a["source_ref"], a["quote"], a["redacted"]) for a in candidate["anchors"]] == [
        (gone_ref, None, True)]
    assert candidate["anchors"][0]["quote_sha256"]
    (fact,) = [r for r in lines if r["kind"] == "edge" and r["anchor"]["source_ref"] == gone_ref]
    assert (fact["status"], fact["subject"], fact["object"], fact["anchor"]["quote"]) == (
        "retracted", None, None, None)
    assert fact["redacted"] and fact["subject_sha256"] and fact["anchor"]["quote_sha256"]
    assert {c["quote"] for c in fact["claims"]} == {None}
    # the owner's retraction of that fact names it by its identity, whose subject and object are
    # spans of the withdrawn quote (K-3): they travel as sha256 only, as the fact's own row does
    (fix,) = [r for r in lines if r["kind"] == "correction" and r["target"]["source_ref"] == gone_ref]
    assert (fix["action"], fix["redacted"]) == ("retract", True)
    assert (fix["target"]["subject"], fix["target"]["object_key"]) == (None, None)
    assert fix["target"]["subject_sha256"] == fact["subject_sha256"]
    assert fix["target"]["object_key_sha256"] and fix["reason"] == "the record is wrong"
    kept = [r for r in lines if r["kind"] == "correction" and r["target"]["source_ref"] != gone_ref]
    assert [(r["redacted"], r["target"]["subject"]) for r in kept] == [(False, "t-1")]
    lessons = [r for r in lines if r["kind"] == "lesson"]
    priors = [r for r in lines if r["kind"] == "prior"]
    assert sorted((r["event"], r["redacted"]) for r in lessons) == [
        ("lesson-admit", False), ("lesson-admit", True), ("lesson-withdraw", True)]
    assert sorted((r["decay_state"], r["redacted"]) for r in priors) == [("DECAYED", True), ("LIVE", False)]
    for row in lessons + priors:
        if row["redacted"]:
            assert row.get("block") is None and row.get("claim") is None
            if row.get("event") != "lesson-withdraw":       # a withdraw row holds no block at all
                assert row.get("block_sha256") or row.get("claim_sha256")
    text = "\n".join(s for r in lines for s in strings(r))
    for withdrawn in (WITHDRAWN, "zebra crossing", "t-10", LESSON_GONE, PRIOR_GONE, "busy index",
                      "weekly digest"):
        assert withdrawn not in text, withdrawn
    for kept in (LESSON, PRIOR, DOCUMENT):
        assert kept in text, kept


def test_a_correction_carries_no_text_of_a_withdrawn_source(project):
    """Check round 1 (contract B1, invariants B2, adversarial B1): a correction names its fact by
    subject and object, which are spans of the fact's anchor quote (K-3). When the fact's source is
    withdrawn after the owner retracted or superseded the fact, the correction row carries them as
    sha256 only; when the owner's own correction note is withdrawn, its replacement and reason go
    the same way. A scan of every string of the export, public or `--private`, finds none of the
    withdrawn text, and the round trip still gives the same bytes and the same accepted digest."""
    def fact_of(body, quote, subject, obj, ts):
        eid = note(project, body, ts)
        cid = propose(project, proposal(project, eid, quote, subject=subject, predicate="related_to",
                                        obj=obj)).candidate_id
        assert admit(project, cid).status == "applied"
        (edge,) = [e for e in edges(project) if e["extractor"] == "llm" and e["obj_literal"] == obj]
        return eid, edge["edge_id"]

    heron, heron_fact = fact_of("The heron gateway kept the zebra crossing keys in the ledger.",
                                "The heron gateway kept the zebra crossing keys", "heron gateway",
                                "zebra crossing keys", T09)
    osprey, osprey_fact = fact_of("The osprey relay kept the amber lantern codes in the shed.",
                                  "The osprey relay kept the amber lantern codes", "osprey relay",
                                  "amber lantern codes", T10)
    _, plover_fact = fact_of("The plover dock kept the copper ledger seals in the vault.",
                             "The plover dock kept the copper ledger seals", "plover dock",
                             "copper ledger seals", T11)
    assert correct(project, heron_fact, "retract").status == "applied"
    assert correct(project, osprey_fact, "supersede",
                   replacement={"obj": "harbour office", "obj_datatype": "string"}).status == "applied"
    moved = correct(project, plover_fact, "supersede",
                    replacement={"obj": "brass mooring tags", "obj_datatype": "string"})
    assert moved.status == "applied"
    for ref in ("event:%d" % heron, "event:%d" % osprey, "event:%d" % moved.record_event_id):
        assert withdraw_source(project, ref).status == "applied"
    capture(project)
    live = digest_of(project)
    public, wide = export(project, "public.jsonl"), export(project, "wide.jsonl", include_private=True)
    for path in (public, wide):
        lines = rows(path)
        fixes = {r["target"]["source_ref"]: r for r in lines if r["kind"] == "correction"}
        for ref, subject, obj in (("event:%d" % heron, "heron gateway", "zebra crossing keys"),
                                  ("event:%d" % osprey, "osprey relay", "amber lantern codes")):
            row = fixes[ref]
            assert row["redacted"] is True
            assert (row["target"]["subject"], row["target"]["object_key"]) == (None, None)
            assert row["target"]["subject_sha256"] == sha256_hex(subject)
            assert row["target"]["object_key_sha256"] == sha256_hex("lit:string:" + obj)
        # the owner's supersede of the osprey fact keeps its replacement: that is the owner's own
        # note, which stays served; the plover correction's note is withdrawn, so its replacement
        # and reason leave as sha256, while its target, a fact of a served note, stays readable
        assert fixes["event:%d" % osprey]["replacement"]["obj"] == "harbour office"
        plover = [r for r in lines if r["kind"] == "correction"
                  and r["record_event_id"] == moved.record_event_id][0]
        assert (plover["redacted"], plover["replacement"], plover["reason"]) == (True, None, None)
        assert plover["replacement_sha256"] and plover["reason_sha256"] == sha256_hex("the record is wrong")
        assert plover["target"]["subject"] == "plover dock"
        text = "\n".join(s for r in lines for s in strings(r))
        for withdrawn in ("heron gateway", "zebra", "crossing keys", "amber lantern", "brass mooring"):
            assert withdrawn not in text, (path.name, withdrawn)
    reset_vault(project)
    report = portable.import_store(project, public)
    assert report["status"] == "ok", report
    assert (report["refused"], report["unrestorable"], report["from_record"]) == ([], {}, {})
    assert digest_of(project) == live
    assert export(project, "again.jsonl").read_bytes() == public.read_bytes()


# ------------------------------------------------------------------ OB-12
def _quarantined_capture(project, event_id, text):
    """Hand-made: a later capture of the same event source that landed private under wiki/private/
    (the quarantine route of architecture.md 9.5, as T/test_servable_set.py makes it)."""
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import Absorber
    header = "op: op-8\nrole: result\nkey: t-9\nkind: result\nevent: %d\nts: x\n\n" % event_id
    path = "wiki/private/raw/op-8/%06d-result-quarantined.md" % event_id
    cfg = Config.for_project(project)
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    absorber = Absorber(cfg, clock=FixedClock(start=T12))
    try:
        absorber.absorb_text(path, header + text + "\n", kind="raw", path=path)
    finally:
        absorber.close()


def _current_version_private(project, source_ref):
    """Hand-made: the document of the source's current version turns private (as a private
    reclassification would leave it, architecture.md 6.2)."""
    with store(project) as conn:
        conn.execute("UPDATE docs SET private=1 WHERE doc_id IN (SELECT doc_id FROM source_versions "
                     "WHERE source_ref=? AND status='current')", (source_ref,))


def test_lesson_from_quarantined_source_exported_without_text(project):
    """OB-12: a lesson and a prior admitted from a source whose later capture is quarantined, and
    from one whose current version is private, export with their sha256, `redacted: true` and no
    text; a scan of the export finds none of their text. A lesson of a servable source keeps its
    text, and the private rows come back on request (`include_private`). The owner's correction of
    a fact read from such a source names the fact by its sha256 only, as the fact's own row does."""
    texts = {}
    for name, ts, session in (("q", T10, "s-q"), ("p", T11, "s-p")):
        for kind in ("lesson", "prior"):
            text = "%s cleared the %s store during the %s capture." % (
                "Retry after backoff" if kind == "lesson" else "The nightly export", name + "-" + kind,
                {"q": "quarantined", "p": "private"}[name])
            eid, cid = admitted_observation(project, text, kind, ts=ts, session=session + kind)
            texts[(name, kind)] = (text, eid, cid)
            if kind == "lesson":
                # an agent fact read from the same note, which the owner then retracts
                quote = "Retry after backoff cleared the %s-lesson store" % name
                fid = propose(project, proposal(project, eid, quote, subject="Retry after backoff",
                                                predicate="related_to",
                                                obj="%s-lesson store" % name)).candidate_id
                assert admit(project, fid).status == "applied"
                (fact,) = [e for e in edges(project) if e["extractor"] == "llm"
                           and e["obj_literal"] == "%s-lesson store" % name]
                assert correct(project, fact["edge_id"], "retract").status == "applied"
    admitted_observation(project, LESSON, "lesson", ts=T12, session="s-kept")
    _quarantined_capture(project, texts[("q", "lesson")][1], "A later capture of the note, private.")
    _quarantined_capture(project, texts[("q", "prior")][1], "Another later capture, private.")
    _current_version_private(project, "event:%d" % texts[("p", "lesson")][1])
    _current_version_private(project, "event:%d" % texts[("p", "prior")][1])
    lines = rows(export(project, "public.jsonl"))
    gated = {cid for _, _, cid in texts.values()}
    with store(project) as conn:
        refs = {r[0]: r[1] for r in conn.execute("SELECT candidate_id, admission_ref FROM candidates")}
    hidden = {refs[cid] for cid in gated}
    out = [r for r in lines if r["kind"] in ("lesson", "prior")
           and (r.get("id") or r.get("claim_id")) in hidden]
    assert len(out) == 4
    for row in out:
        assert row["redacted"] is True
        assert row.get("block") is None and row.get("claim") is None and row.get("falsifiability") is None
        assert row.get("block_sha256") or row.get("claim_sha256")
    for row in (r for r in lines if r["kind"] == "candidate" and r["candidate_id"] in gated):
        assert row["body"] is None and {a["quote"] for a in row["anchors"]} == {None}
    fixes = [r for r in lines if r["kind"] == "correction"]
    assert sorted(r["target"]["source_ref"] for r in fixes) == sorted(
        "event:%d" % texts[(name, "lesson")][1] for name in ("q", "p"))
    assert {(r["redacted"], r["target"]["subject"], r["target"]["object_key"]) for r in fixes} == {
        (True, None, None)}
    assert all(r["target"]["subject_sha256"] and r["target"]["object_key_sha256"] for r in fixes)
    scan = "\n".join(s for r in lines for s in strings(r))
    for text, _, _ in texts.values():
        assert text not in scan
    for word in ("q-lesson", "q-prior", "p-lesson", "p-prior", "private capture", "quarantined capture"):
        assert word not in scan, word
    assert LESSON in scan
    # private rows only on request: the same export with include_private keeps their text
    wide_rows = rows(export(project, "wide.jsonl", include_private=True))
    wide = "\n".join(s for r in wide_rows for s in strings(r))
    assert all(text in wide for text, _, _ in texts.values())
    assert sorted(r["target"]["object_key"] for r in wide_rows if r["kind"] == "correction") == [
        "lit:string:p-lesson store", "lit:string:q-lesson store"]


def test_a_lesson_no_admission_wrote_leaves_without_its_text(project):
    """Fail closed: a lesson the store holds with no candidate whose admission wrote it (hand-made
    through the gate's own replay method) is exported as sha256 only, like a withdrawn one."""
    from alpaca.lessons import LessonsStore
    note(project, "A note so the store exists.", T10)
    with store(project) as conn:
        LessonsStore(conn).replay_admitted({"id": "L-orphan", "description": "An orphan lesson text.",
                                            "value": "An orphan lesson text."}, run_id="none")
    (row,) = [r for r in rows(export(project, "out.jsonl")) if r["kind"] == "lesson"]
    assert (row["id"], row["block"], row["redacted"]) == ("L-orphan", None, True) and row["block_sha256"]


# ------------------------------------------------------------------ OB-19
K_BETWEEN = "2026-03-01T21:00:00+00:00"
QUESTION = "What did the heron gateway retry?"


def _late_capture(project):
    """An event at 09:00 that the live store captures one day later (live capture lags the event)."""
    from alpaca.wiki.ingest import drain
    eid = at(project, "result", {"body": "The heron gateway retried the uploads three times."}, T09)
    drain.capture(project, events(project, {eid}), clock=FixedClock(start="2026-03-02T09:00:00+00:00"))
    return eid


def _notes(result) -> tuple:
    return tuple(result.get("trace") or ())


def test_rebuild_flags_knowledge_time(project):
    """OB-19: an event captured one day after its `ts`; a K-pinned read with K between the two times
    carries `knowledge-time-from-record` on the rebuild, which knew the event at its `ts`, and the
    same read on the live store, which did not know it yet, carries none; an unpinned read on the
    rebuild carries none. `wiki context` prints the note."""
    _late_capture(project)
    assert service.status(project)["knowledge_time"] == "capture"
    live = service.query(project, QUESTION, knowledge_as_of=K_BETWEEN)
    assert "knowledge-time-from-record" not in _notes(live)
    assert "heron" not in json.dumps(live.get("citations"))          # live knew nothing at K
    report = rebuild_store(project)
    assert report["status"] == "ok" and report["knowledge_time"] == "record"
    with store(project) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='knowledge_time'").fetchone()[0] == "record"
        assert {r[0] for r in conn.execute("SELECT recorded_at FROM blocks WHERE doc_id LIKE 'raw/%'")} \
            == {T09}
    pinned = service.query(project, QUESTION, knowledge_as_of=K_BETWEEN)
    assert "knowledge-time-from-record" in _notes(pinned)
    assert "heron" in json.dumps(pinned.get("citations"))            # the rebuild knew it at its ts
    assert "knowledge-time-from-record" not in _notes(service.query(project, QUESTION))
    text = service.context(project, QUESTION + " as known at 2026-03-01")
    assert "Trace: knowledge-time-from-record" in text


def test_import_flags_knowledge_time(project):
    """OB-19: on a store imported from an export of the live store, the same K-pinned read carries
    `knowledge-time-from-record` and an unpinned read carries none."""
    _late_capture(project)
    assert "knowledge-time-from-record" not in _notes(
        service.query(project, QUESTION, knowledge_as_of=K_BETWEEN))
    path = export(project, "live.jsonl")
    reset_vault(project)
    report = portable.import_store(project, path)
    assert report["status"] == "ok" and report["knowledge_time"] == "record"
    with store(project) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='knowledge_time'").fetchone()[0] == "record"
    assert service.status(project)["knowledge_time"] == "record"
    assert "knowledge-time-from-record" in _notes(
        service.query(project, QUESTION, knowledge_as_of=K_BETWEEN))
    assert "knowledge-time-from-record" not in _notes(service.query(project, QUESTION))


def test_a_live_catch_up_keeps_capture_time(project):
    """`wiki recover` on a live store lands what capture missed at the time it learns it, and does not
    flag the store; on a rebuilt store a catch-up keeps the flag the rebuild set."""
    from alpaca.wiki import recovery
    _late_capture(project)
    missed = at(project, "result", {"body": "The osprey relay restarted twice."}, T10)
    report = recovery.run(project, session="s-recover")
    assert report["knowledge_time"] == "capture"
    with store(project) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='knowledge_time'").fetchone() is None
        stamps = {r[0] for r in conn.execute(
            "SELECT b.recorded_at FROM blocks b JOIN source_versions v ON v.doc_id=b.doc_id "
            "WHERE v.source_ref=?", ("event:%d" % missed,))}
    assert stamps and T10 not in stamps
    assert "knowledge-time-from-record" not in _notes(
        service.query(project, QUESTION, knowledge_as_of=K_BETWEEN))
    rebuild_store(project)
    at(project, "result", {"body": "The osprey relay restarted a third time."}, T11)
    assert recovery.run(project, session="s-recover")["knowledge_time"] == "record"


# ------------------------------------------------------------------ disclosed limits and refusals
def test_an_import_holds_no_replaced_version(project):
    """ARCH 10.4: an export carries no replaced version and an import holds none. On the live store
    the replaced text stays visible at an earlier K (ARCH 5.2); after the import it is gone."""
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import Absorber
    cfg = Config.for_project(project)
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    absorber = Absorber(cfg, clock=FixedClock(start=T09))
    try:
        absorber.absorb_text("wiki/notes/osprey.md", "The osprey relay runs on port one.\n",
                             kind="wiki", path="wiki/notes/osprey.md")
        absorber.writer.clock = lambda: T11
        absorber.absorb_text("wiki/notes/osprey.md", "The osprey relay moved to port two.\n",
                             kind="wiki", path="wiki/notes/osprey.md")
    finally:
        absorber.close()
    vault = Path(project, ".alpaca", "wiki", "wiki", "notes")
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "osprey.md").write_text("The osprey relay moved to port two.\n", encoding="utf-8")
    at(project, "result", {"body": "A note so the record has an event."}, T12)
    capture(project)
    with store(project) as conn:
        assert {r[0] for r in conn.execute("SELECT status FROM source_versions WHERE "
                                           "source_ref='document:wiki/notes/osprey.md'")} == {"replaced", "current"}
    early = service.query(project, "Which port does the osprey relay run on?", knowledge_as_of=T10)
    assert "port one" in json.dumps(early)
    path = export(project, "out.jsonl")
    assert "port one" not in path.read_text(encoding="utf-8")
    reset_vault(project)
    report = portable.import_store(project, path)
    assert report["refused"] == [] and report["documents_landed"] == 1
    with store(project) as conn:
        assert {r[0] for r in conn.execute("SELECT status FROM source_versions")} == {"current"}
        assert conn.execute("SELECT recorded_at FROM source_versions WHERE "
                            "source_ref='document:wiki/notes/osprey.md'").fetchone()[0] == T11
    assert "port one" not in json.dumps(
        service.query(project, "Which port does the osprey relay run on?", knowledge_as_of=T12))


def test_the_import_refuses_a_store_that_holds_something_and_another_record(project):
    note(project, "The lantern rota moved to Tuesday.", T10)
    move(project, "doing", T10)                 # a record fact, so the store's ledger has rows
    capture(project)
    path = export(project, "out.jsonl")
    refused = portable.import_store(project, path)
    assert (refused["status"], refused["reason"]) == ("refused", "store-not-empty")
    # the store file alone moved aside: the ledger of the old store is still there, which the Writer
    # would refuse a new store behind; the import says so instead of failing
    vault = Path(project, ".alpaca", "wiki")
    assert (vault / "ledger" / "events.jsonl").stat().st_size > 0
    (vault / "rune.db").rename(Path(project, "old-rune.db"))
    assert portable.import_store(project, path)["reason"] == "store-not-empty"
    assert not (vault / "rune.db").exists()
    manifest, *body = rows(path)
    for change, reason in ((dict(manifest, cutoff_hash="0" * 64), "record-mismatch"),
                           (dict(manifest, cutoff=manifest["cutoff"] + 5), "record-behind-cutoff")):
        other = Path(project, "other.jsonl")
        other.write_text("".join(json.dumps(r) + "\n" for r in [change] + body), encoding="utf-8")
        reset_vault(project)
        out = portable.import_store(project, other)
        assert (out["status"], out["reason"]) == ("refused", reason)
        assert not Path(project, ".alpaca", "wiki", "rune.db").exists()
    with pytest.raises(ValueError, match="rune2-portable/1"):
        bad = Path(project, "bad.jsonl")
        bad.write_text(json.dumps({"kind": "edge"}) + "\n", encoding="utf-8")
        portable.import_store(project, bad)


def test_documents_private_only_on_request_and_a_withdrawn_document_keeps_its_envelope(project):
    """A public export leaves a private document's text out and the import counts it as
    unrestorable; asked for, the text travels and the document lands private again. A withdrawn
    document's text never travels; the import restores its envelope, which the record's withdrawal
    backs."""
    secret = Path(project, "secret.md")
    secret.write_text("The heron vault code word is amber.\n", encoding="utf-8")
    service.ingest(project, secret, doc_id="wiki/private/heron.md", session="s-doc")
    ingest(project, "The gone page said the relay was green.\n", "wiki/notes/gone.md", name="gone.md")
    assert withdraw_source(project, "document:wiki/notes/gone.md").status == "applied"
    ingest(project, DOCUMENT, "wiki/notes/kestrel.md", name="kestrel.md")
    live = digest_of(project)
    public = export(project, "public.jsonl")
    text = public.read_text(encoding="utf-8")
    assert "amber" not in text and "relay was green" not in text and "kestrel" in text
    wide = export(project, "wide.jsonl", include_private=True)
    text = wide.read_text(encoding="utf-8")
    assert "amber" in text and "relay was green" not in text
    reset_vault(project)
    report = portable.import_store(project, public)
    assert report["unrestorable"] == {"source": 1} and report["refused"] == []
    assert report["envelopes_restored"] == 1 and report["documents_landed"] == 1
    with store(project) as conn:
        gone = [tuple(r) for r in conn.execute("SELECT status, private FROM source_versions "
                                               "WHERE source_ref='document:wiki/notes/gone.md'")]
        excluded = [r[0] for r in conn.execute("SELECT source_ref FROM exclusions")]
    assert gone == [("withdrawn", 0)] and excluded == ["document:wiki/notes/gone.md"]
    reset_vault(project)
    out = portable.import_store(project, wide)
    assert (out["refused"], out["unrestorable"], out["from_record"]) == ([], {}, {})
    assert out["documents_landed"] == 2 and digest_of(project) == live
    assert export(project, "wide-again.jsonl", include_private=True).read_bytes() == wide.read_bytes()
    with store(project) as conn:
        assert conn.execute("SELECT private FROM docs WHERE doc_id='wiki/private/heron.md'").fetchone()[0] == 1
    assert "amber" not in json.dumps(service.query(project, "What is the heron vault code word?"))


# ------------------------------------------------------------------ module replacement and K1
def test_another_provider_configuration_imports_the_same_accepted_state(project):
    """Module replacement (ARCH 10.3): a store built under configuration A (narrow, identity,
    structural), exported and imported under B (hybrid, lexical, ensemble), holds the same accepted
    digest and exports the same bytes. Accepted knowledge does not depend on regenerating any
    provider output."""
    set_config(project, CONFIG_A)
    build_store(project)
    live = digest_of(project)
    first = export(project, "a.jsonl")
    set_config(project, CONFIG_B)
    reset_vault(project)
    report = portable.import_store(project, first)
    assert report["refused"] == [] and report["from_record"] == {}
    assert digest_of(project) == live
    assert export(project, "b.jsonl").read_bytes() == first.read_bytes()


def test_export_and_import_run_with_sockets_blocked(project):
    """K1 (ARCH 9.1; interfaces.py.txt section 5): export and import make no network call."""
    move(project, "doing", T09)
    admitted_fact(project, "Task t-9 is blocked on the review.", "Task t-9 is blocked", "t-9")
    before = set(sys.modules)
    with sockets_blocked():
        first = export(project, "first.jsonl")
        reset_vault(project)
        report = portable.import_store(project, first)
        second = export(project, "second.jsonl")
    assert report["refused"] == [] and second.read_bytes() == first.read_bytes()
    assert not {m.split(".")[0] for m in set(sys.modules) - before} & {
        "requests", "urllib3", "httpx", "openai", "anthropic", "torch", "sentence_transformers"}


# ------------------------------------------------------------------ the secret screen (FR-26, PO-7)
SECRET = "SYNTHCANARY-pass-5d02"


def test_an_export_masks_text_a_store_kept_from_before_the_screen(project, monkeypatch):
    """PO-7: no secret leaves in an export. A store whose notes, quotes and documents were captured
    before the secret screen (hand-made: the screen switched off while they land, as before op-008
    t-069 s1) exports them through the screen, and a document stored unscreened goes without its
    text."""
    from alpaca.wiki.ingest import drain
    with monkeypatch.context() as m:
        m.setattr(drain, "screen", lambda text: text)
        eid = note(project, "The glasshouse vent admin password is %s." % SECRET, T10)
        cid = propose(project, proposal(project, eid, "The glasshouse vent admin password is " + SECRET,
                                        subject="glasshouse vent", predicate="related_to",
                                        obj="admin password")).candidate_id
        assert admit(project, cid).status == "applied"
        ingest(project, "The pump house service password is %s.\n" % SECRET, "wiki/notes/gate.md",
               name="gate.md")
    with store(project) as conn:                     # the control: the store holds it in clear
        assert conn.execute("SELECT COUNT(*) FROM blocks WHERE text LIKE ?",
                            ("%" + SECRET + "%",)).fetchone()[0] >= 2
    path = export(project, "out.jsonl", include_private=True)
    text = path.read_text(encoding="utf-8")
    assert SECRET not in text and "[redacted]" in text
    (doc,) = [r for r in rows(path) if r["kind"] == "source"
              and r["source_ref"] == "document:wiki/notes/gate.md"]
    assert doc["text"] is None and doc["redacted"] is False


def test_the_export_and_import_verbs(project, capsys):
    """`bin/alpaca wiki export` writes the file readable by its owner only; `wiki import` refuses a
    store that holds something (exit 2) and pours an empty one (exit 0)."""
    from alpaca import cli
    note(project, "The lantern rota moved to Tuesday.", T10)
    path = Path(project, "cli.jsonl")
    capsys.readouterr()
    assert cli.main(["wiki", "export", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["format"] == "rune2-portable/1"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert cli.main(["wiki", "import", str(path)]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "store-not-empty"
    reset_vault(project)
    assert cli.main(["wiki", "import", str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok" and report["refused"] == []


# ------------------------------------------------------------------ documents and transcripts
def test_a_tampered_document_or_envelope_is_refused(project):
    """A document whose text no longer hashes to its version is not landed, and a withdrawn envelope
    the record does not back (no withdrawal of that document) is not written: both are refused."""
    ingest(project, DOCUMENT, "wiki/notes/kestrel.md", name="kestrel.md")
    note(project, "A note so the record has an event.", T10)
    lines = rows(export(project, "clean.jsonl"))
    doc = next(r for r in lines if r["kind"] == "source"
               and r["source_ref"] == "document:wiki/notes/kestrel.md")
    edited = dict(doc, text="The kestrel queue drains at noon.\n")
    invented = dict(doc, source_ref="document:wiki/notes/never.md", status="withdrawn", text=None,
                    redacted=True)
    path = Path(project, "tampered.jsonl")
    path.write_text("".join(json.dumps(r) + "\n" for r in [r for r in lines if r is not doc]
                            + [edited, invented]), encoding="utf-8")
    reset_vault(project)
    report = portable.import_store(project, path)
    assert sorted(r["reason"] for r in report["refused"]) == ["document-not-landed"] * 2
    assert report["documents_not_landed"] == ["document:wiki/notes/kestrel.md"]
    assert report["envelopes_restored"] == 0
    with store(project) as conn:
        assert not conn.execute("SELECT 1 FROM source_versions WHERE source_ref LIKE 'document:%'").fetchone()
    assert "noon" not in json.dumps(service.query(project, "When does the kestrel queue drain?"))


def _transcript(project, sid, text):
    """A Claude transcript under .alpaca/transcripts/, registered for `sid` as the hooks register one."""
    from alpaca import observability
    path = Path(project, ".alpaca", "transcripts", sid + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "user", "message": {"content": "What happened to the osprey relay?"}},
             {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}]
    path.write_text("".join(json.dumps(r) + "\n" for r in lines), encoding="utf-8")
    observability.expect_source(project, sid, "claude", locator=str(path))
    return path


def test_a_transcript_comes_back_from_its_file_and_a_lost_one_is_counted(project):
    """A transcript is rebuilt by an import from its file, at the record time of its session's last
    event, and the round trip is byte-identical; when the file is gone the import is incomplete and
    counts the transcript as unrestorable, refusing nothing."""
    path = _transcript(project, "s-talk", "The osprey relay restarted after the patch.")
    at(project, "result", {"body": "A note in the talk session."}, T10, session="s-talk")
    service.capture(project, sessions=["s-talk"], session_events=True, deadline_s=None)
    first = export(project, "first.jsonl")
    assert [r["source_ref"] for r in rows(first) if r["kind"] == "source"
            and r["source_ref"].startswith("transcript:")] == ["transcript:s-talk"]
    reset_vault(project)
    report = portable.import_store(project, first)
    assert (report["status"], report["refused"], report["unrestorable"]) == ("ok", [], {})
    assert export(project, "second.jsonl").read_bytes() == first.read_bytes()
    with store(project) as conn:
        stamps = {r[0] for r in conn.execute(
            "SELECT b.recorded_at FROM blocks b JOIN source_versions v ON v.doc_id=b.doc_id "
            "WHERE v.source_ref='transcript:s-talk'")}
    assert stamps == {T10}
    path.unlink()
    reset_vault(project)
    lost = portable.import_store(project, first)
    assert (lost["status"], lost["refused"], lost["unrestorable"]) == ("incomplete", [], {"source": 1})
    assert [m["session"] for m in lost["replay"]["missing_transcripts"]] == ["s-talk"]


def test_a_transcript_projection_names_no_machine_path_or_user(project, tmp_path):
    """t-417 and its disclosure (t-070 check round 1 minor A-10): a transcript registered at an
    absolute path under an operator's home folder lands with no part of that path in the stored
    projection, its blocks, its vault file or an export, private rows included; the same bytes read
    from another path (the project-local copy) render the same document, so the doc id and the
    content_sha256 depend on the transcript's content only."""
    from alpaca import observability, transcripts
    from alpaca.wiki.ingest import drain
    home = tmp_path / "home" / "heron-operator-7731"
    native = home / ".claude" / "projects" / "-srv-heron" / "s-home.jsonl"
    native.parent.mkdir(parents=True)
    lines = [{"type": "user", "message": {"content": "What happened to the osprey relay?"}},
             {"type": "assistant", "message": {"content": [{"type": "text",
                                                            "text": "The osprey relay restarted."}]}}]
    native.write_text("".join(json.dumps(r) + "\n" for r in lines), encoding="utf-8")
    observability.expect_source(project, "s-home", "claude", locator=str(native))
    at(project, "result", {"body": "A note in the home session."}, T10, session="s-home")
    service.capture(project, sessions=["s-home"], session_events=True, deadline_s=None)
    marks = (str(home), "heron-operator-7731", str(tmp_path))
    with store(project) as conn:
        (doc_id,) = [r[0] for r in conn.execute(
            "SELECT doc_id FROM source_versions WHERE source_ref='transcript:s-home'")]
        texts = [r[0] for r in conn.execute("SELECT text FROM blocks WHERE doc_id=?", (doc_id,))]
    stored = Path(project, ".alpaca", "wiki", doc_id).read_text(encoding="utf-8")
    exported = export(project, "private.jsonl", include_private=True).read_text(encoding="utf-8")
    for where, text in [("vault", stored), ("export", exported), ("doc_id", doc_id)] + [
            ("block", t) for t in texts]:
        assert not [m for m in marks if m in text], (where, text[:400])
    copy = Path(transcripts.local_path(project, "s-home"))
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(native.read_bytes())
    assert drain._transcript_doc("s-home", str(copy)) == drain._transcript_doc("s-home", str(native))
