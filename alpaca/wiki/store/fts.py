"""BM25 lexical arm over blocks_fts (FTS5). Deterministic ordering (bm25 asc, block_id asc)."""
from __future__ import annotations

import re

from .asof import AsOf, block_where
from .db import DB

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def fts_query_string(question: str) -> str:
    """Build a safe FTS5 MATCH string: OR of quoted tokens (avoids syntax injection from prose)."""
    toks = _TOKEN.findall(question.lower())
    toks = [t for t in toks if len(t) > 1]
    if not toks:
        return ""
    return " OR ".join(f'"{t}"' for t in toks)


def search_bm25(db: DB, question: str, limit: int = 50, asof: AsOf | None = None,
                narrow: tuple[str, dict] | None = None) -> list[tuple[str, float]]:
    """Return [(block_id, score)] best-first. score = -bm25 (higher better) for RRF rank use.

    `narrow` is the (sql, params) of `filters.block_clause(..., "b")`: the FR-11 filters are applied
    in the query, so a narrow filter ranks within its scope and does not cut a global top-k."""
    q = fts_query_string(question)
    if not q:
        return []
    visible, params = block_where(asof, "b")
    if narrow is not None and narrow[0] != "1":
        visible = f"{visible} AND {narrow[0]}"
        params = {**params, **narrow[1]}
    definition = db.conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='blocks_fts'"
    ).fetchone()
    using_fts5 = bool(definition and definition[0] and "using fts5" in definition[0].lower())
    if using_fts5:
        rows = db.conn.execute(
            "SELECT b.block_id AS block_id, bm25(blocks_fts) AS score "
            # CROSS JOIN fixes the order: the FTS match first, then one blocks row per match. With a
            # plain JOIN the as-of clause (valid_from range, status) lets the planner walk the blocks
            # index and probe the FTS per row, which took 70 s on a 70,000-block store.
            "FROM blocks_fts CROSS JOIN blocks b ON b.rowid = blocks_fts.rowid "
            f"WHERE blocks_fts MATCH :match AND {visible} "
            "ORDER BY score ASC, b.block_id ASC LIMIT :limit",
            {**params, "match": q, "limit": limit},
        ).fetchall()
        return [(r["block_id"], -float(r["score"])) for r in rows]

    query_tokens = set(_TOKEN.findall(question.casefold()))
    rows = db.conn.execute(
        "SELECT b.block_id,b.text,COALESCE(b.context_header,'') AS context_header "
        f"FROM blocks_fts f JOIN blocks b ON b.rowid=f.rowid WHERE {visible} "
        "ORDER BY b.block_id", params
    ).fetchall()
    scored = []
    for row in rows:
        tokens = set(_TOKEN.findall((row["text"] + " " + row["context_header"]).casefold()))
        overlap = len(query_tokens & tokens)
        if overlap:
            scored.append((row["block_id"], float(overlap)))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored[:limit]
