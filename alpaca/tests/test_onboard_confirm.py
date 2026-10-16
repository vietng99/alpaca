"""The owner's yes (op-062 t-656, AC-19 and the fail case: nothing is configured before the owner confirms).

`confirm` takes only the owner's own typed words from the session transcript: a lone yes right after the
ask, or `card-<id> yes`. A click, a subagent line, an `sdk` entry, a yes with a condition, a yes typed
before the ask or after another message, a card the owner was not shown, and a card whose stored copy
changed are all refused."""
import json
import os

import pytest

from alpaca import ownerword
from alpaca.tests import ownerword_fixtures as owf
from alpaca.tests.attachkit import upstream  # noqa: F401  (a fixture)
from alpaca.tests.onboardchat import chat  # noqa: F401  (a fixture)


@pytest.fixture
def carded(chat, upstream):                                  # noqa: F811
    cid, card = chat.walk_to_card(upstream.url)
    chat.cid, chat.card = cid, card
    return chat


def confirm(c, quote, cid=None):
    return c.run("onboard", "confirm", cid or c.cid, "--quote", quote)


def kinds(c):
    return [e["kind"] for e in c.events()]


@pytest.mark.parametrize("reply", ["yes", "Yes", "yes.", "YES!", "  yes  ", "ok", "okay", "go ahead", "Looks good!", "sounds good",
                                   "do it", "yes please", "fine", "approve", "go"])
def test_a_lone_assent_right_after_the_ask_is_the_owners_yes(carded, reply):
    carded.say(reply)
    rc, out = confirm(carded, reply)
    assert rc == 0, out
    assert "confirmed by the owner" in out and "form sole" in out
    ev = carded.events("onboard-confirmed")[-1]
    assert ev["actor"] == "owner" and ev["data"]["card"] == carded.cid and ev["data"]["owner_word"]["quote"] == reply.strip()
    assert ev["data"]["sha256"] and carded.events("onboard-card")[-1]["data"]["sha256"] == ev["data"]["sha256"]
    from alpaca import adopt
    assert adopt.first_chat_state(carded.conn) == "confirmed"


@pytest.mark.parametrize("reply", ["yes, but use L3", "yes?", "sure thing", "yes and also raise the level", "yeah", "y",
                                   "no wait, yes", "what is a gate?", "change line 7 to L3"])
def test_a_reply_that_is_not_a_lone_assent_is_refused_and_read_as_a_change_or_a_question(carded, reply):
    carded.say(reply)
    rc, out = confirm(carded, reply)
    assert rc == 1 and "ONBOARD-NOT-A-YES" in out
    assert "onboard-confirmed" not in kinds(carded)


def test_a_yes_typed_before_the_ask_is_refused(chat, upstream):                          # noqa: F811
    chat.look(upstream.url)
    chat.answers()
    chat.say("yes")                                          # typed while the agent was still working
    cid, card = chat.propose()
    rc, out = chat.run("onboard", "confirm", cid, "--quote", "yes")
    assert rc == 1 and "ONBOARD-NOT-A-YES" in out and "onboard-confirmed" not in kinds(chat)


def test_a_yes_after_another_owner_message_is_refused_until_the_agent_asks_again(carded):
    carded.say("wait, what does a gate do?")
    carded.host("A gate is a command that must pass.")
    carded.say("yes")
    rc, out = confirm(carded, "yes")
    assert rc == 1 and "ONBOARD-NOT-A-YES" in out
    rc, out = carded.run("onboard", "propose", "--reask")
    assert rc == 0 and out.startswith('Reply "yes"') and carded.cid in out and "SETUP PROPOSAL" not in out
    carded.say("yes")
    rc, out = confirm(carded, "yes")
    assert rc == 0, out


def test_a_side_question_costs_no_repaste_of_the_card(carded):
    carded.say("what is a gate?")
    carded.host("A gate is a command that must pass before a change leaves the studio. Reply yes to set up the card above.")
    carded.run("onboard", "propose", "--reask")
    carded.say("yes")
    rc, out = confirm(carded, "yes")
    assert rc == 0, out                                      # the card was shown once, at its first ask


def test_a_click_is_never_the_owners_yes(carded):
    carded.say_click("yes")
    rc, out = confirm(carded, "yes")
    assert rc == 1 and "onboard-confirmed" not in kinds(carded)


def test_an_assistant_or_subagent_line_that_says_yes_is_not_the_owners_yes(carded):
    carded.host("yes", sidechain=False)
    carded._append(owf.typed("yes", ts=carded.now(), isSidechain=True))
    rc, out = confirm(carded, "yes")
    assert rc == 1 and "onboard-confirmed" not in kinds(carded)


def test_a_headless_sdk_entry_is_not_the_owners_yes(carded):
    carded._append(owf.typed("yes", ts=carded.now(), entrypoint="sdk-cli"))
    rc, out = confirm(carded, "yes")
    assert rc == 1 and "onboard-confirmed" not in kinds(carded)


def test_a_quote_that_is_not_a_message_the_owner_typed_is_refused(carded):
    carded.say("yes")
    rc, out = confirm(carded, "yes it is fine by me")
    assert rc == 1 and "ONBOARD-NOT-A-YES" in out


def test_the_long_form_with_the_id_counts_and_a_qualifier_does_not(carded):
    carded.say("%s yes, but use L3" % carded.cid)
    rc, out = confirm(carded, "%s yes, but use L3" % carded.cid)
    assert rc == 1 and "ONBOARD-NOT-A-YES" in out
    carded.say("%s yes" % carded.cid)
    rc, out = confirm(carded, "%s yes" % carded.cid)
    assert rc == 0 and "form id" in out


def test_the_id_of_another_card_is_not_a_yes_to_this_one(carded):
    carded.say("card-00000000 yes")
    rc, out = confirm(carded, "card-00000000 yes")
    assert rc == 1 and "onboard-confirmed" not in kinds(carded)


def test_a_no_records_a_decline_and_confirms_nothing(carded):
    carded.say("no")
    rc, out = confirm(carded, "no")
    assert rc == 1 and "declined" in out
    assert kinds(carded).count("onboard-declined") == 1 and "onboard-confirmed" not in kinds(carded)
    ev = carded.events("onboard-declined")[-1]
    assert ev["actor"] == "owner" and ev["data"]["owner_word"]["quote"] == "no" and ev["data"]["card"] == carded.cid
    from alpaca import adopt
    assert adopt.first_chat_state(carded.conn) == "due"


def test_a_card_the_owner_was_not_shown_is_refused_and_reasking_after_pasting_it_works(chat, upstream):    # noqa: F811
    cid, card = chat.walk_to_card(upstream.url, show=False)
    chat.host("Here is what I propose: set it up as I described.")                 # a paraphrase, not the card
    chat.say("yes")
    rc, out = chat.run("onboard", "confirm", cid, "--quote", "yes")
    assert rc == 1 and "ONBOARD-CARD-NOT-SHOWN" in out and "onboard-confirmed" not in kinds(chat)
    chat.host(card)
    rc, out = chat.run("onboard", "propose", "--reask")
    assert rc == 0
    chat.say("yes")
    rc, out = chat.run("onboard", "confirm", cid, "--quote", "yes")
    assert rc == 0, out


def test_a_card_that_was_trimmed_is_not_the_card_that_was_shown(chat, upstream):                 # noqa: F811
    cid, card = chat.walk_to_card(upstream.url, show=False)
    chat.host(card.replace("9. UNDO", "").replace("Before your yes there is nothing to undo", "Nothing to undo"))
    chat.say("yes")
    rc, out = chat.run("onboard", "confirm", cid, "--quote", "yes")
    assert rc == 1 and "ONBOARD-CARD-NOT-SHOWN" in out


def test_the_card_inside_a_code_fence_and_quote_marks_counts_as_shown(chat, upstream):          # noqa: F811
    cid, card = chat.walk_to_card(upstream.url, show=False)
    chat.host("Here is the card:\n\n```text\n" + "\n".join("> " + line for line in card.split("\n")) + "\n```\n")
    chat.say("yes")
    rc, out = chat.run("onboard", "confirm", cid, "--quote", "yes")
    assert rc == 0, out


def test_a_card_split_over_several_assistant_lines_counts_as_shown(chat, upstream):              # noqa: F811
    cid, card = chat.walk_to_card(upstream.url, show=False)
    lines = card.split("\n")
    chat.host("\n".join(lines[:40]))                         # the assistant's text blocks arrive as separate lines
    chat.host("\n".join(lines[40:]))
    chat.say("yes")
    rc, out = chat.run("onboard", "confirm", cid, "--quote", "yes")
    assert rc == 0, out


def test_a_card_shown_by_a_subagent_does_not_count(chat, upstream):                              # noqa: F811
    cid, card = chat.walk_to_card(upstream.url, show=False)
    chat.host(card, sidechain=True)
    chat.say("yes")
    rc, out = chat.run("onboard", "confirm", cid, "--quote", "yes")
    assert rc == 1 and "ONBOARD-CARD-NOT-SHOWN" in out


def test_a_stored_copy_that_changed_is_refused(carded):
    path = os.path.join(carded.root, ".alpaca", "onboard", "cards", carded.cid + ".md")
    with open(path, "a") as fh:
        fh.write("sneaked in\n")
    carded.say("yes")
    rc, out = confirm(carded, "yes")
    assert rc == 1 and "ONBOARD-COPY-CHANGED" in out and "onboard-confirmed" not in kinds(carded)


def test_an_unknown_card_and_a_card_nobody_asked_about_are_refused(carded):
    carded.say("yes")
    rc, out = confirm(carded, "yes", cid="card-deadbeef")
    assert rc == 1 and "ONBOARD-NO-CARD" in out


def test_a_replaced_card_cannot_be_confirmed_the_newest_one_can(carded):
    old = carded.cid
    carded.say("make it L2 please")
    rc, out = carded.run("onboard", "answer", "level-override", "--value", "L2", "--quote", "make it L2 please")
    assert rc == 0, out
    new, card = carded.propose()
    assert new != old
    carded.say("yes")
    rc, out = confirm(carded, "yes", cid=old)
    assert rc == 1 and "ONBOARD-SUPERSEDED" in out
    rc, out = confirm(carded, "yes", cid=new)
    assert rc == 0, out


def test_confirm_writes_no_configuration(carded):
    before = open(os.path.join(carded.root, "project.yaml"), "rb").read()
    carded.say("yes")
    rc, out = confirm(carded, "yes")
    assert rc == 0
    assert open(os.path.join(carded.root, "project.yaml"), "rb").read() == before
    assert not os.path.exists(os.path.join(carded.root, "app"))


def test_the_terminal_path_prints_the_card_and_takes_the_typed_id(project, monkeypatch, upstream):   # noqa: F811
    """No agent session in the environment (a person at a terminal, or Codex): the card is printed and the
    person types its id."""
    from alpaca.tests.onboardchat import Chat
    from alpaca import onboard_flow
    c = Chat(project, monkeypatch)
    try:
        cid, card = c.walk_to_card(upstream.url)
        for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "ALPACA_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SANDBOX"):
            monkeypatch.delenv(name, raising=False)
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok, lines = onboard_flow.confirm_verb(c.conn, c.root, "cli", cid, quote=None, environ={},
                                                  isatty=lambda: True, read=lambda prompt="": cid)
        assert ok and "form terminal" in lines[0] and "SETUP PROPOSAL" in buf.getvalue()
        assert c.events("onboard-confirmed")[-1]["data"]["owner_word"]["path"] == "terminal"
    finally:
        c.close()


def test_answers_are_proved_against_the_owners_message(chat, upstream):                      # noqa: F811
    chat.look(upstream.url)
    reply = chat.say("1b. I want hello. nothing else")
    run = lambda *a: chat.run("onboard", "answer", *a, "--quote", reply)                     # noqa: E731
    rc, out = run("goal", "--value", "I want a summary of the week")
    assert rc == 1 and "ONBOARD-NOT-QUOTED" in out                                           # a paraphrase fails at the answer
    rc, out = run("others-commit", "--option", "c")
    assert rc == 1 and "ONBOARD-OPTION-NOT-IN-REPLY" in out                                  # the reply holds "1b", not "1c"
    rc, out = run("others-commit", "--option", "b")
    assert rc == 0, out
    rc, out = run("goal", "--value", "I want hello")
    assert rc == 0, out
    rc, out = chat.run("onboard", "answer", "goal", "--value", "x", "--quote", "something the owner never typed")
    assert rc == 1 and "OWNER-WORD" in out
    rc, out = chat.run("onboard", "answer", "goal", "--value", "x", "--quote", reply, "--source", "agent")
    assert rc == 0                                                                           # an agent reading is allowed, and marked
    last = chat.events("onboard-answer")[-1]["data"]
    assert last["source"] == "agent" and last["slot"] == "goal"


def test_an_owner_answer_needs_a_message_typed_after_the_look(chat, upstream):              # noqa: F811
    early = chat.say("1a. I want hello")
    chat.look(upstream.url)
    rc, out = chat.run("onboard", "answer", "goal", "--value", "I want hello", "--quote", early)
    assert rc == 1 and "OWNER-WORD" in out


def test_findings_gates_and_unknowns_are_checked_before_they_are_kept(chat, upstream):      # noqa: F811
    chat.look(upstream.url)
    run = lambda *a: chat.run("onboard", "answer", *a)                                      # noqa: E731
    rc, out = run("finding", "--value", "main prints hello", "--pointer", "repo:src/main.py:1-1", "--source", "agent")
    assert rc == 0 and "source [8]" in out
    for pointer in ("repo:src/nope.py", "repo:src/main.py:1-99", "repo:../../etc/passwd", "src/main.py", "repo:/etc/passwd"):
        rc, out = run("finding", "--value", "x", "--pointer", pointer, "--source", "agent")
        assert rc == 1 and "ONBOARD-POINTER" in out, pointer
    for i in range(2):
        run("finding", "--value", "f%d" % i, "--pointer", "repo:Makefile", "--source", "agent")
    rc, out = run("finding", "--value", "one too many", "--pointer", "repo:Makefile", "--source", "agent")
    assert rc == 1 and "ONBOARD-LIMIT" in out
    rc, out = run("unknown", "--value", "who deploys it", "--source", "agent")
    assert rc == 0
    rc, out = run("gate.extra", "--name", "bad name", "--argv-json", '["true"]', "--reason", "x", "--source", "agent")
    assert rc == 1 and "ONBOARD-GATE" in out
    rc, out = run("gate.extra", "--name", "syntax", "--argv-json", '["clang","-fsyntax-only","a.c"]', "--source", "agent")
    assert rc == 1 and "--reason" in out
    rc, out = run("gate.extra", "--name", "syntax", "--argv-json", '["make","check"]', "--reason", "Makefile", "--source", "agent",
                  "--proves", "the check target passes")
    assert rc == 0 and "not tried before the yes" in out
    rc, out = run("gate.extra", "--name", "syntax", "--argv-json", "not json", "--reason", "x", "--source", "agent")
    assert rc == 1


def test_an_unknown_slot_and_a_second_opening_are_handled(chat, upstream):                  # noqa: F811
    chat.look(upstream.url)
    rc, out = chat.run("onboard", "answer", "favourite-colour", "--value", "blue", "--source", "agent")
    assert rc == 1 and "ONBOARD-SLOT" in out
    rc, out = chat.run("onboard", "answer", "goal", "--value", "x")
    assert rc == 0 and chat.events("onboard-answer")[-1]["data"]["source"] == "agent"       # no quote: an agent reading, marked on the card
    rc, out = chat.run("onboard", "answer", "goal", "--value", "x", "--source", "owner")
    assert rc == 1 and "ONBOARD-QUOTE" in out                                               # an owner answer needs the owner's message
    rc, out = chat.run("onboard", "answer", "done-guess", "--value", "x", "--quote", "y", "--source", "owner")
    assert rc == 1 and "ONBOARD-SOURCE" in out                                              # a guess is never the owner's
    rc, out = chat.run("onboard", "answer", "limits")
    assert rc == 1 and "ONBOARD-VALUE" in out


def test_the_answers_fold_keeps_the_last_answer_and_every_finding(chat, upstream):          # noqa: F811
    from alpaca import onboard_flow
    chat.look(upstream.url)
    chat.run("onboard", "answer", "limits", "--value", "first", "--source", "agent")
    chat.run("onboard", "answer", "limits", "--value", "second", "--source", "agent")
    chat.run("onboard", "answer", "finding", "--value", "a", "--pointer", "repo:Makefile", "--source", "agent")
    chat.run("onboard", "answer", "finding", "--value", "b", "--pointer", "repo:Makefile", "--source", "agent")
    answers = onboard_flow.answers_of(chat.conn)
    assert answers["limits"]["value"] == "second" and [f["value"] for f in answers["finding"]] == ["a", "b"]
    assert answers["opening"]["source"] == "owner"


def test_propose_refuses_until_the_required_answers_exist_and_next_asks_round_two(chat, upstream):   # noqa: F811
    chat.look(upstream.url)
    rc, out = chat.run("onboard", "propose")
    assert rc == 1 and "ONBOARD-NEEDS" in out and "goal" in out
    rc, out = chat.run("onboard", "next")
    assert rc == 1 and "round 2:" in out and "[goal]" in out and "[others-commit]" not in out
    rc, out = chat.run("onboard", "next")                   # round 3: only what is still required, as options questions
    assert rc == 1 and out.startswith("round 3:") and "[goal]" in out and "[ci]" in out and "a) Only on my machine" in out
    reply = chat.say("I want hello")
    chat.run("onboard", "answer", "goal", "--value", "I want hello", "--quote", reply)
    reply = chat.say("2a")                              # ci was printed as question 2 of round 3
    rc, out = chat.run("onboard", "answer", "ci", "--option", "a", "--quote", reply)
    assert rc == 0, out
    rc, out = chat.run("onboard", "next")
    assert rc == 0 and out.strip() == "ready: run bin/alpaca onboard propose"


def test_skip_needs_the_owners_words_and_ends_the_first_chat(chat):
    rc, out = chat.run("onboard", "skip", "--quote", "skip setup")
    assert rc == 1 and "OWNER-WORD" in out                                  # the owner has typed nothing yet
    chat.say("skip it, I will do it by hand")                               # a sentence is not the skip phrase
    rc, out = chat.run("onboard", "skip", "--quote", "skip it, I will do it by hand")
    assert rc == 1 and "ONBOARD-NO-OWNER-PROOF" in out and "skip setup" in out, out
    chat.say("skip setup")
    rc, out = chat.run("onboard", "skip", "--quote", "skip setup")
    assert rc == 0, out
    from alpaca import adopt
    assert adopt.first_chat_state(chat.conn) == "skipped"
    rc, out = chat.run("onboard", "look", "/tmp", "--scratch", chat.scratch, "--quote", "x")
    assert rc == 1 and "ONBOARD-NOT-OPEN" in out


def _changed_card(c):
    """The owner asks for L2 after the first card was shown whole; returns the new card id and card text."""
    c.say("yes, but use L2")
    rc, out = c.run("onboard", "answer", "level-override", "--value", "L2", "--quote", "yes, but use L2")
    assert rc == 0, out
    return c.propose(show=False)


def test_a_changed_card_may_be_shown_as_its_changed_parts_when_the_first_was_shown_whole(carded):
    from alpaca import onboard_card as oc
    old = carded.card
    new_id, new_card = _changed_card(carded)
    assert oc.changed_parts(old, new_card) == [7, 10]                           # the level, and the settings line that names it
    shown = "Changed:\n\n" + "\n".join(
        line for key in (7, 10) for line in [p for p in new_card.split("\n\n") if p.startswith("%d. " % key)])
    carded.host(shown + "\n\n(code %s) Reply yes to set this up, or tell me another line to change." % new_id)
    carded.say("yes")
    rc, out = confirm(carded, "yes", cid=new_id)
    assert rc == 0, out


def test_a_changed_card_is_refused_when_a_changed_part_was_left_out(carded):
    new_id, new_card = _changed_card(carded)
    part7 = [p for p in new_card.split("\n\n") if p.startswith("7. ")][0]
    carded.host(part7 + "\n(code %s)" % new_id)                                  # part 10 changed too and was not shown
    carded.say("yes")
    rc, out = confirm(carded, "yes", cid=new_id)
    assert rc == 1 and "ONBOARD-CARD-NOT-SHOWN" in out


def test_a_changed_card_is_refused_when_its_id_was_not_shown(carded):
    new_id, new_card = _changed_card(carded)
    part7 = [p for p in new_card.split("\n\n") if p.startswith("7. ")][0]
    part10 = [p for p in new_card.split("\n\n") if p.startswith("10. ")][0]
    carded.host(part7 + "\n\n" + part10)
    carded.say("yes")
    rc, out = confirm(carded, "yes", cid=new_id)
    assert rc == 1 and "ONBOARD-CARD-NOT-SHOWN" in out


def test_a_changed_card_needs_the_first_card_shown_whole(chat, upstream):                  # noqa: F811
    cid, card = chat.walk_to_card(upstream.url, show=False)                       # the first card was never shown
    chat.say("yes, but use L2")
    chat.run("onboard", "answer", "level-override", "--value", "L2", "--quote", "yes, but use L2")
    new_id, new_card = chat.propose(show=False)
    parts = [p for p in new_card.split("\n\n") if p.split(".")[0] in ("7", "10")]
    chat.host("\n\n".join(parts) + "\n(code %s)" % new_id)
    chat.say("yes")
    rc, out = chat.run("onboard", "confirm", new_id, "--quote", "yes")
    assert rc == 1 and "ONBOARD-CARD-NOT-SHOWN" in out


def test_propose_names_the_changed_parts_for_the_agent_on_the_error_output(carded, capsys):
    from alpaca import cli
    carded.say("make it L2")
    carded.run("onboard", "answer", "level-override", "--value", "L2", "--quote", "make it L2")
    capsys.readouterr()
    assert cli.main(["onboard", "propose"]) == 0
    err = capsys.readouterr().err
    assert "propose: this card replaces %s. Parts that changed: 7, 10." % carded.cid in err


def test_a_source_number_the_agent_quotes_in_turn_one_is_the_number_in_the_card(chat, upstream):      # noqa: F811
    import re
    out = chat.look(upstream.url)
    printed = dict((int(n), t) for n, t in re.findall(r"^ \[(\d+)\] (.*)$", out, re.M))
    rc, said = chat.run("onboard", "answer", "finding", "--value", "main prints hello", "--pointer", "repo:src/main.py:1-1",
                        "--source", "agent")
    n = int(re.search(r"source \[(\d+)\]", said).group(1))
    chat.answers()
    cid, card = chat.propose()
    entries = dict((int(k), v) for k, v in re.findall(r"^\[(\d+)\] (.*)$", card, re.M))
    assert entries[n] == "repo:src/main.py:1-1" and "main prints hello (my reading of the code) [%d]" % n in " ".join(card.split())
    for k, text in printed.items():
        if k in entries:
            assert entries[k].split()[0] == text.split()[0]                       # the same source under the same number


def test_a_line_the_owner_changes_after_the_card_is_backed_by_their_words(carded):
    carded.say("no style check please, and it is a team repo")
    q = "no style check please, and it is a team repo"
    rc, out = carded.run("onboard", "answer", "preset", "--option", "b", "--quote", q)
    assert rc == 1 and "ONBOARD-OPTION-NOT-IN-REPLY" in out and "was not a question" in out
    rc, out = carded.run("onboard", "answer", "preset", "--option", "b", "--value", "no style check", "--quote", q)
    assert rc == 0, out
    rc, out = carded.run("onboard", "answer", "situation", "--option", "team", "--quote", q)
    assert rc == 0, out
    rc, out = carded.run("onboard", "answer", "situation", "--option", "audit", "--quote", q)
    assert rc == 1 and "ONBOARD-OPTION-NOT-IN-REPLY" in out
    rc, out = carded.run("onboard", "propose")
    assert rc == 1 and "ONBOARD-NEEDS" in out and "merges" in out                          # a team repo needs to know who merges
    rc, out = carded.run("onboard", "answer", "merges", "--option", "a", "--value", "it is a team repo", "--quote", q)
    assert rc == 0, out
    new, card = carded.propose()
    flat = " ".join(card.split())
    assert "plain-writing check off" in flat and "Situation: team repo. You told me:" in flat


def test_an_attach_card_needs_a_gate_and_one_the_agent_adds_makes_the_card(chat, tmp_path):             # noqa: F811
    from alpaca.tests.rehearsal import fixtures as rf
    import datetime
    src = rf.build_repo(tmp_path / "plain", [{"files": {"notes.txt": "hello\n"}, "day": datetime.date.today()}])
    chat.look(src)
    chat.answers()
    rc, out = chat.run("onboard", "propose")
    assert rc == 1 and "ONBOARD-NEEDS" in out and "at least one gate" in out
    rc, out = chat.run("onboard", "answer", "gate.extra", "--name", "diff-check", "--argv-json", '["git","diff","--check"]',
                       "--reason", "notes.txt", "--proves", "no stray white space in the tracked files", "--source", "agent")
    assert rc == 0 and "tried in the look clone: exit 0" in out
    cid, card = chat.propose()
    assert "diff-check (suggested by me): no stray white space in the tracked files. Tried it: pass" in " ".join(card.split())


# ------------------------------------------------- the owner's newest message (t-656 fix round 3; premortem A and S)

def _skip(c, quote):
    return c.run("onboard", "skip", "--quote", quote)


def _fc_state(c):
    from alpaca import adopt
    return adopt.first_chat_state(c.conn)


@pytest.mark.parametrize("word", ["skip", "Skip.", "SKIP!", "no setup", "No setup."])
def test_a_bare_skip_or_no_setup_reply_to_a_round_does_not_skip_the_first_chat(chat, upstream, word):   # noqa: F811
    """An owner who passes over one question of a round writes `skip`; the whole first chat must not end."""
    chat.look(upstream.url)
    chat.say(word)
    rc, out = _skip(chat, word)
    assert rc != 0 and _fc_state(chat) == "due", out
    rc, out = chat.run("attach", "init", upstream.url)
    assert rc != 0 and not os.path.isdir(os.path.join(chat.root, "app")), out


def test_a_bare_skip_typed_before_any_first_chat_step_does_not_skip(chat, upstream):            # noqa: F811
    chat.say("skip")
    chat.host("ok, moving on")
    chat.say("my code is at %s" % upstream.url)
    rc, out = _skip(chat, "skip")
    assert rc != 0 and _fc_state(chat) == "due", out


@pytest.mark.parametrize("looked", [False, True], ids=["no-look", "after-look"])
def test_a_skip_phrase_followed_by_another_owner_message_does_not_skip(chat, upstream, looked):  # noqa: F811
    if looked:
        chat.look(upstream.url)
    chat.say("skip setup")
    chat.host("ok")
    chat.say("actually, my code is at %s" % upstream.url)
    rc, out = _skip(chat, "skip setup")
    assert rc != 0 and _fc_state(chat) == "due", out
    assert "ONBOARD-NO-OWNER-PROOF" in out and "The owner wrote again after `skip setup`" in out, out
    chat.say("skip setup")
    rc, out = _skip(chat, "skip setup")
    assert rc == 0 and _fc_state(chat) == "skipped", out


def test_entries_that_are_not_owner_messages_after_the_phrase_do_not_block_a_real_skip(chat, upstream):  # noqa: F811
    chat.look(upstream.url)
    chat.say("skip setup")
    chat.host("Understood, I will skip the setup.")
    chat.say_click("skip setup")
    chat._append(owf.typed("<local-command-stdout>ok</local-command-stdout>", ts=chat.now(), isMeta=True))
    chat._append(owf.typed("a subagent prompt", ts=chat.now(), isSidechain=True))
    chat._append(owf.typed("summary of the chat so far", ts=chat.now(), isCompactSummary=True))
    rc, out = _skip(chat, "skip setup")
    assert rc == 0 and _fc_state(chat) == "skipped", out


def test_the_newest_message_is_decided_by_file_order_not_the_clock(chat, upstream):            # noqa: F811
    chat.look(upstream.url)
    ts = chat.now()
    chat._append(owf.typed("skip setup", ts=ts))
    chat._append(owf.typed("wait, not yet", ts=ts))
    rc, out = _skip(chat, "skip setup")
    assert rc != 0 and _fc_state(chat) == "due", out
    ts = chat.now()
    chat._append(owf.typed("ok I am sure now", ts=ts))
    chat._append(owf.typed("skip setup", ts=ts))
    rc, out = _skip(chat, "skip setup")
    assert rc == 0 and _fc_state(chat) == "skipped", out


def test_a_question_or_a_negation_is_not_a_skip(chat, upstream):                                # noqa: F811
    chat.look(upstream.url)
    for text in ("skip setup?", "don't skip setup", "should I skip setup", "skip setup, but keep my goal"):
        chat.say(text)
        rc, out = _skip(chat, text)
        assert rc != 0 and _fc_state(chat) == "due", (text, out)


def test_the_terminal_skip_for_codex_still_works(chat):                                          # noqa: F811
    from alpaca import onboard_flow
    lines = onboard_flow.skip_verb(chat.conn, chat.root, "cli", "onboard-skip", environ={}, isatty=lambda: True,
                                   read=lambda prompt="": "onboard-skip")
    assert _fc_state(chat) == "skipped", lines


def test_skip_phrases_hold_only_whole_orders_of_two_words_or_more():
    """Mutation control for the bare-skip cases above: put "skip" back and this fails."""
    from alpaca import onboard_card
    for phrase in onboard_card.SKIP_PHRASES:
        assert len(phrase.split()) >= 2, phrase
    assert "no setup" not in onboard_card.SKIP_PHRASES


def test_a_yes_the_owner_takes_back_in_the_next_message_does_not_confirm(carded):
    carded.say("yes")
    carded.say("wait, change the goal to a goodbye feature first")
    rc, out = confirm(carded, "yes")
    assert rc != 0 and "ONBOARD-NOT-NEWEST" in out and "The owner wrote again after answering the card" in out, out
    assert not carded.events("onboard-confirmed")
    # the agent reads it, the owner says yes again: the old yes is no longer the first message after the ask, so the
    # long form with the id counts
    carded.say("%s yes" % carded.cid)
    rc, out = confirm(carded, "%s yes" % carded.cid)
    assert rc == 0, out


def test_a_no_the_owner_takes_back_in_the_next_message_records_nothing(carded):
    carded.say("no")
    carded.say("sorry, I meant yes")
    rc, out = confirm(carded, "no")
    assert rc != 0 and "ONBOARD-NOT-NEWEST" in out, out
    assert not carded.events("onboard-declined")


def test_mutation_control_without_the_newest_rule_a_taken_back_yes_confirms(carded, monkeypatch):
    real = ownerword.human_write

    def no_newest(*a, **kw):
        kw["newest"] = False
        return real(*a, **kw)

    monkeypatch.setattr(ownerword, "human_write", no_newest)
    carded.say("yes")
    carded.say("wait, change the goal to a goodbye feature first")
    rc, out = confirm(carded, "yes")
    assert rc == 0, "the mutation went unseen: the newest-message case tests nothing"


def test_propose_prints_the_card_for_a_goal_of_any_length_and_keeps_every_word(chat, upstream):      # noqa: F811
    """t-687 (F6-1): a long goal never makes `onboard propose` refuse with ONBOARD-CARD-LINT."""
    chat.look(upstream.url)
    goal = "I want to add a hello feature" + "".join(", and then make the thing number %d work on windows too" % i for i in range(70))
    reply = chat.say("1a. %s. %s" % (goal, chat.LIMIT))
    for args in (["others-commit", "--option", "a"], ["goal", "--value", goal], ["limits", "--value", chat.LIMIT]):
        rc, out = chat.run("onboard", "answer", *args, "--quote", reply)
        assert rc == 0, out
    rc, out = chat.run("onboard", "answer", "done-guess", "--value", "hello works", "--source", "agent")
    assert rc == 0, out
    chat.answer_round_two()
    cid, card = chat.propose()
    assert ('"%s"' % goal) in " ".join(card.split()) and len(card.split("\n")) > 100


def test_the_words_that_name_who_merges_are_the_owners_and_reach_the_card(chat, upstream, monkeypatch):      # noqa: F811
    """Round 1 (P2): "Hana merges" was in the owner's reply and nowhere on the card. `answer merges --option a --value
    <their words>` keeps those words, and a value that is not a part of their message is refused."""
    from alpaca import onboard_flow
    chat.look(upstream.url)
    reply = chat.say("1a. Hana the team lead merges. I want hello. nothing else")
    monkeypatch.setattr(onboard_flow, "_option_number", lambda rounds, slot, key: (1, "My teammates review and merge"))
    rc, out = chat.run("onboard", "answer", "merges", "--option", "a", "--value", "Priya merges", "--quote", reply)
    assert rc == 1 and "ONBOARD-NOT-QUOTED" in out
    rc, out = chat.run("onboard", "answer", "merges", "--option", "a", "--value", "Hana the team lead merges", "--quote", reply)
    assert rc == 0, out
    row = chat.events("onboard-answer")[-1]["data"]
    assert row["slot"] == "merges" and row["option"] == "a" and row["value"] == "Hana the team lead merges"
    assert row["owner_word"]["quote"] == reply
