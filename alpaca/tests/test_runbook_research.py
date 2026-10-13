"""Runbook format 3, the research format: what t-096 adds to `alpaca runbook check`
(design/research-engine/runbook-format-3.md sections 2, 4, 6.1, 7.2, 10 to 12 and 14.1; t-097
completes this file with the coverage codes, the bar parts and the docs section).

Covered here: the version gate rows of section 2.1 and SINCE_3; the checker's two format 2
compatibility tests of section 2.3; the worked examples of sections 10 and 11 pass with the check
output the design prints; one test per code of section 6.1 that t-096 adds (all but COVERS-NONE),
each asserting the code and its location; the invalid variants of section 12, one error each; the
layers, merge rule and conditional defaults of section 7.2; path_admitted; path_hit against
barrier.path_is_protected; research_fields; a placeholder in a text, number, list, mapping and
word field; and the partner kit schema's `research` conditions. Every test has a positive and a
negative side. No format 3 test lives in test_runbook.py, whose calls the kit replay requires to
reach exactly ERROR_CODES (section 1, decision 2).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest
import yaml

from alpaca import barrier, runbook, runbook_kit
from alpaca.tests.conftest import REPO
from alpaca.tests.devfiles import needs_dev_files

FIX = os.path.join(REPO, "alpaca", "tests", "fixtures", "research", "sortbench")
BASELINE = os.path.join(REPO, "alpaca", "tests", "fixtures", "research", "format2-baseline.json")


# ------------------------------------------------------------------------------ helpers
@pytest.fixture
def pack(tmp_path):
    """The worked example at <tmp>/repo/packs/sortbench, under a folder that holds project.yaml,
    so the checker resolves repo.path (".") to <tmp>/repo (section 2.2)."""
    root = tmp_path / "repo"
    shutil.copytree(FIX, str(root / "packs" / "sortbench"))
    (root / "project.yaml").write_text("name: sortbench-test\n", encoding="utf-8")
    return str(root / "packs" / "sortbench")


def _edit(folder, name, *pairs):
    path = os.path.join(folder, name)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    for old, new in pairs:
        assert old in text, (name, old)
        text = text.replace(old, new, 1)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _check(folder, name="improve.yaml", check_files=True):
    spec = name.replace(".yaml", "-spec.md")
    return runbook.check(os.path.join(folder, name), spec_path=os.path.join(folder, spec), check_files=check_files)


def _errors(result):
    return [(e["code"], e["where"]) for e in result["errors"]]


def _one(result, code, where):
    """The result has exactly one error, with this code and location, and its message names the
    field (section 12: the tests assert the code, the location and the field)."""
    assert _errors(result) == [(code, where)], result["errors"]
    field = where.rsplit(".", 1)[-1].split("[")[0].split(":")[-1]
    assert field in result["errors"][0]["message"] or code in ("FIELD-UNFILLED", "BASE-NOT-COMMIT", "HOST-INVALID",
                                                               "TOOL-INVALID", "STAGE-UNUSED", "GATES-ORDER",
                                                               "THEN-ASK-OWNER", "KNOB-MOVES-BAR", "STAGE-UNKNOWN",
                                                               "PATHS-RULE-INVALID", "PATHS-OVERLAP", "STAGE-ROLE",
                                                               "TARGET-SOURCE"), result["errors"][0]


def _minimal(runbook_value=2, **extra):
    data = {"runbook": runbook_value, "id": "tiny", "title": "Tiny",
            "stages": [{"id": "build", "run": "make", "checks": [{"id": "build-exit", "type": "exit-code"}]}]}
    data.update(extra)
    return data


def _write(tmp_path, data, name="runbook.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return str(path)


# ------------------------------------------------------------------- the version (section 2.1)
def test_the_constants_keep_format_2_the_plain_format_and_add_format_3():
    assert runbook.FORMAT == 2 and runbook.FORMATS == (1, 2)
    assert runbook.RESEARCH_FORMAT == 3 and runbook.READ_FORMATS == (1, 2, 3)
    assert runbook.SINCE_3 == {"top": ("research",)} and runbook.TOP_KEYS["research"] is False
    assert set(runbook.RESEARCH_ERROR_CODES).isdisjoint(runbook.ERROR_CODES)
    assert len(runbook.RESEARCH_ERROR_CODES) == 21 and runbook.RESEARCH_ERROR_CODES[-1] == "COVERS-NONE"


def test_runbook_3_with_a_research_key_is_read_as_format_3(pack):
    data, _report = runbook.load(os.path.join(pack, "improve.yaml"))
    assert runbook.runbook_format(data) == 3
    assert runbook.research_block(data) is data["research"]
    assert runbook.runbook_format(dict(data, runbook=True)) == 1       # true/false is never format 3
    del data["research"]
    assert runbook.runbook_format(data) == 2 and runbook.research_block(data) is None


def test_runbook_3_without_a_research_block_is_version_unsupported(tmp_path):
    result = runbook.check(_write(tmp_path, _minimal(3)))
    assert _errors(result) == [("VERSION-UNSUPPORTED", "runbook")]
    message = result["errors"][0]["message"]
    assert "runbook: 2" in message and "research" in message
    assert runbook.check(_write(tmp_path, _minimal(2)))["errors"] == []


def test_any_other_version_is_unsupported_and_names_the_research_format(tmp_path):
    result = runbook.check(_write(tmp_path, _minimal(7)))
    assert _errors(result) == [("VERSION-UNSUPPORTED", "runbook")]
    assert "1, 2" in result["errors"][0]["message"] and "3 for a research runbook" in result["errors"][0]["message"]


@pytest.mark.parametrize("value", [1, 2])
def test_research_key_in_format_2_is_unknown_as_before(tmp_path, value):
    # the message runbook.py gives at f5869e9 for the same file (scratch worktree of f5869e9,
    # recorded in .alpaca/proofs/op-009/t-096.logs/)
    before = ("`research` is not a field here; allowed: description, id, knobs, owner, runbook, spec, stages, "
              "title")
    if value == 2:
        # t-619: a format 2 file also has the ship fields `kind` and `rollback`; format 1 has none
        before = ("`research` is not a field here; allowed: description, id, kind, knobs, owner, rollback, "
                  "runbook, spec, stages, title")
    data = _minimal(value, research={"mode": "improve"})
    result = runbook.check(_write(tmp_path, data), check_files=False)
    assert [e for e in result["errors"] if e["where"] == "research"] == [
        {"code": "FIELD-UNKNOWN", "where": "research", "message": before}]
    assert _errors(result) == [("FIELD-UNKNOWN", "research")]


@needs_dev_files("docs/alpaca-bootstrap/evidence", "docs/runbook-v2/evidence/trial/05-runbook")
def test_format_2_files_check_as_before():
    """Every tracked *runbook*.yaml outside .alpaca/ at f5869e9 (14 files, none with a research
    key) checks as it did at f5869e9: the same verdict and the same errors and warnings, in order.
    The fixture was made by runbook.check at f5869e9, the checkout root written as <REPO>."""
    with open(BASELINE, encoding="utf-8") as fh:
        base = json.load(fh)
    assert len(base["files"]) == 14
    for rel, want in sorted(base["files"].items()):
        path = os.path.join(REPO, rel)
        assert os.path.isfile(path), rel
        got = runbook.check(path)
        text = json.dumps({"verdict": got["verdict"], "errors": got["errors"], "warnings": got["warnings"]})
        assert json.loads(text.replace(REPO, "<REPO>")) == want, rel
    # the fixture agrees with the saved summary of t-088 (verdicts and code sets)
    assert base["files"]["templates/runbook-example/runbook.yaml"] == {"verdict": "PASS", "errors": [], "warnings": []}
    assert [e["code"] for e in base["files"]["docs/runbook-v2/evidence/trial/05-runbook/runbook.yaml"]["errors"]] \
        == ["SPEC-MISSING"]


def test_research_variables_are_format_3_only(tmp_path):
    data = _minimal(2)
    data["stages"][0]["run"] = "make -C ${WORKTREE}"
    result = runbook.check(_write(tmp_path, data))
    assert _errors(result) == [("VAR-UNKNOWN", "stages[0].run")]
    assert "WORKTREE" not in result["errors"][0]["message"].split("(")[1]


# ------------------------------------------------------------- the worked examples (10, 11)
@pytest.mark.parametrize("name,lines", [
    ("improve.yaml", ["runbook: improve.yaml", "spec: improve-spec.md (spec-kit, 8 item(s), 7 required)",
                      "coverage: 7 of 7 required item(s) covered", "GATE alpaca-runbook-check: PASS"]),
    ("repair.yaml", ["runbook: repair.yaml", "spec: repair-spec.md (spec-kit, 5 item(s), 5 required)",
                     "coverage: 5 of 5 required item(s) covered", "GATE alpaca-runbook-check: PASS"]),
])
def test_the_worked_examples_pass_with_the_designs_output(name, lines):
    proc = subprocess.run([__import__("sys").executable, "-c",
                           "import sys; from alpaca import runbook; "
                           "sys.exit(runbook.run_check(sys.argv[1], sys.argv[2]))", name, name.replace(".yaml", "-spec.md")],
                          cwd=FIX, capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=REPO))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.splitlines() == lines


def test_the_worked_examples_pass_in_a_repository_with_their_files(pack):
    for name in ("improve.yaml", "repair.yaml"):
        result = _check(pack, name)
        assert result["verdict"] == "PASS" and result["errors"] == [] and result["warnings"] == [], result


def test_the_repair_example_covers_its_spec_by_cases_and_parts(pack):
    covered = _check(pack, "repair.yaml")["coverage"]["covered"]
    assert covered == {"EC-001": ["case:stable-keys"], "EC-002": ["research:paths"], "SC-001": ["case:stable-keys"],
                       "SC-002": ["case:none-key"], "SC-003": ["research:pass-to-pass"]}
    improve = _check(pack)["coverage"]["covered"]
    assert improve["SC-001"] == ["metric:compares"] and improve["SC-004"] == ["research:stop"]
    assert improve["EC-003"] == ["research:paths"] and improve["SC-003"] == ["unit-pass"]


# ------------------------------------------------------------- the invalid variants (section 12)
IMPROVE_VARIANTS = [
    ([("      margin: 4\n", "")], "FIELD-MISSING", "research.metrics[1].margin"),
    ([("      source: spec:SC-001\n", "      source: default\n")], "TARGET-SOURCE", "research.metrics[0].source"),
    ([("editable: [src/sortlib/]", "editable: [src/sortlib/, packs/sortbench/bench/]")], "PATHS-OVERLAP",
     "research.paths.editable[1]"),
    ([("rule: deficit-pareto", "rule: champion")], "ACCEPT-RULE-MISMATCH", "research.accept.rule"),
]
REPAIR_METRICS = """  metrics:
    - id: compares
      description: comparisons the sort makes on the fixed input set
      command: >-
        python3 ${CONTRACT_DIR}/packs/sortbench/bench/measure.py --src ${WORKTREE}/src
        --metric compares
      parser: markers
      unit: comparisons
      direction: lower
      kind: deterministic
      timeout: 300
      target: 18000
      artifact: one run of src/sortlib over the fixed inputs in bench/inputs
      covers: [SC-001, EC-002]
      source: spec:SC-001
  accept:
"""
REPAIR_VARIANTS = [
    ([("base: 0f4c2a9e1b7d3c5a8e6f0b2d4c6a8e0f1b3d5c7a", "base: main")], "BASE-NOT-COMMIT",
     "research.repair.cases[0].base"),
    ([("  accept:\n", REPAIR_METRICS)], "FIELD-UNKNOWN", "research.metrics"),
    ([("    timeout: 120\n", "")], "TIMEOUT-MISSING", "stages[0].timeout"),
]


@pytest.mark.parametrize("pairs,code,where", IMPROVE_VARIANTS)
def test_each_improve_variant_gives_its_one_error(pack, pairs, code, where):
    _edit(pack, "improve.yaml", *pairs)
    _one(_check(pack), code, where)


@pytest.mark.parametrize("pairs,code,where", REPAIR_VARIANTS)
def test_each_repair_variant_gives_its_one_error(pack, pairs, code, where):
    _edit(pack, "repair.yaml", *pairs)
    result = _check(pack, "repair.yaml")
    _one(result, code, where)
    if code == "FIELD-UNKNOWN":
        # the refused metrics key is not read further: no after_targets default, no coverage
        assert "metric:compares" not in json.dumps(result["coverage"])


def test_the_template_example_made_runbook_3_is_version_unsupported(tmp_path):
    shutil.copytree(os.path.join(REPO, "templates", "runbook-example"), str(tmp_path / "ex"))
    path = str(tmp_path / "ex" / "runbook.yaml")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.replace("runbook: 2", "runbook: 3", 1))
    assert _errors(runbook.check(path)) == [("VERSION-UNSUPPORTED", "runbook")]


# ------------------------------------------------------------- one test per code (section 6.1)
@pytest.mark.parametrize("name,where,old,new", [
    ("text", "research.metrics[0].unit", "unit: comparisons", "unit: <FILL:the unit>"),
    ("number", "research.stop.rounds", "rounds: 5", "rounds: <FILL:N>"),
    ("list", "research.paths.editable", "editable: [src/sortlib/]", "editable: <FILL:editable rules>"),
    ("list item", "research.paths.editable[0]", "editable: [src/sortlib/]", "editable: ['<FILL:a rule>']"),
    ("mapping", "research.accept", "  accept:\n    rule: deficit-pareto\n    after_targets: stop\n",
     "  accept: <FILL:the accept rule>\n"),
    ("word", "research.metrics[0].direction", "direction: lower\n      kind: deterministic",
     "direction: <FILL:lower or higher>\n      kind: deterministic"),
    ("stage", "stages[0].title", "title: the package compiles", "title: <FILL:what the stage shows>"),
    ("stage number", "stages[0].timeout", "    timeout: 120\n", "    timeout: <FILL:seconds>\n"),
])
def test_a_placeholder_gives_field_unfilled_and_no_other_code(pack, name, where, old, new):
    _edit(pack, "improve.yaml", (old, new))
    result = _check(pack)
    assert _errors(result) == [("FIELD-UNFILLED", where)], (name, result["errors"])
    _edit(pack, "improve.yaml", (new, old))
    assert _check(pack)["errors"] == []


def test_a_placeholder_in_the_defaults_file_points_at_the_file(pack):
    _edit(pack, "pack.yaml", ("usd: 40", "usd: <FILL:dollars>"))
    assert _errors(_check(pack)) == [("FIELD-UNFILLED", "pack.yaml:research.budget.usd")]


def test_value_unknown_names_the_words(pack):
    _edit(pack, "improve.yaml", ("direction: lower\n      kind: deterministic", "direction: sideways\n      kind: deterministic"))
    result = _check(pack)
    _one(result, "VALUE-UNKNOWN", "research.metrics[0].direction")
    assert "higher, lower" in result["errors"][0]["message"]


@pytest.mark.parametrize("change", ["missing", "no research", "not yaml"])
def test_defaults_unreadable(pack, change):
    if change == "missing":
        _edit(pack, "improve.yaml", ("defaults: pack.yaml", "defaults: absent.yaml"))
    elif change == "no research":
        _edit(pack, "pack.yaml", ("research:\n", "settings:\n"))
    else:
        _edit(pack, "pack.yaml", ("pack: sortbench\n", "pack: [sortbench\n"))
    result = _check(pack)
    assert ("DEFAULTS-UNREADABLE", "research.defaults") in _errors(result)


def test_target_source_accepts_spec_and_owner_and_refuses_the_rest(pack):
    _edit(pack, "improve.yaml", ("      source: spec:SC-001\n", "      source: owner:dec-1a2b3c\n"))
    assert _check(pack)["errors"] == []
    _edit(pack, "improve.yaml", ("      source: owner:dec-1a2b3c\n", "      source: interview:research-targets\n"))
    _one(_check(pack), "TARGET-SOURCE", "research.metrics[0].source")
    _edit(pack, "improve.yaml", ("      source: interview:research-targets\n", ""))
    _one(_check(pack), "TARGET-SOURCE", "research.metrics[0].source")


def test_accept_rule_mismatch_for_after_targets_without_a_target(pack):
    _edit(pack, "repair.yaml", ("    rule: repair\n", "    rule: repair\n    after_targets: stop\n"))
    _one(_check(pack, "repair.yaml"), "ACCEPT-RULE-MISMATCH", "research.accept.after_targets")


@pytest.mark.parametrize("old,new,where", [
    ("editable: [src/sortlib/]", "editable: [../outside/]", "research.paths.editable[0]"),
    ("editable: [src/sortlib/]", "editable: [./]", "research.paths.editable[0]"),
    ("editable: [src/sortlib/]", "editable: ['src/**/x.py']", "research.paths.editable[0]"),
    ("editable: [src/sortlib/]", "editable: [src/sortlib/]\n    read: ['~/data']", "research.paths.read[0]"),
    ("editable: [src/sortlib/]", "editable: [src/sortlib/]\n    tests: ['/abs/tests/']", "research.paths.tests[0]"),
    ("  mode: improve\n", "  mode: improve\n  repo: {path: '${ROOT}'}\n", "research.repo.path"),
])
def test_paths_rule_invalid(pack, old, new, where):
    _edit(pack, "improve.yaml", (old, new))
    _one(_check(pack), "PATHS-RULE-INVALID", where)


def test_paths_overlap_for_a_hidden_rule_and_a_holdout_rule(pack):
    _edit(pack, "improve.yaml", ("editable: [src/sortlib/]", "editable: [src/sortlib/]\n    hidden: [src/sortlib/secret/]"))
    _one(_check(pack), "PATHS-OVERLAP", "research.paths.editable[0]")


def test_paths_overlap_for_a_lessons_file_under_a_holdout_rule(pack):
    with open(os.path.join(pack, "holdout-notes.md"), "w", encoding="utf-8") as fh:
        fh.write("- held out\n")
    os.makedirs(os.path.join(pack, "holdout"))
    shutil.move(os.path.join(pack, "holdout-notes.md"), os.path.join(pack, "holdout", "notes.md"))
    _edit(pack, "pack.yaml", ("file: lessons.md", "file: holdout/notes.md"))
    result = _check(pack)
    _one(result, "PATHS-OVERLAP", "pack.yaml:research.lessons.file")
    assert "packs/sortbench/holdout/notes.md" in result["errors"][0]["message"]


def test_paths_overlap_for_a_bug_report_under_a_holdout_rule(pack):
    os.makedirs(os.path.join(pack, "holdout"))
    shutil.copy(os.path.join(pack, "bugs", "none-key.md"), os.path.join(pack, "holdout", "none-key.md"))
    _edit(pack, "repair.yaml", ("bug_report: bugs/none-key.md", "bug_report: holdout/none-key.md"),
          ("  coupled:\n", "  holdout:\n    paths: [packs/sortbench/holdout/]\n  coupled:\n"))
    _one(_check(pack, "repair.yaml"), "PATHS-OVERLAP", "research.repair.cases[1].bug_report")


def test_the_lessons_overlap_needs_a_resolvable_repo(tmp_path):
    # without a project.yaml above the pack, repo.path "." cannot be resolved, so the check skips it
    folder = str(tmp_path / "loose")
    shutil.copytree(FIX, folder)
    os.makedirs(os.path.join(folder, "holdout"))
    shutil.copy(os.path.join(folder, "lessons.md"), os.path.join(folder, "holdout", "notes.md"))
    _edit(folder, "pack.yaml", ("file: lessons.md", "file: holdout/notes.md"))
    _edit(folder, "improve.yaml", ("paths: [packs/sortbench/holdout/]", "paths: [holdout/]"))
    assert "PATHS-OVERLAP" not in [c for c, _w in _errors(_check(folder))]


def test_tool_invalid_points_into_the_defaults_file(pack):
    _edit(pack, "pack.yaml", ("tools: [python3]", "tools: [python3, /usr/bin/make]"))
    _one(_check(pack), "TOOL-INVALID", "pack.yaml:research.fingerprint.tools[1]")


def test_host_invalid(pack):
    _edit(pack, "improve.yaml", ("  mode: improve\n", "  mode: improve\n  egress: {allow: [pypi.org, 'https://x.org']}\n"))
    _one(_check(pack), "HOST-INVALID", "research.egress.allow[1]")


def test_stage_unknown(pack):
    _edit(pack, "improve.yaml", ("    - {stage: unit}\n", "    - {stage: unit}\n    - {stage: lint}\n"))
    _one(_check(pack), "STAGE-UNKNOWN", "research.gates[2].stage")


@pytest.mark.parametrize("old,new,where", [
    ("stages: [holdout-bench]", "stages: [holdout-bench, build]", "research.holdout.stages[1]"),
    ("syntax: build", "syntax: holdout-bench", "research.controls.syntax"),
])
def test_stage_role(pack, old, new, where):
    _edit(pack, "improve.yaml", (old, new))
    _one(_check(pack), "STAGE-ROLE", where)


def test_stage_role_for_an_owner_gate_on_a_stage_with_run(pack):
    _edit(pack, "improve.yaml", ("    checks:\n      - {id: build-exit, type: exit-code}\n",
                                 "    checks:\n      - {id: build-exit, type: exit-code}\n"
                                 "    owner_gate: {approve: the owner reads the build log}\n"))
    _one(_check(pack), "STAGE-ROLE", "stages[0].owner_gate")


def test_stage_unused(pack):
    _edit(pack, "improve.yaml", ("\nresearch:\n", "  - id: lint\n    run: python3 -m pyflakes src\n    timeout: 60\n"
                                                  "    checks:\n      - {id: lint-exit, type: exit-code}\n\nresearch:\n"))
    _one(_check(pack), "STAGE-UNUSED", "stages[3]")


def test_gates_order_for_a_gate_and_for_the_prepare_stage(pack):
    _edit(pack, "improve.yaml", ("    - {stage: build}\n    - {stage: unit}\n", "    - {stage: unit}\n    - {stage: build}\n"))
    _one(_check(pack), "GATES-ORDER", "stages[1].needs[0]")
    _edit(pack, "repair.yaml", ("  - id: prepare\n", "  - id: prepare\n    needs: [build]\n"))
    _one(_check(pack, "repair.yaml"), "GATES-ORDER", "stages[1].needs")


def test_then_ask_owner(pack):
    _edit(pack, "improve.yaml", ("      - {id: build-exit, type: exit-code}\n",
                                 "      - {id: build-exit, type: exit-code}\n    fails:\n      - id: disk-full\n"
                                 "        when: the disk is full\n        detect: {type: exit-code, expect: 28}\n"
                                 "        then: ask-owner\n"))
    _one(_check(pack), "THEN-ASK-OWNER", "stages[0].fails[0].then")


KNOB = "\nknobs:\n  - {id: LIMIT, description: the failures a unit run may have, type: int, default: 0%s}\n\nstages:\n"


def test_knob_moves_bar_for_a_gate_checks_value_and_not_for_its_run(pack):
    _edit(pack, "improve.yaml", ("\nstages:\n", KNOB % ""), ('value: 0\n', 'value: "${LIMIT}"\n'))
    _one(_check(pack), "KNOB-MOVES-BAR", "stages[1].checks[0].value")
    _edit(pack, "improve.yaml", ('value: "${LIMIT}"\n', 'value: 0\n'),
          ("--json ${EVIDENCE_DIR}/unit.json\n", "--json ${EVIDENCE_DIR}/unit.json --max-fail ${LIMIT}\n"))
    assert _check(pack)["errors"] == []


def test_knob_moves_bar_for_a_metric_command_and_an_owner_only_knob_passes(pack):
    _edit(pack, "improve.yaml", ("\nstages:\n", KNOB % ""), ("--metric compares\n", "--metric compares --max ${LIMIT}\n"))
    _one(_check(pack), "KNOB-MOVES-BAR", "research.metrics[0].command")
    _edit(pack, "improve.yaml", ("default: 0}", "default: 0, owner_only: true}"))
    assert _check(pack)["errors"] == []


def test_pack_outside_repo_when_repo_path_can_be_resolved(pack, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    _edit(pack, "improve.yaml", ("  mode: improve\n", "  mode: improve\n  repo: {path: %s}\n" % other))
    _one(_check(pack), "PACK-OUTSIDE-REPO", "research.repo.path")
    _edit(pack, "improve.yaml", ("path: %s}" % other, "path: .}"))
    assert _check(pack)["errors"] == []


def test_proposal_field_reserved(pack):
    _edit(pack, "improve.yaml", ("    - field: risk\n", "    - field: title\n"))
    _one(_check(pack), "PROPOSAL-FIELD-RESERVED", "research.proposal[0].field")


def test_file_missing_for_a_lessons_file_and_a_bug_report_and_no_files_skips_it(pack):
    _edit(pack, "pack.yaml", ("file: lessons.md", "file: absent.md"))
    _one(_check(pack), "FILE-MISSING", "pack.yaml:research.lessons.file")
    assert _check(pack, check_files=False)["errors"] == []
    _edit(pack, "repair.yaml", ("bug_report: bugs/none-key.md", "bug_report: bugs/absent.md"))
    errors = _errors(_check(pack, "repair.yaml"))
    assert ("FILE-MISSING", "research.repair.cases[1].bug_report") in errors


def test_base_not_commit(pack):
    _edit(pack, "repair.yaml", ("base: 0f4c2a9e1b7d3c5a8e6f0b2d4c6a8e0f1b3d5c7a", "base: 0f4c2a9"))
    _one(_check(pack, "repair.yaml"), "BASE-NOT-COMMIT", "research.repair.cases[0].base")


def test_covers_and_source_in_the_defaults_file_are_unknown(pack):
    _edit(pack, "pack.yaml", ("    wall: 14400\n", "    wall: 14400\n    covers: [SC-004]\n    source: default\n"))
    assert _errors(_check(pack)) == [("FIELD-UNKNOWN", "pack.yaml:research.budget.covers"),
                                     ("FIELD-UNKNOWN", "pack.yaml:research.budget.source")]


def test_mode_in_the_defaults_file_is_unknown_and_other_top_keys_too(pack):
    _edit(pack, "pack.yaml", ("research:\n", "owner: someone\nresearch:\n  mode: improve\n"))
    assert _errors(_check(pack)) == [("FIELD-UNKNOWN", "pack.yaml:owner"), ("FIELD-UNKNOWN", "pack.yaml:research.mode")]


def test_generic_codes_in_the_block(pack):
    _edit(pack, "improve.yaml", ("slots: 2", "slots: 40"), ("noop_rounds: 2", "noop_rounds: two"))
    assert _errors(_check(pack)) == [("RANGE", "research.stop.slots"), ("FIELD-TYPE", "research.controls.noop_rounds")]


def test_a_noisy_metric_needs_two_repeats_and_a_margin(pack):
    _edit(pack, "improve.yaml", ("repeats: 5", "repeats: 1"), ("min_ok: 3", "min_ok: 1"), ("margin: 4", "margin: 0"),
          ("seeds: [11, 12, 13, 14, 15]", "seeds: [11]"))
    wheres = _errors(_check(pack))
    assert ("RANGE", "research.metrics[1].repeats") in wheres and ("RANGE", "research.metrics[1].margin") in wheres
    assert ("RANGE", "research.metrics[1].min_ok") in wheres


# ------------------------------------------------------ layers, merge and defaults (section 7.2)
def test_expand_research_gives_the_block_of_section_10_4_with_its_layers(pack):
    data, _report = runbook.load(os.path.join(pack, "improve.yaml"))
    block, layers, report = runbook.expand_research(data, pack)
    assert report.errors == []
    assert block["repo"] == {"path": ".", "ref": "HEAD"}
    assert block["metrics"][0]["repeats"] == 1 and block["metrics"][0]["min_ok"] == 1
    assert block["metrics"][0]["margin"] == 0 and block["metrics"][0]["median"] == "worse"
    assert block["paths"]["protected"] == ["packs/sortbench/", "pyproject.toml"] and block["paths"]["hidden"] == []
    assert block["gates"] == [{"stage": "build", "heavy": False, "lock_timeout": 14400},
                              {"stage": "unit", "heavy": False, "lock_timeout": 14400}]
    assert block["lessons"] == {"file": "lessons.md", "note_chars": 300}
    assert block["runtime"] == {"timeouts": {"proposer": 900, "implementer": 1800, "notes": 240, "audit": 900,
                                             "reproducer": 1800, "reviewer": 900}, "effort": {}, "retries": 2}
    assert block["verifier"] == {"timeout": 1800, "rerun_timeout": 1200}
    assert block["egress"] == {"allow": []} and block["coupled"] == []
    assert block["proposal"][0]["required"] is True and "max" not in block["proposal"][0]
    assert layers["research.budget.usd"] == "defaults" and layers["research.paths.editable"] == "runbook"
    assert layers["research.paths.hidden"] == "engine" and layers["research.metrics[compares].repeats"] == "engine"
    assert layers["research.runtime.timeouts.proposer"] == "defaults"
    assert layers["research.runtime.timeouts.notes"] == "engine"
    # a file that is not format 3 has no block
    assert runbook.expand_research({"runbook": 2}, pack)[0] is None


def test_the_repair_block_has_no_after_targets(pack):
    data, _report = runbook.load(os.path.join(pack, "repair.yaml"))
    block, _layers, _report = runbook.expand_research(data, pack)
    assert block["accept"] == {"rule": "repair"}
    case = block["repair"]["cases"][1]
    assert case["fail_to_pass"] == {"origin": "generate", "timeout": 300, "repeats": 2,
                                    "command": "sh ${REPRODUCER_DIR}/run.sh"}
    assert "base" not in case


def test_merge_research_rules():
    base = {"stop": {"rounds": 5, "slots": 2}, "paths": {"editable": ["a/", "b/"]},
            "metrics": [{"id": "m", "target": 1, "kind": "noisy"}, {"id": "n", "unit": "s"}],
            "proposal": [{"field": "risk", "type": "enum"}]}
    over = {"stop": {"slots": 3}, "paths": {"editable": ["a/"]},
            "metrics": [{"id": "n", "unit": "ms"}, {"id": "o", "unit": "x"}],
            "proposal": [{"field": "effort", "type": "int"}], "disk": 4}
    frozen = json.dumps(base, sort_keys=True)
    out = runbook.merge_research(base, over)
    assert json.dumps(base, sort_keys=True) == frozen                     # pure
    assert out["stop"] == {"rounds": 5, "slots": 3}                       # a mapping merges key by key
    assert out["paths"]["editable"] == ["a/"]                             # any other list is replaced
    assert [m["id"] for m in out["metrics"]] == ["m", "n", "o"]           # keyed: merged, new appended
    assert out["metrics"][1] == {"id": "n", "unit": "ms"} and out["metrics"][0]["target"] == 1
    assert [p["field"] for p in out["proposal"]] == ["risk", "effort"] and out["disk"] == 4
    # no layer drops an item
    assert [m["id"] for m in runbook.merge_research(base, {"metrics": []})["metrics"]] == ["m", "n"]


def test_conditional_defaults_follow_each_merge():
    block = {"metrics": [{"id": "d", "kind": "deterministic", "target": 3}, {"id": "n", "kind": "noisy"}],
             "accept": {"rule": "deficit-pareto"},
             "proposal": [{"field": "a", "type": "str"}, {"field": "b", "type": "text"}, {"field": "c", "type": "paths"},
                          {"field": "d", "type": "int"}, {"field": "e", "type": "str", "max": 9}]}
    out = runbook._engine_fill(block)
    assert out["metrics"][0]["repeats"] == 1 and out["metrics"][0]["min_ok"] == 1 and out["metrics"][0]["margin"] == 0
    assert "repeats" not in out["metrics"][1] and out["accept"]["after_targets"] == "stop"
    assert [p.get("max") for p in out["proposal"]] == [200, 4000, 50, None, 9]
    # a task layer that raises a deterministic metric's repeats gets min_ok derived again
    raised = runbook._engine_fill(runbook.merge_research(block, {"metrics": [{"id": "d", "repeats": 10}]}))
    assert raised["metrics"][0]["min_ok"] == 10
    # with no target there is no after_targets
    assert "after_targets" not in runbook._engine_fill({"accept": {"rule": "repair"}})["accept"]


# ---------------------------------------------------------------------------- the matchers
def test_path_admitted_is_strict():
    assert runbook.path_admitted("src/sortlib/sort.py", ["src/sortlib/"]) == "src/sortlib/"
    assert runbook.path_admitted("src/sortlib", ["src/sortlib/"]) == "src/sortlib/"
    assert runbook.path_admitted("tests/src/sortlib/x.py", ["src/sortlib/"]) is None
    assert runbook.path_admitted("SRC/sortlib/x.py", ["src/sortlib/"]) is None
    assert runbook.path_admitted("src/a.py", ["src/*.py"]) == "src/*.py"
    assert runbook.path_admitted("src/sub/a.py", ["src/*.py"]) is None
    assert runbook.path_admitted("README.md", ["README.md"]) == "README.md"
    assert runbook.path_admitted("docs/README.md", ["README.md"]) is None
    assert runbook.path_admitted("./src/x.py", ["./src/"]) == "./src/"
    assert runbook.path_admitted("src/x1.py", ["src/x?.py"]) == "src/x?.py"
    assert runbook.path_admitted("src/x12.py", ["src/x?.py"]) is None
    assert runbook.path_admitted("anything", []) is None


RULE_SHAPES = ["packs/", "Packs/", "./packs/", "pack?/", "pa*/", "[p]acks/", "tests", "Tests", "conftest.py",
               "*.toml", "conf?est.py", "[ct]onftest.py", "packs/sortbench/", "packs/sort*/", "./packs/sortbench/",
               "PACKS/SORTBENCH/", "packs/sortbench/holdout/", "src/sortlib/sort.py", "src/*/sort.py",
               "src/sortlib/s?rt.py", "src/[a-z]ortlib/sort.py", "pyproject.toml", "./pyproject.toml",
               "packs/[!x]ortbench/"]
PATHS = ["packs/sortbench/improve.yaml", "PACKS/sortbench/x", "src/sortlib/sort.py", "a/packs/b.py",
         "sub/tests/test_x.py", "tests/test_sort.py", "conftest.py", "deep/dir/conftest.py", "pyproject.toml",
         "x/pyproject.toml", "packs/sortbench/holdout/inputs.json", "./src/sortlib/sort.py", "src/other/sort.py"]


def test_path_hit_answers_as_barrier_path_is_protected_on_every_rule_shape():
    for rule in RULE_SHAPES:
        for path in PATHS:
            assert runbook.path_hit(path, [rule]) == barrier.path_is_protected(path, [rule]), (rule, path)
    with open(os.path.join(FIX, "improve.yaml"), encoding="utf-8") as fh:
        rules = yaml.safe_load(fh)["research"]["holdout"]["paths"] + ["packs/sortbench/", "pyproject.toml"]
    for path in PATHS:
        assert runbook.path_hit(path, rules) == barrier.path_is_protected(path, rules), path
    # not vacuous: both hit and miss
    assert runbook.path_hit("deep/dir/conftest.py", ["conftest.py"]) == "conftest.py"
    assert runbook.path_hit("src/sortlib/sort.py", ["packs/"]) is None


# ---------------------------------------------------------------------------- research_fields
def test_research_fields_reports_the_required_flags_per_mode():
    fields = dict((f["path"], f["required"]) for f in runbook.research_fields())
    assert fields["research.mode"] == {"improve": True, "repair": True}
    assert fields["research.metrics"] == {"improve": True, "repair": False}
    assert fields["research.metrics[].id"] == {"improve": True, "repair": False}
    assert fields["research.metrics[].covers"] == {"improve": True, "repair": False}
    assert fields["research.metrics[].source"] == {"improve": False, "repair": False}
    assert fields["research.repair.cases[].covers"] == {"improve": False, "repair": True}
    assert fields["research.repair.cases[].fail_to_pass.origin"] == {"improve": False, "repair": True}
    # every budget cap is optional since t-297: a run with none is uncapped
    assert fields["research.budget"] == {"improve": False, "repair": False}
    assert fields["research.budget.usd"] == {"improve": False, "repair": False}
    assert fields["research.budget.tokens"] == {"improve": False, "repair": False}
    assert fields["research.budget.wall"] == {"improve": False, "repair": False}
    assert fields["research.categories"] == {"improve": True, "repair": False}
    assert fields["research.lessons.file"] == {"improve": False, "repair": False}
    assert fields["research.runtime.timeouts.audit"] == {"improve": False, "repair": False}
    assert fields["research.stop.covers"] == {"improve": False, "repair": False}
    assert len(fields) == len(runbook.research_fields())


# ------------------------------------------------------------------- the kit schema and the doc
def test_the_kit_schema_takes_format_3_with_a_block_and_a_spec_only():
    from alpaca.tests.test_runbook_kit import schema_errors
    schema = runbook_kit.schema()
    assert schema["properties"]["runbook"]["enum"] == [1, 2, 3]
    # t-107 (runbook-format-3.md section 13.3) gave the block its own definitions in place of a plain
    # object; a block that names a defaults file is checked for its shape only, so this one passes,
    # and a stage command of a research runbook has a timeout (TIMEOUT-MISSING), which it states too
    assert schema["properties"]["research"]["then"] == {"$ref": "#/$defs/research_layered"}
    base = {"runbook": 3, "id": "r", "title": "R", "spec": "spec.md",
            "research": {"mode": "improve", "defaults": "pack.yaml"},
            "stages": [{"id": "s", "run": "make", "timeout": 60, "checks": [{"id": "c", "type": "exit-code"}]}]}
    assert schema_errors(schema, base) == []
    for change in (lambda d: d.pop("spec"), lambda d: d.pop("research"), lambda d: d.update(research="text"),
                   lambda d: d.update(runbook=2), lambda d: d.update(runbook=1), lambda d: d.update(runbook=4)):
        data = json.loads(json.dumps(base))
        change(data)
        assert schema_errors(schema, data), data


def test_the_format_doc_names_the_research_block():
    with open(os.path.join(REPO, "docs", "runbook-format.md"), encoding="utf-8") as fh:
        doc = fh.read()
    row = [line for line in doc.splitlines() if line.startswith("| `research` |")]
    assert len(row) == 1 and "`runbook: 3`" in row[0]
    for word in runbook_kit.PRODUCT_ONLY:
        assert word not in row[0], word


def test_new_files_are_ascii():
    paths = [os.path.join(REPO, "alpaca", "runbook.py"), os.path.join(REPO, "alpaca", "research", "contract.py"),
             os.path.join(REPO, "alpaca", "research", "owner.py"), os.path.join(REPO, "alpaca", "taskcontract.py"),
             BASELINE, os.path.abspath(__file__)]
    for base, dirs, names in os.walk(FIX):
        dirs[:] = [d for d in dirs if d != "__pycache__"]       # bytecode an import leaves is no source
        paths += [os.path.join(base, n) for n in names if not n.endswith((".pyc", ".pyo"))]
    for path in paths:
        with open(path, "rb") as fh:
            data = fh.read()
        assert all(b < 128 for b in data), path


# =============================================================================== t-097
# Coverage with the research coverers (section 9), COVERS-NONE, the format 3 SPEC-UNCOVERED
# message, the research parts of bar_parts (section 14.3 point 1), the research note: sources,
# and the docs section "The research block".
EXAMPLE2 = os.path.join(REPO, "templates", "runbook-example")


def _loaded(folder, name="improve.yaml"):
    data, _report = runbook.load(os.path.join(folder, name))
    return data, runbook.expand_research(data, folder)[0]


def _spec(folder, name="improve.yaml"):
    return runbook.parse_spec(os.path.join(folder, name.replace(".yaml", "-spec.md")))


# ------------------------------------------------------------------------ COVERS-NONE
def test_covers_none_for_a_metric_without_covers(pack):
    _edit(pack, "improve.yaml", ("      covers: [SC-002]\n", ""))
    result = _check(pack)
    assert _errors(result) == [("SPEC-UNCOVERED", "SC-002"), ("COVERS-NONE", "research.metrics[1].covers")]
    message = result["errors"][1]["message"]
    assert "metric wall_ms covers no required spec item (it has no `covers`)" in message


def test_covers_none_for_a_metric_that_covers_only_a_functional_requirement(pack):
    # SC-002 moves to the other metric, so COVERS-NONE is the one error
    _edit(pack, "improve.yaml", ("covers: [SC-002]", "covers: [FR-001]"),
          ("covers: [SC-001, EC-002]", "covers: [SC-001, EC-002, SC-002]"))
    result = _check(pack)
    assert _errors(result) == [("COVERS-NONE", "research.metrics[1].covers")]
    assert "(it names only FR-001)" in result["errors"][0]["message"]
    # the same metric with a required item passes
    _edit(pack, "improve.yaml", ("covers: [FR-001]", "covers: [FR-001, SC-002]"))
    assert _check(pack)["errors"] == []


def test_covers_none_for_an_empty_covers_list(pack):
    _edit(pack, "improve.yaml", ("covers: [SC-002]", "covers: []"),
          ("covers: [SC-001, EC-002]", "covers: [SC-001, EC-002, SC-002]"))
    result = _check(pack)
    assert _errors(result) == [("COVERS-NONE", "research.metrics[1].covers")]
    assert "(its `covers` is empty)" in result["errors"][0]["message"]


def test_covers_none_for_a_repair_case(pack):
    _edit(pack, "repair.yaml", ("        covers: [SC-002]\n", ""),
          ("    covers: [EC-002]\n", "    covers: [EC-002, SC-002]\n"))
    result = _check(pack, "repair.yaml")
    assert _errors(result) == [("COVERS-NONE", "research.repair.cases[1].covers")]
    assert "repair case none-key" in result["errors"][0]["message"]


def test_covers_none_sees_a_metric_the_defaults_file_declares(pack):
    """The coverers are read from the expanded block, so a metric only pack.yaml declares, which
    can never carry covers there, is COVERS-NONE at its place in pack.yaml."""
    _edit(pack, "pack.yaml", ("research:\n", "research:\n  metrics:\n    - id: memory\n"
                                             "      command: python3 bench/measure.py --metric memory\n"
                                             "      parser: markers\n      unit: bytes\n      direction: lower\n"
                                             "      kind: deterministic\n      timeout: 60\n"))
    result = _check(pack)
    assert _errors(result) == [("COVERS-NONE", "pack.yaml:research.metrics[0].covers")]
    # without the block the defaults metric is invisible to coverage: the keyword is what finds it
    data, block = _loaded(pack)
    report = runbook._Report()
    runbook.coverage(data, _spec(pack), report, block=block)
    assert [e["code"] for e in report.errors] == ["COVERS-NONE"]
    bare = runbook._Report()
    runbook.coverage(data, _spec(pack), bare)
    assert [e["code"] for e in bare.errors] == ["SPEC-UNCOVERED"] * 5


def test_an_entry_that_names_nothing_is_covers_unknown_and_not_covers_none(pack):
    _edit(pack, "improve.yaml", ("covers: [SC-002]", "covers: [SC-099]"),
          ("covers: [SC-001, EC-002]", "covers: [SC-001, EC-002, SC-002]"))
    assert _errors(_check(pack)) == [("COVERS-UNKNOWN", "research.metrics[1].covers[0]")]


@pytest.mark.parametrize("new,where", [
    ("covers: ['<FILL:the items>']", "research.metrics[1].covers[0]"),
    ("covers: <FILL:the items>", "research.metrics[1].covers"),
])
def test_a_placeholder_covers_gives_field_unfilled_and_not_covers_none(pack, new, where):
    _edit(pack, "improve.yaml", ("covers: [SC-002]", new),
          ("covers: [SC-001, EC-002]", "covers: [SC-001, EC-002, SC-002]"))
    assert _errors(_check(pack)) == [("FIELD-UNFILLED", where)]


def test_a_placeholder_in_a_check_covers_names_nothing(pack):
    # section 4 rule 2 for a check's covers entry too: FIELD-UNFILLED, no COVERS-UNKNOWN
    _edit(pack, "improve.yaml", ("covers: [SC-003, FR-001]", "covers: [SC-003, '<FILL:another item>']"))
    result = _check(pack)
    assert _errors(result) == [("FIELD-UNFILLED", "stages[1].checks[0].covers[1]")]
    assert [w["code"] for w in result["warnings"]] == ["FR-UNCOVERED"]


def test_a_raw_placeholder_in_a_block_covers_nothing(pack):
    """A block the checker expanded holds a placeholder as an unread value; a caller may pass a
    block that still holds the raw text (section 14.1: intake passes the loader's). Either way
    the placeholder names no item (no COVERS-UNKNOWN) and gets no COVERS-NONE."""
    data, block = _loaded(pack)
    block["metrics"][1]["covers"] = ["<FILL:the items>"]
    block["metrics"][0]["covers"] = ["SC-001", "EC-002", "SC-002"]
    assert [c for c in runbook._covering(data, block) if c[1] == "metric:wall_ms"] == []
    report = runbook._Report()
    runbook.coverage(data, _spec(pack), report, block=block)
    assert report.errors == []
    block["metrics"][1]["covers"] = []
    runbook.coverage(data, _spec(pack), report, block=block)
    assert [(e["code"], e["where"]) for e in report.errors] == [("COVERS-NONE", "research.metrics[1].covers")]


def test_covers_none_needs_a_spec(pack):
    # without a spec there is no coverage, so no COVERS-NONE (a format 3 file names its spec, so
    # the check reads it; here the spec is taken away)
    _edit(pack, "improve.yaml", ("      covers: [SC-002]\n", ""))
    os.remove(os.path.join(pack, "improve-spec.md"))
    result = runbook.check(os.path.join(pack, "improve.yaml"))
    assert _errors(result) == [("SPEC-MISSING", "spec")]


# -------------------------------------------------------------- SPEC-UNCOVERED in format 3
def test_spec_uncovered_names_the_research_coverers_in_format_3(pack):
    _edit(pack, "improve.yaml", ("    covers: [SC-004]\n", ""))
    result = _check(pack)
    assert _errors(result) == [("SPEC-UNCOVERED", "SC-004")]
    message = result["errors"][0]["message"]
    for word in ("metric", "repair case", "research part", "stop", "repair.pass_to_pass", "owner gate"):
        assert word in message, word
    assert not message.startswith("no runbook check covers")


def test_spec_uncovered_keeps_its_format_2_message(tmp_path):
    folder = str(tmp_path / "ex")
    shutil.copytree(EXAMPLE2, folder)
    _edit(folder, "runbook.yaml", ("covers: [SC-003, FR-006]", "covers: [FR-006]"))
    result = runbook.check(os.path.join(folder, "runbook.yaml"))
    uncovered = [e for e in result["errors"] if e["code"] == "SPEC-UNCOVERED"]
    assert [e["where"] for e in uncovered] == ["SC-003"]
    assert uncovered[0]["message"].startswith("no runbook check covers SC-003 (")
    assert uncovered[0]["message"].endswith("add it to the `covers` list of the check that shows it or of the fail "
                                            "case that detects and answers it, or to an owner gate when only a "
                                            "person can judge it")


# ------------------------------------------------------------- the block keyword (14.1)
def test_covering_reads_the_research_coverers_only_from_the_block(pack):
    data, block = _loaded(pack)
    plain = runbook._covering(data)
    full = runbook._covering(data, block)
    assert all(not who.startswith(("metric:", "case:", "research:")) for _e, who, _w in plain)
    assert full[:len(plain)] == plain
    assert ("SC-001", "metric:compares", "research.metrics[0].covers[0]") in full
    assert ("SC-004", "research:stop", "research.stop.covers[0]") in full
    assert ("EC-003", "research:paths", "research.paths.covers[0]") in full


def test_format_2_callers_get_what_they_got_before(tmp_path):
    """A format 1 or 2 runbook: the block keyword changes nothing in _covering, coverage,
    bar_parts and _note_sources, even when a block is passed (section 2.3, reason 2)."""
    data, _report = runbook.load(os.path.join(EXAMPLE2, "runbook.yaml"))
    fake = {"metrics": [{"id": "m", "covers": ["SC-001"], "source": "note:input/notes/x.md"}],
            "stop": {"rounds": 1, "slots": 1, "covers": ["SC-002"]}}
    assert runbook._covering(data, fake) == runbook._covering(data)
    assert runbook.bar_parts(data, fake) == runbook.bar_parts(data)
    assert runbook._note_sources(data, fake) == runbook._note_sources(data)
    spec = runbook.parse_spec(os.path.join(EXAMPLE2, "spec.md"))
    one, two = runbook._Report(), runbook._Report()
    assert runbook.coverage(data, spec, one, block=fake) == runbook.coverage(data, spec, two)
    assert one.errors == two.errors == [] and one.warnings == two.warnings


# ------------------------------------------------------------------------ bar_parts (14.3)
IMPROVE_BARS = {
    "metric:compares": ("metric", "spec:SC-001",
                        "metric compares: lower is better, comparisons; deterministic, median (worse) of 1 repeats, "
                        "at least 1 ok; beats the champion by more than 0 comparisons; target <= 18000 (owner only)"),
    "metric:wall_ms": ("metric", "spec:SC-002",
                       "metric wall_ms: lower is better, ms; noisy, median (worse) of 5 repeats, at least 3 ok; "
                       "beats the champion by more than 4 ms; target <= 120 (owner only)"),
    "research:accept": ("research", None, "accept deficit-pareto, after every target stop"),
    "research:stop": ("research", "spec:SC-004",
                      "stop when every target is met or after 5 rounds of 2 slots; at 5 the run halts for the owner"),
    "research:budget": ("research", None, "budget 40 USD, 3000000 tokens, 14400 s; past it the run pauses for the owner"),
    "research:paths": ("research", "spec:EC-003",
                       "editable src/sortlib/; protected packs/sortbench/, pyproject.toml; a change outside the "
                       "editable rules or to a protected path breaks the attempt"),
    "research:fingerprint": ("research", None, "fingerprint tools python3 before and after each agent and the eval"),
    "research:controls": ("research", None, "no-op control for 2 rounds before a real run, and the random-edit "
                          "control; an accepted control attempt fails the control run"),
    "research:holdout": ("research", None, "holdout stages holdout-bench run on the final champion only"),
}
REPAIR_BARS = {
    "case:stable-keys": ("case", "spec:SC-001",
                         "repair case stable-keys: stable_equal_keys, stable_all_same_key FAIL on base "
                         "0f4c2a9e1b7d3c5a8e6f0b2d4c6a8e0f1b3d5c7a and PASS on the fix, 1 run(s) each"),
    "case:none-key": ("case", "spec:SC-002",
                      "repair case none-key: the generated reproducer FAIL on the run base and PASS on the fix, "
                      "2 run(s) each"),
    "research:pass-to-pass": ("research", "spec:SC-003",
                              "pass-to-pass: every test that passes on the base still passes, at least 12 tests"),
    "research:accept": ("research", None, "accept repair"),
    "research:stop": ("research", None,
                      "stop when every case is fixed or after 3 rounds of 2 slots; at 3 the run halts for the owner"),
    "research:controls": ("research", None, "no-op control for 2 rounds before a real run; an accepted control "
                          "attempt fails the control run"),
    "research:holdout": ("research", None, "no holdout stage"),
}


@pytest.mark.parametrize("name,want", [("improve.yaml", IMPROVE_BARS), ("repair.yaml", REPAIR_BARS)])
def test_bar_parts_gives_every_research_coverer_its_bar(pack, name, want):
    data, block = _loaded(pack, name)
    bars = runbook.bar_parts(data, block)
    research = dict((who, part) for who, part in bars["parts"].items() if part["stage"] is None)
    for who, (kind, source, bar) in want.items():
        assert research[who] == {"kind": kind, "stage": None, "source": source, "knobs": [], "bar": bar}, who
    extra = set(research) - set(want)
    assert extra <= {"research:budget", "research:paths", "research:fingerprint"}, extra
    # the format 2 parts, the stages and the knobs are what bar_parts gives without the block
    plain = runbook.bar_parts(data)
    assert {w: p for w, p in bars["parts"].items() if p["stage"] is not None} == plain["parts"]
    assert bars["stages"] == plain["stages"] and bars["knobs"] == plain["knobs"]
    # every coverer coverage names has a part, so intake finds a bar for every row
    covered = _check(pack, name)["coverage"]["covered"]
    for who_list in covered.values():
        for who in who_list:
            assert who in bars["parts"], who


def test_the_bars_read_values_the_defaults_file_and_the_engine_set(pack):
    _edit(pack, "pack.yaml", ("usd: 40", "usd: 25"), ("    wall: 14400\n", ""))
    data, block = _loaded(pack)
    bars = runbook.bar_parts(data, block)["parts"]
    assert bars["research:budget"]["bar"] == "budget 25 USD, 3000000 tokens; past it the run pauses for the owner"
    assert "median (worse) of 1 repeats" in bars["metric:compares"]["bar"]      # engine layer


@pytest.mark.parametrize("edits,who,bar", [
    ([("direction: lower\n      kind: deterministic", "direction: higher\n      kind: deterministic")], "metric:compares",
     "target >= 18000 (owner only)"),
    ([("      target: 18000\n", ""), ("      target: 120\n", ""), ("rule: deficit-pareto\n    after_targets: stop\n",
                                                                      "rule: deficit-pareto\n")],
     "research:stop", "stop after 5 rounds of 2 slots; at 5 the run halts for the owner"),
    ([("after_targets: stop", "after_targets: pareto")], "research:stop",
     "stop after 5 rounds of 2 slots; at 5 the run halts for the owner"),
    ([("random: true\n    mutate: bench/mutate.py\n    syntax: build\n", "random: false\n")], "research:controls",
     "no-op control for 2 rounds before a real run; an accepted control attempt fails the control run"),
])
def test_bar_variants(pack, edits, who, bar):
    _edit(pack, "improve.yaml", *edits)
    data, block = _loaded(pack)
    shown = runbook.bar_parts(data, block)["parts"][who]["bar"]
    assert shown.endswith(bar) or shown == bar, shown
    if who == "metric:compares":
        assert "; target <=" not in shown


def test_a_metric_without_a_target_has_no_target_in_its_bar(pack):
    _edit(pack, "improve.yaml", ("      target: 120\n", ""))
    data, block = _loaded(pack)
    bar = runbook.bar_parts(data, block)["parts"]["metric:wall_ms"]["bar"]
    assert bar.endswith("beats the champion by more than 4 ms") and "target" not in bar


def test_the_fingerprint_bar_names_shared_checkouts(pack):
    _edit(pack, "pack.yaml", ("tools: [python3]", "tools: [python3]\n    shared: [../vendor]"))
    data, block = _loaded(pack)
    assert runbook.bar_parts(data, block)["parts"]["research:fingerprint"]["bar"] == (
        "fingerprint tools python3, shared ../vendor before and after each agent and the eval")


# -------------------------------------------------------------- note: sources (_note_sources)
def _note(root, name):
    folder = os.path.join(root, "input", "notes")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, name), "w", encoding="utf-8") as fh:
        fh.write("a note\n")


def test_a_research_note_source_that_names_no_note_warns_source_missing(pack):
    root = os.path.dirname(os.path.dirname(pack))
    _edit(pack, "improve.yaml", ("      target: 120\n", ""), ("source: spec:SC-002", "source: note:input/notes/wall.md"),
          ("source: spec:SC-004", "source: note:input/notes/stop.md"))
    result = _check(pack)
    assert result["errors"] == []
    assert [(w["code"], w["where"]) for w in result["warnings"]] == [
        ("SOURCE-MISSING", "research.metrics[1].source"), ("SOURCE-MISSING", "research.stop.source")]
    _note(root, "wall.md")
    _note(root, "stop.md")
    assert _check(pack)["warnings"] == []
    # --no-files skips the look-up, as for format 2 sources
    os.remove(os.path.join(root, "input", "notes", "stop.md"))
    assert _check(pack, check_files=False)["warnings"] == []


def test_a_case_and_the_pass_to_pass_note_sources_are_looked_up(pack):
    _edit(pack, "repair.yaml", ("source: spec:SC-002", "source: note:input/notes/none-key.md"),
          ("source: spec:SC-003", "source: note:input/notes/old-tests.md"))
    result = _check(pack, "repair.yaml")
    assert [(w["code"], w["where"]) for w in result["warnings"]] == [
        ("SOURCE-MISSING", "research.repair.cases[1].source"),
        ("SOURCE-MISSING", "research.repair.pass_to_pass.source")]
    data, block = _loaded(pack, "repair.yaml")
    assert runbook._note_sources(data) == []                         # the block keyword finds them
    assert [w for w, _n in runbook._note_sources(data, block)] == ["research.repair.cases[1].source",
                                                                    "research.repair.pass_to_pass.source"]


# ------------------------------------------------------------ a test for every new code
#: each research code, and the test above that asserts it with its location
CODE_TESTS = {
    "FIELD-UNFILLED": "test_a_placeholder_gives_field_unfilled_and_no_other_code",
    "VALUE-UNKNOWN": "test_value_unknown_names_the_words",
    "DEFAULTS-UNREADABLE": "test_defaults_unreadable",
    "TARGET-SOURCE": "test_target_source_accepts_spec_and_owner_and_refuses_the_rest",
    "ACCEPT-RULE-MISMATCH": "test_accept_rule_mismatch_for_after_targets_without_a_target",
    "PATHS-RULE-INVALID": "test_paths_rule_invalid",
    "PATHS-OVERLAP": "test_paths_overlap_for_a_hidden_rule_and_a_holdout_rule",
    "TOOL-INVALID": "test_tool_invalid_points_into_the_defaults_file",
    "HOST-INVALID": "test_host_invalid",
    "STAGE-UNKNOWN": "test_stage_unknown",
    "STAGE-ROLE": "test_stage_role",
    "STAGE-UNUSED": "test_stage_unused",
    "GATES-ORDER": "test_gates_order_for_a_gate_and_for_the_prepare_stage",
    "TIMEOUT-MISSING": "test_each_repair_variant_gives_its_one_error",
    "THEN-ASK-OWNER": "test_then_ask_owner",
    "KNOB-MOVES-BAR": "test_knob_moves_bar_for_a_gate_checks_value_and_not_for_its_run",
    "PACK-OUTSIDE-REPO": "test_pack_outside_repo_when_repo_path_can_be_resolved",
    "PROPOSAL-FIELD-RESERVED": "test_proposal_field_reserved",
    "FILE-MISSING": "test_file_missing_for_a_lessons_file_and_a_bug_report_and_no_files_skips_it",
    "BASE-NOT-COMMIT": "test_base_not_commit",
    "COVERS-NONE": "test_covers_none_for_a_metric_without_covers",
}


def test_every_research_code_has_its_test():
    import inspect
    assert set(CODE_TESTS) == set(runbook.RESEARCH_ERROR_CODES)
    module = globals()
    for code, name in CODE_TESTS.items():
        fn = module[name]
        source = inspect.getsource(fn)
        # the code is named in the test itself, or in the variant table it reads
        tables = inspect.getsource(inspect.getmodule(fn)).split("def %s" % name)[0][-6000:]
        assert '"%s"' % code in source or '"%s"' % code in tables, (code, name)


# ------------------------------------------------------------------------ the docs section
def _doc_section():
    with open(os.path.join(REPO, "docs", "runbook-format.md"), encoding="utf-8") as fh:
        doc = fh.read()
    start = doc.index("\n## The research block\n") + 1
    lines, fence = [], False
    for n, line in enumerate(doc[start:].split("\n")):
        if line.startswith("```"):
            fence = not fence
        if n and not fence and line.startswith("## "):
            break                       # the next section; a heading inside a fence is example text
        lines.append(line)
    return doc, "\n".join(lines)


def test_the_doc_names_every_research_field_and_code():
    doc, section = _doc_section()
    paths = [f["path"] for f in runbook.research_fields()]
    assert len(paths) > 100
    for path in paths:
        assert "`%s`" % path in section, path
    for code in runbook.RESEARCH_ERROR_CODES:
        assert "| `%s` |" % code in section, code             # a row of the section's code table
        assert "| `%s` | format 3:" % code in doc, code       # and of the check's code table
    for word in runbook.MODES + runbook.PARSERS + runbook.DIRECTIONS + runbook.METRIC_KINDS + runbook.MEDIANS \
            + runbook.ACCEPT_RULES + runbook.AFTER_TARGETS + runbook.PROPOSAL_TYPES + runbook.F2P_ORIGINS \
            + runbook.AGENT_ROLES + runbook.EFFORTS:
        assert "`%s`" % word in section or "`research.runtime.timeouts.%s`" % word in section, word
    for name in runbook.RESEARCH_VARS + runbook.METRIC_VARS + runbook.F2P_VARS:
        assert "`${%s}`" % name in section, name
    assert "`pack`" in section and "`VALUE-NOT-NUMBER`" in section and "`FILE-UNREADABLE`" in section
    for coverer in ("`metric:<id>`", "`case:<id>`", "`research:pass-to-pass`", "`research:accept`",
                    "`research:stop`", "`research:budget`", "`research:paths`", "`research:fingerprint`",
                    "`research:controls`", "`research:holdout`"):
        assert coverer in section, coverer


def test_the_doc_section_is_plain_ascii_and_carries_no_product_only_word():
    doc, section = _doc_section()
    assert all(ord(c) < 128 for c in doc)
    for word in runbook_kit.PRODUCT_ONLY:
        assert word not in section, word
    from alpaca.tests.test_runbook_kit import BANNED
    for word in BANNED:
        assert word not in section.lower(), word


def test_the_doc_version_lines_name_format_3_and_keep_format_2():
    doc, _section = _doc_section()
    fmt = doc.split("## Format 1 and format 2", 1)[1].split("\n## ", 1)[0]
    assert "`runbook: 2`" in fmt and "`runbook: 3` is the research format" in fmt
    assert "Any other `runbook:` value" in fmt
    row = [line for line in doc.splitlines() if line.startswith("| `runbook` |")]
    assert len(row) == 1 and "`runbook: 2`" in row[0] and "`runbook: 3` for a research runbook" in row[0]


def test_the_doc_improve_example_is_the_fixture_and_checks_as_it_says(pack):
    """The worked example in the doc is the fixture byte for byte, and the edit the doc describes
    gives the two errors it names."""
    _doc, section = _doc_section()
    blocks = section.split("```")
    fenced = [blocks[i].split("\n", 1)[1] for i in range(1, len(blocks), 2)]
    for name in ("improve-spec.md", "pack.yaml", "improve.yaml"):
        with open(os.path.join(FIX, name), encoding="utf-8") as fh:
            assert fh.read() in fenced, name
    assert "runbook: improve.yaml\nspec: improve-spec.md (spec-kit, 8 item(s), 7 required)\n" in section
    _edit(pack, "improve.yaml", ("      covers: [SC-002]\n", ""))
    assert _errors(_check(pack)) == [("SPEC-UNCOVERED", "SC-002"), ("COVERS-NONE", "research.metrics[1].covers")]
    assert "`SPEC-UNCOVERED` for SC-002, and `COVERS-NONE` at\n`research.metrics[1].covers`" in section


def test_the_kit_format_carries_the_research_section():
    text = runbook_kit.build_format(REPO)
    assert "## The research block" in text and "### Coverage in format 3" in text
    for code in runbook.RESEARCH_ERROR_CODES:
        assert "`%s`" % code in text, code
