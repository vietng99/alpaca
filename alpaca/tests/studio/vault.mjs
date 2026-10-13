// The Records page in headless Chrome against the Studio fixture (alpaca/tests/test_evidence_graph.py):
// a search box asks the server once typing stops or on Enter, and keeps the page on screen meanwhile;
// the Wiki tab's Open vault draws the evidence graph, finds and selects an item and keeps it in the
// address (the wide Open vault card under the wiki summary swings two doors open), its Firing and Speed
// sliders drive the pulses and are kept, the Browse tray narrows tier by tier, a legend chip highlights its sector
// and hides nothing, the Brain mode draws, turns, zooms, keeps its Turn speed, lights a highlighted region
// and goes full screen; a double-click sends a wave; Open vault plays the vault entry; tooltips are the
// Studio's own; Close vault returns to search mode.
//   node alpaca/tests/studio/vault.mjs --base URL --root FIXTURE --profile DIR --out results.json
import {readFileSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {launch, navigate, sleep} from './cdp.mjs';

const argv = process.argv.slice(2);
const arg = name => { const i = argv.indexOf('--' + name); return i < 0 ? null : argv[i + 1]; };
const base = arg('base'), root = arg('root'), out = arg('out'), profile = arg('profile');
const [user, ...rest] = readFileSync(join(root, '.studio-fixture', 'auth'), 'utf8').trim().split(':');
const READY = `!!document.querySelector('#main h1,#main .page-heading') && !document.querySelector('#main .loading')`;
const results = {};
const check = (name, ok, detail) => { results[name] = {ok: !!ok, detail}; };

const b = await launch({profile});
const requests = [];
b.on('Network.requestWillBeSent', p => { if (p.request.url.includes('/hub/')) requests.push({url: p.request.url, at: Date.now()}); });
for (const m of ['Page.enable', 'Runtime.enable', 'Network.enable']) await b.send(m);
await b.send('Emulation.setDeviceMetricsOverride', {width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false});
await navigate(b, base + '/login');
const status = await b.js(`fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:${JSON.stringify(JSON.stringify({user, code: rest.join(':')}))}}).then(r=>r.status)`);
if (status !== 200) throw Error('sign-in answered ' + status);
const type = async text => { for (const ch of text) { await b.send('Input.insertText', {text: ch}); await sleep(150); } };
const asked = (path, since, q) => requests.filter(r => r.at >= since && r.url.includes(path) && (!q || new URL(r.url).searchParams.get('q') === q)).length;

try {
  // 0. every tooltip is the Studio's own: hovering a titled control shows the styled tip, no title left
  await navigate(b, base + '/hub/#records?section=ops');
  await b.until(READY, 30000, 100);
  const tbox = await b.js(`(()=>{const e=document.querySelector('#search-launch');const r=e.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2,title:e.getAttribute('title')};})()`);
  await b.send('Input.dispatchMouseEvent', {type: 'mouseMoved', x: tbox.x, y: tbox.y});
  await sleep(600);
  const tipped = await b.js(`(()=>{const t=document.querySelector('.studio-tip'),e=document.querySelector('#search-launch');return {shown:!!t&&!t.hidden,text:t?.textContent||'',title:e.getAttribute('title'),tip:e.dataset.tip||''};})()`);
  check('tooltips-are-the-studio-tip', tipped.shown && tipped.text === tbox.title && tipped.title === null && tipped.tip === tbox.title, {tbox, tipped});
  await b.send('Input.dispatchMouseEvent', {type: 'mouseMoved', x: 2, y: 600});

  // 1. typing asks nothing until it stops, then asks once, and the page stays on screen
  await navigate(b, base + '/hub/#records?section=proofs');
  await b.until(READY, 30000, 100);
  await b.js(`document.querySelector('input[data-filter="q"]').focus()`);
  await b.js(`window.__skeleton=0;new MutationObserver(()=>{if(document.querySelector('#main .loading'))window.__skeleton++;}).observe(document.querySelector('#main'),{childList:true,subtree:true});1`);
  const t0 = Date.now();
  await type('mod');
  const during = asked('/hub/records.json', t0);
  await sleep(1600);
  const after = asked('/hub/records.json', t0), once = asked('/hub/records.json', t0, 'mod');
  const skeleton = await b.js('window.__skeleton');
  const focused = await b.js(`document.activeElement?.dataset?.filter||''`);
  check('search-waits-for-typing-to-stop', during === 0 && after === 1 && once === 1, {during, after, once});
  check('search-keeps-the-page-on-screen', skeleton === 0 && focused === 'q', {skeleton, focused});
  // 2. Enter asks at once
  const t1 = Date.now();
  await b.send('Input.insertText', {text: 'e'});
  await b.send('Input.dispatchKeyEvent', {type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13});
  await b.send('Input.dispatchKeyEvent', {type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13});
  await sleep(350);
  check('search-enter-asks-at-once', asked('/hub/records.json', t1, 'mode') === 1, {asked: asked('/hub/records.json', t1)});
  await sleep(1200);
  check('search-enter-asks-only-once', asked('/hub/records.json', t1) === 1, {asked: asked('/hub/records.json', t1)});

  // 3. the Wiki tab shows the vault seal in place under the wiki's summary; it waits for its dial (no
  // auto-open), and the dial opens the vault without a second entry (t-602)
  await navigate(b, base + '/hub/#records?section=wiki');
  await b.until(READY, 30000, 100);
  const sealUp = await b.until(`!!document.querySelector('.rec-seal.is-sealed .ve .ve-dialwrap')`, 10000, 50);
  const seal = await b.js(`(()=>{const d=document.querySelector('.rec-seal'),r=d.getBoundingClientRect(),p=d.previousElementSibling;return {title:d.querySelector('.rec-door-title')?.textContent||'',below:!!p&&p.matches('.rec-panel,.notice')&&p.getBoundingClientRect().bottom<=r.top,h:Math.round(r.height),w:Math.round(r.width)};})()`);
  const searchMode = await b.js(`!!document.querySelector('input[data-filter="subject"]') && !document.querySelector('#evidence-graph')`);
  check('wiki-search-mode-offers-open-vault', sealUp && seal.title === 'Open vault' && seal.below && seal.h >= 400 && seal.w >= 600 && searchMode, {seal, searchMode});
  await sleep(3500);
  const waits = await b.js(`({await:!!document.querySelector('.rec-seal .ve-dialwrap.is-await'),hash:location.hash,lines:[...document.querySelectorAll('.rec-seal .ve-line')].map(e=>e.textContent).join('|')})`);
  check('the-seal-waits-for-its-dial', waits.await && !waits.hash.includes('vault=open') && /ALPACA VAULT/.test(waits.lines) && /OPERATIONS/.test(waits.lines), waits);
  const t2 = Date.now();
  await b.js(`document.querySelector('.rec-seal .ve-dialwrap').click();1`);
  const drawn = await b.until(`location.hash.includes('vault=open') && !!document.querySelector('#evidence-graph canvas') && document.querySelector('#evidence-graph canvas').width > 0`, 30000, 100);
  const heads = await b.js(`document.querySelector('.eg-canvas')?.getAttribute('aria-label')||''`);
  check('open-vault-draws-every-column', drawn && /evidence/.test(heads) && /sealed proofs/.test(heads) && /tasks/.test(heads) && /operations/.test(heads) && /decisions/.test(heads) && /wiki claims/.test(heads) && asked('/hub/graph.json', t2) === 1,
        {heads, graph: asked('/hub/graph.json', t2)});
  const closeText = await b.js(`document.querySelector('.rec-vault-switch')?.textContent||''`);
  check('open-vault-offers-close-vault', closeText === 'Close vault', {closeText});
  await sleep(600);
  const vaultUi = await b.js(`({second:!!document.querySelector('.ve'),tray:document.querySelector('.eg-tray').hidden,label:document.querySelector('[data-eg-act=tray]').textContent,back:document.querySelector('[data-eg-act=back]').hidden})`);
  check('open-vault-has-no-second-entry-and-browse-starts-folded', !vaultUi.second && vaultUi.tray && vaultUi.label === 'Browse' && vaultUi.back, vaultUi);

  // 4. find waits for typing to stop, Enter selects the first match and the address keeps it
  const graph = await b.js(`fetch('/hub/graph.json').then(r=>r.json()).then(g=>{const c=g.columns.map(x=>x.key);return {task:(g.nodes.find(n=>c[n.c]==='task')||{}).id, keys:c, kinds:g.links.every(l=>typeof l[2]==='string'&&l[2])};})`);
  check('graph-has-the-owner-column-order', graph.keys.length >= 10 && graph.keys.slice(0, 5).join() === 'op,task,decision,proof,ev-event' && graph.kinds, graph);
  await b.js(`document.querySelector('[data-eg-find]').focus();1`);
  await type(graph.task);
  const early = await b.js(`document.querySelector('.eg-found').textContent`);
  await sleep(1100);
  const later = await b.js(`document.querySelector('.eg-found').textContent`);
  check('vault-find-waits-for-typing-to-stop', early === '' && /match/.test(later), {early, later});
  await b.send('Input.dispatchKeyEvent', {type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13});
  await b.send('Input.dispatchKeyEvent', {type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13});
  const shown = await b.until(`(document.querySelector('.eg-id')?.textContent||'')===${JSON.stringify(graph.task)} && !!document.querySelector('.eg-facts')`, 15000, 100);
  const hash = await b.js('location.hash');
  check('vault-selects-and-keeps-the-item', shown && hash.includes('item=' + graph.task), {hash});
  const recorded = await b.js(`[...document.querySelectorAll('.eg-facts dt')].map(e=>e.textContent)`);
  check('vault-item-names-its-record-event', recorded.includes('Recorded'), {recorded});

  // 4b. Browse narrows tier by tier: operations, an operation's items by kind, a task's items; crumbs step back
  await b.js(`document.querySelector('[data-eg-find]').dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));1`);
  await sleep(200);
  const root = await b.js(`({ops:[...document.querySelectorAll('.eg-tray .eg-group')].map(g=>g.dataset.col),cards:document.querySelectorAll('.eg-tray .eg-card').length,crumbs:document.querySelectorAll('.eg-crumb').length})`);
  check('browse-starts-at-the-operations', root.ops.join() === '0' && root.cards >= 1 && root.crumbs === 1, root);
  await b.js(`document.querySelector('.eg-tray .eg-card[data-more]').click();1`);
  await sleep(300);
  const opTier = await b.js(`({crumbs:[...document.querySelectorAll('.eg-crumb')].map(e=>e.textContent),cols:[...document.querySelectorAll('.eg-tray .eg-group')].map(g=>g.dataset.col),sel:document.querySelector('.eg-id')?.textContent||''})`);
  check('browse-opens-an-operation', opTier.crumbs.length === 2 && opTier.cols.includes('1') && !opTier.cols.includes('0'), opTier);
  await b.js(`document.querySelector('.eg-tray .eg-group[data-col="1"] .eg-card').click();1`);
  await sleep(300);
  const taskTier = await b.js(`({crumbs:document.querySelectorAll('.eg-crumb').length,on:!!document.querySelector('.eg-card.on')||document.querySelectorAll('.eg-tray .eg-card').length>=0,id:document.querySelector('.eg-crumb[aria-current="true"]')?.textContent||''})`);
  check('browse-opens-a-task', taskTier.crumbs === 3 && /^t-/.test(taskTier.id), taskTier);
  await b.js(`document.querySelector('[data-eg-act=back]').click();1`);
  const backed = await b.js(`({crumbs:document.querySelectorAll('.eg-crumb').length,back:!document.querySelector('[data-eg-act=back]').hidden,cols:[...document.querySelectorAll('.eg-tray .eg-group')].map(g=>g.dataset.col)})`);
  check('browse-back-steps-up-a-tier', backed.crumbs === 2 && backed.back && backed.cols.includes('1'), backed);
  await b.js(`document.querySelector('[data-eg-crumb="0"]').click();1`);
  check('browse-crumb-steps-back', await b.js(`document.querySelectorAll('.eg-crumb').length===1&&!!document.querySelector('.eg-tray .eg-group[data-col="0"]')&&document.querySelector('[data-eg-act=back]').hidden`), {});
  await b.js(`document.querySelector('[data-eg-act=tray]').click();1`);
  const hidden = await b.js(`({hidden:document.querySelector('.eg-tray').hidden,label:document.querySelector('[data-eg-act=tray]').textContent,kept:localStorage.getItem('alpaca.vault.tray')})`);
  await b.js(`document.querySelector('[data-eg-act=tray]').click();1`);
  check('browse-list-hides-and-keeps-it', hidden.hidden && hidden.label === 'Browse' && hidden.kept === 'closed', hidden);

  // 5. the Firing and Speed sliders: 100 fires every link, the button toggles, the settings are kept
  await b.js(`(()=>{const f=document.querySelector('[data-eg-fire]');f.value=100;f.dispatchEvent(new Event('input',{bubbles:true}));const s=document.querySelector('[data-eg-speed]');s.value=250;s.dispatchEvent(new Event('input',{bubbles:true}));return 1})()`);
  const motion = await b.js(`({fire:document.querySelector('[data-eg-fire-out]').textContent,speed:document.querySelector('[data-eg-speed-out]').textContent,button:document.querySelector('[data-eg-act=all]').textContent,kept:[localStorage.getItem('alpaca.vault.fire'),localStorage.getItem('alpaca.vault.speed')]})`);
  check('vault-fires-every-link-at-100', motion.fire === '100%' && motion.speed === '2.5x' && motion.button === 'Calm' && motion.kept[0] === '100' && motion.kept[1] === '250', motion);
  const frames = await b.js(`new Promise(r=>{let n=0;const t0=performance.now();const s=()=>{n++;if(performance.now()-t0<1000)requestAnimationFrame(s);else r(n);};requestAnimationFrame(s);})`);
  check('vault-keeps-drawing-while-firing', frames >= 20, {frames});
  await b.js(`document.querySelector('[data-eg-act=all]').click();1`);
  const calm = await b.js(`document.querySelector('[data-eg-fire-out]').textContent`);
  check('vault-calm-returns-to-the-earlier-share', calm !== '100%' && calm !== '0%', {calm});

  // 6. a legend chip highlights its sector, hides nothing, and Clear highlight ends it
  const lines = await b.js(`document.querySelectorAll('[data-eg-col]').length`);
  await b.js(`document.querySelector('[data-eg-col="5"]').click();1`);
  const hl = await b.js(`({pressed:document.querySelector('[data-eg-col="5"]').getAttribute('aria-pressed'),focus:document.querySelector('.eg').dataset.focus,clear:!document.querySelector('[data-eg-act=unfocus]').hidden,heads:document.querySelectorAll('[data-eg-col]').length,kept:localStorage.getItem('alpaca.vault.hidden')})`);
  check('legend-highlights-a-sector', hl.pressed === 'true' && hl.focus === 'on' && hl.clear && hl.heads === lines && !hl.kept, {...hl, lines});
  await b.js(`document.querySelector('[data-eg-act=unfocus]').click();1`);
  check('legend-clear-highlight', await b.js(`document.querySelector('[data-eg-col="5"]').getAttribute('aria-pressed')==='false'&&document.querySelector('.eg').dataset.focus==='off'&&document.querySelector('[data-eg-act=unfocus]').hidden`), {});

  // 7. Brain mode: drawn in 3D, turned by a drag, zoomed by the wheel, full screen, mode kept
  await b.js(`(()=>{const f=document.querySelector('[data-eg-fire]');f.value=0;f.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('[data-eg-mode=brain]').click();return 1})()`);
  const brainUp = await b.until(`(()=>{const c=document.querySelector('.eb-canvas');return !!c&&!c.hidden&&c.width>0&&document.querySelector('.eg-canvas').hidden})()`, 15000, 100);
  if (await b.js(`document.querySelector('[data-eg-act=spin]').getAttribute('aria-pressed')==='true'`)) await b.js(`document.querySelector('[data-eg-act=spin]').click();1`);
  // the brain in view: with the Browse list open its centre can sit below the window, out of the mouse's reach
  await b.js(`document.querySelector('.eb-canvas').scrollIntoView({block:'center'});1`);
  await sleep(400);
  const shot = `(()=>{const c=document.querySelector('.eb-canvas'),d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;let h=0,lit=0;for(let i=0;i<d.length;i+=16){h=(h*31+d[i]+d[i+1]*7+d[i+2]*13)>>>0;}const bg=[d[0],d[1],d[2]];let x0=1e9,x1=-1;for(let i=0;i<d.length;i+=16){if(Math.abs(d[i]-bg[0])+Math.abs(d[i+1]-bg[1])+Math.abs(d[i+2]-bg[2])>60){lit++;const x=(i/4)%c.width;if(x<x0)x0=x;if(x>x1)x1=x;}}return {h,lit,span:x1-x0};})()`;
  const before = await b.js(shot);
  check('brain-draws-neurons', brainUp && before.lit > 500, {brainUp, before});
  const box = await b.js(`(()=>{const r=document.querySelector('.eb-canvas').getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};})()`);
  await b.send('Input.dispatchMouseEvent', {type: 'mousePressed', x: box.x, y: box.y, button: 'left', clickCount: 1});
  for (let k = 1; k <= 8; k++) await b.send('Input.dispatchMouseEvent', {type: 'mouseMoved', x: box.x + k * 20, y: box.y + k * 4, button: 'left', buttons: 1});
  await b.send('Input.dispatchMouseEvent', {type: 'mouseReleased', x: box.x + 160, y: box.y + 32, button: 'left', clickCount: 1});
  await sleep(600);                        // past the brain's settle (it repaints in full 180 ms after the last input)
  const turned = await b.js(shot);
  check('brain-turns-on-drag', turned.h !== before.h, {before: before.h, after: turned.h});
  await b.send('Input.dispatchMouseEvent', {type: 'mouseWheel', x: box.x, y: box.y, deltaX: 0, deltaY: -240});
  await sleep(600);
  const zoomed = await b.js(shot);
  check('brain-zooms-on-wheel', zoomed.h !== turned.h && zoomed.span > turned.span, {turned, zoomed});
  await b.js(`(()=>{const t=document.querySelector('[data-eg-turn]');t.value=250;t.dispatchEvent(new Event('input',{bubbles:true}));return 1})()`);
  const turn = await b.js(`({shown:!document.querySelector('[data-eg-turn]').closest('label').hidden,out:document.querySelector('[data-eg-turn-out]').textContent,kept:localStorage.getItem('alpaca.vault.turn'),spin:document.querySelector('[data-eg-act=spin]').getAttribute('aria-pressed')})`);
  check('brain-turn-speed-slider', turn.shown && turn.out === '2.5x' && turn.kept === '250' && turn.spin === 'true', turn);
  // the arrow keys and WASD move the brain while held (t-605)
  await b.js(`document.querySelector('.eb-canvas').focus();1`);
  const beforeKeys = await b.js(shot);
  await b.send('Input.dispatchKeyEvent', {type: 'keyDown', key: 'd', code: 'KeyD', windowsVirtualKeyCode: 68});
  await sleep(400);
  await b.send('Input.dispatchKeyEvent', {type: 'keyUp', key: 'd', code: 'KeyD', windowsVirtualKeyCode: 68});
  await sleep(600);
  const afterKeys = await b.js(shot);
  check('brain-moves-on-wasd', afterKeys.h !== beforeKeys.h, {before: beforeKeys.h, after: afterKeys.h});
  // the Brightness slider (t-603) dims and brightens the brain and is kept
  await b.js(`(()=>{const t=document.querySelector('[data-eg-turn]');t.value=0;t.dispatchEvent(new Event('input',{bubbles:true}));return 1})()`);
  await sleep(500);
  const dimmer = async v => { await b.js(`(()=>{const g=document.querySelector('[data-eg-glow]');g.value=${v};g.dispatchEvent(new Event('input',{bubbles:true}));return 1})()`); await sleep(600); return b.js(shot); };
  const lowLux = await dimmer(30), highLux = await dimmer(180);
  const luxUi = await b.js(`({out:document.querySelector('[data-eg-glow-out]').textContent,kept:localStorage.getItem('alpaca.vault.glow'),shown:!document.querySelector('[data-eg-glow]').closest('label').hidden})`);
  check('brain-brightness-slider', lowLux.h !== highLux.h && luxUi.out === '180%' && luxUi.kept === '180' && luxUi.shown, {luxUi, low: lowLux.h, high: highLux.h});
  await dimmer(70);
  await b.js(`(()=>{const t=document.querySelector('[data-eg-turn]');t.value=0;t.dispatchEvent(new Event('input',{bubbles:true}));return 1})()`);
  await sleep(1200);
  const calmBrain = await b.js(shot);
  await b.js(`document.querySelector('[data-eg-col="1"]').click();1`);
  await sleep(1500);
  const litBrain = await b.js(shot);
  check('brain-highlights-a-region', litBrain.h !== calmBrain.h && (await b.js(`document.querySelector('.eg').dataset.focus`)) === 'on', {calmBrain, litBrain});
  await b.js(`document.querySelector('[data-eg-act=unfocus]').click();1`);
  // a double-click sends a shockwave from the cursor; the still brain returns to the same frame after it
  await b.js(`(()=>{const c=document.querySelector('[data-eg-act=clear]');if(!c.disabled)c.click();return 1})()`);
  let still = await b.js(shot);
  for (let k = 0; k < 30; k++) { await sleep(800); const again = await b.js(shot); if (again.h === still.h) break; still = again; }
  const wbox = await b.js(`(()=>{const r=document.querySelector('.eb-canvas').getBoundingClientRect();return {x:r.left+12,y:r.top+12};})()`);
  for (const c of [1, 2]) { await b.send('Input.dispatchMouseEvent', {type: 'mousePressed', x: wbox.x, y: wbox.y, button: 'left', clickCount: c}); await b.send('Input.dispatchMouseEvent', {type: 'mouseReleased', x: wbox.x, y: wbox.y, button: 'left', clickCount: c}); }
  await sleep(220);
  const mid = await b.js(shot);
  await sleep(2000);
  const settled = await b.js(shot);
  const picked = await b.js(`!document.querySelector('[data-eg-act=clear]').disabled`);
  check('brain-double-click-sends-a-wave', mid.h !== still.h && settled.h === still.h, {still: still.h, mid: mid.h, settled: settled.h, picked});
  await b.js(`document.querySelector('[data-eg-act=full]').click();1`);
  await sleep(400);
  const full = await b.js(`({on:document.fullscreenElement===document.querySelector('.eg')||document.querySelector('.eg').classList.contains('eg-max'),label:document.querySelector('[data-eg-act=full]').textContent})`);
  check('brain-goes-full-screen', full.on && full.label === 'Exit full screen', full);
  await b.js(`document.querySelector('[data-eg-act=full]').click();1`);
  await sleep(300);
  check('brain-leaves-full-screen', await b.js(`!document.fullscreenElement&&!document.querySelector('.eg').classList.contains('eg-max')`), {});
  await b.js(`(()=>{const f=document.querySelector('[data-eg-fire]');f.value=100;f.dispatchEvent(new Event('input',{bubbles:true}));return 1})()`);
  const brainFrames = await b.js(`new Promise(r=>{let n=0;const t0=performance.now();const s=()=>{n++;if(performance.now()-t0<1000)requestAnimationFrame(s);else r(n);};requestAnimationFrame(s);})`);
  check('brain-fires-and-keeps-drawing', brainFrames >= 15 && (await b.js(`localStorage.getItem('alpaca.vault.mode')`)) === 'brain', {brainFrames});
  await b.js(`document.querySelector('[data-eg-mode=columns]').click();1`);
  check('columns-mode-returns', await b.js(`!document.querySelector('.eg-canvas').hidden&&document.querySelector('.eb-canvas').hidden&&localStorage.getItem('alpaca.vault.mode')==='columns'`), {});

  // 8. Close vault returns to search mode
  await b.js(`document.querySelector('.rec-vault-switch').click();1`);
  const back = await b.until(`!!document.querySelector('input[data-filter="subject"]') && !document.querySelector('#evidence-graph')`, 30000, 100);
  check('close-vault-returns-to-search-mode', back, {});
} catch (e) {
  check('script', false, String(e && e.stack || e));
}
writeFileSync(out, JSON.stringify(results, null, 2));
await b.close();
process.exit(0);
