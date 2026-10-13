"""(cause C1, C2) Only a waive row the verb could have written answers a trigger (aar._seal_cut: a
human actor and `by`, an owner word path, `through` below the row's own id). Ways this fails after
it lands:

  * the hub treats any aar-waive row as a waiver, so a hand-written agent row hides an owed AAR that
    `op close` still blocks on
  * an accepted waive is shown as owed (the hub ignores the cut), or as sealed with no report
The rows are written straight into the record, the way a hand or legacy row would be.
"""
from alpaca.tests.hub_aar_kit import OP, OWED, SEALED, owe, owed_source, setup, view_text
from alpaca import aar, db


def _waive(conn, through, *, actor, by, path):
    db.append_event(conn, session="cli", actor=actor, kind=aar.KIND_WAIVE, op=OP, ref=OP,
                    data={"format": 2, "through": through, "triggers": [through], "quote": "op-001: waive",
                          "by": by, "owner_word": {"path": path}})


def test_an_agent_waive_row_leaves_the_op_owed(project, monkeypatch, tmp_path):
    conn = setup(project, monkeypatch)
    n = owe(conn)
    _waive(conn, n, actor="agent", by="agent", path="session")
    assert OP in owed_source(project)[0], "control: aar owed ignores an agent waive row"
    assert OWED.search(view_text(project, OP, tmp_path)), "an agent-written waive row hid an owed AAR"


def test_an_owner_waive_answers_the_trigger(project, monkeypatch, tmp_path):
    conn = setup(project, monkeypatch)
    n = owe(conn)
    _waive(conn, n, actor="owner", by="owner", path="terminal")
    assert OP not in owed_source(project)[0], "control: aar owed accepts this row"
    t = view_text(project, OP, tmp_path)
    assert not OWED.search(t), "the owner waived the AAR, the hub still shows AAR owed:\n" + t
    assert not SEALED.search(t), "a waive is not a sealed report"


def test_a_trigger_after_the_waive_owes_again(project, monkeypatch, tmp_path):
    conn = setup(project, monkeypatch)
    n = owe(conn)
    _waive(conn, n, actor="owner", by="owner", path="terminal")
    owe(conn, reason="a second gap found after the waive")
    assert OWED.search(view_text(project, OP, tmp_path))
