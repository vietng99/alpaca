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


# ------------------------------------------------------ op-009 t-130: one resume point per live op
# Two ops can park work at once. The Resume points section gives each open op its own entry: the
# task it resumes at, the newest handoff whose ref is that op, and the newest checkpoint of a session
# whose claim at the time was on that op, each with its event id, plus the recall line for the
# session that wrote the checkpoint. Every test builds a throwaway record through the real verbs
# (task claim, msg post, session checkpoint), so a change in how those verbs store their events
# shows up here.
MEMORY, ENGINE = "mem00001-aaaa", "eng00002-bbbb"


def _onboarded():
    assert cli.main(["init"]) == 0
    assert cli.main(["onboard", "--name", "demo", "--who", "v:owner", "--what", "w"]) == 0


def _session(sid, *verb):
    assert cli.main(["--session", sid] + list(verb)) == 0


def _handoff(sid, body, ref=None, kind="handoff", sender="w"):
    verb = ["msg", "post", body, "--from", sender, "--to", "next-session", "--kind", kind]
    _session(sid, *(verb + (["--ref", ref] if ref else [])))


def _park_two_ops():
    """op-001 (memory store) parked by MEMORY on t-001, op-002 (research engine) parked by ENGINE on
    t-002. ENGINE parks second, so its handoff and checkpoint are the newest on the record."""
    _onboarded()
    assert cli.main(["op", "new", "memory store"]) == 0                       # op-001
    assert cli.main(["op", "new", "research engine"]) == 0                   # op-002
    cli.main(["task", "add", "--title", "memory scope", "op-001", "define the memory scope"])
    cli.main(["task", "add", "--title", "engine design", "op-002", "design the engine"])
    cli.main(["task", "add", "--title", "memory later", "op-001", "a later memory task"])
    for sid, task, op, word in ((MEMORY, "t-001", "op-001", "memory"),
                                (ENGINE, "t-002", "op-002", "engine")):
        _session(sid, "session", "start")
        _session(sid, "task", "claim", task, "--by", "w-" + word, "--minutes", "600")
        _handoff(sid, "%s handoff: resume %s" % (word, task), ref=op, sender="w-" + word)
        _session(sid, "session", "checkpoint", "--note", "%s checkpoint: %s half done" % (word, task))


def _newest(conn, kind, **where):
    """(id, ts) of the newest event of `kind` matching `where` column values."""
    q = "SELECT id, ts FROM events WHERE kind=?" + "".join(" AND %s=?" % k for k in where)
    r = conn.execute(q + " ORDER BY id DESC LIMIT 1", [kind] + list(where.values())).fetchone()
    assert r is not None, (kind, where)
    return r[0], r[1]


def _resume_block(text):
    """The Resume points section of a rendered pad, up to the next section."""
    assert "\n## Resume points\n" in text
    return "## Resume points\n" + text.split("\n## Resume points\n", 1)[1].split("\n## ", 1)[0]


def _entries(text):
    """op id -> that entry's lines: its '- op' line, then its '  - field:' lines."""
    out, cur = {}, None
    for line in _resume_block(text).splitlines():
        if line.startswith("- "):
            cur = line[2:].split()[0]
            out[cur] = [line]
        elif line.startswith("  - ") and cur:
            out[cur].append(line)
    return out


def _field(entry, name):
    """The one `name:` line of an entry, without its prefix; None when the entry has none."""
    prefix = "  - %s: " % name
    found = [line[len(prefix):] for line in entry if line.startswith(prefix)]
    assert len(found) <= 1, (name, entry)
    return found[0] if found else None


def test_resume_points_give_each_live_op_its_own_task_handoff_and_checkpoint(project):
    """Done bar 1: two live ops, each with a handoff and a checkpoint, render as two entries, each
    naming its own op, next task, handoff and checkpoint with event ids."""
    _park_two_ops()
    conn = db.connect(project)
    text = pad.render(project)
    entries = _entries(text)
    assert list(entries) == ["op-002", "op-001"], "the ops Progress lists as open, in its order"
    for op, sid, task, title, word, other in (
            ("op-001", MEMORY, "t-001", "memory scope", "memory", "engine"),
            ("op-002", ENGINE, "t-002", "engine design", "engine", "memory")):
        entry = entries[op]
        assert entry[0] == "- %s  %s" % (op, "memory store" if op == "op-001" else "research engine")
        assert _field(entry, "next") == "%s [doing] %s (claimed by w-%s)" % (task, title, word)
        hid, hts = _newest(conn, "msg", ref=op)
        assert _field(entry, "handoff") == "event %d at %s from w-%s: %s handoff: resume %s" % (
            hid, hts, word, word, task)
        cid, cts = _newest(conn, "session-checkpoint", session=sid)
        assert _field(entry, "checkpoint") == "event %d at %s session %s: %s checkpoint: %s half done" % (
            cid, cts, sid[:8], word, task)
        assert _field(entry, "resume") == "bin/alpaca recall %s" % sid
        # the fail case: nothing of the other op's handoff, checkpoint or session leaks in.
        joined = "\n".join(entry)
        assert other not in joined and (ENGINE if sid == MEMORY else MEMORY)[:8] not in joined
    # the pad stays a pure function of the record with the section in it.
    assert pad.render(project).split("\n", 3)[3] == text.split("\n", 3)[3]


def test_resume_points_file_a_checkpoint_under_the_op_its_session_held_when_it_wrote_it(project):
    """Fail case: a checkpoint attributed to the wrong op. A session that works on both ops files
    each note under the op of its latest claim before the note, never under both, and never under
    an op it only claimed afterwards."""
    _park_two_ops()
    both = "both0003-cccc"
    cli.main(["task", "add", "--title", "engine tests", "op-002", "test the engine"])      # t-004
    _session(both, "session", "start")
    _session(both, "task", "claim", "t-004", "--by", "w-both", "--minutes", "600")
    _session(both, "session", "checkpoint", "--note", "both on the engine")
    _session(both, "task", "claim", "t-003", "--by", "w-both", "--minutes", "600")
    conn = db.connect(project)
    engine_note, _ = _newest(conn, "session-checkpoint", session=both)
    entries = _entries(pad.render(project))
    assert _field(entries["op-002"], "checkpoint").startswith("event %d " % engine_note)
    assert _field(entries["op-002"], "resume") == "bin/alpaca recall %s" % both
    # both0003 claimed an op-001 task too, and its note is the newest on the record, but it wrote
    # the note while it held op-002 work: op-001 keeps MEMORY's note.
    memory_ckpt, _ = _newest(conn, "session-checkpoint", session=MEMORY)
    assert _field(entries["op-001"], "checkpoint").startswith("event %d " % memory_ckpt)
    assert "both on the engine" not in "\n".join(entries["op-001"])
    assert _field(entries["op-001"], "resume") == "bin/alpaca recall %s" % MEMORY
    # its next note, written after the op-001 claim, is op-001's; op-002 keeps the earlier one.
    _session(both, "session", "checkpoint", "--note", "both on memory now")
    memory_note, _ = _newest(conn, "session-checkpoint", session=both)
    entries = _entries(pad.render(project))
    assert _field(entries["op-001"], "checkpoint").startswith("event %d " % memory_note)
    assert _field(entries["op-001"], "checkpoint").endswith("both on memory now")
    assert _field(entries["op-002"], "checkpoint").startswith("event %d " % engine_note)


def test_resume_points_file_a_handoff_by_its_ref_and_skip_other_messages(project):
    """Fail case: a handoff or checkpoint attributed to the wrong op. The ref decides, not the
    session that posted it; a message of another kind, a handoff with no ref and a handoff whose ref
    is a task rather than an op belong to no op. A checkpoint from before its session's first claim,
    or from the synthetic `cli` writer, belongs to no op either."""
    _park_two_ops()
    conn = db.connect(project)
    memory_handoff, _ = _newest(conn, "msg", ref="op-001")
    _handoff(MEMORY, "memory note about op-001", ref="op-001", kind="note", sender="w-memory")
    _handoff(ENGINE, "loose handoff with no op", sender="w-engine")
    _handoff(ENGINE, "handoff pointing at a task", ref="t-001", sender="w-engine")
    # the session that parked op-001 posts a handoff for op-002: it is op-002's.
    _handoff(MEMORY, "memory hands op-002 over", ref="op-002", sender="w-memory")
    cross, _ = _newest(conn, "msg", ref="op-002")
    # a fresh session that never claimed anything checkpoints: that note belongs to no op.
    _session("idle0004-dddd", "session", "start")
    _session("idle0004-dddd", "session", "checkpoint", "--note", "idle session note")
    # the synthetic `cli` writer is shared by every bare verb call, so its claim says nothing about
    # which sitting wrote a later note under the same id: its checkpoint belongs to no op.
    assert cli.main(["task", "claim", "t-003", "--by", "w-cli", "--minutes", "600"]) == 0
    db.append_event(conn, session="cli", actor="alpaca", kind="session-checkpoint",
                    data={"trigger": "explicit", "note": "service writer note"})
    memory_ckpt, _ = _newest(conn, "session-checkpoint", session=MEMORY)
    text = pad.render(project)
    entries = _entries(text)
    assert _field(entries["op-001"], "handoff").startswith("event %d " % memory_handoff)
    assert _field(entries["op-002"], "handoff") == "event %d at %s from w-memory: memory hands op-002 over" % (
        cross, _newest(conn, "msg", ref="op-002")[1])
    assert _field(entries["op-001"], "checkpoint").startswith("event %d " % memory_ckpt)
    block = _resume_block(text)
    for stray in ("memory note about op-001", "loose handoff", "pointing at a task",
                  "idle session note", "idle0004", "service writer note"):
        assert stray not in block, stray


def test_resume_points_say_so_for_an_op_with_nothing_parked(project):
    """Done bar 2: an op with no handoff and no checkpoint still gets an entry that says so, with
    no recall line; an op with no open task says that too. A closed op gets no entry."""
    _onboarded()
    assert "(no open op)" in _resume_block(pad.render(project))
    assert cli.main(["op", "new", "quiet op"]) == 0                                   # op-001
    entry = _entries(pad.render(project))["op-001"]
    assert entry == ["- op-001  quiet op", "  - next: (no open task)",
                     "  - handoff: (no handoff)", "  - checkpoint: (no checkpoint)"]
    cli.main(["task", "add", "--title", "first step", "op-001", "take the first step"])
    entry = _entries(pad.render(project))["op-001"]
    assert _field(entry, "next") == "t-001 [open] first step (unclaimed)"
    assert _field(entry, "handoff") == "(no handoff)"
    assert _field(entry, "checkpoint") == "(no checkpoint)" and _field(entry, "resume") is None
    # an op closed just now is in Progress but parks nothing to resume.
    conn = db.connect(project)
    db.upsert(conn, "ops", "id", {"id": "op-002", "intent": "closed op", "done_when": None,
                                  "status": "closed", "opened": util.now_iso(),
                                  "closed": util.now_iso(), "phases": None})
    text = pad.render(project)
    assert "op-002" in text.split("## Progress")[1].split("## Current op")[0]
    assert list(_entries(text)) == ["op-001"]


def test_resume_points_sit_right_after_next_action_and_leave_it_unchanged(project):
    _park_two_ops()
    text = pad.render(project)
    line = pad.next_action(project)
    assert line.startswith("t-001 [doing] define the memory scope")
    assert ("\n## Next action\n\n%s\n\n## Resume points\n" % line) in text


def test_resume_points_keep_each_field_on_one_line(project):
    """M4.9: a handoff body and a checkpoint note are externally sourced. A pipe or a line break in
    either stays inside its one line, escaped, and a long one is cut."""
    _onboarded()
    assert cli.main(["op", "new", "one op"]) == 0
    cli.main(["task", "add", "--title", "a task", "op-001", "a task"])
    _session(MEMORY, "session", "start")
    _session(MEMORY, "task", "claim", "t-001", "--by", "w", "--minutes", "600")
    _handoff(MEMORY, "L|R\nsecond line " + "x" * 400, ref="op-001")
    _session(MEMORY, "session", "checkpoint", "--note", "note|with\na break")
    entry = _entries(pad.render(project))["op-001"]
    assert len(entry) == 5, entry
    handoff = _field(entry, "handoff")
    assert "|" not in handoff and handoff.endswith("...") and "L\\pR second line" in handoff
    assert len(handoff.split(": ", 1)[1]) <= pad.RESUME_TEXT_MAX + 1
    assert _field(entry, "checkpoint").endswith("note\\pwith a break")


def test_a_failing_resume_points_section_never_breaks_the_resume_write(project, monkeypatch):
    """Fail case: a render failure in the new section must not break the RESUME write. The pad is
    written without the section, and every other section is still there."""
    _park_two_ops()

    def boom(*a, **k):
        raise RuntimeError("resume points exploded")

    monkeypatch.setattr(pad, "resume_points", boom)
    path = pad.write(project)
    text = open(path, encoding="utf-8").read()
    assert "## Resume points" not in text
    for section in ("## Next action", "## Progress", "## Current op", "## Open tasks",
                    "## Recent events"):
        assert section in text, section
    assert pad.next_action(project) in text
