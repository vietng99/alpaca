// The autodrive level chip: the level in force for a session (alpaca/posture/level.py levels()),
// colored as the /autodrive skill colors its badge (L1 green, L2 cyan, L3 blue, L4 yellow, L5 orange,
// L6 red). `info` is {level: 'L5' or 'OFF', set, goal};
// a level the session never set shows as the project default, dimmed.
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const NAMES = {L1: 'Assist', L2: 'Partial', L3: 'Conditional', L4: 'High', L5: 'Full', L6: 'Fleet, never defers', OFF: 'off'};

export function levelChip(info, compact = false) {
  if (!info || !info.level) return '';
  const level = String(info.level).toUpperCase();
  const what = `Autodrive ${level}${NAMES[level] ? ': ' + NAMES[level] : ''}${info.set ? '' : ' (project default; the session set no level)'}${info.goal ? '. Goal: ' + info.goal : ''}`;
  return `<span class="ad-chip ad-${esc(level.toLowerCase())}${info.set ? '' : ' ad-default'}${compact ? ' compact' : ''}" title="${esc(what)}" aria-label="${esc(what)}">${compact ? '' : '<span class="ad-word">Autodrive</span>'}${esc(level)}</span>`;
}
