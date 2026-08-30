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

EXAMPLE_FILE="$REPO_ROOT/.env.example"

value_of() { sed -n "s/^$1=//p" "$2" | head -n 1; }

replace_key() {
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  KEY="$key" VALUE="$value" awk '
    BEGIN { k = ENVIRON["KEY"]; v = ENVIRON["VALUE"]; done = 0 }
    !done && index($0, k "=") == 1 { print k "=" v; done = 1; next }
    { print }
  ' "$ENV_FILE" > "$tmp"
  cat "$tmp" > "$ENV_FILE"
  rm -f "$tmp"
}

# A .env copied from .env.example has every key present, so "is the key
# there?" is the wrong question -- it kept SECRET_KEY and REDIS_PASSWORD at
# values published in this repository while reporting success. A value still
# identical to the one in .env.example is a published secret, not a choice,
# so it counts as missing. Comparing against the file rather than a hardcoded
# list keeps this correct when the examples change.
ensure_key() {
  local key="$1" value="$2" current example
  if ! grep -qE "^${key}=" "$ENV_FILE"; then
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    echo "[init-env]   $key generated"
    return
  fi
  current="$(value_of "$key" "$ENV_FILE")"
  example=""
  [ -f "$EXAMPLE_FILE" ] && example="$(value_of "$key" "$EXAMPLE_FILE")"
  if [ -z "$current" ] || { [ -n "$example" ] && [ "$current" = "$example" ]; }; then
    replace_key "$key" "$value"
    echo "[init-env]   $key was empty or the .env.example placeholder — regenerated"
  else
    echo "[init-env]   $key already set — kept"
  fi
}

ensure_key SECRET_KEY "$(random_hex)"
ensure_key POSTGRES_PASSWORD "$(random_hex)"
ensure_key REDIS_PASSWORD "$(random_hex)"

chmod 600 "$ENV_FILE" 2>/dev/null || true

echo "[init-env] done — $ENV_FILE"
echo "[init-env] Note: changing SECRET_KEY later invalidates every stored"
echo "[init-env] OIDC client secret, import credential and VL API key."
