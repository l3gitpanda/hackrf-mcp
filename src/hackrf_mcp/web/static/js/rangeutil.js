// The Map's receiver range layer, without the DOM: where an outline's
// corners go, how a distance and a bearing read, and the signal-against-
// distance chart's paths. /api/positions/range gives the numbers.

const EARTH_KM = 6371.0088;
const RAD = Math.PI / 180;

// The point `km` from (lat, lon) on `bearing` degrees (0 = north).
export function destination(lat, lon, bearing, km) {
  const p0 = lat * RAD;
  const l0 = lon * RAD;
  const d = km / EARTH_KM;
  const b = bearing * RAD;
  const p = Math.asin(Math.sin(p0) * Math.cos(d) + Math.cos(p0) * Math.sin(d) * Math.cos(b));
  const l = l0 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(p0), Math.cos(d) - Math.sin(p0) * Math.sin(p));
  return [p / RAD, ((((l / RAD) + 540) % 360) - 180)];
}

// An outline through each sector's farthest fix: an arc at that distance
// across the sector (`perSector` points), and the receiver itself where a
// sector heard nothing, so the shape is what was heard and no more.
export function outline(receiver, kms, sectorDeg, perSector = 3) {
  const pts = [];
  kms.forEach((km, i) => {
    if (km == null || km <= 0) {
      pts.push([receiver[0], receiver[1]]);
      return;
    }
    for (let k = 0; k <= perSector - 1; k++) {
      const bearing = (i + k / Math.max(1, perSector - 1)) * sectorDeg;
      pts.push(destination(receiver[0], receiver[1], bearing, km));
    }
  });
  return pts;
}

const POINTS = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];

// "NNE" for 22.5 degrees: the nearest of the 16 compass points.
export function compass(bearing) {
  return POINTS[Math.round((((bearing % 360) + 360) % 360) / 22.5) % 16];
}

// "312 km (194 mi)".
export function fmtKm(km) {
  if (km == null) return '—';
  const digits = km < 10 ? 1 : 0;
  return `${km.toFixed(digits)} km (${(km / 1.609344).toFixed(digits)} mi)`;
}

// A tidy step for about `n` ticks across `span`.
function niceStep(span, n) {
  const raw = span / Math.max(1, n);
  const mag = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || 10 * mag;
}

// The chart's geometry for bins [from_km, n, p10, p50, p90] of `binKm`:
// the 10-90% band as an area, the median as a line, both in a `w` x `h`
// box with `pad` = [left, top, right, bottom] for the axes, plus ticks.
// Each bin is drawn at its middle. Null when there is nothing to draw.
export function levelChart(bins, binKm, w = 300, h = 140, pad = [34, 6, 6, 18]) {
  const used = (bins || []).filter((b) => b[1] > 0);
  if (!used.length) return null;
  const [pl, pt, pr, pb] = pad;
  const xmaxRaw = Math.max(...used.map((b) => b[0] + binKm));
  const xstep = niceStep(xmaxRaw, 5);
  const xmax = Math.ceil(xmaxRaw / xstep) * xstep;
  const lo = Math.min(...used.map((b) => b[2]));
  const hi = Math.max(...used.map((b) => b[4]));
  const ystep = niceStep(Math.max(hi - lo, 1), 4);
  const ymin = Math.floor(lo / ystep) * ystep;
  const ymax = Math.max(Math.ceil(hi / ystep) * ystep, ymin + ystep);
  const X = (km) => pl + (km / xmax) * (w - pl - pr);
  const Y = (db) => pt + (1 - (db - ymin) / (ymax - ymin)) * (h - pt - pb);
  const mid = (b) => b[0] + binKm / 2;
  const f = (v) => v.toFixed(1);
  const top = used.map((b) => `${f(X(mid(b)))},${f(Y(b[4]))}`);
  const bottom = used.slice().reverse().map((b) => `${f(X(mid(b)))},${f(Y(b[2]))}`);
  const xticks = [];
  for (let v = 0; v <= xmax + 1e-9; v += xstep) xticks.push({ v, x: X(v) });
  const yticks = [];
  for (let v = ymin; v <= ymax + 1e-9; v += ystep) yticks.push({ v, y: Y(v) });
  return {
    band: `M${top.join(' L')} L${bottom.join(' L')} Z`,
    median: `M${used.map((b) => `${f(X(mid(b)))},${f(Y(b[3]))}`).join(' L')}`,
    dots: used.map((b) => ({ x: X(mid(b)), y: Y(b[3]), n: b[1], km: b[0], db: b[3] })),
    xticks,
    yticks,
    box: { left: pl, top: pt, right: w - pr, bottom: h - pb },
  };
}
