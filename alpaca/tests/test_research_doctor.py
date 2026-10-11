"""The research doctor (t-115, architecture.md section 3.9): a pack's tool declaration is checked.

Done bar: a pack whose tool declaration (research.fingerprint.tools) names a tool a run cannot find
fails `alpaca doctor` with one error row naming the pack and the tool (SC-027), on a fresh checkout
with no record too; a pack whose tools resolve shows an ok row naming them; a project with no pack
keeps its exit code. The tool rule is the run's own: the tool PATH contract freeze resolves on,
then the gate PATH fingerprint.take pins on. Silence is never a pass: a git-less copy is walked, an
unreadable declaration is a warn row, and a check module that does not load, raises, returns
nothing or returns a bad level is a warn row.

Fixtures are synthetic and built in tmp_path (alpaca/tests/research_doctorkit.py). No test forks
bubblewrap: the autouse fixture answers the jail probe, and the jail tests set their own answer."""
from __future__ import annotations

import gc
import inspect
import os
import sys
import time

import pytest

from alpaca import cli, doctor, runbook
from alpaca.research import contract, fingerprint, jail, procs
from alpaca.research import doctor as rdoctor
from alpaca.tests import research_doctorkit as k
from alpaca.tests import research_loopkit as kit
from alpaca.tests.conftest import REPO


def _jail(monkeypatch, answer):
    monkeypatch.setattr(jail, "available", lambda *, refresh=False: answer)
    monkeypatch.setattr(jail, "_AVAILABLE", answer)


@pytest.fixture(autouse=True)
def jail_works(monkeypatch):
    _jail(monkeypatch, (True, "bubblewrap test"))


def _no_jail(monkeypatch):
    _jail(monkeypatch, (False, "bwrap: synthetic failure"))


def _tool(folder, name, body="#!/bin/sh\nexit 0\n"):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(path, 0o755)
    return path


def _on_path(monkeypatch, folder, last=False):
    here = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", "%s:%s" % (here, folder) if last else "%s:%s" % (folder, here))


def _key(row):
    return (row["name"], row["level"], row["detail"])


def _research(rows):
    return [r for r in rows if k.text(r).startswith("research")]


# ------------------------------------------------------------ the missing tool, where it counts
def test_a_record_less_project_reports_the_missing_pack_tool(project):
    k.lay_pack(project, tools=(k.MISSING,))
    k.commit(project)
    assert not os.path.isfile(doctor.paths.db_path(project))
    errs, _w, _b = k.fmt3_findings(project, k.RUNBOOK)
    assert errs == [], errs
    rows = doctor.checks(project)
    hit = k.rows_naming(rows, k.MISSING, k.PACK)
    assert hit, "no error row names pack %s and tool %s; rows: %s" % (
        k.PACK, k.MISSING, [k.text(r) for r in rows])
    assert "Alpaca never installs" in k.text(hit[0]) and k.RUNBOOK in k.text(hit[0]), hit


def test_an_onboarded_project_fails_doctor_once_per_missing_tool(project, capsys):
    k.onboarded(project)
    k.lay_pack(project, tools=(k.MISSING,))
    k.commit(project)
    capsys.readouterr()
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if k.MISSING in l]
    assert rc == 2, out
    assert "GATE alpaca-doctor: FAIL" in out, out
    assert len(lines) == 1, lines
    assert "ERROR" in lines[0] and k.PACK in lines[0], lines


def test_the_research_row_appears_once_with_a_record(project):
    k.onboarded(project)
    k.lay_pack(project, tools=(k.MISSING,))
    k.commit(project)
    assert os.path.isfile(doctor.paths.db_path(project))
    rows = doctor.checks(project)
    tools = [r for r in rows if r["name"] == "research-tools"]
    assert len(tools) == 1 and tools[0]["level"] == "error", tools
    assert len([r for r in rows if r["name"] == "research-jail"]) == 1, rows
    assert len([r for r in rows if r["name"] == "research-store"]) == 1, rows


def test_a_git_less_copy_does_not_pass_silently(project):
    assert not os.path.isdir(os.path.join(project, ".git"))
    before = {_key(r) for r in doctor.checks(project)}
    k.lay_pack(project, tools=(k.MISSING,))   # no k.commit: no git metadata
    after = doctor.checks(project)
    new = [r for r in after if _key(r) not in before and r["level"] != "ok"]
    assert new, "a pack with a missing tool in a git-less copy adds no warn or error row; rows: %s" % (
        [k.text(r) for r in after])
    assert k.rows_naming(after, k.MISSING, k.PACK), [k.text(r) for r in after]


def test_the_module_is_built_and_keeps_the_design_signature():
    src = inspect.getsource(rdoctor)
    assert "raise NotImplementedError(" not in src, "alpaca/research/doctor.py is still the t-129 stub"
    params = list(inspect.signature(rdoctor.checks).parameters.values())
    assert [p.name for p in params[:2]] == ["root", "conn"]
    for extra in params[2:]:
        assert extra.kind is extra.KEYWORD_ONLY and extra.default is not extra.empty


def test_checks_with_no_connection_returns_triples_with_the_finding(project):
    k.lay_pack(project, tools=(k.MISSING,))
    k.commit(project)
    rows = rdoctor.checks(project, None)
    assert isinstance(rows, list)
    for row in rows:
        assert isinstance(row, tuple) and len(row) == 3 and all(isinstance(x, str) for x in row), row
        assert row[1] in ("ok", "warn", "error"), row
    assert k.rows_naming(rows, k.MISSING, k.PACK), rows


# ------------------------------------------------------------------ the run's resolution rule
def _versioned_symlink(tmp_path, name):
    opt = tmp_path / "opt" / (name + "-1.2") / "bin"
    target = _tool(str(opt), name + "-1.2")
    binp = tmp_path / "bin"
    binp.mkdir(exist_ok=True)
    os.symlink(target, binp / name)
    return str(binp)


def test_a_tool_the_gate_path_cannot_find_is_an_error(project, tmp_path, monkeypatch):
    name = "faketool-t115v"
    binp = _versioned_symlink(tmp_path, name)
    _on_path(monkeypatch, binp)
    k.lay_pack(project, tools=(name,))
    k.commit(project)
    pin = fingerprint.resolve_tools((name,), procs.gate_path((os.path.realpath(os.path.join(binp, name)),)))[name]
    assert pin.error == "not found on the gate PATH"     # the run's own rule refuses it
    rows = doctor.checks(project)
    hit = k.rows_naming(rows, name, k.PACK)
    assert hit and "gate PATH" in k.text(hit[0]), [k.text(r) for r in rows]


def _sbin_only():
    gate = jail.GATE_PATH.split(":")
    for folder in ("/usr/sbin", "/sbin"):
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            full = os.path.join(folder, name)
            if (os.path.isfile(full) and os.access(full, os.X_OK) and name.isidentifier()
                    and not any(os.path.exists(os.path.join(d, name)) for d in gate)):
                return folder, name
    return None, None


def test_a_tool_only_in_sbin_is_an_error(project, monkeypatch):
    folder, name = _sbin_only()
    if not name:
        pytest.skip("no program here lives only in /usr/sbin or /sbin")
    _on_path(monkeypatch, folder, last=True)
    k.lay_pack(project, tools=(name,))
    k.commit(project)
    rows = doctor.checks(project)
    assert k.rows_naming(rows, name, k.PACK), [k.text(r) for r in rows]


def test_a_tool_a_run_can_find_is_not_an_error(project, tmp_path, monkeypatch):
    name = "goodtool-t115"
    _on_path(monkeypatch, os.path.dirname(_tool(str(tmp_path / "localbin"), name)))
    k.lay_pack(project, tools=(name,))
    k.commit(project)
    rows = doctor.checks(project)
    assert not k.rows_naming(rows, name, levels=("error", "warn")), [k.text(r) for r in rows]
    assert (k.rows_naming(rows, name, levels=("ok",)) or k.rows_naming(rows, k.PACK, levels=("ok",))), \
        "no ok row shows the pack's tools were checked: %s" % [k.text(r) for r in rows]


def test_a_tool_under_the_home_folder_is_an_error(project, tmp_path, monkeypatch):
    name = "hometool-t115"
    home = tmp_path / "home"
    folder = os.path.dirname(_tool(str(home / "bin"), name))
    monkeypatch.setenv("HOME", str(home))
    _on_path(monkeypatch, folder)
    k.lay_pack(project, tools=(name,))
    k.commit(project)
    import shutil
    assert shutil.which(name)                       # the shell finds it; a run does not
    rows = doctor.checks(project)
    hit = k.rows_naming(rows, name, k.PACK)
    assert hit and "tool PATH" in k.text(hit[0]), [k.text(r) for r in rows]


def test_a_tool_inside_the_project_is_an_error(project, monkeypatch):
    name = "projtool-t115"
    folder = os.path.dirname(_tool(os.path.join(project, "tools", "bin"), name))
    _on_path(monkeypatch, folder)
    k.lay_pack(project, tools=(name,))
    k.commit(project)
    rows = doctor.checks(project)
    hit = k.rows_naming(rows, name, k.PACK)
    assert hit and "tool PATH" in k.text(hit[0]), [k.text(r) for r in rows]


def test_a_tool_linked_into_a_forbidden_folder_is_an_error(project, tmp_path, monkeypatch):
    name = "linktool-t115"
    target = _tool(os.path.join(project, "tools"), name)
    binp = tmp_path / "linkbin"
    binp.mkdir()
    os.symlink(target, binp / name)
    _on_path(monkeypatch, str(binp))
    k.lay_pack(project, tools=(name,))
    k.commit(project)
    rows = doctor.checks(project)
    hit = k.rows_naming(rows, name, k.PACK)
    assert hit and "inside forbidden path" in k.text(hit[0]), [k.text(r) for r in rows]


SHAPES = ("plain", "missing", "versioned", "home", "project")


@pytest.mark.parametrize("shape", SHAPES)
def test_doctor_agrees_with_freeze_and_the_gate(project, tmp_path, monkeypatch, shape):
    name = "shape-t115-" + shape
    if shape == "plain":
        _on_path(monkeypatch, os.path.dirname(_tool(str(tmp_path / "localbin"), name)))
    elif shape == "versioned":
        _on_path(monkeypatch, _versioned_symlink(tmp_path, name))
    elif shape == "home":
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        _on_path(monkeypatch, os.path.dirname(_tool(str(tmp_path / "home" / "bin"), name)))
    elif shape == "project":
        _on_path(monkeypatch, os.path.dirname(_tool(os.path.join(project, "tools", "bin"), name)))
    kit.write(project, "project.yaml", "name: doctor-agree\n")
    k.lay_pack(project, tools=(name,))
    k.commit(project)
    frozen = contract.preview(project, k.RUNBOOK)["contract"]
    reals = list(frozen["tools_real"])
    gate = fingerprint.resolve_tools((name,), procs.gate_path(tuple(r for r in reals if r)))
    run_fails = None in reals or any(pin.error for pin in gate.values())
    doctor_fails = bool(k.rows_naming(rdoctor.checks(project, None), name, k.PACK))
    assert doctor_fails == run_fails, (shape, reals, gate, rdoctor.checks(project, None))
    assert run_fails == (shape != "plain"), (shape, reals, gate)


# ------------------------------------------------------------------- which declaration counts
def test_the_runbook_layer_declaration_is_the_one_checked(project):
    own = "nosuchtool-t115b"
    k.lay_pack(project, tools=("sh",), runbook_tools=[own])
    k.commit(project)
    _e, _w, block = k.fmt3_findings(project, k.RUNBOOK)
    assert block["fingerprint"]["tools"] == [own]
    rows = doctor.checks(project)
    assert k.rows_naming(rows, own), [k.text(r) for r in rows]


def test_an_empty_declaration_is_not_a_finding(project):
    k.lay_pack(project, tools=())
    k.commit(project)
    rows = doctor.checks(project)
    assert not k.rows_naming(rows, k.PACK, levels=("warn", "error")), [k.text(r) for r in rows]
    assert k.rows_naming(rows, k.PACK, "declares no tools", levels=("ok",)), [k.text(r) for r in rows]


def test_a_pack_that_declares_nothing_is_no_doctor_fail(project):
    k.lay_pack(project, tools=None)
    k.commit(project)
    errs, _w, _b = k.fmt3_findings(project, k.RUNBOOK)
    assert "FIELD-MISSING" in errs
    rows = doctor.checks(project)
    assert not k.rows_naming(rows, k.PACK, levels=("error",)), [k.text(r) for r in rows]
    assert k.rows_naming(rows, k.PACK, "declares no tools", levels=("ok",)), [k.text(r) for r in rows]


def test_a_runbook_whose_declaration_cannot_be_read_is_reported(project):
    k.lay_pack(project, tools=(k.MISSING,))
    os.remove(os.path.join(project, k.PACK_DIR, "pack.yaml"))
    k.commit(project)
    rows = doctor.checks(project)
    hit = [r for r in rows if r["level"] != "ok" and ("rb.yaml" in k.text(r) or k.PACK_DIR in k.text(r))]
    assert hit, [k.text(r) for r in rows]


# --------------------------------------------- a declaration the run's validator refuses
MALFORMED = {
    "scalar-tools": ("fingerprint:\n    tools: nosuchtool-t115m\n", "FIELD-NOT-LIST"),
    "mapping-tools": ("fingerprint:\n    tools: {nosuchtool-t115m: 1}\n", "FIELD-NOT-LIST"),
    "list-fingerprint": ("fingerprint: [nosuchtool-t115m]\n", "FIELD-TYPE"),
    "misspelt-key": ("fingerprint:\n    tool: [nosuchtool-t115m]\n", "FIELD-UNKNOWN"),
}


def _malformed_pack(project, body, where=k.PACK_DIR, pack=k.PACK):
    k.lay_pack(project, tools=("sh",), where=where)
    kit.write(project, where + "/pack.yaml", "pack: %s\nresearch:\n  %s" % (pack, body))


@pytest.mark.parametrize("shape", sorted(MALFORMED))
def test_a_declaration_the_run_refuses_is_an_error_not_declares_nothing(project, shape):
    """A `tools` that is not a list (the likeliest typo: one tool written without brackets), a
    fingerprint that is not a mapping or a misspelt key: the run's validator refuses each, so a run
    start refuses the pack; doctor names the pack and the validator's code in an error row and
    never says the pack declares no tools."""
    body, code = MALFORMED[shape]
    _malformed_pack(project, body)
    k.commit(project)
    errs, _w, _b = k.fmt3_findings(project, k.RUNBOOK)
    assert code in errs, errs                        # the run's own rule refuses it
    rows = rdoctor.checks(project, None)
    tool_rows = [r for r in rows if r[0] == "research-tools"]
    hit = [r for r in tool_rows if r[1] == "error" and code in r[2]]
    assert len(hit) == 1, tool_rows
    # the refused value is never read as names: no row per character or per mapping key
    assert [r for r in tool_rows if r[1] != "ok"] == hit, tool_rows
    assert hit[0][2].startswith("pack %s declares its tools in a form a run refuses" % k.PACK), hit
    assert k.RUNBOOK in hit[0][2] and "research.fingerprint" in hit[0][2], hit
    assert not any("declares no tools" in r[2] for r in tool_rows), tool_rows


def test_an_onboarded_project_with_a_scalar_tools_fails_doctor(project, capsys):
    k.onboarded(project)
    _malformed_pack(project, MALFORMED["scalar-tools"][0])
    k.commit(project)
    capsys.readouterr()
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if "FIELD-NOT-LIST" in l]
    assert rc == 2 and "GATE alpaca-doctor: FAIL" in out, out
    assert len(lines) == 1 and lines[0].startswith("ERROR") and k.PACK in lines[0], lines


def test_one_malformed_defaults_file_is_one_row_for_its_runbooks(project):
    _malformed_pack(project, MALFORMED["scalar-tools"][0])
    second = k.PACK_DIR + "/rb2.yaml"
    text = open(os.path.join(project, k.RUNBOOK), encoding="utf-8").read()
    kit.write(project, second, text.replace("id: loop-improve", "id: loop-improve-two", 1))
    k.commit(project)
    rows = rdoctor.checks(project, None)
    hit = [r for r in rows if r[1] == "error" and "FIELD-NOT-LIST" in r[2]]
    assert len(hit) == 1, rows
    assert k.RUNBOOK in hit[0][2] and second in hit[0][2], hit


# ------------------------------------------------------- the row names the pack and the tool
def test_the_error_row_names_the_pack_from_its_defaults_file(project):
    """The pack's name (pack.yaml `pack:`) differs from its folder and from the runbook id, so the
    row's pack name cannot come from the runbook path or the id."""
    where = "packs/folderx"
    rel = k.lay_pack(project, tools=(k.MISSING,), where=where)
    kit.write(project, where + "/pack.yaml", "pack: namedpack\nresearch:\n  fingerprint:\n"
              "    tools: [%s]\n" % k.MISSING)
    k.commit(project)
    rows = rdoctor.checks(project, None)
    hit = [r for r in rows if r[1] == "error" and k.MISSING in r[2]]
    assert len(hit) == 1, rows
    assert hit[0][2].startswith("pack namedpack declares tool %s (%s)" % (k.MISSING, rel)), hit


def test_two_runbooks_of_one_pack_give_one_row_per_missing_tool(project):
    k.lay_pack(project, tools=(k.MISSING,))
    second = k.PACK_DIR + "/rb2.yaml"
    text = open(os.path.join(project, k.RUNBOOK), encoding="utf-8").read()
    kit.write(project, second, text.replace("id: loop-improve", "id: loop-improve-two", 1))
    k.commit(project)
    rows = rdoctor.checks(project, None)
    hit = [r for r in rows if r[1] == "error" and k.MISSING in r[2]]
    assert len(hit) == 1, rows
    assert hit[0][2].startswith("pack %s declares tool %s (%s, %s)"
                                % (k.PACK, k.MISSING, k.RUNBOOK, second)), hit


@pytest.mark.parametrize("name", ["sub/dir-tool-t115", "123"], ids=["path", "not-a-string"])
def test_a_name_that_is_not_bare_is_tool_invalid_even_when_it_resolves(project, tmp_path, monkeypatch,
                                                                       name):
    """runbook.TOOL refuses a name with a `/` or a value that is not text (TOOL-INVALID), so a run
    start refuses it. Joined onto a PATH folder, `sub/dir-tool-t115` resolves here (the freeze
    helper alone would find it), so only the bare-name rule keeps doctor from an ok row."""
    folder = tmp_path / "pathdir"
    _tool(str(folder / "sub"), "dir-tool-t115")
    _on_path(monkeypatch, str(folder))
    if "/" in name:
        dirs = contract._tool_dirs(contract._forbidden(project, project, [], []))
        assert contract._resolve_tool(name, dirs)    # the unchecked join does find it
    k.lay_pack(project, tools=("sh", name))
    k.commit(project)
    errs, _w, _b = k.fmt3_findings(project, k.RUNBOOK)
    assert "TOOL-INVALID" in errs, errs
    rows = rdoctor.checks(project, None)
    hit = [r for r in rows if r[1] == "error" and "TOOL-INVALID" in r[2]]
    assert len(hit) == 1 and k.PACK in hit[0][2] and name in hit[0][2], rows
    ok = [r for r in rows if r[0] == "research-tools" and r[1] == "ok"]
    assert ok and all(name not in r[2] for r in ok), ok


# ------------------------------------------------------------------- a git-less folder walk
def test_the_folder_walk_skips_a_fifo_named_yaml(project):
    """A git-less copy is walked; a FIFO named *.yaml would block the read of its head forever. Run
    in a child with a timeout, so a regression fails instead of hanging the suite."""
    k.lay_pack(project, tools=(k.MISSING,))
    os.mkfifo(os.path.join(project, k.PACK_DIR, "evil.yaml"))
    assert not os.path.isdir(os.path.join(project, ".git"))
    code = ("import sys\nsys.path.insert(0, sys.argv[1])\n"
            "from alpaca.research import doctor, jail\n"
            "jail.available = lambda *, refresh=False: (True, 'test')\n"
            "print(doctor.checks(sys.argv[2], None))\n")
    import subprocess
    try:
        done = subprocess.run([sys.executable, "-c", code, REPO, project], capture_output=True,
                              text=True, timeout=60, cwd=project)
    except subprocess.TimeoutExpired:
        pytest.fail("the research doctor blocked on a FIFO named *.yaml in a git-less copy")
    assert done.returncode == 0, done.stderr
    assert k.MISSING in done.stdout and "evil.yaml" not in done.stdout, done.stdout


def test_an_unrelated_broken_yaml_is_not_a_finding(project):
    k.lay_pack(project, tools=("sh",))
    kit.write(project, "ci/config.yml", "key: [unclosed\n\tbad: tab\n")
    k.commit(project)
    rows = doctor.checks(project)
    assert not [r for r in rows if r["level"] == "error" and "config.yml" in k.text(r)], \
        [k.text(r) for r in rows]


# ------------------------------------------------------------------- what is listed as a pack
PLACES = {
    ".alpaca/worktrees/other/packs/synthpack": "nosuchtool-t115w",
    "contracts/research/frozen/packs/synthpack": "nosuchtool-t115c",
    "templates/research/synthpack": "nosuchtool-t115t",
    "vendor/thing/packs/synthpack": "nosuchtool-t115v",
    "research-refs/ref/packs/synthpack": "nosuchtool-t115r",
    "alpaca/tests/fixtures/x/synthpack": "nosuchtool-t115f",
}


@pytest.mark.parametrize("with_git", [True, False])
def test_runbook_copies_outside_packs_are_not_checked(project, with_git):
    for where, tool in PLACES.items():
        k.lay_pack(project, tools=(tool,), where=where)
    if with_git:
        k.commit(project)
    rows = doctor.checks(project)
    for tool in PLACES.values():
        assert not k.rows_naming(rows, tool, levels=("warn", "error")), (tool, [k.text(r) for r in rows])


@pytest.mark.parametrize("with_git", [True, False])
def test_runbooks_under_alpaca_are_not_listed(project, with_git):
    k.lay_pack(project, tools=(k.MISSING,), where="alpaca/tests/fixtures/x")
    if with_git:
        k.commit(project)
    rows = rdoctor.checks(project, None)
    assert not [r for r in rows if r[1] != "ok"], rows
    source = "git" if with_git else "a folder walk"
    assert rows == [("research-tools", "ok", "no format 3 runbook in this project (listed by %s)" % source)]


# -------------------------------------------------------------------- the exit code it keeps
def test_an_onboarded_packless_project_passes(project, capsys):
    k.onboarded(project)
    capsys.readouterr()
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0 and "GATE alpaca-doctor: PASS" in out, out


def test_an_onboarded_packless_git_project_passes(project, capsys):
    k.onboarded(project)
    k.commit(project)
    capsys.readouterr()
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0 and "GATE alpaca-doctor: PASS" in out, out


def test_a_pack_with_present_tools_passes_and_is_shown_checked(project, capsys):
    k.onboarded(project)
    k.lay_pack(project, tools=("sh",))
    k.commit(project)
    capsys.readouterr()
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0 and "GATE alpaca-doctor: PASS" in out, out
    rows = doctor.checks(project)
    assert k.rows_naming(rows, k.PACK, "sh at ", levels=("ok",)), [k.text(r) for r in rows]


# ------------------------------------------------------------------------------ the jail
def test_no_jail_is_not_a_doctor_fail(project, capsys, monkeypatch):
    _no_jail(monkeypatch)
    k.onboarded(project)
    capsys.readouterr()
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc != 2 and "ERROR" not in out, out


def test_the_research_module_reports_no_jail_without_an_error(project, monkeypatch):
    _no_jail(monkeypatch)
    rows = rdoctor.checks(project, None)
    assert not [r for r in rows if r[1] == "error"], rows


def test_jail_unavailable_is_a_warn_naming_the_fix(project, monkeypatch):
    _no_jail(monkeypatch)
    k.lay_pack(project, tools=("sh",))
    k.commit(project)
    rows = [r for r in rdoctor.checks(project, None) if r[0] == "research-jail"]
    assert len(rows) == 1 and rows[0][1] == "warn", rows
    for words in ("RUN-JAIL-UNAVAILABLE", "synthetic failure", "install bubblewrap"):
        assert words in rows[0][2], rows


def test_jail_row_only_with_a_runbook(project):
    rows = rdoctor.checks(project, None)
    assert not [r for r in rows if r[0] in ("research-jail", "research-store")], rows
    k.lay_pack(project, tools=("sh",))
    rows = rdoctor.checks(project, None)
    assert ("research-jail", "ok", "bubblewrap works: bubblewrap test") in rows, rows


# ------------------------------------------------------------------- the reproducer store
def _store(project, rows=None):
    rows = rdoctor.checks(project, None) if rows is None else rows
    got = [r for r in rows if r[0] == "research-store"]
    assert len(got) == 1, rows
    return got[0]


def test_an_ignored_untracked_store_is_ok(project):
    k.lay_pack(project, tools=("sh",))
    k.commit(project)
    assert _store(project)[1] == "ok"


def test_a_tracked_store_is_a_warn(project):
    k.lay_pack(project, tools=("sh",))
    k.commit(project)
    kit.write(project, "contracts/research/rb/case/test.sh", "exit 0\n")
    kit.git(project, "add", "-f", "contracts/research/rb/case/test.sh")
    kit.git(project, "commit", "-q", "-m", "store")
    row = _store(project)
    assert row[1] == "warn" and "RUN-STORE-TRACKED" in row[2] and "contracts/research/rb/case/test.sh" in row[2], row


def test_an_unignored_store_is_a_warn(project):
    k.lay_pack(project, tools=("sh",))
    k.commit(project, ignore=".alpaca/\n")
    row = _store(project)
    assert row[1] == "warn" and ".gitignore" in row[2], row


def test_a_git_less_store_is_ok(project):
    k.lay_pack(project, tools=("sh",))
    assert _store(project) == ("research-store", "ok", "not a git work tree: nothing can be tracked")


# --------------------------------------------------------- the seam in alpaca/doctor.py
def test_an_unloadable_research_doctor_is_reported(project, monkeypatch):
    k.onboarded(project)                            # every other row is ok here
    k.lay_pack(project, tools=(k.MISSING,))
    k.commit(project)
    import alpaca.research
    monkeypatch.delattr(alpaca.research, "doctor", raising=False)
    monkeypatch.setitem(sys.modules, "alpaca.research.doctor", None)
    rows = doctor.checks(project)
    here = os.path.dirname(project)                 # the tmp folder name carries the test name
    bad = [r for r in rows if r["level"] != "ok" and "research" in k.text(r).replace(here, "").lower()]
    assert bad, [k.text(r) for r in rows]
    assert ("research", "warn") == (bad[0]["name"], bad[0]["level"]) and "not loaded" in bad[0]["detail"], bad


def _stub_module(monkeypatch, checks):
    import types
    mod = types.ModuleType("alpaca.t115_stub_checks")
    mod.checks = checks
    monkeypatch.setitem(sys.modules, "alpaca.t115_stub_checks", mod)
    monkeypatch.setattr(doctor, "RECORD_FREE_MODULES", (("t115_stub_checks", "stublabel"),))


@pytest.mark.parametrize("answer,words", [
    (lambda root, conn: [], "returned no finding"),
    (lambda root, conn: [("x", "fatal", "d")], "malformed row"),
    (lambda root, conn: [("x", "ok")], "malformed row"),
    (lambda root, conn: 1 / 0, "checks skipped: ZeroDivisionError"),
], ids=["nothing", "bad-level", "short-row", "raises"])
def test_a_module_that_answers_badly_is_a_warn_row(project, capsys, monkeypatch, answer, words):
    _stub_module(monkeypatch, answer)
    rows = doctor.checks(project)
    hit = [r for r in rows if r["name"] == "stublabel"]
    assert len(hit) == 1 and hit[0]["level"] == "warn" and words in hit[0]["detail"], hit
    cli.main(["doctor"])                            # cmd_doctor ranks every level without a crash
    assert "stublabel" in capsys.readouterr().out


def test_a_module_returning_nothing_is_a_row(project, monkeypatch):
    _stub_module(monkeypatch, lambda root, conn: [])
    assert [r for r in doctor.checks(project) if r["name"] == "stublabel"][0]["level"] == "warn"


def test_a_bad_level_is_a_row(project, monkeypatch):
    _stub_module(monkeypatch, lambda root, conn: [("x", "fatal", "d")])
    rows = doctor.checks(project)
    assert not [r for r in rows if r["level"] == "fatal"], rows
    assert [r for r in rows if r["name"] == "stublabel"][0]["level"] == "warn"


def test_the_module_gets_no_connection_and_runs_before_the_record(project, monkeypatch):
    seen = []
    _stub_module(monkeypatch, lambda root, conn: seen.append((root, conn)) or [("x", "ok", "d")])
    rows = doctor.checks(project)
    assert seen == [(project, None)], seen
    names = [r["name"] for r in rows]
    assert names.index("x") < names.index("record"), names


def test_a_good_row_passes_through_unchanged(project, monkeypatch):
    _stub_module(monkeypatch, lambda root, conn: [("x", "error", "d")])
    rows = doctor.checks(project)
    assert {"name": "x", "level": "error", "detail": "d"} in rows, rows


# ----------------------------------------------------------------- this repo and read-only
def _tracked_format3():
    import subprocess
    got = subprocess.run(["git", "-C", REPO, "ls-files", "-z", "--", "*.yaml", "*.yml"],
                         capture_output=True, check=True)
    for rel in sorted(x.decode() for x in got.stdout.split(b"\0") if x):
        if rel.startswith((".alpaca/", "vendor/", "templates/", "research-refs/")):
            continue
        path = os.path.join(REPO, rel)
        if not os.path.isfile(path):
            continue
        data, _rep = runbook.load(path)
        if isinstance(data, dict) and runbook.research_block(data) is not None:
            yield rel, data, os.path.dirname(path)


def test_every_tracked_pack_tool_resolves_by_the_gate_rule():
    seen = 0
    for rel, data, base in _tracked_format3():
        seen += 1
        block, _l, _r = runbook.expand_research(data, base)
        for tool in ((block or {}).get("fingerprint") or {}).get("tools") or []:
            real = contract._resolve_tool(tool, contract._tool_dirs([]))
            pin = fingerprint.resolve_tools((tool,), procs.gate_path((real,) if real else ()))[tool]
            assert pin.error is None, (rel, tool, pin.error)
    assert seen >= 2


def test_the_research_doctor_on_this_repo_has_no_error_and_is_quick():
    t0 = time.monotonic()
    rows = rdoctor.checks(REPO, None)
    took = time.monotonic() - t0
    errors = [r for r in rows if r[1] == "error" and "jail" not in r[0].lower()]
    assert not errors, errors
    assert took < 60, "research doctor took %.1f s on the repo" % took


def _snapshot(root):
    """Mode, size and mtime of every entry but .git. Left out: the -shm file and an empty -wal file
    SQLite keeps beside the WAL record while a read-only connection (db.connect_readonly, which
    documents them) reads it, and the size and mtime of the folder they appear in. A record write
    still shows: it changes the record file or leaves the -wal file with content (op-008 t-571)."""
    gc.collect()    # setup's leaked connections close (and checkpoint) now, not inside the doctor
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            if name.endswith("-shm") or (name.endswith("-wal") and st.st_size == 0):
                continue
            out[os.path.relpath(p, root)] = (st.st_mode, st.st_size, st.st_mtime_ns)
    out[".alpaca"] = out.get(".alpaca", (None,))[:1]          # its mode; a tmpfs folder size counts entries
    return out


@pytest.mark.parametrize("with_record", [False, True])
def test_doctor_writes_nothing_in_a_pack_project(project, with_record):
    if with_record:
        k.onboarded(project)
    k.lay_pack(project, tools=(k.MISSING, "sh"))
    k.commit(project)
    before = _snapshot(project)
    doctor.checks(project)
    rdoctor.checks(project, None)
    assert _snapshot(project) == before
