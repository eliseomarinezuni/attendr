# Security Policy

## Supported version

Security fixes target the current `main` branch. Attendr is a self-hosted personal
assistant, so deployers are responsible for updating their own checkout and Worker.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/eliseomarinezuni/attendr/security/advisories/new)
to report a security issue privately. Do not open a public issue containing an active
credential, OAuth file, signed URL, academic record, or other personal data.

If a credential is accidentally disclosed, revoke or rotate it with the issuing
service before sharing a redacted report.

## Security boundaries

Attendr processes sensitive Canvas, Calendar, Discord, and course data. Credentials
belong only in ignored local files, GitHub Actions secrets, or Cloudflare Worker
secrets. Public pull requests run validation without owner integration secrets.
Production workflows are restricted to this repository's `main` branch.

The public Worker URL is not an authorization boundary. Private API routes require
the Worker bearer secret, Discord ingress requires Ed25519 signatures, owner actions
require the configured Discord identity, and cloud checkpoints remain independently
encrypted with `ATTENDR_STATE_KEY`.
