# CIMAGE AI Media Platform

Event-driven AI media system for CIMAGE: watch the NAS, turn videos/documents into
structured **knowledge blocks**, store them in PostgreSQL + pgvector, and (later) draft
blogs/social posts for human approval and WordPress publishing.

**Current milestone: V0.2 — Video → Gemini → validated knowledge blocks → PostgreSQL (+ keyword search).**
See [docs/BUILD_SPEC.md](docs/BUILD_SPEC.md) for the full architecture and build order.

## Quick start (development, MacBook)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # then put your Gemini authorization key in GEMINI_API_KEY
docker compose up -d postgres redis   # pgvector on host port 5433, redis on 6380
uvicorn apps.api.main:app --reload --port 8000
```

Migrations in `database/migrations/*.sql` are applied automatically at startup (`AUTO_MIGRATE=true`)
or with `python scripts/migrate.py`. If `DATABASE_URL` is blank or unreachable the app falls back to
JSON files under `data/` so it still runs without Docker.

Open http://localhost:8000 — drop a video, paste a public YouTube URL, or give a path
under `NAS_ALLOWED_ROOTS` (default `./data/nas-test`).

Without `GEMINI_API_KEY` the app runs with the **mock provider** (placeholder blocks,
clearly labelled `[MOCK]`) so the pipeline can be exercised offline.

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/analyze` | multipart: `file` **or** `path` **or** `url` → `202 {job_id}` |
| `GET` | `/api/v1/jobs` | all jobs, newest first |
| `GET` | `/api/v1/jobs/{id}` | job state, stage timeline, counts, usage |
| `GET` | `/api/v1/jobs/{id}/result` | full `AnalysisResult` (structured blocks + provenance) |
| `GET` | `/api/v1/jobs/{id}/blocks?block_type=quote` | flattened block rows (future `knowledge_blocks` table) |
| `GET` | `/api/v1/search?q=…&block_type=quote` | keyword search over all stored blocks (hybrid vector search in V0.3) |
| `GET` | `/api/v1/system` | provider/model/store/config in use |

Submitting the same bytes (sha256) or the same URL twice returns the existing job (`deduplicated: true`)
unless the earlier attempt failed, in which case a retry job is created against the same `media` row.

Job states: `RECEIVED → STABLE → QUEUED → UPLOADED → ANALYZING → BLOCKS_PARTIAL → BLOCKS_COMPLETE` (or `FAILED`).
`EMBEDDING / INDEXED / CONTENT_CANDIDATE` are reserved for later phases.

## Layout

```
apps/api            FastAPI app, config, routes, db (pool + migrations), jobs (states, JSON store, runner), pg_store
services/ai_gateway provider abstraction: base, gemini (Interactions API), mock
services/block_engine schemas (v1 blocks), sources (upload / nas_file / online), engine
prompts/            versioned prompt templates
web/                single-page UI
tests/              pytest (mock provider, schema, API)
docker/, docker-compose.yml   api + pgvector + redis (postgres/redis used from Phase 3)
database/migrations 001_init (pgvector), 002_core (media, processing_jobs, knowledge_blocks, audit_log)
scripts/            analyze.py (benchmark CLI), migrate.py, import_analysis.py (backfill JSON → Postgres)
data/               uploads/, jobs/, analyses/, nas-test/   (git-ignored)
```

## Model choice, quota and latency (measured 2026-09-14, 73 s 720p clip)

| Model | Free tier (RPM / TPM / RPD) | Time | Tokens in / out | Notes |
|---|---|---|---|---|
| `gemini-3.5-flash-lite` (**default**) | 15 / 250K / 500 | **29 s** (16 s of it upload) | 7,090 / 3,000 | comparable blocks; only names people shown/spoken on screen |
| `gemini-3.8-flash` | 5 / 250K / 20 | 378 s | 7,090 / 2,584 | agentic video = several internal requests, throttled by 5 RPM |

- `resolution=low` + `fps=0.5` cut input tokens 33 % but risk missing on-screen text — not worth ≈ ₹0.4/video. Keep `medium`.
- `thinking_level` accepts low | medium | high (no `minimal`).
- 250K TPM means a ~1-hour video at medium resolution (~325K tokens) needs the paid tier or low resolution.
- Paid tier: 3.5 Flash Lite $0.30 / $2.50 per 1M tokens (≈ ₹1 per short clip, ≈ ₹14 per hour of video); data not used for training.
- **Prompts are versioned** (`prompts/video-analysis/v1.md`, `v2.md`, `people_v1.md`); old versions are kept for A/B runs (`scripts/analyze.py --prompt v1`). Default `PROMPT_VERSION=v2`.
- **People**: a focused second pass (`PEOPLE_PASS_VERSION=people_v1`) identifies people against `prompts/known_people.txt`. Names from the screen/speech get `identified_by` on_screen/spoken; recognised people (roster or public figures) get `recognised` with confidence ≤ 0.7; unnamed speakers get `unnamed`. On Flash Lite the single-pass extraction found the Director in 1 of 4 runs; the focused pass found him in 3 of 3.
- **Transcript guard**: if the transcript is implausibly coarse (< 1 segment per 60 s) the engine re-runs once and keeps the better result; still-coarse results are flagged in `warnings`.
- Timeouts are per call type: analysis 1800 s, upload chunk 600 s, status polls 30 s with retries (a dropped poll must never hang a job).

Benchmark any combination with `python scripts/analyze.py <video> --model … --thinking … --resolution … --fps …`.

## Tests

```bash
pytest
```
