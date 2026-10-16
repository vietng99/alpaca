"""While the first chat is open, an agent call runs only the commands on `first_chat.FIRST_CHAT_VERBS` (op-062 t-656,
fix round 3; design/attach-mode/onboarding-design.md 7.1 and 7.7).

Design 7.1: before the owner says yes to the setup card, nothing outside `.alpaca/` changes, `.git/config` and
`.git/info/exclude` stay, and no `app/` appears. `cli.main` asks `first_chat.verb_refusal` before any command runs, so
an ordinary command an agent is told to run elsewhere (`style use` for the plain-writing question, `note add` for the
owner's goal) cannot write setup while the first chat is open. The owner's own terminal is never refused there.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

import pytest

from alpaca import adopt, cli, db, first_chat, onboard_card, onboard_flow, ownerword
from alpaca.tests.attachkit import upstream  # noqa: F401
from alpaca.tests.conftest import REPO
from alpaca.tests.onboardchat import Chat, chat, make_due_studio  # noqa: F401
from alpaca.tests.test_onboard_nowrite import snapshot

TOKEN = "ONBOARD-FIRST-CHAT-OPEN"

#: files and folders outside .alpaca/ that Alpaca's own views rewrite whatever runs (pad.write via `alpaca status`
#: and the SessionStart hook: RESUME.md, CHECKLIST.md, board.json). Design 7.1 allows RESUME.md by name; the other
#: two are written by the same pad.write call (alpaca/pad.py `write`).
VIEWS = ("RESUME.md", "CHECKLIST.md", "board.json")

#: record event kinds a listed command may add besides onboard_card.PRE_CONFIRM_KINDS, each with its reason
VIEW_KINDS = {}


def _scored(before, after):
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    return [k for k in changed if k not in VIEWS]


def _kinds(root, since):
    conn = db.connect(root)
    try:
        return [r["kind"] for r in conn.execute("SELECT kind FROM events WHERE id>? ORDER BY id", (since,))]
    finally:
        conn.close()


def _last_id(root):
    conn = db.connect(root)
    try:
        return conn.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
    finally:
        conn.close()


def _agent_env(c):
    env = dict(os.environ, PYTHONPATH=REPO, CLAUDECODE="1", CLAUDE_CODE_SESSION_ID=c.sid,
               CLAUDE_CONFIG_DIR=c.cfg)
    env.pop("ALPACA_SESSION_ID", None)
    return env


def _agent_child(c, argv, timeout=300):
    """`python -m alpaca <argv>` as an agent runs it: markers set, standard input from /dev/null."""
    done = subprocess.run([sys.executable, "-m", "alpaca", *argv], cwd=c.root, env=_agent_env(c),
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    return done.returncode, done.stdout + done.stderr


# ================================================================ 1. every registered command is classified

def _ns(verb):
    return argparse.Namespace(verb=verb, **{"%s_verb" % verb: None, "dir": None, "name": None})


def test_every_key_of_the_list_is_a_registered_command():
    verbs = cli.registered_verbs()
    assert set(first_chat.FIRST_CHAT_VERBS) <= verbs, sorted(set(first_chat.FIRST_CHAT_VERBS) - verbs)


def test_every_registered_command_not_on_the_list_is_refused_for_an_agent_while_the_first_chat_is_open(chat, upstream):
    chat.look(upstream.url)
    agent = {"CLAUDE_CODE_SESSION_ID": chat.sid}
    unlisted = sorted(cli.registered_verbs() - set(first_chat.FIRST_CHAT_VERBS))
    assert "style" in unlisted and "note" in unlisted and "upgrade" in unlisted
    for verb in unlisted:
        refused = first_chat.verb_refusal(_ns(verb), environ=agent)
        assert refused and refused[0] == TOKEN, verb
    # the owner's own terminal is never refused here
    for verb in unlisted:
        assert first_chat.verb_refusal(_ns(verb), environ={}, isatty=lambda: True) is None, verb
    # a listed command costs no record read and is never refused (bar `new` into this studio, below)
    for verb in sorted(set(first_chat.FIRST_CHAT_VERBS) - {"attach"}):
        assert first_chat.verb_refusal(_ns(verb), environ=agent) is None, verb


def test_a_sub_verb_off_the_list_is_refused(chat, upstream):
    chat.look(upstream.url)
    agent = {"CLAUDE_CODE_SESSION_ID": chat.sid}
    for sub in ("init", "status"):
        assert first_chat.verb_refusal(argparse.Namespace(verb="attach", attach_verb=sub), environ=agent) is None
    # sub-verbs other waves add later (check, handoff, sync, ...) are refused until they are put on the list
    for sub in ("check", "handoff", "sync", "baseline", None):
        refused = first_chat.verb_refusal(argparse.Namespace(verb="attach", attach_verb=sub), environ=agent)
        assert refused and refused[0] == TOKEN, sub


def _s3_writes_setup(c):
    """Cause S3: an agent's `style use plain-writing` mid first chat. True when it changed a setup file."""
    before = snapshot(c.root)
    c.run("style", "use", "plain-writing")
    return bool(_scored(before, snapshot(c.root)))


def test_mutation_control_style_on_the_list_lets_cause_s3_write_project_yaml(chat, upstream, monkeypatch):
    chat.look(upstream.url)
    assert not _s3_writes_setup(chat)
    monkeypatch.setitem(first_chat.FIRST_CHAT_VERBS, "style", None)
    assert _s3_writes_setup(chat), "the mutation went unseen: the S3 check tests nothing"


# ================================================================ 2. every listed command writes nothing it may not

LISTED = [
    ("onboard", "show"),
    ("onboard", "next"),
    ("status",),
    ("doctor",),
    ("verify",),
    ("recall", "chat-1"),
    ("init",),
    ("attach", "status"),
    ("serve", "--status"),
]


@pytest.mark.parametrize("looked", [False, True], ids=["due", "after-look"])
@pytest.mark.parametrize("argv", LISTED, ids=["-".join(a) for a in LISTED])
def test_a_listed_command_run_by_an_agent_leaves_the_setup_files_alone(chat, upstream, argv, looked):
    if looked:
        chat.look(upstream.url)
    assert onboard_flow.first_chat_open(chat.conn, chat.root)
    before, since = snapshot(chat.root), _last_id(chat.root)
    rc, out = _agent_child(chat, list(argv))
    after = snapshot(chat.root)
    kinds = _kinds(chat.root, since)
    print(argv, rc, kinds, out[-300:])
    assert TOKEN not in out, out[-300:]
    assert not _scored(before, after), _scored(before, after)
    assert not after["app"]
    extra = [k for k in kinds if k not in onboard_card.PRE_CONFIRM_KINDS and k not in VIEW_KINDS]
    assert not extra, extra


def test_alpaca_new_by_an_agent_makes_its_project_outside_the_studio_and_not_inside_it(chat, tmp_path):
    before, since = snapshot(chat.root), _last_id(chat.root)
    rc, out = _agent_child(chat, ["new", "--name", "inside", "--dir", os.path.join(chat.root, "sub"), "--yes",
                                  "--no-bootstrap", "--no-serve", "--no-git"])
    assert rc == cli.FAIL and TOKEN in out and first_chat.TEXT_NEW in out, out[-400:]
    assert not os.path.exists(os.path.join(chat.root, "sub"))
    outside = str(tmp_path / "outside")
    rc, out = _agent_child(chat, ["new", "--name", "outside", "--dir", outside, "--yes", "--no-bootstrap",
                                  "--no-serve", "--no-git"], timeout=900)
    assert TOKEN not in out, out[-400:]
    assert os.path.isfile(os.path.join(outside, "project.yaml")), out[-400:]
    assert not _scored(before, snapshot(chat.root))
    assert [k for k in _kinds(chat.root, since) if k not in onboard_card.PRE_CONFIRM_KINDS] == []


# ================================================================ 3. the writers the cause and premortem found

REFUSED = [
    (("style", "use", "plain-writing"), first_chat.TEXT_STYLE),
    (("style", "ban", "frobnicate"), first_chat.TEXT_STYLE),
    (("style", "unban", "frobnicate"), first_chat.TEXT_STYLE),
    (("note", "add", "the owner wants a hello feature"), first_chat.TEXT_GOAL),
    (("start", "the owner wants a hello feature"), first_chat.TEXT_GOAL),
    (("spec", "init", "--kit", "spec-kit"), first_chat.TEXT_ANY),
    (("op", "new", "add a hello feature"), first_chat.TEXT_ANY),
    (("day",), first_chat.TEXT_ANY),
    (("upgrade", "--plan"), first_chat.TEXT_ANY),
    (("task", "add", "op-0", "a task"), first_chat.TEXT_ANY),
]


@pytest.mark.parametrize("looked", [False, True], ids=["due", "after-look"])
@pytest.mark.parametrize("argv,text", REFUSED, ids=["-".join(a[0][:2]) for a in REFUSED])
def test_an_unlisted_command_is_refused_with_a_plain_reason_and_writes_nothing(chat, upstream, argv, text, looked):
    if looked:
        chat.look(upstream.url)
    before, since = snapshot(chat.root), _last_id(chat.root)
    rc, out = chat.run(*argv)
    assert rc == cli.FAIL and ("[%s]" % TOKEN) in out and text in out, out[-400:]
    assert snapshot(chat.root) == before
    assert _kinds(chat.root, since) == []


# ================================================================ 4. guards

def test_after_the_yes_apply_still_writes_setup_and_attaches(chat, upstream):
    from alpaca.attach import config
    from alpaca.tests.test_onboard_attacks import confirmed
    cid, _card = confirmed(chat, upstream.url)
    rc, out = chat.run("onboard", "apply", cid)
    assert rc == 0 and adopt.first_chat_state(chat.conn) == "done", out
    assert config.mode(chat.root) is not None and os.path.isdir(os.path.join(chat.root, config.load(chat.root).repo))
    # after the first chat the ordinary commands run again
    assert chat.run("style", "use", "plain-writing")[0] == 0


def test_the_owner_at_their_own_terminal_runs_style_and_note_mid_chat(chat, upstream, monkeypatch):
    chat.look(upstream.url)
    for name in list(ownerword.AGENT_MARKERS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ownerword, "_stdin_is_tty", lambda: True)
    for argv in (("style", "use", "plain-writing"), ("note", "add", "my own note")):
        rc, out = chat.run(*argv)
        assert rc == 0, (argv, out)


def test_after_the_owners_skip_an_agent_runs_style_and_note_again(chat, upstream):
    chat.look(upstream.url)
    chat.say("skip setup")
    rc, out = chat.run("onboard", "skip", "--quote", "skip setup")
    assert rc == 0, out
    for argv in (("style", "use", "plain-writing"), ("note", "add", "a note")):
        rc, out = chat.run(*argv)
        assert rc == 0, (argv, out)


def test_a_project_with_no_first_chat_is_never_refused(project):
    assert cli.main(["init"]) == 0
    conn = db.connect(str(project))
    assert not first_chat.first_chat_open(conn, str(project))
    assert first_chat.verb_refusal(_ns("style"), environ={"CLAUDE_CODE_SESSION_ID": "s"}) is None


# ================================================================ 5. the cheap reader gives the old answer

def _old_first_chat_open(conn, root):
    """first_chat_open as it was at 20b02d4e4 (alpaca/onboard_flow.py:111-124), the reference for the cheap order."""
    fc = adopt.first_chat_state(conn)
    state = fc if fc else ("fresh" if adopt.detect(root, conn) == adopt.FRESH else "none")
    if state in ("due", "confirmed"):
        return True
    return state == "fresh" and onboard_flow.epoch(conn)[3] is not None


STATES = ["fresh", "fresh-after-look", "due", "confirmed", "done", "skipped", "none"]


@pytest.mark.parametrize("name", STATES)
def test_the_cheap_reader_equals_the_old_function_on_every_state(project, monkeypatch, upstream, name):
    c = Chat(project, monkeypatch, studio=False)
    try:
        from alpaca.tests.attachkit import git
        git(c.root, "init", "-q")
        assert cli.main(["init"]) == 0
        conn = db.connect(c.root)
        if name == "fresh-after-look":
            c.conn = conn
            db.upsert(conn, "sessions", "sid", {"sid": c.sid, "started": c.now(), "transcript": c.transcript})
            db.append_event(conn, session=c.sid, actor="alpaca", kind="session-start", data={"transcript": c.transcript})
            c.look(upstream.url)
        elif name in ("due", "confirmed", "done", "skipped"):
            db.meta_set(conn, adopt.FIRST_CHAT, name)
            conn.commit()
        elif name == "none":
            assert cli.main(["onboard", "--name", "demo", "--who", "ada:owner", "--what", "a demo"]) == 0
        conn = db.connect(c.root)
        want = _old_first_chat_open(conn, c.root)
        assert first_chat.first_chat_open(conn, c.root) == want
        assert onboard_flow.first_chat_open(conn, c.root) == want
        assert want == (name in ("fresh-after-look", "due", "confirmed"))
    finally:
        c.close()


# ================================================================ 6. the claim nudge waits while the chat is open

def _hook(module, payload, root):
    env = dict(os.environ, CLAUDE_PROJECT_DIR=root, ALPACA_SESSION_ID=payload["session_id"],
               PYTHONPATH=REPO)
    return subprocess.run([sys.executable, "-m", module], input=json.dumps(payload), capture_output=True, text=True,
                          encoding="utf-8", cwd=root, env=env)


def _nudged(root):
    _hook("alpaca.hooks.post_tool", {"session_id": "s1", "tool_name": "Edit", "tool_input": {"file_path": "/x/y.py"}},
          root)
    out = _hook("alpaca.hooks.user_prompt", {"session_id": "s1", "prompt": "go"}, root).stdout
    text = json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""
    return "[alpaca claim]" in text


def test_the_task_add_nudge_waits_while_the_first_chat_is_open(project):
    root = os.path.realpath(str(project))
    make_due_studio(root)
    assert not _nudged(root), "the nudge names bin/alpaca task add, which the open first chat refuses"
    conn = db.connect(root)
    db.meta_set(conn, adopt.FIRST_CHAT, "done")
    conn.commit()
    assert _nudged(root), "control: once the first chat is closed the nudge comes back"


# ================================================================ 7. the studio the command writes, from any folder

#: Path-like argument names: an argument whose name holds one of these words names a file or a folder.
PATH_WORDS = {"target", "dir", "root", "out", "output", "path", "destination", "dest", "drop", "scratch", "folder",
              "file", "transcript", "config"}

#: Arguments with a path-like name that only read a file, or name a record entry, never a folder the command writes.
#: Each line says why. A command that writes its own studio only (the one it runs in) needs no line in
#: first_chat.WRITTEN_PATH_ARGS: the dispatcher already checks that studio.
READ_PATH_ARGS = {
    ("wiki propose", "file"): "reads the proposal JSON",
    ("wiki correct", "target"): "an edge id or a JSON file it reads",
    ("wiki correct", "replacement_file"): "reads the replacement JSON",
    ("wiki import", "path"): "reads the export file into this studio's wiki",
    ("wiki extract", "root"): "reads the vault it extracts",
    ("task plan", "file"): "reads the plan JSON",
    ("task contract", "file"): "reads the contract JSON",
    ("apply", "target"): "a record name (ops.cmd_apply writes only the record)",
    ("retention snapshot", "path"): "reads the file; the copy goes to this studio's store",
    ("sandbox widen", "path"): "a path recorded in a sandbox-widen event; the verb writes only the record",
    ("sandbox set", "path"): "a path recorded in a sandbox-set event; the verb writes only the record",
    ("sandbox ask", "path"): "a path printed in an owner-ask event; the verb writes only the record",
    ("new", "attach_gates_file"): "reads the gate list",
    ("attach handoff", "body_file"): "reads the text for the top of the pull request body (attach/handoff._read_body)",
    ("analytics ingest-usage", "file"): "reads a usage file",
    ("analytics price-label", "output"): "a price per token, not a path",
    ("session start", "transcript"): "a transcript path stored in the record",
    ("session end", "transcript"): "a transcript path stored in the record",
    ("session stop", "reply_file"): "reads the reply (alpaca/operator.py)",
    ("collect services", "tunnel_config"): "a path written into the unit files of this studio's .alpaca/services",
    ("collect register", "path"): "a source path stored in the record",
    ("backup verify", "path"): "reads the backup",
    ("backup restore", "path"): "reads the backup; its destination is a written argument",
    ("workspace add", "no_live_config"): "a flag",
    ("workspace move", "from_root"): "the old root a registry entry names; nothing is written there",
    ("workspace move", "no_live_config"): "a flag",
    ("workspace render-ingress", "config"): "reads the live cloudflared config",
    ("workspace render-ingress", "credentials_file"): "a path written into the rendered config",
    ("runbook check", "file"): "reads the runbook",
    ("note add", "file"): "reads the file into the record",
    ("mission define", "file"): "reads the mission file",
    ("research extend", "context_path"): "a file the runtime loaded, read for its digest",
    ("research resume", "context_path"): "a file the runtime loaded, read for its digest",
    ("research submit", "transcript"): "reads the job's transcript",
    ("brief present", "file"): "reads the brief",
    ("brief sign", "file"): "reads the brief",
    ("order check-note", "file"): "reads the note",
    ("attach init", "gates_file"): "reads the gate list",
}


def _parser_args():
    """[(key, dest, sub_dest)] every argument of every command and sub-command `alpaca` dispatches; key is "verb" or
    "verb sub". sub_dest is the attribute of the command's sub-command choice (None for a command without one)."""
    cli.registered_verbs()
    parser, sub = cli.build_parser()
    for _name, reg in cli._EXTRA_PARSERS:
        reg(sub)
    out = []

    def walk(p, trail, sub_dest):
        for a in p._actions:
            if isinstance(a, argparse._SubParsersAction):
                for n, sp in a.choices.items():
                    walk(sp, trail + [n], a.dest if trail else None)
            else:
                out.append((" ".join(trail), a.dest, sub_dest))

    walk(parser, [], None)
    return out


def test_every_path_like_argument_is_a_written_path_or_a_read_one():
    """The class test of round 4's F2: a command that takes a folder or a file to write must be in
    first_chat.WRITTEN_PATH_ARGS, or the dispatcher would not see the studio it writes. A command added later with
    such an argument fails here until it is put in one of the two tables."""
    found = [(k, d) for k, d, _s in _parser_args() if k and set(d.split("_")) & PATH_WORDS]
    written = {(k, d) for k, ds in first_chat.WRITTEN_PATH_ARGS.items() for d in ds}
    unclassified = [kd for kd in found if kd not in written and kd not in READ_PATH_ARGS]
    assert not unclassified, unclassified
    assert not (written & set(READ_PATH_ARGS)), written & set(READ_PATH_ARGS)
    # every line of both tables names an argument the parser has (bar `update`, main's command, not on this branch)
    have = {(k, d) for k, d, _s in _parser_args()}
    registered = cli.registered_verbs()
    stale = [kd for kd in written | set(READ_PATH_ARGS) if kd[0].split()[0] in registered and kd not in have]
    assert not stale, stale


def test_the_sub_command_attribute_of_every_command_is_the_one_the_gate_reads():
    subs = {k.split()[0]: s for k, _d, s in _parser_args() if s and len(k.split()) == 2}
    for verb, attr in subs.items():
        assert first_chat.SUB_DEST.get(verb, "%s_verb" % verb) == attr, (verb, attr)


def _other_studio(tmp_path, name, state):
    """A second studio next to the chat's, with its first chat in `state` ("done" or "due")."""
    root = tmp_path / name
    (root / ".alpaca" / "config").mkdir(parents=True)
    import shutil
    shutil.copy(os.path.join(REPO, "ALPACA-MANIFEST"), root / "ALPACA-MANIFEST")
    conn = db.connect(str(root))
    db.meta_set(conn, adopt.FIRST_CHAT, state)
    conn.commit()
    conn.close()
    return os.path.realpath(str(root))


def _ns_for(key, dest, value):
    parts = key.split()
    verb = parts[0]
    ns = argparse.Namespace(verb=verb, **{dest: value})
    if len(parts) == 2:
        setattr(ns, first_chat.SUB_DEST.get(verb, "%s_verb" % verb), parts[1])
    return ns


WRITTEN = sorted((k, d) for k, ds in first_chat.WRITTEN_PATH_ARGS.items() for d in ds)


@pytest.mark.parametrize("key,dest", WRITTEN, ids=["%s-%s" % kd for kd in WRITTEN])
def test_a_written_path_into_a_due_studio_is_refused_from_any_folder(chat, upstream, tmp_path, monkeypatch, key, dest):
    chat.look(upstream.url)
    agent = {"CLAUDE_CODE_SESSION_ID": chat.sid}
    # from a folder in no studio, and from a studio whose first chat is done
    away = tmp_path / "away"
    away.mkdir()
    done = _other_studio(tmp_path, "done", "done")
    value = chat.root if dest == "target" else os.path.join(chat.root, "sub", "x")
    for cwd in (str(away), done):
        monkeypatch.chdir(cwd)
        refused = first_chat.verb_refusal(_ns_for(key, dest, value), environ=agent)
        assert refused and refused[0] == TOKEN and chat.root in refused[1], (key, dest, cwd, refused)
        # the owner's own terminal is never refused
        assert first_chat.verb_refusal(_ns_for(key, dest, value), environ={}, isatty=lambda: True) is None
    # a path outside every studio is not refused for a listed command (an unlisted one run from the done studio
    # passes too: the done studio's first chat is closed)
    outside = os.path.join(str(away), "out")
    monkeypatch.chdir(done)
    assert first_chat.verb_refusal(_ns_for(key, dest, outside), environ=agent) is None


def test_mutation_control_upgrade_off_the_table_lets_upgrade_target_through(chat, upstream, tmp_path, monkeypatch):
    chat.look(upstream.url)
    away = tmp_path / "away"
    away.mkdir()
    monkeypatch.chdir(str(away))
    ns = argparse.Namespace(verb="upgrade", target=chat.root, source=None)
    agent = {"CLAUDE_CODE_SESSION_ID": chat.sid}
    assert first_chat.verb_refusal(ns, environ=agent)
    monkeypatch.delitem(first_chat.WRITTEN_PATH_ARGS, "upgrade")
    assert first_chat.verb_refusal(ns, environ=agent) is None, "the mutation went unseen: the table check tests nothing"


def test_a_relative_path_is_read_from_the_callers_folder_too(chat, upstream, tmp_path, monkeypatch):
    """bin/alpaca runs Python in the install root and passes the caller's folder as ALPACA_CALLER_CWD."""
    chat.look(upstream.url)
    away = tmp_path / "away"
    away.mkdir()
    monkeypatch.chdir(str(away))
    agent = {"CLAUDE_CODE_SESSION_ID": chat.sid, "ALPACA_CALLER_CWD": chat.root}
    refused = first_chat.verb_refusal(argparse.Namespace(verb="new", dir="inner", name="inner"), environ=agent)
    assert refused and refused[0] == TOKEN


def test_a_root_planted_inside_a_due_studio_does_not_hide_it(chat, upstream, monkeypatch):
    """A folder of the studio that holds its own ALPACA-MANIFEST is still inside the studio; under .alpaca/ it is not
    (design 7.1 lets .alpaca/ change, and a project's worktrees live there)."""
    chat.look(upstream.url)
    import shutil
    agent = {"CLAUDE_CODE_SESSION_ID": chat.sid}
    planted = os.path.join(chat.root, "sub")
    os.makedirs(planted)
    shutil.copy(os.path.join(chat.root, "ALPACA-MANIFEST"), os.path.join(planted, "ALPACA-MANIFEST"))
    monkeypatch.chdir(planted)
    refused = first_chat.verb_refusal(_ns("style"), environ=agent)
    assert refused and refused[0] == TOKEN
    under = os.path.join(chat.root, ".alpaca", "x")
    os.makedirs(under)
    shutil.copy(os.path.join(chat.root, "ALPACA-MANIFEST"), os.path.join(under, "ALPACA-MANIFEST"))
    monkeypatch.chdir(under)
    assert first_chat.verb_refusal(_ns("style"), environ=agent) is None


def test_onboard_look_scratch_in_another_due_studio_is_refused(chat, upstream, tmp_path):
    other = _other_studio(tmp_path, "other", "due")
    agent = {"CLAUDE_CODE_SESSION_ID": chat.sid}
    ns = argparse.Namespace(verb="onboard", onboard_verb="look", scratch=os.path.join(other, "scratch"))
    refused = first_chat.verb_refusal(ns, environ=agent)
    assert refused and refused[0] == TOKEN and other in refused[1]
    # its own studio keeps look's own rule (LOOK-SCRATCH-INSIDE), and a scratch outside every studio passes
    ns.scratch = os.path.join(chat.root, "scratch")
    assert first_chat.verb_refusal(ns, environ=agent) is None
    ns.scratch = str(tmp_path / "pad")
    assert first_chat.verb_refusal(ns, environ=agent) is None


def _away_child(c, argv, cwd, extra=None, timeout=600):
    env = _agent_env(c)
    env.pop("ALPACA_ROOT", None)
    env.update(extra or {})
    done = subprocess.run([sys.executable, "-m", "alpaca", *argv], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=timeout)
    return done.returncode, done.stdout + done.stderr


def test_Q1_upgrade_target_from_a_folder_outside_the_studio_is_refused(chat, upstream, tmp_path):
    """Round 4 probe Q1 (check-adversarial-r4/test_probe_r4.py:77): an ordinary agent call, markers set and no
    terminal, from a sibling folder."""
    chat.look(upstream.url)
    away = tmp_path / "away"
    away.mkdir()
    before = snapshot(chat.root)
    rc, out = _away_child(chat, ["upgrade", "--target", chat.root], str(away))
    assert rc == cli.FAIL and "[%s]" % TOKEN in out and chat.root in out, out[-600:]
    assert snapshot(chat.root) == before


def test_Q2_new_inside_the_studio_from_outside_any_root_is_refused(chat, tmp_path):
    """Round 4 probe Q2 (check-adversarial-r4/test_probe_r4.py:95)."""
    away = tmp_path / "away"
    away.mkdir()
    inner = os.path.join(chat.root, "inner")
    before = snapshot(chat.root)
    rc, out = _away_child(chat, ["new", "--name", "inner", "--dir", inner, "--yes", "--no-bootstrap", "--no-serve",
                                 "--no-git"], str(away), timeout=900)
    assert rc == cli.FAIL and "[%s]" % TOKEN in out, out[-600:]
    assert not os.path.exists(inner)
    assert snapshot(chat.root) == before


def test_Q1b_upgrade_target_and_source_from_outside_leave_a_new_studios_claude_settings(tmp_path):
    """Round 4 probe Q1b (check-adversarial-r4/test_probe_r4.py:187): a studio `alpaca new` made (first chat due),
    the studio's own code, and an Alpaca tree whose Claude settings differ as the source. With the current folder in
    the studio and outside it, the agent's upgrade is refused and the settings stay; the owner's own terminal (a
    pseudo-terminal, no markers) still upgrades it."""
    import shutil
    studio, src, away = tmp_path / "studio", tmp_path / "src", tmp_path / "elsewhere"
    away.mkdir()
    base = {k: v for k, v in os.environ.items() if k not in ownerword.AGENT_MARKERS
            and k not in ownerword.REAL_SESSION_VARS and k != "ALPACA_ROOT"}
    base.update(PYTHONPATH=REPO, CLAUDE_CONFIG_DIR=str(tmp_path / "cfg"))
    made = subprocess.run([sys.executable, "-m", "alpaca", "new", "--name", "s", "--dir", str(studio), "--who",
                           "o:owner", "--yes", "--no-bootstrap", "--no-serve"], cwd=str(tmp_path), env=base,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=900)
    assert made.returncode == 0, made.stdout[-500:] + made.stderr[-500:]
    conn = db.connect(str(studio))
    assert adopt.first_chat_state(conn) == "due" and first_chat.first_chat_open(conn, str(studio))
    conn.close()
    shutil.copytree(str(studio), str(src), ignore=shutil.ignore_patterns(".alpaca", ".git"))
    sp = src / ".claude" / "settings.json"
    cfg = json.loads(sp.read_text())
    cfg["agentPlanted"] = True
    sp.write_text(json.dumps(cfg, indent=2) + "\n")
    agent = dict(base, PYTHONPATH=str(studio), CLAUDECODE="1", CLAUDE_CODE_SESSION_ID="chat-q1b")
    argv = [sys.executable, "-m", "alpaca", "upgrade", "--target", str(studio), "--source", str(src)]
    before = snapshot(str(studio))
    for cwd in (str(studio), str(away)):
        r = subprocess.run(argv, cwd=cwd, env=agent, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=600)
        assert r.returncode == cli.FAIL and "[%s]" % TOKEN in r.stdout, (cwd, (r.stdout + r.stderr)[-400:])
        assert _scored(before, snapshot(str(studio))) == [], cwd
    assert "agentPlanted" not in (studio / ".claude" / "settings.json").read_text()
    # the owner's own terminal: no marker, no session, a terminal on stdin
    owner = dict(base, PYTHONPATH=str(studio))
    cmd = " ".join("'%s'" % a for a in argv)
    r = subprocess.run(["script", "-qec", cmd, "/dev/null"], cwd=str(away), env=owner, stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=600)
    assert TOKEN not in r.stdout, r.stdout[-400:]
    assert "agentPlanted" in (studio / ".claude" / "settings.json").read_text(), r.stdout[-600:]
