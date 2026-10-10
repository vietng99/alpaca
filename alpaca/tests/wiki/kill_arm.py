"""The arming side of the Rune2 kill points (`alpaca/wiki/faults.py`), for tests only (t-070 round 2
C-D: the kill hook is inert in product code).

`faults.boundary(name)` does nothing unless the process holds an arm in `faults._ARM`. Only this
module puts one there, and only in the process that calls `arm` (an arm is not inherited by a child
process and is read from no environment variable, record event, project file or hook payload). A
test that kills another process arms it from inside that process:

- a `python -c` body starts with `prelude(at, log)`;
- the capture runner's real child is given the target `capture` of this module
  (`alpaca.tests.wiki.kill_arm:capture`), which arms the child and then runs `service.capture`.

With `log`, each boundary passed appends `<n> <name>` to that file (a dry run counts them); with `at`,
the process sends itself SIGKILL at the n-th boundary.
"""
from __future__ import annotations

import os
import signal


class Arm:
    """Counts the boundaries a process passes, logs them, and kills the process at one."""

    def __init__(self, at=None, log=None):
        self.at = None if at is None else int(at)
        self.log = None if log is None else str(log)
        self.count = 0

    def passed(self, name: str) -> None:
        self.count += 1
        if self.log:
            with open(self.log, "a", encoding="utf-8") as stream:
                stream.write("%d %s\n" % (self.count, name))
        if self.at is not None and self.count == self.at:
            os.kill(os.getpid(), signal.SIGKILL)


def arm(at=None, log=None) -> Arm:
    """Arm this process: kill it at boundary `at`, log every boundary to `log`."""
    from alpaca.wiki import faults
    faults._ARM = Arm(at, log)
    return faults._ARM


def disarm() -> None:
    from alpaca.wiki import faults
    faults._ARM = None


def prelude(at=None, log=None) -> str:
    """Python source that arms the process it runs in (the first lines of a `python -c` body)."""
    return ("from alpaca.tests.wiki import kill_arm\n"
            "kill_arm.arm(at=%r, log=%r)\n" % (at, None if log is None else str(log)))


def capture(root, *, kill_at=None, kill_log=None, **kwargs):
    """The capture child's work, armed: `service.capture(root, **kwargs)` after `arm`."""
    arm(at=kill_at, log=kill_log)
    from alpaca.wiki import service
    return service.capture(root, **kwargs)
