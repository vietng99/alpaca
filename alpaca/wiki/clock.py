"""Injectable clock so writes are deterministic under test.

Real runs use wall-clock UTC; tests inject a fixed or monotonic clock so bitemporal stamps and
the hash-chain are reproducible. Time is a *stamp source*, never a source of logic branching.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class FixedClock:
    """Returns a fixed ISO timestamp; optionally advances by `step` seconds each call."""

    def __init__(self, start: str = "2026-01-01T00:00:00+00:00", step: int = 0):
        self._t = datetime.fromisoformat(start)
        self._step = step

    def __call__(self) -> str:
        out = self._t.replace(microsecond=0).isoformat()
        if self._step:
            from datetime import timedelta
            self._t = self._t + timedelta(seconds=self._step)
        return out


Clock = Callable[[], str]
