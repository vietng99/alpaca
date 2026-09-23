"""HASHGATE (ADR-042): SHA-256 skip-if-unchanged at doc and block granularity.

Only changed docs are re-processed; within a changed doc, only changed blocks are re-embedded /
re-extracted via the block_sha256 cache - LightRAG-style incremental, no full reindex (R5/R7).
"""
from __future__ import annotations

from ..determinism import sha256_hex
from ..store.db import DB


def doc_changed(db: DB, doc_id: str, content: str) -> tuple[bool, str]:
    """Return (changed, new_sha). Unchanged docs short-circuit the whole pipeline."""
    new_sha = sha256_hex(content)
    row = db.conn.execute("SELECT content_sha256 FROM docs WHERE doc_id=?", (doc_id,)).fetchone()
    return (row is None or row["content_sha256"] != new_sha), new_sha


def block_changed(db: DB, block_id: str, block_sha: str) -> bool:
    row = db.conn.execute("SELECT block_sha256 FROM blocks WHERE block_id=?", (block_id,)).fetchone()
    return row is None or row["block_sha256"] != block_sha
