"""Kill points of the Rune2 write paths, for tests only (t-070 round 2 C-E; made inert in round 2 C-D).

A boundary sits where a crash leaves the store between two durable writes: before the drain lands a
document, after a document's vault bytes and before its store commit, where the replay starts a
record act, after a live door appended a removal act and before its store commit, and where an
import lands a row. `alpaca/tests/wiki/test_kill_states.py` kills a rebuild, an import and a capture
child at these boundaries and then asks every serving surface what it serves.

`boundary` acts only when the process holds an arm in `_ARM`. `_ARM` is None in every process
Alpaca starts, and no module of alpaca outside alpaca/tests sets it: the arm, its count, its log and
its SIGKILL live in `alpaca/tests/wiki/kill_arm.py`, which a test calls inside the process it kills.
This module imports nothing and reads no environment variable, record event, project file or hook
payload, so nothing outside a test can make it act
(`test_kill_states.py::test_the_kill_hook_is_inert_unless_a_test_arms_it`).
"""
from __future__ import annotations

#: The arm of this process: None, or the object a test put here, whose `passed(name)` is called at
#: each boundary.
_ARM = None


def boundary(name: str) -> None:
    """Tell this process's test arm, if it has one, that the write path passed `name`."""
    arm = _ARM
    if arm is not None:
        arm.passed(name)
