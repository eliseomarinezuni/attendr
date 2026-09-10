import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import test, { afterEach } from 'node:test';
import ts from 'typescript';

import {
  applyMigration,
  applyMigrationChain,
  freshDatabase,
  historicalDatabase,
  migrationFiles,
  schemaSignature,
} from './helpers/database.mjs';

const source = readFileSync(new URL('../src/index.ts', import.meta.url), 'utf8') +
  '\nexport { answerAsk, automationWatchdog };';
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
});
const { default: worker, answerAsk, automationWatchdog } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
);
const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

const sessionId = 'attendr:study:' + 'a'.repeat(20) + ':1';
const auth = { authorization: 'Bearer secret' };

function offsetIso(date) {
  return new Date(date.getTime() - 4 * 3600_000).toISOString().replace('Z', '-04:00');
}

function seedBaseDatabase(DB, validDates = true, legacyOffsets = true) {
  const format = legacyOffsets ? offsetIso : (date) => date.toISOString();
  const start = validDates ? format(new Date(Date.now() + 60_000)) : 'not-a-date';
  const end = validDates ? format(new Date(Date.now() + 30 * 60_000)) : 'also-invalid';
  const due = validDates ? format(new Date(Date.now() + 86400_000)) : 'invalid-due';
  DB.sqlite.prepare(`INSERT INTO study_sessions
    (session_id,task_uid,title,course_name,start_at,end_at,task_due_at,calendar_id,event_id,last_synced,updated_at)
    VALUES(?,?,?,?,?,?,?,?,?,?,?)`).run(
      sessionId, 'assignment:1', 'Historical study block', 'Web Development', start, end,
      due, 'study-calendar', 'event-1', 'legacy-sync', 'legacy-update',
    );
  DB.sqlite.prepare('INSERT INTO completed_tasks VALUES(?,?)')
    .run('assignment:completed', '2026-09-01T12:00:00Z');
  return { start };
}

function migratedDatabaseWithHistory() {
  const DB = historicalDatabase();
  const historical = seedBaseDatabase(DB);
  applyMigrationChain(DB.sqlite, (name, sqlite) => {
    if (name === '0002_reliability.sql') {
      sqlite.prepare('INSERT INTO study_operations VALUES(?,?,?,?,?,?,?,?)').run(
        'interaction-1', 'assignment:completed', 'complete', '{}', 'done', 0, null, null,
      );
      sqlite.prepare('INSERT INTO plan_lease VALUES(1,?,0)').run('a'.repeat(32));
    } else if (name === '0003_study_preferences.sql') {
      const windows = Object.fromEntries(Array.from({ length: 7 }, (_, day) => [String(day), day === 1 ? [[600, 660]] : []]));
      sqlite.prepare("INSERT INTO study_preferences VALUES('profile',?)").run(JSON.stringify({ windows }));
    } else if (name === '0004_cloud_state.sql') {
      sqlite.prepare(`UPDATE attendr_state_control SET revision=7,sha256=?,size=3,chunk_count=1 WHERE id=1`)
        .run(createHash('sha256').update('abc').digest('hex'));
      sqlite.prepare("INSERT INTO attendr_state_chunks VALUES(7,0,'YWJj')").run();
    } else if (name === '0005_ask.sql') {
      const course = { key: 'web-development', name: 'Web Development', match: ['web dev'] };
      const record = { id: 'syllabus', hash: 'b'.repeat(64), title: 'Midterm', type: 'syllabus',
        url: 'https://canvas.example/syllabus', updated_at: null, module: null,
        chunks: ['Midterm: October 20, 2026 at 14:00.'], deadline: null };
      sqlite.prepare("INSERT INTO ask_courses VALUES(?,?,'2026-09-01T00:00:00.000Z')")
        .run(course.key, JSON.stringify(course));
      sqlite.prepare('INSERT INTO ask_sources VALUES(?,?,?,?)')
        .run(course.key, record.id, record.hash, JSON.stringify(record));
      sqlite.prepare("INSERT INTO ask_requests VALUES('ask-1',1)").run();
      sqlite.prepare('INSERT INTO ask_staging VALUES(?,?,?,?,?)')
        .run(course.key, '2026-09-02T00:00:00.000Z', 'staged', 'c'.repeat(64), JSON.stringify({ ...record, id: 'staged' }));
    } else if (name === '0006_automation_watchdog.sql') {
      sqlite.prepare("INSERT INTO automation_heartbeats VALUES('academic','success','legacy-run',1,2,2)").run();
      sqlite.prepare("INSERT INTO automation_watchdog VALUES('academic',1)").run();
    }
  });
  return { DB, historical };
}

async function post(DB, path, body, env = {}) {
  return worker.fetch(new Request(`https://worker.test${path}`, {
    method: 'POST', headers: auth, body: JSON.stringify(body),
  }), { DB, STUDY_SYNC_SECRET: 'secret', ...env }, {});
}

test('clean historical database applies the complete ordered migration chain', () => {
  assert.deepEqual(migrationFiles, [
    '0002_reliability.sql',
    '0003_study_preferences.sql',
    '0004_cloud_state.sql',
    '0005_ask.sql',
    '0006_automation_watchdog.sql',
  ]);
  const migrated = historicalDatabase();
  const fresh = freshDatabase();
  applyMigrationChain(migrated.sqlite);
  assert.deepEqual(schemaSignature(migrated.sqlite), schemaSignature(fresh.sqlite));
  assert.deepEqual(
    { ...migrated.sqlite.prepare('SELECT * FROM attendr_state_control').get() },
    { id: 1, revision: 0, lease_token: null, lease_until: 0, sha256: null, size: 0, chunk_count: 0 },
  );
  migrated.sqlite.close();
  fresh.sqlite.close();
});

test('representative rows survive every historical migration', () => {
  const { DB, historical } = migratedDatabaseWithHistory();
  const session = DB.sqlite.prepare('SELECT * FROM study_sessions').get();
  assert.equal(session.title, 'Historical study block');
  assert.equal(session.start_at, new Date(historical.start).toISOString());
  assert.equal(DB.sqlite.prepare('SELECT task_uid FROM completed_tasks').get().task_uid, 'assignment:completed');
  assert.deepEqual(
    { ...DB.sqlite.prepare('SELECT target_start,target_end FROM study_operations').get() },
    { target_start: null, target_end: null },
  );
  assert.deepEqual(
    JSON.parse(DB.sqlite.prepare('SELECT payload FROM study_preferences').get().payload).windows['1'],
    [[600, 660]],
  );
  assert.equal(DB.sqlite.prepare('SELECT revision FROM attendr_state_control').get().revision, 7);
  assert.equal(DB.sqlite.prepare('SELECT data FROM attendr_state_chunks').get().data, 'YWJj');
  assert.equal(DB.sqlite.prepare('SELECT source_id FROM ask_sources').get().source_id, 'syllabus');
  assert.equal(DB.sqlite.prepare('SELECT interaction_id FROM ask_requests').get().interaction_id, 'ask-1');
  assert.equal(DB.sqlite.prepare('SELECT source_id FROM ask_staging').get().source_id, 'staged');
  assert.equal(DB.sqlite.prepare('SELECT run_id FROM automation_heartbeats').get().run_id, 'legacy-run');
  assert.equal(DB.sqlite.prepare('SELECT last_dispatch_at FROM automation_watchdog').get().last_dispatch_at, 1);
  DB.sqlite.close();
});

for (const [kind, createDatabase] of [
  ['fresh schema', freshDatabase],
  ['migrated schema', () => migratedDatabaseWithHistory().DB],
]) {
  test(`current Worker study, ask, cloud-state, and watchdog paths work on ${kind}`, async () => {
    const DB = createDatabase();
    if (kind === 'fresh schema') seedBaseDatabase(DB, true, false);

    let discordSends = 0;
    globalThis.fetch = async () => { discordSends++; return Response.json({ id: 'message-1' }); };
    assert.equal((await post(DB, '/api/reminders/run', {}, {
      DISCORD_BOT_TOKEN: 'bot', DISCORD_STUDY_CHANNEL_ID: 'channel',
    })).status, 200);
    assert.equal(discordSends, 1);
    assert.equal(DB.sqlite.prepare('SELECT notified FROM study_sessions').get().notified, 1);
    assert.equal((await post(DB, '/api/plan/acquire', { token: 'd'.repeat(32) })).status, 200);
    assert.equal((await post(DB, '/api/plan/release', { token: 'd'.repeat(32) })).status, 200);

    if (kind === 'fresh schema') {
      const course = { key: 'web-development', name: 'Web Development', match: ['web dev'] };
      const record = { id: 'syllabus', hash: 'b'.repeat(64), title: 'Midterm', type: 'syllabus',
        url: null, updated_at: null, module: null, chunks: ['Midterm: October 20, 2026.'], deadline: null };
      DB.sqlite.prepare("INSERT INTO ask_courses VALUES(?,?,'2026-09-01T00:00:00.000Z')")
        .run(course.key, JSON.stringify(course));
      DB.sqlite.prepare('INSERT INTO ask_sources VALUES(?,?,?,?)')
        .run(course.key, record.id, record.hash, JSON.stringify(record));
    }
    assert.match(await answerAsk('web dev when is my midterm', { DB }), /October 20/);

    const stateToken = 'e'.repeat(32);
    const acquired = await post(DB, '/api/state-store/acquire', { token: stateToken });
    const revision = (await acquired.json()).revision;
    const stateHeaders = { ...auth, 'x-attendr-lease': stateToken };
    const read = await worker.fetch(new Request('https://worker.test/api/state-store', { headers: stateHeaders }),
      { DB, STUDY_SYNC_SECRET: 'secret' }, {});
    assert.equal(read.status, 200);
    const bytes = Buffer.from('xyz');
    const write = await worker.fetch(new Request('https://worker.test/api/state-store', {
      method: 'PUT', headers: stateHeaders, body: JSON.stringify({ revision,
        sha256: createHash('sha256').update(bytes).digest('hex'), size: bytes.length,
        chunks: [bytes.toString('base64')] }),
    }), { DB, STUDY_SYNC_SECRET: 'secret' }, {});
    assert.deepEqual(await write.json(), { revision: revision + 1 });

    assert.equal((await post(DB, '/api/automation/heartbeat', {
      name: 'academic', status: 'success', run_id: 'current-run',
    })).status, 200);
    DB.sqlite.exec("UPDATE automation_heartbeats SET updated_at=0; UPDATE automation_watchdog SET last_dispatch_at=0");
    let dispatches = 0;
    globalThis.fetch = async () => { dispatches++; return new Response(null, { status: 204 }); };
    await automationWatchdog({ DB, GITHUB_ACTIONS_TOKEN: 'token', GITHUB_REPOSITORY: 'owner/repo' },
      new Date('2026-09-09T16:00:00Z'));
    assert.equal(dispatches, 1);
    DB.sqlite.close();
  });
}

test('migrated and fresh schemas enforce the same critical constraints and indexes', () => {
  for (const DB of [freshDatabase(), historicalDatabase()]) {
    if (!DB.sqlite.prepare("SELECT 1 FROM sqlite_schema WHERE name='study_preferences'").get()) {
      applyMigrationChain(DB.sqlite);
    }
    const indexes = new Set(DB.sqlite.prepare("SELECT name FROM sqlite_schema WHERE type='index'").all().map((row) => row.name));
    assert.ok(indexes.has('idx_study_sessions_reminders'));
    assert.ok(indexes.has('idx_study_sessions_task'));
    assert.ok(indexes.has('idx_active_study_operation'));
    assert.ok(indexes.has('idx_single_active_study_operation'));
    assert.throws(() => DB.sqlite.prepare("INSERT INTO automation_heartbeats VALUES('x','invalid','r',1,NULL,1)").run());
    assert.throws(() => DB.sqlite.prepare("INSERT INTO plan_lease VALUES(2,'token',1)").run());
    assert.throws(() => DB.sqlite.prepare("INSERT INTO ask_courses VALUES('x','not-json','now')").run());
    DB.sqlite.prepare("INSERT INTO study_operations VALUES('1','task','x','{}','running',1,NULL,NULL)").run();
    assert.throws(() => DB.sqlite.prepare("INSERT INTO study_operations VALUES('2','other','x','{}','running',1,NULL,NULL)").run());
    DB.sqlite.close();
  }
});

test('a failing real migration rolls back and cannot look like a trusted final schema', () => {
  const DB = historicalDatabase();
  seedBaseDatabase(DB, false);
  assert.throws(() => applyMigration(DB.sqlite, '0002_reliability.sql'), /NOT NULL/);
  assert.equal(DB.sqlite.prepare('SELECT start_at FROM study_sessions').get().start_at, 'not-a-date');
  assert.equal(DB.sqlite.prepare("SELECT 1 FROM sqlite_schema WHERE name='study_operations'").get(), undefined);
  assert.equal(DB.sqlite.prepare("SELECT 1 FROM sqlite_schema WHERE name='plan_lease'").get(), undefined);
  assert.equal(DB.sqlite.prepare("SELECT 1 FROM sqlite_schema WHERE name='study_preferences'").get(), undefined);
  DB.sqlite.close();
});
