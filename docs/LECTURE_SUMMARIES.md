# Lecture Summaries

## Setup

1. Create or harden the private channel using the existing bot credentials:

   ```bash
   .venv/bin/python scripts/setup_lecture_summaries.py --guild-id YOUR_DISCORD_SERVER_ID
   ```

   The command reuses one existing `#lecture-summaries` or creates it, denies everyone else visibility, grants the configured owner and bot **View Channel**, **Send Messages**, **Embed Links**, and **Read Message History**, saves the channel ID locally, and sends no messages. It fails safely if duplicate channels exist.

2. If doing this manually instead, create `#lecture-summaries`, apply those same permissions, enable Discord Developer Mode, copy the channel ID, and save it locally.
3. Confirm the ignored `.env` now contains `DISCORD_LECTURE_SUMMARIES_CHANNEL_ID`. The setup command writes it automatically; a manual setup must add it. Keep the existing `DISCORD_BOT_TOKEN`; summaries require bot delivery to the dedicated channel and do not fall back to the general webhook.
4. Add the channel ID to GitHub Actions without printing it:

   ```bash
   gh secret set DISCORD_LECTURE_SUMMARIES_CHANNEL_ID --repo OWNER/REPOSITORY
   ```

   Alternatively, after updating `.env`, run the existing uploader:

   ```bash
   .venv/bin/python scripts/configure_github_secrets.py --repo OWNER/REPOSITORY
   ```

5. Optionally set `GEMINI_LECTURE_SUMMARY_MODEL`. A blank value inherits `GEMINI_MODEL`. If configured for hosted runs, add the same-named GitHub Actions secret.
6. Run `python main.py --lecture-summaries` locally or manually dispatch **Post-Class Lecture Review** in GitHub Actions. Verify the result in `#lecture-summaries`, not `#lecture-quizzes`.

No new Discord application, webhook, Worker route, Cloudflare secret, or Google OAuth scope is required.

The selected lecture text is sent to the configured Gemini API, and the validated summary plus non-secret Canvas source identifiers are sent to the configured Discord channel. Raw files, signed download URLs, Canvas tokens, and Gemini credentials are never included in the prompt or Discord payload. Confirm that this use is permitted by the course/institution before enabling the feature.

## Grounding and provenance

The existing lecture-material matcher selects the session's Canvas Module files and Pages. A single `LectureContentBundle` deduplicates identical content, extracts each selected source once, assigns stable `S1`, `S2`, … identifiers, and preserves Canvas IDs, titles, content/text hashes, extraction method, and page/slide ranges where available.

Gemini receives bounded text with explicit source boundaries. The structured result requires citations on every summary point. Attendr rejects unknown source IDs, non-verbatim source quotes, and explicit date or Big-O claims that do not occur in the cited source text. Discord's source section is rendered from trusted bundle metadata, never from model-written provenance. Summaries say they are based on course materials and are not lecture transcripts. Image-only slides, diagrams without extracted text, video/audio, and spoken explanations are unavailable unless the Canvas material also contains readable text.

## Timing, cache, and retries

- A summary is eligible only after a configured lecture has ended. Labs and tutorials are excluded by the existing schedule logic.
- Hosted runs occur 15 and 45 minutes after each lecture ends. Missing slides can retry only while the session remains inside `LECTURE_REVIEW_MAX_AGE_MINUTES` (60 minutes by default); stale quizzes and summaries are not posted later as catch-up messages.
- The temporary Sep 20–Oct 15, 2026 historical backfill runs once daily at 9:30 AM Toronto time and permits at most one uncached Gemini summary per run. Cached or pending Discord parts do not consume that limit. It uses durable deduplication and never bypasses grounding checks.
- All Discord routes observe `DISCORD_QUIET_HOURS_START=0` through `DISCORD_QUIET_HOURS_END=9` in `APP_TIMEZONE`. The guard runs before durable enqueueing or network delivery and also blocks `--force`.
- The generation cache key includes the source-bundle content hash, prompt version, and selected model.
- The validated summary, rendered Discord payload, source provenance, model, and prompt version are committed to SQLite before delivery.
- A Discord delivery failure reuses that record and makes no new Gemini request.
- Confirmed delivered sessions are not regenerated automatically. This avoids silently replacing a study artifact when Canvas files change later. `--force` is the explicit operator override and does not consume normal deduplication state.
- Discord payloads split only at complete summary points/sections; mentions are disabled. Durable outbox parts use the existing retry and uncertain-delivery safeguards.

## Troubleshooting

- **Missing channel configuration:** set both `DISCORD_BOT_TOKEN` and `DISCORD_LECTURE_SUMMARIES_CHANNEL_ID`. The pipeline fails configuration validation instead of cross-posting to another channel.
- **Waiting for slides:** confirm the lecture is in the private timetable, has ended, and its teaching Module is published. Add a `lecture_materials.sessions` override only when normal date/module matching is genuinely ambiguous.
- **No readable text:** export legacy `.ppt`, scans, or image-only decks to a supported text-bearing PDF/PPTX. Attendr does not perform OCR or infer diagram content.
- **Gemini unavailable or quota-limited:** the summary step is degraded and remains retryable; existing quiz delivery and other pipeline steps continue independently.
- **Discord delivery failed:** inspect the durable outbox with `scripts/state_admin.py list`. Do not blindly retry an `uncertain` message until checking the channel for a possible successful delivery.
- **Unexpected summary content:** compare every statement's `[S#]` marker to the deterministic Sources section. Do not treat the summary as evidence of what was said aloud.
