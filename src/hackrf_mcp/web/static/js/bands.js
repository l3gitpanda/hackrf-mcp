// The Bands screen: search the band plan and the official allocations by
// name, service, FCC part or frequency, and open a band in the Live screen's
// "Watch a range". The search is kept in the address (?q=).

import { api } from './api.js';
import { paintAppBar, paintClock } from './appbar.js';
import { loadOfficial, loadPlan, rangeText, serviceText } from './bandstrip.js';
import { search } from './bandsearch.js';
import { assignColors, clear, h, mhz } from './util.js';

const $ = (id) => document.getElementById(id);
const NOW = () => Date.now() / 1000;

const S = { plan: null, official: null, watch: null, colors: {}, cats: new Set(), timer: null };

async function init() {
  const [registry, plan, official] = await Promise.all([api('/api/registry'), loadPlan(), loadOfficial()]);
  S.colors = assignColors(registry.profiles);
  S.watch = registry.watch || null;
  S.plan = plan;
  S.official = official;
  if (!plan) throw new Error('the band plan could not be loaded');
  $('about').textContent = plan.about;
  if (official) $('off-src').textContent = `${official.source}, revised ${official.revised}. ${official.about}`;
  buildCats();
  const u = new URLSearchParams(location.search);
  $('q').value = u.get('q') || '';
  for (const c of (u.get('cats') || '').split(',')) if (plan.categories[c]) S.cats.add(c);
  syncCats();
  $('q').addEventListener('input', () => {
    clearTimeout(S.timer);
    S.timer = setTimeout(render, 120);
  });
  render();
  const poll = async () => {
    try {
      const st = await api('/api/status');
      paintAppBar(st.live, (n) => S.colors[n] || '#8a96a6', 'open');
    } catch (e) {
      paintAppBar(null, () => '#8a96a6', 'closed');
    }
  };
  poll();
  setInterval(poll, 5000);
  setInterval(() => paintClock(NOW()), 1000);
}

function buildCats() {
  const box = $('cats');
  for (const [key, c] of Object.entries(S.plan.categories)) {
    const b = h('button', { type: 'button', class: 'tchip', 'aria-pressed': 'false', 'data-cat': key, '--pc': c.color }, h('span', { class: 'sw' }), c.label);
    b.addEventListener('click', () => {
      if (S.cats.has(key)) S.cats.delete(key);
      else S.cats.add(key);
      syncCats();
      render();
    });
    box.append(b);
  }
}

function syncCats() {
  for (const b of $('cats').querySelectorAll('[data-cat]')) b.setAttribute('aria-pressed', String(S.cats.has(b.dataset.cat)));
}

// A link that fills the Live screen's "Watch a range" with this band (only
// what the HackRF receives; the Live screen clips anything over 10 MHz).
function watchLink(lo, hi) {
  const w = S.watch;
  if (!w || hi <= w.min_hz || lo >= w.max_hz) return null;
  const a = Math.max(lo, w.min_hz);
  const b = Math.min(hi, w.max_hz);
  const wide = b - a > w.rates_hz[w.rates_hz.length - 1];
  const q = new URLSearchParams({ watch_from: String(a / 1e6), watch_to: String(b / 1e6) });
  return h('a', { class: 'btn sm', href: `/?${q}`, title: wide ? 'Wider than one watch: opens its start, scroll for the rest' : 'Fill in Watch a range on the Live screen' }, wide ? 'Watch the start' : 'Watch');
}

function render() {
  const q = $('q').value;
  const r = search(S.plan, S.official, q, S.cats);
  const params = new URLSearchParams();
  if (q.trim()) params.set('q', q.trim());
  if (S.cats.size) params.set('cats', [...S.cats].join(','));
  history.replaceState(null, '', `/bands${params.toString() ? `?${params}` : ''}`);
  const list = $('plan');
  clear(list);
  // At a frequency, the most specific band first (NOAA Weather Radio before
  // the 12 MHz federal band around it).
  const bands = r.freq ? [...r.bands].sort((a, b) => a.hi - a.lo - (b.hi - b.lo)) : r.bands;
  for (const b of bands) {
    const cat = S.plan.categories[b.cat] || {};
    const row = h(
      'div',
      { class: 'bres', role: 'listitem', '--bc': cat.color || '#8a96a6' },
      h('span', { class: 'sw' }),
      h(
        'div',
        { class: 'bmain' },
        h('div', {}, h('b', {}, b.name), h('span', { class: 'mono muted' }, ` ${rangeText(b.lo, b.hi)}`)),
        h('div', { class: 'muted sm' }, [cat.label, b.part ? `FCC part ${b.part}` : '', b.note].filter(Boolean).join(' · ')),
      ),
      watchLink(b.lo, b.hi),
    );
    list.append(row);
  }
  if (!r.bands.length) list.append(h('p', { class: 'empty' }, 'No band in the plan matches.'));
  $('n-plan').textContent = `${r.bands.length} of ${S.plan.bands.length}`;
  const off = $('off');
  clear(off);
  $('p-off').hidden = !r.official.length;
  for (const b of r.official.slice(0, 200)) {
    off.append(
      h(
        'div',
        { class: 'bres off', role: 'listitem' },
        h('span', { class: 'tag' }, b.col),
        h(
          'div',
          { class: 'bmain' },
          h('div', {}, h('span', { class: 'mono' }, rangeText(b.lo, b.hi)), ' ', b.services.length ? b.services.map(serviceText).join(', ') : h('span', { class: 'muted' }, 'no allocation')),
          b.parts && b.parts.length ? h('div', { class: 'muted sm' }, `FCC rule parts: ${b.parts.join(', ')}`) : null,
        ),
        watchLink(b.lo, b.hi),
      ),
    );
  }
  $('n-off').textContent = r.official.length > 200 ? `first 200 of ${r.official.length}` : `${r.official.length}`;
  const f = r.freq;
  $('count').textContent = f ? (f.lo === f.hi ? `at ${mhz(f.lo, 4)} MHz` : `overlapping ${rangeText(f.lo, f.hi)}`) : '';
}

init().catch((e) => {
  $('count').textContent = `Could not load: ${e.message}`;
});
