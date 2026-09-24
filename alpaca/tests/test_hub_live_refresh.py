"""The hub refreshes only the surface whose revision moved, and patches the Sessions page in place.

Server side: the SSE revision blob names the pricing revision (recorded rate cards), and the
project analytics index is computed once per (data revision, pricing revision) however many
requests ask for it. Client side: the per-key dirty rules are a pure module tested with
`node --test`; the DOM patch helper and the analytics notes run in headless Chrome against a
throwaway static server. Each client test skips when its tool is missing.
"""
import html
import http.server
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from alpaca import db, serve
from alpaca.analytics import metrics_index, ratecards

WEB = Path(__file__).resolve().parent.parent / "web"
# Headless Chrome keeps its cookie key in a plain store (the system keyring over D-Bus otherwise
# stalls the first http request by about 25 s on this host) and resolves no host but the local
# test server, so the browser tests stay hermetic.
CHROME_FLAGS = ["--password-store=basic", "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1"]


def _start(live, project):
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(live, project))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://127.0.0.1:%d" % httpd.server_address[1]


def _fetch(url, path, headers=None):
    req = urllib.request.Request(url + path, headers=headers or {})
    try:
        r = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as exc:
        r = exc
    return r.status, r.headers, r.read()


# ------------------------------------------------------------------ server: pricing revision
def test_rev_blob_names_the_pricing_revision_and_a_new_rate_card_moves_it(project):
    conn = db.connect(project)
    live = serve.Live(project)
    live.refresh()
    blob = json.loads(live.rev_blob())
    assert {"data", "lib", "board", "pricing"} <= set(blob)
    assert blob["pricing"] == "", "no recorded rate card yet"
    gen = live.gen
    db.append_event(conn, session="agent-1", actor="agent", kind=ratecards.EVENT, ref="claude-test-9",
                    data={"model": "claude-test-9", "provider": "anthropic",
                          "source_url": "https://example.invalid/pricing", "verified_at": "2026-09-20"})
    live.refresh()
    moved = json.loads(live.rev_blob())
    assert moved["pricing"] and moved["pricing"] == ratecards.revision(project)
    assert live.gen > gen, "a changed pricing revision is a push"
    conn.close()


def test_pricing_revision_is_read_only_when_the_input_signature_moves(project, monkeypatch):
    calls = []
    signature = ["a"]
    monkeypatch.setattr(serve, "_data_sig", lambda root: signature[0])
    monkeypatch.setattr(serve.ratecards, "revision", lambda root, **kw: calls.append(1) or "p1")
    live = serve.Live(project)
    live.refresh(fold=False)
    live.refresh(fold=False)
    assert len(calls) == 1 and json.loads(live.rev_blob())["pricing"] == "p1"
    signature[0] = "b"
    live.refresh(fold=False)
    assert len(calls) == 2


# --------------------------------------------------------------- server: analytics index cache
def test_index_is_computed_once_per_data_and_pricing_revision(project, monkeypatch):
    calls = []
    guard = threading.Lock()

    def fake_index(root, sessions):
        with guard:
            calls.append(1)
        time.sleep(0.3)     # hold the computation so the concurrent requests overlap it
        return {"sessions": [], "totals": {"n": len(calls)}, "scope": "main conversations only"}

    monkeypatch.setattr(metrics_index, "index", fake_index)
    conn = db.connect(project)
    live = serve.Live(project)
    live.refresh()
    httpd, url = _start(live, project)
    try:
        results = []
        workers = [threading.Thread(target=lambda: results.append(_fetch(url, "/hub/analytics-index.json")))
                   for _ in range(4)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(30)
        assert len(calls) == 1, "concurrent requests share one computation"
        assert [r[0] for r in results] == [200] * 4
        assert len({r[2] for r in results}) == 1
        body = json.loads(results[0][2])
        assert set(body) == {"sessions", "totals", "scope"}, "the response keeps its shape"
        status, headers, again = _fetch(url, "/hub/analytics-index.json")
        assert status == 200 and again == results[0][2] and len(calls) == 1
        etag = headers.get("ETag")
        assert etag, "a cached index carries a content tag"
        assert _fetch(url, "/hub/analytics-index.json", {"If-None-Match": etag})[0] == 304

        # The analytics fold moves: a new event changes the data revision.
        before = live.data_rev
        db.append_event(conn, session="s1", actor="agent", kind="heartbeat", data={})
        live.refresh()
        assert live.data_rev != before
        _fetch(url, "/hub/analytics-index.json")
        _fetch(url, "/hub/analytics-index.json")
        assert len(calls) == 2

        # A newly recorded rate card moves only the pricing revision.
        with live.lock:
            live.pricing_rev = "another-rate-card-revision"
        status, _, priced = _fetch(url, "/hub/analytics-index.json")
        assert status == 200 and json.loads(priced)["totals"]["n"] == 3
        _fetch(url, "/hub/analytics-index.json")
        assert len(calls) == 3
    finally:
        httpd.shutdown()
        httpd.server_close()
        conn.close()


def test_watcher_warms_the_index_after_a_fold_that_changed_data(project, monkeypatch):
    calls = []
    monkeypatch.setattr(metrics_index, "index",
                        lambda root, sessions: calls.append(1) or {"sessions": [], "totals": {}, "scope": "x"})
    conn = db.connect(project)
    live = serve.Live(project)
    live.refresh()
    _join(live)                              # the first fold computes once for the gap notice
    assert len(calls) == 1
    live.client_enter()                      # a viewer is connected
    live.note_index_request()                # and the HTTP handler served it the index
    db.append_event(conn, session="s1", actor="agent", kind="heartbeat", data={})
    live.refresh()
    warm = live._warm_thread
    assert warm is not None
    warm.join(10)
    assert len(calls) == 2, "the watcher recomputed the index for the new fold"
    live.analytics_index()
    assert len(calls) == 2, "the request after the fold reads the warmed result"
    live.client_exit()
    conn.close()


def test_index_writes_the_pricing_gap_notice_even_without_a_viewer(project, monkeypatch):
    gap = {"model": "claude-opus-9-1", "provider": "anthropic", "responses": 7, "sessions": 2,
           "fix": "bin/alpaca analytics price-check --model claude-opus-9-1"}
    monkeypatch.setattr(metrics_index, "index", lambda root, sessions: {
        "sessions": [], "totals": {"unpriced_models": [gap], "unpriced_other": 0}, "scope": "x"})
    conn = db.connect(project)
    live = serve.Live(project)
    live.refresh()                           # no viewer and no index request yet
    warm = live._warm_thread
    assert warm is not None, "a stale gap notice makes the watcher compute the index"
    warm.join(10)
    assert [g["model"] for g in ratecards.read_gaps(project)] == ["claude-opus-9-1"]
    # Within GAPS_MIN_S and with no viewer, a new fold does not recompute.
    db.append_event(conn, session="s1", actor="agent", kind="heartbeat", data={})
    live.refresh()
    assert live._warm_thread is warm
    conn.close()


def _join(live):
    if live._warm_thread is not None:
        live._warm_thread.join(10)


def _counting_index(monkeypatch, totals=None):
    calls = []
    monkeypatch.setattr(metrics_index, "index", lambda root, sessions: calls.append(1) or {
        "sessions": [], "totals": dict(totals or {}), "scope": "main conversations only"})
    return calls


def _heartbeat(conn):
    db.append_event(conn, session="s1", actor="agent", kind="heartbeat", data={})


# Review finding 2: a viewer who never opened the Sessions page does not pay for the index.
def test_only_a_recent_index_request_makes_the_watcher_warm_it_for_a_viewer(project, monkeypatch):
    calls = _counting_index(monkeypatch)
    conn = db.connect(project)
    live = serve.Live(project)
    live.refresh()
    _join(live)
    first = len(calls)
    live.client_enter()                      # a cockpit viewer who never asks for the index
    for _ in range(3):
        _heartbeat(conn)
        live.refresh()
        _join(live)
    assert len(calls) == first, "folds do not recompute the index for a viewer who never asked"
    httpd, url = _start(live, project)
    try:
        assert _fetch(url, "/hub/analytics-index.json")[0] == 200
        asked = len(calls)
        _heartbeat(conn)
        live.refresh()
        _join(live)
        assert len(calls) == asked + 1, "a recent request makes the watcher warm the next fold"
        live._index_requested_at -= serve.INDEX_VIEW_S + 1
        _heartbeat(conn)
        live.refresh()
        _join(live)
        assert len(calls) == asked + 1, "a request older than the viewing window does not"
    finally:
        httpd.shutdown()
        httpd.server_close()
        live.client_exit()
        conn.close()


# Review finding 6: before the first successful fold there is nothing to index.
def test_the_index_answers_503_and_caches_nothing_before_the_first_fold(project, monkeypatch):
    calls = _counting_index(monkeypatch)
    monkeypatch.setattr(serve.build_index, "_fold", lambda *args: (_ for _ in ()).throw(OSError("fixture fold failure")))
    conn = db.connect(project)
    gap = {"model": "claude-opus-9-1", "provider": "anthropic", "responses": 7, "sessions": 2}
    ratecards.write_gaps(project, [gap], scope="work")          # a notice from an earlier run
    before = ratecards.read_gaps(project)
    assert before
    live = serve.Live(project)
    live.refresh()
    assert live.data_ready.is_set() and live.data_rev == ""
    httpd, url = _start(live, project)
    try:
        status, headers, body = _fetch(url, "/hub/analytics-index.json")
    finally:
        httpd.shutdown()
        httpd.server_close()
        conn.close()
    assert status == 503 and json.loads(body) == {"error": "first analytics fold pending"}
    assert headers.get("Retry-After")
    assert calls == [] and live._index is None, "nothing is computed or cached"
    assert ratecards.read_gaps(project) == before, "the gap notice is not erased"


# Review finding 7: without a viewer, a warm skipped inside GAPS_MIN_S runs once the window passes.
def test_a_skipped_warm_is_retried_after_the_gap_interval(project, monkeypatch):
    calls = _counting_index(monkeypatch)
    conn = db.connect(project)
    live = serve.Live(project)
    live.refresh()
    _join(live)
    first = len(calls)
    _heartbeat(conn)
    live.refresh()
    _join(live)
    live.refresh()
    _join(live)
    assert len(calls) == first, "inside GAPS_MIN_S and with no viewer the fold waits"
    with live.lock:
        live._gaps_at -= serve.GAPS_MIN_S    # the interval passes; nothing else moves
    live.refresh()
    _join(live)
    assert len(calls) == first + 1, "the pending warm runs on a later watcher tick"
    live.refresh()
    _join(live)
    assert len(calls) == first + 1, "once the index is current the retry stops"
    conn.close()


def test_the_index_writes_the_work_scope_of_the_gap_notice(project, monkeypatch):
    gap = {"model": "claude-opus-9-1", "provider": "anthropic", "responses": 7, "sessions": 2, "fix": "x"}
    _counting_index(monkeypatch, {"unpriced_models": [gap], "unpriced_other": 0})
    writes = []
    monkeypatch.setattr(ratecards, "write_gaps", lambda root, gaps, **kw: writes.append(kw))
    db.connect(project).close()
    live = serve.Live(project)
    live.refresh()
    _join(live)
    assert writes and all(kw.get("scope") == "work" for kw in writes), writes


def test_new_hub_modules_are_served(project):
    live = serve.Live(project)
    live.refresh(fold=False)
    httpd, url = _start(live, project)
    try:
        for name in ("liveflags.js", "morph.js"):
            status, headers, body = _fetch(url, "/hub/assets/" + name)
            assert status == 200 and "javascript" in headers["Content-Type"] and body.isascii()
    finally:
        httpd.shutdown()
        httpd.server_close()


# ------------------------------------------------------------------ client: pure live rules
NODE_TEST = r"""
import test from 'node:test';
import assert from 'node:assert/strict';
import * as lf from '%(module)s';
const {changedFlags, dependsOn, autoRefresh, FLAGS} = lf;
const blob = o => JSON.stringify({data:'d1', lib:'l1', board:'b1', pricing:'', ...o});

test('an identical blob marks nothing', () => {
  assert.deepEqual(changedFlags(blob({}), blob({})), []);
});
test('a board change marks overview and runs only', () => {
  assert.deepEqual(changedFlags(blob({}), blob({board:'b2'})), ['overview','runs']);
});
test('a data change marks the session list and analytics', () => {
  assert.deepEqual(changedFlags(blob({}), blob({data:'d2'})), ['sessions','analytics']);
});
test('a pricing change marks the session list and analytics', () => {
  assert.deepEqual(changedFlags(blob({}), blob({pricing:'p2'})), ['sessions','analytics']);
});
test('a library change marks the library only', () => {
  assert.deepEqual(changedFlags(blob({}), blob({lib:'l2'})), ['library']);
});
test('several keys union their surfaces', () => {
  assert.deepEqual(changedFlags(blob({}), blob({lib:'l2', data:'d2'})), ['sessions','analytics','library']);
});
test('an unreadable blob marks everything', () => {
  assert.deepEqual(changedFlags(blob({}), 'not json'), FLAGS);
  assert.deepEqual(changedFlags('{broken', blob({})), FLAGS);
  assert.deepEqual(changedFlags(blob({}), '[1,2]'), FLAGS);
});
test('the sessions page reads data and pricing, not the board', () => {
  assert.equal(dependsOn('sessions', {sessions:true}), true);
  assert.equal(dependsOn('sessions', {analytics:true}), true);
  assert.equal(dependsOn('sessions', {overview:true, runs:true, library:true}), false);
});
test('the cockpit reads the board and data', () => {
  assert.equal(dependsOn('cockpit', ['overview','runs']), true);
  assert.equal(dependsOn('cockpit', ['sessions','analytics']), true);
  assert.equal(dependsOn('cockpit', ['library']), false);
});
test('record pages read the board', () => {
  for (const route of ['overview','work','activity','messages','runs']) {
    assert.equal(dependsOn(route, ['overview','runs']), true, route);
    assert.equal(dependsOn(route, ['sessions','analytics']), false, route);
    assert.equal(dependsOn(route, ['library']), false, route);
  }
});
test('the library reads the library revision', () => {
  assert.equal(dependsOn('library', ['library']), true);
  assert.equal(dependsOn('library', ['overview','runs','sessions','analytics']), false);
});
test('the live log page never refreshes here', () => {
  assert.equal(dependsOn('live', FLAGS), false);
  assert.equal(autoRefresh('live', FLAGS, {}), false);
});
test('the conversation tab is never refreshed automatically', () => {
  assert.equal(autoRefresh('sessions', ['sessions'], {sid:'s1', view:'conversation'}), false);
  assert.equal(dependsOn('sessions', ['sessions']), true, 'the new-activity label still applies');
  assert.equal(autoRefresh('sessions', ['sessions'], {sid:'s1', view:'cost'}), true);
  assert.equal(autoRefresh('sessions', ['analytics'], {sid:null, view:'analysis'}), true);
  assert.equal(autoRefresh('sessions', ['overview'], {sid:'s1', view:'cost'}), false);
});
// Review finding 10: the Work & reports tab renders tasks and messages from the overview.
test('the report tab also reads the overview', () => {
  assert.equal(dependsOn('sessions', ['overview','runs'], {sid:'s1', view:'report'}), true);
  assert.equal(autoRefresh('sessions', ['overview'], {sid:'s1', view:'report'}), true);
  assert.equal(dependsOn('sessions', ['overview'], {sid:'s1', view:'cost'}), false);
  assert.equal(dependsOn('sessions', ['overview'], {sid:null, view:'report'}), false, 'the index has no report tab');
  assert.deepEqual(lf.needs('sessions', {sid:'s1', view:'report'}).sort(), ['analytics','overview','sessions']);
  assert.deepEqual(lf.needs('live', {}), []);
});
// Review finding 8: a revision that arrives while a fetch is in flight keeps its flag.
test('a flag is cleared only when no newer revision marked it during the fetch', () => {
  const flags = lf.createDirty();
  assert.deepEqual(Object.keys(flags.dirty).sort(), [...FLAGS].sort());
  assert.equal(Object.values(flags.dirty).some(Boolean), false);
  flags.mark(['sessions','analytics']);
  assert.equal(flags.dirty.sessions && flags.dirty.analytics, true);
  const seen = flags.seen('sessions');
  flags.mark(['sessions']);                   // a new revision lands mid-fetch
  flags.settle('sessions', seen);             // the fetch returns with the older payload
  assert.equal(flags.dirty.sessions, true, 'the newer revision is not swallowed');
  const again = flags.seen('sessions');
  flags.settle('sessions', again);
  assert.equal(flags.dirty.sessions, false);
  flags.mark(['library']);
  assert.equal(flags.dirty.library, true);
  assert.equal(flags.dirty.overview, false);
});
"""


def test_live_rules_module_with_node(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    spec = tmp_path / "liveflags.test.mjs"
    spec.write_text(NODE_TEST % {"module": (WEB / "liveflags.js").as_uri()}, encoding="utf-8")
    run = subprocess.run([node, "--test", str(spec)], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr
    assert re.search(r"# pass 15\b", run.stdout), run.stdout


# ------------------------------------------------------------- client: DOM patch in a browser
DOM_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>patch test</title></head>
<body><div id="inspector"></div><div id="inspector-label"></div><div id="inspector-content"></div>
<div id="stage"></div><pre id="result">PENDING</pre>
<script type="module">
const out = document.getElementById('result');
const fails = []; let n = 0;
const check = (name, ok) => { n++; if (!ok) fails.push(name); };
const same = (a, b) => { const t = document.createElement('template'); t.innerHTML = b; return a === t.innerHTML; };
try {
  const {patch, render} = await import('/morph.js');
  const {analyticsIndex, renderAnalytics} = await import('/analytics.js');
  const stage = document.getElementById('stage');
  const box = () => { const d = document.createElement('div'); stage.appendChild(d); return d; };

  { // text change: same nodes, new text
    const el = box(); render(el, '<p id="a">one</p><p>two</p>');
    const p = el.firstChild, t = p.firstChild;
    const changed = patch(el, '<p id="a">uno</p><p>two</p>');
    check('text: reports a change', changed === true);
    check('text: element and text node kept', el.firstChild === p && p.firstChild === t);
    check('text: value updated', t.nodeValue === 'uno');
  }
  { // attribute change: set, update and remove
    const el = box(); render(el, '<div class="x" data-k="1" title="t">v</div>');
    const d = el.firstChild;
    patch(el, '<div class="y" title="t" data-new="2">v</div>');
    check('attr: node kept', el.firstChild === d);
    check('attr: updated', d.className === 'y' && d.getAttribute('data-new') === '2');
    check('attr: removed', !d.hasAttribute('data-k'));
  }
  { // row added at the end: earlier rows keep their identity
    const rows = k => '<table><tbody>' + k.map(v => '<tr><td>' + v + '</td></tr>').join('') + '</tbody></table>';
    const el = box(); render(el, rows([1, 2]));
    const [r1, r2] = el.querySelectorAll('tr');
    patch(el, rows([1, 2, 3]));
    const now = el.querySelectorAll('tr');
    check('row added: count', now.length === 3);
    check('row added: first rows kept', now[0] === r1 && now[1] === r2);
    check('row added: markup', same(el.innerHTML, rows([1, 2, 3])));
    patch(el, rows([1, 3]));
    check('row removed: count', el.querySelectorAll('tr').length === 2);
    check('row removed: markup', same(el.innerHTML, rows([1, 3])));
    check('row removed: first row kept', el.querySelector('tr') === r1);
  }
  { // an open <details> the reader toggled stays open, a closed one stays closed
    const el = box(); render(el, '<details><summary>S</summary><p>old</p></details><details open><summary>T</summary><p>x</p></details>');
    const [a, b] = el.querySelectorAll('details');
    a.open = true; b.open = false;
    patch(el, '<details><summary>S</summary><p>new</p></details><details open><summary>T</summary><p>y</p></details>');
    check('details: node kept', el.querySelector('details') === a);
    check('details: opened stays open', a.open === true);
    check('details: closed stays closed', b.open === false);
    check('details: body updated', a.querySelector('p').textContent === 'new' && b.querySelector('p').textContent === 'y');
  }
  { // form control values are the reader's
    const el = box();
    render(el, '<input id="i" value="a" placeholder="p1"><select id="s"><option value="1">1</option><option value="2">2</option></select><textarea id="t">x</textarea>');
    const i = el.querySelector('#i'), s = el.querySelector('#s'), t = el.querySelector('#t');
    i.value = 'typed'; s.value = '2'; t.value = 'typed too';
    patch(el, '<input id="i" value="b" placeholder="p2"><select id="s"><option value="1">1</option><option value="2">2</option><option value="3">3</option></select><textarea id="t">y</textarea>');
    check('form: nodes kept', el.querySelector('#i') === i && el.querySelector('#s') === s && el.querySelector('#t') === t);
    check('form: input value kept', i.value === 'typed');
    check('form: other attributes synced', i.placeholder === 'p2');
    check('form: select value kept', s.value === '2' && s.options.length === 3);
    check('form: textarea value kept', t.value === 'typed too');
  }
  { // unchanged markup is untouched: no mutation, node identity kept
    const html = '<section class="panel"><h2>T</h2><table><tbody><tr><td>1</td></tr></tbody></table><svg viewBox="0 0 10 10"><circle cx="1" cy="1" r="1"></circle></svg></section>';
    const el = box(); render(el, html);
    const before = [...el.querySelectorAll('*')];
    const seen = []; const mo = new MutationObserver(m => seen.push(...m));
    mo.observe(el, {subtree: true, childList: true, attributes: true, characterData: true});
    const changed = patch(el, html);
    const fresh = box(); fresh.innerHTML = html;          // never rendered through the helper
    const freshBefore = [...fresh.querySelectorAll('*')];
    mo.observe(fresh, {subtree: true, childList: true, attributes: true, characterData: true});
    const changedFresh = patch(fresh, html);
    await Promise.resolve();
    seen.push(...mo.takeRecords()); mo.disconnect();
    check('unchanged: reports no change', changed === false && changedFresh === false);
    check('unchanged: no mutation', seen.length === 0);
    check('unchanged: identity kept', [...el.querySelectorAll('*')].every((x, k) => x === before[k]) &&
      [...fresh.querySelectorAll('*')].every((x, k) => x === freshBefore[k]));
  }
  { // a changed element type is replaced; SVG attributes are synced in place
    const el = box(); render(el, '<p>x</p><svg viewBox="0 0 10 10"><circle cx="1"></circle></svg>');
    const svg = el.querySelector('svg');
    patch(el, '<div>x</div><svg viewBox="0 0 20 20"><circle cx="2"></circle></svg>');
    check('type: replaced', el.firstChild.tagName === 'DIV');
    check('svg: kept and synced', el.querySelector('svg') === svg && svg.getAttribute('viewBox') === '0 0 20 20' && svg.querySelector('circle').getAttribute('cx') === '2');
  }
  { // focus stays on a kept element
    const el = box(); render(el, '<button id="b">go</button><span>1</span>');
    const b = el.querySelector('#b'); b.focus();
    patch(el, '<button id="b">go</button><span>2</span>');
    check('focus: kept', document.activeElement === b && el.querySelector('span').textContent === '2');
  }

  { // review finding 9: an opened <details> keeps its place when two siblings are inserted before it
    const card = (k, body) => '<details><summary>' + k + '</summary><p>' + body + '</p></details>';
    const el = box(); render(el, '<div class="ratecards">' + card('C', 'c') + '</div>');
    const c = el.querySelector('details'); c.open = true;
    patch(el, '<div class="ratecards">' + card('A', 'a') + card('B', 'b') + card('C', 'c') + '</div>');
    const all = [...el.querySelectorAll('details')];
    check('details insert: the opened card is kept', all[2] === c && c.open === true);
    check('details insert: new cards closed', all[0].open === false && all[1].open === false);
    patch(el, '<div class="ratecards">' + card('B', 'b') + card('C', 'c') + '</div>');
    check('details remove: the opened card is kept', el.querySelectorAll('details')[1] === c && c.open === true);
  }
  { // review finding 4: keyed children are matched by key before position
    const list = items => '<ul>' + items.map(([k, t]) => '<li data-key="' + k + '">' + t + '</li>').join('') + '</ul>';
    const el = box(); render(el, list([['a', 'A'], ['b', 'B'], ['c', 'C']]));
    const [a, b, c] = el.querySelectorAll('li');
    patch(el, list([['n1', 'N1'], ['n2', 'N2'], ['a', 'A2'], ['b', 'B'], ['c', 'C']]));
    const now = [...el.querySelectorAll('li')];
    check('keyed insert: kept by key', now[2] === a && now[3] === b && now[4] === c && a.textContent === 'A2');
    check('keyed insert: markup', same(el.innerHTML, list([['n1', 'N1'], ['n2', 'N2'], ['a', 'A2'], ['b', 'B'], ['c', 'C']])));
    patch(el, list([['c', 'C'], ['a', 'A2'], ['n2', 'N2']]));
    const moved = [...el.querySelectorAll('li')];
    check('keyed reorder and remove: kept by key', moved[0] === c && moved[1] === a && moved.length === 3);
    check('keyed reorder: markup', same(el.innerHTML, list([['c', 'C'], ['a', 'A2'], ['n2', 'N2']])));
    const sel = box();
    const opts = (items, pick) => '<select data-session-switch>' + items.map(k => '<option data-key="' + k + '" value="' + k + '"' + (k === pick ? ' selected' : '') + '>Session ' + k + '</option>').join('') + '</select>';
    render(sel, opts(['s1', 's2', 's3'], 's1'));
    const s = sel.querySelector('select'), picked = s.selectedOptions[0];
    patch(sel, opts(['n2', 'n1', 's1', 's2', 's3'], 's1'));
    check('keyed options: selection stays on its session', s.value === 's1' && s.selectedOptions[0] === picked && picked.textContent === 'Session s1');
  }

  // ---- analytics notes: unpriced models and recorded rate cards
  const idx = analyticsIndex({sessions: [], totals: {unpriced_models: [{model: 'claude-new-9', provider: 'anthropic', responses: 3, sessions: 2, fix: 'agent-cli ratecard record claude-new-9 <page>'}], unpriced_other: 4}});
  const ix = box(); ix.innerHTML = idx;
  const text = ix.textContent.replace(/\s+/g, ' ');
  check('unpriced: model sentence', text.includes('claude-new-9: 3 responses in 2 sessions have no rate card. An agent records one from the official price page: agent-cli ratecard record claude-new-9 <page>'));
  check('unpriced: fix is code text', [...ix.querySelectorAll('code')].some(c => c.textContent === 'agent-cli ratecard record claude-new-9 <page>'));
  check('unpriced: other responses', text.includes('4 responses lack usable usage or cache-duration data'));
  const keyed = box(); keyed.innerHTML = analyticsIndex({sessions: [{sid: 'sid-1', title: 'One'}, {sid: 'sid-2', title: 'Two'}], totals: {}});
  check('index rows carry the session key', [...keyed.querySelectorAll('tbody tr')].map(r => r.dataset.key).join() === 'sid-1,sid-2');
  const old = box(); old.innerHTML = analyticsIndex({sessions: [], totals: {}});
  check('unpriced: old payload says nothing', !/rate card|lack usable usage/.test(old.textContent));
  const zero = box(); zero.innerHTML = analyticsIndex({sessions: [], totals: {unpriced_models: [], unpriced_other: 0}});
  check('unpriced: empty lists say nothing', !/rate card|lack usable usage/.test(zero.textContent));

  const card = (extra) => ({model: 'claude-new-9', context_tier: 'standard', service_tier: 'standard', verified_at: '2026-09-20', basis: 'Official list price', source_url: 'https://example.invalid/pricing', rates_per_million: {output: 10}, input_multiplier: 1, output_multiplier: 1, ...extra});
  const analysis = (rc, ledger) => ({session: {sid: 's1'}, coverage: {usage_method: 'claude-response-ids', state: 'complete', usage_total_is_exact: true, total_is_exact: true, reason: 'fixture'},
    summary: {responses: ledger.length, costed_responses: ledger.length, estimated_cost_usd: 0.02, average_cost_usd: 0.01, tool_calls: 0, tokens: {input_tokens: 2, output_tokens: 2, total_tokens: 4}},
    context: {timeline: []}, hourly: [], tools: [], models: [{model: 'claude-new-9', responses: ledger.length, costed_responses: ledger.length, tokens: {}, estimated_cost_usd: 0.02}],
    ledger: ledger.map((id, k) => ({id, ts: '2026-09-2' + (k + 1) + 'T00:00:00Z', model: 'claude-new-9', excerpt: 'response ' + id, usage: {input_tokens: 1, output_tokens: 1}, cost: {total_usd: 0.01, breakdown_usd: {output: 0.01}, rate_card: rc}}))});
  const recorded = card({origin: 'recorded', label: {method: 'fetched the official page and matched the model row', source_sha256: 'abcdef0123456789abcdef', source_copy: '.alpaca/ratecards/page.html', row: 'claude-new-9 | $10 / MTok output', verified_by: 'agent-1', recorded_at: '2026-09-20T01:00:00Z'}});
  const cv = box(); cv.innerHTML = renderAnalytics(analysis(recorded, ['r1']), 'cost');
  const ct = cv.textContent.replace(/\s+/g, ' ');
  check('recorded: agent sentence', /recorded by an agent from the official price page on 2026-09-20/i.test(ct));
  check('recorded: method', ct.includes('fetched the official page and matched the model row'));
  check('recorded: hash prefix only', ct.includes('abcdef012345') && !ct.includes('abcdef0123456'));
  check('recorded: row', ct.includes('claude-new-9 | $10 / MTok output'));
  const bv = box(); bv.innerHTML = renderAnalytics(analysis(card({origin: 'built-in'}), ['r1']), 'cost');
  const bt = bv.textContent;
  check('built-in: old wording', bt.includes('Verified 2026-09-20.') && !/recorded by an agent/i.test(bt));
  const lv = box(); lv.innerHTML = renderAnalytics(analysis(card({}), ['r1']), 'cost');
  check('legacy card: old wording', lv.textContent.includes('Verified 2026-09-20.') && !/recorded by an agent/i.test(lv.textContent));

  { // a live patch keeps the ledger search, filter and page state
    stage.querySelectorAll('#response-ledger').forEach(x => x.id = 'ledger-old');
    const el = box(); render(el, renderAnalytics(analysis(recorded, ['r1', 'r2']), 'cost'));
    const input = el.querySelector('[data-analysis-filter="q"]');
    input.value = 'response r2'; input.dispatchEvent(new Event('input', {bubbles: true}));
    const filtered = el.querySelector('#response-ledger').textContent;
    check('ledger: filter applied', filtered.includes('1 responses') && filtered.includes('response r2'));
    patch(el, renderAnalytics(analysis(recorded, ['r1', 'r2', 'r3']), 'cost', {keepLedger: true}));
    const kept = el.querySelector('#response-ledger').textContent;
    check('ledger: search kept after live patch', el.querySelector('[data-analysis-filter="q"]') === input && input.value === 'response r2' && kept.includes('1 responses') && !kept.includes('response r3'));
    render(el, renderAnalytics(analysis(recorded, ['r1', 'r2', 'r3']), 'cost'));
    check('ledger: a full render resets', el.querySelector('#response-ledger').textContent.includes('3 responses'));
  }

  out.textContent = fails.length ? 'FAIL ' + n + ' checks: ' + fails.join('; ') : 'PASS ' + n + ' checks';
} catch (e) {
  out.textContent = 'FAIL exception: ' + (e && e.stack || e);
}
</script></body></html>
"""


def _static_server(page):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(WEB), **kwargs)

        def do_GET(self):
            if self.path.split("?")[0] == "/test.html":
                body = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            return super().do_GET()

        def guess_type(self, path):
            return "text/javascript" if str(path).endswith(".js") else super().guess_type(path)

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def test_patch_helper_and_analytics_notes_in_headless_chrome(tmp_path):
    chrome = shutil.which("google-chrome")
    if not chrome:
        pytest.skip("google-chrome is not installed")
    httpd = _static_server(DOM_PAGE)
    try:
        url = "http://127.0.0.1:%d/test.html" % httpd.server_address[1]
        run = subprocess.run([chrome, "--headless=new", "--disable-gpu", "--no-first-run", *CHROME_FLAGS,
                              "--no-default-browser-check", "--user-data-dir=%s" % (tmp_path / "chrome"),
                              "--virtual-time-budget=5000", "--dump-dom", url],
                             capture_output=True, text=True, timeout=120)
    finally:
        httpd.shutdown()
        httpd.server_close()
    found = re.search(r'<pre id="result">(.*?)</pre>', run.stdout, re.S)
    result = html.unescape(found.group(1)) if found else "no result element: " + run.stdout[-2000:] + run.stderr[-2000:]
    assert result.startswith("PASS"), result


# ------------------------------------------------------ client: the hub page itself, live
# The real hub (hub.html, hub.js, the /hub routes) against a tmp project whose analytics are
# stubbed. Control routes change one revision in memory, inject an outage or slow one fetch; a
# node script drives headless Chrome over the DevTools protocol through each review finding.
HUB_DRIVER = r"""
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
const fetches = pat => js(`performance.getEntriesByType('resource').filter(e=>e.name.includes(${JSON.stringify(pat)})).length`);
const out = []; const check = (name, ok, info = '') => out.push((ok ? 'PASS ' : 'FAIL ') + name + (info !== '' ? ' [' + info + ']' : ''));
let loads = 0;
async function load(hash, ready) {
  const opened = (await ctl('act=state')).sse;
  try { await js('window.__previous=true'); } catch {}
  await send('Page.navigate', {url: `${base}/hub/?load=${++loads}${hash}`});
  if (!await until(`!window.__previous&&(${ready})`, 30000)) throw Error('page did not render: ' + hash);
  const end = Date.now() + 10000; while ((await ctl('act=state')).sse <= opened && Date.now() < end) await sleep(100);
  await sleep(400);                      // the stream's first revision is the baseline
  await js(`document.documentElement.style.minHeight='7000px';true`);
}
const rows = `!!document.querySelector('#analytics-index tbody tr')`;
const indexHas = text => `document.querySelector('#analytics-index').textContent.includes(${JSON.stringify(text)})`;
try {
  await send('Runtime.enable');

  // Finding 1a: a reader who scrolls while an in-place refresh is running keeps the position.
  await load('#sessions', rows);
  await js('scrollTo(0,400);true');
  await ctl('act=index_delay&value=2.5');
  await ctl('act=data&rename=s000&title=Renamed%20A');
  await sleep(2300);
  await js('scrollTo(0,1500);true');
  check('1a patched', await until(indexHas('Renamed A')));
  await sleep(700);
  check('1a reader scroll kept after an in-place patch', await js('window.scrollY') === 1500, await js('window.scrollY'));

  // Finding 1b: navigating away mid-refresh never lands the new page at the old offset.
  await ctl('act=index_delay&value=2.5');
  await js('scrollTo(0,900);true');
  await ctl('act=data&rename=s001&title=Renamed%20B');
  await sleep(1900);
  await js(`location.hash='#sessions?sid=s002&view=cost';true`);
  check('1b detail rendered', await until(`!!document.querySelector('#session-detail #response-ledger')`));
  await sleep(4000);
  check('1b new page not scrolled to the old offset', await js('window.scrollY') < 100, await js('window.scrollY'));
  await ctl('act=index_delay&value=0');

  // Finding 4: two sessions start while a session is open; the switcher keeps that session.
  await load('#sessions?sid=s000&view=analysis', `!!document.querySelector('#session-detail .session-summary')`);
  const options = await js(`document.querySelector('[data-session-switch]').options.length`);
  await ctl('act=data&top=2');
  check('4 switcher gained two sessions', await until(`document.querySelector('[data-session-switch]').options.length===${options + 2}`));
  check('4 switcher still names the open session', await js(`(s=>s.value==='s000'&&s.selectedOptions[0].value==='s000')(document.querySelector('[data-session-switch]'))`),
    await js(`document.querySelector('[data-session-switch]').value`));

  // Finding 10: a board change refreshes the Work & reports tab (tasks and messages).
  await load('#sessions?sid=s000&view=report', `!!document.querySelector('#session-detail .conversation')`);
  const overview0 = await fetches('/hub/overview.json');
  await ctl('act=board');
  check('10 report tab rereads the overview on a board change', await until(`performance.getEntriesByType('resource').filter(e=>e.name.includes('/hub/overview.json')).length>${overview0}`, 8000));

  // Finding 5: an outage during a quiet refresh keeps the page, then the refresh is retried.
  await load('#sessions', rows);
  await ctl('act=fail&path=/data.json&on=1');
  await ctl('act=data&rename=s004&title=Renamed%20E');
  await sleep(3500);
  check('5 data.json outage keeps the page', await js(`!document.querySelector('.error-box')&&!!document.querySelector('#analytics-index tbody tr')`));
  await ctl('act=fail&path=/data.json&on=0');
  await ctl('act=board');
  check('5 the failed refresh is retried', await until(indexHas('Renamed E')));
  await ctl('act=fail&path=/hub/overview.json&on=1');
  await ctl('act=board');
  await sleep(300);
  await ctl('act=data&rename=s005&title=Renamed%20F');
  await sleep(3500);
  check('5 overview.json outage keeps the page', await js(`!document.querySelector('.error-box')&&!!document.querySelector('#analytics-index tbody tr')`));
  await ctl('act=fail&path=/hub/overview.json&on=0');
  await ctl('act=board');
  check('5 retried after the overview outage', await until(indexHas('Renamed F')));

  // Finding 8: a revision that lands while the session list is being fetched is not lost.
  await load('#sessions', rows);
  const count = () => js(`parseInt(document.querySelector('#main .toolbar-end').textContent)`);
  const before = await count();
  const inflight = (await ctl('act=state')).inflight;
  await ctl('act=slow_data&value=3');
  await ctl('act=data&top=1');
  const started = Date.now(); while ((await ctl('act=state')).inflight === inflight && Date.now() - started < 10000) await sleep(50);
  await sleep(1800);
  await ctl('act=data&top=1');
  check('8 the list reaches the newest revision', await until(`parseInt(document.querySelector('#main .toolbar-end').textContent)===${before + 2}`, 15000), `${before} -> ${await count()}`);
} catch (e) { out.push('FAIL exception ' + e.message); }
console.log(out.join('\n'));
ws.close(); chrome.kill();
process.exit(out.some(line => line.startsWith('FAIL')) ? 1 : 0);
"""


def _fixture_analysis(sid):
    ledger = [{"id": "r%d" % k, "ts": "2026-09-2%dT00:00:00Z" % (k + 1), "model": "claude-test-9",
               "excerpt": "response %d" % k, "usage": {"input_tokens": 1, "output_tokens": 1},
               "cost": {"total_usd": 0.01, "breakdown_usd": {"output": 0.01}}} for k in range(3)]
    return {"session": {"sid": sid, "operator": "claude"},
            "coverage": {"usage_method": "claude-response-ids", "state": "complete", "usage_total_is_exact": True,
                         "total_is_exact": True, "reason": "fixture"},
            "summary": {"responses": 3, "costed_responses": 3, "estimated_cost_usd": 0.03, "average_cost_usd": 0.01,
                        "tool_calls": 0, "tokens": {"input_tokens": 3, "output_tokens": 3, "total_tokens": 6}},
            "context": {"timeline": []}, "hourly": [], "tools": [], "ledger": ledger,
            "models": [{"model": "claude-test-9", "responses": 3, "costed_responses": 3, "tokens": {}, "estimated_cost_usd": 0.03}]}


def test_hub_live_refresh_findings_in_headless_chrome(project, tmp_path, monkeypatch):
    chrome, node = shutil.which("google-chrome"), shutil.which("node")
    if not chrome or not node:
        pytest.skip("google-chrome and node are required")
    from urllib.parse import parse_qs, urlsplit
    from alpaca.analytics import detail as detail_module, metrics
    ctl = {"index_delay": 0.0, "fail": set(), "slow_data": 0.0, "inflight": 0, "sse": 0, "n": 0}

    def sessions_now():
        with live.lock:
            return json.loads(live.data_json).get("sessions", [])

    def fake_index(root, sessions):
        time.sleep(ctl["index_delay"])
        rows = [{"sid": s["sid"], "title": s["title"], "first": s["first"], "operator": "claude",
                 "summary": {"responses": 3, "costed_responses": 3, "estimated_cost_usd": 0.03,
                             "tokens": {"input_tokens": 3, "output_tokens": 3}},
                 "coverage": {"state": "complete"}, "cost": {}} for s in sessions]
        return {"sessions": rows, "totals": {"responses": 3 * len(rows)}, "scope": "main conversations only"}

    def fake_detail(root, sid, before=None, limit=80, child=None, around=None, after=None):
        title = next((s["title"] for s in sessions_now() if s["sid"] == sid), sid)
        return {"session": {"sid": sid, "title": title, "operator": "claude", "tool_calls": 0},
                "coverage": {"reason": "fixture capture", "total_is_exact": True}, "children": [],
                "messages": [], "total": 0, "has_more": False, "has_later": False}

    monkeypatch.setattr(metrics_index, "index", fake_index)
    monkeypatch.setattr(detail_module, "detail", fake_detail)
    monkeypatch.setattr(metrics, "analyze", lambda root, sid, child=None: _fixture_analysis(sid))
    monkeypatch.setattr(ratecards, "write_gaps", lambda *args, **kwargs: None)
    db.connect(project).close()
    live = serve.Live(project)
    live.refresh()
    _join(live)
    start = [{"sid": "s%03d" % k, "title": "Session %03d" % k, "class": "work",
              "first": "2026-09-%02dT00:00:00Z" % (24 - k % 20)} for k in range(60)]
    with live.lock:
        live.data_json, live.data_rev = json.dumps({"sessions": start}), "fixture-0"

    def push():
        with live.cond:
            live.gen += 1
            live.cond.notify_all()

    base = serve.make_handler(live, project)

    class Control(base):
        def _sse(self):
            ctl["sse"] += 1
            return super()._sse()

        def do_GET(self):
            url = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/__ctl":
                act = q["act"]
                if act == "data":
                    items = sessions_now()
                    for s in items:
                        if s["sid"] == q.get("rename"):
                            s["title"] = q["title"]
                    for _ in range(int(q.get("top", "0"))):
                        ctl["n"] += 1
                        items.insert(0, {"sid": "new-%02d" % ctl["n"], "title": "New %02d" % ctl["n"],
                                         "class": "work", "first": "2026-09-25T00:00:00Z"})
                    with live.lock:
                        live.data_json = json.dumps({"sessions": items})
                        live.data_rev = "fixture-%s" % time.time()
                    push()
                elif act == "board":
                    with live.lock:
                        board = json.loads(live.board_json)
                        board["fixture_bump"] = time.time()
                        live.board_json = json.dumps(board)
                    push()
                elif act == "fail":
                    (ctl["fail"].add if q["on"] == "1" else ctl["fail"].discard)(q["path"])
                elif act in ("index_delay", "slow_data"):
                    ctl[act] = float(q["value"])
                return self._json({"sse": ctl["sse"], "inflight": ctl["inflight"]})
            if url.path in ctl["fail"]:
                return self._send(b'{"error": "fixture outage"}', "application/json", 503)
            if url.path == "/data.json" and ctl["slow_data"]:
                delay, ctl["slow_data"] = ctl["slow_data"], 0.0
                with live.lock:                  # the payload is read before the wait, so it predates
                    body, etag = live.data_json.encode("utf-8"), live.data_rev
                ctl["inflight"] += 1
                time.sleep(delay)
                return self._send(body, "application/json", etag=etag, cache="no-cache")
            return super().do_GET()

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Control)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    driver = tmp_path / "hub_driver.mjs"
    driver.write_text(HUB_DRIVER, encoding="utf-8")
    try:
        run = subprocess.run([node, str(driver), "http://127.0.0.1:%d" % httpd.server_address[1], str(tmp_path / "chrome")],
                             capture_output=True, text=True, timeout=300, env=dict(os.environ, CHROME=chrome, CHROME_FLAGS=json.dumps(CHROME_FLAGS)))
    finally:
        httpd.shutdown()
        httpd.server_close()
    report = run.stdout + run.stderr
    assert run.returncode == 0 and "FAIL" not in report, report
    assert report.count("PASS") == 12, report
