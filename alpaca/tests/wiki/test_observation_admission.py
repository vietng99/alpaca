"""op-008 t-067 stage s2-observe: the observation doors of `admit` (design/rune2-memory/architecture.md
7.4; FR-16, FR-33; OB-06, OB-15; dec-38940e1fb35f Q7).

A procedural observation goes to `lessons.write` with a probe corpus, a declarative one to
`KnowledgeStore.propose` as a QUARANTINE prior. Rune2 derives the description, the provenance, the run
id and the gate ids from the checked text and the candidate id. Real record, real drain, real store,
the real gates: nothing here is mocked.
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import (T10, T11, admit, at, digest_of, events, note, propose,  # noqa: E402
                            rebuild_store, row_of, store, wiki_events, withdraw)

from alpaca import knowledge, lessons                          # noqa: E402
from alpaca.wiki import intake                                 # noqa: E402

TEXT = "Retry after backoff cleared the locked store during the capture."
TEXT_B = "Retry after backoff cleared the locked store during the replay."
FALSIFY = "observe: a later note shows the locked store error absent after the backoff"


def corpus(project, name="probe.jsonl", ids=("p1", "p2", "f1", "f2")):
    rows = {"p1": ("PASS", "retry after backoff of two seconds"),
            "p2": ("PASS", "retry after backoff of five seconds"),
            "f1": ("FAIL", "retry immediately in a tight loop"),
            "f2": ("FAIL", "retry immediately without waiting")}
    path = Path(project, name)
    path.write_text("".join(json.dumps({"id": i, "tag": "store-lock", "outcome": rows[i][0],
                                        "action": rows[i][1]}) + "\n" for i in ids), encoding="utf-8")
    return str(path)


def observation(project, eid, text=TEXT, observation=None, scope=None):
    row = next(e for e in events(project) if e["id"] == eid)
    out = {"kind": "observation", "text": text, "sentence_anchors": [[0]],
           "anchors": [{"source_ref": "event:%d" % eid, "quote": text, "hash": row["hash"]}]}
    if observation is not None:
        out["observation"] = observation
    if scope is not None:
        out["scope"] = scope
    return out


def procedural(label="backoff-clears-lock", **extra):
    return dict({"label": label, "governs": ["store-lock"], "prescribes": "retry after backoff"}, **extra)


def declarative(**extra):
    return dict({"falsifiability": FALSIFY}, **extra)


def lessons_log(project):
    with store(project) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='lessons_log'").fetchone():
            return []
        return [dict(r) for r in conn.execute("SELECT * FROM lessons_log ORDER BY seq")]


def facts(project):
    with store(project) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_facts'").fetchone():
            return []
        return [dict(r) for r in conn.execute("SELECT * FROM knowledge_facts ORDER BY claim_id")]


def retained(project):
    with store(project) as conn:
        return lessons.LessonsStore(conn)._retained()


def checked(project, body, session="s-agent"):
    receipt = propose(project, body, session=session)
    assert receipt.status == "checked", receipt.checks
    return receipt.candidate_id


# ------------------------------------------------------------------ the procedural door
def test_a_procedural_observation_needs_a_probe_corpus_without_eval_items(project):
    eid = note(project, TEXT)
    cid = checked(project, observation(project, eid, observation=procedural()))
    none = admit(project, cid)
    assert (none.status, none.reason) == ("refused", "no-probe-corpus")
    empty = Path(project, "empty.jsonl")
    empty.write_text("", encoding="utf-8")
    assert admit(project, cid, probe=str(empty)).reason == "no-probe-corpus"
    assert admit(project, cid, probe=str(Path(project, "missing.jsonl"))).reason == "no-probe-corpus"
    # a question-set item in the corpus: by case id, and by a case field
    held_out = Path(project, "heldout.jsonl")
    held_out.write_text(Path(corpus(project)).read_text(encoding="utf-8") + json.dumps(
        {"id": "C07-001", "tag": "store-lock", "outcome": "PASS", "action": "retry after backoff"}) + "\n",
        encoding="utf-8")
    refused = admit(project, cid, probe=str(held_out))
    assert (refused.status, refused.reason) == ("refused", "probe-has-eval-items")
    assert refused.effects["items"] == ["C07-001"]
    assert row_of(project, cid)["status"] == "checked" and not retained(project)
    assert not [r for r in lessons_log(project) if r["event"] == "lesson-admit"]


def test_the_admitted_lesson_takes_its_description_and_id_from_the_checked_text(project):
    eid = note(project, TEXT)
    cid = checked(project, observation(project, eid, observation=procedural()))
    done = admit(project, cid, probe=corpus(project))
    assert (done.status, done.effects["effect"]) == ("applied", "lesson-admitted")
    (block,) = retained(project)
    assert block["description"] == TEXT == block["value"]
    assert block["id"] == intake.lesson_id_for(cid) == done.effects["ref"]
    assert block["label"] == "backoff-clears-lock" and block["governs"] == ["store-lock"]
    assert block["provenance"].startswith("raw/") and block["provenance"].endswith(".md")
    assert Path(project, ".alpaca", "wiki", block["provenance"]).is_file()
    row = row_of(project, cid)
    assert (row["status"], row["admission_ref"], row["admission_effect"]) == (
        "admitted", block["id"], "lesson-admitted")
    (event,) = wiki_events(project, "wiki-admission")
    assert event["data"]["result"] == "applied" and event["data"]["lesson"]["id"] == block["id"]
    assert event["data"]["knowledge"] is None and event["data"]["edge"] is None
    # a second send of the admission is a duplicate and writes nothing
    assert admit(project, cid, probe=corpus(project)).status == "duplicate"
    assert len(retained(project)) == 1


def test_a_description_field_and_a_label_that_is_not_a_slug_are_refused(project):
    eid = note(project, TEXT)
    with pytest.raises(ValueError, match="observation holds"):
        propose(project, observation(project, eid, observation=procedural(description="my own")))
    bad = propose(project, observation(project, eid, observation=procedural(label="Backoff Clears Lock")))
    assert bad.status == "refused"
    assert {c.check: c.reason for c in bad.checks if not c.passed} == {"K-3": "label-not-slug"}
    assert not wiki_events(project, "wiki-admission")


def test_an_observation_without_a_usable_body_has_no_door(project):
    cases = ((None, "observation-body-missing", "Retry after backoff cleared the locked store at step one."),
             (procedural(prescribes=None), "observation-body-incomplete",
              "Retry after backoff cleared the locked store at step two."),
             (dict(procedural(), falsifiability=FALSIFY), "observation-body-ambiguous",
              "Retry after backoff cleared the locked store at step three."))
    for body, reason, text in cases:
        eid = note(project, text)
        cid = checked(project, observation(project, eid, text, observation=body))
        assert admit(project, cid, probe=corpus(project)).reason == reason
    assert not retained(project) and not facts(project)


def test_a_job_and_the_proposer_cannot_admit_an_observation(project):
    eid = note(project, TEXT)
    cid = checked(project, observation(project, eid, observation=procedural()))
    assert admit(project, cid, session="job:rune2-consolidate", probe=corpus(project)).reason == "job-cannot-admit"
    assert admit(project, cid, session="s-agent", probe=corpus(project)).reason == "self-admission"
    assert not retained(project)


def test_a_probe_that_does_not_discriminate_leaves_only_the_gate_refusal_row(project):
    eid = note(project, TEXT)
    cid = checked(project, observation(project, eid, observation=procedural()))
    before = digest_of(project)
    # every held-out instance is a PASS: a constant lesson cannot clear both halves
    one_sided = corpus(project, "one-sided.jsonl", ids=("p1", "p2"))
    refused = admit(project, cid, probe=one_sided)
    assert (refused.status, refused.reason) == ("refused", "NC-reward")
    assert row_of(project, cid)["status"] == "checked"
    assert digest_of(project) == before
    log = lessons_log(project)
    assert [r["event"] for r in log] == ["lesson-reject"] and not retained(project)
    (event,) = wiki_events(project, "wiki-admission")
    assert event["data"]["result"] == "refused" and event["data"]["reason"] == "NC-reward"


# ------------------------------------------------------------------ the declarative door
def test_a_declarative_observation_lands_as_a_quarantine_prior(project):
    eid = note(project, TEXT)
    cid = checked(project, observation(project, eid, observation=declarative(), scope={"op": "op-8"}))
    done = admit(project, cid)
    assert (done.status, done.effects["effect"]) == ("applied", "prior-admitted")
    (prior,) = facts(project)
    assert prior["claim_id"] == intake.claim_id_for(cid) == done.effects["ref"]
    assert (prior["scope"], prior["claim"], prior["blind_class"]) == ("op-8", TEXT, knowledge.QUARANTINE)
    assert prior["decay_state"] == "LIVE" and prior["stamp"] == knowledge.ASSERTED
    assert not retained(project)
    (event,) = wiki_events(project, "wiki-admission")
    assert event["data"]["knowledge"]["claim_id"] == prior["claim_id"] and event["data"]["lesson"] is None


def test_knowledge_gate_refuses_a_support_count_as_a_verdict(project):
    """FR-16: a support count is never a verdict. The gate refuses the prior set, a callable that
    returns it, a truth test of it, and the count of it, the comparisons made on that count, what is
    worked out from it, and each of these inside a container or returned by a callable. It still passes
    an oracle that is not a count: the gate is not a refusal of every number."""
    for text, ts in ((TEXT, T10), (TEXT_B, T11)):
        eid = note(project, text, ts)
        assert admit(project, checked(project, observation(project, eid, text,
                                                           observation=declarative()))).status == "applied"
    with store(project) as conn:
        hits = knowledge.KnowledgeStore(conn).query(scope="project")
        count = hits.count()
        assert count == 2 and isinstance(count, knowledge.SupportCount) and str(count) == "2"
        with pytest.raises(TypeError, match="prior-derived"):
            knowledge.gate("verdict", hits)
        with pytest.raises(TypeError):
            knowledge.gate("verdict", lambda: hits.count() and hits)
        with pytest.raises(TypeError):
            bool(hits)
        counts = {
            "the count": count,
            "the count, asked": hits.count() > 0,
            "the count, asked of a number": 1 < hits.count(),
            "equal to a number": hits.count() == 2,
            "not equal": hits.count() != 0,
            "at most": hits.count() <= 5,
            "a flag combined": (hits.count() > 0) & True,
            "a flag inverted": ~(hits.count() > 5),
            "sum": hits.count() + 1,
            "sum, from the left": 1 + hits.count(),
            "product": 3 * hits.count(),
            "difference": hits.count() - 1,
            "a ratio": hits.count() / 2,
            "the least of two": min(hits.count(), 9),
            "a sum of counts": sum([hits.count(), hits.count()]),
            "in a list": [hits.count()],
            "in a dict": {"n": hits.count() >= 2},
        }
        for label, value in counts.items():
            with pytest.raises(TypeError, match="prior-derived|oracle"):
                knowledge.gate("verdict", value)
            with pytest.raises(TypeError, match="prior-derived|oracle"):
                knowledge.gate("verdict", lambda value=value: value)
        # the count reads as the number: it prints, formats, serializes and compares in an `assert`
        assert "%d items" % count == "2 items" and f"{count}" == "2" and json.dumps({"n": count}) == '{"n": 2}'
        assert hits.count() > 0 and not hits.count() > 5
        # an oracle that is not a count passes, as before
        assert knowledge.gate("verdict", lambda: True) is True and knowledge.gate("verdict", 0) == 0
        assert knowledge.gate("verdict", {"exit": 0}) == {"exit": 0}


def cited_by_three(project, observation_fields, text=TEXT):
    """An observation cited by three notes that carry the same sentence: a support count of three."""
    ids = [note(project, text, ts) for ts in ("2026-03-01T09:00:00+00:00", T10, T11)]
    rows = {e["id"]: e for e in events(project)}
    body = {"kind": "observation", "text": text, "sentence_anchors": [[0, 1, 2]],
            "anchors": [{"source_ref": "event:%d" % i, "quote": text, "hash": rows[i]["hash"]} for i in ids],
            "observation": observation_fields}
    return checked(project, body)


def test_a_larger_support_count_does_not_stand_in_for_the_probe_or_raise_the_stamp(project):
    """FR-16, fail case 1: an observation that three notes support is admitted by the same gates as one
    that one note supports. A procedural one still needs the probe corpus, and one that a probe refuses
    stays a candidate; a declarative one lands as a QUARANTINE prior stamped ASSERTED, never PROBED."""
    procedural_id = cited_by_three(project, procedural())
    assert row_of(project, procedural_id)["status"] == "checked"
    none = admit(project, procedural_id)
    assert (none.status, none.reason) == ("refused", "no-probe-corpus")
    assert row_of(project, procedural_id)["status"] == "checked" and not retained(project)
    declarative_id = cited_by_three(project, declarative(), text=TEXT_B)
    done = admit(project, declarative_id)
    assert (done.status, done.effects["effect"]) == ("applied", "prior-admitted")
    (prior,) = facts(project)
    assert (prior["stamp"], prior["blind_class"]) == (knowledge.ASSERTED, knowledge.QUARANTINE)
    assert prior["claim_id"] == intake.claim_id_for(declarative_id)


def test_a_prior_whose_falsifiability_is_not_checkable_is_refused(project):
    eid = note(project, TEXT)
    cid = checked(project, observation(project, eid, observation={"falsifiability": "it just seems right"}))
    refused = admit(project, cid)
    assert (refused.status, refused.reason) == ("refused", "NC-falsifiability")
    assert row_of(project, cid)["status"] == "checked" and not facts(project)


# ------------------------------------------------------------------ OB-06
def test_gate_id_collision_never_overwrites(project):
    """OB-06: two observations with one label slug, and two claims equal in their first 44 slug
    characters, all stay retained with their own text; withdrawing one leaves the other live."""
    one, two = note(project, TEXT, T10), note(project, TEXT_B, T11)
    probe = corpus(project)
    l1 = checked(project, observation(project, one, TEXT, observation=procedural()))
    l2 = checked(project, observation(project, two, TEXT_B, observation=procedural()))
    assert admit(project, l1, probe=probe).status == "applied"
    assert admit(project, l2, probe=probe).status == "applied"
    held = retained(project)
    assert sorted(b["value"] for b in held) == sorted([TEXT, TEXT_B])
    assert len({b["id"] for b in held}) == 2 and {b["label"] for b in held} == {"backoff-clears-lock"}
    assert lessons.LessonsStore._mint_id("backoff-clears-lock") not in {b["id"] for b in held}
    # two claims with one 44-character slug
    long_a = "The capture hook records the failed store lock in session notes one."
    long_b = "The capture hook records the failed store lock in session notes two."
    assert knowledge._mint_id(long_a, "project") == knowledge._mint_id(long_b, "project")
    ea, eb = note(project, long_a, T10), note(project, long_b, T11)
    c1 = checked(project, observation(project, ea, long_a, observation=declarative()))
    c2 = checked(project, observation(project, eb, long_b, observation=declarative()))
    assert admit(project, c1).status == "applied" and admit(project, c2).status == "applied"
    assert sorted(f["claim"] for f in facts(project)) == sorted([long_a, long_b])
    assert {f["decay_state"] for f in facts(project)} == {"LIVE"}
    # withdrawing one leaves the other retained and live
    assert withdraw(project, l1).status == "applied" and withdraw(project, c1).status == "applied"
    assert [b["value"] for b in retained(project)] == [TEXT_B]
    state = {f["claim"]: f["decay_state"] for f in facts(project)}
    assert state == {long_a: "DECAYED", long_b: "LIVE"}
    assert row_of(project, l1)["status"] == "retracted" and row_of(project, l2)["status"] == "admitted"


def test_the_gates_refuse_an_id_that_is_already_taken(project):
    """The gate guard under OB-06: a caller-derived id never takes the overwrite path."""
    eid = note(project, TEXT)
    cid = checked(project, observation(project, eid, observation=procedural()))
    admit(project, cid, probe=corpus(project))
    with store(project) as conn:
        again = {"label": "x-y", "description": TEXT_B, "value": TEXT_B, "provenance": "style/banned.txt",
                 "governs": ["store-lock"], "prescribes": "retry after backoff",
                 "lesson_id": intake.lesson_id_for(cid), "roots": [Path(project)]}
        with pytest.raises(lessons.RejectedLesson) as lesson_err:
            lessons.write(conn, again, corpus(project))
        assert lesson_err.value.control == "NC-id-collision"
        store_ = knowledge.KnowledgeStore(conn, roots=[Path(project)])
        store_.propose(TEXT, "project", "style/banned.txt", FALSIFY, "r", claim_id="c_fixed")
        with pytest.raises(knowledge.RejectedWrite) as prior_err:
            store_.propose(TEXT_B, "project", "style/banned.txt", FALSIFY, "r", claim_id="c_fixed")
        assert prior_err.value.control == "NC-id-collision"
        kept = conn.execute("SELECT claim FROM knowledge_facts WHERE claim_id='c_fixed'").fetchall()
        assert [r[0] for r in kept] == [TEXT]


# ------------------------------------------------------------------ OB-15
def hide_private(project, source_ref):
    """A later quarantined capture: the source's document turns private (architecture.md 9.5)."""
    with store(project) as conn:
        conn.execute("UPDATE docs SET private=1 WHERE doc_id IN "
                     "(SELECT doc_id FROM source_versions WHERE source_ref=?)", (source_ref,))


@pytest.mark.parametrize("kind", ["procedural", "declarative", "fact"])
def test_observation_with_nonservable_anchor_is_refused_at_admit(project, kind):
    """OB-15: an anchor that turned non-servable between propose and admit admits nothing, writes no
    lessons_log or knowledge_facts row and leaves accepted_digest as it was."""
    eid = note(project, TEXT)
    if kind == "fact":
        from native_helpers import proposal
        body = proposal(project, eid, "Retry after backoff cleared the locked store",
                        subject="Retry", predicate="status", obj="cleared")
    else:
        body = observation(project, eid, observation=procedural() if kind == "procedural" else declarative())
    cid = checked(project, body)
    before = digest_of(project)
    hide_private(project, "event:%d" % eid)
    refused = admit(project, cid, probe=corpus(project))
    assert (refused.status, refused.reason) == ("refused", "private-anchor")
    assert refused.effects["check"] == "K-4"
    assert row_of(project, cid)["status"] == "checked"
    assert not lessons_log(project) and not facts(project)
    assert digest_of(project) == before
    (event,) = wiki_events(project, "wiki-admission")
    assert event["data"]["result"] == "refused" and event["data"]["reason"] == "private-anchor"


def test_a_quote_that_no_longer_sits_in_its_block_is_refused_at_admit(project):
    eid = note(project, TEXT)
    cid = checked(project, observation(project, eid, observation=declarative()))
    with store(project) as conn:      # the stored quote no longer matches the block it was cut from
        conn.execute("UPDATE candidate_anchors SET quote='a sentence the note never held' "
                     "WHERE candidate_id=? AND role='anchor'", (cid,))
    refused = admit(project, cid)
    assert (refused.status, refused.reason) == ("refused", "quote-not-in-span")
    assert not facts(project)


# ------------------------------------------------------------------ withdrawal and rebuild
def test_withdrawing_an_admitted_observation_withdraws_its_lesson_and_decays_its_prior(project):
    eid, other = note(project, TEXT, T10), note(project, TEXT_B, T11)
    lesson_cid = checked(project, observation(project, eid, observation=procedural()))
    prior_cid = checked(project, observation(project, other, TEXT_B, observation=declarative()),
                        session="s-agent-2")
    admit(project, lesson_cid, probe=corpus(project))
    admit(project, prior_cid)
    assert len(retained(project)) == 1 and facts(project)[0]["decay_state"] == "LIVE"
    out = withdraw(project, lesson_cid)
    assert out.effects["undone"]["derived"] == {"lesson": intake.lesson_id_for(lesson_cid)}
    assert not retained(project)
    withdraw(project, prior_cid, session="s-agent-2")
    assert facts(project)[0]["decay_state"] == "DECAYED"
    with store(project) as conn:
        detail = [r[0] for r in conn.execute("SELECT detail FROM knowledge_log WHERE event='decay'")]
    assert detail == ["candidate-withdrawn -> DECAYED"]


def test_a_rebuild_replays_an_admitted_lesson_and_prior_without_their_probe_or_provenance_file(project):
    eid, other = note(project, TEXT, T10), note(project, TEXT_B, T11)
    probe = corpus(project)
    lesson_cid = checked(project, observation(project, eid, observation=procedural()))
    prior_cid = checked(project, observation(project, other, TEXT_B, observation=declarative()),
                        session="s-agent-2")
    admit(project, lesson_cid, probe=probe)
    admit(project, prior_cid)
    live = (digest_of(project), retained(project), facts(project))
    os.remove(probe)                                    # the probe corpus is gone at rebuild time
    report = rebuild_store(project)
    assert report["status"] == "ok", report
    assert digest_of(project) == live[0]
    assert retained(project) == live[1]
    assert [(f["claim_id"], f["claim"], f["stamp"], f["blind_class"]) for f in facts(project)] == [
        (f["claim_id"], f["claim"], f["stamp"], f["blind_class"]) for f in live[2]]
    assert row_of(project, lesson_cid)["status"] == "admitted"
    assert row_of(project, prior_cid)["admission_effect"] == "prior-admitted"
    # a withdrawal after the admission replays too
    withdraw(project, lesson_cid)
    assert rebuild_store(project)["status"] == "ok"
    assert not retained(project) and row_of(project, lesson_cid)["status"] == "retracted"
    assert [f["decay_state"] for f in facts(project)] == ["LIVE"]


# ------------------------------------------------------------------ K1
def test_both_observation_doors_run_with_sockets_blocked(project):
    from native_helpers import sockets_blocked
    eid, other = note(project, TEXT, T10), note(project, TEXT_B, T11)
    lesson_cid = checked(project, observation(project, eid, observation=procedural()))
    prior_cid = checked(project, observation(project, other, TEXT_B, observation=declarative()),
                        session="s-agent-2")
    before = set(sys.modules)
    with sockets_blocked():
        assert admit(project, lesson_cid, probe=corpus(project)).status == "applied"
        assert admit(project, prior_cid).status == "applied"
    assert not {m.split(".")[0] for m in set(sys.modules) - before} & {
        "requests", "urllib3", "httpx", "openai", "anthropic", "torch", "numpy", "sentence_transformers"}


def test_the_admit_verb_takes_the_probe_corpus_path(project, capsys):
    from alpaca import cli
    eid = note(project, TEXT)
    cid = checked(project, observation(project, eid, observation=procedural()))
    capsys.readouterr()
    assert cli.main(["--session", "s-admin", "wiki", "admit", cid]) == 2          # no corpus: blocked
    assert json.loads(capsys.readouterr().out)["reason"] == "no-probe-corpus"
    assert cli.main(["--session", "s-admin", "wiki", "admit", cid, "--probe", corpus(project)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["status"], out["effects"]["effect"]) == ("applied", "lesson-admitted")
