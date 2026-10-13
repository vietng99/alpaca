// Capture every Studio page as a still, deterministic screenshot (t-474).
//
//   node alpaca/tests/studio/capture.mjs --base http://127.0.0.1:PORT --out DIR --auth FILE --stubs DIR
//        --at 2026-03-18T14:20:00Z [--views a,b] [--themes light,dark] [--widths 1440,390]
//
// The server is `alpaca serve` on the fixture project (alpaca/tests/studio_fixture.py), started under the
// fixed clock (fixed_clock.py); studio_shots.py starts both and calls this. Here the browser side is
// pinned so two captures of one tree are equal pixel for pixel:
//   - time: Date is fixed at --at, performance.now and requestAnimationFrame timestamps stand still,
//     and the time zone and locale are UTC and en-US, so clocks, "5m ago", day charts and the pulse hue
//     (its hour slot) never move;
//   - motion: prefers-reduced-motion is emulated, so the orb engine, Arthur and the globe draw one still
//     frame and the rings and beams stand still; any CSS animation or transition still running is then
//     finished (finite) or paused at its start (infinite) before the shot;
//   - randomness: Math.random is a seeded generator (the sign-in globe's stars, relay glyphs);
//   - host state: /hub/system.json (live CPU and memory), /hub/vcs.json (git) and /hub/vitals.json (the
//     connection label, whose first paint otherwise races the first route, and the health bar, whose hub
//     speed times real requests) are answered from the fixture's stub files over the DevTools Fetch
//     domain; everything else is the real server.
// A view is shot once it is ready (its own condition), no request has been in flight for 600 ms, the
// fonts are loaded and two shots 250 ms apart are byte-identical. Masked areas (MASKS) are recorded per
// image; compare.mjs skips them.
//
// Writes DIR/<view>-<width>-<theme>.png (viewport only, deviceScaleFactor 1, re-encoded at zlib level
// 9) and DIR/manifest.json. Exit 1 when a view never became ready or the page threw.
import {mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {launch, navigate, sleep} from './cdp.mjs';
import {decode, encode} from './png.mjs';

const argv = process.argv.slice(2);
const arg = (name, fallback) => { const i = argv.indexOf('--' + name); return i < 0 ? fallback : argv[i + 1]; };
const base = arg('base'), out = arg('out'), authFile = arg('auth'), stubs = arg('stubs'), at = arg('at', '2026-03-18T14:20:00Z');
if (!base || !out || !authFile || !stubs) { console.error('usage: capture.mjs --base URL --out DIR --auth FILE --stubs DIR [--at ISO]'); process.exit(2); }
const FIXED = Date.parse(at);
const HEIGHT = {1440: 900, 390: 844};

// The pages and views of the Studio (design/studio-template/design.md table 1c), plus the sign-in and
// workspace hub pages. `ready` is evaluated in the page until true.
const HUB_READY = `!!document.querySelector('#main') && !document.querySelector('#main .loading, #main [role="status"] .orb-seq')
  && !!document.querySelector('#main h1, #main .page-heading') && !document.querySelector('#main .error-box')
  && (()=>{const c=document.querySelector('#arthur canvas');if(!c)return false;const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;for(let i=3;i<d.length;i+=64)if(d[i])return true;return false;})()`;
export const VIEWS = [
  ['login', '/login', `!!document.querySelector('form') && document.body.dataset.page==='login'`],
  ['hub', '/', `document.querySelectorAll('.tile').length>0 && !document.querySelector('.tile .skeleton, .tile-live.checking')`],
  ['cockpit', '/hub/#cockpit', HUB_READY + ` && !!document.querySelector('.cockpit-heading')`],
  ['overview', '/hub/#overview', HUB_READY],
  ['roadmap', '/hub/#roadmap', HUB_READY + ` && !!document.querySelector('.rm-panel, [data-rm-node]')`],
  ['roadmap-mission', '/hub/#roadmap?view=mission', HUB_READY + ` && !!document.querySelector('#main svg')`],
  ['roadmap-finished', '/hub/#roadmap?view=finished', HUB_READY + ` && !!document.querySelector('.fin-op, #main .empty')`],
  ['roadmap-finished-op', '/hub/#roadmap?view=finished&op=op-001', HUB_READY + ` && !!document.querySelector('.fin-op-open') && !!document.querySelector('.rm-panel, [data-rm-node]')`],
  ['inflight', '/hub/#inflight', HUB_READY],
  ['research', '/hub/#research', HUB_READY],
  ['work', '/hub/#work', HUB_READY],
  ['activity', '/hub/#activity', HUB_READY],
  ['messages', '/hub/#messages', HUB_READY],
  ['library', '/hub/#library', HUB_READY],
  ['records', '/hub/#records', HUB_READY + ` && !!document.querySelector('.rec-table, #main .empty')`],
  ['records-decisions', '/hub/#records?section=decisions', HUB_READY + ` && !!document.querySelector('.rec-decision, #main .empty')`],
  ['records-proofs', '/hub/#records?section=proofs', HUB_READY + ` && !!document.querySelector('.rec-table, #main .empty')`],
  ['records-wiki', '/hub/#records?section=wiki&op=op-001', HUB_READY + ` && !!document.querySelector('.rec-panel, #main .notice')`],
  ['records-vault', '/hub/#records?section=wiki&vault=open&item=op-001', HUB_READY + ` && !!document.querySelector('#evidence-graph canvas') && !!document.querySelector('.eg-facts')`],
  ['sessions', '/hub/#sessions', HUB_READY + ` && !!document.querySelector('#analytics-index tbody tr')`],
  ['sessions-range', '/hub/#sessions?from=2026-03-16&to=2026-03-17', HUB_READY + ` && !!document.querySelector('#analytics-index')`],
  ['vcs', '/hub/#vcs', HUB_READY],
];
// Areas left out of the comparison: the version label (it names the release number, which a release
// bump changes without any change to the look).
export const MASKS = ['#alpaca-version'];
const STUBS = {'/hub/system.json': 'system.json', '/hub/vcs.json': 'vcs.json', '/hub/vitals.json': 'vitals.json'};

const only = arg('views') ? arg('views').split(',') : null;
const themes = (arg('themes') || 'light,dark').split(',');
const widths = (arg('widths') || '1440,390').split(',').map(Number);
mkdirSync(out, {recursive: true});

const freeze = theme => `(()=>{
  const FIXED=${FIXED};const N=Date;
  class D extends N{constructor(...a){super(...(a.length?a:[FIXED]));}static now(){return FIXED;}}
  D.parse=N.parse;D.UTC=N.UTC;window.Date=D;
  performance.now=()=>1000;
  const raf=window.requestAnimationFrame.bind(window);window.requestAnimationFrame=cb=>raf(()=>cb(1000));
  let seed=20260318;Math.random=()=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed/4294967296;};
  try{for(const k of ['alpaca.theme','alpaca.login-theme','alpaca.hub-theme'])localStorage.setItem(k,'${theme}');}catch{}
})();`;

const settle = `(async()=>{
  await document.fonts.ready;
  for(const a of document.getAnimations()){try{const t=a.effect.getComputedTiming();if(Number.isFinite(t.endTime))a.finish();else{a.pause();a.currentTime=0;}}catch{}}
  document.activeElement&&document.activeElement!==document.body&&document.activeElement.blur?.();
  await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));return true;})()`;

const b = await launch({profile: join(out, '.chrome-profile')});
// The browser build: two builds can rasterize the same page a few pixels apart (op-008 t-571).
const chrome = (await b.send('Browser.getVersion')).result?.product || 'unknown';
const errors = [];
const images = {};
let failed = 0;
try {
  b.on('Runtime.exceptionThrown', p => errors.push((p.exceptionDetails?.exception?.description || p.exceptionDetails?.text || '').slice(0, 300)));
  b.on('Fetch.requestPaused', async p => {
    const path = new URL(p.request.url).pathname;
    const body = readFileSync(join(stubs, STUBS[path]));
    await b.send('Fetch.fulfillRequest', {requestId: p.requestId, responseCode: 200,
      responseHeaders: [{name: 'Content-Type', value: 'application/json'}, {name: 'Cache-Control', value: 'no-store'}],
      body: body.toString('base64')});
  });
  await b.send('Fetch.enable', {patterns: Object.keys(STUBS).map(p => ({urlPattern: '*' + p + '*', requestStage: 'Request'}))});
  for (const m of ['Page.enable', 'Runtime.enable', 'Network.enable']) await b.send(m);
  await b.send('Network.setCacheDisabled', {cacheDisabled: false});
  await b.send('Emulation.setTimezoneOverride', {timezoneId: 'UTC'});
  await b.send('Emulation.setLocaleOverride', {locale: 'en-US'});
  const [user, ...rest] = readFileSync(authFile, 'utf8').trim().split(':');
  let signedIn = false, script = null;
  const views = VIEWS.filter(([key]) => !only || only.includes(key));
  // The sign-in page first, while signed out; then sign in once for the rest.
  views.sort((x, y) => (x[0] === 'login' ? 0 : 1) - (y[0] === 'login' ? 0 : 1));
  for (const [key, path, ready] of views) {
    if (key !== 'login' && !signedIn) {
      await navigate(b, base + '/login');
      const status = await b.js(`fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:${JSON.stringify(JSON.stringify({user, code: rest.join(':')}))}}).then(r=>r.status)`);
      if (status !== 200) throw Error('sign-in answered ' + status);
      signedIn = true;
    }
    for (const width of widths) for (const theme of themes) {
      const name = `${key}-${width}-${theme}`;
      if (script) await b.send('Page.removeScriptToEvaluateOnNewDocument', {identifier: script});
      script = (await b.send('Page.addScriptToEvaluateOnNewDocument', {source: freeze(theme)})).result.identifier;
      await b.send('Emulation.setDeviceMetricsOverride', {width, height: HEIGHT[width] || 900, deviceScaleFactor: 1, mobile: width < 600});
      await b.send('Emulation.setEmulatedMedia', {features: [{name: 'prefers-reduced-motion', value: 'reduce'}, {name: 'prefers-color-scheme', value: theme}]});
      await navigate(b, 'about:blank');
      const before = errors.length;
      await navigate(b, base + path);
      const ok = await b.until(ready, 30000, 150);
      await b.quiet(600, 20000);
      await b.js(settle);
      let shot = null, prev = null;
      for (let i = 0; i < 8; i++) {
        await sleep(250);
        await b.js(settle);
        shot = (await b.send('Page.captureScreenshot', {format: 'png', fromSurface: true})).result.data;
        if (shot === prev) break;
        prev = shot;
      }
      const stable = shot === prev;
      const masks = await b.js(`${JSON.stringify(MASKS)}.flatMap(s=>[...document.querySelectorAll(s)]).map(e=>e.getBoundingClientRect()).filter(r=>r.width&&r.height)
        .map(r=>{const x=Math.max(0,Math.floor(r.left)),y=Math.max(0,Math.floor(r.top));return [x,y,Math.min(innerWidth,Math.ceil(r.right))-x,Math.min(innerHeight,Math.ceil(r.bottom))-y];}).filter(r=>r[2]>0&&r[3]>0)`);
      const png = encode(decode(Buffer.from(shot, 'base64')));
      writeFileSync(join(out, name + '.png'), png);
      const thrown = errors.slice(before);
      images[name] = {view: key, path, width, height: HEIGHT[width] || 900, theme, masks, ready: ok, stable, bytes: png.length, errors: thrown};
      if (!ok || thrown.length) failed++;
      console.log(`${ok ? (stable ? 'shot ' : 'shot (unsettled)') : 'NOT READY'} ${name} ${png.length} bytes${thrown.length ? ' errors: ' + thrown.join(' | ') : ''}`);
    }
  }
} catch (e) {
  console.error('capture failed: ' + (e.stack || e.message));
  failed++;
} finally {
  await b.close();
}
const total = Object.values(images).reduce((n, i) => n + i.bytes, 0);
writeFileSync(join(out, 'manifest.json'), JSON.stringify({capture: {at, chrome, base_clock: 'fixed', reduced_motion: true, timezone: 'UTC', locale: 'en-US',
  device_scale_factor: 1, chrome_flags: 'alpaca/tests/studio/cdp.mjs BASE_FLAGS', masks: MASKS, stubs: STUBS, images: Object.keys(images).length, bytes: total},
  images}, null, 1) + '\n');
console.log(`${Object.keys(images).length} images, ${total} bytes, ${failed} failed`);
process.exit(failed ? 1 : 0);
