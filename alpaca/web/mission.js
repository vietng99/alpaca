import {renderRoadmap,bindRoadmap,rememberRoadmap} from './roadmap.js';
import {createRouter} from './roadmap-routing.js';
// Persistent capabilities with recorded evidence, work and explanatory impact paths.
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels={verified:'Verified',failing:'Failing',stale:'Needs recheck',unknown:'Not checked',blocked:'Blocked'};
const symbols={verified:'&#10003;',failing:'&#215;',stale:'&#8635;',unknown:'&#183;',blocked:'!'};
const date=v=>v?new Date(v).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'Not recorded';
const chip=(s)=>`<span class="mm-state mm-${esc(s)}"><i aria-hidden="true">${symbols[s]||'?'}</i>${esc(labels[s]||s)}</span>`;
const href=(route,params={})=>'#'+route+(Object.keys(params).length?'?'+new URLSearchParams(params):'');
const store=new Map(), models=new Map();
const keyFor=d=>d.title;
function stateFor(d){const key=keyFor(d);if(!store.has(key))store.set(key,{mode:'work',selected:null,depth:'all',query:'',tag:'',all:true,suggested:false,closed:[],focus:null,componentOnly:false});return store.get(key);}
const propagates=e=>['depends_on','feeds','governs'].includes(e.kind);

export function impactPaths(data,start,{depth='all',suggested=false}={}){
 const paths=new Map([[start,[]]]),queue=[start];
 while(queue.length){const id=queue.shift();if(depth==='direct'&&id!==start)continue;
  for(const edge of data.edges){if(edge.source!==id||!propagates(edge)||(!suggested&&edge.confidence!=='confirmed')||paths.has(edge.target))continue;
   paths.set(edge.target,[...paths.get(id),edge]);queue.push(edge.target);}}
 paths.delete(start);return paths;
}

function development(data){
 const plan=data.development||{tasks:[],operations:[]},tasks=new Map((plan.tasks||[]).map(t=>[t.id,t]));
 const nodes=new Map((plan.operations||[]).flatMap(op=>op.groups.flatMap(g=>g.nodes)).map(n=>[n.id,n]));
 const linked=new Set(data.nodes.flatMap(n=>n.tasks.map(t=>t.id)));
 return {...plan,tasks,nodes,unmapped:[...tasks.values()].filter(t=>!linked.has(t.id)&&!['done','cancelled','canceled','skipped'].includes(t.status))};
}
function workSummary(node,dev){
 const tasks=[...new Map(node.tasks.map(t=>[t.id,t])).values()],open=tasks.filter(t=>!['done','cancelled','canceled','skipped'].includes(t.status));
 if(dev.unavailable)return {tone:'quiet',label:'Plan not captured',done:null,total:tasks.length,open:open.length};
 const done=tasks.filter(t=>t.status==='done'&&dev.nodes.get(t.id)?.evidence==='valid').length;
 let tone='quiet',label='No linked work';
 if(node.state==='failing'){tone='failing';label='Check failing';}
 else if(open.some(t=>t.status==='doing')){tone='working';label='In progress';}
 else if(open.some(t=>t.status==='blocked'||['blocked','approval','resources'].includes(dev.nodes.get(t.id)?.state))){tone='waiting';label='Blocked';}
 else if(open.some(t=>dev.nodes.get(t.id)?.state==='ready')){tone='ready';label='Ready to work';}
 else if(open.length){tone='planned';label='Planned';}
 else if(node.state==='stale'||node.state==='blocked'){tone='waiting';label='Needs recheck';}
 else if(tasks.some(t=>t.status==='done')&&done!==tasks.filter(t=>t.status==='done').length){tone='waiting';label='Recheck proof';}
 else if(done){tone='complete';label='Work complete';}
 else if(node.state==='verified'){tone='complete';label='Verified';}
 return {tone,label,done,total:tasks.length,open:open.length};
}
function groupSummary(data,g,dev){
 const children=data.nodes.filter(n=>n.group===g.id),tasks=[...new Map(children.flatMap(n=>n.tasks).map(t=>[t.id,t])).values()];
 return workSummary({tasks,state:children.some(n=>n.state==='failing')?'failing':children.some(n=>['stale','blocked'].includes(n.state))?'stale':'unknown'},dev);
}
function nodeCard(node,dev){const w=workSummary(node,dev);return `<article class="mm-node mm-dev-${esc(w.tone)} ${node.lifecycle!=='current'?'mm-planned':''}" data-mm-node="${esc(node.id)}" data-mm-group-id="${esc(node.group)}">
 <button class="mm-node-title" data-mm-select="${esc(node.id)}" aria-pressed="false" title="${esc(node.purpose)}">${esc(node.title)}</button>
 <div class="mm-node-foot"><span>${esc(w.label)}</span><span title="Linked tasks with current completion proof / all linked tasks">${w.done===null?w.total+' linked':w.total?w.done+'/'+w.total:w.open+' tasks'}</span></div>
 ${node.state==='failing'||node.state==='stale'?`<span class="mm-node-check">${esc(labels[node.state])}</span>`:''}</article>`;}
function workOverview(data,dev){
 if(dev.unavailable)return '<div class="mm-notice">Readiness and proof counts were not captured in this older snapshot.</div>';
 const tasks=[...dev.tasks.values()],nodes=[...dev.nodes.values()];
 const working=tasks.filter(t=>t.status==='doing').length,ready=nodes.filter(n=>n.state==='ready'),blocked=nodes.filter(n=>['blocked','approval','resources'].includes(n.state));
 const todo=tasks.filter(t=>t.status==='open').length,done=nodes.filter(n=>n.state==='done'&&n.evidence==='valid').length;
 return `<div class="mm-stats">${[[working,'In progress','active'],[ready.length,'Ready to work','active'],[todo,'To do',''],[blocked.length,'Blocked','stale'],[done,'Proof sealed','verified']].map(([n,l,c])=>`<div><strong class="mm-count-${c}">${n}</strong><span>${l}</span></div>`).join('')}</div>
 <div class="mm-next"><strong>Ready next</strong>${ready.length?ready.slice(0,3).map(n=>`<button data-task="${esc(n.id)}">${esc(dev.tasks.get(n.id)?.title||n.id)} <small>${esc(n.id)}</small></button>`).join(''):'<span>No task is currently ready. Review the blockers in the development plan.</span>'}</div>`;
}
function renderDevelopment(data,st){
 const dev=development(data);
 if(dev.unavailable)return '<div class="mm-notice">Task-plan readiness was not captured in this older snapshot. Choose a newer snapshot or Current system.</div>';
 const selected=data.nodes.find(n=>n.id===st.selected),ids=st.componentOnly&&selected?selected.tasks.map(t=>t.id):null;
 const overview={project:{name:data.title+' development '+(data.historical?data.at:'current')},tasks:[...dev.tasks.values()],roadmap:{operations:dev.operations||[]},ops:[]};
 const unlinked=dev.unmapped.length?`<details class="mm-unmapped"><summary>Needs component mapping: ${dev.unmapped.length} open tasks</summary><p>These tasks are in the development plan but have no component association yet.</p>${dev.unmapped.map(t=>`<button data-task="${esc(t.id)}">${esc(t.title||t.id)} <small>${esc(t.id)}</small></button>`).join('')}</details>`:'';
 return `<div class="mm-plan-heading"><div><h2>Development plan</h2><p>Concrete tasks and their recorded prerequisites. Select a component to focus its work.</p></div><label><input type="checkbox" data-mm-component-only ${st.componentOnly?'checked':''} ${selected?'':'disabled'}> Selected component only</label></div>${dev.stage_error?`<p class="mm-notice">${esc(dev.stage_error)}</p>`:''}${unlinked}${renderRoadmap(overview,{visibleIds:ids})}`;
}

export function renderMission(data,{compact=false,node=null}={}){
 if(!data)return '<section class="panel mm-empty"><h2>Mission map</h2><p>Map data is unavailable. Refresh to try again.</p></section>';
 models.set(keyFor(data),data);const st=stateFor(data);if(node&&data.nodes.some(n=>n.id===node)){st.selected=node;const group=data.nodes.find(n=>n.id===node).group;st.closed=st.closed.filter(g=>g!==group);}
 const counts=data.counts,dev=development(data),summary=`${data.nodes.filter(n=>n.lifecycle==='current').length} components across ${data.groups.length} sectors`;
 if(compact)return `<section class="panel mm-overview"><div class="panel-head"><div><h2>Mission map</h2><p>Development across ${data.groups.length} sectors</p></div><a class="text-link" href="#mission">Open development map &rarr;</a></div><div class="mm-overview-grid">${data.groups.map(g=>{const w=groupSummary(data,g,dev);return `<a class="mm-overview-area mm-dev-${esc(w.tone)}" href="${esc(href('mission',{group:g.id}))}"><strong>${esc(g.title)}</strong><small>${esc(w.label)}${w.total?' &middot; '+(w.done===null?w.total+' linked':w.done+'/'+w.total+' tasks'):''}</small></a>`;}).join('')}</div></section>`;
 const tags=[...new Set(data.nodes.flatMap(n=>n.tags))].sort();
 return `<section class="mm-root" data-mm-root="${esc(keyFor(data))}">
 <div class="mm-heading"><div><h1>Mission map</h1><p>Follow the components being updated, the work ahead, and what each change affects.</p></div><span class="mm-scope">${esc(summary)}</span></div>
 ${!data.configured?'<div class="mm-notice"><strong>Starter map</strong><p>Configure your project capabilities and their checks with <code>alpaca mission init</code>. This map carries no verification claim.</p></div>':''}
 ${data.errors.length?`<div class="mm-notice mm-error" role="alert">${data.errors.map(e=>`<p>${esc(e)}</p>`).join('')}</div>`:''}
 ${data.historical?`<div class="mm-notice"><strong>Recorded snapshot &middot; ${esc(date(data.snapshots.find(s=>s.id===data.at)?.ts))}</strong><p>States and work reflect this saved observation. They do not certify today's inputs.</p></div>`:''}
 ${workOverview(data,dev)}
 <div class="mm-toolbar"><div class="mm-modes" role="group" aria-label="Map view">${[['work','Development'],['system','Components'],['impact','Change impact']].map(([id,label])=>`<button data-mm-mode="${id}" aria-pressed="${st.mode===id}">${label}</button>`).join('')}</div><label class="mm-search"><span class="mm-sr">Search capabilities</span><input type="search" data-mm-search placeholder="Find a component..." value="${esc(st.query)}"></label>
 <label><span class="mm-sr">Concern filter</span><select data-mm-tag><option value="">All concerns</option>${tags.map(t=>`<option value="${esc(t)}" ${st.tag===t?'selected':''}>${esc(t)}</option>`).join('')}</select></label>
 <label><span class="mm-sr">History snapshot</span><select data-mm-history><option value="">Current system</option>${data.snapshots.map(s=>`<option value="${s.id}" ${data.at===s.id?'selected':''}>${esc(date(s.ts))} / ${s.id}</option>`).join('')}</select></label></div>
 <div class="mm-options"><button class="mm-inline" data-mm-expand>Expand all</button><button class="mm-inline" data-mm-collapse>Collapse all</button><span class="mm-context" data-mm-context></span><label><input type="checkbox" data-mm-all ${st.all?'checked':''}> All links</label><label><input type="checkbox" data-mm-suggested ${st.suggested?'checked':''}> Suggested links</label><label>Impact <select data-mm-depth><option value="all" ${st.depth==='all'?'selected':''}>All dependents</option><option value="direct" ${st.depth==='direct'?'selected':''}>Direct only</option></select></label></div>
 <div class="mm-section-label">SYSTEM SECTORS &middot; COMPONENTS</div><div class="mm-layout"><div class="mm-canvas" data-mm-canvas><svg class="mm-links" aria-hidden="true" data-mm-links></svg><div class="mm-groups">${data.groups.map(g=>`<details class="mm-group mm-${esc(g.state)}" data-mm-group="${esc(g.id)}" ${st.closed.includes(g.id)?'':'open'}><summary><span class="mm-group-label">${esc(g.title)}</span><span class="mm-group-count">${g.total} components</span></summary><div class="mm-group-nodes">${data.nodes.filter(n=>n.group===g.id).map(n=>nodeCard(n,dev)).join('')}</div></details>`).join('')}</div><p class="mm-empty" data-mm-no-results hidden>No capabilities match these filters.</p></div>
 <aside class="mm-detail" data-mm-detail aria-label="Capability details"></aside></div>
 <div class="mm-legend"><span class="mm-dev-complete">Work complete / verified</span><span class="mm-dev-working">In progress</span><span class="mm-dev-ready">Ready</span><span class="mm-dev-waiting">Blocked / needs recheck</span><span class="mm-dev-planned">Planned</span><span>Component colors show development progress. Verification details are separate.</span></div>
 <div data-mm-development>${renderDevelopment(data,st)}</div>
 <details class="mm-coverage-detail"><summary>Map coverage: ${data.coverage.mapped} of ${data.coverage.total} inventoried files mapped${data.coverage.unmapped.length?' &middot; '+data.coverage.unmapped.length+' unmapped':''}</summary><p>Scope: ${esc(data.coverage.scope.join(', ')||'No inventory defined')}. Dependency coverage is declared, not exhaustive.</p>${data.coverage.complete?'':'<p>Some inputs were excluded or could not be read. Coverage is incomplete.</p>'}${data.coverage.unmapped.length?`<ul>${data.coverage.unmapped.map(p=>`<li><code>${esc(p)}</code></li>`).join('')}</ul>`:'<p>Every file in the declared inventory has a capability mapping.</p>'}</details>
 <p class="mm-footnote" role="status" data-mm-message>Checked ${esc(date(data.checked_at))}. Task completion and current component verification are separate records.</p></section>`;
}

function detail(data,st){
 const node=data.nodes.find(n=>n.id===st.selected);
 if(!node)return '<div class="mm-detail-empty"><span class="mm-detail-symbol" aria-hidden="true">&#9678;</span><h2>Explore a capability</h2><p>Select a card to see its responsibility, work, verification and change history.</p><p>Change impact follows confirmed dependency paths. Suggested links are optional.</p></div>';
 const related=data.edges.filter(e=>e.source===node.id||e.target===node.id),paths=impactPaths(data,node.id,st),titles=new Map(data.nodes.map(n=>[n.id,n.title]));
 const linkNode=id=>`<button class="mm-inline" data-mm-select="${esc(id)}">${esc(titles.get(id)||id)}</button>`;
 return `<div class="mm-detail-head"><span>${esc(data.groups.find(g=>g.id===node.group)?.title)}</span><button data-mm-close aria-label="Close capability details">&times;</button></div><h2 tabindex="-1">${esc(node.title)}</h2><p>${esc(node.purpose)}</p><div class="mm-detail-states">${chip(node.state)}${node.work!=='idle'?`<span class="mm-work">${esc(node.work)}</span>`:''}</div><p class="mm-reason">${esc(node.reason)}</p>
 <dl class="mm-dates"><dt>${node.last_changed?.baseline?'First mapped':'Last changed'}</dt><dd>${esc(date(node.last_changed?.ts))}${node.last_changed?.unrecorded?'<small>Filesystem observation; capture pending</small>':''}</dd><dt>Last verified fix</dt><dd>${node.last_fix?`${esc(date(node.last_fix.ts))}<a href="${esc(href('work',{q:node.last_fix.task}))}">${esc(node.last_fix.task)}</a>`:'Not recorded'}</dd><dt>Last checked</dt><dd>${esc(date(node.last_checked?.ts))}${node.last_checked?`<small>Recorded ${esc(node.last_checked.result)}</small>`:''}</dd></dl>
 ${st.mode==='impact'?`<section><h3>Potential impact &middot; ${paths.size}</h3><p class="mm-small">Reachability identifies checks to revisit. It does not establish a failure.</p>${paths.size?`<ol class="mm-paths">${[...paths].map(([id,path])=>`<li>${linkNode(id)}<small>${path.map(e=>esc(titles.get(e.source))).concat(esc(titles.get(id))).join(' &rarr; ')}</small>${path.map(e=>`<p>${esc(e.reason)}${e.confidence==='inferred'?' (suggested)':''}</p>`).join('')}</li>`).join('')}</ol>`:'<p>No dependents are recorded in this scope. Unknown relationships can still exist.</p>'}</section>`:''}
 <section><h3>Linked work</h3>${node.tasks.length?node.tasks.map(t=>`<a class="mm-task" href="${esc(href('work',{q:t.id}))}"><strong>${esc(t.title||t.id)}</strong><small>${esc(t.id)} &middot; ${esc(t.status)} &middot; ${esc(t.link_kind)}</small></a>${t.assigned_session?`<a class="mm-small" href="${esc(href('sessions',{sid:t.assigned_session}))}">Working chat</a>`:''}${t.proof?`<a class="mm-small" href="${esc(href('library',{path:t.proof.replace(/^local:/,'')}))}">Proof report</a>`:''}`).join(''):'<p>No work is explicitly linked to this capability.</p>'}</section>
 <section><h3>Recorded checks</h3>${node.checks.length?node.checks.map(c=>`<div class="mm-check"><strong>${esc(c.data.check)}</strong><span>${esc(c.data.result)} &middot; ${esc(date(c.ts))}</span><code>${esc(c.data.command.join(' '))}</code><button class="mm-inline" data-mm-evidence="${c.id}">Read check evidence</button></div>`).join(''):'<p>No checks have run for this capability.</p>'}<div data-mm-log></div></section>
 <section><h3>Relationships</h3>${related.length?related.map(e=>`<div class="mm-relationship">${linkNode(e.source)}<span>${esc(e.kind.replaceAll('_',' '))}</span>${linkNode(e.target)}<p>${esc(e.reason)}</p><small>${esc(e.confidence)} &middot; ${esc(e.evidence.join(', '))}</small></div>`).join(''):'<p>No relationships recorded.</p>'}</section>
 <details><summary>Mapped files &middot; ${node.files.length}</summary><ul class="mm-file-list">${node.files.map(f=>`<li><code>${esc(f)}</code></li>`).join('')}</ul></details>
 <section><h3>Change history</h3>${node.history.length?`<ol class="mm-history">${node.history.map(e=>`<li><strong>${esc(e.kind.replace('mission-','').replaceAll('-',' '))}${e.data.result?' &middot; '+esc(e.data.result):''}</strong><small>${esc(date(e.ts))}</small><a href="${esc(e.kind==='mission-snapshot'?href('mission',{at:e.id,node:node.id}):href('activity',{ref:node.id,kind:'all'}))}">Record ${e.id}</a></li>`).join('')}</ol>`:'<p>No changes recorded.</p>'}</section>`;
}

function paint(root,data,st,{refreshPlan=true}={}){
 const paths=st.selected?impactPaths(data,st.selected,st):new Map();
 root.querySelectorAll('[data-mm-mode]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.mmMode===st.mode)));
 root.dataset.mode=st.mode;root.classList.toggle('mm-has-selection',Boolean(st.selected));
 let visible=0;
 root.querySelectorAll('[data-mm-node]').forEach(el=>{const n=data.nodes.find(n=>n.id===el.dataset.mmNode);const show=(!st.query||[n.title,n.purpose,n.id,...n.tags].join(' ').toLowerCase().includes(st.query.toLowerCase()))&&(!st.tag||n.tags.includes(st.tag));el.hidden=!show;if(show)visible++;
  el.classList.toggle('mm-selected',n.id===st.selected);el.classList.toggle('mm-affected',paths.has(n.id));el.classList.toggle('mm-working',n.work!=='idle');
  el.querySelector('[data-mm-select]').setAttribute('aria-pressed',String(n.id===st.selected));});
 root.querySelectorAll('[data-mm-group]').forEach(g=>{g.hidden=![...g.querySelectorAll('[data-mm-node]')].some(n=>!n.hidden);});
 root.querySelector('[data-mm-no-results]').hidden=visible>0;
 root.querySelector('[data-mm-context]').textContent=st.mode==='impact'?(st.selected?`${paths.size} potentially affected capabilities`:'Select a capability to trace its dependents'):st.mode==='work'?'Components show work progress; the plan below shows what to do next.':'Select a component to reveal its connections and work.';
 root.querySelector('[data-mm-detail]').innerHTML=detail(data,st);
 const plan=root.querySelector('[data-mm-development]'),filterFocused=document.activeElement?.matches('[data-mm-component-only]');
 if(refreshPlan){rememberRoadmap(plan);plan.innerHTML=renderDevelopment(data,st);}
 plan.hidden=st.mode==='system';bindRoadmap(plan);
 if(filterFocused)plan.querySelector('[data-mm-component-only]')?.focus({preventScroll:true});
 requestAnimationFrame(()=>draw(root,data,st));
}

function draw(root,data,st){
 if(!root.isConnected)return;
 const canvas=root.querySelector('[data-mm-canvas]'),svg=root.querySelector('[data-mm-links]');svg.innerHTML='';
 if(matchMedia('(max-width:700px)').matches)return;
 const bounds=canvas.getBoundingClientRect(),paths=st.selected?impactPaths(data,st.selected,st):new Map();
 svg.setAttribute('viewBox',`0 0 ${bounds.width} ${bounds.height}`);svg.style.width=bounds.width+'px';svg.style.height=bounds.height+'px';
 const box=(element,key)=>{const r=element.getBoundingClientRect();return {key,left:r.left-bounds.left,right:r.right-bounds.left,top:r.top-bounds.top,bottom:r.bottom-bounds.top,x:r.left+r.width/2-bounds.left,y:r.top+r.height/2-bounds.top};};
 const elements=[...canvas.querySelectorAll('[data-mm-node]')].filter(n=>!n.hidden&&n.closest('details').open),rects=elements.map(n=>box(n,n.dataset.mmNode));
 const headers=[...canvas.querySelectorAll('[data-mm-group]:not([hidden])>summary')].map(n=>box(n,'g:'+n.closest('details').dataset.mmGroup));
 const ends=new Map(rects.map(r=>[r.key,r]));
 for(const n of data.nodes){const group=canvas.querySelector(`[data-mm-group="${CSS.escape(n.group)}"]`);if(group&&!group.open&&!group.hidden)ends.set(n.id,headers.find(r=>r.key==='g:'+n.group));}
 const edgeSet=new Set([...paths.values()].flat()),links=[];
 for(const e of data.edges){if(e.confidence==='inferred'&&!st.suggested)continue;
  if(!st.all&&!(st.mode==='impact'?edgeSet.has(e):e.source===st.selected||e.target===st.selected))continue;
  const from=ends.get(e.source),to=ends.get(e.target);if(from&&to&&from.key!==to.key)links.push({e,from,to});
 }
 const route=createRouter([...rects,...headers],[...new Map(links.flatMap(l=>[l.from,l.to]).map(r=>[r.key,r])).values()],bounds.width,bounds.height);
 const lines=['<defs><marker id="mm-direction" viewBox="0 0 8 8" refX="8" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L8 4 L0 8 Z" class="mm-arrow-tip"/></marker></defs>'];
 for(const {e,from,to} of links){const points=route(from,to);if(!points)continue;const selected=edgeSet.has(e)||e.source===st.selected||e.target===st.selected;
  lines.push(`<path data-mm-edge="${esc(e.source+':'+e.target)}" class="${e.confidence==='inferred'?'mm-suggested-edge ':''}${selected?'mm-edge-selected':''}" marker-end="url(#mm-direction)" d="${points.map(([x,y],i)=>(i?'L':'M')+x+','+y).join(' ')}"><title>${esc(e.reason)}</title></path>`);
 }
 svg.innerHTML=lines.join('');
}

export function rememberMission(container){
 const root=container?.querySelector('[data-mm-root]');if(!root)return;rememberRoadmap(root);
 const st=store.get(root.dataset.mmRoot);if(!st)return;
 st.closed=[...root.querySelectorAll('[data-mm-group]')].filter(g=>!g.open).map(g=>g.dataset.mmGroup);
 st.focus=root.contains(document.activeElement)?document.activeElement.dataset.mmSelect||null:null;
 root._mmObserver?.disconnect();
}

export function bindMission(container){
 const root=container.querySelector('[data-mm-root]');if(!root||root.dataset.bound)return;root.dataset.bound='true';
 const data=models.get(root.dataset.mmRoot),st=stateFor(data);
 root.addEventListener('click',async e=>{
  const choose=e.target.closest('[data-mm-select]'),mode=e.target.closest('[data-mm-mode]'),close=e.target.closest('[data-mm-close]'),evidence=e.target.closest('[data-mm-evidence]');
  if(choose){const follow=Boolean(choose.closest('[data-mm-detail]'));st.selected=choose.dataset.mmSelect;const group=root.querySelector(`[data-mm-group="${CSS.escape(data.nodes.find(n=>n.id===st.selected).group)}"]`);if(group)group.open=true;paint(root,data,st);if(follow||matchMedia('(max-width:800px)').matches)root.querySelector('[data-mm-detail] h2').focus({preventScroll:true});if(matchMedia('(max-width:800px)').matches)root.querySelector('[data-mm-detail]').scrollIntoView({block:'start'});}
  if(mode){st.mode=mode.dataset.mmMode;paint(root,data,st);}
  if(close){const previous=st.selected;st.selected=null;paint(root,data,st);root.querySelector(`[data-mm-node] [data-mm-select="${CSS.escape(previous)}"]`)?.focus();}
  if(e.target.closest('[data-mm-expand],[data-mm-collapse]')){const open=Boolean(e.target.closest('[data-mm-expand]'));root.querySelectorAll('[data-mm-group]').forEach(g=>g.open=open);st.closed=open?[]:data.groups.map(g=>g.id);requestAnimationFrame(()=>draw(root,data,st));}
  if(evidence){const box=root.querySelector('[data-mm-log]');box.textContent='Loading check evidence...';evidence.disabled=true;
   try{const response=await fetch('/hub/mission-evidence.json?event='+encodeURIComponent(evidence.dataset.mmEvidence));if(!response.ok)throw Error('Evidence could not be loaded');const value=await response.json();box.innerHTML=`<pre>${esc(value.text)}</pre>${value.truncated?'<p>Showing the first 64 KiB.</p>':''}`;}catch(err){box.textContent=err.message;}finally{evidence.disabled=false;}}
 });
 root.addEventListener('input',e=>{if(e.target.matches('[data-mm-search]')){st.query=e.target.value;paint(root,data,st);}});
 root.addEventListener('change',async e=>{const t=e.target;
  if(!t.matches('[data-mm-component-only],[data-mm-depth],[data-mm-tag],[data-mm-all],[data-mm-suggested],[data-mm-history]'))return;
  if(t.matches('[data-mm-component-only]'))st.componentOnly=t.checked;
  if(t.matches('[data-mm-depth]'))st.depth=t.value;
  if(t.matches('[data-mm-tag]'))st.tag=t.value;
  if(t.matches('[data-mm-all]'))st.all=t.checked;
  if(t.matches('[data-mm-suggested]'))st.suggested=t.checked;
  if(t.matches('[data-mm-history]')){location.hash=href('mission',t.value?{at:t.value}:{});return;}
  paint(root,data,st);
 });
 root.addEventListener('toggle',e=>{if(e.target.matches('[data-mm-group]')){st.closed=[...root.querySelectorAll('[data-mm-group]')].filter(g=>!g.open).map(g=>g.dataset.mmGroup);requestAnimationFrame(()=>draw(root,data,st));}},true);
 root._mmObserver=new ResizeObserver(()=>draw(root,data,st));root._mmObserver.observe(root.querySelector('[data-mm-canvas]'));
 paint(root,data,st,{refreshPlan:false});
 if(st.focus)root.querySelector(`[data-mm-node] [data-mm-select="${CSS.escape(st.focus)}"]`)?.focus({preventScroll:true});
}
