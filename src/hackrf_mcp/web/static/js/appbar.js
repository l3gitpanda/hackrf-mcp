// The app bar every screen shares: the service's state, what it is tuned
// to, and the station clock (UTC).

import { mhz, utc } from './util.js';

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
