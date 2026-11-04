// Pure helpers for the Summaries screen (tests/js/webui.test.mjs runs them).

import { parseUtc } from './util.js';

// [since, until] for the range picker: seconds back from now, "today"
// (since UTC midnight) or a typed UTC from-to.
export function rangeOf(value, now, fromText = '', toText = '') {
  let since;
  let until = now;
  if (value === 'today') {
    since = Math.floor(now / 86400) * 86400;
  } else if (value === 'custom') {
    since = parseUtc(fromText);
    if (String(toText).trim()) until = parseUtc(toText);
  } else {
    const back = Number(value);
    if (!(back > 0)) throw new Error(`unknown range ${value}`);
    since = now - back;
  }
  if (!(since < until)) throw new Error('"From" must be before "To".');
  return [since, until];
}

// How many rate buckets a range gets: the first round width that keeps them
// to about 120 (a day is 96 of 15 minutes, as in the mockup).
const WIDTHS = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400];
export function bucketsFor(span) {
  const w = WIDTHS.find((s) => span / s <= 120) || WIDTHS[WIDTHS.length - 1];
  return Math.max(1, Math.min(500, Math.round(span / w)));
}

// Seconds on the air per profile, and in each kind of gap, over [a, b].
export function onAir(coverage, a, b) {
  const profiles = {};
  const gaps = {};
  for (const c of coverage || []) {
    const s = Math.max(0, Math.min(c.to, b) - Math.max(c.from, a));
    if (!s) continue;
    if (c.state === 'running') profiles[c.profile] = (profiles[c.profile] || 0) + s;
    else gaps[c.state] = (gaps[c.state] || 0) + s;
  }
  return { profiles, gaps };
}

// The stretches one profile was on the air, as [x, width] on a 0..w line.
export function runsOf(coverage, profile, a, b, w) {
  const out = [];
  for (const c of coverage || []) {
    if (c.state !== 'running' || c.profile !== profile) continue;
    const x0 = Math.max(c.from, a);
    const x1 = Math.min(c.to, b);
    if (x1 > x0) out.push([((x0 - a) / (b - a)) * w, ((x1 - x0) / (b - a)) * w]);
  }
  return out;
}

// "45 s", "8 m", "7 h 0 m", "2 d 3 h".
export function durText(s) {
  if (!(s > 0)) return '0 m';
  if (s < 60) return `${Math.round(s)} s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} m`;
  if (m < 2880) return `${Math.floor(m / 60)} h ${m % 60} m`;
  const hrs = Math.round(m / 60);
  return `${Math.floor(hrs / 24)} d ${hrs % 24} h`;
}

// A step-area path for `values` on a w x h box, scaled so `max` reaches
// two pixels below the top; zero sits on the baseline.
export function areaPath(values, w, h, max) {
  const n = values.length;
  if (!n) return '';
  const top = Math.max(max, 1);
  let d = `M0,${h}`;
  values.forEach((v, i) => {
    const y = (h - (Math.max(0, v) / top) * (h - 2)).toFixed(1);
    d += ` L${((i / n) * w).toFixed(1)},${y} L${(((i + 1) / n) * w).toFixed(1)},${y}`;
  });
  return `${d} L${w},${h} Z`;
}

// Times to label under a strip `px` wide: both ends, and round times
// between them (from `timeTicks`) no closer than `gap` pixels to a label.
export function tickTimes(a, b, px, gap, timeTicks) {
  const most = Math.max(1, Math.floor(px / gap) - 1);
  const room = gap / Math.max(px, 1);
  const inner = timeTicks(a, b, most).ticks.filter((t) => (t - a) / (b - a) > room && (b - t) / (b - a) > room);
  return px >= 2 * gap ? [a, ...inner, b] : [a, b];
}

// The columns left after dropping those that only ever repeat a subject's
// label (`top` holds the subjects, each with labels and fields).
export function dropEchoes(cols, top) {
  return cols.filter((f) => {
    const seen = top.filter((e) => e.fields[f] != null && e.fields[f] !== '');
    return !seen.length || seen.some((e) => String(e.fields[f]) !== (e.labels[0] || ''));
  });
}

// The columns of a profile's busiest-subjects table: its table view's
// fields (else its summary fields), without the position and the counts
// the table shows on its own.
const OWN = new Set(['lat', 'lon', 'messages', 'positions']);
export function columnsOf(profile) {
  const table = (profile.views || []).find((v) => v.name === 'table');
  const fields = table && table.fields.length ? table.fields : profile.summary_fields || [];
  return fields.filter((f) => !OWN.has(f));
}

// "vessel 2 · aid to navigation 2 · base station 1", most first.
export function kindsText(byKind) {
  return Object.entries(byKind || {})
    .sort((x, y) => y[1] - x[1] || x[0].localeCompare(y[0]))
    .map(([k, n]) => `${k.replace(/-/g, ' ')} ${n.toLocaleString('en-US')}`)
    .join(' · ');
}

// A field's value for a table cell.
export function fieldText(v) {
  if (v == null || v === '') return '—';
  if (Array.isArray(v)) return v.join(', ');
  if (typeof v === 'number') return Number.isInteger(v) ? v.toLocaleString('en-US') : String(Math.round(v * 10) / 10);
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

// The heat map's cell for a zoom: about `px` screen pixels wide, from a
// ladder of round sizes, never finer than the server allows.
const CELLS = [0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5];
export function cellFor(zoom, px = 6) {
  const want = (px * 360) / (256 * 2 ** zoom);
  return CELLS.find((c) => c >= want) || CELLS[CELLS.length - 1];
}

// Class edges for a heat map whose largest count is `max`: up to `n`
// classes, evenly spaced on a log scale, as whole-number upper bounds.
export function heatBins(max, n = 6) {
  const top = Math.max(1, Math.round(max));
  const out = [];
  for (let i = 1; i <= n; i++) {
    const edge = Math.round(Math.exp((i / n) * Math.log(top + 1)) - 1);
    if (edge >= 1 && (!out.length || edge > out[out.length - 1])) out.push(edge);
  }
  if (out[out.length - 1] !== top) out.push(top);
  return out;
}

// The class of a count: the first edge it doesn't exceed.
export function binOf(v, edges) {
  for (let i = 0; i < edges.length; i++) if (v <= edges[i]) return i;
  return edges.length - 1;
}

// "1", "2–3", "4–9", ... for a legend.
export function binLabels(edges) {
  return edges.map((e, i) => {
    const lo = i ? edges[i - 1] + 1 : 1;
    return lo >= e ? e.toLocaleString('en-US') : `${lo.toLocaleString('en-US')}–${e.toLocaleString('en-US')}`;
  });
}

// Opacity of class i of n over the dark map: dim to full, one hue.
export function binAlpha(i, n) {
  return n <= 1 ? 0.9 : 0.22 + (0.9 - 0.22) * (i / (n - 1));
}

// Web Mercator y of a latitude (radians of the projection), so a grid in
// degrees can be drawn on an image Leaflet stretches linearly.
export function mercY(lat) {
  const r = (Math.max(-85.0511, Math.min(85.0511, lat)) * Math.PI) / 180;
  return Math.log(Math.tan(Math.PI / 4 + r / 2));
}

// Where a grid's cells go on an image: its extent in degrees, the image
// size (at most `maxPx` on a side) and a function giving each cell's
// rectangle [x, y, w, h] in image pixels.
export function gridImage(cells, cell, maxPx = 2048) {
  if (!cells.length) return null;
  let x0 = Infinity;
  let x1 = -Infinity;
  let y0 = Infinity;
  let y1 = -Infinity;
  for (const [x, y] of cells) {
    if (x < x0) x0 = x;
    if (x > x1) x1 = x;
    if (y < y0) y0 = y;
    if (y > y1) y1 = y;
  }
  const west = x0 * cell - 180;
  const east = (x1 + 1) * cell - 180;
  const south = y0 * cell - 90;
  const north = (y1 + 1) * cell - 90;
  const yn = mercY(north);
  const ys = mercY(south);
  const lonRad = ((east - west) * Math.PI) / 180;
  const cols = x1 - x0 + 1;
  // Up to 16 pixels a cell, so the rows' Mercator heights round finely.
  let width = Math.min(maxPx, cols * Math.max(1, Math.min(16, Math.floor(maxPx / cols))));
  let height = Math.max(1, Math.round((width * (yn - ys)) / lonRad));
  if (height > maxPx) {
    width = Math.max(1, Math.round((width * maxPx) / height));
    height = maxPx;
  }
  const cw = width / cols;
  // Edges on whole pixels, so neighbouring cells meet without a seam.
  const rect = (x, y) => {
    const left = Math.round((x - x0) * cw);
    const right = Math.max(left + 1, Math.round((x - x0 + 1) * cw));
    const top = Math.round(((yn - mercY((y + 1) * cell - 90)) / (yn - ys)) * height);
    const bottom = Math.max(top + 1, Math.round(((yn - mercY(y * cell - 90)) / (yn - ys)) * height));
    return [left, top, right - left, bottom - top];
  };
  return { west, east, south, north, width, height, rect };
}
