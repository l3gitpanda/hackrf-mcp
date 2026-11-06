// Captures from a waterfall: picking a range on a band, the links that open
// the Live screen's capture form with a range filled in, and the words for a
// capture. Pure functions, for the tests (node --test). Decoder names come
// from the API; nothing here names a protocol.

// A pick across a band from fx0 to fx1 (0-1 across it): a range in Hz, or
// one frequency for a tap (narrower than `tapFrac` of the band).
export function pickRange(band, fx0, fx1, tapFrac = 0.006) {
  const clamp = (v) => Math.min(1, Math.max(0, v));
  const a = clamp(Math.min(fx0, fx1));
  const b = clamp(Math.max(fx0, fx1));
  const lo = band.center_hz - band.sample_rate_hz / 2;
  if (b - a < tapFrac) {
    const f = Math.round(lo + ((a + b) / 2) * band.sample_rate_hz);
    return { lo_hz: f, hi_hz: f };
  }
  return { lo_hz: Math.round(lo + a * band.sample_rate_hz), hi_hz: Math.round(lo + b * band.sample_rate_hz) };
}

// Where [lo, hi] sits across a band, in percent ({left, width}), at least
// `minPct` wide so a single frequency shows; null when it is all outside.
export function rangePct(band, lo, hi, minPct = 0.5) {
  if (!band || !band.sample_rate_hz) return null;
  const start = band.center_hz - band.sample_rate_hz / 2;
  let left = ((lo - start) / band.sample_rate_hz) * 100;
  let right = ((hi - start) / band.sample_rate_hz) * 100;
  if (right < 0 || left > 100) return null;
  if (right - left < minPct) {
    const mid = (left + right) / 2;
    left = mid - minPct / 2;
    right = mid + minPct / 2;
  }
  left = Math.max(0, left);
  right = Math.min(100, right);
  return { left, width: right - left };
}

const MHZ = (hz) => String(+(hz / 1e6).toFixed(6));

// The Live screen with its capture form filled in for [lo, hi] (nothing is
// recorded until it is confirmed there), or null outside what the HackRF
// tunes (`limits`: {min_hz, max_hz}, the registry's watch limits).
export function captureHref(lo, hi, origin, limits) {
  let a = Math.min(lo, hi);
  let b = Math.max(lo, hi);
  if (limits) {
    a = Math.max(a, limits.min_hz);
    b = Math.min(b, limits.max_hz);
    if (b < a) return null;
  }
  const q = new URLSearchParams({ capture_from: MHZ(a), capture_to: MHZ(b) });
  if (origin) q.set('origin', origin);
  return `/?${q}`;
}

// What captureHref put in the address: {lo_hz, hi_hz, origin}, or null.
export function captureQuery(search) {
  const u = new URLSearchParams(search);
  const from = parseFloat(u.get('capture_from'));
  if (!isFinite(from) || from <= 0) return null;
  const toRaw = parseFloat(u.get('capture_to'));
  const to = isFinite(toRaw) && toRaw > 0 ? toRaw : from;
  const origin = ['live', 'history', 'occupancy'].includes(u.get('origin')) ? u.get('origin') : 'live';
  return { lo_hz: Math.round(Math.min(from, to) * 1e6), hi_hz: Math.round(Math.max(from, to) * 1e6), origin };
}

// "152.0075 MHz" for one frequency, "88.1–91.3 MHz" for a range.
export function rangeLabel(lo, hi) {
  if (Math.abs(hi - lo) < 1) return `${(lo / 1e6).toFixed(4)} MHz`;
  return `${(lo / 1e6).toFixed(4)}–${(hi / 1e6).toFixed(4)} MHz`;
}

// Sizes as the owner was told them: decimal megabytes and gigabytes.
export function sizeText(bytes) {
  if (bytes == null || !isFinite(bytes)) return '—';
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(bytes >= 1e10 ? 0 : 1)} GB`;
  if (bytes >= 1e6) return `${Math.round(bytes / 1e6)} MB`;
  return `${Math.max(1, Math.round(bytes / 1e3))} kB`;
}

export function rateText(rate) {
  return rate % 1000 === 0 ? `${+(rate / 1e6).toFixed(3)} MS/s` : `${Math.round(rate).toLocaleString('en-US')} S/s`;
}

// A capture's state in a few words; `cur` is the live service's current
// capture ({id, state, left_s}) if any.
export function stateText(rec, cur) {
  if (cur && cur.id === rec.id) {
    if (cur.state === 'recording') return cur.left_s != null ? `Recording, ${Math.ceil(cur.left_s)} s left` : 'Recording';
    return 'Waiting for the receiver';
  }
  switch (rec.state) {
    case 'waiting':
      return 'Waiting for the receiver';
    case 'recording':
      return 'Recording';
    case 'decoding': {
      const n = (rec.decodes || []).filter((d) => d.state === 'queued' || d.state === 'running').length;
      return n ? `Decoding (${n} to go)` : 'Drawing the picture';
    }
    case 'done':
      return 'Done';
    case 'failed':
      return 'Failed';
    default:
      return rec.state || '—';
  }
}

// The tone of a state, for its tag: ok, warn or bad.
export function stateTone(rec) {
  if (rec.state === 'failed') return 'bad';
  if (rec.state === 'done') return (rec.decodes || []).some((d) => d.state === 'failed') ? 'warn' : 'ok';
  return 'warn';
}

// The decoders ticked in the form: the user's own choice once made, else
// what the plan suggested.
export function ticked(chosen, suggested) {
  return chosen != null ? chosen.slice() : (suggested || []).slice();
}

// The request body for a plan or a capture.
export function captureBody(form) {
  const body = { from_hz: form.lo_hz, to_hz: form.hi_hz, seconds: form.seconds, origin: form.origin || 'live' };
  if (form.chosen != null) body.decoders = form.chosen.slice();
  if (form.lna != null && form.lna !== '') body.lna_gain_db = Number(form.lna);
  if (form.vga != null && form.vga !== '') body.vga_gain_db = Number(form.vga);
  if (form.amp) body.amp = true;
  return body;
}
