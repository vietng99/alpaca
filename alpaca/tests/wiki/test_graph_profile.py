"""op-008 t-065 stage s3-graph: the `graph` retrieval profile (design/rune2-memory/architecture.md 8.2;
FR-34; OB-09 graph-empty part).

`graph` is `narrow` plus the PPR arm, with no vector work, and like `full` it lets an edge be the
answer claim (`graph_authoritative`). It is not the default profile: bar A3 of the Measure stage does not hold at the
op-008 t-068 after-run (dec-7574faf2ea3b applies A3 at each after-run; X ties X-lex in every category, so the arm is better in
none), so it ships off and is chosen with meta.retrieval_profile. The arm has a demand gate: below the floor of as-of active edges it is
refused and the trace says `graph-empty`. The fast path of a single-hop lexical question is
unchanged. Real drain, real write path, real door; the one spy only records an argument.
"""
import inspect
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import append, capture, events, ingest, set_config, sockets_blocked, store  # noqa: E402

from alpaca.wiki import service                                  # noqa: E402
from alpaca.wiki.config import META_DEFAULTS, RETRIEVAL_PROFILES, Config  # noqa: E402
from alpaca.wiki.engine import recall_ppr, retrieve              # noqa: E402
from alpaca.wiki.engine.loop import GRAPH_EMPTY, Engine         # noqa: E402
from alpaca.wiki.store.asof import AsOf                          # noqa: E402
from alpaca.wiki.store.db import DB                              # noqa: E402

FACT = "The heron gateway retries uploads twice before it pages the owner."
MULTI = "heron gateway and retries uploads"        # `and` makes it multi-hop: no fast path
SINGLE = "heron gateway retries uploads twice"


def _profile(project, name):
    set_config(project, "wiki_providers:\n  meta:\n    retrieval_profile: %s\n" % name)


def _graph_store(project, tasks=4):
    """Record events whose rules place `tasks` in_op edges and as many status edges."""
    for number in range(1, tasks + 1):
        append(project, "task-add", {"title": "t%d" % number}, op="op-8", ref="t-%d" % number)
        append(project, "task-move", {"from": "open", "to": "doing"}, op="op-8", ref="t-%d" % number)
    ingest(project, FACT + "\n", "wiki/heron.md")
    capture(project)


def _answers(project):
    with store(project) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT channels, retrieval_trace FROM answers ORDER BY created_at, rowid")]


def _edge_count(project, status="active"):
    with store(project) as conn:
        return conn.execute("SELECT COUNT(*) FROM edges WHERE status=?", (status,)).fetchone()[0]


def _gather(project, question, **kw):
    cfg = Config.for_project(str(project))
    db = DB(cfg)
    try:
        return retrieve.gather(db, cfg.providers(), question, AsOf("2030-01-01T00:00:00+00:00"),
                               use_vector=False, all_domains=True, **kw)
    finally:
        db.close()


# ------------------------------------------------------------------ the profile and its default
def test_graph_is_a_profile_and_not_the_default(project):
    # Bar A3 (evaluation.md 5.3) does not hold at the op-008 t-068 after-run: X (graph) ties X-lex (narrow) in every category,
    # so the arm is better in none and ships off. The profile stays available by setting meta.retrieval_profile.
    assert "graph" in RETRIEVAL_PROFILES and META_DEFAULTS["retrieval_profile"] == "narrow"
    assert Config.for_project(str(project)).meta["retrieval_profile"] == "narrow"
    report = service.status(project)
    assert report["switches"]["retrieval_profile"] == {"value": "narrow", "origin": "default"}
    base = Config.for_vault(os.path.join(project, "v"))
    assert base.with_settings({"meta": {"retrieval_profile": "graph"}}).meta["retrieval_profile"] == "graph"
    with pytest.raises(ValueError, match="narrow, graph, hybrid, or full"):
        base.with_settings({"meta": {"retrieval_profile": "bogus"}})


# ------------------------------------------------------------------ the arm runs, no vector work
def test_profile_graph_runs_ppr_and_builds_no_vector_cache(project):
    _profile(project, "graph")
    _graph_store(project)
    answer = service.query(project, MULTI)
    assert answer["outcome"] in ("grounded", "historical", "abstained")
    channels = json.loads(_answers(project)[-1]["channels"])
    assert "ppr" in channels and "vector" not in channels
    with store(project) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not {"vec_blocks_fallback", "vec_nodes_fallback", "vector_cache_inputs"} & tables


def test_profile_narrow_runs_no_ppr_and_graph_is_the_only_difference_in_channels(project):
    _graph_store(project)
    _profile(project, "narrow")
    service.query(project, MULTI)
    assert "ppr" not in json.loads(_answers(project)[-1]["channels"])


def test_profile_graph_makes_no_model_call(project):
    _profile(project, "graph")
    _graph_store(project)
    with sockets_blocked():
        service.query(project, MULTI)
        service.query(project, SINGLE)
        service.context(project, MULTI)


@pytest.mark.parametrize("profile, expected", [("narrow", False), ("graph", True),
                                               ("hybrid", False), ("full", True)])
def test_assemble_receives_graph_authoritative_as_under_full(project, monkeypatch, profile, expected):
    """architecture.md 8.2: `graph` sets graph_authoritative as `full` does; `narrow` and `hybrid` do not."""
    _graph_store(project)
    _profile(project, profile)
    seen = []
    real = Engine._assemble

    def spy(self, *args, **kwargs):
        seen.append(kwargs.get("graph_authoritative"))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Engine, "_assemble", spy)
    service.query(project, MULTI)
    assert seen and set(seen) == {expected}


def _engine_answer(project, question):
    engine = Engine(Config.for_project(str(project)))
    try:
        return engine.answer(question)
    finally:
        engine.close()


def test_an_edge_can_be_the_answer_claim_under_graph(project):
    """Under `graph` the claim is the chosen edge's quote and the answer cites that edge; under
    `narrow` the answer is the block text and cites no edge."""
    _profile(project, "graph")
    _graph_store(project)
    append(project, "decision", {"id": "dec-aaaa000001", "choice": "keep the lexical floor"},
           op="op-8", ref="dec-aaaa000001")
    capture(project)
    graph = _engine_answer(project, "dec-aaaa000001 choice")
    assert graph.verdict == "grounded"
    (citation,) = graph.citations
    assert citation.source_edge_id and "id: dec-aaaa000001" in graph.answer_text
    _profile(project, "narrow")
    narrow = _engine_answer(project, "dec-aaaa000001 choice")
    assert narrow.verdict == "grounded" and narrow.citations[0].source_edge_id is None


def test_an_edge_answer_the_block_does_not_entail_falls_back_to_its_quote(project):
    """`dec-aaaa000001 in_op op-8` is not entailed by the header (`in_op` is not a word of it), so the
    entailment door would have dropped it and the answer would abstain where the block grounds."""
    _profile(project, "graph")
    append(project, "decision", {"id": "dec-aaaa000001", "choice": "keep"}, op="op-8",
           ref="dec-aaaa000001")
    capture(project)
    answer = _engine_answer(project, "dec-aaaa000001 choice")
    assert answer.verdict == "grounded" and "in_op" not in answer.answer_text
    assert answer.answer_text.startswith("op: op-8") or "choice: keep" in answer.answer_text


def test_a_wikilink_edge_keeps_its_triple_answer_under_graph(project):
    _profile(project, "graph")
    ingest(project, "[[Ada]] works at [[Acme]]\n", "wiki/ada.md")
    answer = _engine_answer(project, "Ada works at")
    assert answer.verdict == "grounded" and answer.answer_text == "ada works_at acme"


# ------------------------------------------------------------------ the demand gate
def test_the_demand_gate_is_fed_edges_and_the_parameter_says_so():
    assert list(inspect.signature(recall_ppr.graph_demand_met).parameters)[:1] == ["active_edge_count"]
    floor = recall_ppr.GRAPH_EVENT_FLOOR
    assert not recall_ppr.graph_demand_met(floor - 1) and recall_ppr.graph_demand_met(floor)


def test_graph_empty_on_a_store_with_blocks_and_no_edges(project):
    _profile(project, "graph")
    ingest(project, FACT + "\n", "wiki/heron.md")
    retr = _gather(project, MULTI)
    assert retr.skipped_ppr and retr.graph_empty
    assert "graph-empty" in retr.ladder and "graph-ppr" not in retr.ladder
    assert "ppr" not in retr.channel_lists and retr.hits            # the lexical seed still answers
    answer = service.query(project, MULTI)
    assert GRAPH_EMPTY in answer["trace"]


def test_context_prints_the_graph_empty_note(project):
    _profile(project, "graph")
    ingest(project, FACT + "\n", "wiki/heron.md")
    assert "Trace: graph-empty" in service.context(project, MULTI)
    assert GRAPH_EMPTY in json.loads(_answers(project)[-1]["retrieval_trace"])["notes"]


def test_the_arm_runs_once_the_graph_has_edges(project):
    _profile(project, "graph")
    _graph_store(project)
    assert _edge_count(project) >= recall_ppr.GRAPH_EVENT_FLOOR
    retr = _gather(project, MULTI)
    assert not retr.graph_empty and not retr.skipped_ppr and "graph-ppr" in retr.ladder
    assert GRAPH_EMPTY not in service.query(project, MULTI)["trace"]


def test_only_active_as_of_edges_count_toward_the_demand(project):
    """A retracted edge does not count, and nor does a quarantined one."""
    _graph_store(project, tasks=1)                     # in_op + one status edge: 2 edges, below the floor
    assert _gather(project, MULTI).graph_empty
    _graph_store(project, tasks=2)
    assert not _gather(project, MULTI).graph_empty
    with store(project) as conn:
        conn.execute("UPDATE edges SET status='quarantined' WHERE predicate='status'")
    assert _gather(project, MULTI).graph_empty


def test_the_fast_path_is_unchanged(project):
    """A single-hop lexical question that grounds on the first pass never runs the arm."""
    _profile(project, "graph")
    _graph_store(project)
    answer = service.query(project, SINGLE)
    assert answer["outcome"] == "grounded" and answer["extra"]["fast_path"] is True
    assert "ppr" not in json.loads(_answers(project)[-1]["channels"])
    assert GRAPH_EMPTY not in answer["trace"]


def test_an_exact_floor_hit_survives_the_graph_arm(project):
    """The floor can be reordered by PPR and never evicted (R1)."""
    _graph_store(project)
    retr = _gather(project, "t-3")
    assert retr.linked_nodes == ["t-3"] and retr.floor_ids
    assert set(retr.floor_ids) <= {h.block_id for h in retr.hits}


# ------------------------------------------------------------------ the walk reads only servable sources (OB-07, fix-r1)
def _exclude(project, ref):
    with store(project) as conn:
        conn.execute("INSERT INTO exclusions(source_ref,reason,requested_by,record_event_id,recorded_at) VALUES(?,?,?,?,?)",
                     (ref, "test", "owner", 1, "2026-10-02T00:00:00+00:00"))


def _alice_kestrel(project):
    ingest(project, "[[Kestrel Labs]] builds satellites for the coast.\n", "wiki/pub.md", name="pub.md")
    ingest(project, "[[Alice Wong]] works at [[Kestrel Labs]] on secretproject.\n", "wiki/private/p.md", name="p.md")
    ingest(project, "[[Bob Ray]] works at [[Delta Corp]] on tools.\n", "wiki/b.md", name="b.md")
    ingest(project, "[[Cara Lin]] works at [[Echo Inc]] on tools.\n", "wiki/c.md", name="c.md")


def _answer_and_context(project, question):
    context = service.context(project, question, max_chars=4000)
    return service.query(project, question)["outcome"], context


def test_a_private_pages_edge_does_not_move_the_graph_read(project):
    """The only link from "Alice Wong" to the public Kestrel block is an edge of a private page. Under `narrow` the answer is
    refused and the block is absent; the graph profile must give the same, not the public block through the private relation."""
    _alice_kestrel(project)
    _profile(project, "narrow")
    narrow_outcome, narrow_ctx = _answer_and_context(project, "Alice Wong")
    _profile(project, "graph")
    graph_outcome, graph_ctx = _answer_and_context(project, "Alice Wong")
    assert "satellites" not in narrow_ctx and narrow_outcome == "refused"
    assert "satellites" not in graph_ctx and graph_outcome == narrow_outcome


def test_a_withdrawn_events_edge_does_not_move_the_graph_read(project):
    """P3 of check round 1: the only event naming t-9 is withdrawn; its `proves` edge must not pull the public proof-report note
    for orchid-7.md (which never names t-9) into `context t-9`."""
    from test_record_extract import _at, _move
    for n in range(1, 5):
        _at(project, "task-add", {"title": "x%d" % n}, "2026-03-01T09:0%d:00+00:00" % n, ref="t-%d" % (n + 20))
    _at(project, "proof-report", {"path": "orchid-7.md", "sha256": "ab" * 32}, "2026-03-01T09:30:00+00:00", ref="t-3")
    moved = _move(project, task="t-9", frm="todo", to="doing", ts="2026-03-01T10:00:00+00:00", proof="local:orchid-7.md")
    capture(project)
    _exclude(project, "event:%d" % moved)
    _profile(project, "narrow")
    narrow = service.context(project, "t-9", max_chars=4000)
    _profile(project, "graph")
    assert ("orchid-7" in service.context(project, "t-9", max_chars=4000)) == ("orchid-7" in narrow)


# ------------------------------------------------------------------ the entity floor is capped for a ranked answer (A1, fix-r1)
def _many_notes_one_page(project, moves=8):
    """Task t-5 has `moves` notes that name it; one public page holds the facts the question asks about."""
    for number in range(moves):
        append(project, "task-move", {"from": "open" if number % 2 == 0 else "doing", "to": "doing" if number % 2 == 0 else "open"},
               op="op-8", ref="t-5")
    ingest(project, "The nightly gate ran on commit cafe123 and passed on commit beef456.\n", "wiki/gate.md", name="gate.md")
    capture(project)


def test_the_entity_floor_of_a_ranked_answer_is_capped_and_a_context_pack_keeps_it_whole(project):
    _many_notes_one_page(project)
    question = "t-5 gate check ran on which commit and passed on which commit"
    capped = _gather(project, question)
    whole = _gather(project, question, entity_floor_cap=None)
    assert "t-5" in capped.linked_nodes
    assert len(capped.floor_ids) == retrieve.ENTITY_FLOOR_CAP < len(whole.floor_ids)
    assert set(capped.floor_ids) <= set(whole.floor_ids)


def test_a_block_the_questions_words_reached_outranks_the_cut_floor_members(project):
    from alpaca.wiki.engine import fuse
    _many_notes_one_page(project)
    question = "t-5 gate check ran on which commit and passed on which commit"
    cfg = Config.for_project(str(project))
    db = DB(cfg)
    try:
        retr = retrieve.gather(db, cfg.providers(), question, AsOf("2030-01-01T00:00:00+00:00"),
                               use_vector=False, all_domains=True)
        ranked = fuse.fuse(retr.hits, retr.channel_lists, cfg.providers().reranker, question)
        texts = [h.text for h in ranked[:5]]
        assert any("cafe123" in t for t in texts)
        assert sum(1 for h in ranked[:5] if h.exact_floor) <= retrieve.ENTITY_FLOOR_CAP
    finally:
        db.close()


# ------------------------------------------------------------------ the servable cut of the walk is bounded (L1, fix-r3 B1)
def test_the_walks_servable_cut_runs_over_the_graph_blocks_not_the_whole_store(project, monkeypatch):
    """The walk asks about a block only through an edge source, a node bridge, a seed hit or a candidate, so the servable cut
    covers those blocks. Reading the whole as-of set cost a context call at 2,500 characters 1.6 times B0 (EV 5.4)."""
    _graph_store(project)
    for number in range(30):
        ingest(project, "Filler note %d about the quartz harbor tide table.\n" % number, "wiki/filler%d.md" % number, name="filler%d.md" % number)
    capture(project)
    _profile(project, "graph")
    sizes = []
    real = retrieve.servable

    def spy(db, block_ids, **kw):
        ids = list(block_ids)
        sizes.append(len(ids))
        return real(db, ids, **kw)
    monkeypatch.setattr(retrieve, "servable", spy)
    cfg = Config.for_project(str(project))
    db = DB(cfg)
    try:
        retr = retrieve.gather(db, cfg.providers(), MULTI, AsOf("2030-01-01T00:00:00+00:00"), use_vector=False, all_domains=True)
        total = db.conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
    finally:
        db.close()
    assert "graph-ppr" in retr.ladder
    assert sizes and max(sizes) < total
    assert sizes[0] <= db_graph_blocks(project)


def db_graph_blocks(project):
    with store(project) as conn:
        return len({r[0] for r in conn.execute(
            "SELECT source_block_id FROM edges WHERE obj_datatype='node' AND source_block_id IS NOT NULL "
            "UNION SELECT block_id FROM node_blocks")})
