// The vault's Browse tray (op-012 t-596): the record narrowed tier by tier instead of one find box.
// The first tier is every operation; opening an item shows the items linked to it, grouped by their
// column (an operation's tasks, decisions, messages, review cards, phase doors and wiki claims; a task's
// sealed proofs, sessions, messages and decisions; a proof's evidence), and so on down. Items already on
// the path are left out, so a tier never leads back up. One filter box narrows the open tier; at the
// first tier it finds any item in the vault. evgraph.js renders the tray and keeps the path.
import {bareId, labelOf} from './evgraph.js';

export const TIER_LIMIT = 60;            // cards shown per group before "filter to narrow"

const natural = (a, b) => a.localeCompare(b, 'en', {numeric: true});
// Operations and tasks newest first (op-045 before op-001); the rest by their name.
function order(graph, ci, items) {
  const key = graph.columns[ci]?.key;
  if (key === 'op' || key === 'task' || key === 'decision' || key === 'review') return items.sort((a, b) => natural(bareId(graph.nodes[b].id), bareId(graph.nodes[a].id)));
  return items.sort((a, b) => natural(labelOf(graph.nodes[a]), labelOf(graph.nodes[b])));
}
// Every item linked to node i, in either direction.
export function neighbours(graph, i) {
  const out = new Set();
  for (const li of graph.from[i] || []) out.add(graph.links[li][1]);
  for (const li of graph.into[i] || []) out.add(graph.links[li][0]);
  return out;
}
// The groups of one tier: [{ci, items}] in column order. `path` is the list of opened node indexes.
export function tier(graph, path) {
  const opCol = graph.columns.findIndex(c => c.key === 'op');
  if (!path.length) return [{ci: opCol, items: order(graph, opCol, [...(graph.cols[opCol] || [])])}];
  const seen = new Set(path), by = new Map();
  for (const j of neighbours(graph, path[path.length - 1])) {
    if (seen.has(j)) continue;
    const ci = graph.nodes[j].c;
    if (!by.has(ci)) by.set(ci, []);
    by.get(ci).push(j);
  }
  return [...by.keys()].sort((a, b) => a - b).map(ci => ({ci, items: order(graph, ci, by.get(ci))}));
}
// The groups narrowed to items whose id or name holds `text`.
export function narrow(graph, groups, text) {
  const needle = String(text || '').trim().toLowerCase();
  if (!needle) return groups;
  const hit = i => String(graph.nodes[i].id).toLowerCase().includes(needle) || labelOf(graph.nodes[i]).toLowerCase().includes(needle);
  return groups.map(g => ({ci: g.ci, items: g.items.filter(hit)})).filter(g => g.items.length);
}
// Found items (findMatches order kept) grouped by column, for the first tier's search.
export function grouped(graph, items) {
  const by = new Map();
  for (const i of items) { const ci = graph.nodes[i].c; if (!by.has(ci)) by.set(ci, []); by.get(ci).push(i); }
  return [...by.keys()].sort((a, b) => a - b).map(ci => ({ci, items: by.get(ci)}));
}
// How many items an item would open: its links, less the one it was reached from.
export function reach(graph, i, path) {
  let k = 0;
  const seen = new Set(path);
  for (const j of neighbours(graph, i)) if (!seen.has(j) && j !== i) k++;
  return k;
}
