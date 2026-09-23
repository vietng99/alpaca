import json
import os
import re
import sys
import time

from alpaca import db, doctor, serve
from alpaca.analytics import build_index


def _next_wall_second():
    while time.time() - int(time.time()) > 0.1:
        time.sleep(0.01)


def test_live_refresh_observes_wal_event_without_main_db_mtime_change(project):
    conn = db.connect(project)
    _next_wall_second()
    live = serve.Live(project)
    live.refresh()
    assert json.loads(live.data_json)["project"]["events"] == 0

    db.append_event(conn, session="runner", actor="runner",
                    kind="domain-finished", op="domain-run", ref="sim",
                    data={"job_id": "a" * 32, "verdict": "FAIL"})

    live.refresh()
    assert json.loads(live.data_json)["project"]["events"] == 1
    conn.close()


def test_live_reports_stale_fold_and_recovers_after_the_next_success(project, monkeypatch):
    conn = db.connect(project)
    live = serve.Live(project)
    live.refresh()
    original_fold = build_index._fold

    monkeypatch.setattr(build_index, "_fold", lambda *args: (_ for _ in ()).throw(OSError("fixture fold failure")))
    db.append_event(conn, session="s1", actor="agent", kind="heartbeat", data={})
    live.refresh()
    assert json.loads(live.health_json())["state"] == "stale"

    monkeypatch.setattr(build_index, "_fold", original_fold)
    db.append_event(conn, session="s1", actor="agent", kind="heartbeat", data={})
    live.refresh()
    assert json.loads(live.health_json())["state"] == "fresh"
    conn.close()


def test_live_page_includes_explicit_dashboard_health_indicator(project):
    page = serve._serve_html(project)
    assert 'id="alpaca-serve-health"' in page
    assert "/health.json" in page


# ------------------------------------------------- the analytics page reads in either theme
APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "analytics", "app.html")


def _app():
    with open(APP, "r", encoding="utf-8") as fh:
        return fh.read()


def test_the_analytics_page_ships_a_visible_theme_toggle(project):
    text = _app()
    assert 'id="theme-toggle"' in text and "<button" in text, "a toggle a reader can press"
    assert "data-theme" in text, "the toggle flips data-theme on the root element"
    assert "localStorage" in text, "the choice is remembered"
    assert re.search(r"try\s*\{[^}]*localStorage", text), "storage access is wrapped in try/catch"
    assert "prefers-color-scheme: dark" in text, "with nothing remembered the OS decides"
    # the served page carries it too, so the toggle is there whether the page is written or served.
    assert 'id="theme-toggle"' in serve._serve_html(project)


def test_the_analytics_page_declares_both_theme_token_blocks_over_one_palette():
    text = _app()
    assert "@media (prefers-color-scheme:dark)" in text
    assert ":root:not([data-theme=light])" in text, "dark under the OS, with an opt-out to light"
    assert ":root[data-theme=dark]" in text, "dark under the explicit override"
    assert re.search(r"body\s*\{[^}]*background:\s*var\(--", text), \
        "body sets an explicit background from a token"
    # negative: no colour is written straight onto body outside the token set.
    assert not re.search(r"body\s*\{[^}]*background:\s*#", text)


def test_the_analytics_page_is_pure_ascii():
    with open(APP, "rb") as fh:
        raw = fh.read()
    assert [b for b in raw if b > 0x7F] == [], "the analytics page carries a byte above 0x7F"


def test_doctor_shows_no_profile_lines_without_a_profile(project):
    conn = db.connect(project)
    db.append_event(conn, session="runner", actor="runner", kind="domain-finished",
                    op="domain-run", ref="sim", data={"job_id": "a" * 32, "verdict": "FAIL"})
    findings = {f["name"] for f in doctor.checks(project)}
    assert not any(name.startswith(("flow-", "profile")) for name in findings)
    conn.close()


def test_data_signature_includes_registered_subagent_transcript(project):
    conn = db.connect(project)
    transcript = os.path.join(project, ".alpaca", "transcripts", "main.jsonl")
    os.makedirs(os.path.dirname(transcript), exist_ok=True)
    with open(transcript, "w", encoding="utf-8") as fh:
        fh.write("{}\n")
    db.upsert(conn, "sessions", "sid", {"sid": "main", "transcript": transcript})
    before = serve._data_sig(project)

    subagent = os.path.join(project, ".alpaca", "transcripts", "main", "subagents", "worker", "agent-1.jsonl")
    os.makedirs(os.path.dirname(subagent), exist_ok=True)
    with open(subagent, "w", encoding="utf-8") as fh:
        fh.write("{}\n")

    assert serve._data_sig(project) != before
    conn.close()


def test_live_marks_input_revision_failure_stale_and_retries(project, monkeypatch):
    conn = db.connect(project)
    live = serve.Live(project)
    live.refresh()
    live.refresh()  # Prime the stable post-DB signature before a transient read failure.
    original_sig = serve._data_sig

    monkeypatch.setattr(serve, "_data_sig", lambda _root: (_ for _ in ()).throw(OSError("fixture read failure")))
    live.refresh()
    assert json.loads(live.health_json())["state"] == "stale"

    monkeypatch.setattr(serve, "_data_sig", original_sig)
    live.refresh()
    assert json.loads(live.health_json())["state"] == "fresh"
    conn.close()


def test_live_refreshes_after_stable_hook_cost_metadata_update(project):
    conn = db.connect(project)
    db.upsert(conn, "sessions", "sid", {"sid": "session-a", "started": "2026-09-17T00:00:00+00:00"})
    live = serve.Live(project)
    live.refresh()
    live.refresh()

    db.meta_set(conn, "hook-cost:session-a:stop", json.dumps({"runs": 1, "total_ms": 123.4}))
    live.refresh()

    data = json.loads(live.data_json)
    session = next(item for item in data["sessions"] if item["sid"] == "session-a")
    assert session["record"]["hook_runtime_ms"] == 123.4
    conn.close()


# ---------------------------------------------------------------------------------------------
# The session list the owner reads: work sessions only, probes as one line, a live badge.
#
# The desktop app opens a fresh session every few minutes only to read usage. 62 of 67 sessions on
# this record were such probes, 1 to 3 s each, and they filled the list so completely that no one
# could see the sessions that did the work. The fold now labels each session and the page lists
# only the work ones.
#
# The page is driven the way it runs in a browser: the inline script is lifted out of app.html and
# executed under `node` over a small stand-in DOM, so these are not string searches.
# Every control drives a POSITIVE and a NEGATIVE path.
import json
import shutil
import subprocess

import pytest

APP_STUB = r"""
function El(id) {
  this.id = id || "";
  this.attrs = {};
  this.innerHTML = "";
  this.textContent = "";
  this.className = "";
  this.hidden = false;
  this.scrollTop = 0;
  this.__h = null;
}
El.prototype.setAttribute = function (k, v) { this.attrs[k] = v; };
El.prototype.getAttribute = function (k) {
  return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null;
};
El.prototype.addEventListener = function () {};
El.prototype.scrollIntoView = function () {};
El.prototype.querySelectorAll = function () { return []; };
El.prototype.querySelector = function () { return null; };
El.prototype.closest = function () { return null; };
El.prototype.classList = { toggle: function () {} };

var REG = {};
var document = {
  documentElement: new El("html"),
  getElementById: function (id) { return REG[id] || (REG[id] = new El(id)); },
  querySelector: function () { return null; },
  querySelectorAll: function () { return []; },
  addEventListener: function () {},
  hidden: false
};
var localStorage = { getItem: function () { return null; }, setItem: function () {} };
var window = { matchMedia: function () { return { matches: false }; },
               addEventListener: function () {}, EventSource: FAKE_ES,
               location: { protocol: "http:" } };
var EventSource = FAKE_ES;
var fetch = FAKE_FETCH;

function DUMP() {
  var out = { badge: "", badgeClass: "", badgeHidden: true };
  var names = ["tiles", "list", "probes", "svc", "daily", "tools"];
  for (var i = 0; i < names.length; i++) {
    out[names[i]] = REG[names[i]] ? REG[names[i]].innerHTML : "";
  }
  if (REG.livetext) { out.badge = REG.livetext.textContent; }
  if (REG.livebadge) {
    out.badgeClass = REG.livebadge.className;
    out.badgeHidden = REG.livebadge.hidden;
  }
  return out;
}
"""

APP_ES = r"""(function () {
  function ES(url) {
    var self = this;
    this.listeners = {};
    this.addEventListener = function (kind, fn) {
      (self.listeners[kind] = self.listeners[kind] || []).push(fn);
    };
    setTimeout(function () {
      if (self.onopen) { self.onopen(); }
      var revs = self.listeners.rev || [];
      for (var i = 0; i < revs.length; i++) {
        revs[i]({ data: JSON.stringify({ data: "d1", lib: "l1" }) });
      }
    }, 5);
  }
  return ES;
}())"""

APP_NO_ES = "undefined"

APP_FETCH_DEAD = """(function () {
  return function () { return Promise.reject(new Error("connection refused")); };
}())"""


def _app_fetch(payload):
    return ("""(function () {
  var BODY = %s;
  return function (url) {
    if (String(url).indexOf("library.json") >= 0) {
      return Promise.resolve({ ok: true, json: function () { return Promise.resolve([]); } });
    }
    if (String(url).indexOf("data.json") >= 0) {
      return Promise.resolve({ ok: true, json: function () { return Promise.resolve(BODY); } });
    }
    return Promise.reject(new Error("no such endpoint"));
  };
}())""" % json.dumps(payload))


def _app_script():
    body = re.search(r"<script>\n(.*?)\n</script>", _app(), re.S)
    assert body, "the analytics page carries one inline script"
    return body.group(1)


def run_app(tmp_path, *, inline=None, fetch=APP_FETCH_DEAD, eventsource=APP_NO_ES, settle_ms=0):
    """Run the analytics page's script over the stand-in DOM and return what it wrote."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on this host")
    stub = APP_STUB.replace("FAKE_ES", eventsource).replace("FAKE_FETCH", fetch)
    slot = json.dumps(json.dumps(inline)) if inline is not None else json.dumps("null")
    head = stub + "\nREG.data = new El('data');\nREG.data.textContent = " + slot + ";\n"
    # write the dump and exit only once stdout has drained: a big payload is larger than the pipe
    # buffer, and process.exit() on its own would cut the line in half.
    tail = ("\nsetTimeout(function () { process.stdout.write(JSON.stringify(DUMP()) + \"\\n\", "
            "function () { process.exit(0); }); }, %d);\n" % settle_ms)
    js = tmp_path / "app-run.js"
    js.write_text(head + _app_script() + tail, encoding="utf-8")
    done = subprocess.run([shutil.which("node"), str(js)], capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def _session(sid, cls, last, **extra):
    row = {"sid": sid, "class": cls, "title": sid, "first": "2026-09-18T09:00:00+07:00",
           "last": last, "turns": 1, "tool_calls": 1, "subagents": 0, "human_prompts": [],
           "tools": {}, "files": {}, "hourly": {}, "tokens": {}, "span_s": 60,
           "record": {"events": 1, "level": "L4"}}
    row.update(extra)
    return row


APP_PAYLOAD = {
    "project": {"name": "Alpaca", "generated": "2026-09-18T18:38:10+07:00",
                "sessions": 2, "probe_sessions": 62, "turns": 48, "tool_calls": 34,
                "prompts": 9, "events": 2756, "ops": {"open": 1}, "tasks": {"done": 12},
                "cost_reported_usd": None, "cost_notional_usd": None},
    "daily": [{"day": "2026-09-18", "turns": 48, "tools": 34, "sessions": 2}],
    "tools": [{"name": "Edit", "n": 20}],
    "probes": {"count": 62, "first": "2026-09-18T14:23:00+07:00",
               "last": "2026-09-18T18:28:00+07:00"},
    "sessions": [
        _session("older-work", "work", "2026-09-18T11:00:00+07:00", turns=8, tool_calls=4),
        _session("newer-work", "work", "2026-09-18T18:30:00+07:00", turns=40, tool_calls=30,
                 subagents=3, record={"events": 9, "level": "L6"}),
        _session("probe-aaa", "probe", "2026-09-18T18:28:00+07:00", span_s=2),
        _session("alpaca-flow", "service", "2026-09-18T18:20:00+07:00",
                 record={"events": 118, "level": None}),
    ],
}


@pytest.fixture
def app_view(tmp_path):
    return run_app(tmp_path, inline=APP_PAYLOAD)


def test_the_session_list_holds_work_sessions_only(app_view):
    listing = app_view["list"]
    assert "newer-work" in listing and "older-work" in listing
    # negative: neither a probe nor a service id is a row in this list.
    assert "probe-aaa" not in listing, "a usage probe is never a row"
    assert "alpaca-flow" not in listing, "a service id is never a row"


def test_the_session_list_is_ordered_by_the_newest_activity(app_view):
    listing = app_view["list"]
    assert listing.index("newer-work") < listing.index("older-work")


def test_a_subagent_count_sits_under_its_parent_session(app_view):
    assert "3 subagents under this session" in app_view["list"]
    # negative: a session that spawned none says nothing at all.
    assert app_view["list"].count("subagent") == 1


def test_probes_are_one_summary_row_with_no_per_probe_rows(app_view):
    row = app_view["probes"]
    assert "62 usage probes" in row
    assert "14:23 to 18:28" in row
    assert "desktop app usage checks, 1 to 3 s each" in row
    assert "Not listed above." in row
    assert row.count("usage probes") == 1


def test_service_ids_are_a_small_footer_line(app_view):
    assert "alpaca-flow (118 events)" in app_view["svc"]
    assert "Ids that write without a person behind them" in app_view["svc"]


def test_the_headline_totals_count_working_sessions_and_name_probes_apart(app_view):
    tiles = app_view["tiles"]
    assert "working sessions" in tiles
    assert "usage probes, not listed" in tiles
    assert ">2<" in tiles and ">62<" in tiles
    # negative: the bare word "sessions" is no longer a total on its own.
    assert "<span>sessions</span>" not in tiles


def test_a_payload_without_the_labels_still_lists_every_session(tmp_path):
    older = {"project": {"name": "old", "generated": "2026-09-18T00:00:00+07:00"},
             "daily": [], "tools": [],
             "sessions": [_session("plain-one", None, "2026-09-18T10:00:00+07:00")]}
    older["sessions"][0].pop("class")
    out = run_app(tmp_path, inline=older)
    assert "plain-one" in out["list"], "an unlabelled session reads as work"
    assert out["probes"] == "", "and no probe line is invented"
    assert out["svc"] == ""


def test_an_empty_search_result_says_so_rather_than_showing_nothing(tmp_path):
    empty = {"project": {"name": "x", "generated": "t"}, "daily": [], "tools": [], "sessions": []}
    out = run_app(tmp_path, inline=empty)
    assert "No working session matches." in out["list"]


# ------------------------------------------------------------------ the live badge, three states
def test_the_analytics_badge_reads_live_while_the_stream_is_open(tmp_path):
    out = run_app(tmp_path, fetch=_app_fetch(APP_PAYLOAD), eventsource=APP_ES, settle_ms=120)
    assert out["badgeHidden"] is False
    assert out["badge"].startswith("live, server pushed "), out["badge"]
    assert "is-live" in out["badgeClass"]


def test_the_analytics_badge_reads_polling_with_no_stream(tmp_path):
    out = run_app(tmp_path, fetch=_app_fetch(APP_PAYLOAD), eventsource=APP_NO_ES, settle_ms=120)
    assert out["badge"].startswith("polling every 4 s, data read "), out["badge"]
    assert "is-poll" in out["badgeClass"] and "is-live" not in out["badgeClass"]


def test_the_analytics_badge_reads_offline_since_a_clock_time_after_a_failed_fetch(tmp_path):
    out = run_app(tmp_path, fetch=APP_FETCH_DEAD, eventsource=APP_NO_ES, settle_ms=120)
    assert re.match(r"^offline since \d\d:\d\d:\d\d, retry in \d+ s$", out["badge"]), out["badge"]
    assert "is-off" in out["badgeClass"]


def test_a_written_copy_of_the_analytics_page_shows_no_badge(app_view):
    assert app_view["badgeHidden"] is True, "a copy on disk has no server to be live against"


def test_the_analytics_page_explains_itself_and_points_at_the_cockpit():
    text = _app()
    flat = re.sub(r"\s+", " ", text)
    assert '<p class="intro">' in text
    assert "cockpit is the other page" in flat, "the intro says how the two pages differ"
    assert "reads the chat transcripts" in flat, "and what this one reads"
    assert 'id="cockpit-link"' in text, "and it links there"
    # negative: the retired classic board is linked from nowhere on the page.
    assert "board.html" not in text and "/board/" not in text


def test_the_analytics_badge_counts_on_its_own_and_backs_off_to_thirty_seconds():
    text = _app_script()
    assert "badgeTimer=setInterval(paintLive,1000)" in text
    assert "BACKOFF_CAP=30000" in text
    assert "BACKOFF=Math.min(BACKOFF_CAP,BACKOFF*2)" in text
    assert "function timersOff()" in text and "visibilitychange" in text
    body = re.search(r"function paintLive\(\)\{(.*?)\n\}", text, re.S).group(1)
    assert "fetch(" not in body, "the seconds-ago text is repainted without refetching"


def test_the_analytics_script_parses(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on this host")
    js = tmp_path / "app.js"
    js.write_text(_app_script(), encoding="utf-8")
    done = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
