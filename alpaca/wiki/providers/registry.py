"""Resolve provider instances by config name - the seam swap point."""
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


def get_embedder(cfg: Config):
    dim = int(cfg.meta.get("embed_dim", 64))
    if cfg.embedder in ("deterministic", "deterministic-hash", "hash"):
        return HashEmbedder(dim)
    # real embedders (sentence-transformers, etc.) register here
    raise ValueError(f"unknown embedder '{cfg.embedder}' - register it in providers.registry")


def get_reranker(cfg: Config):
    if cfg.reranker == "identity":
        return IdentityReranker()
    if cfg.reranker in ("lexical", "lexical-overlap"):
        return LexicalReranker()
    raise ValueError(f"unknown reranker '{cfg.reranker}'")


def get_entailer(cfg: Config):
    if cfg.entailer in ("structural", "structural-only"):
        return StructuralEntailer()
    if cfg.entailer in ("ensemble", "ensemble-3"):
        return EnsembleEntailer()                    # oracle-safety.1: >=3 checkers, abstain on split
    if cfg.entailer in ("routed", "vn-routed"):
        return RoutedEntailer()                      # oracle-safety.7: VN/EN language-routed lanes
    raise ValueError(f"unknown entailer '{cfg.entailer}' - the HHEM/MiniCheck/Paladin ensemble plugs in here")


def get_llm_extractor(cfg: Config):
    if cfg.llm_extractor in ("off", "none", ""):
        return NullLLMExtractor()
    raise ValueError(f"unknown llm_extractor '{cfg.llm_extractor}'")


class Providers:
    """Bundle resolved once and threaded through ingest + engine."""
    def __init__(self, cfg: Config):
        self.embedder = get_embedder(cfg)
        self.reranker = get_reranker(cfg)
        self.entailer = get_entailer(cfg)
        self.llm_extractor = get_llm_extractor(cfg)
