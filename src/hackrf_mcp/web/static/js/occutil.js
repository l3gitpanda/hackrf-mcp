// Pure helpers of the Occupancy screen (tests/js/webui.test.mjs runs them).
// The data is /api/occupancy*'s: one byte per cell, dbMin + byte * dbStep
// dBFS, byte 0 where nothing was measured.

// The ranges the heat map shows, and how many time buckets each asks for:
// one per 15-minute slot up to a week, an hour a bucket for a month, the
// daily roll-ups for a year.
export const RANGES = [
  { key: '24h', label: 'Last 24 hours', s: 86400, tb: 96 },
  { key: '7d', label: 'Last 7 days', s: 7 * 86400, tb: 672 },
  { key: '30d', label: 'Last 30 days', s: 30 * 86400, tb: 720 },
  { key: '365d', label: 'Last year', s: 365 * 86400, tb: 365 },
];

export const PROFILE_DAYS = [7, 30, 90];
export const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

export function bytesOf(b64) {
  const bin = typeof atob === 'function' ? atob(b64 || '') : Buffer.from(b64 || '', 'base64').toString('binary');
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

// For each of the tb time buckets of a grid answer, the row of its data
// (the answer leaves empty buckets out), or -1.
export function rowMap(grid, tb) {
  const out = new Int32Array(tb).fill(-1);
  (grid.ts || []).forEach((t, k) => {
    const i = Math.round((t - grid.since) / grid.bucket_s);
    if (i >= 0 && i < tb) out[i] = k;
  });
  return out;
}

// Colour levels in dBFS: the bottom a little under the noise floor (the
// 20th percentile of what was measured), `span` dB up to the top, which
// stops at full scale.
export function levels(bytes, dbMin, dbStep, span = 50) {
  const hist = new Uint32Array(256);
  let n = 0;
  for (let i = 0; i < bytes.length; i++) {
    if (bytes[i]) {
      hist[bytes[i]]++;
      n++;
    }
  }
  if (!n) return { lo: -100, hi: -50 };
  let want = Math.floor(n * 0.2);
  let b = 1;
  for (; b < 256; b++) {
    want -= hist[b];
    if (want < 0) break;
  }
  const floor = dbMin + Math.min(b, 255) * dbStep;
  const hi = Math.min(0, floor - 6 + span);
  return { lo: Math.min(floor - 6, hi - 20), hi };
}

// RGBA pixels, nb wide and tb high, newest bucket at the top: each cell's
// byte through `lut` (256 RGB triplets) between lo and hi dBFS; empty
// buckets and unmeasured cells in `empty`.
export function paint(bytes, nb, rows, tb, lut, lo, hi, dbMin, dbStep, empty = [26, 31, 39]) {
  const px = new Uint8ClampedArray(nb * tb * 4);
  const idx = new Uint8Array(256);
  for (let b = 0; b < 256; b++) {
    const v = (dbMin + b * dbStep - lo) / (hi - lo);
    idx[b] = Math.round(Math.min(1, Math.max(0, v)) * 255);
  }
  for (let i = 0; i < tb; i++) {
    const y = tb - 1 - i;
    const k = rows[i];
    for (let x = 0; x < nb; x++) {
      const o = (y * nb + x) * 4;
      const b = k < 0 ? 0 : bytes[k * nb + x];
      if (!b) {
        px[o] = empty[0];
        px[o + 1] = empty[1];
        px[o + 2] = empty[2];
      } else {
        const c = idx[b] * 3;
        px[o] = lut[c];
        px[o + 1] = lut[c + 1];
        px[o + 2] = lut[c + 2];
      }
      px[o + 3] = 255;
    }
  }
  return px;
}

// Frequency labels for [lo, hi] Hz, at most `most`, on a 1-2-5 step.
export function freqTicks(lo, hi, most = 10) {
  const span = (hi - lo) / 1e6;
  const steps = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000];
  const step = steps.find((s) => span / s <= most) || 2000;
  const digits = (String(step).split('.')[1] || '').length;
  const out = [];
  for (let k = Math.ceil(lo / 1e6 / step); k * step <= hi / 1e6 + 1e-9; k++) {
    const f = k * step;
    out.push({ hz: f * 1e6, pct: ((f * 1e6 - lo) / (hi - lo)) * 100, label: f.toFixed(digits) });
  }
  return out;
}

// The frequency span a grid answer covers.
export function spanOf(grid) {
  const lo = grid.f_lo_hz;
  return { lo, hi: Math.min(lo + grid.fbuckets * grid.bucket_hz, grid.grid.hi_hz) };
}

// The cell under a point of the plot (x and y as fractions, y from the
// top): its buckets and its time and frequency edges.
export function cellAt(grid, tb, x01, y01) {
  const fi = Math.min(grid.fbuckets - 1, Math.max(0, Math.floor(x01 * grid.fbuckets)));
  const ti = tb - 1 - Math.min(tb - 1, Math.max(0, Math.floor(y01 * tb)));
  const t0 = grid.since + ti * grid.bucket_s;
  const f0 = grid.f_lo_hz + fi * grid.bucket_hz;
  return { ti, fi, t0, t1: t0 + grid.bucket_s, f0, f1: Math.min(f0 + grid.bucket_hz, grid.grid.hi_hz) };
}

// A cell's level in dBFS, or null where nothing was measured.
export function cellLevel(bytes, rows, cell, nb, dbMin, dbStep) {
  const k = rows[cell.ti];
  if (k == null || k < 0) return null;
  const b = bytes[k * nb + cell.fi];
  return b ? dbMin + b * dbStep : null;
}

// A drag across the plot (x fractions) as a frequency range, whole buckets,
// at least two of them; null for a drag too short to mean a zoom.
export function zoomRange(grid, x0, x1) {
  let a = Math.floor(Math.min(x0, x1) * grid.fbuckets);
  let b = Math.ceil(Math.max(x0, x1) * grid.fbuckets);
  a = Math.max(0, a);
  b = Math.min(grid.fbuckets, b);
  if (b - a < 2) return null;
  return { fmin: grid.f_lo_hz + a * grid.bucket_hz, fmax: Math.min(grid.f_lo_hz + b * grid.bucket_hz, grid.grid.hi_hz) };
}

// The Live screen with "Watch a range" filled in for [f0, f1] (taking the
// receiver is still a click there), or null outside what the HackRF tunes.
export function watchHref(f0, f1, watch) {
  if (!watch) return null;
  const a = Math.max(f0, watch.min_hz);
  const b = Math.min(f1, watch.max_hz);
  if (!(b > a)) return null;
  const q = new URLSearchParams({ watch_from: String(+(a / 1e6).toFixed(4)), watch_to: String(+(b / 1e6).toFixed(4)) });
  return `/?${q}`;
}

// The Log over [t0, t1] (a quarter of an hour either side) for rows within
// [f0, f1].
export function logHref(f0, f1, t0, t1) {
  const q = new URLSearchParams({
    since: String(Math.round(t0 - 900)),
    until: String(Math.round(t1 + 900)),
    freq: ((f0 + f1) / 2 / 1e6).toFixed(4),
    tol: String(Math.max(12500, Math.round((f1 - f0) / 2))),
  });
  return `/log?${q}`;
}

export function dbText(v) {
  return v == null || !isFinite(v) ? '—' : `${v.toFixed(1)} dBFS`;
}

// "Every 15 min on the UTC clock · 24–1704 MHz · 20 passes at 98.0 kHz · LNA
// 24 dB, VGA 40 dB, amp off", from the live service's occupancy status.
export function scheduleText(oc) {
  if (!oc || !oc.enabled) return null;
  const [lo, hi] = oc.range_mhz || [0, 0];
  return (
    `Every ${oc.interval_min} min on the UTC clock · ${lo}–${hi} MHz · ${oc.passes} passes at ${(oc.bin_hz / 1e3).toFixed(1)} kHz` +
    ` · LNA ${oc.lna_gain_db} dB, VGA ${oc.vga_gain_db} dB, amp off`
  );
}

// The unusual items of many slots (newest first, as /api/occupancy/unusual
// lists them) as one entry per signal: items whose ranges overlap, or touch
// within `gapHz`, join one group, with its whole range, when it was first
// and last seen, in how many slots, and its strongest item. Newest last
// seen first; the biggest excess first among those seen in the same slot.
export function groupUnusual(items, gapHz = 0) {
  const groups = [];
  for (const it of items) {
    const g = groups.find((x) => it.lo_hz <= x.hi_hz + gapHz && it.hi_hz >= x.lo_hz - gapHz);
    if (!g) {
      groups.push({ lo_hz: it.lo_hz, hi_hz: it.hi_hz, first: it.ts, last: it.ts, slots: new Set([it.slot]), best: it, latest: it });
      continue;
    }
    g.lo_hz = Math.min(g.lo_hz, it.lo_hz);
    g.hi_hz = Math.max(g.hi_hz, it.hi_hz);
    g.first = Math.min(g.first, it.ts);
    if (it.ts > g.last) {
      g.last = it.ts;
      g.latest = it;
    }
    g.slots.add(it.slot);
    if (it.excess_db > g.best.excess_db) g.best = it;
  }
  return groups
    .map((g) => ({ ...g, slots: g.slots.size }))
    .sort((a, b) => b.last - a.last || b.best.excess_db - a.best.excess_db);
}
