"""Every first-chat question has 2 to 4 options and a recommended one; the core slots include where CI runs and, for a
take-over that others push to, who merges (op-062 t-697; design/attach-mode/definition.md step 3).

The pure rules are pinned on the fixtures of onboardkit; the real verbs are driven with the chat helper to show that an
option which needs the owner's words (limits b, a branch pattern) is recorded with --option and --value and that the
owner's whole reply stays the quote.
"""
from __future__ import annotations

import copy
import re

import pytest

from alpaca import onboard_card as oc
from alpaca.tests import onboardkit as kit
from alpaca.tests.attachkit import upstream  # noqa: F401
from alpaca.tests.onboardchat import chat  # noqa: F401

SITUATIONS = ("start-empty", "take-over", "team", "audit", "migrate")
#: every slot a round can print, in any situation (agent-only choices such as pr-says-agent and preset are never printed)
OPTION_SLOTS = sorted({s for slots in oc.ROUND.values() for s in slots} | set(oc.MIGRATE_SAME_REPO_ONLY) | {"ci", "merges"})


def _opening(fx):
    return {"opening": fx["answers"]["opening"]}


# ---- every question has options and one recommended ----------------------------------------------------------------------

@pytest.mark.parametrize("situation", SITUATIONS)
@pytest.mark.parametrize("slot", OPTION_SLOTS)
def test_every_question_has_two_to_four_options_and_one_recommended_option(slot, situation):
    fx = kit.qtrm64()
    text, options, rec, why = oc._slot_question(slot, fx["look"], situation, fx["answers"])
    assert text and why
    assert 2 <= len(options) <= 4, (slot, situation)
    assert len({o["key"] for o in options}) == len(options)
    assert rec in {o["key"] for o in options} and [o["key"] for o in options] == list("abcd")[:len(options)]
    assert all(o["key"] in oc.SLOTS[slot]["options"] for o in options)


@pytest.mark.parametrize("situation", SITUATIONS)
def test_the_goal_options_fit_the_situation_and_the_last_one_is_the_owners_own_words_and_recommended(situation):
    _text, options, rec, _why = oc._slot_question("goal", kit.qtrm64()["look"], situation)
    assert options[-1]["text"] == "Something else, in my own words" and rec == options[-1]["key"]
    assert len(options) >= 3 and len({o["text"] for o in options}) == len(options)


def test_limits_and_keep_working_have_a_none_and_a_say_them_option():
    _t, options, rec, _w = oc._slot_question("limits", None, "take-over")
    assert [o["text"] for o in options] == ["None", "Yes, I will say them"] and rec == "a"
    _t, options, rec, _w = oc._slot_question("keep-working", None, "migrate")
    assert [o["text"] for o in options] == ["Nothing in particular", "Yes, I will list them"] and rec in ("a", "b")


def test_branch_prefix_recommends_the_pattern_the_side_branches_share_and_otherwise_anything_goes():
    look = kit.qtrm64()["look"]
    _t, options, rec, _w = oc._slot_question("branch-prefix", look, "team")
    assert options[0]["text"] == "Anything goes" and options[1]["text"].startswith("Yes, this pattern:") and rec == "a"
    tips = [{"ref": "origin/feature/SHOP-12", "author": "A", "date": "2026-10-05"},
            {"ref": "origin/feature/SHOP-14", "author": "B", "date": "2026-10-06"}]
    look = dict(look, history=dict(look["history"], tips=tips))
    _t, options, rec, _w = oc._slot_question("branch-prefix", look, "team")
    assert rec == "b" and "feature/" in options[1]["text"]
    mixed = dict(look, history=dict(look["history"], tips=tips + [{"ref": "origin/hotfix/x", "author": "C", "date": "2026-10-07"}]))
    assert oc._slot_question("branch-prefix", mixed, "team")[2] == "a"


def test_a_branch_pattern_picked_with_no_words_uses_the_pattern_the_look_found():
    fx = kit.team()
    fx["look"]["around"]["contributing_branch_prefix"] = None
    fx["look"]["history"]["tips"] = [{"ref": "origin/feature/SHOP-12", "author": "A", "date": "2026-10-05"}]
    fx["answers"]["branch-prefix"] = kit.own(None, "b", quote="3b", ev=9)
    assert oc.settings(fx["look"], fx["answers"])["branch_prefix"] == "feature/"
    fx["answers"]["branch-prefix"] = kit.own(None, "a", quote="3a", ev=9)
    assert oc.settings(fx["look"], fx["answers"])["branch_prefix"] == "alpaca/"


# ---- where CI runs ----------------------------------------------------------------------------------------------------------

def test_the_ci_question_has_the_three_options_and_recommends_no_ci_when_the_look_found_none():
    text, options, rec, why = oc._slot_question("ci", kit.qtrm64()["look"], "take-over")
    assert text == "Where do this repo's checks run?" and rec == "a" and why
    assert [o["text"] for o in options] == ["Only on my machine, there is no CI",
                                            "On a CI service (GitHub Actions, GitLab CI, Jenkins or another)",
                                            "I do not know"]


def test_the_ci_slot_is_not_open_when_the_look_found_ci_files():
    assert oc._slot_open("ci", kit.qtrm64()["look"], "take-over") is True
    assert oc._slot_open("ci", kit.team()["look"], "team") is False


@pytest.mark.parametrize("name,situation", [("qtrm64-take-over", "take-over"), ("audit", "audit"), ("team-repo", "team")])
def test_ci_is_required_in_every_situation_that_attaches_a_repo_unless_the_repo_has_ci_files(name, situation):
    fx = kit.BUILDERS[name]()
    look = copy.deepcopy(fx["look"])
    look["around"]["ci"] = []
    assert "ci" in oc.required_slots(look, situation, _opening(fx))
    look["around"]["ci"] = [".github/workflows/ci.yml"]
    assert "ci" not in oc.required_slots(look, situation, _opening(fx))


def test_ci_is_not_required_for_an_empty_start_or_a_move_into_a_new_repo_and_is_for_a_move_into_the_same_repo():
    fx = kit.migrate_new()
    base = _opening(fx)
    assert "ci" not in oc.required_slots(fx["look"], "migrate", base)
    assert "ci" not in oc.required_slots(fx["look"], "migrate", dict(base, **{"migrate-target": kit.own(None, "b", ev=3)}))
    assert "ci" in oc.required_slots(fx["look"], "migrate", dict(base, **{"migrate-target": kit.own(None, "a", ev=3)}))
    fx = kit.start_empty()
    assert "ci" not in oc.required_slots(None, "start-empty", _opening(fx))


def test_round_one_is_unchanged_and_ci_is_asked_in_round_two_whenever_round_one_is_full_or_not():
    for name, situation, first in (("qtrm64-take-over", "take-over", ["others-commit", "files-in-repo", "goal", "limits"]),
                                   ("team-repo", "team", ["goal", "merges", "files-in-repo", "limits"])):
        fx = kit.BUILDERS[name]()
        look = copy.deepcopy(fx["look"])
        look["around"]["ci"] = []
        assert [q["slot"] for q in oc.next_round(look, _opening(fx))] == first            # round 1 as before, 4 at most
    fx = kit.audit()
    look = copy.deepcopy(fx["look"])
    look["around"]["ci"] = []
    assert [q["slot"] for q in oc.next_round(look, _opening(fx))] == ["goal", "audit-reader", "limits"]
    done = dict(_opening(fx), goal=fx["answers"]["goal"], **{"audit-reader": fx["answers"]["audit-reader"]},
                limits=kit.own("none", "a", quote="3a", ev=9))
    assert [q["slot"] for q in oc.next_round(look, done)] == ["ci"]
    assert oc.MAX_QUESTIONS == 4 and oc.MAX_ROUNDS == 2


# ---- who merges, for a take-over ------------------------------------------------------------------------------------------------

def test_a_take_over_asks_who_merges_only_when_others_push():
    fx = kit.qtrm64()
    base = dict(_opening(fx), goal=fx["answers"]["goal"])
    for option, required in (("a", False), ("b", True), ("c", True)):
        answers = dict(base, **{"others-commit": kit.own(None, option, quote="1" + option, ev=4)})
        assert ("merges" in oc.required_slots(fx["look"], "take-over", answers)) is required
        asked = [q["slot"] for q in oc.next_round(fx["look"], answers)]
        assert ("merges" in asked) is required
    answers = dict(base, **{"others-commit": kit.own(None, "b", quote="1b", ev=4)})
    _t, options, rec, _w = oc._slot_question("merges", fx["look"], "take-over", answers)
    assert rec == "b" and options[1]["text"] == "I merge my own changes"


def test_the_card_says_who_merges_for_a_take_over_and_that_a_solo_owner_merges():
    card = " ".join(kit.render(kit.qtrm64()).split())
    assert "Others who push: nobody now." in card and "Only you commit, so you merge." in card
    card = " ".join(kit.render(kit.qtrm64_others()).split())
    assert "Who merges: your team. You said \"Bach reviews and merges what he pushes\"." in card
    assert "Your team reviews and merges as usual" in card and "Merging is your team's call." in card
    fx = kit.qtrm64_others()
    fx["answers"]["merges"] = kit.own(None, "b", quote="1b", ev=11)
    card = " ".join(kit.render(fx).split())
    assert "Who merges: you." in card and "you merge it there" in card


# ---- the real verbs: an option plus the owner's words -------------------------------------------------------------------------

def _answer(chat, slot, reply, *args):
    rc, out = chat.run("onboard", "answer", slot, *args, "--quote", reply)
    assert rc == 0, out
    return out


def _entry(chat, slot):
    from alpaca import db, onboard_flow
    conn = db.connect(chat.root)
    try:
        return onboard_flow.epoch(conn)[0][slot]
    finally:
        conn.close()


def test_limits_option_b_records_the_option_and_the_owners_words_and_the_whole_reply_is_the_quote(chat, upstream):   # noqa: F811
    chat.look(upstream.url)
    reply = chat.say("1a. 4b, the GUI folder: do not touch the GUI folder. 3d. I want to add a hello feature")
    _answer(chat, "limits", reply, "--option", "b", "--value", "do not touch the GUI folder")
    got = _entry(chat, "limits")
    assert (got["option"], got["value"], got["quote"], got["source"]) == ("b", "do not touch the GUI folder", reply, "owner")
    assert oc.settings(chat_look(chat), {"limits": got})["limits"] == "do not touch the GUI folder"


def chat_look(chat):
    from alpaca import db, onboard_flow
    conn = db.connect(chat.root)
    try:
        return onboard_flow.epoch(conn)[1]
    finally:
        conn.close()


def test_limits_option_a_needs_no_words_and_the_card_says_none(chat, upstream):                                # noqa: F811
    chat.look(upstream.url)
    reply = chat.say("1a. 3d, I want to add a hello feature. 4a")
    _answer(chat, "others-commit", reply, "--option", "a")
    _answer(chat, "goal", reply, "--value", "I want to add a hello feature")
    _answer(chat, "limits", reply, "--option", "a")
    assert _entry(chat, "limits")["value"] is None
    rc, out = chat.run("onboard", "answer", "done-guess", "--value", "hello works", "--source", "agent")
    assert rc == 0, out
    chat.answer_round_two()
    cid, card = chat.propose()
    assert "Limits you gave: none" in " ".join(card.split())


def test_an_option_the_reply_does_not_name_is_refused_for_the_new_option_slots(chat, upstream):               # noqa: F811
    chat.look(upstream.url)
    reply = chat.say("just add a hello feature")
    rc, out = chat.run("onboard", "answer", "limits", "--option", "a", "--quote", reply)
    assert rc != 0 and "ONBOARD-OPTION-NOT-IN-REPLY" in out


def test_the_goal_is_still_the_owners_words_and_the_last_option_with_no_words_is_not_an_answer(chat, upstream):   # noqa: F811
    chat.look(upstream.url)
    reply = chat.say("1a. 3d. I want to add a hello feature")
    _answer(chat, "goal", reply, "--option", "d", "--value", "I want to add a hello feature")
    got = _entry(chat, "goal")
    assert (got["option"], got["value"], got["quote"]) == ("d", "I want to add a hello feature", reply)
    rc, out = chat.run("onboard", "answer", "goal", "--option", "d", "--value", "a summary of mine", "--quote", reply)
    assert rc != 0 and "ONBOARD-NOT-QUOTED" in out                                   # a paraphrase is still refused
    # "3d" alone picks the last option and says nothing: the goal is not answered yet
    bare = {"goal": kit.own(None, "d", quote="3d", ev=5)}
    assert oc.goal_text(bare, kit.qtrm64()["look"], "take-over") == ("", False)
    assert "goal" in oc.missing_required(kit.qtrm64()["look"], dict(bare, opening=kit.qtrm64()["answers"]["opening"]))


def test_a_starter_goal_option_picked_alone_is_the_goal_and_the_card_says_it_came_from_the_list():
    fx = kit.qtrm64()
    fx["answers"]["goal"] = kit.own(None, "a", quote="1a, 3a", ev=5)
    assert oc.goal_text(fx["answers"], fx["look"], "take-over") == ("Get it into a clean, working state first", True)
    card = " ".join(kit.render(fx).split())
    assert 'Your goal, from the option you chose: "Get it into a clean, working state first"' in card
    st = oc.settings(fx["look"], fx["answers"])
    assert st["goal"] == "Get it into a clean, working state first" and st["goal_from_option"] is True
    assert re.search(r"[^\x00-\x7f]", card) is None
    marked = oc.render_marked(fx["look"], fx["answers"], date=fx["date"], name=fx["name"], people=fx["people"])
    assert oc.lint(marked, fx["look"], fx["answers"]) == []


# ---- required slots are never pushed out of round 2 (t-697 fix round r1, D1) ---------------------------------------------------

def _slots_of(out):
    return re.findall(r"^ \d+\. \[([\w.-]+)\]", out, re.M)


def _next(chat):
    rc, out = chat.run("onboard", "next")
    assert (rc == 0) == ("ready: run bin/alpaca onboard propose" in out), out
    return out


def test_a_team_repo_asks_merges_and_ci_in_round_two_with_options_and_reaches_ready(chat, upstream):   # noqa: F811
    text = chat.say("we review pull requests in my team, the repo is at %s" % upstream.url)
    rc, out = chat.run("onboard", "look", upstream.url, "--scratch", chat.scratch, "--quote", text)
    assert rc == 0, out
    assert _slots_of(out) == ["goal", "merges", "files-in-repo", "limits"], out
    reply = chat.say("1c, I want to add a hello feature")
    _answer(chat, "goal", reply, "--option", "c", "--value", "I want to add a hello feature")
    out = _next(chat)
    assert out.startswith("round 2:") and "merges" in _slots_of(out) and "ci" in _slots_of(out), out
    assert "a) Only on my machine, there is no CI" in out and "why:" in out
    n = {s: i for i, s in enumerate(_slots_of(out), 1)}
    reply = chat.say("%da. %da" % (n["merges"], n["ci"]))
    _answer(chat, "merges", reply, "--option", "a")
    _answer(chat, "ci", reply, "--option", "a")
    assert "ready: run bin/alpaca onboard propose" in _next(chat)


def test_a_take_over_that_others_push_to_asks_merges_and_ci_in_round_two_and_reaches_ready(chat, upstream):   # noqa: F811
    chat.look(upstream.url)
    reply = chat.say("1b")                                  # the confirmer's case: only the others question answered
    _answer(chat, "others-commit", reply, "--option", "b")
    out = _next(chat)
    assert out.startswith("round 2:") and _slots_of(out) == ["goal", "merges", "ci"], out
    assert "a) Only on my machine, there is no CI" in out
    reply = chat.say("1d, I want to add a hello feature. 2b. 3a")
    _answer(chat, "goal", reply, "--option", "d", "--value", "I want to add a hello feature")
    _answer(chat, "merges", reply, "--option", "b")
    _answer(chat, "ci", reply, "--option", "a")
    assert "ready: run bin/alpaca onboard propose" in _next(chat)


@pytest.mark.parametrize("name,situation", [("team-repo", "team"), ("qtrm64-others", "take-over"), ("qtrm64-take-over", "take-over"),
                                            ("audit", "audit")])
def test_a_missing_required_slot_comes_before_every_optional_slot_in_a_later_round(name, situation):
    fx = kit.BUILDERS[name]()
    look = copy.deepcopy(fx["look"])
    look["around"]["ci"] = []
    answers = {"opening": fx["answers"]["opening"], "goal": kit.own("a goal", "d", quote="3d, a goal", ev=5)}
    answers["others-commit"] = fx["answers"].get("others-commit") or kit.own(None, "b", quote="1b", ev=4)
    asked = [q["slot"] for q in oc.next_round(look, answers)]
    missing = [s for s in oc.missing_required(look, answers)]
    assert missing and asked[:len(missing)] == missing, (asked, missing)
