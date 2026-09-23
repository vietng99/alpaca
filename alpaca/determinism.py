"""The deterministic middle: one owner for hashes, canonical serialization, numbers
and ordering, so a re-run is byte-reproducible regardless of PYTHONHASHSEED, dict insertion
order or the process locale.

Rules enforced here:
  * `canonical` forbids NaN/Inf (`allow_nan=False`) so a non-finite float can never enter a hash
    surface, sorts keys and preserves UTF-8. It is byte-identical to `util.canonical_json` on the
    same input; that function stays the low-level primitive the append-only event chain already
    hashes, and is left untouched.
  * A float never orders anything directly. `band` projects a float to a fixed-scale integer
    first, so last-bit cross-environment float noise cannot flip a hashed order. `stable_rank`
    applies `band` to any float in a sort key automatically.
  * Wall-clock cost durations are stamps, not inputs. They are excluded from every hash and every
    order; `determinism_hash` folds only the deterministic fields it is given. See the module
    self-test in tests/test_determinism.py.
"""
import hashlib
import json
import unicodedata
from typing import Any, Callable, Iterable

# Fields that are wall-clock cost stamps: recorded for accounting, never hashed or ordered.
COST_STAMP_FIELDS = ("cost_ms", "wall_ms", "elapsed_ms", "duration_ms", "cost_notional_usd")


def nfc(s: str) -> str:
    """NFC-normalize a string before it enters a hash surface."""
    return unicodedata.normalize("NFC", s)


def canonical(obj: Any) -> str:
    """Stable JSON: sorted keys, tight separators, UTF-8 preserved, NaN/Inf refused.

    Byte-identical to util.canonical_json on the same input (that primitive omits allow_nan);
    the extra guard here rejects a non-finite float instead of emitting `NaN`/`Infinity`.
    """
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def canonical_bytes(obj: Any) -> bytes:
    """UTF-8 bytes of the canonical serialization (no BOM). The surface for hashing."""
    return canonical(obj).encode("utf-8")


def stable_hash(data: Any) -> str:
    """sha256 hex of the canonical form. Accepts bytes/str verbatim, any other object canonically."""
    if isinstance(data, bytes):
        return hashlib.sha256(data).hexdigest()
    if isinstance(data, str):
        return hashlib.sha256(data.encode("utf-8")).hexdigest()
    return hashlib.sha256(canonical_bytes(data)).hexdigest()


def band(x: float, scale: int = 1_000_000) -> int:
    """Project a float to a fixed-scale integer BEFORE it can influence a hashed ordering.

    Cross-environment float noise lives in the last bits of a float64; rounding to a fixed
    integer scale absorbs it so an ordering keyed on a score cannot flip between machines.
    """
    return int(round(float(x) * scale))


def stable_rank(items: Iterable[Any], key: Callable[[Any], Any]) -> list:
    """Deterministic sort. `key` returns a scalar or a tuple; any float component is projected to
    an integer band first, so no raw float ever reaches the comparison. Ties fall to the rest of
    the key, which the caller makes total (e.g. append an id) for a byte-reproducible order.
    """
    def _key(item):
        k = key(item)
        if not isinstance(k, tuple):
            k = (k,)
        return tuple(band(part) if isinstance(part, float) else part for part in k)
    return sorted(items, key=_key)


def determinism_hash(fields: dict) -> str:
    """Fold the deterministic fields of a payload into one digest, dropping cost stamps.

    A wall-clock cost duration (COST_STAMP_FIELDS) is a stamp, not an input: two payloads that
    differ only in their cost stamps produce the same digest.
    """
    payload = {k: v for k, v in fields.items() if k not in COST_STAMP_FIELDS}
    return stable_hash(payload)
