"""M2.19 proof - M2 acceptance: two agents over one op through the board (spec 10 M2, 7.3).

This module discharges the whole M2 milestone through the REAL entry points, on a throwaway
project root (the conftest `project` fixture, never the live .alpaca/). It proves the four Done-when
clauses the spec sets for M2:

  A. Two worker identities drive ONE op over the board: each claims a row, works it, authors a
     verdict row (board.move done => verdict_row.discharge), posts a result message, and the board
     folds the cards. A FixedClock (alpaca/clock.py) makes every lease instant deterministic.

  B. `alpaca export` (alpaca/export.py) renders one data.json from the record; the rendered data.json
     bytes carry the op id, BOTH worker ids and every moved card's row id, and its `board` section
     parses to the SAME card-to-column mapping as board.view. The record holds one event per move
     and no hand-edited column (verify_chain green). (The static board page that once rendered
     this payload is retired; the cockpit reads the same payload at `/board/data.json`.)

  C. A lease expires under the FixedClock; the row returns to todo and the lease-expired event id
     (its integer id and its hash-chain hash) is present in BOTH the record and the exported
     data.json bytes. Because export.py surfaces no raw event id, the expiry event id reaches the
     payload through the native channel it is meant to: a `result` message (the native channel rule) that cites
     the expiry event, folded into data.json.messages.

  D. A tiny wiki vault is seeded through the M2.14 absorb path and the wiki answer door is called
     (alpaca/wiki/engine/answer.answer, the seam PART A wired). The result is EITHER an Answer whose
     currency_stamp and completeness are both non-null with at least one resolvable pointer (a
     citation whose source_block_id/source_edge_id resolves in the store), OR an Abstain (verdict
     'abstained') carrying its reason code.

The board/record substrate (a throwaway project root) and the wiki substrate (a throwaway vault
dir with its own rune.db) are independent; the wiki keeps its own clock (alpaca.wiki.clock), never
alpaca.clock, so the blindness/separation M2 relies on is preserved here too.
"""
import json

import pytest

from alpaca import board, claims, clock, db, export, messages, util
from alpaca.checklist import verdict_row
from alpaca.gates import verdict as vc

# ---- fixed instants (offset-bearing, whole-second: matches the clock and the lease math) --------
T0 = "2026-03-01T00:00:00+00:00"          # every claim and discharge is stamped here
T_LATE = "2026-03-01T01:00:00+00:00"      # well past the short lease; the board re-derives at this now
OP = "op-001"
W1, W2 = "w1", "w2"                        # the two worker identities
DONE_ROWS = ("r-1", "r-2")                # worked to done by w1 and w2
EXPIRE_ROW = "r-3"                         # claimed short, then left to expire
MOVED_ROWS = ("r-1", "r-2", "r-3")        # every card a verb moved
#: every payload key the cockpit reads.
KEYS = ("board", "messages", "pulse", "gates", "wiki", "runs", "metrics")


# ---- obligation-row seeding (same shape the M2.18 export test uses) -----------------------------
def _seed_row(conn, rid, *, phase):
    db.upsert(conn, "rows", "id", {
        "id": rid, "kind": "item", "op": OP, "phase": phase, "step": "s1",
        "statement": "an obligation for %s to fold on the board" % rid,
        "proof": "local:spec.md", "where_": "", "how": "", "when_": "", "why": "",
        "session": None, "operator": None, "status": "open", "tag": "Specced",
        "content_hash": util.sha256_hex("row/" + rid),
        "prev_hash": None, "supersedes": None, "superseded_by": None})


def _column_map(board_view: dict) -> dict:
    """row_id -> derived column, the board's card-to-column mapping."""
    return {c["row_id"]: c["column"] for c in board_view["cards"]}


# ---- the fixture: two workers drive one op over the board, then a lease expires -----------------
@pytest.fixture
def worked(project):
    """Build the record for one op: seed rows, run both workers to done, claim r-3 short, then
    let its lease expire under the advanced FixedClock. Yields (root, conn, expiry_event)."""
    util.set_clock(clock.FixedClock(T0, step=0))
    conn = db.connect(project)

    # the op and its three obligation rows.
    db.upsert(conn, "ops", "id", {
        "id": OP, "intent": "two agents over the board", "done_when": "board green",
        "status": "open", "opened": util.now_iso(), "closed": None, "phases": None})
    _seed_row(conn, "r-1", phase="build")
    _seed_row(conn, "r-2", phase="verify")
    _seed_row(conn, EXPIRE_ROW, phase="build")

    # each worker takes its row (a long lease so only r-3 expires later), works it to done (which
    # AUTHORS the verdict row), and posts a result message citing the discharged row.
    for rid, worker in ((("r-1"), W1), (("r-2"), W2)):
        board.move(conn, rid, board.DOING, worker, session=worker, minutes=100000)
        board.move(conn, rid, board.DONE, worker, proof="local:spec.md", level="L3",
                   session=worker)
        messages.post(conn, worker, "broadcast", "result",
                      "%s discharged by %s" % (rid, worker), pointer=rid,
                      session=worker, root=project)

    # r-3: a short 30-minute lease taken at T0 (claims.take is the M2.5 formalized claim path).
    claims.take(conn, EXPIRE_ROW, W1, minutes=30, session=W1, now=T0)

    # advance the clock past the lease and expire it: the row returns to todo and a lease-expired
    # event lands on the record. The board re-derives at this later now, so r-3 is no longer doing.
    util.set_clock(clock.FixedClock(T_LATE, step=0))
    freed = claims.expire_due(conn, now=T_LATE)
    assert EXPIRE_ROW in freed, "the short lease expired and r-3 was freed"

    # the expiry event on the record (its id and hash are the expiry event id we trace).
    expiry = [e for e in db.events(conn, kind=claims.EXPIRE_KIND, limit=10 ** 9)
              if e["ref"] == EXPIRE_ROW]
    assert len(expiry) == 1, "exactly one lease-expired event for r-3"
    expiry_ev = expiry[0]

    # push the expiry event id onto the record through the native channel (a result message), so
    # it reaches data.json (export.py surfaces no raw event id).
    messages.post(conn, "alpaca", "broadcast", "result",
                  "lease-expired event id=%s hash=%s returned %s to todo"
                  % (expiry_ev["id"], expiry_ev["hash"], EXPIRE_ROW),
                  pointer=EXPIRE_ROW, session="alpaca", root=project)

    yield project, conn, expiry_ev
    conn.close()
    util.set_clock(None)


# ================================================================ Done-when A: two agents, one op
def test_two_workers_drive_one_op_to_done_and_the_board_folds(worked):
    root, conn, _ = worked
    v = board.view(conn, op=OP)
    cols = _column_map(v)
    # each worker's row folded to done off its verdict row; the discharge authored the verdict.
    for rid in DONE_ROWS:
        assert cols[rid] == board.DONE, "%s folded to done: %r" % (rid, cols)
        assert verdict_row.status_fold(conn, rid) == verdict_row.DISCHARGED
    # both worker identities are on the op's cards (as claimants) - two distinct agents worked it.
    claimants = {c["claimant"] for c in v["cards"] if c["claimant"]}
    assert {W1, W2} <= claimants, claimants
    # every discharge is one verdict event; every claim is one claim event: one event per move.
    verdicts = [e for e in db.events(conn, kind=verdict_row.KIND, limit=10 ** 9)]
    assert len(verdicts) == len(DONE_ROWS)
    ok, detail = db.verify_chain(conn)
    assert ok, "the record's hash chain verifies (no hand-edited column): %s" % detail


def test_result_messages_are_on_the_record(worked):
    root, conn, _ = worked
    bodies = [m["body"] for m in messages.read(conn)]
    for rid, worker in zip(DONE_ROWS, (W1, W2)):
        assert any(("%s discharged by %s" % (rid, worker)) == b for b in bodies), bodies


# ================================================================ Done-when B: export bytes
def test_data_json_carries_op_workers_and_moved_rows(worked):
    root, conn, _ = worked
    data_text = export.render(root)
    data = json.loads(data_text)                       # data.json parses
    assert set(KEYS) <= set(data)                       # every key the cockpit reads is on the payload

    needles = (OP, W1, W2) + MOVED_ROWS
    for needle in needles:
        assert needle in data_text, "data.json is missing %r" % needle


def test_data_json_board_section_matches_board_view(worked):
    root, conn, _ = worked
    exported = json.loads(export.render(root))["board"]
    # the mapping parsed from data.json equals the mapping board.view derives from the record.
    assert _column_map(exported) == _column_map(board.view(conn))
    # and it is the folded shape we expect: two done, one back in todo.
    cols = _column_map(exported)
    for rid in DONE_ROWS:
        assert cols[rid] == board.DONE
    assert cols[EXPIRE_ROW] == board.TODO


# ================================================================ Done-when C: lease expiry event
def test_expired_row_returns_to_todo_with_its_event_id_on_record_and_in_data_json(worked):
    root, conn, expiry_ev = worked
    # the row returned to todo (the board re-derives it there once the lease passed).
    assert board.view(conn)["by_column"][board.TODO]
    assert _column_map(board.view(conn))[EXPIRE_ROW] == board.TODO

    # the expiry event is on the record: a lease-expired event bound to r-3 with a chained hash.
    on_record = [e for e in db.events(conn, kind=claims.EXPIRE_KIND, limit=10 ** 9)
                 if e["ref"] == EXPIRE_ROW]
    assert len(on_record) == 1
    assert on_record[0]["id"] == expiry_ev["id"]
    assert on_record[0]["hash"] == expiry_ev["hash"]

    # the expiry event id (integer id AND hash-chain hash) is present in the exported data.json.
    data_text = export.render(root)
    for token in (str(expiry_ev["id"]), expiry_ev["hash"]):
        assert token in data_text, "data.json is missing expiry id token %r" % token


# ================================================================ Done-when D: the wiki answer door
def _seed_vault(vault_dir):
    """Seed a tiny wiki vault through the M2.14 absorb path (one fact) and return its Config. The
    wiki keeps its OWN clock (alpaca.wiki.clock), never alpaca.clock, so this never touches the board's."""
    from alpaca.wiki.clock import FixedClock as WikiFixedClock
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import Absorber
    vault_dir.mkdir(parents=True, exist_ok=True)   # the store pins an existing vault directory
    cfg = Config.for_vault(vault_dir)
    a = Absorber(cfg, clock=WikiFixedClock(start="2020-01-01T00:00:00+00:00"))
    try:
        a.absorb_text("ada.md", "[[Ada]] works at [[Acme]].\n")
    finally:
        a.close()
    return cfg


def _pointer_resolves(cfg, ans) -> bool:
    """A citation's source_block_id or source_edge_id resolves in the wiki store."""
    from alpaca.wiki.store.db import DB
    dbh = DB(cfg)
    try:
        for c in ans.citations:
            if c.source_block_id and dbh.conn.execute(
                    "SELECT 1 FROM blocks WHERE block_id=?", (c.source_block_id,)).fetchone():
                return True
            if c.source_edge_id and dbh.conn.execute(
                    "SELECT 1 FROM edges WHERE edge_id=?", (c.source_edge_id,)).fetchone():
                return True
        return False
    finally:
        dbh.close()


def test_wiki_answer_door_answers_or_abstains(tmp_path):
    from alpaca.wiki.engine.answer import answer
    cfg = _seed_vault(tmp_path / "vault")
    ans = answer(cfg, "Where does Ada work?")

    # the frozen contract: currency_stamp and completeness are non-null by construction.
    assert ans.currency_stamp is not None
    assert ans.completeness is not None

    if ans.abstained:
        # an Abstain must carry its reason code.
        assert ans.verdict == "abstained"
        assert ans.extra.get("reason"), "an abstain names its reason code"
    else:
        # an Answer: grounded/conflict, with at least one resolvable pointer.
        assert ans.verdict in ("grounded", "conflict")
        assert _pointer_resolves(cfg, ans), "the answer cites at least one resolvable pointer"


def test_wiki_abstains_on_an_unknown_entity_with_a_reason_code(tmp_path):
    # the negative path (P4: a selftest that cannot fail is not a selftest): an unknown entity
    # abstains, and the abstain names its reason code.
    from alpaca.wiki.engine.answer import answer
    cfg = _seed_vault(tmp_path / "vault2")
    ans = answer(cfg, "Where does Zeus work?")
    assert ans.abstained
    assert ans.verdict == "abstained"
    assert ans.extra.get("reason"), "the abstain carries a reason code"
    assert ans.currency_stamp is not None and ans.completeness is not None
