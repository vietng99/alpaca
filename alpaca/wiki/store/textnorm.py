"""Neutral surface-normalization helper.

Lives outside the guarded `store.read` retrieval surface so ingest (resolve) and retrieval (read)
can both use it without a module becoming a second retrieval door (R1).
"""
from __future__ import annotations

import re

_NORM = re.compile(r"[^\w]+", re.UNICODE)


def norm_surface(s: str) -> str:
    return _NORM.sub(" ", s.lower()).strip()
