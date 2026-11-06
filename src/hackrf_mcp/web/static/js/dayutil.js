// Day plans: the day split into parts on a local clock, each with its own
// rotation (live_day.py has the rules). Pure functions for Live's day strip
// and the rotation editor's parts, run by tests/js/webui.test.mjs.

// "HH:MM" on a 24-hour clock to minutes after midnight; NaN if it isn't one.
export function parseStart(text) {
  const m = /^\s*(\d{1,2}):(\d{2})\s*$/.exec(String(text == null ? '' : text));
  if (!m) return NaN;
  const hh = Number(m[1]);
  const mm = Number(m[2]);
  return hh <= 23 && mm <= 59 ? hh * 60 + mm : NaN;
}

export function fmtStart(minutes) {
  const m = ((Math.round(minutes) % 1440) + 1440) % 1440;
  return `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
}

const FORMATS = new Map();
function format(zone, opts) {
  const key = `${zone}|${JSON.stringify(opts)}`;
  if (!FORMATS.has(key)) FORMATS.set(key, new Intl.DateTimeFormat('en-US', { timeZone: zone, ...opts }));
  return FORMATS.get(key);
}

// The plan's local clock at an instant: "HH:MM".
export function localTime(ts, zone) {
  const parts = format(zone, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(new Date(ts * 1000));
  const get = (t) => (parts.find((p) => p.type === t) || {}).value || '00';
  return `${get('hour')}:${get('minute')}`;
}

// The zone's short name at an instant (e.g. "CET" or "GMT+1"); the zone's
// own name if the browser has none.
export function zoneAbbr(ts, zone) {
  try {
    const p = format(zone, { timeZoneName: 'short' }).formatToParts(new Date(ts * 1000)).find((x) => x.type === 'timeZoneName');
    return p ? p.value : zone;
  } catch {
    return zone;
  }
}

// The segment of today's strip (status day.today.segments) holding now.
export function partAt(segments, now) {
  return (segments || []).find((s) => now >= s.from && now < s.to) || null;
}

// Today's strip: each part's stretch as a left offset and width (fractions
// of the local day), which one is current or past, and where now is.
export function strip(today, now) {
  const span = today.to - today.from;
  if (!(span > 0)) return { segs: [], fraction: 0 };
  const segs = today.segments.map((s) => ({
    ...s,
    left: (s.from - today.from) / span,
    width: (s.to - s.from) / span,
    cur: now >= s.from && now < s.to,
    past: s.to <= now,
  }));
  return { segs, fraction: Math.min(1, Math.max(0, (now - today.from) / span)) };
}

// Parts in order of start; ones without a valid start go last.
export function sortParts(parts) {
  const key = (p) => (Number.isNaN(parseStart(p.start)) ? 1e9 : parseStart(p.start));
  return parts.map((p, i) => ({ p, i })).sort((a, b) => key(a.p) - key(b.p) || a.i - b.i).map((x) => x.p);
}

// How many minutes each part lasts in a 24-hour day, in the order given
// (which must be sorted by start).
export function partMinutes(parts) {
  const starts = parts.map((p) => parseStart(p.start));
  return starts.map((s, i) => {
    const next = i + 1 < starts.length ? starts[i + 1] : starts[0] + 1440;
    return parts.length === 1 ? 1440 : next - s;
  });
}

// Each profile's share of a 24-hour day: every part starts at its first
// step and its last cycle is cut short by the next part. Largest first.
export function dayShare(parts) {
  const sorted = sortParts(parts);
  const lengths = partMinutes(sorted);
  const by = {};
  sorted.forEach((p, i) => {
    const L = lengths[i];
    const steps = p.steps || [];
    if (!steps.length || !(L > 0)) return;
    if (steps.length === 1) {
      by[steps[0].profile] = (by[steps[0].profile] || 0) + L;
      return;
    }
    let t = 0;
    while (t < L) {
      for (const s of steps) {
        const m = Math.max(0, Number(s.minutes) || 0);
        by[s.profile] = (by[s.profile] || 0) + Math.min(m, L - t);
        t += m;
        if (t >= L) break;
      }
      if (!steps.some((s) => Number(s.minutes) > 0)) break;
    }
  });
  const total = Object.values(by).reduce((a, b) => a + b, 0) || 1;
  return Object.fromEntries(Object.entries(by).sort((a, b) => b[1] - a[1]).map(([k, v]) => [k, v / total]));
}

// What is wrong with the parts of a draft plan, or null.
export function checkParts(parts) {
  if (parts.length < 2) return 'A day plan needs at least two parts.';
  const seen = new Map();
  const names = new Set();
  for (const p of parts) {
    const name = (p.name || '').trim();
    const m = parseStart(p.start);
    if (Number.isNaN(m)) return `${name || 'A part'} needs a start time like 23:00.`;
    if (seen.has(m)) return `${seen.get(m)} and ${name || 'another part'} both start at ${fmtStart(m)}.`;
    seen.set(m, name || fmtStart(m));
    if (!name) return `The part at ${fmtStart(m)} needs a name.`;
    if (names.has(name)) return `Two parts are called ${name}.`;
    names.add(name);
    if (!(p.steps || []).length) return `${name} needs at least one step.`;
  }
  return null;
}

// A start for a new part: twelve hours after `from`, or the next free hour.
export function freeStart(parts, from) {
  const taken = new Set(parts.map((p) => parseStart(p.start)));
  const base = Number.isNaN(parseStart(from)) ? 0 : parseStart(from);
  for (let k = 0; k < 24; k += 1) {
    const m = (Math.floor(base / 60) * 60 + 720 + k * 60) % 1440;
    if (!taken.has(m)) return fmtStart(m);
  }
  for (let m = 0; m < 1440; m += 1) if (!taken.has(m)) return fmtStart(m);
  return null;
}

// The profile a part spends most of its cycle on (the first step's on a
// tie), to colour the part in the day strip.
export function mainProfile(steps) {
  const by = new Map();
  for (const s of steps || []) by.set(s.profile, (by.get(s.profile) || 0) + (Number(s.minutes) || 0));
  let best = null;
  for (const [k, v] of by) if (best === null || v > by.get(best)) best = k;
  return best;
}

// Which of some tick positions (fractions, ascending) to label so that no
// two labels are closer than minGap: the first always, then each far
// enough from the last one kept.
export function spaced(fracs, minGap) {
  let last = -Infinity;
  return fracs.map((f) => {
    if (f - last < minGap) return false;
    last = f;
    return true;
  });
}
