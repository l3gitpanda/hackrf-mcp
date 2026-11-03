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
