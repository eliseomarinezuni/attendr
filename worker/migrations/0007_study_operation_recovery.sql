-- Replace global unfinished-operation blocking with bounded, observable recovery.
DROP INDEX IF EXISTS idx_single_active_study_operation;

ALTER TABLE study_operations ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE study_operations ADD COLUMN next_retry_at INTEGER NOT NULL DEFAULT 0;
ALTER TABLE study_operations ADD COLUMN last_attempt_at INTEGER;
ALTER TABLE study_operations ADD COLUMN last_error_code TEXT;
ALTER TABLE study_operations ADD COLUMN intent_applied INTEGER NOT NULL DEFAULT 0;
ALTER TABLE study_operations ADD COLUMN created_at INTEGER NOT NULL DEFAULT 0;
ALTER TABLE study_operations ADD COLUMN updated_at INTEGER NOT NULL DEFAULT 0;
ALTER TABLE study_operations ADD COLUMN finished_at INTEGER;

-- Legacy running rows were not actively leased through plan_lease. Make them due
-- for the new recovery worker without discarding their durable payload.
UPDATE study_operations
SET status='retryable', lease_until=0, next_retry_at=0,
    created_at=CAST(strftime('%s','now') AS INTEGER)*1000,
    updated_at=CAST(strftime('%s','now') AS INTEGER)*1000
WHERE status!='done';

UPDATE study_operations
SET created_at=CAST(strftime('%s','now') AS INTEGER)*1000,
    updated_at=CAST(strftime('%s','now') AS INTEGER)*1000,
    finished_at=CAST(strftime('%s','now') AS INTEGER)*1000
WHERE status='done';

CREATE INDEX IF NOT EXISTS idx_study_operations_recovery
  ON study_operations(status,next_retry_at,lease_until);
