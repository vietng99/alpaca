"""The evidence helper of the live round (tools/live_round.py, in-session-runtime.md section 16).

The live round itself is run by the orchestrator after the merge. This test runs the helper over the
record of the scripted round, so the commands the orchestrator will type are known to work on a
record of the same shape, and shows each machine-checkable item of the confirmer's list fails when
its evidence is bad.
"""
from __future__ import annotations

import importlib.util
import json
import os

import pytest

from alpaca import db
from alpaca.tests.conftest import REPO
from alpaca.tests.devfiles import needs_dev_files
from alpaca.tests.test_research_session_runtime import _new_run, _winner, _why, needs_jail

PATH = os.path.join(REPO, "tools", "live_round.py")
pytestmark = needs_dev_files("tools/live_round.py")


@pytest.fixture(scope="module")
def lr():
    spec = importlib.util.spec_from_file_location("live_round_tool", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_runner_argv_pattern_and_the_headless_pattern(lr):
    good = "/home/u/Harness/Alpaca/.venv/bin/python -I -m alpaca.research.runner --run rr-001 --root /x"
    assert lr.RUNNER_ARGV.search(good)
    assert not lr.RUNNER_ARGV.search("/home/u/.venv/bin/python -m alpaca.research.runner --run rr-001")
    assert not lr.RUNNER_ARGV.search("python3 -I -m alpaca.research.runner")
    assert lr.HEADLESS_ARGV.search("claude --model x -p hello")
    assert lr.HEADLESS_ARGV.search("/usr/bin/codex exec --json")
    assert not lr.HEADLESS_ARGV.search("claude --dangerously-skip-permissions")
    assert not lr.HEADLESS_ARGV.search("python3 -m pytest -p no:cacheprovider")


def test_the_sampler_command_writes_into_the_evidence_folder(lr, capsys, tmp_path):
    assert lr.main(["sampler", "--out", str(tmp_path / "ev")]) == 0
    text = capsys.readouterr().out
    assert "ps -eo pid,args" in text and "ps-samples.txt" in text and "ps-sampler.pid" in text
    assert "# ancestors:" in text


@needs_jail
def test_collect_and_confirm_over_the_scripted_round(lr, project, monkeypatch, tmp_path, capsys):
    root, session = _new_run(project, monkeypatch, behavior=_winner)
    run = session.start()["run"]
    done = session.drive(run, timeout=900)
    assert done["action"] == "report", _why(root, run, done)
    out = str(tmp_path / "live-round")
    os.makedirs(out)
    rc, body = session.verb("status", run)
    with open(os.path.join(out, "status-final.json"), "w", encoding="utf-8") as fh:
        json.dump(body, fh)
    session.verb("report", run)
    assert lr.main(["collect", "--run", run, "--out", out, "--root", root]) == 0
    for name in ("events.jsonl", "jobs.txt", "refusals.txt", "attachments.txt", "text-sources.txt",
                 "context.txt", "guard-denials.txt", "headless-refusals.txt",
                 "transcript-hashes.txt"):
        assert os.path.isfile(os.path.join(out, name)), name
    jobs_txt = open(os.path.join(out, "jobs.txt"), encoding="utf-8").read().splitlines()
    assert jobs_txt[0].startswith("job role model agent_id") and len(jobs_txt) == 1 + 16
    assert all(line.split()[-1] == "session" for line in jobs_txt[1:])
    assert "JOB-TOOL-OFF-LIST 0" in open(os.path.join(out, "refusals.txt"), encoding="utf-8").read()
    assert os.path.isfile(os.path.join(out, "report.md"))
    # the samples of a clean run: the runner under the venv interpreter and -I, and no headless agent
    ps = os.path.join(out, "ps-samples.txt")
    with open(ps, "w", encoding="utf-8") as fh:
        fh.write("# ancestors: 10 20\n# 1\n  10 claude --dangerously-skip-permissions -p x\n"
                 "  500 /home/u/Harness/Alpaca/.venv/bin/python -I -m alpaca.research.runner --run %s\n"
                 "  600 sleep 1\n" % run)
    capsys.readouterr()
    code = lr.main(["confirm", "--run", run, "--out", out, "--root", root])
    text = capsys.readouterr().out
    fails = [line for line in text.splitlines() if line.startswith("FAIL")]
    assert code == 0 and not fails, text
    assert "MANUAL" in text and text.count("PASS") >= 10
    # each machine-checked item fails on bad evidence
    with open(ps, "a", encoding="utf-8") as fh:
        fh.write("  700 claude --model x -p do-this\n")
    assert lr.main(["confirm", "--run", run, "--out", out, "--root", root]) == 1
    assert "no matching headless process" in capsys.readouterr().out
    with open(ps, "w", encoding="utf-8") as fh:
        fh.write("  500 /usr/bin/python3 -m alpaca.research.runner --run %s\n" % run)
    assert lr.main(["confirm", "--run", run, "--out", out, "--root", root]) == 1
    assert "the runner's argv" in capsys.readouterr().out
    with open(ps, "w", encoding="utf-8") as fh:
        fh.write("  500 /h/.venv/bin/python -I -m alpaca.research.runner --run %s\n" % run)
    with open(os.path.join(out, "headless-refusals.txt"), "w", encoding="utf-8") as fh:
        fh.write("2026-10-02T00:00:00 RUN-HEADLESS-REFUSED: a headless claude agent is refused\n")
    assert lr.main(["confirm", "--run", run, "--out", out, "--root", root]) == 1
    assert "headless-refusals.txt is empty" in capsys.readouterr().out
    os.remove(os.path.join(out, "status-final.json"))
    with open(os.path.join(out, "headless-refusals.txt"), "w", encoding="utf-8") as fh:
        fh.write("")
    assert lr.main(["confirm", "--run", run, "--out", out, "--root", root]) == 1
    assert "engine.ok" in capsys.readouterr().out
