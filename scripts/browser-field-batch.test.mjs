import assert from 'node:assert/strict';
import test from 'node:test';
import { operateFieldBatch } from './browser-field-batch.mjs';
import { ControlNotReady } from './browser-form-state.mjs';

function fixture(change = () => {}) {
  const initial = { page_url: 'https://fixture.test/job/1', fields: ['a', 'b'].map(key => ({
    field_key: key, selector: key, label: key, control: 'text', value: '', options: [],
    disabled: false, readonly: false, invalid: false,
  })) };
  const state = structuredClone(initial);
  const writes = [];
  const tab = { playwright: {
    evaluate: async () => structuredClone(state),
    locator: key => ({ count: async () => 1,
      fill: async v => { writes.push(key); state.fields.find(f => f.field_key === key).value = v; change(state); },
      press: async () => {},
    }),
  } };
  const steps = ['a', 'b'].map(key => ({ operation: 'fill_control', field_key: key, value: key.toUpperCase() }));
  return { initial, state, tab, writes, steps };
}

test('batch verifies each field via real operation adapter, using one page owner', async () => {
  const r = fixture();
  const out = await operateFieldBatch(r.tab, r.initial, r.steps);
  assert.equal(out.batch_result.status, 'verified');
  assert.deepEqual(r.writes, ['a', 'b']);
  assert.deepEqual(r.state.fields.map(f => f.value), ['A', 'B']);
});

test('malformed, duplicate or unsupported later steps cause zero writes', async () => {
  for (const alter of [r => r.steps.push(r.steps[0]), r => r.steps[1].operation = 'click',
    r => r.initial.fields[1].control = 'combobox', r => r.steps[1].field_key = 'unseen',
    r => r.initial.fields[1].value_source = 'unavailable']) {
    const r = fixture(); alter(r);
    await assert.rejects(operateFieldBatch(r.tab, r.initial, r.steps), ControlNotReady);
    assert.equal(r.writes.length, 0);
  }
});

test('rollback, dependent value mutation, options drift and navigation park after first input', async () => {
  for (const change of [s => s.fields[0].value = '', s => s.fields[1].value = 'parsed',
    s => s.fields[1].options.push({ value: 'new' }), s => s.page_url += '/next']) {
    const r = fixture(change);
    const out = await operateFieldBatch(r.tab, r.initial, r.steps);
    assert.equal(out.batch_result.status, 'parked');
    assert.equal(out.batch_result.completed, 1);
    assert.deepEqual(r.writes, ['a']);
  }
});

test('prewrite drift after a completed field reports partial progress; unknown outcome propagates', async () => {
  for (const unknown of [false, true]) {
    const r = fixture(); let n = 0;
    const operate = async () => {
      if (n++) throw unknown ? new Error('disconnected after input') : new ControlNotReady('field changed');
      return { observation: r.initial, field_key: 'a', persisted: true, invalid: false };
    };
    if (unknown) await assert.rejects(operateFieldBatch(r.tab, r.initial, r.steps, operate), /disconnected/);
    else assert.equal((await operateFieldBatch(r.tab, r.initial, r.steps, operate)).batch_result.completed, 1);
  }
});
