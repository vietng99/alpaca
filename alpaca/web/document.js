// Reading aids for the hub's document reader. Pure string helpers: the hub mounts their output and
// wires the section buttons, so a section jump scrolls the reader and never changes the hub route.
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// The section a proof report opens with for a reader: At a glance in a format 2 report, else its
// Result. Text inside code fences is skipped; an unwritten placeholder shows nothing.
export function proofOverview(text){
 if(!/^# Proof report /m.test(String(text||'')))return '';
 let selected='',fenced=false;const body=[];
 for(const line of String(text).split('\n')){
  if(/^\s*```/.test(line)){fenced=!fenced;continue;}
  if(fenced)continue;
  if(/^## /.test(line)){
   if(selected)break;
   const name=line.slice(3).trim();
   if(name==='At a glance'||name==='Result')selected=name;
  }else if(selected)body.push(line);
 }
 const prose=body.join('\n').trim();
 if(!selected||!prose||prose.startsWith('TODO(agent):'))return '';
 return `<section class="proof-overview"><span class="eyebrow">${esc(selected)}</span><p>${esc(prose)}</p></section>`;
}

// One button per rendered section heading, [{id, title}]; nothing for a document with fewer than two.
export function documentNavigation(headings){
 const list=(headings||[]).filter(h=>h&&h.id);
 if(list.length<2)return '';
 return `<nav class="document-nav" aria-label="Sections in this document">${list.map(h=>`<button type="button" data-document-section="${esc(h.id)}">${esc(h.title)}</button>`).join('')}</nav>`;
}
