"""op-019 t-210 (design 4): owner orders. An order is a record row with the owner's quoted words and
a pointer; an L6 door stays PAUSED while one applies; a handoff or landing note that drops one fails
its check; only the owner closes one."""
import io
import os
from contextlib import redirect_stdout

import pytest

from alpaca import cli, db, intentgate, messages, orders, ownerword, pad, proof
from alpaca.gates import verdict as vc
from alpaca.tests import proofkit
from alpaca.tests.ownerword_fixtures import EARLY, LATE, Project, queued, typed

ORDER = "do not open the build door until I say so"


@pytest.fixture
def p(tmp_path, monkeypatch):
    proj = Project(tmp_path, monkeypatch)
    proj.op()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", proj.sid)
    return proj


def add(p, quote=ORDER, door="design->build", **kw):
    kw.setdefault("environ", p.env)
    return orders.add(p.conn, p.sid, "op-1", quote, door, "owner", root=p.root, **kw)


def task(p, tid="t-1", phase="build", op="op-1"):
    db.upsert(p.conn, "tasks", "id", {"id": tid, "op": op, "phase": phase, "statement": "s",
                                      "status": "open", "title": "t"})
    return dict(p.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())


def batch(p):
    db.append_event(p.conn, session="s", actor="alpaca", kind="batch-item", op="op-1", ref="runtime",
                    data={"state": "open"})


def run(p, monkeypatch, *argv):
    monkeypatch.chdir(p.root)
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(list(argv))
    return code, out.getvalue()


# (1) an owner order is a row with the owner's quoted words and a pointer
def test_order_row_has_the_quote_and_the_pointer(p):
    p.write([typed("Please " + ORDER + ", that is final.")])
    o = add(p)
    assert o["id"] == "ord-1" and o["status"] == "open"
    row = p.conn.execute("SELECT * FROM events WHERE kind='owner-order'").fetchone()
    assert row["ref"] == "ord-1" and row["op"] == "op-1"
    got = orders.open_orders(p.conn, "op-1")[0]
    assert got["quote"] == ORDER and got["door"] == "design->build"
    word = got["owner_word"]
    assert word["transcript"] == p.transcript_of(p.sid) and word["line"] == 1 and word["timestamp"]


def test_order_refused_without_owner_words_or_short_or_early(p):
    p.write([typed("hello there")])
    with pytest.raises(ownerword.OwnerWordRefusal):
        add(p)                                                   # not in any owner message
    p.write([typed("stop now please")])
    with pytest.raises(ownerword.OwnerWordRefusal):
        add(p, quote="stop now")                                  # under three words
    p.write([typed(ORDER, ts=EARLY)])
    with pytest.raises(ownerword.OwnerWordRefusal):
        add(p)                                                   # typed before the op opened
    with pytest.raises(orders.OrderError):
        add(p, door="whenever")
    assert orders.all_orders(p.conn) == []


def test_mid_turn_queued_command_order_is_accepted(p):
    p.write([typed("go on"), queued("also " + ORDER + " ok")])
    o = add(p)
    assert o["owner_word"]["line"] == 2 and o["status"] == "open"


# (2) closing: only the owner
def test_agent_cannot_close_and_owner_words_can(p):
    p.write([typed(ORDER)])
    add(p)
    with pytest.raises(ownerword.OwnerWordRefusal):
        orders.close(p.conn, p.sid, "ord-1", "agent", "ord-1 resume", root=p.root, environ=p.env)
    with pytest.raises(ownerword.OwnerWordRefusal):
        orders.close(p.conn, p.sid, "ord-1", "owner", "ord-1 resume", root=p.root,
                     environ=p.env)                               # owner never typed it
    assert orders.open_orders(p.conn, "op-1")
    p.write([typed(ORDER), typed("ord-1 resume")])
    orders.close(p.conn, p.sid, "ord-1", "owner", "ord-1 resume", root=p.root, environ=p.env)
    assert orders.open_orders(p.conn, "op-1") == []
    closed = orders.all_orders(p.conn, "op-1")[0]
    assert closed["status"] == "closed" and closed["closed_by"]["line"] == 2
    with pytest.raises(orders.OrderError):
        orders.close(p.conn, p.sid, "ord-1", "owner", "ord-1 resume", root=p.root, environ=p.env)


def test_door_is_paused_at_l6_while_an_order_applies_and_passes_after_close(p):
    assert orders.door_link(p.conn, "op-1", "design->build", level=6, session=p.sid,
                            root=p.root) is None                  # no order, no link
    p.write([typed(ORDER), typed("ord-1 done")])
    add(p)
    link = intentgate.door_links(p.conn, "op-1", "design->build", level_flag="L6", session=p.sid,
                                 root=p.root)
    assert ("owner-orders", vc.PAUSED) in link
    other = orders.door_link(p.conn, "op-1", "build->verify", level=6, session=p.sid, root=p.root)
    assert other == ("owner-orders", vc.PASS)                      # binds another boundary only
    orders.close(p.conn, p.sid, "ord-1", "owner", "ord-1 done", root=p.root, environ=p.env)
    assert orders.door_link(p.conn, "op-1", "design->build", level=6, session=p.sid,
                            root=p.root) == ("owner-orders", vc.PASS)


def test_any_order_pauses_every_boundary(p):
    p.write([typed(ORDER)])
    add(p, door="any")
    for b in orders.BOUNDARIES:
        assert orders.door_link(p.conn, "op-1", b, level=6, session=p.sid,
                                root=p.root) == ("owner-orders", vc.PAUSED)


def test_later_phase_claim_and_move_refused_phaseless_counts_after_every_boundary(p):
    batch(p)
    p.write([typed(ORDER)])
    add(p)                                                        # design->build
    for action in ("claim", "doing", "review", "done"):
        with pytest.raises(intentgate.IntentGateRefusal) as e:
            intentgate.task_gate(p.conn, task(p, "t-b", "build"), action, session=p.sid,
                                 root=p.root)
        assert e.value.reason == "ORDER-OPEN" and "ord-1" in str(e.value)
    intentgate.task_gate(p.conn, task(p, "t-d", "design"), "claim", session=p.sid, root=p.root)
    with pytest.raises(intentgate.IntentGateRefusal):
        intentgate.task_gate(p.conn, task(p, "t-n", None), "doing", session=p.sid, root=p.root)
    with pytest.raises(intentgate.IntentGateRefusal):
        intentgate.task_gate(p.conn, task(p, "t-x", "weird"), "doing", session=p.sid, root=p.root)
    intentgate.task_gate(p.conn, task(p, "t-b"), "open", session=p.sid, root=p.root)  # not gated


def test_task_gate_leaves_an_op_without_a_batch_alone(p):
    p.write([typed(ORDER)])
    add(p, door="any")
    intentgate.task_gate(p.conn, task(p), "claim", session=p.sid, root=p.root)   # design D-1


# (3) handoffs and landing notes
def test_handoff_dropping_an_order_is_refused_naming_it_is_accepted(p):
    p.write([typed(ORDER)])
    add(p)
    with pytest.raises(messages.MessageError) as e:
        messages.post(p.conn, "a", "op-1", "handoff", "all done, nothing open", session=p.sid,
                      root=p.root)
    assert "ORDER-DROPPED" in str(e.value) and "ord-1" in str(e.value)
    assert p.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    messages.post(p.conn, "a", "op-1", "handoff", "open order ord-1 holds the build door",
                  session=p.sid, root=p.root)
    messages.post(p.conn, "a", "op-1", "note", "no order named, a note is fine", session=p.sid,
                  root=p.root)


def test_task_ref_handoff_resolves_to_its_op_and_unresolved_checks_every_op(p):
    p.write([typed(ORDER)])
    add(p)
    task(p, "t-9", "build")
    with pytest.raises(messages.MessageError):
        messages.post(p.conn, "a", "someone", "handoff", "nothing", pointer="t-9", session=p.sid,
                      root=p.root)
    with pytest.raises(messages.MessageError):
        messages.post(p.conn, "a", "someone", "handoff", "nothing", session=p.sid, root=p.root)
    messages.post(p.conn, "a", "someone", "handoff", "ord-1 is open", pointer="t-9",
                  session=p.sid, root=p.root)
    p.op("op-2")
    messages.post(p.conn, "a", "op-2", "handoff", "nothing open here", session=p.sid, root=p.root)


def test_check_note_on_a_landing_file(p, monkeypatch):
    p.write([typed(ORDER)])
    add(p)
    note = os.path.join(p.root, "LANDING.md")
    open(note, "w").write("Landed. Nothing else.\n")
    code, out = run(p, monkeypatch, "order", "check-note", "LANDING.md")
    assert code == cli.FAIL and "ORDER-DROPPED" in out and "ord-1" in out
    open(note, "w").write("Landed. Order ord-1 still holds.\n")
    code, out = run(p, monkeypatch, "order", "check-note", "LANDING.md", "--op", "op-1")
    assert code == cli.PASS


def test_proof_seal_runs_the_note_check_on_landing_and_handoff_files(project):
    cli.main(["init"])
    cli.main(["op", "new", "ship it", "--done-when", "shipped"])
    cli.main(["task", "add", "--title", "task", "op-001", "write the thing", "--phase", "build"])
    conn = db.connect(project)
    db.append_event(conn, session="s", actor="owner", kind="owner-order", op="op-001", ref="ord-1",
                    data={"id": "ord-1", "op": "op-001", "quote": "hold the release door",
                          "door": "any", "owner_word": {}})
    ptr = proofkit.evidence_file(project, "t-001", "Landed. Nothing else.\n")
    note = os.path.join(project, "LANDING-NOTE.md")
    open(note, "w").write("Landed. Nothing else.\n")
    proofkit.write_report(project, "t-001", conn=conn, evidence=["local:LANDING-NOTE.md", ptr])
    with pytest.raises(proof.Refusal) as e:
        proof.seal(conn, project, "t-001")
    assert any("ORDER-DROPPED" in x and "ord-1" in x for x in e.value.problems)
    open(note, "w").write("Landed. ord-1 still holds the release door.\n")
    proofkit.write_report(project, "t-001", conn=conn, evidence=["local:LANDING-NOTE.md", ptr])
    assert proof.seal(conn, project, "t-001")["sha256"]


# lifting, not-order, accounting, pad, items
def test_lifted_by_records_the_order_closed_with_a_derived_choice(p):
    p.write([typed(ORDER), typed("ok you can go ahead now", ts="2999-01-02T00:00:00.000Z")])
    o = add(p, lifted_by="ok you can go ahead now")
    assert o["status"] == "closed" and orders.open_orders(p.conn, "op-1") == []
    ev = p.conn.execute("SELECT * FROM events WHERE kind='derived-choice'").fetchone()
    assert ev["op"] == "op-1" and "order-lifted" in ev["data"]
    with pytest.raises(ownerword.OwnerWordRefusal):               # not a whole owner message
        add(p, lifted_by="you can go")


def test_not_order_is_a_derived_choice_and_accounts_for_the_message(p):
    msg = "what do you think about the build door"
    assert not orders.accounts_for(p.conn, "op-1", msg)
    orders.not_order(p.conn, p.sid, "op-1", msg, "a question, no instruction", environ=p.env)
    assert orders.accounts_for(p.conn, "op-1", msg)
    assert p.conn.execute("SELECT COUNT(*) FROM events WHERE kind='derived-choice'").fetchone()[0] == 1
    with pytest.raises(orders.OrderError):
        orders.not_order(p.conn, p.sid, "op-1", msg, " ")


def test_accounts_for_an_open_or_closed_order_quote(p):
    p.write([typed(ORDER), typed("ord-1 resume")])
    add(p)
    assert orders.accounts_for(p.conn, "op-1", "Boss said: " + ORDER + "!")
    orders.close(p.conn, p.sid, "ord-1", "owner", "ord-1 resume", root=p.root, environ=p.env)
    assert orders.accounts_for(p.conn, "op-1", ORDER)
    assert not orders.accounts_for(p.conn, "op-1", "something else")
    assert not orders.accounts_for(p.conn, "op-2", ORDER)


def test_pad_lists_open_orders_and_owner_items_return_them(p):
    p.write([typed(ORDER)])
    add(p)
    lines = "\n".join(pad.resume_point_lines(p.conn, ["op-1"]))
    assert "open owner order ord-1" in lines and "design->build" in lines
    items = orders.owner_items(p.conn, "op-1", root=p.root)
    assert items[0]["id"] == "ord-1" and items[0]["quote"] == ORDER and items[0]["status"] == "open"
    assert intentgate.owner_items(p.conn, "op-1", root=p.root)


def test_cli_add_list_close_and_not_order(p, monkeypatch):
    p.write([typed(ORDER), typed("ord-1 lift")])
    code, out = run(p, monkeypatch, "order", "add", "op-1", "--quote", ORDER, "--door", "any",
                    "--by", "owner")
    assert code == cli.PASS and "ord-1 open" in out
    code, out = run(p, monkeypatch, "order", "list", "--open")
    assert "ord-1 [open]" in out
    code, out = run(p, monkeypatch, "order", "close", "ord-1", "--by", "owner", "--quote", "ord-1 lift")
    assert code == cli.PASS
    code, out = run(p, monkeypatch, "order", "list", "--open")
    assert "no orders" in out
    code, out = run(p, monkeypatch, "order", "not-order", "op-1", "--quote", "hmm what now",
                    "--reason", "a question")
    assert code == cli.PASS
    code, out = run(p, monkeypatch, "order", "close", "ord-1", "--by", "agent", "--quote", "x")
    assert code == cli.FAIL


# round 1 fix: adversarial findings 1 and 2

def test_fix_r1_1_terminal_add_records_the_owner_words(p, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    o = orders.add(p.conn, "cli", "op-1", ORDER, "design->build", "owner", root=p.root,
                   environ={}, isatty=lambda: True, read=lambda prompt: "op-1")
    assert o["quote"] == ORDER, "recorded quote is %r" % o["quote"]

def test_fix_r1_1b_terminal_order_cannot_be_dropped_by_naming_the_op(p, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    orders.add(p.conn, "cli", "op-1", ORDER, "design->build", "owner", root=p.root,
               environ={}, isatty=lambda: True, read=lambda prompt: "op-1")
    # a handoff that names the op but not the order or its words must be refused
    with pytest.raises(messages.MessageError):
        messages.post(p.conn, "a", "op-1", "handoff", "op-1 all done, nothing open",
                      session="cli", root=p.root)

def test_fix_r1_1c_terminal_order_quote_does_not_account_for_every_message(p, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    orders.add(p.conn, "cli", "op-1", ORDER, "design->build", "owner", root=p.root,
               environ={}, isatty=lambda: True, read=lambda prompt: "op-1")
    assert not orders.accounts_for(p.conn, "op-1", "for op-1 also never push to main")


# A2: a not-order record with a tiny quote, or a quote that is not any owner message, accounts for
# a real order message the self-check (t-203) would otherwise report as dropped-order. The agent
# needs no owner word for this: the fail case (an order dropped without the owner) stays open.

def test_fix_r1_2_tiny_not_order_quote_does_not_account_for_an_order(p):
    msg = "never push to main until I review the diff"
    assert not orders.accounts_for(p.conn, "op-1", msg)
    orders.not_order(p.conn, p.sid, "op-1", "the", "agent reading", environ=p.env)
    assert not orders.accounts_for(p.conn, "op-1", msg), "a not-order quote 'the' accounts for it"

def test_fix_r1_2b_superstring_not_order_does_not_account_for_many_messages(p):
    m1 = "never push to main until I review the diff"
    m2 = "stop before the release door and ask me"
    orders.not_order(p.conn, p.sid, "op-1", "chit chat. " + m1 + " " + m2 + " bye",
                     "agent reading", environ=p.env)
    assert not (orders.accounts_for(p.conn, "op-1", m1) and orders.accounts_for(p.conn, "op-1", m2))
