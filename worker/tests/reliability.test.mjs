import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import test, { afterEach } from 'node:test';
import { loadWorker } from './helpers/worker.mjs';
import { freshDatabase } from './helpers/database.mjs';

const { default: worker, busyIntervals, allCalendarIds, verifyDiscord, handleButton, recoverOperations, nextSlot, automationWatchdog, discordQuietHoursActive } = await loadWorker(['busyIntervals', 'allCalendarIds', 'verifyDiscord', 'handleButton', 'recoverOperations', 'nextSlot', 'automationWatchdog', 'discordQuietHoursActive']);
const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

function database() {
  return freshDatabase();
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
function reminderRequest() {
  return new Request('https://example.test/api/reminders/run', { method: 'POST', headers: { authorization: 'Bearer secret' } });
}
function reminderEnv(DB) {
  return { DB, STUDY_SYNC_SECRET: 'secret', DISCORD_BOT_TOKEN: 'bot', DISCORD_STUDY_CHANNEL_ID: 'channel' };
}

test('Toronto quiet hours block automated reminders from midnight until nine', () => {
  assert.equal(discordQuietHoursActive(new Date('2026-09-18T06:16:00Z'), '0', '9'), true);
  assert.equal(discordQuietHoursActive(new Date('2026-09-18T12:59:59Z'), '0', '9'), true);
  assert.equal(discordQuietHoursActive(new Date('2026-09-18T13:00:00Z'), '0', '9'), false);
  assert.throws(() => discordQuietHoursActive(new Date(), '9', '9'), /Invalid/);
});

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
  await Promise.all([1, 2].map(() => worker.fetch(reminderRequest(), reminderEnv(DB), {})));
  assert.equal(sends, 1);
  assert.equal(DB.sqlite.prepare('SELECT notified FROM study_sessions').get().notified, 1);
  assert.equal(DB.sqlite.prepare('SELECT count(*) AS count FROM plan_lease').get().count, 0);
  DB.sqlite.close();
});

test('planner cannot acquire while a reminder delivery is in flight', async () => {
  const DB = database();
  const original = session();
  await sync(DB, { sessions: [original] });
  let startSend;
  const sendStarted = new Promise((resolve) => { startSend = resolve; });
  let finishSend;
  globalThis.fetch = async () => {
    startSend();
    return new Promise((resolve) => { finishSend = () => resolve(Response.json({ id: 'message' })); });
  };
  const delivery = worker.fetch(reminderRequest(), reminderEnv(DB), {});
  await sendStarted;
  assert.equal(DB.sqlite.prepare('SELECT notified FROM study_sessions').get().notified, -1);
  assert.equal((await lease(DB, 'acquire', 'b'.repeat(32))).status, 409);
  const shifted = { ...original,
    start: new Date(Date.now() + 86400_000).toISOString(),
    end: new Date(Date.now() + 88200_000).toISOString(),
    task_due_at: new Date(Date.now() + 2 * 86400_000).toISOString() };
  const rejectedSync = await worker.fetch(new Request('https://example.test/api/sessions/sync', {
    method: 'POST', headers: { authorization: 'Bearer secret' },
    body: JSON.stringify({ sessions: [shifted], plan_token: 'b'.repeat(32) }),
  }), { DB, STUDY_SYNC_SECRET: 'secret' }, {});
  assert.equal(rejectedSync.status, 409);
  assert.equal(DB.sqlite.prepare('SELECT start_at FROM study_sessions').get().start_at, original.start);
  finishSend();
  assert.equal((await delivery).status, 200);
  assert.equal(DB.sqlite.prepare('SELECT notified FROM study_sessions').get().notified, 1);
  assert.equal((await lease(DB, 'acquire', 'b'.repeat(32))).status, 200);
  await lease(DB, 'release', 'b'.repeat(32));
  DB.sqlite.close();
});

test('lost reminder response stays unresolved instead of automatically repeating', async () => {
  const DB = database();
  await sync(DB, { sessions: [session()] });
  let sends = 0;
  globalThis.fetch = async () => { sends++; throw new Error('response lost'); };
  await worker.fetch(reminderRequest(), reminderEnv(DB), {});
  assert.equal((await lease(DB, 'acquire', 'b'.repeat(32))).status, 409);
  DB.sqlite.exec('UPDATE plan_lease SET lease_until=0');
  assert.equal((await lease(DB, 'acquire', 'b'.repeat(32))).status, 200);
  await lease(DB, 'release', 'b'.repeat(32));
  await worker.fetch(reminderRequest(), reminderEnv(DB), {});
  assert.equal(sends, 1);
  assert.equal(DB.sqlite.prepare('SELECT notified FROM study_sessions').get().notified, -1);
  DB.sqlite.close();
});

test('crash before reminder claim recovers after lease expiry without losing the reminder', async () => {
  const DB = database();
  await sync(DB, { sessions: [session()] });
  const prepare = DB.prepare;
  let failClaim = true;
  DB.prepare = (sql) => {
    if (failClaim && sql.startsWith('UPDATE study_sessions SET notified=-1')) {
      failClaim = false;
      throw new Error('injected claim failure');
    }
    return prepare(sql);
  };
  await assert.rejects(worker.fetch(reminderRequest(), reminderEnv(DB), {}), /injected claim failure/);
  assert.equal(DB.sqlite.prepare('SELECT notified FROM study_sessions').get().notified, 0);
  assert.equal((await lease(DB, 'acquire', 'b'.repeat(32))).status, 409);
  DB.sqlite.exec('UPDATE plan_lease SET lease_until=0');
  let sends = 0;
  globalThis.fetch = async () => { sends++; return Response.json({ id: 'message' }); };
  assert.equal((await worker.fetch(reminderRequest(), reminderEnv(DB), {})).status, 200);
  assert.equal(sends, 1);
  assert.equal(DB.sqlite.prepare('SELECT notified FROM study_sessions').get().notified, 1);
  DB.sqlite.close();
});

test('planner cancellation cannot race an obsolete reminder delivery', async () => {
  const DB = database();
  await sync(DB, { sessions: [session()] });
  assert.equal((await lease(DB, 'acquire')).status, 200);
  let sends = 0;
  globalThis.fetch = async () => { sends++; return Response.json({ id: 'message' }); };
  await worker.fetch(reminderRequest(), reminderEnv(DB), {});
  const cancelled = await worker.fetch(new Request('https://example.test/api/sessions/sync', {
    method: 'POST', headers: { authorization: 'Bearer secret' },
    body: JSON.stringify({ sessions: [], plan_token: 'a'.repeat(32) }),
  }), { DB, STUDY_SYNC_SECRET: 'secret' }, {});
  assert.equal(cancelled.status, 200);
  await lease(DB, 'release');
  await worker.fetch(reminderRequest(), reminderEnv(DB), {});
  assert.equal(sends, 0);
  assert.equal(DB.sqlite.prepare('SELECT status FROM study_sessions').get().status, 'cancelled');
  DB.sqlite.close();
});

test('planner reschedule cannot race an obsolete reminder delivery', async () => {
  const DB = database();
  const original = session();
  await sync(DB, { sessions: [original] });
  assert.equal((await lease(DB, 'acquire')).status, 200);
  let sends = 0;
  globalThis.fetch = async () => { sends++; return Response.json({ id: 'message' }); };
  await worker.fetch(reminderRequest(), reminderEnv(DB), {});
  const later = new Date(Date.now() + 86400_000).toISOString();
  const laterEnd = new Date(new Date(later).getTime() + 1800_000).toISOString();
  const laterDue = new Date(Date.now() + 2 * 86400_000).toISOString();
  const rescheduled = await worker.fetch(new Request('https://example.test/api/sessions/sync', {
    method: 'POST', headers: { authorization: 'Bearer secret' },
    body: JSON.stringify({ sessions: [{ ...original, start: later, end: laterEnd, task_due_at: laterDue }], plan_token: 'a'.repeat(32) }),
  }), { DB, STUDY_SYNC_SECRET: 'secret' }, {});
  assert.equal(rescheduled.status, 200);
  await lease(DB, 'release');
  await worker.fetch(reminderRequest(), reminderEnv(DB), {});
  assert.equal(sends, 0);
  assert.equal(DB.sqlite.prepare('SELECT start_at FROM study_sessions').get().start_at, later);
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

test('D1 intent failure is retained and recovery completes safely', async () => {
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
  await handleButton(interaction, { DB });
  assert.equal(DB.sqlite.prepare('SELECT status FROM study_operations').get().status, 'retryable');
  DB.sqlite.exec('UPDATE study_operations SET next_retry_at=0');
  await recoverOperations({ DB });
  assert.equal(DB.sqlite.prepare('SELECT status FROM study_sessions').get().status, 'completed');
  assert.equal(DB.sqlite.prepare('SELECT status FROM study_operations').get().status, 'done');
  await handleButton(interaction, { DB });
  assert.equal(deletions, 1);
  DB.sqlite.close();
});

test('planner lease queues a button without losing it', async () => {
  const DB = database();
  await sync(DB, { sessions: [session()] });
  assert.equal((await lease(DB, 'acquire')).status, 200);
  assert.equal((await lease(DB, 'acquire', 'b'.repeat(32))).status, 409);
  let writes = 0;
  globalThis.fetch = async () => { writes++; return Response.json({}); };
  await handleButton({ id: '987', token: '', data: { custom_id: 'study:complete:' + sessionId } }, { DB });
  assert.equal(writes, 0);
  assert.equal(DB.sqlite.prepare('SELECT status FROM study_operations').get().status, 'pending');
  await lease(DB, 'release');
  assert.equal((await lease(DB, 'acquire', 'b'.repeat(32))).status, 200);
  await lease(DB, 'release', 'b'.repeat(32));
  globalThis.fetch = async (url, init = {}) => {
    if (String(url).includes('/token')) return Response.json({ access_token: 'fake' });
    if (init.method === 'DELETE') return new Response(null, { status: 204 });
    return Response.json({});
  };
  await recoverOperations({ DB });
  assert.equal(DB.sqlite.prepare('SELECT status FROM study_operations').get().status, 'done');
  DB.sqlite.close();
});

test('crashed planner lease expires and does not permanently block mutations', async () => {
  const DB = database();
  assert.equal((await lease(DB, 'acquire')).status, 200);
  assert.equal((await lease(DB, 'acquire', 'b'.repeat(32))).status, 409);
  DB.sqlite.exec('UPDATE plan_lease SET lease_until=0');
  assert.equal((await lease(DB, 'acquire', 'b'.repeat(32))).status, 200);
  await lease(DB, 'release', 'b'.repeat(32));
  DB.sqlite.close();
});

test('permanent Worker OAuth failure is terminal and does not starve planner or reminders', async () => {
  const DB = database();
  const second = { ...session(), session_id: 'attendr:study:' + 'b'.repeat(20) + ':1', task_uid: 'assignment:2', event_id: 'event-2' };
  await sync(DB, { sessions: [session(), second] });
  let tokenCalls = 0;
  globalThis.fetch = async (url) => {
    if (String(url).includes('/token')) { tokenCalls++; return new Response('{}', { status: 400 }); }
    return Response.json({ id: 'message' });
  };
  await handleButton({ id: '1001', type: 3, token: '', application_id: '', data: { custom_id: 'study:complete:' + sessionId } }, { DB });
  const operation = DB.sqlite.prepare('SELECT status,attempt_count,last_error_code,intent_applied FROM study_operations').get();
  assert.deepEqual({ ...operation }, { status: 'failed_terminal', attempt_count: 1, last_error_code: 'google_oauth_invalid', intent_applied: 1 });
  assert.equal(DB.sqlite.prepare('SELECT status FROM study_sessions WHERE session_id=?').get(sessionId).status, 'completed');
  await recoverOperations({ DB });
  assert.equal(tokenCalls, 1);
  assert.equal((await lease(DB, 'acquire', 'b'.repeat(32))).status, 200);
  await lease(DB, 'release', 'b'.repeat(32));
  let reminderSends = 0;
  globalThis.fetch = async () => { reminderSends++; return Response.json({ id: 'message' }); };
  await worker.fetch(reminderRequest(), reminderEnv(DB), {});
  assert.equal(reminderSends, 1);
  DB.sqlite.close();
});

test('transient operation failures use bounded retries and then stop', async () => {
  const DB = database();
  await sync(DB, { sessions: [session()] });
  let tokenCalls = 0;
  globalThis.fetch = async (url) => {
    if (String(url).includes('/token')) { tokenCalls++; return new Response('{}', { status: 503 }); }
    return Response.json({});
  };
  await handleButton({ id: '1002', type: 3, token: '', application_id: '', data: { custom_id: 'study:complete:' + sessionId } }, { DB });
  for (let attempt = 1; attempt < 5; attempt++) {
    DB.sqlite.exec('UPDATE study_operations SET next_retry_at=0');
    await recoverOperations({ DB });
  }
  const operation = DB.sqlite.prepare('SELECT status,attempt_count,last_error_code FROM study_operations').get();
  assert.deepEqual({ ...operation }, { status: 'failed_terminal', attempt_count: 5, last_error_code: 'google_unavailable' });
  await recoverOperations({ DB });
  assert.equal(tokenCalls, 5);
  DB.sqlite.close();
});

test('temporary failure recovers and an already deleted Calendar event is success', async () => {
  const DB = database();
  await sync(DB, { sessions: [session()] });
  let healthy = false;
  globalThis.fetch = async (url, init = {}) => {
    if (String(url).includes('/token')) return healthy ? Response.json({ access_token: 'fake' }) : new Response('{}', { status: 503 });
    if (init.method === 'DELETE') return new Response(null, { status: 410 });
    return Response.json({});
  };
  await handleButton({ id: '1003', type: 3, token: '', application_id: '', data: { custom_id: 'study:complete:' + sessionId } }, { DB });
  assert.equal(DB.sqlite.prepare('SELECT status FROM study_operations').get().status, 'effects_pending');
  healthy = true;
  DB.sqlite.exec('UPDATE study_operations SET next_retry_at=0');
  await recoverOperations({ DB });
  assert.equal(DB.sqlite.prepare('SELECT status FROM study_operations').get().status, 'done');
  DB.sqlite.close();
});

test('completion intent is durable before the external delete and supports yesterday sessions', async () => {
  const DB = database();
  const yesterday = { ...session(),
    start: new Date(Date.now() - 86400_000).toISOString(),
    end: new Date(Date.now() - 84600_000).toISOString(),
    task_due_at: new Date(Date.now() + 86400_000).toISOString() };
  await sync(DB, { sessions: [yesterday] });
  globalThis.fetch = async (url, init = {}) => {
    if (String(url).includes('/token')) return Response.json({ access_token: 'fake' });
    if (init.method === 'DELETE') {
      assert.equal(DB.sqlite.prepare('SELECT status FROM study_sessions').get().status, 'completed');
      assert.equal(DB.sqlite.prepare('SELECT intent_applied FROM study_operations').get().intent_applied, 1);
      return new Response(null, { status: 204 });
    }
    return Response.json({});
  };
  await handleButton({ id: '1004', type: 3, token: '', application_id: '', data: { custom_id: 'study:complete:' + sessionId } }, { DB });
  assert.equal(DB.sqlite.prepare('SELECT status FROM study_operations').get().status, 'done');
  DB.sqlite.close();
});

test('Discord acknowledgement failure does not reopen a completed operation', async () => {
  const DB = database();
  await sync(DB, { sessions: [session()] });
  globalThis.fetch = async (url, init = {}) => {
    if (String(url).includes('/token')) return Response.json({ access_token: 'fake' });
    if (init.method === 'DELETE') return new Response(null, { status: 204 });
    throw new Error('Discord unavailable');
  };
  await handleButton({ id: '1005', type: 3, token: 'discord-token', application_id: 'app', data: { custom_id: 'study:complete:' + sessionId } }, { DB });
  assert.equal(DB.sqlite.prepare('SELECT status FROM study_operations').get().status, 'done');
  DB.sqlite.close();
});

test('safe operation inspection omits payload and manual retry completes after OAuth repair', async () => {
  const DB = database();
  await sync(DB, { sessions: [session()] });
  globalThis.fetch = async (url, init = {}) => {
    if (String(url).includes('/token')) return new Response('{}', { status: 400 });
    return Response.json({});
  };
  await handleButton({ id: '1006', type: 3, token: '', application_id: '', data: { custom_id: 'study:complete:' + sessionId } }, { DB });
  const inspected = await worker.fetch(new Request('https://example.test/api/operations', { headers: { authorization: 'Bearer secret' } }), { DB, STUDY_SYNC_SECRET: 'secret' }, {});
  const body = await inspected.json();
  assert.equal(body.operations[0].status, 'failed_terminal');
  assert.equal('payload' in body.operations[0], false);
  assert.equal(JSON.stringify(body).includes('calendar_id'), false);
  globalThis.fetch = async (url, init = {}) => {
    if (String(url).includes('/token')) return Response.json({ access_token: 'fake' });
    if (init.method === 'DELETE') return new Response(null, { status: 404 });
    return Response.json({});
  };
  const retried = await worker.fetch(new Request('https://example.test/api/operations/retry', {
    method: 'POST', headers: { authorization: 'Bearer secret' }, body: JSON.stringify({ interaction_id: '1006' }),
  }), { DB, STUDY_SYNC_SECRET: 'secret' }, {});
  assert.deepEqual(await retried.json(), { interaction_id: '1006', status: 'done' });
  DB.sqlite.close();
});

test('operation logs use safe categories and never include provider error bodies', async () => {
  const DB = database();
  await sync(DB, { sessions: [session()] });
  const logs = [];
  const originalError = console.error;
  console.error = (...values) => logs.push(values);
  try {
    globalThis.fetch = async (url) => {
      if (String(url).includes('/token')) throw new Error('fake-secret-token');
      return Response.json({});
    };
    await handleButton({ id: '1007', type: 3, token: '', application_id: '', data: { custom_id: 'study:complete:' + sessionId } }, { DB });
  } finally {
    console.error = originalError;
  }
  assert.match(JSON.stringify(logs), /google_network/);
  assert.doesNotMatch(JSON.stringify(logs), /fake-secret-token/);
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
