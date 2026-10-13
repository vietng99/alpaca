// Border beams (beam.css): the ring markup and the off-screen pause. A ring that is not on screen
// stops turning, so only the beams in view cost anything. Templates add BEAM_RING as the last child
// of a .beam host. A live refresh (morph.js) can add a ring, turn a span into one or one into a
// span, and flip a card between live and alert (a ring that has no animation while red), so the
// observer follows class changes as well as new nodes and re-applies the visibility it last saw.
export const BEAM_RING = '<span class="beam-ring" aria-hidden="true"><i></i></span>';
const shown = new WeakMap();       // ring -> true/false as the visibility observer last saw it
const watched = new WeakSet();
const see = typeof IntersectionObserver === 'function' ? new IntersectionObserver(entries => {
  for (const e of entries) { shown.set(e.target, e.isIntersecting); apply(e.target); }
}) : null;
// Only the comet of a real ring is touched; a ring off screen holds still, one in view turns.
function apply(ring) {
  const comet = ring.matches('.beam-ring') ? ring.querySelector(':scope > i') : null;
  if (!comet || !shown.has(ring)) return;
  for (const a of comet.getAnimations()) shown.get(ring) ? a.play() : a.pause();
}
function follow(el) {
  if (el.matches('.beam-ring')) { if (!watched.has(el)) { watched.add(el); see.observe(el); } apply(el); }
  else if (watched.has(el)) { watched.delete(el); see.unobserve(el); shown.delete(el); }
  for (const r of el.querySelectorAll(':scope > .beam-ring')) follow(r);   // a host that changed state
}
const sweep = node => {
  if (node.nodeType !== 1) return;
  follow(node);
  for (const r of node.querySelectorAll('.beam-ring')) follow(r);
};
if (see && typeof document !== 'undefined') {
  new MutationObserver(list => {
    for (const m of list) { if (m.type === 'attributes') follow(m.target); else m.addedNodes.forEach(sweep); }
  }).observe(document.documentElement, {childList: true, subtree: true, attributes: true, attributeFilter: ['class']});
  sweep(document.documentElement);
  matchMedia('(prefers-reduced-motion: reduce)').addEventListener('change', () => document.querySelectorAll('.beam-ring').forEach(apply));
}
