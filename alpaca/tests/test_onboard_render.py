"""The setup card (op-062 t-656, AC-19 reads back the goal and proposes one card): same inputs, same bytes;
every part present; every fact points at a source; every Alpaca word explained once; plain and short."""
import copy
import json
import os
import re

import pytest

from alpaca import onboard_card as oc
from alpaca.gates import literal_guard as lg
from alpaca.tests import onboardkit as kit

NAMES = sorted(kit.BUILDERS)


def _fx(name):
    return kit.BUILDERS[name]()


def _card(fx):
    return kit.render(fx)


def _flat(text):
    return " ".join(text.split())


@pytest.mark.parametrize("name", NAMES)
def test_the_card_is_byte_equal_to_its_golden_and_the_fixture_file_is_the_kit_output(name):
    fx = _fx(name)
    with open(os.path.join(kit.FIXTURES, name + ".card.txt"), encoding="ascii") as fh:
        assert _card(fx) == fh.read(), "run python3 -m alpaca.tests.onboardkit --write and read the diff"
    with open(os.path.join(kit.FIXTURES, name + ".json"), encoding="ascii") as fh:
        assert json.load(fh) == json.loads(json.dumps(fx))


@pytest.mark.parametrize("name", NAMES)
def test_same_inputs_give_the_same_bytes_and_id_and_the_id_is_in_the_title_and_the_long_form(name):
    fx = _fx(name)
    a, b = _card(fx), _card(copy.deepcopy(fx))
    assert a == b
    cid = oc.card_id(a)
    assert re.fullmatch(r"card-[0-9a-f]{8}", cid)
    assert a.splitlines()[0].startswith("SETUP PROPOSAL %s for " % cid)
    assert "(The long form works too: %s yes)" % cid in a
    assert len(set(re.findall(r"card-[0-9a-f]{8}", a))) == 1


@pytest.mark.parametrize("name", NAMES)
def test_one_changed_answer_gives_a_new_id(name):
    fx = _fx(name)
    other = copy.deepcopy(fx)
    other["answers"]["goal"]["value"] = other["answers"]["goal"]["value"] + " and make it fast"
    other["answers"]["goal"]["quote"] = other["answers"]["goal"]["quote"] + " and make it fast"
    assert oc.card_id(_card(fx)) != oc.card_id(_card(other))


@pytest.mark.parametrize("name", NAMES)
def test_the_goal_line_holds_the_owners_words_byte_for_byte(name):
    fx = _fx(name)
    goal = fx["answers"]["goal"]["value"]
    card = _card(fx)
    assert _flat('"%s"' % goal) in _flat(card)
    limits = fx["answers"].get("limits")
    if limits and limits["value"] not in ("none",):
        assert _flat('"%s"' % limits["value"]) in _flat(card)


@pytest.mark.parametrize("name", NAMES)
def test_all_ten_parts_are_present_in_order_for_every_situation(name):
    card = _card(_fx(name))
    last = -1
    for heading in oc.HEADINGS:
        at = card.find("\n" + heading)
        assert at > last, heading
        last = at
    # the parts the owner decides on, in words
    assert "WHERE ALPACA SITS: " in card and " mode" in card
    assert re.search(r"about \d+ to \d+ minutes|about \d+ minutes", card)           # first steps carry an estimate
    assert "I never raise it myself" in _flat(card)                                        # the level
    assert "WHAT STAYS WITH YOU" in card and "UNDO" in card
    assert "SETTINGS I WILL WRITE" in card and "plain-writing check" in _flat(card)
    assert card.rstrip().endswith("yes)")


@pytest.mark.parametrize("name", NAMES)
def test_lint_is_clean_ascii_within_the_line_cap_the_agent_wrote_96_wide(name):
    fx = _fx(name)
    card = _card(fx)
    assert oc.lint(card, fx["look"], fx["answers"]) == []
    card.encode("ascii")
    lines = card.rstrip("\n").split("\n")
    assert len(lines) - len(oc.owner_block_lines(lines)) <= oc.MAX_LINES and max(len(x) for x in lines) <= 96


@pytest.mark.parametrize("name", NAMES)
def test_every_citation_has_a_source_and_every_source_is_cited(name):
    card = _card(_fx(name))
    body, _, sources = card.partition("\nSources (")
    cited = set(re.findall(r"\[(\d+)\]", body))
    listed = set(re.findall(r"^\[(\d+)\] ", sources, re.M))
    assert cited == listed
    if cited:
        assert sources.startswith("repo paths at commit ")


@pytest.mark.parametrize("name", NAMES)
def test_each_alpaca_word_is_explained_once_at_first_use_and_only_if_used(name):
    card = _card(_fx(name))
    flat = _flat(card)
    for word, gloss in oc.GLOSSARY.items():
        seen = re.search(r"(?<![\w])%s(?![\w])" % word, flat, re.I)
        count = flat.count(gloss)
        if seen is None:
            assert count == 0, word
        else:
            assert count == 1, (word, count)
            first = flat[seen.start():]
            assert first.lower().startswith(("%s (%s" % (word, gloss)).lower()), (word, first[:60])


@pytest.mark.parametrize("name", NAMES)
def test_the_card_passes_the_plain_writing_ban_list(name):
    tiers = {"words": [], "phrases": [], "placeholders": [], "structures": []}
    parsed = lg.parse_ban_list(lg.shipped_preset_path("plain-writing"))
    for tier in lg.STYLE_TIERS:
        tiers[tier].extend(parsed[tier])
    assert lg.lint(_card(_fx(name)), tiers) == []


def test_the_qtrm64_card_says_what_the_design_card_says():
    card = _card(_fx("qtrm64-take-over"))
    for line in ('Situation: take over. You said "I took over this firmware project from my old team"',
                 "The hex and bin step is written twice, so every build runs objcopy twice (my reading of the code) [10]",
                 "- py-syntax: the 6 Python files parse. Tried it: pass, 0.05 s [9]",
                 "arm-syntax (suggested by me)", "Not a gate here: the real build. It needs arm-none-eabi-gcc",
                 "7. HOW MUCH I DO ALONE: L2", "Your repo gets no Alpaca file.",
                 "Say \"raise the level\" when you trust the work; I never raise it myself."):
        assert _flat(line) in _flat(card), line
    assert "Others who push: nobody now. You said \"bach was only testing uart stuff\"." in _flat(card)


def test_a_line_the_agent_wrote_is_marked_and_a_line_the_owner_wrote_is_quoted():
    card = _flat(_card(_fx("qtrm64-take-over")))
    assert "Done when (my first guess;" in card
    assert "(my reading of the code)" in card and "(suggested by me)" in card
    assert 'Your goal, in your words: "' in card


def test_an_owner_answer_that_was_only_read_from_the_reply_is_marked():
    fx = _fx("qtrm64-take-over")
    fx["answers"]["others-commit"] = {"value": None, "option": "a", "source": "agent", "quote": None, "event": 4,
                                      "reason": "my reading of: bach was only testing"}
    assert "(my reading of your reply)" in _card(fx)


def test_a_card_without_a_look_has_no_sources_and_says_nothing_was_read():
    fx = _fx("start-empty")
    card = _card(fx)
    assert "Sources" not in card and "[1]" not in card
    assert "No existing code" in card and "mode native" in card


def test_unknown_things_are_listed_as_unknown_and_never_as_facts():
    fx = _fx("qtrm64-take-over")
    fx["answers"]["ci"] = dict(fx["answers"]["ci"], option="c", quote="1c")            # the owner does not know
    card = _flat(_card(fx))
    assert "Still unknown: whether a CI runs outside the repo (none in it [6]); which arm-none-eabi-gcc version" in card
    assert "Where checks run" not in card


def test_the_ci_answer_replaces_the_unknown_line_and_a_ci_file_in_the_repo_is_stated():
    card = _flat(_card(_fx("qtrm64-take-over")))
    assert "Where checks run: only on your machine, no CI. You said \"there is no CI anywhere" in card
    assert "whether a CI runs outside the repo" not in card
    card = _flat(_card(_fx("qtrm64-others")))
    assert "Where checks run: on a CI service I cannot see from the repo's files." in card
    assert "whether a CI runs outside the repo" not in card
    card = _flat(_card(_fx("team-repo")))
    assert "Where checks run: on a CI; the repo holds its files (.github/workflows/ci.yml) [6]." in card


def test_estimates_are_ranges_with_a_named_basis():
    fx = _fx("qtrm64-take-over")
    est = oc.estimates(fx["look"], oc.settings(fx["look"], fx["answers"])["gates"], "take-over")
    assert est["study"] == "10 to 20 minutes" and re.fullmatch(r"\d+ to \d+ minutes", est["baseline"])
    for own, band in ((100, "10 to 20 minutes"), (101, "20 to 45 minutes"), (1000, "20 to 45 minutes"),
                      (1001, "45 to 90 minutes"), (5000, "45 to 90 minutes"), (5001, "90 minutes or more, in parts")):
        look = {"tracked": {"own": own}}
        assert oc.estimates(look, [], "team")["study"] == band
    assert oc.estimates(None, [], "audit")["questions"] == 15 and oc.estimates(None, [], "start-empty")["questions"] == 10
    # 3 untried gates, 60 s each, plus nothing: 3 minutes, written as 3 to 4
    gates = [{"timeout": 600, "trial": None}] * 3
    assert oc.estimates(None, gates, "team")["baseline"] == "3 to 4 minutes"


def test_the_change_flow_and_what_stays_with_the_owner_follow_the_situation():
    take = _flat(_card(_fx("qtrm64-take-over")))
    team = _flat(_card(_fx("team-repo")))
    audit = _flat(_card(_fx("audit")))
    empty = _flat(_card(_fx("start-empty")))
    assert "and you merge it there" in take and "Your team reviews and merges as usual" in team
    assert "Sending changes is switched off" in audit and "I send nothing to GitHub" in audit
    assert "Nothing leaves your machine unless you push it yourself" in empty
    assert "branch feature/t-001" not in team and "named feature/ and the task number (for example feature/t-001)" in team
    assert "Merging (your team's call)" in team and "Publishing anywhere" in empty
    assert "never changed" in audit and "delete this folder" in empty


def test_a_repo_that_is_not_on_github_says_it_cannot_send_the_branch():
    fx = _fx("qtrm64-take-over")
    fx["look"]["repository"] = None
    assert "This repo is not on GitHub, so I cannot send the branch for you" in _flat(_card(fx))


def test_a_level_override_wins_up_to_l4_and_l5_is_answered_with_a_line():
    fx = _fx("qtrm64-take-over")
    fx["answers"]["level-override"] = {"value": "L3", "source": "owner", "quote": "use L3", "event": 11}
    card = _flat(_card(fx))
    assert "7. HOW MUCH I DO ALONE: L3" in card and "Why L3: you asked for it" in card
    fx["answers"]["level-override"]["value"] = "L5"
    card = _flat(_card(fx))
    assert "7. HOW MUCH I DO ALONE: L2" in card and "L5 and L6 are set later, with the level command" in card


def test_the_plain_writing_preset_can_be_turned_off_on_the_card():
    fx = _fx("qtrm64-take-over")
    fx["answers"]["preset"] = {"value": None, "option": "b", "source": "owner", "quote": "no style check", "event": 12}
    assert "plain-writing check off" in _flat(_card(fx))


def test_lint_names_each_kind_of_broken_card():
    fx = _fx("qtrm64-take-over")
    good = _card(fx)
    problems = lambda text: oc.lint(text, fx["look"], fx["answers"])           # noqa: E731
    assert any("lines" in p for p in problems(good + "x\n" * 12))
    assert any("characters" in p for p in problems(good.replace("2. WHAT I FOUND", "2. WHAT I FOUND " + "x" * 100)))
    assert any("non-ASCII" in p for p in problems(good.replace("Nothing is set up", "Nothing is set up \u201c")))
    assert any("missing part" in p for p in problems(good.replace("9. UNDO", "9. BACK")))
    assert any("has no Sources entry" in p for p in problems(re.sub(r"\n\[7\] [^\n]*", "", good)))
    assert any("never cited" in p for p in problems(good.replace("[11]", "[x]", 1)))
    assert any("gloss of studio appears 2 times" in p for p in problems(
        good.replace("the studio's own git", "the studio (the folder you made with alpaca new)'s own git")))
    assert any("without its gloss" in p for p in problems(good.replace("the studio (the folder you made with alpaca new)", "the studio")))


def test_the_shown_check_ignores_fences_quote_marks_and_white_space():
    card = _card(_fx("start-empty"))
    shown = "```text\n" + "\n".join("> " + line for line in card.split("\n")) + "\n```"
    assert oc.normalize_shown(card) in oc.normalize_shown(shown)
    assert oc.normalize_shown(card) not in oc.normalize_shown(card.replace("Nothing is set up", "Everything is set up"))


def test_option_tokens_in_a_reply():
    assert oc.option_token_present("1a, bach was only testing", 1, "a")
    assert oc.option_token_present("1) a and then my goal", 1, "a")
    assert oc.option_token_present("2.b", 2, "b")
    assert not oc.option_token_present("I like option a", 1, "a")
    assert not oc.option_token_present("11a", 1, "a")
    assert oc.option_token_present("it is a team repo with reviews, yes", 1, "c", "Yes, it is still a team repo with reviews") is False
    assert oc.option_token_present("yes, it is still a team repo with reviews", 1, "c", "Yes, it is still a team repo with reviews")


# ---- t-687 (F6-4): a quote on the card is never cut inside

def test_the_situation_line_never_quotes_half_a_bracket_or_half_a_sentence():
    fx = _fx("qtrm64-take-over")
    fx["answers"]["opening"] = dict(fx["answers"]["opening"], value=(
        "hi, I took over this firmware project from my old team (it is at https://github.com/example-org/QTRM64). not sure"))
    card = _flat(_card(fx))
    assert 'Situation: take over. You said "I took over this firmware project from my old team"; main has not' in card
    assert "(it is at" not in card and "..." not in card.split("2. WHAT I FOUND")[0]
    assert oc.lint(_card(fx), fx["look"], fx["answers"]) == []


# ---- t-687 (F6-1): a goal of any length

QTRM_GOAL = ("I want to get it into a clean state first, the build has some weird stuff and the readme is out of date. "
             "later I want to add new features for the beam control")


def _long_goal(fx, extra):
    goal = QTRM_GOAL + extra
    fx["answers"]["goal"] = dict(fx["answers"]["goal"], value=goal)
    return goal


@pytest.mark.parametrize("clauses", [1, 3, 12, 60])
def test_a_goal_of_any_length_renders_a_card_that_passes_lint_with_every_word_kept(clauses):
    fx = _fx("qtrm64-take-over")
    goal = _long_goal(fx, "".join(", and make the uart tool number %d work on windows too" % i for i in range(clauses)))
    card = _card(fx)
    assert oc.lint(card, fx["look"], fx["answers"]) == [], "the owner's goal must never be what fails the lint"
    assert _flat('"%s"' % goal) in _flat(card)
    assert "..." not in card.split("2. WHAT I FOUND")[0]
    assert max(len(x) for x in card.split("\n")) <= 96
    card.encode("ascii")


def test_the_cap_counts_the_lines_the_agent_wrote_and_not_the_owners_goal_and_limits():
    fx = _fx("qtrm64-take-over")
    short = _card(fx)
    fx["answers"]["limits"] = dict(fx["answers"]["limits"], value="dont touch the GUI for now" + ", and the build" * 40)
    _long_goal(fx, ", and the same goal again" * 50)
    card = _card(fx)
    lines = card.rstrip("\n").split("\n")
    assert len(lines) > oc.MAX_LINES and oc.lint(card, fx["look"], fx["answers"]) == []
    # the lines outside the goal and limits are the lines of the short card: nothing the agent wrote was dropped
    short_lines = short.rstrip("\n").split("\n")
    assert len(lines) - len(oc.owner_block_lines(lines)) == len(short_lines) - len(oc.owner_block_lines(short_lines))
    # a card whose own lines pass the cap is still refused
    assert any("outside your goal and limits" in p for p in oc.lint(card + "x\n" * 12, fx["look"], fx["answers"]))


def test_a_goal_with_a_word_longer_than_a_line_is_not_cut_inside_the_word():
    fx = _fx("qtrm64-take-over")
    link = "https://example.org/" + "a" * 130
    goal = _long_goal(fx, ", the notes are at " + link)
    card = _card(fx)
    assert oc.lint(card, fx["look"], fx["answers"]) == []
    assert link in card and _flat('"%s"' % goal) in _flat(card)




def test_a_word_the_owner_typed_needs_no_gloss_on_the_card():
    fx = _fx("start-empty")
    _long_goal(fx, ", and I want a gate for the op amp, a level shifter and a task list")
    assert oc.lint(_card(fx), fx["look"], fx["answers"]) == []


def test_a_goal_in_the_owners_language_with_accents_passes_lint_when_it_wraps_over_lines():
    fx = _fx("qtrm64-take-over")
    goal = _long_goal(fx, ". T\u00f4i mu\u1ed1n d\u1ecdn m\u00e3 ngu\u1ed3n cho g\u1ecdn, t\u00e0i li\u1ec7u \u0111\u00e3 c\u0169, sau \u0111\u00f3 th\u00eam t\u00ednh n\u0103ng")
    card = _card(fx)
    assert not card.isascii() and _flat('"%s"' % goal) in _flat(card)
    assert oc.lint(card, fx["look"], fx["answers"]) == []
    assert any("non-ASCII" in p for p in oc.lint(card.replace("Nothing is set up", "Nothing is set up \u00e9"), fx["look"], fx["answers"]))


# ---- t-687 (F6-5): the host in parts 6, 8 and 9

def test_a_card_for_a_repo_off_github_follows_the_host_in_parts_6_8_and_9():
    git_host = _fx("qtrm64-take-over")
    git_host["look"].update(repository=None, upstream="https://gitlab.com/example-org/QTRM64", source="https://gitlab.com/example-org/QTRM64")
    local = _fx("qtrm64-take-over")
    local["look"].update(repository=None, upstream="/srv/work/QTRM64", source="/srv/work/QTRM64", source_kind="path")
    gh = _flat(_card(_fx("qtrm64-take-over")))
    assert "any GitHub setting" in gh and "branches and pull requests you approved" in gh
    for fx, six in ((git_host, "This repo is not on GitHub, so I cannot send the branch for you"),
                    (local, "This repo is only a folder on this machine, so I cannot send the branch anywhere for you")):
        card = _flat(_card(fx))
        assert oc.lint(_card(fx), fx["look"], fx["answers"]) == []
        assert six in card
        assert "GitHub" not in card.split("8. WHAT STAYS WITH YOU")[1].split("10. SETTINGS")[0]
        assert "pull request" not in card.split("9. UNDO")[1].split("10. SETTINGS")[0]
        assert "the alpaca/ branches you approved" in card
    assert "any setting on your git host" in _flat(_card(git_host)).split("8. WHAT STAYS WITH YOU")[1]
    assert "any setting" not in _flat(_card(local)).split("8. WHAT STAYS WITH YOU")[1].split("9. UNDO")[0]


def test_a_repo_with_a_file_address_or_a_path_is_a_folder_and_one_with_a_remote_address_is_a_git_host():
    assert oc.host_kind({"repository": "a/b", "upstream": "https://github.com/a/b"}) == "github"
    for where in ("https://gitlab.com/a/b", "git@git.example.org:a/b.git", "ssh://git@host/a/b", "git://host/a/b"):
        assert oc.host_kind({"repository": None, "upstream": where}) == "git-host", where
    for where in ("/srv/work/QTRM64", "file:///srv/work/QTRM64", "../x", None):
        assert oc.host_kind({"repository": None, "upstream": where, "source": where}) == "folder", where
    assert oc.host_kind(None) == "folder"


@pytest.mark.parametrize("name", ["team-repo", "audit"])
def test_the_other_situations_follow_the_host_too(name):
    fx = _fx(name)
    fx["look"].update(repository=None, upstream="https://git.example.org/acme/webapp")
    fx["answers"]["done-guess"] = dict(fx["answers"]["done-guess"], value="the report test passes and Hana merged it")
    card = _flat(_card(fx))
    assert oc.lint(_card(fx), fx["look"], fx["answers"]) == []
    assert "GitHub" not in card.replace("This repo is not on GitHub", "")
    # the card promises no pull request; the footer setting is still shown (t-691) and says none is sent for this repo
    assert "pull request" not in re.sub(r"(?i)pull request footer", "footer", card).replace(
        "I send no pull request for this repo", "")
    assert "It is not used here: I send no pull request for this repo" in card


def test_the_merge_question_does_not_name_github_for_a_repo_that_is_not_on_it():
    fx = _fx("team-repo")
    on = oc._slot_question("merges", fx["look"], "team")[1][0]["text"]
    fx["look"]["repository"] = None
    off = oc._slot_question("merges", fx["look"], "team")[1][0]["text"]
    assert "GitHub" in on and "GitHub" not in off


def _team_with_merger(value, *, option="a", source="owner"):
    fx = _fx("team-repo")
    fx["answers"]["merges"] = dict(fx["answers"]["merges"], value=value, option=option, source=source)
    return fx


@pytest.mark.parametrize("said", ["Hana the team lead merges", "Priya merges after review", "Minh, our CTO, does the merging"])
def test_the_card_names_who_merges_when_the_owner_named_the_person(said):
    """Rehearsal round 1 (P2): the owner said "Hana merges" and the card never named her. Any person's name counts."""
    fx = _team_with_merger(said)
    card = _card(fx)
    assert '- Who merges: your team. You said "%s".' % said in card
    assert oc.lint(oc.render_marked(fx["look"], fx["answers"], date=fx["date"], name=fx["name"], people=fx["people"]),
                   fx["look"], fx["answers"], fx["people"]) == []


def test_the_card_does_not_invent_a_merger_the_owner_did_not_name():
    fx = _team_with_merger(None)
    assert "Who merges" not in _card(fx)
    assert "Who merges" not in _card(_team_with_merger("I merge my own changes", option="b"))
    reading = _card(_team_with_merger("Hana merges", source="agent"))
    assert 'Who merges: your team (my reading of your reply: "Hana merges").' in _flat(reading)


def test_a_native_card_says_who_merges_a_task_branch():
    """Rehearsal round 1 (P3): the card never said who merges a task branch."""
    card = _flat(_card(_fx("start-empty")))
    assert "Each task works on its own branch there, and I merge it into main only when its checks pass and you say so" in card
    assert "Publishing anywhere, merging a task into main, raising the level" in card
