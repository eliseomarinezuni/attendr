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
