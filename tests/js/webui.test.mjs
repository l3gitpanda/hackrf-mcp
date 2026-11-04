// Pure functions of the web UI, run by tests/test_web_static.py with
// `node --test` when node is installed.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PALETTE, assignColors, decodeRow, fmtUtc, mhz, mhzShort, parseFreqs, parseUtc, quantile, rotation, utc } from '../../src/hackrf_mcp/web/static/js/util.js';
import { AutoLevel, makeLut, resample, ticks } from '../../src/hackrf_mcp/web/static/js/waterfall.js';
import { bandsAt, layoutBands, officialAt, rangeText, serviceText } from '../../src/hackrf_mcp/web/static/js/bandstrip.js';
import { clampCenter, leftText, parseMhz, scrolled, watchTuning } from '../../src/hackrf_mcp/web/static/js/watchutil.js';
import { parseQuery, search } from '../../src/hackrf_mcp/web/static/js/bandsearch.js';
import { channelLevels, dayBounds, dayOf, gapsIn, pathOf, stepAt, stepsOf, timeTicks } from '../../src/hackrf_mcp/web/static/js/historyutil.js';

test('profiles get their own colour, else the next free palette colour, in registry order', () => {
  const c = assignColors([{ name: 'adsb' }, { name: 'ais', color: PALETTE[0] }, { name: 'acars' }]);
  assert.equal(c.ais, PALETTE[0]);
  assert.equal(c.adsb, PALETTE[1]);
  assert.equal(c.acars, PALETTE[2]);
});

test('frequencies and times', () => {
  assert.equal(mhz(1090000000), '1090.0000');
  assert.equal(mhzShort(161975000), '161.975');
  assert.equal(mhzShort(1090000000), '1090');
  assert.deepEqual(parseFreqs('931.75, 931.9375'), [931750000, 931937500]);
  assert.deepEqual(parseFreqs(''), []);
  assert.throws(() => parseFreqs('931.75 abc'), /not a frequency/);
  assert.equal(utc(0), '00:00:00');
  assert.equal(utc(3661, false), '01:01');
});

test('UTC times typed into the Log search', () => {
  assert.equal(parseUtc('2026-11-02 18:00'), Date.UTC(2026, 10, 2, 18, 0) / 1000);
  assert.equal(parseUtc('2026-11-02T18:00:30Z'), Date.UTC(2026, 10, 2, 18, 0, 30) / 1000);
  assert.equal(parseUtc('2026-11-02'), Date.UTC(2026, 10, 2) / 1000);
  assert.throws(() => parseUtc('yesterday'), /not a UTC time/);
  assert.equal(fmtUtc(parseUtc('2026-11-02 18:05')), '2026-11-02 18:05');
});

test('spectrum rows decode on their scale', () => {
  const b64 = Buffer.from([0, 100, 255]).toString('base64');
  assert.deepEqual(Array.from(decodeRow(b64, -130, 0.5)), [-130, -80, -2.5]);
  assert.equal(quantile([5, 1, 3, 2, 4], 0.5), 3);
});

test('the rotation knows its current step and when it ends', () => {
  const sched = [{ profile: 'adsb', minutes: 10 }, { profile: 'ais', minutes: 10 }];
  const r = rotation(sched, 0, 60 * 25);
  assert.equal(r.current.profile, 'adsb');
  assert.equal(r.current.to, 60 * 30);
  assert.equal(r.fraction, 0.25);
  assert.equal(rotation([], 0, 1), null);
});

test('resampling keeps narrow peaks and averages means', () => {
  const v = new Float32Array(512).fill(-90);
  v[300] = -20;
  const px = resample(v, 100, 'max');
  assert.equal(px.length, 100);
  assert.equal(Math.max(...px), -20);
  const wide = resample(Float32Array.from([1, 3]), 4, 'mean');
  assert.deepEqual(Array.from(wide), [1, 1, 3, 3]);
});

test('the colour scale follows the noise floor', () => {
  const lvl = new AutoLevel(50, 6);
  for (let i = 0; i < 100; i++) lvl.update(new Float32Array(512).fill(-56));
  assert.ok(Math.abs(lvl.lo - -62) < 0.5);
  assert.ok(Math.abs(lvl.hi - -12) < 0.5);
  const hot = new AutoLevel(50, 6);
  hot.update(new Float32Array(512).fill(-30));
  assert.equal(hot.hi, 0);  // never above full scale
  assert.equal(hot.lo, -36);
  const lut = makeLut();
  assert.equal(lut.length, 768);
  assert.deepEqual(Array.from(lut.slice(0, 3)), [5, 8, 15]);
});

test('frequency ticks are round and inside the band', () => {
  const t = ticks(1090000000, 2400000);
  assert.ok(t.length >= 5 && t.length <= 9);
  for (const x of t) assert.ok(x.pct >= 0 && x.pct <= 100);
  assert.ok(t.some((x) => x.label === '1090.0'));
  const ais = ticks(162000000, 2000000);
  assert.ok(ais.some((x) => x.label === '161.25'), JSON.stringify(ais.map((x) => x.label)));
});

import { heading, positionAt, prune, targetKey, trackWindow } from '../../src/hackrf_mcp/web/static/js/maputil.js';

test('the map finds each target where it was at the cursor', () => {
  const track = [{ ts: 10, lat: 1 }, { ts: 20, lat: 2 }, { ts: 30, lat: 3 }];
  assert.equal(positionAt(track, 25).lat, 2);
  assert.equal(positionAt(track, 30).lat, 3);
  assert.equal(positionAt(track, 5), null);
  assert.equal(positionAt(track, 100, 60), null); // too old
  assert.equal(positionAt([], 1), null);
  assert.deepEqual(trackWindow(track, 15, 30).map((p) => p.ts), [20, 30]);
  assert.equal(targetKey('p', 's'), 'p|s');
});

test('heading comes from the fix, else the declared field', () => {
  assert.equal(heading({ track: 90 }, { course: 10 }, 'course'), 90);
  assert.equal(heading({ track: null }, { course: 10 }, 'course'), 10);
  assert.equal(heading({}, { course: 360 }, 'course'), null); // AIS "not available"
  assert.equal(heading({}, {}, undefined), null);
});

test('a live map forgets fixes older than its window', () => {
  const targets = new Map([
    ['a', { track: [{ ts: 1 }, { ts: 50 }], last: 50 }],
    ['b', { track: [{ ts: 2 }], last: 2 }],
  ]);
  prune(targets, 10);
  assert.deepEqual([...targets.keys()], ['a']);
  assert.deepEqual(targets.get('a').track.map((p) => p.ts), [50]);
});

test('history days are UTC days', () => {
  assert.deepEqual(dayBounds('2026-11-03'), [1793664000, 1793750400]);
  assert.equal(dayBounds('11/03/2026'), null);
  assert.equal(dayOf(1793664000), '2026-11-03');
  assert.equal(dayOf(1793750399.5), '2026-11-03');
});

test('the steps of a day are its spectrum segments, clipped, the last one running to now', () => {
  const segs = [
    { id: 2, profile: 'b', ts_start: 1600, ts_end: 2200 },
    { id: 1, profile: 'a', ts_start: 900, ts_end: 1600 },
    { id: 3, profile: 'a', ts_start: 2200, ts_end: null },
    { id: 0, profile: 'a', ts_start: 100, ts_end: 900 },
  ];
  const steps = stepsOf(segs, 1000, 3000, 2500);
  assert.deepEqual(steps.map((s) => [s.id, s.a, s.b, s.live]), [[1, 1000, 1600, false], [2, 1600, 2200, false], [3, 2200, 2500, true]]);
  assert.equal(stepAt(steps, 1700), 1);
  assert.equal(stepAt(steps, 2900), 2);
  assert.equal(stepAt([], 2900), -1);
});

test('gaps are the stretches of coverage that are not running, clipped to the step', () => {
  const cov = [
    { state: 'running', profile: 'a', from: 0, to: 100 },
    { state: 'paused', profile: null, from: 100, to: 130 },
    { state: 'running', profile: 'a', from: 130, to: 500 },
    { state: 'error', profile: 'a', from: 500, to: 700 },
  ];
  assert.deepEqual(gapsIn(cov, 50, 600).map((g) => [g.state, g.a, g.b]), [['paused', 100, 130], ['error', 500, 600]]);
  assert.match(gapsIn(cov, 50, 600)[0].label, /paused/);
});

test('channel level is the strongest bin near each channel, the floor the 20th percentile', () => {
  // 10 bins over 100-110 Hz; a channel at 103 Hz, one outside the band.
  const row = Float32Array.from([-90, -91, -92, -40, -93, -94, -95, -96, -97, -98]);
  const lv = channelLevels([row], 105, 10, [103.5, 200], 0.6);
  assert.deepEqual(lv.channels, [103.5]);
  assert.deepEqual(lv.levels, [[-40]]);
  assert.equal(lv.floor[0], -97);
});

test('chart paths lift the pen across gaps and clamp to the box', () => {
  const d = pathOf([0, 1, 2, 10, 11], [-50, -60, NaN, -200, -50], 0, 11, -40, -100, 110, 60, 3);
  assert.equal(d, 'M0.0,10.0 L10.0,20.0 M100.0,60.0 L110.0,10.0');
});

test('time ticks pick a step that keeps their number down', () => {
  const t = timeTicks(0, 600, 10);
  assert.equal(t.step, 60);
  assert.equal(t.ticks.length, 11 - 0);
  assert.equal(timeTicks(0, 86400, 10).step, 10800);
});

test('band plan lanes: broadest on top, overlaps pushed down, the rest listed but off the strip', () => {
  const bands = [
    { lo: 960, hi: 1215, name: 'wide' },
    { lo: 1089, hi: 1091, name: 'narrow' },
    { lo: 1000, hi: 1100, name: 'mid' },
    { lo: 1090, hi: 1090.5, name: 'tiny' },
    { lo: 2000, hi: 2100, name: 'away' },
  ];
  const { bands: out, lanes } = layoutBands(bands, 1080, 1100, 3);
  const by = Object.fromEntries(out.map((b) => [b.name, b]));
  assert.equal(lanes, 3);
  assert.equal(by.wide.lane, 0);
  assert.equal(by.mid.lane, 1);
  assert.equal(by.narrow.lane, 2);
  assert.equal(by.tiny.lane, null);
  assert.equal(by.away, undefined);
  assert.equal(by.wide.left, 0);
  assert.equal(by.wide.width, 100);
  assert.equal(by.narrow.left, 45);
  assert.equal(by.narrow.width, 10);
  assert.deepEqual(bandsAt(bands, 1090.2).map((b) => b.name), ['tiny', 'narrow', 'mid', 'wide']);
  assert.deepEqual(bandsAt(bands, 1215), []);
});

test('band ranges read in the unit that suits them', () => {
  assert.equal(rangeText(162400000, 162550000), '162.4–162.55 MHz');
  assert.equal(rangeText(535000, 1705000), '535–1705 kHz');
  assert.equal(rangeText(2400000000, 2483500000), '2.4–2.4835 GHz');
});

test('the official table: the band at a frequency, and primary or secondary from the printed case', () => {
  const rows = [{ lo: 88e6, hi: 108e6, services: ['BROADCASTING'] }, { lo: 108e6, hi: 117.975e6, services: ['AERONAUTICAL RADIONAVIGATION'] }];
  assert.equal(officialAt(rows, 98e6).services[0], 'BROADCASTING');
  assert.equal(officialAt(rows, 108e6).lo, 108e6);
  assert.equal(officialAt(rows, 120e6), null);
  assert.equal(serviceText('MOBILE except aeronautical mobile'), 'MOBILE except aeronautical mobile');
  assert.equal(serviceText('AERONAUTICAL MOBILE (R)'), 'AERONAUTICAL MOBILE (R)');
  assert.equal(serviceText('Mobile except aeronautical mobile'), 'Mobile except aeronautical mobile (secondary)');
  assert.equal(serviceText('Radiolocation'), 'Radiolocation (secondary)');
});

test('watching a range: the narrowest width that covers it, scrolling by half widths, inside 1 MHz-6 GHz', () => {
  const rates = [2e6, 5e6, 8e6, 10e6];
  assert.equal(parseMhz('88.5'), 88500000);
  assert.equal(parseMhz('162,4'), 162400000);
  assert.ok(Number.isNaN(parseMhz('fm')));
  assert.deepEqual(watchTuning(88e6, 98e6, rates, 1e6, 6e9), { center_hz: 93e6, sample_rate_hz: 10e6, clipped: false });
  assert.deepEqual(watchTuning(162.4e6, 162.55e6, rates, 1e6, 6e9), { center_hz: 162475000, sample_rate_hz: 2e6, clipped: false });
  assert.deepEqual(watchTuning(88e6, 108e6, rates, 1e6, 6e9), { center_hz: 93e6, sample_rate_hz: 10e6, clipped: true });
  assert.deepEqual(watchTuning(1.5e6, 1.5e6, rates, 1e6, 6e9), { center_hz: 2e6, sample_rate_hz: 2e6, clipped: false });
  assert.equal(scrolled(93e6, 10e6, 1, 1e6, 6e9), 98e6);
  assert.equal(scrolled(93e6, 10e6, -1, 1e6, 6e9), 88e6);
  assert.equal(scrolled(5.998e9, 10e6, 1, 1e6, 6e9), 5.995e9);
  assert.equal(clampCenter(1e6, 2e6, 1e6, 6e9), 2e6);
  assert.equal(leftText(660), '11 min left');
  assert.equal(leftText(40), '40 s left');
  assert.equal(leftText(-1), 'ending');
});

test('band search: frequencies, ranges and words', () => {
  assert.deepEqual(parseQuery('162.4'), { freq: { lo: 162400000, hi: 162400000 }, words: [] });
  assert.deepEqual(parseQuery('1.09 GHz'), { freq: { lo: 1090000000, hi: 1090000000 }, words: [] });
  assert.deepEqual(parseQuery('535 kHz'), { freq: { lo: 535000, hi: 535000 }, words: [] });
  assert.deepEqual(parseQuery('88-108 broadcast'), { freq: { lo: 88e6, hi: 108e6 }, words: ['broadcast'] });
  assert.deepEqual(parseQuery('144 to 148 MHz'), { freq: { lo: 144e6, hi: 148e6 }, words: [] });
  assert.deepEqual(parseQuery('Marine VHF'), { freq: null, words: ['marine', 'vhf'] });
  assert.deepEqual(parseQuery('part 97').words, ['97']);
  const plan = {
    categories: { marine: { label: 'Marine' }, amateur: { label: 'Amateur radio' } },
    bands: [
      { lo: 144e6, hi: 148e6, name: '2 m amateur', cat: 'amateur', part: '97' },
      { lo: 156e6, hi: 157.45e6, name: 'Marine VHF', cat: 'marine', part: '80', note: 'Ship and coast stations' },
    ],
  };
  const official = {
    non_federal: [{ lo: 144e6, hi: 146e6, services: ['AMATEUR', 'AMATEUR-SATELLITE'], parts: ['Amateur Radio (97)'] }],
    federal: [{ lo: 144e6, hi: 148e6, services: [] }],
  };
  assert.deepEqual(search(plan, official, 'ship').bands.map((b) => b.name), ['Marine VHF']);
  assert.deepEqual(search(plan, official, 'part 97').bands.map((b) => b.name), ['2 m amateur']);
  assert.deepEqual(search(plan, official, '97').bands, []); // a bare number is a frequency: 97 MHz
  assert.deepEqual(search(plan, official, '145').bands.map((b) => b.name), ['2 m amateur']);
  assert.deepEqual(search(plan, official, '145').official.map((b) => b.col), ['Non-Federal']);
  assert.deepEqual(search(plan, official, 'amateur-satellite').official.length, 1);
  assert.deepEqual(search(plan, official, '', new Set(['marine'])).bands.map((b) => b.name), ['Marine VHF']);
  assert.equal(search(plan, official, '').official.length, 0);
});

import {
  areaPath, binAlpha, binLabels, binOf, bucketsFor, cellFor, columnsOf, dropEchoes, durText, fieldText, gridImage, heatBins,
  kindsText, mercY, numericCols, onAir, rangeOf, runsOf, tickTimes,
} from '../../src/hackrf_mcp/web/static/js/summutil.js';

test('summary periods: back from now, today since UTC midnight, or typed', () => {
  const now = Date.UTC(2026, 10, 3, 21, 14) / 1000;
  assert.deepEqual(rangeOf('3600', now), [now - 3600, now]);
  assert.deepEqual(rangeOf('today', now), [Date.UTC(2026, 10, 3) / 1000, now]);
  assert.deepEqual(rangeOf('custom', now, '2026-11-02 18:00', '2026-11-02 20:00'), [Date.UTC(2026, 10, 2, 18) / 1000, Date.UTC(2026, 10, 2, 20) / 1000]);
  assert.deepEqual(rangeOf('custom', now, '2026-11-02 18:00', ' '), [Date.UTC(2026, 10, 2, 18) / 1000, now]);
  assert.throws(() => rangeOf('custom', now, '2026-11-02 20:00', '2026-11-02 18:00'), /before/);
  assert.throws(() => rangeOf('soon', now), /unknown range/);
});

test('rate buckets are round widths, about 120 at most', () => {
  assert.equal(bucketsFor(86400), 96); // 15 minutes, as the mockup
  assert.equal(bucketsFor(3600), 60);
  assert.equal(bucketsFor(7 * 86400), 84); // 2 hours
  assert.equal(bucketsFor(30), 1);
});

test('time on the air per profile and in gaps, clipped to the period', () => {
  const cov = [
    { state: 'down', profile: null, from: 0, to: 100 },
    { state: 'running', profile: 'p1', from: 100, to: 400 },
    { state: 'paused', profile: null, from: 400, to: 430 },
    { state: 'running', profile: 'p2', from: 430, to: 1000 },
  ];
  assert.deepEqual(onAir(cov, 50, 900), { profiles: { p1: 300, p2: 470 }, gaps: { down: 50, paused: 30 } });
  assert.deepEqual(runsOf(cov, 'p1', 0, 1000, 100), [[10, 30]]);
  assert.deepEqual(runsOf(cov, 'p2', 500, 1000, 100), [[0, 100]]);
  assert.deepEqual(runsOf(cov, 'nope', 0, 1000, 100), []);
});

test('durations read short', () => {
  assert.equal(durText(0), '0 m');
  assert.equal(durText(30), '30 s');
  assert.equal(durText(8 * 60), '8 m');
  assert.equal(durText(7 * 3600 + 20), '7 h 0 m');
  assert.equal(durText(2 * 86400 + 3 * 3600), '2 d 3 h');
});

test('rate areas step per bucket from the baseline; time labels keep their distance', () => {
  assert.equal(areaPath([0, 10], 100, 30, 10), 'M0,30 L0.0,30.0 L50.0,30.0 L50.0,2.0 L100.0,2.0 L100,30 Z');
  assert.equal(areaPath([], 100, 30, 0), '');
  const day = [0, 86400];
  const wide = tickTimes(...day, 1200, 60, timeTicks);
  assert.equal(wide[0], 0);
  assert.equal(wide[wide.length - 1], 86400);
  assert.ok(wide.length > 4 && wide.every((t, i) => !i || (t - wide[i - 1]) / 86400 * 1200 >= 60));
  const narrow = tickTimes(...day, 300, 60, timeTicks);
  assert.ok(narrow.length < wide.length && narrow.every((t, i) => !i || (t - narrow[i - 1]) / 86400 * 300 >= 60));
  assert.deepEqual(tickTimes(...day, 100, 60, timeTicks), [0, 86400]);
});

test('summary columns come from the table view, without position and counts', () => {
  assert.deepEqual(columnsOf({ views: [{ name: 'table', fields: ['callsign', 'lat', 'lon', 'speed', 'messages'] }] }), ['callsign', 'speed']);
  assert.deepEqual(columnsOf({ views: [{ name: 'map', fields: [] }], summary_fields: ['type', 'positions'] }), ['type']);
  const top = [{ labels: ['UAL1'], fields: { callsign: 'UAL1', squawk: '1200' } }, { labels: [], fields: { callsign: '' } }];
  assert.deepEqual(dropEchoes(['callsign', 'squawk', 'speed'], top), ['squawk', 'speed']);
  assert.equal(kindsText({ vessel: 2, 'aid-to-navigation': 3, 'base-station': 1 }), 'aid to navigation 3 · vessel 2 · base station 1');
  // Rows the decoder couldn't type further carry the profile's name: "other", last.
  assert.equal(kindsText({ p1: 9, vessel: 2 }, 'p1'), 'vessel 2 · other 9');
  const tops = [{ fields: { speed: 12, name: 'X' } }, { fields: { speed: null, name: 'Y' } }];
  assert.deepEqual([...numericCols(['name', 'speed', 'none'], tops)], ['speed']);
  assert.equal(fieldText(12500), '12,500');
  assert.equal(fieldText(3.14159), '3.1');
  assert.equal(fieldText(['a', 'b']), 'a, b');
  assert.equal(fieldText(''), '—');
});

test('heat map cells follow the zoom, classes are log-spaced whole numbers', () => {
  assert.equal(cellFor(2), 5);
  assert.equal(cellFor(8), 0.05);
  assert.equal(cellFor(18), 0.002);
  const edges = heatBins(1000);
  assert.equal(edges[edges.length - 1], 1000);
  assert.ok(edges.every((e, i) => !i || e > edges[i - 1]) && edges.length <= 7);
  assert.deepEqual(heatBins(1), [1]);
  assert.deepEqual(heatBins(3), [1, 2, 3]);
  assert.equal(binOf(1, edges), 0);
  assert.equal(binOf(1000, edges), edges.length - 1);
  assert.deepEqual(binLabels([1, 3, 9]), ['1', '2–3', '4–9']);
  assert.ok(binAlpha(0, 6) < binAlpha(5, 6) && binAlpha(5, 6) <= 0.9);
});

test('a grid in degrees lands on a Mercator image Leaflet can stretch', () => {
  const cell = 0.5;
  const cells = [[2 * 180, 2 * 90, 1, 1], [2 * 180 + 3, 2 * 90 + 1, 1, 1]]; // lon 0..2, lat 0..1
  const img = gridImage(cells, cell, 400);
  assert.deepEqual([img.west, img.east, img.south, img.north], [0, 2, 0, 1]);
  assert.equal(img.width, 64); // 4 columns at 16 pixels
  // Mercator near the equator is nearly square: 1 degree tall, 2 wide.
  assert.ok(Math.abs(img.height - 32) <= 1);
  const [x, y, w, h] = img.rect(2 * 180, 2 * 90);
  assert.deepEqual([x, w], [0, 16]);
  assert.equal(y + h, img.height); // the southern row sits at the foot
  const [, y2, , h2] = img.rect(2 * 180 + 3, 2 * 90 + 1);
  assert.equal(y2, 0); // the northern row at the top
  assert.equal(y2 + h2, y); // and the rows meet
  const wide = gridImage([[0, 0, 1, 1], [999, 0, 1, 1]], 0.01, 400);
  assert.equal(wide.width, 400); // a thousand columns: under a pixel each, capped
  assert.equal(gridImage([], cell), null);
  assert.ok(mercY(60) - mercY(59) > mercY(1) - mercY(0)); // rows grow toward the poles
});

import { draftOf, fieldsFor, hitWhen, parseValues, repeatText, ruleBody, sameRule } from '../../src/hackrf_mcp/web/static/js/alertutil.js';
import { alertKeys, hitLinks, sameKeys } from '../../src/hackrf_mcp/web/static/js/appbar.js';

test('alert rules: values typed as text, fields for the chosen profiles, drafts round-trip', () => {
  assert.deepEqual(parseValues('7500, 7600\n7700,, '), ['7500', '7600', '7700']);
  const by = { p1: ['subject', 'label', 'squawk', 'callsign'], p2: ['subject', 'kind', 'name'] };
  assert.deepEqual(fieldsFor(by, ['p1']), ['subject', 'label', 'callsign', 'squawk']);
  assert.deepEqual(fieldsFor(by, []), ['subject', 'label', 'kind', 'callsign', 'name', 'squawk']);
  const rule = { id: 3, name: ' Emergency ', enabled: true, profiles: ['p1'], conditions: [{ field: 'squawk', op: 'any_of', values: ['7500', '7700'] }], webhook: true, repeat_s: 600 };
  const d = draftOf(rule);
  assert.equal(d.conditions[0].values, '7500, 7700');
  assert.deepEqual(ruleBody(d), { name: 'Emergency', enabled: true, profiles: ['p1'], conditions: [{ field: 'squawk', op: 'any_of', values: ['7500', '7700'] }], webhook: true, repeat_s: 600 });
  assert.ok(sameRule(d, draftOf(rule)));
  assert.ok(!sameRule(d, { ...d, repeat_s: 0 }));
  assert.equal(repeatText(0), 'every hit');
  assert.equal(repeatText(3600), '1 hour');
  assert.equal(repeatText(1200), '20 min');
});

test('a hit links to its rows in the log and its moment on the map', () => {
  const l = hitLinks({ ts: 1000, profile: 'p1', subject: 'A1' });
  assert.equal(l.log, '/log?since=400&until=1600&subject=A1&profiles=p1');
  assert.equal(l.map, '/map?at=1000&profile=p1&subject=A1');
  const day = Date.UTC(2026, 10, 4, 12) / 1000;
  assert.equal(hitWhen(day - 60, day), '11:59:00');
  assert.equal(hitWhen(day - 86400, day), '11-03 12:00');
});

test('subjects with an unacknowledged hit, keyed as the Map keys targets', () => {
  const k = alertKeys({ active: [{ profile: 'p1', subject: 'A1' }, { profile: 'p2', subject: '42' }] });
  assert.ok(k.has('p1|A1') && k.has('p2|42') && k.size === 2);
  assert.equal(alertKeys(null).size, 0);
  assert.ok(sameKeys(k, new Set(['p2|42', 'p1|A1'])));
  assert.ok(!sameKeys(k, new Set(['p1|A1'])));
});

import { keyHeading, panelColumns, panelQueryText, windowText } from '../../src/hackrf_mcp/web/static/js/panelutil.js';

test('panel files described and laid out', () => {
  const p = { kinds: ['aircraft'], fields: ['squawk', 'callsign'], filter: 'squawk is any of 7700', group_by: 'squawk', sort: 'rows', limit: 5, window_s: 3600 };
  assert.equal(panelQueryText(p), 'aircraft · squawk, callsign · where squawk is any of 7700 · by squawk, most rows first, 5 · last 1 h');
  assert.equal(panelQueryText({ ...p, filter: null, window_s: null, sort: 'last' }), "aircraft · squawk, callsign · by squawk, newest first, 5 · the screen's period");
  assert.deepEqual(panelColumns(p), ['callsign']);
  assert.equal(keyHeading(p), 'squawk');
  assert.equal(keyHeading({ group_by: 'subject' }), 'Subject');
  assert.equal(windowText(86400 * 7), '7 d');
  assert.equal(windowText(1800), '30 min');
});
