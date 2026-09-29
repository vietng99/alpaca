"""Pure vector encoding and validation shared by the read and write doors."""
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


