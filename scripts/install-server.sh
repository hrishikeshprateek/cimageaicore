#!/usr/bin/env bash
# CIMAGE AI Media Platform - one-shot server install (Ubuntu/Debian, x86-64 or arm64).
#
#   curl -fsSL https://raw.githubusercontent.com/hrishikeshprateek/cimageaicore/main/scripts/install-server.sh | bash
#   (or: git clone the repo and run  bash scripts/install-server.sh)
#
# What it does (idempotent - safe to re-run):
#   1. installs Docker Engine + compose plugin if missing, enables it on boot
#   2. clones / updates the repo into $INSTALL_DIR (default /opt/cimage-ai)
#   3. creates .env from .env.example on first run and fills the keys that matter
#   4. installs a systemd unit so `docker compose up -d` runs at every boot (after the NAS mount)
#   5. starts the stack: api + postgres/pgvector + redis + ollama (+ pulls the embedding model)
#
# Environment variables you can pass instead of answering prompts:
#   GEMINI_API_KEY, NAS_WATCH_DIR (default /mnt/nas), INSTALL_DIR, REPO_URL, GIT_REF (default main)
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/hrishikeshprateek/cimageaicore.git}"
GIT_REF="${GIT_REF:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/cimage-ai}"
SERVICE_NAME="cimage-ai"
say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Linux" ] || die "this installer is for Linux servers (on Windows use Docker Desktop + WSL2, see docs/DEPLOY.md)"
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
command -v sudo >/dev/null || [ -z "$SUDO" ] || die "sudo is required"
ME="${USER:-$(id -un)}"

# ---------------------------------------------------------------- 1. Docker
say "Installing prerequisites (curl, git, openssl, cifs-utils)"
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq ca-certificates curl git openssl cifs-utils >/dev/null
if ! command -v docker >/dev/null 2>&1; then
  say "Installing Docker Engine"
  curl -fsSL https://get.docker.com | $SUDO sh
else
  say "Docker already installed: $(docker --version)"
fi
docker compose version >/dev/null 2>&1 || $SUDO docker compose version >/dev/null 2>&1 || die "docker compose plugin missing (apt-get install docker-compose-plugin)"
$SUDO systemctl enable --now docker >/dev/null 2>&1 || true
if [ -n "$SUDO" ] && ! id -nG "$ME" | grep -qw docker; then
  $SUDO usermod -aG docker "$ME" && say "added $ME to the docker group (takes effect at your next login)"
fi
DOCKER="$SUDO docker"

# ---------------------------------------------------------------- 2. repo
# the checkout is owned by the installing user (git refuses to touch repos owned by someone else)
if [ -d "$INSTALL_DIR/.git" ]; then
  say "Updating $INSTALL_DIR"
  $SUDO chown -R "$ME":"$ME" "$INSTALL_DIR"
  git -C "$INSTALL_DIR" fetch -q origin && git -C "$INSTALL_DIR" checkout -q "$GIT_REF" && git -C "$INSTALL_DIR" pull -q --ff-only origin "$GIT_REF"
else
  say "Cloning into $INSTALL_DIR"
  $SUDO mkdir -p "$INSTALL_DIR" && $SUDO chown "$ME":"$ME" "$INSTALL_DIR"
  git clone -q --branch "$GIT_REF" "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"
mkdir -p data/composer/templates data/renders data/images data/nas-test/AI-Test

# ---------------------------------------------------------------- 3. .env
set_env() {  # set_env KEY VALUE  (replaces the line or appends)
  local key="$1" val="$2"
  if grep -qE "^${key}=" .env; then
    sed -i "s|^${key}=.*|${key}=${val}|" .env
  else
    printf '%s=%s\n' "$key" "$val" >> .env
  fi
}
if [ ! -f .env ]; then
  say "Creating .env"
  cp .env.example .env
  key="${GEMINI_API_KEY:-}"
  if [ -z "$key" ] && [ -t 0 ]; then read -r -p "Gemini API key (authorization key from AI Studio; blank = mock provider for now): " key; fi
  set_env GEMINI_API_KEY "$key"
  nas="${NAS_WATCH_DIR:-}"
  if [ -z "$nas" ] && [ -t 0 ]; then read -r -p "Host folder where the NAS is mounted [/mnt/nas] (blank = local ./data/nas-test for testing): " nas; fi
  set_env NAS_WATCH_DIR "${nas:-./data/nas-test}"
  set_env COMPOSER_ENABLED true
  # Fernet key for the WordPress application passwords: 32 random bytes, url-safe base64
  set_env PUBLISH_SECRET_KEY "$(openssl rand -base64 32 | tr '+/' '-_')"
  say ".env written - edit later with: nano $INSTALL_DIR/.env  (then: docker compose up -d)"
else
  say ".env exists - keeping it"
  grep -qE "^PUBLISH_SECRET_KEY=.+" .env || set_env PUBLISH_SECRET_KEY "$(openssl rand -base64 32 | tr '+/' '-_')"
fi
chmod 600 .env
nas_dir="$(grep -E '^NAS_WATCH_DIR=' .env | cut -d= -f2-)"
if [ -n "$nas_dir" ] && [ "${nas_dir#./}" = "$nas_dir" ] && [ ! -d "$nas_dir" ]; then
  say "NOTE: $nas_dir does not exist yet - mount the NAS there (see docs/DEPLOY.md, /etc/fstab) and create $nas_dir/AI-Test"
  mkdir -p "$nas_dir" 2>/dev/null || $SUDO mkdir -p "$nas_dir"
fi

# ---------------------------------------------------------------- 4. systemd: start on boot
say "Installing systemd unit $SERVICE_NAME"
$SUDO tee /etc/systemd/system/${SERVICE_NAME}.service >/dev/null <<UNIT
[Unit]
Description=CIMAGE AI Media Platform (docker compose stack)
Requires=docker.service
After=docker.service network-online.target remote-fs.target
Wants=network-online.target remote-fs.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${INSTALL_DIR}
ExecStart=/usr/bin/docker compose up -d --remove-orphans
ExecStop=/usr/bin/docker compose stop
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
UNIT
$SUDO systemctl daemon-reload
$SUDO systemctl enable ${SERVICE_NAME} >/dev/null
$SUDO systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1 || true

# ---------------------------------------------------------------- 5. start
say "Pulling images and starting the stack (first run downloads ~1.5 GB incl. the embedding model)"
$DOCKER compose pull -q
$DOCKER compose up -d --remove-orphans
say "Waiting for the API to become healthy"
for _ in $(seq 1 60); do
  if curl -fsS http://localhost:8000/health >/dev/null 2>&1; then break; fi
  sleep 5
done
if curl -fsS http://localhost:8000/api/v1/system 2>/dev/null | grep -q '"store": *"postgres"'; then
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  say "Up. Admin: http://${ip:-<server-ip>}:8000/admin"
  $DOCKER compose ps
  say "The embedding model downloads in the background (docker compose logs ollama-pull). Search is keyword-only until it finishes."
else
  say "The API is not healthy yet. Check:  docker compose ps   and   docker compose logs -f api"
fi
