import {createRouter} from './roadmap-routing.js';
import {orb} from './orb.js';
import {levelChip} from './autodrive.js';
import {BEAM_RING} from './beam.js';
// Recorded work areas and dependencies shared by the cockpit and Work checklist.
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const models=new Map(), memory=new Map(), bound=new WeakSet(), shape=new WeakMap();
let pendingFocus=null;
const scope=o=>`${globalThis.location?.pathname||''}:${o.project?.name||'project'}:v2`;
function prefs(key){if(!memory.has(key)){let value={};try{value=JSON.parse(sessionStorage.getItem('alpaca.roadmap.'+key)||'{}');}catch{}memory.set(key,{collapsed:{},details:{},focus:'',chat:'',scroll:0,category:'',showDone:false,...(value&&typeof value==='object'?value:{})});}return memory.get(key);}
// Which cards the focus or chat selection marks: the class each card carries, in the markup itself so a
// live patch does not strip it and put it back.
function selectionClasses(op,p){
 const all=nodesOf(op),selected=new Set(p.chat?all.filter(n=>n.session===p.chat).map(n=>n.id):p.focus?[p.focus]:[]),related=new Set();
 for(const edge of op.edges||[]){if(selected.has(edge.from))related.add(edge.to);if(selected.has(edge.to))related.add(edge.from);}
 return id=>selected.has(id)?' rm-selected':selected.size?(related.has(id)?' rm-related':' rm-dimmed'):'';
}
function keep(key){try{sessionStorage.setItem('alpaca.roadmap.'+key,JSON.stringify(prefs(key)));}catch{}}
// The statuses listed under Not now (alpaca/ops.py NOT_NOW): never the focus, never current, never terminal.
export const NOT_NOW=['deferred'];
export const TRIGGER_MET='trigger met: reconsider';
// The phrases ops.not_now_words prints, from the task row's deferral. "Met" is read from the row, never decided here.
export function notNowWords(d){
 if(d?.error)return ['The deferral could not be read ('+d.error+')'];
 const fold=v=>String(v).split(/\s+/).filter(Boolean).join(' '),q=v=>v?'"'+fold(v)+'"':'(not recorded)';
 return ['Why not now: '+q(d?.reason),'Comes back when: '+q(d?.reactivate_when),...(d?.trigger_met?[TRIGGER_MET+(d.met_by?' ('+d.met_by+')':'')]:[])];
}
// The Not now panel of one operation, built from its task rows so a filter or a missing plan cannot empty it.
export function notNowPanel(o,opId){
 const parked=(o.tasks||[]).filter(t=>t.op===opId&&NOT_NOW.includes(String(t.status).toLowerCase())).sort((a,b)=>String(a.id).localeCompare(String(b.id),undefined,{numeric:true}));
 if(!parked.length)return '';
 return `<section class="rm-not-now" data-key="not-now:${esc(opId)}" aria-label="Not now"><h3>Not now <span class="rm-not-now-count">${parked.length}</span></h3><p>Parked on purpose. Not counted as open work; each comes back when its trigger is met.</p><ul>${parked.map(t=>{const d=t.deferral,words=notNowWords(d),met=!d?.error&&d?.trigger_met;
  return `<li class="rm-not-now-item${met?' rm-not-now-met':''}" data-key="not-now:${esc(t.id)}"><button type="button" data-task="${esc(t.id)}">${esc(t.title||t.id)} <small>${esc(t.id)}</small></button><ul class="rm-reasons">${words.map((w,k)=>`<li>${met&&k===words.length-1?`<strong>${esc(w)}</strong>`:esc(w)}</li>`).join('')}</ul>${d?.op_closed?`<p class="rm-not-now-closed">${esc(opId)} is closed.</p>`:''}</li>`;}).join('')}</ul></section>`;
}
// The AAR state of one operation as alpaca/aarview.py words it; no state, no panel.
const AAR_TONE={owed:'owed',changed:'changed',sealed:'sealed',draft:'draft'};
export function aarPanel(o,opId){
 const a=(o.ops||[]).find(x=>x.id===opId)?.aar;
 if(!a)return '';
 const n=a.overdue||0;
 return `<section class="rm-aar rm-aar-${AAR_TONE[a.badge]||'neutral'}${a.blocks_close?' rm-aar-blocking':''}" data-key="aar:${esc(opId)}" aria-label="After-action report"><h3>After-action report${a.label?` <span class="rm-aar-badge">${esc(a.label)}</span>`:''}</h3>${a.error?`<p class="rm-aar-error">${esc(a.error)}</p>`:''}${a.overdue_error?`<p class="rm-aar-error">${esc(a.overdue_error)}</p>`:''}${(a.lines||[]).length?`<ul class="rm-aar-lines">${a.lines.map(l=>`<li>${esc(l)}</li>`).join('')}</ul>`:''}${(a.triggers||[]).length?`<ul class="rm-aar-triggers">${a.triggers.map(t=>`<li data-key="trigger:${esc(t.id)}">${esc(t.text)}</li>`).join('')}</ul>`:''}${a.report?`<p><a class="rm-aar-link" href="#library?path=${encodeURIComponent(a.report)}">Open the report</a></p>`:''}${n?`<p>${n} action item${n===1?'':'s'} overdue, marked on the roadmap.</p>`:''}</section>`;
}
const overdueChip=t=>t?.aar_overdue?`<span class="rm-overdue" data-rm-overdue="${esc(t.id)}">${esc(t.aar_overdue.label)}</span>`:'';
const taskMap=o=>new Map((o.tasks||[]).map(t=>[t.id,t]));
const nodesOf=op=>(op.groups||[]).flatMap(g=>g.nodes||[]);
function plans(o){if(o.roadmap?.operations)return o.roadmap.operations;return [...new Set((o.tasks||[]).map(t=>t.op))].map(op=>({op,revision:0,edges:[],groups:[{id:'__unplanned__',title:'Needs organization',category:'Needs organization',nodes:o.tasks.filter(t=>t.op===op).map(t=>NOT_NOW.includes(String(t.status).toLowerCase())?{id:t.id,state:'deferred',label:'Not now',depth:0,after:[],reasons:notNowWords(t.deferral),session:null}:{id:t.id,state:'unplanned',label:'Needs organization',depth:0,after:[],reasons:['Task relationships have not been recorded.'],session:t.assigned_session})}]}));}
const operatorName=node=>({codex:'Codex',claude:'Claude','claude-code':'Claude Code',cli:'CLI'}[node.operator]||node.operator||'Chat');
const titleOf=(o,op)=>op.title||(o.ops||[]).find(x=>x.id===op.op)?.title||op.op;
// A short name for a pinned map: the op's recorded short title (op new --title, op title), else the
// plan's title, else the intent's lead-in before the first colon, else the intent cut at a word near
// 48 characters. The full intent stays in the op record.
const shortTitle=(o,op)=>{
 const rec=(o.ops||[]).find(x=>x.id===op.op);if(rec?.short_title)return rec.short_title;if(op.title)return op.title;
 const intent=String(rec?.title||'').trim(),head=intent.split(':')[0].trim();
 if(!intent)return '';if(head.length<=48)return head;
 const cut=head.slice(0,48),space=cut.lastIndexOf(' ');return (space>24?cut.slice(0,space):cut)+'...';
};
const opLabel=id=>/^op-0*(\d+)$/.test(id)?'Op'+id.replace(/^op-0*/,''):id;
const claimChip=(node,tasks,compact=false,extra='')=>{
 const task=tasks.get(node.id)||{};if(terminal(node,tasks)||!(node.session||task.claimant))return '';
 const who=node.session?`<span class="mono">${esc(String(node.session).slice(0,8))}</span> / ${esc(operatorName(node))}`:esc(task.claimant);
 const title=esc('Claimed by '+(node.session?operatorName(node)+' / '+node.session:task.claimant));
 return compact?`<span class="rm-claim rm-claim-line" title="${title}">${extra}<span class="mono">@${esc(String(node.session||task.claimant).slice(0,8))}</span>${node.session?' / '+esc(operatorName(node)):''}</span>`:`<span class="rm-claim" title="${title}">Claimed by ${who}</span>`;
};
const taskLink=(id,tasks)=>`<button type="button" data-task="${esc(id)}">${esc(tasks.get(id)?.title||id)} <small>${esc(id)}</small></button>`;
const terminal=(node,tasks)=>node.run?node.done:['done','skipped','cancelled','canceled'].includes(tasks.get(node.id)?.status);
// Research-run operations (one per `alpaca research` run, every group in the taskplan category
// 'research') are engine work with their own page. The workspace map shows each run as one card,
// led by its most urgent open task, instead of every attempt; picking the run's op still lists them.
const isResearch=op=>Boolean(op.groups?.length)&&op.groups.every(g=>g.category==='research');
const RUN_RANK=['failed','blocked','approval','assignment_expired','working','ready','resources','waiting','unplanned'];
// The engine's run state (overview research_runs, alpaca/hub.py _research_states) decides the card:
// a finished or stopped run is done whatever tasks it left open; a held run waits on the owner.
const RUN_STATE={running:['working','Running'],starting:['working','Starting'],stopping:['working','Stopping'],
 held:['approval','Waiting on the owner'],halted:['approval','Halted: owner decision'],paused:['approval','Paused: budget decision'],
 interrupted:['waiting','Interrupted, can resume'],resumable:['waiting','Resumable'],finished:['done','Finished'],stopped:['done','Stopped']};
function runNode(op,tasks,o){
 const nodes=nodesOf(op),open=nodes.filter(n=>!terminal(n,tasks)),active=open.filter(n=>n.state!=='deferred'),
  lead=RUN_RANK.map(st=>active.find(n=>n.state===st)).find(Boolean)||active[0]||open[0];
 const rec=o.research_runs?.[op.op],run=rec?.run||(String(op.groups[0].title).match(/rr-\d+/)||[op.op])[0],[state,label]=RUN_STATE[rec?.state]||[lead?lead.state:'done',lead?lead.label:'Complete'];
 const done=rec&&RUN_STATE[rec.state]?state==='done':!open.length;
 return {id:'run:'+op.op,run,op:op.op,state,label,done,open:open.length,total:nodes.length,session:done?null:lead?.session,lead:done?null:lead?.id,after:[],reasons:[]};
}
// Live work rides a border beam (beam.css); work that needs attention, a warning or an owner
// decision rides the red one. The state label carries the matching orb.
const ALERT=['approval','blocked','failed','assignment_expired'];
const beamOf=(node,tasks)=>terminal(node,tasks)?'':node.state==='working'?' beam':ALERT.includes(node.state)?' beam beam-alert':'';
const ringOf=(node,tasks)=>beamOf(node,tasks)?BEAM_RING:'';
const STATE_ORB={working:'working',approval:'breathing',assignment_expired:'breathing',blocked:'searching',failed:'searching',ready:'connecting',resources:'weaving'};
const stateOrb=(node,tasks)=>!terminal(node,tasks)&&STATE_ORB[node.state]?`<span class="rm-state-orb">${orb(STATE_ORB[node.state],20)}</span>`:'';
const nodeLevel=(o,node,tasks)=>terminal(node,tasks)?'':levelChip(o.levels?.[node.session||tasks.get(node.id)?.assigned_session],true);
function workspace(available,tasks,o){
 const groups=new Map(),research=available.filter(isResearch),plain=available.filter(op=>!isResearch(op));
 for(const op of plain)for(const group of op.groups){
  const category=group.category||'Other work',key=JSON.stringify([category,group.title]);
  if(!groups.has(key))groups.set(key,{...group,id:key,category,nodes:[]});
  groups.get(key).nodes.push(...group.nodes);
 }
 const runGroups=research.length?[{id:'__research_runs__',title:'Research runs',category:'Research runs',nodes:research.map(op=>runNode(op,tasks,o))}]:[];
 return {op:'all',title:'All operations',revision:1,groups:[...groups.values(),...runGroups],edges:plain.flatMap(op=>op.edges||[]),error:available.filter(op=>op.error).map(op=>op.op+': '+op.error).join('; ')};
}
function runCard(node,tasks,o,mark=''){
 const lead=node.lead&&tasks.get(node.lead);
 return `<article class="rm-node rm-compact rm-run rm-${esc(node.state)}${beamOf(node,tasks)}${mark}" data-rm-node="${esc(node.id)}" data-key="n:${esc(node.id)}">
   <div class="rm-node-top"><span class="mono">${esc(node.run)}</span><span class="rm-state" title="${esc(node.label)}">${stateOrb(node,tasks)}${esc(node.label)}</span></div>${node.session&&!node.done?`<span class="rm-claim rm-claim-line">${nodeLevel(o,node,tasks)}<span class="mono">@${esc(String(node.session).slice(0,8))}</span></span>`:''}
   <a class="rm-title" href="#research?run=${encodeURIComponent(node.run)}" title="Open research run ${esc(node.run)}"><span class="rm-title-text">Research run ${esc(node.run)}</span></a>
   <small class="rm-run-count">${node.total-node.open} of ${node.total} tasks done${lead?` / now ${esc(lead.id)}: ${esc(lead.title||'')}`:''}</small>
  ${ringOf(node,tasks)}</article>`;
}
function nodeCard(node,tasks,o,compact=false,expanded=false,mark=''){
 if(node.run)return runCard(node,tasks,o,mark);
 const task=tasks.get(node.id)||{id:node.id,title:node.id},needs=node.after||[],reasons=node.reasons||[],op=plans(o).find(p=>p.op===task.op);
 if(compact){
  return `<article class="rm-node rm-compact rm-${esc(node.state)}${beamOf(node,tasks)}${mark}" data-rm-node="${esc(node.id)}" data-key="n:${esc(node.id)}">
   <div class="rm-node-top"><span class="mono">${esc(node.id)}</span><span class="rm-state" title="${esc(node.label)}">${stateOrb(node,tasks)}${esc(node.label||'Needs organization')}</span></div>${overdueChip(task)}${claimChip(node,tasks,true,nodeLevel(o,node,tasks))}
   <button type="button" class="rm-title" data-task="${esc(node.id)}" title="${esc(task.title)}"><span class="rm-title-text">${esc(task.title)}</span></button>
   <button type="button" class="rm-more" data-rm-details="${esc(node.id)}" aria-controls="rm-info-${esc(node.id)}" aria-expanded="${expanded}" aria-label="Details and actions for ${esc(node.id)}" title="Details and actions">&#8943;</button>
   <div id="rm-info-${esc(node.id)}" class="rm-node-info" ${expanded?'':'hidden'}><div class="rm-context">${esc(op?titleOf(o,op):task.op)}${task.phase?' / '+esc(task.phase):''}</div>
   ${node.state==='unplanned'?`<small class="rm-recorded">Recorded: ${esc(task.status||'unknown')}</small>`:''}
   <div class="rm-needs">${needs.length?'After '+needs.map(id=>taskLink(id,tasks)).join(' + '):node.state==='unplanned'?'Order not recorded':'No task prerequisites'}</div>
   ${reasons.length?`<ul class="rm-reasons">${reasons.map(r=>`<li>${esc(r)}</li>`).join('')}</ul>`:''}<div class="rm-node-foot">${node.session?`<a class="rm-chat" href="#sessions?sid=${encodeURIComponent(node.session)}" title="${esc(operatorName(node)+' / '+node.session)}">Open chat</a>`:''}<button type="button" class="rm-focus" data-rm-focus="${esc(node.id)}" aria-label="Trace ${esc(node.id)} dependencies">Trace path</button></div></div>
  ${ringOf(node,tasks)}</article>`;
 }
 return `<article class="rm-node rm-${esc(node.state)}${beamOf(node,tasks)}${mark}" data-rm-node="${esc(node.id)}" data-key="n:${esc(node.id)}">
 <div class="rm-node-top"><span class="mono">${esc(node.id)}</span><span class="rm-state" title="${esc(node.label)}">${stateOrb(node,tasks)}${esc(node.label||'Needs organization')}</span></div>${overdueChip(task)}
 <button type="button" class="rm-title" data-task="${esc(node.id)}" title="${esc(task.title)}">${esc(task.title)}</button>${claimChip(node,tasks)}${nodeLevel(o,node,tasks)}
 <div class="rm-context">${esc(op?titleOf(o,op):task.op)}${task.phase?' / '+esc(task.phase):''}</div>
 ${node.state==='unplanned'?`<small class="rm-recorded">Recorded: ${esc(task.status||'unknown')}</small>`:''}
 ${needs.length?`<div class="rm-needs"><span>After</span> ${needs.map(id=>taskLink(id,tasks)).join(' <span>+</span> ')}</div>`:'<div class="rm-needs">'+(node.state==='unplanned'?'Order not recorded':'No task prerequisites')+'</div>'}
 ${reasons.length?`<ul class="rm-reasons">${reasons.map(r=>`<li>${esc(r)}</li>`).join('')}</ul>`:''}
 <div class="rm-node-foot">${node.session?`<a class="rm-chat" href="#sessions?sid=${encodeURIComponent(node.session)}" title="${esc(node.session)}">Open assigned chat</a>`:`<span class="rm-unassigned">${task.claimant?'Worker: '+esc(task.claimant):terminal(node,tasks)?'Completed work':'No assigned chat'}</span>`}<button type="button" class="rm-focus" data-rm-focus="${esc(node.id)}" aria-label="Trace ${esc(node.id)} dependencies">Trace</button></div>
 ${ringOf(node,tasks)}</article>`;
}
function operationPicker(o,selected){
 const tasks=taskMap(o),available=plans(o),row=(id,title,nodes)=>{
  const done=nodes.filter(n=>tasks.get(n.id)?.status==='done').length,working=nodes.filter(n=>['working','assignment_expired'].includes(n.state)).length,parked=nodes.filter(n=>n.state==='deferred').length;
  return `<button type="button" class="rm-choice" data-rm-operation="${esc(id)}" aria-pressed="${selected===id}" data-rm-search-text="${esc((title+' '+id).toLowerCase())}"><span><strong>${esc(title)}</strong><small>${esc(id||'Workspace')} / ${done} of ${nodes.length} done${working?' / '+working+' in progress':''}${parked?' / '+parked+' not now':''}</small></span><span aria-hidden="true">${selected===id?'&#10003;':'&rarr;'}</span></button>`;
 };
 return `<details class="rm-picker" data-rm-picker="operation"><summary aria-label="Choose operation">${esc(selected?titleOf(o,available.find(p=>p.op===selected)||{op:selected}):'All operations')} <span aria-hidden="true">&#9662;</span></summary><div class="rm-picker-menu"><label class="rm-search-label">Find an operation<input type="search" data-rm-search placeholder="Search name or ID" autocomplete="off"></label>${row('','All operations',available.flatMap(nodesOf))}${available.filter(op=>!isResearch(op)).map(op=>row(op.op,titleOf(o,op),nodesOf(op))).join('')}${available.some(isResearch)?`<p class="rm-picker-hint">Research runs</p>${available.filter(isResearch).map(op=>row(op.op,titleOf(o,op),nodesOf(op))).join('')}`:''}<p data-rm-no-results hidden>No matching operations.</p></div></details>`;
}
function chatPicker(nodes,tasks,p){
 const chats=new Map();for(const n of nodes)if(n.session){if(!chats.has(n.session))chats.set(n.session,[]);chats.get(n.session).push(n);}
 if(!chats.size)return '<span class="rm-no-chats">No assigned chats in this view</span>';
 return `<details class="rm-picker" data-rm-picker="chat"><summary>Find a chat <span class="rm-count">${chats.size}</span> <span aria-hidden="true">&#9662;</span></summary><div class="rm-picker-menu"><p class="rm-picker-hint">Highlight a chat by the work assigned to it.</p>${[...chats].map(([sid,assigned])=>`<button type="button" class="rm-choice" data-rm-chat="${esc(sid)}" aria-pressed="${p.chat===sid}"><span><strong>${assigned.map(n=>esc(n.run?'Research run '+n.run:tasks.get(n.id)?.title||n.id)).join(', ')}</strong><small>${esc(operatorName(assigned[0]))} / ${assigned.map(n=>esc(n.label)).join(', ')} / ${esc(sid)}</small></span></button>`).join('')}</div></details>`;
}
// Each chat holding unfinished work, with the tasks it claims; a chip highlights that chat's cards.
function claimsStrip(nodes,tasks,p){
 const chats=new Map();for(const n of nodes)if(n.session&&!terminal(n,tasks)){if(!chats.has(n.session))chats.set(n.session,[]);chats.get(n.session).push(n);}
 if(!chats.size)return '';
 return `<div class="rm-claims" role="group" aria-label="Claimed work by chat"><span class="rm-claims-label">Claimed now</span>${[...chats].map(([sid,assigned])=>`<button type="button" class="rm-claim-chip" data-rm-chat="${esc(sid)}" aria-pressed="${p.chat===sid}" title="${esc(operatorName(assigned[0])+' / '+sid)}"><span class="mono">${esc(String(sid).slice(0,8))}</span> ${esc(operatorName(assigned[0]))}: ${assigned.map(n=>`<strong>${esc(n.run||n.id)}</strong> ${esc(n.run?'research run':tasks.get(n.id)?.title||'')}`).join(', ')}</button>`).join('')}</div>`;
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
 // The model keeps its identity across renders: handlers bound to a panel the patch kept read the newest data.
 const model={o,op,mode,key,options,visibleIds,selector,renderedGroups:groups},prior=models.get(viewKey);
 if(prior)Object.assign(prior,model);else models.set(viewKey,model);
 const mark=selectionClasses(op,p);
 const done=all.filter(n=>n.run?n.done:tasks.get(n.id)?.status==='done').length,working=scoped.filter(n=>n.state==='working'||n.state==='assignment_expired').length,ready=scoped.filter(n=>n.state==='ready').length,parked=all.filter(n=>n.state==='deferred').length;
 const holder=layout?p:global,folded=mode==='map'&&Boolean(holder.panelCollapsed),bodyId='rm-body-'+encodeURIComponent(viewKey);
 const title=op.op==='all'?'Work by area':titleOf(o,op),summary=op.summary||(op.op==='all'?'Follow each feature from its current task to what comes next.':'Follow the recorded prerequisites within this operation.');
 return `<section class="panel rm-panel rm-${mode==='map'?'map':'list'} ${layout?'rm-'+esc(layout):''} ${folded?'rm-folded':''}" data-rm-key="${esc(viewKey)}" data-key="rm:${esc(viewKey)}">
 ${mode==='map'?`<div class="rm-shell-heading"><button type="button" data-rm-toggle aria-expanded="${!folded}" aria-controls="${esc(bodyId)}"><span class="rm-chevron" aria-hidden="true">&gt;</span><strong>${esc(heading||'Task roadmap')}</strong><span class="rm-shell-count">${working} in progress / ${ready} ready / ${done} of ${all.length} done${parked?' / '+parked+' not now':''}</span><span class="rm-toggle-label">${folded?'Expand':'Collapse'}</span></button></div>`:''}
 <div class="rm-body" id="${esc(bodyId)}" ${folded?'hidden':''}>
 <div class="rm-heading"><div><span class="eyebrow">${esc(eyebrow||(mode==='map'?'TASK ROADMAP':'GROUPED CHECKLIST'))}</span><h2>${esc(title)}</h2><p>${esc(summary)}</p><p class="rm-progress-line">${working} in progress <span aria-hidden="true">/</span> ${ready} ready <span aria-hidden="true">/</span> ${done} of ${all.length} done${parked?` <span aria-hidden="true">/</span> ${parked} not now`:''}</p></div><div class="rm-controls">${selector?operationPicker(o,op.op==='all'?'':op.op):''}${chatPicker(shown,tasks,p)}<button type="button" class="button rm-clear" data-rm-clear ${!p.chat&&!p.focus?'hidden':''}>Clear focus</button></div></div>
 ${preface}
 ${op.op==='all'?'':aarPanel(o,op.op)+notNowPanel(o,op.op)}
 <div class="rm-viewbar">${layout?'':`<div class="rm-categories" role="group" aria-label="Work area">${['',...categories].map(c=>`<button type="button" data-rm-category="${esc(c)}" aria-pressed="${c===category}">${esc(c||'All areas')}</button>`).join('')}</div>`}<div class="rm-fold-all" role="group" aria-label="Open or close cards"><button type="button" data-rm-expand="true">Open all</button><button type="button" data-rm-expand="summary">Summary</button><button type="button" data-rm-expand="false">Close all</button></div>${layout?'':`<div class="rm-visibility" role="group" aria-label="Plan scope"><button type="button" data-rm-completed="false" aria-pressed="${!showDone}" ${completeOnly?'disabled':''}>Active work</button><button type="button" data-rm-completed="true" aria-pressed="${showDone}">Full plan</button></div>`}</div>
 ${claimsStrip(shown,tasks,p)}
 ${op.error?`<p class="inline-warning rm-note">Plan unavailable: ${esc(op.error)}</p>`:''}
 <div class="rm-legend"><span><i class="rm-dot rm-dot-working"></i>In progress</span><span><i class="rm-dot rm-dot-ready"></i>Ready</span><span><i class="rm-dot rm-dot-waiting"></i>Waiting</span><span><i class="rm-dot rm-dot-blocked"></i>Blocked or failed</span><span><i class="rm-dot rm-dot-done"></i>Done</span>${all.some(n=>n.state==='deferred')?'<span><i class="rm-dot rm-dot-deferred"></i>Not now</span>':''}${all.some(n=>tasks.get(n.id)?.aar_overdue)?'<span><i class="rm-dot rm-dot-overdue"></i>Overdue action item</span>':''}<span><i class="rm-dot rm-dot-unplanned"></i>Needs organization</span><span class="rm-legend-help">Arrows show prerequisites. Tasks sharing a resource wait their turn.</span></div>
 ${!groups.length?`<p class="rm-note">${all.length?'No tasks match these filters.':'No tasks have been recorded yet.'}</p>`:''}
 <div class="rm-scroll" data-key="scroll"><div class="rm-canvas"${canvasStyle?` style="${esc(canvasStyle)}"`:''}><svg class="rm-connectors" aria-hidden="true" data-key="rm-connectors" data-morph-keep></svg>${groups.map(group=>{
  const count=group.nodes.filter(n=>terminal(n,tasks)).length,complete=count===group.nodes.length,groupNodes=group.nodes.filter(n=>shownIds.has(n.id));
  const expanded=!Object.hasOwn(p.collapsed,group.id)?!complete:!p.collapsed[group.id];
  const current=group.nodes.find(n=>['working','assignment_expired'].includes(n.state))||group.nodes.find(n=>n.state==='ready')||group.nodes.find(n=>!terminal(n,tasks)&&n.state!=='deferred')||group.nodes.find(n=>n.state==='deferred');
  return `<details class="rm-group ${group.id.includes('__unplanned__')?'rm-unplanned-group':''}" data-rm-group="${esc(group.id)}" data-key="g:${esc(group.id)}" ${expanded?'open':''}><summary><span class="rm-group-port" aria-hidden="true"></span><span class="rm-chevron" aria-hidden="true">&gt;</span><span class="rm-group-name"><small>${esc(group.category||'Other work')}</small><strong>${esc(group.title)}</strong></span><span class="rm-group-count">${count}/${group.nodes.length}${mode==='map'?'':' complete'}</span>${(late=>late?`<span class="rm-group-overdue">${late} overdue</span>`:'')(group.nodes.filter(n=>tasks.get(n.id)?.aar_overdue).length)}<span class="rm-group-current">${current&&['working','assignment_expired'].includes(current.state)?orb('working',20):current&&ALERT.includes(current.state)?orb('breathing',20):''}${current?esc((tasks.get(current.id)?.title||current.id)+' / '+current.label):'Complete'}</span></summary>${groupNodes.length?(group.subgroups?group.subgroups.map(s=>{const inSub=s.nodes.filter(n=>shownIds.has(n.id));return inSub.length?`<div class="rm-sub" data-rm-sub="${esc(s.id)}" data-key="s:${esc(s.id)}"><div class="rm-sub-title">${esc(s.title)}${s.note?`<small>${esc(s.note)}</small>`:''}</div><div class="rm-track">${inSub.map(n=>nodeCard(n,tasks,o,mode==='map',Boolean(p.details[n.id]),mark(n.id))).join('')}</div></div>`:'';}).join(''):`<div class="rm-track">${groupNodes.map(n=>nodeCard(n,tasks,o,mode==='map',Boolean(p.details[n.id]),mark(n.id))).join('')}</div>`):`<p class="rm-note">${count} completed tasks. <button type="button" class="button" data-rm-completed="true">View full plan</button></p>`}</details>`;
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
 const order=[...declared,...[...byPhase.keys()].filter(p=>p&&!declared.includes(p))],sequence=order.slice();
 // Tasks outside the phase sequence are research ahead of the plan: they sit at the top of the first
 // phase with work under a Research label instead of taking a column of their own.
 const unphased=byPhase.get('')||[],host=sequence.find(p=>byPhase.has(p));if(unphased.length&&!host)order.push('');
 const name=phase=>phase?phase[0].toUpperCase()+phase.slice(1):'Not in a phase';
 const groups=order.filter(p=>byPhase.has(p)).map((phase,i)=>{
  const subgroups=[],items=phase===host?[...unphased,...byPhase.get(phase)]:byPhase.get(phase);
  if(phase===host)subgroups.push({id:'__research__',title:'Research',note:'Outside the phase sequence',nodes:unphased.map(x=>x.node)});
  for(const {node,group} of byPhase.get(phase)){let sub=subgroups.find(s=>s.id===group.id);if(!sub){sub={id:group.id,title:group.title,nodes:[]};subgroups.push(sub);}sub.nodes.push(node);}
  return {id:'phase:'+(phase||'none'),title:name(phase),category:phase?'Phase '+(sequence.indexOf(phase)+1)+' / '+PHASE_STATUS[phaseStatus(phase,state)]:'Outside the phase sequence',nodes:items.map(x=>x.node),subgroups,wide:byPhase.get(phase).length>7};
 });
 const nodes=nodesOf(plan),count=(list,states)=>list.filter(n=>states.includes(n.state)).length;
 const strip=`<ol class="rm-phase-strip" aria-label="Phases">${sequence.map((phase,i)=>{
  const inPhase=(byPhase.get(phase)||[]).map(x=>x.node),doneCount=inPhase.filter(n=>terminal(n,tasks)).length,status=phaseStatus(phase,state),working=count(inPhase,['working','assignment_expired']);
  return `<li class="rm-phase rm-phase-${status}"><span class="rm-phase-step">${working?orb('working',20,'Work in progress in this phase'):i+1}</span><div><strong>${esc(name(phase))}</strong><small>${esc(PHASE_STATUS[status])} / ${doneCount} of ${inPhase.length} done${working?' / '+working+' in progress':''}</small><span class="rm-phase-bar" aria-hidden="true"><i style="width:${inPhase.length?Math.round(100*doneCount/inPhase.length):0}%"></i></span></div></li>`;
 }).join('')}</ol>${unphased.length?`<p class="rm-note rm-unphased">${unphased.length} task${unphased.length===1?' is':'s are'} not in a phase; ${host?`they are listed under Research in ${esc(name(host))}`:'they are listed after the phases'}.</p>`:''}`;
 const owner=nodes.filter(n=>n.state==='approval'),blocked=nodes.filter(n=>['blocked','failed'].includes(n.state));
 const notes=[state.door?`<p class="inline-warning rm-note">Phase door ${esc(state.door.boundary)}: ${esc(state.door.verdict)}${state.door.reason?' ('+esc(state.door.reason)+')':''}</p>`:'',
  owner.length?`<p class="rm-note rm-owner-wait">Waiting on the owner: ${owner.map(n=>taskLink(n.id,tasks)).join(', ')}</p>`:'',
  blocked.length?`<p class="inline-warning rm-note">Blocked: ${blocked.map(n=>taskLink(n.id,tasks)).join(', ')}</p>`:''].join('');
 // Who is driving this op: each session holding unfinished work here, with its autodrive level.
 const held=new Map();for(const n of nodes){const sid=!terminal(n,tasks)&&(n.session||tasks.get(n.id)?.assigned_session);if(sid)held.set(sid,(held.get(sid)||0)+1);}
 const driving=`<div class="rm-autodrive" aria-label="Autodrive level of the sessions working this operation"><span class="rm-autodrive-label">${orb(held.size?'working':'breathing',20)}Autodrive</span>${held.size?[...held].map(([sid,n])=>`<a class="rm-driver" href="#sessions?sid=${encodeURIComponent(sid)}" title="${esc('Session '+sid)}">${levelChip(o.levels?.[sid])}<span class="mono">${esc(String(sid).slice(0,8))}</span><small>${n} task${n===1?'':'s'}</small></a>`).join(''):'<span class="rm-autodrive-none">No session holds open work in this operation</span>'}</div>`;
 const mapName=shortTitle(o,plan);
 return renderOne(o,{...plan,title:mapName||plan.title,summary:plan.summary||op.done_when||'',groups},{...options,mode:'map',layout:'phases',selector:false,fixedOrder:true,showAll:true,
  heading:'Operation map '+opLabel(opId)+(mapName?' - '+mapName:''),eyebrow:'PINNED OPERATION / '+opId,preface:driving+strip+notes,
  // A wide container gives a phase with many tasks twice the width, room for its two-column track,
  // so no phase column runs far below the others; narrow screens keep the stacked layout.
  canvasStyle:'--rm-phase-cols:'+groups.map(g=>g.wide?'minmax(300px,2fr)':'minmax(200px,1fr)').join(' ')});
}
export function renderRoadmap(o,options={}){
 const available=plans(o);

 const chosen=options.op||(options.all?'':prefs(scope(o)).op),op=available.find(p=>p.op===chosen)||workspace(available,taskMap(o),o);
 // An op named on purpose that has no task has no plan, but its AAR and Not now facts belong to it.
 const bare=options.op&&!available.some(p=>p.op===options.op)&&(o.ops||[]).find(x=>x.id===options.op);
 if(bare)return `<section class="panel rm-panel rm-list"><div class="rm-body"><div class="rm-heading"><div><span class="eyebrow">OPERATION</span><h2>${esc(opLabel(bare.id))}</h2></div></div>${aarPanel(o,bare.id)+notNowPanel(o,bare.id)}<p class="rm-note">No tasks have been recorded yet.</p></div></section>`;
 return renderOne(o,op,options);
}

// What decides where the cards sit: the cards and groups shown, the recorded edges, which groups and
// card details are open. A live patch routes the arrows again only when this changed.
function layoutShape(panel,model){
 return JSON.stringify([model.mode,model.renderedGroups.map(g=>[g.id,g.nodes.map(n=>n.id)]),(model.op.edges||[]).map(e=>[e.from,e.to]),
  [...panel.querySelectorAll('[data-rm-group]')].map(g=>g.open),[...panel.querySelectorAll('[data-rm-details]')].map(b=>b.getAttribute('aria-expanded')),[...panel.querySelectorAll('[data-rm-overdue]')].map(e=>e.dataset.rmOverdue),panel.querySelector('.rm-body')?.hidden]);
}
let markerSerial=0;
function draw(panel,model){
 const canvas=panel.querySelector('.rm-canvas'),svg=panel.querySelector('.rm-connectors');
 if(!canvas||!svg)return;
 shape.set(panel,layoutShape(panel,model));
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
 const mark=selectionClasses(model.op,prefs(model.key));
 for(const el of panel.querySelectorAll('[data-rm-node]')){const marks=mark(el.dataset.rmNode);for(const name of ['rm-selected','rm-related','rm-dimmed'])el.classList.toggle(name,marks.includes(name));}
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
  if(bound.has(panel))continue;bound.add(panel);
  const model=models.get(panel.dataset.rmKey);if(!model)continue;
  const p=prefs(model.key),scroll=panel.querySelector('.rm-scroll');
  for(const group of panel.querySelectorAll('[data-rm-group]'))if(Object.hasOwn(p.collapsed,group.dataset.rmGroup))group.open=!p.collapsed[group.dataset.rmGroup];
  scroll.scrollLeft=p.scroll||0;
  // Scroll, toggle and search input are listened for on the panel, not on the elements: a live patch can
  // add a group or a picker, and what it adds is covered without binding again.
  panel.addEventListener('scroll',event=>{if(event.target.matches('.rm-scroll')){p.scroll=event.target.scrollLeft;keep(model.key);}},{passive:true,capture:true});
  panel.addEventListener('toggle',event=>{
   const el=event.target;
   if(el.matches('details[data-rm-group]')){p.collapsed[el.dataset.rmGroup]=!el.open;keep(model.key);draw(panel,model);return;}
   if(!el.matches('[data-rm-picker]')||!panel.isConnected)return;
   if(el.open){
    for(const other of panel.querySelectorAll('[data-rm-picker]'))if(other!==el)other.open=false;
    p.pickers=[el.dataset.rmPicker];
   }else p.pickers=p.pickers.filter(name=>name!==el.dataset.rmPicker);
   keep(model.key);
  },true);
  function filterOperations(){
   const search=panel.querySelector('[data-rm-search]');if(!search)return;
   const query=search.value.trim().toLowerCase();let count=0;
   for(const item of panel.querySelectorAll('[data-rm-operation]')){item.hidden=!item.dataset.rmSearchText.includes(query);if(!item.hidden)count++;}
   panel.querySelector('[data-rm-no-results]').hidden=Boolean(count);
  }
  const search=panel.querySelector('[data-rm-search]');
  if(search){search.value=p.search||'';filterOperations();}
  panel.addEventListener('input',event=>{if(event.target.matches('[data-rm-search]')){p.search=event.target.value;keep(model.key);filterOperations();}},true);
  panel._rmFilter=filterOperations;
  p.pickers=(p.pickers||[]).slice(-1);
  for(const picker of panel.querySelectorAll('[data-rm-picker]'))picker.open=p.pickers.includes(picker.dataset.rmPicker);
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
    // Open all: groups and card details open. Summary: groups open, card details closed. Close all: groups closed.
    const mode=button.dataset.rmExpand,open=mode!=='false',details=mode==='true';
    for(const group of panel.querySelectorAll('details[data-rm-group]')){group.open=open;p.collapsed[group.dataset.rmGroup]=!open;}
    for(const more of panel.querySelectorAll('[data-rm-details]')){p.details[more.dataset.rmDetails]=details;more.setAttribute('aria-expanded',String(details));more.closest('[data-rm-node]').querySelector('.rm-node-info').hidden=!details;}
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

// After a live patch of a container holding roadmap panels: bind a panel the patch added, put back the
// operation filter a patch may have undone, and route the arrows again only when the layout changed.
export function patchedRoadmap(root=document){
 bindRoadmap(root);
 for(const panel of root.querySelectorAll('[data-rm-key]')){
  const model=models.get(panel.dataset.rmKey);if(!model)continue;
  if(panel.querySelector('[data-rm-search]')?.value)panel._rmFilter?.();
  if(shape.get(panel)!==layoutShape(panel,model))draw(panel,model);
 }
}

export function taskPlanDetail(o,id){
 const op=plans(o).find(p=>nodesOf(p).some(n=>n.id===id)),group=op?.groups.find(g=>g.nodes.some(n=>n.id===id)),node=group?.nodes.find(n=>n.id===id);
 if(!node)return '';
 const tasks=taskMap(o),next=(op.edges||[]).filter(e=>e.from===id).map(e=>e.to);
 return `<section class="detail-section rm-detail"><h3>Place in the plan</h3><p><strong>${esc(group.category||'Other work')} / ${esc(group.title)}</strong> / ${esc(node.label)}</p><p>Prerequisites: ${(node.after||[]).map(t=>taskLink(t,tasks)).join(', ')||'None recorded'}</p><p>Unblocks: ${next.map(t=>taskLink(t,tasks)).join(', ')||'No successor recorded'}</p>${node.resources?.length?`<p>Exclusive resources: ${node.resources.map(esc).join(', ')}</p>`:''}${node.reasons?.length?`<ul>${node.reasons.map(r=>`<li>${esc(r)}</li>`).join('')}</ul>`:''}</section>`;
}
