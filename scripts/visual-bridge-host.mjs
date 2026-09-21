/** Supervised host: import in the supported Codex JavaScript session only.
 * No standalone browser, helper executable, arbitrary code or background pump.
 * Inspect peek() and the current observation before execute(request_id).
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { observeForm, changedFields, operateObservedControl, ControlNotReady } from './browser-form-state.mjs';
import { BridgeMetrics, canReuseActionReadback, observationCoverage, observationSizes,
  queueWaitMs } from './browser-observation-feedback.mjs';

const text = value => ({ type: 'text', text: typeof value === 'string' ? value : JSON.stringify(value) });
const activeTabs = new Set();
async function writeJson(file, value) {
  const temp = `${file}.${randomUUID()}.tmp`;
  await fs.writeFile(temp, JSON.stringify(value));
  await fs.rename(temp, file);
}

/** Attach directly to a returned IAB tab; callers do not invent CDP identities. */
export async function createInAppBrowserHost({ directory, tab, phase = 'prepare', submission_authorized = false,
  artifacts = {}, reuseFormObservations = true }) {
  if (typeof reuseFormObservations !== 'boolean') throw new TypeError('reuseFormObservations must be boolean');
  if (activeTabs.has(tab.id)) throw Error('This tab already has an active owner');
  activeTabs.add(tab.id);
  try {
    const host = await createVisualHost({ directory,
      adapter: browserAdapter(tab, { artifacts, reuseFormObservations: phase === 'prepare' && reuseFormObservations }),
      phase, submission_authorized,
      target: { runtime: 'iab', tab_id: tab.id, application_url: await tab.url() } });
    const close = host.close;
    host.close = async () => { await close(); activeTabs.delete(tab.id); };
    return host;
  } catch (error) { activeTabs.delete(tab.id); throw error; }
}

export async function createVisualHost({ directory, adapter, target, phase = 'prepare', submission_authorized = false }) {
  if (!['discovery', 'prepare', 'submit'].includes(phase)) throw Error('Unsupported browser phase');
  if (phase === 'submit' && (submission_authorized !== true || target.runtime !== 'iab')) {
    throw Error('Submit requires an in-app browser target and explicit submission_authorized=true');
  }
  if (phase !== 'submit' && submission_authorized !== false) throw Error('Submission authorization requires submit phase');
  if (phase === 'discovery' && target.runtime !== 'iab') throw Error('Discovery requires an in-app browser target');
  if (target.runtime === 'iab' && (adapter.surface !== 'browser' || adapter.tabId !== target.tab_id ||
      !target.tab_id || target.cdp_port !== undefined || target.worker_session_verified !== undefined)) {
    throw Error('In-app target must match the actual adapter tab, without a CDP alias');
  }
  const root = path.resolve(directory);
  for (const name of ['pending', 'claimed', 'responses', 'cancelled']) {
    await fs.mkdir(path.join(root, name), { recursive: true });
  }
  // A fresh directory/session owns a target. Never take over a live queue.
  try { await fs.writeFile(path.join(root, '.host-owner'), randomUUID(), { flag: 'wx' }); }
  catch { throw Error('Host already attached; use a fresh session directory'); }
  const binding = {
    schema_version: 1, status: 'active', session_id: randomUUID(), token_epoch: randomUUID(),
    surface: adapter.surface, phase, submission_authorized, target: structuredClone(target),
  };
  const metrics = new BridgeMetrics();
  let observationId = null;
  let busy = false;
  let closed = false;
  let paused = false;
  let pauseReason = null;
  async function heartbeat() {
    if (closed) throw Error('Host is closed');
    await writeJson(path.join(root, 'host.json'), {
      ...binding, status: paused ? 'paused' : 'active', pause_reason: pauseReason,
      heartbeat_at: Date.now() / 1000,
    });
  }
  // Do not advertise a connected host when its selected surface cannot observe.
  // A policy stop or disconnected target must fail attachment before any worker waits.
  await adapter.observe({ mode: 'dom' });
  await heartbeat();
  return {
    get binding() { return structuredClone(binding); },
    heartbeat,
    metrics: () => metrics.snapshot(),
    invalidate() { observationId = null; },
    async pause(reason = 'operator_required') {
      if (busy || closed) throw Error('Host unavailable or already executing');
      paused = true;
      pauseReason = reason;
      observationId = null;
      await heartbeat();
    },
    async resume() {
      if (busy || closed) throw Error('Host unavailable or already executing');
      // The original adapter still owns the original tab. Never attach by URL.
      observationId = null;
      await adapter.observe({ mode: 'dom' });
      paused = false;
      pauseReason = null;
      await heartbeat();
    },
    async inspect() {
      if (busy || closed) throw Error('Host unavailable or already executing');
      busy = true;
      observationId = null;
      try {
        const content = await adapter.observe({ mode: 'dom' });
        await heartbeat();
        return { session_id: binding.session_id, target: structuredClone(binding.target),
          phase, content, submission_authorized: binding.submission_authorized };
      } finally { busy = false; }
    },
    async peek() {
      await heartbeat();
      const names = (await fs.readdir(path.join(root, 'pending'))).filter(n => /^[a-f0-9-]+\.json$/.test(n));
      const requests = [];
      for (const name of names) {
        try { requests.push(JSON.parse(await fs.readFile(path.join(root, 'pending', name), 'utf8'))); }
        catch (error) { if (error.code !== 'ENOENT') throw error; }
      }
      return requests;
    },
    async execute(requestId) {
      if (busy || closed || paused) throw Error('Host unavailable, paused or already executing');
      if (!/^[a-f0-9-]{36}$/.test(requestId)) throw Error('Invalid request id');
      busy = true;
      let request;
      let claimed = false;
      let inputStarted = false;
      let adapterStarted = false;
      const serviceStarted = performance.now();
      const sample = { outcome: 'rejected', queue_wait_ms: null, action_ms: null,
        observation_ms: null, text_bytes: null, image_bytes: null };
      try {
        await heartbeat();
        const source = path.join(root, 'pending', `${requestId}.json`);
        request = JSON.parse(await fs.readFile(source, 'utf8'));
        if (request.session_id !== binding.session_id || request.token_epoch !== binding.token_epoch ||
            request.surface !== binding.surface || request.phase !== binding.phase ||
            JSON.stringify(request.target) !== JSON.stringify(binding.target)) throw Error('Target/session mismatch');
        if (request.deadline_at <= Date.now() / 1000) throw Error('Expired request; no input performed');
        sample.queue_wait_ms = queueWaitMs(request.created_at, Date.now() / 1000);
        // Cancellation and claiming compete for the same file: only one wins.
        await fs.rename(source, path.join(root, 'claimed', `${requestId}.json`));
        claimed = true;
        if (request.operation !== 'observe' && request.observation_id !== observationId) {
          throw Error('Stale observation; observe again before input');
        }
        if (request.deadline_at <= Date.now() / 1000) throw Error('Expired before execution');
        observationId = null;
        adapterStarted = true;
        sample.outcome = 'failed';
        let actionResult;
        if (request.operation !== 'observe') {
          inputStarted = true;
          sample.outcome = 'outcome_unknown';
          const started = performance.now();
          try { actionResult = await adapter.act(request.operation, request.arguments); }
          finally { sample.action_ms = performance.now() - started; }
        }
        let content;
        const observeStarted = performance.now();
        try {
          content = await adapter.observe({ mode: request.arguments.mode || 'dom',
            actionResult: binding.target.runtime === 'iab' && phase === 'prepare' ? actionResult : undefined });
        } finally { sample.observation_ms = performance.now() - observeStarted; }
        Object.assign(sample, observationSizes(content));
        observationId = randomUUID();
        const response = {
          schema_version: 1, request_id: requestId, session_id: binding.session_id,
          token_epoch: binding.token_epoch, ok: true, outcome: 'completed', observation_id: observationId,
          content: [text({ observation_id: observationId, surface: binding.surface }), ...content],
        };
        await writeJson(path.join(root, 'responses', `${requestId}.json`), response);
        await heartbeat();
        sample.outcome = 'completed';
        return response;
      } catch (error) {
        observationId = null;
        if (!claimed) throw error;
        const rejectedBeforeInput = error instanceof ControlNotReady;
        if (adapterStarted && !rejectedBeforeInput) {
          // A runtime stop/disconnection is not permission to retry input.
          closed = true;
          await writeJson(path.join(root, 'host.json'), { ...binding, status: 'stopped', heartbeat_at: Date.now() / 1000 });
        }
        const response = {
          schema_version: 1, request_id: requestId, session_id: binding.session_id,
          token_epoch: binding.token_epoch, ok: false,
          outcome: inputStarted && !rejectedBeforeInput ? 'outcome_unknown' : 'failed',
          content: [text({ error: String(error.message), reobserve_before_retry: true })],
        };
        sample.outcome = response.outcome;
        await writeJson(path.join(root, 'responses', `${requestId}.json`), response);
        return response;
      } finally {
        sample.host_service_ms = performance.now() - serviceStarted;
        try { metrics.record(sample); } finally { busy = false; }
      }
    },
    async close() {
      if (busy) throw Error('Wait for the executing operation before closing');
      closed = true;
      observationId = null;
      await writeJson(path.join(root, 'host.json'), { ...binding, status: 'stopped', heartbeat_at: Date.now() / 1000 });
    },
  };
}

export function browserAdapter(tab, { artifacts = {}, reuseFormObservations = false } = {}) {
  if (typeof reuseFormObservations !== 'boolean') throw new TypeError('reuseFormObservations must be boolean');
  // Only the trusted host supplies paths. Workers select opaque references.
  const artifactFiles = new Map(Object.entries(artifacts));
  let lastMode = 'dom';
  let observedLinks = new Set();
  let observedInputs = new Set();
  let observedNodes = new Set();
  let formSnapshot = null;
  let uploadBaseline = null;
  let lastControlResult = null;
  let pendingReadback = null;
  return {
    surface: 'browser',
    tabId: tab.id,
    async observe({ mode = 'dom', actionResult } = {}) {
      // Consume exactly once. Explicit observations, pause/resume and screenshots never reuse it.
      const pending = pendingReadback;
      pendingReadback = null;
      if (reuseFormObservations && (!pending || actionResult !== pending || mode !== 'dom')) lastControlResult = null;
      lastMode = mode;
      const url = await tab.url();
      const context = text({ tab_id: tab.id, page_url: url, title: await tab.title(), artifact_ids: [...artifactFiles.keys()] });
      observedLinks = new Set();
      observedInputs = new Set();
      observedNodes = new Set();
      if (mode === 'screenshot') {
        formSnapshot = null;
        return [context, { type: 'image', mimeType: 'image/png', data: Buffer.from(await tab.screenshot({})).toString('base64') }];
      }
      const dom = await tab.dom_cua.get_visible_dom();
      // Keep current visible DOM (including alerts/navigation) even in the smaller reply.
      const reuse = reuseFormObservations && mode === 'dom' &&
        canReuseActionReadback(pending, actionResult, url, performance.now()) && await tab.url() === url;
      const snapshot = reuse ? '' : await tab.playwright.domSnapshot();
      for (const match of dom.matchAll(/\bnode_id=["']?([^\s"'>]+)/g)) observedNodes.add(match[1]);
      for (const match of dom.matchAll(/<(input|textarea)\b([^>]*)>/g)) {
        const id = /\bnode_id=["']?([^\s"'>]+)/.exec(match[2])?.[1];
        const type = /\btype=["']?([^\s"'>]+)/.exec(match[2])?.[1]?.toLowerCase() || 'text';
        if (id && ['text', 'search', 'email', 'tel', 'url', 'number'].includes(type)) observedInputs.add(id);
      }
      // Only links returned by this page observation may be opened by the worker.
      for (const match of dom.matchAll(/href="([^"]+)"/g)) {
        try {
          const link = new URL(match[1].replaceAll('&amp;', '&'), url);
          if (['http:', 'https:'].includes(link.protocol) && !link.username && !link.password) observedLinks.add(link.href);
        } catch { /* Non-web links are not navigation targets. */ }
      }
      for (const match of snapshot.matchAll(/^\s*- \/url: (.+)$/gm)) {
        try {
          const link = new URL(match[1], url);
          if (['http:', 'https:'].includes(link.protocol) && !link.username && !link.password) observedLinks.add(link.href);
        } catch { /* Non-web links are not navigation targets. */ }
      }
      const content = [context, text(dom), ...(reuse ? [] : [text(snapshot)])];
      if (typeof tab.playwright.evaluate === 'function') {
        const previous = formSnapshot;
        formSnapshot = reuse ? pending.form : await observeForm(tab);
        if (reuseFormObservations && formSnapshot.page_url !== url) {
          throw Error('Page changed during observation; observe again before continuing');
        }
        // A delayed full refresh must not attach old persistence to a changed current value.
        if (reuseFormObservations && pending && !reuse && lastControlResult) {
          const state = field => field && JSON.stringify([field.selector, field.label, field.group_key,
            field.group, field.control, field.value, field.checked, field.selected_display]);
          const old = pending.form.fields.find(field => field.field_key === lastControlResult.field_key);
          const current = formSnapshot.fields.find(field => field.field_key === lastControlResult.field_key);
          if (pending.form.page_url !== formSnapshot.page_url || !old || !current ||
              current.value_source === 'unavailable' || state(old) !== state(current)) {
            lastControlResult = { ...lastControlResult, persisted: null,
              invalid: current?.invalid ?? null, validation_message: current?.validation_message || '' };
          }
        }
        content.push(text({ form_state: formSnapshot,
          changed_fields: changedFields(previous, formSnapshot),
          post_upload_changes: changedFields(uploadBaseline, formSnapshot),
          control_result: lastControlResult,
          ...(reuseFormObservations ? { observation_feedback: {
            kind: reuse ? 'action_readback_with_visible_dom' : 'full_dom', form_readback_reused: reuse,
            full_observation_available: true, coverage: observationCoverage(formSnapshot),
            // The next write still re-reads its control; this is not evidence of later persistence.
            immediate_readback_only: true,
          } } : {}) }));
        lastControlResult = null;
      }
      return content;
    },
    async act(operation, args) {
      pendingReadback = null;
      if (['fill_control', 'select_control', 'set_checked'].includes(operation)) {
        const result = await operateObservedControl(tab, formSnapshot, operation, args);
        const { observation, ...report } = result;
        lastControlResult = report;
        // Pass a private identity ticket directly to this action's reply, never to the worker.
        if (reuseFormObservations) {
          pendingReadback = { form: observation, before: formSnapshot, report, observedAt: performance.now() };
          return pendingReadback;
        }
        return;
      }
      if (operation === 'upload_artifact') {
        const file = artifactFiles.get(args.artifact_id);
        if (typeof file !== 'string' || !path.isAbsolute(file)) throw Error('Unknown artifact or non-absolute artifact path');
        if (!observedNodes.has(args.node_id)) throw Error('Upload requires a node from the current DOM observation');
        if (!(await fs.stat(file)).isFile()) throw Error('Artifact must be a regular file');
        uploadBaseline = formSnapshot;
        const chooserPromise = tab.playwright.waitForEvent('filechooser', { timeoutMs: 10000 });
        // Click may fail first; the pending waiter must still have a rejection handler.
        chooserPromise.catch(() => {});
        await tab.dom_cua.click({ node_id: args.node_id });
        const chooser = await chooserPromise;
        await chooser.setFiles([file]);
        return;
      }
      if (operation === 'navigate') {
        if (!observedLinks.has(args.url)) throw Error('Navigation requires an exact link from the current observation');
        return tab.goto(args.url);
      }
      if (operation === 'click') {
        if (args.node_id) return tab.dom_cua.click({ node_id: args.node_id });
        if (lastMode !== 'screenshot') throw Error('Observe screenshot before coordinate input');
        return tab.cua.click({ x: args.x, y: args.y });
      }
      if (operation === 'scroll') {
        if (args.x !== undefined && args.y !== undefined) {
          if (lastMode !== 'screenshot') throw Error('Observe screenshot before coordinate scroll');
          return tab.cua.scroll({ x: args.x, y: args.y, scrollX: args.scroll_x || 0, scrollY: args.scroll_y });
        }
        return tab.dom_cua.scroll({ x: args.scroll_x || 0, y: args.scroll_y });
      }
      if (operation === 'type_text') {
        if (args.node_id) {
          if (!observedInputs.has(args.node_id)) throw Error('Targeted typing requires an observed text input');
          await tab.dom_cua.click({ node_id: args.node_id });
        }
        return tab.dom_cua.type({ text: args.text });
      }
      if (operation === 'press_key') return tab.dom_cua.keypress({ keys: args.keys || [args.key] });
      throw Error('Unsupported browser operation');
    },
  };
}

export function computerAdapter(sky, returnedWindow) {
  let state = null;
  return {
    surface: 'computer_use',
    async observe({ mode = 'dom' } = {}) {
      state = await sky.get_window_state({
        window: state?.window || returnedWindow,
        include_screenshot: mode === 'screenshot', include_text: mode !== 'screenshot',
      });
      const content = state.accessibility ? [text(state.accessibility)] : [];
      // These image blocks are the observation sent to the requesting worker.
      for (const shot of state.screenshots || []) {
        const match = /^data:(image\/[^;]+);base64,(.+)$/s.exec(shot.url);
        if (match) content.push({ type: 'image', mimeType: match[1], data: match[2] });
      }
      return content;
    },
    async act(operation, args) {
      const observed = state;
      state = null;
      if (!observed) throw Error('Observe before input');
      const window = observed.window;
      if (operation === 'click') {
        if (args.element_index !== undefined) return sky.click({ window, element_index: args.element_index });
        const screenshotId = observed.screenshots?.[0]?.id;
        if (!screenshotId) throw Error('Observe screenshot before coordinate input');
        return sky.click({ window, screenshotId, x: args.x, y: args.y });
      }
      if (operation === 'scroll') {
        const screenshotId = observed.screenshots?.[0]?.id;
        if (!screenshotId || args.x === undefined || args.y === undefined) throw Error('Observe screenshot and specify scroll point');
        return sky.scroll({ window, screenshotId, x: args.x, y: args.y, scrollX: args.scroll_x || 0, scrollY: args.scroll_y });
      }
      if (operation === 'type_text') {
        if (!observed.accessibility?.focused_element) throw Error('Observe focus before typing');
        return sky.type_text({ window, text: args.text });
      }
      if (operation === 'press_key') {
        const keys = { Enter: 'Return', Escape: 'Escape', Space: 'space', ArrowDown: 'Down', ArrowUp: 'Up', PageDown: 'Next', PageUp: 'Prior' };
        return sky.press_key({ window, key: (args.keys || [args.key]).map(k => keys[k] || k).join('+') });
      }
      throw Error('Unsupported computer operation');
    },
  };
}
