// The History screen: the past in signal form. The spectrum history is
// recorded per tuning, so a period is one rotation step (a spectrum
// segment). For the chosen step it shows the recorded waterfall with the
// rows decoded during it marked at their time and frequency, the gaps in
// coverage (paused, error, stopped), the decoded rows themselves, each
// channel's level against the noise floor, and how many rows were logged
// over time. Everything about profiles comes from /api/registry.

import { api } from './api.js';
import { paintAlerts, paintAppBar, paintClock } from './appbar.js';
import { assignColors, clear, h, mhz, utc } from './util.js';
import { AutoLevel, makeLut, resample, ticks } from './waterfall.js';
import { BandStrip } from './bandstrip.js';
import { channelLevels, dayBounds, dayOf, gapsIn, pathOf, stepAt, stepsOf, timeTicks } from './historyutil.js';

const $ = (id) => document.getElementById(id);
const NOW = () => Date.now() / 1000;
const SVG = 'http://www.w3.org/2000/svg';
const LUT = makeLut();
const DASH = ['none', '6 4', '2 3', '8 3 2 3'];

const S = {
  registry: null,
  profiles: {},
  colors: {},
  day: null, // 'YYYY-MM-DD'
  steps: [],
  coverage: [],
  idx: -1,
  rows: [],
  sel: null,
  wf: null, // { seg, ts: [], rows: [Float32Array], bucket_s, tier }
  views: new Set(['wf', 'bands', 'list', 'lvl', 'rate']),
  layout: 'side',
  want: null, // a time to open at, from the address
  token: 0, // drops answers for a step no longer shown
};

const color = (name) => S.colors[name] || '#8a96a6';
const STRIP = new BandStrip($('w-bands'));
const hasMap = (profile) => ((S.profiles[profile] || {}).views || []).some((v) => v.name === 'map');

// --- Start-up ------------------------------------------------------------------------------

async function init() {
  S.registry = await api('/api/registry');
  for (const p of S.registry.profiles) S.profiles[p.name] = p;
  S.colors = assignColors(S.registry.profiles);
  const u = new URLSearchParams(location.search);
  const at = Number(u.get('at'));
  S.want = at > 0 ? at : null;
  S.wantSegment = Number(u.get('segment')) || null;
  if (u.get('layout') === 'stack') S.layout = 'stack';
  wire();
  pollStatus();
  setInterval(pollStatus, 5000);
  setInterval(() => paintClock(NOW()), 1000);
  await loadDay(u.get('day') || dayOf(S.want || NOW()));
}

function wire() {
  $('d-prev').addEventListener('click', () => shiftDay(-1));
  $('d-next').addEventListener('click', () => shiftDay(1));
  $('d-date').addEventListener('change', () => {
    if (dayBounds($('d-date').value)) loadDay($('d-date').value);
  });
  $('s-prev').addEventListener('click', () => pick(S.idx - 1));
  $('s-next').addEventListener('click', () => pick(S.idx + 1));
  document.addEventListener('keydown', (e) => {
    if (e.target.closest('input, select, textarea')) return;
    if (e.key === 'ArrowLeft') pick(S.idx - 1);
    if (e.key === 'ArrowRight') pick(S.idx + 1);
  });
  for (const b of document.querySelectorAll('[data-view]')) {
    b.addEventListener('click', () => {
      const v = b.dataset.view;
      if (S.views.has(v)) S.views.delete(v);
      else S.views.add(v);
      applyViews();
      if (v === 'wf' && S.views.has('wf')) drawWaterfall();
    });
  }
  for (const b of document.querySelectorAll('[data-layout]')) {
    b.addEventListener('click', () => {
      S.layout = b.dataset.layout;
      applyViews();
      drawWaterfall();
    });
  }
  $('d-close').addEventListener('click', () => select(null));
  let resize = null;
  window.addEventListener('resize', () => {
    clearTimeout(resize);
    resize = setTimeout(() => {
      drawLane();
      drawWaterfall();
    }, 150);
  });
}

async function pollStatus() {
  try {
    const st = await api('/api/status');
    paintAppBar(st.live, color, 'open');
    paintAlerts(st.alerts, color, hasMap);
  } catch (e) {
    paintAppBar(null, color, 'closed');
  }
}

function showError(msg) {
  $('herr').hidden = !msg;
  $('herr').textContent = msg || '';
}

function applyViews() {
  $('hgrid').classList.toggle('stack', S.layout === 'stack');
  for (const b of document.querySelectorAll('[data-view]')) b.setAttribute('aria-pressed', String(S.views.has(b.dataset.view)));
  for (const b of document.querySelectorAll('[data-layout]')) b.setAttribute('aria-pressed', String(S.layout === b.dataset.layout));
  $('p-wf').hidden = !S.views.has('wf');
  $('w-bands').classList.toggle('off', !S.views.has('bands'));
  $('p-list').hidden = !S.views.has('list');
  $('p-lvl').hidden = !S.views.has('lvl');
  $('p-rate').hidden = !S.views.has('rate');
}

// --- The day and its steps -------------------------------------------------------------------

function shiftDay(n) {
  const b = dayBounds(S.day);
  if (b) loadDay(dayOf(b[0] + n * 86400));
}

async function loadDay(day) {
  const b = dayBounds(day);
  if (!b) return;
  showError(null);
  S.day = day;
  $('d-date').value = day;
  $('d-next').disabled = b[1] > NOW();
  let segs;
  let cov;
  try {
    [segs, cov] = await Promise.all([api(`/api/spectrum/segments?since=${b[0]}&until=${b[1]}`), api(`/api/coverage?since=${b[0]}&until=${Math.min(b[1], NOW())}`)]);
  } catch (e) {
    showError(`Could not load the day: ${e.message}`);
    return;
  }
  S.steps = stepsOf(segs.segments, b[0], b[1], NOW());
  S.coverage = cov.coverage;
  const n = S.steps.length;
  $('daynote').textContent = n
    ? `The waterfall is recorded per tuning, so a period is one rotation step: ${n} on ${day}, ${utc(S.steps[0].a, false)}–${S.steps[n - 1].live ? 'now' : utc(S.steps[n - 1].b, false)} UTC. ← → move between steps.`
    : `Nothing was recorded on ${day}. The spectrum history keeps second rows for 7 days and minute rows for 90; nothing is recorded while paused or stopped.`;
  drawLane();
  let i = n - 1;
  if (S.wantSegment) i = S.steps.findIndex((s) => s.id === S.wantSegment);
  else if (S.want) i = stepAt(S.steps, S.want);
  S.wantSegment = null;
  await pick(i < 0 ? n - 1 : i);
}

// The day as a lane: one block per step in its profile's colour, hatched
// where coverage had a gap; click one to open it.
function drawLane() {
  const lane = $('dlane');
  const tk = $('dticks');
  clear(lane);
  clear(tk);
  const b = dayBounds(S.day);
  if (!b) return;
  const pct = (t) => `${((t - b[0]) / 86400) * 100}%`;
  S.steps.forEach((s, i) => {
    const gap = gapsIn(S.coverage, s.a, s.b).find((g) => g.state === 'paused' || g.state === 'error');
    const el = h('button', { type: 'button', 'aria-pressed': String(i === S.idx), class: gap ? `gap ${gap.state}` : '', title: `${s.profile} ${utc(s.a, false)}–${s.live ? 'now' : utc(s.b, false)} UTC${gap ? ` · ${gap.label}` : ''}` });
    el.style.left = pct(s.a);
    el.style.width = `max(2px, ${((s.b - s.a) / 86400) * 100}%)`;
    el.style.setProperty('--pc', color(s.profile));
    el.setAttribute('aria-label', el.title);
    el.addEventListener('click', () => pick(i));
    lane.append(el);
  });
  if (NOW() < b[1]) {
    const now = h('span', { class: 'now' });
    now.style.left = pct(NOW());
    lane.append(now);
  }
  for (let hr = 0; hr <= 24; hr += 3) {
    const t = h('span', {}, `${String(hr).padStart(2, '0')}:00`);
    t.style.left = `${(hr / 24) * 100}%`;
    tk.append(t);
  }
}

// --- One step --------------------------------------------------------------------------------

async function pick(i) {
  if (i < 0 || i >= S.steps.length) {
    if (!S.steps.length) renderEmpty();
    return;
  }
  S.idx = i;
  S.sel = null;
  const s = S.steps[i];
  const token = ++S.token;
  const q = new URLSearchParams({ day: S.day, segment: String(s.id) });
  if (S.layout === 'stack') q.set('layout', 'stack');
  history.replaceState(null, '', `/history?${q}`);
  for (const [k, el] of [...$('dlane').querySelectorAll('button')].entries()) el.setAttribute('aria-pressed', String(k === i));
  $('s-label').textContent = `${s.profile} ${utc(s.a, false)}–${s.live ? 'now' : utc(s.b, false)}`;
  $('s-prev').disabled = i === 0;
  $('s-next').disabled = i === S.steps.length - 1;
  const seg = s.seg;
  $('w-prof').textContent = s.profile;
  $('w-prof').style.setProperty('--pc', color(s.profile));
  $('w-title').textContent = `${utc(s.a)}–${s.live ? 'now' : utc(s.b)} UTC · ${mhz(seg.center_hz)} MHz · ${seg.sample_rate_hz / 1e6} MS/s`;
  $('l-log').setAttribute('href', `/log?${new URLSearchParams({ profiles: s.profile, since: String(Math.floor(s.a)), until: String(Math.ceil(s.b)) })}`);
  const box = $('w-box');
  const buckets = Math.max(60, Math.min(2000, Math.round(box.clientHeight || 560)));
  const span = s.b - s.a;
  const rateBuckets = Math.max(4, Math.min(60, Math.round(span / 30)));
  let wf;
  let log;
  let hist;
  try {
    [wf, log, hist] = await Promise.all([
      api(`/api/spectrum?segment=${seg.id}&since=${s.a}&until=${s.b}&buckets=${buckets}`),
      api(`/api/log?${new URLSearchParams({ profiles: s.profile, since: String(s.a), until: String(s.b), limit: '2000' })}`),
      api(`/api/log/histogram?${new URLSearchParams({ profiles: s.profile, since: String(s.a), until: String(s.b), buckets: String(rateBuckets) })}`),
    ]);
  } catch (e) {
    if (token === S.token) showError(`Could not load the step: ${e.message}`);
    return;
  }
  if (token !== S.token) return;
  showError(null);
  const peaks = Uint8Array.from(atob(wf.peak), (c) => c.charCodeAt(0));
  const rows = [];
  for (let k = 0; k < wf.ts.length; k++) rows.push(Float32Array.from(peaks.subarray(k * seg.bins, (k + 1) * seg.bins), (v) => seg.db_min + v * seg.db_step));
  S.wf = { seg, ts: wf.ts, rows, bucket_s: wf.bucket_s, tier: wf.tier };
  S.rows = log.rows;
  S.more = log.more;
  S.hist = hist;
  drawWaterfall();
  renderList();
  renderSel();
  drawLevels();
  drawRate();
}

function renderEmpty() {
  S.idx = -1;
  S.wf = null;
  S.rows = [];
  $('s-label').textContent = '—';
  $('w-title').textContent = 'No step';
  $('s-prev').disabled = true;
  $('s-next').disabled = true;
  drawWaterfall();
  renderList();
  renderSel();
  drawLevels();
  drawRate();
}

// --- The waterfall ---------------------------------------------------------------------------

function drawWaterfall() {
  const box = $('w-box');
  const canvas = $('w-canvas');
  for (const el of box.querySelectorAll('.hmark, .hgap, .hfm')) el.remove();
  clear($('w-times'));
  clear($('w-freqs'));
  const W = Math.max(8, box.clientWidth);
  const H = Math.max(8, box.clientHeight);
  canvas.width = W;
  canvas.height = H;
  const g = canvas.getContext('2d');
  g.fillStyle = '#05080f';
  g.fillRect(0, 0, W, H);
  const s = S.steps[S.idx];
  if (!s || !S.wf || !S.views.has('wf')) {
    $('w-note').textContent = '';
    STRIP.show(null, null);
    return;
  }
  const { seg, ts, rows } = S.wf;
  // A row covers its bucket, and at least the length of a recorded row (1 s
  // or a minute), so a short step doesn't come out striped.
  const rowS = rowSeconds();
  const a = s.a;
  const b = s.b;
  const span = b - a;
  const yOf = (t) => ((b - t) / span) * H; // newest at the top
  const level = new AutoLevel(50, 6);
  for (const r of rows) level.update(r);
  const img = g.createImageData(W, H);
  for (let i = 0; i < rows.length; i++) {
    const px = resample(rows[i], W, 'max');
    const y0 = Math.floor(yOf(ts[i] + rowS));
    const y1 = Math.ceil(yOf(ts[i]));
    for (let y = Math.max(0, y0); y < Math.min(H, Math.max(y1, y0 + 1)); y++) {
      for (let x = 0; x < W; x++) {
        let v = (px[x] - level.lo) / (level.hi - level.lo);
        v = v < 0 ? 0 : v > 1 ? 1 : v;
        const k = (v * 255) | 0;
        const o = (y * W + x) * 4;
        img.data[o] = LUT[k * 3];
        img.data[o + 1] = LUT[k * 3 + 1];
        img.data[o + 2] = LUT[k * 3 + 2];
        img.data[o + 3] = 255;
      }
    }
  }
  g.putImageData(img, 0, 0);
  const lo = seg.center_hz - seg.sample_rate_hz / 2;
  const xPct = (f) => ((f - lo) / seg.sample_rate_hz) * 100;
  STRIP.show(lo, lo + seg.sample_rate_hz);
  // Frequency scale and the profile's channels.
  for (const t of ticks(seg.center_hz, seg.sample_rate_hz)) {
    const el = h('span', {}, t.label);
    el.style.left = `${t.pct}%`;
    $('w-freqs').append(el);
  }
  const chans = (S.profiles[s.profile] || {}).default_freqs_hz || [];
  for (const f of chans) {
    if (f < lo || f > lo + seg.sample_rate_hz) continue;
    const el = h('span', { class: 'hfm', title: `${mhz(f)} MHz` });
    el.style.left = `${xPct(f)}%`;
    box.append(el);
  }
  // Time scale.
  for (const t of timeTicks(a, b, Math.max(4, Math.floor(H / 48))).ticks) {
    const el = h('span', {}, utc(t, span < 600));
    el.style.top = `${(yOf(t) / H) * 100}%`;
    $('w-times').append(el);
  }
  // Gaps in coverage.
  for (const gp of gapsIn(S.coverage, a, b)) {
    const el = h('div', { class: `hgap ${gp.state}` }, h('span', {}, `${gp.label} · ${utc(gp.a)}–${utc(gp.b)}`));
    el.style.top = `${(yOf(gp.b) / H) * 100}%`;
    el.style.height = `${Math.max(0.4, ((gp.b - gp.a) / span) * 100)}%`;
    box.append(el);
  }
  // A marker per decoded row, at its frequency (or the band's centre).
  for (const r of S.rows) {
    const f = r.freq_hz && r.freq_hz >= lo && r.freq_hz <= lo + seg.sample_rate_hz ? r.freq_hz : seg.center_hz;
    const el = h('button', { type: 'button', class: 'hmark', 'aria-pressed': String(S.sel === r.id), title: `${utc(r.ts)} ${r.subject || ''}${r.label ? ` ${r.label}` : ''}` });
    el.setAttribute('aria-label', el.title);
    el.style.left = `${xPct(f)}%`;
    el.style.top = `${(yOf(r.ts) / H) * 100}%`;
    el.style.setProperty('--pc', color(r.profile));
    el.addEventListener('click', () => select(r.id));
    box.append(el);
  }
  const res = S.wf.tier === 60 ? 'minute averages (second rows are kept for 7 days)' : `${Math.round(rowS)} s each`;
  $('w-note').textContent = `${s.profile} at ${mhz(seg.center_hz)} MHz, ${mhz(lo, 3)}–${mhz(lo + seg.sample_rate_hz, 3)}; ${rows.length} rows, ${res}; dashed lines are the profile's channels.${S.more ? ' More rows were logged than are marked (the first 2000).' : ''}`;
}

function rowSeconds() {
  return Math.max(S.wf.bucket_s, S.wf.tier || 1);
}

// --- The decoded rows --------------------------------------------------------------------------

function summary(r) {
  const f = r.fields || {};
  const keys = (S.profiles[r.profile] || {}).summary_fields || [];
  const parts = [];
  for (const k of keys) if (f[k] != null && f[k] !== '' && parts.length < 4) parts.push(`${k} ${Array.isArray(f[k]) ? f[k].join(', ') : f[k]}`);
  return parts.join(' · ');
}

function renderList() {
  const list = $('list');
  clear(list);
  $('l-count').textContent = S.rows.length ? `${S.rows.length}${S.more ? '+' : ''} rows` : '';
  if (!S.rows.length) {
    list.append(h('p', { class: 'empty' }, S.idx < 0 ? 'No step chosen.' : 'Nothing was decoded in this step.'));
    return;
  }
  for (const r of S.rows) {
    const el = h(
      'button',
      { type: 'button', class: 'hli', role: 'listitem', 'aria-current': String(S.sel === r.id) },
      h('span', { class: 't' }, utc(r.ts)),
      h('span', { class: 's' }, h('b', { class: 'mono' }, r.subject || '—'), r.label ? ` ${r.label}` : '', h('span', { class: 'muted' }, ` · ${r.kind || ''}`)),
      h('span', {}),
      h('span', { class: 'muted sm' }, summary(r)),
    );
    el.style.setProperty('--pc', color(r.profile));
    el.addEventListener('click', () => select(r.id));
    list.append(el);
  }
}

function select(id) {
  S.sel = S.sel === id ? null : id;
  for (const el of document.querySelectorAll('.hmark')) el.setAttribute('aria-pressed', 'false');
  renderList();
  renderSel();
  const r = S.rows.find((x) => x.id === S.sel);
  if (!r) return;
  const i = S.rows.indexOf(r);
  const marks = $('w-box').querySelectorAll('.hmark');
  if (marks[i]) marks[i].setAttribute('aria-pressed', 'true');
  const item = $('list').children[i];
  if (item) item.scrollIntoView({ block: 'nearest' });
}

function renderSel() {
  const r = S.rows.find((x) => x.id === S.sel);
  $('sel').hidden = !r;
  if (!r) return;
  $('d-prof').textContent = r.profile;
  $('d-prof').style.setProperty('--pc', color(r.profile));
  $('d-subj').textContent = r.subject || '—';
  $('d-label').textContent = r.label || '';
  const kv = $('d-fields');
  clear(kv);
  const add = (k, v) => kv.append(h('dt', {}, k), h('dd', { class: 'mono' }, v));
  add('time', `${utc(r.ts)}${r.ts_end && r.ts_end - r.ts >= 1 ? `–${utc(r.ts_end)}` : ''} UTC`);
  add('kind', r.kind || '—');
  if (r.freq_hz) add('frequency', `${mhz(r.freq_hz)} MHz`);
  if (r.level_db != null) add('level', `${Number(r.level_db).toFixed(1)} dB`);
  for (const [k, v] of Object.entries(r.fields || {})) add(k, v == null ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v));
  $('d-log').setAttribute('href', `/log?${new URLSearchParams({ subject: r.subject || '', profiles: r.profile, around: String(Math.round(r.ts)) })}`);
  $('d-map').hidden = !(hasMap(r.profile) && r.subject);
  $('d-map').setAttribute('href', `/map?${new URLSearchParams({ at: String(Math.round(r.ts)), profile: r.profile, subject: r.subject || '' })}`);
}

// --- Channel level and message rate --------------------------------------------------------------

function svgEl(tag, attrs) {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

function timeScale(id, a, b) {
  const el = $(id);
  clear(el);
  for (const t of timeTicks(a, b, 8).ticks) {
    const s = h('span', {}, utc(t, b - a < 600));
    s.style.left = `${((t - a) / (b - a)) * 100}%`;
    el.append(s);
  }
}

function drawLevels() {
  const svg = $('lv');
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  clear($('lv-legend'));
  clear($('lv-times'));
  const s = S.steps[S.idx];
  if (!s || !S.wf || !S.wf.rows.length) return;
  const { seg, ts, rows } = S.wf;
  const bs = rowSeconds();
  const chans = (S.profiles[s.profile] || {}).default_freqs_hz || [];
  const lv = channelLevels(rows, seg.center_hz, seg.sample_rate_hz, chans);
  const times = ts.map((t) => t + bs / 2);
  const all = [...lv.floor, ...lv.levels.flat()].filter(isFinite);
  const top = Math.ceil((Math.max(...all) + 3) / 10) * 10;
  const bottom = Math.floor((Math.min(...all) - 3) / 10) * 10;
  const W = 600;
  const H = 170;
  for (let db = bottom; db <= top; db += 10) {
    const y = ((top - db) / (top - bottom)) * H;
    svg.append(svgEl('line', { x1: 0, x2: W, y1: y, y2: y, stroke: '#1f2a37', 'stroke-width': 1, 'vector-effect': 'non-scaling-stroke' }));
  }
  const brk = Math.max(3 * bs, 5);
  svg.append(svgEl('path', { d: pathOf(times, lv.floor, s.a, s.b, top, bottom, W, H, brk), fill: 'none', stroke: '#5b6b7f', 'stroke-width': 1.2, 'vector-effect': 'non-scaling-stroke' }));
  lv.channels.forEach((f, i) => {
    svg.append(svgEl('path', { d: pathOf(times, lv.levels[i], s.a, s.b, top, bottom, W, H, brk), fill: 'none', stroke: color(s.profile), 'stroke-width': 1.4, 'stroke-dasharray': DASH[i % DASH.length], 'vector-effect': 'non-scaling-stroke' }));
  });
  const leg = $('lv-legend');
  lv.channels.forEach((f, i) => {
    const sw = svgEl('svg', { width: 22, height: 8, 'aria-hidden': 'true' });
    sw.append(svgEl('line', { x1: 0, x2: 22, y1: 4, y2: 4, stroke: color(s.profile), 'stroke-width': 2, 'stroke-dasharray': DASH[i % DASH.length] }));
    leg.append(h('span', {}, sw, h('span', { class: 'mono' }, `${mhz(f, 3)} MHz`)));
  });
  const fl = svgEl('svg', { width: 22, height: 8, 'aria-hidden': 'true' });
  fl.append(svgEl('line', { x1: 0, x2: 22, y1: 4, y2: 4, stroke: '#5b6b7f', 'stroke-width': 2 }));
  leg.append(h('span', {}, fl, h('span', {}, 'noise floor')));
  leg.append(h('span', { class: 'muted mono' }, `${top} dBFS at the top, ${bottom} at the foot, lines every 10 dB`));
  timeScale('lv-times', s.a, s.b);
}

function drawRate() {
  const svg = $('rt');
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  clear($('rt-times'));
  $('rt-sub').textContent = '';
  const s = S.steps[S.idx];
  if (!s || !S.hist) return;
  const counts = S.hist.counts.map((c) => c[s.profile] || 0);
  const peak = Math.max(1, ...counts);
  const W = 600;
  const H = 110;
  const bw = W / counts.length;
  counts.forEach((n, i) => {
    if (!n) return;
    const hh = (n / peak) * (H - 4);
    svg.append(svgEl('rect', { x: (i * bw + 1).toFixed(1), y: (H - hh).toFixed(1), width: Math.max(1, bw - 2).toFixed(1), height: hh.toFixed(1), fill: color(s.profile), opacity: 0.8 }));
  });
  const width = S.hist.bucket_s;
  $('rt-sub').textContent = `log rows per ${width >= 90 ? `${Math.round(width / 60)} min` : `${Math.round(width)} s`} · peak ${peak} · ${counts.reduce((x, y) => x + y, 0)} in the step`;
  timeScale('rt-times', s.a, s.b);
}

applyViews();
init().catch((e) => showError(`Could not load: ${e.message}`));
