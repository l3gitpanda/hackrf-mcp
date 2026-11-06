// Listening: the pure parts, for the tests (node --test). Frames off
// /ws/audio, mu-law, clip files, resampling, when to play the next piece,
// and the channels. The live service makes the audio (listen.py); the
// browser gets audio and levels only. Nothing here names a protocol.

export const AUDIO = 1;
export const METER = 2;
export const DROPPED = 3;
export const FLAG_OPEN = 1;
export const FLAG_ACTIVE = 2;
export const FLAG_RECORDING = 4;
// listen.FRAME: magic, type, channel, flags, rate, seq, ts, level, threshold, floor.
export const HEADER_BYTES = 36;
const MAGIC = 0x484b4131; // "HKA1"

// The profile name the live service logs clips (and its window) under.
export const LISTEN = 'listen';

export function parseFrame(buf) {
  if (!buf || buf.byteLength < HEADER_BYTES) return null;
  const v = new DataView(buf);
  if (v.getUint32(0, false) !== MAGIC) return null;
  return {
    type: v.getUint8(4),
    channel: v.getUint8(5),
    flags: v.getUint16(6, true),
    rate: v.getUint32(8, true),
    seq: v.getUint32(12, true),
    ts: v.getFloat64(16, true),
    level_db: v.getFloat32(24, true),
    threshold_db: v.getFloat32(28, true),
    floor_db: v.getFloat32(32, true),
    payload: new Uint8Array(buf, HEADER_BYTES),
  };
}

// G.711 mu-law to [-1, 1].
export const ULAW = (() => {
  const t = new Float32Array(256);
  for (let i = 0; i < 256; i++) {
    const u = ~i & 0xff;
    const mag = ((((u & 0x0f) << 3) + 0x84) << ((u >> 4) & 0x07)) - 0x84;
    t[i] = (u & 0x80 ? -mag : mag) / 32768;
  }
  return t;
})();

export function ulawDecode(bytes) {
  const out = new Float32Array(bytes.length);
  for (let i = 0; i < bytes.length; i++) out[i] = ULAW[bytes[i]];
  return out;
}

// A clip file: mono WAV, mu-law (format 7) or 8/16-bit PCM. Returns
// {rate, samples} or null.
export function parseWav(buf) {
  const v = new DataView(buf);
  const tag = (o) => String.fromCharCode(v.getUint8(o), v.getUint8(o + 1), v.getUint8(o + 2), v.getUint8(o + 3));
  if (buf.byteLength < 12 || tag(0) !== 'RIFF' || tag(8) !== 'WAVE') return null;
  let fmt = null;
  let o = 12;
  while (o + 8 <= buf.byteLength) {
    const id = tag(o);
    const size = v.getUint32(o + 4, true);
    const body = o + 8;
    if (id === 'fmt ') fmt = { format: v.getUint16(body, true), channels: v.getUint16(body + 2, true), rate: v.getUint32(body + 4, true), bits: v.getUint16(body + 14, true) };
    if (id === 'data' && fmt && fmt.channels === 1) {
      const n = Math.min(size, buf.byteLength - body);
      const bytes = new Uint8Array(buf, body, n);
      if (fmt.format === 7) return { rate: fmt.rate, samples: ulawDecode(bytes) };
      if (fmt.format === 1 && fmt.bits === 8) return { rate: fmt.rate, samples: Float32Array.from(bytes, (b) => (b - 128) / 128) };
      if (fmt.format === 1 && fmt.bits === 16) {
        const out = new Float32Array(n >> 1);
        for (let i = 0; i < out.length; i++) out[i] = v.getInt16(body + 2 * i, true) / 32768;
        return { rate: fmt.rate, samples: out };
      }
      return null;
    }
    o = body + size + (size % 2);
  }
  return null;
}

// Linear interpolation from one rate to another, carrying its place from
// one piece to the next so the pieces join without a click.
export class Resampler {
  constructor(inRate, outRate) {
    this.step = inRate / outRate;
    this.pos = 0;
    this.last = 0;
  }

  push(x) {
    const n = x.length;
    if (!n) return new Float32Array(0);
    const out = new Float32Array(Math.ceil((n - this.pos) / this.step) + 1);
    let k = 0;
    let p = this.pos;
    // Positions count in [last, x0, x1, ...]: 0 is the previous piece's last sample.
    while (p < n) {
      const i = Math.floor(p);
      const a = i === 0 ? this.last : x[i - 1];
      out[k++] = a + (x[i] - a) * (p - i);
      p += this.step;
    }
    this.pos = p - n;
    this.last = x[n - 1];
    return out.subarray(0, k);
  }
}

// When the next piece of a channel plays: straight after the last one,
// unless that time has passed (it ran dry: start again `lead` from now) or
// more than `maxAhead` is already queued (it ran ahead: drop this piece, so
// the delay comes back down). {at: seconds or null to drop, reset}.
export function nextStart(nextAt, now, lead = 0.25, maxAhead = 1.5) {
  if (nextAt == null || nextAt < now + 0.02) return { at: now + lead, reset: nextAt != null };
  if (nextAt > now + maxAhead) return { at: null, reset: false };
  return { at: nextAt, reset: false };
}

// --- channels ----------------------------------------------------------------------

export function defaultMode(hz) {
  if (hz >= 108e6 && hz <= 137e6) return 'am';
  if (hz >= 88e6 && hz < 108e6) return 'wfm';
  return 'nfm';
}

// A tap on the waterfall lands between channels: the nearest on the usual
// raster for that part of the spectrum.
export function snapHz(hz) {
  let step = 5000;
  if (hz >= 108e6 && hz <= 137e6) step = 25000;
  else if (hz >= 88e6 && hz < 108e6) step = 100000;
  else if (hz >= 156e6 && hz <= 163e6) step = 25000;
  return Math.round(hz / step) * step;
}

// A channel's frequency to three or four decimals, after its label if any.
export function channelName(c) {
  const s = (c.freq_hz / 1e6).toFixed(4);
  const text = s.endsWith('0') ? s.slice(0, -1) : s;
  return c.label ? `${c.label} · ${text}` : text;
}

// The channel list with `ch` in it: a frequency already there is updated,
// a new one added at the end, or, with `replace`, the list becomes [ch].
// At most `max`: the oldest goes.
export function withChannel(list, ch, max = 4, replace = false) {
  if (replace) return [ch];
  const out = list.filter((c) => Math.abs(c.freq_hz - ch.freq_hz) >= 1000);
  out.push(ch);
  return out.slice(-max);
}

export function withoutChannel(list, freqHz) {
  return list.filter((c) => c.freq_hz !== freqHz);
}

export function sameChannels(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  return a.every((c, i) => c.freq_hz === b[i].freq_hz && c.mode === b[i].mode);
}

// What a request sends for each channel.
export function channelBody(c) {
  const out = { freq_hz: Math.round(c.freq_hz), mode: c.mode };
  if (c.squelch_db) out.squelch_db = c.squelch_db;
  if (c.label) out.label = c.label;
  return out;
}

// The words asking before a listen retunes the receiver.
export function planText(plan, minutes) {
  const win = `a ${(plan.sample_rate_hz / 1e6).toFixed(0)} MHz window at ${(plan.center_hz / 1e6).toFixed(4)} MHz`;
  if (!plan.retune) return `Inside the band the receiver is on${plan.riding ? ` (${plan.riding})` : ''}: no retune, nothing stops.`;
  if (plan.running === LISTEN) return `Outside this listen's window: it moves to ${win}. Nothing else stops; its time left stays.`;
  const stops = plan.running ? `${plan.running} stops` : 'nothing is running now';
  return `This retunes the receiver to ${win}: ${stops} for ${minutes} min, then comes back by itself.`;
}

// Where a level sits on a meter from `lo` to `hi` dBFS, in percent.
export function meterPct(db, lo = -100, hi = -10) {
  if (db == null || !isFinite(db)) return 0;
  return Math.max(0, Math.min(100, ((db - lo) / (hi - lo)) * 100));
}

// The squelch slider's words: at the bottom it stays open.
export function squelchText(db, range) {
  if (db <= range[0]) return 'open';
  return `${db > 0 ? '+' : ''}${db} dB`;
}

export function isClip(row) {
  return !!(row && row.profile === LISTEN && row.kind === 'clip' && row.fields && row.fields.clip);
}

// The registry's profiles, plus one for the listen's clips and window when
// the station listens, so the Log and History colour and filter them.
export function withListen(registry) {
  const l = registry && registry.listen;
  const profiles = (registry && registry.profiles) || [];
  if (!l || !l.enabled) return profiles;
  return [
    ...profiles,
    {
      name: LISTEN,
      summary: 'Listening: audio clips of transmissions',
      color: l.color,
      views: [{ name: 'table', kinds: ['clip'], fields: ['seconds', 'mode'] }],
      summary_kinds: ['clip'],
      summary_fields: ['seconds', 'mode'],
      default_freqs_hz: [],
      installed: true,
      listen: true,
    },
  ];
}
