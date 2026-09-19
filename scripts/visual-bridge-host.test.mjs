import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { createVisualHost, createInAppBrowserHost, browserAdapter } from './visual-bridge-host.mjs';
import { ControlNotReady } from './browser-form-state.mjs';

test('an unobservable target is never advertised as an active host', async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'applypilot-visual-probe-'));
  await assert.rejects(createVisualHost({
    directory, target: { tab_id: 'fixture' },
    adapter: { surface: 'computer_use', observe: async () => { throw Error('URL observation unavailable'); } },
  }), /URL observation unavailable/);
  await assert.rejects(fs.readFile(path.join(directory, 'host.json')), { code: 'ENOENT' });
});

test('IAB review, pause and resume keep the actual tab; navigation uses observed links only', async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'applypilot-iab-'));
  let current = 'https://jobs.example.test/job/1';
  let snapshot = '- heading "Job 1"\n- link "Guest":\n  - /url: /apply/1';
  const tab = {
    id: 'actual-7', url: async () => current, title: async () => 'Job 1',
    goto: async url => { current = url; snapshot = '- textbox "Name"'; },
    dom_cua: { get_visible_dom: async () => '<a node_id=1 href="/apply/1">Guest</a>' },
    playwright: { domSnapshot: async () => snapshot },
  };
  const host = await createInAppBrowserHost({ directory, tab });
  assert.deepEqual(host.binding.target, { runtime: 'iab', tab_id: tab.id, application_url: current });
  await assert.rejects(createInAppBrowserHost({ directory: directory + '-other', tab }), /active owner/);
  async function request(operation, observation_id, args = {}) {
    const request_id = randomUUID();
    await fs.writeFile(path.join(directory, 'pending', `${request_id}.json`), JSON.stringify({
      ...host.binding, request_id, operation, observation_id, arguments: args, deadline_at: Date.now() / 1000 + 30,
    }));
    return host.execute(request_id);
  }
  const observation = await request('observe');
  await host.pause('auth_required');
  assert.equal(JSON.parse(await fs.readFile(path.join(directory, 'host.json'))).status, 'paused');
  await host.resume();
  assert.equal((await request('navigate', observation.observation_id, { url: 'https://jobs.example.test/apply/1' })).ok, false);
  const fresh = await request('observe');
  assert.equal((await request('navigate', fresh.observation_id, { url: 'https://jobs.example.test/apply/1' })).ok, true);
  const review = await host.inspect();
  assert.equal(review.target.tab_id, tab.id);
  assert.equal(JSON.parse(review.content[0].text).page_url, 'https://jobs.example.test/apply/1');
  assert.equal(review.submission_authorized, false);
  assert.match(review.content[2].text, /Name/);
  const adapter = browserAdapter(tab);
  await adapter.observe();
  await assert.rejects(adapter.act('navigate', { url: 'https://unobserved.example.test' }), /current observation/);
  await host.close();
});

test('host rejects stale observations and stops after an interrupted input', async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'applypilot-visual-host-'));
  let inputs = 0;
  const adapter = {
    surface: 'browser',
    observe: async () => [{ type: 'text', text: 'fixture state' }],
    act: async () => { inputs++; throw Error('runtime stopped'); },
  };
  const host = await createVisualHost({ directory, adapter, target: { tab_id: 'fixture' } });
  async function request(operation, observation_id) {
    const request_id = randomUUID();
    await fs.writeFile(path.join(directory, 'pending', `${request_id}.json`), JSON.stringify({
      ...host.binding, request_id, operation, observation_id, arguments: {}, deadline_at: Date.now() / 1000 + 30,
    }));
    return host.execute(request_id);
  }
  const first = await request('observe');
  host.invalidate();
  assert.equal((await request('click', first.observation_id)).ok, false);
  assert.equal(inputs, 0);
  const fresh = await request('observe');
  const interrupted = await request('click', fresh.observation_id);
  assert.equal(interrupted.outcome, 'outcome_unknown');
  assert.equal(inputs, 1);
  await assert.rejects(host.peek(), /closed/);
  assert.equal(JSON.parse(await fs.readFile(path.join(directory, 'host.json'))).status, 'stopped');
});

test('targeted text entry focuses an observed input and refuses action controls', async () => {
  const actions = [];
  const adapter = browserAdapter({
    id: 'input-test', url: async () => 'https://example.test/form', title: async () => 'Form',
    dom_cua: {
      get_visible_dom: async () => '<input node_id=1 required="true" /><input node_id=2 type="submit" />',
      click: async args => actions.push(['click', args]),
      type: async args => actions.push(['type', args]),
    },
    playwright: { domSnapshot: async () => '- textbox "Test name"' },
  });
  await adapter.observe();
  await adapter.act('type_text', { node_id: '1', text: 'Test', mode: 'dom' });
  assert.deepEqual(actions, [['click', { node_id: '1' }], ['type', { text: 'Test' }]]);
  await assert.rejects(adapter.act('type_text', { node_id: '2', text: 'Test' }), /observed text input/);
  assert.equal(actions.length, 2);
});

test('a proven pre-input control rejection invalidates observation but allows recovery', async t => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'applypilot-control-recovery-'));
  const host = await createVisualHost({ directory, target: { tab_id: 'recovery' }, adapter: {
    surface: 'browser', observe: async () => [{ type: 'text', text: 'fixture' }],
    act: async () => { throw new ControlNotReady('Options changed; observe again'); },
  } });
  t.after(async () => { await host.close(); await fs.rm(directory, { recursive: true, force: true }); });
  async function request(operation, observation_id) {
    const request_id = randomUUID();
    await fs.writeFile(path.join(directory, 'pending', `${request_id}.json`), JSON.stringify({
      ...host.binding, request_id, operation, observation_id, arguments: {}, deadline_at: Date.now() / 1000 + 30,
    }));
    return host.execute(request_id);
  }
  const observed = await request('observe');
  const rejected = await request('select_control', observed.observation_id);
  assert.equal(rejected.ok, false);
  assert.equal(rejected.outcome, 'failed');
  assert.equal(JSON.parse(await fs.readFile(path.join(directory, 'host.json'))).status, 'active');
  const fresh = await request('observe');
  assert.equal(fresh.ok, true);
  assert.notEqual(fresh.observation_id, observed.observation_id);
});

test('submit attachment requires explicit authorization and inspection preserves it', async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'applypilot-submit-'));
  const tab = {
    id: 'submit-tab', url: async () => 'https://example.test/apply', title: async () => 'Apply',
    dom_cua: { get_visible_dom: async () => '<button node_id=1>Submit</button>' },
    playwright: { domSnapshot: async () => '- button "Submit"' },
  };
  for (const authorization of [false, 'true', 1]) {
    await assert.rejects(createInAppBrowserHost({ directory, tab, phase: 'submit', submission_authorized: authorization }), /explicit/);
  }
  await assert.rejects(createInAppBrowserHost({ directory, tab, phase: 'prepare', submission_authorized: true }), /requires submit phase/);
  const host = await createInAppBrowserHost({ directory, tab, phase: 'submit', submission_authorized: true });
  assert.equal(host.binding.submission_authorized, true);
  assert.equal((await host.inspect()).submission_authorized, true);
  assert.equal(JSON.parse(await fs.readFile(path.join(directory, 'host.json'))).submission_authorized, true);
  await host.close();
});

test('artifact upload registers chooser before click and exposes only artifact references', async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'applypilot-upload-'));
  const file = path.join(directory, 'resume.pdf');
  await fs.writeFile(file, 'fixture');
  const calls = [];
  const adapter = browserAdapter({
    id: 'upload-tab', url: async () => 'https://example.test/apply', title: async () => 'Apply',
    dom_cua: {
      get_visible_dom: async () => '<button node_id=7>Upload resume</button>',
      click: async args => calls.push(['click', args]),
    },
    playwright: {
      domSnapshot: async () => '- button "Upload resume"',
      waitForEvent: (event, options) => {
        calls.push(['wait', event, options]);
        return Promise.resolve({ setFiles: async files => calls.push(['files', files]) });
      },
    },
  }, { artifacts: { resume: file, directory, missing: path.join(directory, 'absent.pdf') } });
  const observation = await adapter.observe();
  assert.deepEqual(JSON.parse(observation[0].text).artifact_ids, ['resume', 'directory', 'missing']);
  assert.equal(JSON.stringify(observation).includes(file), false);
  await assert.rejects(adapter.act('upload_artifact', { artifact_id: 'unknown', node_id: '7' }), /Unknown artifact/);
  await assert.rejects(adapter.act('upload_artifact', { artifact_id: 'resume', node_id: '8' }), /current DOM observation/);
  await assert.rejects(adapter.act('upload_artifact', { artifact_id: 'directory', node_id: '7' }), /regular file/);
  await assert.rejects(adapter.act('upload_artifact', { artifact_id: 'missing', node_id: '7' }), { code: 'ENOENT' });
  assert.deepEqual(calls, []);
  await adapter.act('upload_artifact', { artifact_id: 'resume', node_id: '7' });
  assert.deepEqual(calls, [
    ['wait', 'filechooser', { timeoutMs: 10000 }], ['click', { node_id: '7' }], ['files', [file]],
  ]);
});

test('failed chooser click does not leave an unhandled waiter rejection', async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'applypilot-chooser-error-'));
  const file = path.join(directory, 'resume.pdf');
  await fs.writeFile(file, 'fixture');
  let rejectWaiter;
  const adapter = browserAdapter({
    id: 'chooser-error', url: async () => 'https://example.test/apply', title: async () => 'Apply',
    dom_cua: {
      get_visible_dom: async () => '<button node_id=7>Upload</button>',
      click: async () => { throw Error('click stopped'); },
    },
    playwright: {
      domSnapshot: async () => '- button "Upload"',
      waitForEvent: () => new Promise((resolve, reject) => { rejectWaiter = reject; }),
    },
  }, { artifacts: { resume: file } });
  await adapter.observe();
  await assert.rejects(adapter.act('upload_artifact', { artifact_id: 'resume', node_id: '7' }), /click stopped/);
  rejectWaiter(Error('chooser timeout'));
  await new Promise(resolve => setImmediate(resolve));
});
