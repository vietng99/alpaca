"""Vector arm. sqlite-vec seam + a pure-stdlib brute-force fallback (<=100k blocks, per §3).

Embeddings are a recomputable cache (LEANN-style), never ground truth. If the sqlite-vec
extension loads, vec0 virtual tables are used; otherwise a plain BLOB table + brute-force cosine.
Both paths are deterministic given the same vectors.
"""
from __future__ import annotations

from collections.abc import Sequence
import math
import struct

from .db import DB


DEFAULT_EMBED_DIM = 64
MAX_EMBED_DIM = 65536
_FLOAT_BYTES = 8


class VectorValidationError(ValueError):
    """Vector data is unsafe or incompatible with the configured embedding dimension."""


def _validated_embed_dim(db: DB) -> int:
    raw = db.get_meta("embed_dim")
    raw = DEFAULT_EMBED_DIM if raw is None else raw
    if isinstance(raw, bool):
        raise VectorValidationError("embed_dim must be an integer, not bool")
    try:
        dim = int(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise VectorValidationError(f"embed_dim must be an integer: {raw!r}") from exc
    if str(raw).strip() not in (str(dim), f"+{dim}"):
        raise VectorValidationError(f"embed_dim must be an integer: {raw!r}")
    if not 1 <= dim <= MAX_EMBED_DIM:
        raise VectorValidationError(
            f"embed_dim must be between 1 and {MAX_EMBED_DIM}, got {dim}"
        )
    return dim


def _validated_vector(vec: Sequence[float], expected_dim: int | None = None) -> list[float]:
    if isinstance(vec, (str, bytes, bytearray, memoryview)) or not isinstance(vec, Sequence):
        raise VectorValidationError("vector must be a numeric sequence")
    size = len(vec)
    if size < 1 or size > MAX_EMBED_DIM:
        raise VectorValidationError(f"vector dimension must be between 1 and {MAX_EMBED_DIM}")
    if expected_dim is not None and size != expected_dim:
        raise VectorValidationError(
            f"vector dimension mismatch: expected {expected_dim}, got {size}"
        )
    values: list[float] = []
    for value in vec:
        if isinstance(value, bool):
            raise VectorValidationError("vector values must be finite numbers")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise VectorValidationError("vector values must be finite numbers") from exc
        if not math.isfinite(number):
            raise VectorValidationError("vector values must be finite numbers")
        values.append(number)
    return values


def _pack(vec: Sequence[float], expected_dim: int | None = None) -> bytes:
    values = _validated_vector(vec, expected_dim)
    try:
        return struct.pack(f"<{len(values)}d", *values)
    except struct.error as exc:
        raise VectorValidationError("vector cannot be encoded") from exc


def _unpack(blob: bytes, expected_dim: int | None = None) -> list[float]:
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise VectorValidationError("vector blob must be bytes-like")
    raw = bytes(blob)
    if not raw or len(raw) % _FLOAT_BYTES:
        raise VectorValidationError("vector blob length is not aligned to float64 values")
    size = len(raw) // _FLOAT_BYTES
    if size > MAX_EMBED_DIM:
        raise VectorValidationError("vector blob exceeds maximum embedding dimension")
    if expected_dim is not None and size != expected_dim:
        raise VectorValidationError(
            f"vector blob dimension mismatch: expected {expected_dim}, got {size}"
        )
    try:
        values = struct.unpack(f"<{size}d", raw)
    except struct.error as exc:
        raise VectorValidationError("vector blob cannot be decoded") from exc
    return _validated_vector(values, expected_dim)


def _cosine(a: list[float], b: list[float]) -> float:
    try:
        av = _validated_vector(a)
        bv = _validated_vector(b, len(av))
    except VectorValidationError:
        return 0.0
    scale_a = max(abs(x) for x in av)
    scale_b = max(abs(x) for x in bv)
    if scale_a == 0.0 or scale_b == 0.0:
        return 0.0
    scaled_a = [x / scale_a for x in av]
    scaled_b = [x / scale_b for x in bv]
    dot = sum(x * y for x, y in zip(scaled_a, scaled_b))
    na = math.sqrt(sum(x * x for x in scaled_a))
    nb = math.sqrt(sum(y * y for y in scaled_b))
    score = dot / (na * nb)
    if not math.isfinite(score):
        return 0.0
    return max(-1.0, min(1.0, score))


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
    if _has_vec0(db):
        db.conn.execute("INSERT OR REPLACE INTO vec_blocks(block_id, embedding) VALUES(?, ?)",
                        (block_id, packed))
    else:
        db.conn.execute("INSERT OR REPLACE INTO vec_blocks_fallback(block_id, embedding) VALUES(?, ?)",
                        (block_id, packed))


def upsert_node_vec(db: DB, node_id: str, vec: list[float]) -> None:
    packed = _pack(vec, _validated_embed_dim(db))
    if _has_vec0(db):
        db.conn.execute("INSERT OR REPLACE INTO vec_nodes(node_id, embedding) VALUES(?, ?)",
                        (node_id, packed))
    else:
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
    ).fetchall() if not _has_vec0(db) else db.conn.execute(
        "SELECT block_id, embedding FROM vec_blocks"
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
    tbl = "vec_nodes" if _has_vec0(db) else "vec_nodes_fallback"
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
