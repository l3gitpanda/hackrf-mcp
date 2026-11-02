// Pure functions of the web UI, run by tests/test_web_static.py with
// `node --test` when node is installed.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PALETTE, assignColors, decodeRow, mhz, mhzShort, parseFreqs, quantile, rotation, utc } from '../../src/hackrf_mcp/web/static/js/util.js';
import { AutoLevel, makeLut, resample, ticks } from '../../src/hackrf_mcp/web/static/js/waterfall.js';

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
