import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { createHash } from 'node:crypto';
import test, { afterEach } from 'node:test';
import ts from 'typescript';

const source = readFileSync(new URL('../src/index.ts', import.meta.url), 'utf8') +
  '\nexport { busyIntervals, allCalendarIds, verifyDiscord, handleButton, recoverOperations, nextSlot, automationWatchdog };';
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } });
const { default: worker, busyIntervals, allCalendarIds, verifyDiscord, handleButton, recoverOperations, nextSlot, automationWatchdog } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);
const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

function database() {
  const sqlite = new DatabaseSync(':memory:');
  sqlite.exec(readFileSync(new URL('../schema.sql', import.meta.url), 'utf8'));
  const wrap = (sql, values = []) => ({
    bind: (...args) => wrap(sql, args),
    all: async () => ({ results: sqlite.prepare(sql).all(...values) }),
    first: async () => sqlite.prepare(sql).get(...values) ?? null,
    run: async () => sqlite.prepare(sql).run(...values),
  });
  return { sqlite, prepare: (sql) => wrap(sql), batch: async (statements) => {
    sqlite.exec('BEGIN');
    try { const results = []; for (const statement of statements) results.push(await statement.run()); sqlite.exec('COMMIT'); return results; }
    catch (error) { sqlite.exec('ROLLBACK'); throw error; }
  } };
}
const sessionId = 'attendr:study:' + 'a'.repeat(20) + ':1';
function session() {
  return { session_id: sessionId, task_uid: 'assignment:1', title: 'Study', course_name: 'Math',
    start: new Date(Date.now() + 60_000).toISOString(), end: new Date(Date.now() + 1800_000).toISOString(),
    task_due_at: new Date(Date.now() + 86400_000).toISOString(), calendar_id: 'study', event_id: 'event' };
}
async function lease(DB, path, token = 'a'.repeat(32)) {
  return worker.fetch(new Request('https://example.test/api/plan/' + path, {
    method: 'POST', headers: { authorization: 'Bearer secret' }, body: JSON.stringify({ token }),
  }), { DB, STUDY_SYNC_SECRET: 'secret' }, {});
}
async function sync(DB, payload) {
  await lease(DB, 'acquire');
  try {
    return await worker.fetch(new Request('https://example.test/api/sessions/sync', {
      method: 'POST', headers: { authorization: 'Bearer secret' }, body: JSON.stringify(payload && typeof payload === 'object' ? { ...payload, plan_token: 'a'.repeat(32) } : payload),
    }), { DB, STUDY_SYNC_SECRET: 'secret' }, {});
  } finally { await lease(DB, 'release'); }
}

for (const payload of [null, {}, { sessions: {} }, { sessions: 'bad' }, { sessions: [], replace: 'false' }]) {
  test('invalid sync envelope cannot cancel existing sessions: ' + JSON.stringify(payload), async () => {
    const DB = database();
    await sync(DB, { sessions: [session()] });
    assert.equal((await sync(DB, payload)).status, 400);
    assert.equal(DB.sqlite.prepare('SELECT status FROM study_sessions').get().status, 'scheduled');
    DB.sqlite.close();
  });
}

test('invalid ordering and naive dates are rejected', async () => {
  const DB = database();
  for (const invalid of [{ ...session(), start: '2026-09-06T12:00:00' }, { ...session(), end: '2020-01-01T00:00:00Z' }])
    assert.equal((await sync(DB, { sessions: [invalid] })).status, 400);
  DB.sqlite.close();
});

test('equivalent offset dates preserve manual overrides', async () => {
  const DB = database();
  const item = { ...session(), start: '2026-09-06T12:00:00+00:00', end: '2026-09-06T13:00:00+00:00', task_due_at: '2026-09-08T12:00:00+00:00' };
  await sync(DB, { sessions: [item] });
  DB.sqlite.exec("UPDATE study_sessions SET manual_override=1,start_at='2026-09-07T12:00:00.000Z',end_at='2026-09-07T13:00:00.000Z'");
  await sync(DB, { sessions: [{ ...item, task_due_at: '2026-09-08T08:00:00-04:00' }] });
  const row = DB.sqlite.prepare('SELECT * FROM study_sessions').get();
  assert.equal(row.start_at, '2026-09-07T12:00:00.000Z');
  assert.equal(row.manual_override, 1);
  DB.sqlite.close();
});

test('concurrent reminder workers send once', async () => {
  const DB = database();
  await sync(DB, { sessions: [session()] });
  let sends = 0;
  globalThis.fetch = async () => { sends++; return Response.json({ id: 'message' }); };
  const request = () => new Request('https://example.test/api/reminders/run', { method: 'POST', headers: { authorization: 'Bearer secret' } });
  await Promise.all([1, 2].map(() => worker.fetch(request(), { DB, STUDY_SYNC_SECRET: 'secret' }, {})));
  assert.equal(sends, 1);
  assert.equal(DB.sqlite.prepare('SELECT notified FROM study_sessions').get().notified, 1);
  DB.sqlite.close();
});

test('lost reminder response stays unresolved instead of automatically repeating', async () => {
  const DB = database();
  await sync(DB, { sessions: [session()] });
  let sends = 0;
  globalThis.fetch = async () => { sends++; throw new Error('response lost'); };
  const request = () => new Request('https://example.test/api/reminders/run', { method: 'POST', headers: { authorization: 'Bearer secret' } });
  await worker.fetch(request(), { DB, STUDY_SYNC_SECRET: 'secret' }, {});
  await worker.fetch(request(), { DB, STUDY_SYNC_SECRET: 'secret' }, {});
  assert.equal(sends, 1);
  assert.equal(DB.sqlite.prepare('SELECT notified FROM study_sessions').get().notified, -1);
  DB.sqlite.close();
});

test('all calendars are paginated and FreeBusy is batched', async () => {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push([String(url), init]);
    if (String(url).includes('calendarList')) return Response.json(String(url).includes('pageToken=next') ?
      { items: [{ id: '50' }] } : { items: Array.from({ length: 50 }, (_, i) => ({ id: String(i) })), nextPageToken: 'next' });
    return Response.json({ calendars: Object.fromEntries(JSON.parse(init.body).items.map(({ id }) => [id, { busy: [] }])) });
  };
  const ids = await allCalendarIds('fake-token', 'study');
  assert.equal(ids.length, 51);
  assert.deepEqual(await busyIntervals('fake-token', ids, new Date(), new Date(Date.now() + 60000)), []);
  assert.deepEqual(calls.filter(([url]) => url.includes('freeBusy')).map(([, init]) => JSON.parse(init.body).items.length), [50, 1]);
});

test('FreeBusy errors prevent placement', async () => {
  globalThis.fetch = async () => Response.json({ calendars: { primary: { errors: [{ reason: 'forbidden' }] } } });
  await assert.rejects(busyIntervals('fake', ['primary'], new Date(), new Date()), /Incomplete/);
});

test('stale correctly signed Discord requests are rejected', async () => {
  const keys = await crypto.subtle.generateKey('Ed25519', true, ['sign', 'verify']);
  const publicKey = Buffer.from(await crypto.subtle.exportKey('raw', keys.publicKey)).toString('hex');
  const body = '{"type":1}';
  const timestamp = String(Math.floor(Date.now() / 1000) - 600);
  const signature = Buffer.from(await crypto.subtle.sign('Ed25519', keys.privateKey, new TextEncoder().encode(timestamp + body))).toString('hex');
  const request = new Request('https://example.test/interactions', { headers: { 'x-signature-timestamp': timestamp, 'x-signature-ed25519': signature } });
  assert.equal(await verifyDiscord(request, body, { DISCORD_PUBLIC_KEY: publicKey }), false);
});

test('Google deletion followed by D1 failure resumes without replaying the action', async () => {
  const DB = database();
  await sync(DB, { sessions: [session()] });
  let deletions = 0;
  globalThis.fetch = async (url, init) => {
    if (String(url).includes('/token')) return Response.json({ access_token: 'fake' });
    if (init.method === 'DELETE') { deletions++; return new Response(null, { status: deletions === 1 ? 204 : 404 }); }
    return Response.json({});
  };
  const interaction = { id: '123', type: 3, token: '', application_id: '', data: { custom_id: 'study:complete:' + sessionId } };
  const prepare = DB.prepare;
  let fail = true;
  DB.prepare = (sql) => {
    if (fail && sql.startsWith("UPDATE study_sessions SET status='completed'")) { fail = false; throw new Error('injected D1 failure'); }
    return prepare(sql);
  };
  await assert.rejects(handleButton(interaction, { DB }), /injected/);
  DB.sqlite.exec('UPDATE study_operations SET lease_until=0');
  await recoverOperations({ DB });
  assert.equal(DB.sqlite.prepare('SELECT status FROM study_sessions').get().status, 'completed');
  assert.equal(DB.sqlite.prepare('SELECT status FROM study_operations').get().status, 'done');
  await handleButton(interaction, { DB });
  assert.equal(deletions, 2);
  DB.sqlite.close();
});

test('planner lease excludes both another planner and live buttons', async () => {
  const DB = database();
  await sync(DB, { sessions: [session()] });
  assert.equal((await lease(DB, 'acquire')).status, 200);
  assert.equal((await lease(DB, 'acquire', 'b'.repeat(32))).status, 409);
  let writes = 0;
  globalThis.fetch = async () => { writes++; return Response.json({}); };
  await handleButton({ id: '987', token: '', data: { custom_id: 'study:complete:' + sessionId } }, { DB });
  assert.equal(writes, 0);
  assert.equal(DB.sqlite.prepare('SELECT count(*) AS count FROM study_operations').get().count, 0);
  await lease(DB, 'release');
  assert.equal((await lease(DB, 'acquire', 'b'.repeat(32))).status, 200);
  DB.sqlite.close();
});

test('invalid study profile cannot replace sessions or stored preferences', async () => {
  const DB = database();
  await sync(DB, { sessions: [session()] });
  const response = await sync(DB, { sessions: [], profile: { windows: { '0': [[900, 600]] } } });
  assert.equal(response.status, 400);
  assert.equal(DB.sqlite.prepare('SELECT status FROM study_sessions').get().status, 'scheduled');
  DB.sqlite.close();
});

test('Python study preferences persist and control Worker rescheduling', async () => {
  const DB = database();
  const item = { ...session(), task_due_at: new Date(Date.now() + 3 * 86400_000).toISOString() };
  const profile = { windows: Object.fromEntries(Array.from({length: 7}, (_, day) => [String(day), [[600, 660]]])) };
  assert.equal((await sync(DB, { sessions: [item], profile })).status, 200);
  assert.deepEqual(JSON.parse(DB.sqlite.prepare("SELECT payload FROM study_preferences WHERE name='profile'").get().payload), profile);
  globalThis.fetch = async (url) => {
    if (String(url).includes('calendarList')) return Response.json({ items: [] });
    throw new Error('Unexpected request: ' + String(url));
  };
  const stored = DB.sqlite.prepare('SELECT * FROM study_sessions').get();
  const slot = await nextSlot(stored, 'fake-token', { DB });
  assert.ok(slot);
  const hour = new Intl.DateTimeFormat('en', { timeZone: 'America/Toronto', hour: 'numeric', hourCycle: 'h23' }).format(slot.start);
  assert.equal(hour, '10');
  DB.sqlite.close();
});

test('Worker skips nonexistent spring-forward slots in custom windows', async () => {
  const DB = database();
  const item = { ...session(), start: '2026-03-07T17:00:00Z', end: '2026-03-07T17:30:00Z', task_due_at: '2026-03-09T03:59:00Z' };
  const profile = { windows: Object.fromEntries(Array.from({length: 7}, (_, day) => [String(day), day === 0 ? [[120, 180]] : []])) };
  await sync(DB, { sessions: [item], profile });
  globalThis.fetch = async () => Response.json({ items: [] });
  assert.equal(await nextSlot(DB.sqlite.prepare('SELECT * FROM study_sessions').get(), 'fake', { DB }, new Date('2026-03-08T05:00:00Z')), null);
  DB.sqlite.close();
});

test('cloud state requires a lease and enforces revisions', async () => {
  const DB = database();
  const auth = { authorization: 'Bearer secret' };
  const token = 'c'.repeat(32);
  const acquire = await worker.fetch(new Request('https://example.test/api/state-store/acquire', {
    method: 'POST', headers: auth, body: JSON.stringify({ token }),
  }), { DB, STUDY_SYNC_SECRET: 'secret' }, {});
  assert.equal(acquire.status, 200);
  assert.equal((await acquire.json()).revision, 0);
  const leaseHeaders = { ...auth, 'x-attendr-lease': token };
  const payload = { revision: 0, sha256: createHash('sha256').update('abc').digest('hex'), size: 3, chunks: ['YWJj'] };
  const write = await worker.fetch(new Request('https://example.test/api/state-store', {
    method: 'PUT', headers: leaseHeaders, body: JSON.stringify(payload),
  }), { DB, STUDY_SYNC_SECRET: 'secret' }, {});
  assert.deepEqual(await write.json(), { revision: 1 });
  const read = await worker.fetch(new Request('https://example.test/api/state-store', {
    headers: leaseHeaders,
  }), { DB, STUDY_SYNC_SECRET: 'secret' }, {});
  assert.deepEqual(await read.json(), { revision: 1, sha256: payload.sha256, size: 3, chunk_count: 1, chunks: ['YWJj'] });
  const stale = await worker.fetch(new Request('https://example.test/api/state-store', {
    method: 'PUT', headers: leaseHeaders, body: JSON.stringify(payload),
  }), { DB, STUDY_SYNC_SECRET: 'secret' }, {});
  assert.equal(stale.status, 409);
  const competing = await worker.fetch(new Request('https://example.test/api/state-store/acquire', {
    method: 'POST', headers: auth, body: JSON.stringify({ token: 'e'.repeat(32) }),
  }), { DB, STUDY_SYNC_SECRET: 'secret' }, {});
  assert.equal(competing.status, 409);
  DB.sqlite.close();
});

test('automation heartbeat is authenticated and suppresses watchdog recovery', async () => {
  const DB = database();
  const heartbeat = await worker.fetch(new Request('https://example.test/api/automation/heartbeat', {
    method: 'POST', headers: { authorization: 'Bearer secret' },
    body: JSON.stringify({ name: 'academic', status: 'success', run_id: '12345' }),
  }), { DB, STUDY_SYNC_SECRET: 'secret' }, {});
  assert.equal(heartbeat.status, 200);
  assert.equal(DB.sqlite.prepare("SELECT status FROM automation_heartbeats WHERE name='academic'").get().status, 'success');
  let dispatches = 0;
  globalThis.fetch = async () => { dispatches++; return new Response(null, { status: 204 }); };
  await automationWatchdog({ DB, GITHUB_ACTIONS_TOKEN: 'token', GITHUB_REPOSITORY: 'owner/repo' }, new Date());
  assert.equal(dispatches, 0);
  DB.sqlite.close();
});

test('stale heartbeat dispatches one rate-limited recovery run', async () => {
  const DB = database();
  let dispatches = 0;
  globalThis.fetch = async (url, init) => {
    dispatches++;
    assert.equal(String(url), 'https://api.github.com/repos/owner/repo/actions/workflows/schedule.yml/dispatches');
    assert.deepEqual(JSON.parse(init.body), { ref: 'main' });
    return new Response(null, { status: 204 });
  };
  const now = new Date('2026-09-09T16:00:00Z');
  const env = { DB, GITHUB_ACTIONS_TOKEN: 'token', GITHUB_REPOSITORY: 'owner/repo' };
  await automationWatchdog(env, now);
  await automationWatchdog(env, new Date(now.getTime() + 5 * 60_000));
  assert.equal(dispatches, 1);
  DB.sqlite.close();
});

test('public freebusy notFound falls back to paginated events', async () => {
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.includes('freeBusy')) return Response.json({ calendars: { holidays: { errors: [{ reason: 'notFound' }] } } });
    if (!url.includes('pageToken')) return Response.json({ items: [{ transparency: 'transparent' }], nextPageToken: 'next' });
    return Response.json({ items: [{ start: { date: '2026-09-07' }, end: { date: '2026-09-08' } }] });
  };
  const busy = await busyIntervals('token', ['holidays'], new Date('2026-09-06Z'), new Date('2026-09-09Z'));
  assert.deepEqual(busy, [{ start: '2026-09-07T04:00:00.000Z', end: '2026-09-08T04:00:00.000Z' }]);
});
