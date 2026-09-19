import assert from 'node:assert/strict';
import test from 'node:test';
import { changedFields, operateObservedControl, ControlNotReady, enrichLiveValues } from './browser-form-state.mjs';

const field = (overrides = {}) => ({ field_key: '#name', selector: '#name', label: 'Name', group_key: '#education1',
  group: 'Education 1', control: 'text', value: 'Old', checked: null, selected_display: [], options: [], ...overrides });
const snapshot = (...fields) => ({ page_url: 'https://fixture.test/apply', fields });

function runtime(initial, afterInput = value => value) {
  const state = structuredClone(initial);
  const writes = [];
  const locator = selector => ({
    count: async () => state.fields.filter(f => f.selector === selector || f.options.some(o => o.selector === selector)).length,
    fill: async value => { writes.push(['fill', selector, value]); state.fields.find(f => f.selector === selector).value = afterInput(value); },
    press: async key => { writes.push(['press', key]); },
    selectOption: async ({ value }) => { writes.push(['select', value]); state.fields.find(f => f.selector === selector).value = value; },
    setChecked: async value => { writes.push(['checked', value]); state.fields.find(f => f.selector === selector).checked = value; },
    click: async () => {
      const f = state.fields.find(f => f.options.some(o => o.selector === selector));
      const option = f.options.find(o => o.selector === selector);
      f.value = f.control === 'combobox' ? '' : option.value;
      if (f.control === 'combobox') f.selected_display = f.selected_display?.length ? [...f.selected_display, option.label] : [option.label];
      writes.push(['click', selector]);
    },
  });
  return { state, writes, tab: { playwright: { evaluate: async () => structuredClone(state), locator } } };
}

test('replacement commits blur and reports persistence; rollback is not success', async () => {
  for (const rollback of [false, true]) {
    const before = snapshot(field());
    const r = runtime(before, value => rollback ? '' : value);
    const result = await operateObservedControl(r.tab, before, 'fill_control', { field_key: '#name', value: 'New' });
    assert.equal(result.persisted, !rollback);
    assert.deepEqual(r.writes, [['fill', '#name', 'New'], ['press', 'Tab']]);
  }
});

test('same labels in separate education rows target only the observed group', async () => {
  const before = snapshot(field(), field({ field_key: '#name2', selector: '#name2', group_key: '#education2', group: 'Education 2' }));
  const r = runtime(before);
  await operateObservedControl(r.tab, before, 'fill_control', { field_key: '#name2', value: 'Second school' });
  assert.equal(r.state.fields[0].value, 'Old');
  assert.equal(r.state.fields[1].value, 'Second school');
});

test('native date segments advance until blur before reporting validation', async () => {
  const before = snapshot(field({ control: 'date', focused: true }));
  const r = runtime(before);
  let tabs = 0;
  const locator = r.tab.playwright.locator;
  r.tab.playwright.locator = selector => ({ ...locator(selector), press: async () => {
    tabs++;
    if (tabs === 3) { r.state.fields[0].focused = false; r.state.fields[0].invalid = true; }
  } });
  const result = await operateObservedControl(r.tab, before, 'fill_control', { field_key: '#name', value: '2026-01-01' });
  assert.equal(tabs, 3);
  assert.equal(result.persisted, true);
  assert.equal(result.invalid, true);
});

test('changed identity, protected/missing, readonly and ambiguous controls reject before input', async () => {
  for (const change of [s => { s.page_url += '/other'; }, s => { s.fields[0].group = 'Other education'; },
    s => { s.fields = []; }, s => { s.fields[0].readonly = true; }, s => { s.fields.push(structuredClone(s.fields[0])); }]) {
    const before = snapshot(field());
    const r = runtime(before);
    change(r.state);
    await assert.rejects(operateObservedControl(r.tab, before, 'fill_control', { field_key: '#name', value: 'New' }), ControlNotReady);
    assert.equal(r.writes.length, 0);
  }
});

test('native and custom options use observed options; async drift needs fresh observation', async () => {
  for (const control of ['select', 'combobox']) {
    const before = snapshot(field({ control, options: [{ value: 'sg', label: 'Singapore', selector: '#sg' }] }));
    const r = runtime(before);
    const result = await operateObservedControl(r.tab, before, 'select_control', { field_key: '#name', value: 'Singapore' });
    assert.equal(result.persisted, true);
    r.state.fields[0].options.push({ value: 'new', label: 'New' });
    const count = r.writes.length;
    await assert.rejects(operateObservedControl(r.tab, before, 'select_control', { field_key: '#name', value: 'Singapore' }), /Options changed/);
    assert.equal(r.writes.length, count);
  }
});

test('single React Select combobox persists by its unique selected display label', async () => {
  const before = snapshot(field({ control: 'combobox', value: '', selected_display: [], options: [
    { value: 'sg', label: 'Singapore', selector: '#sg' },
  ], value_source: 'unavailable' }));
  const r = runtime(before);
  const result = await operateObservedControl(r.tab, before, 'select_control', { field_key: '#name', value: 'sg' });
  assert.equal(result.persisted, true);
  assert.deepEqual(r.state.fields[0].selected_display, ['Singapore']);
  assert.equal(r.state.fields[0].value, '');
});

test('multi-select combobox persistence stays unknown under ambiguous replace semantics', async () => {
  const before = snapshot(field({ control: 'combobox', value: '', selected_display: ['China'], options: [
    { value: 'sg', label: 'Singapore', selector: '#sg' },
  ] }));
  const r = runtime(before);
  const result = await operateObservedControl(r.tab, before, 'select_control', { field_key: '#name', value: 'sg' });
  assert.equal(result.persisted, null);
  assert.deepEqual(r.state.fields[0].selected_display, ['China', 'Singapore']);
});

test('upload deltas retain prior values as untrusted observations, excluding new/deleted/rebound fields', () => {
  const before = snapshot(field(), field({ field_key: '#phone', selector: '#phone', value: '+65 90000000' }));
  const after = snapshot(field({ value: 'Parser value' }), field({ field_key: '#new', selector: '#new', value: 'New' }));
  const delta = changedFields(before, after);
  assert.equal(delta.length, 1);
  assert.equal(delta[0].previous_value, 'Old');
  assert.equal(delta[0].observed_value, 'Parser value');
  assert.equal(delta[0].requires_fact_check, true);
  assert.deepEqual(changedFields(before, { ...after, page_url: 'https://other.test' }), []);
});

test('changedFields reports combobox selected-display changes even when value is blank or unavailable', () => {
  const before = snapshot(field({ control: 'combobox', value: '', selected_display: ['Old'], value_source: 'unavailable' }));
  const after = snapshot(field({ control: 'combobox', value: '', selected_display: ['Singapore'], value_source: 'unavailable' }));
  const delta = changedFields(before, after);
  assert.equal(delta.length, 1);
  assert.equal(delta[0].previous_value, 'Old');
  assert.equal(delta[0].observed_value, 'Singapore');
  assert.equal(delta[0].requires_fact_check, true);
});

test('IAB DOM snapshot supplies live input values; unavailable values never imply empty input', () => {
  const form = snapshot(field({ selector: '[id="name"]' }), field({ field_key: '#missing', selector: '#missing' }));
  enrichLiveValues(form, { strings: [form.page_url, 'HTML', 'INPUT', 'id', 'name', 'Live value'], documents: [{
    documentURL: 0, nodes: { nodeType: [1, 1], nodeName: [1, 2], parentIndex: [-1, 0], attributes: [[], [3, 4]], inputValue: { index: [1], value: [5] } },
  }] });
  assert.equal(form.fields[0].value, 'Live value');
  assert.equal(form.fields[0].value_source, 'live_dom_snapshot');
  assert.equal(form.fields[1].value, null);
  assert.equal(form.fields[1].value_source, 'unavailable');
  assert.throws(() => enrichLiveValues(form, { strings: [], documents: [] }), /document changed/);
});
