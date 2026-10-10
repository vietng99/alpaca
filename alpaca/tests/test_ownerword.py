"""op-019 t-201 (design section 1): owner words. Both entry shapes of an owner message, the answer
forms, the session binding, the transcript rules and the terminal path."""
import pytest

from alpaca import db, decisions, ownerword
from alpaca.ownerword import OwnerWordRefusal
from alpaca.tests.ownerword_fixtures import EARLY, LATE, Project, queued, tool_result, typed


@pytest.fixture
def p(tmp_path, monkeypatch):
    return Project(tmp_path, monkeypatch)


def refused(token, call, *a, **kw):
    with pytest.raises(OwnerWordRefusal) as exc:
        call(*a, **kw)
    assert exc.value.reason == token, exc.value
    return exc.value


def test_typed_prompt_answers_in_the_id_form(p):
    p.ask("q1")
    p.write([typed("q1 yes")])
    word = p.write_as("q1", "yes", "q1 yes", since=ownerword.ask_ts(p.conn, "q1"))
    assert word["path"] == "session" and word["line"] == 1 and word["quote"] == "q1 yes"
    assert word["transcript"] == p.transcript_of(p.sid)


def test_queued_command_message_counts(p):
    # the mid-turn shape: brief R4's order sits in it (89d39740 line 2196)
    p.ask("ord-3")
    p.write([queued("ord-3 yes")])
    word = p.write_as("ord-3", "yes", "ord-3 yes", since=ownerword.ask_ts(p.conn, "ord-3"))
    assert word["line"] == 1


def test_queued_command_needs_prompt_mode_and_human_origin(p):
    entry = queued("q1 yes")
    entry["attachment"]["commandMode"] = "bash"
    other = queued("q1 yes")
    other["attachment"]["origin"] = {"kind": "agent"}
    p.write([entry, other])
    refused(ownerword.R_NOT_FOUND, p.write_as, "q1", "yes", "q1 yes")


@pytest.mark.parametrize("entry", [
    typed("q1 yes", entrypoint="sdk-cli"),            # a claude -p run
    typed("q1 yes", isMeta=True),
    typed("q1 yes", isCompactSummary=True),
    typed("q1 yes", isSidechain=True),
    queued("q1 yes", entrypoint="sdk-cli"),
    queued("q1 yes", isSidechain=True),
    tool_result(),
])
def test_entries_that_are_not_owner_messages_never_count(p, entry):
    p.write([entry])
    refused(ownerword.R_NOT_FOUND, p.write_as, "q1", "yes", "q1 yes")


def test_an_entry_with_no_origin_never_counts(p):
    entry = typed("q1 yes")
    del entry["origin"]
    p.write([entry])
    refused(ownerword.R_NOT_FOUND, p.write_as, "q1", "yes", "q1 yes")


def test_text_blocks_are_joined_and_a_tool_result_block_refuses(p):
    blocks = typed("x")
    blocks["message"]["content"] = [{"type": "text", "text": "q1 yes"}]
    p.write([blocks])
    assert p.write_as("q1", "yes", "q1 yes")["line"] == 1
    mixed = typed("x")
    mixed["message"]["content"] = [{"type": "text", "text": "q1 yes"},
                                   {"type": "tool_result", "content": "z"}]
    p.write([mixed])
    refused(ownerword.R_NOT_FOUND, p.write_as, "q1", "yes", "q1 yes")


def test_an_unrelated_reply_answers_nothing(p):
    p.write([typed("ok now lets talk about autonomy")])
    refused(ownerword.R_UNBOUND, p.write_as, "q1", "yes", "ok now lets talk about autonomy")


def test_q1_is_not_matched_inside_q10(p):
    p.write([typed("q10 yes")])
    refused(ownerword.R_UNBOUND, p.write_as, "q1", "yes", "q10 yes")
    p.write([typed("q1 yes, q10 no")])
    assert p.write_as("q1", "yes", "q1 yes, q10 no")["answer"] == "yes"
    assert p.write_as("q10", "no", "q1 yes, q10 no")["answer"] == "no"
    p.write([typed("q1 yes q10 no")])      # the next subject id ends the segment, with no comma
    assert p.write_as("q1", "yes", "q1 yes q10 no", others=("q10",))["answer"] == "yes"


def test_runtime_ok_but_not_headless_is_not_a_yes(p):
    text = "runtime ok but not headless"
    p.write([typed(text)])
    refused(ownerword.R_UNBOUND, p.write_as, "runtime", "yes", text, others=("credentials",))
    text = "runtime ok but not headless, credentials yes"
    p.write([typed(text)])
    refused(ownerword.R_UNBOUND, p.write_as, "runtime", "yes", text)
    assert p.write_as("credentials", "yes", text)["answer"] == "yes"


def test_two_items_in_one_sentence_answer_neither(p):
    text = "O2 list is fine O3 as well"
    p.write([typed(text)])
    refused(ownerword.R_UNBOUND, p.write_as, "O2", "yes", text, others=("O3",))
    refused(ownerword.R_UNBOUND, p.write_as, "O3", "yes", text)


def test_a_segment_with_a_question_mark_answers_nothing(p):
    p.write([typed("q1 yes?")])
    refused(ownerword.R_UNBOUND, p.write_as, "q1", "yes", "q1 yes?")
    p.write([typed("q1 yes? q2 yes")])
    refused(ownerword.R_UNBOUND, p.write_as, "q1", "yes", "q1 yes? q2 yes")


def test_no_and_text_forms(p):
    p.write([typed("q1: no\nq2: inside the operator's session\nq3 reject.")])
    quote = "q1: no\nq2: inside the operator's session\nq3 reject."
    assert p.write_as("q1", "no", quote)["answer"] == "no"
    word = p.write_as("q2", "text", quote)
    assert word["text"] == "inside the operator's session"
    assert p.write_as("q2", "inside the operator's session", quote)["text"].startswith("inside")
    refused(ownerword.R_UNBOUND, p.write_as, "q2", "another text", quote)
    assert p.write_as("q3", "no", quote)["answer"] == "no"
    refused(ownerword.R_UNBOUND, p.write_as, "q1", "yes", quote)   # the owner said no


def test_the_quote_must_be_the_whole_message(p):
    p.write([typed("thanks. q1 yes, and also q2 later")])
    refused(ownerword.R_NOT_FOUND, p.write_as, "q1", "yes", "q1 yes")


def test_a_message_typed_before_the_subject_existed_never_counts(p):
    p.write([typed("q1 yes", ts=EARLY)])
    p.ask("q1")
    refused(ownerword.R_UNBOUND, p.write_as, "q1", "yes", "q1 yes",
            since=ownerword.ask_ts(p.conn, "q1"))
    p.write([typed("q1 yes", ts=EARLY), typed("q1 yes", ts=LATE)])
    assert p.write_as("q1", "yes", "q1 yes", since=ownerword.ask_ts(p.conn, "q1"))["line"] == 2


def test_extra_assent_words_for_order_close(p):
    p.write([typed("ord-2 lift")])
    refused(ownerword.R_UNBOUND, p.write_as, "ord-2", "yes", "ord-2 lift")
    assert p.write_as("ord-2", "yes", "ord-2 lift", extra_yes=("close", "lift"))["answer"] == "yes"


# ---- the whole-batch form
@pytest.mark.parametrize("phrase", ["all yes", "Yes all.", "yes to all", "ALL OK!"])
def test_whole_batch_phrase_in_the_first_message_after_the_ask(p, phrase):
    p.ask("batch:op-1")
    p.write([typed(phrase)])
    word = p.write_as("batch:op-1", "yes", phrase, form="batch",
                      since=ownerword.ask_ts(p.conn, "batch:op-1"))
    assert word["answer"] == "yes"


@pytest.mark.parametrize("phrase", ["all yes except runtime", "not all ok", "all yes q3", "yes"])
def test_whole_batch_form_takes_the_exact_phrase_only(p, phrase):
    p.write([typed(phrase)])
    refused(ownerword.R_UNBOUND, p.write_as, "batch:op-1", "yes", phrase, form="batch")


def test_whole_batch_phrase_in_a_later_message_is_refused(p):
    p.write([typed("hold on, one thing first"), typed("all yes")])
    refused(ownerword.R_UNBOUND, p.write_as, "batch:op-1", "yes", "all yes", form="batch",
            since=EARLY)


def test_batch_window_ends_at_until(p):
    p.write([typed("q1 yes", ts="2999-06-01T00:00:00.000Z")])
    refused(ownerword.R_UNBOUND, p.write_as, "q1", "yes", "q1 yes", until="2999-03-01T00:00:00Z")


# ---- session binding
def test_a_session_that_differs_from_the_environment_is_refused(p):
    p.write([typed("q1 yes")])
    with pytest.raises(OwnerWordRefusal) as exc:
        ownerword.human_write(p.conn, "fresh-x", "owner", subject="q1", answer="yes",
                              quote="q1 yes", root=p.root, environ=p.env)
    assert exc.value.reason == ownerword.R_SESSION


def test_a_batch_item_is_answered_only_from_the_session_that_asked(p):
    p.write([typed("q1 yes")])
    refused(ownerword.R_UNBOUND, p.write_as, "q1", "yes", "q1 yes", bound_session="another")
    assert p.write_as("q1", "yes", "q1 yes", bound_session=p.sid)["answer"] == "yes"


def test_the_captured_environment_beats_a_later_session_overwrite(p, monkeypatch):
    # cli.main captures before --session overwrites ALPACA_SESSION_ID
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "real-sid")
    ownerword.capture()
    try:
        monkeypatch.setenv("ALPACA_SESSION_ID", "cli")
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
        assert ownerword.real_session() == "real-sid"
        refused(ownerword.R_SESSION, ownerword.require_real_session, "cli")
    finally:
        ownerword.release()


def test_not_a_human_identity_is_refused(p):
    p.write([typed("q1 yes")])
    with pytest.raises(OwnerWordRefusal) as exc:
        ownerword.human_write(p.conn, p.sid, "agent", subject="q1", answer="yes", quote="q1 yes",
                              root=p.root, environ=p.env)
    assert exc.value.reason == ownerword.R_NOT_HUMAN


# ---- the transcript rules
def test_a_repointed_transcript_is_refused(p, tmp_path):
    p.write([typed("q1 yes")])
    elsewhere = tmp_path / "other.jsonl"
    elsewhere.write_text(open(p.transcript_of(p.sid)).read())
    db.patch(p.conn, "sessions", "sid", p.sid, {"transcript": str(elsewhere)})
    refused(ownerword.R_NO_TRANSCRIPT, p.write_as, "q1", "yes", "q1 yes")


def test_a_transcript_outside_the_claude_folder_is_refused(p, tmp_path):
    other = tmp_path / "elsewhere" / "projects" / ownerword.project_slug(p.root)
    other.mkdir(parents=True)
    path = other / (p.sid + ".jsonl")
    path.write_text("")
    db.patch(p.conn, "sessions", "sid", p.sid, {"transcript": str(path)})
    refused(ownerword.R_NO_TRANSCRIPT, p.write_as, "q1", "yes", "q1 yes")


def test_a_session_with_no_transcript_is_refused(p):
    db.upsert(p.conn, "sessions", "sid", {"sid": "bare", "started": "x"})
    with pytest.raises(OwnerWordRefusal) as exc:
        ownerword.find_quote(p.conn, "bare", "anything here", p.root)
    assert exc.value.reason == ownerword.R_NO_TRANSCRIPT


def test_find_quote_takes_a_substring_of_one_message_and_a_word_minimum(p):
    p.add_session("other")
    p.write([typed("when finish design phase, pause, we will eval and review")], sid="other")
    word = ownerword.find_quote(p.conn, "other", "when finish design phase, pause", p.root,
                                min_words=3)
    assert word["line"] == 1
    refused(ownerword.R_NOT_FOUND, ownerword.find_quote, p.conn, "other", "invented words here",
            p.root)
    refused(ownerword.R_UNBOUND, ownerword.find_quote, p.conn, "other", "pause", p.root,
            min_words=3)


# ---- the terminal path
@pytest.mark.parametrize("marker", ["CLAUDECODE", "CODEX_THREAD_ID", "CODEX_SANDBOX"])
def test_terminal_path_refused_with_an_agent_marker(p, marker):
    refused(ownerword.R_NOT_TERMINAL, ownerword.human_write, p.conn, "cli", "owner",
            subject="q1", answer="yes", quote=None, root=p.root, environ={marker: "1"},
            isatty=lambda: True, read=lambda prompt: "q1")


@pytest.mark.parametrize("marker", ownerword.REAL_SESSION_VARS)
def test_a_session_variable_moves_the_write_to_the_session_path(p, marker):
    # with a session in the environment the terminal path is closed, whatever the TTY says
    refused(ownerword.R_SESSION, ownerword.human_write, p.conn, "cli", "owner", subject="q1",
            answer="yes", quote="q1 yes", root=p.root, environ={marker: "real"},
            isatty=lambda: True, read=lambda prompt: "q1")


def test_terminal_path_needs_a_tty_and_the_typed_id(p):
    call = lambda **kw: ownerword.human_write(p.conn, "cli", "owner", subject="q1", answer="yes",
                                              quote=None, root=p.root, environ={}, **kw)
    refused(ownerword.R_NOT_TERMINAL, call, isatty=lambda: False, read=lambda prompt: "q1")
    refused(ownerword.R_NOT_TERMINAL, call, isatty=lambda: True, read=lambda prompt: "nope")
    word = call(isatty=lambda: True, read=lambda prompt: "q1")
    assert word["path"] == "terminal" and word["answer"] == "yes"


# ---- actors and decisions
def test_session_actor(monkeypatch):
    assert ownerword.session_actor("13de2353", environ={}) == "13de2353"
    assert ownerword.session_actor("cli", environ={}) == "human"
    assert ownerword.session_actor(None, environ={"CLAUDECODE": "1"}) == "agent"
    assert ownerword.session_actor("cli", environ={"CLAUDECODE": "1"}) == "agent"
    # a human identity named inside an agent session is never the actor
    env = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "sess-x"}
    assert ownerword.session_actor("human", environ=env) == "sess-x"
    assert ownerword.session_actor("owner", environ={"CLAUDECODE": "1"}) == "agent"


def test_project_slug_replaces_every_non_alnum():
    assert ownerword.project_slug("/data/a_b c/x.y-z") == "-data-a-b-c-x-y-z"


def test_grandfather_pointer_with_single_quotes(p):
    whole = "signed, O1 yes, O2 list is fine O3 as well"
    p.write([typed(whole)])
    ghost = ownerword.GRANDFATHER_CANDIDATES[0]
    db.append_event(p.conn, session=p.sid, actor="owner", kind="decision", ref=ghost,
                    data={"id": ghost, "pointer": "owner message 2026-10-04: '%s'" % whole})
    assert ownerword.ensure_grandfathered(p.conn, p.root, p.env) == [ghost]
    assert ownerword.decision_is_owner(p.conn, ghost, p.root, environ=p.env) is True


def _decide(p, quote=None, word=None, pointer="the owner said so"):
    return decisions.record(p.conn, "go", "ctx", ["a"], "a", "does a", pointer, actor="owner",
                            session=p.sid, root=p.root, owner_word=word)


def test_decision_is_owner_only_with_a_verified_owner_word(p):
    plain = _decide(p)
    assert ownerword.decision_is_owner(p.conn, plain["id"], p.root) is False
    did = decisions.preview_id("go", "ctx2", ["a"], "a", "does a", "ptr")
    p.ask(did)
    p.write([typed("%s yes" % did)])
    word = p.write_as(did, "yes", "%s yes" % did, since=ownerword.ask_ts(p.conn, did))
    rec = decisions.record(p.conn, "go", "ctx2", ["a"], "a", "does a", "ptr", actor="owner",
                           session=p.sid, root=p.root, owner_word=word)
    assert rec["id"] == did
    assert ownerword.decision_is_owner(p.conn, did, p.root, environ=p.env) is True
    assert ownerword.decision_words(p.conn, did, p.root, environ=p.env) == "%s yes" % did
    # the transcript no longer holds the message: the claim is not proved any more
    p.write([typed("something else")])
    assert ownerword.decision_is_owner(p.conn, did, p.root, environ=p.env) is False


def test_grandfathered_decision_is_written_once_into_meta(p):
    whole = "signed, O1 yes, O2 list is fine O3 as well"
    p.write([typed(whole)])
    ghost = ownerword.GRANDFATHER_CANDIDATES[0]
    assert ownerword.ensure_grandfathered(p.conn, p.root, p.env) == []   # no such decision yet
    db.append_event(p.conn, session=p.sid, actor="owner", kind="decision", ref=ghost,
                    data={"id": ghost, "pointer": 'the owner wrote "%s" in chat' % whole})
    assert ownerword.ensure_grandfathered(p.conn, p.root, p.env) == [ghost]
    assert db.meta_get(p.conn, ownerword.GRANDFATHER_KEY) == '["%s"]' % ghost
    assert ownerword.decision_is_owner(p.conn, ghost, p.root, environ=p.env) is True
    # a later decision whose pointer quotes a real message is not on the list
    later = _decide(p, pointer='"%s"' % whole)
    assert ownerword.decision_is_owner(p.conn, later["id"], p.root, environ=p.env) is False
