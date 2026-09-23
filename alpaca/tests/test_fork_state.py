"""M4.3 proof: FRESH sentinel and fork-state detection.

detect(root) reads the state of the record, never a flag pair. Onboarding lands op-0 (open and
close) plus the onboarded mark in one transaction, so the record is all-or-nothing: a kill between
the project.yaml write and the record commit can no longer leave op-zero unclosed. It leaves a
committed identity on disk with no matching record, which reads as a cloned but unrun copy and is
named as such. Detection is a pure query with no side effect; onboarding is idempotent by
precondition (runs only when fresh, else refuses with the state named).
"""
import json, os, subprocess, sys

from alpaca import adopt, cli, clock, db, pad, project as proj, util


def _hook(payload, cwd, env_extra=None):
    env = dict(os.environ); env.update(env_extra or {})
    p = subprocess.run([sys.executable, "-m", "alpaca.hooks.session_start"], input=json.dumps(payload),
                       capture_output=True, text=True, encoding="utf-8", cwd=cwd, env=env)
    return p.returncode, p.stdout, p.stderr


# --- the three states, each derived from the record --------------------------------------------

def test_detect_fresh(project):
    # No record onboarding and no committed identity: true first contact.
    cli.main(["init"])
    assert adopt.detect(project) == adopt.FRESH == "fresh"


def test_detect_populated(project):
    cli.main(["init"])
    cli.main(["onboard", "--name", "demo", "--who", "v:owner", "--what", "w"])
    assert adopt.detect(project) == adopt.POPULATED == "populated"
    # keyed on the record (op-0 present), not on any single flag
    conn = db.connect(project)
    assert db.rows(conn, "ops", "id=?", ("op-0",))[0]["status"] == "closed"


def test_detect_second_machine_from_clone(project):
    # A committed project.yaml travelled in (a clone); the local record was never onboarded here.
    cli.main(["init"])
    proj.save(project, {"name": "cloned-elsewhere"})
    assert adopt.detect(project) == adopt.SECOND_MACHINE == "second-machine"


def test_detect_before_any_record_is_fresh(project):
    # Even with no .alpaca yet, detect is a read-only query and returns fresh.
    assert adopt.detect(project) == adopt.FRESH


# --- detection is a pure query -----------------------------------------------------------------

def test_detect_has_no_side_effect(project):
    cli.main(["init"])
    conn = db.connect(project)
    before = len(db.events(conn))
    for _ in range(3):
        assert adopt.detect(project) == adopt.FRESH
    conn = db.connect(project)
    assert len(db.events(conn)) == before          # no event appended
    assert db.rows(conn, "ops", "id=?", ("op-0",)) == []   # no op row written
    assert not proj.is_onboarded(project)          # no flag set


# --- the P-005 crash sequence, replayed exactly ------------------------------------------------

def test_p005_crash_names_second_machine_and_never_leaves_op_zero_unclosed(project, monkeypatch):
    util.set_clock(clock.FixedClock(start="2026-09-17T00:00:00+00:00"))
    cli.main(["init"])
    assert adopt.detect(project) == adopt.FRESH
    # Kill the write at the onboarded mark, exactly between the project.yaml write (already done
    # inside the transaction) and the record commit.
    real = db.meta_set
    def boom(conn, key, value):
        if key == "onboarded":
            raise RuntimeError("injected crash")
        return real(conn, key, value)
    monkeypatch.setattr(db, "meta_set", boom)
    assert cli.main(["onboard", "--name", "d", "--who", "a:owner", "--what", "w"]) == cli.INTERNAL
    monkeypatch.setattr(db, "meta_set", real)
    conn = db.connect(project)
    # op-zero is never left unclosed: it is all-or-nothing, so after the crash there is no op-0.
    assert db.rows(conn, "ops", "id=?", ("op-0",)) == []
    assert db.verify_chain(conn)[0] is True
    # The identity survives on disk with an empty record: named a second machine, acts on nothing.
    assert proj.load(project)["name"] == "d"
    assert adopt.detect(project) == adopt.SECOND_MACHINE
    util.set_clock(None)


# --- onboarding is idempotent by precondition, not by a flag -----------------------------------

def test_onboard_runs_only_when_fresh(project):
    cli.main(["init"])
    assert adopt.detect(project) == adopt.FRESH
    assert cli.main(["onboard", "--name", "demo", "--who", "v:owner", "--what", "w"]) == cli.PASS


def test_onboard_refuses_when_populated_naming_the_state(project, capsys):
    cli.main(["init"])
    cli.main(["onboard", "--name", "demo", "--who", "v:owner", "--what", "w"])
    capsys.readouterr()
    assert cli.main(["onboard", "--name", "again", "--who", "v:owner", "--what", "w"]) == cli.FAIL
    out = capsys.readouterr().out
    assert adopt.POPULATED in out


def test_onboard_refuses_when_second_machine_naming_the_state(project, capsys):
    cli.main(["init"])
    proj.save(project, {"name": "cloned-elsewhere"})
    capsys.readouterr()
    assert cli.main(["onboard", "--name", "d", "--who", "a:owner", "--what", "w"]) == cli.FAIL
    out = capsys.readouterr().out
    assert adopt.SECOND_MACHINE in out
    # refused without touching the committed identity
    assert proj.load(project)["name"] == "cloned-elsewhere"


# --- the sentinel shows in the pad in one line -------------------------------------------------

def test_pad_carries_the_fork_sentinel(project):
    cli.main(["init"])
    text = pad.render(project)
    line = [l for l in text.splitlines() if l.startswith("fork-state:")]
    assert len(line) == 1 and adopt.FRESH in line[0]
    cli.main(["onboard", "--name", "demo", "--who", "v:owner", "--what", "w"])
    line = [l for l in pad.render(project).splitlines() if l.startswith("fork-state:")]
    assert len(line) == 1 and adopt.POPULATED in line[0]


# --- the SessionStart hook names a clone and does not tell it to onboard ------------------------

def test_hook_fresh_still_prompts_first_chat(project):
    env = {"CLAUDE_PROJECT_DIR": project,
           "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.dirname(__file__)))}
    rc, out, err = _hook({"session_id": "abcd1234-0000", "transcript_path": "", "cwd": project,
                          "source": "startup"}, cwd=project, env_extra=env)
    assert rc == 0, err
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "FIRST CHAT" in ctx


def test_hook_names_second_machine_and_acts_on_nothing(project):
    proj.save(project, {"name": "cloned-elsewhere"})
    env = {"CLAUDE_PROJECT_DIR": project,
           "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.dirname(__file__)))}
    rc, out, err = _hook({"session_id": "abcd1234-0000", "transcript_path": "", "cwd": project,
                          "source": "startup"}, cwd=project, env_extra=env)
    assert rc == 0, err
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "SECOND MACHINE" in ctx
    assert "FIRST CHAT" not in ctx      # a clone is not told to onboard
    # acts on nothing: no op-0 landed
    assert db.rows(db.connect(project), "ops", "id=?", ("op-0",)) == []
