// Pure helpers for panels (the operator's panel files), on the Plugins and
// Summaries screens. tests/js/webui.test.mjs runs them.

// "aircraft · squawk, callsign · where squawk is any of 7700 · by subject,
// newest first, 20 · last 1 h"
export function panelQueryText(p) {
  const sorts = { last: 'newest first', rows: 'most rows first', messages: 'most messages first' };
  const bits = [];
  if (p.kinds && p.kinds.length) bits.push(p.kinds.join(', '));
  if (p.fields && p.fields.length) bits.push(p.fields.join(', '));
  if (p.filter) bits.push(`where ${p.filter}`);
  bits.push(`by ${p.group_by.replace(/_/g, ' ')}, ${sorts[p.sort] || p.sort}, ${p.limit}`);
  bits.push(p.window_s ? `last ${windowText(p.window_s)}` : "the screen's period");
  return bits.join(' · ');
}

export function windowText(s) {
  if (s % 86400 === 0) return `${s / 86400} d`;
  if (s % 3600 === 0) return `${s / 3600} h`;
  return `${Math.round(s / 60)} min`;
}

// A table panel's columns after the group key: its fields, less the one it
// groups by (that is the key already).
export function panelColumns(p) {
  return (p.fields || []).filter((f) => f !== p.group_by);
}

// The key column's heading.
export function keyHeading(p) {
  return p.group_by === 'subject' ? 'Subject' : p.group_by.replace(/_/g, ' ');
}
