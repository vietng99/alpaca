"""Provider protocols - the seams where real CPU models plug in.

The template ships deterministic, dependency-free defaults (providers/deterministic.py) so it runs
and its tests pass with pure stdlib. A populated instance swaps these for real models by name in
rune.toml - the rest of the engine is blind to which implementation is behind the seam.
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

        A real deployment routes numeric/temporal/logical claims to Paladin, prose to HHEM/MiniCheck,
        and ABSTAINS on ensemble disagreement - never a lone LLM judge.
        """
        ...


@runtime_checkable
class LLMExtractor(Protocol):
    name: str
    def extract(self, block_text: str, context: str) -> list[dict]:
        """Fallback typed-edge extraction where deterministic yields nothing. Output is quarantined."""
        ...
