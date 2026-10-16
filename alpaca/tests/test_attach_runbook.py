"""`${ATTACH_WORKTREE}` and the stage runner (op-062 t-653, requirement R15, acceptance row AC-16) and the
pre-mortem attacks of t-653 (items 1, 2 and 5) and X5.

The variable exists only in a project with an `attach:` block. Every plain project keeps its old behaviour:
the name is `VAR-UNKNOWN`, a knob may carry it, nothing is relaxed. `alpaca attach stage` is the executor the
plan adds because the repository has a checker and a check evaluator but nothing that runs a plain stage.
"""
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from alpaca import cli, db, runbook
from alpaca.attach import AttachError, config, gitops, runbooks
from alpaca.gates import verdict
from alpaca.tests.attachkit import (add_tasks, attached, git, set_attach, studio, upstream)  # noqa: F401


def minimal(**stage):
    """A format 1 runbook with one stage; `stage` overrides or adds keys of that stage."""
    body = {"id": "s1", "title": "write a file", "run": "true", "timeout": 60,
            "checks": [{"id": "c1", "type": "exit-code"}]}
    body.update(stage)
    return {"runbook": 1, "id": "rb", "title": "t", "stages": [body]}


def write_runbook(folder, data, name="runbook.yaml"):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return str(path)


def codes(result):
    return [e["code"] for e in result["errors"]]


def claimed(att, count=1):
    """Open tasks and claim the first one, which cuts its work folder; returns (task, folder)."""
    tasks = add_tasks(att.root, count)
    assert cli.main(["task", "claim", tasks[0], "--by", "w1"]) == cli.PASS
    return tasks[0], att.root / ".alpaca/attach/worktrees" / tasks[0]


def wt_status(att, task, folder):
    return gitops.in_worktree(str(att.app), task, os.path.realpath(str(folder)), "status", "--porcelain",
                              "--untracked-files=all")


STAGE_WRITES_FILE = dict(run="echo made > ${ATTACH_WORKTREE}/out.txt", workdir="${ATTACH_WORKTREE}",
                         checks=[{"id": "ran", "type": "exit-code"},
                                 {"id": "file", "type": "file-exists", "path": "${ATTACH_WORKTREE}/out.txt"}])


# ---- the checker: a project without attach: is unchanged (X5) --------------------------------------------------------

def test_the_variable_is_unknown_in_a_plain_project(studio, tmp_path):
    path = write_runbook(studio / "runbooks" / "a", minimal(**STAGE_WRITES_FILE))
    got = runbook.check(path)
    assert "VAR-UNKNOWN" in codes(got) and got["code"] == verdict.FAIL
    assert "ATTACH_WORKTREE" not in runbook.BUILTIN_VARS and runbook.ATTACH_VARS == ("ATTACH_WORKTREE",)


def test_the_variable_is_known_in_an_attached_project(attached):
    path = write_runbook(attached.root / "runbooks" / "a", minimal(**STAGE_WRITES_FILE))
    got = runbook.check(path)
    assert got["errors"] == [] and got["code"] == verdict.PASS


@pytest.mark.parametrize("value", [None, {}, [], "app"])
def test_an_attach_key_that_is_not_a_block_with_a_repo_does_not_switch_the_mode_on(studio, value):
    """X5: presence of the key is not the mode (P1)."""
    set_attach(studio, value)
    path = write_runbook(studio / "runbooks" / "a", minimal(**STAGE_WRITES_FILE))
    assert "VAR-UNKNOWN" in codes(runbook.check(path))
    assert runbooks.attach_on(str(studio)) is False


def test_a_project_with_a_broken_attach_block_reads_runbooks_like_a_plain_project(attached):
    set_attach(attached.root, {"repo": "../elsewhere", "url": "x"})
    path = write_runbook(attached.root / "runbooks" / "a", minimal(**STAGE_WRITES_FILE))
    assert "VAR-UNKNOWN" in codes(runbook.check(path))


def test_the_other_variable_errors_are_unchanged_when_attached(attached):
    data = minimal(run="make ${JOBS}")
    got = runbook.check(write_runbook(attached.root / "runbooks" / "a", data))
    bad = [e for e in got["errors"] if e["code"] == "VAR-UNKNOWN"]
    assert bad and "JOBS" in bad[0]["message"] and "ATTACH_WORKTREE" in bad[0]["message"]


def test_a_knob_named_like_the_variable_is_refused_only_in_an_attached_project(attached, studio_plain=None):
    knob = {"id": "ATTACH_WORKTREE", "description": "d", "type": "str", "default": "/"}
    data = minimal(workdir="${ATTACH_WORKTREE}")
    data["knobs"] = [knob]
    got = runbook.check(write_runbook(attached.root / "runbooks" / "a", data))
    assert "KNOB-SHADOWS-BUILTIN" in codes(got)


def test_a_knob_of_that_name_is_just_a_knob_in_a_plain_project(studio):
    """The contract fail case: nothing is relaxed or newly refused for a project without attach:."""
    knob = {"id": "ATTACH_WORKTREE", "description": "d", "type": "str", "default": "x"}
    data = minimal(run="echo ${ATTACH_WORKTREE}")
    data["knobs"] = [knob]
    assert runbook.check(write_runbook(studio / "runbooks" / "a", data))["errors"] == []


def test_a_workdir_that_climbs_out_is_still_path_escapes(attached):
    for bad in ("${ATTACH_WORKTREE}/../../..", "../x", "/etc"):
        got = runbook.check(write_runbook(attached.root / "runbooks" / "a", minimal(workdir=bad)))
        if bad.startswith("${"):
            continue                 # x/../../.. is judged at run time, below (the static side sees `x/`)
        assert "PATH-ESCAPES" in codes(got), bad


def test_the_docs_table_names_the_variable_as_attach_only():
    text = (Path(__file__).resolve().parents[2] / "docs" / "runbook-format.md").read_text(encoding="utf-8")
    row = [line for line in text.splitlines() if line.startswith("| `${ATTACH_WORKTREE}`")]
    assert len(row) == 1 and "attach" in row[0].lower()


# ---- evaluate: an absent value, a symlink, a knob ----------------------------------------------------------------------

def test_an_absent_value_never_resolves_to_a_path_at_the_filesystem_root(tmp_path):
    """PM X5: `attach_worktree or ""` would turn ${ATTACH_WORKTREE}/out.json into /out.json."""
    chk = {"id": "c", "type": "file-exists", "path": "${ATTACH_WORKTREE}/out.json"}
    code, why = runbook.evaluate(chk, workdir=str(tmp_path), runbook_dir=str(tmp_path))
    assert code == verdict.BLOCKED and "ATTACH_WORKTREE is not set" in why
    for kind in ("regex-in-file", "json-field"):
        chk = {"id": "c", "type": kind, "path": "${ATTACH_WORKTREE}/out.json", "pattern": "x", "field": "a", "op": "==", "value": 1}
        assert runbook.evaluate(chk, workdir=str(tmp_path), runbook_dir=str(tmp_path))[0] == verdict.BLOCKED


def test_a_path_under_the_worktree_is_judged_when_the_value_is_given(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "out.json").write_text(json.dumps({"n": 3}), encoding="utf-8")
    rb = tmp_path / "rb"
    rb.mkdir()
    chk = {"id": "c", "type": "json-field", "path": "${ATTACH_WORKTREE}/out.json", "field": "n", "op": "==", "value": 3}
    assert runbook.evaluate(chk, workdir=str(rb), runbook_dir=str(rb), attach_worktree=str(wt))[0] == verdict.PASS
    # a plain relative path is still held to the runbook folder
    plain = {"id": "c", "type": "file-exists", "path": "../wt/out.json"}
    assert runbook.evaluate(plain, workdir=str(rb), runbook_dir=str(rb), attach_worktree=str(wt))[0] == verdict.BLOCKED


def test_a_symlink_in_the_worktree_that_leaves_it_is_blocked(tmp_path):
    """PM t-653.2: the containment test uses real paths, not the lexical ones."""
    wt, out, rb = tmp_path / "wt", tmp_path / "outside", tmp_path / "rb"
    for d in (wt, out, rb):
        d.mkdir()
    (out / "secret.txt").write_text("s\n", encoding="utf-8")
    os.symlink(str(out), str(wt / "link"))
    chk = {"id": "c", "type": "file-exists", "path": "${ATTACH_WORKTREE}/link/secret.txt"}
    code, why = runbook.evaluate(chk, workdir=str(rb), runbook_dir=str(rb), attach_worktree=str(wt))
    assert code == verdict.BLOCKED and "outside" in why
    chk["path"] = "${ATTACH_WORKTREE}/../outside/secret.txt"
    assert runbook.evaluate(chk, workdir=str(rb), runbook_dir=str(rb), attach_worktree=str(wt))[0] == verdict.BLOCKED


def test_a_knob_value_cannot_stand_in_for_the_variable_at_run_time(tmp_path):
    wt, rb = tmp_path / "wt", tmp_path / "rb"
    wt.mkdir()
    rb.mkdir()
    chk = {"id": "c", "type": "file-exists", "path": "${ATTACH_WORKTREE}/x"}
    code, _why = runbook.evaluate(chk, workdir=str(rb), runbook_dir=str(rb), knobs={"ATTACH_WORKTREE": "/"},
                                  attach_worktree=str(wt))
    assert code == verdict.FAIL                      # looked at wt/x (missing), not at /x


def _worktree_with_a_link_out(tmp_path):
    wt, outside = tmp_path / "wt", tmp_path / "outside"
    wt.mkdir()
    outside.mkdir()
    (outside / "passwd").write_text(json.dumps({"pw": "hunter2"}), encoding="utf-8")
    os.symlink(str(outside), str(wt / "out"))
    return wt, outside


@pytest.mark.parametrize("written", ["${ATTACH_WORKTREE}/out/passwd", "out/passwd",
                                     "${RUNBOOK_DIR}/wt/out/passwd"])
@pytest.mark.parametrize("kind", ["file-exists", "regex-in-file", "json-field"])
def test_a_link_in_the_worktree_is_not_followed_even_when_the_runbook_folder_holds_the_worktree(tmp_path, written, kind):
    """PM t-653.2, adversarial F1: the class is every way to write a path that sits in the work folder, whichever
    folder is the runbook folder. Here the runbook folder (the studio root, or `.alpaca/`) holds the work folder."""
    wt, _outside = _worktree_with_a_link_out(tmp_path)
    chk = {"id": "c", "type": kind, "path": written, "pattern": "hunter2", "field": "pw", "op": "==", "value": "hunter2"}
    # the work folder is the working folder, as in the stage runner, and the runbook folder is its parent
    code, why = runbook.evaluate(chk, workdir=str(wt), runbook_dir=str(tmp_path), attach_worktree=str(wt))
    assert code == verdict.BLOCKED and "hunter2" not in why, (kind, written, why)
    # and with the pre-mortem's own sketch: runbook_dir left to default to the working folder
    if "RUNBOOK_DIR" not in written:
        code, why = runbook.evaluate(chk, workdir=str(wt), attach_worktree=str(wt))
        assert code == verdict.BLOCKED and "hunter2" not in why, (kind, written, why)


def test_a_link_in_the_worktree_that_stays_in_the_studio_is_not_followed_either(tmp_path):
    """The runbook folder holds both the work folder and a studio file: a link to the studio file is out of the
    work folder all the same."""
    wt = tmp_path / "wt"
    wt.mkdir()
    (tmp_path / "private.txt").write_text("studio only\n", encoding="utf-8")
    os.symlink(str(tmp_path / "private.txt"), str(wt / "p.txt"))
    for written in ("p.txt", "${ATTACH_WORKTREE}/p.txt"):
        chk = {"id": "c", "type": "file-exists", "path": written}
        assert runbook.evaluate(chk, workdir=str(wt), runbook_dir=str(tmp_path), attach_worktree=str(wt))[0] == verdict.BLOCKED


def test_a_file_in_the_worktree_is_found_when_the_runbook_folder_holds_the_worktree(tmp_path):
    """The other side of the class: no link, the file is where it is written, every spelling passes."""
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "ok.txt").write_text("fine\n", encoding="utf-8")
    for written in ("ok.txt", "${ATTACH_WORKTREE}/ok.txt", "${RUNBOOK_DIR}/wt/ok.txt"):
        chk = {"id": "c", "type": "file-exists", "path": written}
        assert runbook.evaluate(chk, workdir=str(wt), runbook_dir=str(tmp_path), attach_worktree=str(wt))[0] == verdict.PASS, written


def test_a_link_in_the_runbook_folder_is_not_followed_when_the_worktree_is_given(tmp_path):
    rb, outside = tmp_path / "rb", tmp_path / "outside"
    rb.mkdir()
    outside.mkdir()
    (outside / "f.txt").write_text("x\n", encoding="utf-8")
    os.symlink(str(outside), str(rb / "l"))
    wt = tmp_path / "wt"
    wt.mkdir()
    chk = {"id": "c", "type": "file-exists", "path": "l/f.txt"}
    assert runbook.evaluate(chk, workdir=str(rb), runbook_dir=str(rb), attach_worktree=str(wt))[0] == verdict.BLOCKED


def test_without_a_worktree_the_paths_are_judged_as_they_always_were(tmp_path):
    """X5: no attach_worktree, no change. A link inside the runbook folder is followed, as on the base commit."""
    rb, outside = tmp_path / "rb", tmp_path / "outside"
    rb.mkdir()
    outside.mkdir()
    (outside / "f.txt").write_text("x\n", encoding="utf-8")
    os.symlink(str(outside), str(rb / "l"))
    chk = {"id": "c", "type": "file-exists", "path": "l/f.txt"}
    assert runbook.evaluate(chk, workdir=str(rb), runbook_dir=str(rb))[0] == verdict.PASS


def test_a_knob_of_that_name_still_resolves_a_check_path_in_a_plain_project(tmp_path):
    """X5, adversarial F2: research gates call evaluate with knobs and no work folder; a knob named like the
    variable is a value there, so the path resolves through it as on the base commit."""
    rb = tmp_path / "rb"
    (rb / "out").mkdir(parents=True)
    (rb / "out" / "m.json").write_text(json.dumps({"n": 1}), encoding="utf-8")
    chk = {"id": "c", "type": "file-exists", "path": "${ATTACH_WORKTREE}/m.json"}
    code, why = runbook.evaluate(chk, workdir=str(rb), runbook_dir=str(rb), knobs={"ATTACH_WORKTREE": "out"})
    assert code == verdict.PASS and why == "out/m.json exists"
    # no knob and no work folder is still BLOCKED, never a path at the filesystem root
    assert runbook.evaluate(chk, workdir=str(rb), runbook_dir=str(rb), knobs={"OTHER": 1})[0] == verdict.BLOCKED


def test_a_research_runbook_does_not_get_the_variable_in_an_attached_project(attached):
    """Adversarial F3: the research engine never sets ${ATTACH_WORKTREE}, so a format 3 command would run with
    the name expanded to an empty string by the shell. Format 3 keeps VAR-UNKNOWN, in every project."""
    from alpaca.tests import research_loopkit as kit
    text = (kit.RB.replace("ROUNDS", "1").replace("SLOTS", "1").replace("RETRIES", "2").replace("DISK", "0.001")
            .replace("USD", "10").replace("run: test -f ${WORKTREE}/src/value.txt",
                                          "run: test -f ${ATTACH_WORKTREE}/src/value.txt"))
    text = text.replace("research:\n  mode: improve\n", "research:\n  mode: improve\n  repo: {path: app}\n")
    text = text.replace("sh ${CONTRACT_DIR}/pack/measure.sh", "sh ${RUNBOOK_DIR}/measure.sh").replace(
        "    protected: [pack/]\n", "    protected: [Makefile]\n")
    kit.write(str(attached.root), "research/spec.md", kit.SPEC)
    kit.write(str(attached.root), "research/measure.sh", kit.MEASURE, 0o755)
    kit.write(str(attached.root), "research/rb.yaml", text)
    got = runbook.check(str(attached.root / "research" / "rb.yaml"))
    assert "VAR-UNKNOWN" in codes(got) and any("ATTACH_WORKTREE" in e["message"] for e in got["errors"])
    # and the knob of that name is not refused as a shadow in format 3 (it is a knob there, as in a plain project)
    knob = (text.replace("knobs:\n", "knobs:\n  - id: ATTACH_WORKTREE\n    description: d\n    type: str\n    default: x\n", 1)
            if "knobs:\n" in text else None)
    if knob is not None:
        kit.write(str(attached.root), "research/rb2.yaml", knob.replace("${ATTACH_WORKTREE}", "x"))
        assert "KNOB-SHADOWS-BUILTIN" not in codes(runbook.check(str(attached.root / "research" / "rb2.yaml")))


# ---- the stage runner (AC-16) ---------------------------------------------------------------------------------------

def test_a_stage_runs_in_the_task_worktree_and_its_file_check_passes(attached, capsys):
    """AC-16: the stage's `run` writes out.txt into the worktree; the `file-exists` check of
    ${ATTACH_WORKTREE}/out.txt passes; out.txt is in the worktree and not committed."""
    task, folder = claimed(attached)
    rb = write_runbook(attached.root / "runbooks" / "a", minimal(**STAGE_WRITES_FILE))
    head = gitops.in_worktree(str(attached.app), task, os.path.realpath(str(folder)), "rev-parse", "HEAD")
    capsys.readouterr()
    assert cli.main(["attach", "stage", task, rb, "s1"]) == cli.PASS
    out = capsys.readouterr().out
    assert "GATE alpaca-attach-stage: PASS" in out and "wrote out.txt" in out and "nothing was committed" in out
    assert (folder / "out.txt").read_text() == "made\n"
    assert wt_status(attached, task, folder) == "?? out.txt"
    assert gitops.in_worktree(str(attached.app), task, os.path.realpath(str(folder)), "rev-parse", "HEAD") == head
    result = json.loads((attached.root / ".alpaca/attach/stages" / task / "s1" / "result.json").read_text())
    assert result["verdict"] == "PASS" and result["files_written"] == ["out.txt"] and result["committed"] is False
    assert [c["verdict"] for c in result["checks"]] == ["PASS", "PASS"]
    conn = db.connect(str(attached.root))
    try:
        events = [e for e in db.events(conn, limit=10 ** 9) if e["kind"] == "attach-stage"]
    finally:
        conn.close()
    assert len(events) == 1 and events[0]["ref"] == task and events[0]["data"]["verdict"] == "PASS"


def test_the_main_checkout_of_the_clone_is_untouched_by_a_stage(attached):
    task, folder = claimed(attached)
    rb = write_runbook(attached.root / "runbooks" / "a", minimal(**STAGE_WRITES_FILE))
    before = (git(attached.app, "rev-parse", "HEAD"), git(attached.app, "status", "--porcelain", "--ignored"))
    runbooks.run_stage(str(attached.root), task, rb, "s1")
    assert (git(attached.app, "rev-parse", "HEAD"), git(attached.app, "status", "--porcelain", "--ignored")) == before
    assert not (attached.app / "out.txt").exists()


def test_a_failing_check_fails_the_stage(attached, capsys):
    task, folder = claimed(attached)
    data = minimal(**dict(STAGE_WRITES_FILE, run="true"))
    rb = write_runbook(attached.root / "runbooks" / "a", data)
    capsys.readouterr()
    assert cli.main(["attach", "stage", task, rb, "s1"]) == cli.FAIL
    assert "file: FAIL" in capsys.readouterr().out
    assert not (folder / "out.txt").exists()


def test_a_stage_that_commits_fails_and_says_so(attached):
    task, folder = claimed(attached)
    run = ("echo x > ${ATTACH_WORKTREE}/c.txt && git -C ${ATTACH_WORKTREE} add c.txt && "
           "git -C ${ATTACH_WORKTREE} -c user.name=a -c user.email=a@b commit -q -m c")
    rb = write_runbook(attached.root / "runbooks" / "a", minimal(run=run, workdir="${ATTACH_WORKTREE}"))
    result = runbooks.run_stage(str(attached.root), task, rb, "s1")
    assert result["verdict"] == "FAIL" and result["head_moved"] and "never commits" in result["reason"]


def test_a_workdir_that_resolves_outside_both_folders_is_refused(attached):
    """PM t-653.2: `${ATTACH_WORKTREE}/..` reads as `x/..` to the static check and is judged here; two more
    steps up are caught by the checker already."""
    task, _folder = claimed(attached)
    rb = write_runbook(attached.root / "runbooks" / "a", minimal(workdir="${ATTACH_WORKTREE}/..", run="echo hi > escaped.txt"))
    with pytest.raises(AttachError, match="outside the work folder"):
        runbooks.run_stage(str(attached.root), task, rb, "s1")
    rb = write_runbook(attached.root / "runbooks" / "a", minimal(workdir="${ATTACH_WORKTREE}/../../..", run="echo hi > escaped.txt"))
    with pytest.raises(AttachError, match="PATH-ESCAPES"):
        runbooks.run_stage(str(attached.root), task, rb, "s1")
    assert not list(attached.root.rglob("escaped.txt"))


def test_a_symlink_workdir_that_leaves_the_worktree_is_refused(attached, tmp_path):
    task, folder = claimed(attached)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(str(outside), str(folder / "link"))
    rb = write_runbook(attached.root / "runbooks" / "a", minimal(workdir="${ATTACH_WORKTREE}/link", run="echo hi > escaped.txt"))
    with pytest.raises(AttachError, match="outside the work folder"):
        runbooks.run_stage(str(attached.root), task, rb, "s1")
    assert not (outside / "escaped.txt").exists()


def test_the_default_workdir_is_the_runbook_folder(attached):
    task, _folder = claimed(attached)
    folder = attached.root / "runbooks" / "a"
    rb = write_runbook(folder, minimal(run="echo here > local.txt",
                                      checks=[{"id": "f", "type": "file-exists", "path": "local.txt"}]))
    assert runbooks.run_stage(str(attached.root), task, rb, "s1")["verdict"] == "PASS"
    assert (folder / "local.txt").exists()


def test_a_runbook_that_fails_the_checker_does_not_run(attached):
    task, folder = claimed(attached)
    rb = write_runbook(attached.root / "runbooks" / "a", minimal(run="touch ${ATTACH_WORKTREE}/ran.txt", checks=[]))
    with pytest.raises(AttachError, match="does not pass"):
        runbooks.run_stage(str(attached.root), task, rb, "s1")
    assert not (folder / "ran.txt").exists()


def test_a_research_runbook_is_not_run_by_the_stage_runner(attached):
    task, _folder = claimed(attached)
    rb = write_runbook(attached.root / "runbooks" / "a", {"runbook": 3, "id": "r"})
    with pytest.raises(AttachError):
        runbooks.run_stage(str(attached.root), task, rb, "s1")


def test_a_task_without_a_work_folder_is_refused(attached):
    (task,) = add_tasks(attached.root, 1)               # opened, not claimed: no folder
    rb = write_runbook(attached.root / "runbooks" / "a", minimal(**STAGE_WRITES_FILE))
    with pytest.raises(AttachError, match="no proved work folder"):
        runbooks.run_stage(str(attached.root), task, rb, "s1")


def test_a_runbook_outside_the_studio_is_refused(attached, tmp_path):
    task, _folder = claimed(attached)
    rb = write_runbook(tmp_path / "elsewhere", minimal(**STAGE_WRITES_FILE))
    with pytest.raises(AttachError, match="not a runbook file of this studio"):
        runbooks.run_stage(str(attached.root), task, rb, "s1")


def test_a_runbook_the_attached_repository_carries_is_refused(attached):
    """Adversarial F6: app/ holds upstream content; a runbook there is not a studio runbook."""
    task, _folder = claimed(attached)
    rb = write_runbook(attached.app / "ci", minimal(**STAGE_WRITES_FILE))
    with pytest.raises(AttachError, match="inside the attached repository"):
        runbooks.run_stage(str(attached.root), task, rb, "s1")


@pytest.mark.parametrize("where", [".alpaca", ".alpaca/attach", ".alpaca/research/r-001"])
def test_a_runbook_under_the_dot_alpaca_folder_is_refused(attached, where):
    task, _folder = claimed(attached)
    rb = write_runbook(attached.root / where, minimal(**STAGE_WRITES_FILE))
    with pytest.raises(AttachError, match=r"\.alpaca/"):
        runbooks.run_stage(str(attached.root), task, rb, "s1")


def test_a_check_cannot_follow_a_link_out_of_the_worktree_when_the_runbook_sits_at_the_studio_root(attached, tmp_path):
    """Adversarial F1 end to end: the runbook folder (the studio root) holds the work folder; the worker planted a
    link in its own work folder. All three file checks are BLOCKED and the value does not reach result.json."""
    task, folder = claimed(attached)
    outside = tmp_path / "outside-secret"
    outside.mkdir()
    (outside / "secret.json").write_text(json.dumps({"pw": "hunter2"}), encoding="utf-8")
    os.symlink(str(outside), str(Path(os.path.realpath(str(folder))) / "link"))
    checks = [{"id": "exists", "type": "file-exists", "path": "${ATTACH_WORKTREE}/link/secret.json"},
              {"id": "regex", "type": "regex-in-file", "path": "${ATTACH_WORKTREE}/link/secret.json", "pattern": "hunter2"},
              {"id": "field", "type": "json-field", "path": "${ATTACH_WORKTREE}/link/secret.json", "field": "pw",
               "op": "==", "value": "hunter2"}]
    rb = write_runbook(attached.root, minimal(run="true", workdir="${ATTACH_WORKTREE}", checks=checks), name="leak-runbook.yaml")
    result = runbooks.run_stage(str(attached.root), task, rb, "s1")
    assert {c["id"]: c["verdict"] for c in result["checks"]} == {"exists": "BLOCKED", "regex": "BLOCKED", "field": "BLOCKED"}
    assert "hunter2" not in json.dumps(result)


def test_a_stage_id_cannot_climb(attached):
    task, _folder = claimed(attached)
    rb = write_runbook(attached.root / "runbooks" / "a", minimal(**STAGE_WRITES_FILE))
    for bad in ("../s1", "a/b", "..", ""):
        with pytest.raises(AttachError):
            runbooks.run_stage(str(attached.root), task, rb, bad)


def test_the_token_never_reaches_the_stage_command_or_a_plugin_check(attached, monkeypatch):
    """P5: the credential variables are out of the process while a stage and its checks run."""
    monkeypatch.setenv("ALPACA_ATTACH_TOKEN", "sentinel-token-0123")
    monkeypatch.setenv("GITHUB_TOKEN", "sentinel-token-4567")
    task, folder = claimed(attached)
    rb_dir = attached.root / "runbooks" / "a"
    rb = write_runbook(rb_dir, minimal(run="env > ${ATTACH_WORKTREE}/env.txt", workdir="${ATTACH_WORKTREE}"))
    assert runbooks.run_stage(str(attached.root), task, rb, "s1")["verdict"] == "PASS"
    dump = (folder / "env.txt").read_text()
    assert "sentinel-token" not in dump
    assert os.environ["ALPACA_ATTACH_TOKEN"] == "sentinel-token-0123"          # and it is back afterwards


def test_a_stage_env_name_that_is_not_a_plain_name_is_refused(attached):
    task, _folder = claimed(attached)
    rb = write_runbook(attached.root / "runbooks" / "a", minimal(env={"A B": "x"}))
    with pytest.raises(AttachError, match="plain variable name"):
        runbooks.run_stage(str(attached.root), task, rb, "s1")


def test_stage_env_values_reach_the_command(attached):
    task, folder = claimed(attached)
    rb = write_runbook(attached.root / "runbooks" / "a", minimal(
        run='printf "%s" "$GREETING" > ${ATTACH_WORKTREE}/g.txt', workdir="${ATTACH_WORKTREE}", env={"GREETING": "hi there"}))
    assert runbooks.run_stage(str(attached.root), task, rb, "s1")["verdict"] == "PASS"
    assert (folder / "g.txt").read_text() == "hi there"


def test_a_stage_that_times_out_fails(attached):
    task, _folder = claimed(attached)
    rb = write_runbook(attached.root / "runbooks" / "a", minimal(run="sleep 30", timeout=1))
    result = runbooks.run_stage(str(attached.root), task, rb, "s1")
    assert result["verdict"] == "FAIL" and result["timed_out"] is True


def test_the_verb_is_blocked_in_a_plain_project(studio, capsys):
    capsys.readouterr()
    assert cli.main(["attach", "stage", "t-001", "x.yaml", "s1"]) == cli.BLOCKED
    assert "GATE alpaca-attach-stage: BLOCKED" in capsys.readouterr().out


def test_the_verb_prints_json_with_the_surface_keys(attached, capsys):
    task, _folder = claimed(attached)
    rb = write_runbook(attached.root / "runbooks" / "a", minimal(**STAGE_WRITES_FILE))
    capsys.readouterr()
    assert cli.main(["attach", "stage", task, rb, "s1", "--json"]) == cli.PASS
    got = json.loads(capsys.readouterr().out)
    assert got["verdict"] == "PASS" and got["task"] == task and got["receipt"]["stage"] == "s1"


# ---- the partner kit still builds without the attach package -----------------------------------------------------------

def test_the_partner_kit_checker_builds_and_carries_the_lazy_name_only_as_text():
    """The kit builder refuses a carried checker that imports anything but the standard library; the attach
    package is reached with `__import__` and a bare `except`, so the standalone checker has it off."""
    from alpaca import runbook_kit
    src = runbook_kit.build_checker(runbook_kit.product_root())
    assert '__import__("alpaca.attach.runbooks"' in src


@pytest.mark.parametrize("kind", ["file-exists", "regex-in-file", "json-field"])
@pytest.mark.parametrize("who", ["workdir_real", "worktree_real", "runbook_dir_linked"])
def test_a_link_in_the_worktree_is_not_followed_whichever_argument_is_in_a_linked_spelling(tmp_path, kind, who):
    """Adversarial note N1 (round 2), now a rule: the work folder, the working folder and the runbook folder may
    each be given in a real or a linked spelling, and a link inside the work folder still carries no check out."""
    real = tmp_path / "real"
    wt = real / "wt"
    wt.mkdir(parents=True)
    (real / "private").mkdir()
    (real / "private" / "secret.json").write_text(json.dumps({"pw": "hunter2"}), encoding="utf-8")
    os.symlink(str(real / "private"), str(wt / "l"))
    alias = tmp_path / "alias"
    os.symlink(str(real), str(alias))
    awt = alias / "wt"
    workdir, attach_wt, rb_dir = {"workdir_real": (wt, awt, real), "worktree_real": (awt, wt, alias),
                                  "runbook_dir_linked": (wt, wt, alias)}[who]
    chk = {"id": "c", "type": kind, "path": "l/secret.json", "pattern": "hunter2", "field": "pw", "op": "==",
           "value": "hunter2"}
    code, why = runbook.evaluate(chk, workdir=str(workdir), runbook_dir=str(rb_dir), attach_worktree=str(attach_wt))
    assert code == verdict.BLOCKED and "hunter2" not in why, (who, kind, why)
