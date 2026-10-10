"""The runbook forge writes format 3 (op-009 t-107; design/research-engine/runbook-format-3.md
section 13.2, FR-049, EC-024, SC-014).

/alpaca-runbook-forge is a skill: an agent follows its steps. What the forge writes from the
worked spec (runbook-format-3.md section 10.1) is the worked research runbook of its step 4a, so
these tests read that example out of the skill and hold it to the done bar and the fail case of
t-107:

* forged from the worked spec, the runbook passes `alpaca runbook check` with every required item
  covered, and follows the forge's rules (the pack's defaults file named and not repeated, a
  metric per measured criterion, the runbook folder protected, a timeout on every stage, every
  program fingerprinted, an owner gate kept out of the gates, no `source: default`);
* the forge never invents a target: every target is stated by the spec item its source names, a
  bar value the spec does not state names the signed interview slot it came from, and a criterion
  with no number gets no target (EC-024). The check cannot see an invented target, since it reads
  only the shape of a source (section 1, decision 5), so the trace here is the guard, and it
  catches an invented target in each form.

No test runs an agent or touches the network.
"""
from __future__ import annotations

import os
import re
import shutil

import pytest
import yaml

from alpaca import runbook
from alpaca.tests.conftest import REPO

SKILL = os.path.join(REPO, ".claude", "skills", "alpaca-runbook-forge", "SKILL.md")
FIX = os.path.join(REPO, "alpaca", "tests", "fixtures", "research", "sortbench")
SLOTS = os.path.join(REPO, "templates", "interview", "research-slots.yaml")
FOLDER = "packs/sortbench"                      # the runbook folder, from the repository root

#: the bar values of a research block (section 8), by the key a runbook line writes, with the
#: field path an interview slot names for it
BAR_KEYS = {"target": "research.metrics[].target", "repeats": "research.metrics[].repeats",
            "min_ok": "research.metrics[].min_ok", "margin": "research.metrics[].margin",
            "rounds": "research.stop.rounds", "slots": "research.stop.slots",
            "usd": "research.budget.usd", "tokens": "research.budget.tokens", "wall": "research.budget.wall"}


# ------------------------------------------------------------------------------ helpers
def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def research_step(text):
    """Step 4a of a forge text (the skill, or the kit's AGENTS.md built from it)."""
    assert "\n## Step 4a: " in text, "the forge has no research step"
    return text.split("\n## Step 4a: ", 1)[1].split("\n## Step 5", 1)[0]


def fenced(text):
    """[(language, body)] of the fenced blocks of a markdown text, in order."""
    return [(m.group(1), m.group(2)) for m in re.finditer(r"^```(\w*)\n(.*?)^```$", text, re.M | re.S)]


def worked_example(text):
    """(spec, runbook, numberless SC-001 line, metric written for it) from step 4a."""
    blocks = fenced(research_step(text))
    assert [lang for lang, _body in blocks] == ["markdown", "yaml", "markdown", "yaml"], blocks
    return tuple(body for _lang, body in blocks)


#: the files of the worked pack the forged runbook sits next to (runbook-format-3.md section 10):
#: the pack's defaults file and the lessons file it names
PACK_FILES = ("pack.yaml", "lessons.md")


def forge_tree(tmp_path, spec, runbook_text, pack_files=PACK_FILES):
    """The forged files in <tmp>/repo/packs/sortbench, with the pack's own files beside them, under
    a folder that holds project.yaml, so the checker resolves repo.path to <tmp>/repo as the
    pack's repository."""
    root = tmp_path / "repo"
    folder = root / FOLDER
    folder.mkdir(parents=True)
    (root / "project.yaml").write_text("name: forge-test\n", encoding="utf-8")
    for name in pack_files:
        (folder / name).write_text(_read(os.path.join(FIX, name)), encoding="utf-8")
    (folder / "improve-spec.md").write_text(spec, encoding="utf-8")
    (folder / "improve.yaml").write_text(runbook_text, encoding="utf-8")
    return str(folder)


def pack_defaults():
    """The `research` mapping of the worked pack's defaults file."""
    return yaml.safe_load(_read(os.path.join(FIX, "pack.yaml")))["research"]


def merged(data):
    """The research block a runbook and the defaults file it names give together (section 7.2)."""
    block = data["research"]
    assert block.get("defaults") == "pack.yaml", "the forged runbook names no defaults file"
    return runbook.merge_research(pack_defaults(), block)


def inlined(data):
    """The runbook with its defaults file merged into it and no `defaults` key: the same contract,
    written as one file."""
    out = dict(data)
    out["research"] = {k: v for k, v in merged(data).items() if k != "defaults"}
    return out


def check(folder):
    return runbook.check(os.path.join(folder, "improve.yaml"),
                         spec_path=os.path.join(folder, "improve-spec.md"))


def spec_items(spec_text, tmp_path):
    path = tmp_path / "spec-items.md"
    path.write_text(spec_text, encoding="utf-8")
    return {item["id"]: item["text"] for item in runbook.parse_spec(str(path))["items"]}


def _number(value):
    return str(int(value)) if isinstance(value, (int, float)) and float(value).is_integer() else str(value)


def states(text, value):
    """True when `text` states the number `value` as a number of its own."""
    return re.search(r"(?<![\d.])%s(?![\d]|\.\d)" % re.escape(_number(value)), text) is not None


def untraced_targets(data, items):
    """[(metric id, why)] for every target the spec does not state: the forge writes a target only
    with `source: spec:<item id>`, and only when that item states the number (section 13.2)."""
    out = []
    for metric in (data.get("research") or {}).get("metrics") or []:
        if "target" not in metric:
            continue
        source = metric.get("source")
        if not isinstance(source, str) or not source.startswith("spec:"):
            out.append((metric.get("id"), "source %r is not a spec item" % (source,)))
        elif source[5:] not in items:
            out.append((metric.get("id"), "%s names no item of the spec" % source))
        elif not states(items[source[5:]], metric["target"]):
            out.append((metric.get("id"), "%s does not state %s" % (source, _number(metric["target"]))))
    return out


def research_slots():
    with open(SLOTS, encoding="utf-8") as fh:
        return {slot["id"]: slot.get("fields") or [] for slot in yaml.safe_load(fh)["slots"]}


def unsourced_bar_values(runbook_text, items):
    """[(line number, key, why)] for every bar value of the research block that neither quotes the
    spec item stating it on its own line (`# SC-002: "over 5 runs"`) nor sits below a comment
    that names the signed slot holding it (`# min_ok and margin: interview:research-noise`)."""
    slots = research_slots()
    lines = runbook_text.split("\n")
    start = next(n for n, line in enumerate(lines) if line.startswith("research:"))
    out = []
    for n in range(start, len(lines)):
        m = re.match(r"^(\s*)(%s):\s*([^#\s]+)" % "|".join(BAR_KEYS), lines[n])
        if not m:
            continue
        indent, key, value = len(m.group(1)), m.group(2), m.group(3)
        quote = re.search(r'#\s*((?:SC|EC)-\d+):\s*"([^"]+)"', lines[n])
        if quote:
            item = items.get(quote.group(1))
            if item is None or quote.group(2) not in item or not states(quote.group(2), yaml.safe_load(value)):
                out.append((n + 1, key, "the quote does not state %s in %s" % (value, quote.group(1))))
            continue
        named = None
        for up in range(n - 1, start, -1):
            line = lines[up]
            c = re.match(r"^\s*#\s*(.+?):\s*interview:([a-z0-9-]+)\s*$", line)
            if c and key in re.split(r",\s*|\s+and\s+", c.group(1)):
                named = c.group(2)
                break
            if line.strip() and not line.lstrip().startswith("#") and len(line) - len(line.lstrip()) < indent - 2:
                break                   # left the part that holds the value
        if named is None:
            out.append((n + 1, key, "no spec quote and no slot comment"))
        elif BAR_KEYS[key] not in slots.get(named, []):
            out.append((n + 1, key, "slot %s does not hold %s" % (named, BAR_KEYS[key])))
    return out


@pytest.fixture
def example():
    return worked_example(_read(SKILL))


# ------------------------------------------------------------------------ the done bar
def test_the_forge_example_is_forged_from_the_worked_spec(example):
    spec, _runbook, _line, _metric = example
    assert spec == _read(os.path.join(FIX, "improve-spec.md"))


def test_the_forged_runbook_passes_the_check_with_every_item_covered(example, tmp_path):
    spec, text, _line, _metric = example
    result = check(forge_tree(tmp_path, spec, text))
    assert result["verdict"] == "PASS", result["errors"]
    assert result["errors"] == [] and result["warnings"] == []
    cov = result["coverage"]
    assert cov["required"] == 7 and cov["missing"] == []
    assert cov["covered"] == {"SC-001": ["metric:compares"], "EC-002": ["metric:compares"],
                              "SC-002": ["metric:wall_ms"], "SC-003": ["unit-pass"], "EC-001": ["unit-edge"],
                              "SC-004": ["research:stop"], "EC-003": ["research:paths"]}
    data = yaml.safe_load(text)
    assert data["runbook"] == runbook.RESEARCH_FORMAT and data["spec"] == "improve-spec.md"
    assert runbook.runbook_format(data) == runbook.RESEARCH_FORMAT


def test_the_forged_runbook_names_the_packs_defaults_file_and_repeats_none_of_it(example, tmp_path):
    """Section 13.2 and the `research.defaults` row: the pack has a defaults file, so the forge
    names it and writes only what the runbook adds or changes."""
    spec, text, _line, _metric = example
    block = yaml.safe_load(text)["research"]
    assert block["defaults"] == "pack.yaml"
    pack = pack_defaults()
    for key, value in pack.items():
        if isinstance(value, dict) and key in block:
            assert not set(value) & set(block[key]), key       # e.g. paths: editable here, protected there
        else:
            assert key not in block, key
    # this project at its current commit: no repo
    assert "repo" not in block
    # the name is load-bearing: without the file, or without naming it, the check refuses the runbook
    alone = check(forge_tree(tmp_path / "no-file", spec, text, pack_files=("lessons.md",)))
    assert ("DEFAULTS-UNREADABLE", "research.defaults") in {(e["code"], e["where"]) for e in alone["errors"]}
    unnamed = check(forge_tree(tmp_path / "unnamed", spec, text.replace("  defaults: pack.yaml\n", "", 1)))
    missing = {e["where"] for e in unnamed["errors"] if e["code"] == "FIELD-MISSING"}
    assert {"research.fingerprint", "research.disk"} <= missing, unnamed["errors"]
    assert "research.budget" not in missing      # optional since t-297: a run with none is uncapped
    # and the runbook with the file merged into it is the same contract, which the check passes
    same = check(forge_tree(tmp_path / "inlined", spec, yaml.safe_dump(inlined(yaml.safe_load(text)),
                                                                        sort_keys=False)))
    assert same["verdict"] == "PASS" and same["coverage"]["missing"] == [], same["errors"]


def test_an_owner_gate_stage_is_named_in_no_research_list(example, tmp_path):
    """Step 4a: an owner gate (a criterion with no number) is a stage of its own; the check refuses
    it in `gates` (STAGE-ROLE), and passes it outside every research list."""
    spec, text, _line, _metric = example
    gate = ("  - id: compares-target\n    title: the owner decides the comparisons target\n"
            "    needs: [unit]\n    owner_gate:\n      approve: the owner sets the comparisons target of SC-001\n"
            "      covers: [SC-001]\n      source: spec:SC-001\n")
    at = text.index("\nresearch:\n")
    outside = text[:at] + "\n" + gate + text[at:]
    assert check(forge_tree(tmp_path / "outside", spec, outside))["verdict"] == "PASS"
    inside = outside.replace("    - {stage: unit}\n", "    - {stage: unit}\n    - {stage: compares-target}\n", 1)
    codes = {(e["code"], e["where"]) for e in check(forge_tree(tmp_path / "inside", spec, inside))["errors"]}
    assert ("STAGE-ROLE", "research.gates[2].stage") in codes, codes


def test_the_forged_runbook_fails_the_check_without_its_research_coverers(example, tmp_path):
    """The check the example passes is a real bar: drop the metric that covers SC-001 and EC-002,
    or the stop part's cover, and the check refuses it."""
    spec, text, _line, _metric = example
    no_cover = text.replace("      covers: [SC-001, EC-002]\n", "", 1)
    result = check(forge_tree(tmp_path / "a", spec, no_cover))
    codes = {(e["code"], e["where"]) for e in result["errors"]}
    assert result["verdict"] == "FAIL"
    assert {("SPEC-UNCOVERED", "SC-001"), ("SPEC-UNCOVERED", "EC-002"),
            ("COVERS-NONE", "research.metrics[0].covers")} <= codes
    no_stop = text.replace("    covers: [SC-004]\n", "", 1)
    result = check(forge_tree(tmp_path / "b", spec, no_stop))
    assert ("SPEC-UNCOVERED", "SC-004") in {(e["code"], e["where"]) for e in result["errors"]}


def test_the_forged_runbook_follows_the_forge_rules(example):
    _spec, text, _line, _metric = example
    data = yaml.safe_load(text)
    block = merged(data)                        # the rules hold on the runbook and its defaults file
    # one metric per success criterion that states a measured number, covering it
    assert [m["covers"][0] for m in block["metrics"]] == ["SC-001", "SC-002"]
    for metric in block["metrics"]:
        assert metric["source"] == "spec:%s" % metric["covers"][0]
        assert metric["direction"] == "lower"                   # both criteria say "at most"
    # the accept rule follows from the targets
    assert block["accept"]["rule"] == "deficit-pareto" and "after_targets" in block["accept"]
    # the runbook folder and the build configuration are protected; nothing protected is editable
    assert FOLDER + "/" in block["paths"]["protected"] and "pyproject.toml" in block["paths"]["protected"]
    assert not set(block["paths"]["editable"]) & set(block["paths"]["protected"])
    # every stage command has a timeout and a research role
    named = {g["stage"] for g in block["gates"]}
    for stage in data["stages"]:
        assert "timeout" in stage and stage["id"] in named, stage["id"]
    # every program the stages and metrics run is fingerprinted
    programs = {s["run"].split()[0] for s in data["stages"]} | {m["command"].split()[0] for m in block["metrics"]}
    assert programs <= set(block["fingerprint"]["tools"]), programs
    # each part that shows an item names it, with that item as its source; no source is `default`
    for part in ("paths", "stop"):
        assert block[part]["source"] == "spec:%s" % block[part]["covers"][0], part
    assert "source: default" not in text.split("\nresearch:\n", 1)[1]
    # every stage is a gate here, since the spec has no owner gate and no recovery stage
    assert [g["stage"] for g in block["gates"]] == [s["id"] for s in data["stages"]]
    # a value the agent chose is a setting that is not a bar, and says so on its line
    for n, line in enumerate(text.split("\n")):
        if "# default chosen by the agent:" in line:
            key = line.split(":", 1)[0].strip()
            assert key in ("timeout", "disk"), (n, line)


def test_every_target_is_stated_by_the_spec_item_it_names(example, tmp_path):
    spec, text, _line, _metric = example
    data = yaml.safe_load(text)
    assert [m["target"] for m in data["research"]["metrics"]] == [18000, 120]
    assert untraced_targets(data, spec_items(spec, tmp_path)) == []


def test_every_bar_value_comes_from_the_spec_or_a_signed_slot(example, tmp_path):
    spec, text, _line, _metric = example
    assert unsourced_bar_values(text, spec_items(spec, tmp_path)) == []
    # and it reads each of them: every bar value of the block is found by the scan
    block = yaml.safe_load(text)["research"]
    assert {"target", "repeats", "min_ok", "margin"} <= set(block["metrics"][1])
    assert set(block["stop"]) >= {"rounds", "slots"}
    # the budget is the pack's: its defaults file sets it, and the runbook does not repeat it
    assert "budget" not in block and set(merged(yaml.safe_load(text))["budget"]) == {"usd", "tokens", "wall"}


# -------------------------------------------------------------------------- the fail case
@pytest.mark.parametrize("old,new,flagged", [
    # a number no criterion states
    ("      target: 18000  # SC-001: \"at most 18000 comparisons\"\n", "      target: 15000\n", "compares"),
    ("      target: 120  # SC-002: \"at most 120 ms\"\n", "      target: 100\n", "wall_ms"),
    # a number put on an item that states no number, or on no item at all
    ("      source: spec:SC-002\n", "      source: spec:SC-003\n", "wall_ms"),
    ("      source: spec:SC-001\n", "      source: owner:the-forge\n", "compares"),
    ("      source: spec:SC-001\n", "      source: spec:SC-009\n", "compares"),
])
def test_an_invented_target_is_caught(example, tmp_path, old, new, flagged):
    """t-107's fail case. The check still passes most of these runbooks: it reads a target's
    source for its shape only, so the trace to the spec item is what refuses them."""
    spec, text, _line, _metric = example
    assert text.count(old) == 1, old
    bad = text.replace(old, new)
    found = untraced_targets(yaml.safe_load(bad), spec_items(spec, tmp_path))
    assert [m for m, _why in found] == [flagged], found
    if "SC-009" not in new:
        assert check(forge_tree(tmp_path, spec, bad))["verdict"] == "PASS"


def test_a_target_without_its_quote_or_a_bar_value_without_its_slot_is_caught(example, tmp_path):
    spec, text, _line, _metric = example
    items = spec_items(spec, tmp_path)
    moved = text.replace("      target: 18000  # SC-001: \"at most 18000 comparisons\"\n", "      target: 15000  "
                         "# SC-001: \"at most 18000 comparisons\"\n")
    assert [(k, w.split()[0]) for _n, k, w in unsourced_bar_values(moved, items)] == [("target", "the")]
    unsigned = text.replace("      # min_ok and margin: interview:research-noise\n", "")
    assert [k for _n, k, _w in unsourced_bar_values(unsigned, items)] == ["min_ok", "margin"]
    wrong_slot = text.replace("    # slots: interview:research-stop\n", "    # slots: interview:research-budget\n")
    assert [k for _n, k, _w in unsourced_bar_values(wrong_slot, items)] == ["slots"]
    unquoted = text.replace("    rounds: 5  # SC-004: \"after 5 rounds\"\n", "    rounds: 5\n")
    assert [k for _n, k, _w in unsourced_bar_values(unquoted, items)] == ["rounds"]


def test_a_criterion_with_no_number_gets_no_target(example, tmp_path):
    """EC-024: the spec states no number for a target, so the forge writes none; the runbook still
    passes, and keeping the old target on it is caught as invented."""
    spec, text, line, metric = example
    old = [s for s in spec.split("\n") if s.startswith("- **SC-001**:")][0]
    numberless = spec.replace(old, line.strip("\n"))
    assert not re.search(r"\d", line.split(":", 1)[1])
    written = yaml.safe_load(metric)
    assert len(written) == 1 and written[0]["id"] == "compares" and "target" not in written[0]
    assert written[0]["covers"][0] == "SC-001" and written[0]["source"] == "spec:SC-001"
    start = text.index("    - id: compares\n")
    end = text.index("    - id: wall_ms\n")
    forged = text[:start] + metric + text[end:]
    result = check(forge_tree(tmp_path / "forged", numberless, forged))
    assert result["verdict"] == "PASS", result["errors"]
    data = yaml.safe_load(forged)
    assert data["research"]["accept"]["rule"] == "deficit-pareto"      # wall_ms keeps its target
    items = spec_items(numberless, tmp_path)
    assert untraced_targets(data, items) == []
    assert unsourced_bar_values(forged, items) == []
    # the target of the numbered spec, left on the numberless one, is an invented target
    assert [m for m, _w in untraced_targets(yaml.safe_load(text), items)] == ["compares"]


# ------------------------------------------------------------------------- the skill text
def test_the_skill_states_the_research_rules():
    text = " ".join(_read(SKILL).split())
    step = " ".join(research_step(_read(SKILL)).split())
    for must in ("`runbook: 3` at the top instead of `runbook: 2`", "`research` block",
                 "One metric per success criterion that states a measured number",
                 "Write a `target` only when the criterion states the number",
                 "`source: spec:<item id>`", "`TARGET-SOURCE`", "With no number in the criterion, write no target",
                 "owner gate", "`repeats`, `min_ok` and `margin`", "`stop.rounds`, `stop.slots` and the `budget`",
                 "`# min_ok and margin: interview:research-noise`", "`deficit-pareto`", "`champion`", "`COVERS-NONE`",
                 "the runbook folder and the eval configuration", "`conftest.py`", "bare name",
                 "never `ask-owner`", "`# default chosen by the agent: <why>`", "Write no `source: default`",
                 "`metric:<id>`", "`case:<id>`", "`research:<part>`", "`origin: store`", "40-digit commit id",
                 # the repository, the defaults file, the stage roles and the slots of the other parts
                 "write them as `repo.path` and `repo.ref`",
                 "Leave `repo` out only for this project at its current commit",
                 "name it in `defaults:` and write only what the runbook adds or changes",
                 "Every stage that runs a command and is not a recovery stage is named once",
                 "are named in none of them (`STAGE-ROLE`)",
                 GATES_ORDER_RULE,
                 "`categories` (required in improve mode) come from the `research-proposal` slot",
                 "`controls` come from the `research-controls` slot",
                 "Write a metric's `workdir` only when"):
        assert must in step, must
    assert "step 4a" in text.split("## Two modes", 1)[0]                     # the intro points at it
    assert "never a target the spec does not state" in text.split("---", 2)[1]  # and the description
    never = text.split("## Never", 1)[1]
    assert "Never write a research `target` the spec does not state" in never


#: the GATES-ORDER rule as step 4a states it (runbook-format-3.md section 6.1, alpaca/runbook.py
#: GATES-ORDER); the test below holds each clause of it to the checker
GATES_ORDER_RULE = ("A gate stage's `needs` names only earlier gate stages or the prepare stage; a holdout stage's "
                    "names only gate stages, earlier holdout stages or the prepare stage; the prepare stage has no "
                    "`needs` (`GATES-ORDER`).")


def _pack_copy(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(FIX, str(root / "packs" / "sortbench"))
    (root / "project.yaml").write_text("name: forge-test\n", encoding="utf-8")
    return str(root / "packs" / "sortbench")


def _gates_order(folder, name, *pairs):
    path = os.path.join(folder, name)
    text = _read(path)
    for old, new in pairs:
        assert old in text, (name, old)
        text = text.replace(old, new, 1)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    result = runbook.check(path, spec_path=os.path.join(folder, name.replace(".yaml", "-spec.md")))
    return [(e["code"], e["where"]) for e in result["errors"] if e["code"] == "GATES-ORDER"]


def test_the_gates_order_rule_the_skill_states_is_the_one_the_check_applies(tmp_path):
    step = " ".join(research_step(_read(SKILL)).split())
    assert GATES_ORDER_RULE in step
    assert "A stage's `needs` names only earlier gate stages." not in step    # the round-1 wording
    # the prepare stage has no `needs`
    folder = _pack_copy(tmp_path / "a")
    assert _gates_order(folder, "repair.yaml") == []
    assert _gates_order(folder, "repair.yaml", ("  - id: prepare\n", "  - id: prepare\n    needs: [build]\n")) == [
        ("GATES-ORDER", "stages[1].needs")]
    # a gate stage may need the prepare stage
    folder = _pack_copy(tmp_path / "b")
    prepare = ("  - id: prepare\n    title: write the test helper data into the case's base tree\n"
               "    run: python3 bench/prepare.py --src ${WORKTREE}/src\n    timeout: 300\n    checks:\n"
               "      - {id: prepare-exit, type: exit-code}\n")
    assert _gates_order(folder, "repair.yaml", (prepare, ""), ("\nstages:\n", "\nstages:\n" + prepare),
                        ("  - id: build\n", "  - id: build\n    needs: [prepare]\n")) == []
    # a gate stage may not need a later gate stage
    folder = _pack_copy(tmp_path / "c")
    assert _gates_order(folder, "improve.yaml", ("    - {stage: build}\n    - {stage: unit}\n",
                                                 "    - {stage: unit}\n    - {stage: build}\n")) == [
        ("GATES-ORDER", "stages[1].needs[0]")]
    # a holdout stage may need a gate stage and an earlier holdout stage, and not a later one
    folder = _pack_copy(tmp_path / "d")
    second = ("  - id: holdout-check\n    title: the held-out results are read back\n    needs: [unit, holdout-bench]\n"
              "    run: python3 bench/measure.py --src ${WORKTREE}/src --inputs holdout --check\n    timeout: 300\n"
              "    checks:\n      - {id: holdout-check-exit, type: exit-code}\n\nresearch:\n")
    assert _gates_order(folder, "improve.yaml", ("\nresearch:\n", "\n" + second),
                        ("    stages: [holdout-bench]\n", "    stages: [holdout-bench, holdout-check]\n")) == []
    assert _gates_order(folder, "improve.yaml", ("    stages: [holdout-bench, holdout-check]\n",
                                                 "    stages: [holdout-check, holdout-bench]\n")) == [
        ("GATES-ORDER", "stages[3].needs[1]")]


def test_the_slots_the_skill_names_are_research_slots():
    named = set(re.findall(r"interview:(research-[a-z-]+)", _read(SKILL)))
    named |= set(re.findall(r"`(research-[a-z]+)`", research_step(_read(SKILL))))
    assert {"research-mode", "research-noise", "research-stop", "research-accept", "research-proposal",
            "research-controls"} <= named
    assert named <= set(research_slots()), named - set(research_slots())
