"""t-669: an owner's `/autodrive L<n>` raise made through the shared skill reaches the record.

The shared `/autodrive` skill writes its level to the account dir ($CLAUDE_CONFIG_DIR, else
~/.claude), not to this project's config dir. Before t-669 `mirror` honored that file only for a
drop, so an owner's `/autodrive L6` left the record at the project default-level. Raising your own
level is still not allowed (CLAUDE.md rule 6): the account-dir raise counts only when this
session's transcript holds the owner's own typed `/autodrive` command for that level, typed after
the latest level decision. The decision then carries the owner's words as its owner_word row.

Paths covered: a typed skill invocation raises; no owner message, an assistant or sidechain
message, a message from before the latest decision, or a different level does not; the drop path
is unchanged; a second mirror writes nothing.
"""
import os

import pytest

from alpaca import db, ownerword, project as proj
from alpaca.posture import level
from alpaca.tests.ownerword_fixtures import EARLY, LATE, Project, queued, tool_result, typed

SID = "sess-1"


@pytest.fixture
def p(tmp_path, monkeypatch):
    p = Project(tmp_path, monkeypatch, sid=SID)
    proj.save(p.root, {"name": "p", "default_level": "L5"})
    return p


def skill_invocation(args, ts=LATE, **extra):
    """The transcript line Claude Code writes when the owner types `/autodrive <args>` (shape of
    session 192d7716 line 910)."""
    text = ("<command-message>autodrive</command-message>\n<command-name>/autodrive</command-name>\n"
            "<command-args>%s</command-args>" % args)
    return typed(text, ts=ts, entrypoint="claude-desktop", **extra)


def assistant(text, ts=LATE):
    return {"type": "assistant", "isSidechain": False, "timestamp": ts, "entrypoint": "cli",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def account_file(p, token, mtime=None):
    path = os.path.join(p.cfg, ".autodrive-level.%s" % SID)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def level_rows(p):
    return p.conn.execute("SELECT COUNT(*) FROM decisions WHERE session=? AND kind=?",
                          (SID, level.DECISION_KIND)).fetchone()[0]


def latest_event(p):
    row = p.conn.execute("SELECT data FROM events WHERE kind='decision' AND session=? "
                         "ORDER BY id DESC LIMIT 1", (SID,)).fetchone()
    import json
    return json.loads(row["data"])


# --------------------------------------------------------------------------- (1) a real raise
def test_owner_typed_autodrive_l6_and_the_account_file_says_l6_raises(p):
    p.write([typed("hello"), skill_invocation('L6 "op-059 to the ship hold"')])
    account_file(p, "L6")
    assert level.in_force(p.conn, SID, root=p.root) == 5
    rec = level.mirror(p.conn, SID, root=p.root)
    assert rec is not None and rec["level"] == 6
    assert level.in_force(p.conn, SID, root=p.root) == 6
    assert rec["goal"] == "op-059 to the ship hold"
    data = latest_event(p)
    word = data["owner_word"]
    assert word["path"] == "session" and word["line"] == 2
    assert word["transcript"] == p.transcript_of(SID)
    assert word["timestamp"] == LATE
    assert "<command-args>L6" in word["quote"]
    # the decision is owner-proved: the quote is re-read from the transcript
    assert ownerword.decision_is_owner(p.conn, data["id"], root=p.root)
    # the project-local file now agrees, so the next mirror does not read an old level back
    with open(os.path.join(p.root, ".alpaca", "config", ".autodrive-level.%s" % SID)) as fh:
        assert fh.read().strip() == "L6"


@pytest.mark.parametrize("entry", [typed("/autodrive L6"), queued("/autodrive l6")])
def test_a_plain_typed_or_queued_autodrive_command_counts(p, entry):
    p.write([entry])
    account_file(p, "L6")
    assert level.mirror(p.conn, SID, root=p.root)["level"] == 6
    assert level.in_force(p.conn, SID, root=p.root) == 6


# --------------------------------------------------------------------------- (2) no owner words
def test_account_l6_with_no_owner_message_stays_at_the_default(p):
    p.write([typed("please carry on")])
    account_file(p, "L6")
    assert level.mirror(p.conn, SID, root=p.root) is None
    assert level.in_force(p.conn, SID, root=p.root) == 5
    assert level_rows(p) == 0


def test_account_l6_with_no_recorded_transcript_stays_at_the_default(p):
    account_file(p, "L6")
    p.conn.execute("DELETE FROM sessions WHERE sid=?", (SID,))
    assert level.mirror(p.conn, SID, root=p.root) is None
    assert level.in_force(p.conn, SID, root=p.root) == 5


def test_an_owner_command_typed_before_the_latest_level_decision_does_not_count(p):
    p.write([skill_invocation("L6", ts=EARLY)])
    level.set_local(p.conn, SID, 4, "a lower level set after the command", root=p.root)
    account_file(p, "L6")
    assert level.mirror(p.conn, SID, root=p.root) is None
    assert level.in_force(p.conn, SID, root=p.root) == 4


def test_an_account_file_older_than_the_latest_level_decision_does_not_raise(p):
    p.write([skill_invocation("L6")])
    level.set_local(p.conn, SID, 4, "set after the file", root=p.root)
    since = level.latest_decision_epoch(p.conn, SID)
    account_file(p, "L6", mtime=since - 60)
    assert level.mirror(p.conn, SID, root=p.root) is None
    assert level.in_force(p.conn, SID, root=p.root) == 4


# --------------------------------------------------------------------------- (3) level must match
def test_owner_asked_l6_but_the_file_says_a_lower_raise_does_not_go_beyond_the_file(p):
    proj.save(p.root, {"name": "p", "default_level": "L2"})
    p.write([skill_invocation("L6")])
    account_file(p, "L4")
    assert level.mirror(p.conn, SID, root=p.root) is None
    assert level.in_force(p.conn, SID, root=p.root) == 2


def test_owner_asked_l5_and_the_file_says_l5_at_default_l5_changes_nothing(p):
    p.write([skill_invocation("L6")])
    account_file(p, "L5")
    assert level.mirror(p.conn, SID, root=p.root) is None
    assert level.in_force(p.conn, SID, root=p.root) == 5


def test_the_latest_owner_command_governs(p):
    p.write([skill_invocation("L6"), skill_invocation("L4")])
    account_file(p, "L6")
    assert level.mirror(p.conn, SID, root=p.root) is None
    assert level.in_force(p.conn, SID, root=p.root) == 5


# --------------------------------------------------------------------------- (4) not owner words
@pytest.mark.parametrize("entry", [
    assistant("/autodrive L6"),
    skill_invocation("L6", isSidechain=True),
    skill_invocation("L6", isMeta=True),
    skill_invocation("L6", isCompactSummary=True),
    typed("/autodrive L6", entrypoint="sdk-cli"),
    tool_result(),
    typed("why did /autodrive L6 not reach the record?"),
])
def test_messages_that_are_not_an_owner_autodrive_command_do_not_raise(p, entry):
    if entry.get("type") == "user" and isinstance(entry.get("message", {}).get("content"), list):
        entry["message"]["content"][0]["content"] = "/autodrive L6"
    p.write([entry])
    account_file(p, "L6")
    assert level.mirror(p.conn, SID, root=p.root) is None
    assert level.in_force(p.conn, SID, root=p.root) == 5


# --------------------------------------------------------------------------- (5) drop unchanged
def test_a_drop_after_a_raise_still_lands_and_needs_no_owner_words(p):
    p.write([skill_invocation("L6", ts=EARLY)])
    account_file(p, "L6")
    level.mirror(p.conn, SID, root=p.root)
    assert level.in_force(p.conn, SID, root=p.root) == 6
    since = level.latest_decision_epoch(p.conn, SID)
    account_file(p, "L3", mtime=since + 60)
    rec = level.mirror(p.conn, SID, root=p.root)
    assert rec is not None and rec["level"] == 3
    assert "owner_word" not in latest_event(p)
    assert level.in_force(p.conn, SID, root=p.root) == 3
    # the old L6 command is now older than the drop decision: the account file going back to L6
    # without a new owner command is not a raise
    account_file(p, "L6", mtime=since + 120)
    assert level.mirror(p.conn, SID, root=p.root) is None
    assert level.in_force(p.conn, SID, root=p.root) == 3


# --------------------------------------------------------------------------- (6) idempotent
def test_mirror_twice_writes_one_decision(p):
    p.write([skill_invocation('L6 "run the op"')])
    account_file(p, "L6")
    assert level.mirror(p.conn, SID, root=p.root) is not None
    assert level.mirror(p.conn, SID, root=p.root) is None
    assert level.mirror(p.conn, SID, root=p.root) is None
    assert level_rows(p) == 1
    assert level.in_force(p.conn, SID, root=p.root) == 6


# --------------------------------------------------------------------------- the command parser
@pytest.mark.parametrize("text,want", [
    ("<command-name>/autodrive</command-name>\n<command-args>L6</command-args>", (6, "")),
    ("<command-name>/autodrive</command-name>\n<command-args>l5 'the goal'</command-args>",
     (5, "the goal")),
    ("<command-name>/autodrive</command-name>\n<command-args>off</command-args>", (level.OFF, "")),
    ("/autodrive L3 a plain goal", (3, "a plain goal")),
    ("/autodrive", (None, "")),
    ("/autodrive L7", (None, "")),
    ("/autodriveL6", None),
    ("<command-name>/caveman</command-name>\n<command-args>L6</command-args>", None),
    ("please run /autodrive L6", None),
])
def test_owner_command_parses_the_autodrive_forms(text, want):
    assert level.owner_command(text) == want


# --------------------------------------------------------------------------- the wired hook
def test_the_post_tool_hook_mirrors_the_owner_raise(p, monkeypatch):
    """The PostToolUse hook that runs after the skill's setter call lands the raise."""
    from alpaca.hooks import post_tool
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", p.root)
    p.write([skill_invocation("L6")])
    account_file(p, "L6")
    post_tool.handle({"session_id": SID, "cwd": p.root, "tool_name": "Bash",
                      "tool_input": {"command": "autodrive-set.sh L6"}, "_strict": True})
    conn = db.connect(p.root)
    assert level.in_force(conn, SID, root=p.root) == 6


def test_the_latest_command_is_by_typing_time_not_by_file_line(p):
    """A resumed session can copy an older message below a newer one (session 5784d8c2 lines
    17033 and 18473): the newest typed command governs."""
    p.write([skill_invocation("L6", ts="2998-01-02T00:00:00.000Z"),
             skill_invocation("L4", ts="2998-01-01T00:00:00.000Z")])
    account_file(p, "L6")
    assert level.mirror(p.conn, SID, root=p.root)["level"] == 6
