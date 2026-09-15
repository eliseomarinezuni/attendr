import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

const source = readFileSync(new URL('../src/index.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
});
const { default: worker } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);

for (const secret of [undefined, '', '   ']) {
  test(`missing or blank secret fails closed: ${JSON.stringify(secret)}`, async () => {
    const response = await worker.fetch(
      new Request('https://worker.example/api/state', { headers: { authorization: `Bearer ${secret}` } }),
      { STUDY_SYNC_SECRET: secret }, {},
    );
    assert.equal(response.status, 401);
  });
}

test('incorrect credential is rejected', async () => {
  const response = await worker.fetch(
    new Request('https://worker.example/api/state', { headers: { authorization: 'Bearer wrong' } }),
    { STUDY_SYNC_SECRET: 'configured-secret' }, {},
  );
  assert.equal(response.status, 401);
});

test('correct credential can read state', async () => {
  const response = await worker.fetch(
    new Request('https://worker.example/api/state', { headers: { authorization: 'Bearer configured-secret' } }),
    { STUDY_SYNC_SECRET: 'configured-secret', DB: { prepare: () => ({ all: async () => ({ results: [] }) }) } }, {},
  );
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { completed_tasks: [], completed_sessions: [], rescheduled_sessions: {}, operations: [] });
});

test('Google health check is authenticated and returns only a safe category', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response('provider-secret-body', { status: 400 });
  try {
    const unauthorized = await worker.fetch(
      new Request('https://worker.example/api/google/health'),
      { STUDY_SYNC_SECRET: 'configured-secret' }, {},
    );
    assert.equal(unauthorized.status, 401);
    const response = await worker.fetch(
      new Request('https://worker.example/api/google/health', { headers: { authorization: 'Bearer configured-secret' } }),
      { STUDY_SYNC_SECRET: 'configured-secret', GOOGLE_CLIENT_ID: 'id', GOOGLE_CLIENT_SECRET: 'secret', GOOGLE_REFRESH_TOKEN: 'refresh' }, {},
    );
    assert.equal(response.status, 503);
    assert.deepEqual(await response.json(), { ok: false, category: 'google_oauth_invalid', retryable: false });
  } finally {
    globalThis.fetch = originalFetch;
  }
});
