"""The owner's words on the setup card are never what makes it fail (op-062 t-687, blind confirm round 1: B1, B2).

The class: every span of the card that holds the owner's words (the goal, the limits, a "You said" quote drawn from
the opening or from an answer) is marked as owner text when the card is rendered, and every lint check runs on the card
with all owner spans removed. A refusal can only name text the agent wrote. The owner writes Vietnamese, so accents,
source numbers, card ids and Alpaca words in their message are ordinary input."""
import os
import re

import pytest

from alpaca import onboard_card as oc
from alpaca.gates import literal_guard as lg
from alpaca.tests import onboardkit as kit
from alpaca.tests.attachkit import upstream  # noqa: F401  (a fixture)
from alpaca.tests.onboardchat import chat  # noqa: F401  (a fixture)

GOAL = ("I want to get it into a clean state first, the build has some weird stuff and the readme is out of date. "
        "later I want to add new features for the beam control")


def _fx(name="qtrm64-take-over"):
    return kit.BUILDERS[name]()


def _with_goal(fx, goal):
    fx["answers"]["goal"] = dict(fx["answers"]["goal"], value=goal)
    return fx


def _problems(fx):
    return oc.lint(kit.render(fx), fx["look"], fx["answers"])


def _flat(text):
    return " ".join(text.split())


def _sources(card):
    return sorted(int(n) for n in re.findall(r"^\[(\d+)\] ", card.partition("\nSources (")[2], re.M))


# ---- B1: the goal is the owner's, whatever it holds

def test_a_goal_with_a_source_number_in_brackets_renders_and_passes_lint():
    fx = _with_goal(_fx(), "I want to add a hello feature, the one in issue [42]")
    card = kit.render(fx)
    assert _problems(fx) == []
    assert '"I want to add a hello feature, the one in issue [42]"' in _flat(card)
    assert "[42]" not in card.partition("\nSources (")[2]


def test_a_goal_with_a_card_id_renders_and_passes_lint_and_keeps_the_id_word_for_word():
    fx = _with_goal(_fx(), GOAL + ", the same as what we said on card-7ec2c737 and on card-00000000")
    card = kit.render(fx)
    assert _problems(fx) == []
    assert "card-7ec2c737 and on card-00000000" in _flat(card), "the card's own id must not rewrite the owner's words"
    own_id = oc.card_id(card)
    assert card.splitlines()[0].startswith("SETUP PROPOSAL %s for " % own_id)
    assert "(The long form works too: %s yes)" % own_id in card


def test_two_goals_that_differ_only_in_a_card_id_they_quote_get_two_card_ids():
    a = kit.render(_with_goal(_fx(), GOAL + ", see card-aaaaaaaa"))
    b = kit.render(_with_goal(_fx(), GOAL + ", see card-bbbbbbbb"))
    assert oc.card_id(a) != oc.card_id(b)


@pytest.mark.parametrize("word", ["gate", "studio", "baseline", "op", "task", "level"])
def test_a_goal_that_repeats_a_gloss_sentence_renders_and_passes_lint(word):
    fx = _with_goal(_fx(), GOAL + ". Please explain it: %s %s (%s)" % (word, oc.GLOSSARY[word], oc.GLOSSARY[word]))
    card = kit.render(fx)
    assert _problems(fx) == []
    assert _flat(oc.GLOSSARY[word]) in _flat(card).partition("2. WHAT I FOUND")[0], "the owner's sentence is kept"


@pytest.mark.parametrize("name,goal", [
    ("qtrm64-take-over", "fix the build, see issue [99] and [100], and card-7ec2c737"),
    ("team-repo", "add CSV export, see [3] and [12]"),
    ("audit", "read the code and tell me about [5]"),
    ("start-empty", "make a tool for my notes, like [1] but smaller"),
])
def test_the_owners_goal_never_changes_which_sources_the_card_lists(name, goal):
    base = _fx(name)
    other = _with_goal(_fx(name), goal)
    assert _problems(other) == []
    assert _sources(kit.render(other)) == _sources(kit.render(base)), "a [n] the owner typed is not a citation"


def test_a_one_in_the_goal_adds_no_sources_entry():
    for name in ("start-empty", "audit", "team-repo", "qtrm64-take-over"):
        base = kit.render(_fx(name))
        missing = [k for k in range(1, 7) if k not in _sources(base)]
        if not missing:
            continue
        fx = _with_goal(_fx(name), "do what [%d] says" % missing[0])
        card = kit.render(fx)
        assert _sources(card) == _sources(base) and missing[0] not in _sources(card)
        assert _problems(fx) == []
        return
    pytest.fail("no fixture leaves a source in 1 to 6 uncited")


def test_the_limits_get_the_same_treatment_as_the_goal():
    fx = _fx()
    fx["answers"]["limits"] = dict(fx["answers"]["limits"], value="dont touch [7], card-7ec2c737 or \"%s\" itself" % oc.GLOSSARY["gate"])
    assert _problems(fx) == []
    assert "dont touch [7], card-7ec2c737" in _flat(kit.render(fx))


def test_the_owners_words_are_never_changed_by_the_card():
    goal = GOAL + ", keep [[gate]] and [[op]] and card-00000000 and  two  spaces"
    fx = _with_goal(_fx(), goal)
    card = _flat(kit.render(fx))
    assert '"%s"' % _flat(goal) in card
    assert _problems(fx) == []


def test_a_refusal_can_only_name_agent_text():
    fx = _with_goal(_fx(), "see [42] and card-7ec2c737")
    card = kit.render(fx)
    broken = card.replace("Nothing is set up", "Nothing is set up [77] \u00e9")
    problems = oc.lint(broken, fx["look"], fx["answers"])
    assert any("[77]" in p for p in problems) and any("non-ASCII" in p for p in problems)
    assert not any("42" in p or "7ec2c737" in p for p in problems)


def test_the_text_the_lint_reads_holds_no_owner_word_and_passes_the_ban_list():
    goal = GOAL + ", and a bespoke build that is arguably more cohesive, [42], card-7ec2c737"
    fx = _with_goal(_fx(), goal)
    card = kit.render(fx)
    agent = oc.agent_text(card, fx["look"], fx["answers"])
    assert "bespoke" not in agent and "[42]" not in agent and "card-7ec2c737" not in agent
    assert "I want to get it into a clean state" not in agent
    tiers = {"words": [], "phrases": [], "placeholders": [], "structures": []}
    parsed = lg.parse_ban_list(lg.shipped_preset_path("plain-writing"))
    for tier in lg.STYLE_TIERS:
        tiers[tier].extend(parsed[tier])
    assert lg.lint(agent, tiers) == []


# ---- B2: the owner's words quoted outside the goal block

OPENING_ACCENT = ("hi, t\u00f4i took over this firmware project from my old team https://github.com/example-org/QTRM64 . "
                  "not sure where to start with alpaca")


def test_an_opening_with_accents_that_the_situation_line_quotes_does_not_block_the_card():
    fx = _fx()
    fx["answers"]["opening"] = dict(fx["answers"]["opening"], value=OPENING_ACCENT, quote=OPENING_ACCENT)
    card = kit.render(fx)
    assert 'You said "t\u00f4i took over this firmware project from my old team"' in _flat(card) or \
        'you said "t\u00f4i took over this firmware project from my old team"' in _flat(card)
    assert not card.isascii()
    assert _problems(fx) == []
    assert any("non-ASCII" in p for p in oc.lint(card.replace("Nothing is set up", "Nothing is set up \u00e9"), fx["look"], fx["answers"]))


def test_an_others_answer_with_accents_that_the_card_quotes_does_not_block_the_card():
    fx = _fx()
    reply = "1a, b\u1ea1ch ch\u1ec9 th\u1eed uart th\u00f4i. " + GOAL
    fx["answers"]["others-commit"] = dict(fx["answers"]["others-commit"], quote=reply)
    card = kit.render(fx)
    assert "b\u1ea1ch ch\u1ec9 th\u1eed uart th\u00f4i" in card
    assert _problems(fx) == []


def test_a_situation_the_owner_overrode_in_their_own_words_does_not_block_the_card():
    fx = _fx()
    fx["answers"]["situation"] = {"value": None, "option": "team", "source": "owner", "event": 11,
                                  "quote": "kh\u00f4ng, \u0111\u00e2y l\u00e0 repo c\u1ee7a nh\u00f3m"}
    card = kit.render(fx)
    assert "kh\u00f4ng, \u0111\u00e2y l\u00e0 repo c\u1ee7a nh\u00f3m" in card
    assert _problems(fx) == []


def test_an_unknown_the_owner_listed_with_accents_does_not_block_the_card():
    fx = _fx()
    fx["answers"]["unknown"] = [dict(kit.own("kh\u00f4ng bi\u1ebft CI ch\u1ea1y \u1edf \u0111\u00e2u", quote="kh\u00f4ng bi\u1ebft CI ch\u1ea1y \u1edf \u0111\u00e2u", ev=12))]
    card = kit.render(fx)
    assert "kh\u00f4ng bi\u1ebft CI ch\u1ea1y \u1edf \u0111\u00e2u" in card
    assert _problems(fx) == []


def test_accents_in_the_agents_own_text_still_fail_the_lint():
    fx = _fx()
    fx["answers"]["finding"] = [kit.agent("The build is \u00e9crit twice", ev=8, pointer="repo:CMakeLists.txt:101-112")]
    assert any("non-ASCII" in p for p in _problems(fx))
    fx = _fx()
    fx["answers"]["done-guess"] = dict(fx["answers"]["done-guess"], value="it builds \u2014 done")
    assert any("non-ASCII" in p for p in _problems(fx))


# ---- nested quotes are not doubled

@pytest.mark.parametrize("opening", [
    '"I took over" this firmware project from my old team https://github.com/example-org/QTRM64 . not sure',
    'hi, I took over this firmware project and "my old team" left https://github.com/example-org/QTRM64 . not sure',
    'I said "I took over this firmware project from my old team" https://github.com/example-org/QTRM64 . not sure',
])
def test_a_quote_inside_the_owners_opening_is_not_doubled_on_the_situation_line(opening):
    fx = _fx()
    fx["answers"]["opening"] = dict(fx["answers"]["opening"], value=opening, quote=opening)
    card = kit.render(fx)
    assert '""' not in card and "''" not in card
    assert _problems(fx) == []
    situation = _flat(card).partition("- Situation:")[2].partition("- Others")[0]
    assert "took over" in situation


@pytest.mark.parametrize("goal,shown", [
    ('"I want to add a hello feature"', 'in your words: "I want to add a hello feature"'),
    ('fix the "build" step', 'in your words: "fix the "build" step"'),
    ('"fix" the build and "test" it', "in your words: '\"fix\" the build and \"test\" it'"),
])
def test_a_quote_inside_the_owners_goal_is_not_doubled(goal, shown):
    fx = _with_goal(_fx(), goal)
    card = _flat(kit.render(fx))
    assert shown in card and '""' not in card.replace('"fix the "build" step"', "")
    assert _problems(fx) == []


def test_a_quote_inside_the_others_answer_is_not_doubled():
    fx = _fx()
    reply = '1a, "bach" was only testing uart stuff. ' + GOAL
    fx["answers"]["others-commit"] = dict(fx["answers"]["others-commit"], quote=reply)
    card = _flat(kit.render(fx))
    assert '""' not in card and "bach" in card
    assert _problems(fx) == []


# ---- the goldens are untouched and the marks never reach the card

@pytest.mark.parametrize("name", sorted(kit.BUILDERS))
def test_a_card_never_holds_a_mark_and_its_marked_form_strips_to_it(name):
    fx = _fx(name)
    args = dict(date=fx["date"], name=fx["name"], people=fx["people"])
    card = oc.render(fx["look"], fx["answers"], **args)
    marked = oc.render_marked(fx["look"], fx["answers"], **args)
    assert "\x02" not in card and "\x03" not in card
    assert oc.strip_marks(marked) == card and marked != card
    assert oc.lint(marked, fx["look"], fx["answers"]) == oc.lint(card, fx["look"], fx["answers"]) == []


def test_the_owner_spans_are_the_goal_the_limits_and_each_quote_the_card_drew_from_the_owner():
    fx = _fx()
    spans = oc.owner_spans(fx["look"], fx["answers"])
    assert GOAL in spans and "dont touch the GUI for now" in spans
    assert "I took over this firmware project from my old team" in spans
    assert "bach was only testing uart stuff" in spans
    assert not any("objcopy" in s or "CMake" in s for s in spans), "what the agent wrote is not owner text"


# ---- the real verbs

def test_propose_prints_the_card_for_a_goal_with_a_source_number_and_an_accented_opening(chat, upstream):      # noqa: F811
    opening = "t\u00f4i took over this project from my old team, it is at %s . not sure where to start" % upstream.url
    chat.look(upstream.url, opening=opening)
    goal = "I want to add a hello feature, the one in issue [42], and card-7ec2c737 says the same"
    reply = chat.say("1a. %s. %s" % (goal, chat.LIMIT))
    for args in (["others-commit", "--option", "a"], ["goal", "--value", goal], ["limits", "--value", chat.LIMIT]):
        rc, out = chat.run("onboard", "answer", *args, "--quote", reply)
        assert rc == 0, out
    rc, out = chat.run("onboard", "answer", "done-guess", "--value", "hello works", "--source", "agent")
    assert rc == 0, out
    chat.answer_round_two()
    cid, card = chat.propose()
    assert ('"%s"' % goal) in _flat(card)
    assert "t\u00f4i took over this project from my old team" in card
    assert "ONBOARD-CARD-LINT" not in card and "\x02" not in card and "\x03" not in card
    with open(os.path.join(chat.root, ".alpaca", "onboard", "cards", cid + ".md"), encoding="utf-8") as fh:
        stored = fh.read()
    assert stored.rstrip("\n") == card
    assert "\x02" not in stored and "\x03" not in stored


def test_a_name_the_owner_gave_for_the_people_line_may_hold_accents():
    name = "Nguy\u1ec5n Qu\u1ed1c Vi\u1ec7t"
    fx = _fx()
    fx["people"] = [{"name": name, "role": "owner"}]
    card = kit.render(fx)
    assert "people: %s as owner" % name in _flat(card)
    assert oc.lint(card, fx["look"], fx["answers"], people=fx["people"]) == []
    marked = oc.render_marked(fx["look"], fx["answers"], date=fx["date"], name=fx["name"], people=fx["people"])
    assert oc.lint(marked, fx["look"], fx["answers"]) == []
