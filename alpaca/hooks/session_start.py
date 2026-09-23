"""SessionStart: init if needed, record the session, inject the boot block + pad + level."""
import os
import shlex

from alpaca.hooks import common


def export_session(sid) -> bool:
    """Hand the session id to every later Bash call of this Claude Code session.

    Claude Code gives a SessionStart hook `CLAUDE_ENV_FILE`; lines written there are sourced
    before each Bash tool call. With the id in `ALPACA_SESSION_ID`, a profile verb and every gate
    run carry their real origin by script, so the agent never has to remember `--session` and a
    receipt is not left on a synthetic service session. Idempotent across
    resume and compact. An operator without that file (Codex, a terminal) is unaffected."""
    path = os.environ.get("CLAUDE_ENV_FILE")
    if not path or not sid or sid == "unknown":
        return False
    line = "export ALPACA_SESSION_ID=%s\n" % shlex.quote(str(sid))
    try:
        existing = ""
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                existing = fh.read()
        if line not in existing:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
        return True
    except OSError:
        return False


def handle(payload):
    from alpaca import db, pad, paths, project, util
    root = paths.root(payload.get("cwd"))
    sid = common.session_of(payload)
    export_session(sid)
    project.ensure_instance(root)
    conn = db.connect(root)
    if db.meta_get(conn, "initialized") is None:
        db.append_event(conn, session=sid, actor="alpaca", kind="init", data={"via": "session_start"})
        db.meta_set(conn, "initialized", util.now_iso())
    level = common.autodrive_level(sid, root=root)
    cfg = project.load(root)
    operator = payload.get("operator") or "claude"
    prior_operator = db.meta_get(conn, "operator:%s" % sid)
    if prior_operator and prior_operator != operator:
        raise ValueError("session id belongs to another operator")
    db.meta_set(conn, "operator:%s" % sid, operator)
    existing = db.rows(conn, "sessions", "sid=?", (sid,))
    with db.transaction(conn):
        db.append_event(conn, session=sid, actor="alpaca", kind="session-start",
                        data={"cwd": payload.get("cwd"), "source": payload.get("source"), "level": level,
                              "transcript": payload.get("transcript_path"), "resume": bool(existing), "operator": operator})
        if existing:
            db.patch(conn, "sessions", "sid", sid, {"ended": None, "cwd": payload.get("cwd"), "transcript": payload.get("transcript_path") or existing[0]["transcript"], "level": level or existing[0]["level"]})
        else:
            db.upsert(conn, "sessions", "sid", {"sid": sid, "started": util.now_iso(), "cwd": payload.get("cwd"),
                                                "transcript": payload.get("transcript_path"), "level": level})
    from alpaca import observability
    source_path = payload.get('transcript_path') or (existing[0].get('transcript') if existing else None)
    observability.expect_source(root,sid,operator,locator=source_path,
                               capabilities={'native_transcript':bool(source_path),'child_inventory_complete':False})
    # M3.1: mirror the /autodrive level and its verbatim goal into the record as a decision row,
    # so every gate reads the level in force from the record, not from the environment.
    try:
        from alpaca.posture import level as posture_level
        if payload.get("level") is not None:
            posture_level.set_local(conn, sid, payload["level"], payload.get("goal") or "",
                                    actor=operator, root=root)
        else:
            posture_level.mirror(conn, sid, root=root)
        level = "L%d" % posture_level.in_force(conn, sid, root=root)
    except Exception:
        if payload.get("_strict"):
            raise
    pad_text = pad.render(root); pad.write(root)
    from alpaca import token
    resume_verdict, resume_detail = token.resume_check(conn, root)
    head = ["=== Alpaca BOOT ===",
            "project: %s | level %s | alpaca %s" % (cfg.get("name") or "(not onboarded)", level or cfg.get("default_level", "L2"), util.now_iso()),
            "Rules: alpaca is the only writer of the record; every claim carries a pointer; a task is done only with a sealed proof report (alpaca proof new, seal, then --proof);",
            "ship and irreversible external actions are human decisions; run `bin/alpaca status` to re-read this pad."]
    # M4.14: state the boot read-order. The core set (doctrine/CORE-CARD.md + MAP.md section 2)
    # loads unconditionally for every role and phase; the MAP.md router narrows the reads only
    # AFTER the core set is loaded. The ordered reads: pad, manifest, core set, router, level, modes.
    head.append("BOOT READ-ORDER (core set loads before the router narrows): 1 pad  2 ALPACA-MANIFEST  "
                "3 core set (doctrine/CORE-CARD.md + MAP.md section 2)  4 router (MAP.md section 3)  "
                "5 level  6 modes.")
    # M4.3: route first contact on the fork state derived from the record, not on the onboarded
    # flag. A fresh copy is walked through onboarding; a cloned but unrun copy is named as such and
    # told to just start work (it acts on nothing until then); a populated record says nothing.
    from alpaca import adopt
    fork_state = adopt.detect(root, conn)
    # A domain profile (alpaca/profile.py `boot_lines`) may say how a bundled project starts; a
    # template project with a profile that says so needs no onboarding.
    from alpaca import profile
    ready = [str(line) for line in profile.load(root).boot_lines(root) or []]
    head.extend(ready)
    # A named profile that did not load, or a hook that failed, is said at boot, never hidden.
    head.extend("PROFILE PROBLEM: %s" % line for line in profile.problems(root))
    bundled = bool(cfg.get("template") and ready)
    if bundled:
        pass                    # the profile's lines above say how to start; nothing to onboard
    elif fork_state == adopt.FRESH:
        head.append("FIRST CHAT: this project is not onboarded. Ask, in this order, then run the verb once:")
        head.append("  1) who is working on this (names and roles)  2) what is being worked on (one paragraph)")
        head.append("  3) the open tasks as a list (no goals)  4) enable the plain-writing preset? (y/n)")
        head.append("  then: bin/alpaca onboard --name <n> --who <a:role,b:role> --what \"...\" --task \"...\" [--preset plain-writing]; then bin/alpaca doctor")
    elif fork_state == adopt.SECOND_MACHINE:
        head.append("SECOND MACHINE: this is a cloned but unrun copy of %s; it acts on nothing. Do not onboard; just start work and the record builds here locally." % (cfg.get("name") or "this project"))
    if resume_verdict == "BLOCK":
        head.append("RESUME BLOCK: %d verb(s) killed between the intent note and the result note. The pad names each token, its target and two resolutions. Do not re-run; resolve first." % len(resume_detail["blocked"]))
    # do-not-race: an actively-leased task is advancing; read the live claim, do not take it over.
    live = db.rows(conn, "tasks", "status='doing' AND lease_until IS NOT NULL AND lease_until > ?", (util.now_iso(),))
    if live:
        t = live[0]
        head.append("DO NOT RACE: %s is claimed by %s until %s; confirm it is stalled before taking over." % (t["id"], t["claimant"], t["lease_until"]))
    if payload.get("dashboard"):
        from alpaca import serve
        url = serve.ensure_running(root)
        if url:
            head.append("Live dashboard: %s (read-only; bin/alpaca serve --stop to stop)" % url)
    conn.close()
    return {"context": "\n".join(head) + "\n\n" + pad_text, "level": level}

@common.fail_open
def main():
    result = handle(common.read_stdin())
    common.emit_context("SessionStart", result["context"])


if __name__ == "__main__":
    main()
