// The Map screen: every profile whose protocol module declares a "map" view
// (the registry says which, and which fields hold lat, lon, altitude,
// heading and the label), drawn from the log's positions table, live or
// for a past range, with tracks. Leaflet (vendored, the global L) does pan
// and zoom. Nothing is loaded from other sites: the base map is drawn only
// if the operator has put one on the server (/api/basemap.pmtiles).

import { api, LiveSocket } from './api.js';
import { paintAppBar, paintClock } from './appbar.js';
import { ago, assignColors, clear, fmtUtc, h, parseUtc, utc } from './util.js';
import { heading, positionAt, prune, targetKey, trackWindow } from './maputil.js';
import { addBasemap } from './basemap.js';

const $ = (id) => document.getElementById(id);
const NOW = () => Date.now() / 1000;
const SVG = 'http://www.w3.org/2000/svg';
// A target with no fix for this long is drawn faded; AIS stations that
// stand still are only logged every 5 minutes, so the limit is above that.
const STALE_S = 330;
const HIDE_S = 1800;

const S = {
  registry: null,
  profiles: {},
  colors: {},
  mapProfiles: [],
  on: new Set(),
  targets: new Map(),
  mode: 'live',
  since: 0,
  until: 0,
  cursor: NOW(),
  sel: null,
  status: null,
  statusAt: 0,
  playing: null,
  fitted: false,
  layers: { markers: null, tracks: null, rings: null },
};

const color = (name) => S.colors[name] || '#8a96a6';
const view = (profile) => (S.profiles[profile].views || []).find((v) => v.name === 'map');
const role = (profile, r) => {
  const v = view(profile);
  return v && v.roles ? v.roles[r] : undefined;
};

// --- Start-up ------------------------------------------------------------------------------

async function init() {
  S.registry = await api('/api/registry');
  for (const p of S.registry.profiles) S.profiles[p.name] = p;
  S.colors = assignColors(S.registry.profiles);
  S.mapProfiles = S.registry.profiles.filter((p) => view(p.name)).map((p) => p.name);
  for (const p of S.mapProfiles) S.on.add(p);
  S.map = L.map('map', { zoomControl: true, attributionControl: true, worldCopyJump: true, preferCanvas: false }).setView([20, 0], 2);
  S.map.attributionControl.setPrefix('Leaflet');
  L.control.scale({ imperial: true, metric: true }).addTo(S.map);
  S.layers.base = L.layerGroup().addTo(S.map);
  S.layers.grid = L.layerGroup().addTo(S.map);
  S.layers.rings = L.layerGroup().addTo(S.map);
  S.layers.tracks = L.layerGroup().addTo(S.map);
  S.layers.markers = L.layerGroup().addTo(S.map);
  S.map.on('moveend', () => {
    drawGrid();
    renderList();
  });
  wire();
  buildLayerChips();
  loadBasemap();
  pollStatus();
  setInterval(pollStatus, 10000);
  const u = new URLSearchParams(location.search);
  const at = Number(u.get('at'));
  if (at > 0) await openAt(at, u.get('profile') || '', u.get('subject') || '');
  else await loadRange(NOW() - Number($('trail').value), null);
  new LiveSocket(onMessage, () => {});
  setInterval(tick, 1000);
}

function wire() {
  $('m-live').addEventListener('click', () => setMode('live'));
  $('m-hist').addEventListener('click', () => setMode('history'));
  $('trail').addEventListener('change', () => loadRange(NOW() - Number($('trail').value), null));
  $('h-load').addEventListener('click', loadHistory);
  $('h-play').addEventListener('click', togglePlay);
  $('cursor').addEventListener('input', () => {
    S.cursor = S.since + (Number($('cursor').value) / 1000) * (S.until - S.since);
    renderCursorLabel();
    render();
  });
  for (const id of ['o-tracks', 'o-labels', 'o-rings']) $(id).addEventListener('change', render);
  $('s-close').addEventListener('click', () => select(null));
  $('s-zoom').addEventListener('click', () => {
    const t = S.sel && S.targets.get(S.sel);
    const p = t && (positionAt(t.track, S.cursor) || t.track[t.track.length - 1]);
    if (p) S.map.setView([p.lat, p.lon], Math.max(S.map.getZoom(), 11));
  });
}

async function pollStatus() {
  try {
    const st = await api('/api/status');
    S.status = st;
    S.statusAt = NOW();
    paintAppBar(st.live, color, 'open');
  } catch (e) {
    paintAppBar(null, color, 'closed');
  }
}

function showError(msg) {
  $('merr').hidden = !msg;
  $('merr').textContent = msg || '';
}

// --- Data ----------------------------------------------------------------------------------

function target(profile, subject) {
  const key = targetKey(profile, subject);
  let t = S.targets.get(key);
  if (!t) {
    t = { key, profile, subject, label: null, kind: null, fields: {}, track: [], last: 0 };
    S.targets.set(key, t);
  }
  return t;
}

async function loadRange(since, until) {
  showError(null);
  S.since = since;
  S.until = until || NOW();
  S.targets = new Map();
  S.layers.markers.clearLayers();
  S.layers.tracks.clearLayers();
  const q = `since=${since}${until ? `&until=${until}` : ''}`;
  const [pos, ...sums] = await Promise.all([
    api(`/api/positions?${q}&limit=50000`),
    ...S.mapProfiles.map((p) => api(`/api/summary?profile=${encodeURIComponent(p)}&${q}&limit=2000`)),
  ]);
  S.step = pos.step;
  S.truncated = pos.truncated;
  for (const p of pos.positions) {
    if (!S.profiles[p.profile] || !view(p.profile)) continue;
    const t = target(p.profile, p.subject);
    t.track.push({ ts: p.ts, lat: p.lat, lon: p.lon, alt: p.alt, speed: p.speed, track: p.track });
    t.last = Math.max(t.last, p.ts);
  }
  sums.forEach((sum, i) => {
    const profile = S.mapProfiles[i];
    for (const e of sum.subjects) {
      const t = target(profile, e.subject);
      t.label = e.labels && e.labels.length ? e.labels[e.labels.length - 1] : t.label;
      t.kind = e.kinds && e.kinds.length ? e.kinds.join(', ') : t.kind;
      t.fields = e.fields || {};
      t.rows = e.rows;
      t.last = Math.max(t.last, e.last);
    }
  });
  for (const t of S.targets.values()) t.track.sort((a, b) => a.ts - b.ts);
  if (S.mode === 'history') {
    const cov = await api(`/api/coverage?since=${S.since}&until=${S.until}`);
    drawBands(cov.coverage);
  }
  S.cursor = S.mode === 'live' ? NOW() : S.until;
  if (!S.fitted) fit();
  render();
}

function onMessage(m) {
  if (S.mode !== 'live') return;
  if (m.type === 'position' && view(m.profile || '') !== undefined && S.profiles[m.profile]) {
    const t = target(m.profile, m.subject);
    t.track.push({ ts: m.ts, lat: m.lat, lon: m.lon, alt: m.alt, speed: m.speed, track: m.track });
    t.last = Math.max(t.last, m.ts);
    S.dirty = true;
  } else if (m.type === 'row' && S.profiles[m.profile] && view(m.profile) && m.subject) {
    const t = target(m.profile, m.subject);
    if (m.label) t.label = m.label;
    t.kind = m.kind;
    Object.assign(t.fields, m.fields || {});
    t.last = Math.max(t.last, m.ts_end || m.ts);
    S.dirty = true;
  }
}

function tick() {
  paintClock(S.status ? S.status.now + (NOW() - S.statusAt) : NOW());
  if (S.mode === 'live') {
    S.cursor = NOW();
    S.since = NOW() - Number($('trail').value);
    prune(S.targets, S.since - 60);
    $('t-label').textContent = `${utc(S.cursor)} UTC`;
    $('t-sub').textContent = 'now, updating';
    render();
  }
}

// --- Modes ---------------------------------------------------------------------------------

function setMode(mode) {
  S.mode = mode;
  $('m-live').setAttribute('aria-pressed', String(mode === 'live'));
  $('m-hist').setAttribute('aria-pressed', String(mode === 'history'));
  $('trail-wrap').hidden = mode !== 'live';
  $('hist-range').hidden = mode !== 'history';
  $('scrub').hidden = mode !== 'history';
  stopPlay();
  if (mode === 'live') return loadRange(NOW() - Number($('trail').value), null);
  if (!$('h-from').value) {
    $('h-from').value = fmtUtc(NOW() - 3600);
    $('h-to').value = fmtUtc(NOW());
  }
  return loadHistory();
}

// Opened from a row on another screen (/map?at=&profile=&subject=): History
// for the half hour either side, the cursor at that moment, the target
// picked and centred.
async function openAt(at, profile, subject) {
  $('h-from').value = fmtUtc(at - 1800);
  // Times are typed to the minute; an empty "to" is now, so a moment in the
  // last half hour isn't cut off.
  $('h-to').value = at + 1800 < NOW() ? fmtUtc(at + 1800) : '';
  await setMode('history');
  if (!(S.until > S.since)) return;
  S.cursor = Math.min(Math.max(at, S.since), S.until);
  $('cursor').value = String(Math.round(((S.cursor - S.since) / (S.until - S.since)) * 1000));
  renderCursorLabel();
  const key = targetKey(profile, subject);
  const t = S.targets.get(key);
  if (t) {
    S.sel = key;
    const p = positionAt(t.track, S.cursor) || t.track[t.track.length - 1];
    if (p) S.map.setView([p.lat, p.lon], Math.max(S.map.getZoom(), 9));
  }
  render();
}

async function loadHistory() {
  try {
    const since = parseUtc($('h-from').value);
    const until = $('h-to').value.trim() ? parseUtc($('h-to').value) : NOW();
    if (since >= until) throw new Error('"From" must be before "to".');
    if (until - since > 7 * 86400) throw new Error('Choose at most 7 days.');
    await loadRange(since, until);
    $('cursor').value = '1000';
    renderCursorLabel();
  } catch (e) {
    showError(e.message);
  }
}

function renderCursorLabel() {
  $('t-label').textContent = `${utc(S.cursor)} UTC`;
  // Long ranges come back thinned (one fix per target per step seconds).
  const thin = S.step ? ` · a fix per ${S.step >= 60 ? `${S.step / 60} min` : `${S.step} s`}${S.truncated ? ', cut short' : ''}` : '';
  $('t-sub').textContent = new Date(S.cursor * 1000).toISOString().slice(0, 10) + thin;
}

function togglePlay() {
  if (S.playing) return stopPlay();
  if (Number($('cursor').value) >= 1000) $('cursor').value = '0';
  $('h-play').setAttribute('aria-pressed', 'true');
  $('h-play').textContent = 'Pause';
  // About a minute to play the whole range, whatever its length.
  S.playing = setInterval(() => {
    const v = Math.min(1000, Number($('cursor').value) + 2);
    $('cursor').value = String(v);
    S.cursor = S.since + (v / 1000) * (S.until - S.since);
    renderCursorLabel();
    render();
    if (v >= 1000) stopPlay();
  }, 120);
}

function stopPlay() {
  if (S.playing) clearInterval(S.playing);
  S.playing = null;
  $('h-play').setAttribute('aria-pressed', 'false');
  $('h-play').textContent = 'Play';
}

function drawBands(coverage) {
  const box = $('sbands');
  clear(box);
  const span = S.until - S.since;
  for (const c of coverage) {
    const l = ((Math.max(c.from, S.since) - S.since) / span) * 100;
    const w = ((Math.min(c.to, S.until) - Math.max(c.from, S.since)) / span) * 100;
    if (w <= 0) continue;
    const el = h('span', { class: c.state === 'running' ? '' : 'stopped', '--pc': color(c.profile), title: `${c.profile || c.state} ${utc(c.from, false)}–${utc(c.to, false)}` }, w > 6 ? c.profile || c.state : '');
    el.style.left = `${l}%`;
    el.style.width = `${w}%`;
    box.append(el);
  }
  const ticks = $('sticks');
  clear(ticks);
  for (let i = 0; i <= 4; i++) {
    const t = h('span', {}, utc(S.since + (span * i) / 4, false));
    t.style.left = `${(i / 4) * 100}%`;
    if (i === 0) t.style.transform = 'none';
    if (i === 4) t.style.transform = 'translateX(-100%)';
    ticks.append(t);
  }
}

// --- Drawing -------------------------------------------------------------------------------

function buildLayerChips() {
  const box = $('layers');
  clear(box);
  if (!S.mapProfiles.length) {
    box.append(h('span', { class: 'muted' }, 'No profile declares a map view.'));
    return;
  }
  for (const p of S.mapProfiles) {
    const n = [...S.targets.values()].filter((t) => t.profile === p && positionAt(t.track, S.cursor, HIDE_S)).length;
    const chip = h('button', { type: 'button', class: 'tchip', '--pc': color(p), 'aria-pressed': String(S.on.has(p)) }, h('span', { class: 'sw' }), p, h('span', { class: 'n' }, `${n}`));
    chip.addEventListener('click', () => {
      if (S.on.has(p)) S.on.delete(p);
      else S.on.add(p);
      render();
    });
    box.append(chip);
  }
}

function icon(t, pos, stale) {
  const hdg = heading(pos, t.fields, role(t.profile, 'heading'));
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('viewBox', '0 0 18 18');
  svg.setAttribute('aria-hidden', 'true');
  const shape = document.createElementNS(SVG, hdg == null ? 'circle' : 'path');
  if (hdg == null) {
    shape.setAttribute('cx', '9');
    shape.setAttribute('cy', '9');
    shape.setAttribute('r', '4.5');
  } else {
    shape.setAttribute('d', 'M9 1 L14.5 16 L9 12.5 L3.5 16 Z');
    shape.setAttribute('transform', `rotate(${Math.round(hdg)} 9 9)`);
  }
  shape.setAttribute('fill', 'currentColor');
  shape.setAttribute('stroke', '#05080f');
  shape.setAttribute('stroke-width', '1');
  svg.append(shape);
  const alt = pos.alt ?? t.fields[role(t.profile, 'alt')];
  const sub = [alt != null && alt !== '' ? `${Math.round(alt)} ft` : null, pos.speed != null ? `${Math.round(pos.speed)} kn` : null].filter(Boolean).join(' · ');
  const label = h('span', { class: 'tl2' }, t.label || t.subject, sub ? h('small', {}, sub) : null);
  const cls = `mk${stale ? ' stale' : ''}${S.sel === t.key ? ' sel' : ''}${$('o-labels').checked ? '' : ' nolabel'}`;
  const el = h('div', { class: cls, '--pc': color(t.profile) }, h('span', { class: 'ico' }, svg), label);
  return L.divIcon({ html: el, className: '', iconSize: [0, 0] });
}

function render() {
  const showTracks = $('o-tracks').checked;
  S.layers.tracks.clearLayers();
  const seen = new Set();
  for (const t of S.targets.values()) {
    if (!S.on.has(t.profile)) {
      if (t.marker) {
        S.layers.markers.removeLayer(t.marker);
        t.marker = null;
      }
      continue;
    }
    const pos = positionAt(t.track, S.cursor, HIDE_S);
    if (!pos) {
      if (t.marker) {
        S.layers.markers.removeLayer(t.marker);
        t.marker = null;
      }
      continue;
    }
    seen.add(t.key);
    const stale = S.cursor - pos.ts > STALE_S;
    const ic = icon(t, pos, stale);
    if (!t.marker) {
      t.marker = L.marker([pos.lat, pos.lon], { icon: ic, keyboard: false, title: t.label || t.subject });
      t.marker.on('click', () => select(t.key));
      S.layers.markers.addLayer(t.marker);
    } else {
      t.marker.setLatLng([pos.lat, pos.lon]);
      t.marker.setIcon(ic);
    }
    if (showTracks) {
      const pts = trackWindow(t.track, S.cursor - Number($('trail').value || 1800), S.cursor).map((p) => [p.lat, p.lon]);
      if (pts.length > 1) S.layers.tracks.addLayer(L.polyline(pts, { color: color(t.profile), weight: S.sel === t.key ? 2.5 : 1.5, opacity: stale ? 0.3 : 0.65, interactive: false }));
    }
  }
  buildLayerChips();
  drawRings();
  renderList();
  renderSel();
  S.dirty = false;
  const n = [...S.targets.values()].length;
  $('mapnote').textContent = n
    ? 'Targets are logged only while their profile runs; in a rotation each disappears between its slots. Faded: no fix for over 5 minutes.'
    : 'Nothing with a position in this time. Positions are logged from 2026-11-02, while a profile with a map view runs.';
}

function fit() {
  const pts = [];
  for (const t of S.targets.values()) for (const p of t.track) pts.push([p.lat, p.lon]);
  if (S.registry.receiver) pts.push(S.registry.receiver);
  if (!pts.length) {
    // Nothing logged yet: show the base map's busiest part, if there is one.
    const b = S.registry.basemap;
    const box = b && (b.core || b.bounds);
    if (box) S.map.fitBounds([[box[1], box[0]], [box[3], box[2]]]);
    return;
  }
  S.map.fitBounds(L.latLngBounds(pts).pad(0.08), { maxZoom: 11 });
  S.fitted = true;
}

// A graticule, so the map has a scale of its own without a basemap.
function drawGrid() {
  S.layers.grid.clearLayers();
  const b = S.map.getBounds();
  const span = Math.max(b.getEast() - b.getWest(), b.getNorth() - b.getSouth());
  const steps = [0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30];
  const step = steps.find((s) => span / s <= 12) || 30;
  const style = { color: '#2a3647', weight: 1, opacity: S.registry.basemap ? 0.45 : 0.8, interactive: false };
  for (let lat = Math.ceil(b.getSouth() / step) * step; lat <= b.getNorth(); lat += step) {
    S.layers.grid.addLayer(L.polyline([[lat, b.getWest() - span], [lat, b.getEast() + span]], style));
  }
  for (let lon = Math.ceil(b.getWest() / step) * step; lon <= b.getEast(); lon += step) {
    S.layers.grid.addLayer(L.polyline([[b.getSouth() - span, lon], [b.getNorth() + span, lon]], style));
  }
}

function loadBasemap() {
  addBasemap(S.layers.base, S.registry.basemap); // none on the server: the graticule alone
}

function drawRings() {
  S.layers.rings.clearLayers();
  const r = S.registry.receiver;
  $('rings-wrap').hidden = !r;
  if (!r || !$('o-rings').checked) return;
  for (const km of [25, 50, 100, 200]) {
    S.layers.rings.addLayer(L.circle(r, { radius: km * 1000, color: '#3a4858', weight: 1, fill: false, interactive: false, dashArray: '4 6' }));
  }
  S.layers.rings.addLayer(L.circleMarker(r, { radius: 4, color: '#e8eef5', weight: 1.5, fillOpacity: 0, interactive: false }));
}

// --- Lists and the selected target ----------------------------------------------------------

function describe(t, pos) {
  const bits = [t.kind];
  const alt = pos.alt ?? t.fields[role(t.profile, 'alt')];
  if (alt != null && alt !== '') bits.push(`${Math.round(alt)} ft`);
  const spd = pos.speed ?? t.fields[role(t.profile, 'speed')];
  if (spd != null && spd !== '') bits.push(`${Math.round(spd)} kn`);
  return bits.filter(Boolean).join(' · ');
}

function renderList() {
  const box = $('tlist');
  clear(box);
  const bounds = S.map.getBounds();
  const rows = [];
  for (const t of S.targets.values()) {
    if (!S.on.has(t.profile)) continue;
    const pos = positionAt(t.track, S.cursor, HIDE_S);
    if (!pos || !bounds.contains([pos.lat, pos.lon])) continue;
    rows.push({ t, pos });
  }
  rows.sort((a, b) => b.pos.ts - a.pos.ts);
  $('n-view').textContent = `${rows.length}`;
  for (const { t, pos } of rows.slice(0, 200)) {
    const stale = S.cursor - pos.ts > STALE_S;
    const b = h(
      'button',
      { type: 'button', class: `tli${stale ? ' stale' : ''}${S.sel === t.key ? ' on' : ''}`, '--pc': color(t.profile) },
      h('span', { class: 'sw' }),
      h('span', { class: 'nm' }, t.label || t.subject),
      h('span', { class: 'ag' }, ago(pos.ts, S.cursor)),
      h('span', { class: 'ds' }, `${t.profile} · ${t.label ? `${t.subject} · ` : ''}${describe(t, pos)}`),
    );
    b.addEventListener('click', () => select(t.key));
    box.append(b);
  }
  if (!rows.length) box.append(h('div', { class: 'empty' }, 'Nothing to show for this time, these layers and this part of the map.'));
}

function select(key) {
  S.sel = key;
  render();
}

function fmtValue(v) {
  if (v == null) return '—';
  if (Array.isArray(v)) return v.join(', ');
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

function renderSel() {
  const t = S.sel && S.targets.get(S.sel);
  $('sel').hidden = !t;
  if (!t) return;
  // The last fix at or before the cursor, however old; none if the target
  // was first seen after it (History mode).
  const pos = positionAt(t.track, S.cursor);
  $('sel').style.setProperty('--pc', color(t.profile));
  $('s-profile').textContent = t.profile;
  $('s-kind').textContent = t.kind || '';
  $('s-ident').textContent = t.label ? `${t.label} · ${t.subject}` : t.subject;
  const age = pos ? S.cursor - pos.ts : null;
  $('s-stale').hidden = pos ? !(age > STALE_S) : false;
  $('s-stale').textContent = pos ? `no fix · ${ago(pos.ts, S.cursor)}` : 'no fix yet at this time';
  const alts = trackWindow(t.track, S.since, S.cursor).filter((p) => p.alt != null);
  $('s-altbox').hidden = alts.length < 2;
  if (alts.length >= 2) {
    const svg = $('s-alt');
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    const a0 = Math.min(...alts.map((p) => p.alt));
    const a1 = Math.max(...alts.map((p) => p.alt));
    const t0 = alts[0].ts;
    const t1 = alts[alts.length - 1].ts;
    const line = document.createElementNS(SVG, 'polyline');
    line.setAttribute('points', alts.map((p) => `${((p.ts - t0) / Math.max(1, t1 - t0)) * 120},${38 - ((p.alt - a0) / Math.max(1, a1 - a0)) * 36}`).join(' '));
    line.setAttribute('fill', 'none');
    line.setAttribute('stroke', 'var(--pc)');
    line.setAttribute('stroke-width', '1.5');
    line.setAttribute('vector-effect', 'non-scaling-stroke');
    svg.append(line);
    const lab = $('s-altlab');
    clear(lab);
    lab.append(h('span', {}, `${utc(t0, false)} · ${Math.round(a0)} ft`), h('span', { class: 'sp' }), h('span', {}, `${Math.round(a1)} ft · ${utc(t1, false)}`));
  }
  const kv = $('s-fields');
  clear(kv);
  if (pos) {
    kv.append(h('dt', {}, 'position'), h('dd', { class: 'mono' }, `${pos.lat.toFixed(5)}, ${pos.lon.toFixed(5)}`));
    kv.append(h('dt', {}, 'fix'), h('dd', { class: 'mono' }, `${utc(pos.ts)} UTC (${ago(pos.ts, S.cursor)})`));
    if (pos.speed != null) kv.append(h('dt', {}, 'speed'), h('dd', { class: 'mono' }, `${pos.speed} kn`));
    if (pos.track != null) kv.append(h('dt', {}, 'track'), h('dd', { class: 'mono' }, `${pos.track}°`));
  }
  // Speed and heading are shown from the fix when it has them.
  const shown = new Set(['lat', 'lon', 'message']);
  if (pos && pos.speed != null) shown.add(role(t.profile, 'speed'));
  if (pos && pos.track != null) shown.add(role(t.profile, 'heading'));
  for (const [k, v] of Object.entries(t.fields)) {
    if (shown.has(k)) continue;
    kv.append(h('dt', {}, k), h('dd', { class: 'mono' }, fmtValue(v)));
  }
  const q = new URLSearchParams({ subject: t.subject, profiles: t.profile, around: String(Math.round(pos ? pos.ts : t.last)) });
  $('s-log').setAttribute('href', `/log?${q}`);
}

init().catch((e) => showError(`Could not load: ${e.message}`));
