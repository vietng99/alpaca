// Runs & logs dashboard. The log scan only points at lines; the receipt verdict stays the result.
import {icon} from './icons.js';
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const n=v=>Number(v||0).toLocaleString();
const href=(route,params={})=>'#'+route+(Object.keys(params).length?'?'+new URLSearchParams(params):'');
const short=(s,len=160)=>String(s||'').length>len?String(s).slice(0,len)+'...':String(s||'');
const tones={pass:'good',done:'good',running:'live',doing:'live',blocked:'attention',stale:'attention',fail:'danger',failed:'danger',error:'danger'};
const tone=s=>tones[String(s).toLowerCase()]||'quiet';
const when=v=>{if(!v)return 'Not recorded';const d=new Date(typeof v==='number'?v*1000:v);return Number.isNaN(+d)?'Not recorded':d.toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});};
const ago=v=>{if(!v)return 'never';const m=Math.floor((Date.now()-new Date(typeof v==='number'?v*1000:v))/60000);return m<1?'just now':m<60?m+'m ago':m<1440?Math.floor(m/60)+'h ago':Math.floor(m/1440)+'d ago';};
const span=s=>s==null?'Not recorded':s<60?Math.round(s)+'s':s<3600?(s/60).toFixed(1)+'m':Math.floor(s/3600)+'h '+Math.round(s%3600/60)+'m';
const SEVS=[['error','Errors'],['warning','Warnings'],['info','Notes']];
// A metric whose sign or count decides sign-off gets a tone; the rest stay neutral.
const NEGATIVE_BAD=new Set(['wns','tns','worst_slack','setup_slack','hold_slack']);
const POSITIVE_BAD=new Set(['drc_violations','router_drc','klayout_drc','antenna','antenna_violations','tests_failed','lvs_mismatch']);
const metricTone=m=>NEGATIVE_BAD.has(m.key)?(m.value<0?'danger':'good'):POSITIVE_BAD.has(m.key)?(m.value>0?'danger':'good'):'quiet';
const fmt=v=>typeof v!=='number'?String(v):Math.abs(v)>=10000?n(Math.round(v)):Number.isInteger(v)?n(v):String(+v.toFixed(4));

// ---- Runs overview: where every design stands, from the latest receipt of each stage.
export function runsOverview(rows,stageName,stageOrder=[]){
 const latest=new Map(),attempts=new Map(),designs=[],stages=[];
 // A stage named after a design is that design's own run, even inside a job over every stage.
 const own=new Set(rows.filter(r=>r.design&&r.design===r.stage).map(r=>r.stage));
 for(const r of rows)for(const s0 of (r.stages?.length?r.stages:[{stage:r.stage,verdict:r.verdict,receipt_id:r.receipt_id,started_at:r.started_at}])){
  const mine=own.has(s0.stage)||s0.design===s0.stage,d=mine?s0.stage:(r.design||'unspecified'),s={...s0,stage:mine?'design-flow':s0.stage};
  const key=d+'\u0000'+s.stage;
  attempts.set(key,(attempts.get(key)||0)+1);
  const t=s.started_at||s.finished_at||r.started_at||0,prev=latest.get(key);
  if(!prev||t>prev.t)latest.set(key,{t,verdict:s.verdict,run:r.id,receipt:s.receipt_id,reason:s.reason||r.reason});
  if(!designs.includes(d))designs.push(d);
  if(!stages.includes(s.stage))stages.push(s.stage);
 }
 const cells=[...latest.values()],failing=cells.filter(c=>tone(c.verdict)==='danger').length,passing=cells.filter(c=>tone(c.verdict)==='good').length;
 const last=rows.reduce((m,r)=>Math.max(m,r.started_at||0),0);
 const kpi=(label,value,note,t)=>`<div class="rl-kpi tone-${t}"><span>${esc(label)}</span><strong>${value}</strong><small>${esc(note)}</small></div>`;
 const kpis=`<div class="rl-kpis">${kpi('Recorded runs',n(rows.length),'jobs and receipts','quiet')}${kpi('Passing now',n(passing),`of ${n(cells.length)} pairs`,passing?'good':'quiet')}${kpi('Failing now',n(failing),failing?'latest attempt failed':'none failing',failing?'danger':'good')}${kpi('Last run',esc(ago(last)),when(last),'quiet')}</div>`;
 if(!cells.length)return kpis;
 // Column order is the profile's stage order (alpaca/profile.py `stages`), own-design runs last.
 const order=[...stageOrder,'design-flow'];
 const short4={pass:'PASS',fail:'FAIL',running:'RUN',blocked:'BLKD',interrupted:'STOP',queued:'WAIT'};
 const label=s=>s==='design-flow'?'Own design run':stageName(s);
 stages.sort((a,b)=>(order.indexOf(a)+1||99)-(order.indexOf(b)+1||99));
 designs.sort();
 const grid=`<section class="panel rl-matrix-panel"><div class="panel-head"><div><h2>Design and stage status</h2><p>Latest recorded attempt per pair. Select a cell to open that stage log.</p></div><div class="rl-legend"><span><i class="tone-good"></i>Pass</span><span><i class="tone-danger"></i>Fail</span><span><i class="tone-attention"></i>Blocked</span><span><i class="tone-live"></i>Running</span></div></div><div class="rl-matrix-wrap"><table class="rl-matrix"><thead><tr><th scope="col">Design</th>${stages.map(s=>`<th scope="col"><span>${esc(label(s))}</span></th>`).join('')}</tr></thead><tbody>${designs.map(d=>`<tr><th scope="row">${esc(d)}</th>${stages.map(s=>{const key=d+'\u0000'+s,c=latest.get(key);if(!c)return '<td><span class="rl-cell rl-none" aria-label="No attempt"></span></td>';const a=attempts.get(key);return `<td><a class="rl-cell tone-${tone(c.verdict)}" href="${esc(href('runs',{run:c.run,receipt:c.receipt||''}))}" title="${esc(`${d} / ${label(s)}: ${c.verdict}, ${a} attempt${a===1?'':'s'}, ${when(c.t)}. ${short(c.reason,140)}`)}"><b>${esc(short4[String(c.verdict).toLowerCase()]||String(c.verdict).slice(0,4))}</b>${a>1?`<small>x${a}</small>`:''}</a></td>`;}).join('')}</tr>`).join('')}</tbody></table></div></section>`;
 return kpis+grid;
}

// ---- Run dashboard skeleton; the scan and the log fill it after mount.
export function runDashboard(r,active,stageName,{evidence='',session=''}={}){
 const stages=r.stages||[];
 const steps=stages.length>1?`<ol class="rl-steps" aria-label="Stages in this run">${stages.map(s=>`<li><a class="rl-step tone-${tone(s.verdict)} ${s.receipt_id===active?.receipt_id?'current':''}" href="${esc(href('runs',{run:r.id,receipt:s.receipt_id}))}" ${s.receipt_id===active?.receipt_id?'aria-current="step"':''}><i></i><span>${esc(stageName(s.stage))}</span><small>${esc(s.verdict)} &middot; ${esc(span(s.elapsed_s))}</small></a></li>`).join('')}</ol>`:'';
 const a=active||r;
 return `<section class="panel rl-run"><div class="panel-head"><div><span class="eyebrow">RUN ${esc(short(r.id,14))}</span><h2>${esc(active?.design||r.design||'Flow run')} / ${esc(stageName(a.stage||r.stage))}</h2><p class="rl-reason tone-${tone(a.verdict)}"><b class="rl-verdict">${esc(a.verdict)}</b>${esc(a.reason||r.reason||'No explanation recorded')}</p></div><div class="rl-head-actions">${session}<a class="text-link" href="${esc(href('runs'))}">Close run ${icon('arrow')}</a></div></div>
${steps}
<div class="rl-meta"><span>Started <b>${esc(when(a.started_at||r.started_at))}</b></span><span>Duration <b>${esc(span(a.elapsed_s??r.elapsed_s))}</b></span><span>Receipt <b class="mono">${esc(short(a.receipt_id||r.receipt_id||'none',14))}</b></span></div>
<div id="rl-contract" class="rl-contract-wrap"></div>
<div id="rl-kpis" class="rl-kpis"><div class="rl-kpi tone-quiet"><span>Log scan</span><strong><span class="spinner"></span></strong><small>Reading the captured output</small></div></div>
<div id="rl-callout"></div>
<div id="rl-review"></div>
<div id="rl-map"></div>
<div class="rl-body"><aside class="rl-findings" id="rl-findings" aria-label="Lines to check"></aside>
<div class="log-shell rl-shell"><div class="log-toolbar"><span>${icon('code')} Stage output</span><button data-action="log-follow" id="log-follow">Pause following</button><button data-action="log-start">From beginning</button><button data-action="log-flagged" id="log-flagged" aria-pressed="false">Flagged lines only</button><input id="log-search" type="search" placeholder="Filter lines..." aria-label="Filter captured log lines"><button data-action="log-save">Save log</button></div><div id="log-output" class="rl-log" tabindex="0" role="log" aria-label="Captured stage output">Loading captured output...</div><div class="log-status" id="log-status">Connecting to receipt log</div></div></div>
<p class="rl-note">Highlights come from a pattern scan of this log. They point at lines to check and never change the recorded result.</p>
${evidence}</section>`;
}

// ---- Scan projection: tiles, first-failure callout, minimap, findings list.
export function renderScan(scan,run,active){
 const kp=document.getElementById('rl-kpis');if(!kp)return;
 const groups=sev=>scan.findings.filter(f=>f.severity===sev);
 const errs=groups('error'),warns=groups('warning');
 const tile=(label,value,note,t,line)=>`<${line?`button type="button" data-line="${line}"`:'div'} class="rl-kpi tone-${t}"><span>${esc(label)}</span><strong>${value}</strong><small>${esc(note)}</small></${line?'button':'div'}>`;
 const res=scan.resources||{};
 let html=tile('Errors',n(errs.length),`${n(scan.counts.error)} lines flagged`,errs.length?'danger':'good',errs[0]?.line)
  +tile('Warnings',n(warns.length),`${n(scan.counts.warning)} lines flagged`,warns.length?'attention':'good',warns[0]?.line);
 for(const m of (scan.metrics||[]).slice(0,8))html+=tile(m.label||m.key,esc(fmt(m.value))+(m.unit?`<em>${esc(m.unit)}</em>`:''),`line ${n(m.line)}`,metricTone(m),m.line);
 if(res.peak_mem_kb)html+=tile('Peak memory',esc((res.peak_mem_kb/1048576).toFixed(2))+'<em>GB</em>',res.elapsed_s!=null?`${span(res.elapsed_s)} wall clock`:'tool report','quiet',res.line);
 html+=tile('Log size',n(scan.lines)+'<em>lines</em>',`${(scan.size/1024).toFixed(0)} KB, sha256 ${String(scan.sha256).slice(0,10)}`,'quiet');
 kp.innerHTML=html;
 // The callout states the one line most worth reading first.
 const verdict=String(active?.verdict||run.verdict).toLowerCase(),fe=scan.first_error;
 let call='';
 if(fe)call=`<div class="rl-callout tone-danger">${icon('alert')}<div><h3>First error at line ${n(fe.line)}${fe.count>1?` <small>repeated ${n(fe.count)} times</small>`:''}</h3><pre>${esc(fe.text)}</pre>${scan.exit&&scan.exit.line!==fe.line?`<p>Exit: <code>${esc(short(scan.exit.text,200))}</code> at line ${n(scan.exit.line)}</p>`:''}</div><button class="button" data-line="${fe.line}">Go to line</button></div>`;
 else if(verdict==='fail')call=`<div class="rl-callout tone-attention">${icon('alert')}<div><h3>The stage failed and the scan found no error line</h3><p>The reason on the receipt is the recorded cause. Read the end of the log.</p></div><button class="button" data-line="${scan.lines}">Go to end</button></div>`;
 else if(verdict==='pass'&&scan.counts.warning)call=`<div class="rl-callout tone-quiet">${icon('check')}<div><h3>Recorded PASS with ${n(scan.counts.warning)} warning lines</h3><p>The warnings did not fail the stage. Check the list for anything unexpected.</p></div></div>`;
 document.getElementById('rl-callout').innerHTML=call;
 // Minimap: one column per slice of the log, taller where more lines were flagged.
 const d=scan.density||[],peak=Math.max(1,...d.map(b=>b.error+b.warning)),per=scan.lines/Math.max(1,d.length);
 const ticks=(scan.phases||[]).slice(0,40).map(p=>`<button type="button" class="rl-tick" data-line="${p.line}" style="left:${(100*(p.line-1)/Math.max(1,scan.lines)).toFixed(2)}%" title="${esc('Line '+p.line+': '+p.label)}" aria-label="${esc('Phase '+p.label)}"></button>`).join('');
 const fePos=fe?`<span class="rl-first" style="left:${(100*(fe.line-1)/Math.max(1,scan.lines)).toFixed(2)}%" title="First error"></span>`:'';
 document.getElementById('rl-map').innerHTML=d.length?`<div class="rl-map"><div class="rl-map-head"><strong>Where the flagged lines are</strong><span>${n(scan.lines)} lines, left to right. Ticks mark tool phases. Select a column to jump.</span></div><div class="rl-map-track">${d.map((b,i)=>{const line=Math.floor(i*per)+1,t=b.error?'danger':b.warning?'attention':'quiet',h=b.error+b.warning?Math.max(18,100*(b.error+b.warning)/peak):6;return `<button type="button" class="rl-col tone-${t}" data-line="${line}" style="--h:${h.toFixed(0)}%" title="${esc(`Lines ${line} to ${Math.floor((i+1)*per)}: ${b.error} errors, ${b.warning} warnings`)}"></button>`;}).join('')}${ticks}${fePos}</div></div>`:'';
 renderFindings(scan);
}

export function renderFindings(scan,sev){
 const box=document.getElementById('rl-findings');if(!box)return;
 const by=s=>scan.findings.filter(f=>f.severity===s);
 sev=sev||box.dataset.tab||(by('error').length?'error':by('warning').length?'warning':'info');
 box.dataset.tab=sev;
 const list=by(sev);
 box.innerHTML=`<div class="rl-tabs" role="tablist">${SEVS.map(([k,l])=>`<button type="button" role="tab" data-sev="${k}" aria-selected="${k===sev}" class="rl-tab sev-${k}">${l}<b>${n(by(k).length)}</b></button>`).join('')}</div><ol class="rl-list">${list.length?list.map(f=>`<li><button type="button" class="rl-item sev-${f.severity}" data-line="${f.line}"><code>${esc(short(f.text,220))}</code><span>line ${n(f.line)}${f.count>1?` &middot; ${n(f.count)} times`:''} &middot; ${esc(f.tool||'log')}</span></button></li>`).join(''):`<li class="rl-empty">No ${esc(sev==='info'?'notes':sev+'s')} found by the scan.</li>`}</ol>${scan.findings_truncated?`<p class="rl-more">${n(scan.findings_truncated)} more groups not listed.</p>`:''}`;
}

// ---- Log lines with numbers and severity highlights.
export function renderLogLines(l){
 const el=document.getElementById('log-output');if(!l||!el)return;
 const marks=new Map();
 for(const [sev] of SEVS.slice().reverse())for(const line of (l.scan?.marks?.[sev]||[]))marks.set(line,sev);
 const lines=l.text.split('\n');if(lines.length&&lines.at(-1)==='')lines.pop();
 const q=(l.query||'').toLowerCase(),base=l.base||1;
 let html='',shown=0,gap=0;
 const flush=()=>{if(gap){html+=`<div class="ll-gap">${n(gap)} lines hidden</div>`;gap=0;}};
 for(let i=0;i<lines.length;i++){
  const num=base+i,sev=marks.get(num),text=lines[i];
  if((l.flagged&&!sev)||(q&&!text.toLowerCase().includes(q))){gap++;continue;}
  flush();shown++;
  const rv=l.reviewLines?.get(num);
  html+=`<div class="ll${sev?' sev-'+sev:''}${rv?' rv':''}" id="L${num}"${rv?` title="${esc('Agent review '+rv)}"`:''}><a class="ln" href="#L${num}" data-line="${num}" tabindex="-1">${num}</a><span class="lt">${esc(text)||' '}</span></div>`;
 }
 flush();
 el.innerHTML=shown?html:'<div class="ll-gap">No lines match the current filter.</div>';
 if(l.following)el.scrollTop=el.scrollHeight;
 const st=document.getElementById('log-status');
 if(st)st.textContent=`${n(l.offset)} of ${n(l.size)} bytes read. ${l.has_more?'More output available.':'At the end of captured output.'}${l.trimmed?' Earlier output was dropped from the display; use From beginning to reread it.':''}`;
 const fb=document.getElementById('log-follow');if(fb)fb.textContent=l.following?'Pause following':'Follow output';
 const fl=document.getElementById('log-flagged');if(fl)fl.setAttribute('aria-pressed',String(!!l.flagged));
}

export function jumpToLine(l,line){
 if(!l)return false;
 l.following=false;
 const target=()=>document.getElementById('L'+line);
 if(!target()&&(l.query||l.flagged)){l.query='';l.flagged=false;const s=document.getElementById('log-search');if(s)s.value='';renderLogLines(l);}
 const el=target();if(!el)return false;
 const box=document.getElementById('log-output');
 box.scrollTop=el.offsetTop-box.clientHeight/2;
 box.querySelectorAll('.ll.hit').forEach(x=>x.classList.remove('hit'));
 el.classList.add('hit');
 document.getElementById('log-follow').textContent='Follow output';
 return true;
}

// ---- Agent review: plain-language reading of the log, each claim quoting its lines.
const OUTCOME={'matches-verdict':['good','Log supports the recorded result'],'disagrees-with-verdict':['danger','Log disagrees with the recorded result'],'inconclusive':['attention','Log does not settle the result']};
export function reviewLines(data){
 const m=new Map();
 for(const f of data?.reviews?.[0]?.findings||[])for(let i=f.line;i<=f.end_line;i++)if(!m.has(i))m.set(i,f.id);
 return m;
}
export function renderReview(data,receipt){
 const box=document.getElementById('rl-review');if(!box)return;
 const r=data?.reviews?.[0];
 // The review commands belong to the profile; its review payload names them (packet_command,
 // mark_command with {receipt} and {finding} placeholders). Without them no command is shown.
 const fill=t=>String(t||'').replaceAll('{receipt}',receipt).replaceAll('{finding}',r?.id||'');
 const cmd=fill(data?.packet_command);
 if(!r){box.innerHTML=`<section class="rl-review rl-review-empty">${icon('file')}<div><h3>No agent review of this log yet</h3><p>An agent reads the flagged lines and explains them; every claim quotes the log and is checked before it is kept.${cmd?' Ask an agent to run:':''}</p>${cmd?`<code>${esc(cmd)}</code>`:''}</div></section>`;return;}
 const [t,label]=OUTCOME[r.outcome]||['quiet',r.outcome];
 const fs=r.findings||[],done=fs.filter(f=>f.mark).length,ok=fs.filter(f=>f.mark?.mark==='confirmed').length,bad=fs.filter(f=>f.mark?.mark==='disputed').length;
 const markChip=f=>!f.mark?'<span class="rl-mark tone-quiet">Not checked</span>':`<span class="rl-mark tone-${f.mark.mark==='confirmed'?'good':'danger'}" title="${esc((f.mark.note||'')+' '+(f.mark.ts||''))}">${esc(f.mark.mark==='confirmed'?'Confirmed':'Disputed')} by ${esc(f.mark.by)}</span>`;
 box.innerHTML=`<section class="rl-review"><div class="rl-review-head"><div><span class="eyebrow">AGENT REVIEW &middot; ${esc(r.reviewer||'agent')} &middot; ${esc(String(r.submitted_at||'').slice(0,16).replace('T',' '))}</span><h3><span class="rl-outcome tone-${t}">${esc(label)}</span></h3></div><div class="rl-progress" title="${ok} confirmed, ${bad} disputed, ${fs.length-done} not checked"><div class="rl-progress-bar"><b class="tone-good" style="flex:${ok}"></b><b class="tone-danger" style="flex:${bad}"></b><b class="tone-quiet" style="flex:${fs.length-done}"></b></div><small>${done} of ${fs.length} findings checked by an engineer</small></div></div>
<p class="rl-summary">${esc(r.summary)}</p>
${r.intact===false?'<p class="rl-tamper">The kept review file no longer matches the hash in the record. Do not rely on it.</p>':`<p class="rl-intact">${icon('check')} Every quote was matched against log sha256 ${esc(String(r.log_sha256).slice(0,12))} before the review was kept.</p>`}
<ol class="rl-rfind">${fs.map(f=>`<li class="sev-${esc(f.severity)}"><div class="rl-rf-top"><button type="button" class="rl-line" data-line="${f.line}">line ${f.line}${f.end_line>f.line?'&ndash;'+f.end_line:''}</button><span class="rl-rf-id">${esc(f.id)}</span>${markChip(f)}</div><blockquote><code>${esc(f.quote)}</code></blockquote><p>${esc(f.meaning)}</p>${f.check?`<p class="rl-check"><b>Check:</b> ${esc(f.check)}</p>`:''}</li>`).join('')}</ol>
${data?.mark_command?`<details class="rl-howto"><summary>Confirm or dispute a finding</summary><p>Marks are appended to the record from a terminal; the newest mark per finding is shown.</p><code>${esc(fill(data.mark_command))}</code></details>`:''}
${data.reviews.length>1?`<p class="rl-more">${data.reviews.length-1} earlier review${data.reviews.length>2?'s':''} kept for this log.</p>`:''}</section>`;
}

// ---- Stage contract: input, expected output, done bar, fail cases for this receipt.
const st=v=>String(v||'unknown').toUpperCase();
const failList=(all,hit)=>[...all.filter(f=>hit.has(f.id)),...all.filter(f=>!hit.has(f.id))].slice(0,Math.max(5,all.filter(f=>hit.has(f.id)).length));
const statusTone=v=>({pass:'good',fail:'danger',blocked:'attention',stale:'attention',running:'live'})[String(v).toLowerCase()]||'quiet';
export function renderContract(c,receipt){
 const box=document.getElementById('rl-contract');if(!box)return;
 if(!c||c.error){box.innerHTML=c?.error?`<p class="inline-warning">Stage contract unavailable: ${esc(c.error)}</p>`:'';return;}
 const inp=c.input||{},ex=c.expected||{},db=c.done_bar||{},fc=c.fail_cases||{};
 const ch=inp.changed_since,moved=ch&&!ch.error?[...(ch.changed||[]),...(ch.added||[]),...(ch.removed||[])]:[];
 const knobs=inp.knobs||[];
 const input=`<section class="ct-card"><h3><span class="ct-n">1</span>Input</h3>
<dl class="ct-dl"><dt>Command</dt><dd><code>${esc(inp.command||c.declared_command||'not recorded')}</code></dd>
${inp.clock?`<dt>Clock</dt><dd><b>${esc(inp.clock.clk_period_ns)} ns</b> (${esc(inp.clock.clk_period_ns?Math.round(1000/inp.clock.clk_period_ns):'-')} MHz) from ${esc(inp.clock.file)}${inp.clock.io_delays_applied?', I/O delays applied':''}</dd>`:''}
${inp.design?`<dt>Design</dt><dd>${Object.entries(inp.design).map(([k,v])=>`${esc(k)} <b>${esc(v)}</b>`).join(' &middot; ')}</dd>`:''}
${knobs.length?`<dt>Knobs</dt><dd class="ct-knobs">${knobs.slice(0,6).map(k=>`<span><i>${esc(k.name)}</i> ${esc(short(k.value,40))}</span>`).join('')}${knobs.length>6?`<details><summary>${knobs.length-6} more from ${esc(inp.knob_file)}</summary>${knobs.slice(6).map(k=>`<span><i>${esc(k.name)}</i> ${esc(short(k.value,60))}</span>`).join('')}</details>`:''}</dd>`:''}
<dt>Files</dt><dd>${inp.archived?`<details><summary>${n(inp.files?.length)} input files hashed for this run</summary><ul class="ct-files">${(inp.files||[]).map(f=>`<li><code>${esc(f.path)}</code> <small>${esc(String(f.sha256).slice(0,10))}</small></li>`).join('')}</ul></details>`:'No archived input list for this run'}</dd>
<dt>Since this run</dt><dd>${ch?.error?`<span class="ct-warn">could not compare: ${esc(ch.error)}</span>`:!ch?'<span class="ct-muted">not compared</span>':moved.length?`<span class="ct-warn">${n(moved.length)} input${moved.length===1?'':'s'} changed:</span> ${moved.slice(0,6).map(m=>`<code>${esc(m)}</code>`).join(' ')}${moved.length>6?' ...':''}`:'<span class="ct-ok">inputs unchanged</span>'}</dd></dl></section>`;
 const outs=ex.outputs||[],mets=ex.metrics||[];
 const expected=`<section class="ct-card"><h3><span class="ct-n">2</span>Expected output</h3><ul class="ct-list">${outs.map(o=>`<li class="${o.present?'ok':o.tracked?'miss':'na'}"><b>${o.present?'&check;':o.tracked?'&#10007;':'&middot;'}</b><div><code>${esc(o.path)}</code><small>${esc(o.what)}${o.present?'':o.tracked?' &middot; missing from this receipt':' &middot; checked in the log or tree, not kept as an artifact'}</small></div></li>`).join('')||'<li class="na"><div><small>No declared outputs</small></div></li>'}</ul>${mets.length?`<div class="ct-metrics">${mets.filter(m=>m.present).map(m=>`<span class="ok"><i>${esc(m.key)}</i> ${esc(m.value)}</span>`).join('')}${mets.some(m=>!m.present)?`<span class="miss" title="${esc(mets.filter(m=>!m.present).map(m=>m.key).join(', '))}">${mets.filter(m=>!m.present).length} of ${mets.length} metrics missing</span>`:''}</div>`:''}</section>`;
 const items=db.items||[];
 const done=`<section class="ct-card"><h3><span class="ct-n">3</span>Done bar</h3>${db.error?`<p class="ct-warn">${esc(db.error)}</p>`:''}<ul class="ct-list">${items.map(i=>`<li class="tone-${statusTone(i.status)}"><span class="ct-badge">${esc(st(i.status))}</span><div><b>${esc(i.id)}</b> ${esc(i.statement)}${i.observed?`<small>observed: ${esc(short(i.observed,160))}</small>`:''}${i.cites_this_run?'<small class="ct-this">this run is the cited evidence</small>':''}</div></li>`).join('')||'<li class="na"><div><small>No acceptance item names this stage</small></div></li>'}</ul><p class="ct-note">Status is the current check against today\'s inputs.</p></section>`;
 const hit=new Set(fc.hit||[]);
 const fails=`<section class="ct-card"><h3><span class="ct-n">4</span>Fail cases</h3><ul class="ct-list">${failList(fc.declared||[],hit).map(f=>`<li class="${hit.has(f.id)?'hit':''}"><b>${hit.has(f.id)?'&#9679;':'&middot;'}</b><div>${esc(f.when)}${hit.has(f.id)?' <span class="ct-hit">this run</span>':''}<small><code>${esc(f.check)}</code></small></div></li>`).join('')}</ul>${(fc.declared||[]).length>failList(fc.declared||[],hit).length?`<details class="ct-more"><summary>${(fc.declared||[]).length-failList(fc.declared||[],hit).length} more declared fail cases</summary><ul class="ct-list">${(fc.declared||[]).filter(f=>!failList(fc.declared||[],hit).includes(f)).map(f=>`<li><b>&middot;</b><div>${esc(f.when)}<small><code>${esc(f.check)}</code></small></div></li>`).join('')}</ul></details>`:''}${fc.unmatched_reason?`<p class="ct-warn">This run failed with a reason no declared case matches: ${esc(c.receipt?.reason)}</p>`:''}${(fc.history||[]).length?`<details class="ct-hist" ${hit.size||fc.unmatched_reason?'open':''}><summary>${n(fc.history.reduce((a,h)=>a+h.count,0))} recorded failures of this stage</summary><ul>${fc.history.slice(0,10).map(h=>`<li><a href="${esc(href('runs',{receipt:h.latest}))}">${esc(h.verdict)} x${h.count}</a> ${esc(short(h.reason,160))}</li>`).join('')}</ul></details>`:'<p class="ct-note">No recorded failures of this stage.</p>'}</section>`;
 box.innerHTML=`<div class="ct"><div class="ct-head"><strong>Stage contract</strong><span>${esc(c.purpose||'')}</span></div><div class="ct-grid">${input}${expected}${done}${fails}</div></div>`;
}
