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
| `POST` | `/api/v1/drafts` | `{opportunity_id}` or `{brief}` (+ `job_id`, `depth`, `include_images`) → Blog Agent runs in the background (202) |
| `PUT` | `/api/v1/drafts/{id}/images` | editor sets the hero / inline pictures (new version) |
| `POST/GET` | `/api/v1/jobs/{id}/frames` · `/api/v1/images?job_id=&kind=` · `/api/v1/images/{id}` | frames from a video, image list, image file |
| `POST` | `/api/v1/images/library` · `/api/v1/images/library/import` | add photos to the local library (upload / folder) |
| `GET/PUT` | `/api/v1/drafts/{id}`, `…/versions`, `…/regenerate`, `…/status` | draft with evidence + citations; editor edits are versioned; `in_review` / `approved` / `rejected` |
| `GET` | `/api/v1/agent-runs` | every agent call: model, prompt version, tokens, seconds |

Submitting the same bytes (sha256) or the same URL twice returns the existing job (`deduplicated: true`)
unless the earlier attempt failed, in which case a retry job is created against the same `media` row.

Job states: `RECEIVED → STABLE → QUEUED → UPLOADED → ANALYZING (main) → ANALYZING (people) → BLOCKS_PARTIAL → BLOCKS_COMPLETE → EMBEDDING → INDEXED → CONTENT_CANDIDATE` (or `FAILED`).
An embedding failure leaves the job at `BLOCKS_COMPLETE` (blocks are safe); `python scripts/embed_backfill.py` finishes it.

## Layout

```
apps/api            FastAPI app, config, routes, content_routes, db, jobs (states, JSON store, runner), pg_store, content_store
agents/blog_agent   Blog Agent: brief → evidence pack (+ offered pictures) → grounded draft (citations and image markers checked)
services/media_library  images: frames from analysed videos (ffmpeg, sharpest candidate) + local photo library; table `images`
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
services/video_composer  template model + placeholder, Pillow text (captions/lower-third), ffmpeg renderer, cut rules, renders store (V0.7)
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
(a whole video's blocks ≈ 2–5k tokens).

**Local alternative (default in docker-compose, `EMBEDDING_PROVIDER=ollama`)**: Google's EmbeddingGemma-300M served by
Ollama — same 768 dims and the same prompt formats, 100+ languages, ~25 ms per query on CPU, ₹0 and no internet needed for
search. `ollama pull embeddinggemma` once. The store records `embedding_model` per block; switching providers marks the
other model's vectors stale, `scripts/embed_backfill.py` re-embeds them (130 blocks ≈ 3 s), and vector search only
matches blocks embedded by the current model, so a half-migrated index never returns nonsense. Cross-lingual: Hindi queries find English blocks. YouTube URLs are canonicalised
to `watch?v=<id>` (playlist/radio parameters make Gemini return 403).

## Blog Agent (V0.5)

`POST /api/v1/drafts` → the Retriever builds an evidence pack (all blocks of the anchoring video + hybrid hits
across the library, facts before colour, bounded to 40 blocks / 14k chars) → `prompts/content-generation/blog_v1.md`
with the style guide derived from cimage.in/blog → strict JSON (title, slug, SEO, body markdown, tags, citations,
hero image block, LinkedIn/Instagram/Facebook posts, evidence gaps). Every citation is checked against the evidence;
unknown ones are dropped and flagged. Inline `[id=…]` markers stay in the stored body for review and are stripped in
`body_markdown_clean`. First real run: 685 words, 18 valid citations from two videos, 7.8k tokens (≈ ₹0.6 paid).

**Depth + pictures (blog_v2, 2026-09-15).** `POST /api/v1/drafts {brief, job_id?, depth: in_depth|standard, include_images}`:
*in-depth* asks for ≈1500 words, 6–8 sections, a Key Takeaways list and an FAQ where the evidence supports it, with a
larger evidence budget (80 blocks / 30k chars). Pictures come from the institution's own material only: **AI-verified stills from
the analysed video** (`POST /api/v1/jobs/{id}/frames`, default `mode=ai`: ffmpeg shot detection → near-duplicates dropped →
numbered contact sheets go to Gemini with the nearby knowledge blocks (`prompts/content-generation/shots_v1.md`,
gateway `describe_images()`) → each still is stored with what is *actually visible*, a quality verdict, suitability and the
block it shows; the analysis timestamps alone are ±2–5 s off, which is why `mode=blocks` frames are only a no-AI fallback),
the **YouTube thumbnail** for jobs analysed from a CIMAGE YouTube URL (no local file), and a **local photo library** the media
team fills (`POST /api/v1/images/library` upload or `/images/library/import` a folder under `NAS_ALLOWED_ROOTS`, with descriptions/tags).
Table `images`, files in `data/images/`. Campus-tour video: 33 shots indexed in 27 s / 9.7k tokens, all correctly described. The writer is offered these as
`[img=…]` lines, picks a hero and 2–5 inline pictures with captions and alt text, and everything is validated like citations
(unknown ids dropped, missing markers placed, hero fallback). The Content Center's **Images** tab shows the placements,
the video's frames and the library, with Hero / Insert / Remove and a captions editor; `body_markdown_clean` renders the
figures as Markdown images. Picture ids written as `[id=…]` citations are scrubbed rather than counted as bad citations. Live run on the campus-tour video after
indexing: 7 sections incl. takeaways + FAQ, 1,240 words, 21 citations, hero + 5 verified stills with accurate captions, 12k tokens.

## Folder watcher + auto-draft + admin panel (V0.4 / V0.6 / V0.8)

Set `WATCHER_ENABLED=true` and `WATCH_ROOTS=<folder>`: `services/ingestion/watcher.py` polls the folder (polling, because
SMB/NFS/Docker mounts don't deliver file events), waits until a file's size/mtime stop changing, submits it through the
same path as `/analyze` (content-hash dedupe, audit), keeps at most `WATCHER_MAX_ACTIVE_JOBS` analyses in flight, and
between scans embeds any block that lacks a vector or carries one from a previous embedding model. Its index
(`data/watcher_state.json`) survives restarts. With `AUTO_DRAFT=true` the best opportunity of every finished video becomes
a blog draft with nobody clicking (`apps/api/content_routes.py::auto_draft_job`).

**One UI — `/admin`** (`web/admin.html` + ES modules in `web/admin/`, no build step). Google-console styling: Google Sans
Flex + Google Sans Code + Material Symbols from Google Fonts (inline-SVG icon fallback when offline), white cards on `#f8f9fa`,
`#dadce0` hairlines, blue `#1a73e8` / `#e8f0fe` selection; navigation drawer → rail → bottom bar as the screen narrows; light/dark. Sections: Overview (pipeline strip, spend,
"needs your attention"), Videos (analyse via upload / YouTube URL / NAS path, library, per-video blocks), Search (hybrid /
semantic / keyword), Opportunities, Review drafts (read, pictures & photo library, SEO/social, evidence, edit, versions,
approve / needs changes / reject; `#drafts/<id>` deep-links), Reels studio (the composer: cuts, framing, captions, renders),
Folder watcher, Activity log. The old stand-alone pages (`/`, `/content`, `/composer`) redirect into their sections; their
files are kept under `web/_legacy/` and are not served. Data: `apps/api/admin_routes.py` (`/admin/overview`, `/watcher*`,
`/audit`) plus the existing routers. Deployment: `docs/DEPLOY.md`.

## Video Composer (V0.7)

`COMPOSER_ENABLED=true` → **http://localhost:8000/composer**: pick an analysed video, take one of the 2–3 proposed cuts
(quotes + key moments; optional Gemini refinement), nudge in/out, edit the caption cues, render Reels 9:16 / feed 1:1 /
YouTube 16:9 inside the college frame with burned-in Devanagari-capable captions and a lower-third. Renders stay in
`data/renders/` (table `renders`) until the approval gate publishes them. Real frame assets slot in through the UI
(*Template → Upload a real layer*) or `data/composer/templates/<name>/`. Details: [docs/COMPOSER.md](docs/COMPOSER.md).

## Tests

```bash
pytest
```
