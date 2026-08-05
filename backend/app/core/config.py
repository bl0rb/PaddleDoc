from pathlib import Path
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    app_name: str = 'paddledoc API'
    database_url: str = ''
    postgres_host: str = ''
    postgres_port: int = 5432
    postgres_db: str = ''
    postgres_user: str = ''
    postgres_password: str = ''
    redis_url: str = 'redis://redis:6379/0'
    cors_origins: list[str] = ['http://localhost:3000']
    max_upload_bytes: int = 100 * 1024 * 1024
    rate_limit_per_minute: int = 60
    # Reverse-proxy trust for X-Forwarded-For/X-Real-IP (used to key the
    # rate limiter -- see app/services/security.py:_client_id_from_request).
    # Those headers are attacker-controlled unless the direct TCP peer is
    # itself one of our own proxies, so they're only honored when the peer
    # matches an entry here. Individual IPs or CIDR ranges (e.g. the
    # in-cluster pod CIDR an ingress-controller/LB connects from). Empty
    # (the default) means never trust the headers -- the limiter falls back
    # to keying on the direct TCP peer, which is safe but coarse (shared
    # bucket per proxy) until this is configured for the deployment.
    trusted_proxy_ips: list[str] = []
    # How many trusted-proxy hops are expected to have appended to
    # X-Forwarded-For (e.g. 1 for a single ingress-controller/ALB in front
    # of the pod). The limiter reads the hop this many positions in from
    # the right of the comma-separated chain, not the leftmost (client-
    # supplied, spoofable) entry.
    trusted_proxy_hops: int = 1
    uploads_dir: Path = Path('backend/storage/uploads')
    results_dir: Path = Path('backend/storage/results')
    paddle_default_profile: str = 'ppocrv6_tiny'
    paddle_timeout_seconds: int = 300
    worker_concurrency: int = 1

    # Celery task hard/soft time limits. Long OCR jobs on CPU can legitimately
    # run for many minutes, but a hung/stuck task (e.g. a wedged onnxruntime
    # call) should eventually be killed rather than block a worker slot
    # forever. Soft raises SoftTimeLimitExceeded inside the task (catchable
    # for cleanup); hard SIGKILLs the worker child. Defaults: 25min/30min.
    celery_task_soft_time_limit_seconds: int = 1500
    celery_task_time_limit_seconds: int = 1800
    # Redis broker visibility_timeout: how long a task can be "invisible"
    # (claimed by a worker) before Redis assumes the worker died and
    # redelivers it to another worker. Must be >= celery_task_time_limit_seconds
    # -- otherwise a still-running long OCR task gets redelivered and
    # processed a second time before the first attempt's hard limit even
    # fires. celery_app.py also clamps this defensively at startup.
    celery_broker_visibility_timeout_seconds: int = 1800
    openai_api_base_url: str = ''
    openai_api_bearer_token: str = ''

    # Session-cookie signing, OIDC state HMAC, and the key material Fernet
    # client-secret encryption is derived from (see app/services/security.py).
    # Required in any real (postgres) deployment -- see
    # _resolve_secret_key below, which fails fast rather than silently
    # running with a guessable key.
    secret_key: str = ''
    # Base URL the API is publicly reachable at; used to build the OIDC
    # redirect_uri (`{public_api_url}/api/v1/auth/oidc/{slug}/callback`).
    public_api_url: str = 'http://localhost:8000'

    # --- Confluence import (app/services/confluence*.py, safe_fetch,
    # app/workers/import_tasks.py). The caps are hard server-side clamps:
    # client-supplied values can only lower them, never raise them.
    # Kill-switch: when False the /import API surface returns 404s.
    import_enabled: bool = True
    # Hostnames ('host' or 'host:port') of private-network Confluence servers
    # outbound import fetches may reach. Passed into safe_fetch as
    # allowed_private_hosts and enforced per redirect hop; cloud-metadata IPs
    # remain blocked unconditionally, allowlist or not. JSON list env value
    # (IMPORT_PRIVATE_HOST_ALLOWLIST), same parsing as cors_origins.
    import_private_host_allowlist: list[str] = []
    import_max_pages: int = 200
    import_max_depth: int = 10
    # Pages processed per chunked task execution before re-enqueueing, so
    # queued OCR jobs can interleave with a long crawl.
    import_chunk_pages: int = 25
    import_fetch_timeout_seconds: int = 30
    # safe_fetch max_bytes for JSON/HTML responses.
    import_fetch_max_bytes: int = 5 * 1024 * 1024
    # Per-attachment download cap; larger attachments are skipped, not fatal.
    import_attachment_max_bytes: int = 20 * 1024 * 1024
    # Cap on artifact_bytes + content_bytes accumulated by a single run
    # (stored page HTML counts too); reaching it ends discovery gracefully.
    import_run_max_total_bytes: int = 500 * 1024 * 1024
    import_max_active_runs_per_user: int = 1
    # A 'running' run whose updated_at is older than this is stale (worker
    # lost) -- drives the lease reclaim, requeue-on-worker-ready, force-cancel
    # and active-run-cap reaping paths.
    import_stale_run_seconds: int = 600
    # DB-backed cooldown between /import/sources/{id}/test probes (429 inside
    # the window); holds even when the Redis rate limiter fails open.
    import_test_cooldown_seconds: int = 10
    # Per-process asyncio semaphore capping concurrent outbound test probes.
    import_probe_concurrency: int = 4


def _build_database_url(settings: Settings) -> str:
    if settings.database_url:
        return settings.database_url

    if settings.postgres_host and settings.postgres_db and settings.postgres_user:
        user = quote_plus(settings.postgres_user)
        password = quote_plus(settings.postgres_password)
        db = quote_plus(settings.postgres_db)
        if settings.postgres_password:
            auth = f'{user}:{password}'
        else:
            auth = user
        return f'postgresql+psycopg://{auth}@{settings.postgres_host}:{settings.postgres_port}/{db}'

    return 'sqlite:///./paddledoc.db'


# NOT a real secret -- deterministic placeholder so sqlite-backed local dev
# and the pytest suite work with zero setup. Never used when database_url
# resolves to postgres (see _resolve_secret_key).
_DEV_ONLY_SQLITE_SECRET_KEY = 'dev-only-insecure-secret-key-do-not-use-in-production'


def _resolve_secret_key(settings: Settings) -> str:
    if settings.secret_key:
        return settings.secret_key
    if settings.database_url.startswith('sqlite'):
        return _DEV_ONLY_SQLITE_SECRET_KEY
    raise RuntimeError(
        'SECRET_KEY is required when database_url is not sqlite (i.e. any '
        'real, multi-user deployment). It signs session cookies and the '
        'OIDC state cookie, and client secrets are encrypted with a key '
        'derived from it -- set SECRET_KEY via env/secret before startup.'
    )


settings = Settings()
settings.database_url = _build_database_url(settings)
settings.secret_key = _resolve_secret_key(settings)
