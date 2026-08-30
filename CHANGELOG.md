# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Outbound webhooks for automation (n8n and friends): every user can register webhook
  connections under Connections › Webhooks — URL, optional signing secret (stored encrypted,
  never displayed; deliveries then carry an `X-PaddleDoc-Signature` HMAC-SHA256 header), and
  the events the connection accepts (job finished, job failed, import run finished).
  Delivery is strictly per-task opt-in: the File Task wizard's profile step and the
  Confluence import wizard's options both grew an optional "Send result to webhook"
  selection, and only a task configured that way ever sends — a connection that merely
  subscribes to an event receives nothing. The worker delivers automatically with retries
  and a visible delivery history, finished jobs can also be sent manually from the jobs
  table or detail page, and payloads carry the job metadata plus the full markdown so a
  flow can process it without calling back. Deliveries run through the SSRF guard;
  private-network targets are allowed via `WEBHOOK_PRIVATE_HOST_ALLOWLIST` (wired through
  compose and the Helm chart, both backend and worker)
- Confluence imports are diagnosable: every failed API request is logged (path, HTTP status,
  server kind, and Confluence's own error message) with plain-language readings of the
  common cases — including Data Center's habit of answering 404 for pages you lack
  permission to see — and import runs log start/finish summaries, per-page and
  children-listing failures with context, and cap events, all visible in the admin Logs tab
  and the run's error list. Personal spaces (`~` keys) get an explicit hint

### Changed
- **Breaking (compose file names).** One standard deployment for every platform:
  `docker-compose.nas.yml` became `docker-compose.yml`, so Windows, macOS, Linux and NAS
  hosts all start with a plain `docker compose up -d`. The old name described the file's
  origin, not its audience — it was always the deployment everyone should use. The
  local-build file that held the `docker-compose.yml` name is now `docker-compose.dev.yml`
  and runs under its own compose project name, so a contributor's trust-auth Postgres can
  no longer end up sharing a volume with a real installation.
- **Breaking (data location).** The standard file keeps Postgres, Redis, document storage
  and the PaddleOCR model cache in Docker-managed volumes instead of bind-mounting
  `./nas-data/`. That directory only ever existed on Windows and macOS because a compose
  file demanded it, and it forced a `chown -R 1000:1000` on Linux hosts. Named volumes
  need neither. To keep data on a specific share or disk, the new
  `docker-compose.nas.example.yml` overlay redirects all four volumes to host paths and
  changes nothing else. Migration steps for existing installations are in the README under
  *Upgrading an existing installation*.

### Fixed
- Postgres and Redis no longer restart-loop on startup. Both official images enter their
  entrypoint as root, `chown`/`chmod` their data directory and only then drop to uid 999
  via `gosu`/`setpriv`; the blanket `cap_drop: ALL` added in the security audit removed
  `CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETGID` and `SETUID`, so all of that failed with
  `EPERM` (`chown: changing ownership of '.': Operation not permitted`), the entrypoint
  aborted and `restart: always` looped it — taking the backend (unhealthy) and worker
  (never started) down with it. Both services now add back exactly those five capabilities,
  keeping the other nine of Docker's defaults dropped. The Helm chart is unaffected: it
  pairs `capabilities.drop: [ALL]` with `runAsUser: 999`, so its containers never enter
  the entrypoint's root branch at all
- Pasted Confluence Server/Data-Center page links now work as import scope:
  `/display/SPACE/Page+Title` URLs (umlauts, personal spaces included) are resolved to the
  page id at creation time via the source's API, with a clear message when resolution fails —
  previously only bare page ids and Cloud-style URLs were accepted. A page link pasted into
  the *space* scope field is rejected instead of silently importing the whole space

## [1.3.3] - 2026-08-22

### Added
- Job lists and job details name their owner: every job row carries `owner` ({id, username},
  batch-loaded in one query; legacy pre-auth jobs show none) — the foundation for the
  personal home dashboard and the "by <username>" attribution on the Processing work center
- VL connections power normal processing, not just benchmarks: every enabled connection
  now appears as a selectable profile (`VL: <name>`) in the File Task wizard, the
  re-run-with-profile dialog, mail-API ingestion, and Confluence attachment OCR. Selecting
  one stores the same settings shape benchmark variants use, so the worker path — including
  key decryption strictly inside the worker and the clean failure when a connection was
  removed — is shared and unchanged. Previously the "OpenAI-compatible Vision API" profile
  read credentials from environment variables only, so a VL connection that worked in a
  benchmark silently had no effect on normal uploads. The jobs table shows such jobs by
  their connection name; the deployment default profile deliberately stays restricted to
  the static OCR profiles
- Confluence imports carry their logical location: every imported page's frontmatter now
  records `space`, `confluence_path` (the ancestor titles down to its parent), `parent_title`,
  and `depth`, and a `> Confluence: A › B › C` breadcrumb line sits at the top of the body so
  RAG chunking keeps the context beyond the first chunk. The path comes from the crawl tree
  itself (plus one `ancestors` API call for the import root), not from parsing rendered
  table-of-contents macros — deterministic on every re-import, and auto-refresh resolves it
  per changed page the same way, best effort. Pages that are essentially link lists (children/
  TOC macros) are flagged `is_navigation: true`, parents get `children_titles` stamped in the
  end-of-run pass that already rewrites cross-page links, and an opt-in **Use hierarchy as
  tags** toggle on the import wizard turns breadcrumb parts into job tags so the jobs list
  can filter by section
- Sign-in events show up in the admin Logs tab: OIDC logins (with a claim diagnostic —
  which of email/preferred_username/upn/unique_name the ID token carried and whether the
  userinfo endpoint had to be queried), account provisioning, username/email syncs and
  skipped collisions, plus local sign-ins, failures, and lockouts, all as `backend` rows.
  Messages never contain tokens, passwords, or full subject identifiers
- Jobs can be re-run with a different OCR profile: `POST /api/v1/jobs/{id}/restart` now
  accepts an optional `{"profile_id": ...}` body (validated against the profile registry,
  persisted as previous/requested/effective profile like the existing lower-profile retry),
  and both the jobs table and the job detail page grew a "Re-run with profile…" dialog for
  finished and failed jobs. Confluence-imported pages stay excluded, as with Restart
- Confluence import runs are re-runnable with edits: the run detail API now exposes
  `source_id` and the stored options snapshot, and an "Edit & run again" action on the
  imports list and run detail opens the import wizard prefilled with that run's connection,
  scope, and options (`/imports/new?from=<run>`) — starting it creates a new run, so history
  and page-version chaining stay intact
- The markdown editor is reachable from the jobs table: an Edit action per finished job
  opens the job detail page straight in edit mode (`?edit=1`), and the detail page's
  toolbar gained an "Edit markdown" button that jumps to the editor
- Identity providers have a "Use email as username" switch (for IdPs like Microsoft Entra
  whose `preferred_username` is a UPN): when enabled, the claimed email becomes the
  account's username at provisioning, and existing accounts are renamed on their next
  login — skipped safely when another account already holds that name or the IdP sent no
  real email claim (migration 0012_provider_email_username; its revision id is deliberately
  ≤ 32 chars — see Fixed below)
- The Processing page is now an overview: per-user stat tiles (running/finished/failed,
  pages processed), a breakdown by job type (single, multiple files, Confluence import,
  mail), and the most recent jobs — the upload wizard moved to its own **File Task** entry
  under Processing in the sidebar (`/processing/new`)
- PaddleDoc has an app icon: two crossed paddles behind a document page on the emerald
  tile — served as SVG favicon and apple-touch-icon, used as the brand mark in the
  sidebar and on the login/setup pages (shared `PaddleDocLogo` component), and shown
  in the README header
- **Connections** is a new top-level page (`/connections`) where every user configures the
  external systems their account talks to, in three tabs: Confluence (create, test, rename,
  delete import sources and their auto-refresh interval — moved out of the collapsible
  section on the imports page), OpenWebUI (the full connection CRUD and recent-push history
  that used to live at `/openwebui`, which now redirects here), and VL connections. VL stays
  an admin-managed, deployment-wide resource: administrators get the same CRUD panel as in
  the admin area, everyone else a read-only list of the enabled models so they can see what a
  benchmark can run against

### Changed
- Creating work is one centered gesture everywhere: Home, Processing, and Imports show the
  pair **New File Task** + **New import** together, centered under the page header in a soft
  emerald style — no more lone primary button tucked into the top corner, and the empty
  states offer the same pair
- Home and Processing now have distinct jobs: Home is a personal overview — "Welcome back",
  quick actions, a **Needs attention** section (failed jobs, your own first, with a counter),
  **Your recent jobs**, and a slim one-line stats summary instead of the four big tiles —
  while Processing is the team's work center with the full stat tiles, a type distribution
  bar plus four clickable type cards that jump to the jobs list pre-filtered
  (`/jobs?type=…`, shown there as a clearable filter chip), and recent jobs attributed to
  their owner
- The File Task wizard's Continue is context-aware: disabled with a visible reason until the
  step is valid (step 3 needs a chosen file or at least one uploaded collection file), the
  active step carries a ring and emphasized label on top of the existing checkmarks, and
  step-bound errors appear right under the element they concern instead of only in a global
  banner
- Mail reads like an inbox now: one prominent search bar (Enter applies), sender, subject
  and parts summary on the left with date and status on the right, the whole row linking to
  the message, and the remaining filters tucked behind a Filters toggle
- A UI polish pass across the whole app: the home dashboard leads with "what do I do now"
  (New File Task, stats with errors surfaced, five most recent jobs) and demotes the system
  readout to a compact strip with expandable details; the File Task wizard became a real
  four-step flow (Metadata → Profile → Upload → Review & Start) with a persistent, keyboard-
  operable stepper — a single file now uploads when you hit Start, not when you pick it; the
  jobs list gained All/Running/Completed/Failed filter chips with counts; the sidebar groups
  entries under Workspace and Analyze & connect, moves Mail API out of Processing as its own
  input channel, renames Confluence Import to Imports, marks Admin as privileged, and
  strengthens the active state; Connections separates External services from AI models (the
  VL tab is now "VL Models"); benchmark results compare side by side with Best result and
  Fastest badges; and throughout: one primary action per screen, a consistent type scale,
  lighter cards, empty states that say what to do next, aria-current/aria-controls/aria-live
  wiring, and arrow-key tab navigation
- The File Task wizard sheds its email, tags, and password fields; folder creation
  (folder + subfolder) moved into an "Add folder" dialog. The department field remains for
  multi-file batches
- The jobs table breathes again: real column gaps and no-wrap cells (profile, pages, and
  quality no longer run together into one string) and a wider page container. The New Task
  button left the Jobs page — starting tasks lives in the sidebar now
- The sidebar is now two levels deep: Processing carries Jobs, API Mail Extraction (the
  former Mail entry) and Confluence Import (which previously had no navigation entry at all)
  as a collapsible submenu, and Benchmark is named **VL Benchmark** to say what it compares —
  the same document run through several vision-language models
- Paddle runtime settings (default OCR profile, timeout) moved from the top of the Processing
  page into a **Paddle** tab in the admin area, where they belong: `PUT /api/v1/paddle/settings`
  has always required an admin, so non-admin users were shown a form that failed with 403 on
  save. The Processing page still reads the deployment default to preselect a profile
- Confluence importing has exactly one entry point: the wizard's "Import from Confluence"
  tile is gone, and everything import-related lives under Processing > Confluence Import.
  The File Task wizard keeps two equally sized tiles, Single file and Multiple files
- The jobs table got denser and calmer: every row action is an icon button with a hover
  tooltip (download, restart, retry lower, re-run with profile, edit, push, delete), the
  Created column is gone (the timestamp lives in the filename's hover tooltip; sorting
  still defaults to newest first), and Used Profile shows compact codes like `ocr6m+v3`
  with the full profile name on hover. The freed width goes to the document column
- One naming scheme throughout: the processed items are **Jobs** everywhere (the jobs page
  was still headlined "Tasks"), and **File Task** is deliberately the only "task" — the flow
  that creates jobs from uploads. Alongside, the jobs table dropped its never-rendered
  title/description/compact/hideHeader props and dead password state, and the orphaned
  documents-center component was deleted — the frontend lints with zero warnings again
- The home dashboard dropped the "Document Magic" marketing hero and its Tasks shortcut —
  navigation runs through the sidebar. The live service readout it contained (pipeline state,
  Paddle service, queue depth, containers, worker nodes, GPU/CPU runtime) survives as a
  standalone **System status** card, including its honest degradation to last-known values
  when the backend health check fails

### Fixed
- Migration 0012 crash-looped every PostgreSQL deployment: its original revision id
  (`0012_provider_use_email_as_username`, 35 chars) exceeded Alembic's
  `alembic_version.version_num` VARCHAR(32), so the final version UPDATE aborted the whole
  transactional-DDL upgrade — the backend died before serving a request. SQLite, which the
  test suite runs on, silently ignores varchar limits, so 466 tests stayed green. The
  revision is renamed to `0012_provider_email_username` (28 chars) and a guard test now
  fails the suite if any revision id in the chain exceeds 32 chars
- Entra sign-ins no longer provision garbage accounts (username = raw `sub`, email =
  `sub@entra.oidc.invalid`): the email is now resolved from `email`, `upn`, `unique_name`,
  or `preferred_username` (values must look like an address; `DOMAIN\user` forms are
  rejected), and when the ID token carries none of them the userinfo endpoint is queried
  with the access token — best effort, a failure never blocks the login. Existing accounts
  self-heal on their next sign-in: a genuinely resolved email replaces a stored synthetic
  one (guarded against collisions; account matching stays strictly `(provider, sub)`), and
  the "Use email as username" sync then picks it up
- Uploading files that already exist as identical versions during a multi-file batch
  looked like nothing happened: the "N unchanged file(s) skipped" notice (and the upload
  progress panel) rendered only on the wizard's last step, while collection uploads happen
  on step 2. The feedback now shows on every step, in a visible notice box
- Every page of every Confluence import failed on PostgreSQL with
  `ForeignKeyViolation: import_page_states_job_id_fkey`. `ImportPageState` carries the raw
  `jobs.id` foreign key but no `relationship()` to `Job`, so SQLAlchemy's unit of work saw no
  ordering between the two mappers and wrote `import_page_states` *before* the `jobs` INSERT
  of the very job the row points at. The importer now flushes the job row before the
  page-state upsert. It stayed invisible because SQLite ignores foreign keys unless
  `PRAGMA foreign_keys=ON` is set per connection, so the test suite stored the dangling
  reference without complaint -- the suite now pins that pragma, matching PostgreSQL, and
  `import_page_states.job_id` was the only FK column in the schema lacking a relationship
- The API token form under Settings put its two inputs on different baselines: the
  "Expires in (days)" field carried a `hint`, which `Field` renders as an extra block below
  the input and which the row's `items-end` alignment then pushed the input up against. The
  optional-ness now lives in the placeholder
- `VL_PRIVATE_HOST_ALLOWLIST` was documented in `.env.example` but never passed to the
  containers: neither compose file forwarded it and the Helm chart had no value for it, so a
  self-hosted vLLM/Ollama/LiteLLM on a private address could not be reached at all. Both
  compose files now forward it to backend and worker, and the chart exposes
  `vl.privateHostAllowlist` on both deployments (the test probe runs in the backend, the OCR
  call in the worker)
- The SSRF fetcher's "resolves to a blocked address" rejection now says what to do about it.
  A private-address target that is perfectly reachable from inside the pod is still refused
  by design, and the old message gave no hint that a per-host allowlist exists — it read like
  a bug rather than a policy. Firewall docs gained a section on the same point

## [1.3.1] - 2026-08-15

### Changed
- Processing and Mail are now cleanly separated by intent: the Processing page handles
  every manual upload — including `.eml`, which joins the normal file picker and produces
  one regular job whose markdown contains the rendered mail body plus each attachment as
  its own section (converted with the selected OCR profile), with the standard frontmatter,
  folder/tags/password settings, and content-hash versioning like any other document. The
  Mail page now exclusively shows messages ingested programmatically via
  `POST /api/v1/mail/messages` (list locked to `source=api`); its manual upload zone moved
  to Processing. API ingestion keeps its own `profile_id` parameter (falls back to the
  configured default profile), and mail messages default to `source=api`

## [1.3.0] - 2026-08-15

### Added
- Universal mail ingestion: `POST /api/v1/mail/messages` accepts a raw RFC-822 email
  (`Content-Type: message/rfc822`, or `multipart/form-data` with a single `file` part for
  `curl -F` / n8n form mode) from any client — SMTP gateway, n8n workflow, a script — reading
  the body with a streaming, size-capped reader (413 once `MAX_MAIL_MESSAGE_BYTES`, default
  100 MiB, is exceeded) so nothing buffers unboundedly. The message is parsed server-side with
  Python's stdlib `email` package via a deterministic depth-first MIME-tree walk (not
  `iter_attachments()`, which loses content inside `multipart/signed`): S/MIME-signed mail,
  nested containers, inline `Content-ID` images, and forwarded `message/rfc822` attachments are
  all classified correctly, and an unsupported or oversized part is recorded as skipped without
  failing the whole request. The body renders straight to Markdown (no OCR) via the existing
  Confluence markdownify pipeline; every supported attachment becomes an ordinary job and runs
  through the standard OCR pipeline like any upload. Identity is `sha256(raw bytes)`: replaying
  already-seen bytes returns the existing message instead of reprocessing it (200,
  `replayed: true`) and re-dispatches any of its attachment jobs still stuck PENDING from a
  crashed request — the mechanism that makes sender-side retry loops (gateway outbox, n8n
  retry-on-fail) safe. Full retrieval API for downstream consumers (n8n, Bedrock AgentCore, RAG
  ingestors): list/detail with filters (`q`, `message_id`, `sha256`, `source`, date range), body
  as plain text, the original `.eml` and any individual part as a download, and
  `GET .../export.json` (schema `paddledoc.mail-export/1`) bundling the envelope, body markdown,
  and every attachment's OCR markdown in one call for poll-until-`complete` consumers. `DELETE`
  removes a message, optionally cascading to its attachment jobs. See
  [docs/integrations/mail-ingestion.md](docs/integrations/mail-ingestion.md)
- Mail UI: a first-class **Mail** section (`/mail`, sidebar entry) with a filterable, paginated
  message list showing an aggregate status derived from each message's attachment jobs, and a
  message detail page (`/mail/{id}`) with the envelope, rendered body, a parts table linking
  every processed attachment to its job, and downloads for the raw `.eml` and individual parts.
  The detail page polls every 2.5s while any attachment job is still PENDING/RUNNING. Attachment
  jobs carry a "from mail" badge linking back to their source message on the job list and job
  detail pages
- Manual `.eml` upload on `/mail`: a drag-and-drop zone plus file picker uploads one or more
  `.eml` files sequentially against the ingest endpoint, with per-file progress and a result
  list (ingested / already ingested / failed, per file — one bad file never aborts the rest of
  the batch); uploading a single file that ingests successfully navigates straight to its
  detail page. Also reachable from the Processing page's step-1 source picker

### Changed
- Uploaded-file version chaining (matching filename -> next `document_version`) now excludes
  mail-attachment jobs — an attachment named e.g. `invoice.pdf` arriving by mail from an
  unrelated sender no longer gets chained into an unrelated document's version history. Mail
  attachment jobs still appear normally everywhere else (stats, folders, `/markdown-files`)

### Fixed
- `_attach_tags` could create a duplicate `Tag` row (surfacing only as a misleading commit-time
  `IntegrityError`) when called more than once for the same brand-new tag name inside a single
  uncommitted transaction — exactly what mail ingestion now does once per attachment job. A
  missing `db.flush()` after inserting a new tag meant a second call's lookup couldn't see the
  first call's still-pending insert

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
