/** Local form/Chromium integration, not a live IAB, model or employer benchmark. */
import assert from 'node:assert/strict';
import test from 'node:test';
import { pathToFileURL } from 'node:url';
import { browserAdapter } from './visual-bridge-host.mjs';

const modulePath = process.env.APPLYPILOT_TEST_PLAYWRIGHT_MODULE;
assert.ok(modulePath, 'Run via the pytest browser wrapper to locate Playwright');
const { chromium } = await import(pathToFileURL(modulePath).href);
const fixture = `<!doctype html><html><body>
  <h1>Local application fixture</h1>
  <p>No external requests or submission controls are present.</p>
  <label for="name">Name</label><input id="name" required>
  <label for="city">City</label><input id="city" value="Singapore" required>
  <label for="role">Role</label><select id="role"><option value="intern">Intern</option><option value="other">Other</option></select>
  <div id="extra"></div><div role="alert">Current visible feedback</div>
  <script>document.querySelector('#role').addEventListener('change', () => {
    document.querySelector('#extra').innerHTML = '<label for="detail">Detail</label><input id="detail" required>';
  });</script>
</body></html>`;

async function setup(t, reuseFormObservations) {
  const browser = await chromium.launch({ headless: true,
    ...(process.env.APPLYPILOT_TEST_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.APPLYPILOT_TEST_CHROMIUM_EXECUTABLE } : {}),
    args: ['--no-sandbox', '--disable-background-networking'] });
  t.after(() => browser.close());
  const page = await browser.newPage();
  const base = 'about:blank';
  await page.route('**/*', route => route.abort());
  await page.goto(base);
  await page.setContent(fixture);
  const counts = { form: 0, dom: 0, snapshot: 0 };
  const tab = { id: 'local-chromium', url: async () => page.url(), title: () => page.title(),
    dom_cua: { get_visible_dom: async () => { counts.dom++; return page.locator('body').innerText(); } },
    playwright: {
      evaluate: async fn => { counts.form++; return page.evaluate(fn); },
      domSnapshot: async () => { counts.snapshot++; return page.content(); },
      locator: selector => page.locator(selector),
    },
  };
  const adapter = browserAdapter(tab, { reuseFormObservations });
  const initial = await adapter.observe();
  return { adapter, page, counts, initial, base };
}
const block = content => content.flatMap(item => {
  try { return item.type === 'text' ? [JSON.parse(item.text)] : []; } catch { return []; }
}).find(item => item.form_state);
const key = (content, label) => block(content).form_state.fields.find(field => field.label === label).field_key;
async function fill(f, content, label, value) {
  const actionResult = await f.adapter.act('fill_control', { field_key: key(content, label), value });
  return f.adapter.observe({ actionResult });
}

test('same five real field writes: equivalent values with fewer full reads', async t => {
  const results = [];
  for (const reuse of [false, true]) {
    const f = await setup(t, reuse);
    let content = f.initial;
    let reused = 0;
    for (let i = 0; i < 5; i++) {
      content = await fill(f, content, 'Name', `Candidate ${i}`);
      if (block(content).observation_feedback?.form_readback_reused === true) reused++;
    }
    assert.equal(await f.page.locator('#name').inputValue(), 'Candidate 4');
    assert.match(content[1].text, /Current visible feedback/);
    assert.equal(block(content).form_state.fields.find(field => field.label === 'City').value, 'Singapore');
    assert.equal(f.counts.form, 16 - reused);
    assert.equal(f.counts.snapshot, 6 - reused);
    assert.equal(f.counts.dom, 6);
    results.push({ reuse, ...f.counts, reused });
  }
  // Slow runners may legitimately exceed the reuse age budget. Never disable that guard for a speed test.
  assert.equal(results[0].reused, 0);
  t.diagnostic(JSON.stringify({ evidence: 'local_chromium_fixed_five_writes', results }));
});

test('a real delayed parser overwrite appears on explicit refresh', async t => {
  const f = await setup(t, true);
  const content = await fill(f, f.initial, 'Name', 'Candidate');
  await f.page.evaluate(() => setTimeout(() => { document.querySelector('#city').value = 'Late overwrite'; }, 50));
  await f.page.waitForFunction(() => document.querySelector('#city').value === 'Late overwrite');
  const refreshed = await f.adapter.observe();
  assert.equal(block(refreshed).observation_feedback.form_readback_reused, false);
  assert.equal(block(refreshed).form_state.fields.find(field => field.label === 'City').value, 'Late overwrite');
  assert.ok(block(refreshed).changed_fields.some(field => field.observed_value === 'Late overwrite'));
  assert.equal(block(content).observation_feedback.coverage.all_steps, 'unverified');
});

test('a real conditional required field forces the full observation path', async t => {
  const f = await setup(t, true);
  const actionResult = await f.adapter.act('select_control', { field_key: key(f.initial, 'Role'), value: 'Other' });
  const content = await f.adapter.observe({ actionResult });
  assert.equal(block(content).observation_feedback.form_readback_reused, false);
  assert.ok(block(content).form_state.fields.some(field => field.label === 'Detail' && field.required && field.invalid));
});

test('navigation after a real write cannot reuse its previous page readback', async t => {
  const f = await setup(t, true);
  const actionResult = await f.adapter.act('fill_control', { field_key: key(f.initial, 'Name'), value: 'Candidate' });
  await f.page.goto(f.base + '#step=2');
  await f.page.setContent(fixture);
  const content = await f.adapter.observe({ actionResult });
  assert.equal(block(content).observation_feedback.form_readback_reused, false);
  assert.equal(block(content).form_state.fields.find(field => field.label === 'Name').value, '');
});
