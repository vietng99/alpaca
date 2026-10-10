"""t-104: the fill-in research contract templates and their field coverage
(design/research-engine/operator-surface.md sections 6.4, 6.5 and 6.7).

templates/research/improve.yaml and repair.yaml hold a line for every format 3 field their mode
allows; every value to fill is a quoted `<FILL:<field path>>` placeholder. The tests:

- the coverage assertions of 6.4: every required format 3 field is asked by a research interview
  slot (templates/interview/research-slots.yaml), has a placeholder or a fixed value in the
  template of each mode that needs it, and every slot field and placeholder names a known field;
- the fill of 6.5: each template filled with the worked example's values (fixtures under
  alpaca/tests/fixtures/research/) equals the worked runbook, and the filled file passes the
  format 3 check with its spec, while the unfilled template fails with FIELD-UNFILLED and no
  FIELD-UNKNOWN.

The format 3 check is the product's `runbook.check(path, spec_path=...)` and the field list is
`runbook.research_fields()` (operator-surface.md section 6.7, runbook-format-3.md section 14.1);
every fill runs on a tmp copy of t-096's sortbench pack (alpaca/tests/fixtures/research/sortbench/).
"""
import copy
import os
import re
import shutil

import pytest
import yaml

from alpaca import runbook
from alpaca.tests.conftest import REPO
from alpaca.tests.test_interview_skill import BANNED

TEMPLATES = os.path.join(REPO, "templates", "research")
SLOTS = os.path.join(REPO, "templates", "interview", "research-slots.yaml")
DEFAULT_SLOTS = os.path.join(REPO, "templates", "interview", "slots.yaml")
FIXTURES = os.path.join(REPO, "alpaca", "tests", "fixtures", "research")
WORKED = os.path.join(FIXTURES, "sortbench")
MODES = ("improve", "repair")
PH = re.compile(r"^<FILL:([^>]*)>$")
#: the two required paths no slot asks: the spec step and the forge write them (6.4 assertion 1)
FORGE_WRITES = ("research.metrics[].covers", "research.repair.cases[].covers")
#: paths the check requires only under a condition (a metric kind, noisy, a random control, a
#: target, an enum proposal field, a repair case); the table marks them optional, so each is named
#: here and must be asked by a slot all the same (runbook-format-3.md section 4). The tuple mirrors
#: the conditional checks of alpaca/runbook.py by hand: FIELD-MISSING for a json-field metric's
#: path and field (:1742), a noisy metric's repeats, min_ok and margin (:1758), an enum proposal
#: field's values (:1901), the random control's mutate and syntax (:1947) and a store case's
#: fail_to_pass.tests (:2024), plus TARGET-SOURCE for a target's source (:1786). A new condition
#: there must be added here too.
REQUIRED_WHEN = ("research.metrics[].path", "research.metrics[].field", "research.metrics[].repeats",
                 "research.metrics[].min_ok", "research.metrics[].margin", "research.metrics[].source",
                 "research.controls.mutate", "research.controls.syntax", "research.proposal[].values",
                 "research.repair.cases[].fail_to_pass.tests")


def _errors(path, spec):
    """The product format 3 check's findings on a runbook file and its spec."""
    return runbook.check(path, spec_path=spec)["errors"]


def _yaml(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _template(mode):
    data, report = runbook.load(os.path.join(TEMPLATES, "%s.yaml" % mode))
    assert data is not None, report.errors
    return data


def _slot_fields():
    return {s["id"]: s["fields"] for s in _yaml(SLOTS)["slots"]}


# ------------------------------------------------------------------------ the template paths
def _placeholder(value):
    """The field path of a placeholder value (`"<FILL:x>"` or `["<FILL:x>"]`), else None."""
    if isinstance(value, str):
        m = PH.match(value)
        return m.group(1) if m else None
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
        m = PH.match(value[0])
        return m.group(1) if m else None
    return None


def _held(template):
    """{template path: "placeholder" | fixed value}: every value the template holds, by its path
    ([] for one item of a list). Asserts that each placeholder names the path it sits at."""
    out = {}

    def walk(node, path):
        name = _placeholder(node)
        if name is not None:
            assert name == path, "the placeholder %s sits at %s" % (name, path)
            out[path] = "placeholder"
        elif isinstance(node, dict):
            for key, value in node.items():
                walk(value, "%s.%s" % (path, key) if path else key)
        elif isinstance(node, list) and len(node) == 1 and isinstance(node[0], dict):
            walk(node[0], path + "[]")
        else:
            out[path] = node
    walk(template, "")
    return out


def _has_line(held, path):
    """A template has a line for a field when it holds the path itself, something under it, or a
    placeholder above it: a whole mapping such as `runtime.timeouts` is one value at the template's
    grain (operator-surface.md section 6.5), so its line covers the per-role fields under it."""
    return any(h == path or h.startswith(path + ".") or h.startswith(path + "[]")
               or (v == "placeholder" and (path.startswith(h + ".") or path.startswith(h + "[]")))
               for h, v in held.items())


def _covers_path(entry, path):
    """6.4 assertion 1: a slot field asks a required path when it equals it, is an ancestor of it
    (the entry followed by `.` or `[]` begins the path) or lies under it."""
    return entry == path or path.startswith(entry + ".") or path.startswith(entry + "[]") \
        or entry.startswith(path + ".") or entry.startswith(path + "[]")


def _format2_key(path):
    top = set(runbook.TOP_KEYS) | {"research"}
    if "." not in path and "[" not in path:
        return path in top
    return path.startswith("stages[].") and path[len("stages[]."):] in runbook.STAGE_KEYS


# ------------------------------------------------------------------------------ the fill
def fill(template, values):
    """Fill a parsed template with a values mapping {indexed field path: value} (6.5 steps 1-4):
    copy each item pattern once per index the values name (dropping the list when none), put each
    value on its placeholder (a lone placeholder in a list takes the whole list), drop the
    placeholders left and the mappings and lists they leave empty, and fail naming any value path
    that landed on no placeholder."""
    used = set()

    def indices(prefix):
        pat = re.compile(r"^%s\[(\d+)\]" % re.escape(prefix))
        return sorted({int(m.group(1)) for m in (pat.match(k) for k in values) if m})

    def rename(node, old, new):
        if isinstance(node, dict):
            return {k: rename(v, old, new) for k, v in node.items()}
        if isinstance(node, list):
            return [rename(v, old, new) for v in node]
        if isinstance(node, str) and PH.match(node):
            return node.replace("<FILL:%s" % old, "<FILL:%s" % new, 1)
        return node

    drop = object()

    def walk(node, path):
        name = _placeholder(node)
        if name is not None:
            if name in values:
                used.add(name)
                return copy.deepcopy(values[name])
            return drop
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                got = walk(value, "%s.%s" % (path, key) if path else key)
                if got is not drop:
                    out[key] = got
            return out if out or not node else drop
        if isinstance(node, list) and len(node) == 1 and isinstance(node[0], dict):
            items = []
            for i in indices(path):
                got = walk(rename(node[0], path + "[]", "%s[%d]" % (path, i)), "%s[%d]" % (path, i))
                if got is not drop:
                    items.append(got)
            return items if items else drop
        return copy.deepcopy(node)

    out = walk(template, "")
    lost = sorted(set(values) - used)
    assert not lost, "the template has no line for %s" % ", ".join(lost)
    return out


def _values(mode):
    return _yaml(os.path.join(FIXTURES, "%s-values.yaml" % mode))


def _filled(mode):
    return fill(_template(mode), _values(mode))


def _write_pack(tmp_path, mode, doc=None, text=None):
    """A tmp copy of the worked pack folder with the filled (or given) runbook as <mode>.yaml."""
    pack = tmp_path / "pack"
    shutil.copytree(WORKED, str(pack))
    target = pack / ("%s.yaml" % mode)
    target.write_text(text if text is not None else yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return str(target), str(pack / ("%s-spec.md" % mode))


# ------------------------------------------------------------------- 6.4 field coverage
def test_every_required_field_is_asked_by_a_research_slot():
    """6.4 assertion 1, the fail case: a slot the check needs that the interview never asks."""
    entries = [e for fields in _slot_fields().values() for e in fields]
    required = [f["path"] for f in runbook.research_fields() if any(f["required"].values())]
    assert "research.mode" in required and "research.disk" in required
    unasked = [p for p in required if p not in FORGE_WRITES and not any(_covers_path(e, p) for e in entries)]
    assert unasked == []
    known = {f["path"] for f in runbook.research_fields()}
    assert set(REQUIRED_WHEN) <= known
    unasked_when = [p for p in REQUIRED_WHEN if not any(_covers_path(e, p) for e in entries)]
    assert unasked_when == []


def test_a_slot_dropped_from_the_slot_file_leaves_a_required_field_unasked():
    """The assertion above has teeth: without research-budget, research.disk goes unasked (the
    budget caps are optional since t-297: a run with none is uncapped)."""
    fields = _slot_fields()
    fields.pop("research-budget")
    entries = [e for f in fields.values() for e in f]
    required = [f["path"] for f in runbook.research_fields() if any(f["required"].values())]
    unasked = [p for p in required if p not in FORGE_WRITES and not any(_covers_path(e, p) for e in entries)]
    assert "research.disk" in unasked and "research.budget.usd" not in unasked


def test_a_dropped_conditional_field_leaves_it_unasked():
    """The REQUIRED-when half has teeth: without research.metrics[].margin in research-noise, the
    margin a noisy metric needs goes unasked."""
    fields = _slot_fields()
    fields["research-noise"] = [e for e in fields["research-noise"] if e != "research.metrics[].margin"]
    entries = [e for f in fields.values() for e in f]
    unasked = [p for p in REQUIRED_WHEN if not any(_covers_path(e, p) for e in entries)]
    assert unasked == ["research.metrics[].margin"]


def test_every_required_field_has_a_line_in_the_template_of_its_mode():
    """6.4 assertion 2: a placeholder or a fixed value at or under each required path."""
    fields = runbook.research_fields()
    for mode in MODES:
        held = _held(_template(mode))
        for f in fields:
            if not f["required"][mode]:
                continue
            path = f["path"]
            assert _has_line(held, path), (mode, path)
        for path in ("runbook", "id", "title", "spec", "stages[].id", "stages[].run",
                     "stages[].timeout", "stages[].checks"):
            assert path in held, (mode, path)
        assert held["runbook"] == 3 and held["research.mode"] == mode


def test_the_repair_template_fixes_the_rule_and_leaves_out_what_repair_refuses():
    held = _held(_template("repair"))
    assert held["research.accept.rule"] == "repair"
    assert not any(p.startswith("research.metrics") for p in held)
    assert "research.accept.after_targets" not in held
    improve = _held(_template("improve"))
    assert not any(p.startswith("research.repair") for p in improve)
    assert improve["research.accept.rule"] == "placeholder"


def test_every_slot_field_and_placeholder_names_a_known_field():
    """6.4 assertion 3: no stale name; a name outside the block is a format 2 key."""
    known = {f["path"] for f in runbook.research_fields()}
    names = [e for fields in _slot_fields().values() for e in fields]
    for mode in MODES:
        names += [p for p, v in _held(_template(mode)).items() if v == "placeholder"]
    for name in names:
        if name.startswith("research."):
            assert name in known, name
        else:
            assert _format2_key(name), name
    assert "research.metrics[].artifact" in known


def test_every_optional_field_of_the_table_has_a_line_in_each_template_that_allows_it():
    """6.5: a line for every field its mode allows; after_targets is left out of repair, whose
    block has no target (ACCEPT-RULE-MISMATCH there)."""
    known = [f["path"] for f in runbook.research_fields()]
    for mode in MODES:
        held = _held(_template(mode))
        for path in known:
            if mode == "repair" and path == "research.accept.after_targets":
                assert path not in held
                continue
            head = path.split(".")[1].split("[")[0]
            if runbook.MODE_KEYS[mode].get(head, True) is None:
                assert not any(h.startswith("research.%s" % head) for h in held), (mode, path)
                continue
            assert _has_line(held, path), (mode, path)


def test_a_mapping_placeholder_covers_the_fields_under_it_and_nothing_else_does():
    """The field table lists research.runtime.timeouts.<role> one role at a time
    (runbook-format-3.md section 3, the `research.runtime.timeouts.<role>` row); the template holds
    the mapping as one placeholder, which is a line for every role. A fixed value above a path
    does not stand in for it, and dropping the mapping line leaves the roles without one."""
    for mode in MODES:
        held = _held(_template(mode))
        assert held["research.runtime.timeouts"] == "placeholder"
        for role in ("proposer", "implementer", "notes", "audit", "reproducer", "reviewer"):
            assert _has_line(held, "research.runtime.timeouts." + role), (mode, role)
        cut = {h: v for h, v in held.items() if h != "research.runtime.timeouts"}
        assert not _has_line(cut, "research.runtime.timeouts.proposer"), mode
    assert not _has_line({"research.mode": "improve"}, "research.mode.sub")
    assert _has_line({"research.gates[].stage": "placeholder"}, "research.gates")


def test_each_template_comment_names_a_slot_that_exists():
    ids = {s["id"] for s in _yaml(DEFAULT_SLOTS)["slots"]} | set(_slot_fields())
    for mode in MODES:
        text = open(os.path.join(TEMPLATES, "%s.yaml" % mode), encoding="utf-8").read()
        named = set()
        for line in text.splitlines():
            for m in re.finditer(r"slot: ([a-z0-9, -]+)$", line.rstrip()):
                named |= {n.strip() for n in m.group(1).split(",")}
        named.discard("none")
        assert named and named <= ids, named - ids
        # every research slot is named beside the fields it fills; repair has no metric lines
        metric_only = {"research-metric", "research-noise", "research-artifact", "research-targets"}
        want = set(_slot_fields()) - (metric_only if mode == "repair" else set())
        assert want <= named, (mode, want - named)
        assert "bin/alpaca interview use research" in text
        assert "bin/alpaca runbook check <this file> --spec <your spec>" in text
        for line in text.splitlines():
            if "<FILL:" in line and not line.lstrip().startswith("#"):
                assert re.search(r"#.*\b(REQUIRED|OPTIONAL|one item per)", line), line


# ------------------------------------------------------------------------------ 6.5 fill
@pytest.mark.parametrize("mode", MODES)
def test_the_template_filled_with_the_worked_values_is_the_worked_runbook(mode):
    worked = _yaml(os.path.join(WORKED, "%s.yaml" % mode))
    assert _filled(mode) == worked


@pytest.mark.parametrize("mode", MODES)
def test_the_filled_template_passes_the_format_3_check(tmp_path, mode):
    """t-104's done bar: the template filled with the worked example passes the product format 3
    check, `runbook.check` with its spec."""
    path, spec = _write_pack(tmp_path, mode, _filled(mode))
    result = runbook.check(path, spec_path=spec)
    assert result["verdict"] == "PASS", result["errors"]
    assert result["errors"] == []


@pytest.mark.parametrize("mode", MODES)
def test_the_unfilled_template_fails_with_field_unfilled_and_no_unknown_field(tmp_path, mode):
    path, spec = _write_pack(tmp_path, mode, text=open(os.path.join(TEMPLATES, "%s.yaml" % mode),
                                                       encoding="utf-8").read())
    codes = {f["code"] for f in _errors(path, spec)}
    assert "FIELD-UNFILLED" in codes and "FIELD-UNKNOWN" not in codes, codes


def test_a_value_the_template_has_no_line_for_fails_the_fill():
    values = dict(_values("improve"), **{"research.metrics[0].colour": "blue"})
    with pytest.raises(AssertionError, match=re.escape("research.metrics[0].colour")):
        fill(_template("improve"), values)


def test_the_fill_drops_unfilled_lines_and_item_patterns_with_no_index():
    filled = _filled("improve")
    assert "knobs" not in filled and "repo" not in filled["research"]
    assert "coupled" not in filled["research"] and "runtime" not in filled["research"]
    assert "seeds" not in filled["research"]["metrics"][0]
    assert filled["research"]["metrics"][1]["seeds"] == [11, 12, 13, 14, 15]


# ----------------------------------------------------- the format 3 check has teeth
def _broken(tmp_path, mode, edit):
    doc = _filled(mode)
    edit(doc)
    path, spec = _write_pack(tmp_path, mode, doc)
    return {(f["code"], f["where"]) for f in _errors(path, spec)}


def test_the_check_names_a_missing_noisy_margin(tmp_path):
    found = _broken(tmp_path, "improve", lambda d: d["research"]["metrics"][1].pop("margin"))
    assert found == {("FIELD-MISSING", "research.metrics[1].margin")}


def test_the_check_names_a_target_without_a_spec_or_owner_source(tmp_path):
    def edit(d):
        d["research"]["metrics"][0]["source"] = "default"
    assert _broken(tmp_path, "improve", edit) == {("TARGET-SOURCE", "research.metrics[0].source")}


def test_the_check_refuses_a_metric_in_repair_mode(tmp_path):
    def edit(d):
        d["research"]["metrics"] = copy.deepcopy(_filled("improve")["research"]["metrics"][:1])
    assert ("FIELD-UNKNOWN", "research.metrics") in _broken(tmp_path, "repair", edit)


def test_the_check_names_an_uncovered_success_criterion(tmp_path):
    def edit(d):
        d["research"]["stop"].pop("covers")
    assert _broken(tmp_path, "improve", edit) == {("SPEC-UNCOVERED", "SC-004")}


def test_the_check_names_a_stage_without_a_timeout(tmp_path):
    assert _broken(tmp_path, "repair", lambda d: d["stages"][0].pop("timeout")) == \
        {("TIMEOUT-MISSING", "stages[0].timeout")}


def test_the_check_names_a_required_field_the_defaults_file_does_not_set(tmp_path):
    def edit(d):
        d["research"].pop("defaults")
    found = _broken(tmp_path, "improve", edit)
    assert ("FIELD-MISSING", "research.disk") in found
    # research.budget is optional since t-297: a runbook with no budget is uncapped
    assert ("FIELD-MISSING", "research.budget") not in found


def test_the_check_names_a_case_from_the_store_without_tests(tmp_path):
    def edit(d):
        d["research"]["repair"]["cases"][0]["fail_to_pass"].pop("tests")
    assert _broken(tmp_path, "repair", edit) == {("FIELD-MISSING", "research.repair.cases[0].fail_to_pass.tests")}


# ------------------------------------------------------------------------------ plain files
def test_the_slot_file_and_the_templates_are_plain_ascii_with_no_banned_word():
    for path in (SLOTS, os.path.join(TEMPLATES, "improve.yaml"), os.path.join(TEMPLATES, "repair.yaml")):
        text = open(path, encoding="utf-8").read()
        assert chr(0x2014) not in text, path
        assert all(ord(c) < 128 for c in text), path
        low = text.lower()
        for word in BANNED:
            assert not re.search(r"\b%s" % word, low), (path, word)
