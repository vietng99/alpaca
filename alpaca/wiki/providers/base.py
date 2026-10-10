"""Provider protocols - the seams between the engine and its deterministic providers.

Rune2 ships only deterministic, dependency-free providers (providers/deterministic.py): a hash
embedder, identity and lexical rerankers, structural, ensemble and routed entailers and a null
extractor. The registry (providers/registry.py) is closed to those names, so no model, embedder or
reranker plugs in (design/rune2-memory/architecture.md 3.2). The engine is blind to which of them is
behind the seam; configurations swap them by name in project.yaml or rune.toml.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Embedder(Protocol):
    dim: int
    name: str
    def embed(self, text: str) -> list[float]: ...


@runtime_checkable
class Reranker(Protocol):
    name: str
    def rerank(self, query: str, blocks: list[tuple[str, str]]) -> dict[str, float]:
        """blocks: [(block_id, text)] -> {block_id: rerank_score}. Higher = more relevant."""
        ...


@runtime_checkable
class Entailer(Protocol):
    name: str
    def entail(self, claim: str, source_text: str) -> tuple[str, float]:
        """Return (label, score) where label in {'supported','unsupported','contradicted'}.

        The deterministic ensemble abstains on checker disagreement; no model judge exists.
        """
        ...


@runtime_checkable
class LLMExtractor(Protocol):
    name: str
    def extract(self, block_text: str, context: str) -> list[dict]:
        """Fallback typed-edge extraction where deterministic yields nothing. Output is quarantined."""
        ...
