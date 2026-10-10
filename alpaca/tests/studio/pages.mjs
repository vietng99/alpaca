// Visit Studio pages in headless Chrome and report what each one shows (t-479).
//
//   node alpaca/tests/studio/pages.mjs --base http://127.0.0.1:PORT --views '[["cockpit","/hub/#cockpit"],...]'
//        --profile DIR [--auth FILE] [--shots DIR]
//
// For each view: the page is opened fresh, then read once it has settled (its heading is drawn, no
// loader is left in the main area and no request has been in flight for 600 ms, or 30 s passed; a
// view may give its own settled expression as a third item). It prints one JSON line per view: the
// heading, the empty-state texts, an error view's text, whether a loader is still showing, the main
// area's text (its first 20000 characters and its length), and every console error, uncaught
// exception and failed load seen while the view was open (the browser's own favicon request aside).
// With --auth (user:code, as the fixture writes it) it signs in first; with --shots it also saves a
// screenshot of each view there (<view>.png). Exit 1 when the page threw.
import {mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {launch, navigate, sleep} from './cdp.mjs';

const argv = process.argv.slice(2);
const arg = (name, fallback) => { const i = argv.indexOf('--' + name); return i < 0 ? fallback : argv[i + 1]; };
const base = arg('base'), views = JSON.parse(arg('views', '[]')), authFile = arg('auth'), profile = arg('profile'), shots = arg('shots');
if (!base || !views.length || !profile) { console.error('usage: pages.mjs --base URL --views JSON --profile DIR [--auth FILE] [--shots DIR]'); process.exit(2); }

const LOADER = `#main .loading, #main [role="status"] .orb-seq`;
const SETTLED = `!!document.querySelector('#main') && !document.querySelector('${LOADER}')
  && !!document.querySelector('#main h1, #main .page-heading, #main .error-box')`;
const READ = `(()=>{const m=document.querySelector('#main');const t=e=>(e?.textContent||'').replace(/\\s+/g,' ').trim();
  return {title:document.title, heading:t(m?.querySelector('h1, .page-heading')).slice(0,120),
    empty:[...(m?.querySelectorAll('.empty, .lt-empty, [class*="empty"]')||[])].map(t).filter(Boolean).slice(0,8).map(s=>s.slice(0,160)),
    error:t(m?.querySelector('.error-box')).slice(0,300), loader:!!document.querySelector('${LOADER}'),
    main:t(m||document.body).slice(0,20000), text:t(m||document.body).length, nav:[...document.querySelectorAll('#navigation a.nav-link')].map(a=>a.getAttribute('href')).join(' ')};})()`;

const b = await launch({profile});
let problems = [];
const note = text => problems.push(text.slice(0, 400));
b.on('Runtime.exceptionThrown', p => note('exception: ' + (p.exceptionDetails?.exception?.description || p.exceptionDetails?.text || '')));
b.on('Runtime.consoleAPICalled', p => { if (p.type === 'error') note('console.error: ' + JSON.stringify(p.args.map(a => a.value ?? a.description))); });
b.on('Log.entryAdded', p => { if (p.entry.level === 'error' && !/\/favicon\.ico$/.test(p.entry.url || '')) note('log: ' + p.entry.text + ' ' + (p.entry.url || '')); });
let failed = 0;
try {
  for (const m of ['Page.enable', 'Runtime.enable', 'Log.enable', 'Network.enable']) await b.send(m);
  if (authFile) {
    const [user, ...rest] = readFileSync(authFile, 'utf8').trim().split(':');
    await navigate(b, base + '/login');
    const status = await b.js(`fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:${JSON.stringify(JSON.stringify({user, code: rest.join(':')}))}}).then(r=>r.status)`);
    if (status !== 200) throw Error('sign-in answered ' + status);
  }
  for (const [key, path, ready] of views) {
    await navigate(b, 'about:blank');
    problems = [];
    await navigate(b, base + path);
    const settled = await b.until(ready || SETTLED, 30000, 150);
    await b.quiet(600, 20000);
    await sleep(800);
    const seen = await b.js(READ);
    if (shots) {
      mkdirSync(shots, {recursive: true});
      const shot = (await b.send('Page.captureScreenshot', {format: 'png'})).result.data;
      writeFileSync(join(shots, key + '.png'), Buffer.from(shot, 'base64'));
    }
    const row = {view: key, path, settled, ...seen, problems};
    if (problems.length) failed++;
    console.log(JSON.stringify(row));
  }
} catch (e) {
  console.log(JSON.stringify({view: 'driver', error: e.stack || e.message}));
  failed++;
} finally {
  await b.close();
}
process.exit(failed ? 1 : 0);
