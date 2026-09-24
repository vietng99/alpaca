// Patch a container's children to match new markup, keeping every node that can stay.
// Nodes of the same type and tag are updated in place: text through nodeValue, attributes synced.
// What the reader set is left alone: the open state of <details>, and the value of form controls.
// A live refresh therefore keeps scroll position, focus and open panels.
const last=new WeakMap();
// Full render: replace the content and remember the markup, so an identical patch is skipped.
export function render(el,html){el.innerHTML=html;last.set(el,html);}
// Live render: returns true when the DOM changed. Identical markup is not even parsed.
export function patch(el,html){if(!el)return false;if(last.get(el)===html)return false;const t=document.createElement('template');t.innerHTML=html;const changed=morphChildren(el,t.content);last.set(el,html);return changed;}
const sameKind=(a,b)=>a.nodeType===b.nodeType&&(a.nodeType!==1||(a.localName===b.localName&&a.namespaceURI===b.namespaceURI));
// Children with a data-key (or an id) are matched by key before position: a session row or a
// switcher option stays the same node however many rows start above it.
const keyOf=n=>n.nodeType===1?(n.getAttribute('data-key')||n.id||null):null;
// Attributes that hold the reader's state rather than the page's.
const readerOwned=(el,name)=>(el.localName==='details'&&name==='open')||(el.localName==='option'&&name==='selected')||(['input','textarea','select'].includes(el.localName)&&(name==='value'||name==='checked'));
const READER='details,input,select,textarea,option';
const pageAttrs=el=>[...el.attributes].filter(x=>!readerOwned(el,x.name)).map(x=>(x.namespaceURI||'')+' '+x.name+'='+x.value).sort().join('\n');
function alike(a,b){if(!sameKind(a,b))return false;if(a.nodeType!==1)return a.nodeValue===b.nodeValue;if(pageAttrs(a)!==pageAttrs(b))return false;const ca=a.childNodes,cb=b.childNodes;if(ca.length!==cb.length)return false;for(let i=0;i<ca.length;i++)if(!alike(ca[i],cb[i]))return false;return true;}
// Equal as the page renders it: a <details> the reader opened still matches its closed markup.
const same=(a,b)=>a.isEqualNode(b)||(a.nodeType===1&&b.nodeType===1&&(a.matches(READER)||!!a.querySelector(READER))&&alike(a,b));
function syncAttributes(a,b){let changed=false;for(const attr of [...a.attributes]){if(readerOwned(a,attr.name))continue;if(!b.hasAttributeNS(attr.namespaceURI,attr.localName)){a.removeAttributeNS(attr.namespaceURI,attr.localName);changed=true;}}for(const attr of [...b.attributes]){if(readerOwned(a,attr.name))continue;if(a.getAttributeNS(attr.namespaceURI,attr.localName)!==attr.value){a.setAttributeNS(attr.namespaceURI,attr.name,attr.value);changed=true;}}return changed;}
function morphNode(a,b){if(a.nodeType===3||a.nodeType===8){if(a.nodeValue===b.nodeValue)return false;a.nodeValue=b.nodeValue;return true;}if(a.nodeType!==1)return false;const changed=syncAttributes(a,b);if(a.localName==='textarea')return changed;return morphChildren(a,b)||changed;}
const LOOK=8;   // how far ahead an unkeyed insert or removal is looked for
// Walk both child lists once. Keyed children are matched by key. An unkeyed child equal to the
// current one is kept; a run of up to LOOK inserted or removed siblings is detected by looking
// ahead, so the nodes after it (and their reader state) keep their identity; otherwise the node is
// updated in place, or replaced when its type differs.
function morphChildren(from,to){
 let changed=false,cur=from.firstChild;const next=[...to.childNodes],wanted=new Set(),byKey=new Map(),used=new Set();
 for(const n of next){const k=keyOf(n);if(k)wanted.add(k);}
 for(let n=from.firstChild;n;n=n.nextSibling){const k=keyOf(n);if(k&&!byKey.has(k))byKey.set(k,n);}
 const drop=()=>{const gone=cur;cur=cur.nextSibling;from.removeChild(gone);changed=true;};
 const spare=n=>{const k=keyOf(n);return Boolean(k)&&(!wanted.has(k)||used.has(k)||byKey.get(k)!==n);};
 for(let i=0;i<next.length;i++){
  const node=next[i],k=keyOf(node);
  while(cur&&spare(cur))drop();
  if(k){const old=byKey.get(k);if(old&&!used.has(k)&&sameKind(old,node)){used.add(k);if(old===cur)cur=cur.nextSibling;else{from.insertBefore(old,cur);changed=true;}if(morphNode(old,node))changed=true;}else{from.insertBefore(node,cur);changed=true;}continue;}
  if(!cur){from.appendChild(node);changed=true;continue;}
  if(keyOf(cur)){from.insertBefore(node,cur);changed=true;continue;}
  if(same(cur,node)){cur=cur.nextSibling;continue;}
  let ahead=cur.nextSibling,steps=1;while(ahead&&steps<LOOK&&!keyOf(ahead)&&!same(ahead,node)){ahead=ahead.nextSibling;steps++;}
  if(ahead&&!keyOf(ahead)&&same(ahead,node)){while(cur!==ahead)drop();cur=cur.nextSibling;continue;}
  let j=i+1;while(j<next.length&&j-i<=LOOK&&!keyOf(next[j])&&!same(cur,next[j]))j++;
  if(j<next.length&&j-i<=LOOK&&!keyOf(next[j])&&same(cur,next[j])){for(;i<j;i++)from.insertBefore(next[i],cur);cur=cur.nextSibling;changed=true;continue;}
  if(sameKind(cur,node)){if(morphNode(cur,node))changed=true;cur=cur.nextSibling;}
  else{const gone=cur;cur=cur.nextSibling;from.replaceChild(node,gone);changed=true;}
 }
 while(cur)drop();
 return changed;
}
