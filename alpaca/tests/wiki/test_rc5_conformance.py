"""op-008 t-070 RC5: code and its specification drifted with no conformance check (fix plan RC5).

Each finding of the s1 review (`.alpaca/rune2-development/execution/t-070/review-findings.md`) gets
its fix and a test that pins it, and the cause gets small conformance checks that fail when code and
text drift apart again:

* S-1: production code reads nothing of the development-only evaluation tooling: no string in an
  `alpaca/` module (tests aside) names the development tree, and no file under `alpaca/`, `bin/` or
  `setup/` names the evaluation tooling or its question set (the eval isolation test's own check).
* S-3: every adapted third-party row of THIRD-PARTY-NOTICES.md names its copyright line and a
  license text that is shipped and holds that line and the license's permission text.
* A-7: every sentence of the operator docs about re-rendering stored notes names
  `--rerender-notes`; `wiki status` points at the collector when the collector owns capture
  (t-334 deviation 3).
* D-1: a `wiki ingest` page cannot claim an op, session, operator or kind scope by its first lines.
* D-2, D-3: an ISO date-time in a question pins T or K at that instant; "as known at <date>"
  covers the whole day for K and does not pin T at its start.
* D-4: provider states configured, available, unavailable, unsupported; a configured unsupported
  provider never makes `wiki status` raise.
* D-5: `wiki status` lists quarantined captures and unmatched proofs (ARCH 9.3), with every other
  9.3 field.
* D-6: `api.Memory` names the fourteen functions of the interface file, with the service's own
  signatures.
* B-5: the advisory verdict at propose reads the primary anchor's block, as admission does.
"""
import ast
import hashlib
import inspect
import io
import os
import re
import sys
import tokenize
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import (T09, T10, admit, append, at, capture, events, ingest,  # noqa: E402
                            note, proposal, propose, rebuild_store)

from alpaca.wiki import api, intake, service  # noqa: E402
from alpaca.wiki.api import Filters  # noqa: E402
from alpaca.tests.devfiles import require_dev_files  # noqa: E402

REPO = Path(__file__).resolve().parents[3]


# ------------------------------------------------------------------ S-1: no production path into the dev tree
DEV_TREE = "rune2-development"
#: The question set's file name, built from parts so this file does not name it (the eval
#: isolation test greps for it).
QUESTION_SET = "cases" + ".jsonl"
EVAL_TOOLING = DEV_TREE + "/" + "eval"


def _production_modules():
    for path in sorted((REPO / "alpaca").rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        if rel.startswith("alpaca/tests/") or "__pycache__" in rel:
            continue
        yield rel, path


def test_no_production_module_names_the_development_tree_in_code():
    """No string a production module's code holds names `.alpaca/rune2-development/` or the question
    set: no path built from parts, no import through a computed path. Comments that cite evidence are
    not code and are left out here (the next test reads them for the evaluation tooling)."""
    hits = []
    for rel, path in _production_modules():
        source = path.read_text(encoding="utf-8")
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.STRING and (DEV_TREE in tok.string or QUESTION_SET in tok.string):
                hits.append("%s:%d %s" % (rel, tok.start[0], tok.string[:60]))
    assert hits == []


def test_no_shipped_file_names_the_evaluation_tooling():
    """The FR-13/FR-27 isolation check of the evaluation tooling (its
    `test_eval_tooling_stays_out_of_release_and_production`, which greps `alpaca`, `bin` and `setup`
    for the tooling's path and its question set) holds on this tree, tests and comments included."""
    hits = []
    for top in ("alpaca", "bin", "setup"):
        for path in sorted((REPO / top).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if EVAL_TOOLING in text or QUESTION_SET in text:
                hits.append(path.relative_to(REPO).as_posix())
    assert hits == []


def test_the_lesson_probe_guard_keeps_the_id_form_and_the_field_checks():
    """S-1 dropped only the equality with the question set file; a probe corpus item in the case id
    form, or one carrying a `case` or `question` field, is still a held-out evaluation item."""
    corpus = [{"id": "C07-001", "action": "retry"}, {"id": "x1", "case": "C01"},
              {"id": "x2", "question": "what now?"}, {"id": "plain", "action": "retry after backoff"},
              "not an object"]
    assert intake.probe_eval_items(corpus) == ["C07-001", "x1", "x2"]
    assert list(inspect.signature(intake.probe_eval_items).parameters) == ["corpus"]


# ------------------------------------------------------------------ S-3: third-party notices
NOTICES = REPO / "THIRD-PARTY-NOTICES.md"
MIT_PERMISSION = ("Permission is hereby granted, free of charge, to any person obtaining a copy",
                  "The above copyright notice and this permission notice shall be included in all")


def _vendored_rows():
    text = NOTICES.read_text(encoding="utf-8")
    section = text.split("## Vendored or third-party files", 1)[1].split("\n## ", 1)[0]
    rows = []
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 5 and cells[0] not in ("files", "---"):
            rows.append(dict(zip(("files", "what", "upstream", "license", "text"), cells)))
    return text, rows


def _license_text(text, row):
    """The license text a row points at: a file in the tree named in backticks, or the section of
    this file it names ("kept verbatim in this file")."""
    if "in this file" in row["text"]:
        kept = text.split("## License texts kept in this file", 1)[1].split("\n## ", 1)[0]
        return kept
    path = re.search(r"`([^`]+)`", row["text"])
    assert path, row
    return (REPO / path[1]).read_text(encoding="utf-8")


def test_every_adapted_third_party_row_names_its_copyright_line_and_license_text():
    text, rows = _vendored_rows()
    assert rows
    adapted = [r for r in rows if "adapt" in (r["files"] + r["what"]).lower()]
    assert any("resolve.py" in r["files"] for r in adapted)
    for row in rows:
        assert "Copyright" in row["license"], row["files"]
        assert "not in" not in row["license"] and "missing" not in row["license"], row["files"]
    for row in adapted:
        line = re.search(r"Copyright[^|;]*?(?= \(the same notice\)|$)", row["license"])[0].strip()
        kept = _license_text(text, row)
        assert line in kept, (row["files"], line)
        if row["license"].startswith("MIT"):
            for sentence in MIT_PERMISSION:
                assert sentence in kept, (row["files"], sentence)


def test_the_hindsight_row_carries_the_upstream_copyright_and_mit_text():
    text, rows = _vendored_rows()
    row = next(r for r in rows if "resolve.py" in r["files"])
    assert "Copyright (c) 2025 Vectorize AI, Inc." in row["license"]
    kept = _license_text(text, row)
    assert "Copyright (c) 2025 Vectorize AI, Inc." in kept
    upstream = REPO / ".alpaca/research/hindsight-2026-09-28/upstream/LICENSE"
    if upstream.is_file():                      # the read-only research copy, where it is present
        assert upstream.read_text(encoding="utf-8") in kept


# ------------------------------------------------------------------ A-7: docs name --rerender-notes
DOCS = ("docs/operators.md", "docs/observability-operations.md")
RERENDER = re.compile(r"re-?render|renders? (?:it )?again|rendered again", re.IGNORECASE)
RUNBOOK = "## Re-rendering stored notes (owner runbook)"


def _sentences(text):
    """Sentences of a markdown page, headings left out; a code block stays inside the sentence
    that introduces it (so "an owner job:" followed by the command reads as one sentence)."""
    body = "\n".join(l for l in text.splitlines() if not l.startswith("#"))
    flat = re.sub(r"\s+", " ", body)
    return re.split(r"(?<=[.!?])\s+(?=[A-Z`(\"])", flat)


@pytest.mark.parametrize("doc", DOCS)
def test_every_sentence_about_re_rendering_names_rerender_notes(doc):
    """A-7: since the merge's B4 fix only `wiki recover --rerender-notes` re-renders a stored note.
    Outside the owner runbook (whose heading and first sentence name the job), a sentence about
    re-rendering names the flag, unless it is about a store rebuilt from the record."""
    text = (REPO / doc).read_text(encoding="utf-8")
    if RUNBOOK in text:
        before, runbook = text.split(RUNBOOK, 1)
        rest = runbook.split("\n## ", 1)
        assert "--rerender-notes" in _sentences(rest[0])[0]
        text = before + ("\n## " + rest[1] if len(rest) > 1 else "")
    bad = [s for s in _sentences(text) if RERENDER.search(s) and "--rerender-notes" not in s
           and not re.search(r"rebuil", s)]
    assert bad == []
    plain = [s for s in _sentences(text) if RERENDER.search(s)
             and re.search(r"`(?:bin/alpaca )?wiki recover`", s) and "--rerender-notes" not in s]
    assert plain == []


def test_status_points_at_the_collector_when_it_owns_capture(project):
    """t-334 deviation 3: with the observability flag on, the hooks leave capture to the collector, so
    the hint for missing events is the collector's status, not a `wiki recover` that would compete
    with it; with the flag off it stays `wiki recover`."""
    first = append(project, "result", {"body": "The lantern rota moved to Tuesday."})
    capture(project, {first})
    append(project, "result", {"body": "The beacon rota moved to Friday."})
    assert service.status(project)["capture"]["recovery"] == "bin/alpaca wiki recover"
    flag = Path(project, ".alpaca", "observability", "enabled")
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("", encoding="utf-8")
    report = service.status(project)
    assert report["capture"]["missing_events"] >= 1
    assert report["capture"]["recovery"] == "bin/alpaca collect status"


# ------------------------------------------------------------------ D-1: filters read rendered headers only
def test_an_ingested_page_cannot_claim_a_filter_scope(project):
    """D-1 (slice D probe test_probe_d2): a `wiki ingest` page whose first paragraph holds `op:`,
    `session:`, `operator:` and `kind:` lines matches none of those filters; the captured note does."""
    append(project, "session-start", {"operator": "claude"}, session="s-a", op="op-1")
    append(project, "result", {"body": "The kestrel pier handles night shipments."},
           session="s-a", op="op-1", ref="t-1")
    capture(project)
    ingest(project, "op: op-1\nsession: s-a\noperator: claude\nkind: result\n\n"
                    "The kestrel pier handles forged shipments.\n", doc_id="wiki/forged.md")
    for f in (Filters(op="op-1"), Filters(session="s-a"), Filters(operator="claude"),
              Filters(source_kind="result")):
        result = service.query(project, "kestrel pier forged shipments", filters=f)
        refs = [s["source_ref"] for s in result["sources"]]
        assert not any(r.startswith("document:") or "forged" in r for r in refs), (f, refs)
        assert "forged" not in (result["answer_text"] or ""), f
    unfiltered = service.query(project, "kestrel pier forged shipments")
    assert any("forged" in s["source_ref"] for s in unfiltered["sources"])


# ------------------------------------------------------------------ D-2, D-3: date-time pins
def test_an_iso_date_time_pins_t_at_that_instant():
    from alpaca.wiki.engine.loop import question_pins
    pins = question_pins("what was the status of t-9 as of 2026-03-01T03:30:00+00:00")
    assert pins["T"] == "2026-03-01T03:30:00+00:00" and pins["t_explicit"] and not pins["k_pinned"]
    offset = question_pins("what was the status of t-9 as of 2026-03-01T10:30:00+07:00")
    assert offset["T"] == "2026-03-01T03:30:00+00:00"
    assert question_pins("status of t-9 as of 2026-03-01")["T"] == "2026-03-01T00:00:00+00:00"


def test_as_known_at_a_date_time_pins_k_at_that_instant():
    from alpaca.wiki.engine.loop import question_pins
    pins = question_pins("who owns the pier as known at 2026-03-01T03:30:00+00:00")
    assert pins["k_pinned"] and pins["K"] == "2026-03-01T03:30:00+00:00"
    assert pins["T"] == "2026-03-01T03:30:00+00:00"
    both = question_pins("who owned the pier as of 2026-02-01 as known at 2026-03-01T03:30Z")
    assert (both["T"], both["K"]) == ("2026-02-01T00:00:00+00:00", "2026-03-01T03:30:00+00:00")


def test_as_known_at_a_date_covers_the_whole_day_for_k_and_does_not_pin_t_at_its_start():
    from alpaca.wiki.engine.loop import question_pins
    pins = question_pins("who owns the pier as known at 2026-03-01")
    assert pins["K"] == "2026-03-01T23:59:59+00:00"
    assert pins["T"] == "2026-03-01T23:59:59+00:00" and pins["t_explicit"]


QUESTION = "heron gateway retries uploads"
#: The door abstains on question words a note does not hold, "as known at <time>" included
#: (test_outcomes.py), so each fact quotes the time words its questions use, as
#: test_knowledge_time.py's FACT_DATED does.
FACT_DAY = "The heron gateway retries uploads three times, as known at 2026-03-01."
FACT_TIMES = ("The heron gateway retries uploads three times, as known at 2026-03-01T09:30:00+00:00 "
              "and as known at 2026-03-01T10:30:00+00:00.")


def _recorded_at_ten(project, body):
    """A fact whose event is at 2026-03-01T10:00 and which the store recorded then: a rebuild lands
    every event at the time the record gives it (OB-19)."""
    eid = at(project, "result", {"body": body}, T10)
    rebuild_store(project)
    return eid


def test_a_fact_valid_and_recorded_later_on_k_day_is_served_as_known_at_that_day(project):
    """D-3: "as known at 2026-03-01" read T at 00:00 of that day, which hid a fact valid from 10:00 the
    same day at its own K."""
    _recorded_at_ten(project, FACT_DAY)
    result = service.query(project, QUESTION + " as known at 2026-03-01")
    assert (result["outcome"], result["reason"]) == ("grounded", None)
    assert FACT_DAY in result["answer_text"]


def test_as_known_at_a_date_time_before_the_record_hides_the_fact(project):
    """D-2: "as known at 2026-03-01T09:30:00+00:00" pinned no K (a silent latest-knowledge read), so a
    fact recorded at 10:00 was served; pinned now, it is hidden, and at 10:30 it is served."""
    _recorded_at_ten(project, FACT_TIMES)
    early = service.query(project, QUESTION + " as known at 2026-03-01T09:30:00+00:00")
    assert early["currency_stamp"]["txn_axis"] == "2026-03-01T09:30:00+00:00"
    assert early["outcome"] != "grounded" and FACT_TIMES not in (early["answer_text"] or "")
    late = service.query(project, QUESTION + " as known at 2026-03-01T10:30:00+00:00")
    assert late["currency_stamp"]["txn_axis"] == "2026-03-01T10:30:00+00:00"
    assert (late["outcome"], late["reason"]) == ("grounded", None)
    assert FACT_TIMES in late["answer_text"]


# ------------------------------------------------------------------ D-4: provider states
def _vault_settings(project, text):
    path = Path(project, ".alpaca", "wiki", "rune.toml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_status_reports_an_unsupported_provider_and_does_not_raise(project):
    append(project, "result", {"body": "The lantern rota moved to Tuesday."})
    _vault_settings(project, 'embedder = "openai-text-embedding-3"\n')
    report = service.status(project)
    states = report["provider_states"]
    assert states["embedder"]["state"] == "unsupported"
    assert states["embedder"]["configured"] == "openai-text-embedding-3"
    assert states["embedder"]["origin"] == "vault"
    assert all(states[s]["state"] == "available" for s in ("reranker", "entailer", "llm_extractor"))
    assert set(service.PROVIDER_STATES) == {"configured", "available", "unavailable", "unsupported"}
    assert all(row["model"] is False for row in states.values())


def test_status_reports_a_registry_provider_that_cannot_be_built_as_unavailable(project, monkeypatch):
    """A name the closed registry holds whose provider fails to build (stood in for by a raising
    builder: every deterministic provider builds today) is `unavailable` with its error type, and
    the other seams are still reported."""
    from alpaca.wiki.providers import registry

    def broken(cfg):
        raise RuntimeError("entailer tables missing")
    monkeypatch.setattr(registry, "get_entailer", broken)
    states = service.status(project)["provider_states"]
    assert states["entailer"]["state"] == "unavailable"
    assert states["entailer"]["reason"] == "RuntimeError"
    assert states["embedder"]["state"] == states["reranker"]["state"] == "available"


# ------------------------------------------------------------------ D-5: status fields per ARCH 9.3
ARCH = REPO / "design" / "rune2-memory" / "architecture.md"
#: Each item ARCH 9.3 names for `wiki status`, and where the status dict carries it.
ARCH_9_3 = {
    "provider states": ("provider_states",),
    "model_calls: none": ("model_calls",),
    "the switches with their origin": ("switches",),
    "graph and candidate counts": ("corpus", "edges"),
    "candidate counts": ("candidates",),
    "job checkpoints": ("jobs",),
    "failed job items with class, attempts and retryable": ("jobs",),
    "missing event ids": ("capture", "missing_event_ids"),
    "unmatched proofs": ("unmatched_proofs",),
    "quarantined captures": ("quarantined_captures",),
    "withdrawn": ("capture", "withdrawn_events"),
    "schema version": ("schema_version",),
}


def _arch_9_3():
    require_dev_files("design/rune2-memory/architecture.md")
    text = ARCH.read_text(encoding="utf-8")
    return re.sub(r"\s+", " ", text.split("### 9.3 Health and status", 1)[1].split("### 9.4", 1)[0])


def test_status_carries_every_field_arch_9_3_names(project):
    section = _arch_9_3()
    for phrase in ARCH_9_3:
        assert phrase in section, phrase
    append(project, "result", {"body": "The lantern rota moved to Tuesday."})
    capture(project)
    report = service.status(project)
    for phrase, path in ARCH_9_3.items():
        node = report
        for key in path:
            assert isinstance(node, dict) and key in node, (phrase, path)
            node = node[key]
    assert {"nodes", "edges"} <= set(report["corpus"])
    assert {"checkpoint_event_id", "failed"} <= set(report["jobs"]["rune2-consolidate"])


def test_status_names_a_quarantined_capture_and_an_unmatched_proof(project):
    """D-5: after a marking-scanner trip and a proof whose bytes no longer match their seal,
    `wiki status` names both (before, only that one capture's report did)."""
    marked = append(project, "result", {"body": "Handling NOFORN material: the vault door code is amber."},
                    session="s-q", op="op-q")
    text = "# Proof t-1\n\n## At a glance\n\nThe run passed.\n"
    path = Path(project, ".alpaca", "proofs", "op-1", "t-1.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    sealed = append(project, "proof-report", {"path": ".alpaca/proofs/op-1/t-1.md",
                    "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}, ref="t-1")
    good = Path(project, ".alpaca", "proofs", "op-1", "t-2.md")
    good.write_text(text, encoding="utf-8")
    append(project, "proof-report", {"path": ".alpaca/proofs/op-1/t-2.md",
           "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}, ref="t-2")
    capture(project)
    path.write_text(text + "\nEdited after the seal.\n", encoding="utf-8")
    report = service.status(project)
    quarantined = report["quarantined_captures"]
    assert quarantined["count"] == 1
    assert quarantined["documents"][0]["doc_id"] == "wiki/private/raw/op-q/%06d-result.md" % marked
    unmatched = report["unmatched_proofs"]
    assert unmatched == {"count": 1, "proofs": [{"event": sealed, "path": ".alpaca/proofs/op-1/t-1.md",
                                                 "reason": "sha256-mismatch"}]}
    path.unlink()
    assert service.status(project)["unmatched_proofs"]["proofs"][0]["reason"] == "proof-file-missing"


# ------------------------------------------------------------------ D-6: the Memory protocol
INTERFACES = REPO / "design" / "rune2-memory" / "interfaces.py.txt"


def _interface_memory_names():
    require_dev_files("design/rune2-memory/interfaces.py.txt")
    tree = ast.parse(INTERFACES.read_text(encoding="utf-8"))
    memory = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Memory")
    return [f.name for f in memory.body if isinstance(f, ast.FunctionDef)]


def _protocol_names():
    return [n for n, v in vars(api.Memory).items() if callable(v) and not n.startswith("_")]


def test_every_interface_memory_name_is_on_api_memory():
    names = _interface_memory_names()
    assert len(names) == 14
    assert sorted(names) == sorted(_protocol_names())


def test_api_memory_signatures_are_the_service_functions_own():
    for name in _protocol_names():
        proto = inspect.signature(getattr(api.Memory, name))
        params = [(p.name, p.kind, p.default is inspect.Parameter.empty)
                  for p in list(proto.parameters.values())[1:]]          # without self
        real = inspect.signature(getattr(service, name))
        assert params == [(p.name, p.kind, p.default is inspect.Parameter.empty)
                          for p in real.parameters.values()], name


# ------------------------------------------------------------------ B-5: the advisory verdict's block
def test_advisory_verdict_reads_the_primary_anchors_block(project):
    """B-5 (slice B probe test_probe_b5): with a first anchor that does not hold the fact's fields and
    a second, primary one that does, the propose-time verdict equals the admission's."""
    a = note(project, "Task t-9 is blocked on the review.", T09)
    b = note(project, "Task t-9 waits for the owner.", T10)
    one = propose(project, proposal(project, a, "Task t-9 is blocked"))
    assert admit(project, one.candidate_id).status == "applied"
    hb = next(e for e in events(project) if e["id"] == b)["hash"]
    body = proposal(project, a, "Task t-9 is blocked on the review")
    body["anchors"].insert(0, {"source_ref": "event:%d" % b, "quote": "Task t-9 waits", "hash": hb})
    two = propose(project, body)
    got = admit(project, two.candidate_id)
    assert two.verdict == got.effects.get("verdict"), (two.verdict, got.effects)
