"""op-019: the shared gate module (alpaca/intentgate.py, design sections 3.3, 4, 5.3, 6). The level a
gate reads is the highest over the named session and the environment's sessions; providers that are
not built are skipped; a provider's refusal stops the task verb."""
import sys
import types

import pytest

from alpaca import intentgate, ownerword, profile
from alpaca.posture import level as posture_level
from alpaca.tests.ownerword_fixtures import Project


@pytest.fixture
def p(tmp_path, monkeypatch):
    monkeypatch.setattr(profile, "load", lambda root: profile.Profile())
    proj = Project(tmp_path, monkeypatch)
    proj.op()
    return proj


def test_the_named_session_cannot_lower_the_level_of_the_environment_session(p, monkeypatch):
    posture_level.set(p.conn, "real-6", 6, "goal", root=p.root)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "real-6")
    ownerword.capture()
    try:
        assert intentgate.level(p.conn, "fresh-x", root=p.root) == 6
        assert intentgate.level(p.conn, "cli", root=p.root) == 6
    finally:
        ownerword.release()


def test_the_original_alpaca_session_counts_too(p, monkeypatch):
    posture_level.set(p.conn, "alp-5", 5, "goal", root=p.root)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "other")
    monkeypatch.setenv("ALPACA_SESSION_ID", "alp-5")
    ownerword.capture()
    try:
        assert intentgate.level(p.conn, "fresh-x", root=p.root) == 5
    finally:
        ownerword.release()


def test_a_refusal_stops_the_action(p, monkeypatch):
    seen = []
    fake = types.ModuleType("alpaca.fakegate")

    def task_gate(conn, task, action, *, level, session, root):
        seen.append((task["id"], action, level))
        if action == "done":
            raise intentgate.IntentGateRefusal("FAKE-REFUSED", "no")

    fake.task_gate = task_gate
    monkeypatch.setattr(intentgate, "_providers", lambda: [fake])
    intentgate.task_gate(p.conn, {"id": "t-1", "op": "op-1"}, "claim", session="s", root=p.root)
    with pytest.raises(intentgate.IntentGateRefusal) as e:
        intentgate.task_gate(p.conn, {"id": "t-1", "op": "op-1"}, "done", session="s", root=p.root)
    assert e.value.reason == "FAKE-REFUSED"
    intentgate.task_gate(p.conn, {"id": "t-1", "op": "op-1"}, "open", session="s", root=p.root)
    assert [a for _, a, _ in seen] == ["claim", "done"]


def test_door_links_read_the_higher_of_the_flag_and_the_level_in_force(p, monkeypatch):
    got = []
    fake = types.ModuleType("alpaca.fakedoor")
    fake.door_link = lambda conn, op, boundary, *, level, session, root: got.append(level) or ("fake", 0)
    monkeypatch.setattr(intentgate, "_providers", lambda: [fake])
    posture_level.set(p.conn, "real-6", 6, "goal", root=p.root)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "real-6")
    ownerword.capture()
    try:
        assert intentgate.door_links(p.conn, "op-1", "design->build", level_flag="L4", session="cli",
                                     root=p.root) == [("fake", 0)]
    finally:
        ownerword.release()
    assert got == [6]
