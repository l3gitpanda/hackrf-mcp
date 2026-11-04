// Pure helpers for watching a custom range (tests/js/webui.test.mjs runs them).

// "88" / "88.5" / "162.400" (MHz) to Hz, or NaN.
export function parseMhz(text) {
  const t = String(text || '').trim().replace(',', '.');
  if (!/^\d+(\.\d+)?$/.test(t)) return NaN;
  return Math.round(Number(t) * 1e6);
}

// The tuning for watching [fromHz, toHz]: centred on it, at the narrowest
// allowed width that covers it, or the widest one centred on the start of a
// range too wide to see at once (scroll for the rest). Kept inside
// [minHz, maxHz]. Returns {center_hz, sample_rate_hz, clipped}.
export function watchTuning(fromHz, toHz, ratesHz, minHz, maxHz) {
  let lo = Math.min(fromHz, toHz);
  let hi = Math.max(fromHz, toHz);
  if (!(hi > lo)) hi = lo; // a single frequency: the narrowest width around it
  const rates = [...ratesHz].sort((a, b) => a - b);
  const rate = rates.find((r) => r >= hi - lo) || rates[rates.length - 1];
  const clipped = hi - lo > rate;
  let center = clipped ? lo + rate / 2 : (lo + hi) / 2;
  center = clampCenter(Math.round(center / 1000) * 1000, rate, minHz, maxHz);
  return { center_hz: center, sample_rate_hz: rate, clipped };
}

export function clampCenter(center, rate, minHz, maxHz) {
  return Math.min(Math.max(center, minHz + rate / 2), maxHz - rate / 2);
}

// Where a scroll of `steps` half-widths (negative: down) puts the centre,
// snapped to 25 kHz.
export function scrolled(center, rate, steps, minHz, maxHz) {
  const c = center + (steps * rate) / 2;
  return clampCenter(Math.round(c / 25000) * 25000, rate, minHz, maxHz);
}

// "11 min left" / "40 s left".
export function leftText(seconds) {
  if (!(seconds > 0)) return 'ending';
  return seconds >= 90 ? `${Math.round(seconds / 60)} min left` : `${Math.round(seconds)} s left`;
}

// The band plan as a list to pick from: every band the HackRF can tune
// (inside minHz-maxHz), lowest first (the wider first where two start
// together), with its category's label and colour. A query keeps the bands
// holding that frequency when it reads as one (in MHz, e.g. "915" or
// "162.4"), else those whose name, category, FCC part or note contain it.
export function bandList(plan, query, minHz, maxHz) {
  if (!plan || !plan.bands) return [];
  const cats = plan.categories || {};
  const q = String(query || '').trim().toLowerCase();
  const hz = q && /^[0-9.]+$/.test(q) ? parseMhz(q) : NaN;
  const out = [];
  for (const b of plan.bands) {
    if (!(b.hi > minHz && b.lo < maxHz)) continue;
    const cat = cats[b.cat] || {};
    const item = { name: b.name, lo: b.lo, hi: b.hi, cat: b.cat, label: cat.label || b.cat || '', color: cat.color || '#8a96a6', note: b.note || '' };
    if (q) {
      if (isFinite(hz)) {
        if (!(b.lo <= hz && hz <= b.hi)) continue;
      } else {
        const text = `${b.name} ${item.label} ${b.part ? `part ${b.part}` : ''} ${item.note}`.toLowerCase();
        if (!text.includes(q)) continue;
      }
    }
    out.push(item);
  }
  return out.sort((a, b) => a.lo - b.lo || b.hi - b.lo - (a.hi - a.lo));
}

// Which of a list's bands overlap lo-hi (the range in view): their indexes.
export function bandsInView(list, lo, hi) {
  const out = [];
  list.forEach((b, i) => {
    if (b.hi > lo && b.lo < hi) out.push(i);
  });
  return out;
}
