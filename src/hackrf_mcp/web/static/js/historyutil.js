// Pure helpers for the History screen (tests/js/webui.test.mjs runs them).

// A UTC day 'YYYY-MM-DD' as [start, end) in Unix seconds, or null.
export function dayBounds(day) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day || '');
  if (!m) return null;
  const t0 = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])) / 1000;
  return isFinite(t0) ? [t0, t0 + 86400] : null;
}

// The UTC day a time falls in, as 'YYYY-MM-DD'.
export function dayOf(ts) {
  return new Date(ts * 1000).toISOString().slice(0, 10);
}

// The rotation steps of [t0, t1): spectrum segments clipped to the day, in
// time order. A step still recording (ts_end null) runs to `now`.
export function stepsOf(segments, t0, t1, now) {
  return segments
    .map((s) => {
      const end = s.ts_end == null ? now : s.ts_end;
      return { id: s.id, profile: s.profile, seg: s, a: Math.max(s.ts_start, t0), b: Math.min(end, t1), live: s.ts_end == null };
    })
    .filter((s) => s.b > s.a)
    .sort((x, y) => x.a - y.a);
}

// The step holding `ts`, else the nearest one; -1 when there are none.
export function stepAt(steps, ts) {
  let best = -1;
  let dist = Infinity;
  steps.forEach((s, i) => {
    const d = ts < s.a ? s.a - ts : ts > s.b ? ts - s.b : 0;
    if (d < dist) {
      dist = d;
      best = i;
    }
  });
  return best;
}

// The gaps in coverage over [a, b]: every stretch that isn't "running",
// clipped to the range, with a label for the waterfall.
const GAP_LABEL = { paused: 'paused · lent to a device tool', error: 'error · retrying', idle: 'stopped', down: 'service down' };
export function gapsIn(coverage, a, b) {
  return coverage
    .filter((c) => c.state !== 'running' && c.to > a && c.from < b)
    .map((c) => ({ a: Math.max(c.from, a), b: Math.min(c.to, b), state: c.state, label: GAP_LABEL[c.state] || c.state }));
}

// Per time row: the strongest bin within `halfHz` of each channel, and the
// noise floor (the row's 20th percentile). `rows` hold dB per bin across
// [centerHz - rateHz/2, centerHz + rateHz/2]. Channels outside the band
// are left out.
export function channelLevels(rows, centerHz, rateHz, channelsHz, halfHz = 12500) {
  const lo = centerHz - rateHz / 2;
  const chans = channelsHz.filter((f) => f >= lo && f <= lo + rateHz);
  const out = { channels: chans, levels: chans.map(() => []), floor: [] };
  for (const row of rows) {
    const n = row.length;
    chans.forEach((f, ci) => {
      const a = Math.max(0, Math.floor(((f - halfHz - lo) / rateHz) * n));
      const b = Math.min(n - 1, Math.ceil(((f + halfHz - lo) / rateHz) * n));
      let m = -Infinity;
      for (let i = a; i <= b; i++) if (row[i] > m) m = row[i];
      out.levels[ci].push(m);
    });
    const sorted = Float32Array.from(row).sort();
    out.floor.push(sorted[Math.floor(0.2 * (sorted.length - 1))]);
  }
  return out;
}

// SVG path data for values over times on a w x h box, with y running from
// `top` dB at the top to `bottom` dB at the foot; the pen lifts where two
// samples are more than `breakS` apart (a gap in the recording).
export function pathOf(times, values, a, b, top, bottom, w, h, breakS) {
  let d = '';
  let prev = null;
  for (let i = 0; i < times.length; i++) {
    const v = values[i];
    if (!isFinite(v)) {
      prev = null;
      continue;
    }
    const x = ((times[i] - a) / (b - a)) * w;
    const y = ((top - v) / (top - bottom)) * h;
    const lift = prev === null || times[i] - prev > breakS;
    d += `${lift ? 'M' : 'L'}${x.toFixed(1)},${Math.min(h, Math.max(0, y)).toFixed(1)} `;
    prev = times[i];
  }
  return d.trim();
}

// Time labels for [a, b]: a step that gives at most `most` of them.
export function timeTicks(a, b, most = 10) {
  const steps = [30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600];
  const step = steps.find((s) => (b - a) / s <= most) || 43200;
  const out = [];
  for (let t = Math.ceil(a / step) * step; t <= b; t += step) out.push(t);
  return { step, ticks: out };
}
