"""op-008 t-062: the shared caller contract (FR-1, FR-24; design/rune2-memory/architecture.md 2,
10.3).

One contract, run unchanged on configuration A (profile narrow, identity reranker, structural
entailer) and configuration B (profile hybrid, lexical reranker, ensemble entailer). Only
project.yaml changes between them: the caller code, the questions and the record are the same. Each
configuration captures real record events through the real drain, answers through the real door and
reports through the real status; nothing on those paths is mocked. A store built under A and one
built under B from the same record have the same accepted_digest, as do two builds under one
configuration, and two runs of one query give the same determinism hash. The one mock injects a
fault (a locked store at the door) to check the failure outcome on both configurations.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import (CONFIG_A, CONFIG_B, append, capture, event_hash, ingest,  # noqa: E402
                            reset_vault, set_config, store)

from alpaca.wiki import service                        # noqa: E402
from alpaca.wiki.api import EVAL_CLASS                 # noqa: E402
from alpaca.wiki.digest import accepted_digest         # noqa: E402

CONFIGS = {"A": CONFIG_A, "B": CONFIG_B}
EXPECTED_IMPLEMENTATIONS = {
    "A": {"reranker": "IdentityReranker", "entailer": "StructuralEntailer"},
    "B": {"reranker": "LexicalReranker", "entailer": "EnsembleEntailer"},
}
FACT = "The kestrel queue drains nightly because the batch window closes at two."
QUESTION = "kestrel queue drains nightly batch window"
WIKI_PAGE = "[[Ada]] works at [[Acme]] @2020-01-01\n\nThe osprey index is rebuilt weekly.\n"
QUERY_KEYS = {"question", "outcome", "reason", "verdict", "answer_text", "citations", "sources",
              "coverage", "determinism_hash", "trace", "trust"}
SOURCE_KEYS = {"source_ref", "quote", "source_block_id", "block_content_id", "doc_id", "path",
               "source_time", "operator", "session", "event_hash"}


def _record(project) -> int:
    """The one record both configurations are built from."""
    append(project, "task-claim", {"statement": "drain the kestrel queue"}, op="op-950",
           ref="t-950")
    return append(project, "result", {"body": FACT}, op="op-950", ref="t-950")


def _build(project):
    """Capture every record event and ingest the one wiki page, through the caller-facing paths."""
    summary = capture(project)
    ingest(project, WIKI_PAGE, "wiki/ada.md", "ada.md")
    return summary


@pytest.fixture(params=sorted(CONFIGS))
def configured(project, request):
    set_config(project, CONFIGS[request.param])
    return project, request.param


# ------------------------------------------------------------------ the shared contract
def test_capture_query_context_status_contract(configured):
    project, name = configured
    fact_event = _record(project)
    summary = _build(project)
    assert summary["notes"] == 2 and summary["through_event"] == fact_event

    report = service.status(project)
    assert report["capture"]["missing_events"] == 0
    assert report["model_calls"] == "none"
    assert report["schema_version"] == "4"
    for seam, implementation in EXPECTED_IMPLEMENTATIONS[name].items():
        assert report["provider_states"][seam]["implementation"] == implementation
    assert report["switches"]["retrieval_profile"]["origin"] == "project"

    result = service.query(project, QUESTION)
    assert QUERY_KEYS <= set(result)
    assert (result["outcome"], result["verdict"], result["trust"]) == (
        "grounded", "grounded", "evidence-only")
    assert EVAL_CLASS[result["outcome"]] == "answer"
    assert FACT in result["answer_text"]
    source = result["sources"][0]
    assert SOURCE_KEYS <= set(source)
    assert source["source_ref"] == "event:%d" % fact_event
    assert source["event_hash"] == event_hash(project, fact_event)       # OA-1
    assert source["quote"] and source["quote"] in result["answer_text"]
    assert source["session"] == "s-native"
    assert set(result["coverage"]) == {"scope", "missing_event_ids_in_scope",
                                       "missing_transcripts_in_scope", "project_missing_events",
                                       "project_missing_transcripts"}

    again = service.query(project, QUESTION)
    assert again["determinism_hash"] == result["determinism_hash"]
    assert again["outcome"] == result["outcome"]

    text = service.context(project, QUESTION)
    assert "evidence-only" in text and "Outcome: grounded" in text and "Verdict: grounded" in text
    assert source["doc_id"] in text and FACT in text

    wiki = service.query(project, "osprey index rebuilt weekly")
    assert wiki["outcome"] == "grounded"
    assert wiki["sources"][0]["source_ref"] == "document:wiki/ada.md"
    assert wiki["sources"][0]["event_hash"] is None

    empty = service.query(project, "zebra quantum nebula")
    # narrow retrieves nothing; hybrid's vector arm retrieves a block the door then refuses
    assert EVAL_CLASS[empty["outcome"]] == "abstain" and empty["answer_text"] is None


def test_a_locked_store_fault_is_unavailable(configured, monkeypatch):
    project, _ = configured
    _record(project)
    _build(project)
    from alpaca.wiki.engine import answer as door

    def locked(*args, **kwargs):
        raise RuntimeError("vault rebuild in progress; database access is temporarily locked")

    monkeypatch.setattr(door, "answer", locked)                         # fault injection only
    result = service.query(project, QUESTION)
    assert (result["outcome"], result["reason"]) == ("unavailable", "store-locked")
    assert result["answer_text"] is None and result["verdict"] is None


# ------------------------------------------------------------------ swap: the same record, A and B
def test_accepted_digest_is_equal_across_configurations_and_rebuilds(project):
    _record(project)
    digests, hashes = {}, {}
    for label, config in (("A", CONFIG_A), ("A-rebuild", CONFIG_A), ("B", CONFIG_B)):
        set_config(project, config)
        _build(project)
        with store(project) as conn:
            digests[label] = accepted_digest(conn)
            edges = conn.execute("SELECT COUNT(*) FROM edges WHERE status='active'").fetchone()[0]
        assert edges >= 1, "the digest must cover at least one accepted edge"
        first = service.query(project, QUESTION)["determinism_hash"]
        assert service.query(project, QUESTION)["determinism_hash"] == first
        hashes[label] = first
        reset_vault(project)
    assert digests["A"] == digests["A-rebuild"] == digests["B"]
    assert hashes["A"] == hashes["A-rebuild"]


def test_accepted_digest_moves_with_accepted_state(project):
    _record(project)
    set_config(project, CONFIG_A)
    _build(project)
    with store(project) as conn:
        before = accepted_digest(conn)
    ingest(project, "[[Bob]] works at [[Hooli]] @2021-01-01\n", "wiki/bob.md", "bob.md")
    with store(project) as conn:
        after = accepted_digest(conn)
        conn.execute("INSERT INTO exclusions(source_ref,reason,requested_by,record_event_id,"
                     "recorded_at) VALUES('event:1','test','owner',1,'2026-01-01')")
    with store(project) as conn:
        excluded = accepted_digest(conn)
    assert len({before, after, excluded}) == 3


def _fixed_clock(monkeypatch, stamp: str) -> None:
    """Pin the Writer clock (alpaca/wiki/clock.py now_iso) to one instant for a whole build."""
    from datetime import datetime

    from alpaca.wiki import clock as clock_module

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(stamp).astimezone(tz)

    monkeypatch.setattr(clock_module, "datetime", _Frozen)


UNDATED_PAGES = (("[[Ada]] works at [[Acme]]\n", "wiki/ada.md", "ada.md"),
                 ("[[Bob]] works at [[Initech]]\n", "wiki/bob.md", "bob.md"),
                 ("[[Bob]] works at [[Hooli]] @2021-01-01\n", "wiki/bob-2.md", "bob-2.md"))


def _undated_build(project, monkeypatch, stamp: str, pages=UNDATED_PAGES):
    _fixed_clock(monkeypatch, stamp)
    for text, doc_id, name in pages:
        ingest(project, text, doc_id, name)
    with store(project) as conn:
        digest = accepted_digest(conn)
        times = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
            "SELECT obj_node, valid_from, recorded_at, valid_until FROM edges")}
    reset_vault(project)
    return digest, times


def test_accepted_digest_ignores_the_capture_clock_on_undated_facts(project, monkeypatch):
    """check-invariants-r4 B1: an undated fact takes the Writer clock as its valid_from (the
    capture-time proxy of architecture.md 5.1), and a dated fact recorded before a later proxy
    fact takes that proxy as its valid_until (ingest/reconcile.py). Two builds of the same sources
    under two different clocks still have one accepted_digest; a change of a real date moves it, and
    so does reopening a window that ended at an unknown time."""
    set_config(project, CONFIG_A)
    first, t1 = _undated_build(project, monkeypatch, "2026-03-01T10:00:00+00:00")
    second, t2 = _undated_build(project, monkeypatch, "2026-03-02T11:22:33+00:00")
    # setup: the clock really reached the stored times, so the two stores differ row by row
    assert t1["acme"][0] == t1["acme"][1] == "2026-03-01T10:00:00+00:00"
    assert t2["acme"][0] == t2["acme"][1] == "2026-03-02T11:22:33+00:00"
    assert t1["hooli"][0] == t2["hooli"][0] == "2021-01-01T00:00:00+00:00"
    assert t1["hooli"][2] == t1["initech"][0] and t2["hooli"][2] == t2["initech"][0]
    assert first == second
    # In place on one store: a real date still counts, and a window ended at an unknown time is
    # not an open window.
    _fixed_clock(monkeypatch, "2026-03-01T10:00:00+00:00")
    for text, doc_id, name in UNDATED_PAGES:
        ingest(project, text, doc_id, name)
    with store(project) as conn:
        assert accepted_digest(conn) == first
        conn.execute("UPDATE edges SET valid_from='2019-01-01T00:00:00+00:00' "
                     "WHERE obj_node='hooli'")
    with store(project) as conn:
        redated = accepted_digest(conn)
        conn.execute("UPDATE edges SET valid_until=NULL WHERE obj_node='hooli'")
    with store(project) as conn:
        reopened = accepted_digest(conn)
    assert len({first, redated, reopened}) == 3


def test_answer_outcomes_and_answers_stay_outside_the_digest(project):
    _record(project)
    set_config(project, CONFIG_A)
    _build(project)
    with store(project) as conn:
        before = accepted_digest(conn)
    service.query(project, QUESTION)
    service.query(project, QUESTION, knowledge_as_of="2030-01-01T00:00:00+00:00")
    with store(project) as conn:
        assert conn.execute("SELECT COUNT(*) FROM answer_outcomes").fetchone()[0] == 2
        assert accepted_digest(conn) == before


def test_the_caller_interface_is_the_same_module_for_both_configurations():
    """The swap happens behind the functions: callers import one module whatever the config."""
    names = {"status", "query", "context", "ingest"}
    assert names <= set(dir(service))
    assert Path(service.__file__).name == "service.py"


def test_cli_prints_the_outcome(configured, capsys):
    import json
    from alpaca import cli
    project, _ = configured
    _record(project)
    _build(project)
    assert cli.main(["wiki", "query", QUESTION]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["outcome"] == "grounded" and printed["reason"] is None
    assert cli.main(["wiki", "query", QUESTION, "--knowledge-as-of", "2030-01-01"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert (printed["outcome"], printed["reason"]) == ("grounded", None)
    assert printed["currency_stamp"]["txn_axis"].startswith("2030-01-01")
    assert cli.main(["wiki", "context", QUESTION]) == 0
    assert "Outcome: grounded" in capsys.readouterr().out
