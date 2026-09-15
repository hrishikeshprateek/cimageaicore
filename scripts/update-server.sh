#!/usr/bin/env bash
# Update a server installed with install-server.sh to the latest release: pull code + images, restart, keep data.
set -euo pipefail
cd "${INSTALL_DIR:-/opt/cimage-ai}"
SUDO=""; docker ps >/dev/null 2>&1 || SUDO="sudo"
git pull --ff-only
$SUDO docker compose pull -q
$SUDO docker compose up -d --remove-orphans      # migrations run on startup; volumes untouched
$SUDO docker compose ps
