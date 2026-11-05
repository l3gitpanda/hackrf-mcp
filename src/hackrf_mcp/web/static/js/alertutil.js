// Pure helpers for the Alerts screen (tests/js/webui.test.mjs runs them).

// "7500, 7600\n7700" -> ['7500', '7600', '7700'].
export function parseValues(text) {
  return String(text || '')
    .split(/[\n,]+/)
    .map((v) => v.trim())
    .filter(Boolean);
}

// The fields a rule may test for the chosen profiles (all when none are
// chosen): the row's own columns first, then the profiles' fields A-Z.
const BASE = ['subject', 'label', 'kind', 'freq_hz', 'level_db'];
export function fieldsFor(byProfile, chosen) {
  const names = chosen && chosen.length ? chosen : Object.keys(byProfile || {});
  const seen = new Set();
  for (const n of names) for (const f of (byProfile || {})[n] || []) seen.add(f);
  const base = BASE.filter((f) => seen.has(f));
  const rest = [...seen].filter((f) => !BASE.includes(f)).sort();
  return [...base, ...rest];
}

// What the API takes from the editor's draft.
export function ruleBody(d) {
  return {
    name: String(d.name || '').trim(),
    enabled: !!d.enabled,
    profiles: [...(d.profiles || [])],
    conditions: (d.conditions || []).map((c) => ({ field: c.field, op: c.op, values: parseValues(c.values) })),
    webhook: !!d.webhook,
    repeat_s: Number(d.repeat_s) || 0,
  };
}

// A rule from the API as the editor's draft (values as text).
export function draftOf(rule) {
  return {
    id: rule.id ?? null,
    name: rule.name || '',
    enabled: rule.enabled !== false,
    profiles: [...(rule.profiles || [])],
    conditions: (rule.conditions || []).map((c) => ({ field: c.field, op: c.op, values: (c.values || []).join(', ') })),
    webhook: !!rule.webhook,
    repeat_s: rule.repeat_s ?? 600,
  };
}

export const REPEATS = [
  [0, 'every hit'],
  [600, '10 min'],
  [3600, '1 hour'],
  [86400, '1 day'],
];

export function repeatText(s) {
  const r = REPEATS.find(([v]) => v === Number(s));
  if (r) return r[1];
  return s >= 60 ? `${Math.round(s / 60)} min` : `${s} s`;
}

// Whether two drafts would save the same rule.
export function sameRule(a, b) {
  return JSON.stringify(ruleBody(a)) === JSON.stringify(ruleBody(b));
}

// A hit's time: "21:08:10" today (UTC), "11-02 21:08" before.
export function hitWhen(ts, now) {
  const d = new Date(ts * 1000).toISOString();
  return d.slice(0, 10) === new Date(now * 1000).toISOString().slice(0, 10) ? d.slice(11, 19) : `${d.slice(5, 10)} ${d.slice(11, 16)}`;
}

// A field's value list with a choice switched on or off (a field with a
// fixed set of values, from /api/alerts' choices). A choice is on when the
// list names its value or its label; 'equals' (single) keeps one value.
export function hasChoice(text, choice) {
  const names = [choice.value, choice.label].filter(Boolean).map((v) => String(v).toLowerCase());
  return parseValues(text).some((v) => names.includes(v.toLowerCase()));
}
export function toggleChoice(text, choice, single) {
  const on = hasChoice(text, choice);
  if (single) return on ? '' : choice.value;
  if (on) {
    const names = [choice.value, choice.label].filter(Boolean).map((v) => String(v).toLowerCase());
    return parseValues(text).filter((v) => !names.includes(v.toLowerCase())).join(', ');
  }
  return [...parseValues(text), choice.value].join(', ');
}

// Hits or preview rows that are aircraft (their profile's subjects are ICAO
// addresses) and not yet looked up, as /api/aircraft's list: ICAO:CALLSIGN.
export function aircraftToAsk(rows, profiles, known) {
  const out = new Map();
  for (const r of rows || []) {
    const p = (profiles || {})[r.profile];
    if (!p || p.subject_type !== 'icao24' || !r.subject) continue;
    const icao = String(r.subject).toUpperCase();
    if (!/^[0-9A-F]{6}$/.test(icao) || (known && known.has(icao)) || out.has(icao)) continue;
    out.set(icao, r.label ? `${icao}:${String(r.label).trim()}` : icao);
  }
  return [...out.values()].slice(0, 500);
}
