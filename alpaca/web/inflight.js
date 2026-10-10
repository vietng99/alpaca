// In-flight tab: one card per task in progress, newest activity first. Data: /hub/inflight.json
// (alpaca/analytics/inflight.py). Every value on a card is recorded or follows a fixed rule; nothing is
// generated. All dynamic text is escaped; colors, sizes and spacing come from theme.css tokens.
import {orb} from './orb.js';
import {levelChip} from './autodrive.js';
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const grp = n => Number(n || 0).toLocaleString();
const fmt = n => { n = Number(n || 0); return n >= 1e9 ? (n/1e9).toFixed(2)+' B' : n >= 1e6 ? (n/1e6).toFixed(1)+' M' : n >= 1e3 ? Math.round(n/1e3)+' k' : String(Math.round(n)); };
const money = v => v == null ? '&ndash;' : esc(new Intl.NumberFormat(undefined,{style:'currency',currency:'USD',minimumFractionDigits:2,maximumFractionDigits:2}).format(v));
const clock = s => s ? new Date(s*1000).toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'}) : '';
const stamp = s => s ? new Date(s*1000).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : 'Not recorded';
const span = s => { s = Math.max(0, Math.round(Number(s || 0) / 60)); const h = Math.floor(s/60), m = s%60; return h ? `${h} h ${String(m).padStart(2,'0')} m` : `${m} m`; };
const STATE = {done:'Done', doing:'In progress', ready:'Ready', open:'Ready', blocked:'Blocked', unknown:'Unknown'};
const KIND = {read:'Read', edit:'Edit', run:'Run', test:'Test', agent:'Agent', workflow:'Workflow', message:'Message', ask:'Owner', tool:'Tool'};

// Activity a reader loaded with "Show earlier", kept per task so a live refresh keeps it on screen.
const earlier = new Map();

// The in-flight count for the sidebar: a task in progress whose lease is still live, the same rule
// the server applies (alpaca/analytics/inflight.py _live).
export function inflightCount(tasks, now = Date.now()) {
  return (tasks || []).filter(t => t.status === 'doing' && t.claimant && t.lease_until && Date.parse(t.lease_until) > now).length;
}
// The link every "Open in-flight" button uses.
export function inflightHref(id) { return '#inflight?' + new URLSearchParams({task: id}); }
export function inflightButton(id, cls = 'button') {
  return `<a class="${cls} inflight-link" href="${esc(inflightHref(id))}">${orb('working',20)}<span>Open in-flight</span></a>`;
}

function chip(state) { return `<span class="if-state if-state-${esc(state)}">${esc(STATE[state] || state)}</span>`; }
function taskRef(t) { return t ? `<li><button type="button" data-task="${esc(t.id)}">${esc(t.id)} ${esc(t.title)}</button>${chip(t.status)}</li>` : ''; }

function progress(p) {
  if (p.percent == null) return `<div class="if-progress none"><div class="if-pct">&ndash;</div><div class="if-plabel">${esc(p.label)}</div></div>`;
  const pct = Math.max(0, Math.min(100, p.percent));
  const phases = (p.phases || []).filter(x => x.total).map(x => `<div class="if-phase"><span>${esc(x.phase)}</span><span class="if-track"><i style="width:${100*x.done/x.total}%"></i></span><span>${x.done}/${x.total}${x.failed ? ', '+x.failed+' failed' : ''}</span></div>`).join('');
  return `<div class="if-progress ${esc(p.basis)}"><div class="if-pct">${Math.round(pct)}<small>%</small></div><div class="if-pmain"><div class="if-track big" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(pct)}" aria-label="${esc(p.label)}"><i style="width:${pct}%"></i></div><div class="if-plabel">${esc(p.label)}</div>${phases}</div></div>`;
}

function milestones(list) {
  return `<ol class="if-miles">${list.map(m => `<li class="${m.ts ? 'reached' : 'pending'}"><i aria-hidden="true"></i><span>${esc(m.label)}</span><small>${m.ts ? esc(stamp(m.ts)) : 'not yet'}</small></li>`).join('')}</ol>`;
}

function feedLines(lines) {
  return lines.map(l => `<li class="if-line if-${esc(l.kind)}${l.ok === false ? ' bad' : ''}"><time>${esc(clock(l.ts))}</time><span class="if-kind">${esc(KIND[l.kind] || l.kind)}</span><span class="if-text">${esc(l.text)}</span></li>`).join('');
}

function contract(c) {
  if (!c) return '<p class="if-muted">No task contract is recorded.</p>';
  const list = (title, items) => items && items.length ? `<h5>${esc(title)}</h5><ul>${items.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : '';
  return list('Input', c.input) + list('Expected', c.expected) + list('Done bar', c.done_bar) + list('Fail cases', c.fail_cases);
}

function signals(c) {
  const s = c.signals || {}, out = [];
  for (const f of s.loops || []) out.push(`<li class="warn">Loop ${esc(f.rule)}: ${esc(f.label)} &times;${f.count} <code>${esc(String(f.call).slice(0, 120))}</code></li>`);
  for (const b of s.runaway || []) out.push(`<li class="warn">Runaway at ${esc(clock(b.start))}: ${esc(fmt(b.tokens))} tokens in 15 minutes, ${esc(b.ratio.toFixed(1))}&times; the session&#39;s median</li>`);
  for (const f of s.friction || []) out.push(`<li class="warn">${esc(f.label)} at ${esc(clock(f.ts))}</li>`);
  const idle = s.idle || {};
  if (idle.quiet_s != null && idle.quiet_s > 600) out.push(`<li class="warn">No model response for ${esc(span(idle.quiet_s))}</li>`);
  if (idle.gaps) out.push(`<li>Idle ${esc(span(idle.idle_s))} in ${idle.gaps} gaps over 5 minutes since the claim</li>`);
  return out.length ? `<ul class="if-signals">${out.join('')}</ul>` : '<p class="if-muted">No loop, runaway, friction or long silence since the claim.</p>';
}

// Needs attention: a loop, a runaway burst, friction, a silence over 10 minutes (the warn lines of
// signals() above), or a lease that lapses within 10 minutes.
function alert(c, now = Date.now()) {
  const s = c.signals || {}, quiet = (s.idle || {}).quiet_s, lease = Date.parse(c.lease_until);
  return Boolean((s.loops || []).length || (s.runaway || []).length || (s.friction || []).length
    || (quiet != null && quiet > 600) || (lease && lease - now < 600000));
}

function workflows(list, id) {
  if (!list.length) return '<p class="if-muted">No workflow run started since the claim, and none is running.</p>';
  return list.map(w => {
    const total = Math.max(1, w.planned || w.agents);
    const head = w.live
      ? `${grp(w.agents)} agents so far: ${grp(w.done)} finished, ${grp(w.running)} still open. The planned count is written only when the run ends.`
      : `${esc(stamp(w.start))} &middot; ${grp(w.done)} done, ${grp(w.running)} running, ${grp(w.failed)} failed of ${grp(w.planned)} &middot; up to ${grp(w.peak)} at once`;
    const mine = w.live && w.task_agents ? `<div class="if-phase"><span>For ${esc(id)}</span><span class="if-track"><i style="width:${100*w.task_done/Math.max(1, w.task_agents)}%"></i></span><span>${w.task_done}/${w.task_agents}</span></div>` : '';
    return `<div class="if-run"><div class="if-runhead"><strong>${esc(w.name)}</strong><span class="if-state if-state-${w.status === 'completed' ? 'done' : w.status === 'running' ? 'doing' : 'blocked'}">${esc(w.status)}</span><small>${head}</small></div>
    <div class="if-track big seg"><i class="done" style="width:${100*w.done/total}%"></i><i class="failed" style="width:${100*(w.failed || 0)/total}%"></i><i class="running" style="width:${100*w.running/total}%"></i></div>
    ${mine}${w.phases.map(p => `<div class="if-phase"><span>${esc(p.title)}</span><span class="if-track"><i style="width:${100*p.done/Math.max(1, p.agents)}%"></i></span><span>${p.done}/${p.agents}${p.failed ? ', '+p.failed+' failed' : ''}</span></div>`).join('')}</div>`;
  }).join('');
}

function card(c, focus, href) {
  const t = c.tokens || {}, mode = c.mode || {}, tests = c.tests || {runs: []};
  const more = earlier.get(c.id);
  const lines = c.feed.concat(more ? more.lines : []);
  const start = more ? more.start : c.feed_start;
  const complete = more ? more.complete : c.feed_complete;
  const trend = tests.runs.length ? tests.runs.map(r => `<span class="if-test ${r.ok ? 'ok' : 'bad'}" title="${esc(stamp(r.ts))}">${r.ok ? (r.passed != null ? r.passed+' passed' : 'pass') : r.failed+' failed'}</span>`).join('') : '';
  const stat = (k, v, n = '') => `<div class="if-stat"><span>${k}</span><strong>${v}</strong>${n ? `<small>${n}</small>` : ''}</div>`;
  // Live work rides the beam; a card whose session shows off-track signals or a lease about to lapse rides the red one.
  return `<article class="panel if-card beam beam-inside${alert(c) ? ' beam-alert' : ''}${focus ? ' focus' : ''}" id="if-${esc(c.id)}" data-key="if-${esc(c.id)}">
    <header class="if-head"><div class="if-title">${orb('working',20)}<div><span class="eyebrow">${esc(c.id)} &middot; ${esc(c.op)}${c.op_phase ? ' &middot; phase '+esc(c.op_phase) : ''}${c.workstream ? ' &middot; '+esc(c.workstream.title) : ''}</span><h2><button type="button" data-task="${esc(c.id)}">${esc(c.title)}</button></h2></div></div>
      <div class="if-meta">${levelChip(c.level)}<span>Running <strong>${esc(span(c.running_s))}</strong></span><span>since ${esc(stamp(c.claimed_at))}</span>${c.session ? `<a class="text-link" href="${esc(href('sessions', {sid: c.session}))}">Session ${esc(c.session.slice(0, 8))}</a>` : '<span>No claiming session recorded</span>'}<span>Lease to ${esc(stamp(Date.parse(c.lease_until)/1000))}</span></div></header>
    ${progress(c.progress)}
    <div class="if-now"><span class="if-mode">${esc(mode.mode || 'no activity yet')}</span><p>${c.now ? esc(c.now) : 'No status line from the session since the claim.'}</p></div>
    ${c.shared_with.length ? `<p class="if-note">This session also holds ${c.shared_with.map(esc).join(', ')}: the activity below is the session&#39;s, not this task&#39;s alone.</p>` : ''}
    ${!c.available ? '<p class="if-note">The claiming session has no readable transcript, so its activity cannot be shown.</p>' : ''}
    ${milestones(c.milestones)}
    <div class="if-stats">${stat('Responses', grp(c.responses))}${stat('Tokens', esc(fmt(c.total_tokens)), `${esc(fmt(t.cache_read))} cache read, ${esc(fmt(t.cache_write))} written, ${esc(fmt(t.output))} out`)}${stat('Estimated cost', money(c.cost_usd))}${stat('Tool calls', grp(c.tool_calls))}${stat('Subagents started', grp(c.subagents))}${stat('Workflow runs', grp(c.workflows.length), c.workflows.some(w => w.live) ? grp(c.workflows.filter(w => w.live).length)+' running now' : '')}</div>
    <div class="if-grid">
      <section class="if-feed"><h3>What it is doing</h3><ol>${feedLines(lines) || '<li class="if-muted">No tool call since the claim.</li>'}</ol>
        ${!complete && start ? `<button type="button" class="button" data-if-earlier="${esc(c.id)}" data-before="${esc(start)}">Show earlier</button>` : ''}</section>
      <div class="if-side">
        <section><h3>Direction</h3><dl class="if-dl"><dt>Mode</dt><dd>${mode.mode ? esc(mode.mode) : '&ndash;'}${mode.window ? ` <small>from the last ${mode.window} tool calls</small>` : ''}</dd><dt>Focus</dt><dd>${(c.focus || []).map(([f, n]) => `<code>${esc(f)}</code> <small>${n}</small>`).join(' ') || 'No file touched yet'}</dd><dt>Tests</dt><dd>${esc(tests.verdict)}${tests.total ? ` <small>${tests.total} runs</small>` : ''}<div class="if-tests">${trend}</div></dd></dl></section>
        <section><h3>Off-track signals</h3>${signals(c)}</section>
        <section><h3>Plan</h3><dl class="if-dl"><dt>Prerequisites</dt><dd><ul class="if-tasks">${c.prerequisites.map(taskRef).join('') || '<li class="if-muted">None recorded</li>'}</ul></dd><dt>Previous step</dt><dd><ul class="if-tasks">${taskRef(c.stream_prev) || '<li class="if-muted">First in its workstream</li>'}</ul></dd><dt>Next step</dt><dd><ul class="if-tasks">${taskRef(c.stream_next) || '<li class="if-muted">Last in its workstream</li>'}</ul></dd><dt>Unblocks</dt><dd><ul class="if-tasks">${c.successors.map(taskRef).join('') || '<li class="if-muted">No task waits on it</li>'}</ul></dd></dl></section>
        <section><h3>Workflow fan-out</h3>${workflows(c.workflows, c.id)}</section>
        <section><h3>Status notes</h3>${c.status.length ? `<ol class="if-notes">${c.status.map(s => `<li><time>${esc(clock(s.ts))}</time>${esc(s.text)}</li>`).join('')}</ol>` : '<p class="if-muted">None since the claim.</p>'}</section>
        ${c.prompts.length ? `<section><h3>Owner instructions</h3><ol class="if-notes">${c.prompts.map(s => `<li><time>${esc(clock(s.ts))}</time>${esc(s.text)}</li>`).join('')}</ol></section>` : ''}
        <details class="if-contract"><summary>Statement and contract</summary><p>${esc(c.statement)}</p>${contract(c.contract)}</details>
      </div></div></article>`;
}

// The whole tab. `focus` is the task named in #inflight?task=..., `href(page, params)` builds hub links.
export function renderInflight(d, {focus = null, href}) {
  const cards = d.tasks || [];
  const head = `<div class="if-summary"><span class="if-live">${orb('working',20)}${grp(cards.length)} in flight</span><span>Updated ${esc(clock(d.generated))}, every 15 seconds. Every line is read from the record and the claiming session&#39;s transcripts; nothing is generated.</span></div>`;
  if (!cards.length) return head + '<div class="empty">'+orb('breathing',32)+'<h3>Nothing is in flight</h3><p>No task is in progress with a live lease. A claimed task appears here with its activity.</p></div>';
  return head + `<div class="if-cards">${cards.map(c => card(c, c.id === focus, href)).join('')}</div>`;
}

// "Show earlier": read the chunk of the session transcript before the oldest line shown.
export async function loadEarlier(api, id, before) {
  const got = await api('/hub/inflight.json', {task: id, before});
  const prev = earlier.get(id);
  earlier.set(id, {lines: (prev ? prev.lines : []).concat(got.feed), start: got.start, complete: got.complete});
}
