"""The runbook format (docs/runbook-format.md) and `alpaca runbook check`.

A runbook is the executable plan of a domain: stages, the command each stage runs, its inputs and
outputs, pass checks, knobs, retry rules and owner gates. `alpaca runbook check <file> [--spec
<path>]` refuses a malformed runbook with a message that names the field, and with a spec it fails
when a spec-kit success criterion (SC-nnn) or an OpenSpec scenario is covered by no check.

The spec fixtures below follow the upstream templates: spec-kit's templates/spec-template.md
(`**SC-001**:` bullets under `## Success Criteria`) and OpenSpec's spec and delta shape
(`### Requirement:` blocks with `#### Scenario:` children, `## ADDED/MODIFIED/REMOVED
Requirements` in a change).
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap

import pytest
import yaml

from alpaca import cli, runbook, surface
from alpaca.gates import verdict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
EXAMPLE = os.path.join(REPO, "templates", "runbook-example")
# the worked example as it was in format 1, kept so format 1 stays tested
EXAMPLE_V1 = os.path.join(REPO, "alpaca", "tests", "fixtures", "runbook-example-v1")


# ------------------------------------------------------------------------------- fixtures
SPEC_KIT = """\
# Feature Specification: Nightly report

**Feature Branch**: `002-nightly-report`

**Created**: 2026-01-10

**Status**: Draft

**Input**: User description: "mail a summary of yesterday's orders every night"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Get the summary (Priority: P1)

The manager reads the summary each morning.

**Acceptance Scenarios**:

1. **Given** orders yesterday, **When** the job runs, **Then** a summary is mailed.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST read every order of the previous day.
- **FR-002**: System MUST mail the summary to [NEEDS CLARIFICATION: which list?]

## Success Criteria *(mandatory)*

<!--
  - **SC-099**: a commented-out example that is not a criterion
-->

### Measurable Outcomes

- **SC-001**: The summary counts every order of the day.
- **SC-002**: The job finishes in under 15 minutes.
- **SC-003**: The manager reads the summary before 08:00 on 95% of days.

```text
- **SC-042**: inside a fence, not a criterion
```

## Assumptions

- Orders are in one database.
"""

OPENSPEC_MAIN = """\
# session-timeout Specification

## Purpose
Expire idle sessions.

## Requirements

### Requirement: Session Timeout
The system SHALL expire a session after 30 minutes of inactivity.

#### Scenario: Idle timeout
- **GIVEN** an authenticated session
- **WHEN** 30 minutes pass with no activity
- **THEN** the session is invalidated

#### Scenario: Activity resets the clock
- **WHEN** a request arrives at minute 29
- **THEN** the session stays valid for 30 more minutes

### Requirement: Logout
The system SHALL end a session on logout.

#### Scenario: Explicit logout ####
- **WHEN** the user logs out
- **THEN** the session is invalidated

```markdown
#### Scenario: Fenced example
- **WHEN** this sits in a fence
- **THEN** it is not a scenario
```
"""

OPENSPEC_DELTA = """\
## ADDED Requirements
### Requirement: Remember me
The system SHALL keep a remembered session for 14 days.

#### Scenario: Remembered session survives a restart
- **WHEN** the browser restarts within 14 days
- **THEN** the user is still signed in

## MODIFIED Requirements
### Requirement: Session Timeout
The system SHALL expire a session after 20 minutes of inactivity.

#### Scenario: Idle timeout
- **WHEN** 20 minutes pass with no activity
- **THEN** the session is invalidated

## REMOVED Requirements
### Requirement: Legacy token
**Reason**: replaced by remembered sessions

#### Scenario: Legacy token accepted
- **WHEN** an old token arrives
- **THEN** it is accepted
"""


def minimal(**over):
    """A small valid runbook as a dict; keyword arguments replace top-level keys."""
    data = {
        "runbook": 1,
        "id": "demo",
        "title": "Demo",
        "knobs": [{"id": "WORKERS", "description": "worker count", "type": "int",
                   "default": 2, "min": 1, "max": 8}],
        "stages": [
            {"id": "build", "run": "make build", "checks": [
                {"id": "build-exit", "type": "exit-code", "covers": ["SC-001"]}]},
            {"id": "load", "needs": ["build"], "run": "make load W=${WORKERS}", "checks": [
                {"id": "p95", "type": "json-field", "path": "out/load.json",
                 "field": "p95_ms", "op": "<=", "value": 50, "covers": ["SC-002"]}],
             "retry": {"max_attempts": 3, "on_fail": ["p95"],
                       "move": {"knob": "WORKERS", "by": 2}}},
        ],
    }
    data.update(over)
    return data


def write(tmp_path, data, name="runbook.yaml"):
    path = tmp_path / name
    path.write_text(data if isinstance(data, str) else yaml.safe_dump(data, sort_keys=False),
                    encoding="utf-8")
    return str(path)


def codes(result):
    return [e["code"] for e in result["errors"]]


def run_cli(tmp_path, monkeypatch, capsys, *argv):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["runbook", "check"] + list(argv))
    return rc, capsys.readouterr()


# ------------------------------------------------------------------------ valid runbooks
def test_minimal_runbook_passes(tmp_path):
    result = runbook.check(write(tmp_path, minimal()))
    assert result["errors"] == []
    assert result["verdict"] == "PASS" and result["code"] == verdict.PASS


def test_worked_example_passes_against_its_own_spec():
    result = runbook.check(os.path.join(EXAMPLE_V1, "runbook.yaml"))
    assert result["errors"] == [], result["errors"]
    assert result["verdict"] == "PASS"
    assert result["spec"]["format"] == "spec-kit"
    cov = result["coverage"]
    assert cov["missing"] == []
    assert sorted(cov["covered"]) == ["SC-001", "SC-002", "SC-003", "SC-004"]
    # SC-004 is human-judged: the release owner gate covers it.
    assert cov["covered"]["SC-004"] == ["gate:release"]


def test_worked_example_plugin_is_executable():
    script = os.path.join(EXAMPLE, "checks", "status_codes.py")
    assert os.stat(script).st_mode & stat.S_IXUSR


def test_worked_example_fails_when_a_criterion_loses_its_check(tmp_path):
    with open(os.path.join(EXAMPLE, "runbook.yaml"), encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    for stage in data["stages"]:
        for chk in stage.get("checks", []):
            chk["covers"] = [c for c in chk.get("covers", []) if c != "SC-003"]
    path = write(tmp_path, data)
    result = runbook.check(path, spec_path=os.path.join(EXAMPLE, "spec.md"), check_files=False)
    assert result["verdict"] == "FAIL"
    missing = [e for e in result["errors"] if e["code"] == "SPEC-UNCOVERED"]
    assert [e["where"] for e in missing] == ["SC-003"]
    assert "no runbook check covers" in missing[0]["message"]


# ---------------------------------------------------------------------- malformed input
def test_yaml_syntax_error_names_the_line(tmp_path):
    result = runbook.check(write(tmp_path, "runbook: 1\nid: [unclosed\n"))
    assert codes(result) == ["YAML-SYNTAX"]
    assert "line" in result["errors"][0]["message"]
    assert result["verdict"] == "FAIL"


def test_top_level_must_be_a_mapping(tmp_path):
    result = runbook.check(write(tmp_path, "- one\n- two\n"))
    assert codes(result) == ["NOT-MAPPING"]


def test_duplicate_yaml_key_is_refused(tmp_path):
    text = yaml.safe_dump(minimal(), sort_keys=False) + "title: Again\n"
    result = runbook.check(write(tmp_path, text))
    assert "KEY-DUPLICATE" in codes(result)
    assert any("title" in e["message"] for e in result["errors"])


def test_missing_version_and_title(tmp_path):
    data = minimal()
    del data["runbook"], data["title"]
    result = runbook.check(write(tmp_path, data))
    wheres = {(e["code"], e["where"]) for e in result["errors"]}
    assert ("FIELD-MISSING", "runbook") in wheres
    assert ("FIELD-MISSING", "title") in wheres


def test_unsupported_version(tmp_path):
    result = runbook.check(write(tmp_path, minimal(runbook=7)))
    assert codes(result) == ["VERSION-UNSUPPORTED"]


def test_unknown_key_suggests_the_near_one(tmp_path):
    data = minimal()
    data["stages"][0]["comand"] = data["stages"][0].pop("run")
    result = runbook.check(write(tmp_path, data))
    unknown = [e for e in result["errors"] if e["code"] == "FIELD-UNKNOWN"]
    assert unknown and unknown[0]["where"] == "stages[0].comand"
    assert "did you mean `command`" not in unknown[0]["message"]
    assert "`run`" in unknown[0]["message"]


def test_empty_stage_list(tmp_path):
    result = runbook.check(write(tmp_path, minimal(stages=[])))
    assert ("FIELD-EMPTY", "stages") in {(e["code"], e["where"]) for e in result["errors"]}


def test_bad_ids_and_duplicates(tmp_path):
    data = minimal()
    data["id"] = "Not A Slug"
    data["stages"][1]["id"] = "build"
    data["stages"][1]["needs"] = []
    data["stages"][1]["checks"][0]["id"] = "build-exit"
    result = runbook.check(write(tmp_path, data))
    got = {(e["code"], e["where"]) for e in result["errors"]}
    assert ("ID-INVALID", "id") in got
    assert ("ID-DUPLICATE", "stages[1].id") in got
    assert ("ID-DUPLICATE", "stages[1].checks[0].id") in got


def test_needs_must_name_an_earlier_stage(tmp_path):
    data = minimal()
    data["stages"][0]["needs"] = ["load"]
    data["stages"][1]["needs"] = ["nowhere"]
    result = runbook.check(write(tmp_path, data))
    got = [(e["code"], e["where"]) for e in result["errors"]]
    assert ("NEEDS-UNKNOWN", "stages[0].needs[0]") in got
    assert ("NEEDS-UNKNOWN", "stages[1].needs[0]") in got


def test_stage_needs_run_or_owner_gate_and_checks(tmp_path):
    data = minimal()
    del data["stages"][0]["run"]
    data["stages"][1]["checks"] = []
    result = runbook.check(write(tmp_path, data))
    got = {(e["code"], e["where"]) for e in result["errors"]}
    assert ("RUN-MISSING", "stages[0]") in got
    assert ("CHECKS-EMPTY", "stages[1].checks") in got


def test_pure_owner_gate_stage_is_valid(tmp_path):
    data = minimal()
    data["stages"].append({"id": "ship", "needs": ["load"],
                           "owner_gate": {"approve": "the owner approves the release"}})
    result = runbook.check(write(tmp_path, data))
    assert result["errors"] == []


@pytest.mark.parametrize("checks", [
    [{"id": "wall", "type": "json-field", "path": "out/bench.json", "field": "wall_seconds",
      "op": "<", "value": 600, "covers": ["SC-002"]}],
    [],
])
def test_owner_gate_stage_without_run_may_not_carry_checks(tmp_path, checks):
    # A gate-only stage runs nothing, so intake makes it an approval task only: a check there
    # would be in no task contract and never judged, while its `covers` still counted.
    data = minimal()
    data["stages"][1]["checks"][0]["covers"] = []
    data["stages"].append({"id": "bench", "needs": ["load"], "checks": checks,
                           "owner_gate": {"approve": "the owner reads the benchmark"}})
    result = runbook.check(write(tmp_path, data))
    assert [(e["code"], e["where"]) for e in result["errors"]] == [("CHECKS-WITHOUT-RUN", "stages[2].checks")]
    assert "run" in result["errors"][0]["message"]
    assert result["verdict"] == "FAIL"


def test_stage_ranges_are_named_once():
    """The ranges the checker enforces are module constants that the kit schema reads too
    (test_runbook_kit.py moves them and watches both follow; a moved range here would reach the
    kit replay of this file's checks)."""
    assert runbook.EXPECT_RANGE == (0, 255)
    assert runbook.PLUGIN_TIMEOUT_RANGE == (1, 24 * 3600)
    assert runbook.STAGE_TIMEOUT_RANGE == (1, 7 * 24 * 3600)


def test_owner_gate_needs_approve_text(tmp_path):
    data = minimal()
    data["stages"].append({"id": "ship", "owner_gate": {"evidence": ["out/x"]}})
    result = runbook.check(write(tmp_path, data))
    assert ("FIELD-MISSING", "stages[2].owner_gate.approve") in {
        (e["code"], e["where"]) for e in result["errors"]}


@pytest.mark.parametrize("chk,code,where", [
    ({"id": "c", "type": "smoke-signal"}, "CHECK-TYPE-UNKNOWN", "stages[0].checks[0].type"),
    ({"id": "c", "type": "file-exists"}, "FIELD-MISSING", "stages[0].checks[0].path"),
    ({"id": "c", "type": "regex-in-file", "path": "log", "pattern": "(unclosed"},
     "REGEX-INVALID", "stages[0].checks[0].pattern"),
    ({"id": "c", "type": "json-field", "path": "m.json", "field": "a", "op": "~=", "value": 1},
     "OP-UNKNOWN", "stages[0].checks[0].op"),
    ({"id": "c", "type": "json-field", "path": "m.json", "field": "a", "op": "<", "value": "fast"},
     "VALUE-NOT-NUMBER", "stages[0].checks[0].value"),
    ({"id": "c", "type": "exit-code", "expect": 300}, "RANGE", "stages[0].checks[0].expect"),
    ({"id": "c", "type": "exit-code", "path": "x"}, "FIELD-UNKNOWN", "stages[0].checks[0].path"),
    ({"id": "c", "type": "plugin"}, "FIELD-MISSING", "stages[0].checks[0].script"),
    ({"id": "c", "type": "plugin", "script": "checks/none.py"},
     "PLUGIN-MISSING", "stages[0].checks[0].script"),
    ({"id": "c", "type": "exit-code", "covers": "SC-001"}, "FIELD-NOT-LIST", "stages[0].checks[0].covers"),
])
def test_check_shapes(tmp_path, chk, code, where):
    data = minimal()
    data["stages"][0]["checks"] = [chk]
    result = runbook.check(write(tmp_path, data))
    assert (code, where) in {(e["code"], e["where"]) for e in result["errors"]}, result["errors"]
    assert result["verdict"] == "FAIL"


def test_check_type_message_lists_the_types(tmp_path):
    data = minimal()
    data["stages"][0]["checks"] = [{"id": "c", "type": "smoke-signal"}]
    result = runbook.check(write(tmp_path, data))
    msg = [e for e in result["errors"] if e["code"] == "CHECK-TYPE-UNKNOWN"][0]["message"]
    for kind in runbook.CHECK_TYPES:
        assert kind in msg


def test_plugin_must_be_executable(tmp_path):
    (tmp_path / "checks").mkdir()
    script = tmp_path / "checks" / "c.py"
    script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    data = minimal()
    data["stages"][0]["checks"] = [{"id": "c", "type": "plugin", "script": "checks/c.py"}]
    path = write(tmp_path, data)
    assert "PLUGIN-NOT-EXECUTABLE" in codes(runbook.check(path))
    script.chmod(0o755)
    assert runbook.check(path)["errors"] == []
    # check_files=False skips the file-system look (a runbook written before its scripts)
    script.unlink()
    assert runbook.check(path, check_files=False)["errors"] == []


def test_plugin_script_may_not_leave_the_runbook_folder(tmp_path):
    data = minimal()
    data["stages"][0]["checks"] = [{"id": "c", "type": "plugin", "script": "../outside.py"}]
    result = runbook.check(write(tmp_path, data), check_files=False)
    assert ("PATH-ESCAPES", "stages[0].checks[0].script") in {
        (e["code"], e["where"]) for e in result["errors"]}


def test_plugin_script_may_not_hold_a_variable(tmp_path):
    # `evaluate` runs the script path as written (it replaces ${NAME} only in args), so a script
    # named through a variable would never run as meant: refuse it when the runbook is checked
    data = minimal()
    data["stages"][0]["checks"] = [{"id": "c", "type": "plugin", "script": "checks/${EVIDENCE_DIR}.py"}]
    result = runbook.check(write(tmp_path, data), check_files=False)
    assert ("PLUGIN-SCRIPT-VARIABLE", "stages[0].checks[0].script") in {
        (e["code"], e["where"]) for e in result["errors"]}, result["errors"]
    assert result["verdict"] == "FAIL"


@pytest.mark.parametrize("knob,code,where", [
    ({"id": "W", "description": "d", "type": "int", "default": 20, "min": 1, "max": 8},
     "KNOB-DEFAULT-RANGE", "knobs[0].default"),
    ({"id": "W", "description": "d", "type": "int", "default": 2, "min": 9, "max": 8},
     "RANGE", "knobs[0].min"),
    ({"id": "W", "description": "d", "type": "int", "default": 2.5}, "KNOB-DEFAULT-TYPE", "knobs[0].default"),
    ({"id": "W", "description": "d", "type": "enum", "default": "c", "values": ["a", "b"]},
     "KNOB-DEFAULT-RANGE", "knobs[0].default"),
    ({"id": "W", "description": "d", "type": "enum", "default": "a"}, "FIELD-MISSING", "knobs[0].values"),
    ({"id": "W", "description": "d", "type": "colour", "default": "a"}, "KNOB-TYPE-UNKNOWN", "knobs[0].type"),
    ({"id": "9W", "description": "d", "type": "int", "default": 1}, "ID-INVALID", "knobs[0].id"),
])
def test_knob_shapes(tmp_path, knob, code, where):
    data = minimal(knobs=[knob])
    data["stages"][1].pop("retry")
    data["stages"][1]["run"] = "make load"
    result = runbook.check(write(tmp_path, data))
    assert (code, where) in {(e["code"], e["where"]) for e in result["errors"]}, result["errors"]


@pytest.mark.parametrize("retry,code,where", [
    ({"max_attempts": 0}, "RANGE", "stages[1].retry.max_attempts"),
    ({"max_attempts": 3, "on_fail": ["build-exit"]}, "RETRY-CHECK-UNKNOWN", "stages[1].retry.on_fail[0]"),
    ({"max_attempts": 3, "on_fail": ["p95"], "stop_on": ["p95"]}, "RETRY-OVERLAP", "stages[1].retry.stop_on[0]"),
    ({"max_attempts": 3, "move": {"knob": "THREADS", "by": 1}}, "KNOB-UNKNOWN", "stages[1].retry.move.knob"),
    ({"max_attempts": 3, "move": {"knob": "WORKERS", "by": 0}}, "RANGE", "stages[1].retry.move.by"),
    ({"max_attempts": 3, "move": {"knob": "WORKERS"}}, "FIELD-MISSING", "stages[1].retry.move.by"),
    ({"attempts": 3}, "FIELD-UNKNOWN", "stages[1].retry.attempts"),
])
def test_retry_shapes(tmp_path, retry, code, where):
    data = minimal()
    data["stages"][1]["retry"] = retry
    result = runbook.check(write(tmp_path, data))
    assert (code, where) in {(e["code"], e["where"]) for e in result["errors"]}, result["errors"]


def test_bare_on_key_reads_as_true_and_is_named(tmp_path):
    # YAML 1.1 reads a bare `on:` key as the boolean true; the message says so.
    text = yaml.safe_dump(minimal(), sort_keys=False).replace("on_fail:", "on:")
    result = runbook.check(write(tmp_path, text))
    bad = [e for e in result["errors"] if e["where"] == "stages[1].retry.true"]
    assert bad and "on_fail" in bad[0]["message"]


def test_retry_may_not_move_an_owner_only_knob(tmp_path):
    data = minimal()
    data["knobs"][0]["owner_only"] = True
    result = runbook.check(write(tmp_path, data))
    assert ("KNOB-OWNER-ONLY", "stages[1].retry.move.knob") in {
        (e["code"], e["where"]) for e in result["errors"]}


def test_retry_move_needs_a_number_knob(tmp_path):
    data = minimal()
    data["knobs"].append({"id": "MODE", "description": "d", "type": "enum",
                          "default": "a", "values": ["a", "b"]})
    data["stages"][1]["retry"]["move"]["knob"] = "MODE"
    result = runbook.check(write(tmp_path, data))
    assert "KNOB-NOT-NUMBER" in codes(result)


def test_unknown_variable(tmp_path):
    data = minimal()
    data["stages"][0]["run"] = "make build J=${JOBS}"
    result = runbook.check(write(tmp_path, data))
    bad = [e for e in result["errors"] if e["code"] == "VAR-UNKNOWN"]
    assert bad and bad[0]["where"] == "stages[0].run" and "JOBS" in bad[0]["message"]


def test_builtin_variables_are_known(tmp_path):
    data = minimal()
    data["stages"][0]["run"] = "make build OUT=${EVIDENCE_DIR} N=${ATTEMPT} S=${STAGE} R=${RUNBOOK_DIR}"
    assert runbook.check(write(tmp_path, data))["errors"] == []


def test_knob_reference_as_threshold_must_be_a_number_knob(tmp_path):
    data = minimal()
    data["knobs"].append({"id": "LIMIT", "description": "d", "type": "float", "default": 50})
    data["stages"][1]["checks"][0]["value"] = "${LIMIT}"
    assert runbook.check(write(tmp_path, data))["errors"] == []
    data["knobs"][1] = {"id": "LIMIT", "description": "d", "type": "str", "default": "x"}
    assert "VALUE-NOT-NUMBER" in codes(runbook.check(write(tmp_path, data)))


def test_every_error_is_reported_at_once(tmp_path):
    data = minimal()
    del data["title"]
    data["stages"][0]["checks"] = [{"id": "c", "type": "smoke-signal"}]
    data["stages"][1]["retry"]["max_attempts"] = 0
    result = runbook.check(write(tmp_path, data))
    assert {"FIELD-MISSING", "CHECK-TYPE-UNKNOWN", "RANGE"} <= set(codes(result))


# ------------------------------------------------------------------------- spec parsing
def test_parse_spec_kit(tmp_path):
    path = tmp_path / "spec.md"
    path.write_text(SPEC_KIT, encoding="utf-8")
    spec = runbook.parse_spec(str(path))
    assert spec["format"] == "spec-kit"
    required = [i["id"] for i in spec["items"] if i["required"]]
    assert required == ["SC-001", "SC-002", "SC-003"]          # comment and fence skipped
    assert [i["id"] for i in spec["items"] if not i["required"]] == ["FR-001", "FR-002"]
    assert spec["clarifications"] == 1


def test_parse_openspec_main_spec(tmp_path):
    path = tmp_path / "spec.md"
    path.write_text(OPENSPEC_MAIN, encoding="utf-8")
    spec = runbook.parse_spec(str(path))
    assert spec["format"] == "openspec"
    assert [i["id"] for i in spec["items"]] == [
        "Session Timeout/Idle timeout",
        "Session Timeout/Activity resets the clock",
        "Logout/Explicit logout",
    ]
    assert all(i["required"] for i in spec["items"])


def test_parse_openspec_delta_skips_removed(tmp_path):
    path = tmp_path / "spec.md"
    path.write_text(OPENSPEC_DELTA, encoding="utf-8")
    spec = runbook.parse_spec(str(path))
    got = {i["id"]: i["required"] for i in spec["items"]}
    assert got == {
        "Remember me/Remembered session survives a restart": True,
        "Session Timeout/Idle timeout": True,
        "Legacy token/Legacy token accepted": False,
    }


def test_parse_openspec_directory_prefixes_capabilities(tmp_path):
    for cap, text in (("auth", OPENSPEC_MAIN), ("billing", OPENSPEC_MAIN.replace("Logout", "Invoice"))):
        (tmp_path / "specs" / cap).mkdir(parents=True)
        (tmp_path / "specs" / cap / "spec.md").write_text(text, encoding="utf-8")
    spec = runbook.parse_spec(str(tmp_path / "specs"))
    ids = [i["id"] for i in spec["items"]]
    assert "auth/Session Timeout/Idle timeout" in ids
    assert "billing/Invoice/Explicit logout" in ids


def test_spec_with_nothing_to_cover_is_refused(tmp_path):
    path = tmp_path / "spec.md"
    path.write_text("# Notes\n\nNothing measurable here.\n", encoding="utf-8")
    rb = write(tmp_path, minimal())
    result = runbook.check(rb, spec_path=str(path))
    assert "SPEC-EMPTY" in codes(result)
    assert result["verdict"] == "FAIL"


# ----------------------------------------------------------------------------- coverage
def test_spec_kit_uncovered_criterion_fails(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC_KIT, encoding="utf-8")
    result = runbook.check(write(tmp_path, minimal()), spec_path=str(spec))
    assert result["verdict"] == "FAIL"
    assert [e["where"] for e in result["errors"] if e["code"] == "SPEC-UNCOVERED"] == ["SC-003"]
    assert result["coverage"]["covered"] == {"SC-001": ["build-exit"], "SC-002": ["p95"]}
    # functional requirements without a check are a warning, not a failure
    assert {w["where"] for w in result["warnings"] if w["code"] == "FR-UNCOVERED"} == {"FR-001", "FR-002"}
    assert any(w["code"] == "SPEC-CLARIFY" for w in result["warnings"])


def test_spec_kit_fully_covered_passes(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC_KIT, encoding="utf-8")
    data = minimal()
    data["stages"][1]["checks"][0]["covers"] = ["SC-002", "SC-003", "FR-001"]
    result = runbook.check(write(tmp_path, data), spec_path=str(spec))
    assert result["errors"] == [] and result["verdict"] == "PASS"


def test_covers_naming_nothing_in_the_spec_fails(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC_KIT, encoding="utf-8")
    data = minimal()
    data["stages"][1]["checks"][0]["covers"] = ["SC-002", "SC-003", "SC-017"]
    result = runbook.check(write(tmp_path, data), spec_path=str(spec))
    bad = [e for e in result["errors"] if e["code"] == "COVERS-UNKNOWN"]
    assert bad and "SC-017" in bad[0]["message"]


def test_owner_gate_covers_a_criterion(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC_KIT, encoding="utf-8")
    data = minimal()
    data["stages"].append({"id": "ship", "needs": ["load"], "owner_gate": {
        "approve": "the manager confirms the morning read rate", "covers": ["SC-003"]}})
    result = runbook.check(write(tmp_path, data), spec_path=str(spec))
    assert result["errors"] == []
    assert result["coverage"]["covered"]["SC-003"] == ["gate:ship"]


def openspec_runbook(covers):
    data = minimal()
    data["stages"][0]["checks"][0]["covers"] = covers
    data["stages"][1]["checks"][0]["covers"] = []
    return data


def test_openspec_uncovered_scenario_fails(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text(OPENSPEC_MAIN, encoding="utf-8")
    data = openspec_runbook(["Session Timeout/Idle timeout", "logout / explicit  LOGOUT"])
    result = runbook.check(write(tmp_path, data), spec_path=str(spec))
    assert result["verdict"] == "FAIL"
    assert [e["where"] for e in result["errors"] if e["code"] == "SPEC-UNCOVERED"] == [
        "Session Timeout/Activity resets the clock"]


def test_openspec_fully_covered_passes(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text(OPENSPEC_MAIN, encoding="utf-8")
    data = openspec_runbook(["Session Timeout/Idle timeout", "Session Timeout/Activity resets the clock",
                             "Logout/Explicit logout"])
    result = runbook.check(write(tmp_path, data), spec_path=str(spec))
    assert result["errors"] == [] and result["verdict"] == "PASS"


def test_openspec_delta_needs_no_check_for_removed(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text(OPENSPEC_DELTA, encoding="utf-8")
    data = openspec_runbook(["Remember me/Remembered session survives a restart",
                             "Session Timeout/Idle timeout"])
    result = runbook.check(write(tmp_path, data), spec_path=str(spec))
    assert result["errors"] == []


def test_openspec_directory_bare_key_must_be_unambiguous(tmp_path):
    for cap in ("auth", "admin"):
        (tmp_path / "specs" / cap).mkdir(parents=True)
        (tmp_path / "specs" / cap / "spec.md").write_text(OPENSPEC_MAIN, encoding="utf-8")
    data = openspec_runbook(["Logout/Explicit logout"])
    result = runbook.check(write(tmp_path, data), spec_path=str(tmp_path / "specs"))
    assert "COVERS-AMBIGUOUS" in codes(result)
    full = ["%s/%s" % (cap, key) for cap in ("auth", "admin") for key in (
        "Session Timeout/Idle timeout", "Session Timeout/Activity resets the clock",
        "Logout/Explicit logout")]
    result = runbook.check(write(tmp_path, openspec_runbook(full)), spec_path=str(tmp_path / "specs"))
    assert result["errors"] == []


def test_runbook_spec_field_is_used_without_flag(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC_KIT, encoding="utf-8")
    result = runbook.check(write(tmp_path, minimal(spec="spec.md")))
    assert [e["where"] for e in result["errors"] if e["code"] == "SPEC-UNCOVERED"] == ["SC-003"]
    result = runbook.check(write(tmp_path, minimal(spec="gone.md")))
    assert "SPEC-MISSING" in codes(result)


# ---------------------------------------------------------------------------------- CLI
def test_cli_pass_fail_blocked_and_usage(tmp_path, monkeypatch, capsys):
    good = write(tmp_path, minimal())
    rc, out = run_cli(tmp_path, monkeypatch, capsys, good)
    assert rc == verdict.PASS
    assert "GATE alpaca-runbook-check: PASS" in out.out

    bad = minimal()
    del bad["stages"][0]["run"]
    rc, out = run_cli(tmp_path, monkeypatch, capsys, write(tmp_path, bad, "bad.yaml"))
    assert rc == verdict.FAIL
    assert "RUN-MISSING stages[0]" in out.out
    assert "GATE alpaca-runbook-check: FAIL" in out.out

    rc, out = run_cli(tmp_path, monkeypatch, capsys, str(tmp_path / "absent.yaml"))
    assert rc == verdict.BLOCKED
    assert "cannot read" in out.out

    monkeypatch.chdir(tmp_path)
    assert cli.main(["runbook", "check"]) == verdict.USAGE


def test_cli_spec_flag_and_json(tmp_path, monkeypatch, capsys):
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC_KIT, encoding="utf-8")
    rc, out = run_cli(tmp_path, monkeypatch, capsys, write(tmp_path, minimal()),
                      "--spec", str(spec), "--json")
    assert rc == verdict.FAIL
    body = json.loads(out.out)
    assert set(surface.output_keys("runbook")) <= set(body)
    assert body["verdict"] == "FAIL"
    assert body["coverage"]["missing"] == ["SC-003"]


def test_cli_writes_no_record(tmp_path, monkeypatch, capsys):
    run_cli(tmp_path, monkeypatch, capsys, write(tmp_path, minimal()))
    assert not (tmp_path / ".alpaca").exists()


def test_verb_is_declared_and_documented():
    assert "runbook" in cli.registered_verbs()
    assert "runbook" in surface.TABLE
    assert "runbook" in surface.documented(REPO)
    assert [f for f in surface.lint(REPO) if f["verb"] == "runbook"] == []


# ----------------------------------------------------------------------- check semantics
def test_evaluate_exit_code(tmp_path):
    chk = {"id": "c", "type": "exit-code"}
    assert runbook.evaluate(chk, workdir=str(tmp_path), exit_code=0)[0] == verdict.PASS
    assert runbook.evaluate(chk, workdir=str(tmp_path), exit_code=2)[0] == verdict.FAIL
    assert runbook.evaluate(chk, workdir=str(tmp_path), exit_code=None)[0] == verdict.BLOCKED
    assert runbook.evaluate(dict(chk, expect=3), workdir=str(tmp_path), exit_code=3)[0] == verdict.PASS


def test_evaluate_file_exists(tmp_path):
    chk = {"id": "c", "type": "file-exists", "path": "out/a.bin"}
    assert runbook.evaluate(chk, workdir=str(tmp_path))[0] == verdict.FAIL
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "a.bin").write_bytes(b"")
    code, reason = runbook.evaluate(chk, workdir=str(tmp_path))
    assert code == verdict.FAIL and "empty" in reason
    assert runbook.evaluate(dict(chk, non_empty=False), workdir=str(tmp_path))[0] == verdict.PASS
    (tmp_path / "out" / "a.bin").write_bytes(b"x")
    assert runbook.evaluate(chk, workdir=str(tmp_path))[0] == verdict.PASS


def test_evaluate_regex_in_file(tmp_path):
    (tmp_path / "log.txt").write_text("12 passed, 0 failed\n", encoding="utf-8")
    chk = {"id": "c", "type": "regex-in-file", "path": "log.txt", "pattern": r"\b0 failed"}
    assert runbook.evaluate(chk, workdir=str(tmp_path))[0] == verdict.PASS
    assert runbook.evaluate(dict(chk, absent=True), workdir=str(tmp_path))[0] == verdict.FAIL
    assert runbook.evaluate(dict(chk, pattern="ERROR"), workdir=str(tmp_path))[0] == verdict.FAIL
    assert runbook.evaluate(dict(chk, pattern="ERROR", absent=True), workdir=str(tmp_path))[0] == verdict.PASS
    assert runbook.evaluate(dict(chk, pattern="PASSED", ignore_case=True), workdir=str(tmp_path))[0] == verdict.PASS
    assert runbook.evaluate(dict(chk, path="none.txt", absent=True), workdir=str(tmp_path))[0] == verdict.FAIL


def test_evaluate_json_field(tmp_path):
    (tmp_path / "m.json").write_text(json.dumps(
        {"redirect": {"p95_ms": 41.5}, "runs": [{"lost": 0}], "bad": float("nan")}), encoding="utf-8")
    base = {"id": "c", "type": "json-field", "path": "m.json"}
    ev = lambda **kw: runbook.evaluate(dict(base, **kw), workdir=str(tmp_path),
                                       knobs={"LIMIT": 40})[0]
    assert ev(field="redirect.p95_ms", op="<=", value=50) == verdict.PASS
    assert ev(field="redirect.p95_ms", op="<=", value="${LIMIT}") == verdict.FAIL
    assert ev(field="runs.0.lost", op="==", value=0) == verdict.PASS
    assert ev(field="redirect.p99_ms", op="<=", value=50) == verdict.FAIL
    assert ev(field="bad", op="<", value=1) == verdict.FAIL
    assert ev(field="redirect", op=">", value=1) == verdict.FAIL
    assert runbook.evaluate(dict(base, path="none.json", field="a", op="==", value=1),
                            workdir=str(tmp_path))[0] == verdict.FAIL


def plugin(tmp_path, body):
    script = tmp_path / "plug.py"
    script.write_text("#!/usr/bin/env python3\nimport os, sys\n" + textwrap.dedent(body), encoding="utf-8")
    script.chmod(0o755)
    return {"id": "c", "type": "plugin", "script": "plug.py"}


@pytest.mark.parametrize("rc,want", [(0, verdict.PASS), (1, verdict.FAIL), (2, verdict.BLOCKED),
                                     (3, verdict.PAUSED), (7, verdict.BLOCKED)])
def test_evaluate_plugin_exit_codes(tmp_path, rc, want):
    chk = plugin(tmp_path, "print('first'); print('the reason'); sys.exit(%d)\n" % rc)
    code, reason = runbook.evaluate(chk, workdir=str(tmp_path), runbook_dir=str(tmp_path))
    assert code == want
    assert "the reason" in reason


def test_evaluate_plugin_gets_args_env_and_cwd(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    chk = plugin(tmp_path, """\
        ok = (sys.argv[1:] == ["a", "8"] and os.environ["ALPACA_STAGE"] == "load"
              and os.environ["ALPACA_CHECK"] == "c" and os.environ["ALPACA_ATTEMPT"] == "2"
              and os.environ["ALPACA_KNOB_WORKERS"] == "8"
              and os.environ["ALPACA_RUNBOOK_DIR"] == os.path.dirname(os.path.abspath(__file__))
              and os.getcwd() == os.environ["EXPECT_CWD"])
        print("env ok" if ok else "env wrong: %r" % (sys.argv,))
        sys.exit(0 if ok else 1)
        """)
    chk["args"] = ["a", "${WORKERS}"]
    os.environ["EXPECT_CWD"] = str(work)
    try:
        code, reason = runbook.evaluate(chk, workdir=str(work), runbook_dir=str(tmp_path), stage="load",
                                        attempt=2, knobs={"WORKERS": 8})
    finally:
        del os.environ["EXPECT_CWD"]
    assert (code, reason) == (verdict.PASS, "env ok")


def test_evaluate_plugin_timeout_is_blocked(tmp_path):
    chk = plugin(tmp_path, "import time; time.sleep(5)\n")
    chk["timeout"] = 1
    code, reason = runbook.evaluate(chk, workdir=str(tmp_path), runbook_dir=str(tmp_path))
    assert code == verdict.BLOCKED and "timed out" in reason


def test_example_plugin_on_sample_results(tmp_path):
    chk = {"id": "c", "type": "plugin", "script": "checks/status_codes.py",
           "args": ["load.json", "301"]}
    (tmp_path / "load.json").write_text(json.dumps({"status": {"301": 6000}}), encoding="utf-8")
    assert runbook.evaluate(chk, workdir=str(tmp_path), runbook_dir=EXAMPLE)[0] == verdict.PASS
    (tmp_path / "load.json").write_text(json.dumps({"status": {"301": 5990, "502": 10}}), encoding="utf-8")
    code, reason = runbook.evaluate(chk, workdir=str(tmp_path), runbook_dir=EXAMPLE)
    assert code == verdict.FAIL and "502" in reason
    (tmp_path / "load.json").unlink()
    assert runbook.evaluate(chk, workdir=str(tmp_path), runbook_dir=EXAMPLE)[0] == verdict.BLOCKED


# ------------------------------------------------------------------------- retry rules
def load_stage():
    data = minimal()
    data["stages"][1]["checks"].append({"id": "codes", "type": "exit-code"})
    data["stages"][1]["retry"]["stop_on"] = ["codes"]
    return data, data["stages"][1]


def test_retry_moves_the_knob_until_the_attempts_run_out():
    data, stage = load_stage()
    knobs = runbook.knob_values(data)
    step = runbook.next_attempt(data, stage, {"p95": verdict.FAIL, "codes": verdict.PASS}, 1, knobs)
    assert step["retry"] and step["knobs"]["WORKERS"] == 4
    step = runbook.next_attempt(data, stage, {"p95": verdict.FAIL, "codes": verdict.PASS}, 2, step["knobs"])
    assert step["retry"] and step["knobs"]["WORKERS"] == 6
    step = runbook.next_attempt(data, stage, {"p95": verdict.FAIL, "codes": verdict.PASS}, 3, step["knobs"])
    assert not step["retry"] and "3 of 3" in step["reason"]


def test_retry_stops_on_pass_stop_on_blocked_and_range():
    data, stage = load_stage()
    knobs = runbook.knob_values(data)
    assert not runbook.next_attempt(data, stage, {"p95": verdict.PASS, "codes": verdict.PASS}, 1, knobs)["retry"]
    step = runbook.next_attempt(data, stage, {"p95": verdict.FAIL, "codes": verdict.FAIL}, 1, knobs)
    assert not step["retry"] and "codes" in step["reason"]
    step = runbook.next_attempt(data, stage, {"p95": verdict.BLOCKED, "codes": verdict.PASS}, 1, knobs)
    assert not step["retry"] and "BLOCKED" in step["reason"]
    step = runbook.next_attempt(data, stage, {"p95": verdict.FAIL, "codes": verdict.PASS}, 1, {"WORKERS": 8})
    assert not step["retry"] and "range" in step["reason"]


def test_retry_only_for_listed_checks():
    data = minimal()
    stage = data["stages"][1]
    stage["checks"].append({"id": "other", "type": "exit-code"})
    step = runbook.next_attempt(data, stage, {"p95": verdict.PASS, "other": verdict.FAIL}, 1,
                                runbook.knob_values(data))
    assert not step["retry"] and "other" in step["reason"]


def test_no_retry_block_means_one_attempt():
    data = minimal()
    step = runbook.next_attempt(data, data["stages"][0], {"build-exit": verdict.FAIL}, 1,
                                runbook.knob_values(data))
    assert not step["retry"]


# ---------------------------------------------------------------- the format document
def test_format_doc_names_every_field_and_check_type():
    with open(os.path.join(REPO, "docs", "runbook-format.md"), encoding="utf-8") as fh:
        doc = fh.read()
    for table in (runbook.TOP_KEYS, runbook.KNOB_KEYS, runbook.STAGE_KEYS, runbook.CHECK_KEYS,
                  runbook.RETRY_KEYS, runbook.MOVE_KEYS, runbook.GATE_KEYS, runbook.OUTPUT_KEYS,
                  runbook.FAIL_KEYS):
        for key in table:
            assert "`%s`" % key in doc, key
    for kind, keys in runbook.TYPE_KEYS.items():
        assert "`%s`" % kind in doc, kind
        for key in keys:
            assert "`%s`" % key in doc, (kind, key)
    for code in runbook.ERROR_CODES + runbook.WARNING_CODES:
        assert "`%s`" % code in doc, code


def test_format_doc_ships():
    with open(os.path.join(REPO, "ALPACA-MANIFEST"), encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    assert "docs/runbook-format.md" in lines


def test_forge_skill_ships():
    path = os.path.join(REPO, ".claude", "skills", "alpaca-runbook-forge", "SKILL.md")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    assert text.startswith("---\nname: alpaca-runbook-forge\n")
    assert "alpaca runbook check" in text
    assert "docs/runbook-format.md" in text


def test_added_files_are_ascii_and_free_of_em_dash():
    paths = [os.path.join(REPO, "docs", "runbook-format.md"),
             os.path.join(REPO, ".claude", "skills", "alpaca-runbook-forge", "SKILL.md"),
             os.path.join(REPO, "alpaca", "runbook.py")]
    for base, _dirs, names in os.walk(EXAMPLE):
        paths += [os.path.join(base, n) for n in names if not n.endswith(".pyc")]
    for path in paths:
        with open(path, "rb") as fh:
            data = fh.read()
        assert all(b < 128 for b in data), path


# ------------------------------------------------------------ review fixes (t-007 review)
# M1: a success criterion written in another common shape must not drop out of coverage.
@pytest.mark.parametrize("line", [
    "- **SC-001**: the summary is mailed",
    "- **SC-001:** the summary is mailed",
    "* **SC-001**: the summary is mailed",
    "+ **SC-001**: the summary is mailed",
    "- SC-001: the summary is mailed",
    "1. **SC-001**: the summary is mailed",
    "1) SC-001: the summary is mailed",
    "**SC-001**: the summary is mailed",
    "**SC-001:** the summary is mailed",
    "SC-001: the summary is mailed",
    "### SC-001: the summary is mailed",
    "| SC-001 | the summary is mailed |",
    "| **SC-001** | the summary is mailed |",
])
def test_spec_kit_item_shapes(tmp_path, line):
    path = tmp_path / "spec.md"
    path.write_text("# Feature Specification: x\n\n## Success Criteria\n\n%s\n" % line, encoding="utf-8")
    spec = runbook.parse_spec(str(path))
    assert [(i["id"], i["required"]) for i in spec["items"]] == [("SC-001", True)]
    assert spec["items"][0]["text"] == "the summary is mailed"
    assert not spec["items"][0].get("loose")


REVIEW_MIXED_SPEC = """\
# Feature Specification: x
## Success Criteria
- **SC-001**: one
- SC-002: two, written without bold
1. **SC-003**: numbered list
**SC-004**: no bullet
| SC-005 | table row |
"""


def test_review_probe_mixed_spec_fails_on_the_uncovered_criteria(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text(REVIEW_MIXED_SPEC, encoding="utf-8")
    data = minimal()
    data["stages"][1]["checks"][0]["covers"] = ["SC-001"]
    result = runbook.check(write(tmp_path, data), spec_path=str(spec))
    assert result["verdict"] == "FAIL"
    assert result["spec"]["required"] == 5
    assert result["coverage"]["missing"] == ["SC-002", "SC-003", "SC-004", "SC-005"]


def test_an_id_seen_outside_a_known_shape_still_counts(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text("# Feature Specification: x\n\n## Success Criteria\n\n- **SC-001**: one\n\n"
                    "Also, SC-002 holds: the export finishes before FR-009 runs.\n", encoding="utf-8")
    parsed = runbook.parse_spec(str(spec))
    got = {i["id"]: (i["required"], bool(i.get("loose"))) for i in parsed["items"]}
    assert got == {"SC-001": (True, False), "SC-002": (True, True), "FR-009": (False, True)}
    data = minimal()
    data["stages"][1]["checks"][0]["covers"] = ["FR-009"]
    result = runbook.check(write(tmp_path, data), spec_path=str(spec))
    assert result["verdict"] == "FAIL"        # SC-002 is loose, but required and uncovered
    assert codes(result) == ["SPEC-UNCOVERED"]
    assert result["coverage"]["missing"] == ["SC-002"]
    assert "SPEC-UNPARSED" in runbook.WARNING_CODES
    loose = [w for w in result["warnings"] if w["code"] == "SPEC-UNPARSED"]
    assert {w["where"] for w in loose} == {"SC-002", "FR-009"}
    assert "line 7" in loose[0]["message"]


def test_ids_in_comments_and_fences_are_not_items(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text("# Feature Specification: x\n\n## Success Criteria\n\n- **SC-001**: one\n"
                    "<!-- - **SC-009**: an example in a one-line comment -->\n"
                    "```\nSC-010: fenced\n```\n", encoding="utf-8")
    assert [i["id"] for i in runbook.parse_spec(str(spec))["items"]] == ["SC-001"]


# M2: `bin/alpaca` changes folder to the install root before Python starts; relative paths on
# the command line must still be read from the folder the command was run in.
def _install_copy(tmp_path):
    inst = tmp_path / "inst"
    (inst / "bin").mkdir(parents=True)
    for name in ("alpaca", "alpaca-python"):
        shutil.copy2(os.path.join(REPO, "bin", name), str(inst / "bin" / name))
    os.symlink(os.path.join(REPO, "alpaca"), str(inst / "alpaca"))
    py = inst / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text('#!/bin/sh\nexec "%s" "$@"\n' % sys.executable, encoding="utf-8")
    py.chmod(0o755)
    return inst


def test_bin_alpaca_reads_relative_paths_from_the_caller_folder(tmp_path):
    inst = _install_copy(tmp_path)
    domain = inst / "domains" / "report"
    domain.mkdir(parents=True)
    (domain / "spec.md").write_text(SPEC_KIT, encoding="utf-8")
    data = minimal()
    data["stages"][1]["checks"][0]["covers"] = ["SC-002", "SC-003"]
    write(domain, data)
    env = {k: v for k, v in os.environ.items() if not k.startswith("ALPACA_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run([str(inst / "bin" / "alpaca"), "runbook", "check", "runbook.yaml",
                           "--spec", "spec.md"], cwd=str(domain), env=env, capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == verdict.PASS, proc.stdout + proc.stderr
    assert "runbook: runbook.yaml" in proc.stdout
    assert "spec: spec.md (spec-kit" in proc.stdout
    assert "GATE alpaca-runbook-check: PASS" in proc.stdout
    assert not (inst / ".alpaca").exists() and not (domain / ".alpaca").exists()


def test_caller_folder_is_ignored_when_python_runs_elsewhere(tmp_path, monkeypatch, capsys):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("ALPACA_CALLER_CWD", str(other))
    write(tmp_path, minimal())
    rc, out = run_cli(tmp_path, monkeypatch, capsys, "runbook.yaml")
    assert rc == verdict.PASS, out.out


# L1: a json-field compares a bool only with a bool.
def test_json_field_bool_is_not_a_number(tmp_path):
    (tmp_path / "m.json").write_text(json.dumps({"ok": True, "off": False, "n": 1, "z": 0}),
                                     encoding="utf-8")
    base = {"id": "c", "type": "json-field", "path": "m.json"}
    ev = lambda **kw: runbook.evaluate(dict(base, **kw), workdir=str(tmp_path))[0]
    assert ev(field="ok", op="==", value=1) == verdict.FAIL
    assert ev(field="off", op="==", value=0) == verdict.FAIL
    assert ev(field="n", op="==", value=True) == verdict.FAIL
    assert ev(field="z", op="!=", value=False) == verdict.PASS
    assert ev(field="ok", op="==", value=True) == verdict.PASS
    assert ev(field="ok", op="!=", value=1) == verdict.PASS
    assert ev(field="n", op="==", value=1.0) == verdict.PASS


# L2: a path that uses a variable is checked again once the variable is replaced.
def test_evaluate_path_variable_may_not_leave_the_runbook_folder(tmp_path):
    book = tmp_path / "book"
    evidence = tmp_path / "evidence"
    book.mkdir()
    evidence.mkdir()
    (tmp_path / "outside.txt").write_text("x", encoding="utf-8")
    (evidence / "run.txt").write_text("x", encoding="utf-8")
    (book / "in.txt").write_text("x", encoding="utf-8")
    chk = {"id": "c", "type": "file-exists", "path": "${F}"}
    ev = lambda path, **kn: runbook.evaluate(dict(chk, path=path), workdir=str(book), runbook_dir=str(book),
                                             knobs=kn, evidence_dir=str(evidence))
    code, reason = ev("${F}", F=str(tmp_path / "outside.txt"))
    assert code == verdict.BLOCKED and "outside the runbook folder" in reason
    assert ev("${F}", F="../outside.txt")[0] == verdict.BLOCKED
    assert ev("${F}", F="in.txt")[0] == verdict.PASS
    assert ev("${RUNBOOK_DIR}/in.txt")[0] == verdict.PASS
    assert ev("${EVIDENCE_DIR}/run.txt")[0] == verdict.PASS
    assert ev("${EVIDENCE_DIR}/../outside.txt")[0] == verdict.BLOCKED


# L3: regex-in-file searches the whole file at once, `^` and `$` at each line (pinned, as the doc says).
def test_regex_in_file_searches_the_whole_file(tmp_path):
    (tmp_path / "log.txt").write_text("total\n12\n", encoding="utf-8")
    chk = {"id": "c", "type": "regex-in-file", "path": "log.txt", "pattern": r"total\s+12$"}
    assert runbook.evaluate(chk, workdir=str(tmp_path))[0] == verdict.PASS
    assert runbook.evaluate(dict(chk, pattern=r"^12$"), workdir=str(tmp_path))[0] == verdict.PASS
    with open(os.path.join(REPO, "docs", "runbook-format.md"), encoding="utf-8") as fh:
        assert "searched line by line" not in fh.read()


# L4: a json-field value with a partial ${NAME} is checked and replaced like a whole one.
def test_json_field_value_with_a_partial_variable(tmp_path):
    data = minimal()
    data["stages"][1]["checks"][0].update(op="==", value="v${NOPE}")
    result = runbook.check(write(tmp_path, data))
    assert [e["where"] for e in result["errors"] if e["code"] == "VAR-UNKNOWN"] == [
        "stages[1].checks[0].value"]
    (tmp_path / "m.json").write_text(json.dumps({"version": "v3"}), encoding="utf-8")
    chk = {"id": "c", "type": "json-field", "path": "m.json", "field": "version", "op": "==",
           "value": "v${RELEASE}"}
    assert runbook.evaluate(chk, workdir=str(tmp_path), knobs={"RELEASE": 3})[0] == verdict.PASS
    assert runbook.evaluate(chk, workdir=str(tmp_path), knobs={"RELEASE": 4})[0] == verdict.FAIL


# L5: the model plugin answers BLOCKED, with a reason, for a count that is not a whole number.
def test_example_plugin_blocks_on_a_count_that_is_not_whole(tmp_path):
    chk = {"id": "c", "type": "plugin", "script": "checks/status_codes.py",
           "args": ["load.json", "301"]}
    for counts in ({"301": 5, "502": "3"}, {"301": 5.5}, {"301": True}, {"301": -1}):
        (tmp_path / "load.json").write_text(json.dumps({"status": counts}), encoding="utf-8")
        code, reason = runbook.evaluate(chk, workdir=str(tmp_path), runbook_dir=EXAMPLE)
        assert code == verdict.BLOCKED, (counts, reason)
        assert "whole number" in reason, reason


# ================================================================= format 2 (op-003 part A)
# `runbook: 2` adds numbered edge cases (EC-nnn) as spec items, known failures with a detection
# and a response (`detect`, `then`, recovery stages), and provenance (`source`). A `runbook: 1`
# file reads and checks as before; the only new thing it can get is the EC-IGNORED warning.
SPEC_EC = """\
# Feature Specification: Nightly report

## User Scenarios & Testing

### Edge Cases

- **EC-001**: An empty day mails a summary that says no orders.
- **EC-002**: A day with more than 100000 orders still finishes.

## Success Criteria

- **SC-001**: The summary counts every order of the day.
- **SC-002**: The job finishes in under 15 minutes.
"""


def minimal2(**over):
    """minimal() as format 2."""
    return minimal(**dict({"runbook": 2}, **over))


def spec_file(tmp_path, text, name="spec.md"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def ec_runbook():
    """Format 2, covering SC-001 and EC-001 by checks and EC-002 by a fail case."""
    data = minimal2()
    data["stages"][0]["checks"][0]["covers"] = ["SC-001", "EC-001"]
    data["stages"].append({"id": "free-port", "recovery": True, "run": "make free-port",
                           "checks": [{"id": "port-free", "type": "exit-code"}]})
    data["stages"][1]["fails"] = [{"id": "too-many", "when": "the day holds too many orders",
                                   "detect": {"type": "regex-in-file", "path": "out/load.log",
                                              "pattern": "MemoryError"},
                                   "then": "stop", "covers": ["EC-002"]}]
    return data


def warn_codes(result):
    """The warning codes, without COVERS-WITHOUT-SPEC (a check with `covers` read with no spec)."""
    return [w["code"] for w in result["warnings"] if w["code"] != "COVERS-WITHOUT-SPEC"]


def where_codes(result, key="errors"):
    return {(e["code"], e["where"]) for e in result[key]}


# ------------------------------------------------------------------------ the version
def test_format_2_is_the_newest_and_format_1_still_reads(tmp_path):
    assert runbook.FORMAT == 2 and runbook.FORMATS == (1, 2)
    assert runbook.check(write(tmp_path, minimal2()))["errors"] == []
    assert runbook.check(write(tmp_path, minimal()))["errors"] == []
    result = runbook.check(write(tmp_path, minimal(runbook=3)))
    assert codes(result) == ["VERSION-UNSUPPORTED"]
    assert "runbook: 2" in result["errors"][0]["message"]


def test_format_1_refuses_the_format_2_fields(tmp_path):
    data = minimal()
    data["knobs"][0]["source"] = "default"
    data["stages"][0]["recovery"] = False
    data["stages"][0]["checks"][0]["source"] = "spec:SC-001"
    data["stages"][1]["fails"] = [{"id": "f", "when": "w", "detect": {"type": "exit-code"},
                                   "then": "stop", "covers": ["SC-002"], "source": "default"}]
    data["stages"].append({"id": "ship", "owner_gate": {"approve": "the owner ships", "source": "owner:x"}})
    result = runbook.check(write(tmp_path, data))
    unknown = {w for c, w in where_codes(result) if c == "FIELD-UNKNOWN"}
    assert unknown == {"knobs[0].source", "stages[0].recovery", "stages[0].checks[0].source",
                       "stages[1].fails[0].detect", "stages[1].fails[0].then", "stages[1].fails[0].covers",
                       "stages[1].fails[0].source", "stages[2].owner_gate.source"}, result["errors"]
    assert set(codes(result)) == {"FIELD-UNKNOWN"}
    # the same file as format 2 is clean
    data["runbook"] = 2
    assert runbook.check(write(tmp_path, data))["errors"] == []


# ------------------------------------------------------------------ A1: edge cases
@pytest.mark.parametrize("line", [
    "- **EC-001**: an empty day mails an empty summary",
    "- **EC-001:** an empty day mails an empty summary",
    "1. **EC-001**: an empty day mails an empty summary",
    "EC-001: an empty day mails an empty summary",
    "### EC-001: an empty day mails an empty summary",
    "| EC-001 | an empty day mails an empty summary |",
])
def test_edge_case_item_shapes(tmp_path, line):
    path = spec_file(tmp_path, "# Feature Specification: x\n\n## Success Criteria\n\n- **SC-001**: one\n\n"
                               "### Edge Cases\n\n%s\n" % line)
    spec = runbook.parse_spec(path)
    # the items of format 1 are what they always were
    assert [i["id"] for i in spec["items"]] == ["SC-001"]
    assert [(i["id"], i["kind"], i["required"], i["text"]) for i in spec["edge_cases"]] == [
        ("EC-001", "EC", True, "an empty day mails an empty summary")]
    assert not spec["edge_cases"][0].get("loose")
    assert [i["id"] for i in runbook.spec_items(spec, 2)] == ["EC-001", "SC-001"]
    assert [i["id"] for i in runbook.spec_items(spec, 1)] == ["SC-001"]
    assert spec["unnumbered"] == []


def test_edge_cases_are_required_items_in_format_2(tmp_path):
    spec = spec_file(tmp_path, SPEC_EC)
    result = runbook.check(write(tmp_path, ec_runbook()), spec_path=spec)
    assert result["errors"] == [], result["errors"]
    assert result["verdict"] == "PASS"
    assert result["spec"]["required"] == 4 and result["spec"]["items"] == 4
    cov = result["coverage"]
    assert cov["covered"]["EC-001"] == ["build-exit"]
    # a fail case that detects and answers an edge case covers it
    assert cov["covered"]["EC-002"] == ["fail:load/too-many"]
    assert result["warnings"] == []


def test_an_uncovered_edge_case_fails_like_a_criterion(tmp_path):
    spec = spec_file(tmp_path, SPEC_EC)
    data = ec_runbook()
    del data["stages"][1]["fails"][0]["covers"]
    result = runbook.check(write(tmp_path, data), spec_path=spec)
    assert result["verdict"] == "FAIL"
    assert [e["where"] for e in result["errors"] if e["code"] == "SPEC-UNCOVERED"] == ["EC-002"]
    assert result["coverage"]["missing"] == ["EC-002"]
    msg = [e for e in result["errors"] if e["code"] == "SPEC-UNCOVERED"][0]["message"]
    assert "no runbook check covers EC-002" in msg and "fail case" in msg


def test_a_loose_edge_case_id_still_counts(tmp_path):
    spec = spec_file(tmp_path, SPEC_EC + "\n## Assumptions\n\nAlso EC-003 matters: a holiday has no orders.\n")
    result = runbook.check(write(tmp_path, ec_runbook()), spec_path=spec)
    assert result["coverage"]["missing"] == ["EC-003"]
    loose = [w for w in result["warnings"] if w["code"] == "SPEC-UNPARSED"]
    assert [w["where"] for w in loose] == ["EC-003"]
    assert "a required edge case" in loose[0]["message"] and "line 17" in loose[0]["message"]


def test_an_unnumbered_edge_case_bullet_is_an_error(tmp_path):
    text = SPEC_EC.replace("- **EC-002**: A day with", "- A day with")
    text = text.replace("says no orders.\n", "says no orders.\n  - a nested detail line is not an edge case\n")
    spec = spec_file(tmp_path, text)
    data = ec_runbook()
    del data["stages"][1]["fails"][0]["covers"]
    result = runbook.check(write(tmp_path, data), spec_path=spec)
    assert result["verdict"] == "FAIL"
    bad = [e for e in result["errors"] if e["code"] == "EC-UNNUMBERED"]
    assert [e["where"] for e in bad] == ["%s:9" % spec], result["errors"]
    assert "line 9" in bad[0]["message"] and "A day with more than 100000 orders" in bad[0]["message"]
    assert "EC-001" in bad[0]["message"]
    # the reader never numbers a bullet by its position
    assert [i["id"] for i in runbook.parse_spec(spec)["edge_cases"]] == ["EC-001"]


def test_edge_case_heading_levels_and_section_end(tmp_path):
    text = ("# Feature Specification: x\n\n## Edge Cases\n\n- **EC-001**: one\n- unnumbered two\n\n"
            "## Success Criteria\n\n- **SC-001**: a bullet outside the section\n- not an edge case\n")
    spec = runbook.parse_spec(spec_file(tmp_path, text))
    assert [u["line"] for u in spec["unnumbered"]] == [6]
    assert spec["edge_case_bullets"] == 2


def test_format_1_ignores_edge_cases_with_one_warning(tmp_path):
    text = SPEC_EC.replace("- **EC-002**: A day with", "- A day with")
    spec = spec_file(tmp_path, text)
    result = runbook.check(write(tmp_path, minimal()), spec_path=spec)
    assert result["errors"] == [] and result["verdict"] == "PASS"
    assert [(w["code"], w["where"]) for w in result["warnings"]] == [("EC-IGNORED", spec)]
    assert "format 1 does not cover edge cases; move to runbook: 2" in result["warnings"][0]["message"]
    assert result["spec"]["required"] == 2
    # an EC id is not an item in format 1
    data = minimal()
    data["stages"][0]["checks"][0]["covers"] = ["SC-001", "EC-001"]
    assert "COVERS-UNKNOWN" in codes(runbook.check(write(tmp_path, data), spec_path=spec))


def test_format_1_example_fixture_still_passes_with_only_the_ec_warning():
    result = runbook.check(os.path.join(EXAMPLE_V1, "runbook.yaml"))
    assert result["errors"] == [] and result["verdict"] == "PASS"
    assert [w["code"] for w in result["warnings"]] == ["EC-IGNORED"]
    assert sorted(result["coverage"]["covered"]) == ["SC-001", "SC-002", "SC-003", "SC-004"]


def test_openspec_reads_the_same_under_format_2(tmp_path):
    spec = spec_file(tmp_path, OPENSPEC_MAIN)
    covers = ["Session Timeout/Idle timeout", "Session Timeout/Activity resets the clock",
              "Logout/Explicit logout"]
    for fmt in (1, 2):
        data = openspec_runbook(covers)
        data["runbook"] = fmt
        result = runbook.check(write(tmp_path, data), spec_path=spec)
        assert result["errors"] == [] and result["warnings"] == [], fmt
        assert result["spec"]["required"] == 3


# ------------------------------------------------------------ A2: detect and then
def fails2(**fail):
    """Format 2 with a recovery stage `free-port` and one fail case on stage `load`."""
    data = minimal2()
    data["stages"].append({"id": "free-port", "recovery": True, "run": "make free-port",
                           "checks": [{"id": "port-free", "type": "exit-code"}]})
    data["stages"][1]["fails"] = [dict({"id": "port-busy", "when": "the port is taken"}, **fail)]
    return data


DETECT = {"type": "regex-in-file", "path": "out/load.log", "pattern": "Address already in use"}


@pytest.mark.parametrize("then", ["retry", "stop", "ask-owner", {"run": "free-port"}])
def test_a_fail_case_with_detect_and_then_passes(tmp_path, then):
    result = runbook.check(write(tmp_path, fails2(detect=DETECT, then=then)))
    assert result["errors"] == [] and result["verdict"] == "PASS", result["errors"]


def test_a_fail_case_without_detect_keeps_its_format_1_shape(tmp_path):
    assert runbook.check(write(tmp_path, fails2()))["errors"] == []


@pytest.mark.parametrize("fail,code,where", [
    ({"detect": DETECT}, "THEN-MISSING", "stages[1].fails[0].then"),
    ({"detect": DETECT, "then": "again"}, "THEN-UNKNOWN", "stages[1].fails[0].then"),
    ({"detect": DETECT, "then": {"run": "free-port", "wait": 5}}, "THEN-UNKNOWN", "stages[1].fails[0].then"),
    ({"detect": DETECT, "then": {"run": 5}}, "THEN-UNKNOWN", "stages[1].fails[0].then"),
    ({"detect": DETECT, "then": {"run": "nowhere"}}, "RECOVERY-UNKNOWN", "stages[1].fails[0].then.run"),
    ({"detect": DETECT, "then": {"run": "build"}}, "RECOVERY-NOT-MARKED", "stages[1].fails[0].then.run"),
    ({"detect": {"type": "regex-in-file", "path": "out/load.log"}, "then": "stop"},
     "DETECT-INVALID", "stages[1].fails[0].detect.pattern"),
    ({"detect": dict(DETECT, id="d"), "then": "stop"}, "DETECT-INVALID", "stages[1].fails[0].detect.id"),
    ({"detect": dict(DETECT, covers=["SC-001"]), "then": "stop"}, "DETECT-INVALID",
     "stages[1].fails[0].detect.covers"),
    ({"detect": dict(DETECT, source="default"), "then": "stop"}, "DETECT-INVALID",
     "stages[1].fails[0].detect.source"),
    ({"detect": dict(DETECT, pattern="(unclosed"), "then": "stop"}, "DETECT-INVALID",
     "stages[1].fails[0].detect.pattern"),
    ({"detect": {"type": "smoke-signal"}, "then": "stop"}, "DETECT-INVALID", "stages[1].fails[0].detect.type"),
    ({"detect": "grep -q busy out/load.log", "then": "stop"}, "DETECT-INVALID", "stages[1].fails[0].detect"),
    ({"detect": {"type": "plugin", "script": "checks/none.py"}, "then": "stop"}, "DETECT-INVALID",
     "stages[1].fails[0].detect.script"),
    ({"covers": "SC-001"}, "FIELD-NOT-LIST", "stages[1].fails[0].covers"),
])
def test_fail_case_refusals(tmp_path, fail, code, where):
    result = runbook.check(write(tmp_path, fails2(**fail)))
    assert (code, where) in where_codes(result), result["errors"]
    assert result["verdict"] == "FAIL"


def test_detect_invalid_names_the_check_rule_it_broke(tmp_path):
    result = runbook.check(write(tmp_path, fails2(detect={"type": "regex-in-file", "path": "out/load.log"},
                                                  then="stop")))
    bad = [e for e in result["errors"] if e["code"] == "DETECT-INVALID"]
    assert len(bad) == 1 and "FIELD-MISSING" in bad[0]["message"] and "pattern" in bad[0]["message"]


def test_no_stage_may_need_a_recovery_stage_and_a_recovery_stage_needs_nothing(tmp_path):
    data = fails2()
    data["stages"].insert(2, {"id": "report", "needs": ["free-port"], "run": "make report",
                              "checks": [{"id": "report-exit", "type": "exit-code"}]})
    data["stages"][3]["needs"] = ["build"]
    result = runbook.check(write(tmp_path, data))
    got = where_codes(result)
    assert ("RECOVERY-NEEDED", "stages[2].needs[0]") in got, result["errors"]
    assert ("RECOVERY-NEEDED", "stages[3].needs") in got
    assert "NEEDS-UNKNOWN" not in codes(result)
    # a recovery stage declared earlier is still refused as a need, not taken as a run-order stage
    data = fails2()
    data["stages"].insert(0, data["stages"].pop())
    data["stages"][1]["needs"] = ["free-port"]
    assert ("RECOVERY-NEEDED", "stages[1].needs[0]") in where_codes(runbook.check(write(tmp_path, data)))


def test_a_recovery_stage_may_not_send_to_itself(tmp_path):
    data = fails2()
    data["stages"][2]["fails"] = [{"id": "still-busy", "when": "w", "detect": DETECT,
                                   "then": {"run": "free-port"}}]
    result = runbook.check(write(tmp_path, data))
    assert where_codes(result) == {("RECOVERY-SELF", "stages[2].fails[0].then.run")}, result["errors"]


def test_a_recovery_stage_runs_a_command_and_is_never_an_owner_gate(tmp_path):
    data = fails2()
    del data["stages"][2]["run"]
    del data["stages"][2]["checks"]
    data["stages"][2]["owner_gate"] = {"approve": "the owner frees the port"}
    got = where_codes(runbook.check(write(tmp_path, data)))
    assert ("RUN-MISSING", "stages[2]") in got and ("FIELD-UNKNOWN", "stages[2].owner_gate") in got, got
    data = fails2()
    data["stages"][2]["checks"] = []
    assert ("CHECKS-EMPTY", "stages[2].checks") in where_codes(runbook.check(write(tmp_path, data)))
    data = fails2()
    data["stages"][2]["recovery"] = "yes"
    assert ("FIELD-TYPE", "stages[2].recovery") in where_codes(runbook.check(write(tmp_path, data)))


# ------------------------------------------------------------------- A4: provenance
@pytest.mark.parametrize("source", ["spec:SC-002", "spec:Session Timeout/Idle timeout",
                                    "note:input/notes/20260924T101500Z-0123456789ab.md",
                                    "interview:thresholds", "owner:decision d-004", "default"])
def test_source_shapes_that_are_accepted(tmp_path, source):
    data = fails2(detect=DETECT, then="stop", source=source)
    data["knobs"][0]["source"] = source
    data["stages"][0]["checks"][0]["source"] = source
    data["stages"].append({"id": "ship", "needs": ["load"], "owner_gate": {"approve": "the owner ships",
                                                                             "source": source}})
    result = runbook.check(write(tmp_path, data))
    assert result["errors"] == [] and warn_codes(result) == [], (result["errors"], result["warnings"])


@pytest.mark.parametrize("source", ["the meeting on Tuesday", "note:../secret.md", "note:input/other/a.md",
                                    "note:input/notes/", "interview:Done Bar", "spec:", "owner:", "defaults",
                                    "note:input/notes/../../x.md"])
def test_a_source_of_another_shape_is_a_warning(tmp_path, source):
    data = minimal2()
    data["knobs"][0]["source"] = source
    result = runbook.check(write(tmp_path, data))
    assert result["errors"] == [] and result["verdict"] == "PASS"
    assert [(w["code"], w["where"]) for w in result["warnings"]
            if w["code"] != "COVERS-WITHOUT-SPEC"] == [("SOURCE-SHAPE", "knobs[0].source")]


def test_a_source_must_be_text(tmp_path):
    data = minimal2()
    data["stages"][0]["checks"][0]["source"] = 5
    assert ("FIELD-TYPE", "stages[0].checks[0].source") in where_codes(runbook.check(write(tmp_path, data)))


def test_a_note_source_must_exist_when_a_spec_is_given(tmp_path):
    proj = tmp_path / "proj"
    domain = proj / "domain"
    domain.mkdir(parents=True)
    (proj / "project.yaml").write_text("name: p\n", encoding="utf-8")
    spec = spec_file(domain, SPEC_KIT)
    data = minimal2()
    data["stages"][1]["checks"][0]["covers"] = ["SC-002", "SC-003"]
    data["knobs"][0]["source"] = "note:input/notes/a.md"
    path = write(domain, data)
    # without a spec only the shape is read
    assert "SOURCE-MISSING" not in [w["code"] for w in runbook.check(path)["warnings"]]
    result = runbook.check(path, spec_path=spec)
    assert result["errors"] == []
    missing = [w for w in result["warnings"] if w["code"] == "SOURCE-MISSING"]
    assert [w["where"] for w in missing] == ["knobs[0].source"]
    assert "input/notes/a.md" in missing[0]["message"]
    # the project root is the folder that holds project.yaml; the note is found there
    (proj / "input" / "notes").mkdir(parents=True)
    (proj / "input" / "notes" / "a.md").write_text("the load test uses two workers\n", encoding="utf-8")
    result = runbook.check(path, spec_path=spec)
    assert "SOURCE-MISSING" not in [w["code"] for w in result["warnings"]]
    # --no-files skips the look-up, as it skips the plugin look-ups
    (proj / "input" / "notes" / "a.md").unlink()
    assert "SOURCE-MISSING" not in [w["code"] for w in runbook.check(path, spec_path=spec, check_files=False)["warnings"]]


def test_a_note_above_the_project_root_is_not_found(tmp_path):
    (tmp_path / "input" / "notes").mkdir(parents=True)
    (tmp_path / "input" / "notes" / "a.md").write_text("x\n", encoding="utf-8")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "project.yaml").write_text("name: p\n", encoding="utf-8")
    spec = spec_file(proj, SPEC_KIT)
    data = minimal2(spec="spec.md")
    data["stages"][1]["checks"][0]["covers"] = ["SC-002", "SC-003"]
    data["stages"][1]["checks"][0]["source"] = "note:input/notes/a.md"
    result = runbook.check(write(proj, data))
    assert result["spec"]["path"].endswith("spec.md") and spec
    assert [w["where"] for w in result["warnings"] if w["code"] == "SOURCE-MISSING"] == [
        "stages[1].checks[0].source"]


# ------------------------------------------------------------------ A2: respond()
def respond_data(*thens):
    data = minimal2()
    data["stages"].append({"id": "free-port", "recovery": True, "run": "make free-port",
                           "checks": [{"id": "port-free", "type": "exit-code"}]})
    stage = data["stages"][1]
    stage["checks"] += [{"id": "codes", "type": "exit-code"}, {"id": "other", "type": "exit-code"}]
    stage["retry"]["stop_on"] = ["codes"]
    stage["fails"] = [{"id": "f%d" % n, "when": "w", "detect": {"type": "exit-code", "expect": 9}, "then": t}
                      for n, t in enumerate(thens)]
    return data, stage


P, F, B = verdict.PASS, verdict.FAIL, verdict.BLOCKED
FAILED = {"p95": F, "codes": P, "other": P}


def test_respond_stop_ends_fail():
    data, stage = respond_data("stop")
    step = runbook.respond(data, stage, FAILED, 1, runbook.knob_values(data), {"f0": P})
    assert (step["retry"], step["run"], step["end"], step["fail"]) == (False, None, F, "f0")
    assert "f0" in step["reason"]


def test_respond_ask_owner_ends_paused():
    data, stage = respond_data("ask-owner")
    step = runbook.respond(data, stage, FAILED, 1, runbook.knob_values(data), {"f0": P})
    assert (step["retry"], step["run"], step["end"], step["fail"]) == (False, None, verdict.PAUSED, "f0")


def test_respond_retry_goes_through_the_retry_rule_and_its_limits():
    data, stage = respond_data("retry")
    knobs = runbook.knob_values(data)
    # `other` is not in on_fail, so the retry rule alone would stop; the fail case names the
    # failure as one a new attempt may fix
    results = {"p95": P, "codes": P, "other": F}
    assert not runbook.next_attempt(data, stage, results, 1, knobs)["retry"]
    step = runbook.respond(data, stage, results, 1, knobs, {"f0": P})
    assert step["retry"] and step["knobs"]["WORKERS"] == 4 and step["end"] is None and step["fail"] == "f0"
    # the attempts still run out
    step = runbook.respond(data, stage, results, 3, knobs, {"f0": P})
    assert not step["retry"] and step["end"] == F and "3 of 3" in step["reason"]
    # the knob range still holds
    step = runbook.respond(data, stage, results, 1, {"WORKERS": 8}, {"f0": P})
    assert not step["retry"] and step["end"] == F and "range" in step["reason"]
    # a stop_on check still stops
    step = runbook.respond(data, stage, {"p95": P, "codes": F, "other": P}, 1, knobs, {"f0": P})
    assert not step["retry"] and step["end"] == F and "stop_on" in step["reason"]


def test_respond_run_sends_to_the_recovery_stage_while_an_attempt_is_left():
    data, stage = respond_data({"run": "free-port"})
    knobs = runbook.knob_values(data)
    step = runbook.respond(data, stage, FAILED, 1, knobs, {"f0": P})
    assert (step["retry"], step["run"], step["end"], step["fail"]) == (False, "free-port", None, "f0")
    assert step["knobs"] == knobs and "free-port" in step["reason"]
    # the rerun after the recovery is an attempt of the failed stage: none left, no recovery
    step = runbook.respond(data, stage, FAILED, 3, knobs, {"f0": P})
    assert (step["run"], step["end"]) == (None, F) and "3 of 3" in step["reason"]
    # a stop_on check that failed is never run again, recovered or not
    step = runbook.respond(data, stage, {"p95": F, "codes": F, "other": P}, 1, knobs, {"f0": P})
    assert (step["run"], step["end"]) == (None, F)
    # a recovery may supply what a BLOCKED check missed
    step = runbook.respond(data, stage, {"p95": B, "codes": P, "other": P}, 1, knobs, {"f0": P})
    assert step["run"] == "free-port"


def test_respond_first_recognized_fail_case_in_file_order_decides():
    data, stage = respond_data("stop", "ask-owner")
    knobs = runbook.knob_values(data)
    assert runbook.respond(data, stage, FAILED, 1, knobs, {"f0": P, "f1": P})["fail"] == "f0"
    step = runbook.respond(data, stage, FAILED, 1, knobs, {"f0": F, "f1": P})
    assert step["fail"] == "f1" and step["end"] == verdict.PAUSED
    # a fail case without a detect is never recognized, whatever the caller passes
    stage["fails"].insert(0, {"id": "plain", "when": "w"})
    assert runbook.respond(data, stage, FAILED, 1, knobs, {"plain": P, "f1": P})["fail"] == "f1"


def test_respond_with_no_fail_case_recognized_is_the_retry_rule():
    data, stage = respond_data("stop")
    knobs = runbook.knob_values(data)
    cases = [({"p95": F, "codes": P, "other": P}, 1, P, None), ({"p95": F, "codes": P, "other": P}, 3, P, F),
             ({"p95": F, "codes": F, "other": P}, 1, P, F), ({"p95": B, "codes": P, "other": P}, 1, P, B),
             ({"p95": F, "codes": P, "other": P}, 1, F, None), ({"p95": verdict.PAUSED, "codes": P, "other": P}, 1,
                                                               None, verdict.PAUSED)]
    for results, attempt, det, end in cases:
        detected = {} if det is None else {"f0": det}
        if det == P:
            detected = {"f9": P}             # names no fail case of the stage
        want = runbook.next_attempt(data, stage, results, attempt, knobs)
        step = runbook.respond(data, stage, results, attempt, knobs, detected)
        assert {k: step[k] for k in want} == want, (results, attempt)
        assert (step["run"], step["fail"], step["end"]) == (None, None, end), (results, attempt, step)


def test_respond_passes_when_every_check_passed_whatever_was_detected():
    data, stage = respond_data("stop")
    step = runbook.respond(data, stage, {"p95": P, "codes": P, "other": P}, 1, runbook.knob_values(data), {"f0": P})
    assert (step["retry"], step["run"], step["end"], step["fail"]) == (False, None, P, None)


def test_respond_on_a_format_1_stage_is_next_attempt():
    data, stage = load_stage()
    knobs = runbook.knob_values(data)
    for results in ({"p95": F, "codes": P}, {"p95": P, "codes": P}, {"p95": F, "codes": F}):
        want = runbook.next_attempt(data, stage, results, 1, knobs)
        step = runbook.respond(data, stage, results, 1, knobs, {})
        assert {k: step[k] for k in want} == want


# --------------------------------------------------------- bar_parts (for intake, B1)
def bar_data():
    return {"runbook": 2, "id": "bars", "title": "Bars",
            "knobs": [{"id": "P95_MS", "description": "d", "type": "float", "default": 50, "owner_only": True,
                       "source": "spec:SC-002"},
                      {"id": "WORKERS", "description": "d", "type": "int", "default": 2, "source": "default"}],
            "stages": [
                {"id": "build", "run": "make", "checks": [
                    {"id": "build-exit", "type": "exit-code"},
                    {"id": "artifact", "type": "file-exists", "path": "out/app.bin", "source": "interview:done-bar"},
                    {"id": "any-log", "type": "file-exists", "path": "out/app.log", "non_empty": False}]},
                {"id": "load-test", "needs": ["build"], "run": "make load W=${WORKERS}", "checks": [
                    {"id": "redirect-p95", "type": "json-field", "path": "out/p95.json", "field": "p95_ms",
                     "op": "<=", "value": "${P95_MS}", "source": "spec:SC-002"},
                    {"id": "no-errors", "type": "regex-in-file", "path": "out/load.log", "pattern": "ERROR",
                     "absent": True, "ignore_case": True},
                    {"id": "codes", "type": "plugin", "script": "checks/codes.py",
                     "args": ["out/load.json", 301, "${WORKERS}"]},
                    {"id": "tag", "type": "json-field", "path": "out/v.json", "field": "tag", "op": "==",
                     "value": "ok"}],
                 "fails": [{"id": "port-busy", "when": "the port is taken", "detect": DETECT,
                            "then": {"run": "free-port"}, "source": "note:input/notes/a.md"},
                           {"id": "flaky", "when": "the network\n drops"},
                           {"id": "full", "when": "w", "detect": {"type": "exit-code", "expect": 3},
                            "then": "ask-owner"}]},
                {"id": "release", "needs": ["load-test"], "owner_gate": {"approve": "the owner reads\n  the load report",
                                                                         "source": "owner:d-001"}},
                {"id": "free-port", "recovery": True, "run": "make free-port", "checks": [
                    {"id": "port-free", "type": "exit-code", "expect": 0}]}]}


def test_bar_parts_renders_every_part_in_the_item_file_shapes(tmp_path):
    data = bar_data()
    assert runbook.check(write(tmp_path, data), check_files=False)["errors"] == []
    parts = runbook.bar_parts(data)["parts"]
    bars = {who: part["bar"] for who, part in parts.items()}
    assert bars == {
        "build-exit": "build/build-exit: exit-code exit == 0",
        "artifact": "build/artifact: file-exists out/app.bin exists, non-empty",
        "any-log": "build/any-log: file-exists out/app.log exists",
        "redirect-p95": "load-test/redirect-p95: json-field out/p95.json p95_ms <= 50 (knob P95_MS, owner only)",
        "no-errors": "load-test/no-errors: regex-in-file out/load.log does not match /ERROR/i",
        "codes": "load-test/codes: plugin checks/codes.py out/load.json 301 2 exits 0 (knob WORKERS)",
        "tag": 'load-test/tag: json-field out/v.json tag == "ok"',
        "fail:load-test/port-busy": "fail load-test/port-busy: detect regex-in-file out/load.log matches "
                                    "/Address already in use/ then run free-port",
        "fail:load-test/flaky": "fail load-test/flaky: when the network drops",
        "fail:load-test/full": "fail load-test/full: detect exit-code exit == 3 then ask-owner",
        "gate:release": "owner approves: the owner reads the load report",
        "port-free": "free-port/port-free: exit-code exit == 0",
    }
    assert {who: (p["kind"], p["stage"]) for who, p in parts.items()}["fail:load-test/flaky"] == ("fail", "load-test")
    assert parts["gate:release"]["kind"] == "gate" and parts["gate:release"]["stage"] == "release"
    assert parts["redirect-p95"]["kind"] == "check" and parts["redirect-p95"]["knobs"] == ["P95_MS"]
    assert parts["build-exit"]["knobs"] == []


def test_bar_parts_gives_run_order_positions_and_sources():
    out = runbook.bar_parts(bar_data())
    assert out["stages"] == {
        "build": {"position": 1, "of": 3, "label": "1/3 build", "recovery": False},
        "load-test": {"position": 2, "of": 3, "label": "2/3 load-test", "recovery": False},
        "release": {"position": 3, "of": 3, "label": "3/3 release", "recovery": False},
        "free-port": {"position": None, "of": 3, "label": "recovery free-port", "recovery": True},
    }
    sources = {who: p["source"] for who, p in out["parts"].items() if p["source"]}
    assert sources == {"artifact": "interview:done-bar", "redirect-p95": "spec:SC-002",
                       "fail:load-test/port-busy": "note:input/notes/a.md", "gate:release": "owner:d-001"}
    assert out["knobs"] == {"P95_MS": {"default": 50, "owner_only": True, "source": "spec:SC-002"},
                            "WORKERS": {"default": 2, "owner_only": False, "source": "default"}}


@pytest.mark.parametrize("default,shown", [(50, "50"), (50.0, "50"), (49.5, "49.5"), (0.25, "0.25")])
def test_bar_parts_writes_a_whole_number_the_same_way_in_both_types(default, shown):
    data = bar_data()
    data["knobs"][0]["default"] = default
    bar = runbook.bar_parts(data)["parts"]["redirect-p95"]["bar"]
    assert bar == "load-test/redirect-p95: json-field out/p95.json p95_ms <= %s (knob P95_MS, owner only)" % shown


def test_bar_parts_on_the_worked_example():
    with open(os.path.join(EXAMPLE, "runbook.yaml"), encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    out = runbook.bar_parts(data)
    assert out["parts"]["redirect-p95"]["bar"] == ("load-test/redirect-p95: json-field out/load.json "
                                                   "redirect.p95_ms <= 50 (knob P95_LIMIT_MS, owner only)")
    assert out["stages"]["load-test"]["label"] == "4/5 load-test"
    assert out["stages"]["free-port"]["position"] is None


# ------------------------------------------------------------ A5: the worked example
def test_worked_example_is_format_2_and_passes():
    with open(os.path.join(EXAMPLE, "runbook.yaml"), encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert data["runbook"] == 2
    result = runbook.check(os.path.join(EXAMPLE, "runbook.yaml"))
    assert result["errors"] == [] and result["warnings"] == [], (result["errors"], result["warnings"])
    cov = result["coverage"]
    assert cov["missing"] == []
    assert sorted(cov["covered"]) == ["EC-001", "EC-002", "EC-003", "SC-001", "SC-002", "SC-003", "SC-004"]
    assert cov["covered"]["EC-003"] == ["fail:load-test/port-busy"]
    stages = {s["id"]: s for s in data["stages"]}
    assert [s["id"] for s in data["stages"] if s.get("recovery")] == ["free-port"]
    fails = [f for s in data["stages"] for f in s.get("fails", []) if "detect" in f]
    assert {"then" in f for f in fails} == {True}
    assert {"run": "free-port"} in [f["then"] for f in fails]
    assert stages["load-test"]["retry"]["max_attempts"] >= 2
    sources = [k.get("source") for k in data["knobs"]] + [
        c.get("source") for s in data["stages"] for c in s.get("checks", [])]
    assert "spec:SC-002" in sources


def test_worked_example_spec_numbers_its_edge_cases():
    spec = runbook.parse_spec(os.path.join(EXAMPLE, "spec.md"))
    assert [i["id"] for i in spec["edge_cases"]] == ["EC-001", "EC-002", "EC-003"]
    assert spec["unnumbered"] == [] and not any(i.get("loose") for i in spec["edge_cases"])


# ---------------------------------------------------------------- A5: the document
def test_format_doc_describes_format_2():
    with open(os.path.join(REPO, "docs", "runbook-format.md"), encoding="utf-8") as fh:
        doc = fh.read()
    for must in ("## Format 1 and format 2", "`runbook: 2`", "`recovery: true`", "`respond`",
                 "`EC-001`", "`fail:<stage>/<fail id>`", "`spec:<item id>`", "`note:input/notes/",
                 "`interview:<slot id>`", "`owner:<decision ref>`", "`default`", "`then`", "`detect`",
                 "## Edge cases", "## Known failures", "## Provenance"):
        assert must in doc, must
    assert "—" not in doc
