// The hub's loading sequence: large thinking-orbs (orb.js) that slide in one after another, each
// with its own line, so a long read shows the work moving on instead of one small spinner.
//
//   loader('record')                 the full-page sequence for a route
//   loader('document', {compact:1})  a 32px sequence for a panel or the inspector
//
// The steps are timed by CSS (loader rules in hub.css), not by the read itself: an orb joins
// every STEP_S seconds, the earlier ones settle smaller to its left, and the last line stays until
// the page replaces the loader. Reduced motion shows every orb at rest and only the last line.
import {orb} from './orb.js';
import {PAGES} from './pages.js';

export const STEP_S = 1.1;
const READ = ['searching', 'Searching the project record'];
const LONG = ['breathing', 'Still reading; a large record takes a few seconds'];
// Loader steps by kind: each page's are its registry `loader` (pages.json); the others are the default
// record read, the mission view of Roadmap, the burn board and the reader. Every orb state appears in
// at least one sequence.
export const SEQUENCES = {
  record: [READ, ['solving', 'Reading tasks, proofs and runs'], ['connecting', 'Linking work to its plan'], ['composing', 'Composing the page'], LONG],
  ...Object.fromEntries(PAGES.filter(p => p.loader).map(p => [p.id, p.loader])),
  mission: [READ, ['shaping', 'Shaping the mission map'], LONG],
  burn: [['solving', 'Reading tokens and cost across sessions'], ['weaving', 'Folding the burn board']],
  document: [['searching', 'Finding the document'], ['composing', 'Rendering it']],
};

const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

export function loader(kind = 'record', {compact = false, steps = null, note = ''} = {}) {
  const list = steps || SEQUENCES[kind] || SEQUENCES.record;
  const size = compact ? 32 : 64;
  const orbs = list.map(([state], i) => `<span class="seq-orb" style="--i:${i}">${orb(state, size)}</span>`).join('');
  const lines = list.map(([, text], i) => `<span class="seq-line" style="--i:${i}">${esc(text)}...</span>`).join('');
  return `<div class="loading orb-seq${compact ? ' compact' : ''}" role="status" aria-live="polite" aria-label="${esc(list.at(-1)[1])}" style="--n:${list.length};--step:${STEP_S}s">`
    + `<div class="seq-orbs">${orbs}</div><div class="seq-lines">${lines}</div><span class="seq-beam" aria-hidden="true"></span>`
    + (note ? `<p class="seq-note">${esc(note)}</p>` : '') + '</div>';
}

// The page each route shows while it reads.
export const routeLoader = route => loader(SEQUENCES[route] ? route : 'record');
