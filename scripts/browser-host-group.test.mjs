import test from 'node:test';
import assert from 'node:assert/strict';
import { createBrowserHostGroup } from './browser-host-group.mjs';

test('group never executes from peek; one stopped host does not suppress another', async () => {
  const writes = [];
  const entries = ['a', 'b'].map(job_id => ({ job_id, host: {
    binding: { phase: 'prepare', target: { runtime: 'iab', tab_id: job_id } },
    peek: async () => { if (job_id === 'a') throw Error('stopped'); return [{ request_id: 'b1' }]; },
    metrics: () => ({}), execute: async id => { writes.push(id); return { ok: true }; },
  } }));
  const group = createBrowserHostGroup(entries);
  const pending = await group.peek();
  assert.equal(pending[0].error, 'stopped');
  assert.equal(writes.length, 0);
  await assert.rejects(group.execute([{ job_id: 'b', request_id: 'b1' }, { job_id: 'b', request_id: 'b2' }]));
  assert.equal(writes.length, 0);
  await group.execute([{ job_id: 'b', request_id: 'b1' }]);
  assert.deepEqual(writes, ['b1']);
  assert.throws(() => createBrowserHostGroup([entries[0], entries[0]]));
});

test('four workers share at most two simultaneous browser operations', async () => {
  let active = 0, peak = 0;
  const entries = ['a', 'b', 'c', 'd'].map(job_id => ({ job_id, host: {
    binding: { phase: 'prepare', target: { runtime: 'iab', tab_id: job_id } },
    execute: async () => { peak = Math.max(peak, ++active); await new Promise(resolve => setTimeout(resolve, 5)); active--; return { ok: true }; },
  } }));
  const replies = await createBrowserHostGroup(entries).execute(entries.map(e => ({ job_id: e.job_id, request_id: e.job_id })));
  assert.equal(peak, 2);
  assert.equal(replies.length, 4);
});
