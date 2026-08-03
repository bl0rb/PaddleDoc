#!/bin/sh
set -e

db_target=$(python - <<'PY'
import sys

from sqlalchemy.engine import make_url

from app.core.config import settings

try:
    url = make_url(settings.database_url)
except Exception as e:
    print(f"WARN: could not parse DATABASE_URL ({e}); skipping postgres wait.", file=sys.stderr)
    sys.exit(0)

if not url.host:
    print("WARN: DATABASE_URL has no host (e.g. sqlite); skipping postgres wait.", file=sys.stderr)
    sys.exit(0)

print(f"{url.host} {url.port or 5432}")
PY
)

if [ -n "$db_target" ]; then
  db_host=$(echo "$db_target" | cut -d' ' -f1)
  db_port=$(echo "$db_target" | cut -d' ' -f2)
  echo "Waiting for postgres at $db_host:$db_port..."
  until python - "$db_host" "$db_port" <<'PY'
import sys, socket
host = sys.argv[1]
port = int(sys.argv[2])
try:
    s = socket.create_connection((host, port), timeout=2)
    s.close()
except Exception as e:
    print(f"Not ready: {e}", file=sys.stderr)
    sys.exit(1)
PY
  do
    sleep 1
  done
  echo "Postgres is ready."
fi

if [ "${RUN_ALEMBIC_ON_STARTUP:-true}" = "true" ]; then
  # If alembic_version table doesn't exist but the jobs table does, the database
  # was created before Alembic was introduced. Stamp to the initial Alembic
  # revision, then run upgrade head so all idempotent migrations are executed.
  set +e
  python - <<'PY'
import os, sys
import sqlalchemy as sa
from app.core.config import settings

url = settings.database_url
engine = sa.create_engine(url)
insp = sa.inspect(engine)
has_version_table = insp.has_table("alembic_version")
has_jobs_table = insp.has_table("jobs")
if not has_version_table and has_jobs_table:
    print("Pre-Alembic database detected — stamping to current head.", flush=True)
    sys.exit(2)
sys.exit(0)
PY
  exit_code=$?
  set -e
  if [ "$exit_code" = "2" ]; then
    alembic stamp 0001_init
  fi

  echo "Running alembic migrations..."
  alembic upgrade head

  # Safety net for databases that were previously stamped to head before
  # password_hash migration could run.
  python - <<'PY'
import os
import sqlalchemy as sa
from sqlalchemy import text
from app.core.config import settings

url = settings.database_url
engine = sa.create_engine(url)
insp = sa.inspect(engine)
if insp.has_table("jobs"):
    cols = {c["name"] for c in insp.get_columns("jobs")}
    if "password_hash" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS password_hash varchar(255)"))
        print("Applied schema self-heal: added jobs.password_hash", flush=True)
PY
else
  echo "Skipping Alembic migrations at startup (RUN_ALEMBIC_ON_STARTUP=false)."
fi

echo "Starting server..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
