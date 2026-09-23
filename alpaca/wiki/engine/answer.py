"""engine.answer - THE ONLY retrieval door (R1).

`answer(cfg, question, as_of)` is the single public entry point. /query, inline fan-out, Workflows,
and sub-agents all call this exact function; there is no second door. Store read primitives are
import-guarded to `engine.retrieve` and asserted by tests/test_r1_import_guard.py.
"""
from __future__ import annotations

from ..config import Config
from ..types import Answer
from .loop import Engine


def answer(cfg: Config, question: str, as_of: str | None = None,
           knowledge_as_of: str | None = None, wiki_blind: bool = False) -> Answer:
    eng = Engine(cfg)
    try:
        return eng.answer(
            question, as_of, knowledge_as_of=knowledge_as_of, wiki_blind=wiki_blind,
        )
    finally:
        eng.close()


__all__ = ["answer", "Engine"]
