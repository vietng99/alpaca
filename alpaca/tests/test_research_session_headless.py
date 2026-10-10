"""Fail case 1 of the in-session runtime: a real run that starts a headless agent process
(design/research-engine/in-session-runtime.md sections 11 and 16).

The launcher stays as the test and control runtime. `launcher.refuse_headless` sits at the top of
`launcher.launch` and refuses a `claude` or `codex` spec unless a test switch is on and the project
root is under a temp prefix, and always for a run that recorded `runtime_mode: session`. Removing
the guard makes the first three tests fail; turning a default launch site back to `launcher.launch`
makes the static test fail.
"""
from __future__ import annotations

import ast
import os

import pytest

from alpaca import db
from alpaca.gates import workspace_guard
from alpaca.research import events, launcher, session_runtime
from alpaca.research import paths as research_paths
from alpaca.tests import research_fakes as fakes
from alpaca.tests.conftest import REPO
from alpaca.tests.test_research_launcher import lab  # noqa: F401 - the fixture of the launcher suite

RESEARCH = os.path.join(REPO, "alpaca", "research")


def _marker_runtimes(lab, tmp_path, monkeypatch):
    """Fake `claude` and `codex` on PATH that write a marker file when they run."""
    marker = tmp_path / "RAN"
    bin_dir = tmp_path / "markerbin"
    bin_dir.mkdir()
    for name in ("claude", "codex"):
        script = bin_dir / name
        script.write_text("#!/bin/sh\necho %s >> %s\n" % (name, marker))
        script.chmod(0o755)
    monkeypatch.setenv("PATH", "%s:%s" % (bin_dir, os.environ["PATH"]))
    return marker


def _refusals(root):
    log = os.path.join(research_paths.research_dir(root), "headless-refusals.log")
    try:
        with open(log, encoding="utf-8") as fh:
            return fh.read().splitlines()
    except OSError:
        return []


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_headless_refused_without_switch(lab, tmp_path, monkeypatch, runtime):
    monkeypatch.delenv(launcher.TEST_SWITCH_ENV, raising=False)
    marker = _marker_runtimes(lab, tmp_path, monkeypatch)
    with pytest.raises(launcher.HeadlessRefused) as exc:
        launcher.launch(lab.implementer(runtime=runtime), conn=lab.conn, root=lab.root)
    assert "RUN-HEADLESS-REFUSED" in str(exc.value) and "switch is off" in str(exc.value)
    assert not marker.exists()
    assert len(_refusals(lab.root)) == 1
    # no launch row, no home and no process: the guard sits before everything else
    assert lab.events(events.LAUNCH) == []


def test_headless_refused_for_a_real_root(lab, tmp_path, monkeypatch):
    # the switch is on (the lab fixture sets it), but the root is made to look like a real project
    marker = _marker_runtimes(lab, tmp_path, monkeypatch)
    monkeypatch.setattr(workspace_guard, "default_tmp_prefixes", lambda: ())
    with pytest.raises(launcher.HeadlessRefused) as exc:
        launcher.launch(lab.implementer(), conn=lab.conn, root=lab.root)
    assert "not under a temp prefix" in str(exc.value)
    assert not marker.exists()


def test_headless_refused_for_a_session_run(lab, tmp_path, monkeypatch):
    # the switch is on and the root is a temp root: a run that says session is still refused
    marker = _marker_runtimes(lab, tmp_path, monkeypatch)
    events.emit(lab.conn, events.RUN_START, run=fakes.RUN, ref=fakes.RUN, op=None, session="s",
                data={"runtime_mode": "session", "mode": "improve"})
    with pytest.raises(launcher.HeadlessRefused) as exc:
        launcher.launch(lab.implementer(), conn=lab.conn, root=lab.root)
    assert "is a session run" in str(exc.value)
    assert not marker.exists()


def test_a_test_run_with_the_switch_on_a_temp_root_is_not_refused(lab):
    # the positive case: the guard does not block the test runtime, so the three tests above fail
    # for the guard's reason alone
    launcher.refuse_headless(lab.implementer(), lab.conn, lab.root)


def test_control_spec_routes_to_launcher_and_is_not_refused(lab, monkeypatch):
    # a control spec is a script, not an agent: with the switch off it is not refused, and the
    # session runtime hands it to the launcher
    monkeypatch.delenv(launcher.TEST_SWITCH_ENV, raising=False)
    events.emit(lab.conn, events.RUN_START, run=fakes.RUN, ref=fakes.RUN, op=None, session="s",
                data={"runtime_mode": "session", "mode": "improve"})
    spec = lab.implementer(runtime="control")
    launcher.refuse_headless(spec, lab.conn, lab.root)
    seen = []
    monkeypatch.setattr(launcher, "launch", lambda s, **kw: seen.append(s.runtime) or {"ok": 1})
    assert session_runtime.launch(spec, conn=lab.conn, root=lab.root) == {"ok": 1}
    assert seen == ["control"]


def test_a_claude_spec_through_the_session_runtime_never_reaches_the_launcher(lab, monkeypatch):
    def refuse(*a, **kw):                      # pragma: no cover - reached only when the test fails
        raise AssertionError("session_runtime.launch called launcher.launch for a claude spec")
    monkeypatch.setattr(launcher, "launch", refuse)
    issued = []
    monkeypatch.setattr(session_runtime, "_launch", lambda spec, conn, root, ctx: issued.append(
        spec.runtime) or {"outcome": "ok"})
    assert session_runtime.launch(lab.implementer(), conn=lab.conn, root=lab.root) == {"outcome": "ok"}
    assert issued == ["claude"]


# ------------------------------------------------------------------ the static test
def _research_modules():
    for name in sorted(os.listdir(RESEARCH)):
        if name.endswith(".py"):
            yield name, os.path.join(RESEARCH, name)


def _launch_references(tree):
    """The places a module names the launcher's `launch`: `X.launch` where X is the launcher module
    (under the name `launcher` or an alias), `from ...launcher import launch`, and
    getattr(<launcher>, "launch")."""
    aliases = {"launcher"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "alpaca.research":
            aliases |= {a.asname or a.name for a in node.names if a.name == "launcher"}
        elif isinstance(node, ast.Import):
            aliases |= {a.asname or a.name for a in node.names
                        if a.name == "alpaca.research.launcher"}
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "alpaca.research.launcher" and \
                any(a.name == "launch" for a in node.names):
            hits.append((node.lineno, "from launcher import launch"))
        elif isinstance(node, ast.Attribute) and node.attr == "launch" and \
                isinstance(node.value, ast.Name) and node.value.id in aliases:
            hits.append((node.lineno, "%s.launch" % node.value.id))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and \
                node.func.id == "getattr" and len(node.args) >= 2 and \
                isinstance(node.args[0], ast.Name) and node.args[0].id in aliases and \
                isinstance(node.args[1], ast.Constant) and node.args[1].value == "launch":
            hits.append((node.lineno, "getattr(%s, 'launch')" % node.args[0].id))
    return hits


def test_only_session_runtime_names_launcher_launch():
    named = {}
    for name, path in _research_modules():
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        hits = _launch_references(tree)
        if hits:
            named[name] = hits
    # the launcher itself defines it; session_runtime.py is its one caller
    named.pop("launcher.py", None)
    assert set(named) == {"session_runtime.py"}, named
    # the call sits under an `if` on the spec's runtime being control
    with open(os.path.join(RESEARCH, "session_runtime.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    sites = []
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for node in ast.walk(fn):
                if isinstance(node, ast.If) and "control" in ast.dump(node.test):
                    for inner in ast.walk(node):
                        if isinstance(inner, ast.Attribute) and inner.attr == "launch" and \
                                isinstance(inner.value, ast.Name) and inner.value.id == "launcher":
                            sites.append((fn.name, inner.lineno))
    assert sites and len(sites) == len(named["session_runtime.py"]), (sites, named)


def test_the_static_test_sees_a_planted_call():
    planted = ast.parse("from alpaca.research import launcher as L\nL.launch(spec)\n"
                        "from alpaca.research.launcher import launch\n"
                        "getattr(L, 'launch')\n")
    assert sorted(what for _line, what in _launch_references(planted)) == [
        "L.launch", "from launcher import launch", "getattr(L, 'launch')"]
    clean = ast.parse("from alpaca.research import launcher\nlauncher._result(spec)\n")
    assert _launch_references(clean) == []


def test_the_default_launch_sites_are_the_session_runtime():
    for name, needle in (("lifecycle.py", "session_runtime.launch_for"),
                         ("audit.py", "session_runtime.launch"),
                         ("verifier.py", "session_runtime.launch")):
        with open(os.path.join(RESEARCH, name), encoding="utf-8") as fh:
            assert needle in fh.read(), name
