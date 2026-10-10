"""Question coverage must recognize grammar while still requiring the requested evidence."""
from dataclasses import replace

import pytest

from alpaca.wiki.clock import FixedClock
from alpaca.wiki.config import Config
from alpaca.wiki.engine.answer import answer
from alpaca.wiki.engine.oracle import _required_subclaim_entail, evaluate
from alpaca.wiki.ingest.absorb import Absorber
from alpaca.wiki.providers.deterministic import EnsembleEntailer, StructuralEntailer
from alpaca.wiki.store.asof import AsOf
from alpaca.wiki.store.db import DB


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


# An absent-target question (the shape of the C04 control items, op-008 t-067; synthetic words): the
# claim cut from the top block (its first 300 characters) holds no negation word and the rest of the
# block does, so the structural entailer judges the claim `contradicted` by its own block; no block
# names "supercomputer", so the question is uncovered.
UNCOVERED_BLOCK = (
    "The asset catalog moved from the old file server to the new file server during the autumn "
    "cleanup, and the earlier attempt to move the asset catalog kept every folder, every thumbnail "
    "and the checksum list over each image. The archivists copied the files, checked the checksums twice "
    "and switched the viewers over at the end of the month. It took one afternoon and no viewer lost "
    "a picture during the switch."
)
UNCOVERED_QUESTION = "Did an earlier attempt to move the asset catalog onto a supercomputer fail?"


@pytest.mark.parametrize("profile", ["narrow", "graph"])
def test_uncovered_question_with_a_contradicted_claim_abstains(tmp_path, profile):
    cfg = Config.for_vault(tmp_path)
    cfg = replace(cfg, meta={**cfg.meta, "retrieval_profile": profile})
    absorber = Absorber(cfg, clock=FixedClock(start="2020-01-01T00:00:00+00:00"))
    try:
        absorber.absorb_text("catalog-move.md", UNCOVERED_BLOCK)
    finally:
        absorber.close()

    result = answer(cfg, UNCOVERED_QUESTION)
    assert [c.verdict for c in result.citations] == ["contradicted"], "setup: the cited claim is contradicted"
    assert not result.completeness.subclaim_coverage
    assert result.verdict == "abstained", result
    assert result.answer_text is None
    assert result.extra["reason"] == "unmet predicate: SUBCLAIM_COVERAGE"


class _ContradictsEverything:
    name = "contradicts-everything"

    def entail(self, claim, source):
        return "contradicted", 0.9


def test_contradicted_claim_is_a_conflict_only_when_the_cited_block_speaks_to_the_question(tmp_path):
    claim = "An earlier attempt to move the asset catalog to the new server did fail."
    block = claim + " The old server had no spare disk."
    db = DB(Config.for_vault(tmp_path))
    db.pour()

    def roll_up(required_subclaims, entailer=StructuralEntailer()):
        result = evaluate(db, entailer, AsOf("2030-01-01T00:00:00+00:00"),
                          ["SUBCLAIM_COVERAGE"], [{"text": claim, "source_block_id": "b"}],
                          {"b": block}, [], set(), [], [], ["b"],
                          required_subclaims=required_subclaims)
        assert [c.verdict for c in result.citations] == ["contradicted"]
        return result.verdict, result.reason

    try:
        covered = roll_up(["Did an earlier attempt to move the asset catalog fail"])
        uncovered = roll_up(["Did an earlier attempt to move the asset catalog onto a supercomputer fail"])
        without_plan = roll_up(None)
        sub_claim_contradicted = roll_up(["asset catalog supercomputer"], _ContradictsEverything())
    finally:
        db.close()
    assert covered == ("conflict", "contradiction/overturner")
    assert without_plan == ("conflict", "contradiction/overturner")
    assert sub_claim_contradicted == ("conflict", "contradiction/overturner")
    assert uncovered == ("insufficient", "unmet predicate: SUBCLAIM_COVERAGE")
