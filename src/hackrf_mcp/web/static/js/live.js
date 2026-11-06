// The Live screen: what the receiver is doing now, its waterfall, what it
// decodes as it decodes it, and the controls to change it. Everything about
// profiles (names, channels, which fields to show) comes from /api/registry;
// nothing here names a protocol.

import { api, post, LiveSocket } from './api.js';
import { bandList, bandsInView, leftText, parseMhz, scrolled, watchTuning } from './watchutil.js';
import { loadPlan } from './bandstrip.js';
import { alertKeys, paintAlerts, paintAppBar, paintClock, sameKeys } from './appbar.js';
import { Waterfall } from './waterfall.js';
import { ago, assignColors, clear, fmtUtc, h, inSeconds, mhz, mhzShort, mib, parseFreqs, rotation, utc } from './util.js';
import { captureBody, captureQuery, pickRange, rangeLabel, rateText, sizeText, stateText, stateTone, ticked } from './captureutil.js';

const $ = (id) => document.getElementById(id);
const FEED_MAX = 300;
const NOW = () => Date.now() / 1000;

const S = {
  alerted: new Set(),
  registry: null,
  profiles: {},
  colors: {},
  status: null,
  statusAt: 0,
  socket: 'connecting',
  lastFftAt: 0,
  cpu: {},
  mpm: [],
  items: [],
  held: [],
  hold: false,
  hidden: new Set(),
  showEvents: true,
  dropped: 0,
  // How long a live waterfall row is (its "seconds"): history rows of a
  // second are drawn as tall as that many live rows.
  liveRowS: 0.1,
  draft: null,
  // Captures: Select on the waterfall, the form being filled in, the list.
  selecting: false,
  cap: null,
  caps: [],
  capStatus: null,
  capKey: null,
  capOpen: new Set(),
  capPics: new Set(),
};

function profileColor(name) {
  return S.colors[name] || '#8a96a6';
}

// The station's clock: the server's "now" moved on by the time since.
function now() {
  return S.status ? S.status.now + (NOW() - S.statusAt) : NOW();
}

function live() {
  return (S.status && S.status.live) || {};
}

// --- Start-up --------------------------------------------------------------------

const WATCH_PROFILE = 'watch';
const watching = () => (live().watch ? live().watch : null);

async function init() {
  S.registry = await api('/api/registry');
  for (const p of S.registry.profiles) S.profiles[p.name] = p;
  S.colors = assignColors(S.registry.profiles);
  S.wf = new Waterfall($('wf'), (lo, hi) => {
    $('wf-lo').textContent = `${Math.round(lo)}`;
    $('wf-hi').textContent = `${Math.round(hi)} dBFS`;
  });
  buildControls();
  buildWatch();
  buildCapture();
  buildBandList().catch(() => {});
  buildChips();
  wire();
  try {
    renderStatus(await api('/api/status'));
  } catch (e) {
    showCtlError(e.message);
  }
  await Promise.all([seedFeed().catch(() => {}), prefillWaterfall().catch(() => {})]);
  new LiveSocket(onMessage, onSocket);
  setInterval(tick, 1000);
  tick();
}

function buildControls() {
  const pick = $('pick');
  clear(pick);
  for (const p of S.registry.profiles) {
    const label = p.installed ? `${p.name} — ${p.summary.split(/[;(]/)[0].trim()}` : `${p.name} (needs ${p.programs.join(' or ')})`;
    pick.append(h('option', { value: p.name, disabled: !p.installed }, label));
  }
  const lna = $('g-lna');
  const vga = $('g-vga');
  lna.append(h('option', { value: '' }, 'profile'));
  vga.append(h('option', { value: '' }, 'profile'));
  for (let g = 0; g <= 40; g += 8) lna.append(h('option', { value: g }, `${g} dB`));
  for (let g = 0; g <= 62; g += 2) vga.append(h('option', { value: g }, `${g} dB`));
  pick.addEventListener('change', updatePick);
  updatePick();
}

function updatePick() {
  const p = S.profiles[$('pick').value];
  const freqs = $('pick-freqs');
  freqs.hidden = !(p && (p.needs_freqs || (!p.fixed_freqs && p.default_freqs_hz.length === 0)));
  freqs.placeholder = p && p.needs_freqs ? 'channels, MHz (required)' : 'channels, MHz';
  updateStartLabel();
}

function updateStartLabel() {
  const l = live();
  const running = l.state === 'running' || l.state === 'paused' || l.state === 'waiting' || l.state === 'starting';
  $('btn-start-text').textContent = running ? 'Switch' : 'Start';
}

function wire() {
  $('btn-start').addEventListener('click', startPicked);
  $('btn-stop').addEventListener('click', stopLive);
  $('btn-rot').addEventListener('click', openEditor);
  $('rot-close').addEventListener('click', closeEditor);
  $('rot-cancel').addEventListener('click', closeEditor);
  $('rot-backdrop').addEventListener('click', closeEditor);
  $('rot-add').addEventListener('click', () => {
    const last = S.draft[S.draft.length - 1];
    S.draft.push({ profile: last ? last.profile : firstInstalled(), minutes: 10, freqs: '' });
    renderDraft();
  });
  $('rot-save').addEventListener('click', saveRotation);
  $('btn-hold').addEventListener('click', () => {
    S.hold = !S.hold;
    if (!S.hold) {
      for (const it of S.held.reverse()) addItem(it, false);
      S.held = [];
      renderFeed();
    }
    renderHoldButton();
  });
  $('btn-peak').addEventListener('click', (e) => {
    S.wf.peakHold = !S.wf.peakHold;
    e.currentTarget.setAttribute('aria-pressed', String(S.wf.peakHold));
  });
  const bandsOn = localStorage.getItem('hl.bands') !== 'off';
  S.wf.showBands(bandsOn);
  $('btn-bands').setAttribute('aria-pressed', String(bandsOn));
  $('btn-bands').addEventListener('click', (e) => {
    const on = e.currentTarget.getAttribute('aria-pressed') !== 'true';
    S.wf.showBands(on);
    e.currentTarget.setAttribute('aria-pressed', String(on));
    localStorage.setItem('hl.bands', on ? 'on' : 'off');
  });
  $('btn-wfmode').addEventListener('click', (e) => {
    S.wf.usePeak = !S.wf.usePeak;
    e.currentTarget.setAttribute('aria-pressed', String(S.wf.usePeak));
    e.currentTarget.textContent = S.wf.usePeak ? 'Peaks' : 'Means';
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('rot-drawer').hidden) closeEditor();
  });
}

function firstInstalled() {
  const p = S.registry.profiles.find((x) => x.installed);
  return p ? p.name : S.registry.profiles[0].name;
}

// --- The live socket -----------------------------------------------------------------

function onSocket(state) {
  S.socket = state;
  renderStatus(S.status);
}

function onMessage(m) {
  switch (m.type) {
    case 'status':
      S.mpm.push(m.live && m.live.decoded_1m != null ? m.live.decoded_1m : 0);
      if (S.mpm.length > 60) S.mpm.shift();
      renderStatus(m);
      break;
    case 'fft':
      onFft(m);
      break;
    case 'row':
      feedIn({ ...m, kindOf: 'row' });
      break;
    case 'event':
      feedIn({ ...m, kindOf: 'event' });
      if (m.event === 'stop' || m.event === 'pause') S.wf.pushGap(m.ts);
      break;
    case 'dropped':
      S.dropped += m.n;
      paintDropped();
      break;
    default:
      break;
  }
}

// Rows this browser missed because it fell behind the feed: the waterfall
// joins the rows either side, so say so rather than show a seamless picture.
function paintDropped() {
  const el = $('wf-drop');
  el.hidden = !S.dropped;
  el.textContent = `${S.dropped} feed message${S.dropped === 1 ? '' : 's'} missed`;
  el.title = 'This browser fell behind the live feed and missed these (waterfall rows and decoded rows); a slow connection or a busy tab does it.';
}

function onFft(m) {
  S.lastFftAt = NOW();
  if (m.seconds > 0) S.liveRowS = m.seconds;
  const det = live().tuning_detail;
  const same = det && det.center_hz === m.center_hz && det.sample_rate_hz === m.sample_rate_hz;
  const band = {
    profile: m.profile,
    center_hz: m.center_hz,
    sample_rate_hz: m.sample_rate_hz,
    freqs_hz: same ? det.freqs_hz : [],
    color: profileColor(m.profile),
  };
  if (!S.wf.band || S.wf.band.center_hz !== m.center_hz || S.wf.band.sample_rate_hz !== m.sample_rate_hz || S.wf.band.profile !== m.profile) {
    S.wf.setBand(band);
    renderBandHeader(band);
  }
  S.wf.pushFft(m);
  if (live().state === 'running') S.wf.setOverlay(null);
}

// --- Status --------------------------------------------------------------------------

function renderStatus(st) {
  if (st) {
    S.status = st;
    S.statusAt = NOW();
    const keys = alertKeys(st.alerts);
    if (!sameKeys(keys, S.alerted)) {
      S.alerted = keys;
      renderFeed();
    }
  }
  const l = live();
  const reachable = S.socket !== 'closed' && l.state && l.state !== 'unreachable';
  $('unreach').hidden = reachable || !S.status;
  if (!reachable && S.status) {
    $('unreach-title').textContent = S.socket === 'closed' ? 'Lost the connection to hackrf-web' : 'The hackrf-live service is not running';
    $('unreach-why').textContent =
      S.socket === 'closed'
        ? 'This page is reconnecting. If it keeps failing, the web service or the network is down.'
        : `${l.error || ''} Nothing is being decoded or logged. What was logged before is still there.`;
  }
  renderAppBar(reachable);
  if (!S.status) return;
  fillBandChannels();
  renderControl();
  renderRotation();
  renderBanner();
  renderOverlay();
  renderHealth();
  renderWatch();
  renderCaptureStatus();
  $('feed-rate').textContent = `${l.decoded_1m ?? '—'} msg/min`;
}

function renderAppBar() {
  paintAppBar(live(), profileColor, S.socket);
  paintAlerts(S.status && S.status.alerts, profileColor, (p) => ((S.profiles[p] || {}).views || []).some((v) => v.name === 'map'));
}

function renderControl() {
  const l = live();
  const det = l.tuning_detail;
  const name = det ? det.profile : l.target || null;
  const p = name ? S.profiles[name] : null;
  $('ctl-prof').style.setProperty('--pc', profileColor(name));
  $('cur-name').textContent = name || '—';
  $('cur-desc').textContent =
    name === WATCH_PROFILE
      ? 'Watching a range: the waterfall only, nothing decoded'
      : p
        ? p.summary.split(';')[0]
        : l.state === 'paused'
          ? 'Paused'
          : 'Nothing running';
  $('t-center').textContent = det ? mhz(det.center_hz) : '—';
  $('t-rate').textContent = det && det.sample_rate_hz ? (det.sample_rate_hz / 1e6).toFixed(3).replace(/0+$/, '').replace(/\.$/, '') : '—';
  $('t-lna').textContent = det ? det.lna_gain_db : '—';
  $('t-vga').textContent = det ? det.vga_gain_db : '—';
  $('t-amp').textContent = det ? (det.amp ? 'on' : 'off') : '—';
  $('btn-stop').disabled = !l.desired && l.state !== 'running';
  updateStartLabel();
}

// After a switch the first FFT rows can arrive before the status that
// names the new channels: add them once the status catches up.
function fillBandChannels() {
  const det = live().tuning_detail;
  const b = S.wf && S.wf.band;
  if (!det || !b || det.center_hz !== b.center_hz || det.sample_rate_hz !== b.sample_rate_hz) return;
  if ((b.freqs_hz || []).join() === det.freqs_hz.join()) return;
  const band = { ...b, freqs_hz: det.freqs_hz };
  S.wf.setBand(band);
  renderBandHeader(band);
}

function renderBandHeader(band) {
  if (S.plan && S.blKey !== `${band.center_hz}|${band.sample_rate_hz}`) renderBandList(!$('bl-q').value);
  $('wf-name').style.setProperty('--pc', band.color);
  $('wf-name').textContent = band.profile;
  const lo = band.center_hz - band.sample_rate_hz / 2;
  const hi = band.center_hz + band.sample_rate_hz / 2;
  $('wf-band').textContent = `${mhz(band.center_hz)} MHz · ${mhz(lo, 3)}–${mhz(hi, 3)} · ${band.sample_rate_hz / 1e6} MS/s`;
  const chans = $('wf-chans');
  clear(chans);
  for (const f of band.freqs_hz || []) chans.append(h('span', { class: 'chl', '--pc': band.color }, h('span', { class: 'sw' }), mhzShort(f)));
}

function renderRotation() {
  const l = live();
  const d = l.desired;
  const rot = $('rot');
  const ticks = $('rotx');
  clear(rot);
  clear(ticks);
  $('rot-note').hidden = !(d && d.schedule);
  if (!d) {
    $('rot-text').textContent = 'Nothing scheduled. Start a profile, or set up a rotation.';
    return;
  }
  if (d.profile) {
    $('rot-text').textContent = `Single profile: ${d.profile.profile}, no rotation${watching() ? '; on hold while watching' : ''}.`;
    const seg = h('span', { class: 'rs cur', '--pc': profileColor(d.profile.profile) }, d.profile.profile);
    seg.style.width = '100%';
    rot.append(seg);
    return;
  }
  const r = rotation(d.schedule, d.anchor, now());
  if (!r) return;
  const parts = d.schedule.map((e) => `${e.profile} ${e.minutes} min`);
  // While a watch has the receiver the rotation keeps time but runs nothing.
  const when = watching() ? 'on hold while watching' : `next switch ${utc(r.current.to, false)} (in ${inSeconds(r.current.to, now())})`;
  $('rot-text').textContent = `${parts.join(' → ')}, repeating · ${when}`;
  for (const s of r.steps) {
    const cls = s.index === r.current.index ? 'rs cur' : s.to <= now() ? 'rs past' : 'rs';
    const seg = h('span', { class: cls, '--pc': profileColor(s.profile), title: `${s.profile}: ${utc(s.from, false)}–${utc(s.to, false)}` }, `${s.profile} ${s.minutes}m`);
    seg.style.width = `${s.share * 100}%`;
    rot.append(seg);
    const t = h('span', {}, utc(s.from, false));
    t.style.left = `${((s.from - r.start) / r.total) * 100}%`;
    ticks.append(t);
  }
  const mark = h('span', { class: 'now' });
  mark.style.left = `${r.fraction * 100}%`;
  rot.append(mark);
}

function renderBanner() {
  const l = live();
  const n = now();
  let b = null;
  const cc = (l.captures || {}).current;
  if (S.socket === 'closed' || !l.state || l.state === 'unreachable') b = null;
  else if (cc && cc.state === 'recording' && cc.until) {
    b = {
      cls: 'warn',
      title: 'Recording a capture',
      sub: `${mhz(cc.center_hz, 3)} MHz at ${rateText(cc.sample_rate_hz)} for ${cc.seconds} s; ${(l.hold && l.hold.stopped) || 'decoding'} resumes when it ends, in ${inSeconds(cc.until, n)}.`,
      bar: Math.min(100, Math.max(0, ((n - (cc.until - cc.seconds)) / cc.seconds) * 100)),
    };
  } else if (cc) {
    b = { cls: 'warn', title: 'A capture is waiting for the receiver', sub: l.hold ? `It is lent to ${l.hold.owner}; the capture waits up to ${(l.captures || {}).wait_s} s for it.` : 'It starts in a moment.' };
  } else if (l.state === 'paused' && l.hold) {
    const total = l.hold.until - l.hold.since;
    b = {
      cls: 'warn',
      title: `Paused for ${l.hold.owner}`,
      sub: `The HackRF is lent out; ${l.hold.stopped || 'decoding'} resumes when it finishes, at ${utc(l.hold.until)} at the latest (in ${inSeconds(l.hold.until, n)}).`,
      bar: total > 0 ? Math.min(100, ((n - l.hold.since) / total) * 100) : null,
    };
  } else if (l.state === 'waiting') {
    b = { cls: 'bad', title: `Waiting to retry ${l.target || ''}`, sub: `Next try at ${utc(l.retry_at)} (in ${inSeconds(l.retry_at, n)}).`, err: l.last_error };
  } else if (l.falling_behind) {
    b = { cls: 'warn', title: 'Falling behind', sub: 'Samples arrive slower than the sample rate, so some are being lost. The box may be busy.' };
  } else if (l.log && l.log.low_disk) {
    b = { cls: 'bad', title: 'Disk nearly full', sub: 'Nothing is being written to the log while free space is under the floor.' };
  }
  $('banner').hidden = !b;
  if (!b) return;
  $('banner').className = `banner ${b.cls}`;
  $('banner-title').textContent = b.title;
  $('banner-sub').textContent = b.sub;
  $('banner-err').hidden = !b.err;
  $('banner-err').textContent = b.err || '';
  $('banner-bar').hidden = b.bar == null;
  if (b.bar != null) $('banner-bar').style.width = `${b.bar}%`;
}

function renderOverlay() {
  const l = live();
  if (l.state === 'idle') S.wf.setOverlay('Nothing is decoding', 'Start a profile or a rotation to see its band here.');
  else if (l.state === 'paused' && (l.captures || {}).current) S.wf.setOverlay('Recording a capture', 'The waterfall resumes when it ends; its results appear under Captures.');
  else if (l.state === 'paused') S.wf.setOverlay(`Paused for ${l.hold ? l.hold.owner : 'a device tool'}`, 'The waterfall resumes with the profile.');
  else if (l.state === 'waiting') S.wf.setOverlay('Waiting to retry after an error', l.last_error || '');
  else if (l.state === 'running' && NOW() - S.lastFftAt > 5) S.wf.setOverlay('Waiting for samples…', 'The spectrum appears within a second or two of a start.');
  else if (l.state === 'running') S.wf.setOverlay(null);
}

// --- Health --------------------------------------------------------------------------

function cpuShare(key, stats) {
  if (!stats) return null;
  const prev = S.cpu[key];
  const t = S.status.now;
  S.cpu[key] = { usec: stats.cpu_usec, t };
  if (!prev || t <= prev.t) return prev ? prev.share : null;
  const share = ((stats.cpu_usec - prev.usec) / 1e6 / (t - prev.t)) * 100;
  S.cpu[key].share = share;
  return share;
}

function card(cls, cap, big, unit, meterPct, meterColor, sub, extra) {
  const meter = meterPct == null ? null : h('span', { class: 'meter', '--sc': meterColor || 'var(--text2)' }, h('span'));
  if (meter) meter.firstChild.style.width = `${Math.max(0, Math.min(100, meterPct))}%`;
  return h('div', { class: `hc ${cls || ''}` }, h('span', { class: 'cap' }, cap), h('span', { class: 'big' }, big, unit ? h('small', {}, unit) : null), meter, extra || null, sub ? h('span', { class: 'sub2' }, sub) : null);
}

function renderHealth() {
  const st = S.status;
  const l = live();
  const hl = l.health || {};
  const box = $('health');
  clear(box);
  if (!l.state || l.state === 'unreachable') return;
  const ratio = hl.input_ratio;
  box.append(
    card(
      ratio != null && ratio < 0.9 ? 'warn' : '',
      'Input samples',
      ratio == null ? '—' : `${Math.round(ratio * 100)}`,
      '% of rate',
      ratio == null ? null : ratio * 100,
      ratio != null && ratio < 0.9 ? 'var(--warn)' : 'var(--ok)',
      ratio == null ? 'Measured once a profile has run for 12 s.' : ratio < 0.9 ? 'Samples are being lost.' : 'Arriving at the full rate.',
    ),
  );
  const p = hl.input_power_dbfs;
  // hackrf_transfer reports the average; peaks run several dB above it.
  const hot = p != null && p > -5;
  box.append(
    card(
      hot ? 'warn' : '',
      'Input power',
      p == null ? '—' : p.toFixed(1),
      'dBFS',
      p == null ? null : ((p + 60) / 60) * 100,
      hot ? 'var(--warn)' : 'var(--text2)',
      p == null ? '' : hot ? 'Clipping likely (0 dBFS is full scale): try a lower gain.' : p > -10 ? 'Strong; full scale is 0 dBFS.' : 'Full scale is 0 dBFS.',
    ),
  );
  const levels = hl.channel_levels_dbfs;
  const lv = h('div', { class: 'col' });
  if (levels && Object.keys(levels).length) {
    const color = profileColor(l.profile);
    for (const [f, db] of Object.entries(levels)) {
      const row = h('div', { class: 'lvl', '--pc': color }, h('span', { class: 'ln' }, f), h('span', { class: 'trk' }, h('span', { class: 'fill' })), h('span', { class: 'lv' }, db == null ? '—' : `${db} dB`));
      row.querySelector('.fill').style.width = `${db == null ? 0 : Math.max(0, Math.min(100, ((db + 90) / 90) * 100))}%`;
      lv.append(row);
    }
  } else {
    lv.append(h('span', { class: 'muted' }, 'This profile reports no per-channel levels.'));
  }
  const chCard = card('', 'Channel levels', '', '', null, null, '', lv);
  chCard.querySelector('.big').remove();
  box.append(chCard);
  const max = Math.max(1, ...S.mpm);
  const pts = S.mpm.map((v, i) => `${(i / Math.max(1, S.mpm.length - 1)) * 120},${26 - (v / max) * 24}`).join(' ');
  const svgNS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(svgNS, 'svg');
  svg.setAttribute('class', 'spark');
  svg.setAttribute('viewBox', '0 0 120 26');
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.style.setProperty('--pc', profileColor(l.profile));
  const line = document.createElementNS(svgNS, 'polyline');
  line.setAttribute('points', pts);
  line.setAttribute('fill', 'none');
  line.setAttribute('stroke', 'var(--pc)');
  line.setAttribute('stroke-width', '1.5');
  line.setAttribute('vector-effect', 'non-scaling-stroke');
  svg.append(line);
  box.append(card('', 'Messages / min', `${l.decoded_1m ?? '—'}`, '', null, null, `${l.rows_10m ?? 0} log rows in 10 min`, svg));
  const live_cpu = cpuShare('live', st.service);
  const web_cpu = cpuShare('web', st.web);
  // Its CPU limit, as 100 per core (the container's or the unit's); the meter fills at it.
  const limit = st.service && st.service.cpu_limit_pct ? st.service.cpu_limit_pct : null;
  box.append(
    card(
      live_cpu != null && live_cpu > 0.8 * (limit || 150) ? 'warn' : '',
      'CPU · memory',
      live_cpu == null ? '—' : `${Math.round(live_cpu)}`,
      `% · ${st.service ? mib(st.service.memory_bytes) : '—'} MiB`,
      live_cpu == null ? null : Math.min(100, (live_cpu / (limit || 100)) * 100),
      'var(--text2)',
      `hackrf-live, of one core${limit ? ` (limit ${Math.round(limit)}%)` : ''}. hackrf-web ${web_cpu == null ? '—' : web_cpu.toFixed(1)}% · ${st.web ? mib(st.web.memory_bytes) : '—'} MiB.`,
    ),
  );
  const log = l.log || {};
  const logPct = log.max_db_mb ? (log.file_bytes / 1048576 / log.max_db_mb) * 100 : null;
  box.append(
    card(
      logPct != null && logPct > 90 ? 'warn' : '',
      'Log',
      mib(log.file_bytes),
      `/ ${log.max_db_mb ?? '—'} MiB`,
      logPct,
      logPct != null && logPct > 90 ? 'var(--warn)' : 'var(--text2)',
      `Kept ${log.max_age_days ?? '—'} days · pager text ${log.pager_content ? 'logged' : 'not logged'}`,
    ),
  );
  const sp = l.spectrum || {};
  if (sp.enabled) {
    const spPct = sp.max_mb ? (sp.file_bytes / 1048576 / sp.max_mb) * 100 : null;
    box.append(
      card(
        '',
        'Spectrum history',
        mib(sp.file_bytes),
        `/ ${sp.max_mb ?? '—'} MiB`,
        spPct,
        'var(--text2)',
        sp.tap_cpu_share != null ? `FFTs ${(sp.tap_cpu_share * 100).toFixed(1)}% of a core · seconds kept ${sp.max_age_days} days` : `Seconds kept ${sp.max_age_days} days`,
      ),
    );
  }
}

// --- Controls ------------------------------------------------------------------------

function showCtlError(msg) {
  $('ctl-err').hidden = !msg;
  $('ctl-err').textContent = msg || '';
}

async function startPicked() {
  showCtlError(null);
  const name = $('pick').value;
  const p = S.profiles[name];
  const body = { profile: name };
  try {
    const freqText = $('pick-freqs').hidden ? '' : $('pick-freqs').value;
    const freqs = parseFreqs(freqText);
    if (p.needs_freqs && !freqs.length) throw new Error(`${name} needs its channels: type them in MHz, e.g. 931.75`);
    if (freqs.length) body.frequencies_hz = freqs;
    if ($('g-lna').value !== '') body.lna_gain_db = Number($('g-lna').value);
    if ($('g-vga').value !== '') body.vga_gain_db = Number($('g-vga').value);
    if ($('g-amp').checked) body.amp_enable = true;
    $('btn-start').disabled = true;
    const st = await post('/api/live/start', body);
    renderStatus({ ...S.status, live: st, now: st.now || now() });
  } catch (e) {
    showCtlError(e.message);
  } finally {
    $('btn-start').disabled = false;
  }
}

async function stopLive() {
  showCtlError(null);
  try {
    const st = await post('/api/live/stop', {});
    renderStatus({ ...S.status, live: st, now: st.now || now() });
  } catch (e) {
    showCtlError(e.message);
  }
}

// --- Rotation editor -------------------------------------------------------------------

function openEditor() {
  const d = live().desired;
  if (d && d.schedule) {
    S.draft = d.schedule.map((e) => ({ profile: e.profile, minutes: e.minutes, freqs: (e.freqs_hz || []).map((f) => f / 1e6).join(', ') }));
  } else {
    const cur = d && d.profile ? d.profile.profile : firstInstalled();
    S.draft = [{ profile: cur, minutes: 10, freqs: '' }];
  }
  $('rot-err').hidden = true;
  renderDraft();
  $('rot-backdrop').hidden = false;
  $('rot-drawer').hidden = false;
}

function closeEditor() {
  $('rot-backdrop').hidden = true;
  $('rot-drawer').hidden = true;
}

function renderDraft() {
  const box = $('rot-steps');
  clear(box);
  S.draft.forEach((s, i) => {
    const p = S.profiles[s.profile];
    const sel = h('select', { class: 'inp', id: `step-p-${i}`, 'aria-label': `Step ${i + 1} profile` });
    for (const q of S.registry.profiles) sel.append(h('option', { value: q.name, disabled: !q.installed, selected: q.name === s.profile }, q.name));
    sel.addEventListener('change', () => {
      s.profile = sel.value;
      renderDraft();
    });
    const mins = h('input', { class: 'inp mono', type: 'number', min: 1, max: 1440, value: s.minutes, 'aria-label': `Step ${i + 1} minutes` });
    mins.addEventListener('change', () => {
      s.minutes = Math.max(1, Math.min(1440, Number(mins.value) || 1));
      renderDraftPreview();
    });
    const move = (k) => {
      const j = i + k;
      [S.draft[i], S.draft[j]] = [S.draft[j], S.draft[i]];
      renderDraft();
    };
    const row = h(
      'div',
      { class: 'steprow', '--pc': profileColor(s.profile) },
      h('span', { class: 'mono muted' }, `${i + 1}`),
      sel,
      h('span', { class: 'row' }, mins, h('span', { class: 'muted' }, 'min')),
      h(
        'span',
        { class: 'row' },
        h('button', { type: 'button', class: 'btn sm icon ghost', 'aria-label': 'Move step up', disabled: i === 0, onClick: () => move(-1) }, '↑'),
        h('button', { type: 'button', class: 'btn sm icon ghost', 'aria-label': 'Move step down', disabled: i === S.draft.length - 1, onClick: () => move(1) }, '↓'),
        h('button', { type: 'button', class: 'btn sm icon ghost', 'aria-label': 'Remove step', disabled: S.draft.length === 1, onClick: () => { S.draft.splice(i, 1); renderDraft(); } }, '×'),
      ),
    );
    if (p && (p.needs_freqs || (!p.fixed_freqs && !p.default_freqs_hz.length))) {
      const fr = h('input', { class: 'inp mono freqs', value: s.freqs, placeholder: p.needs_freqs ? 'channels, MHz (required)' : 'channels, MHz', 'aria-label': `Step ${i + 1} channels` });
      fr.addEventListener('change', () => { s.freqs = fr.value; });
      row.append(h('span'), fr);
    }
    box.append(row);
  });
  renderDraftPreview();
}

function renderDraftPreview() {
  const total = S.draft.reduce((a, s) => a + s.minutes, 0);
  $('rot-total').textContent = `Preview · one cycle = ${total} min`;
  const bar = $('rot-preview');
  const share = $('rot-share');
  clear(bar);
  clear(share);
  const by = {};
  for (const s of S.draft) {
    const seg = h('span', { class: 'rs', '--pc': profileColor(s.profile) }, `${s.profile} ${s.minutes}m`);
    seg.style.width = `${(s.minutes / total) * 100}%`;
    bar.append(seg);
    by[s.profile] = (by[s.profile] || 0) + s.minutes;
  }
  for (const [name, m] of Object.entries(by)) share.append(h('span', { '--pc': profileColor(name) }, h('span', { class: 'sw' }), `${name} ${Math.round((m / total) * 100)}%`));
}

async function saveRotation() {
  $('rot-err').hidden = true;
  try {
    const steps = S.draft.map((s) => {
      const freqs = parseFreqs(s.freqs);
      if (S.profiles[s.profile].needs_freqs && !freqs.length) throw new Error(`${s.profile} needs its channels (MHz).`);
      return { profile: s.profile, minutes: s.minutes, frequencies_hz: freqs.length ? freqs : undefined };
    });
    const body = steps.length >= 2 ? { schedule: steps } : { profile: steps[0].profile, frequencies_hz: steps[0].frequencies_hz };
    const st = await post('/api/live/start', body);
    closeEditor();
    renderStatus({ ...S.status, live: st, now: st.now || now() });
  } catch (e) {
    $('rot-err').hidden = false;
    $('rot-err').textContent = e.message;
  }
}

// --- The decoded feed ------------------------------------------------------------------

function buildChips() {
  const box = $('feed-chips');
  clear(box);
  for (const p of S.registry.profiles) {
    const n = S.items.filter((it) => it.kindOf === 'row' && it.profile === p.name).length;
    const chip = h('button', { type: 'button', class: 'tchip', '--pc': profileColor(p.name), 'aria-pressed': String(!S.hidden.has(p.name)) }, h('span', { class: 'sw' }), p.name, h('span', { class: 'n' }, `${n}`));
    chip.addEventListener('click', () => {
      if (S.hidden.has(p.name)) S.hidden.delete(p.name);
      else S.hidden.add(p.name);
      buildChips();
      renderFeed();
    });
    box.append(chip);
  }
  const ev = h('button', { type: 'button', class: 'tchip', 'aria-pressed': String(S.showEvents) }, 'Events');
  ev.addEventListener('click', () => {
    S.showEvents = !S.showEvents;
    buildChips();
    renderFeed();
  });
  box.append(ev);
}

function renderHoldButton() {
  $('btn-hold').setAttribute('aria-pressed', String(S.hold));
  $('btn-hold').textContent = S.hold ? `Resume${S.held.length ? ` (${S.held.length} new)` : ''}` : 'Pause feed';
}

function feedIn(item) {
  if (S.hold) {
    S.held.push(item);
    if (S.held.length > FEED_MAX) S.held.shift();
    renderHoldButton();
    return;
  }
  addItem(item, true);
}

function addItem(item, render) {
  S.items.unshift(item);
  if (S.items.length > FEED_MAX) S.items.length = FEED_MAX;
  if (render) {
    const el = itemEl(item, true);
    if (el) {
      const feed = $('feed');
      feed.prepend(el);
      while (feed.childElementCount > FEED_MAX) feed.lastChild.remove();
    }
    if (item.kindOf === 'row') buildChips();
  }
}

function renderFeed() {
  const feed = $('feed');
  clear(feed);
  for (const it of S.items) {
    const el = itemEl(it, false);
    if (el) feed.append(el);
  }
  if (!feed.childElementCount) feed.append(h('div', { class: 'empty' }, 'Nothing decoded yet in what is shown.'));
}

function fmtValue(v) {
  if (v == null) return '—';
  if (Array.isArray(v)) return v.join(', ');
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

function itemEl(it, fresh) {
  if (it.kindOf === 'event') {
    if (!S.showEvents) return null;
    return h(
      'div',
      { class: `fiw ev${fresh ? ' new' : ''}` },
      h('div', { class: 'fi' }, h('span', { class: 'ft' }, utc(it.ts)), h('span', { class: 'l1' }, `● ${it.event}${it.profile ? ` [${it.profile}]` : ''}${it.detail ? `: ${it.detail}` : ''}`)),
    );
  }
  if (S.hidden.has(it.profile)) return null;
  const p = S.profiles[it.profile];
  const fields = it.fields || {};
  const view = p && p.views.find((v) => v.name === 'table');
  const keys = view && view.fields.length ? view.fields : Object.keys(fields);
  const summary = [];
  for (const k of keys) {
    if (fields[k] == null || fields[k] === '' || k === 'message') continue;
    summary.push(h('span', {}, h('span', { class: 'muted' }, `${k} `), fmtValue(fields[k])));
    if (summary.length >= 4) break;
  }
  const alerted = it.subject && S.alerted.has(`${it.profile}|${it.subject}`);
  const wrap = h('div', { class: `fiw${fresh ? ' new' : ''}${alerted ? ' alerted' : ''}`, '--pc': profileColor(it.profile) });
  const btn = h(
    'button',
    { type: 'button', class: 'fi', 'aria-expanded': 'false' },
    h('span', { class: 'ft' }, utc(it.ts)),
    h(
      'span',
      { class: 'l1' },
      h('span', { class: 'pbadge' }, it.profile),
      it.subject ? h('span', { class: 'subj' }, it.subject) : null,
      alerted ? h('span', { class: 'tag bad altag' }, 'alert') : null,
      it.label ? h('span', { class: 'lbl' }, it.label) : null,
      h('span', { class: 'kind muted' }, it.kind),
    ),
    h('span', { class: 'l2' }, summary, h('span', { class: 'mono muted' }, `${it.freq_hz ? mhz(it.freq_hz) : '—'} MHz · ${it.level_db == null ? '—' : Number(it.level_db).toFixed(1)} dB`)),
  );
  btn.addEventListener('click', () => {
    const open = btn.getAttribute('aria-expanded') === 'true';
    btn.setAttribute('aria-expanded', String(!open));
    wrap.classList.toggle('open', !open);
    const det = wrap.querySelector('.fdet');
    if (det) {
      det.remove();
      return;
    }
    const kv = h('dl', { class: 'kv' });
    for (const [k, v] of Object.entries(fields)) kv.append(h('dt', {}, k), h('dd', { class: 'mono' }, fmtValue(v)));
    const q = new URLSearchParams({ profiles: it.profile, around: String(Math.round(it.ts)) });
    if (it.subject) q.set('subject', it.subject);
    wrap.append(
      h(
        'div',
        { class: 'fdet' },
        kv,
        it.raw ? h('pre', { class: 'raw' }, it.raw) : null,
        h('div', { class: 'acts' }, h('a', { class: 'btn sm', href: `/log?${q}` }, it.subject ? 'Open in log' : 'Open the log here')),
      ),
    );
  });
  wrap.append(btn);
  return wrap;
}

async function seedFeed() {
  const [log, ev] = await Promise.all([api('/api/log?since=30m&limit=60'), api('/api/events?since=2h&limit=15')]);
  const items = [...log.rows.map((r) => ({ ...r, kindOf: 'row' })), ...ev.events.map((e) => ({ ...e, kindOf: 'event' }))];
  items.sort((a, b) => a.ts - b.ts);
  for (const it of items) addItem(it, false);
  buildChips();
  renderFeed();
}

// The last two minutes of the current band, from the spectrum history, so
// the waterfall isn't empty when the page opens.
async function prefillWaterfall() {
  const { segments } = await api('/api/spectrum/segments?since=3m');
  const seg = segments[segments.length - 1];
  if (!seg) return;
  const data = await api(`/api/spectrum?segment=${seg.id}&since=2m&buckets=120&tier=second`);
  if (!data.ts.length) return;
  const det = live().tuning_detail;
  const same = det && det.center_hz === seg.center_hz && det.sample_rate_hz === seg.sample_rate_hz;
  const band = { profile: seg.profile, center_hz: seg.center_hz, sample_rate_hz: seg.sample_rate_hz, freqs_hz: same ? det.freqs_hz : [], color: profileColor(seg.profile) };
  S.wf.setBand(band);
  renderBandHeader(band);
  const bins = seg.bins;
  const mean = Uint8Array.from(atob(data.mean), (c) => c.charCodeAt(0));
  const peak = Uint8Array.from(atob(data.peak), (c) => c.charCodeAt(0));
  const toDb = (u8) => Float32Array.from(u8, (b) => seg.db_min + b * seg.db_step);
  for (let i = 0; i < data.ts.length; i++) {
    S.wf.pushRow(toDb(mean.subarray(i * bins, (i + 1) * bins)), toDb(peak.subarray(i * bins, (i + 1) * bins)), data.ts[i], Math.max(1, Math.round(1 / S.liveRowS)));
  }
}

// --- Every second ------------------------------------------------------------------------

function tick() {
  paintClock(now());
  if (!S.status) return;
  renderRotation();
  renderBanner();
  renderOverlay();
  const l = live();
  if (S.socket === 'closed') {
    $('unreach-retry').textContent = `Reconnecting… (last contact ${ago(S.statusAt, NOW())})`;
  } else if (l.state === 'unreachable') {
    $('unreach-retry').textContent = 'Checking again every few seconds.';
  }
}


// --- Watching a custom range --------------------------------------------------------------

function buildWatch() {
  const w = S.registry.watch;
  if (!w) {
    $('watch-box').hidden = true;
    return;
  }
  const sel = $('w-min');
  for (const m of [5, 15, 30, 60].filter((x) => x <= w.max_minutes)) {
    sel.append(h('option', { value: String(m), selected: m === w.default_minutes ? true : null }, `for ${m} min`));
  }
  const go = () => watchRange();
  $('btn-watch').addEventListener('click', go);
  for (const id of ['w-from', 'w-to']) $(id).addEventListener('keydown', (e) => e.key === 'Enter' && go());
  $('watch-down').addEventListener('click', () => scrollWatch(-1));
  $('watch-up').addEventListener('click', () => scrollWatch(1));
  $('watch-more').addEventListener('click', () => {
    const cur = watching();
    if (!cur) return;
    const minutes = Math.min(w.max_minutes, Math.ceil(cur.left_s / 60) + 15);
    sendWatch({ center_hz: cur.center_hz, sample_rate_hz: cur.sample_rate_hz, minutes });
  });
  $('watch-stop').addEventListener('click', async () => {
    showCtlError(null);
    try {
      const st = await post('/api/live/watch/stop', {});
      renderStatus({ ...S.status, live: st, now: st.now || now() });
    } catch (e) {
      showCtlError(e.message);
    }
  });
  // Opened from the Bands screen: the range is filled in; taking the
  // receiver is still a click on Watch.
  const u = new URLSearchParams(location.search);
  if (u.get('watch_from')) {
    $('w-from').value = u.get('watch_from');
    $('w-to').value = u.get('watch_to') || '';
    $('watch-hint').textContent = 'Press Watch to tune there; decoding pauses until the time runs out or you stop.';
    $('watch-box').scrollIntoView({ block: 'center' });
    $('btn-watch').focus();
  }
  // Drag the waterfall sideways to scroll while watching.
  const plot = S.wf.wfPlot;
  let x0 = null;
  plot.addEventListener('pointerdown', (e) => {
    x0 = watching() && !S.selecting ? e.clientX : null;
  });
  plot.addEventListener('pointerup', (e) => {
    const cur = watching();
    if (x0 === null || !cur) return;
    const dx = e.clientX - x0;
    x0 = null;
    if (Math.abs(dx) < 24) return;
    const shift = (-dx / Math.max(1, plot.clientWidth)) * cur.sample_rate_hz;
    sendWatch({ center_hz: scrolled(cur.center_hz + shift, cur.sample_rate_hz, 0, w.min_hz, w.max_hz), sample_rate_hz: cur.sample_rate_hz });
  });
}

function watchRange() {
  const w = S.registry.watch;
  const from = parseMhz($('w-from').value);
  const to = $('w-to').value.trim() ? parseMhz($('w-to').value) : from;
  if (!isFinite(from) || !isFinite(to)) {
    showCtlError('Type the range in MHz, e.g. 88 to 98 (or one frequency in "from").');
    return;
  }
  const t = watchTuning(from, to, w.rates_hz, w.min_hz, w.max_hz);
  const body = { ...t, minutes: Number($('w-min').value) };
  delete body.clipped;
  if ($('g-lna').value !== '') body.lna_gain_db = Number($('g-lna').value);
  if ($('g-vga').value !== '') body.vga_gain_db = Number($('g-vga').value);
  if ($('g-amp').checked) body.amp_enable = true;
  sendWatch(body, t.clipped ? `At most ${w.rates_hz[w.rates_hz.length - 1] / 1e6} MHz fits at once: showing the start of the range; scroll for the rest.` : null);
}

// --- The band list --------------------------------------------------------------------
// Every band of the plan the HackRF can tune, to scroll through; clicking one
// watches it, through the same request as "Watch a range".

async function buildBandList() {
  const w = S.registry.watch;
  const plan = await loadPlan();
  if (!w || !plan) return;
  S.plan = plan;
  $('bl-box').hidden = false;
  $('bl-q').addEventListener('input', () => renderBandList(true));
  renderBandList(true);
}

function renderBandList(scrollToView = false) {
  const w = S.registry.watch;
  const list = bandList(S.plan, $('bl-q').value, w.min_hz, w.max_hz);
  const box = clear($('bl'));
  const band = S.wf && S.wf.band;
  const inView = band ? new Set(bandsInView(list, band.center_hz - band.sample_rate_hz / 2, band.center_hz + band.sample_rate_hz / 2)) : new Set();
  S.blKey = band ? `${band.center_hz}|${band.sample_rate_hz}` : '';
  let first = null;
  list.forEach((b, i) => {
    const wide = b.hi - b.lo > w.rates_hz[w.rates_hz.length - 1];
    const item = h(
      'button',
      {
        type: 'button',
        class: `bli${inView.has(i) ? ' cur' : ''}`,
        role: 'listitem',
        '--bc': b.color,
        title: `${b.label}${b.note ? ` · ${b.note}` : ''}${wide ? ' · wider than one watch: it opens at the start; scroll for the rest' : ''}`,
      },
      h('span', { class: 'sw' }),
      h('span', { class: 'bln' }, b.name),
      h('span', { class: 'mono t2 blr' }, `${mhz(b.lo, 3)}–${mhz(b.hi, 3)}`),
    );
    item.addEventListener('click', () => {
      $('w-from').value = mhz(b.lo, 3);
      $('w-to').value = mhz(b.hi, 3);
      watchRange();
    });
    if (inView.has(i) && !first) first = item;
    box.append(item);
  });
  if (!list.length) box.append(h('span', { class: 't2 sm' }, 'No band matches.'));
  if (scrollToView && first) box.scrollTop = first.offsetTop - box.offsetTop - 4;
}

function scrollWatch(dir) {
  const w = S.registry.watch;
  const cur = watching();
  if (!cur) return;
  sendWatch({ center_hz: scrolled(cur.center_hz, cur.sample_rate_hz, dir, w.min_hz, w.max_hz), sample_rate_hz: cur.sample_rate_hz });
}

async function sendWatch(body, note = null) {
  showCtlError(null);
  try {
    // The reply is the live service's status, as for start and stop.
    const st = await post('/api/live/watch', body);
    renderStatus({ ...S.status, live: st, now: st.now || now() });
    if (note) showCtlError(note);
  } catch (e) {
    showCtlError(e.message);
  }
}

function renderWatch() {
  const cur = watching();
  $('watch-form').hidden = !!cur;
  $('watch-on').hidden = !cur;
  if (!cur) return;
  const lo = cur.center_hz - cur.sample_rate_hz / 2;
  const hi = cur.center_hz + cur.sample_rate_hz / 2;
  $('watch-range').textContent = `${mhz(lo, 3)}–${mhz(hi, 3)} MHz`;
  const left = cur.until - ((S.status && S.status.now) || NOW());
  $('watch-left').textContent = `${leftText(left)}, then decoding resumes`;
  const w = S.registry.watch;
  $('watch-down').disabled = lo <= w.min_hz;
  $('watch-up').disabled = hi >= w.max_hz;
}

// --- Captures ------------------------------------------------------------------------------
// With Select on, a tap on the waterfall picks a signal and a drag a range.
// The form under the waterfall asks the live service to plan the capture
// (rate, centre, size, what it would stop) and records it only when Capture
// is pressed. History and Occupancy open this form with a range filled in.
// The browser gets the results and a picture, never the samples.

function buildCapture() {
  const lna = $('cap-lna');
  const vga = $('cap-vga');
  lna.append(h('option', { value: '' }, 'default'));
  vga.append(h('option', { value: '' }, 'default'));
  for (let g = 0; g <= 40; g += 8) lna.append(h('option', { value: g }, `${g} dB`));
  for (let g = 0; g <= 62; g += 2) vga.append(h('option', { value: g }, `${g} dB`));
  $('btn-select').addEventListener('click', () => setSelecting(!S.selecting));
  $('cap-close').addEventListener('click', closeCapture);
  $('cap-cancel').addEventListener('click', closeCapture);
  $('cap-go').addEventListener('click', startCapture);
  $('caps-refresh').addEventListener('click', loadCaptures);
  for (const id of ['cap-sec', 'cap-lna', 'cap-vga', 'cap-amp']) {
    $(id).addEventListener('change', () => {
      readCaptureForm();
      planCaptureSoon();
    });
  }
  for (const plot of [S.wf.wfPlot, S.wf.specPlot]) wireSelect(plot);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && S.selecting && !S.cap) setSelecting(false);
  });
  const q = captureQuery(location.search);
  if (q) openCapture({ ...q, fromAddress: true });
}

function setSelecting(on) {
  S.selecting = on;
  $('btn-select').setAttribute('aria-pressed', String(on));
  $('wf').classList.toggle('selecting', on);
  $('sel-hint').hidden = !on || !!S.cap;
}

function wireSelect(plot) {
  let start = null;
  const fxOf = (e) => {
    const r = plot.getBoundingClientRect();
    return (e.clientX - r.left) / r.width;
  };
  plot.addEventListener('pointerdown', (e) => {
    const band = S.wf.band;
    if (!S.selecting || !band || !band.sample_rate_hz) return;
    start = { fx: fxOf(e), band: { center_hz: band.center_hz, sample_rate_hz: band.sample_rate_hz } };
    if (plot.setPointerCapture) plot.setPointerCapture(e.pointerId);
    S.wf.setSelection(pickRange(start.band, start.fx, start.fx));
    e.preventDefault();
  });
  plot.addEventListener('pointermove', (e) => {
    if (start) S.wf.setSelection(pickRange(start.band, start.fx, fxOf(e)));
  });
  plot.addEventListener('pointerup', (e) => {
    if (!start) return;
    const sel = pickRange(start.band, start.fx, fxOf(e));
    start = null;
    openCapture({ ...sel, origin: 'live', withGains: true });
  });
  plot.addEventListener('pointercancel', () => {
    start = null;
    S.wf.setSelection(S.cap);
  });
}

const ORIGIN_NOTE = {
  history: 'From History: this records the range now, not at the time shown there (History keeps only power, no samples).',
  occupancy: 'From Occupancy: this records the range now.',
};

function openCapture(sel) {
  const td = live().tuning_detail;
  const prev = S.cap;
  const gains = sel.withGains && td;
  S.cap = {
    lo_hz: sel.lo_hz,
    hi_hz: sel.hi_hz,
    origin: sel.origin || 'live',
    // The suggestions stay ticked until a box is changed.
    chosen: null,
    seconds: prev ? prev.seconds : 30,
    lna: gains ? td.lna_gain_db : prev ? prev.lna : '',
    vga: gains ? td.vga_gain_db : prev ? prev.vga : '',
    amp: gains ? !!td.amp : false,
    plan: null,
    seq: 0,
  };
  $('cap-sec').value = S.cap.seconds;
  $('cap-lna').value = S.cap.lna ?? '';
  $('cap-vga').value = S.cap.vga ?? '';
  $('cap-amp').checked = S.cap.amp;
  $('cap-range').textContent = rangeLabel(S.cap.lo_hz, S.cap.hi_hz);
  $('cap-origin').hidden = !ORIGIN_NOTE[S.cap.origin];
  $('cap-origin').textContent = ORIGIN_NOTE[S.cap.origin] || '';
  $('cap-form').hidden = false;
  $('sel-hint').hidden = true;
  S.wf.setSelection(S.cap);
  if (sel.fromAddress) $('cap-form').scrollIntoView({ block: 'center' });
  planCapture();
}

function closeCapture() {
  S.cap = null;
  $('cap-form').hidden = true;
  S.wf.setSelection(null);
  setSelecting(false);
  if (captureQuery(location.search)) history.replaceState(null, '', location.pathname);
}

function readCaptureForm() {
  const f = S.cap;
  if (!f) return;
  f.seconds = Number($('cap-sec').value) || 30;
  f.lna = $('cap-lna').value;
  f.vga = $('cap-vga').value;
  f.amp = $('cap-amp').checked;
}

let planTimer = null;
function planCaptureSoon() {
  clearTimeout(planTimer);
  planTimer = setTimeout(planCapture, 250);
}

async function planCapture() {
  const f = S.cap;
  if (!f) return;
  const seq = ++f.seq;
  $('cap-go').disabled = true;
  try {
    const r = await post('/api/captures/plan', captureBody(f));
    if (S.cap !== f || seq !== f.seq) return;
    f.plan = r;
    renderCaptureForm();
  } catch (e) {
    if (S.cap !== f) return;
    f.plan = null;
    renderCaptureForm(e.message);
  }
}

function renderCaptureForm(failure = null) {
  const f = S.cap;
  if (!f) return;
  const r = f.plan || {};
  const on = new Set(ticked(f.chosen, r.suggested));
  const suggested = new Set(r.suggested || []);
  const decs = (r.decoders || []).slice().sort((a, b) => Number(suggested.has(b.name)) - Number(suggested.has(a.name)));
  const box = clear($('cap-decs'));
  for (const d of decs) {
    const input = h('input', { type: 'checkbox', checked: on.has(d.name), disabled: !d.installed });
    input.addEventListener('change', () => {
      const now = new Set(ticked(f.chosen, r.suggested));
      if (input.checked) now.add(d.name);
      else now.delete(d.name);
      f.chosen = decs.map((x) => x.name).filter((n) => now.has(n));
      planCaptureSoon();
    });
    box.append(
      h('label', { class: 'check', title: d.installed ? d.summary : `${d.summary} (not installed)` }, input, h('b', {}, d.name), suggested.has(d.name) ? h('span', { class: 'tag ok' }, 'suggested') : null),
    );
  }
  if (!decs.length && !failure) box.append(h('span', { class: 'muted' }, 'Loading…'));
  const lim = r.limits || {};
  if (lim.max_seconds) $('cap-sec').max = lim.max_seconds;
  const kv = clear($('cap-plan'));
  const p = r.plan;
  if (p) {
    const add = (k, v) => kv.append(h('dt', {}, k), h('dd', {}, v));
    add('Tuned', `${mhz(p.center_hz)} MHz at ${rateText(p.sample_rate_hz)}`);
    add('Holds', `${mhz(p.covers_hz[0], 3)}–${mhz(p.covers_hz[1], 3)} MHz`);
    add('Length', `${p.seconds} s, ${sizeText(p.bytes)}`);
    add('Gains', `LNA ${p.lna_gain_db} dB, VGA ${p.vga_gain_db} dB${p.amp ? ', RF amp on' : ''}`);
    add(
      'Decoders',
      p.decoders.length
        ? p.decoders.map((d) => (d.freqs_hz ? `${d.name} (${d.freqs_hz.map((x) => mhz(x)).join(', ')} MHz)` : d.name)).join(' · ')
        : 'none: the recording and its picture only',
    );
  }
  const notes = clear($('cap-notes'));
  for (const n of (p && p.notes) || []) notes.append(h('li', {}, n));
  const eff = clear($('cap-eff'));
  for (const e of r.effects || []) eff.append(h('li', {}, e));
  const err = failure || r.error;
  $('cap-err').hidden = !err;
  $('cap-err').textContent = err || '';
  $('cap-go').disabled = !p || !!r.error;
}

async function startCapture() {
  const f = S.cap;
  if (!f || !f.plan || !f.plan.plan) return;
  $('cap-go').disabled = true;
  try {
    const r = await post('/api/captures', { ...captureBody(f), decoders: ticked(f.chosen, f.plan.suggested) });
    closeCapture();
    S.caps = [r.capture, ...S.caps.filter((c) => c.id !== r.capture.id)];
    renderCaptures();
    $('caps').scrollIntoView({ block: 'nearest' });
  } catch (e) {
    $('cap-err').hidden = false;
    $('cap-err').textContent = e.message;
    $('cap-go').disabled = false;
  }
}

// The list follows the live service's capture counters: a change (a
// capture started, recorded, decoded or deleted) reloads it.
function renderCaptureStatus() {
  const cs = live().captures;
  if (!cs) return;
  if (!cs.enabled) {
    $('btn-select').hidden = true;
    $('caps').hidden = true;
    return;
  }
  $('btn-select').hidden = false;
  $('caps').hidden = false;
  const cur = cs.current;
  const key = [cs.count, cs.recorded, cs.failed, cs.decoded, cur ? `${cur.id}:${cur.state}` : '', cs.decoding || '', cs.queued].join('|');
  if (key !== S.capKey) {
    S.capKey = key;
    loadCaptures();
  } else if (cur) {
    renderCaptures();
  }
}

async function loadCaptures() {
  try {
    const r = await api('/api/captures');
    S.caps = r.captures || [];
    S.capStatus = r.status || null;
    renderCaptures();
  } catch (e) {
    clear($('cap-list')).append(h('p', { class: 'capi muted' }, `Could not load the captures: ${e.message}`));
  }
}

function renderCaptures() {
  const cs = S.capStatus || live().captures || {};
  // In the station's own units: its limits are set in MB of 2^20 bytes.
  $('caps-sum').textContent = cs.enabled
    ? `${S.caps.length} kept · ${Math.round(cs.used_mb || 0)} MB used of ${+((cs.keep_mb || 0) / 1024).toFixed(1)} GB, for ${cs.keep_days} days`
    : '';
  const box = clear($('cap-list'));
  if (!S.caps.length) {
    box.append(h('p', { class: 'capi muted' }, 'No captures yet. Turn on Select above the waterfall, tap a signal or drag across a range, then press Capture.'));
    return;
  }
  const cur = (live().captures || {}).current;
  for (const rec of S.caps) box.append(captureEl(rec, cur));
}

function captureEl(rec, cur) {
  const p = rec.plan;
  const del = h('button', { type: 'button', class: 'btn sm danger', disabled: rec.state === 'waiting' || rec.state === 'recording' || rec.state === 'decoding' }, 'Delete');
  del.addEventListener('click', () => deleteCapture(rec));
  const el = h(
    'div',
    { class: 'capi', role: 'listitem' },
    h(
      'div',
      { class: 'row wr' },
      h('span', { class: `tag ${stateTone(rec)}` }, stateText(rec, cur)),
      h('b', { class: 'mono' }, rangeLabel(p.lo_hz, p.hi_hz)),
      h('span', { class: 't2' }, `${fmtUtc(rec.created)} UTC · ${p.seconds} s at ${rateText(p.sample_rate_hz)} on ${mhz(p.center_hz)} MHz · ${sizeText(rec.state === 'waiting' || rec.state === 'recording' ? p.bytes : rec.bytes || p.bytes)}`),
      rec.origin && rec.origin !== 'live' ? h('span', { class: 'tag' }, `from ${rec.origin}`) : null,
      h('span', { class: 'sp' }),
      del,
    ),
  );
  if (rec.error) el.append(h('span', { class: 'err' }, rec.error));
  const bits = [];
  if (rec.paused) bits.push(rec.held_s != null ? `${rec.paused} paused ${rec.held_s} s` : `${rec.paused} paused`);
  if (rec.note) bits.push(rec.note);
  if (bits.length) el.append(h('span', { class: 't2' }, bits.join(' · ')));
  for (const d of rec.decodes || []) el.append(decodeEl(rec, d));
  if (rec.picture) el.append(pictureEl(rec));
  else if (rec.picture_error) el.append(h('span', { class: 't2' }, `No picture: ${rec.picture_error}`));
  // The live service lists the decoders this capture's rate and centre suit.
  const more = rec.more || [];
  if (more.length) {
    const sel = h('select', { class: 'inp', 'aria-label': 'Another decoder' }, more.map((n) => h('option', { value: n }, n)));
    const run = h('button', { type: 'button', class: 'btn sm' }, 'Run');
    const msg = h('span', { class: 'err', hidden: true });
    run.addEventListener('click', async () => {
      run.disabled = true;
      try {
        await post(`/api/captures/${encodeURIComponent(rec.id)}/decode`, { decoders: [sel.value] });
        loadCaptures();
      } catch (e) {
        msg.hidden = false;
        msg.textContent = e.message;
        run.disabled = false;
      }
    });
    el.append(h('div', { class: 'row wr' }, h('span', { class: 't2' }, 'Run another decoder on it'), sel, run, msg));
  }
  return el;
}

function decodeEl(rec, d) {
  const key = `${rec.id}|${d.decoder}`;
  const words = { queued: 'queued', running: 'running…' };
  const head = d.headline || d.error || words[d.state] || d.state;
  const det = h('details', { open: S.capOpen.has(key) }, h('summary', {}, h('b', {}, d.decoder), ` ${head}`));
  det.addEventListener('toggle', () => (det.open ? S.capOpen.add(key) : S.capOpen.delete(key)));
  for (const n of d.notes || []) det.append(h('div', { class: 't2' }, n));
  const lines = (title, list, more) => {
    if (!list || !list.length) return;
    det.append(h('div', { class: 'cap' }, title), h('ol', { class: 'mono' }, list.map((x) => h('li', {}, x))));
    if (more) det.append(h('div', { class: 't2' }, `… ${more} more not kept`));
  };
  lines('Table', d.table, d.table_more);
  lines('Messages', d.messages, d.messages_more);
  if (d.seconds != null) det.append(h('div', { class: 'muted' }, `Ran ${d.seconds} s.`));
  return det;
}

function pictureEl(rec) {
  const det = h('details', { open: S.capPics.has(rec.id) }, h('summary', {}, 'Spectrogram'));
  const show = () => {
    if (det.querySelector('img')) return;
    det.append(h('img', { src: `/api/captures/${encodeURIComponent(rec.id)}/picture`, alt: `Spectrogram of the capture of ${rangeLabel(rec.plan.lo_hz, rec.plan.hi_hz)}`, loading: 'lazy' }));
  };
  if (det.open) show();
  det.addEventListener('toggle', () => {
    if (det.open) {
      S.capPics.add(rec.id);
      show();
    } else S.capPics.delete(rec.id);
  });
  return det;
}

async function deleteCapture(rec) {
  if (!confirm(`Delete the capture of ${rangeLabel(rec.plan.lo_hz, rec.plan.hi_hz)} (${sizeText(rec.bytes)}) and its results?`)) return;
  try {
    await post(`/api/captures/${encodeURIComponent(rec.id)}/delete`, {});
    S.caps = S.caps.filter((c) => c.id !== rec.id);
    renderCaptures();
    loadCaptures();
  } catch (e) {
    showCtlError(e.message);
  }
}

init().catch((e) => {
  showCtlError(`Could not load: ${e.message}`);
});
