// The Log screen: search everything the live service has logged, see when
// it was found and when the receiver wasn't listening, and look at any row
// on the waterfall of its moment. Profiles, colours and the fields shown
// come from /api/registry; nothing here names a protocol.

import { api } from './api.js';
import { paintAppBar, paintClock } from './appbar.js';
import { AutoLevel, makeLut, resample } from './waterfall.js';
import { assignColors, clear, fmtUtc, h, mhz, parseFreqs, parseUtc, utc } from './util.js';

const $ = (id) => document.getElementById(id);
const NOW = () => Date.now() / 1000;
const LUT = makeLut();
const PAGE = 200;
const BUCKETS = 96;
const GAP_LABEL = { paused: 'paused', error: 'error', idle: 'stopped', down: 'service down' };
const GAP_CLASS = { paused: 'paused', error: 'error', idle: 'stopped', down: 'stopped' };

const S = {
  registry: null,
  profiles: {},
  colors: {},
  on: new Set(),
  q: null,
  rows: [],
  more: false,
  sel: null,
  bucket: null,
  coverage: [],
  status: null,
  statusAt: 0,
};

const color = (name) => S.colors[name] || '#8a96a6';

// --- Times ----------------------------------------------------------------------------

function when(ts, long) {
  if (ts == null) return '';
  return long ? `${new Date(ts * 1000).toISOString().slice(5, 10)} ${utc(ts)}` : utc(ts);
}

// --- Start-up ---------------------------------------------------------------------------

async function init() {
  S.registry = await api('/api/registry');
  for (const p of S.registry.profiles) S.profiles[p.name] = p;
  S.colors = assignColors(S.registry.profiles);
  for (const p of S.registry.profiles) S.on.add(p.name);
  buildChips();
  wire();
  pollStatus();
  setInterval(pollStatus, 10000);
  setInterval(() => paintClock(S.status ? S.status.now + (NOW() - S.statusAt) : NOW()), 1000);
  const around = fromUrl();
  await search(readForm(), around);
}

async function pollStatus() {
  try {
    const st = await api('/api/status');
    S.status = st;
    S.statusAt = NOW();
    paintAppBar(st.live, color, 'open');
    const log = st.live && st.live.log;
    if (log) $('logstats').textContent = `${(log.file_bytes / 1048576).toFixed(1)} MiB of ${log.max_db_mb} MiB · kept ${log.max_age_days} days`;
  } catch (e) {
    paintAppBar(null, color, 'closed');
  }
}

function wire() {
  $('sform').addEventListener('submit', (e) => {
    e.preventDefault();
    runForm();
  });
  $('range').addEventListener('change', () => {
    if ($('range').value === 'custom' && !$('from').value) {
      const now = NOW();
      $('from').value = fmtUtc(now - 3600);
      $('to').value = '';
    }
  });
  for (const id of ['from', 'to']) $(id).addEventListener('input', () => { $('range').value = 'custom'; });
  $('reset').addEventListener('click', () => {
    $('sform').reset();
    $('range').value = '3600';
    for (const p of S.registry.profiles) S.on.add(p.name);
    buildChips();
    runForm();
  });
  $('csv').addEventListener('click', exportCsv);
  $('more-btn').addEventListener('click', loadMore);
  $('bucket-clear').addEventListener('click', () => {
    S.bucket = null;
    $('bucket').hidden = true;
    for (const c of $('tl').querySelectorAll('.c.on')) c.classList.remove('on');
    fetchRows(S.q);
  });
  $('d-close').addEventListener('click', closeDetail);
  $('d-subj').addEventListener('click', () => {
    if (!S.sel || !S.sel.subject) return;
    $('subj').value = S.sel.subject;
    runForm();
  });
}

function buildChips() {
  const box = $('pchips');
  clear(box);
  for (const p of S.registry.profiles) {
    const chip = h('button', { type: 'button', class: 'tchip', '--pc': color(p.name), 'aria-pressed': String(S.on.has(p.name)) }, h('span', { class: 'sw' }), p.name);
    chip.addEventListener('click', () => {
      if (S.on.has(p.name)) S.on.delete(p.name);
      else S.on.add(p.name);
      buildChips();
    });
    box.append(chip);
  }
}

// The search in the address bar, so a search can be bookmarked or linked
// (the Live screen links rows here with subject, profiles and around).
function fromUrl() {
  const u = new URLSearchParams(location.search);
  if (u.get('subject')) $('subj').value = u.get('subject');
  if (u.get('text')) $('txt').value = u.get('text');
  if (u.get('freq')) $('freq').value = u.get('freq');
  if (u.get('profiles')) {
    S.on = new Set(u.get('profiles').split(',').filter((n) => S.profiles[n]));
    buildChips();
  }
  const around = Number(u.get('around'));
  if (u.get('since') || around) {
    $('range').value = 'custom';
    const since = around ? around - 1800 : Number(u.get('since'));
    const until = around ? around + 1800 : Number(u.get('until')) || null;
    $('from').value = fmtUtc(since);
    $('to').value = until ? fmtUtc(until) : '';
  } else if (u.get('range')) {
    $('range').value = u.get('range');
  }
  return around || null;
}

function readForm() {
  const now = NOW();
  let since;
  let until = now;
  if ($('range').value === 'custom') {
    since = parseUtc($('from').value);
    if ($('to').value.trim()) until = parseUtc($('to').value);
  } else {
    since = now - Number($('range').value);
  }
  if (since >= until) throw new Error('"From" must be before "To".');
  const freqs = parseFreqs($('freq').value);
  const all = S.on.size === S.registry.profiles.length;
  return {
    since,
    until,
    range: $('range').value,
    subject: $('subj').value.trim(),
    text: $('txt').value.trim(),
    freq_hz: freqs[0] || null,
    profiles: all ? [] : [...S.on],
  };
}

function params(q, extra = {}) {
  const p = new URLSearchParams();
  p.set('since', String(q.since));
  p.set('until', String(q.until));
  if (q.subject) p.set('subject', q.subject);
  if (q.text) p.set('text', q.text);
  if (q.freq_hz) {
    p.set('freq_hz', String(q.freq_hz));
    p.set('freq_tolerance_hz', '12500');
  }
  if (q.profiles.length) p.set('profiles', q.profiles.join(','));
  for (const [k, v] of Object.entries(extra)) p.set(k, String(v));
  return p;
}

function showError(msg) {
  $('serr').hidden = !msg;
  $('serr').textContent = msg || '';
}

async function runForm() {
  showError(null);
  try {
    await search(readForm());
  } catch (e) {
    showError(e.message);
  }
}

async function search(q, around = null) {
  S.q = q;
  S.bucket = null;
  $('bucket').hidden = true;
  const url = new URLSearchParams();
  if (q.range === 'custom') {
    url.set('since', String(Math.round(q.since)));
    url.set('until', String(Math.round(q.until)));
  } else {
    url.set('range', q.range);
  }
  if (q.subject) url.set('subject', q.subject);
  if (q.text) url.set('text', q.text);
  if ($('freq').value.trim()) url.set('freq', $('freq').value.trim());
  if (q.profiles.length) url.set('profiles', q.profiles.join(','));
  history.replaceState(null, '', `/log?${url}`);
  const [hist, cov] = await Promise.all([
    api(`/api/log/histogram?${params(q, { buckets: BUCKETS })}`),
    api(`/api/coverage?since=${q.since}&until=${q.until}`),
  ]);
  S.coverage = cov.coverage;
  renderTimeline(hist, cov.coverage, q);
  await fetchRows(q);
  if (around) {
    let best = null;
    for (const r of S.rows) if (!best || Math.abs(r.ts - around) < Math.abs(best.ts - around)) best = r;
    if (best) openDetail(best);
  }
}

async function fetchRows(q, bucket = S.bucket) {
  const range = bucket ? { ...q, since: bucket.from, until: bucket.to } : q;
  const log = await api(`/api/log?${params(range, { limit: PAGE })}`);
  S.rows = log.rows;
  S.more = log.more;
  renderRows();
}

async function loadMore() {
  if (!S.rows.length) return;
  const range = S.bucket ? { ...S.q, since: S.bucket.from, until: S.bucket.to } : S.q;
  const log = await api(`/api/log?${params(range, { limit: PAGE, before_id: S.rows[S.rows.length - 1].id })}`);
  S.rows = S.rows.concat(log.rows);
  S.more = log.more;
  renderRows();
}

// --- The timeline --------------------------------------------------------------------------

function renderTimeline(hist, coverage, q) {
  const tl = $('tl');
  clear(tl);
  const span = q.until - q.since;
  const long = span > 86400;
  const totals = hist.counts.map((c) => Object.values(c).reduce((a, b) => a + b, 0));
  const max = Math.max(1, ...totals);
  const byProfile = {};
  hist.counts.forEach((c, i) => {
    const from = q.since + i * hist.bucket_s;
    const to = from + hist.bucket_s;
    const parts = Object.entries(c).map(([p, n]) => `${p} ${n}`).join(', ');
    const col = h('button', { type: 'button', class: 'c', 'aria-label': `${when(from, long)}–${when(to, long)}: ${parts || 'nothing'}`, title: `${when(from, long)}–${when(to, long)} UTC\n${parts || 'nothing'}` });
    for (const p of S.registry.profiles) {
      const n = c[p.name];
      if (!n) continue;
      byProfile[p.name] = (byProfile[p.name] || 0) + n;
      const seg = h('span', { class: 's', '--pc': color(p.name) });
      seg.style.height = `${(n / max) * 100}%`;
      col.append(seg);
    }
    col.addEventListener('click', () => {
      for (const x of tl.querySelectorAll('.c.on')) x.classList.remove('on');
      col.classList.add('on');
      S.bucket = { from, to };
      $('bucket').hidden = false;
      $('bucket-label').textContent = `${when(from, long)}–${when(to, long)} UTC`;
      fetchRows(S.q);
    });
    tl.append(col);
  });
  const lane = $('covlane');
  clear(lane);
  for (const seg of coverage) {
    const left = ((Math.max(seg.from, q.since) - q.since) / span) * 100;
    const width = ((Math.min(seg.to, q.until) - Math.max(seg.from, q.since)) / span) * 100;
    if (width <= 0) continue;
    if (seg.state === 'running') {
      const s = h('span', { '--pc': color(seg.profile), title: `${seg.profile}: ${when(seg.from, long)}–${when(seg.to, long)}` });
      s.style.left = `${left}%`;
      s.style.width = `${width}%`;
      lane.append(s);
    } else {
      const g = h('div', { class: `gap ${GAP_CLASS[seg.state] || 'stopped'}${width < 0.6 ? ' thin' : ''}`, title: `${GAP_LABEL[seg.state] || seg.state}: ${when(seg.from, long)}–${when(seg.to, long)}` });
      g.style.left = `${left}%`;
      g.style.width = `${width}%`;
      if (width > 7) g.append(h('span', { class: 'gl' }, GAP_LABEL[seg.state] || seg.state));
      tl.append(g);
    }
  }
  const ticks = $('tlx');
  clear(ticks);
  // As many labels as fit: about one per 120 px.
  const n = Math.max(2, Math.min(6, Math.floor(tl.clientWidth / 120)));
  for (let i = 0; i <= n; i++) {
    const t = h('span', {}, when(q.since + (span * i) / n, long));
    t.style.left = `${(i / n) * 100}%`;
    if (i === 0) t.style.transform = 'none';
    if (i === n) t.style.transform = 'translateX(-100%)';
    ticks.append(t);
  }
  const legend = $('tl-legend');
  clear(legend);
  for (const p of S.registry.profiles) {
    if (!byProfile[p.name]) continue;
    legend.append(h('span', { '--pc': color(p.name) }, h('span', { class: 'sw' }), p.name, ' ', h('span', { class: 'mono muted' }, `${byProfile[p.name]}`)));
  }
  legend.append(h('span', {}, h('i', { class: 'hatch' }), 'stopped'), h('span', {}, h('i', { class: 'hatch paused' }), 'paused'), h('span', {}, h('i', { class: 'hatch error' }), 'error'));
  const b = hist.bucket_s;
  const per = b >= 3600 ? `${(b / 3600).toFixed(1)} h` : b >= 60 ? `${Math.round(b / 60)} min` : `${Math.round(b)} s`;
  $('tl-title').textContent = `${fmtUtc(q.since)} → ${fmtUtc(q.until)} UTC · ${per} per bar`;
}

// --- Results -------------------------------------------------------------------------------

function fmtValue(v) {
  if (v == null) return '—';
  if (Array.isArray(v)) return v.join(', ');
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

function summary(r) {
  const p = S.profiles[r.profile];
  const fields = r.fields || {};
  const view = p && p.views.find((v) => v.name === 'table');
  const keys = view && view.fields.length ? view.fields : Object.keys(fields);
  const out = [];
  for (const k of keys) {
    if (fields[k] == null || fields[k] === '' || k === 'message') continue;
    out.push(h('span', {}, h('span', { class: 'muted' }, `${k} `), fmtValue(fields[k])));
    if (out.length >= 5) break;
  }
  return out;
}

function renderRows() {
  const tb = $('rows');
  clear(tb);
  const long = S.q && S.q.until - S.q.since > 86400;
  for (const r of S.rows) {
    const subj = r.subject
      ? h('button', { type: 'button', class: 'linkbtn', 'aria-label': `Search ${r.subject}` }, r.subject)
      : h('span', { class: 'muted' }, '—');
    if (r.subject) {
      subj.addEventListener('click', (e) => {
        e.stopPropagation();
        $('subj').value = r.subject;
        runForm();
      });
    }
    const tr = h(
      'tr',
      { class: `click${S.sel && S.sel.id === r.id ? ' sel' : ''}`, '--pc': color(r.profile) },
      h('td', { class: 'mono' }, when(r.ts, long)),
      h('td', { class: 'mono muted' }, r.ts_end && r.ts_end !== r.ts ? utc(r.ts_end) : ''),
      h('td', {}, h('span', { class: 'pbadge' }, r.profile)),
      h('td', { class: 'kind' }, r.kind),
      h('td', { class: 'mono r' }, r.freq_hz ? mhz(r.freq_hz) : '—'),
      h('td', { class: 'mono r' }, r.level_db == null ? '—' : Number(r.level_db).toFixed(1)),
      h('td', { class: 'mono' }, subj),
      h('td', {}, h('b', {}, r.label || '')),
      h('td', { class: 'wc' }, h('span', { class: 'row wr' }, summary(r))),
    );
    tr.addEventListener('click', () => openDetail(r));
    tb.append(tr);
  }
  const none = !S.rows.length;
  $('empty').hidden = !none;
  $('tscroll').hidden = none;
  $('more').hidden = !S.more;
  $('count').textContent = none ? '' : `${S.rows.length}${S.more ? '+' : ''} rows, newest first`;
  if (none) {
    $('empty-why').textContent = S.bucket ? 'Nothing matches in the chosen bar.' : 'Nothing matches in this range. Widen the range or remove a filter.';
    const wanted = S.q.profiles.length ? S.q.profiles : null;
    const ran = new Set(S.coverage.filter((c) => c.state === 'running').map((c) => c.profile));
    const idle = wanted ? wanted.filter((p) => !ran.has(p)) : [];
    $('empty-cover').textContent = idle.length ? `${idle.join(', ')} did not run in this range.` : '';
  }
}

// --- Detail -----------------------------------------------------------------------------------

function closeDetail() {
  S.sel = null;
  $('detail').hidden = true;
  $('glog').classList.add('closed');
  renderRows();
}

function openDetail(r) {
  S.sel = r;
  renderRows();
  $('detail').hidden = false;
  $('glog').classList.remove('closed');
  $('detail').style.setProperty('--pc', color(r.profile));
  $('d-profile').textContent = r.profile;
  $('d-kind').textContent = r.kind;
  $('d-subject').textContent = r.subject || '—';
  $('d-label').textContent = r.label || '';
  $('d-when').textContent = `${fmtUtc(r.ts)}:${utc(r.ts).slice(6)}${r.ts_end && r.ts_end !== r.ts ? ` → ${utc(r.ts_end)}` : ''} UTC · ${r.freq_hz ? mhz(r.freq_hz) : '—'} MHz · ${r.level_db == null ? '—' : Number(r.level_db).toFixed(1)} dB`;
  const kv = $('d-fields');
  clear(kv);
  for (const [k, v] of Object.entries(r.fields || {})) kv.append(h('dt', {}, k), h('dd', { class: 'mono' }, fmtValue(v)));
  $('d-raw').textContent = r.raw || '';
  $('d-subj').disabled = !r.subject;
  $('d-hist').setAttribute('href', `/history?at=${Math.round(r.ts)}`);
  const mapped = r.subject && (S.profiles[r.profile].views || []).some((v) => v.name === 'map');
  $('d-map').hidden = !mapped;
  $('d-map').setAttribute('href', `/map?${new URLSearchParams({ at: String(Math.round(r.ts)), profile: r.profile, subject: r.subject || '' })}`);
  drawThumb(r);
}

// The spectrum a minute either side of the row, newest at the top like the
// Live waterfall, with the row's time and frequency marked.
async function drawThumb(r) {
  const box = $('d-thumb');
  const canvas = $('d-canvas');
  for (const el of box.querySelectorAll('.band, .fmark')) el.remove();
  const g = canvas.getContext('2d');
  canvas.width = Math.max(8, box.clientWidth);
  canvas.height = Math.max(8, box.clientHeight);
  g.fillStyle = '#05080f';
  g.fillRect(0, 0, canvas.width, canvas.height);
  const t = r.ts;
  const t0 = t - 60;
  const t1 = (r.ts_end || t) + 60;
  let seg = null;
  try {
    const { segments } = await api(`/api/spectrum/segments?since=${t0}&until=${t1}`);
    seg = segments.find((s) => s.profile === r.profile && s.ts_start <= t && (s.ts_end == null || s.ts_end >= t)) || segments.find((s) => s.profile === r.profile);
  } catch (e) {
    seg = null;
  }
  if (S.sel !== r) return;
  if (!seg) {
    $('d-thumbcap').textContent = 'No spectrum was recorded then: the history starts on 2026-11-02 (AIS a little later), and nothing is recorded while paused or stopped.';
    return;
  }
  const data = await api(`/api/spectrum?segment=${seg.id}&since=${t0}&until=${t1}&buckets=120&tier=second`);
  if (S.sel !== r) return;
  const W = canvas.width;
  const H = canvas.height;
  const bins = seg.bins;
  const peaks = Uint8Array.from(atob(data.peak), (c) => c.charCodeAt(0));
  const level = new AutoLevel(50, 6);
  const rows = [];
  for (let i = 0; i < data.ts.length; i++) {
    const db = Float32Array.from(peaks.subarray(i * bins, (i + 1) * bins), (b) => seg.db_min + b * seg.db_step);
    level.update(db);
    rows.push(db);
  }
  const span = t1 - t0;
  const rowH = Math.max(1, Math.ceil((data.bucket_s / span) * H));
  const img = g.createImageData(W, H);
  for (let i = 0; i < rows.length; i++) {
    const px = resample(rows[i], W, 'max');
    const y0 = Math.floor(((t1 - data.ts[i] - data.bucket_s) / span) * H);
    for (let y = Math.max(0, y0); y < Math.min(H, y0 + rowH); y++) {
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
  const band = h('div', { class: 'band' });
  const top = ((t1 - (r.ts_end || t)) / span) * 100;
  const height = Math.max(1.5, (((r.ts_end || t) - t) / span) * 100);
  band.style.top = `${top}%`;
  band.style.height = `${height}%`;
  box.append(band);
  const lo = seg.center_hz - seg.sample_rate_hz / 2;
  if (r.freq_hz && r.freq_hz >= lo && r.freq_hz <= lo + seg.sample_rate_hz) {
    const mark = h('span', { class: 'fmark' });
    mark.style.left = `${((r.freq_hz - lo) / seg.sample_rate_hz) * 100}%`;
    box.append(mark);
  }
  $('d-thumbcap').textContent = `${seg.profile} at ${mhz(seg.center_hz)} MHz, ${mhz(lo, 3)}–${mhz(lo + seg.sample_rate_hz, 3)}; ${utc(t0)}–${utc(t1)} UTC, newest at the top. The band marks this row${r.freq_hz ? ', the triangle its frequency' : ''}.`;
}

// --- Export -------------------------------------------------------------------------------------

function csvCell(v) {
  const s = v == null ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function exportCsv() {
  const head = ['time_utc', 'end_utc', 'profile', 'kind', 'freq_hz', 'level_db', 'subject', 'label', 'fields', 'raw'];
  const lines = [head.join(',')];
  for (const r of S.rows) {
    lines.push(
      [new Date(r.ts * 1000).toISOString(), r.ts_end ? new Date(r.ts_end * 1000).toISOString() : '', r.profile, r.kind, r.freq_hz, r.level_db, r.subject, r.label, r.fields, r.raw]
        .map(csvCell)
        .join(','),
    );
  }
  const blob = new Blob([lines.join('\n') + '\n'], { type: 'text/csv' });
  const a = h('a', { href: URL.createObjectURL(blob), download: `hackrf-log-${fmtUtc(NOW()).replace(/[: ]/g, '')}.csv` });
  document.body.append(a);
  a.click();
  a.remove();
}

init().catch((e) => showError(`Could not load: ${e.message}`));
