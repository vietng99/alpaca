"""t-622 round 1 fixes: the NO-GO read fails closed on a broken profile (op close prints BLOCKED,
`aar owed` prints FAIL), `aar owed` survives an event that names an op with no row, and a reason
carries no terminal control codes."""
import sys
import uuid
from pathlib import Path

import pytest

from alpaca import cli, db
from alpaca.tests import aarowedkit as k

REASON = "the rollback step was skipped in the release"

PROFILE_SRC = '''
import os
from alpaca.profile import Profile


class Dom(Profile):
    name = "dom"

    def events(self):
        if os.environ.get("AAR_BREAK"):
            raise RuntimeError("the profile broke after the NO-GO was recorded")
        return {"review_flow_kinds": ["run-review-flow"]}


PROFILE = Dom()
'''


@pytest.fixture
def dom(project, monkeypatch):
    root = Path(project)
    pkg = "aardom_%s" % uuid.uuid4().hex[:10]
    (root / pkg).mkdir()
    (root / pkg / "__init__.py").write_text("", encoding="utf-8")
    (root / pkg / "profile.py").write_text(PROFILE_SRC, encoding="utf-8")
    monkeypatch.delenv("AAR_BREAK", raising=False)
    yield root, pkg + ".profile"
    sys.modules.pop(pkg + ".profile", None)
    sys.modules.pop(pkg, None)
    while str(root) in sys.path:
        sys.path.remove(str(root))


def test_a_broken_profile_does_not_open_the_gate(dom, capsys, monkeypatch):
    root, dotted = dom
    conn = k.two_ops(str(root))
    with open(root / "project.yaml", "a", encoding="utf-8") as fh:
        fh.write("\nprofile: %s\n" % dotted)
    k.finish(str(root), conn, "t-001")
    ev = db.append_event(conn, session="main", actor="agent", kind="run-review-flow", op=k.OP, ref="j-1",
                         data={"decision": {"value": "NO-GO", "session": "main",
                                            "decided_at": "2026-10-13T10:00:00+00:00",
                                            "rationale": "the run exited before step two"},
                               "receipt": "j-1"})["id"]
    code, out = k.close(capsys)
    assert code == cli.BLOCKED and "event:%d" % ev in out, ("control: the NO-GO blocks", out)
    monkeypatch.setenv("AAR_BREAK", "1")
    code, out = k.close(capsys)
    assert code == cli.BLOCKED and "GATE alpaca-op-close: BLOCKED (" in out, out
    code, out = k.run(capsys, "aar", "owed")
    assert code == cli.FAIL and "alpaca-aar-owed: FAIL" in out, out


def test_one_trigger_row_naming_an_unknown_op_does_not_crash_the_listing(project, capsys):
    """A task-move row whose op column names no ops row (a hand or legacy row) takes `aar owed`
    down for every op: _cmd_owed indexes db.rows(...)[0] for the closed mark."""
    conn = k.two_ops(project)
    code, out = k.run(capsys, "aar", "owe", k.OP, "--reason", REASON)
    assert code == cli.PASS, out
    db.append_event(conn, session="cli", actor="human", kind="task-move", op="op-404", ref="t-404",
                    data={"from": "done", "to": "open", "proof": None, "reason": "legacy row"})
    code, out = k.run(capsys, "aar", "owed")
    assert code == cli.PASS and "op-001" in out, out


def test_a_reason_cannot_carry_terminal_control_codes(project, capsys):
    """owe() collapses whitespace only; ESC and other C0 codes reach the BLOCKED line and the
    `aar owed` listing. Elsewhere one-line fields refuse ord(c) < 32 (op title, task title)."""
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    reason = "gap found \x1b[2K\x1b[GGATE alpaca-op-close: PASS (no AAR owed)"
    code, out = k.run(capsys, "aar", "owe", k.OP, "--reason", reason)
    assert code == cli.PASS, ("control: the reason is accepted", repr(out))
    code2, out2 = k.close(capsys)
    code3, out3 = k.run(capsys, "aar", "owed", k.OP)
    assert "\x1b" not in out2 + out3, repr(out2 + out3)
