import test from 'node:test';
import assert from 'node:assert/strict';

import { ApiError, request } from '../src/api.js';

test('request preserves structured API errors for user-visible mutations', async t => {
  const originalFetch = global.fetch;
  t.after(() => { global.fetch = originalFetch; });
  global.fetch = async () => new Response(
    JSON.stringify({ error: 'invalid_request', message: '원본 경로가 없습니다.' }),
    { status: 400, headers: { 'Content-Type': 'application/json' } },
  );
  await assert.rejects(
    request('/projects', { method: 'POST', body: '{}' }),
    error => error instanceof ApiError
      && error.status === 400
      && error.code === 'invalid_request'
      && error.message === '원본 경로가 없습니다.',
  );
});

test('request reports non-JSON and network failures instead of returning fixtures', async t => {
  const originalFetch = global.fetch;
  t.after(() => { global.fetch = originalFetch; });
  global.fetch = async () => new Response('gateway failure', { status: 502 });
  await assert.rejects(request('/projects'), /gateway failure/);
  global.fetch = async () => { throw new Error('connection refused'); };
  await assert.rejects(request('/projects'), /로컬 런타임에 연결할 수 없습니다/);
});
