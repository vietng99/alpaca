// Compare a Studio capture with the stored baseline, image by image.
//
//   node alpaca/tests/studio/compare.mjs <baseline-dir> <capture-dir> <out-dir> [--max-pixels N] [--tolerance T]
//
// Threshold (stated, per image): a pixel differs when any of its R, G, B, A channels differs by more
// than --tolerance (default 0, exact); an image fails when more than --max-pixels (default 0) pixels
// differ outside its masks. The defaults are strict because two captures of one tree give zero
// differing pixels (the determinism proof in the t-474 proof report); raising them is a decision to
// record, never a way to make a refactor pass. Masks are the rectangles each capture recorded in its
// manifest.json (Arthur's canvas); the union of the baseline's and the capture's masks is skipped.
//
// Writes <out-dir>/summary.json and summary.txt, and <out-dir>/diff/<name>.png for every image with a
// difference: the baseline faded to grey, differing pixels red, masked areas blue. Exit 1 when an image
// is over the threshold, missing, or a different size; exit 0 otherwise.
import {readFileSync, writeFileSync, mkdirSync, existsSync} from 'node:fs';
import {join} from 'node:path';
import {decode, encode} from './png.mjs';

const args = process.argv.slice(2);
const opt = (name, fallback) => { const i = args.indexOf(name); if (i < 0) return fallback; const v = args[i + 1]; args.splice(i, 2); return Number(v); };
const maxPixels = opt('--max-pixels', Number(process.env.STUDIO_MAX_DIFF_PIXELS || 0));
const tolerance = opt('--tolerance', Number(process.env.STUDIO_TOLERANCE || 0));
const [baseDir, capDir, outDir] = args;
if (!baseDir || !capDir || !outDir) { console.error('usage: compare.mjs <baseline-dir> <capture-dir> <out-dir> [--max-pixels N] [--tolerance T]'); process.exit(2); }
const manifest = dir => JSON.parse(readFileSync(join(dir, 'manifest.json'), 'utf8'));
const base = manifest(baseDir), cap = manifest(capDir);
mkdirSync(join(outDir, 'diff'), {recursive: true});
const inside = (masks, x, y) => masks.some(([mx, my, mw, mh]) => x >= mx && x < mx + mw && y >= my && y < my + mh);

const rows = [];
for (const [name, meta] of Object.entries(base.images)) {
  const file = name + '.png', row = {name, status: 'same', differing: 0, compared: 0, masked: 0};
  rows.push(row);
  if (!cap.images[name] || !existsSync(join(capDir, file))) { row.status = 'missing'; continue; }
  const a = decode(readFileSync(join(baseDir, file))), b = decode(readFileSync(join(capDir, file)));
  if (a.width !== b.width || a.height !== b.height) { row.status = 'size'; row.detail = `${a.width}x${a.height} vs ${b.width}x${b.height}`; continue; }
  const masks = [...(meta.masks || []), ...(cap.images[name].masks || [])];
  const out = Buffer.alloc(a.data.length);
  let minX = Infinity, minY = Infinity, maxX = -1, maxY = -1;
  for (let y = 0; y < a.height; y++) for (let x = 0; x < a.width; x++) {
    const o = (y * a.width + x) * 4;
    const grey = (a.data[o] * 0.3 + a.data[o + 1] * 0.59 + a.data[o + 2] * 0.11) * 0.35 + 165;
    if (masks.length && inside(masks, x, y)) { row.masked++; out[o] = grey * 0.6; out[o + 1] = grey * 0.7; out[o + 2] = 255; out[o + 3] = 255; continue; }
    row.compared++;
    let d = 0;
    for (let c = 0; c < 4; c++) d = Math.max(d, Math.abs(a.data[o + c] - b.data[o + c]));
    if (d > tolerance) {
      row.differing++; out[o] = 230; out[o + 1] = 20; out[o + 2] = 30; out[o + 3] = 255;
      minX = Math.min(minX, x); minY = Math.min(minY, y); maxX = Math.max(maxX, x); maxY = Math.max(maxY, y);
    } else { out[o] = out[o + 1] = out[o + 2] = grey; out[o + 3] = 255; }
  }
  if (row.differing) {
    row.box = [minX, minY, maxX - minX + 1, maxY - minY + 1];
    writeFileSync(join(outDir, 'diff', file), encode({width: a.width, height: a.height, data: out}));
  }
  if (row.differing > maxPixels) row.status = 'changed';
  else if (row.differing) row.status = 'within-threshold';
}
const extra = Object.keys(cap.images).filter(n => !base.images[n]);
const failed = rows.filter(r => !['same', 'within-threshold'].includes(r.status));
const summary = {baseline: baseDir, capture: capDir, threshold: {max_differing_pixels_per_image: maxPixels, channel_tolerance: tolerance},
  images: rows.length, failed: failed.length, differing_pixels_total: rows.reduce((n, r) => n + r.differing, 0),
  extra_in_capture: extra, baseline_capture: base.capture || null, this_capture: cap.capture || null, rows};
writeFileSync(join(outDir, 'summary.json'), JSON.stringify(summary, null, 1) + '\n');
const text = [`Studio compare: ${rows.length} images, ${failed.length} over the threshold (max ${maxPixels} differing pixels per image, channel tolerance ${tolerance}), ${summary.differing_pixels_total} differing pixels in all.`,
  ...rows.map(r => `${r.status.padEnd(16)} ${r.name.padEnd(34)} differing ${String(r.differing).padStart(7)} of ${r.compared}${r.masked ? ` (masked ${r.masked})` : ''}${r.box ? ' box ' + r.box.join(',') : ''}${r.detail ? ' ' + r.detail : ''}`),
  ...(extra.length ? ['extra in capture (not in baseline): ' + extra.join(', ')] : [])].join('\n') + '\n';
writeFileSync(join(outDir, 'summary.txt'), text);
process.stdout.write(text);
process.exit(failed.length ? 1 : 0);
