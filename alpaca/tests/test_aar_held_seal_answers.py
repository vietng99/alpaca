"""t-633 (done_bar[0]): the signed brief says op close refuses until the owed AAR is sealed or the
owner waives it, so the seal of an op answers every trigger counted for that op, a NO-GO or release
check FAIL no op is tied to included. `aar owe <op> --event` stays as the way to move such a trigger
to the op it belongs to.

Ways this still fails after it lands:
  * a loose trigger keeps blocking an op after its seal (the old rule)
  * a seal of op X answers the trigger for the other ops it blocks
  * a trigger recorded after the seal is covered by it
  * a report edited or deleted after the seal still answers (the seal holds only while the report
    hashes to it), or a hand-written aar-seal row answers
  * a read error on the seal path lets the op close
  * the BLOCKED line, `aar owed` or the hub still say a seal does not answer it
  * the owner waive stopped closing the op
  * `aar owe <op> --event` after a seal no longer makes the op owe the report again"""
import io
from contextlib import redirect_stdout

import pytest

from alpaca import aar, aarview, cli, db
from alpaca.tests import aarowedkit as k
from alpaca.tests import aarheldkit as kit
from alpaca.tests.ownerword_fixtures import Project, typed


@pytest.fixture(autouse=True)
def flow_profile(monkeypatch):
    kit.flow_profile(monkeypatch)


def _ready(project):
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    k.finish(project, conn, "t-002")
    return conn


def _hub(project, conn):
    ops = [dict(r) for r in conn.execute("SELECT * FROM ops ORDER BY id")]
    return aarview.project(conn, project, ops)[0]


def test_an_unattributed_fail_is_answered_by_the_ops_seal(project, capsys):
    conn = _ready(project)
    kit.fail(project, "cli")
    k.seal_aar(project, conn, capsys)
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)


def test_an_unattributed_nogo_is_answered_by_the_ops_seal(project, capsys):
    conn = _ready(project)
    kit.nogo(conn, "holds-nothing")
    k.seal_aar(project, conn, capsys)
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)


def test_the_seal_answers_only_its_own_op(project, capsys):
    conn = _ready(project)
    ev = kit.fail(project, "cli")
    k.seal_aar(project, conn, capsys)
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)
    code, out = k.close(capsys, k.OTHER)
    k.assert_blocked(conn, code, out, op=k.OTHER, events=(ev,))
    named = kit.blocks_named(capsys, ev)
    assert k.OTHER in named and k.OP not in named, k.owed(capsys)


def test_a_loose_trigger_after_the_seal_blocks_again(project, capsys):
    conn = _ready(project)
    k.seal_aar(project, conn, capsys)
    ev = kit.fail(project, "cli")
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out, events=(ev,))


def test_a_report_edited_after_the_seal_does_not_answer(project, capsys):
    conn = _ready(project)
    ev = kit.fail(project, "cli")
    k.seal_aar(project, conn, capsys)
    with open(k.report(project, k.OP), "a") as fh:
        fh.write("\nAn added line after the seal.\n")
    code, out = k.close(capsys)
    line = k.assert_blocked(conn, code, out, events=(ev,))
    assert "changed or is gone since event:" in line, line


def test_a_forged_seal_row_does_not_answer_a_loose_trigger(project, capsys):
    conn = _ready(project)
    ev = kit.fail(project, "cli")
    db.append_event(conn, session="cli", actor="agent", kind="aar-seal", op=k.OP, ref=k.OP,
                    data={"format": 2, "path": ".alpaca/aar/%s.md" % k.OP, "sha256": "0" * 64})
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out, events=(ev,))


def test_a_read_error_on_the_seal_path_never_closes(project, capsys, monkeypatch):
    conn = _ready(project)
    kit.fail(project, "cli")
    k.seal_aar(project, conn, capsys)

    def boom(root, op):
        raise RuntimeError("the report cannot be read")
    monkeypatch.setattr(aar, "load", boom)
    try:
        code, out = k.close(capsys)
    except RuntimeError:
        code, out = None, ""
    assert code != cli.PASS
    assert k.count(conn, "op-close", k.OP) == 0 and k.op_status(conn) == "open"


def test_the_blocked_line_names_the_seal_for_a_loose_trigger(project, capsys):
    conn = _ready(project)
    ev = kit.fail(project, "cli")
    code, out = k.close(capsys)
    line = k.assert_blocked(conn, code, out, events=(ev,))
    assert "aar seal %s" % k.OP in line and "aar owe" in line and "--event" in line, line
    assert "does not answer" not in line, line


def test_the_hub_shows_sealed_after_the_seal(project, capsys):
    conn = _ready(project)
    kit.fail(project, "cli")
    k.seal_aar(project, conn, capsys)
    states = _hub(project, conn)
    assert states[k.OP]["badge"] == "sealed", states[k.OP]
    assert aarview.UNTIED_LINE not in states[k.OP].get("lines", [])
    assert states[k.OTHER]["badge"] == "owed" and aarview.UNTIED_LINE in states[k.OTHER]["lines"]


def test_owe_event_after_the_seal_owes_again(project, capsys):
    """The brief keeps `aar owe --event`: it moves the trigger to the op, which then owes a report
    (its aar-owe row is newer than the seal) and clears it for the others."""
    conn = _ready(project)
    ev = kit.fail(project, "cli")
    k.seal_aar(project, conn, capsys)
    code, out = kit.owe_event(capsys, k.OP, ev)
    assert code == cli.PASS, out
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out)
    code, out = k.close(capsys, k.OTHER)
    k.assert_closed(conn, code, out, k.OTHER)


# ------------------------------------------------------------------ the owner waive still closes
WORDS = "op-1: waive"


def test_an_owner_waive_still_closes_it(tmp_path, monkeypatch):
    kit.flow_profile(monkeypatch)
    p = Project(tmp_path, monkeypatch)
    p.op("op-1")
    db.append_event(p.conn, session="s", actor="human", kind="op-open", op="op-1",
                    data={"intent": "x", "done_when": "x"})
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", p.sid)
    monkeypatch.chdir(p.root)
    ev = kit.raw_fail(p.conn, ref="attempt-t633")

    def run(*argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    code, out = run("op", "close", "op-1", "--basis", "the work is finished for the test")
    assert code == cli.BLOCKED and kit.has(out, ev), out
    p.write([typed(WORDS)])
    code, out = run("aar", "waive", "op-1", "--by", "owner", "--quote", WORDS)
    assert code == cli.PASS, out
    code, out = run("op", "close", "op-1", "--basis", "the work is finished for the test")
    assert code == cli.PASS, out
