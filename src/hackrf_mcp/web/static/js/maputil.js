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

// --- Labels that don't overlap -------------------------------------------------------
// A busy approach at the zoom that fits the whole area piles labels on top
// of each other. Labels are placed in order of importance and one that
// would overlap a label already placed is left out (its marker stays; the
// label shows when the pointer is on it).

// The box a marker's label takes on screen, from the CSS (.mk .tl2: 11 px
// to the right of the point and 8 px up, 600 11px/1.2 monospace, a 10 px
// second line). Monospace digits are about 0.6 em wide.
export function labelBox(text, sub, x, y) {
  const w = Math.max(String(text || '').length * 6.7, String(sub || '').length * 6.1) + 4;
  return { x: x + 11, y: y - 8, w, h: sub ? 26 : 14 };
}

// Placing order: the selected target, then those with an alert, then
// those moving with a fresh fix, then the most recently heard.
export function labelRank({ selected = false, alerted = false, moving = false, fresh = false, ts = 0 }) {
  return (selected ? 8 : 0) + (alerted ? 4 : 0) + (moving ? 2 : 0) + (fresh ? 1 : 0) + Math.min(ts, 4e9) / 4e9;
}

// The keys of the labels to leave out so that no two overlap: `boxes` are
// {key, x, y, w, h, rank} in screen pixels; higher ranks are placed first.
export function declutter(boxes, pad = 2) {
  const cell = 64;
  const grid = new Map();
  const hidden = new Set();
  const span = (a, len) => [Math.floor((a - pad) / cell), Math.floor((a + len + pad) / cell)];
  const clash = (a, b) => a.x < b.x + b.w + pad && b.x < a.x + a.w + pad && a.y < b.y + b.h + pad && b.y < a.y + a.h + pad;
  for (const b of boxes.slice().sort((p, q) => q.rank - p.rank)) {
    const [x0, x1] = span(b.x, b.w);
    const [y0, y1] = span(b.y, b.h);
    let hit = false;
    for (let gx = x0; gx <= x1 && !hit; gx++) {
      for (let gy = y0; gy <= y1 && !hit; gy++) {
        for (const o of grid.get(`${gx},${gy}`) || []) {
          if (clash(b, o)) {
            hit = true;
            break;
          }
        }
      }
    }
    if (hit) {
      hidden.add(b.key);
      continue;
    }
    for (let gx = x0; gx <= x1; gx++) {
      for (let gy = y0; gy <= y1; gy++) {
        const k = `${gx},${gy}`;
        if (!grid.has(k)) grid.set(k, []);
        grid.get(k).push(b);
      }
    }
  }
  return hidden;
}
