// Motion and live-path checks for the Studio (t-475): one named check per animation (M rows) and live
// path (L rows) of design/studio-template/design.md section 1d, run in headless Chrome against the
// fixture project (alpaca/tests/studio_fixture.py) served by `alpaca serve`.
//
//   node alpaca/tests/studio/motion.mjs --base URL --root FIXTURE --python PY --out results.json
//        [--at ISO] [--checks id,id] [--mutate id]
//
// Each check opens a fresh page, observes the real behavior (computed styles, running animations,
// canvas frames, requests, Arthur's player state) and returns PASS or FAIL with what it saw. The page
// clock can run faster (`warp`: Date, performance.now, timers and frames at N times real speed) so a
// 7 s orb cycle or a 15 s timer is seen in a second or two; record changes are real writes to the
// fixture (studio/poke.py) that the server pushes over its event stream.
//
// --mutate <id> removes one piece of animation or live-path code in the page (MUTATIONS: an edit of
// one served asset, or a stylesheet) to prove the check that guards it fails without it.
//
// Arthur: the poses check reads every action of the player's manifest (window.ArthurAssets), so poses
// a newer package adds are drawn-checked with no change here; the looping poses by priority are rows of
// ARTHUR_LOOPS and the record-driven reactions rows of ARTHUR_TRIGGERS. Detective is not wired by
// arthur.js (no record field marks an investigation task), so it has no trigger check.
import {readFileSync, writeFileSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {join, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {launch, navigate, sleep} from './cdp.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const argv = process.argv.slice(2);
const arg = (name, fallback) => { const i = argv.indexOf('--' + name); return i < 0 ? fallback : argv[i + 1]; };
const AT = Date.parse(arg('at', '2026-03-18T14:20:00Z'));
const base = arg('base'), root = arg('root'), python = arg('python', 'python3'), outFile = arg('out');
const only = arg('checks') ? arg('checks').split(',') : null, mutateId = arg('mutate');
if (!base || !root) { console.error('usage: motion.mjs --base URL --root FIXTURE [--python PY] [--out FILE] [--checks ids] [--mutate id]'); process.exit(2); }
const fixtureEnv = JSON.parse(readFileSync(join(root, '.studio-fixture', 'env.json'), 'utf8')).env;
const [user, ...rest] = readFileSync(join(root, '.studio-fixture', 'auth'), 'utf8').trim().split(':');

// ---- removals that prove a check guards its code (the t-475 fail case) -----------------------------
export const MUTATIONS = {
  'pulse-cycle-off': {check: 'M1-pulse-cycle', asset: 'pulse.js', find: 'cycleTimer = setInterval(() => { if (busy) { step++; setState(); } }, CYCLE_MS);', replace: 'cycleTimer = 0;'},
  'pulse-rings-still': {check: 'M1-pulse-rings', css: '.pulse-ring,.pulse-sparks i{animation:none!important}'},
  'pulse-hue-snap': {check: 'M1-pulse-hue-fade', asset: 'pulse.js', find: 'const path = fadePath(shown, target);', replace: 'const path = [target];'},
  'shapeshift-snap': {check: 'M2-orb-shapeshift-eases-back', asset: 'pulse.js', find: 'return 1 - smooth((t - WILD_MS) / SETTLE_MS);', replace: 'return 0;'},
  'orb-click-no-reload': {check: 'M2-orb-click-reloads', asset: 'hub.js', find: "$('#pulse-orb').addEventListener('click',()=>route(true));", replace: ''},
  'orbs-frozen': {check: 'M3-thinking-orbs-animate', asset: 'orb.js', find: 'if (live.size && !reduced()) raf = requestAnimationFrame(tick);', replace: ''},
  'loader-still': {check: 'M4-loader-sequence', css: '.seq-orb,.seq-orb:last-child,.seq-line,.seq-line:last-child{animation:none!important}'},
  'beams-still': {check: 'M5-border-beams', css: '.beam-ring,.beam-ring>i{animation:none!important}'},
  'beams-no-pause': {check: 'M5-border-beams', asset: 'beam.js', find: 'shown.get(ring) ? a.play() : a.pause();', replace: 'a.play();'},
  'morph-full-repaint': {check: 'M6-morph-patch-in-place', asset: 'morph.js', find: 'const t=document.createElement(\'template\');t.innerHTML=html;const changed=morphChildren(el,t.content);last.set(el,html);return changed;', replace: 'el.innerHTML=html;last.set(el,html);return true;'},
  'arthur-no-offline': {check: 'M7-arthur-loop-priority', asset: 'arthur.js', find: "if (offline) return {action: 'problem', caption: 'Offline'};", replace: ''},
  'arthur-no-celebrate': {check: 'M8-arthur-celebrates-done', asset: 'arthur.js', find: "if (cheer) out.push({action: 'celebrate', why: cheer});", replace: ''},
  'arthur-no-pace': {check: 'M9-arthur-paces', asset: 'arthur.js', find: 'stage.style.transform = x ? `translateX(${x}px)` : \'\';', replace: ''},
  'arthur-pace-any-hash': {check: 'M9-arthur-paces', asset: 'arthur.js', find: 'if (next !== page && !offline) react([{pace: true}]);', replace: 'if (!offline) react([{pace: true}]);'},
  'arthur-peek-on-wheel': {check: 'M9-arthur-peekaboo-on-sidebar-scroll', asset: 'arthur.js', find: "if (sidebar) sidebar.addEventListener('scroll', peek, {passive: true});", replace: "if (sidebar) { sidebar.addEventListener('scroll', peek, {passive: true}); sidebar.addEventListener('wheel', peek, {passive: true}); }"},
  'arthur-offline-any-amber': {check: 'M7-arthur-loop-priority', asset: 'arthur.js', find: "export const connectionLost = conn => conn.classList.contains('error') && LOST_LABELS.has((conn.querySelector('span')?.textContent || '').trim());", replace: "export const connectionLost = conn => conn.classList.contains('error');"},
  // the fold memory has two writers (the toggle handler and rememberRoadmap before a re-render): drop both
  'roadmap-forgets-folds': {check: 'M10-roadmap-fold-kept', asset: 'roadmap.js', edits: [
    ["group.addEventListener('toggle',()=>{p.collapsed[group.dataset.rmGroup]=!group.open;", "group.addEventListener('toggle',()=>{"],
    ["for(const group of panel.querySelectorAll('[data-rm-group]'))p.collapsed[group.dataset.rmGroup]=!group.open;", '']]},
  'mission-no-redraw': {check: 'M11-mission-redraw-on-resize', asset: 'mission.js', find: 'root._mmObserver=new ResizeObserver(()=>draw(root,data,st));', replace: 'root._mmObserver=new ResizeObserver(()=>{});'},
  'flash-still': {check: 'M12-event-flash', css: '.vtl-item.event-flash,.activity-item.event-flash{animation:none!important}'},
  'globe-still': {check: 'M13-signin-globe-and-unlock', asset: '/login/assets/vault-globe.js', find: 'if (!reduced) raf = requestAnimationFrame(frame);', replace: ''},
  'sse-deaf': {check: 'L1-sse-rev-ping', asset: 'hub.js', find: "stream.addEventListener('rev',", replace: "stream.addEventListener('rev-ignored',"},
  'lib-marks-all': {check: 'L2-per-part-refresh', asset: 'liveflags.js', find: "lib:['library']}", replace: "lib:['library','sessions','analytics']}"},
  'flags-forget-generation': {check: 'L3-live-flag-generation', asset: 'liveflags.js', find: 'settle(flag,seen){if((gen[flag]||0)===seen)dirty[flag]=false;}', replace: 'settle(flag,seen){dirty[flag]=false;}'},
  'overview-timer-off': {check: 'L4-overview-timer-and-live-orb', asset: 'hub.js', find: 'finally{liveOrb(false);liveOverviewBusy=false;}},15000);', replace: 'finally{liveOrb(false);liveOverviewBusy=false;}},15000e6);'},
  'clock-frozen': {check: 'L4-topbar-clock-ticks', asset: 'hub.js', find: 'paintClock();setInterval(paintClock,15000);', replace: 'paintClock();'},
  'no-notice': {check: 'L4-new-activity-notice', asset: 'hub.js', find: "if(dependsOn(state.route,marked,liveParams())){newActivity();liveOrb(true,'listening');}", replace: ''},
  'health-once': {check: 'L5-connection-health', asset: 'hub.js', find: 'refreshHealth();setInterval(refreshHealth,10000);', replace: 'refreshHealth();'},
  'system-poll-off': {check: 'L6-own-polls', asset: 'system.js', find: 'timer = setInterval(() => { tick(); }, EVERY_MS);', replace: 'timer = 0;'},
  'reduced-ignored': {check: 'R1-reduced-motion', asset: 'hub.css', find: '@media (prefers-reduced-motion:reduce) { .pulse-ring { animation:none;', replace: '@media (prefers-reduced-motion:no-preference-ever) { .pulse-ring { animation:none;'},
};

// ---- page setup -------------------------------------------------------------------------------------
const INSTRUMENT = (warp) => `(()=>{
  const realST=window.setTimeout.bind(window),realSI=window.setInterval.bind(window),realRAF=window.requestAnimationFrame.bind(window);
  window.__realST=realST;
  try{performance.setResourceTimingBufferSize(100000);}catch{}
  // Real-time sampler: fn is evaluated every 'every' ms for 'ms' ms with the page's own (unwarped) timers.
  window.__sample=(fn,ms,every=40)=>new Promise(r=>{const out=[],end=performance.__real()+ms;const tick=()=>{try{out.push(fn());}catch(e){out.push('ERR '+e.message);}if(performance.__real()<end)realST(tick,every);else r(out);};tick();});
  window.__actions=[];window.__gaze=[];window.__revs=[];window.__es=[];
  document.addEventListener('arthur:actionchange',e=>window.__actions.push(e.detail&&e.detail.action),true);
  document.addEventListener('arthur:gazechange',e=>window.__gaze.push(e.detail&&e.detail.index),true);
  // Arthur's player instance, as the hub creates it (window.ArthurAvatar.create).
  let player;Object.defineProperty(window,'ArthurAvatar',{configurable:true,get(){return player;},set(v){
    const create=v.create.bind(v);v.create=async(...a)=>{const p=await create(...a);window.__arthur=p;return p;};player=v;}});
  // The hub's event stream: every rev blob it receives.
  const ES=window.EventSource;window.EventSource=function(url,opts){const es=new ES(url,opts);window.__es.push(es);es.addEventListener('rev',e=>window.__revs.push(e.data));return es;};
  window.EventSource.prototype=ES.prototype;
  const W=${warp},pn=performance.now.bind(performance);performance.__real=pn;
  // The page clock starts at the fixture's instant (the server's clock starts there too), so the
  // record's recent events fall inside the live timeline's window and "5m ago" reads as in the capture.
  const N=Date,rn=N.now.bind(N),r0=rn(),p0=pn(),AT=${AT};let dateShift=0;
  if(W!==1){
    const now=()=>AT+(rn()-r0)*W+dateShift;
    class D extends N{constructor(...a){super(...(a.length?a:[now()]));}static now(){return now();}}
    D.parse=N.parse;D.UTC=N.UTC;window.Date=D;
    performance.now=()=>p0+(pn()-p0)*W;
    window.setTimeout=(f,ms,...a)=>realST(f,(+ms||0)/W,...a);
    window.setInterval=(f,ms,...a)=>realSI(f,Math.max(1,(+ms||0)/W),...a);
    window.requestAnimationFrame=cb=>realRAF(t=>cb(p0+(t-p0)*W));
    window.__clock={shiftDate(ms){dateShift+=ms;}};
  }else{
    const now=()=>AT+(rn()-r0)+dateShift;class D extends N{constructor(...a){super(...(a.length?a:[now()]));}static now(){return now();}}
    D.parse=N.parse;D.UTC=N.UTC;window.Date=D;window.__clock={shiftDate(ms){dateShift+=ms;}};
  }
})();`;
const STYLE = css => `document.addEventListener('DOMContentLoaded',()=>{const s=document.createElement('style');s.textContent=${JSON.stringify(css)};document.head.appendChild(s);});`;

const b = await launch({profile: join(outFile ? dirname(outFile) : '/tmp', '.chrome-motion-' + process.pid)});
const counts = new Map();
// a mutation names a hub asset by file name, or any served file by its full path
const assetPath = name => name.startsWith('/') ? name : '/hub/assets/' + name;
let mutated = 0;
let held = [], holdPath = null, holdOne = false, script = null, rewrite = null;
const mutation = mutateId ? MUTATIONS[mutateId] : null;
if (mutateId && !mutation) { console.error('unknown mutation ' + mutateId); process.exit(2); }

b.on('Network.requestWillBeSent', p => { try { const u = new URL(p.request.url); counts.set(u.pathname, (counts.get(u.pathname) || 0) + 1); } catch {} });
const count = path => counts.get(path) || 0;
b.on('Fetch.requestPaused', async p => {
  const path = new URL(p.request.url).pathname;
  try {
    if (holdPath && path === holdPath && p.responseStatusCode && (!holdOne || !held.length)) { held.push(p); return; }
    if (rewrite && path === rewrite.path && p.responseStatusCode === 200) {
      const body = await b.send('Fetch.getResponseBody', {requestId: p.requestId});
      const data = JSON.parse(body.result.base64Encoded ? Buffer.from(body.result.body, 'base64').toString('utf8') : body.result.body);
      rewrite.fn(data);
      return await fulfil(p, Buffer.from(JSON.stringify(data), 'utf8'));
    }
    if (mutation?.asset && path === assetPath(mutation.asset) && p.responseStatusCode === 200) {
      mutated++;
      const body = await b.send('Fetch.getResponseBody', {requestId: p.requestId});
      let text = body.result.base64Encoded ? Buffer.from(body.result.body, 'base64').toString('utf8') : body.result.body;
      for (const [find, replace] of mutation.edits || [[mutation.find, mutation.replace]]) {
        if (!text.includes(find)) console.error('MUTATION TEXT NOT FOUND in ' + mutation.asset + ': ' + find);
        text = text.split(find).join(replace);
      }
      return await fulfil(p, Buffer.from(text, 'utf8'));
    }
    await b.send('Fetch.continueRequest', {requestId: p.requestId});
  } catch (e) { console.error('fetch hook: ' + e.message); }
});
async function fulfil(p, body) {
  const headers = (p.responseHeaders || []).filter(h => !/^(content-encoding|content-length|etag)$/i.test(h.name));
  await b.send('Fetch.fulfillRequest', {requestId: p.requestId, responseCode: p.responseStatusCode || 200, responseHeaders: headers, body: body.toString('base64')});
}
async function patterns() {
  const list = [];
  if (mutation?.asset) list.push({urlPattern: '*' + assetPath(mutation.asset) + '*', requestStage: 'Response'});
  if (holdPath) list.push({urlPattern: '*' + holdPath + '*', requestStage: 'Response'});
  if (rewrite && rewrite.path !== holdPath) list.push({urlPattern: '*' + rewrite.path + '*', requestStage: 'Response'});
  if (list.length) await b.send('Fetch.enable', {patterns: list}); else await b.send('Fetch.disable');
}
// Rewrite one JSON read for the rest of a check (fn edits the parsed body), e.g. the overview's arthur block.
async function rewriteJSON(path, fn) { rewrite = path ? {path, fn} : null; await patterns(); }
async function hold(path, {first = false} = {}) { holdPath = path; holdOne = first; held = []; await patterns(); }
async function release() {
  const now = held; held = []; holdPath = null; await patterns();
  for (const p of now) {
    const body = await b.send('Fetch.getResponseBody', {requestId: p.requestId});
    if (body.result) await fulfil(p, Buffer.from(body.result.body, body.result.base64Encoded ? 'base64' : 'utf8'));
    else await b.send('Fetch.continueRequest', {requestId: p.requestId});
  }
  return now.length;
}

function poke(action) {
  const run = spawnSync(python, [join(HERE, 'poke.py'), root, action], {env: {...process.env, ...fixtureEnv}, encoding: 'utf8', timeout: 120000});
  if (run.status !== 0) throw Error('poke ' + action + ' failed: ' + run.stderr.slice(-500));
  return run.stdout.trim();
}

const HUB_READY = `!!document.querySelector('#main h1,#main .page-heading') && !document.querySelector('#main .loading')`;
async function open(path, {warp = 1, reduced = false, width = 1440, height = 900, ready = HUB_READY, theme = 'light', signin = true} = {}) {
  if (script) await b.send('Page.removeScriptToEvaluateOnNewDocument', {identifier: script});
  const source = INSTRUMENT(warp) + (mutation?.css ? STYLE(mutation.css) : '') +
    `try{for(const k of ['alpaca.theme','alpaca.login-theme','alpaca.hub-theme'])localStorage.setItem(k,'${theme}');}catch{}`;
  script = (await b.send('Page.addScriptToEvaluateOnNewDocument', {source})).result.identifier;
  await b.send('Emulation.setDeviceMetricsOverride', {width, height, deviceScaleFactor: 1, mobile: false});
  await b.send('Emulation.setEmulatedMedia', {features: [{name: 'prefers-reduced-motion', value: reduced ? 'reduce' : 'no-preference'}, {name: 'prefers-color-scheme', value: theme}]});
  await b.send('Animation.enable');
  await b.send('Animation.setPlaybackRate', {playbackRate: 1});
  await navigate(b, 'about:blank');
  counts.clear();
  await navigate(b, base + path);
  if (ready && !await b.until(ready, 30000, 120)) throw Error('page not ready: ' + path);
  // Arthur loads a strip the first time a pose needs it (t-595), so the page's first poses fetch and decode images
  // for a moment after it is ready; a check that samples frames waits for that to end rather than race it.
  await b.quiet(400, 10000);
}
const js = expr => b.js(expr);
const until = (expr, ms = 20000) => b.until(expr, ms, 100);
async function arthurReady() { return until(`!!window.__arthur && window.__arthur.getState().ready`, 20000); }
async function mouse(type, x, y, extra = {}) { await b.send('Input.dispatchMouseEvent', {type, x, y, button: 'none', pointerType: 'mouse', ...extra}); }
async function click(selector) {
  const box = await js(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});if(!e)return null;e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2];})()`);
  if (!box) throw Error('nothing to click: ' + selector);
  await mouse('mouseMoved', box[0], box[1]);
  await b.send('Input.dispatchMouseEvent', {type: 'mousePressed', x: box[0], y: box[1], button: 'left', clickCount: 1});
  await b.send('Input.dispatchMouseEvent', {type: 'mouseReleased', x: box[0], y: box[1], button: 'left', clickCount: 1});
}
const animNames = sel => `[...document.querySelectorAll(${JSON.stringify(sel)})].flatMap(e=>e.getAnimations({subtree:true}).map(a=>a.animationName||a.transitionProperty||a.constructor.name))`;
// `patience` (real ms, 0 = off): a canvas that has not changed after `wait` is looked at again every
// 50 real ms until it does or the patience runs out. Headless Chrome on a loaded host can drop to about
// one frame a second, and under warp 10 a 400 ms wait is 40 real ms, so a moving canvas can look still.
const canvasFrames = (sel, wait, patience = 0) => `(async()=>{const cs=[...document.querySelectorAll(${JSON.stringify(sel)})].map(e=>e.shadowRoot?e.shadowRoot.querySelector('canvas'):e).filter(c=>c&&c.width&&c.getBoundingClientRect().width&&c.getBoundingClientRect().top<innerHeight);
  const a=cs.map(c=>c.toDataURL());await new Promise(r=>setTimeout(r,${wait}));
  const moved=cs.map((c,i)=>c.toDataURL()!==a[i]);
  for(const end=performance.__real()+${patience};moved.includes(false)&&performance.__real()<end;){
    await new Promise(r=>window.__realST(r,50));cs.forEach((c,i)=>{if(!moved[i]&&c.toDataURL()!==a[i])moved[i]=true;});}
  const blank=cs.filter(c=>{const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;for(let i=3;i<d.length;i+=16)if(d[i])return false;return true;}).length;
  return {n:cs.length,changed:moved.filter(Boolean).length,blank};})()`;

// ---- the checks -------------------------------------------------------------------------------------
const CHECKS = [];
const check = (id, row, name, run) => CHECKS.push({id, row, name, run});

check('M1-pulse-cycle', 'M1', 'Pulse orb walks the thinking states while work runs', async () => {
  await open('/hub/#cockpit', {warp: 10});
  const states = await js(`window.__sample(()=>document.querySelector('#pulse-orb alpaca-orb').getAttribute('state'),3200,60)`);
  const distinct = [...new Set(states)];
  const CYCLE = ['searching', 'solving', 'connecting', 'weaving', 'composing', 'shaping', 'working'];
  const ordered = distinct.every((s, i) => i === 0 || CYCLE.indexOf(s) === (CYCLE.indexOf(distinct[i - 1]) + 1) % CYCLE.length);
  return {pass: distinct.length >= 3 && distinct.every(s => CYCLE.includes(s)) && ordered, info: distinct.join(' > ')};
});
check('M1-pulse-rings', 'M1', 'Pulse rings and sparks ripple out, more with more work', async () => {
  await open('/hub/#cockpit');
  const got = await js(`(async()=>{const el=document.querySelector('#pulse-orb');const rings=[...el.querySelectorAll('.pulse-ring')].filter(r=>!r.hidden);
    const t0=rings.map(r=>getComputedStyle(r).transform);await new Promise(r=>setTimeout(r,400));const t1=rings.map(r=>getComputedStyle(r).transform);
    const names=rings.flatMap(r=>r.getAnimations().filter(a=>a.playState==='running').map(a=>a.animationName));
    const sparks=el.querySelectorAll('.pulse-sparks i');const sparkRun=[...sparks].filter(s=>s.getAnimations().some(a=>a.animationName==='spark-fly'&&a.playState==='running')).length;
    return {load:+el.dataset.load,waves:+getComputedStyle(el).getPropertyValue('--waves'),rings:rings.length,moving:t0.filter((t,i)=>t!==t1[i]).length,names:[...new Set(names)],sparks:sparks.length,sparkRun};})()`);
  return {pass: got.load >= 1 && got.rings === got.waves && got.rings >= 3 && got.moving === got.rings && got.names.includes('pulse-out') && got.sparks >= 8 && got.sparkRun === got.sparks, info: JSON.stringify(got)};
});
check('M1-pulse-hue-fade', 'M1', 'Pulse hue follows the hour and fades to the next slot, never snaps', async () => {
  await open('/hub/#cockpit', {warp: 10});
  const h0 = await js(`+document.querySelector('#pulse-orb').style.getPropertyValue('--pulse-hue')`);
  await js(`window.__clock.shiftDate(5*60*1000);true`);
  const hues = await js(`window.__sample(()=>+document.querySelector('#pulse-orb').style.getPropertyValue('--pulse-hue'),9000,60)`);
  // the clock runs at 10x after a 5 minute jump: the hue moves on by one or more 1.25 degree slots, each in
  // 20 small steps (FADE_MS / FADE_TICK_MS), the short way round; a snap is one jump of a whole slot
  const path = [h0, ...hues].filter((h, i, a) => i === 0 || h !== a[i - 1]);
  const deltas = path.slice(1).map((h, i) => ((h - path[i] + 540) % 360) - 180);
  const biggest = Math.max(0, ...deltas.map(Math.abs)), total = deltas.reduce((a, d) => a + d, 0);
  return {pass: deltas.length >= 8 && biggest <= 0.2 && deltas.every(d => d > 0) && total >= 1.2, info: `from ${h0} to ${path.at(-1)} in ${deltas.length} steps, largest ${biggest.toFixed(2)} degrees`};
});
check('M2-orb-shapeshift-eases-back', 'M2', 'Orb click: blob shapeshift, squash and twist, eases back with no snap', async () => {
  await open('/hub/#cockpit');
  await until(`!document.querySelector('#main .loading')`);
  await click('#pulse-orb');
  const s = await js(`window.__sample(()=>{const el=document.querySelector('#pulse-orb'),st=el.style;return [performance.__real(),st.getPropertyValue('--wob-sx'),st.getPropertyValue('--wob-sy'),st.getPropertyValue('--wob-r'),st.getPropertyValue('--wob-blob'),el.querySelector('alpaca-orb').getAttribute('state')];},5200,30)`);
  const t0 = s[0][0], rows = s.map(([t, sx, sy, r, blob, state]) => ({t: t - t0, dev: sx === '' ? null : Math.max(Math.abs(+sx - 1), Math.abs(+sy - 1)), r, blob, state}));
  const env = (a, z) => Math.max(0, ...rows.filter(x => x.t >= a && x.t < z && x.dev !== null).map(x => x.dev));
  const wild = env(700, 2500), settle = [2700, 3100, 3500, 3900].map(a => +env(a, a + 400).toFixed(3));
  const shapes = new Set(rows.filter(x => x.t < 2600).map(x => x.state)).size;
  const blobs = new Set(rows.filter(x => x.t < 2600).map(x => x.blob)).size;
  const restored = rows.at(-1).dev === null;
  const graded = settle.filter(v => v > 0.004 && v < wild * 0.95).length >= 3 && settle.every((v, i) => i === 0 || v <= settle[i - 1] + 0.01);
  return {pass: wild > 0.1 && shapes >= 4 && blobs >= 10 && graded && restored, info: `wild ${wild.toFixed(3)}, settle ${settle.join(' > ')}, shapes ${shapes}, blobs ${blobs}, back at rest ${restored}`};
});
check('M2-orb-click-reloads', 'M2', 'Orb click reloads the view (the reload control since t-485)', async () => {
  await open('/hub/#overview');
  await sleep(500);
  const before = count('/hub/overview.json');
  await js(`window.__mounted=0;new MutationObserver(()=>{if(document.querySelector('#main .orb-seq'))window.__mounted++;}).observe(document.querySelector('#main'),{childList:true});true`);
  await click('#pulse-orb');
  const ok = await until(`window.__mounted>0 && !document.querySelector('#main .loading') && !!document.querySelector('#main h1')`, 15000);
  await sleep(300);
  return {pass: ok && count('/hub/overview.json') > before, info: `overview reads ${before} -> ${count('/hub/overview.json')}, loader mounted ${await js('window.__mounted')}`};
});
check('M3-thinking-orbs-animate', 'M3', 'Thinking orbs draw moving frames in badges, empty states, nav and connection', async () => {
  await open('/hub/#cockpit');
  await sleep(400);
  const got = await js(canvasFrames('alpaca-orb', 350));
  const states = await js(`[...new Set([...document.querySelectorAll('alpaca-orb')].map(o=>o.getAttribute('state')))]`);
  return {pass: got.n >= 4 && got.changed >= Math.ceil(got.n * 0.8) && got.blank === 0, info: `${got.changed} of ${got.n} visible orbs moved, ${got.blank} blank; states ${states.join(',')}`};
});
check('M4-loader-sequence', 'M4', 'Loader: orbs join one by one with their lines while a read lasts', async () => {
  await open('/hub/#overview');
  await hold('/hub/overview.json');
  await js(`document.querySelector('#pulse-orb').click();true`);
  const shown = await until(`!!document.querySelector('#main .orb-seq')`, 8000);
  const n = await js(`document.querySelectorAll('#main .orb-seq .seq-orb').length`);
  const names = await js(`[...new Set(${animNames('#main .orb-seq .seq-orb')})]`);
  const op = await js(`window.__sample(()=>[...document.querySelectorAll('#main .orb-seq .seq-orb')].map(o=>+getComputedStyle(o).opacity),2600,100)`);
  const joined = op.length ? op[op.length - 1].filter(v => v > 0.3).length : 0, first = op.length ? op[0].filter(v => v > 0.3).length : 0;
  const lines = await js(`[...document.querySelectorAll('#main .orb-seq .seq-line')].map(l=>+getComputedStyle(l).opacity)`);
  const released = await release();
  const done = await until(`!document.querySelector('#main .orb-seq') && !!document.querySelector('#main h1, #main .rm-panel')`, 15000);
  return {pass: shown && n >= 3 && names.includes('seq-in') && joined > first && done && released > 0, info: `orbs ${n}, animations ${names.join(',')}, visible ${first} -> ${joined}, lines ${lines.map(v => v.toFixed(1)).join('/')}, released ${released}`};
});
check('M5-border-beams', 'M5', 'Border beams ride live cards; the alert beam is a still red ring; off-screen beams pause', async () => {
  await open('/hub/#roadmap', {ready: HUB_READY + ` && !!document.querySelector('.beam .beam-ring')`});
  const got = await js(`(async()=>{const wait=ms=>new Promise(r=>setTimeout(r,ms));await wait(400);
    const beams=[...document.querySelectorAll('.beam')],alerts=beams.filter(e=>e.classList.contains('beam-alert')),live=beams.filter(e=>!e.classList.contains('beam-alert'));
    const comet=e=>e.querySelector(':scope > .beam-ring > i'),state=e=>comet(e).getAnimations().filter(a=>a.animationName==='beam-spin').map(a=>a.playState).join();
    const inView=e=>{const r=e.getBoundingClientRect();return r.bottom>0&&r.top<innerHeight&&r.right>0&&r.left<innerWidth;};
    const onScreen=live.filter(inView),t0=onScreen.map(e=>getComputedStyle(comet(e)).transform);await wait(300);const t1=onScreen.map(e=>getComputedStyle(comet(e)).transform);
    const onRunning=onScreen.filter(e=>state(e)==='running').length;
    const alertMoving=alerts.filter(e=>e.getAnimations({subtree:true}).some(a=>a.playState==='running')).length;
    const filtered=beams.filter(e=>getComputedStyle(e.querySelector('.beam-ring')).filter!=='none'||getComputedStyle(comet(e)).filter!=='none').length;
    // take one live card off screen: its beam must pause, and run again when it returns
    const host=live[0];host.style.top='-9000px';await wait(400);const hidden=state(host);host.style.top='';await wait(400);const back=state(host);
    return {beams:beams.length,alerts:alerts.length,live:live.length,onScreen:onScreen.length,onRunning,turning:t0.filter((x,i)=>x!==t1[i]).length,alertMoving,filtered,hidden,back};})()`);
  return {pass: got.beams >= 2 && got.alerts >= 1 && got.live >= 1 && got.onScreen >= 1 && got.onRunning === got.onScreen && got.turning === got.onScreen && got.alertMoving === 0 && got.filtered === 0 && got.hidden === 'paused' && got.back === 'running', info: JSON.stringify(got)};
});
check('M6-morph-patch-in-place', 'M6', 'A live refresh patches the page in place: same nodes, scroll kept', async () => {
  // In-flight refreshes its board through morph.js patch(); a new claim adds a card and keeps every other node.
  // Scroll anchoring is off for the page: Chrome would move scrollY itself to keep the visible part still
  // when the new card lands above it, and the check is that the page's own code never moves the reader.
  await open('/hub/#inflight', {ready: HUB_READY + ` && !!document.querySelector('#inflight-board')`});
  await until(`window.__revs.length>0`, 15000);
  await sleep(800);
  const n = `(document.querySelector('#main').textContent.match(/(\\d+) in flight/)||[])[1]`;
  const before = await js(n);
  await js(`document.documentElement.style.minHeight='5000px';document.documentElement.style.overflowAnchor='none';scrollTo(0,300);window.__nodes=[...document.querySelectorAll('#inflight-board *')];window.__board=document.querySelector('#inflight-board');true`);
  poke('claim');
  const ok = await until(`${n}!==${JSON.stringify(before)}`, 30000);
  await sleep(400);
  const got = await js(`({kept:window.__nodes.filter(x=>x.isConnected).length,of:window.__nodes.length,same:window.__board===document.querySelector('#inflight-board'),y:scrollY})`);
  return {pass: ok && got.same && got.of > 20 && got.kept / got.of > 0.8 && got.y === 300, info: `in flight ${before} -> ${await js(n)}; ${got.kept} of ${got.of} board nodes kept, board same ${got.same}, scroll ${got.y}`};
});
check('M7-arthur-loop-priority', 'M7', 'Arthur plays the looping pose the record calls for, problem first when offline', async () => {
  await open('/hub/#cockpit', {warp: 10});
  await arthurReady();
  const want = await until(`window.__arthur.getState().action==='waiting'`, 20000);
  const cap1 = await js(`document.querySelector('#arthur-caption').textContent`);
  // an amber row with a reachable server ("Stale") is not offline, even well past 30 s (brief L4)
  await js(`(()=>{const c=document.querySelector('#connection');c.classList.add('error');c.querySelector('span').textContent='Stale';return true;})()`);
  await sleep(5000);
  const stale = await js(`window.__arthur.getState().action`);
  // the lost connection: the health call fails, so the page's own 10 s check keeps the row at "Offline"
  await js(`(()=>{const f=window.fetch;window.__realFetch=f;window.fetch=(u,o)=>String(u).includes('/hub/vitals.json')?Promise.reject(new TypeError('offline')):f(u,o);return true;})()`);
  const offline = await until(`window.__arthur.getState().action==='problem' && document.querySelector('#arthur-caption').textContent==='Offline'`, 15000);
  await js(`(()=>{window.fetch=window.__realFetch;const c=document.querySelector('#connection');c.classList.remove('error');c.querySelector('span').textContent='Live';return true;})()`);
  const back = await until(`window.__arthur.getState().action==='waiting'`, 15000);
  return {pass: want && /^Waiting on you:/.test(cap1) && stale === 'waiting' && offline && back, info: `waiting ${want} (${cap1}), stale row ${stale}, problem when the health call fails ${offline}, back ${back}`};
});
check('M7-arthur-poses-render', 'M7', 'Every pose in the Arthur package draws its frames', async () => {
  await open('/hub/#cockpit');
  await arthurReady();
  const actions = await js(`Object.keys(window.ArthurAssets.actions)`);
  const bad = [];
  for (const a of actions) {
    // arthur.js puts its own pose back on its 15 s tick; a sample it cut short is taken again
    let got;
    // a pose's strip loads the first time it is played (t-595): have it decoded before the frames are sampled
    await js(`(async()=>{const p=window.__arthur;await p._load(p.manifest.actions[${JSON.stringify(a)}].image);return true;})()`);
    for (let attempt = 0; attempt < 3; attempt++) {
    got = await js(`(async()=>{const p=window.__arthur;p.setAction(${JSON.stringify(a)},{loop:true});const c=document.querySelector('#arthur canvas');const seen=new Set(),frames=new Set();
      for(let i=0;i<14;i++){await new Promise(r=>setTimeout(r,90));const st=p.getState();frames.add(st.frameIndex);seen.add(c.toDataURL().length+':'+c.toDataURL().slice(-64));}
      const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;let ink=0;for(let i=3;i<d.length;i+=32)if(d[i])ink++;
      return {action:p.getState().action,frames:frames.size,images:seen.size,ink,count:window.ArthurAssets.actions[${JSON.stringify(a)}].frames.length};})()`);
    if (got.action === a) break;
    }
    if (got.action !== a || !got.ink || (got.count > 1 && got.images < 2)) bad.push(`${a}:${JSON.stringify(got)}`);
  }
  return {pass: actions.length >= 11 && !bad.length, info: `${actions.length} poses: ${actions.join(',')}${bad.length ? ' BAD ' + bad.join(' ') : ''}`};
});
// Reactions read from the record: each trigger is a real fixture write (poke.py), the expected one-shot.
// To add a reaction: a poke.py action that writes its record signal, the pose, the caption it shows.
export const ARTHUR_TRIGGERS = [
  ['M8-arthur-celebrates-done', 'done', 'celebrate', /A task is done/],
  ['M8-arthur-stamps-sealed-proof', 'done', 'stamping', /A proof was sealed/],
  ['M8-arthur-problem-on-gate-fail', 'gatefail', 'problem', /A gate failed/],
  ['M8-arthur-greets-new-session', 'session', 'greeting', /new session joined/],
  ['M8-arthur-raises-hand-new-question', 'question', 'raising_hand', /A new question for you/],
  ['M8-arthur-shield-up-on-blocked', 'gateblocked', 'shield_up', /blocked a step/],
  ['M8-arthur-packs-release-build', 'release', 'packing_box', /release candidate was built/],
  ['M8-arthur-merges-papers-on-main', 'merge', 'merging_papers', /Merged to main/],
  ['M8-arthur-dances-op-done', 'finishop', 'dance', /Every task of op-003 is done/],
];
check('M8-arthur-greets-on-load', 'M8', 'Arthur waves once on load, then takes up the looping pose', async () => {
  await open('/hub/#cockpit');
  await arthurReady();
  const done = await until(`window.__actions.includes('greeting') && window.__arthur.getState().action==='waiting'`, 20000);
  const seq = await js('window.__actions');
  return {pass: done && seq[0] === 'greeting', info: seq.join(' > ')};
});
for (const [id, action, pose, why] of ARTHUR_TRIGGERS) {
  check(id, 'M8', `Arthur reacts to ${action}: ${pose}`, async () => {
    await open('/hub/#cockpit');
    await arthurReady();
    await until(`window.__arthur.getState().action==='waiting'`, 20000);
    await js(`window.__actions=[];window.__captions=[];new MutationObserver(()=>window.__captions.push(document.querySelector('#arthur-caption').textContent)).observe(document.querySelector('#arthur-caption'),{childList:true,characterData:true,subtree:true});true`);
    poke(action);
    const ok = await until(`window.__actions.includes(${JSON.stringify(pose)})`, 30000);
    const captions = await js('window.__captions');
    return {pass: ok && captions.some(c => why.test(c)), info: `${(await js('window.__actions')).join(' > ')}; captions ${[...new Set(captions)].join(' | ')}`};
  });
}
// Looping poses by priority (package 2, t-487): each scenario edits the overview's arthur block (and
// tasks or jobs) the page reads, so every rung of loopFor is seen live. To add a pose: one row here.
const AT_S = AT / 1000;
const clear = o => { const a = o.arthur; a.waiting = {questions: 0, briefs: 0, orders: 0}; return a; };
const calm = o => { const a = clear(o); a.research = 0; a.research_started = []; a.reading = {proofs: 0, reviews: 0}; a.thinking = {design: 0, selfchecks: 0}; a.verify = 0; o.active_jobs = []; return a; };
const noFlight = o => { for (const t of o.tasks || []) if (t.status === 'doing') t.status = 'open'; };
export const ARTHUR_LOOPS = [
  ['waiting', o => {}],
  ['headphones', o => { const a = clear(o); a.research = 1; a.research_started = [AT_S - 3600]; }],
  ['searching', o => { const a = clear(o); a.research = 1; a.research_started = [AT_S - 60]; }],
  ['drinking_coffee', o => { const a = calm(o); o.active_jobs = [{id: 'job-1', kind: 'build'}]; a.jobs_started = [AT_S - 7200]; }],
  ['juggling', o => { clear(o); const t = (o.tasks || []).find(x => x.status === 'doing' && x.lease_until > '2100'); for (let i = 0; i < 8; i++) o.tasks.push({...t, id: 't-9' + i}); }],
  ['reading', o => { const a = calm(o); a.reading = {proofs: 1, reviews: 0}; }],
  ['thinking', o => { const a = calm(o); a.thinking = {design: 1, selfchecks: 0}; }],
  ['checklist', o => { const a = calm(o); a.verify = 1; }],
  ['typing', o => { calm(o); }],
  ['sleeping', o => { const a = calm(o); noFlight(o); a.last_work = AT_S - 3 * 3600; }],
  ['idle', o => { const a = calm(o); noFlight(o); a.last_work = AT_S - 60; }],
];
for (const [pose, edit] of ARTHUR_LOOPS) {
  check('M7-arthur-loop-' + pose.replace(/_/g, '-'), 'M7', `Arthur loops ${pose} when the record calls for it`, async () => {
    await rewriteJSON('/hub/overview.json', edit);
    await open('/hub/#cockpit');
    await arthurReady();
    const ok = await until(`window.__arthur.getState().action===${JSON.stringify(pose)}`, 25000);
    return {pass: ok, info: `${(await js('window.__actions')).join(' > ')}; caption ${await js(`document.querySelector('#arthur-caption').textContent`)}`};
  });
}
check('M7-arthur-telescope-on-roadmap', 'M7', 'Arthur looks through the telescope while the Roadmap is open', async () => {
  await open('/hub/#cockpit');
  await arthurReady();
  await until(`window.__arthur.getState().action==='waiting'`, 20000);
  await js(`location.hash='#roadmap';true`);
  const on = await until(`window.__arthur.getState().action==='telescope'`, 20000);
  await js(`location.hash='#cockpit';true`);
  const off = await until(`window.__arthur.getState().action==='waiting'`, 20000);
  return {pass: on && off, info: `telescope ${on}, back to waiting ${off}; ${(await js('window.__actions')).join(' > ')}`};
});
check('M7-arthur-types-along', 'M7', 'Arthur types along while keys go into Find anything', async () => {
  await open('/hub/#cockpit');
  await arthurReady();
  await until(`window.__arthur.getState().action==='waiting'`, 20000);
  await js(`document.querySelector('#search-launch').click();true`);
  let seen = false;
  for (let i = 0; i < 12 && !seen; i++) { await b.send('Input.insertText', {text: 'ab'[i % 2]}); await sleep(120); seen = await js(`window.__arthur.getState().action==='mimic_typing'`); }
  const quiet = await until(`window.__arthur.getState().action!=='mimic_typing'`, 8000);
  return {pass: seen && quiet, info: `typing along ${seen}, stops when quiet ${quiet}`};
});
// One-shot page reactions of package 2: what the person does in the page, and two page-side reads.
check('M9-arthur-spits-on-rapid-clicks', 'M9', 'Ten quick clicks on Arthur make him spit', async () => {
  await open('/hub/#cockpit');
  await arthurReady();
  await until(`window.__arthur.getState().action==='waiting'`, 20000);
  const box = await js(`(()=>{const r=document.querySelector('#arthur').getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2];})()`);
  for (let i = 0; i < 10; i++) { await b.send('Input.dispatchMouseEvent', {type: 'mousePressed', x: box[0], y: box[1], button: 'left', clickCount: 1}); await b.send('Input.dispatchMouseEvent', {type: 'mouseReleased', x: box[0], y: box[1], button: 'left', clickCount: 1}); }
  const ok = await until(`window.__actions.includes('spit')`, 5000);
  return {pass: ok, info: (await js('window.__actions')).join(' > ')};
});
check('M9-arthur-theme-switch-poses', 'M9', 'A theme switch plays nightcap (to dark) and sunglasses (to light)', async () => {
  await open('/hub/#cockpit', {theme: 'light'});
  await arthurReady();
  await until(`window.__arthur.getState().action==='waiting'`, 20000);
  await click('[data-theme-control]');
  const night = await until(`window.__actions.includes('nightcap')`, 6000);
  await sleep(300);
  await click('[data-theme-control]');
  const sun = await until(`window.__actions.includes('sunglasses')`, 8000);
  return {pass: night && sun, info: (await js('window.__actions')).join(' > ')};
});
check('M9-arthur-yawns-on-return', 'M9', 'Back on the tab after five minutes away, Arthur yawns and stretches', async () => {
  await open('/hub/#cockpit');
  await arthurReady();
  await until(`window.__arthur.getState().action==='waiting'`, 20000);
  const vis = s => `Object.defineProperty(document,'visibilityState',{configurable:true,get:()=>'${s}'});Object.defineProperty(document,'hidden',{configurable:true,get:()=>${s === 'hidden'}});document.dispatchEvent(new Event('visibilitychange'));true`;
  await js(vis('hidden'));
  await js(`window.__clock.shiftDate(301000);true`);
  await js(vis('visible'));
  const ok = await until(`window.__actions.includes('yawn_stretch')`, 6000);
  return {pass: ok, info: (await js('window.__actions')).join(' > ')};
});
check('M9-arthur-peekaboo-on-sidebar-scroll', 'M9', 'Scrolling the sidebar after Arthur has rested a minute plays peekaboo', async () => {
  await rewriteJSON('/hub/overview.json', ARTHUR_LOOPS.find(r => r[0] === 'idle')[1]);
  await open('/hub/#cockpit');
  await arthurReady();
  await until(`window.__arthur.getState().action==='idle'`, 25000);
  await sleep(300);
  // a wheel turn that scrolls nothing plays nothing; a real scroll of the sidebar does
  await js(`window.__clock.shiftDate(61000);document.querySelector('#sidebar').dispatchEvent(new WheelEvent('wheel',{deltaY:40}));true`);
  await sleep(1500);
  const wheelOnly = await js(`window.__actions.includes('peekaboo')`);
  await js(`(()=>{const s=document.querySelector('#sidebar');s.style.maxHeight='300px';s.style.overflowY='auto';s.scrollTop=40;return true;})()`);
  const ok = await until(`window.__actions.includes('peekaboo')`, 6000);
  return {pass: !wheelOnly && ok, info: `wheel alone ${wheelOnly}; ` + (await js('window.__actions')).join(' > ')};
});
check('M9-arthur-counts-coins-on-cost-spike', 'M9', 'A fresh cost spike on the token board plays counting_coins', async () => {
  await rewriteJSON('/hub/burn.json', d => { const s = (d.sessions || [])[0]; if (s) { s.signals = s.signals || {}; s.signals.runaway = {...(s.signals.runaway || {}), bins: [{start: AT_S - 300}]}; } });
  await open('/hub/#sessions', {ready: HUB_READY + ` && !!document.querySelector('#analytics-index')`});
  await arthurReady();
  const ok = await until(`window.__actions.includes('counting_coins')`, 25000);
  return {pass: ok, info: (await js('window.__actions')).join(' > ')};
});
check('M9-arthur-party-hat-on-party-day', 'M9', 'A party day from project.yaml plays party_hat once', async () => {
  await rewriteJSON('/hub/overview.json', o => { o.arthur.party_days = [new Date(AT).toISOString().slice(0, 10)]; });
  await open('/hub/#cockpit');
  await arthurReady();
  const ok = await until(`window.__actions.includes('party_hat')`, 25000);
  return {pass: ok, info: (await js('window.__actions')).join(' > ')};
});
check('M9-arthur-gaze', 'M9', 'Arthur turns to the cursor and goes back to work when it stops', async () => {
  await open('/hub/#cockpit');
  await arthurReady();
  await until(`window.__arthur.getState().action==='waiting'`, 20000);
  for (let i = 0; i < 12; i++) { await mouse('mouseMoved', 300 + i * 70, 200 + i * 30); await sleep(60); }
  const looking = await until(`window.__arthur.getState().action==='idle' && window.__gaze.some(g=>g!==null)`, 4000);
  const back = await until(`window.__arthur.getState().action==='waiting'`, 8000);
  return {pass: looking && back, info: `looked ${looking} (gaze frames ${await js('window.__gaze.filter(g=>g!==null).length')}), back to work ${back}`};
});
check('M9-arthur-click-tricks', 'M9', 'A click on Arthur plays the next trick at once', async () => {
  await open('/hub/#cockpit');
  await arthurReady();
  await until(`window.__arthur.getState().action==='waiting'`, 20000);
  await js(`window.__actions=[];true`);
  await click('#arthur');
  const first = await until(`window.__arthur.getState().action==='celebrate'`, 4000);
  await click('#arthur');
  const second = await until(`window.__arthur.getState().action==='greeting'`, 4000);
  return {pass: first && second, info: (await js('window.__actions')).join(' > ')};
});
check('M9-arthur-paces', 'M9', 'Arthur paces across the slot on a page switch', async () => {
  await open('/hub/#cockpit');
  await arthurReady();
  await until(`window.__arthur.getState().action==='waiting'`, 20000);
  // the same page with a new query (a filter, a tab) is no page switch and does not pace
  await js(`window.__actions=[];location.hash='#cockpit?focus=1';true`);
  await sleep(1500);
  const sameWalk = await js(`window.__actions.filter(a=>a==='move-right'||a==='move-left').length`);
  await js(`window.__actions=[];location.hash='#overview';true`);
  const xs = await js(`window.__sample(()=>{const m=getComputedStyle(document.querySelector('#arthur')).transform;return m==='none'?0:Math.round(new DOMMatrix(m).m41);},10000,80)`);
  const acts = await js('window.__actions');
  const right = Math.max(...xs), left = Math.min(...xs);
  return {pass: sameWalk === 0 && right > 4 && left < -4 && xs.at(-1) === 0 && acts.includes('move-right') && acts.includes('move-left'), info: `same-page hash change paced ${sameWalk} times; x from ${left} to ${right}, ends ${xs.at(-1)}; ${acts.join(' > ')}`};
});
check('M10-roadmap-fold-kept', 'M10', 'Roadmap: a folded group animates its chevron and stays folded across a refresh and a page switch', async () => {
  // a workstream group of the task roadmap (the phase groups of the operation map keep their own state)
  const pick = `[...document.querySelectorAll('details[data-rm-group][open]')].find(d=>!d.dataset.rmGroup.startsWith('phase:')&&d.querySelector('[data-rm-node]'))`;
  await open('/hub/#roadmap', {ready: HUB_READY + ` && !!${pick}`});
  const key = await js(`${pick}.dataset.rmGroup`);
  const group = `[...document.querySelectorAll('details[data-rm-group]')].find(d=>d.dataset.rmGroup===${JSON.stringify(key)})`;
  await js(`${group}.querySelector(':scope > summary').setAttribute('data-motion-fold','1');true`);
  await click('[data-motion-fold]');
  const turned = await js(`[...(${group}.querySelector('.rm-chevron')?.getAnimations()||[])].map(a=>a.transitionProperty||a.animationName)`);
  await sleep(300);
  await js(`location.hash='#overview';true`);
  await until(`document.title.startsWith('Overview')`);
  await sleep(500);
  await js(`location.hash='#roadmap';true`);
  await until(`!!${group}`);
  await sleep(500);
  const open_ = await js(`${group}.open`);
  return {pass: turned.includes('transform') && open_ === false, info: `group ${key}: chevron ${turned.join(',') || 'no transition'}, open after switch ${open_}`};
});
check('M10-roadmap-arrows-redraw', 'M10', 'Roadmap: prerequisite arrows are redrawn when the layout changes', async () => {
  await open('/hub/#roadmap', {ready: HUB_READY + ` && document.querySelectorAll('[data-rm-edge]').length>0`});
  const d0 = await js(`[...document.querySelectorAll('[data-rm-edge]')].map(p=>p.getAttribute('d'))`);
  await b.send('Emulation.setDeviceMetricsOverride', {width: 1100, height: 900, deviceScaleFactor: 1, mobile: false});
  await sleep(1200);
  const d1 = await js(`[...document.querySelectorAll('[data-rm-edge]')].map(p=>p.getAttribute('d'))`);
  return {pass: d0.length > 0 && d1.length === d0.length && d1.some((d, i) => d !== d0[i]), info: `${d0.length} arrows, ${d1.filter((d, i) => d !== d0[i]).length} redrawn`};
});
check('M10-roadmap-card-focus', 'M10', 'Roadmap: focusing a chat dims the other cards with a fade', async () => {
  await open('/hub/#roadmap', {ready: HUB_READY + ` && !!document.querySelector('.rm-claims [data-rm-chat]')`});
  await click('.rm-claims [data-rm-chat]');
  const got = await js(`(()=>{const dim=[...document.querySelectorAll('.rm-node.rm-dimmed')];return {dimmed:dim.length,fading:dim.filter(n=>n.getAnimations().some(a=>a.transitionProperty==='opacity')).length,selected:document.querySelectorAll('.rm-node.rm-selected,.rm-node.rm-related').length};})()`);
  await sleep(500);
  const opacity = await js(`+getComputedStyle(document.querySelector('.rm-node.rm-dimmed')||document.body).opacity`);
  return {pass: got.dimmed > 0 && got.fading > 0 && opacity < 0.6, info: JSON.stringify({...got, opacity})};
});
check('M11-mission-redraw-on-resize', 'M11', 'Mission map draws its links and redraws them on resize', async () => {
  await open('/hub/#roadmap?view=mission', {ready: HUB_READY + ` && document.querySelectorAll('[data-mm-canvas] svg path').length>0`});
  const d0 = await js(`[...document.querySelectorAll('[data-mm-canvas] svg path')].map(p=>p.getAttribute('d'))`);
  await b.send('Emulation.setDeviceMetricsOverride', {width: 1100, height: 900, deviceScaleFactor: 1, mobile: false});
  await sleep(1200);
  const d1 = await js(`[...document.querySelectorAll('[data-mm-canvas] svg path')].map(p=>p.getAttribute('d'))`);
  return {pass: d0.length > 0 && d1.length === d0.length && d1.some((d, i) => d !== d0[i]), info: `${d0.length} paths, ${d1.filter((d, i) => d !== d0[i]).length} redrawn`};
});
check('M12-timeline-pulse', 'M12', 'The live timeline marks now with a pulse', async () => {
  await open('/hub/#activity', {ready: HUB_READY + ` && !!document.querySelector('.lt-now i, .vtl-now')`});
  const names = await js(`[...new Set(${animNames('.lt-now i, .vtl-now .vtl-node')})]`);
  return {pass: names.length > 0, info: names.join(',')};
});
check('M12-event-flash', 'M12', 'Picking a timeline event scrolls to it and flashes it', async () => {
  await open('/hub/#activity', {ready: HUB_READY + ` && !!document.querySelector('[data-timeline-event], .lt-dot')`});
  const id = await js(`[...document.querySelectorAll('[data-timeline-event]')].map(e=>e.dataset.timelineEvent).find(i=>document.getElementById('event-'+i))`);
  if (!id) return {pass: false, info: 'no timeline dot with a row on the page'};
  await js(`document.querySelector('[data-timeline-event="${id}"]').click();true`);
  await sleep(120);
  const got = await js(`(()=>{const r=document.getElementById('event-${id}');return {flash:r.classList.contains('event-flash'),names:r.getAnimations().map(a=>a.animationName)};})()`);
  return {pass: got.flash && got.names.includes('event-flash'), info: `event ${id}: ${JSON.stringify(got)}`};
});
check('M13-signin-globe-and-unlock', 'M13', 'Sign-in: the globe and rings move, the relay feed runs, unlock plays the takeover', async () => {
  await b.send('Network.clearBrowserCookies');
  await open('/login', {ready: `!!document.querySelector('#login-form') && document.querySelector('#globe').width>0`});
  await sleep(600);
  // still moving a second after load (the first frames also change while the coastlines arrive)
  await sleep(1000);
  const g1 = await js(canvasFrames('#globe', 400, 3000)), g2 = await js(canvasFrames('#globe', 400, 3000));
  const globe = {n: g1.n, changed: Math.min(g1.changed, g2.changed), blank: g1.blank};
  const relay0 = await js(`document.querySelector('#relay').textContent`);
  await sleep(2500);
  const relay1 = await js(`document.querySelector('#relay').textContent`);
  await js(`document.querySelector('#user').value=${JSON.stringify(user)};const c=document.querySelector('#code');c.value=${JSON.stringify(rest.join(':'))};c.dispatchEvent(new Event('input',{bubbles:true}));true`);
  await js(`document.querySelector('#login-form').requestSubmit();true`);
  const takeover = await until(`!!document.querySelector('.takeover')`, 10000);
  const landed = await until(`location.pathname==='/' || location.pathname.startsWith('/hub')`, 30000);
  return {pass: globe.changed >= 1 && globe.blank === 0 && relay0 !== relay1 && takeover && landed, info: `globe moved ${globe.changed}/${globe.n}, relay changed ${relay0 !== relay1}, takeover ${takeover}, landed ${landed}`};
});
check('M13-hub-tiles', 'M13', 'Workspace hub: the backdrop moves and the tiles poll their live summary', async () => {
  await open('/', {warp: 10, ready: `document.querySelectorAll('.tile').length>0 && !document.querySelector('.tile .skeleton')`});
  const backdrop = await js(canvasFrames('#backdrop', 400, 3000));
  const c0 = count('/board/data.json');
  await sleep(4000);
  const c1 = count('/board/data.json');
  return {pass: backdrop.changed >= 1 && c1 - c0 >= 2, info: `backdrop moved ${backdrop.changed}/${backdrop.n}, tile summary reads ${c0} -> ${c1}`};
});
check('L1-sse-rev-ping', 'L1', 'The event stream pushes one revision per surface and the hub acts on a change', async () => {
  // Work & evidence has no 15 s overview timer, so a reread there comes from the pushed revision alone.
  await open('/hub/#work');
  await until(`window.__revs.length>0`, 15000);
  await sleep(800);
  const first = JSON.parse(await js('window.__revs[0]'));
  const reads = count('/hub/overview.json');
  poke('task');
  const moved = await until(`window.__revs.length>1 && window.__revs.at(-1)!==window.__revs[0]`, 25000);
  const last = moved ? JSON.parse(await js('window.__revs.at(-1)')) : {};
  const reacted = await (async () => { const end = Date.now() + 12000; while (Date.now() < end) { if (count('/hub/overview.json') > reads) return true; await sleep(100); } return false; })();
  const keys = Object.keys(first).sort().join(',');
  return {pass: keys === 'board,data,lib,pricing' && moved && last.board !== first.board && reacted, info: `keys ${keys}; new revision ${moved}, board moved ${moved && last.board !== first.board}; hub reread the overview ${reacted} (${reads} -> ${count('/hub/overview.json')})`};
});
check('L2-per-part-refresh', 'L2', 'A revision refreshes only the pages that read its surface', async () => {
  // A new document moves only the library revision: the Library page rereads its documents, the
  // Sessions page (which reads sessions and analytics) rereads nothing.
  // Earlier checks' writes can still be folding on the server (its fold is throttled), so the stream is
  // let settle first, and a window in which the data revision moved anyway is measured once more.
  let d0, d1, i0, i1, libOnly;
  for (let attempt = 0; attempt < 2; attempt++) {
    await open('/hub/#sessions', {ready: HUB_READY + ` && !!document.querySelector('#analytics-index tbody tr')`});
    await until(`window.__revs.length>0`, 15000);
    for (let n = -1, t = Date.now(); Date.now() - t < 30000;) { const m = await js('window.__revs.length'); if (m === n) break; n = m; await sleep(4000); }
    const start = await js('window.__revs.length');
    d0 = count('/data.json'); i0 = count('/hub/analytics-index.json');
    poke('doc');
    await until(`window.__revs.length>${start}`, 25000);
    await sleep(3500);
    const revs = (await js(`window.__revs.slice(${start - 1})`)).map(x => JSON.parse(x));
    d1 = count('/data.json'); i1 = count('/hub/analytics-index.json');
    libOnly = revs.length > 1 && revs.at(-1).lib !== revs[0].lib && revs.every(r => r.data === revs[0].data && r.board === revs[0].board);
    if (libOnly) break;
  }
  await open('/hub/#library');
  await until(`window.__revs.length>0`, 15000);
  await sleep(800);
  const l0 = count('/hub/documents.json');
  poke('doc');
  const l = await until(`window.__revs.length>1`, 25000) && await (async () => { const end = Date.now() + 10000; while (Date.now() < end) { if (count('/hub/documents.json') > l0) return true; await sleep(100); } return false; })();
  return {pass: libOnly && d1 === d0 && i1 === i0 && l, info: `library-only revision ${libOnly}; sessions page: data.json ${d0} -> ${d1}, index ${i0} -> ${i1}; library page reread ${l}`};
});
check('L3-live-flag-generation', 'L3', 'A revision that lands while a refresh is reading is not lost', async () => {
  await open('/hub/#work');
  await until(`window.__revs.length>0`, 15000);
  await sleep(800);
  const count0 = await js(`document.querySelectorAll('#main [data-task]').length`);
  await hold('/hub/overview.json', {first: true});      // only the first read waits; later reads pass
  poke('task');
  const heldOne = await (async () => { const end = Date.now() + 20000; while (Date.now() < end) { if (held.length) return true; await sleep(100); } return false; })();
  const revs = await js('window.__revs.length');
  poke('task');
  await until(`window.__revs.length>${revs}`, 20000);
  await sleep(2500);
  // the held read predates the second task; release it, then let the reader go idle once
  await release();
  await sleep(2500);
  await js(`document.dispatchEvent(new FocusEvent('focusout'));true`);
  const want = count0 + 2;
  const ok = await until(`document.querySelectorAll('#main [data-task]').length>=${want}`, 12000);
  return {pass: heldOne && ok, info: `held a read ${heldOne}; tasks on page ${count0} -> ${await js(`document.querySelectorAll('#main [data-task]').length`)} (want ${want})`};
});
check('L4-overview-timer-and-live-orb', 'L4', 'The 15 s overview timer rereads in place and shows the refreshing orb', async () => {
  await open('/hub/#cockpit', {warp: 10});
  await js(`window.__orbShown=0;new MutationObserver(()=>{if(!document.querySelector('#live-orb').hidden)window.__orbShown++;}).observe(document.querySelector('#live-orb'),{attributes:true});true`);
  const c0 = count('/hub/overview.json');
  await sleep(4500);
  const c1 = count('/hub/overview.json');
  return {pass: c1 - c0 >= 2 && await js('window.__orbShown') > 0, info: `overview reads ${c0} -> ${c1}, live orb shown ${await js('window.__orbShown')} times`};
});
check('L4-topbar-clock-ticks', 'L4', 'The topbar date and time tick', async () => {
  // on a page without the overview timer, so only the clock's own tick repaints it
  await open('/hub/#work', {warp: 10});
  const texts = await js(`window.__sample(()=>document.querySelector('#checked-at .clock-time')?.textContent,7000,200)`);
  const distinct = [...new Set(texts.filter(Boolean))];
  return {pass: distinct.length >= 2, info: distinct.join(' > ')};
});
check('L4-new-activity-notice', 'L4', 'A change while the reader is busy shows the New activity notice, then refreshes when idle', async () => {
  await open('/hub/#work');
  await until(`window.__revs.length>0`, 15000);
  await sleep(600);
  await js(`(document.querySelector('#main input[data-filter]')||document.querySelector('#main input')).focus();true`);
  poke('task');
  const notice = await until(`document.querySelector('#checked-at .clock-notice')?.textContent==='New activity available' && !document.querySelector('#live-orb').hidden && document.querySelector('#live-orb').getAttribute('state')==='listening'`, 20000);
  await js(`document.activeElement.blur();true`);
  const cleared = await until(`!document.querySelector('#checked-at').dataset.notice && !!document.querySelector('#checked-at .clock-date')`, 15000);
  return {pass: notice && cleared, info: `notice ${notice}, cleared after idle ${cleared}`};
});
check('L5-connection-health', 'L5', 'Connection health every 10 s; the sidebar orb listens, and reconnects when the stream drops', async () => {
  await open('/hub/#work', {warp: 10});
  await until(`window.__es.length>0 && document.querySelector('#connection alpaca-orb').getAttribute('state')==='listening'`, 15000);
  const h0 = count('/hub/vitals.json');
  await sleep(3500);
  const h1 = count('/hub/vitals.json');
  await js(`window.__es[0].dispatchEvent(new Event('error'));true`);
  const lost = await until(`document.querySelector('#connection span').textContent==='Reconnecting' && document.querySelector('#connection alpaca-orb').getAttribute('state')==='connecting'`, 5000);
  await js(`window.__es[0].dispatchEvent(new Event('open'));true`);
  const back = await until(`document.querySelector('#connection alpaca-orb').getAttribute('state')==='listening'`, 5000);
  // the health bar under Arthur shows what the fixture server's own /hub/vitals.json says (t-590): its
  // label, aria-valuenow and band equal a fresh read of the endpoint, within one 10 s poll
  const meter = await until(`fetch('/hub/vitals.json').then(r=>r.json()).then(v=>{const m=document.querySelector('#health-meter');window.__vitals=v.score.value+' '+v.score.band;return m.getAttribute('aria-valuenow')===String(v.score.value)&&m.querySelector('.health-label').textContent==='Health '+v.score.value+'/1000'&&m.dataset.band===v.score.band;})`, 15000);
  const shown = await js(`document.querySelector('#health-meter .health-label').textContent+' '+document.querySelector('#health-meter').dataset.band`);
  return {pass: h1 - h0 >= 2 && lost && back && meter, info: `health reads ${h0} -> ${h1}, reconnecting shown ${lost}, back ${back}; meter ${shown} for vitals ${await js('window.__vitals')} ${meter}`};
});
check('L6-own-polls', 'L6', 'Pages with their own poll keep it: the cockpit host panel every 3 s', async () => {
  await open('/hub/#cockpit', {warp: 10});
  await until(`!!document.querySelector('.sys-head')`, 15000);
  const s0 = count('/hub/system.json');
  await sleep(2500);
  const s1 = count('/hub/system.json');
  return {pass: s1 - s0 >= 3, info: `host panel reads ${s0} -> ${s1} in 2.5 s at 10x`};
});
check('R1-reduced-motion', 'reduced motion', 'Reduced motion: still rings, beams and orbs, one shape change, Arthur in place', async () => {
  await open('/hub/#roadmap', {reduced: true, ready: HUB_READY + ` && !!document.querySelector('.beam')`});
  await arthurReady();
  const got = await js(`(async()=>{const el=document.querySelector('#pulse-orb');
    const ringsRunning=[...el.querySelectorAll('.pulse-ring')].filter(r=>!r.hidden&&getComputedStyle(r).display!=='none'&&r.getAnimations().length).length;
    const ringsShown=[...el.querySelectorAll('.pulse-ring')].filter(r=>!r.hidden&&getComputedStyle(r).display!=='none').length;
    const beamsRunning=[...document.querySelectorAll('.beam')].filter(b=>b.getAnimations({subtree:true}).length).length;
    return {ringsRunning,ringsShown,beamsRunning,arthurReduced:window.__arthur.getState().reducedMotion};})()`);
  const orbs = await js(canvasFrames('alpaca-orb', 400));
  const s0 = await js(`document.querySelector('#pulse-orb alpaca-orb').getAttribute('state')`);
  await js(`document.querySelector('#pulse-orb').click();true`);
  const wob = await js(`window.__sample(()=>[document.querySelector('#pulse-orb').style.getPropertyValue('--wob-sx'),document.querySelector('#pulse-orb alpaca-orb').getAttribute('state')],1200,60)`);
  const shapes = new Set(wob.map(w => w[1]).filter(s => s !== s0)).size;
  const wobbled = wob.some(w => w[0] !== '');
  const pass = got.ringsRunning === 0 && got.ringsShown === 1 && got.beamsRunning === 0 && got.arthurReduced && orbs.changed === 0 && shapes === 1 && !wobbled;
  return {pass, info: JSON.stringify({...got, orbsMoved: orbs.changed, orbs: orbs.n, shapeChanges: shapes, wobbled})};
});

// ---- run ---------------------------------------------------------------------------------------------
const results = [];
try {
  for (const m of ['Page.enable', 'Runtime.enable', 'Network.enable']) await b.send(m);
  await b.send('Emulation.setTimezoneOverride', {timezoneId: 'UTC'});
  await b.send('Emulation.setLocaleOverride', {locale: 'en-US'});
  await b.send('Network.setCacheDisabled', {cacheDisabled: true});
  await patterns();
  const signin = async () => {
    await navigate(b, base + '/login');
    const status = await b.js(`fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:${JSON.stringify(JSON.stringify({user, code: rest.join(':')}))}}).then(r=>r.status)`);
    if (status !== 200) throw Error('sign-in answered ' + status);
  };
  await signin();
  for (const c of CHECKS) {
    if (only && !only.includes(c.id)) continue;
    const started = Date.now();
    let r;
    try { r = await c.run(); } catch (e) { r = {pass: false, info: 'error: ' + e.message}; }
    if (holdPath) await release();
    if (rewrite) await rewriteJSON(null);
    if (c.id.startsWith('M13-signin')) await signin();
    results.push({id: c.id, row: c.row, name: c.name, pass: !!r.pass, info: r.info, ms: Date.now() - started});
    console.log(`${r.pass ? 'PASS' : 'FAIL'} ${c.id} [${c.row}] ${c.name} :: ${r.info}`);
  }
} catch (e) {
  console.log('FAIL driver ' + (e.stack || e.message));
  results.push({id: 'driver', row: '-', name: 'driver', pass: false, info: e.message});
} finally {
  await b.close();
}
if (mutation?.asset && !mutated) console.log('MUTATION NOT APPLIED: ' + assetPath(mutation.asset) + ' was never served');
if (outFile) writeFileSync(outFile, JSON.stringify({mutation: mutateId || null, mutated, results}, null, 1) + '\n');
process.exit(results.every(r => r.pass) ? 0 : 1);
