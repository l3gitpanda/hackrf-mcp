// The Alerts screen: rules over the live log (a field is any of, equals,
// matches, or is above or below a value), a preview of what a rule would
// have matched in the last 7 days, and the hits with their acknowledgement.
// Profiles, their fields (with the values of those that have a fixed set,
// such as the aircraft classes) and colours come from the API; nothing here
// names a protocol. hackrf-web checks the rows itself (web/alerts.py), with
// this page open or not. Hits and preview rows on aircraft show the class
// the aircraft database gives them.

import { api, post } from './api.js';
import { hitLinks, paintAlerts, paintAppBar, paintClock } from './appbar.js';
import { REPEATS, aircraftToAsk, draftOf, fieldsFor, hasChoice, hitWhen, parseValues, repeatText, ruleBody, sameRule, toggleChoice } from './alertutil.js';
import { ago, assignColors, clear, h } from './util.js';

const $ = (id) => document.getElementById(id);
const NOW = () => Date.now() / 1000;
const PAGE = 100;

const S = {
  registry: null,
  profiles: {},
  colors: {},
  data: null,
  sel: null, // a rule id, or null for a new rule
  draft: null,
  saved: null,
  hits: [],
  more: false,
  status: null,
  statusAt: 0,
  previewSeq: 0,
  previewTimer: null,
  preview: null,
  // Aircraft classes (from the registry) and what /api/aircraft said per
  // ICAO address (null: asked, nothing known).
  acClasses: new Map(),
  ac: new Map(),
};

const color = (name) => S.colors[name] || '#8a96a6';
const hasMap = (p) => ((S.profiles[p] || {}).views || []).some((v) => v.name === 'map');
const NEW_RULE = { name: '', enabled: true, profiles: [], conditions: [{ field: 'subject', op: 'any_of', values: '' }], webhook: false, repeat_s: 600 };

// --- Start-up ------------------------------------------------------------------------------

async function init() {
  S.registry = await api('/api/registry');
  for (const p of S.registry.profiles) S.profiles[p.name] = p;
  S.colors = assignColors(S.registry.profiles);
  for (const c of (S.registry.aircraft && S.registry.aircraft.classes) || []) S.acClasses.set(c.name, c);
  for (const [v, label] of REPEATS) $('ed-repeat').append(h('option', { value: String(v) }, label));
  wire();
  await loadRules();
  edit(null);
  await loadHits();
  pollStatus();
  setInterval(pollStatus, 10000);
  setInterval(() => paintClock(S.status ? S.status.now + (NOW() - S.statusAt) : NOW()), 1000);
}

async function pollStatus() {
  try {
    const st = await api('/api/status');
    const before = S.status && S.status.alerts && S.status.alerts.latest ? S.status.alerts.latest.id : null;
    S.status = st;
    S.statusAt = NOW();
    paintAppBar(st.live, color, 'open');
    paintAlerts(st.alerts, color, hasMap);
    const latest = st.alerts && st.alerts.latest ? st.alerts.latest.id : null;
    if (latest !== before && before !== null) {
      // A new hit (or one acknowledged elsewhere): refresh the lists.
      await Promise.all([loadRules(), loadHits()]);
    }
  } catch (e) {
    paintAppBar(null, color, 'closed');
  }
}

function showError(msg) {
  $('aerr').hidden = !msg;
  $('aerr').textContent = msg || '';
}

function wire() {
  $('r-new').addEventListener('click', () => edit(null));
  $('ed').addEventListener('submit', (e) => {
    e.preventDefault();
    save();
  });
  $('ed-revert').addEventListener('click', () => edit(S.sel));
  $('ed-delete').addEventListener('click', remove);
  $('ed-add').addEventListener('click', () => {
    if (S.draft.conditions.length >= 8) return;
    const fields = fieldsFor(S.data.fields, S.draft.profiles);
    S.draft.conditions.push({ field: fields[0] || 'subject', op: 'any_of', values: '' });
    renderConditions();
    changed();
  });
  $('ed-name').addEventListener('input', () => {
    S.draft.name = $('ed-name').value;
    changed(false);
  });
  $('ed-webhook').addEventListener('change', () => {
    S.draft.webhook = $('ed-webhook').checked;
    changed(false);
  });
  $('ed-repeat').addEventListener('change', () => {
    S.draft.repeat_s = Number($('ed-repeat').value);
    changed(false);
  });
  $('ed-enabled').addEventListener('change', () => {
    S.draft.enabled = $('ed-enabled').checked;
    changed(false);
  });
  $('ed-test').addEventListener('click', testWebhook);
  $('h-ackall').addEventListener('click', () => ack(null));
  $('h-more-btn').addEventListener('click', () => loadHits(true));
}

// --- Rules ---------------------------------------------------------------------------------

async function loadRules() {
  try {
    S.data = await api('/api/alerts');
    showError(null);
  } catch (e) {
    showError(`Could not read the rules: ${e.message}`);
    return;
  }
  renderRules();
  $('r-webhook').textContent = S.data.webhook
    ? `A webhook is set up (${S.data.webhook_format}); rules that ask for it post their hits there.`
    : 'No webhook is set up, so hits show only here. The operator sets one with HACKRF_WEB_ALERT_WEBHOOK_FILE.';
}

function renderRules() {
  const box = clear($('rules'));
  const rules = S.data.rules;
  $('r-count').textContent = rules.length ? `${rules.length}` : '';
  if (!rules.length) {
    box.append(h('p', { class: 'muted sm pb' }, 'No rules yet. Make one on the right: for example, a squawk that is any of 7500, 7600, 7700.'));
    return;
  }
  for (const r of rules) {
    const toggle = h('input', { type: 'checkbox', class: 'switch', 'aria-label': `${r.name} on`, checked: r.enabled });
    toggle.addEventListener('change', async () => {
      try {
        await api(`/api/alerts/rules/${r.id}`, { method: 'PUT', body: JSON.stringify({ ...r, enabled: toggle.checked }) });
        await loadRules();
        if (S.sel === r.id) edit(r.id);
      } catch (e) {
        toggle.checked = !toggle.checked;
        showError(e.message);
      }
    });
    const name = h('button', { type: 'button', class: 'nm' }, r.name);
    name.addEventListener('click', () => edit(r.id));
    box.append(
      h(
        'div',
        { class: `rule${S.sel === r.id ? ' on' : ''}${r.enabled ? '' : ' off'}`, role: 'listitem' },
        toggle,
        name,
        h('span', { class: 'hits' }, `${r.hits_24h} ${r.hits_24h === 1 ? 'hit' : 'hits'} · 24 h`),
        h('span', { class: 'cond' }, r.text),
        h(
          'span',
          { class: 'meta' },
          r.profiles.length ? r.profiles.map((p) => h('span', { class: 'pbadge', '--pc': color(p) }, p)) : h('span', {}, 'all profiles'),
          h('span', {}, r.webhook ? 'here and the webhook' : 'here'),
          h('span', {}, `at most every ${repeatText(r.repeat_s)}`),
          h('span', {}, r.last_hit ? `last hit ${ago(r.last_hit, NOW())}` : 'no hits yet'),
        ),
      ),
    );
  }
}

// --- The editor ----------------------------------------------------------------------------

function edit(id) {
  const rule = id == null ? null : S.data.rules.find((r) => r.id === id);
  S.sel = rule ? rule.id : null;
  S.draft = draftOf(rule || NEW_RULE);
  S.saved = draftOf(rule || NEW_RULE);
  if (!rule) {
    const fields = fieldsFor(S.data.fields, []);
    S.draft.conditions[0].field = fields[0] || 'subject';
    S.saved.conditions[0].field = S.draft.conditions[0].field;
  }
  $('ed-title').textContent = rule ? `Rule: ${rule.name}` : 'New rule';
  $('ed-delete').hidden = !rule;
  $('ed-name').value = S.draft.name;
  $('ed-webhook').checked = S.draft.webhook;
  $('ed-webhook').disabled = !S.data.webhook;
  $('ed-webhooknote').textContent = S.data.webhook ? '' : 'No webhook is set up, so this has no effect yet.';
  $('ed-test').hidden = !S.data.webhook;
  $('ed-repeat').value = String(S.draft.repeat_s);
  if ($('ed-repeat').value !== String(S.draft.repeat_s)) {
    $('ed-repeat').append(h('option', { value: String(S.draft.repeat_s) }, repeatText(S.draft.repeat_s)));
    $('ed-repeat').value = String(S.draft.repeat_s);
  }
  $('ed-enabled').checked = S.draft.enabled;
  $('ed-err').hidden = true;
  $('ed-msg').textContent = '';
  renderProfiles();
  renderConditions();
  renderRules();
  preview();
}

function renderProfiles() {
  const box = clear($('ed-profiles'));
  for (const p of S.registry.profiles) {
    const on = S.draft.profiles.includes(p.name);
    const chip = h('button', { type: 'button', class: 'tchip', '--pc': color(p.name), 'aria-pressed': String(on) }, h('span', { class: 'sw' }), p.name);
    chip.addEventListener('click', () => {
      S.draft.profiles = on ? S.draft.profiles.filter((n) => n !== p.name) : [...S.draft.profiles, p.name];
      renderProfiles();
      renderConditions();
      changed();
    });
    box.append(chip);
  }
}

function renderConditions() {
  const box = clear($('ed-conds'));
  const fields = fieldsFor(S.data.fields, S.draft.profiles);
  S.draft.conditions.forEach((c, i) => {
    const field = h('select', { class: 'inp', 'aria-label': `Condition ${i + 1}: field` });
    for (const f of fields) field.append(h('option', { value: f }, f.replace(/_/g, ' ')));
    if (!fields.includes(c.field)) field.append(h('option', { value: c.field }, `${c.field} (not logged by these profiles)`));
    field.value = c.field;
    const op = h('select', { class: 'inp', 'aria-label': `Condition ${i + 1}: operator` });
    for (const [k, label] of Object.entries(S.data.ops)) op.append(h('option', { value: k }, label));
    op.value = c.op;
    const values = h('input', {
      class: 'inp mono',
      'aria-label': `Condition ${i + 1}: values`,
      placeholder: c.op === 'matches' ? 'pattern, * and ?' : c.op === 'gt' || c.op === 'lt' ? 'a number' : c.op === 'equals' ? 'one value' : 'values, comma separated',
      value: c.values,
    });
    const del = h('button', { type: 'button', class: 'btn sm ghost', 'aria-label': `Remove condition ${i + 1}` }, '×');
    del.hidden = S.draft.conditions.length < 2;
    field.addEventListener('change', () => {
      c.field = field.value;
      renderConditions(); // a field with a fixed set of values shows them
      changed();
    });
    op.addEventListener('change', () => {
      c.op = op.value;
      renderConditions();
      changed();
    });
    values.addEventListener('input', () => {
      c.values = values.value;
      changed();
    });
    del.addEventListener('click', () => {
      S.draft.conditions.splice(i, 1);
      renderConditions();
      changed();
    });
    box.append(h('div', { class: 'condrow' }, field, op, values, del));
    const choices = (S.data.choices || {})[c.field];
    if (choices && (c.op === 'any_of' || c.op === 'equals')) {
      const row = h('div', { class: 'condchoices', role: 'group', 'aria-label': `Condition ${i + 1}: ${c.field.replace(/_/g, ' ')}` });
      for (const ch of choices) {
        const chip = h('button', { type: 'button', class: 'tchip', '--pc': ch.color || '#8a96a6', 'aria-pressed': String(hasChoice(c.values, ch)) }, h('span', { class: 'sw' }), ch.label || ch.value);
        chip.addEventListener('click', () => {
          c.values = toggleChoice(c.values, ch, c.op === 'equals');
          renderConditions();
          changed();
        });
        row.append(chip);
      }
      box.append(row);
    }
  });
  $('ed-fieldnote').textContent = S.registry.pager_content ? '' : 'Pager text is never a field to match on.';
}

function changed(repreview = true) {
  $('ed-msg').textContent = sameRule(S.draft, S.saved) ? '' : 'Not saved yet.';
  $('ed-err').hidden = true;
  if (repreview) preview();
}

async function save() {
  const body = ruleBody(S.draft);
  try {
    const saved = S.sel == null
      ? await post('/api/alerts/rules', body)
      : await api(`/api/alerts/rules/${S.sel}`, { method: 'PUT', body: JSON.stringify(body) });
    await loadRules();
    edit(saved.id);
    $('ed-msg').textContent = 'Saved.';
  } catch (e) {
    $('ed-err').hidden = false;
    $('ed-err').textContent = e.message;
  }
}

async function remove() {
  if (S.sel == null) return;
  const r = S.data.rules.find((x) => x.id === S.sel);
  if (!window.confirm(`Delete the rule "${r ? r.name : ''}"? Its hits stay in the history.`)) return;
  try {
    await api(`/api/alerts/rules/${S.sel}`, { method: 'DELETE' });
    await loadRules();
    edit(null);
  } catch (e) {
    showError(e.message);
  }
}

async function testWebhook() {
  $('ed-msg').textContent = 'Sending…';
  try {
    const r = await post('/api/alerts/test', {});
    $('ed-msg').textContent = `Webhook: ${r.status}`;
  } catch (e) {
    $('ed-msg').textContent = `Webhook: ${e.message}`;
  }
}

// --- Preview -------------------------------------------------------------------------------

function preview() {
  clearTimeout(S.previewTimer);
  S.previewTimer = setTimeout(runPreview, 500);
}

async function runPreview() {
  const body = ruleBody(S.draft);
  clear($('pv'));
  S.preview = null;
  if (!body.conditions.length || body.conditions.some((c) => !c.values.length)) {
    $('pv-count').textContent = 'Give every condition a value to see what it would match.';
    return;
  }
  const seq = ++S.previewSeq;
  $('pv-count').textContent = 'Looking…';
  let out;
  try {
    out = await post('/api/alerts/preview', { ...body, name: body.name || 'preview' });
  } catch (e) {
    if (seq === S.previewSeq) $('pv-count').textContent = e.message;
    return;
  }
  if (seq !== S.previewSeq) return;
  $('pv-count').textContent = `${out.matches.toLocaleString('en-US')} matching ${out.matches === 1 ? 'row' : 'rows'}${out.truncated ? ' (more rows than were read)' : ''}`;
  S.preview = out.samples.slice(0, 50);
  renderPreview();
  lookAircraft(S.preview, renderPreview);
}

function renderPreview() {
  const tbody = clear($('pv'));
  for (const s of S.preview || []) {
    tbody.append(
      h(
        'tr',
        {},
        h('td', { class: 'mono' }, hitWhen(s.ts, NOW())),
        h('td', {}, h('span', { class: 'pbadge', '--pc': color(s.profile) }, s.profile)),
        h('td', { class: 'mono' }, acTag(s), s.subject || '—'),
        h('td', {}, s.label || '—'),
        h('td', { class: 'mono wc' }, s.detail),
      ),
    );
  }
}

// --- Aircraft --------------------------------------------------------------------------------

// Asks /api/aircraft what the aircraft among these rows are, once each, then
// redraws.
async function lookAircraft(rows, redraw) {
  if (!S.acClasses.size) return;
  const ask = aircraftToAsk(rows, S.profiles, S.ac);
  if (!ask.length) return;
  let r;
  try {
    r = await api(`/api/aircraft?a=${encodeURIComponent(ask.join(','))}`);
  } catch (e) {
    return; // the tags are extra: the rows stand without them
  }
  for (const [icao, info] of Object.entries(r.aircraft || {})) S.ac.set(icao, info);
  for (const a of ask) if (!S.ac.has(a.split(':')[0])) S.ac.set(a.split(':')[0], null);
  redraw();
}

// A row's aircraft class as a tag (its registration, type and operator or
// owner on hover); null for anything else.
function acTag(x) {
  const info = x.subject ? S.ac.get(String(x.subject).toUpperCase()) : null;
  const c = info && S.acClasses.get(info.class);
  if (!c) return null;
  const model = [info.manufacturer, info.model].filter(Boolean).join(' ') || info.type;
  const title = [c.label, info.registration, model, info.operator || info.owner].filter(Boolean).join(' · ');
  return h('span', { class: 'actag', '--cc': c.color, title }, c.short);
}

// --- Hits ----------------------------------------------------------------------------------

async function loadHits(more = false) {
  const before = more && S.hits.length ? `&before_id=${S.hits[S.hits.length - 1].id}` : '';
  let out;
  try {
    out = await api(`/api/alerts/hits?limit=${PAGE}${before}`);
  } catch (e) {
    showError(`Could not read the hits: ${e.message}`);
    return;
  }
  S.hits = more ? [...S.hits, ...out.hits] : out.hits;
  S.more = out.more;
  renderHits();
  lookAircraft(S.hits, renderHits);
}

function renderHits() {
  const tbody = clear($('hits'));
  const fresh = S.hits.filter((x) => x.acked == null).length;
  const total = (S.status && S.status.alerts && S.status.alerts.unacked) ?? fresh;
  $('h-new').hidden = !total;
  $('h-new').textContent = `${total} new`;
  $('h-ackall').hidden = !total;
  $('h-more').hidden = !S.more;
  if (!S.hits.length) {
    tbody.append(h('tr', {}, h('td', { colspan: '8', class: 'muted' }, 'No hits yet.')));
    return;
  }
  for (const x of S.hits) {
    const links = hitLinks(x);
    let state;
    if (x.acked == null) {
      state = h('button', { type: 'button', class: 'btn sm' }, 'Acknowledge');
      state.addEventListener('click', () => ack([x.id]));
    } else {
      state = h('span', { class: 'muted' }, 'acknowledged');
    }
    tbody.append(
      h(
        'tr',
        { class: x.acked == null ? 'unacked' : '' },
        h('td', { class: 'mono' }, hitWhen(x.ts, NOW())),
        h('td', {}, x.rule_name),
        h('td', {}, h('span', { class: 'pbadge', '--pc': color(x.profile) }, x.profile)),
        h(
          'td',
          {},
          acTag(x),
          h('a', { class: 'mono', href: links.log, title: 'Open in the log' }, x.subject || '—'),
          hasMap(x.profile) && x.subject ? h('a', { class: 'btn sm ghost maplink', href: links.map }, 'Map') : null,
        ),
        h('td', {}, x.label || '—'),
        h('td', { class: 'mono wc' }, x.detail),
        h('td', { class: 'muted' }, x.webhook || '—'),
        h('td', {}, state),
      ),
    );
  }
}

async function ack(ids) {
  try {
    const out = await post('/api/alerts/ack', ids ? { ids } : { all: true });
    if (S.status) S.status.alerts = { ...S.status.alerts, ...out.summary };
    paintAlerts(out.summary, color, hasMap);
    await Promise.all([loadHits(), loadRules()]);
  } catch (e) {
    showError(e.message);
  }
}

init().catch((e) => showError(`Could not start: ${e.message}`));
