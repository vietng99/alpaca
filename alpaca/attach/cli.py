"""The `alpaca attach` verb group (build plan section 3).

`MODULES` is complete from the first task: each name is a module of alpaca/attach/ that may define

    SUBVERBS = {"name": (add_parser(verbs), run(args) -> int)}

`add_parser` adds the sub-verb's own parser to the group; `run` returns an exit code of alpaca/cli.py
(0 PASS, 1 FAIL, 2 BLOCKED, 64 usage). A module that is not built yet is skipped, the pattern of
alpaca/cli.py's VERB_MODULES; a module that breaks is reported on stderr and skipped, so a fault in
attach can never stop the CLI of a project that does not use it.

Every verb ends with `GATE alpaca-attach-<verb>: PASS|FAIL|BLOCKED (reason)`, or with `--json` one
object with the keys `verdict`, `task` and `receipt` (the `surface.TABLE["attach"]` keys). A module
prints through `finish`, imported inside `run` (this module imports the others, not the reverse).
"""
from __future__ import annotations

import importlib
import json
import sys

from alpaca import cli

MODULES = ("repo", "health", "baseline", "check", "study", "runbooks", "approval", "handoff", "sync", "onboarding")

_RUNNERS: dict = {}
_CODES = {"PASS": cli.PASS, "FAIL": cli.FAIL, "BLOCKED": cli.BLOCKED}


def finish(args, verb, verdict, reason="", *, lines=(), receipt=None, task=None) -> int:
    """Print a verb's result and return its exit code. `lines` are printed before the GATE line
    (not at all with --json, which prints one object instead)."""
    if getattr(args, "json", False):
        print(json.dumps({"verdict": verdict, "task": task, "receipt": receipt, "reason": reason or None},
                         sort_keys=True, default=str))
    else:
        for line in lines:
            print(line)
        print("GATE alpaca-attach-%s: %s%s" % (verb, verdict, " (%s)" % reason if reason else ""))
    return _CODES[verdict]


def _module(name):
    """The module `alpaca.attach.<name>`, or None when it is not built yet or breaks on import."""
    full = "alpaca.attach.%s" % name
    try:
        return importlib.import_module(full)
    except ModuleNotFoundError as exc:
        if exc.name == full:
            return None
        print("alpaca: attach module %s unavailable (%s: %s)" % (name, type(exc).__name__, exc), file=sys.stderr)
    except Exception as exc:                                       # noqa: BLE001 - never stop the CLI
        print("alpaca: attach module %s unavailable (%s: %s)" % (name, type(exc).__name__, exc), file=sys.stderr)
    return None


def parser(sub) -> None:
    p = sub.add_parser("attach", help="run Alpaca beside an existing repository (init, status, check, handoff, sync)")
    verbs = p.add_subparsers(dest="attach_verb")
    _RUNNERS.clear()
    for name in MODULES:
        mod = _module(name)
        for verb, (add, run) in sorted((getattr(mod, "SUBVERBS", None) or {}).items()):
            add(verbs)
            _RUNNERS[verb] = run


@cli.command("attach")
def cmd_attach(args) -> int:
    run = _RUNNERS.get(getattr(args, "attach_verb", None))
    if run is None:
        print("usage: alpaca attach {%s} ..." % ",".join(sorted(_RUNNERS)), file=sys.stderr)
        return cli.USAGE
    return run(args)


cli.register_parser("attach", parser)
