"""A move to new code through the real first-chat verbs (op-062 t-692, fix round r1).

Round 1 is built by `onboard look` from the opening alone, so `migrate-target` is never answered when it is chosen.
A move into the same repo (target a) is attached and hands its changes to that repo, so who else commits and whether
Alpaca files may live in it become required in the next round; a move into a new repo (target b) never asks them.
"""
from __future__ import annotations

import re

import pytest

from alpaca import onboard_card
from alpaca.tests.attachkit import upstream  # noqa: F401
from alpaca.tests.onboardchat import Chat, chat  # noqa: F401

OPENING = "we want to rewrite this old code in Python, in a new repo. it is at %s"
GOAL = "a Python version with the same pages"
KEEP = "the old pages keep working until the switch"
LIMIT = "do not touch the vendor folder"


def _slots(out):
    return re.findall(r"^ \d+\. \[([\w.-]+)\]", out, re.M)


def _next(chat):
    rc, out = chat.run("onboard", "next")             # 0 when ready, 1 when it printed a round
    assert (rc == 0) == ("ready: run bin/alpaca onboard propose" in out), out
    return out


def _answer(chat, slot, reply, *args):
    rc, out = chat.run("onboard", "answer", slot, *args, "--quote", reply)
    assert rc == 0, out


def _look(chat, source, opening=None):
    return chat.look(source, opening or OPENING % source)


def _answer_round_one(chat, target):
    reply = chat.say("1%s. %s. %s. %s" % (target, GOAL, KEEP, LIMIT))
    _answer(chat, "migrate-target", reply, "--option", target)
    _answer(chat, "goal", reply, "--value", GOAL)
    _answer(chat, "keep-working", reply, "--value", KEEP)
    _answer(chat, "limits", reply, "--value", LIMIT)
    return reply


def test_a_move_into_a_new_repo_is_asked_neither_footprint_question(chat, upstream):
    out = _look(chat, upstream.url)
    assert _slots(out) == ["migrate-target", "goal", "keep-working", "limits"]
    _answer_round_one(chat, "b")
    assert "ready: run bin/alpaca onboard propose" in _next(chat)
    asked = [e["data"]["questions"] for e in chat.events("onboard-round")]
    assert all(q["slot"] not in ("others-commit", "files-in-repo") for qs in asked for q in qs)


def test_a_move_into_the_same_repo_is_asked_the_footprint_questions_in_round_two(chat, upstream):
    out = _look(chat, upstream.url)
    assert _slots(out) == ["migrate-target", "goal", "keep-working", "limits"]
    _answer_round_one(chat, "a")
    out = _next(chat)
    assert "not ready" not in out and "ready:" not in out
    assert out.startswith("round 2:")
    assert _slots(out) == ["others-commit", "files-in-repo", "ci"]
    reply = chat.say("1a. 2a. 3a")
    _answer(chat, "others-commit", reply, "--option", "a")
    _answer(chat, "files-in-repo", reply, "--option", "a")
    _answer(chat, "ci", reply, "--option", "a")
    assert "ready: run bin/alpaca onboard propose" in _next(chat)
    rc, out = chat.run("onboard", "answer", "done-guess", "--value", "the pages match", "--source", "agent")
    assert rc == 0, out
    cid, card = chat.propose()
    assert "Commit identity: Maintenance bot <maintenance@localhost> (proposed)" in card
    assert "Pull request footer: off" in card and "Branch prefix: fix/ (proposed)" in card
    assert "Others who push: nobody now" in card


def test_a_footprint_slot_the_look_cannot_open_does_not_block_ready(chat, upstream):
    """No recent authors: `others-commit` cannot be asked, so it is not required."""
    _look(chat, upstream.url)
    _answer_round_one(chat, "a")
    look, answers = chat_epoch(chat)
    look = dict(look, history=dict(look.get("history") or {}, recent_authors=[]))
    assert onboard_card.required_slots(look, "migrate", answers) == ["goal", "migrate-target", "files-in-repo", "ci"]
    look["around"] = dict(look.get("around") or {}, alpaca_files=["ALPACA-MANIFEST"])
    assert onboard_card.missing_required(look, answers) == ["ci"]
    look["around"] = dict(look["around"], ci=[".github/workflows/ci.yml"])          # CI files in the repo have answered
    assert onboard_card.missing_required(look, answers) == []


def chat_epoch(chat):
    from alpaca import db, onboard_flow
    conn = db.connect(chat.root)
    try:
        answers, look, _look_id, _opening = onboard_flow.epoch(conn)
    finally:
        conn.close()
    return look, answers


@pytest.mark.parametrize("opening,first", [
    ("I took over this project from my old team, it is at %s . not sure where to start",
     ["others-commit", "files-in-repo", "goal", "limits"]),
    ("we review pull requests in my team, the repo is at %s", ["goal", "merges", "files-in-repo", "limits"]),
    ("can you audit this repo for me, it is at %s, I just want to understand how login works",
     ["goal", "audit-reader", "limits"]),
])
def test_the_other_situations_keep_their_first_round(chat, upstream, opening, first):
    out = _look(chat, upstream.url, opening % upstream.url)
    assert _slots(out) == first


def test_a_start_empty_round_is_unchanged():
    assert [q["slot"] for q in onboard_card.next_round(None, {"opening": {"value": "I want to make a little app for my class",
                                                                        "source": "owner",
                                                                        "quote": "I want to make a little app for my class"}})] == [
        "goal", "stack", "limits"]
