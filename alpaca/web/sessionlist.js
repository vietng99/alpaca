// Sessions index: filter by agent and sort. An agent is read from the session's recorded
// operator (claude, claude-code, codex, ...). Cost and tokens are main conversation plus subagents,
// the figures the row shows; a session with no measured value sorts last either way. With a filter
// on, the summary figures are summed over the sessions shown, as the server sums them over all.
export const AGENT_CHOICES=[['','All agents'],['claude','Claude only'],['codex','Codex only'],['other','Other or unknown']];
export const SORT_CHOICES=[['newest','Newest first'],['oldest','Oldest first'],['active','Recently active'],['cost-desc','Most cost'],['cost-asc','Least cost'],['tokens-desc','Most tokens'],['responses-desc','Most responses']];
const agentOf=s=>{const o=String(s.operator||'').toLowerCase();return o.includes('codex')?'codex':o.includes('claude')?'claude':'other';};
const famCost=s=>s.family?.estimated_cost_usd??s.summary?.estimated_cost_usd??null,famTokens=s=>s.family?.total_tokens??s.summary?.tokens?.total_tokens??null,famResponses=s=>s.family?.responses??s.summary?.responses??null;
const stamp=v=>{const t=Date.parse(v||'');return Number.isFinite(t)?t:null;};
const SORTS={newest:s=>stamp(s.first),oldest:s=>stamp(s.first),active:s=>stamp(s.last)??stamp(s.first),'cost-desc':famCost,'cost-asc':famCost,'tokens-desc':famTokens,'responses-desc':famResponses};
// A range of local days (#sessions?from=YYYY-MM-DD&to=YYYY-MM-DD). The days are the viewer's
// own: start is local midnight of From, end local midnight after To, both as epoch milliseconds,
// so the server never guesses a zone. A day after today is moved to today (the note says so); a
// From after To, or a date that is not a real day, is refused with `error` and no range applies.
// One bound alone: no To means up to today, no From means the To day only.
const DAY=/^(\d{4})-(\d{2})-(\d{2})$/;
const localDay=k=>{const m=DAY.exec(k);if(!m)return null;const d=new Date(+m[1],m[2]-1,+m[3]);return d.getFullYear()===+m[1]&&d.getMonth()===m[2]-1&&d.getDate()===+m[3]?d:null;};
export function dayRange(from,to,today){
 from=from||'';to=to||'';if(!from&&!to)return null;
 if((from&&!localDay(from))||(to&&!localDay(to)))return {from,to,error:'A date in the range is not a real day. Pick the days again or clear the range.'};
 const moved=from>today||to>today;if(from>today)from=today;if(to>today)to=today;
 if(!to)to=today;if(!from)from=to;
 if(from>to)return {from,to,error:'The From date is after the To date, so no date range is applied. Pick a From date on or before the To date.'};
 const end=localDay(to);end.setDate(end.getDate()+1);
 return {from,to,start:+localDay(from),end:+end,note:moved?'Dates after today were moved to today.':''};
}
// A session is in the range when its activity (first to last recorded time) overlaps it.
const inRange=(s,r)=>{const a=stamp(s.first),b=stamp(s.last)??a;return a!=null&&a<r.end&&b>=r.start;};
export function sessionList(index,q='',agent='',sort='newest',range=null){
 const all=index.sessions||[],key=SORTS[sort]||SORTS.newest,up=sort==='oldest'||sort==='cost-asc',days=range&&range.start!=null?range:null;
 const shown=all.filter(s=>(!q||JSON.stringify([s.title,s.sid,s.operator]).toLowerCase().includes(q))&&(!agent||agentOf(s)===agent)&&(!days||inRange(s,days)));
 const sorted=shown.map((s,i)=>({s,i,v:key(s)})).sort((a,b)=>(a.v==null)-(b.v==null)||(a.v==null?0:up?a.v-b.v:b.v-a.v)||a.i-b.i).map(x=>x.s);
 if(!q&&!agent&&!days)return {...index,sessions:sorted};
 const add=(a,b)=>b==null?a:(a??0)+b,t={...index.totals};
 Object.assign(t,{estimated_cost_usd:null,total_tokens:null,responses:0,costed_responses:0,measured_sessions:0,partial_sessions:0,subagents:0,subagents_measured:0});
 for(const s of sorted){t.estimated_cost_usd=add(t.estimated_cost_usd,famCost(s));t.total_tokens=add(t.total_tokens,famTokens(s));t.responses+=famResponses(s)||0;t.costed_responses+=s.family?.costed_responses??s.summary?.costed_responses??0;if(s.summary?.usage_responses&&s.summary?.tokens?.total_tokens!=null)t.measured_sessions++;if(!s.coverage?.complete)t.partial_sessions++;t.subagents+=s.subagents?.count||0;t.subagents_measured+=s.subagents?.measured||0;}
 return {...index,sessions:sorted,totals:t,filtered:{shown:sorted.length,of:all.length}};
}
