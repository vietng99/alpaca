"""Ledger authority: DB-alone lineage + deterministic poisoned-event replay (knowledge-model.8).

Two independent readers of the transaction-time truth:

* `lineage(db, edge_id)` reconstructs an assertion's belief windows from the `edges` TABLE ALONE
  (recorded_at / superseded_at + the supersedes chain). It never touches events.jsonl, so lineage
  survives `rm events.jsonl` - rune.db is self-sufficient for "what did the KB believe, and when
  did it stop".

* `replay_events(rows)` rebuilds state deterministically from the ingest_event hash-chain and, on
  the first poisoned/checksum-broken event, HALTS at the last good seq and returns a recoverable
  snapshot. Two runs over the same rows halt at the identical `last_good_seq`.

Pure stdlib. Reuses the frozen `event_checksum` so the halt point matches `verify_chain`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .ledger import GENESIS, event_checksum


# ---------------------------------------------------------------- DB-alone lineage

def _get_edge(db: Any, edge_id: str) -> Optional[dict]:
    r = db.conn.execute("SELECT * FROM edges WHERE edge_id = ?", (edge_id,)).fetchone()
    return dict(r) if r else None


def lineage(db: Any, edge_id: str) -> list[dict]:
    """Belief-window lineage for an assertion, read from the edges table alone.

    Walks the supersedes chain back to its root, then forward through superseded_by, emitting one
    window per link: [believed_from = recorded_at, believed_until = superseded_at) plus who
    retired it (superseded_by_edge_id). No events.jsonl / Ledger read anywhere.
    """
    start = _get_edge(db, edge_id)
    if start is None:
        return []

    # walk back to the chain root via supersedes_edge_id
    root = start
    guard: set[str] = set()
    while root.get("supersedes_edge_id") and root["edge_id"] not in guard:
        guard.add(root["edge_id"])
        prev = _get_edge(db, root["supersedes_edge_id"])
        if prev is None:
            break
        root = prev

    # Walk forward through reverse supersedes links. Transaction corrections also carry the direct
    # superseded_by pointer, but world-change predecessors intentionally remain active and only the
    # successor points backward. Querying reverse links covers both axes.
    chain: list[dict] = []
    seen: set[str] = set()
    pending: list[dict] = [root]
    while pending:
        pending.sort(key=lambda edge: (
            edge.get("valid_from") or "", edge.get("recorded_at") or "", edge["edge_id"],
        ))
        cur = pending.pop(0)
        if cur["edge_id"] in seen:
            continue
        seen.add(cur["edge_id"])
        chain.append({
            "edge_id": cur["edge_id"],
            "subj_node": cur["subj_node"],
            "predicate": cur["predicate"],
            "obj": cur["obj_node"] if cur["obj_node"] is not None else cur["obj_literal"],
            "believed_from": cur["recorded_at"],
            "believed_until": cur["superseded_at"],
            "valid_from": cur["valid_from"],
            "valid_until": cur["valid_until"],
            "superseded_by_edge_id": cur["superseded_by_edge_id"],
            "status": cur["status"],
        })
        children = db.conn.execute(
            "SELECT * FROM edges WHERE supersedes_edge_id=? ORDER BY valid_from,recorded_at,edge_id",
            (cur["edge_id"],),
        ).fetchall()
        pending.extend(dict(row) for row in children if row["edge_id"] not in seen)
    return chain


# ---------------------------------------------------------------- deterministic replay

@dataclass
class ReplayResult:
    last_good_seq: int
    halted: bool
    reason: str
    applied: list[int] = field(default_factory=list)
    state: dict[str, dict] = field(default_factory=dict)

    def recoverable_snapshot(self) -> dict[str, dict]:
        """The state accumulated up to (and including) the last good seq - safe to trust."""
        return dict(self.state)


def _apply(state: dict[str, dict], event: dict) -> None:
    """Deterministic reduction of one good event onto the projected state.

    Only edge-affecting ops mutate the snapshot; the projection is intentionally small and stable
    so the recoverable snapshot is byte-reproducible across runs.
    """
    op = event.get("op")
    payload = event.get("payload", {}) or {}
    eid = payload.get("edge_id")
    if op == "upsert_edge" and eid:
        if payload.get("reanchor_to") and eid in state:
            state[eid]["source_block_id"] = payload["reanchor_to"]
            state[eid]["source_doc_id"] = payload.get("source_doc_id")
        else:
            state[eid] = {"op": op, "subj": payload.get("subj"), "pred": payload.get("pred"),
                          "obj_key": payload.get("obj_key") or payload.get("obj_key_sha256"),
                          "status": "active"}
    elif op == "supersede_edge":
        prior = payload.get("prior")
        if prior and prior in state:
            if payload.get("world_change"):
                state[prior]["valid_until"] = payload.get("world_at")
            else:
                state[prior]["status"] = "superseded"
    elif op == "cure" and eid and eid in state:
        state[eid]["status"] = "retracted"


def replay_events(rows: list[dict]) -> ReplayResult:
    """Rebuild state from ingest_event rows; halt deterministically at the last good seq.

    `rows` are event dicts carrying seq/prev_checksum/checksum/op/doc_id/payload (as produced by
    query.all_events or Ledger.read_all), ordered by seq ascending. On the first seq gap, broken
    prev link, or recomputed-checksum mismatch, replay stops WITHOUT applying the poisoned event
    and returns the snapshot at last_good_seq.
    """
    prev = GENESIS
    last_good_seq = 0
    state: dict[str, dict] = {}
    applied: list[int] = []

    for r in rows:
        seq = r["seq"]
        if seq != last_good_seq + 1:
            return ReplayResult(last_good_seq, True, f"seq gap at {seq}", applied, state)
        if r.get("prev_checksum") != prev:
            return ReplayResult(last_good_seq, True, f"prev break at seq {seq}", applied, state)
        expect = event_checksum(seq, prev, r["op"], r.get("doc_id"), r.get("payload", {}) or {})
        if expect != r.get("checksum"):
            return ReplayResult(last_good_seq, True, f"checksum mismatch at seq {seq}", applied, state)
        # good event: apply and advance
        _apply(state, r)
        applied.append(seq)
        prev = r["checksum"]
        last_good_seq = seq

    return ReplayResult(last_good_seq, False, "ok", applied, state)
