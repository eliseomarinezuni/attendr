import { readFileSync, readdirSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';

const workerRoot = new URL('../../', import.meta.url);
const migrationsRoot = new URL('../../migrations/', import.meta.url);

export const migrationFiles = readdirSync(migrationsRoot)
  .filter((name) => /^\d{4}_.+\.sql$/.test(name))
  .sort();

export function wrapDatabase(sqlite) {
  const wrap = (sql, values = []) => ({
    bind: (...args) => wrap(sql, args),
    all: async () => ({ results: sqlite.prepare(sql).all(...values) }),
    first: async () => sqlite.prepare(sql).get(...values) ?? null,
    run: () => ({ results: sqlite.prepare(sql).all(...values) }),
  });
  return {
    sqlite,
    prepare: (sql) => wrap(sql),
    batch: async (statements) => {
      sqlite.exec('BEGIN');
      try {
        // Execute without yielding: a D1 transaction cannot interleave with
        // another request. Preserve RETURNING rows like the real batch API.
        const results = statements.map((statement) => statement.run());
        sqlite.exec('COMMIT');
        return results;
      } catch (error) {
        sqlite.exec('ROLLBACK');
        throw error;
      }
    },
  };
}

export function freshDatabase() {
  const sqlite = new DatabaseSync(':memory:');
  sqlite.exec('PRAGMA foreign_keys=ON');
  sqlite.exec(readFileSync(new URL('schema.sql', workerRoot), 'utf8'));
  return wrapDatabase(sqlite);
}

export function historicalDatabase() {
  const sqlite = new DatabaseSync(':memory:');
  sqlite.exec('PRAGMA foreign_keys=ON');
  sqlite.exec(readFileSync(new URL('../fixtures/0001_base.sql', import.meta.url), 'utf8'));
  return wrapDatabase(sqlite);
}

export function applyMigration(sqlite, name) {
  const sql = readFileSync(new URL(name, migrationsRoot), 'utf8');
  sqlite.exec('BEGIN IMMEDIATE');
  try {
    sqlite.exec(sql);
    sqlite.exec('COMMIT');
  } catch (error) {
    sqlite.exec('ROLLBACK');
    throw error;
  }
}

export function applyMigrationChain(sqlite, afterMigration = () => {}) {
  for (const name of migrationFiles) {
    applyMigration(sqlite, name);
    afterMigration(name, sqlite);
  }
}

export function schemaSignature(sqlite) {
  const objects = sqlite.prepare(`SELECT type,name,tbl_name,sql FROM sqlite_schema
    WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name`).all();
  return objects.map((object) => ({
    ...object,
    sql: object.sql?.replace(/\s+/g, ' ').replace(/\s+,/g, ',').replace(/\s+\)/g, ')').trim() ?? null,
  }));
}
