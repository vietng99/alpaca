"""M2.2: the injectable clock. FixedClock is the test seam; util.set_clock is the process-wide
install; db.append_event(clock=...) is the explicit per-write seam. Real time is a stamp, never a
branch."""
import re
from alpaca import clock, db, util

ISO_OFFSET = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}([+-]\d{2}:\d{2}|Z)$")


def test_system_clock_stamps_offset_seconds():
    assert ISO_OFFSET.match(clock.SystemClock()())
    assert ISO_OFFSET.match(clock.system_now_iso())
    assert util.get_clock() is None  # the default: no clock installed


def test_fixed_clock_is_constant_without_step():
    fc = clock.FixedClock(start="2026-01-01T00:00:00+00:00")
    assert fc() == "2026-01-01T00:00:00+00:00"
    assert fc() == "2026-01-01T00:00:00+00:00"
    assert ISO_OFFSET.match(fc())


def test_fixed_clock_advances_by_step():
    fc = clock.FixedClock(start="2026-01-01T00:00:00+00:00", step=5)
    assert fc() == "2026-01-01T00:00:00+00:00"
    assert fc() == "2026-01-01T00:00:05+00:00"
    assert fc() == "2026-01-01T00:00:10+00:00"


def test_fixed_clock_drops_microseconds():
    fc = clock.FixedClock(start="2026-03-04T05:06:07.123456+07:00")
    assert fc() == "2026-03-04T05:06:07+07:00"


def test_set_clock_routes_now_iso_and_restores():
    try:
        util.set_clock(clock.FixedClock(start="2026-02-02T02:02:02+00:00"))
        assert util.now_iso() == "2026-02-02T02:02:02+00:00"
    finally:
        util.set_clock(None)
    assert util.now_iso() != "2026-02-02T02:02:02+00:00"
    assert util.get_clock() is None


def test_append_event_clock_param_stamps_ts(project):
    conn = db.connect(project)
    fc = clock.FixedClock(start="2026-05-05T05:05:05+00:00")
    e = db.append_event(conn, session="s", actor="a", kind="k", clock=fc)
    assert e["ts"] == "2026-05-05T05:05:05+00:00"


def test_installed_clock_reaches_the_writer(project):
    conn = db.connect(project)
    try:
        util.set_clock(clock.FixedClock(start="2026-06-06T06:06:06+00:00"))
        e = db.append_event(conn, session="s", actor="a", kind="k")
    finally:
        util.set_clock(None)
    assert e["ts"] == "2026-06-06T06:06:06+00:00"
