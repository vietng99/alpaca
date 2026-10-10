"""op-008 t-066: the name-alike guard (FR-15, HS-EXT-07 adapted, dec-804f2708474c Q-F).

Negative controls on name-alike pairs: entities never merge because their
names resemble each other; an exact identifier only equals itself; an identity that resolves to more
than one node abstains; an invented detail or a detail from another source is refused. Real store,
real intake, no mock and no model.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import appender, fact_event, fact_proposal, ingest, store  # noqa: E402

from alpaca.wiki import service                                   # noqa: E402
from alpaca.wiki.config import Config                              # noqa: E402
from alpaca.wiki.ingest import resolve                             # noqa: E402
from alpaca.wiki.store.ledger import Ledger                           # noqa: E402
from alpaca.wiki.store.db import DB                                # noqa: E402
from alpaca.wiki.store.write import Writer                         # noqa: E402

# synthetic name-alike pairs: one shares a first name, one a leading noun, one a trailing noun
PAIRS = [("Orla Finch", "Orla Marsh"), ("Signal relay daemon", "Signal mast heater"),
         ("Heron dock", "Puffin dock")]


def propose(project, body, session="s-agent"):
    return service.propose(project, body, session=session, record=appender(project, session))


def failed(receipt):
    return {c.check: c.reason for c in receipt.checks if not c.passed}


@pytest.mark.parametrize("a,b", PAIRS)
def test_name_alike_pairs_are_distinct_in_both_orders(a, b):
    assert resolve.name_verdict(a, b) == "distinct" == resolve.name_verdict(b, a)
    assert resolve.trigram_similarity(a, b) < resolve.MIN_NAME_SIMILARITY


def test_the_token_guard_alone_keeps_a_shared_long_word_from_hiding_a_different_short_one():
    # "John Smith" / "Jane Smith" is 0.47 by trigram: below the cutoff, so the word rule is judged
    # by itself, as in the upstream calibration
    assert not resolve._tokens_are_compatible("John Smith", "Jane Smith")
    assert not resolve._tokens_are_compatible("Orla Finch", "Orla Marsh")
    assert resolve._tokens_are_compatible("Harbor ledger", "Harbour ledger")
    assert resolve._tokens_are_compatible("Nick", "Nicolas")        # single words are exempt


def test_a_high_trigram_score_with_a_different_word_is_still_distinct():
    a, b = "Acme Corporation Annex North", "Acme Corporation Annex Harbor"
    assert resolve.trigram_similarity(a, b) >= resolve.MIN_NAME_SIMILARITY
    assert resolve.name_verdict(a, b) == "distinct"


def test_numbered_names_and_exact_identifiers_never_merge():
    assert resolve.name_verdict("Room 101", "Room 102") == "distinct"
    assert resolve.name_verdict("TR-9", "TR-10") == "distinct"
    assert resolve.name_verdict("t-066", "t-0666") == "distinct"
    assert resolve.name_verdict("3816", "3861") == "distinct"
    assert resolve.name_verdict("UA123", "UA124") == "distinct"
    assert resolve.name_verdict("TR-9", "tr 9") == "same"
    assert resolve.is_identifier("TR-9") and not resolve.is_identifier("Room 101")
    # an identifier is never "review", even against a close spelling of itself
    assert resolve.name_verdict("TR-9", "TR9") == "distinct"


def test_a_spelling_variant_is_only_surfaced_for_review():
    assert resolve.name_verdict("Harbor ledger", "Harbour ledger") == "review"
    assert resolve.name_verdict("Harbor ledger", "harbor  Ledger") == "same"


def _nodes(project, names):
    """Mint a node per name through ensure_node over one real ingested block."""
    ingest(project, "# Kiln rota\n\nOrla Finch runs the west kiln shift.\n", "wiki/guards.md")
    with store(project) as conn:
        block = conn.execute("SELECT block_id FROM blocks ORDER BY block_id LIMIT 1").fetchone()[0]
    cfg = Config.for_project(str(project))
    db = DB(cfg)
    try:
        db.pour()
        writer = Writer(db, Ledger(cfg.ledger_path, vault_dir=cfg.vault_dir))
        with writer.transaction():
            made = [resolve.ensure_node(db, writer, slug, name, block) for slug, name in names]
    finally:
        db.close()
    return made


def _candidates(project):
    with store(project) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM merge_candidate ORDER BY cand_id")]


def test_name_alike_nodes_stay_two_nodes_with_no_candidate(project):
    names = [("orla-finch", "Orla Finch"), ("orla-marsh", "Orla Marsh"),
             ("signal-relay", "Signal relay daemon"), ("signal-mast", "Signal mast heater"),
             ("heron-dock", "Heron dock"), ("puffin-dock", "Puffin dock"),
             ("tr-9", "TR-9"), ("tr-10", "TR-10")]
    assert _nodes(project, names) == [s for s, _ in names]
    assert _candidates(project) == []
    with store(project) as conn:
        assert conn.execute("SELECT COUNT(*) FROM nodes WHERE canonical_node_id IS NOT NULL").fetchone()[0] == 0


def test_a_close_spelling_is_surfaced_and_never_bound(project):
    _nodes(project, [("harbor-ledger", "Harbor ledger"), ("harbour-ledger", "Harbour ledger")])
    (cand,) = _candidates(project)
    assert (cand["node_a"], cand["node_b"]) == ("harbor-ledger", "harbour-ledger")
    assert cand["method"] == "name-guard" and cand["status"] == "pending" and 0.5 <= cand["score"] < 1
    with store(project) as conn:
        assert conn.execute("SELECT COUNT(*) FROM nodes WHERE canonical_node_id IS NOT NULL").fetchone()[0] == 0


def _two_orlas(project, quote_text):
    eid = fact_event(project, quote_text)
    with store(project) as conn:
        for node in ("orla-a", "orla-b"):
            conn.execute("INSERT INTO nodes(node_id,display_name,valid_from,recorded_at,status) "
                         "VALUES(?,?,?,?,'active')", (node, "Orla", "2026", "2026"))
            conn.execute("INSERT INTO aliases(node_id,surface,norm_surface,kind,status) "
                         "VALUES(?,?,?,?,'bound')", (node, "Orla", "orla", "exact"))
    return eid


def test_an_alias_collision_abstains(project):
    eid = _two_orlas(project, "Orla reviewed the capture of session c29 on 2026-10-02.")
    receipt = propose(project, fact_proposal(project, eid, quote="Orla reviewed the capture of session c29",
                                             subject="Orla", obj="c29", predicate="failed_with"))
    assert receipt.status == "refused" and failed(receipt) == {"K-3": "ambiguous-entity"}
    with store(project) as conn:
        assert conn.execute("SELECT COUNT(*) FROM edges WHERE extractor='llm'").fetchone()[0] == 0


def test_an_invented_name_alike_detail_is_refused(project):
    eid = fact_event(project, "Orla Finch runs the west kiln shift since 2026-10-02.")
    ok = propose(project, fact_proposal(project, eid, quote="Orla Finch runs the west kiln shift",
                                        subject="t-066", obj="blocked"))
    assert failed(ok) == {"K-3": "field-not-in-span"}               # subject not in the span
    other = propose(project, fact_proposal(project, eid, quote="Orla Finch runs the west kiln shift",
                                           subject="Orla Marsh", obj="blocked"))
    assert failed(other) == {"K-3": "field-not-in-span"}            # the name-alike is not the cited one


def test_a_value_from_the_name_alike_source_is_refused(project):
    heron = fact_event(project, "The Heron dock south hatch code is 3816.", ref="t-1")
    puffin = fact_event(project, "The Puffin dock south hatch code is 7052.", ref="t-2")
    wrong = propose(project, fact_proposal(project, heron, quote="The Heron dock south hatch code is 3816",
                                           subject="Heron dock", obj="7052", predicate="choice"))
    assert wrong.status == "refused" and failed(wrong) == {"K-3": "field-not-in-span"}
    right = propose(project, fact_proposal(project, puffin, quote="The Puffin dock south hatch code is 7052",
                                           subject="Puffin dock", obj="7052", predicate="choice"))
    assert right.status == "checked" and not failed(right)
