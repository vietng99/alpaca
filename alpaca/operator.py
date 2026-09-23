"""Explicit lifecycle for Codex and terminal operators, shared with Claude hooks.

Claude wrappers own hook JSON and fail-open behavior. This command invokes the
underlying handlers directly so a failed checkpoint returns a real error.
"""
import json
import re
import uuid
from pathlib import Path

from alpaca import cli, db, paths, util

OPERATORS = ("codex", "claude", "terminal")
SESSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class SessionError(ValueError):
    pass


def run(action, root, session=None, *, operator=None, level=None, goal="",
        note="", reason=None, reply=None, tool=None, ref="", dashboard=False,
        transcript=None):
    """Apply one explicit boundary; return JSON-ready context and result data.

    `transcript` is an explicit path to this session's transcript file, accepted by start and end
    only. It is stored in `sessions.transcript`, which is what `alpaca.transcripts.snapshot` copies,
    so a Codex or terminal operator lands the same durable copy a Claude session does. There is no
    discovery behind it: an operator that names no path registers none.
    """
    root = paths.root(root)
    if action == "start" and not session:
        session = "%s-%s" % (operator or "codex", uuid.uuid4())
    if not session or not SESSION_PATTERN.fullmatch(session):
        raise SessionError("supply --session with 1-128 letters, digits, dots, underscores or hyphens")
    if operator is not None and operator not in OPERATORS:
        raise SessionError("unknown operator %r" % operator)
    if action == 'start' and transcript is None and (operator or 'codex') == 'codex':
        candidate = session.removeprefix('codex-')
        try:
            native_id = str(uuid.UUID(candidate))
        except ValueError:
            native_id = None
        if native_id:
            from alpaca.transcripts import discover_codex
            transcript = discover_codex(root,native_id)
    if transcript is not None:
        if action not in ("start", "end"):
            raise SessionError("--transcript belongs to session start or session end")
        transcript = str(Path(transcript).expanduser().resolve())
        if not Path(transcript).is_file():
            raise SessionError("--transcript names no readable file: %s" % transcript)
    if action != "start" and not Path(paths.db_path(root)).is_file():
        raise SessionError("session is not started; run session start first")
    conn = db.connect(root)
    try:
        row = db.rows(conn, "sessions", "sid=?", (session,))
        previous_operator = db.meta_get(conn, "operator:%s" % session)
        selected_operator = operator or previous_operator or "codex"
        if previous_operator and previous_operator != selected_operator:
            raise SessionError("session %s belongs to %s; choose a separate session id" %
                               (session, previous_operator))
        if action != "start" and not row:
            raise SessionError("session is not started; run session start first")
        if row and row[0].get("ended") and action not in ("start", "end"):
            raise SessionError("session ended; run session start to resume it")
        payload = {"cwd": root, "session_id": session, "operator": selected_operator,
                   "_strict": True, "reason": reason, "note": note,
                   "dashboard": dashboard, "reply": reply,
                   "level": level, "goal": goal,
                   "transcript_path": transcript,
                   "source": "resume" if row else "startup"}
        result = {"session": session, "operator": selected_operator, "action": action}
        if transcript is not None:
            result["transcript"] = transcript
            if action == "end":
                # session_end writes no transcript path of its own, so register it before the
                # handler runs: its snapshot then copies the file the operator just named.
                db.patch(conn, "sessions", "sid", session, {"transcript": transcript})
        if action == "start":
            from alpaca.hooks import session_start
            result.update(session_start.handle(payload))
        elif action == "checkpoint":
            from alpaca.hooks import pre_compact
            payload["event_kind"] = "session-checkpoint"
            payload["trigger"] = reason or "explicit"
            result.update(pre_compact.handle(payload))
        elif action == "end":
            from alpaca.hooks import session_end
            result.update(session_end.handle(payload))
        elif action == "stop":
            from alpaca.hooks import stop
            result.update(stop.handle(payload))
        elif action == "prompt":
            from alpaca.hooks import user_prompt
            result.update(user_prompt.handle(payload))
        elif action == "beat":
            from alpaca.hooks import post_tool
            if not tool:
                raise SessionError("beat requires --tool")
            payload.update(tool_name=tool, tool_input={"description": ref})
            result.update(post_tool.handle(payload))
        elif action == "level":
            from alpaca.posture import level as posture
            posture.set_local(conn, session, level, goal, actor=selected_operator, root=root)
            from alpaca import pad
            pad.write(root)
            result["level"] = "L%d" % posture.in_force(conn, session, root=root)
        else:
            raise SessionError("unknown session action %r" % action)
        return result
    finally:
        conn.close()


@cli.command("session")
def cmd_session(args):
    try:
        reply = None
        if getattr(args, "reply_file", None):
            reply = Path(args.reply_file).read_text(encoding="utf-8")
        result = run(args.session_action, cli._root(), args.session,
                     operator=getattr(args, "operator", None),
                     level=getattr(args, "level", None), goal=getattr(args, "goal", ""),
                     note=getattr(args, "note", ""), reason=getattr(args, "reason", None),
                     reply=reply, tool=getattr(args, "tool", None), ref=getattr(args, "ref", ""),
                     dashboard=getattr(args, "dashboard", False),
                     transcript=getattr(args, "transcript", None))
    except SessionError as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc)}))
        return cli.BLOCKED
    print(json.dumps(result, ensure_ascii=True))
    return cli.BLOCKED if result.get("decision") == "block" else cli.PASS


def _parser(sub):
    p = sub.add_parser("session", help="explicit portable operator lifecycle")
    verbs = p.add_subparsers(dest="session_action", required=True)
    start = verbs.add_parser("start", help="start or resume and return the boot context")
    start.add_argument("--operator", choices=OPERATORS, default=None)
    start.add_argument("--level", choices=["L%d" % n for n in range(1, 7)])
    start.add_argument("--goal", default="")
    start.add_argument("--dashboard", action="store_true")
    start.add_argument("--transcript", default=None,
                       help="explicit path to this session's transcript file, stored in the record")
    cp = verbs.add_parser("checkpoint", help="persist events and a continuation note")
    cp.add_argument("--note", default="")
    cp.add_argument("--reason", default="explicit")
    end = verbs.add_parser("end", help="close a recorded session and refresh projections")
    end.add_argument("--reason", default="explicit")
    end.add_argument("--transcript", default=None,
                     help="explicit path to this session's transcript file, stored in the record")
    stop = verbs.add_parser("stop", help="record a turn boundary and check stop rules")
    stop.add_argument("--reply-file", default=None)
    verbs.add_parser("prompt", help="apply the style and posture reminder cadence")
    beat = verbs.add_parser("beat", help="record an explicitly reported tool action")
    beat.add_argument("--tool", required=True)
    beat.add_argument("--ref", default="")
    level = verbs.add_parser("level", help="record an owner-authorized session level")
    level.add_argument("level", choices=["L%d" % n for n in range(1, 7)])
    level.add_argument("--goal", default="")


cli.register_parser("session", _parser)
