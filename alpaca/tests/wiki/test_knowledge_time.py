"""op-008 t-065 stage s2-time: knowledge-time reads (design/rune2-memory/architecture.md 5.2; FR-5;
OB-09, OB-20).

A K-pinned read (the `knowledge_as_of` argument or "as known at <date>" in the question) is served,
not refused: the lexical arm, the exact-match floor, the block rows and the ambiguity gate read the
as-of clause, so a block retired after K is visible at K. A withdrawn source never is (the servable
predicate). The vector arm keeps its active-only cache, so a K-pinned read under `hybrid` skips it and
says so in the trace (`vector-arm-skipped-knowledge-time`), which `context` prints (OB-09). A block
retired before this change carries `valid_until = superseded_at`; `wiki recover` clears that once
(OB-20). Real absorber, real door, no mock.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import CONFIG_B, append, capture, set_config, store  # noqa: E402

from alpaca.wiki import service                         # noqa: E402
from alpaca.wiki.clock import FixedClock                # noqa: E402
from alpaca.wiki.config import Config                   # noqa: E402
from alpaca.wiki.ingest.absorb import Absorber          # noqa: E402

FACT = "The heron gateway retries uploads three times before failing."
FACT_DATED = "The heron gateway retries uploads three times, as known at 2030-01-01."
QUESTION = "heron gateway retries uploads"
JAN = "2026-01-01T00:00:00+00:00"
FEB = "2026-02-01T00:00:00+00:00"
MID = "2026-01-15T00:00:00+00:00"
SKIPPED = "vector-arm-skipped-knowledge-time"


def _absorb(project, docs, start):
    """Absorb (doc_id, text) wiki documents with a fixed clock: the recorded time of the blocks."""
    cfg = Config.for_project(project)
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    absorber = Absorber(cfg, clock=FixedClock(start=start, step=0))
    try:
        for doc_id, text in docs:
            absorber.absorb_text(doc_id, text, kind="wiki", path=doc_id)
    finally:
        absorber.close()


def _retire(project, doc_ids, at):
    absorber = Absorber(Config.for_project(project), clock=FixedClock(start=at, step=0))
    try:
        absorber.retire_documents(doc_ids, reason="test-retire")
    finally:
        absorber.close()


def _block(project, doc_id):
    with store(project) as conn:
        return dict(conn.execute("SELECT * FROM blocks WHERE doc_id=?", (doc_id,)).fetchone())


# ------------------------------------------------------------------ the as-of clause on every arm
def test_k_pinned_read_is_served_not_refused(project):
    _absorb(project, [("wiki/h.md", FACT + "\n")], JAN)
    result = service.query(project, QUESTION, knowledge_as_of=MID)
    assert (result["outcome"], result["reason"]) == ("grounded", None)
    assert FACT in result["answer_text"]
    assert result["currency_stamp"]["txn_axis"].startswith("2026-01-15")


def test_knowledge_pin_in_the_question_text_is_served_too(project):
    """"as known at <date>" pins K and, as plan.classify reads it, T at the same date: an event note
    carries its source time, so the read is not valid-time-unknown."""
    append(project, "result", {"body": FACT_DATED}, session="s-text")
    capture(project)
    result = service.query(project, QUESTION + " as known at 2030-01-01")
    assert (result["outcome"], result["reason"]) == ("grounded", None)
    assert FACT_DATED in result["answer_text"]
    assert result["currency_stamp"]["txn_axis"].startswith("2030-01-01")


def test_retired_block_visible_at_k_on_lexical_and_floor_arms(project):
    from alpaca.wiki.engine import retrieve
    from alpaca.wiki.store.asof import AsOf
    from alpaca.wiki.store.db import DB
    _absorb(project, [("wiki/h.md", FACT + "\n")], JAN)
    _retire(project, ["wiki/h.md"], FEB)
    block = _block(project, "wiki/h.md")
    assert block["status"] == "superseded" and block["superseded_at"] == FEB
    # the retirement no longer writes valid_until (OB-20): the validity window stays open
    assert block["valid_until"] is None
    cfg = Config.for_project(project)
    db = DB(cfg)
    try:
        providers = cfg.providers()
        now = "2026-06-01T00:00:00+00:00"
        at_k = retrieve.gather(db, providers, FACT, AsOf(now, K=MID), skip_ppr=True,
                               use_vector=False)
        assert block["block_id"] in at_k.channel_lists["bm25"]
        assert block["block_id"] in at_k.floor_ids
        latest = retrieve.gather(db, providers, FACT, AsOf(now), skip_ppr=True, use_vector=False)
        assert block["block_id"] not in latest.channel_lists["bm25"]
        assert block["block_id"] not in latest.floor_ids
        before_record = retrieve.gather(db, providers, FACT, AsOf(now, K="2025-12-01T00:00:00+00:00"),
                                        skip_ppr=True, use_vector=False)
        assert block["block_id"] not in before_record.channel_lists["bm25"]
    finally:
        db.close()
    served = service.query(project, QUESTION, knowledge_as_of=MID)
    assert served["outcome"] in ("grounded", "historical") and FACT in served["answer_text"]
    now_read = service.query(project, QUESTION)
    assert now_read["outcome"] in ("empty", "abstained") and now_read["answer_text"] is None


def test_k_before_the_capture_sees_nothing(project):
    _absorb(project, [("wiki/h.md", FACT + "\n")], FEB)
    early = service.query(project, QUESTION, knowledge_as_of=MID)
    assert early["outcome"] in ("empty", "abstained") and early["answer_text"] is None


def test_older_version_answers_at_an_earlier_k_and_newer_at_latest(project):
    _absorb(project, [("wiki/h.md", "The heron gateway retries uploads twice.\n")], JAN)
    _absorb(project, [("wiki/h.md", "The heron gateway retries uploads five times.\n")], FEB)
    old = service.query(project, QUESTION, knowledge_as_of=MID)
    assert "twice" in old["answer_text"] and "five" not in old["answer_text"]
    new = service.query(project, QUESTION)
    assert "five times" in new["answer_text"] and "twice" not in new["answer_text"]


def test_retired_block_counts_for_the_ambiguity_gate_at_k(project):
    _absorb(project, [("wiki/dat-a.md", "[[Dat Nguyen|Dat]] works at [[Acme]]\n"),
                      ("wiki/dat-b.md", "[[Dat Tran|Dat]] works at [[Hooli]]\n")], JAN)
    _retire(project, ["wiki/dat-b.md"], FEB)
    at_k = service.query(project, "Where does Dat work", knowledge_as_of=MID)
    assert sorted((at_k.get("extra") or {}).get("ambiguous_entities") or []) == [
        "dat-nguyen", "dat-tran"]
    assert at_k["outcome"] == "abstained"
    latest = service.query(project, "Where does Dat work")
    assert "ambiguous_entities" not in (latest.get("extra") or {})


def test_withdrawn_block_is_never_visible_at_k(project):
    _absorb(project, [("wiki/h.md", FACT + "\n")], JAN)
    _retire(project, ["wiki/h.md"], FEB)
    with store(project) as conn:
        conn.execute("INSERT INTO exclusions(source_ref,reason,requested_by,record_event_id,"
                     "recorded_at) VALUES(?,?,?,?,?)",
                     ("document:wiki/h.md", "owner request", "owner", 1, FEB))
    result = service.query(project, QUESTION, knowledge_as_of=MID)
    assert result["outcome"] == "refused" and result["reason"] == "not-servable"
    assert FACT not in json.dumps(result)
    assert FACT not in service.context(project, QUESTION + " as known at 2026-01-15")


# ------------------------------------------------------------------ the vector arm and the trace
def test_k_pinned_read_under_hybrid_skips_the_vector_arm_and_says_so(project):
    set_config(project, CONFIG_B)
    _absorb(project, [("wiki/h.md", FACT + "\n")], JAN)
    pinned = service.query(project, QUESTION, knowledge_as_of=MID)
    assert pinned["outcome"] == "grounded"
    assert SKIPPED in pinned["trace"]
    assert "trace" not in pinned["extra"]
    unpinned = service.query(project, QUESTION)
    assert SKIPPED not in unpinned["trace"]
    with store(project) as conn:
        rows = conn.execute("SELECT txn_axis, channels, retrieval_trace FROM answers "
                            "ORDER BY created_at, rowid").fetchall()
    by_axis = {r["txn_axis"]: r for r in rows}
    assert "vector" not in json.loads(by_axis[MID]["channels"])
    assert "vector" in json.loads(by_axis["latest"]["channels"])
    assert SKIPPED in json.loads(by_axis[MID]["retrieval_trace"])["notes"]


def test_k_pinned_read_under_narrow_has_no_skip_note(project):
    _absorb(project, [("wiki/h.md", FACT + "\n")], JAN)
    assert service.query(project, QUESTION, knowledge_as_of=MID)["trace"] == []


def test_context_prints_trace_notes(project):
    """OB-09: `wiki context` prints the trace notes of its query."""
    set_config(project, CONFIG_B)
    append(project, "result", {"body": FACT_DATED}, session="s-ctx")
    capture(project)
    text = service.context(project, QUESTION + " as known at 2030-01-01")
    assert "Trace: " + SKIPPED in text
    assert "Outcome: grounded" in text
    assert "Trace:" not in service.context(project, QUESTION)


# ------------------------------------------------------------------ OB-20 legacy retired rows
def _legacy_retire(project, doc_id, recorded, retired):
    """The schema-3 retire_block: status superseded, superseded_at and valid_until both the retire
    time. `recorded` backdates the capture so a K can sit between the two."""
    with store(project) as conn:
        conn.execute("UPDATE blocks SET recorded_at=?, status='superseded', superseded_at=?, "
                     "valid_until=? WHERE doc_id=?", (recorded, retired, retired, doc_id))


def test_legacy_retired_block_visible_at_k(project):
    from alpaca.wiki import recovery
    eid = append(project, "result", {"body": FACT}, session="s-leg")
    capture(project)
    with store(project) as conn:
        doc_id = conn.execute("SELECT doc_id FROM docs WHERE doc_id LIKE ?",
                              ("raw/%%/%06d-%%" % eid,)).fetchone()[0]
    _legacy_retire(project, doc_id, JAN, FEB)
    assert _block(project, doc_id)["valid_until"] == FEB
    # before the repair the K clause reads valid_until as the end of validity at T=now
    assert service.query(project, QUESTION, knowledge_as_of=MID)["outcome"] in ("empty", "abstained")
    report = recovery.run(project, session="s-fix")
    assert report["legacy_block_validity_repaired"] >= 1
    assert _block(project, doc_id)["valid_until"] is None
    served = service.query(project, QUESTION, knowledge_as_of=MID)
    assert FACT in (served["answer_text"] or "")
    # a second run changes no row of the repaired block
    legacy = _block(project, doc_id)
    again = recovery.run(project, session="s-fix")
    assert again["legacy_block_validity_repaired"] == 0
    assert _block(project, doc_id) == legacy


def test_repair_leaves_a_block_with_a_real_valid_until(project):
    from alpaca.wiki.store.ledger import Ledger
    from alpaca.wiki.store.write import Writer
    from alpaca.wiki.store.db import DB
    _absorb(project, [("wiki/h.md", FACT + "\n")], JAN)
    with store(project) as conn:
        conn.execute("UPDATE blocks SET status='superseded', superseded_at=?, valid_until=?",
                     (FEB, "2026-01-20T00:00:00+00:00"))
    cfg = Config.for_project(project)
    db = DB(cfg)
    try:
        writer = Writer(db, Ledger(cfg.ledger_path, vault_dir=cfg.vault_dir))
        with writer.transaction():
            assert writer.clear_legacy_block_valid_until() == 0
    finally:
        db.close()
    assert _block(project, "wiki/h.md")["valid_until"] == "2026-01-20T00:00:00+00:00"
