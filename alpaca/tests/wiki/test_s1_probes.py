"""op-008 t-070 s1 review probes C-1, B-1 and B-3, kept as tests (fix plan RC1 and RC3).

Copied from `.alpaca/proofs/op-008/t-070.logs/s1/sliceC-probes/test_probe_c1_race.py.txt` and
`sliceB-probes/test_probe_b1.py.txt`, `test_probe_b3.py.txt`. The probe bodies are kept; only the
imports (the suite's `project` fixture and helpers instead of the scratch worktree path) and the
closing asserts, which now state the finding's pass condition, differ.

* C-1: a withdrawal that commits between the absorb door's exclusion read and its write transaction
  must leave no active block and no `current` version of the withdrawn event, and the live store
  must equal its rebuild.
* B-1: a fact re-proposed after its admitted edge was retracted gets an active edge or a refusal that
  names the retracted edge, never `applied` with no live edge (t-070 picks the refusal).
* B-3: two admits of one candidate, the second having read the row before the first committed, give
  exactly one `applied` admission.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import (T09, T10, admit, appender, at, capture, claims, digest_of,  # noqa: E402
                            edges, llm_edges, note, proposal, propose, rebuild_store, row_of,
                            store, wiki_events, withdraw)

from alpaca.wiki import service                                    # noqa: E402
from alpaca.wiki.ingest import absorb                              # noqa: E402

BLOCKED = "Task t-9 is blocked on the review."


def test_withdrawal_between_check_and_write(project, monkeypatch):
    """C-1 (slice C probe): the withdrawal fires from inside `absorb_text` after the exclusion read
    (a patched `fw_scan`, which the door calls after `_excluded` and before its transaction)."""
    first = at(project, "result", {"body": "The lantern rota moved to Tuesday."}, T09, op="op-8")
    capture(project, {first})
    gone = at(project, "result", {"body": "The beacon rota moved to Friday."}, T10, op="op-8")
    real = absorb.fw_scan
    fired = {}

    def scan(text):
        if "beacon" in text and not fired:
            fired["x"] = "running"           # the withdrawal captures first, which scans again
            fired["x"] = service.withdraw_source(project, "event:%d" % gone, session="s-owner",
                                                 record=appender(project, "s-owner"), reason="race")
        return real(text)
    monkeypatch.setattr(absorb, "fw_scan", scan)
    capture(project, {gone})
    monkeypatch.setattr(absorb, "fw_scan", real)
    assert fired["x"].status == "applied", fired["x"]
    with store(project) as conn:
        act = conn.execute("SELECT COUNT(*) FROM blocks WHERE status='active' AND doc_id LIKE ?",
                           ("raw/%%/%06d-%%" % gone,)).fetchone()[0]
        sv = [tuple(r) for r in conn.execute(
            "SELECT status, version_seq FROM source_versions WHERE source_ref=?", ("event:%d" % gone,))]
    live = digest_of(project)
    rebuild_store(project)
    assert act == 0, "withdrawn note active after race"
    assert all(status != "current" for status, _seq in sv), sv
    assert live == digest_of(project), "live store differs from its rebuild"


def test_readmit_after_retract(project):
    """B-1 (slice B probe): admit, withdraw (retracts the edge), propose the same fact from the same
    block with another quote (another candidate id, the same content-derived edge id), admit."""
    eid = note(project, BLOCKED)
    one = propose(project, proposal(project, eid, "Task t-9 is blocked"))
    assert admit(project, one.candidate_id).status == "applied"
    (edge,) = llm_edges(project)
    assert withdraw(project, one.candidate_id).status == "applied"
    assert [e["status"] for e in edges(project, edge_id=edge["edge_id"])] == ["retracted"]
    two = propose(project, proposal(project, eid, "Task t-9 is blocked on the review"))
    assert two.status == "checked" and two.candidate_id != one.candidate_id
    r2 = admit(project, two.candidate_id)
    active = [e for e in llm_edges(project) if e["status"] == "active"]
    if r2.status == "applied":
        assert active, "B-1: the admitted fact has no active edge"
    else:
        assert r2.status == "refused" and r2.effects.get("edge_id") == edge["edge_id"], r2
        assert r2.reason == "edge-not-active" and r2.effects.get("edge_status") == "retracted", r2
        assert row_of(project, two.candidate_id)["status"] == "checked"   # a refusal leaves checked
        assert not [c for c in claims(project, edge["edge_id"])
                    if c["claim_ref"] == "candidate:" + two.candidate_id]
    live = digest_of(project)
    rebuild_store(project)
    assert digest_of(project) == live


def test_interleaved_double_admission(project, monkeypatch):
    """B-3 (slice B probe): the second admit (s-admin-2) has read the row as `checked`; the first
    admit runs whole inside its capture step, standing in for another process that wins the store
    lock first."""
    eid = note(project, BLOCKED)
    cid = propose(project, proposal(project, eid, "Task t-9 is blocked")).candidate_id
    real = service._capture_before_admission
    state = {"inner": None}

    def racing(root, cfg):
        if state["inner"] is None:
            state["inner"] = "running"
            state["inner"] = admit(project, cid, session="s-admin-1")
        return real(root, cfg)

    monkeypatch.setattr(service, "_capture_before_admission", racing)
    outer = admit(project, cid, session="s-admin-2")
    inner = state["inner"]
    applied = [e["data"] for e in wiki_events(project, "wiki-admission")
               if e["data"]["result"] == "applied"]
    assert inner.status == "applied", inner
    assert outer.status in ("duplicate", "refused") and outer.status != "applied", outer
    assert len(applied) == 1, "B-3: one candidate admitted twice"
    assert row_of(project, cid)["admission_event_id"] == inner.record_event_id
    live = digest_of(project)
    rebuild_store(project)
    assert digest_of(project) == live
