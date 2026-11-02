// The waterfall and spectrum: canvases fed with spectrum rows (the live
// feed's "fft" messages, or rows from the history API). The newest row is at
// the top; each row's colour comes from the mockup's palette, levelled to
// the noise floor, which differs a lot between bands (about -56 dBFS per
// bin on 1090 MHz, -36 on 162 MHz next to NOAA Weather Radio).

import { clear, decodeRow, h, mhz, mhzShort, quantile, utc } from './util.js';

// Dark at the floor so a quiet band stays calm (the mockup's palette).
const STOPS = [[0, '#05080f'], [0.15, '#0a1530'], [0.3, '#102a5c'], [0.45, '#12508a'], [0.58, '#11809a'], [0.7, '#27a88f'], [0.8, '#8cc46a'], [0.9, '#f0d264'], [1, '#fff4d6']];

function h2r(c) {
  return [parseInt(c.slice(1, 3), 16), parseInt(c.slice(3, 5), 16), parseInt(c.slice(5, 7), 16)];
}

export function makeLut() {
  const out = new Uint8ClampedArray(256 * 3);
  for (let i = 0; i < 256; i++) {
    const v = i / 255;
    let k = 0;
    while (k < STOPS.length - 2 && v > STOPS[k + 1][0]) k++;
    const a = STOPS[k];
    const b = STOPS[k + 1];
    const u = Math.min(1, Math.max(0, (v - a[0]) / (b[0] - a[0])));
    const ca = h2r(a[1]);
    const cb = h2r(b[1]);
    for (let j = 0; j < 3; j++) out[i * 3 + j] = Math.round(ca[j] + (cb[j] - ca[j]) * u);
  }
  return out;
}

const LUT = makeLut();

// `values` (one per bin) to `width` pixels: the highest bin a pixel covers
// ('max', so a narrow burst never vanishes) or their average ('mean').
export function resample(values, width, mode = 'max') {
  const n = values.length;
  const out = new Float32Array(width);
  for (let x = 0; x < width; x++) {
    const a = Math.floor((x * n) / width);
    const b = Math.max(a + 1, Math.floor(((x + 1) * n) / width));
    let acc = mode === 'max' ? -Infinity : 0;
    for (let i = a; i < b && i < n; i++) acc = mode === 'max' ? Math.max(acc, values[i]) : acc + values[i];
    out[x] = mode === 'max' ? acc : acc / (Math.min(b, n) - a);
  }
  return out;
}

// The colour scale follows the noise floor: `below` dB under it to `span`
// dB above that, smoothed so it doesn't flicker.
export class AutoLevel {
  constructor(span = 50, below = 6) {
    this.span = span;
    this.below = below;
    this.floor = null;
  }

  update(row) {
    const f = quantile(row, 0.2);
    if (!isFinite(f)) return;
    this.floor = this.floor == null ? f : this.floor * 0.92 + f * 0.08;
  }

  reset() {
    this.floor = null;
  }

  get lo() {
    return (this.floor ?? -100) - this.below;
  }

  get hi() {
    return this.lo + this.span;
  }
}

// Frequency ticks for a band: a round step giving 5-9 labels.
export function ticks(centerHz, rateHz) {
  const lo = centerHz - rateHz / 2;
  const span = rateHz / 1e6;
  const steps = [0.01, 0.02, 0.025, 0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 5];
  const step = steps.find((s) => span / s <= 9) || 10;
  const out = [];
  const first = Math.ceil(lo / 1e6 / step) * step;
  for (let f = first; f <= (lo + rateHz) / 1e6 + 1e-9; f += step) {
    out.push({ mhz: f, pct: ((f * 1e6 - lo) / rateHz) * 100 });
  }
  // As many decimals as the step has, so 161.25 never reads as 161.3.
  const digits = (String(step).split('.')[1] || '').length;
  return out.map((t) => ({ ...t, label: t.mhz.toFixed(digits) }));
}

function quantileMax(values) {
  let m = -Infinity;
  for (const v of values) if (v > m) m = v;
  return m;
}

export class Waterfall {
  constructor(root, onLevels = () => {}) {
    this.onLevels = onLevels;
    this.level = new AutoLevel();
    // The spectrum's own scale, from the mean, so the mean and the peak
    // hold (about 10 dB higher on noise) both fit.
    this.specLevel = new AutoLevel(50, 8);
    this.usePeak = true;
    this.peakHold = true;
    this.band = null;
    this.specCanvas = h('canvas', { role: 'img', 'aria-label': 'Spectrum' });
    this.wfCanvas = h('canvas', { role: 'img', 'aria-label': 'Waterfall, newest at the top' });
    this.dbGut = h('div', { class: 'gut' });
    this.chanGuides = h('div');
    this.tickBox = h('div', { class: 'ticks' });
    this.timeGut = h('div', { class: 'gut' });
    this.ovTitle = h('b');
    this.ovSub = h('span');
    this.overlay = h('div', { class: 'ovl', hidden: true }, this.ovTitle, this.ovSub);
    this.specPlot = h('div', { class: 'plot' }, this.specCanvas, this.chanGuides);
    this.wfPlot = h('div', { class: 'plot' }, this.wfCanvas, this.overlay);
    root.append(
      h(
        'div',
        { class: 'wfp' },
        h('div', { class: 'spec' }, this.dbGut, this.specPlot),
        h('div', { class: 'fax' }, h('span', { class: 'fu' }, 'MHz'), this.tickBox),
        h('div', { class: 'wf' }, this.timeGut, this.wfPlot),
      ),
    );
    this.resize();
    if (typeof ResizeObserver !== 'undefined') {
      new ResizeObserver(() => {
        if (this.wfPlot.clientWidth !== this.W || this.wfPlot.clientHeight !== this.H) this.resize();
      }).observe(this.wfPlot);
    }
  }

  resize() {
    const W = Math.max(8, Math.round(this.wfPlot.clientWidth));
    const H = Math.max(8, Math.round(this.wfPlot.clientHeight));
    this.W = W;
    this.H = H;
    this.wfCanvas.width = W;
    this.wfCanvas.height = H;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    this.dpr = dpr;
    this.specCanvas.width = Math.max(1, Math.round(this.specPlot.clientWidth * dpr));
    this.specCanvas.height = Math.max(1, Math.round(this.specPlot.clientHeight * dpr));
    this.rowTs = new Float64Array(H).fill(NaN);
    this.avg = null;
    this.hold = null;
    const g = this.wfCanvas.getContext('2d');
    g.fillStyle = '#05080f';
    g.fillRect(0, 0, W, H);
    this.drawAxis();
  }

  // The band being shown: {profile, center_hz, sample_rate_hz, freqs_hz,
  // color}. A different tuning clears the picture.
  setBand(band) {
    const key = band ? `${band.center_hz}|${band.sample_rate_hz}` : '';
    const prevKey = this.band ? `${this.band.center_hz}|${this.band.sample_rate_hz}` : '';
    this.band = band;
    if (key !== prevKey) {
      this.level.reset();
      this.specLevel.reset();
      this.resize();
    } else {
      this.drawAxis();
    }
  }

  setOverlay(title, sub = '') {
    this.overlay.hidden = !title;
    this.ovTitle.textContent = title || '';
    this.ovSub.textContent = sub || '';
  }

  drawAxis() {
    clear(this.tickBox);
    clear(this.chanGuides);
    const b = this.band;
    if (!b || !b.sample_rate_hz) return;
    const lo = b.center_hz - b.sample_rate_hz / 2;
    for (const t of ticks(b.center_hz, b.sample_rate_hz)) {
      if (t.pct < 4 || t.pct > 96) continue; // half of it would hide under the edge
      const el = h('span', { class: 'tick' }, t.label);
      el.style.left = `${t.pct}%`;
      this.tickBox.append(el);
    }
    const pcts = (b.freqs_hz || []).map((f) => ((f - lo) / b.sample_rate_hz) * 100).sort((x, y) => x - y);
    const roomy = (pct) => pcts.every((q) => q === pct || Math.abs(q - pct) >= 7);
    for (const f of b.freqs_hz || []) {
      const pct = ((f - lo) / b.sample_rate_hz) * 100;
      if (pct < 0 || pct > 100) continue;
      // Channels too close to label apart keep their marks; the panel
      // header lists them.
      const mark = h('span', { class: 'chm', title: `${mhz(f)} MHz`, '--pc': b.color || '#8a96a6' }, roomy(pct) ? mhzShort(f) : '');
      mark.style.left = `${pct}%`;
      const guide = h('span', { class: 'chg', '--pc': b.color || '#8a96a6' });
      guide.style.left = `${pct}%`;
      this.tickBox.append(mark);
      this.chanGuides.append(guide);
    }
  }

  // A row from the live feed ("fft" message).
  pushFft(msg) {
    const mean = decodeRow(msg.mean, msg.db_min, msg.db_step);
    const peak = decodeRow(msg.peak, msg.db_min, msg.db_step);
    this.pushRow(mean, peak, msg.ts, 1);
  }

  // One row of mean and peak dB per bin, drawn `rows` pixels tall (history
  // rows of a second are drawn as tall as four live rows of a quarter).
  pushRow(meanDb, peakDb, ts, rows = 1) {
    const W = this.W;
    const H = this.H;
    rows = Math.max(1, Math.min(rows, H));
    const src = this.usePeak ? peakDb : meanDb;
    this.level.update(src);
    this.specLevel.update(meanDb);
    const lo = this.level.lo;
    const range = this.level.hi - lo;
    const px = resample(src, W, 'max');
    const g = this.wfCanvas.getContext('2d');
    g.drawImage(this.wfCanvas, 0, 0, W, H - rows, 0, rows, W, H - rows);
    const img = g.createImageData(W, rows);
    for (let x = 0; x < W; x++) {
      let v = (px[x] - lo) / range;
      v = v < 0 ? 0 : v > 1 ? 1 : v;
      const i = (v * 255) | 0;
      for (let y = 0; y < rows; y++) {
        const o = (y * W + x) * 4;
        img.data[o] = LUT[i * 3];
        img.data[o + 1] = LUT[i * 3 + 1];
        img.data[o + 2] = LUT[i * 3 + 2];
        img.data[o + 3] = 255;
      }
    }
    g.putImageData(img, 0, 0);
    this.rowTs.copyWithin(rows, 0, H - rows);
    this.rowTs.fill(ts, 0, rows);
    this.drawSpectrum(meanDb, peakDb);
    this.onLevels(lo, this.level.hi);
    if (!this.lastLabels || Date.now() - this.lastLabels > 2000) {
      this.lastLabels = Date.now();
      this.drawTimes();
    }
  }

  drawSpectrum(meanDb, peakDb) {
    const c = this.specCanvas;
    const g = c.getContext('2d');
    const W = c.width;
    const H = c.height;
    const mean = resample(meanDb, W, 'mean');
    const peak = resample(peakDb, W, 'max');
    if (!this.avg || this.avg.length !== W) {
      this.avg = Float32Array.from(mean);
      this.hold = Float32Array.from(peak);
    }
    for (let x = 0; x < W; x++) {
      this.avg[x] = this.avg[x] * 0.6 + mean[x] * 0.4;
      this.hold[x] = Math.max(this.hold[x] - 0.05, peak[x]);
    }
    const lo = this.specLevel.lo;
    const hi = Math.max(this.specLevel.hi, quantileMax(this.hold) + 4);
    const y = (db) => H - ((db - lo) / (hi - lo)) * H;
    g.clearRect(0, 0, W, H);
    g.lineWidth = 1 * this.dpr;
    if (this.peakHold) {
      g.strokeStyle = 'rgba(240,210,100,.75)';
      g.beginPath();
      for (let x = 0; x < W; x++) (x ? g.lineTo : g.moveTo).call(g, x, y(this.hold[x]));
      g.stroke();
    }
    g.fillStyle = 'rgba(92,184,255,.14)';
    g.strokeStyle = '#9fd0ff';
    g.beginPath();
    g.moveTo(0, H);
    for (let x = 0; x < W; x++) g.lineTo(x, y(this.avg[x]));
    g.lineTo(W, H);
    g.closePath();
    g.fill();
    g.beginPath();
    for (let x = 0; x < W; x++) (x ? g.lineTo : g.moveTo).call(g, x, y(this.avg[x]));
    g.stroke();
    clear(this.dbGut);
    for (const frac of [0.15, 0.5, 0.85]) {
      const db = hi - frac * (hi - lo);
      const el = h('span', { class: 'db' }, `${Math.round(db)}`);
      el.style.top = `${frac * 100}%`;
      this.dbGut.append(el);
    }
  }

  drawTimes() {
    clear(this.timeGut);
    for (const frac of [0.02, 0.25, 0.5, 0.75, 0.97]) {
      const ts = this.rowTs[Math.min(this.H - 1, Math.round(frac * (this.H - 1)))];
      if (!isFinite(ts)) continue;
      const el = h('span', { class: 'tlab' }, utc(ts));
      el.style.top = `${frac * 100}%`;
      this.timeGut.append(el);
    }
  }

  // A gap in the picture (a switch or a pause): a dark band, so time doesn't
  // look continuous where it wasn't.
  pushGap(ts, rows = 3) {
    const g = this.wfCanvas.getContext('2d');
    g.drawImage(this.wfCanvas, 0, 0, this.W, this.H - rows, 0, rows, this.W, this.H - rows);
    g.fillStyle = '#2a1f0a';
    g.fillRect(0, 0, this.W, rows);
    this.rowTs.copyWithin(rows, 0, this.H - rows);
    this.rowTs.fill(ts, 0, rows);
  }
}
