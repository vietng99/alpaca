// Records: the project's official records and its wiki, read-only. Data:
// /hub/records.json (alpaca/hub.py records: operations, decisions, sealed proofs), and on the Wiki
// tab /hub/wiki-status.json and /hub/wiki-briefing.json (alpaca/hub.py wiki_status, wiki_briefing), and
// with the vault open /hub/graph.json (alpaca/hub.py evidence_graph, drawn by evgraph.js).
// Nothing here writes: every link opens an existing reader (the task record, the Library reader, the
// finished operation on the Roadmap). All dynamic text is escaped; colors come from theme.css.
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = v => Number(v || 0).toLocaleString();
export const SECTIONS = [['ops', 'Operations'], ['decisions', 'Decisions'], ['proofs', 'Sealed proofs'], ['wiki', 'Wiki']];

function tabs(section, counts, href) {
  return `<nav class="tabs records-tabs" aria-label="Record sections">${SECTIONS.map(([id, label]) => {
    const n = counts && id in counts ? ` <span class="tab-count">${num(counts[id])}</span>` : '';
    return `<a class="tab ${id === section ? 'active' : ''}" href="${esc(href('records', id === 'ops' ? {} : {section: id}))}"${id === section ? ' aria-current="page"' : ''}>${label}${n}</a>`;
  }).join('')}</nav>`;
}

const opName = x => x.short_title || String(x.title || '').split(':')[0].trim() || x.id;

function operations(items, c) {
  if (!items.length) return c.empty('No operations recorded', 'An operation starts with bin/alpaca op new "intent".');
  const rows = items.map(x => {
    const open = x.status !== 'closed';
    const target = open ? c.href('work', {op: x.id}) : c.href('roadmap', {view: 'finished', op: x.id});
    return `<tr><td><span class="mono">${esc(x.id)}</span></td>
      <td><a class="row-title" href="${esc(target)}">${esc(opName(x))}</a><span class="row-sub">${esc(x.title || '')}</span></td>
      <td><span class="badge ${open ? 'open' : 'done'}">${open ? 'Open' : 'Closed'}</span></td>
      <td class="rec-date">${esc(c.date(x.opened))}</td><td class="rec-date">${x.closed ? esc(c.date(x.closed)) : '&ndash;'}</td>
      <td class="num">${num(x.tasks_done)} / ${num(x.tasks_total)}</td>
      <td><a class="text-link" href="${esc(c.href('records', {section: 'wiki', op: x.id}))}">Briefing</a></td></tr>`;
  }).join('');
  return `<div class="table-wrap"><table class="data-table rec-table"><thead><tr><th>Op</th><th>Operation</th><th>Status</th><th>Opened</th><th>Closed</th><th class="num">Tasks done</th><th>Wiki</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function decisions(items, c) {
  if (!items.length) return c.empty('No decisions recorded', 'A decision is recorded with its context, options and choice (bin/alpaca decide).');
  return `<div class="rec-decisions">${items.map(d => `<article class="rec-decision">
    <div class="rec-decision-head"><span class="rec-kind">${esc(d.kind || 'decision')}</span><time datetime="${esc(d.ts || '')}">${esc(c.date(d.ts))}</time>${d.ref ? `<span class="mono rec-ref">${esc(d.ref)}</span>` : ''}</div>
    <p class="rec-choice">${esc(d.choice || d.text || '')}</p>
    ${d.consequence ? `<p class="rec-consequence">${esc(d.consequence)}</p>` : ''}
    ${d.context || d.options.length ? `<details class="rec-more"><summary>Context and options</summary>${d.context ? `<p>${esc(d.context)}</p>` : ''}${d.options.length ? `<ul class="rec-options">${d.options.map(o => `<li${o === d.choice ? ' class="chosen"' : ''}>${esc(o)}</li>`).join('')}</ul>` : ''}${d.pointer ? `<p class="mono rec-pointer">${esc(d.pointer)}</p>` : ''}</details>` : ''}
  </article>`).join('')}</div>`;
}

function proofs(items, c) {
  if (!items.length) return c.empty('No sealed proofs yet', 'A task is done with a sealed proof report: bin/alpaca proof seal TASK.');
  const rows = items.map(p => `<tr><td><button type="button" class="row-title mono" data-task="${esc(p.task)}">${esc(p.task)}</button></td>
    <td><span class="rec-title">${esc(p.title || '')}</span><span class="row-sub mono">${esc(p.op || '')}</span></td>
    <td class="rec-date">${esc(c.date(p.completed))}</td>
    <td>${p.path ? `<a class="text-link" href="${esc(c.href('library', {category: 'proofs', path: p.path}))}">Read report</a>` : `<span class="row-sub">${esc(p.pointer || '')}</span>`}</td></tr>`).join('');
  return `<div class="table-wrap"><table class="data-table rec-table"><thead><tr><th>Task</th><th>Work</th><th>Done</th><th>Report</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function wikiStatus(s, c) {
  if (!s) return '';
  if (s.error) return `<div class="notice"><div><h3>The wiki could not be read</h3><p>${esc(s.error)}</p></div></div>`;
  if (!s.available) return `<div class="notice"><div><h3>The wiki could not be read</h3><p>${esc(s.error || 'No status')}</p></div></div>`;
  const corpus = s.corpus || {}, serving = s.serving || {};
  const fact = (label, value) => `<div class="rec-fact"><span>${esc(label)}</span><strong>${value}</strong></div>`;
  return `<section class="panel rec-panel"><div class="panel-head"><div><h2>Project wiki</h2><p>What the wiki holds, read from its store without changing it.</p></div><span class="badge ${serving.state === 'available' ? 'pass' : 'blocked'}">${serving.state === 'available' ? 'Serving' : 'Not serving'}</span></div>
    <div class="panel-body rec-facts">${fact('Serving', esc(serving.state === 'available' ? 'Available' : 'Unavailable' + (serving.reason ? ': ' + serving.reason : '')))}
    ${fact('Documents', num(corpus.documents))}${fact('Active blocks', num(corpus.active_blocks))}${fact('Nodes', num(corpus.nodes))}${fact('Links', num(corpus.edges))}
    ${fact('Knowledge time from', esc(s.knowledge_time || 'unknown'))}${fact('Quarantined captures', num(s.quarantined_captures))}</div></section>`;
}

function briefing(b, subject, c) {
  if (!subject) return `<p class="route-hint">Name an operation or a task to read what the wiki knows about it, with a source for every line.</p>`;
  if (b && b.error) return `<div class="notice"><div><h3>No briefing for ${esc(subject)}</h3><p>${esc(b.error)}</p></div></div>`;
  if (!b) return '';
  const lines = String(b.text || '').split('\n').filter(Boolean);
  const marker = lines.shift() || '';
  const gapAt = lines.findIndex(l => l.startsWith('Gaps:'));
  const items = gapAt < 0 ? lines : lines.slice(0, gapAt), gaps = gapAt < 0 ? [] : lines.slice(gapAt);
  // the briefing is kept between reads: say when its inputs were read, and that a newer one is being built
  const asOf = b.built_at ? new Date(b.built_at * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}) : '';
  const note = (asOf ? ', as of ' + asOf : '') + (b.refreshing ? ', refreshing' : '');
  return `<section class="panel rec-panel"><div class="panel-head"><div><h2>Briefing: ${esc(subject)}</h2><p>${esc(marker)}</p></div><span class="row-sub">${num(b.items)} sourced items${b.truncated ? ', cut to fit' : ''}${esc(note)}</span></div>
    <div class="panel-body"><ul class="rec-brief">${items.map(l => `<li>${esc(l.replace(/^- /, ''))}</li>`).join('')}</ul>${gaps.length ? `<p class="rec-gaps">${esc(gaps.join(' '))}</p>` : ''}</div></section>`;
}

// The page body below its heading. `c` carries the hub's helpers (href, date, badge, empty, search)
// and the page state (section, q, subject, data, wiki, brief).
export function renderRecords(c) {
  const {section, data} = c;
  if (section === 'wiki') {
    // Search mode reads one briefing; Open vault shows everything as the evidence graph (evgraph.js,
    // mounted into #evidence-graph by the hub once this markup is on the page).
    if (c.vault) {
      const g = c.graph;
      const body = !g ? '' : g.error ? `<div class="notice"><div><h3>The vault could not be read</h3><p>${esc(g.error)}</p></div></div>`
        : `${g.wiki && !g.wiki.available ? `<p class="route-hint">Wiki claims are not shown: ${esc(g.wiki.reason || 'the wiki is unavailable')}.</p>` : ''}<div class="rec-vault-stage"><div id="evidence-graph" class="eg-host"></div></div>`;
      return tabs(section, data && data.counts, c.href) +
        `<div class="toolbar"><p class="row-sub">Every operation, task, sealed proof, the evidence it cites, decision and wiki claim. Pulses run from operations (left) toward the wiki (right).</p><a class="button rec-vault-switch" href="${esc(c.href('records', {section: 'wiki'}))}">Close vault</a></div>` + body;
    }
    // Search mode: the briefing search, the wiki's summary, and right below it the vault seal (t-602):
    // ventry.js plays the seal in this box and opens the vault when its dial is pressed; the card inside
    // is the plain way in (reduced motion, no script, keyboard)
    const vaultHref = c.href('records', {section: 'wiki', vault: 'open'});
    const door = `<div class="rec-seal" data-href="${esc(vaultHref)}"><a class="rec-door rec-vault-switch" href="${esc(vaultHref)}"><span class="rec-door-wheel" aria-hidden="true"></span><span class="rec-door-text"><strong class="rec-door-title">Open vault</strong><span>Every operation, task, decision, sealed proof, piece of evidence and wiki claim in one graph: 15 colour-coded columns, or the whole record as a 3D brain.</span></span><span class="rec-door-go" aria-hidden="true">&rarr;</span></a></div>`;
    return tabs(section, data && data.counts, c.href) +
      `<div class="toolbar">${c.search('Briefing for op-12 or t-345', c.subject || '', 'subject')}</div>` +
      wikiStatus(c.wiki, c) + door + briefing(c.brief, c.subject, c);
  }
  const body = section === 'decisions' ? decisions(data.items, c) : section === 'proofs' ? proofs(data.items, c) : operations(data.items, c);
  const more = data.next_offset != null ? `<p class="route-hint">Showing ${num(data.items.length)} of ${num(data.total)}. Search to narrow the list.</p>` : '';
  const placeholder = {ops: 'Search operations', decisions: 'Search decisions', proofs: 'Search sealed work'}[section];
  return tabs(section, data.counts, c.href) + `<div class="toolbar">${c.search(placeholder, c.q || '', 'q')}</div>` + body + more;
}
