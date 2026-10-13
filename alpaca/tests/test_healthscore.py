"""The hub's health score out of 1000 (t-590): each part, its boundaries, and the fail case that a
stale page or a dead collector can never read as good."""
import datetime as dt

import pytest

from alpaca import healthscore as hs

NOW = 1_800_000_000.0
MB = 1000 * 1000


def iso(at):
    return dt.datetime.fromtimestamp(at, dt.timezone(dt.timedelta(hours=7))).isoformat()


def full(**changes):
    inputs = {"page": {"state": "fresh"}, "speed": [], "collector": {"status": "fresh", "liveness": "alive"},
              "backlog": 0, "issues": [], "issues_open": 0, "failures": {}, "missing_children": 0}
    return {**inputs, **changes}


def part(result, key):
    return next(p for p in result["parts"] if p["key"] == key)


def test_full_marks_sum_to_1000_with_no_reasons():
    result = hs.score(full(), NOW)
    assert (result["value"], result["max"], result["band"]) == (1000, 1000, "good")
    assert [p["max"] for p in result["parts"]] == [250, 250, 200, 150, 150]
    assert all(p["points"] == p["max"] and p["reasons"] == [] for p in result["parts"])
    assert result["notes"] == []


@pytest.mark.parametrize("state,points", [("fresh", 250), ("starting", 100), ("stale", 50), ("error", 0), (None, 0)])
def test_page_data(state, points):
    result = hs.score(full(page={"state": state, "detail": "watcher idle"}), NOW)
    assert part(result, "page")["points"] == points
    assert result["value"] == 750 + points
    if points < 250:
        assert part(result, "page")["reasons"] == ["page data %s: watcher idle" % (state or "unknown")]


@pytest.mark.parametrize("seconds,points", [(0.1, 250), (0.5, 250), (2.75, 125), (4.9, 6), (5.0, 0), (30, 0)])
def test_hub_speed_is_linear_between_half_a_second_and_five(seconds, points):
    result = hs.score(full(speed=[(NOW - 10, seconds)]), NOW)
    assert part(result, "speed")["points"] == points
    if points < 250:
        assert part(result, "speed")["reasons"] == ["p95 %.1f s over the last 5 min (1 reads)" % seconds]


def test_hub_speed_reads_only_the_last_five_minutes_at_p95():
    assert part(hs.score(full(speed=[(NOW - 301, 9.0)]), NOW), "speed")["points"] == 250
    assert part(hs.score(full(speed=[(NOW - 300, 9.0)]), NOW), "speed")["points"] == 0
    # one slow read in twenty is the 5 % the p95 leaves out; two are not
    fast = [(NOW - 5, 0.1)] * 19
    assert part(hs.score(full(speed=fast + [(NOW - 5, 9.0)]), NOW), "speed")["points"] == 250
    assert part(hs.score(full(speed=fast[1:] + [(NOW - 5, 9.0)] * 2), NOW), "speed")["points"] == 0
    assert hs.p95([]) is None and hs.p95([3.0]) == 3.0


@pytest.mark.parametrize("collector,points", [
    ({"status": "fresh", "liveness": "alive"}, 200), ({"status": "fresh", "liveness": "unknown"}, 90),
    ({"status": "stale", "liveness": "alive"}, 0), ({"status": "stale", "liveness": "unknown"}, 0),
    ({"status": "fresh", "liveness": "stopped"}, 0), ({"status": "stale", "liveness": "stopped"}, 0),
    ({"status": "fresh"}, 0), ({"status": "unknown", "liveness": "unknown"}, 0), ({"status": "unavailable"}, 0),
    ({}, 0), (None, 0)])
def test_capture_collector(collector, points):
    result = hs.score(full(collector=collector), NOW)
    assert part(result, "collector")["points"] == points
    assert bool(part(result, "collector")["reasons"]) == (points < 200)


@pytest.mark.parametrize("backlog,points", [(None, 150), (0, 150), (MB, 150), (MB + 1, 150), (50.5 * MB, 100),
                                            (100 * MB, 50), (500 * MB, 50)])
def test_capture_backlog_costs_up_to_100_from_1_to_100_mb(backlog, points):
    assert part(hs.score(full(backlog=backlog), NOW), "capture")["points"] == points


def test_capture_issues_seen_in_the_last_hour_cost_10_each_at_most_50():
    def capture(stamps, opened=None):
        return part(hs.score(full(issues=stamps, issues_open=len(stamps) if opened is None else opened), NOW), "capture")
    assert capture([iso(NOW - 3601)])["points"] == 150
    assert capture([iso(NOW - 3600)])["points"] == 140
    assert capture([iso(NOW - 60)] * 3)["reasons"] == ["3 open issues seen in the last hour"]
    assert capture([iso(NOW - 60)] * 3)["points"] == 120
    assert capture([iso(NOW - 60)] * 9)["points"] == 100
    assert capture(["not a stamp", iso(NOW - 60)])["points"] == 130   # an unreadable stamp counts as now
    # the summary keeps the newest 100 stamps: past that the count is a floor
    assert capture([iso(NOW - 60)] * 100, 7913)["reasons"] == ["100+ open issues seen in the last hour (7913 open in all)"]
    both = part(hs.score(full(backlog=200 * MB, issues=[iso(NOW)] * 9, issues_open=9), NOW), "capture")
    assert both["points"] == 0 and len(both["reasons"]) == 2


def test_server_errors_in_the_last_ten_minutes_cost_50_each():
    def errors(*ages):
        failures = {"c%d" % i: {"count": 1, "last": {"at": NOW - age, "type": "OSError"}} for i, age in enumerate(ages)}
        return part(hs.score(full(failures=failures), NOW), "errors")
    assert errors(601)["points"] == 150
    assert errors(600)["points"] == 100
    assert errors(120)["reasons"] == ["c0 failed 2 min ago (OSError)"]
    assert errors(1, 2, 3)["points"] == 0
    assert errors(1, 2, 3, 4)["points"] == 0


@pytest.mark.parametrize("value,band", [(1000, "good"), (900, "good"), (899, "attention"), (600, "attention"),
                                        (599, "bad"), (0, "bad")])
def test_bands(value, band):
    assert hs.band(value) == band


def test_missing_child_sources_are_a_note_not_a_cost():
    result = hs.score(full(missing_children=4394), NOW)
    assert result["value"] == 1000
    assert result["notes"] == ["4394 missing child sources (long-standing coverage gaps)"]


@pytest.mark.parametrize("page", ["starting", "stale", "error", None])
def test_a_page_that_is_not_fresh_never_reads_good(page):
    result = hs.score(full(page={"state": page}), NOW)
    assert result["value"] <= 850 and result["band"] != "good"


@pytest.mark.parametrize("collector", [{"status": "fresh", "liveness": "stopped"}, {"status": "stale", "liveness": "stopped"},
                                       {"status": "stale", "liveness": "alive"}, {"status": "stale", "liveness": "unknown"},
                                       {"status": "unknown"}, {"status": "unavailable"}, {}])
def test_a_dead_hung_or_missing_collector_never_reads_good(collector):
    result = hs.score(full(collector=collector), NOW)
    assert result["value"] <= 800 and result["band"] != "good"


@pytest.mark.parametrize("hours", [0.05, 2, 48])
def test_a_one_off_collect_never_reads_good(hours):
    """The review's once case (t-590 r1): `alpaca collect once` writes mode once, so liveness stays
    unknown and nothing proves a collector keeps running: 90 while its heartbeat is fresh, so the
    score stays under the good band, and 0 once it is past 120 s and the status is stale."""
    status = "fresh" if hours * 3600 < 120 else "stale"
    collector = {"status": status, "liveness": "unknown", "mode": "once", "age_seconds": hours * 3600}
    result = hs.score(full(collector=collector), NOW)
    assert part(result, "collector")["points"] == (90 if status == "fresh" else 0)
    assert result["value"] == (890 if status == "fresh" else 800) and result["band"] == "attention"
    if status == "stale":
        assert part(result, "collector")["reasons"] == ["collector stale: no heartbeat for %d min" % (hours * 60)]


def test_a_hung_watch_collector_scores_nothing():
    # the watch process is alive but its heartbeat stopped: stale, alive (review edges.py)
    result = hs.score(full(collector={"status": "stale", "liveness": "alive", "age_seconds": 600}), NOW)
    assert (result["value"], result["band"]) == (800, "attention")


def test_parts_sum_to_value_and_stay_in_range_under_any_mix():
    result = hs.score({"page": {"state": "stale"}, "speed": [(NOW, 99.0)], "collector": None, "backlog": 10 ** 12,
                       "issues": [iso(NOW)] * 50, "issues_open": 50,
                       "failures": {k: {"last": {"at": NOW}} for k in "abcdef"}}, NOW)
    assert result["value"] == sum(p["points"] for p in result["parts"]) == 50
    assert result["band"] == "bad"
    assert all(0 <= p["points"] <= p["max"] for p in result["parts"])


def test_every_loss_is_named_and_the_losses_add_up():
    result = hs.score(full(backlog=40.6 * MB, issues=[iso(NOW)] * 5, issues_open=5,
                           failures={c: {"last": {"at": NOW, "type": "OSError"}} for c in "abcd"}), NOW)
    capture = part(result, "capture")
    assert capture["losses"] == [{"points": 40, "reason": "backlog 40.6 MB"},
                                 {"points": 50, "reason": "5 open issues seen in the last hour"}]
    errors = part(result, "errors")
    assert [l["points"] for l in errors["losses"]] == [50, 50, 50] and errors["points"] == 0
    for p in result["parts"]:
        assert p["max"] - p["points"] == sum(l["points"] for l in p["losses"])
        assert p["reasons"] == [l["reason"] for l in p["losses"]]


def test_a_client_that_left_is_not_a_server_error():
    failures = {"disconnect": {"count": 3, "last": {"at": NOW, "type": "BrokenPipeError"}},
                "endpoint": {"count": 1, "last": {"at": NOW, "type": "ConnectionResetError"}},
                "overview": {"count": 1, "last": {"at": NOW - 30, "type": "OperationalError"}}}
    errors = part(hs.score(full(failures=failures), NOW), "errors")
    assert errors["points"] == 100 and errors["reasons"] == ["overview failed 0 min ago (OperationalError)"]


def test_inputs_that_are_not_numbers_count_as_the_worst_case():
    nan, inf = float("nan"), float("inf")
    assert part(hs.score(full(backlog=inf), NOW), "capture")["losses"] == [{"points": 100, "reason": "backlog unreadable"}]
    assert part(hs.score(full(backlog=nan), NOW), "capture")["points"] == 50
    assert part(hs.score(full(backlog="big"), NOW), "capture")["points"] == 50
    assert part(hs.score(full(backlog=-5), NOW), "capture")["points"] == 150
    assert part(hs.score(full(speed=[(NOW, nan)]), NOW), "speed")["points"] == 0
    assert part(hs.score(full(speed=[(NOW, inf)]), NOW), "speed")["reasons"] == ["p95 5.0 s over the last 5 min (1 reads)"]
    assert part(hs.score(full(speed=[(NOW, -1.0)]), NOW), "speed")["points"] == 250
    assert part(hs.score(full(speed=[(NOW, 1e9)]), NOW), "speed")["points"] == 0
    assert part(hs.score(full(speed=[(nan, 9.0), (NOW, 0.1)]), NOW), "speed")["points"] == 0
    assert hs.score(full(collector={"status": "stale", "age_seconds": nan}), NOW)["parts"][2]["reasons"] == ["collector stale"]


def test_stamps_in_the_future_or_unreadable_count_as_now():
    later = NOW + 3 * 3600
    assert part(hs.score(full(issues=[iso(later)], issues_open=1), NOW), "capture")["points"] == 140
    assert part(hs.score(full(issues=["garbage"], issues_open=1), NOW), "capture")["points"] == 140
    assert part(hs.score(full(speed=[(later, 9.0)]), NOW), "speed")["points"] == 0
    for at in (later, "x", None):
        failures = {"endpoint": {"last": {"at": at, "type": "OSError"}}}
        assert part(hs.score(full(failures=failures), NOW), "errors")["points"] == 100
    # a naive stamp is UTC, as observability._time reads it
    naive = dt.datetime.fromtimestamp(NOW - 3599, dt.timezone.utc).replace(tzinfo=None).isoformat()
    assert part(hs.score(full(issues=[naive], issues_open=1), NOW), "capture")["points"] == 140


def test_a_full_speed_ring_is_named_in_the_reason():
    samples = [(NOW - 60, 6.0)] * 4096
    result = part(hs.score(full(speed=samples, speed_cap=4096), NOW), "speed")
    assert result["reasons"] == ["p95 6.0 s over the last 5 min (4096 reads, the newest 4096 kept)"]
    assert part(hs.score(full(speed=samples[:10], speed_cap=4096), NOW), "speed")["reasons"][0].endswith("(10 reads)")


def test_hub_speed_is_a_five_minute_window_not_a_sample_count():
    """The review's ring case (t-590 r1): 390 reads in 5 min, 30 slow ones early in the window."""
    samples = []
    for i in range(300):
        at = NOW - 300 + i
        samples += [(at, 0.005)] * (3 if i % 3 == 0 else 0) + [(at, 0.3)] * (3 if i % 15 == 0 else 0)
        if i < 120 and i % 4 == 0:
            samples.append((at, 6.0))
    assert part(hs.score(full(speed=samples, speed_cap=4096), NOW), "speed")["points"] == 0


@pytest.mark.parametrize("status", ["fresh", "stale", "unknown", "unavailable", None])
@pytest.mark.parametrize("liveness", ["alive", "stopped", "unknown", None])
def test_only_a_fresh_and_alive_collector_can_read_good(status, liveness):
    """Everything else at full marks: any collector short of fresh and alive keeps the band under
    good (a fresh one of unknown liveness scores 90, so 890 at best)."""
    result = hs.score(full(collector={"status": status, "liveness": liveness}), NOW)
    assert (result["band"] == "good") == (status == "fresh" and liveness == "alive")
