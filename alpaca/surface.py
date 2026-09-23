"""alpaca.surface: the operating-surface verb budget and the orphan-verb lint (M4.12).

Sources: D9 (spec:158, the CLI is the workspace), spec 5.12:586-592, Q11 (spec:177); absorb-gap
AG-M29 (a small front verb set as a tested dispatch contract with declared output keys).

The operating surface is the set of verbs `alpaca` dispatches. This module makes that surface a tested
contract in three pieces:

  * ``FRONT`` is a small, budgeted front set - the everyday verbs a human reaches for first.
    ``FRONT_BUDGET`` is the ceiling; a surface that grows its front page past the budget stops
    being a front page. FRONT is a subset of the whole surface, never the whole of it.
  * ``table()`` declares every dispatched verb with its output keys and its group. A caller and the
    HTML page can rely on the declared shape instead of reading the code to guess it.
  * ``lint(root)`` is the orphan-verb lint. It cross-checks three sources - the dispatch table the
    CLI actually registers (``cli.registered_verbs``), this module's declared ``TABLE``, and the
    ``MANUAL.md`` verb chapter under ``root`` - and returns one finding per verb that is
    registered-but-undeclared, declares no output keys, is documented nowhere, or is declared here
    yet never dispatched, plus a finding when FRONT runs over budget. An empty list is a clean
    surface. boot-check (M4.15) and the manual gate (M4.16) turn the findings into a verdict; the
    lint itself is read-only and writes nothing to the record.

A verb beyond the front budget is still reachable: it dispatches like any other, it is just not on
the front page. ``reachable`` and ``is_front`` name that distinction for a caller.
"""
from __future__ import annotations

import os

#: The front verb budget. The front page is kept small on purpose; a surface whose FRONT grows past
#: this ceiling fails the lint. The rest of the verbs stay grouped behind the front page.
FRONT_BUDGET = 12

#: The small front verb set: the everyday operating verbs (daily use, board, decisions, questions,
#: phase, doctor). Everything else is reachable but grouped behind this.
FRONT = (
    "status",
    "op",
    "task",
    "board",
    "msg",
    "decide",
    "ask",
    "answer",
    "phase",
    "doctor",
)

#: The declared dispatch table: every registered verb, its output keys (the shape a caller and the
#: page can rely on), and the group it sits in. Groups other than "front" are reachable but live
#: behind the front page. Keep this in step with what the CLI registers; the lint fails when they
#: disagree in either direction.
TABLE: dict[str, dict] = {
    "session": {"keys": ("session", "operator", "context"), "group": "front"},
    # front page (FRONT above marks these front; the group label mirrors that)
    "status": {"keys": ("root", "version", "events", "last_event", "onboarded",
                         "sessions", "ops_open", "tasks_open", "op_index"), "group": "front"},
    "op": {"keys": ("verdict", "op", "status"), "group": "front"},
    "task": {"keys": ("verdict", "task", "status"), "group": "front"},
    "board": {"keys": ("verdict", "board", "rows"), "group": "front"},
    "msg": {"keys": ("verdict", "message"), "group": "front"},
    "decide": {"keys": ("verdict", "decision"), "group": "front"},
    "ask": {"keys": ("verdict", "question"), "group": "front"},
    "answer": {"keys": ("verdict", "question", "answer"), "group": "front"},
    "phase": {"keys": ("verdict", "op", "phase", "level"), "group": "front"},
    "doctor": {"keys": ("verdict", "findings"), "group": "front"},
    # setup and maintenance
    "init": {"keys": ("verdict", "root"), "group": "setup"},
    "onboard": {"keys": ("verdict", "project"), "group": "setup"},
    "upgrade": {"keys": ("verdict", "plan", "applied"), "group": "setup"},
    "spec": {"keys": ("verdict", "kit", "version", "files"), "group": "setup"},
    "collect": {"keys": ("status", "collector", "sources", "coverage", "consumers"), "group": "maintenance"},
    "artifact": {"keys": ("availability", "objects", "bytes", "pins"), "group": "maintenance"},
    "backup": {"keys": ("ok", "watermark", "completeness", "known_gaps"), "group": "maintenance"},
    "workspace": {"keys": ("registry", "workspace", "workspaces"), "group": "maintenance"},
    "verify": {"keys": ("verdict", "reason"), "group": "maintenance"},
    "retention": {"keys": ("verdict", "kept", "dropped"), "group": "maintenance"},
    "apply": {"keys": ("verdict", "change"), "group": "maintenance"},
    "token": {"keys": ("verdict", "token"), "group": "maintenance"},
    # record and coordination
    "day": {"keys": ("date", "totals", "ops", "unsorted"), "group": "record"},
    "proof": {"keys": ("verdict", "path", "sha256", "sections", "evidence"), "group": "record"},
    "review": {"keys": ("verdict", "card", "rows"), "group": "record"},
    "barrier": {"keys": ("verdict", "tier", "allowed"), "group": "record"},
    # specs and runbooks
    "runbook": {"keys": ("verdict", "runbook", "spec", "errors", "warnings", "coverage"), "group": "spec"},
    "intake": {"keys": ("verdict", "op", "spec", "runbook", "rows", "tasks", "profile", "dry_run"),
               "group": "spec"},
    "start": {"keys": ("verdict", "kit", "mode", "reason", "steps", "prepared"), "group": "spec"},
    # knowledge and output
    "sort": {"keys": ("verdict", "sorted", "unsorted"), "group": "knowledge"},
    "wiki": {"keys": ("verdict", "vault", "out"), "group": "knowledge"},
    "export": {"keys": ("verdict", "out"), "group": "output"},
    "deploy": {"keys": ("verdict", "target"), "group": "output"},
    "serve": {"keys": ("verdict", "url"), "group": "output"},
    "analytics": {"keys": ("verdict", "index"), "group": "output"},
    "recall": {"keys": ("verdict", "session"), "group": "output"},
    # style
    "style": {"keys": ("verdict", "term", "preset", "findings"), "group": "style"},
}


def declared(root: str | None = None) -> dict[str, dict]:
    """TABLE plus the verbs a domain profile declares (alpaca/profile.py `verbs`), verb ->
    {"keys": tuple, "group": str}. The profile of `root`, else of the current project."""
    from alpaca import profile
    prof = profile.load(root) if root else profile.current()
    out = {v: dict(spec) for v, spec in TABLE.items()}
    try:
        extra = prof.verbs() or {}
    except Exception:
        extra = {}
    for verb, spec in extra.items():
        if isinstance(spec, dict) and verb not in out:
            keys = spec.get("keys") if isinstance(spec.get("keys"), (list, tuple)) else ()
            out[str(verb)] = {"keys": tuple(str(k) for k in keys), "group": str(spec.get("group") or "profile")}
    return out


def table() -> dict[str, dict]:
    """The declared dispatch table, verb -> {"keys": tuple, "group": str}, profile verbs
    included. A copy, so a caller cannot mutate the module's declaration by accident."""
    return declared()


def output_keys(verb: str) -> tuple:
    """The declared output keys for a verb, or an empty tuple when the verb is not declared."""
    spec = declared().get(verb)
    return tuple(spec["keys"]) if spec else ()


def is_front(verb: str) -> bool:
    """True when the verb is on the front page."""
    return verb in FRONT


def reachable(verb: str) -> bool:
    """True when the verb is in the live dispatch table (registered), front-page or not."""
    from alpaca import cli
    return verb in cli.registered_verbs()


# --------------------------------------------------------------------------- documentation read
def _manual_path(root: str) -> str:
    return os.path.join(root, "MANUAL.md")


def documented(root: str) -> set:
    """The set of verbs the MANUAL verb chapter under ``root`` documents.

    A verb is documented when its command token ```alpaca <verb>``` appears in MANUAL.md. A
    missing MANUAL yields the empty set (every verb then reads as undocumented), never an error.
    """
    path = _manual_path(root)
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return set()
    found = set()
    for verb in declared(root):
        if ("`alpaca %s`" % verb) in text:
            found.add(verb)
    return found


# --------------------------------------------------------------------------- the lint
def lint(root: str) -> list:
    """The orphan-verb lint. Returns a list of findings; an empty list is a clean surface.

    Each finding is ``{"verb": <name-or-None>, "reason": <str>, "detail": <str>}``. Reasons:

      * ``over-budget``   - FRONT has more verbs than FRONT_BUDGET (verb is None).
      * ``front-unregistered`` - a FRONT verb the CLI does not dispatch.
      * ``front-undeclared``   - a FRONT verb missing from TABLE.
      * ``undeclared``    - a dispatched verb with no TABLE entry (an orphan verb).
      * ``no-output-keys``- a declared verb whose output keys are empty.
      * ``undocumented``  - a dispatched, declared verb the MANUAL does not name.
      * ``unregistered``  - a verb declared in TABLE that the CLI never dispatches (a phantom).

    The lint is read-only: it reads the code's dispatch table and MANUAL.md bytes and writes
    nothing.
    """
    from alpaca import cli

    findings: list = []

    def add(verb, reason, detail=""):
        findings.append({"verb": verb, "reason": reason, "detail": detail})

    registered = cli.registered_verbs()
    spec_of = declared(root)
    declared_verbs = set(spec_of)
    docs = documented(root)

    # 1) the front page stays within budget.
    if len(FRONT) > FRONT_BUDGET:
        add(None, "over-budget",
            "FRONT has %d verbs, budget is %d" % (len(FRONT), FRONT_BUDGET))

    # 2) every FRONT verb is declared and registered.
    for verb in FRONT:
        if verb not in declared_verbs:
            add(verb, "front-undeclared", "FRONT names a verb not in the table")
        if verb not in registered:
            add(verb, "front-unregistered", "FRONT names a verb the CLI does not dispatch")

    # 3) every dispatched verb is declared, keyed and documented.
    for verb in sorted(registered):
        if verb not in declared_verbs:
            add(verb, "undeclared", "dispatched but not declared in the surface table")
            add(verb, "undocumented", "dispatched but not named in MANUAL.md")
            continue
        if not spec_of[verb]["keys"]:
            add(verb, "no-output-keys", "declared with no output keys")
        if verb not in docs:
            add(verb, "undocumented", "dispatched but not named in MANUAL.md")

    # 4) no phantom: every declared verb is actually dispatched.
    for verb in sorted(declared_verbs):
        if verb not in registered:
            add(verb, "unregistered", "declared in the table but the CLI does not dispatch it")

    return findings
