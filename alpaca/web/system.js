// Host load panel for the cockpit. Polls /hub/system.json every 3 s and re-renders #sys-panel in place.
// Pure ASCII file: symbols use HTML entities or \u escapes.

const URL_ = '/hub/system.json';
const EVERY_MS = 3000;
const HISTORY = 80;

let last = null;        // last good snapshot
let error = '';         // last fetch error, shown muted while last good data stays
let history = [];       // [{t, cpu, mem}]
let timer = null;
let inflight = false;
let showAll = false;

const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const num = (v, d = 0) => (Number.isFinite(+v) ? +v : 0).toFixed(d);
const pct = v => Math.max(0, Math.min(100, +v || 0));
const tone = p => (p >= 85 ? 'bad' : p >= 70 ? 'warn' : 'ok');
const short = (s, n) => { s = String(s ?? ''); return s.length > n ? s.slice(0, n - 3) + '...' : s; };

function bytes(b) {
  b = +b || 0;
  const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return (i === 0 ? num(b) : b >= 100 ? num(b) : num(b, 1)) + ' ' + u[i];
}
const gib = b => num((+b || 0) / 1073741824, (+b || 0) >= 1073741824 * 100 ? 0 : 1);

function span(s) {
  s = Math.max(0, Math.floor(+s || 0));
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  if (d) return d + 'd ' + h + 'h';
  if (h) return h + 'h ' + m + 'm';
  if (m) return m + 'm ' + (s % 60) + 's';
  return s + 's';
}

const CSS = `
#sys-panel{min-width:0}
.sys-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap}
.sys-head>div{min-width:0}
.sys-head p{overflow-wrap:anywhere}
.sys-live{display:inline-flex;align-items:center;gap:7px;font-size:10px;color:var(--green);white-space:nowrap}
.sys-live i{width:6px;height:6px;border-radius:50%;background:currentColor;box-shadow:0 0 0 4px var(--green-bg)}
.sys-live.sys-stale{color:var(--muted)}
.sys-live.sys-stale i{box-shadow:none}
.sys-body{padding:16px 18px 18px;display:grid;gap:18px;min-width:0}
.sys-err{font-size:11px;color:var(--muted);margin:0}
.sys-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:12px}
.sys-stat{border:1px solid var(--line);border-radius:var(--radius,8px);padding:12px;min-width:0;background:var(--surface-raised,var(--surface))}
.sys-stat-top{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.sys-label{font-size:10px;letter-spacing:1.2px;color:var(--muted);font-weight:500;text-transform:uppercase}
.sys-big{font-size:26px;font-weight:500;line-height:1.1;letter-spacing:-.5px;font-variant-numeric:tabular-nums}
.sys-big small{font-size:13px;color:var(--muted);margin-left:1px}
.sys-sub{font-size:11px;color:var(--muted);overflow-wrap:anywhere}
.sys-bar{height:6px;border-radius:3px;background:var(--surface-muted);overflow:hidden;margin:7px 0 5px}
.sys-bar>b{display:block;height:100%;border-radius:3px;background:var(--accent);transition:width .4s ease}
.sys-bar.sys-thin{height:4px;margin:6px 0 4px}
.sys-warn>b{background:var(--amber)}
.sys-bad>b{background:var(--red)}
.sys-t-warn{color:var(--amber)}
.sys-t-bad{color:var(--red)}
.sys-spark{display:block;width:100%;height:44px;margin-top:6px}
.sys-spark path.sys-a{fill:var(--chart-area,var(--accent-soft));stroke:none}
.sys-spark path.sys-l{fill:none;stroke:var(--chart-input,var(--accent));stroke-width:1.5;vector-effect:non-scaling-stroke;stroke-linejoin:round}
.sys-spark.sys-mem path.sys-l{stroke:var(--chart-output,var(--green))}
.sys-spark line{stroke:var(--line);stroke-width:1;vector-effect:non-scaling-stroke;stroke-dasharray:2 3}
.sys-sec h3{font-size:12px;font-weight:500;margin:0 0 8px;display:flex;justify-content:space-between;gap:8px;align-items:baseline}
.sys-sec h3 small{font-size:10px;color:var(--muted);font-weight:400}
.sys-rows{display:grid;gap:10px}
.sys-row{min-width:0}
.sys-row-top{display:flex;justify-content:space-between;gap:10px;font-size:12px;align-items:baseline}
.sys-row-top>span:first-child{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sys-row-top>span:last-child{white-space:nowrap;color:var(--muted);font-variant-numeric:tabular-nums;font-size:11px}
.sys-note{font-size:10px;color:var(--muted);display:block}
.sys-mono{font-family:var(--mono,monospace);font-size:11px}
.sys-table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;border:1px solid var(--line);border-radius:var(--radius,8px);max-width:100%}
.sys-table{width:100%;border-collapse:collapse;font-size:12px;min-width:460px}
.sys-table th{font-size:10px;letter-spacing:.8px;text-transform:uppercase;color:var(--muted);font-weight:500;text-align:left;padding:7px 10px;background:var(--surface-muted);white-space:nowrap}
.sys-table td{padding:7px 10px;border-top:1px solid var(--line);vertical-align:top}
.sys-table .sys-n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.sys-table td.sys-proc{max-width:260px}
.sys-table td.sys-proc strong{font-weight:500}
.sys-table td.sys-proc .sys-cmd{display:block;color:var(--muted);font-family:var(--mono,monospace);font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sys-toggle{margin-top:8px;font-size:11px;color:var(--accent);padding:4px 0}
.sys-notes{margin:0;padding:0;list-style:none;font-size:10px;color:var(--muted)}
.sys-empty{font-size:12px;color:var(--muted);padding:6px 0}
@media (max-width:480px){.sys-body{padding:14px 12px 14px}.sys-big{font-size:22px}}
`;

function injectStyle() {
  if (typeof document === 'undefined' || document.getElementById('sys-style')) return;
  const el = document.createElement('style');
  el.id = 'sys-style';
  el.textContent = CSS;
  document.head.appendChild(el);
}

function bar(p, cls = '') {
  const v = pct(p);
  return `<div class="sys-bar ${cls} sys-${tone(v)}" role="presentation"><b style="width:${num(v, 1)}%"></b></div>`;
}

function spark(key, cls) {
  const pts = history.map(h => pct(h[key]));
  if (!pts.length) return `<svg class="sys-spark ${cls}" viewBox="0 0 100 44" preserveAspectRatio="none" aria-hidden="true"></svg>`;
  if (pts.length === 1) pts.push(pts[0]);
  const n = pts.length - 1;
  const xy = pts.map((v, i) => [(i / n * 100).toFixed(2), (42 - v * 0.4).toFixed(2)]);
  const line = 'M' + xy.map(p => p.join(' ')).join('L');
  const area = line + `L100 44L0 44Z`;
  return `<svg class="sys-spark ${cls}" viewBox="0 0 100 44" preserveAspectRatio="none" aria-hidden="true"><line x1="0" x2="100" y1="22" y2="22"></line><path class="sys-a" d="${area}"></path><path class="sys-l" d="${line}"></path></svg>`;
}

function header(s) {
  const bits = [];
  if (s) {
    bits.push(esc(s.host || 'host'));
    if (s.cpu) bits.push(esc(num(s.cpu.count)) + ' cores');
    if (s.uptime_s != null) bits.push('up ' + esc(span(s.uptime_s)));
    if (Array.isArray(s.load)) bits.push('load ' + s.load.map(v => esc(num(v, 2))).join(' / '));
  }
  const stale = !!error || !s;
  return `<div class="panel-head sys-head"><div><h2>Host load</h2><p>${bits.length ? bits.join(' &middot; ') : 'Waiting for the first reading&hellip;'}</p></div><span class="sys-live ${stale ? 'sys-stale' : ''}" title="${esc(s && s.ts ? 'Last reading ' + s.ts : '')}"><i></i>every 3 s</span></div>`;
}

function stats(s) {
  const c = s.cpu || {}, m = s.mem || {};
  const cp = pct(c.pct), mp = pct(m.pct);
  const swap = m.swap_total_b ? ` &middot; swap ${esc(bytes(m.swap_used_b))}` : '';
  return `<div class="sys-stats">
<div class="sys-stat"><div class="sys-stat-top"><span class="sys-label">CPU</span><span class="sys-sub">${esc(num(c.count))} cores</span></div><div class="sys-big sys-t-${tone(cp)}">${esc(num(cp))}<small>%</small></div>${bar(cp)}<div class="sys-sub">whole machine</div>${spark('cpu', 'sys-cpu')}</div>
<div class="sys-stat"><div class="sys-stat-top"><span class="sys-label">RAM</span><span class="sys-sub">${esc(bytes(m.available_b))} free</span></div><div class="sys-big sys-t-${tone(mp)}">${esc(num(mp))}<small>%</small></div>${bar(mp)}<div class="sys-sub">${esc(bytes(m.used_b))} used of ${esc(bytes(m.total_b))}${swap}</div>${spark('mem', 'sys-mem')}</div>
</div>`;
}

function disks(s) {
  const rows = (s.disks || []).map(d => {
    const p = pct(d.pct);
    const label = d.mount === '/tmp' ? '/tmp' : (d.paths || []).map(x => x === 'project' ? 'project' : x).join(', ') || d.mount;
    const tmp = d.mount === '/tmp' ? '<span class="sys-note">RAM-backed tmpfs, per-user quota</span>' : `<span class="sys-note">${esc(d.mount)} &middot; ${esc(d.fs)}</span>`;
    return `<div class="sys-row"><div class="sys-row-top"><span class="sys-mono" title="${esc(d.path)}">${esc(label)}</span><span>${esc(gib(d.used_b))} / ${esc(gib(d.total_b))} GiB &middot; ${esc(num(p))}%</span></div>${bar(p, 'sys-thin')}${tmp}</div>`;
  });
  return `<div class="sys-sec"><h3>Disks</h3><div class="sys-rows">${rows.join('') || '<div class="sys-empty">No disks read.</div>'}</div></div>`;
}

function users(s) {
  const list = (s.users || []).slice(0, 8);
  const scale = Math.max(100, ...list.map(u => +u.cpu_pct || 0));
  const rows = list.map(u => {
    const w = (+u.cpu_pct || 0) / scale * 100;
    return `<div class="sys-row"><div class="sys-row-top"><span><strong>${esc(u.user)}</strong> <span class="sys-note" style="display:inline">${esc(u.procs)} procs &middot; top ${esc(u.top)}</span></span><span>${esc(num(u.cpu_pct))}% CPU &middot; ${esc(num(u.mem_pct, 1))}% &middot; ${esc(bytes(u.rss_b))}</span></div><div class="sys-bar sys-thin"><b style="width:${num(w, 1)}%"></b></div></div>`;
  });
  const more = (s.users || []).length > list.length ? `<small>top ${list.length} of ${esc((s.users || []).length)}</small>` : '<small>CPU % of one core</small>';
  return `<div class="sys-sec"><h3>By user ${more}</h3><div class="sys-rows">${rows.join('') || '<div class="sys-empty">No processes read.</div>'}</div></div>`;
}

function procs(s) {
  const all = s.procs || [];
  const list = showAll ? all : all.slice(0, 10);
  const rows = list.map(p => `<tr><td>${esc(p.user)}</td><td class="sys-proc" title="${esc(p.cmd)}"><strong>${esc(p.name)}</strong> <span class="sys-note" style="display:inline">${esc(p.pid)}</span><span class="sys-cmd">${esc(short(p.cmd, 70))}</span></td><td class="sys-n">${esc(num(p.cpu_pct, 1))}</td><td class="sys-n">${esc(num(p.mem_pct, 1))}</td><td class="sys-n">${esc(bytes(p.rss_b))}</td><td class="sys-n">${esc(span(p.elapsed_s))}</td></tr>`);
  const toggle = all.length > 10 ? `<button type="button" class="sys-toggle" data-sys-toggle aria-expanded="${showAll}">${showAll ? 'Show top 10' : 'Show all ' + esc(all.length)}</button>` : '';
  return `<div class="sys-sec"><h3>Top processes <small>CPU % of one core</small></h3><div class="sys-table-wrap"><table class="sys-table"><thead><tr><th>User</th><th>Process</th><th class="sys-n">CPU %</th><th class="sys-n">MEM %</th><th class="sys-n">RSS</th><th class="sys-n">Runtime</th></tr></thead><tbody>${rows.join('') || '<tr><td colspan="6" class="sys-empty">No processes read.</td></tr>'}</tbody></table></div>${toggle}</div>`;
}

function inner() {
  const s = last;
  const err = error ? `<p class="sys-err">Host monitor unavailable: ${esc(error)}</p>` : '';
  if (!s) return header(null) + `<div class="sys-body">${err || '<p class="sys-err">Loading host readings&hellip;</p>'}</div>`;
  const notes = (s.notes || []).length ? `<ul class="sys-notes">${s.notes.map(n => `<li>${esc(n)}</li>`).join('')}</ul>` : '';
  return header(s) + `<div class="sys-body">${err}${stats(s)}${disks(s)}${users(s)}${procs(s)}${notes}</div>`;
}

export function systemPanel() {
  injectStyle();
  return `<section class="panel visual-panel" id="sys-panel" aria-live="off">${inner()}</section>`;
}

function paint() {
  const el = typeof document !== 'undefined' && document.getElementById('sys-panel');
  if (el) el.innerHTML = inner();
}

async function tick(force = false) {
  if (inflight) return;
  if (!force && (document.hidden || !document.getElementById('sys-panel'))) return;
  inflight = true;
  try {
    const r = await fetch(URL_, {cache: 'no-store', credentials: 'same-origin'});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const s = await r.json();
    if (s && s.error) throw new Error(s.error);
    const prev = history[history.length - 1];
    if (!prev || prev.t !== s.ts) {
      history.push({t: s.ts, cpu: +(s.cpu && s.cpu.pct) || 0, mem: +(s.mem && s.mem.pct) || 0});
      if (history.length > HISTORY) history = history.slice(-HISTORY);
    }
    last = s;
    error = '';
  } catch (e) {
    error = (e && e.message) || 'request failed';
  } finally {
    inflight = false;
  }
  paint();
}

export function startSystem() {
  injectStyle();
  if (timer) return;
  timer = setInterval(() => { tick(); }, EVERY_MS);
  setTimeout(() => { tick(); }, 0);
  document.addEventListener('click', e => {
    const b = e.target && e.target.closest && e.target.closest('[data-sys-toggle]');
    if (!b || !b.closest('#sys-panel')) return;
    showAll = !showAll;
    paint();
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });
}
