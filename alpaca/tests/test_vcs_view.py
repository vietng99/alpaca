"""Version control history is navigable without hiding its ancestry."""
import http.server
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / 'web'


def fixture():
    commits = [dict(sha=f'{i:040x}', short=f'{i:08x}', parents=[f'{i+1:040x}'],
                    time=1700000000-i, author='Author', subject=f'Commit {i}',
                    refs=['main'] if i == 1 else [], tags=[], tasks=[], ops=[],
                    merge=False, lane=(i - 1) % 12,
                    edges=[dict(to=f'{i+1:040x}', lane=i % 12 if i < 90 else None, inside=i < 90)])
               for i in range(1, 91)]
    commits[4]['subject'] = 'Needle captured subject ' + 'long subject ' * 12
    commits[44]['author'] = 'Needle author'
    commits[4]['refs'] = ['feature/' + 'long-reference-' * 12]
    commits[4]['tags'] = ['v-long-' + 'tag-' * 20]
    commits[7]['merge'] = True
    commits[7]['parents'].append('f' * 40)
    commits[7]['edges'].append(dict(to='f' * 40, lane=None, inside=False))
    return dict(root='/repo', main='main', generated=1700000000,
                graph=dict(commits=commits, lanes=12, truncated=True, days=7),
                branches=[dict(name='main', head=commits[0]['sha'], main=True, merged=True, ahead=0, behind=0)],
                worktrees=[dict(path='/repo/worktree-' + str(i) + '-' + 'long-path-' * 8,
                                branch='feature/' + str(i), head=commits[i]['sha'],
                                head_short=commits[i]['short'], head_time=1700000000,
                                is_main=False, locked=None, prunable=None, ahead=i % 3,
                                behind=0, uncommitted=i % 5 if i < 40 else None, oldest_change=1700000000, session=None,
                                watch='recent' if i < 5 else 'largest' if i < 10 else None, loaded=i < 40,
                                checked_at=1700000000 if i < 40 else None)
                           for i in range(67)], uncommitted=[], activity=dict(stash=[], items=[]),
                remotes=dict(remotes=[], tracking=[], release=[], tags=[], fetch_head=None), denials=[])


def test_graph_first_with_accessible_details_and_honest_coverage(tmp_path):
    if not shutil.which('node'):
        pytest.skip('node unavailable')
    data = fixture()
    data['graph']['commits'][0]['subject'] = '<img src=x onerror=alert(1)> t-123'
    data['main'] = '<svg/onload=document.body.dataset.vcsxss=1>'
    data['uncommitted'] = [dict(session=None, files=[dict(code='??', path=f'file-{i}', touched=None, last_commit=None) for i in range(45)])]
    source = tmp_path / 'data.json'
    source.write_text(json.dumps(data))
    run = subprocess.run(['node', '--input-type=module', '-e', """
import fs from 'node:fs';
import {pathToFileURL} from 'node:url';
const {renderVcs}=await import(pathToFileURL(process.argv[1]).href);   // vcs.js imports ./orb.js
console.log(renderVcs(JSON.parse(fs.readFileSync(process.argv[2])),{href:()=> '#sessions',windowHref:d=>'#vcs?days='+d}));
""", str(WEB / 'vcs.js'), str(source)], capture_output=True, text=True, check=True)
    page = run.stdout
    assert page.index('id="vc-history"') < page.index('id="vc-worktrees"')
    assert 'Expand all loaded commits' in page
    assert 'More commits exist' in page
    assert 'data-vc-commit=' in page and 'data-vc-search' in page
    assert 'file-44' in page
    assert '<img src=x' not in page and '&lt;img' in page
    assert '<svg/onload=' not in page and 'Main checkout: &lt;svg/onload=' in page


# Reuse the existing hermetic CDP connection, rather than a DOM emulation. These
# checks observe geometry and reader behavior after the shared hub styles cascade.
VCS_DRIVER = r"""
const out = [];
const check = (name, ok, info = '') => out.push((ok ? 'PASS ' : 'FAIL ') + name + (info ? ' [' + info + ']' : ''));
const key = async value => {
  await send('Input.dispatchKeyEvent', {type:'keyDown',key:value,code:value,text:value==='Enter'?'\r':' ',windowsVirtualKeyCode:value==='Enter'?13:32});
  await send('Input.dispatchKeyEvent', {type:'keyUp',key:value,code:value,windowsVirtualKeyCode:value==='Enter'?13:32});
  await sleep(40);
};
try {
  await send('Runtime.enable');
  await send('Page.navigate', {url:base+'/fixture.html'});
  if (!await until('window.ready===true')) throw Error('VCS fixture failed to render');
  check('graph precedes worktree inventory', await js(`document.querySelector('.vc-graph').compareDocumentPosition([...document.querySelectorAll('h2')].find(e=>e.textContent.includes('Worktree'))) & Node.DOCUMENT_POSITION_FOLLOWING`));
  check('repository branch text is literal and cannot execute', await js(`document.querySelector('#vc-changes>summary').textContent.includes(window.data.main)&&!document.querySelector('#vc-changes>summary svg')&&!document.body.dataset.vcsxss`));
  check('graph viewport accepts keyboard focus', await js(`document.querySelector('.vc-graph').tabIndex>=0`));
  check('all commit rows and nodes align at fixed pitch', await js(`(()=>{const rows=[...document.querySelectorAll('.vc-commits>li')],dots=[...document.querySelectorAll('.vc-lanes circle')];return rows.length===90&&dots.length===90&&rows.every((r,i)=>{const a=r.getBoundingClientRect(),b=dots[i].getBoundingClientRect();return Math.abs(a.height-40)<.1&&Math.abs(a.top+a.height/2-b.top-b.height/2)<1})})()`));
  check('every missing parent has a local continuation marker', await js(`(()=>{const e=[...document.querySelectorAll('[data-vc-boundary]')];return e.length===2&&e.every(x=>x.getBBox().height<=40)})()`));
  await js(`document.querySelector('[data-vc-jump="oldest"]').click();true`);
  check('oldest commit is reachable inside compact viewport', await js(`(()=>{const g=document.querySelector('.vc-graph').getBoundingClientRect(),r=document.querySelector('.vc-commits>li:last-child').getBoundingClientRect();return r.top>=g.top-1&&r.bottom<=g.bottom+1})()`));
  await js(`document.querySelector('[data-vc-jump="newest"]').click();document.querySelector('[data-vc-commit="'+window.data.graph.commits[4].sha+'"]').focus();true`);
  await key('Enter');
  check('keyboard commit selection exposes captured fields', await js(`(()=>{const t=document.querySelector('[data-vc-inspector]').textContent,c=window.data.graph.commits[4];return [c.sha,c.subject,c.author,c.refs[0],c.tags[0]].every(v=>t.includes(v))&&document.querySelector('[data-vc-inspector] [data-vc-commit]').dataset.vcCommit===c.parents[0]&&/captured/i.test(t)})()`));
  await js(`window.topology=document.querySelector('.vc-lanes').innerHTML;const s=document.querySelector('[data-vc-search]');s.value='Needle';s.dispatchEvent(new Event('input',{bubbles:true}));true`);
  check('search highlights two matches without removing ancestry', await js(`document.querySelectorAll('.vc-commits>li').length===90&&document.querySelector('.vc-lanes').innerHTML===window.topology&&/2/.test(document.querySelector('[data-vc-matches]').textContent)`));
  await js(`document.querySelector('[data-vc-next]').click();window.firstMatch=document.querySelector('.vc-graph').scrollTop;document.querySelector('[data-vc-next]').click();true`);
  check('next match advances reading position', await js(`document.querySelector('.vc-graph').scrollTop!==window.firstMatch`));
  // Preserve an identity and its offset through insertion, independently of the state object's shape.
  await js(`window.selected=window.data.graph.commits[4].sha;document.querySelector('[data-vc-commit="'+window.selected+'"]').click();const g=document.querySelector('.vc-graph');g.scrollTop=1223;const rs=[...document.querySelectorAll('.vc-commits>li')];window.anchor=rs.find(r=>r.getBoundingClientRect().bottom>g.getBoundingClientRect().top);window.anchorKey=window.anchor.dataset.key;window.anchorOffset=window.anchor.getBoundingClientRect().top-g.getBoundingClientRect().top;const ds=[...document.querySelectorAll('details[data-key]')];if(ds.length)ds[0].open=true;window.disclosureKey=ds[0]?.dataset.key;document.querySelector('[data-vc-search]').focus();window.refresh(true);true`);
  await sleep(100);
  check('refresh preserves selected commit search focus and disclosure', await js(`document.querySelector('[data-vc-search]').value==='Needle'&&document.activeElement.matches('[data-vc-search]')&&document.querySelector('[data-vc-inspector]').textContent.includes(window.selected)&&!!window.disclosureKey&&[...document.querySelectorAll('details[data-key]')].find(d=>d.dataset.key===window.disclosureKey)?.open`));
  check('new first row preserves the visible commit offset', await js(`(()=>{const g=document.querySelector('.vc-graph'),r=[...document.querySelectorAll('.vc-commits>li')].find(r=>r.dataset.key===window.anchorKey);return document.querySelectorAll('.vc-commits>li').length===91&&Math.abs(r.getBoundingClientRect().top-g.getBoundingClientRect().top-window.anchorOffset)<2})()`));
  await js(`document.querySelector('[data-vc-expand]').focus();true`);
  await key('Enter');
  check('expansion exposes every loaded commit using page scroll', await js(`(()=>{const g=document.querySelector('.vc-graph');return g.clientHeight>=91*40&&g.scrollHeight<=g.clientHeight+2})()`),await js(`(()=>{const g=document.querySelector('.vc-graph');return [g.className,g.clientHeight,g.scrollHeight,g.getBoundingClientRect().height].join(',')})()`));
  await js(`window.refresh(false);true`);
  await sleep(100);
  check('refresh keeps repository branch text inert', await js(`document.querySelector('#vc-changes>summary').textContent.includes(window.data.main)&&!document.querySelector('#vc-changes>summary svg')&&!document.body.dataset.vcsxss`));
  check('expansion survives refresh', await js(`document.querySelector('.vc-graph').clientHeight>=91*40`),await js(`document.querySelector('.vc-graph').className+' '+document.querySelector('.vc-graph').clientHeight`));
  await js(`document.querySelector('[data-vc-jump="oldest"]').click();true`);
  await sleep(100);
  check('expanded oldest commit is reachable by page scroll', await js(`(()=>{const r=document.querySelector('.vc-commits>li:last-child').getBoundingClientRect();return r.top>=-1&&r.bottom<=innerHeight+1})()`));
  for (const width of [320,390,768,1440]) {
    await send('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false});
    for (const theme of ['light','dark']) {
      await js(`document.documentElement.dataset.theme=${JSON.stringify(theme)};document.querySelectorAll('details').forEach(d=>d.open=true);true`);
      await sleep(50);
      check(`no page overflow ${width}px ${theme}`, await js(`document.documentElement.scrollWidth<=innerWidth+1`), await js(`document.documentElement.scrollWidth+' / '+innerWidth`));
      check(`row centers stay aligned ${width}px ${theme}`, await js(`(()=>{const rows=[...document.querySelectorAll('.vc-commits>li')],dots=[...document.querySelectorAll('.vc-lanes circle')];return rows.every((r,i)=>{const a=r.getBoundingClientRect(),b=dots[i].getBoundingClientRect();return Math.abs(a.height-40)<.1&&Math.abs(a.top+a.height/2-b.top-b.height/2)<1})})()`));
    }
  }
  check('captured hostile and long filenames remain readable', await js(`(()=>{const p=window.data.uncommitted[0].files[44].path;const f=[...document.querySelectorAll('.vc-files code')].find(e=>e.textContent===p);return !!f&&f.getBoundingClientRect().width>0&&f.scrollWidth<=f.clientWidth+1&&!document.querySelector('img[src=x]')})()`));
} finally {
  console.log(out.join('\n'));
  ws.close();
  chrome.kill();
}
if(out.some(x=>x.startsWith('FAIL'))) process.exitCode=1;
"""


def test_vcs_browser_geometry_navigation_and_refresh(tmp_path):
    from alpaca.tests.test_hub_live_refresh import CHROME_FLAGS, HUB_DRIVER

    node = shutil.which('node')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not node or not chrome:
        pytest.skip('node and Chrome required for browser geometry')
    data = fixture()
    data['main'] = '<svg/onload=document.body.dataset.vcsxss=1>'
    data['uncommitted'] = [dict(session=None, files=[dict(code='??',
        path=f'directory-{i}/' + 'long-filename-' * 14 + '<img src=x onerror=alert(1)>.txt',
        touched=None, last_commit=None) for i in range(45)])]
    page = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/hub.css"><link rel="stylesheet" href="/vcs.css"><link rel="stylesheet" href="/theme.css">
</head><body><main id="fixture" style="padding:16px;min-width:0"></main><script type="module">
import {renderVcs,bindVcs,captureVcs} from '/vcs.js';
import {patch} from '/morph.js';
window.data=DATA;
const root=document.querySelector('#fixture'),opts={href:()=> '#sessions',windowHref:d=>'#vcs?days='+d};
root.innerHTML=renderVcs(window.data,opts);bindVcs(root,window.data);
window.refresh=insert=>{const view=captureVcs(root);if(insert){const first=structuredClone(window.data.graph.commits[0]);first.sha='a'.repeat(40);first.short='aaaaaaaa';first.subject='New first row';first.parents=[window.data.graph.commits[0].sha];first.edges=[{to:first.parents[0],lane:window.data.graph.commits[0].lane,inside:true}];window.data.graph.commits.unshift(first);}patch(root,renderVcs(window.data,{...opts,view}));bindVcs(root,window.data,view);};
window.ready=true;</script></body></html>""".replace('DATA', json.dumps(data))

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split('?')[0]
            if path == '/fixture.html':
                body, mime = page.encode(), 'text/html'
            elif path.lstrip('/') in {'hub.css', 'vcs.css', 'theme.css', 'vcs.js', 'morph.js', 'orb.js',
                                      'vendor/thinking-orbs.js'}:
                body = (WEB / path.lstrip('/')).read_bytes()
                mime = 'text/javascript' if path.endswith('.js') else 'text/css'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    driver = tmp_path / 'vcs_driver.mjs'
    driver.write_text(HUB_DRIVER.split('const ctl =')[0] + VCS_DRIVER)
    try:
        run = subprocess.run([node, str(driver), f'http://127.0.0.1:{httpd.server_address[1]}', str(tmp_path / 'chrome')],
            capture_output=True, text=True, timeout=120,
            env=dict(os.environ, CHROME=chrome, CHROME_FLAGS=json.dumps(CHROME_FLAGS)))
    finally:
        httpd.shutdown()
        httpd.server_close()
    report = run.stdout + run.stderr
    assert run.returncode == 0 and 'FAIL ' not in report, report
    assert report.count('PASS ') >= 30, report


HUB_VCS_DRIVER = r"""
const out=[];
const check=(name,ok)=>out.push((ok?'PASS ':'FAIL ')+name);
const enter=async()=>{
  await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',text:'\r',windowsVirtualKeyCode:13});
  await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
};
try {
  await send('Runtime.enable');
  await send('Page.navigate',{url:base+'/hub/#vcs'});
  if(!await until(`!!document.querySelector('#vcs-board [data-vc-expand]')`))throw Error('actual hub VCS did not mount');
  await js(`window.board=document.querySelector('#vcs-board');window.sha=document.querySelectorAll('.vc-commits>li')[4].dataset.key;document.querySelector('[data-vc-commit="'+window.sha+'"]').focus();true`);
  await enter();
  check('actual hub binds native commit selection',await js(`document.querySelector('[data-vc-inspector]').textContent.includes(window.sha)`));
  await js(`const s=document.querySelector('[data-vc-search]');s.value='Needle';s.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('[data-vc-expand]').focus();true`);
  await enter();
  check('actual hub binds search and expansion',await js(`document.querySelector('[data-vc-matches]').textContent.includes('2')&&document.querySelector('.vc-graph').classList.contains('expanded')`));
  // The 5 s status poll (t-592) patches one cell in place: same node, same board, scroll kept.
  await js(`window.cell=document.querySelectorAll('[data-wt-status]')[1];window.cellBefore=window.cell.textContent;window.scrollTo(0,200);window.y=window.scrollY;true`);
  check('status cell starts from the snapshot count',await js(`!window.cellBefore.includes('77')&&window.cellBefore.includes('checked')`));
  check('status poll updates the cell without a re-render',await until(`window.cell.textContent.includes('77')&&window.cell.isConnected&&window.cell===document.querySelectorAll('[data-wt-status]')[1]&&window.board===document.querySelector('#vcs-board')`,9000));
  check('status poll keeps the scroll position',await js(`window.scrollY===window.y`));
  // A worktree outside the live set loads on demand: its cell fills from /hub/vcs-tree.json.
  await js(`window.loadBtn=document.querySelectorAll('[data-wt-load]')[40];window.loadPath=window.loadBtn.dataset.wtLoad;window.loadBtn.click();true`);
  check('load status fills an on-demand row',await until(`[...document.querySelectorAll('[data-wt-status]')].find(c=>c.dataset.wtStatus===window.loadPath).textContent.includes('55')`,5000));
  // The hub deliberately postpones timed updates while a control in main has
  // focus. Blur after setting the state, then let its real 15-second clock run.
  await js(`document.activeElement.blur();window.started=performance.now();true`);
  check('real automatic refresh inserts new snapshot commit',await until(`!!document.querySelector('.vc-commits>li[data-key="${'b'.repeat(40)}"]')`,22000));
  check('automatic refresh uses the same mounted board',await js(`window.board===document.querySelector('#vcs-board')`));
  check('actual hub preserves selected commit query and expansion',await js(`document.querySelector('[data-vc-inspector]').textContent.includes(window.sha)&&document.querySelector('[data-vc-search]').value==='Needle'&&document.querySelector('.vc-graph').classList.contains('expanded')&&document.querySelectorAll('.vc-commits>li').length===91`));
  check('the timed re-render keeps the newer status count',await js(`document.querySelectorAll('[data-wt-status]')[1].textContent.includes('77')`));
  check('a row loaded on demand keeps its count through the re-render',await js(`[...document.querySelectorAll('[data-wt-status]')].find(c=>c.dataset.wtStatus===window.loadPath).textContent.includes('55')`));
} finally {
  console.log(out.join('\n'));ws.close();chrome.kill();
}
if(out.some(x=>x.startsWith('FAIL')))process.exitCode=1;
"""


def test_actual_hub_vcs_preserves_reader_state_on_timed_refresh(project, tmp_path, monkeypatch):
    import copy
    from alpaca import db, serve
    from alpaca.tests.test_hub_live_refresh import CHROME_FLAGS, HUB_DRIVER, _join, _start

    node = shutil.which('node')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not node or not chrome:
        pytest.skip('node and Chrome required for actual hub integration')
    conn = db.connect(project)
    live = serve.Live(project)
    live.refresh()
    _join(live)
    calls = []

    def snapshot(days):
        calls.append(days)
        data = fixture()
        if len(calls) > 1:
            first = copy.deepcopy(data['graph']['commits'][0])
            first.update(sha='b' * 40, short='bbbbbbbb', subject='Arrived during automatic refresh',
                         parents=[data['graph']['commits'][0]['sha']],
                         edges=[dict(to=data['graph']['commits'][0]['sha'], lane=0, inside=True)])
            data['graph']['commits'].insert(0, first)
        return data

    from alpaca import gitview
    path = fixture()['worktrees'][1]['path']
    monkeypatch.setattr(live, 'vcs', snapshot)
    monkeypatch.setattr(live, 'vcs_status', lambda: {'generated': time.time(), 'hot_backlog': 0,
        'worktrees': {path: dict(uncommitted=77, oldest_change=1700000000, checked_at=time.time())}})
    other = fixture()['worktrees'][50]['path']
    loaded = []

    def vcs_tree(asked):
        loaded.append(asked)
        return dict(path=asked, uncommitted=55, oldest_change=1700000000, checked_at=time.time(), status_error=None,
                    watch='opened', loaded=True, generated=time.time()) if asked == other else None

    monkeypatch.setattr(live, 'vcs_tree', vcs_tree)
    httpd, base = _start(live, project)
    driver = tmp_path / 'actual_hub_vcs.mjs'
    driver.write_text(HUB_DRIVER.split('const ctl =')[0] + HUB_VCS_DRIVER)
    try:
        run = subprocess.run([node, str(driver), base, str(tmp_path / 'chrome')],
            capture_output=True, text=True, timeout=70,
            env=dict(os.environ, CHROME=chrome, CHROME_FLAGS=json.dumps(CHROME_FLAGS)))
    finally:
        httpd.shutdown()
        httpd.server_close()
        conn.close()
    report = run.stdout + run.stderr
    print(report)
    assert run.returncode == 0 and 'FAIL ' not in report, report
    assert report.count('PASS ') == 11 and len(calls) >= 2 and loaded == [other], report


def test_status_cell_says_when_a_read_failed_and_the_backlog_note_is_said_not_hidden(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('node required')
    web = Path(__file__).resolve().parents[1] / 'web'
    driver = tmp_path / 'cell.mjs'
    driver.write_text(f"""import {{statusCell, backlogNote}} from '{web / "vcs.js"}';
console.log(JSON.stringify([
 statusCell({{uncommitted: 3, oldest_change: 100, checked_at: 900, status_error: 'GitError'}}, 1000),
 statusCell({{uncommitted: null, checked_at: null, status_error: 'GitError'}}, 1000),
 backlogNote(0), backlogNote(4)]));""")
    out = json.loads(subprocess.run([node, str(driver)], capture_output=True, text=True, check=True).stdout)
    assert 'status could not be read (GitError); last read 2 min ago' in out[0] and 'vc-dirty">3<' in out[0]
    assert 'never read' in out[1]
    assert out[2] == '' and '4 hot worktrees wait' in out[3]


def test_worktree_card_shows_the_overall_line_the_live_groups_and_the_rest_on_demand(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('node required')
    data = fixture()
    data['worktrees'][0].update(is_main=True, watch='main', path='/repo')
    data['overall'] = dict(worktrees=67, uncommitted=80, loaded=40, not_loaded=27, dirty=32, failed=0, watched=10, main='/repo')
    source = tmp_path / 'data.json'
    source.write_text(json.dumps(data))
    run = subprocess.run([node, '--input-type=module', '-e', """
import fs from 'node:fs';
import {pathToFileURL} from 'node:url';
const {renderVcs}=await import(pathToFileURL(process.argv[1]).href);
console.log(renderVcs(JSON.parse(fs.readFileSync(process.argv[2])),{href:()=> '#sessions',windowHref:d=>'#vcs?days='+d}));
""", str(WEB / 'vcs.js'), str(source)], capture_output=True, text=True, check=True)
    page = run.stdout
    card = page[page.index('<h2>Worktrees</h2>'):page.index('id="vc-changes"')]
    assert '67 worktrees' in card and '<strong>80</strong> uncommitted files across the 40 with a known status' in card
    assert '27 not loaded' in card and 'data-wt-overall' in card
    assert card.index('Main tree') < card.index('Most recent') < card.index('Largest') < card.index('Other worktrees (57)')
    live = card[:card.index('Other worktrees')]
    assert live.count('data-wt-status=') == 10 and 'data-wt-load' not in live    # main + 4 recent + 5 largest
    rest = card[card.index('Other worktrees'):]
    assert rest.count('data-wt-load=') == 57 and rest.count('data-wt-status=') == 57
    assert '<details class="vc-disclosure" data-key="other-worktrees">' in card            # closed by default
    assert rest.count('not loaded') == 27                                                 # never read: said, not 0
    hub = (WEB / 'hub.js').read_text(encoding='utf-8')
    assert "'/hub/vcs-tree.json?path='+encodeURIComponent(path)" in hub and 'loadTree:loadVcsTree' in hub
    for name in ('vcs.js', 'vcs.css', 'hub.js'):
        text = (WEB / name).read_bytes()
        assert text.isascii(), name
