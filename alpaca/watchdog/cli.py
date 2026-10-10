"""The `alpaca watchdog` verbs (design/watchdog/design.md sections 3 to 6).

watch       print one line per new git or record event; nothing when idle (`--once` for one tick)
preflight   report what a git command would touch and give a verdict: exit 0 safe, 1 risky, 2 stop
undo        keep an undo point for a preflight run: refs under refs/watchdog/<id>/ and a record snapshot
rollback    recreate the branches and worktrees an undo point saved (`--dry-run` prints the plan)
postcheck   after the operation: verify the record, compare refs with the preflight, post the result
"""
from __future__ import annotations

import json
import sys

from alpaca import cli
from alpaca.watchdog import runs

VERDICT_CODE = {"safe": cli.PASS, "risky": cli.FAIL, "stop": cli.BLOCKED}


def _print(result, as_json, lines):
    if as_json:
        print(json.dumps(result, indent=1, sort_keys=True))
    else:
        for line in lines:
            print(line)


@cli.command("watchdog")
def cmd_watchdog(args):
    root = cli._root()
    sid = args.session or "cli"
    verb = getattr(args, "watchdog_verb", None)
    try:
        if verb == "watch":
            from alpaca.watchdog import watcher
            return watcher.run(root, interval=args.interval, once=args.once, as_json=args.json)
        if verb == "preflight":
            from alpaca.watchdog import preflight
            argv = list(args.command or [])
            if argv[:1] == ["--"]:
                argv = argv[1:]
            if not argv:
                print("GATE alpaca-watchdog-preflight: FAIL (give the git command after --)")
                return cli.USAGE
            result = preflight.run(root, argv, for_=args.for_, session=sid, cwd=args.cwd)
            _print(result, args.json, ["preflight %s: %s" % (result["id"], result["verdict"]),
                                       "report: %s" % result["report"],
                                       "next: alpaca watchdog undo %s" % result["id"]])
            return VERDICT_CODE.get(result["verdict"], cli.FAIL)
        if verb == "undo":
            from alpaca.watchdog import undo
            result = undo.create(root, args.id, session=sid)
            _print(result, args.json, ["undo point %s: %d ref(s), record snapshot %s"
                                       % (args.id, len(result["refs"]), result["snapshot"]["path"]),
                                       "rollback: %s" % result["rollback"]]
                   + ["NOT CAPTURED %s: %s" % (n["path"], n["error"]) for n in result["not_captured"]])
            return cli.PASS
        if verb == "rollback":
            from alpaca.watchdog import undo
            result = undo.rollback(root, args.id, dry_run=args.dry_run, session=sid)
            _print(result, args.json, ["%s %s" % (step["action"], step["detail"]) for step in result["steps"]]
                   + ["rollback %s: %s" % (args.id, "ok" if result["ok"] else "incomplete")])
            return cli.PASS if result["ok"] else cli.FAIL
        if verb == "postcheck":
            from alpaca.watchdog import postcheck
            result = postcheck.run(root, args.id, to=args.to, session=sid)
            _print(result, args.json, ["postcheck %s: %s" % (args.id, result["result"])]
                   + ["  %s" % m for m in result["mismatches"]] + ["report: %s" % result["report"]])
            return cli.PASS if result["result"] == "ok" else cli.FAIL
    except runs.RunError as exc:
        print("GATE alpaca-watchdog-%s: FAIL (%s)" % (verb, exc))
        return cli.FAIL
    print("usage: alpaca watchdog {watch,preflight,undo,rollback,postcheck}", file=sys.stderr)
    return cli.USAGE


def _parser(sub):
    p = sub.add_parser("watchdog", help="advisory guard for destructive git work (op-021)")
    verbs = p.add_subparsers(dest="watchdog_verb")
    w = verbs.add_parser("watch", help="print one line per new event; nothing when idle")
    w.add_argument("--interval", type=float, default=30.0, help="seconds between ticks (default 30)")
    w.add_argument("--once", action="store_true", help="run one tick and exit")
    w.add_argument("--json", action="store_true", help="one JSON object per event line")
    f = verbs.add_parser("preflight", help="report and verdict for a git command given after --")
    f.add_argument("--for", dest="for_", default=None, help="the session or task asking")
    f.add_argument("--cwd", default=None, help="the asker's directory: base for a command without -C (default: here)")
    f.add_argument("--json", action="store_true")
    f.add_argument("command", nargs=__import__("argparse").REMAINDER)
    u = verbs.add_parser("undo", help="keep an undo point for a preflight run")
    u.add_argument("id")
    u.add_argument("--json", action="store_true")
    r = verbs.add_parser("rollback", help="recreate what an undo point saved")
    r.add_argument("id")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--json", action="store_true")
    c = verbs.add_parser("postcheck", help="verify and compare refs after the operation")
    c.add_argument("id")
    c.add_argument("--to", default=None, help="who gets the result message (default: the preflight's --for)")
    c.add_argument("--json", action="store_true")


cli.register_parser("watchdog", _parser)
