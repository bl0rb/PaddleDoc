# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.1] - 2026-08-13

### Added
- VL benchmark: admins manage multiple vision-language connections (OpenAI-compatible
  endpoint, model, write-only encrypted API key, per-connection system prompt, test button)
  in a new admin tab; users benchmark a document against up to 6 VL connections plus
  optionally one OCR profile (2-7 variants) on the new Benchmark page and get a comparison
  report — duration, pages, output size, quality grade, fallback/error per variant — with
  per-variant markdown preview and JSON export. Benchmark variants are processed as regular
  jobs but stay out of the jobs list, stats, folders, and document versioning (#60)
- Document versioning via content hash: every upload stores a SHA-256 of the file; uploading
  a same-named file visible to the same team creates version N+1 linked to its predecessor,
  byte-identical re-uploads are rejected with a pointer to the existing version instead of
  being processed twice. Job detail shows the version history (who uploaded which version
  when), the job list marks re-versioned documents with a version badge (#60)
- JSON result export: `GET /api/v1/jobs/{id}/export.json` (same per-job password gate as the
  markdown download) bundles document metadata (filename, hash, version, tags), uploader and
  team, processing details (profile, engine, pages, quality gate) and the full markdown;
  "Download JSON" button on the job detail page (#60)
- Personal API tokens: users create bearer tokens (shown exactly once, stored hashed,
  optional expiry) on the new Settings page and use them via `Authorization: Bearer` for
  programmatic access to the full API — no session cookie required (#60)

- Worker logs in the admin console: Celery workers mirror their log output into a new
  `worker_log_entries` Postgres table (portable — no docker.sock; identical under Compose
  and Helm/EKS), exposed via the admin-only endpoint `GET /api/v1/auth/admin/worker-logs`
  with level/worker/text/time filters and pagination, and a terminal-style "Logs" tab at
  Admin → Logs with auto-refresh, expandable tracebacks, and "load older" paging (#57)
- New worker tunables `WORKER_LOG_CAPTURE_LEVEL` (default `INFO`, genuinely independent of
  the worker `--loglevel` — console verbosity is unaffected) and
  `WORKER_LOG_RETENTION_MAX_ROWS` (default 20000), in compose env and Helm values (#57)

### Changed
- Result markdown frontmatter is now proper YAML (`yaml.safe_dump`) and carries
  `job_id`, `document_version`, `content_sha256`, `previous_job_id`, `uploaded_by`, `team`,
  `tags`, `processed_at` and `engine` in addition to the previous keys; plain-text fallback
  results now get a frontmatter header too (previously none) (#60)
- The sidebar is now a permanently visible rail on desktop viewports (≥1024px); small
  screens keep the burger-toggled drawer (#57)
- Snappier navigation: a dependency-free stale-while-revalidate cache renders Home, Tasks,
  and Processing instantly from cached data and refreshes in the background; polling is
  adaptive (fast only while jobs are pending/running) and pauses while the tab is hidden;
  the job detail page lazy-loads the markdown renderer and shows loading skeletons instead
  of blank states (#57)
- The dashboard degrades honestly when the backend becomes unreachable: service status,
  stats, and job state are marked as "last known" instead of continuing to display the
  pre-outage values as current (#57)

### Fixed
- The delete-confirmation modal could render beneath the (now permanent) sidebar on desktop (#57)
- Filter "Reset" buttons (document list, admin logs) fetched with the pre-reset filter
  values due to a stale closure, leaving the list filtered while the inputs showed cleared (#57)

## [1.2.0] - 2026-08-05

### Added
- Confluence → Markdown import: managed sources, chunked crawler runs with live progress,
  import wizard, rendered markdown view, and optional attachment OCR (#53, #54)
- OCR fallback visibility: when a job silently degrades to plain-text extraction (no OCR
  ran — e.g. the worker cannot download model weights), the job detail page now shows a
  warning banner with the real failure reason and the document list marks the job instead
  of displaying a profile that never ran (#55)
- Helm: PVC-backed worker model cache (`worker.modelCache.persistence.*`) so model weights
  survive pod restarts, `worker.extraEnv` for proxy/model-source configuration behind
  restricted egress, and `worker.updateStrategy` (#55)
- Firewall connection matrix per component in `docs/firewall-requirements.md` (#55)

### Changed
- Dashboard links consolidated into a single "Tasks" entry; the tasks page gains a
  "New Task" button that opens the upload flow (#55)
- Worker Deployment defaults to `strategy: Recreate` when the model-cache PVC has no
  ReadWriteMany access mode, so image upgrades cannot deadlock on the volume attachment (#55)

### Fixed
- `ppocrv6_small` profiles referenced model names that exist in no PaddleOCR release and
  always failed into the plain-text fallback; corrected to `PP-OCRv6_small_det`/`_rec` (#55)
- Docker Compose: the worker now waits for the backend healthcheck (i.e. completed Alembic
  migrations) before starting — fixes the `column jobs.owner_id does not exist` race on
  stack boot after the v1.1.0 upgrade (#55)

## [1.1.0] - 2026-08-04

### Added
- User accounts with authentication: every page and API endpoint now requires a login
- First-run `/setup` flow that bootstraps the initial admin account
- Local username/password login (rate-limited, no account enumeration)
- Runtime-configurable OIDC SSO (Keycloak, Microsoft Entra ID): providers are managed,
  tested, and enabled in the admin UI without redeploying; client secrets stored encrypted
- Admin console at `/admin`: users, teams, and identity providers
- Per-user data visibility; teams share access to all team jobs; pre-existing jobs stay
  admin-only until assigned via "Assign ownerless jobs"
- DB-backed httpOnly sessions (7-day sliding expiry, 30-day cap)

### Changed
- `SECRET_KEY` is now required (compose env var; Helm `auth.secretKey.*` — the chart fails
  to render without it). Set it once and never rotate it casually: rotation invalidates all
  sessions and makes stored OIDC client secrets unreadable
- Redis now requires a password (`REDIS_PASSWORD` / Helm `redis.auth.*`)
- `CORS_ORIGINS` must list concrete frontend origins (credentialed cookies; no wildcard)
- `PUBLIC_API_URL` must point at the externally reachable backend URL for OIDC redirects

### Security
- SSRF-safe OIDC discovery/token fetching, CSRF origin guard, trusted-proxy handling for
  `X-Forwarded-*`, Celery task time limits

## [0.1.0] - 2026-06-17

### Added
- Initial release of PaddleDoc
- Document processing with PaddleOCR support
- Web UI for uploading and managing documents
- FastAPI backend with Celery workers for async processing
- Support for PDFs, Office files, and images
- Structured Markdown output generation
- Folder organization and tagging system
- Password protection for sensitive documents
- Job tracking and status monitoring
- Docker and Docker Compose support
- Helm charts for Kubernetes deployment
- Database migrations with Alembic
- Comprehensive API endpoints
- Frontend dashboard with Next.js
