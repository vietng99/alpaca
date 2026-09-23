"""Rune-2 - a provenance+time-first bitemporal assertion-log GraphRAG memory store.

Shareable, ownership-neutral ENGINE TEMPLATE (D-2). One door (`engine.answer`), two writers
(`ingest.absorb`, `reflect`). Single-file SQLite, pure-stdlib runnable, real models behind seams.
"""
from .config import Config

__all__ = ["Config", "__version__"]
__version__ = "0.1.0"
