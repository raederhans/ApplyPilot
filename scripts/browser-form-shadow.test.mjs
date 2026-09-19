import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdtemp, rm } from 'node:fs/promises';
import { homedir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { observeForm, operateObservedControl } from './browser-form-state.mjs';

const fixture = fileURLToPath(new URL('../tests/fixtures/apply/ats_shadow.html', import.meta.url));

function chromiumPath() {
  const root = process.env.PLAYWRIGHT_BROWSERS_PATH || path.join(homedir(), 'AppData', 'Local', 'ms-playwright');
  const candidates = ['chromium-1234', 'chromium-1228', 'chromium-1223', 'chromium-1208'];
  const suffix = process.platform === 'win32' ? path.join('chrome-win64', 'chrome.exe') : path.join('chrome-linux', 'chrome');
  return candidates.map(name => path.join(root, name, suffix)).find(value => existsSync(value));
}

class Cdp {
  constructor(url) {
    this.socket = new WebSocket(url);
    this.nextId = 1;
    this.pending = new Map();
    this.ready = new Promise((resolve, reject) => {
      this.socket.addEventListener('open', resolve, { once: true });
      this.socket.addEventListener('error', event => reject(event.error || Error('CDP WebSocket error')), { once: true });
    });
    this.socket.addEventListener('message', event => {
      const message = JSON.parse(event.data);
      if (!message.id) return;
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      if (message.error) pending.reject(new Error(message.error.message));
      else pending.resolve(message.result);
    });
  }

  async send(method, params = {}, sessionId) {
    await this.ready;
    const id = this.nextId++;
    const message = { id, method, params };
    if (sessionId) message.sessionId = sessionId;
    const result = new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
    this.socket.send(JSON.stringify(message));
    return result;
  }

  async close() {
    if (this.socket.readyState === WebSocket.OPEN) this.socket.close();
  }
}

async function launchFixture({ withSnapshot = true } = {}) {
  const executable = chromiumPath();
  assert.ok(executable, 'A local Playwright Chromium executable is required');
  const userData = await mkdtemp(path.join(process.env.TEMP || '/tmp', 'applypilot-shadow-'));
  const child = spawn(executable, [
    '--headless=new', '--no-sandbox', '--disable-gpu', '--disable-background-networking',
    '--disable-component-update', '--disable-default-apps', '--no-first-run',
    '--allow-file-access-from-files', '--remote-debugging-port=0', `--user-data-dir=${userData}`,
  ], { stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
  const devtoolsUrl = await new Promise((resolve, reject) => {
    let output = '';
    const timer = setTimeout(() => reject(Error(`Chromium did not announce CDP: ${output}`)), 10000);
    const onData = chunk => {
      output += String(chunk);
      const match = output.match(/DevTools listening on (ws:\/\/[^\s]+)/);
      if (match) { clearTimeout(timer); resolve(match[1]); }
    };
    child.stderr.on('data', onData);
    child.stdout.on('data', onData);
    child.once('exit', code => { clearTimeout(timer); reject(Error(`Chromium exited before CDP startup (${code}): ${output}`)); });
  });
  const browser = new Cdp(devtoolsUrl);
  const target = await browser.send('Target.createTarget', { url: `file:///${fixture.replaceAll('\\', '/')}` });
  const attached = await browser.send('Target.attachToTarget', { targetId: target.targetId, flatten: true });
  const sessionId = attached.sessionId;
  await browser.send('Runtime.enable', {}, sessionId);
  await browser.send('DOMSnapshot.enable', {}, sessionId).catch(() => {});

  const evaluate = async (expression, awaitPromise = true) => {
    const result = await browser.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise }, sessionId);
    if (result.exceptionDetails) throw Error(result.exceptionDetails.text || 'Runtime evaluation failed');
    return result.result?.value;
  };
  const tab = {
    playwright: {
      evaluate: fn => evaluate(`(${fn.toString()})()`),
      locator: selector => locatorFor(evaluate, selector),
    },
  };
  if (withSnapshot) {
    tab.capabilities = { get: async name => name === 'cdp' ? { send: (method, params) => browser.send(method, params, sessionId) } : null };
  }
  await evaluate('document.readyState === "complete" ? true : new Promise(resolve => addEventListener("load", () => resolve(true), { once: true }))');
  return { browser, child, userData, tab, evaluate };
}

async function closeFixture(runtime) {
  await runtime.browser.close();
  if (!runtime.child.killed) runtime.child.kill();
  await new Promise(resolve => {
    if (runtime.child.exitCode !== null) resolve();
    else runtime.child.once('exit', resolve);
  });
  await new Promise(resolve => setTimeout(resolve, 100));
  await rm(runtime.userData, { recursive: true, force: true });
}

function locatorFor(evaluate, selector) {
  const args = JSON.stringify(selector);
  const resolveOne = `(() => {
    const wanted = ${args};
    const roots = [document];
    for (let i = 0; i < roots.length; i++) {
      for (const el of roots[i].querySelectorAll('*')) if (el.shadowRoot) roots.push(el.shadowRoot);
    }
    const found = new Set();
    for (const root of roots) {
      for (const candidate of root.querySelectorAll(wanted)) found.add(candidate);
      // Production selectors cross open shadow boundaries with a space-separated
      // host path. The last segment is sufficient to identify this fixture's
      // unique observed control while retaining ordinary CSS semantics first.
      const last = wanted.trim().split(/\\s+/).at(-1);
      if (last && last !== wanted) for (const candidate of root.querySelectorAll(last)) found.add(candidate);
    }
    return [...found];
  })()`;
  return {
    count: async () => evaluate(`${resolveOne}.length`),
    fill: async value => evaluate(`(() => { const nodes = ${resolveOne}; if (nodes.length !== 1) throw Error('locator fill ambiguous'); const el = nodes[0]; el.focus(); const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set || Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set; if (setter) setter.call(el, ${JSON.stringify(value)}); else el.value = ${JSON.stringify(value)}; el.dispatchEvent(new Event('input', { bubbles: true, composed: true })); el.dispatchEvent(new Event('change', { bubbles: true, composed: true })); })()`),
    press: async key => evaluate(`(() => { const nodes = ${resolveOne}; if (nodes.length !== 1) throw Error('locator press ambiguous'); const el = nodes[0]; el.dispatchEvent(new KeyboardEvent('keydown', { key: ${JSON.stringify(key)}, bubbles: true, composed: true })); if (${JSON.stringify(key)} === 'Tab') el.blur(); el.dispatchEvent(new KeyboardEvent('keyup', { key: ${JSON.stringify(key)}, bubbles: true, composed: true })); })()`),
    selectOption: async ({ value }) => evaluate(`(() => { const nodes = ${resolveOne}; if (nodes.length !== 1) throw Error('locator select ambiguous'); const el = nodes[0]; el.value = ${JSON.stringify(value)}; el.dispatchEvent(new Event('change', { bubbles: true, composed: true })); })()`),
    setChecked: async value => evaluate(`(() => { const nodes = ${resolveOne}; if (nodes.length !== 1) throw Error('locator check ambiguous'); const el = nodes[0]; el.checked = ${Boolean(value)}; el.dispatchEvent(new Event('change', { bubbles: true, composed: true })); })()`),
    click: async () => evaluate(`(() => { const nodes = ${resolveOne}; if (nodes.length !== 1) throw Error('locator click ambiguous'); nodes[0].click(); })()`),
  };
}

test('observeForm traverses nested open roots and keeps duplicate and anonymous controls stable', async t => {
  const runtime = await launchFixture();
  t.after(() => closeFixture(runtime));
  const form = await observeForm(runtime.tab);
  assert.equal(form.coverage.scope, 'visible_top_document_open_shadow');
  assert.ok(form.coverage.open_shadow_count >= 4);
  assert.equal(form.protected_count, 2);
  assert.equal(form.fields.filter(field => field.label === 'Full name').length, 1);
  const name = form.fields.find(field => field.label === 'Full name');
  assert.match(name.field_key, /input\[id="duplicate-id"\]/);
  assert.equal(name.field_key, 'input[id="duplicate-id"]');
  const dates = form.fields.filter(field => field.control === 'date');
  assert.equal(dates.length, 2);
  assert.notEqual(dates[0].field_key, dates[1].field_key);
  const topNote = form.fields.find(field => field.field_key === '[id="top-note"]');
  assert.equal(topNote.value, 'visible');
  assert.equal(topNote.value_source, 'live_dom_snapshot');
  // DOMSnapshot flattens shadow descendants; an anonymous shadow selector
  // that cannot be reconciled is reported as unknown rather than as empty.
  assert.ok(dates.every(field => field.value === null && field.value_source === 'unavailable'));
  assert.equal(form.fields.some(field => field.label === 'Account password'), false);
  assert.equal(form.fields.some(field => field.label === 'One-time code'), false);
});

test('operateObservedControl fills the second anonymous date in its nested shadow root', async t => {
  const runtime = await launchFixture({ withSnapshot: false });
  t.after(() => closeFixture(runtime));
  const before = await observeForm(runtime.tab);
  const dates = before.fields.filter(field => field.control === 'date');
  const result = await operateObservedControl(runtime.tab, before, 'fill_control', { field_key: dates[1].field_key, value: '2027-06-30' });
  assert.equal(result.persisted, true);
  const values = await runtime.evaluate(`(() => [...document.querySelectorAll('spl-dates')].flatMap(host => [...host.shadowRoot.querySelectorAll('input')].map(input => input.value)))()`);
  assert.deepEqual(values, ['', '2027-06-30']);
});

test('React Select single selection persists by selected display when input value stays blank', async t => {
  const runtime = await launchFixture({ withSnapshot: false });
  t.after(() => closeFixture(runtime));
  const before = await observeForm(runtime.tab);
  const country = before.fields.find(field => field.label === 'Preferred country');
  assert.ok(country);
  assert.equal(country.value, '');
  assert.deepEqual(country.selected_display, ['China']);
  const result = await operateObservedControl(runtime.tab, before, 'select_control', { field_key: country.field_key, value: 'Singapore' });
  assert.equal(result.persisted, true);
  const after = result.observation.fields.find(field => field.field_key === country.field_key);
  assert.equal(after.value, '');
  assert.deepEqual(after.selected_display, ['Singapore']);
  assert.deepEqual(after.options, []);
});

test('cross-shadow aria-controls resolves the unique visible ancestor-root list', async t => {
  const runtime = await launchFixture();
  t.after(() => closeFixture(runtime));
  const form = await observeForm(runtime.tab);
  const country = form.fields.find(field => field.label === 'Country');
  assert.ok(country);
  assert.equal(country.control, 'combobox');
  // The input and linked list are in different open roots of the same widget.
  assert.deepEqual(country.options.map(option => option.label), ['Singapore', 'China']);
});
