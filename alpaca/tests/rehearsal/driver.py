"""The rehearsal driver (op-062 t-656, AC-20): the pieces that let an agent play the host and another agent
play a newcomer, in a real studio, with a transcript the owner-word checks accept.

A rehearsal is run by a Workflow script (`run-workflow.js` next to this file's README); this module is what
the agents call, from the shell:

    python -m alpaca.tests.rehearsal.driver make P2 --scratch DIR [--today 2026-10-14] [--probe CLONE]
    python -m alpaca.tests.rehearsal.driver host   DIR --text-file F     # append what the host said to the owner
    python -m alpaca.tests.rehearsal.driver owner  DIR --text-file F     # append what the persona typed
    python -m alpaca.tests.rehearsal.driver snapshot DIR LABEL           # fingerprint the studio, record events seen
    python -m alpaca.tests.rehearsal.driver state  DIR                   # applied? how many owner turns? latest card?
    python -m alpaca.tests.rehearsal.driver persona DIR                  # the persona sheet (for the persona agent only)
    python -m alpaca.tests.rehearsal.driver finish DIR                   # write transcript.md, events.tsv, cards/

`make` builds, under DIR/<persona id>/: the studio (`alpaca new`), the fixture repo, a config folder with the
session transcript where Claude Code writes it, the session row and its first event, and `alpaca`, a wrapper
that runs the studio's own CLI as session `reh-<id>`. The wrapper sets ALPACA_REHEARSAL=1: the event writer
(`db.append_event`) copies it into event data as `rehearsal: true` for every verb and no check reads it, so production learns no bypass. The persona's
typed lines are real `typed` entries (origin human, entrypoint cli), so `onboard confirm` proves them the way
it proves a person's.

The host agent never reads `alpaca/tests/rehearsal/personas/`; the wrapper logs every verb it runs to
`commands.log`, and `score.personas_read` checks that log (and any log text the caller adds) for the folder.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shlex
import subprocess
import sys

import yaml

from alpaca import db, ownerword, util

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
PERSONAS = os.path.join(HERE, "personas")

QUIZ = (
    "Will Alpaca put files in your repo?",
    "How does a change get in, and who merges it?",
    "What may the agent do without asking you?",
    "How do you undo everything?",
    "What happens in the first 30 minutes after you say yes?",
)


# ---------------------------------------------------------------------------------------- paths

def run_dir(scratch, persona_id):
    return os.path.join(os.path.abspath(scratch), persona_id)


def meta_path(rd):
    return os.path.join(rd, "meta.json")


def load_meta(rd):
    with open(meta_path(rd), encoding="utf-8") as fh:
        return json.load(fh)


def load_persona(persona_id):
    with open(os.path.join(PERSONAS, persona_id + ".yaml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def public_view(persona, meta):
    """What the host side may know of a persona: nothing. What the persona agent is told as its public part."""
    pub = {k: (v.replace("{repo_path}", meta.get("repo_path") or "") if isinstance(v, str) else v)
           for k, v in persona["public"].items()}
    return {"id": persona["id"], "name": persona["name"], "public": pub}


# ------------------------------------------------------------------------------------ the studio

def _clock_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds")


def make_run(scratch, persona_id, *, today=None, probe=None, install=REPO, python=sys.executable):
    """Build the studio, fixture repo, session and wrapper for one persona. Returns the meta dict (also saved)."""
    from alpaca.tests.rehearsal import fixtures
    persona = load_persona(persona_id)
    rd = run_dir(scratch, persona_id)
    if os.path.exists(rd):
        raise RuntimeError("%s exists; use a new scratch folder (a rehearsal always starts from a fresh studio)" % rd)
    os.makedirs(rd)
    today = today or datetime.date.today().isoformat()
    day = datetime.date.fromisoformat(today)
    repo_path = None
    if persona["repo"] == "team-repo":
        repo_path = fixtures.build_team_repo(os.path.join(rd, "repo"), day)
    elif persona["repo"] == "qtrm64-mirror":
        if not probe:
            raise RuntimeError("P1 needs --probe, a local clone of the QTRM64 repository")
        repo_path = fixtures.mirror_qtrm64(os.path.join(rd, "repo"), probe)
    studio = os.path.join(rd, "studio")
    name = {"P1": "firmware", "P2": "shop-reports", "P3": "class-reading"}.get(persona_id, persona_id.lower())
    done = subprocess.run([python, "-m", "alpaca", "new", "--yes", "--no-serve", "--no-bootstrap", "--name", name,
                           "--dir", studio, "--who", "%s:owner" % persona["name"].lower()],
                          cwd=install, capture_output=True, text=True,
                          env={k: v for k, v in os.environ.items() if k not in ("CLAUDE_CODE_SESSION_ID", "ALPACA_SESSION_ID")})
    if done.returncode != 0:
        raise RuntimeError("alpaca new failed:\n" + done.stdout[-800:] + done.stderr[-800:])
    cfg = os.path.join(rd, "cfg")
    sid = "reh-%s" % persona_id
    transcript = os.path.join(cfg, "projects", ownerword.project_slug(os.path.realpath(studio)), sid + ".jsonl")
    os.makedirs(os.path.dirname(transcript), exist_ok=True)
    open(transcript, "a").close()
    conn = db.connect(os.path.realpath(studio))
    db.upsert(conn, "sessions", "sid", {"sid": sid, "started": util.now_iso(), "transcript": transcript})
    db.append_event(conn, session=sid, actor="alpaca", kind="session-start", data={"transcript": transcript})
    base_event = conn.execute("SELECT MAX(id) FROM events").fetchone()[0]
    conn.close()
    wrapper = os.path.join(rd, "alpaca")
    with open(wrapper, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n"
                 "# runs the studio's own CLI as the rehearsal session; logs every call\n"
                 "printf '%%s\\n' \"$*\" >> %s\n"
                 "cd %s || exit 64\n"
                 "exec env -u CLAUDE_CODE_SESSION_ID -u CLAUDECODE ALPACA_SESSION_ID=%s CLAUDE_CONFIG_DIR=%s "
                 "ALPACA_REHEARSAL=1 PYTHONPATH=%s %s -m alpaca \"$@\"\n"
                 % (shlex.quote(os.path.join(rd, "commands.log")), shlex.quote(os.path.realpath(studio)), sid,
                    shlex.quote(cfg), shlex.quote(os.path.realpath(studio)), shlex.quote(python)))
    os.chmod(wrapper, 0o755)
    meta = {"persona": persona_id, "run_dir": rd, "studio": os.path.realpath(studio), "cfg": cfg, "sid": sid,
            "transcript": transcript, "wrapper": wrapper, "repo_path": repo_path, "today": today,
            "scratch": os.path.join(rd, "look-scratch"), "base_event": base_event, "install": install}
    with open(meta_path(rd), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=1, sort_keys=True)
    for name_ in ("snapshots.tsv", "commands.log", "persona_turns.jsonl"):
        open(os.path.join(rd, name_), "a").close()
    snapshot(rd, "S0")
    return meta


# --------------------------------------------------------------------------------- transcript

def _append(meta, entry):
    with open(meta["transcript"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def append_owner(rd, text):
    """A line the persona typed, in the shape Claude Code writes a typed prompt."""
    meta = load_meta(rd)
    ts = _clock_now()
    _append(meta, {"parentUuid": "p", "isSidechain": False, "promptId": "x", "type": "user",
                   "message": {"role": "user", "content": text}, "uuid": "u", "timestamp": ts,
                   "origin": {"kind": "human"}, "promptSource": "typed", "userType": "external", "entrypoint": "cli",
                   "sessionId": meta["sid"]})
    with open(os.path.join(rd, "persona_turns.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"text": text, "ts": ts}) + "\n")
    return ts


def append_host(rd, text):
    """A message the host agent showed the owner, in the shape Claude Code writes an assistant text block."""
    meta = load_meta(rd)
    ts = _clock_now()
    _append(meta, {"type": "assistant", "isSidechain": False, "uuid": "a", "timestamp": ts, "sessionId": meta["sid"],
                   "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
    return ts


def turns(rd):
    """The transcript as [(who, text)] in order: who is `owner` or `host`."""
    meta = load_meta(rd)
    out = []
    with open(meta["transcript"], encoding="utf-8") as fh:
        for raw in fh:
            entry = json.loads(raw)
            if entry.get("type") == "user":
                out.append(("owner", entry["message"]["content"]))
            elif entry.get("type") == "assistant":
                out.append(("host", "\n".join(b["text"] for b in entry["message"]["content"] if b.get("type") == "text")))
    return out


# ----------------------------------------------------------------------------------- snapshots

def fingerprint(studio):
    """sha256 of every file outside `.alpaca/` and `.git/`, the studio's git config and exclude file, and
    whether `app/` exists."""
    files = {}
    for dp, dns, fns in os.walk(studio):
        dns[:] = sorted(d for d in dns if d not in (".alpaca", ".git"))
        for fn in sorted(fns):
            path = os.path.join(dp, fn)
            try:
                with open(path, "rb") as fh:
                    files[os.path.relpath(path, studio)] = hashlib.sha256(fh.read()).hexdigest()
            except OSError:
                files[os.path.relpath(path, studio)] = "unreadable"
    for rel in (".git/config", ".git/info/exclude"):
        path = os.path.join(studio, rel)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                files[rel] = hashlib.sha256(fh.read()).hexdigest()
    files["app/"] = "present" if os.path.exists(os.path.join(studio, "app")) else "absent"
    return files


def last_event_id(studio):
    conn = db.connect(studio)
    try:
        return conn.execute("SELECT MAX(id) FROM events").fetchone()[0] or 0
    finally:
        conn.close()


def snapshot(rd, label):
    """Append `label<TAB>digest<TAB>last event id` to snapshots.tsv and keep the file list."""
    meta = load_meta(rd)
    files = fingerprint(meta["studio"])
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    event = last_event_id(meta["studio"])
    with open(os.path.join(rd, "snapshots.tsv"), "a", encoding="utf-8") as fh:
        fh.write("%s\t%s\t%d\n" % (label, digest, event))
    os.makedirs(os.path.join(rd, "snapshots"), exist_ok=True)
    with open(os.path.join(rd, "snapshots", label + ".json"), "w", encoding="utf-8") as fh:
        json.dump(files, fh, indent=1, sort_keys=True)
    return digest


def events(rd):
    """[(id, kind, actor, data)] of the studio record after the rehearsal began."""
    meta = load_meta(rd)
    conn = db.connect(meta["studio"])
    try:
        rows = conn.execute("SELECT id, kind, actor, data FROM events WHERE id > ? ORDER BY id", (meta["base_event"],)).fetchall()
        return [(r["id"], r["kind"], r["actor"], json.loads(r["data"])) for r in rows]
    finally:
        conn.close()


def state(rd):
    """A short status the loop reads: has the setup been applied, how many owner lines, the newest card."""
    evs = events(rd)
    kinds = [k for _i, k, _a, _d in evs]
    cards = [d["id"] for _i, k, _a, d in evs if k == "onboard-card"]
    applied = any(k == "onboard-applied" and d.get("step") == "done" for _i, k, _a, d in evs)
    return {"applied": applied, "owner_lines": sum(1 for who, _ in turns(rd) if who == "owner"),
            "host_lines": sum(1 for who, _ in turns(rd) if who == "host"), "confirmed": "onboard-confirmed" in kinds,
            "declined": kinds.count("onboard-declined"), "latest_card": cards[-1] if cards else None,
            "events": len(evs)}


# ------------------------------------------------------------------------------------- finish

def finish(rd):
    """Write transcript.md, events.tsv and the copies of the card files; return the paths."""
    meta = load_meta(rd)
    with open(os.path.join(rd, "transcript.md"), "w", encoding="utf-8") as fh:
        for n, (who, text) in enumerate(turns(rd), 1):
            fh.write("## %d %s\n\n%s\n\n" % (n, "OWNER" if who == "owner" else "HOST", text.rstrip()))
    with open(os.path.join(rd, "events.tsv"), "w", encoding="utf-8") as fh:
        for i, kind, actor, data in events(rd):
            fh.write("%d\t%s\t%s\t%s\n" % (i, kind, actor, "rehearsal" if data.get("rehearsal") else ""))
    src = os.path.join(meta["studio"], ".alpaca", "onboard", "cards")
    dst = os.path.join(rd, "cards")
    os.makedirs(dst, exist_ok=True)
    if os.path.isdir(src):
        for fn in sorted(os.listdir(src)):
            with open(os.path.join(src, fn), "rb") as a, open(os.path.join(dst, fn), "wb") as b:
                b.write(a.read())
    return {"transcript": os.path.join(rd, "transcript.md"), "events": os.path.join(rd, "events.tsv"), "cards": dst}


# ----------------------------------------------------------------------------------------- CLI

def _read_text(args):
    if args.text_file:
        with open(args.text_file, encoding="utf-8") as fh:
            return fh.read().rstrip("\n")
    return sys.stdin.read().rstrip("\n")


def _added_line(who, rd, text):
    """The one line `host` and `owner` print: the message is in the chat. transcript.md is rewritten by `finish`, so it
    is stale until then (a silent verb made one host send its message twice)."""
    n = len(turns(rd))
    return "added: %s message %d (%d characters) is in the chat; run finish to refresh transcript.md" % (who, n, len(text))


def main(argv=None):
    p = argparse.ArgumentParser(prog="driver")
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("persona")
    m.add_argument("--scratch", required=True)
    m.add_argument("--today")
    m.add_argument("--probe")
    for name in ("host", "owner"):
        s = sub.add_parser(name)
        s.add_argument("run_dir")
        s.add_argument("--text-file")
    s = sub.add_parser("snapshot")
    s.add_argument("run_dir")
    s.add_argument("label")
    for name in ("state", "persona", "finish", "quiz"):
        s = sub.add_parser(name)
        s.add_argument("run_dir")
    a = p.parse_args(argv)
    if a.cmd == "make":
        print(json.dumps(make_run(a.scratch, a.persona, today=a.today, probe=a.probe), indent=1, sort_keys=True))
    elif a.cmd in ("host", "owner"):
        text = _read_text(a)
        (append_host if a.cmd == "host" else append_owner)(a.run_dir, text)
        print(_added_line(a.cmd, a.run_dir, text))
    elif a.cmd == "snapshot":
        print(snapshot(a.run_dir, a.label))
    elif a.cmd == "state":
        print(json.dumps(state(a.run_dir), sort_keys=True))
    elif a.cmd == "persona":
        meta = load_meta(a.run_dir)
        persona = load_persona(meta["persona"])
        print(json.dumps({"public": public_view(persona, meta), "sealed": persona["sealed"]}, indent=1))
    elif a.cmd == "finish":
        print(json.dumps(finish(a.run_dir), indent=1))
    elif a.cmd == "quiz":
        print("\n".join("Q%d. %s" % (i, q) for i, q in enumerate(QUIZ, 1)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
