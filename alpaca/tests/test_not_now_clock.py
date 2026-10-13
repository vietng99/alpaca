"""(class B): the date a date: trigger is compared with. B2: a +07:00 stamp just after
local midnight is already that local day (converting to UTC first moves it back a day). B3: the
day itself counts (on or after). B1: the projection freshness gate (freshness.py, composed into
every phase door) diffs RESUME.md, CHECKLIST.md and data.json against a fresh render with only
stamp lines volatile; if 'today' is read from the wall clock, a 'trigger met' line appears at
midnight with no record change and every door fails PROJECTION-STALE. Read today from the
record (sessions_view.record_now, as pad.py:206 and export.py:85 already do), so it moves only
when the record moves."""
from alpaca.tests.not_now_kit import not_now, blocks, defer, freeze, marked, ok, resume, run, section, setup
from alpaca import clock, db, export, freshness, pad, util


def test_local_midnight_counts_as_that_day(project, monkeypatch):
    freeze(monkeypatch, "2026-10-13T00:30:00+07:00")
    setup(project, 3)
    defer("t-001", "parked", "date:2026-10-13")
    defer("t-002", "parked", "date:2026-10-14")
    defer("t-003", "parked", "date:2026-10-12")
    body = "\n".join(not_now(resume(project)) or [])
    assert any(marked(b) for b in blocks(body, "t-001")), body
    assert not any(marked(b) for b in blocks(body, "t-002")), body
    assert any(marked(b) for b in blocks(body, "t-003")), body


def test_late_evening_is_not_yet_the_next_day(project, monkeypatch):
    freeze(monkeypatch, "2026-10-13T23:30:00-05:00")
    setup(project, 1)
    defer("t-001", "parked", "date:2026-10-14")
    assert not marked(resume(project))


def test_projections_stay_fresh_across_midnight_with_no_record_change(project, monkeypatch):
    freeze(monkeypatch, "2026-10-12T23:59:00+07:00")
    setup(project, 2)
    defer("t-002", "parked", "date:2026-10-13")
    pad.write(project)
    export.write(project)
    assert freshness.check(project) == [], "setup: projections fresh right after writing"
    monkeypatch.setattr(util, "_CLOCK", clock.FixedClock("2026-10-13T00:01:00+07:00"))
    assert freshness.check(project) == [], \
        "a clock-only date flip made a projection stale: 'today' must come from the record"


def test_the_record_moving_past_the_date_marks_the_trigger(project, monkeypatch):
    freeze(monkeypatch, "2026-10-12T23:59:00+07:00")
    setup(project, 2)
    defer("t-002", "parked", "date:2026-10-13")
    assert not marked(resume(project))
    monkeypatch.setattr(util, "_CLOCK", clock.FixedClock("2026-10-13T00:01:00+07:00"))
    ok("msg", "post", "--from", "w1", "--to", "all", "--kind", "note", "a later event")
    assert any(marked(b) for b in blocks("\n".join(not_now(resume(project)) or []), "t-002"))
