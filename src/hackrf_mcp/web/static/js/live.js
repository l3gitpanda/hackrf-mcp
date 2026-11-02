// The Live screen: what the receiver is doing now, its waterfall, what it
// decodes as it decodes it, and the controls to change it. Everything about
// profiles (names, channels, which fields to show) comes from /api/registry;
// nothing here names a protocol.

import { api, post, LiveSocket } from './api.js';
import { Waterfall } from './waterfall.js';
import { ago, assignColors, clear, h, inSeconds, mhz, mhzShort, mib, parseFreqs, rotation, utc } from './util.js';

const $ = (id) => document.getElementById(id);
const FEED_MAX = 300;
const NOW = () => Date.now() / 1000;

const S = {
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
  draft: null,
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

async function init() {
  S.registry = await api('/api/registry');
  for (const p of S.registry.profiles) S.profiles[p.name] = p;
  S.colors = assignColors(S.registry.profiles);
  S.wf = new Waterfall($('wf'), (lo, hi) => {
    $('wf-lo').textContent = `${Math.round(lo)}`;
    $('wf-hi').textContent = `${Math.round(hi)} dBFS`;
  });
  buildControls();
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
      break;
    default:
      break;
  }
}

function onFft(m) {
  S.lastFftAt = NOW();
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
  renderControl();
  renderRotation();
  renderBanner();
  renderOverlay();
  renderHealth();
  $('feed-rate').textContent = `${l.decoded_1m ?? '—'} msg/min`;
}

function stateOf(l) {
  if (S.socket === 'closed') return ['st-stopped', 'Disconnected'];
  switch (l.state) {
    case 'running':
      return l.falling_behind ? ['st-behind', 'Falling behind'] : ['st-running', 'Running'];
    case 'paused':
      return ['st-paused', 'Paused'];
    case 'waiting':
      return ['st-error', 'Waiting to retry'];
    case 'starting':
      return ['st-idle', 'Starting'];
    case 'idle':
      return ['st-idle', 'Idle'];
    default:
      return ['st-stopped', 'Service not running'];
  }
}

function renderAppBar() {
  const l = live();
  const [cls, text] = stateOf(l);
  $('ab-state').className = `chip st ${cls}`;
  $('ab-state-text').textContent = text;
  const det = l.tuning_detail;
  $('ab-prof').hidden = !det;
  if (det) {
    $('ab-prof').style.setProperty('--pc', profileColor(det.profile));
    $('ab-prof-text').textContent = `${det.profile} · ${mhz(det.center_hz, 3)}`;
  }
}

function renderControl() {
  const l = live();
  const det = l.tuning_detail;
  const name = det ? det.profile : l.target || null;
  const p = name ? S.profiles[name] : null;
  $('ctl-prof').style.setProperty('--pc', profileColor(name));
  $('cur-name').textContent = name || '—';
  $('cur-desc').textContent = p ? p.summary.split(';')[0] : l.state === 'paused' ? 'Paused' : 'Nothing running';
  $('t-center').textContent = det ? mhz(det.center_hz) : '—';
  $('t-rate').textContent = det && det.sample_rate_hz ? (det.sample_rate_hz / 1e6).toFixed(3).replace(/0+$/, '').replace(/\.$/, '') : '—';
  $('t-lna').textContent = det ? det.lna_gain_db : '—';
  $('t-vga').textContent = det ? det.vga_gain_db : '—';
  $('t-amp').textContent = det ? (det.amp ? 'on' : 'off') : '—';
  $('btn-stop').disabled = !l.desired && l.state !== 'running';
  updateStartLabel();
}

function renderBandHeader(band) {
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
    $('rot-text').textContent = `Single profile: ${d.profile.profile}, no rotation.`;
    const seg = h('span', { class: 'rs cur', '--pc': profileColor(d.profile.profile) }, d.profile.profile);
    seg.style.width = '100%';
    rot.append(seg);
    return;
  }
  const r = rotation(d.schedule, d.anchor, now());
  if (!r) return;
  const parts = d.schedule.map((e) => `${e.profile} ${e.minutes} min`);
  $('rot-text').textContent = `${parts.join(' → ')}, repeating · next switch ${utc(r.current.to, false)} (in ${inSeconds(r.current.to, now())})`;
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
  if (S.socket === 'closed' || !l.state || l.state === 'unreachable') b = null;
  else if (l.state === 'paused' && l.hold) {
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
  box.append(
    card(
      p != null && p > -10 ? 'warn' : '',
      'Input power',
      p == null ? '—' : p.toFixed(1),
      'dBFS',
      p == null ? null : ((p + 60) / 60) * 100,
      p != null && p > -10 ? 'var(--warn)' : 'var(--text2)',
      p == null ? '' : p > -10 ? 'Hot: close to clipping at 0 dBFS. A lower gain may decode better.' : 'Clips at 0 dBFS.',
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
  S.mpm.push(l.decoded_1m || 0);
  if (S.mpm.length > 60) S.mpm.shift();
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
  box.append(
    card(
      live_cpu != null && live_cpu > 120 ? 'warn' : '',
      'CPU · memory',
      live_cpu == null ? '—' : `${Math.round(live_cpu)}`,
      `% · ${st.service ? mib(st.service.memory_bytes) : '—'} MiB`,
      live_cpu == null ? null : Math.min(100, live_cpu / 1.5),
      'var(--text2)',
      `hackrf-live, of one core (limit 150%). hackrf-web ${web_cpu == null ? '—' : web_cpu.toFixed(1)}% · ${st.web ? mib(st.web.memory_bytes) : '—'} MiB.`,
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
  const wrap = h('div', { class: `fiw${fresh ? ' new' : ''}`, '--pc': profileColor(it.profile) });
  const btn = h(
    'button',
    { type: 'button', class: 'fi', 'aria-expanded': 'false' },
    h('span', { class: 'ft' }, utc(it.ts)),
    h(
      'span',
      { class: 'l1' },
      h('span', { class: 'pbadge' }, it.profile),
      it.subject ? h('span', { class: 'subj' }, it.subject) : null,
      it.label ? h('span', { class: 'lbl' }, it.label) : null,
      h('span', { class: 'kind muted' }, it.kind),
    ),
    h('span', { class: 'l2' }, summary, h('span', { class: 'mono muted' }, `${it.freq_hz ? mhz(it.freq_hz) : '—'} MHz · ${it.level_db ?? '—'} dB`)),
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
    wrap.append(h('div', { class: 'fdet' }, kv, it.raw ? h('pre', { class: 'raw' }, it.raw) : null));
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
    S.wf.pushRow(toDb(mean.subarray(i * bins, (i + 1) * bins)), toDb(peak.subarray(i * bins, (i + 1) * bins)), data.ts[i], 4);
  }
}

// --- Every second ------------------------------------------------------------------------

function tick() {
  $('ab-clock').textContent = `${utc(now())} UTC`;
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

init().catch((e) => {
  showCtlError(`Could not load: ${e.message}`);
});
