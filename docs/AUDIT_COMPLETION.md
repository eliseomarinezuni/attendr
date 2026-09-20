# September audit completion

All 15 audit findings have corresponding code changes and regression coverage. Items 1–2 began in the preceding changes; this update completes the remaining items.

| # | Priority | Completed change |
|---|---|---|
| 1 | P1 | Atomic D1 checkpoint publication gates every write on lease/revision ownership; rollback preserves the prior checkpoint. |
| 2 | P1 | Shared time recognition preserves 24-hour, AM/PM, noon and midnight deadlines; grounding rejects omitted explicit times. |
| 3 | P1 | Cancelled, example and hypothetical deadlines are excluded in extraction and grounding; versioned cache identities force revalidation. |
| 4 | P1 | Canvas timeouts, rate limits and unreadable advertised pages cannot publish a reduced search inventory as complete. Hidden-tab exceptions remain narrowly classified. |
| 5 | P1 | Authoritative syllabus/timetable scans remove obsolete future managed events. Past events, unscanned source types and failed courses are protected. |
| 6 | P2 | Reversible assignment retirement resolves confirmed permanent deletion; individual Canvas course failures no longer freeze healthy academic Calendar updates. |
| 7 | P2 | Reschedule retries reload current sessions, reject superseded operations, confirm lost PATCH responses and recompute availability instead of replaying stale targets. |
| 8 | P2 | Lecture outbox entries persist lecture-end-based expiry; every send/retry checks it. Legacy lecture entries with unknown expiry are discarded. Explicit forced sends retain their manual override behavior. |
| 9 | P2 | Parent runner renews cloud leases every five minutes while the child works; renewal failure terminates the run's process group. Expired leases cannot be revived by renewal. |
| 10 | P2 | Quiz generation shares bounded source-preserving selection with summaries, including exact separator/header budgeting. |
| 11 | P2 | Bounded extracted-text caching, safe payload retention, compact checkpoints, size monitoring and batching of recomputable cache writes reduce checkpoint growth and uploads. |
| 12 | P2 | Retrieval ranks distinct original sources before limiting answers, including sources split across publication records. |
| 13 | P2 | Study-button smoke test acquires/passes/releases the planner lease and releases it before triggering reminders. |
| 14 | P2 | CI checks all Python application/scripts, enforces formatting and a 75% main-pipeline coverage floor; new tests cover failures and recovery across integration boundaries. |
| 15 | P3 | Worker checkpoint, HTTP, environment and default-configuration modules are separated; pipeline types and deadline rules are shared; timetable field/range/duplicate validation is stricter. |

## Rollout

1. Deploy the updated Worker first: the updated cloud runner requires `/api/state-store/renew`. No D1 schema migration is needed.
2. Deploy the Python runner. SQLite automatically upgrades to schema version 3, preserving existing state and adding delivery expiry and explicit retirement records. Keep a backup before downgrading; older binaries reject schema version 3.
3. The next successful source sync revalidates deadline caches and republishes stable search source identities. Existing search snapshots remain readable before that refresh.

Local tests use synthetic provider responses; this change does not send live messages, modify production calendars, deploy the Worker, or change remote secrets. Run the repaired live button smoke test only when an actual test alert is wanted.

## Safety boundaries

- Timetable/exam merging and global study-plan replacement wait for complete inputs; healthy academic events can update independently.
- A 404 may mean inaccessible data. Assignment retirement is an explicit operator decision, with a matching restoration command.
- Retention removes disposable content, not the markers needed to prevent duplicate delivery. If non-disposable state reaches the checkpoint limit, the run fails safely and requires state review.

## Verification

- 423 Python tests and 25 subtests passed.
- 103 Worker tests passed, using bundled production modules and transactional SQLite fixtures.
- Overall Python coverage: 79.61%; main pipeline: 77% (up from 54%).
- Full-source Pyright: zero errors or warnings. TypeScript, Ruff lint/format and whitespace checks passed.
