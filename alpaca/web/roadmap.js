import {createRouter} from './roadmap-routing.js';
// Recorded work areas and dependencies shared by the cockpit and Work checklist.
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const models=new Map(), memory=new Map();
let pendingFocus=null;
const scope=o=>`${globalThis.location?.pathname||''}:${o.project?.name||'project'}:v2`;
function prefs(key){if(!memory.has(key)){let value={};try{value=JSON.parse(sessionStorage.getItem('alpaca.roadmap.'+key)||'{}');}catch{}memory.set(key,{collapsed:{},details:{},focus:'',chat:'',scroll:0,category:'',showDone:false,...(value&&typeof value==='object'?value:{})});}return memory.get(key);}
function keep(key){try{sessionStorage.setItem('alpaca.roadmap.'+key,JSON.stringify(prefs(key)));}catch{}}
const taskMap=o=>new Map((o.tasks||[]).map(t=>[t.id,t]));
const nodesOf=op=>(op.groups||[]).flatMap(g=>g.nodes||[]);
function plans(o){if(o.roadmap?.operations)return o.roadmap.operations;return [...new Set((o.tasks||[]).map(t=>t.op))].map(op=>({op,revision:0,edges:[],groups:[{id:'__unplanned__',title:'Needs organization',category:'Needs organization',nodes:o.tasks.filter(t=>t.op===op).map(t=>({id:t.id,state:'unplanned',label:'Needs organization',depth:0,after:[],reasons:['Task relationships have not been recorded.'],session:t.assigned_session}))}]}));}
const operatorName=node=>({codex:'Codex',claude:'Claude','claude-code':'Claude Code',cli:'CLI'}[node.operator]||node.operator||'Chat');
const titleOf=(o,op)=>op.title||(o.ops||[]).find(x=>x.id===op.op)?.title||op.op;
const claimChip=(node,tasks,compact=false)=>{
 const task=tasks.get(node.id)||{};if(terminal(node,tasks)||!(node.session||task.claimant))return '';
 const who=node.session?`<span class="mono">${esc(String(node.session).slice(0,8))}</span> / ${esc(operatorName(node))}`:esc(task.claimant);
 const title=esc('Claimed by '+(node.session?operatorName(node)+' / '+node.session:task.claimant));
 return compact?`<span class="rm-claim mono" title="${title}">@${esc(String(node.session||task.claimant).slice(0,8))}</span>`:`<span class="rm-claim" title="${title}">Claimed by ${who}</span>`;
};
const taskLink=(id,tasks)=>`<button type="button" data-task="${esc(id)}">${esc(tasks.get(id)?.title||id)} <small>${esc(id)}</small></button>`;
const terminal=(node,tasks)=>['done','skipped','cancelled','canceled'].includes(tasks.get(node.id)?.status);
function workspace(available){
 const groups=new Map();
 for(const op of available)for(const group of op.groups){
  const category=group.category||'Other work',key=JSON.stringify([category,group.title]);
  if(!groups.has(key))groups.set(key,{...group,id:key,category,nodes:[]});
  groups.get(key).nodes.push(...group.nodes);
 }
 return {op:'all',title:'All operations',revision:1,groups:[...groups.values()],edges:available.flatMap(op=>op.edges||[]),error:available.filter(op=>op.error).map(op=>op.op+': '+op.error).join('; ')};
}
function nodeCard(node,tasks,o,compact=false,expanded=false){
 const task=tasks.get(node.id)||{id:node.id,title:node.id},needs=node.after||[],reasons=node.reasons||[],op=plans(o).find(p=>p.op===task.op);
 if(compact){
  return `<article class="rm-node rm-compact rm-${esc(node.state)}" data-rm-node="${esc(node.id)}">
   <div class="rm-node-top"><span class="mono">${esc(node.id)}</span>${claimChip(node,tasks,true)}<span class="rm-state" title="${esc(node.label)}">${esc(node.label||'Needs organization')}</span></div>
   <button type="button" class="rm-title" data-task="${esc(node.id)}" title="${esc(task.title)}"><span class="rm-title-text">${esc(task.title)}</span></button>
   <button type="button" class="rm-more" data-rm-details="${esc(node.id)}" aria-controls="rm-info-${esc(node.id)}" aria-expanded="${expanded}" aria-label="Details and actions for ${esc(node.id)}" title="Details and actions">&#8943;</button>
   <div id="rm-info-${esc(node.id)}" class="rm-node-info" ${expanded?'':'hidden'}><div class="rm-context">${esc(op?titleOf(o,op):task.op)}${task.phase?' / '+esc(task.phase):''}</div>
   ${node.state==='unplanned'?`<small class="rm-recorded">Recorded: ${esc(task.status||'unknown')}</small>`:''}
   <div class="rm-needs">${needs.length?'After '+needs.map(id=>taskLink(id,tasks)).join(' + '):node.state==='unplanned'?'Order not recorded':'No task prerequisites'}</div>
   ${reasons.length?`<ul class="rm-reasons">${reasons.map(r=>`<li>${esc(r)}</li>`).join('')}</ul>`:''}<div class="rm-node-foot">${node.session?`<a class="rm-chat" href="#sessions?sid=${encodeURIComponent(node.session)}" title="${esc(operatorName(node)+' / '+node.session)}">Open chat</a>`:''}<button type="button" class="rm-focus" data-rm-focus="${esc(node.id)}" aria-label="Trace ${esc(node.id)} dependencies">Trace path</button></div></div>
  </article>`;
 }
 return `<article class="rm-node rm-${esc(node.state)}" data-rm-node="${esc(node.id)}">
 <div class="rm-node-top"><span class="mono">${esc(node.id)}</span><span class="rm-state" title="${esc(node.label)}">${esc(node.label||'Needs organization')}</span></div>
 <button type="button" class="rm-title" data-task="${esc(node.id)}" title="${esc(task.title)}">${esc(task.title)}</button>${claimChip(node,tasks)}
 <div class="rm-context">${esc(op?titleOf(o,op):task.op)}${task.phase?' / '+esc(task.phase):''}</div>
 ${node.state==='unplanned'?`<small class="rm-recorded">Recorded: ${esc(task.status||'unknown')}</small>`:''}
 ${needs.length?`<div class="rm-needs"><span>After</span> ${needs.map(id=>taskLink(id,tasks)).join(' <span>+</span> ')}</div>`:'<div class="rm-needs">'+(node.state==='unplanned'?'Order not recorded':'No task prerequisites')+'</div>'}
 ${reasons.length?`<ul class="rm-reasons">${reasons.map(r=>`<li>${esc(r)}</li>`).join('')}</ul>`:''}
 <div class="rm-node-foot">${node.session?`<a class="rm-chat" href="#sessions?sid=${encodeURIComponent(node.session)}" title="${esc(node.session)}">Open assigned chat</a>`:`<span class="rm-unassigned">${task.claimant?'Worker: '+esc(task.claimant):terminal(node,tasks)?'Completed work':'No assigned chat'}</span>`}<button type="button" class="rm-focus" data-rm-focus="${esc(node.id)}" aria-label="Trace ${esc(node.id)} dependencies">Trace</button></div>
 </article>`;
}
function operationPicker(o,selected){
 const tasks=taskMap(o),available=plans(o),row=(id,title,nodes)=>{
  const done=nodes.filter(n=>tasks.get(n.id)?.status==='done').length,working=nodes.filter(n=>['working','assignment_expired'].includes(n.state)).length;
  return `<button type="button" class="rm-choice" data-rm-operation="${esc(id)}" aria-pressed="${selected===id}" data-rm-search-text="${esc((title+' '+id).toLowerCase())}"><span><strong>${esc(title)}</strong><small>${esc(id||'Workspace')} / ${done} of ${nodes.length} done${working?' / '+working+' in progress':''}</small></span><span aria-hidden="true">${selected===id?'&#10003;':'&rarr;'}</span></button>`;
 };
 return `<details class="rm-picker" data-rm-picker="operation"><summary aria-label="Choose operation">${esc(selected?titleOf(o,available.find(p=>p.op===selected)||{op:selected}):'All operations')} <span aria-hidden="true">&#9662;</span></summary><div class="rm-picker-menu"><label class="rm-search-label">Find an operation<input type="search" data-rm-search placeholder="Search name or ID" autocomplete="off"></label>${row('','All operations',available.flatMap(nodesOf))}${available.map(op=>row(op.op,titleOf(o,op),nodesOf(op))).join('')}<p data-rm-no-results hidden>No matching operations.</p></div></details>`;
}
function chatPicker(nodes,tasks,p){
 const chats=new Map();for(const n of nodes)if(n.session){if(!chats.has(n.session))chats.set(n.session,[]);chats.get(n.session).push(n);}
 if(!chats.size)return '<span class="rm-no-chats">No assigned chats in this view</span>';
 return `<details class="rm-picker" data-rm-picker="chat"><summary>Find a chat <span class="rm-count">${chats.size}</span> <span aria-hidden="true">&#9662;</span></summary><div class="rm-picker-menu"><p class="rm-picker-hint">Highlight a chat by the work assigned to it.</p>${[...chats].map(([sid,assigned])=>`<button type="button" class="rm-choice" data-rm-chat="${esc(sid)}" aria-pressed="${p.chat===sid}"><span><strong>${assigned.map(n=>esc(tasks.get(n.id)?.title||n.id)).join(', ')}</strong><small>${esc(operatorName(assigned[0]))} / ${assigned.map(n=>esc(n.label)).join(', ')} / ${esc(sid)}</small></span></button>`).join('')}</div></details>`;
}
// Each chat holding unfinished work, with the tasks it claims; a chip highlights that chat's cards.
function claimsStrip(nodes,tasks,p){
 const chats=new Map();for(const n of nodes)if(n.session&&!terminal(n,tasks)){if(!chats.has(n.session))chats.set(n.session,[]);chats.get(n.session).push(n);}
 if(!chats.size)return '';
 return `<div class="rm-claims" role="group" aria-label="Claimed work by chat"><span class="rm-claims-label">Claimed now</span>${[...chats].map(([sid,assigned])=>`<button type="button" class="rm-claim-chip" data-rm-chat="${esc(sid)}" aria-pressed="${p.chat===sid}" title="${esc(operatorName(assigned[0])+' / '+sid)}"><span class="mono">${esc(String(sid).slice(0,8))}</span> ${esc(operatorName(assigned[0]))}: ${assigned.map(n=>`<strong>${esc(n.id)}</strong> ${esc(tasks.get(n.id)?.title||'')}`).join(', ')}</button>`).join('')}</div>`;
}
function renderOne(o,op,options={}){
 // layout 'phases' is the pinned operation map: groups are the op's phases in their recorded order,
 // done work stays on the map, and panel state is kept per map instead of workspace-wide.
 const {mode='map',selector=true,visibleIds=null,layout='',fixedOrder=false,showAll=false,heading='',eyebrow='',preface='',canvasStyle=''}=options,key=scope(o)+':'+op.op+(layout?':'+layout:''),viewKey=key+':'+mode,p=prefs(key),global=prefs(scope(o)),tasks=taskMap(o),all=nodesOf(op),visible=visibleIds?new Set(visibleIds):null;
 const scoped=all.filter(n=>!visible||visible.has(n.id)),completeOnly=scoped.length&&scoped.every(n=>terminal(n,tasks)),showDone=showAll||global.showDone||Boolean(completeOnly);
 const categories=layout?[]:[...new Set(op.groups.map(g=>g.category||'Other work'))];
 const category=categories.includes(global.category)?global.category:'';
 const groups=op.groups.filter(g=>!category||(g.category||'Other work')===category).map(g=>({...g,nodes:g.nodes.filter(n=>!visible||visible.has(n.id))})).filter(g=>g.nodes.length).sort((a,b)=>fixedOrder?0:Number(a.nodes.every(n=>terminal(n,tasks)))-Number(b.nodes.every(n=>terminal(n,tasks))));
 const shown=groups.flatMap(g=>g.nodes.filter(n=>showDone||!terminal(n,tasks))),shownIds=new Set(shown.map(n=>n.id));
 if(p.chat&&!shown.some(n=>n.session===p.chat))p.chat='';
 if(p.focus&&!shownIds.has(p.focus))p.focus='';
 models.set(viewKey,{o,op,mode,key,options,visibleIds,selector,renderedGroups:groups});
 const done=all.filter(n=>tasks.get(n.id)?.status==='done').length,working=scoped.filter(n=>n.state==='working'||n.state==='assignment_expired').length,ready=scoped.filter(n=>n.state==='ready').length;
 const holder=layout?p:global,folded=mode==='map'&&Boolean(holder.panelCollapsed),bodyId='rm-body-'+encodeURIComponent(viewKey);
 const title=op.op==='all'?'Work by area':titleOf(o,op),summary=op.summary||(op.op==='all'?'Follow each feature from its current task to what comes next.':'Follow the recorded prerequisites within this operation.');
 return `<section class="panel rm-panel rm-${mode==='map'?'map':'list'} ${layout?'rm-'+esc(layout):''} ${folded?'rm-folded':''}" data-rm-key="${esc(viewKey)}">
 ${mode==='map'?`<div class="rm-shell-heading"><button type="button" data-rm-toggle aria-expanded="${!folded}" aria-controls="${esc(bodyId)}"><span class="rm-chevron" aria-hidden="true">&gt;</span><strong>${esc(heading||'Task roadmap')}</strong><span class="rm-shell-count">${working} in progress / ${ready} ready / ${done} of ${all.length} done</span><span class="rm-toggle-label">${folded?'Expand':'Collapse'}</span></button></div>`:''}
 <div class="rm-body" id="${esc(bodyId)}" ${folded?'hidden':''}>
 <div class="rm-heading"><div><span class="eyebrow">${esc(eyebrow||(mode==='map'?'TASK ROADMAP':'GROUPED CHECKLIST'))}</span><h2>${esc(title)}</h2><p>${esc(summary)}</p><p class="rm-progress-line">${working} in progress <span aria-hidden="true">/</span> ${ready} ready <span aria-hidden="true">/</span> ${done} of ${all.length} done</p></div><div class="rm-controls">${selector?operationPicker(o,op.op==='all'?'':op.op):''}${chatPicker(shown,tasks,p)}<button type="button" class="button rm-clear" data-rm-clear ${!p.chat&&!p.focus?'hidden':''}>Clear focus</button></div></div>
 ${preface}
 <div class="rm-viewbar">${layout?'':`<div class="rm-categories" role="group" aria-label="Work area">${['',...categories].map(c=>`<button type="button" data-rm-category="${esc(c)}" aria-pressed="${c===category}">${esc(c||'All areas')}</button>`).join('')}</div>`}<div class="rm-fold-all" role="group" aria-label="Open or close cards"><button type="button" data-rm-expand="true">Open all</button><button type="button" data-rm-expand="false">Close all</button></div>${layout?'':`<div class="rm-visibility" role="group" aria-label="Plan scope"><button type="button" data-rm-completed="false" aria-pressed="${!showDone}" ${completeOnly?'disabled':''}>Active work</button><button type="button" data-rm-completed="true" aria-pressed="${showDone}">Full plan</button></div>`}</div>
 ${claimsStrip(shown,tasks,p)}
 ${op.error?`<p class="inline-warning rm-note">Plan unavailable: ${esc(op.error)}</p>`:''}
 <div class="rm-legend"><span><i class="rm-dot rm-dot-working"></i>In progress</span><span><i class="rm-dot rm-dot-ready"></i>Ready</span><span><i class="rm-dot rm-dot-waiting"></i>Waiting</span><span><i class="rm-dot rm-dot-blocked"></i>Blocked or failed</span><span><i class="rm-dot rm-dot-done"></i>Done</span><span><i class="rm-dot rm-dot-unplanned"></i>Needs organization</span><span class="rm-legend-help">Arrows show prerequisites. Tasks sharing a resource wait their turn.</span></div>
 ${!groups.length?`<p class="rm-note">${all.length?'No tasks match these filters.':'No tasks have been recorded yet.'}</p>`:''}
 <div class="rm-scroll"><div class="rm-canvas"${canvasStyle?` style="${esc(canvasStyle)}"`:''}><svg class="rm-connectors" aria-hidden="true"></svg>${groups.map(group=>{
  const count=group.nodes.filter(n=>terminal(n,tasks)).length,complete=count===group.nodes.length,groupNodes=group.nodes.filter(n=>shownIds.has(n.id));
  const expanded=!Object.hasOwn(p.collapsed,group.id)?!complete:!p.collapsed[group.id];
  const current=group.nodes.find(n=>['working','assignment_expired'].includes(n.state))||group.nodes.find(n=>n.state==='ready')||group.nodes.find(n=>!terminal(n,tasks));
  return `<details class="rm-group ${group.id.includes('__unplanned__')?'rm-unplanned-group':''}" data-rm-group="${esc(group.id)}" ${expanded?'open':''}><summary><span class="rm-group-port" aria-hidden="true"></span><span class="rm-chevron" aria-hidden="true">&gt;</span><span class="rm-group-name"><small>${esc(group.category||'Other work')}</small><strong>${esc(group.title)}</strong></span><span class="rm-group-count">${count}/${group.nodes.length}${mode==='map'?'':' complete'}</span><span class="rm-group-current">${current?esc((tasks.get(current.id)?.title||current.id)+' / '+current.label):'Complete'}</span></summary>${groupNodes.length?(group.subgroups?group.subgroups.map(s=>{const inSub=s.nodes.filter(n=>shownIds.has(n.id));return inSub.length?`<div class="rm-sub"><div class="rm-sub-title">${esc(s.title)}</div><div class="rm-track">${inSub.map(n=>nodeCard(n,tasks,o,mode==='map',Boolean(p.details[n.id]))).join('')}</div></div>`:'';}).join(''):`<div class="rm-track">${groupNodes.map(n=>nodeCard(n,tasks,o,mode==='map',Boolean(p.details[n.id]))).join('')}</div>`):`<p class="rm-note">${count} completed tasks. <button type="button" class="button" data-rm-completed="true">View full plan</button></p>`}</details>`;
 }).join('')}</div></div>
 <p class="rm-footnote">${layout==='phases'?'Phases run in order; each lists its tasks by workstream. Finished tasks stay on the map as done.':showDone?'Full plan includes completed work.':'Completed work is folded away. Choose Full plan to see every step.'} A chat assignment does not confirm a running process.</p></div></section>`;
}
const PHASE_STATUS={complete:'Complete',current:'Current phase',open:'Open',upcoming:'Not opened'};
const phaseStatus=(phase,state)=>(state.completed||[]).includes(phase)?'complete':state.current===phase?'current':(state.opened||[]).includes(phase)?'open':'upcoming';
// The operation map for a pinned op: its phases, in recorded order, as the map's sectors. Node states
// come from the same plan projection as the roadmap, so a finished task shows as done on the next
// refresh; nothing here stores task state of its own.
export function renderPhaseMap(o,opId,options={}){
 const plan=plans(o).find(p=>p.op===opId);if(!plan)return '';
 const tasks=taskMap(o),op=(o.ops||[]).find(x=>x.id===opId)||{id:opId},state=op.phase_state||{},declared=op.phases||[];
 const byPhase=new Map();
 for(const group of plan.groups)for(const node of group.nodes){const phase=tasks.get(node.id)?.phase||'';if(!byPhase.has(phase))byPhase.set(phase,[]);byPhase.get(phase).push({node,group});}
 const order=[...declared,...[...byPhase.keys()].filter(p=>!declared.includes(p))];
 const name=phase=>phase?phase[0].toUpperCase()+phase.slice(1):'No phase recorded';
 const groups=order.filter(p=>byPhase.has(p)).map((phase,i)=>{
  const subgroups=[];
  for(const {node,group} of byPhase.get(phase)){let sub=subgroups.find(s=>s.id===group.id);if(!sub){sub={id:group.id,title:group.title,nodes:[]};subgroups.push(sub);}sub.nodes.push(node);}
  return {id:'phase:'+(phase||'none'),title:name(phase),category:'Phase '+(i+1)+' / '+PHASE_STATUS[phaseStatus(phase,state)],nodes:byPhase.get(phase).map(x=>x.node),subgroups};
 });
 const nodes=nodesOf(plan),count=(list,states)=>list.filter(n=>states.includes(n.state)).length;
 const strip=`<ol class="rm-phase-strip" aria-label="Phases">${order.map((phase,i)=>{
  const inPhase=(byPhase.get(phase)||[]).map(x=>x.node),doneCount=inPhase.filter(n=>terminal(n,tasks)).length,status=phaseStatus(phase,state),working=count(inPhase,['working','assignment_expired']);
  return `<li class="rm-phase rm-phase-${status}"><span class="rm-phase-step">${i+1}</span><div><strong>${esc(name(phase))}</strong><small>${esc(PHASE_STATUS[status])} / ${doneCount} of ${inPhase.length} done${working?' / '+working+' in progress':''}</small><span class="rm-phase-bar" aria-hidden="true"><i style="width:${inPhase.length?Math.round(100*doneCount/inPhase.length):0}%"></i></span></div></li>`;
 }).join('')}</ol>`;
 const owner=nodes.filter(n=>n.state==='approval'),blocked=nodes.filter(n=>['blocked','failed'].includes(n.state));
 const notes=[state.door?`<p class="inline-warning rm-note">Phase door ${esc(state.door.boundary)}: ${esc(state.door.verdict)}${state.door.reason?' ('+esc(state.door.reason)+')':''}</p>`:'',
  owner.length?`<p class="rm-note rm-owner-wait">Waiting on the owner: ${owner.map(n=>taskLink(n.id,tasks)).join(', ')}</p>`:'',
  blocked.length?`<p class="inline-warning rm-note">Blocked: ${blocked.map(n=>taskLink(n.id,tasks)).join(', ')}</p>`:''].join('');
 return renderOne(o,{...plan,summary:plan.summary||op.done_when||'',groups},{...options,mode:'map',layout:'phases',selector:false,fixedOrder:true,showAll:true,
  heading:'Operation map',eyebrow:'PINNED OPERATION / '+opId,preface:strip+notes,
  // A wide container gives a phase with many tasks proportionally more width, so no phase column
  // runs far below the others; narrow screens keep the stacked layout.
  canvasStyle:'--rm-phase-cols:'+groups.map(g=>'minmax(200px,'+Math.max(1,Math.ceil(g.nodes.length/7))+'fr)').join(' ')});
}
export function renderRoadmap(o,options={}){
 const available=plans(o);

 const chosen=options.op||(options.all?'':prefs(scope(o)).op),op=available.find(p=>p.op===chosen)||workspace(available);
 return renderOne(o,op,options);
}

let markerSerial=0;
function draw(panel,model){
 const canvas=panel.querySelector('.rm-canvas'),svg=panel.querySelector('.rm-connectors');
 if(!canvas||!svg)return;
 svg.replaceChildren();
 if(model.mode!=='map'||panel.querySelector('.rm-body')?.hidden)return;
 const bounds=canvas.getBoundingClientRect(),ns='http://www.w3.org/2000/svg';
 svg.setAttribute('width',String(bounds.width));svg.setAttribute('height',String(bounds.height));
 const make=name=>document.createElementNS(ns,name),defs=make('defs'),marker=make('marker'),tip=make('path'),markerId='rm-arrow-'+(++markerSerial);
 marker.id=markerId;marker.setAttribute('viewBox','0 0 8 8');marker.setAttribute('refX','8');marker.setAttribute('refY','4');marker.setAttribute('markerWidth','8');marker.setAttribute('markerHeight','8');marker.setAttribute('markerUnits','userSpaceOnUse');marker.setAttribute('orient','auto');
 tip.setAttribute('d','M0 0 L8 4 L0 8 Z');tip.setAttribute('fill','context-stroke');marker.append(tip);defs.append(marker);svg.append(defs);
 const elements=new Map([...panel.querySelectorAll('[data-rm-node]')].map(e=>[e.dataset.rmNode,e]));
 const groups=new Map([...panel.querySelectorAll('[data-rm-group]')].map(e=>[e.dataset.rmGroup,e]));
 const membership=new Map((model.renderedGroups||[]).flatMap(g=>g.nodes.map(n=>[n.id,g.id])));
 function endpoint(id){
  const group=groups.get(membership.get(id));if(!group)return null;
  const folded=!group.open,element=folded?group.querySelector('.rm-group-port'):elements.get(id);
  if(!element)return null;
  const r=element.getBoundingClientRect();
  if(!r.width||!r.height)return null;
  return {key:(folded?'g:':'t:')+(folded?group.dataset.rmGroup:id),folded,element,group,
   left:r.left-bounds.left,right:r.right-bounds.left,top:r.top-bounds.top,bottom:r.bottom-bounds.top,
   x:r.left+r.width/2-bounds.left,y:r.top+r.height/2-bounds.top};
 }
 const visible=[...elements.keys()].map(endpoint).filter(e=>e&&!e.folded);
 const headers=[...groups.values()].map(group=>{const r=group.querySelector('summary').getBoundingClientRect();return {left:r.left-bounds.left,right:r.right-bounds.left,top:r.top-bounds.top,bottom:r.bottom-bounds.top};});
 const links=new Map();
 for(const edge of model.op.edges||[]){
  const from=endpoint(edge.from),to=endpoint(edge.to);if(!from||!to||from.key===to.key)continue;
  const key=JSON.stringify([from.key,to.key]);
  if(!links.has(key))links.set(key,{from,to,edges:[]});links.get(key).edges.push(edge);
 }
 const endpoints=[...new Map([...links.values()].flatMap(l=>[l.from,l.to]).map(e=>[e.key,e])).values()];
 const route=createRouter([...visible,...headers],endpoints,bounds.width,bounds.height);
 for(const {from:a,to:b,edges} of links.values()){
  const points=route(a,b);if(!points)continue;
  const path=make('path');path.setAttribute('d',points.map(([x,y],i)=>(i?'L':'M')+x+','+y).join(' '));
  path.setAttribute('marker-end','url(#'+markerId+')');
  path.dataset.rmEdge=edges[0].from+':'+edges[0].to;path.dataset.rmEdges=JSON.stringify(edges);path.dataset.rmFrom=a.key;path.dataset.rmTo=b.key;
  const p=prefs(model.key),focused=new Set(p.chat?nodesOf(model.op).filter(n=>n.session===p.chat).map(n=>n.id):p.focus?[p.focus]:[]);
  if(edges.some(e=>focused.has(e.from)||focused.has(e.to)))path.classList.add('rm-edge-selected');
  svg.append(path);
 }
}

function highlight(panel,model){
 const p=prefs(model.key),all=nodesOf(model.op),selected=new Set(p.chat?all.filter(n=>n.session===p.chat).map(n=>n.id):p.focus?[p.focus]:[]);
 const related=new Set();for(const edge of model.op.edges||[]){if(selected.has(edge.from))related.add(edge.to);if(selected.has(edge.to))related.add(edge.from);}
 for(const el of panel.querySelectorAll('[data-rm-node]')){const id=el.dataset.rmNode;el.classList.toggle('rm-selected',selected.has(id));el.classList.toggle('rm-related',!selected.has(id)&&related.has(id));el.classList.toggle('rm-dimmed',selected.size>0&&!selected.has(id)&&!related.has(id));}
 draw(panel,model);
}

// Capture controls by task/group identity so a refresh can move nodes without losing focus.
function controlId(el){
 if(!el)return null;
 const node=el.closest('[data-rm-node]')?.dataset.rmNode||'',group=el.closest('[data-rm-group]')?.dataset.rmGroup||'';
 let role=el.matches('summary')?'summary:'+(el.closest('[data-rm-picker]')?.dataset.rmPicker||''):el.hasAttribute('data-task')?'task:'+el.dataset.task+':'+el.classList.contains('rm-title'):el.classList.contains('rm-chat')?'chat-link':null;
 for(const key of ['focus','operation','chat','clear','category','completed','search','details','toggle','expand'])if(el.hasAttribute('data-rm-'+key))role=key+':'+el.getAttribute('data-rm-'+key);
 return role?JSON.stringify([node,group,role]):null;
}
export function rememberRoadmap(root=document){
 pendingFocus=null;
 for(const panel of root.querySelectorAll('[data-rm-key]')){
  const model=models.get(panel.dataset.rmKey);if(!model)continue;
  const p=prefs(model.key);
  p.scroll=panel.querySelector('.rm-scroll')?.scrollLeft||0;
  for(const group of panel.querySelectorAll('[data-rm-group]'))p.collapsed[group.dataset.rmGroup]=!group.open;
  p.pickers=[...panel.querySelectorAll('[data-rm-picker][open]')].map(el=>el.dataset.rmPicker);
  p.search=panel.querySelector('[data-rm-search]')?.value||'';
  keep(model.key);
  if(panel.contains(document.activeElement))pendingFocus={key:panel.dataset.rmKey,id:controlId(document.activeElement)};
 }
}

export function bindRoadmap(root=document){
 for(const panel of root.querySelectorAll('[data-rm-key]')){
  if(panel.dataset.rmBound)continue;panel.dataset.rmBound='1';
  const model=models.get(panel.dataset.rmKey);if(!model)continue;
  const p=prefs(model.key),scroll=panel.querySelector('.rm-scroll');
  for(const group of panel.querySelectorAll('[data-rm-group]'))if(Object.hasOwn(p.collapsed,group.dataset.rmGroup))group.open=!p.collapsed[group.dataset.rmGroup];
  scroll.scrollLeft=p.scroll||0;
  scroll.addEventListener('scroll',()=>{p.scroll=scroll.scrollLeft;keep(model.key);},{passive:true});
  for(const group of panel.querySelectorAll('details[data-rm-group]'))group.addEventListener('toggle',()=>{p.collapsed[group.dataset.rmGroup]=!group.open;keep(model.key);draw(panel,model);});
  const search=panel.querySelector('[data-rm-search]');
  function filterOperations(){
   const query=search.value.trim().toLowerCase();let count=0;
   for(const item of panel.querySelectorAll('[data-rm-operation]')){item.hidden=!item.dataset.rmSearchText.includes(query);if(!item.hidden)count++;}
   panel.querySelector('[data-rm-no-results]').hidden=Boolean(count);
  }
  if(search){search.value=p.search||'';filterOperations();search.addEventListener('input',()=>{p.search=search.value;keep(model.key);filterOperations();});}
  const pickers=[...panel.querySelectorAll('[data-rm-picker]')];
  p.pickers=(p.pickers||[]).slice(-1);
  for(const picker of pickers){
   picker.open=p.pickers.includes(picker.dataset.rmPicker);
   picker.addEventListener('toggle',()=>{
    if(!panel.isConnected)return;
    if(picker.open){
     for(const other of pickers)if(other!==picker)other.open=false;
     p.pickers=[picker.dataset.rmPicker];
    }else p.pickers=p.pickers.filter(name=>name!==picker.dataset.rmPicker);
    keep(model.key);
   });
  }
  function replace(options=model.options){
   rememberRoadmap(panel.parentElement);
   const wrapper=document.createElement('div');wrapper.innerHTML=options.layout==='phases'?renderPhaseMap(model.o,model.op.op,options):renderRoadmap(model.o,options);
   const next=wrapper.firstElementChild;panel.replaceWith(next);bindRoadmap(next.parentElement);
  }
  panel.addEventListener('keydown',event=>{if(event.key==='Escape'){const picker=event.target.closest('[data-rm-picker]');if(picker){picker.open=false;picker.querySelector('summary').focus();}}});
  panel.addEventListener('click',event=>{
   const button=event.target.closest('button');if(!button)return;
   const global=prefs(scope(model.o));
   if(button.hasAttribute('data-rm-toggle')){
    const holder=model.options.layout?p:global;
    holder.panelCollapsed=!panel.querySelector('.rm-body').hidden;
    panel.querySelector('.rm-body').hidden=holder.panelCollapsed;
    panel.classList.toggle('rm-folded',holder.panelCollapsed);
    button.setAttribute('aria-expanded',String(!holder.panelCollapsed));
    button.querySelector('.rm-toggle-label').textContent=holder.panelCollapsed?'Expand':'Collapse';
    keep(model.options.layout?model.key:scope(model.o));draw(panel,model);return;
   }
   if(button.hasAttribute('data-rm-expand')){
    const open=button.dataset.rmExpand==='true';
    for(const group of panel.querySelectorAll('details[data-rm-group]')){group.open=open;p.collapsed[group.dataset.rmGroup]=!open;}
    for(const more of panel.querySelectorAll('[data-rm-details]')){p.details[more.dataset.rmDetails]=open;more.setAttribute('aria-expanded',String(open));more.closest('[data-rm-node]').querySelector('.rm-node-info').hidden=!open;}
    keep(model.key);draw(panel,model);return;
   }
   if(button.hasAttribute('data-rm-details')){
    const id=button.dataset.rmDetails,expanded=button.getAttribute('aria-expanded')!=='true';
    p.details[id]=expanded;button.setAttribute('aria-expanded',String(expanded));button.closest('[data-rm-node]').querySelector('.rm-node-info').hidden=!expanded;
    keep(model.key);draw(panel,model);return;
   }
   if(button.hasAttribute('data-rm-operation')){
    const op=button.dataset.rmOperation;global.op=op;global.category='';keep(scope(model.o));
    button.closest('details').open=false;button.closest('details').querySelector('summary').focus();p.pickers=[];p.search='';if(search)search.value='';
    if(model.options.routeScope){
     const [page,query='']=location.hash.slice(1).split('?'),params=new URLSearchParams(query);
     if(op)params.set('op',op);else params.delete('op');location.hash=page+'?'+params.toString();
    }else replace({...model.options,op,all:!op});
    return;
   }
   if(button.hasAttribute('data-rm-category')||button.hasAttribute('data-rm-completed')){
    if(button.hasAttribute('data-rm-category'))global.category=button.dataset.rmCategory;
    else global.showDone=button.dataset.rmCompleted==='true';
    keep(scope(model.o));replace();return;
   }
   if(button.hasAttribute('data-rm-clear')){p.focus='';p.chat='';}
   else if(button.hasAttribute('data-rm-focus')){p.focus=button.dataset.rmFocus;p.chat='';}
   else if(button.hasAttribute('data-rm-chat')){
    p.chat=button.dataset.rmChat;p.focus='';const picker=button.closest('details');if(picker){picker.open=false;picker.querySelector('summary').focus();}
    for(const group of model.op.groups)if(group.nodes.some(n=>n.session===p.chat)){
     const el=[...panel.querySelectorAll('[data-rm-group]')].find(e=>e.dataset.rmGroup===group.id);if(el)el.open=true;
    }
   }else return;
   panel.querySelector('[data-rm-clear]').hidden=!p.chat&&!p.focus;
   for(const item of panel.querySelectorAll('[data-rm-chat]'))item.setAttribute('aria-pressed',String(item.dataset.rmChat===p.chat));
   keep(model.key);highlight(panel,model);
  });
  const observer=new ResizeObserver(()=>{if(!panel.isConnected){observer.disconnect();return;}draw(panel,model);});observer.observe(panel);
  highlight(panel,model);
  if(pendingFocus&&(pendingFocus.key===panel.dataset.rmKey||pendingFocus.id===JSON.stringify(['','','summary:operation']))){
   const target=[...panel.querySelectorAll('button,a,input,summary')].find(el=>controlId(el)===pendingFocus.id);
   if(target)target.focus({preventScroll:true});
  }
 }
 pendingFocus=null;
}

export function taskPlanDetail(o,id){
 const op=plans(o).find(p=>nodesOf(p).some(n=>n.id===id)),group=op?.groups.find(g=>g.nodes.some(n=>n.id===id)),node=group?.nodes.find(n=>n.id===id);
 if(!node)return '';
 const tasks=taskMap(o),next=(op.edges||[]).filter(e=>e.from===id).map(e=>e.to);
 return `<section class="detail-section rm-detail"><h3>Place in the plan</h3><p><strong>${esc(group.category||'Other work')} / ${esc(group.title)}</strong> / ${esc(node.label)}</p><p>Prerequisites: ${(node.after||[]).map(t=>taskLink(t,tasks)).join(', ')||'None recorded'}</p><p>Unblocks: ${next.map(t=>taskLink(t,tasks)).join(', ')||'No successor recorded'}</p>${node.resources?.length?`<p>Exclusive resources: ${node.resources.map(esc).join(', ')}</p>`:''}${node.reasons?.length?`<ul>${node.reasons.map(r=>`<li>${esc(r)}</li>`).join('')}</ul>`:''}</section>`;
}
