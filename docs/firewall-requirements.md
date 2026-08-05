# Firewall / Network Requirements

Connection matrix for PaddleDoc deployments (Docker Compose and Kubernetes/Helm).
All rows are TCP. "Required" means the app cannot operate without it; "optional"
rows depend on the listed feature being used.

An important architectural note up front: **the browser talks to the backend
API directly** — the frontend container does not proxy API requests. Users
therefore need network access to *both* the frontend and the backend (or to
both ingress hosts).

## Frontend (Next.js)

| Direction | Peer | Port | Purpose | Needed |
|---|---|---|---|---|
| Inbound | User browsers / ingress controller | 3000 (or 443 via ingress) | Web UI | Required |
| Outbound | — | — | None. The frontend serves static/SSR content only; API calls originate from the browser. | — |

## Backend (FastAPI)

| Direction | Peer | Port | Purpose | Needed |
|---|---|---|---|---|
| Inbound | User browsers / ingress controller | 8000 (or 443 via ingress) | REST API (UI, session auth) | Required |
| Inbound | API clients (n8n, scripts) | 8000 (or 443 via ingress) | REST API (session auth) | Optional |
| Outbound | PostgreSQL | 5432 | Jobs, users, sessions, results | Required |
| Outbound | Redis | 6379 | Celery broker/results, runtime settings | Required |
| Outbound | OIDC identity provider (e.g. `login.microsoftonline.com` for Entra ID, or your Keycloak host) | 443 | OIDC discovery, JWKS, token exchange | Optional — only when OIDC login is configured |
| Outbound | Confluence (`*.atlassian.net` for Cloud, or internal Server/DC `host:port`) | 443 / custom | Import source validation and synchronous test probes | Optional — Confluence import |

## Worker (Celery)

No inbound connections — the worker listens on no port.

| Direction | Peer | Port | Purpose | Needed |
|---|---|---|---|---|
| Outbound | Redis | 6379 | Celery broker/results, runtime settings | Required |
| Outbound | PostgreSQL | 5432 | Job state, results | Required |
| Outbound | PaddleOCR model hosts: `*.bcebos.com` (default BOS source, e.g. `paddle-model-ecology.bj.bcebos.com`); `huggingface.co` + `cdn-lfs*.huggingface.co` when `PADDLE_PDX_MODEL_SOURCE=HuggingFace` | 443 | Runtime download of OCR model weights on first use / cold cache | Required for `ppocrv6_*`/`paddlevl` profiles unless the model cache is pre-warmed (persistent PVC / compose volume) or models are baked into the image. Without egress, jobs silently degrade to the plain-text fallback (no OCR). |
| Outbound | Confluence (`*.atlassian.net` for Cloud, or internal Server/DC `host:port`) | 443 / custom | Import crawl: page + attachment downloads run on the worker | Optional — Confluence import |
| Outbound | OpenAI-compatible vision endpoint (host from `OPENAI_API_BASE_URL`) | 443 / custom | `openai_vision` profile sends page images for OCR | Optional — only with the `openai_vision` profile |

## PostgreSQL

| Direction | Peer | Port | Purpose | Needed |
|---|---|---|---|---|
| Inbound | Backend, Worker | 5432 | Application database | Required |
| Inbound | Migration job (Kubernetes only; runs the backend image) | 5432 | Alembic migrations | Required (K8s with `migrationJob.enabled`) |
| Outbound | — | — | None | — |

## Redis

| Direction | Peer | Port | Purpose | Needed |
|---|---|---|---|---|
| Inbound | Backend, Worker | 6379 | Celery broker/result backend, runtime settings, locks | Required |
| Outbound | — | — | None | — |

## Deployment-specific notes

**Kubernetes / Helm chart**

- Only the frontend and backend Services need to be reachable from outside the
  cluster (via their ingresses, typically 443). Redis (bundled) and the model
  cache stay cluster-internal.
- With external PostgreSQL (the only mode the chart supports), backend, worker,
  and migration-job pods need egress to that database host on 5432.
- If worker egress to the model hosts is blocked, either allow the hosts above,
  route through a proxy (`worker.extraEnv`: `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`),
  switch the source (`PADDLE_PDX_MODEL_SOURCE`), and/or persist the cache
  (`worker.modelCache.persistence`) so the download happens once.
- Cluster nodes additionally need registry access to pull images
  (`ghcr.io` + `pkg-containers.githubusercontent.com`, 443) — usually covered
  by cluster-level infrastructure rules.

**Docker Compose (NAS: `docker-compose.nas.yml`)**

- Published host ports: `3000` (frontend) and `8000` (backend) only. PostgreSQL
  and Redis stay on the internal compose network and must not be exposed.
- The worker persists models in `./nas-data/paddlex_models`, so model-host
  egress is only needed until the cache is populated.

**Docker Compose (development: `docker-compose.yml`)**

- Additionally publishes `5432` and `6379` to the host for local development
  convenience. Do not use this file on a reachable network without firewalling
  those ports.
