"""The one place the op-019 gates meet the task verbs and the phase doors.

Design: design/intent-fidelity/design.md sections 3.3, 4, 5.3 and 6. Each gate lives in its own
module (`alpaca.batch`, `alpaca.orders`, `alpaca.oob`, `alpaca.selfcheck`); this module only asks them, in a
fixed order, so `alpaca/ops.py` and `alpaca/phase/doors.py` each carry one call and the gates can
be built and changed apart.

A provider module may define:

  * `task_gate(conn, task, action, *, level, session, root)`: raise `IntentGateRefusal` to refuse
    `task claim` (action "claim") or `task move` to doing, review or done (action "doing",
    "review", "done") of `task` (a tasks row as a dict). Return None to allow.
  * `door_link(conn, op, boundary, *, level, session, root)`: return a (name, verdict code) pair
    for the phase door, or None when the module has nothing to say about this door.
  * `owner_items(conn, op, *, root)`: return a list of the owner's answers, orders or decisions
    for the op as plain dicts, for the self-check packet (design section 5.1).

The level every gate reads is `level(conn, session)`: the highest level in force over the session
the call names and every session the environment named before the CLI set ALPACA_SESSION_ID, so
`--session cli` or a made-up id inside an agent session does not lower it (design section 3.3).
"""
from __future__ import annotations

#: the provider modules, asked in this order. A module that is not there (not built yet) is skipped.
PROVIDERS = ("alpaca.batch", "alpaca.orders", "alpaca.oob", "alpaca.selfcheck")

#: the task actions the gates see. `task move` to open or blocked is never gated.
ACTIONS = ("claim", "doing", "review", "done")


class IntentGateRefusal(ValueError):
    """A refused task action: `.reason` is the reason token, the message names what is missing."""

    def __init__(self, reason, detail):
        super().__init__("%s: %s" % (reason, detail))
        self.reason = reason
        self.detail = detail


def _providers():
    """The provider modules that are built, in PROVIDERS order. Imported by literal name: an engine
    file never loads a module by a computed name (alpaca/tests/test_research_session_engine.py)."""
    out = []
    try:
        from alpaca import batch
        out.append(batch)
    except ImportError:
        pass
    try:
        from alpaca import orders
        out.append(orders)
    except ImportError:
        pass
    try:
        from alpaca import oob
        out.append(oob)
    except ImportError:
        pass
    try:
        from alpaca import selfcheck
        out.append(selfcheck)
    except ImportError:
        pass
    return out


def level(conn, session, *, root=None) -> int:
    """The highest level in force over the named session and the environment's sessions."""
    from alpaca import ownerword
    from alpaca.posture import level as posture_level
    seen, best = set(), 0
    for sid in [session] + ownerword.real_sessions():
        if not sid or sid in seen:
            continue
        seen.add(sid)
        try:
            best = max(best, posture_level.in_force(conn, sid, root=root))
        except Exception:
            continue
    if not best:
        best = posture_level.default_level(root)
    return best


def task_gate(conn, task, action, *, session, root=None):
    """Ask every provider about one task action; the first refusal is raised as IntentGateRefusal."""
    if action not in ACTIONS:
        return
    n = level(conn, session, root=root)
    for mod in _providers():
        fn = getattr(mod, "task_gate", None)
        if fn is not None:
            fn(conn, dict(task), action, level=n, session=session, root=root)


def door_links(conn, op, boundary, *, level_flag=None, session=None, root=None) -> list:
    """The (name, verdict code) links the providers add to one phase door. The level is the higher of
    the door's `--level` flag and `level(conn, session)`, so a lower flag cannot skip a link."""
    from alpaca.phase import defaults
    n = level(conn, session, root=root)
    if level_flag is not None:
        try:
            n = max(n, defaults.level_num(level_flag))
        except ValueError:
            pass
    out = []
    for mod in _providers():
        fn = getattr(mod, "door_link", None)
        if fn is None:
            continue
        link = fn(conn, op, boundary, level=n, session=session, root=root)
        if link is not None:
            out.append(link)
    return out


def owner_items(conn, op, *, root=None) -> list:
    """The owner's answers, orders and decisions for `op` from every provider (self-check packet)."""
    out = []
    for mod in _providers():
        fn = getattr(mod, "owner_items", None)
        if fn is not None:
            out.extend(fn(conn, op, root=root) or [])
    return out
