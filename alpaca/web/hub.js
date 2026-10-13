import {renderMission,bindMission,rememberMission,patchedMission,missionMoved} from './mission.js';
import {renderResearchPage,bindResearch} from './research.js';
import {icon, hydrate} from './icons.js';
import {orb} from './orb.js';
import {loader,routeLoader} from './loader.js';
import {updatePulse,startPulse} from './pulse.js';
import {updateArthur,startArthur,noteBurnBoard} from './arthur.js';
import {documentNavigation, proofOverview} from './document.js';
import {renderRoadmap, renderPhaseMap, bindRoadmap, rememberRoadmap, patchedRoadmap, taskPlanDetail} from './roadmap.js';
import {renderAnalytics, analyticsIndex, renderCrew} from './analytics.js';
import {sessionList, dayRange, AGENT_CHOICES, SORT_CHOICES} from './sessionlist.js';
import {renderBurn} from './burn.js';
import {renderInflight, loadEarlier, inflightCount, inflightButton} from './inflight.js';
import {renderVcs,bindVcs,captureVcs,statusCell,backlogNote,overallNote} from './vcs.js';
import {renderRecords} from './records.js';
import {mountGraph} from './evgraph.js';
import {installTips} from './tips.js';
import {renderCockpit, cockpitTaskIds, renderCockpitExtras, renderWorkCards, renderWorkTimeline, segmented, dayChart, runVisuals, collectionVisuals, categoryBars, eventSummary, liveTimeline, laneOf, todayKey, SPANS, clock, startClocks, setStageNames, profileProblems, STATE_LABELS} from './cockpit.js';
import {createLive} from './live.js';
import {patch as patchHtml, render as renderHtml, forget as forgetHtml} from './morph.js';
import {changedFlags, dependsOn, autoRefresh, needs, createDirty, connectionHealth, healthMeter, refreshDelay, burnPoll, routeAlias, ROUTE_NEEDS, FLAGS} from './liveflags.js';
import {PAGES,PAGE} from './pages.js';
import {runsOverview, runDashboard, renderScan, renderFindings, renderLogLines, jumpToLine, renderReview, reviewLines, renderContract} from './runlog.js';
const $ = (q, root=document) => root.querySelector(q);
const readerEngaged=()=>document.hidden||document.activeElement?.closest('#main,#navigation')||$('#inspector').open||$('#command').open||(state.route==='cockpit'&&state.cockpitPaused);
// Every page comes from the registry (pages.js, pages.json), in its nav order. A page gated on the
// profile ('runs' and 'live') is shown only when the domain profile turns it on (alpaca/profile.py `web`
// pages) and serves its routes; without a profile the hub has no run or live-log page. The mission map is a view inside
// Roadmap (#roadmap?view=mission), so it has no page of its own. A project's own pages (profile `web`
// pages given as objects, the overview's profile.slot_pages) join the nav by their `nav` number.
const profileOn = id => (state.overview?.profile?.pages||[]).includes(id);
const slotPages=()=>state.overview?.profile?.slot_pages||[];
const pageOf=id=>PAGE[id]||slotPages().find(p=>p.id===id);
function visiblePages(){return [...PAGES.filter(p=>p.gate!=='profile'||profileOn(p.id)),...slotPages()].sort((a,b)=>a.nav-b.nav);}
const state = {overview:null, runs:null, sessions:null, documents:null, route:'cockpit', params:new URLSearchParams(), ticket:0, log:null, timer:null, history:null, detail:null, mounts:0, mountY:0};
// One dirty flag per client cache (liveflags.js FLAGS) with a generation each: a fetch clears its flag
// only when no newer revision marked it while the fetch was in flight.
const flags=createDirty();
const liveParams=()=>({sid:state.params.get('sid'),view:state.params.get('view')||'analysis'});
const esc = value => String(value??'').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const count = value => Number(value||0).toLocaleString();
const date = value => {if(!value)return 'Not recorded'; const d=new Date(typeof value==='number'?value*1000:value);return Number.isNaN(+d)?String(value):d.toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});};
const relative = value => {const d=new Date(typeof value==='number'?value*1000:value);const m=Math.floor((Date.now()-d)/60000);return !value||Number.isNaN(m)?'Not recorded':m<1?'Just now':m<60?`${m}m ago`:m<1440?`${Math.floor(m/60)}h ago`:date(value);};
const duration = value => value==null?'Not recorded':value<60?`${Math.round(value)}s`:value<3600?`${Math.round(value/60)}m`:`${Math.floor(value/3600)}h ${Math.round(value%3600/60)}m`;
const short = (value,n=110) => String(value??'').length>n?String(value).slice(0,n)+'...':String(value??'');
const human = value => STATE_LABELS[String(value).toLowerCase()]||String(value||'unknown').replace(/[_-]/g,' ').replace(/\b\w/g,c=>c.toUpperCase());
const badge = value => `<span class="badge ${esc(['pass','done','complete','blocked','stale','open','fail','error','running','active','doing','in_progress'].includes(String(value).toLowerCase())?String(value).toLowerCase():'neutral')}">${['running','active','doing','in_progress'].includes(String(value).toLowerCase())?orb('working',20):'<i class="badge-dot"></i>'}${esc(human(value))}</span>`;
const stageName = value=>(state.overview?.profile?.stage_names||{})[value]||human(value);
const href=(page,params={})=>'#'+page+(Object.keys(params).length?'?'+new URLSearchParams(params):'');
const link=(page,label,params={},cls='text-link')=>`<a class="${cls}" href="${esc(href(page,params))}">${esc(label)}${icon('arrow')}</a>`;
const empty=(title,body,symbol='file')=>`<div class="empty">${orb('breathing',32)}<h3>${esc(title)}</h3><p>${esc(body)}</p></div>`;
const heading=(title,subtitle,action='',eyebrow='WORKSPACE')=>`<div class="page-heading"><div><span class="eyebrow">${esc(eyebrow)}</span><h1>${esc(title)}</h1><p>${esc(subtitle)}</p></div>${action}</div>`;
const panel=(title,body,action='',subtitle='')=>`<section class="panel"><div class="panel-head"><div><h2>${esc(title)}</h2>${subtitle?`<p>${esc(subtitle)}</p>`:''}</div>${action}</div>${body}</section>`;
const metric=(label,value,note,symbol)=>`<div class="metric"><div class="metric-label">${esc(label)}${icon(symbol)}</div><div class="metric-value">${value}</div><div class="metric-note">${note}</div></div>`;
const search=(placeholder,value='',name='q')=>`<label class="filter-input">${icon('search')}<input data-filter="${name}" type="search" placeholder="${esc(placeholder)}" value="${esc(value)}" aria-label="${esc(placeholder)}"></label>`;
// A day picker in a toolbar: native date input, never past today.
const dayInput=(name,label,value,max)=>`<label class="filter-date"><span>${esc(label)}</span><input data-filter="${name}" type="date" value="${esc(value||'')}" max="${esc(max)}" aria-label="${esc(label)} date"></label>`;
const select=(name,choices,current,label)=>`<select class="filter-select" data-filter="${name}" aria-label="${esc(label)}">${choices.map(([v,t])=>`<option value="${esc(v)}" ${v===current?'selected':''}>${esc(t)}</option>`).join('')}</select>`;
const errorView=err=>`<div class="error-box"><h2>This view could not be loaded</h2><p>${esc(err.message||err)}</p><button class="button" data-action="retry">Try again</button></div>`;
// The sign-in and workspace takeovers park the first render's reads in the Cache API (vault.js,
// prefetchWorkspace). The first route takes each parked response once; the cache is then dropped.
const HANDOFF='alpaca-handoff',HANDOFF_MAX_AGE_MS=60000;
let handoff=(()=>{try{return window.caches?caches.open(HANDOFF).catch(()=>null):null;}catch{return null;}})();
async function fromHandoff(url){if(!handoff)return null;try{const cache=await handoff;const r=cache&&await cache.match(url);if(!r)return null;await cache.delete(url);if(Date.now()-Number(r.headers.get('X-Handoff-At')||0)>HANDOFF_MAX_AGE_MS)return null;return await r.json();}catch{return null;}}
function dropHandoff(){if(!handoff)return;handoff=null;try{caches.delete(HANDOFF).catch(()=>{});}catch{}}
const apiReads=new Map();
async function api(path, params={}){const qs=new URLSearchParams(Object.entries(params).filter(([,v])=>v!==null&&v!==undefined&&v!==''));const url=path+(qs.size?'?'+qs:'');const shared=path==='/hub/research.json'||path==='/hub/mission.json'||path==='/hub/burn.json';if(shared&&apiReads.has(url))return apiReads.get(url);const read=(async()=>{const parked=await fromHandoff(url);if(parked)return parked;const r=await fetch(url,{credentials:'same-origin'});if(!r.ok){let reason;try{reason=(await r.json()).error;}catch{}const error=Error(reason||`The server returned ${r.status}. ${r.status===401?'Sign in and refresh this page.':'Please try again.'}`);error.status=r.status;throw error;}const value=await r.json();if(r.headers.get('X-Alpaca-Analytics-State')==='refreshing')value.refreshing=true;const tag=r.headers.get('ETag');if(tag&&value&&typeof value==='object')Object.defineProperty(value,'etag',{value:tag});return value;})().finally(()=>{if(shared)apiReads.delete(url);});if(shared)apiReads.set(url,read);return read;}
let overviewRead=null;
async function loadOverview(force=false){if(overviewRead)return overviewRead;if(!state.overview||force||flags.dirty.overview){const seen=flags.seen('overview');overviewRead=(async()=>{try{state.overview=await api('/hub/overview.json');applyTaskText(state.overview);flags.settle('overview',seen);for(const p of slotPages())ROUTE_NEEDS[p.id]=(p.needs||[]).filter(f=>FLAGS.includes(f));setStageNames(state.overview?.profile?.stage_names||{});return state.overview;}finally{overviewRead=null;}})();return overviewRead;}return state.overview;}
// t-591: /hub/overview.json carries each task without its text (report, history, contract, description, purpose,
// last_completion, reason) but with a text_rev, a hash of that text, and with the text itself for the tasks the
// cockpit and overview pages draw. A view that shows more awaits needTaskText(ids) (no ids: every task). The text
// is kept by (id, text_rev), so a change to it, whatever caused it, reads again from /hub/tasks.json.
const TASK_TEXT=['report','history','contract','description','purpose','last_completion','reason'],taskText=new Map(),textReads=new Map();
let textSeq=0;
const pickText=t=>Object.fromEntries(TASK_TEXT.map(k=>[k,t[k]]));
// A task shows the newest text cached for its id, whatever its rev: when the rev moved it keeps the previous text
// until the read for the new rev lands, never an empty card.
const putText=t=>{const c=taskText.get(t.id);if(c)Object.assign(t,c.text);};
// text_rev stays off the task's own keys, so the work page search (JSON.stringify(t)) cannot match a hash
function applyTaskText(o){for(const t of o.tasks||[]){const rev=t.text_rev;Object.defineProperty(t,'text_rev',{value:rev,enumerable:false,configurable:true});if(t.text){taskText.set(t.id,{rev,seq:++textSeq,text:t.text});delete t.text;}putText(t);}}
// Reads are shared per (id, text_rev): a read for an older rev is not reused for a newer one. A response is kept
// only when no later read stored a newer one for that id. A rev that moved while reading gets one more read.
async function needTaskText(ids){
 for(let pass=0;pass<2;pass++){
  const o=state.overview;if(!o)return;
  const want=ids?o.tasks.filter(t=>ids.includes(t.id)):o.tasks,missing=want.filter(t=>taskText.get(t.id)?.rev!==t.text_rev);
  if(!missing.length)return;
  const key=t=>t.id+'|'+t.text_rev,ask=missing.filter(t=>!textReads.has(key(t)));
  if(ask.length){
   const seq=++textSeq,read=api('/hub/tasks.json',{ids:ask.length>60?'':ask.map(t=>t.id).join(',')}).then(d=>{for(const f of d.tasks)if((taskText.get(f.id)?.seq||0)<seq)taskText.set(f.id,{rev:f.text_rev,seq,text:pickText(f)});}).finally(()=>{for(const t of ask)textReads.delete(key(t));});
   for(const t of ask)textReads.set(key(t),read);
  }
  await Promise.all([...new Set(missing.map(t=>textReads.get(key(t))))]);
  for(const t of state.overview.tasks)putText(t);
 }
}
// A list view draws without the text when its read fails, and reads again on its next render.
async function tryTaskText(ids){try{await needTaskText(ids);}catch{}}
async function loadMessages(){state.messages=(await api('/hub/messages.json')).messages;return state.messages;}
async function loadRuns(force=false){if(!state.runs||force||flags.dirty.runs){const seen=flags.seen('runs');state.runs=await api('/hub/runs.json');flags.settle('runs',seen);}return state.runs;}
// The cockpit's Parallel sessions panel reads only a title and the latest ask of the recent sessions, so it takes
// this small view instead of the whole /data.json (t-595). It settles the sessions flag as loadSessions does and
// drops the Sessions page's copy when the flag was set, so that page still reads /data.json fresh.
async function loadSessionAsks(){const seen=flags.seen('sessions'),stale=flags.dirty.sessions;const value=await api('/hub/session-asks.json');if(stale)state.sessions=null;flags.settle('sessions',seen);return value;}
async function loadSessions(force=false){if(!state.sessions||force||flags.dirty.sessions){const seen=flags.seen('sessions');state.sessions=await api('/data.json');flags.settle('sessions',seen);}return state.sessions;}
// A full mount. `view` names what is on screen (viewTag), so a live refresh of that same view can patch it in place.
function mount(html,view=''){state.mounts++;state.mountY=window.scrollY;state.shown=view;rememberMission($('#main'));rememberRoadmap($('#main'));renderHtml($('#main'),html);hydrate($('#main'));bindRoadmap($('#main'));bindMission($('#main'));}
const viewTag=()=>state.route+'?'+state.params;
// A live refresh patches the view where it stands when the reader is still on it (no other page mounted since).
const onScreen=automatic=>automatic&&state.shown===viewTag();
function toast(message){const e=$('#toast');e.textContent=message;e.style.display='block';setTimeout(()=>e.style.display='none',3500);}
function updateChrome(){const o=state.overview;updatePulse(o);updateArthur(o);if(o?.project?.name)$('#crumb-project').textContent=o.project.name;const v=o?.alpaca?.version;if(v){$('#alpaca-version').textContent='Alpaca '+v;$('#alpaca-version').title=o.alpaca.channel==='dev'?'Development build'+(o.alpaca.commit?' at commit '+o.alpaca.commit:''):'Release build';}const nav=visiblePages().map(({id,label,icon:symbol,divider})=>`${divider?'<div class="nav-divider"></div>':''}<a class="nav-link ${id===state.route?'active':''}" href="#${id}" ${id===state.route?'aria-current="page"':''}>${icon(symbol)}<span>${esc(label)}</span>${id==='inflight'&&o&&inflightCount(o.tasks)?`<span class="nav-count nav-inflight" title="Tasks in flight">${orb('working',20,'Tasks in flight')}${inflightCount(o.tasks)}</span>`:''}${id==='live'&&o?.active_jobs?.length?`<span class="nav-count nav-live">${o.active_jobs.length}</span>`:''}${id==='work'&&o?`<span class="nav-count">${o.tasks.filter(t=>!['done','cancelled','canceled'].includes(t.status)).length}</span>`:''}</a>`).join('');if(nav!==state.nav){state.nav=nav;$('#navigation').innerHTML=nav;}$('#crumb').textContent=visiblePages().find(p=>p.id===state.route)?.label||'Overview';}
function verifiedNote(o){const blocked=(o.items||[]).filter(x=>x.status!=='PASS');if(!blocked.length)return '';return `<div class="notice">${icon('alert')}<div><h3>${blocked.length} acceptance outcomes need current evidence</h3><p>Recorded completions are preserved. Current checks identify expired, missing, or changed evidence before work can resume.</p></div>${link('work','Review evidence',{tab:'outcomes'})}</div>`;}
function eventRows(items,full=false){return items.map(e=>`<div class="activity-item"${full?` id="event-${esc(e.id)}"`:''}><span class="event-symbol">${icon(/msg/.test(e.kind)?'messages':/task|check|proof/.test(e.kind)?'work':/run|receipt|job/.test(e.kind)?'runs':'activity')}</span><div class="event-text"><h3>${esc(e.kind==='run'&&e.ref?stageName(e.ref)+': ':'')}${esc(eventSummary(e))}${repeatNote(e)}</h3><div class="event-meta"><span>${esc(human(e.kind))}</span>${e.ref?`<a href="${esc(href('work',{q:e.ref}))}">${esc(e.ref)}</a>`:''}${e.session?`<a href="${esc(href('sessions',{sid:e.session}))}">${esc(short(e.actor||e.session,26))}</a>`:''}</div>${full?`<details><summary>View recorded context</summary><pre class="raw-data">${esc(JSON.stringify(e.data,null,2))}</pre></details>`:''}</div>${full?`<span class="event-time"><time datetime="${esc(e.ts)}">${esc(clock(e.ts))}</time><small>${esc(relative(e.ts))}</small></span>`:`<span class="event-time">${esc(relative(e.ts))}</span>`}</div>`).join('');}
function docMini(doc){return `<a class="doc-mini" href="${esc(href('library',{path:doc.path}))}"><span class="doc-icon">${icon('file')}</span><span><strong>${esc(short(doc.title,90))}</strong><small>${esc(human(doc.category))} &middot; ${esc(date(doc.mtime))}${doc.sealed?' &middot; Sealed proof':''}</small></span></a>`;}
// Cockpit cards a domain profile supplies (alpaca/profile.py `web` cards), loaded once per module.
// A card module exports render(o, runs) and may export bind(); a broken card is left out.
const cardModules=new Map();
async function loadCards(o){const out=[];for(const c of o?.profile?.cards||[]){try{if(!cardModules.has(c.module))cardModules.set(c.module,await import(c.module));const m=cardModules.get(c.module);if(typeof m.render==='function')out.push({id:c.id,render:m.render,bind:m.bind});}catch{}}return out;}
// A project's own page (docs/studio.md 4.0): its module, one of the profile's assets, exports render(ctx)
// (markup, or a promise of it) and may export bind(ctx), called once the markup is in #main, and stop(),
// called when the reader leaves. ctx (v 1) is the whole contract, so a page never imports the hub's modules;
// it reads its own profile routes through ctx.api. A missing or broken module shows the error view.
const slotModules=new Map();let slotMounted=null;
function stopSlot(){const m=slotMounted;slotMounted=null;try{m?.stop?.();}catch{}}
const slotContext=page=>Object.freeze({v:1,page:{id:page.id,label:page.label},params:new URLSearchParams(state.params),overview:state.overview,api,esc,icon,heading,panel,href,orb,loader,patch:(html,el=$('#main'))=>{const changed=patchHtml(el,html);hydrate(el);return changed;}});
async function slotView(ticket){const page=pageOf(state.route);if(!slotModules.has(page.module))slotModules.set(page.module,import(page.module).catch(e=>{slotModules.delete(page.module);throw e;}));const m=await slotModules.get(page.module);if(ticket!==state.ticket)return;if(typeof m.render!=='function')throw Error(`The page ${page.label} has no render function.`);const ctx=slotContext(page),html=await m.render(ctx);if(ticket!==state.ticket)return;mount(String(html??''));slotMounted=m;if(typeof m.bind==='function')m.bind(ctx);}
// In-flight: every task in progress with a live lease, one card each. A live refresh patches the
// cards in place, so the page never jumps; #inflight?task=<id> scrolls to that task's card.
async function inflightView(ticket,automatic=false){
 const data=await api('/hub/inflight.json');if(ticket!==state.ticket)return;state.inflight=data;
 const focus=state.params.get('task'),board=renderInflight(data,{focus,href});
 if(automatic&&$('#inflight-board')){patchHtml($('#inflight-board'),board);return;}
 mount(heading('In flight','What each task in progress is doing right now: its activity, plan, progress and signals, read from the record and the claiming session.',link('roadmap','Open the roadmap',{},'button'),'LIVE WORK')+`<div id="inflight-board">${board}</div>`);
 // route() scrolls to the top after a view renders; the card is brought into view after that and again
 // once the layout has settled (fonts, a narrow screen reflowing the cards).
 if(focus&&!automatic){const show=()=>{const card=document.getElementById('if-'+focus);if(card&&ticket===state.ticket)card.scrollIntoView({block:'start'});};requestAnimationFrame(show);setTimeout(show,400);}
}
// Records: operations, decisions and sealed proofs from the record, and the wiki's
// status and cited briefings (#records?section=wiki&op=op-12), all read-only.
async function recordsView(ticket,automatic=false){
 const p=state.params,section=['ops','decisions','proofs','wiki'].includes(p.get('section'))?p.get('section'):'ops';
 const vault=section==='wiki'&&p.get('vault')==='open';
 if(automatic&&vault&&$('#evidence-graph'))return false; // an open vault keeps its selection and zoom
 if(automatic&&section==='wiki'&&!vault&&$('.rec-seal.is-sealed'))return false; // the seal in place keeps playing (t-602)
 const subject=(p.get('subject')||p.get('op')||p.get('task')||'').trim();
 const soft=promise=>promise.catch(error=>({error:error.message||String(error)}));
 const [data,wiki,brief,graph]=await Promise.all([api('/hub/records.json',{section:section==='wiki'?'ops':section,q:p.get('q'),limit:200}),
  section==='wiki'&&!vault?soft(api('/hub/wiki-status.json')):null,
  section==='wiki'&&!vault&&subject?soft(api('/hub/wiki-briefing.json',/^t-/.test(subject)?{task:subject}:{op:subject})):null,
  vault?soft(api('/hub/graph.json')):null]);
 if(ticket!==state.ticket)return;if(automatic&&readerEngaged())return false;
 mount(heading('Records','The official record of this project: operations, decisions and sealed proofs, and what the wiki knows. Read-only.','','RECORD')+
  renderRecords({section,data,wiki,brief,subject,vault,graph,q:p.get('q'),href,date,badge,empty,search}));
 mountSeal($('.rec-seal'),data,wiki);
 const host=$('#evidence-graph');
 if(host&&graph&&!graph.error)mountGraph(host,graph,{href,api,item:p.get('item'),find:p.get('find'),
  onState:({item})=>{const q=new URLSearchParams(state.params);if(item)q.set('item',item);else q.delete('item');state.params=q;history.replaceState(null,'',href('records',Object.fromEntries(q)));}});
}
// Worktree statuses (t-592): /hub/vcs-status.json is polled every 5 s while the page is visible and its rows are
// patched into the Uncommitted cells, because the snapshot behind vcs.json is only rebuilt about once a minute.
// The newest rows are kept so the timed re-render of the whole board does not bring an older count back.
// The poll answers for the live worktrees only (main, most recent, largest, and those loaded on demand), plus the
// overall line over every worktree; a row that leaves the live set keeps its last count and its age.
function overlayVcsStatus(data){
 const got=state.vcsStatus;if(!got||!data.worktrees)return;
 data.worktrees=data.worktrees.map(w=>{const row=got.worktrees[w.path];return row&&(row.checked_at||0)>=(w.checked_at||0)?{...w,...row}:w;});
 if(got.overall&&got.generated>(data.served_at||0))data.overall=got.overall;
 if(got.generated>(data.served_at||0))data.served_at=got.generated;
 data.hot_backlog=got.hot_backlog;
}
// The poll gives up on a fetch after 8 s and waits 5, 10, 20, then 40 s after errors; one success returns it to 5 s.
// Write each cell from the newest rows: an older read never replaces a newer one, and an unchanged row still gets its
// "checked N s ago" label recomputed (only that text node changes), so the label stays true between changes.
function paintVcsStatus(board,got,now){
 for(const cell of board.querySelectorAll('[data-wt-status]')){
  const row=got.worktrees[cell.dataset.wtStatus];if(!row)continue;
  const seen=Number((cell.dataset.checked||'0|').split('|')[0]);
  if((row.checked_at||0)<seen)continue;
  forgetHtml(cell);patchHtml(cell,statusCell(row,now));cell.dataset.checked=(row.checked_at||0)+'|'+(row.status_error||'');
 }
}
// The label also moves on a 5 s client tick, so a poll that backs off or fails does not leave it frozen.
setInterval(()=>{const board=$('#vcs-board');if(state.route==='vcs'&&board&&state.vcsStatus&&document.visibilityState==='visible')paintVcsStatus(board,state.vcsStatus,state.vcsStatus.generated+(Date.now()-state.vcsStatusAt)/1000);},5000);
let vcsStatusWait=5000;
async function vcsStatusTick(){
 const board=$('#vcs-board');
 if(state.route!=='vcs'||!board||document.visibilityState!=='visible')return setTimeout(vcsStatusTick,vcsStatusWait);
 const ticket=state.ticket,stop=new AbortController(),timer=setTimeout(()=>stop.abort(),8000);
 try{
  const r=await fetch('/hub/vcs-status.json',{credentials:'same-origin',signal:stop.signal});
  if(!r.ok)throw Error(String(r.status));
  const got=await r.json();
  vcsStatusWait=5000;
  if(ticket!==state.ticket||got.error)return;
  got.worktrees={...(state.vcsStatus?.worktrees||{}),...got.worktrees};
  state.vcsStatus=got;
  state.vcsStatusAt=Date.now();paintVcsStatus(board,got,got.generated);
  const note=board.querySelector('[data-wt-backlog]');if(note)patchHtml(note,backlogNote(got.hot_backlog));
  const line=board.querySelector('[data-wt-overall]');if(line&&got.overall)patchHtml(line,overallNote(got.overall));
 }catch{vcsStatusWait=Math.min(vcsStatusWait*2,40000);}finally{clearTimeout(timer);setTimeout(vcsStatusTick,vcsStatusWait);}
}
setTimeout(vcsStatusTick,5000);
// "Load status" on a worktree outside the live set: read now by /hub/vcs-tree.json, then kept with the polled rows
// so a re-render shows it; the server keeps it live for 2 minutes, and the poll carries it while it does.
async function loadVcsTree(path){
 const r=await fetch('/hub/vcs-tree.json?path='+encodeURIComponent(path),{credentials:'same-origin'});
 const row=await r.json();if(!r.ok||row.error)throw Error(row.error||String(r.status));
 if(!state.vcsStatus){state.vcsStatus={generated:row.generated,worktrees:{},hot_backlog:0};state.vcsStatusAt=Date.now();}
 state.vcsStatus.worktrees={...state.vcsStatus.worktrees,[path]:{uncommitted:row.uncommitted,oldest_change:row.oldest_change,checked_at:row.checked_at,status_error:row.status_error}};
 return row;
}
// The vault seal in place on the Wiki page (ventry.js, t-602): it waits for its dial, plays the release and
// then opens the vault; the record's and the wiki's counts are its telemetry. Reduced motion keeps the card.
function mountSeal(box,data,wiki){
 if(!box||matchMedia('(prefers-reduced-motion: reduce)').matches)return;
 const c=(data&&data.counts)||{},corpus=(wiki&&wiki.corpus)||{},serving=wiki&&wiki.available&&!wiki.error;
 const lines=[['OPERATIONS',String(c.ops??0)],['DECISIONS',String(c.decisions??0)],['SEALED PROOFS',String(c.proofs??0)],
  ['WIKI',serving?`${Number(corpus.documents||0).toLocaleString('en-US')} DOCS / ${Number(corpus.edges||0).toLocaleString('en-US')} LINKS`:'NOT SERVING',serving?'':'is-warn']];
 box.classList.add('is-sealed');
 import('./ventry.js').then(m=>m.playEntry(box,{lines},{inline:true,onOpen:()=>{location.hash=box.dataset.href.replace(/^[^#]*/,'');}})).catch(()=>box.classList.remove('is-sealed'));
}
// Version control: a read-only picture of the repository; #vcs?days=N picks the graph window.
async function vcsView(ticket,automatic=false){
 const days=[1,7,30,90].includes(Number(state.params.get('days')))?Number(state.params.get('days')):7;
 const data=await api('/hub/vcs.json',{days});if(ticket!==state.ticket)return;
 overlayVcsStatus(data);
 const board=$('#vcs-board'),view=automatic&&board?captureVcs(board):{};
 const body=renderVcs(data,{days,href,windowHref:k=>href('vcs',{days:k}),view});
 if(automatic&&board){patchHtml(board,body);bindVcs(board,data,view,{loadTree:loadVcsTree});return;}
 mount(heading('Version control','Explore branch history, inspect commits and review local work.','','REPOSITORY')+`<div id="vcs-board">${body}</div>`);
 bindVcs($('#vcs-board'),data,view,{loadTree:loadVcsTree});
}
function cockpitExtras(){return state.cockpitExtras||=(Object.fromEntries(['mission','research','board','sessions'].map(key=>[key,{value:null,loading:true,error:false,pending:null}])));}
function paintCockpitExtras(ticket){
 if(ticket!==state.ticket||state.route!=='cockpit'||state.cockpitPaused)return;
 const y=window.scrollY;
 const parts=renderCockpitExtras(state.overview,cockpitExtras());
 for(const [key,html] of Object.entries(parts)){const slot=$(`[data-cockpit-extra="${key}"]`);if(!slot)continue;rememberMission(slot);patchHtml(slot,html);hydrate(slot);bindMission(slot);}
 window.scrollTo(0,y);
}
// only: the panels to read again (all of them by default). A panel that already shows a value keeps it
// while the read is in flight, so a quiet refresh never flashes "Refreshing".
function refreshCockpitExtras(ticket,only=null){
 const extras=cockpitExtras(),readers={mission:()=>api('/hub/mission.json',{view:'compact'}),research:()=>api('/hub/research.json',{view:'compact'}),board:()=>api('/hub/live-work.json'),sessions:()=>loadSessionAsks()};
 for(const [key,read] of Object.entries(readers)){
  if(only&&!only.includes(key))continue;
  const extra=extras[key];
  if(!extra.pending){extra.loading=extra.value===null;extra.error=false;extra.pending=Promise.resolve().then(read).then(value=>{extra.value=value;extra.loading=false;},()=>{extra.error=true;extra.loading=false;}).finally(()=>{extra.pending=null;});}
  extra.pending.then(()=>paintCockpitExtras(ticket));
 }
}
// The four panels that load beside the cockpit, as the guard below sees them: always still loading, so the
// guard compares the record's own cards and never the panels' separate data.
const EXTRAS_AT_REST=Object.fromEntries(['mission','research','board','sessions'].map(key=>[key,{value:null,loading:true,error:false,pending:null}]));
async function cockpit(ticket,automatic=false){
  const focusedAction=document.activeElement?.dataset?.action;
  // Supplemental panels never delay the core work record or navigation.
  const [o,r,h]=await Promise.all([loadOverview(),loadRuns(),api('/hub/history.json',{limit:6,kind:'progress'})]);
  if(ticket!==state.ticket)return;if(automatic&&readerEngaged())return false;
  await tryTaskText(cockpitTaskIds(o));if(ticket!==state.ticket)return;
  const cards=await loadCards(o);if(ticket!==state.ticket)return;
  const options={paused:Boolean(state.cockpitPaused),cards,presenting:document.body.classList.contains('presentation')};
  // A live refresh changes the page only when the record behind it changed (the host panel updates itself,
  // the extra panels read their own data, and "Nm ago" labels tick every minute), and at least every
  // 60 s (a patch, with the extra panels read again) so those labels and the sessions panel stay current. What is compared is the cockpit drawn from the record with
  // the extra panels at rest; a changed record is patched into the page in place.
  const same=renderCockpit(o,r,h,{...options,extras:EXTRAS_AT_REST}).replace(/<section[^>]*id="sys-panel"[\s\S]*?<\/section>/,'').replace(/\b\d+[smhd] ago\b|just now/g,'');
  const patching=onScreen(automatic);
  if(patching&&same===state.cockpitSame&&Date.now()-(state.cockpitAt||0)<60000){
    // Nothing moved in the record: only the live sessions panel reads again, when the session data moved.
    if(flags.dirty.sessions)refreshCockpitExtras(ticket,['board','sessions']);
    return;
  }
  state.cockpitSame=same;state.cockpitAt=Date.now();
  const html=renderCockpit(o,r,h,{...options,extras:cockpitExtras()});
  if(patching)patchHtml($('#main'),html);else mount(html,viewTag());
  refreshCockpitExtras(ticket);
  if(focusedAction?.startsWith('cockpit-'))$(`[data-action="${CSS.escape(focusedAction)}"]`)?.focus();
  const button=$('[data-action="cockpit-present"]');if(button){button.setAttribute('aria-pressed',String(document.body.classList.contains('presentation')));button.querySelector('span').textContent=document.body.classList.contains('presentation')?'Exit focus view':'Focus view';}
}
// Roadmap owns the plan: the pinned operation maps, then every workstream with its prerequisites.
// Cockpit and Work & evidence link here instead of repeating it.
// Roadmap has three views, switched by the tabs at its top: the task roadmap, the mission map
// (?view=mission, with its node, group and at) and the finished operations (?view=finished). Live refresh and the selection keep working as they
// did on the old Mission map page, because the mission view still renders through missionView.
const roadmapTabs=(view,finished=0)=>`<nav class="tabs roadmap-tabs" aria-label="Roadmap views">${[['tasks','Task roadmap',{}],['mission','Mission map',{view:'mission'}],['finished',`Finished operations${finished?' <span class="tab-count">'+count(finished)+'</span>':''}`,{view:'finished'}]].map(([id,label,params])=>`<a class="tab ${id===view?'active':''}" href="${esc(href('roadmap',params))}"${id===view?' aria-current="page"':''}>${label}</a>`).join('')}</nav>`;
// The finished operations: closed ops, newest close first, each with its close basis, task
// counts and how long it ran; ?op=<id> opens one on its own task roadmap with the done work shown.
// Read from the overview's ops (hub.overview: opened, closed, close_basis), nothing else is fetched.
const closedOps=o=>(o.ops||[]).filter(x=>x.status==='closed').sort((a,b)=>(Date.parse(b.closed)||0)-(Date.parse(a.closed)||0)||String(b.id).localeCompare(String(a.id)));
const opName=x=>x.short_title||String(x.title||'').split(':')[0].trim()||x.id;
const ranFor=(a,b)=>{const d=(Date.parse(b)-Date.parse(a))/864e5;if(!Number.isFinite(d)||d<0)return '';const n=Math.round(d);return d<1?'ran under a day':`ran ${n} day${n===1?'':'s'}`;};
const finishedRow=x=>`<article class="fin-op"><div class="fin-head"><a class="fin-title" href="${esc(href('roadmap',{view:'finished',op:x.id}))}"><span class="mono">${esc(x.id)}</span><span>${esc(opName(x))}</span></a><time class="fin-when" datetime="${esc(x.closed||'')}">${esc(x.closed?'Closed '+date(x.closed):'Close time not recorded')}</time></div><p class="fin-meta">${count(x.tasks_done)} of ${count(x.tasks_total)} tasks done${ranFor(x.opened,x.closed)?' / '+ranFor(x.opened,x.closed):''}</p>${x.close_basis?`<details class="fin-basis"><summary>Why it closed</summary><p>${esc(x.close_basis)}</p></details>`:''}</article>`;
async function finishedView(ticket,automatic=false){
 const o=await loadOverview();if(ticket!==state.ticket)return;if(automatic&&readerEngaged())return false;
 const ops=closedOps(o),one=ops.find(x=>x.id===state.params.get('op'));
 if(one){
  const planned=(o.roadmap?.operations||[]).some(p=>p.op===one.id);
  mount(roadmapTabs('finished',ops.length)+heading(opName(one),one.title||one.id,link('roadmap','All finished operations',{view:'finished'},'button'),one.id.toUpperCase()+' / FINISHED')+
   `<div class="fin-summary">${finishedRow(one).replace('<article class="fin-op">','<article class="fin-op fin-op-open">')}${one.done_when?`<p class="fin-done-when"><span class="eyebrow">DONE WHEN</span>${esc(one.done_when)}</p>`:''}</div>`+
   (planned?renderRoadmap(o,{op:one.id,selector:false,showAll:true}):'<p class="route-hint">This operation recorded no tasks.</p>'));
  return;
 }
 mount(roadmapTabs('finished',ops.length)+heading('Finished operations','Closed operations, newest first, with the reason each one closed.',link('roadmap','Back to the task roadmap',{},'button'),'PLAN')+
  (ops.length?`<div class="fin-list">${ops.map(finishedRow).join('')}</div>`:empty('No finished operations yet','An operation shows here once it closes: bin/alpaca op close OP --basis "why".')));
}
async function roadmapView(ticket,automatic=false){
 if(state.params.get('view')==='mission')return missionView(ticket,automatic);
 if(state.params.get('view')==='finished')return finishedView(ticket,automatic);
 const o=await loadOverview();if(ticket!==state.ticket)return;if(automatic&&readerEngaged())return false;
 const pinned=(o.ops||[]).filter(x=>x.pinned&&x.status!=='closed');
 const html=roadmapTabs('tasks',closedOps(o).length)+heading('Roadmap','Operations by phase, workstreams, and the prerequisites between tasks.',link('work','Open checklist',{},'button'),'PLAN')+
  (pinned.length?pinned.map(x=>renderPhaseMap(o,x.id)).join(''):'<p class="route-hint">Pin an operation with <code>bin/alpaca op pin OP</code> to show its phases here.</p>')+
  renderRoadmap(o);
 // A live refresh patches the plan where it stands: identical markup writes nothing, and the arrows are
 // routed again only when the cards moved.
 if(onScreen(automatic)){if(patchHtml($('#main'),html))patchedRoadmap($('#main'));return;}
 mount(html,viewTag());
}
// The research page (operator-surface.md section 7): the run list, or one run with ?run=<id>.
// It re-reads /hub/research.json on every render, so the 15 s refresh and a pushed board
// revision both redraw it from the record.
async function researchView(ticket,automatic=false){
 const run=state.params.get('run');
 const data=await api('/hub/research.json',run?{run}:{});if(ticket!==state.ticket)return;if(automatic&&readerEngaged())return false;
 mount(renderResearchPage(data,state.params));bindResearch($('#main'));
}
async function missionView(ticket,automatic=false){
 const data=await api('/hub/mission.json',{at:state.params.get('at')});if(ticket!==state.ticket)return;if(automatic&&readerEngaged())return false;
 const group=state.params.get('group');const node=state.missionEntry===location.hash?null:state.params.get('node')||(group?data.nodes.find(n=>n.group===group)?.id:null);state.missionEntry=location.hash;
 const root=$('#main [data-mm-root]');
 if(onScreen(automatic)&&root&&!missionMoved($('#main'),data))return;
 const html=roadmapTabs('mission')+renderMission(data,{node});
 if(onScreen(automatic)&&root){patchHtml($('#main'),html);patchedMission($('#main'));return;}
 mount(html,viewTag());
}
async function overview(ticket,automatic=false){const [o,h,docs,r]=await Promise.all([loadOverview(),api('/hub/history.json',{limit:5}),api('/hub/documents.json',{limit:4}),loadRuns()]);if(ticket!==state.ticket)return;if(automatic&&readerEngaged())return false;const active=o.tasks.filter(t=>!['done','cancelled','canceled'].includes(t.status));const done=o.tasks.filter(t=>t.status==='done');const lastRun=r.items?.[0];await tryTaskText(active.filter(t=>t.status==='doing'||t.claimant).slice(0,2).map(t=>t.id));if(ticket!==state.ticket)return;
mount(heading('A clear view of the work.','Follow progress, understand decisions, and trace every result to its evidence.',link('activity','Explore activity',{},'button'),'OPERATIONS OVERVIEW')+
profileProblems(o)+verifiedNote(o)+`<div class="metrics">${metric('Work in progress',count(active.length),`${count(o.tasks.length)} tasks across ${count(o.ops.length)} operations`,'work')}${o.items.length?metric('Current acceptance',`${count(o.counts.items_pass)}<small>/ ${count(o.items.length)}</small>`,'Passing with current evidence','check'):''}${metric('Completed tasks',count(done.length),'Recorded work and linked proof','file')}${metric('Open operations',count(o.ops.filter(op=>op.status!=='closed').length),`${count(o.ops.length)} recorded in total`,'roadmap')}${metric('Completed this week',count(done.filter(t=>Date.now()-Date.parse(t.completed_at)<7*864e5).length),'Tasks recorded complete in the last 7 days','check')}${profileOn('runs')?metric('Active runs',count(o.active_jobs?.length),lastRun?`Last result: ${esc(stageName(lastRun.stage))} &middot; ${esc(human(lastRun.verdict))}`:'No run receipt recorded','runs'):''}</div>
<div class="duo">${panel('Work in focus',active.filter(t=>t.status==='doing'||t.claimant).slice(0,2).map(t=>`<div class="workstream"><span class="eyebrow">CURRENT ASSIGNMENT / ${esc(t.id)}</span><button class="workstream-title" style="text-align:left;padding:8px 0 0" data-task="${esc(t.id)}">${esc(t.title)}</button><small>${esc(t.reason||'Open the task for its scope, report, and evidence.')}</small></div>`).join('')+o.ops.filter(op=>op.status!=='closed').slice(-4).reverse().map(op=>{const pct=op.tasks_total?Math.round(op.tasks_done/op.tasks_total*100):0;return `<div class="workstream"><div class="workstream-top"><div><a class="workstream-title" href="${esc(href('work',{op:op.id}))}">${esc(op.title)}</a><small>${esc(op.id)} &middot; ${esc(short(op.done_when||'Open the work record for scope and next actions',130))}</small></div>${badge(op.status==='open'?'active':op.status)}</div><div class="progress-track"><span style="width:${pct}%"></span></div><div class="progress-caption"><span>${op.tasks_done} of ${op.tasks_total} tasks recorded complete</span><span>${pct}%</span></div></div>`;}).join('')||empty('No open operations','Work will appear here when an operation is recorded.','work'),link('work','All work'))}
${dayChart(done,{field:'completed_at',label:'Recorded completions',subtitle:'Task completions by day, across every operation'})}
</div>${(o.stages||[]).length?panel(`${o.profile?.name||'Stage'} readiness`,`<div class="stage-list">${(o.stages||[]).slice(0,11).map((s,i)=>`<a class="stage-row" href="${esc(href('work',{tab:'outcomes',stage:s.stage}))}"><span class="stage-symbol ${s.status==='PASS'?'pass':''}">${s.status==='PASS'?icon('check'):String(i+1).padStart(2,'0')}</span><span style="color:var(--text)">${esc(stageName(s.stage))}</span>${badge(s.status)}</a>`).join('')}</div><div class="panel-foot">Current evidence checked ${esc(date(o.stages?.[0]?.verified_at))}. ${profileOn('runs')?link('runs','Inspect run history'):''}</div>`,'','Current checks for the profile stages'):''}
<div class="trio">${panel('Recent work',`<div class="activity-list">${eventRows(h.items)}</div>`,link('activity','Full history'),'Meaningful changes from the shared record')}${panel('Capture status',`<div class="panel-body">${o.profile?.name?`<div class="link-row">${badge(o.capture?.profile?.status||'unknown')}<span class="muted">${esc(o.profile.name)} record capture</span></div>`:''}<p class="row-sub">Collector activity: ${esc(o.observability?.collector?.status||'unavailable')}; liveness: ${esc(o.observability?.collector?.liveness||'unknown')}. Capture: ${esc(o.observability?.status||'unavailable')}. Wiki: ${esc(o.observability?.consumer_health?.wiki?.status||'unavailable')}. Artifacts: ${esc(o.observability?.consumer_health?.artifacts?.status||'unavailable')}. Backup: ${esc(o.observability?.consumer_health?.backup?.status||'unavailable')}.</p><p class="row-sub">${esc(o.capture?.profile?.reason||'Session coverage is shown with each captured conversation.')}</p>${o.capture?.errors?.length?`<p class="row-sub">${o.capture.errors.length} recent recorded capture errors. ${link('activity','Inspect history',{kind:'all',q:'failed'})}</p>`:''}${(o.capture?.states||[]).map(s=>`<p class="row-sub">State, not an error: ${esc(s.summary||s.kind)} (${esc(s.recovery||'')})</p>`).join('')}</div>`)}${panel('Latest reports',docs.items.map(docMini).join('')||empty('No reports yet','Reports and sealed proofs will appear here.'),link('library','Library'))}
</div>`);}
function taskRows(tasks){return tasks.map(t=>`<tr><td><span class="mono">${String(t.number||Number(t.id.replace(/\D/g,''))).padStart(2,'0')}</span></td><td><button class="row-title" data-task="${esc(t.id)}">${esc(t.title)}</button><span class="row-sub"><span class="mono">${esc(t.id)}</span> / ${esc(t.op||'Unassigned operation')}</span></td><td>${badge(t.status)}</td><td><span class="row-sub">${esc(short(t.report?.summary||t.report?.result||t.report?.what||(t.status==='done'?'Work summary not recorded':'Work not yet recorded complete'),150))}</span></td><td><span class="row-sub">${esc(date(t.completed_at))}</span>${t.completed_session?link('sessions',short(t.completed_session,21),{sid:t.completed_session}):'<small class="muted">Session not recorded</small>'}</td><td>${t.proof?`<a href="${esc(href('library',{path:proofPath(t.proof)}))}">Proof ${icon('external')}</a>`:'<span class="muted">No proof linked</span>'}</td></tr>`).join('');}
function outcomeRows(items){return items.map(x=>`<tr><td><span class="task-number">${String(x.number||Number(x.id.replace(/\D/g,''))).padStart(2,'0')}</span></td><td><button class="row-title" data-outcome="${esc(x.id)}">${esc(x.title)}</button><span class="row-sub">${esc(x.id)} / ${esc(stageName(x.stage))}</span></td><td>${badge(x.status)}</td><td>${badge(x.recorded_status)}<span class="row-sub">${esc(date(x.recorded_completed_at))}</span>${x.recorded_completed_session?link('sessions',short(x.recorded_completed_session,20),{sid:x.recorded_completed_session}):''}</td><td><span class="row-sub">${esc(short(x.reason||'Open for method, result and proof',140))}</span></td></tr>`).join('');}
async function work(ticket){
 const o=await loadOverview();if(ticket!==state.ticket)return;const p=state.params,tab=p.get('tab')||'tasks',q=(p.get('q')||'').toLowerCase(),status=p.get('status')||'',op=p.get('op')||'',stage=p.get('stage')||'',layout=p.get('layout')||(tab==='tasks'?'checklist':'timeline');
 // the cards, timeline, table and the search read the task text; the checklist and roadmap layouts without a search do not
 if(tab==='tasks'&&(q||!['checklist','roadmap'].includes(layout))){await tryTaskText();if(ticket!==state.ticket)return;}
 const scope=(tab==='outcomes'?o.items:o.tasks).filter(t=>(!op||t.op===op)&&(!stage||t.stage===stage));
 const items=scope.filter(t=>(!q||JSON.stringify(t).toLowerCase().includes(q))&&(!status||String(t.status).toLowerCase()===status));
 const visuals=`<div class="page-visuals">${segmented(scope,'status',{label:tab==='outcomes'?'Current outcome checks':'Task completion',route:'work',params:{tab,...(op?{op}:{}),...(stage?{stage}:{})}})}${tab==='outcomes'?segmented(scope,'recorded_status',{label:'Last recorded outcome results'}):dayChart(scope.filter(t=>t.status==='done'),{field:'completed_at',label:'Recorded completions',subtitle:'Dated task completions in this operation scope'})}</div>`;
 const filters=`<div class="toolbar">${search('Search work, method, result or identifier...',p.get('q'))}${select('status',[['','All statuses'],...Array.from(new Set(scope.map(t=>String(t.status).toLowerCase()))).sort().map(s=>[s,human(s)])],status,'Filter status')}${tab==='tasks'&&['checklist','roadmap'].includes(layout)?'':select('op',[['','All operations'],...o.ops.map(x=>[x.id,`${x.id} / ${short(x.title,35)}`])],op,'Filter operation')}${stage?`<button class="button" data-clear="stage">${esc(stageName(stage))} ${icon('close')}</button>`:''}<div class="work-view-toggle">${(tab==='tasks'?[['checklist','Checklist'],['roadmap','Roadmap'],['timeline','History'],['cards','Cards'],['table','Table']]:[['timeline','Timeline'],['table','Table']]).map(([v,t])=>`<a class="${layout===v?'active':''}" href="${esc(href('work',{...Object.fromEntries(p),layout:v}))}">${t}</a>`).join('')}</div><span class="toolbar-end">${items.length} ${tab==='outcomes'?'outcomes':'tasks'}</span></div>`;
 const body=tab==='tasks'&&['checklist','roadmap'].includes(layout)?renderRoadmap(o,{op,all:!op,routeScope:true,mode:layout==='roadmap'?'map':'list',visibleIds:items.map(t=>t.id)}):!items.length?empty('No matching work','Change the search or filters to see other work items.','work'):layout==='timeline'?renderWorkTimeline(items,tab):tab==='tasks'&&layout==='cards'?renderWorkCards(items):`<div class="panel table-wrap"><table class="data-table"><thead><tr>${(tab==='outcomes'?['No.','Outcome','Current check','Recorded completion','What needs attention']:['No.','Work item','State','What was delivered','Completed / session','Evidence']).map(t=>`<th>${t}</th>`).join('')}</tr></thead><tbody>${tab==='outcomes'?outcomeRows(items):taskRows(items)}</tbody></table></div>`;
 mount(heading('Work & evidence','A numbered checklist of work, methods, results and proof.',`<div class="page-actions">${link('roadmap','Roadmap',{},'button')}${link('library','Read reports',{},'button')}</div>`)+`<div class="tabs"><a class="tab ${tab==='tasks'?'active':''}" href="#work?tab=tasks">Tasks <span class="muted">${o.tasks.length}</span></a>${o.items.length||tab==='outcomes'?`<a class="tab ${tab==='outcomes'?'active':''}" href="#work?tab=outcomes">Runbook outcomes <span class="muted">${o.items.length}</span></a>`:''}</div>`+visuals+filters+(tab==='outcomes'?'<p class="route-hint">Current evidence and historical completion are separate. Open a numbered outcome for its source, method, result and completing session.</p>':'')+body);
}
function proofPath(value){if(typeof value==='object')value=value.path||value.ref||'';return String(value||'').replace(/^local:/,'');}
function evidenceLinks(values){if(!values?.length)return '<p class="muted">No evidence reference is recorded.</p>';return values.map(v=>{const path=proofPath(v);const receipt=profileOn('runs')&&path.match(/\/(?:receipts|logs)\/([a-f0-9]{32})\.(?:json|log)$/);if(receipt)return `<a class="proof-link" href="${esc(href('runs',{receipt:receipt[1]}))}">${icon('runs')}<span>Open receipt ${esc(receipt[1].slice(0,12))} and captured output</span>${icon('arrow')}</a>`;const readable=/\.(md|html?|txt|log|json|csv)$/i.test(path);return readable?`<a class="proof-link" href="${esc(href('library',{path}))}">${icon('file')}<span>${esc(path)}</span>${icon('external')}</a>`:`<div class="proof-link">${icon('file')}<span>${esc(path)}</span></div>`;}).join('');}
function showInspector(title,html,label='WORK RECORD',reader=false){$('#inspector').classList.toggle('reader',reader);$('#inspector-label').textContent=label;$('#inspector-content').innerHTML=`<h2 id="inspector-title" class="detail-title">${esc(title)}</h2>${html}`;hydrate($('#inspector'));if(!$('#inspector').open)$('#inspector').showModal();}
function taskContract(t){const c=t.contract;const cmd=`bin/alpaca task contract ${t.id} --input "..." --expected "..." --done-bar "..." --fail-case "..."`;if(!c)return `<section class="detail-section"><h3>Contract</h3><p class="inline-warning">No input, expected output, done bar or fail case is recorded for this task yet.</p><code class="ct-cmd">${esc(cmd)}</code></section>`;const card=(n,title,list,cls='')=>`<section class="ct-card ${cls}"><h3><span class="ct-n">${n}</span>${title}</h3>${list.length?`<ul class="ct-list">${list.map(x=>`<li><b>&middot;</b><div>${esc(x)}</div></li>`).join('')}</ul>`:'<p class="ct-note">Not stated.</p>'}</section>`;const st=c.stage&&(state.overview.stages||[]).find(s=>s.stage===c.stage);return `<section class="detail-section"><h3>Contract</h3><div class="ct"><div class="ct-head"><strong>${esc(t.id)}</strong><span>recorded ${esc(date(c.at))} by ${esc(c.by||'unknown')}${c.source?' &middot; from '+esc(c.source):''}</span>${c.stage?`<span>stage <b>${esc(stageName(c.stage))}</b>${st?.receipt_id?(profileOn('runs')?` &middot; <a class="text-link" href="${esc(href('runs',{receipt:st.receipt_id}))}">latest run contract ${badge(st.status)}</a>`:` &middot; latest run ${badge(st.status)}`):' &middot; no run yet'}</span>`:''}</div><div class="ct-grid ct-grid-task">${card(1,'Input',c.input||[])}${card(2,'Expected output',c.expected||[])}${card(3,'Done bar',c.done_bar||[])}${card(4,'Fail cases',c.fail_cases||[])}</div></div></section>`;}
async function inspectTask(id){
 try{await needTaskText([id]);}catch(err){toast(err.message);return;}
 const base=state.overview.tasks.find(t=>t.id===id);if(!base)return;const t={...base,...taskText.get(id)?.text},op=state.overview.ops.find(x=>x.id===t.op),r=t.report;
 const paragraph=(title,body,cls='')=>`<section class="detail-section ${cls}"><h3>${title}</h3><div class="record-prose markdown">${markdown(body||'Not recorded.',r?.path||'')}</div></section>`;
 showInspector(`${String(t.number||Number(t.id.replace(/\D/g,''))).padStart(2,'0')}. ${t.title}`,`<dl class="detail-grid"><dt>Task</dt><dd class="mono">${esc(t.id)}</dd><dt>State</dt><dd>${badge(t.status)}</dd>${t.description?`<dt>Full statement</dt><dd>${esc(t.description)}</dd>`:''}<dt>Operation</dt><dd>${esc(op?.title||t.op)}</dd><dt>Created</dt><dd>${esc(date(t.created_at))}</dd><dt>Last updated</dt><dd>${esc(date(t.updated_at))}</dd><dt>Completed</dt><dd>${esc(date(t.completed_at))}</dd><dt>Completing session</dt><dd>${t.completed_session?link('sessions',t.completed_session,{sid:t.completed_session}):'Not recorded'}</dd><dt>Assigned worker</dt><dd>${esc(t.claimant||'Unassigned')}</dd><dt>Assigned session</dt><dd>${t.assigned_session?link('sessions',t.assigned_session,{sid:t.assigned_session}):'Not recorded'}</dd></dl>${t.status==='doing'?`<p class="inflight-row">${inflightButton(t.id,'button primary')}</p>`:''}${taskPlanDetail(state.overview,t.id)}${taskContract(t)}${paragraph('Purpose and source',t.purpose||t.reason||op?.done_when)}${t.source?paragraph('Recorded source reference',t.source):''}${t.status!=='done'&&t.last_completion?'<p class="inline-warning">This task was reopened. The linked report describes earlier work; it does not establish a new completion.</p>':''}${r?.summary?paragraph('At a glance',r.summary):''}${paragraph('What was done',r?.what)}${paragraph('How it was done',r?.how,'record-method')}${paragraph('Result',r?.result)}${r?.truncated?'<p class="inline-warning">These are bounded report excerpts. Read the full report for the complete method and result.</p>':''}<section class="detail-section"><h3>Work report and proof</h3>${evidenceLinks(t.proof?[t.proof]:[])}${r?.sealed?`<p class="row-sub">Seal recorded ${esc(date(r.sealed_at))}. Current proof verification has not been run by this view.</p>`:''}</section>${t.history?.length?`<section class="detail-section"><h3>Work history</h3><ol class="record-lifecycle">${t.history.map(e=>`<li><time>${esc(date(e.at))}</time><span>${esc(e.to?'Moved to '+human(e.to):human(e.kind))}${e.session?`<br>${link('sessions',short(e.session,34),{sid:e.session})}`:''}</span></li>`).join('')}</ol></section>`:''}<div class="link-row">${link('activity','Task history',{ref:t.id})}${link('messages','Related messages',{q:t.id})}</div>`);
}
function inspectOutcome(id){
 const t=state.overview.items.find(x=>x.id===id);if(!t)return;
 showInspector(`${String(t.number||Number(t.id.replace(/\D/g,''))).padStart(2,'0')}. ${t.title}`,`<dl class="detail-grid"><dt>Outcome</dt><dd class="mono">${esc(t.id)}</dd><dt>Current check</dt><dd>${badge(t.status)}</dd>${t.description?`<dt>Full statement</dt><dd>${esc(t.description)}</dd>`:''}<dt>Recorded result</dt><dd>${badge(t.recorded_status)}</dd><dt>Stage</dt><dd>${esc(stageName(t.stage))}</dd><dt>Current check time</dt><dd>${esc(date(t.verified_at))}</dd><dt>Recorded completion</dt><dd>${esc(date(t.recorded_completed_at))}</dd><dt>Completing session</dt><dd>${t.recorded_completed_session?link('sessions',t.recorded_completed_session,{sid:t.recorded_completed_session}):'Not recorded'}</dd></dl><section class="detail-section"><h3>What the check found</h3><p>${esc(t.reason||'No explanation was recorded.')}</p></section><section class="detail-section record-method"><h3>Recorded verification method</h3><p>${esc(t.method?.description||t.method?.oracle||'Method not recorded.')}</p>${t.method?.proof_kind?`<p class="row-sub">Evidence kind: ${esc(t.method.proof_kind)}</p>`:''}</section>${t.observed?`<section class="detail-section"><h3>Observed result</h3><pre class="raw-data">${esc(typeof t.observed==='string'?t.observed:JSON.stringify(t.observed,null,2))}</pre></section>`:''}<section class="detail-section"><h3>Evidence</h3>${evidenceLinks(t.evidence)}</section><section class="detail-section"><h3>Runbook source</h3>${evidenceLinks(typeof t.source==='string'?[t.source]:[])}${typeof t.source!=='string'?`<p>${esc(JSON.stringify(t.source||{}))}</p>`:''}</section><div class="link-row">${profileOn('runs')?link('runs','Open run history'):''}${link('activity','Outcome history',{ref:t.ref||t.id})}</div>`);
}
// The Activity list as a vertical timeline: clock time left of the line, a dot per event in its
// lane's color, a day marker where the date changes, and a live Now marker at the head.
// t-606: a run of identical failure notes arrives folded into one item (hub.history); say how many.
function repeatNote(e){return e.repeat>1?` <small class="event-repeat">${esc(`repeated ${count(e.repeat)} times${e.sessions>1?` across ${count(e.sessions)} sessions`:''}${e.variants>1?`, ${count(e.variants)} different messages`:''}, since ${relative(e.first_ts)}`)}</small>`:'';}
// An automatic refresh joins the newest page to the loaded one; a fold that continues across the seam stays one row.
function joinFolded(fresh,old){const a=fresh[fresh.length-1],b=old[0];if(!a||!b||!a.fold||a.fold!==b.fold)return [...fresh,...old];const sessions=Math.max(a.sessions||1,b.sessions||1),variants=Math.max(a.variants||1,b.variants||1);return [...fresh.slice(0,-1),{...a,repeat:(a.repeat||1)+(b.repeat||1),first_ts:b.first_ts||b.ts,sessions,variants},...old.slice(1)];}
function timelineRows(items,head=true){
 const day=v=>new Date(v).toLocaleDateString(undefined,{weekday:'short',month:'short',day:'numeric'}),today=day(Date.now());
 let last='';
 return (head?`<div class="vtl-now"><time data-live-clock>${esc(clock(Date.now()))}</time><span class="vtl-node"></span><strong>Now</strong></div>`:'')+items.map(e=>{
  const d=day(e.ts),mark=d!==last?`<div class="vtl-day"><span></span><strong>${esc(d===today?'Today, '+d:d)}</strong></div>`:'';last=d;
  return mark+`<div class="vtl-item lt-${laneOf(e)}" id="event-${esc(e.id)}"><time class="vtl-time" datetime="${esc(e.ts)}">${esc(clock(e.ts))}<small>${esc(relative(e.ts))}</small></time><span class="vtl-node" aria-hidden="true"></span><div class="event-text"><h3>${esc(e.kind==='run'&&e.ref?stageName(e.ref)+': ':'')}${esc(eventSummary(e))}${repeatNote(e)}</h3><div class="event-meta"><span>${esc(human(e.kind))}</span>${e.ref?`<a href="${esc(href('work',{q:e.ref}))}">${esc(e.ref)}</a>`:''}${e.session?`<a href="${esc(href('sessions',{sid:e.session}))}">${esc(short(e.actor||e.session,26))}</a>`:''}</div><details><summary>View recorded context</summary><pre class="raw-data">${esc(JSON.stringify(e.data,null,2))}</pre></details></div></div>`;
 }).join('');
}
// Activity: task, op and run progress by default (subagent and session bookkeeping stay in the
// other views), a live timeline of the loaded events, and the event list. An automatic refresh
// prepends what is new and keeps the earlier pages already loaded.
async function activity(ticket,more=false,automatic=false){
 const p=state.params,kind=p.get('kind')||'progress',span=p.get('span')||'6',view=p.get('view')==='days'?'days':'live',day=view==='days'?(p.get('day')||todayKey()):'';
 // By day reads the chosen day whole plus the day-by-hour counts; Live pages back through history.
 const data=await api('/hub/history.json',{q:p.get('q'),kind,session:p.get('session'),ref:p.get('ref'),limit:day?1000:more?50:100,before:more?state.history?.next_before:null,day,calendar:day?1:null});
 if(ticket!==state.ticket)return;if(automatic&&readerEngaged())return false;
 const old=state.history,top=old?.items[0]?.id??-1;
 state.history=more?{...data,items:[...old.items,...data.items]}:automatic&&old&&!day?{...old,total:data.total,items:joinFolded(data.items.filter(e=>e.id>top),old.items)}:data;
 const h=state.history,keep=Object.fromEntries([...p].filter(([k])=>!['view','day','path'].includes(k)));
 const link=extra=>href('activity',{...keep,...(extra.view==='days'?{view:'days'}:{}),...(extra.day?{day:extra.day}:{})});
 mount(heading('Activity','Task, op and run progress from the project record, on a live clock. Open an event to inspect its original context.')+
  liveTimeline(h.items,{span,view,day,hours:h.hours||[],summary:eventSummary,link})+
  `<div class="toolbar">${search('Search the recorded history...',p.get('q'))}${select('kind',[['progress','Task, op and run progress'],['work','Work, sessions and messages'],['all','All recorded events'],['task-move','Task transitions'],['msg','Messages'],['session-start','Session starts']],kind,'Filter events')}${view==='days'?'':select('span',SPANS,span,'Timeline window')}${p.get('session')?`<button class="button" data-clear="session">Session filter ${icon('close')}</button>`:''}${p.get('ref')?`<button class="button" data-clear="ref">${esc(p.get('ref'))} ${icon('close')}</button>`:''}<span class="toolbar-end">${count(h.items.length)} shown / ${count(h.total)} matching events / refreshes every 15 s</span></div>`+
  `<section class="panel timeline vtl">${h.items.length?timelineRows(h.items,!day):empty('No matching events','Try another term or switch to all recorded events.','activity')}${h.has_more?'<div class="pagination"><button class="button" data-action="history-more">Load earlier events</button></div>':'<div class="panel-foot">Beginning of the matching record.</div>'}</section>`);
 startClocks();
}
async function messages(ticket){const [o,all]=await Promise.all([loadOverview(),loadMessages()]);if(ticket!==state.ticket)return;const p=state.params;const q=(p.get('q')||'').toLowerCase(),kind=p.get('kind')||'',to=p.get('to')||'';const list=all.filter(m=>(!q||JSON.stringify(m).toLowerCase().includes(q))&&(!kind||m.kind===kind)&&(!to||m.to===to));mount(heading('Messages','Reports, decisions, and handoffs left in the shared record for people and agents.')+collectionVisuals(list,'kind','Message types in the filtered record')+`<div class="toolbar">${search('Search messages or task references...',p.get('q'))}${select('kind',[['','All message types'],...Array.from(new Set(all.map(m=>m.kind))).sort().map(k=>[k,human(k)])],kind,'Message type')}${select('to',[['','All recipients'],...Array.from(new Set(all.map(m=>m.to).filter(Boolean))).sort().map(k=>[k,k])],to,'Recipient')}<span class="toolbar-end">${list.length} messages</span></div>${list.map(m=>`<article class="message-card"><div class="message-header"><span class="avatar">${esc((m.sender||m.session||'A').slice(0,2).toUpperCase())}</span><strong>${esc(m.sender||short(m.session,30)||'Agent')}</strong><span>to ${esc(m.to||'workspace')}</span>${m.kind==='question'?orb('listening',20,'Waiting for an answer'):''}${badge(m.kind)}<time>${esc(date(m.ts))}</time></div><div class="message-body-preview">${esc(short(m.body,650))}</div>${String(m.body||'').length>650?`<details class="message-expand"><summary>Read full message</summary><div class="message-body">${esc(m.body)}</div></details>`:''}<div class="message-footer">${m.ref?link('activity',m.ref,{ref:m.ref}):''}${m.session?link('sessions','Open session',{sid:m.session}):''}</div></article>`).join('')||empty('No matching messages','Agent reports and handoffs appear here when they are recorded.','messages')}`);}
async function library(ticket,more=false){const p=state.params;const seen=flags.seen('library');const data=await api('/hub/documents.json',{q:p.get('q'),category:p.get('category')||'reports',ext:p.get('ext'),offset:more?state.documents?.next_offset:0});if(ticket!==state.ticket)return;flags.settle('library',seen);state.documents=more?{...data,items:[...state.documents.items,...data.items]}:data;const d=state.documents,category=p.get('category')||'reports';
 // Library sections: one tab per collection, so runbooks and proofs each have a place of their own.
 const sections=[['reports','Reports & knowledge'],['proofs','Sealed work & proof'],['runbooks','Runbooks'],['decisions','Decisions & plans'],['tools','Tool documentation'],['all','All documents']];
 const kind=doc=>doc.sealed?'Sealed proof':doc.category==='runbooks'?(doc.ext==='yaml'?'Runbook':'Runbook guide'):doc.category;
 mount(heading('Library','Reports, runbooks and project knowledge, organized for reading. Tool documentation is available when you need it.')+`<nav class="tabs" aria-label="Library sections">${sections.map(([key,label])=>`<a class="tab ${category===key?'active':''}" href="${esc(href('library',{...(p.get('q')?{q:p.get('q')}:{}),category:key}))}"${category===key?' aria-current="page"':''}>${esc(label)}</a>`).join('')}</nav>`+(category==='runbooks'?'<p class="route-hint">Runbooks (YAML) are the executable plans an op runs from; runbook guides explain the format. Files kept as evidence are left out.</p>':'')+collectionVisuals(d.items,'ext','Formats in '+d.items.length+' loaded documents / '+d.total+' matching documents')+`<div class="toolbar">${search('Search reports, titles, or paths...',p.get('q'))}${select('ext',[['','All formats'],['md','Markdown'],['html','HTML'],['yaml','YAML']],p.get('ext')||'','Document format')}<span class="toolbar-end">${count(d.total)} documents</span></div><div class="panel table-wrap">${d.items.length?`<table class="data-table"><thead><tr><th>Document</th><th>Collection</th><th>Format</th><th>Updated</th></tr></thead><tbody>${d.items.map(doc=>`<tr><td><a class="row-title" href="${esc(href('library',{...Object.fromEntries(p),path:doc.path}))}">${esc(doc.title)}</a><span class="row-sub">${esc(short(doc.path,110))}${doc.task?` &middot; ${esc(doc.task)}`:''}</span></td><td>${badge(kind(doc))}</td><td><span class="mono">${esc(doc.ext.toUpperCase())}</span></td><td><span class="row-sub">${esc(date(doc.mtime))}</span></td></tr>`).join('')}</tbody></table>`:empty('No documents match','Change the collection or search to find a report.')} ${d.has_more?'<div class="pagination"><button class="button" data-action="documents-more">Load more documents</button></div>':''}</div>`);if(p.get('path'))await readDocument(p.get('path'),ticket);}
// Markdown is rendered from escaped text only. Raw HTML never enters the parent document.
function inline(text,basePath=""){let v=esc(text);const codes=[];v=v.replace(/`([^`]+)`/g,(_,s)=>{codes.push(`<code>${s}</code>`);return `\u0001${codes.length-1}\u0001`;});v=v.replace(/\[([^\]]+)\]\(([^\s)]+)\)/g,(_,label,target)=>{const raw=target.replace(/&amp;/g,'&');if(/^https?:\/\//i.test(raw))return `<a href="${target}" target="_blank" rel="noopener noreferrer">${label}</a>`;if(/^(?:javascript|data|vbscript):/i.test(raw))return label;try{const local=raw.replace(/^local:/,'');const resolved=new URL(local,'https://hub.invalid/'+basePath);if(resolved.origin==='https://hub.invalid'&&/\.(md|html?)$/i.test(resolved.pathname))return `<a href="${esc(href('library',{path:decodeURIComponent(resolved.pathname).replace(/^\//,'')}))}">${label}</a>`;}catch{}return `<span>${label} <code>${target}</code></span>`;});v=v.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/\*([^*]+)\*/g,'<em>$1</em>');return v.replace(/\u0001(\d+)\u0001/g,(_,n)=>codes[Number(n)]);}
function markdown(text,basePath=""){const fmt=t=>inline(t,basePath);const lines=String(text).split('\n');let out='',para=[],code=null,list=null,headings=0;const flush=()=>{if(para.length){out+=`<p>${para.map(fmt).join('<br>')}</p>`;para=[];}if(list){out+=`</${list}>`;list=null;}};for(let i=0;i<lines.length;i++){const line=lines[i];if(/^\s*```/.test(line)){flush();if(code!==null){out+=`<pre><code>${esc(code.join('\n'))}</code></pre>`;code=null;}else code=[];continue;}if(code!==null){code.push(line);continue;}if(i+1<lines.length&&line.includes('|')&&/^\s*\|?\s*:?-{3,}/.test(lines[i+1])){flush();const cells=s=>s.trim().replace(/^\||\|$/g,'').split('|').map(v=>v.trim());out+='<table><thead><tr>'+cells(line).map(v=>`<th>${fmt(v)}</th>`).join('')+'</tr></thead><tbody>';i+=2;for(;i<lines.length&&lines[i].includes('|')&&lines[i].trim();i++)out+='<tr>'+cells(lines[i]).map(v=>`<td>${fmt(v)}</td>`).join('')+'</tr>';out+='</tbody></table>';i--;continue;}const h=line.match(/^(#{1,6})\s+(.+)/);if(h){flush();out+=`<h${h[1].length} id="doc-section-${headings++}" tabindex="-1">${fmt(h[2])}</h${h[1].length}>`;continue;}if(/^\s*([-*_])\1\1+\s*$/.test(line)){flush();out+='<hr>';continue;}const li=line.match(/^\s*(?:([-*+])|\d+\.)\s+(.+)/);if(li){if(para.length){out+=`<p>${para.map(fmt).join('<br>')}</p>`;para=[];}const tag=li[1]?'ul':'ol';if(list!==tag){if(list)out+=`</${list}>`;out+=`<${tag}>`;list=tag;}out+=`<li>${fmt(li[2]).replace(/^\[x\]/i,'&#9745;').replace(/^\[ \]/,'&#9744;')}</li>`;continue;}if(line.startsWith('>')){flush();out+=`<blockquote>${fmt(line.replace(/^>\s?/,''))}</blockquote>`;continue;}if(!line.trim()){flush();continue;}if(list){out+=`</${list}>`;list=null;}para.push(line);}flush();if(code!==null)out+=`<pre><code>${esc(code.join('\n'))}</code></pre>`;return out;}
async function readDocument(path,ticket=state.ticket){showInspector(path,loader('document',{compact:true}),'DOCUMENT',true);try{const d=await api(/\.(md|html?|ya?ml)$/i.test(path)?'/hub/document.json':'/files/read',{path});if(ticket!==state.ticket)return;if(d.error)throw Error(d.error);const html=/\.html?$/i.test(path),yaml=/\.ya?ml$/i.test(path);const doc=state.documents?.items.find(x=>x.path===path);showInspector(doc?.title||path.split('/').pop(),`<div class="reader-controls"><span class="reader-path">${esc(path)}</span>${badge(html?'HTML':yaml?'YAML':'Markdown')}<button class="button" data-action="download-document">${icon('download')}Save copy</button></div>${d.truncated?'<div class="notice"><div><h3>This document is partially displayed</h3><p>The reader reached the server limit. The full source remains in the project.</p></div></div>':''}${html?'<iframe class="report-frame" sandbox="" referrerpolicy="no-referrer" title="Report preview"></iframe>':`<div class="document-overview">${proofOverview(d.text)}<div data-document-nav></div></div><article class="markdown">${yaml?`<pre class="raw-data">${esc(d.text)}</pre>`:markdown(d.text,path)}</article>`}`,'DOCUMENT',true);state.document=d;if(!html){const nav=$('#inspector [data-document-nav]');if(nav)nav.outerHTML=documentNavigation([...document.querySelectorAll('#inspector .markdown h2[id]')].map(h=>({id:h.id,title:h.textContent})));}if(html){const parsed=new DOMParser().parseFromString(d.text,'text/html');parsed.querySelectorAll('script,base,meta[http-equiv],iframe,object,embed,form').forEach(e=>e.remove());const csp=parsed.createElement('meta');csp.httpEquiv='Content-Security-Policy';csp.content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; form-action 'none'; base-uri 'none'";parsed.head.prepend(csp);$('#inspector iframe').srcdoc='<!doctype html>'+parsed.documentElement.outerHTML;}}catch(e){$('#inspector-content').innerHTML=`<h2 id="inspector-title" class="detail-title">Document unavailable</h2><p class="reader-path">${esc(path)}</p><div class="detail-section">${errorView(e)}</div>`;}}
async function runs(ticket){const [data,o]=await Promise.all([loadRuns(),loadOverview()]);if(ticket!==state.ticket)return;const p=state.params,q=(p.get('q')||'').toLowerCase(),status=p.get('status')||'';const rows=data.items.filter(r=>(!q||JSON.stringify(r).toLowerCase().includes(q))&&(!status||String(r.verdict).toLowerCase()===status));const selected=rows.find(r=>r.id===p.get('run')||(p.get('receipt')&&(r.receipt_id===p.get('receipt')||r.stages?.some(s=>s.receipt_id===p.get('receipt')))));mount(heading('Runs & logs','Follow execution from recorded attempts to stage results and the original output.',link('work','Check current evidence',{tab:'outcomes'},'button'))+`<div class="toolbar">${search('Search design, stage, result, or run...',p.get('q'))}${select('status',[['','All results'],...Array.from(new Set(data.items.map(r=>String(r.verdict).toLowerCase()))).map(v=>[v,human(v)])],status,'Run result')}<span class="toolbar-end">${rows.length} recorded runs</span></div><p class="route-hint">Run results describe what happened at the time. Current acceptance is checked separately against today's inputs and evidence.</p>${selected?runDetail(selected,p.get('receipt')):(o.active_jobs?.length?`<a class="lv-banner" href="#live">${orb('working',20)}<strong>${o.active_jobs.length} job${o.active_jobs.length===1?'':'s'} running now</strong><span>Open live logs</span>${icon('arrow')}</a>`:'')+runsOverview(rows,stageName,o.profile?.stages||[])+runVisuals(rows,null)}<div class="panel table-wrap">${rows.length?`<table class="data-table"><thead><tr><th>Run / design</th><th>Stage</th><th>Result</th><th>Started</th><th>Duration</th></tr></thead><tbody>${rows.map(r=>`<tr><td><a class="row-title" href="${esc(href('runs',{run:r.id}))}">${esc(r.design||'Unspecified design')}</a><span class="row-sub mono">${esc(short(r.id,24))}</span></td><td>${esc(stageName(r.stage))}<span class="row-sub">${r.stages?.length||1} recorded stage${r.stages?.length===1?'':'s'}</span></td><td>${badge(r.verdict)}</td><td><span class="row-sub">${esc(date(r.started_at))}</span></td><td><span class="row-sub">${esc(duration(r.elapsed_s))}</span></td></tr>`).join('')}</tbody></table>`:empty('No runs match','Run receipts will appear here as stages are executed.','runs')}</div>${data.errors?.length?`<p class="inline-warning">${data.errors.length} run records could not be fully read.</p>`:''}`);if(selected){const stages=selected.stages||[];const s=stages.find(s=>s.receipt_id===p.get('receipt'))||stages.at(-1);const receipt=s?.receipt_id||selected.receipt_id;if(receipt){const live=String(s?.verdict||selected.verdict).toUpperCase()==='RUNNING';state.log={receipt,text:'',offset:0,base:1,following:live,flagged:false,query:'',run:selected,active:s,scan:null,scanSize:-1,jump:+p.get('line')||0};loadReview(state.log);loadContract(state.log);await fetchLog(true);state.timer=setInterval(()=>{if(state.log?.following)fetchLog();},2500);}}}
function runDetail(r,receipt){const s=(r.stages||[]).find(x=>x.receipt_id===receipt)||(r.stages||[]).at(-1);return runDashboard(r,s,stageName,{session:r.session?link('sessions','Session behind this run',{sid:r.session}):'',evidence:`<details class="detail-section"><summary>Evidence references</summary>${evidenceLinks(r.evidence)}</details><div id="resources" class="resource-line"></div>`});}
let liveCtl=null;
function stopLive(){if(liveCtl){liveCtl.stop();liveCtl=null;}}
async function live(ticket){stopLive();const pinned=(state.params.get('jobs')||'').split(',').filter(j=>/^[a-f0-9]{32}$/.test(j));const ctl=createLive({api,stageName});liveCtl=ctl;const timer=await ctl.start(pinned);if(ticket!==state.ticket||liveCtl!==ctl){clearInterval(timer);ctl.stop();return;}state.timer=timer;}
let logBusy=false;
const LOG_CAP=8*1024*1024;
async function fetchLog(all=false){const l=state.log;if(!l||logBusy)return;logBusy=true;try{do{const d=await api('/hub/log.json',{receipt:l.receipt,offset:l.offset});if(state.log!==l)return;if(d.offset<l.offset){l.text='';l.base=1;}l.text+=d.text;l.offset=d.offset;l.has_more=d.has_more;l.size=d.size;}while(all&&l.has_more&&l.text.length<LOG_CAP);if(l.text.length>LOG_CAP){const cut=l.text.indexOf('\n',l.text.length-LOG_CAP)+1;l.base+=(l.text.slice(0,cut).match(/\n/g)||[]).length;l.text=l.text.slice(cut);l.trimmed=true;}renderLog();if(l.size!==l.scanSize&&(!l.scanAt||Date.now()-l.scanAt>8000))await loadScan(l);}catch(e){if(state.log===l&&$('#log-output')){$('#log-output').textContent=e.message;$('#log-status').textContent='No captured output is available for this receipt.';}}finally{logBusy=false;}}
async function loadContract(l){try{const c=await api('/hub/contract.json',{receipt:l.receipt});if(state.log===l)renderContract(c,l.receipt);}catch(e){if(state.log===l)renderContract({error:e.message});}}
async function loadReview(l){try{const data=await api('/hub/logreview.json',{receipt:l.receipt});if(state.log!==l)return;l.reviewLines=reviewLines(data);renderReview(data,l.receipt);renderLog();}catch(e){if(state.log===l&&$('#rl-review'))$('#rl-review').innerHTML=`<p class="inline-warning">Agent reviews could not be read: ${esc(e.message)}</p>`;}}
async function loadScan(l){l.scanAt=Date.now();try{const scan=await api('/hub/logscan.json',{receipt:l.receipt});if(state.log!==l)return;l.scan=scan;l.scanSize=scan.size;renderScan(scan,l.run,l.active);renderLog();const first=l.jump||(!l.following&&scan.first_error?.line);if(first&&!l.jumped){l.jumped=true;jumpToLine(l,first);}}catch(e){if(state.log===l&&$('#rl-kpis'))$('#rl-kpis').innerHTML=`<div class="rl-kpi tone-attention"><span>Log scan</span><strong>Unavailable</strong><small>${esc(e.message)}</small></div>`;}}
function renderLog(){renderLogLines(state.log);}
function saveText(text,name){const a=document.createElement('a');const url=URL.createObjectURL(new Blob([text],{type:'text/plain;charset=utf-8'}));a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
async function sessions(ticket,automatic=false){
 const p=state.params,sid=p.get('sid'),view=p.get('view')||'analysis',key=location.hash;
 // A live refresh patches the view already on screen instead of rebuilding it. The conversation
 // tab is being read: it keeps its content and the "New activity available" refresh button.
 if(automatic&&sid&&view==='conversation')return false;
 const inPlace=Boolean(automatic&&state.sessionsKey===key&&$(sid?'#session-detail':'#analytics-index'));
 // A failed list fetch goes to route(): a quiet refresh keeps the page and retries later.
 const seen=flags.seen('analytics'),data=await loadSessions();if(ticket!==state.ticket)return;
 const rows=(data.sessions||[]).filter(s=>s.class==='work'||!s.class),q=(p.get('q')||'').toLowerCase();
 const selected=rows.find(s=>s.sid===sid)||(sid?{sid,title:sid}:null);
 // The From/To days filter the list and drive the burn board; a refused range shows why.
 const today=todayKey(),range=sid?null:dayRange(p.get('from'),p.get('to'),today),days=range&&!range.error?range:null,rangeNote=range?.error||range?.note||'';
 const dates=`${dayInput('from','From',days?days.from:p.get('from'),today)}${dayInput('to','To',days?days.to:p.get('to'),today)}${range?`<button class="button" data-clear="from,to">Clear dates ${icon('close')}</button>`:''}`;
 const bar=sid?'analysis-session-controls':'toolbar',controls=sid?`${link('sessions','All sessions')}<select class="filter-select" data-session-switch aria-label="Choose a session">${rows.map(s=>`<option data-key="${esc(s.sid)}" value="${esc(s.sid)}" ${sid===s.sid?'selected':''}>${esc(short(s.title||s.sid,95))}</option>`).join('')}</select><span class="toolbar-end">${rows.length} working sessions / probes excluded</span>`:`${search('Search session title or identifier...',p.get('q'))}${select('agent',AGENT_CHOICES,p.get('agent')||'','Agent')}${select('sort',SORT_CHOICES,p.get('sort')||'newest','Sort sessions')}${dates}<span class="toolbar-end">${rows.length} working sessions / probes excluded</span>${rangeNote?`<p class="range-note inline-warning" role="status">${esc(rangeNote)}</p>`:''}`;
 if(inPlace)patchHtml($(`#main .${bar}`),controls);
 else{mount(heading('Session analytics','Understand the cost, context and actions behind the work.',link('library','Browse reports',{},'button'),'ENGINEERING INSIGHT')+`<div class="${bar}">${controls}</div>`+`<div class="${sid?'session-analytics-layout':'analysis-index'}">${sid?'':'<div id="burn-board" class="burn-board">'+loader('burn',{compact:true})+'</div>'}<div ${sid?'class="panel" id="session-detail"':'id="analytics-index"'}>${loader('sessions',{compact:true})}</div></div>`);state.sessionsKey=key;}
 // The switcher names the open session whatever the patch did to the option list.
 const switcher=$('#main [data-session-switch]');if(switcher&&[...switcher.options].some(o=>o.value===sid))switcher.value=sid;
 try{
  if(!sid){
   const board=burnBoard(ticket,inPlace,days);let index=await api('/hub/analytics-index.json');
   while(index.state==='building'){
    if(ticket!==state.ticket)return;
    if(!inPlace)$('#analytics-index').innerHTML=loader('sessions',{compact:true,note:'This first scan can take a minute; other pages remain available.'});
    await new Promise(resolve=>setTimeout(resolve,2000));if(ticket!==state.ticket)return;
    index=await api('/hub/analytics-index.json');
   }
   if(ticket!==state.ticket)return;
   const filtered=sessionList(index,q,p.get('agent')||'',p.get('sort')||'newest',days);
   // While the server rebuilds the index the same body comes back until the fold moves: draw it only
   // when its revision (ETag) changed, and poll with a growing delay (2, 4, 8 ... 30 s), not every 2 s.
   if(!inPlace)indexPoll={tries:0,etag:null};
   const unchanged=inPlace&&index.refreshing&&index.etag&&index.etag===indexPoll.etag;
   if(!unchanged)(inPlace?patchHtml:renderHtml)($('#analytics-index'),analyticsIndex(filtered));
   indexPoll.etag=index.etag||null;
   if(index.refreshing){flags.mark(['analytics']);const delay=refreshDelay(indexPoll.tries++);setTimeout(()=>{if(ticket===state.ticket&&state.route==='sessions')applyLive();},delay);}
   else indexPoll.tries=0;
   await board;if(!index.refreshing)flags.settle('analytics',seen);return;
  }
  const [d,a,family]=await Promise.all([api('/hub/session.json',{sid,child:p.get('child'),around:view==='conversation'?p.get('around'):null}),api('/hub/analytics.json',{sid,child:p.get('child')}),view==='crew'?api('/hub/analytics-children.json',{sid}):null,view==='report'&&state.overview?Promise.all([tryTaskText(),loadMessages()]):null]);
  if(ticket!==state.ticket)return;state.detail=d;state.analytics=a;state.family=family;if(inPlace)patchHtml($('#session-detail'),sessionDetailHtml(d,selected,true));else renderSessionDetail(d,selected);flags.settle('analytics',seen);
 }catch(e){if(ticket!==state.ticket)return;if(inPlace)throw e;$(sid?'#session-detail':'#analytics-index').innerHTML=sid&&e.status===404?`<div class="panel-body">${empty('No captured session is registered','The work record names '+sid+', but no session or conversation source is available for that identifier. Completion provenance remains in the task history.','sessions')}<div class="link-row">${link('activity','Read recorded activity',{session:sid})}${link('work','Back to work and evidence')}</div></div>`:errorView(e);}
}
// The token and cost board above the session index (burn.js, /hub/burn.json). It loads beside the
// index, never before it, and a failure here leaves the index alone. A day range replaces
// the window: the browser sends its local midnights as epoch seconds; a window chip drops the range.
let indexPoll={tries:0,etag:null},burnPollState={tries:0,stamp:null},burnTimer=0;
async function burnBoard(ticket,inPlace,days,retry=false){
 const p=state.params,win=['24h','7d','all'].includes(p.get('window'))?p.get('window'):'24h',q=p.get('q');
 try{const data=await api('/hub/burn.json',days?{start:days.start/1000,end:days.end/1000}:{window:win});if(ticket!==state.ticket)return;const el=$('#burn-board');if(!el)return;
  clearTimeout(burnTimer);if(!inPlace&&!retry)burnPollState={tries:0,stamp:null};const bp=burnPoll(data,burnPollState);burnPollState=bp.poll;
  if(bp.draw)(inPlace?patchHtml:renderHtml)(el,renderBurn(data,{href:sid=>href('sessions',{sid}),childLink:(sid,child)=>href('sessions',{sid,child,view:'conversation'}),windowHref:w=>href('sessions',{...(q?{q}:{}),window:w}),rangeLabel:days?(days.from===days.to?days.from:days.from+' to '+days.to):''}));
  if(bp.draw)noteBurnBoard(data);
  // A board served while the server rebuilds it is asked for again with the index's growing delay (2, 4, 8 ... 30 s).
  if(bp.delay!==null)burnTimer=setTimeout(()=>{if(ticket===state.ticket&&state.route==='sessions')burnBoard(ticket,true,days,true);},bp.delay);}
 catch(e){if(ticket!==state.ticket)return;
  // A poll that failed leaves the board it drew (still marked as updating) and asks again with the next delay, 30 s at most.
  if(inPlace&&retry){clearTimeout(burnTimer);burnTimer=setTimeout(()=>{if(ticket===state.ticket&&state.route==='sessions')burnBoard(ticket,true,days,true);},refreshDelay(burnPollState.tries++));return;}
  if(inPlace)return;const el=$('#burn-board');if(el)el.innerHTML=`<div class="error-box"><h2>The token and cost board could not be loaded</h2><p>${esc(e.message||e)}</p><button class="button" data-action="retry">Try again</button></div>`;}
}
function renderSessionDetail(d,summary){renderHtml($('#session-detail'),sessionDetailHtml(d,summary));}
// keepLedger: a live patch of the same tab keeps the response-ledger search, filters and page.
function sessionDetailHtml(d,summary,keepLedger=false){
 const p=state.params,tab=p.get('view')||'analysis',sid=d.session.sid,child=p.get('child'),coverage=d.coverage,children=d.children||[],a=state.analytics;
 const tabs=[['analysis','Overview'],['cost','Costs & tokens'],['context','Context window'],['actions','Actions & hooks'],['crew','Agent crew'],['conversation','Conversation & tools'],['report','Work & reports']];
 let content;
 if(['analysis','cost','context','actions'].includes(tab)){content=renderAnalytics(a,tab,{keepLedger});}
 else if(tab==='crew'){content=renderCrew(state.family);}
 else if(tab==='report'){
  const tasks=(state.overview?.tasks||[]).filter(t=>t.assigned_session===sid||t.completed_session===sid||t.history?.some(e=>e.session===sid)),msgs=(state.messages||[]).filter(m=>m.session===sid);
  content=`<div class="conversation"><section class="detail-section"><h3>Recorded work</h3>${tasks.length?`<div class="table-wrap"><table class="data-table"><tbody>${taskRows(tasks)}</tbody></table></div>`:'<p class="muted">No task assignment, completion or lifecycle event is recorded for this session.</p>'}</section><section class="detail-section"><h3>Reports and handoffs</h3>${msgs.length?msgs.map(m=>`<article class="message-card"><div class="message-header">${badge(m.kind)}${esc(date(m.ts))}</div><p class="message-body">${esc(m.body)}</p></article>`).join(''):'<p class="muted">No session messages are recorded.</p>'}</section>${link('activity','Full session work history',{session:sid})}</div>`;
 }else{
  content=`<div class="conversation">${p.has('around')?`<div class="conversation-focus">Opened around transcript line ${esc(p.get('around'))}. Usage is measured per model response; a response can contain multiple visible blocks. <a href="${esc(href('sessions',{sid,...(child?{child}:{}),view:'cost'}))}">Back to costs and tokens</a></div>`:''}${d.has_more?'<div class="pagination"><button class="button" data-action="session-earlier">Load earlier conversation</button></div>':''}${d.messages.length?d.messages.map(messageTurn).join(''):empty('No conversation is captured',coverage.reason||'Only the session registration is available.','messages')}${d.has_later?'<div class="pagination"><button class="button" data-action="session-later">Load later conversation</button></div>':''}<div class="capture-note">${esc(coverage.reason)} ${d.messages.length} of ${d.total}${coverage.total_is_exact?'':'+'} captured entries displayed.</div></div>`;
 }
 return `<div class="session-summary"><span class="eyebrow">${child?'CHILD AGENT':'SESSION RECORD'} / ${esc(a?.session.operator||d.session.operator)}</span><h2>${esc(child?d.session.title:summary.title||d.session.title)}</h2><span class="reader-path">${esc(sid)}</span><div class="session-statline"><div><strong>${count(a?.summary.responses)}</strong>model responses</div><div><strong>${count(a?.summary.tool_calls??d.session.tool_calls)}</strong>tool calls</div><div><strong>${count(children.length)}</strong>child records</div><div><strong>${esc(child?'Separate':duration(summary.span_s))}</strong>${child?'child scope':'captured span'}</div></div>${children.length||child?`<div class="child-links">${child?`<a href="${esc(href('sessions',{sid,view:tab}))}">Back to parent session</a>`:''}${children.length?`<details class="children-summary"><summary>Explore ${children.length} child agent records</summary><div class="child-links">${children.map(c=>`<a href="${esc(href('sessions',{sid,child:c.id,view:tab}))}">${esc(short(c.title,65))}</a>`).join('')}</div></details>`:''}</div>`:''}<div class="link-row">${link('activity','Recorded activity',{session:sid})}${link('messages','Messages',{q:sid})}</div></div><div class="tabs" style="padding:0 12px;margin-bottom:0">${tabs.map(([key,label])=>`<a class="tab ${tab===key?'active':''}" href="${esc(href('sessions',{sid,...(child?{child}:{}),view:key}))}">${label}</a>`).join('')}</div>${content}`;
}
function messageTurn(m){
 const a=state.analytics;const entry=(a?.ledger||[]).find(r=>r.message_ids?.includes(m.id)||r.source?.line===m.source?.line),around=Number(state.params.get('around'));
 const meta=entry?`${entry.usage?.input_tokens!=null?count(entry.usage.input_tokens)+' input / '+count(entry.usage.output_tokens)+' output':''}${entry.cost?.total_usd!=null?' / $'+entry.cost.total_usd.toFixed(4)+' estimated':''}`:'';
 return `<article class="turn ${around&&m.source?.line===around?'usage-focus':''}" data-source-line="${m.source?.line||''}"><div class="turn-header"><span class="role ${esc(m.role)}">${esc(human(m.role))}</span><time>${esc(date(m.ts))}</time><span class="mono">${esc(m.source?.kind||'')}${m.source?.line?' / '+m.source.line:''}</span></div>${meta?`<div class="capture-note">${esc(meta)} <a href="${esc(href('sessions',{sid:state.params.get('sid'),view:'cost',...(state.params.get('child')?{child:state.params.get('child')}:{})}))}">Response costs</a></div>`:''}${m.text?`<div class="turn-text">${esc(m.text)}</div>`:''}${m.truncated?'<p class="inline-warning">This captured entry is truncated.</p>':''}${(m.tools||[]).map(t=>`<details class="tool"><summary>${esc(t.name)} &middot; ${esc(t.status||'captured')}${t.input_truncated||t.result_truncated?' / partial capture':''}</summary><div class="panel-body"><h4>Input</h4><pre class="tool-output">${esc(typeof t.input==='string'?t.input:JSON.stringify(t.input,null,2))}</pre><h4>Result</h4><pre class="tool-output">${esc(typeof t.result==='string'?t.result:JSON.stringify(t.result,null,2)||'No result captured')}</pre></div></details>`).join('')}</article>`;
}
// key may list several names ("from,to" clears a range); a picked day replaces the window chip.
function changeFilter(key,value){const p=new URLSearchParams(state.params);for(const k of key.split(',')){if(value)p.set(k,value);else p.delete(k);if(value&&state.route==='sessions'&&(k==='from'||k==='to'))p.delete('window');}p.delete('path');location.hash=href(state.route,Object.fromEntries(p));}
// The mission map moved into Roadmap: an old #mission address (saved links, history, a cockpit
// fallback link) is rewritten in place to #roadmap?view=mission with its node, group and at kept.
// The built-in page views (registry renderer 'builtin'), each called as view(ticket,automatic): a quiet
// refresh passes automatic only to a page whose registry `quiet` is set, the 15 s timer to every `timer` page.
const VIEWS={cockpit,inflight:inflightView,vcs:vcsView,records:recordsView,roadmap:roadmapView,research:researchView,overview,work,runs,live,activity:(t,automatic)=>activity(t,false,automatic),messages,library,sessions};
async function route(force=false,quiet=false){const moved=routeAlias(location.hash);if(moved)history.replaceState(null,'',moved);const focused=document.activeElement?.dataset?.filter;const caret=document.activeElement?.selectionStart;const ticket=++state.ticket;clearInterval(state.timer);state.timer=null;state.log=null;stopLive();stopSlot();const [rawName,query='']=location.hash.slice(1).split('?');const name=rawName==='lib'?'library':rawName|| (location.pathname.startsWith('/analytics')?'sessions':'cockpit');if((!PAGE[name]||PAGE[name].gate==='profile')&&!state.overview){try{await loadOverview();}catch{}}state.route=visiblePages().some(p=>p.id===name)?name:'cockpit';if(state.route!=='cockpit')document.body.classList.remove('presentation');state.params=new URLSearchParams(query);if(state.route!=='roadmap'||state.params.get('view')!=='mission')state.missionEntry=null;$('#sidebar').classList.remove('open');$('#menu').setAttribute('aria-expanded','false');if($('#inspector').open)$('#inspector').close();if($('#command').open)$('#command').close();updateChrome();document.title=`${visiblePages().find(p=>p.id===state.route).label} | Alpaca Operations Studio`;const filtering=state.filtering;state.filtering=false;if(!quiet&&!filtering)mount(routeLoader(state.route==='roadmap'&&state.params.get('view')==='mission'?'mission':state.route));try{if(force){state.overview=null;state.runs=null;state.sessions=null;}await loadOverview();if(ticket!==state.ticket)return;updateChrome();const shown=await (VIEWS[state.route]||slotView)(ticket,quiet&&pageOf(state.route).quiet===true);if(ticket!==state.ticket)return;if(!focused&&!quiet)window.scrollTo(0,0);if(focused){const input=$(`[data-filter="${CSS.escape(focused)}"]`);if(input){input.focus();if(input.type==='search'&&caret!==null)input.setSelectionRange(caret,caret);}}if(!(quiet&&shown===false)){liveOrb(false);}if(!(quiet&&shown===false))showClock();$('#connection').classList.remove('error');$('#connection span').textContent='Live';}catch(e){if(ticket!==state.ticket)return;if(quiet)return liveFailed();mount(errorView(e));$('#connection').classList.add('error');$('#connection span').textContent='Offline';}}
// A quiet refresh that fails keeps the page on screen. The connection label says so, and the caches
// the page reads are marked stale again, so the next revision or idle moment retries.
// The topbar shows the current date and time, ticking. 'New activity available' holds that spot
// until a refresh loads the activity: its notice flag keeps the tick to the time alone, which phone
// width shows in place of the notice.
function clockTime(d){return esc(d.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'}));}
function paintClock(){const e=$('#checked-at');if(!e)return;const d=new Date();if(e.dataset.notice){const t=e.querySelector('.clock-time');if(t&&t.innerHTML!==clockTime(d))t.innerHTML=clockTime(d);return;}const html=`<span class="clock-date">${esc(d.toLocaleDateString(undefined,{weekday:'short',day:'2-digit',month:'short',year:'numeric'}))}</span><span class="clock-sep"> \u00b7 </span><span class="clock-time">${clockTime(d)}</span>`;if(e.innerHTML!==html)e.innerHTML=html;}
function showClock(){delete $('#checked-at').dataset.notice;paintClock();}
function newActivity(){const e=$('#checked-at');e.dataset.notice='1';e.innerHTML=`<span class="clock-notice">New activity available</span><span class="clock-time">${clockTime(new Date())}</span>`;}
paintClock();setInterval(paintClock,15000);
function liveFailed(){flags.mark(needs(state.route,liveParams()));$('#connection').classList.add('error');$('#connection span').textContent='Stale';newActivity();return false;}
// A filter box asks the server once typing stops: SEARCH_IDLE_MS after the last key in a search box
// (450 ms for a date), or at once on Enter. A value equal to the address's current one asks nothing,
// and the page keeps its content on screen while the new result loads (state.filtering skips the
// loading skeleton route() would mount).
const SEARCH_IDLE_MS=900;
let filterTimer;
function applyFilter(target){clearTimeout(filterTimer);const key=target.dataset.filter,value=target.value.trim();if((state.params.get(key.split(',')[0])||'')===value)return;const before=location.hash;state.filtering=true;changeFilter(key,value);if(location.hash===before)state.filtering=false;}
document.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.matches?.('input[data-filter]')){e.preventDefault();applyFilter(e.target);}});
document.addEventListener('input',event=>{const target=event.target;if(target.matches('input[data-filter]')){clearTimeout(filterTimer);filterTimer=setTimeout(()=>applyFilter(target),target.type==='search'?SEARCH_IDLE_MS:450);}if(target.id==='log-search'&&state.log){state.log.query=target.value;renderLog();}if(target.id==='command-query')renderCommands(target.value);});
document.addEventListener('change',event=>{if(event.target.matches('[data-session-switch]')){location.hash=href('sessions',{sid:event.target.value});return;}if(event.target.matches('select[data-filter]'))changeFilter(event.target.dataset.filter,event.target.value);});
document.addEventListener('click',event=>{const e=event.target.closest('[data-line],[data-sev]');if(!e||!state.log||!e.closest('#main'))return;event.preventDefault();event.stopPropagation();if(e.dataset.sev&&state.log.scan)renderFindings(state.log.scan,e.dataset.sev);else if(e.dataset.line)jumpToLine(state.log,+e.dataset.line);},true);
document.addEventListener('click',async event=>{const e=event.target.closest('button,a');if(!e)return;if(e.matches('a.skip')){event.preventDefault();$('#main').focus();return;}if(e.dataset.close){$('#'+e.dataset.close).close();if(e.dataset.close==='inspector'&&state.params.has('path')){state.params.delete('path');history.replaceState(null,'',href(state.route,Object.fromEntries(state.params)));}return;}if(e.dataset.ifEarlier){e.disabled=true;try{await loadEarlier(api,e.dataset.ifEarlier,e.dataset.before);patchHtml($('#inflight-board'),renderInflight(state.inflight,{focus:state.params.get('task'),href}));}catch(err){e.disabled=false;toast(err.message);}return;}if(e.dataset.task){inspectTask(e.dataset.task);return;}if(e.dataset.outcome){inspectOutcome(e.dataset.outcome);return;}if(e.dataset.clear){changeFilter(e.dataset.clear,'');return;}if(e.dataset.timelineEvent){const row=document.getElementById('event-'+e.dataset.timelineEvent);if(row){row.scrollIntoView({block:'center',behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'});row.classList.remove('event-flash');void row.offsetWidth;row.classList.add('event-flash');}return;}const action=e.dataset.action;if(!action)return;try{if(action==='cockpit-pause'){state.cockpitPaused=!state.cockpitPaused;return cockpit(state.ticket);}if(action==='cockpit-present'){const on=document.body.classList.toggle('presentation');e.setAttribute('aria-pressed',String(on));e.querySelector('span').textContent=on?'Exit focus view':'Focus view';return;}if(action==='retry')return route(true);if(action==='history-more'){e.disabled=true;await activity(state.ticket,true);}if(action==='documents-more'){e.disabled=true;await library(state.ticket,true);}if(action==='session-earlier'){e.disabled=true;const d=state.detail;const next=await api('/hub/session.json',{sid:d.session.sid,child:state.params.get('child'),before:d.next_before});if(state.detail!==d)return;state.detail={...next,messages:[...next.messages,...d.messages],has_later:d.has_later,next_after:d.next_after};const summary=state.sessions.sessions.find(s=>s.sid===d.session.sid)||d.session;renderSessionDetail(state.detail,summary);}if(action==='session-later'){e.disabled=true;const d=state.detail;const next=await api('/hub/session.json',{sid:d.session.sid,child:state.params.get('child'),after:d.next_after});if(state.detail!==d)return;state.detail={...next,messages:[...d.messages,...next.messages],has_more:d.has_more,next_before:d.next_before};renderSessionDetail(state.detail,state.sessions.sessions.find(s=>s.sid===d.session.sid)||d.session);}if(action==='log-follow'&&state.log){state.log.following=!state.log.following;renderLog();}if(action==='log-more')await fetchLog();if(action==='log-start'&&state.log){state.log.offset=0;state.log.text='';state.log.base=1;state.log.trimmed=false;await fetchLog(true);}if(action==='log-flagged'&&state.log){state.log.flagged=!state.log.flagged;renderLog();}if(action==='log-save'&&state.log)saveText(state.log.text,state.log.receipt+'.log');if(action==='download-document'&&state.document)saveText(state.document.text,state.document.path.split('/').pop());}catch(err){e.disabled=false;toast(err.message);}});
$('#menu').addEventListener('click',()=>{const open=$('#sidebar').classList.toggle('open');$('#menu').setAttribute('aria-expanded',String(open));});
document.addEventListener('click',e=>{if(window.innerWidth<=700&&!e.target.closest('#sidebar,#menu')){$('#sidebar').classList.remove('open');$('#menu').setAttribute('aria-expanded','false');}});
// The pulse orb is the reload control: a click or Enter/Space reloads the view, as the old
// refresh button did, and pulse.js plays the shapeshift on the same events as its feedback.
$('#pulse-orb').addEventListener('click',()=>route(true));
$('#pulse-orb').addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' ')route(true);});
function openCommands(){renderCommands('');$('#command-query').value='';$('#command').showModal();$('#command-query').focus();}
$('#search-launch').addEventListener('click',openCommands);
document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();openCommands();}if(e.key==='Escape'){document.body.classList.remove('presentation');const focus=$('[data-action=cockpit-present]');if(focus){focus.setAttribute('aria-pressed','false');focus.querySelector('span').textContent='Focus view';}$('#sidebar').classList.remove('open');$('#menu').setAttribute('aria-expanded','false');}});
function renderCommands(query){const q=query.toLowerCase();const results=visiblePages().filter(p=>p.label.toLowerCase().includes(q)).map(p=>({url:href(p.id),title:p.label,type:'Page',symbol:p.icon}));if(q&&state.overview)results.push(...state.overview.tasks.filter(t=>(t.title+' '+t.id).toLowerCase().includes(q)).slice(0,8).map(t=>({url:href('work',{q:t.id}),title:t.title,type:'Work item',symbol:'work'})));if(q)results.push({url:href('library',{q:query}),title:`Search reports for "${query}"`,type:'Library',symbol:'library'},{url:href('activity',{q:query}),title:`Search history for "${query}"`,type:'Activity',symbol:'activity'});$('#command-results').innerHTML=results.map(r=>`<a class="command-result" href="${esc(r.url)}">${icon(r.symbol)}<span>${esc(r.title)}</span><small>${r.type}</small></a>`).join('');}
// Avoid scroll jumps during reading. Data changes offer a refresh; logs follow independently.
let revisions=null;
// New record data is applied on its own once the reader is idle: no open panel, no focused field.
let liveTimer=null;
function applyLive(){clearTimeout(liveTimer);liveTimer=setTimeout(()=>{if(!autoRefresh(state.route,flags.dirty,liveParams()))return;if(document.hidden||state.log||state.route==='live'||(state.route==='cockpit'&&state.cockpitPaused)||document.querySelector('dialog[open]')||['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName))return;const ticket=state.ticket+1,mounts=state.mounts;Promise.resolve(route(false,true)).then(()=>{if(state.ticket===ticket&&state.mounts!==mounts)window.scrollTo(0,state.mountY);});},1500);}
// Scroll is put back only after a full mount by this same refresh: an in-place patch keeps the layout
// (and whatever the reader scrolled to meanwhile), and a reader who navigated keeps the new page's position.
document.addEventListener('close',e=>{if(e.target.tagName==='DIALOG')applyLive();},true);
document.addEventListener('focusout',()=>applyLive());
document.addEventListener('visibilitychange',()=>{if(!document.hidden)applyLive();});
try{const stream=new EventSource('/events');stream.addEventListener('rev',e=>{if(revisions!==null&&revisions!==e.data){const marked=changedFlags(revisions,e.data);flags.mark(marked);if(dependsOn(state.route,marked,liveParams())){newActivity();liveOrb(true,'listening');}applyLive();}revisions=e.data;});stream.onerror=()=>{$('#connection').classList.add('error');$('#connection span').textContent='Reconnecting';};stream.onopen=()=>{$('#connection').classList.remove('error');$('#connection span').textContent='Live';};}catch{}
window.addEventListener('hashchange',()=>route());
// Behind the host hub's path router (its shim script carries data-prefix) this workspace lives under
// /<slug>/ and the router prefixes a "/" link back into it. "All workspaces" is the host hub's root, so
// it becomes a protocol-relative link, which the router and its shim leave alone.
const routedPrefix=document.querySelector('script[data-prefix]')?.dataset.prefix;
if(routedPrefix&&location.pathname.startsWith(routedPrefix+'/'))document.querySelectorAll('a[data-hub-home]').forEach(a=>a.setAttribute('href','//'+location.host+'/'));
hydrate();installTips();startPulse();startArthur();Promise.resolve(route()).finally(dropHandoff);
$('#inspector').addEventListener('cancel',()=>{if(state.params.has('path')){state.params.delete('path');history.replaceState(null,'',href(state.route,Object.fromEntries(state.params)));}});
$('#command-results').addEventListener('click',e=>{if(e.target.closest('a'))$('#command').close();});

// Refresh the overview in place while other views remain stable for reading.
let liveOverviewBusy=false;
// The topbar orb shows a live refresh in progress (connecting) and new activity waiting to load (listening).
const liveOrb=(on,mode='connecting')=>{const o=$('#live-orb');if(!o)return;o.hidden=!on;if(on&&o.getAttribute('state')!==mode)o.setAttribute('state',mode);};
// The sidebar connection orb listens while the stream is live and reconnects while it is not.
new MutationObserver(()=>{const c=$('#connection'),o=c?.querySelector('alpaca-orb');if(o){const want=c.classList.contains('error')?'connecting':'listening';if(o.getAttribute('state')!==want)o.setAttribute('state',want);}}).observe($('#connection'),{attributes:true,attributeFilter:['class']});
setInterval(async()=>{if(readerEngaged()||!PAGE[state.route]?.timer||liveOverviewBusy)return;liveOverviewBusy=true;liveOrb(true);const ticket=state.ticket,mounts=state.mounts;try{await loadOverview(true);flags.mark(['runs']);if(ticket===state.ticket){const rendered=await VIEWS[state.route](ticket,true);if(rendered!==false&&ticket===state.ticket){updateChrome();if(state.mounts!==mounts)window.scrollTo(0,state.mountY);showClock();}}}catch{if(ticket===state.ticket){$('#connection').classList.add('error');$('#connection span').textContent='Stale';}}finally{liveOrb(false);liveOverviewBusy=false;}},15000);

// HTTP connectivity, page freshness and source coverage are independent observations.
// The poll reads /hub/vitals.json, about 1 KB; /health.json keeps the full capture lists (t-590).
let lastHealth=null;
function healthBar(score,opts){const e=$('#health-meter'),m=healthMeter(score,opts);if(!e)return;e.dataset.band=m.band;e.toggleAttribute('data-stale',m.stale);e.setAttribute('aria-valuenow',m.value);e.setAttribute('aria-valuetext',m.valuetext);e.title=m.title;e.querySelector('.health-fill').style.width=m.width;e.querySelector('.health-label').textContent=m.text;}
async function refreshHealth(){try{const h=await api('/hub/vitals.json');const e=$('#connection');const state=connectionHealth(h);e.classList.toggle('error',state.error);e.querySelector('span').textContent=state.label;e.title=state.title;lastHealth={score:h.score,at:Date.now()};healthBar(h.score);}catch(error){const e=$('#connection');e.classList.add('error');e.querySelector('span').textContent='Offline';if(error?.status===404)healthBar(null,{old:true});else healthBar(lastHealth?.score,{since:lastHealth?.at});}}
refreshHealth();setInterval(refreshHealth,10000);
// A proof or document section button scrolls the reader to that heading; the route stays.
document.addEventListener('click',event=>{const button=event.target.closest('[data-document-section]');if(!button)return;const target=document.getElementById(button.dataset.documentSection);if(target){event.preventDefault();target.scrollIntoView({block:'start'});target.focus({preventScroll:true});}});
