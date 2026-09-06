-- Apply to existing D1 databases before deploying the P1 Worker.
CREATE TABLE IF NOT EXISTS study_operations (
  interaction_id TEXT PRIMARY KEY,
  task_uid TEXT NOT NULL,
  action TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL,
  lease_until INTEGER NOT NULL,
  target_start TEXT,
  target_end TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_study_operation
  ON study_operations(task_uid) WHERE status != 'done';

-- Canonical UTC milliseconds keep lexical comparisons and equality consistent.
UPDATE study_sessions SET
  start_at = strftime('%Y-%m-%dT%H:%M:%fZ', start_at),
  end_at = strftime('%Y-%m-%dT%H:%M:%fZ', end_at),
  task_due_at = strftime('%Y-%m-%dT%H:%M:%fZ', task_due_at);

CREATE TABLE IF NOT EXISTS plan_lease (
  id INTEGER PRIMARY KEY CHECK(id=1),
  token TEXT NOT NULL,
  lease_until INTEGER NOT NULL
);

-- Rescheduling different tasks must also be serialized: they share availability.
CREATE UNIQUE INDEX IF NOT EXISTS idx_single_active_study_operation
  ON study_operations((1)) WHERE status != 'done';
