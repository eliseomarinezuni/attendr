# Attendr

Read-only Canvas ingestion, automatic syllabus/announcement date extraction, Google Calendar sync, Discord alerts, and post-lecture Gemini quizzes from Canvas PDFs, PowerPoints, and Pages. Python 3.11.

## Run

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
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
.venv/bin/python main.py --quiz-only --quiz-pdf data/materials/lecture.pdf
```

The default run sends unseen announcements, extracts dated items from syllabuses and all recent announcements, syncs deadlines plus Example University academic dates and every lecture/lab/tutorial in the verified timetable, sends a 48-hour digest, and retries any post-lecture quiz waiting for slides. Canvas assignments override announcements; newest announcements override syllabuses. A date without a stated time is placed at 11:59 PM the day before.

`--lecture-quizzes` scans published Canvas Modules for the lecture that just ended. It reads PDF, PowerPoint (`.pptx`), and Canvas Page content; labs/tutorials never produce quizzes. Each quiz contains two conceptual multiple-choice questions and one short-answer active-recall question. The verified Fall 2026 timetable and no-class dates are in `data/course_schedule.json`.

The older manual `--daily-quiz` mode uses `DAILY_QUIZ_TOPIC`, `--topic`, `--quiz-pdf`, or a date entry in `data/daily_topics.json`:

```json
{
  "2026-09-08": "Binary search trees",
  "2026-09-09": "AVL tree rotations"
}
```

Discord sends are recorded only after success. `data/seen_ids.json` is used in GitHub Actions. Google Calendar deduplicates independently with each Canvas UID in `extendedProperties.private` and updates an existing event when its Canvas data changes.

Downloaded syllabuses are stored under `data/materials/course-<id>/` and excluded from Git. `data/materials_index.json` stores only content hashes and validated extracted deadlines, so unchanged documents do not consume Gemini quota again. Live Canvas assignments win over matching syllabus findings. Conflicting extracted dates are skipped and reported rather than guessed. Date changes for the same course/deadline title update the existing Google event; Attendr never automatically deletes Calendar events.

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

`.github/workflows/schedule.yml` runs every two hours for deadline/announcement sync and late-slide retries. `.github/workflows/class-quizzes.yml` runs at each lecture end time; GitHub may start scheduled jobs a few minutes late. Both persist deduplication state. The repository should remain private because this state reveals academic activity.

Required repository secrets:

- `CANVAS_BASE_URL`
- `CANVAS_API_TOKEN`
- `DISCORD_WEBHOOK_URL`
- `GEMINI_API_KEY`
- `GOOGLE_CREDENTIALS_B64`
- `GOOGLE_TOKEN_B64`
- Optional: `GOOGLE_CALENDAR_NAME`, `DAILY_QUIZ_TOPIC`

After authenticating GitHub CLI, upload them directly from the local `.env` and OAuth files without displaying their values:

```bash
.venv/bin/python scripts/configure_github_secrets.py --repo OWNER/attendr
```

Manual file encoding alternatives:

```bash
base64 -i credentials.json -o credentials.base64
base64 -i token.json -o token.base64
```

Copy each encoded file's contents into the matching GitHub repository secret, then delete the encoded copies. Base64 is encoding, not encryption; only place it in GitHub Secrets. Never commit `.env`, `credentials.json`, or `token.json`.

The scheduled workflow requests only `contents: write`, which it uses to commit `data/seen_ids.json`. In **Actions**, run **Scheduled Academic Assistant** manually once and inspect its summary.

Google OAuth apps left in Testing can issue refresh tokens that expire after seven days. For reliable headless runs, move the consent screen to Production when appropriate; verification is generally unnecessary for a personal app limited to your own account.

GitHub Actions usage is subject to the included minutes/storage quota of the repository owner's plan. A lightweight private repository normally fits the free allowance, but it is not an unlimited guarantee.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```
