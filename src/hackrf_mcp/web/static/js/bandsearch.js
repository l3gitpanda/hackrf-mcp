// Searching the band plan and the official allocations (the Bands screen).
// Pure functions; tests/js/webui.test.mjs runs them.

const UNIT = { k: 1e3, khz: 1e3, m: 1e6, mhz: 1e6, g: 1e9, ghz: 1e9 };

// A frequency or a range in the query: "162.4", "162.4 MHz", "1.09 GHz",
// "535 kHz", "88-108", "88 to 108 MHz" (MHz unless a unit says otherwise).
// Returns {lo, hi} in Hz (lo == hi for one frequency) and the rest of the
// query as text, or freq null.
export function parseQuery(q) {
  let text = String(q || '').trim().toLowerCase();
  // "part 97" is an FCC rule part, not 97 MHz.
  const parts = [];
  text = text.replace(/\bpart\s+(\d+[a-z]?)\b/g, (_, n) => {
    parts.push(n);
    return ' ';
  });
  const r = parseFreq(text);
  return { freq: r.freq, words: [...r.words, ...parts] };
}

function parseFreq(text) {
  const num = '(\\d+(?:[.,]\\d+)?)';
  const unit = '\\s*(khz|mhz|ghz|k|m|g)?';
  const range = new RegExp(`(?:^|\\s)${num}${unit}\\s*(?:-|–|to)\\s*${num}${unit}(?=\\s|$)`);
  const one = new RegExp(`(?:^|\\s)${num}${unit}(?=\\s|$)`);
  const hz = (n, u) => Number(n.replace(',', '.')) * (UNIT[u || 'mhz'] || 1e6);
  let m = range.exec(text);
  if (m) {
    const u = m[4] || m[2];
    const a = hz(m[1], m[2] || u);
    const b = hz(m[3], u);
    return { freq: { lo: Math.min(a, b), hi: Math.max(a, b) }, words: words(text.replace(m[0], ' ')) };
  }
  m = one.exec(text);
  if (m) {
    const f = hz(m[1], m[2]);
    return { freq: { lo: f, hi: f }, words: words(text.replace(m[0], ' ')) };
  }
  return { freq: null, words: words(text) };
}

function words(t) {
  return t.split(/\s+/).filter(Boolean);
}

function overlaps(b, freq) {
  return freq.lo === freq.hi ? b.lo <= freq.lo && freq.lo < b.hi : b.lo < freq.hi && freq.lo < b.hi;
}

// The plan's bands and the official table's bands matching the query, in
// frequency order. `cats` (a Set) limits the plan's bands to those
// categories when it isn't empty. Every word must appear somewhere in the
// band's text (name, note, category, FCC part; services and parts for the
// official ones).
export function search(plan, official, q, cats = new Set()) {
  const { freq, words: ws } = parseQuery(q);
  const has = (hay) => ws.every((w) => hay.includes(w));
  const bands = (plan ? plan.bands : [])
    .filter((b) => !cats.size || cats.has(b.cat))
    .filter((b) => !freq || overlaps(b, freq))
    .filter((b) => {
      const cat = (plan.categories[b.cat] || {}).label || '';
      return has([b.name, b.note || '', cat, b.cat, b.part ? `part ${b.part}` : ''].join(' ').toLowerCase());
    });
  const off = [];
  if (official && !cats.size && (freq || ws.length)) {
    for (const [col, label] of [['non_federal', 'Non-Federal'], ['federal', 'Federal']]) {
      for (const b of official[col]) {
        if (freq && !overlaps(b, freq)) continue;
        if (!b.services.length && !(b.parts || []).length) continue;
        if (has([...b.services, ...(b.parts || []), label].join(' ').toLowerCase())) off.push({ ...b, col: label });
      }
    }
    off.sort((a, b) => a.lo - b.lo || (a.col < b.col ? -1 : 1));
  }
  return { freq, bands, official: off };
}
