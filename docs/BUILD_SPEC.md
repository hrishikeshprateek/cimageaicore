# CIMAGE AI Media Platform — Build Specification

Condensed from the design discussion (September 2026). This is the implementation
blueprint; do not redesign, build in phases.

## 0. Objective
An event-driven AI media platform that watches the CIMAGE NAS, accepts uploaded videos
and permitted online video sources, analyses media with the Gemini API, optionally uses
local models for selected tasks, converts media into structured knowledge blocks, stores
metadata + embeddings in PostgreSQL + pgvector, and later generates blogs/social content
with human approval before publishing to WordPress.

**Do not build the autonomous blog/publishing system first. The first milestone is the
Video → Knowledge Blocks engine.**

## 1. Architecture
```
USER / MEDIA TEAM ── upload | NAS drop | permitted online URL
        ↓
INGESTION (stable-file check) → REDIS QUEUE → VIDEO WORKER
        ↓
AI GATEWAY ── Gemini API (video / reasoning)  |  Local provider (optional tasks)
        ↓
STRUCTURED BLOCKS → VALIDATE / NORMALISE → POSTGRESQL + PGVECTOR
        ↓
RAG / AGENTS (blog, social, search) → APPROVAL UI → WORDPRESS API
```
Principles: event-driven (never rescan the NAS); NAS stays the source of truth; hybrid AI
(local orchestration, cloud multimodal); progressive processing; human approval;
tool-controlled agents (explicit tools, no unrestricted shell/NAS/internet).

## 2. Development strategy
- Develop on the MacBook Air; deploy the same Docker services to the 64 GB editor
  workstation as the 24×7 server. Never develop on production.
- Git + Docker from day one. Provider-agnostic AI Gateway (`analyze_video`, `extract_blocks`,
  `summarize`, `generate_blog`). Gemini first; local model later through the same gateway.
- Small test dataset (`/NAS/AI-Test`, 5–10 files), never the whole NAS.

## 3. Gemini
Education Plus gives Gemini *Apps*, not API quota. Use a dedicated Google Cloud / AI Studio
project, an **authorization key** (standard keys are rejected from September 2026),
budgets and monitoring. Use the File API for large/long videos; treat analysis as an async
job; persist provider file/interaction IDs. Never automate the Gemini web UI.

## 4. Block model (v1)
| Block | Minimum fields |
|---|---|
| video | title, type, language, description, observed_duration |
| event | name, date, venue, organizer, description, confidence |
| person | name, role, context, timestamps, confidence |
| transcript | speaker, start_time, end_time, text, language |
| topic | name, evidence, timestamps, confidence |
| key_moment | timestamp, description, importance |
| quote | speaker, text, timestamp, source_reference |
| media | timestamp, description, suitable_for |
| summary | short_summary, detailed_summary, key_points |
| content_opportunity | title, reason, suggested_formats, audience, confidence |

Strict JSON, versioned schema, Pydantic validation; reject/repair/retry invalid output.

## 5. Progressive job states
`RECEIVED → STABLE → QUEUED → UPLOADED → ANALYZING → BLOCKS_PARTIAL → BLOCKS_COMPLETE →
EMBEDDING → INDEXED → CONTENT_CANDIDATE` (+ `FAILED`). Real-time = immediate detection +
asynchronous processing, not instant completion of a two-hour video.

## 6. Database (Phase 3)
Tables: media, media_sources, processing_jobs, events, people, transcript_segments, topics,
knowledge_blocks, embeddings, content_opportunities, agent_runs, audit_log. Store paths and
references, not the video. Hybrid retrieval: SQL filters + vector similarity; every block
carries source ID + timestamp.

## 7. Agents (after block MVP)
Tools: search_internal, get_blocks, search_web, fetch_webpage, generate_blog,
generate_social, save_draft, request_approval, publish_wordpress. Internal facts vs external
research must be distinguishable; external claims keep citations.

## 8. Security
No direct LAN exposure of Postgres/Redis/workers; least-privilege read-only NAS account;
secrets server-side only; allow-list of folders permitted for external AI processing;
audit logs for ingestion, AI calls, block changes, approvals, publishing; no auto-publishing
of sensitive/student data; retries, rate limits, timeouts, dead-letter queue; backups.

## 9. Hardware target
Now: 64 GB editor workstation (inspect CPU/GPU), Cat5 LAN (verify link speed), 200 Mbps
internet. Preferred: 16–24 cores, 128 GB RAM, 2–4 TB NVMe, 10 GbE, Ubuntu Server LTS, UPS.
GPU optional for the Gemini-first design.

## 10. MVP Definition of Done
- [x] Upload a video through the local web UI
- [ ] NAS test-folder video detected automatically
- [x] Permitted online video enters the same pipeline (YouTube URL → Gemini)
- [x] Validated structured blocks produced (verified on a real CIMAGE video, 2026-09-14)
- [~] Partial results stored before the job completes (BLOCKS_PARTIAL state; true streaming of partial blocks pending)
- [x] All blocks carry source references / timestamps
- [~] PostgreSQL stores metadata ✅ · pgvector embeddings pending (V0.3)
- [ ] Semantic search over blocks
- [x] Failed jobs retry without duplicates (sha256/url dedupe; failed → retry on same media row)
- [x] Every step observable in dashboard/logs (stage timeline + audit_log)
- [x] No secret reaches the browser

## 11. Build order
1. Inspect environment, create repository ✅
2. Docker Compose: API + PostgreSQL/pgvector + Redis ✅ (compose written; DB unused until step 3)
3. Database migrations and models ✅ (002_core; PostgresJobStore; sha256/url de-duplication; audit_log)
4. AI Gateway abstraction ✅
5. Gemini provider + structured video-analysis call ✅
6. Pydantic block schemas + validation ✅
7. Video Analysis endpoint + minimal web UI ✅
8. Job queue + video worker (Redis)
9. Progressive job states + persistence ✅ (Postgres, JSON fallback)
10. NAS watcher against /AI-Test only
11. Embeddings + pgvector retrieval
12. Block/search UI ✅ keyword (FTS) — vector/hybrid pending step 11
13. Permitted online-video source adapter ✅ (YouTube URL → Gemini)
14. Content-opportunity generation
15. Blog Agent
16. Approval dashboard + WordPress
17. Production deployment on the 64 GB server

Version ladder: V0.1 `Video → Gemini → JSON blocks` · V0.2 `→ PostgreSQL` · V0.3 `→ pgvector search`
· V0.4 `NAS auto-detect` · V0.5 `Blog Agent` · V0.6 `Approval` · V1.0 `WordPress + social`.
