"""t-475: every animation and live path of the Studio has a named, browser-driven check.

The checks live in alpaca/tests/studio/motion.mjs, one or more per row M1 to M13 and L1 to L6 of
design/studio-template/design.md section 1d, plus reduced motion. They run once per module in headless
Chrome against the fixture project (studio_fixture.py) served by `alpaca serve` under the fixed clock;
each check is reported here as its own test. The fail case of the contract (a check that passes with
its animation code removed) is covered by test_a_check_fails_when_its_code_is_removed: motion.mjs
--mutate removes one piece of code in the served page and the check that guards it must fail.
STUDIO_ALL_MUTATIONS=1 runs every mutation instead of the default few. Skips without node and
google-chrome."""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

from alpaca.tests import studio_shots
from alpaca.tests.devfiles import needs_dev_files

HERE = os.path.dirname(os.path.abspath(__file__))
MOTION = os.path.join(HERE, "studio", "motion.mjs")
DESIGN = os.path.join(os.path.dirname(os.path.dirname(HERE)), "design", "studio-template", "design.md")
SOURCE = open(MOTION, encoding="utf-8").read()
# the plain checks, the reaction rows (ARTHUR_TRIGGERS) and the looping-pose rows (ARTHUR_LOOPS)
CHECK_IDS = (re.findall(r"^check\('([^']+)'", SOURCE, re.M) + re.findall(r"^  \['(M8-[^']+)'", SOURCE, re.M)
             + ["M7-arthur-loop-" + p.replace("_", "-") for p in re.findall(r"^  \['([a-z_]+)', o =>", SOURCE, re.M)])
MUTATIONS = dict(re.findall(r"^  '([a-z-]+)': \{check: '([^']+)'", SOURCE, re.M))
# Run in the suite by default: a still CSS animation, a removed timer, a removed state rule, a snap
# instead of an ease, and a lost live-path generation.
DEFAULT_MUTATIONS = ["beams-still", "pulse-cycle-off", "arthur-no-offline", "shapeshift-snap", "lib-marks-all"]


def _rows(kind):
    text = open(DESIGN, encoding="utf-8").read()
    return sorted(set(re.findall(r"^\| (%s\d+) \|" % kind, text, re.M)), key=lambda r: int(r[1:]))


# design/ is a development folder; a project made by alpaca new or a release copy does not carry it
@needs_dev_files("design/studio-template/design.md")
def test_every_animation_and_live_row_of_the_design_has_a_named_check():
    rows = {re.match(r"([ML]\d+)-", c).group(1) for c in CHECK_IDS if re.match(r"[ML]\d+-", c)}
    want = _rows("M") + _rows("L")
    assert want and set(want) <= rows, sorted(set(want) - rows)
    assert len(CHECK_IDS) == len(set(CHECK_IDS))
    assert set(MUTATIONS.values()) <= set(CHECK_IDS)


@pytest.fixture(scope="module")
def studio(tmp_path_factory):
    node, chrome, why = studio_shots.tools()
    if not node:
        pytest.skip(why)
    work = str(tmp_path_factory.mktemp("studio-motion"))
    root = studio_shots.build_fixture(work)
    with studio_shots.serve(root, running=True) as base:
        def run(out, *, checks=None, mutate=None):
            cmd = [node, MOTION, "--base", base, "--root", root, "--python", sys.executable, "--out", out]
            if checks:
                cmd += ["--checks", ",".join(checks)]
            if mutate:
                cmd += ["--mutate", mutate]
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=2400, env=dict(os.environ, CHROME=chrome))
            with open(out, encoding="utf-8") as fh:
                return json.load(fh), done.stdout + done.stderr
        yield run


@pytest.fixture(scope="module")
def results(studio, tmp_path_factory):
    out = str(tmp_path_factory.mktemp("motion") / "results.json")
    data, log = studio(out)
    print(log)
    return {r["id"]: r for r in data["results"]}, log


@pytest.mark.parametrize("check", CHECK_IDS)
def test_motion_check(results, check):
    found, log = results
    assert check in found, log[-3000:]
    assert found[check]["pass"], "%s: %s" % (check, found[check]["info"])


@pytest.mark.parametrize("mutation", sorted(MUTATIONS) if os.environ.get("STUDIO_ALL_MUTATIONS") else DEFAULT_MUTATIONS)
def test_a_check_fails_when_its_code_is_removed(studio, tmp_path, mutation):
    target = MUTATIONS[mutation]
    data, log = studio(str(tmp_path / "mutated.json"), checks=[target], mutate=mutation)
    assert "MUTATION TEXT NOT FOUND" not in log and "MUTATION NOT APPLIED" not in log, log   # the removal happened
    row = {r["id"]: r for r in data["results"]}[target]
    assert not row["pass"], "%s still passes with %s applied: %s" % (target, mutation, row["info"])
