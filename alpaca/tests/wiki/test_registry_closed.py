"""op-008 t-062: the closed provider registry and bar K1 (design/rune2-memory/architecture.md 3.2,
9.1; interfaces.py.txt section 5; FR-25).

Only the deterministic names resolve; any other name is refused when configured, with a message that
names the capability as unsupported. The provider seams no longer invite a model. Every public
function of alpaca/wiki/service.py, and the export and import of alpaca/wiki/portable.py (t-069,
FR-23; interfaces.py.txt section 5), runs with socket creation blocked (a later verb must join the
walk to pass), and no Hindsight or model module is imported by the default path.
"""
import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import (append, appender, capture, event_hash, events, set_config,  # noqa: E402
                            sockets_blocked)

from alpaca.wiki import portable, service                        # noqa: E402
from alpaca.wiki.api import Filters                              # noqa: E402
from alpaca.wiki.config import Config                            # noqa: E402
from alpaca.wiki.providers import registry                       # noqa: E402

EXPECTED = {
    "embedder": ("deterministic", "deterministic-hash", "hash"),
    "reranker": ("identity", "lexical", "lexical-overlap"),
    "entailer": ("structural", "structural-only", "ensemble", "ensemble-3", "routed", "vn-routed"),
    "llm_extractor": ("off", "none", ""),
}
MODEL_NAMES = ("sentence-transformers", "mxbai-rerank-v2", "hhem", "minicheck", "paladin",
               "openai", "hindsight", "bge-reranker", "cross-encoder")
FORBIDDEN_MODULES = {"hindsight", "hindsight_api", "hindsight_client", "torch", "transformers",
                     "sentence_transformers", "openai", "anthropic", "sqlite_vec"}
WIKI = Path(__file__).resolve().parents[2] / "wiki"


def test_the_registry_is_the_closed_set():
    assert registry.CLOSED_REGISTRY == EXPECTED


@pytest.mark.parametrize("seam", sorted(EXPECTED))
def test_only_the_deterministic_names_resolve(tmp_path, seam):
    base = Config.for_vault(tmp_path / "v")
    for name in EXPECTED[seam]:
        providers = base.with_settings({seam: name}).providers()
        assert getattr(providers, seam) is not None
    for name in MODEL_NAMES:
        with pytest.raises(ValueError) as caught:
            base.with_settings({seam: name}).providers()
        message = str(caught.value)
        assert "unsupported capability" in message and f"unknown {seam}" in message
        assert "register" not in message


def test_a_model_provider_in_project_settings_fails_at_configuration(project):
    set_config(project, "wiki_providers:\n  reranker: mxbai-rerank-v2\n")
    with pytest.raises(ValueError, match="unsupported capability"):
        Config.for_project(project)


def test_provider_seams_no_longer_invite_a_model():
    for rel in ("providers/registry.py", "providers/base.py", "providers/deterministic.py",
                "config.py"):
        text = (WIKI / rel).read_text(encoding="utf-8").lower()
        for word in ("hhem", "minicheck", "paladin", "mxbai", "real cpu", "real embedders",
                     "plugs in here", "register it", "swap for"):
            assert word not in text, (rel, word)


def test_status_reports_provider_states_and_no_model_calls(project):
    set_config(project, "wiki_providers:\n  reranker: lexical\n")
    report = service.status(project)
    assert report["model_calls"] == "none"
    assert report["extractor"] == "off"
    assert set(report["provider_states"]) == set(EXPECTED)
    # FR-25 state names since t-070 D-4: `available` (was `resolved`); see test_rc5_conformance.py
    assert all(s["state"] == "available" and s["model"] is False
               for s in report["provider_states"].values())
    assert report["provider_states"]["reranker"]["configured"] == "lexical"
    assert report["provider_states"]["reranker"]["implementation"] == "LexicalReranker"
    assert report["switches"]["reranker"] == {"value": "lexical", "origin": "project"}
    assert report["switches"]["entailer"]["origin"] == "default"
    assert report["switches"]["consolidate_after_capture"] == {"value": False, "origin": "default"}
    assert report["learning"]["consolidate_after_capture"] is False
    assert report["learning"]["dream_armed"] is False
    assert report["learning"]["automatic_lesson_admission"] is False


# ------------------------------------------------------------------ K1: the socket-blocked walk
def _walk_calls(project):
    """One call per public service function. A verb added later must be added here, or
    test_every_public_service_function_is_walked fails."""
    page = Path(project, "k1.md")
    page.write_text("The kestrel queue drains nightly.\n", encoding="utf-8")
    return {
        "status": lambda: service.status(project),
        "query": lambda: service.query(project, "kestrel queue drains"),
        "context": lambda: service.context(project, "kestrel queue drains"),
        # t-068: the task briefing, built on demand from the store (it writes nothing)
        "briefing": lambda: service.briefing(project, task="t-9"),
        # op-012 t-580: the Studio evidence graph's readers (servable claims, an item's assertions)
        "graph_claims": lambda: service.graph_claims(project),
        "graph_assertions": lambda: service.graph_assertions(project, "t-9"),
        # op-012 t-589: every servable assertion, for the vault columns
        "graph_assertion_list": lambda: service.graph_assertion_list(project),
        "ingest": lambda: service.ingest(project, page, doc_id="wiki/k1.md"),
        "capture": lambda: service.capture(project, sessions=["s-native"], session_events=True,
                                           deadline_s=30),
        "record_facts_preview": lambda: service.record_facts_preview(
            project, from_event=1, through_event=50),
        # t-066: the intake verbs; the proposal cites the first captured event note
        "propose": lambda: service.propose(project, _proposal(project), session="s-native",
                                           record=appender(project, "s-native")),
        "candidates": lambda: service.candidates(project),
        # t-066 s2: admission and withdrawal, by a session other than the proposer
        "admit": lambda: service.admit(project, _candidate_id(project), session="s-admin",
                                       record=appender(project, "s-admin")),
        "withdraw_candidate": lambda: service.withdraw_candidate(
            project, _candidate_id(project), session="s-native", reason="walk",
            record=appender(project, "s-native")),
        # t-067: one bounded consolidation step (it proposes and admits nothing)
        "consolidate": lambda: service.consolidate(project, deadline_s=5,
                                                   record=appender(project, "job:rune2-consolidate")),
        # t-067: source withdrawal and the owner correction
        "withdraw_source": lambda: service.withdraw_source(
            project, "document:wiki/k1.md", session="s-native", reason="walk",
            record=appender(project, "s-native")),
        "correct": lambda: service.correct(
            project, _status_edge(project), "supersede", session="s-owner", by="owner",
            reason="walk", replacement={"obj": "status:blocked", "obj_datatype": "node"},
            record=appender(project, "s-owner")),
        # t-069: the rune2-portable/1 export and import (FR-23); the import pours an empty store, so
        # the walked store is moved aside first
        "export_store": lambda: portable.export_store(project, Path(project, "k1-export.jsonl")),
        "import_store": lambda: _import_into_empty(project),
    }


def _import_into_empty(project):
    from native_helpers import reset_vault
    reset_vault(project, keep_as="k1-walked-vault")
    return portable.import_store(project, Path(project, "k1-export.jsonl"))


def _status_edge(project):
    from native_helpers import store
    with store(project) as conn:
        return conn.execute("SELECT edge_id FROM edges WHERE predicate='status' "
                            "AND status='active'").fetchone()[0]


def _candidate_id(project):
    from alpaca.wiki import intake
    return intake.candidate_id(intake.parse_proposal(_proposal(project)))


def _proposal(project):
    eid = next(e["id"] for e in events(project) if e["kind"] == "result")
    return {"kind": "fact", "anchors": [{"source_ref": "event:%d" % eid, "hash": event_hash(project, eid),
                                         "quote": "The kestrel queue drains nightly at two"}],
            "fact": {"subject": "kestrel queue", "predicate": "related_to", "obj": "nightly",
                     "obj_datatype": "string"}}


def _public_functions():
    """Every public function of service.py, and of portable.py, whose export and import are not
    caller verbs of the Memory interface and join the walk explicitly (t-069)."""
    return sorted(name for module in (service, portable)
                  for name, fn in inspect.getmembers(module, inspect.isfunction)
                  if not name.startswith("_") and fn.__module__ == module.__name__)


def test_every_public_service_function_is_walked(project):
    assert _public_functions() == sorted(_walk_calls(project))


def test_every_public_service_function_runs_with_sockets_blocked(project):
    append(project, "result", {"body": "The kestrel queue drains nightly at two."}, op="op-9")
    append(project, "task-move", {"from": "open", "to": "done", "proof": None, "reason": None},
           op="op-9", ref="t-9")
    capture(project)
    calls = _walk_calls(project)
    before = set(sys.modules)
    walked = {}
    with sockets_blocked():
        for name in ("ingest", "status", "query", "context", "capture", "record_facts_preview",
                     "briefing", "propose", "candidates", "admit", "withdraw_candidate",
                     "consolidate", "withdraw_source", "correct"):
            walked[name] = calls[name]()
        # t-068: a context question that names a task serves its briefing, and the op briefing
        assert "Rune2 briefing task:t-9" in service.context(project, "What is the state of t-9?")
        service.briefing(project, op="op-9", max_chars=16000)
        # t-068 fix-r1: the claimed task's briefing beside a question that names no work
        assert "Rune2 briefing task:t-9" in service.context(project, "kestrel queue drains",
                                                            briefing_task="t-9")
        # t-065: the filtered read and the budgeted context pack run no network call either
        every = Filters(op="op-9", task="t-9", operator="claude", session="s-native",
                        source_kind="result", fact_kind="record-fact", include_bookkeeping=True)
        service.query(project, "kestrel queue drains", filters=every)
        service.context(project, "kestrel queue drains", max_chars=16000, filters=every)
        service.context(project, "kestrel queue drains", max_chars=256)
        # t-069: export the walked store, then pour an empty store from the export and the record
        walked["export_store"] = calls["export_store"]()
        walked["import_store"] = calls["import_store"]()
    new = {m.split(".")[0] for m in set(sys.modules) - before}
    # ... and really exported the store and imported it back, with nothing refused
    assert walked["export_store"]["format"] == "rune2-portable/1"
    assert (walked["import_store"]["status"], walked["import_store"]["refused"]) == ("ok", [])
    # the walk really admitted and withdrew a fact, not only refused it
    assert (walked["admit"].status, walked["withdraw_candidate"].status) == ("applied", "applied")
    # ... and really withdrew a source and corrected a fact
    assert (walked["withdraw_source"].status, walked["correct"].status) == ("applied", "applied")
    # ... and really built a briefing with the task's state in it
    assert [i.section for i in walked["briefing"].items if i.label == "record-fact"]
    assert not (new & FORBIDDEN_MODULES)
    assert not any(m.split(".")[0] in FORBIDDEN_MODULES for m in sys.modules)


def test_the_socket_block_has_teeth():
    import socket
    with sockets_blocked():
        with pytest.raises(AssertionError, match="network"):
            socket.socket()


def test_default_imports_hold_no_optional_or_model_dependency():
    code = ("import sys\n"
            "import alpaca.wiki.service, alpaca.wiki.engine.answer, alpaca.wiki.servable\n"
            "import alpaca.wiki.digest, alpaca.wiki.outcome, alpaca.wiki.api, alpaca.wiki.extract\n"
            "import alpaca.wiki.ingest.absorb, alpaca.wiki.providers.registry\n"
            "print(sorted(m for m in sys.modules if m.split('.')[0] in %r))" % FORBIDDEN_MODULES)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(WIKI.parents[1]), check=True)
    assert out.stdout.strip() == "[]", out.stdout
