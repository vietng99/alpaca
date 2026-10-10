// The pulse orb (at the right end of the topbar): one orb on every page. It cycles thinking states
// while work runs and breathes when nothing does; the ring around it pulses stronger and faster as
// more tasks are in flight and jobs run at once; its hue follows the local time of day around the
// colour wheel. The look is cosmetic; the caption under it states the counts it is drawn from.
import {inflightCount} from './inflight.js';

// Thinking states the orb walks through while work runs, one every CYCLE_MS.
export const CYCLE = ['searching', 'solving', 'connecting', 'weaving', 'composing', 'shaping', 'working'];
const CYCLE_MS = 7000;

// The hue for a local time: the full wheel once a day, deep blue at midnight, magenta at dawn,
// red mid-morning, gold at noon, green in the afternoon, teal and cyan in the evening. The clock is
// read in 5-minute slots, so the hue moves 1.25 degrees per step (288 steps a day).
export const STEP_MIN = 5;
export function hueAt(date = new Date()) {
  const slot = Math.floor((date.getHours() * 60 + date.getMinutes()) / STEP_MIN);
  return +((230 + slot * STEP_MIN / 1440 * 360) % 360).toFixed(2);
}

// A step fades from the shown hue to the new one over FADE_MS, the short way round the wheel.
const FADE_MS = 30000, FADE_TICK_MS = 1500;
export function fadePath(from, to, steps = FADE_MS / FADE_TICK_MS) {
  const delta = ((to - from + 540) % 360) - 180;
  return Array.from({length: steps}, (_, i) => +((from + delta * (i + 1) / steps + 360) % 360).toFixed(2));
}
const PERIODS = [[5, 'Night'], [8, 'Dawn'], [12, 'Morning'], [17, 'Afternoon'], [21, 'Evening'], [24, 'Night']];
export const periodAt = (date = new Date()) => PERIODS.find(([end]) => date.getHours() < end)[1];

// Pulse strength from concurrency: n = in-flight tasks + running jobs. Zero keeps a faint, slow
// breath of two waves; each concurrent item makes the ripples reach further, glow brighter and run a
// little quicker, adds a wave (up to four) and more particles, up to eight items.
export function pulseFor(n) {
  const k = Math.min(Math.max(n, 0), 8);
  return {scale: +(1.18 + 0.05 * k).toFixed(2), alpha: +(0.16 + 0.08 * k).toFixed(2), seconds: +(6 - 0.35 * k).toFixed(2),
    waves: k === 0 ? 2 : k < 4 ? 3 : 4, sparks: k === 0 ? 4 : Math.min(6 + 2 * k, 18), reach: 40 + 3 * k};
}

// Spark directions spread by the golden angle, so any count covers the circle evenly.
export const sparkAngles = count => Array.from({length: count}, (_, i) => Math.round((i * 137.508) % 360));

export function caption(tasks, jobs) {
  if (!tasks && !jobs) return 'Idle';
  const part = (n, one) => `${n} ${one}${n === 1 ? '' : 's'}`;
  return [tasks ? part(tasks, 'task') + ' in flight' : '', jobs ? part(jobs, 'job') + ' running' : ''].filter(Boolean).join(' / ');
}

let busy = false, step = 0, cycleTimer = 0;

// Called whenever the overview is read (hub.js updateChrome).
export function updatePulse(o) {
  const el = document.getElementById('pulse-orb');
  if (!el || !o) return;
  const tasks = inflightCount(o.tasks), jobs = (o.active_jobs || []).length, n = tasks + jobs;
  const p = pulseFor(n);
  el.style.setProperty('--pulse-scale', p.scale);
  el.style.setProperty('--pulse-alpha', p.alpha);
  el.style.setProperty('--pulse-s', p.seconds + 's');
  el.style.setProperty('--waves', p.waves);
  el.style.setProperty('--reach', p.reach + 'px');
  el.querySelectorAll('.pulse-ring').forEach((ring, i) => { ring.hidden = i >= p.waves; });
  const sparks = el.querySelector('.pulse-sparks');
  if (sparks && sparks.childElementCount !== p.sparks)
    sparks.innerHTML = sparkAngles(p.sparks).map((a, i) => `<i style="--a:${a}deg;--d:${(i * 0.37 % 1).toFixed(2)}"></i>`).join('');
  el.dataset.load = String(Math.min(n, 8));
  el.querySelector('.pulse-caption').textContent = caption(tasks, jobs);
  el.title = `${caption(tasks, jobs)}. ${periodAt()} colour; the pulse grows with concurrent work. Click to reload this view.`;
  busy = n > 0;
  setState();
}

function setState() {
  const orb = document.querySelector('#pulse-orb alpaca-orb');
  if (!orb || crazyEnd) return;
  const want = busy ? CYCLE[step % CYCLE.length] : 'breathing';
  if (orb.getAttribute('state') !== want) orb.setAttribute('state', want);
}

// A click on the orb: the orb shapeshifts. The same click also reloads the view
// (hub.js binds that), so the shapeshift doubles as the reload's feedback. For WILD_MS its outline
// becomes a moving blob, it squashes, stretches and twists, its rings take the same blob outline, and it steps through the
// orb's particle shapes every SHAPE_STEP_MS; then everything eases back to the calm circle over
// SETTLE_MS, and the workspace state comes back early in the settle, where the motion hides the swap.
// The colour moves only a little. pulse.js sets the CSS variables (--wob-*) frame by frame and every
// one of them returns to its rest value, so nothing snaps when it ends. Reduced motion gets one shape
// change and no movement.
export const SHAPES = ['working', 'searching', 'solving', 'listening', 'connecting', 'weaving', 'composing', 'shaping', 'breathing'];
export const RISE_MS = 300, WILD_MS = 2600, SETTLE_MS = 1600, SHAPE_STEP_MS = 420;
const smooth = x => x * x * (3 - 2 * x);
// Strength of the effect t ms after the click: eases in, holds at 1, eases out to exactly 0.
export function wildness(t) {
  if (!(t > 0) || t >= WILD_MS + SETTLE_MS) return 0;
  if (t < RISE_MS) return smooth(t / RISE_MS);
  if (t < WILD_MS) return 1;
  return 1 - smooth((t - WILD_MS) / SETTLE_MS);
}
// The blob outline s seconds in at strength e: eight corner radii drifting around 50%.
export function blobAt(s, e) {
  const r = k => (50 + e * 22 * Math.sin(s * (2.1 + k * 0.7) + k * 1.9)).toFixed(1) + '%';
  return [0, 1, 2, 3].map(r).join(' ') + ' / ' + [4, 5, 6, 7].map(r).join(' ');
}
const WOB = ['--wob-r', '--wob-sx', '--wob-sy', '--wob-blob', '--wob-hue'];
let wildRaf = 0, wildStart = 0, crazyEnd = 0, shapeAt = 0;
export function goCrazy() {
  const el = document.getElementById('pulse-orb'), orb = el?.querySelector('alpaca-orb');
  if (!orb) return;
  let i = SHAPES.indexOf(orb.getAttribute('state'));
  const next = () => { i = (i + 1 + Math.floor(Math.random() * (SHAPES.length - 1))) % SHAPES.length; orb.setAttribute('state', SHAPES[i]); };
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) {
    crazyEnd = 1; next();
    setTimeout(() => { crazyEnd = 0; setState(); }, WILD_MS);
    return;
  }
  const now = performance.now();
  // A click while it is still moving picks up from the strength it has now instead of restarting.
  const active = wildRaf !== 0, e0 = active ? wildness(now - wildStart) : 0;
  wildStart = now - RISE_MS * e0;
  if (!active) { shapeAt = now; next(); }
  crazyEnd = 1;
  if (active) return;
  const frame = ts => {
    const t = ts - wildStart, e = wildness(t), s = t / 1000;
    el.style.setProperty('--wob-r', (e * 16 * Math.sin(s * 5.3)).toFixed(2) + 'deg');
    el.style.setProperty('--wob-sx', (1 + e * 0.16 * Math.sin(s * 6.7)).toFixed(3));
    el.style.setProperty('--wob-sy', (1 + e * 0.16 * Math.cos(s * 5.9)).toFixed(3));
    el.style.setProperty('--wob-blob', blobAt(s, e));
    el.style.setProperty('--wob-hue', (e * 24 * Math.sin(s * 2.2)).toFixed(1) + 'deg');
    if (t < WILD_MS && ts - shapeAt >= SHAPE_STEP_MS) { shapeAt = ts; next(); }
    if (crazyEnd && t >= WILD_MS + SETTLE_MS * 0.3) { crazyEnd = 0; setState(); }
    if (t >= WILD_MS + SETTLE_MS) { WOB.forEach(v => el.style.removeProperty(v)); wildRaf = 0; return; }
    wildRaf = requestAnimationFrame(frame);
  };
  wildRaf = requestAnimationFrame(frame);
}

let shown = null, fadeTimer = 0;
function setHue(el, hue) {
  shown = hue;
  el.style.setProperty('--pulse-hue', hue);
  // The orb reads its ink when it sets up; touching its size makes it read the new colour.
  const orb = el.querySelector('alpaca-orb');
  if (orb) orb.setAttribute('size', orb.getAttribute('size'));
}

// First paint sets the colour at once; later slots fade into the next colour.
function paint() {
  const el = document.getElementById('pulse-orb');
  if (!el) return;
  el.dataset.period = periodAt().toLowerCase();
  const target = hueAt();
  if (shown === null) return setHue(el, target);
  if (target === shown) return;
  clearInterval(fadeTimer);
  const path = fadePath(shown, target);
  fadeTimer = setInterval(() => { setHue(el, path.shift()); if (!path.length) clearInterval(fadeTimer); }, FADE_TICK_MS);
}

export function startPulse() {
  const el = document.getElementById('pulse-orb');
  if (el && !el.dataset.crazyBound) {
    el.dataset.crazyBound = '1';
    el.addEventListener('click', goCrazy);
    el.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); goCrazy(); } });
  }
  paint();
  setInterval(paint, 30000);
  clearInterval(cycleTimer);
  cycleTimer = setInterval(() => { if (busy) { step++; setState(); } }, CYCLE_MS);
}
