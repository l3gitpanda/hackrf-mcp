// Pure helpers for the Map screen (tested with node --test).

export function targetKey(profile, subject) {
  return `${profile}|${subject}`;
}

// The last fix at or before `cursor`, if it is at most `maxAge` seconds old.
// `track` is sorted by time.
export function positionAt(track, cursor, maxAge = Infinity) {
  let lo = 0;
  let hi = track.length - 1;
  let best = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (track[mid].ts <= cursor) {
      best = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  if (best < 0) return null;
  const p = track[best];
  return cursor - p.ts <= maxAge ? p : null;
}

// The fixes with from <= ts <= to.
export function trackWindow(track, from, to) {
  return track.filter((p) => p.ts >= from && p.ts <= to);
}

// Where it points: the fix's own track, else the field the profile's map
// view names as its heading; null if neither is a usable angle.
export function heading(pos, fields, headingField) {
  const ok = (v) => typeof v === 'number' && isFinite(v) && v >= 0 && v < 360;
  if (pos && ok(pos.track)) return pos.track;
  const v = headingField && fields ? Number(fields[headingField]) : NaN;
  return ok(v) ? v : null;
}

// Drop fixes older than `since` (and targets left with none), so a map left
// open in live mode doesn't grow without end.
export function prune(targets, since) {
  for (const [key, t] of targets) {
    if (t.track.length && t.track[0].ts < since) t.track = t.track.filter((p) => p.ts >= since);
    if (!t.track.length && t.last < since) targets.delete(key);
  }
}
