"""Neutral surface-normalization helper.

Lives outside the guarded `store.read` retrieval surface so ingest (resolve) and retrieval (read)
can both use it without a module becoming a second retrieval door (R1).
"""
from __future__ import annotations

import re

_NORM = re.compile(r"[^\w]+", re.UNICODE)


def norm_surface(s: str) -> str:
    return _NORM.sub(" ", s.lower()).strip()


def surface_in_question(norm: str, question_norm: str, kind: str | None = None) -> bool:
    """Whether an alias surface (already normalised) is named in a normalised question.

    A `declared` alias is an exact identifier (a task, op, decision or session id, architecture.md
    8.1), so it matches only as whole tokens: `t-061` is named by "t-061" and never inside "t-0612".
    Other kinds keep the older test, the surface as a substring or as one token of the question."""
    if not norm:
        return False
    if kind == "declared":
        return f" {norm} " in f" {question_norm} "
    return norm in question_norm or norm in set(question_norm.split())
