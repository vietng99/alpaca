"""A yes with polite filler is the owner's yes (op-062 t-688, rehearsal round 1 item 6).

Round 1: "ok got it. yes" and "Okay, yes, please set it up." were refused with ONBOARD-NOT-A-YES, and each refusal cost
one owner message. `confirm` now removes a closed list of assent and filler phrases from the reply; when the words
left are exactly `yes` the reply is a yes. Any other word keeps it a change ("yes, but use L3"). The owner-word proof
rules stay: the owner typed it, as the first message after the ask, and it is the newest."""
import pytest

from alpaca import ownerword
from alpaca import onboard_card as oc
from alpaca.tests.attachkit import upstream  # noqa: F401  (a fixture)
from alpaca.tests.onboardchat import chat  # noqa: F401  (a fixture)
from alpaca.tests.test_onboard_confirm import carded, confirm, kinds  # noqa: F401

YES_WITH_FILLER = ["ok got it. yes", "Okay, yes, please set it up.", "yes, thanks", "Sure, yes! Go ahead.",
                   "thank you, yes", "yes, sounds good", "OK. Yes. Set it up.", "got it, yes, thanks",
                   "yes\nthanks", "Yes, set it up", "okay okay yes"]
STILL_A_CHANGE = ["yes, but use L3", "ok yes but L3", "yes, set it up with L3", "ok, thanks", "sure", "yes yes",
                  "yes, not yet", "ok no yes", "yes?", "okay, yes, please set it up?", "yes, set it up later",
                  "thanks, but yes use L3", "no, set it up", "yes please do not set it up", "ok got it"]


@pytest.mark.parametrize("reply", YES_WITH_FILLER)
def test_a_yes_with_only_assent_and_filler_words_is_a_yes(reply):
    assert oc.reads_as_yes(reply), reply


@pytest.mark.parametrize("reply", STILL_A_CHANGE)
def test_any_other_word_keeps_the_reply_a_change(reply):
    assert not oc.reads_as_yes(reply), reply


def test_the_filler_list_is_closed_and_written_out():
    assert set(oc.YES_FILLER) == {"ok", "okay", "got it", "sure", "please set it up", "set it up", "go ahead", "thanks",
                                  "thank you", "sounds good"}


@pytest.mark.parametrize("reply", ["ok got it. yes", "Okay, yes, please set it up."])
def test_confirm_takes_the_two_round_one_replies(carded, reply):
    carded.say(reply)
    rc, out = confirm(carded, reply)
    assert rc == 0, out
    ev = carded.events("onboard-confirmed")[-1]
    assert ev["actor"] == "owner" and ev["data"]["form"] == "sole" and ev["data"]["owner_word"]["quote"] == reply
    assert ev["data"]["owner_word"]["path"] == "session" and ev["data"]["owner_word"]["line"]


@pytest.mark.parametrize("reply", ["yes, but use L3", "ok yes but L3", "yes, set it up with L3"])
def test_a_yes_with_a_condition_is_still_refused_and_read_as_a_change(carded, reply):
    carded.say(reply)
    rc, out = confirm(carded, reply)
    assert rc == 1 and "ONBOARD-NOT-A-YES" in out and "onboard-confirmed" not in kinds(carded)


def test_the_owner_word_rules_hold_for_a_yes_with_filler(chat, upstream):                  # noqa: F811
    """Typed before the ask, typed after another message, not typed at all, written by the assistant: all refused."""
    chat.look(upstream.url)
    chat.answers()
    chat.say("ok got it. yes")                                # typed while the agent was still working
    cid, card = chat.propose()
    rc, out = chat.run("onboard", "confirm", cid, "--quote", "ok got it. yes")
    assert rc == 1 and "ONBOARD-NOT-A-YES" in out and "onboard-confirmed" not in kinds(chat)
    chat.say("wait, what does a gate do?")
    chat.host("A gate is a command that must pass.")
    chat.say("Okay, yes, please set it up.")
    rc, out = chat.run("onboard", "confirm", cid, "--quote", "Okay, yes, please set it up.")
    assert rc == 1 and "ONBOARD-NOT-A-YES" in out
    rc, out = chat.run("onboard", "confirm", cid, "--quote", "ok thanks, yes")                   # the owner never typed this
    assert rc == 1 and "ONBOARD-NOT-A-YES" in out and "onboard-confirmed" not in kinds(chat)


def test_a_filler_yes_the_owner_takes_back_in_the_next_message_does_not_confirm(carded):
    carded.say("ok got it. yes")
    carded.say("actually wait")
    rc, out = confirm(carded, "ok got it. yes")
    assert rc == 1 and "onboard-confirmed" not in kinds(carded)


def test_a_click_or_an_assistant_line_with_filler_is_not_the_owners_yes(carded):
    carded.say_click("ok got it. yes")
    rc, out = confirm(carded, "ok got it. yes")
    assert rc == 1 and "onboard-confirmed" not in kinds(carded)


def test_doctor_reads_the_filler_yes_back_from_the_transcript_without_a_warning(carded):
    from alpaca import onboard_flow
    carded.say("Okay, yes, please set it up.")
    rc, out = confirm(carded, "Okay, yes, please set it up.")
    assert rc == 0, out
    word = carded.events("onboard-confirmed")[-1]["data"]["owner_word"]
    assert onboard_flow._reads_back(word) == "ok"
    assert not [t for lvl, t in onboard_flow.evidence_lines(carded.conn, carded.root) if lvl == "warn" and "yes" in t.lower()]
