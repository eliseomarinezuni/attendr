# P1 reliability and rollout

The default production repository is `ATTENDR_DB` (`data/attendr.db`). One database represents one Canvas/Google/Discord account configuration. Keep it on a persistent local disk, outside a runner checkout. Do not use a shared network filesystem, Git commits, Actions caches, or independently restored database copies as the authoritative state.

## Local upgrade

Stop existing schedulers and wait for active runs to finish. Back up legacy JSON and SQLite files. From the repository root:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python scripts/state_admin.py --db data/attendr.db migrate --legacy-directory data
```

Set `ATTENDR_DB=data/attendr.db` in `.env`. Remove obsolete `NOTIFICATION_STATE_FILE`, `NOTIFICATION_STATE_DB`, and `LECTURE_QUIZ_STATE_FILE` configuration. The legacy notification file variable remains an import source; it no longer selects JSON production storage. Direct constructor callers may still select the legacy JSON adapter explicitly.

Migration preserves legacy files, validates their format, and imports each notification source transactionally once. Existing lecture-session completion markers are imported into `quiz_history`. Invalid data fails migration instead of resetting deduplication. Material and announcement extraction indexes are read as legacy inputs and moved into the database when updated. Existing Calendar events are adopted by their managed UID when encountered; migration does not create duplicate events or emit adoption notices. Do not run the old implementation against the same accounts after upgrading: its JSON state no longer receives updates.

The migration does not infer historical daily quizzes from changing generated-message fingerprints. An already sent daily quiz from the cutover date may need to be skipped manually for that date. Old announcement cache entries lack source metadata: persistent corrections are retained after those announcements are next observed by this version.

## Persistent scheduled runner

Both scheduled workflows now require one trusted macOS/Linux self-hosted runner labelled `attendr`. Hosted CI still runs tests. Until that runner and repository variable are configured, scheduled jobs will queue. Provisioning is not performed by these code changes.

1. Create a private directory outside the checkout, such as `/srv/attendr-state`, owned by the runner account; set directory permissions to `0700`.
2. Copy the configured `.env`, `credentials.json`, `token.json`, `materials_index.json`, and `announcement_dates_index.json` into it. Preserve existing material files if desired. Keep secret file permissions at `0600`.
3. Create `/srv/attendr-state/venv` with Python 3.11 and install the repository's `requirements.lock` into it. Reinstall this lock after dependency updates.
4. Run the migration against the original legacy directory:

   ```bash
   /srv/attendr-state/venv/bin/python scripts/state_admin.py \
     --db /srv/attendr-state/attendr.db migrate --legacy-directory data
   ```

5. Set repository **variable** `ATTENDR_HOME=/srv/attendr-state`. Attach the `attendr` runner label to exactly one persistent machine. Both workflows share a concurrency group and local process locks. Only trusted default-branch code should execute on this runner.
6. Enable the schedules after the Worker upgrade below, if study controls are configured. A manual workflow dispatch runs the real pipeline and can post messages and change calendars.

The wrapper forces database, material cache, and OAuth paths into `ATTENDR_HOME`. It refuses a missing database, so a lost volume cannot silently create empty deduplication state. It preserves refreshed `token.json` across runs. Browser authorization remains explicit through `scripts/setup_google.py`; a revoked refresh token requires reauthorization using the persistent configuration paths. The old GitHub-secret upload helper is retained for compatibility but is not used by these workflows.

## State and delivery semantics

- `seen_announcements` and `sent_notifications`: successful delivery fingerprints, including imported legacy records.
- `synced_assignments`: Calendar ID, source UID, Google event ID, desired body, fingerprint, action, revision, and pending/synced status. Active-course assignments are revisited after leaving the lookahead window. A successfully fetched assignment with its due date removed explicitly removes its managed deadline; inaccessible/404 assignments fail closed and require review. Inactive courses are left untouched.
- `quiz_history`: immutable generated payload per local day or lecture session, with completion timestamp. Cached lecture quizzes can finish without downloading slides again. `--force` consumes no normal delivery or quiz completion state.
- `delivery_outbox`: immutable payloads, per-message keys, claim tokens, attempts, remote message IDs where returned, and pending/sending/sent/uncertain/failed status. Oversized multi-embed quizzes split into independently recoverable messages while preserving answer spoilers.
- `extraction_cache`: validated material and announcement extraction state. Invalidation includes content, model, extraction version, timezone, and timetable context. Missing previously indexed active-course syllabuses retain cached findings and block destructive reconciliation.
- `runs` and `schema_migrations`: execution status and schema version. Abruptly terminated runs remain `running`, providing evidence of interrupted execution.

Calendar insert IDs are deterministic. Pending inserts reconcile by event ID after interrupted requests; 409 conflicts are verified against managed content. Calendar comparisons inspect actual managed fields, including equivalent RFC3339 instants, rather than trusting the stored fingerprint. Notification intent is confirmed in the same SQLite transaction as successful Calendar mapping updates, and pending notifications drain even when a later Calendar write fails. The database lock serializes normal local runs.

There is **no exactly-once transaction across SQLite, Discord, Google, and D1**. In particular, a lost Discord response or expired sending lease becomes `uncertain`; automatic retries could duplicate a real message, so operator reconciliation is required. Rate limits with a known rejected response remain pending. Permanent Discord rejection becomes failed.

```bash
.venv/bin/python scripts/state_admin.py --db data/attendr.db list
# After checking the destination, use the exact key and fingerprint from list:
.venv/bin/python scripts/state_admin.py --db data/attendr.db resolve KEY FINGERPRINT --as sent
# Use --as pending only after confirming the message was not delivered.
```

A failed delivery also requires configuration repair before setting it pending. Do not edit outbox payloads: change the source and create a new delivery revision. Database and backups contain private academic text; never commit them.

Use SQLite's online backup API through the helper; the destination must not exist:

```bash
.venv/bin/python scripts/state_admin.py --db data/attendr.db backup /private/backup/attendr-2026-09-06.db
```

Restoring an older backup can replay side effects that happened after the backup. Reconcile those deliveries before restarting schedules.

## Worker upgrade

The Worker changes require D1 schema migration before deployment. Stop Python scheduling during the transition. From `worker/`, after installing dependencies with `npm ci`:

```bash
npx wrangler d1 execute attendr-study --remote --file=migrations/0002_reliability.sql
npm run deploy
```

These are deployment instructions, not commands executed as part of the local implementation. The migration adds resumable operations and canonicalizes existing timestamps to UTC milliseconds. Invalid legacy timestamps fail rather than silently becoming valid dates. New installations can use `schema.sql`.

Signed interactions now have a five-minute freshness bound, persistent interaction IDs, and one active operation globally. Selected reschedule destinations are saved before Google PATCH; interrupted operations replay the same destination. Google deletes tolerate already-deleted events. Successful D1 mutations are recorded before editing the Discord response, preventing a failed response update from repeating the action. Recovery runs from the five-minute Worker schedule after an expired ten-minute operation lease. Pending operations block new Python study reconciliation when observed.

Reminders claim `notified=-1` before sending. A lost response remains unresolved. Inspect the Discord channel and the D1 row before setting it to `1` (confirmed sent) or `0` (confirmed absent). Python and Worker FreeBusy requests paginate all calendars, batch groups of 50, and reject incomplete coverage.

A shared D1 planner lease excludes button operations while Python reads availability and updates Calendar. Button operations are serialized globally because different tasks share calendar openings. Claims capture the current session atomically. The planner stops starting Calendar mutations after 15 minutes; its server lease lasts 20 minutes, with Calendar HTTP calls bounded to 60 seconds. A crashed planner can temporarily block study controls until lease expiry. Pending Worker sync payloads are cached locally and retried under the next acquired lease. The Worker availability windows currently use America/Toronto; retain that timezone for online study controls.

## Verification

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -B -m pytest
(cd worker && npm ci && npm test && npm run check)
```

Pytest blocks network sockets by default. Tests use temporary databases and mocked services, including concurrent claims, uncertain delivery, partial quiz delivery, Calendar write/commit failures, late submission reconciliation, cached quiz reuse, FreeBusy pagination/errors, signed-request freshness, and Google/D1 partial recovery. Worker tests use Node 22.13+ and its in-memory SQLite implementation. `scripts/test_*.py` remain live smoke tests and are excluded from pytest discovery.
