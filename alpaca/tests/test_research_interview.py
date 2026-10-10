"""t-104: the research interview slots (design/research-engine/operator-surface.md sections 6.1 to
6.3 and 6.7).

templates/interview/research-slots.yaml holds fourteen slots a format 3 research contract needs.
`alpaca interview use research` turns them on for a project: it writes `include: [research]` into
input/interview/slots.yaml, and the slot map becomes the twelve defaults, then the fourteen research
slots, then the project's own. Status lists them, readback and sign-off carry them, and a sign-off
from before the set was turned on is stale. An unknown set, a set slot with a default id, or an
include list of a shape `use` cannot edit is refused. The research-artifact slot asks whether every
component of a metric is measured on one artifact (gap map F43).
"""
import json
import os
import re
import shutil

import pytest
import yaml

from alpaca import interview, surface
from alpaca.tests.conftest import REPO

DEFAULT_SLOTS = ["goal", "scope-out", "done-bar", "thresholds", "edge-cases", "failures", "never",
                 "owner-gates", "rollback", "evidence", "knobs", "commands"]
RESEARCH_SLOTS = ["research-mode", "research-metric", "research-noise", "research-artifact",
                  "research-targets", "research-accept", "research-editable", "research-protected",
                  "research-gates", "research-stop", "research-budget", "research-proposal",
                  "research-controls", "research-repair"]
SLOT_FILE = os.path.join(REPO, "templates", "interview", "research-slots.yaml")
SKILL = os.path.join(REPO, ".claude", "skills", "alpaca-interview", "SKILL.md")
DOC = os.path.join(REPO, "docs", "interview.md")


@pytest.fixture
def clock(monkeypatch):
    from alpaca import util
    now = {"t": "2026-09-30T10:00:00+00:00"}
    monkeypatch.setattr(util, "_CLOCK", lambda: now["t"])
    return now


def _cli(argv, capsys):
    from alpaca import cli
    rc = cli.main(argv)
    out = capsys.readouterr().out
    try:
        return rc, json.loads(out)
    except ValueError:
        return rc, out


def _set(capsys, slot, state, value, source, reason=None):
    argv = ["interview", "set", slot, "--" + state, "--value", value, "--source", source]
    if reason is not None:
        argv += ["--reason", reason]
    rc, out = _cli(argv, capsys)
    assert rc == 0, out
    return out


def _slots_file(project):
    return os.path.join(project, "input", "interview", "slots.yaml")


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ------------------------------------------------------------------------------ the slot file
def test_the_research_slot_file_has_fourteen_slots_each_with_fills_and_fields():
    data = yaml.safe_load(_read(SLOT_FILE))
    assert [s["id"] for s in data["slots"]] == RESEARCH_SLOTS
    for s in data["slots"]:
        assert s["fills"].strip() and s["becomes"].strip(), s["id"]
        assert s["fields"] and all(isinstance(f, str) and f for f in s["fields"]), s["id"]
        assert s["mode"] in ("both", "improve", "repair"), s["id"]
        if s["mode"] != "both":
            assert s["waive"].strip(), s["id"]                 # the reason to give when waiving it
    # the loader reads it like any slot file and keeps id, fills and becomes
    read = interview._read_slots(SLOT_FILE, "research-slots.yaml")
    assert [s["id"] for s in read] == RESEARCH_SLOTS


def test_the_artifact_slot_asks_whether_every_metric_component_is_measured_on_one_artifact():
    """Gap map F43: the metric contract question folded into t-104."""
    slot = [s for s in yaml.safe_load(_read(SLOT_FILE))["slots"] if s["id"] == "research-artifact"][0]
    assert slot["fields"] == ["research.metrics[].artifact"]
    fills = " ".join(slot["fills"].split())
    assert "every component of each metric is measured on one artifact" in fills
    assert "no" in slot["becomes"] and "before sign-off" in slot["becomes"]


def test_a_project_that_never_turns_research_on_keeps_the_twelve_defaults(project):
    assert interview.slot_ids(project) == DEFAULT_SLOTS
    assert not os.path.exists(_slots_file(project))
    assert interview.sets() == ["intent", "research"]


# ---------------------------------------------------------------------- alpaca interview use
def test_use_research_adds_the_fourteen_slots_after_the_twelve_defaults(project, capsys, clock):
    rc, out = _cli(["interview", "use", "research", "--json"], capsys)
    assert rc == 0, out
    assert out == {"verdict": "PASS", "file": "input/interview/slots.yaml",
                   "slots": DEFAULT_SLOTS + RESEARCH_SLOTS, "progress": "0/26 slots settled"}
    assert set(out) <= set(surface.TABLE["interview"]["keys"])
    data = yaml.safe_load(_read(_slots_file(project)))
    assert data["include"] == ["research"] and data["slots"] == []
    assert interview.slot_ids(project) == DEFAULT_SLOTS + RESEARCH_SLOTS
    assert [s["from"] for s in interview.slots(project)] == ["default"] * 12 + ["research"] * 14


def test_interview_status_lists_the_research_slots_and_fails_until_they_are_settled(project, capsys, clock):
    _cli(["interview", "use", "research"], capsys)
    for slot in DEFAULT_SLOTS:
        _set(capsys, slot, "answered", "value of %s" % slot, "owner")
    rc, out = _cli(["interview", "status", "--json"], capsys)
    assert rc == 1 and out["verdict"] == "FAIL"
    assert out["progress"] == "12/26 slots settled" and out["open"] == RESEARCH_SLOTS
    assert [s["slot"] for s in out["slots"]] == DEFAULT_SLOTS + RESEARCH_SLOTS
    rc, text = _cli(["interview", "status"], capsys)
    assert rc == 1
    for slot in RESEARCH_SLOTS:
        assert re.search(r"^  %s +open " % re.escape(slot), text, re.M), slot
    for slot in RESEARCH_SLOTS:
        if slot in ("research-repair",):
            _set(capsys, slot, "waived", "", "round:1/q1", reason="improve mode")
        else:
            _set(capsys, slot, "answered", "value of %s" % slot, "round:1/q2")
    rc, out = _cli(["interview", "status", "--json"], capsys)
    assert rc == 0 and out["progress"] == "26/26 slots settled" and out["open"] == []


def test_use_prints_the_slot_count_before_and_after_and_is_idempotent(project, capsys, clock):
    rc, text = _cli(["interview", "use", "research"], capsys)
    assert rc == 0
    assert "(12 -> 26 slots)" in text and "0/26 slots settled" in text and "PASS" in text
    first = _read(_slots_file(project))
    rc, text = _cli(["interview", "use", "research"], capsys)
    assert rc == 0 and "already on" in text and "(26 -> 26 slots)" in text
    assert _read(_slots_file(project)) == first
    assert interview.slot_ids(project) == DEFAULT_SLOTS + RESEARCH_SLOTS


def test_use_keeps_the_project_slots_and_appends_the_include_line(project, capsys, clock):
    own = ("# our slots\nslots:\n  - id: data-retention\n    fills: how long a stored link is kept\n"
           "  - id: research-stop\n    fills: our own stop question\n")
    _write(_slots_file(project), own)
    with pytest.raises(interview.InterviewError, match="unknown slot"):
        interview.append(project, "research-mode", "answered", "x", "owner")
    rc, out = _cli(["interview", "use", "research", "--json"], capsys)
    assert rc == 0, out
    text = _read(_slots_file(project))
    assert text.startswith(own) and text[len(own):] == "include: [research]\n"
    ids = interview.slot_ids(project)
    assert ids == DEFAULT_SLOTS + RESEARCH_SLOTS + ["data-retention"]
    stop = [s for s in interview.slots(project) if s["id"] == "research-stop"][0]
    assert stop["from"] == "project" and stop["fills"] == "our own stop question"


def test_use_adds_the_set_to_a_one_line_include_list(project, capsys, clock):
    _write(_slots_file(project), "include: []   # sets\nslots: []\n")
    rc, out = _cli(["interview", "use", "research", "--json"], capsys)
    assert rc == 0, out
    assert _read(_slots_file(project)) == "include: [research]  # sets\nslots: []\n"


def test_use_refuses_an_include_list_it_cannot_edit_and_writes_nothing(project, capsys, clock):
    block = "include:\n  - research\nslots: []\n"
    for text in (block, "include: [research]\nslots: []\ninclude: []\n"):
        _write(_slots_file(project), text)
        rc, out = _cli(["interview", "use", "research"], capsys)
        assert rc == 1, out
        assert "by hand" in out
        assert _read(_slots_file(project)) == text


def test_an_unknown_set_is_blocked(project, capsys, clock):
    rc, out = _cli(["interview", "use", "colour", "--json"], capsys)
    assert rc == 2 and out["verdict"] == "BLOCKED" and "research" in out["reason"]
    assert not os.path.exists(_slots_file(project))
    for bad in ("../slots", "Research"):
        rc, out = _cli(["interview", "use", bad, "--json"], capsys)
        assert rc == 2 and out["verdict"] == "BLOCKED" and "lower-case" in out["reason"]
        assert not os.path.exists(_slots_file(project))
    _write(_slots_file(project), "include: [colour]\nslots: []\n")
    rc, out = _cli(["interview", "status", "--json"], capsys)
    assert rc == 2 and out["verdict"] == "BLOCKED" and "colour" in out["reason"]


@pytest.fixture
def product(tmp_path, monkeypatch):
    """A product root of our own with the default slots, for slot sets the real product lacks."""
    root = tmp_path / "product"
    (root / "templates" / "interview").mkdir(parents=True)
    shutil.copy(os.path.join(REPO, "templates", "interview", "slots.yaml"),
                str(root / "templates" / "interview" / "slots.yaml"))
    monkeypatch.setattr(interview, "product_root", lambda: str(root))
    return root / "templates" / "interview"


def test_a_set_slot_with_a_default_id_is_blocked(project, product, capsys, clock):
    (product / "clash-slots.yaml").write_text("slots:\n  - id: goal\n    fills: another goal\n")
    rc, out = _cli(["interview", "use", "clash", "--json"], capsys)
    assert rc == 2 and out["verdict"] == "BLOCKED" and "goal" in out["reason"]
    assert not os.path.exists(_slots_file(project))           # refused before the write
    _write(_slots_file(project), "include: [clash]\nslots: []\n")
    rc, out = _cli(["interview", "status", "--json"], capsys)
    assert rc == 2 and "default slot" in out["reason"]


def test_sets_are_read_in_the_listed_order_and_may_not_repeat_a_slot(project, product, capsys, clock):
    (product / "one-slots.yaml").write_text("slots:\n  - id: alpha\n    fills: a\n")
    (product / "two-slots.yaml").write_text("slots:\n  - id: beta\n    fills: b\n")
    _write(_slots_file(project), "include: [two, one]\nslots: []\n")
    assert interview.slot_ids(project) == DEFAULT_SLOTS + ["beta", "alpha"]
    (product / "dup-slots.yaml").write_text("slots:\n  - id: alpha\n    fills: again\n")
    _write(_slots_file(project), "include: [one, dup]\nslots: []\n")
    with pytest.raises(interview.InterviewError) as err:
        interview.slot_ids(project)
    assert err.value.code == interview.BLOCKED and "alpha" in str(err.value)


# --------------------------------------------------------------------- readback and sign-off
def _settle_everything(capsys):
    for slot in DEFAULT_SLOTS:
        _set(capsys, slot, "answered", "value of %s" % slot, "owner")
    for n, slot in enumerate(RESEARCH_SLOTS, 1):
        if slot == "research-repair":
            _set(capsys, slot, "waived", "", "round:1/q%d" % n, reason="improve mode")
        elif slot == "research-stop":
            _set(capsys, slot, "default", "5 rounds, 2 slots", "round:1/q%d" % n)
        else:
            _set(capsys, slot, "answered", "value of %s" % slot, "round:1/q%d" % n)


def test_readback_and_signoff_carry_the_research_slots(project, capsys, clock):
    from alpaca import db
    _cli(["interview", "use", "research"], capsys)
    _settle_everything(capsys)
    rc, rb = _cli(["interview", "readback", "--json"], capsys)
    assert rc == 0
    added = [i["slot"] for i in rb["buckets"]["added"]]
    assert [s for s in RESEARCH_SLOTS if s not in ("research-repair", "research-stop")] == \
        [s for s in added if s.startswith("research-")]
    assert [i["slot"] for i in rb["buckets"]["default"]] == ["research-stop"]
    assert [(i["slot"], i["reason"]) for i in rb["buckets"]["waived"]] == [("research-repair", "improve mode")]
    rc, text = _cli(["interview", "readback"], capsys)
    assert "- research-artifact: value of research-artifact (round:1/q4)" in text
    rc, out = _cli(["interview", "signoff", "--by", "the operator", "--json"], capsys)
    assert rc == 0, out
    signed = _read(os.path.join(project, out["file"]))
    head = signed.split("---\n")[1]
    assert "slots: %s\n" % " ".join(DEFAULT_SLOTS + RESEARCH_SLOTS) in head
    assert "- research-repair: improve mode (round:1/q14)" in signed
    events = db.events(db.connect(project), kind="interview-signoff")
    assert events[-1]["data"]["slots"] == DEFAULT_SLOTS + RESEARCH_SLOTS
    assert events[-1]["data"]["total"] == 26
    assert interview.state(project) == "signed"


def test_signoff_refuses_while_a_research_slot_is_open(project, capsys, clock):
    _cli(["interview", "use", "research"], capsys)
    for slot in DEFAULT_SLOTS:
        _set(capsys, slot, "answered", "value of %s" % slot, "owner")
    rc, out = _cli(["interview", "signoff", "--by", "the operator", "--json"], capsys)
    assert rc == 1 and "research-mode" in out["reason"]


def test_a_signoff_from_before_use_is_stale(project, capsys, clock):
    for slot in DEFAULT_SLOTS:
        _set(capsys, slot, "answered", "value of %s" % slot, "owner")
    rc, out = _cli(["interview", "signoff", "--by", "the operator", "--json"], capsys)
    assert rc == 0 and interview.state(project) == "signed"
    _cli(["interview", "use", "research"], capsys)
    st = interview.signoff_state(project)
    assert st["state"] == "stale"
    assert "the slot map gained %s" % ", ".join(RESEARCH_SLOTS) in st["why"]
    rc, text = _cli(["interview", "status"], capsys)
    assert rc == 1 and "sign-off: stale" in text


# ------------------------------------------------------------------ the skill, the docs, the manual
def test_the_skill_has_a_research_probe_bank_with_two_probes_per_research_slot():
    text = _read(SKILL)
    bank = text.split("## Probe bank", 1)[1].split("\n## ", 1)[0]
    sections = dict(re.findall(r"^### `([a-z-]+)`\n(.*?)(?=^### |\Z)", bank, re.M | re.S))
    for slot in RESEARCH_SLOTS:
        assert slot in sections, slot
        probes = [l for l in sections[slot].splitlines() if l.startswith("- ")]
        assert len(probes) >= 2, (slot, probes)
    artifact = " ".join(sections["research-artifact"].split()).lower()
    assert "same build" in artifact
    flat = " ".join(text.split())
    assert "bin/alpaca interview use research" in flat


def test_the_docs_and_the_manual_name_the_research_slots_and_use():
    doc = " ".join(_read(DOC).split())
    assert "alpaca interview use research" in doc and "templates/interview/research-slots.yaml" in doc
    assert "templates/research/improve.yaml" in doc and "templates/research/repair.yaml" in doc
    for slot in RESEARCH_SLOTS:
        assert "`%s`" % slot in doc, slot
    manual = _read(os.path.join(REPO, "MANUAL.md"))
    line = [l for l in manual.splitlines() if l.startswith("- `alpaca interview`")][0]
    assert "`alpaca interview use <set>`" in line and "research" in line
    # the field reference pointer resolves in a shipped tree (check round 1 B1, round 2): it names a
    # function of a file that ships, never a development tree such as design/
    for path in (DOC, os.path.join(REPO, "templates", "interview", "research-slots.yaml")):
        text = " ".join(_read(path).replace("#", " ").split())
        assert "`research_fields()` in `alpaca/runbook.py`" in text, path
        assert "design/" not in text, path
        assert _dangling_code_pointers(_read(path)) == [], path


def _shipment_forbidden():
    """The trees setup/ship.py never ships (its FORBIDDEN set), read from the file that ships."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("t104_ship", os.path.join(REPO, "setup", "ship.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.FORBIDDEN


def _dangling_code_pointers(text, root=REPO):
    """The `` `name()` in `file.py` `` pointers of a text that do not resolve in a shipped tree
    under root: the file is missing, lies under a tree setup/ship.py never ships, or defines no
    such function. The text must hold at least one pointer."""
    text = " ".join(re.sub(r"(?m)^\s*#\s?", "", text).split())
    found = re.findall(r"`(\w+)\(\)` in `([\w./-]+\.py)`", text)
    assert found, "no code pointer"
    forbidden = _shipment_forbidden()
    bad = []
    for name, rel in found:
        target = os.path.join(root, rel)
        ships = not any(rel == f or rel.startswith(f + "/") for f in forbidden)
        body = _read(target) if os.path.exists(target) else ""
        if not ships or not re.search(r"(?m)^def %s\(" % re.escape(name), body):
            bad.append((name, rel))
    return bad


def test_a_pointer_to_a_function_the_file_lacks_is_dangling(tmp_path):
    """The pointer check has teeth: a pointer to a function its file does not define is named, so
    is a pointer to a file that does not exist, and so is one into a tree that never ships even
    when the file is there and defines the function."""
    bad = "`no_such_function()` in `alpaca/runbook.py` lists every field."
    assert _dangling_code_pointers(bad) == [("no_such_function", "alpaca/runbook.py")]
    gone = "`research_fields()` in `alpaca/no_such_module.py` lists every field."
    assert _dangling_code_pointers(gone) == [("research_fields", "alpaca/no_such_module.py")]
    for rel in ("design/research-engine/fields.py", "alpaca/fields.py"):
        os.makedirs(os.path.dirname(str(tmp_path / rel)), exist_ok=True)
        (tmp_path / rel).write_text("def research_fields():\n    return []\n")
    dev = "`research_fields()` in `design/research-engine/fields.py` lists every field."
    assert _dangling_code_pointers(dev, root=str(tmp_path)) == [
        ("research_fields", "design/research-engine/fields.py")]
    shipped = "`research_fields()` in `alpaca/fields.py` lists every field."
    assert _dangling_code_pointers(shipped, root=str(tmp_path)) == []
    assert _dangling_code_pointers("`research_fields()` in `alpaca/runbook.py` lists them.") == []
