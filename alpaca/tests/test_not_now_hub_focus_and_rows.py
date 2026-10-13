"""(class A1, C4): hub.py:1252 counts a deferred task as open and sorts by updated_at,
and a deferral patches updated, so the task just parked becomes the overview's focus. The hub
task rows must carry the deferral (reason, trigger) for the Not now view t-618 draws, in the full
overview and in the slim /hub/overview.json row (TASK_TEXT fields are dropped there)."""
from pathlib import Path

from alpaca.tests.not_now_kit import R1, W1, defer, setup, strings
from alpaca import clock, hub, util


def test_focus_never_picks_a_deferred_task():
    tasks = [{"id": "t-001", "status": "open", "claimant": None, "updated_at": "2026-01-01T00:00:00+00:00"},
             {"id": "t-002", "status": "deferred", "claimant": None, "updated_at": "2026-01-02T00:00:00+00:00"}]
    got = hub._inline_text_ids(tasks)
    assert "t-002" not in got and "t-001" in got, got


def test_focus_after_a_real_deferral(project, monkeypatch):
    # a clock that moves on every read, so the deferral really is the newest update
    monkeypatch.setattr(util, "_CLOCK", clock.FixedClock("2026-10-13T10:00:00+07:00", step=60))
    setup(project, 2)
    defer("t-002")
    view = hub.overview(Path(project))
    assert "t-002" not in hub._inline_text_ids(view["tasks"])


def _row(rows, tid):
    return next(t for t in rows if t["id"] == tid)


def test_overview_row_carries_reason_and_trigger(project):
    setup(project, 2)
    defer("t-002")
    full = hub.overview(Path(project))
    text = list(strings({k: v for k, v in _row(full["tasks"], "t-002").items() if k != "history"}))
    assert any(R1 in s for s in text) and any(W1 in s for s in text), \
        "the hub task row carries the deferral only inside history (or not at all)"


def test_slim_overview_row_carries_reason_and_trigger(project):
    setup(project, 2)
    defer("t-002")
    slim = hub.page_overview(hub.overview(Path(project)))
    text = list(strings({k: v for k, v in _row(slim["tasks"], "t-002").items() if k != "text"}))
    assert any(R1 in s for s in text) and any(W1 in s for s in text), \
        "the slim overview drops the deferral; a Not now view would need one request per task"
