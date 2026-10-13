// Studio tooltips (op-012 t-598): every tooltip is the Studio's own styled tip (tips.css), never the
// browser's. An element's title attribute, or an SVG element's <title> child, is moved into data-tip
// the moment the pointer or keyboard focus reaches it, before the browser would show its own, and the
// tip shows after a short wait beside the element, kept inside the window. An element that had only
// its title as a name keeps it as aria-label. Pointer down, scroll, Escape or leaving hides the tip.
export const TIP_DELAY_MS = 320;

// The text an element carries for a tip, taking it out of title or <title> on the way.
export function takeTip(el) {
  if (!el || el.nodeType !== 1) return '';
  const title = el.getAttribute('title');
  if (title != null) {
    if (title.trim()) {
      el.setAttribute('data-tip', title);
      if (!el.hasAttribute('aria-label') && !el.hasAttribute('aria-labelledby') && !el.textContent.trim()) el.setAttribute('aria-label', title);
    }
    el.removeAttribute('title');
  }
  const svgTitle = el.namespaceURI === 'http://www.w3.org/2000/svg' && [...el.children].find(c => c.tagName.toLowerCase() === 'title');
  if (svgTitle) { if (svgTitle.textContent.trim()) el.setAttribute('data-tip', svgTitle.textContent.trim()); svgTitle.remove(); }
  return el.getAttribute('data-tip') || '';
}
// The nearest element at or above `node` that carries a tip.
export function tipOwner(node) {
  for (let el = node; el && el.nodeType === 1; el = el.parentElement || el.parentNode) {
    if (el.hasAttribute('title') || el.hasAttribute('data-tip')) return el;
    if (el.namespaceURI === 'http://www.w3.org/2000/svg' && [...el.children].some(c => c.tagName.toLowerCase() === 'title')) return el;
  }
  return null;
}

export function installTips(doc = document) {
  if (doc.__tips) return doc.__tips;
  const tip = doc.createElement('div');
  tip.className = 'studio-tip'; tip.setAttribute('role', 'tooltip'); tip.id = 'studio-tip'; tip.hidden = true;
  doc.body.append(tip);
  let owner = null, timer = 0;
  const hide = () => { clearTimeout(timer); timer = 0; tip.hidden = true; tip.classList.remove('is-on'); if (owner && owner.getAttribute('aria-describedby') === 'studio-tip') owner.removeAttribute('aria-describedby'); owner = null; };
  const place = el => {
    const r = el.getBoundingClientRect(), w = doc.documentElement.clientWidth, h = doc.documentElement.clientHeight;
    tip.style.left = '0px'; tip.style.top = '0px';
    const t = tip.getBoundingClientRect(), gap = 6;
    const below = r.top - t.height - gap < 4;
    const top = below ? Math.min(h - t.height - 4, r.bottom + gap) : r.top - t.height - gap;
    const left = Math.max(6, Math.min(w - t.width - 6, r.left + r.width / 2 - t.width / 2));
    tip.style.left = Math.round(left) + 'px'; tip.style.top = Math.round(top) + 'px';
  };
  const show = el => {
    if (!el.isConnected || owner !== el) return;
    const text = takeTip(el);
    if (!text) return;
    tip.textContent = text; tip.hidden = false; place(el);
    if (!el.hasAttribute('aria-describedby')) el.setAttribute('aria-describedby', 'studio-tip');
    requestAnimationFrame(() => tip.classList.add('is-on'));
  };
  const reach = (el, wait) => {
    if (el === owner) return;
    hide();
    if (!el) return;
    owner = el;
    takeTip(el);                                   // at once, so the browser's own tooltip never starts
    timer = setTimeout(() => show(el), wait);
  };
  doc.addEventListener('pointerover', e => reach(tipOwner(e.target), TIP_DELAY_MS), true);
  doc.addEventListener('pointerout', e => { if (owner && !(e.relatedTarget && owner.contains(e.relatedTarget))) hide(); }, true);
  doc.addEventListener('focusin', e => { const el = tipOwner(e.target); if (el && el.matches?.(':focus-visible')) reach(el, 120); }, true);
  doc.addEventListener('focusout', hide, true);
  doc.addEventListener('pointerdown', hide, true);
  doc.addEventListener('scroll', hide, true);
  doc.addEventListener('keydown', e => { if (e.key === 'Escape') hide(); }, true);
  doc.__tips = {hide, tip};
  return doc.__tips;
}
