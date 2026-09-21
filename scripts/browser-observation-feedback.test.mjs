import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { browserAdapter, createInAppBrowserHost } from './visual-bridge-host.mjs';
import { BridgeMetrics, canReuseActionReadback, observationCoverage, observationSizes,
  queueWaitMs, ACTION_READBACK_MAX_AGE_MS } from './browser-observation-feedback.mjs';

function fixture() {
  const state = {
    page_url: 'https://example.test/apply/1', protected_count: 0,
    coverage: { scope: 'visible_top_document_open_shadow', open_shadow_count: 0, iframe_count: 0 },
    fields: ['name', 'city'].map(name => ({ field_key: name, selector: `#${name}`, label: name,
      group: '', group_key: '', control: 'text', value: '', required: true, disabled: false,
      readonly: false, invalid: false, validation_message: '', options: [], files: [] })),
  };
  const counts = { dom: 0, snapshot: 0, form: 0, writes: 0 };
  const hooks = {};
  const tab = {
    id: randomUUID(), url: async () => state.page_url, title: async () => 'Application',
    screenshot: async () => Buffer.from('synthetic-image'),
    goto: async url => { state.page_url = url; },
    dom_cua: {
      get_visible_dom: async () => {
        counts.dom++; await hooks.dom?.();
        return '<input node_id=1 type="text" /><button node_id=7>Upload</button><div role="alert">Visible feedback</div>';
      },
      click: async () => { counts.writes++; }, type: async () => { counts.writes++; },
    },
    playwright: {
      domSnapshot: async () => { counts.snapshot++; return '- textbox "name"\n' + 'snapshot-only-text '.repeat(300); },
      evaluate: async () => { counts.form++; await hooks.form?.(); return structuredClone(state); },
      locator: selector => ({
        count: async () => 1,
        fill: async value => {
          counts.writes++;
          state.fields.find(field => field.selector === selector).value = value;
          await hooks.fill?.();
        },
        press: async () => { await hooks.blur?.(); },
        setChecked: async value => {
          counts.writes++; state.fields.find(field => field.selector === selector).checked = value;
        },
        selectOption: async ({ value }) => {
          counts.writes++; state.fields.find(field => field.selector === selector).value = value;
        },
      }),
      waitForEvent: async () => ({ setFiles: async () => { await hooks.upload?.(); } }),
    },
  };
  return { state, tab, counts, hooks };
}
const parsed = content => content.flatMap(block => {
  try { return block.type === 'text' ? [JSON.parse(block.text)] : []; } catch { return []; }
});
const formBlock = content => parsed(content).find(item => item.form_state);
async function adapterFixture() {
  const f = fixture();
  f.adapter = browserAdapter(f.tab, { reuseFormObservations: true });
  await f.adapter.observe();
  return f;
}
async function operate(f, args = { field_key: 'name', value: 'Candidate' }) {
  const ticket = await f.adapter.act('fill_control', args);
  return { ticket, content: await f.adapter.observe({ actionResult: ticket }) };
}
async function hostFixture(t, options = {}) {
  const f = fixture();
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'applypilot-feedback-'));
  const host = await createInAppBrowserHost({ directory, tab: f.tab, ...options });
  t.after(async () => { await host.close(); await fs.rm(directory, { recursive: true, force: true }); });
  async function request(operation, observation_id, args = {}) {
    const request_id = randomUUID();
    await fs.writeFile(path.join(directory, 'pending', `${request_id}.json`), JSON.stringify({
      ...host.binding, request_id, operation, observation_id, arguments: args,
      created_at: Date.now() / 1000 - 0.2, deadline_at: Date.now() / 1000 + 30,
    }));
    return host.execute(request_id);
  }
  return { ...f, host, request, directory };
}

test('one successful action retains visible DOM but removes duplicate form and snapshot reads', async () => {
  const f = await adapterFixture();
  const before = { ...f.counts };
  const { content } = await operate(f);
  assert.equal(f.counts.form - before.form, 2); // pre-write check plus immediate readback
  assert.equal(f.counts.snapshot - before.snapshot, 0);
  assert.equal(f.counts.dom - before.dom, 1);
  assert.match(content[1].text, /Visible feedback/);
  assert.equal(formBlock(content).form_state.fields[0].value, 'Candidate');
  assert.equal(formBlock(content).observation_feedback.form_readback_reused, true);
  assert.equal(formBlock(content).changed_fields[0].requires_fact_check, true);
});

test('legacy browser adapter keeps its full observation behavior', async () => {
  const f = fixture(); f.adapter = browserAdapter(f.tab);
  await f.adapter.observe(); const before = { ...f.counts };
  const { content } = await operate(f);
  assert.equal(f.counts.form - before.form, 3);
  assert.equal(f.counts.snapshot - before.snapshot, 1);
  assert.equal(formBlock(content).observation_feedback, undefined);
});

test('readback is one-shot; explicit observe sees later asynchronous field changes', async () => {
  const f = await adapterFixture();
  const { ticket } = await operate(f);
  f.state.fields[0].value = 'Late parser overwrite';
  const content = await f.adapter.observe({ actionResult: ticket });
  assert.equal(formBlock(content).observation_feedback.form_readback_reused, false);
  assert.equal(formBlock(content).form_state.fields[0].value, 'Late parser overwrite');
});

test('a second semantic write still independently reads control identity', async () => {
  const f = await adapterFixture(); await operate(f);
  f.state.fields[0].label = 'Changed meaning';
  await assert.rejects(f.adapter.act('fill_control', { field_key: 'name', value: 'Do not write' }), /changed/);
  assert.equal(f.counts.writes, 1);
});

for (const [name, change] of [
  ['new required field', state => state.fields.push({ ...state.fields[0], field_key: 'new', selector: '#new' })],
  ['removed field', state => state.fields.pop()],
  ['validation error', state => { state.fields[0].invalid = true; }],
  ['unknown value', state => { state.fields[1].value_source = 'unavailable'; state.fields[1].value = null; }],
  ['changed options', state => { state.fields[1].options = [{ value: 'new' }]; }],
  ['new iframe', state => { state.coverage.iframe_count = 1; }],
  ['new protected field', state => { state.protected_count = 1; }],
  ['page transition', state => { state.page_url = 'https://example.test/apply/2'; }],
]) {
  test(`${name} forces full observation rather than reused feedback`, async () => {
    const f = await adapterFixture(); f.hooks.blur = () => change(f.state);
    const before = f.counts.snapshot;
    const { content } = await operate(f);
    assert.equal(f.counts.snapshot, before + 1);
    assert.equal(formBlock(content).observation_feedback.form_readback_reused, false);
  });
}

test('dependent values remain in full structured state and change feedback', async () => {
  const f = await adapterFixture(); f.hooks.blur = () => { f.state.fields[1].value = 'Parser city'; };
  const { content } = await operate(f);
  const block = formBlock(content);
  assert.equal(block.form_state.fields.length, 2);
  assert.deepEqual(block.changed_fields.map(field => field.field_key), ['name', 'city']);
});

test('screenshots consume but do not reuse action readbacks', async () => {
  const f = await adapterFixture();
  const ticket = await f.adapter.act('fill_control', { field_key: 'name', value: 'Candidate' });
  const shot = await f.adapter.observe({ mode: 'screenshot', actionResult: ticket });
  assert.equal(shot[1].type, 'image');
  const content = await f.adapter.observe({ actionResult: ticket });
  assert.equal(formBlock(content).observation_feedback.form_readback_reused, false);
});

test('reused feedback never crosses adapter instances, even at the same URL', async () => {
  const a = await adapterFixture(); const b = await adapterFixture();
  const ticket = await a.adapter.act('fill_control', { field_key: 'name', value: 'A' });
  const content = await b.adapter.observe({ actionResult: ticket });
  assert.equal(formBlock(content).form_state.fields[0].value, '');
  assert.equal(formBlock(content).observation_feedback.form_readback_reused, false);
});

test('age bound and private identity reject expired, future and copied tickets', () => {
  const { state } = fixture();
  const pending = { before: state, form: structuredClone(state), observedAt: 10,
    report: { persisted: true, invalid: false } };
  assert.equal(canReuseActionReadback(pending, pending, state.page_url, 10), true);
  assert.equal(canReuseActionReadback(pending, { ...pending }, state.page_url, 10), false);
  assert.equal(canReuseActionReadback(pending, pending, state.page_url, 9), false);
  assert.equal(canReuseActionReadback(pending, pending, state.page_url, 11 + ACTION_READBACK_MAX_AGE_MS), false);
  assert.equal(canReuseActionReadback(pending, pending, 'https://other.test', 10), false);
});

test('navigation while reading visible DOM prevents reuse', async () => {
  const f = await adapterFixture();
  const ticket = await f.adapter.act('fill_control', { field_key: 'name', value: 'Candidate' });
  f.hooks.dom = () => { f.state.page_url = 'https://example.test/different'; };
  await assert.rejects(f.adapter.observe({ actionResult: ticket }), /Page changed during observation/);
});

test('upload always observes again and later feedback preserves upload deltas', async t => {
  const f = fixture();
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'applypilot-feedback-upload-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const resume = path.join(directory, 'resume.pdf'); await fs.writeFile(resume, 'synthetic');
  f.adapter = browserAdapter(f.tab, { reuseFormObservations: true, artifacts: { resume } });
  await f.adapter.observe();
  f.hooks.upload = () => { f.state.fields[1].value = 'Reparsed city'; };
  await f.adapter.act('upload_artifact', { artifact_id: 'resume', node_id: '7' });
  const uploaded = formBlock(await f.adapter.observe());
  assert.equal(uploaded.observation_feedback.form_readback_reused, false);
  const { content } = await operate(f);
  assert.ok(formBlock(content).post_upload_changes.some(field => field.field_key === 'city'));
});

test('coverage never equates partial inspection or missing metadata with completeness', () => {
  const f = fixture();
  assert.equal(observationCoverage(f.state).all_steps, 'unverified');
  assert.equal(observationCoverage(f.state).frame_contents, 'not_inspected');
  assert.equal(observationCoverage(undefined).iframe_count, null);
  assert.equal(observationCoverage(undefined).field_count, null);
});

test('host wires prepare reuse and full inspection without changing submission authority', async t => {
  const f = await hostFixture(t);
  const first = await f.request('observe');
  const filled = await f.request('fill_control', first.observation_id, { field_key: 'name', value: 'Candidate' });
  assert.equal(filled.ok, true);
  assert.equal(formBlock(filled.content).observation_feedback.form_readback_reused, true);
  const before = f.counts.form; const review = await f.host.inspect();
  assert.equal(f.counts.form, before + 1);
  assert.equal(formBlock(review.content).observation_feedback.form_readback_reused, false);
  assert.equal(review.submission_authorized, false);
  const stale = await f.request('fill_control', filled.observation_id, { field_key: 'city', value: 'No' });
  assert.equal(stale.ok, false);
  assert.equal(f.counts.writes, 1);
});

for (const options of [
  { phase: 'discovery' }, { phase: 'submit', submission_authorized: true }, { reuseFormObservations: false },
]) {
  test(`no reuse in ${JSON.stringify(options)}`, async t => {
    const f = await hostFixture(t, options);
    const first = await f.request('observe');
    const before = f.counts.form;
    const result = await f.request('fill_control', first.observation_id, { field_key: 'name', value: 'Candidate' });
    assert.equal(result.ok, true);
    assert.equal(f.counts.form - before, 3);
    assert.equal(formBlock(result.content).observation_feedback, undefined);
  });
}

test('pause/resume invalidates old observations and re-reads the same tab', async t => {
  const f = await hostFixture(t); const first = await f.request('observe');
  await f.host.pause(); f.state.fields[0].value = 'Manual change'; await f.host.resume();
  const stale = await f.request('fill_control', first.observation_id, { field_key: 'name', value: 'No' });
  assert.equal(stale.ok, false); assert.equal(f.counts.writes, 0);
  const fresh = await f.request('observe');
  assert.equal(formBlock(fresh.content).form_state.fields[0].value, 'Manual change');
});

test('post-input failure still stops the host with outcome_unknown, not a replay invitation', async t => {
  const f = await hostFixture(t); const first = await f.request('observe');
  f.hooks.fill = () => { throw Error('Driver connection lost after input'); };
  const result = await f.request('fill_control', first.observation_id, { field_key: 'name', value: 'Candidate' });
  assert.equal(result.outcome, 'outcome_unknown');
  await assert.rejects(f.host.peek(), /closed/);
  assert.equal(f.host.metrics().lifetime.outcome_unknown, 1);
});

test('host metrics distinguish queue, action and observation and contain no applicant text', async t => {
  const f = await hostFixture(t); const first = await f.request('observe');
  await f.request('fill_control', first.observation_id, { field_key: 'name', value: 'PRIVATE-APPLICANT' });
  const metrics = f.host.metrics();
  assert.equal(metrics.lifetime.operations, 2); assert.equal(metrics.lifetime.form_readbacks_reused, 1);
  assert.equal(metrics.recent.action_ms.unavailable, 1);
  assert.ok(metrics.recent.queue_wait_ms.p50 >= 200);
  assert.ok(metrics.recent.text_bytes.p50 > 0);
  assert.equal(metrics.model_time, 'unavailable');
  assert.equal(metrics.submission_success, 'not_measured');
  assert.doesNotMatch(JSON.stringify(metrics), /PRIVATE-APPLICANT|example\.test/);
});

test('metrics retain a bounded window, allowlist fields and preserve unknowns', () => {
  const metrics = new BridgeMetrics(2);
  metrics.record({ outcome: 'completed', action_ms: 10, secret: 'DO-NOT-RETAIN' });
  metrics.record({ outcome: 'failed', action_ms: 20 });
  metrics.record({ outcome: 'outcome_unknown', action_ms: NaN });
  const result = metrics.snapshot();
  assert.equal(result.lifetime.operations, 3); assert.equal(result.window_size, 2);
  assert.equal(result.recent.action_ms.p95, 20); assert.equal(result.recent.action_ms.unavailable, 1);
  assert.equal(result.recent.queue_wait_ms.p95, null);
  assert.doesNotMatch(JSON.stringify(result), /DO-NOT-RETAIN/);
  result.lifetime.operations = 999; assert.equal(metrics.snapshot().lifetime.operations, 3);
  assert.throws(() => new BridgeMetrics(0)); assert.throws(() => new BridgeMetrics(1025));
});

test('queue wait treats unavailable, invalid and future clocks as unknown', () => {
  assert.equal(queueWaitMs(1, 2), 1000);
  for (const value of [undefined, null, '1', NaN, Infinity, -1, 3]) assert.equal(queueWaitMs(value, 2), null);
});

test('payload metrics count UTF-8 and binary bytes, not tokens or encoded image characters', () => {
  const result = observationSizes([{ type: 'text', text: '中' }, { type: 'image', data: 'aGk=' }]);
  assert.equal(result.text_bytes, 3); assert.equal(result.image_bytes, 2);
});

test('same-page host exclusion remains active', async t => {
  const f = await hostFixture(t);
  await assert.rejects(createInAppBrowserHost({ directory: f.directory + '-other', tab: f.tab }), /active owner/);
});

test('expired readback followed by a parser overwrite does not report current persistence', async () => {
  const f = await adapterFixture();
  const ticket = await f.adapter.act('fill_control', { field_key: 'name', value: 'Candidate' });
  ticket.observedAt -= ACTION_READBACK_MAX_AGE_MS + 1;
  f.state.fields[0].value = 'Later value';
  const content = await f.adapter.observe({ actionResult: ticket });
  assert.equal(formBlock(content).control_result.persisted, null);
  assert.equal(formBlock(content).form_state.fields[0].value, 'Later value');
});

test('explicit refresh does not carry an old control result', async () => {
  const f = await adapterFixture();
  await f.adapter.act('fill_control', { field_key: 'name', value: 'Candidate' });
  const content = await f.adapter.observe();
  assert.equal(formBlock(content).control_result, null);
  assert.equal(formBlock(content).observation_feedback.form_readback_reused, false);
});
