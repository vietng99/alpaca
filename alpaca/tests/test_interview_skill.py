"""The interview skill and the texts around it (docs/interview.md).

/alpaca-interview runs the interview in rounds of questions with options, probes for what the
operator does not know to say, reads the answers back in five buckets, signs off, and hands the
signed file to the spec step. /alpaca-from-notes and alpaca start send raw notes to the inbox and
the interview first; /alpaca-runbook-forge has a quiet mode and an interview mode.
"""
import os
import re

import yaml

from alpaca.tests.conftest import REPO

SKILL = os.path.join(REPO, ".claude", "skills", "alpaca-interview", "SKILL.md")
FROM_NOTES = os.path.join(REPO, ".claude", "skills", "alpaca-from-notes", "SKILL.md")
FORGE = os.path.join(REPO, ".claude", "skills", "alpaca-runbook-forge", "SKILL.md")
DOC = os.path.join(REPO, "docs", "interview.md")
BANNED = ("ensure", "robust", "seamless", "leverage", "utilize", "comprehensive", "crucial",
          "essential", "framework", "dynamic", "optimize", "streamline", "validate", "canonical",
          "granular", "insights", "harness", "journey", "navigate")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _flat(text):
    return " ".join(text.split())


def _default_slots():
    with open(os.path.join(REPO, "templates", "interview", "slots.yaml"), encoding="utf-8") as fh:
        return [s["id"] for s in yaml.safe_load(fh)["slots"]]


def test_the_skill_asks_in_rounds_with_options_and_a_recommended_default():
    text = _flat(_read(SKILL))
    assert text.startswith("--- name: alpaca-interview ")
    for must in ("up to 4 questions", "interactive question tool", "2 to 4 options",
                 "recommended default first", "alpaca interview status", "alpaca note list",
                 "alpaca interview set", "--answered", "--default", "--waived", "--reason"):
        assert must in text, must


def test_the_skill_turns_contradictions_into_questions():
    text = _flat(_read(SKILL)).lower()
    assert "contradict" in text and "question" in text.split("contradict", 1)[1][:300]


def test_the_skill_has_a_probe_bank_with_two_probes_per_default_slot():
    text = _read(SKILL)
    assert "## Probe bank" in text
    bank = text.split("## Probe bank", 1)[1].split("\n## ", 1)[0]
    sections = dict(re.findall(r"^### `([a-z-]+)`\n(.*?)(?=^### |\Z)", bank, re.M | re.S))
    for slot in _default_slots():
        assert slot in sections, slot
        probes = [l for l in sections[slot].splitlines() if l.startswith("- ")]
        assert len(probes) >= 2, (slot, probes)
    flat = _flat(bank).lower()
    for probe in ("what input would break this?", "what must never happen, even on a retry?",
                  "how would someone else know it is done without asking you?",
                  "if this goes wrong in use, how do we undo it?"):
        assert probe in flat, probe


def test_the_skill_reads_back_signs_off_and_hands_over():
    text = _flat(_read(SKILL))
    for must in ("alpaca interview readback", "Clear from the start", "Added during the interview",
                 "Filled by default", "Waived", "Drifted", "Approve", "Adjust", "One more round",
                 "alpaca interview signoff --by", "/speckit-specify", "/opsx:propose",
                 "EC-001", "SC-", "fail case", "stop_on", "absent"):
        assert must in text, must
    assert "done-bar" in text and "thresholds" in text and "edge-cases" in text


def test_the_skill_records_defaults_when_no_one_answers():
    text = _flat(_read(SKILL))
    assert "round:<n>/q<n>" in text and "--default" in text
    no_one = text.lower().split("no one to answer", 1)
    assert len(no_one) == 2 and "readback" in no_one[1][:600]


def test_from_notes_sends_raw_notes_to_the_inbox_and_the_interview_first():
    text = _flat(_read(FROM_NOTES))
    assert "alpaca note add" in text and "/alpaca-interview" in text
    assert text.index("alpaca note add") < text.index("/speckit-specify")
    assert text.index("/alpaca-interview") < text.index("/speckit-specify")
    assert "signed" in text


def test_the_forge_has_a_quiet_mode_and_an_interview_mode():
    text = _flat(_read(FORGE))
    assert "Quiet mode" in text and "Interview mode" in text
    for must in ("runbook: 2", "source: interview:<slot>", "`detect`", "`then`", "recovery: true",
                 "up to 4 questions", "a partner"):
        assert must in text, must


def test_the_docs_describe_the_interview_and_link_it():
    doc = _flat(_read(DOC))
    for must in ("alpaca note add", "alpaca note list", "alpaca interview status",
                 "alpaca interview set", "alpaca interview readback", "alpaca interview signoff",
                 "input/notes/", "input/interview/log.jsonl", "input/interview/slots.yaml",
                 "templates/interview/slots.yaml", "/alpaca-interview", "stale"):
        assert must in doc, must
    for slot in _default_slots():
        assert "`%s`" % slot in doc, slot
    intake = _read(os.path.join(REPO, "docs", "intake.md"))
    path_line = intake.split("```", 2)[1]
    assert "interview" in path_line and "docs/interview.md" in intake
    manual = _read(os.path.join(REPO, "MANUAL.md"))
    assert "`alpaca note`" in manual and "`alpaca interview`" in manual
    assert "docs/interview.md" in manual and "/alpaca-interview" in manual


def test_the_new_texts_ship_as_mechanism_paths():
    text = _read(os.path.join(REPO, "ALPACA-MANIFEST"))
    for path in ("docs/interview.md", ".claude/skills/alpaca-interview/"):
        assert "\n%s\n" % path in text, path


def test_the_new_texts_are_plain():
    for path in (SKILL, DOC, os.path.join(REPO, "templates", "interview", "slots.yaml")):
        text = _read(path)
        assert chr(0x2014) not in text, path
        assert all(ord(c) < 128 for c in text), path
        low = text.lower()
        for word in BANNED:
            assert not re.search(r"\b%s" % word, low), (path, word)


# ------------------------------------------------------------------ review and trial fixes (op-003)
def test_from_notes_names_edge_cases_in_the_move_to_openspec():
    text = _flat(_read(FROM_NOTES))
    assert "one requirement per `SC-nnn`, `EC-nnn` and `FR-nnn`" in text


def test_from_notes_numbers_the_edge_cases_that_spec_kit_leaves_unnumbered():
    """spec-kit's own template writes edge cases as unnumbered questions; the numbering rule is
    Alpaca's, so the skill says to number them and names the error the check gives otherwise."""
    step = _flat(_read(FROM_NOTES).split("## Step 2a", 1)[1].split("\n## ", 1)[0])
    assert "does not number" in step and "- **EC-001**: ..." in step and "EC-UNNUMBERED" in step


def test_a_rollback_a_person_approves_is_not_a_recovery_stage():
    """A recovery stage runs with no one to wait for, so it is never an owner gate: a restore
    that a person must approve first is an ask-owner answer plus a gated stage."""
    for path in (SKILL, FORGE):
        text = _flat(_read(path))
        low = text.lower()
        assert "never an owner gate" in low, path
        assert "then: ask-owner" in text and "owner_gate" in text, path
        part = low.split("never an owner gate", 1)[1][:600] + low.split("never an owner gate", 1)[0][-600:]
        assert "restore" in part or "undo" in part, path


def test_the_forge_records_the_answers_of_its_own_rounds():
    """The forge's rounds answer what the signed slots lack; each answer goes into the slot it
    fills, so the brief holds every fact the runbook depends on, and the operator signs again."""
    text = _flat(_read(FORGE))
    mode = text.split("Interview mode", 1)[1].split("## Before you start", 1)[0]
    for must in ("alpaca interview set", "--source round:<n>/q<n>", "alpaca interview readback",
                 "signs off again"):
        assert must in mode, must
    doc = _flat(_read(DOC)).split("## After the sign-off", 1)[1]
    assert "alpaca interview set" in doc and "signs off again" in doc


def test_the_interview_does_not_stop_on_a_thin_slot():
    """status exits 0 once every slot has a value; a slot is only done when its value answers
    the probes the runbook needs (commands: build, run, test, measure; thresholds: the load or
    size the number holds under). A note that answers part of a slot leaves the rest to ask."""
    text = _flat(_read(SKILL))
    step2 = text.split("## Step 2: ask in rounds", 1)[1].split("## Step 3", 1)[0]
    assert "thin" in step2 and "probe" in step2
    assert "load or size" in step2 and "build, run, test" in step2
    assert "answers part of a slot" in text


def test_a_repeated_proposal_in_the_operators_words_is_an_answer():
    text = _flat(_read(SKILL))
    rules = text.split("## How an answer is recorded", 1)[1].split("## Step 1", 1)[0]
    assert "repeats" in rules and "--answered" in rules.split("repeats", 1)[1][:300]
