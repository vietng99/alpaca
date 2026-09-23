"""Write-time citation capture on the answer path (§5 STEP 5 / R3).

The answer path is read-only over the GRAPH; the only rows it writes are its own audit records -
answers + claims + eval_ledger - so logging is unskippable and answer->claim->source is one hop.
This module does NOT import store.write (the graph writer); it inserts its own audit rows.
"""
from __future__ import annotations

import uuid

from ..clock import Clock, now_iso
from ..determinism import canonical_json
from ..store.db import DB
from ..types import Answer


def persist(db: DB, answer: Answer, *, qtype: str, query_hash: str, dag: dict,
            channels: dict, model_versions: dict, determinism_hash: str,
            fast_path: bool, latency_ms: int, retrieval_trace: dict,
            asof_gated: int = 0, stale_serve: int = 0,
            clock: Clock = now_iso) -> str:
    """Write the answer path's own audit rows: answers + claims + eval_ledger.

    `clock` is injectable for the same reason `store.write.Writer` takes one: these three tables
    stamp `created_at` / `ts`, and a caller replaying a fixture under a pinned clock cannot
    reproduce rows this module stamped from wall time. MEASURED before it was added: two brains
    built from one frozen fixture under one pinned clock produced the SAME answer
    `determinism_hash` while `answers.created_at`, `claims.created_at` and `eval_ledger.ts` all
    carried real wall time, so their canonical dumps diverged on `eval_ledger` alone.

    The default is `now_iso`, so every existing caller keeps exactly the behaviour it had; this
    is an additive parameter, not a change of policy about when the ledger stamps.

    HONEST LIMIT, deliberately not papered over: `latency_ms` is measured by the CALLER from a
    `perf_counter` delta and is wall-derived by nature. No clock injection can make it
    reproducible, and pretending otherwise would be worse than declaring it. A caller that needs
    a byte-identical `eval_ledger` must pin `latency_ms` itself.
    """
    from ..store.db import assert_rebuild_access
    assert_rebuild_access(db.cfg)
    now = clock()
    axis_key = f"{query_hash}|{answer.currency_stamp.as_of}|{answer.currency_stamp.txn_axis}"
    db.conn.execute("BEGIN IMMEDIATE")
    try:
        assert_rebuild_access(db.cfg)
        ordinal = int(db.conn.execute(
            "SELECT COUNT(*) FROM answers WHERE query_hash=? AND as_of=?",
            (query_hash, answer.currency_stamp.as_of),
        ).fetchone()[0]) + 1
        base = "a_" + uuid.uuid5(uuid.NAMESPACE_URL, axis_key).hex[:20]
        aid = f"{base}_{ordinal:08d}"
        while db.conn.execute("SELECT 1 FROM answers WHERE answer_id=?", (aid,)).fetchone():
            ordinal += 1
            aid = f"{base}_{ordinal:08d}"
        db.conn.execute(
            "INSERT INTO answers(answer_id,question,query_hash,as_of,txn_axis,qtype,verdict,"
            "answer_text,success_predicate,verdict_dag,retrieval_trace,channels,model_versions,"
            "determinism_hash,fast_path,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, answer.question, query_hash, answer.currency_stamp.as_of,
             answer.currency_stamp.txn_axis, qtype, answer.verdict,
             answer.answer_text, canonical_json({"completeness": answer.completeness.__dict__}),
             canonical_json(dag), canonical_json(retrieval_trace), canonical_json(channels),
             canonical_json(model_versions), determinism_hash, 1 if fast_path else 0, now),
        )
        for i, c in enumerate(answer.citations):
            db.conn.execute(
                "INSERT INTO claims(claim_id,answer_id,claim_text,claim_kind,source_block_id,"
                "source_edge_id,entailment_score,checker,verdict,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"{aid}:c{i}", aid, c.claim_text, c.claim_kind, c.source_block_id, c.source_edge_id,
                 c.entailment_score, c.checker, c.verdict, now),
            )
        n_cit = len(answer.citations)
        db.conn.execute(
            "INSERT INTO eval_ledger(query_id,ts,qtype,channels,verdict,n_citations,"
            "latency_ms,abstained,fast_path,asof_gated,stale_serve) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (aid, now, qtype, canonical_json(list(channels.keys())), answer.verdict, n_cit,
             latency_ms, 1 if answer.abstained else 0, 1 if fast_path else 0,
             int(asof_gated), int(stale_serve)),
        )
        db.conn.commit()
    except BaseException:
        db.conn.rollback()
        raise
    return aid
