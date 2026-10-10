// Version control tab: a read-only picture of the repository. Data: /hub/vcs.json
// (alpaca/gitview.py). All dynamic text is escaped; colors, sizes and spacing come from theme.css.
import {orb} from './orb.js';
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const grp = n => Number(n || 0).toLocaleString();
const stamp = s => s ? new Date(s*1000).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : 'Not recorded';
const age = (s, now = Date.now() / 1000) => { if (!s) return '&ndash;'; const m = Math.max(0, Math.round((now - s) / 60)); return m < 60 ? m+' min' : m < 2880 ? Math.round(m/60)+' h' : Math.round(m/1440)+' d'; };
const LANE = ['var(--chart-input)','var(--chart-output)','var(--chart-write)','var(--chart-reason)','var(--chart-cache)','var(--accent)'];
const ROW = 40, STEP = 14, PAD = 10;
const WINDOWS = [[1,'1 day'],[7,'7 days'],[30,'30 days'],[90,'90 days']];

// A commit message with its task and operation ids as links: a task opens its record (hub data-task).
export function linkIds(text) {
  return esc(text).replace(/\b(t-\d{3,})\b/g, '<button type="button" class="vc-id" data-task="$1">$1</button>')
                  .replace(/\b(op-\d{3,})\b/g, '<a class="vc-id" href="#work?op=$1">$1</a>');
}
function rel(path, root) { return root && path.startsWith(root + '/') ? path.slice(root.length + 1) : path === root ? '(main checkout)' : path; }
function sessionLink(s, href) { return s ? `<a class="text-link" href="${esc(href('sessions', {sid: s.sid}))}">${esc(s.sid.slice(0, 8))}</a><small>${esc(s.basis)}</small>` : '<span class="vc-muted">unknown</span>'; }
const card = (title, sub, body) => `<section class="panel vc-card"><div class="vc-head"><h2>${title}</h2>${sub ? `<p>${sub}</p>` : ''}</div>${body}</section>`;

function worktrees(d, href) {
  const rows = d.worktrees.map(w => `<tr><td class="vc-path"><code>${esc(rel(w.path, d.root))}</code>${w.is_main ? ' <span class="vc-chip main">main tree</span>' : ''}${w.locked ? ' <span class="vc-chip warn" title="'+esc(w.locked)+'">locked</span>' : ''}${w.prunable ? ' <span class="vc-chip warn" title="'+esc(w.prunable)+'">prunable</span>' : ''}</td>
    <td>${w.branch ? esc(w.branch) : '<span class="vc-chip">detached HEAD</span>'}</td><td class="num"><code>${esc(w.head_short)}</code><small>${age(w.head_time)} ago</small></td>
    <td class="num">${w.ahead == null ? '&ndash;' : `<span class="${w.ahead ? 'vc-ahead' : ''}">+${w.ahead}</span> / <span class="${w.behind ? 'vc-behind' : ''}">-${w.behind}</span>`}</td>
    <td class="num">${w.uncommitted ? `<span class="vc-dirty">${grp(w.uncommitted)}</span><small>oldest ${age(w.oldest_change)}</small>` : '0'}</td><td>${sessionLink(w.session, href)}</td></tr>`).join('');
  return card('Worktrees', `Ahead and behind are compared with ${esc(d.main)}. Session attribution uses the active working directory or latest recorded file edit; its basis appears below the session.`,
    `<div class="table-wrap"><table class="data-table vc-table"><thead><tr><th>Worktree</th><th>Branch</th><th>HEAD</th><th>Ahead / behind ${esc(d.main)}</th><th>Uncommitted</th><th>Session</th></tr></thead><tbody>${rows}</tbody></table></div>`);
}

function graph(d, days, windowHref, view) {
  const g = d.graph, n = g.commits.length, width = PAD * 2 + Math.max(0, g.lanes - 1) * STEP;
  const x = l => PAD + l * STEP, y = r => r * ROW + ROW / 2;
  const row = new Map(g.commits.map((c, i) => [c.sha, i]));
  const paths = g.commits.flatMap((c, i) => c.edges.map((e, k) => {
    const color = LANE[c.lane % LANE.length];
    if (e.inside && row.has(e.to)) {
      const j = row.get(e.to), xl = x(e.lane);
      return `<path d="M${x(c.lane)} ${y(i)} L${xl} ${y(i) + Math.min(ROW, ROW * (j-i)/2)} L${xl} ${y(j)}" stroke="${color}"/>`;
    }
    const end = x(c.lane) + (k % 2 ? 6 : -6);
    return `<path data-vc-boundary="${esc(e.to)}" d="M${x(c.lane)} ${y(i)} L${end} ${y(i)+13} m-3 -3 l3 3 l3 -3" stroke="${color}" stroke-dasharray="2 2"><title>Parent outside loaded history: ${esc(e.to)}</title></path>`;
  })).join('');
  const dots = g.commits.map((c, i) => `<circle cx="${x(c.lane)}" cy="${y(i)}" r="${c.merge ? 5 : 4}" fill="${c.merge ? 'var(--surface)' : LANE[c.lane % LANE.length]}" stroke="${LANE[c.lane % LANE.length]}"/>`).join('');
  const selected = view.selected || g.commits[0]?.sha;
  const rows = g.commits.map(c => `<li data-key="${esc(c.sha)}" style="height:${ROW}px"${selected === c.sha ? ' class="vc-selected"' : ''}><button type="button" class="vc-commit" data-vc-commit="${esc(c.sha)}" aria-pressed="${selected === c.sha}" aria-label="Open commit ${esc(c.short)}: ${esc(c.subject)}"><code>${esc(c.short)}</code><span class="vc-subject">${esc(c.subject)}</span></button><span class="vc-row-refs" title="${esc([...c.refs,...c.tags].join(', '))}">${esc(c.refs[0] || c.tags[0] || (c.merge ? 'merge' : ''))}${c.refs.length+c.tags.length>1 ? ` +${c.refs.length+c.tags.length-1}` : ''}</span><small>${esc(c.author)} &middot; ${age(c.time)}</small></li>`).join('');
  const tabs = WINDOWS.map(([k,l])=>`<a class="tab${k===days?' active':''}" ${k===days?'aria-current="page"':''} href="${esc(windowHref(k))}">${l}</a>`).join('');
  return `<section class="panel vc-card" id="vc-history"><div class="vc-head"><h2>Commit graph</h2><p>History across local branches. Select a commit to inspect it.</p></div>
    <div class="vc-bar"><nav class="tabs vc-tabs" aria-label="History window">${tabs}</nav><span class="vc-muted">${grp(n)} loaded commits &middot; ${grp(g.lanes)} lanes</span></div>
    <div class="vc-tools"><label>Find in loaded history <input class="filter-input" type="search" data-vc-search placeholder="Message, hash, author or branch" value="${esc(view.query || '')}"></label><button class="button" data-vc-next type="button">Next match</button><output class="vc-muted" data-vc-matches aria-live="polite"></output><button class="button" type="button" data-vc-expand aria-expanded="${Boolean(view.expanded)}">${view.expanded?'Compact view':'Expand all loaded commits'}</button></div>
    ${n ? `<div class="vc-history-layout"><div class="vc-history-main"><div class="vc-graph${view.expanded?' expanded':''}" tabindex="0" role="region" aria-label="Commit graph. Scroll to explore all loaded rows and lanes."><div class="vc-canvas"><svg class="vc-lanes" width="${width}" height="${n*ROW}" style="width:${width}px;height:${n*ROW}px" viewBox="0 0 ${width} ${n*ROW}" aria-hidden="true"><g fill="none" stroke-width="2">${paths}</g>${dots}</svg><ol class="vc-commits">${rows}</ol></div></div><div class="vc-graph-footer"><span>Hollow dot: merge &middot; dashed arrow: parent outside loaded history</span><span><button type="button" class="vc-id" data-vc-jump="newest">Newest</button> &middot; <button type="button" class="vc-id" data-vc-jump="oldest">Oldest loaded</button></span></div></div><aside class="vc-inspector" data-vc-inspector aria-label="Selected commit">${commitDetails(d,selected)}</aside></div>` : '<p class="vc-muted vc-pad">No commits in this window. Try a wider time window.</p>'}
    <p class="vc-coverage">${g.truncated ? `More commits exist in this window. Showing the first ${grp(n)} in topology order (limit 400).` : `Showing ${grp(n)} commits from local branches in the last ${days} days.`} Remote-only histories are not included. HEAD is used if there are no local branches.</p></section>`;
}
function commitDetails(d, sha) {
  const c=d.graph.commits.find(c=>c.sha===sha);
  if(!c) return '<h3>Commit unavailable</h3><p class="vc-muted">This commit is no longer in the loaded window. Select another row.</p>';
  const loaded=new Set(d.graph.commits.map(c=>c.sha));
  return `<h3>Selected commit ${c.merge?'<span class="vc-chip">merge</span>':''}</h3><button class="vc-id vc-back" type="button" data-vc-jump="selected">Back to selected row</button><p class="vc-detail-subject">${linkIds(c.subject)}</p><small class="vc-muted">Captured subject (up to 200 characters)</small><dl><dt>Commit</dt><dd><code>${esc(c.sha)}</code></dd><dt>Author</dt><dd>${esc(c.author)}</dd><dt>Committed</dt><dd>${esc(stamp(c.time))}</dd><dt>References</dt><dd>${[...c.refs,...c.tags].map(r=>`<span class="vc-chip ref">${esc(r)}</span>`).join(' ')||'No references at this commit'}</dd><dt>Parents</dt><dd>${c.parents.map(p=>loaded.has(p)?`<button type="button" class="vc-id" data-vc-commit="${esc(p)}">${esc(p.slice(0,8))}</button>`:`<span><code>${esc(p)}</code> <small>(outside loaded history)</small></span>`).join('<br>')||'Root commit'}</dd></dl>`;
}

function branchOverview(d) {
  const groups=[['Main',d.branches.filter(b=>b.main).length,'main'],['Merged into '+d.main,d.branches.filter(b=>!b.main&&b.merged).length,'merged'],['Not merged',d.branches.filter(b=>!b.main&&!b.merged).length,'open']];
  const max=Math.max(1,...groups.map(g=>g[1]));
  const bars=groups.map(([label,n,kind])=>`<div class="vc-status-row"><span>${esc(label)}</span><span class="vc-status-track"><i class="${kind}" style="width:${n/max*100}%"></i></span><strong>${grp(n)}</strong></div>`).join('');
  const loaded=new Set(d.graph.commits.map(c=>c.sha));
  const rows=d.branches.map(b=>`<tr><td><code>${esc(b.name)}</code>${b.main?' <span class="vc-chip main">main</span>':''}</td><td>${b.main?'Main':b.merged?'Merged':'Not merged'}</td><td class="num">${b.ahead==null?'Unknown':`+${grp(b.ahead)} / -${grp(b.behind)}`}</td><td>${loaded.has(b.head)?`<button class="vc-id" type="button" data-vc-commit="${esc(b.head)}">Open ${esc(b.head.slice(0,8))}</button>`:'Tip outside loaded history'}</td></tr>`).join('');
  return card('Branch status', `Compared with ${esc(d.main)}. Open a branch tip to locate it in the graph.`, `<div class="vc-status-bars">${bars}</div><details class="vc-disclosure" data-key="branch-directory"><summary>Browse all ${grp(d.branches.length)} local branches</summary><div class="table-wrap"><table class="data-table vc-table"><thead><tr><th>Branch</th><th>Status</th><th>Ahead / behind ${esc(d.main)}</th><th>Tip</th></tr></thead><tbody>${rows}</tbody></table></div></details>`);
}
function disclosure(key,title,sub,body,open=false) {
  return `<details class="panel vc-section" id="vc-${key}" data-key="${key}"${open?' open':''}><summary><strong>${esc(title)}</strong><span>${esc(sub)}</span></summary>${body}</details>`;
}

function uncommitted(d, href) {
  const groups = d.uncommitted.filter(g => !g.capped), capped = d.uncommitted.find(g => g.capped);
  const body = groups.map(g => `<div class="vc-group"><h3>${g.session ? `Session <a class="text-link" href="${esc(href('sessions', {sid: g.session}))}">${esc(g.session.slice(0, 8))}</a>` : 'Unattributed'} <small>${grp(g.files.length)} files</small></h3>
    <ul class="vc-files">${g.files.map(f => `<li><span class="vc-code">${esc(f.code)}</span><code>${esc(f.path)}</code><small>${f.touched ? 'edited '+age(f.touched)+' ago' : f.last_commit ? 'last commit '+age(f.last_commit)+' ago' : 'never committed'}</small></li>`).join('')}</ul></div>`).join('');
  return card(`Uncommitted in ${esc(d.main)}`, `Files changed in the main checkout, grouped by the session whose Edit or Write touched each one after the file's last commit. A file no transcript touched is unattributed.`,
    `<div class="vc-groups">${body || '<p class="vc-muted vc-pad">The main checkout has no uncommitted change.</p>'}${capped ? `<p class="vc-muted vc-pad">${grp(capped.capped)} more files were not attributed: the view reads at most 300.</p>` : ''}</div>`);
}

function activity(d) {
  const a = d.activity;
  const stash = a.stash.length ? `<div class="vc-warn"><strong>${grp(a.stash.length)} stash entr${a.stash.length === 1 ? 'y' : 'ies'}.</strong> Saved changes are listed below.<ul>${a.stash.map(s => `<li><code>${esc(s.ref)}</code> ${esc(stamp(s.ts))} &middot; ${esc(s.message)}</li>`).join('')}</ul></div>` : '<p class="vc-muted vc-pad">No stash entries.</p>';
  const items = a.items.map(e => `<li><time>${esc(stamp(e.ts))}</time><span class="vc-act vc-act-${esc(e.action)}">${esc(e.action)}</span><span class="vc-where">${esc(e.where)}</span><span class="vc-msg"><code>${esc(e.sha)}</code> ${esc(e.message || '(no message)')}</span></li>`).join('');
  return card('Git activity', 'From the reflogs of main and of each worktree&#39;s HEAD, newest first: commits, merges, resets, checkouts, rebases.', stash + `<ol class="vc-activity">${items}</ol>`);
}

function remotes(d) {
  const r = d.remotes;
  const track = r.tracking.map(t => `<tr><td><code>${esc(t.branch)}</code></td><td><code>${esc(t.upstream)}</code></td><td class="num">${t.gone ? '<span class="vc-chip warn">upstream gone</span>' : `+${t.ahead} / -${t.behind}`}</td></tr>`).join('');
  return card('Remotes, releases and tags', `Ahead and behind as of the last fetch; nothing is fetched here. FETCH_HEAD was written ${r.fetch_head ? age(r.fetch_head)+' ago ('+esc(stamp(r.fetch_head))+')' : 'never'}.`,
    `<div class="vc-cols"><div><h3>Remotes</h3><ul class="vc-list">${r.remotes.map(x => `<li><code>${esc(x.name)}</code> ${esc(x.url)}</li>`).join('') || '<li class="vc-muted">None</li>'}</ul>
     <h3>Branches with an upstream</h3>${track ? `<div class="table-wrap"><table class="data-table vc-table"><thead><tr><th>Branch</th><th>Upstream</th><th>Ahead / behind</th></tr></thead><tbody>${track}</tbody></table></div>` : '<p class="vc-muted">None</p>'}</div>
     <div><h3>Release branches</h3><ul class="vc-list">${r.release.map(x => `<li><code>${esc(x.ref)}</code> ${esc(x.sha)} &middot; ${age(x.time)} ago</li>`).join('') || '<li class="vc-muted">None</li>'}</ul>
     <h3>Tags</h3><ul class="vc-list">${r.tags.map(x => `<li><code>${esc(x.tag)}</code> ${esc(x.sha)} &middot; ${esc(stamp(x.time))}</li>`).join('') || '<li class="vc-muted">No tags</li>'}</ul></div></div>`);
}

function denials(d, href) {
  const rows = d.denials.map(x => `<li><time>${esc(stamp(x.ts))}</time><a class="text-link" href="${esc(href('sessions', {sid: x.session, view: 'conversation', around: String(x.line)}))}">${esc(x.session.slice(0, 8))}${x.thread ? ' agent '+esc(x.thread.slice(0, 8)) : ''}, line ${x.line}</a><span>${esc(x.text)}</span></li>`).join('');
  return card('Git guard denials', 'Commands the Alpaca git guard refused, found in the session transcripts of the last 30 days. Alpaca writes no record event for a denial, so the transcript line is the reference.',
    rows ? `<ol class="vc-denials">${rows}</ol>` : '<p class="vc-muted vc-pad">No git guard denial in the last 30 days of transcripts.</p>');
}

export function renderVcs(d, {days = 7, href, windowHref, view = {}}) {
  if (d.repository === false) return `<div class="empty">${orb('breathing',32)}<h3>No git repository</h3><p>${esc(d.root)} is not a git repository yet. Run git init there to see its branches, commits and local changes here.</p></div>`;
  const dirty=d.worktrees.reduce((n,w)=>n+w.uncommitted,0), dirtyTrees=d.worktrees.filter(w=>w.uncommitted).length;
  const attention=[
    [dirtyTrees,'worktrees with changes'],
    [d.worktrees.filter(w=>w.locked||w.prunable).length,'locked or prunable worktrees'],
    [d.remotes.tracking.filter(t=>t.gone||((t.ahead||0)>0&&(t.behind||0)>0)).length,'diverged or missing upstreams (last fetch)'],
    [d.activity.stash.length,'stash entries']
  ];
  return `<div class="vc-context"><code>${esc(d.root)}</code><span class="vc-muted">Read-only &middot; Updated ${esc(stamp(d.generated))}</span></div>
    <nav class="vc-section-nav" aria-label="Repository sections"><a href="#vc-branches" data-vc-section="branches">Branches</a><a href="#vc-worktrees" data-vc-section="worktrees">Worktrees</a><a href="#vc-changes" data-vc-section="changes">Local changes</a><a href="#vc-activity" data-vc-section="activity">Activity</a><a href="#vc-remotes" data-vc-section="remotes">Remotes</a></nav>
    ${graph(d,days,windowHref,view)}

    <div class="vc-overview" id="vc-branches">${branchOverview(d)}${card('Workspace signals', 'Counts across worktrees. Remote status reflects the last fetch.', `<div class="vc-signals">${attention.map(([n,l])=>`<div><strong>${grp(n)}</strong><span>${l}</span></div>`).join('')}</div><p class="vc-muted vc-pad">${grp(dirty)} changed-file entries across ${grp(d.worktrees.length)} worktrees. File details below cover the main checkout.</p>`)}</div>
    ${disclosure('worktrees',`Worktrees (${grp(d.worktrees.length)})`,`${grp(dirtyTrees)} with local changes`,worktrees(d,href))}
    ${disclosure('changes','Local changes',`Main checkout: ${d.main}`,uncommitted(d,href),true)}
    ${disclosure('activity','Git activity',`${grp(d.activity.items.length)} captured events`,activity(d))}
    ${disclosure('remotes','Remotes & releases',`${grp(d.remotes.tracking.length)} tracked branches`,remotes(d))}
    ${disclosure('guards','Git guard history',`${grp(d.denials.length)} captured denials`,denials(d,href))}`;
}

// State belongs to this mounted board, so navigating away starts a fresh view.
const boards=new WeakMap();
export function captureVcs(root) {
  const state=boards.get(root)||{}, graph=root.querySelector('.vc-graph');
  const anchor=graph && !state.expanded ? root.querySelectorAll('.vc-commits > li')[Math.floor(graph.scrollTop/ROW)]?.dataset.key : null;
  return {selected:state.selected,query:root.querySelector('[data-vc-search]')?.value||'',expanded:Boolean(state.expanded),match:state.match,
    anchor,offset:graph?graph.scrollTop%ROW:0,top:graph?.scrollTop||0,left:graph?.scrollLeft||0};
}
export function bindVcs(root,d,view={}) {
  if(!root||d.repository===false)return;
  const state={...view,selected:view.selected||d.graph.commits[0]?.sha};boards.set(root,state);
  const graph=root.querySelector('.vc-graph'), search=root.querySelector('[data-vc-search]');
  const rows=[...root.querySelectorAll('.vc-commits > li')];
  const findRow=sha=>rows.find(r=>r.dataset.key===sha);
  const locate=sha=>{
    const row=findRow(sha);if(!row)return;
    const index=rows.indexOf(row);
    if(state.expanded)row.scrollIntoView({block:'center'});
    else {graph.scrollTop=Math.max(0,index*ROW-graph.clientHeight/2+ROW/2);if(graph.getBoundingClientRect().bottom<0||graph.getBoundingClientRect().top>innerHeight)graph.scrollIntoView({block:'center'});}
  };
  const select=sha=>{
    state.selected=sha;
    rows.forEach(r=>{const selected=r.dataset.key===sha;r.classList.toggle('vc-selected',selected);r.querySelector('[data-vc-commit]').setAttribute('aria-pressed',String(selected));});
    const inspector=root.querySelector('[data-vc-inspector]');if(inspector)inspector.innerHTML=commitDetails(d,sha);
  };
  let matches=[];
  const searchUpdate=()=>{
    const query=search.value.trim().toLowerCase();state.query=search.value;
    matches=d.graph.commits.filter(c=>query&&[c.sha,c.subject,c.author,...c.refs,...c.tags].join(' ').toLowerCase().includes(query)).map(c=>c.sha);
    const set=new Set(matches);rows.forEach(r=>r.classList.toggle('vc-match',set.has(r.dataset.key)));
    root.querySelector('[data-vc-matches]').textContent=query?`${matches.length} matches in loaded history`:'Search highlights rows; history stays intact';
    root.querySelector('[data-vc-next]').disabled=!matches.length;
  };
  if(search){search.value=view.query||'';searchUpdate();}
  if(graph){const anchor=findRow(view.anchor);graph.scrollTop=anchor?rows.indexOf(anchor)*ROW+(view.offset||0):(view.top||0);graph.scrollLeft=view.left||0;}
  root.oninput=e=>{if(e.target.matches('[data-vc-search]')){state.match=null;searchUpdate();}};
  root.onkeydown=e=>{if(e.key==='Enter'&&e.target.matches('[data-vc-search]')){e.preventDefault();root.querySelector('[data-vc-next]').click();}};
  root.onclick=e=>{
    const button=e.target.closest('[data-vc-commit],[data-vc-expand],[data-vc-next],[data-vc-jump],[data-vc-section]');if(!button)return;
    if(button.hasAttribute('data-vc-section')){e.preventDefault();const target=root.querySelector('#vc-'+button.dataset.vcSection);if(target){if(target.tagName==='DETAILS')target.open=true;target.scrollIntoView({block:'start'});}return;}
    if(button.hasAttribute('data-vc-commit')){select(button.dataset.vcCommit);locate(button.dataset.vcCommit);if(matchMedia('(max-width:1200px)').matches)root.querySelector('[data-vc-inspector]')?.scrollIntoView({block:'start'});return;}
    if(button.hasAttribute('data-vc-next')&&matches.length){state.match=matches[(matches.indexOf(state.match)+1)%matches.length];select(state.match);locate(state.match);return;}
    if(button.hasAttribute('data-vc-expand')){state.expanded=!state.expanded;graph?.classList.toggle('expanded',state.expanded);button.textContent=state.expanded?'Compact view':'Expand all loaded commits';button.setAttribute('aria-expanded',String(state.expanded));return;}
    if(button.hasAttribute('data-vc-jump'))locate(button.dataset.vcJump==='selected'?state.selected:(button.dataset.vcJump==='oldest'?d.graph.commits.at(-1):d.graph.commits[0])?.sha);
  };
}
