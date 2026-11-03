// Pure functions of the web UI, run by tests/test_web_static.py with
// `node --test` when node is installed.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PALETTE, assignColors, decodeRow, fmtUtc, mhz, mhzShort, parseFreqs, parseUtc, quantile, rotation, utc } from '../../src/hackrf_mcp/web/static/js/util.js';
import { AutoLevel, makeLut, resample, ticks } from '../../src/hackrf_mcp/web/static/js/waterfall.js';
import { bandsAt, layoutBands, rangeText } from '../../src/hackrf_mcp/web/static/js/bandstrip.js';
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
