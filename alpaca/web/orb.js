// <alpaca-orb>: the hub's loading and working indicator, a dotted thought-orb drawn by the
// vendored thinking-orbs engine (vendor/thinking-orbs.js, MIT, Jakub Antalik).
//
//   <alpaca-orb state="searching" size="32"></alpaca-orb>
//
// state: working | searching | solving | listening | connecting | weaving | composing | breathing | shaping
// size:  20 (inline text), 32 (compact) or 64 (avatar); each is a tuned preset, not a scale factor.
// label: accessible name; defaults to the state's verb.
// The dots take the element's CSS `color` (near dots full color, far dots fade toward the page), so
// a stylesheet picks the ink; --orb-px shows the preset smaller or larger. The canvas lives in a shadow root and the host gets no attributes
// of its own, so morph.js patches see the same markup they rendered and never strip the canvas.
// All orbs share one clock and one animation frame loop; an orb off screen is skipped, and
// prefers-reduced-motion draws one still frame.
import {MODE_FRAMES, resolvePreset, STATE_TO_MODE, paintFrame} from './vendor/thinking-orbs.js';

const SIZES = [20, 32, 64];
const LABELS = {working: 'Working', searching: 'Searching', solving: 'Solving', listening: 'Listening',
  connecting: 'Connecting', weaving: 'Weaving', composing: 'Composing', breathing: 'Thinking', shaping: 'Shaping'};
// The element is browser-only; the views also load under node --test, where only orb() is used.
if (typeof customElements !== 'undefined' && !customElements.get('alpaca-orb')) define();

function define() {
  const motion = window.matchMedia?.('(prefers-reduced-motion: reduce)');
  const reduced = () => Boolean(motion?.matches);
  const dark = () => document.documentElement.dataset.theme === 'dark';
  const live = new Set();
  let raf = 0;

  function rgb(color) {
    const m = /^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)/i.exec(color || '');
    return m ? {r: +m[1], g: +m[2], b: +m[3]} : undefined;
  }

  function tick() {
    raf = 0;
    const t = performance.now() / 1000;
    for (const orb of live) if (orb.visible) orb.draw(t);
    if (live.size && !reduced()) raf = requestAnimationFrame(tick);
  }
  const wake = () => { if (!raf && live.size && !reduced()) raf = requestAnimationFrame(tick); };

  const seen = typeof IntersectionObserver === 'function'
    ? new IntersectionObserver(entries => { for (const e of entries) e.target.visible = e.isIntersecting; wake(); })
    : null;

  // Theme or motion changes repaint every orb, the still ones included.
  const refresh = () => { for (const orb of live) orb.setup(); wake(); };
  window.addEventListener('alpaca:themechange', refresh);
  motion?.addEventListener?.('change', refresh);

  class AlpacaOrb extends HTMLElement {
    static observedAttributes = ['state', 'size', 'label'];

    constructor() {
      super();
      const root = this.attachShadow({mode: 'open'});
      root.innerHTML = '<style>:host{display:inline-block;flex:none;vertical-align:middle;line-height:0}canvas{display:block}</style><canvas role="img"></canvas>';
      this.canvas = root.querySelector('canvas');
      this.visible = !seen;
    }

    connectedCallback() { this.setup(); live.add(this); seen?.observe(this); wake(); }
    disconnectedCallback() { live.delete(this); seen?.unobserve(this); }
    attributeChangedCallback() { if (this.isConnected) this.setup(); }

    setup() {
      const state = STATE_TO_MODE[this.getAttribute('state')] ? this.getAttribute('state') : 'working';
      const want = +this.getAttribute('size') || 20;
      const size = SIZES.reduce((a, b) => Math.abs(b - want) < Math.abs(a - want) ? b : a);
      const {mode, speed, opts} = resolvePreset(state, size);
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      Object.assign(this, {size, dpr, speed, opts, frame: MODE_FRAMES[mode], dark: dark(), tint: rgb(getComputedStyle(this).color)});
      this.canvas.style.width = this.canvas.style.height = `var(--orb-px, ${size}px)`;
      this.canvas.width = this.canvas.height = Math.round(size * dpr);
      this.canvas.setAttribute('aria-label', this.getAttribute('label') || LABELS[state]);
      this.draw(reduced() ? 0.6 / speed : performance.now() / 1000);
    }

    draw(t) {
      const ctx = this.canvas.getContext('2d');
      if (!ctx) return;
      ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      ctx.clearRect(0, 0, this.size, this.size);
      paintFrame(ctx, this.frame(this.size, t * this.speed, this.opts), this.dark, this.tint);
    }
  }

  customElements.define('alpaca-orb', AlpacaOrb);
}

// Markup helper for the template-string views.
export const orb = (state = 'working', size = 20, label = '') =>
  `<alpaca-orb state="${state}" size="${size}"${label ? ` label="${label}"` : ''}></alpaca-orb>`;
