"""Machine-wide locks: one per heavy gate stage, and the concurrency-tier semaphore (section 4.7).

Design: design/research-engine/architecture.md sections 3.3 and 4.7 (FR-026, EC-055). The lock
files live in paths.machine_lock_dir(), a locks/ folder beside the host's workspace registry, so
every project and every slot on one host shares them. Each file is taken with the unchanged
resilience.worker_launch.ReentryLock (an flock the OS frees when its holder dies), non-blocking,
and polled until the timeout: no blocking acquire is added to alpaca/resilience/ (section 13.2).
A lock of capacity n is n files, research-<name>-<slot>.lock for slot 0..n-1; a holder takes the
first free one.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
import os
import time

from alpaca.research import paths
from alpaca.research.errors import LockTimeout


@dataclass(frozen=True)
class LockHold:
    """One held slot of a machine lock and how long the holder waited for it."""
    name: str
    slot: int
    waited_s: float


def _lock_file(name: str, slot: int) -> str:
    return os.path.join(paths.machine_lock_dir(), "research-%s-%d.lock" % (name, slot))


@contextlib.contextmanager
def machine_lock(name: str, *, capacity: int = 1, timeout_s: float, poll_s: float = 0.5):
    """A counting lock over `capacity` lock files in paths.machine_lock_dir(), each taken with
    resilience.worker_launch.ReentryLock (alpaca/resilience/worker_launch.py:51-89) non-blocking
    and polled until timeout_s, which for a heavy gate stage is its GateStage.lock_timeout_s
    (research.gates[].lock_timeout). Yields a LockHold(name, slot, waited_s), a small dataclass
    local to locks.py (no other module names it, so it is not in the shared types of section 12.2).
    Raises LockTimeout."""
    from alpaca.resilience.worker_launch import LockHeldError, ReentryLock

    paths.check_name(name)
    capacity = int(capacity)
    if capacity < 1:
        raise ValueError("a machine lock needs a capacity of at least 1, not %d" % capacity)
    os.makedirs(paths.machine_lock_dir(), exist_ok=True)
    start = time.monotonic()
    deadline = start + max(0.0, float(timeout_s))
    held = None
    slot = -1
    while held is None:
        for n in range(capacity):
            lock = ReentryLock(_lock_file(name, n))
            try:
                lock.acquire()
            except LockHeldError:
                continue
            held, slot = lock, n
            break
        if held is not None:
            break
        now = time.monotonic()
        if now >= deadline:
            raise LockTimeout("machine lock %s (capacity %d) not taken within %ss"
                              % (name, capacity, timeout_s))
        time.sleep(max(0.01, min(float(poll_s), deadline - now)))
    try:
        yield LockHold(name=name, slot=slot, waited_s=round(time.monotonic() - start, 3))
    finally:
        held.release()


@contextlib.contextmanager
def heavy(stage: str, root: str, *, timeout_s: float):
    """The stage lock `stage-<paths.stage_slug(stage)>` (capacity 1), then one slot of `heavy`,
    whose capacity is the project's concurrency tier (resilience.budget.concurrency_limit,
    alpaca/resilience/budget.py:118-129). Always in that order.

    Yields the two LockHold values (stage lock, heavy slot); the whole wait is the sum of their
    waited_s. The heavy slot gets what is left of timeout_s after the stage lock's wait."""
    from alpaca.resilience import budget

    try:
        tier = int(budget.concurrency_limit(root))
    except Exception:          # an unreadable project.yaml: the smallest tier, never a wider one
        tier = 1
    start = time.monotonic()
    with machine_lock("stage-%s" % paths.stage_slug(stage), capacity=1,
                      timeout_s=timeout_s) as stage_hold:
        left = max(0.0, float(timeout_s) - (time.monotonic() - start))
        with machine_lock("heavy", capacity=max(1, tier), timeout_s=left) as heavy_hold:
            yield stage_hold, heavy_hold
