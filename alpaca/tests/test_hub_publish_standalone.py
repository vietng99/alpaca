"""setup/hub-publish.py: the standalone publisher, and the one implementation behind `alpaca hub publish`.

  * It runs alone: a copy in an empty folder, under `python3 -I` (no PYTHONPATH, no user site),
    imports only the standard library and writes the tile.
  * Its --print output equals `alpaca hub publish --print` for the same workspace and settings
    (exact bytes with a fixed clock in process; every key but `updated` through two processes).
  * `alpaca hub publish` uses its functions, not a copy of them.
  * The review lows of the publisher: the docstring names only flags the parser has (L4); a
    relative --drop is read from the caller's folder (L3); the session-end tile is built from a
    data.json refreshed from the record (L2).
"""
import ast
import datetime
import json
import os
import re
import shutil
import subprocess
import sys

import pytest
import yaml

from alpaca import cli, hub_publish as pub
from alpaca.tests.conftest import REPO

SCRIPT = os.path.join(REPO, "setup", "hub-publish.py")
ME = pub.current_user()
DATA = {"generated": "2026-01-01T00:00:00+00:00", "pulse": {
    "ops_open": 1, "tasks": {"open": 2, "doing": 1, "done": 3},
    "now": {"level": "L3", "next_action": "t-1 [doing] write the page",
            "ops": [{"id": "op-1", "status": "open", "title": "Ship the page", "rows_total": 4,
                     "rows_done": 1}],
            "sessions": {"work": [{"active": True}]}}}}
FIXED = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone(datetime.timedelta(hours=7)))


@pytest.fixture(autouse=True)
def no_env_drop(monkeypatch):
    monkeypatch.delenv("ALPACA_HUB_DROP", raising=False)


def configure(root, **hub):
    settings = {"enabled": False, "drop": "/srv/alpaca-hub/tiles", "slug": None, "name": None,
                "href": None, "what": None}
    settings.update(hub)
    with open(os.path.join(root, "project.yaml"), "w", encoding="utf-8") as fh:
        yaml.safe_dump({"name": "Demo Space", "hub": settings}, fh, sort_keys=False)


def write_data(root, data=DATA):
    with open(os.path.join(root, "data.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def script(*args, cwd=None, python=(sys.executable,), path=SCRIPT):
    return subprocess.run([*python, path, *args], capture_output=True, text=True,
                          encoding="utf-8", cwd=cwd, timeout=60)


SETTINGS = dict(slug="demo-space", name="Demo Space", href="https://demo.example.com/hub/",
                what="A demo workspace")


def script_args(root):
    return ["--root", root, "--slug", SETTINGS["slug"], "--name", SETTINGS["name"],
            "--href", SETTINGS["href"], "--what", SETTINGS["what"]]


# ---- it runs alone -------------------------------------------------------------------------------

def test_the_script_imports_only_the_standard_library():
    tree = ast.parse(open(SCRIPT, encoding="utf-8").read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import"
            names.add(node.module.split(".")[0])
    assert names and names <= set(sys.stdlib_module_names), names - set(sys.stdlib_module_names)
    assert os.stat(SCRIPT).st_mode & 0o100, "the script is executable"


def test_a_lone_copy_publishes_a_tile(tmp_path):
    lone = tmp_path / "elsewhere"
    lone.mkdir()
    copy = lone / "hub-publish.py"
    shutil.copy(SCRIPT, copy)
    work = tmp_path / "work"
    work.mkdir()
    write_data(str(work))
    drop = tmp_path / "drop"
    drop.mkdir()
    run = script(*script_args(str(work)), "--drop", str(drop), cwd=str(lone),
                 python=(sys.executable, "-I"), path=str(copy))
    assert run.returncode == 0, run.stderr
    tile_path = drop / ("%s--demo-space.json" % ME)
    assert run.stdout.strip() == "published %s" % tile_path
    assert os.stat(tile_path).st_mode & 0o777 == 0o640
    tile = json.loads(tile_path.read_text(encoding="ascii"))
    assert tile["status"]["headline"] == "op-1: Ship the page" and tile["user"] == ME


def test_the_script_refuses_another_user_and_bad_input(tmp_path):
    write_data(str(tmp_path))
    run = script("--root", str(tmp_path), "--slug", "x", "--name", "X", "--user", "someone-else",
                 "--print")
    assert run.returncode == 2 and "not the user running this script" in run.stderr
    run = script("--root", str(tmp_path), "--slug", "Bad Slug", "--name", "X", "--print")
    assert run.returncode == 2 and "--slug must be" in run.stderr
    run = script("--root", str(tmp_path), "--slug", "x", "--name", "X", "--href", "http://a.example.com/")
    assert run.returncode == 2 and "https" in run.stderr
    run = script("--root", str(tmp_path), "--name", "X")
    assert run.returncode == 64
    run = script("--root", str(tmp_path / "none"), "--slug", "x", "--name", "X", "--print")
    assert run.returncode == 2 and "neither" in run.stderr


def test_the_script_timer_runs_the_script_itself_and_installs_nothing(tmp_path):
    write_data(str(tmp_path))
    before = sorted(os.listdir(tmp_path))
    run = script(*script_args(str(tmp_path)), "--drop", "rel-drop", "--timer", cwd=str(tmp_path))
    assert run.returncode == 0, run.stderr
    out = run.stdout
    assert "~/.config/systemd/user/hub-publish-demo-space.service" in out
    assert "~/.config/systemd/user/hub-publish-demo-space.timer" in out
    exec_line = next(line for line in out.splitlines() if line.startswith("ExecStart="))
    assert exec_line == ('ExecStart="%s" "%s" --root "%s" --slug "demo-space" --name "Demo Space" '
                         '--href "https://demo.example.com/hub/" --what "A demo workspace" --drop "%s"'
                         % (sys.executable, SCRIPT, tmp_path, tmp_path / "rel-drop"))
    assert "OnUnitActiveSec=60s" in out and "UMask=0027" in out
    assert sorted(os.listdir(tmp_path)) == before


# ---- one implementation, one output --------------------------------------------------------------

def test_alpaca_hub_publish_runs_the_script_code():
    for name in ("build", "render", "write_tile", "check_href", "clip", "from_data", "from_board"):
        code = getattr(pub, name).__code__
        assert os.path.samefile(code.co_filename, SCRIPT), name
    assert pub.PublishError is pub.core.PublishError


def test_print_bytes_are_identical_with_a_fixed_clock(project, monkeypatch, capsys):
    write_data(project)
    configure(project, **SETTINGS)
    monkeypatch.setattr(pub.core, "current_time", lambda: FIXED)
    assert cli.main(["hub", "publish", "--print"]) == cli.PASS
    alpaca_out = capsys.readouterr().out
    assert pub.core.main(script_args(project) + ["--print"]) == 0
    script_out = capsys.readouterr().out
    assert alpaca_out == script_out
    assert json.loads(script_out)["updated"] == "2026-01-02T03:04:05+07:00"


def test_script_print_equals_alpaca_hub_publish_print(project, capsys):
    write_data(project)
    configure(project, **SETTINGS)
    assert cli.main(["hub", "publish", "--print"]) == cli.PASS
    alpaca_tile = json.loads(capsys.readouterr().out)
    run = script(*script_args(project), "--print")
    assert run.returncode == 0, run.stderr
    script_tile = json.loads(run.stdout)
    for tile in (alpaca_tile, script_tile):
        assert datetime.datetime.fromisoformat(tile.pop("updated")).tzinfo is not None
    assert script_tile == alpaca_tile


# ---- review lows ---------------------------------------------------------------------------------

def test_the_docstring_names_only_flags_the_parser_has():
    """L4: the module docstring listed `alpaca hub timer [--python PATH]`, a flag that never existed."""
    import argparse
    doc = pub.__doc__
    verbs = doc[doc.index("Verbs:"):]
    parser = argparse.ArgumentParser()
    pub._parser(parser.add_subparsers())
    hub = parser._subparsers._group_actions[0].choices["hub"]
    verbs_parsers = hub._subparsers._group_actions[0].choices
    for line in verbs.splitlines():
        found = re.search(r"alpaca hub (\w+)(.*)", line)
        if not found:
            continue
        verb, rest = found.group(1), found.group(2)
        known = {s for a in verbs_parsers[verb]._actions for s in a.option_strings}
        for flag in re.findall(r"--[a-z-]+", rest.split("   ")[0]):
            assert flag in known, "docstring names %s for alpaca hub %s" % (flag, verb)


def _install_copy(tmp_path):
    """A workspace laid out as a clone is: bin/alpaca and bin/alpaca-python, the package and setup/
    (links to this checkout), ALPACA-MANIFEST, and a .venv python that is this interpreter."""
    inst = tmp_path / "inst"
    (inst / "bin").mkdir(parents=True)
    for name in ("alpaca", "alpaca-python"):
        shutil.copy2(os.path.join(REPO, "bin", name), str(inst / "bin" / name))
    for name in ("alpaca", "setup"):
        os.symlink(os.path.join(REPO, name), str(inst / name))
    shutil.copy(os.path.join(REPO, "ALPACA-MANIFEST"), str(inst / "ALPACA-MANIFEST"))
    py = inst / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text('#!/bin/sh\nexec "%s" "$@"\n' % sys.executable, encoding="utf-8")
    py.chmod(0o755)
    return inst


def test_a_relative_drop_is_read_from_the_caller_folder(tmp_path):
    """L3: bin/alpaca-python runs Python in the install root; --drop d means <caller>/d."""
    inst = _install_copy(tmp_path)
    write_data(str(inst))
    configure(str(inst), enabled=True, slug="rel")
    caller = tmp_path / "caller"
    (caller / "d").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ALPACA_", "CLAUDE_"))}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    run = subprocess.run([str(inst / "bin" / "alpaca"), "hub", "publish", "--drop", "d"],
                         cwd=str(caller), env=env, capture_output=True, text=True, timeout=120)
    assert run.returncode == cli.PASS, run.stdout + run.stderr
    assert os.listdir(caller / "d") == ["%s--rel.json" % ME]
    assert not (inst / "d").exists()


def test_session_end_publishes_from_a_refreshed_projection(project, tmp_path):
    """L2: the tile at session end comes from data.json rendered after the session closed, not
    from the last Stop hook's copy. The stale copy here names an op the record does not have."""
    cli.main(["init"])
    write_data(project)                           # stale: op-1 "Ship the page", one live session
    drop = tmp_path / "drop"
    drop.mkdir()
    configure(project, enabled=True, drop=str(drop), slug="ended")
    run = subprocess.run([sys.executable, "-m", "alpaca.hooks.session_end"],
                         input=json.dumps({"session_id": "s1", "reason": "exit"}), capture_output=True,
                         text=True, encoding="utf-8", cwd=project,
                         env={**os.environ, "CLAUDE_PROJECT_DIR": project, "PYTHONPATH": REPO})
    assert run.returncode == 0 and run.stdout == "", run.stderr
    tile = json.load(open(drop / ("%s--ended.json" % ME), encoding="ascii"))
    assert tile["status"]["headline"] == "No open operation."
    assert tile["status"]["live"] == 0
    assert json.load(open(os.path.join(project, "data.json"), encoding="utf-8")) != DATA


def test_session_end_does_not_render_when_publishing_is_off(project, monkeypatch):
    cli.main(["init"])
    write_data(project)
    configure(project, enabled=False)
    from alpaca import export
    from alpaca.hooks import session_end
    calls = []
    monkeypatch.setattr(export, "write_if_changed", lambda root: calls.append(root))
    session_end.handle({"session_id": "s9", "reason": "exit", "cwd": project})
    assert calls == []
