-- Original production schema predating the numbered D1 migrations.
-- Preserved from worker/schema.sql at commit 418f4b1d1e6a79eeae1aa71ce33a2c83541676aa.
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
