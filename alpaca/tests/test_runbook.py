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
    result = runbook.check(os.path.join(EXAMPLE, "runbook.yaml"))
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
    path = os.path.join(REPO, "skills", "alpaca-runbook-forge", "SKILL.md")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    assert text.startswith("---\nname: alpaca-runbook-forge\n")
    assert "alpaca runbook check" in text
    assert "docs/runbook-format.md" in text


def test_added_files_are_ascii_and_free_of_em_dash():
    paths = [os.path.join(REPO, "docs", "runbook-format.md"),
             os.path.join(REPO, "skills", "alpaca-runbook-forge", "SKILL.md"),
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
