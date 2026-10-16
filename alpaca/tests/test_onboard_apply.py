"""`onboard apply` (op-062 t-656): configure from the confirmed card's recorded inputs, step by step, once."""
import hashlib
import json
import os

import pytest

from alpaca import adopt, onboard_flow, project
from alpaca.attach import config
from alpaca.tests.attachkit import git, upstream  # noqa: F401
from alpaca.tests.onboardchat import chat  # noqa: F401


@pytest.fixture
def confirmed(chat, upstream):                                                              # noqa: F811
    cid, card = chat.walk_to_card(upstream.url)
    chat.say("yes")
    rc, out = chat.run("onboard", "confirm", cid, "--quote", "yes")
    assert rc == 0, out
    chat.cid = cid
    chat.upstream = upstream
    return chat


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def test_apply_refuses_a_card_the_owner_did_not_confirm(chat, upstream):                    # noqa: F811
    cid, card = chat.walk_to_card(upstream.url)
    rc, out = chat.run("onboard", "apply", cid)
    assert rc == 1 and "ONBOARD-NOT-CONFIRMED" in out
    assert not os.path.exists(os.path.join(chat.root, "app")) and "attach" not in project.load(chat.root)


def test_apply_refuses_a_card_that_was_replaced_and_one_whose_copy_changed(confirmed):
    c = confirmed
    path = os.path.join(c.root, ".alpaca", "onboard", "cards", c.cid + ".md")
    original = open(path).read()
    with open(path, "a") as fh:
        fh.write("x\n")
    rc, out = c.run("onboard", "apply", c.cid)
    assert rc == 1 and "ONBOARD-COPY-CHANGED" in out
    with open(path, "w") as fh:
        fh.write(original)
    assert c.run("onboard", "show")[1].startswith("confirmed card")
    with open(path, "a") as fh:
        fh.write("x\n")
    assert "changed:" in c.run("onboard", "show")[1]


def test_apply_on_a_due_studio_writes_the_revision_the_block_the_note_the_queue_and_the_level(confirmed):
    c = confirmed
    yaml_before = sha(os.path.join(c.root, "project.yaml"))
    rc, out = c.run("onboard", "apply", c.cid)
    assert rc == 0, out
    for needle in ("apply: onboard revision of the provisional setup:", "apply: attach init ", "(same as the look)",
                   "apply: style: plain-writing on", "apply: goal kept as note input/notes/", "intents/queue.md: 1 line",
                   "apply: session level: L3 recorded", "apply: scratch copy removed", "apply: recorded op-0 event",
                   "GATE alpaca-onboard: PASS (applied %s)" % c.cid):
        assert needle in out, needle
    cfg = project.load(c.root)
    assert cfg["name"] == "demo" and cfg["what"] == c.GOAL and cfg["default_level"] == "L3" and cfg["existing_repo"] is True
    assert cfg["paths"]["product_tree"] == "app" and "plain-writing" in cfg["style"]["presets"]
    block = config.mode(c.root)
    assert block.url == c.upstream.url and block.base == "main" and block.branch_prefix == "alpaca/"
    assert [g["name"] for g in block.gates] == ["build", "test"]
    assert git(os.path.join(c.root, "app"), "rev-parse", "HEAD") == c.upstream.sha()
    # the goal note holds the owner's words byte for byte, and the queue has the line
    note = [e for e in c.events("note")][-1]["data"]["file"] if c.events("note") else None
    assert note and open(os.path.join(c.root, note), "rb").read().endswith(c.GOAL.encode())
    assert "- [ ] %s" % c.GOAL in open(os.path.join(c.root, "intents", "queue.md")).read()
    assert adopt.first_chat_state(c.conn) == "done" and not os.path.exists(c.scratch)
    assert sha(os.path.join(c.root, "project.yaml")) != yaml_before
    ev = c.events("onboard-applied")[-1]["data"]
    assert ev["step"] == "done" and ev["mode"] == "attach" and ev["level"] == "L3"
    attach_ev = c.events("attach-init")[-1]["data"]
    assert attach_ev["card"] == c.cid and attach_ev["gates_source"] == "confirmed card %s" % c.cid
    # the revision event says what changed and which setup it replaces
    rev = [e for e in c.events("onboard") if e["data"].get("revision") == 2][-1]["data"]
    assert rev["card"] == c.cid and rev["revises"] and "default_level" in rev["changes"]
    assert c.events("onboard")[0]["data"].get("provisional") is True


def test_a_second_apply_writes_nothing(confirmed):
    c = confirmed
    assert c.run("onboard", "apply", c.cid)[0] == 0
    yaml, count = sha(os.path.join(c.root, "project.yaml")), len(c.events())
    rc, out = c.run("onboard", "apply", c.cid)
    assert rc == 0 and "already applied" in out
    assert sha(os.path.join(c.root, "project.yaml")) == yaml and len(c.events()) == count


def test_a_session_level_the_owner_already_set_is_never_raised_or_lowered(confirmed):
    from alpaca.posture import level
    c = confirmed
    level.set(c.conn, c.sid, 2, "the owner chose L2", root=c.root)                         # the card proposes L3
    rc, out = c.run("onboard", "apply", c.cid)
    assert rc == 0 and "session level: kept" in out
    assert level.in_force(c.conn, c.sid, root=c.root) == 2


def test_a_failure_in_the_attach_step_then_a_rerun_finishes_every_step_once(confirmed, monkeypatch):
    from alpaca.attach import init
    c = confirmed
    real = init.init_verb
    calls = []

    def boom(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the network dropped")
        return real(*a, **k)
    monkeypatch.setattr(init, "init_verb", boom)
    rc, out = c.run("onboard", "apply", c.cid)
    assert rc == 1 and "ONBOARD-APPLY-STEP" in out and "step attach failed" in out and "run apply again to resume" in out
    assert adopt.first_chat_state(c.conn) == "confirmed" and "attach" not in project.load(c.root)
    assert [e["data"]["step"] for e in c.events("onboard-applied")] == ["config"]
    rc, out = c.run("onboard", "apply", c.cid)
    assert rc == 0 and "apply: config ... done earlier" in out
    steps = [e["data"]["step"] for e in c.events("onboard-applied")]
    assert steps == ["config", "attach", "preset", "note", "level", "scratch", "done"]
    assert config.mode(c.root) is not None and len(c.events("attach-init")) == 1


def test_a_studio_made_with_new_attach_keeps_its_block_and_a_different_repo_is_refused(chat, upstream, tmp_path):   # noqa: F811
    from alpaca.attach import repo
    repo.attach(chat.root, upstream.url)
    cid, card = chat.walk_to_card(upstream.url)
    chat.say("yes")
    assert chat.run("onboard", "confirm", cid, "--quote", "yes")[0] == 0
    rc, out = chat.run("onboard", "apply", cid)
    assert rc == 0 and "already attached by alpaca new --attach; kept as is" in out
    assert len(chat.events("attach-init")) == 1


def test_a_card_for_another_repo_than_the_attached_one_is_refused(chat, upstream, tmp_path):        # noqa: F811
    from alpaca.attach import repo
    from alpaca.tests.attachkit import make_upstream
    other = make_upstream(tmp_path, name="other")
    repo.attach(chat.root, other.url)
    cid, card = chat.walk_to_card(upstream.url)
    chat.say("yes")
    assert chat.run("onboard", "confirm", cid, "--quote", "yes")[0] == 0
    rc, out = chat.run("onboard", "apply", cid)
    assert rc == 1 and "ONBOARD-ATTACH-MISMATCH" in out


def test_a_native_card_configures_without_a_clone(chat):
    opening = chat.say("I want to make a little app for my class but I am not a programmer")
    rc, out = chat.run("onboard", "answer", "opening", "--value", opening, "--quote", opening)
    assert rc == 0 and "this is a new project" in out and "round 1:" in out and "[stack]" in out
    reply = chat.say("a page that tracks reading logs. 2a. nothing is published online")
    for args in (["goal", "--value", "a page that tracks reading logs"], ["stack", "--option", "a"],
                 ["limits", "--value", "nothing is published online"]):
        assert chat.run("onboard", "answer", *args, "--quote", reply)[0] == 0
    cid, card = chat.propose()
    assert "mode native" in card and "WHERE ALPACA SITS: native mode" in card
    chat.say("yes")
    assert chat.run("onboard", "confirm", cid, "--quote", "yes")[0] == 0
    rc, out = chat.run("onboard", "apply", cid)
    assert rc == 0, out
    cfg = project.load(chat.root)
    assert "attach" not in cfg and not os.path.exists(os.path.join(chat.root, "app"))
    assert cfg["default_level"] == "L3" and cfg["existing_repo"] is False and cfg["what"] == "a page that tracks reading logs"
    assert "attach ... skipped (native mode" in out


def test_the_pad_and_the_doctor_follow_the_first_chat(chat, upstream):                           # noqa: F811
    from alpaca import pad
    assert "first chat: tell Alpaca about your project" in pad.render(chat.root)
    cid, card = chat.walk_to_card(upstream.url)
    chat.say("yes")
    chat.run("onboard", "confirm", cid, "--quote", "yes")
    assert "setup confirmed, not applied: run bin/alpaca onboard apply %s" % cid in pad.render(chat.root)
    chat.run("onboard", "apply", cid)
    text = pad.render(chat.root)
    assert "setup: %s confirmed " % cid in text and "(attach, L3)" in text
    assert "first chat:" not in text
