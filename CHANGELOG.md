# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
