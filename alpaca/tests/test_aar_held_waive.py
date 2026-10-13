"""A04 (done_bar[1] waive exit; cause C1 deadlock, C2 waive scope): the owner can waive a loose
trigger for an op it blocks, through the one owner path t-622 built (ownerword.human_write, whole
message exactly '<op>: waive', one message waives once).

Ways this still fails after it lands:
  * deadlock: op close is BLOCKED by a loose trigger but `aar waive` reads only the attributed
    triggers and refuses with "owes no AAR; there is nothing to waive"
  * the waive of op-1 clears the trigger for op-2 as well, or attributes it (it must stay loose)
  * a loose trigger recorded after the waive is covered by it
  * a refused waive writes something or opens the close
CLI level, as test_aar_owed_waive_owner_only.py: the env names the session and the transcript sits
under CLAUDE_CONFIG_DIR (ownerword_fixtures.Project). Ops get an op-open event, so rule (2) can
place the trigger inside their lifetime."""
import io
from contextlib import redirect_stdout

import pytest

from alpaca import cli, db
from alpaca.tests.ownerword_fixtures import Project, typed

from alpaca.tests import aarheldkit as kit

WORDS = "op-1: waive"


@pytest.fixture
def p(tmp_path, monkeypatch):
    proj = Project(tmp_path, monkeypatch)
    for op in ("op-1", "op-2"):
        proj.op(op)
        db.append_event(proj.conn, session="s", actor="human", kind="op-open", op=op,
                        data={"intent": "x", "done_when": "x"})
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", proj.sid)
    monkeypatch.chdir(proj.root)
    return proj


def run(*argv):
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            code = cli.main(list(argv))
    except SystemExit as exc:
        code = exc.code
    return code, out.getvalue()


def count(p, kind=None):
    q = "SELECT COUNT(*) FROM events" + (" WHERE kind=?" if kind else "")
    return p.conn.execute(q, (kind,) if kind else ()).fetchone()[0]


def close(p, op="op-1"):
    return run("op", "close", op, "--basis", "the work is finished for the test")


def blocked(code, out, ev=None):
    assert code == cli.BLOCKED, out
    assert "GATE alpaca-op-close: BLOCKED (" in out, out
    if ev is not None:
        assert kit.has(out, ev), out


def test_the_owner_waives_a_loose_trigger_for_one_op_only(p):
    ev = kit.raw_fail(p.conn, ref="attempt-w1")
    blocked(*close(p), ev)
    p.write([typed(WORDS)])
    code, out = run("aar", "waive", "op-1", "--by", "owner", "--quote", WORDS)
    assert code == cli.PASS, out
    got = db.events(p.conn, kind="aar-waive", limit=10)
    assert len(got) == 1 and ev in (got[0]["data"].get("triggers") or []), got
    assert count(p, "op-close") == 0
    code, out = close(p)
    assert code == cli.PASS, out
    blocked(*close(p, "op-2"), ev)
    code, out = run("aar", "owed")
    assert kit.has(kit.loose_part(out), ev), "the waive attributed the trigger: %s" % out


@pytest.mark.parametrize("argv", [
    ("aar", "waive", "op-1", "--by", "agent", "--quote", WORDS),
    ("aar", "waive", "op-1", "--by", "owner", "--quote", "waive"),
    ("aar", "waive", "op-1", "--by", "owner", "--quote", "do not type op-1: waive, I want the report"),
])
def test_a_refused_waive_of_a_loose_trigger_writes_nothing(p, argv):
    ev = kit.raw_fail(p.conn, ref="attempt-w2")
    p.write([typed(WORDS), typed("do not type op-1: waive, I want the report")])
    before = count(p)
    code, out = run(*argv)
    assert code not in (cli.PASS, 0, None), (argv, out)
    assert count(p) == before
    blocked(*close(p), ev)


def test_a_loose_trigger_after_the_waive_blocks_again(p):
    kit.raw_fail(p.conn, ref="attempt-w3")
    p.write([typed(WORDS)])
    assert run("aar", "waive", "op-1", "--by", "owner", "--quote", WORDS)[0] == cli.PASS
    later = kit.raw_fail(p.conn, ref="attempt-w4")
    blocked(*close(p), later)


def test_waive_and_attributed_triggers_together(p):
    """An op blocked by an aar owe and a loose FAIL: one waive answers both (its triggers list both)."""
    code, out = run("aar", "owe", "op-1", "--reason", kit.REASON)
    assert code == cli.PASS, out
    owe = p.conn.execute("SELECT MAX(id) FROM events WHERE kind='aar-owe'").fetchone()[0]
    ev = kit.raw_fail(p.conn, ref="attempt-w5")
    p.write([typed(WORDS)])
    code, out = run("aar", "waive", "op-1", "--by", "owner", "--quote", WORDS)
    assert code == cli.PASS, out
    trig = db.events(p.conn, kind="aar-waive", limit=10)[0]["data"]["triggers"]
    assert owe in trig and ev in trig, trig
    code, out = close(p)
    assert code == cli.PASS, out
