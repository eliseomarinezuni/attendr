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
