"""Vector arm. sqlite-vec seam + a pure-stdlib brute-force fallback (<=100k blocks, per §3).

Embeddings are a recomputable cache, never ground truth. A portable float64 BLOB
cache and brute-force cosine keep reads deterministic with or without sqlite-vec.
The extension setup seam is retained for compatibility; reads use the portable cache.
"""
from __future__ import annotations

import math

from .db import DB


from .vector_codec import (
    DEFAULT_EMBED_DIM, MAX_EMBED_DIM, VectorValidationError,
    _validated_embed_dim, _validated_vector, _pack, _unpack, _cosine,
)


def try_load_sqlite_vec(db: DB) -> bool:
    """Best-effort: load the extension + create vec0 tables. Returns True if active."""
    loaded = False
    try:
        dim = _validated_embed_dim(db)
        import sqlite_vec  # type: ignore
        db.conn.enable_load_extension(True)
        sqlite_vec.load(db.conn)
        db.conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS vec_blocks USING "
            f"vec0(block_id TEXT PRIMARY KEY, embedding float[{dim}])"
        )
        db.conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS vec_nodes USING "
            f"vec0(node_id TEXT PRIMARY KEY, embedding float[{dim}])"
        )
        db.set_meta("sqlite_vec_version", getattr(sqlite_vec, "__version__", "loaded"))
        db.commit()
        loaded = True
    except Exception:
        loaded = False
    finally:
        try:
            db.conn.enable_load_extension(False)
        except Exception:
            loaded = False
    return loaded


def ensure_vec_tables(db: DB) -> None:
    """Create the stdlib fallback tables (always safe). vec0 tables are created lazily on load."""
    db.conn.execute("CREATE TABLE IF NOT EXISTS vec_blocks_fallback(block_id TEXT PRIMARY KEY, embedding BLOB)")
    db.conn.execute("CREATE TABLE IF NOT EXISTS vec_nodes_fallback(node_id TEXT PRIMARY KEY, embedding BLOB)")
    db.commit()


def _has_vec0(db: DB) -> bool:
    row = db.conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE name IN ('vec_blocks','vec_nodes') AND type IN ('table','virtual')"
    ).fetchone()
    return bool(row and int(row[0]) == 2)


def upsert_block_vec(db: DB, block_id: str, vec: list[float]) -> None:
    packed = _pack(vec, _validated_embed_dim(db))
    db.conn.execute("INSERT OR REPLACE INTO vec_blocks_fallback(block_id, embedding) VALUES(?, ?)",
                    (block_id, packed))


def upsert_node_vec(db: DB, node_id: str, vec: list[float]) -> None:
    packed = _pack(vec, _validated_embed_dim(db))
    db.conn.execute("INSERT OR REPLACE INTO vec_nodes_fallback(node_id, embedding) VALUES(?, ?)",
                    (node_id, packed))


def knn_blocks(db: DB, qvec: list[float], limit: int = 50) -> list[tuple[str, float]]:
    """Brute-force cosine kNN over active blocks. Deterministic tie-break by block_id ASC."""
    try:
        query = _validated_vector(qvec, _validated_embed_dim(db))
    except VectorValidationError:
        return []
    rows = db.conn.execute(
        "SELECT block_id, embedding FROM vec_blocks_fallback"
    ).fetchall()
    scored = []
    active = {r["block_id"] for r in db.conn.execute("SELECT block_id FROM blocks WHERE status='active'")}
    for r in rows:
        bid = r["block_id"]
        if bid not in active:
            continue
        try:
            stored = _unpack(r["embedding"], len(query))
        except VectorValidationError:
            continue
        score = _cosine(query, stored)
        if math.isfinite(score):
            scored.append((bid, score))
    scored.sort(key=lambda t: (-t[1], t[0]))
    return scored[:limit]


def knn_nodes(db: DB, qvec: list[float], limit: int = 20) -> list[tuple[str, float]]:
    try:
        query = _validated_vector(qvec, _validated_embed_dim(db))
    except VectorValidationError:
        return []
    tbl = "vec_nodes_fallback"
    rows = db.conn.execute(f"SELECT node_id, embedding FROM {tbl}").fetchall()
    scored = []
    for r in rows:
        try:
            stored = _unpack(r["embedding"], len(query))
        except VectorValidationError:
            continue
        score = _cosine(query, stored)
        if math.isfinite(score):
            scored.append((r["node_id"], score))
    scored.sort(key=lambda t: (-t[1], t[0]))
    return scored[:limit]
