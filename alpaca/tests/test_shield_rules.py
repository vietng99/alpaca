"""op-019 t-201 (design section 2, brief P2): the lens rule. Every prompt shieldRounds sends carries
SHIELD_RULES, so no check lens, investigator, judge, confirmer or fixer cites an agent-proposed
contract line as owner authority. The rule's text is the same in round-loop.js, taskcontract.LENS_RULE
and shield SKILL.md."""
import os
import re

from alpaca import taskcontract
from alpaca.tests.conftest import REPO

SKILL = os.path.join(REPO, ".claude", "skills", "shield")
LOOP = open(os.path.join(SKILL, "round-loop.js"), encoding="utf-8").read()


def _rounds_body():
    start = LOOP.index("async function shieldRounds")
    return LOOP[start:]


def _agent_calls(body):
    """The argument text of every `agent(` call: from the paren to its matching close."""
    calls = []
    for m in re.finditer(r"(?<![\w.])agent\(", body):
        depth, i = 1, m.end()
        while depth and i < len(body):
            depth += {"(": 1, ")": -1}.get(body[i], 0)
            i += 1
        calls.append(body[m.end(): i - 1])
    return calls


def test_every_agent_call_in_shield_rounds_builds_its_prompt_with_shield_rules():
    calls = _agent_calls(_rounds_body())
    # mechanical, lens, confirm, investigate, judge, class reviewer, fix
    assert len(calls) == 7
    for call in calls:
        first_argument = call.split(", {")[0]
        assert "SHIELD_RULES" in first_argument, call


def test_the_think_stage_prompts_carry_the_rule_too():
    start = LOOP.index("async function shieldThink")
    calls = _agent_calls(LOOP[start: LOOP.index("function shieldThinkSize")])
    assert len(calls) == 1
    assert "SHIELD_RULES" in calls[0].split(", {")[0]


def test_the_rule_text_is_the_same_everywhere():
    m = re.search(r"const SHIELD_RULES = ((?:'[^']*'\s*\+?\s*)+)", LOOP)
    assert m, "round-loop.js defines SHIELD_RULES"
    text = "".join(re.findall(r"'([^']*)'", m.group(1)))
    assert text == taskcontract.LENS_RULE
    skill = open(os.path.join(SKILL, "SKILL.md"), encoding="utf-8").read()
    assert "never owner authority" in skill and "SHIELD_RULES" in skill


def test_the_rule_names_the_four_things_a_checker_may_cite():
    for needle in ("agent-proposed", "signed brief", "owner-quote", "owner-proved decision",
                   "batch answer"):
        assert needle in taskcontract.LENS_RULE
