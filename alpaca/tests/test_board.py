"""M2.3 proof - the board, a kanban view derived purely over obligation rows.

Asserts the Done-when on BOTH the positive and the negative path:

  * the four column mappings are DERIVED, never stored: open is todo, open with a live
    claim is doing, discharged or waived is done, blocked or failed is blocked WITH the
    reason (spec 5.7:453-459).
  * a hand-written `status` column on the row is IGNORED: the view re-derives from the
    verdict rows and the live claims, so a column can never be a claim that outlives its
    evidence.
  * every card move is exactly ONE event (spec: "card move = one alpaca verb = one event").
  * a swimlane per phase, one board per op, and the project board over every op.
  * negative paths: an unknown column and a move against an absent row both HALT.
  * the CLI boundary `alpaca board show|move` obeys the verdict contract, and `board.json`
    renders as a projection.
"""
import json
import os

import pytest

from alpaca import board, cli, db, util
from alpaca.checklist import Halt, verdict_row
from alpaca.gates import verdict as vc
from alpaca.tests import proofkit


# --------------------------------------------------------------- fixtures / helpers
def _setup(project):
    cli.main(["init"])
    return db.connect(project)


def _row(conn, rid, *, op=None, phase="build", step="s1",
         statement="do the thing properly here now", why="", where_="", proof="local:spec.md"):
    """Land one obligation row directly (the M2.1 mapping is exercised elsewhere; here the
    row is a fixture so the pure derivation has real rows to fold). content_hash is a stable
    witness over the id so a board move can bind to it."""
    r = {
        "id": rid, "kind": "item", "op": op, "phase": phase, "step": step,
        "statement": statement, "proof": proof, "where_": where_, "how": "", "when_": "",
        "why": why, "session": None, "operator": None, "status": "open", "tag": "Specced",
        "content_hash": util.sha256_hex("row/" + rid), "prev_hash": None, "supersedes": None,
    }
    db.upsert(conn, "rows", "id", r)
    return r


def _card(view, rid):
    for c in view["cards"]:
        if c["row_id"] == rid:
            return c
    raise AssertionError("row %r not on the board" % rid)


def _n_events(conn):
    return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


# --------------------------------------------------------------- the four column mappings
def test_open_row_is_todo(project):
    conn = _setup(project)
    r = _row(conn, "r-open")
    v = board.view(conn)
    assert _card(v, r["id"])["column"] == board.TODO


def test_open_with_a_live_claim_is_doing(project):
    conn = _setup(project)
    r = _row(conn, "r-claim")
    board.claim(conn, r["id"], worker="alice", session="s1")
    c = _card(board.view(conn), r["id"])
    assert c["column"] == board.DOING
    assert c["claimant"] == "alice"


def test_discharged_or_waived_is_done(project):
    conn = _setup(project)
    a = _row(conn, "r-done")
    b = _row(conn, "r-waived")
    verdict_row.discharge(conn, a["id"], a["content_hash"], "probe", vc.PASS,
                          ["local:spec.md"], "L2", "s1")
    verdict_row.waive(conn, b["id"], b["content_hash"], reason="out of scope", level="L2",
                      session="s1")
    v = board.view(conn)
    assert _card(v, a["id"])["column"] == board.DONE
    assert _card(v, b["id"])["column"] == board.DONE


def test_blocked_or_failed_is_blocked_with_reason(project):
    conn = _setup(project)
    a = _row(conn, "r-blocked")
    b = _row(conn, "r-failed")
    # a: blocked through the board, carrying a reason the card must surface.
    board.move(conn, a["id"], board.BLOCKED, actor="alice", reason="waiting on the door")
    # b: a FAIL verdict authored directly folds to failed, which the board shows as blocked.
    verdict_row.discharge(conn, b["id"], b["content_hash"], "probe", vc.FAIL, [], "L2", "s1")
    v = board.view(conn)
    ca, cbl = _card(v, a["id"]), _card(v, b["id"])
    assert ca["column"] == board.BLOCKED
    assert ca["reason"] == "waiting on the door"
    assert cbl["column"] == board.BLOCKED


# --------------------------------------------------------------- hand-written column ignored
def test_a_hand_written_column_value_is_ignored(project):
    conn = _setup(project)
    r = _row(conn, "r-hand")
    # someone hand-writes the stored status column to a finished-looking value.
    db.patch(conn, "rows", "id", r["id"], {"status": "done"})
    # the board ignores it: with no verdict row and no claim, the row is still todo.
    assert _card(board.view(conn), r["id"])["column"] == board.TODO


# --------------------------------------------------------------- one move = one event
def test_a_claim_move_is_exactly_one_event(project):
    conn = _setup(project)
    r = _row(conn, "r-1claim")
    before = _n_events(conn)
    board.move(conn, r["id"], board.DOING, actor="alice")
    assert _n_events(conn) - before == 1


def test_a_done_move_is_exactly_one_event(project):
    conn = _setup(project)
    r = _row(conn, "r-1done")
    before = _n_events(conn)
    board.move(conn, r["id"], board.DONE, actor="alice", proof="local:spec.md")
    assert _n_events(conn) - before == 1
    assert _card(board.view(conn), r["id"])["column"] == board.DONE


def test_a_blocked_move_is_exactly_one_event(project):
    conn = _setup(project)
    r = _row(conn, "r-1blocked")
    before = _n_events(conn)
    board.move(conn, r["id"], board.BLOCKED, actor="alice", reason="blocked here")
    assert _n_events(conn) - before == 1


def test_releasing_a_claim_returns_the_row_to_todo(project):
    conn = _setup(project)
    r = _row(conn, "r-release")
    board.move(conn, r["id"], board.DOING, actor="alice")
    assert _card(board.view(conn), r["id"])["column"] == board.DOING
    before = _n_events(conn)
    board.move(conn, r["id"], board.TODO, actor="alice")
    assert _n_events(conn) - before == 1
    assert _card(board.view(conn), r["id"])["column"] == board.TODO


# --------------------------------------------------------------- swimlanes and per-op boards
def test_a_swimlane_per_phase(project):
    conn = _setup(project)
    _row(conn, "r-req", phase="requirement")
    _row(conn, "r-bld", phase="build")
    v = board.view(conn)
    phases = {lane["phase"] for lane in v["swimlanes"]}
    assert {"requirement", "build"} <= phases


def test_one_board_per_op_and_the_project_board_over_all(project):
    conn = _setup(project)
    _row(conn, "r-a", op="op-001")
    _row(conn, "r-b", op="op-002")
    # a per-op board carries only that op's rows.
    only_a = {c["row_id"] for c in board.view(conn, op="op-001")["cards"]}
    assert only_a == {"r-a"}
    # the project board (op=None) carries every op.
    allrows = {c["row_id"] for c in board.view(conn)["cards"]}
    assert {"r-a", "r-b"} <= allrows


# --------------------------------------------------------------- the card carries its fields
def test_card_carries_tag_proof_where_why_and_claimant(project):
    conn = _setup(project)
    r = _row(conn, "r-fields", where_="host:/tmp", why="local:decisions/why.md",
             proof="local:spec.md")
    board.claim(conn, r["id"], worker="alice", session="s1")
    c = _card(board.view(conn), r["id"])
    assert c["tag"] == "Specced"
    assert c["proof"] == "local:spec.md"
    assert c["where"] == "host:/tmp"
    assert c["why"] == "local:decisions/why.md"
    assert c["claimant"] == "alice"


# --------------------------------------------------------------- negative paths
def test_an_unknown_column_halts(project):
    conn = _setup(project)
    r = _row(conn, "r-badcol")
    with pytest.raises(Halt) as ex:
        board.move(conn, r["id"], "in-review", actor="alice")
    assert ex.value.verdict == vc.BLOCKED
    assert ex.value.code == board.R_UNKNOWN_COLUMN


def test_a_move_against_an_absent_row_halts(project):
    conn = _setup(project)
    with pytest.raises(Halt) as ex:
        board.move(conn, "no-such-row", board.DONE, actor="alice", proof="local:spec.md")
    assert ex.value.verdict == vc.BLOCKED


# --------------------------------------------------------------- CLI boundary + projection
def test_cli_board_show_and_move_and_projection(project):
    conn = _setup(project)
    _row(conn, "r-cli")
    assert cli.main(["board", "show"]) == vc.PASS
    # board.json renders as a projection in the root.
    assert os.path.isfile(os.path.join(project, "board.json"))
    data = json.loads(util.read_text(os.path.join(project, "board.json")))
    assert any(c["row_id"] == "r-cli" for c in data["cards"])
    # a move over the CLI is a PASS and lands the derived column.
    assert cli.main(["board", "move", "r-cli", "doing", "--by", "alice"]) == vc.PASS
    assert _card(board.view(db.connect(project)), "r-cli")["column"] == board.DOING


def test_cli_board_move_done_requires_proof(project):
    conn = _setup(project)
    _row(conn, "r-cliproof")
    assert cli.main(["board", "move", "r-cliproof", "done"]) == vc.FAIL
    # E4: done over the CLI needs the SEALED proof report, not a bare pointer.
    assert cli.main(["board", "move", "r-cliproof", "done", "--proof", "local:ALPACA-MANIFEST"]) == vc.FAIL
    ptr = proofkit.seal_for(project, "r-cliproof")
    assert cli.main(["board", "move", "r-cliproof", "done", "--proof", ptr]) == vc.PASS
