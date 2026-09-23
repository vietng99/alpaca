// Live logs: one tile per running job, the grid dividing itself by job count and window size.
// A tile follows /live/log across stage changes; the log scan colors lines and never grades.
// This page is a profile page (alpaca/profile.py `web` pages "live"): the profile that turns it on
// serves /live/log and /live/telemetry.json through its routes.
import {icon} from './icons.js';
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const n=v=>Number(v||0).toLocaleString();
const href=(route,params={})=>'#'+route+(Object.keys(params).length?'?'+new URLSearchParams(params):'');
const span=s=>s==null||!Number.isFinite(s)?'-':s<60?Math.round(s)+'s':s<3600?Math.floor(s/60)+'m '+String(Math.round(s%60)).padStart(2,'0')+'s':Math.floor(s/3600)+'h '+String(Math.round(s%3600/60)).padStart(2,'0')+'m';
const gb=kb=>kb==null?'-':kb>=1048576?(kb/1048576).toFixed(2)+' GB':(kb/1024).toFixed(0)+' MB';
const WHOLE=4*1024*1024;      // a log up to this size is read from byte 0, so line numbers match the scan
const KEEP=4000;              // lines kept per tile
const TICK=2000, ACTIVE_EVERY=10000, SCAN_EVERY=10000, TELE_EVERY=10000;

export function createLive({api,stageName,route}){
 const board={tiles:new Map(),order:[],mode:'auto',focus:null,pausedAll:false,lastActive:0,busy:false,runs:null,pinned:[],dead:false};
 const $=q=>document.querySelector(q);

 function page(pinned){
  board.pinned=pinned;
  return `<div class="page-heading lv-heading"><div><span class="eyebrow">WORKSPACE</span><h1>Live logs</h1><p>Every running job, side by side. Tiles follow each job across its stages.</p></div><div class="lv-actions"><a class="button" href="${esc(href('runs'))}">All runs ${icon('arrow')}</a></div></div>
<div class="lv-toolbar"><span class="lv-count" id="lv-count"><i class="lv-pulse"></i>Looking for running jobs...</span>
<label>Layout <select id="lv-mode" aria-label="Tile layout"><option value="auto">Auto</option><option value="columns">Side by side</option><option value="rows">Stacked</option><option value="grid2">Two columns</option></select></label>
<button type="button" data-lv="pause-all" id="lv-pause-all">Pause all</button><button type="button" data-lv="clear">Clear finished</button><span class="lv-hint">Scan highlights point at lines; the recorded result stays on the receipt.</span></div>
<div id="lv-strip" class="lv-strip" hidden></div>
<div id="lv-grid" class="lv-grid" aria-live="off"></div>
<div id="lv-empty"></div>`;
 }

 function tileHtml(t){
  return `<section class="lv-tile" id="lv-${t.job}" data-job="${t.job}"><header class="lv-head"><span class="lv-dot" aria-hidden="true"></span><div class="lv-title"><strong class="lv-name"></strong><small class="lv-sub"></small></div><div class="lv-chips"><span class="lv-chip sev-error" title="Error lines flagged by the scan"><b class="lv-err">0</b> err</span><span class="lv-chip sev-warning" title="Warning lines flagged by the scan"><b class="lv-warn">0</b> warn</span><span class="lv-chip lv-mem" title="Memory of the job's process tree"><svg class="lv-spark" viewBox="0 0 60 16" preserveAspectRatio="none" aria-hidden="true"><polyline points=""/></svg><b class="lv-rss">-</b></span></div><div class="lv-buttons"><button type="button" data-lv="pause" title="Pause following" aria-label="Pause following">${icon('pause')}</button><button type="button" data-lv="focus" title="Maximize" aria-label="Maximize tile">${icon('overview')}</button><a class="lv-open" title="Open the full run" aria-label="Open the full run" href="#">${icon('external')}</a><button type="button" data-lv="close" title="Close tile" aria-label="Close tile">${icon('close')}</button></div></header><div class="lv-bar" hidden></div><div class="lv-log" tabindex="0" role="log" aria-label="Live output"></div><footer class="lv-foot"><span class="lv-state">Connecting</span><span class="lv-bytes"></span></footer></section>`;
 }

 function tile(job,{pinned=false}={}){
  let t=board.tiles.get(job);
  if(t)return t;
  t={job,pinned,receipt:null,offset:0,base:1,lines:[],numbered:true,scan:null,scanSize:-1,scanAt:0,tele:null,teleAt:0,paused:false,done:false,verdict:null,size:0,updated:0,startedAt:null,run:null,stageCount:0};
  board.tiles.set(job,t);board.order.push(job);
  $('#lv-grid')?.insertAdjacentHTML('beforeend',tileHtml(t));
  layout();
  return t;
 }

 function drop(job){board.tiles.delete(job);board.order=board.order.filter(j=>j!==job);if(board.focus===job)board.focus=null;document.getElementById('lv-'+job)?.remove();layout();}

 // The grid: tile count and width choose the columns, the window height is shared by the rows,
 // and a short last row stretches so no hole is left.
 function layout(){
  const grid=$('#lv-grid');if(!grid)return;
  const ids=board.order.filter(j=>board.tiles.has(j));
  const visible=board.focus&&ids.includes(board.focus)?[board.focus]:ids;
  const W=grid.clientWidth||window.innerWidth,count=visible.length;
  let cols=1;
  if(board.mode==='columns')cols=Math.max(1,count);
  else if(board.mode==='rows')cols=1;
  else if(board.mode==='grid2')cols=Math.min(2,Math.max(1,count));
  else cols=count<=1||W<760?1:count===2?2:count===3?(W>=1500?3:2):count<=4?2:W>=1500?Math.ceil(Math.sqrt(count)):2;
  const rows=Math.max(1,Math.ceil(count/cols));
  const avail=window.innerHeight-Math.max(0,grid.getBoundingClientRect().top)-24;
  const h=Math.max(W<760?360:280,Math.floor((avail-12*(rows-1))/rows));
  grid.style.gridTemplateColumns=`repeat(${cols},minmax(${board.mode==='columns'&&cols>2?'360px':'0'},1fr))`;
  grid.style.gridAutoRows=h+'px';
  grid.dataset.cols=cols;
  const rest=count%cols;
  ids.forEach(id=>{const el=document.getElementById('lv-'+id);if(!el)return;el.hidden=!visible.includes(id);el.style.gridColumn='';el.classList.toggle('focused',board.focus===id);});
  if(rest&&cols>1){const last=document.getElementById('lv-'+visible.at(-1));if(last)last.style.gridColumn=`span ${cols-rest+1}`;}
  const strip=$('#lv-strip');
  if(strip){strip.hidden=!board.focus;strip.innerHTML=board.focus?`<button type="button" data-lv="unfocus">${icon('overview')} Show all ${n(ids.length)} tiles</button>`+ids.filter(j=>j!==board.focus).map(j=>{const t=board.tiles.get(j);return `<button type="button" data-lv="focus-to" data-job="${j}" class="lv-mini ${t.done?'tone-'+tone(t.verdict):'tone-live'}"><i></i>${esc(label(t))}</button>`;}).join(''):'';}
  const c=$('#lv-count');
  if(c){const live=ids.filter(j=>!board.tiles.get(j).done).length;c.innerHTML=`<i class="lv-pulse ${live?'':'off'}"></i>${live?`${n(live)} job${live===1?'':'s'} running`:'No job running'}${ids.length>live?` &middot; ${n(ids.length-live)} finished or pinned`:''}`;}
  const empty=$('#lv-empty');if(empty)empty.innerHTML=ids.length?'':emptyHtml();
 }

 const tone=v=>({pass:'good',fail:'danger',failed:'danger',blocked:'attention',interrupted:'danger'})[String(v).toLowerCase()]||'quiet';
 const label=t=>{const r=t.run,st=r?.stages?.find(s=>s.receipt_id===t.receipt)||r?.stages?.at(-1);const d=st?.design||r?.design||'run';return `${d} / ${stageName(st?.stage||r?.current_stage||r?.stage||'stage')}`;};

 function emptyHtml(){
  const recent=(board.runs?.items||[]).filter(r=>r.job_id&&r.log).slice(0,4);
  return `<div class="lv-empty">${icon('runs')}<h3>No job is running now</h3><p>Tiles appear here as soon as a job starts, one per job. To replay finished jobs side by side, pin them:</p><div class="lv-recent">${recent.map(r=>`<a class="button" href="${esc(href('live',{jobs:r.job_id}))}">${esc(r.design)} / ${esc(stageName(r.stage))} <small>${esc(r.verdict)}</small></a>`).join('')}${recent.length>1?`<a class="button primary" href="${esc(href('live',{jobs:recent.map(r=>r.job_id).join(',')}))}">Pin these ${recent.length}</a>`:''}</div></div>`;
 }

 // ---- data
 async function refreshActive(force=false){
  if(!force&&Date.now()-board.lastActive<ACTIVE_EVERY)return;
  board.lastActive=Date.now();
  const [o,runs]=await Promise.all([api('/hub/overview.json'),api('/hub/runs.json')]);
  if(board.dead)return;
  board.runs=runs;
  const byJob=new Map(runs.items.filter(r=>r.job_id).map(r=>[r.job_id,r]));
  const active=new Set(o.active_jobs||[]);
  for(const job of [...active,...board.pinned])if(/^[a-f0-9]{32}$/.test(job))tile(job,{pinned:board.pinned.includes(job)});
  for(const t of board.tiles.values()){
   t.run=byJob.get(t.job)||t.run;
   t.startedAt=t.run?.started_at||t.startedAt;
   const finished=!active.has(t.job);
   if(finished&&!t.done){t.done=true;t.verdict=t.run?.verdict||'finished';t.finalRead=false;}
   if(!finished&&t.done){t.done=false;t.verdict=null;}
  }
  layout();
 }

 async function readLog(t){
  if(t.paused&&!t.done)return;
  if(t.done&&t.finalRead)return;
  let d=await api('/live/log',{job:t.job,offset:t.offset});
  if(d.receipt_id!==t.receipt){
   // a new stage: start its log over, from byte 0 when it is small enough to number exactly
   const first=t.receipt===null;
   t.receipt=d.receipt_id;t.scan=null;t.scanSize=-1;t.stageCount++;
   if(!first)t.lines.push({sep:`stage changed: ${label(t)}`});
   t.base=1;t.numbered=true;t.offset=0;
   if(d.size>WHOLE){t.offset=d.size-WHOLE;t.numbered=false;}
   if(d.offset!==t.offset)d=await api('/live/log',{job:t.job,offset:t.offset});
   if(!first)t.lines=t.lines.slice(-1);else t.lines=[];
   t.partial='';
  }
  let text=d.text||'';
  let loops=0;
  while(text!==undefined){
   const chunk=(t.partial||'')+text;const parts=chunk.split('\n');t.partial=parts.pop();
   if(!t.numbered&&t.offset===d.offset&&t.lines.length===0&&d.offset>0)parts.shift();
   for(const line of parts)t.lines.push({text:line});
   t.offset=d.next;t.size=d.size;
   if(d.eof||++loops>80)break;
   d=await api('/live/log',{job:t.job,offset:t.offset});text=d.text||'';
  }
  if(t.lines.length>KEEP){const cut=t.lines.length-KEEP;t.base+=t.lines.slice(0,cut).filter(l=>!l.sep).length;t.lines=t.lines.slice(cut);}
  t.updated=Date.now();
  if(t.done)t.finalRead=true;
 }

 async function readScan(t){
  if(!t.receipt||t.size===t.scanSize||Date.now()-t.scanAt<SCAN_EVERY&&t.scan)return;
  t.scanAt=Date.now();
  try{t.scan=await api('/hub/logscan.json',{receipt:t.receipt});t.scanSize=t.scan.size;}catch{}
 }
 async function readContract(t){
  if(!t.receipt||t.contractFor===t.receipt&&Date.now()-t.contractAt<60000)return;
  t.contractFor=t.receipt;t.contractAt=Date.now();
  try{t.contract=await api('/hub/contract.json',{receipt:t.receipt});}catch{t.contract=null;}
 }
 async function readTele(t){
  if(t.done&&t.tele||Date.now()-t.teleAt<TELE_EVERY)return;
  t.teleAt=Date.now();
  try{t.tele=await api('/live/telemetry.json',{job:t.job});}catch{}
 }

 // ---- render one tile
 function paint(t){
  const el=document.getElementById('lv-'+t.job);if(!el)return;
  el.classList.toggle('done',t.done);el.dataset.tone=t.done?tone(t.verdict):'live';
  el.querySelector('.lv-name').textContent=label(t);
  const elapsed=t.startedAt?(t.done&&t.run?.finished_at?t.run.finished_at:Date.now()/1000)-t.startedAt:null;
  el.querySelector('.lv-sub').textContent=`${t.done?String(t.verdict).toUpperCase():'running'} \u00b7 ${span(elapsed)} \u00b7 job ${t.job.slice(0,8)}${t.pinned?' \u00b7 pinned':''}`;
  const sc=t.scan;
  el.querySelector('.lv-err').textContent=n(sc?.counts?.error);
  el.querySelector('.lv-warn').textContent=n(sc?.counts?.warning);
  const s=(t.tele?.samples||[]).slice(-60).map(x=>x.tree_rss_kb).filter(v=>v!=null);
  if(s.length){const max=Math.max(...s,1);el.querySelector('.lv-spark polyline').setAttribute('points',s.map((v,i)=>`${(60*i/Math.max(1,s.length-1)).toFixed(1)},${(15-14*v/max).toFixed(1)}`).join(' '));el.querySelector('.lv-rss').textContent=gb(s.at(-1));}
  const open=el.querySelector('.lv-open');if(t.run)open.href=href('runs',{run:t.run.id,receipt:t.receipt||''});
  const pb=el.querySelector('[data-lv="pause"]');pb.classList.toggle('on',t.paused);pb.title=t.paused?'Follow output':'Pause following';
  el.querySelector('.lv-state').textContent=t.done?`Finished: ${t.verdict}`:t.paused?'Paused':`Following, updated ${Math.max(0,Math.round((Date.now()-t.updated)/1000))}s ago`;
  el.querySelector('.lv-bytes').textContent=t.size?`${n(t.offset)} of ${n(t.size)} bytes${t.numbered?'':' (tail only)'}`:'';
  const bar=el.querySelector('.lv-bar'),c=t.contract;
  if(c&&c.receipt?.id===t.receipt){
   const items=c.done_bar?.items||[],outs=(c.expected?.outputs||[]).filter(o=>o.tracked),hit=new Set(c.fail_cases?.hit||[]);
   const chip=(cls,text,title)=>`<span class="lv-bc ${cls}" title="${esc(title)}">${text}</span>`;
   const tn=v=>({pass:'good',fail:'danger',blocked:'attention',stale:'attention'})[String(v).toLowerCase()]||'quiet';
   const html=(items.length?'<b>Done bar</b>'+items.map(i=>chip('tone-'+tn(i.status),esc(i.id)+' '+esc(String(i.status||'').toUpperCase()),i.statement+(i.observed?' | observed: '+i.observed:''))).join(''):'')
    +(outs.length?(()=>{const got=outs.filter(o=>o.present).length;return '<b>Output</b>'+chip(got===outs.length?'tone-good':t.done?'tone-danger':'tone-quiet',got+' / '+outs.length+' present','Present: '+(outs.filter(o=>o.present).map(o=>String(o.path).split('/').pop()).join(', ')||'none')+' | Missing: '+(outs.filter(o=>!o.present).map(o=>String(o.path).split('/').pop()).join(', ')||'none'));})():'')
    +(hit.size?'<b>Fail case</b>'+[...hit].map(h=>chip('tone-danger',esc(h),(c.fail_cases.declared.find(f=>f.id===h)||{}).when||h)).join(''):'');
   bar.hidden=!html;if(bar.dataset.html!==html){bar.innerHTML=html;bar.dataset.html=html;}
  }
  const box=el.querySelector('.lv-log');
  const atBottom=box.scrollHeight-box.scrollTop-box.clientHeight<40;
  const marks=new Map();
  if(sc&&t.numbered)for(const sev of ['info','warning','error'])for(const line of sc.marks?.[sev]||[])marks.set(line,sev);
  let num=t.base,html='';
  const first=sc?.first_error?.line;
  for(const l of t.lines){
   if(l.sep){html+=`<div class="lv-sep">${esc(l.sep)}</div>`;continue;}
   const sev=marks.get(num);
   html+=`<div class="ll${sev?' sev-'+sev:''}${num===first?' lv-first':''}">${t.numbered?`<span class="ln">${num}</span>`:''}<span class="lt">${esc(l.text)||' '}</span></div>`;num++;
  }
  if(t.partial)html+=`<div class="ll lv-partial">${t.numbered?`<span class="ln">${num}</span>`:''}<span class="lt">${esc(t.partial)}</span></div>`;
  if(html!==t.painted){box.innerHTML=html||'<div class="lv-sep">Waiting for output</div>';t.painted=html;if(!t.paused&&(atBottom||!t.scrolled))box.scrollTop=box.scrollHeight;t.scrolled=true;}
 }

 async function tick(){
  if(board.busy||board.dead||document.hidden)return;
  board.busy=true;
  try{
   await refreshActive();
   await Promise.all([...board.tiles.values()].map(async t=>{try{await readLog(t);await Promise.all([readScan(t),readTele(t),readContract(t)]);}catch(e){t.error=e.message;}paint(t);}));
  }catch(e){const c=$('#lv-count');if(c)c.textContent='Live data unavailable: '+e.message;}
  finally{board.busy=false;}
 }

 function onClick(event){
  const b=event.target.closest('[data-lv]');if(!b)return;
  const job=b.closest('[data-job]')?.dataset.job||b.dataset.job,t=board.tiles.get(job),act=b.dataset.lv;
  if(act==='pause'&&t){t.paused=!t.paused;paint(t);}
  if(act==='focus'&&t){board.focus=board.focus===job?null:job;layout();}
  if(act==='focus-to'){board.focus=job;layout();}
  if(act==='unfocus'){board.focus=null;layout();}
  if(act==='close'&&t){board.pinned=board.pinned.filter(j=>j!==job);drop(job);}
  if(act==='clear'){for(const x of [...board.tiles.values()])if(x.done&&!x.pinned)drop(x.job);}
  if(act==='pause-all'){board.pausedAll=!board.pausedAll;for(const x of board.tiles.values())x.paused=board.pausedAll;b.textContent=board.pausedAll?'Follow all':'Pause all';for(const x of board.tiles.values())paint(x);}
  // The hub's own capture-phase handler owns data-line; these buttons are ours alone.
  event.preventDefault();
 }
 const onResize=()=>layout();

 async function start(pinned){
  board.dead=false;
  document.getElementById('main').innerHTML=page(pinned);
  const mode=document.getElementById('lv-mode');
  try{board.mode=localStorage.getItem('alpaca.live.mode')||'auto';}catch{}
  mode.value=board.mode;
  mode.addEventListener('change',()=>{board.mode=mode.value;try{localStorage.setItem('alpaca.live.mode',board.mode);}catch{}layout();});
  document.getElementById('main').addEventListener('click',onClick);
  window.addEventListener('resize',onResize);
  await refreshActive(true);
  await tick();
  return setInterval(tick,TICK);
 }
 function stop(){board.dead=true;window.removeEventListener('resize',onResize);document.getElementById('main')?.removeEventListener('click',onClick);board.tiles.clear();board.order=[];board.focus=null;}
 return {start,stop,board};
}
