CREATE TABLE IF NOT EXISTS study_sessions (
  session_id TEXT PRIMARY KEY,
  task_uid TEXT NOT NULL,
  title TEXT NOT NULL,
  course_name TEXT NOT NULL,
  start_at TEXT NOT NULL,
  end_at TEXT NOT NULL,
  task_due_at TEXT NOT NULL,
  calendar_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'scheduled',
  notified INTEGER NOT NULL DEFAULT 0,
  manual_override INTEGER NOT NULL DEFAULT 0,
  message_id TEXT,
  last_synced TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_study_sessions_reminders
  ON study_sessions(status, notified, start_at);

CREATE INDEX IF NOT EXISTS idx_study_sessions_task
  ON study_sessions(task_uid, status);

CREATE TABLE IF NOT EXISTS completed_tasks (
  task_uid TEXT PRIMARY KEY,
  completed_at TEXT NOT NULL
);

-- Durable, resumable Google/D1 mutations. One active operation per task.
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

CREATE TABLE IF NOT EXISTS plan_lease (
  id INTEGER PRIMARY KEY CHECK(id=1),
  token TEXT NOT NULL,
  lease_until INTEGER NOT NULL
);

-- Rescheduling different tasks must also be serialized: they share availability.
CREATE UNIQUE INDEX IF NOT EXISTS idx_single_active_study_operation
  ON study_operations((1)) WHERE status != 'done';

CREATE TABLE IF NOT EXISTS study_preferences (
  name TEXT PRIMARY KEY,
  payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attendr_state_control (
  id INTEGER PRIMARY KEY CHECK(id=1),
  revision INTEGER NOT NULL DEFAULT 0,
  lease_token TEXT,
  lease_until INTEGER NOT NULL DEFAULT 0,
  sha256 TEXT,
  size INTEGER NOT NULL DEFAULT 0,
  chunk_count INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO attendr_state_control(id) VALUES(1);

CREATE TABLE IF NOT EXISTS attendr_state_chunks (
  revision INTEGER NOT NULL,
  chunk_index INTEGER NOT NULL,
  data TEXT NOT NULL,
  PRIMARY KEY(revision, chunk_index)
);
