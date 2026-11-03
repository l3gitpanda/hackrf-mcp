// The band plan strip: coloured bands along the top of a waterfall, saying
// which service a stretch of spectrum belongs to. The plan is data
// (/static/bandplan-us.json), not code. Bands overlap as a rule (902-928 MHz
// is ISM and amateur at once; 1090 MHz sits inside 960-1215 MHz), so they're
// laid out in lanes, broadest on top; pointing at the strip (or tapping it)
// lists everything at that frequency, and under it the official allocation
// from the FCC's Table of Frequency Allocations (/static/fcc-us-allocations.json).

import { clear, h, mhz } from './util.js';

let planPromise = null;
let officialPromise = null;

export function loadPlan(url = '/static/bandplan-us.json') {
  if (!planPromise) {
    planPromise = fetch(url, { credentials: 'same-origin' })
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null);
  }
  return planPromise;
}

export function loadOfficial(url = '/static/fcc-us-allocations.json') {
  if (!officialPromise) {
    officialPromise = fetch(url, { credentials: 'same-origin' })
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null);
  }
  return officialPromise;
}

// The official table's band holding f in one column (the bands tile the
// spectrum, so there is at most one), or null.
export function officialAt(rows, f) {
  return rows.find((b) => b.lo <= f && f < b.hi) || null;
}

// "Mobile" -> "Mobile (secondary)": the table prints a primary allocation's
// first word in capitals ("MOBILE except aeronautical mobile") and a
// secondary one's in mixed case.
export function serviceText(name) {
  return /[a-z]/.test(name.split(' ')[0]) ? `${name} (secondary)` : name;
}

// The bands overlapping [lo, hi], each with a lane (0 at the top) and its
// position in percent of the view. Broader bands get the upper lanes; a band
// that fits in none of `maxLanes` gets lane null (left off the strip, still
// listed under the pointer).
export function layoutBands(bands, lo, hi, maxLanes = 3) {
  const span = hi - lo;
  const visible = bands
    .filter((b) => b.hi > lo && b.lo < hi)
    .sort((a, b) => b.hi - b.lo - (a.hi - a.lo) || a.lo - b.lo);
  const lanes = [];
  const out = [];
  for (const b of visible) {
    let lane = lanes.findIndex((l) => l.every(([a, c]) => b.hi <= a || b.lo >= c));
    if (lane < 0 && lanes.length < maxLanes) {
      lanes.push([]);
      lane = lanes.length - 1;
    }
    if (lane < 0) {
      out.push({ ...b, lane: null });
      continue;
    }
    lanes[lane].push([b.lo, b.hi]);
    const a = Math.max(b.lo, lo);
    const c = Math.min(b.hi, hi);
    out.push({ ...b, lane, left: ((a - lo) / span) * 100, width: ((c - a) / span) * 100 });
  }
  return { bands: out, lanes: lanes.length };
}

// Every band holding frequency f, narrowest (most specific) first.
export function bandsAt(bands, f) {
  return bands.filter((b) => b.lo <= f && f < b.hi).sort((a, b) => a.hi - a.lo - (b.hi - b.lo));
}

// "162.4000–162.5500 MHz", or kHz/GHz where that reads better.
export function rangeText(lo, hi) {
  const span = Math.max(lo, hi);
  if (span >= 1e9) return `${(lo / 1e9).toFixed(4).replace(/\.?0+$/, '')}–${(hi / 1e9).toFixed(4).replace(/\.?0+$/, '')} GHz`;
  if (span < 3e6) return `${(lo / 1e3).toFixed(1).replace(/\.0$/, '')}–${(hi / 1e3).toFixed(1).replace(/\.0$/, '')} kHz`;
  return `${(lo / 1e6).toFixed(4).replace(/\.?0+$/, '')}–${(hi / 1e6).toFixed(4).replace(/\.?0+$/, '')} MHz`;
}

const LANE_PX = 15;

export class BandStrip {
  // `root` is an empty element the strip fills; `opts.lanes` caps the lanes.
  constructor(root, opts = {}) {
    this.root = root;
    this.maxLanes = opts.lanes || 3;
    this.lo = null;
    this.hi = null;
    this.plan = null;
    this.root.classList.add('bstrip');
    this.box = h('div', { class: 'blanes', role: 'img' });
    this.tip = h('div', { class: 'btip', hidden: true });
    this.root.append(this.box, this.tip);
    this.box.addEventListener('pointermove', (e) => this.point(e));
    this.box.addEventListener('pointerdown', (e) => this.point(e));
    this.box.addEventListener('pointerleave', (e) => {
      if (e.pointerType === 'mouse') this.tip.hidden = true;
    });
    document.addEventListener('pointerdown', (e) => {
      if (!this.root.contains(e.target)) this.tip.hidden = true;
    });
  }

  async show(lo, hi) {
    this.lo = lo;
    this.hi = hi;
    if (!this.plan) [this.plan, this.official] = await Promise.all([loadPlan(), loadOfficial()]);
    if (this.lo !== lo || this.hi !== hi) return; // a newer view came in meanwhile
    this.draw();
  }

  draw() {
    clear(this.box);
    this.tip.hidden = true;
    const plan = this.plan;
    if (!plan || this.lo == null || !(this.hi > this.lo)) {
      this.root.hidden = true;
      return;
    }
    const { bands, lanes } = layoutBands(plan.bands, this.lo, this.hi, this.maxLanes);
    this.root.hidden = false;
    this.box.style.height = `${Math.max(1, lanes) * LANE_PX}px`;
    const names = [];
    for (const b of bands) {
      if (b.lane == null) continue;
      const cat = plan.categories[b.cat] || {};
      const seg = h('div', { class: 'bseg' }, h('span', {}, b.name));
      seg.style.left = `${b.left}%`;
      seg.style.width = `${b.width}%`;
      seg.style.top = `${b.lane * LANE_PX}px`;
      seg.style.setProperty('--bc', cat.color || '#8a96a6');
      this.box.append(seg);
      names.push(b.name);
    }
    if (!lanes) {
      this.box.append(h('span', { class: 'bnone' }, 'No band in the plan here'));
    }
    this.box.setAttribute('aria-label', names.length ? `Bands: ${names.join(', ')}` : 'No band in the plan here');
  }

  point(e) {
    if (!this.plan || this.lo == null) return;
    const r = this.box.getBoundingClientRect();
    const x = Math.min(Math.max(0, e.clientX - r.left), r.width);
    const f = this.lo + (x / Math.max(1, r.width)) * (this.hi - this.lo);
    const here = bandsAt(this.plan.bands, f);
    clear(this.tip);
    this.tip.append(h('b', { class: 'mono' }, `${mhz(f, 4)} MHz`));
    if (!here.length) this.tip.append(h('div', { class: 'muted' }, 'No band in the plan here'));
    for (const b of here) {
      const cat = this.plan.categories[b.cat] || {};
      const row = h(
        'div',
        { class: 'brow' },
        h('span', { class: 'sw' }),
        h('span', {}, h('b', {}, b.name), ` · ${rangeText(b.lo, b.hi)}`, b.part ? ` · FCC part ${b.part}` : '', h('br'), h('span', { class: 'muted' }, [cat.label, b.note].filter(Boolean).join(' · '))),
      );
      row.style.setProperty('--bc', cat.color || '#8a96a6');
      this.tip.append(row);
    }
    this.tip.append(h('div', { class: 'muted bsrc' }, `${this.plan.name} band plan (${this.plan.updated}); approximate edges`));
    const off = this.official;
    if (off) {
      const nf = officialAt(off.non_federal, f);
      const fed = officialAt(off.federal, f);
      const sec = h('div', { class: 'boff' }, h('b', {}, 'Official allocation'));
      const line = (label, b) => {
        if (!b) return;
        const what = b.services.length ? b.services.map(serviceText).join(', ') : 'no allocation';
        sec.append(h('div', {}, h('span', { class: 'muted' }, `${label} ${rangeText(b.lo, b.hi)}: `), what));
        if (b.parts && b.parts.length) sec.append(h('div', { class: 'muted' }, `FCC rule parts: ${b.parts.join(', ')}`));
      };
      line('Non-Federal', nf);
      line('Federal', fed);
      sec.append(h('div', { class: 'muted bsrc' }, `${off.source}, revised ${off.revised}`));
      this.tip.append(sec);
    }
    this.tip.hidden = false;
    // Keep the tip inside the strip's width, next to the pointer.
    const tw = Math.min(360, r.width);
    this.tip.style.width = `${tw}px`;
    this.tip.style.left = `${Math.min(Math.max(0, x - tw / 2), r.width - tw)}px`;
  }
}
