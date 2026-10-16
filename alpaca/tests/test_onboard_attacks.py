"""Attacks on the first chat, kept as tests (op-062 t-656, fix round 1).

Each test states the safe behaviour. The first chat must not run a program the repository wrote before the owner's yes
(a gate is tried only when the allow list in `onboard_look.classify_gate` says it parses and runs nothing), must not
write the studio's configuration or attach before the yes, and must apply exactly the card the owner confirmed. The
timing test pins the ask floor: a message typed in the same second as, but before, the second ask is not the reply."""
import datetime
import hashlib
import json
import os
import subprocess
import sys

import pytest

from alpaca import cli, db, ownerword, project, util
from alpaca.attach import config
from alpaca.tests import ownerword_fixtures as owf
from alpaca.tests.attachkit import git, make_upstream, upstream  # noqa: F401  (a fixture)
from alpaca.tests.onboardchat import Chat, assistant, chat  # noqa: F401  (a fixture)

WORKTREE = os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__)))


# ------------------------------------------------------------------------------------------ helpers

def snapshot(root):
    """sha256 of every file of the studio outside .alpaca/ and .git/, plus .git/config and .git/info/exclude."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        if rel.split(os.sep)[0] in (".alpaca", ".git"):
            dirnames[:] = []
            continue
        for f in filenames:
            p = os.path.join(dirpath, f)
            if os.path.islink(p):
                out[os.path.relpath(p, root)] = "link:" + os.readlink(p)
                continue
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = hashlib.sha256(fh.read()).hexdigest()
    for extra in (".git/config", ".git/info/exclude"):
        p = os.path.join(root, extra)
        if os.path.isfile(p):
            with open(p, "rb") as fh:
                out[extra] = hashlib.sha256(fh.read()).hexdigest()
    return out


def make_repo(base, name, files, links=None, modes=None):
    """A bare repo (returned path) with one commit of `files`, symlinks `links` {path: target} and modes."""
    work = os.path.join(str(base), name + "-work")
    bare = os.path.join(str(base), name + ".git")
    os.makedirs(work)
    git(work, "init", "-q", "-b", "main")
    for rel, text in files.items():
        p = os.path.join(work, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
    for rel, target in (links or {}).items():
        p = os.path.join(work, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        os.symlink(target, p)
    for rel, mode in (modes or {}).items():
        os.chmod(os.path.join(work, rel), mode)
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "first")
    git(base, "clone", "-q", "--bare", work, bare)
    return bare


def look_and_extra_gate(chat, src, argv, name="syntax"):
    """The first chat looks at `src`, then the agent records the gate a repo file 'points to'."""
    chat.look(src)
    return chat.run("onboard", "answer", "gate.extra", "--name", name, "--argv-json", json.dumps(argv),
                    "--reason", "README.md says: check the syntax with this command", "--source", "agent")


def confirmed(chat, url):
    cid, card = chat.walk_to_card(url)
    chat.say("yes")
    rc, out = chat.run("onboard", "confirm", cid, "--quote", "yes")
    assert rc == 0, out
    return cid, card


def cards_dir(chat):
    return os.path.join(chat.root, ".alpaca", "onboard", "cards")


def child(root, argv, env_extra=None, timeout=120):
    env = dict(os.environ)
    env.update(PYTHONPATH=WORKTREE)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "alpaca", *argv], cwd=root, env=env, capture_output=True, text=True,
                          timeout=timeout)


# --------------------------------------------------------------- the ask floor (rehearsal flake, B1)

class SecondsClock:
    """The real clock's shape: an event carries whole seconds, `precise` has microseconds. Time moves 0.1 s per reading."""

    def __init__(self, start, step=0.1):
        self.t, self.step = start, datetime.timedelta(seconds=step)

    def _tick(self):
        self.t += self.step
        return self.t

    def __call__(self):
        return self._tick().isoformat(timespec="seconds")

    def precise(self):
        return self._tick().isoformat(timespec="microseconds")


def test_a_reply_typed_in_the_same_second_before_the_second_ask_is_not_the_reply_to_it(chat, upstream):  # noqa: F811
    cid1, card1 = chat.walk_to_card(upstream.url)
    start = (chat.clock.t + datetime.timedelta(seconds=100)).replace(microsecond=0)
    clock = SecondsClock(start)
    util.set_clock(clock)
    trap = "yes, but use L2"
    chat._append(owf.typed(trap, ts=(start + datetime.timedelta(milliseconds=50)).isoformat(timespec="microseconds")))
    rc, out = chat.run("onboard", "confirm", cid1, "--quote", trap)
    assert rc == 1 and "ONBOARD-NOT-A-YES" in out
    rc, out = chat.run("onboard", "answer", "level-override", "--value", "L2", "--quote", trap)
    assert rc == 0, out
    rc, card2 = chat.run("onboard", "propose")
    assert rc == 0, card2
    cid2 = card2.split()[2]
    chat._append(assistant(card2.rstrip("\n"), clock.precise()))
    chat._append(owf.typed("yes", ts=clock.precise()))
    rc, out = chat.run("onboard", "confirm", cid2, "--quote", "yes")
    assert rc == 0, out
    ask = ownerword.latest_ask(chat.conn, cid2)
    assert ask["ts"] == start.isoformat(timespec="seconds"), "the ask must fall in the second of the trap line (the premise)"
    assert ownerword.ask_floor(ask).startswith(start.isoformat()[:19] + ".") and ownerword.ask_floor(ask) > ask["ts"]


def test_an_ask_recorded_without_a_precise_time_falls_back_to_its_event_time(chat):  # noqa: F811
    row = {"ts": "2026-10-14T10:00:05+00:00", "data": json.dumps({"subject": "x"})}
    assert ownerword.ask_floor(row) == "2026-10-14T10:00:05+00:00"
    row = {"ts": "2026-10-14T10:00:05+00:00", "data": json.dumps({"subject": "x", "at": "not a time"})}
    assert ownerword.ask_floor(row) == "2026-10-14T10:00:05+00:00"
    row = {"ts": "2026-10-14T10:00:05+00:00", "data": json.dumps({"subject": "x", "at": "2026-10-14T10:00:05.700000+00:00"})}
    assert ownerword.ask_floor(row) == "2026-10-14T10:00:05.700000+00:00"


# --------------------------------------------------------- gates tried before the yes (D1, D3: A1 to A4)

def test_A1_a_repo_script_named_gcc_is_not_run_before_the_yes(chat, tmp_path):  # noqa: F811
    marker = str(tmp_path / "PWNED-A1")
    src = make_repo(tmp_path, "evil-a1", {"README.md": "check syntax: tools/gcc -fsyntax-only main.c\n",
                                          "main.c": "int main(void){return 0;}\n",
                                          "tools/gcc": "#!/bin/sh\ntouch %s\nexit 0\n" % marker},
                    modes={"tools/gcc": 0o755})
    before = snapshot(chat.root)
    rc, out = look_and_extra_gate(chat, src, ["tools/gcc", "-fsyntax-only", "main.c"])
    assert not os.path.exists(marker), out
    assert snapshot(chat.root) == before


def test_A1b_a_repo_script_found_through_a_path_entry_in_the_clone_is_not_run(chat, tmp_path, monkeypatch):  # noqa: F811
    """A PATH entry that is empty, relative or inside the clone must not resolve the bare name `gcc` to the repo."""
    marker = str(tmp_path / "PWNED-A1b")
    src = make_repo(tmp_path, "evil-a1b", {"main.c": "int main(void){return 0;}\n",
                                           "bin/gcc": "#!/bin/sh\ntouch %s\nexit 0\n" % marker}, modes={"bin/gcc": 0o755})
    monkeypatch.setenv("PATH", ":bin:.:" + os.environ["PATH"])
    rc, out = look_and_extra_gate(chat, src, ["gcc", "-fsyntax-only", "main.c"])
    assert not os.path.exists(marker), out


def test_A2_gcc_specs_from_the_repo_run_nothing_before_the_yes(chat, tmp_path):  # noqa: F811
    marker = str(tmp_path / "PWNED-A2")
    src = make_repo(tmp_path, "evil-a2", {"README.md": "syntax: gcc --specs=tools/check.specs -fsyntax-only src/x.zz\n",
                                          "src/x.zz": "int a;\n", "tools/check.specs": ".zz:\ntouch %s\n" % marker})
    rc, out = look_and_extra_gate(chat, src, ["gcc", "--specs=tools/check.specs", "-fsyntax-only", "src/x.zz"])
    assert not os.path.exists(marker), out


def test_A3_a_dependency_file_flag_cannot_write_a_studio_file_before_the_yes(chat, tmp_path):  # noqa: F811
    src = make_repo(tmp_path, "evil-a3", {"README.md": "x\n", "main.c": "int main(void){return 0;}\n"})
    target = os.path.join(chat.root, "project.yaml")
    before = snapshot(chat.root)
    rc, out = look_and_extra_gate(chat, src, ["gcc", "-fsyntax-only", "-MD", "-MF", target, "main.c"])
    assert snapshot(chat.root) == before, out


def test_a_gate_that_parses_is_still_tried_and_its_result_kept(chat, tmp_path):  # noqa: F811
    src = make_repo(tmp_path, "ok-c", {"README.md": "x\n", "main.c": "int main(void){return 0;}\n"})
    rc, out = look_and_extra_gate(chat, src, ["gcc", "-fsyntax-only", "-Wall", "main.c"])
    assert rc == 0, out
    ans = [e for e in chat.events("onboard-answer") if e["data"].get("slot") == "gate.extra"][-1]["data"]
    assert ans["classify"] == "parse" and ans["trial"]["exit"] == 0, ans


def test_A4_the_py_syntax_trial_does_not_read_through_a_symlink_to_dev_zero(tmp_path):
    """A tracked `z.py -> /dev/zero` made the trial read without end. Run in a child with RLIMIT_AS 2 GiB so the host is
    safe: an unbounded read would show as MemoryError in the trial tail."""
    from alpaca import onboard_look as ol
    src = make_repo(tmp_path, "evil-a4", {"README.md": "x\n", "ok.py": "x = 1\n"}, links={"z.py": "/dev/zero"})
    studio = tmp_path / "studio-a4"
    studio.mkdir()
    code = ("import resource,json,sys;resource.setrlimit(resource.RLIMIT_AS,(2<<30,2<<30));"
            "from alpaca import onboard_look as ol;"
            "p=ol.look(sys.argv[1],sys.argv[2],scratch=sys.argv[3],now='2026-10-14',trial_timeout=20);"
            "print(json.dumps([(g['name'],g['trial']) for g in p['gates']]))")
    done = subprocess.run([sys.executable, "-c", code, str(studio), src, str(tmp_path / "scr-a4")], capture_output=True,
                          text=True, timeout=120, env=dict(os.environ, PYTHONPATH=WORKTREE))
    gates = json.loads(done.stdout.strip().splitlines()[-1])
    trial = dict(gates)["py-syntax"]
    assert trial["exit"] == 0 and "parsed 1 files" in trial["tail"], trial
    assert ol.PY_SYNTAX[-1].count("islink") == 1


def _tree(base):
    """(path, size, mtime, link target) of everything under `base`, to compare before and after."""
    out = []
    for dirpath, dirnames, filenames in os.walk(str(base)):
        for name in sorted(dirnames + filenames):
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            out.append((p, st.st_size, st.st_mtime_ns, os.readlink(p) if os.path.islink(p) else None))
    return sorted(out)


def test_a_sample_of_gates_the_allow_list_calls_inert_run_nothing_and_write_nothing(tmp_path):
    """The probe behind the sentence 'a gate tried before the yes runs nothing the repository wrote': 600 argv built at
    random (fixed seed) from flags that name a program, a plug-in, a specs or config file, an output or a dependency
    file; every one the allow list calls `parse` is run with a real compiler against a repository whose files are set up
    to leave a mark if any of them is used. No mark and no change anywhere under the temp folder."""
    import random
    import shutil
    from alpaca import onboard_look as ol
    if not (shutil.which("gcc") and shutil.which("clang")):
        pytest.skip("needs gcc and clang on the PATH")
    base = tmp_path / "fz"
    clone = base / "clone"
    (clone / "tools").mkdir(parents=True)
    (clone / "inc").mkdir()
    (clone / "bin").mkdir()
    mark = base / "MARK"
    script = "#!/bin/sh\ntouch %s\nexit 0\n" % mark
    for rel, body in (("a.c", "#include \"h.h\"\nint main(void){return H;}\n"), ("b.c", "int b;\n"), ("inc/h.h", "#define H 0\n"),
                      ("tools/s.specs", "*cc1:\n+ -DS\n\n.zz:\n%s\n" % script.replace("\n", " ")), ("c.cfg", "-DC\n"),
                      ("bin/gcc", script), ("bin/clang", script), ("tools/gcc", script), ("p.so", "x")):
        (clone / rel).write_text(body)
        if rel in ("bin/gcc", "bin/clang", "tools/gcc"):
            os.chmod(str(clone / rel), 0o755)
    good = ["-w", "-pedantic", "-Wall", "-Wextra", "-Werror=format", "-std=c11", "-DX=1", "-UX", "-Iinc", "-I", "inc", "-x", "c",
            "-xc", "a.c", "b.c", "inc/h.h"]
    bad = ["--specs=tools/s.specs", "-specs=tools/s.specs", "-MD", "-MMD", "-MF", "out.d", "-MT", "t", "-o", "out.o", "-c", "-S",
           "-E", "-fplugin=./p.so", "-fpass-plugin=./p.so", "-B", "tools", "-Btools", "-Wp,-MD,leak.d", "-Wl,--wrap=x", "-Xclang",
           "-load", "@c.cfg", "--config=c.cfg", "--config", "c.cfg", "-save-temps", "-fdump-tree-all", "-wrapper", "tools/gcc",
           "-###", "-print-prog-name=cc1", "-dumpspecs", "-v", "-pipe", "-gen-cdb-fragment-path", "x", "-x", "assembler", "../a.c",
           "/etc/passwd.c", "a.o", "-I..", "-I/etc", "-D$(touch x)", "-Wno-error=foo,bar"]
    rng = random.Random(656)
    before = _tree(base)
    ran = 0
    for _ in range(600):
        argv = [rng.choice(("gcc", "clang", "gcc", "tools/gcc", "./gcc", "bin/clang"))]
        argv += ["-fsyntax-only"] + [rng.choice(good) for _ in range(rng.randint(0, 4))] + ["a.c"]
        if rng.random() < 0.6:
            argv.insert(rng.randint(1, len(argv)), rng.choice(bad))
        if ol.classify_gate(argv, str(clone), str(tmp_path / "studio")) != "parse":
            continue
        res = ol.trial(str(clone), {"command": argv}, root=str(tmp_path / "studio"))
        ran += 1
        assert res["exit"] != 126, argv
        assert not mark.exists(), "a command the allow list called inert ran a repository file: %s" % argv
        assert _tree(base) == before, "a command the allow list called inert changed files: %s" % argv
    assert ran >= 50, "the sample must reach the trial often enough to mean something (%d)" % ran


def test_a_trial_runs_with_memory_time_and_file_limits(tmp_path):
    """The limits are set in the child: a program that writes a file or takes more memory than the cap is stopped."""
    from alpaca import onboard_look as ol
    probe = ("import resource;print(resource.getrlimit(resource.RLIMIT_AS)[0], resource.getrlimit(resource.RLIMIT_FSIZE)[0],"
             "resource.getrlimit(resource.RLIMIT_CPU)[0])")
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, preexec_fn=ol._limits)
    as_, fsize, cpu = (int(x) for x in done.stdout.split())
    assert as_ == 2 << 30 and fsize == 0 and cpu == ol.TRIAL_TIMEOUT + 10


def test_a_command_that_is_not_inert_is_not_run_by_trial(tmp_path):
    from alpaca import onboard_look as ol
    marker = tmp_path / "ran"
    res = ol.trial(str(tmp_path), {"command": ["sh", "-c", "touch %s" % marker]})
    assert res["exit"] == 126 and not marker.exists()


# ------------------------------------------------ the inputs the yes binds (D2: B1, B1b, an honest second propose)

def _edit_inputs(chat, cid, change):
    path = os.path.join(cards_dir(chat), cid + ".json")
    with open(path, encoding="utf-8") as fh:
        inputs = json.load(fh)
    change(inputs)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(inputs, fh)


def test_B1_apply_refuses_inputs_that_changed_after_the_yes(chat, upstream, tmp_path):  # noqa: F811
    cid, card = confirmed(chat, upstream.url)
    (tmp_path / "other-up").mkdir()
    other = make_upstream(tmp_path / "other-up", name="other")

    def change(inputs):
        inputs["settings"]["source"] = other.url
        inputs["settings"]["level"] = "L6"
    _edit_inputs(chat, cid, change)
    rc, out = chat.run("onboard", "apply", cid)
    assert rc != 0 and "ONBOARD-COPY-CHANGED" in out, out
    assert config.mode(chat.root) is None and project.load(chat.root).get("default_level") != "L6"
    assert not [e for e in chat.events("onboard-applied")]


def test_B1_a_change_to_a_field_the_card_does_not_show_is_refused_too(chat, upstream):  # noqa: F811
    cid, card = confirmed(chat, upstream.url)
    _edit_inputs(chat, cid, lambda i: i["look"].__setitem__("clone", "/etc"))
    rc, out = chat.run("onboard", "apply", cid)
    assert rc != 0 and "ONBOARD-COPY-CHANGED" in out, out


def test_B1b_attach_init_card_refuses_inputs_that_changed_after_the_yes(chat, upstream):  # noqa: F811
    cid, card = confirmed(chat, upstream.url)
    _edit_inputs(chat, cid, lambda i: i["settings"].__setitem__("gates", [{"name": "sneaky", "command": ["sh", "-c", "x"],
                                                                           "timeout": 60}]))
    rc, out = chat.run("attach", "init", upstream.url, "--card", cid)
    assert rc != 0 and "ATTACH-CARD-CHANGED" in out, out
    assert config.mode(chat.root) is None


def test_inputs_edited_between_the_ask_and_the_yes_are_refused_at_the_confirm(chat, upstream):  # noqa: F811
    cid, card = chat.walk_to_card(upstream.url)
    _edit_inputs(chat, cid, lambda i: i["settings"].__setitem__("level", "L6"))
    chat.say("yes")
    rc, out = chat.run("onboard", "confirm", cid, "--quote", "yes")
    assert rc != 0 and "ONBOARD-COPY-CHANGED" in out, out
    assert not chat.events("onboard-confirmed")


def test_a_card_proposed_again_with_the_same_text_and_new_answer_events_still_applies(chat, upstream):  # noqa: F811
    """propose rewrites the inputs file every time but records the card only once: the hash that binds the yes is on the
    ask, so an honest second propose (same card text, other answer event ids) must not break apply."""
    cid, card = chat.walk_to_card(upstream.url)
    first = chat.events("onboard-card")
    rc, out = chat.run("onboard", "answer", "goal", "--value", chat.GOAL, "--quote", chat.REPLY)
    assert rc == 0, out
    cid2, card2 = chat.propose()
    assert cid2 == cid and len(chat.events("onboard-card")) == len(first)
    asks = [e for e in chat.events("owner-ask") if e["ref"] == cid]
    assert len(asks) == 2 and asks[0]["data"]["inputs_sha256"] != asks[1]["data"]["inputs_sha256"]
    chat.say("yes")
    rc, out = chat.run("onboard", "confirm", cid, "--quote", "yes")
    assert rc == 0, out
    rc, out = chat.run("onboard", "apply", cid)
    assert rc == 0, out
    assert config.mode(chat.root).url == upstream.url


def test_propose_reask_keeps_the_inputs_hash_and_refuses_when_the_file_changed(chat, upstream):  # noqa: F811
    cid, card = chat.walk_to_card(upstream.url)
    rc, out = chat.run("onboard", "propose", "--reask")
    assert rc == 0, out
    a, b = [e["data"]["inputs_sha256"] for e in chat.events("owner-ask") if e["ref"] == cid]
    assert a == b
    _edit_inputs(chat, cid, lambda i: i["settings"].__setitem__("level", "L6"))
    rc, out = chat.run("onboard", "propose", "--reask")
    assert rc != 0 and "ONBOARD-COPY-CHANGED" in out, out


# ------------------------------------------- an agent session sets nothing up and attaches nothing mid chat (D4, D5)

def test_B3_an_agent_attach_in_a_due_studio_is_refused_whatever_the_environment_says(chat, upstream, monkeypatch):  # noqa: F811
    before = snapshot(chat.root)
    monkeypatch.setenv("ALPACA_NEW_ATTACH", "1")
    rc, out = chat.run("attach", "init", upstream.url)
    assert rc != 0 and "ATTACH-NEEDS-CARD" in out, out
    assert snapshot(chat.root) == before


def test_B3_a_marker_with_no_session_id_is_an_agent_too(chat, upstream, monkeypatch):  # noqa: F811
    for name in ownerword.REAL_SESSION_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDECODE", "1")
    rc, out = chat.run("attach", "init", upstream.url)
    assert rc != 0 and "ATTACH-NEEDS-CARD" in out, out


def test_new_attach_inside_an_agent_session_is_refused_before_anything_is_made(tmp_path, monkeypatch, upstream):  # noqa: F811
    monkeypatch.setenv("CLAUDECODE", "1")
    target = tmp_path / "studio"
    rc = cli.main(["new", "--name", "X", "--dir", str(target), "--attach", upstream.url, "--who", "t:owner", "--yes",
                   "--no-bootstrap", "--no-serve"])
    assert rc == cli.FAIL and not target.exists()


def test_new_attach_from_a_plain_terminal_still_works(tmp_path, upstream, capsys, pty_stdin):  # noqa: F811
    target = tmp_path / "studio"
    rc = cli.main(["new", "--name", "X", "--dir", str(target), "--attach", upstream.url, "--who", "t:owner", "--yes",
                   "--no-bootstrap", "--no-serve"])
    assert rc == 0, capsys.readouterr().out
    assert (target / "app" / ".git").is_dir()


@pytest.fixture
def fresh_chat(project, monkeypatch):
    """A studio in the `fresh` state: the record made, not onboarded, no first_chat mark."""
    c = Chat(project, monkeypatch, studio=False)
    git(c.root, "init", "-q")
    assert cli.main(["init"]) == 0
    c.conn = db.connect(c.root)
    yield c
    c.close()


def test_B4b_a_plain_onboard_from_the_agent_is_refused_while_the_first_chat_is_open(fresh_chat, upstream):  # noqa: F811
    c = fresh_chat
    c.look(upstream.url)
    before = snapshot(c.root)
    rc, out = c.run("onboard", "--name", "x", "--who", "a:owner", "--what", "w", "--existing-repo")
    assert rc != 0 and "ONBOARD-FIRST-CHAT-OPEN" in out and "onboard propose" in out and "onboard skip" in out, out
    assert snapshot(c.root) == before and not project.is_onboarded(c.root)


def test_B4b_an_attach_from_the_agent_is_refused_while_the_first_chat_is_open_in_a_fresh_studio(fresh_chat, upstream):  # noqa: F811
    c = fresh_chat
    c.look(upstream.url)
    before = snapshot(c.root)
    rc, out = c.run("attach", "init", upstream.url)
    assert rc != 0 and "ATTACH-NEEDS-CARD" in out, out
    assert snapshot(c.root) == before


def test_a_plain_onboard_from_the_agent_still_works_when_no_first_chat_was_opened(fresh_chat):
    c = fresh_chat
    rc, out = c.run("onboard", "--name", "x", "--who", "a:owner", "--what", "w")
    assert rc == 0, out
    assert project.is_onboarded(c.root)


def test_the_owner_at_a_plain_terminal_may_still_onboard_by_hand_mid_chat(fresh_chat, upstream, monkeypatch):  # noqa: F811
    c = fresh_chat
    c.look(upstream.url)
    for name in list(ownerword.AGENT_MARKERS):
        monkeypatch.delenv(name, raising=False)
    rc, out = c.run("onboard", "--name", "x", "--who", "a:owner", "--what", "w")
    assert rc == 0, out


def test_after_the_owner_skips_the_first_chat_the_flat_onboard_works_again(fresh_chat, upstream):  # noqa: F811
    """The sentence in the first-chat skill: skip ends the first chat on the owner's word, and the flat form then sets up."""
    c = fresh_chat
    c.look(upstream.url)
    text = c.say("skip setup")
    rc, out = c.run("onboard", "skip", "--quote", text)
    assert rc == 0, out
    rc, out = c.run("onboard", "--name", "x", "--who", "a:owner", "--what", "w")
    assert rc == 0, out


# ------------------------------------------- E3: the inputs are read once, hashed, and parsed from those bytes

def test_E3_the_inputs_the_owner_confirmed_are_the_bytes_that_are_parsed_even_if_the_file_is_swapped_after_the_read(
        chat, upstream, tmp_path, monkeypatch):  # noqa: F811
    from alpaca import onboard_flow
    cid, card = confirmed(chat, upstream.url)
    path = os.path.join(cards_dir(chat), cid + ".json")
    good = open(path, "rb").read()
    evil = json.loads(good)
    evil["settings"]["source"] = "file:///elsewhere"
    evil["settings"]["level"] = "L6"
    real, reads = onboard_flow._read_regular, []

    def swapping(p):
        data = real(p)
        if p == path:
            reads.append(1)
            with open(p, "wb") as fh:                      # the attacker's write lands right after the read
                fh.write(json.dumps(evil).encode("utf-8"))
        return data

    monkeypatch.setattr(onboard_flow, "_read_regular", swapping)
    inputs = onboard_flow.checked_inputs(chat.conn, chat.root, cid)
    assert len(reads) == 1, "the inputs file was opened %d times" % len(reads)
    assert inputs["settings"]["source"] == upstream.url and inputs["settings"]["level"] != "L6"
    # the file now holds other bytes: the next read is refused
    monkeypatch.setattr(onboard_flow, "_read_regular", real)
    with pytest.raises(onboard_flow.Refused, match="ONBOARD-COPY-CHANGED"):
        onboard_flow.checked_inputs(chat.conn, chat.root, cid)


def test_E3_a_link_a_pipe_or_a_folder_in_place_of_the_inputs_is_refused_without_blocking(chat, upstream, tmp_path):  # noqa: F811
    import signal
    from alpaca import onboard_flow
    cid, card = confirmed(chat, upstream.url)
    path = os.path.join(cards_dir(chat), cid + ".json")
    good = open(path, "rb").read()
    kept = tmp_path / "kept.json"
    kept.write_bytes(good)
    def clear():
        if os.path.isdir(path) and not os.path.islink(path):
            os.rmdir(path)
        elif os.path.lexists(path):
            os.remove(path)

    signal.alarm(20)                                       # a read that waits for a writer fails the test, not the run
    try:
        for make in (lambda: os.symlink(str(kept), path), lambda: os.mkfifo(path), lambda: os.mkdir(path)):
            clear()
            make()
            with pytest.raises(onboard_flow.Refused, match="ONBOARD-COPY-CHANGED"):
                onboard_flow.checked_inputs(chat.conn, chat.root, cid)
            assert onboard_flow._read_regular(path) is None
    finally:
        signal.alarm(0)
    clear()
    kept.rename(path)
    assert onboard_flow._read_regular(path) == good


# ------------------------------------------- E4: a trial runs with an environment built from a fixed list

def test_E4_a_trial_sees_only_the_listed_variables_and_an_empty_home(tmp_path, monkeypatch):
    from alpaca import onboard_look as ol
    clone, studio, bindir = tmp_path / "scratch" / "clone", tmp_path / "studio", tmp_path / "bin"
    for d in (clone, studio, bindir):
        d.mkdir(parents=True)
    (clone / "a.c").write_text("int main(void){return 0;}\n")
    fake = bindir / "gcc"
    fake.write_text("#!/bin/sh\n"
                    "bad=$(env | cut -d= -f1 | grep -v -x -E 'PATH|LC_ALL|LANG|HOME|TMPDIR|GIT_CONFIG_NOSYSTEM|GIT_CONFIG_GLOBAL|"
                    "GIT_TERMINAL_PROMPT|PWD|SHLVL|OLDPWD|_' | tr '\\n' ' ')\n"
                    "[ -z \"$bad\" ] || { echo \"extra: $bad\"; exit 3; }\n"
                    "[ \"$HOME\" = \"$TMPDIR\" ] && same=yes || same=no\n"
                    "echo \"empty=$(ls -A \"$HOME\" | wc -l) same=$same home=$HOME\"\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", "%s%s%s" % (bindir, os.pathsep, os.environ.get("PATH", "")))
    for name, value in (("COMPILER_PATH", "tools"), ("GCC_EXEC_PREFIX", "tools/"), ("LD_PRELOAD", "x.so"), ("PYTHONPATH", "x"),
                        ("GIT_EXEC_PATH", "x"), ("GIT_SSH_COMMAND", "x"), ("DEPENDENCIES_OUTPUT", "x"), ("GITHUB_TOKEN", "t"),
                        ("CPATH", "x"), ("LIBRARY_PATH", "x"), ("TOKEN_X", "x")):
        monkeypatch.setenv(name, value)
    res = ol.trial(str(clone), {"command": ["gcc", "-fsyntax-only", "a.c"]}, root=str(studio))
    assert res["exit"] == 0, res
    home = res["tail"].split("home=", 1)[1].strip()
    assert res["tail"].startswith("empty=0 same=yes"), res
    assert not home.startswith(str(clone)) and not home.startswith(str(studio))
    assert not os.path.exists(home), "the trial's home folder was left behind"


def test_E4_the_environment_of_a_trial_is_the_listed_names_only(tmp_path):
    from alpaca import onboard_look as ol
    env = ol.trial_env(str(tmp_path))
    assert sorted(env) == ["GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT", "HOME", "LANG", "LC_ALL", "PATH",
                           "TMPDIR"]
    assert env["HOME"] == env["TMPDIR"] == str(tmp_path) and env["LC_ALL"] == env["LANG"] == "C"
