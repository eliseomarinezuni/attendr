# P1 reliability and rollout

The default local repository is `ATTENDR_DB` (`data/attendr.db`). Scheduled GitHub-hosted runs restore it from an encrypted Cloudflare D1 checkpoint. One database represents one Canvas/Google/Discord account configuration.

## Local upgrade

Stop existing schedulers and wait for active runs to finish. Back up legacy JSON and SQLite files. From the repository root:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python scripts/state_admin.py --db data/attendr.db migrate --legacy-directory data
```

Set `ATTENDR_DB=data/attendr.db` in `.env`. Remove obsolete `NOTIFICATION_STATE_FILE`, `NOTIFICATION_STATE_DB`, and `LECTURE_QUIZ_STATE_FILE` configuration. The legacy notification file variable remains an import source; it no longer selects JSON production storage. Direct constructor callers may still select the legacy JSON adapter explicitly.

Migration preserves legacy files, validates their format, and imports each notification source transactionally once. Existing lecture-session completion markers are imported into `quiz_history`. Invalid data fails migration instead of resetting deduplication. Material and announcement extraction indexes are read as legacy inputs and moved into the database when updated. Existing Calendar events are adopted by their managed UID when encountered; migration does not create duplicate events or emit adoption notices. Do not run the old implementation against the same accounts after upgrading: its JSON state no longer receives updates.

The migration does not infer historical daily quizzes from changing generated-message fingerprints. An already sent daily quiz from the cutover date may need to be skipped manually for that date. Old announcement cache entries lack source metadata: persistent corrections are retained after those announcements are next observed by this version.

## Hosted scheduled runner

Both scheduled workflows use `ubuntu-latest` and share one concurrency group. `scripts/cloud_run.py` acquires an exclusive D1 lease, restores the encrypted SQLite checkpoint, runs the pipeline, and releases the lease. Every committed state change checkpoints before its caller continues. A terminated job leaves a 20-minute lease; a later run safely resumes after expiry.

Configure the integration secrets with `scripts/configure_github_secrets.py`.
`STUDY_SYNC_SECRET` remains the Worker/API bearer credential.
`ATTENDR_STATE_KEY` is a separate secret used only to derive the AES-256-GCM
checkpoint key. The hosted workflows require both and never substitute one for the
other. `ATTENDR_STATE_SECRET` is the runtime name used for state-endpoint bearer
authentication and may continue to receive `STUDY_SYNC_SECRET`; it is not an
encryption key. Apply Worker migration `0004_cloud_state.sql` before dispatching
either workflow.

### One-time checkpoint key separation

Existing hosted checkpoints created before this change were encrypted with the value
of `STUDY_SYNC_SECRET`. Do not update the workflow secret before rotating that
checkpoint.

1. Disable both scheduled workflows and wait for any active run and its state lease to
   finish.
2. Generate a new independent key locally and save it in a password manager:

   ```bash
   python -c 'import secrets; print(secrets.token_urlsafe(48))'
   ```

3. In the private, mode-`0600` `.env`, set `ATTENDR_STATE_KEY` to that new value. Add
   `ATTENDR_STATE_OLD_KEY` temporarily with the current `STUDY_SYNC_SECRET` value.
   Keep `STUDY_SYNC_SECRET` unchanged.
4. Choose a private backup path outside the repository and rotate under the Worker's
   lease:

   ```bash
   .venv/bin/python scripts/rotate_state_key.py \
     --backup /path/to/private/attendr-state-before-key-rotation.json
   ```

   The command first decrypts and SQLite-integrity-checks the old checkpoint. It saves
   the untouched encrypted remote payload with exclusive file creation, encrypts the
   exact SQLite bytes with the new key, locally decrypts that candidate, and only then
   performs the revision-checked upload. Finally it downloads with the new key, repeats
   SQLite integrity verification, and compares the database bytes. It never prints key
   values. An interruption before upload leaves the old remote checkpoint in place; an
   interruption after the atomic upload is safe because the new ciphertext was already
   verified. Re-running the command recognizes an already-rotated checkpoint.
5. Set the dedicated GitHub Actions secret without displaying it in logs:

   ```bash
   gh secret set ATTENDR_STATE_KEY --repo OWNER/REPOSITORY
   ```

   Paste the new key when prompted. Alternatively, after `.env` contains all cloud
   values, run `scripts/configure_github_secrets.py --repo OWNER/REPOSITORY`.
6. Remove `ATTENDR_STATE_OLD_KEY` from `.env`, re-enable the workflows, and manually
   run each once. Retain the private encrypted backup and old key until both runs pass;
   never commit the backup.

If rotation fails, leave the workflows disabled. The remote checkpoint remains on its
last atomic revision, and the private old-key backup is never overwritten. Do not
initialize a new checkpoint to work around a decryption failure.

The OAuth refresh token remains in the encrypted `GOOGLE_TOKEN_B64` repository secret. Access-token refresh does not require a browser. A revoked refresh token fails the run and requires explicit local reauthorization with `scripts/setup_google.py`, a noninteractive `scripts/setup_google.py --check`, and replacement of the GitHub secret. Generate the replacement value locally with `base64 < token.json | tr -d '\n'`; never paste it into chat or logs. Repeated expiry can be caused by an OAuth consent screen left in Testing status. Inspect Google Cloud Console → Google Auth Platform / OAuth consent screen → Publishing status and evaluate the appropriate production status; publication does not bypass any applicable Google verification requirements.

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
npx wrangler d1 execute attendr-study --remote --file=migrations/0003_study_preferences.sql
npx wrangler d1 execute attendr-study --remote --file=migrations/0004_cloud_state.sql
npx wrangler d1 execute attendr-study --remote --file=migrations/0005_ask.sql
npx wrangler d1 execute attendr-study --remote --file=migrations/0006_automation_watchdog.sql
npm run deploy
```

These are deployment instructions, not commands executed as part of the local implementation. Apply only migrations not already applied, in filename order. The migrations add resumable operations and canonicalize existing timestamps to UTC milliseconds. Invalid legacy timestamps fail rather than silently becoming valid dates. New installations can use `schema.sql`.

Signed interactions now have a five-minute freshness bound, persistent interaction IDs, and one active operation globally. Selected reschedule destinations are saved before Google PATCH; interrupted operations replay the same destination. Google deletes tolerate already-deleted events. Successful D1 mutations are recorded before editing the Discord response, preventing a failed response update from repeating the action. Recovery runs from the five-minute Worker schedule after an expired ten-minute operation lease. Pending operations block new Python study reconciliation when observed.

Reminders acquire the shared mutation lease before atomically claiming `notified=-1`, so Python planning and button operations cannot cancel or reschedule a session while its Discord delivery is in flight. Confirmed delivery and confirmed Discord rate limiting release the lease immediately. A crash or uncertain response leaves `notified=-1` to prevent duplicate delivery, while the reminder's 60-second lease expires automatically so planning cannot deadlock. Inspect the Discord channel and the D1 row before setting an unresolved reminder to `1` (confirmed sent) or `0` (confirmed absent). Python and Worker FreeBusy requests paginate all calendars, batch groups of 50, and reject incomplete coverage.

A shared D1 planner lease excludes button operations while Python reads availability and updates Calendar. Button operations are serialized globally because different tasks share calendar openings. Claims capture the current session atomically. The planner stops starting Calendar mutations after 15 minutes; its server lease lasts 20 minutes, with Calendar HTTP calls bounded to 60 seconds. A crashed planner can temporarily block study controls until lease expiry. Pending Worker sync payloads are cached locally and retried under the next acquired lease. The Worker availability windows currently use America/Toronto; retain that timezone for online study controls.

## Verification

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -B -m pytest
(cd worker && npm ci && npm test && npm run check)
```

Pytest blocks network sockets by default. Tests use temporary databases and mocked services, including concurrent claims, uncertain delivery, partial quiz delivery, Calendar write/commit failures, late submission reconciliation, cached quiz reuse, FreeBusy pagination/errors, signed-request freshness, and Google/D1 partial recovery. Worker tests use Node 22.13+ and its in-memory SQLite implementation. `scripts/test_*.py` remain live smoke tests and are excluded from pytest discovery.
