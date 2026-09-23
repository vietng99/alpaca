"""M3.5 proof - review cards, the review budget, and curation by exception.

Proof for task M3.5. A review card exists because something needs a human decision (curate by
exception, not a routine notification). The module proves, on both the positive and the negative
path, under a fixed clock:

  * only a human identity can move a review card: an agent move is refused (FAIL), an owner move
    lands (Step 1);
  * a clean op produces zero cards: a run that needed no decision has an empty queue (Done-when);
  * the conservation law: an over-budget queue DEFERS rather than DROPS, with
    `len(surfaced) + len(deferred) == len(all)` asserted at every budget (Step 2, Done-when);
  * the budget is on the QUEUE only, never on card creation: capping attention loses no card
    (Step 2);
  * the four card classes (ship, irreversible external action, human-owned write below L5,
    owner-only pause) each produce exactly one card, and a repeat request dedupes to that one card
    (Step 1, curate by exception, Step 3);
  * `alpaca deploy` still exits 3 (it still pauses for a human) now through a real card rather than an
    unconditional refusal (Step 4).

Timing is driven by a FixedClock so every recorded `created`/`moved` stamp is deterministic.

The control table (`test_control_table`) is the ported --selftest: a table of (case -> verdict)
rows the wrapper drives, failing on any non-PASS.
"""
import json

import pytest

from alpaca import cli, clock, db, review, util


@pytest.fixture
def fixed():
    """A FixedClock installed process-wide, so card stamps are deterministic."""
    util.set_clock(clock.FixedClock("2026-04-01T00:00:00+00:00", step=1))
    try:
        yield
    finally:
        util.set_clock(None)


@pytest.fixture
def proj(project, fixed):
    """An initialized project with the record ready; cwd already inside it (conftest)."""
    cli.main(["init"])
    conn = db.connect(project)
    yield project, conn
    conn.close()


# ------------------------------------------------------- a clean op produces zero cards
def test_a_clean_op_produces_zero_cards(proj):
    root, conn = proj
    assert review.open_cards(conn) == []
    q = review.queue(conn, budget=10)
    assert q["all"] == [] and q["surfaced"] == [] and q["deferred"] == []


# ------------------------------------------------------- the four card classes
def test_the_four_card_classes_each_produce_exactly_one_card(proj):
    root, conn = proj
    review.card(conn, review.SHIP, "op-001", "ship op-001 outside the box")
    review.card(conn, review.IRREVERSIBLE, "push origin main", "push to a shared remote")
    review.card(conn, review.HUMAN_OWNED_WRITE, "contracts/phase-build.md", "an L3 write there")
    review.card(conn, review.OWNER_PAUSE, "question:q-001", "owner-only question open")
    cards = review.open_cards(conn)
    assert len(cards) == 4, cards
    kinds = sorted(c["kind"] for c in cards)
    assert kinds == sorted([review.SHIP, review.IRREVERSIBLE,
                            review.HUMAN_OWNED_WRITE, review.OWNER_PAUSE])
    # each class produced exactly one card
    for k in (review.SHIP, review.IRREVERSIBLE, review.HUMAN_OWNED_WRITE, review.OWNER_PAUSE):
        assert len([c for c in cards if c["kind"] == k]) == 1, k


def test_a_repeat_request_dedupes_to_one_card(proj):
    root, conn = proj
    a = review.card(conn, review.SHIP, "op-001", "ship op-001")
    b = review.card(conn, review.SHIP, "op-001", "ship op-001 again")
    assert a["id"] == b["id"]
    assert len(review.open_cards(conn)) == 1


def test_an_unknown_kind_is_refused(proj):
    root, conn = proj
    with pytest.raises(review.ReviewRefusal) as ei:
        review.card(conn, "not-a-card-class", "x", "y")
    assert ei.value.reason == review.R_KIND_UNKNOWN


def test_an_empty_subject_is_refused(proj):
    root, conn = proj
    with pytest.raises(review.ReviewRefusal) as ei:
        review.card(conn, review.SHIP, "  ", "y")
    assert ei.value.reason == review.R_SUBJECT_EMPTY


# ------------------------------------------------------- only a human can move a card
def test_an_agent_move_is_refused(proj):
    root, conn = proj
    c = review.card(conn, review.SHIP, "op-001", "ship it")
    for agent in ("agent", "harness", "instrument", "cli"):
        with pytest.raises(review.ReviewRefusal) as ei:
            review.move(conn, c["id"], "approve", agent)
        assert ei.value.reason == review.R_AGENT_MOVE
    # and the card is untouched: still open after the refused moves
    assert review.open_cards(conn)[0]["status"] == review.OPEN


def test_a_human_move_lands(proj):
    root, conn = proj
    c = review.card(conn, review.SHIP, "op-001", "ship it")
    for human in review.HUMAN_IDENTITIES:
        pass
    moved = review.move(conn, c["id"], "approve", "owner")
    assert moved["status"] == review.MOVED
    assert moved["decision"] == "approve"
    assert moved["moved_by"] == "owner"
    # a moved card leaves the open queue (curate by exception: it no longer needs a decision)
    assert review.open_cards(conn) == []


def test_move_of_an_unknown_card_is_refused(proj):
    root, conn = proj
    with pytest.raises(review.ReviewRefusal) as ei:
        review.move(conn, "rc-999", "approve", "owner")
    assert ei.value.reason == review.R_NOT_FOUND


def test_a_moved_card_is_not_moved_again(proj):
    root, conn = proj
    c = review.card(conn, review.SHIP, "op-001", "ship it")
    review.move(conn, c["id"], "approve", "owner")
    with pytest.raises(review.ReviewRefusal) as ei:
        review.move(conn, c["id"], "reject", "owner")
    assert ei.value.reason == review.R_ALREADY_MOVED


def test_an_empty_decision_is_refused(proj):
    root, conn = proj
    c = review.card(conn, review.SHIP, "op-001", "ship it")
    with pytest.raises(review.ReviewRefusal) as ei:
        review.move(conn, c["id"], "   ", "owner")
    assert ei.value.reason == review.R_DECISION_EMPTY


# ------------------------------------------------------- the conservation law / budget
def _make_n(conn, n):
    for i in range(n):
        review.card(conn, review.SHIP, "subject-%03d" % i, "reason %d" % i)


def test_conservation_law_over_budget_defers_never_drops(proj):
    root, conn = proj
    _make_n(conn, 5)
    for budget in (0, 1, 2, 3, 4, 5, 6, 100, None):
        q = review.queue(conn, budget=budget)
        # nothing is ever lost: surfaced + deferred == all
        assert len(q["surfaced"]) + len(q["deferred"]) == len(q["all"]) == 5, budget
        if budget is not None and budget >= 0:
            assert len(q["surfaced"]) == min(budget, 5), budget
            assert len(q["deferred"]) == 5 - min(budget, 5), budget
        else:
            # an unbounded budget surfaces all, defers none
            assert len(q["deferred"]) == 0, budget


def test_budget_is_on_the_queue_only_never_on_card_creation(proj):
    root, conn = proj
    _make_n(conn, 5)
    # a tight budget on the queue does not remove any card: five still stand
    q = review.queue(conn, budget=2)
    assert len(q["surfaced"]) == 2
    assert len(q["deferred"]) == 3
    assert len(review.open_cards(conn)) == 5, "capping attention creates/destroys no card"
    # the surfaced and deferred sets are disjoint and together cover all
    sids = {c["id"] for c in q["surfaced"]}
    dids = {c["id"] for c in q["deferred"]}
    assert sids.isdisjoint(dids)
    assert sids | dids == {c["id"] for c in q["all"]}


def test_moved_cards_leave_the_queue(proj):
    root, conn = proj
    _make_n(conn, 3)
    cards = review.open_cards(conn)
    review.move(conn, cards[0]["id"], "approve", "owner")
    q = review.queue(conn, budget=10)
    assert len(q["all"]) == 2, "a decided card is no longer in the open queue"


# ------------------------------------------------------- owner-pause bridge from questions
def test_owner_pause_cards_from_open_owner_questions(proj):
    root, conn = proj
    from alpaca import questions
    questions.bank(conn, "should we widen the scope?", "owner-only", phase="build")
    made = review.sync_owner_pauses(conn)
    assert len(made) == 1
    cards = review.open_cards(conn)
    assert len(cards) == 1
    assert cards[0]["kind"] == review.OWNER_PAUSE
    # idempotent: a re-sync makes no duplicate card
    review.sync_owner_pauses(conn)
    assert len(review.open_cards(conn)) == 1


# ------------------------------------------------------- deploy wiring (Step 4)
def test_alpaca_deploy_still_exits_3_now_through_a_card(proj):
    root, conn = proj
    code = cli.main(["deploy"])
    assert code == 3, "deploy still pauses for a human (exit 3), now via a card"
    # and a real review card now stands for the deploy pause
    conn2 = db.connect(root)
    cards = review.open_cards(conn2)
    assert len(cards) >= 1
    assert any(c["kind"] in (review.SHIP, review.IRREVERSIBLE) for c in cards)
    conn2.close()


def test_alpaca_review_move_from_the_cli(proj):
    root, conn = proj
    c = review.card(conn, review.SHIP, "op-001", "ship it")
    code = cli.main(["review", "move", c["id"], "approve", "--by", "owner"])
    assert code == 0
    conn2 = db.connect(root)
    got = [x for x in review.all_cards(conn2) if x["id"] == c["id"]][0]
    assert got["status"] == review.MOVED
    conn2.close()


def test_alpaca_review_move_by_an_agent_is_refused_at_the_cli(proj):
    root, conn = proj
    c = review.card(conn, review.SHIP, "op-001", "ship it")
    code = cli.main(["review", "move", c["id"], "approve", "--by", "agent"])
    assert code == 1, "an agent move is FAIL at the CLI too"


def test_alpaca_review_list_runs(proj):
    root, conn = proj
    review.card(conn, review.SHIP, "op-001", "ship it")
    code = cli.main(["review", "list", "--json"])
    assert code == 0


# ------------------------------------------------------- the control table (ported --selftest)
def test_control_table(proj):
    """The ported --selftest control table: every row must PASS. A single non-PASS fails the
    wrapper, exactly as the upstream selftest exit would."""
    root, conn = proj

    def _agent_move_refused():
        c = review.card(conn, review.SHIP, "ct-a", "x")
        try:
            review.move(conn, c["id"], "approve", "agent")
            return "did not refuse an agent move"
        except review.ReviewRefusal as e:
            return None if e.reason == review.R_AGENT_MOVE else "wrong reason %s" % e.reason

    def _human_move_lands():
        c = review.card(conn, review.IRREVERSIBLE, "ct-b", "x")
        m = review.move(conn, c["id"], "approve", "owner")
        return None if m["status"] == review.MOVED else "human move did not land"

    def _conservation():
        review.card(conn, review.SHIP, "ct-c1", "x")
        review.card(conn, review.SHIP, "ct-c2", "x")
        q = review.queue(conn, budget=1)
        return (None if len(q["surfaced"]) + len(q["deferred"]) == len(q["all"])
                else "conservation law violated")

    def _dedupe():
        a = review.card(conn, review.OWNER_PAUSE, "ct-d", "x")
        b = review.card(conn, review.OWNER_PAUSE, "ct-d", "x")
        return None if a["id"] == b["id"] else "a repeat request did not dedupe"

    table = {
        "agent-move-refused": _agent_move_refused,
        "human-move-lands": _human_move_lands,
        "conservation-law": _conservation,
        "dedupe-by-exception": _dedupe,
    }
    failures = {name: fn() for name, fn in table.items()}
    bad = {k: v for k, v in failures.items() if v is not None}
    assert not bad, "control table non-PASS rows: %s" % json.dumps(bad)
