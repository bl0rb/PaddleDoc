# <img src="docs/logo.svg" width="30" alt="" align="top"> PaddleDoc

[![PR CI](https://img.shields.io/github/actions/workflow/status/bl0rb/PaddleDoc/pr-ci.yml?branch=main&label=PR%20CI&logo=githubactions&logoColor=white&style=flat-square)](https://github.com/bl0rb/PaddleDoc/actions/workflows/pr-ci.yml)
[![Release](https://img.shields.io/github/v/release/bl0rb/PaddleDoc?label=Release&logo=github&color=234C77&style=flat-square)](https://github.com/bl0rb/PaddleDoc/releases/latest)
[![License](https://img.shields.io/badge/License-MIT-6C7686?style=flat-square)](LICENSE)

[![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white&style=flat-square)](backend/Dockerfile)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.141-009688?logo=fastapi&logoColor=white&style=flat-square)](backend/requirements.in)
[![Celery](https://img.shields.io/badge/Celery-5.6-37814A?logo=celery&logoColor=white&style=flat-square)](backend/worker.Dockerfile)
[![Next.js](https://img.shields.io/badge/Next.js-16-000000?logo=nextdotjs&logoColor=white&style=flat-square)](frontend/package.json)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white&style=flat-square)](docker-compose.nas.yml)
[![Redis](https://img.shields.io/badge/Redis-7-FF4438?logo=redis&logoColor=white&style=flat-square)](docker-compose.nas.yml)
[![Docker](https://img.shields.io/badge/Docker-compose%20%C2%B7%20NAS-2496ED?logo=docker&logoColor=white&style=flat-square)](docker-compose.nas.yml)
[![Kubernetes](https://img.shields.io/badge/Kubernetes-Helm%20chart-326CE5?logo=kubernetes&logoColor=white&style=flat-square)](charts/paddledoc)

[![pip-audit](https://img.shields.io/badge/pip--audit-0%20findings-216B52?logo=python&logoColor=white&style=flat-square)](.github/workflows/pr-ci.yml)
[![npm audit](https://img.shields.io/badge/npm%20audit-0%20findings-216B52?logo=npm&logoColor=white&style=flat-square)](.github/workflows/pr-ci.yml)
[![Audit](https://img.shields.io/badge/Security%20audit-Claude%20Opus%205-D97757?logo=anthropic&logoColor=white&style=flat-square)](#security)
[![Containers](https://img.shields.io/badge/Containers-non--root%20uid%201000-0DB7ED?logo=docker&logoColor=white&style=flat-square)](backend/Dockerfile)
[![Dependencies](https://img.shields.io/badge/Dependencies-hash--locked-8A5A07?style=flat-square)](backend/requirements.txt)
[![Tests](https://img.shields.io/badge/Tests-519%20backend-3F6382?logo=pytest&logoColor=white&style=flat-square)](backend/tests)

PaddleDoc is a document processing platform powered by PaddleOCR that converts PDFs, Office files, Mails and images into structured Markdown for RAG and AI pipelines.

It is built for teams that need reliable ingestion quality, searchable outputs, and simple deployment options from standalone NAS Docker to Kubernetes.

![Home page](docs/screenshots/overview-133.png)

Screenshots of every screen live in the [User Guide](https://github.com/bl0rb/PaddleDoc/wiki/User-Guide).

## Why PaddleDoc

Managing OCR and document normalization at scale gets messy fast. PaddleDoc gives you one workflow for ingestion, extraction, quality scoring, versioning, and retrieval-ready output.

- AI-first Markdown output with rich YAML frontmatter (source, hash, version, uploader, team, engine)
- **Document versioning built in** — re-uploading a changed file becomes version N+1 with full history; byte-identical re-uploads are detected and skipped
- Multiple OCR and vision profiles (fast OCR, layout-aware, VL, OpenAI-compatible) — **VL connections work as normal processing profiles**, not just benchmark participants: any admin-configured vision-language endpoint shows up as `VL: <name>` in the File Task wizard and every re-run picker
- **VL Benchmark** — run one document against up to 6 vision-language models plus an OCR baseline and compare the results side by side
- **Personal API tokens** — programmatic access via `Authorization: Bearer`, no cookie handling
- **Mail ingestion** — POST a raw email and PaddleDoc parses it, renders the body
  to Markdown, and OCRs every attachment as its own job; idempotent by content hash
- **Confluence import with real hierarchy** — imported pages carry breadcrumb frontmatter (`space`, `confluence_path`, `parent_title`, `depth`), navigation pages are flagged, parents get `children_titles`, and an opt-in toggle turns the breadcrumb into filterable job tags; runs can be edited and re-run from their own history
- Folder and tag organization, search, quality grades, JSON export per job
- Queue-based processing with backend + worker separation; worker logs live in the admin console
- User accounts with team visibility, local login and OIDC SSO (Keycloak, Microsoft Entra ID), with Entra-friendly claim resolution and sign-in events visible in the admin Logs tab

## Get Started

Choose your deployment mode:

| Mode | Best for | Command |
|---|---|---|
| Standalone Docker | Local server or NAS (UGREEN/QNAP/Synology) | `./scripts/init-env.sh && docker compose -f docker-compose.nas.yml up -d` |
| Docker (Dev/Single Host) | Local development with local builds | `./scripts/init-env.sh && docker compose up --build` |
| Docker + NVIDIA GPU | Windows Docker Desktop with GPU-enabled worker profile | `wsl bash scripts/init-env.sh; docker compose -f docker-compose.nas.yml -f docker-compose.gpu.yml up -d` |
| Kubernetes (Helm) | k3s/k8s clusters and scale-out deployments | `helm upgrade --install paddledoc ./charts/paddledoc -n paddledoc --create-namespace --set auth.secretKey.value=$(openssl rand -hex 32)` |

### Standalone NAS (No Kubernetes)

Use prebuilt GHCR images and persistent local folders.

```bash
# Generates SECRET_KEY, POSTGRES_PASSWORD and REDIS_PASSWORD into .env.
# The compose files have no fallback values for these on purpose — a default
# that ships in the repository is a published secret, and SECRET_KEY is the
# key every stored OIDC client secret, Confluence credential and VL API key
# is encrypted under. Compose refuses to start until they are set.
./scripts/init-env.sh

docker compose -f docker-compose.nas.yml up -d
```

Further environment values go into the same `.env` next to the compose file — see `.env.example`:

```bash
POSTGRES_USER=paddledoc
POSTGRES_PASSWORD=change-this
POSTGRES_DB=paddledoc
PADDLEDOC_TAG=latest
PADDLEDOC_PUBLIC_API_URL=http://NAS_IP:8000

# Since v1.1.0 (authentication):
SECRET_KEY=generate-with-openssl-rand-hex-32   # signs sessions, encrypts stored secrets — set once, never change
REDIS_PASSWORD=change-this-too
PUBLIC_API_URL=http://NAS_IP:8000              # backend URL used for OIDC redirect URIs
CORS_ORIGINS=["http://NAS_IP:3000"]            # your frontend origin(s); no wildcard — cookies are credentialed
```

Endpoints:

- Frontend: `http://NAS_IP:3000`
- Backend: `http://NAS_IP:8000`

First run: open `http://NAS_IP:3000/setup` and create the initial admin account. Everything else requires a login from then on. Database migrations run automatically on backend startup.

### Docker (Local Build)

```bash
docker compose up --build
```

Endpoints:

- Frontend: `http://localhost:3000`
- Backend: `http://localhost:8000`

### Kubernetes (Helm)

Quick install from local chart:

```bash
helm upgrade --install paddledoc ./charts/paddledoc \
  --namespace paddledoc --create-namespace \
  --set auth.secretKey.value=$(openssl rand -hex 32)
```

Install from GHCR OCI chart:

```bash
helm install paddledoc oci://ghcr.io/bl0rb/charts/paddledoc --version 1.3.3 \
  --namespace paddledoc --create-namespace \
  --set auth.secretKey.value=$(openssl rand -hex 32)
```

Since chart 1.1.0 a `SECRET_KEY` is required — the chart refuses to render without `auth.secretKey.value` or `auth.secretKey.existingSecret` (prefer the latter in real deployments, e.g. provisioned via External Secrets Operator). More chart options and examples are in [charts/paddledoc/README.md](charts/paddledoc/README.md).

## Core Features

- Upload via drag and drop or file picker (PDF, DOCX, PPTX, XLSX, XLS, PNG, JPG, JPEG)
- **Content-hash document versioning**: same-named uploads within a team become version chains with full history; identical content is deduplicated
- Job lifecycle `PENDING -> RUNNING -> FINISHED / FAILED` with adaptive live updates, owner attribution on every row
- Folder tree navigation, tags, search and filtering, date ranges, status chips and a job-type filter (single file / collection / Confluence import / mail)
- A/B/C document quality gate per job
- Versioned markdown editing on the job detail page, reachable straight from a jobs-table row action
- **JSON export per job** — metadata, uploader, processing details, and markdown in one file
- **VL Benchmark** with per-variant metrics and side-by-side markdown comparison; the same VL connections double as selectable processing profiles for regular uploads
- **Personal API tokens** for programmatic access (created on the Settings page, shown once, stored hashed)
- **Mail ingestion** — POST a raw RFC-822 email, body renders to Markdown, attachments become regular jobs; idempotent by content hash, with a Mail UI and manual `.eml` upload
- **Confluence import** with hierarchy-aware frontmatter, an opt-in hierarchy-as-tags toggle, and an "Edit & run again" action that reopens a past run's settings
- Jobs can be re-run with a different profile from either the jobs table or the job detail page
- **Worker logs in the admin console** — level/worker/text filters, auto-refresh, tracebacks — alongside a Logs tab entry for every sign-in (OIDC and local)
- Password-gated view/download/edit/delete per job
- OpenAI-compatible page-by-page vision profile
- User accounts with per-user/team data visibility, local login and OIDC SSO
- Admin console for users, teams, identity providers, worker logs, sign-in logs, Paddle runtime settings, and VL connections

## Product Walkthrough

### Navigation

The sidebar is grouped by intent:

- **Workspace** — Home (personal overview), Processing (the team's work center, with a collapsible submenu for **File Task**, **Jobs**, and **Imports**), and **Mail API** (messages ingested programmatically)
- **Analyze & connect** — **VL Benchmark** and **Connections**

### Home (`/`)

A personal "what do I do now" landing page: a **New File Task** call to action, a **Needs attention** section (your own failed jobs first, with a counter), **Your recent jobs**, and a slim one-line stats summary. The live service readout (Paddle service, queue depth, containers, worker nodes, GPU/CPU runtime) lives in a compact, expandable **System status** card — with honest degradation: if the backend becomes unreachable, it marks data as "last known" instead of pretending it is current.

### Processing (`/processing`)

The team's work center: stat tiles (running/finished/failed, pages processed), a type-distribution bar with four clickable cards (single file, multiple files, Confluence import, mail) that jump to the jobs list pre-filtered, and recent jobs attributed to their owner. Starting new work happens via the **File Task** and **New import** actions, centered under the header.

### File Task (`/processing/new`)

The upload wizard is a four-step flow — **Metadata → Profile → Upload → Review & Start** — with a persistent, keyboard-operable stepper. Metadata covers mode (single file or collection), target folder/subfolder (via an "Add folder" dialog) and department; Profile lists the static OCR profiles alongside every enabled VL connection as `VL: <name>`; a single file uploads only when you hit Start on the Review & Start step, not the moment you pick it. Continue stays disabled with a visible reason until the current step is valid, and step-bound errors and duplicate-skip notices appear right under the element they concern.

### Jobs (`/jobs`)

![Jobs](docs/screenshots/jobs-133.png)

Browse all jobs with folder tree, All/Running/Completed/Failed filter chips with counts, a job-type filter, quality grades, and version badges — `v2` marks documents that were re-uploaded with changed content. Every row action is an icon button with a hover tooltip: download, restart, retry with a lower profile, re-run with a different profile, edit markdown, push (OpenWebUI), delete. The Used Profile column shows compact codes like `ocr6m+v3` (full name on hover), and jobs processed by a VL connection show its name.

### Job Detail (`/jobs/{id}`)

Review metadata, quality gate, and processing info; preview or edit markdown (an "Edit markdown" toolbar button jumps straight into edit mode); download the result as Markdown or JSON; re-run with a different profile. The **Versions** table shows the full history of the document — who uploaded which version when, with content hashes — and links to every prior version.

### Mail API (`/mail`)

Reads like an inbox: a search bar, sender/subject/parts summary on the left, date and status on the right, with the remaining filters tucked behind a Filters toggle. It shows email messages ingested programmatically via `POST /api/v1/mail/messages`; manual `.eml` upload lives on the Processing page alongside regular file uploads (drag and drop or file picker), producing a normal job. See [API Quickstart](#api-quickstart) below for programmatic ingestion.

Open a message to see the full envelope, the body rendered as Markdown, and a parts table — every attachment links to its OCR job, with per-part downloads and a raw `.eml` download for the whole message. The page polls while any attachment job is still processing. Attachment jobs carry a "from mail" badge back to their source message on the Jobs list and Job Detail pages.

### Imports (`/imports`)

Confluence import runs, each with a status and page count. Every imported page's frontmatter carries `space`, `confluence_path` (ancestor titles down to its parent), `parent_title`, and `depth`, plus a `> Confluence: A › B › C` breadcrumb line at the top of the body — so RAG chunking keeps hierarchical context beyond the first chunk. Pages that are essentially link lists are flagged `is_navigation: true`; parents get `children_titles`. An opt-in **Use hierarchy as tags** toggle on the import wizard turns breadcrumb parts into job tags so the Jobs list can filter by section. Any run can be reopened via **Edit & run again**, which prefills the wizard with that run's connection, scope, and options and starts a fresh run — history and page-version chaining stay intact.

### VL Benchmark (`/benchmark`)

Run one document against up to 6 admin-configured VL connections plus optionally one OCR profile (2–7 variants per run) — the same document through several vision-language models, compared side by side.

![Benchmark report](docs/screenshots/benchmark-report-133.png)

The report compares duration, pages, output size, quality grade, and errors per variant — with Best result and Fastest badges, a tabbed markdown preview, links to each variant's job, and a JSON export. Variants that silently degraded to plain-text fallback are never crowned fastest/best.

### Connections (`/connections`)

Every user configures the external systems their account talks to, in two groups of tabs: **External services** (Confluence — create, test, rename, delete import sources and their auto-refresh interval; OpenWebUI — connection CRUD and recent-push history) and **AI models** (**VL Models** — administrators get the full CRUD panel, everyone else a read-only list of the enabled connections so they can see what a File Task or benchmark can run against).

### Settings (`/settings`)

Create personal API tokens for programmatic access. Tokens are shown exactly once, stored as a hash, support optional expiry, and can be revoked anytime. Token management itself requires a browser session — a leaked token cannot mint replacements.

### Admin Console (`/admin`)

Tabs: **Users** (roles, teams, activation, password resets, assigning legacy ownerless jobs), **Teams**, **Identity Providers**, **Logs**, **VL Connections**, and **Paddle** (default OCR profile and timeout).

**Registering an OIDC provider (Keycloak, Microsoft Entra ID, ...):** add it under **Admin → Identity Providers** with the issuer URL, client ID/secret and scopes from your IdP. In the IdP's app registration, set the redirect URI / callback URL to:

```
{PUBLIC_API_URL}/api/v1/auth/oidc/{slug}/callback
```

`{slug}` is the URL-safe identifier you choose in the "Slug" field (e.g. `entra`), and `{PUBLIC_API_URL}` is the backend's externally reachable base URL (`PUBLIC_API_URL` above / `auth.publicApiUrl` in the Helm chart). Example for slug `entra`: `https://paddledoc.example.com/api/v1/auth/oidc/entra/callback`.

For IdPs like Microsoft Entra ID whose `preferred_username` is a UPN rather than an email, a per-provider **"Use email as username"** switch makes the claimed email the account's username at provisioning (and renames existing accounts on their next login, skipped safely on a collision). Claim resolution itself falls back through `email`, `upn`, `unique_name`, and `preferred_username`, then queries the userinfo endpoint as a last resort — best effort, never blocking the login.

**VL Connections** hold OpenAI-compatible vision endpoints: base URL, model, API key (encrypted at rest, never displayed again), and a per-connection system prompt — with a test button that reports latency. Internal endpoints (vLLM, LiteLLM, Ollama) are first-class citizens. Every enabled connection is usable both from the VL Benchmark and, since 1.3.3, as a normal processing profile (`VL: <name>`) in the File Task wizard, re-run dialogs, mail ingestion, and Confluence attachment OCR.

**Logs** covers both worker output and sign-ins: worker logs stream the processing containers' output — level/worker/text filtering, auto-refresh, expandable tracebacks, identical under Docker Compose and Kubernetes (persisted via the database, no docker.sock required) — and sign-in events (OIDC and local logins, account provisioning, username/email syncs, failures and lockouts) are logged alongside them, with an OIDC claim diagnostic and no tokens, passwords, or full subject identifiers in the message text.

## OCR Profiles

| Profile | Typical Use |
|---|---|
| PP-OCRv6 Tiny | Fastest throughput, lowest resource usage |
| PP-OCRv6 Small | Balanced speed and quality |
| PP-OCRv6 Medium | Higher OCR quality |
| PP-StructureV3 variants | Stronger table/layout extraction |
| PaddleOCR-VL 1.6 (0.9B) | Rich document understanding, best on GPU |
| OpenAI-compatible Vision API | Route each page to an OpenAI-compatible endpoint |

## API Quickstart

The fastest way to use the API programmatically is a **personal API token** (Settings → API tokens):

```bash
TOKEN=pd_your-token-here

# Upload a document
curl -H "Authorization: Bearer $TOKEN" \
  -F "file=@invoice.pdf" -F "profile_id=ppocrv6_tiny" -F "folder=finance" \
  http://localhost:8000/api/v1/upload

# Poll the job, then fetch the result as markdown or JSON
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/jobs/<job_id>
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/jobs/<job_id>/preview
curl -H "Authorization: Bearer $TOKEN" -o result.json \
  http://localhost:8000/api/v1/jobs/<job_id>/export.json
```

Mail ingestion (since v1.3.0) takes the raw `.eml` bytes as the request body — no multipart encoding needed:

```bash
# POST a raw email; 201 on first ingest, 200 (replayed:true) if those exact
# bytes were already ingested — either way the response has the message id
curl -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: message/rfc822" \
  --data-binary @quarterly-report.eml \
  http://localhost:8000/api/v1/mail/messages

# Poll until every attachment job is terminal, then fetch the aggregated export
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/mail/messages/<message_id>
curl -H "Authorization: Bearer $TOKEN" -o mail-export.json \
  http://localhost:8000/api/v1/mail/messages/<message_id>/export.json
```

Browser-style session login works too (`POST /api/v1/auth/login` with a cookie jar); token management endpoints themselves always require a session.

Common endpoints:

- `POST /api/v1/upload` — upload one document (409 with `duplicate_of` if content is identical to the latest version)
- `GET /api/v1/jobs` / `GET /api/v1/jobs/{id}` — list/detail
- `GET /api/v1/jobs/{id}/versions` — version history of the document
- `GET /api/v1/jobs/{id}/preview` / `/download` — markdown result
- `GET /api/v1/jobs/{id}/export.json` — structured JSON export (metadata + markdown)
- `PUT /api/v1/jobs/{id}/save` — edit markdown (creates an edit version)
- `POST /api/v1/benchmarks` — start a benchmark run; `GET /api/v1/benchmarks/{id}/report` for the comparison
- `GET /api/v1/vl-connections` — enabled VL connections (id, name, model)
- `POST /api/v1/mail/messages` — ingest a raw RFC-822 email (`Content-Type: message/rfc822`, or `multipart/form-data` with a `file` part); parses the message, converts the body to markdown, and OCRs each supported attachment as its own job. Idempotent by content hash: replaying the same bytes returns the existing message (200) instead of reprocessing; a new message is 201. See [docs/integrations/mail-ingestion.md](docs/integrations/mail-ingestion.md).
- `GET /api/v1/mail/messages` / `GET /api/v1/mail/messages/{id}` — list/detail (filters: `q`, `message_id`, `sha256`, `source`, `from_date`/`to_date`)
- `GET /api/v1/mail/messages/{id}/export.json` — envelope + body markdown + every attachment's OCR markdown in one call, for n8n/Bedrock AgentCore-style polling consumers
- `POST /api/v1/auth/tokens` / `GET` / `DELETE /api/v1/auth/tokens/{id}` — API token management (session only)
- `GET /api/v1/auth/admin/worker-logs` — worker logs (admin)
- `GET /api/v1/stats`, `GET /api/v1/health`, `GET /api/v1/paddle/status`, `GET /api/v1/paddle/capabilities`

## n8n Integration

Use HTTP Request nodes with a simple upload -> poll -> fetch pattern.

```mermaid
flowchart LR
   A[Document Source\nPDF DOCX PPTX XLSX PNG JPG] --> B[n8n Trigger\nWebhook / Schedule / Drive Watch]
   B --> C[n8n HTTP Request\nPOST /api/v1/upload]
   C --> D[PaddleDoc Queue\nCelery + Worker]
   D --> E[PaddleOCR Processing\nStructured Markdown Output]
   E --> F[n8n Poll Loop\nGET /api/v1/jobs/job-id]
   F --> G[n8n Fetch Result\nGET preview / export.json]
   G --> H[RAG Ingestion\nChunk + Embed + Index]
   H --> I[Retrieval + Answering\nVector Search + LLM]
```

Since v1.2.1, the simplest integration is a **personal API token**: create one under Settings for a dedicated PaddleDoc user and set a single `Authorization: Bearer pd_...` header on every HTTP Request node — no login node, no cookie forwarding. Use `/export.json` to get markdown plus metadata (hash, version, quality grade) in one call.

**Mail ingestion (since v1.3.0)** gives n8n a second, even simpler pattern: an HTTP Request node with `Bearer pd_...` auth, method `POST`, URL `{base}/api/v1/mail/messages?source=n8n`, and the body set to **binary data** with content type `message/rfc822` — straight from an IMAP/Email-Trigger node's raw output, no attachment decoding or base64 handling on the n8n side. `id` in the response (200 and 201 are both success — 200 just means "already known") feeds the same poll-then-fetch pattern, ending at `GET .../{id}/export.json` for the body markdown plus every attachment's OCR markdown in one call.

n8n URL choice:

- n8n inside Docker with PaddleDoc: `http://backend:8000`
- n8n on host machine: `http://localhost:8000`

## Deployment and Runtime Notes

### Upgrading an existing installation

Two changes in this release need a one-time step on hosts that were set up earlier:

1. **The containers no longer run as root** (UID/GID 1000 now, matching what the Helm chart already enforced). Bind-mounted directories still belong to root, so hand them over before starting:

   ```bash
   docker compose -f docker-compose.nas.yml down
   sudo chown -R 1000:1000 ./nas-data
   docker compose -f docker-compose.nas.yml up -d
   ```

   The PaddleOCR model cache moved from `/root/.paddlex` to `/home/paddledoc` inside the container; the updated compose file mounts the new path already. Kubernetes deployments are unaffected — the chart set `runAsUser: 1000` all along.

2. **The compose files no longer carry fallback secrets.** If you never overrode `SECRET_KEY`, `POSTGRES_PASSWORD` or `REDIS_PASSWORD`, the stack was running with the values from this repository. Compose now refuses to start without them:

   ```bash
   ./scripts/init-env.sh   # fills in only the keys that are missing
   ```

   Careful with `SECRET_KEY`: keep the old value if you have stored OIDC client secrets, Confluence credentials or VL API keys — they are encrypted under a key derived from it and cannot be read with a new one. If it was the repository default, treat those credentials as compromised, generate a fresh key and re-enter them.

Firewall rules for restricted networks: see
[docs/firewall-requirements.md](docs/firewall-requirements.md) for the full
inbound/outbound connection matrix per component.

### Architecture

```text
frontend  (Next.js + TypeScript + Tailwind)
backend   (FastAPI + SQLAlchemy + Alembic + Celery)
postgres  (default in Docker compose)
redis     (queue/broker)
worker    (Celery worker; mirrors its logs into Postgres for the admin console)
```

Migrations run automatically on backend startup (Alembic, currently `0001` … `0012`).

### GPU Runtime (Windows + NVIDIA)

Recommended: prebuilt GHCR images plus the GPU override — no local build. The published `paddledoc-worker` image already contains `paddlepaddle-gpu` on amd64, and `docker-compose.gpu.yml` is a pure runtime overlay (NVIDIA device reservation, VL default profile, solo worker pool), so it stacks directly on the prebuilt-image compose file:

```powershell
# generate SECRET_KEY / POSTGRES_PASSWORD / REDIS_PASSWORD into .env
# (bash script: run via WSL as shown — needs an installed distro like Ubuntu —
# or from Git Bash without the wsl prefix)
wsl bash scripts/init-env.sh

docker compose -f docker-compose.nas.yml -f docker-compose.gpu.yml up -d
```

The defaults target a desktop setup (`http://localhost:3000` / `http://localhost:8000`), so no further URL configuration is needed. Prerequisite: a current NVIDIA driver on the Windows host — Docker Desktop's WSL2 integration handles the CUDA passthrough itself.

Building the images locally instead (only needed when you changed backend/worker/frontend code):

```powershell
wsl bash scripts/init-env.sh
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

Behavior summary:

- Worker image includes `paddlepaddle-gpu` (amd64 — the published image; a locally built arm64 image ships the CPU wheel instead)
- Runtime auto-detects CUDA and falls back to CPU
- GPU override switches default profile to `paddlevl_1_6_0_9b`
- Uses safer worker settings for CUDA stability (`solo`, concurrency `1`)

### Worker Scaling and Tuning

Scale workers (local-build dev compose only — `docker-compose.nas.yml` pins `container_name: paddledoc_worker`, which cannot be scaled, and the GPU overlay's solo-pool settings assume a single worker):

```bash
docker compose up --build -d --scale worker=2
```

Memory-constrained baseline:

- `WORKER_MEMORY_LIMIT=2g` to `3g`
- `CELERY_WORKER_CONCURRENCY=1`
- `CELERY_MAX_TASKS_PER_CHILD=5`
- `OMP_NUM_THREADS=1`
- `ONNXRUNTIME_INTRA_OP_NUM_THREADS=1`

Worker log capture (admin console): `WORKER_LOG_CAPTURE_LEVEL` (default `INFO`, independent of the console `--loglevel`) and `WORKER_LOG_RETENTION_MAX_ROWS` (default `20000`).

## OpenAI-Compatible Vision

Two ways to use vision-language models:

**1. VL connections (recommended)** — admins add any number of OpenAI-compatible endpoints under Admin → VL Connections (base URL, model, encrypted API key, per-connection system prompt) and test them from the UI. Every enabled connection is usable both from the VL Benchmark and as a regular processing profile (`VL: <name>`) in the File Task wizard, re-run dialogs, mail ingestion, and Confluence attachment OCR — key decryption stays strictly inside the worker either way. Internal endpoints like vLLM/LiteLLM/Ollama work out of the box. The connection appends `/v1/chat/completions` to the base URL.

**2. `openai_vision` profile (env-based)** — a single global endpoint for regular processing:

```dotenv
OPENAI_API_BASE_URL=https://api.openai.com
OPENAI_API_BEARER_TOKEN=sk-your-key-here
```

Ollama example:

```dotenv
OPENAI_API_BASE_URL=http://host.docker.internal:11434
OPENAI_API_BEARER_TOKEN=ollama
```

Apply env changes without rebuilding images (pass the same `-f` file list your deployment uses, e.g. `-f docker-compose.nas.yml -f docker-compose.gpu.yml`):

```bash
docker compose up -d --no-deps backend worker
```

## Security

PaddleDoc is built to run inside your own network. What is wired in by default:

| Area | Implementation |
| --- | --- |
| Sessions | Opaque database-backed tokens (not JWTs), stored as SHA-256, `HttpOnly` · `Secure` · `SameSite=Lax`, sliding 7 days with a hard 30-day cap, revoked instantly on logout or deactivation |
| Login | bcrypt, one constant-time verification on every path (no username enumeration), one generic error for every failure mode, plus a per-account lockout after 10 failed attempts |
| API tokens | `Authorization: Bearer`, stored as SHA-256, optional expiry, never mixed up with a browser session |
| Outbound requests | Every admin-supplied URL (OIDC discovery, Confluence import, VL endpoints) goes through `safe_fetch`: connection pinned to the validated IP (no DNS rebinding), every redirect hop re-checked, credentials dropped on origin change, cloud-metadata addresses blocked unconditionally |
| Secrets at rest | OIDC client secrets, import credentials and VL API keys under three HKDF-separated Fernet keys derived from `SECRET_KEY` |
| Rows | Jobs, collections and mail scoped by owner and team; ownerless legacy rows are admin-only |
| HTTP | CSP, `X-Frame-Options`, `nosniff`, `Referrer-Policy`, `Permissions-Policy`, HSTS over TLS; CSRF checked via `Origin`/`Referer` on every state-changing request |
| Containers | All three images run as UID/GID 1000, `no-new-privileges` and `cap_drop: ALL` in compose, matching `securityContext` in the Helm chart |
| Supply chain | Actions pinned to commit SHAs, backend and worker dependencies hash-locked, `pip-audit` and `npm audit` run on every PR |

**Reviewed by:** the codebase was put through a security audit by **Claude Opus 5** (authentication and authorization, injection, SSRF, secret handling, containers, CI and supply chain). All findings are fixed; the fixes are covered by the test suite.

**Two things worth knowing when you deploy:**

- `SECRET_KEY` is what makes the stored third-party credentials readable. Set it once via `scripts/init-env.sh` (or your secret manager) and keep it — rotating it invalidates every stored OIDC client secret, import credential and VL API key, and they have to be entered again.
- Self-hosted VL endpoints (vLLM, Ollama, LiteLLM) live on private addresses, which `safe_fetch` blocks by default. List them in `VL_PRIVATE_HOST_ALLOWLIST` (`["vl.internal:8000"]`), the same way `IMPORT_PRIVATE_HOST_ALLOWLIST` works for an internal Confluence. Cloud-metadata addresses stay blocked either way.
- Any private-address target needs an app-level allowlist on top of the firewall rule: an internal Confluence in `IMPORT_PRIVATE_HOST_ALLOWLIST`, a self-hosted OpenWebUI in `OPENWEBUI_PRIVATE_HOST_ALLOWLIST`. A host that is reachable from inside the pod is still rejected with `resolves to a blocked address` until it is listed. Set it on the backend *and* the worker and restart both — see [docs/firewall-requirements.md](docs/firewall-requirements.md#privateinternal-targets-need-an-app-level-allowlist-too).

To report a vulnerability, please open a GitHub Security Advisory rather than a public issue.

## Publishing to GHCR

Published images:

- `ghcr.io/bl0rb/paddledoc-backend`
- `ghcr.io/bl0rb/paddledoc-worker`
- `ghcr.io/bl0rb/paddledoc-frontend`

### Image publishing (automated)

Workflow: `.github/workflows/publish-ghcr-images.yml`

Trigger publish via git tag:

```bash
git tag v1.3.3
git push origin v1.3.3
```

This publishes multi-arch images (`linux/amd64`, `linux/arm64`; worker is amd64) tagged with the version and `latest`. Pre-release tags (anything with a hyphen, e.g. `v1.3.0-rc.1`) publish their version tag but deliberately do **not** move `latest`, and their GitHub release is marked as a prerelease.

### Helm chart publishing (automated)

Workflow: `.github/workflows/publish-ghcr-helm-chart.yml`

On `v*` tags, the chart is packaged and pushed to:

- `oci://ghcr.io/bl0rb/charts`

## Troubleshooting

### Dashboard loads but stats/profiles/jobs stay empty (Windows)

Symptom: UI loads but API requests to localhost fail intermittently due to WSL2/IPv6 loopback forwarding.

Fix backend port forward — use the same `-f` file list your deployment runs with, or compose will silently switch the backend to the other stack's configuration:

```powershell
# prebuilt-image deployment (recommended GPU path):
docker compose -f docker-compose.nas.yml -f docker-compose.gpu.yml up -d --force-recreate --no-deps backend

# local-build deployment:
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --force-recreate --no-deps backend
```

IPv4 health check:

```powershell
curl.exe -s -o NUL -w "%{http_code}\n" http://127.0.0.1:8000/api/v1/health
```

### Worker warnings about model hosters (restricted egress)

If worker egress to the model download hosts is blocked and models come from a pre-warmed cache, set `PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True` in the worker environment (Helm: via `worker.extraEnv`; Compose: add it to the worker service's `environment:` block) to skip the ~9s connectivity check per model instantiation. Egress rules per component: [docs/firewall-requirements.md](docs/firewall-requirements.md).

## Local Development

Backend:

```bash
cd backend
python -m pip install -r requirements.txt
pytest -q
uvicorn app.main:app --reload
```

Frontend:

```bash
cd frontend
npm install
npm run build
npm run dev
```

## Roadmap

### RAG Quality Foundation

- [ ] Define measurable quality and retrieval benchmarks
- [x] Grade A/B/C document quality gate
- [x] Multi-model VL benchmark with comparison reports
- [ ] Add a regression-focused RAG evaluation harness

### Reliability and Operations

- [x] Worker logs in the admin console (portable, DB-backed)
- [x] Sign-in audit trail (OIDC and local logins, provisioning, lockouts) in the admin Logs tab
- [ ] Add deeper observability (queue depth, latency, retries, failures)
- [ ] Add stronger governance (stricter validation, RBAC)

### Delivery and Workflow

- [x] Automate multi-arch GHCR image publishing on release tags
- [x] Automate Helm OCI chart publishing to GHCR on release tags
- [x] Add PR CI gates (lint, tests, and build checks) via `.github/workflows/pr-ci.yml`
- [x] Pre-release (rc) tags that never move `latest`
- [ ] Add image signing/provenance verification and immutable release policy
- [ ] Expand security scanning and SBOM coverage

### Product and Ecosystem

- [x] Personal API tokens for programmatic pipelines
- [x] Document versioning with content hashes
- [x] JSON export per job
- [x] VL connections usable as normal processing profiles, not just benchmark participants
- [x] Confluence import hierarchy metadata (breadcrumbs, navigation flags, hierarchy-as-tags) for RAG chunking
- [ ] Improve batch progress and operator feedback UX
- [ ] Add vector DB export/webhook integrations
