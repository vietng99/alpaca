// Token and cost board on the Sessions tab. Data: /hub/burn.json (alpaca/analytics/burn.py).
// Every dynamic string is escaped. Colors, type sizes and spacing come from theme.css tokens; the
// charts draw shapes in SVG and keep every label in HTML so no text scales below 12px on a phone.
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const grp = n => Number(n || 0).toLocaleString();
const fmt = n => { n = Number(n || 0); return n >= 1e9 ? (n/1e9).toFixed(2)+' B' : n >= 1e6 ? (n/1e6).toFixed(1)+' M' : n >= 1e3 ? Math.round(n/1e3)+' k' : String(Math.round(n)); };
const money = v => v == null ? 'Unavailable' : new Intl.NumberFormat(undefined,{style:'currency',currency:'USD',minimumFractionDigits:2,maximumFractionDigits:2}).format(v);
const moneyShort = v => new Intl.NumberFormat(undefined,{style:'currency',currency:'USD',notation:'compact',maximumFractionDigits:1}).format(v);
const pct = (v,d=1) => Number(v || 0).toFixed(d)+'%';
const clock = s => new Date(s*1000).toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'});
const day = s => new Date(s*1000).toLocaleDateString(undefined,{day:'numeric',month:'short'});
const stamp = s => s ? day(s)+' '+clock(s) : 'Not recorded';
const span = s => { s = Math.max(0, Math.round(Number(s || 0) / 60)); const h = Math.floor(s/60), m = s%60; return h ? `${h} h ${String(m).padStart(2,'0')} m` : `${m} m`; };
const short = (v,n=64) => { v = String(v ?? ''); return v.length > n ? v.slice(0,n-3)+'...' : v; };
const tipAttr = html => esc(html);   // a tooltip is HTML kept in an attribute: escape it once more
const sidShort = sid => String(sid || '').replace(/^codex-/,'').slice(0,8);
const FAMILY = {opus:'Opus', sonnet:'Sonnet', haiku:'Haiku', gpt:'GPT', other:'Other'};
const FAMCOLOR = {opus:'var(--chart-input)', sonnet:'var(--chart-output)', haiku:'var(--chart-write)', gpt:'var(--chart-reason)', other:'var(--chart-cache)'};
const HEAT = [18,36,56,78,100];
const heat = p => `color-mix(in srgb, var(--accent) ${p}%, var(--surface))`;
const WINDOWS = [['24h','24 hours'],['7d','7 days'],['all','All']];
const KINDNAME = {main:'Main conversation', workflow:'Workflow subagents', subagent:'Other subagents'};

let last = null, lastHref = sid => '#sessions?' + new URLSearchParams({sid});
const open = new Set();

function level(v, edges) { let i = 0; for (const e of edges) if (v >= e) i++; return i; }
function edgeLabel(edges, i) { return i === 0 ? '&lt;'+fmt(edges[0]) : i === edges.length ? fmt(edges[edges.length-1])+'+' : fmt(edges[i-1])+'&ndash;'+fmt(edges[i]); }
function resolution(s) { return s < 3600 ? (s/60)+'-minute' : s < 86400 ? (s/3600)+'-hour' : 'one-day'; }
function cellWidth(n) { return n <= 120 ? 9 : n <= 240 ? 6 : 5; }

function tiles(d) {
  const t = d.totals, w = d.window;
  const top = t.top;
  const tile = (k,v,n) => `<div class="burn-tile"><div class="burn-k">${k}</div><div class="burn-v">${v}</div><div class="burn-n">${n}</div></div>`;
  return `<section class="burn-tiles" aria-label="Summary figures">
    ${tile(`Tokens, ${esc(windowName(w.key))}`, esc(fmt(t.tokens)), `${grp(t.sessions)} sessions, ${grp(t.people.length)} ${t.people.length === 1 ? 'person' : 'people'}, ${grp(t.requests)} requests`)}
    ${tile('Estimated API cost', esc(money(t.cost_usd)), t.unpriced_requests ? `${grp(t.unpriced_requests)} requests have no rate card` : 'Every request priced from the rate cards')}
    ${tile('Top session&#39;s share', top ? esc(pct(top.share)) : '&ndash;', top ? `${esc(short(top.title,48))} &middot; ${esc(fmt(top.tokens))}` : 'No activity')}
    ${tile('Subagents', esc(grp(t.subagents)), `active in this window, in ${grp(t.subagent_sessions)} ${t.subagent_sessions === 1 ? 'session' : 'sessions'}`)}
    ${tile('Span', t.first ? esc(span(t.last - t.first)) : '&ndash;', t.first ? `${esc(stamp(t.first))} &rarr; ${esc(stamp(t.last))}` : 'No requests in this window')}
  </section>`;
}
function windowName(k) { return k === 'range' ? 'chosen days' : (WINDOWS.find(w => w[0] === k) || [k,k])[1].toLowerCase(); }

function axis(d, cw) {
  const w = d.window, n = w.buckets, size = w.bucket_s;
  const every = Math.max(1, Math.ceil(64 / cw));
  const hourStep = [1,2,3,4,6,8,12,24,48,72,96,168].map(h => h*3600).find(s => s/size >= every) || size*every;
  let out = '';
  const first = Math.ceil(w.start / hourStep) * hourStep;
  for (let s = first; s < w.end; s += hourStep) {
    const b = (s - w.start) / size, x = b * cw;
    const date = new Date(s*1000), label = hourStep >= 86400 ? day(s) : clock(s);
    // A label that would run past either end of the track is left out; its tick stays.
    const room = x >= 34 && x <= n * cw - 34;
    out += `${room ? `<span class="burn-t" style="left:${x}px">${esc(label)}</span>` : ''}<i class="burn-tick" style="left:${x}px"></i>`;
    if (date.getHours() === 0 && date.getMinutes() === 0 && hourStep < 86400) out += `<span class="burn-day" style="left:${x+3}px">${esc(day(s))}</span><i class="burn-dayline" style="left:${x}px"></i>`;
  }
  return out;
}

function lanes(d) {
  const w = d.window, n = w.buckets, cw = cellWidth(n), track = n * cw, edges = d.heat.edges, size = w.bucket_s;
  const width = `style="--cw:${cw}px;--track:${track}px"`;
  let captionEnd = -Infinity;
  const bands = (d.events.bands || []).map(b => {
    const a = Math.max(0, (b.start - w.start) / size), z = Math.min(n, (b.end - w.start) / size);
    if (z <= a) return '';
    const text = String(b.label).replace(/_/g,' ') + ' limit reached', x = a * cw, wide = text.length * 7.4 + 12;
    const right = x + wide > track, left = right ? Math.max(0, x + Math.max((z-a)*cw, 3) - wide) : x;
    const caption = left >= captionEnd ? `<span class="burn-bandcap" style="left:${left}px">${esc(text)}</span>` : '';
    if (caption) captionEnd = left + wide;
    return `<div class="burn-band" style="left:${x}px;width:${Math.max((z-a)*cw,3)}px" data-tip="${tipAttr(`${esc(text)} &middot; ${esc(stamp(b.start))} to ${esc(clock(b.end))}`)}"></div>${caption}`;
  }).join('');
  const rows = d.sessions.map((x, i) => {
    const ks = Object.keys(x.bins).map(Number).sort((a,b) => a-b);
    const f = ks[0], l = ks[ks.length-1];
    const hot = new Set(((x.signals || {}).runaway || {bins: []}).bins.map(b => b.cell));
    const cells = ks.map(k => `<i class="burn-cell${hot.has(k) ? ' runaway' : ''}" style="left:${k*cw}px;background:${heat(HEAT[level(x.bins[k], edges)])}"></i>`).join('');
    const marks = Object.entries(x.events || {}).map(([k,v]) => `<i class="burn-mark ${v.refusals ? 'refusal' : 'retry'}" style="left:${Number(k)*cw + cw/2 - 4}px"></i>`).join('');
    return `<div class="burn-row burn-lane" tabindex="0" data-burn-lane="${i}" data-key="lane-${esc(x.sid)}" aria-label="${esc(x.title)}: ${esc(fmt(x.tokens))} tokens over ${esc(span(x.span_s))}. Open its breakdown.">
      <div class="burn-lab"><span class="burn-title">${esc(short(x.title, 60))}</span><span class="burn-meta">${esc(sidShort(x.sid))} &middot; ${esc(x.person)} &middot; ${esc(fmt(x.tokens))}</span></div>
      <div class="burn-track">${ks.length ? `<i class="burn-idle" style="left:${f*cw}px;width:${Math.max((l-f+1)*cw-2,4)}px"></i>` : ''}${cells}${marks}</div></div>`;
  }).join('');
  const fan = fanLanes(d, cw);
  const ev = d.events.buckets || {}, vals = Object.values(ev).map(v => v.retries + v.refusals), max = Math.max(1, ...vals);
  const evMarks = Object.entries(ev).map(([k,v]) => {
    const tot = v.retries + v.refusals, h = Math.max(4, Math.round(16 * Math.log(1+tot) / Math.log(1+max)));
    const st = Object.entries(v.statuses).map(([s,c]) => `${c} ${esc(s)}`).join(', ');
    return `<i class="burn-ev ${v.refusals ? 'refusal' : 'retry'}" style="left:${Number(k)*cw}px;height:${h}px" data-tip="${tipAttr(`${esc(clock(d.window.start + Number(k)*size))} &middot; ${v.refusals} refused, ${v.retries} retried &middot; ${st}`)}"></i>`;
  }).join('');
  const evRow = `<div class="burn-row burn-evlane"><div class="burn-lab"><span class="burn-title">Refusals and retries</span><span class="burn-meta">${grp(d.events.refusals)} refused &middot; ${grp(d.events.retries)} retried</span></div><div class="burn-track plain">${evMarks}</div></div>`;
  const legend = `<div class="burn-legend"><div class="burn-lg"><span>Tokens per ${esc(resolution(size).replace('-',' '))}</span><span class="burn-ramp"><i style="background:var(--line-strong)"></i><span>idle</span>${HEAT.map((p,i) => `<i style="background:${heat(p)}"></i><span>${edgeLabel(edges, i)}</span>`).join('')}</span></div>
    ${fan ? fanLegend(d) : ''}
    <div class="burn-lg"><i class="burn-sw runaway"></i>Runaway: 5&times; the session&#39;s median 15 minutes</div><div class="burn-lg"><i class="burn-sw refusal"></i>Refused request (429, limit)</div><div class="burn-lg"><i class="burn-sw retry"></i>API retry (overloaded, 5xx, connection)</div>${(d.events.bands || []).length ? '<div class="burn-lg"><i class="burn-sw band"></i>Limit reached until its recorded reset</div>' : ''}</div>`;
  return `<section class="panel burn-card"><div class="burn-head"><h2>Session timing, ${esc(resolution(size))} resolution</h2><p>Each cell is one ${esc(resolution(size).replace('-',' '))} step, shaded by the tokens that session spent in it, subagents included. The thin rail shows a session open but quiet between its first and last request. Select a lane to open its row in the table below.</p></div>
    <div class="burn-scroll"><div class="burn-plot" ${width}>
      <div class="burn-row burn-axisrow"><div class="burn-lab"></div><div class="burn-axis">${axis(d, cw)}</div></div>
      <div class="burn-row burn-striprow"><div class="burn-lab"></div><div class="burn-strip">${bands.replace(/<div class="burn-band"[^>]*><\/div>/g, '')}</div></div>
      <div class="burn-bandlayer">${bands.replace(/<span class="burn-bandcap"[^>]*>[^<]*<\/span>/g, '')}</div>
      ${rows || `<div class="burn-row"><div class="burn-lab"><span class="burn-meta">No session made a request in this window.</span></div><div class="burn-track plain"></div></div>`}
      ${fan}${evRow}
    </div></div>${legend}
    <p class="burn-foot">${esc(d.events.note)}</p></section>`;
}

function push(d) {
  const rows = d.sessions;
  if (!rows.length) return '';
  const maxRate = Math.max(1, ...rows.map(x => x.rate)), rt = niceTop(maxRate);
  const ct = niceTop(Math.max(0.01, ...rows.map(x => x.cost_usd)));
  const keys = Object.keys(FAMILY);
  const seg = (x, frac, val, pick) => {
    const parts = keys.map(k => [k, pick(k)]).filter(p => p[1] > 0), sum = parts.reduce((a,p) => a + p[1], 0) || 1;
    return `<div class="burn-btrack"><div class="burn-bbar" style="width:${Math.max(100*frac, 0.5)}%">${parts.map(([k,v]) => `<i style="flex:${v} 1 0;background:${FAMCOLOR[k]}" data-tip="${tipAttr(`${esc(FAMILY[k])} &middot; ${val(k, v, sum)} &middot; ${esc(short(x.title,50))}`)}"></i>`).join('')}</div></div>`;
  };
  const body = rows.map(x => `<div class="burn-brow" data-key="push-${esc(x.sid)}">
      <div class="burn-blab"><div class="burn-title">${esc(short(x.title, 60))}</div><div class="burn-meta">${esc(x.person)} &middot; ${grp(x.active_minutes)} active min</div></div>
      ${seg(x, x.rate / rt, (k,v,sum) => fmt(x.rate * v / sum)+' per active minute', k => (x.families[k] || {}).tokens || 0)}
      <div class="burn-bval">${esc(fmt(x.rate))}<small>per min</small></div>
      ${seg(x, x.cost_usd / ct, (k,v) => money(v), k => (x.families[k] || {}).cost || 0)}
      <div class="burn-bval">${esc(money(x.cost_usd))}<small>estimated</small></div></div>`).join('');
  const ticks = (top, n, f) => Array.from({length: n+1}, (_,i) => `<span style="left:${100*i/n}%">${esc(f(top*i/n))}</span>`).join('');
  const legend = keys.filter(k => rows.some(x => (x.families[k] || {}).tokens)).map(k => `<div class="burn-lg"><i class="burn-sw" style="background:${FAMCOLOR[k]}"></i>${esc(FAMILY[k])}</div>`).join('');
  return `<section class="panel burn-card"><div class="burn-head"><h2>How hard each session pushed, and what it cost</h2>
    <p><b>On the left, how hard a session pushed while it was working:</b> tokens per active minute, where an active minute is one with at least one request, so a session is not flattered by sitting idle. <b>On the right, what it cost:</b> the estimated API list price of every request, subagents included. A fast session that runs for twenty minutes costs less than a steady one that runs all night.</p>
    <p>${esc(d.cost.note)}</p></div>
    <div class="burn-push"><div class="burn-brow burn-bhead"><div>Session</div><div>Tokens per active minute</div><div class="r">Rate</div><div>Estimated API cost</div><div class="r">Cost</div></div>
    ${body}
    <div class="burn-bscale"><div></div><div class="burn-bticks">${ticks(rt, 4, fmt)}</div><div></div><div class="burn-bticks">${ticks(ct, 4, moneyShort)}</div><div></div></div></div>
    <div class="burn-legend"><div class="burn-lg"><span>Model, on both bars</span></div>${legend}</div></section>`;
}
function niceTop(v) { const p = Math.pow(10, Math.floor(Math.log10(v))); for (const s of [1,2,2.5,5,10]) if (v <= s*p) return s*p; return 10*p; }

function sawtooth(d) {
  const rows = d.sessions.filter(x => x.curve.turns);
  if (!rows.length) return '<p class="burn-empty">No main-conversation turns in this window.</p>';
  const peak = Math.max(...rows.map(x => x.curve.peak)), ymax = niceTop(peak * 1.02), maxTurns = Math.max(...rows.map(x => x.curve.turns));
  const W = 1000, H = 100;
  return `<div class="burn-smgrid">${rows.map(x => {
    const c = x.curve, pts = c.points, frac = Math.max(c.turns / maxTurns, 0.04);
    const px = i => (W * frac * (pts.length < 2 ? 0 : i / (pts.length - 1))).toFixed(1), py = v => (H - H * Math.min(v / ymax, 1)).toFixed(1);
    const line = pts.map((v,i) => (i ? 'L' : 'M') + px(i) + ' ' + py(v)).join('');
    const fill = pts.length ? line + `L${px(pts.length-1)} ${H}L0 ${H}Z` : '';
    const tip = `${esc(x.title)} &middot; ${grp(c.turns)} main-thread turns, peak ${grp(c.peak)} tokens of context${c.compactions ? ', compacted '+c.compactions+' times' : ', never compacted'}${x.subagents ? ' &middot; plus '+x.subagents+' subagents on their own curves' : ''}`;
    return `<div class="burn-sm" data-tip="${tipAttr(tip)}"><div class="burn-title">${esc(short(x.title, 48))}</div>
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(x.title)}: context climbs to ${Math.round(c.peak/1000)} thousand tokens over ${c.turns} turns, compacted ${c.compactions} times."><line class="burn-base" x1="0" x2="${W}" y1="${H}" y2="${H}" vector-effect="non-scaling-stroke"/>${pts.length ? `<path class="burn-sfill" d="${fill}"/><path class="burn-sline" d="${line}" vector-effect="non-scaling-stroke"/>` : ''}</svg>
      <div class="burn-cap">${grp(c.turns)} turns &middot; peak ${Math.round(c.peak/1000)}k<br>${c.compactions ? 'compacted '+c.compactions+'&times;' : 'never compacted'}${x.subagents ? ' &middot; '+x.subagents+' subagents' : ''}</div></div>`;
  }).join('')}</div>
  <p class="burn-foot">Each panel is one session's main conversation, left to right from its first request to its last in this window, on a shared vertical scale topping out at ${esc(fmt(ymax))} tokens. Its width is proportional to how many turns the session took, so a short panel is a short session. Context is input + cache read + cache write of one response; a compaction is a turn below 60% of the one before and at least 20 k tokens smaller. Subagent traffic is excluded here and handled below.</p>`;
}

function crewSection(d) {
  const c = d.crew;
  if (!c) return `<div class="burn-subhead"><h3>Subagents</h3><p>No subagent made a request in this window.</p></div>`;
  const G = c.growth, T = G.length ? G[G.length-1].t : 1;
  const mini = (k,v,n) => `<div class="burn-mini"><div class="burn-k">${k}</div><div class="burn-v">${v}</div><div class="burn-n">${n}</div></div>`;
  const W = 1000, H1 = 200, H2 = 110;
  const px = t => (T <= 1 ? 0 : W * (t - 1) / (T - 1)).toFixed(1);
  const ymax = niceTop(Math.max(1, ...G.map(g => g.q3)) * 1.05), py = v => (H1 - H1 * v / ymax).toFixed(1);
  const up = G.map((g,i) => (i ? 'L' : 'M') + px(g.t) + ' ' + py(g.q3)).join(''), dn = G.slice().reverse().map(g => 'L' + px(g.t) + ' ' + py(g.q1)).join('');
  const med = G.map((g,i) => (i ? 'L' : 'M') + px(g.t) + ' ' + py(g.med)).join('');
  const grid = [0, .25, .5, .75, 1].map(f => `<line class="burn-gl" x1="0" x2="${W}" y1="${(H1 - H1*f).toFixed(1)}" y2="${(H1 - H1*f).toFixed(1)}" vector-effect="non-scaling-stroke"/>`).join('');
  const ylab = [0, .25, .5, .75, 1].map(f => `<span style="top:${100 - 100*f}%">${esc(f ? fmt(ymax*f) : '0')}</span>`).join('');
  const xt = [...new Set([1, ...[.25, .5, .75].map(f => Math.round(1 + (T-1)*f)), T])].filter(t => t >= 1 && t <= T);
  const xlab = xt.map(t => `<span style="left:${T <= 1 ? 0 : 100*(t-1)/(T-1)}%">${t}</span>`).join('');
  const cmax = Math.max(1, ...G.map(g => g.cost)), bw = Math.max(W / T - 1.5, 1);
  const cbars = G.map(g => `<rect class="burn-cb" x="${(Number(px(g.t)) - (T <= 1 ? 0 : bw/2)).toFixed(1)}" y="${(H2 - H2*g.cost/cmax).toFixed(1)}" width="${bw.toFixed(1)}" height="${(H2*g.cost/cmax).toFixed(1)}"/>`).join('');
  const fams = [...new Set(c.caps.flatMap(k => Object.keys(k.split)))];
  const caps = c.caps.map(k => `<div class="burn-caprow"><span class="burn-ck">stop at ${k.cap} turns</span><span class="burn-captrack"><span class="burn-capbar" style="width:${100*k.tokens/c.tokens}%">${fams.map(f => (k.split[f] ? `<i style="flex:${k.split[f]} 1 0;background:${FAMCOLOR[f]}" data-tip="${tipAttr(`${esc(FAMILY[f] || f)} agents: ${esc(fmt(k.split[f]))} kept`)}"></i>` : '')).join('')}</span></span><span class="burn-cv">${esc(fmt(k.tokens))} &middot; saves ${esc(fmt(k.save))} &middot; ${k.cut} cut short</span></div>`).join('');
  const ran = `<div class="burn-caprow total"><span class="burn-ck">what ran</span><span class="burn-captrack"><span class="burn-capbar" style="width:100%">${fams.map(f => { const v = c.by_model.find(m => m.family === f); return v ? `<i style="flex:${v.tokens} 1 0;background:${FAMCOLOR[f]}"></i>` : ''; }).join('')}</span></span><span class="burn-cv">${esc(fmt(c.tokens))}</span></div>`;
  const split = c.by_model.map(m => `<div><span class="burn-h">${esc(FAMILY[m.family] || m.family)} agents</span><b>${grp(m.agents)} agents &middot; median ${Math.round(m.median_turns)} turns &middot; ${esc(fmt(m.median_tokens))} each &middot; ${esc(fmt(m.tokens))} in total</b></div>`).join('');
  return `<div class="burn-subhead"><h3>The ${grp(c.agents)} subagents in ${esc(short(c.title, 80))}</h3>
    <p>That sawtooth explains an ordinary session. This session spent the most on subagents in the window: each subagent is its own conversation, climbing its own curve, and none of them shares a cached token with the others.</p></div>
    <div class="burn-minis">${mini('Subagents', grp(c.agents), `median ${Math.round(c.median_turns)} turns, longest ${grp(c.max_turns)}`)}${mini(`Context by turn ${c.probe_turn || 1}`, (c.ctx_probe == null ? '&ndash;' : esc(Math.round(c.ctx_probe/1000)+'k')), `from ${c.ctx1 == null ? '&ndash;' : Math.round(c.ctx1/1000)+'k'} on turn one (median agent)`)}${mini('Spent re-reading', esc(pct(c.reread_pct)), `of the ${esc(fmt(c.tokens))} went to cache reads, not new work`)}${mini('Read per token written', c.read_per_written == null ? '&ndash;' : esc(grp(Math.round(c.read_per_written)))+'&times;', 'cached tokens in, one output token out')}</div>
    <div class="burn-panel"><h4>What each agent was carrying, by turn</h4><p>The line is the median agent; the band holds the middle half of them. It rises because nothing is dropped from a conversation until it is compacted.</p>
      <div class="burn-chart" data-burn-growth="1"><div class="burn-ylab">${ylab}</div><div class="burn-plotarea"><svg viewBox="0 0 ${W} ${H1}" preserveAspectRatio="none" role="img" aria-label="Median context per subagent rises from ${Math.round((c.ctx1||0)/1000)} thousand tokens on turn one to ${Math.round((c.ctx_probe||0)/1000)} thousand by turn ${c.probe_turn || 1}.">${grid}<path class="burn-bnd" d="${up}${dn}Z"/><path class="burn-ln" d="${med}" vector-effect="non-scaling-stroke"/></svg><div class="burn-xlab">${xlab}</div></div></div>
      <h4>What every agent together spent on that turn</h4><p>Later turns cost more each, but fewer agents get that far.</p>
      <div class="burn-chart short" data-burn-growth="1"><div class="burn-ylab"><span style="top:0%">${esc(fmt(cmax))}</span><span style="top:100%">0</span></div><div class="burn-plotarea"><svg viewBox="0 0 ${W} ${H2}" preserveAspectRatio="none" role="img" aria-label="Tokens spent at each turn number across every subagent.">${cbars}</svg><div class="burn-xlab">${xlab}</div></div></div>
      <h4>What a limit on turns per agent would have cost</h4><p>The same ${grp(c.agents)} agents, stopped after a fixed number of turns, split by model. Nothing else changed.</p>
      <div class="burn-caps">${caps || '<p class="burn-empty">Every agent stopped within a few turns; no limit would have saved anything.</p>'}${ran}</div></div>
    <div class="burn-split">${split}</div>`;
}

function why(d) {
  return `<section class="panel burn-card"><div class="burn-head"><h2>Why it costs what it does, session by session</h2>
    <p>A conversation pays for its whole history on every turn. As a session reads files and runs commands, what it carries grows, and each new turn sends all of it again, mostly as cache reads. <b>A compaction is the only thing that brings the context down</b>, and a session that works for hours repeats the climb until it stops.</p></div>
    ${sawtooth(d)}${crewSection(d)}</section>`;
}

function bars(items) {
  const max = Math.max(1, ...items.map(i => i.v));
  return items.map(i => `<div class="burn-drow"><span class="burn-dk">${i.dot ? `<i style="background:${i.dot}"></i>` : ''}${esc(i.k)}</span><span class="burn-dbar${i.v ? '' : ' zero'}" style="width:${Math.max(100*i.v/max, 2)}%"></span><span class="burn-dv">${esc(i.t)}</span></div>`).join('');
}
function breakdown(d, x, href) {
  const s = x.split || {};
  const work = bars(['main','workflow','subagent'].map(k => ({k: KINDNAME[k] + (s[k] && s[k].agents ? ` (${s[k].agents})` : ''), v: (s[k] || {}).tokens || 0, t: fmt((s[k] || {}).tokens || 0)})));
  const models = bars(x.models.filter(m => m.tokens).map(m => ({k: m.model, v: m.tokens, t: fmt(m.tokens)})));
  const kinds = bars([['cache_read','Cache reads'],['cache_write','Cache writes'],['input','Fresh input'],['output','Output']].map(([k,l]) => ({k: l, v: x.kinds[k], t: fmt(x.kinds[k])})));
  const costs = bars(x.models.filter(m => m.cost).map(m => ({k: m.model, v: m.cost, t: money(m.cost)})));
  const reqs = ['main','workflow','subagent'].filter(k => s[k]).map(k => KINDNAME[k].toLowerCase()+' '+grp(s[k].requests)).join(', ');
  return `<div class="burn-detwrap"><div class="burn-dgrid"><div class="burn-dblock"><h4>Where the tokens went</h4>${work}</div><div class="burn-dblock"><h4>Models</h4>${models || '<p class="burn-empty">No model recorded.</p>'}</div><div class="burn-dblock"><h4>What kind of tokens</h4>${kinds}</div><div class="burn-dblock"><h4>Estimated cost by model</h4>${costs || '<p class="burn-empty">Not priced.</p>'}</div></div>
    <div class="burn-dnote">Busiest step <b>${esc(fmt(x.peak_bucket))}</b> tokens${x.peak_bucket_at != null ? ' at '+esc(stamp(d.window.start + Number(x.peak_bucket_at)*d.window.bucket_s)) : ''} &middot; active in <b>${grp(x.active_minutes)}</b> minutes &middot; requests by kind: ${esc(reqs)}${x.errors ? ` &middot; <b>${grp(x.errors)}</b> retried or refused` : ''}${x.subagents_unreadable ? ` &middot; ${grp(x.subagents_unreadable)} recorded subagents have no readable transcript` : ''}${x.unpriced ? ` &middot; ${grp(x.unpriced)} requests without a rate card` : ''}
    &middot; <a class="text-link" href="${esc(href(x.sid))}">Open the session: ledger and conversation</a></div>
    ${signalsView(d, x)}${(x.runs || []).map(r => runView(x, r)).join('')}${plainView(d, x)}</div>`;
}

// ---- workflow fan-out ----
const STATE = {done:'done', error:'failed', failed:'failed', killed:'failed', cancelled:'failed', canceled:'failed'};
const secs = v => v == null ? '&ndash;' : v < 1 ? '&lt;1 s' : v < 90 ? Math.round(v)+' s' : v < 5400 ? Math.round(v/60)+' min' : (v/3600).toFixed(1)+' h';
function agentClass(a) { return 'burn-agent ' + (STATE[a.state] || (a.unfinished ? 'unfinished' : a.state === 'done' ? 'done' : 'running')) + (a.attempt > 1 ? ' retry' : ''); }
function agentTip(a, r) {
  const phase = (r.phases.find(p => p.index === a.phase) || {}).title || '';
  return `<span class="tt">${esc(r.name)}${phase ? ' &middot; '+esc(phase) : ''}</span><span class="tv">${esc(a.label || a.id)}</span><span class="tn">${esc(a.model)} &middot; ${esc(a.state)}${a.attempt > 1 ? ' &middot; attempt '+a.attempt : ''}${a.q != null && a.s != null ? ' &middot; queued '+secs(a.s - a.q) : ''} &middot; ran ${a.dur != null ? secs(a.dur) : 'to '+esc(clock(a.e))}${a.tokens != null ? ' &middot; '+esc(fmt(a.tokens))+' tokens'+(a.src === 'record' ? ' (run record)' : '') : ''}${a.cost != null ? ' &middot; '+esc(money(a.cost)) : ''}${a.tools != null ? ' &middot; '+a.tools+' tool calls' : ''}${a.child ? ' &middot; select to open its conversation' : ''}</span>`;
}
let childHref = (sid, child) => '#sessions?' + new URLSearchParams({sid, child, view: 'conversation'});
function runView(x, r) {
  const f = r.figures, t0 = r.start, t1 = Math.max(r.end || t0 + 1, t0 + 1), span0 = t1 - t0;
  const pos = t => Math.min(100, Math.max(0, 100 * (t - t0) / span0));
  const phaseBands = r.phases.filter(p => p.start != null).map((p, i) => `<div class="burn-phase ${i % 2 ? 'odd' : ''}" style="left:${pos(p.start)}%;width:${Math.max(pos(p.end) - pos(p.start), .4)}%" data-tip="${tipAttr(`<span class="tt">phase ${p.index}</span><span class="tv">${esc(p.title)}</span><span class="tn">${grp(p.agents)} agents &middot; peak ${grp(p.peak)} at once${p.failed ? ' &middot; '+p.failed+' failed' : ''}${p.retries ? ' &middot; '+p.retries+' retried' : ''}${p.detail ? ' &middot; '+esc(p.detail) : ''}</span>`)}"><span>${esc(p.title)}</span></div>`).join('');
  const bars = r.agents.map(a => {
    const s0 = a.s ?? a.q ?? t0, e0 = Math.max(a.e ?? s0, s0);
    const q = a.q != null && a.s != null && a.s > a.q ? `<i class="burn-queue" style="left:${pos(a.q)}%;width:${Math.max(pos(a.s) - pos(a.q), .15)}%;top:${a.slot*14 + 6}px"></i>` : '';
    const body = `style="left:${pos(s0)}%;width:${Math.max(pos(e0) - pos(s0), .25)}%;top:${a.slot*14 + 2}px;--fam:${FAMCOLOR[a.fam] || FAMCOLOR.other}" data-tip="${tipAttr(agentTip(a, r))}"`;
    return q + (a.child ? `<a class="${agentClass(a)}" href="${esc(childHref(x.sid, a.child))}" ${body} aria-label="${esc(a.label || a.id)}: open its conversation"></a>` : `<i class="${agentClass(a)}" ${body}></i>`);
  }).join('');
  const ticks = [0, .25, .5, .75, 1].map(v => `<span style="left:${v*100}%">${esc(clock(t0 + v*span0))}</span>`).join('');
  const fig = (k, v, n='') => `<div class="burn-fig"><div class="burn-k">${k}</div><div class="burn-fv">${v}</div>${n ? `<div class="burn-n">${n}</div>` : ''}</div>`;
  const perPhase = r.phases.filter(p => p.agents).map(p => `${esc(p.title)} ${p.agents}`).join(' &middot; ') || '&ndash;';
  const models = Object.entries(r.models).map(([m, n]) => `${esc(m)} ${n}`).join(', ');
  return `<div class="burn-run"><div class="burn-runhead"><h4>${esc(r.name)} <span class="badge ${r.status === 'completed' ? 'done' : r.status === 'running' ? 'running' : 'blocked'}">${esc(r.status)}</span></h4><p>${esc(r.run)} &middot; ${esc(stamp(t0))} to ${esc(clock(t1))} (${esc(span(span0))}) &middot; ${esc(models)}</p></div>
    <div class="burn-figs">${fig('Agents', grp(f.agents), perPhase)}${fig('Peak at once', grp(f.peak), 'the fan-out width')}${fig('Median queue wait', secs(f.queue_median_s), f.queue_longest_s != null ? 'longest '+secs(f.queue_longest_s) : '')}${fig('Agent duration', 'median '+secs(f.duration_median_s), 'longest '+secs(f.duration_longest_s))}${fig('Agent tokens', f.tokens != null ? esc(fmt(f.tokens)) : '&ndash;', f.cost_usd != null ? esc(money(f.cost_usd))+' estimated &middot; '+f.joined+' of '+f.agents+' from transcripts'+(f.from_record ? ', '+f.from_record+' from the run record' : '') : 'from the run record, not priced')}${fig('Failures and retries', `${grp(f.failed)} failed &middot; ${grp(f.retries)} retried`, `${f.unfinished ? f.unfinished+' unfinished when the run ended'+(f.extra_attempts ? ' &middot; ' : '') : ''}${f.extra_attempts ? f.extra_attempts+' extra attempts' : ''}`)}</div>
    <div class="burn-runplot"><div class="burn-phases">${phaseBands}</div><div class="burn-agents" style="height:${Math.max(1, r.rows)*14 + 4}px">${bars}</div><div class="burn-runaxis">${ticks}</div></div></div>`;
}
function plainView(d, x) {
  const p = x.plain || {agents: []};
  if (!p.agents.length) return '';
  const t0 = Math.min(...p.agents.map(a => a.s)), t1 = Math.max(t0 + 60, ...p.agents.map(a => a.e)), sp = t1 - t0;
  const pos = t => 100 * (t - t0) / sp;
  const bars = p.agents.map(a => {
    const tip = `<span class="tt">${esc(a.type || 'subagent')} &middot; ${esc(stamp(a.s))} to ${esc(clock(a.e))}</span><span class="tv">${esc(a.label || a.id)}</span><span class="tn">${esc(a.model)} &middot; ${grp(a.responses)} responses &middot; ${esc(fmt(a.tokens))} tokens${a.cost != null ? ' &middot; '+esc(money(a.cost)) : ''}${a.child ? ' &middot; select to open its conversation' : ''}</span>`;
    const body = `style="left:${pos(a.s)}%;width:${Math.max(pos(a.e) - pos(a.s), .4)}%;top:${a.slot*14 + 2}px;--fam:${FAMCOLOR[a.fam] || FAMCOLOR.other}" data-tip="${tipAttr(tip)}"`;
    return a.child ? `<a class="burn-agent done" href="${esc(childHref(x.sid, a.child))}" ${body} aria-label="${esc(a.label || a.id)}: open its conversation"></a>` : `<i class="burn-agent done" ${body}></i>`;
  }).join('');
  const ticks = [0, .5, 1].map(v => `<span style="left:${v*100}%">${esc(clock(t0 + v*sp))}</span>`).join('');
  return `<div class="burn-run"><div class="burn-runhead"><h4>Other subagents</h4><p>${grp(p.agents.length)} subagents outside any workflow, each from its first response to its last &middot; ${esc(fmt(p.agents.reduce((a,b) => a + b.tokens, 0)))} tokens</p></div>
    <div class="burn-runplot"><div class="burn-agents" style="height:${Math.max(1, p.rows)*14 + 4}px">${bars}</div><div class="burn-runaxis">${ticks}</div></div></div>`;
}
function fanLanes(d, cw) {
  const w = d.window, size = w.bucket_s, fan = d.fanout || [];
  if (!fan.length) return '';
  const byId = Object.fromEntries(d.sessions.map(x => [x.sid, x]));
  const rows = fan.map(ref => {
    const x = byId[ref.sid], r = x && x.runs[ref.run];
    if (!r) return '';
    const px = t => (t - w.start) / size * cw;
    const slots = Math.min(r.rows, 16), unit = 3, h = 8 + slots * (unit + 1);
    const phases = r.phases.filter(p => p.start != null).map((p, i) => `<i class="burn-fphase ${i % 2 ? 'odd' : ''}" style="left:${px(p.start)}px;width:${Math.max(px(p.end) - px(p.start), 2)}px"></i>`).join('');
    const bars = r.agents.filter(a => a.s != null).map(a => `<i class="${agentClass(a)} thin" style="left:${px(a.s)}px;width:${Math.max(px(Math.max(a.e, a.s)) - px(a.s), 2)}px;top:${4 + Math.min(a.slot, slots - 1) * (unit + 1)}px;--fam:${FAMCOLOR[a.fam] || FAMCOLOR.other}"></i>`).join('');
    const f = r.figures;
    return `<div class="burn-row burn-flane" tabindex="0" data-burn-open-run="${esc(x.sid)}" data-tip="${tipAttr(`<span class="tt">${esc(x.title)}</span><span class="tv">${esc(r.name)}</span><span class="tn">${grp(f.agents)} agents, ${grp(f.peak)} at once &middot; ${esc(fmt(f.tokens || 0))} tokens${f.failed ? ' &middot; '+f.failed+' failed' : ''}${f.retries ? ' &middot; '+f.retries+' retried' : ''} &middot; select for the full run</span>`)}">
      <div class="burn-lab"><span class="burn-title">${esc(short(r.name, 40))}</span><span class="burn-meta">${esc(sidShort(x.sid))} &middot; ${grp(f.agents)} agents &middot; ${grp(f.peak)} wide</span></div>
      <div class="burn-track plain" style="height:${h}px">${phases}${bars}</div></div>`;
  }).join('');
  return rows ? `<div class="burn-group"><div class="burn-gtitle">Workflow fan-out, the ${fan.length === 1 ? 'busiest run' : fan.length+' busiest runs'}: one line per agent, stacked by how many ran at once</div></div>${rows}` : '';
}
function fanLegend(d) {
  const fams = new Set();
  for (const ref of d.fanout || []) { const x = d.sessions.find(s => s.sid === ref.sid); for (const a of (x && x.runs[ref.run] || {agents: []}).agents) fams.add(a.fam); }
  return [...fams].map(f => `<div class="burn-lg"><i class="burn-sw" style="background:${FAMCOLOR[f] || FAMCOLOR.other}"></i>${esc(FAMILY[f] || f)} agent</div>`).join('')
    + '<div class="burn-lg"><i class="burn-sw failed"></i>Agent failed</div><div class="burn-lg"><i class="burn-sw unfinished"></i>Unfinished when the run ended</div><div class="burn-lg"><i class="burn-sw retry-mark"></i>Retried (attempt 2+)</div>';
}
// ---- off-track signals (alpaca/analytics/insights.py) ----
const mins = v => v == null ? '&ndash;' : v < 60 ? Math.round(v)+' s' : v < 5400 ? Math.round(v/60)+' min' : (v/3600).toFixed(1)+' h';
const RULES = {R1:'same call repeated', R2:'same file edited', R3:'test fail, edit, fail'};
const FRICTION = [['git_guard','git guard'],['hook_denial','hook denial'],['style','style'],['phase_gate','phase gate'],['permission','permission']];
function convLink(x, thread, line) {
  if (!line) return '';
  const child = thread ? (x.child_links || {})[thread] : null;
  if (thread && !child) return `<span class="burn-ref">agent ${esc(String(thread).slice(0,8))}, line ${line}</span>`;
  return `<a class="text-link burn-ref" href="#sessions?${esc(new URLSearchParams({sid: x.sid, ...(child ? {child} : {}), view: 'conversation', around: String(line)}).toString())}">${thread ? 'agent '+esc(String(thread).slice(0,8))+', ' : ''}line ${line}</a>`;
}
function flag(v, bad) { return bad ? `<span class="burn-flag">${v}</span>` : v; }
function signalsCard(d) {
  const rows = d.sessions.filter(x => x.signals);
  if (!rows.length) return '';
  const body = rows.map(x => {
    const g = x.signals, o = g.offtask, i = g.idle, f = g.friction.counts, h = g.hooks;
    const off = o.off.cost, all = o.claimed.cost + o.off.cost + o.unclaimed.cost;
    const fr = FRICTION.filter(([k]) => f[k]).map(([k, l]) => `${f[k]} ${l}`).join(', ');
    return `<tr><td class="burn-sid"><button class="burn-exp" type="button" data-burn-open-run="${esc(x.sid)}"><span>${esc(short(x.title, 46))}<small>${esc(sidShort(x.sid))}</small></span></button></td>
      <td class="num">${g.loops.length ? flag(g.loops.length+' &middot; '+esc(g.loops[0].rule)+' &times;'+g.loops[0].count, true) : '&ndash;'}</td>
      <td class="num">${g.waste.cost ? flag(esc(money(g.waste.cost)), g.waste.cost > 0.1 * x.cost_usd) : '&ndash;'}</td>
      <td class="num">${all ? flag(esc(money(off))+' <small>'+esc(pct(100*off/all, 0))+'</small>', off > 0.25 * all) : '&ndash;'}<small>unclaimed ${esc(money(o.unclaimed.cost))}</small></td>
      <td class="num">${esc(mins(i.idle_s))}<small>${i.gaps} gaps</small></td>
      <td class="num">${i.rewrites ? esc(money(i.rewrite_cost)) : '&ndash;'}<small>${i.rewrites} after idle</small></td>
      <td class="num">${esc(mins(i.owner_wait_s))}<small>${i.owner_waits} waits${i.questions.length ? ', '+i.questions.length+' questions' : ''}</small></td>
      <td class="num">${g.runaway.bins.length || g.runaway.agents.length ? flag(`${g.runaway.bins.length} &middot; ${g.runaway.agents.length}`, true) : '&ndash;'}<small>steps &middot; agents</small></td>
      <td class="num">${g.collisions.length ? flag(g.collisions.length, true) : '&ndash;'}</td>
      <td class="num">${fr ? flag(esc(fr), true) : '&ndash;'}</td>
      <td class="num">${esc(mins(h.ms / 1000))}<small>${h.failures ? flag(h.failures+' failed', true) : 'no failures'}</small></td></tr>`;
  }).join('');
  return `<section class="panel burn-card"><div class="burn-head"><h2>Is any session going off track?</h2><p>Nine signals from each session&#39;s transcripts and the record, over the same window. A highlighted value is worth a look; select a session for the evidence behind each one. The rules are listed under the table.</p></div>
    <div class="table-wrap"><table class="data-table burn-table burn-signals"><thead><tr><th scope="col">Session</th><th scope="col">Loops</th><th scope="col">Wasted</th><th scope="col">Off-task</th><th scope="col">Idle</th><th scope="col">Cache re-write</th><th scope="col">Owner wait</th><th scope="col">Runaway</th><th scope="col">Collisions</th><th scope="col">Friction</th><th scope="col">Hook time</th></tr></thead><tbody>${body}</tbody></table></div>
    <details class="burn-rules"><summary>How each signal is measured</summary><pre>${esc(d.rules || '')}</pre></details></section>`;
}
// With chosen days (window key `range`) the card keeps the tasks done inside them and recounts each
// operation's done, captured and cost; the median and outlier flags stay those of the operation's
// whole captured history, which the outlier rule names (the burn board shows only the chosen
// days). The 1, 7, 30 and 90 day windows show all captured history, as they always did.
export function outcomesFor(d) {
  const w = d.window || {}, all = d.outcomes || [];
  if (w.key !== 'range') return all;
  return all.map(o => {
    const tasks = (o.tasks || []).filter(t => t.done >= w.start && t.done <= w.end), captured = tasks.filter(t => t.captured);
    return {...o, tasks, done: tasks.length, captured: captured.length, cost: captured.reduce((n, t) => n + (t.cost || 0), 0)};
  }).filter(o => o.tasks.length);
}
function outcomesCard(d) {
  const ops = outcomesFor(d), chosen = (d.window || {}).key === 'range';
  if (!ops.length) return chosen && (d.outcomes || []).length ? `<section class="panel burn-card"><div class="burn-head"><h2>Cost per outcome</h2><p>No task was done in the chosen days.</p></div></section>` : '';
  const task = t => `<div class="burn-drow burn-trow"><span class="burn-dk">${esc(t.task)} ${esc(t.title)}${t.outlier ? ' <span class="burn-flag">'+esc((t.ratio || 0).toFixed(1))+'&times; the median</span>' : ''}${t.cycles > 1 ? ' <span class="burn-flag">reopened</span>' : ''}</span><span class="burn-dv">${t.captured ? esc(fmt(t.tokens)) : '&ndash;'}</span><span class="burn-dv">${t.captured ? esc(money(t.cost)) : 'not captured'}</span></div>`;
  const body = ops.map(o => `<div class="burn-op"><h4>${esc(o.op)} <span>${grp(o.done)} done &middot; ${grp(o.captured)} with captured spend &middot; ${esc(money(o.cost))} &middot; median ${o.median == null ? '&ndash;' : esc(money(o.median))}</span></h4>
    ${o.tasks.filter(t => t.captured).slice(0, 3).map(task).join('') || '<p class="burn-empty">No done task in this operation has captured spend.</p>'}
    ${o.tasks.length > 3 ? `<details><summary>All ${grp(o.tasks.length)} done tasks</summary>${o.tasks.map(task).join('')}</details>` : ''}</div>`).join('');
  return `<section class="panel burn-card"><div class="burn-head"><h2>Cost per outcome</h2><p>What each done task cost from its claim to the move to done: every response its holding sessions and their subagents made while they held it, split evenly when a session held several tasks at once. ${chosen ? 'Only the tasks done in the chosen days; the median is the operation&#39;s over all captured history.' : 'All captured history, not only this window.'} An outlier costs more than 2&times; the median done task of its operation; a task done before its session&#39;s transcript was captured shows as not captured.</p></div>
    <div class="burn-ops">${body}</div></section>`;
}
function sharedCard(d) {
  const c = d.collisions || [], h = d.hooks || [];
  const coll = c.length ? c.map(v => `<div class="burn-drow burn-crow"><span class="burn-dk" title="${esc(v.path)}">${esc(v.path)}</span><span class="burn-dv">${esc(sidShort(v.a))} &times;${v.a_edits} &middot; ${esc(sidShort(v.b))} &times;${v.b_edits}</span><span class="burn-dv">${esc(stamp(v.start))} &ndash; ${esc(clock(v.end))}</span></div>`).join('') : '<p class="burn-empty">No two sessions edited the same file within 15 minutes of each other in this window.</p>';
  const max = Math.max(1, ...h.map(v => v.ms));
  const hooks = h.length ? h.map(v => `<div class="burn-drow burn-hrow"><span class="burn-dk">${esc(v.hook)}</span><span class="burn-dbar" style="width:${Math.max(2, 100*v.ms/max)}%"></span><span class="burn-dv">${esc(mins(v.ms/1000))} &middot; ${grp(v.runs)} runs &middot; max ${esc(mins(v.max_ms/1000))}${v.failures ? ' &middot; '+v.failures+' failed' : ''}</span></div>`).join('') : '<p class="burn-empty">No Alpaca hook run was recorded in this window.</p>';
  return `<section class="panel burn-card"><div class="burn-head"><h2>Collisions and hook overhead</h2><p>Files two sessions edited within 15 minutes of each other (subagents count toward their session), and the Alpaca hooks agents waited on the longest, measured from the hook durations Claude Code writes into the transcripts, with failures from the transcripts and the hook pool.</p></div>
    <div class="burn-dgrid burn-shared"><div class="burn-dblock"><h4>Cross-session collisions</h4>${coll}</div><div class="burn-dblock"><h4>Slowest Alpaca hooks</h4>${hooks}</div></div></section>`;
}
function signalsView(d, x) {
  const g = x.signals;
  if (!g) return '';
  const block = (title, body) => `<div class="burn-dblock"><h4>${title}</h4>${body}</div>`;
  const loops = g.loops.length ? g.loops.map(f => `<div class="burn-sig"><b>${esc(f.rule)} ${esc(RULES[f.rule] || f.label)}</b> &times;${f.count}${f.within ? ' within '+f.within+' responses' : ' in a row'} &middot; ${esc(stamp(f.ts))} &middot; ${convLink(x, f.thread, f.line)}<code>${esc(short(f.call, 160))}</code></div>`).join('') : '<p class="burn-empty">No loop rule fired.</p>';
  const waste = g.waste.items.length ? g.waste.items.slice(0, 10).map(w => `<div class="burn-drow"><span class="burn-dk">${esc(w.cause)}: ${esc(short(w.agent, 50))}${w.run ? ' <small>'+esc(w.run)+'</small>' : ''}</span><span class="burn-dv">${w.tokens != null ? esc(fmt(w.tokens)) : '&ndash;'}</span><span class="burn-dv">${w.cost != null ? esc(money(w.cost)) : 'not priced'}</span></div>`).join('') + Object.entries(g.waste.runs).map(([r, v]) => `<div class="burn-n">Run ${esc(r)}: ${esc(money(v))} wasted</div>`).join('') : '<p class="burn-empty">No killed, failed or retried agent and no reopened task.</p>';
  const o = g.offtask, tot = Math.max(1e-9, o.claimed.cost + o.off.cost + o.unclaimed.cost);
  const off = bars([['claimed','On a claimed task'],['off','Off the claimed task'],['unclaimed','No task claimed']].map(([k, l]) => ({k: `${l} (${mins(o[k].time)})`, v: o[k].cost, t: money(o[k].cost)})))
    + (o.files.length ? `<div class="burn-n">Files that made spend off-task:</div>` + o.files.slice(0, 8).map(f => `<div class="burn-drow"><span class="burn-dk"><code>${esc(f.path)}</code></span><span class="burn-dv">${grp(f.responses)} resp.</span><span class="burn-dv">${esc(money(f.cost))}</span></div>`).join('') : '')
    + `<div class="burn-n">Tasks held: ${esc(o.tasks.join(', ') || 'none')}${o.pathless.length ? ' &middot; no file named in '+esc(o.pathless.join(', '))+', so its spend counts as claimed' : ''}</div>`;
  const i = g.idle;
  const idle = `<div class="burn-n">Idle ${esc(mins(i.idle_s))} in ${i.gaps} gaps over 5 minutes &middot; cache re-written after idle ${i.rewrites} times, ${esc(fmt(i.rewrite_tokens))} tokens, ${esc(money(i.rewrite_cost))} &middot; waiting on the owner ${esc(mins(i.owner_wait_s))} over ${i.owner_waits} prompts</div>`
    + i.top_rewrites.map(w => `<div class="burn-drow"><span class="burn-dk">${esc(stamp(w.ts))} after ${esc(mins(w.idle_s))} idle${w.thread ? ' <small>agent '+esc(String(w.thread).slice(0,8))+'</small>' : ''}</span><span class="burn-dv">${esc(fmt(w.tokens))}</span><span class="burn-dv">${w.cost != null ? esc(money(w.cost)) : '&ndash;'}</span></div>`).join('')
    + i.questions.map(q => `<div class="burn-n">Question ${esc(q.id)} (${esc(q.status || 'recorded')}) at ${esc(stamp(q.ts))}: ${esc(short(q.text, 120))}</div>`).join('');
  const r = g.runaway;
  const run = (r.bins.length || r.agents.length) ? r.bins.map(b => `<div class="burn-n">${esc(stamp(b.start))}: ${esc(fmt(b.tokens))} tokens in 15 minutes, ${esc(b.ratio.toFixed(1))}&times; the session&#39;s median ${esc(fmt(b.median))}</div>`).join('') + r.agents.map(a => `<div class="burn-n">Agent ${esc(String(a.agent).slice(0,8))}: ${esc(fmt(a.rate))} per active minute, ${esc(a.ratio.toFixed(1))}&times; the median agent</div>`).join('') : `<p class="burn-empty">No 15-minute step above 5&times; this session&#39;s median (${esc(fmt(r.median || 0))}) and no agent far above its peers.</p>`;
  const coll = g.collisions.length ? g.collisions.map(c => `<div class="burn-n"><code>${esc(c.path)}</code> with ${esc(sidShort(c.a === x.sid ? c.b : c.a))}, ${esc(stamp(c.start))} to ${esc(clock(c.end))}</div>`).join('') : '<p class="burn-empty">No file edited by another session within 15 minutes.</p>';
  const fr = g.friction.items.length ? g.friction.items.slice(0, 10).map(f => `<div class="burn-sig"><b>${esc(f.label)}</b> &middot; ${esc(stamp(f.ts))} &middot; ${f.event ? 'record event #'+f.event : convLink(x, f.thread, f.line)}<code>${esc(short(f.text, 160))}</code></div>`).join('') : '<p class="burn-empty">No git guard denial, hook denial, style rejection, refused phase gate or denied permission was recorded.</p>';
  const rec = Object.entries(g.hooks.recorded || {}).map(([n, v]) => `${esc(n)} ${grp(v.runs)} runs ${esc(mins(v.ms/1000))}`).join(', ');
  const hooksBody = (Object.keys(g.hooks.per_hook).length ? Object.entries(g.hooks.per_hook).sort((a, b) => b[1].ms - a[1].ms).map(([n, v]) => `<div class="burn-drow"><span class="burn-dk">${esc(n)}</span><span class="burn-dv">${grp(v.runs)} runs</span><span class="burn-dv">${esc(mins(v.ms/1000))}${v.failures ? ' &middot; '+v.failures+' failed' : ''}</span></div>`).join('') : '<p class="burn-empty">No Alpaca hook run in the transcripts for this window.</p>')
    + (rec ? `<div class="burn-n">Alpaca&#39;s own record (all time): ${rec}</div>` : '')
    + g.hooks.pool_failures.map(f => `<div class="burn-n">Hook pool: ${esc(f.hook)} failed (${esc(f.error)}) at ${esc(stamp(f.ts))}</div>`).join('');
  const out = g.outcomes.length ? g.outcomes.map(t => `<div class="burn-drow"><span class="burn-dk">${esc(t.task)}${t.reopened ? ' <span class="burn-flag">reopened</span>' : ''} <small>done ${esc(stamp(t.done))}</small></span><span class="burn-dv">${esc(fmt(t.tokens))}</span><span class="burn-dv">${esc(money(t.cost))}</span></div>`).join('') : '<p class="burn-empty">This session held no task that reached done.</p>';
  return `<div class="burn-run"><div class="burn-runhead"><h4>Off-track signals</h4><p class="prose">From this session&#39;s transcripts, its subagents and the record, over the window. Links open the conversation at the line.</p></div>
    <div class="burn-dgrid burn-sigrid">${block('Loops and thrash', loops)}${block('Wasted spend', waste)}${block('Claimed, off-task and unclaimed spend', off)}${block('Idle, cache re-writes and owner wait', idle)}${block('Runaway', run)}${block('Collisions', coll)}${block('Rule friction', fr)}${block('Hook overhead', hooksBody)}${block('Cost per outcome, tasks it worked on', out)}</div></div>`;
}
function table(d, href) {
  const rows = d.sessions.map((x, i) => {
    const isOpen = open.has(x.sid), id = 'burn-det-' + i;
    return `<tr class="${x.share > 50 ? 'peak' : ''}" data-key="burn-row-${esc(x.sid)}" id="burn-row-${esc(x.sid)}"><td class="burn-sid"><button class="burn-exp" type="button" aria-expanded="${isOpen}" aria-controls="${id}" data-burn-open="${esc(x.sid)}"><span class="burn-car" aria-hidden="true"></span><span>${esc(short(x.title, 70))}<small>${esc(sidShort(x.sid))}${x.runs && x.runs.length ? ' &middot; '+x.runs.length+' workflow run'+(x.runs.length === 1 ? '' : 's') : ''}</small></span></button></td>
      <td>${esc(x.person)}</td><td class="burn-desc">${esc(short(x.what || x.title, 140))}</td><td class="num">${esc(span(x.span_s))}</td><td class="num">${grp(x.requests)}</td><td class="num">${x.subagents ? grp(x.subagents) : '&ndash;'}</td>
      <td class="num">${grp(x.tokens)}</td><td class="num">${grp(Math.round(x.rate))}</td><td class="num">${esc(money(x.cost_usd))}</td></tr>
      <tr class="burn-det" id="${id}" data-key="burn-det-${esc(x.sid)}" data-burn-sid="${esc(x.sid)}" ${isOpen ? '' : 'hidden'}><td colspan="9">${isOpen ? breakdown(d, x, href) : ''}</td></tr>`;
  }).join('');
  const r = d.rest, rest = r.sessions ? `<tr class="burn-rest"><td class="burn-sid">${grp(r.sessions)} smaller sessions</td><td>everyone else</td><td class="burn-desc">Everything else in the same window, added together.</td><td class="num">&ndash;</td><td class="num">${grp(r.requests)}</td><td class="num">&ndash;</td><td class="num">${grp(r.tokens)}</td><td class="num">&ndash;</td><td class="num">${esc(money(r.cost_usd))}</td></tr>` : '';
  const crd = d.totals.tokens ? 100 * d.sessions.reduce((a,x) => a + x.kinds.cache_read, 0) / Math.max(1, d.sessions.reduce((a,x) => a + x.tokens, 0)) : 0;
  return `<section class="panel burn-card"><div class="burn-head"><h2>The same numbers as a table</h2><p>The ${grp(d.sessions.length)} heaviest sessions by tokens, times in your browser's zone. <b>Select a session to open its breakdown:</b> how much went to the main thread, to workflow subagents and to other subagents, which models it used, what kind of tokens they were, and how each workflow run fanned out, one bar per agent. The breakdown links to the session's own ledger and conversation.</p></div>
    <div class="table-wrap"><table class="data-table burn-table"><thead><tr><th scope="col">Session</th><th scope="col">Person</th><th scope="col">What it did</th><th scope="col">Span</th><th scope="col">Requests</th><th scope="col">Subagents</th><th scope="col">Tokens</th><th scope="col">Tok / min</th><th scope="col">Est. cost</th></tr></thead><tbody>${rows}${rest}</tbody></table></div>
    <p class="burn-foot">Source: this project's own transcripts, each session's main conversation plus every subagent transcript the record or the project copies place under it (${grp(d.source.sessions_read)} sessions read${d.source.children_unreadable ? ', '+grp(d.source.children_unreadable)+' recorded subagents without a readable transcript' : ''}), summed over ${esc(d.source.token_definition)}, and each workflow run record (workflows/wf_*.json beside the session transcript, or its project copy). Cache reads are ${esc(pct(crd))} of the tokens above, which is why a long-running subagent costs so much: every turn replays the context it already holds. Person is the account in the transcript path, else the operator. Cost is an API list-price estimate from Alpaca's rate cards, not a bill.</p></section>`;
}

// The whole board. `href(sid)` opens a session's detail; `windowHref(key)` switches the window.
// `rangeLabel` names a day range from the Sessions toolbar: no window chip is then current.
export function renderBurn(d, {href, windowHref, childLink, rangeLabel = ''}) {
  last = d; lastHref = href; if (childLink) childHref = childLink;
  const w = d.window, t = d.totals, top = t.top;
  const tabs = WINDOWS.map(([k,l]) => `<a class="tab${w.key === k ? ' active' : ''}" href="${esc(windowHref(k))}" ${w.key === k ? 'aria-current="page"' : ''}>${esc(l)}</a>`).join('')
    + (w.key === 'range' ? `<span class="tab active" aria-current="page">${esc(rangeLabel || 'Chosen days')}</span>` : '');
  const stand = t.first ? `Every session that made a request between ${esc(stamp(w.key === 'all' ? t.first : w.start))} and ${esc(stamp(w.key === 'all' ? t.last : Math.min(w.now, w.end)))}, in ${esc(resolution(w.bucket_s).replace('-',' '))} steps.${top ? ` One session, <b>${esc(short(top.title, 70))}</b>, took <b>${esc(pct(top.share))}</b> of the ${esc(fmt(t.tokens))} tokens spent.` : ''}` : 'No session made a request in this window.';
  return `<div class="burn">
    <div class="burn-top"><div><span class="eyebrow">TOKEN AND COST BOARD</span><p class="burn-stand">${stand}${d.refreshing ? ' <span class="burn-n">Updating&hellip;</span>' : ''}</p></div><nav class="tabs burn-tabs" aria-label="Time window">${tabs}</nav></div>
    ${tiles(d)}${lanes(d)}${push(d)}${why(d)}${signalsCard(d)}${outcomesCard(d)}${sharedCard(d)}${table(d, href)}</div>`;
}

// ---- interaction: tooltip, lanes that open table rows, expandable rows ----
let tip = null;
function tipEl() { if (!tip) { tip = document.createElement('div'); tip.id = 'burn-tip'; tip.setAttribute('role', 'status'); document.body.append(tip); } return tip; }
function show(html, x, y) {
  const t = tipEl(); t.innerHTML = html; t.classList.add('on');
  const r = t.getBoundingClientRect();
  let left = x + 14, top = y + 14;
  if (left + r.width > innerWidth - 8) left = x - r.width - 14;
  if (top + r.height > innerHeight - 8) top = y - r.height - 14;
  t.style.left = Math.max(8, left) + 'px'; t.style.top = Math.max(8, top) + 'px';
}
function hide() { if (tip) tip.classList.remove('on'); }
function laneTip(lane, ev) {
  const x = last.sessions[Number(lane.dataset.burnLane)], track = lane.querySelector('.burn-track');
  if (!x || !track) return;
  const w = last.window, cw = cellWidth(w.buckets), r = track.getBoundingClientRect();
  const k = Math.floor((ev.clientX - r.left) / cw);
  if (k < 0 || k >= w.buckets) return hide();
  const v = x.bins[String(k)] || 0, e = (x.events || {})[String(k)];
  const a = w.start + k * w.bucket_s;
  show(`<span class="tt">${esc(clock(a))} &ndash; ${esc(clock(a + w.bucket_s))}, ${esc(day(a))}</span><span class="tv">${v ? esc(grp(v))+' tokens' : 'idle, no request'}</span><span class="tn">${esc(sidShort(x.sid))} &middot; ${esc(x.person)}${e ? ` &middot; ${e.refusals} refused, ${e.retries} retried` : ''}</span>`, ev.clientX, ev.clientY);
}
function growthTip(el, ev) {
  const c = last && last.crew; if (!c || !c.growth.length) return;
  const area = el.querySelector('.burn-plotarea').getBoundingClientRect(), G = c.growth, T = G[G.length-1].t;
  const t = Math.min(T, Math.max(1, Math.round(1 + (T - 1) * (ev.clientX - area.left) / area.width)));
  const g = G.find(v => v.t === t); if (!g) return;
  show(`<span class="tt">turn ${g.t}</span><span class="tv">${esc(grp(g.med))} tokens of context</span><span class="tn">middle half ${Math.round(g.q1/1000)}k&ndash;${Math.round(g.q3/1000)}k &middot; ${g.n} agents still running &middot; ${esc(fmt(g.cost))} spent on this turn</span>`, ev.clientX, ev.clientY);
}
function toggle(sid, force) {
  const btn = document.querySelector(`[data-burn-open="${CSS.escape(sid)}"]`); if (!btn) return;
  const now = force ?? btn.getAttribute('aria-expanded') !== 'true';
  btn.setAttribute('aria-expanded', String(now));
  const det = document.getElementById(btn.getAttribute('aria-controls'));
  if (det) {
    // The breakdown, with its fan-out view, is built the first time it opens.
    const cell = det.querySelector('td'), x = last && last.sessions.find(v => v.sid === sid);
    if (now && cell && !cell.firstElementChild && x) cell.innerHTML = breakdown(last, x, lastHref);
    det.hidden = !now;
  }
  if (now) open.add(sid); else open.delete(sid);
  return btn;
}
document.addEventListener('mousemove', ev => {
  if (!last || !(ev.target instanceof Element)) return;
  const lane = ev.target.closest('.burn-lane');
  if (lane) return laneTip(lane, ev);
  const growth = ev.target.closest('[data-burn-growth]');
  if (growth) return growthTip(growth, ev);
  const t = ev.target.closest('.burn [data-tip]');
  if (t) return show(t.getAttribute('data-tip'), ev.clientX, ev.clientY);
  hide();
});
addEventListener('scroll', hide, true);
document.addEventListener('click', ev => {
  if (!(ev.target instanceof Element)) return;
  const btn = ev.target.closest('[data-burn-open]');
  if (btn) { toggle(btn.dataset.burnOpen); return; }
  const run = ev.target.closest('[data-burn-open-run]');
  if (run && last) { const b = toggle(run.dataset.burnOpenRun, true); b?.closest('tr')?.nextElementSibling?.scrollIntoView({block:'start', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'}); return; }
  const lane = ev.target.closest('.burn-lane');
  if (lane && last) { const x = last.sessions[Number(lane.dataset.burnLane)]; if (x) { const b = toggle(x.sid, true); b?.closest('tr')?.scrollIntoView({block:'center', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'}); } }
});
document.addEventListener('keydown', ev => {
  if ((ev.key === 'Enter' || ev.key === ' ') && ev.target instanceof Element && ev.target.matches('.burn-lane, .burn-flane')) { ev.preventDefault(); ev.target.click(); }
});
