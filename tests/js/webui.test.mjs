// Pure functions of the web UI, run by tests/test_web_static.py with
// `node --test` when node is installed.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PALETTE, assignColors, decodeRow, fmtUtc, mhz, mhzShort, parseFreqs, parseUtc, quantile, rotation, stepName, utc } from '../../src/hackrf_mcp/web/static/js/util.js';
import { AutoLevel, binAt, freqAt, freqText, makeLut, resample, ticks } from '../../src/hackrf_mcp/web/static/js/waterfall.js';
import { bandsAt, layoutBands, officialAt, rangeText, serviceText } from '../../src/hackrf_mcp/web/static/js/bandstrip.js';
import { bandList, bandsInView, clampCenter, leftText, parseMhz, scrolled, watchTuning } from '../../src/hackrf_mcp/web/static/js/watchutil.js';
import { parseQuery, search } from '../../src/hackrf_mcp/web/static/js/bandsearch.js';
import { channelLevels, dayBounds, dayOf, gapsIn, pathOf, stepAt, stepsOf, timeTicks } from '../../src/hackrf_mcp/web/static/js/historyutil.js';
import { compass, destination, fmtKm, levelChart, outline } from '../../src/hackrf_mcp/web/static/js/rangeutil.js';
import { RANGES, bytesOf, cellAt, cellLevel, freqTicks, groupUnusual, levels, logHref, paint, rowMap, scheduleText, spanOf, watchHref, zoomRange } from '../../src/hackrf_mcp/web/static/js/occutil.js';
import { captureBody, captureHref, captureQuery, pickRange, rangeLabel, rangePct, rateText, sizeText, stateText, stateTone, ticked } from '../../src/hackrf_mcp/web/static/js/captureutil.js';
import {
  AUDIO, DROPPED, FLAG_OPEN, HEADER_BYTES, LISTEN, METER, Resampler, ULAW, channelBody, channelName, defaultMode, isClip, meterPct, nextStart, parseFrame, parseWav,
  planText, sameChannels, snapHz, squelchText, ulawDecode, withChannel, withListen, withoutChannel,
} from '../../src/hackrf_mcp/web/static/js/listenutil.js';

test('profiles get their own colour, else the next free palette colour, in registry order', () => {
  const c = assignColors([{ name: 'adsb' }, { name: 'ais', color: PALETTE[0] }, { name: 'acars' }]);
  assert.equal(c.ais, PALETTE[0]);
  assert.equal(c.adsb, PALETTE[1]);
  assert.equal(c.acars, PALETTE[2]);
});

test('the sixth profile colour is no longer the fifth one under colour blindness', () => {
  assert.equal(PALETTE[4], '#8fd46a');
  assert.equal(PALETTE[5], '#fff27a');
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

test('the pointer reads out the exact frequency and its bin', () => {
  const band = { center_hz: 907150000, sample_rate_hz: 2000000 };
  assert.equal(freqAt(band, 0), 906150000);
  assert.equal(freqAt(band, 1), 908150000);
  assert.equal(freqText(freqAt(band, 0.3625)), '906.8750 MHz'); // LongFast's slot
  assert.equal(freqAt(band, -0.2), 906150000); // held to the band
  assert.equal(binAt(512, 0), 0);
  assert.equal(binAt(512, 0.5), 256);
  assert.equal(binAt(512, 1), 511);
});

import { heading, insertFix, positionAt, primaryProfile, prune, reportSource, targetKey, trackRuns, trackWindow } from '../../src/hackrf_mcp/web/static/js/maputil.js';

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

test('the band list: what the HackRF can tune, lowest first, filtered by words or a frequency', () => {
  const plan = {
    categories: { ism: { label: 'ISM', color: '#aaa' }, amateur: { label: 'Amateur', color: '#bbb' }, broadcast: { label: 'Broadcast', color: '#ccc' } },
    bands: [
      { lo: 902e6, hi: 928e6, name: '33 cm amateur', cat: 'amateur', part: '97' },
      { lo: 902e6, hi: 928e6, name: 'ISM 915 MHz', cat: 'ism', part: '18', note: 'LoRa, Meshtastic' },
      { lo: 535e3, hi: 1705e3, name: 'AM broadcast', cat: 'broadcast' },
      { lo: 88e6, hi: 108e6, name: 'FM broadcast', cat: 'broadcast' },
      { lo: 5.85e9, hi: 7.1e9, name: 'Beyond', cat: 'ism' },
    ],
  };
  const all = bandList(plan, '', 1e6, 6e9);
  // AM broadcast reaches into the tunable range; everything is lowest first.
  assert.deepEqual(all.map((b) => b.name), ['AM broadcast', 'FM broadcast', '33 cm amateur', 'ISM 915 MHz', 'Beyond']);
  assert.equal(all[3].label, 'ISM');
  assert.deepEqual(bandList(plan, 'meshtastic', 1e6, 6e9).map((b) => b.name), ['ISM 915 MHz']);
  assert.deepEqual(bandList(plan, 'part 97', 1e6, 6e9).map((b) => b.name), ['33 cm amateur']);
  assert.deepEqual(bandList(plan, '915', 1e6, 6e9).map((b) => b.name), ['33 cm amateur', 'ISM 915 MHz']);
  assert.deepEqual(bandList(plan, 'zzz', 1e6, 6e9), []);
  assert.deepEqual(bandsInView(all, 906.15e6, 908.15e6), [2, 3]);
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

import { aircraftName, aircraftQuery, aircraftWanted, areaPath, binAlpha, binLabels, binOf, bucketsFor, cellFor, columnsOf, dropEchoes, durText, fieldText, gridImage, heatBins, kindsText, mercY, numericCols, onAir, rangeOf, runsOf, tickTimes } from '../../src/hackrf_mcp/web/static/js/summutil.js';

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

import { aircraftToAsk, draftOf, fieldsFor, hasChoice, hitWhen, parseValues, repeatText, ruleBody, sameRule, toggleChoice } from '../../src/hackrf_mcp/web/static/js/alertutil.js';
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

import { declutter, labelBox, labelRank } from '../../src/hackrf_mcp/web/static/js/maputil.js';

test('map labels: boxes from the text, the important placed first, overlaps left out', () => {
  const b = labelBox('UAL123', '35000 ft · 450 kn', 100, 50);
  assert.equal(b.x, 111);
  assert.equal(b.y, 42);
  assert.equal(b.h, 26);
  assert.ok(b.w > 17 * 6);
  assert.equal(labelBox('A', '', 0, 0).h, 14);
  const sel = labelRank({ selected: true, ts: 1 });
  const alerted = labelRank({ alerted: true, ts: 2e9 });
  const moving = labelRank({ moving: true, fresh: true, ts: 1 });
  const old = labelRank({ fresh: false, ts: 2e9 });
  assert.ok(sel > alerted && alerted > moving && moving > old);
  assert.ok(labelRank({ ts: 2e9 }) > labelRank({ ts: 1e9 })); // newer first
  // Three labels on top of each other and one apart: the highest of the
  // three stays, the other two go; the far one stays.
  const boxes = [
    { key: 'a', x: 0, y: 0, w: 60, h: 14, rank: 1 },
    { key: 'b', x: 10, y: 5, w: 60, h: 14, rank: 3 },
    { key: 'c', x: 20, y: 8, w: 60, h: 14, rank: 2 },
    { key: 'd', x: 300, y: 300, w: 60, h: 14, rank: 0 },
  ];
  assert.deepEqual([...declutter(boxes)].sort(), ['a', 'c']);
  // Side by side with a gap wider than the padding: both stay.
  assert.equal(declutter([{ key: 'x', x: 0, y: 0, w: 50, h: 14, rank: 1 }, { key: 'y', x: 60, y: 0, w: 50, h: 14, rank: 0 }]).size, 0);
  assert.equal(declutter([]).size, 0);
});

test('the range outline: points at a distance and bearing, an arc per sector, the receiver where nothing was heard', () => {
  const rx = [45, 10];
  // 111.2 km north is one degree of latitude; due east stays near the same latitude.
  const [n0, n1] = destination(45, 10, 0, 111.195);
  assert.ok(Math.abs(n0 - 46) < 1e-3 && Math.abs(n1 - 10) < 1e-9);
  const [e0, e1] = destination(45, 10, 90, 50);
  assert.ok(Math.abs(e0 - 45) < 0.02 && e1 > 10.6 && e1 < 10.7);
  // Across the antimeridian the longitude wraps.
  assert.ok(destination(0, 179.9, 90, 50)[1] < -179);
  const kms = [100, null, 50, 0];
  const pts = outline(rx, kms, 90);
  assert.equal(pts.length, 3 + 1 + 3 + 1);
  assert.deepEqual(pts[3], rx);
  assert.deepEqual(pts[7], rx);
  // Sector 0's arc runs from 0 to 90 degrees at 100 km.
  assert.ok(Math.abs(pts[0][0] - destination(45, 10, 0, 100)[0]) < 1e-12);
  assert.deepEqual(pts[2], destination(45, 10, 90, 100));
  assert.deepEqual(pts[4], destination(45, 10, 180, 50));
});

test('compass points and distances in km and miles', () => {
  assert.equal(compass(0), 'N');
  assert.equal(compass(22.5), 'NNE');
  assert.equal(compass(359), 'N');
  assert.equal(compass(-90), 'W');
  assert.equal(compass(200), 'SSW');
  assert.equal(fmtKm(312.4), '312 km (194 mi)');
  assert.equal(fmtKm(8.04), '8.0 km (5.0 mi)');
  assert.equal(fmtKm(null), '—');
});

test('the signal-against-distance chart: a 10-90% band, the median line and tidy ticks', () => {
  assert.equal(levelChart([], 25), null);
  assert.equal(levelChart([[0, 0, -5, -5, -5]], 25), null);
  const g = levelChart([[0, 3, -8, -6, -5], [100, 2, -33, -31, -30]], 25, 300, 140, [34, 6, 6, 18]);
  // Bins are drawn at their middles: 12.5 and 112.5 km, on an axis to 125 km.
  assert.deepEqual(g.xticks.map((t) => t.v), [0, 25, 50, 75, 100, 125]);
  assert.equal(g.dots.length, 2);
  assert.ok(Math.abs(g.dots[0].x - (34 + (12.5 / 125) * 260)) < 1e-9);
  assert.ok(g.yticks[0].v <= -33 && g.yticks[g.yticks.length - 1].v >= -5);
  // Louder is higher on the chart.
  assert.ok(g.dots[0].y < g.dots[1].y);
  assert.ok(g.band.startsWith('M') && g.band.endsWith('Z') && g.median.split('L').length === 2);
  assert.deepEqual(g.box, { left: 34, top: 6, right: 294, bottom: 122 });
});

test('a field with a fixed set of values: choices toggle in the value list by value or label', () => {
  const mil = { value: 'military', label: 'Military' };
  const law = { value: 'law_enforcement', label: 'Law enforcement' };
  assert.equal(toggleChoice('', mil, false), 'military');
  assert.equal(toggleChoice('military', law, false), 'military, law_enforcement');
  assert.equal(toggleChoice('military, law_enforcement', mil, false), 'law_enforcement');
  assert.equal(hasChoice('Law Enforcement', law), true); // a typed label counts
  assert.equal(toggleChoice('Law enforcement, military', law, false), 'military');
  assert.equal(toggleChoice('military', law, true), 'law_enforcement'); // 'equals' keeps one
  assert.equal(toggleChoice('military', mil, true), '');
  assert.equal(hasChoice('', mil), false);
});

test('aircraft to look up: rows of ICAO-address profiles, once each, with the callsign', () => {
  const profiles = { air: { subject_type: 'icao24' }, ships: { subject_type: null } };
  const rows = [
    { profile: 'air', subject: 'a1b2c3', label: 'UAL1 ' },
    { profile: 'air', subject: 'A1B2C3', label: 'UAL1' },
    { profile: 'air', subject: 'ADF7C9', label: null },
    { profile: 'air', subject: 'N123AB' }, // not an address
    { profile: 'ships', subject: '366999999' },
    { profile: 'air', subject: 'C0FFEE' },
  ];
  assert.deepEqual(aircraftToAsk(rows, profiles, new Map([['C0FFEE', null]])), ['A1B2C3:UAL1', 'ADF7C9']);
  assert.deepEqual(aircraftToAsk([], profiles, new Map()), []);
});

test('one aircraft heard by several profiles is one target, by its address', () => {
  const profiles = { live: { subject_type: 'icao24' }, dl: { subject_type: 'icao24', positions_reported: true }, tails: { subject_type: 'registration', positions_reported: true }, ships: {} };
  assert.equal(targetKey('live', 'a1b2c3', profiles), 'icao24|A1B2C3');
  assert.equal(targetKey('dl', 'A1B2C3', profiles), 'icao24|A1B2C3');
  assert.equal(targetKey('dl', '10916A0', profiles), 'dl|10916A0'); // not an address
  assert.equal(targetKey('ships', '366999999', profiles), 'ships|366999999');
  assert.equal(targetKey('tails', 'N12345', profiles), 'tails|N12345'); // until its address is known
  assert.equal(targetKey('tails', 'N12345', profiles, new Map([['tails|N12345', 'A0B1C2']])), 'icao24|A0B1C2');
  assert.equal(targetKey('live', 'A1B2C3'), 'live|A1B2C3'); // without the registry, as before
  // The first profile is the target's own, unless it only reports and the next is heard live.
  assert.equal(primaryProfile('dl', 'live', profiles), 'live');
  assert.equal(primaryProfile('live', 'dl', profiles), 'live');
  assert.equal(primaryProfile('dl', 'tails', profiles), 'dl');
  assert.equal(primaryProfile(null, 'dl', profiles), 'dl');
});

test('reported fixes go in time order, are drawn dashed, and say what they were', () => {
  const tr = [{ ts: 10 }, { ts: 20 }, { ts: 40 }];
  insertFix(tr, { ts: 30, rep: true });
  insertFix(tr, { ts: 50 });
  insertFix(tr, { ts: 5 });
  assert.deepEqual(tr.map((f) => f.ts), [5, 10, 20, 30, 40, 50]);
  const runs = trackRuns(tr);
  assert.deepEqual(runs.map((r) => [r.dashed, r.pts.map((f) => f.ts)]), [[false, [5, 10, 20]], [true, [20, 30, 40]], [false, [40, 50]]]);
  assert.deepEqual(trackRuns([{ ts: 1 }]), []);
  assert.equal(reportSource({ ts: 100.25 }, { pos_source: 'ADS-C', pos_ts: 100.25 }), 'ADS-C');
  assert.equal(reportSource({ ts: 100 }, { pos_source: 'ADS-C', pos_ts: 900 }), 'datalink'); // a later report's row
  assert.equal(reportSource({ ts: 100 }, null), 'datalink');
  // A summary's fields don't say which fix: the newest one reported is it.
  assert.equal(reportSource({ ts: 100 }, { pos_source: 'CPDLC' }, true), 'CPDLC');
  assert.equal(reportSource({ ts: 100 }, { pos_source: 'CPDLC' }, false), 'datalink');
});

// --- Occupancy (occutil.js) ---------------------------------------------------

// A grid answer as /api/occupancy gives it: 4 buckets of 15 min, the second
// empty, 5 frequency buckets of 1 MHz from 24 MHz.
const OGRID = {
  since: 1000, until: 4600, bucket_s: 900, ts: [1000, 2800, 3700],
  f_lo_hz: 24e6, bucket_hz: 1e6, fbuckets: 5, grid: { lo_hz: 24e6, bin_hz: 1e6, bins: 40, hi_hz: 64e6 },
  db_min: -130, db_step: 0.5,
};

test('occupancy: buckets, levels and the picture, newest at the top', () => {
  const rows = rowMap(OGRID, 4);
  assert.deepEqual([...rows], [0, -1, 1, 2]);
  // Bytes: 3 rows x 5 buckets; 100 = -80 dBFS, 160 = -50.
  const bytes = Uint8Array.from([100, 100, 160, 100, 0, 100, 100, 100, 100, 100, 100, 160, 100, 100, 100]);
  const lv = levels(bytes, -130, 0.5);
  assert.equal(lv.lo, -86);
  assert.equal(lv.hi, -36);
  assert.deepEqual(levels(new Uint8Array(4), -130, 0.5), { lo: -100, hi: -50 });
  const lut = new Uint8ClampedArray(256 * 3).map((_, i) => Math.floor(i / 3));
  const px = paint(bytes, 5, rows, 4, lut, -86, -36, -130, 0.5);
  const at = (x, y) => [...px.slice((y * 5 + x) * 4, (y * 5 + x) * 4 + 4)];
  // Top row (y 0) is the newest bucket (data row 2): its carrier at x 1.
  assert.ok(at(1, 0)[0] > at(0, 0)[0]);
  // The empty bucket (bucket 1) is grey, at y 2; an unmeasured cell too.
  assert.deepEqual(at(0, 2), [26, 31, 39, 255]);
  assert.deepEqual(at(4, 3), [26, 31, 39, 255]);
  assert.equal(at(2, 3)[0], Math.round(((-50 + 86) / 50) * 255));
});

test('occupancy: cells, zoom and frequency labels', () => {
  const rows = rowMap(OGRID, 4);
  const c = cellAt(OGRID, 4, 0.5, 0.01);
  assert.deepEqual(c, { ti: 3, fi: 2, t0: 3700, t1: 4600, f0: 26e6, f1: 27e6 });
  const bytes = Uint8Array.from([100, 100, 160, 100, 0, 100, 100, 100, 100, 100, 100, 160, 100, 100, 100]);
  assert.equal(cellLevel(bytes, rows, c, 5, -130, 0.5), -80);
  assert.equal(cellLevel(bytes, rows, { ...c, ti: 1 }, 5, -130, 0.5), null);
  assert.equal(cellLevel(bytes, rows, { ...c, ti: 3, fi: 1 }, 5, -130, 0.5), -50);
  assert.deepEqual(zoomRange(OGRID, 0.21, 0.79), { fmin: 25e6, fmax: 28e6 });
  assert.equal(zoomRange(OGRID, 0.41, 0.45), null);
  assert.deepEqual(spanOf(OGRID), { lo: 24e6, hi: 29e6 });
  const t = freqTicks(24e6, 1704e6, 10);
  assert.deepEqual(t.map((x) => x.label), ['200', '400', '600', '800', '1000', '1200', '1400', '1600']);
  assert.equal(freqTicks(162e6, 163e6, 10)[1].label, '162.1');
  assert.equal(RANGES[0].tb, 96);
  assert.deepEqual([...bytesOf('AAH/')], [0, 1, 255]);
});

test('occupancy: links to watch a range and to the log, and the schedule line', () => {
  const watch = { min_hz: 1e6, max_hz: 6e9, rates_hz: [2e6, 10e6] };
  assert.equal(watchHref(162.35e6, 162.45e6, watch), '/?watch_from=162.35&watch_to=162.45');
  assert.equal(watchHref(7e9, 8e9, watch), null);
  assert.equal(watchHref(1e6, 2e6, null), null);
  const log = new URL(logHref(162.35e6, 162.45e6, 3700, 4600), 'http://x');
  assert.equal(log.pathname, '/log');
  assert.equal(log.searchParams.get('freq'), '162.4000');
  assert.equal(log.searchParams.get('tol'), '50000');
  assert.equal(log.searchParams.get('since'), '2800');
  assert.equal(log.searchParams.get('until'), '5500');
  assert.equal(new URL(logHref(162.4e6, 162.41e6, 0, 900), 'http://x').searchParams.get('tol'), '12500');
  const oc = { enabled: true, interval_min: 15, range_mhz: [24, 1704], passes: 20, bin_hz: 98039.2, lna_gain_db: 24, vga_gain_db: 40 };
  assert.equal(scheduleText(oc), 'Every 15 min on the UTC clock · 24–1704 MHz · 20 passes at 98.0 kHz · LNA 24 dB, VGA 40 dB, amp off');
  assert.equal(scheduleText({ enabled: false }), null);
});

test('occupancy: the unusual items of many slots, one entry per signal', () => {
  const it = (slot, ts, lo, hi, ex) => ({ slot, ts, lo_hz: lo, hi_hz: hi, freq_hz: (lo + hi) / 2, excess_db: ex, level_db: -50, usual_db: -80 });
  const g = groupUnusual([
    it(3, 3000, 155.27e6, 155.37e6, 38),
    it(3, 3000, 460e6, 460.2e6, 12),
    it(2, 2100, 155.27e6, 155.33e6, 40),
    it(1, 1200, 155.37e6, 155.47e6, 20), // touches the first within one bin
    it(1, 1200, 931.8e6, 932e6, 25),
  ], 100e3);
  assert.equal(g.length, 3);
  assert.deepEqual([g[0].lo_hz, g[0].hi_hz, g[0].first, g[0].last, g[0].slots, g[0].best.excess_db, g[0].latest.slot], [155.27e6, 155.47e6, 1200, 3000, 3, 40, 3]);
  assert.equal(g[1].lo_hz, 460e6);
  assert.equal(g[2].last, 1200);
  assert.equal(groupUnusual([it(1, 1, 1e6, 2e6, 10), it(1, 1, 2.2e6, 3e6, 10)]).length, 2);
});

test('a tap on a waterfall picks one frequency, a drag a range, either way round', () => {
  const band = { center_hz: 152_000_000, sample_rate_hz: 2_000_000 };
  assert.deepEqual(pickRange(band, 0.5, 0.502), { lo_hz: 152_002_000, hi_hz: 152_002_000 });
  assert.deepEqual(pickRange(band, 0.75, 0.25), { lo_hz: 151_500_000, hi_hz: 152_500_000 });
  assert.deepEqual(pickRange(band, -0.2, 0.25), { lo_hz: 151_000_000, hi_hz: 151_500_000 });
});

test('a picked range is drawn where it sits, never thinner than a sliver', () => {
  const band = { center_hz: 100_000_000, sample_rate_hz: 10_000_000 };
  assert.deepEqual(rangePct(band, 97_500_000, 100_000_000), { left: 25, width: 25 });
  const tap = rangePct(band, 100_000_000, 100_000_000);
  assert.ok(Math.abs(tap.left - 49.75) < 1e-9 && Math.abs(tap.width - 0.5) < 1e-9);
  assert.equal(rangePct(band, 80_000_000, 90_000_000), null);
  assert.deepEqual(rangePct(band, 90_000_000, 96_000_000), { left: 0, width: 10 });
});

test('the capture link fills in Live and comes back out of the address', () => {
  const href = captureHref(152_007_500, 152_007_500, 'history', { min_hz: 1e6, max_hz: 6e9 });
  assert.equal(href, '/?capture_from=152.0075&capture_to=152.0075&origin=history');
  assert.deepEqual(captureQuery(href.slice(1)), { lo_hz: 152_007_500, hi_hz: 152_007_500, origin: 'history' });
  assert.deepEqual(captureQuery('?capture_from=98&capture_to=88&origin=elsewhere'), { lo_hz: 88_000_000, hi_hz: 98_000_000, origin: 'live' });
  assert.deepEqual(captureQuery('?capture_from=433.92'), { lo_hz: 433_920_000, hi_hz: 433_920_000, origin: 'live' });
  assert.equal(captureQuery('?watch_from=98'), null);
  assert.equal(captureHref(7e9, 8e9, 'occupancy', { min_hz: 1e6, max_hz: 6e9 }), null);
  assert.equal(captureHref(5.9e9, 6.1e9, 'occupancy', { min_hz: 1e6, max_hz: 6e9 }), '/?capture_from=5900&capture_to=6000&origin=occupancy');
});

test('capture words: ranges, sizes, rates and states', () => {
  assert.equal(rangeLabel(152_007_500, 152_007_500), '152.0075 MHz');
  assert.equal(rangeLabel(88_100_000, 91_300_000), '88.1000–91.3000 MHz');
  assert.equal(sizeText(120_000_000), '120 MB');
  assert.equal(sizeText(1_200_000_000), '1.2 GB');
  assert.equal(sizeText(4_000), '4 kB');
  assert.equal(rateText(2_400_000), '2.4 MS/s');
  assert.equal(rateText(2_083_333), '2,083,333 S/s');
  const rec = { id: 'a', state: 'decoding', decodes: [{ decoder: 'x', state: 'done' }, { decoder: 'y', state: 'queued' }] };
  assert.equal(stateText(rec, null), 'Decoding (1 to go)');
  assert.equal(stateText({ id: 'a', state: 'waiting' }, { id: 'a', state: 'recording', left_s: 12.2 }), 'Recording, 13 s left');
  assert.equal(stateText({ id: 'a', state: 'done' }, { id: 'b', state: 'recording' }), 'Done');
  assert.equal(stateTone({ state: 'done', decodes: [{ state: 'failed' }] }), 'warn');
  assert.equal(stateTone({ state: 'failed' }), 'bad');
  assert.equal(stateTone({ state: 'done', decodes: [] }), 'ok');
});

test('the form ticks what was suggested until the user picks, and sends only what was set', () => {
  assert.deepEqual(ticked(null, ['x']), ['x']);
  assert.deepEqual(ticked([], ['x']), []);
  assert.deepEqual(captureBody({ lo_hz: 1, hi_hz: 2, seconds: 30, chosen: null, lna: '', vga: null, amp: false }), { from_hz: 1, to_hz: 2, seconds: 30, origin: 'live' });
  assert.deepEqual(captureBody({ lo_hz: 1, hi_hz: 2, seconds: 10, chosen: ['x'], lna: '24', vga: 30, amp: true, origin: 'history' }), {
    from_hz: 1, to_hz: 2, seconds: 10, origin: 'history', decoders: ['x'], lna_gain_db: 24, vga_gain_db: 30, amp: true,
  });
});

test('Summaries asks the aircraft database about aircraft subjects only, with their callsigns', () => {
  const profiles = { a: { subject_type: 'icao24' }, t: { subject_type: 'registration' }, s: { subject_type: null } };
  const data = { profiles: {
    a: { top: [{ subject: 'a1b2c3', labels: ['UAL12'] }, { subject: '~ad0001', labels: [] }, { subject: 'A1B2C3', labels: [] }] },
    t: { top: [{ subject: 'N123AB', labels: ['X'] }] },
    s: { top: [{ subject: '367000001', labels: [] }] },
  } };
  const w = aircraftWanted(profiles, data);
  assert.deepEqual(w, { icao: [['A1B2C3', 'UAL12']], tails: ['N123AB'] });
  assert.equal(aircraftQuery([['A1B2C3', 'UAL12'], ['D4E5F6', '']]), 'A1B2C3:UAL12,D4E5F6');
});

test('an aircraft is named by registration, make and model, and owner', () => {
  const n = aircraftName({ class: 'private', why: 'registered to an individual', registration: 'N123AB', manufacturer: 'CESSNA', model: '172S', owner: 'DOE JOHN', registrant: 'Individual', kind: 'Fixed wing single engine' });
  assert.equal(n.short, 'N123AB · CESSNA 172S · DOE JOHN (individual)');
  assert.equal(n.cls, 'private');
  assert.match(n.full, /owner DOE JOHN \(individual\)/);
  assert.match(n.full, /registered to an individual/);
  const m = aircraftName({ class: 'military', why: 'listed by plane-alert-db', type: 'C-17A', operator: 'USAF', category: 'Military', tags: ['Transport'] });
  assert.equal(m.short, 'C-17A · USAF');
  assert.match(m.full, /listed as Military · Transport/);
  assert.equal(aircraftName(null), null);
});


// --- listening ---------------------------------------------------------------------

function frameBytes(type, channel, flags, rate, seq, ts, level, thr, floor, payload = []) {
  const buf = new ArrayBuffer(HEADER_BYTES + payload.length);
  const v = new DataView(buf);
  [0x48, 0x4b, 0x41, 0x31].forEach((b, i) => v.setUint8(i, b));
  v.setUint8(4, type);
  v.setUint8(5, channel);
  v.setUint16(6, flags, true);
  v.setUint32(8, rate, true);
  v.setUint32(12, seq, true);
  v.setFloat64(16, ts, true);
  v.setFloat32(24, level, true);
  v.setFloat32(28, thr, true);
  v.setFloat32(32, floor, true);
  new Uint8Array(buf, HEADER_BYTES).set(payload);
  return buf;
}

test('audio frames are read as listen.FRAME packs them', () => {
  const f = parseFrame(frameBytes(AUDIO, 2, FLAG_OPEN, 8000, 7, 1.5, -30, -40, -48, [0xff, 0x80]));
  assert.deepEqual([f.type, f.channel, f.flags, f.rate, f.seq, f.ts], [AUDIO, 2, FLAG_OPEN, 8000, 7, 1.5]);
  assert.equal(f.level_db, -30);
  assert.equal(f.threshold_db, -40);
  assert.deepEqual([...f.payload], [0xff, 0x80]);
  assert.equal(parseFrame(frameBytes(METER, 0, 0, 8000, 1, 0, 0, 0, 0)).payload.length, 0);
  const bad = frameBytes(DROPPED, 0, 0, 0, 3, 0, 0, 0, 0);
  new DataView(bad).setUint8(0, 0);
  assert.equal(parseFrame(bad), null);
  assert.equal(parseFrame(new ArrayBuffer(10)), null);
});

test('mu-law decodes as G.711 (the same table as listen.py)', () => {
  assert.equal(ULAW[0xff], 0);
  assert.ok(Math.abs(ULAW[0x80] - 32124 / 32768) < 1e-7);
  assert.ok(Math.abs(ULAW[0x00] + 32124 / 32768) < 1e-7);
  assert.deepEqual([...ulawDecode(new Uint8Array([0xff, 0x7f]))], [0, -0]);
});

function wav(format, bits, rate, data) {
  const fmt = 16;
  const buf = new ArrayBuffer(12 + 8 + fmt + 8 + data.length + (data.length % 2));
  const v = new DataView(buf);
  const put = (o, s) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  put(0, 'RIFF');
  v.setUint32(4, buf.byteLength - 8, true);
  put(8, 'WAVE');
  put(12, 'fmt ');
  v.setUint32(16, fmt, true);
  v.setUint16(20, format, true);
  v.setUint16(22, 1, true);
  v.setUint32(24, rate, true);
  v.setUint32(28, rate * (bits / 8), true);
  v.setUint16(32, bits / 8, true);
  v.setUint16(34, bits, true);
  put(36, 'data');
  v.setUint32(40, data.length, true);
  new Uint8Array(buf, 44).set(data);
  return buf;
}

test('a clip file is read whatever its encoding', () => {
  const u = parseWav(wav(7, 8, 8000, [0xff, 0x80, 0x00]));
  assert.equal(u.rate, 8000);
  assert.equal(u.samples.length, 3);
  assert.equal(u.samples[0], 0);
  const p = parseWav(wav(1, 16, 16000, [0x00, 0x40, 0x00, 0xc0]));
  assert.deepEqual([p.rate, ...p.samples], [16000, 0.5, -0.5]);
  assert.equal(parseWav(wav(3, 32, 8000, [0, 0, 0, 0])), null);
  assert.equal(parseWav(new ArrayBuffer(4)), null);
});

test('resampling joins pieces without a seam', () => {
  const x = Float32Array.from({ length: 80 }, (_, i) => Math.sin((2 * Math.PI * i) / 16));
  const whole = new Resampler(8000, 48000).push(x);
  const r = new Resampler(8000, 48000);
  const parts = [...r.push(x.subarray(0, 33)), ...r.push(x.subarray(33, 50)), ...r.push(x.subarray(50))];
  assert.equal(parts.length, whole.length);
  assert.ok(parts.every((v, i) => Math.abs(v - whole[i]) < 1e-6));
  assert.ok(Math.abs(whole.length - 80 * 6) <= 1);
  assert.ok(Math.abs(new Resampler(8000, 44100).push(new Float32Array(8000)).length - 44100) <= 1);
});

test('pieces play back to back, restart after running dry, and drop when far ahead', () => {
  assert.deepEqual(nextStart(null, 10), { at: 10.25, reset: false });
  assert.deepEqual(nextStart(10.5, 10), { at: 10.5, reset: false });
  assert.deepEqual(nextStart(9.9, 10), { at: 10.25, reset: true });
  assert.deepEqual(nextStart(12, 10), { at: null, reset: false });
});

test('channels: modes by band, the raster a tap snaps to, names and the list', () => {
  assert.equal(defaultMode(121_500_000), 'am');
  assert.equal(defaultMode(98_100_000), 'wfm');
  assert.equal(defaultMode(162_550_000), 'nfm');
  assert.equal(snapHz(121_511_000), 121_500_000);
  assert.equal(snapHz(98_140_000), 98_100_000);
  assert.equal(snapHz(162_540_000), 162_550_000);
  assert.equal(snapHz(146_521_300), 146_520_000);
  assert.equal(channelName({ freq_hz: 121_500_000 }), '121.500');
  assert.equal(channelName({ freq_hz: 123_458_333, label: 'Chat' }), 'Chat · 123.4583');
  const a = { freq_hz: 1e8, mode: 'wfm' };
  const b = { freq_hz: 1.2e8, mode: 'am' };
  assert.deepEqual(withChannel([a], b), [a, b]);
  assert.deepEqual(withChannel([a, b], { ...a, squelch_db: 5 }), [b, { ...a, squelch_db: 5 }]);
  assert.deepEqual(withChannel([a, b], b, 4, true), [b]);
  assert.deepEqual(withChannel([a, b, { freq_hz: 3e8 }, { freq_hz: 4e8 }], { freq_hz: 5e8 }, 4).map((c) => c.freq_hz), [1.2e8, 3e8, 4e8, 5e8]);
  assert.deepEqual(withoutChannel([a, b], 1e8), [b]);
  assert.ok(sameChannels([a, b], [{ ...a, squelch_db: 3 }, b]));
  assert.ok(!sameChannels([a, b], [b, a]));
  assert.deepEqual(channelBody({ freq_hz: 1.215e8 + 0.4, mode: 'am', squelch_db: 0, label: null }), { freq_hz: 121_500_000, mode: 'am' });
  assert.deepEqual(channelBody({ freq_hz: 1, mode: 'nfm', squelch_db: -3, label: 'x' }), { freq_hz: 1, mode: 'nfm', squelch_db: -3, label: 'x' });
});

test('the words before a retune say what stops, and for how long', () => {
  const plan = { retune: true, running: 'decoder', sample_rate_hz: 2e6, center_hz: 121_525_000 };
  assert.equal(planText(plan, 15), 'This retunes the receiver to a 2 MHz window at 121.5250 MHz: decoder stops for 15 min, then comes back by itself.');
  assert.match(planText({ ...plan, running: LISTEN }, 15), /^Outside this listen's window: it moves to a 2 MHz window/);
  assert.match(planText({ ...plan, running: null }, 5), /nothing is running now for 5 min/);
  assert.match(planText({ retune: false, riding: 'watch' }, 5), /no retune, nothing stops/);
});

test('meters, the squelch slider and clip rows', () => {
  assert.equal(meterPct(-100), 0);
  assert.equal(meterPct(-55), 50);
  assert.equal(meterPct(0), 100);
  assert.equal(meterPct(null), 0);
  assert.equal(squelchText(-20, [-20, 30]), 'open');
  assert.equal(squelchText(6, [-20, 30]), '+6 dB');
  assert.equal(squelchText(-4, [-20, 30]), '-4 dB');
  assert.ok(isClip({ profile: LISTEN, kind: 'clip', fields: { clip: 'x' } }));
  assert.ok(!isClip({ profile: LISTEN, kind: 'clip', fields: {} }));
  assert.ok(!isClip({ profile: 'other', kind: 'clip', fields: { clip: 'x' } }));
  const reg = { profiles: [{ name: 'p' }], listen: { enabled: true, color: '#d2a64a' } };
  assert.deepEqual(withListen(reg).map((p) => p.name), ['p', LISTEN]);
  assert.equal(withListen(reg)[1].color, '#d2a64a');
  assert.deepEqual(withListen({ ...reg, listen: { enabled: false } }).map((p) => p.name), ['p']);
  assert.deepEqual(withListen({ profiles: [{ name: 'p' }] }).map((p) => p.name), ['p']);
});

test('rotation steps of one profile on different bands read apart', () => {
  assert.equal(stepName({ profile: 'p' }), 'p');
  assert.equal(stepName({ profile: 'p', freqs_hz: [] }), 'p');
  assert.equal(stepName({ profile: 'p', freqs_hz: [433920000] }), 'p 433.92');
  assert.equal(stepName({ profile: 'p', freqs_hz: [315000000] }), 'p 315');
  assert.equal(stepName({ profile: 'p', freqs_hz: [931750000, 929612500] }), 'p 931.75…');
});
