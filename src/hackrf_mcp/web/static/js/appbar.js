// The app bar every screen shares: the service's state, what it is tuned
// to, and the station clock (UTC).

import { post } from './api.js';
import { ago, h, mhz, utc } from './util.js';

const $ = (id) => document.getElementById(id);

// [css class, label] for a status's live part and the page's connection.
export function stateOf(live, connection = 'open') {
  if (connection === 'closed') return ['st-stopped', 'Disconnected'];
  switch (live && live.state) {
    case 'running':
      return live.falling_behind ? ['st-behind', 'Falling behind'] : ['st-running', 'Running'];
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

export function paintAppBar(live, colorOf, connection = 'open') {
  const [cls, text] = stateOf(live, connection);
  $('ab-state').className = `chip st ${cls}`;
  $('ab-state-text').textContent = text;
  const det = live && live.tuning_detail;
  $('ab-prof').hidden = !det;
  if (det) {
    $('ab-prof').style.setProperty('--pc', colorOf(det.profile));
    $('ab-prof-text').textContent = `${det.profile} · ${mhz(det.center_hz, 3)}`;
  }
}

export function paintClock(nowTs) {
  $('ab-clock').textContent = `${utc(nowTs)} UTC`;
}

// --- Alerts: the chip and banner every screen shares ------------------------------
// From /api/status's `alerts`: how many hits are unacknowledged and the
// latest of them. The banner shows that hit until it is acknowledged here
// or dismissed in this browser; the Alerts screen lists them all.

const SEEN_KEY = 'hackrf-alert-seen';

function seenId() {
  try {
    return Number(localStorage.getItem(SEEN_KEY) || 0);
  } catch (e) {
    return 0;
  }
}

function setSeen(id) {
  try {
    localStorage.setItem(SEEN_KEY, String(id));
  } catch (e) {
    // private browsing: the banner just comes back on reload
  }
}

// Where a hit's rows are: the Log around its time, and the Map at that
// moment for profiles with positions.
export function hitLinks(hit) {
  const q = new URLSearchParams({ since: String(Math.round(hit.ts - 600)), until: String(Math.round(hit.ts + 600)) });
  if (hit.subject) q.set('subject', hit.subject);
  if (hit.profile) q.set('profiles', hit.profile);
  const m = new URLSearchParams({ at: String(Math.round(hit.ts)), profile: hit.profile || '' });
  if (hit.subject) m.set('subject', hit.subject);
  return { log: `/log?${q}`, map: `/map?${m}` };
}

// The subjects with an unacknowledged hit, as "profile|subject" keys (the
// Map's target keys), for the screens to highlight.
export function alertKeys(a) {
  return new Set(((a && a.active) || []).map((x) => `${x.profile}|${x.subject}`));
}

export function sameKeys(a, b) {
  if (a.size !== b.size) return false;
  for (const k of a) if (!b.has(k)) return false;
  return true;
}

// paintAlerts(status.alerts, colorOf, hasMap): hasMap(profile) says
// whether a Map link makes sense for a hit.
export function paintAlerts(a, colorOf = () => '#8a96a6', hasMap = () => false) {
  const right = document.querySelector('.ab-r');
  let chip = $('ab-alerts');
  if (!chip && right) {
    chip = h('a', { class: 'chip alchip', id: 'ab-alerts', href: '/alerts', title: 'Unacknowledged alerts', hidden: true },
      h('span', { class: 'nbadge', id: 'ab-alerts-n' }), 'Alerts');
    right.insertBefore(chip, $('ab-clock'));
  }
  const n = (a && a.unacked) || 0;
  if (chip) {
    chip.hidden = !n;
    $('ab-alerts-n').textContent = n > 99 ? '99+' : String(n);
  }
  const latest = a && a.latest;
  const main = document.querySelector('main.main');
  let banner = $('alert-banner');
  if (!latest || latest.id <= seenId() || location.pathname === '/alerts' || !main) {
    if (banner) banner.remove();
    return;
  }
  if (banner && banner.dataset.hit === String(latest.id)) {
    $('alert-when').textContent = ago(latest.ts, Date.now() / 1000);
    return;
  }
  if (banner) banner.remove();
  const links = hitLinks(latest);
  const ackBtn = h('button', { type: 'button', class: 'btn sm' }, 'Acknowledge');
  const closeBtn = h('button', { type: 'button', class: 'btn sm ghost', 'aria-label': 'Dismiss in this browser' }, 'Dismiss');
  banner = h(
    'section',
    { class: 'banner bad alban', id: 'alert-banner', role: 'alert', 'data-hit': String(latest.id) },
    h('span', { class: 'alico', 'aria-hidden': 'true' }, '!'),
    h(
      'div',
      { class: 'altext' },
      h('b', {}, latest.rule_name),
      ' ',
      h('span', { class: 'pbadge', '--pc': colorOf(latest.profile) }, latest.profile),
      ' ',
      h('span', { class: 'mono' }, latest.subject || '—'),
      latest.label && latest.label !== latest.subject ? h('span', { class: 't2' }, ` ${latest.label}`) : null,
      h('span', { class: 't2' }, ` · ${latest.detail} · `),
      h('span', { class: 'mono t2', id: 'alert-when' }, ago(latest.ts, Date.now() / 1000)),
      n > 1 ? h('a', { class: 'almore', href: '/alerts' }, ` +${n - 1} more`) : null,
    ),
    h(
      'div',
      { class: 'row wr' },
      h('a', { class: 'btn sm', href: links.log }, 'Log'),
      hasMap(latest.profile) ? h('a', { class: 'btn sm', href: links.map }, 'Map') : null,
      ackBtn,
      closeBtn,
    ),
  );
  ackBtn.addEventListener('click', async () => {
    ackBtn.disabled = true;
    try {
      await post('/api/alerts/ack', { ids: [latest.id] });
      setSeen(latest.id);
      banner.remove();
    } catch (e) {
      ackBtn.disabled = false;
      ackBtn.textContent = 'Retry';
    }
  });
  closeBtn.addEventListener('click', () => {
    setSeen(latest.id);
    banner.remove();
  });
  main.prepend(banner);
}
