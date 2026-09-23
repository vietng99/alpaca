import os
from alpaca import cli, clock, db, pad, sessions_view, util
from alpaca.tests import proofkit

def test_pad_before_onboarding(project):
    cli.main(["init"])
    text = pad.render(project)
    assert "not onboarded" in text and "alpaca onboard" in text

def test_pad_shows_current_op_and_first_task(project):
    cli.main(["init"]); cli.main(["onboard", "--name", "demo", "--who", "v:owner", "--what", "w"])
    cli.main(["op", "new", "add hello", "--done-when", "hello prints"])
    cli.main(["task", "add", "--title", "task", "op-001", "write verb", "--phase", "build"])
    cli.main(["task", "add", "--title", "task", "op-001", "write test", "--phase", "verify"])
    cli.main(["task", "move", "t-001", "done", "--proof", proofkit.seal_for(project, "t-001")])
    text = pad.render(project)
    assert "op-001" in text and "add hello" in text
    assert "t-002" in text and "write test" in text
    assert pad.next_action(project).startswith("t-002")
    p = pad.write(project)
    assert os.path.isfile(p) and "demo" in open(p, encoding="utf-8").read()

def test_pad_counts_stay_consistent_when_a_lease_expires(project):
    cli.main(["init"]); cli.main(["onboard", "--name", "demo", "--who", "v:owner", "--what", "w"])
    cli.main(["op", "new", "x"]); cli.main(["task", "add", "--title", "task", "op-001", "s"])
    cli.main(["task", "claim", "t-001", "--by", "w1", "--minutes", "-5"])
    text = pad.render(project)
    header_events = int(text.split("events: ")[1].split(" |")[0])
    listed = [l for l in text.split("## Recent events")[1].splitlines() if l.startswith("- ")]
    assert "lease-expired" in text and header_events >= len(listed)

# ------------------------------------------------------------------ op-006: where the project is
def _probe(conn, sid):
    """What the desktop app leaves behind when it opens a session to run one local command."""
    db.upsert(conn, "sessions", "sid", {"sid": sid, "started": util.now_iso(),
                                        "ended": util.now_iso(), "beats": 0})
    db.append_event(conn, session=sid, actor="agent", kind="session-start")
    db.append_event(conn, session=sid, actor="agent", kind="session-end")


def _worker(conn, sid, level="L5", beats=3):
    db.upsert(conn, "sessions", "sid", {"sid": sid, "started": util.now_iso(), "level": level,
                                        "beats": beats})
    db.meta_set(conn, "operator:%s" % sid, "claude")
    for n in range(beats):
        db.append_event(conn, session=sid, actor="agent", kind="heartbeat",
                        data={"tool": "Bash", "ref": "step %d" % n})


def test_header_counts_working_sessions_and_probes_apart(project):
    cli.main(["init"]); cli.main(["onboard", "--name", "demo", "--who", "v:owner", "--what", "w"])
    conn = db.connect(project)
    _worker(conn, "aaaaaaaa-1111")
    for n in range(12):
        _probe(conn, "probe-%02d" % n)
    text = pad.render(project)
    header = [l for l in text.splitlines() if l.startswith("updated:")][0]
    assert "sessions: 1 working, 12 probes" in header
    last = [l for l in text.splitlines() if l.startswith("last session:")][0]
    assert last.startswith("last session: aaaaaaaa"), "a probe never becomes the last session"
    assert "level L5" in last, "the level printed is the working session's own"


def test_active_now_names_the_live_session_its_tool_and_what_it_holds(project):
    util.set_clock(clock.FixedClock("2026-06-01T00:00:00+00:00", step=0))
    try:
        cli.main(["init"]); cli.main(["onboard", "--name", "demo", "--who", "v:owner", "--what", "w"])
        cli.main(["op", "new", "x"]); cli.main(["task", "add", "--title", "task", "op-001", "a task to hold"])
        cli.main(["--session", "aaaaaaaa-1111", "task", "claim", "t-001", "--by", "w1",
                  "--minutes", "60"])
        conn = db.connect(project)
        _worker(conn, "aaaaaaaa-1111")
        block = pad.render(project).split("## Active now")[1].split("##")[0]
        assert "aaaaaaaa" in block and "claude" in block and "L5" in block
        # four beats: the claim's own first heartbeat, then the three this worker recorded.
        assert "beats 4" in block and "Bash" in block and "step 2" in block
        assert "t-001" in block, "the pad names the task the live session holds"
    finally:
        util.set_clock(None)


def test_active_now_reads_the_record_clock_and_says_so_when_nobody_is_live(project):
    util.set_clock(clock.FixedClock("2026-06-01T00:00:00+00:00", step=0))
    try:
        cli.main(["init"]); cli.main(["onboard", "--name", "demo", "--who", "v:owner", "--what", "w"])
        conn = db.connect(project)
        _worker(conn, "aaaaaaaa-1111")
        assert "aaaaaaaa" in pad.render(project).split("## Active now")[1].split("##")[0]
        # nothing is appended and the wall clock runs on a year: the record still reads the same.
        util.set_clock(clock.FixedClock("2027-06-01T00:00:00+00:00", step=0))
        assert "aaaaaaaa" in pad.render(project).split("## Active now")[1].split("##")[0]
        # a newer event moves the record's own clock past the window, and only then is it idle.
        db.append_event(conn, session="other", actor="agent", kind="task-add")
        block = pad.render(project).split("## Active now")[1].split("##")[0]
        assert "aaaaaaaa" not in block and "record time" in block
    finally:
        util.set_clock(None)


def test_progress_reports_each_open_op_and_the_op_closed_today(project):
    cli.main(["init"]); cli.main(["onboard", "--name", "demo", "--who", "v:owner", "--what", "w"])
    cli.main(["op", "new", "first"])
    for statement in ("one", "two"):
        cli.main(["task", "add", "--title", "task", "op-001", statement])
    cli.main(["task", "move", "t-001", "done", "--proof", proofkit.seal_for(project, "t-001")])
    block = pad.render(project).split("## Progress")[1].split("## Current op")[0]
    assert "op-001" in block and "open" in block and "tasks 1/2" in block
    assert "rows 0/0 (blocked 0)" in block
    # negative: an op closed long ago is not where the project is now.
    conn = db.connect(project)
    db.upsert(conn, "ops", "id", {"id": "op-old", "intent": "an old op", "done_when": None,
                                  "status": "closed", "opened": "2020-01-01T00:00:00+00:00",
                                  "closed": "2020-01-02T00:00:00+00:00", "phases": None})
    assert "op-old" not in pad.render(project).split("## Progress")[1].split("## Current op")[0]


def test_recent_events_counts_the_noise_instead_of_listing_it(project):
    cli.main(["init"]); cli.main(["onboard", "--name", "demo", "--who", "v:owner", "--what", "w"])
    cli.main(["op", "new", "x"]); cli.main(["task", "add", "--title", "task", "op-001", "a real task"])
    conn = db.connect(project)
    _worker(conn, "aaaaaaaa-1111", beats=20)
    for n in range(5):
        _probe(conn, "probe-%02d" % n)
    block = pad.render(project).split("## Recent events")[1]
    listed = [l for l in block.splitlines() if l.startswith("- ")]
    assert len(listed) <= 8
    assert not [l for l in listed if " heartbeat " in l or " session-start " in l]
    assert "task-add" in block, "the events a reader wants are still there"
    closing = [l for l in block.splitlines() if l.startswith("(+ ")][0]
    assert "heartbeats" in closing and "probe sessions" in closing
    assert "20 heartbeats" in closing and "5 probe sessions" in closing


def test_the_pad_is_a_pure_function_of_the_record(project):
    """The freshness gate re-renders and diffs, so two renders of one record must agree outside
    the declared volatile lines."""
    from alpaca import freshness
    cli.main(["init"]); cli.main(["onboard", "--name", "demo", "--who", "v:owner", "--what", "w"])
    cli.main(["op", "new", "x"]); cli.main(["task", "add", "--title", "task", "op-001", "a real task"])
    conn = db.connect(project)
    _worker(conn, "aaaaaaaa-1111")
    for n in range(4):
        _probe(conn, "probe-%d" % n)
    assert freshness.content_hash(pad.render(project)) == \
        freshness.content_hash(pad.render(project))
    pad.write(project)
    assert freshness.check(project) == []


def test_next_action_can_be_read_without_the_lease_sweep(project):
    """A projection renders the record; it never writes to it. The read-only caller gets the same
    line without the sweep that a pad render performs."""
    cli.main(["init"]); cli.main(["onboard", "--name", "demo", "--who", "v:owner", "--what", "w"])
    cli.main(["op", "new", "x"]); cli.main(["task", "add", "--title", "task", "op-001", "s"])
    cli.main(["task", "claim", "t-001", "--by", "w1", "--minutes", "-5"])
    conn = db.connect(project)
    before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    line = pad.next_action(project, conn=conn, expire=False)
    assert line.startswith("t-001")
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
    # negative: the sweeping caller DOES record the expiry.
    assert pad.next_action(project, conn=conn, expire=True).startswith("t-001")
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] > before


def test_pad_onboarded_without_op_and_with_all_done(project):
    cli.main(["init"]); cli.main(["onboard", "--name", "demo", "--who", "v:owner", "--what", "w"])
    assert pad.next_action(project).startswith("no open op")
    cli.main(["op", "new", "x"]); cli.main(["task", "add", "--title", "task", "op-001", "s"])
    cli.main(["task", "move", "t-001", "done", "--proof", proofkit.seal_for(project, "t-001")])
    assert "has no open tasks" in pad.next_action(project)
    assert "(none)" in pad.render(project)
