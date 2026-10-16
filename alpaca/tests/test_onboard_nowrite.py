"""AC-19: the first chat reads the repo, reads back the goal, proposes one card, and writes nothing before the
owner confirms (op-062 t-656; the fail case of its contract: configure or attach before the yes).

The snapshot covers every file of the studio outside `.alpaca/`, the studio's git config and exclude file,
and the folder `app/`. The verbs may add record events of the kinds in `PRE_CONFIRM_KINDS` and card files
under `.alpaca/onboard/cards/`, and nothing else."""
import hashlib
import os

import pytest

from alpaca import cli, onboard_card as oc
from alpaca.tests.attachkit import git, upstream  # noqa: F401
from alpaca.tests.onboardchat import Chat, chat, make_due_studio  # noqa: F401


def snapshot(root):
    out = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in (".alpaca", ".git")]
        for fn in fns:
            path = os.path.join(dp, fn)
            with open(path, "rb") as fh:
                out[os.path.relpath(path, root)] = hashlib.sha256(fh.read()).hexdigest()
    for rel in (".git/config", ".git/info/exclude"):
        path = os.path.join(root, rel)
        if os.path.exists(path):
            with open(path, "rb") as fh:
                out[rel] = hashlib.sha256(fh.read()).hexdigest()
    out["app"] = os.path.exists(os.path.join(root, "app"))
    return out


def kinds_after(chat, count):
    return [e["kind"] for e in chat.events()[count:]]


@pytest.fixture(params=["due", "fresh"])
def studio_chat(request, project, monkeypatch):
    """A studio after `alpaca new` (first chat due) and a fresh copy no one has onboarded."""
    c = Chat(project, monkeypatch, studio=False)
    if request.param == "due":
        make_due_studio(c.root)
    else:
        git(c.root, "init", "-q")
        assert cli.main(["init"]) == 0
    from alpaca import db
    c.conn = db.connect(c.root)
    # the session row and its first event live in the record the studio just made
    import json
    db.upsert(c.conn, "sessions", "sid", {"sid": c.sid, "started": c.now(), "transcript": c.transcript})
    db.append_event(c.conn, session=c.sid, actor="alpaca", kind="session-start", data={"transcript": c.transcript})
    c.state = request.param
    yield c
    c.close()


def test_look_answers_propose_decline_supersede_and_refuse_write_nothing_before_the_yes(studio_chat, upstream):   # noqa: F811
    c = studio_chat
    before = snapshot(c.root)
    count = len(c.events())
    c.look(upstream.url)
    c.answers()
    c.run("onboard", "answer", "finding", "--value", "Makefile has two targets", "--pointer", "repo:Makefile:1-4", "--source", "agent")
    c.run("onboard", "answer", "gate.extra", "--name", "syntax", "--argv-json", '["python3","-c","pass"]',
          "--reason", "src/main.py", "--source", "agent")
    first, card = c.propose()
    c.say("no")
    rc, out = c.run("onboard", "confirm", first, "--quote", "no")                       # a decline
    assert rc == 1 and "declined" in out
    c.say("change the goal: I want hello world")
    c.run("onboard", "answer", "goal", "--value", "I want hello world", "--quote", "change the goal: I want hello world")
    second, card2 = c.propose()                                                         # supersedes the first card
    assert second != first
    c.say("yes")
    rc, out = c.run("onboard", "confirm", first, "--quote", "yes")                      # a refused confirm: the old card
    assert rc == 1 and "ONBOARD-SUPERSEDED" in out
    rc, out = c.run("onboard", "apply", second)                                         # no confirm yet
    assert rc == 1 and "ONBOARD-NOT-CONFIRMED" in out
    rc, out = c.run("attach", "init", upstream.url)                                     # the agent must not attach on its own
    assert rc == 1 and ("ATTACH-NEEDS-CARD" in out or "not onboarded yet" in out)          # a fresh copy: not even onboarded
    rc, out = c.run("attach", "init", upstream.url, "--card", second)                   # nor through an unconfirmed card
    assert rc == 1 and "ATTACH-CARD-NOT-CONFIRMED" in out
    # nothing outside the record and the cards changed
    assert snapshot(c.root) == before
    new = kinds_after(c, count)
    assert set(new) <= oc.PRE_CONFIRM_KINDS | {"decision", "heartbeat"}, set(new) - oc.PRE_CONFIRM_KINDS
    assert {"onboard-look", "onboard-round", "onboard-answer", "onboard-card", "owner-ask", "onboard-declined"} <= set(new)
    cards = sorted(os.listdir(os.path.join(c.root, ".alpaca", "onboard", "cards")))
    assert cards == sorted([first + ".json", first + ".md", second + ".json", second + ".md"])
    assert not os.path.exists(os.path.join(c.root, "app"))
    # the scratch copy lives outside the studio
    assert not os.path.realpath(c.scratch).startswith(c.root)


def test_after_the_confirm_apply_changes_the_project_and_attaches(studio_chat, upstream):                          # noqa: F811
    c = studio_chat
    before = snapshot(c.root)
    cid, card = c.walk_to_card(upstream.url)
    c.say("yes")
    rc, out = c.run("onboard", "confirm", cid, "--quote", "yes")
    assert rc == 0, out
    assert snapshot(c.root) == before                                                   # confirm writes no configuration
    rc, out = c.run("onboard", "apply", cid)
    assert rc == 0, out
    after = snapshot(c.root)
    assert after["project.yaml"] != before.get("project.yaml") and after["app"] is True
    kinds = {e["kind"] for e in c.events()}
    assert {"onboard-confirmed", "onboard-applied", "attach-init"} <= kinds
    if c.state == "fresh":
        assert "onboard" in kinds and "op-close" in kinds
