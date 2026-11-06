// The Summaries screen: for a period, what the station heard per profile,
// from the log as it is (no storage of its own): when each profile was on
// the air, how many subjects it heard and the busiest of them, its message
// rate over time, and where the ones with positions were (a heat map).
// Profiles, colours, fields and which profiles have positions all come from
// /api/registry; nothing here names a protocol.

import { api } from './api.js';
import { paintAlerts, paintAppBar, paintClock } from './appbar.js';
import { addBasemap } from './basemap.js';
import { timeTicks } from './historyutil.js';
import { ago, assignColors, clear, fmtUtc, h, mhzShort, utc } from './util.js';
import { keyHeading, panelColumns, panelQueryText, windowText } from './panelutil.js';
import {
  aircraftName, aircraftQuery, aircraftWanted, areaPath, binAlpha, binLabels, binOf, bucketsFor, cellFor, columnsOf,
  dropEchoes, durText, fieldText, gridImage, heatBins, kindsText, numericCols, onAir, rangeOf, runsOf, tickTimes,
} from './summutil.js';

const $ = (id) => document.getElementById(id);
const NOW = () => Date.now() / 1000;
const SVG = 'http://www.w3.org/2000/svg';
const RATE_W = 1000;
const RATE_H = 36;
const TOP = 20;
const GAP_LABEL = { paused: 'paused', error: 'error', idle: 'stopped', down: 'service down' };
const GAP_CLASS = { paused: 'paused', error: 'error', idle: '', down: '' };

const S = {
  registry: null,
  profiles: {},
  colors: {},
  data: null,
  since: 0,
  until: 0,
  status: null,
  statusAt: 0,
  heat: { profile: null, weight: 'targets', grid: null, cell: null, box: null, overlay: null, seq: 0, fitted: false },
  // The aircraft database's answers, kept across periods: per ICAO address
  // (with the callsign it was asked with), and the address of each tail.
  ac: { info: new Map(), asked: new Map(), tails: new Map(), seq: 0, failed: false, built: false },
};

const color = (name) => S.colors[name] || '#8a96a6';
const hasMap = (name) => (S.profiles[name] && S.profiles[name].views || []).some((v) => v.name === 'map');
const nf = (n) => Math.round(n).toLocaleString('en-US');

// --- Start-up ------------------------------------------------------------------------------

async function init() {
  S.registry = await api('/api/registry');
  for (const p of S.registry.profiles) S.profiles[p.name] = p;
  S.colors = assignColors(S.registry.profiles);
  wire();
  pollStatus();
  setInterval(pollStatus, 10000);
  setInterval(() => paintClock(S.status ? S.status.now + (NOW() - S.statusAt) : NOW()), 1000);
  fromUrl();
  await load();
}

async function pollStatus() {
  try {
    const st = await api('/api/status');
    S.status = st;
    S.statusAt = NOW();
    paintAppBar(st.live, color, 'open');
    paintAlerts(st.alerts, color, hasMap);
  } catch (e) {
    paintAppBar(null, color, 'closed');
  }
}

function wire() {
  $('range').addEventListener('change', () => {
    const custom = $('range').value === 'custom';
    $('custom').hidden = !custom;
    if (custom) {
      if (!$('from').value) $('from').value = fmtUtc(S.since || NOW() - 86400);
      $('from').focus();
    } else {
      load();
    }
  });
  $('rform').addEventListener('submit', (e) => {
    e.preventDefault();
    load();
  });
  for (const b of document.querySelectorAll('[data-weight]')) {
    b.addEventListener('click', () => {
      S.heat.weight = b.dataset.weight;
      for (const o of document.querySelectorAll('[data-weight]')) o.setAttribute('aria-pressed', String(o === b));
      drawHeat();
    });
  }
  $('cov').addEventListener('pointermove', coverageTip);
  $('cov').addEventListener('pointerleave', hideTip);
  $('rates').addEventListener('pointermove', rateTip);
  $('rates').addEventListener('pointerleave', () => {
    hideTip();
    const x = $('rates').querySelector('.xhair');
    if (x) x.remove();
  });
}

function fromUrl() {
  const u = new URLSearchParams(location.search);
  const since = Number(u.get('since'));
  if (since > 0) {
    $('range').value = 'custom';
    $('custom').hidden = false;
    $('from').value = fmtUtc(since);
    const until = Number(u.get('until'));
    $('to').value = until > 0 ? fmtUtc(until) : '';
  } else if (u.get('range') && [...$('range').options].some((o) => o.value === u.get('range'))) {
    $('range').value = u.get('range');
  }
}

function showError(msg) {
  $('serr').hidden = !msg;
  $('serr').textContent = msg || '';
}

// --- Data ----------------------------------------------------------------------------------

async function load() {
  showError(null);
  let since;
  let until;
  try {
    [since, until] = rangeOf($('range').value, NOW(), $('from').value, $('to').value);
  } catch (e) {
    showError(e.message);
    return;
  }
  S.since = since;
  S.until = until;
  const u = new URLSearchParams();
  if ($('range').value === 'custom') {
    u.set('since', String(Math.round(since)));
    if ($('to').value.trim()) u.set('until', String(Math.round(until)));
  } else {
    u.set('range', $('range').value);
  }
  history.replaceState(null, '', `/summaries?${u}`);
  $('smain').classList.add('busy');
  try {
    // The operator's panels for this screen come with the built-in cards;
    // one that fails takes nothing else down.
    [S.data, S.panels] = await Promise.all([
      api(`/api/summaries?since=${since}&until=${until}&buckets=${bucketsFor(until - since)}&top=${TOP}`),
      api(`/api/panels?slot=summaries.card&since=${since}&until=${until}`).catch(() => null),
    ]);
  } catch (e) {
    showError(`Could not read the log: ${e.message}`);
    return;
  } finally {
    $('smain').classList.remove('busy');
  }
  const span = until - since;
  $('r-label').textContent = `${fmtUtc(since)} – ${fmtUtc(until)} UTC`;
  $('r-note').textContent = `${durText(span)} · rate per ${durText(S.data.bucket_s)}`;
  $('c-range').textContent = `${fmtUtc(since)} – ${utc(until, false)} UTC`;
  S.air = onAir(S.data.coverage, since, until);
  renderCoverage();
  renderCards();
  loadAircraftNames();
  renderRates();
  S.heat.fitted = false;
  S.heat.grid = null;
  S.heat.box = null;
  renderHeatControls();
  await loadHeat();
}

// --- Coverage ------------------------------------------------------------------------------

function renderCoverage() {
  const lane = clear($('cov'));
  const { since: a, until: b } = S;
  for (const c of S.data.coverage) {
    const x0 = Math.max(c.from, a);
    const x1 = Math.min(c.to, b);
    if (x1 <= x0) continue;
    const running = c.state === 'running';
    lane.append(
      h('span', {
        class: running ? '' : `gapx ${GAP_CLASS[c.state] || ''}`,
        '--l': `${(((x0 - a) / (b - a)) * 100).toFixed(3)}%`,
        '--w': `${(((x1 - x0) / (b - a)) * 100).toFixed(3)}%`,
        '--pc': running ? color(c.profile) : null,
      }),
    );
  }
  const leg = clear($('c-legend'));
  const on = Object.entries(S.air.profiles).sort((x, y) => y[1] - x[1]);
  for (const [name, s] of on) {
    leg.append(h('span', { '--pc': color(name) }, h('span', { class: 'sw' }), name, ' ', h('span', { class: 'mono muted' }, durText(s))));
  }
  for (const [state, s] of Object.entries(S.air.gaps)) {
    if (s < 1) continue; // a stop and start a moment apart: nothing to show
    leg.append(h('span', {}, h('i', { class: `hatch ${GAP_CLASS[state] || ''}` }), GAP_LABEL[state] || state, ' ', h('span', { class: 'mono muted' }, durText(s))));
  }
  if (!on.length) leg.append(h('span', { class: 'muted' }, 'Nothing was on the air in this period.'));
  ticks($('c-ticks'));
}

// Time labels under a strip, as many as its width has room for.
function ticks(box) {
  clear(box);
  const { since: a, until: b } = S;
  const long = b - a > 2 * 86400;
  const list = tickTimes(a, b, box.clientWidth || 600, long ? 90 : 60, timeTicks);
  list.forEach((t, i) => {
    const label = long ? fmtUtc(t).slice(5, 16) : utc(t, false);
    box.append(h('span', { class: i === 0 ? 'first' : i === list.length - 1 ? 'last' : '', '--l': `${(((t - a) / (b - a)) * 100).toFixed(2)}%` }, label));
  });
}

function coverageTip(e) {
  const r = $('cov').getBoundingClientRect();
  const t = S.since + ((e.clientX - r.left) / r.width) * (S.until - S.since);
  const c = S.data && S.data.coverage.find((x) => t >= x.from && t < x.to);
  if (!c) return hideTip();
  const what = c.state === 'running' ? c.profile : GAP_LABEL[c.state] || c.state;
  showTip(e, [
    h('div', { class: 'tt' }, `${fmtUtc(Math.max(c.from, S.since))} – ${utc(Math.min(c.to, S.until), false)} UTC`),
    h('div', { class: 'tr' }, c.state === 'running' ? h('span', { class: 'tk', '--pc': color(c.profile) }) : null, h('span', { class: 'tv' }, durText(Math.min(c.to, S.until) - Math.max(c.from, S.since))), what),
  ]);
}

// --- Cards ---------------------------------------------------------------------------------

function renderCards() {
  const box = clear($('cards'));
  const quiet = [];
  for (const p of S.registry.profiles) {
    const st = S.data.profiles[p.name];
    if (!st) continue;
    if (!st.rows) {
      quiet.push(p.name);
      continue;
    }
    box.append(card(p, st));
  }
  $('notheard').hidden = !quiet.length;
  $('notheard').textContent = quiet.length
    ? `Nothing logged in this period by ${quiet.map((n) => (S.air.profiles[n] ? `${n} (on the air ${durText(S.air.profiles[n])})` : n)).join(', ')}.`
    : '';
  const panels = (S.panels && S.panels.panels) || [];
  for (const p of panels) box.append(panelCard(p));
  const bad = (S.panels && S.panels.errors) || [];
  if (bad.length) {
    $('notheard').hidden = false;
    $('notheard').append(
      `${$('notheard').textContent ? ' ' : ''}${bad.length} panel ${bad.length === 1 ? 'file' : 'files'} didn't load: `,
      h('a', { href: '/plugins' }, 'see Plugins'),
      '.',
    );
  }
  if (!box.children.length) {
    box.append(h('section', { class: 'panel' }, h('div', { class: 'pb' }, h('div', { class: 'empty' }, h('b', {}, 'Nothing logged in this period'), h('span', {}, 'Pick a longer period, or see when the receiver was on the air above.')))));
  }
}

// A card from one of the operator's panel files (web/panels.py).
function panelCard(p) {
  const subjects = p.group_by === 'subject';
  const cols = panelColumns(p);
  const long = S.until - S.since > 86400;
  const when = (ts) => (long ? fmtUtc(ts).slice(5) : utc(ts));
  const keyCell = (r) =>
    subjects && r.subject
      ? h('a', { href: logHref({ subject: r.subject, profiles: r.profile }) }, r.key)
      : h('span', { class: 'mono' }, r.key);
  let body;
  if (p.error) {
    body = h('p', { class: 'ctl-err' }, p.error);
  } else if (!p.rows.length) {
    body = h('p', { class: 'muted sm' }, 'Nothing in this period.');
  } else if (p.render === 'stat') {
    const what = p.group_by === 'subject' ? (p.total === 1 ? 'subject' : 'subjects') : `different ${p.group_by.replace(/_/g, ' ')} ${p.total === 1 ? 'value' : 'values'}`;
    body = h('div', { class: 'cardh' }, h('span', { class: 'big' }, nf(p.total)), h('span', { class: 't2' }, `${what} · ${nf(p.matched)} rows`));
  } else if (p.render === 'list') {
    body = h('ul', { class: 'plist' }, p.rows.map((r) => h('li', {}, keyCell(r), r.label && r.label !== r.key ? h('span', { class: 't2' }, ` ${r.label}`) : null, h('span', { class: 'muted mono' }, ` · ${ago(r.last, NOW())}`))));
  } else {
    body = h(
      'div',
      { class: 'tscroll' },
      h(
        'table',
        { class: 'tbl' },
        h('thead', {}, h('tr', {}, h('th', {}, keyHeading(p)), cols.map((f) => h('th', {}, f.replace(/_/g, ' '))), h('th', { class: 'r' }, 'Rows'), h('th', {}, 'Last'))),
        h(
          'tbody',
          {},
          p.rows.map((r) =>
            h(
              'tr',
              {},
              h('td', {}, keyCell(r), subjects && r.label && r.label !== r.key ? h('span', { class: 'lbl' }, r.label) : null),
              cols.map((f) => h('td', { class: typeof r.fields[f] === 'number' ? 'mono r' : 'mono' }, fieldText(r.fields[f]))),
              h('td', { class: 'mono r' }, nf(r.rows)),
              h('td', { class: 'mono' }, when(r.last)),
            ),
          ),
        ),
      ),
    );
  }
  const more = !p.error && p.total > p.rows.length ? h('div', { class: 'more' }, `${p.rows.length} of ${nf(p.total)}.`) : null;
  return h(
    'section',
    { class: 'panel scard', '--pc': color(p.profiles[0]), 'aria-label': p.title, title: panelQueryText(p) },
    h(
      'div',
      { class: 'ph' },
      p.profiles.map((n) => h('span', { class: 'pbadge', '--pc': color(n) }, n)),
      h('h2', {}, p.title),
      h('span', { class: 'tag' }, 'panel'),
      p.window_s ? h('span', { class: 'muted sm' }, `last ${windowText(p.window_s)}`) : null,
      h('span', { class: 'sp' }),
      h('a', { class: 'btn sm', href: logHref({ profiles: p.profiles.join(',') }) }, 'Log'),
    ),
    h('div', { class: 'pb col' }, body, more),
  );
}

function logHref(extra) {
  const u = new URLSearchParams({ since: String(Math.round(S.since)), until: String(Math.round(S.until)), ...extra });
  return `/log?${u}`;
}

// --- Aircraft names -------------------------------------------------------------------------
// For profiles whose subjects are aircraft (registry subject_type), the
// busiest subjects get a name from the offline aircraft database, as the
// Map does: registration, make and model, owner. Tails are first turned
// into ICAO addresses. The cards are drawn again once the answers are in.

// Shown once the database has answered and has been built (its weekly
// refresh may be off while a database built earlier is still there).
const acEnabled = () => !!S.registry.aircraft && S.ac.built;
const isAircraftProfile = (p) => p.subject_type === 'icao24' || p.subject_type === 'registration';

async function loadAircraftNames() {
  if (!S.registry.aircraft || !S.data) return;
  const seq = ++S.ac.seq;
  S.ac.failed = false;
  const want = aircraftWanted(S.profiles, S.data);
  try {
    const tails = want.tails.filter((t) => !S.ac.tails.has(t)).slice(0, 500);
    if (tails.length) {
      const r = await api(`/api/aircraft?r=${encodeURIComponent(tails.join(','))}`);
      S.ac.built = !!(r.db && r.db.built_at);
      const found = r.registrations || {};
      for (const t of tails) S.ac.tails.set(t, found[t] || null);
    }
    const pairs = want.icao.slice();
    for (const t of want.tails) {
      const i = S.ac.tails.get(t);
      if (i && !pairs.some(([x]) => x === i)) pairs.push([i, '']);
    }
    const ask = pairs.filter(([i, c]) => !S.ac.asked.has(i) || (c && S.ac.asked.get(i) !== c)).slice(0, 500);
    if (ask.length) {
      const r = await api(`/api/aircraft?a=${encodeURIComponent(aircraftQuery(ask))}`);
      S.ac.built = !!(r.db && r.db.built_at);
      for (const [i, c] of ask) S.ac.asked.set(i, c);
      for (const [i, info] of Object.entries(r.aircraft || {})) S.ac.info.set(i, info);
    }
  } catch (e) {
    S.ac.failed = true; // the cards stand without names
  }
  if (seq === S.ac.seq) renderCards();
}

function aircraftCell(p, subject) {
  const s = String(subject || '').trim().toUpperCase();
  const icao = p.subject_type === 'registration' ? S.ac.tails.get(s) : s;
  const info = icao ? S.ac.info.get(icao) : null;
  if (!info) {
    // "…" while the database hasn't answered yet; "—" when it knows nothing.
    const pending =
      !S.ac.failed &&
      (p.subject_type === 'registration' ? !S.ac.tails.has(s) || (icao && !S.ac.asked.has(icao)) : /^[0-9A-F]{6}$/.test(s) && !S.ac.asked.has(s));
    return h('td', { class: 'acname muted' }, pending ? '…' : '—');
  }
  const n = aircraftName(info);
  const c = (S.registry.aircraft.classes || []).find((x) => x.name === n.cls);
  return h(
    'td',
    { class: 'acname', title: n.full },
    c && n.cls !== 'unknown' ? h('span', { class: 'actag', '--cc': c.color }, c.short) : null,
    n.short || h('span', { class: 'muted' }, c ? c.label : n.cls),
  );
}

function card(p, st) {
  // A field that only repeats the label (a callsign, a ship's name) is
  // left out: the label is shown with the subject.
  const cols = dropEchoes(columnsOf(p), st.top);
  const named = acEnabled() && isAircraftProfile(p);
  const numeric = numericCols(cols, st.top);
  const kinds = Object.keys(st.by_kind);
  const air = S.air.profiles[p.name] || 0;
  const head = h(
    'div',
    { class: 'ph' },
    h('span', { class: 'pbadge' }, p.name),
    h('h2', {}, 'Subjects heard'),
    p.redacted ? h('span', { class: 'tag' }, 'metadata only') : null,
    h('span', { class: 'sp' }),
    hasMap(p.name) ? h('a', { class: 'btn sm', href: '/map' }, 'Map') : null,
    h('a', { class: 'btn sm', href: logHref({ profiles: p.name }) }, 'Log'),
  );
  const sub = [
    kinds.length > 1 ? kindsText(st.by_kind, p.name) : (kinds[0] || '').replace(/-/g, ' '),
    `${nf(st.messages)} messages`,
    `on the air ${durText(air)} of ${durText(S.until - S.since)}`,
  ].filter(Boolean).join(' · ');
  const big = h('div', { class: 'cardh' }, h('span', { class: 'big' }, nf(st.subjects)), h('span', { class: 't2' }, sub));
  const thead = h(
    'tr',
    {},
    h('th', {}, 'Subject'),
    named ? h('th', {}, 'Aircraft') : null,
    h('th', { class: 'r' }, 'Msgs'),
    h('th', {}, 'Last'),
    cols.map((f) => h('th', { class: numeric.has(f) ? 'r' : null }, f.replace(/_/g, ' '))),
    h('th', {}, 'Channel'),
  );
  const long = S.until - S.since > 86400;
  const rows = st.top.map((e) =>
    h(
      'tr',
      {},
      h(
        'td',
        {},
        h('a', { href: logHref({ subject: e.subject, profiles: p.name }), title: `Open ${e.subject} in the log` }, e.subject),
        e.labels.length ? h('span', { class: 'lbl' }, e.labels[0]) : null,
      ),
      named ? aircraftCell(p, e.subject) : null,
      h('td', { class: 'mono r' }, nf(e.messages)),
      h('td', { class: 'mono' }, long ? fmtUtc(e.last).slice(5) : utc(e.last)),
      cols.map((f) => h('td', { class: numeric.has(f) ? 'mono r' : 'mono' }, fieldText(e.fields[f]))),
      h('td', { class: 'mono' }, e.freqs.length ? e.freqs.map((f) => mhzShort(f)).join(', ') : '—'),
    ),
  );
  const more = st.subjects > st.top.length ? h('div', { class: 'more' }, `The ${st.top.length} with the most messages of ${nf(st.subjects)}.`) : null;
  return h(
    'section',
    { class: 'panel scard', '--pc': color(p.name), 'aria-label': `${p.name}: subjects heard` },
    head,
    h('div', { class: 'pb col' }, big, h('div', { class: 'tscroll' }, h('table', { class: 'tbl' }, h('thead', {}, thead), h('tbody', {}, rows))), more),
  );
}

// --- Message rate --------------------------------------------------------------------------

function rateProfiles() {
  return S.registry.profiles.map((p) => p.name).filter((n) => {
    const st = S.data.profiles[n];
    return st && (st.rows || S.air.profiles[n]);
  });
}

function renderRates() {
  const box = clear($('rates'));
  const names = rateProfiles();
  const bs = S.data.bucket_s;
  for (const name of names) {
    const st = S.data.profiles[name];
    const svg = document.createElementNS(SVG, 'svg');
    svg.setAttribute('viewBox', `0 0 ${RATE_W} ${RATE_H}`);
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', `${name}: ${nf(st.messages)} messages in the period`);
    const max = Math.max(0, ...st.rate);
    const area = document.createElementNS(SVG, 'path');
    area.setAttribute('class', 'area');
    area.setAttribute('d', areaPath(st.rate, RATE_W, RATE_H - 6, max));
    svg.append(area);
    for (const [x, w] of runsOf(S.data.coverage, name, S.since, S.until, RATE_W)) {
      const r = document.createElementNS(SVG, 'rect');
      r.setAttribute('class', 'onair');
      r.setAttribute('x', x.toFixed(1));
      r.setAttribute('y', String(RATE_H - 4));
      r.setAttribute('width', Math.max(w, 0.5).toFixed(1));
      r.setAttribute('height', '4');
      svg.append(r);
    }
    const base = document.createElementNS(SVG, 'path');
    base.setAttribute('class', 'base');
    base.setAttribute('d', `M0,${RATE_H - 5.5} H${RATE_W}`);
    svg.append(base);
    const air = S.air.profiles[name] || 0;
    box.append(
      h(
        'div',
        { class: 'rr', '--pc': color(name) },
        h('span', { class: 'row' }, h('span', { class: 'pbadge' }, name)),
        svg,
        h(
          'span',
          { class: 'tot' },
          st.messages ? `${nf(st.messages)} msgs` : '—',
          h('small', {}, `${durText(air)} on the air`),
          h('small', {}, `busiest ${durText(bs)}: ${nf(Math.max(0, ...st.rate))}`),
        ),
      ),
    );
  }
  if (!names.length) box.append(h('p', { class: 'muted sm pb' }, 'Nothing was on the air in this period.'));
  ticks($('rt-ticks'));
}

function rateTip(e) {
  const svg = $('rates').querySelector('svg');
  if (!svg || !S.data) return;
  const r = svg.getBoundingClientRect();
  const f = (e.clientX - r.left) / r.width;
  if (f < 0 || f >= 1) {
    hideTip();
    return;
  }
  const names = rateProfiles();
  const n = S.data.profiles[names[0]].rate.length;
  const i = Math.min(n - 1, Math.floor(f * n));
  const a = S.since + i * S.data.bucket_s;
  const b = Math.min(S.until, a + S.data.bucket_s);
  let x = $('rates').querySelector('.xhair');
  if (!x) {
    x = h('div', { class: 'xhair' });
    $('rates').append(x);
  }
  const box = $('rates').getBoundingClientRect();
  x.style.setProperty('--x', `${r.left - box.left + ((i + 0.5) / n) * r.width}px`);
  showTip(e, [
    h('div', { class: 'tt' }, `${fmtUtc(a)} – ${utc(b, false)} UTC`),
    names.map((name) => {
      const v = S.data.profiles[name].rate[i];
      const air = runsOf(S.data.coverage, name, a, b, 1).reduce((s, [, w]) => s + w, 0);
      return h('div', { class: 'tr' }, h('span', { class: 'tk', '--pc': color(name) }), h('span', { class: 'tv' }, nf(v)), name, air ? null : h('span', { class: 'muted' }, '· off the air'));
    }),
  ]);
}

// --- Tooltip -------------------------------------------------------------------------------

function showTip(e, children) {
  const tip = clear($('tip'));
  tip.append(...children.flat());
  tip.hidden = false;
  const w = tip.offsetWidth;
  const left = e.clientX + 14 + w > window.innerWidth ? e.clientX - 14 - w : e.clientX + 14;
  tip.style.setProperty('--tx', `${Math.max(4, left)}px`);
  tip.style.setProperty('--ty', `${e.clientY + 14}px`);
}

function hideTip() {
  $('tip').hidden = true;
}

// --- Heat map ------------------------------------------------------------------------------

function heatProfiles() {
  return S.registry.profiles.map((p) => p.name).filter((n) => hasMap(n));
}

function renderHeatControls() {
  const names = heatProfiles();
  const box = clear($('hm-profiles'));
  if (!S.heat.profile || !names.includes(S.heat.profile)) {
    // The one that heard the most subjects in the period.
    S.heat.profile = names.slice().sort((x, y) => (S.data.profiles[y]?.subjects || 0) - (S.data.profiles[x]?.subjects || 0))[0] || null;
  }
  for (const name of names) {
    const b = h('button', { type: 'button', 'aria-pressed': String(name === S.heat.profile) }, h('span', { class: 'sw', '--pc': color(name) }), name);
    b.addEventListener('click', () => {
      S.heat.profile = name;
      for (const o of box.children) o.setAttribute('aria-pressed', String(o === b));
      S.heat.fitted = false;
      S.heat.grid = null;
      S.heat.box = null;
      loadHeat();
    });
    box.append(b);
  }
}

function ensureMap() {
  if (S.map) return;
  S.map = L.map('hmap', { zoomControl: true, attributionControl: true, worldCopyJump: true }).setView(S.registry.receiver || [20, 0], S.registry.receiver ? 8 : 2);
  S.map.attributionControl.setPrefix('Leaflet');
  L.control.scale({ imperial: true, metric: true }).addTo(S.map);
  S.baseLayer = L.layerGroup().addTo(S.map);
  addBasemap(S.baseLayer, S.registry.basemap);
  S.map.on('moveend', () => loadHeat());
  S.map.on('mousemove', heatInfo);
  S.map.on('mouseout', () => {
    $('hm-info').textContent = infoText();
  });
}

// Asks for the grid again only when the zoom wants another cell size, or
// the view has left the area already fetched.
async function loadHeat() {
  if (!S.data) return;
  const name = S.heat.profile;
  if (!name) {
    $('hm-info').textContent = 'No profile here logs positions.';
    return;
  }
  ensureMap();
  const view = S.map.getBounds();
  const cell = S.heat.fitted ? cellFor(S.map.getZoom()) : 0.05;
  if (S.heat.grid && S.heat.cell === cell && (!S.heat.box || S.heat.box.contains(view))) return;
  const params = new URLSearchParams({ since: String(S.since), until: String(S.until), profile: name, cell: String(cell) });
  let box = null;
  if (S.heat.fitted) {
    box = view.pad(0.5);
    const w = Math.max(-180, box.getWest());
    const e = Math.min(180, box.getEast());
    const s = Math.max(-90, box.getSouth());
    const n = Math.min(90, box.getNorth());
    params.set('bbox', [w, s, e, n].map((v) => v.toFixed(4)).join(','));
  }
  // The same request already on its way (moveend and the follow-up after
  // fitting can both ask): let that one answer.
  const key = params.toString();
  if (S.heat.pending === key) return;
  S.heat.pending = key;
  const seq = ++S.heat.seq;
  let grid;
  try {
    grid = await api(`/api/positions/grid?${params}`);
  } catch (e) {
    $('hm-info').textContent = `Could not read positions: ${e.message}`;
    return;
  } finally {
    if (S.heat.pending === key) S.heat.pending = null;
  }
  if (seq !== S.heat.seq) return; // a newer request has gone out
  S.heat.grid = grid;
  S.heat.cell = cell;
  S.heat.box = box;
  S.heat.index = new Map((grid.cells[name] || []).map((c) => [`${c[0]},${c[1]}`, c]));
  if (!S.heat.fitted) {
    S.heat.fitted = true;
    const cells = grid.cells[name] || [];
    const img = gridImage(cells, grid.cell_deg);
    if (img) S.map.fitBounds([[img.south, img.west], [img.north, img.east]], { maxZoom: 11, padding: [12, 12] });
    // moveend asks again at the zoom's own cell size; if the view didn't
    // move there is no moveend, so ask once more anyway (a no-op otherwise).
    setTimeout(loadHeat, 400);
  }
  drawHeat();
}

function drawHeat() {
  const grid = S.heat.grid;
  const name = S.heat.profile;
  if (S.heat.overlay) {
    S.heat.overlay.remove();
    S.heat.overlay = null;
  }
  const leg = clear($('hm-legend'));
  $('hm-info').textContent = infoText();
  if (!grid || !name) return;
  const cells = grid.cells[name] || [];
  const img = gridImage(cells, grid.cell_deg);
  if (!img) return;
  const k = S.heat.weight === 'fixes' ? 2 : 3;
  const edges = heatBins(Math.max(...cells.map((c) => c[k])));
  const canvas = document.createElement('canvas');
  canvas.width = img.width;
  canvas.height = img.height;
  const g = canvas.getContext('2d');
  g.fillStyle = color(name);
  for (const c of cells) {
    g.globalAlpha = binAlpha(binOf(c[k], edges), edges.length);
    const [x, y, w, hh] = img.rect(c[0], c[1]);
    g.fillRect(x, y, w, hh);
  }
  S.heat.overlay = L.imageOverlay(canvas.toDataURL('image/png'), [[img.south, img.west], [img.north, img.east]], {
    className: 'heatimg',
    interactive: false,
    zIndex: 5,
  }).addTo(S.map);
  leg.append(h('span', { class: 'muted' }, S.heat.weight === 'fixes' ? 'fixes per cell' : 'targets per cell'));
  binLabels(edges).forEach((label, i) => {
    leg.append(h('span', { class: 'hleg', '--pc': color(name), '--a': `${Math.round(binAlpha(i, edges.length) * 100)}%` }, h('span', { class: 'hs' }), label));
  });
}

function infoText() {
  const grid = S.heat.grid;
  if (!grid) return 'Loading positions…';
  const name = S.heat.profile;
  const km = grid.cell_deg * 111;
  const size = km < 1 ? `${Math.round(km * 1000)} m` : `${km.toFixed(km < 10 ? 1 : 0)} km`;
  if (!grid.fixes) return `No positions from ${name} in this view for the period.`;
  return `${nf(grid.targets)} targets and ${nf(grid.fixes)} fixes from ${name} in and around this view · cells ${grid.cell_deg}° (about ${size} north–south)${grid.truncated ? ' · more cells than shown' : ''}. Point at a cell for its count.`;
}

function heatInfo(e) {
  const grid = S.heat.grid;
  if (!grid || !S.heat.index) return;
  const cell = grid.cell_deg;
  const key = `${Math.floor((e.latlng.lng + 180) / cell)},${Math.floor((e.latlng.lat + 90) / cell)}`;
  const c = S.heat.index.get(key);
  $('hm-info').textContent = c
    ? `${nf(c[3])} ${c[3] === 1 ? 'target' : 'targets'} · ${nf(c[2])} ${c[2] === 1 ? 'fix' : 'fixes'} in this ${cell}° cell`
    : `No positions from ${S.heat.profile} in this ${cell}° cell`;
}

init().catch((e) => showError(`Could not start: ${e.message}`));
