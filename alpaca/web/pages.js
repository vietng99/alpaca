// The Studio page registry (pages.json, its "about" says what each key means). serve.py writes an inline
// copy into the hub page, so the first paint makes no extra request; outside a browser (the Node tests)
// the file itself is read.
const inline=globalThis.document?.getElementById?.('studio-pages');
export const REGISTRY=JSON.parse(inline?inline.textContent:globalThis.process?.getBuiltinModule?.('node:fs').readFileSync(new URL('./pages.json',import.meta.url),'utf8')??'{"shell":{},"pages":[]}');
// Every page in nav order, and each page by id.
export const PAGES=[...REGISTRY.pages].sort((a,b)=>a.nav-b.nav);
export const PAGE=Object.fromEntries(PAGES.map(p=>[p.id,p]));
