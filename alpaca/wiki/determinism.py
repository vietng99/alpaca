"""The deterministic middle (R4, ADR-028).

Owns every hash, canonical serialization, RRF math, PPR iteration, and the stable tie-break.
The LLM never emits a scalar, citation, or ordering - those are all computed here so a re-run is
byte-reproducible. `determinism_hash` folds the answer's numbers/citations/order into one digest.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Iterable

# ---------------------------------------------------------------- canonical primitives

def canonical_json(obj: Any) -> str:
    """Stable JSON: sorted keys, no ambiguous whitespace, UTF-8 preserved (ensure_ascii off).

    Nuclear Op-5 Fork-A ruling: the existing sort_keys surface is kept (byte-identical to RFC 8785
    JCS on the ASCII-keyed objects we hash, so no edge_id re-pour). `allow_nan=False` forbids a NaN/
    Inf ever entering a hash surface, and strings must be NFC-normalized before they get here.
    """
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def nfc(s: str) -> str:
    """NFC-normalize a string at the write boundary before it enters any hash surface (Fork-A)."""
    return unicodedata.normalize("NFC", s)


def assert_nfc(s: str) -> str:
    """Read-side guard: a persisted string that is not already NFC is a normalization bug."""
    if s != unicodedata.normalize("NFC", s):
        raise ValueError("non-NFC string reached a hash surface")
    return s


def to_band(x: float, scale: int = 1_000_000) -> int:
    """Project a float score to an INTEGER band BEFORE it can influence a hashed ordering (Fork-A).

    Cross-environment float noise (BLAS/OMP thread counts, scipy PPR, reranker) lives in the last
    bits of a float64; rounding to a fixed integer scale absorbs it so `determinism_hash` (which
    folds the final ordering) cannot flip between machines.
    """
    return int(round(x * scale))


def canonical_bytes(obj: Any) -> bytes:
    """UTF-8, NO BOM (utf-8 codec never emits a BOM). The canonical serialization for hashing."""
    return canonical_json(obj).encode("utf-8")


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def normalize_text(text: str) -> str:
    """Content normalization for the block-content anchor: NFC, collapse whitespace, strip.

    Extractor-independent and position-independent by design (Op-3-Keystone): survives re-chunk
    because it depends only on the visible text, not on ordinals or char spans.
    """
    t = unicodedata.normalize("NFC", text)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()


def block_content_id(text: str, doc_id: str, occurrence_index: int) -> str:
    """CONTENT anchor folded into edge_id: sha256(normalized_text) + (doc_id, occurrence_index).

    NOT the display block_id (doc#ordinal), which is positional and excluded from identity.
    """
    h = sha256_hex(normalize_text(text))
    return f"{h}:{doc_id}:{occurrence_index}"


def edge_id(subj_node: str, predicate: str, obj_key: str, block_content_id_val: str) -> str:
    """CONTENT-DERIVED edge identity (Op-3-Keystone). doc#ordinal is NEVER folded in.

    Idempotent across re-chunk: same subject/predicate/object asserted by the same content text
    yields the same edge_id, so re-ingest is a no-op, not a forged supersede.
    """
    payload = canonical_json([subj_node, predicate, obj_key, block_content_id_val])
    return "e_" + sha256_hex(payload)[:32]


def determinism_hash(fields: dict[str, Any]) -> str:
    """Fold an answer's numbers/dates/counts/citations/order into one reproducible digest."""
    return sha256_hex(canonical_bytes(fields))


# ---------------------------------------------------------------- RRF fusion (rank-based)

RRF_K_DEFAULT = 60


def rrf_fuse(ranked_lists: dict[str, list[str]], k: int = RRF_K_DEFAULT) -> dict[str, float]:
    """Reciprocal Rank Fusion. rank-based, NO score normalization (blueprint §5 FUSE).

    ranked_lists: channel -> ordered list of ids (best first). Returns id -> fused score.
    """
    scores: dict[str, float] = {}
    for _channel, ids in sorted(ranked_lists.items()):     # sorted for determinism
        for rank, _id in enumerate(ids):
            scores[_id] = scores.get(_id, 0.0) + 1.0 / (k + rank + 1)
    return scores


def stable_rank(hits: Iterable[Any]) -> list[Any]:
    """Deterministic final tie-break: BAND DESC -> AUTHORITY DESC -> page_band DESC -> valid_at
    DESC -> block_id ASC.

    The primary key is the INTEGER band (Fork-A), not the raw float score, so cross-environment
    float noise can never flip the delivered order (and thus never the determinism_hash). `hits`
    expose .band (int; falls back to a banded .fused), .authority, .page_band, .valid_at, .block_id.

    AUTHORITY (C12) is the second key, not the mechanism: because the band quantizes at 1e6 an
    exact band tie is rare, so the key alone would leave a PROVISIONAL block ahead of a SIGNED one
    at every NEAR-tie - that is what the bounded additive prior in `engine.fuse` is for. What this
    key adds is the guarantee at an EXACT tie: when two candidates are indistinguishable on score,
    the attested one is served first rather than whichever block_id happens to sort lower. It
    reads through getattr with a 0 default, so it is a strict no-op on any hit set where nothing
    was marked (every stock vault: `docs.source_authority` defaults to 0) and on duck-typed hits
    that carry no authority at all.
    """
    return sorted(
        hits,
        key=lambda h: (-int(getattr(h, "band", None) if getattr(h, "band", None) is not None
                            else to_band(_num(h.fused))),
                       -int(getattr(h, "authority", 0) or 0),
                       -_num(getattr(h, "page_band", 0)),
                       _neg_str(getattr(h, "valid_at", "")), getattr(h, "block_id", "")),
    )


def _num(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


class _neg_str:
    """Sort a string DESC while keeping ASC-sorted siblings ASC (for valid_at DESC in a tuple key)."""
    __slots__ = ("s",)

    def __init__(self, s: str):
        self.s = s or ""

    def __lt__(self, other: "_neg_str") -> bool:
        return self.s > other.s

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _neg_str) and self.s == other.s


# ---------------------------------------------------------------- Personalized PageRank (deterministic)

def ppr(
    edges: list[tuple[str, str, float]],
    seed: dict[str, float],
    alpha: float = 0.15,
    iters: int = 40,
    epsilon: float = 1e-6,
) -> dict[str, float]:
    """Deterministic Personalized PageRank via power iteration on r <- alpha*s + (1-alpha)*P^T r.

    PRIMARY implementation is pure-python (no deps) with fixed node ordering and float64 semantics.
    `store.adjacency` may substitute a scipy-CSR build behind the same signature when scipy is
    present; that path is ACKNOWLEDGED-DIVERGENT (open sub-decision #4) and never silently swapped.

    edges: (src, dst, weight) already restricted to edges_active_as_of(:T) and non-quarantined.
    seed:  node -> restart mass (will be L1-normalized here defensively).
    """
    nodes = sorted({n for e in edges for n in (e[0], e[1])} | set(seed))
    if not nodes:
        return {}
    idx = {n: i for i, n in enumerate(nodes)}

    # out-weight per source, then row-normalize (P is row-stochastic over out-neighbours)
    out_w: dict[int, float] = {}
    adj: dict[int, list[tuple[int, float]]] = {i: [] for i in range(len(nodes))}
    for src, dst, w in edges:
        si, di = idx[src], idx[dst]
        w = float(w)
        adj[si].append((di, w))
        out_w[si] = out_w.get(si, 0.0) + w

    # seed vector s, L1-normalized
    s = [0.0] * len(nodes)
    total = sum(max(0.0, float(v)) for v in seed.values())
    if total <= 0:
        for i in range(len(nodes)):
            s[i] = 1.0 / len(nodes)      # uniform restart if no usable seed
    else:
        for n, v in seed.items():
            s[idx[n]] = max(0.0, float(v)) / total

    r = list(s)
    for _ in range(iters):
        nxt = [alpha * s[i] for i in range(len(nodes))]
        dangling = 0.0
        for i in range(len(nodes)):
            ow = out_w.get(i, 0.0)
            if ow <= 0.0:
                dangling += r[i]
                continue
            share = (1.0 - alpha) * r[i] / ow
            for j, w in adj[i]:
                nxt[j] += share * w
        if dangling > 0.0:                # redistribute dangling mass over seed (personalized)
            for i in range(len(nodes)):
                nxt[i] += (1.0 - alpha) * dangling * s[i]
        delta = sum(abs(nxt[i] - r[i]) for i in range(len(nodes)))
        r = nxt
        if delta < epsilon:
            break
    return {nodes[i]: r[i] for i in range(len(nodes))}
