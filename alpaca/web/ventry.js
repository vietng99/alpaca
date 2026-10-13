// The vault entry (op-012 t-598): Open vault plays a two-act lock over the vault before it shows,
// modelled on the war-room entry of the owner's Rune dashboard (Drop-in/view.js, mountSeal).
// ACT 1 (about 1 s, t-600): the status decrypts, the record's telemetry types out beside a combination
// dial, the key slots fill and roll hex, and three concentric glyph rings and the two door
// inscriptions align glyph by glyph while the dial's arc arms; the vault then parks at AWAIT, the
// wheels idling and the dial beckoning. ACT 2 (the hand: press the dial, Enter, or wait
// AWAIT_AUTO_MS): ACCESS GRANTED, the counter-rotating rings ramp from slow to a streaking blur, the
// unlock cascade opens the rings inner to outer (a back-snap, a flash at the ring's radius, an
// expand-and-dissolve), the four seam bolts draw, the seam flares and the doors part onto the vault.
// A click during act 1 jumps to AWAIT; a double-click or Escape opens at once; a watchdog always
// opens. Reduced motion skips the entry. Transform and opacity only; ventry.css holds the look.
const HEX = '0123456789ABCDEF';
// Elder Futhark, the Rune dashboard's own glyph set (written as escapes: the source stays ASCII)
const FUTHARK = [...'\u16A0\u16A2\u16A6\u16A8\u16B1\u16B2\u16B7\u16B9\u16BA\u16BE\u16C1\u16C3\u16C7\u16C8\u16C9\u16CA\u16CF\u16D2\u16D6\u16D7\u16DA\u16DC\u16DE\u16DF'];
const SEAL = [...'\u16A0\u16A2\u16A6\u16A8\u16B1\u16B2\u16B7\u16B9\u16BA\u16BE\u16C1\u16CF\u16D2\u16D6\u16D7\u16DF'];
export const AWAIT_AUTO_MS = 700;                          // AWAIT opens on its own after this, if no hand

const pad = (label, value) => (label + ' ').padEnd(16, '.') + ' ' + value;

// Text that resolves out of random glyphs, left to right, over `ms`.
function decrypt(el, text, ms) {
  const t0 = performance.now();
  const step = now => {
    if (!el.isConnected) return;
    const p = Math.min(1, (now - t0) / ms), n = Math.floor(p * text.length);
    el.textContent = text.slice(0, n) + [...text.slice(n)].map(c => c === ' ' ? ' ' : HEX[(Math.random() * 16) | 0]).join('');
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function dial() {
  let ticks = '', spokes = '', tumb = '';
  for (let i = 0; i < 36; i++) {
    const a = i / 36 * Math.PI * 2, r2 = i % 3 === 0 ? 43 : 46;
    ticks += `<line x1="${(60 + Math.cos(a) * 50).toFixed(1)}" y1="${(60 + Math.sin(a) * 50).toFixed(1)}" x2="${(60 + Math.cos(a) * r2).toFixed(1)}" y2="${(60 + Math.sin(a) * r2).toFixed(1)}"/>`;
  }
  for (let i = 0; i < 6; i++) { const a = i / 6 * Math.PI * 2; spokes += `<line x1="60" y1="60" x2="${(60 + Math.cos(a) * 33).toFixed(1)}" y2="${(60 + Math.sin(a) * 33).toFixed(1)}"/>`; }
  for (let i = 0; i < 4; i++) { const a = -Math.PI / 2 + (i - 1.5) * 0.46; tumb += `<circle class="ve-tumb" cx="${(60 + Math.cos(a) * 22).toFixed(1)}" cy="${(60 + Math.sin(a) * 22).toFixed(1)}" r="2.6"/>`; }
  const arc = (2 * Math.PI * 44).toFixed(1);
  return `<svg class="ve-dial" viewBox="0 0 120 120" aria-hidden="true"><circle class="ve-bezel" cx="60" cy="60" r="54"/><g class="ve-ticks">${ticks}</g>
    <circle class="ve-track" cx="60" cy="60" r="44"/><circle class="ve-arc" cx="60" cy="60" r="44" style="stroke-dasharray:${arc};stroke-dashoffset:${arc}"/>
    <g class="ve-rotor">${spokes}<circle cx="60" cy="60" r="33" class="ve-rotor-ring"/></g><g>${tumb}</g><circle class="ve-core-dot" cx="60" cy="60" r="7"/></svg>`;
}
const ring = (count, cls, set = FUTHARK) => `<div class="ve-ring ${cls}">${Array.from({length: count}, (_, i) => {
  const a = i / count * 360 - 90;
  return `<span class="ve-glyph" style="transform:rotate(${a}deg) translateY(calc(var(--ve-r) * -1)) rotate(${-a}deg)">${set[i % set.length]}</span>`;
}).join('')}</div>`;
const column = (side, seed) => `<div class="ve-col ${side}">${Array.from({length: 7}, (_, i) => `<span class="ve-glyph">${FUTHARK[(seed + i * 3) % FUTHARK.length]}</span>`).join('')}</div>`;

// Play the entry over `stage`. `facts` are the record's counts for the telemetry: {ops, tasks, proofs,
// evidence, links, wiki}, or `lines`, a ready list of [label, value, cls]. `opts.inline` (t-602) is the
// seal in place on the Wiki page: it fits its box, waits for the dial however long it takes (no
// auto-open, no watchdog), a double-click or Enter also opens it, and at the end it calls opts.onOpen()
// with the doors apart instead of leaving. Returns {done} (a Promise), open() and skip().
export function playEntry(stage, facts = {}, opts = {}) {
  const inline = !!opts.inline;
  const old = stage.querySelector(':scope > .ve'); if (old) old.remove();
  const ov = document.createElement('div');
  ov.className = 've'; ov.tabIndex = -1;
  if (inline) { ov.setAttribute('role', 'region'); ov.setAttribute('aria-label', 'The vault seal: press the dial or Enter to open the vault'); }
  else { ov.setAttribute('role', 'dialog'); ov.setAttribute('aria-label', 'Opening the vault: press the dial or Enter to open, Escape to skip'); }
  const n = v => Number(v || 0).toLocaleString('en-US');
  const head = ['ALPACA VAULT // ' + (d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`)(new Date()), ''];
  const lines = facts.lines ? [head, ...facts.lines.map(([label, value, cls]) => [pad(label, value), cls || ''])] : [head,
    [pad('MOUNT RECORD', `${n(facts.ops)} OPS / ${n(facts.tasks)} TASKS`), ''],
    [pad('SEALED PROOFS', n(facts.proofs)), ''],
    [pad('EVIDENCE', n(facts.evidence)), ''],
    [pad('LINK GRID', `${n(facts.links)} LINKS`), ''],
    [pad('WIKI', facts.wiki ? `${n(facts.wiki)} ITEMS` : 'NOT SERVING'), facts.wiki ? '' : 'is-warn'],
  ];
  if (inline) ov.classList.add('is-inline');
  ov.innerHTML = `<div class="ve-door is-left">${column('is-left', 2)}</div><div class="ve-door is-right">${column('is-right', 11)}</div>
    <div class="ve-seam"></div>${[16, 37, 63, 84].map(top => `<div class="ve-bolt" style="top:${top}%"></div>`).join('')}
    <div class="ve-rings">${ring(20, 'is-mid')}${ring(28, 'is-outer')}<div class="ve-pulse"></div></div>
    <div class="ve-core"><div class="ve-status"></div><div class="ve-dialwrap" role="button" tabindex="0" aria-label="Open the vault">${dial()}${ring(16, 'is-seal', SEAL)}</div>
      <div class="ve-info"><div class="ve-log">${lines.map(([, cls]) => `<div class="ve-line ${cls}"></div>`).join('')}</div>
      <div class="ve-keys">${'<span class="ve-key"></span>'.repeat(12)}</div><div class="ve-hint">${inline ? 'press the dial to open the vault' : 'press the dial to open &middot; double-click to skip'}</div></div></div>`;
  stage.append(ov);
  const $ = s => ov.querySelector(s), $$ = s => [...ov.querySelectorAll(s)];
  const status = $('.ve-status'), wrap = $('.ve-dialwrap'), arc = $('.ve-arc'), rings = $('.ve-rings');
  const lineEls = $$('.ve-line'), keys = $$('.ve-key'), tumbs = $$('.ve-tumb'), bolts = $$('.ve-bolt');
  const seal = $('.ve-ring.is-seal'), mid = $('.ve-ring.is-mid'), outer = $('.ve-ring.is-outer');
  const glyphs = [...seal.children, ...mid.children, ...outer.children, ...$$('.ve-col .ve-glyph')];
  const timers = [];
  const beat = (ms, fn) => timers.push(setTimeout(() => { if (ov.isConnected) try { fn(); } catch { /* a dead beat never stalls the entry */ } }, ms));
  const setStatus = (text, cls) => { status.className = 've-status' + (cls ? ' ' + cls : ''); decrypt(status, text, 160); };

  // the rings sit on the dial's centre; the two outer ones are sized from the vault, the outer one
  // clipping its edges by design
  const place = () => {
    const o = ov.getBoundingClientRect(), d = wrap.getBoundingClientRect();
    const cx = d.left - o.left + d.width / 2, cy = d.top - o.top + d.height / 2, size = inline ? Math.min(o.width, 2 * cy, 2 * (o.height - cy)) : Math.min(o.width, 2 * cy);
    rings.style.left = cx + 'px'; rings.style.top = cy + 'px';
    const k = inline ? [0.3, 0.44] : [0.34, 0.5];         // in place, the outer ring stays inside the box
    mid.style.setProperty('--ve-r', Math.round(size * k[0]) + 'px'); outer.style.setProperty('--ve-r', Math.round(size * k[1]) + 'px');
    return [Math.round(size * k[0]), Math.round(size * k[1])];
  };
  let [r2, r3] = place();
  seal.style.setProperty('--ve-r', '86px');

  // the spin engine: JS-driven so the rate can ramp; the rings counter-rotate
  const RINGS = [{el: seal, dir: 1, mul: 1, a: 0, t0: 0}, {el: mid, dir: -1, mul: 0.72, a: 0, t0: 0}, {el: outer, dir: 1, mul: 0.5, a: 0, t0: 0}];
  let act = 0, done = false, spinV = 0, accel = false, blur = false, last = 0, rolling = 0, resolve;
  const finished = new Promise(r => { resolve = r; });
  const frame = now => {
    if (done || !ov.isConnected) return;
    const dt = Math.min(64, last ? now - last : 16) / 1000; last = now;
    for (const r of RINGS) {
      if (r.t0) { const p = Math.min(1, (now - r.t0) / 360), e = 1 - Math.pow(1 - p, 3); r.el.style.transform = `rotate(${r.a}deg) scale(${1 + 0.38 * e})`; r.el.style.opacity = String(1 - e); }
      else { r.a += spinV * r.mul * r.dir * dt; r.el.style.transform = `rotate(${r.a}deg)`; }
    }
    if (accel) spinV = Math.min(720, spinV * (1 + 6.5 * dt));
    if (!blur && spinV > 330) { blur = true; ov.classList.add('is-blur'); }
    requestAnimationFrame(frame);
  };
  const spin = v => { spinV = v; if (!last) requestAnimationFrame(frame); };
  const unlock = (i, radius) => {
    const r = RINGS[i]; r.a -= 17 * r.dir; r.t0 = performance.now();
    const f = document.createElement('div'); f.className = 've-flash';
    f.style.width = f.style.height = radius * 2 + 'px'; f.style.margin = `${-radius}px 0 0 ${-radius}px`;
    rings.append(f); setTimeout(() => f.remove(), 640);
  };
  const roll = () => { rolling = setInterval(() => { if (done || act >= 2) { clearInterval(rolling); return; } for (const k of keys) if (k.classList.contains('is-on') && Math.random() < 0.75) k.textContent = HEX[(Math.random() * 16) | 0]; }, 60); };

  function finish() {
    if (done) return;
    done = true; clearInterval(rolling); timers.forEach(clearTimeout); removeEventListener('resize', onResize);
    ov.classList.add('is-gone');
    setTimeout(() => { ov.remove(); resolve(); }, 320);
  }
  function toAwait() {
    if (done || act >= 1) return;
    act = 1;
    glyphs.forEach(g => g.classList.add('is-set')); tumbs.forEach(t => t.classList.add('is-lit'));
    keys.forEach(k => { if (!k.textContent) k.textContent = HEX[(Math.random() * 16) | 0]; k.classList.add('is-on'); });
    lines.forEach(([text], k) => { if (!lineEls[k].textContent) lineEls[k].textContent = text; });
    arc.style.strokeDashoffset = '0';
    ov.classList.add('is-charged'); wrap.classList.add('is-await');
    setStatus('ENGAGE THE SEAL', 'is-await');
    spin(9);
    if (!inline) beat(AWAIT_AUTO_MS, act2);
  }
  function act2() {
    if (done || act !== 1) return;
    act = 2; clearInterval(rolling);
    wrap.classList.remove('is-await'); ov.classList.add('is-granted');
    setStatus('ACCESS GRANTED', 'is-granted');
    spin(Math.max(spinV, 14));
    // t-600: the release in about 1.9 s (was 3.65 s)
    beat(60, () => ov.classList.add('is-pulsing'));
    beat(150, () => { accel = true; });
    beat(420, () => setStatus('RELEASING SEAL', 'is-granted'));
    beat(760, () => unlock(0, 86));
    beat(860, () => unlock(1, r2));
    beat(960, () => { unlock(2, r3); setStatus('SEAL OPEN', 'is-granted'); });
    bolts.forEach((b, i) => beat(1020 + i * 50, () => b.classList.add('is-drawn')));
    beat(1260, () => { ov.classList.add('is-breaking'); setStatus('VAULT OPEN', 'is-granted'); });
    beat(1330, () => ov.classList.add('is-open'));
    beat(1900, inline ? () => { done = true; clearInterval(rolling); opts.onOpen?.(); resolve(); } : finish);
  }

  wrap.addEventListener('click', e => { e.stopPropagation(); if (act === 0) toAwait(); else if (act === 1) act2(); });
  ov.addEventListener('click', () => { if (act === 0) toAwait(); });
  ov.addEventListener('dblclick', e => { e.stopPropagation(); if (inline) { toAwait(); act2(); } else finish(); });
  ov.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !inline) { e.preventDefault(); finish(); }
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (act === 0) toAwait(); else act2(); }
  });
  const onResize = () => { if (!done) [r2, r3] = place(); };
  addEventListener('resize', onResize);

  // ACT 1
  setStatus('VAULT SEALED');
  roll();
  // t-600: act 1 in about 0.95 s (was 2.25 s)
  lines.forEach(([text], k) => beat(60 + k * 80, () => decrypt(lineEls[k], text, 180)));
  keys.forEach((key, k) => beat(100 + k * 35, () => { key.textContent = HEX[(Math.random() * 16) | 0]; key.classList.add('is-on'); }));
  glyphs.forEach((g, k) => beat(80 + k * 9, () => g.classList.add('is-set')));
  tumbs.forEach((t, k) => beat(200 + k * 140, () => t.classList.add('is-lit')));
  beat(30, () => { arc.style.strokeDashoffset = '0'; });
  beat(950, toAwait);
  if (!inline) beat(14000, finish);                        // the watchdog: the vault always opens
  if (!inline) try { ov.focus({preventScroll: true}); } catch { /* focus is a courtesy */ }
  return {done: finished, open: () => { toAwait(); act2(); }, skip: finish};
}
