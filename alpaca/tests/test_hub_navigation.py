"""The real hub stays usable while supplemental reads are held or fail."""
import http.server
import json
import os
import shutil
import subprocess
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

from alpaca import db, serve
from alpaca.tests.test_hub_live_refresh import CHROME_FLAGS, HUB_DRIVER


DRIVER = HUB_DRIVER.split('let loads = 0;')[0] + r"""
try {
  await send('Runtime.enable');
  await send('Page.enable');
  await send('Page.addScriptToEvaluateOnNewDocument',{source:`window.__researchFetches=0;const originalFetch=window.fetch;window.fetch=function(url,...args){if(new URL(typeof url==='string'?url:url.url,location.href).pathname==='/hub/research.json')window.__researchFetches++;return originalFetch.call(this,url,...args);};`});
  await send('Page.navigate', {url: base+'/hub/#cockpit'});
  const core=await until(`!!document.querySelector('.cockpit-heading')`,2500);
  check('core renders before supplemental requests finish',core);
  if(!core)throw Error('Cockpit core is blocked by pending supplemental requests');
  check('pending research is loading, not absent or failed', await js(`!!document.querySelector('[data-cockpit-extra="research"] [role="status"]')`));
  await js(`window.__focusCard=document.querySelector('.focus-card');document.documentElement.style.minHeight='7000px';scrollTo(0,600);true`);
  await ctl('act=release');
  check('late research appears', await until(`document.querySelector('[data-cockpit-extra="research"]')?.textContent.includes('No research run is recorded')`));
  check('late content preserves core and scroll', await js(`window.__focusCard===document.querySelector('.focus-card')&&scrollY===600`),await js(`JSON.stringify({same:window.__focusCard===document.querySelector('.focus-card'),scroll:scrollY})`));

  await ctl('act=reset');
  await js('window.__oldNavigation=true');
  await send('Page.navigate', {url: base+'/hub/?second=1#cockpit'});
  await until(`!window.__oldNavigation&&!!document.querySelector('.cockpit-heading')`);
  await sleep(200);
  const researchBefore=await js('window.__researchFetches');
  check('panel read instrumentation is active',researchBefore===1,String(researchBefore));
  await js(`location.hash='#research';true`);await sleep(1200);
  check('research navigation reuses the pending panel read',await js('window.__researchFetches')===researchBefore,await js(`JSON.stringify({before:${researchBefore??'null'},after:window.__researchFetches,title:document.title,loading:!!document.querySelector('#main .loading')})`));
  await js(`location.hash='#roadmap';true`);
  check('navigation works while extras are pending', await until(`document.querySelector('#main h1')?.textContent==='Roadmap'`, 2500));
  await ctl('act=release');await sleep(500);
  check('late extras never overwrite the destination', await js(`document.querySelector('#main h1')?.textContent==='Roadmap'`));

  await ctl('act=fail');
  await js('window.__oldNavigation=true');
  await send('Page.navigate', {url: base+'/hub/?third=1#cockpit'});
  check('supplemental failure leaves core usable', await until(`!window.__oldNavigation&&!!document.querySelector('.cockpit-heading')&&!document.querySelector('#main .error-box')`,2500));
  check('failed research is disclosed',await until(`document.querySelector('[data-cockpit-extra="research"]')?.textContent.includes('could not be refreshed')`));

  const before=(await ctl('act=hold-overview')).overview;
  await js('window.__oldNavigation=true');
  await send('Page.navigate',{url:base+'/hub/?fourth=1#roadmap'});
  await until(`!window.__oldNavigation&&document.querySelector('#main')&&document.title.includes('Roadmap')`);
  await js(`location.hash='#sessions';true`);await sleep(100);
  await js(`location.hash='#research';true`);await sleep(100);
  await js(`location.hash='#roadmap';true`);await sleep(200);
  check('overlapping routes share one overview read',(await ctl('act=state')).overview-before===1);
  await ctl('act=release');
  check('latest navigation renders after shared read',await until(`document.querySelector('#main h1')?.textContent==='Roadmap'`,2500));
}catch(e){out.push('FAIL exception '+e.message);}
console.log(out.join('\n'));
ws.close();chrome.kill('SIGTERM');
if(out.some(row=>row.startsWith('FAIL')))process.exitCode=1;
"""


def test_core_navigation_survives_delayed_and_failed_supplemental_reads(project, tmp_path):
    chrome, node = shutil.which('google-chrome'), shutil.which('node')
    if not chrome or not node:
        pytest.skip('google-chrome and node are required')
    db.connect(project).close()
    live = serve.Live(project)
    live.refresh(fold=False)
    live.data_json = json.dumps({'sessions': []})
    live.data_ready.set()
    extras, overview = threading.Event(), threading.Event()
    overview.set()
    ctl = {'fail': False, 'overview': 0, 'research': 0}
    base = serve.make_handler(live, project)

    class Control(base):
        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == '/__ctl':
                act = parse_qs(parsed.query)['act'][0]
                if act == 'release':
                    extras.set()
                    overview.set()
                elif act == 'reset':
                    extras.clear()
                elif act == 'fail':
                    ctl['fail'] = True
                    extras.set()
                elif act == 'hold-overview':
                    overview.clear()
                return self._json({'overview': ctl['overview'], 'research': ctl['research']})
            if parsed.path == '/hub/research.json':
                ctl['research'] += 1
            if parsed.path == '/hub/overview.json':
                ctl['overview'] += 1
                overview.wait(12)
            if parsed.path in ('/hub/research.json', '/hub/mission.json', '/board/data.json', '/data.json'):
                extras.wait(12)
                if parsed.path == '/hub/research.json':
                    return self._json({'error': 'fixture outage'}, 503) if ctl['fail'] else self._json({'runs': []})
            return super().do_GET()

    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Control)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    driver = tmp_path / 'navigation.mjs'
    driver.write_text(DRIVER, encoding='utf-8')
    try:
        run = subprocess.run([node, str(driver), 'http://127.0.0.1:%d' % httpd.server_address[1], str(tmp_path / 'chrome')],
                             capture_output=True, text=True, timeout=90,
                             env=dict(os.environ, CHROME=chrome, CHROME_FLAGS=json.dumps(CHROME_FLAGS)))
    finally:
        extras.set()
        overview.set()
        httpd.shutdown()
        httpd.server_close()
    print(run.stdout)
    assert run.returncode == 0 and 'FAIL' not in run.stdout + run.stderr, run.stdout + run.stderr
    assert run.stdout.count('PASS') == 12, run.stdout
