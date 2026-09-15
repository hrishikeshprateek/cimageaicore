# Deploying the CIMAGE AI Media Platform

One Docker Compose stack runs everything: the API (+ folder watcher, workers, admin panel), PostgreSQL/pgvector,
Redis and Ollama (local embeddings). Gemini is the only external call (video analysis + writing).

```
NAS share ──(read-only mount)──▶ watcher ─▶ Gemini analysis ─▶ knowledge blocks ─▶ local embeddings (Ollama)
                                                                       │
                                                       opportunities ─▶ auto blog draft ─▶ /admin review ─▶ approve
```

## 1. Server

Recommended: **Ubuntu 24.04 LTS** (dedicated box — not dual-boot; the service must be up while Windows isn't).
Windows works via Docker Desktop + WSL2 but needs a signed-in user for Docker Desktop; see the notes at the end.

```bash
# Docker Engine + compose plugin (Ubuntu)
sudo apt-get update && sudo apt-get install -y ca-certificates curl git cifs-utils
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker

# never sleep
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

Mount the college NAS read-only (one line in `/etc/fstab`; `nofail` so a NAS outage never blocks boot):

```
//192.168.1.50/home  /mnt/nas  cifs  credentials=/etc/nas.cred,ro,uid=1000,gid=1000,_netdev,nofail,iocharset=utf8,vers=3.0  0  0
```
(the CIMAGE NAS is `digital.local` = 192.168.1.50, share `home`; use the IP in fstab — `.local` names need avahi/mDNS on Ubuntu Server)

`/etc/nas.cred` holds `username=` / `password=` of a **read-only** NAS account (`chmod 600`). The watcher scans exactly the
folder you put in `NAS_WATCH_DIR` (e.g. `NAS_WATCH_DIR="/mnt/nas/Cimage AI Agent"`), never the whole NAS — that folder is
mounted at `/nas` inside the container.

## 2. One-shot install (recommended)

On a fresh Ubuntu/Debian box, as a user with sudo:

```bash
GEMINI_API_KEY=... NAS_WATCH_DIR=/mnt/nas bash -c "$(curl -fsSL https://raw.githubusercontent.com/hrishikeshprateek/cimageaicore/main/scripts/install-server.sh)"
```

`scripts/install-server.sh` installs Docker if needed, clones the repo into `/opt/cimage-ai`, writes `.env` (Gemini key,
NAS folder, `COMPOSER_ENABLED=true`, a generated `PUBLISH_SECRET_KEY`), installs a **systemd unit (`cimage-ai.service`)
that runs `docker compose up -d` at every boot after the NAS mount**, disables sleep, **builds the api image from the
checked-out source** (3–6 min the first time) and starts the stack. Re-running it is safe (it updates the checkout, keeps
`.env`, rebuilds). Later updates: `bash /opt/cimage-ai/scripts/update-server.sh` — pulls `main`, rebuilds, restarts.

Building on the server means it always runs *exactly* the code on `main`; nothing depends on someone pushing an image
from a laptop. To use the published Docker Hub image instead (slow CPU, or no build tools): `PULL_IMAGE=1` on either script.

**Which build is running?** `curl -s localhost:8000/api/v1/system` shows `"build": {"git_sha": …}`, and the admin
sidebar footer shows the same short SHA — compare it with `git -C /opt/cimage-ai rev-parse --short HEAD`.

Useful afterwards: `systemctl status cimage-ai` · `docker compose -f /opt/cimage-ai/docker-compose.yml ps` ·
`docker compose logs -f api` · `nano /opt/cimage-ai/.env && docker compose up -d`.

## 2a. Manual — pull from Docker Hub (no build on the server)

Every release is published as `hrishikeshprateek/cimage-ai-api:<version>` (and `:latest`). The compose file already
points at the current release, so a server only needs the repo's config files, not a Python toolchain:

```bash
git clone <repo> cimage-ai && cd cimage-ai        # or copy docker-compose.yml + .env.example + database/ + data/composer/
cp .env.example .env && nano .env                 # GEMINI_API_KEY, NAS_WATCH_DIR, COMPOSER_ENABLED=true, PUBLISH_SECRET_KEY
docker compose up -d                              # pulls api (≈650 MB, linux/amd64), pgvector, redis, ollama; the embedding model is pulled by `ollama-pull`
docker compose ps                                 # wait until api is "healthy" and ollama-pull has exited (0)
curl -s localhost:8000/api/v1/system              # expect "store":"postgres" and "embedder":"ollama"
```

The compose file waits for Postgres, Redis and Ollama to be **healthy** before starting the API (a fresh database takes
a few seconds to initialise; without the wait the API would fall back to JSON files). Postgres, Redis and Ollama listen
on **localhost only**; the only LAN port is 8000. Templates (the real frame PNGs), renders and images live under `./data`
(mounted at `/data`), so `docker compose down && up` keeps them.

Moving an existing installation (e.g. from the dev Mac): `docker compose exec postgres pg_dump -U cimage cimage_ai > cimage.sql`
on the old box, copy `cimage.sql` + the `data/` folder to the server, then on the server after the first `up`:
`docker compose exec -T postgres psql -U cimage cimage_ai < cimage.sql`. Use the **same** `PUBLISH_SECRET_KEY`, or re-enter the
WordPress application passwords in Admin → Publishing.

Upgrade to a newer release: change the tag in `docker-compose.yml` (or `export API_IMAGE=hrishikeshprateek/cimage-ai-api:0.10.0`),
then `docker compose pull && docker compose up -d` — migrations run on startup, data volumes are untouched.

No internet on the server? `dist/cimage-ai-api-<version>.tar.gz` (made with `docker save`) can be copied over and loaded with
`docker load < cimage-ai-api-<version>.tar.gz`; the tag inside matches the compose file.

**Automatic image publishing**: `.github/workflows/docker-image.yml` builds a multi-arch (amd64 + arm64) image on every
push to `main` and on `v*` tags and pushes `:latest`, `:<version>` and `:sha-<commit>` to Docker Hub. One-time setup in the
GitHub repo: *Settings → Secrets and variables → Actions* → `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (an access token with
Read & Write). Until those secrets exist the workflow fails harmlessly and servers keep building from source.

Publishing a release by hand from a dev machine (maintainers):

```bash
# multi-arch: the production box is x86-64 (AMD), the dev Mac is arm64 - a plain `docker build` on the Mac gives an arm64-only image
docker buildx build --platform linux/amd64,linux/arm64 -f docker/api.Dockerfile \
  -t hrishikeshprateek/cimage-ai-api:0.9.2 -t hrishikeshprateek/cimage-ai-api:latest --push .
docker manifest inspect hrishikeshprateek/cimage-ai-api:0.9.2 | grep architecture     # expect amd64 + arm64
# offline copy (Docker Desktop's containerd store saves BOTH platforms into one tarball; `docker load` picks the server's):
docker save hrishikeshprateek/cimage-ai-api:0.9.2 | gzip -1 > dist/cimage-ai-api-0.9.0.tar.gz     # ≈ 650 MB
# note: dist/cimage-ai-api-0.8.0.tar.gz and the 0.8.0 tag on Docker Hub are arm64-only and will not run on the AMD server
```

## 2b. App — build from source

```bash
git clone <repo> cimage-ai && cd cimage-ai
cp .env.example .env
```

Edit `.env` — the lines that matter on a server:

```
GEMINI_API_KEY=...                 # paid Google Cloud project, dedicated key
NAS_WATCH_DIR=/mnt/nas             # host folder mounted at /nas in the container
WATCHER_ENABLED=true               # (compose sets this; WATCH_ROOTS is /nas/AI-Test inside the container)
AUTO_DRAFT=true
COMPOSER_ENABLED=true
```

Start:

```bash
docker compose up -d --build                              # the embedding model is pulled automatically (ollama-pull, ~620 MB, kept in the `ollama` volume)
docker compose logs -f api                                # wait for "Application startup complete"
```

Open **http://\<server-ip\>:8000/admin**. `restart: unless-stopped` on every service means a reboot or power cut
brings the stack back on its own; only `docker compose stop` keeps it down.

## 3. Day-to-day

| Task | How |
|---|---|
| Analyse a video | copy it into `/mnt/nas/AI-Test` — nothing else, raw camera files included. The watcher waits until the copy finishes, dedupes by content hash, shrinks raw/huge files to a 720p upload proxy (cost is per second of video, not per byte; >2 GB can't be uploaded otherwise), queues the analysis, embeds the blocks, extracts opportunities and drafts the best one. |
| See what's happening | `/admin` → Overview (pipeline strip, spend, what needs a decision) and Folder watcher (files seen, queued, duplicates, errors, per-file result, pause / scan now / re-scan). |
| Review an article | `/admin` → Review drafts → read → **Approve** / Needs changes / Reject, or Edit (saves a new version). Every decision lands in the Activity log. |
| Re-analyse a file that was replaced | Folder watcher → *re-scan* next to the file (or drop it under a new name). |
| Watch logs | `docker compose logs -f api` |
| Update the app | bump the tag in `docker-compose.yml`, then `docker compose pull && docker compose up -d` (migrations run on startup); or `docker compose up -d --build` when building from source |
| Backup | `docker compose exec postgres pg_dump -U cimage cimage_ai > backup.sql` plus the `./data` folder (uploads, renders, images) |

Search works without internet (local embeddings); if Ollama is down, search degrades to keyword-only and the
watcher's sweeper re-embeds anything missed once it's back. If Gemini is unreachable, jobs fail visibly and the
file can be re-scanned; nothing is silently skipped. Nothing is ever published without an approval in `/admin`.

## 4. Sizing & cost

The API, Postgres and Ollama are light. FFmpeg renders (Video Composer) are the only CPU-heavy step. 8 GB RAM is the
floor, 16 GB comfortable. Gemini cost is ≈ ₹20–40 per hour of footage at paid-tier rates (see README "Model choice");
embeddings and search are free (local).

## 5. Windows instead of Ubuntu

Docker Desktop (WSL2 backend) runs the same compose file. Clone the repo *inside* WSL (`\\wsl$\Ubuntu\home\…`) for
disk speed, mount the NAS in WSL (`sudo mount -t drvfs '\\NAS\media' /mnt/nas`) and point `NAS_WATCH_DIR` at it.
Docker Desktop only starts after a user signs in: enable *Start Docker Desktop when you sign in* plus Windows
auto sign-in (`netplwiz`), and set power options to never sleep.
