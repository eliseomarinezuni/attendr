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
  target_end TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_retry_at INTEGER NOT NULL DEFAULT 0,
  last_attempt_at INTEGER,
  last_error_code TEXT,
  intent_applied INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0,
  finished_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_study_operation
  ON study_operations(task_uid) WHERE status != 'done';
CREATE INDEX IF NOT EXISTS idx_study_operations_recovery
  ON study_operations(status,next_retry_at,lease_until);

CREATE TABLE IF NOT EXISTS plan_lease (
  id INTEGER PRIMARY KEY CHECK(id=1),
  token TEXT NOT NULL,
  lease_until INTEGER NOT NULL
);

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
CREATE TABLE IF NOT EXISTS ask_courses (
  course_key TEXT PRIMARY KEY,
  metadata TEXT NOT NULL CHECK(json_valid(metadata)),
  synced_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ask_sources (
  course_key TEXT NOT NULL REFERENCES ask_courses(course_key) ON DELETE CASCADE,
  source_id TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  record TEXT NOT NULL CHECK(json_valid(record)),
  PRIMARY KEY(course_key, source_id)
);
CREATE TABLE IF NOT EXISTS ask_requests (
  interaction_id TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ask_staging (
  course_key TEXT NOT NULL,
  revision TEXT NOT NULL,
  source_id TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  record TEXT NOT NULL CHECK(json_valid(record)),
  PRIMARY KEY(course_key, revision, source_id)
);

CREATE TABLE IF NOT EXISTS automation_heartbeats (
  name TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK(status IN ('started','success','failure','degraded')),
  run_id TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  finished_at INTEGER,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS automation_watchdog (
  name TEXT PRIMARY KEY,
  last_dispatch_at INTEGER NOT NULL
);
