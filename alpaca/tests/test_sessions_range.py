"""t-483: a From/To day range on the Session analytics page.

The burn board covers a custom range the browser sends as explicit instants (its own local
midnights), with the bucket size picked for the span, and the route refuses a reversed, future or
malformed range with a JSON 400. The session list keeps only sessions active in the chosen local
days and sums its figures over them; a day after today is moved to today and a From after To is
refused with a message.
"""
import http.server
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from alpaca import serve, transcripts
from alpaca.analytics import build_index, burn
from alpaca.tests.test_sessions_burn import NOW, family, register, response, write

WEB = Path(__file__).resolve().parents[1] / "web"
NODE = shutil.which("node")
ICT = timezone(timedelta(hours=7))


def midnight(y, m, d):
    return datetime(y, m, d, tzinfo=ICT).timestamp()


def ranged(root, start, end, now=NOW):
    return burn.board(root, build_index.collect(root), start=start, end=end, now=now)


def test_board_covers_exactly_a_custom_range(project):
    register(project, "s1")
    start, end = midnight(2026, 9, 29), midnight(2026, 9, 30)          # one local day, +07:00
    write(transcripts.local_path(project, "s1"), [
        response("before", start - 60),                                 # the evening before
        response("in1", start + 10), response("in2", start + 3600 * 10),
        response("after", end + 60)])                                    # the next local day
    d = ranged(project, start, end)
    w = d["window"]
    assert w["key"] == "range" and w["start"] == start and w["end"] == end
    assert w["bucket_s"] == 900 and w["buckets"] == 96
    assert d["totals"]["requests"] == 2 and d["sessions"][0]["requests"] == 2
    assert d["sessions"][0]["bins"] == {"0": 110, "40": 110}            # bins count from local midnight
    week = ranged(project, midnight(2026, 9, 23), end)
    assert week["window"]["bucket_s"] == 1800 and week["totals"]["requests"] == 3
    # ISO with its offset is the same instant as the epoch.
    iso = ranged(project, "2026-09-29T00:00:00+07:00", "2026-09-30T00:00:00+07:00")
    assert iso["window"]["start"] == start and iso["totals"]["requests"] == 2
    # A range that runs past now ends at now; its last bucket may be partial.
    today = ranged(project, midnight(2026, 9, 30), midnight(2026, 10, 1))
    assert today["window"]["end"] == NOW and today["window"]["buckets"] == 76   # 19 h since local midnight
    # The fixed windows are unchanged.
    assert burn.board(project, build_index.collect(project), "24h", now=NOW)["window"]["key"] == "24h"


@pytest.mark.parametrize("start,end,why", [
    (midnight(2026, 9, 30), midnight(2026, 9, 29), "starts after it ends"),
    (midnight(2026, 9, 29), midnight(2026, 9, 29), "starts after it ends"),
    (NOW + 3600, NOW + 7200, "future"),
    ("yesterday", midnight(2026, 9, 29), "not an epoch"),
    ("2026-09-29T00:00:00", midnight(2026, 9, 30), "offset"),
    (midnight(2026, 9, 29), None, "both start and end"),
    (NOW - 20 * 365 * 86400, NOW, "ten years"),
    (NOW - 3654 * 86400, NOW, "ten years"),
])
def test_a_bad_custom_range_is_refused(start, end, why):
    with pytest.raises(ValueError, match=why):
        burn.custom_range(start, end, NOW)


def test_burn_route_serves_a_range_and_refuses_bad_ones(project):
    family(project)
    live = serve.Live(project)
    live.refresh()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(live, project))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d" % httpd.server_address[1]

    def get(**params):
        try:
            r = urllib.request.urlopen(url + "/hub/burn.json?" + urllib.parse.urlencode(params), timeout=15)
        except urllib.error.HTTPError as exc:
            r = exc
        return r.status, json.loads(r.read())
    now = time.time()
    try:
        status, data = get(start=now - 2 * 86400, end=now + 86400)
        assert status == 200 and data["window"]["key"] == "range"
        assert data["window"]["start"] == pytest.approx(now - 2 * 86400) and data["window"]["end"] <= time.time()
        other = get(start=now - 3 * 86400, end=now - 86400)[1]
        assert other["window"]["start"] == pytest.approx(now - 3 * 86400)          # a range is its own cache entry
        assert get(window="24h")[1]["window"]["key"] == "24h"
        for params in ({"start": now - 3600, "end": now - 7200},                 # reversed
                       {"start": now + 3600, "end": now + 7200},                 # in the future
                       {"start": "monday", "end": now},                          # malformed
                       {"start": now - 3600}):                                   # one bound only
            status, body = get(**params)
            assert status == 400 and body["error"], params
    finally:
        httpd.shutdown()
        httpd.server_close()


def node(script, tz="Asia/Ho_Chi_Minh"):
    if not NODE:
        pytest.skip("node is required")
    run = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True,
                         cwd=str(WEB), timeout=60, env={**os.environ, "TZ": tz})
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_session_list_keeps_sessions_active_in_the_local_days():
    got = node("""
import {sessionList,dayRange} from './sessionlist.js';
const row=(sid,first,last,cost,tok)=>({sid,title:sid,operator:'claude',first,last,coverage:{complete:true},
 summary:{responses:1,usage_responses:1,costed_responses:1,estimated_cost_usd:cost,tokens:{total_tokens:tok}},
 subagents:{count:0,measured:0},family:{estimated_cost_usd:cost,total_tokens:tok,responses:1,costed_responses:1}});
const index={totals:{estimated_cost_usd:99},sessions:[
 row('a','2026-10-01T10:00:00+07:00','2026-10-07T09:00:00+07:00',5,500),   // spans the range
 row('b','2026-10-03T23:30:00+07:00','2026-10-03T23:50:00+07:00',1,100),   // inside, late evening local
 row('c','2026-10-04T00:10:00+07:00',null,2,200),                          // the day after, start only
 row('d','2026-10-02T16:59:00Z','2026-10-02T16:59:30Z',3,300),             // 23:59 local on Oct 2
 row('e','2026-09-20T10:00:00+07:00','2026-09-21T10:00:00+07:00',7,700),   // before
 row('f',null,null,4,400)]};                                               // no time recorded
const today='2026-10-08',r=dayRange('2026-10-03','2026-10-03',today),list=sessionList(index,'','','newest',r);
console.log(JSON.stringify({start:new Date(r.start).toISOString(),end:new Date(r.end).toISOString(),
 ids:list.sessions.map(s=>s.sid).join(''),cost:list.totals.estimated_cost_usd,tokens:list.totals.total_tokens,shown:list.filtered,
 wide:sessionList(index,'','','oldest',dayRange('2026-10-02','2026-10-04',today)).sessions.map(s=>s.sid).join(''),
 none:sessionList(index).totals.estimated_cost_usd,
 reversed:dayRange('2026-10-05','2026-10-03',today),future:dayRange('2026-10-07','2026-12-01',today),
 allFuture:dayRange('2026-11-01','2026-11-02',today),fromOnly:dayRange('2026-10-06','',today),toOnly:dayRange('','2026-10-06',today),
 bad:dayRange('2026-02-30','',today).error,empty:dayRange('','',today),
 refused:sessionList(index,'','','newest',dayRange('2026-10-05','2026-10-03',today)).sessions.length}));""")
    assert got["start"] == "2026-10-02T17:00:00.000Z" and got["end"] == "2026-10-03T17:00:00.000Z"   # local midnights
    assert got["ids"] == "ba"                       # active in Oct 3 local; d ended 23:59 Oct 2, c began Oct 4
    assert got["cost"] == 6 and got["tokens"] == 600 and got["shown"] == {"shown": 2, "of": 6}
    assert got["wide"] == "adbc"
    assert got["none"] == 99                        # no range, no filter: the server totals stand
    assert "after the To date" in got["reversed"]["error"] and "start" not in got["reversed"]
    assert got["future"]["to"] == "2026-10-08" and got["future"]["note"].startswith("Dates after today")
    assert (got["allFuture"]["from"], got["allFuture"]["to"]) == ("2026-10-08", "2026-10-08")
    assert (got["fromOnly"]["from"], got["fromOnly"]["to"]) == ("2026-10-06", "2026-10-08")
    assert (got["toOnly"]["from"], got["toOnly"]["to"]) == ("2026-10-06", "2026-10-06")
    assert got["bad"] and got["empty"] is None
    assert got["refused"] == 6                      # a refused range filters nothing
    hub = (WEB / "hub.js").read_text(encoding="utf-8")
    assert "dayInput('from','From'" in hub and "data-clear=\"from,to\"" in hub
    assert "{start:days.start/1000,end:days.end/1000}" in hub
