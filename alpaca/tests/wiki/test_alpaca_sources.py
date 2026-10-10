"""op-008 t-065 stage s1-capture: Alpaca sources (FR-9; design/rune2-memory/architecture.md 8.3;
alpaca/wiki/ingest/alpaca_sources.py).

A proof report, a task contract and a decision page become Rune2 sources when the drain captures the
event that sealed or wrote them. Each source document carries `source_ref:` and `event:` in its
first paragraph, and `projection_identity` reads `source_ref:` first, so a newer version of the
source retires the older one while the event's own note stays active. A proof whose bytes no longer
hash to the sealed value is a gap, never a source. Real drain, real write path, no mock.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import CONFIG_A, append, capture, events, set_config, store  # noqa: E402

from alpaca.wiki import service                              # noqa: E402
from alpaca.wiki.ingest import alpaca_sources, drain         # noqa: E402

FAILURE = "The ember queue stalled because the cinder lock stayed held overnight."


def _proof(project, text=None, *, task="t-1", name="t-1.md", session="s-src", **extra):
    """Write a proof report and append the `proof-report` event that sealed it."""
    text = text or ("# Proof t-1\n\n## At a glance\n\nThe run failed. %s\n\n## Result\n\n"
                    "Nothing else broke.\n" % FAILURE)
    rel = ".alpaca/proofs/op-1/" + name
    path = Path(project, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    data = {"path": rel, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "bytes": len(text.encode("utf-8")), "sections": ["At a glance"], "evidence": []}
    data.update(extra)
    return append(project, "proof-report", data, session=session, op="op-1", ref=task), rel, path


def _active_docs(project, like):
    with store(project) as conn:
        return [r["doc_id"] for r in conn.execute(
            "SELECT DISTINCT d.doc_id FROM docs d JOIN blocks b ON b.doc_id=d.doc_id "
            "WHERE b.status='active' AND d.doc_id LIKE ? ORDER BY d.doc_id", (like,))]


def _header(project, doc_id):
    text = (Path(project, ".alpaca", "wiki") / doc_id).read_text(encoding="utf-8")
    return dict(line.partition(": ")[::2] for line in text.split("\n\n", 1)[0].splitlines()), text


# ------------------------------------------------------------------ proof sources
def test_proof_report_becomes_a_source_with_anchors(project):
    eid, rel, _ = _proof(project)
    report = drain.capture(str(project), events(project, {eid}))
    assert report["source_gaps"] == []
    (doc,) = _active_docs(project, "raw/sources/proof/%")
    meta, text = _header(project, doc)
    assert meta["source_ref"] == "proof:" + rel and meta["event"] == str(eid)
    assert meta["role"] == "proof" and meta["kind"] == "proof-report"
    assert meta["session"] == "s-src" and meta["task"] == "t-1" and meta["sha256"]
    assert drain.projection_identity(text) == "proof:" + rel
    answer = service.query(project, "ember queue stalled cinder lock held")
    assert answer["outcome"] == "grounded" and "cinder lock" in answer["answer_text"]
    assert [s["source_ref"] for s in answer["sources"]] == ["proof:" + rel]


def test_capturing_a_proof_source_leaves_its_proof_report_event_note_active(project):
    eid, rel, _ = _proof(project)
    drain.capture(str(project), events(project, {eid}))
    notes = _active_docs(project, "raw/op-1/%06d-%%" % eid)
    assert len(notes) == 1
    meta, text = _header(project, notes[0])
    assert drain.projection_identity(text) == "event:%d" % eid and "source_ref" not in meta
    assert len(_active_docs(project, "raw/sources/proof/%")) == 1


def test_proof_whose_bytes_changed_is_a_gap(project):
    eid, rel, path = _proof(project)
    path.write_text(path.read_text(encoding="utf-8") + "\nA line added after sealing.\n",
                    encoding="utf-8")
    report = drain.capture(str(project), events(project, {eid}))
    assert report["source_gaps"] == [{"event": eid, "source_ref": "proof:" + rel,
                                      "reason": "sha256-mismatch"}]
    assert _active_docs(project, "raw/sources/%") == []
    assert len(_active_docs(project, "raw/op-1/%06d-%%" % eid)) == 1    # the event note still lands
    assert service.query(project, "ember queue stalled cinder lock")["answer_text"] is None


def test_proof_gaps_name_their_reason(project):
    missing, rel, path = _proof(project, name="gone.md")
    path.unlink()
    outside = append(project, "proof-report", {"path": "../../elsewhere.md", "sha256": "0" * 64},
                     session="s-src", op="op-1", ref="t-1")
    pathless = append(project, "proof-report", {"sha256": "0" * 64}, session="s-src", ref="t-1")
    report = drain.capture(str(project), events(project, {missing, outside, pathless}))
    assert {(g["event"], g["reason"]) for g in report["source_gaps"]} == {
        (missing, "proof-file-missing"), (outside, "path-outside-root"),
        (pathless, "no-source-ref")}
    assert report["notes"] == 3                           # every event note landed anyway


# ------------------------------------------------------------------ legacy proof paths (W-07)
def _legacy_proof(project, text=None, *, sha=None, name="t-4.md"):
    """A proof event that names `.te/proofs/op-1/<name>`, the folder the project had before the
    rename, with the file only at its `.alpaca/proofs` place (as a rebuild or a clone without the
    old `.te` link holds it)."""
    text = text or "# Proof t-4\n\n## At a glance\n\nThe ember queue stalled; the cinder lock stayed held.\n"
    current = Path(project, ".alpaca", "proofs", "op-1", name)
    current.parent.mkdir(parents=True, exist_ok=True)
    current.write_bytes(text.encode("utf-8"))
    legacy = ".te/proofs/op-1/" + name
    data = {"path": legacy, "sha256": sha or hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "bytes": len(text.encode("utf-8"))}
    return append(project, "proof-report", data, session="s-src", op="op-1", ref="t-4"), legacy


def test_legacy_te_proof_path_is_read_from_the_alpaca_proofs_copy(project):
    eid, legacy = _legacy_proof(project)
    assert not Path(project, ".te").exists()
    report = drain.capture(str(project), events(project, {eid}))
    assert report["source_gaps"] == []
    (doc,) = _active_docs(project, "raw/sources/proof/%")
    meta, text = _header(project, doc)
    assert meta["source_ref"] == "proof:" + legacy and meta["path"] == legacy   # named as recorded
    assert meta["event"] == str(eid) and "cinder lock stayed held" in text
    answer = service.query(project, "ember queue stalled cinder lock held")
    assert answer["outcome"] == "grounded"
    assert [s["source_ref"] for s in answer["sources"]] == ["proof:" + legacy]


def test_legacy_te_proof_path_still_needs_the_sealed_bytes(project):
    eid, legacy = _legacy_proof(project, sha="0" * 64)
    report = drain.capture(str(project), events(project, {eid}))
    assert report["source_gaps"] == [{"event": eid, "source_ref": "proof:" + legacy,
                                      "reason": "sha256-mismatch"}]
    assert _active_docs(project, "raw/sources/%") == []


def test_legacy_te_proof_path_with_no_copy_is_a_missing_file_gap(project):
    eid, legacy = _legacy_proof(project)
    Path(project, ".alpaca", "proofs", "op-1", "t-4.md").unlink()
    report = drain.capture(str(project), events(project, {eid}))
    assert [g["reason"] for g in report["source_gaps"]] == ["proof-file-missing"]


def test_the_legacy_mapping_never_leaves_the_proofs_folder(project):
    sha = hashlib.sha256(b"outside the proofs folder\n").hexdigest()
    Path(project, "secret.md").write_text("outside the proofs folder\n", encoding="utf-8")
    Path(project, ".alpaca").mkdir(exist_ok=True)
    Path(project, ".alpaca", "secret.md").write_text("outside the proofs folder\n", encoding="utf-8")
    climbs = append(project, "proof-report", {"path": ".te/proofs/../../../x.md", "sha256": sha},
                    session="s-src", op="op-1", ref="t-4")
    sibling = append(project, "proof-report", {"path": ".te/other/secret.md", "sha256": sha},
                     session="s-src", op="op-1", ref="t-4")
    up_one = append(project, "proof-report", {"path": ".te/proofs/../secret.md", "sha256": sha},
                    session="s-src", op="op-1", ref="t-4")
    report = drain.capture(str(project), events(project, {climbs, sibling, up_one}))
    # `.te/proofs/..` is not under the legacy folder, so it maps nowhere; `.te/other` neither.
    assert {(g["event"], g["reason"]) for g in report["source_gaps"]} == {
        (climbs, "path-outside-root"), (sibling, "proof-file-missing"), (up_one, "proof-file-missing")}
    assert _active_docs(project, "raw/sources/%") == []


def test_event_whose_note_is_missing_counts_as_missing_even_when_its_proof_source_is_captured(project):
    eid, rel, _ = _proof(project)
    other = append(project, "result", {"body": "a later event"}, session="s-src")
    drain.capture(str(project), events(project, {eid, other}))
    assert service.status(project)["capture"]["missing_events"] == 0
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import Absorber
    (note,) = _active_docs(project, "raw/op-1/%06d-%%" % eid)
    absorber = Absorber(Config.for_project(str(project)))
    try:
        absorber.retire_documents([note], reason="test-note-missing")
    finally:
        absorber.close()
    assert _active_docs(project, "raw/sources/proof/%")      # the proof source is still captured
    capture_report = service.status(project)["capture"]
    assert capture_report["captured_events"] == 1            # only the later event
    assert capture_report["missing_events"] == 1
    assert capture_report["missing_event_ids"] == [eid]


def test_second_proof_event_for_the_same_path_retires_the_earlier_source(project):
    first, rel, path = _proof(project, "# Proof\n\nThe first draft said the ember queue was fine.\n")
    drain.capture(str(project), events(project, {first}))
    second, _, _ = _proof(project, "# Proof\n\nThe second report found the cinder lock stuck.\n")
    drain.capture(str(project), events(project, {second}))
    (doc,) = _active_docs(project, "raw/sources/proof/%")
    assert doc.startswith("raw/sources/proof/%06d-" % second)
    assert len(_active_docs(project, "raw/op-1/%06d-%%" % first)) == 1   # event notes stay


# ------------------------------------------------------------------ contract sources
def _contract(project, done, *, task="t-1", session="s-src"):
    return append(project, "task-contract",
                  {"input": ["the baseline"], "expected": ["an after-run"],
                   "done_bar": [done], "fail_cases": ["better recall admits withdrawn evidence"],
                   "stage": None, "source": "dec-1"},
                  session=session, op="op-1", ref=task)


def test_task_contract_becomes_a_source(project):
    eid = _contract(project, "the pelican bar holds on the fixed set")
    drain.capture(str(project), events(project, {eid}))
    (doc,) = _active_docs(project, "raw/sources/contract/%")
    meta, text = _header(project, doc)
    assert meta["source_ref"] == "contract:t-1" and meta["event"] == str(eid)
    assert "## Done bar\n- the pelican bar holds on the fixed set" in text
    assert "## Fail cases\n- better recall admits withdrawn evidence" in text
    answer = service.query(project, "pelican bar holds fixed set")
    assert answer["outcome"] == "grounded"
    assert [s["source_ref"] for s in answer["sources"]][0] == "contract:t-1"


def test_second_task_contract_event_retires_the_earlier_contract_source(project):
    first = _contract(project, "the first done bar about the walrus")
    second = _contract(project, "the second done bar about the narwhal")
    drain.capture(str(project), events(project, {first}))
    drain.capture(str(project), events(project, {second}))
    (doc,) = _active_docs(project, "raw/sources/contract/%")
    assert doc.startswith("raw/sources/contract/%06d-" % second)
    answer = service.query(project, "done bar about the walrus")
    assert "walrus" not in json.dumps({k: answer[k] for k in ("answer_text", "sources")})


def test_two_contract_events_in_one_batch_leave_one_active_source(project):
    first = _contract(project, "the first done bar about the walrus")
    second = _contract(project, "the second done bar about the narwhal")
    drain.capture(str(project), events(project, {first, second}))
    (doc,) = _active_docs(project, "raw/sources/contract/%")
    assert doc.startswith("raw/sources/contract/%06d-" % second)


def test_an_older_contract_event_captured_late_does_not_replace_a_newer_source(project):
    """A replay that reaches sessions out of order must not let an old contract win."""
    first = _contract(project, "the first done bar about the walrus", session="s-a")
    second = _contract(project, "the second done bar about the narwhal", session="s-b")
    drain.capture(str(project), events(project, {second}))
    report = drain.capture(str(project), events(project, {first}))
    assert len(report["stale_sources"]) == 1
    (doc,) = _active_docs(project, "raw/sources/contract/%")
    assert doc.startswith("raw/sources/contract/%06d-" % second)
    assert len(_active_docs(project, "raw/op-1/%06d-%%" % first)) == 1  # the old event note landed


def test_contracts_of_two_tasks_stay_separate(project):
    one = _contract(project, "the done bar of task one", task="t-1")
    two = _contract(project, "the done bar of task two", task="t-2")
    drain.capture(str(project), events(project, {one, two}))
    assert len(_active_docs(project, "raw/sources/contract/%")) == 2


# ------------------------------------------------------------------ decision sources
def test_decision_page_becomes_a_source(project):
    eid = append(project, "decision",
                 {"id": "dec-abc123def456", "kind": "design", "context": "Which store holds facts?",
                  "options": ["a ledger", "a table"], "choice": "keep the quokka table",
                  "consequence": "Replay rebuilds the quokka table.", "pointer": "",
                  "ts": "2026-09-01T10:00:00+00:00"},
                 session="s-src", op="op-1", ref="dec-abc123def456")
    drain.capture(str(project), events(project, {eid}))
    (doc,) = _active_docs(project, "raw/sources/decision/%")
    meta, text = _header(project, doc)
    assert meta["source_ref"] == "decision:dec-abc123def456" and meta["event"] == str(eid)
    assert "task" not in meta                              # a decision ref is not a task
    assert "## Choice\nkeep the quokka table" in text and "- a ledger" in text
    # The decision page is a retrievable source. Under `graph` the record rules' `choice` edge anchors
    # in the event note, which then answers and is cited, so the page's own citation is read under
    # `narrow`, the profile whose answer is the best-ranked block (the default since the t-068 A3 run).
    set_config(project, CONFIG_A)
    answer = service.query(project, "keep the quokka table")
    assert answer["outcome"] == "grounded"
    assert answer["sources"][0]["source_ref"] == "decision:dec-abc123def456"


# ------------------------------------------------------------------ header identity
def test_projection_identity_checks_source_ref_before_event():
    both = "source_ref: contract:t-9\nrole: contract\nevent: 12\nts: x\n\nbody\n"
    assert drain.projection_identity(both) == "contract:t-9"
    assert drain.projection_identity("role: result\nevent: 12\nts: x\n\nbody\n") == "event:12"
    assert drain.projection_identity("role: transcript\nsession: s1\nop: session\n\nbody\n") \
        == "transcript:s1"
    assert drain.projection_identity("nothing: here\n") is None


def test_event_kinds_that_are_not_sources_render_nothing():
    assert alpaca_sources.render("/nowhere", {"kind": "result", "id": 1, "data": {}}) is None
    assert alpaca_sources.SOURCE_KINDS == {"proof-report", "task-contract", "decision"}


def test_recovery_rebuilds_sources_and_verifies(project):
    from alpaca.wiki import recovery
    eid, rel, _ = _proof(project)
    _contract(project, "the pelican bar holds on the fixed set")
    report = recovery.run(str(project), session="s-src")
    assert report["status"] == "ok", report
    assert _active_docs(project, "raw/sources/proof/%") and _active_docs(project, "raw/sources/contract/%")
    again = recovery.run(str(project), session="s-src")
    assert again["status"] == "ok"
    assert len(_active_docs(project, "raw/sources/contract/%")) == 1
