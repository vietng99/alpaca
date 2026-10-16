"""op-019 t-200 (design 3.2 and 3.3): the start-of-op question batch and the start gate.

Each done_bar line of the task has a test here that fails without alpaca/batch.py:
  (1) the batch covers the six classes and every plan question;
  (2) an unanswered item makes an L5 or L6 start refuse, naming the item;
  (3) existing signed interviews stay valid (alpaca/tests/test_interview.py, run unchanged).
The three design-check residuals (design D-20) are S1, S2 and S3 below.
"""
import io
import json
import os
from contextlib import redirect_stdout

import pytest

from alpaca import batch, brief, cli, db, intentgate, interview, ops, ownerword, pad, review, selfcheck
from alpaca.posture import level as posture_level
from alpaca.tests.conftest import REPO
from alpaca.tests.ownerword_fixtures import Project, typed

SIX = ["runtime", "credentials", "sandbox", "spend", "external", "deleting"]
PLAN = """# Plan

- Should the runner use a container? It needs a sandbox. Which account pays for it?
1. Is the cache cleared on deploy? Yes. "Can two runs share a folder?"
Plain statement with no question.
Is the cache cleared on deploy?
"""


@pytest.fixture
def p(tmp_path, monkeypatch):
    proj = Project(tmp_path, monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", proj.sid)
    monkeypatch.chdir(proj.root)
    return proj


def run(p, *argv):
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(list(argv))
    return code, out.getvalue()


def new_op(p, intent="the work"):
    code, out = run(p, "op", "new", intent, "--done-when", "done")
    assert code == 0, out
    return out.split()[0]


def propose_all(p, op, only=None):
    for name in batch.items(p.conn, op):
        if only is None or name in only:
            batch.propose(p.conn, op, name, "proposal for %s" % name, session=p.sid)


def sign_brief(p, op):
    db.append_event(p.conn, session=p.sid, actor="human", kind=brief.SIGNED_KIND, op=op,
                    ref="abc", data={"op": op, "file": "brief.md", "sha256": "abc"})


def task(p, op, phase="build"):
    return ops.add_task(p.conn, op, "do it", "Do it", phase=phase, session=p.sid)


def level(p, n, sid=None):
    posture_level.set(p.conn, sid or p.sid, n, "goal", root=p.root)


# ------------------------------------------------------------------ done_bar (1): the items
def test_the_six_classes_come_from_the_data_file(p):
    assert [c[0] for c in batch.class_items()] == SIX
    assert all(fills and becomes for _n, fills, becomes in batch.class_items())
    assert interview.set_file("intent").endswith("templates/interview/intent-slots.yaml")
    assert "intent" in interview.sets()


def test_op_new_opens_the_batch_with_the_six_classes(p):
    op = new_op(p)
    assert batch.has_batch(p.conn, op)
    state = batch.items(p.conn, op)
    assert list(state) == SIX and {s["state"] for s in state.values()} == {batch.OPEN}
    assert not any(s["late"] for s in state.values())


def test_plan_questions_are_every_sentence_ending_with_a_question_mark(p):
    assert batch.extract_questions(PLAN) == [
        "Should the runner use a container?", "Which account pays for it?",
        "Is the cache cleared on deploy?", "Can two runs share a folder?"]
    op = new_op(p)
    plan = os.path.join(p.root, "plan.md")
    open(plan, "w").write(PLAN)
    code, out = run(p, "batch", "open", op, "--plan", plan, "--question", "Who signs off?")
    assert code == 0, out
    state = batch.items(p.conn, op)
    assert list(state) == SIX + ["q1", "q2", "q3", "q4", "q5"]
    assert state["q2"]["question"] == "Which account pays for it?"
    assert state["q5"]["question"] == "Who signs off?"
    run(p, "batch", "open", op, "--plan", plan)                      # a repeat adds nothing
    assert len(batch.items(p.conn, op)) == 11


# ------------------------------------------------------------------ states and the proposal
def test_an_agent_proposal_is_not_an_owner_answer(p):
    op = new_op(p)
    propose_all(p, op)
    state = batch.items(p.conn, op)
    assert {s["state"] for s in state.values()} == {batch.PROPOSED}
    assert [r for r, _d in batch.check(p.conn, op)] == [batch.R_UNANSWERED, batch.R_BRIEF]
    assert batch.owner_items(p.conn, op) == []
    code, out = run(p, "batch", "check", op)
    assert code == cli.FAIL and "BATCH-UNANSWERED" in out and "runtime" in out


def test_propose_is_refused_on_an_answered_item(p):
    op = new_op(p)
    propose_all(p, op, ["runtime"])
    run(p, "batch", "show", op)
    p.write([typed("runtime yes")])
    code, out = run(p, "batch", "answer", op, "runtime", "--yes", "--by", "owner",
                    "--quote", "runtime yes")
    assert code == 0, out
    with pytest.raises(batch.BatchError):
        batch.propose(p.conn, op, "runtime", "again", session=p.sid)


def test_show_prints_every_item_the_proposal_mark_and_the_forms_and_records_the_ask(p):
    op = new_op(p)
    plan = os.path.join(p.root, "plan.md")
    open(plan, "w").write("Which account pays?\n")
    run(p, "batch", "open", op, "--plan", plan)
    propose_all(p, op, ["runtime"])
    code, out = run(p, "batch", "show", op)
    assert code == 0
    for name in SIX + ["q1"]:
        assert "[%s]" % name in out
    assert "Which account pays?" in out
    assert "PROPOSAL (agent, not an answer): proposal for runtime" in out
    assert "NO PROPOSAL" in out and "`all yes` is not available" in out
    assert "<id> yes" in out and "<id> no" in out and "<id>: <answer>" in out and "all yes" in out
    ask = ownerword.latest_ask(p.conn, "batch:" + op)
    assert ask["session"] == p.sid and ask["op"] == op
    assert json.loads(ask["data"])["items"]["runtime"]["proposal"] == "proposal for runtime"
    code, out = run(p, "batch", "show", op, "--json")
    assert [i["id"] for i in json.loads(out)["items"]][:2] == ["runtime", "credentials"]


def test_show_refuses_a_session_that_differs_from_the_environment(p):
    op = new_op(p)
    code, out = run(p, "--session", "fresh-x", "batch", "show", op)
    assert code == cli.FAIL and "OWNER-WORD-SESSION" in out
    assert ownerword.latest_ask(p.conn, "batch:" + op) is None


# ------------------------------------------------------------------ answers
def test_one_reply_answers_items_in_the_id_form(p):
    op = new_op(p)
    propose_all(p, op, ["runtime", "credentials"])
    run(p, "batch", "show", op)
    reply = "runtime yes, credentials: only the deploy key, sandbox no, spend: nothing"
    p.write([typed(reply)])
    code, out = run(p, "batch", "answer", op, "--reply", "--by", "owner", "--quote", reply)
    assert code == 0, out
    state = batch.items(p.conn, op)
    assert state["runtime"]["state"] == batch.ANSWERED
    assert state["runtime"]["answer"] == "proposal for runtime"        # what the owner was shown
    assert state["credentials"]["answer"] == "only the deploy key"
    assert state["spend"]["answer"] == "nothing"
    assert state["sandbox"]["state"] == batch.ANSWERED and state["sandbox"]["answer"] == "no"
    assert state["external"]["state"] == batch.OPEN
    assert state["runtime"]["owner_word"]["path"] == "session"
    assert "external" in out and "deleting" in out


def test_a_no_to_a_proposal_leaves_the_item_open(p):
    op = new_op(p)
    propose_all(p, op, ["runtime"])
    run(p, "batch", "show", op)
    p.write([typed("runtime no")])
    code, out = run(p, "batch", "answer", op, "runtime", "--no", "--by", "owner",
                    "--quote", "runtime no")
    assert code == 0, out
    s = batch.items(p.conn, op)["runtime"]
    assert s["state"] == batch.OPEN and s["rejected"] == "proposal for runtime"
    assert "proposal" not in s


def test_the_whole_batch_form_records_every_proposal_as_shown(p):
    op = new_op(p)
    propose_all(p, op)
    run(p, "batch", "show", op)
    p.write([typed("All yes.")])
    code, out = run(p, "batch", "answer", op, "--all-yes", "--by", "owner", "--quote", "All yes.")
    assert code == 0, out
    state = batch.items(p.conn, op)
    assert {s["state"] for s in state.values()} == {batch.ANSWERED}
    assert state["spend"]["answer"] == "proposal for spend"
    assert len(batch.owner_items(p.conn, op)) == 6


# S1: the whole-batch form accepts only the exact phrase
@pytest.mark.parametrize("message", ["all yes except runtime", "not all ok", "yes to all but spend",
                                     "all yes, runtime: no", "ok"])
def test_s1_the_whole_batch_form_answers_nothing_but_the_exact_phrase(p, message):
    op = new_op(p)
    propose_all(p, op)
    run(p, "batch", "show", op)
    p.write([typed(message)])
    code, out = run(p, "batch", "answer", op, "--all-yes", "--by", "owner", "--quote", message)
    assert code == cli.FAIL and "OWNER-WORD-" in out
    assert {s["state"] for s in batch.items(p.conn, op).values()} == {batch.PROPOSED}


def test_the_whole_batch_phrase_counts_only_in_the_first_message_after_the_ask(p):
    op = new_op(p)
    propose_all(p, op)
    run(p, "batch", "show", op)
    p.write([typed("hold on, one question"), typed("all yes")])
    code, out = run(p, "batch", "answer", op, "--all-yes", "--by", "owner", "--quote", "all yes")
    assert code == cli.FAIL and "OWNER-WORD-UNBOUND" in out


def test_all_yes_is_refused_when_an_item_had_no_proposal(p):
    op = new_op(p)
    propose_all(p, op, ["runtime"])
    run(p, "batch", "show", op)
    p.write([typed("all yes")])
    code, out = run(p, "batch", "answer", op, "--all-yes", "--by", "owner", "--quote", "all yes")
    assert code == cli.FAIL and "no proposal" in out
    assert batch.items(p.conn, op)["runtime"]["state"] == batch.PROPOSED


def test_a_re_proposed_item_refuses_a_yes(p):
    op = new_op(p)
    propose_all(p, op, ["runtime", "spend"])
    run(p, "batch", "show", op)
    batch.propose(p.conn, op, "runtime", "a different plan", session=p.sid)   # after the ask
    p.write([typed("runtime yes"), typed("all yes")])
    code, out = run(p, "batch", "answer", op, "runtime", "--yes", "--by", "owner",
                    "--quote", "runtime yes")
    assert code == cli.FAIL and "OWNER-WORD-UNBOUND" in out and "proposed again" in out
    assert batch.items(p.conn, op)["runtime"]["state"] == batch.PROPOSED
    # the whole-batch form refuses as a whole when one item was proposed again
    code, out = run(p, "batch", "answer", op, "--all-yes", "--by", "owner", "--quote", "all yes")
    assert code == cli.FAIL and "proposed again" in out
    # shown again, the owner can say yes to the new text
    run(p, "batch", "show", op)
    p.write([typed("runtime yes")])
    code, out = run(p, "batch", "answer", op, "runtime", "--yes", "--by", "owner",
                    "--quote", "runtime yes")
    assert code == 0, out
    assert batch.items(p.conn, op)["runtime"]["answer"] == "a different plan"


def test_an_unrelated_owner_reply_answers_nothing(p):
    op = new_op(p)
    propose_all(p, op)
    run(p, "batch", "show", op)
    for reply in ("looks good, carry on", "runtime ok but not headless", "what is the spend cap?"):
        p.write([typed(reply)])
        for extra in (["runtime", "--yes"], ["--reply"]):
            code, out = run(p, "batch", "answer", op, *extra, "--by", "owner", "--quote", reply)
            assert code == cli.FAIL and "OWNER-WORD-UNBOUND" in out, (reply, out)
    assert {s["state"] for s in batch.items(p.conn, op).values()} == {batch.PROPOSED}


def test_an_answer_needs_the_asking_session_and_the_window(p):
    op = new_op(p)
    propose_all(p, op, ["runtime"])
    run(p, "batch", "show", op)
    p.write([typed("runtime yes", ts="2000-01-01T00:00:00.000Z")])         # before the ask
    code, out = run(p, "batch", "answer", op, "runtime", "--yes", "--by", "owner",
                    "--quote", "runtime yes")
    assert code == cli.FAIL and "OWNER-WORD-UNBOUND" in out
    p.write([typed("runtime yes")])
    ownerword.record_ask(p.conn, p.sid, "ord-9", "another op?", op="op-77")   # the session moved on
    code, out = run(p, "batch", "answer", op, "runtime", "--yes", "--by", "owner",
                    "--quote", "runtime yes")
    assert code == cli.FAIL and "OWNER-WORD-UNBOUND" in out
    run(p, "batch", "show", op)                                              # asked again
    code, out = run(p, "batch", "answer", op, "runtime", "--yes", "--by", "owner",
                    "--quote", "runtime yes")
    assert code == 0, out
    # another session cannot answer: the batch item id is bound to the asking session
    p.add_session("other")
    p.write([typed("credentials: any")], sid="other")
    with pytest.raises(ownerword.OwnerWordRefusal) as exc:
        batch.answer(p.conn, op, "credentials", session="other", by="owner",
                     quote="credentials: any", root=p.root, kind="text", text="any",
                     environ={"CLAUDE_CONFIG_DIR": p.cfg, "CLAUDE_CODE_SESSION_ID": "other"})
    assert exc.value.reason == ownerword.R_UNBOUND


def test_the_owner_is_a_human_identity_and_the_session_is_the_real_one(p):
    op = new_op(p)
    propose_all(p, op, ["runtime"])
    run(p, "batch", "show", op)
    p.write([typed("runtime yes")])
    code, out = run(p, "batch", "answer", op, "runtime", "--yes", "--by", "agent",
                    "--quote", "runtime yes")
    assert code == cli.FAIL and "OWNER-WORD-NOT-HUMAN" in out
    code, out = run(p, "--session", "fresh-x", "batch", "answer", op, "runtime", "--yes",
                    "--by", "owner", "--quote", "runtime yes")
    assert code == cli.FAIL and "OWNER-WORD-SESSION" in out


def answer_everything(p, op):
    propose_all(p, op)
    run(p, "batch", "show", op)
    p.write([typed("all yes")])
    code, out = run(p, "batch", "answer", op, "--all-yes", "--by", "owner", "--quote", "all yes")
    assert code == 0, out


def test_check_passes_when_every_item_is_answered_and_the_brief_is_signed(p):
    op = new_op(p)
    answer_everything(p, op)
    code, out = run(p, "batch", "check", op)
    assert code == cli.FAIL and "BRIEF-UNSIGNED" in out and "BATCH-UNANSWERED" not in out
    sign_brief(p, op)
    code, out = run(p, "batch", "check", op)
    assert code == 0 and "PASS" in out


# ------------------------------------------------------------------ done_bar (2): the start gate
def test_l6_claim_and_move_are_refused_naming_the_items(p):
    op = new_op(p)
    t = task(p, op)
    level(p, 6)
    for argv in (["task", "claim", t, "--by", "w"], ["task", "move", t, "doing"],
                 ["task", "move", t, "done"]):
        code, out = run(p, *argv)
        assert code == cli.FAIL, (argv, out)
        assert "BATCH-UNANSWERED" in out, out
        for name in SIX:
            assert name in out
    # the CLI has no `review` status; the gate itself refuses that action too
    row = dict(db.rows(p.conn, "tasks", "id=?", (t,))[0])
    with pytest.raises(intentgate.IntentGateRefusal) as exc:
        intentgate.task_gate(p.conn, row, "review", session=p.sid, root=p.root)
    assert exc.value.reason == "BATCH-UNANSWERED" and "runtime" in exc.value.detail
    assert batch.cleared_event(p.conn, op) is None


def test_l5_is_gated_and_l4_is_not(p):
    op = new_op(p)
    t = task(p, op)
    level(p, 4)
    code, out = run(p, "task", "move", t, "doing")
    assert code == 0, out
    level(p, 5)
    code, out = run(p, "task", "move", t, "done")
    assert code == cli.FAIL and "BATCH-UNANSWERED" in out


def test_the_gate_reads_the_environment_session_even_with_session_fresh_x(p):
    op = new_op(p)
    t = task(p, op)
    level(p, 6)                                    # p.sid is the real session
    code, out = run(p, "--session", "fresh-x", "task", "claim", t, "--by", "w")
    assert code == cli.FAIL and "BATCH-UNANSWERED" in out


def test_an_answered_batch_without_a_brief_still_refuses(p):
    op = new_op(p)
    t = task(p, op)
    level(p, 6)
    answer_everything(p, op)
    code, out = run(p, "task", "claim", t, "--by", "w")
    assert code == cli.FAIL and "BRIEF-UNSIGNED" in out


def test_batch_cleared_lets_later_claims_through(p):
    op = new_op(p)
    # two design tasks: no boundary with tasks lies before the design phase, so only the batch gate
    # speaks here (the door self-check of later phases is tested in test_selfcheck.py)
    t1, t2 = task(p, op, "design"), task(p, op, "design")
    level(p, 6)
    answer_everything(p, op)
    sign_brief(p, op)
    code, out = run(p, "task", "claim", t1, "--by", "w")
    assert code == 0, out
    assert batch.cleared_event(p.conn, op) is not None
    code, out = run(p, "task", "claim", t2, "--by", "w")
    assert code == 0, out
    assert p.conn.execute("SELECT COUNT(*) FROM events WHERE kind='batch-cleared'").fetchone()[0] == 1


def test_a_late_item_at_l6_is_derived_with_its_reason(p):
    op = new_op(p)
    t1, t2 = task(p, op), task(p, op)
    level(p, 6)
    answer_everything(p, op)
    sign_brief(p, op)
    assert run(p, "task", "claim", t1, "--by", "w")[0] == 0
    code, out = run(p, "batch", "open", op, "--question", "Which locale does it use?")
    assert code == 0 and batch.items(p.conn, op)["q1"]["late"] is True
    code, out = run(p, "task", "claim", t2, "--by", "w")
    assert code == cli.FAIL and "BATCH-LATE-ITEM" in out and "q1" in out
    code, out = run(p, "batch", "derive", op, "q1", "--text", "en-US", "--reason", "the brief is English")
    assert code == 0, out
    s = batch.items(p.conn, op)["q1"]
    assert s["state"] == batch.ANSWERED and s["source"] == "derived" and s["reason"]
    assert run(p, "task", "claim", t2, "--by", "w")[0] == 0
    assert len(batch.owner_items(p.conn, op)) == 6                    # the derived item is not the owner's
    rows = list(selfcheck.derived_choices(p.conn, op))
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "batch-derive" and r["item"] == "q1" and r["answer"] == "en-US"
    assert r["reason"] == "the brief is English" and r["level"] == 6
    assert r["question"] == "Which locale does it use?" and r["batch_event"] == s["event"]
    ev = p.conn.execute("SELECT op, ref FROM events WHERE kind='derived-choice'").fetchone()
    assert (ev["op"], ev["ref"]) == (op, op + ":q1")
    assert selfcheck.derived_count(p.conn, op) == 1
    code, out = run(p, "selfcheck", "derived", "--op", op)
    assert code == 0 and "q1" in out and "en-US" in out and "the brief is English" in out
    assert any("derived choices: 1" in l for l in pad.resume_point_lines(p.conn, [op]))


def test_a_derived_choice_refuses_a_start_item_a_missing_reason_and_a_lower_level(p):
    op = new_op(p)
    t = task(p, op)
    level(p, 6)
    code, out = run(p, "batch", "derive", op, "runtime", "--text", "x", "--reason", "y")
    assert code == cli.FAIL and "BATCH-NOT-LATE" in out
    assert selfcheck.derived_count(p.conn, op) == 0
    answer_everything(p, op)
    sign_brief(p, op)
    run(p, "task", "claim", t, "--by", "w")
    run(p, "batch", "open", op, "--question", "One more?")
    code, out = run(p, "batch", "derive", op, "q1", "--text", "x", "--reason", " ")
    assert code == cli.FAIL and "BATCH-REASON" in out
    assert selfcheck.derived_count(p.conn, op) == 0
    code, out = run(p, "batch", "derive", op, "q9", "--text", "x", "--reason", "y")
    assert code == cli.FAIL and "BATCH-ITEM-UNKNOWN" in out
    assert selfcheck.derived_count(p.conn, op) == 0
    level(p, 5)
    code, out = run(p, "batch", "derive", op, "q1", "--text", "x", "--reason", "y")
    assert code == cli.FAIL and "BATCH-DERIVE-LEVEL" in out
    assert batch.items(p.conn, op)["q1"]["state"] == batch.OPEN
    assert selfcheck.derived_count(p.conn, op) == 0


def _late_q1(p):
    op = new_op(p)
    t1, t2 = task(p, op), task(p, op)
    level(p, 6)
    answer_everything(p, op)
    sign_brief(p, op)
    assert run(p, "task", "claim", t1, "--by", "w")[0] == 0
    assert run(p, "batch", "open", op, "--question", "Which locale does it use?")[0] == 0
    return op, t2


def test_a_second_derive_of_the_same_item_is_refused_and_writes_no_second_choice(p):
    op, _ = _late_q1(p)
    assert run(p, "batch", "derive", op, "q1", "--text", "en-US", "--reason", "r")[0] == 0
    code, out = run(p, "batch", "derive", op, "q1", "--text", "fr", "--reason", "r2")
    assert code == cli.FAIL and "BATCH-ITEM-ANSWERED" in out
    assert selfcheck.derived_count(p.conn, op) == 1
    n = p.conn.execute("SELECT COUNT(*) FROM events WHERE kind=? AND ref='q1' AND "
                       "json_extract(data,'$.state')='answered'", (batch.ITEM_KIND,)).fetchone()[0]
    assert n == 1


def test_a_failed_derived_choice_write_rolls_back_the_answer(p, monkeypatch):
    op, t2 = _late_q1(p)
    real = db.append_event

    def boom(conn, **kw):
        if kw.get("kind") == selfcheck.DERIVED_KIND:
            raise RuntimeError("write failed")
        return real(conn, **kw)
    monkeypatch.setattr(db, "append_event", boom)
    with pytest.raises(RuntimeError):
        batch.derive(p.conn, op, "q1", "en-US", "r", session=p.sid, root=p.root)
    monkeypatch.setattr(db, "append_event", real)
    assert batch.items(p.conn, op)["q1"]["state"] == batch.OPEN
    assert selfcheck.derived_count(p.conn, op) == 0
    assert db.verify_chain(p.conn)[0] is True
    code, out = run(p, "task", "claim", t2, "--by", "w")
    assert code == cli.FAIL and "BATCH-LATE-ITEM" in out


def test_a_late_item_at_l5_raises_an_out_of_brief_card(p):
    op = new_op(p)
    t1, t2 = task(p, op), task(p, op)
    level(p, 5)
    answer_everything(p, op)
    sign_brief(p, op)
    assert run(p, "task", "claim", t1, "--by", "w")[0] == 0
    assert review.OUT_OF_BRIEF == "out-of-brief" and review.OUT_OF_BRIEF in review.CARD_KINDS
    assert review.open_cards(p.conn) == []
    run(p, "batch", "open", op, "--question", "Which locale does it use?")
    code, out = run(p, "task", "claim", t2, "--by", "w")
    assert code == 0, out
    cards = review.open_cards(p.conn)
    assert [(c["kind"], c["subject"], c["op"]) for c in cards] == [("out-of-brief", op + ":q1", op)]
    run(p, "task", "move", t2, "doing")                               # a repeat adds no second card
    assert len(review.open_cards(p.conn)) == 1


def test_owner_items_return_the_owner_answers(p):
    op = new_op(p)
    assert batch.owner_items(p.conn, op) == []
    answer_everything(p, op)
    got = batch.owner_items(p.conn, op)
    assert [g["id"] for g in got] == SIX and got[0]["kind"] == "batch-answer"
    assert got[0]["quote"] == "all yes" and got[0]["answer"] == "proposal for runtime"
    assert intentgate.owner_items(p.conn, op, root=p.root)[0]["id"] == "runtime"


# ------------------------------------------------------------------ D-19 and S2: phases
def test_s2_add_task_needs_a_ladder_phase_on_an_op_with_a_batch(p):
    op = new_op(p)
    for phase, token in ((None, "TASK-PHASE-REQUIRED"), ("  ", "TASK-PHASE-REQUIRED"),
                         ("impl", "TASK-PHASE-UNKNOWN")):
        with pytest.raises(ValueError) as exc:
            ops.add_task(p.conn, op, "s", "t", phase=phase, session=p.sid)
        assert token in str(exc.value)
    for phase in ("requirement", "design", "build", "verify", "release"):
        assert ops.add_task(p.conn, op, "s", "t", phase=phase, session=p.sid)
    code, out = run(p, "task", "add", op, "do it", "--title", "T")
    assert code == cli.FAIL and "TASK-PHASE-REQUIRED" in out
    code, out = run(p, "task", "add", op, "do it", "--title", "T", "--phase", "impl")
    assert code == cli.FAIL and "TASK-PHASE-UNKNOWN" in out
    code, out = run(p, "task", "add", op, "do it", "--title", "T", "--phase", "build")
    assert code == 0, out
    assert p.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 6


def test_s2_an_intake_made_op_gets_phases(project, capsys):
    import shutil
    from alpaca import taskcontract
    dst = os.path.join(project, "domain")
    shutil.copytree(os.path.join(REPO, "alpaca", "tests", "fixtures", "runbook-example-v1"), dst)
    rc = cli.main(["op", "new", "Link shortener", "--done-when", "every row proven"])
    capsys.readouterr()
    assert rc == 0
    rc = cli.main(["intake", os.path.join(dst, "spec.md"), os.path.join(dst, "runbook.yaml")])
    out = capsys.readouterr().out
    assert rc == 0, out
    conn = db.connect(project)
    assert batch.has_batch(conn, "op-001")
    phases = {r["id"]: r["phase"] for r in db.rows(conn, "tasks", "op=?", ("op-001",))}
    assert phases and set(phases.values()) <= {"build", "verify"}
    assert "build" in phases.values()


def test_a_task_of_an_op_without_a_batch_needs_no_phase(p):
    op = p.op()                                                       # opened without a batch
    assert not batch.has_batch(p.conn, op)
    assert ops.add_task(p.conn, op, "s", "t", phase=None, session=p.sid)


# ------------------------------------------------------------------ S3: research-opened ops
def test_s3_an_op_opened_through_open_op_has_no_batch_and_is_not_gated_at_l6(p):
    op = ops.open_op(p.conn, "a research run", "its report", session=p.sid, actor="human")
    assert not batch.has_batch(p.conn, op)
    t = ops.add_task(p.conn, op, "s", "t", phase=None, session=p.sid)
    level(p, 6)
    for argv in (["task", "claim", t, "--by", "w"], ["task", "move", t, "doing"]):
        code, out = run(p, *argv)
        assert code == 0, (argv, out)
    code, out = run(p, "task", "move", t, "done")                     # stops at the proof gate only
    assert "BATCH" not in out and "BRIEF-UNSIGNED" not in out
    assert batch.cleared_event(p.conn, op) is None


def test_the_research_runner_opens_its_op_through_open_op():
    src = open(os.path.join(REPO, "alpaca", "research", "runner.py"), encoding="utf-8").read()
    assert "ops.open_op(" in src and "open_batch" not in src and "batch.open" not in src


# ------------------------------------------------------------------ done_bar (3): interviews
def test_the_intent_set_is_an_optional_interview_slot_set(project, capsys):
    before = interview.slot_ids(project)
    assert not set(SIX) & set(before)
    rc = cli.main(["interview", "use", "intent"])
    capsys.readouterr()
    assert rc == 0
    assert interview.slot_ids(project) == before + SIX


# ------------------------------------------------------------------ fix round 1
def test_a_class_word_inside_an_answer_does_not_cut_or_flip_it(p):
    op = new_op(p)
    run(p, "batch", "show", op)
    reply = "sandbox: only the repo folder and the runtime cache, spend: nothing"
    p.write([typed(reply)])
    code, out = run(p, "batch", "answer", op, "--reply", "--by", "owner", "--quote", reply)
    assert code == 0, out
    state = batch.items(p.conn, op)
    assert state["sandbox"]["answer"] == "only the repo folder and the runtime cache"
    assert state["spend"]["answer"] == "nothing"
    assert state["runtime"]["state"] == batch.OPEN


def test_an_answer_that_opens_with_no_is_text_not_a_refusal(p):
    op = new_op(p)
    run(p, "batch", "show", op)
    reply = "sandbox: no external network, repo folder only"
    p.write([typed(reply)])
    code, out = run(p, "batch", "answer", op, "--reply", "--by", "owner", "--quote", reply)
    assert code == 0, out
    s = batch.items(p.conn, op)["sandbox"]
    assert s["answer"] == "no external network, repo folder only" and s["answer_kind"] == "text"
    assert batch.items(p.conn, op)["external"]["state"] == batch.OPEN


def test_the_sandbox_proposal_and_answer_are_read_back_to_the_owner(p):
    """op-059 t-695 round 3: the batch text and the answer's confirmation show how the write guard reads
    the sandbox answer; another item's answer prints no sandbox reading. Round 4: the reading is a
    proposal, never enforced by itself; it prints the exact `alpaca sandbox set` command to confirm."""
    op = new_op(p)
    batch.propose(p.conn, op, "sandbox", "Writes stay in this project root and the session scratchpad; "
                  "no sudo", session=p.sid)
    code, out = run(p, "batch", "show", op)
    assert code == 0, out
    shown = out.split("[sandbox]")[1].split("[spend]")[0]
    assert "PROPOSAL" in shown and "the write guard's proposal" in shown and "NOT ENFORCED" in shown
    assert "no sudo" in shown
    assert "alpaca sandbox set %s --path root --path scratchpad --by owner --quote" % op in shown
    p.write([typed("sandbox yes")])
    code, out = run(p, "batch", "answer", op, "sandbox", "--yes", "--by", "owner", "--quote", "sandbox yes")
    assert code == 0, out
    assert "the write guard's proposal" in out and "NOT ENFORCED" in out and "no sudo" in out
    assert "alpaca sandbox set %s --path root --path scratchpad" % op in out
    p.write([typed("spend: nothing")])
    code, out = run(p, "batch", "answer", op, "spend", "--text", "nothing", "--by", "owner",
                    "--quote", "spend: nothing")
    assert code == 0, out
    assert "write guard" not in out
    again = run(p, "batch", "show", op)[1]
    assert "ANSWERED" in again.split("[sandbox]")[1].split("[spend]")[0] and "ENFORCED" in again


@pytest.mark.parametrize("reply", ["runtime yes, but run it headless",
                                   "runtime yes, though only on weekdays"])
def test_a_qualified_yes_answers_nothing(p, reply):
    op = new_op(p)
    propose_all(p, op, ["runtime"])
    run(p, "batch", "show", op)
    p.write([typed(reply)])
    for extra in (["--reply"], ["runtime", "--yes"]):
        code, out = run(p, "batch", "answer", op, *extra, "--by", "owner", "--quote", reply)
        assert code == cli.FAIL and "OWNER-WORD-UNBOUND" in out, out
    assert batch.items(p.conn, op)["runtime"]["state"] == batch.PROPOSED


def test_an_ask_about_a_task_or_free_text_of_the_same_op_keeps_the_window_open(p):
    op = new_op(p)
    propose_all(p, op, ["runtime"])
    run(p, "batch", "show", op)
    tid = task(p, op)
    ownerword.record_ask(p.conn, p.sid, tid, "ship it?")                  # a task of this op
    ownerword.record_ask(p.conn, p.sid, "a free-text question", "which colour?")
    p.write([typed("runtime yes")])
    code, out = run(p, "batch", "answer", op, "runtime", "--yes", "--by", "owner",
                    "--quote", "runtime yes")
    assert code == 0, out


def test_an_ask_about_a_task_of_another_op_closes_the_window(p):
    op = new_op(p)
    other = new_op(p, "another")
    propose_all(p, op, ["runtime"])
    run(p, "batch", "show", op)
    ownerword.record_ask(p.conn, p.sid, task(p, other), "ship it?")
    p.write([typed("runtime yes")])
    code, out = run(p, "batch", "answer", op, "runtime", "--yes", "--by", "owner",
                    "--quote", "runtime yes")
    assert code == cli.FAIL and "OWNER-WORD-UNBOUND" in out


def test_the_research_engine_files_its_tasks_under_a_ladder_phase_on_a_batch_op(p):
    op = new_op(p)
    with pytest.raises(ValueError) as exc:                       # no exemption for an `extra` key
        ops.add_task(p.conn, op, "attempt", "Attempt", phase=None, session=p.sid,
                     extra={"research": {"role": "attempt"}})
    assert "TASK-PHASE-REQUIRED" in str(exc.value)
    assert batch.ladder_phase(p.conn, op, "build") == "build"
    bare = ops.open_op(p.conn, "bare", "done", session=p.sid, actor="human")
    assert batch.ladder_phase(p.conn, bare, "build") is None


def test_the_release_smoke_passes_a_phase_to_task_add():
    text = open(os.path.join(REPO, "setup", "release_checks.py"), encoding="utf-8").read()
    assert "'--phase', 'build'" in text
