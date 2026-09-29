"""Exercise the real roadmap module in a local headless browser."""
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

from alpaca import db, taskplan, work_record
from alpaca.tests.test_taskplan import plan

WEB = Path(__file__).resolve().parents[1] / 'web'


def browser_fixture(root):
    conn = db.connect(root)
    db.upsert(conn, 'ops', 'id', {'id': 'op-001', 'intent': 'Release roadmap', 'status': 'open'})
    titles = ['Prepare inputs', 'Build feature', 'Run checks', 'Write report', 'Write guide', 'Final review', '<img src=x onerror="window.injected=1">']
    for n, title in enumerate(titles, 1):
        db.upsert(conn, 'tasks', 'id', {'id': 't-%03d' % n, 'op': 'op-001', 'statement': title, 'title': title,
                  'status': 'doing' if n in (2, 5) else 'open',
                  'claimant': 'worker-%d' % n if n in (2, 5) else None,
                  'lease_until': '2999-01-01T00:00:00Z' if n in (2, 5) else None})
        if n in (2, 5):
            db.append_event(conn, session='chat-%d' % n, actor='worker', kind='claim', ref='t-%03d' % n,
                            data={'worker': 'worker-%d' % n})
    db.meta_set(conn, 'operator:chat-2', 'codex'); db.meta_set(conn, 'operator:chat-5', 'claude')
    data = plan(); data['title'] = 'Feature delivery'; data['groups'][0]['category'] = 'Features'; data['groups'][1]['category'] = 'Documentation'
    taskplan.record(conn, 'op-001', data, session='planner', actor='planner')
    db.upsert(conn, 'ops', 'id', {'id':'op-002','intent':'Second feature','status':'open'})
    db.upsert(conn, 'tasks', 'id', {'id':'t-008','op':'op-002','statement':'Parallel feature','title':'Parallel feature','status':'open'})
    taskplan.record(conn,'op-002',{'version':1,'revision':0,'title':'Second feature',
        'groups':[{'id':'build','title':'Implementation','category':'Features'}],
        'tasks':[{'id':'t-008','group':'build','after':[],'resources':[],'requires_stages':[],'hold':None}]},session='planner',actor='planner')
    tasks = work_record.tasks(conn)
    data = {'project': {'name': 'Demo'}, 'ops': [{'id': 'op-001', 'title': 'Release roadmap', 'status': 'open'}],
            'tasks': tasks, 'roadmap': taskplan.project(conn, tasks, root=root)}
    conn.close()
    return data


GEOMETRY_CHECK = "window.roadmapGeometry=()=>{\n const root=document.querySelector('.rm-panel'),svg=root.querySelector('.rm-connectors'),box=svg.getBoundingClientRect();\n const edges=[...svg.querySelectorAll('[data-rm-edge]')],cards=[...root.querySelectorAll('[data-rm-node]')].filter(e=>e.closest('[data-rm-group]').open);\n const get=key=>key.startsWith('t:')?cards.find(e=>e.dataset.rmNode===key.slice(2)):[...root.querySelectorAll('[data-rm-group]')].find(e=>e.dataset.rmGroup===key.slice(2))?.querySelector('.rm-group-port');\n const boundary=(point,rect)=>{\n  const x=point.x+box.left,y=point.y+box.top,epsilon=1;\n  return ((Math.abs(x-rect.left)<=epsilon||Math.abs(x-rect.right)<=epsilon)&&y>=rect.top-epsilon&&y<=rect.bottom+epsilon)||((Math.abs(y-rect.top)<=epsilon||Math.abs(y-rect.bottom)<=epsilon)&&x>=rect.left-epsilon&&x<=rect.right+epsilon);\n };\n const connected=edges.every(path=>{\n  const a=get(path.dataset.rmFrom),b=get(path.dataset.rmTo);if(!a||!b)return false;\n  return boundary(path.getPointAtLength(0),a.getBoundingClientRect())&&boundary(path.getPointAtLength(path.getTotalLength()),b.getBoundingClientRect());\n });\n const unobscured=edges.every(path=>{\n  for(let distance=2;distance<path.getTotalLength()-1;distance+=4){\n   const p=path.getPointAtLength(distance),x=p.x+box.left,y=p.y+box.top;\n   if([...cards,...root.querySelectorAll('[data-rm-group]>summary')].some(card=>{const r=card.getBoundingClientRect();return x>r.left+1&&x<r.right-1&&y>r.top+1&&y<r.bottom-1;}))return false;\n  }\n  return true;\n });\n return {connected,unobscured,aboveHeaders:Number(getComputedStyle(svg).zIndex)>Number(getComputedStyle(root.querySelector('[data-rm-group]>summary')).zIndex),edges:edges.length};\n};\n"


SCRIPT = r"""
const checks = {};
const assert = (name, test) => {checks[name] = Boolean(test);};
const tick = () => new Promise(resolve => setTimeout(resolve, 80));
try {
  const {renderRoadmap, bindRoadmap, rememberRoadmap, taskPlanDetail} = await import('/roadmap.js');
  const root = document.querySelector('#app');
  const render = options => {rememberRoadmap?.(root); root.innerHTML = renderRoadmap(DATA, options); bindRoadmap(root);};
  render({op:'op-001', mode:'map'});
  await tick();
  assert('all_tasks_visible', root.querySelectorAll('[data-rm-node]').length === 7);
  assert('readable_operator_label',root.querySelector('[data-rm-chat="chat-2"]')?.textContent.includes('Codex'));
  assert('readable_chat_choices', root.querySelector('[data-rm-chat="chat-5"]')?.textContent.includes('Write guide'));
  assert('groups_visible', root.textContent.includes('Implementation') && root.textContent.includes('Documentation'));
  assert('chat_links', [...root.querySelectorAll('a')].some(a=>a.hash.includes('sid=chat-2')));
  assert('escaped_title', !root.querySelector('img') && !window.injected && root.textContent.includes('<img'));
  assert('join_names_both_inputs', root.querySelector('[data-rm-node="t-006"]').textContent.includes('t-004') && root.querySelector('[data-rm-node="t-006"]').textContent.includes('t-005'));
  assert('recorded_edges_only', root.querySelectorAll('[data-rm-edge]').length === 5);
  assert('map_needs_no_horizontal_scroll',[...root.querySelectorAll('.rm-scroll,.rm-canvas,.rm-track')].every(e=>e.scrollWidth<=e.clientWidth+1));
  const cards=[...root.querySelectorAll('[data-rm-group="build"] [data-rm-node]')];
  assert('compact_task_height',cards.every(e=>e.getBoundingClientRect().height<=70));
  const edge=id=>getComputedStyle(root.querySelector(`[data-rm-node="${id}"]`)).borderLeftColor;
  const working=root.querySelector('.rm-node.rm-working'),notWorking=root.querySelector('.rm-node:not(.rm-working)');
  assert('status_color_per_task',working&&notWorking&&edge(working.dataset.rmNode)!==edge(notWorking.dataset.rmNode)&&getComputedStyle(working).borderLeftWidth==='4px');
  assert('card_names_claiming_chat',root.querySelector('[data-rm-node="t-002"] .rm-claim')?.textContent.includes('chat-2'));
  assert('claims_strip_lists_task',root.querySelector('.rm-claims [data-rm-chat="chat-2"]')?.textContent.includes('t-002'));
  root.querySelector('[data-rm-expand="true"]').click();await tick();
  assert('open_all',[...root.querySelectorAll('details[data-rm-group]')].every(g=>g.open)&&[...root.querySelectorAll('.rm-node-info')].every(e=>!e.hidden));
  root.querySelector('[data-rm-expand="false"]').click();await tick();
  assert('close_all',[...root.querySelectorAll('details[data-rm-group]')].every(g=>!g.open)&&[...root.querySelectorAll('.rm-node-info')].every(e=>e.hidden));
  root.querySelector('[data-rm-expand="true"]').click();await tick();
  for(const more of root.querySelectorAll('[data-rm-details][aria-expanded="true"]'))more.click();await tick();
  const sectors=[...root.querySelectorAll('[data-rm-group]')].map(e=>e.getBoundingClientRect());
  assert('desktop_sectors_share_rows',innerWidth<900||sectors.some((a,i)=>sectors.some((b,j)=>i!==j&&Math.abs(a.top-b.top)<2&&Math.abs(a.left-b.left)>100)));
  const toggle=root.querySelector('[data-rm-toggle]');
  assert('roadmap_has_collapse_control',!!toggle);
  if(toggle){
    toggle.click();await tick();
    assert('whole_map_collapses',root.querySelector('.rm-body').hidden&&root.querySelector('.rm-panel').getBoundingClientRect().height<=80);
    render({op:'op-001',mode:'map'});await tick();
    assert('whole_map_stays_collapsed_after_refresh',root.querySelector('.rm-body').hidden&&root.querySelector('[data-rm-toggle]').getAttribute('aria-expanded')==='false');
    root.querySelector('[data-rm-toggle]').click();await tick();
    assert('reopened_map_reconnects',Object.values(roadmapGeometry()).every(Boolean));
  }
  assert('series_wraps',(()=>{const current=[...root.querySelectorAll('[data-rm-group="build"] [data-rm-node]')];return current.at(-1).getBoundingClientRect().top>current[0].getBoundingClientRect().top})());
  assert('connectors_use_attached_arrowheads',[...root.querySelectorAll('[data-rm-edge]')].every(p=>p.hasAttribute('marker-end')&&(p.getAttribute('d').match(/M/g)||[]).length===1));
  assert('edges_connect_without_occlusion',Object.values(roadmapGeometry()).every(Boolean));
  root.querySelector('[data-rm-details="t-002"]').click();await tick();
  assert('details_expansion_reconnects',Object.values(roadmapGeometry()).every(Boolean));
  render({op:'op-001',mode:'map'});await tick();
  assert('details_survive_refresh',root.querySelector('[data-rm-details="t-002"]').getAttribute('aria-expanded')==='true');
  root.querySelector('[data-rm-details="t-002"]').click();await tick();
  const group = root.querySelector('[data-rm-group="build"]');
  group.querySelector('summary').click(); await tick();
  assert('collapse_operates', !group.open);
  assert('folded_edges_connect_without_occlusion',Object.values(roadmapGeometry()).every(Boolean));
  assert('collapsed_group_retains_external_arrow',root.querySelectorAll('[data-rm-edge]').length===1&&root.querySelector('[data-rm-edge="t-005:t-006"]')?.dataset.rmTo==='g:build');
  render({op:'op-001', mode:'map'}); await tick();
  assert('collapse_survives_refresh', !root.querySelector('[data-rm-group="build"]').open);
  root.querySelector('[data-rm-group="build"] summary').click(); await tick();
  root.querySelector('[data-rm-details="t-002"]').click();await tick();
  root.querySelector('[data-rm-focus="t-002"]').click(); await tick();
  assert('focus_marks_current', root.querySelector('[data-rm-node="t-002"]').classList.contains('rm-selected'));
  assert('focus_marks_predecessor', root.querySelector('[data-rm-node="t-001"]').classList.contains('rm-related'));
  assert('focus_marks_successor', root.querySelector('[data-rm-node="t-003"]').classList.contains('rm-related'));
  root.querySelector('[data-rm-focus="t-002"]').focus();
  root.querySelector('.rm-scroll').scrollLeft = 100;
  const left = root.querySelector('.rm-scroll').scrollLeft;
  render({op:'op-001', mode:'map'}); await tick();
  assert('keyboard_focus_survives_refresh', document.activeElement?.dataset.rmFocus === 't-002');
  assert('horizontal_position_survives_refresh', root.querySelector('.rm-scroll').scrollLeft === left);
  root.querySelector('[data-rm-chat="chat-5"]').click();
  assert('chat_focus_marks_assignment', root.querySelector('[data-rm-node="t-005"]').classList.contains('rm-selected'));
  render({op:'op-001', mode:'list'}); await tick();
  assert('checklist_has_same_nodes', root.querySelectorAll('[data-rm-node]').length === 7);
  assert('checklist_mode', !!root.querySelector('.rm-list'));
  assert('inspector_has_prerequisites', taskPlanDetail(DATA, 't-006').includes('t-004') && taskPlanDetail(DATA, 't-006').includes('t-005'));
  assert('viewport_has_no_page_overflow', document.documentElement.scrollWidth <= window.innerWidth + 1);
  render({op:'op-001', mode:'map'}); await tick();
  assert('map_has_no_page_overflow', document.documentElement.scrollWidth <= window.innerWidth + 1);
  render({op:'op-001', mode:'list', selector:false, visibleIds:['t-002']}); await tick();
  assert('filtered_view_has_one_node', root.querySelectorAll('[data-rm-node]').length === 1);
  assert('scoped_view_has_no_competing_selector', !root.querySelector('[data-rm-operation]'));
  assert('filtered_prerequisite_stays_named', root.querySelector('[data-rm-node]').textContent.includes('t-001'));
  assert('visible_flow_starts_at_first_column',root.querySelector('[data-rm-node]').style.gridColumn==='');
  render({all:true,mode:'map'}); await tick();
  assert('workspace_is_one_map',root.querySelectorAll('.rm-panel').length===1&&root.textContent.includes('All operations'));
  root.querySelector('[data-rm-category="Features"]').click(); await tick();
  assert('chat_choices_match_category',!root.querySelector('[data-rm-chat="chat-5"]'));
  assert('hidden_chat_focus_clears',!root.querySelector('.rm-dimmed'));
  assert('category_filters_workstreams',root.querySelectorAll('[data-rm-node]').length===6);
  assert('same_workstream_merges_across_operations',root.querySelectorAll('[data-rm-group]').length===1);
  root.querySelector('[data-rm-category=""]').click(); await tick();
  const search=root.querySelector('[data-rm-search]');search.value='no match';search.dispatchEvent(new Event('input'));
  assert('operation_search_filters',root.querySelector('[data-rm-operation="op-001"]').hidden);
  search.value='Feature';search.dispatchEvent(new Event('input'));
  assert('operation_search_uses_short_title',!root.querySelector('[data-rm-operation="op-001"]').hidden);
  DATA.tasks.find(t=>t.id==='t-001').status='done';
  DATA.roadmap.operations[0].groups[0].nodes[0].state='done';
  render({op:'op-001',mode:'map'});await tick();
  assert('active_hides_completed',!root.querySelector('[data-rm-node="t-001"]'));
  assert('completed_prerequisite_still_named',root.querySelector('[data-rm-node="t-002"]').textContent.includes('Prepare inputs'));
  assert('active_flow_has_no_empty_leading_column',root.querySelector('[data-rm-node="t-002"]').style.gridColumn==='');
  root.querySelector('[data-rm-completed="true"]').click();await tick();
  assert('full_plan_restores_completed',!!root.querySelector('[data-rm-node="t-001"]'));
  document.documentElement.dataset.theme='dark';await tick();
  const luminance=color=>{const rgb=color.match(/[0-9.]+/g).slice(0,3).map(v=>{v=Number(v)/255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;});return rgb[0]*.2126+rgb[1]*.7152+rgb[2]*.0722;};
  const card=root.querySelector('[data-rm-node="t-002"]'),bg=luminance(getComputedStyle(card).backgroundColor);
  assert('dark_text_contrast',[card.querySelector('.rm-title'),card.querySelector('.rm-state'),card.querySelector('.rm-context')].every(el=>{const fg=luminance(getComputedStyle(el).color);return (Math.max(fg,bg)+.05)/(Math.min(fg,bg)+.05)>=4.5;}));
  assert('touch_targets', [...root.querySelectorAll('button,summary,.rm-chat')].filter(el=>el.getBoundingClientRect().height).every(el=>el.getBoundingClientRect().height>=44));
  document.documentElement.dataset.theme='light';
  render({op:'op-001',mode:'map',visibleIds:[]});await tick();
  assert('empty_scope_keeps_navigation',root.querySelectorAll('[data-rm-node]').length===0&&!!root.querySelector('[data-rm-operation="op-001"]'));
  DATA.tasks.filter(t=>t.op==='op-001').forEach(t=>t.status='done');DATA.project.name='Completed demo';
  render({op:'op-001',mode:'map'});await tick();
  assert('completed_only_plan_accessible',root.querySelectorAll('[data-rm-node]').length===7&&!root.querySelector('[data-rm-group][open]'));
  assert('empty_project_message',renderRoadmap({tasks:[]}).includes('No tasks have been recorded'));
  document.querySelector('#results').textContent = JSON.stringify({ok:Object.values(checks).every(Boolean), checks});
} catch(error) {document.querySelector('#results').textContent = JSON.stringify({ok:false, checks, error:String(error)});}
"""


@pytest.mark.parametrize('width', [1440, 390])
def test_roadmap_interactions_and_refresh(tmp_path, width):
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('headless Chrome not available')
    root = tmp_path / 'fixture'; root.mkdir()
    data = browser_fixture(root)
    page = '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' \
           '<link rel="stylesheet" href="/hub.css"><link rel="stylesheet" href="/roadmap.css"><link rel="stylesheet" href="/theme.css"></head><body>' \
           '<main id="app" style="padding:20px;min-width:0;max-width:1100px"></main><pre id="results">pending</pre>' \
           '<script type="module">const DATA=' + json.dumps(data).replace('<', '\\u003c') + ';' + GEOMETRY_CHECK + SCRIPT + '</script></body></html>'
    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/fixture':
                body = page.encode(); self.send_response(200); self.send_header('Content-Type','text/html'); self.end_headers(); self.wfile.write(body)
            else:
                super().do_GET()
        def log_message(self, *args):
            pass
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(WEB)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = subprocess.run([chrome, '--headless=new', '--no-sandbox', '--disable-gpu', '--no-first-run',
            '--password-store=basic', '--no-default-browser-check', '--user-data-dir='+str(tmp_path/'chrome'),
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


def test_roadmap_assets_are_served_by_the_actual_hub(project):
    from alpaca import serve
    from alpaca.tests.test_hub_live_refresh import _start, _fetch
    db.connect(project).close()
    live = serve.Live(project); live.refresh()
    server, url = _start(live, project)
    try:
        for filename, content_type in [('roadmap.js', 'javascript'), ('roadmap-routing.js', 'javascript'), ('roadmap.css', 'text/css')]:
            status, headers, body = _fetch(url, '/hub/assets/' + filename)
            assert status == 200, filename
            assert content_type in headers['Content-Type']
            assert len(body) > 100
    finally:
        server.shutdown(); server.server_close()


def test_work_roadmap_filters_and_keyboard_focus_survive_live_refresh(project, tmp_path):
    import os
    from urllib.parse import urlsplit
    from alpaca import serve
    from alpaca.tests.test_hub_live_refresh import HUB_DRIVER, CHROME_FLAGS
    chrome, node = shutil.which('google-chrome'), shutil.which('node')
    if not chrome or not node:
        pytest.skip('headless Chrome and node are required')
    browser_fixture(project)
    live = serve.Live(project); live.refresh()
    control = {'sse': 0}
    base = serve.make_handler(live, project)

    class Handler(base):
        def _sse(self):
            control['sse'] += 1
            return super()._sse()

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == '/__ctl':
                if url.query == 'act=rename':
                    conn = db.connect(project)
                    task = db.rows(conn, 'tasks', 'id=?', ('t-002',))[0]
                    db.upsert(conn, 'tasks', 'id', {**task, 'title': 'Build feature updated'})
                    db.append_event(conn, session='planner', actor='planner', kind='task-edit', ref='t-002', data={})
                    conn.close(); live.refresh()
                return self._json(control)
            return super().do_GET()

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    driver = HUB_DRIVER.split("try {\n  await send('Runtime.enable');")[0] + r"""
try {
  await send('Runtime.enable');
  const geometrySource="window.roadmapGeometry=()=>{\n const root=document.querySelector('.rm-panel'),svg=root.querySelector('.rm-connectors'),box=svg.getBoundingClientRect();\n const edges=[...svg.querySelectorAll('[data-rm-edge]')],cards=[...root.querySelectorAll('[data-rm-node]')].filter(e=>e.closest('[data-rm-group]').open);\n const get=key=>key.startsWith('t:')?cards.find(e=>e.dataset.rmNode===key.slice(2)):[...root.querySelectorAll('[data-rm-group]')].find(e=>e.dataset.rmGroup===key.slice(2))?.querySelector('.rm-group-port');\n const boundary=(point,rect)=>{\n  const x=point.x+box.left,y=point.y+box.top,epsilon=1;\n  return ((Math.abs(x-rect.left)<=epsilon||Math.abs(x-rect.right)<=epsilon)&&y>=rect.top-epsilon&&y<=rect.bottom+epsilon)||((Math.abs(y-rect.top)<=epsilon||Math.abs(y-rect.bottom)<=epsilon)&&x>=rect.left-epsilon&&x<=rect.right+epsilon);\n };\n const connected=edges.every(path=>{\n  const a=get(path.dataset.rmFrom),b=get(path.dataset.rmTo);if(!a||!b)return false;\n  return boundary(path.getPointAtLength(0),a.getBoundingClientRect())&&boundary(path.getPointAtLength(path.getTotalLength()),b.getBoundingClientRect());\n });\n const unobscured=edges.every(path=>{\n  for(let distance=2;distance<path.getTotalLength()-1;distance+=4){\n   const p=path.getPointAtLength(distance),x=p.x+box.left,y=p.y+box.top;\n   if([...cards,...root.querySelectorAll('[data-rm-group]>summary')].some(card=>{const r=card.getBoundingClientRect();return x>r.left+1&&x<r.right-1&&y>r.top+1&&y<r.bottom-1;}))return false;\n  }\n  return true;\n });\n return {connected,unobscured,aboveHeaders:Number(getComputedStyle(svg).zIndex)>Number(getComputedStyle(root.querySelector('[data-rm-group]>summary')).zIndex),edges:edges.length};\n};\n";
  const hash='#work?op=op-001&layout=roadmap&status=doing&q=feature';
  await load(hash, `!!document.querySelector('[data-rm-node="t-002"]')`);
  check('Work filters restrict nodes', await js(`document.querySelectorAll('[data-rm-node]').length===1`));
  check('Work owns its operation filter', await js(`!document.querySelector('[data-filter="op"]')&&!!document.querySelector('[data-rm-operation="op-001"]')`));
  await js(`document.querySelector('[data-rm-details="t-002"]').click();document.querySelector('[data-rm-focus="t-002"]').focus();true`);
  await ctl('act=rename');
  check('live update arrived', await until(`document.querySelector('[data-rm-node="t-002"]')?.textContent.includes('Build feature updated')`));
  check('keyboard focus survives real Work refresh', await js(`document.activeElement?.dataset.rmFocus==='t-002'`));
  check('URL and filters survive refresh', await js(`location.hash===${JSON.stringify(hash)}&&document.querySelectorAll('[data-rm-node]').length===1`));
  await js(`document.querySelector('[data-rm-operation="op-002"]').click();true`);
  check('operation change preserves other URL filters',await until(`location.hash.includes('op=op-002')&&location.hash.includes('q=feature')&&location.hash.includes('status=doing')`));
  check('empty results retain operation navigation',await until(`!!document.querySelector('[data-rm-operation="op-001"]')&&document.querySelectorAll('[data-rm-node]').length===0`));
  await js(`document.querySelector('[data-rm-operation="op-001"]')?.click();true`);
  check('returning scope preserves filters and restores work',await until(`!!document.querySelector('[data-rm-node="t-002"]')&&location.hash.includes('q=feature')&&location.hash.includes('status=doing')`));
  check('hidden prerequisite remains readable', await js(`document.querySelector('[data-rm-node]').textContent.includes('t-001')`));
  {
    const {writeFileSync}=await import('node:fs');
    await load('#work?op=op-001&layout=roadmap', `document.querySelectorAll('[data-rm-node]').length===7`);
    await js(`document.querySelectorAll('[data-rm-details][aria-expanded="true"]').forEach(b=>b.click());document.querySelector('[data-rm-toggle]').click();true`);
    await send('Page.reload');
    check('whole roadmap remains closed after page reload',await until(`document.querySelector('.rm-body')?.hidden&&document.querySelector('[data-rm-toggle]')?.getAttribute('aria-expanded')==='false'`));
    await js(`document.querySelector('[data-rm-toggle]').click();true`);await sleep(100);
    await js(`document.documentElement.style.minHeight='';true`);await js(geometrySource);
    for(const [name,width,height] of [['desktop',1440,1000],['tablet',768,1024],['phone414',414,1000],['phone375',375,1000],['phone320',320,1000]]){
      await send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<600});
      await sleep(250);
      await js(`document.querySelector('.rm-panel').scrollIntoView();true`);
      check(name+' page fits viewport',await js('document.documentElement.scrollWidth<=innerWidth+1'));
      check(name+' roadmap needs no sideways pan',await js(`[...document.querySelectorAll('.rm-scroll,.rm-canvas,.rm-track')].every(e=>e.scrollWidth<=e.clientWidth+1)`));
      check(name+' continuous unobscured arrows',await js(`Object.values(roadmapGeometry()).every(Boolean)`));
      check(name+' all recorded arrows remain visible',await js(`document.querySelectorAll('[data-rm-edge]').length===5`));
      for(const kind of ['operation','chat','operation']){
        await js(`document.querySelector('[data-rm-picker="${kind}"]').open=true;true`);await sleep(50);
        check(name+' open '+kind+' menu fits viewport',await js(`(()=>{const r=document.querySelector('[data-rm-picker="${kind}"] .rm-picker-menu').getBoundingClientRect();return r.left>=0&&r.right<=innerWidth+1&&document.documentElement.scrollWidth<=innerWidth+1})()`));
        check(name+' only '+kind+' picker stays open',await js(`document.querySelectorAll('[data-rm-picker][open]').length===1&&document.querySelector('[data-rm-picker="${kind}"]').open`));
      }

      await js(`document.querySelectorAll('[data-rm-picker]').forEach(p=>p.open=false);true`);
      check(name+' collapsed title text uses at most two lines',await js(`[...document.querySelectorAll('.rm-title-text')].every(e=>e.getBoundingClientRect().height<=32)`));
      if(process.env.ALPACA_ROADMAP_SCREENSHOTS){const shot=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
      writeFileSync(process.env.ALPACA_ROADMAP_SCREENSHOTS+'/'+name+'.png',Buffer.from(shot.result.data,'base64'));}

    }
  }
} catch(e){out.push('FAIL exception '+e.message);}
console.log(out.join('\n'));ws.close();chrome.kill();
process.exit(out.some(line=>line.startsWith('FAIL'))?1:0);
"""
    path = tmp_path / 'roadmap_driver.mjs'; path.write_text(driver)
    try:
        run = subprocess.run([node, str(path), 'http://127.0.0.1:%d' % server.server_port, str(tmp_path/'chrome')],
            capture_output=True, text=True, timeout=100,
            env=dict(os.environ, CHROME=chrome, CHROME_FLAGS=json.dumps(CHROME_FLAGS)))
        assert run.returncode == 0, run.stdout + run.stderr
        assert run.stdout.count('PASS') >= 6, run.stdout
    finally:
        server.shutdown(); server.server_close()
