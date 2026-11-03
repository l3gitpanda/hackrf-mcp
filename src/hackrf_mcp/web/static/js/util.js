// Small helpers shared by the screens. Pure functions are exported for the
// tests (node --test); nothing here touches the DOM when imported.

// Profile colours: the mockup's, in registry order, then more. A profile's
// own `color` (Profile.color) wins.
export const PALETTE = [
  '#5cb8ff', '#3fd0c0', '#a98bff', '#e483d6', '#8fd46a', '#d8d65a', '#ff9d5c', '#ff6474',
  '#7fd1ff', '#c9a3ff', '#ffc46b', '#9be3a1', '#f39cc4', '#b4c8ff',
];

export function assignColors(profiles) {
  const used = new Set(profiles.map((p) => p.color).filter(Boolean));
  const free = PALETTE.filter((c) => !used.has(c));
  const out = {};
  let i = 0;
  for (const p of profiles) out[p.name] = p.color || free[i++ % free.length] || '#8a96a6';
  return out;
}

export function mhz(hz, digits = 4) {
  if (hz == null || !isFinite(hz)) return '—';
  return (hz / 1e6).toFixed(digits);
}

// "1090.0000" -> shortest form that still tells channels apart.
export function mhzShort(hz) {
  const s = (hz / 1e6).toFixed(4);
  return s.replace(/0+$/, '').replace(/\.$/, '');
}

export function utc(ts, withSeconds = true) {
  if (ts == null) return '—';
  const s = new Date(ts * 1000).toISOString();
  return withSeconds ? s.slice(11, 19) : s.slice(11, 16);
}

// "2026-11-02 18:00" for a Unix time (UTC, to the minute).
export function fmtUtc(ts) {
  return new Date(ts * 1000).toISOString().slice(0, 16).replace('T', ' ');
}

// "2026-11-02 18:00" (UTC), seconds optional; a date alone is midnight.
export function parseUtc(text) {
  const m = /^\s*(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?\s*Z?\s*$/.exec(text);
  if (!m) throw new Error(`"${text}" is not a UTC time like 2026-11-02 18:00`);
  return Date.UTC(+m[1], +m[2] - 1, +m[3], +(m[4] || 0), +(m[5] || 0), +(m[6] || 0)) / 1000;
}

export function ago(ts, now) {
  if (ts == null) return 'never';
  const s = Math.max(0, now - ts);
  if (s < 90) return `${Math.round(s)} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  return `${(s / 3600).toFixed(1)} h ago`;
}

export function inSeconds(ts, now) {
  if (ts == null) return '';
  const s = Math.max(0, ts - now);
  if (s < 90) return `${Math.round(s)} s`;
  return `${Math.round(s / 60)} min`;
}

export function mib(bytes) {
  return bytes == null ? '—' : (bytes / 1048576).toFixed(1);
}

// Channels typed as "931.75, 931.9375" (MHz) -> [931750000, 931937500].
export function parseFreqs(text) {
  const out = [];
  for (const part of String(text || '').split(/[\s,;]+/)) {
    if (!part) continue;
    const v = Number(part);
    if (!isFinite(v) || v <= 0) throw new Error(`"${part}" is not a frequency in MHz`);
    out.push(Math.round(v * 1e6));
  }
  return out;
}

// A spectrum row from the feed or the history API: base64 of one byte per
// bin, each `dbMin + byte * dbStep` dBFS.
export function decodeRow(b64, dbMin, dbStep) {
  const bin = typeof atob === 'function' ? atob(b64) : Buffer.from(b64, 'base64').toString('binary');
  const out = new Float32Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = dbMin + bin.charCodeAt(i) * dbStep;
  return out;
}

// The value below which `q` of the values fall (q in 0..1).
export function quantile(values, q) {
  const a = Float32Array.from(values).sort();
  if (!a.length) return NaN;
  return a[Math.min(a.length - 1, Math.max(0, Math.floor(q * (a.length - 1))))];
}

// Rotation geometry: where `now` is in the cycle, and each step's span.
export function rotation(schedule, anchor, now) {
  const total = schedule.reduce((s, e) => s + e.minutes * 60, 0);
  if (!total) return null;
  const pos = (((now - anchor) % total) + total) % total;
  const start = now - pos;
  let t = 0;
  const steps = schedule.map((e, i) => {
    const s = { ...e, index: i, from: start + t, to: start + t + e.minutes * 60, share: (e.minutes * 60) / total };
    t += e.minutes * 60;
    return s;
  });
  const current = steps.find((s) => now >= s.from && now < s.to) || steps[steps.length - 1];
  return { total, pos, start, steps, current, fraction: pos / total };
}

// Element builder: h('div', {class: 'x', onClick: fn, '--pc': '#fff'}, children...).
export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('--')) el.style.setProperty(k, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else el.setAttribute(k, v === true ? '' : String(v));
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : String(c));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}
