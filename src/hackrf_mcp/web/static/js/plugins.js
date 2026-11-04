// The Plugins screen: what the station can decode (from the registries the
// protocol modules fill), whether each program is installed, and the
// operator's panel files with any that didn't load. Read-only.

import { api } from './api.js';
import { paintAlerts, paintAppBar, paintClock } from './appbar.js';
import { assignColors, clear, h, mhz } from './util.js';
import { panelQueryText } from './panelutil.js';

const $ = (id) => document.getElementById(id);
const NOW = () => Date.now() / 1000;
const S = { cat: null, colors: {}, status: null, statusAt: 0 };
const color = (n) => S.colors[n] || '#8a96a6';
const hasMap = (n) => ((S.cat.profiles.find((p) => p.name === n) || {}).views || []).some((v) => v.name === 'map');

async function init() {
  S.cat = await api('/api/plugins');
  S.colors = assignColors(S.cat.profiles);
  renderProfiles();
  renderDecoders();
  renderPanels();
  pollStatus();
  setInterval(pollStatus, 10000);
  setInterval(() => paintClock(S.status ? S.status.now + (NOW() - S.statusAt) : NOW()), 1000);
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

const status = (ok, missing) =>
  h('span', { class: `pst ${ok ? 'ok' : 'miss'}` }, h('span', { class: 'dot' }), ok ? 'installed' : `missing ${missing}`);

function tuning(p) {
  if (!p.sample_rate_hz) return 'its program tunes itself';
  const center = p.center_hz ? `${mhz(p.center_hz, 3)} MHz` : 'centred on its channels';
  const chans = p.default_freqs_hz.length ? ` · ${p.default_freqs_hz.map((f) => mhz(f, 3)).join(', ')}` : p.needs_freqs ? ' · channels given at start' : '';
  return `${center} · ${(p.sample_rate_hz / 1e6).toFixed(1)} MS/s${chans}`;
}

function renderProfiles() {
  const tb = clear($('profiles'));
  $('prof-count').textContent = `${S.cat.profiles.length} · ${S.cat.profiles.filter((p) => !p.installed).length} missing a program`;
  for (const p of S.cat.profiles) {
    const views = ['map', 'table', 'timeline', 'waterfall'].map((v) => {
      const on = v === 'waterfall' ? p.spectrum : (p.views || []).some((x) => x.name === v);
      return h('span', { class: `vchip${on ? '' : ' off'}`, title: on ? `has a ${v} view` : `no ${v} view` }, v);
    });
    tb.append(
      h(
        'tr',
        { '--pc': color(p.name) },
        h('td', {}, h('span', { class: 'pbadge' }, p.name)),
        h('td', { class: 'wc' }, p.summary, p.redacted ? h('span', { class: 'tag' }, 'metadata only') : null),
        h('td', { class: 'mono' }, p.programs.join(' or ')),
        h('td', {}, status(p.installed, p.programs[0])),
        h('td', { class: 'mono sm' }, tuning(p)),
        h('td', { class: 'mono sm' }, (p.summary_kinds || []).join(', ') || '—'),
        h('td', {}, h('span', { class: 'row wr vchips' }, views)),
        h('td', { class: 'mono sm' }, h('span', { class: 'sw pswatch' }), ` ${color(p.name)} `, h('span', { class: 'muted' }, p.color ? 'its own' : 'from the palette')),
      ),
    );
  }
}

function renderDecoders() {
  const tb = clear($('decoders'));
  $('dec-count').textContent = `${S.cat.decoders.length} · ${S.cat.decoders.filter((d) => !d.installed).length} not installed`;
  for (const d of S.cat.decoders) {
    tb.append(
      h(
        'tr',
        {},
        h('td', { class: 'mono' }, d.name),
        h('td', { class: 'wc' }, d.summary),
        h('td', { class: 'mono' }, d.programs.join(' or ')),
        h('td', {}, status(d.installed, d.programs[0])),
      ),
    );
  }
}

function renderPanels() {
  const tb = clear($('panels'));
  const { panels, panel_errors: errors } = S.cat;
  $('pan-dir').textContent = S.cat.panels_dir;
  $('pan-count').textContent = `${panels.length} loaded${errors.length ? ` · ${errors.length} with an error` : ''}`;
  if (!panels.length) tb.append(h('tr', {}, h('td', { colspan: '5', class: 'muted' }, 'No panels yet. The example below shows how to add one.')));
  for (const p of panels) {
    tb.append(
      h(
        'tr',
        {},
        h('td', {}, h('b', {}, p.title), h('div', { class: 'mono muted sm' }, p.id)),
        h('td', { class: 'mono sm' }, p.slot),
        h('td', { class: 'mono sm wc' }, p.profiles.map((n) => h('span', { class: 'pbadge', '--pc': color(n) }, n)), ' ', panelQueryText(p)),
        h('td', { class: 'mono sm' }, p.render),
        h('td', { class: 'mono sm' }, p.file),
      ),
    );
  }
  const box = clear($('pan-errors'));
  box.hidden = !errors.length;
  for (const e of errors) box.append(h('div', { class: 'ctl-err perr' }, h('span', { class: 'mono' }, e.file), `: ${e.error}`));
}

init().catch((e) => {
  $('perr').hidden = false;
  $('perr').textContent = `Could not read the catalogue: ${e.message}`;
});
