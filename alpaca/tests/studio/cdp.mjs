// Headless Chrome over the DevTools protocol, shared by the Studio capture (capture.mjs) and the
// motion and live checks (motion.mjs). No npm package: node's own WebSocket and fetch.
//
//   const b = await launch({chrome, profile, flags});
//   await b.send('Page.navigate', {url});  await b.js('document.title');  await b.until('expr');
//   b.on('Network.requestWillBeSent', fn);  await b.quiet();  await b.close();
//
// Chrome keeps its cookie key in a plain store and resolves no host but 127.0.0.1 (the flags the
// hub tests use, alpaca/tests/test_hub_live_refresh.py CHROME_FLAGS), so a run stays hermetic.
import {spawn} from 'node:child_process';
import {existsSync, readFileSync, rmSync} from 'node:fs';

export const sleep = ms => new Promise(r => setTimeout(r, ms));
export const BASE_FLAGS = ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--password-store=basic', '--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1',
  '--hide-scrollbars', '--force-color-profile=srgb', '--disable-lcd-text', '--font-render-hinting=none',
  '--disable-background-timer-throttling', '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows',
  '--disable-features=Translate,MediaRouter,OptimizationHints', '--mute-audio', '--remote-debugging-port=0',
  // raster the same way every time: no CPU-specific Skia paths, no partial or checker-imaged raster
  '--disable-skia-runtime-opts', '--disable-partial-raster', '--disable-checker-imaging', '--disable-gpu-compositing',
  '--run-all-compositor-stages-before-draw', '--disable-threaded-animation'];

// A loaded host can make Chrome miss its start; launch tries once more before it gives up.
export async function launch(options = {}) {
  try { return await launchOnce(options); } catch (e) {
    console.error('Chrome did not start (' + e.message + '); trying once more');
    try { rmSync(options.profile, {recursive: true, force: true}); } catch {}
    await sleep(2000);
    return launchOnce(options);
  }
}

async function launchOnce({chrome = process.env.CHROME || 'google-chrome', profile, flags = [], keepProfile = false} = {}) {
  if (!profile) throw Error('launch needs a profile directory');
  const proc = spawn(chrome, [...BASE_FLAGS, ...flags, '--user-data-dir=' + profile, '--window-size=1440,900', 'about:blank'],
    {stdio: 'ignore'});
  let port;
  for (let i = 0; i < 600 && !port; i++) {
    await sleep(100);
    if (existsSync(profile + '/DevToolsActivePort')) port = readFileSync(profile + '/DevToolsActivePort', 'utf8').split('\n')[0];
  }
  if (!port) { proc.kill('SIGKILL'); throw Error('Chrome did not open its DevTools port'); }
  let target;
  for (let i = 0; i < 300 && !target; i++) {
    try { target = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(t => t.type === 'page'); } catch {}
    if (!target) await sleep(100);
  }
  if (!target) { proc.kill('SIGKILL'); throw Error('Chrome opened no page target'); }
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
  let seq = 0;
  const pending = new Map(), handlers = new Map();
  ws.onmessage = m => {
    const d = JSON.parse(m.data);
    if (d.id && pending.has(d.id)) { pending.get(d.id)(d); pending.delete(d.id); return; }
    for (const fn of [...(handlers.get(d.method) || [])]) { try { fn(d.params || {}); } catch {} }
  };
  const send = (method, params = {}) => new Promise(r => { const n = ++seq; pending.set(n, r); ws.send(JSON.stringify({id: n, method, params})); });
  const js = async (expr, {await: wait = true} = {}) => {
    const r = await send('Runtime.evaluate', {expression: expr, awaitPromise: wait, returnByValue: true});
    if (r.error) throw Error(r.error.message);
    if (r.result.exceptionDetails) throw Error(expr.slice(0, 100) + ': ' + JSON.stringify(r.result.exceptionDetails).slice(0, 400));
    return r.result.result.value;
  };
  const until = async (expr, ms = 20000, every = 100) => {
    const end = Date.now() + ms;
    while (Date.now() < end) { try { if (await js(expr)) return true; } catch {} await sleep(every); }
    return false;
  };
  // Returns a function that removes the handler again.
  const on = (method, fn) => {
    if (!handlers.has(method)) handlers.set(method, []);
    handlers.get(method).push(fn);
    return () => { const list = handlers.get(method) || []; const i = list.indexOf(fn); if (i >= 0) list.splice(i, 1); };
  };

  // Requests in flight, without the event stream (it never ends) and without data: URLs.
  const inflight = new Map();
  let lastChange = Date.now();
  on('Network.requestWillBeSent', p => {
    if (p.type === 'EventSource' || /^data:|^blob:|\/favicon\.ico$/.test(p.request?.url || '')) return;
    inflight.set(p.requestId, p.request.url); lastChange = Date.now();
  });
  const done = p => { if (inflight.delete(p.requestId)) lastChange = Date.now(); };
  on('Network.loadingFinished', done);
  on('Network.loadingFailed', done);
  on('Network.responseReceived', p => { if (p.type === 'EventSource' || /text\/event-stream/.test(p.response?.mimeType || '')) done(p); });
  // Resolves once no request has been in flight for `idle` ms (or after `max` ms, returning false).
  const quiet = async (idle = 500, max = 20000) => {
    const end = Date.now() + max;
    while (Date.now() < end) {
      if (!inflight.size && Date.now() - lastChange >= idle) return true;
      await sleep(50);
    }
    if (process.env.CDP_DEBUG) console.error('not quiet:', [...inflight.values()].join(' '));
    return false;
  };
  const close = async () => {
    try { ws.close(); } catch {}
    proc.kill('SIGTERM');
    await new Promise(r => { const t = setTimeout(() => { try { proc.kill('SIGKILL'); } catch {} r(); }, 3000); proc.once('exit', () => { clearTimeout(t); r(); }); });
    if (!keepProfile) { try { rmSync(profile, {recursive: true, force: true}); } catch {} }
  };
  return {send, js, until, on, quiet, close, inflight, proc};
}

// Navigate and wait for the load event of the new document.
export async function navigate(b, url, ms = 30000) {
  b.inflight.clear();                // requests of the page being left never report an end
  let off = () => {};
  const loaded = new Promise(r => { const t = setTimeout(r, ms); off = b.on('Page.loadEventFired', () => { clearTimeout(t); r(); }); });
  await b.send('Page.navigate', {url});
  await loaded;
  off();
}
