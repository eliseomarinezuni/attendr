import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import test from 'node:test';
import { loadWorker } from './helpers/worker.mjs';
import { freshDatabase } from './helpers/database.mjs';

const { default: worker } = await loadWorker([]);
const owner = 'a'.repeat(32);
const successor = 'b'.repeat(32);
const env = DB => ({ DB, STUDY_SYNC_SECRET: 'secret' });
const headers = token => ({ authorization: 'Bearer secret', 'x-attendr-lease': token });
function payload(text, revision = 0) {
  const bytes = Buffer.from(text);
  return { revision, size: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex'),
    chunks: bytes.toString('base64').match(/.{1,4}/g) };
}
function acquire(DB, token = owner) {
  return worker.fetch(new Request('https://worker.test/api/state-store/acquire', {
    method: 'POST', headers: headers(token), body: JSON.stringify({ token }),
  }), env(DB), {});
}
function write(DB, value, token = owner) {
  return worker.fetch(new Request('https://worker.test/api/state-store', {
    method: 'PUT', headers: headers(token), body: JSON.stringify(value),
  }), env(DB), {});
}
async function read(DB, token = owner) {
  const response = await worker.fetch(new Request('https://worker.test/api/state-store', {
    headers: headers(token),
  }), env(DB), {});
  assert.equal(response.status, 200);
  return response.json();
}
function snapshot(DB) {
  return { control: DB.sqlite.prepare('SELECT * FROM attendr_state_control').get(),
    chunks: DB.sqlite.prepare('SELECT * FROM attendr_state_chunks ORDER BY revision,chunk_index').all() };
}
function pauseNextBatch(DB) {
  const batch = DB.batch;
  let entered, resume;
  const ready = new Promise(resolve => { entered = resolve; });
  const gate = new Promise(resolve => { resume = resolve; });
  DB.batch = async statements => {
    DB.batch = batch;
    entered();
    await gate;
    return batch(statements);
  };
  return { ready, resume };
}

test('multi-chunk publication replaces metadata and all chunks together', async t => {
  const DB = freshDatabase(); t.after(() => DB.sqlite.close());
  await acquire(DB);
  assert.equal((await write(DB, payload('old checkpoint'))).status, 200);
  const next = payload('new checkpoint'.repeat(5), 1);
  assert.equal((await write(DB, next)).status, 200);
  assert.deepEqual(await read(DB), { ...next, revision: 2, chunk_count: next.chunks.length });
  assert.deepEqual(DB.sqlite.prepare('SELECT DISTINCT revision FROM attendr_state_chunks').all().map(r => r.revision), [2]);
});

test('concurrent writers with the same revision have exactly one winner', async t => {
  const DB = freshDatabase(); t.after(() => DB.sqlite.close());
  await acquire(DB);
  const paused = pauseNextBatch(DB);
  const loser = write(DB, payload('losing checkpoint'));
  await paused.ready;
  const winner = payload('winning checkpoint');
  assert.equal((await write(DB, winner)).status, 200);
  const committed = snapshot(DB);
  paused.resume();
  assert.equal((await loser).status, 409);
  assert.deepEqual(snapshot(DB), committed);
  assert.equal((await read(DB)).sha256, winner.sha256);
});

test('an expired writer cannot destroy a successor checkpoint', async t => {
  const DB = freshDatabase(); t.after(() => DB.sqlite.close());
  await acquire(DB);
  await write(DB, payload('initial checkpoint'));
  const paused = pauseNextBatch(DB);
  const stale = write(DB, payload('stale checkpoint', 1));
  await paused.ready;
  DB.sqlite.exec('UPDATE attendr_state_control SET lease_until=0');
  assert.equal((await acquire(DB, successor)).status, 200);
  const next = payload('successor checkpoint', 1);
  assert.equal((await write(DB, next, successor)).status, 200);
  const committed = snapshot(DB);
  paused.resume();
  assert.equal((await stale).status, 409);
  assert.deepEqual(snapshot(DB), committed);
  assert.deepEqual(await read(DB, successor), { ...next, revision: 2, chunk_count: next.chunks.length });
});

test('lease expiry while queued rejects publication even without a successor', async t => {
  const DB = freshDatabase(); t.after(() => DB.sqlite.close());
  await acquire(DB);
  await write(DB, payload('initial checkpoint'));
  const paused = pauseNextBatch(DB);
  const stale = write(DB, payload('expired checkpoint', 1));
  await paused.ready;
  DB.sqlite.exec('UPDATE attendr_state_control SET lease_until=0');
  const previous = snapshot(DB);
  paused.resume();
  assert.equal((await stale).status, 409);
  assert.deepEqual(snapshot(DB), previous);
});

for (const [name, trigger] of [
  ['chunk insertion', 'BEFORE INSERT ON attendr_state_chunks WHEN NEW.chunk_index=1'],
  ['metadata publication', 'BEFORE UPDATE ON attendr_state_control'],
  ['obsolete chunk cleanup', 'BEFORE DELETE ON attendr_state_chunks'],
]) {
  test(`failure during ${name} rolls back the whole checkpoint`, async t => {
    const DB = freshDatabase(); t.after(() => DB.sqlite.close());
    await acquire(DB);
    await write(DB, payload('initial checkpoint'));
    const previous = snapshot(DB);
    DB.sqlite.exec(`CREATE TRIGGER fail_publication ${trigger} BEGIN SELECT RAISE(ABORT,'injected failure'); END`);
    await assert.rejects(write(DB, payload('replacement checkpoint', 1)), /injected failure/);
    assert.deepEqual(snapshot(DB), previous);
    DB.sqlite.exec('DROP TRIGGER fail_publication');
    assert.equal((await write(DB, payload('replacement checkpoint', 1))).status, 200);
  });
}

test('lost-response retry returns conflict without changing the committed checkpoint', async t => {
  const DB = freshDatabase(); t.after(() => DB.sqlite.close());
  await acquire(DB);
  const value = payload('confirmed checkpoint');
  await write(DB, value);
  const committed = snapshot(DB);
  assert.equal((await write(DB, value)).status, 409);
  assert.deepEqual(snapshot(DB), committed);
  assert.equal((await read(DB)).sha256, value.sha256);
});

test('renewal is authenticated and cannot revive an expired or replaced lease', async () => {
  const DB = freshDatabase();
  const renew = token => worker.fetch(new Request('https://test/api/state-store/renew', {
    method: 'POST', headers: headers(token),
  }), env(DB), {});
  await acquire(DB, owner);
  assert.equal((await renew(owner)).status, 200);
  assert.equal((await renew(successor)).status, 409);
  DB.sqlite.exec('UPDATE attendr_state_control SET lease_until=0');
  assert.equal((await renew(owner)).status, 409);
  assert.equal((await worker.fetch(new Request('https://test/api/state-store/renew', {method:'POST'}), env(DB), {})).status, 401);
});
