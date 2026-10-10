"""t-109: the reference pack of the research engine (packs/refpack/ and packs/refpack-toyzip/),
design/research-engine/architecture.md section 2.3 and runbook-format-3.md sections 10 and 11,
FR-057, FR-061, SC-005, SC-014 and SC-025 of design/research-engine/spec.md.

Done when (the contract of t-109):

  * the pack's runbooks pass the format 3 check and intake, together in one project
    (test_both_runbooks_pass_the_format_3_check, test_the_check_does_not_pass_a_broken_pack,
    test_both_runbooks_take_intake_in_one_project, test_the_loader_freezes_both_runbooks,
    test_the_catalog_lists_both_runbooks);
  * the baseline is measured and recorded: three measurements with the declared repeats equal the
    recorded baseline (test_the_baseline_is_measured_with_the_declared_repeats and, through the
    engine's own gates in a jail, test_the_engine_measures_the_baseline_and_runs_the_holdout);
  * each repair task shows base FAIL and fix PASS: the pack's own check on made-up upstream history
    (test_verify_cases_shows_base_fail_and_fix_pass, with its negative cases), the engine's gates on
    the same history (test_the_engine_proves_and_gates_a_repair_case), and the five real cases when
    the pinned upstream is reachable (test_the_real_cases_fail_on_the_base_and_pass_on_the_fix).

Fail case: metric repeats that disagree beyond the declared noise margin
(test_repeats_that_disagree_beyond_the_margin_are_caught, and the engine's untested verdict in
test_the_engine_measures_the_baseline_and_runs_the_holdout).

Every test builds its project in a temporary folder. No test touches the network or starts an agent
runtime. The gate tests need bubblewrap and skip without it; the repair gate tests also need the
pinned test dependencies (`setup_cases.py deps`) and skip without them. The test of the five real
cases runs only when REFPACK_UPSTREAM names a clone of python-humanize (or its URL).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap

import pytest

from alpaca import cli, db, intake, runbook
from alpaca.gates import verdict as vc
from alpaca.research import catalog, contract, events, gates, holdout, isolation, jail, markers, paths
from alpaca.tests.conftest import REPO
from alpaca.tests.devfiles import require_dev_files

PACK = os.path.join(REPO, "packs", "refpack")
TOYZIP = os.path.join(REPO, "packs", "refpack-toyzip")
BENCH = os.path.join(PACK, "bench")
REPAIR_DIR = os.path.join(PACK, "repair")
DEPS = "/var/tmp/alpaca-refpack-deps"
IMPROVE = "packs/refpack/improve.yaml"
IMPROVE_SPEC = "packs/refpack/improve-spec.md"
REPAIR = "packs/refpack/repair.yaml"
REPAIR_SPEC = "packs/refpack/repair-spec.md"

# The pack's own test: module-level code below loads the pack's scripts, and a release ships no
# packs/ (ALPACA-MANIFEST), so the module skips there and runs in the development tree.
require_dev_files("packs/refpack", "packs/refpack-toyzip")

needs_jail = pytest.mark.skipif(not jail.available()[0], reason="bubblewrap is not available on this host")
needs_deps = pytest.mark.skipif(not os.path.isdir(os.path.join(DEPS, "pytest")),
                                reason="the pinned test dependencies are not installed (setup_cases.py deps)")


# ------------------------------------------------------------------------------ helpers
def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


setup_cases = _module("refpack_setup_cases", os.path.join(REPAIR_DIR, "setup_cases.py"))
render_repair = _module("refpack_render_repair_yaml", os.path.join(REPAIR_DIR, "render_repair_yaml.py"))
verify_cases = _module("refpack_verify_cases", os.path.join(REPAIR_DIR, "verify_cases.py"))
mutate = _module("refpack_mutate", os.path.join(BENCH, "mutate.py"))
corpus_gen = _module("refpack_corpus_gen", os.path.join(BENCH, "corpus_gen.py"))


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def _write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if mode is not None:
        os.chmod(path, mode)
    return path


def _git(root, *args, env=None):
    return subprocess.run(["git", "-C", root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                           "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"] + list(args),
                          check=True, capture_output=True, text=True, env=env).stdout


def _run(args, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("timeout", 300)
    return subprocess.run([sys.executable] + list(args), **kw)


def _metric_lines(text):
    return [line for line in text.splitlines() if line.startswith("METRIC:")]


def _measure(src, corpus=None, manifest=None, *extra):
    corpus = corpus or os.path.join(BENCH, "corpus")
    manifest = manifest or os.path.join(BENCH, "corpus.json")
    return _run([os.path.join(BENCH, "measure.py"), "--src", src, "--corpus", corpus, "--manifest", manifest]
                + list(extra))


def _codec(tmp_path, text, name="codec"):
    """A folder that holds a toyzip.py with the given source."""
    folder = tmp_path / name
    folder.mkdir()
    (folder / "toyzip.py").write_text(text, encoding="utf-8")
    return str(folder)


def _plain():
    return _read(TOYZIP, "toyzip.py")


#: a codec whose output size differs from one process to the next: it pads its output with a number
#: of zero bytes taken from the per-process string hash, which `python -I` randomizes. The padding is
#: ignored by decompress, so every round trip still passes.
JITTER = _plain() + textwrap.dedent('''

    _plain_compress = compress


    def compress(data):
        return _plain_compress(data) + bytes(hash("toyzip") % 1009)
    ''')


@pytest.fixture
def repo(project):
    """The pack and the code it edits in a temporary git repository that is also the project root,
    committed once, with one open op: the shape of section 10 of the format 3 design."""
    root = project
    _write(os.path.join(root, "project.yaml"), "# the refpack test project\nname: refpack-test\n")
    _write(os.path.join(root, ".gitignore"), ".alpaca/\ncontracts/research/\n")
    shutil.copytree(PACK, os.path.join(root, "packs", "refpack"), ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(TOYZIP, os.path.join(root, "packs", "refpack-toyzip"), ignore=shutil.ignore_patterns("__pycache__"))
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    assert cli.main(["op", "new", "Smaller toyzip", "--done-when", "every row proven"]) == 0
    return root


# ------------------------------------------------------------------------ the format 3 check
@pytest.mark.parametrize("rel, spec, items", [(IMPROVE, IMPROVE_SPEC, 9), (REPAIR, REPAIR_SPEC, 13)])
def test_both_runbooks_pass_the_format_3_check(rel, spec, items):
    got = runbook.check(os.path.join(REPO, rel), spec_path=os.path.join(REPO, spec))
    assert got["verdict"] == "PASS" and got["code"] == vc.PASS, got["errors"]
    assert got["errors"] == [] and got["warnings"] == []
    assert got["coverage"]["required"] == items and got["coverage"]["missing"] == []
    assert len(got["coverage"]["covered"]) == items


def test_the_cli_check_ends_in_a_pass_line_for_both_runbooks(capsys):
    for rel, spec in ((IMPROVE, IMPROVE_SPEC), (REPAIR, REPAIR_SPEC)):
        capsys.readouterr()
        assert cli.main(["runbook", "check", os.path.join(REPO, rel), "--spec", os.path.join(REPO, spec)]) == 0
        assert capsys.readouterr().out.rstrip().endswith("GATE alpaca-runbook-check: PASS")


def test_the_two_runbooks_are_format_3_and_name_their_pack():
    for rel, mode in ((IMPROVE, "improve"), (REPAIR, "repair")):
        data, _report = runbook.load(os.path.join(REPO, rel))
        assert data["runbook"] == 3 and data["research"]["mode"] == mode
        assert data["research"]["defaults"] == "pack.yaml"
    pack = _read(PACK, "pack.yaml")
    assert pack.startswith("# Settings both research runbooks") and "pack: refpack" in pack
    # the toyzip lessons reach the improve prompts only: the repair runbook carries none
    assert "lessons" not in pack and "lessons" not in runbook.load(os.path.join(REPO, REPAIR))[0]["research"]
    assert runbook.load(os.path.join(REPO, IMPROVE))[0]["research"]["lessons"] == {"file": "lessons.md"}


def _broken(tmp_path, rel, old, new):
    folder = tmp_path / "pack"
    shutil.copytree(PACK, str(folder), ignore=shutil.ignore_patterns("__pycache__"))
    path = os.path.join(str(folder), os.path.basename(rel))
    text = _read(path)
    assert old in text, old
    _write(path, text.replace(old, new, 1))
    spec = os.path.join(str(folder), os.path.basename(rel).replace(".yaml", "-spec.md"))
    return runbook.check(path, spec_path=spec)


def test_the_check_does_not_pass_a_broken_pack(tmp_path):
    """The pass above means something: take a covers line off the metric, a covered item off a repair
    case, or give a target a default source, and the check refuses each with its own code."""
    got = _broken(tmp_path / "a", IMPROVE, "      covers: [SC-001, SC-004, EC-002]\n", "")
    assert got["verdict"] == "FAIL" and "COVERS-NONE" in [e["code"] for e in got["errors"]]
    got = _broken(tmp_path / "b", IMPROVE, "      source: spec:SC-001\n", "      source: default\n")
    assert "TARGET-SOURCE" in [e["code"] for e in got["errors"]]
    got = _broken(tmp_path / "c", REPAIR, "        covers: [SC-105, EC-106]\n", "        covers: [SC-105]\n")
    assert got["verdict"] == "FAIL" and "SPEC-UNCOVERED" in [e["code"] for e in got["errors"]]
    got = _broken(tmp_path / "d", REPAIR, "        base: 441094817334e072c2b77cff33662a722438db32\n", "        base: main\n")
    assert "BASE-NOT-COMMIT" in [e["code"] for e in got["errors"]]


def test_the_improve_contract_declares_a_deterministic_metric_with_a_zero_margin():
    """The fail case's contract side: the declared noise margin is 0, the repeats are 3, and every
    number the spec states appears in the runbook that measures it."""
    block = runbook.load(os.path.join(REPO, IMPROVE))[0]["research"]
    (metric,) = block["metrics"]
    assert (metric["id"], metric["kind"], metric["direction"]) == ("total_bytes", "deterministic", "lower")
    assert (metric["repeats"], metric["min_ok"], metric["margin"]) == (3, 3, 0)
    spec = _read(PACK, "improve-spec.md")
    assert "at most %d bytes" % metric["target"] in spec and metric["source"] == "spec:SC-001"
    assert "noise margin is 0 bytes" in spec
    stage = [s for s in runbook.load(os.path.join(REPO, IMPROVE))[0]["stages"] if s["id"] == "holdout-bench"][0]
    baseline = json.loads(_read(PACK, "baseline.json"))
    assert "--max-total %d" % baseline["holdout"]["total_bytes"] in stage["run"]
    assert "at most %d bytes" % baseline["holdout"]["total_bytes"] in spec
    assert block["controls"]["random"] is True and block["accept"]["after_targets"] == "pareto"


# ------------------------------------------------------------------------------- intake, loader
def _intake(capsys, root, spec, rel, *extra):
    capsys.readouterr()
    code = cli.main(["--session", "test", "intake", os.path.join(root, spec), os.path.join(root, rel), "--json"]
                    + list(extra))
    return code, json.loads(capsys.readouterr().out)


def _files(root):
    out = {}
    for base, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in names:
            rel = os.path.relpath(os.path.join(base, name), root).replace(os.sep, "/")
            if not rel.startswith(".alpaca/alpaca.db"):
                with open(os.path.join(base, name), "rb") as fh:
                    out[rel] = fh.read()
    return out


def test_both_runbooks_take_intake_in_one_project(repo, capsys):
    """SC-005 and SC-025: the improve and the repair runbook take `alpaca intake` in one project, each
    lands its own contract, project.yaml keeps its bytes and no profile is written, and a re-intake
    changes nothing."""
    conn = db.connect(repo)
    project_yaml = _read(repo, "project.yaml")
    code, improve = _intake(capsys, repo, IMPROVE_SPEC, IMPROVE)
    assert code == vc.PASS and improve["verdict"] == "PASS" and improve["changed"] is True
    rows = dict((r["key"], r) for group in improve["rows"].values() for r in group)
    assert sorted(rows) == ["EC-001", "EC-002", "EC-003", "EC-004", "SC-001", "SC-002", "SC-003", "SC-004", "SC-005"]
    assert rows["SC-001"]["shown_by"] == "research metric:total_bytes" and "target <= 88000" in rows["SC-001"]["bar"]
    assert rows["SC-002"]["shown_by"] == "unit/unit-exit"
    code, repair = _intake(capsys, repo, REPAIR_SPEC, REPAIR)
    assert code == vc.PASS and repair["verdict"] == "PASS"
    repair_rows = dict((r["key"], r) for group in repair["rows"].values() for r in group)
    assert sorted(repair_rows) == ["EC-101", "EC-102", "EC-103", "EC-104", "EC-105", "EC-106", "SC-101", "SC-102",
                                   "SC-103", "SC-104", "SC-105", "SC-106", "SC-107"]
    assert repair_rows["SC-101"]["bar"].startswith("repair case h1-filesize-rollover:")
    assert "at least 500 tests" in repair_rows["SC-106"]["bar"]
    # the two specs number their items apart, so the op holds one row per item and intake warns of nothing
    assert not set(rows) & set(repair_rows) and not improve.get("warnings") and not repair.get("warnings")
    landed = db.events(conn, kind=contract.KIND_CONTRACT, limit=10 ** 9)
    assert [(e["ref"], e["data"]["runbook"], e["data"]["mode"], e["data"]["via"]) for e in landed] == [
        ("refpack-improve", IMPROVE, "improve", "intake"), ("refpack-repair", REPAIR, "repair", "intake")]
    assert all(e["data"]["pack_sha256"] for e in landed)
    # the fail case of intake: no profile, no change to project.yaml
    assert _read(repo, "project.yaml") == project_yaml
    assert not os.path.exists(os.path.join(repo, intake.PROFILE_FILE))
    # a re-intake of either one writes nothing
    before = db.events(conn, limit=10 ** 9)
    assert _intake(capsys, repo, IMPROVE_SPEC, IMPROVE)[1]["changed"] is False
    assert _intake(capsys, repo, REPAIR_SPEC, REPAIR)[1]["changed"] is False
    assert len(db.events(conn, limit=10 ** 9)) == len(before)


def test_intake_refuses_a_pack_whose_check_fails(repo, capsys):
    """The negative of the intake test: a metric with no covers stops intake before any row lands."""
    path = os.path.join(repo, IMPROVE)
    _write(path, _read(path).replace("      covers: [SC-001, SC-004, EC-002]\n", ""))
    conn = db.connect(repo)
    before = len(db.events(conn, limit=10 ** 9))
    capsys.readouterr()
    code = cli.main(["--session", "test", "intake", os.path.join(repo, IMPROVE_SPEC), path])
    assert code != vc.PASS
    assert len(db.events(conn, limit=10 ** 9)) == before


def test_the_loader_freezes_both_runbooks(repo):
    """The loader reads both runbooks: it freezes the protected files, keeps the held-out files and
    the case data out of the copies agents see, and reads the store reproducers of the five cases."""
    conn = db.connect(repo)
    frozen, _fields = contract.freeze(conn, repo, "rr-001", runbook=IMPROVE, task=None, session="test")
    assert frozen.mode == "improve" and frozen.runbook_id == "refpack-improve" and frozen.pack == "refpack"
    folder = paths.contract_dir(repo, "rr-001")
    manifest = json.loads(_read(folder, "files.json"))
    assert "files/packs/refpack/bench/measure.py" in manifest
    assert "files/packs/refpack/bench/corpus/notes.txt" in manifest
    assert os.access(os.path.join(folder, "files", "packs", "refpack", "bench", "mutate.py"), os.X_OK)
    assert "holdout/packs/refpack/holdout/corpus/notes.txt" in manifest
    assert not any(k.startswith("files/packs/refpack/holdout/") for k in manifest)
    assert not any("refpack-toyzip" in k for k in manifest)       # the editable code is not frozen
    assert [m.id for m in frozen.metrics] == ["total_bytes"] and frozen.targets[0].value == 88000
    assert frozen.editable == ("packs/refpack-toyzip/",) and frozen.protected == ("packs/refpack/",)
    assert (frozen.slots, frozen.rounds, frozen.controls_random, frozen.controls_mutate) == (
        2, 3, True, "bench/mutate.py")
    assert frozen.holdout_stages == ("holdout-bench",) and frozen.holdout_paths == ("packs/refpack/holdout/",)
    # the clone an agent works in holds the code, the verifier and the visible corpus, not the held-out files
    clone = isolation.build_clone(repo, "rr-001", frozen, line="champion", stamp=paths.stamp())
    tracked = _git(clone.path, "ls-tree", "-r", "--name-only", clone.base_commit).split()
    assert "packs/refpack-toyzip/toyzip.py" in tracked and "packs/refpack/bench/corpus/notes.txt" in tracked
    assert not [p for p in tracked if p.startswith("packs/refpack/holdout/")]


@needs_deps
def test_the_loader_freezes_the_repair_runbook_with_its_store(repo):
    assert setup_cases.main(["store", "--project", repo]) == 0
    conn = db.connect(repo)
    frozen, _fields = contract.freeze(conn, repo, "rr-001", runbook=REPAIR, task=None, session="test")
    cases = json.loads(_read(REPAIR_DIR, "hidden", "cases.json"))["cases"]
    assert frozen.mode == "repair" and frozen.accept_rule == "repair"
    assert [r.case_id for r in frozen.repair] == [c["id"] for c in cases]
    assert [r.base for r in frozen.repair] == [c["base"] for c in cases]
    assert [list(r.fail_to_pass.tests) for r in frozen.repair] == [c["fail_to_pass"] for c in cases]
    assert all(r.fail_to_pass.origin == "store" for r in frozen.repair)
    assert sorted(frozen.reproducers) == sorted(c["id"] for c in cases)
    assert [r.fix_terms for r in frozen.repair] == [(c["fix"], c["fix"][:12]) for c in cases]
    assert frozen.pass_to_pass.min_tests == 500 and frozen.read_paths == (DEPS,)
    folder = paths.contract_dir(repo, "rr-001")
    manifest = json.loads(_read(folder, "files.json"))
    # the case data is hidden: it is neither frozen into files/ nor part of the clone
    assert not any("repair/hidden" in k for k in manifest)
    clone = isolation.build_clone(repo, "rr-001", frozen, line="champion", stamp=paths.stamp())
    assert not [p for p in _git(clone.path, "ls-tree", "-r", "--name-only", clone.base_commit).split()
                if "repair/hidden" in p]
    for case in cases:
        assert "contract/hidden/%s/run.sh" % case["id"] in frozen.reproducers[case["id"]] + "/run.sh"
        assert os.access(os.path.join(folder, "hidden", case["id"], "run.sh"), os.X_OK)


@needs_deps
def test_the_loader_refuses_the_repair_runbook_without_its_store(repo):
    """Without the store the reproducers are missing: the loader says so and writes nothing."""
    from alpaca.research import errors
    conn = db.connect(repo)
    with pytest.raises(errors.ContractRefusal) as err:
        contract.freeze(conn, repo, "rr-001", runbook=REPAIR, task=None, session="test")
    assert err.value.reason == "CONTRACT-REPRODUCER-MISSING"
    assert not os.path.exists(paths.contract_dir(repo, "rr-001"))


def test_the_catalog_lists_both_runbooks(repo):
    """Two runbooks of one pack are listed together (DD-04). The repair runbook reads as not ready
    until the owner fills the reproducer store, and ready after."""
    found = catalog.find(repo)
    assert [(e["path"], e["id"], e["mode"]) for e in found] == [
        (IMPROVE, "refpack-improve", "improve"), (REPAIR, "refpack-repair", "repair")]
    assert found[0]["check"] == "PASS" and found[1]["check"] == "CONTRACT-REPRODUCER-MISSING"
    assert found[0]["n"] == found[1]["n"] == 3 and found[0]["k"] == found[1]["k"] == 2
    assert found[0]["contract_sha256"] != found[1]["contract_sha256"]
    if os.path.isdir(os.path.join(DEPS, "pytest")):
        assert setup_cases.main(["store", "--project", repo]) == 0
        assert [e["check"] for e in catalog.find(repo)] == ["PASS", "PASS"]


# ------------------------------------------------------------------------------ the case data
def _cases():
    return json.loads(_read(REPAIR_DIR, "hidden", "cases.json"))


def test_the_cases_file_and_the_repair_runbook_say_the_same_thing():
    data = _cases()
    assert render_repair.render(data["cases"]) == _read(PACK, "repair.yaml")
    block = runbook.load(os.path.join(REPO, REPAIR))[0]["research"]
    got = [(c["id"], c["base"], c["fail_to_pass"]["tests"], c["suspect_files"], c["covers"])
           for c in block["repair"]["cases"]]
    assert got == [(c["id"], c["base"], c["fail_to_pass"], c["suspect_files"], c["covers"]) for c in data["cases"]]


def test_every_case_is_pinned_to_full_commit_ids_with_a_report_and_tests():
    data = _cases()
    assert len(data["cases"]) == 5 and data["upstream"]["license"] == "MIT"
    assert data["upstream"]["url"] == "https://github.com/python-humanize/humanize"
    ids = set()
    for case in data["cases"]:
        for key in ("fix", "parent", "base"):
            assert len(case[key]) == 40 and set(case[key]) <= set("0123456789abcdef"), (case["id"], key)
        assert case["base"] != case["parent"] != case["fix"]
        assert case["fail_to_pass"] and all("::" in t for t in case["fail_to_pass"])
        assert case["test_files"] and case["fix_files"] and all(f.startswith("src/humanize/") for f in case["fix_files"])
        assert os.path.isfile(os.path.join(PACK, "bugs", case["id"] + ".md"))
        ids.add(case["id"])
    assert len(ids) == 5 and len({c["base"] for c in data["cases"]}) == 5
    assert len(data["upstream"]["pin"]) == 40


def test_the_bug_reports_hide_the_fix_and_its_tests():
    """An agent reads the bug report and sees the tests it must make pass; it must not find the
    fix commit, the pull request or the test ids there."""
    for case in _cases()["cases"]:
        text = _read(PACK, "bugs", case["id"] + ".md")
        assert case["fix"][:7] not in text and case["parent"][:7] not in text
        assert "pull/" not in text and "test_" not in text and "tests/" not in text
        assert "```python" in text and "Expected" in text or "expected" in text


def test_the_pack_credits_its_sources_and_copies_no_upstream_code():
    readme = _read(PACK, "README.md")
    data = _cases()
    assert "python-humanize" in readme and "MIT license" in readme and "Jason Moiron" in readme
    assert data["upstream"]["pin"] in readme and "https://github.com/python-humanize/humanize" in readme
    for case in data["cases"]:
        assert case["fix"] in readme and case["base"] in readme
    assert "Hutter Prize" in readme and "requirements.txt" in readme
    for line in _read(REPAIR_DIR, "requirements.txt").splitlines():
        if line and not line.startswith("#"):
            assert "==" in line and "--hash=sha256:" in line, line
    # no file of the pack holds a function of the upstream library
    for base, _dirs, names in os.walk(PACK):
        for name in names:
            if name.endswith((".py", ".md", ".yaml", ".json")):
                text = _read(base, name)
                assert "def naturalsize" not in text and "def intcomma" not in text, name


def test_the_pack_files_are_ascii_and_free_of_the_long_dash():
    for top in (PACK, TOYZIP):
        for base, dirs, names in os.walk(top):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in names:
                with open(os.path.join(base, name), "rb") as fh:
                    raw = fh.read()
                assert raw.isascii(), os.path.join(base, name)
                assert b"\xe2\x80\x94" not in raw


def test_the_pack_lives_where_the_design_puts_a_pack_and_ships_no_other_domain():
    assert os.path.isdir(PACK) and os.path.isfile(os.path.join(PACK, "pack.yaml"))
    assert sorted(os.listdir(os.path.join(REPO, "packs"))) == ["refpack", "refpack-toyzip"]
    for name in ("improve.yaml", "repair.yaml", "improve-spec.md", "repair-spec.md", "README.md", "baseline.json"):
        assert os.path.isfile(os.path.join(PACK, name)), name


# --------------------------------------------------------------------------- the improve corpus
def _manifest_of(files) -> dict:
    return {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}


def _holdout_seed() -> int:
    """The seed of the held-out corpus, found by a bounded search over the generator's output (its
    manifest is `holdout/corpus.json`). This file ships in the clone an attempt's agents read, so it
    holds no literal seed for the held-out corpus (t-351 F49, rr-021 t-346: an attempt regenerated
    the held-out corpus from a seed it found in a file it could read). The search states residual R2
    of in-session-runtime.md section 12 in code: a small seed that is guessed or searched still
    regenerates the held-out bytes from the generator an agent can read."""
    want = json.loads(_read(PACK, "holdout/corpus.json"))
    for seed in range(1, 64):
        if _manifest_of(corpus_gen.generate(seed)) == want:
            return seed
    raise AssertionError("no seed below 64 cuts the held-out corpus")


@pytest.mark.parametrize("which, folder, manifest", [("visible", "bench/corpus", "bench/corpus.json"),
                                                     ("held-out", "holdout/corpus", "holdout/corpus.json")])
def test_the_corpora_are_what_the_generator_writes(tmp_path, which, folder, manifest):
    """The generator is deterministic and the committed files are its output: a changed byte in a
    corpus file, or a changed generator, fails this test."""
    seed = 11 if which == "visible" else _holdout_seed()
    got = _run([os.path.join(BENCH, "corpus_gen.py"), "--seed", str(seed), "--out", str(tmp_path / "c"),
                "--manifest", str(tmp_path / "m.json")])
    assert got.returncode == 0, got.stderr
    assert _read(str(tmp_path), "m.json") == _read(PACK, manifest)
    recorded = json.loads(_read(PACK, manifest))
    for name, sha in recorded.items():
        with open(os.path.join(PACK, folder, name), "rb") as fh:
            data = fh.read()
        assert hashlib.sha256(data).hexdigest() == sha
        with open(str(tmp_path / "c" / name), "rb") as fh:
            assert fh.read() == data
    assert sorted(os.listdir(os.path.join(PACK, folder))) == sorted(recorded)


def test_the_two_corpora_differ_and_the_generator_depends_only_on_the_seed():
    a, b = corpus_gen.generate(11), corpus_gen.generate(_holdout_seed())
    assert corpus_gen.generate(11) == a and all(a[k] != b[k] for k in a)
    assert sum(len(v) for v in a.values()) == 212992 == sum(len(v) for v in b.values())


# -------------------------------------------------------------------------- baseline and repeats
def test_the_baseline_is_measured_with_the_declared_repeats():
    """SC-005: the baseline recorded in baseline.json is what three measurements of the unchanged
    codec give, they agree within the declared margin, and the held-out number is recorded too."""
    recorded = json.loads(_read(PACK, "baseline.json"))
    declared = runbook.load(os.path.join(REPO, IMPROVE))[0]["research"]["metrics"][0]
    assert (recorded["repeats"], recorded["margin"]) == (declared["repeats"], declared["margin"])
    values = []
    for _ in range(recorded["repeats"]):
        got = _measure(TOYZIP)
        assert got.returncode == 0, got.stderr
        (line,) = _metric_lines(got.stdout)
        values.append(int(line.split()[2]))
    assert values == recorded["visible"]["repeats"] == [recorded["visible"]["total_bytes"]] * 3
    assert max(values) - min(values) <= declared["margin"] == recorded["visible"]["spread"]
    held = _measure(TOYZIP, os.path.join(PACK, "holdout", "corpus"), os.path.join(PACK, "holdout", "corpus.json"))
    assert int(_metric_lines(held.stdout)[0].split()[2]) == recorded["holdout"]["total_bytes"]
    codec = os.path.getsize(os.path.join(TOYZIP, "toyzip.py"))
    assert codec == recorded["visible"]["codec_bytes"]
    assert recorded["visible"]["compressed_bytes"] + codec == recorded["visible"]["total_bytes"]
    assert recorded["visible"]["total_bytes"] > declared["target"] > 0


def test_the_repeat_check_agrees_on_the_unchanged_codec():
    got = _run([os.path.join(BENCH, "repeat_check.py"), "--src", TOYZIP])
    assert got.returncode == 0, got.stderr
    out = json.loads(got.stdout)
    assert out["verdict"] == "AGREE" and out["spread"] == 0 and len(out["values"]) == 3


def test_repeats_that_disagree_beyond_the_margin_are_caught(tmp_path):
    """The fail case: a codec whose output size changes from run to run gives repeats that disagree,
    and the check says so with a non-zero exit. With a margin wide enough for the spread it agrees."""
    src = _codec(tmp_path, JITTER)
    got = _run([os.path.join(BENCH, "repeat_check.py"), "--src", src, "--repeats", "5"])
    assert got.returncode == 1, got.stdout
    out = json.loads(got.stdout)
    assert out["verdict"] == "DISAGREE" and out["spread"] > out["margin"] == 0 and len(set(out["values"])) > 1
    wide = _run([os.path.join(BENCH, "repeat_check.py"), "--src", src, "--repeats", "3", "--margin", "100000"])
    assert wide.returncode == 0 and json.loads(wide.stdout)["verdict"] == "AGREE"


# ----------------------------------------------------------------------------- the metric gates
def test_the_metric_block_has_one_token_line_and_nothing_else(tmp_path):
    got = _measure(TOYZIP)
    lines = got.stdout.splitlines()
    assert lines[0] == "ALPACA-RESULTS-BEGIN" and lines[-1] == "ALPACA-RESULTS-END" and len(lines) == 3
    # inside the engine the token comes on a descriptor and ends every marker line
    r, w = os.pipe()
    os.write(w, b"tok123\n")
    os.close(w)
    env = dict(os.environ, ALPACA_RESULTS_FD=str(r))
    cmd = [sys.executable, os.path.join(BENCH, "measure.py"), "--src", TOYZIP, "--corpus", os.path.join(BENCH, "corpus"),
           "--manifest", os.path.join(BENCH, "corpus.json")]
    done = subprocess.run(cmd, env=env, pass_fds=(r,), capture_output=True, text=True)
    os.close(r)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[1:] == ["METRIC: total_bytes 101578 tok123", "ALPACA-RESULTS-END tok123"]
    report = markers.parse_metric(markers._with_token(done.stdout, "tok123"), "total_bytes")
    assert report == 101578


@pytest.mark.parametrize("source, gate", [
    ("import zlib\n", "source"),
    ("import os\n", "source"),
    ("from zlib import compress, decompress\n", "source"),
    ("from . import x\n", "source"),
    ("def compress(d):\n    return open('x', 'rb').read()\n", "source"),
    ("def compress(d):\n    return eval('d')\n", "source"),
    ("def compress(d):\n    return d.__class__\n", "source"),
    ("compress = decompress = lambda d: getattr(d, 'x')\n", "source"),
    ("def compress(:\n", "source"),
])
def test_the_source_gate_refuses_a_codec_that_borrows_or_cheats(tmp_path, source, gate):
    src = _codec(tmp_path, source)
    got = _measure(src)
    assert got.returncode == 2 and "GATE %s:" % gate in got.stderr
    assert "ALPACA-RESULTS-BEGIN" not in got.stdout and _metric_lines(got.stdout) == []
    check = _run([os.path.join(BENCH, "source_check.py"), "--src", src])
    assert check.returncode == 1
    # the unchanged codec passes the same gate
    assert _run([os.path.join(BENCH, "source_check.py"), "--src", TOYZIP]).returncode == 0


def test_a_run_that_prints_no_markers_gets_no_score(tmp_path):
    """EC-002: a codec that crashes, hangs on output or returns junk ends with exit 2 and no block, so
    the engine reads GATE-MARKERS and no number."""
    for name, text in (("crash", "def compress(d):\n    return 1 // 0\n\ndef decompress(b):\n    return b\n"),
                       ("junk", "def compress(d):\n    return d\n\ndef decompress(b):\n    return b'x'\n")):
        got = _measure(_codec(tmp_path, text, name))
        assert got.returncode == 2 and "ALPACA-RESULTS" not in got.stdout, name
    with pytest.raises(markers.MarkerError):
        markers.parse_metric(_measure(_codec(tmp_path, "def compress(d):\n    return 1 // 0\n", "again")).stdout, "total_bytes")


def test_the_corpus_must_match_its_manifest(tmp_path):
    corpus = tmp_path / "corpus"
    shutil.copytree(os.path.join(BENCH, "corpus"), str(corpus))
    (corpus / "notes.txt").write_bytes((corpus / "notes.txt").read_bytes() + b"x")
    got = _measure(TOYZIP, str(corpus))
    assert got.returncode == 2 and "GATE corpus_hash:" in got.stderr and "notes.txt" in got.stderr
    # a file the manifest lists but the folder lacks is a gate too
    (corpus / "notes.txt").write_bytes(b"")
    os.remove(str(corpus / "jobs.jsonl"))
    got = _measure(TOYZIP, str(corpus))
    assert got.returncode == 2 and "GATE corpus_hash:" in got.stderr
    assert sorted(json.loads(_read(BENCH, "corpus.json"))) == ["access.log", "jobs.jsonl", "ledger.csv", "notes.txt"]


def test_the_total_limit_turns_a_worse_holdout_into_a_failure(tmp_path):
    held = (os.path.join(PACK, "holdout", "corpus"), os.path.join(PACK, "holdout", "corpus.json"))
    assert _measure(TOYZIP, *held, "--max-total", "101462").returncode == 0
    worse = _codec(tmp_path, _plain() + "\n_pad = 1\n")
    got = _measure(worse, *held, "--max-total", "101462")
    assert got.returncode == 2 and "GATE max_total:" in got.stderr and _metric_lines(got.stdout) == []


def test_the_codec_own_size_is_charged(tmp_path):
    """Padding the source with comment bytes raises the total by exactly that many bytes."""
    plain = _metric_lines(_measure(TOYZIP).stdout)[0]
    padded = _metric_lines(_measure(_codec(tmp_path, _plain() + "#" * 100 + "\n")).stdout)[0]
    assert int(padded.split()[2]) - int(plain.split()[2]) == 101


def test_the_unit_gate_passes_the_baseline_and_refuses_a_codec_that_does_not_round_trip(tmp_path):
    args = ["--corpus", os.path.join(BENCH, "corpus"), "--manifest", os.path.join(BENCH, "corpus.json")]
    good = _run([os.path.join(BENCH, "unit_tests.py"), "--src", TOYZIP] + args)
    assert good.returncode == 0 and "cases passed" in good.stderr and "PASS empty" in good.stderr
    # a decompressor that drops its last byte: no input comes back whole
    head, _sep, tail = _plain().rpartition("return bytes(out)")
    wrong = _codec(tmp_path, head + "return bytes(out[:-1])" + tail, "wrong")
    bad = _run([os.path.join(BENCH, "unit_tests.py"), "--src", wrong] + args)
    assert bad.returncode == 1 and "FAIL roundtrip:" in bad.stderr


# ------------------------------------------------------------------------- the random control
@pytest.mark.parametrize("seed", range(1, 41))
def test_a_random_edit_never_makes_the_codec_smaller(tmp_path, seed):
    """The control's operators cannot beat the baseline: the edited file still parses, and it either
    fails a gate or measures at least the baseline. The same seed makes the same edit."""
    text, what = mutate.mutate(_plain(), seed)
    assert what is not None and text != _plain() and mutate.mutate(_plain(), seed) == (text, what)
    assert len(text.encode()) >= len(_plain().encode()), what          # an edit never shrinks the source
    compile(text, "toyzip.py", "exec")
    got = _measure(_codec(tmp_path, text))
    if got.returncode == 0:
        assert int(_metric_lines(got.stdout)[0].split()[2]) >= 101578, what
    else:
        assert got.returncode == 2


def test_the_mutate_script_edits_a_file_in_place_and_says_what_it_did(tmp_path):
    target = tmp_path / "toyzip.py"
    target.write_text(_plain(), encoding="utf-8")
    got = _run([os.path.join(BENCH, "mutate.py"), str(target), "7"])
    assert got.returncode == 0 and str(target) in got.stdout and target.read_text() != _plain()
    assert os.access(os.path.join(BENCH, "mutate.py"), os.X_OK)
    assert _run([os.path.join(BENCH, "mutate.py"), str(target)]).returncode == 64
    plain = tmp_path / "plain.py"
    plain.write_text("X = 1\n", encoding="utf-8")
    assert "no edit applied" in _run([os.path.join(BENCH, "mutate.py"), str(plain), "3"]).stdout


# -------------------------------------------------------------------------- the engine, improve
def _project_with_pack(repo, run="rr-001"):
    conn = db.connect(repo)
    frozen, _fields = contract.freeze(conn, repo, run, runbook=IMPROVE, task=None, session="test")
    clone = isolation.build_clone(repo, run, frozen, line="champion", stamp=paths.stamp())
    wt = isolation.fresh_tree(repo, clone, commit=clone.base_commit,
                              path=os.path.join(repo, ".alpaca", "trees", "t-001"))
    return conn, frozen, clone, wt


@needs_jail
def test_the_engine_measures_the_baseline_and_runs_the_holdout(repo):
    """SC-005 through the engine's own gates, each in an offline jail: the gate chain passes, the
    metric gate records three repeats with a spread inside the margin, the holdout stage passes on
    the champion; then a codec with a jittery size makes the metric UNTESTED (the fail case)."""
    conn, frozen, clone, wt = _project_with_pack(repo)
    res = gates.run_chain(conn, repo, run="rr-001", task="t-001", contract=frozen, wt=wt, mode="improve",
                          session="test")
    assert res.verdict == vc.PASS and res.first_failure is None
    # no gate leaves a file in the tree: the post-eval check finds no new ignored output and no change
    after = isolation.post_eval(clone, wt, head=clone.base_commit, keep=tuple(frozen.keep),
                                eval_outputs=tuple(frozen.outputs), before=())
    assert after.ok and after.new == () and after.changed == ()
    value = res.metrics["total_bytes"]
    baseline = json.loads(_read(PACK, "baseline.json"))
    assert value.median == baseline["visible"]["total_bytes"] and value.repeats == (101578.0,) * 3
    assert value.n_ok == 3 and value.spread == 0 <= frozen.metrics[0].margin and value.untested is False
    metric_event = [e for e in db.events(conn, kind=events.GATE, limit=10 ** 9)
                    if e["data"]["stage"] == "metric:total_bytes"][0]["data"]
    assert metric_event["offline"] is True and metric_event["verdict"] == "PASS"
    assert metric_event["metric"]["values"] == [101578.0] * 3 and metric_event["metric"]["spread"] == 0
    assert [e["data"]["stage"] for e in db.events(conn, kind=events.GATE, limit=10 ** 9)] == [
        "build", "source", "unit", "metric:total_bytes"]
    (held,) = holdout.run_holdout(conn, repo, "rr-001", champion=clone.base_commit, contract=frozen, session="test")
    assert held["verdict"] == "PASS" and held["stage"] == "holdout-bench"
    # the fail case: repeats that disagree beyond the margin leave the measurement untested
    codec = os.path.join(wt.path, "packs", "refpack-toyzip", "toyzip.py")
    _write(codec, JITTER)
    shaky = gates.measure(conn, repo, run="rr-001", task="t-002", contract=frozen, wt=wt, session="test")
    jittery = shaky["total_bytes"]
    assert jittery.untested is True and jittery.spread > frozen.metrics[0].margin
    assert len(set(jittery.repeats)) > 1


@needs_jail
def test_the_engine_refuses_a_worse_codec_at_the_gate_that_catches_it(repo):
    conn, frozen, _clone, wt = _project_with_pack(repo)
    codec = os.path.join(wt.path, "packs", "refpack-toyzip", "toyzip.py")
    _write(codec, "import zlib\n" + _plain())
    res = gates.run_chain(conn, repo, run="rr-001", task="t-001", contract=frozen, wt=wt, mode="improve",
                          session="test")
    assert res.verdict == vc.FAIL and res.first_failure == "source" and res.metrics == {}
    head, _sep, tail = _plain().rpartition("return bytes(out)")
    _write(codec, head + "return bytes(out[:-1])" + tail)
    res = gates.run_chain(conn, repo, run="rr-001", task="t-003", contract=frozen, wt=wt, mode="improve",
                          session="test")
    assert res.verdict == vc.FAIL and res.first_failure == "unit"


# --------------------------------------------------------------------- the repair tools, offline
RUNNER = os.path.join(REPAIR_DIR, "run_pytest.py")

TOY_TESTS = textwrap.dedent('''
    import pytest
    from humanize.lib import value

    def test_passes():
        assert value() == 1

    def test_fails():
        assert value() == 2

    @pytest.mark.skip(reason="skipped on purpose")
    def test_skipped():
        pass

    @pytest.mark.parametrize("a, b", [(1, 1), ("x y", "x y"), ("TEST: sneaky", "TEST: sneaky")])
    def test_param(a, b):
        assert a == b
    ''')


def _toy_tree(tmp_path, tests=TOY_TESTS, lib="def value():\n    return 1\n"):
    tree = tmp_path / "tree"
    _write(str(tree / "src" / "humanize" / "__init__.py"), "")
    _write(str(tree / "src" / "humanize" / "lib.py"), lib)
    _write(str(tree / "tests" / "test_toy.py"), tests)
    _write(str(tree / "pyproject.toml"), "[tool.pytest]\naddopts = ['--color=yes']\ntestpaths = ['tests']\n")
    return str(tree)


def _digest(root):
    out = {}
    for base, dirs, names in os.walk(root):
        for name in names:
            full = os.path.join(base, name)
            with open(full, "rb") as fh:
                out[os.path.relpath(full, root)] = hashlib.sha256(fh.read()).hexdigest()
    return out


def _runner(tree, tmp_path, *extra, token=None):
    empty = tmp_path / "nodeps"
    empty.mkdir(exist_ok=True)
    cmd = [sys.executable, RUNNER, "--tree", tree, "--deps", str(empty)] + list(extra)
    if token is None:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    r, w = os.pipe()
    os.write(w, (token + "\n").encode())
    os.close(w)
    done = subprocess.run(cmd, env=dict(os.environ, ALPACA_RESULTS_FD=str(r)), pass_fds=(r,), capture_output=True,
                          text=True, timeout=300)
    os.close(r)
    return done


def _tests(out):
    return dict((n, r) for n, _s, r in (line[len("TEST: "):].rpartition(" ... ") for line in out.splitlines()
                                        if line.startswith("TEST: ")))


def test_the_runner_reports_every_test_and_never_changes_the_tree(tmp_path):
    tree = _toy_tree(tmp_path)
    before = _digest(tree)
    got = _runner(tree, tmp_path)
    assert got.returncode == 0, got.stderr
    tests = _tests(got.stdout)
    assert tests["tests/test_toy.py::test_passes"] == "PASS" and tests["tests/test_toy.py::test_fails"] == "FAIL"
    assert tests["tests/test_toy.py::test_skipped"] == "SKIP"
    assert tests["tests/test_toy.py::test_param[x y-x y]"] == "PASS"
    assert _digest(tree) == before and not os.path.exists(os.path.join(tree, "src", "humanize", "_version.py"))
    lines = got.stdout.splitlines()
    assert lines[0] == "ALPACA-RESULTS-BEGIN" and lines[-1] == "ALPACA-RESULTS-END"
    report = markers.parse_tests(got.stdout)
    assert report.tests["tests/test_toy.py::test_passes"] == "PASS"
    # a test name that holds a marker word is written so the parser reads it as a name, not a marker
    assert report.tests["tests/test_toy.py::test_param[TEST_ sneaky-TEST_ sneaky]"] == "PASS"
    assert "TEST: sneaky" not in got.stdout


def test_the_runner_ends_every_marker_line_with_the_run_token(tmp_path):
    tree = _toy_tree(tmp_path)
    got = _runner(tree, tmp_path, token="abc123")
    assert got.returncode == 0, got.stderr
    body = got.stdout.splitlines()
    assert body[0] == "ALPACA-RESULTS-BEGIN" and body[-1] == "ALPACA-RESULTS-END abc123"
    assert all(line.endswith(" abc123") for line in body[1:])
    report = markers.parse_tests(markers._with_token(got.stdout, "abc123"))
    assert report.tests["tests/test_toy.py::test_fails"] == "FAIL"
    # the wrong token is not accepted by the parser
    with pytest.raises(markers.MarkerError):
        markers.parse_tests(markers._with_token(got.stdout, "other"))


def test_the_runner_selects_tests_and_reports_a_missing_one_as_a_failure(tmp_path):
    tree = _toy_tree(tmp_path)
    select = tmp_path / "select.txt"
    select.write_text("tests/test_toy.py::test_fails\ntests/test_toy.py::test_passes\ntests/test_toy.py::test_gone\n")
    got = _runner(tree, tmp_path, "--select-file", str(select))
    assert got.returncode == 0, got.stderr
    assert _tests(got.stdout) == {"tests/test_toy.py::test_fails": "FAIL", "tests/test_toy.py::test_passes": "PASS",
                                  "tests/test_toy.py::test_gone": "FAIL"}


def test_the_runner_records_a_module_that_does_not_import_as_a_failure(tmp_path):
    """A break that stops a test module from importing must read as missing passes, never as a pass."""
    tree = _toy_tree(tmp_path, lib="def value(:\n")
    got = _runner(tree, tmp_path)
    assert got.returncode == 0
    tests = _tests(got.stdout)
    assert tests and set(tests.values()) == {"FAIL"} and not [n for n in tests if "test_passes" in n]


def test_the_runner_gives_the_lib_its_generated_version_file_in_the_copy_only(tmp_path):
    tree = _toy_tree(tmp_path, tests="from humanize import _version\n\ndef test_version():\n    assert _version.__version__\n")
    got = _runner(tree, tmp_path)
    assert _tests(got.stdout) == {"tests/test_toy.py::test_version": "PASS"}
    assert not os.path.exists(os.path.join(tree, "src", "humanize", "_version.py"))


# --------------------------------------------------------- made-up upstream history for the tools
FUNCS = ["f1", "f2", "f3", "f4", "f5"]
BUGGY = {"f1": "x + 2", "f2": "x * 3", "f3": "x - 2", "f4": "x * x * x", "f5": "x"}
FIXED = {"f1": "x + 1", "f2": "x * 2", "f3": "x - 1", "f4": "x * x", "f5": "abs(x)"}
CHECK = {"f1": ("f1(1)", 2), "f2": ("f2(4)", 8), "f3": ("f3(5)", 4), "f4": ("f4(3)", 9), "f5": ("f5(-4)", 4)}
BASE_TESTS = ("from humanize.lib import f1, f2, f3, f4, f5\n\n\ndef test_callable():\n"
              "    assert all(callable(f) for f in (f1, f2, f3, f4, f5))\n\n\n"
              "def test_f2_zero():\n    assert f2(0) == 0\n\n\ndef test_f4_zero():\n    assert f4(0) == 0\n")


def _lib(fixed):
    return "".join("def %s(x):\n    return %s\n\n\n" % (f, FIXED[f] if f in fixed else BUGGY[f]) for f in FUNCS)


def _tests_text(fixed):
    return BASE_TESTS + "".join("\n\ndef test_%s_fixed():\n    assert %s == %d\n" % (f, CHECK[f][0], CHECK[f][1])
                                for f in FUNCS if f in fixed)


def _toy_upstream(path):
    """A git repository with an initial commit and five fix commits, each fixing one function and
    adding its test. Returns the repository path and, per case, (parent, fix) commit ids."""
    os.makedirs(path)
    _git(path, "init", "-q", "-b", "main")
    _write(os.path.join(path, "src", "humanize", "__init__.py"), "")
    pairs, fixed = [], []

    def commit(message):
        _write(os.path.join(path, "src", "humanize", "lib.py"), _lib(fixed))
        _write(os.path.join(path, "tests", "test_lib.py"), _tests_text(fixed))
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", message)
        return _git(path, "rev-parse", "HEAD").strip()

    parent = commit("initial")
    for f in FUNCS:
        fixed.append(f)
        fix = commit("fix %s" % f)
        pairs.append((parent, fix))
        parent = fix
    return path, pairs


def _toy_cases(upstream, pairs, bases=None):
    pack_cases = _cases()["cases"]
    cases = []
    for n, (f, (parent, fix)) in enumerate(zip(FUNCS, pairs)):
        real = pack_cases[n]
        cases.append({"id": real["id"], "pr": 1, "issue": None, "fix": fix, "parent": parent,
                      "base": (bases or {}).get(real["id"]), "test_files": ["tests/test_lib.py"],
                      "fix_files": ["src/humanize/lib.py"], "suspect_files": ["src/humanize/lib.py"],
                      "covers": real["covers"], "source": real["source"],
                      "fail_to_pass": ["tests/test_lib.py::test_%s_fixed" % f]})
    return {"upstream": {"name": "toy", "url": upstream, "license": "MIT", "pin": pairs[-1][1]},
            "stub": _cases()["stub"], "cases": cases}


def _bases(repo_path, upstream, cases_file, *flags):
    return setup_cases.main(["--cases", cases_file, "bases", "--repo", repo_path, "--upstream", upstream] + list(flags))


def _toy_world(tmp_path, capsys):
    """(upstream path, the cases file with every base filled in, the repository the bases live in)."""
    upstream, pairs = _toy_upstream(str(tmp_path / "upstream"))
    cases_file = str(tmp_path / "cases.json")
    _write(cases_file, json.dumps(_toy_cases(upstream, pairs)))
    work = str(tmp_path / "work")
    os.makedirs(work)
    _git(work, "init", "-q", "-b", "main")
    capsys.readouterr()
    assert _bases(work, upstream, cases_file, "--refs") == 0
    built = dict(line.split() for line in capsys.readouterr().out.splitlines())
    data = _toy_cases(upstream, pairs, built)
    _write(cases_file, json.dumps(data))
    return upstream, cases_file, work, data


def test_the_base_commit_is_the_fix_parent_with_the_fix_tests_and_no_fix_code(tmp_path, capsys):
    upstream, cases_file, work, data = _toy_world(tmp_path, capsys)
    for case in data["cases"]:
        base = case["base"]
        assert _git(work, "rev-list", "--parents", "-n", "1", base).split() == [base]       # a root commit
        assert _git(work, "log", "-1", "--format=%an %ae %aI", base).strip() == "refpack refpack@localhost 2026-01-01T00:00:00Z"
        lib = _git(work, "show", "%s:src/humanize/lib.py" % base)
        assert lib == _git(upstream, "show", "%s:src/humanize/lib.py" % case["parent"])    # the code before the fix
        tests = _git(work, "show", "%s:tests/test_lib.py" % base)
        assert tests == _git(upstream, "show", "%s:tests/test_lib.py" % case["fix"])        # the tests of the fix
        assert _git(work, "show", "%s:packs/refpack/BASE-STUB" % base).startswith("placeholder:")
        assert _git(work, "rev-parse", "refs/refpack/%s" % case["id"]).strip() == base
        assert _git(work, "rev-parse", "refs/refpack/upstream/%s" % case["id"]).strip() == case["fix"]
        names = _git(work, "ls-tree", "-r", "--name-only", base).split()
        assert sorted(names) == ["packs/refpack/BASE-STUB", "src/humanize/__init__.py", "src/humanize/lib.py",
                                 "tests/test_lib.py"]
    # the ids are the same when built again in another repository
    other = str(tmp_path / "other")
    os.makedirs(other)
    _git(other, "init", "-q", "-b", "main")
    assert _bases(other, upstream, cases_file, "--check") == 0
    # and a case file that names another id is a mismatch, exit 1
    wrong = json.loads(_read(cases_file))
    wrong["cases"][0]["base"] = "0" * 40
    _write(str(tmp_path / "wrong.json"), json.dumps(wrong))
    assert _bases(other, upstream, str(tmp_path / "wrong.json"), "--check") == 1


def test_the_base_builder_refuses_a_case_whose_parent_is_not_the_fix_parent(tmp_path, capsys):
    upstream, cases_file, work, data = _toy_world(tmp_path, capsys)
    bad = json.loads(_read(cases_file))
    bad["cases"][1]["parent"] = bad["cases"][3]["parent"]
    _write(str(tmp_path / "bad.json"), json.dumps(bad))
    assert _bases(work, upstream, str(tmp_path / "bad.json")) == 1


def test_the_store_folder_holds_a_reproducer_the_engine_can_freeze(tmp_path, capsys):
    root = str(tmp_path / "proj")
    os.makedirs(root)
    _, cases_file, _work, data = _toy_world(tmp_path, capsys)
    assert setup_cases.main(["--cases", cases_file, "store", "--project", root, "--deps", "/opt/deps"]) == 0
    for case in data["cases"]:
        folder = os.path.join(root, "contracts", "research", "refpack-repair", case["id"])
        assert sorted(os.listdir(folder)) == ["fix-terms.txt", "run.sh", "tests.txt"]
        run_sh = _read(folder, "run.sh")
        assert os.access(os.path.join(folder, "run.sh"), os.X_OK) and run_sh.startswith("#!/bin/sh")
        assert '"$CONTRACT_DIR/packs/refpack/repair/run_pytest.py"' in run_sh and "--deps /opt/deps" in run_sh
        assert '--select-file "$REPRODUCER_DIR/tests.txt"' in run_sh
        assert _read(folder, "tests.txt").split("\n")[:-1] == case["fail_to_pass"]
        assert _read(folder, "fix-terms.txt").split() == [case["fix"], case["fix"][:12]]


def _verify(repo_path, cases_file, logdir, *extra, deps):
    return verify_cases.main(["--repo", repo_path, "--deps", deps, "--logdir", logdir, "--cases", cases_file,
                              "--min-tests", "3", "--repeat", "2"] + list(extra))


def test_verify_cases_shows_base_fail_and_fix_pass(tmp_path, capsys):
    """The done bar: every case fails its reproducing test on the base and passes it on the fix, the
    old tests keep passing, in every repeat; each run is saved as a log with the commit it ran on."""
    _upstream, cases_file, work, data = _toy_world(tmp_path, capsys)
    empty = tmp_path / "nodeps"
    empty.mkdir()
    logs = str(tmp_path / "logs")
    capsys.readouterr()
    assert _verify(work, cases_file, logs, deps=str(empty)) == 0
    out = capsys.readouterr().out
    assert out.count(" OK ") == 5 and "NOT-OK" not in out
    summary = json.loads(_read(logs, "summary.json"))
    for res in summary["results"]:
        assert res["ok"] and len(res["repeats"]) == 2
        for rep in res["repeats"]:
            assert set(rep["base_f2p"].values()) == {"FAIL"} and set(rep["fix_f2p"].values()) == {"PASS"}
            assert rep["lost"] == [] and rep["baseline_passed"] >= 3
    case = data["cases"][2]
    base_log = _read(logs, "%s-base-fail-to-pass-r1.log" % case["id"])
    assert "# commit: %s" % case["base"] in base_log and "test_f3_fixed ... FAIL" in base_log
    assert "test_f3_fixed ... PASS" in _read(logs, "%s-fix-fail-to-pass-r1.log" % case["id"])


def test_verify_cases_refuses_a_reproducer_that_passes_on_the_base_and_a_fix_that_loses_a_test(tmp_path, capsys):
    upstream, cases_file, work, data = _toy_world(tmp_path, capsys)
    empty = tmp_path / "nodeps"
    empty.mkdir()
    # a reproducing test that already passes on the base is weak: base FAIL is not shown
    weak = json.loads(_read(cases_file))
    weak["cases"][0]["fail_to_pass"] = ["tests/test_lib.py::test_callable"]
    _write(str(tmp_path / "weak.json"), json.dumps(weak))
    assert _verify(work, str(tmp_path / "weak.json"), str(tmp_path / "l1"), "--case", weak["cases"][0]["id"],
                   deps=str(empty)) == 1
    res = json.loads(_read(str(tmp_path / "l1"), "summary.json"))["results"][0]
    assert res["repeats"][0]["base_f2p"] == {"tests/test_lib.py::test_callable": "PASS"} and res["ok"] is False
    # a test the upstream fix never added is missing on the fix: not a pass
    gone = json.loads(_read(cases_file))
    gone["cases"][1]["fail_to_pass"] = ["tests/test_lib.py::test_nothing"]
    _write(str(tmp_path / "gone.json"), json.dumps(gone))
    assert _verify(work, str(tmp_path / "gone.json"), str(tmp_path / "l2"), "--case", gone["cases"][1]["id"],
                   deps=str(empty)) == 1
    # fix_files that miss part of the upstream change are a data error, reported
    short = json.loads(_read(cases_file))
    short["cases"][2]["fix_files"] = ["src/humanize/__init__.py"]
    _write(str(tmp_path / "short.json"), json.dumps(short))
    capsys.readouterr()
    assert _verify(work, str(tmp_path / "short.json"), str(tmp_path / "l3"), "--case", short["cases"][2]["id"],
                   deps=str(empty)) == 1
    assert "differ from the commit's src changes" in capsys.readouterr().out


# ----------------------------------------------------------------- the engine, repair (toy history)
@needs_jail
@needs_deps
def test_the_engine_proves_and_gates_a_repair_case(repo, tmp_path, capsys, monkeypatch):
    """The pack's repair side through the engine's own steps and gates, on the made-up history: the
    store reproducer is shown failing on the base live, the base pass set is measured, a no-op
    attempt is refused at fail-to-pass, the upstream fix passes fail-to-pass and pass-to-pass, and a
    fix that loses an old test is refused at pass-to-pass. Every item ran in an offline jail."""
    from alpaca.research import repair, runner
    from alpaca.research.types import PassToPass, RunContext
    from alpaca.tests import research_loopkit as kit
    upstream, cases_file, work, data = _toy_world(tmp_path, capsys)
    # the project holds the pack with the toy case data: the runbook is rendered from it
    _write(os.path.join(repo, "packs", "refpack", "repair", "hidden", "cases.json"), json.dumps(data))
    _write(os.path.join(repo, REPAIR), render_repair.render(data["cases"], min_tests=3))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "toy cases")
    # the same two setup steps an owner runs: the base commits into the project's git store, the store folders
    assert _bases(repo, upstream, cases_file, "--check") == 0
    assert setup_cases.main(["--cases", cases_file, "store", "--project", repo]) == 0
    monkeypatch.setattr(runner, "_TMP_PREFIXES", ())
    got = kit.start(repo, runbook=REPAIR)
    conn = db.connect(repo)
    frozen = contract.load_frozen(conn, repo, got["run"])
    ctx = RunContext(root=repo, run=got["run"], op=got["op"], session=events.runner_session(got["run"]),
                     contract=frozen, runtime="control", tmp_prefixes=())
    task = got["repair_tasks"][0]
    plan = dict((s["name"], s["run"]) for s in repair.repair_plan(ctx, task)["steps"])
    assert [plan[n](db.connect(repo)) for n in ("claim", "repo", "prepare", "reproducer", "base-measure")] == [vc.PASS] * 5

    def latest(kind):
        row = conn.execute("SELECT data FROM events WHERE kind=? AND ref=? ORDER BY id DESC", (kind, task)).fetchone()
        return json.loads(row["data"])

    proof = latest(events.REPRODUCER)
    case = data["cases"][0]
    assert proof["proved"] is True and proof["tests"] == case["fail_to_pass"]
    base_gate = json.loads(conn.execute("SELECT data FROM events WHERE id=?", (proof["base_fail_event"],)).fetchone()["data"])
    assert base_gate["tests"] == {case["fail_to_pass"][0]: "FAIL"} and base_gate["offline"] is True
    baseline = latest(events.BASELINE)
    assert baseline["ok"] is True and "tests/test_lib.py::test_f2_zero" in baseline["pass_to_pass"]["base_pass"]
    assert case["fail_to_pass"][0] in baseline["left_out"]
    ctx.clone = repair._clone(ctx, task)
    commit = repair.case_base(conn, ctx, task)
    base = isolation.fresh_tree(repo, ctx.clone, commit=commit, path=os.path.join(repo, ".alpaca", "trees", "base"))
    attempt = isolation.fresh_tree(repo, ctx.clone, commit=commit, path=os.path.join(repo, ".alpaca", "trees", "attempt"))
    start = PassToPass(baseline=(), base_pass=tuple(baseline["pass_to_pass"]["base_pass"]), attempt_pass=(),
                       lost=(), ok=True)

    def chain():
        return gates.run_chain(conn, repo, run=got["run"], task=task, contract=frozen, wt=attempt, mode="repair",
                               session="test", repair_case=case["id"], base=base, baseline=start)

    lib = os.path.join(attempt.path, "src", "humanize", "lib.py")
    noop = chain()
    assert noop.verdict == vc.FAIL and noop.first_failure == "fail-to-pass:" + case["id"]
    assert noop.fail_to_pass.ok is False
    fixed = _git(upstream, "show", "%s:src/humanize/lib.py" % case["fix"])
    _write(lib, fixed)
    good = chain()
    assert good.verdict == vc.PASS and good.fail_to_pass.ok and good.pass_to_pass.ok
    # the gates wrote nothing into the attempt tree: no ignored output the post-eval check would flag
    assert isolation.post_eval(ctx.clone, attempt, head=commit, keep=(), eval_outputs=(), before=()).new == ()
    assert set(good.fail_to_pass.base.values()) == {"FAIL"} and set(good.fail_to_pass.attempt.values()) == {"PASS"}
    assert good.pass_to_pass.lost == ()
    _write(lib, fixed + "\n_old = f2\n\n\ndef f2(x):\n    return 1 if x == 0 else _old(x)\n")
    loses = chain()
    assert loses.verdict == vc.FAIL and loses.first_failure == "pass-to-pass"
    assert loses.pass_to_pass.lost == ("tests/test_lib.py::test_f2_zero",)
    assert set(loses.fail_to_pass.attempt.values()) == {"PASS"}
    gate_events = [e["data"] for e in db.events(conn, kind=events.GATE, limit=10 ** 9)]
    assert gate_events and all(e["offline"] is True for e in gate_events if e.get("ran"))


# ------------------------------------------------------------------- the real cases, when reachable
UPSTREAM = os.environ.get("REFPACK_UPSTREAM")


@pytest.mark.skipif(not UPSTREAM, reason="REFPACK_UPSTREAM does not name python-humanize (a clone or its URL)")
@needs_deps
def test_the_real_cases_fail_on_the_base_and_pass_on_the_fix(tmp_path):
    """The five real bug fixes: the base commits are built from the pinned upstream commits and equal
    the ids in the runbook, and every case shows base FAIL and fix PASS with the old suite intact,
    three times over."""
    work = str(tmp_path / "work")
    os.makedirs(work)
    _git(work, "init", "-q", "-b", "main")
    assert setup_cases.main(["bases", "--repo", work, "--upstream", UPSTREAM, "--check"]) == 0
    logs = str(tmp_path / "logs")
    assert verify_cases.main(["--repo", work, "--deps", DEPS, "--logdir", logs, "--repeat", "3"]) == 0
    summary = json.loads(_read(logs, "summary.json"))
    assert [r["case"] for r in summary["results"]] == [c["id"] for c in _cases()["cases"]]
    assert all(r["ok"] and len(r["repeats"]) == 3 for r in summary["results"])
    counts = [r["repeats"][0]["baseline_passed"] for r in summary["results"]]
    assert min(counts) >= 500


# ------------------------------------------------------------------- the held-out seed is not in the clone
#: the paths an attempt's clone does not carry (isolation._clone_policy): the held-out files and the record
_STRIPPED = ("packs/refpack/holdout/", ".alpaca/", "research-refs/")
#: the legacy sentence of the frozen generator's docstring. Dropping it is an edit of a pack file that
#: needs the owner's loosen grant, so it lands last (t-351 C8); until then it alone is not a hit, and
#: any other line of that file is. Once it lands this entry matches nothing.
_PENDING_PACK_EDIT = {"packs/refpack/bench/corpus_gen.py": r"hold" + r"out/corpus \(seed %d\), cut by the same"}


def test_no_agent_readable_file_ties_the_holdout_to_a_seed():
    """rr-021 t-346 (F49): an attempt read the held-out seed in the generator's docstring and in this
    test file, both in its clone, and regenerated the held-out corpus with it. No tracked file an
    attempt can read puts the held-out seed number beside the held-out corpus: not as `seed N`,
    `generate(N)` or `--seed N` within two lines of the word held-out, and not as a row that pairs N
    with the held-out folder. The seed is found by search (_holdout_seed), so this file names none."""
    seed = _holdout_seed()
    number = r"(?<![\w.])%d(?![\w.])" % seed
    seedish = re.compile(r"seed\W{0,3}%d\b|generate\(\s*%d\s*\)|--seed[= ]0*%d\b" % (seed, seed, seed))
    pair = re.compile(r"\(\s*%d\s*,\s*[\"'][^\"']*hold" % seed)
    topic = re.compile(r"hold" + r"out|hold" + r"-?\s?" + r"out|held[- ]out|generate\(")
    out = subprocess.run(["git", "-C", REPO, "ls-files", "-z"], capture_output=True, check=True)
    hits = []
    for rel in out.stdout.decode("utf-8", "replace").split("\0"):
        if not rel or rel.startswith(_STRIPPED):
            continue
        path = os.path.join(REPO, rel)
        try:
            if os.path.getsize(path) > 1 << 20:
                continue
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        pending = _PENDING_PACK_EDIT.get(rel)
        for i, line in enumerate(lines):
            if not re.search(number, line):
                continue
            low = line.lower()
            window = " ".join(lines[max(0, i - 2):i + 3]).lower()
            if (seedish.search(low) and topic.search(window)) or pair.search(low):
                if pending and re.search(pending % seed, line):
                    continue
                hits.append("%s:%d" % (rel, i + 1))
    assert not hits, "agent-readable files tie the held-out seed to the held-out corpus: %s" % hits
