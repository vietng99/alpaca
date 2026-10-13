"""t-583: the Sessions page polls a board served while it rebuilds, in headless Chrome.

The real hub page against a server that scripts /hub/burn.json: a refreshing board is asked for again
with the index backoff, a same-build-time answer is not redrawn, the note goes when the rebuild lands,
polling stops, and one failed poll fetch does not end the polling."""
import http.server
import json
import os
import shutil
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from alpaca import db, serve
from alpaca.analytics import build_index, burn, metrics, metrics_index, ratecards
from alpaca.tests.test_hub_live_refresh import CHROME_FLAGS, _fixture_analysis, _join
from alpaca.tests.test_burn_cache import build
from alpaca.tests.test_session_insights import NOW

DRIVER = r"""
import {spawn} from 'node:child_process';
import {readFileSync, existsSync} from 'node:fs';
const [,, base, profile] = process.argv;
const chrome = spawn(process.env.CHROME, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check', ...JSON.parse(process.env.CHROME_FLAGS),
  '--remote-debugging-port=0', '--user-data-dir=' + profile, '--window-size=1280,900', 'about:blank'], {stdio: 'ignore'});
const sleep = ms => new Promise(r => setTimeout(r, ms));
let port; for (let i = 0; i < 200 && !port; i++) { await sleep(100); if (existsSync(profile + '/DevToolsActivePort')) port = readFileSync(profile + '/DevToolsActivePort', 'utf8').split('\n')[0]; }
const page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(t => t.type === 'page');
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise(r => ws.onopen = r);
let seq = 0; const pending = new Map();
ws.onmessage = m => { const d = JSON.parse(m.data); if (d.id && pending.has(d.id)) { pending.get(d.id)(d); pending.delete(d.id); } };
const send = (method, params = {}) => new Promise(r => { const n = ++seq; pending.set(n, r); ws.send(JSON.stringify({id: n, method, params})); });
const js = async expr => { const r = await send('Runtime.evaluate', {expression: expr, awaitPromise: true, returnByValue: true}); if (r.result.exceptionDetails) throw Error(expr.slice(0, 80) + ': ' + JSON.stringify(r.result.exceptionDetails).slice(0, 300)); return r.result.result.value; };
const until = async (expr, ms = 20000) => { const end = Date.now() + ms; while (Date.now() < end) { try { if (await js(expr)) return true; } catch {} await sleep(150); } return false; };
const ctl = async query => (await fetch(base + '/__ctl?' + query)).json();
const out = []; const check = (name, ok, info = '') => out.push((ok ? 'PASS ' : 'FAIL ') + name + (info !== '' ? ' [' + info + ']' : ''));
const board = sel => `(document.querySelector('#burn-board')||{textContent:''}).textContent.includes(${JSON.stringify(sel)})`;
const gaps = list => list.slice(1).map((t, i) => Math.round((t - list[i]) / 100) / 10);
try {
  await send('Runtime.enable');
  // Phase A: the first board is served refreshing.
  await ctl('act=set&title=Marker-A&gen=g1&refreshing=1');
  await send('Page.navigate', {url: `${base}/hub/?load=1#sessions`});
  check('A first board drawn', await until(board('Marker-A'), 30000));
  check('A shows Updating', await js(board('Updating')));
  const t0 = (await ctl('act=state')).times.length;
  await sleep(500);
  await ctl('act=set&title=Marker-SAMEGEN&gen=g1&refreshing=1');     // same build time: must not draw
  await sleep(7500);
  let st = await ctl('act=state');
  const times = st.times.slice(t0 - 1).map(x => x[0]);
  check('A re-fetched while refreshing', times.length >= 3, JSON.stringify(gaps(times)));
  const g = gaps(times);
  // The first re-fetch comes after the first delay (2 s); a later one may come early when the page's own live refresh
  // fetches the board in place, but never later than the next backoff step (4 s) and never as a second chain.
  check('A backoff starts at 2 s', g.length >= 2 && g[0] >= 1.8 && g[0] <= 2.8 && g[1] <= 4.8, JSON.stringify(g));
  check('A same generated not redrawn', !(await js(board('Marker-SAMEGEN'))) && await js(board('Marker-A')));
  // The rebuild lands: a new board, not refreshing.
  await ctl('act=set&title=Marker-B&gen=g2&refreshing=0');
  check('A new board drawn after rebuild', await until(board('Marker-B'), 20000));
  check('A Updating gone', !(await js(board('Updating'))));
  const n1 = (await ctl('act=state')).times.length;
  await ctl('act=set&title=Marker-C&gen=g3&refreshing=0');
  await sleep(12000);
  const n2 = (await ctl('act=state')).times.length;
  check('A polling stopped once not refreshing', n2 === n1, `${n1} -> ${n2}`);
  check('A not redrawn afterwards', !(await js(board('Marker-C'))));

  // A failed poll fetch (the server answers 503 for a while) keeps the old board and the polling.
  await ctl('act=set&title=Marker-F&gen=g6&refreshing=1');
  await send('Page.navigate', {url: `${base}/hub/?load=4#sessions`});
  check('D board drawn', await until(board('Marker-F'), 30000));
  await ctl('act=fail&on=1');
  await sleep(2600);
  await ctl('act=fail&on=0');
  await ctl('act=set&title=Marker-G&gen=g7&refreshing=0');
  const recovered = await until(board('Marker-G'), 15000);
  check('D polling survives one failed fetch', recovered);
} catch (e) { out.push('FAIL exception ' + e.message); }
console.log(out.join('\n'));
ws.close(); chrome.kill();
process.exit(0);
"""


def test_a_refreshing_board_is_polled_in_chrome_and_a_failed_fetch_does_not_end_it(project, tmp_path, monkeypatch):
    chrome, node = shutil.which("google-chrome"), shutil.which("node")
    if not chrome or not node:
        pytest.skip("google-chrome and node are required")
    from alpaca.analytics import detail as detail_module
    build(project)
    real = burn.board(project, build_index.collect(project), "24h", now=NOW)
    assert real["sessions"], "fixture board has sessions"
    script = {"title": "Marker-A", "gen": "g1", "refreshing": True, "fail": False, "times": []}
    t_start = time.time()

    def fake_index(root, sessions, *, yield_control=None):
        rows = [{"sid": s["sid"], "title": s["title"], "first": s["first"], "operator": "claude",
                 "summary": {"responses": 3, "costed_responses": 3, "estimated_cost_usd": 0.03,
                             "tokens": {"input_tokens": 3, "output_tokens": 3}},
                 "coverage": {"state": "complete"}, "cost": {}} for s in sessions]
        return {"sessions": rows, "totals": {"responses": 3 * len(rows)}, "scope": "main conversations only"}

    monkeypatch.setattr(metrics_index, "index", fake_index)
    monkeypatch.setattr(metrics, "analyze", lambda root, sid, child=None: _fixture_analysis(sid))
    monkeypatch.setattr(ratecards, "write_gaps", lambda *a, **k: None)
    live = serve.Live(project)
    live.refresh()
    _join(live)
    start = [{"sid": "s%03d" % k, "title": "Session %03d" % k, "class": "work",
              "first": "2026-09-%02dT00:00:00Z" % (24 - k)} for k in range(5)]
    with live.lock:
        live.data_json, live.data_rev = json.dumps({"sessions": start}), "fixture-0"
    base = serve.make_handler(live, project)

    class Control(base):
        def do_GET(self):
            url = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/__ctl":
                if q["act"] == "set":
                    script.update(title=q["title"], gen=q["gen"], refreshing=q["refreshing"] == "1")
                elif q["act"] == "fail":
                    script["fail"] = q["on"] == "1"
                return self._json({"times": script["times"]})
            if url.path == "/hub/burn.json":
                window = q.get("window") or ("range" if q.get("start") else "24h")
                script["times"].append([round((time.time() - t_start) * 1000), window])
                if script["fail"]:
                    return self._send(b'{"error": "fixture outage"}', "application/json", 503)
                value = json.loads(json.dumps(real))
                value["sessions"][0]["title"] = script["title"]
                value["generated"] = script["gen"]
                value["window"]["key"] = window if window in ("24h", "7d", "all") else value["window"]["key"]
                if script["refreshing"]:
                    value["refreshing"] = True
                body = json.dumps(value)
                return self._send(body.encode("utf-8"), "application/json", etag=str(hash(body)), cache="no-cache")
            return super().do_GET()

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Control)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    driver = tmp_path / "b1_driver.mjs"
    driver.write_text(DRIVER, encoding="utf-8")
    try:
        run = subprocess.run([node, str(driver), "http://127.0.0.1:%d" % httpd.server_address[1], str(tmp_path / "chrome")],
                             capture_output=True, text=True, timeout=400,
                             env=dict(os.environ, CHROME=chrome, CHROME_FLAGS=json.dumps(CHROME_FLAGS)))
    finally:
        httpd.shutdown()
        httpd.server_close()
    report = run.stdout + run.stderr
    print(report)
    print("burn fetches:", script["times"])
    assert "PASS D polling survives one failed fetch" in report and "FAIL" not in report, report
