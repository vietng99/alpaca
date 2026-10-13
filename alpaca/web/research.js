// The research run view (docs/studio.md, Research): the cockpit card
// renderResearchSection(data), the hub page renderResearchPage(data, params) and bindResearch().
// It draws only what /hub/research.json carries from the record: a missing metric is never drawn
// as zero, a round with no champion value breaks the champion line, and the y range spans only
// recorded values (EC-025). This module defines functions and constants only: it touches no
// document, window or fetch at import, so cockpit.js stays importable under a stub document.
import {orb} from './orb.js';
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const href=(route,params={})=>'#'+route+(Object.keys(params).length?'?'+new URLSearchParams(params):'');
const has=v=>v!==null&&v!==undefined&&v!=='';
const shortSha=v=>has(v)?String(v).slice(0,12):'not recorded';
const fmt=v=>{if(typeof v!=='number'||!Number.isFinite(v))return 'no value';const a=Math.abs(v);return a!==0&&(a>=1e6||a<1e-3)?v.toExponential(3):String(Math.round(v*10000)/10000);};
const when=v=>{if(!v)return 'not recorded';const d=new Date(v);return Number.isNaN(+d)?String(v):d.toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});};
const minutesSince=(v,now)=>{const t=Date.parse(v);return Number.isFinite(t)?Math.floor((now-t)/60000):null;};
const ENDED=['finished','stopped','failed'];
export const STALE_MINUTES=10;
// Outcome tones, from the cockpit's tone classes (alpaca/web/cockpit.js `tones`).
const OUTCOME_TONE={improvement:'good',fixed:'good',baseline:'good',regression:'attention','repeats-failed':'attention',untested:'attention','not-fixed':'attention','gate-failed':'attention',broken:'danger',abandoned:'quiet'};
const STATE_TONE={running:'live',starting:'live',stopping:'attention',paused:'attention',halted:'attention',resumable:'attention',interrupted:'danger',failed:'danger',stopped:'quiet',finished:'good'};
const VERDICT_TONE={PASS:'good',FAIL:'danger',BLOCKED:'danger',PAUSED:'attention'};
const chip=(text,tone='quiet',title='')=>`<span class="status-chip tone-${esc(tone)}"${title?` title="${esc(title)}"`:''}>${esc(text)}</span>`;
// A run's state chip carries the orb of what the run is doing: solving while it runs, breathing while it waits.
const RUN_ORB={running:'solving',starting:'searching',stopping:'working',paused:'breathing',halted:'breathing',resumable:'breathing',interrupted:'breathing'};
const runChip=state=>{const s=state||'unknown',o=RUN_ORB[s];return `<span class="status-chip tone-${esc(STATE_TONE[s]||'quiet')} rs-state-chip">${o?orb(o,20):''}${esc(s)}</span>`;};
const outcomeTone=a=>a.recorded?(OUTCOME_TONE[a.outcome]||'quiet'):(a.running?'live':'quiet');
const runLink=(id,text)=>`<a class="text-link" href="${esc(href('research',{run:id}))}">${esc(text||id)}</a>`;
const libraryLink=(path,text)=>`<a class="text-link" href="${esc(href('library',{path}))}">${esc(text)}</a>`;
const src=(sources,key)=>sources&&sources[key]?` title="${esc('Source: '+sources[key])}"`:'';
const metricDecl=(run,id)=>(run.metrics||[]).find(m=>m.id===id)||{id};
const direction=m=>m.direction==='higher'?'higher is better':m.direction==='lower'?'lower is better':'direction not recorded';

// A recorded outcome, acceptance, election and merge are separate facts.
// Only the folded champion identifies the current promoted candidate.
const disclosures=new Map();
const boundDisclosures=new WeakSet();
let focusedReader=null;
const researchUrl=(run,params,changes={})=>{const p=new URLSearchParams(params);p.set('run',run);for(const [k,v] of Object.entries(changes)){if(v===null||v==='')p.delete(k);else p.set(k,v);}return '#research?'+p;};
const candidateLink=(run,task,label,params=new URLSearchParams())=>`<a class="text-link rs-candidate-link" href="${esc(researchUrl(run,params,{candidate:task}))}">${esc(label||task)}</a>`;
const disclosure=(key,title,body,open=false)=>`<details class="rs-disclosure" data-rs-disclosure="${esc(key)}"${(disclosures.has(key)?disclosures.get(key):open)?' open':''}><summary>${esc(title)}</summary>${body}</details>`;
const roundKey=r=>JSON.stringify([r.repair_task||null,r.round??null]);
function latestRounds(run){
  const latest=new Map();
  for(const r of run.rounds||[]){const key=roundKey(r);if(!latest.has(key)||(r.event??0)>=(latest.get(key).event??0))latest.set(key,r);}
  return [...latest.values()];
}
const election=(run,a)=>latestRounds(run).find(r=>r.winner===a.task&&r.round!==0);
const rejected=a=>a.accepted===false||['rejected','broken'].includes(a.verdict)||['refuted','unverified','exhausted'].includes(a.verify?.state)||['regression','repeats-failed','untested','not-fixed','gate-failed','broken','abandoned'].includes(a.outcome);
function decision(run,a){
  if(a.baseline||a.round===0)return {key:'baseline',label:'Baseline',tone:'quiet'};
  if(run.mode!=='repair'&&run.champion?.attempt===a.task)return {key:'champion',label:'Champion',tone:'good'};
  if(rejected(a))return {key:'rejected',label:'Rejected',tone:'attention'};
  if(election(run,a))return {key:'elected',label:'Elected',tone:'good'};
  if(a.accepted===true)return {key:'accepted',label:'Accepted',tone:'good'};
  if(a.status==='doing'||a.running)return {key:'evaluating',label:'Evaluating',tone:'live'};
  return {key:'pending',label:'Pending',tone:'quiet'};
}
const decisionChip=(run,a)=>{const d=decision(run,a);return chip(d.label,d.tone);};
const acceptance=a=>a.accepted===true?'Accepted by rule':a.accepted===false?'Rejected by rule':'Acceptance not decided';
const roundName=r=>r.round===0?'Baseline':`Round ${r.round??'unknown'}`;
function roundProgress(run){
  const n=latestRounds(run).filter(r=>Number.isInteger(r.round)&&r.round>0).length;
  if(run.mode==='repair')return `${n} case round decision${n===1?'':'s'} recorded`;
  return Number.isInteger(run.rounds_n)?`${n} of ${run.rounds_n} search rounds judged`:`${n} search rounds judged`;
}
function activeWork(run){
  const candidates=(run.attempts||[]).filter(a=>a.status==='doing'||a.running||(run.running_slots||[]).some(s=>s.task===a.task));
  const parents=(run.repair?.items||[]).filter(a=>a.state==='doing'||a.verify?.state==='running').map(a=>({...a,repairCase:true}));
  const missing=(run.running_slots||[]).filter(s=>!candidates.some(a=>a.task===s.task)).map(s=>({...s,running:s.stage}));
  return [...candidates,...missing,...parents];
}
function latestEvidence(a){
  if(a.verify?.state==='running')return 'verifying';
  if(a.audit?.verdict)return 'audit '+a.audit.verdict;
  if(a.accepted===true)return 'accepted by rule';
  if(a.accepted===false)return 'rejected by rule';
  if(a.running)return 'last recorded stage: '+a.running;
  return a.verify?.word||a.outcome_word||'task marked in progress';
}
function workLink(run,a,params){
  return a.repairCase?`<button class="row-title" data-task="${esc(a.task)}">Repair case ${esc(a.task)}</button>`:candidateLink(run.run,a.task,a.task,params);
}
function progressPanel(run,params){
  const active=activeWork(run);
  const limit=Number.isInteger(run.rounds_n)?`Round limit ${run.rounds_n}${has(run.rounds_n_start)&&run.rounds_n_start!==run.rounds_n?` (extended from ${run.rounds_n_start})`:''}`:'No round limit recorded';
  const steps=run.mode==='repair'?['Reproduce','Build fix','Test','Accept & audit','Verify','Elect & merge']:['Baseline','Propose & build','Test','Accept & audit','Verify','Elect & merge'];
  const stage=active.map(a=>a.task+': '+latestEvidence(a)).join('; ');
  const nowSteps=['starting','running'].includes(run.state)?new Set(active.filter(a=>a.verify?.state==='running').map(()=>4)):new Set();
  return `<section class="rs-progress" aria-label="Run progress"><div class="rs-progress-head"><div><h2>${esc(roundProgress(run))}</h2><p>${esc(limit)} &middot; ${esc(run.attempts_total??run.attempt_count??0)} attempts registered</p></div><span class="rs-updated">Updated ${esc(when(run.last_event_at))}</span></div><ol class="rs-flow" aria-label="Research workflow">${steps.map((s,i)=>`<li${nowSteps.has(i)?' class="rs-flow-current" aria-current="step"':''}><span>${nowSteps.has(i)?'Now':i+1}</span>${esc(s)}</li>`).join('')}</ol><p class="rs-current">${active.length?`<strong>${ENDED.includes(run.state)||run.state==='interrupted'?'Unfinished work':'In progress'}:</strong> ${active.map(a=>workLink(run,a,params)).join(', ')} <span class="muted">Latest evidence: ${esc(stage)}</span>`:run.open_holds?.length?`${run.open_holds.length} open hold${run.open_holds.length===1?'':'s'}; see the recorded decision below.`:ENDED.includes(run.state)?'This run has ended. Its partial results and decisions remain available.':'No candidate is currently marked in progress.'}</p></section>`;
}
function championPanel(run,params){
  if(run.mode==='repair')return '';
  const c=run.champion,b=run.baseline;
  const a=(run.attempts||[]).find(a=>a.task===c?.attempt);
  const values=(run.metrics||[]).map(m=>{const val=c?.metrics?.[m.id],base=b?.metrics?.[m.id];return `<div><span>${esc(m.id)}${m.unit?' ('+esc(m.unit)+')':''}</span><strong>${typeof val==='number'?esc(fmt(val)):'Not measured'}</strong><small>Baseline ${typeof base==='number'?esc(fmt(base)):'not measured'} &middot; ${esc(direction(m))}</small></div>`;}).join('');
  return `<section class="rs-champion-summary"><div><h2>Current champion</h2><p>${c?.attempt?candidateLink(run.run,c.attempt,c.attempt,params)+' &middot; merged in round '+esc(c.round):b?'Baseline retained; no candidate promoted yet.':'Baseline not measured yet.'}</p>${a&&rejected(a)?'<p class="rs-blocked">The promoted candidate has a later rejection. Open its evidence.</p>':''}<p class="muted">${code(shortSha(c?.commit))}</p>${c?.attempt?candidateLink(run.run,c.attempt,'Open champion',params):''}</div><div class="rs-champion-values">${values}</div></section>`;
}
function candidateReader(run,params){
  const task=params.get('candidate');if(!task)return '';
  const close=`<a class="button" href="${esc(researchUrl(run.run,params,{candidate:null}))}">Close</a>`;
  const a=(run.attempts||[]).find(a=>a.task===task);
  if(!a)return `<section class="panel rs-reader" data-candidate-reader="${esc(task)}" tabindex="-1"><div class="panel-head"><h2>Candidate unavailable</h2>${close}</div><div class="panel-body"><p>Details for ${code(task)} are not included in this view. Older attempts may be outside the displayed limit.</p><button class="button" data-task="${esc(task)}">Open task</button></div></section>`;
  const r=election(run,a),rk=latestRounds(run).find(r=>roundKey(r)===roundKey(a));
  const ranked=(rk?.ranking||[]).find(x=>x.attempt===a.task);
  const fact=(label,value)=>`<div><dt>${esc(label)}</dt><dd>${value}</dd></div>`;
  const metrics=(run.metrics||[]).map(m=>{const v=a.metrics?.[m.id];return `<tr><th scope="row">${esc(m.id)}</th><td>${typeof v?.median==='number'?esc(fmt(v.median)):'No metric'} ${esc(m.unit||'')}</td><td>${esc(direction(m))}</td><td>${esc((v?.repeats||[]).map(fmt).join(', ')||'No repeats recorded')}${v?.untested?' (untested)':''}</td></tr>`;}).join('');
  const selected=r?`Elected in round ${r.round}. ${r.merge?.attempt===a.task?'Merged '+shortSha(r.merge.commit):'Merge not recorded'}`:a.baseline?'Baseline measurement':rk?'Not elected in the latest round decision':'Election not recorded';
  return `<section class="panel rs-reader" data-candidate-reader="${esc(task)}" tabindex="-1"><div class="panel-head"><div><h2>${esc(a.task)} ${decisionChip(run,a)}</h2><p>${esc(a.title||'Research candidate')}</p></div>${close}</div><div class="panel-body"><div class="rs-reader-actions"><button class="button" data-task="${esc(a.task)}">Open task</button>${a.proof?.path?libraryLink(a.proof.path,'Open proof'):'<span class="muted">Proof not sealed</span>'}${run.report?.path?libraryLink(run.report.path,'Open run report'):''}</div><dl class="rs-detail-grid">${fact('Position',esc(`${roundName(a)} / slot ${a.slot??'?'}${a.category?' / '+a.category:''}${a.repair_task?' / case '+a.repair_task:''}`))}${fact('Acceptance',esc(acceptance(a)))}${fact('Outcome',chip(a.outcome_word||a.outcome||'No outcome recorded',outcomeTone(a)))}${fact('Reason',esc(a.reason||'No outcome reason recorded'))}${fact('Election',esc(selected))}${fact('Round decision',esc(rk?.reason||'No round decision recorded'))}${fact('Rank',has(ranked?.rank)?esc(ranked.rank):'Not ranked')}${fact('Task status',esc(a.status||'Unknown'))}</dl><h3>Measurements</h3>${metrics?`<div class="table-wrap"><table class="data-table rs-measurements"><thead><tr><th>Metric</th><th>Value</th><th>Direction</th><th>Repeats</th></tr></thead><tbody>${metrics}</tbody></table></div>`:'<p class="muted">No metric declared.</p>'}<h3>Checks and verification</h3><dl class="rs-detail-grid">${fact('Gates',gateChips(a))}${fact('Protected files',tamperCell(a.tamper))}${fact('Audit',auditCell(a.audit))}${fact('Verification',verifyCell(a.verify))}</dl><h3>Changes and cost</h3><dl class="rs-detail-grid">${fact('Forked from',code(a.forked_from||'Not recorded'))}${fact('Candidate commit',code(a.head||'Not recorded'))}${fact('Diff fingerprint',code(a.diff_sha256||'Not recorded'))}${fact('Cost',costCell(a))}</dl>${a.infra?.length?`<h3>Infrastructure attempts</h3><ul class="rs-list">${a.infra.map(i=>`<li>${esc(i.role||'runtime')} / try ${esc(i.try??'?')}: ${esc(i.class||'unknown')} ${esc(i.detail||'')}</li>`).join('')}</ul>`:''}</div></section>`;
}
function candidateFlow(run,params){
  const all=run.attempts||[],filter=params.get('decision')||'',q=(params.get('q')||'').toLowerCase(),roundFilter=params.get('round')||'';
  const rounds=latestRounds(run),groups=new Map();
  for(const r of rounds)groups.set(roundKey(r),{...r,attempts:[]});
  for(const a of all){const k=roundKey(a);if(!groups.has(k))groups.set(k,{round:a.round,repair_task:a.repair_task,attempts:[]});groups.get(k).attempts.push(a);}
  const choices=[['','All candidates'],['champion','Champion'],['elected','Elected'],['accepted','Accepted'],['rejected','Rejected'],['evaluating','Evaluating'],['pending','Pending'],['baseline','Baseline']].filter(([key])=>run.mode!=='repair'||!['champion','baseline'].includes(key));
  const select=`<label>Decision<select class="filter-select" data-filter="decision">${choices.map(([v,l])=>`<option value="${v}"${filter===v?' selected':''}>${l}</option>`).join('')}</select></label>`;
  const counts=all.reduce((c,a)=>{const k=decision(run,a).key;c[k]=(c[k]||0)+1;return c;},{});
  const totals=choices.slice(1).filter(([k])=>counts[k]).map(([k,l])=>`<a class="rs-count${filter===k?' active':''}" href="${esc(researchUrl(run.run,params,{decision:filter===k?null:k}))}">${counts[k]} ${l.toLowerCase()}</a>`).join('');
  const rows=[...groups.values()].sort((a,b)=>(a.repair_task||'').localeCompare(b.repair_task||'')||(b.round??-1)-(a.round??-1)).map(r=>{
    if(roundFilter&&String(r.round)!==roundFilter)return '';
    const attempts=r.attempts.filter(a=>(!filter||decision(run,a).key===filter)&&(!q||[a.task,a.title,a.reason,a.category,a.outcome].join(' ').toLowerCase().includes(q)));
    if((filter||q)&&!attempts.length)return '';
    const elected=r.round===0?'Baseline measurement':r.winner?`Elected ${candidateLink(run.run,r.winner,r.winner,params)} &middot; ${r.merge?.attempt===r.winner?'merged':'merge not recorded'}`:r.event?'No candidate elected; previous result retained.':'Election pending';
    const candidates=attempts.map(a=>`<article class="rs-candidate" data-candidate-row="${esc(a.task)}"><div class="rs-candidate-main"><div class="rs-candidate-title">${candidateLink(run.run,a.task,a.task,params)} ${decisionChip(run,a)}${a.status==='doing'?' '+chip('In progress','live'):''} <span class="muted">slot ${esc(a.slot??'?')}${a.category?' / '+esc(a.category):''}</span></div><p>${esc(a.title||'Research candidate')}</p><p class="rs-candidate-reason">${esc(a.reason||a.outcome_word||'No outcome yet')}${a.accepted===true&&rejected(a)?' &middot; accepted earlier; later checks did not pass':''}</p></div><div class="rs-candidate-result">${(run.metrics||[]).map(m=>`<span><strong>${typeof a.metrics?.[m.id]?.median==='number'?esc(fmt(a.metrics[m.id].median)):'No metric'}</strong> ${esc(m.unit||m.id)}</span>`).join('')}<small>${esc(a.verify?.word||'Verification not started')}</small></div>${candidateLink(run.run,a.task,'Open candidate',params)}</article>`).join('');
    const history=(run.rounds||[]).filter(x=>roundKey(x)===roundKey(r)).sort((a,b)=>(b.event??0)-(a.event??0));
    const ranking=(r.ranking||[]).map(x=>`<li>${has(x.rank)?'Rank '+esc(x.rank)+': ':''}${candidateLink(run.run,x.attempt,x.attempt,params)} &middot; ${esc(x.accepted===true?'accepted':x.accepted===false?'not accepted':'not decided')}${x.verify?' &middot; '+esc(x.verify):''}</li>`).join('');
    const evidence=disclosure(`${run.run}:round:${roundKey(r)}`,'Round decision and ranking',`<div class="rs-round-evidence"><p>${esc(r.reason||'No round decision recorded yet.')}</p>${ranking?`<ol class="rs-list">${ranking}</ol>`:''}${history.length>1?`<p>${history.length} recorded decisions for this round, newest shown above.</p><ul class="rs-list">${history.map(h=>`<li>Event ${esc(h.event)}: ${esc(h.reason||'No reason recorded')}${h.winner?' / elected '+esc(h.winner):' / no election'}</li>`).join('')}</ul>`:''}${r.merge?`<p>Recorded merge: ${candidateLink(run.run,r.merge.attempt,r.merge.attempt,params)} / ${code(shortSha(r.merge.commit))}</p>`:''}</div>`);
    return `<section class="rs-round" data-round-flow="${esc(r.round??'unknown')}"${r.repair_task?` data-repair-case="${esc(r.repair_task)}"`:''}><div class="rs-round-head"><div><h3>${esc(roundName(r))}${r.repair_task?` <small>case ${esc(r.repair_task)}</small>`:''}</h3><p>${elected}</p></div>${chip(r.round===0?'Reference':r.event?'Decision recorded':'Awaiting decision',r.event?'quiet':'live')}</div>${candidates||'<p class="rs-note">Candidate details are not included in the displayed attempts.</p>'}${evidence}</section>`;
  }).join('');
  const capped=run.attempts_total>all.length;
  return `<section class="rs-candidates"><div class="rs-section-head"><div><h2>Candidate decisions</h2><p>Open a candidate to see its measurements, checks and proof. Newest rounds first.</p></div></div><div class="rs-counts">${totals}</div>${capped?`<p class="rs-note">Counts and filters cover the ${all.length} displayed attempts of ${run.attempts_total}; older round decisions remain below.</p>`:''}<div class="rs-filters">${select}<label>Find candidate<input class="filter-input" type="search" data-filter="q" value="${esc(params.get('q')||'')}" placeholder="Task, title or reason"></label>${roundFilter?`<a class="text-link" href="${esc(researchUrl(run.run,params,{round:null}))}">Clear round filter</a>`:''}</div>${rows||'<p class="rs-empty-filter">No candidates match these filters.</p>'}</section>`;
}

// ------------------------------------------------------------------ the metric chart (7.5)
// chart: one entry of run.chart.metrics; bands: run.chart.bands; xCount: run.chart.x_count.
export function metricChart(chart,bands,xCount,{compact=false,state='',runId=null,params=new URLSearchParams(),width=null,mode='improve'}={}){
  if(!chart)return `<p class="rs-chart-note">No metric is declared for this run.</p>`;
  const W=compact?320:Math.max(80,width??760),H=compact?96:220,R=12,T=12,B=compact?24:52;
  const span=chart.y?chart.y.max-chart.y.min:1;
  const precision=Math.max(3,Math.min(12,Math.ceil(Math.log10(Math.max(Math.abs(chart.y?.min||0),Math.abs(chart.y?.max||0),1)/(span||1)))+3));
  const tickLabel=v=>Number(v.toPrecision(precision)).toString();
  const tickCount=W<400?3:5;
  const ticks=chart.y?Array.from({length:tickCount},(_,i)=>chart.y.min+span*i/(tickCount-1)):[];
  const showTicks=!compact&&W>=200;
  const L=showTicks?Math.max(76,...ticks.map(v=>tickLabel(v).length*7.3+12)):compact?8:12;
  const pw=W-L-R,ph=H-T-B,label=`${chart.metric}${chart.unit?' ('+chart.unit+')':''}, ${direction(chart)}`;
  const responsive=!compact&&width===null?` data-rs-chart="${esc(JSON.stringify({chart,bands,xCount,state,runId,params:params.toString(),mode}))}"`:'';
  const attrs=`data-metric="${esc(chart.metric)}" data-compact="${compact}"${responsive}`;
  const champ=(chart.champion||[]).filter(c=>typeof c.value==='number');
  const aria=champ.length?`${chart.metric} champion: first ${fmt(champ[0].value)}, last ${fmt(champ[champ.length-1].value)}`:`${chart.metric}: no champion value recorded`;
  if(!xCount||!chart.y){
    const note=state==='starting'?'Measuring the baseline (round 0).':(chart.missing||[]).length?`${chart.missing.length} attempt${chart.missing.length===1?' has':'s have'} no metric and ${chart.missing.length===1?'is':'are'} not plotted.`:'No attempt has a value for this metric yet.';
    return `<figure class="rs-chart rs-chart-empty" ${attrs}><figcaption>${esc(note)}</figcaption></figure>`;
  }
  const step=pw/xCount,x=i=>L+(i+0.5)*step,y0=chart.y.min,y1=chart.y.max,y=v=>T+(1-(v-y0)/((y1-y0)||1))*ph;
  const clip=v=>Math.max(y0,Math.min(y1,v));
  const out=[];
  out.push(`<rect class="rs-plot" x="${L}" y="${T}" width="${pw}" height="${ph}"/>`);
  for(const b of bands||[]){if(b.n<2)continue;out.push(`<rect class="rs-band rs-band-${Math.abs(b.round)%2?'odd':'even'}" x="${(x(b.x0)-step/2).toFixed(2)}" y="${T}" width="${(step*(b.x1-b.x0+1)).toFixed(2)}" height="${ph}"/>`);}
  if(!compact){
    for(const v of ticks)out.push(`<line class="rs-grid" x1="${L}" x2="${L+pw}" y1="${y(v).toFixed(2)}" y2="${y(v).toFixed(2)}"/>${showTicks?`<text class="rs-tick" x="${L-8}" y="${(y(v)+4).toFixed(2)}" text-anchor="end">${esc(tickLabel(v))}</text>`:''}`);
    let right=-Infinity;
    for(const b of bands||[]){
      const text='r'+(b.round??'?'),half=text.length*3.7,cx=(x(b.x0)+x(b.x1))/2;
      if(cx-half<right+12||cx+half>W-R||cx-half<0)continue;
      out.push(`<text class="rs-tick" x="${cx.toFixed(2)}" y="${T+ph+18}" text-anchor="middle">${esc(text)}</text>`);right=cx+half;
    }
  }
  for(const t of chart.targets||[]){const ty=y(t.value).toFixed(2);out.push(`<line class="rs-target" x1="${L}" x2="${L+pw}" y1="${ty}" y2="${ty}" data-target="${esc(t.value)}"/>`);}
  // the champion line: one path per segment; a missing champion value breaks it (never 0)
  const band=new Map((bands||[]).map(b=>[b.round,b]));
  (chart.champion_segments||[]).forEach((seg,i)=>{let d='';for(const c of seg){const b=band.get(c.round);if(!b)continue;const a=(x(b.x0)-step/2).toFixed(2),z=(x(b.x1)+step/2).toFixed(2),cy=y(c.value).toFixed(2);d+=(d?` L${a} ${cy}`:`M${a} ${cy}`)+` L${z} ${cy}`;}if(d)out.push(`<path class="rs-champion" data-segment="${i}" d="${d}"/>`);});
  for(const p of chart.points||[]){
    const cx=x(p.x).toFixed(2),cy=y(p.value).toFixed(2),tone=OUTCOME_TONE[p.outcome]||'quiet';
    const tip=`${p.task} r${p.round}.${p.slot}: ${fmt(p.value)}${chart.unit?' '+chart.unit:''} (${p.outcome||'no outcome'})${p.repeats?', '+(p.n_ok??'?')+' of '+p.repeats+' repeats ok':''}`;
    if(typeof p.lo==='number'&&typeof p.hi==='number')out.push(`<line class="rs-whisker" x1="${cx}" x2="${cx}" y1="${y(clip(p.lo)).toFixed(2)}" y2="${y(clip(p.hi)).toFixed(2)}"/>`);
    if(p.winner)out.push(`<circle class="rs-ring" cx="${cx}" cy="${cy}" r="${compact?4.5:7}"/>`);
    const dot=`<circle class="rs-dot rs-o-${tone}${p.untested?' rs-hollow':''}" cx="${cx}" cy="${cy}" r="${compact?2.6:4.2}" data-task="${esc(p.task)}" data-value="${esc(p.value)}"><title>${esc(tip)}</title></circle>`;
    out.push(runId?`<a href="${esc(researchUrl(runId,params,{candidate:p.task}))}" aria-label="${esc('Open candidate '+tip)}">${dot}</a>`:dot);
  }
  // attempts with no metric: a grey tick in the strip under the x axis, never on the value axis
  const sy=T+ph+(compact?14:34);
  if((chart.missing||[]).length&&showTicks)out.push(`<text class="rs-tick" x="${L-8}" y="${sy+4}" text-anchor="end">no metric</text>`);
  for(const m of chart.missing||[]){const mx=x(m.x).toFixed(2);const tick=`<line class="rs-missing" x1="${mx}" x2="${mx}" y1="${sy-5}" y2="${sy+5}" data-task="${esc(m.task)}"><title>${esc(`${m.task} r${m.round}.${m.slot}: no metric (${m.reason||'no reason recorded'})`)}</title></line>`;out.push(runId?`<a href="${esc(researchUrl(runId,params,{candidate:m.task}))}" aria-label="${esc('Open candidate '+m.task+', no metric')}">${tick}</a>`:tick);}
  const caption=chart.caption?esc(chart.caption)+'.':'Every attempt with a recorded value is plotted.';
  const targets=(chart.targets||[]).map(t=>`<span>Target ${esc(String(t.value))}${chart.unit?' '+esc(chart.unit):''}</span>`).join('');
  return `<figure class="rs-chart" ${attrs}><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label+'. '+aria)}" class="rs-svg" preserveAspectRatio="xMidYMid meet">${out.join('')}</svg>${compact?'':`<figcaption class="rs-caption">${targets?`<div class="rs-target-key">${targets}</div>`:''}${caption} Ring: round winner. Hollow: untested.${mode==='repair'?'':' Line: champion.'} Round labels are spaced to fit.</figcaption>`}</figure>`;
}

// ------------------------------------------------------------------ the cockpit card (7.1, 7.10)
function headline(run){
  if(run.mode==='repair'){const r=run.repair?.rate;return r?`${r.resolved} of ${r.total} resolved (${r.percent} percent)${r.so_far?' so far':''}`:'no repair tasks recorded';}
  const m=(run.metrics||[])[0];if(!m)return 'no metric declared';
  const c=run.champion?.metrics?.[m.id],b=run.baseline?.metrics?.[m.id],t=(run.targets||[]).find(t=>t.metric===m.id);
  return `${m.id}: champion ${typeof c==='number'?fmt(c):'not measured'}, baseline ${typeof b==='number'?fmt(b):'not measured'}${t?', target '+fmt(t.value):''}`;
}
function runCard(run,compact=false){
  const holds=(run.open_holds||[]).length;
  const control=run.control?(run.control_failed?chip('control failed','danger'):chip(run.control_label||'control','quiet')):'';
  return `<article class="rs-card" data-run="${esc(run.run)}"><div class="rs-card-head">${runLink(run.run)}${runChip(run.state)}${control}</div><p class="rs-runbook">${esc(run.runbook||'Runbook not recorded')}</p><p class="rs-card-line">${esc(run.mode||'mode not recorded')} &middot; ${esc(roundProgress(run))}</p><p class="rs-card-line">${esc(headline(run))}</p><p class="rs-card-line muted">${holds?`<strong class="tone-text-attention">${holds} open hold${holds>1?'s':''}</strong> &middot; `:''}${esc(run.attempt_count??0)} attempts &middot; ${esc(when(run.last_event_at))}</p>${compact&&run.mode!=='repair'?metricChart((run.chart?.metrics||[])[0],run.chart?.bands,run.chart?.x_count,{compact:true,state:run.state}):''}<div class="rs-card-footer">${runLink(run.run,'Open run')}${run.report?.path?libraryLink(run.report.path,'Report'):''}</div></article>`;
}
export function renderResearchSection(data){
  const head=sub=>`<section class="panel visual-panel rs-section" id="research-section"><div class="panel-head"><div><h2>Research runs</h2><p>${esc(sub)}</p></div><a class="text-link" href="#research">Open research</a></div>`;
  if(!data||!Array.isArray(data.runs))return head('research runs unavailable')+`<div class="panel-body"><p class="muted">research runs unavailable: the research view could not be read.</p></div></section>`;
  if(!data.runs.length)return head('No research run is recorded')+`<div class="panel-body"><p class="muted">Start one with <code>/autoresearch</code> or <code>bin/alpaca research start --runbook PATH</code>.</p></div></section>`;
  const live=data.runs.filter(r=>!ENDED.includes(r.state)),ended=data.runs.filter(r=>ENDED.includes(r.state)).slice(0,3);
  return head(`${live.length} not finished, ${data.recorded??data.runs.length} recorded`)+`<div class="rs-cards">${[...live,...ended].map(r=>runCard(r,true)).join('')}</div></section>`;
}

// ------------------------------------------------------------------ the run page
function banner(tone,title,body){return `<div class="rs-banner rs-banner-${esc(tone)}" role="status" data-banner="${esc(title)}"><h3>${esc(title)}</h3>${body}</div>`;}
const code=t=>`<code>${esc(t)}</code>`;
// A hold's state as the record holds it: lifted (research-hold-lifted), a ship hold whose card the
// owner moved before settle_ship records the lift ("card moved"), else open (section 7.3 holds row).
const holdStatus=h=>h.status||(h.lifted?'lifted':'open');
function holdState(h){
  const st=holdStatus(h);
  if(st==='lifted')return `lifted, answer ${esc(h.lifted?.answer||'recorded')} (decision ${code(h.lifted?.decision||'not recorded')})`;
  if(st==='card moved')return `card ${esc(h.card||'?')} moved: ${esc(h.card_decision||'decision not recorded')}${has(h.card_move_event)?` (event ${esc(h.card_move_event)})`:''}; the lift is recorded at the next research verb`;
  return `open${h.card?' on card '+esc(h.card):''}`;
}
function stateBanner(run,now,params=new URLSearchParams()){
  const id=run.run,fin=run.finalize||{},stops=run.stops||[],stop=stops[stops.length-1],holds=run.holds||[];
  const files=`${fin.log?`<p>Runner log: ${libraryLink(fin.log,'Open runner log')}</p>`:''}${fin.env_snapshot?`<p>Environment snapshot: ${libraryLink(fin.env_snapshot,'Open environment snapshot')}</p>`:''}${fin.forensics?`<p>Forensics: ${libraryLink(fin.forensics,'Open forensics')}</p>`:''}`;
  const stopHold=holds.find(h=>h.lifted&&h.lifted.answer==='stop');
  const stopGrant=(run.grants||[]).filter(g=>g.gate==='stop').slice(-1)[0];
  switch(run.state){
    case 'starting':return banner('live','Starting',`<p>Measuring the baseline (round 0). Contract ${code(shortSha(run.start?.contract_sha256))} from ${code(run.runbook||'no runbook recorded')}.</p>`);
    case 'running':{const age=minutesSince(run.last_event_at,now);const slots=activeWork(run);
      return banner('live',`Running: round ${run.round??0} of ${run.rounds_n??'?'}`,`<p>${slots.length?`In progress: ${slots.map(s=>`${workLink(run,s,params)} (latest evidence: ${esc(latestEvidence(s))})`).join(', ')}`:'No work is currently marked in progress.'}</p><p>Last activity ${esc(when(run.last_event_at))}.${age!==null&&age>STALE_MINUTES?` <strong class="rs-stale">No record activity for ${age} minutes.</strong> This is an observation, not a diagnosis: the view cannot see the process.`:''}</p>${run.runner?`<p>Runner ${run.runner.alive?'holds its lock on this host':'lock not held on this host'}${has(run.runner.pid)?` (pid ${esc(run.runner.pid)} on ${esc(run.runner.host||'unknown host')})`:''}.</p>`:''}`);}
    case 'paused':{const b=run.budget||{};const h=holds.find(x=>x.gate==='budget'&&!x.lifted);
      return banner('attention','Paused at a budget hold',`<p>${esc(h?.detail||'The budget hold is open.')}</p><p>Spent ${esc(fmt(b.usd_spent))} of ${esc(has(b.usd_cap)?fmt(b.usd_cap):'no')} USD cap, ${esc(fmt(b.tokens_spent))} of ${esc(has(b.tokens_cap)?fmt(b.tokens_cap):'no')} token cap${b.unpriced?`; ${esc(b.unpriced)} launches unpriced`:''}.</p><p>The owner decides: ${code(`bin/alpaca research grant ${id} budget --usd X --tokens T`)} or ${code(`bin/alpaca research grant ${id} stop`)}.</p>`);}
    case 'halted':return banner('attention','Halted at the round limit',`<p>Round ${esc(run.rounds_n)} of ${esc(run.rounds_n)} done; targets not met; owner extension owed.</p><p>The owner decides: ${code(`bin/alpaca research grant ${id} extend --rounds M`)} or ${code(`bin/alpaca research grant ${id} stop`)}.</p>`);
    case 'resumable':{const lift=holds.find(h=>h.lifted&&h.lifted.answer!=='stop')?.lifted;
      return banner('attention','Resumable',`<p>The owner's decision is recorded${lift?` (decision ${code(lift.decision)})`:''}; the run starts again with ${code(`bin/alpaca research resume ${id}`)}.</p>`);}
    case 'interrupted':return banner('danger','Interrupted',`<p>The loop runner stopped without a recorded stop; resume it with ${code(`bin/alpaca research resume ${id}`)}.</p><p>Finalize reason: ${esc(fin.reason||'not recorded')}.</p>${files}`);
    case 'stopping':case 'stopped':{const finished=(run.attempts||[]).filter(a=>a.status==='done').length;
      return banner(run.state==='stopped'?'quiet':'attention',run.state==='stopped'?'Stopped':'Stopping',`<p>${stop?`Stopped by session ${code(stop.session||'not recorded')} at ${esc(when(stop.ts))}: ${esc(stop.reason||'no reason recorded')}.`:'No stop event is recorded.'}</p><p>${finished} attempt task${finished===1?' is':'s are'} recorded done.</p>${stopHold?`<p>The owner stopped the run at its ${esc(stopHold.gate)} hold (event ${esc(stopHold.id)})${stopGrant?` with the stop grant, decision ${code(stopGrant.decision)}`:stopHold.lifted?.decision?`, decision ${code(stopHold.lifted.decision)}`:''}. A new real run under this runbook needs the owner's reopen grant.</p>`:''}`);}
    case 'failed':return banner('danger','Failed',`<p>${esc(fin.reason||'No finalize reason is recorded.')}</p>${files}<p>The partial table and chart below keep their recorded values.</p>`);
    case 'finished':{const ship=holds.find(h=>h.gate==='ship');
      return banner('good','Finished',`<p>${esc(fin.reason||'Finished.')} ${esc(headline(run))}.</p>${ship?`<p>Ship decision: ${holdState(ship)}.</p>`:''}<p>${run.report?libraryLink(run.report.path,'Open the run report'):'no report yet'}</p>`);}
    default:return banner('quiet','State unknown',`<p>The record does not give this run a state.</p>`);
  }
}
function gateChips(a){
  if(!(a.gates||[]).length)return '<span class="muted">no gate recorded</span>';
  return a.gates.map(g=>g.not_run?chip(`${g.stage}: not run`,'quiet'):chip(`${g.stage}${g.stage===a.first_failing_gate?' (first fail)':''}: ${g.verdict||'?'}`,VERDICT_TONE[g.verdict]||'quiet',`rc ${g.rc??'not recorded'}${g.offline===false?', network on':''}`)).join(' ');
}
function tamperCell(t){
  if(!t||!t.verdict)return chip('no fingerprint recorded','attention');
  const n=(t.changed||[]).length+(t.missing||[]).length;
  return t.broken?`<details class="rs-expand"><summary>${chip(`broken: ${n} path${n===1?'':'s'}`,'danger')}</summary><ul>${[...(t.changed||[]).map(p=>'changed '+p),...(t.missing||[]).map(p=>'missing '+p)].map(p=>`<li>${esc(p)}</li>`).join('')}</ul></details>`:chip('PASS','good',`${(t.points||[]).length} fingerprint points`);
}
function auditCell(au){
  if(!au)return chip('audit pending','quiet');
  if(au.verdict==='PASS'&&!(au.flags||[]).length)return chip('PASS','good',`audit event ${au.event}`);
  const flags=(au.flags||[]).map(f=>{
    if(typeof f!=='object'||f===null)return String(f);
    if(!f.flag)return JSON.stringify(f);
    return [f.flag,f.rule?'rule '+f.rule:null,f.lines?.length?'line '+f.lines.join(', '):null,f.source||null].filter(Boolean).join(' / ');
  });
  return `${chip(au.verdict||'?','danger',`audit event ${au.event}`)}${flags.length?`<ul class="rs-audit-flags">${flags.map(f=>`<li>${esc(f)}</li>`).join('')}</ul>`:''}`;
}
function verifyCell(v){
  if(!v)return chip('verification not started','quiet');
  const two=v.ruling?`<span class="rs-ruling">ruling (event ${esc(v.ruling.event)}${has(v.ruling.round)?', round '+esc(v.ruling.round):''}): ${esc(v.ruling.reason||'no reason recorded')}</span>`:(v.verifiers||[]).slice(-2).map(x=>chip(`${x.verifier||'?'} r${x.round??'?'}: ${x.verdict||'?'}`,VERDICT_TONE[x.verdict]||'quiet',x.runtime?`runtime ${x.runtime}`:'')).join(' ');
  const tone=v.state==='verified'?'good':['refuted','unverified','exhausted'].includes(v.state)||String(v.word).includes('blocked')?'danger':v.state==='stopped'?'attention':'quiet';
  const lost=(v.lost||[]).length?`<details class="rs-expand"><summary>${v.lost.length} lost tr${v.lost.length===1?'y':'ies'}</summary><ul>${v.lost.map(t=>`<li>${esc(t)}</li>`).join('')}</ul></details>`:'';
  return `${chip(v.word||v.state||'verification pending',tone)}${two?' '+two:''}${lost}<small class="rs-cmd" title="The verifier's own status">${esc(v.command||'')}</small>`;
}
function costCell(a){
  const usd=a.priced===false?'unpriced':typeof a.cost_usd==='number'?'$'+a.cost_usd.toFixed(4):'not measured';
  const tok=typeof a.tokens==='number'?a.tokens.toLocaleString()+' tokens':'tokens not measured';
  return `${esc(usd)}<br><small>${esc(tok)}</small>`;
}
function attemptTable(run,params){
  const all=run.attempts||[],metrics=run.metrics||[],S=run.sources||{};
  const fo=params.get('outcome')||'',fr=params.get('round')||'';
  const rows=all.filter(a=>(!fo||(a.recorded?a.outcome:'none')===fo)&&(!fr||String(a.round)===fr));
  const outcomes=[...new Set(all.map(a=>a.recorded?a.outcome:'none'))].sort();
  const rounds=[...new Set(all.map(a=>a.round))].sort((a,b)=>b-a);
  const sel=(name,choices,cur,label)=>`<select class="filter-select" data-filter="${name}" aria-label="${esc(label)}"><option value="">${esc(label)}</option>${choices.map(v=>`<option value="${esc(v)}" ${String(v)===cur?'selected':''}>${esc(name==='round'?'round '+v:v==='none'?'no outcome yet':v)}</option>`).join('')}</select>`;
  const th=(text,key)=>`<th${src(S,key)}>${esc(text)}</th>`;
  const cell=(label,body,cls='')=>`<td data-label="${esc(label)}"${cls?` class="${cls}"`:''}>${body}</td>`;
  const body=rows.map(a=>`<tr data-attempt="${esc(a.task)}">`+
    cell('Task',candidateLink(run.run,a.task,a.task,params))+
    cell('Round.slot',`${esc(a.round??'?')}.${esc(a.slot??'?')}${a.baseline?' <small>baseline</small>':''}`)+
    cell('Category',esc(a.category||'none'))+
    cell('Outcome',`${chip(a.outcome_word,outcomeTone(a),a.reason||'')}${a.accepted===true?' '+chip('accepted','good'):a.accepted===false?' '+chip('rejected','attention'):' <small>not decided</small>'}${a.winner?' '+chip('winner','good'):''}${a.infra_failure?' '+chip('infrastructure','danger',(a.infra||[]).map(i=>i.class).join(', ')):''}`)+
    metrics.map(m=>{const v=a.metrics?.[m.id];return cell(m.id,typeof v?.median==='number'?`<span title="${esc((v.n_ok??'?')+' of '+(v.repeats||[]).length+' repeats ok')}">${esc(fmt(v.median))}</span>${v.untested?' <small>untested</small>':''}`:`<span class="rs-nometric" title="${esc(a.reason||a.outcome_word||'no metric')}">no metric</span>`,'rs-num');}).join('')+
    cell('Gates',gateChips(a))+cell('Tamper',tamperCell(a.tamper))+cell('Audit',auditCell(a.audit))+cell('Verifiers',verifyCell(a.verify))+
    cell('Proof',a.proof?.path?libraryLink(a.proof.path,'proof '+shortSha(a.proof.sha256)):'<span class="muted">not sealed</span>')+
    cell('Status',chip(a.status||'unknown',{done:'good',doing:'live',blocked:'attention'}[a.status]||'quiet',a.claimant?'claimant '+a.claimant:''))+
    cell('Cost',costCell(a))+'</tr>').join('');
  const capped=run.attempts_total>all.length?`<p class="rs-note">showing the newest ${all.length} of ${run.attempts_total} attempts</p>`:'';
  return `<section class="panel rs-panel"><div class="panel-head"><div><h2>Attempts</h2><p>${all.length} attempt${all.length===1?'':'s'}, newest round first. The table is the text form of the chart.</p></div></div><div class="panel-body"><div class="toolbar">${sel('outcome',outcomes,fo,'All outcomes')}${sel('round',rounds,fr,'All rounds')}</div>${capped}${rows.length?`<div class="table-wrap rs-table-wrap"><table class="data-table rs-table"><thead><tr>${th('Task','attempt.task')}${th('Round.slot','attempt.round')}${th('Category','attempt.category')}${th('Outcome','attempt.outcome')}${metrics.map(m=>th(m.id+(m.unit?' ('+m.unit+')':''),'attempt.metrics')).join('')}${th('Gates','attempt.gates')}${th('Tamper','attempt.tamper')}${th('Audit','attempt.audit')}${th('Verifiers','attempt.verify')}${th('Proof','attempt.proof')}${th('Status','attempt.status')}${th('Cost and tokens','attempt.cost')}</tr></thead><tbody>${body}</tbody></table></div>`:'<p class="muted">No attempt is recorded for this run yet.</p>'}</div></section>`;
}
function chartPanel(run,params){
  const charts=run.chart?.metrics||[];
  if(run.mode==='repair'&&!charts.some(c=>(c.points||[]).length))return '';
  const pick=params.get('metric');const chart=charts.find(c=>c.metric===pick)||charts[0];
  const picker=charts.length>1?`<div class="rs-picker" role="group" aria-label="Charted metric">${charts.map(c=>`<a class="rs-pick${c===chart?' active':''}" href="${esc(researchUrl(run.run,params,{metric:c.metric}))}">${esc(c.metric)}</a>`).join('')}</div>`:'';
  const multi=(run.chart?.targets_metrics||0)>1;
  const met=multi?`<div class="rs-met"><strong>Targets met per round</strong>${(run.rounds||[]).filter(r=>!r.repair_task).slice().reverse().map(r=>{const parts=charts.map(c=>{const m=(c.targets_met||[]).find(x=>x.round===r.round);return `${c.metric} ${m?.met===true?'met':m?.met===false?'not met':'no value'}`;});return `<span>r${esc(r.round)}: ${esc(parts.join(', '))}</span>`;}).join('')}</div>`:'';
  return `<section class="panel rs-panel rs-metric-panel"><div class="panel-head"><div><h2>Metric over attempts</h2><p${src(run.sources,'chart')}>${chart?esc(chart.metric+(chart.unit?' ('+chart.unit+')':'')+', '+direction(chart)):'no metric declared'}</p></div>${picker}</div><div class="panel-body">${metricChart(chart,run.chart?.bands,run.chart?.x_count,{state:run.state,runId:run.run,params,mode:run.mode})}${met}</div></section>`;
}
function evidenceVisuals(run,params){
  const charts=run.chart?.metrics||[];
  if(run.mode==='repair'&&!charts.length)return '';
  const coverage=charts.map(c=>{
    const n=(c.points||[]).length,missing=(c.missing||[]).length,total=n+missing;
    return `<li><a class="text-link" href="${esc(researchUrl(run.run,params,{metric:c.metric}))}">${esc(c.metric)}</a><p>${n} measured / ${total} attempts <span class="muted">&middot; ${missing} without a value</span></p><div class="rs-bar-track" aria-hidden="true"><span class="rs-bar-measured" style="width:${total?n/total*100:0}%"></span></div></li>`;
  }).join('');
  const coveragePanel=`<section class="panel rs-panel rs-summary-panel" data-viz="coverage"><div class="panel-head"><div><h2>Measurement coverage</h2><p>All attempts, including baseline.</p></div></div><div class="panel-body">${coverage?`<ul class="rs-bar-list">${coverage}</ul>`:'<p class="rs-note">No declared metrics.</p>'}</div></section>`;
  if(run.mode==='repair')return `<div class="rs-visuals">${chartPanel(run,params)}${coveragePanel}</div>`;
  const records=charts.length?[...(charts[0].points||[]),...(charts[0].missing||[])]:null;
  const counts=new Map();for(const a of records||[]){const key=a.outcome||'';counts.set(key,(counts.get(key)||0)+1);}
  const names={baseline:'Baseline',improvement:'Improvement',regression:'Regression','repeats-failed':'Repeats failed',untested:'Untested','gate-failed':'Gate failed',broken:'Broken',abandoned:'Abandoned',fixed:'Fixed','not-fixed':'Not fixed'};
  const max=Math.max(1,...counts.values());
  const rows=[...counts].sort((a,b)=>b[1]-a[1]||a[0].localeCompare(b[0])).map(([key,n])=>`<li><div class="rs-bar-label"><span>${esc(names[key]||key||'No outcome recorded')}</span><strong>${n}</strong></div><div class="rs-bar-track" aria-hidden="true"><span class="rs-bar-outcome rs-bar-${OUTCOME_TONE[key]||'quiet'}" style="width:${n/max*100}%"></span></div></li>`).join('');
  const outcomesPanel=`<section class="panel rs-panel rs-summary-panel" data-viz="outcomes"><div class="panel-head"><div><h2>Recorded outcomes</h2><p>${records?`${records.length} attempts, including baseline`:'Outcome summary unavailable'}</p></div></div><div class="panel-body">${rows?`<ul class="rs-bar-list">${rows}</ul>`:`<p class="rs-note">${records?'No attempts recorded.':'A declared metric is needed for complete outcome counts.'}</p>`}<p class="rs-viz-note">Run-wide counts. Acceptance, election and promotion are separate decisions.</p></div></section>`;
  return `<div class="rs-visuals">${chartPanel(run,params)}${coveragePanel}${outcomesPanel}</div>`;
}
function lineagePanel(run,params=new URLSearchParams()){
  if(run.mode==='repair')return '';
  const nodes=run.lineage||[];
  const li=nodes.map(n=>`<li class="rs-node${n.lineage_break?' rs-break':''}"><div><strong>round ${esc(n.round)}</strong> ${code(shortSha(n.commit))} ${n.winner?candidateLink(run.run,n.winner,n.winner,params):''}${n.merge_event?` <small>merge event ${esc(n.merge_event)}</small>`:''}</div><div class="rs-node-metrics">${Object.entries(n.metrics||{}).map(([k,v])=>{const c=n.change?.[k];return `<span>${esc(k)} ${esc(typeof v==='number'?fmt(v):'no value')}${c===null||c===undefined?'':c==='not comparable'?' (not comparable)':` (${c>=0?'+':''}${esc(fmt(c))})`}</span>`;}).join('')}</div>${n.lineage_break?`<p class="rs-break-note">lineage break: forked from ${code(shortSha(n.forked_from))}, previous champion ${code(shortSha(n.previous_commit))}</p>`:''}</li>`).join('');
  return `<section class="panel rs-panel"><div class="panel-head"><div><h2>Champion lineage</h2><p${src(run.sources,'lineage')}>Each round whose winner became the champion, round 0 first.</p></div></div><div class="panel-body">${nodes.length?`<ol class="rs-lineage">${li}<li class="rs-node rs-branch">branch ${code(run.research_branch||'not recorded')}</li></ol>`:'<p class="muted">baseline not measured</p>'}</div></section>`;
}
function repairPanel(run,params=new URLSearchParams()){
  if(run.mode!=='repair')return '';
  const rep=run.repair;
  if(!rep||!(rep.items||[]).length)return `<section class="panel rs-panel"><div class="panel-head"><div><h2>Resolved rate</h2></div></div><div class="panel-body"><p class="rs-rate">no repair tasks recorded</p></div></section>`;
  const r=rep.rate,b=rep.buckets||{};
  const buckets=['resolved','unresolved','error','pending'];
  const bar=`<div class="rs-repair-bars" data-viz="repair-status"><p>${r.total} cases${r.so_far?' so far':''}: ${buckets.map(k=>`${(b[k]||[]).length} ${k}`).join(' / ')}</p><div class="rs-bar-track" aria-hidden="true">${buckets.map(k=>`<span class="rs-bar-${{resolved:'good',unresolved:'attention',error:'danger',pending:'live'}[k]}" style="width:${r.total?(b[k]||[]).length/r.total*100:0}%"></span>`).join('')}</div></div>`;
  const rows=rep.items.map(it=>`<tr data-repair="${esc(it.task)}"><td data-label="Repair task"><button class="row-title" data-task="${esc(it.task)}">${esc(it.task)}</button><small> ${esc(it.case||'')}</small></td><td data-label="Bucket">${chip(it.bucket,{resolved:'good',error:'danger',unresolved:'attention',pending:'live'}[it.bucket])}</td><td data-label="Case base">${code(shortSha(it.base_commit))}, base pass ${esc(has(it.base_pass)?it.base_pass:'not recorded')}</td><td data-label="Reproducer">${it.reproducer_proved?chip('proved','good'):chip('not proved','quiet')}</td><td data-label="Selected or latest attempt">${it.best_attempt?candidateLink(run.run,it.best_attempt,it.best_attempt,params):'none'}</td><td data-label="Fix">${it.fix?`${code(shortSha(it.fix.commit))} from ${esc(it.fix.attempt)} (r${esc(it.fix.round??'?')})`:'no fix merged'}</td><td data-label="Outcome">${esc(it.outcome||'no outcome yet')}${it.blocked_reason?`<br><small class="rs-blocked">${esc(it.blocked_reason)}</small>`:''}${it.infra_classes?.length?`<br><small>infrastructure: ${esc(it.infra_classes.join(', '))}</small>`:''}</td><td data-label="Gates">${it.best_attempt?gateChips({gates:it.best_gates||[],first_failing_gate:it.best_first_failing_gate}):'<span class="muted">no attempt</span>'}</td><td data-label="Verify">${it.verify?verifyCell(it.verify):'<span class="muted">verification not started</span>'}</td></tr>`).join('');
  return `<section class="panel rs-panel"><div class="panel-head"><div><h2>Resolved rate</h2><p${src(run.sources,'repair')}>A repair task is resolved when its fix is verified, merged and the task is done.</p></div></div><div class="panel-body"><p class="rs-rate" data-rate="${esc(r.resolved)}/${esc(r.total)}"><strong>${r.resolved} of ${r.total} resolved (${r.percent} percent)${r.so_far?' so far':''}</strong></p>${bar}<p class="rs-buckets">${['resolved','unresolved','error','pending'].map(k=>`<span data-bucket="${k}">${k}: ${(b[k]||[]).length}${(b[k]||[]).length?' ('+esc(b[k].join(', '))+')':''}</span>`).join(' &middot; ')}</p><div class="table-wrap rs-table-wrap"><table class="data-table rs-table"><thead><tr><th>Repair task</th><th>Bucket</th><th>Case base</th><th>Reproducer</th><th>Selected or latest attempt</th><th>Fix</th><th>Outcome</th><th>Gates</th><th>Verify</th></tr></thead><tbody>${rows}</tbody></table></div></div></section>`;
}
function summaryLine(run){
  const v=Object.entries(run.verify_counts||{}).map(([k,n])=>`${n} ${k}`).join(', ')||'none';
  return `<p class="rs-summary">Verify states: ${esc(v)} &middot; tamper catches: ${esc(run.tamper_catches||0)} &middot; audit flags: ${esc(run.audit_flags||0)}</p>`;
}
function headlineGrid(run){
  const b=run.budget||{};const cell=(k,v,key)=>`<div class="rs-fact"${src(run.sources,key)}><span>${esc(k)}</span><strong>${v}</strong></div>`;
  const metric=(run.metrics||[])[0];
  const champ=run.mode==='repair'?'':cell('Champion',esc(metric&&typeof run.champion?.metrics?.[metric.id]==='number'?fmt(run.champion.metrics[metric.id]):'not measured')+` <small>${esc(shortSha(run.champion?.commit))}</small>`,'baseline')+
    cell('Baseline',esc(metric&&typeof run.baseline?.metrics?.[metric.id]==='number'?fmt(run.baseline.metrics[metric.id]):'baseline not measured'),'baseline');
  const spent=`${esc(fmt(b.usd_spent))} USD of ${esc(has(b.usd_cap)?fmt(b.usd_cap):'no cap')}${b.unpriced?` <small>(${esc(b.unpriced)} launches unpriced)</small>`:''}`;
  return `<div class="rs-facts">${cell('Round',`${esc(run.round??0)} / ${esc(run.rounds_n??'?')}`,'rounds_n')}${cell('Slots',esc(run.slots_k??'?'),'slots_k')}${champ}${cell('Targets',esc((run.targets||[]).map(t=>t.metric+' '+fmt(t.value)).join(', ')||'none'),'targets')}${cell('Spend',spent,'budget')}${cell('Project budget',esc(b.posture||'not read'),'budget.posture')}${cell('Op',esc(run.op||'no op recorded'),'op')}${cell('Level',esc(run.level||'not recorded'),'level')}</div>`;
}
function recordPanel(run){
  const holds=(run.holds||[]).map(h=>`<li>${chip(h.gate||'hold',holdStatus(h)==='open'?'attention':'quiet')} event ${esc(h.id)} ${esc(h.code||'')}: ${esc(h.detail||'')} &middot; ${holdState(h)}</li>`).join('');
  const grants=(run.grants||[]).map(g=>`<li>${esc(g.gate)} grant, decision ${code(g.decision)}${g.decision_recorded?'':' (decision not found)'}${g.confirmed?' ('+esc(g.confirmed)+')':''}</li>`).join('');
  const stops=(run.stops||[]).map(s=>`<li>stop by ${code(s.session||'?')} at ${esc(when(s.ts))}${s.final?' (final)':''}: ${esc(s.reason||'no reason')}</li>`).join('');
  return `<section class="panel rs-panel"><div class="panel-head"><div><h2>Holds, grants and report</h2><p>Owner decisions and the run's report.</p></div></div><div class="panel-body"><h3${src(run.sources,'holds')}>Holds</h3>${holds?`<ul class="rs-list">${holds}</ul>`:'<p class="muted">none</p>'}<h3${src(run.sources,'grants')}>Grants</h3>${grants?`<ul class="rs-list">${grants}</ul>`:'<p class="muted">none</p>'}${stops?`<h3${src(run.sources,'stop')}>Stops</h3><ul class="rs-list">${stops}</ul>`:''}<h3${src(run.sources,'report')}>Report</h3><p>${run.report?libraryLink(run.report.path,'Open the run report')+` <small>${esc(shortSha(run.report.sha256))}</small>`:'no report yet'}</p></div></section>`;
}
function runPage(data,params,now){
  const run=data.run;
  const control=run.control?(run.control_failed?chip('control failed','danger'):chip(run.control_label||'control','quiet')):'';
  return `<div class="page-heading rs-heading"><div><span class="eyebrow">RESEARCH RUN</span><h1>${esc(run.run)} ${runChip(run.state)} ${control}</h1><p${src(run.sources,'runbook')}>${esc(run.mode||'mode not recorded')} run on ${code(run.runbook||'no runbook recorded')}, started ${esc(when(run.started_at))}, via ${esc(run.via||'not recorded')}.</p></div><a class="button" href="#research">All runs</a></div>`+
    progressPanel(run,params)+championPanel(run,params)+candidateReader(run,params)+stateBanner(run,now,params)+repairPanel(run,params)+evidenceVisuals(run,params)+candidateFlow(run,params)+lineagePanel(run,params)+disclosure(run.run+':technical','Full attempt evidence',summaryLine(run)+attemptTable(run,params))+disclosure(run.run+':settings','Run settings and decisions',headlineGrid(run)+recordPanel(run));
}
export function renderResearchPage(data,params=new URLSearchParams(),now=Date.now()){
  if(!(params instanceof URLSearchParams))params=new URLSearchParams(params||{});
  if(!data)return `<div class="error-box"><h2>research runs unavailable</h2><p>The research view could not be read.</p></div>`;
  if(data.run)return runPage(data,params,now);
  const all=data.runs||[],q=(params.get('q')||'').toLowerCase(),kind=params.get('kind')||'',mode=params.get('mode')||'';
  const runs=all.filter(r=>(!q||[r.run,r.runbook,r.state].join(' ').toLowerCase().includes(q))&&(!kind||(kind==='control'?r.control:!r.control))&&(!mode||r.mode===mode));
  const head=`<div class="page-heading"><div><h1>Research runs</h1><p>Follow the rounds, compare candidates and open the evidence behind each decision.</p></div></div>`;
  if(!all.length)return head+`<div class="empty rs-empty"><h3>No research run is recorded</h3><p>Start one with <code>/autoresearch</code> or <code>bin/alpaca research start --runbook PATH</code>.</p></div>`;
  const selector=(name,label,choices,value)=>`<label>${label}<select class="filter-select" data-filter="${name}">${choices.map(([v,l])=>`<option value="${v}"${v===value?' selected':''}>${l}</option>`).join('')}</select></label>`;
  const filters=`<div class="rs-filters"><label>Find run<input class="filter-input" type="search" data-filter="q" value="${esc(params.get('q')||'')}" placeholder="Run, runbook or state"></label>${selector('kind','Run type',[['','All run types'],['research','Research'],['control','Control']],kind)}${selector('mode','Mode',[['','All modes'],['improve','Improve'],['repair','Repair']],mode)}<span class="muted">${runs.length} of ${all.length} runs</span></div>`;
  const groups=[['In progress',r=>!r.control&&['starting','running','stopping'].includes(r.state)],['Needs attention',r=>!r.control&&!ENDED.includes(r.state)&&!['starting','running','stopping'].includes(r.state)],['Past runs',r=>!r.control&&ENDED.includes(r.state)],['Control runs',r=>!!r.control]];
  const body=groups.map(([title,test])=>{const list=runs.filter(test);if(!list.length)return '';return `<section class="rs-run-group"><h2>${title} <span class="muted">${list.length}</span></h2>${title==='Control runs'?'<p class="rs-note">Baseline and control checks are separate from real research runs.</p>':''}<div class="rs-cards rs-cards-page">${list.map(r=>runCard(r)).join('')}</div></section>`;}).join('');
  return head+filters+(body||'<div class="empty">'+orb('breathing',32)+'<h2>No matching runs</h2><p>Clear the search or choose all run types and modes.</p><a class="button" href="#research">Clear filters</a></div>');
}
// Module state survives the hub's automatic remount. Keys include run and logical round.
const chartMounts=new Map();
let chartObserver=null;
function resizeChart(el){
  const mount=chartMounts.get(el);
  if(!mount)return;
  if(!el.isConnected){chartObserver?.unobserve(el);chartMounts.delete(el);return;}
  const width=Math.floor(el.getBoundingClientRect().width);
  if(!width||width===mount.width)return;
  mount.width=width;
  const focused=el.contains(document.activeElement)?document.activeElement.closest('a')?.getAttribute('href'):null;
  const {chart,bands,xCount,...options}=mount.data;
  const template=document.createElement('template');
  template.innerHTML=metricChart(chart,bands,xCount,{...options,params:new URLSearchParams(options.params),width});
  el.innerHTML=template.content.firstElementChild.innerHTML;
  if(focused)[...el.querySelectorAll('a')].find(a=>a.getAttribute('href')===focused)?.focus({preventScroll:true});
}
export function bindResearch(root){
  if(!root?.querySelectorAll)return true;
  for(const el of chartMounts.keys())if(!el.isConnected){chartObserver?.unobserve(el);chartMounts.delete(el);}
  if(!chartObserver&&typeof ResizeObserver!=='undefined')chartObserver=new ResizeObserver(entries=>entries.forEach(e=>resizeChart(e.target)));
  root.querySelectorAll('[data-rs-chart]').forEach(el=>{
    if(chartMounts.has(el))return;
    chartMounts.set(el,{data:JSON.parse(el.dataset.rsChart),width:null});
    resizeChart(el);chartObserver?.observe(el);
  });
  root.querySelectorAll('[data-rs-disclosure]').forEach(el=>{if(boundDisclosures.has(el))return;boundDisclosures.add(el);el.addEventListener('toggle',()=>{
    if(!el.isConnected)return;
    disclosures.set(el.dataset.rsDisclosure,el.open);
    if(disclosures.size>500)disclosures.delete(disclosures.keys().next().value);
  });});
  const reader=root.querySelector('[data-candidate-reader]');
  const key=reader?new URLSearchParams(location.hash.split('?')[1]||'').get('run')+':'+reader.dataset.candidateReader:null;
  if(reader&&key!==focusedReader)reader.focus({preventScroll:true});
  focusedReader=key;
  return true;
}
