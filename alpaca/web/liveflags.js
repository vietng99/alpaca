// Live update rules for the hub, kept pure so they can be tested without a browser.
// The server pushes one revision per surface ({data, lib, board, pricing}); the hub keeps one
// dirty flag per client cache. These tables decide which flags a pushed revision sets and which
// flags each page reads, so a moving board no longer rebuilds the Sessions page.
import {PAGES} from './pages.js';
export const FLAGS=['overview','runs','sessions','analytics','library'];
// Server revision key -> client caches it makes stale. An unknown key marks everything.
export const SURFACES={board:['overview','runs'],data:['sessions','analytics'],pricing:['sessions','analytics'],lib:['library']};
// Page -> caches it renders from (its registry `needs`, pages.json), and so clears when it renders; a flag
// a page never clears would refresh it on every idle moment. The live log page follows its own stream and
// never refreshes here.
export const ROUTE_NEEDS=Object.fromEntries(PAGES.map(p=>[p.id,p.needs]));
// Retired page addresses and where they live now. The mission map became a view of Roadmap, so
// '#mission?node=x' answers '#roadmap?view=mission&node=x'; any other address returns null (no change).
export function routeAlias(hash){const [name,query='']=String(hash||'').replace(/^#/,'').split('?');if(name!=='mission')return null;const p=new URLSearchParams(query);p.delete('view');return '#roadmap?'+new URLSearchParams([['view','mission'],...p]);}
// The caches one page reads. A session's Work & reports tab also renders tasks and messages from the overview.
export function needs(route,params={}){const base=ROUTE_NEEDS[route]||FLAGS;return route==='sessions'&&params.sid&&params.view==='report'?[...base,'overview']:[...base];}
export function parseRev(text){try{const value=JSON.parse(text);return value&&typeof value==='object'&&!Array.isArray(value)?value:null;}catch{return null;}}
// Flags to set when the revision blob moves from prev to next; an unreadable blob marks every flag.
export function changedFlags(prev,next){if(prev===next)return [];const a=parseRev(prev),b=parseRev(next);if(!a||!b)return [...FLAGS];const hit=new Set();for(const key of new Set([...Object.keys(a),...Object.keys(b)])){if(a[key]!==b[key])for(const flag of SURFACES[key]||FLAGS)hit.add(flag);}return FLAGS.filter(flag=>hit.has(flag));}
const asSet=dirty=>dirty instanceof Set?dirty:new Set(Array.isArray(dirty)?dirty:Object.keys(dirty||{}).filter(key=>dirty[key]));
// True when the page reads a cache that is stale; used for the "New activity available" label.
export function dependsOn(route,dirty,params={}){const set=asSet(dirty);return needs(route,params).some(flag=>set.has(flag));}
// True when the page should refresh itself now. The conversation tab is being read, so it keeps its
// content and offers the refresh button instead.
export function autoRefresh(route,dirty,params={}){if(route==='sessions'&&params.sid&&params.view==='conversation')return false;return dependsOn(route,dirty,params);}
// Dirty flags with a generation per flag. A fetch notes the generation before it starts and clears
// the flag only if no newer revision marked it meanwhile, so a revision that lands mid-fetch (whose
// payload the fetch may predate) is never swallowed.
export function createDirty(){const dirty=Object.fromEntries(FLAGS.map(flag=>[flag,false])),gen=Object.fromEntries(FLAGS.map(flag=>[flag,0]));return {dirty,mark(flags){for(const flag of flags){dirty[flag]=true;gen[flag]=(gen[flag]||0)+1;}},seen(flag){return gen[flag]||0;},settle(flag,seen){if((gen[flag]||0)===seen)dirty[flag]=false;}};}

// Transport, page freshness and capture coverage must remain distinguishable.
export function connectionHealth(h) {
  const page=h.projection?.state||'unknown',capture=h.observability||{};
  const gaps=capture.status!=='ok';
  const label=page==='fresh'?(gaps?'Live - capture gaps':'Live'):({stale:'Stale',starting:'Preparing',error:'Page error'}[page]||'Status unknown');
  const missing=capture.coverage?.missing_children?.length||0;
  const backlog=capture.backlog?.bytes;
  return {label,error:page!=='fresh'||gaps,title:`Page ${page}; capture ${capture.status||'unavailable'}; collector ${capture.collector?.status||'unknown'}${backlog==null?'':'; '+Intl.NumberFormat().format(backlog)+' bytes pending'}${missing?'; '+missing+' missing child sources':''}`};
}
