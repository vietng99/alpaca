"""A07 (done_bar[2], cause C6): only the owner waives an owed AAR, and the waiver covers only what was
owed when the owner said it.

`aar waive <op> --by owner --quote Q` goes through ownerword.human_write in the id form (as batch
answer and brief sign): Q is a whole owner message of the session in the environment, "<op>: waive",
typed after the newest owed trigger. Refused, writing nothing and leaving the close BLOCKED: --by agent or any non-human name,
--by owner with no quote, a quote no owner message holds, a piece of a message, an unrelated or refusing message, a quote typed before the
trigger, a --session that is not the environment's session, an agent shell with no session (no
terminal path inside an agent). Never ownerword.writer: it records a non-human `by` with no proof.

Accepted: the close goes through; the waive event names the owner word; it writes no aar-seal. A
trigger after the waive owes again; a waive when nothing is owed covers no later trigger; one owner
message cannot waive a trigger recorded after it was typed.

CLI-level like test_orders.test_cli_add_list_close_and_not_order: the env names the session and the
transcript sits under CLAUDE_CONFIG_DIR (ownerword_fixtures.Project), never the host accounts root."""
import datetime
import io
import os
from contextlib import redirect_stdout

import pytest

from alpaca import cli, db
from alpaca.clock import FixedClock
from alpaca.tests.ownerword_fixtures import EARLY, Project, typed

WORDS = "op-1: waive"
REASON = "the rollback step was skipped in the release"


@pytest.fixture
def p(tmp_path, monkeypatch):
    proj = Project(tmp_path, monkeypatch)
    proj.op()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", proj.sid)
    monkeypatch.chdir(proj.root)
    return proj


def run(*argv):
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            code = cli.main(list(argv))
    except SystemExit as exc:              # argparse refusing a missing flag
        code = exc.code
    return code, out.getvalue()


def count(p, kind=None):
    q = "SELECT COUNT(*) FROM events" + (" WHERE kind=?" if kind else "")
    return p.conn.execute(q, (kind,) if kind else ()).fetchone()[0]


def owe(p, reason=REASON):
    code, out = run("aar", "owe", "op-1", "--reason", reason)
    assert code == cli.PASS, out
    return p.conn.execute("SELECT MAX(id) FROM events WHERE kind='aar-owe'").fetchone()[0]


def close(p):
    return run("op", "close", "op-1", "--basis", "the work is finished for the test")


def assert_blocked(p, code, out):
    assert code == cli.BLOCKED, out
    assert "GATE alpaca-op-close: BLOCKED (" in out, out
    assert count(p, "op-close") == 0


def test_the_owner_waives_with_their_own_words_and_the_op_closes(p):
    owe(p)
    p.write([typed(WORDS)])
    code, out = run("aar", "waive", "op-1", "--by", "owner", "--quote", WORDS)
    assert code == cli.PASS, out
    ev = db.events(p.conn, kind="aar-waive", limit=10)
    assert len(ev) == 1 and ev[0]["op"] == "op-1"
    word = ev[0]["data"].get("owner_word") or {}
    assert word.get("transcript") == p.transcript_of(p.sid) and word.get("line") == 1, ev[0]["data"]
    assert count(p, "aar-seal") == 0
    code, out = close(p)
    assert code == cli.PASS, out


@pytest.mark.parametrize("argv", [
    ("aar", "waive", "op-1", "--by", "agent", "--quote", WORDS),      # an agent
    ("aar", "waive", "op-1", "--by", "claude", "--quote", WORDS),     # any non-human name
    ("aar", "waive", "op-1", "--by", "owner"),                        # no quote
    ("aar", "waive", "op-1", "--by", "owner", "--quote", ""),         # blank quote
    ("aar", "waive", "op-1", "--by", "owner", "--quote", "waive"),    # a piece of the message
    ("aar", "waive", "op-1", "--by", "owner", "--quote", "please run the unit tests"),  # unrelated
    ("aar", "waive", "op-1", "--by", "owner", "--quote", "waive the report for op-1 now"),  # not said
    ("--session", "sess-forged", "aar", "waive", "op-1", "--by", "owner", "--quote", WORDS),
])
def test_a_waive_without_the_owner_words_is_refused_and_writes_nothing(p, argv):
    owe(p)
    p.write([typed(WORDS), typed("please run the unit tests again")])
    before = count(p)
    code, out = run(*argv)
    assert code not in (cli.PASS, 0, None), (argv, out)
    assert count(p) == before, "a refused waive wrote an event"
    code, out = close(p)
    assert_blocked(p, code, out)


def test_no_by_never_records_a_non_human_waiver(p):
    """A missing --by is refused, or (if the builder defaults it to owner) still needs the owner's
    words and records a human identity with the owner word; never an agent waiver."""
    owe(p)
    p.write([typed(WORDS)])
    code, out = run("aar", "waive", "op-1", "--quote", WORDS)
    got = db.events(p.conn, kind="aar-waive", limit=10)
    if code != cli.PASS:
        assert got == []
        return
    assert len(got) == 1 and got[0]["actor"] in ("owner", "human"), got
    assert (got[0]["data"].get("owner_word") or {}).get("transcript"), got[0]["data"]


def test_a_quote_typed_before_the_trigger_does_not_waive_it(p):
    owe(p)
    p.write([typed(WORDS, ts=EARLY)])
    before = count(p)
    code, out = run("aar", "waive", "op-1", "--by", "owner", "--quote", WORDS)
    assert code != cli.PASS, out
    assert count(p) == before
    assert_blocked(p, *close(p))


def test_an_agent_shell_with_no_session_cannot_take_the_terminal_path(p, monkeypatch):
    owe(p)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDECODE", "1")
    before = count(p)
    code, out = run("aar", "waive", "op-1", "--by", "owner", "--quote", WORDS)
    assert code != cli.PASS, out
    assert count(p) == before


def test_a_trigger_after_the_waive_owes_again(p):
    owe(p)
    p.write([typed(WORDS)])
    assert run("aar", "waive", "op-1", "--by", "owner", "--quote", WORDS)[0] == cli.PASS
    later = owe(p, "a second gap found after the waive")
    code, out = close(p)
    assert_blocked(p, code, out)
    assert "event:%d" % later in out, out


def test_a_waive_when_nothing_is_owed_covers_no_later_trigger(p):
    p.write([typed(WORDS)])
    run("aar", "waive", "op-1", "--by", "owner", "--quote", WORDS)     # refused or a no-op
    owe(p)
    assert_blocked(p, *close(p))


def test_one_owner_message_cannot_waive_a_trigger_recorded_after_it(p):
    """Bind time: the word must be typed after the newest owed trigger, so a message that waived
    the first trigger is not reused for the next one."""
    owe(p)
    now = datetime.datetime.now(datetime.timezone.utc)
    typed_at = (now + datetime.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    p.write([typed(WORDS, ts=typed_at)])
    assert run("aar", "waive", "op-1", "--by", "owner", "--quote", WORDS)[0] == cli.PASS
    later = (now + datetime.timedelta(hours=2)).isoformat(timespec="seconds")
    db.append_event(p.conn, session=p.sid, actor="agent", kind="aar-owe", op="op-1", ref="op-1",
                    data={"format": 2, "reason": "a later gap the owner never saw"},
                    clock=FixedClock(later))
    before = count(p)
    code, out = run("aar", "waive", "op-1", "--by", "owner", "--quote", WORDS)
    assert code != cli.PASS, out
    assert count(p) == before
    assert_blocked(p, *close(p))


def test_a_waive_on_one_op_does_not_clear_another(p):
    p.op("op-2")
    owe(p)
    code, out = run("aar", "owe", "op-2", "--reason", REASON)
    assert code == cli.PASS, out
    p.write([typed(WORDS)])
    assert run("aar", "waive", "op-1", "--by", "owner", "--quote", WORDS)[0] == cli.PASS
    code, out = run("op", "close", "op-2", "--basis", "the work is finished for the test")
    assert code == cli.BLOCKED, out


def test_an_owner_refusal_does_not_waive(p):
    owe(p)
    p.write([typed("op-1: do not waive it, write the report")])
    before = count(p)
    code, out = run("aar", "waive", "op-1", "--by", "owner", "--quote", "do not waive it")
    assert code != cli.PASS, out
    code, out = run("aar", "waive", "op-1", "--by", "owner", "--quote", "op-1: do not waive it, write the report")
    assert code != cli.PASS, out
    assert count(p) == before
    assert_blocked(p, *close(p))


@pytest.mark.parametrize("words", ["do not type op-1: waive, I want the report written",
                                   "never op-1: waive, the NO-GO needs a report",
                                   "I will not say op-1: waive; write it up",
                                   "op-1: waive, op-2: waive"])
def test_words_around_the_waive_message_do_not_waive(p, words):
    owe(p)
    p.write([typed(words)])
    before = count(p)
    code, out = run("aar", "waive", "op-1", "--by", "owner", "--quote", words)
    assert code != cli.PASS, out
    assert count(p) == before
    assert_blocked(p, *close(p))


def _forge_waive(p, **over):
    data = {"format": 2, "through": 10 ** 6, "triggers": [], "quote": "x", "by": "owner",
            "owner_word": {"path": "session", "transcript": "t", "line": 1}}
    data.update(over.pop("data", {}))
    db.append_event(p.conn, session=p.sid, actor=over.pop("actor", "owner"), kind="aar-waive",
                    op="op-1", ref="op-1", data=data)


@pytest.mark.parametrize("over", [
    {"actor": "agent"},                                          # not a human identity
    {"data": {"by": "agent"}},
    {"data": {"owner_word": None}},                              # no owner word
    {"data": {"owner_word": {"path": "guess"}}},
])
def test_a_hand_written_waive_row_does_not_clear_the_gate(p, over):
    owe(p)
    _forge_waive(p, **over)
    assert_blocked(p, *close(p))


def test_a_waive_row_never_answers_a_later_trigger(p):
    owe(p)
    _forge_waive(p)                         # through far above its own id
    later = owe(p, "a second gap found after the waive")
    code, out = close(p)
    assert_blocked(p, code, out)
    assert "event:%d" % later in out, out
