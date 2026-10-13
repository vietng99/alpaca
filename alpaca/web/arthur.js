// Arthur the alpaca: the sidebar slot under the brand. Arthur acts out what the
// record says, read from the overview (alpaca/hub.py overview: the tasks, ops, active_jobs and the
// `arthur` block that _arthur builds), and what the person does in this page. One looping pose at a
// time, the first that holds wins (loopFor):
//   problem          the live connection has been lost for OFFLINE_MS (a short reconnect is not
//                    "offline": a proxy ends long streams and the browser retries in 3 s);
//   mimic_typing     keys are typed in the Ctrl K search (#command-query), until TUNE.typingQuietMs quiet;
//   telescope        the Roadmap page is open (its task view or its mission view);
//   waiting          the owner owes an answer: an open batch item, a presented brief not signed, an open order;
//   juggling         JUGGLE_AT or more tasks in flight and jobs running together;
//   headphones       a research run has been active longer than RESEARCH_LONG_MS;
//   drinking_coffee  a job has been running longer than JOB_LONG_MS;
//   searching        a research run is active or a job runs;
//   reading          a proof is being written for a task in flight, or a log review runs;
//   thinking         a design-phase task is in flight, or a self-check runs;
//   checklist        a verify-phase task is in flight;
//   typing           tasks are in flight;
//   sleeping         nothing is in flight and nothing moved for SLEEP_QUIET_MS, or it is night on the
//                    orb clock (pulse.js periodAt);
//   idle             otherwise. His resting pose stays plain idle, so cursor gaze keeps working.
// The base order (problem, waiting, searching, reading, thinking, typing, idle) is kept; package 2's
// poses sit between them. The two page poses come before waiting: they answer what the person does
// right now and last only while they type or look at the Roadmap (a new wait still raises a hand).
// sleeping, juggling and checklist play their entry once and hold the last pose (the package's
// loop:false context clips); the others loop. Detective is not wired: no record field marks a task
// as a debug or investigation task (phases are requirement, design, build, verify, release).
// One-shot reactions (reactionsFor) fire when a counter or newest event id grows between two overview
// reads, never on the first read, in this order: raising_hand when a new owner wait appears (the
// waiting loop stays while it is owed); shield_up when the research guard denies a call, a gate says
// BLOCKED or the git guard denies a command (its deny note file grew); merging_papers and then celebrate when a merge lands on main; celebrate when a task reaches
// done, an op closes or a gate passes; dance when every task of an op is done; stamping when a proof
// is sealed; packing_box when a release build passes; problem on a gate FAIL, a NO-GO review or a
// failed test run; a wave when a new session joins (and one wave on load). Page reactions: a click (or
// Enter or Space, it is a button) plays the next trick of TRICKS at once, and the TUNE.spitClicks-th
// click inside TUNE.spitWindowMs makes him spit instead; a theme switch plays sunglasses (to light)
// or nightcap (to dark) once; coming back to the tab after TUNE.yawnAwayMs hidden plays yawn_stretch;
// scrolling the sidebar after TUNE.peekIdleMs at rest plays peekaboo; a fresh cost spike on the token
// board (burn.js) plays counting_coins; a day listed in project.yaml `studio.party_days` plays
// party_hat once. They queue and play one after another. Whenever the cursor moves he turns to look
// at it (the package draws gaze only in its idle pose) and goes back to work LOOK_MS after the cursor
// stops. He paces across the slot once on each page switch and after PACE_IDLE_MS idle with no
// cursor movement: the stage slides with a CSS transform while he plays move-right and move-left (the
// package's movement poses do not move the element); with reduced motion he plays the poses in place.
// The caption says why he does what he does. The artwork and player are the owner's Arthur package,
// vendored under vendor/arthur with one owner-accepted patch (see vendor/README.md); reduced motion follows the system.
import {inflightCount} from './inflight.js';
import {caption, periodAt} from './pulse.js';

const BASE = '/hub/assets/vendor/arthur/assets/';
export const OFFLINE_MS = 30000;
export const LOOK_MS = 2500;
export const PACE_IDLE_MS = 180000;
// One leg of a pace: two cycles of the 1060 ms movement pose. The middle leg crosses twice as far.
export const LEG_MS = 2120;
// What a click on Arthur plays, in turn: a jump, a wave, the pencil, the book, the magnifying glass.
export const TRICKS = ['celebrate', 'greeting', 'thinking', 'reading', 'searching'];
// Thresholds of the record-driven poses (the action roster of package 2 names each one).
export const JUGGLE_AT = 8;
export const RESEARCH_LONG_MS = 30 * 60000;
export const JOB_LONG_MS = 60 * 60000;
export const SLEEP_QUIET_MS = 30 * 60000;
// A runaway cell on the token board (the board's own spike mark: 15 minutes at 5 times the session's
// median, alpaca/analytics/insights.py runaway) counts as a fresh cost spike while it is this recent.
export const SPIKE_FRESH_MS = 60 * 60000;
// The page-driven thresholds. A test may shorten them (the browser check does).
export const TUNE = {typingQuietMs: 600, spitClicks: 10, spitWindowMs: 2000, yawnAwayMs: 300000, peekIdleMs: 60000};
// Poses that count as Arthur at rest, for peekaboo.
const REST = new Set(['idle', 'sleeping']);

const plural = (n, one, many = one + 's') => `${n} ${n === 1 ? one : many}`;
const list = parts => parts.filter(Boolean).join(' / ');
const older = (stamps, now, ms) => (stamps || []).filter(at => typeof at === 'number' && now - at * 1000 > ms).length;

// The looping pose and its caption for a workspace state, by the priority in the header. `now` is
// epoch milliseconds; the record's stamps (researchStarted, jobsStarted, lastWork) are epoch seconds.
export function loopFor({offline = false, waiting = {}, typing = false, roadmap = false, research = 0, jobs = 0,
  reading = {}, thinking = {}, verify = 0, tasks = 0, researchStarted = [], jobsStarted = [], lastWork = null,
  night = false, now = Date.now()} = {}) {
  if (offline) return {action: 'problem', caption: 'Offline'};
  if (typing) return {action: 'mimic_typing', caption: 'Typing along with you'};
  if (roadmap) return {action: 'telescope', caption: 'Looking over the roadmap'};
  const owed = [waiting.orders && plural(waiting.orders, 'order'), waiting.questions && plural(waiting.questions, 'question'),
    waiting.briefs && plural(waiting.briefs, 'brief to sign', 'briefs to sign')].filter(Boolean);
  if (owed.length) return {action: 'waiting', caption: 'Waiting on you: ' + owed.join(', ')};
  if (tasks + jobs >= JUGGLE_AT) return {action: 'juggling', caption: list([plural(tasks, 'task') + ' in flight', jobs && plural(jobs, 'job') + ' running'])};
  const longRuns = older(researchStarted, now, RESEARCH_LONG_MS);
  if (research && longRuns) return {action: 'headphones', caption: `${plural(Math.min(longRuns, research), 'research run')} active over 30 min`};
  const longJobs = older(jobsStarted, now, JOB_LONG_MS);
  if (jobs && longJobs) return {action: 'drinking_coffee', caption: `${plural(Math.min(longJobs, jobs), 'job')} running over an hour`};
  if (research || jobs) return {action: 'searching', caption: list([research && plural(research, 'research run') + ' active',
    jobs && plural(jobs, 'job') + ' running'])};
  if (reading.proofs || reading.reviews) return {action: 'reading', caption: list([
    reading.proofs && (reading.proofs === 1 ? 'Writing a proof' : `Writing ${reading.proofs} proofs`),
    reading.reviews && (reading.reviews === 1 ? 'Reviewing a log' : `Reviewing ${reading.reviews} logs`)])};
  if (thinking.design || thinking.selfchecks) return {action: 'thinking', caption: list([
    thinking.design && 'Designing: ' + plural(thinking.design, 'task'),
    thinking.selfchecks && (thinking.selfchecks === 1 ? 'Self-check running' : `${thinking.selfchecks} self-checks running`)])};
  if (verify) return {action: 'checklist', caption: 'Verifying: ' + plural(verify, 'task')};
  if (tasks) return {action: 'typing', caption: caption(tasks, 0)};
  const quiet = typeof lastWork === 'number' ? now - lastWork * 1000 : null;
  if (night) return {action: 'sleeping', caption: 'Asleep: night, nothing in flight'};
  if (quiet !== null && quiet >= SLEEP_QUIET_MS) return {action: 'sleeping', caption: `Asleep: quiet for ${Math.floor(quiet / 60000)} min`};
  return {action: 'idle', caption: 'Idle'};
}

// The state loopFor reads and the counters reactionsFor compares, from one overview payload. A payload
// without the `arthur` block (an older server, or a record the block could not read) leaves those
// counters null, so a later read never mistakes "unknown" for "grew from zero".
export function readOverview(o, now = Date.now()) {
  const a = o && o.arthur;
  const num = v => (typeof v === 'number' ? v : 0);
  const state = {tasks: inflightCount(o && o.tasks, now), jobs: ((o && o.active_jobs) || []).length,
    waiting: (a && a.waiting) || {}, research: (a && a.research) || 0, reading: (a && a.reading) || {},
    thinking: (a && a.thinking) || {}, verify: (a && a.verify) || 0, researchStarted: (a && a.research_started) || [],
    jobsStarted: (a && a.jobs_started) || [], lastWork: a && typeof a.last_work === 'number' ? a.last_work : null,
    partyDays: (a && a.party_days) || []};
  const w = a && a.waiting;
  const opsDone = {};
  for (const op of (o && o.ops) || []) if (op && op.id) opsDone[op.id] = op.tasks_total > 0 && op.tasks_done === op.tasks_total;
  const marks = {done: ((o && o.tasks) || []).filter(t => t.status === 'done').length,
    sessions: a && typeof a.sessions === 'number' ? a.sessions : null, events: (a && a.events) || null,
    main: (a && a.main) || null, owed: w ? num(w.questions) + num(w.briefs) + num(w.orders) : null,
    ops: o && Array.isArray(o.ops) ? opsDone : null};
  return {state, marks};
}

// The one-shot reactions between two reads, in the order they play. Never on the first read.
export function reactionsFor(prev, next) {
  if (!prev || !next) return [];
  const up = k => !!(prev.events && next.events) && (next.events[k] || 0) > (prev.events[k] || 0);
  const merged = !!(prev.main && next.main && next.main.merge && next.main.head !== prev.main.head);
  const out = [];
  if (typeof prev.owed === 'number' && typeof next.owed === 'number' && next.owed > prev.owed)
    out.push({action: 'raising_hand', why: 'A new question for you'});
  if (up('blocked') || up('git_blocked')) out.push({action: 'shield_up', why: 'A guard or a gate blocked a step'});
  if (merged) out.push({action: 'merging_papers', why: 'Merged to main'});
  const cheer = list([next.done > prev.done && 'A task is done', up('op_closed') && 'An op closed',
    up('gate_pass') && 'A gate passed', merged && 'Merged to main']);
  const fail = list([up('gate_fail') && 'A gate failed', up('no_go') && 'A review said NO-GO', up('tests_failed') && 'Tests failed']);
  if (cheer) out.push({action: 'celebrate', why: cheer});
  const finished = prev.ops && next.ops ? Object.keys(next.ops).filter(id => next.ops[id] && prev.ops[id] === false) : [];
  if (finished.length) out.push({action: 'dance', why: `Every task of ${finished.join(', ')} is done`});
  if (up('proof_sealed')) out.push({action: 'stamping', why: 'A proof was sealed'});
  if (up('release_built')) out.push({action: 'packing_box', why: 'A release candidate was built'});
  if (fail) out.push({action: 'problem', why: fail});
  if (prev.sessions !== null && next.sessions !== null && next.sessions > prev.sessions) {
    const n = next.sessions - prev.sessions;
    out.push({action: 'greeting', why: n === 1 ? 'A new session joined' : `${n} new sessions joined`});
  }
  return out;
}

// Whether an idle Arthur should pace now: idle pose, nothing playing, no cursor movement and no pace
// for PACE_IDLE_MS.
export function paceDue({action, busy, sinceMove, sincePace}) {
  return action === 'idle' && !busy && sinceMove >= PACE_IDLE_MS && sincePace >= PACE_IDLE_MS;
}

// Clicks on Arthur: the click times kept inside the window and whether this click is the one that
// makes him spit (TUNE.spitClicks clicks inside TUNE.spitWindowMs). A spit clears the count.
export function rapidClicks(times, now, count = TUNE.spitClicks, windowMs = TUNE.spitWindowMs) {
  const kept = [...(times || []).filter(t => now - t < windowMs), now];
  return kept.length >= count ? {spit: true, times: []} : {spit: false, times: kept};
}

// The pose a theme switch plays once: sunglasses to light, nightcap to dark; nothing when it did not change.
export function themeReaction(before, after) {
  if (!after || before === after) return null;
  return after === 'dark' ? 'nightcap' : 'sunglasses';
}

// Coming back to the tab: a yawn and a stretch after TUNE.yawnAwayMs hidden.
export const yawnDue = (hiddenAt, now) => !!hiddenAt && now - hiddenAt >= TUNE.yawnAwayMs;

// The sidebar scrolled: peekaboo when Arthur has rested (idle or asleep, nothing playing) TUNE.peekIdleMs.
export const peekDue = ({action, busy, restSince, now}) => REST.has(action) && !busy && !!restSince && now - restSince >= TUNE.peekIdleMs;

// The newest runaway cell on a token board (burn.json: sessions[].signals.runaway.bins[].start, epoch
// seconds), or 0.
export function newestSpike(board) {
  let at = 0;
  for (const s of (board && board.sessions) || []) {
    for (const b of (((s || {}).signals || {}).runaway || {}).bins || []) if (typeof b.start === 'number' && b.start > at) at = b.start;
  }
  return at;
}

// The local calendar day, YYYY-MM-DD, and whether it is one of the party days.
export const localDay = (date = new Date()) =>
  `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
export const partyToday = (days, date = new Date()) => Array.isArray(days) && days.includes(localDay(date));

// The page route: the address hash's page name, with the old #mission address read as Roadmap.
// Package 2's assets/avatar-manifest.js ships byte for byte. Its 21 new actions point at
// full-size sheets (assets/new-sheets) that the Studio does not ship; the Studio plays each
// from one strip of the package's own 192 x 208 frames (assets/strips/<action>.png, built by
// setup/vendor_arthur.py), so the avatar reads the manifest through stripsManifest: a new action keeps
// its frames, durations, loop flag, label and trigger and drops the full-size sheet geometry.
export const STRIP_KEEP = ['frames', 'durationsMs', 'loop', 'label', 'trigger'];
// The gaze atlas ships as a lossless WebP (setup/vendor_arthur.py), so its path ends in .webp here; the strips stay
// PNG (a WebP strip decodes one level off on 693 edge pixels, which the Studio baseline refuses). `v` is the
// content version of Arthur's files (the page's arthur-version meta, serve.py arthur_version): every image path
// gets it as a query, so the browser can keep the artwork for a day and still see a changed file at once (t-595).
export function stripsManifest(m, v = '') {
  if (!m || !m.actions) return m;
  const at = (image, ext = '') => (ext ? image.replace(/\.png$/, ext) : image) + (v ? `?v=${v}` : '');
  const actions = {};
  for (const [id, a] of Object.entries(m.actions)) {
    if (a && typeof a.image === 'string' && a.image.startsWith('new-sheets/')) {
      const e = {image: at(`strips/${id}.png`)};
      for (const k of STRIP_KEEP) if (k in a) e[k] = a[k];
      actions[id] = e;
    } else actions[id] = a && typeof a.image === 'string' ? {...a, image: at(a.image)} : a;
  }
  // The shipped atlas holds only the rows of the gaze frames (slim_atlas): the full width, from the top of the
  // first frame row to the bottom of the last, so the frames sit that far higher in the file.
  const frames = m.gaze && m.gaze.frames, top = frames ? Math.min(...frames.map(f => f.y)) : 0;
  const atlas = m.atlas && {...m.atlas, image: at(m.atlas.image, '.webp'), height: frames ? Math.max(...frames.map(f => f.y + f.height)) - top : m.atlas.height};
  return {...m, actions, atlas, ...(frames ? {gaze: {...m.gaze, frames: frames.map(f => ({...f, y: f.y - top}))}} : {})};
}

export const routeOf = hash => { const name = String(hash || '').replace(/^#/, '').split('?')[0]; return name === 'mission' ? 'roadmap' : name; };

let avatar = null, stage = null, want = 'idle', why = '', offline = false, looking = false, busy = null;
let state = {}, marks = null, queue = [], trick = 0, clicks = [], typingUntil = 0, hiddenAt = 0, restSince = 0;
let theme = null, seenSpike = 0, partied = new Set();
let lookTimer = 0, lostTimer = 0, paceTimer = 0, typeTimer = 0, onceTimer = 0, lastMove = Date.now(), lastPace = Date.now();
const reduced = () => typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;

function say(text) {
  const cap = document.getElementById('arthur-caption');
  if (cap) cap.textContent = text;
}

// A pose the package plays once and holds (loop:false in its manifest) holds its last frame as a loop.
function enter(action) {
  const spec = avatar.manifest && avatar.manifest.actions[action];
  avatar.setAction(action, spec && !spec.loop ? {loop: false, returnTo: action} : {});
}

function show(force = false) {
  if (!avatar || busy) return;
  const action = looking ? 'idle' : want;
  if (force || avatar.getState().action !== action) enter(action);
  if (!REST.has(action)) restSince = 0;
  else if (!restSince) restSince = Date.now();
}

function current() {
  // Before the first overview lands Arthur knows nothing, so he stays on the idle strip he already loaded: a rest
  // pose chosen from the clock alone would fetch a second strip on the first visit (t-595 confirm: sleeping at night).
  if (!marks && !offline) return {action: 'idle', caption: 'Idle'};
  return loopFor({...state, offline, typing: Date.now() < typingUntil, roadmap: routeOf(location.hash) === 'roadmap',
    night: periodAt(new Date()) === 'Night', now: Date.now()});
}

function refresh() {
  const loop = current();
  want = loop.action;
  if (!(busy === 'once' && why)) say(loop.caption);
  show();
}

function stopPace() {
  clearTimeout(paceTimer);
  if (stage) stage.style.transform = '';
  if (busy === 'pace') busy = null;
}

// Pace across the slot: right, all the way left, back to the middle; then the queue goes on.
function pace() {
  lastPace = Date.now();
  restSince = 0;
  const slot = document.getElementById('arthur-slot');
  const reach = reduced() || !slot ? 0 : Math.max(0, Math.floor((slot.clientWidth - stage.offsetWidth) / 2));
  const legs = [['move-right', reach, LEG_MS], ['move-left', -reach, 2 * LEG_MS], ['move-right', 0, LEG_MS]];
  busy = 'pace';
  const step = i => {
    if (busy !== 'pace') return;
    if (i === legs.length) { stopPace(); playNext(); return; }
    const [action, x, ms] = legs[i];
    avatar.setAction(action);
    stage.style.transitionDuration = ms + 'ms';
    stage.style.transform = x ? `translateX(${x}px)` : '';
    paceTimer = setTimeout(() => step(i + 1), ms);
  };
  step(0);
}

// A one-shot ends on the package's completion event. With reduced motion the package holds the first
// frame and never completes, so a timer of the clip's length (plus a margin) ends it too.
function finishOnce() {
  clearTimeout(onceTimer);
  if (busy !== 'once') return;
  busy = null;
  playNext();
  if (!busy) show(true);
}

function playNext() {
  if (!avatar || busy) return;
  const next = queue.shift();
  if (!next) { why = ''; refresh(); return; }
  if (next.pace) { pace(); return; }
  busy = 'once';
  restSince = 0;
  why = next.why || '';
  say(why || current().caption);
  avatar.setAction(next.action, {loop: false, returnTo: looking ? 'idle' : want});
  const spec = avatar.manifest && avatar.manifest.actions[next.action];
  const ms = spec ? spec.durationsMs.reduce((s, d) => s + d, 0) : 2000;
  clearTimeout(onceTimer);
  onceTimer = setTimeout(finishOnce, ms + 1500);
}

function react(items, first = false) {
  for (const item of items) {
    if (item.pace && (busy === 'pace' || queue.some(q => q.pace))) continue;
    if (first) queue.unshift(item); else queue.push(item);
  }
  playNext();
}

function checkParty() {
  const today = localDay();
  if (!partied.has(today) && partyToday(state.partyDays, new Date())) {
    partied.add(today);
    react([{action: 'party_hat', why: 'A day to celebrate'}]);
  }
}

// Called whenever the overview is read (hub.js updateChrome).
export function updateArthur(o) {
  if (!o) return;
  const read = readOverview(o);
  const found = reactionsFor(marks, read.marks);
  state = read.state;
  marks = read.marks;
  refresh();
  if (found.length) react(found);
  if (avatar) checkParty();
}

// Called with each token board the Sessions page draws (hub.js burnBoard): a runaway cell newer than
// the last one Arthur counted and younger than SPIKE_FRESH_MS plays counting_coins once.
export function noteBurnBoard(board) {
  const at = newestSpike(board);
  if (at > seenSpike) {
    seenSpike = at;
    if (Date.now() - at * 1000 <= SPIKE_FRESH_MS) react([{action: 'counting_coins', why: 'A cost spike on the token board'}]);
  }
}

function lookAtCursor(event) {
  if (event.pointerType === 'touch' || !avatar) return;
  lastMove = Date.now();
  clearTimeout(lookTimer);
  lookTimer = setTimeout(() => { looking = false; show(); }, LOOK_MS);
  if (!looking) { looking = true; show(); }
}

// The connection row turns amber for several states; only the two that mean the live connection is
// gone count toward offline: the stream retrying ("Reconnecting") and a page load that failed
// ("Offline"). "Stale", "Preparing" and "Live - capture gaps" are a reachable server.
export const LOST_LABELS = new Set(['Reconnecting', 'Offline']);
export const connectionLost = conn => conn.classList.contains('error') && LOST_LABELS.has((conn.querySelector('span')?.textContent || '').trim());
// "Stale" (a quiet refresh that failed) proves nothing either way: while the server is gone the 15 s refresh
// writes it between the health check's "Offline" every 10 s, so it neither starts nor clears the 30 s wait,
// and a wait that ends on it still counts as lost.
export const HOLD_LABELS = new Set(['Stale']);
export const connectionHeld = conn => conn.classList.contains('error') && HOLD_LABELS.has((conn.querySelector('span')?.textContent || '').trim());
// What a change of the connection row does to the 30 s wait: start it, leave it as it is, or clear it.
export const connectionStep = conn => connectionLost(conn) ? 'start' : connectionHeld(conn) ? 'hold' : 'clear';

function watchConnection(conn) {
  const check = () => {
    const step = connectionStep(conn);
    if (step === 'clear') {
      clearTimeout(lostTimer); lostTimer = 0;
      if (offline) { offline = false; refresh(); }
    } else if (step === 'start' && !lostTimer && !offline) {
      lostTimer = setTimeout(() => { lostTimer = 0; if (connectionStep(conn) !== 'clear') { offline = true; refresh(); } }, OFFLINE_MS);
    }
  };
  new MutationObserver(check).observe(conn, {attributes: true, attributeFilter: ['class'], childList: true, characterData: true, subtree: true});
  check();
}

// The page signals of package 2: Ctrl K typing, theme switches, tab visibility, sidebar scrolling.
function watchPage() {
  document.addEventListener('input', event => {
    if (event.target && event.target.id === 'command-query') {
      typingUntil = Date.now() + TUNE.typingQuietMs;
      refresh();
      clearTimeout(typeTimer);
      typeTimer = setTimeout(refresh, TUNE.typingQuietMs + 20);
    }
  });
  theme = document.documentElement.dataset.theme || null;
  window.addEventListener('alpaca:themechange', event => {
    const next = (event.detail && event.detail.resolved) || document.documentElement.dataset.theme || null;
    const pose = themeReaction(theme, next);
    theme = next;
    if (pose) react([{action: pose, why: next === 'dark' ? 'Dark theme on' : 'Light theme on'}], true);
  });
  document.addEventListener('visibilitychange', () => {
    const now = Date.now();
    if (document.visibilityState === 'hidden') { hiddenAt = now; return; }
    const away = yawnDue(hiddenAt, now);
    hiddenAt = 0;
    if (away) react([{action: 'yawn_stretch', why: 'Welcome back'}], true);
  });
  const sidebar = document.getElementById('sidebar');
  const peek = () => {
    if (peekDue({action: looking ? 'idle' : want, busy, restSince, now: Date.now()})) react([{action: 'peekaboo', why: 'Peekaboo'}], true);
  };
  // only a real scroll counts (the roster: the sidebar scrolls across Arthur's slot); a wheel turn on a
  // sidebar that fits the window moves nothing and plays nothing
  if (sidebar) sidebar.addEventListener('scroll', peek, {passive: true});
}

export async function startArthur() {
  const host = document.getElementById('arthur');
  if (!host || !window.ArthurAvatar) return;
  host.addEventListener('arthur:complete', () => { if (busy === 'once') finishOnce(); });
  const conn = document.getElementById('connection');
  if (conn) watchConnection(conn);
  try {
    avatar = await window.ArthurAvatar.create(host, {assetBaseURL: BASE, manifest: stripsManifest(window.ArthurAssets, document.querySelector('meta[name="arthur-version"]')?.content || ''),
      reducedMotion: 'system', label: 'Arthur the alpaca'});
  } catch (e) {
    host.hidden = true;
    return;
  }
  stage = host;
  document.addEventListener('pointermove', lookAtCursor, {passive: true});
  // a click plays its trick at once: it cuts a pace or a reaction short rather than wait in the queue;
  // the TUNE.spitClicks-th quick click plays spit instead
  host.addEventListener('click', () => {
    const count = rapidClicks(clicks, Date.now());
    clicks = count.times;
    stopPace();
    clearTimeout(onceTimer);
    busy = null;
    queue.unshift(count.spit ? {action: 'spit', why: 'Too many clicks'} : {action: TRICKS[trick++ % TRICKS.length]});
    playNext();
  });
  // a pace on a page switch only: a filter, a search, a date or the Roadmap/Mission tab
  // changes the hash on the same page and does not pace
  let page = routeOf(location.hash);
  window.addEventListener('hashchange', () => {
    refresh();
    const next = routeOf(location.hash);
    if (next !== page && !offline) react([{pace: true}]);
    page = next;
  });
  watchPage();
  setInterval(() => {
    const now = Date.now();
    if (!busy) refresh();
    checkParty();
    if (!offline && !queue.length && paceDue({action: want, busy: busy || looking, sinceMove: now - lastMove, sincePace: now - lastPace})) pace();
  }, 15000);
  react([{action: 'greeting'}]);
  checkParty();
}
