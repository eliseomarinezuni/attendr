# Attendr

Read-only Canvas ingestion, automatic syllabus/announcement date extraction, Google Calendar sync, Discord alerts, and post-lecture Gemini quizzes from Canvas PDFs, PowerPoints, and Pages. Python 3.11.

## Run

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
cp .env.example .env
# Fill in .env and download Desktop OAuth credentials first.
.venv/bin/python scripts/setup_google.py
.venv/bin/python main.py
```

Useful modes:

```bash
.venv/bin/python main.py --sync-only
.venv/bin/python main.py --sync-only --no-materials
.venv/bin/python main.py --announcements-only
.venv/bin/python main.py --digest-only
.venv/bin/python main.py --quiz-only --topic "Binary search trees"
.venv/bin/python main.py --daily-quiz
.venv/bin/python main.py --lecture-quizzes
.venv/bin/python main.py --study-plan-only
.venv/bin/python main.py --quiz-only --quiz-pdf data/materials/lecture.pdf
```

The default run sends unseen announcements, extracts dated items from syllabuses and all recent announcements, syncs deadlines plus Example University academic dates and every lecture/lab/tutorial in the verified timetable, sends a configurable digest (72 hours by default), and retries any post-lecture quiz waiting for slides. Canvas assignments override announcements; newest announcements override syllabuses. A date without a stated time remains an all-day event on its stated date. Academic date ranges use an exclusive next-day end. The exception is a same-course midterm or exam on an unambiguous lecture date: when the source omits the time, Attendr uses that lecture slot. Explicit exam times are preserved when replacing a lecture occurrence.

`--lecture-quizzes` scans every published Canvas Module for the lecture that just ended. It reads PDF, PowerPoint (`.pptx`), Canvas Page content, and readable Google Slides links regardless of filenames; labs/tutorials never produce quizzes. It matches explicit date/lecture labels first, then stable Canvas IDs and ordered topic modules. Multiple items in one module are combined. Each quiz contains two conceptual multiple-choice questions and one short-answer active-recall question. The verified Fall 2026 timetable and no-class dates are in `data/course_schedule.json`.

Large lecture files up to `LECTURE_MAX_FILE_MB=768` are streamed to temporary disk with a five-minute download budget and bounded retries. PowerPoint extraction reads slide/table/notes XML without loading embedded video or images. Extracted large-file text is cached by Canvas file ID, update time, and size in the encrypted SQLite checkpoint, so fresh GitHub runners reuse it. Raw large files are deleted after extraction. A changed Canvas revision triggers re-extraction.

Lecture quizzes retry unsent sessions for `LECTURE_QUIZ_RETRY_HOURS=336` (14 days), including slides posted late. A failed source does not stop other lecture files. Files above the configured cap get an explicit search exclusion note. Unpublished/inaccessible slides and image-only or video-only content cannot yield a text-grounded quiz; missing or structurally ambiguous matches are reported rather than guessed. Private Google Slides require a one-time `python scripts/setup_google.py --lecture-slides` authorization with an account that can read the course decks, the Google Slides API enabled in the OAuth project, and a `GOOGLE_SLIDES_TOKEN_B64` repository secret containing the separate `google_slides_token.json` token (the Calendar account stays unchanged). Scheduled runs never request interactive login. Google Slides exports refresh every six hours; their access controls remain enforced. Per-course `lecture_materials` rules in `data/course_schedule.json` can exclude non-teaching modules or map a date/session explicitly with `module`, `module_id`, `item_ids`, or `source_ids`; explicit mappings override learned stable IDs.

The older manual `--daily-quiz` mode uses `DAILY_QUIZ_TOPIC`, `--topic`, `--quiz-pdf`, or a date entry in `data/daily_topics.json`:

```json
{
  "2026-09-08": "Binary search trees",
  "2026-09-09": "AVL tree rotations"
}
```

Production state lives in `ATTENDR_DB` (`data/attendr.db`): durable notification delivery, Calendar mappings, quiz history, and extraction caches. Calendar events have deterministic IDs and are checked for actual field changes. Read [P1 migration, recovery, and deployment instructions](docs/P1_RELIABILITY.md) before upgrading an existing scheduled installation.

Discord is separated by purpose: Canvas announcements, deadline alerts, and digests go to `#announcements`; created/changed Calendar items go to `#calendar-updates`; post-lecture quizzes go to `#lecture-quizzes`; interactive reminders remain in `#study-sessions`.

Attendr also creates a separate `Attendr Study Plan` calendar. It places a small number of conflict-free study blocks before each assignment, quiz, project, presentation, midterm, or exam. Regular work uses 30–45 minute sessions; exams use 60 minutes. Thursday is excluded, Wednesday/Friday 6–8 PM is blocked, and existing readable Google calendars are respected. One study block is scheduled per day.

The free Cloudflare Worker under `worker/` stays online when the computer is off. Every five minutes it checks D1 for sessions that are starting and posts buttons in `#study-sessions`: **Session complete**, **Reschedule**, and **Task complete**. Completion removes the corresponding Calendar block; task completion removes all remaining blocks for that task. The Python planner and Worker share only opaque IDs and session metadata through an authenticated endpoint.

Attendr first reads the Canvas Syllabus tab, then searches Files, Modules, module-linked files/Pages, and standalone syllabus/course-outline Pages. Explicit syllabus links are fetched directly even when Canvas hides the source from the general Files area. PDF and Word (`.docx`) syllabuses are supported, including Word tables. Not finding a syllabus is non-fatal. Downloads are stored under `data/materials/course-<id>/` and excluded from Git. The SQLite extraction cache stores content hashes, context fingerprints, and validated extracted deadlines so unchanged documents do not consume Gemini quota again. Existing JSON indexes are accepted as migration inputs. Live Canvas assignments win over matching syllabus findings. Conflicting extracted dates are skipped and reported rather than guessed. Date changes for the same course/deadline title update the existing Google event. Attendr can delete replaced exam entries and obsolete managed study blocks. Failed or incomplete Canvas/material/announcement input blocks calendar reconciliation; unread announcements can still be delivered. An unavailable or malformed Worker state blocks study-plan writes. These guards preserve existing events until a complete run succeeds.

`--no-materials` skips timetable replacement and study-plan reconciliation because it omits a required source. It can still sync available Canvas and announcement dates. Undated Canvas assignments are reported and omitted from scheduling without marking the whole fetch incomplete.

Canvas API requests and file downloads use 5-second connect and 15-second read timeouts. Read requests retry up to three total attempts for timeouts, connection failures, HTTP 429, and HTTP 500/502/503/504. Retry-After waits longer than 15 seconds are deferred to a later run.

## Personalized planning and review

[P2 features](docs/P2_FEATURES.md) add configurable study windows, session durations and course priorities; explainable announcement triage with explicit mute rules; and spaced quiz reviews with `again` / `good` / `easy` grades. Edit `data/preferences.json`. Reviews run with the normal pipeline or `main.py --review-only`; grade locally with `scripts/review.py` against the same database.

P2 upgrades the local database to schema version 2 and adds a Worker preferences migration. Follow the [P2 upgrade instructions](docs/P2_FEATURES.md#upgrade) before rollout.

## Local automation

### macOS or Linux cron

Create the log directory, then edit your crontab:

```bash
mkdir -p /path/to/attendr/logs
crontab -e
```

Run every two hours from 8:00 AM through 10:00 PM:

```cron
0 8-22/2 * * * cd /path/to/attendr && /path/to/attendr/.venv/bin/python main.py >> /path/to/attendr/logs/attendr.log 2>&1
```

Check it with `crontab -l`. The Mac must be awake and online.

### Windows Task Scheduler

1. Open **Task Scheduler → Create Task** and name it `Attendr`.
2. Select **Run whether user is logged on or not**.
3. Add a daily trigger beginning at 8:00 AM; repeat every 2 hours for 14 hours.
4. Add an action **Start a program**.
5. Program: `C:\path\to\attendr\.venv\Scripts\pythonw.exe`
6. Arguments: `main.py`
7. Start in: `C:\path\to\attendr`
8. Enable **Run task as soon as possible after a scheduled start is missed**, save, then choose **Run** once to test.

## GitHub Actions automation

Scheduled sync and lecture-quiz workflows run on GitHub-hosted Linux runners, so the Mac may remain off. A Cloudflare D1 lease serializes runs and stores an AES-256-GCM-encrypted SQLite checkpoint after each committed mutation. `STUDY_SYNC_SECRET` authenticates Worker requests; an independent `ATTENDR_STATE_KEY` encrypts the checkpoint. D1 never receives plaintext application state. Existing installations must follow the one-time key-separation procedure in [P1 reliability and rollout](docs/P1_RELIABILITY.md#one-time-checkpoint-key-separation) before changing the GitHub secret.

The main cron runs at minute 17 to avoid top-of-hour GitHub congestion. Each hosted run records an authenticated heartbeat in D1. The Worker's five-minute cron dispatches a recovery run when the academic heartbeat is more than 150 minutes old during the 8:00 AM–10:00 PM Toronto window. Configure a fine-grained GitHub token with Actions write access as the Worker secret `GITHUB_ACTIONS_TOKEN`; repository, workflow, and ref are non-secret Wrangler variables.

The workflows reconstruct `.env`, Google OAuth files, and the database from repository secrets. Required secrets are documented by `scripts/configure_github_secrets.py`; deployment also requires `STUDY_WORKER_URL`, `STUDY_SYNC_SECRET`, and an independent `ATTENDR_STATE_KEY`. Missing or revoked credentials fail promptly, and scheduled runs never launch interactive OAuth.

## Tests

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -B -m pytest
(cd worker && npm test && npm run check)
```

The automated tests use fakes and temporary state. Files under `scripts/test_*.py` are live smoke tests and can write to Google Calendar or Discord.

### Calendar destination and duplicate recovery

Set `GOOGLE_CALENDAR_ID` to the same dedicated Attendr calendar in local `.env` and GitHub Actions secrets. The explicit ID takes precedence over the display name. This prevents local and hosted runs from writing separate copies to different calendars.

For an older installation that also wrote to your primary calendar, preview confirmed cross-calendar duplicates with:

```bash
.venv/bin/python scripts/repair_calendar_duplicates.py --source-calendar primary
```

Add `--apply` to remove only Attendr-tagged source events whose stable UID already exists in the configured destination. Each cleanup saves a private JSON backup under `data/` and rechecks the destination before deletion. Unmatched events and personal events are preserved.

Cloud checkpoints are compressed before authenticated encryption. Existing uncompressed checkpoints remain readable. After a failed upload, Attendr checks the remote revision and checksum before retrying, including recovery from a successful write whose response was lost. Public calendars that reject free/busy queries are read through paginated event listings; unavailable calendars still block planning safely.

## Course assistant: `/ask` in `#ask`

The existing Cloudflare Worker answers questions even while your Mac is off. In the
separate `#ask` channel, select `/ask` and enter one sentence in its **question** field:

- `for my web development course whens my midterm`
- `EXMP 3030, explain HTTP requests`
- `what is due tomorrow in algorithms`
- `when is my web dev lecture next week`

There is no structured course option. Names, keys, codes, `match` entries, and optional
`aliases` from `data/course_schedule.json` identify the course. Unknown or multiple
matches produce a clarification. The existing owner restriction also protects `/ask`;
answers are ephemeral, visible only to the requesting owner. Using `/ask` outside
`#ask` gives a redirect message. Ordinary channel messages are not processed.

### Architecture and grounding

The scheduled GitHub Actions run enables `ATTENDR_ASK_SYNC=true` and uses the same
`cloud_run.py` encrypted-state lease and checkpoint flow. Canvas enumeration indexes
published pages, syllabus HTML, PDF/PPTX files, assignments (including undated ones),
announcements (including read announcements), module metadata, and the verified
course timetable. Extraction hashes are cached in the existing encrypted SQLite
checkpoint; keys and private files are never needed on the Mac at question time.

Normalized records are stored separately as searchable text in D1. Uploads use the
existing bearer-authenticated Worker bridge:

1. `POST /api/knowledge/stage` uploads bounded batches for one course/revision.
2. `POST /api/knowledge/publish` checks the expected record count and atomically
   publishes the snapshot, updates changed hashes, and deletes absent records.
3. Failed enumeration/extraction/uploads leave the previous complete snapshot active.
   Retries reuse the revision and stable source/part IDs. Older revisions cannot
   replace newer ones; abandoned staging data expires on subsequent uploads.

`POST /api/knowledge/sync` also accepts a single complete snapshot for small imports.
All endpoints require `Authorization: Bearer $STUDY_SYNC_SECRET`. No new public data
endpoint is exposed. D1's searchable records do not replace or change the encryption
of the existing state/checkpoint; treat the D1 database as private course data.

Retrieval filters by course in SQL **before** selecting up to six relevant chunks.
Deadline lookup is deterministic and works without Gemini. Explicit Canvas deadlines
are displayed in `America/Toronto`; other dates are quoted from their source instead
of inferred. Relative assignment dates and timetable dates use Toronto calendar days
and Monday–Sunday weeks. The timetable respects the term and no-class ranges.

For explanatory questions, `@google/genai` selects up to three exact source excerpts.
The Worker verifies each quote against its retrieved chunk and attaches its own source
title/Canvas link. This deliberately conservative format prevents generated dates,
policies, grades, or explanations unsupported by synchronized material. Canvas text
is untrusted reference data, suspicious instruction-bearing chunks are excluded, and
Gemini receives no tools or other courses. Missing evidence produces an explicit
missing-information response. Snapshots older than 48 hours show a freshness note.

Limits: 1,000 input characters, six questions/minute, six retrieved chunks, 600 model
output tokens, 12-second model timeout, and two bounded Discord message-edit attempts.
Mentions are disabled; questions, reference text, credentials, and interaction tokens
are not logged. Discord replies are deferred immediately, as required by the
[Discord interaction API](https://docs.discord.com/developers/interactions/receiving-and-responding).

### Setup and deployment

Existing installations retain the same `DB` binding in `worker/wrangler.jsonc` and the
same `/interactions` endpoint. No Vectorize binding or gateway process is required.

```bash
cd worker
npm ci
npx wrangler d1 execute attendr-study --remote --file=migrations/0005_ask.sql
npm run deploy
cd ..
.venv/bin/python scripts/setup_ask.py --guild-id YOUR_SERVER_ID --upload-worker-secrets
.venv/bin/python scripts/configure_discord_endpoint.py
```

For a **new** D1 database, apply `worker/schema.sql` instead of just migration 0005.
For local verification, replace `--remote` with `--local`.

`setup_ask.py` creates a private text channel named `ask` (owner and bot access) or
reuses one existing `#ask`, preserving its permissions. It registers/upserts only the
`ask` guild command; it does not replace other commands or post messages. The bot needs
**Manage Channels** for channel creation, and must be installed with **bot** and
**applications.commands** scopes. Keep **View Channel**, **Send Messages**, and
**Read Message History** available to the bot, and **Use Application Commands** to
the owner. Set the application's Interactions Endpoint URL to
`https://YOUR_WORKER/interactions`. Duplicate existing `#ask` channels must be resolved
before setup. Existing channel visibility is unchanged; replies remain private.

The setup script reads these values from `.env` or the environment:

| Setting | Used by |
| --- | --- |
| `DISCORD_BOT_TOKEN` | Discord setup; existing Worker behavior |
| `DISCORD_APPLICATION_ID`, `DISCORD_PUBLIC_KEY` | Existing Worker signature/application validation |
| `DISCORD_OWNER_USER_ID` | Owner authorization and private channel creation |
| `DISCORD_ASK_CHANNEL_ID` | Written by setup; upload to Worker secrets |
| `GEMINI_API_KEY` | Worker secret; also existing GitHub secret |
| `GEMINI_MODEL` | Optional Worker model override, same default as Attendr |
| `STUDY_WORKER_URL`, `STUDY_SYNC_SECRET` | GitHub/Worker authenticated synchronization bridge |
| `ATTENDR_STATE_KEY` | Independent AES-GCM checkpoint encryption key; never a Worker bearer credential |
| `CANVAS_BASE_URL`, `CANVAS_API_TOKEN` | Existing GitHub secrets for Canvas synchronization |

`--upload-worker-secrets` uploads `DISCORD_ASK_CHANNEL_ID`, `GEMINI_API_KEY`, and
`GEMINI_MODEL` through your authenticated Wrangler session. Without that flag, setup
only stores the channel ID locally; upload those three values separately with
`npx wrangler secret put NAME` from `worker/`. Existing Google, Discord study-channel,
and encrypted-state secrets stay as configured. No additional `/ask`-specific GitHub
secrets are needed; `ATTENDR_STATE_KEY` is still required for hosted checkpoint runs.
Deploy the migration/Worker **before** enabling the updated workflow.

Push the code/workflow through your normal repository process, then manually run
**Scheduled Academic Assistant** in GitHub Actions for the first cloud snapshot.
Subsequent existing scheduled runs keep it current. For a local sync using the same
cloud-state safeguards, run `ATTENDR_ASK_SYNC=true .venv/bin/python scripts/cloud_run.py`.
That command also performs the normal Attendr run and can send its normal notifications.

### Verification and troubleshooting

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pyright --pythonpath .venv/bin/python
.venv/bin/python -m pytest --cov=src/academic_assistant --cov=main --cov-report=term-missing
.venv/bin/pip-audit --strict --no-deps --disable-pip -r requirements.lock
npm --prefix worker test
npm --prefix worker run check
npm --prefix worker audit --omit=dev --audit-level=high
cd worker && npx wrangler deploy --dry-run
```

CI enforces Ruff linting, the scoped Pyright baseline, at least 75% branch coverage,
locked Python dependency auditing, production npm dependency auditing, and secret
scanning. Ruff formatting is currently report-only while existing formatting drift is
removed incrementally.

Tests disable live Python sockets and mock Discord, Canvas, D1, and Gemini. They cover
aliases, ambiguity, signatures, deferred replies, source isolation, deadlines,
untrusted content, synchronization retries/updates/removals, and existing controls.

- **No `/ask` command:** rerun setup for the correct server and confirm the
  `applications.commands` installation scope. Guild commands are registered per server.
- **Use the dedicated channel:** upload the saved `DISCORD_ASK_CHANNEL_ID` to the Worker.
- **Only owner allowed:** confirm `DISCORD_OWNER_USER_ID` matches your Discord user ID.
- **No synchronized material:** apply migration 0005 and run the updated scheduled
  workflow. Inspect its Course search status and verify existing Worker/Canvas secrets.
- **Missing or outdated answers:** inspect the linked source and last sync status.
  A failed course snapshot preserves old data rather than publishing incomplete data.
  Scanned PDFs without extractable text, unsupported legacy `.ppt` files, and files
  over 25 MB need conversion/OCR or a supported Canvas source. No OCR is performed.
- **Sync failure:** verify source access, valid timestamps, and database migration.
  Batches are capped at 300 KB, requests at 900 KB, and each course at 20,000 source
  parts. The old snapshot is preserved if a limit is exceeded.
- **AI unavailable:** verify Worker `GEMINI_API_KEY`/`GEMINI_MODEL` access and quota.
  Deterministic deadline/timetable questions still work without Gemini.

Attendr content-addresses verified deadline extractions, rechecks them when grounding
rules change, and sends only structurally relevant deadline sections to Gemini.
`GEMINI_DEADLINE_MODEL` and `GEMINI_QUIZ_MODEL` are optional task-specific overrides;
both inherit `GEMINI_MODEL` when blank. Scheduled Python requests are serialized using
`GEMINI_MIN_REQUEST_INTERVAL_SECONDS` (default `0.5`), while deterministic extraction
and verified-cache hits make no provider request and do not sleep.
- **Reply delivery failed:** Discord rejected or timed out on both message edits;
  ask again. Attendr logs only a generic failure and never persists interaction tokens.
