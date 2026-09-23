"""M3.4: bounded loops, the stuck report, halt this thread.

Proof for task M3.4. A row that fails its instrument the declared number of times produces a
stuck report naming the last three attempts and their verdicts, halts THAT thread only, and
leaves the rest of the op working. Attempts are counted in the record keyed on the row and the
instrument, so a crash (a fresh connection) does not reset the counter. The stuck report is
emitted as a message and a pad line with pointers, and a question is banked with its class
(M1.18). The Stop hook refuses a top-level stop only while a done marker is genuinely absent, and
NOT while a thread is halted: unattended never means unbounded.

Positive and negative paths, per Step 1:
  * retry bound reached (attempt counts, exhausted at the declared bound, an override honoured),
  * the stuck report content (the last three attempts and their verdicts, in record order),
  * one thread halted while another advances,
  * the Stop hook refusing a top-level stop only while a done marker is genuinely absent and not
    while a thread is halted.

FixedClock stamps every attempt so the report order is asserted against deterministic times.
"""
import json
import os
import subprocess
import sys

import pytest

from alpaca import clock, db, messages, project as proj, questions, util
from alpaca.posture import level, loops


@pytest.fixture(autouse=True)
def _fixed_clock():
    """Deterministic, monotonically advancing timestamps for every write in a test."""
    util.set_clock(clock.FixedClock(start="2026-09-16T00:00:00+00:00", step=1))
    try:
        yield
    finally:
        util.set_clock(None)


# --------------------------------------------------------------------------- attempt counter
def test_attempt_counts_in_the_record_and_a_crash_does_not_reset(project):
    """attempt(conn, ref) returns the running count; the count lives in the append-only record,
    so a fresh connection (a crash and resume) reads the same number, never zero."""
    conn = db.connect(project)
    assert loops.attempt(conn, "R1", instrument="build") == 1
    assert loops.attempt(conn, "R1", instrument="build") == 2
    # a crash: a brand-new connection to the same db file must not reset the counter.
    conn2 = db.connect(project)
    assert loops.count(conn2, "R1", instrument="build") == 2
    assert loops.attempt(conn2, "R1", instrument="build") == 3


def test_attempt_is_keyed_on_the_row_and_the_instrument(project):
    """Two instruments over the same row count independently; two rows under the same instrument
    count independently. The key is the pair, never the row alone."""
    conn = db.connect(project)
    loops.attempt(conn, "R1", instrument="build")
    loops.attempt(conn, "R1", instrument="build")
    loops.attempt(conn, "R1", instrument="lint")
    loops.attempt(conn, "R2", instrument="build")
    assert loops.count(conn, "R1", instrument="build") == 2
    assert loops.count(conn, "R1", instrument="lint") == 1
    assert loops.count(conn, "R2", instrument="build") == 1


# --------------------------------------------------------------------------- the retry bound
def test_exhausted_at_the_declared_bound(project):
    """The default bound is three; the pair is exhausted only once it is reached."""
    conn = db.connect(project)
    assert loops.bound(project) == 3
    for i in range(2):
        loops.attempt(conn, "R1", instrument="build")
        assert loops.exhausted(conn, "R1", instrument="build", root=project) is False
    loops.attempt(conn, "R1", instrument="build")
    assert loops.exhausted(conn, "R1", instrument="build", root=project) is True


def test_project_yaml_overrides_the_bound(project):
    """A project may declare its own loop bound; the on-disk value wins over the default."""
    proj.save(project, {"name": "p", "loop_bound": 2})
    conn = db.connect(project)
    assert loops.bound(project) == 2
    loops.attempt(conn, "R1", instrument="build")
    assert loops.exhausted(conn, "R1", instrument="build", root=project) is False
    loops.attempt(conn, "R1", instrument="build")
    assert loops.exhausted(conn, "R1", instrument="build", root=project) is True


# --------------------------------------------------------------------------- the stuck report
def test_stuck_report_names_the_last_three_attempts_and_their_verdicts(project):
    """The report names the last three attempts (newest of four) and each verdict, in record
    order, and carries the row, the instrument, the count and the bound as pointers."""
    conn = db.connect(project)
    verdicts = ["FAIL", "FAIL", "BLOCKED", "FAIL"]
    for v in verdicts:
        loops.attempt(conn, "R1", instrument="build", verdict=v)
    rep = loops.stuck_report(conn, "R1", instrument="build")
    assert rep["ref"] == "R1" and rep["instrument"] == "build"
    assert rep["count"] == 4 and rep["bound"] == 3
    last3 = rep["last_three"]
    assert len(last3) == 3
    assert [a["n"] for a in last3] == [2, 3, 4]                 # the LAST three, in record order
    assert [a["verdict"] for a in last3] == ["FAIL", "BLOCKED", "FAIL"]
    # deterministic clock: each attempt is stamped one second after the last.
    assert last3[0]["ts"] < last3[1]["ts"] < last3[2]["ts"]


def test_stuck_report_of_fewer_than_three_attempts_names_them_all(project):
    conn = db.connect(project)
    loops.attempt(conn, "R1", instrument="build", verdict="FAIL")
    rep = loops.stuck_report(conn, "R1", instrument="build")
    assert [a["n"] for a in rep["last_three"]] == [1]


# --------------------------------------------------------------------------- halt this thread
def test_halt_emits_a_message_a_pad_line_and_banks_a_question(project):
    """halt records the halt, posts a message with the stuck report, surfaces a pad line naming
    the pointers, and banks a question with its class (owner-owed, never machine-decided)."""
    from alpaca import cli
    cli.main(["init"])
    conn = db.connect(project)
    for v in ("FAIL", "FAIL", "FAIL"):
        loops.attempt(conn, "R1", instrument="build", verdict=v)
    rec = loops.halt(conn, "R1", "instrument failed the declared number of times",
                     instrument="build", session="s1", root=project)
    assert rec["ref"] == "R1"
    # the message: a blocker carrying the stuck report, readable off the board.
    msgs = messages.read(conn)
    assert any(m["kind"] == "blocker" and "R1" in (m["body"] or "") for m in msgs)
    # the pad line: the halted thread and its pointers show in the pad block.
    pad = "\n".join(loops.pad_lines(conn, root=project))
    assert "R1" in pad and "build" in pad and "halt" in pad.lower()
    # the banked question: owner-owed (routes to the owner), blocks the halted row, still open.
    owed = questions.open_owner_owed(conn)
    assert owed, "a stuck thread banks an owner-owed question"
    q = owed[-1]
    assert q["route"] == questions.ROUTE_OWNER
    assert "R1" in q["blocks"]


def test_halt_one_thread_leaves_the_rest_working(project):
    """A halt stops THAT thread only: the halted row reads halted, another row does not, and the
    op's other work is free to advance."""
    conn = db.connect(project)
    for v in ("FAIL", "FAIL", "FAIL"):
        loops.attempt(conn, "R1", instrument="build", verdict=v)
    loops.halt(conn, "R1", "stuck", instrument="build", session="s1", root=project)
    assert loops.is_halted(conn, "R1") is True
    assert loops.is_halted(conn, "R2") is False
    assert loops.halted(conn) == ["R1"]


# --------------------------------------------------------------------------- the done marker
def test_done_marker_absent_then_present(project):
    conn = db.connect(project)
    assert loops.done_marker_present(conn, "s1") is False
    loops.mark_done(conn, "s1")
    assert loops.done_marker_present(conn, "s1") is True


def test_stop_below_full_autodrive_never_refuses(project):
    """Below L6 the Stop hook never refuses a top-level stop, done marker or not."""
    conn = db.connect(project)
    level.set(conn, "s1", 2, "a modest goal")
    refuse, _ = loops.stop_should_refuse(conn, "s1", root=project)
    assert refuse is False


def test_stop_at_l6_refuses_only_while_the_done_marker_is_absent(project):
    """At L6 the stop is refused while no done marker is present; once the run marks itself done
    the refusal lifts."""
    conn = db.connect(project)
    level.set(conn, "s1", 6, "run the whole op to the ship card")
    refuse, reason = loops.stop_should_refuse(conn, "s1", root=project)
    assert refuse is True and reason
    loops.mark_done(conn, "s1")
    refuse2, _ = loops.stop_should_refuse(conn, "s1", root=project)
    assert refuse2 is False


def test_stop_at_l6_does_not_refuse_while_a_thread_is_halted(project):
    """A halted thread is a bounded terminal state, not an unfinished run: the Stop hook does not
    refuse the top-level stop while a thread is halted, even with no done marker. Unattended
    never means unbounded."""
    conn = db.connect(project)
    level.set(conn, "s1", 6, "run the whole op")
    for v in ("FAIL", "FAIL", "FAIL"):
        loops.attempt(conn, "R1", instrument="build", verdict=v)
    loops.halt(conn, "R1", "stuck", instrument="build", session="s1", root=project)
    refuse, _ = loops.stop_should_refuse(conn, "s1", root=project)
    assert refuse is False


# --------------------------------------------------------------------------- the Stop hook wire
def _hook(module, payload, project):
    env = {**os.environ, "CLAUDE_PROJECT_DIR": project,
           "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))}
    return subprocess.run([sys.executable, "-m", module], input=json.dumps(payload),
                          capture_output=True, text=True, encoding="utf-8", cwd=project, env=env)


def test_stop_hook_blocks_at_l6_without_a_done_marker(project):
    """The wired Stop hook: at L6 with no done marker it prints a block decision and still exits
    0 (fail-open); once the run is done it prints nothing and exits 0."""
    from alpaca import cli
    cli.main(["init"])
    conn = db.connect(project)
    level.set(conn, "s1", 6, "run the whole op")
    p = _hook("alpaca.hooks.stop", {"session_id": "s1", "stop_hook_active": False}, project)
    assert p.returncode == 0
    out = json.loads(p.stdout) if p.stdout.strip() else {}
    assert out.get("decision") == "block" and out.get("reason")
    # once the run marks itself done, the stop is allowed: no decision printed.
    loops.mark_done(conn, "s1")
    p2 = _hook("alpaca.hooks.stop", {"session_id": "s1", "stop_hook_active": False}, project)
    assert p2.returncode == 0 and p2.stdout.strip() == ""


def test_stop_hook_is_silent_below_full_autodrive(project):
    """The pre-existing contract: an ordinary stop records the turn end and prints nothing."""
    from alpaca import cli
    cli.main(["init"])
    p = _hook("alpaca.hooks.stop", {"session_id": "s1", "stop_hook_active": False}, project)
    assert p.returncode == 0 and p.stdout == ""
    assert db.events(db.connect(project), kind="turn-end")
