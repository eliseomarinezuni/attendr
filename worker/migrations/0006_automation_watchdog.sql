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
