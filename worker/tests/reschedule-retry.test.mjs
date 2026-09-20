import assert from 'node:assert/strict';
import test, { afterEach } from 'node:test';
import { freshDatabase } from './helpers/database.mjs';
import { loadWorker } from './helpers/worker.mjs';
const { reschedule, performOperation } = await loadWorker(['reschedule', 'performOperation']);
const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });
const iso = delta => new Date(Date.now()+delta).toISOString();
function fixture() {
  const DB = freshDatabase();
  const session = {session_id:'s', task_uid:'task', start_at:iso(3600_000), end_at:iso(5400_000),
    task_due_at:iso(7*86400_000), calendar_id:'study', event_id:'event', status:'scheduled'};
  const operation = {interaction_id:'i', action:'reschedule', payload:JSON.stringify(session),
    target_start:iso(-86400_000), target_end:iso(-84600_000)};
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push([String(url), init]);
    if (String(url).includes('oauth2')) return Response.json({access_token:'test'});
    if (init?.method === 'PATCH') return Response.json({});
    if (String(url).includes('/events/event')) return Response.json({start:{dateTime:session.start_at},end:{dateTime:session.end_at}});
    if (String(url).includes('calendarList')) return Response.json({items:[{id:'study'},{id:'personal'}]});
    if (String(url).includes('freeBusy')) return Response.json({calendars:{personal:{busy:[]}}});
    throw new Error(`Unexpected request ${url}`);
  };
  return { DB, session, operation, calls };
}
test('past retry target is replaced only after current availability is checked', async () => {
  const {DB,session,operation,calls} = fixture();
  await reschedule(operation, session, {DB});
  const patch = calls.find(([,init]) => init?.method === 'PATCH');
  assert.ok(patch);
  const body = JSON.parse(patch[1].body);
  assert.ok(Date.parse(body.start.dateTime) > Date.now());
  assert.ok(Date.parse(body.end.dateTime) < Date.parse(session.task_due_at));
  assert.ok(calls.some(([url]) => url.includes('freeBusy')));
});
test('lost PATCH response is confirmed without moving the event again', async () => {
  const {DB,session,operation,calls} = fixture();
  operation.target_start=session.start_at; operation.target_end=session.end_at;
  await reschedule(operation, session, {DB});
  assert.equal(calls.filter(([,init]) => init?.method === 'PATCH').length, 0);
});
test('a retired current session cannot be rescheduled from its old operation snapshot', async () => {
  const {DB,operation,calls} = fixture();
  await assert.rejects(performOperation(operation, {DB}), /session_no_longer_scheduled/);
  assert.equal(calls.length, 0);
});

test('an old button operation cannot overwrite a newer planner decision', async () => {
  const {DB,session,operation,calls} = fixture();
  DB.sqlite.prepare(`INSERT INTO study_sessions(session_id,task_uid,title,course_name,start_at,end_at,task_due_at,calendar_id,event_id,status,last_synced,updated_at)
    VALUES(?,?,'Study','Course',?,?,?,?,?,'scheduled','now','now')`).run(
      session.session_id, session.task_uid, iso(2*86400_000),iso(2*86400_000+1800_000),session.task_due_at,session.calendar_id,session.event_id);
  await assert.rejects(performOperation(operation, {DB}), /session_changed_since_request/);
  assert.equal(calls.length, 0);
});
