"""The pinned operation map: op pin and unpin, phase state in the overview, and the phase map
rendered by the real roadmap module in a headless browser."""
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

from alpaca import cli, db, hub, taskplan, work_record

WEB = Path(__file__).resolve().parents[1] / 'web'


def _op(project, phases='requirement,design,build,release'):
    cli.main(['init'])
    assert cli.main(['op', 'new', 'Pinned work', '--phases', phases]) == 0
    return db.connect(project)


def _overview_op(project, op='op-001'):
    return next(o for o in hub.overview(Path(project))['ops'] if o['id'] == op)


def test_op_pin_and_unpin_are_recorded_and_the_newest_wins(project, capsys):
    root = Path.cwd().parent
    _op(root)
    assert _overview_op(root)['pinned'] is False
    assert cli.main(['op', 'pin', 'op-001']) == 0
    assert _overview_op(root)['pinned'] is True
    assert cli.main(['op', 'unpin', 'op-001']) == 0
    assert _overview_op(root)['pinned'] is False
    assert cli.main(['op', 'pin', 'op-001']) == 0
    assert _overview_op(root)['pinned'] is True
    assert cli.main(['op', 'pin', 'op-404']) == 1
    assert 'no op op-404' in capsys.readouterr().out


def test_phase_state_follows_entry_advance_and_door_refusals(project):
    root = Path.cwd().parent
    conn = _op(root)
    state = _overview_op(root)['phase_state']
    assert _overview_op(root)['phases'] == ['requirement', 'design', 'build', 'release']
    assert state == {'opened': [], 'completed': [], 'current': None, 'door': None}
    db.append_event(conn, session='s', actor='t', kind='phase-enter', op='op-001', ref='requirement',
                    data={'phase': 'requirement'})
    db.append_event(conn, session='s', actor='t', kind='phase-door-closed', op='op-001', ref='requirement->design',
                    data={'boundary': 'requirement->design', 'verdict': 'BLOCKED', 'reason': 'rows open'})
    state = _overview_op(root)['phase_state']
    assert state['current'] == 'requirement' and state['completed'] == []
    assert state['door']['verdict'] == 'BLOCKED' and state['door']['boundary'] == 'requirement->design'
    db.append_event(conn, session='s', actor='t', kind='phase-advance', op='op-001', ref='requirement->design',
                    data={'boundary': 'requirement->design'})
    state = _overview_op(root)['phase_state']
    assert state['current'] == 'design' and state['completed'] == ['requirement']
    assert state['opened'] == ['requirement', 'design'] and state['door'] is None


def browser_fixture(root):
    conn = db.connect(root)
    db.upsert(conn, 'ops', 'id', {'id': 'op-001', 'intent': 'Pinned work', 'status': 'open',
                                  'phases': json.dumps(['requirement', 'design', 'build', 'release'])})
    rows = [('t-001', 'Study one', 'requirement', 'doing'), ('t-002', 'Study two', 'requirement', 'open'),
            ('t-003', 'Build it', 'build', 'open'), ('t-004', 'Ship decision', 'release', 'open')]
    for ident, title, phase, status in rows:
        db.upsert(conn, 'tasks', 'id', {'id': ident, 'op': 'op-001', 'statement': title, 'title': title,
                                        'phase': phase, 'status': status,
                                        'claimant': 'worker' if status == 'doing' else None,
                                        'lease_until': '2999-01-01T00:00:00Z' if status == 'doing' else None})
    db.append_event(conn, session='chat-1', actor='worker', kind='claim', ref='t-001', data={'worker': 'worker'})
    db.append_event(conn, session='s', actor='t', kind='phase-enter', op='op-001', ref='requirement',
                    data={'phase': 'requirement'})
    taskplan.record(conn, 'op-001', {
        'version': 1, 'revision': 0, 'title': 'Pinned work',
        'groups': [{'id': 'study', 'title': 'Study', 'category': 'Features'},
                   {'id': 'ship', 'title': 'Shipping', 'category': 'CI/CD'}],
        'tasks': [{'id': 't-001', 'group': 'study', 'after': [], 'resources': [], 'requires_stages': [], 'hold': None},
                  {'id': 't-002', 'group': 'study', 'after': [], 'resources': [], 'requires_stages': [], 'hold': None},
                  {'id': 't-003', 'group': 'study', 'after': ['t-001', 't-002'], 'resources': [], 'requires_stages': [], 'hold': None},
                  {'id': 't-004', 'group': 'ship', 'after': ['t-003'], 'resources': [], 'requires_stages': [],
                   'hold': {'kind': 'approval', 'reason': 'Owner decides shipping'}}]},
        session='planner', actor='planner')
    tasks = work_record.tasks(conn)
    ops = [{'id': 'op-001', 'title': 'Pinned work', 'status': 'open', 'pinned': True, 'done_when': 'Shipped',
            'phases': ['requirement', 'design', 'build', 'release'],
            'phase_state': {'opened': ['requirement'], 'completed': [], 'current': 'requirement', 'door': None}}]
    data = {'project': {'name': 'Demo'}, 'ops': ops, 'tasks': tasks, 'roadmap': taskplan.project(conn, tasks, root=root)}
    conn.close()
    return data


SCRIPT = r"""
const checks = {};
const assert = (name, test) => {checks[name] = Boolean(test);};
const tick = () => new Promise(resolve => setTimeout(resolve, 80));
try {
  const {renderPhaseMap, renderRoadmap, bindRoadmap, rememberRoadmap} = await import('/roadmap.js');
  const map = document.querySelector('#map'), plain = document.querySelector('#plain');
  const render = () => {
    rememberRoadmap(document); map.innerHTML = renderPhaseMap(DATA, 'op-001'); plain.innerHTML = renderRoadmap(DATA, {op: 'op-001', mode: 'map'});
    bindRoadmap(document);
  };
  render(); await tick();
  const strip = () => [...map.querySelectorAll('.rm-phase')].map(li => li.textContent.replace(/\s+/g, ' ').trim());
  assert('strip_lists_every_declared_phase_in_order', [...map.querySelectorAll('.rm-phase strong')].map(e => e.textContent).join(',') === 'Requirement,Design,Build,Release');
  assert('sectors_are_phases_with_work_in_order', [...map.querySelectorAll('[data-rm-group] summary strong')].map(e => e.textContent).join(',') === 'Requirement,Build,Release');
  assert('current_phase_marked', map.querySelector('.rm-phase-current')?.textContent.includes('Requirement'));
  assert('empty_phase_counts_zero', strip()[1].includes('0 of 0 done'));
  assert('workstreams_label_tasks_inside_a_phase', [...map.querySelectorAll('.rm-sub-title')].map(e => e.textContent).includes('Study'));
  assert('every_task_on_the_map', map.querySelectorAll('[data-rm-node]').length === 4);
  assert('owner_wait_listed', map.querySelector('.rm-owner-wait')?.textContent.includes('t-004'));
  assert('in_progress_shown', map.querySelector('[data-rm-node="t-001"]').classList.contains('rm-working'));
  assert('prerequisite_arrows_drawn', map.querySelectorAll('.rm-connectors path').length >= 3);
  assert('no_competing_operation_picker', !map.querySelector('[data-rm-operation]'));
  // A finished task: the record projection now reports it done, and the next refresh shows it.
  DATA.tasks.find(t => t.id === 't-001').status = 'done';
  DATA.roadmap.operations[0].groups[0].nodes.find(n => n.id === 't-001').state = 'done';
  render(); await tick();
  assert('finished_task_stays_on_map_as_done', map.querySelector('[data-rm-node="t-001"]')?.classList.contains('rm-done'));
  assert('phase_strip_counts_the_finish', strip()[0].includes('1 of 2 done'));
  assert('sector_count_updates', map.querySelector('[data-rm-group] .rm-group-count').textContent.startsWith('1/2'));
  // Folding the operation map leaves the workspace roadmap open.
  map.querySelector('[data-rm-toggle]').click(); await tick();
  assert('map_folds', map.querySelector('.rm-body').hidden);
  assert('roadmap_unaffected', !plain.querySelector('.rm-body').hidden);
  render(); await tick();
  assert('fold_survives_refresh', map.querySelector('.rm-body').hidden && !plain.querySelector('.rm-body').hidden);
  document.querySelector('#results').textContent = JSON.stringify({ok: Object.values(checks).every(Boolean), checks});
} catch (error) {document.querySelector('#results').textContent = JSON.stringify({ok: false, checks, error: String(error)});}
"""


@pytest.mark.parametrize('width', [1440, 390])
def test_operation_map_updates_when_a_task_finishes(tmp_path, width):
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('headless Chrome not available')
    root = tmp_path / 'fixture'; root.mkdir()
    data = browser_fixture(root)
    page = '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' \
           '<link rel="stylesheet" href="/hub.css"><link rel="stylesheet" href="/roadmap.css"><link rel="stylesheet" href="/theme.css"></head><body>' \
           '<main style="padding:20px;min-width:0;max-width:1400px"><div id="map"></div><div id="plain"></div></main><pre id="results">pending</pre>' \
           '<script type="module">const DATA=' + json.dumps(data).replace('<', '\\u003c') + ';' + SCRIPT + '</script></body></html>'

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/fixture':
                body = page.encode(); self.send_response(200); self.send_header('Content-Type', 'text/html'); self.end_headers(); self.wfile.write(body)
            else:
                super().do_GET()

        def log_message(self, *args):
            pass
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(WEB)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = subprocess.run([chrome, '--headless=new', '--no-sandbox', '--disable-gpu', '--no-first-run',
                                 '--password-store=basic', '--no-default-browser-check', '--user-data-dir=' + str(tmp_path / 'chrome'),
                                 '--window-size=%d,1000' % width, '--virtual-time-budget=12000', '--dump-dom',
                                 'http://127.0.0.1:%d/fixture' % server.server_port], text=True, capture_output=True, timeout=45)
        match = re.search(r'<pre id="results">(.*?)</pre>', result.stdout, re.S)
        assert match, result.stderr[-1000:]
        observed = html.unescape(match.group(1))
        assert observed != 'pending', result.stderr[-1000:]
        outcome = json.loads(observed)
        assert outcome['ok'], json.dumps(outcome, indent=2)
    finally:
        server.shutdown(); server.server_close()
