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


def to_clock_format(value) -> str | None:
    """A source time (an ISO-8601 string with any offset, or `Z`) in the store clock's format:
    UTC, whole seconds, `+00:00`. Valid time is compared as text against T and K, which are in that
    format, so a record `ts` kept as written (`+07:00`, fractions, `Z`) would order wrongly. None
    when the value is not a readable time: an unknown time stays null (architecture.md 5.1)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"      # fromisoformat reads a Z only from Python 3.11 on (op-008 t-571)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()
