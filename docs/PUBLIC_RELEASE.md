# Public repository security model

Attendr is self-hosted. Public source never grants access to an owner's Canvas,
Google, Discord, Gemini, GitHub, or Cloudflare resources.

## Trust boundary

Public pull requests run only the validation workflow. That workflow has read-only
repository permission and receives no owner integration secrets. It uses normal
`pull_request`, never `pull_request_target`, and does not upload runtime artifacts.

The scheduled workflows are separate. Their jobs run only in
`eliseomarinezuni/attendr` on `main`, where GitHub supplies the owner's repository
secrets. Forks can copy those workflow files but cannot copy or read those secrets.
Manual dispatch exposes no inputs and cannot select another ref in the workflow.

All Actions are pinned to immutable commits, checkout credentials are not persisted,
and caches contain dependencies only. Runtime state, OAuth files, downloaded course
materials, databases, logs, and private schedule/preferences are ignored.

## Private runtime data

The repository provides synthetic `*.example.json` files. An owner copies and edits
them under their non-example names, which Git ignores. Scheduled runs reconstruct the
private schedule and preferences from `COURSE_SCHEDULE_B64` and
`ATTENDR_PREFERENCES_B64`; the restoration is atomic, validates JSON, and creates
owner-readable files only.

When Lecture Summaries are enabled, selected extracted course text crosses the Gemini
API boundary and the validated result crosses the Discord API boundary. Attendr does
not put signed Canvas download URLs, bearer credentials, or raw lecture files in the
summary payload. Deployers must evaluate their institution's rules before enabling it.

`STUDY_SYNC_SECRET` authenticates private Worker API calls. A different
`ATTENDR_STATE_KEY` encrypts the SQLite checkpoint with AES-GCM. The Worker's public
URL and deployment identifiers do not authorize state access. Discord interactions
also require a valid timestamped Discord signature and the configured owner/channel.

## Maintainer settings

After publication, enable GitHub private vulnerability reporting and secret scanning.
Enable GitHub's default CodeQL setup for Python and JavaScript/TypeScript; using default
setup avoids adding a workflow that may fail while the repository is still private and
GitHub Advanced Security is unavailable.

Protect `main` after any one-time history cleanup: require the validation checks before
merge, block force pushes and deletion, and optionally require pull requests. Review
Dependabot alerts and its grouped weekly pip, npm, and GitHub Actions updates.

Do not publish an active credential in an issue, pull request, test, log, artifact, or
example. Treat any credential ever committed to Git as compromised and rotate it after
removing it from reachable history.
