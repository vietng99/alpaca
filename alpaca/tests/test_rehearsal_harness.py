"""The AC-20 rehearsal harness (op-062 t-656): personas, fixtures, driver and scorer, tested on a scripted run.

The three-persona rehearsal itself (agents playing the host and the newcomers, a fresh judge) is run by the
main session after the merge, with `alpaca/tests/rehearsal/README.md`. These tests prove the pieces it relies
on: the driver builds a studio whose transcript the owner-word checks accept, the wrapper runs the studio's own
CLI as the rehearsal session, and the scorer computes its rows from what the run recorded."""
import datetime
import json
import os
import re
import subprocess

import pytest

from alpaca.tests.rehearsal import driver, fixtures, score

TODAY = datetime.date(2026, 10, 14)


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    """A scripted P2 rehearsal: the host and the persona are lines in this test, the studio is real."""
    scratch = tmp_path_factory.mktemp("rehearsal")
    meta = driver.make_run(str(scratch), "P2", today=TODAY.isoformat())
    rd = meta["run_dir"]

    def host(*args):
        done = subprocess.run([meta["wrapper"], *args], capture_output=True, text=True, timeout=120)
        return done.returncode, done.stdout, done.stderr

    persona = driver.load_persona("P2")
    opening = persona["public"]["opening"].replace("{repo_path}", meta["repo_path"])
    driver.append_owner(rd, opening)
    rc, out, err = host("onboard", "look", meta["repo_path"], "--scratch", meta["scratch"], "--quote", opening)
    assert rc == 0, out + err
    driver.snapshot(rd, "after-look")
    driver.append_host(rd, "I read your repo. Questions: 1. goal 2. who merges 3. limits")
    reply = ("1) add CSV export to the report command. 2a - Hana the team lead merges. 3) The repo must not get any Alpaca "
             "file, and their CI must stay green. Branches follow feature/<ticket>")
    driver.append_owner(rd, reply)
    for args in (["goal", "--value", "add CSV export to the report command"], ["merges", "--option", "a"],
                 ["limits", "--value", "The repo must not get any Alpaca file, and their CI must stay green"]):
        rc, out, err = host("onboard", "answer", *args, "--quote", reply)
        assert rc == 0, out + err
    rc, out, err = host("onboard", "answer", "done-guess", "--value", "report --csv writes the same rows, with a test",
                        "--source", "agent")
    assert rc == 0, out + err
    rc, out, err = host("onboard", "next")
    assert rc == 0, out + err
    rc, card1, err = host("onboard", "propose")
    assert rc == 0, card1 + err
    driver.append_host(rd, card1.rstrip("\n"))
    driver.snapshot(rd, "after-card")
    trap = persona["sealed"]["trap_reply"]
    driver.append_owner(rd, trap)
    cid1 = card1.split()[2]
    rc, out, err = host("onboard", "confirm", cid1, "--quote", trap)
    assert rc == 1 and "ONBOARD-NOT-A-YES" in out
    rc, out, err = host("onboard", "answer", "level-override", "--value", "L3", "--quote", trap)
    assert rc == 0, out + err
    rc, card2, err = host("onboard", "propose")
    assert rc == 0 and "Parts that changed" in err
    cid2 = card2.split()[2]
    parts = {p.split(".")[0]: p for p in card2.split("\n\n")}
    driver.append_host(rd, "Changed (level 3 as you asked):\n\n%s\n\n%s\n\n(code %s) Reply yes to set this up."
                       % (parts["7"], parts["10"], cid2))
    driver.append_owner(rd, "yes")
    rc, out, err = host("onboard", "confirm", cid2, "--quote", "yes")
    assert rc == 0, out + err
    driver.snapshot(rd, "after-confirm")
    rc, out, err = host("onboard", "apply", cid2)
    assert rc == 0, out + err
    driver.append_host(rd, "Set up. The studio (the folder you made with alpaca new) now holds your settings.")
    driver.finish(rd)
    return meta, cid1, cid2


def test_the_personas_have_a_public_part_and_a_sealed_part_and_the_three_situations():
    got = {}
    for pid in ("P1", "P2", "P3"):
        p = driver.load_persona(pid)
        assert set(p) >= {"id", "name", "repo", "expected_situation", "expected_mode", "public", "sealed"}
        assert p["public"]["opening"] and p["sealed"]["goal"] and p["sealed"]["limits"] and p["sealed"]["traps"]
        got[pid] = (p["expected_situation"], p["expected_mode"])
        view = driver.public_view(p, {"repo_path": "/x/repo"})
        assert "sealed" not in json.dumps(view) and p["sealed"]["goal"] not in json.dumps(view)
    assert got == {"P1": ("take-over", "attach"), "P2": ("team", "attach"), "P3": ("start-empty", "native")}
    assert driver.load_persona("P2")["sealed"]["trap_reply"] == "yes, but use L3"


def test_the_team_repo_fixture_has_the_facts_the_persona_relies_on_on_any_day(tmp_path):
    for today in (TODAY, datetime.date(2027, 3, 1)):
        path = fixtures.build_team_repo(tmp_path / ("t%s" % today), today)
        authors = fixtures.git(path, "log", "--format=%an|%ad", "--date=short").splitlines()
        days = sorted((today - datetime.date.fromisoformat(l.split("|")[1])).days for l in authors)
        assert days == [3, 9, 17, 26] and len({l.split("|")[0] for l in authors}) == 4
        files = fixtures.git(path, "ls-files").split()
        for needed in (".github/workflows/ci.yml", "CODEOWNERS", "CONTRIBUTING.md", "AGENTS.md", ".husky/pre-push", "tests/test_cli.py"):
            assert needed in files
        assert "feature/<ticket>" in open(os.path.join(path, "CONTRIBUTING.md")).read()


def test_the_qtrm64_mirror_is_a_bare_copy_of_a_clone(tmp_path):
    src = fixtures.build_team_repo(tmp_path / "src", TODAY)
    mirror = fixtures.mirror_qtrm64(tmp_path / "mirror.git", src)
    assert fixtures.git(mirror, "rev-parse", "--is-bare-repository") == "true"
    assert fixtures.git(mirror, "rev-parse", "main") == fixtures.git(src, "rev-parse", "main")


def test_the_driver_built_a_studio_a_transcript_the_checks_accept_and_a_wrapper(run):
    meta, cid1, cid2 = run
    assert os.path.isfile(meta["wrapper"]) and os.access(meta["wrapper"], os.X_OK)
    assert meta["transcript"].endswith("/projects/%s/reh-P2.jsonl" % __import__("alpaca.ownerword", fromlist=["x"]).project_slug(meta["studio"]))
    log = open(os.path.join(meta["run_dir"], "commands.log")).read()
    assert "onboard look" in log and "onboard apply %s" % cid2 in log
    evs = driver.events(meta["run_dir"])
    assert all(d.get("rehearsal") is True for _i, k, _a, d in evs if k.startswith("onboard"))
    st = driver.state(meta["run_dir"])
    assert st["applied"] and st["confirmed"] and st["latest_card"] == cid2 and st["owner_lines"] == 4
    assert [who for who, _ in driver.turns(meta["run_dir"])][:2] == ["owner", "host"]
    assert os.path.isfile(os.path.join(meta["run_dir"], "transcript.md")) and os.path.isdir(os.path.join(meta["run_dir"], "cards"))
    # the verbs ran as the rehearsal session, in the studio, and nothing leaked into the install that ran the test
    assert {e[2] for e in evs if e[1] == "onboard-confirmed"} == {"owner"}


def test_every_event_of_a_rehearsal_carries_the_mark_not_only_the_first_chat_verbs(run):
    """Round 1 left attach-init, attach-baseline, attach-study, note and decision unmarked: the mark is set where an
    event is written, not by each verb."""
    meta, _a, _b = run
    evs = driver.events(meta["run_dir"])
    assert {k for _i, k, _a2, _d in evs} >= {"onboard-applied", "attach-init"}
    assert [(i, k, a) for i, k, a, d in evs if d.get("rehearsal") is not True] == []
    out = open(os.path.join(meta["run_dir"], "events.tsv")).read().splitlines()
    assert out and all(line.split("\t")[3] == "rehearsal" for line in out)


def test_the_mark_is_set_by_the_event_writer_only_when_the_environment_says_rehearsal(tmp_path, monkeypatch):
    from alpaca import db
    conn = db.connect(str(tmp_path))
    monkeypatch.delenv("ALPACA_REHEARSAL", raising=False)
    plain = db.append_event(conn, session="s", actor="a", kind="note", data={"x": 1})
    monkeypatch.setenv("ALPACA_REHEARSAL", "1")
    marked = db.append_event(conn, session="s", actor="a", kind="note", data={"x": 1})
    bare = db.append_event(conn, session="s", actor="a", kind="note")
    assert "rehearsal" not in json.loads(plain["data"])
    assert json.loads(marked["data"]) == {"x": 1, "rehearsal": True}
    assert json.loads(bare["data"]) == {"rehearsal": True}
    assert db.verify_chain(conn)[0]


def test_the_scorer_gives_the_mechanical_rows_of_a_clean_run(run):
    meta, cid1, cid2 = run
    got = score.score(meta["run_dir"])
    rows, detail = got["rows"], got["detail"]
    assert rows["R2"] == 2, detail["R2"]
    assert rows["R3"] == 2, detail["R3"]
    assert rows["R5"] == 2, detail["R5"]
    assert rows["R7"] == 2, detail["R7"]
    assert rows["R8"] == 2 and "3 owner messages" in detail["R8"][0]            # opening, reply, yes; the trap reply is not counted
    assert rows["R10"] == 2, detail["R10"]
    assert rows["R11"] == 2, detail["R11"]
    assert rows["R4"] == 2, detail["R4"]
    assert rows["R1"] is None and rows["R6"] is None and rows["R9"] is None
    assert got["card"] == cid2


def test_finalize_applies_the_pass_bar(run):
    meta, _c1, _c2 = run
    mech = score.score(meta["run_dir"])
    ok = score.finalize(mech, {"R1": 2, "R6": 2, "R9": "pending"})
    assert ok["passed"] and ok["total"] == 22 and ok["rows"]["R9"] == "pending"
    low = score.finalize(mech, {"R1": 1, "R6": 2, "R9": 2})
    assert not low["passed"] and "R1" in low["why"]
    weak = score.finalize(mech, {"R1": 2, "R6": 0, "R9": 0})
    assert weak["total"] == 18 and weak["passed"]                                 # exactly the bar
    under = score.finalize(dict(mech, rows=dict(mech["rows"], R8=1)), {"R1": 2, "R6": 0, "R9": 0})
    assert under["total"] == 17 and not under["passed"] and any("under 18" in w for w in under["why"])
    failing = dict(mech, rows=dict(mech["rows"], R2=0))
    assert "R2" in score.finalize(failing, {"R1": 2, "R6": 2, "R9": 2})["why"]


def _copy_run(run, tmp_path):
    import shutil
    meta, _a, _b = run
    dst = tmp_path / "copy"
    shutil.copytree(meta["run_dir"], str(dst), symlinks=True)
    new = dict(meta, run_dir=str(dst))
    with open(os.path.join(str(dst), "meta.json"), "w") as fh:
        json.dump(new, fh)
    return str(dst)


def test_the_scorer_fails_a_run_that_read_the_sealed_sheet(run, tmp_path):
    rd = _copy_run(run, tmp_path)
    assert score.score(rd)["rows"]["R2"] == 2
    assert score.personas_read("cat /repo/alpaca/tests/rehearsal/personas/P2.yaml")
    got = score.score(rd, extra_log="cat alpaca/tests/rehearsal/personas/P2.yaml")
    assert got["rows"]["R2"] == 0 and "read the personas folder" in got["detail"]["R2"][0]


def test_the_scorer_fails_a_snapshot_that_changed_before_the_confirm(run, tmp_path):
    rd = _copy_run(run, tmp_path)
    path = os.path.join(rd, "snapshots.tsv")
    lines = open(path).read().splitlines()
    label, digest, last = lines[1].split("\t")
    lines[1] = "\t".join([label, "0" * 64, last])
    open(path, "w").write("\n".join(lines) + "\n")
    got = score.score(rd)
    assert got["rows"]["R2"] == 0 and "changed before the confirm" in got["detail"]["R2"][0]


def test_the_scorer_fails_a_confirm_the_persona_did_not_type(run, tmp_path):
    rd = _copy_run(run, tmp_path)
    open(os.path.join(rd, "persona_turns.jsonl"), "w").write(json.dumps({"text": "something else", "ts": "x"}) + "\n")
    got = score.score(rd)
    assert got["rows"]["R7"] == 0 and "not a line the persona typed" in got["detail"]["R7"][0]


def test_the_scorer_counts_owner_messages_and_unglossed_words(run, tmp_path):
    rd = _copy_run(run, tmp_path)
    turns = [json.loads(l) for l in open(os.path.join(rd, "persona_turns.jsonl"))]
    extra = [{"text": "extra %d" % i, "ts": "x"} for i in range(3)]
    with open(os.path.join(rd, "persona_turns.jsonl"), "w") as fh:
        for t in extra + turns:
            fh.write(json.dumps(t) + "\n")
    assert score.score(rd)["rows"]["R8"] == 0                                    # 6 messages until the yes, trap not counted
    meta = json.load(open(os.path.join(rd, "meta.json")))
    body = open(meta["transcript"]).read()
    early = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "The gate failed. The task is done."}]}})
    open(meta["transcript"], "w").write(early + "\n" + body)                   # said before the card explained them
    r11 = score.score(rd)
    assert r11["rows"]["R11"] == 0
    assert any(w in " ".join(r11["detail"]["R11"]) for w in ("gate", "task"))


def test_the_scorer_fails_a_situation_the_card_got_wrong(run, tmp_path):
    rd = _copy_run(run, tmp_path)
    for fn in os.listdir(os.path.join(rd, "cards")):
        if fn.endswith(".json"):
            path = os.path.join(rd, "cards", fn)
            data = json.load(open(path))
            data["settings"]["situation"] = "audit"
            json.dump(data, open(path, "w"))
    assert score.score(rd)["rows"]["R5"] == 0


def test_the_driver_refuses_to_reuse_a_run_folder(run, tmp_path):
    meta, _a, _b = run
    with pytest.raises(RuntimeError, match="fresh studio"):
        driver.make_run(os.path.dirname(meta["run_dir"]), "P2", today=TODAY.isoformat())


def test_the_driver_cli_prints_state_the_quiz_and_the_sheet_for_the_persona(run):
    meta, _a, _b = run
    for args, needle in ((["state", meta["run_dir"]], '"applied": true'), (["quiz", meta["run_dir"]], "Q5. What happens in the first 30 minutes"),
                         (["persona", meta["run_dir"]], "Add CSV export")):
        done = subprocess.run([os.sys.executable, "-m", "alpaca.tests.rehearsal.driver", *args], capture_output=True, text=True,
                              cwd=driver.REPO, env=dict(os.environ, PYTHONPATH=driver.REPO))
        assert done.returncode == 0 and needle in done.stdout, done.stderr


def test_the_p1_public_part_gives_the_persona_the_real_repo_path_to_tell_the_host():
    """Round 1: the P1 persona made up a path (cost one owner turn). The sheet's public part holds the placeholder the
    driver fills, and no placeholder is left unfilled in any public part."""
    view = driver.public_view(driver.load_persona("P1"), {"repo_path": "/x/repo"})
    assert view["public"]["path_when_asked"] == "/x/repo"
    for pid in ("P1", "P2", "P3"):
        shown = json.dumps(driver.public_view(driver.load_persona(pid), {"repo_path": "/x/repo"}))
        assert "{repo_path}" not in shown, pid


def test_the_driver_host_and_owner_verbs_print_one_confirmation_line(tmp_path):
    meta = driver.make_run(str(tmp_path / "scratch"), "P2", today=TODAY.isoformat())
    for who, text in (("owner", "hello there"), ("host", "Is there code already?")):
        f = tmp_path / (who + ".txt")
        f.write_text(text + "\n")
        done = subprocess.run([os.sys.executable, "-m", "alpaca.tests.rehearsal.driver", who, meta["run_dir"], "--text-file", str(f)],
                              capture_output=True, text=True, cwd=driver.REPO, env=dict(os.environ, PYTHONPATH=driver.REPO))
        assert done.returncode == 0, done.stderr
        lines = done.stdout.splitlines()
        assert len(lines) == 1 and lines[0].startswith("added: %s message " % who), done.stdout
    assert lines[0].startswith("added: host message 2 ") and "finish" in lines[0]


def _workflow_commands():
    """Every driver command the Workflow script puts in a prompt, as the text between its backticks."""
    path = os.path.join(driver.HERE, "run-workflow.js")
    text = open(path, encoding="utf-8").read()
    return text, [m.group(1) for m in re.finditer(r"\\`(\$\{DRV\} [^`]*?)\\`", text)]


def test_the_workflow_prompts_put_every_driver_command_in_backticks_with_no_trailing_words():
    """Round 1: the host prompt ended `driver state <dir> .` and every host passed the "." to state; the quiz prompt
    said `finish <dir>  first` and two agents passed "first". A command is now fenced, and its arguments are exact."""
    text, cmds = _workflow_commands()
    assert text.count("${DRV}") == len(cmds), "a driver command is not inside backticks"
    shape = {
        "make": r"make \$\{id\} --scratch \$\{args\.scratch\} --today \$\{args\.today\}(\$\{probe\})?",
        "host": r"host \$\{made\.runDir\} --text-file <file>",
        "owner": r"owner \$\{made\.runDir\} --text-file <file>",
        "snapshot": r"snapshot \$\{made\.runDir\} host-\$\{turn\}",
        "state": r"state \$\{made\.runDir\}",
        "finish": r"finish \$\{(made|m|r\.run)\.runDir\}",
        "persona": r"persona \$\{(made|r\.run)\.runDir\}",
    }
    seen = set()
    for cmd in cmds:
        verb = cmd[len("${DRV} "):].split()[0]
        assert verb in shape and re.fullmatch(r"\$\{DRV\} " + shape[verb], cmd), cmd
        seen.add(verb)
    assert seen == set(shape)
    assert " first" not in "".join(cmds) and not any(c.rstrip().endswith(" .") for c in cmds)


def _r10_run(tmp_path, pid, typed, limit_line):
    """A bare run folder with the lines the persona typed, and a card that holds `limit_line`."""
    rd = tmp_path / "r10"
    rd.mkdir(exist_ok=True)
    with open(rd / "persona_turns.jsonl", "w") as fh:
        for t in typed:
            fh.write(json.dumps({"text": t, "ts": "x"}) + "\n")
    studio = tmp_path / "studio"
    studio.mkdir(exist_ok=True)
    settings = {"mode": "attach" if pid == "P1" else "native", "branch_prefix": "alpaca/"}
    text = "1. WHAT I UNDERSTOOD\n- Your goal, in your words: \"x\"\n- Limits you gave: %s\n- Done when: later\n" % limit_line
    return str(rd), {"studio": str(studio)}, {"settings": settings, "_text": text}


def test_r10_finds_the_gui_limit_the_persona_typed_in_lower_case(tmp_path):
    """Round 1 false negative (P1): the scorer matched "GUI" with case; the owner typed "gui"."""
    typed = ["old firmware repo", "dont change the gui. and nothing goes straight to main", "yes"]
    rd, meta, card = _r10_run(tmp_path, "P1", typed,
                              '"dont change the gui. and nothing goes straight to main"')
    value, detail = score._r10({"id": "P1"}, rd, meta, card)
    assert detail == [] and value == 2


def test_r10_finds_the_no_publishing_limit_in_the_persona_s_own_words(tmp_path):
    """Round 1 false negative (P3): the scorer looked for the literal "published online"; the persona was told to
    use its own words and typed "put online"."""
    typed = ["Nothing should ever be put online, not anywhere on the internet. And my students' names must stay on "
             "my laptop only."]
    rd, meta, card = _r10_run(tmp_path, "P3", typed, '"Nothing should ever be put online, not anywhere on the\n  internet. '
                              'And my students\' names must stay on my laptop only."')
    value, detail = score._r10({"id": "P3"}, rd, meta, card)
    assert detail == [] and value == 2


def test_r10_still_fails_a_card_without_the_limit_the_owner_typed(tmp_path):
    typed = ["dont change the gui. and nothing goes straight to main"]
    rd, meta, card = _r10_run(tmp_path, "P1", typed, '"nothing goes straight to main"')
    value, detail = score._r10({"id": "P1"}, rd, meta, card)
    assert value == 1 and "dont change the gui" in detail[0]
    # whole words: "guide" is not "gui", and a limit line that never names the topic fails when nothing was typed
    rd, meta, card = _r10_run(tmp_path, "P1", ["hello"], '"read the guide first"')
    assert score._r10({"id": "P1"}, rd, meta, card)[1] == ["the P1 limit is not on the card"]
    rd, meta, card = _r10_run(tmp_path, "P3", ["no sharing"], '"Never publish my class list"')
    assert score._r10({"id": "P3"}, rd, meta, card)[1] == []


def test_without_rehearsal_gives_back_the_data_the_writer_meant():
    from alpaca import db
    assert db.without_rehearsal({"x": 1, "rehearsal": True}) == {"x": 1}
    assert db.without_rehearsal({"x": 1}) == {"x": 1}
    assert db.without_rehearsal(None) is None and db.without_rehearsal([1]) == [1]
    assert db.without_rehearsal(db.rehearsal_marked({"x": 1})) == {"x": 1} or not os.environ.get("ALPACA_REHEARSAL")

