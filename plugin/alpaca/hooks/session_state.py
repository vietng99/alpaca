#!/usr/bin/env python3
"""Portable optional autonomy hooks. Session state is owned by the local Alpaca record."""
import argparse
import json
import os
from pathlib import Path
import re
import sys


ROOT = Path(os.environ.get("ALPACA_ROOT") or Path(__file__).resolve().parents[3]).resolve()
if not (ROOT / "ALPACA-MANIFEST").is_file():
    raise SystemExit("Optional Alpaca hooks require an explicit local Alpaca project.")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def payload():
    if sys.stdin.isatty():
        return {}
    try:
        data = json.loads(sys.stdin.read(1024 * 1024) or "{}")
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def state(session):
    directory = ROOT / ".alpaca" / "config"
    try:
        level = (directory / (".autodrive-level." + session)).read_text().strip()
    except OSError:
        return None, ""
    if level not in {"L%d" % value for value in range(1, 7)}:
        return None, ""
    try:
        goal = (directory / (".autodrive-goal." + session)).read_text().strip()
    except OSError:
        goal = ""
    return level, goal


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inject", "stop", "badge", "set"))
    parser.add_argument("values", nargs="*")
    parser.add_argument("--session")
    args = parser.parse_intermixed_args(argv)
    data = {} if args.action == "set" else payload()
    session = args.session or data.get("session_id") or data.get("sessionId")
    if args.action == "set":
        session = session or os.environ.get("ALPACA_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not isinstance(session, str) or not SID.fullmatch(session):
        if args.action == "set":
            print("Supply --session with the active Alpaca session ID.", file=sys.stderr)
            return 2
        return 0
    if args.action == "set":
        if not args.values or args.values[0].upper() not in {"OFF", "L1", "L2", "L3", "L4", "L5", "L6"}:
            print("Expected L1 through L6 or OFF, followed by optional authorized scope.", file=sys.stderr)
            return 2
        level = args.values[0].upper()
        goal = " ".join(args.values[1:])
        try:
            from alpaca import operator
            if level == "OFF":
                operator.run("checkpoint", str(ROOT), session,
                             note="Autodrive OFF: stop autonomous advancement. " + goal)
                result = operator.run("end", str(ROOT), session, reason="autodrive-off")
                result["autodrive"] = "OFF"
            else:
                result = operator.run("level", str(ROOT), session, level=level, goal=goal)
        except (ValueError, OSError) as exc:
            print("Alpaca autonomy: " + str(exc), file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=True))
        return 0
    level, goal = state(session)
    if level is None or args.action == "stop":
        # The project's real Stop handler owns completion and interruption decisions.
        return 0
    if args.action == "badge":
        print("Alpaca %s | session %s" % (level, session))
        return 0
    event = data.get("hook_event_name") or (args.values[0] if args.values else "UserPromptSubmit")
    if event not in ("SessionStart", "UserPromptSubmit", "PostToolUse"):
        return 0
    context = ("Alpaca session %s posture: %s. Authorized scope: %s. "
               "Follow the current owner's scope, project boundaries, and acceptance checks. "
               "A level does not grant separate unattended authority or permission to publish. "
               "Checkpoint before interruption and inspect a running job before launching another.") % (
                   session, level, goal or "none recorded")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event,
                                             "additionalContext": context}}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
