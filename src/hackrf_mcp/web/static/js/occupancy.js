// The Occupancy screen: what the scheduled wide sweeps saw (occupancy.py),
// as a heat map of frequency across and time down (newest at the top, UTC)
// with the band plan above it; the same frequencies folded by hour of day
// and by weekday; and the ranges well above their usual level. Clicking a
// cell gives its level against its usual one, with links to watch that
// range on the Live screen and to the Log's rows there. The view is kept in
// the address (?range, view, fmin, fmax in MHz, days).

import { ApiError, api } from './api.js';
import { paintAlerts, paintAppBar, paintClock } from './appbar.js';
import { BandStrip, bandsAt, loadPlan, rangeText } from './bandstrip.js';
import { timeTicks } from './historyutil.js';
import {
  PROFILE_DAYS,
  RANGES,
  WEEKDAYS,
  bytesOf,
  cellAt,
  cellLevel,
  dbText,
  freqTicks,
  groupUnusual,
  levels,
  logHref,
  paint,
  rowMap,
  scheduleText,
  spanOf,
  watchHref,
  zoomRange,
} from './occutil.js';
import { ago, assignColors, clear, fmtUtc, h, mhz, utc } from './util.js';
import { makeLut } from './waterfall.js';

const $ = (id) => document.getElementById(id);
const NOW = () => Date.now() / 1000;
const LUT = makeLut();
const GREY = '#8a96a6';

const S = {
  registry: null,
  colors: {},
  plan: null,
  watch: null,
  range: '24h',
  view: 'mean',
  fmin: null,
  fmax: null,
  days: 30,
  grid: null,
  rows: null,
  bytes: null,
  lv: null,
  hour: null,
  week: null,
  sel: null,
  oc: null,
  lastSlot: undefined,
  loading: 0,
};
const STRIP = new BandStrip($('o-bands'), { lanes: 3 });

function rangeOf() {
  return RANGES.find((r) => r.key === S.range) || RANGES[0];
}

function showError(msg) {
  $('o-err').hidden = !msg;
  $('o-err').textContent = msg || '';
}

function freqParams(p) {
  if (S.fmin != null && S.fmax != null) {
    p.set('fmin_hz', String(S.fmin));
    p.set('fmax_hz', String(S.fmax));
  }
  return p;
}

function syncUrl() {
  const q = new URLSearchParams();
  if (S.range !== '24h') q.set('range', S.range);
  if (S.view !== 'mean') q.set('view', S.view);
  if (S.fmin != null) {
    q.set('fmin', String(+(S.fmin / 1e6).toFixed(4)));
    q.set('fmax', String(+(S.fmax / 1e6).toFixed(4)));
  }
  if (S.days !== 30) q.set('days', String(S.days));
  const s = q.toString();
  history.replaceState(null, '', s ? `?${s}` : location.pathname);
}

function fromUrl() {
  const u = new URLSearchParams(location.search);
  if (RANGES.some((r) => r.key === u.get('range'))) S.range = u.get('range');
  if (u.get('view') === 'peak') S.view = 'peak';
  const a = Number(u.get('fmin'));
  const b = Number(u.get('fmax'));
  if (a > 0 && b > a) {
    S.fmin = Math.round(a * 1e6);
    S.fmax = Math.round(b * 1e6);
  }
  if (PROFILE_DAYS.includes(Number(u.get('days')))) S.days = Number(u.get('days'));
}

async function init() {
  fromUrl();
  for (const r of RANGES) $('o-range').append(h('option', { value: r.key }, r.label));
  $('o-range').value = S.range;
  for (const d of PROFILE_DAYS) $('p-days').append(h('option', { value: String(d) }, `the last ${d} days`));
  $('p-days').value = String(S.days);
  syncView();
  const [registry, plan] = await Promise.all([api('/api/registry'), loadPlan()]);
  S.registry = registry;
  S.colors = assignColors(registry.profiles);
  S.watch = registry.watch || null;
  S.plan = plan;
  $('o-range').addEventListener('change', () => {
    S.range = $('o-range').value;
    syncUrl();
    loadGrid();
  });
  $('p-days').addEventListener('change', () => {
    S.days = Number($('p-days').value);
    syncUrl();
    loadProfiles();
  });
  for (const b of document.querySelectorAll('[data-view]')) {
    b.addEventListener('click', () => {
      S.view = b.dataset.view;
      syncView();
      syncUrl();
      drawAll();
    });
  }
  $('o-zoom').addEventListener('submit', (e) => {
    e.preventDefault();
    const a = Number($('o-fmin').value.replace(',', '.'));
    const b = Number($('o-fmax').value.replace(',', '.'));
    if (!(a > 0 && b > a)) {
      showError('Type the range in MHz, low to high, e.g. 150 to 175.');
      return;
    }
    zoom(Math.round(a * 1e6), Math.round(b * 1e6));
  });
  $('o-whole').addEventListener('click', () => zoom(null, null));
  $('c-close').addEventListener('click', () => select(null));
  wirePlot();
  wireProfile('ph-box', 'ph-tip', () => S.hour, (i) => `${String(i).padStart(2, '0')}:00–${String(i).padStart(2, '0')}:59 UTC`);
  wireProfile('pw-box', 'pw-tip', () => S.week, (i) => WEEKDAYS[i]);
  let resize = null;
  window.addEventListener('resize', () => {
    clearTimeout(resize);
    resize = setTimeout(drawAll, 150);
  });
  await loadAll();
  const poll = async () => {
    try {
      const st = await api('/api/status');
      paintAppBar(st.live, (n) => S.colors[n] || GREY, 'open');
      paintAlerts(st.alerts, (n) => S.colors[n] || GREY);
      renderSchedule(st.live && st.live.occupancy, st.now);
    } catch (e) {
      paintAppBar(null, () => GREY, 'closed');
    }
  };
  poll();
  setInterval(poll, 5000);
  setInterval(() => paintClock(NOW()), 1000);
}

function syncView() {
  for (const b of document.querySelectorAll('[data-view]')) b.setAttribute('aria-pressed', String(b.dataset.view === S.view));
}

function zoom(fmin, fmax) {
  showError(null);
  S.fmin = fmin;
  S.fmax = fmax;
  syncUrl();
  loadAll();
}

// --- The schedule line, and new slots ------------------------------------------------

function renderSchedule(oc, now) {
  S.oc = oc || null;
  const line = scheduleText(oc);
  if (!line) {
    $('o-sched').textContent =
      'Occupancy sweeps are off on this station (HACKRF_LIVE_OCCUPANCY): what is shown is what earlier sweeps stored.';
  } else {
    const last = oc.last;
    const parts = [line];
    if (oc.sweeping_since) parts.push('sweeping now');
    else if (oc.next_slot) parts.push(`next ${utc(oc.next_slot, false)} UTC`);
    if (last) parts.push(`last ${last.result} ${ago(last.at, now || NOW())}${last.result === 'stored' ? '' : `: ${last.detail}`}`);
    parts.push(`${oc.stored} stored, ${oc.skipped} skipped, ${oc.errors} failed since the service started`);
    $('o-sched').textContent = parts.join(' · ');
  }
  // A new slot: the newest rows of the views change.
  const slot = oc && oc.last ? oc.last.at : null;
  if (S.lastSlot !== undefined && slot !== S.lastSlot && oc && oc.last && oc.last.result === 'stored') loadAll(false);
  S.lastSlot = slot;
}

// --- Loading -------------------------------------------------------------------------

async function loadAll(clearSel = true) {
  if (clearSel) select(null);
  await Promise.all([loadGrid(), loadProfiles(), loadUnusual()]);
}

function fbucketsFor(box) {
  return Math.max(64, Math.min(2000, Math.round($(box).clientWidth || 1200)));
}

async function loadGrid() {
  const r = rangeOf();
  const until = rangeEnd(r, NOW());
  const p = freqParams(new URLSearchParams({ since: String(until - r.s), until: String(until), tbuckets: String(r.tb), fbuckets: String(fbucketsFor('o-box')) }));
  const token = ++S.loading;
  try {
    const g = await api(`/api/occupancy?${p}`);
    if (token !== S.loading) return;
    showError(null);
    S.grid = g;
    S.tb = r.tb;
    S.rows = rowMap(g, r.tb);
    S.bytes = { mean: bytesOf(g.mean), peak: bytesOf(g.peak) };
  } catch (e) {
    if (token !== S.loading) return;
    S.grid = null;
    showError(e instanceof ApiError && /no occupancy history/.test(e.message) ? 'No occupancy sweep has been stored yet.' : `Could not load the heat map: ${e.message}`);
  }
  drawHeat();
}

// The end of the range on a bucket's boundary (a slot's, an hour's, a
// UTC day's), so each bucket holds whole slots.
function rangeEnd(r, now) {
  const step = r.s / r.tb;
  return Math.ceil(now / step) * step;
}

async function loadProfiles() {
  const until = NOW();
  const base = { since: String(until - S.days * 86400), until: String(until), fbuckets: String(fbucketsFor('ph-box')) };
  try {
    const [hr, wk] = await Promise.all(
      ['hour', 'weekday'].map((by) => api(`/api/occupancy/profile?${freqParams(new URLSearchParams({ ...base, by }))}`)),
    );
    S.hour = hr.grid ? { ...hr, bytes: { mean: bytesOf(hr.mean), peak: bytesOf(hr.peak) } } : null;
    S.week = wk.grid ? { ...wk, bytes: { mean: bytesOf(wk.mean), peak: bytesOf(wk.peak) } } : null;
  } catch (e) {
    S.hour = S.week = null;
  }
  drawProfiles();
}

async function loadUnusual() {
  const until = NOW();
  const list = clear($('u-list'));
  try {
    const u = await api(`/api/occupancy/unusual?since=${until - 86400}&until=${until}&limit=2000`);
    renderUnusual(u);
  } catch (e) {
    list.append(h('p', { class: 'muted ulp' }, e instanceof ApiError && /no occupancy history/.test(e.message) ? 'Nothing stored yet.' : `Could not load: ${e.message}`));
  }
}

// --- The heat map ----------------------------------------------------------------------

function drawAll() {
  drawHeat();
  drawProfiles();
}

function blit(canvas, box, px, nb, rows) {
  const W = Math.max(8, box.clientWidth);
  const H = Math.max(8, box.clientHeight);
  canvas.width = W;
  canvas.height = H;
  const g = canvas.getContext('2d');
  g.fillStyle = '#05080f';
  g.fillRect(0, 0, W, H);
  if (!px) return;
  const off = document.createElement('canvas');
  off.width = nb;
  off.height = rows;
  off.getContext('2d').putImageData(new ImageData(px, nb, rows), 0, 0);
  g.imageSmoothingEnabled = false;
  g.drawImage(off, 0, 0, W, H);
}

function drawLegend(lv) {
  const c = $('o-bar');
  const g = c.getContext('2d');
  for (let x = 0; x < c.width; x++) {
    const k = Math.round((x / (c.width - 1)) * 255) * 3;
    g.fillStyle = `rgb(${LUT[k]},${LUT[k + 1]},${LUT[k + 2]})`;
    g.fillRect(x, 0, 1, c.height);
  }
  $('o-lv').textContent = lv ? `${lv.lo.toFixed(0)} to ${lv.hi.toFixed(0)} dBFS` : '';
}

function drawHeat() {
  const box = $('o-box');
  clear($('o-times'));
  clear($('o-freqs'));
  $('o-cell').hidden = true;
  const g = S.grid;
  if (!g || !g.grid || !g.rows) {
    blit($('o-canvas'), box, null);
    STRIP.show(null, null);
    $('o-title').textContent = '—';
    drawLegend(null);
    $('o-note').textContent = g && g.grid === null ? 'No slot in this period.' : $('o-note').textContent;
    return;
  }
  const nb = g.fbuckets;
  const bytes = S.bytes[S.view];
  S.lv = levels(bytes, g.db_min, g.db_step);
  const px = paint(bytes, nb, S.rows, S.tb, LUT, S.lv.lo, S.lv.hi, g.db_min, g.db_step);
  blit($('o-canvas'), box, px, nb, S.tb);
  drawLegend(S.lv);
  const { lo, hi } = spanOf(g);
  STRIP.show(lo, hi);
  $('o-fmin').value = mhz(lo, 3);
  $('o-fmax').value = mhz(hi, 3);
  const col = g.bucket_hz >= 1e6 ? `${(g.bucket_hz / 1e6).toFixed(2)} MHz` : `${(g.bucket_hz / 1e3).toFixed(1)} kHz`;
  $('o-title').textContent = `${rangeText(lo, hi)} · ${col} a column · ${g.tier === 'daily' ? 'a day' : bucketText(g.bucket_s)} a row`;
  for (const t of freqTicks(lo, hi, Math.max(4, Math.floor(box.clientWidth / 90)))) {
    const el = h('span', {}, t.label);
    el.style.left = `${t.pct}%`;
    $('o-freqs').append(el);
  }
  const a = g.since;
  const b = g.until;
  for (const t of timeTicks(a, b, Math.max(4, Math.floor(box.clientHeight / 56))).ticks) {
    const label = b - a > 2 * 86400 ? `${fmtUtc(t).slice(5, 10)} ${utc(t, false)}` : utc(t, false);
    const el = h('span', {}, label);
    el.style.top = `${((b - t) / (b - a)) * 100}%`;
    $('o-times').append(el);
  }
  const notes = [`${g.rows} slot${g.rows === 1 ? '' : 's'} in this period`];
  if (g.tier === 'daily') notes.push("daily roll-ups (today's slots join them after midnight UTC)");
  if (g.other_grid) notes.push(`${g.other_grid} with other settings left out`);
  notes.push('newest at the top, UTC; point at a cell for its level, click it for details, drag across to zoom in');
  $('o-note').textContent = notes.join(' · ');
  if (S.sel) markCell(S.sel);
}

function bucketText(s) {
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400) return `${(s / 3600).toFixed(s % 3600 ? 1 : 0)} h`;
  return `${(s / 86400).toFixed(1)} days`;
}

function pointCell(e) {
  const g = S.grid;
  if (!g || !g.grid) return null;
  const r = $('o-box').getBoundingClientRect();
  const x = (e.clientX - r.left) / Math.max(1, r.width);
  const y = (e.clientY - r.top) / Math.max(1, r.height);
  if (x < 0 || x > 1 || y < 0 || y > 1) return null;
  return { cell: cellAt(g, S.tb, x, y), x, y, r };
}

function tipText(c) {
  const g = S.grid;
  const mean = cellLevel(S.bytes.mean, S.rows, c, g.fbuckets, g.db_min, g.db_step);
  const peak = cellLevel(S.bytes.peak, S.rows, c, g.fbuckets, g.db_min, g.db_step);
  const when = g.tier === 'daily' ? fmtUtc(c.t0).slice(0, 10) : `${fmtUtc(c.t0)}–${utc(c.t1, false)}`;
  const level = mean == null ? 'no slot' : `mean ${dbText(mean)} · peak ${dbText(peak)}`;
  return `${when} UTC · ${rangeText(c.f0, c.f1)} · ${level}`;
}

function wirePlot() {
  const box = $('o-box');
  const tip = $('o-tip');
  const drag = $('o-drag');
  let down = null;
  box.addEventListener('pointerdown', (e) => {
    const p = pointCell(e);
    if (!p) return;
    down = { x: p.x, px: e.clientX };
    box.setPointerCapture(e.pointerId);
  });
  box.addEventListener('pointermove', (e) => {
    const p = pointCell(e);
    if (down && p) {
      const a = Math.min(down.x, p.x);
      const b = Math.max(down.x, p.x);
      drag.hidden = Math.abs(e.clientX - down.px) < 6;
      drag.style.left = `${a * 100}%`;
      drag.style.width = `${(b - a) * 100}%`;
    }
    if (!p) {
      tip.hidden = true;
      return;
    }
    tip.textContent = tipText(p.cell);
    tip.hidden = false;
    const tw = tip.offsetWidth;
    const x = e.clientX - p.r.left;
    tip.style.left = `${Math.min(Math.max(0, x + 12), p.r.width - tw)}px`;
    tip.style.top = `${Math.max(0, e.clientY - p.r.top - 30)}px`;
  });
  box.addEventListener('pointerleave', () => {
    if (!down) tip.hidden = true;
  });
  const end = (e) => {
    if (!down) return;
    const p = pointCell(e);
    const start = down;
    down = null;
    drag.hidden = true;
    if (!p) return;
    if (Math.abs(e.clientX - start.px) >= 6) {
      const z = zoomRange(S.grid, start.x, p.x);
      if (z) zoom(z.fmin, z.fmax);
      return;
    }
    select({ ...p.cell, tier: S.grid.tier });
  };
  box.addEventListener('pointerup', end);
  box.addEventListener('pointercancel', () => {
    down = null;
    drag.hidden = true;
  });
}

function markCell(c) {
  const g = S.grid;
  const el = $('o-cell');
  if (!g || !g.grid || c.t1 <= g.since || c.t0 >= g.until) {
    el.hidden = true;
    return;
  }
  const { lo, hi } = spanOf(g);
  const span = hi - lo;
  const T = g.until - g.since;
  el.style.left = `${((Math.max(c.f0, lo) - lo) / span) * 100}%`;
  el.style.width = `${Math.max(0.3, ((Math.min(c.f1, hi) - Math.max(c.f0, lo)) / span) * 100)}%`;
  el.style.top = `${((g.until - Math.min(c.t1, g.until)) / T) * 100}%`;
  el.style.height = `${Math.max(0.5, ((Math.min(c.t1, g.until) - Math.max(c.t0, g.since)) / T) * 100)}%`;
  el.hidden = false;
}

// --- The cell's details -------------------------------------------------------------------

async function select(c) {
  S.sel = c;
  const kv = clear($('c-kv'));
  $('c-empty').hidden = !!c;
  $('c-kv').hidden = !c;
  $('c-acts').hidden = !c;
  $('c-close').hidden = !c;
  $('c-watchnote').hidden = true;
  if (!c) {
    $('o-cell').hidden = true;
    return;
  }
  markCell(c);
  const add = (k, v) => kv.append(h('dt', {}, k), h('dd', {}, v));
  const when = c.tier === 'daily' ? `${fmtUtc(c.t0).slice(0, 10)} (the day's roll-up)` : `${fmtUtc(c.t0)}–${utc(c.t1, false)} UTC`;
  add('When', when);
  add('Frequency', rangeText(c.f0, c.f1));
  const mid = (c.f0 + c.f1) / 2;
  const here = S.plan ? bandsAt(S.plan.bands, mid) : [];
  add('Band plan', here.length ? here.map((b) => b.name).join(' · ') : 'no band in the plan here');
  if (S.grid && c.ti != null) {
    const g = S.grid;
    add('In the heat map', `mean ${dbText(cellLevel(S.bytes.mean, S.rows, c, g.fbuckets, g.db_min, g.db_step))} · peak ${dbText(cellLevel(S.bytes.peak, S.rows, c, g.fbuckets, g.db_min, g.db_step))}`);
  }
  const usual = h('dd', {}, 'Loading…');
  kv.append(h('dt', {}, 'Nearest slot'), usual);
  const wh = watchHref(c.f0, c.f1, S.watch);
  $('c-watch').hidden = !wh;
  if (wh) $('c-watch').href = wh;
  $('c-watchnote').hidden = !wh;
  $('c-log').href = logHref(c.f0, c.f1, c.t0, c.t1);
  try {
    const p = new URLSearchParams({ ts: String((c.t0 + c.t1) / 2), fmin_hz: String(c.f0), fmax_hz: String(c.f1) });
    const d = await api(`/api/occupancy/cell?${p}`);
    if (S.sel !== c) return;
    const s = d.slot;
    clear(usual).append(
      `${fmtUtc(s.ts)} UTC (${s.passes} passes, ${s.seconds.toFixed(1)} s): mean ${dbText(d.mean_db)}, peak ${dbText(d.peak_db)}`,
      h('br'),
      d.usual_db == null
        ? h('span', { class: 'muted' }, `Usual level: not enough history at this hour yet (the ${d.baseline_days} days before)`)
        : h('span', {}, `Usual at this hour, the ${d.baseline_days} days before: ${dbText(d.usual_db)} (${d.mean_db - d.usual_db >= 0 ? '+' : ''}${(d.mean_db - d.usual_db).toFixed(1)} dB)`),
    );
  } catch (e) {
    if (S.sel !== c) return;
    clear(usual).append(h('span', { class: 'muted' }, e.message));
  }
}

// --- New or unusual ----------------------------------------------------------------------

function renderUnusual(u) {
  const list = clear($('u-list'));
  $('u-sub').textContent = `last 24 hours · ${u.slots} slot${u.slots === 1 ? '' : 's'}`;
  if (!u.slots) {
    list.append(h('p', { class: 'muted ulp' }, 'No slot in the last 24 hours.'));
    return;
  }
  if (!u.items.length) {
    const why =
      u.no_baseline === u.slots
        ? `Not enough history yet: each slot is compared with at least 3 slots at the same UTC hour on the ${u.baseline_days} days before.`
        : `Nothing ${u.threshold_db} dB or more over its usual level.`;
    list.append(h('p', { class: 'muted ulp' }, why));
    return;
  }
  const groups = groupUnusual(u.items, S.grid && S.grid.grid ? S.grid.grid.bin_hz : 0);
  const shown = groups.slice(0, 60);
  const slotS = ((S.oc && S.oc.interval_min) || 15) * 60;
  for (const g of shown) {
    const it = g.best;
    const here = S.plan ? bandsAt(S.plan.bands, it.freq_hz) : [];
    const seen = g.slots > 1 ? `${utc(g.first, false)}–${utc(g.last, false)}, ${g.slots} slots` : utc(g.last, false);
    const row = h(
      'button',
      { type: 'button', class: 'urow', role: 'listitem', title: here.map((b) => b.name).join(' · ') || 'No band in the plan here' },
      h(
        'span',
        { class: 'uname' },
        h('span', { class: 'mono' }, rangeText(g.lo_hz, g.hi_hz)),
        h('br'),
        h('span', { class: 'muted' }, `peak ${mhz(it.freq_hz, 3)} · ${here.length ? here[0].name : 'no band in the plan'}`),
        h('br'),
        h('span', { class: 'muted mono' }, `${seen} UTC`),
      ),
      h('span', { class: 'mono uex' }, `+${it.excess_db.toFixed(0)} dB`, h('br'), h('span', { class: 'muted' }, `${it.level_db.toFixed(0)} / ${it.usual_db.toFixed(0)}`)),
    );
    // The newest slot it was seen in.
    row.addEventListener('click', () => {
      const l = g.latest;
      select({ t0: l.ts, t1: l.ts + slotS, f0: l.lo_hz, f1: l.hi_hz, tier: 'slots', ti: null });
    });
    list.append(row);
  }
  if (groups.length > shown.length) list.append(h('p', { class: 'muted ulp' }, `${groups.length - shown.length} more not shown`));
  if (u.no_baseline) list.append(h('p', { class: 'muted ulp' }, `${u.no_baseline} slot${u.no_baseline === 1 ? '' : 's'} without enough history at their hour to compare.`));
}

// --- Hour of day and weekday --------------------------------------------------------------

function drawProfile(canvasId, boxId, rowsId, data, labels, every = 1) {
  const box = $(boxId);
  clear($(rowsId));
  if (!data) {
    blit($(canvasId), box, null);
    return;
  }
  const n = labels.length;
  const nb = data.fbuckets;
  // paint() draws bucket i at y = n - 1 - i; these rows read top-down
  // (00:00, Monday first), so bucket i holds row n - 1 - i.
  const rows = Int32Array.from({ length: n }, (_, i) => (data.counts[n - 1 - i] ? n - 1 - i : -1));
  const bytes = data.bytes[S.view];
  const lv = S.lv || levels(bytes, data.db_min, data.db_step);
  const px = paint(bytes, nb, rows, n, LUT, lv.lo, lv.hi, data.db_min, data.db_step);
  blit($(canvasId), box, px, nb, n);
  labels.forEach((label, i) => {
    if (i % every) return;
    const el = h('span', {}, label);
    el.style.top = `${((i + 0.5) / n) * 100}%`;
    $(rowsId).append(el);
  });
}

function drawProfiles() {
  const hours = Array.from({ length: 24 }, (_, i) => `${String(i).padStart(2, '0')}:00`);
  drawProfile('ph-canvas', 'ph-box', 'ph-rows', S.hour, hours, 3);
  drawProfile('pw-canvas', 'pw-box', 'pw-rows', S.week, WEEKDAYS);
  clear($('ph-freqs'));
  const d = S.hour;
  if (d && d.grid) {
    const { lo, hi } = spanOf(d);
    for (const t of freqTicks(lo, hi, Math.max(4, Math.floor($('ph-box').clientWidth / 90)))) {
      const el = h('span', {}, t.label);
      el.style.left = `${t.pct}%`;
      $('ph-freqs').append(el);
    }
    $('p-sub').textContent = `UTC · ${d.rows} slots · same colours as the heat map`;
  } else {
    $('p-sub').textContent = 'UTC · no slot in this period';
  }
}

function wireProfile(boxId, tipId, data, label) {
  const box = $(boxId);
  const tip = $(tipId);
  box.addEventListener('pointermove', (e) => {
    const d = data();
    if (!d || !d.grid) return;
    const r = box.getBoundingClientRect();
    const n = d.counts.length;
    const x = Math.min(0.9999, Math.max(0, (e.clientX - r.left) / r.width));
    const i = Math.min(n - 1, Math.max(0, Math.floor(((e.clientY - r.top) / r.height) * n)));
    const fi = Math.floor(x * d.fbuckets);
    const f0 = d.f_lo_hz + fi * d.bucket_hz;
    const b = d.bytes[S.view][i * d.fbuckets + fi];
    const v = d.counts[i] && b ? d.db_min + b * d.db_step : null;
    tip.textContent = `${label(i)} · ${rangeText(f0, Math.min(f0 + d.bucket_hz, d.grid.hi_hz))} · ${d.counts[i]} slots · ${dbText(v)}`;
    tip.hidden = false;
    const tw = tip.offsetWidth;
    tip.style.left = `${Math.min(Math.max(0, e.clientX - r.left + 12), r.width - tw)}px`;
    tip.style.top = `${Math.max(0, e.clientY - r.top - 30)}px`;
  });
  box.addEventListener('pointerleave', () => {
    tip.hidden = true;
  });
}

init().catch((e) => showError(`Could not load: ${e.message}`));
