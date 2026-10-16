"""`alpaca attach` is registered, declared and documented (the surface contract of alpaca/surface.py)."""
import os

from alpaca import cli, surface
from alpaca.attach import cli as attach_cli
from alpaca.tests.attachkit import attached, studio, upstream  # noqa: F401
from alpaca.tests.conftest import REPO

# `propose` of the first plan is gone: the first chat looks at the repo with `alpaca onboard look` (decision
# dec-13714a8e49e2, onboarding-design.md), so no `attach propose` verb is built
SUBVERBS = ("init", "status", "baseline", "check", "study", "stage", "land", "approve", "handoff", "merge", "sync")


def test_attach_is_registered_declared_and_documented():
    assert "attach" in cli.registered_verbs()
    assert surface.table()["attach"] == {"keys": ("verdict", "task", "receipt"), "group": "setup"}
    assert surface.lint(REPO) == []


def test_the_manual_names_every_sub_verb_of_the_group():
    manual = open(os.path.join(REPO, "MANUAL.md"), encoding="utf-8").read()
    line = [x for x in manual.splitlines() if x.startswith("- `alpaca attach`")]
    assert len(line) == 1
    for verb in SUBVERBS:
        assert "`%s`" % verb in line[0], verb


def test_modules_tuple_is_complete_from_the_first_task():
    assert attach_cli.MODULES == ("repo", "health", "baseline", "check", "study", "runbooks", "approval", "handoff", "sync",
                                  "onboarding")


def test_a_module_that_is_not_built_yet_is_skipped_quietly(capsys):
    assert attach_cli._module("zz_not_built_yet") is None
    assert capsys.readouterr().err == ""


def test_a_module_that_breaks_is_reported_and_skipped(monkeypatch, capsys):
    import importlib

    def broken(name):
        raise ImportError("inner problem", name="not_this_module")
    monkeypatch.setattr(importlib, "import_module", broken)
    assert attach_cli._module("repo") is None
    assert "attach module repo unavailable" in capsys.readouterr().err


def test_the_group_lists_its_built_sub_verbs_and_a_bare_call_is_usage(project, capsys):
    assert cli.main(["attach"]) == cli.USAGE
    err = capsys.readouterr().err
    assert "init" in err and "status" in err


def test_status_in_a_project_without_attach_is_blocked_not_an_error(project, capsys):
    cli.main(["init"])
    assert cli.main(["attach", "status"]) == cli.BLOCKED
    assert "GATE alpaca-attach-status: BLOCKED (this project is not attached" in capsys.readouterr().out


def test_status_json_has_the_surface_keys(attached, capsys):
    import json
    assert cli.main(["attach", "status", "--json"]) == cli.PASS
    out = json.loads(capsys.readouterr().out)
    assert set(out) >= {"verdict", "task", "receipt"} and out["receipt"]["gates"] == ["build", "test"]
