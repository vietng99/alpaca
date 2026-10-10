"""engine.answer - THE ONLY retrieval door (R1).

`answer(cfg, question, as_of)` is the single public entry point. /query, inline fan-out, Workflows,
and sub-agents all call this exact function; there is no second door. Store read primitives are
import-guarded to `engine.retrieve` and asserted by tests/test_r1_import_guard.py.
"""
from __future__ import annotations

from ..api import Filters
from ..config import Config
from ..types import Answer
from .loop import Engine


def answer(cfg: Config, question: str, as_of: str | None = None,
           knowledge_as_of: str | None = None, wiki_blind: bool = False,
           after=None, filters: Filters | None = None, pack: bool = False) -> Answer:
    """`after(db, answer)`, when given, runs on the door's own store connection after the answer
    and its audit rows are written and before the connection closes; the caller interface uses it
    to record the answer's outcome (alpaca/wiki/service.py) without opening the store twice.
    `filters` are the FR-11 structured filters (store/filters.py); None means the defaults.
    `pack` marks a context pack: it keeps every claim block of a linked identifier eligible (retrieve.ENTITY_FLOOR_CAP)."""
    eng = Engine(cfg)
    try:
        ans = eng.answer(
            question, as_of, knowledge_as_of=knowledge_as_of, wiki_blind=wiki_blind,
            filters=filters, pack=pack,
        )
        if after is not None:
            after(eng.db, ans)
        return ans
    finally:
        eng.close()


__all__ = ["answer", "Engine"]
