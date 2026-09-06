# P2 features

Configure `data/preferences.json`, or set `ATTENDR_PREFERENCES_FILE` to a private JSON file. Invalid or explicitly missing configuration fails validation before the pipeline runs. The shipped defaults preserve the existing study windows, mute nothing, and enable up to three review questions per local day.

## Study preferences

`study_windows` maps weekdays **Monday=0 through Sunday=6** to ordered, non-overlapping pairs of minutes after midnight. For example, `[600, 720]` means 10 AM–noon. Supply all seven weekdays; `[]` excludes a day. Values range from 0 to 1439. Windows cannot cross midnight; split them across adjacent days.

`study_minutes` overrides session duration by task kind (`assignment`, `quiz`, `exam`, `project`, `presentation`), from 15 to 180 minutes. Omitted kinds retain their existing durations and session counts.

`course_priorities` maps Canvas course IDs to weights 1–5. Deadlines take precedence; the higher-weight course gets first choice of available time when deadlines tie. Attendr still respects calendar conflicts, one study session per day, submitted/completed tasks, and manual overrides. It reports tasks that cannot fit instead of extending the available windows.

```json
{
  "study_windows": {
    "0": [[600, 720]], "1": [[1080, 1200]], "2": [[600, 720]],
    "3": [], "4": [[1080, 1200]], "5": [[660, 900]], "6": [[660, 900]]
  },
  "study_minutes": {"exam": 75, "assignment": 30},
  "course_priorities": {"12345": 5},
  "muted_topics": ["weekly newsletter"],
  "review_enabled": true,
  "review_daily_limit": 3
}
```

The Python planner sends its availability profile to D1 under the existing planner lease, in the same transaction as session synchronization. Worker rescheduling uses that stored profile. Online study controls currently require `APP_TIMEZONE=America/Toronto`. Local planning can use other IANA timezones.

## Announcement triage

Announcements receive an explanatory **Action required**, **Coursework**, or **Information** field. Deadline, exam, and schedule-change keywords are highlighted. Explicit `muted_topics` phrases match case-insensitively against titles and body text; recognized deadline/coursework notices bypass those mute rules.

This is deterministic, rule-based triage, not an AI judgment or a claim that every urgent notice can be detected. No messages are muted by default. Classification does not change Canvas read state or interfere with announcement date extraction. Updating a presentation label does not resend an otherwise unchanged announcement. Decisions are stored in `announcement_triage` for inspection. `--force` bypasses muting for an explicit test send.

## Spaced repetition

Successfully delivered daily and lecture quizzes become review cards, one per question. Existing completed quiz payloads are imported once; migration-only legacy completion records without questions produce no cards. The first review is due on the local calendar day after the original quiz was sent.

The normal scheduled pipeline sends due cards to the existing lecture-quiz destination, with answers still hidden behind spoilers. Each local day has a fixed selection of at most `review_daily_limit` cards (1–10). Repeated runs do not send that selection again. Sending a card never changes its learning interval: ungraded cards remain due and can be selected on a later day.

Send reviews without initializing Canvas, Google, or Gemini:

```bash
.venv/bin/python main.py --review-only
```

Grade on the machine that owns the same persistent SQLite database:

```bash
.venv/bin/python scripts/review.py list
.venv/bin/python scripts/review.py grade CARD_ID good --revision 1
```

Each review message includes its card ID and revision in a copyable command. Use the Python interpreter from your virtual environment. On a persistent runner, set `ATTENDR_HOME` or provide `--db /path/to/attendr.db` before the subcommand. This release uses explicit local grading; Discord rating buttons are not part of P2.

- `again`: next review in one day.
- `good`: three days initially, then doubles the previous interval.
- `easy`: seven days initially, then triples the previous interval.

Intervals cap at 180 days. Revision checks reject duplicate grades and early grading. Ratings are retained in `review_history`; an `again` grade brings a weak question back sooner. This is a transparent interval policy, not a calibrated prediction of retention. Grading cancels pending unsent reminders for the old revision. Uncertain deliveries still require the P1 outbox reconciliation procedure.

Set `review_enabled` to `false` to stop selecting new reviews. Previously queued messages remain subject to the existing outbox delivery policy. `--force` does not consume a normal daily selection or advance a card's review interval.

## Upgrade

Back up the existing SQLite database before running the new code. Opening it upgrades the local schema from version 1 to 2, preserving P1 tables and adding review and triage tables. The P1 application cannot open a version 2 database; a rollback needs the pre-upgrade backup and reconciliation of subsequent external deliveries.

If using the Worker, apply the additional migration before deploying the updated Worker, then resume Python scheduling:

```bash
cd worker
npx wrangler d1 execute attendr-study --remote --file=migrations/0003_study_preferences.sql
npm run deploy
```

New installations use `schema.sql`. Installations that have not completed P1 must also apply its `0002_reliability.sql` migration first. The persistent runner requirement from P1 is unchanged. No deployment or live account mutation is performed by implementing these repository changes.

## Verification

```bash
.venv/bin/python -B -m pytest
(cd worker && npm test && npm run check)
```

Tests cover preference validation, contested-slot priorities, custom durations/windows, Worker profile consistency, triage mute safeguards, legacy deduplication, local-date review scheduling, explicit grading, stale revisions, daily caps, forced sends, pending-reminder cancellation, and schema upgrades.
