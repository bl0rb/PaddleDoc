#!/usr/bin/env bash
# Creates a .env file with freshly generated secrets next to the compose files.
#
# The compose files deliberately have no fallback values for SECRET_KEY,
# POSTGRES_PASSWORD and REDIS_PASSWORD: a default that ships in the
# repository is a published secret, and SECRET_KEY in particular is the key
# every stored OIDC client secret, Confluence credential and VL API key is
# encrypted under (see backend/app/services/security.py). Anyone with a copy
# of the repo plus a copy of the database could otherwise decrypt all of
# them. Existing values in .env are never overwritten.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${1:-$REPO_ROOT/.env}"

if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl is required to generate secrets" >&2
  exit 1
fi

random_hex() { openssl rand -hex 32; }

if [ -f "$ENV_FILE" ]; then
  echo "[init-env] $ENV_FILE exists — filling in only the missing keys"
else
  echo "[init-env] creating $ENV_FILE"
  : > "$ENV_FILE"
fi

ensure_key() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    echo "[init-env]   $key already set — kept"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    echo "[init-env]   $key generated"
  fi
}

ensure_key SECRET_KEY "$(random_hex)"
ensure_key POSTGRES_PASSWORD "$(random_hex)"
ensure_key REDIS_PASSWORD "$(random_hex)"

chmod 600 "$ENV_FILE" 2>/dev/null || true

echo "[init-env] done — $ENV_FILE"
echo "[init-env] Note: changing SECRET_KEY later invalidates every stored"
echo "[init-env] OIDC client secret, import credential and VL API key."
