"""t-070 round 2, class C-A: every Python child an Alpaca hook, verb or collector starts is confined
like the hook that starts it.

The hooks run `bin/alpaca-python --isolated`, which is `python -I`: the working folder and
PYTHONPATH stay off `sys.path`, so a module an agent plants in the project never runs in an engine
process (bin/alpaca-python:5-9). A child started as `[sys.executable, "-m", ...]` with `cwd=root` puts
the root first on its path and undoes that (check round 1, B-1:
.alpaca/proofs/op-008/t-070.logs/attacks/b01_planted_module_in_capture_child.out). The rule
(.alpaca/rune2-development/execution/t-070/fix-plan-r2.md section 2): a Python child starts with
`-I` through the one launcher `alpaca.child.python_argv`, which loads the parent's package by its
location (`alpaca/boot.py`, t-428), its import path is the parent's without the package root, and it
gets no PYTHONPATH.

- `test_every_python_child_starts_through_the_launcher` is the static pin: no module builds an argv
  that starts with `sys.executable` outside the launcher, except the exemptions named with their
  reason.
- `test_a_planted_module_runs_in_no_process_a_hook_starts` is the b01 attack against the real hooks
  and the real `.pth`.
"""
import ast
import json
import os
import re
import shutil
import site
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]          # the tree that holds this `alpaca` package
PACKAGE = REPO / "alpaca"

#: modules that build a `sys.executable` argv on purpose, each with its reason
EXEMPT = {
    "alpaca/child.py": "the launcher itself",
    "alpaca/research/": "the research engine has its own isolation design and gate "
                        "(RUN-ISOLATION, design/research-engine/in-session-runtime.md section 12.5)",
    "alpaca/gates/regression_suite.py": "runs pytest over a tree by design",
    "alpaca/release/": "release tooling builds and checks a clean copy",
    "alpaca/new.py": "the scaffold verb runs the new copy's own package from the copy's root",
    "alpaca/gates/monotonicity.py": "selftest control commands that only print and import nothing",
    "alpaca/runbook_kit.py": "runs the built partner kit's checker as a partner runs it "
                             "(-E, the kit folder first, no alpaca on the path)",
}


def _is_sys_executable(node):
    return (isinstance(node, ast.Attribute) and node.attr == "executable"
            and isinstance(node.value, ast.Name) and node.value.id == "sys")


def _python_child_sites(path):
    """(line, what) for every argv in the module that starts with `sys.executable`: a list or tuple
    display whose first item is it, or an os.exec*/os.spawn* call whose program is it."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    sites = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts and _is_sys_executable(node.elts[0]):
            sites.append((node.lineno, "argv display starting with sys.executable"))
        elif isinstance(node, ast.Call) and node.args and _is_sys_executable(node.args[0]):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name.startswith(("exec", "spawn", "posix_spawn", "Popen", "run", "call", "check_")):
                sites.append((node.lineno, "%s(sys.executable, ...)" % name))
    return sites


def test_every_python_child_starts_through_the_launcher():
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        if rel.startswith("alpaca/tests/") or any(rel == e or (e.endswith("/") and rel.startswith(e))
                                                  for e in EXEMPT):
            continue
        found += ["%s:%d %s" % (rel, line, what) for line, what in _python_child_sites(path)]
    assert not found, ("start these children through alpaca.child.python_argv (or add an exemption "
                       "with its reason):\n" + "\n".join(found))


# ---------------------------------------------------------------------------- generated commands
# t-070 round 3 (check round 2 F-1): the units, scripts and hooks Alpaca writes start Python by the same
# rule. A generated command line that starts Python has `-I` right after the program (the wrapper
# `bin/alpaca-python` passes it through on every branch), and a unit that starts Python unsets
# PYTHONPATH for the children its service starts.

#: modules that write a command line starting Python; `test_every_generated_command_starts_python_isolated`
#: runs each one's generator
GENERATORS = {
    "alpaca/serve.py": "service_units: the dashboard and collector units",
    "alpaca/observability/maintenance.py": "write_services: the backup unit, the tunnel units and the "
                                            "@reboot web-up script",
    "alpaca/barrier.py": "hook_body: the pre-push hook",
}
#: command text that is never started by Alpaca, by module, with a piece of the text and the reason
COMMAND_TEXT = {
    "alpaca/child.py": ("python -m alpaca.child", "its usage message"),
    "alpaca/boot.py": ("usage: python -I <tree>/alpaca/boot.py", "its usage message"),
    "alpaca/import_probe.py": ("python -m alpaca.import_probe", "its usage message"),
    "alpaca/disk.py": ("python -m alpaca.disk", "the argparse program name"),
    "alpaca/spec_kits.py": ("python -m alpaca.spec_kits", "its usage message"),
    "alpaca/research/proofs.py": ("python3 -m alpaca.research.proofs", "a replay command a report "
                                  "prints for a person"),
    "alpaca/research/report.py": ("python3 -m alpaca.research.proofs", "a replay command a report "
                                  "prints for a person"),
    "alpaca/runbook_kit.py": ("check_runbook.py", "the partner kit's own text and usage lines"),
    "alpaca/release/workflows.py": ("bin/alpaca-python -m alpaca.release.ci", "a CI step in a fresh "
                                    "checkout of the repository: the code it runs is the checkout"),
}
_PYTHON_COMMAND = re.compile(r"alpaca-python|ExecStart=|(^|[\s\"'/])python3?(\.\d+)?\s+(-[A-Za-z]+\s+)*"
                             r"(-m\s+alpaca|\S+\.py\b)")


def _strings(path):
    """(line, text) of every string constant of a module that is not a docstring."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    return [(n.lineno, n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]


def test_every_module_that_writes_a_python_command_is_checked():
    """Static: a string in product code that holds a command line starting Python (`bin/alpaca-python`,
    `python ... -m alpaca`, `python ... <file>.py`, a unit's `ExecStart=`) is in a module whose
    generator the next test runs, or is command text Alpaca never starts, named with its reason."""
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        if rel.startswith("alpaca/tests/") or rel in GENERATORS:
            continue
        for line, text in _strings(path):
            if not _PYTHON_COMMAND.search(text):
                continue
            piece, _reason = COMMAND_TEXT.get(rel, (None, None))
            if piece is None or piece not in text:
                found.append("%s:%d %r" % (rel, line, text[:120]))
    assert not found, ("a command line that starts Python, outside the checked generators (add the "
                       "generator to GENERATORS and its output to the next test, or name the text in "
                       "COMMAND_TEXT with its reason):\n" + "\n".join(found))


def python_starts(text):
    """(line, argv) of every line of a unit or a script that starts Python: the words from the
    program on (`bin/alpaca-python`, `python`, `python3`, a venv's `bin/python`), when the program is
    in command position (an `if [ -x bin/alpaca-python ]` test is not a start)."""
    import shlex
    out = []
    for line in text.splitlines():
        body = line.split("=", 1)[1] if re.match(r"Exec[A-Za-z]*=", line) else line
        try:
            words = shlex.split(body, comments=True)
        except ValueError:
            continue
        for i, word in enumerate(words):
            base = os.path.basename(word)
            # a word in command position: first, or after a word that starts a command
            placed = i == 0 or words[i - 1] in _COMMAND_START or words[i - 1].endswith(";")
            if placed and (base == "alpaca-python" or re.fullmatch(r"python3?(\.\d+)?", base)):
                out.append((line, words[i:]))
                break
    return out


_COMMAND_START = frozenset({"exec", "then", "do", "else", "&&", "||", "|", "nohup", "env", "command"})


def generated_commands(root, home):
    """{name: text} of everything the GENERATORS write for project `root`: the instance units, the
    backup unit and timer, the tunnel unit and its watchdog, the @reboot script, the pre-push hook."""
    from alpaca import barrier, serve
    from alpaca.observability import maintenance
    out = {"serve:" + name: body for name, body in serve.service_units(root, port=7391).items()}
    out.update({"serve-python:" + name: body for name, body in
                serve.service_units(root, port=7391, python=sys.executable).items()})
    config = Path(home, ".cloudflared", "config.yml")
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("tunnel: edge\ningress:\n  - hostname: a.example.com\n    service: "
                      "http://127.0.0.1:7391\n  - service: http_status:404\n", encoding="utf-8")
    result = maintenance.write_services(root, port=7391, tunnel="edge",
                                        cloudflared="/usr/local/bin/cloudflared",
                                        tunnel_config=str(config),
                                        watchdog_state=str(Path(home, "wd.state")),
                                        watchdog_script_path=str(Path(home, "wd.sh")))
    folder = Path(result["directory"])
    out.update({"services:" + p.name: p.read_text(encoding="utf-8") for p in sorted(folder.iterdir())})
    out["web-up-public"] = maintenance.web_up_script(root, "alpaca-x", 7391, public=True)
    out["barrier:pre-push"] = barrier.hook_body()
    return out


def test_every_generated_command_starts_python_isolated(project, tmp_path, monkeypatch):
    """Every unit, script and hook the GENERATORS write: each line that starts Python has `-I` right
    after the program, and each unit that starts Python unsets PYTHONPATH. The live collector unit
    started `bin/alpaca-python -m alpaca collect watch` with the install root as its working folder
    (check round 2, F-1: .alpaca/proofs/op-008/t-070.logs/check-r2-adv/probes/q05_*.out)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALPACA_CLOUDFLARED_CONFIG", str(home / ".cloudflared" / "config.yml"))
    monkeypatch.setenv("ALPACA_WORKSPACES", str(tmp_path / "registry" / "workspaces.json"))
    commands = generated_commands(project, home)
    for kind in ("serve:", "serve-python:", "services:", "web-up-public", "barrier:pre-push"):
        assert any(k.startswith(kind) for k in commands), (kind, sorted(commands))
    bad, seen = [], 0
    for name, text in sorted(commands.items()):
        starts = python_starts(text)
        seen += len(starts)
        for line, argv in starts:
            if argv[1:2] != ["-I"]:
                bad.append("%s: %s" % (name, line.strip()))
        if starts and name.endswith(".service") and "\nUnsetEnvironment=PYTHONPATH\n" not in text:
            bad.append("%s: starts Python and keeps PYTHONPATH for its children" % name)
    # the dashboard, collector (twice), backup, web-up (two scripts, one start each) and the hook (two)
    assert seen >= 9, (seen, sorted(commands))
    assert not bad, "generated commands that start Python without the launcher rule:\n" + "\n".join(bad)


def test_a_generated_unit_start_runs_no_planted_module(project, tmp_path):
    """The collector unit's own start, run as systemd would in a folder standing for the install
    root that holds a planted module, with PYTHONPATH naming it too: the module named after `-m` is
    the planted one, so it runs only when the working folder or PYTHONPATH reaches the path."""
    import shlex
    from alpaca import serve
    units = serve.service_units(project, port=7391, python=sys.executable)
    collector = next(body for name, body in units.items() if name.endswith("-collector.service"))
    argv = shlex.split(next(line.split("=", 1)[1] for line in collector.splitlines()
                            if line.startswith("ExecStart=")))
    at = argv.index("-m")
    install = tmp_path / "install-root"
    install.mkdir()
    marker = tmp_path / "PLANTED-RAN.txt"
    (install / "planted_probe_r3.py").write_text("open(%r, 'a').write('planted ran\\n')\n" % str(marker))
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env["PYTHONPATH"] = str(install)
    proc = subprocess.run(argv[:at + 1] + ["planted_probe_r3"], cwd=str(install), env=env,
                          capture_output=True, text=True, timeout=60)
    assert not marker.exists(), (argv[:at + 1], proc.stderr[-400:])
    assert proc.returncode != 0 and "No module named planted_probe_r3" in proc.stderr, proc.stderr[-400:]


# ---------------------------------------------------------------------------- the real hooks
def _pth_root():
    """The root `alpaca-root.pth` names in this interpreter's site-packages, or None."""
    for folder in site.getsitepackages():
        pth = Path(folder, "alpaca-root.pth")
        if pth.is_file():
            return (pth.read_text(encoding="utf-8").splitlines() or [""])[0].strip() or None
    return None


def hook_argv(hook):
    """The hook command as `.claude/settings.json` runs it: `bin/alpaca-python --isolated -m
    alpaca.hooks.<hook>`, which with a venv is `python -I alpaca/boot.py -m alpaca.hooks.<hook>`, in a
    linked worktree too (t-428). A tree with no `.venv` (a sparse task worktree) runs the wrapper's
    venv start by hand with this interpreter."""
    if (REPO / ".venv" / "bin" / "python").is_file() and (REPO / "bin" / "alpaca-python").is_file():
        return [str(REPO / "bin" / "alpaca-python"), "--isolated", "-m", "alpaca.hooks." + hook]
    return [sys.executable, "-I", str(PACKAGE / "boot.py"), "-m", "alpaca.hooks." + hook]


PLANTED = r'''
import importlib, os, sys
with open(%(marker)r, "a") as fh:
    fh.write("%%s %%d %%s\n" %% (%(name)r, os.getpid(), " ".join(sys.argv)))
_here = os.path.dirname(os.path.abspath(__file__))
_mine = sys.modules.pop(__name__, None)
_saved = list(sys.path)
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _here]
try:
    _real = importlib.import_module(__name__)
finally:
    sys.path[:] = _saved
sys.modules[__name__] = _real
globals().update({k: v for k, v in _real.__dict__.items() if not k.startswith("__")})
'''


def _absent_optional_imports():
    """Top-level names alpaca imports under `except ImportError` that this interpreter lacks: a
    planted module of that name would otherwise be the one found."""
    names = set()
    for path in PACKAGE.rglob("*.py"):
        if "/tests/" in path.as_posix():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            caught = {getattr(h.type, "id", None) for h in node.handlers} | {
                getattr(e, "id", None) for h in node.handlers if isinstance(h.type, ast.Tuple)
                for e in h.type.elts}
            if not caught & {"ImportError", "ModuleNotFoundError", "Exception"}:
                continue
            for stmt in node.body:
                if isinstance(stmt, ast.Import):
                    names |= {a.name.split(".")[0] for a in stmt.names}
                elif isinstance(stmt, ast.ImportFrom) and stmt.module and not stmt.level:
                    names.add(stmt.module.split(".")[0])
    import importlib.util
    return sorted(n for n in names if n != "alpaca" and importlib.util.find_spec(n) is None)


def _plant(folder, marker):
    folder.mkdir(parents=True, exist_ok=True)
    names = ["json", "sitecustomize", "usercustomize"] + _absent_optional_imports()
    for name in names:
        (folder / (name + ".py")).write_text(PLANTED % {"marker": str(marker), "name": name})
    # a package named `alpaca`: a child whose path puts this folder before the package root runs it
    (folder / "alpaca").mkdir(exist_ok=True)
    (folder / "alpaca" / "__init__.py").write_text(
        "open(%r, 'a').write('alpaca-package %%d\\n' %% __import__('os').getpid())\n" % str(marker))
    return names


def _hook_env(root):
    env = {k: v for k, v in os.environ.items()
           if k not in ("ALPACA_ROOT", "ALPACA_SESSION_ID", "PYTHONPATH", "SQLITE_TMPDIR",
                        "ALPACA_DISK_SWEEP", "CLAUDE_CODE_SESSION_ID")}
    env.update(CLAUDE_PROJECT_DIR=root, ALPACA_NO_AUTOSERVE="1", PYTHONDONTWRITEBYTECODE="1")
    return env


def _wait_for_sweep(root, since, timeout=60.0):
    stamp = Path(root, ".alpaca", "config", "disk-sweep.stamp")
    lock = Path(root, ".alpaca", "config", "disk-sweep.lock")
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if stamp.exists() and stamp.stat().st_mtime >= since - 1:
            import fcntl
            with open(lock, "a") as fh:                 # the sweep holds it until it is done
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return True
                except OSError:
                    pass
        time.sleep(0.2)
    return False


def test_a_planted_module_runs_in_no_process_a_hook_starts(project, tmp_path):
    root = project
    marker = tmp_path / "PLANTED-RAN.txt"
    work = tmp_path / "work"                           # the folder the hook command is run from
    planted = _plant(Path(root), marker)
    _plant(work, marker)
    scratch = tmp_path / "scratch-base"
    scratch.mkdir()
    # a disk policy whose only effect is a due sweep over an empty scratch base
    Path(root, "project.yaml").write_text(
        "disk:\n  scratch_base: %s\n  sweep_every_hours: 1\n" % json.dumps(str(scratch)))
    from alpaca import db
    conn = db.connect(root)
    for i in range(5):
        db.append_event(conn, session="s-plant", actor="agent", kind="result",
                        data={"body": "planted-module test note %d" % i})
    conn.close()
    env = _hook_env(root)
    runs = {}
    sweeps = []
    for hook, extra in (("stop", {}), ("session_end", {"reason": "other"}),
                        ("pre_compact", {"trigger": "manual"}), ("session_start", {"source": "resume"})):
        if hook == "session_start":                    # make the sweep due again
            Path(root, ".alpaca", "config", "disk-sweep.stamp").unlink(missing_ok=True)
        payload = dict({"session_id": "s-plant", "cwd": root, "transcript_path": "",
                        "hook_event_name": hook}, **extra)
        started = time.time()
        p = subprocess.run(hook_argv(hook), input=json.dumps(payload), text=True, capture_output=True,
                           cwd=str(work), env=env, timeout=180)
        runs[hook] = (p.returncode, p.stderr[-600:])
        if hook in ("session_end", "session_start"):
            sweeps.append((hook, _wait_for_sweep(root, started)))
    lines = marker.read_text().splitlines() if marker.exists() else []
    assert not lines, "planted modules %s ran:\n%s" % (planted, "\n".join(lines))
    assert all(code == 0 for code, _ in runs.values()), runs
    assert sweeps == [("session_end", True), ("session_start", True)], (sweeps, runs)
    conn = sqlite3.connect(os.path.join(root, ".alpaca", "alpaca.db"))
    try:
        kinds = [r[0] for r in conn.execute("SELECT kind FROM events WHERE session='s-plant'")]
        failed = [r[0] for r in conn.execute("SELECT data FROM events WHERE kind='drain-failed'")]
    finally:
        conn.close()
    # every capture child ran alpaca from this tree and landed the notes (a child that imported
    # another tree's alpaca reports it, and the hook records drain-failed)
    assert not failed, failed
    assert "session-end" in kinds and "turn-end" in kinds, kinds
    store = sqlite3.connect("file:%s?mode=ro" % os.path.join(root, ".alpaca", "wiki", "rune.db"), uri=True)
    try:
        notes = store.execute("SELECT COUNT(*) FROM blocks WHERE text LIKE '%planted-module test note%'").fetchone()[0]
    finally:
        store.close()
    assert notes >= 5, notes


def test_the_serve_code_probe_ignores_a_planted_module(project, tmp_path):
    """The live server checks new code imports in a child before it re-executes itself
    (serve.code_importable); that child must not run a module planted in the root either."""
    marker = tmp_path / "PLANTED-RAN.txt"
    _plant(Path(project), marker)
    from alpaca import serve
    ok, why = serve.code_importable(project)
    lines = marker.read_text().splitlines() if marker.exists() else []
    assert not lines, "planted modules ran in the probe:\n" + "\n".join(lines)
    assert ok, why


@pytest.mark.parametrize("broken", [None, "syntax error", "missing module"])
def test_the_serve_code_probe_fails_on_broken_code(tmp_path, monkeypatch, broken):
    """The reload guard's failure path (review of 97b59a438, m3): when the code on disk does not
    import, serve.code_importable returns False with the error, so the server keeps running the old
    code instead of re-executing into a broken one. The package the probe child imports is the one
    at `child.package_root()`; here that is a copy holding the real `alpaca.import_probe` and five
    stub server modules, one of them broken. The control (no broken module) returns True, so the
    False comes from the broken module and not from the stand-in tree."""
    from alpaca import child, serve
    fake = tmp_path / "fake-root"
    (fake / "alpaca").mkdir(parents=True)
    (fake / "alpaca" / "__init__.py").write_text("")
    shutil.copy(PACKAGE / "import_probe.py", fake / "alpaca" / "import_probe.py")
    shutil.copy(PACKAGE / "boot.py", fake / "alpaca" / "boot.py")
    for name in ("serve", "hub", "work_record", "export", "proof"):
        (fake / "alpaca" / (name + ".py")).write_text("X = 1\n")
    if broken == "syntax error":
        (fake / "alpaca" / "hub.py").write_text("def half_written(:\n")
    elif broken == "missing module":
        (fake / "alpaca" / "proof.py").unlink()
    monkeypatch.setattr(child, "package_root", lambda: str(fake))
    served = tmp_path / "served"
    served.mkdir()
    ok, why = serve.code_importable(str(served))
    if broken is None:
        assert ok, why
    else:
        assert not ok, why
        assert ("SyntaxError" if broken == "syntax error" else "ModuleNotFoundError") in why, why


def test_the_import_probe_exit_codes(tmp_path):
    """`python -m alpaca.import_probe`: 0 when every module imports, 1 (the traceback) when one does
    not, and 2 with its usage message when no module is named. The no-name exit is decided as 2,
    not the 0 the old `alpaca.child import` gave (review of 97b59a438, m3): a probe that imported
    nothing has checked nothing, so a caller that lost its module list must not read it as a pass.
    The one caller (serve.code_importable) always names five modules."""
    from alpaca import child

    def probe(*names):
        return subprocess.run(child.python_argv("alpaca.import_probe", *names), cwd=str(tmp_path),
                              env=child.child_env(), capture_output=True, text=True, timeout=60)

    ok = probe("alpaca.proof")
    assert ok.returncode == 0, ok.stderr
    missing = probe("alpaca.proof", "alpaca.no_such_module_for_the_probe_test")
    assert missing.returncode == 1 and "ModuleNotFoundError" in missing.stderr, missing.stderr
    none = probe()
    assert none.returncode == 2, (none.returncode, none.stderr)
    assert "usage: python -m alpaca.import_probe" in none.stderr, none.stderr


def test_the_launcher_gives_the_parents_package_and_no_working_folder(tmp_path):
    from alpaca import child
    work = tmp_path / "work"
    marker = tmp_path / "PLANTED-RAN.txt"
    _plant(work, marker)
    served = tmp_path / "served"
    (served / ".alpaca").mkdir(parents=True)
    shutil.copy(REPO / "ALPACA-MANIFEST", served / "ALPACA-MANIFEST")
    env = child.child_env(served_root=str(served), base=dict(os.environ, PYTHONPATH=str(work)))
    assert "PYTHONPATH" not in env
    p = subprocess.run(child.python_argv("alpaca.child", "describe", "--x", "1"), cwd=str(work), env=env,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    got = json.loads(p.stdout)
    assert not marker.exists(), marker.read_text()
    assert got["package"] == str(REPO), got
    assert got["isolated"] == 1 and got["argv"][1:] == ["describe", "--x", "1"], got
    path = [os.path.realpath(x or ".") for x in got["path"]]
    assert os.path.realpath(str(work)) not in path and "" not in got["path"], got
    assert os.path.realpath(str(REPO)) not in path, got            # loaded by its location (t-428)
    pth = _pth_root()
    if pth and pth != str(REPO):                       # a worktree: the main checkout is not on it
        assert os.path.realpath(pth) not in path, got
    assert got["sqlite_tmpdir"] == os.path.join(str(served), ".alpaca", "tmp", "sqlite"), got


def _stray_alpaca(kind, other, marker):
    """Put an `alpaca` of the given shape in `other` (a folder, or a zip file for "zip") whose code,
    when it runs, appends a line to `marker`. Returns the path entry, or None when the shape cannot
    be built here (an extension needs a C compiler)."""
    import importlib.machinery
    import py_compile
    import zipfile
    code = "open(%r, 'a').write('ran\\n')\n" % str(marker)
    src = other.parent / ("stray-%s-src.py" % kind)
    if kind == "zip":
        with zipfile.ZipFile(other, "w") as z:
            z.writestr("alpaca/__init__.py", code)
            z.writestr("alpaca/child.py", code)
        return other
    other.mkdir(parents=True)
    if kind == "source package":
        (other / "alpaca").mkdir()
        (other / "alpaca" / "__init__.py").write_text(code)
        (other / "alpaca" / "child.py").write_text(code)
    elif kind == "source module":
        (other / "alpaca.py").write_text(code)
    elif kind == "sourceless package":                  # alpaca/__init__.pyc and no source
        (other / "alpaca").mkdir()
        src.write_text(code)
        py_compile.compile(str(src), cfile=str(other / "alpaca" / "__init__.pyc"), doraise=True)
        (other / "alpaca" / "child.py").write_text(code)
    elif kind == "sourceless module":                   # alpaca.pyc and no source
        src.write_text(code)
        py_compile.compile(str(src), cfile=str(other / "alpaca.pyc"), doraise=True)
    elif kind == "extension":                           # alpaca<EXT_SUFFIX>: init writes the marker
        cc = shutil.which("cc") or shutil.which("gcc")
        if not cc:
            return None
        c_src = other.parent / "stray-alpaca.c"
        c_src.write_text('#include <stdio.h>\n'
                         'void *PyInit_alpaca(void) { FILE *f = fopen("%s", "a");'
                         ' if (f) { fputs("ran\\n", f); fclose(f); } return 0; }\n' % marker)
        so = other / ("alpaca" + importlib.machinery.EXTENSION_SUFFIXES[0])
        subprocess.run([cc, "-shared", "-fPIC", "-o", str(so), str(c_src)], check=True,
                       capture_output=True, timeout=60)
    elif kind == "namespace":                           # alpaca/ with no __init__: a portion only
        (other / "alpaca").mkdir()
        (other / "alpaca" / "child.py").write_text(code)
    return other


@pytest.mark.parametrize("kind", ["source package", "source module", "sourceless package",
                                  "sourceless module", "zip", "extension", "namespace"])
def test_a_stray_alpaca_on_the_parents_path_never_redirects_the_child(tmp_path, monkeypatch, kind):
    """The child imports the `alpaca` the parent imported, whatever else the parent's path holds.
    A path entry ahead of it from which the import system would load another `alpaca` (a copied
    tree some code left on `sys.path`, as the in-process wiki mutation oracle did with the m4 fresh
    copy: landing full suite, .alpaca/proofs/op-008/t-070.logs/land/red-m4-then-new37.log) can
    neither shadow the package nor run its code, in every shape the path finder loads: a source or
    sourceless package or module, a zip archive, a compiled extension (review of 97b59a438, m1,
    probe r01 cases B to D) and a namespace folder. Since t-452 the entry stays on the child's path,
    so any dependency it holds is kept: the child's `alpaca` is loaded by its location
    (`alpaca/boot.py`) before anything imports it, so no path entry is searched for it."""
    from alpaca import child
    other = tmp_path / ("other.zip" if kind == "zip" else "other")
    marker = tmp_path / "STRAY-ALPACA-RAN.txt"
    entry = _stray_alpaca(kind, other, marker)
    if entry is None:
        pytest.skip("no C compiler to build an extension module named alpaca")
    monkeypatch.setattr(sys, "path", [str(entry)] + list(sys.path))
    argv = child.python_argv("alpaca.child", "describe")
    assert argv[3] == "--path", argv
    sent = [os.path.realpath(x) for x in json.loads(argv[4])]
    assert os.path.realpath(str(entry)) in sent, sent                 # kept (t-452)
    assert os.path.realpath(str(REPO)) not in sent, sent              # loaded by its location (t-428)
    p = subprocess.run(argv, cwd=str(tmp_path), env=child.child_env(), capture_output=True, text=True,
                       timeout=60)
    assert not marker.exists(), marker.read_text()
    assert p.returncode == 0, p.stderr
    got = json.loads(p.stdout)
    assert got["package"] == str(REPO), got
