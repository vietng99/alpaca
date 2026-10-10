// A sample project page (docs/studio.md 4.0). Everything it uses comes in ctx; it reads its own profile
// route and keeps a count of stop() calls on globalThis so a test can see the hub called it.
export async function render(ctx){const data=await ctx.api('/hub/bench.json');return ctx.heading('Bench','Readings from the bench profile route','','PROJECT')+`<div class="bench-page" data-ctx-version="${ctx.esc(ctx.v)}">${ctx.panel('Readings',`<ul class="bench-list">${data.items.map(i=>`<li>${ctx.esc(i.name)}: ${ctx.esc(i.value)}</li>`).join('')}</ul><p><button class="button" data-bench-ping>Ping</button></p><p id="bench-out">${ctx.esc(ctx.page.label)} ready</p>`)}</div>`;}
export function bind(ctx){document.querySelector('[data-bench-ping]')?.addEventListener('click',()=>ctx.patch(`${ctx.esc(ctx.page.label)} pinged`,document.getElementById('bench-out')));}
export function stop(){globalThis.benchStops=(globalThis.benchStops||0)+1;}
