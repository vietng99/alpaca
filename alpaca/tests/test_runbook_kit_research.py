"""The partner kit covers format 3, the research format (op-009 t-107;
design/research-engine/runbook-format-3.md sections 13.3 and 15.2, FR-049, SC-014).

* the kit's check_runbook.py answers as `alpaca runbook check` on the research fixtures and every
  variant test_runbook_research.py checks (captured live while that file runs, as
  test_runbook_kit.py captures test_runbook.py), and on the forge's worked research runbook as the
  kit's own AGENTS.md prints it;
* runbook.schema.json has a definition per part of the research block, generated from the
  checker's research tables, with the mode conditions, and it agrees with the checker on the
  research fixtures and variants, by its own classes of RESEARCH_ERROR_CODES;
* FORMAT.md names every research field and code; AGENTS.md, the kit skill, README.md and the
  our-side part carry the forge's research step; VERSION names the research format.

The kit stays alpaca-runbook-kit-v2 with a `runbook: 2` template (section 13.3), which
test_runbook_kit.py keeps pinning.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys

import pytest
import yaml

from alpaca import runbook
from alpaca.gates import verdict
from alpaca.tests import test_research_forge as forge
from alpaca.tests.test_runbook_kit import (CAPTURE_PLUGIN, SCHEMA_ALWAYS, SCHEMA_NEVER, SCHEMA_SOME,
                                           _as_json, kit_run, product_json, schema_errors)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
FIX = os.path.join(REPO, "alpaca", "tests", "fixtures", "research", "sortbench")
KIT_NAME = "alpaca-runbook-kit-v%d" % runbook.FORMAT

#: the parts of the block, each with the table its definition is generated from
PART_TABLES = {"research_repo": runbook.REPO_KEYS, "research_metric": runbook.METRIC_KEYS,
               "research_accept": runbook.ACCEPT_KEYS, "research_paths": runbook.PATHS_KEYS,
               "research_fingerprint": runbook.FINGERPRINT_KEYS, "research_gate": runbook.GATE_ENTRY_KEYS,
               "research_stop": runbook.STOP_KEYS, "research_budget": runbook.BUDGET_KEYS,
               "research_proposal_field": runbook.PROPOSAL_FIELD_KEYS, "research_category": runbook.CATEGORY_KEYS,
               "research_lessons": runbook.LESSONS_KEYS, "research_controls": runbook.CONTROLS_KEYS,
               "research_holdout": runbook.HOLDOUT_KEYS, "research_repair": runbook.REPAIR_KEYS,
               "research_case": runbook.CASE_KEYS, "research_fail_to_pass": runbook.F2P_KEYS,
               "research_pass_to_pass": runbook.P2P_KEYS, "research_coupled": runbook.COUPLED_KEYS,
               "research_runtime": runbook.RUNTIME_KEYS, "research_verifier": runbook.VERIFIER_KEYS,
               "research_audit": runbook.AUDIT_KEYS, "research_egress": runbook.EGRESS_KEYS,
               "research": runbook.RESEARCH_KEYS}

# --------------------------------------------------------------- the schema's own classes
#: research codes a JSON Schema states in every case the checker reports them in the runbook file
RESEARCH_ALWAYS = {"VALUE-UNKNOWN", "PATHS-RULE-INVALID", "TOOL-INVALID", "HOST-INVALID", "TIMEOUT-MISSING",
                   "THEN-ASK-OWNER", "PROPOSAL-FIELD-RESERVED", "BASE-NOT-COMMIT"}
#: research codes it states in some cases only: a placeholder in a text field is text; a target the
#: defaults file sets is out of the runbook's sight
RESEARCH_SOME = {"FIELD-UNFILLED", "TARGET-SOURCE"}
#: research codes that need another part of the file, the merged block, the spec or the disk
RESEARCH_NEVER = {"DEFAULTS-UNREADABLE", "ACCEPT-RULE-MISMATCH", "PATHS-OVERLAP", "STAGE-UNKNOWN",
                  "STAGE-ROLE", "STAGE-UNUSED", "GATES-ORDER", "KNOB-MOVES-BAR", "PACK-OUTSIDE-REPO",
                  "FILE-MISSING", "COVERS-NONE"}
#: format 2 codes a block that names a defaults file can meet through the merge: a required field
#: or a keyed list's items may come from the file
LAYERED_SOME = {"FIELD-MISSING", "FIELD-EMPTY"}


def always(layered):
    out = (SCHEMA_ALWAYS | RESEARCH_ALWAYS)
    return out - LAYERED_SOME if layered else out


def never():
    return SCHEMA_NEVER | RESEARCH_NEVER


def test_schema_code_classes_cover_every_research_code():
    classes = [RESEARCH_ALWAYS, RESEARCH_SOME, RESEARCH_NEVER]
    union = set().union(*classes)
    assert union == set(runbook.RESEARCH_ERROR_CODES)
    assert sum(len(c) for c in classes) == len(union)
    assert LAYERED_SOME <= SCHEMA_ALWAYS


# ------------------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def built(tmp_path_factory):
    from alpaca import runbook_kit
    return runbook_kit.build(REPO, str(tmp_path_factory.mktemp("kit-out")))


@pytest.fixture(scope="module")
def alone(built, tmp_path_factory):
    """A folder that holds only the kit: its checker runs there with no alpaca on its path."""
    dest = os.path.join(str(tmp_path_factory.mktemp("alone")), KIT_NAME)
    shutil.copytree(built["kit"], dest)
    return dest


@pytest.fixture(scope="module")
def schema(built):
    with open(os.path.join(built["kit"], "runbook.schema.json"), encoding="ascii") as fh:
        return json.load(fh)


def _read(built, rel):
    with open(os.path.join(built["kit"], rel), encoding="ascii") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def captured(alone, tmp_path_factory):
    """Run test_runbook_research.py with the capture plugin of test_runbook_kit.py: every
    runbook.check call it makes becomes one row with the product's answer and the kit's."""
    work = tmp_path_factory.mktemp("capture-research")
    plugin_dir = work / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "kit_capture.py").write_text(CAPTURE_PLUGIN, encoding="utf-8")
    out = work / "rows.jsonl"
    env = dict(os.environ)
    env.update(KIT_CAPTURE_OUT=str(out), KIT_CAPTURE_KIT=alone,
               PYTHONPATH=os.pathsep.join([str(plugin_dir), REPO]))
    env.pop("PYTEST_ADDOPTS", None)
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "kit_capture",
                           "--basetemp", str(work / "inner"), os.path.join(HERE, "test_runbook_research.py")],
                          cwd=REPO, env=env, capture_output=True, text=True, timeout=1800)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    with open(str(out), encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if json.loads(line)["kind"] == "check"]


def _research_rows(captured):
    """The captured rows whose file is a research runbook (read as format 3)."""
    rows = []
    for row in captured:
        try:
            data = yaml.safe_load(row["text"]) if row["text"] is not None else None
        except yaml.YAMLError:
            continue
        if isinstance(data, dict) and runbook.runbook_format(data) == runbook.RESEARCH_FORMAT:
            rows.append((row, data))
    return rows


# --------------------------------------------------------------------- the carried checker
def test_kit_checker_matches_the_product_on_the_research_fixtures_and_variants(captured):
    rows = _research_rows(captured)
    assert len(rows) >= 60, len(rows)
    wrong = [(r["test"], r["args"], r["product_code"], r["kit_code"]) for r, _d in rows
             if r["product"] != r["kit"] or r["product_code"] != r["kit_code"]]
    assert wrong == []
    reached = {e["code"] for r, _d in rows for e in r["product"]["errors"]}
    # together the variants reach every research refusal, and the passing path
    assert set(runbook.RESEARCH_ERROR_CODES) <= reached, sorted(set(runbook.RESEARCH_ERROR_CODES) - reached)
    assert {r["product_code"] for r, _d in rows} >= {verdict.PASS, verdict.FAIL}


@pytest.mark.parametrize("name", ["improve.yaml", "repair.yaml"])
def test_kit_checker_passes_the_worked_research_runbooks(alone, tmp_path, name):
    folder = tmp_path / "repo" / "packs" / "sortbench"
    shutil.copytree(FIX, str(folder))
    (tmp_path / "repo" / "project.yaml").write_text("name: kit-test\n", encoding="utf-8")
    path = str(folder / name)
    spec = str(folder / name.replace(".yaml", "-spec.md"))
    want, code = product_json(path, spec)
    assert code == verdict.PASS, want["errors"]
    proc = kit_run(alone, [path, "--spec", spec, "--json"], cwd=tmp_path)
    assert proc.returncode == code and json.loads(proc.stdout) == want
    plain = kit_run(alone, [os.path.basename(path), "--spec", os.path.basename(spec)], cwd=folder)
    assert plain.stdout.strip().splitlines()[-1] == "GATE alpaca-runbook-check: PASS"


def test_kit_checker_passes_the_forge_example_its_agents_md_prints(built, alone, tmp_path):
    """The partner's agent reads the worked research runbook in AGENTS.md; forged from the worked
    spec, it passes the kit's checker, which answers as the product does."""
    spec, text, _line, _metric = forge.worked_example(_read(built, "AGENTS.md"))
    folder = forge.forge_tree(tmp_path, spec, text)
    path, spec_path = os.path.join(folder, "improve.yaml"), os.path.join(folder, "improve-spec.md")
    want, code = product_json(path, spec_path)
    assert code == verdict.PASS, want["errors"]
    proc = kit_run(alone, [path, "--spec", spec_path, "--json"], cwd=tmp_path)
    assert proc.returncode == code and json.loads(proc.stdout) == want
    # and with an invented target the kit's check still passes: the trace is the forge's rule
    bad = text.replace('target: 18000  # SC-001: "at most 18000 comparisons"', "target: 15000")
    assert forge.untraced_targets(yaml.safe_load(bad), forge.spec_items(spec, tmp_path)) != []


# ------------------------------------------------------------------------------ the schema
def test_schema_has_a_definition_per_research_part_from_the_checker_tables(schema):
    from alpaca import runbook_kit
    defs = schema["$defs"]
    assert set(runbook_kit.RESEARCH_DEFS) == set(PART_TABLES)
    for name, table in PART_TABLES.items():
        for full, strict in ((name, True), (name + "_layered", False)):
            assert full in defs, full
            assert set(defs[full]["properties"]) == set(table), full
            assert defs[full]["additionalProperties"] is False, full
            if strict:
                assert set(defs[full]["required"]) == {k for k, req in table.items() if req}, full
            else:
                assert "required" not in defs[full], full
        if set(runbook.PART_EXTRAS) <= set(table):
            props = defs[name]["properties"]
            assert props["covers"] == {"$ref": "#/$defs/texts"} and props["source"] == {"$ref": "#/$defs/text"}
    timeouts = defs["research_runtime"]["properties"]["timeouts"]["properties"]
    assert set(timeouts) == set(runbook.AGENT_ROLES)
    block = defs["research"]["properties"]
    assert block["mode"] == {"enum": list(runbook.MODES)}
    assert defs["research_metric"]["properties"]["parser"] == {"enum": list(runbook.PARSERS)}
    assert defs["research_accept"]["properties"]["rule"] == {"enum": list(runbook.ACCEPT_RULES)}
    # the top property picks the form by whether the block names a defaults file
    assert schema["properties"]["research"] == {"if": {"type": "object", "required": ["defaults"]},
                                                "then": {"$ref": "#/$defs/research_layered"},
                                                "else": {"$ref": "#/$defs/research"}}


def test_schema_accepts_the_worked_research_runbooks_and_their_expanded_blocks(schema):
    for name in ("improve.yaml", "repair.yaml"):
        with open(os.path.join(FIX, name), encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        assert schema_errors(schema, _as_json(data)) == [], name
        # the same block with the defaults file merged in, which the strict form reads
        block, _layers, report = runbook.expand_research(data, FIX)
        assert report.errors == []
        whole = copy.deepcopy(data)
        whole["research"] = {k: v for k, v in block.items() if k != "defaults"}
        assert schema_errors(schema, _as_json(whole)) == [], name
        # without the defaults file, the runbook's own block misses what the file gave
        own = copy.deepcopy(data)
        del own["research"]["defaults"]
        found = schema_errors(schema, _as_json(own))
        for part in ("fingerprint", "disk"):
            assert "$.research: %s is required" % part in found, (name, part)
        # research.budget is optional since t-297: a run with no budget is uncapped
        assert "$.research: budget is required" not in found, name


def test_schema_accepts_the_forge_example(schema):
    with open(forge.SKILL, encoding="utf-8") as fh:
        _spec, text, _line, metric = forge.worked_example(fh.read())
    data = yaml.safe_load(text)
    assert schema_errors(schema, _as_json(data)) == []          # names pack.yaml: the layered form
    data["research"]["metrics"][0] = yaml.safe_load(metric)[0]
    assert schema_errors(schema, _as_json(data)) == []
    whole = forge.inlined(yaml.safe_load(text))                  # the file merged in: the strict form
    assert schema_errors(schema, _as_json(whole)) == []


def _forged(schema_change):
    """The forge example with its defaults file merged in (one file, the strict form of the
    schema), then changed."""
    with open(forge.SKILL, encoding="utf-8") as fh:
        data = forge.inlined(yaml.safe_load(forge.worked_example(fh.read())[1]))
    schema_change(data)
    return _as_json(data)


def _set(path, value):
    def change(data):
        node = data
        for key in path[:-1]:
            node = node[key]
        if value is DROP:
            del node[path[-1]]
        else:
            node[path[-1]] = value
    return change


DROP = object()
R = "research"


@pytest.mark.parametrize("change,code", [
    (_set([R, "metrics", 1, "margin"], DROP), "FIELD-MISSING"),
    (_set([R, "metrics", 1, "path"], DROP), "FIELD-MISSING"),
    (_set([R, "metrics", 0, "path"], "out.json"), "FIELD-UNKNOWN"),
    (_set([R, "metrics", 0, "source"], "default"), "TARGET-SOURCE"),
    (_set([R, "metrics", 0, "source"], DROP), "TARGET-SOURCE"),
    (_set([R, "metrics", 1, "repeats"], 1), "RANGE"),
    (_set([R, "metrics", 0, "direction"], "down"), "VALUE-UNKNOWN"),
    (_set([R, "categories"], DROP), "FIELD-MISSING"),
    (_set([R, "repair"], {"cases": [], "pass_to_pass": {"command": "x", "min_tests": 1, "timeout": 5}}),
     "FIELD-UNKNOWN"),
    (_set([R, "mode"], "repair"), "FIELD-UNKNOWN"),
    (_set([R, "paths", "editable"], ["."]), "PATHS-RULE-INVALID"),
    (_set([R, "paths", "protected"], ["../elsewhere/"]), "PATHS-RULE-INVALID"),
    (_set([R, "fingerprint", "tools"], ["/usr/bin/python3"]), "TOOL-INVALID"),
    (_set([R, "egress"], {"allow": ["https://example.com"]}), "HOST-INVALID"),
    (_set([R, "budget", "usd"], 0), "RANGE"),
    (_set([R, "disk"], "20G"), "FIELD-TYPE"),
    (_set([R, "proposal"], [{"field": "expected", "type": "str"}]), "PROPOSAL-FIELD-RESERVED"),
    (_set([R, "controls", "random"], True), "FIELD-MISSING"),
    (_set(["stages", 0, "timeout"], DROP), "TIMEOUT-MISSING"),
    (_set(["stages", 0, "fails"], [{"id": "f", "when": "w", "detect": {"type": "exit-code", "expect": 3},
                                    "then": "ask-owner"}]), "THEN-ASK-OWNER"),
])
def test_schema_refuses_what_the_checker_refuses_in_the_forge_example(schema, tmp_path, change, code):
    """Each edit gives the checker's code (so the edit is the refusal it names), and the schema
    refuses the edited file too."""
    data = _forged(change)
    with open(forge.SKILL, encoding="utf-8") as fh:
        spec = forge.worked_example(fh.read())[0]
    folder = forge.forge_tree(tmp_path, spec, yaml.safe_dump(data, sort_keys=False))
    codes = {e["code"] for e in forge.check(folder)["errors"]}
    assert code in codes, codes
    assert schema_errors(schema, data), code


RULES = ["src/", "./src/", "src/*.py", "conftest.py", "a/b/c", ".", "./", "*", "*/", "", "  ", "/abs", " /x",
         "a\\b", "a/**/b", "~/x", "a/${X}", "..", "../x", "a/../b", "./..", "a/..", "...", "a..b/", "a/.b"]
LOCATIONS = ["/srv/data", "../shared", "data", "", " ", "~/data", "${HOME}/x", "a~b"]


@pytest.mark.parametrize("value", RULES)
def test_schema_root_rules_follow_the_checker_grammar(schema, value):
    defs = schema["$defs"]
    rule = defs["research_paths"]["properties"]["protected"]["items"]
    editable = defs["research_paths"]["properties"]["editable"]["items"]
    assert bool(schema_errors(rule, value, schema)) == (runbook._rule_problem(value) is not None), value
    assert bool(schema_errors(editable, value, schema)) == (runbook._rule_problem(value, True) is not None), value


@pytest.mark.parametrize("value", LOCATIONS)
def test_schema_locations_follow_the_checker_grammar(schema, value):
    location = schema["$defs"]["research_repo"]["properties"]["path"]
    assert bool(schema_errors(location, value, schema)) == (runbook._location_problem(value) is not None), value


def test_schema_takes_a_repair_block_and_its_own_proposal_fields(schema):
    with open(os.path.join(FIX, "repair.yaml"), encoding="utf-8") as fh:
        data = _as_json(yaml.safe_load(fh))
    # `expected` is an improve base field, free in repair; `suspects` the other way round
    data["research"]["proposal"] = [{"field": "expected", "type": "text"}]
    assert schema_errors(schema, data) == []
    data["research"]["proposal"] = [{"field": "suspects", "type": "text"}]
    assert schema_errors(schema, data)
    data["research"]["proposal"] = []
    data["research"]["repair"]["cases"][0]["base"] = "main"
    assert schema_errors(schema, data)                         # BASE-NOT-COMMIT


def test_schema_agrees_with_the_checker_on_the_research_variants(schema, captured):
    refused = accepted = 0
    disagree = []
    for row, data in _research_rows(captured):
        errors = row["product"]["errors"]
        codes = {e["code"] for e in errors}
        # an error the defaults file holds is outside what a schema of the runbook reads
        own = {e["code"] for e in errors if ":" not in e["where"]}
        layered = isinstance(data.get("research"), dict) and "defaults" in data["research"]
        found = schema_errors(schema, _as_json(data))
        if own & always(layered):
            refused += 1
            if not found:
                disagree.append(("schema accepts", row["test"], sorted(codes)))
        elif codes <= never():
            accepted += 1
            if found:
                disagree.append(("schema refuses", row["test"], sorted(codes), found[:3]))
    assert disagree == []
    assert refused >= 10 and accepted >= 30, (refused, accepted)


def test_schema_with_a_full_validator_when_one_is_installed(schema, captured):
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    for row, data in _research_rows(captured):
        mine = bool(schema_errors(schema, _as_json(data)))
        theirs = bool(list(validator.iter_errors(_as_json(data))))
        assert mine == theirs, row["test"]


def test_research_schema_ranges_come_from_the_checker(monkeypatch):
    from alpaca import runbook_kit
    monkeypatch.setattr(runbook, "STAGE_TIMEOUT_RANGE", (3, 9))
    defs = runbook_kit.schema()["$defs"]
    for name, key in (("research_metric", "timeout"), ("research_gate", "lock_timeout"),
                      ("research_pass_to_pass", "timeout"), ("research_verifier", "rerun_timeout")):
        prop = defs[name]["properties"][key]
        assert (prop["minimum"], prop["maximum"]) == (3, 9), name
    monkeypatch.setattr(runbook, "MODES", ("improve", "repair", "explore"))
    assert runbook_kit.schema()["$defs"]["research"]["properties"]["mode"]["enum"][-1] == "explore"


def test_schema_comment_names_the_research_rules_it_cannot_state(schema):
    text = schema["$comment"]
    for code in ("COVERS-NONE", "STAGE-UNKNOWN", "STAGE-ROLE", "STAGE-UNUSED", "GATES-ORDER", "PATHS-OVERLAP",
                 "TARGET-SOURCE", "ACCEPT-RULE-MISMATCH", "KNOB-MOVES-BAR", "PACK-OUTSIDE-REPO"):
        assert code in text, code
    assert "defaults file" in text and "runbook: 3" in schema["description"]


# ------------------------------------------------------------------------------- the texts
def test_format_md_names_every_research_field_and_code(built):
    text = _read(built, "FORMAT.md")
    assert "## The research block" in text
    for field in runbook.research_fields():
        assert "`%s`" % field["path"] in text, field["path"]
    for code in runbook.RESEARCH_ERROR_CODES:
        assert "`%s`" % code in text, code


def test_version_names_the_research_format_and_the_kit_stays_format_2(built):
    text = _read(built, "VERSION")
    assert re.search(r"^format: 2$", text, re.M) and re.search(r"^research_format: 3$", text, re.M)
    assert re.search(r"^kit: alpaca-runbook-kit-v2$", text, re.M)
    assert re.search(r"^runbook: 2\b", _read(built, "templates/runbook.yaml"), re.M)


def test_agents_md_and_the_kit_skill_carry_the_research_step(built):
    with open(forge.SKILL, encoding="utf-8") as fh:
        skill = fh.read()
    for rel in ("AGENTS.md", ".claude/skills/runbook-forge/SKILL.md"):
        text = _read(built, rel)
        step = forge.research_step(text)
        # the same example as the product skill, byte for byte
        assert forge.worked_example(text) == forge.worked_example(skill), rel
        flat = " ".join(step.split())
        for must in ("`runbook: 3`", "Write a `target` only when the criterion states the number",
                     "`source: spec:<item id>`", "With no number in the criterion, write no target",
                     "The section \"The research block\" of `<kit>/FORMAT.md`"):
            assert must in flat, (rel, must)
        assert "Never write a research `target` the spec does not state" in " ".join(text.split())
    intro = " ".join(_read(built, "AGENTS.md").split("## Two modes", 1)[0].split())
    assert "A research runbook (`runbook: 3`)" in intro and "step 4a" in intro
    head = yaml.safe_load(_read(built, ".claude/skills/runbook-forge/SKILL.md").split("\n---\n", 1)[0].lstrip("-\n"))
    assert "research runbook (runbook: 3)" in head["description"]


def test_readme_and_our_side_tell_the_partner_about_research_runbooks(built):
    readme = " ".join(_read(built, "README.md").split())
    for must in ("A research runbook (`runbook: 3`)", "`research` block", "\"The research block\"",
                 "step 4a", "only when your spec states the number", "`research_format`"):
        assert must in readme, must
    ours = " ".join(_read(built, "FORMAT.md").split("## What happens on our side", 1)[1].split())
    for must in ("For a research runbook (`runbook: 3`)", "frozen when a run starts",
                 "Loosening a target, or setting one on a metric that had none, needs the owner's recorded decision"):
        assert must in ours, must
