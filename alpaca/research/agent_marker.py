"""The marker a subagent's shell carries, and the one rule that reads it.

No environment variable tells a subagent's shell from the driver's: `CLAUDE_CODE_CHILD_SESSION` is
set in both and the two environments are equal variable for variable (in-session-runtime.md
section 3.4, live-round/env-measure.txt). The PreToolUse payload of a subagent's tool call does
carry `agent_id`, and a PreToolUse hook may return `updatedInput` for a Bash call. So the guard
(alpaca/hooks/research_guard.py) puts `export ALPACA_RESEARCH_AGENT=<agent id>;` in front of the
command of every Bash call of a subagent, and the verbs that drive a run (`start`, `resume`,
`extend`, `stop`, `next-job`, `submit`, `proof verify`) refuse with RUN-DRIVER-IS-SUBAGENT when the
variable is set in their environment. The driver's calls carry no `agent_id`, so its shell never
has the variable.

This module imports nothing but the standard library: the guard loads it on every tool call of a
subagent and must stay fast. A command that names the variable is denied by the guard (a subagent
that unsets or overrides the marker), which is what keeps the marker honest.
"""
from __future__ import annotations

import os
import re

#: the environment variable that marks a subagent's shell
ENV = "ALPACA_RESEARCH_AGENT"
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")
_PREFIX = re.compile(r"^export %s=[A-Za-z0-9_.-]*; " % ENV)


def prefix_for(agent_id) -> str:
    """The text put in front of a subagent's command."""
    return "export %s=%s; " % (ENV, _SAFE.sub("-", str(agent_id)) or "-")


def mark(command: str, agent_id) -> str:
    """The command with the marker in front of it."""
    return prefix_for(agent_id) + command


def unmark(command):
    """The command without the marker prefix this module puts in front, when it is there."""
    if isinstance(command, str):
        return _PREFIX.sub("", command, count=1)
    return command


def names_marker(text) -> bool:
    """True when a command text names the variable at all (a subagent that unsets or sets it)."""
    return isinstance(text, str) and ENV in text


def current(environ=None):
    """The agent id the environment carries, or None."""
    value = (environ if environ is not None else os.environ).get(ENV)
    return value or None


def refusal_reason(verb: str, agent_id) -> str:
    return ("%s is set in this shell (agent %s): only the driving session runs `%s`, a subagent "
            "never does" % (ENV, agent_id, verb))
