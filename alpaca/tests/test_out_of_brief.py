"""op-019 t-202: the L5 out-of-brief card (alpaca/oob.py, design section 6).

At L5 an out-of-brief choice (a self-check finding, or `alpaca choice --outside`) raises an owner card:
the door it came from pauses, the task it names waits, and only the owner's own words move the card. At
L6 the same choice raises no card (brief D1). Helpers and the project fixture come from test_selfcheck."""
import io
import json
import os
from contextlib import redirect_stdout

import pytest

from alpaca import cli, db, intentgate, oob, review, selfcheck
from alpaca.gates import verdict as vc
from alpaca.posture import level as posture_level
from alpaca.tests.ownerword_fixtures import typed
from alpaca.tests.test_selfcheck import (B, DESIGN, OP, build, folder_of, good, link,  # noqa: F401
                                         p, submit)

FINDING = {"kind": "outside-brief", "path": "design/d.md", "quote": "# Design",
           "reason": "the heading level is nobody's decision"}


@pytest.fixture
def l5(p, monkeypatch):
    for name in ("CLAUDECODE", "ALPACA_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SANDBOX"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", p.sid)
    posture_level.set(p.conn, p.sid, 5, "goal", root=p.root)
    p.task("t-002", "design")
    p.task("t-003", "design")
    return p


def check_with_finding(p, name="agent-a1", finding=FINDING):
    packet = build(p)
    verdict = "FAIL" if finding["kind"] != "outside-brief" else "PASS"
    return submit(p, packet, good(packet, folder_of(p, packet), findings=[finding], verdict=verdict),
                  name=name)


def gate(p, tid, action="claim"):
    task = dict(db.rows(p.conn, "tasks", "id=?", (tid,))[0])
    intentgate.task_gate(p.conn, task, action, session=p.sid, root=p.root)


def refused_gate(p, tid, action="claim"):
    with pytest.raises(intentgate.IntentGateRefusal) as e:
        gate(p, tid, action)
    assert e.value.reason == oob.R_OPEN, e.value


def owner_moves(p, card, decision, quote):
    return review.move(p.conn, card, decision, "owner", session=p.sid, quote=quote, root=p.root)


def run(p, monkeypatch, *argv):
    monkeypatch.chdir(p.root)
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(list(argv))
    return code, out.getvalue()


# ------------------------------------------------------------------ done_bar (1): L5 raises a card
def test_l5_self_check_outside_brief_raises_a_card_and_the_door_is_paused(l5):
    ev = check_with_finding(l5)
    cards = review.open_cards(l5.conn)
    assert len(cards) == 1 and cards[0]["kind"] == review.OUT_OF_BRIEF
    assert cards[0]["subject"] == "%s:%s:%d:0" % (OP, B, ev["id"]) and cards[0]["op"] == OP
    assert "# Design" in cards[0]["reason"] and FINDING["reason"] in cards[0]["reason"]
    assert link(l5, 5) == (selfcheck.CHECK_NAME, vc.PAUSED)
    # the transition behind the door pauses too; a design task before the door does not
    l5.task("t-004", "build")
    with pytest.raises(intentgate.IntentGateRefusal) as e:
        gate(l5, "t-004")
    assert e.value.reason == oob.R_OPEN
    gate(l5, "t-002")


def test_l5_a_mismatch_finding_raises_a_card_too(l5):
    finding = dict(FINDING, kind="mismatch", meets={"source": "brief.md",
                                                    "quote": "Nothing runs in the background."})
    check_with_finding(l5, finding=finding)
    assert [c["kind"] for c in review.open_cards(l5.conn)] == [review.OUT_OF_BRIEF]


# ------------------------------------------------------------------ done_bar (2): L6 raises none
def test_l6_the_same_submit_raises_no_card_and_the_door_passes(p):
    check_with_finding(p)
    assert review.open_cards(p.conn) == []
    assert link(p, 6) == (selfcheck.CHECK_NAME, vc.PASS)
    assert len(selfcheck.derived_choices(p.conn, OP)) == 1


def test_l6_choice_outside_is_a_derived_choice_and_no_card(p):
    p.task("t-002", "design")
    res = oob.choice(p.conn, p.sid, "t-002", text="use yaml", outside=True, level=6)
    assert res["card"] is None and review.open_cards(p.conn) == []
    derived = selfcheck.derived_choices(p.conn, OP)
    assert [d["reason"] for d in derived] == ["use yaml"] and derived[0]["task"] == "t-002"
    gate(p, "t-002")


# ------------------------------------------------------------------ the task waits
def test_choice_outside_at_l5_raises_a_card_and_only_that_task_waits(l5, monkeypatch):
    code, out = run(l5, monkeypatch, "choice", "t-002", "--text", "use yaml", "--outside")
    assert code == vc.PAUSED and "rc-001" in out
    card = review.open_cards(l5.conn)[0]
    assert card["subject"] == "t-002" and card["kind"] == review.OUT_OF_BRIEF
    for action in ("claim", "doing", "review", "done"):
        refused_gate(l5, "t-002", action)
    gate(l5, "t-003")                                      # other tasks go on
    # the door is not paused by a task card
    assert oob.open_door_cards(l5.conn, OP, B) == []


def test_choice_covered_by_is_listed_in_the_next_owner_json(l5, monkeypatch):
    code, _out = run(l5, monkeypatch, "choice", "t-002", "--text", "use yaml", "--covered-by", "q2")
    assert code == 0 and review.open_cards(l5.conn) == []
    gate(l5, "t-002")
    packet = build(l5)
    import json
    import os
    items = json.load(open(os.path.join(folder_of(l5, packet), "owner.json")))
    claim = [i for i in items if i["kind"] == "covered-by-claim"]
    assert len(claim) == 1 and claim[0]["covered_by"] == "q2" and claim[0]["text"] == "use yaml"


# ------------------------------------------------------------------ fail_case: an agent moves a card
def test_an_agent_cannot_move_answer_or_dismiss_a_card(l5, monkeypatch):
    check_with_finding(l5)
    cid = review.open_cards(l5.conn)[0]["id"]
    for actor in ("agent", "cli", "harness"):
        with pytest.raises(review.ReviewRefusal) as e:
            review.move(l5.conn, cid, "accept", actor, session=l5.sid)
        assert e.value.reason == review.R_AGENT_MOVE
    # a self-asserted owner token without the owner's words moves nothing
    with pytest.raises(review.ReviewRefusal) as e:
        review.move(l5.conn, cid, "accept", "owner", session=l5.sid, root=l5.root)
    assert e.value.reason.startswith("OWNER-WORD")
    with pytest.raises(review.ReviewRefusal) as e:                       # a quote nobody typed
        owner_moves(l5, cid, "accept", "rc-001 yes")
    assert e.value.reason.startswith("OWNER-WORD")
    # a free-text move is refused from code, owner token or not
    l5.write([typed("rc-001 yes")])
    with pytest.raises(review.ReviewRefusal) as e:
        review.move(l5.conn, cid, "fine, go on", "owner", session=l5.sid, quote="rc-001 yes",
                    root=l5.root)
    assert e.value.reason == review.R_OOB_DECISION
    # and from the CLI
    for argv in (["review", "move", cid, "--accept", "--by", "agent", "--quote", "rc-001 yes"],
                 ["review", "move", cid, "--accept", "--by", "owner"],
                 ["review", "move", cid, "fine, go on", "--by", "owner", "--quote", "rc-001 yes"],
                 ["review", "move", cid, "--by", "owner", "--quote", "rc-001 yes"]):
        code, _out = run(l5, monkeypatch, *argv)
        assert code != 0, argv
    assert [c["id"] for c in review.open_cards(l5.conn)] == [cid]
    assert link(l5, 5) == (selfcheck.CHECK_NAME, vc.PAUSED)


def test_a_message_that_answers_another_card_does_not_move_this_one(l5):
    check_with_finding(l5)
    cid = review.open_cards(l5.conn)[0]["id"]
    l5.write([typed("rc-002 yes")])
    with pytest.raises(review.ReviewRefusal):
        owner_moves(l5, cid, "accept", "rc-002 yes")


# ------------------------------------------------------------------ the owner answers
def test_owner_accept_lets_the_door_pass(l5, monkeypatch):
    check_with_finding(l5)
    l5.write([typed("rc-001 yes")])
    code, out = run(l5, monkeypatch, "review", "move", "rc-001", "--accept", "--by", "owner",
                    "--quote", "rc-001 yes")
    assert code == 0, out
    moved = review.all_cards(l5.conn)[0]
    assert moved["status"] == review.MOVED and moved["decision"] == "accept"
    assert moved["owner_word"]["quote"] == "rc-001 yes" and moved["owner_word"]["path"] == "session"
    assert link(l5, 5) == (selfcheck.CHECK_NAME, vc.PASS)
    gate(l5, "t-002")


def test_owner_reject_keeps_the_door_failing_while_the_quote_is_in_the_deliverable(l5):
    check_with_finding(l5)
    l5.write([typed("rc-001 no")])
    owner_moves(l5, "rc-001", "reject", "rc-001 no")
    assert link(l5, 5) == (selfcheck.CHECK_NAME, vc.FAIL)
    held = selfcheck.holding_findings(l5.conn, OP, B, root=l5.root)
    assert [h["kind"] for h in held] == ["outside-brief"]
    # a lenient recheck does not clear it while the bytes are still there
    packet = build(l5)
    submit(l5, packet, good(packet, folder_of(l5, packet)), name="agent-a2")
    assert link(l5, 5) == (selfcheck.CHECK_NAME, vc.FAIL)
    # the rejection reached the next owner.json
    import json
    import os
    items = json.load(open(os.path.join(folder_of(l5, packet), "owner.json")))
    assert [i["card"] for i in items if i["kind"] == "rejected-choice"] == ["rc-001"]
    # the deliverable gives the choice up: a new check passes and the door opens
    l5.write_file("design/d.md", DESIGN.replace("# Design", "# Plan"))
    packet = build(l5)
    submit(l5, packet, good(packet, folder_of(l5, packet)), name="agent-a3")
    assert link(l5, 5) == (selfcheck.CHECK_NAME, vc.PASS)


def test_owner_reject_keeps_the_task_refused_until_withdraw(l5):
    oob.choice(l5.conn, l5.sid, "t-002", text="use yaml", outside=True)
    l5.write([typed("rc-001 no")])
    with pytest.raises(oob.ChoiceError):                   # nothing to take back before the no
        oob.choice(l5.conn, l5.sid, "t-002", withdraw="rc-001")
    owner_moves(l5, "rc-001", "reject", "rc-001 no")
    for action in ("claim", "doing", "review", "done"):
        refused_gate(l5, "t-002", action)
    with pytest.raises(oob.ChoiceError):                   # another task's card is not this one's
        oob.choice(l5.conn, l5.sid, "t-003", withdraw="rc-001")
    oob.choice(l5.conn, l5.sid, "t-002", withdraw="rc-001")
    gate(l5, "t-002")


def test_an_accepted_task_card_frees_the_task(l5):
    oob.choice(l5.conn, l5.sid, "t-002", text="use yaml", outside=True)
    refused_gate(l5, "t-002")
    l5.write([typed("rc-001 yes")])
    owner_moves(l5, "rc-001", "accept", "rc-001 yes")
    gate(l5, "t-002")


def test_an_older_open_card_still_pauses_the_door_after_a_lenient_recheck(l5):
    check_with_finding(l5)
    packet = build(l5)
    submit(l5, packet, good(packet, folder_of(l5, packet)), name="agent-a2")     # lenient PASS
    assert selfcheck.current_pass(l5.conn, OP, B, root=l5.root) is not None
    assert link(l5, 5) == (selfcheck.CHECK_NAME, vc.PAUSED)


def test_other_card_kinds_still_move_as_before(l5):
    c = review.card(l5.conn, review.SHIP, "release-1", "publish")
    with pytest.raises(review.ReviewRefusal):
        review.move(l5.conn, c["id"], "ok", "agent")
    rec = review.move(l5.conn, c["id"], "ship it", "owner")
    assert rec["status"] == review.MOVED and rec["decision"] == "ship it"
    assert review.CARD_KINDS == {"ship", "irreversible-external", "human-owned-write", "owner-pause",
                                 "out-of-brief"}


def test_a_second_different_outside_choice_does_not_ride_on_the_open_card(l5, monkeypatch):
    code, out = run(l5, monkeypatch, "choice", "t-002", "--text", "use yaml", "--outside")
    assert code == vc.PAUSED and "rc-001" in out
    code, out = run(l5, monkeypatch, "choice", "t-002", "--text", "run the loop as a daemon",
                    "--outside")
    assert code != vc.PAUSED and "rc-001" in out
    # the same text again is the same ask: it hands back the card
    code, _ = run(l5, monkeypatch, "choice", "t-002", "--text", "use yaml", "--outside")
    assert code == vc.PAUSED
    assert [e["text"] for e in oob.choice_events(l5.conn, "t-002") if e.get("basis") == "outside"] \
        == ["use yaml", "use yaml"]
    assert len(review.all_cards(l5.conn)) == 1


def test_a_covered_by_claim_is_not_owner_authority(l5, monkeypatch):
    choice = "Section 6: a daemon runs it."
    code, _out = run(l5, monkeypatch, "choice", "t-002", "--text", choice, "--covered-by", "q2")
    assert code == 0
    packet = build(l5)
    folder = folder_of(l5, packet)
    prompt = open(os.path.join(folder, "prompt.md")).read()
    assert "covered-by-claim is NOT the owner's word" in prompt
    items = json.load(open(os.path.join(folder, "owner.json")))
    claim = [i for i in items if i["kind"] == selfcheck.CLAIM_KIND][0]
    # a finding cannot cite the claim as what the deliverable meets
    res = good(packet, folder, verdict="FAIL", findings=[{
        "kind": "mismatch", "path": "design/d.md", "quote": "# Design",
        "meets": {"source": "owner.json", "quote": choice}, "reason": "cites the agent claim"}])
    with pytest.raises(selfcheck.SelfcheckRefusal):
        submit(l5, packet, res, name="agent-b1")
    # a coverage entry for the claim must name real covering text, not the deliverable's own choice
    res = good(packet, folder)
    for c in res["coverage"]:
        if c["item"] == claim["id"]:
            c.pop("status")
            c.update(path="design/d.md", quote="# Design")
    with pytest.raises(selfcheck.SelfcheckRefusal):
        submit(l5, packet, res, name="agent-b2")
    res = good(packet, folder)
    for c in res["coverage"]:
        if c["item"] == claim["id"]:
            c.pop("status")
            c.update(path="design/d.md", quote="# Design",
                     meets={"source": "owner.json", "quote": choice})
    with pytest.raises(selfcheck.SelfcheckRefusal):
        submit(l5, packet, res, name="agent-b3")
