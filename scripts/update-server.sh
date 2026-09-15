#!/usr/bin/env bash
# Update a server installed with install-server.sh: pull the latest code, rebuild the api image from it, restart, keep data.
#   bash /opt/cimage-ai/scripts/update-server.sh            # build from source (default: always the code in git)
#   PULL_IMAGE=1 bash /opt/cimage-ai/scripts/update-server.sh   # use the published Docker Hub image instead
set -euo pipefail
cd "${INSTALL_DIR:-/opt/cimage-ai}"
SUDO=""; docker ps >/dev/null 2>&1 || SUDO="sudo"
say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
before="$(git rev-parse --short HEAD)"
git pull --ff-only
export GIT_SHA="$(git rev-parse --short HEAD)" BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
say "code: $before -> $GIT_SHA"
$SUDO docker compose pull -q postgres redis ollama 2>/dev/null || true
if [ "${PULL_IMAGE:-0}" = "1" ]; then
  say "Pulling the published api image"
  $SUDO docker compose pull -q api
else
  say "Building the api image from source at $GIT_SHA"
  $SUDO docker compose build --pull api
fi
$SUDO docker compose up -d --remove-orphans      # migrations run on startup; volumes untouched
$SUDO docker image prune -f >/dev/null 2>&1 || true
for _ in $(seq 1 40); do curl -fsS http://localhost:8000/health >/dev/null 2>&1 && break; sleep 3; done
running="$(curl -fsS http://localhost:8000/api/v1/system 2>/dev/null | sed -n 's/.*"git_sha": *"\([^"]*\)".*/\1/p')"
say "running build: ${running:-not healthy yet}  (expected $GIT_SHA)"
$SUDO docker compose ps
