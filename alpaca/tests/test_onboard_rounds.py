"""A required slot is always asked as an options question (op-062 t-697 fix round r2, D5 and D6).

Some required slots only become known late: a move's same-repo slots once `migrate-target` is `a`, or every slot after an
empty round 1. `onboard next` therefore prints another round with only the required slots still missing, up to
MAX_REQUIRED_ROUNDS rounds in all, and only then says to ask in plain words. The real verbs run the two cases the
confirmer found; a sweep over every situation runs the decision they share (`onboard_card.plan_next`).
"""
from __future__ import annotations

import copy
import itertools
import re

import pytest

from alpaca import onboard_card as oc
from alpaca.tests import onboardkit as kit
from alpaca.tests.attachkit import upstream  # noqa: F401
from alpaca.tests.onboardchat import chat  # noqa: F401


def _slots(out):
    return re.findall(r"^ \d+\. \[([\w.-]+)\]", out, re.M)


def _next(chat):
    rc, out = chat.run("onboard", "next")
    assert (rc == 0) == ("ready: run bin/alpaca onboard propose" in out), out
    return out


def _answer(chat, slot, reply, *args):
    rc, out = chat.run("onboard", "answer", slot, *args, "--quote", reply)
    assert rc == 0, out


def _look(chat, url, opening):
    text = chat.say(opening % url)
    rc, out = chat.run("onboard", "look", url, "--scratch", chat.scratch, "--quote", text)
    assert rc == 0, out
    return out


# ---- the confirmer's two cases, through the real verbs -------------------------------------------------------------------------

def test_a_move_whose_target_is_answered_in_round_two_gets_the_same_repo_slots_in_round_three(chat, upstream):   # noqa: F811
    out = _look(chat, upstream.url, "I want to migrate the repo at %s to a cleaner layout")
    assert _slots(out) == ["migrate-target", "goal", "keep-working", "limits"], out
    reply = chat.say("2c, I want a cleaner layout")
    _answer(chat, "goal", reply, "--option", "c", "--value", "I want a cleaner layout")
    out = _next(chat)
    assert out.startswith("round 2:") and _slots(out) == ["migrate-target"], out
    reply = chat.say("1a")
    _answer(chat, "migrate-target", reply, "--option", "a")
    out = _next(chat)
    assert out.startswith("round 3:") and _slots(out) == ["others-commit", "files-in-repo", "ci"], out
    assert out.count("(recommended") == 3 and "a) Only on my machine, there is no CI" in out and "why:" in out
    reply = chat.say("1a. 2a. 3a")
    for slot in ("others-commit", "files-in-repo", "ci"):
        _answer(chat, slot, reply, "--option", "a")
    assert "ready: run bin/alpaca onboard propose" in _next(chat)


def test_a_team_repo_whose_owner_answers_nothing_in_round_one_is_still_asked_ci_with_options(chat, upstream):   # noqa: F811
    out = _look(chat, upstream.url, "we review pull requests in my team, the repo is at %s")
    assert _slots(out) == ["goal", "merges", "files-in-repo", "limits"], out
    chat.say("not sure, you pick")                                  # nothing to record
    out = _next(chat)
    assert out.startswith("round 2:") and _slots(out) == ["goal", "merges", "ci"], out
    reply = chat.say("1c, I want to add a hello feature. 2a. 3a")
    _answer(chat, "goal", reply, "--option", "c", "--value", "I want to add a hello feature")
    _answer(chat, "merges", reply, "--option", "a")
    _answer(chat, "ci", reply, "--option", "a")
    assert "ready: run bin/alpaca onboard propose" in _next(chat)


def test_the_hard_stop_falls_back_to_plain_words_after_four_rounds(chat, upstream):                           # noqa: F811
    _look(chat, upstream.url, "we review pull requests in my team, the repo is at %s")
    for n in (2, 3, 4):
        assert _next(chat).startswith("round %d:" % n)
    rc, out = chat.run("onboard", "next")
    assert rc == 1 and "not ready: still missing goal, merges, ci after 4 rounds" in out and "plain words" in out


# ---- the sweep: every situation, CI files, recent authors, Alpaca files, and each early round answered fully, partly or not at all ----

BEHAVIOURS = ("full", "partial", "none")
SITUATIONS = ("start-empty", "take-over", "team", "audit", "migrate")


def _look_for(situation, ci, authors, alpaca_files):
    if situation == "start-empty":
        return None
    fx = kit.qtrm64()
    look = copy.deepcopy(fx["look"])
    look["around"]["ci"] = [".github/workflows/ci.yml"] if ci else []
    look["around"]["alpaca_files"] = ["ALPACA-MANIFEST"] if alpaca_files else []
    look["history"]["recent_authors"] = ([{"name": "A", "last": "2026-10-10"}, {"name": "B", "last": "2026-10-09"}]
                                         if authors else [])
    return look


def _opening(situation):
    return {"opening": kit.own("an opening", quote="an opening", ev=2),
            "situation": {"option": situation, "source": "agent", "reason": "test", "event": 3}}


def _reply(slot, question, target, others):
    """The answer the simulated owner gives to one printed question: the recommended option, or `target` for the move's
    target and `others` for who else pushes; the goal in their own words (its recommended option is the last one)."""
    option = {"migrate-target": target, "others-commit": others}.get(slot, question["recommended"])
    value = "my own goal" if slot == "goal" else None
    return kit.own(value, option, quote="1" + option, ev=10)


def _walk(situation, ci, authors, alpaca_files, behaviours, target, others):
    look = _look_for(situation, ci, authors, alpaca_files)
    answers = _opening(situation)
    printed = {}                                            # slot -> the question as printed (options, recommended)
    questions = oc.next_round(look, answers)               # round 1, as `onboard look` prints it
    rounds = 1
    while True:
        assert 1 <= len(questions) <= oc.MAX_QUESTIONS or rounds > 1
        behaviour = behaviours[rounds - 1] if rounds <= len(behaviours) else "full"
        for i, q in enumerate(questions):
            printed.setdefault(q["slot"], []).append(q)
            if behaviour == "full" or (behaviour == "partial" and i % 2 == 0):
                answers[q["slot"]] = _reply(q["slot"], q, target, others)
        state, got = oc.plan_next(look, answers, rounds)
        if state == "ready":
            return look, answers, printed, rounds
        assert state == "ask", (state, got, rounds)
        assert 1 <= len(got) <= oc.MAX_QUESTIONS
        assert {q["slot"] for q in got} == set(oc.missing_required(look, answers)), (got, rounds)
        questions = got
        rounds += 1


CASES = [(s, ci, au, af, b, t, o) for s in SITUATIONS for ci in (False, True) for au in (False, True) for af in (False, True)
         for b in itertools.product(BEHAVIOURS, repeat=2) for t in (("a", "b") if s == "migrate" else ("a",)) for o in "abc"]


def test_the_sweep_covers_at_least_the_confirmers_space():
    assert len([c for c in CASES if c[0] == "migrate"]) >= 336 > 0


@pytest.mark.parametrize("situation", SITUATIONS)
def test_every_path_reaches_ready_within_the_hard_stop_and_every_required_slot_was_printed_with_options(situation):
    ran = 0
    for case in (c for c in CASES if c[0] == situation):
        _s, ci, authors, alpaca_files, behaviours, target, others = case
        look, answers, printed, rounds = _walk(situation, ci, authors, alpaca_files, behaviours, target, others)
        ran += 1
        assert rounds <= oc.MAX_REQUIRED_ROUNDS, case
        for slot in oc.required_slots(look, situation, answers):
            qs = printed.get(slot)
            assert qs, (case, slot)                         # every required slot was printed ...
            assert any(2 <= len(q["options"]) <= 4 and q["recommended"] and
                       sum(1 for o in q["options"] if o["recommended"]) == 1 for q in qs), (case, slot)   # ... with options
        assert oc.missing_required(look, answers) == [], case
    assert ran >= 8


def test_a_chat_that_answers_nothing_ever_stops_at_the_hard_stop_and_never_loops():
    look = _look_for("team", False, True, False)
    answers = _opening("team")
    for rounds in range(1, oc.MAX_REQUIRED_ROUNDS):
        assert oc.plan_next(look, answers, rounds)[0] == "ask"
    assert oc.plan_next(look, answers, oc.MAX_REQUIRED_ROUNDS) == ("stop", ["goal", "merges", "ci"])
    assert oc.MAX_ROUNDS == 2 and oc.MAX_REQUIRED_ROUNDS == 4
