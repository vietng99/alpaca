// The Brain mode of the vault (evgraph.js loads it on first use): the evidence graph as a 3D brain.
// Every record item is a neuron placed in the region its column maps to (REGION): operations in the
// thalamus at the core, phase doors down the brainstem, tasks through the frontal lobe, decisions at the
// frontal pole, review cards in the cingulate, sealed proofs through the parietal lobe, the four kinds
// of evidence in the occipital and temporal lobes, sessions in the basal ganglia, messages in the
// language areas, wiki assertions in the hippocampus, claims in the amygdala and wiki sources in the
// cerebellum. A lobe's neurons fill its volume from the cortex inward (t-594), so the brain has depth.
// Every link is a fibre bowed through the white matter toward the centre, and a firing link sends a
// pulse along its fibre that makes the neuron it reaches flash in and out. A wireframe of cross-section
// contours draws the hemispheres, cerebellum and brainstem; it carries no data. Positions come from a
// hash of each item's id, so the same record draws the same brain every time.
//
// Drag turns the brain, Shift-drag, right-drag or two fingers move it, the wheel or a pinch zooms,
// double-click sends a shockwave (and turns a neuron hit squarely to the front); arrows or WASD move it,
// Shift+arrows turn, + and - zoom, 0 resets, space toggles
// auto-rotate, whose speed is the Turn slider (st.turn, percent of TURN_RATE). Selection, find, the sliders and the legend are evgraph.js's own (shared state `st`).
// Dependency-free: a hand-rolled perspective projection on a 2D canvas.
import {offset, BEAT_MS, TRAVEL, FLASH, waveFront, drawWaves} from './evgraph.js';

export const REGION = {op: 'thalamus', phase: 'stem', task: 'frontal', decision: 'pole', review: 'cingulate',
  proof: 'parietal', 'ev-event': 'temporal-left', 'ev-log': 'occipital', 'ev-code': 'parieto-occipital',
  'ev-file': 'temporal-right', session: 'basal', message: 'language', assertion: 'hippocampus', claim: 'amygdala',
  source: 'cerebellum'};
const HEMI = {cx: 0.25, cy: 0.1, out: 0.45, inn: 0.23, ry: 0.5, rz: 0.84};   // one hemisphere: flat inner face, +z is the front
const DIST = 3.4;                                                 // camera distance in brain units
const VIEW = {yaw: -1.05, pitch: 0.2};                            // the first view: the left side, a little from the front
const SCALE = 0.6;                                                // the brain's size against the shorter side of the view
const TURN_RATE = 0.00016;                                        // auto-rotate at Turn 100, in radians per millisecond
const NODE = 2.6, BANDS = 8;                                      // a dot's size in px at unit depth; depth bands for brightness
const SOMA_PX = 3;                                              // a neuron's soma radius in px at unit depth, before its link-count growth
const VARIANTS = 4, SPRITE = 96, SOMA = 0.36;                     // neuron sprite shapes per colour (lit chains), sprite size, soma per half sprite

// A small seeded generator per item: the same id always gives the same position.
function seeded(text) {
  let h = 2166136261;
  for (let k = 0; k < text.length; k++) { h ^= text.charCodeAt(k); h = Math.imul(h, 16777619); }
  let s = h >>> 0;
  return () => { s = (s + 0x6D2B79F5) >>> 0; let t = s; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}
// Folds of the cortex: a small radial offset that rises and falls over the surface.
const gyri = (x, y, z) => 0.045 * Math.sin(11 * x + 3 * Math.sin(5 * z)) * Math.sin(9 * y + 13 * z) + 0.022 * Math.sin(23 * z + 7 * y + 4 * x);
function direction(rnd) {
  const w = 2 * rnd() - 1, a = 2 * Math.PI * rnd(), r = Math.sqrt(1 - w * w);
  return [r * Math.cos(a), w, r * Math.sin(a)];          // [outward, up, front] on the unit sphere
}
// The cortical patch of each region, on one hemisphere's unit directions (dx > 0 faces outward).
const PATCH = {
  pole: d => d[2] > 0.8,
  frontal: d => d[2] > 0.3 && d[2] <= 0.8 && d[1] > -0.25,
  motor: d => d[2] > 0.1 && d[2] <= 0.3 && d[1] > 0.15,
  parietal: d => d[2] > -0.5 && d[2] <= 0.1 && d[1] > 0.35,
  'parieto-occipital': d => d[2] > -0.62 && d[2] <= 0.1 && d[1] > -0.12 && d[1] <= 0.35 && d[0] > 0.15,
  occipital: d => d[2] <= -0.55,
  'temporal-left': d => d[1] <= -0.12 && d[2] > -0.55 && d[2] <= 0.32 && d[0] > 0.25,
  'temporal-right': d => d[1] <= -0.12 && d[2] > -0.55 && d[2] <= 0.32 && d[0] > 0.25,
  language: d => d[0] > 0.55 && d[1] > -0.32 && d[1] < 0.18 && d[2] > -0.25 && d[2] < 0.5,
  cingulate: d => d[0] < -0.62 && d[1] > 0.05 && d[2] > -0.3 && d[2] < 0.65,
};
const SIDE = {'temporal-left': -1, 'temporal-right': 1, language: -1};
const smooth = (a, b, v) => { const t = Math.max(0, Math.min(1, (v - a) / (b - a))); return t * t * (3 - 2 * t); };
// A point on one hemisphere's cortex for a unit direction d: the folds, the lateral fissure above the
// temporal lobe, the flat floor under the frontal lobe, the occipital floor over the cerebellum and the
// temporal lobe hanging lower at the side.
export function surface(d, side, shell) {
  const [dx, dy, dz] = d;
  let r = shell * (1 + gyri(dx, dy, dz));
  const fissure = dy - (-0.08 + 0.3 * dz);
  if (dx > 0.2 && dz > -0.3 && dz < 0.6 && Math.abs(fissure) < 0.07) r *= 0.9 + 0.1 * Math.abs(fissure) / 0.07;
  let ys = 1;
  if (dy < 0) {
    const front = smooth(0.15, 0.5, dz), back = smooth(-0.2, -0.5, dz);
    ys = 1 - 0.45 * front - 0.42 * back + 0.16 * smooth(0.05, 0.4, dx) * (1 - front) * (1 - back);
  }
  return [side * (HEMI.cx + dx * (dx > 0 ? HEMI.out : HEMI.inn) * r), HEMI.cy + dy * HEMI.ry * r * ys, dz * HEMI.rz * r];
}
function cortex(region, rnd) {
  const test = PATCH[region] || (() => true);
  let d = direction(rnd);
  for (let k = 0; k < 600 && !test(d); k++) d = direction(rnd);
  const side = SIDE[region] || (rnd() < 0.5 ? -1 : 1);
  // the lobe's volume from the cortex inward: most neurons near the surface, some deep inside
  const shell = region === 'cingulate' ? 0.72 + 0.16 * rnd() : region === 'language' ? 0.82 + 0.18 * rnd() : 1 - 0.58 * Math.pow(rnd(), 1.5);
  return surface(d, side, shell);
}
function inside(cx, cy, cz, rx, ry, rz, rnd) {
  let x, y, z;
  do { x = 2 * rnd() - 1; y = 2 * rnd() - 1; z = 2 * rnd() - 1; } while (x * x + y * y + z * z > 1);
  return [cx + x * rx, cy + y * ry, cz + z * rz];
}
export function place(region, id) {
  const rnd = seeded(String(id));
  const side = rnd() < 0.5 ? -1 : 1;
  if (region === 'thalamus') return inside(side * 0.11, 0, -0.06, 0.1, 0.1, 0.18, rnd);
  if (region === 'basal') return rnd() < 0.55                 // caudate arching over the thalamus, putamen beside it
    ? (() => { const u = rnd(), a = Math.PI * (0.15 + 0.85 * u); return inside(side * 0.17, 0.02 + 0.2 * Math.sin(a), 0.05 + 0.32 * Math.cos(a), 0.05, 0.05, 0.06, rnd); })()
    : inside(side * 0.29, -0.02, 0.06, 0.06, 0.11, 0.17, rnd);
  if (region === 'amygdala') return inside(side * 0.27, -0.24, 0.3, 0.07, 0.065, 0.075, rnd);
  if (region === 'hippocampus') {                       // a curved tube along the floor of each temporal lobe
    const u = rnd(), a = 2 * Math.PI * rnd(), rr = 0.07 * Math.sqrt(rnd());
    return [side * (0.3 + 0.03 * Math.sin(Math.PI * u)) + rr * Math.cos(a), -0.2 - 0.06 * Math.sin(Math.PI * u) + rr * Math.sin(a), 0.22 - 0.6 * u];
  }
  if (region === 'stem') {
    const u = rnd(), a = 2 * Math.PI * rnd(), rr = 0.11 * (1 - 0.3 * u) * Math.sqrt(rnd());
    return [rr * Math.cos(a), -0.24 - 0.72 * u, -0.16 - 0.16 * u + rr * Math.sin(a)];
  }
  if (region === 'cerebellum') {                        // the surface of the cerebellum, with its fine folia
    const d = direction(rnd), shell = 1 - 0.6 * Math.pow(rnd(), 1.4);
    return cerebellum(d, shell);
  }
  return cortex(region, rnd);
}
function cerebellum(d, shell) {
  const f = 1 + 0.05 * Math.sin(48 * d[1] + 6 * d[0]);
  return [d[0] * 0.46 * shell * f, -0.4 + d[1] * 0.2 * shell * f, -0.56 + d[2] * 0.27 * shell * f];
}
// The wireframe: closed contours over both hemispheres (coronal slices front to back and axial slices
// bottom to top), the cerebellum's folia and rings down the brainstem, as flat [x, y, z, ...] lines.
export function wireframe() {
  const lines = [], ring = (count, at) => { const out = []; for (let k = 0; k <= count; k++) out.push(...at(2 * Math.PI * k / count)); return out; };
  for (const side of [-1, 1]) {
    for (let dz = -0.94; dz <= 0.95; dz += 0.11) { const q = Math.sqrt(1 - dz * dz); lines.push(ring(72, a => surface([Math.cos(a) * q, Math.sin(a) * q, dz], side, 1))); }
    for (let dy = -0.75; dy <= 0.9; dy += 0.165) { const q = Math.sqrt(1 - dy * dy); lines.push(ring(80, a => surface([Math.cos(a) * q, dy, Math.sin(a) * q], side, 1))); }
  }
  for (let dy = -0.9; dy <= 0.91; dy += 0.15) { const q = Math.sqrt(1 - dy * dy); lines.push(ring(56, a => cerebellum([Math.cos(a) * q, dy, Math.sin(a) * q], 1))); }
  for (let u = 0; u <= 1.001; u += 0.2) lines.push(ring(24, a => { const rr = 0.11 * (1 - 0.3 * u); return [rr * Math.cos(a), -0.24 - 0.72 * u, -0.16 - 0.16 * u + rr * Math.sin(a)]; }));
  return lines.map(l => Float32Array.from(l));
}

// Colour helpers for the neuron sprites: an rgb triple for any CSS colour, a mix toward another.
export function rgbOf(color, doc = globalThis.document) {
  const c = doc.createElement('canvas'); c.width = c.height = 1;
  const g = c.getContext('2d'); g.fillStyle = '#888'; g.fillStyle = color; g.fillRect(0, 0, 1, 1);
  const d = g.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2]];
}
const mix = (a, b, t) => a.map((v, k) => Math.round(v + (b[k] - v) * t));
const rgba = (c, alpha = 1) => `rgba(${c[0]},${c[1]},${c[2]},${alpha})`;
// One neuron, drawn once per colour and shape variant (t-596): tapered dendrites that fork, a soma with
// an uneven membrane lit from the upper left (a highlight, the body colour, a darker rim), a thin
// membrane line and a bright nucleus spark. The variants differ in their arms and a slight tint.
export function neuronSprite(rgb, variant, dark, doc = globalThis.document) {
  const c = doc.createElement('canvas'); c.width = c.height = SPRITE;
  c.small = null;
  const g = c.getContext('2d'), h = SPRITE / 2, r = h * SOMA, rnd = seeded('neuron:' + variant);
  const tint = variant === 1 ? mix(rgb, [255, 255, 255], 0.14) : variant === 2 ? mix(rgb, [0, 0, 0], 0.12) : variant === 3 ? mix(rgb, [255, 255, 255], 0.06) : rgb;
  const rim = mix(tint, [0, 0, 0], dark ? 0.55 : 0.42), light = mix(tint, [255, 255, 255], 0.5), arm = mix(tint, rim, 0.3);
  const limb = (x, y, angle, len, width, depth) => {
    const ux = Math.cos(angle), uy = Math.sin(angle), px = -uy, py = ux, bend = (rnd() - 0.5) * 0.6 * len;
    const tx = x + ux * len, ty = y + uy * len, mx = x + ux * len * 0.5 + px * bend, my = y + uy * len * 0.5 + py * bend;
    g.beginPath(); g.moveTo(x + px * width, y + py * width);
    g.quadraticCurveTo(mx + px * width * 0.45, my + py * width * 0.45, tx, ty);
    g.quadraticCurveTo(mx - px * width * 0.45, my - py * width * 0.45, x - px * width, y - py * width);
    g.closePath(); g.fill();
    if (depth < 1 && len > h * 0.28) {                       // one fork past the middle of the arm
      const fx = x + ux * len * 0.55 + px * bend * 0.5, fy = y + uy * len * 0.55 + py * bend * 0.5;
      limb(fx, fy, angle + (rnd() < 0.5 ? -1 : 1) * (0.45 + 0.4 * rnd()), len * (0.4 + 0.2 * rnd()), width * 0.55, depth + 1);
    }
  };
  g.fillStyle = rgba(arm, dark ? 0.85 : 0.8);
  const arms = 4 + Math.floor(rnd() * 4);
  for (let k = 0; k < arms; k++) {
    const angle = 2 * Math.PI * k / arms + (rnd() - 0.5) * 0.9;
    limb(h + Math.cos(angle) * r * 0.8, h + Math.sin(angle) * r * 0.8, angle, h * (0.42 + 0.5 * rnd()), r * (0.2 + 0.12 * rnd()), 0);
  }
  const body = g.createRadialGradient(h - r * 0.38, h - r * 0.42, r * 0.08, h, h, r * 1.08);
  body.addColorStop(0, rgba(light)); body.addColorStop(0.42, rgba(tint)); body.addColorStop(1, rgba(rim));
  const phase = rnd() * 6.3;
  g.beginPath();
  for (let k = 0; k <= 32; k++) {
    const a = 2 * Math.PI * k / 32, rr = r * (1 + 0.09 * Math.sin(3 * a + phase) + 0.05 * Math.sin(5 * a + 2 * phase));
    k ? g.lineTo(h + rr * Math.cos(a), h + rr * Math.sin(a)) : g.moveTo(h + rr * Math.cos(a), h + rr * Math.sin(a));
  }
  g.closePath(); g.fillStyle = body; g.fill();
  g.strokeStyle = rgba(rim, 0.7); g.lineWidth = 1.2; g.stroke();
  const spark = g.createRadialGradient(h - r * 0.32, h - r * 0.36, 0, h - r * 0.32, h - r * 0.36, r * 0.42);
  spark.addColorStop(0, 'rgba(255,255,255,0.6)'); spark.addColorStop(1, 'rgba(255,255,255,0)');
  g.fillStyle = spark; g.fillRect(0, 0, SPRITE, SPRITE);
  // a pre-scaled copy for neurons drawn small: scaling 96 px down to a few px every frame costs more
  const small = doc.createElement('canvas'); small.width = small.height = SPRITE / 4;
  const sg = small.getContext('2d'); sg.imageSmoothingQuality = 'high'; sg.drawImage(c, 0, 0, SPRITE / 4, SPRITE / 4);
  c.small = small;
  return c;
}

// Mount the brain on `canvas`. `st` is evgraph.js's state (colours, selection, sliders, hidden columns);
// `hooks` reach back into it: select(i), hover(i, x, y), kick(), showMotion().
export function mountBrain({canvas, graph, st, hooks}) {
  const ctx = canvas.getContext('2d');
  const n = graph.nodes.length, m = graph.links.length;
  const P = new Float32Array(n * 3), C = new Float32Array(m * 6);
  graph.nodes.forEach((node, i) => { const p = place(REGION[graph.columns[node.c].key] || 'parietal', node.id); P.set(p, i * 3); });
  // an axon (t-601, after the H01 cortex map): a long thin thread that wanders on its way, two control
  // points a third and two thirds along, drawn toward the centre and pushed off the straight line by a
  // seeded wobble, so the web reads as tangled threads rather than arcs
  graph.links.forEach(([a, b], li) => {
    const rnd = seeded('axon:' + li), len = Math.hypot(P[a * 3] - P[b * 3], P[a * 3 + 1] - P[b * 3 + 1], P[a * 3 + 2] - P[b * 3 + 2]);
    for (const [slot, t] of [[0, 0.33], [3, 0.67]]) for (let k = 0; k < 3; k++)
      C[li * 6 + slot + k] = (P[a * 3 + k] * (1 - t) + P[b * 3 + k] * t) * 0.62 + (rnd() - 0.5) * 0.5 * len;
  });
  const frameLines = wireframe();
  // neurites: one to three thin branches from each soma, a short seeded random walk in 3D that bends as
  // it goes (more branches and longer ones for busier items); NS[k] is where neurite k starts in NP
  const NP = [], NS = [], NC = [];
  const X = new Float32Array(n), Y = new Float32Array(n), Z = new Float32Array(n), S = new Float32Array(n);
  const view = {yaw: VIEW.yaw, pitch: VIEW.pitch, zoom: 1, panX: 0, panY: 0, spin: !st.reduce, ease: null, last: 0, w: 0, h: 0, dpr: 1, flashSprites: true};
  let layer = document.createElement('canvas'), spare = document.createElement('canvas'), job = null;
  const threads = document.createElement('canvas');      // the axons at rest, at half resolution (t-607)
  // a finished layer is copied once into `still`, an image of its pixels: drawing the layer itself scaled,
  // as a pan or zoom does every frame, made the browser replay every stroke behind it (t-607)
  const still = document.createElement('canvas'), stillHalf = document.createElement('canvas');
  function freeze(lq) {
    if (still.width !== layer.width || still.height !== layer.height) { still.width = layer.width; still.height = layer.height; }
    const s2 = still.getContext('2d'); s2.setTransform(1, 0, 0, 1, 0, 0); s2.globalCompositeOperation = 'copy'; s2.globalAlpha = 1; s2.drawImage(layer, 0, 0);
    // and a copy at no more than half the canvas, which a pan or zoom scales every frame
    const k = Math.min(1, 0.5 * canvas.width / Math.max(1, layer.width)), hw = Math.max(1, Math.round(layer.width * k)), hh = Math.max(1, Math.round(layer.height * k));
    if (stillHalf.width !== hw || stillHalf.height !== hh) { stillHalf.width = hw; stillHalf.height = hh; }
    const s3 = stillHalf.getContext('2d'); s3.setTransform(1, 0, 0, 1, 0, 0); s3.globalCompositeOperation = 'copy'; s3.drawImage(still, 0, 0, hw, hh);
    stillHalf.q = lq * k;
  }
  const SLICE_MS = 9;                                      // the full layer's paint budget per frame (t-607)
  let layerKey = '', sprites = [], neurons = [], spriteKey = '', pointers = new Map(), gesture = null, clicked = false;   // a direct click selects without turning the brain
  // a neuron grows with its links (1x to 1.9x, by log count), and keeps one shape variant by its id
  const deg = new Float32Array(n), grow = new Float32Array(n), shape = new Uint8Array(n);
  graph.links.forEach(([a, b]) => { deg[a]++; deg[b]++; });
  let most = 1; for (let i = 0; i < n; i++) if (deg[i] > most) most = deg[i];
  const topLog = Math.log1p(most);
  graph.nodes.forEach((node, i) => { grow[i] = 1 + 0.9 * Math.log1p(deg[i]) / topLog; shape[i] = Math.floor(seeded(node.id + ':shape')() * VARIANTS); });
  graph.nodes.forEach((node, i) => {
    const rnd = seeded(node.id + ':neurite'), arms = 1 + (grow[i] > 1.25) + (grow[i] > 1.6);
    for (let k = 0; k < arms; k++) {
      let [dx, dy, dz] = direction(rnd), x = P[i * 3], y = P[i * 3 + 1], z = P[i * 3 + 2];
      NS.push(NP.length / 3); NC.push(i); NP.push(x, y, z);
      const stepLen = 0.017 * grow[i] * (0.7 + 0.6 * rnd());
      for (let s2 = 0; s2 < 5; s2++) {
        const [ex, ey, ez] = direction(rnd); dx += ex * 0.7; dy += ey * 0.7; dz += ez * 0.7;
        const l = Math.hypot(dx, dy, dz) || 1; dx /= l; dy /= l; dz /= l;
        x += dx * stepLen; y += dy * stepLen; z += dz * stepLen; NP.push(x, y, z);
      }
    }
  });
  NS.push(NP.length / 3);
  const NPF = Float32Array.from(NP), NX = new Float32Array(NP.length / 3), NY = new Float32Array(NP.length / 3);
  const BC = () => st.bcolors || st.colors, TEXT = () => st.btext || '#dfe7f5';
  const LUX = () => Math.max(0.1, Math.min(2, (st.glow ?? 70) / 100));   // the Brightness slider (t-603), 1 at 100 percent
  let lastInput = -1e9, settleTimer = 0, layerBase = '', layerView = null;
  const keys = new Set();                                  // held pan keys (arrows, WASD)
  // while the brain turns, pans or zooms its still layer is painted at half resolution, and once it rests
  // (SETTLE_MS after the last input) again at full resolution
  const SETTLE_MS = 600;
  const busy = now => spinning() || keys.size > 0 || !!gesture || now - lastInput < SETTLE_MS;
  const touched = () => { lastInput = performance.now(); clearTimeout(settleTimer); settleTimer = setTimeout(() => hooks.kick(), SETTLE_MS + 20); };

  function resize(w, h, dpr) {
    dpr = Math.min(dpr, 1.5);                                // the brain caps its pixel density: four times the pixels on a 2x screen bought no detail
    view.w = w; view.h = h; view.dpr = dpr;
    canvas.width = w * dpr; canvas.height = h * dpr; canvas.style.width = w + 'px'; canvas.style.height = h + 'px';
    hooks.kick();
  }
  // a neuron sprite copied once at a fixed pixel size: sizes in steps, cached by colour, shape and size
  const SIZES = [6, 8, 10, 12, 14, 16, 19, 22, 26, 30, 36, 44, 54, 66, 80, 96];
  let sizedCache = new Map();
  function sized(ci, v, want) {
    let size = SIZES[SIZES.length - 1];
    for (const s0 of SIZES) if (s0 >= want) { size = s0; break; }
    const key = ci * 1000 + v * 100 + size;
    let bmp = sizedCache.get(key);
    if (!bmp) {
      bmp = document.createElement('canvas'); bmp.width = bmp.height = size;
      const bg = bmp.getContext('2d'); bg.imageSmoothingQuality = 'high'; bg.drawImage(neurons[ci][v], 0, 0, size, size);
      sizedCache.set(key, bmp);
    }
    return bmp;
  }
  function makeSprites() {
    sizedCache = new Map();
    spriteKey = (st.bcolors || st.colors).join() + st.dark;
    neurons = (st.bcolors || st.colors).map(color => { const rgb = rgbOf(color); return Array.from({length: VARIANTS}, (_, v) => neuronSprite(rgb, v, !!st.dark)); });
    sprites = (st.bcolors || st.colors).map(color => {
      const s = document.createElement('canvas'); s.width = s.height = 64;
      const g = s.getContext('2d'), grad = g.createRadialGradient(32, 32, 0, 32, 32, 32);
      grad.addColorStop(0, color); grad.addColorStop(0.25, color); grad.addColorStop(1, 'rgba(0,0,0,0)');
      g.fillStyle = grad; g.globalAlpha = 1; g.fillRect(0, 0, 64, 64);
      return s;
    });
  }
  // rotate (yaw about y, then pitch about x) and project; returns [screen x, screen y, depth, scale]
  function frame() {
    const cy = Math.cos(view.yaw), sy = Math.sin(view.yaw), cp = Math.cos(view.pitch), sp = Math.sin(view.pitch);
    const scale = Math.min(view.w, view.h) * SCALE * view.zoom, ox = view.w / 2 + view.panX, oy = view.h / 2 + view.panY;
    return (x, y, z) => {
      const x1 = x * cy - z * sy, z1 = x * sy + z * cy, y2 = y * cp - z1 * sp, z2 = y * sp + z1 * cp;
      const f = DIST / (DIST - z2);
      return [ox + x1 * scale * f, oy - y2 * scale * f, z2, f];
    };
  }
  const fog = z => Math.max(0.18, Math.min(1, 0.55 + z * 0.5));
  const live = i => !st.hidden.has(graph.nodes[i].c);
  const liveLink = li => live(graph.links[li][0]) && live(graph.links[li][1]);
  function fibre(li, proj) {
    const [a, b] = graph.links[li], o = li * 6, [c1x, c1y] = proj(C[o], C[o + 1], C[o + 2]), [c2x, c2y] = proj(C[o + 3], C[o + 4], C[o + 5]);
    return [X[a], Y[a], c1x, c1y, c2x, c2y, X[b], Y[b]];
  }
  function at(f, t) { const u = 1 - t, a = u * u * u, b = 3 * u * u * t, c = 3 * u * t * t, d = t * t * t; return [a * f[0] + b * f[2] + c * f[4] + d * f[6], a * f[1] + b * f[3] + c * f[5] + d * f[7]]; }
  const thread = (g, f) => { g.moveTo(f[0], f[1]); g.bezierCurveTo(f[2], f[3], f[4], f[5], f[6], f[7]); };

  function draw(t = 0) {
    if (spriteKey !== (st.bcolors || st.colors).join() + st.dark) makeSprites();
    step(t);
    const w = view.w, h = view.h, proj = frame(), glow = st.dark ? 'lighter' : 'source-over';
    for (let i = 0; i < n; i++) { const [sx, sy, z, f] = proj(P[i * 3], P[i * 3 + 1], P[i * 3 + 2]); X[i] = sx; Y[i] = sy; Z[i] = z; S[i] = f; }
    // a shockwave pushes the neurons it passes outward; they spring back once the front has gone by
    const now = performance.now(), waves = st.waves || [], size = Math.min(w, h);
    for (const wv of waves) {
      const {r, fade} = waveFront(wv, now, size), amp = 30 * fade * fade;
      if (amp < 0.3) continue;
      for (let i = 0; i < n; i++) {
        const dx = X[i] - wv.x, dy = Y[i] - wv.y, d = Math.hypot(dx, dy) || 1, u = (d - r) / 34;
        if (u < -3 || u > 3) continue;
        const push = amp * Math.exp(-u * u) * (1 - 0.6 * u);
        X[i] += dx / d * push; Y[i] += dy / d * push;
      }
    }
    const lit = st.lit, focus = st.focus.size > 0;
    // the still layer (background, wireframe, fibres, neurons) is painted again only when the view, the
    // selection, the highlight or the colours change; a still brain then costs only its pulses
    // t-605: while a pan or zoom is under way (the view is not turning) the finished layer is reused,
    // scaled and shifted to the new view (one blit a frame), and painted again once input rests
    const base = [view.yaw, view.pitch, w, h, view.dpr, st.sel, lit ? lit.nodes.size : -1, [...st.focus].join('.'), spriteKey, st.hidden.size, st.glow, waves.length ? now : 0].join();
    const g = ctx;
    g.setTransform(1, 0, 0, 1, 0, 0); g.globalCompositeOperation = 'source-over'; g.globalAlpha = 1;
    g.imageSmoothingEnabled = true;
    const reuse = layerView && layerBase === base && now - lastInput < SETTLE_MS && !spinning() && gesture?.kind !== 'turn';
    if (reuse) {
      const k = view.zoom / layerView.zoom, cx = w / 2, cy = h / 2, d = view.dpr, lq = stillHalf.q || layerView.q;
      g.fillStyle = st.brainBg; g.fillRect(0, 0, canvas.width, canvas.height);
      g.setTransform(k / lq, 0, 0, k / lq, d * (cx + view.panX - k * (cx + layerView.panX)), d * (cy + view.panY - k * (cy + layerView.panY)));
      g.drawImage(stillHalf, 0, 0);
      g.setTransform(1, 0, 0, 1, 0, 0);
    } else {
      // the layer for this view: a sketch painted now if the layer on hand is for another view, then at
      // rest the full layer, painted in slices into the spare canvas over the next frames and swapped in
      const viewKey = base + '|' + [view.zoom, view.panX, view.panY].join(), sketchKey = viewKey + '|0.5', fullKey = viewKey + '|1';
      if (layerKey !== sketchKey && layerKey !== fullKey) {
        job = null;
        paintLayer(proj, glow, lit, focus, 0.5); freeze(0.5); layerKey = sketchKey; layerBase = base; layerView = {zoom: view.zoom, panX: view.panX, panY: view.panY, q: 0.5};
      }
      if (!busy(now) && layerKey !== fullKey) {
        if (!job || job.key !== fullKey) job = {key: fullKey, steps: paintSteps(spare, proj, glow, lit, focus, 1, true)};
        deadline = performance.now() + SLICE_MS;
        if (job.steps.next().done) {
          [layer, spare] = [spare, layer]; freeze(1); layerKey = fullKey; layerBase = base; layerView = {zoom: view.zoom, panX: view.panX, panY: view.panY, q: 1}; job = null;
        } else hooks.kick();
      } else if (busy(now)) job = null;
      g.drawImage(still, 0, 0, still.width, still.height, 0, 0, canvas.width, canvas.height);
    }
    g.setTransform(view.dpr, 0, 0, view.dpr, 0, 0);
    for (const wv of waves) {                            // and the neurons on the front flare
      const {r, fade} = waveFront(wv, now, Math.min(w, h));
      g.globalCompositeOperation = glow;
      for (let i = 0; i < n; i++) {
        const d = Math.hypot(X[i] - wv.x, Y[i] - wv.y), q = Math.exp(-((d - r) ** 2) / 500) * fade;
        if (q < 0.1) continue;
        const rr = SOMA_PX * S[i] * grow[i] * 3; g.globalAlpha = 0.3 * q; g.drawImage(sprites[graph.nodes[i].c], X[i] - rr, Y[i] - rr, rr * 2, rr * 2);
      }
      g.globalCompositeOperation = 'source-over'; g.globalAlpha = 1;
    }
    if (animating()) pulses(lit ? [...lit.links].filter(liveLink) : (focus ? st.firing.filter(st.focused) : st.firing).filter(liveLink), t, proj, glow);
    if (lit) {
      g.globalCompositeOperation = glow;
      for (const i of lit.nodes) { if (!live(i)) continue; const r = 8 * S[i] * grow[i]; g.globalAlpha = 0.45 * fog(Z[i]); g.drawImage(sprites[graph.nodes[i].c], X[i] - r, Y[i] - r, r * 2, r * 2); }
      g.globalCompositeOperation = 'source-over'; g.globalAlpha = 1;
      for (const i of lit.nodes) { if (!live(i)) continue; const half = 1.5 * SOMA_PX * S[i] * grow[i] / SOMA; g.drawImage(neurons[graph.nodes[i].c][shape[i]], X[i] - half, Y[i] - half, half * 2, half * 2); }
      labels([...lit.nodes].filter(live));
    }
    g.globalCompositeOperation = 'source-over'; g.globalAlpha = 1; g.lineWidth = 1.4; g.strokeStyle = TEXT();
    for (const i of st.matches) { if (!live(i)) continue; g.beginPath(); g.arc(X[i], Y[i], 5 * S[i], 0, Math.PI * 2); g.stroke(); }
    if (st.sel != null && live(st.sel)) { g.lineWidth = 2; g.beginPath(); g.arc(X[st.sel], Y[st.sel], 8 * S[st.sel], 0, Math.PI * 2); g.stroke(); }
    if (st.hover != null && live(st.hover)) { g.lineWidth = 1.5; g.beginPath(); g.arc(X[st.hover], Y[st.hover], 6 * S[st.hover], 0, Math.PI * 2); g.stroke(); }
    if (waves.length) drawWaves(g, waves, now, Math.min(w, h), st.accent || TEXT());
    view.last = t;
  }
  // The still layer, after the H01 map of human cortex: a dark field, the wireframe, the axons as long
  // thin threads blended additively so crossings glow, then each soma's neurites and the somas themselves
  // as small bright points with a soft halo. `q` is the resolution (0.5 while moving).
  // t-607: the full layer is painted into the spare canvas in slices (paintSteps yields once a slice
  // passes its budget) and swapped in when done; a sketch is painted in one go.
  let deadline = Infinity;
  function paintLayer(proj, glow, lit, focus, q = 1) { for (const _ of paintSteps(layer, proj, glow, lit, focus, q, false)); }
  function* paintSteps(target, proj, glow, lit, focus, q, sliced) {
    const G = target.getContext('2d'), w = view.w, h = view.h, lw = Math.round(canvas.width * q), lh = Math.round(canvas.height * q), px = view.dpr * q;
    if (target.width !== lw || target.height !== lh) { target.width = lw; target.height = lh; }
    // sliced: the drawing calls are spread over frames within SLICE_MS each; the browser still draws the
    // pixels when the finished layer is first shown, which the half-resolution axons keep short
    let g = G;
    const due = () => sliced && performance.now() > deadline;
    const CHUNK = 1500;
    g.setTransform(px, 0, 0, px, 0, 0);
    g.globalCompositeOperation = 'source-over'; g.globalAlpha = 1;
    const bg = g.createRadialGradient(w / 2 + view.panX, h / 2 + view.panY, 0, w / 2, h / 2, Math.max(w, h) * 0.75);
    bg.addColorStop(0, st.brainBg2); bg.addColorStop(1, st.brainBg);
    g.fillStyle = bg; g.fillRect(0, 0, w, h);
    const near = outline(proj, g);
    const dim = lit ? 0.3 : focus ? 0.28 : 1, byCol = {};
    // while moving (q < 1) the layer is a lighter sketch: one axon in six as three straight strokes along
    // its curve, neurites only on the near side, square somas, no halos; at rest it is painted in full
    const sketch = q < 1, every = sketch ? 6 : 1;
    for (let li = 0; li < m; li += every) if (liveLink(li)) (byCol[graph.nodes[graph.links[li][0]].c] ||= []).push(li);
    // at rest the axons, thin and see-through, are drawn at half resolution on their own canvas and laid
    // over the field scaled up (t-607): a quarter of the pixels, the costliest part of a full paint
    const field = g, A = sketch ? null : threads.getContext('2d');
    if (A) {
      const tw = Math.max(1, Math.round(lw / 2)), th = Math.max(1, Math.round(lh / 2));
      if (threads.width !== tw || threads.height !== th) { threads.width = tw; threads.height = th; }
      A.setTransform(1, 0, 0, 1, 0, 0); A.globalCompositeOperation = 'source-over'; A.globalAlpha = 1; A.clearRect(0, 0, tw, th);
      A.setTransform(px / 2, 0, 0, px / 2, 0, 0);
      g = A;
    }
    g.globalCompositeOperation = glow; g.lineWidth = sketch ? 0.55 : 0.5;
    for (const [ci, list] of Object.entries(byCol)) {
      g.strokeStyle = BC()[ci]; g.globalAlpha = Math.min(1, Math.min(0.17, Math.max(0.025, 1.3 / Math.sqrt(list.length * every))) * dim * (sketch ? 1.4 : 1) * LUX() * (st.dark ? 1 : 1.5));
      for (let c0 = 0; c0 < list.length; c0 += CHUNK) {
        g.beginPath();
        const part = list.slice(c0, c0 + CHUNK);
        if (sketch) for (const li of part) { const f = fibre(li, proj); g.moveTo(f[0], f[1]); g.lineTo(f[2], f[3]); g.lineTo(f[4], f[5]); g.lineTo(f[6], f[7]); }
        else for (const li of part) thread(g, fibre(li, proj));
        g.stroke();
        if (due(glow)) yield;
      }
    }
    if (focus && !lit) {                         // a highlighted sector's axons stand out of the dimmed web
      const mine = {};
      for (let li = 0; li < m; li++) if (liveLink(li) && st.focused(li)) (mine[graph.nodes[graph.links[li][0]].c] ||= []).push(li);
      g.lineWidth = 0.9;
      for (const [ci, list] of Object.entries(mine)) {
        g.strokeStyle = BC()[ci]; g.globalAlpha = Math.min(1, Math.min(0.55, Math.max(0.08, 4 / Math.sqrt(list.length))) * LUX());
        g.beginPath();
        for (const li of list) thread(g, fibre(li, proj));
        g.stroke();
      }
    }
    if (A) {                                       // the threads onto the field, then the full-resolution rest
      g = field;
      g.setTransform(1, 0, 0, 1, 0, 0); g.globalAlpha = 1; g.globalCompositeOperation = glow; g.imageSmoothingEnabled = true;
      g.drawImage(threads, 0, 0, lw, lh);
      g.setTransform(px, 0, 0, px, 0, 0);
    }
    if (lit) {
      g.globalCompositeOperation = glow; g.lineWidth = 1.3; g.globalAlpha = 0.9;
      for (const li of lit.links) { if (!liveLink(li)) continue; g.strokeStyle = BC()[graph.nodes[graph.links[li][0]].c]; g.beginPath(); thread(g, fibre(li, proj)); g.stroke(); }
    }
    // which somas are in view, and how near: three depth groups for brightness
    const zoomed = Math.pow(view.zoom, 0.55), pad = 30, group = new Int8Array(n).fill(-1);
    for (let i = 0; i < n; i++) if (live(i) && X[i] > -pad && X[i] < w + pad && Y[i] > -pad && Y[i] < h + pad) group[i] = Z[i] < -0.35 ? 0 : Z[i] < 0.25 ? 1 : 2;
    const fade = [0.4, 0.72, 1], mineOf = i => st.focus.has(graph.nodes[i].c), dimOf = i => lit ? 0.4 : focus && !mineOf(i) ? 0.12 : 1;
    // neurites: projected point by point, stroked in batches by colour and depth group
    for (let k = 0; k < NC.length; k++) {
      if (group[NC[k]] < (sketch ? 2 : 0)) continue;
      for (let p2 = NS[k]; p2 < NS[k + 1]; p2++) { const [sx, sy] = proj(NPF[p2 * 3], NPF[p2 * 3 + 1], NPF[p2 * 3 + 2]); NX[p2] = sx; NY[p2] = sy; }
    }
    const arms = {};
    for (let k = 0; k < NC.length; k++) { const i = NC[k]; if (group[i] < (sketch ? 2 : 0)) continue; (arms[graph.nodes[i].c * 3 + group[i]] ||= []).push(k); }
    g.lineWidth = 0.8;
    for (const [key, list] of Object.entries(arms)) {
      const ci = Math.floor(key / 3), gr = key % 3, i0 = NC[list[0]];
      g.strokeStyle = BC()[ci]; g.globalAlpha = Math.min(1, 0.42 * fade[gr] * dimOf(i0) * LUX());
      g.beginPath();
      for (const k of list) { const s0 = NS[k], s1 = NS[k + 1]; g.moveTo(NX[s0], NY[s0]); for (let p = s0 + 1; p < s1; p++) g.lineTo(NX[p], NY[p]); }
      g.stroke();
      if (due(glow)) yield;
    }
    // somas as organic neuron cells (t-605, the t-596 look the owner asked back for): every soma big
    // enough to read is the shaded neuron sprite with its forked dendrites, in its own shape and tint;
    // the smallest are dots. While moving only the near side keeps its sprites. A soft halo at rest.
    const cells = {}, lux = LUX(), radius = i => 1.25 * zoomed * S[i] * Math.sqrt(grow[i]) * (mineOf(i) ? 1.35 : 1);
    const cellular = i => { const r = radius(i); return sketch ? group[i] === 2 && r >= 1.8 : r >= 1.4; };
    for (let i = 0; i < n; i++) if (group[i] >= 0) (cells[graph.nodes[i].c * 3 + group[i]] ||= []).push(i);
    if (!sketch) {                                           // the halos first, then the cores over them
      for (const [key, list] of Object.entries(cells)) {
        const ci = Math.floor(key / 3), gr = key % 3, d = dimOf(list[0]), thick = Math.min(1, 26 / Math.sqrt(list.length));
        g.fillStyle = BC()[ci]; g.globalCompositeOperation = glow; g.globalAlpha = Math.min(1, 0.07 * fade[gr] * d * thick * lux * (st.dark ? 1 : 1.6)); g.beginPath();
        for (const i of list) { const r = 2.6 * radius(i); g.moveTo(X[i] + r, Y[i]); g.arc(X[i], Y[i], r, 0, Math.PI * 2); }
        g.fill();
      }
      if (due(glow)) yield;
    }
    for (const [key, list] of Object.entries(cells)) {
      const ci = Math.floor(key / 3), gr = key % 3, d = dimOf(list[0]), thick = Math.min(1, 26 / Math.sqrt(list.length));
      g.fillStyle = BC()[ci];
      g.globalCompositeOperation = 'source-over'; g.globalAlpha = Math.min(1, 0.85 * fade[gr] * d * Math.max(0.45, thick) * Math.min(1.15, 0.45 + 0.55 * lux)); g.beginPath();
      for (const i of list) {
        if (cellular(i)) continue;
        const r = radius(i);
        if (r < 1.6) g.rect(X[i] - r, Y[i] - r, 2 * r, 2 * r); else { g.moveTo(X[i] + r, Y[i]); g.arc(X[i], Y[i], r, 0, Math.PI * 2); }
      }
      g.fill();
      if (due('source-over')) yield;
    }
    // each cell is stamped 1:1 in device pixels from a copy made once at that size (t-607): resizing a
    // sprite on every stamp was most of a full paint
    let stamped = 0;
    g.setTransform(1, 0, 0, 1, 0, 0);
    for (let gr = 0; gr < 3; gr++) for (let i = 0; i < n; i++) {
      if (group[i] !== gr || !cellular(i)) continue;
      g.globalAlpha = Math.min(1, fade[gr] * dimOf(i) * Math.min(1.1, 0.5 + 0.5 * lux));
      const bmp = sized(graph.nodes[i].c, shape[i], 2 * radius(i) * 1.5 / SOMA * px), half = bmp.width >> 1;
      g.drawImage(bmp, Math.round(X[i] * px) - half, Math.round(Y[i] * px) - half);
      if (++stamped % 600 === 0 && due('source-over')) yield;
    }
    g.setTransform(px, 0, 0, px, 0, 0);
    g.globalCompositeOperation = 'source-over'; g.globalAlpha = 1;
    glass(near, g);
  }
  // A pulse runs each firing fibre on the link's own beat; the neuron it reaches flashes.
  function pulses(list, t, proj, glow) {
    const g = ctx, beat = BEAT_MS * 100 / st.speed, dots = {}, flashes = [];
    for (const li of list) {
      const cyc = (t / beat + offset(li)) % 1, b = graph.links[li][st.back?.[li] ? 0 : 1];
      if (cyc < TRAVEL) (dots[graph.nodes[b].c] ||= []).push(li, cyc / TRAVEL);
      else if (cyc < TRAVEL + FLASH) { const p = (cyc - TRAVEL) / FLASH; flashes.push(b, Math.sin(Math.PI * Math.min(1, p * 1.6 < 0.5 ? p * 1.6 : 0.5 + (p - 0.3125) / 1.375))); }
    }
    g.globalCompositeOperation = glow;
    for (const [ci, flat] of Object.entries(dots)) {
      g.fillStyle = BC()[ci];
      for (const [lag, alpha, size] of [[0.08, 0.25, 1.6], [0.04, 0.5, 2], [0, 1, 2.6]]) {
        g.globalAlpha = Math.min(1, alpha * LUX()); g.beginPath();
        for (let k = 0; k < flat.length; k += 2) { const s = flat[k + 1] - lag; if (s < 0) continue; const [px, py] = at(fibre(flat[k], proj), st.back?.[flat[k]] ? 1 - s : s); g.rect(px - size / 2, py - size / 2, size, size); }
        g.fill();
      }
    }
    // flashes as batched dots (t-601: a sprite per flash cost 84 percent of a frame): by colour and by
    // four brightness steps, a soft halo then a bright core
    const lumps = {};
    for (let k = 0; k < flashes.length; k += 2) {
      const i = flashes[k], amount = flashes[k + 1];
      if (!live(i) || amount < 0.05) continue;
      (lumps[graph.nodes[i].c * 4 + Math.min(3, Math.floor(amount * 4))] ||= []).push(i, amount);
    }
    for (const [key, flat] of Object.entries(lumps)) {
      const ci = Math.floor(key / 4), level = (key % 4 + 0.5) / 4;
      g.fillStyle = BC()[ci];
      for (const [scale, alpha] of [[3.2, 0.16], [1.2, 0.85]]) {
        g.globalAlpha = Math.min(1, alpha * level * LUX()); g.beginPath();
        for (let k = 0; k < flat.length; k += 2) { const i = flat[k], r = scale * 1.25 * S[i] * Math.sqrt(grow[i]) * (0.7 + 0.6 * flat[k + 1]); g.moveTo(X[i] + r, Y[i]); g.arc(X[i], Y[i], r, 0, Math.PI * 2); }
        g.fill();
      }
    }
    g.globalCompositeOperation = 'source-over'; g.globalAlpha = 1;
  }
  // Project the wireframe; draw its far half now and return the near half's segments for glass().
  function outline(proj, g = ctx) {
    const far = [[], []], near = [];
    for (const line of frameLines) {
      let px = 0, py = 0, pz = 0;
      for (let k = 0; k < line.length; k += 3) {
        const [sx, sy, z] = proj(line[k], line[k + 1], line[k + 2]);
        if (k) { const mz = (z + pz) / 2; (mz < -0.25 ? far[0] : mz < 0.15 ? far[1] : near).push(px, py, sx, sy); }
        px = sx; py = sy; pz = z;
      }
    }
    g.globalCompositeOperation = 'source-over'; g.strokeStyle = TEXT(); g.lineWidth = 0.8;
    for (const [segs, alpha] of [[far[0], 0.07], [far[1], 0.12]]) {
      g.globalAlpha = alpha; g.beginPath();
      for (let k = 0; k < segs.length; k += 4) { g.moveTo(segs[k], segs[k + 1]); g.lineTo(segs[k + 2], segs[k + 3]); }
      g.stroke();
    }
    return near;
  }
  function glass(segs, g = ctx) {
    g.globalCompositeOperation = 'source-over'; g.strokeStyle = TEXT(); g.lineWidth = 0.9; g.globalAlpha = 0.14;
    g.beginPath();
    for (let k = 0; k < segs.length; k += 4) { g.moveTo(segs[k], segs[k + 1]); g.lineTo(segs[k + 2], segs[k + 3]); }
    g.stroke(); g.globalAlpha = 1;
  }
  function labels(list) {
    if (list.length > 30) return;
    const g = ctx;
    g.font = `11px ${st.font}`; g.textAlign = 'left';
    const taken = [];
    for (const i of list.sort((a, b) => Z[b] - Z[a])) {
      if (Z[i] < -0.2) continue;                     // a label on the far side would float over the near one
      const x = X[i] + 7, y = Y[i] + 4;
      if (taken.some(([tx, ty]) => Math.abs(tx - x) < 120 && Math.abs(ty - y) < 13)) continue;
      taken.push([x, y]);
      let text = graph.nodes[i].label || String(graph.nodes[i].id).replace(/^(ev|proof|claim|session|msg|door|as|src):/, '');
      if (text.length > 48) text = text.slice(0, 45) + '...';
      const wdt = g.measureText(text).width;
      g.fillStyle = st.brainBg; g.globalAlpha = 0.82; g.fillRect(x - 3, y - 10, wdt + 6, 14);
      g.globalAlpha = 1; g.fillStyle = TEXT(); g.fillText(text, x, y);
    }
  }
  // time: auto-rotate and the turn toward a chosen neuron
  function step(t) {
    const dt = view.last ? Math.min(100, Math.max(0, t - view.last)) : 0;
    if (view.ease) {
      const e = view.ease;
      if (!e.start) e.start = t || 1;
      const k = Math.min(1, Math.max(0, (t - e.start) / 700)), s = k * k * (3 - 2 * k);
      view.yaw = e.yaw0 + (e.yaw1 - e.yaw0) * s; view.pitch = e.pitch0 + (e.pitch1 - e.pitch0) * s;
      view.panX = e.panX0 * (1 - s); view.panY = e.panY0 * (1 - s);
      if (k >= 1) view.ease = null;
    } else if (view.spin && !held() && (!st.reduce || st.moved)) view.yaw += dt * TURN_RATE * (st.turn ?? 100) / 100;
    if (keys.size) {                                       // held arrows or WASD glide the brain, 0.6 px per ms
      const v = 0.6 * dt;
      if (keys.has('left')) view.panX += v; if (keys.has('right')) view.panX -= v;
      if (keys.has('up')) view.panY += v; if (keys.has('down')) view.panY -= v;
      touched();
    }
  }
  function animating() { return (!st.reduce || st.moved) && (!!st.lit || st.fire > 0); }
  // auto-rotate holds still while the viewer pans, zooms or holds a key, and for SETTLE_MS after (t-607):
  // a turning brain must be painted again every frame, a still one is one blit of the finished layer
  function held() { return !!gesture || keys.size > 0 || performance.now() - lastInput < SETTLE_MS; }
  function spinning() { return !!view.ease || (view.spin && (st.turn ?? 100) > 0 && (!st.reduce || st.moved) && !held()); }
  function moving() { return spinning() || keys.size > 0 || !!job; }
  function turnTo(i) {
    if (i == null) return;
    face(P[i * 3], P[i * 3 + 1], P[i * 3 + 2]);
  }
  // turn the brain so the point (x, y, z) faces the viewer
  function face(x, y, z) {
    let yaw1 = Math.atan2(x, z);
    const z1 = Math.hypot(x, z), pitch1 = Math.max(-1.2, Math.min(1.2, Math.atan2(y, z1)));
    while (yaw1 - view.yaw > Math.PI) yaw1 -= 2 * Math.PI;
    while (yaw1 - view.yaw < -Math.PI) yaw1 += 2 * Math.PI;
    view.ease = {start: 0, yaw0: view.yaw, yaw1, pitch0: view.pitch, pitch1, panX0: view.panX, panY0: view.panY};
    view.spin = false; hooks.showMotion(); hooks.kick();
  }
  function pick(x, y) {
    let best = null, bestScore = Infinity;
    for (let i = 0; i < n; i++) {
      if (!live(i)) continue;
      const dx = X[i] - x, dy = Y[i] - y, d2 = dx * dx + dy * dy, reach = 7 + 3 * S[i] * grow[i];
      if (d2 > reach * reach) continue;
      const score = d2 - Z[i] * 40;                  // the nearer neuron wins a close call
      if (score < bestScore) { bestScore = score; best = i; }
    }
    return best;
  }
  const local = e => { const r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
  canvas.addEventListener('contextmenu', e => e.preventDefault());
  canvas.addEventListener('pointerdown', e => {
    canvas.setPointerCapture?.(e.pointerId);
    pointers.set(e.pointerId, local(e));
    const [x, y] = local(e);
    if (pointers.size === 2) { const [a, b] = [...pointers.values()]; gesture = {kind: 'pinch', d: Math.hypot(a[0] - b[0], a[1] - b[1]), zoom: view.zoom, mid: [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2], panX: view.panX, panY: view.panY, moved: true}; return; }
    gesture = {kind: e.shiftKey || e.button === 2 || e.ctrlKey ? 'pan' : 'turn', x, y, yaw: view.yaw, pitch: view.pitch, panX: view.panX, panY: view.panY, moved: false};
    view.ease = null;
  });
  canvas.addEventListener('pointermove', e => {
    const [x, y] = local(e);
    if (pointers.has(e.pointerId)) pointers.set(e.pointerId, [x, y]);
    if (gesture?.kind === 'pinch' && pointers.size === 2) {
      const [a, b] = [...pointers.values()], d = Math.hypot(a[0] - b[0], a[1] - b[1]);
      view.zoom = Math.max(0.4, Math.min(12, gesture.zoom * d / Math.max(1, gesture.d)));
      view.panX = gesture.panX + (a[0] + b[0]) / 2 - gesture.mid[0]; view.panY = gesture.panY + (a[1] + b[1]) / 2 - gesture.mid[1];
      touched(); hooks.kick(); return;
    }
    if (gesture) {
      const dx = x - gesture.x, dy = y - gesture.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) gesture.moved = true;
      if (!gesture.moved) return;
      if (gesture.kind === 'turn') { view.yaw = gesture.yaw + dx * 0.006; view.pitch = Math.max(-1.45, Math.min(1.45, gesture.pitch + dy * 0.006)); }
      else { view.panX = gesture.panX + dx; view.panY = gesture.panY + dy; }
      touched(); hooks.kick(); return;
    }
    const i = pick(x, y);
    canvas.style.cursor = i == null ? 'grab' : 'pointer';
    hooks.hover(i, x, y);
    hooks.kick();
  });
  function end(e) {
    pointers.delete(e.pointerId);
    const was = gesture;
    if (pointers.size === 0) { if (gesture) touched(); gesture = null; }
    else if (was?.kind === 'pinch') gesture = null;
    if (e.type === 'pointerup' && was && !was.moved && was.kind !== 'pinch') { const [x, y] = local(e); clicked = true; hooks.select(pick(x, y)); clicked = false; }
  }
  canvas.addEventListener('pointerup', end);
  canvas.addEventListener('pointercancel', end);
  canvas.addEventListener('pointerleave', () => { if (!gesture) hooks.hover(null, 0, 0); });
  // a double-click sends a shockwave from the cursor; squarely on a neuron it also turns that neuron to the front
  canvas.addEventListener('dblclick', e => {
    const [x, y] = local(e), i = pick(x, y);
    hooks.wave?.(x, y);
    if (i != null && Math.hypot(X[i] - x, Y[i] - y) <= SOMA_PX * S[i] * grow[i] + 2) { hooks.select(i); turnTo(i); }
  });
  canvas.addEventListener('wheel', e => {
    e.preventDefault();
    const [x, y] = local(e), k = e.deltaY < 0 ? 1.15 : 1 / 1.15, z = Math.max(0.4, Math.min(12, view.zoom * k)), f = z / view.zoom;
    const ox = x - view.w / 2, oy = y - view.h / 2;                // keep the point under the cursor in place
    view.panX = ox - (ox - view.panX) * f; view.panY = oy - (oy - view.panY) * f; view.zoom = z;
    touched(); hooks.kick();
  }, {passive: false});
  // keys (t-605): arrows and WASD move the brain in x and y while held (on the brain, or with the pointer
  // over it), Shift+arrows turn it, + and - zoom, 0 resets, space toggles auto-rotate
  const PAN_KEYS = {ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down', a: 'left', d: 'right', w: 'up', s: 'down', A: 'left', D: 'right', W: 'up', S: 'down'};
  const typing = () => { const el = document.activeElement; return !!el && el !== canvas && (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || el.isContentEditable); };
  const aimed = () => !canvas.hidden && (document.activeElement === canvas || canvas.matches(':hover')) && !typing();
  document.addEventListener('keydown', e => {
    if (!canvas.isConnected || !aimed() || e.ctrlKey || e.metaKey || e.altKey) return;
    const dir = !e.shiftKey && PAN_KEYS[e.key];
    if (!dir) return;
    e.preventDefault();
    if (!keys.has(dir)) { keys.add(dir); view.ease = null; hooks.kick(); }
  });
  document.addEventListener('keyup', e => { const dir = PAN_KEYS[e.key]; if (dir && keys.delete(dir)) { touched(); hooks.kick(); } });
  addEventListener('blur', () => { if (keys.size) { keys.clear(); touched(); hooks.kick(); } });
  canvas.addEventListener('keydown', e => {
    if (PAN_KEYS[e.key] && !e.shiftKey) return;           // the document handler moves the brain
    const turn = e.shiftKey && {ArrowLeft: [-0.12, 0], ArrowRight: [0.12, 0], ArrowUp: [0, -0.12], ArrowDown: [0, 0.12]}[e.key];
    if (turn) { view.yaw += turn[0]; view.pitch = Math.max(-1.45, Math.min(1.45, view.pitch + turn[1])); }
    else if (e.key === '+' || e.key === '=') { view.zoom = Math.min(12, view.zoom * 1.15); touched(); }
    else if (e.key === '-') { view.zoom = Math.max(0.4, view.zoom / 1.15); touched(); }
    else if (e.key === '0') reset();
    else if (e.key === ' ') { view.spin = !view.spin; st.moved = true; hooks.showMotion(); }
    else return;
    e.preventDefault(); hooks.kick();
  });
  function reset() { Object.assign(view, {yaw: VIEW.yaw, pitch: VIEW.pitch, zoom: 1, panX: 0, panY: 0, ease: null}); hooks.kick(); }
  return {
    draw, resize, reset, moving,
    changed() { layerKey = ''; touched(); hooks.kick(); },
    select(i) { if (i != null && st.mode === 'brain' && !clicked) turnTo(i); hooks.kick(); },
    // a highlighted region turns to the front: the brain faces the centre of that column's neurons
    focus(ci) {
      if (ci == null || st.mode !== 'brain') { hooks.kick(); return; }
      const ids = graph.cols[ci] || [];
      if (!ids.length) return;
      let x = 0, y = 0, z = 0;
      for (const i of ids) { x += P[i * 3]; y += P[i * 3 + 1]; z += P[i * 3 + 2]; }
      if (Math.hypot(x, z) / ids.length < 0.08) { hooks.kick(); return; }   // a central region faces every side alike
      face(x / ids.length, y / ids.length, z / ids.length);
    },
    spinning: () => view.spin,
    spin(on) { view.spin = !!on; view.last = 0; hooks.kick(); },
    view, positions: P,
  };
}
