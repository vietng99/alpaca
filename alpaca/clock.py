"""Injectable clock so the record is deterministic under test (AG-M21).

Real runs stamp wall-clock local time with its offset; tests inject a FixedClock so the
hash-chain ts and every rendered projection are reproducible. Time is a stamp source, never a
branch in logic: nothing here reads the clock to decide anything, it only stamps.

The seam is deliberately small. `util.set_clock` installs a Clock process-wide so `util.now_iso`
(and therefore the pad renderer, the analytics fold and the export, which all go through it)
picks it up; `db.append_event(..., clock=...)` is the explicit per-write seam for the record
writer. `FixedClock` is the test seam for both.
"""
from datetime import datetime, timezone, timedelta
from typing import Callable

# A Clock is any zero-arg callable returning an ISO-8601 string with an offset.
Clock = Callable[[], str]


def system_now_iso() -> str:
    """Wall-clock local time to whole seconds, with the offset (spec: ISO-8601 with offset)."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


class SystemClock:
    """The default clock: wall-clock local time. Stateless, safe to share."""

    def __call__(self) -> str:
        return system_now_iso()


class FixedClock:
    """Returns a fixed ISO timestamp; advances by `step` seconds each call when step != 0.

    The start string must carry an offset (e.g. "2026-01-01T00:00:00+00:00") so the stamp it
    returns is offset-bearing like the real clock. Whole-second resolution matches SystemClock.
    """

    def __init__(self, start: str = "2026-01-01T00:00:00+00:00", step: int = 0):
        t = datetime.fromisoformat(start)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        self._t = t.replace(microsecond=0)
        self._step = int(step)

    def __call__(self) -> str:
        out = self._t.isoformat(timespec="seconds")
        if self._step:
            self._t = self._t + timedelta(seconds=self._step)
        return out
