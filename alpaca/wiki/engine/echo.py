"""engine.echo - echo-collapse to an independent-witness count or a hard NULL (oracle-safety.2).

The hole this closes: an echoed claim ("As [[X]] noted, …", a verbatim re-quote, a near-duplicate
restatement) inflated `corroboration_count` and then WON conflict arbitration on manufactured
weight. The fix collapses echo chains onto their provable primary witness and reports the count of
DISTINCT independent roots - or a **hard NULL** when independence cannot be proven. A fabricated
integer never leaks: ambiguity resolves to NULL, never a guess (there is no model in the loop).

Derivation is captured at absorb in `block_provenance(block_id, cites_block_id, cites_node_id,
relation, detector)` with `relation ∈ {quotes, attributes, restates, original}`:
  - `quotes`    - a verbatim span of a known block  -> provable primary (follow cites_block_id).
  - `attributes`- "As [[X]] noted" / VN "theo|dẫn lời|trích [[X]]" -> primary resolved IFF the cited
                  node has a UNIQUE original block, else NULL (unresolvable attribution).
  - `restates`  - near-duplicate claim w/o attribution -> conservatively collapsed to the earliest
                  asserter (undercount, never overcount).
  - `original`  - a root witness (records the node it defines in cites_node_id).

`independent_witness_count(db, edge_id)` walks every corroborating block's cites chain to a fixpoint
root and returns len(distinct roots); NULL on an unresolvable attribution or a cycle. The value is
persisted on `edges.independent_witness_count` (NULLABLE). `arbitrate_conflict` must consume it in
place of `corroboration_count` and DROP the corroboration term when it is NULL (authority + recency
still order) - see `corroboration_for_arbitration` and the shared_edits wiring note.

This module additively provisions its own schema (block_provenance table + three edges columns) at
runtime via the guarded `DB.migrate_column` / `CREATE TABLE IF NOT EXISTS` - no schema.sql edit.
"""
from __future__ import annotations

import re
from collections import defaultdict
import math
from typing import Optional

from ..determinism import normalize_text

# --------------------------------------------------------------------------- schema (additive)

def ensure_schema(db) -> None:
    """Idempotently add the echo-provenance table + edges columns. Safe to call repeatedly."""
    db.conn.execute(
        "CREATE TABLE IF NOT EXISTS block_provenance ("
        " block_id       TEXT NOT NULL,"
        " cites_block_id TEXT,"
        " cites_node_id  TEXT,"
        " relation       TEXT NOT NULL CHECK (relation IN ('quotes','attributes','restates','original')),"
        " detector       TEXT,"
        " PRIMARY KEY (block_id, relation, cites_block_id, cites_node_id))"
    )
    db.migrate_column("edges", "derived", "INTEGER DEFAULT 0")
    db.migrate_column("edges", "echo_of_edge_id", "TEXT")
    db.migrate_column("edges", "independent_witness_count", "INTEGER")
    db.commit()


def record_provenance(db, block_id: str, relation: str, cites_block_id: Optional[str] = None,
                      cites_node_id: Optional[str] = None, detector: str = "det") -> None:
    """Persist one derivation row (called at absorb). `relation` governs how the chain is walked."""
    db.conn.execute(
        "INSERT OR IGNORE INTO block_provenance"
        "(block_id, cites_block_id, cites_node_id, relation, detector) VALUES(?,?,?,?,?)",
        (block_id, cites_block_id, cites_node_id, relation, detector),
    )


# --------------------------------------------------------------------------- deterministic detector

# EN: "As [[X]] noted/said/wrote/argued", "according to [[X]]", "per [[X]]".
# VN: "theo [[X]]", "dẫn lời [[X]]", "trích [[X]]".
_ATTRIB = re.compile(
    r"(?:\bAs\b|\baccording to\b|\bper\b|\btheo\b|\bdẫn lời\b|\btrích\b)\s*\[\[([^\]|]+)",
    re.IGNORECASE)


def _slug(surface: str) -> str:
    s = normalize_text(surface).lower()
    s = re.sub(r"[^a-z0-9à-ỹ]+", "-", s).strip("-")
    return s


class EchoIndex:
    """Incremental normalized/token index for deterministic echo detection."""

    def __init__(self):
        self._doc: dict[str, str | None] = {}
        self._norm: dict[str, str] = {}
        self._tokens: dict[str, set[str]] = {}
        self._exact: dict[str, set[str]] = defaultdict(set)
        self._postings: dict[str, set[str]] = defaultdict(set)

    def add(self, block_id: str, text: str, doc_id: str | None = None) -> None:
        self.remove(block_id)
        normalized = normalize_text(text)
        tokens = set(normalized.split())
        self._doc[block_id] = doc_id
        self._norm[block_id] = normalized
        self._tokens[block_id] = tokens
        self._exact[normalized].add(block_id)
        for token in tokens:
            self._postings[token].add(block_id)

    def remove(self, block_id: str) -> None:
        normalized = self._norm.pop(block_id, None)
        tokens = self._tokens.pop(block_id, set())
        self._doc.pop(block_id, None)
        if normalized is not None:
            ids = self._exact.get(normalized)
            if ids is not None:
                ids.discard(block_id)
                if not ids:
                    self._exact.pop(normalized, None)
        for token in tokens:
            ids = self._postings.get(token)
            if ids is not None:
                ids.discard(block_id)
                if not ids:
                    self._postings.pop(token, None)

    def detect(self, text: str, restate_threshold: float = 0.8,
               exclude_doc_id: str | None = None) -> tuple[str, Optional[str], Optional[str]]:
        normalized = normalize_text(text)
        tokens = set(normalized.split())

        def allowed(block_id: str) -> bool:
            return exclude_doc_id is None or self._doc.get(block_id) != exclude_doc_id

        exact = sorted(block_id for block_id in self._exact.get(normalized, ()) if allowed(block_id))
        if exact:
            return "quotes", exact[0], None

        candidates: set[str] = set()
        # A Jaccard match at threshold t can omit at most floor((1-t)*|query|) query tokens.
        # Probe one more than that many least-common tokens: every qualifying candidate must share
        # at least one probe, while common prose tokens no longer turn each lookup into a full scan.
        probe_count = max(1, math.floor((1.0 - restate_threshold) * len(tokens)) + 1)
        probes = sorted(tokens, key=lambda token: (len(self._postings.get(token, ())), token))[
            :probe_count
        ]
        for token in probes:
            candidates.update(self._postings.get(token, ()))
        candidates = {block_id for block_id in candidates if allowed(block_id)}
        for block_id in sorted(candidates):
            known = self._norm[block_id]
            if known and known in normalized:
                return "quotes", block_id, None

        match = _ATTRIB.search(text)
        if match:
            return "attributes", None, _slug(match.group(1))

        best_id, best_score = None, 0.0
        for block_id in sorted(candidates):
            known_tokens = self._tokens[block_id]
            union = tokens | known_tokens
            score = len(tokens & known_tokens) / len(union) if union else 0.0
            if score > best_score:
                best_id, best_score = block_id, score
        if best_id is not None and best_score >= restate_threshold:
            return "restates", best_id, None
        return "original", None, None


def detect_echo(text: str, known_blocks: dict[str, str] | EchoIndex,
                restate_threshold: float = 0.8,
                exclude_doc_id: str | None = None) -> tuple[str, Optional[str], Optional[str]]:
    """Classify a block's derivation deterministically.

    `known_blocks`: block_id -> that block's text (the already-absorbed corpus to test echoes against).
    Returns (relation, cites_block_id, cites_node_id). Order: verbatim quote -> attribution ->
    near-duplicate restatement -> original. Never guesses across the primary; ambiguity is left for
    `independent_witness_count` to resolve to NULL.
    """
    if isinstance(known_blocks, EchoIndex):
        return known_blocks.detect(text, restate_threshold, exclude_doc_id)
    index = EchoIndex()
    for block_id, known_text in known_blocks.items():
        index.add(block_id, known_text)
    return index.detect(text, restate_threshold)


# --------------------------------------------------------------------------- independence walk

def _originals_for_node(db, node_id: str) -> list[str]:
    rows = db.conn.execute(
        "SELECT block_id FROM block_provenance WHERE relation='original' AND cites_node_id=? "
        "ORDER BY block_id", (node_id,),
    ).fetchall()
    return [r["block_id"] for r in rows]


def _prov_row(db, block_id: str) -> Optional[dict]:
    r = db.conn.execute(
        "SELECT block_id, cites_block_id, cites_node_id, relation FROM block_provenance "
        "WHERE block_id=? ORDER BY relation, cites_block_id, cites_node_id LIMIT 1", (block_id,),
    ).fetchone()
    return dict(r) if r else None


def _trace_root(db, block_id: str, visited: set) -> Optional[str]:
    """Follow a block's cites chain to a fixpoint primary root. NULL on cycle / unresolvable attrib."""
    if block_id in visited:
        return None                                   # cycle -> unprovable
    visited.add(block_id)
    row = _prov_row(db, block_id)
    if row is None or row["relation"] == "original":
        return block_id                               # a root witness
    rel = row["relation"]
    if rel in ("quotes", "restates"):
        nxt = row["cites_block_id"]
        return _trace_root(db, nxt, visited) if nxt else block_id
    if rel == "attributes":
        node = row["cites_node_id"]
        if not node:
            return None
        originals = _originals_for_node(db, node)
        if len(originals) != 1:
            return None                               # 0 or >1 primaries -> unresolvable attribution
        return _trace_root(db, originals[0], visited)
    return None


def _corroborating_blocks(db, edge_id: str) -> list[str]:
    rows = db.conn.execute(
        "SELECT source_block_id FROM edge_corroborations WHERE edge_id=? ORDER BY source_block_id",
        (edge_id,),
    ).fetchall()
    blocks = [r["source_block_id"] for r in rows]
    if not blocks:
        r = db.conn.execute("SELECT source_block_id FROM edges WHERE edge_id=?", (edge_id,)).fetchone()
        if r and r["source_block_id"]:
            blocks = [r["source_block_id"]]
    return blocks


def independent_witness_count(db, edge_id: str) -> Optional[int]:
    """Collapse echo chains onto primaries; return the count of DISTINCT independent roots, or NULL.

    Returns an int ONLY when every corroborating source resolves to a provable primary root; any
    unresolvable attribution or cycle yields a HARD NULL (flagged-uncomputed) - never a fabricated
    integer, and never the raw (echo-inflated) corroboration_count.
    """
    roots: set[str] = set()
    for b in _corroborating_blocks(db, edge_id):
        root = _trace_root(db, b, set())
        if root is None:
            return None
        roots.add(root)
    return len(roots)


def persist_independent_witness_count(db, edge_id: str) -> Optional[int]:
    """Compute and store the count on edges.independent_witness_count. Returns the value (int|None)."""
    ensure_schema(db)
    val = independent_witness_count(db, edge_id)
    db.conn.execute("UPDATE edges SET independent_witness_count=? WHERE edge_id=?", (val, edge_id))
    db.commit()
    return val


# --------------------------------------------------------------------------- arbitration hook

def corroboration_for_arbitration(db, edge: dict) -> Optional[int]:
    """The value `arbitrate_conflict` must use instead of raw corroboration_count.

    Prefer the persisted independent_witness_count; recompute if absent. Returns NULL when
    independence is unprovable - the caller MUST then DROP the corroboration term entirely (letting
    authority + recency order), never fall back to the inflated raw count.
    """
    if edge.get("independent_witness_count") is not None:
        return int(edge["independent_witness_count"])
    eid = edge.get("edge_id")
    if not eid:
        return None
    return independent_witness_count(db, eid)
