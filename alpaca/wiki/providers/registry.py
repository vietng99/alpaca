"""Resolve provider instances by config name - the seam swap point.

The registry is CLOSED (design/rune2-memory/architecture.md 3.2): only the deterministic names in
CLOSED_REGISTRY resolve. Any other name is refused when configured (Config.for_project resolves the
bundle), and the message names the capability as unsupported: Rune2 has no model embedder, no
cross-encoder reranker, no model entailer and no model extractor, and makes no model call.
"""
from __future__ import annotations

from ..config import Config
from .deterministic import (
    EnsembleEntailer,
    HashEmbedder,
    IdentityReranker,
    LexicalReranker,
    NullLLMExtractor,
    RoutedEntailer,
    StructuralEntailer,
)

# The only names that resolve, per seam (interfaces.py.txt section 5).
CLOSED_REGISTRY: dict[str, tuple[str, ...]] = {
    "embedder": ("deterministic", "deterministic-hash", "hash"),
    "reranker": ("identity", "lexical", "lexical-overlap"),
    "entailer": ("structural", "structural-only", "ensemble", "ensemble-3", "routed", "vn-routed"),
    "llm_extractor": ("off", "none", ""),
}


def _unsupported(seam: str, name: str) -> ValueError:
    allowed = ", ".join(repr(n) for n in CLOSED_REGISTRY[seam])
    return ValueError(
        f"unknown {seam} '{name}': unsupported capability. Rune2 resolves only the deterministic "
        f"{seam} names {allowed}; no model provider, embedder or reranker is supported")


def get_embedder(cfg: Config):
    dim = int(cfg.meta.get("embed_dim", 64))
    if cfg.embedder in CLOSED_REGISTRY["embedder"]:
        return HashEmbedder(dim)
    raise _unsupported("embedder", cfg.embedder)


def get_reranker(cfg: Config):
    if cfg.reranker == "identity":
        return IdentityReranker()
    if cfg.reranker in ("lexical", "lexical-overlap"):
        return LexicalReranker()
    raise _unsupported("reranker", cfg.reranker)


def get_entailer(cfg: Config):
    if cfg.entailer in ("structural", "structural-only"):
        return StructuralEntailer()
    if cfg.entailer in ("ensemble", "ensemble-3"):
        return EnsembleEntailer()                    # oracle-safety.1: >=3 checkers, abstain on split
    if cfg.entailer in ("routed", "vn-routed"):
        return RoutedEntailer()                      # oracle-safety.7: VN/EN language-routed lanes
    raise _unsupported("entailer", cfg.entailer)


def get_llm_extractor(cfg: Config):
    if cfg.llm_extractor in CLOSED_REGISTRY["llm_extractor"]:
        return NullLLMExtractor()
    raise _unsupported("llm_extractor", cfg.llm_extractor)


class Providers:
    """Bundle resolved once and threaded through ingest + engine."""
    def __init__(self, cfg: Config):
        self.embedder = get_embedder(cfg)
        self.reranker = get_reranker(cfg)
        self.entailer = get_entailer(cfg)
        self.llm_extractor = get_llm_extractor(cfg)
