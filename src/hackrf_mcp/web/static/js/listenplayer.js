// Playing a listen's audio and its clips in the page. The audio comes over
// /ws/audio as mu-law frames (listen.FRAME); each piece is turned into an
// AudioBuffer at the page's own rate and started right after the last piece
// of its channel, so the channels of a scan play side by side. This works on
// a plain-HTTP LAN page: an AudioWorklet would need a secure context.
// The page must create the AudioContext from a click (browsers keep audio
// off until the user asks for it); ensure() does that.

import { AUDIO, DROPPED, Resampler, nextStart, parseFrame, parseWav, ulawDecode } from './listenutil.js';

export class Player {
  constructor() {
    this.ctx = null;
    this.volume = 0.8;
    this.muted = false;
    this.channels = new Map();
    this.stats = { frames: 0, played_s: 0, restarts: 0, dropped_pieces: 0, missed_frames: 0 };
    this.clip = null;
  }

  // From a click: the AudioContext, created or resumed.
  ensure() {
    if (!this.ctx) {
      const C = window.AudioContext || window.webkitAudioContext;
      if (!C) return null;
      this.ctx = new C();
      this.out = this.ctx.createGain();
      this.out.gain.value = this.muted ? 0 : this.volume;
      this.out.connect(this.ctx.destination);
    }
    if (this.ctx.state === 'suspended') this.ctx.resume();
    return this.ctx;
  }

  get running() {
    return !!this.ctx && this.ctx.state === 'running';
  }

  setVolume(v) {
    this.volume = v;
    if (this.out) this.out.gain.value = this.muted ? 0 : v;
  }

  setMuted(m) {
    this.muted = m;
    if (this.out) this.out.gain.value = m ? 0 : this.volume;
  }

  // Each channel: its resampler, a low-pass under its own Nyquist (linear
  // interpolation leaves images above it), and when its next piece starts.
  channel(i, rate) {
    let c = this.channels.get(i);
    if (!c || c.rate !== rate) {
      if (c) c.lp.disconnect();
      const lp = this.ctx.createBiquadFilter();
      lp.type = 'lowpass';
      lp.frequency.value = rate * 0.45;
      lp.connect(this.out);
      c = { rate, rs: new Resampler(rate, this.ctx.sampleRate), next: null, lp };
      this.channels.set(i, c);
    }
    return c;
  }

  frame(buf) {
    const f = parseFrame(buf);
    if (!f) return null;
    if (f.type === DROPPED) this.stats.missed_frames += f.seq;
    if (f.type !== AUDIO) return f;
    this.stats.frames += 1;
    if (!this.running) return f;
    const c = this.channel(f.channel, f.rate);
    const x = c.rs.push(ulawDecode(f.payload));
    if (!x.length) return f;
    const s = nextStart(c.next, this.ctx.currentTime);
    if (s.reset) this.stats.restarts += 1;
    if (s.at == null) {
      this.stats.dropped_pieces += 1;
      return f;
    }
    const b = this.ctx.createBuffer(1, x.length, this.ctx.sampleRate);
    b.copyToChannel(x, 0);
    const src = this.ctx.createBufferSource();
    src.buffer = b;
    src.connect(c.lp);
    src.start(s.at);
    c.next = s.at + b.duration;
    this.stats.played_s += b.duration;
    return f;
  }

  reset() {
    for (const c of this.channels.values()) c.lp.disconnect();
    this.channels.clear();
  }

  // A clip: fetched, decoded here, played once. Resolves when it ends (or
  // is stopped); a second clip stops the first.
  async playClip(url, onState = () => {}) {
    this.stopClip();
    const ctx = this.ensure();
    if (!ctx) throw new Error('this browser has no Web Audio');
    const res = await fetch(url, { credentials: 'same-origin' });
    if (!res.ok) throw new Error(res.status === 404 ? 'that clip is no longer kept' : `${res.status} ${res.statusText}`);
    const wav = parseWav(await res.arrayBuffer());
    if (!wav) throw new Error('not a clip this page can play');
    const x = new Resampler(wav.rate, ctx.sampleRate).push(wav.samples);
    const b = ctx.createBuffer(1, Math.max(1, x.length), ctx.sampleRate);
    b.copyToChannel(x, 0);
    const lp = ctx.createBiquadFilter();
    lp.type = 'lowpass';
    lp.frequency.value = wav.rate * 0.45;
    lp.connect(this.out);
    const src = ctx.createBufferSource();
    src.buffer = b;
    src.connect(lp);
    const clip = { src, url };
    this.clip = clip;
    onState('playing');
    await new Promise((resolve) => {
      src.onended = resolve;
      clip.stop = () => {
        try {
          src.stop();
        } catch (e) {
          // not started or already ended
        }
        resolve();
      };
      src.start();
    });
    lp.disconnect();
    if (this.clip === clip) this.clip = null;
    onState('ended');
  }

  stopClip() {
    if (this.clip && this.clip.stop) this.clip.stop();
    this.clip = null;
  }
}

// The /ws/audio stream while it is wanted: onFrame(ArrayBuffer) for every
// frame, onState('open' | 'closed'); reconnects with a growing delay until
// close().
export class AudioSocket {
  constructor(onFrame, onState = () => {}) {
    this.onFrame = onFrame;
    this.onState = onState;
    this.delay = 1000;
    this.wanted = true;
    this.connect();
  }

  connect() {
    if (!this.wanted) return;
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws/audio`);
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    ws.onopen = () => {
      this.delay = 1000;
      this.onState('open');
    };
    ws.onmessage = (e) => {
      if (e.data instanceof ArrayBuffer) this.onFrame(e.data);
    };
    ws.onclose = () => {
      this.onState('closed');
      if (!this.wanted) return;
      setTimeout(() => this.connect(), this.delay);
      this.delay = Math.min(this.delay * 2, 15000);
    };
  }

  close() {
    this.wanted = false;
    if (this.ws) this.ws.close();
  }
}

// Play / Stop, Download and Delete for a clip's row (Live's feed, the Log,
// History). Delete asks once more on the button itself.
export function clipActions(h, post, player, row, onDeleted = () => {}) {
  const id = row.fields.clip;
  const url = `/api/clips/${encodeURIComponent(id)}`;
  const note = h('span', { class: 't2 sm' });
  const play = h('button', { type: 'button', class: 'btn sm' }, '▶ Play');
  play.addEventListener('click', async (e) => {
    e.stopPropagation();
    if (play.dataset.state === 'playing') {
      player.stopClip();
      return;
    }
    note.textContent = '';
    try {
      await player.playClip(url, (st) => {
        play.dataset.state = st;
        play.textContent = st === 'playing' ? '■ Stop' : '▶ Play';
      });
    } catch (err) {
      play.dataset.state = '';
      play.textContent = '▶ Play';
      note.textContent = err.message;
    }
  });
  const dl = h('a', { class: 'btn sm', href: `${url}?download=1`, download: `clip-${id}.wav` }, 'Download');
  dl.addEventListener('click', (e) => e.stopPropagation());
  const del = h('button', { type: 'button', class: 'btn sm danger' }, 'Delete');
  let armed = null;
  del.addEventListener('click', async (e) => {
    e.stopPropagation();
    if (!armed) {
      del.textContent = 'Delete this clip?';
      armed = setTimeout(() => {
        armed = null;
        del.textContent = 'Delete';
      }, 4000);
      return;
    }
    clearTimeout(armed);
    armed = null;
    try {
      await post(`/api/clips/${encodeURIComponent(id)}/delete`, {});
      player.stopClip();
      play.disabled = true;
      del.disabled = true;
      dl.hidden = true;
      note.textContent = 'Deleted (the row stays in the log).';
      onDeleted(row);
    } catch (err) {
      del.textContent = 'Delete';
      note.textContent = err.message;
    }
  });
  return h('span', { class: 'row wr clipacts' }, play, dl, del, note);
}
