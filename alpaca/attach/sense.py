"""Attach gates from the files of the attached repository (build plan P10).

Sensing reads bytes only and runs nothing. `adopt.sense` already knows a root Makefile, package.json,
pyproject.toml, pytest.ini and setup.cfg; each command it finds is a shell string that traces to a file,
and a gate is an argv list, so the string is split with `shlex.split` (it is never handed to a shell).
CMake is added here: a root CMakeLists.txt gives the configure step as a gate, because a firmware
project such as QTRM64 senses nothing else.

Traces are prefixed with the repository folder (`app/Makefile`), so a pointer resolves from the studio
root and a file named like one the Alpaca install ships (`pytest.ini`) is not mistaken for it.
"""
from __future__ import annotations

import os
import shlex

from alpaca import adopt
from alpaca.attach import AttachError

#: sensed command kinds that become gates, in gate order; "run" starts the product and is not a check
GATE_KINDS = ("build", "test", "lint")
CMAKE_CONFIGURE = ["cmake", "-S", ".", "-B", "build/alpaca-gate"]


#: the root files sensing reads (adopt.sense: Makefile, pyproject.toml, pytest.ini, setup.cfg, package.json; here: CMakeLists.txt)
SENSED_FILES = ("Makefile", "pyproject.toml", "pytest.ini", "setup.cfg", "package.json", "CMakeLists.txt")


def _link_problem(repo_dir, repo_name):
    """The first sensed file that is a link resolving outside the repository, as a refusal text, or None.
    A link inside the repository is read as the file it points to."""
    real = os.path.realpath(str(repo_dir))
    for name in SENSED_FILES:
        path = os.path.join(str(repo_dir), name)
        if os.path.islink(path):
            target = os.path.realpath(path)
            if target != real and not target.startswith(real + os.sep):
                return ("%s/%s is a link that points outside the repository; sensing does not read it "
                        "(pass --gates-file with the gates to use)" % (repo_name, name))
    return None


def sense_gates(repo_dir, repo_name):
    """(gates, traces) of the repository at `repo_dir`. `gates` is a list of
    {"name", "command": argv, "timeout"} dicts; `traces` maps each gate name to the file it was read
    from, as `<repo_name>/<file>`. Both are empty when no file implies a gate (never a guess)."""
    problem = _link_problem(repo_dir, repo_name)
    if problem:
        raise AttachError(problem)
    facts = adopt.sense(repo_dir)
    commands = facts.get("commands") or {}
    gates, traces = [], {}
    for kind in GATE_KINDS:
        entry = commands.get(kind)
        if not entry:
            continue
        try:
            argv = shlex.split(entry["value"])
        except ValueError:
            continue
        if not argv:
            continue
        gates.append({"name": kind, "command": argv, "timeout": 600})
        traces[kind] = "%s/%s" % (repo_name, entry["evidence"])
    if os.path.isfile(os.path.join(str(repo_dir), "CMakeLists.txt")):
        gates.append({"name": "cmake-configure", "command": list(CMAKE_CONFIGURE), "timeout": 600})
        traces["cmake-configure"] = "%s/CMakeLists.txt" % repo_name
    return gates, traces
