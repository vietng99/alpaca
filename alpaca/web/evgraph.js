// The evidence graph of the Records page (Wiki tab, Open vault), in two modes over the same data.
// Columns: a layered node-link diagram, one colour-coded column per item type in the order of
// alpaca/hub.py GRAPH_COLUMNS (operations, tasks, decisions, sealed proofs, four kinds of evidence,
// review cards, phase doors, sessions, messages, wiki assertions, claims and sources), each entry a
// short mark stacked down its column and a curved line for every link. Brain (evbrain.js, loaded on
// first use): the same items as neurons in a 3D brain, one region per column, the links as fibres.
// In both, selecting an entry lights its chain: everything it rests on and everything resting on it,
// and the side panel names the record event, report note or wiki quote behind each link
// (/hub/graph-item.json). A legend chip highlights its column's sector (t-594): its entries and links
// stand out, everything else stays on screen dimmed, and its links fire. Read-only.
//
// Data: /hub/graph.json {columns:[{key, label, short, count}], nodes:[{id, c, label?, s?, k?}],
// links:[[from, to, kind]]}; a link runs from the item that supports to the item supported. A node
// without a label is its own label (an evidence pointer, a proof path). Colours come from the
// --eg-c<column> tokens of evgraph.css and are read again when the theme flips. Motion: the Firing
// slider sets the share of links that fire (at 100 every link fires, each on its own beat, and the
// marks a pulse reaches flash), the Speed slider the pulse rate, the Turn slider the brain's
// auto-rotate speed; a lit chain always pulses. Firing, speed, turn and the mode are kept for the
// viewer. prefers-reduced-motion starts with firing at 0 and no rotation, and still frames until the
// viewer moves a slider.
//
// Browse (evbrowse.js, t-596): a tray of cards under the toolbar, narrowed tier by tier from the
// operations down; a card selects its item and opens the items linked to it; a path of crumbs steps back.
// Its one filter box narrows the open tier, and at the first tier finds any item in the vault.
import {tier, narrow, grouped, reach, TIER_LIMIT} from './evbrowse.js';
// The double-click shockwave (t-598, after the Rune dashboard): rings run out from the cursor and the
// marks (in the brain, the neurons, pushed and springing back) light as the front passes.
export const WAVE_MS = 1400, WAVE_RING_MS = 650;
export function waveFront(w, now, size) {
  const age = now - w.t0, p = Math.min(1, age / WAVE_RING_MS), r1 = Math.max(110, size * 0.45);
  return {age, r: 6 + (r1 - 6) * (1 - Math.pow(1 - p, 3)), fade: Math.max(0, 1 - age / WAVE_MS)};
}
export function drawWaves(ctx, waves, now, size, color) {
  for (let k = waves.length - 1; k >= 0; k--) if (now - waves[k].t0 > WAVE_MS) waves.splice(k, 1);
  ctx.save();
  for (const w of waves) {
    const {age, r, fade} = waveFront(w, now, size), p = Math.min(1, age / WAVE_RING_MS);
    const shock = ctx.createRadialGradient(w.x, w.y, Math.max(0, r - 26), w.x, w.y, r + 6);
    shock.addColorStop(0, 'rgba(0,0,0,0)'); shock.addColorStop(0.8, color); shock.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.globalAlpha = 0.28 * (1 - p); ctx.fillStyle = shock; ctx.beginPath(); ctx.arc(w.x, w.y, r + 6, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = color;
    ctx.globalAlpha = 0.9 * (1 - p * p) * fade; ctx.lineWidth = 2.5; ctx.beginPath(); ctx.arc(w.x, w.y, r, 0, Math.PI * 2); ctx.stroke();
    const trail = waveFront({t0: w.t0 + 90}, now, size * 0.5);
    if (trail.age > 0) { ctx.globalAlpha = 0.35 * (1 - Math.min(1, trail.age / WAVE_RING_MS)); ctx.lineWidth = 1.2; ctx.beginPath(); ctx.arc(w.x, w.y, trail.r, 0, Math.PI * 2); ctx.stroke(); }
    if (age < 160) { ctx.globalAlpha = 0.5 * (1 - age / 160); ctx.fillStyle = color; ctx.beginPath(); ctx.arc(w.x, w.y, 4 + age / 20, 0, Math.PI * 2); ctx.fill(); }
  }
  ctx.restore();
}
export const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const num = v => Number(v || 0).toLocaleString();
export const FIND_IDLE_MS = 900;          // the find box waits for typing to stop, as the Studio search boxes do
export const KIND_LABEL = {evidence: 'Evidence', proof: 'Sealed proof', task: 'Task', op: 'Operation', decision: 'Decision',
  claim: 'Wiki claim', review: 'Review card', phase: 'Phase door', session: 'Session', message: 'Message',
  assertion: 'Wiki assertion', source: 'Wiki source'};
const ZOOM_MAX = 80;
export const FIRE_DEFAULT = 30, SPEED_DEFAULT = 100;     // percent of links firing; speed in percent of the base rate
export const BEAT_MS = 1800;                             // one pulse cycle at speed 100: travel, arrive, rest
export const TRAVEL = 0.55, FLASH = 0.2;                 // shares of the cycle: the pulse travels, then the mark flashes
const KEY = 'alpaca.vault.';
export const stored = (name, fallback) => { try { const v = localStorage.getItem(KEY + name); return v == null ? fallback : v; } catch { return fallback; } };
export const keep = (name, value) => { try { localStorage.setItem(KEY + name, String(value)); } catch { /* the page works without it */ } };
// A link's own two fixed numbers in [0, 1): whether it is among the firing share, and its beat offset.
export const rank = li => ((li * 2654435761) >>> 0) / 4294967296;
export const offset = li => (((li + 7919) * 2246822519) >>> 0) / 4294967296;
// Pulses run left to right, from operations toward the wiki: 1 marks a link whose stored direction
// points the other way, so its pulse starts at its second end. Lineage still reads the stored direction.
export function leftward(graph) { const out = new Uint8Array(graph.links.length); graph.links.forEach((l, li) => { out[li] = graph.nodes[l[0]].c > graph.nodes[l[1]].c ? 1 : 0; }); return out; }
// The links that fire at `percent`: the same links at every frame, and every link at 100.
export function firing(count, percent) { const share = Math.max(0, Math.min(100, percent)) / 100; const out = []; for (let li = 0; li < count; li++) if (share >= 1 || rank(li) < share) out.push(li); return out; }
const PREFIX = /^(ev|proof|claim|session|msg|door|as|src):/;
export const bareId = id => String(id).replace(PREFIX, '');
export const labelOf = n => n.label || bareId(n.id);
const times = v => (v / 100).toFixed(2).replace(/\.?0+$/, '') + 'x';     // 250 -> 2.5x, 100 -> 1x, 0 -> 0x
const kindOf = key => String(key || '').startsWith('ev-') ? 'evidence' : key;

// Everything the item rests on (walking links back) and everything resting on it (walking forward).
export function lineage(graph, start) {
  const seen = new Set([start]), lit = new Set();
  for (const dir of ['in', 'out']) {
    const queue = [start], mine = new Set([start]);
    while (queue.length) {
      const at = queue.pop();
      for (const li of (dir === 'in' ? graph.into[at] : graph.from[at]) || []) {
        lit.add(li);
        const next = dir === 'in' ? graph.links[li][0] : graph.links[li][1];
        if (!mine.has(next)) { mine.add(next); seen.add(next); queue.push(next); }
      }
    }
  }
  return {nodes: seen, links: lit};
}

// The graph in the shape the drawing reads: per column its node indexes, per node its links.
export function prepare(data) {
  const nodes = data.nodes || [], links = data.links || [], cols = (data.columns || []).map(() => []);
  const from = {}, into = {};
  nodes.forEach((n, i) => { (cols[n.c] ||= []).push(i); n.row = cols[n.c].length - 1; });
  links.forEach((l, i) => { (from[l[0]] ||= []).push(i); (into[l[1]] ||= []).push(i); });
  return {columns: data.columns || [], nodes, links, cols, from, into, index: new Map(nodes.map((n, i) => [n.id, i]))};
}

export function findMatches(graph, text, limit = 8) {
  const needle = String(text || '').trim().toLowerCase();
  if (!needle) return [];
  const out = [];
  for (let i = 0; i < graph.nodes.length && out.length < 400; i++) {
    const n = graph.nodes[i];
    if (String(n.id).toLowerCase().includes(needle) || labelOf(n).toLowerCase().includes(needle)) out.push(i);
  }
  // an exact id first, then ids that start with the text (record items before evidence files), then the rest
  const bare = i => bareId(graph.nodes[i].id).toLowerCase();
  const evidence = i => String(graph.columns[graph.nodes[i].c]?.key || '').startsWith('ev-');
  const order = i => bare(i) === needle ? 0 : bare(i).startsWith(needle) ? (evidence(i) ? 2 : 1) : 3;
  out.sort((a, b) => order(a) - order(b) || a - b);
  return limit ? out.slice(0, Math.max(limit, 0)) : out;
}

function facts(item) {
  return `<dl class="eg-facts">${(item.facts || []).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>`;
}

function opener(item, c) {
  if (item.kind === 'task') return `<button type="button" class="text-link" data-task="${esc(item.id)}">Open task</button>`;
  if (item.kind === 'proof' && item.path) return `<a class="text-link" href="${esc(c.href('library', {category: 'proofs', path: item.path}))}">Read report</a>`;
  if (item.kind === 'op') return `<a class="text-link" href="${esc(c.href('records', {section: 'wiki', op: item.id}))}">Wiki briefing</a>`;
  return '';
}

export function detailHtml(graph, item, c) {
  const groups = {in: [], out: []};
  for (const l of item.links || []) groups[l.dir].push(l);
  const linkList = (list, title) => list.length ? `<h4>${title} <span class="tab-count">${num(list.length)}</span></h4><ul class="eg-links">${list.slice(0, 60).map(l => {
    const i = graph.index.get(l.id), n = i == null ? null : graph.nodes[i];
    const col = n ? graph.columns[n.c] : null;
    return `<li><button type="button" class="eg-link" data-eg-pick="${esc(l.id)}"><span class="eg-dot" data-col="${n ? n.c : 0}"></span><span class="eg-link-kind">${esc(l.kind)}</span><span class="eg-link-name">${esc(n ? labelOf(n) : l.id)}</span></button><span class="row-sub">${esc(col ? KIND_LABEL[kindOf(col.key)] || col.label : '')}</span></li>`;
  }).join('')}${list.length > 60 ? `<li class="row-sub">and ${num(list.length - 60)} more</li>` : ''}</ul>` : '';
  const wiki = item.wiki;
  const said = wiki && wiki.available && wiki.assertions.length ? `<h4>What the wiki records <span class="tab-count">${num(wiki.assertions.length)}</span></h4><ul class="eg-quotes">${wiki.assertions.map(a => `<li><p><span class="mono">${esc(a.subject)}</span> <strong>${esc(a.predicate)}</strong> <span class="mono">${esc(a.object)}</span></p><blockquote>${esc(a.quote)}</blockquote><p class="row-sub mono">${esc(a.source || '')}</p></li>`).join('')}</ul>`
    : wiki && !wiki.available ? `<p class="row-sub">Wiki: ${esc(wiki.reason || 'unavailable')}</p>` : '';
  const i = graph.index.get(item.id), col = i == null ? null : graph.columns[graph.nodes[i].c];
  return `<div class="eg-detail-head"><span class="eg-kind" data-col="${i == null ? 0 : graph.nodes[i].c}"><span class="eg-dot"></span>${esc(col ? col.label : KIND_LABEL[item.kind] || item.kind)}</span><span class="mono eg-id">${esc(bareId(item.id))}</span>${opener(item, c)}</div>
    <p class="eg-title">${esc(item.label)}</p>${facts(item)}${linkList(groups.in, 'Rests on')}${linkList(groups.out, 'Supports')}${said}`;
}

const HINT = {columns: 'Select an entry to light its chain: the evidence it rests on and what rests on it. Scroll on the graph to zoom, drag to move. A legend chip highlights its column.',
  brain: 'Drag to turn the brain, Shift-drag, right-drag, the arrow keys or WASD to move it, scroll or pinch to zoom. Select a neuron to light its chain; a legend chip highlights its region and turns it to the front.'};

// Mount the graph into `host`. `c` carries href, api (path, params) and the initial selection `item`
// and `find`; `onState({item})` is told when the selection changes so the address can keep it.
export function mountGraph(host, data, c) {
  const graph = prepare(data);
  const reduce = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
  const ncol = graph.columns.length;
  const legend = graph.columns.map((col, ci) => `<button type="button" class="eg-key" data-eg-col="${ci}" data-col="${ci}" aria-pressed="false" title="${esc(col.label)}: highlight"><span class="eg-dot"></span><span class="eg-key-name">${esc(col.label)}</span><span class="eg-key-count">${num(col.count)}</span></button>`).join('');
  host.innerHTML = `<div class="eg">
    <div class="eg-bar"><button type="button" class="button eg-back" data-eg-act="back" hidden>&larr; Back</button><nav class="eg-crumbs" aria-label="Browse path"></nav>
      <input type="search" class="eg-filter" data-eg-find placeholder="Filter, or find any item: t-345, a file, a claim" aria-label="Filter this list or find an item in the vault" value="${esc(c.find || '')}">
      <span class="eg-found row-sub" aria-live="polite"></span><button type="button" class="text-link eg-tray-toggle" data-eg-act="tray" aria-expanded="true">Hide list</button>
      <div class="eg-actions"><div class="eg-modes" role="group" aria-label="Vault view"><button type="button" class="button" data-eg-mode="columns" aria-pressed="true">Columns</button><button type="button" class="button" data-eg-mode="brain" aria-pressed="false">Brain</button></div>
        <button type="button" class="button" data-eg-act="clear" disabled>Clear selection</button><button type="button" class="button" data-eg-act="fit">Fit</button><button type="button" class="button" data-eg-act="full" aria-pressed="false">Full screen</button></div></div>
    <div class="eg-tray" role="region" aria-label="Browse the vault"></div>
    <div class="eg-motion"><label class="eg-slider"><span>Firing</span><input type="range" min="0" max="100" step="1" data-eg-fire aria-label="Share of links firing"><output data-eg-fire-out></output></label>
      <label class="eg-slider"><span>Speed</span><input type="range" min="25" max="400" step="5" data-eg-speed aria-label="Pulse speed"><output data-eg-speed-out></output></label>
      <button type="button" class="button" data-eg-act="all" aria-pressed="false">Fire all</button>
      <button type="button" class="button eg-brain-only" data-eg-act="spin" aria-pressed="false" hidden>Auto-rotate</button>
      <label class="eg-slider eg-brain-only" hidden><span>Turn</span><input type="range" min="0" max="400" step="5" data-eg-turn aria-label="Auto-rotate speed"><output data-eg-turn-out></output></label>
      <label class="eg-slider eg-brain-only" hidden><span>Brightness</span><input type="range" min="10" max="200" step="5" data-eg-glow aria-label="Brain brightness"><output data-eg-glow-out></output></label></div>
    <div class="eg-legend" role="group" aria-label="Highlight a sector">${legend}<button type="button" class="text-link eg-key-all" data-eg-act="unfocus" hidden>Clear highlight</button></div>
    <div class="eg-main"><div class="eg-stage"><canvas class="eg-canvas" role="img" aria-label="Evidence graph: ${esc(graph.columns.map(col => `${num(col.count)} ${col.label.toLowerCase()}`).join(', '))}"></canvas><canvas class="eb-canvas" tabindex="0" role="img" aria-label="Evidence graph as a brain" hidden></canvas><div class="eg-tip" hidden></div></div>
      <aside class="eg-detail" aria-live="polite"><p class="route-hint">${HINT.columns}</p></aside></div></div>`;
  const root = host.querySelector('.eg'), canvas = host.querySelector('.eg-canvas'), brainCanvas = host.querySelector('.eb-canvas');
  const stage = host.querySelector('.eg-stage'), tip = host.querySelector('.eg-tip');
  const detail = host.querySelector('.eg-detail'), found = host.querySelector('.eg-found'), tray = host.querySelector('.eg-tray'), crumbs = host.querySelector('.eg-crumbs');
  const ctx = canvas.getContext('2d');
  const hidden = new Set();       // every column always shows; the legend highlights instead (t-594)
  const st = {w: 0, h: 0, dpr: 1, zoom: 1, pan: 0, sel: null, lit: null, matches: new Set(), hover: null, frame: 0, raf: 0, colors: [], base: null, alive: true,
    fire: reduce ? 0 : Number(stored('fire', FIRE_DEFAULT)), speed: Number(stored('speed', SPEED_DEFAULT)), moved: false, before: FIRE_DEFAULT,
    waves: [], mode: stored('mode', 'columns') === 'brain' ? 'brain' : 'columns', hidden, focus: new Set(), vis: [], reduce,
    turn: Number(stored('turn', 100)), glow: Number(stored('glow', 70)), back: leftward(graph)};
  st.glow = Math.max(10, Math.min(200, Number.isFinite(st.glow) ? st.glow : 70));
  st.turn = Math.max(0, Math.min(400, Number.isFinite(st.turn) ? st.turn : 100));
  st.fire = Math.max(0, Math.min(100, Number.isFinite(st.fire) ? st.fire : FIRE_DEFAULT));
  st.speed = Math.max(25, Math.min(400, Number.isFinite(st.speed) ? st.speed : SPEED_DEFAULT));
  st.firing = firing(graph.links.length, st.fire);
  let brain = null, brainLoading = null;
  const TOP = 44, BOTTOM = 14;

  function readColors() {
    const css = getComputedStyle(host);
    const v = name => css.getPropertyValue(name).trim() || '#888';
    st.colors = graph.columns.map((_, ci) => v(`--eg-c${ci}`));
    st.red = v('--red'); st.accent = v('--accent'); st.text = v('--text'); st.muted = v('--muted'); st.line = v('--line-strong'); st.bg = v('--surface');
    st.brainBg = v('--eg-brain-bg'); st.brainBg2 = v('--eg-brain-bg2'); st.btext = v('--eg-brain-text');
    st.bcolors = graph.columns.map((_, ci) => v(`--eg-bc${ci}`)); st.dark = document.documentElement.dataset.theme === 'dark';
    st.font = css.getPropertyValue('--font-sans').trim() || 'sans-serif';
  }
  function visible() { st.vis = graph.columns.map((_, ci) => ci).filter(ci => !st.hidden.has(ci)); st.slot = new Map(st.vis.map((ci, k) => [ci, k])); }
  function gap() { return st.vis.length > 1 ? (st.w - pad() * 2) / (st.vis.length - 1) : st.w; }
  function pad() { return st.w < 640 ? 22 : 54; }
  function colX(ci) {
    const k = st.slot.get(ci), n = st.vis.length;
    if (k == null) return -1e4;
    return n < 2 ? st.w / 2 : pad() + k * (st.w - pad() * 2) / (n - 1);
  }
  function rowY(i) {
    const n = graph.nodes[i], count = graph.cols[n.c].length, span = (st.h - TOP - BOTTOM) * st.zoom;
    return TOP + st.pan + (count < 2 ? span / 2 : n.row / (count - 1) * span);
  }
  const shown = li => !st.hidden.has(graph.nodes[graph.links[li][0]].c) && !st.hidden.has(graph.nodes[graph.links[li][1]].c);
  // a link belongs to a highlighted sector when either end sits in a highlighted column
  const focused = li => st.focus.has(graph.nodes[graph.links[li][0]].c) || st.focus.has(graph.nodes[graph.links[li][1]].c);
  st.focused = focused;
  function colorOf(i) {
    const n = graph.nodes[i];
    return graph.columns[n.c].key === 'claim' && n.s === 'contradicted' ? st.red : st.colors[n.c];
  }
  function size() {
    const r = stage.getBoundingClientRect();
    st.dpr = Math.min(window.devicePixelRatio || 1, 2);
    st.w = Math.max(280, Math.floor(r.width)); st.h = Math.max(360, Math.floor(r.height));
    canvas.width = st.w * st.dpr; canvas.height = st.h * st.dpr;
    canvas.style.width = st.w + 'px'; canvas.style.height = st.h + 'px';
    st.base = null;
    brain?.resize(st.w, st.h, st.dpr);
  }
  // A link's ends sit just beside its two columns, on the side facing the other column.
  function ends(li) {
    const [a, b] = graph.links[li], xa = colX(graph.nodes[a].c), xb = colX(graph.nodes[b].c), s = xb >= xa ? 1 : -1;
    return [xa + 5 * s, rowY(a), xb - 5 * s, rowY(b)];
  }
  function curve(g, li) {
    const [ax, ay, bx, by] = ends(li), mx = (ax + bx) / 2;
    g.moveTo(ax, ay); g.bezierCurveTo(mx, ay, mx, by, bx, by);
  }
  function point(li, t) {
    const [ax, ay, bx, by] = ends(li), mx = (ax + bx) / 2, u = 1 - t;
    return [u*u*u*ax + 3*u*u*t*mx + 3*u*t*t*mx + t*t*t*bx, u*u*u*ay + 3*u*u*t*ay + 3*u*t*t*by + t*t*t*by];
  }
  // The still layer: every link faint, every mark, the column heads. Kept in an offscreen canvas and
  // drawn again only when the size, zoom, pan, theme, highlight or selection dimming changes.
  function drawBase() {
    const off = st.base = document.createElement('canvas');
    off.width = canvas.width; off.height = canvas.height;
    const g = off.getContext('2d');
    g.setTransform(st.dpr, 0, 0, st.dpr, 0, 0);
    const dim = st.lit ? 0.35 : st.focus.size ? 0.3 : 1;
    const byCol = {};
    graph.links.forEach((l, li) => { if (shown(li)) (byCol[graph.nodes[l[0]].c] ||= []).push(li); });
    for (const [ci, list] of Object.entries(byCol)) {
      g.beginPath();
      for (const li of list) curve(g, li);
      g.strokeStyle = st.colors[ci]; g.globalAlpha = Math.min(0.45, Math.max(0.018, 2.4 / Math.sqrt(list.length) * Math.sqrt(st.zoom))) * dim; g.lineWidth = 1;
      g.stroke();
    }
    if (!st.lit && st.focus.size) {                       // a highlighted sector's links stand out of the dimmed web
      const mine = {};
      graph.links.forEach((l, li) => { if (focused(li)) (mine[graph.nodes[l[0]].c] ||= []).push(li); });
      for (const [ci, list] of Object.entries(mine)) {
        g.beginPath();
        for (const li of list) curve(g, li);
        g.strokeStyle = st.colors[ci]; g.globalAlpha = Math.min(0.7, Math.max(0.08, 6 / Math.sqrt(list.length) * Math.sqrt(st.zoom))); g.lineWidth = 1;
        g.stroke();
      }
    } else if (!st.lit && st.fire > 0) {                  // the firing links stand out of the faint web
      const firingByCol = {};
      for (const li of st.firing) if (shown(li)) (firingByCol[graph.nodes[graph.links[li][0]].c] ||= []).push(li);
      for (const [ci, list] of Object.entries(firingByCol)) {
        g.beginPath();
        for (const li of list) curve(g, li);
        g.strokeStyle = st.colors[ci]; g.globalAlpha = Math.min(0.6, Math.max(0.04, 4 / Math.sqrt(list.length) * Math.sqrt(st.zoom))); g.lineWidth = 1;
        g.stroke();
      }
    }
    g.globalAlpha = 1;
    const half = Math.max(16, Math.min(64, gap() / 2 - 1));
    for (const ci of st.vis) {
      const col = graph.columns[ci], x = colX(ci), ids = graph.cols[ci] || [];
      // a dense column draws each mark fainter, so its stack reads as a band with visible grain
      const step = (st.h - TOP - BOTTOM) * st.zoom / Math.max(1, ids.length - 1);
      const faded = st.focus.size && !st.focus.has(ci) ? 0.22 : 1;
      g.fillStyle = st.colors[ci]; g.globalAlpha = Math.min(0.95, Math.max(0.25, step / 3)) * (st.lit ? 0.55 : 1) * faded;
      const tick = st.w < 640 ? 6 : 10;
      for (const i of ids) {
        const y = rowY(i);
        if (y < TOP - 4 || y > st.h + 2) continue;
        if (col.key === 'claim') g.fillStyle = colorOf(i);
        g.fillRect(x - tick / 2, y - 0.6, tick, 1.4);
      }
      g.globalAlpha = 1;
      g.fillStyle = st.bg; g.fillRect(x - half, 0, half * 2, TOP - 6);
      const mark = st.focus.has(ci) ? 6 : 3;
      g.fillStyle = st.colors[ci]; g.fillRect(x - Math.min(half, 18), TOP - 6 - mark, Math.min(half, 18) * 2, mark);
      g.font = `600 ${st.w < 640 || half < 40 ? 10 : 12}px ${st.font}`; g.textAlign = 'center'; g.fillStyle = st.text;
      g.fillText(half >= 58 && col.label.length < 16 ? col.label : col.short || col.label, x, 14);
      g.font = `${st.w < 640 ? 9 : 11}px ${st.font}`; g.fillStyle = st.muted;
      g.fillText(num(col.count), x, 28);
    }
  }
  function drawLabels(g, set) {
    const perCol = {};
    for (const i of set) if (!st.hidden.has(graph.nodes[i].c)) (perCol[graph.nodes[i].c] ||= []).push(i);
    g.font = `11px ${st.font}`;
    const room = gap() - 26;
    if (room < 40) return;
    for (const [ci, list] of Object.entries(perCol)) {
      if (list.length > 18 || st.w < 640) continue;
      const x = colX(Number(ci)), right = st.slot.get(Number(ci)) < st.vis.length - 1;
      g.textAlign = right ? 'left' : 'right';
      let last = -Infinity;
      for (const i of list.sort((a, b) => rowY(a) - rowY(b))) {
        const y = rowY(i);
        if (y < TOP || y > st.h || y - last < 13) continue;     // one label per 13 px, so none overlap
        last = y;
        let text = labelOf(graph.nodes[i]);                      // a label stays inside the gap to the next column
        if (g.measureText(text).width > room) { while (text.length > 4 && g.measureText(text + '...').width > room) text = text.slice(0, -1); text += '...'; }
        g.fillStyle = st.bg; g.globalAlpha = 0.8;
        const wdt = g.measureText(text).width;
        g.fillRect(right ? x + 8 : x - 10 - wdt, y - 7, wdt + 4, 13);
        g.globalAlpha = 1; g.fillStyle = st.text;
        g.fillText(text, right ? x + 10 : x - 8, y + 4);
      }
    }
  }
  function draw(t = 0) {
    if (st.mode === 'brain') { if (brain) brain.draw(t); return; }
    if (!st.base) drawBase();
    ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(st.base, 0, 0);
    ctx.setTransform(st.dpr, 0, 0, st.dpr, 0, 0);
    const lit = st.lit;
    if (lit) {
      ctx.lineWidth = 1.6; ctx.globalAlpha = 0.95;
      for (const li of lit.links) { if (!shown(li)) continue; ctx.beginPath(); curve(ctx, li); ctx.strokeStyle = st.colors[graph.nodes[graph.links[li][0]].c]; ctx.stroke(); }
    }
    if (animating()) pulses((lit ? [...lit.links] : st.focus.size ? st.firing.filter(focused) : st.firing).filter(shown), t);
    if (lit) {
      ctx.globalAlpha = 1;
      for (const i of lit.nodes) { if (st.hidden.has(graph.nodes[i].c)) continue; const x = colX(graph.nodes[i].c), y = rowY(i); ctx.fillStyle = colorOf(i); ctx.fillRect(x - 7, y - 1.5, 14, 3); }
      drawLabels(ctx, lit.nodes);
    }
    ctx.globalAlpha = 1;
    for (const i of st.matches) { if (st.hidden.has(graph.nodes[i].c)) continue; const x = colX(graph.nodes[i].c), y = rowY(i); ctx.strokeStyle = st.text; ctx.lineWidth = 1.5; ctx.strokeRect(x - 9, y - 4, 18, 8); }
    if (st.sel != null && !st.hidden.has(graph.nodes[st.sel].c)) { const x = colX(graph.nodes[st.sel].c), y = rowY(st.sel); ctx.strokeStyle = st.text; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(x, y, 7, 0, Math.PI * 2); ctx.stroke(); }
    if (st.hover != null) { const x = colX(graph.nodes[st.hover].c), y = rowY(st.hover); ctx.fillStyle = st.text; ctx.fillRect(x - 8, y - 1.5, 16, 3); }
    if (st.waves.length) {                         // the marks the shock front passes light up
      const now = performance.now(), size = Math.min(st.w, st.h);
      for (const w of st.waves) {
        const {r, fade} = waveFront(w, now, size);
        for (const ci of st.vis) {
          const x = colX(ci); if (Math.abs(x - w.x) > r + 20) continue;
          ctx.fillStyle = st.colors[ci];
          for (const i of graph.cols[ci]) { const y = rowY(i), d = Math.hypot(x - w.x, y - w.y), g = Math.exp(-((d - r) ** 2) / 450); if (g < 0.08) continue; ctx.globalAlpha = g * fade; ctx.fillRect(x - 9, y - 2, 18, 4); }
        }
      }
      ctx.globalAlpha = 1;
      drawWaves(ctx, st.waves, now, size, st.accent);
    }
  }
  // Pulses: each link of `list` runs its own beat. A pulse with a short tail travels the link, then
  // the mark it reaches flashes and fades. Drawn in batches by colour, so every link can fire at once.
  function pulses(list, t) {
    const beat = BEAT_MS * 100 / st.speed, dots = {}, flashes = {}, starts = {};
    for (const li of list) {
      const cyc = (t / beat + offset(li)) % 1, l = graph.links[li], [a, b] = st.back[li] ? [l[1], l[0]] : l;
      if (cyc < TRAVEL) {
        const s = cyc / TRAVEL, col = graph.nodes[b].c;
        (dots[col] ||= []).push(li, s);
        if (s < 0.12) (starts[graph.nodes[a].c] ||= []).push(a);
      } else if (cyc < TRAVEL + FLASH) {
        (flashes[graph.nodes[b].c] ||= []).push(b, 1 - (cyc - TRAVEL) / FLASH);
      }
    }
    for (const [col, flat] of Object.entries(dots)) {
      ctx.fillStyle = st.colors[col];
      for (const [lag, alpha, sz] of [[0.06, 0.25, 2], [0.03, 0.5, 2.4], [0, 1, 3]]) {
        ctx.globalAlpha = alpha; ctx.beginPath();
        for (let k = 0; k < flat.length; k += 2) { const s = flat[k + 1] - lag; if (s < 0) continue; const [px, py] = point(flat[k], st.back[flat[k]] ? 1 - s : s); ctx.rect(px - sz / 2, py - sz / 2, sz, sz); }
        ctx.fill();
      }
    }
    for (const [col, flat] of Object.entries(flashes)) {
      const x = colX(Number(col));
      for (const [lo, alpha, w, h] of [[0, 0.18, 22, 7], [0.5, 0.9, 14, 3]]) {
        ctx.globalAlpha = alpha; ctx.beginPath();
        for (let k = 0; k < flat.length; k += 2) { if (flat[k + 1] < lo) continue; const y = rowY(flat[k]); ctx.rect(x - w / 2, y - h / 2, w, h); }
        ctx.fillStyle = st.colors[col]; ctx.fill();
      }
    }
    ctx.globalAlpha = 0.6;
    for (const [col, ids] of Object.entries(starts)) { const x = colX(Number(col)); ctx.beginPath(); for (const i of ids) ctx.rect(x - 6, rowY(i) - 1.5, 12, 3); ctx.fillStyle = st.colors[col]; ctx.fill(); }
    ctx.globalAlpha = 1;
  }
  // Motion runs when a chain is lit or links fire, or the brain turns; reduced motion waits for the
  // viewer to move a slider. Nothing runs while the page is hidden or the graph is off screen.
  st.onScreen = true;
  function animating() {
    if (!st.onScreen || (typeof document !== 'undefined' && document.hidden)) return false;
    return st.waves.length > 0 || ((!reduce || st.moved) && (st.lit || st.fire > 0)) || (st.mode === 'brain' && !!brain && brain.moving());
  }
  // One animation loop, stopped when the graph leaves the page or nothing moves.
  function loop(t) {
    st.raf = 0;
    if (!st.alive || !host.isConnected) { st.alive = false; return; }
    draw(t);
    if (animating()) st.raf = requestAnimationFrame(loop);
  }
  // one frame now; loop() keeps going only while something moves. A one-shot frame here would swallow a
  // kick that starts motion (a wave right after a click) and leave it frozen
  function kick() { if (!st.raf) st.raf = requestAnimationFrame(loop); }
  const fireInput = host.querySelector('[data-eg-fire]'), speedInput = host.querySelector('[data-eg-speed]'), allButton = host.querySelector('[data-eg-act=all]');
  const spinButton = host.querySelector('[data-eg-act=spin]'), fullButton = host.querySelector('[data-eg-act=full]'), turnInput = host.querySelector('[data-eg-turn]'), glowInput = host.querySelector('[data-eg-glow]');
  function showMotion() {
    fireInput.value = st.fire; speedInput.value = st.speed;
    host.querySelector('[data-eg-fire-out]').textContent = st.fire + '%';
    host.querySelector('[data-eg-speed-out]').textContent = times(st.speed);
    allButton.setAttribute('aria-pressed', String(st.fire === 100));
    allButton.textContent = st.fire === 100 ? 'Calm' : 'Fire all';
    spinButton.setAttribute('aria-pressed', String(!!brain && brain.spinning()));
    turnInput.value = st.turn; host.querySelector('[data-eg-turn-out]').textContent = times(st.turn);
    glowInput.value = st.glow; host.querySelector('[data-eg-glow-out]').textContent = `${st.glow}%`;
  }
  function setFire(value) {
    st.fire = Math.max(0, Math.min(100, Math.round(value))); st.moved = true;
    if (st.fire > 0 && st.fire < 100) st.before = st.fire;
    st.firing = firing(graph.links.length, st.fire); st.base = null; keep('fire', st.fire); showMotion(); kick();
  }
  fireInput.addEventListener('input', () => setFire(Number(fireInput.value)));
  speedInput.addEventListener('input', () => { st.speed = Number(speedInput.value); st.moved = true; keep('speed', st.speed); showMotion(); kick(); });
  allButton.addEventListener('click', () => setFire(st.fire === 100 ? st.before : 100));
  glowInput.addEventListener('input', () => { st.glow = Number(glowInput.value); keep('glow', st.glow); showMotion(); brain?.changed(); kick(); });
  turnInput.addEventListener('input', () => { st.turn = Number(turnInput.value); st.moved = true; keep('turn', st.turn); if (st.turn > 0) brain?.spin(true); showMotion(); kick(); });
  function showLegend() {
    host.querySelectorAll('[data-eg-col]').forEach(b => b.setAttribute('aria-pressed', String(st.focus.has(Number(b.dataset.egCol)))));
    host.querySelector('[data-eg-act=unfocus]').hidden = !st.focus.size;
    root.dataset.focus = st.focus.size ? 'on' : 'off';
  }
  // A chip adds its column to the highlight or takes it out again; several sectors can light at once.
  function toggleFocus(ci) {
    if (st.focus.has(ci)) st.focus.delete(ci); else st.focus.add(ci);
    st.base = null; brain?.changed(); brain?.focus(st.focus.has(ci) ? ci : null); showLegend(); kick();
  }
  function nodeAt(x, y) {
    let best = null, bestDx = Infinity;
    for (const ci of st.vis) { const dx = Math.abs(colX(ci) - x); if (dx < bestDx) { bestDx = dx; best = ci; } }
    const ids = graph.cols[best] || [];
    if (best == null || bestDx > Math.min(gap() / 2, 40) || !ids.length) return null;
    const span = (st.h - TOP - BOTTOM) * st.zoom, step = ids.length < 2 ? span : span / (ids.length - 1);
    const row = ids.length < 2 ? 0 : Math.round((y - TOP - st.pan) / step);
    if (row < 0 || row >= ids.length) return null;
    const i = ids[row];
    return Math.abs(rowY(i) - y) <= Math.max(5, step / 2) ? i : null;
  }
  function showTip(i, x, y) {
    if (i == null) { tip.hidden = true; return; }
    const n = graph.nodes[i];
    tip.innerHTML = `<strong>${esc(graph.columns[n.c].label)}</strong> <span class="mono">${esc(bareId(n.id))}</span>${n.label ? `<br>${esc(n.label)}` : ''}`;
    tip.hidden = false;
    tip.style.left = Math.max(4, Math.min(x + 14, st.w - 260)) + 'px'; tip.style.top = Math.max(4, Math.min(y + 12, st.h - 60)) + 'px';
  }
  function hint() { return `<p class="route-hint">${HINT[st.mode]}</p>`; }
  async function select(i, quiet = false) {
    st.sel = i; st.lit = i == null ? null : lineage(graph, i); st.base = null;
    tray.querySelectorAll('.eg-card').forEach(b => b.classList.toggle('on', i != null && b.dataset.egOpen === graph.nodes[i].id));
    host.querySelector('[data-eg-act=clear]').disabled = i == null;
    if (!quiet && c.onState) c.onState({item: i == null ? null : graph.nodes[i].id});
    brain?.select(i);
    if (i == null) { detail.innerHTML = hint(); kick(); return; }
    const y = rowY(i);
    if (y < TOP || y > st.h - BOTTOM) { st.pan -= y - st.h / 2; clampPan(); }
    kick();
    const id = graph.nodes[i].id;
    detail.innerHTML = `<p class="route-hint">Reading ${esc(labelOf(graph.nodes[i]))}&hellip;</p>`;
    try {
      const item = await c.api('/hub/graph-item.json', {id});
      if (st.sel !== i) return;
      detail.innerHTML = detailHtml(graph, item, c);
    } catch (error) {
      if (st.sel === i) detail.innerHTML = `<div class="notice"><div><h3>Could not read ${esc(id)}</h3><p>${esc(error.message || error)}</p></div></div>`;
    }
  }
  function clampPan() {
    const span = (st.h - TOP - BOTTOM) * st.zoom, room = st.h - TOP - BOTTOM;
    st.pan = Math.min(0, Math.max(room - span, st.pan));
  }
  function zoomAt(y, factor) {
    const z = Math.min(ZOOM_MAX, Math.max(1, st.zoom * factor));
    const at = (y - TOP - st.pan) / st.zoom;
    st.pan = y - TOP - at * z; st.zoom = z; clampPan(); st.base = null; kick();
  }
  // The brain module loads on first use; the same selection, find, sliders and legend drive it.
  function loadBrain() {
    if (brain || brainLoading) return brainLoading;
    brainLoading = import('./evbrain.js').then(m => {
      brain = m.mountBrain({canvas: brainCanvas, graph, st, hooks: {select: i => select(i), hover: (i, x, y) => { if (i !== st.hover) st.hover = i; showTip(i, x, y); }, kick, showMotion, wave: (x, y) => wave(x, y)}});
      brain.resize(st.w, st.h, st.dpr); showMotion(); kick();
      return brain;
    }).catch(error => { brainLoading = null; detail.innerHTML = `<div class="notice"><div><h3>Could not load the brain view</h3><p>${esc(error.message || error)}</p></div></div>`; setMode('columns'); });
    return brainLoading;
  }
  function setMode(mode) {
    st.mode = mode === 'brain' ? 'brain' : 'columns'; keep('mode', st.mode);
    root.dataset.mode = st.mode;
    host.querySelectorAll('[data-eg-mode]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.egMode === st.mode)));
    canvas.hidden = st.mode === 'brain'; brainCanvas.hidden = st.mode !== 'brain';
    host.querySelectorAll('.eg-brain-only').forEach(el => { el.hidden = st.mode !== 'brain'; });
    host.querySelector('[data-eg-act=fit]').textContent = st.mode === 'brain' ? 'Reset view' : 'Fit';
    tip.hidden = true; st.hover = null;
    if (st.sel == null) detail.innerHTML = hint();
    if (st.mode === 'brain') loadBrain(); else { st.base = null; kick(); }
  }
  // Full screen: the browser's own when it allows one, else the vault fills the window.
  function fullOn() { return document.fullscreenElement === root || root.classList.contains('eg-max'); }
  function showFull() { fullButton.setAttribute('aria-pressed', String(fullOn())); fullButton.textContent = fullOn() ? 'Exit full screen' : 'Full screen'; }
  async function toggleFull() {
    if (fullOn()) { if (document.fullscreenElement === root) await document.exitFullscreen?.(); root.classList.remove('eg-max'); showFull(); return; }
    try { if (!root.requestFullscreen) throw Error('no full screen'); await root.requestFullscreen(); }
    catch { root.classList.add('eg-max'); }
    showFull();
  }
  document.addEventListener('fullscreenchange', () => { if (!host.isConnected) return; showFull(); requestAnimationFrame(() => { size(); kick(); }); });
  root.addEventListener('keydown', e => { if (e.key === 'Escape' && root.classList.contains('eg-max')) { root.classList.remove('eg-max'); showFull(); } });
  // Browse: the open tier's cards. The filter narrows the tier after typing stops (FIND_IDLE_MS) or at
  // once on Enter, which also selects the first card; at the first tier it finds any item instead.
  const browse = {path: [], text: '', open: stored('tray', 'closed') === 'open'};   // collapsed until opened (t-602)
  function shownGroups() {
    if (!browse.path.length && browse.text.trim()) return grouped(graph, findMatches(graph, browse.text, 0));
    return narrow(graph, tier(graph, browse.path), browse.text);
  }
  function card(i) {
    const n = graph.nodes[i], more = reach(graph, i, browse.path);
    const meta = [n.s || n.k || '', more ? `${num(more)} linked` : ''].filter(Boolean).map(esc).join(' &middot; ');
    return `<button type="button" class="eg-card${st.sel === i ? ' on' : ''}" data-eg-open="${esc(n.id)}" data-col="${n.c}"${more ? ' data-more="1"' : ''}><span class="eg-card-id mono">${esc(bareId(n.id))}</span>${n.label ? `<span class="eg-card-name">${esc(n.label)}</span>` : ''}${meta ? `<span class="eg-card-meta">${meta}</span>` : ''}</button>`;
  }
  function renderTray() {
    const steps = [`<button type="button" class="eg-crumb" data-eg-crumb="0"${browse.path.length ? '' : ' aria-current="true"'}>Operations</button>`]
      .concat(browse.path.map((i, k) => `<span class="eg-crumb-sep" aria-hidden="true">/</span><button type="button" class="eg-crumb mono" data-eg-crumb="${k + 1}"${k === browse.path.length - 1 ? ' aria-current="true"' : ''} title="${esc(labelOf(graph.nodes[i]))}">${esc(bareId(graph.nodes[i].id))}</button>`));
    crumbs.innerHTML = steps.join('');
    host.querySelector('[data-eg-act=back]').hidden = !browse.path.length;
    host.querySelector('[data-eg-act=tray]').textContent = browse.open ? 'Hide list' : 'Browse';
    host.querySelector('[data-eg-act=tray]').setAttribute('aria-expanded', String(browse.open));
    tray.hidden = !browse.open;
    if (!browse.open) return;
    const groups = shownGroups();
    tray.innerHTML = groups.length ? groups.map(g => {
      const col = graph.columns[g.ci], extra = g.items.length - TIER_LIMIT;
      return `<section class="eg-group" data-col="${g.ci}"><h5><span class="eg-dot"></span>${esc(col.label)} <span class="tab-count">${num(g.items.length)}</span></h5>
        <div class="eg-cards">${g.items.slice(0, TIER_LIMIT).map(card).join('')}</div>${extra > 0 ? `<p class="row-sub eg-more">${num(extra)} more: type in the filter to narrow</p>` : ''}</section>`;
    }).join('') : `<p class="route-hint">${browse.text.trim() ? 'Nothing here matches the filter.' : 'Nothing else is linked to this item.'}</p>`;
  }
  function open(i) {
    select(i);
    if (reach(graph, i, browse.path) > 0) { browse.path.push(i); browse.text = ''; findInput.value = ''; found.textContent = ''; st.matches = new Set(); brain?.changed(); }
    renderTray(); tray.scrollTop = 0;
  }
  let findTimer = 0;
  function runFind(input, pickFirst) {
    clearTimeout(findTimer);
    browse.text = input.value;
    const global = !browse.path.length, all = global ? findMatches(graph, input.value, 0) : [];
    st.matches = new Set(all.slice(0, 400));
    const groups = shownGroups(), count = groups.reduce((k, g) => k + g.items.length, 0);
    found.textContent = input.value.trim() ? (count ? `${num(count)} match${count === 1 ? '' : 'es'}` : 'No match') : '';
    if (!browse.open && input.value.trim()) { browse.open = true; keep('tray', 'open'); }
    renderTray();
    const first = global ? all[0] : groups[0]?.items[0];
    if (pickFirst && first != null) select(first);
    brain?.changed();
    kick();
  }
  const findInput = host.querySelector('[data-eg-find]');
  findInput.addEventListener('input', () => { clearTimeout(findTimer); findTimer = setTimeout(() => runFind(findInput, false), FIND_IDLE_MS); });
  findInput.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); runFind(findInput, true); } if (e.key === 'Escape' && findInput.value) { findInput.value = ''; runFind(findInput, false); } });
  host.addEventListener('click', e => {
    const pick = e.target.closest('[data-eg-pick]');
    if (pick) { const i = graph.index.get(pick.dataset.egPick); if (i != null) select(i); return; }
    const opener = e.target.closest('[data-eg-open]');
    if (opener) { const i = graph.index.get(opener.dataset.egOpen); if (i != null) open(i); return; }
    const crumb = e.target.closest('[data-eg-crumb]');
    if (crumb) { browse.path = browse.path.slice(0, Number(crumb.dataset.egCrumb)); browse.text = ''; findInput.value = ''; found.textContent = ''; st.matches = new Set(); brain?.changed(); renderTray(); kick(); return; }
    const key = e.target.closest('[data-eg-col]');
    if (key) { toggleFocus(Number(key.dataset.egCol)); return; }
    const mode = e.target.closest('[data-eg-mode]');
    if (mode) { setMode(mode.dataset.egMode); return; }
    const act = e.target.closest('[data-eg-act]')?.dataset.egAct;
    if (act === 'clear') select(null);
    if (act === 'tray') { browse.open = !browse.open; keep('tray', browse.open ? 'open' : 'closed'); renderTray(); }
    if (act === 'back' && browse.path.length) { browse.path.pop(); browse.text = ''; findInput.value = ''; found.textContent = ''; st.matches = new Set(); brain?.changed(); if (!browse.open) { browse.open = true; keep('tray', 'open'); } renderTray(); kick(); }
    if (act === 'fit') { if (st.mode === 'brain') brain?.reset(); else { st.zoom = 1; st.pan = 0; st.base = null; } kick(); }
    if (act === 'full') toggleFull();
    if (act === 'spin' && brain) { brain.spin(!brain.spinning()); st.moved = true; showMotion(); kick(); }
    if (act === 'unfocus') { st.focus.clear(); st.base = null; brain?.changed(); showLegend(); kick(); }
  });
  let drag = null;
  canvas.addEventListener('pointerdown', e => { drag = {y: e.clientY, pan: st.pan, moved: false}; canvas.setPointerCapture?.(e.pointerId); });
  canvas.addEventListener('pointermove', e => {
    const r = canvas.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
    if (drag) { const dy = e.clientY - drag.y; if (Math.abs(dy) > 3) drag.moved = true; if (drag.moved && st.zoom > 1) { st.pan = drag.pan + dy; clampPan(); st.base = null; kick(); } return; }
    const i = nodeAt(x, y);
    if (i !== st.hover) { st.hover = i; kick(); }
    canvas.style.cursor = i == null ? (st.zoom > 1 ? 'grab' : 'default') : 'pointer';
    showTip(i, x, y);
  });
  canvas.addEventListener('pointerleave', () => { tip.hidden = true; if (st.hover != null) { st.hover = null; kick(); } });
  canvas.addEventListener('pointerup', e => {
    const moved = drag && drag.moved; drag = null;
    if (moved) return;
    const r = canvas.getBoundingClientRect();
    select(nodeAt(e.clientX - r.left, e.clientY - r.top));
  });
  canvas.addEventListener('dblclick', e => { const r = canvas.getBoundingClientRect(); wave(e.clientX - r.left, e.clientY - r.top); });   // a click still selects
  function wave(x, y) { st.waves.push({x, y, t0: performance.now()}); if (st.waves.length > 4) st.waves.shift(); kick(); }
  canvas.addEventListener('wheel', e => { e.preventDefault(); const r = canvas.getBoundingClientRect(); zoomAt(e.clientY - r.top, e.deltaY < 0 ? 1.25 : 0.8); }, {passive: false});
  const resize = typeof ResizeObserver === 'function' ? new ResizeObserver(() => { if (!host.isConnected) { resize.disconnect(); return; } size(); kick(); }) : null;
  resize?.observe(stage);
  const themeWatch = typeof MutationObserver === 'function' ? new MutationObserver(() => { if (!host.isConnected) { themeWatch.disconnect(); return; } readColors(); st.base = null; brain?.changed(); kick(); }) : null;
  themeWatch?.observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme']});
  const sight = typeof IntersectionObserver === 'function' ? new IntersectionObserver(entries => { if (!host.isConnected) { sight.disconnect(); return; } st.onScreen = entries[entries.length - 1].isIntersecting; kick(); }) : null;
  sight?.observe(stage);
  document.addEventListener('visibilitychange', () => { if (host.isConnected) kick(); });
  readColors(); visible(); size(); showMotion(); showLegend(); renderTray();
  // Open vault plays the vault entry over the stage (ventry.js, t-598), unless motion is reduced
  const entryStage = host.closest('.rec-vault-stage[data-entry]');
  if (entryStage && !reduce) {
    const count = test => graph.columns.reduce((k, col, ci) => k + (test(col.key) ? graph.cols[ci].length : 0), 0);
    import('./ventry.js').then(m => m.playEntry(entryStage, {ops: count(k => k === 'op'), tasks: count(k => k === 'task'), proofs: count(k => k === 'proof'),
      evidence: count(k => k.startsWith('ev-')), links: graph.links.length,
      wiki: data.wiki && data.wiki.available === false ? 0 : count(k => k === 'assertion' || k === 'claim' || k === 'source')})).catch(() => {});
  }
  setMode(st.mode);
  if (c.find) runFind(findInput, false);
  const start = c.item != null ? graph.index.get(c.item) : null;
  if (start != null) select(start, true); else kick();
  return {graph, select: id => select(graph.index.get(id) ?? null), state: st, mode: setMode, brain: () => brainLoading};
}
