"""Question coverage must recognize grammar while still requiring the requested evidence."""
from dataclasses import replace

import pytest

from alpaca.wiki.clock import FixedClock
from alpaca.wiki.config import Config
from alpaca.wiki.engine.answer import answer
from alpaca.wiki.engine.oracle import _required_subclaim_entail
from alpaca.wiki.ingest.absorb import Absorber
from alpaca.wiki.providers.deterministic import EnsembleEntailer, StructuralEntailer


SQLITE_REASON = "The project uses SQLite because deployments must run offline."


@pytest.mark.parametrize("profile", ["narrow", "full"])
def test_ordinary_why_question_returns_cited_reason(tmp_path, profile):
    cfg = Config.for_vault(tmp_path)
    cfg = replace(cfg, meta={**cfg.meta, "retrieval_profile": profile})
    absorber = Absorber(cfg, clock=FixedClock(start="2020-01-01T00:00:00+00:00"))
    try:
        absorber.absorb_text("decision.md", SQLITE_REASON)
    finally:
        absorber.close()

    result = answer(cfg, "Why does the project use SQLite?")
    assert result.verdict == "grounded", result
    assert result.completeness.subclaim_coverage
    assert any("offline" in citation.claim_text for citation in result.citations)
    assert all(citation.source_block_id for citation in result.citations)
    assert answer(cfg, "SQLite offline").verdict == "grounded"
    assert answer(cfg, "Why does the project use PostgreSQL?").abstained


@pytest.mark.parametrize("provider", [StructuralEntailer, EnsembleEntailer])
@pytest.mark.parametrize("question, source, supported", [
    ("Why does the project use SQLite?", SQLITE_REASON, True),
    ("Does the project use SQLite?", "The project uses SQLite.", True),
    ("Where does Ada work?", "Ada works at Acme.", True),
    ("What does the project deploy?", "The project deploys SQLite.", True),
    ("What does the project copy?", "The project copies SQLite.", True),
    ("Why does the project use PostgreSQL?", SQLITE_REASON, False),
    ("Does the project use SQL?", "The project uses SQLite.", False),
    ("Does the project use SQLitePlus?", "The project uses SQLite.", False),
    ("Does the project use SQLite?", "The project uses SQLitePlus.", False),
    ("Is the project using SQLitePlus?", "The project is using SQLite.", False),
    ("Does the project use SQLite?", "The project does not use SQLite.", False),
    ("Does the project not use SQLite?", "The project uses SQLite.", False),
    ("Does the project use SQLite?", "The project never uses SQLite.", False),
    ("Does the project use SQLite?", "The project doesn't use SQLite.", False),
    ("Does the project use SQLite 16?", "The project uses SQLite 160.", False),
    ("Does the project uses SQLite 2026?", "The project uses SQLite 20260.", False),
    ("Does the project use SQLite 16?", "The project uses SQLite 16.", True),
    ("Does the project use SQLite 3.14?", "The project uses SQLite 3.14.", True),
    ("Does the project use SQLite 3.14?", "The project uses SQLite 3.15.", False),
    ("Why does the project not use SQLite?",
     "The project does not use SQLite because deployments require replication.", True),
    ("Why does the project use SQLite?", "The project uses SQLite.", False),
    ("Why does the project use SQLite?",
     "The project uses SQLite. Tests run offline because networking is unavailable.", False),
    ("Why does the project use SQLite?",
     "The project uses SQLite because.", False),
    ("Why does the project use SQLite?",
     "The project uses SQLite due to offline deployments.", True),
    ("Why does the project uses SQLite?", "The project uses SQLite.", False),
])
def test_coverage_requires_subject_polarity_numbers_and_reason(provider, question, source, supported):
    label, _ = _required_subclaim_entail(question, source, provider().entail)
    assert (label == "supported") is supported


def test_explicit_provider_contradiction_cannot_be_overridden():
    def contradiction(claim, source):
        return "contradicted", 0.95

    assert _required_subclaim_entail(
        "Where does Ada work?", "Ada works at Acme.", contradiction
    ) == ("contradicted", 0.95)


@pytest.mark.parametrize("provider", [StructuralEntailer, EnsembleEntailer])
@pytest.mark.parametrize("question, source, supported", [
    ("Does the project use SQLite 16?",
     "The project uses SQLite 15 alongside PostgreSQL 16.", False),
    ("Does the project use SQLite 16?",
     "The project uses SQLite alongside PostgreSQL 16.", False),
    ("Does the project use SQLite 15?",
     "The project uses SQLite 15 alongside PostgreSQL 16.", True),
    ("Does the project use SQLite 16?",
     "Database decision. The project uses SQLite 16.", True),
    ("Does the project use SQLite 3.14?",
     "The project uses SQLite 3.15 alongside PostgreSQL 3.14.", False),
    ("Why does the project use SQLite?",
     "The project uses SQLite because deployments cannot rely on networking.", True),
    ("Why does the project use SQLite for offline deployments?",
     "The project uses SQLite for offline deployments because networking cannot be guaranteed.", True),
    ("Why does the project use SQLite?",
     "The project does not use SQLite because deployments cannot rely on networking.", False),
    ("Why does the project not use SQLite?",
     "The project does not use SQLite because deployments cannot rely on networking.", True),
])
def test_numeric_binding_and_causal_premise_polarity(provider, question, source, supported):
    label, _ = _required_subclaim_entail(question, source, provider().entail)
    assert (label == "supported") is supported


@pytest.mark.parametrize("profile", ["narrow", "full"])
@pytest.mark.parametrize("question, source, supported", [
    ("Does the project use SQLite 16?",
     "The project uses SQLite 15 alongside PostgreSQL 16.", False),
    ("Why does the project use SQLite?",
     "The project uses SQLite because deployments cannot rely on networking.", True),
])
def test_answer_door_numeric_binding_and_causal_polarity(tmp_path, profile, question, source, supported):
    cfg = Config.for_vault(tmp_path)
    cfg = replace(cfg, meta={**cfg.meta, "retrieval_profile": profile})
    absorber = Absorber(cfg, clock=FixedClock(start="2020-01-01T00:00:00+00:00"))
    try:
        absorber.absorb_text("decision.md", source)
    finally:
        absorber.close()
    result = answer(cfg, question)
    assert (result.verdict == "grounded") is supported, result
    if supported:
        assert any("networking" in citation.claim_text for citation in result.citations)
    else:
        assert result.abstained
