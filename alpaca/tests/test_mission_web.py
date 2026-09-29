"""Exercise the mission map's real browser behavior and authenticated API boundary."""
import functools
import html
import http.server
import json
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from alpaca import db, mission
from alpaca.tests.test_mission import definition

WEB = Path(__file__).resolve().parents[1] / 'web'


def fixture(root):
    (root / 'src').mkdir(parents=True, exist_ok=True)
    for name in ('parser', 'usage', 'view'): (root / 'src' / (name + '.txt')).write_text(name)
    data = definition()
    data['nodes'][0]['title'] = '<img src=x onerror="window.injected=1">'
    mission.define(root, data, session='owner')
    conn = db.connect(root)
    db.upsert(conn, 'ops', 'id', {'id': 'op-1', 'intent': 'Improve parser', 'status': 'open'})
    db.upsert(conn, 'tasks', 'id', {'id': 't-1', 'op': 'op-1', 'statement': 'Repair parser', 'title': 'Repair parser', 'status': 'doing'})
    for ident, title in [('t-2', 'Define usage interface'), ('t-3', 'Build the consumer'), ('t-4', 'Unmapped improvement')]:
        db.upsert(conn, 'tasks', 'id', {'id': ident, 'op': 'op-1', 'statement': title, 'title': title, 'status': 'open'})
    from alpaca import taskplan
    taskplan.record(conn, 'op-1', {'version': 1, 'revision': 0,
        'groups': [{'id': 'parser', 'title': 'Parser repairs'}, {'id': 'usage', 'title': 'Usage development'}],
        'tasks': [{'id': ident, 'group': 'parser' if ident == 't-1' else 'usage', 'after': ['t-2'] if ident == 't-3' else [],
                   'resources': [], 'requires_stages': [], 'hold': None} for ident in ('t-1', 't-2', 't-3', 't-4')]},
        session='owner', actor='owner')
    conn.close()
    mission.link(root, 'parser', 't-1', 'work', session='owner')
    mission.link(root, 'usage', 't-2', 'work', session='owner')
    mission.link(root, 'view', 't-3', 'work', session='owner')
    return mission.project(root)


SCRIPT = r"""
const checks = {};
const publish=value=>{document.querySelector('#results').textContent=JSON.stringify(value);if(parent!==window)parent.document.querySelector('#results').textContent=JSON.stringify(value);};
const assert=(name,test)=>checks[name]=Boolean(test);
const tick=()=>new Promise(resolve=>setTimeout(resolve,70));
try {
  const {renderMission,bindMission,rememberMission}=await import('/mission.js');
  const root=document.querySelector('#app');
  const render=()=>{rememberMission(root);root.innerHTML=renderMission(DATA);bindMission(root);};
  render();await tick();
  assert('all_nodes_present',root.querySelectorAll('[data-mm-node]').length===3);
  assert('development_default',root.dataset.mode==='work'||root.querySelector('[data-mm-root]')?.dataset.mode==='work');
  assert('concrete_work_visible',root.querySelectorAll('[data-rm-node]').length===4);
  assert('ready_work_visible',root.querySelector('[data-rm-node="t-2"]')?.textContent.includes('Ready'));
  assert('waiting_work_explained',root.querySelector('[data-rm-node="t-3"]')?.textContent.includes('t-2'));
  assert('unmapped_work_visible',root.textContent.includes('Needs component mapping')&&root.textContent.includes('Unmapped improvement'));
  assert('recorded_task_edges',!!root.querySelector('[data-rm-edge="t-2:t-3"]'));
  assert('no_invented_task_edges',!root.querySelector('[data-rm-edge="t-1:t-2"]'));
  root.querySelector('[data-rm-operation="op-1"]').click();await tick();
  assert('operation_selected',root.querySelector('[data-rm-picker="operation"] summary').textContent.includes('op-1'));
  root.querySelector('[data-rm-picker="operation"] summary').focus();render();await tick();
  assert('roadmap_focus_survives_refresh',document.activeElement?.matches('[data-rm-picker="operation"] summary'));
  assert('operation_survives_refresh',root.querySelector('[data-rm-picker="operation"] summary').textContent.includes('op-1'));
  assert('escaped_labels',!root.querySelector('img')&&!window.injected&&root.textContent.includes('<img'));
  root.querySelector('[data-mm-select="parser"]').click();await tick();
  var componentFilter=root.querySelector('[data-mm-component-only]');componentFilter.focus();componentFilter.checked=true;componentFilter.dispatchEvent(new Event('change',{bubbles:true}));await tick();
  assert('component_filter',root.querySelectorAll('[data-rm-node]').length===1&&!!root.querySelector('[data-rm-node="t-1"]'));
  assert('component_filter_focus',document.activeElement?.matches('[data-mm-component-only]'));
  componentFilter=root.querySelector('[data-mm-component-only]');componentFilter.checked=false;componentFilter.dispatchEvent(new Event('change',{bubbles:true}));await tick();
  assert('unchecked_is_labelled',root.querySelector('[data-mm-detail]').textContent.includes('Not checked'));
  assert('detail_opens',root.querySelector('[data-mm-detail]').textContent.includes('Own parser'));
  assert('distinct_timestamps',root.querySelector('[data-mm-detail]').textContent.includes('Last verified fix')&&root.querySelector('[data-mm-detail]').textContent.includes('Last checked'));
  assert('task_link',!!root.querySelector('a[href*="q=t-1"]'));
  root.querySelector('[data-mm-mode="impact"]').click();await tick();
  assert('transitive_impact',root.querySelector('[data-mm-node="view"]').classList.contains('mm-affected'));
  assert('path_explained',root.querySelector('[data-mm-detail]').textContent.includes('usage consumes parser'));
  root.querySelector('[data-mm-depth]').value='direct';root.querySelector('[data-mm-depth]').dispatchEvent(new Event('change',{bubbles:true}));await tick();
  assert('direct_impact',!root.querySelector('[data-mm-node="view"]').classList.contains('mm-affected')&&root.querySelector('[data-mm-node="usage"]').classList.contains('mm-affected'));
  root.querySelector('[data-mm-mode="work"]').click();await tick();
  assert('work_mode',root.querySelector('[data-mm-node="parser"]').classList.contains('mm-working'));
  const group=root.querySelector('[data-mm-group]');group.querySelector('summary').click();await tick();
  assert('group_collapses',!group.open);
  render();await tick();
  assert('collapse_survives_refresh',!root.querySelector('[data-mm-group]').open);
  root.querySelector('[data-mm-group] summary').click();await tick();
  root.querySelector('[data-mm-select="parser"]').focus();render();await tick();
  assert('focus_survives_refresh',document.activeElement?.dataset.mmSelect==='parser');
  root.querySelector('[data-mm-detail] [data-mm-select="usage"]').click();await tick();
  assert('relationship_focus',document.activeElement?.matches('[data-mm-detail] h2'));
  root.querySelector('[data-mm-close]').click();await tick();
  assert('close_returns_focus',document.activeElement?.dataset.mmSelect==='usage');
  const search=root.querySelector('[data-mm-search]');search.value='usage';search.dispatchEvent(new Event('input',{bubbles:true}));await tick();
  assert('search_filters',root.querySelectorAll('[data-mm-node]:not([hidden])').length===1);
  assert('history_control',root.querySelector('[data-mm-history]').options.length>1);
  assert('no_overflow',document.documentElement.scrollWidth<=window.innerWidth+1);
  const legacy={...DATA,historical:true,at:1,development:{tasks:DATA.development.tasks,operations:[],unavailable:true},nodes:DATA.nodes.map(n=>({...n,tasks:n.tasks.map(t=>({...t,status:'done'}))}))};
  root.innerHTML=renderMission(legacy);bindMission(root);await tick();
  assert('legacy_readiness_unavailable',root.textContent.includes('Readiness and proof counts were not captured'));
  assert('legacy_no_false_readiness',!root.textContent.includes('No task is currently ready'));
  assert('legacy_no_false_bad_proof',!root.textContent.includes('Recheck proof'));
  assert('actual_viewport',window.innerWidth===WIDTH);publish({ok:Object.values(checks).every(Boolean),checks});
} catch(error){publish({ok:false,checks,error:String(error),stack:error.stack});}
"""


@pytest.mark.parametrize('width', [1440, 768, 414, 375, 320])
def test_map_interactions_and_responsiveness(tmp_path, width):
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome: pytest.skip('Chrome is unavailable')
    data = fixture(tmp_path / 'project')
    page = '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' \
           '<link rel="stylesheet" href="/hub.css"><link rel="stylesheet" href="/theme.css"><link rel="stylesheet" href="/mission.css"><link rel="stylesheet" href="/roadmap.css"></head>' \
           '<body><main id="app" style="padding:16px;min-width:0"></main><pre id="results" style="white-space:pre-wrap;overflow-wrap:anywhere">pending</pre>' \
           '<script type="module">const WIDTH=' + str(width) + ';const DATA=' + json.dumps(data).replace('<', '\\u003c') + ';' + SCRIPT + '</script></body></html>'
    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/outer':
                self.send_response(200); self.end_headers(); self.wfile.write(('<!doctype html><html><head><meta charset="utf-8"></head><body><iframe src="/fixture" style="border:0;width:%dpx;height:1100px"></iframe><pre id="results">pending</pre></body></html>' % width).encode())
            elif self.path == '/fixture':
                self.send_response(200); self.send_header('Content-Type', 'text/html'); self.end_headers(); self.wfile.write(page.encode())
            else: super().do_GET()
        def log_message(self, *args): pass
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(WEB)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        run = subprocess.run([chrome, '--headless=new', '--no-sandbox', '--disable-gpu', '--no-first-run', '--password-store=basic',
                              '--user-data-dir=' + str(tmp_path / 'chrome'), '--window-size=%d,1100' % width,
                              '--virtual-time-budget=9000', '--dump-dom', 'http://127.0.0.1:%d/outer' % server.server_port],
                             capture_output=True, text=True, timeout=40)
        match = re.search(r'<pre id="results"[^>]*>(.*?)</pre>', run.stdout, re.S)
        assert match, run.stderr[-1500:]
        result = json.loads(html.unescape(match.group(1)))
        assert result['ok'], json.dumps(result, indent=2)
    finally:
        server.shutdown(); server.server_close()


def test_map_api_and_assets_use_real_hub_routes(project):
    from alpaca import serve
    from alpaca.tests.test_hub_live_refresh import _start, _fetch
    fixture(Path(project))
    live = serve.Live(project); live.refresh()
    server, url = _start(live, project)
    try:
        status, headers, body = _fetch(url, '/hub/mission.json')
        assert status == 200
        assert json.loads(body)['configured'] is True
        for asset in ('mission.js', 'mission.css'):
            assert _fetch(url, '/hub/assets/' + asset)[0] == 200
        assert _fetch(url, '/hub/mission.json?at=-1')[0] == 400
    finally:
        server.shutdown(); server.server_close()
