#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
FRONTEND_DIR="$REPO_ROOT/frontend"

PY_BASELINE="3.12.9"
PY_ALT="3.14.3"
NODE_BASELINE="22"
NODE_ALT="26"

RECREATE_VENV=false
WITH_DOCKER=false
SETUP_ONLY=false
ALLOW_CHECK_FAILURES=false

BACKEND_CHECK_FAILED=false
FRONTEND_CHECK_FAILED=false

for arg in "$@"; do
  case "$arg" in
    --recreate-venv)
      RECREATE_VENV=true
      ;;
    --with-docker)
      WITH_DOCKER=true
      ;;
    --setup-only)
      SETUP_ONLY=true
      ;;
    --allow-check-failures)
      ALLOW_CHECK_FAILURES=true
      ;;
    *)
      echo "Unknown option: $arg"
      echo "Usage: $0 [--recreate-venv] [--with-docker] [--setup-only] [--allow-check-failures]"
      exit 1
      ;;
  esac
done

log() {
  printf "\n[bootstrap] %s\n" "$1"
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1"
    exit 1
  fi
}

append_if_missing() {
  local line="$1"
  local file="$2"
  grep -Fqx "$line" "$file" 2>/dev/null || printf "%s\n" "$line" >> "$file"
}

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script is intended for macOS. Current OS: $(uname -s)"
  exit 1
fi

need_cmd xcode-select
xcode-select --install >/dev/null 2>&1 || true

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required but not installed."
  echo "Install from https://brew.sh and rerun this script."
  exit 1
fi

log "Installing required CLI tools via Homebrew"
brew update
brew install pyenv nvm jq gh

log "Configuring shell init for pyenv and nvm"
ZSHRC="$HOME/.zshrc"
touch "$ZSHRC"
append_if_missing 'export PYENV_ROOT="$HOME/.pyenv"' "$ZSHRC"
append_if_missing 'command -v pyenv >/dev/null || export PATH="$PYENV_ROOT/bin:$PATH"' "$ZSHRC"
append_if_missing 'eval "$(pyenv init -)"' "$ZSHRC"
append_if_missing 'export NVM_DIR="$HOME/.nvm"' "$ZSHRC"
append_if_missing '[ -s "/opt/homebrew/opt/nvm/nvm.sh" ] && . "/opt/homebrew/opt/nvm/nvm.sh"' "$ZSHRC"
append_if_missing '[ -s "/usr/local/opt/nvm/nvm.sh" ] && . "/usr/local/opt/nvm/nvm.sh"' "$ZSHRC"

export PYENV_ROOT="$HOME/.pyenv"
if [[ -d "$PYENV_ROOT/bin" ]]; then
  export PATH="$PYENV_ROOT/bin:$PATH"
fi
eval "$(pyenv init -)"

export NVM_DIR="$HOME/.nvm"
mkdir -p "$NVM_DIR"
if [[ -s "/opt/homebrew/opt/nvm/nvm.sh" ]]; then
  . "/opt/homebrew/opt/nvm/nvm.sh"
elif [[ -s "/usr/local/opt/nvm/nvm.sh" ]]; then
  . "/usr/local/opt/nvm/nvm.sh"
else
  echo "nvm.sh not found after installation."
  exit 1
fi

log "Installing Python runtimes"
pyenv install -s "$PY_BASELINE"
pyenv install -s "$PY_ALT"

log "Installing Node runtimes"
nvm install "$NODE_BASELINE"
nvm install "$NODE_ALT"

log "Preparing backend baseline environment (Python $PY_BASELINE)"
cd "$BACKEND_DIR"
pyenv local "$PY_BASELINE"
if [[ "$RECREATE_VENV" == "true" && -d .venv ]]; then
  rm -rf .venv
fi
if [[ ! -d .venv ]]; then
  python -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate
python -m pip install -U pip setuptools wheel
pip install -r requirements.txt

if [[ "$SETUP_ONLY" == "false" ]]; then
  if ! python -m pytest -q; then
    BACKEND_CHECK_FAILED=true
  fi
fi

log "Preparing frontend baseline environment (Node $NODE_BASELINE)"
cd "$FRONTEND_DIR"
nvm use "$NODE_BASELINE"
npm ci

if [[ "$SETUP_ONLY" == "false" ]]; then
  if ! npm run lint; then
    FRONTEND_CHECK_FAILED=true
  fi
  if ! npm run build; then
    FRONTEND_CHECK_FAILED=true
  fi
fi

if [[ "$WITH_DOCKER" == "true" ]]; then
  need_cmd docker
  log "Starting local docker services"
  cd "$REPO_ROOT"
  docker compose up -d postgres redis
  docker compose up --build -d backend worker frontend
  docker compose ps
fi

log "Done"
echo "Tip: run './scripts/bootstrap_local_dev.sh --with-docker' to include docker services."

if [[ "$SETUP_ONLY" == "true" ]]; then
  echo "Checks skipped (--setup-only)."
  exit 0
fi

if [[ "$BACKEND_CHECK_FAILED" == "true" || "$FRONTEND_CHECK_FAILED" == "true" ]]; then
  echo "One or more checks failed."
  echo "- Backend tests failed: $BACKEND_CHECK_FAILED"
  echo "- Frontend checks failed: $FRONTEND_CHECK_FAILED"
  if [[ "$ALLOW_CHECK_FAILURES" == "true" ]]; then
    echo "Continuing because --allow-check-failures was provided."
    exit 0
  fi
  echo "Re-run with --allow-check-failures to complete setup without failing the script."
  exit 1
fi
