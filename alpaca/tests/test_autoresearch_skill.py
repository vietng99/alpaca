"""t-106: the /autoresearch skill (.claude/skills/autoresearch/SKILL.md) and the one WARN check it
adds to `alpaca doctor`; design/research-engine/operator-surface.md sections 3 and 5.

Done when (the contract of t-106):

  * the skill follows the repo's skill-writing conventions: front matter with a name equal to its
    folder, a short opening that names the verbs, numbered steps with exact `bin/alpaca`
    commands, a "What only the owner types" section and a "Never" section at the end, pure ASCII,
    no em dash, none of the words the interview texts ban, no link into design/;
  * a dry run reaches the start of a run: on a fixture project with the fake loop runner of
    test_research_cli.py, a driver runs the verbs in the order of the skill's steps and ends at
    the first `research start` (the no-op control, since the contract's burn-in is needed), whose
    run-start event says `via: autoresearch`;
  * it never raises the level on its own: outside "## Never" and "## What only the owner types"
    the text holds none of the level writers, and the dry runs record no `level` decision between
    a run's start and its finish.

Fail case: the skill continues past a ship or spend decision. A driver applies the exit-code rule
it reads from the skill's table; in three cases (a budget hold, a ship card, the owner's "no" at
two holds) it stops at the first exit 3, and the record holds no research-launch, research-merge
or research-resume event and no refused attempt to go on after the hold. A control test edits the
table to say "continues" and shows the same driver then tries to resume and is refused, so the
checks above do fail when the rule is removed.

The loop runner is faked behind runner.open_run and runner.launch (test_research_cli.py). No test
touches the network or launches an agent runtime.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys

import pytest

from alpaca import cli, db, doctor, util
from alpaca.gates import verdict as vc
from alpaca.posture import level as level_mod, loops
from alpaca.research import catalog, events
from alpaca.research import cli as research_cli
from alpaca.tests import research_loopkit as kit
from alpaca.tests.conftest import REPO
from alpaca.tests.test_research_cli import (FakeRunner, RB, SESSION, _call, _conn, _ev,
                                            _finalize, _grant, _hold)
from alpaca.tests.plain_op import plain_op

SKILL = os.path.join(REPO, ".claude", "skills", "autoresearch", "SKILL.md")
BANNED = ("ensure", "robust", "seamless", "leverage", "utilize", "comprehensive", "crucial",
          "essential", "framework", "dynamic", "optimize", "streamline", "validate", "canonical",
          "granular", "insights", "harness", "journey", "navigate")
#: written only in the two owner-facing sections: the writers of the level and the owner's verbs
OWNER_ONLY = ("/autodrive L", "session level", "--level", ".autodrive-level", ".autodrive-goal",
              "default_level", "level.set", "level.mirror", "research grant", "review move")
OWNER_SECTION = "What only the owner types"
NEVER_SECTION = "Never"
EXIT_SECTION = "What the skill does with each exit code"


def _text():
    with open(SKILL, encoding="utf-8") as fh:
        return fh.read()


def _flat(text):
    return " ".join(text.split())


def _sections(text):
    """{heading: body} for each `## ` section, in file order (the opening is under '')."""
    out, name, buf = {}, "", []
    for line in text.splitlines():
        if line.startswith("## "):
            out[name] = "\n".join(buf)
            name, buf = line[3:].strip(), []
        else:
            buf.append(line)
    out[name] = "\n".join(buf)
    return out


def _outside_owner_sections(text):
    sec = _sections(text)
    return "\n".join(body for name, body in sec.items() if name not in (OWNER_SECTION,
                                                                          NEVER_SECTION))


def _exit_rules(text):
    """{exit code: (stops, row text)} read from the skill's exit-code table. A row stops the flow
    when its text says "stops"; the PASS row continues."""
    rules = {}
    for line in _sections(text)[EXIT_SECTION].splitlines():
        m = re.match(r"\| (\d+) \| ([A-Z-]+) \| (.*) \|\s*$", line)
        if m:
            rules[int(m.group(1))] = ("stops" in m.group(3), m.group(3))
    return rules


def _start_argv(text, *, control, rb):
    """The `research start` argv the skill's step 3 (control) or step 4 prints, with the runbook
    path filled and the optional `[--task <id>]` left out."""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("bin/alpaca research start"):
            continue
        if ("--control noop" in line) != control:
            continue
        words = shlex.split(line.replace("[--task <id>]", "").replace("<path>", rb))
        return [w for w in words[2:] if w != "--json"]    # after `bin/alpaca research`; _call adds it
    raise AssertionError("the skill prints no start command with control=%s" % control)


# ------------------------------------------------------------------ the file and its conventions
def test_the_skill_is_a_claude_code_project_skill_named_autoresearch():
    text = _text()
    assert text.startswith("---\n")
    head = text[4:text.index("\n---\n", 4)]
    meta = dict(re.findall(r"^([a-z]+): *(.*)$", head, re.M))
    assert meta.get("name") == "autoresearch"
    assert meta.get("description")
    flat = _flat(head)
    for trigger in ("/autoresearch", "run a research loop", "improve this metric",
                    "repair this bug with the research engine"):
        assert trigger in flat, trigger
    # negative: the folder name and the name agree, so Claude Code offers /autoresearch
    assert os.path.basename(os.path.dirname(SKILL)) == meta["name"]


def test_the_skill_is_pure_ascii_with_no_em_dash_and_no_banned_word():
    raw = open(SKILL, "rb").read()
    assert raw.isascii()
    assert chr(0x2014) not in raw.decode("ascii")
    low = raw.decode("ascii").lower()
    for word in BANNED:
        assert not re.search(r"\b%s" % word, low), word


def test_the_skill_never_points_into_the_design_folder_or_an_old_skill_path():
    text = _text()
    assert "design/" not in text                  # design/ is not shipped by ALPACA-MANIFEST
    old_paths = ("skills/" + "alpaca-", "alpaca-" + "intake")    # the strings a repo scan flags
    assert not any(bad in text for bad in old_paths)


def test_the_skill_has_the_conventional_shape():
    text = _text()
    sec = _sections(text)
    names = list(sec)
    # a short opening that names the verbs and the help text that lists them
    opening = _flat(sec[""])
    assert "bin/alpaca research" in opening and "bin/alpaca research --help" in opening
    assert len(sec[""].splitlines()) < 40            # short
    # numbered steps 0 to 8, in order, each a section
    steps = [n for n in names if n.startswith("Step ")]
    assert [n.split(":")[0] for n in steps] == ["Step %d" % i for i in range(9)]
    # the owner section and the Never section close the file, in that order
    assert names[-2:] == [OWNER_SECTION, NEVER_SECTION]


def test_the_skill_is_listed_in_the_manifest_and_digest_map(tmp_path):
    with open(os.path.join(REPO, "ALPACA-MANIFEST"), encoding="utf-8") as fh:
        lines = [line.strip() for line in fh]
    assert ".claude/skills/autoresearch/" in lines
    mech = doctor._manifest_paths(REPO)["mechanism"]
    assert ".claude/skills/autoresearch/" in mech
    # the folder name has no `alpaca-` prefix: it is its own line, not covered by a glob
    assert not any(m.rstrip("/") == ".claude/skills" or m.rstrip("/") == ".claude" for m in mech)
    # the digest map the manifest gives (setup/gen_manifest.py) carries the file with its sha256
    import importlib.util
    spec = importlib.util.spec_from_file_location("gen_manifest_t106",
                                                  os.path.join(REPO, "setup", "gen_manifest.py"))
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    tree = tmp_path / "tree"
    (tree / ".claude" / "skills" / "autoresearch").mkdir(parents=True)
    (tree / "ALPACA-MANIFEST").write_text("[mechanism]\n.claude/skills/autoresearch/\n[memory]\n"
                                          ".alpaca/\n", encoding="utf-8")
    (tree / ".claude" / "skills" / "autoresearch" / "SKILL.md").write_bytes(
        open(SKILL, "rb").read())
    built, walk_errors, read_errors = gen.build(str(tree))
    assert not walk_errors and not read_errors
    assert built["files"][".claude/skills/autoresearch/SKILL.md"] == util.sha256_hex(
        open(SKILL, "rb").read())
    # the committed MANIFEST.json is regenerated once at the end of the op (python3
    # setup/gen_manifest.py --write); when it lists the skill, the digest is the file's
    with open(os.path.join(REPO, "MANIFEST.json"), encoding="utf-8") as fh:
        listed = json.load(fh)["files"].get(".claude/skills/autoresearch/SKILL.md")
    assert listed in (None, util.sha256_hex(open(SKILL, "rb").read()))


def test_the_repos_lists_of_alpacas_own_skills_name_autoresearch():
    for rel in ("docs/operators.md", "README.md"):
        with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
            text = fh.read()
        assert "autoresearch" in text, rel
    with open(os.path.join(REPO, "README.md"), encoding="utf-8") as fh:
        readme = fh.read()
    # the heading glob `.claude/skills/alpaca-*` does not match the new folder, so it is named
    assert "`.claude/skills/autoresearch/`" in readme
    glob_line = [l for l in readme.splitlines() if "`.claude/skills/alpaca-*`" in l]
    assert glob_line and "`.claude/skills/autoresearch/`" in glob_line[0]


# ------------------------------------------------------------------ the text's content
def test_every_research_verb_the_skill_names_is_a_verb_research_help_lists(capsys):
    flat = _flat(_text())
    named = set(re.findall(r"bin/alpaca research ([a-z]+(?:-[a-z]+)*)", flat))
    assert {"start", "status", "contracts", "report", "resume", "extend", "stop",
            "template", "next-job", "submit"} <= named
    assert named <= set(research_cli.VERBS)
    assert cli.main(["research", "--help"]) == vc.PASS
    helped = capsys.readouterr().out
    for verb in named:
        assert verb in helped, verb
    # negative: a made-up verb is not listed, so the check above would catch it
    assert "launch-now" not in helped


def test_outside_never_and_the_owner_section_the_text_has_no_level_writer_or_owner_verb():
    outside = _outside_owner_sections(_text())
    for needle in OWNER_ONLY:
        assert needle not in outside, needle
    # control: the sections the split removed do carry them, so the split is real
    sec = _sections(_text())
    assert "/autodrive L" in sec[OWNER_SECTION] and "research grant" in sec[OWNER_SECTION]
    assert "review move" in sec[OWNER_SECTION]
    assert "session level" in sec[NEVER_SECTION] and ".autodrive-level" in sec[NEVER_SECTION]
    assert "default_level" in sec[NEVER_SECTION]


def test_the_never_section_forbids_raising_the_level_and_the_owner_only_acts():
    never = _flat(_sections(_text())[NEVER_SECTION])
    for must in ("Never raise the autodrive level, and never write it",
                 "no `alpaca session start --level`",
                 "Never run `bin/alpaca research grant`",
                 "terminal check", "Never move a review card", "`--by owner`",
                 "loosens a field or raises a budget cap", "`budget.totals`",
                 "Never merge the research branch", "push, publish or deploy",
                 "Never sign the interview off", "Never continue past exit 3 on the same run"):
        assert must in never, must


SESSION_NEVER = ("Never pass anything but `spawn_prompt` to a job's agent",
                 "never reuse an agent for a second job",
                 "never spawn a handed job again",
                 "never save a memory, or write any file the runtime loads into an agent, while a job "
                 "is open",
                 "Never pass `--repin-engine` or `--context-path`",
                 "never call `next-job` or `submit` from a subagent",
                 "Never start `claude -p` or `codex exec`")
SESSION_LEVEL = ("A run that this session drives cannot start at L1",
                 "`start` refuses with `RESEARCH-LEVEL-TOO-LOW`",
                 "The owner's yes to the start covers the drive loop of step 5")


def test_the_never_section_and_the_level_rows_carry_the_session_runtime_rules():
    flat = _flat(_text())
    never = _flat(_sections(_text())[NEVER_SECTION])
    for must in SESSION_NEVER:
        assert must in never, must
    for must in SESSION_LEVEL:
        assert must in flat, must
    # control: a skill without the rule fails the same check
    broken = _flat(_text()).replace("Never start `claude -p` or `codex exec`", "Start what you like")
    assert "Never start `claude -p` or `codex exec`" not in broken


def test_the_drive_loop_commands_parse_against_the_live_parser_and_name_the_job_fields():
    text = _text()
    flat = _flat(text)
    step5 = _flat(_sections(text)["Step 5: drive and watch"])
    for must in ("`next-job <run> --wait 45 --json`", "`spawn_prompt` exactly and nothing added",
                 "`model` set to the job's `model`", "Use the job's model, never another",
                 "TaskStop", "`next-job <run> --list`",
                 "submit <job> --nonce <nonce> --agent-id <id>"):
        assert must.replace("`", "") in step5.replace("`", ""), must
    # the one case where the spawn leaves `model` out: the job's model is this session's own, so
    # the agent inherits it (a run started with --model, F13)
    assert ("when the job's `model` is the model this session runs on" in step5
            and "the call may leave `model` out, and the agent inherits this session's model, which "
            "is the job's" in step5)
    # the start step passes --model when the owner requires one model, and names its two refusals
    step4 = _flat(_sections(text)["Step 4: start the run"])
    for must in ("When the owner requires one model for every agent of the run",
                 "add `--model <alias>` to this command and to the start command of step 3",
                 "every job of the run names it, the verifiers and the auditor included",
                 "`RUN-MODEL-UNKNOWN`", "`RUN-MODEL-MISMATCH`"):
        assert must in step4, must
    assert _parse(["research", "start", "--runbook", "r.yaml", "--model", "opus"]) is not None
    for verb in ("resume", "extend"):
        assert _parse(["research", verb, "rr-001", "--model", "opus"]) is not None
    assert "next-job --verification <vf> --wait 45" in flat.replace("`", "")
    assert "an agent's message beyond passing the agent id to `submit`" in step5
    for words in (["research", "next-job", "rr-001", "--wait", "45", "--json"],
                  ["research", "next-job", "rr-001", "--list"],
                  ["research", "next-job", "--verification", "vf-001", "--wait", "45"],
                  ["research", "submit", "j-1", "--nonce", "n", "--agent-id", "a1", "--json"],
                  ["research", "submit", "j-1", "--nonce", "n", "--agent-id", "a1",
                   "--usage-json", "u.json"],
                  ["research", "resume", "rr-001", "--repin-engine", "--context-path", "x"]):
        assert _parse(words) is not None, words
    # the loop ends at report or hold and the report waits for the runner to exit
    assert "answers `report` or `hold`" in flat and "only after the runner has exited" in flat


def test_the_skill_states_the_exit_code_rule_and_names_exit_3_as_a_stop():
    rules = _exit_rules(_text())
    assert set(rules) == {0, 1, 2, 3, 64, 67}
    assert rules[0][0] is False and "continues" in rules[0][1]
    for code in (1, 2, 3, 64, 67):
        assert rules[code][0] is True, code
    assert "step 6" in rules[3][1] and "never a prompt to try again" in rules[3][1]


def test_each_level_row_and_the_owner_gates_are_stated():
    flat = _flat(_text())
    for row in ("| L1 Assist |", "| L2 Partial |", "| L3 Conditional |", "| L4 High |",
                "| L5 Full |", "| L6 Never-defer |"):
        assert row in flat, row
    l6 = flat.split("| L6 Never-defer |", 1)[1].split(" |", 1)[0]
    for gate in ("ship", "spend past the budget", "extension past N rounds", "loosening a target",
                 "reopening a runbook the owner stopped"):
        assert gate in l6, gate
    # the level is read and the owner sets it
    assert "The skill reads the autodrive level and never sets it" in flat
    assert "`level_source`" in flat and "unreadable" in flat


def test_the_steps_run_in_the_order_of_the_design():
    flat = _flat(_text())
    order = ["research status --json", "research contracts --json", "research start --runbook",
             "--control noop", "research report"]
    at = [flat.index(item) for item in order]
    assert at == sorted(at), order
    # both start commands say who started the run (FR-052) and the control says what it is
    assert flat.count("--via autoresearch") == 2 and flat.count("--control noop") == 1
    # at a hold the skill prints the decision and stops, and never asks in a run at L5 and L6
    assert "never call the interactive question tool" in flat
    assert "does not ask the question itself" in flat or "Do not ask the question yourself" in flat


#: (section, phrase) pairs: the owner-decision rows and the stop rules of the steps, each of which
#: removes the stop at an owner gate when it changes (check-contract-s1 blocking 1 and 2)
SECTION_PINS = (
    ("Who decides", "| set or raise the autodrive level | the owner, always; the skill only states it |"),
    ("Who decides", "| loosen a field that sets the bar, or raise a budget cap above the pack value "
                    "| the owner, always |"),
    ("Who decides", "| extend a run past N rounds | the owner, always |"),
    ("Who decides", "| spend past the budget | the owner, always |"),
    ("Who decides", "| stop a run at an open hold (the owner's answer \"no\") | the owner, always |"),
    ("Who decides", "| ship: adopt the champion into main, push, publish | the owner, always |"),
    ("Who decides", "| start a real run under a runbook whose last real run the owner stopped "
                    "| the owner, always |"),
    ("Who decides", "| accept or reject an attempt, pick a round's winner | nobody; the engine decides "
                    "from the frozen contract |"),
    ("Step 0", "A hold on a run the goal does not name is left alone and named in the hand-over "
               "(step 8)."),
    ("Step 1", "If `level_source` is `unreadable`, report it and stop (exit 2 below)."),
    ("Step 3", "report it and stop, because a gate or the accept rule is broken"),
    ("Step 4", "On exit 3 go to step 6:"),
    ("Step 4", "never what belongs to the owner or the host (free disk, the budget, a loosened field, "
               "a run the owner stopped)"),
    ("Step 5", "at L4 to L6 run `bin/alpaca research resume <run> --json`; at L1 hand it over; at L2 "
               "and L3 confirm first. A run the owner stopped at a hold reads as `stopped`, never "
               "`resumable`, and `resume` refuses it."),
    ("Step 6", "If the state is `stopped`, the owner answered \"stop\": go to step 7, resume nothing "
               "and start nothing under that runbook"),
    ("Step 6", "Show the `decision` from `start`'s JSON, or the `holds` of `contracts`. When the owner "
               "says the grant is recorded, go back to step 4."),
    ("Step 6", "show the reason and the owner's reopen command (`contracts` shows `reopen_needed: "
               "true`). When the owner says the grant is recorded, go back to step 4."),
    ("Step 6", "Nothing is resumed or started after it, and the flow goes to step 8."),
    ("Dry run", "and stop before it. Nothing is started and no money is spent."),
    ("Never", "- Never start a run under a runbook whose earlier run the owner stopped at a hold, "
              "unless the owner asks for it. Never write a new runbook to get around a hold."),
    ("Never", "- Never touch an attempt worktree, the research branch or the reproducer store by hand."),
    ("Never", "- Never sign the interview off for the operator, and never record a target number "
              "nobody gave as an answer."),
    ("Never", "- Never continue past exit 3 on the same run."),
)
LEVEL_ROW_PINS = (
    "It asks before anything outside that scope: a contract the goal does not name",
    "starting the control run and the real run (spending starts there)",
    "Before any run starts it also stops when nobody signed the interview off, and asks the one "
    "contract question when no rule of step 2 decides.",
)


def _check_prose(text):
    """Step 5 to 8 and the L1/L2 rows carry the stop rules in prose that the exit-code table and
    the Never section only repeat. A rewrite that lets the skill merge, resume at once or run at
    L1 must fail here (check-contract-s1 m-c2)."""
    sec = _sections(text)

    def step(name):
        key = [n for n in sec if n.startswith(name + ":")]
        assert len(key) == 1, name
        return _flat(sec[key[0]])

    flat = _flat(text)
    s5 = step("Step 5")
    assert "`running`, `starting` or `stopping`" in s5 and "keep watching" in s5
    assert "Never stop a run on your own" in s5
    assert "Exit 3: step 6" in s5
    s6 = step("Step 6")
    assert ("Do not ask the question yourself, and do not try `start`, `extend` or `resume` "
            "until the owner says the grant is recorded") in s6
    assert "Nothing is resumed or started after it" in s6
    assert "Never write a done marker to end the turn" in s6
    s7 = step("Step 7")
    assert ("exits 3 until the owner moves it: show the card id and the owner's commands, "
            "and stop") in s7
    assert "merge the champion" not in s7 and "merges the champion" not in s7
    s8 = step("Step 8")
    assert "The skill never merges the research branch, never pushes and never publishes" in s8
    l1 = flat.split("| L1 Assist |", 1)[1].split(" |", 1)[0]
    assert "prints every other command for the owner to run and changes nothing" in l1
    l2 = flat.split("| L2 Partial |", 1)[1].split(" |", 1)[0]
    assert "one changing command at a time after the owner confirms it" in l2
    assert "the spec step" in l2 and "`alpaca intake`" in l2 and "`start`" in l2
    for where, phrase in SECTION_PINS:
        keys = [n for n in sec if n == where or n.startswith(where + ":")]
        assert len(keys) == 1, where
        assert phrase in _flat(sec[keys[0]]), (where, phrase)
    for phrase in LEVEL_ROW_PINS:
        assert phrase in flat, phrase


def test_the_prose_of_steps_5_to_8_and_the_level_rows_is_pinned():
    _check_prose(_text())


@pytest.mark.parametrize("good,bad", [
    ("`running`, `starting` or `stopping`", "`running` or `starting`"),
    ("Never stop a run on your own.", "Stop a run when it looks stuck."),
    ("and do not try `start`, `extend` or `resume` until the owner says the grant is recorded",
     "and try `resume` at once"),
    ("Nothing is resumed or started after it", "Resume once the card is open"),
    ("Never write a done marker", "Write a done marker"),
    ("exits 3 until the owner moves it: show the card id and the owner's commands, and stop",
     "exits 3 until the owner moves it, then merge the champion"),
    ("The skill never merges the research branch", "The skill merges the research branch"),
    ("changes nothing, not even an interview answer", "runs it"),
    ("the spec step, ", ""),
])
def test_the_prose_pins_fail_when_the_stop_wording_changes(good, bad):
    text = _text()
    flat_free = text.replace(good, bad)
    if flat_free == text:                       # the phrase wraps over a line break
        words = good.split()
        text = re.sub(r"\s+".join(re.escape(w) for w in words), bad, text)
        flat_free = text
    assert flat_free != _text(), good
    with pytest.raises(AssertionError):
        _check_prose(flat_free)


@pytest.mark.parametrize("where,phrase", SECTION_PINS + tuple(("", p) for p in LEVEL_ROW_PINS),
                         ids=lambda v: v[:30] if isinstance(v, str) else str(v))
def test_each_owner_decision_pin_fails_when_its_phrase_is_removed(where, phrase):
    text = _text()
    words = phrase.split()
    pat = r"\s+".join(re.escape(w) for w in words)
    broken, n = re.subn(pat, "REMOVED", text, count=1)
    assert n == 1, phrase
    with pytest.raises(AssertionError):
        _check_prose(broken)


def _fill(words):
    fill = {"<run>": "rr-001", "<M>": "3", "<X>": "5", "<T>": "1000", "<S>": "60",
            "<path>": "pack/rb.yaml", "<FIELD=VALUE>": "research.stop.rounds=3",
            "<card>": "rc-1", "<decision text>": "ship it"}
    return [fill.get(w, w) for w in words]


def _parse(words):
    parser, sub = cli.build_parser()
    for mod in cli.VERB_MODULES:
        __import__("alpaca.%s" % mod)
    for _name, reg in cli._EXTRA_PARSERS:
        reg(sub)
    return parser.parse_args(words)


def test_every_command_in_the_owner_section_parses_against_the_live_parser():
    body = _sections(_text())[OWNER_SECTION]
    commands = [line.strip() for line in body.splitlines() if line.strip().startswith("bin/alpaca")]
    assert len(commands) == 7          # six grant forms and the card move
    kinds = set()
    for line in commands:
        words = _fill(shlex.split(line))
        assert words[0] == "bin/alpaca"
        args = _parse(words[1:])
        kinds.add((args.verb, getattr(args, "research_verb", None) or
                   getattr(args, "review_verb", None)))
    assert kinds == {("research", "grant"), ("review", "move")}
    # the card move takes the card and the decision text positionally; the flag form does not parse
    assert _parse(["review", "move", "rc-1", "ship it", "--by", "owner"]).review_verb == "move"
    with pytest.raises(SystemExit):
        _parse(["review", "move", "rc-1", "--decision", "ship it", "--by", "owner"])
    assert "/autodrive L<n>" in body


def test_the_owner_section_holds_only_the_owners_commands():
    body = _sections(_text())[OWNER_SECTION]
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("bin/alpaca") or s.startswith("/autodrive"):
            assert ("research grant" in s or "review move" in s or s.startswith("/autodrive")), s
    assert "bin/alpaca research start" not in body and "bin/alpaca research resume" not in body


# ------------------------------------------------------------------ doctor's one WARN check
def _skill_project(tmp_path, *, body=None, digests="match"):
    root = tmp_path / "proj"
    skill = root / ".claude" / "skills" / "autoresearch"
    if body is not None:
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_bytes(body)
    if digests is not None:
        files = {}
        if digests == "match" and body is not None:
            files[doctor.AUTORESEARCH_SKILL] = util.sha256_hex(body)
        elif digests == "other":
            files[doctor.AUTORESEARCH_SKILL] = "0" * 64
        root.mkdir(exist_ok=True)
        (root / "MANIFEST.json").write_text(json.dumps({"files": files}), encoding="utf-8")
    root.mkdir(exist_ok=True)
    return str(root)


def _doctor_rows(root):
    rows = []
    doctor._autoresearch_skill(root, lambda name, level, detail="": rows.append(
        (name, level, detail)))
    return rows


def test_doctor_is_ok_when_the_skill_is_the_shipped_file(tmp_path):
    rows = _doctor_rows(_skill_project(tmp_path, body=b"shipped\n"))
    assert [(n, l) for n, l, _d in rows] == [("autoresearch-skill", "ok")]


def test_doctor_warns_when_the_skill_is_not_the_shipped_file(tmp_path):
    root = _skill_project(tmp_path, body=b"the project's own skill\n", digests="other")
    rows = _doctor_rows(root)
    assert [(n, l) for n, l, _d in rows] == [("autoresearch-skill", "warn")]
    assert "upgrade replaces it" in rows[0][2] and "another digest" in rows[0][2]
    # a file MANIFEST.json does not list is the same finding
    root = _skill_project(tmp_path / "b", body=b"own\n", digests="none")
    rows = _doctor_rows(root)
    assert rows[0][1] == "warn" and "does not list it" in rows[0][2]


def test_doctor_is_silent_with_no_skill_folder_or_no_digest_map(tmp_path):
    assert _doctor_rows(_skill_project(tmp_path, body=None, digests="none")) == []
    assert _doctor_rows(_skill_project(tmp_path / "x", body=b"own\n", digests=None)) == []


def test_doctor_checks_carries_the_row_and_the_repo_still_passes_doctor(tmp_path, capsys):
    from alpaca.tests.test_doctor import _full
    root = tmp_path / "proj"
    root.mkdir()
    _full(str(root))
    names = [c["name"] for c in doctor.checks(str(root))]
    assert "autoresearch-skill" not in names          # folder is empty: nothing to compare
    (root / ".claude" / "skills" / "autoresearch").mkdir(parents=True, exist_ok=True)
    (root / ".claude" / "skills" / "autoresearch" / "SKILL.md").write_bytes(b"own\n")
    (root / "MANIFEST.json").write_text(json.dumps({"files": {}}), encoding="utf-8")
    rows = {c["name"]: c for c in doctor.checks(str(root))}
    assert rows["autoresearch-skill"]["level"] == "warn"
    # the repo itself: warn is allowed, an error is not (the manifest line points at a real folder)
    p = subprocess.run([sys.executable, "-m", "alpaca", "doctor"], cwd=REPO, capture_output=True,
                       text=True, encoding="utf-8", env={**os.environ, "CLAUDE_PROJECT_DIR": REPO})
    assert p.returncode in (0, 1), p.stdout + p.stderr
    assert "ERROR" not in p.stdout


# ------------------------------------------------------------------ the dry run (SC-020)
@pytest.fixture
def rec(project, monkeypatch):
    """A tmp project that is a research project (kit.make_project), a record with op-001, the fake
    loop runner and no agent marker."""
    from alpaca.research import owner, runner
    cli.main(["init"])
    plain_op("intent", "bar")
    kit.make_project(project)
    fake = FakeRunner(project)
    monkeypatch.setattr(runner, "open_run", fake.open_run)
    monkeypatch.setattr(runner, "launch", fake.launch)
    for name in owner.AGENT_MARKERS:
        monkeypatch.delenv(name, raising=False)
    return argparse.Namespace(root=project, fake=fake)


class Driver:
    """What an agent following the skill does, written as code: the verbs in the order of the
    steps, and at each exit code the action the skill's table gives. `rules` is `_exit_rules` of the
    skill text, so the driver stops at a code exactly when the table says it does."""

    def __init__(self, capsys, rules, text=None):
        self.capsys, self.rules, self.trace, self.run = capsys, rules, [], None
        self.shown = None
        self.text = text if text is not None else _text()

    def call(self, *argv):
        code, body = _call(self.capsys, *argv)
        self.trace.append((argv, code))
        return code, body

    def stops(self, code):
        return self.rules[code][0]

    def go_on_anyway(self):
        """What a skill that continues at an exit 3 does next: it tries to resume the run."""
        return self.call("resume", self.run) if self.run else (None, None)

    def start_to_hold(self, world, *, rb=RB, stop_at_start=False):
        """Steps 0 to 5. `world(run)` plays the loop runner once after the start. Returns True when
        the flow reached a finished state (step 7), False when it stopped at an owner gate."""
        code, body = self.call("status")                           # step 0
        assert code == vc.PASS and body["level"] == "L6"           # step 1: read, never written
        code, body = self.call("contracts")                        # step 2
        picked = [c for c in body["contracts"] if c["path"] == rb]
        assert picked and picked[0]["check"] == "PASS"
        control = picked[0]["burn_in"]["state"] == "needed"        # step 3
        code, body = self.call(*_start_argv(self.text, control=control, rb=rb))
        if self.stops(code):
            self.shown = body
            return False
        self.run = body["run"]
        if stop_at_start:
            return True
        world(self.run)                                            # the runner works
        for _ in range(3):                                         # step 5: watch
            code, body = self.call("status", self.run)
            if self.stops(code):
                self.shown = body
                return False
            if body["run"]["state"] in ("finished", "stopped", "failed"):
                return True
        raise AssertionError("the run never reached a finished state or a hold")

    def report(self):
        code, body = self.call("report", self.run)                 # step 7
        if self.stops(code):
            self.shown = body
            return False
        return True


def _burn_in_done(monkeypatch):
    real = catalog._burn_in
    monkeypatch.setattr(catalog, "_burn_in", lambda *a, **k: {"state": "done", "rounds": 2})
    return real


def _level6(root):
    conn = _conn(root)
    try:
        level_mod.set(conn, SESSION, 6, "run %s through /autoresearch until it ends or an owner "
                      "gate holds it" % RB, actor="owner", root=root)
    finally:
        conn.close()


def _after(root, kinds, event_id):
    got = []
    for kind in kinds:
        got += [e["id"] for e in _ev(root, kind) if e["id"] > event_id]
    return got


def _no_level_decision_after(root, event_id):
    conn = _conn(root)
    try:
        rows = [e for e in db.events(conn, kind="decision", limit=10 ** 6) if e["id"] > event_id]
    finally:
        conn.close()
    return [e for e in rows if e["data"].get("kind") in ("level", "level_change")]


GO_ON = (events.LAUNCH, events.MERGE, events.RESUME)


def test_a_dry_run_on_a_contract_reaches_the_start_of_a_run(rec, capsys):
    """The done bar: from step 0 the driver reaches the first `research start`, the no-op
    control, which records via autoresearch, the level in force and no level change."""
    _level6(rec.root)
    d = Driver(capsys, _exit_rules(_text()))
    assert d.start_to_hold(lambda run: None, stop_at_start=True) is True
    assert [a[0] for a, _c in d.trace] == ["status", "contracts", "start"]
    assert all(code == vc.PASS for _a, code in d.trace)
    start = _ev(rec.root, events.RUN_START)
    assert len(start) == 1 and start[0]["data"]["via"] == "autoresearch"
    assert start[0]["data"]["control"] == "noop" and start[0]["data"]["level"] == "L6"
    assert rec.fake.starts[0]["control"] == "noop"
    # the skill reads the level and never sets it: the owner's decision came before the run and
    # no level decision follows the run's start
    assert _no_level_decision_after(rec.root, start[0]["id"]) == []
    assert len(_no_level_decision_after(rec.root, 0)) == 1


def test_a_budget_hold_after_round_one_stops_the_driver_at_the_first_exit_3(rec, capsys,
                                                                            monkeypatch):
    _burn_in_done(monkeypatch)
    _level6(rec.root)
    d = Driver(capsys, _exit_rules(_text()))
    assert d.start_to_hold(lambda run: _hold(rec.root, run, "budget")) is False
    assert d.trace[-1][1] == vc.PAUSED and d.trace[-1][0][0] == "status"
    hold_ev = [e for e in _ev(rec.root, events.HOLD) if e["data"]["gate"] == "budget"][0]
    # nothing launched, merged or resumed after the hold, and the driver tried nothing more
    assert _after(rec.root, GO_ON, hold_ev["id"]) == []
    assert _after(rec.root, (events.REFUSED,), hold_ev["id"]) == []
    assert rec.fake.launches == [(d.run, False)]
    # the hold shows the owner's own command in the terminal, and no agent runs it
    code, text = _call(capsys, "status", d.run, json_out=False)
    assert code == vc.PAUSED
    assert "owner, in your own terminal: bin/alpaca research grant %s budget" % d.run in text
    conn = _conn(rec.root)
    try:
        assert loops.stop_should_refuse(conn, SESSION, root=rec.root)[0] is False
    finally:
        conn.close()
    assert _no_level_decision_after(rec.root, _ev(rec.root, events.RUN_START)[0]["id"]) == []
    assert _ev(rec.root, events.GRANT) == []                     # the driver never grants


def _finish_with_champion(root, run):
    conn = _conn(root)
    try:
        from alpaca import ops
        sess = "research-%s" % run
        events.emit(conn, events.ROUND, run=run, ref=run, op="op-001", session=sess, code=vc.PASS,
                    data={"round": 0, "repair_task": None, "winner": None,
                          "champion_commit": "b" * 40,
                          "champion_metrics": {"value": {"median": 100}},
                          "accepted": 0, "targets_met": False})
        tid = ops.add_task(conn, "op-001", "an attempt", "attempt",
                           extra={"research": {"run": run, "role": "attempt",
                                               "key": "%s:r1:s0" % run, "round": 1, "slot": 0}})
        events.emit(conn, events.ATTEMPT, run=run, ref=tid, op="op-001", session=sess,
                    data={"outcome": "improvement", "metrics": {"value": {"median": 8}}})
        events.emit(conn, events.MERGE, run=run, ref=run, op="op-001", session=sess,
                    data={"round": 1, "repair_task": None, "attempt": tid, "commit": "c" * 40,
                          "parent": "b" * 40, "research_branch": "research/%s" % run})
        events.emit(conn, events.ROUND, run=run, ref=run, op="op-001", session=sess, code=vc.PASS,
                    data={"round": 1, "repair_task": None, "winner": tid,
                          "champion_commit": "c" * 40,
                          "champion_metrics": {"value": {"median": 8}},
                          "accepted": 1, "targets_met": True})
    finally:
        conn.close()
    _finalize(root, run)


def test_a_new_champion_raises_the_ship_card_and_the_driver_stops_at_the_report(rec, capsys,
                                                                               monkeypatch):
    _burn_in_done(monkeypatch)
    _level6(rec.root)
    d = Driver(capsys, _exit_rules(_text()))
    assert d.start_to_hold(lambda run: _finish_with_champion(rec.root, run)) is True
    assert d.report() is False                          # exit 3: the ship card is open
    assert d.trace[-1] == (("report", d.run), vc.PAUSED)
    card = [e for e in _ev(rec.root, "review-card")][-1]
    assert card["data"]["kind"] == "ship"
    assert _after(rec.root, GO_ON, card["id"]) == []
    assert _after(rec.root, (events.REFUSED,), card["id"]) == []
    assert rec.fake.launches == [(d.run, False)]
    conn = _conn(rec.root)
    try:
        assert loops.stop_should_refuse(conn, SESSION, root=rec.root)[0] is False
    finally:
        conn.close()
    # the skill never moves the card or merges: the card is still open
    from alpaca import review
    conn = _conn(rec.root)
    try:
        assert any(c["kind"] == "ship" for c in review.open_cards(conn))
    finally:
        conn.close()
    assert _no_level_decision_after(rec.root, _ev(rec.root, events.RUN_START)[0]["id"]) == []


def _second_runbook(root):
    """pack/rb2.yaml: the same contract under another id, tracked, so a second run can start while
    the first run's runbook is held or stopped."""
    text = open(os.path.join(root, RB), encoding="utf-8").read().replace("id: loop-improve",
                                                                          "id: loop-improve-2")
    kit.write(root, "pack/rb2.yaml", text)
    kit.git(root, "add", "-A")
    kit.git(root, "commit", "-q", "-m", "second runbook")
    return "pack/rb2.yaml"


def test_the_owners_no_ends_each_held_run_stopped_and_the_driver_never_resumes(rec, capsys,
                                                                              monkeypatch):
    # Reads only the exit-code table from the skill. What Step 6 says about a stopped run, a
    # contract-ref hold and a ship hold is pinned in SECTION_PINS (check-contract-s1 blocking 2).
    _burn_in_done(monkeypatch)
    _level6(rec.root)
    rb2 = _second_runbook(rec.root)
    # one run halted at an extension hold at N, another at a budget hold
    runs = {}
    for gate, rb in (("extension", RB), ("budget", rb2)):
        d = Driver(capsys, _exit_rules(_text()))
        assert d.start_to_hold(lambda run, gate=gate: _hold(rec.root, run, gate), rb=rb) is False
        assert d.trace[-1][1] == vc.PAUSED and d.stops(vc.PAUSED)     # exit 3: the driver stops
        runs[gate] = d.run
    launched = list(rec.fake.launches)
    assert len(launched) == 2
    # the owner types `grant RUN stop` at a terminal; the driver goes on from step 6
    for gate, run in runs.items():
        code, body = _grant(capsys, monkeypatch, run, run, "stop")
        assert code == vc.PASS, body
        grant = body["grant"]["event"]
        d = Driver(capsys, _exit_rules(_text()))
        d.run = run
        code, body = d.call("status", run)
        assert code == vc.PASS and body["run"]["state"] == "stopped", (gate, body)
        assert d.report() is True                                    # step 7: no ship card
        assert [a[0] for a, _c in d.trace] == ["status", "report"]   # never step 5's resume
        # the test's own resume is refused: the run is finished
        code, body = _call(capsys, "resume", run)
        assert code == vc.FAIL and body["code"] == "RESEARCH-RUN-FINISHED"
        assert _after(rec.root, (events.RESUME, events.LAUNCH), grant) == []
    assert rec.fake.launches == launched
    # a new real run under a stopped runbook is the owner's reopen decision, not the skill's
    code, body = _call(capsys, "start", "--runbook", RB, "--via", "autoresearch")
    assert code == vc.PAUSED and body["code"] == "RESEARCH-RUNBOOK-STOPPED"
    assert _no_level_decision_after(rec.root, _ev(rec.root, events.RUN_START)[0]["id"]) == []


def test_the_check_is_sensitive_the_driver_goes_past_a_hold_when_the_table_says_continue(
        rec, capsys, monkeypatch):
    """Control: with the exit 3 row edited to continue, the same driver tries to resume at the
    hold and is refused, which is what the tests above assert never happens."""
    _burn_in_done(monkeypatch)
    _level6(rec.root)
    text = _text()
    row = re.search(r"^\| 3 \| PAUSED-FOR-DECISION \| .*$", text, re.M).group(0)
    rules = _exit_rules(text.replace(row, "| 3 | PAUSED-FOR-DECISION | continues the flow |"))
    assert rules[3][0] is False and _exit_rules(text)[3][0] is True
    d = Driver(capsys, rules)
    code, body = d.call("start", "--runbook", RB, "--via", "autoresearch")
    assert code == vc.PASS
    d.run = body["run"]
    _hold(rec.root, d.run, "budget")                 # the runner halts the run at a budget hold
    hold = [e for e in _ev(rec.root, events.HOLD) if e["data"]["gate"] == "budget"][0]
    code, _body = d.call("status", d.run)
    assert code == vc.PAUSED and not d.stops(code)
    code, body = d.go_on_anyway()                    # what a continuing skill does next
    assert code == vc.PAUSED and body["code"] == "RESEARCH-OWNER-GRANT-MISSING"
    assert len(_after(rec.root, (events.REFUSED,), hold["id"])) == 1    # the record shows it
    # the engine still refused: nothing was resumed, whatever the skill did
    assert _after(rec.root, (events.RESUME,), hold["id"]) == []
