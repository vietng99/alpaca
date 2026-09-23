"""BM25 lexical arm over blocks_fts (FTS5). Deterministic ordering (bm25 asc, block_id asc)."""
from __future__ import annotations

import re

from .db import DB

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def fts_query_string(question: str) -> str:
    """Build a safe FTS5 MATCH string: OR of quoted tokens (avoids syntax injection from prose)."""
    toks = _TOKEN.findall(question.lower())
    toks = [t for t in toks if len(t) > 1]
    if not toks:
        return ""
    return " OR ".join(f'"{t}"' for t in toks)


def search_bm25(db: DB, question: str, limit: int = 50) -> list[tuple[str, float]]:
    """Return [(block_id, score)] best-first. score = -bm25 (higher better) for RRF rank use."""
    q = fts_query_string(question)
    if not q:
        return []
    definition = db.conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='blocks_fts'"
    ).fetchone()
    using_fts5 = bool(definition and definition[0] and "using fts5" in definition[0].lower())
    if using_fts5:
        rows = db.conn.execute(
            "SELECT b.block_id AS block_id, bm25(blocks_fts) AS score "
            "FROM blocks_fts JOIN blocks b ON b.rowid = blocks_fts.rowid "
            "WHERE blocks_fts MATCH ? AND b.status='active' "
            "ORDER BY score ASC, b.block_id ASC LIMIT ?",
            (q, limit),
        ).fetchall()
        return [(r["block_id"], -float(r["score"])) for r in rows]

    query_tokens = set(_TOKEN.findall(question.casefold()))
    rows = db.conn.execute(
        "SELECT b.block_id,b.text,COALESCE(b.context_header,'') AS context_header "
        "FROM blocks_fts f JOIN blocks b ON b.rowid=f.rowid WHERE b.status='active' "
        "ORDER BY b.block_id"
    ).fetchall()
    scored = []
    for row in rows:
        tokens = set(_TOKEN.findall((row["text"] + " " + row["context_header"]).casefold()))
        overlap = len(query_tokens & tokens)
        if overlap:
            scored.append((row["block_id"], float(overlap)))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored[:limit]
