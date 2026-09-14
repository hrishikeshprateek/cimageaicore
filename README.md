# CIMAGE AI Media Platform

Event-driven AI media system for CIMAGE: watch the NAS, turn videos/documents into
structured **knowledge blocks**, store them in PostgreSQL + pgvector, and (later) draft
blogs/social posts for human approval and WordPress publishing.

**Current milestone: V0.5/V0.6 — video → knowledge blocks → hybrid search → content-opportunity queue → grounded blog drafts → review (approve / edit / reject).** Publishing to WordPress is V1.0.
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
under `NAS_ALLOWED_ROOTS` (default `./data/nas-test`). http://localhost:8000/content is the
**Content Center**: the opportunities queue, drafts, evidence panel, edit and approval.

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
| `GET` | `/api/v1/search?q=…&mode=hybrid\|keyword\|vector&block_type=&media_id=` | hybrid search: Postgres full-text + pgvector cosine, reciprocal-rank fused; each hit says what matched it |
| `GET` | `/api/v1/system` | provider/model/store/config in use |
| `GET/POST` | `/api/v1/opportunities`, `…/{id}/status` | content-opportunity queue (AI-proposed per video, or manual); accept / dismiss |
| `POST` | `/api/v1/drafts` | `{opportunity_id}` or `{brief}` → Blog Agent runs in the background (202) |
| `GET/PUT` | `/api/v1/drafts/{id}`, `…/versions`, `…/regenerate`, `…/status` | draft with evidence + citations; editor edits are versioned; `in_review` / `approved` / `rejected` |
| `GET` | `/api/v1/agent-runs` | every agent call: model, prompt version, tokens, seconds |

Submitting the same bytes (sha256) or the same URL twice returns the existing job (`deduplicated: true`)
unless the earlier attempt failed, in which case a retry job is created against the same `media` row.

Job states: `RECEIVED → STABLE → QUEUED → UPLOADED → ANALYZING (main) → ANALYZING (people) → BLOCKS_PARTIAL → BLOCKS_COMPLETE → EMBEDDING → INDEXED → CONTENT_CANDIDATE` (or `FAILED`).
An embedding failure leaves the job at `BLOCKS_COMPLETE` (blocks are safe); `python scripts/embed_backfill.py` finishes it.

## Layout

```
apps/api            FastAPI app, config, routes, content_routes, db, jobs (states, JSON store, runner), pg_store, content_store
agents/blog_agent   Blog Agent: brief → evidence pack → grounded draft (citations checked against evidence)
services/retrieval  Retriever: hybrid search → bounded, source-referenced evidence pack
services/ai_gateway provider abstraction: base, gemini (Interactions API), mock
services/block_engine schemas (v1 blocks), sources (upload / nas_file / online), engine
prompts/            versioned prompts: video-analysis/{v1,v2,people_v1}, content-generation/{blog_v1, style_guide}, known_people.txt
web/                single-page UI
tests/              pytest (mock provider, schema, API)
docker/, docker-compose.yml   api + pgvector + redis (postgres/redis used from Phase 3)
database/migrations 001_init, 002_core, 003_embeddings, 004_content (content_opportunities, drafts, draft_versions, agent_runs)
scripts/            analyze.py (benchmark CLI), migrate.py, import_analysis.py, embed_backfill.py
services/ai_gateway/embeddings.py   Gemini Embedding 2 (768-d, per-text via Content wrapping) or mock
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

## Embeddings (V0.3)

`gemini-embedding-2` at 768 dimensions (documents as `title: … | text: …`, queries as `task: search result | query: …`),
stored in `knowledge_blocks.embedding` with an HNSW cosine index. Free tier: 100 RPM / 1K RPD; paid $0.20 per 1M tokens
(a whole video's blocks ≈ 2–5k tokens). Cross-lingual: Hindi queries find English blocks. YouTube URLs are canonicalised
to `watch?v=<id>` (playlist/radio parameters make Gemini return 403).

## Blog Agent (V0.5)

`POST /api/v1/drafts` → the Retriever builds an evidence pack (all blocks of the anchoring video + hybrid hits
across the library, facts before colour, bounded to 40 blocks / 14k chars) → `prompts/content-generation/blog_v1.md`
with the style guide derived from cimage.in/blog → strict JSON (title, slug, SEO, body markdown, tags, citations,
hero image block, LinkedIn/Instagram/Facebook posts, evidence gaps). Every citation is checked against the evidence;
unknown ones are dropped and flagged. Inline `[id=…]` markers stay in the stored body for review and are stripped in
`body_markdown_clean`. First real run: 685 words, 18 valid citations from two videos, 7.8k tokens (≈ ₹0.6 paid).

## Tests

```bash
pytest
```
