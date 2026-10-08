# CIMAGE AI Media Platform

Event-driven AI media system for CIMAGE: watch the NAS, turn videos/documents into
structured **knowledge blocks**, store them in PostgreSQL + pgvector, and (later) draft
blogs/social posts for human approval and WordPress publishing.

**Current milestone: V1.0 — video → knowledge blocks → hybrid search → content-opportunity queue → grounded blog drafts with pictures → review (approve / edit / reject) → WordPress publishing (draft or live, per site).**
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
| `POST/GET` | `/api/v1/scripts`, `…/{id}`, `…/{id}/regenerate`, `…/{id}/status`, `…/{id}/export` | Script Writer: idea (typed or dictated) → timed scenes; edit, rewrite, approve, download |
| `GET` | `/api/v1/script-options` | languages, styles, lengths (with word budgets), dictation + voiceover providers |
| `POST` | `/api/v1/scripts/{id}/voiceover`, `…/{id}/video` | speak the script (ElevenLabs) · cut it together as a branded video |
| `GET` | `/api/v1/tts/voices` | the voices on the ElevenLabs key, with the characters left |
| `GET/POST` | `/api/v1/jobs/{id}/transcript` | measured word/sentence timings for precise cuts; POST re-measures |
| `GET` | `/api/v1/renders/{id}/timeline`, `…/export?format=` | the edit as data; FCPXML / EDL / SRT / JSON for another editor |
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

## Prompts (editable in the UI)

Every prompt the platform sends to a model is a versioned Markdown file (`## system` / `## user` + `{placeholders}`):
video analysis `v1`/`v2`, people pass, blog writer, shot picker, reel cuts, plus the style guide and the known-people
roster. `/admin → Prompts` reads them all, lets you save an edit as a **new version** (bundled files are never changed —
custom versions go to `data/prompts/…` on the data volume), switch the active version per kind and edit the institution
context; changes apply live via `services/prompts/registry.py` (the engine and the writer reload their templates).
`.env` values (`PROMPT_VERSION`, `BLOG_PROMPT_VERSION`, …) remain the defaults; `data/prompt_config.json` holds overrides.

## Upload proxies (raw camera files)

Gemini charges per **second** of video (~1 frame/s sampled + audio), never per byte — a 21 GB ProRes master and a 200 MB
H.264 of the same ten minutes cost the same. Bytes only hurt: the File API refuses anything over 2 GB, and a 21 GB upload
takes ~15 min on 200 Mbps. So `services/block_engine/proxy.py` probes every local source before upload and, when it is
≥ `PROXY_MIN_MB` (400), above `PROXY_MAX_BITRATE_KBPS` (6 Mbps) or over the 2 GB limit, transcodes it to a
`PROXY_MAX_HEIGHT` (720p) H.264 / AAC proxy at CRF 28 — typically 50–200× smaller — and uploads that instead. The job
shows a `TRANSCODING` stage with the reason, sizes, ratio and seconds. The original never moves (the Reels studio cuts
from it); the proxy is deleted after analysis unless `PROXY_KEEP=true`. Phone/H.264 exports under the thresholds go as-is.


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

## Script Writer (V1.1)

Say or type an idea — in Hindi, English or a mix — and get a **timed, shootable video script** built from the same
knowledge base the blog writer uses. `/admin#scripts`.

```
mic (Chrome speech API, hi-IN) ─┐
                                ├─▶ idea ─▶ Retriever (pgvector over our own videos) ─▶ Gemini (script_v1) ─▶ scenes
typed idea ─────────────────────┘                                                                              │
                              teleprompter · b-roll picks · approve  ◀── /admin#scripts review ◀───────────────┘
```

`POST /api/v1/scripts {idea, language: hi|en|hinglish, seconds, style, job_id?, spoken}` → `202` and the page polls.
The agent retrieves up to 60 blocks, then writes strict JSON: title, **hook** (the first three seconds), scenes
(`seconds`, `visual`, `voiceover`, `on_screen_text`, `b_roll_block_id`, `b_roll_image_id`, `evidence_ids`), CTA, caption,
hashtags, thumbnail idea, music mood, a shot list of what still has to be filmed and the evidence gaps.

**Length is a budget, not a hope**: seconds × speaking rate (Hindi 2.1 w/s, Hinglish 2.3, English 2.5) sets the word
count and `scene_plan()` the number of scenes; afterwards the agent *checks* that the scenes add up to the length asked
for, that the voiceover fits, that the hook is short, that every id exists (ids the model copies as `[id=…]` markers are
normalised) and — for Hindi — that the voiceover really is Devanagari. Anything off becomes a warning on the script, never
a silent pass. Styles: viral reel · testimonial · campus tour · explainer · announcement · ad.

The page: one idea box with a **mic** (`STT_PROVIDER=browser`, `STT_LANGUAGE=hi-IN`; a local Whisper endpoint can replace
it without touching the UI), language / length / style / footage chips, then a scene timeline you can edit — swap a
scene's picture with the shared picker (stills · library · **upload from this computer**), reorder, add or delete scenes,
rewrite at a different length or language, and a **teleprompter** that scrolls the voiceover in exactly the length of the
video (space = start/stop, A± = size). Approve / reject like drafts; every edit keeps the previous version
(`scripts`, `script_versions`), and `GET /api/v1/scripts/{id}/export?format=md|txt` downloads the shooting script or the
plain voiceover. The prompt is `script_v1` in the registry, so the viral formula is tunable from **Prompts**.
Live 30 s Hindi reel from the CIMAGE library: 5 scenes, 65 words, all Devanagari, 4 cited blocks, 5 real stills, ₹0.3.

**Voiceover (ElevenLabs).** `POST /api/v1/scripts/{id}/voiceover {voice_id?, fit_scenes}` speaks **each scene separately**
(`eleven_multilingual_v2`, so Devanagari Hindi, Hinglish and English all work) into `data/voiceovers/<script>/scene_NNN.mp3`.
Every line's real duration is measured, compared with the scene it belongs to, and a scene that is too short is stretched
to fit (a versioned edit, like any other). `GET /api/v1/tts/voices` lists the account's voices and the characters left on
the key. Set `TTS_PROVIDER=elevenlabs` + `ELEVENLABS_API_KEY`; `mock` writes silence of the right length for tests.

**Script → finished video.** `POST /api/v1/scripts/{id}/video {preset, template, audio}` cuts the script together with the
*same* reel machinery: each scene becomes one branded segment — the video moment its `b_roll_block_id` points at (job +
timestamp, trimmed so it never runs past the end), the still from `b_roll_image_id` with a slow push-in, or a brand slate
when nothing has been shot yet — rendered through `render()` with the template frame and the on-screen text as its caption.
The segments are concatenated by stream copy (same preset, template and encoder settings) and the voiceover is laid over
the result, each line inside its own scene so the voice stays in sync; `audio` picks voiceover only, voiceover over ducked
source, or the footage's own sound. It runs on the composer's render queue, so it appears in the Reels studio and streams
from `/api/v1/renders/{id}/video` like any other render, with per-scene progress while it works. A 33 s 1080×1920 reel out
of a five-scene Hindi script took 40 s to render on the dev Mac.

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

## Publishing to WordPress (V1.0)

**Admin → Publishing** holds the websites: name, URL, WordPress username and an *application password* (Users → Profile →
Application Passwords), a per-site mode (**create a WP draft** you press Publish on, or **publish live**), *auto on approval*,
a default category and the SEO plugin (Yoast / Rank Math get title + meta description). The password is Fernet-encrypted at
rest with `PUBLISH_SECRET_KEY` (or a key generated once into `DATA_DIR/.secret_key`) and is never returned by the API.
**Test** checks the REST API (`/wp-json`, falling back to `?rest_route=`) and the account's capabilities.

Approving a draft (`POST /drafts/{id}/status approved`) sends it to every enabled site with auto-publish, in that site's mode;
**Publish** on an approved draft sends it to chosen sites with an optional mode override, and a site that already has the
article gets the same post updated. The hero becomes the featured image, inline pictures are uploaded to the media library once
(reused on re-publish) and the article is converted to Gutenberg block markup (`services/publishing/markdown_blocks.py`)
laid out per site (`layout`, defaults = cimage.in's house style: the lead image in the body after the intro paragraph — the
Elementor post template does not show the featured image — each section's picture directly under its heading, no captions);
tags/category are created if missing. Everything is recorded in `publications` (`database/migrations/012_publishing.sql`)
and the audit log (`publication.draft|published|failed`). Tested against a fake WordPress (`tests/test_publishing.py`).

| Method | Path | Purpose |
|---|---|---|
| `GET/POST` | `/api/v1/publish/targets` · `PUT/DELETE /{id}` · `POST /{id}/test` | websites |
| `POST` | `/api/v1/drafts/{id}/publish` `{target_ids?, mode?}` | publish / re-publish an approved draft (202) |
| `GET` | `/api/v1/drafts/{id}/publications` · `/api/v1/publish/publications` | what went where |

## Word-level transcripts (V1.2) - the timings cuts are made on

Gemini's analysis gives meaning with timestamps good to a second or two, and sentence boundaries *inside* a segment were
estimated by splitting the time in proportion to the characters - which is why a reel could clip a syllable. Local
Whisper measures the timing instead.

`POST /api/v1/jobs/{id}/transcript`, and automatically after every analysis (`TRANSCRIBE_ON_ANALYSIS=true`):
`faster-whisper` (`large-v3-turbo`, int8) over ffmpeg-decoded 16 kHz mono gives every word a start and end, grouped into
sentences (punctuation, then a pause >= 0.45 s, then the widest gap in an over-long run). Three things make it usable for
cutting rather than just reading:

* **The language comes from the analysis.** Left to guess, Whisper calls Hindi-with-English-words "English" and
  *translates* it; passing the language the analysis already found keeps the Devanagari.
* **Silences are measured on the waveform**, not taken from Whisper's word gaps - it reports fast speech as back-to-back
  words. A 10 ms RMS envelope with a threshold that adapts to the room (or the music bed) gives the real quiet spans, and
  `snap_in` / `snap_out` place a cut *inside* one: ~100 ms before the first word, ~160 ms after the last.
* **Speakers are carried over** from the analysis transcript by time overlap, so every sentence knows who said it.

Stored per job (`transcripts` table, JSON sidecar without Postgres), served by `GET /api/v1/jobs/{id}/transcript`
(`?words=true` for the word array) with `ends_open` / `starts_with_filler` flags, so the cutter can refuse to end a clip
on "लेकिन" or open it on "तो". A 40 s clip takes ~45 s on the dev Mac; the model downloads once (1.6 GB) to `data/models`.
Measurement is queued, never inline, so a long video never holds up embeddings or drafts.

## The edit studio (V1.4)

Reels studio opens as an editor, laid out and coloured the way an editor is — a dark, full-height workspace so the picture is the brightest thing on screen: **Sources** on the left, the **program monitor** in the
middle, the **clip inspector** on the right, and a **timeline** across the bottom. The AI fills the timeline in; the rest
is ordinary editing.

* **Sources** lists every analysed video; opening one shows its AI-proposed cuts *and* its measured sentences, each a
  click away from the timeline. That is how one reel ends up drawing on several shoots - clips from another video are a
  different colour on the track.
* **Timeline**: drag a clip to reorder, drag its edges to trim (released edges snap to sentence boundaries inside the
  measured silence), click to select, **S** splits at the playhead, **D** duplicates, **⌫** removes, **+/−** zoom,
  `fit` fits the edit to the width. A second lane shows the on-screen text of each clip, and the playhead can be
  scrubbed anywhere on the ruler.
* **Program monitor** plays the edit clip after clip with real transport (⏮ ◀◀ ▶ ▶▶), a running timecode and the
  on-screen text burnt over the picture as it will appear.
* **Inspector**: label, text on screen, ±0.2 s/±1 s nudges, snap-to-sentence, Auto/Fill/Fit, focus, follow-the-speaker
  (face tracking for that clip), mute.
* **Render** stitches it through the storyboard renderer; **Export** hands the same edit to Resolve or Premiere.
* The monitor frames the real output shape (9:16 / 1:1 / 16:9), clips on the track are **filmstrips** of their own first
  frame with the source badged on footage borrowed from another video, and the whole page fits the window - panels
  scroll inside themselves, nothing pushes the timeline off screen.

It saves itself as you work (`timelines` table). API: `POST/GET/PUT/DELETE /api/v1/timelines`, `POST …/clips`,
`POST …/render`, `GET …/export`, plus `POST /api/v1/jobs/{id}/track` for one clip's crop path. Measured: four clips from
two videos, reordered, trimmed, one muted - a 43.6 s branded reel in 14.7 s; splitting a clip keeps the edit's length.

## Editing studio: tracked framing, the timeline, and getting out (V1.3)

In the studio (`/admin#composer`) the measured transcript is drawn as a **speech lane** under the trim bar: one block per
sentence, positioned on the timeline. Click a sentence to cut exactly that, shift-click to extend to it, and with
**snap to speech** on (default) every handle you drag lands on a sentence boundary inside the measured silence
(`GET /api/v1/jobs/{id}/snap`). A **"what this cut says"** panel shows the words the clip will contain and warns when it
ends mid-thought. **Follow the speaker** turns on face tracking for the render, and a finished render offers
**timeline → FCPXML / EDL / SRT / JSON**. The side menu collapses to an icon rail with the button in the top bar or `[`,
and the choice is remembered.


**The crop follows the speaker.** `locate_subject()` answers "where are the faces on average" and gives one focus point
for a whole cut; `services/video_composer/tracking.py` answers "where is the speaker *now*". It samples every 0.5 s,
picks the dominant face (the biggest, unless a smaller one is clearly the one already being followed), then - because a
crop that chases every detection looks worse than a static one - smooths the path exponentially, ignores movement under
3.5% of the frame, caps the speed at a quarter-frame per second and reduces it to the fewest keyframes that still
describe it (Douglas-Peucker). If the speaker never really moves, it returns nothing and the static focus is used. The
renderer turns the keys into a piecewise-linear `crop=w:h:x(t):y(t)` expression, clamped to the frame.
`POST /api/v1/jobs/{id}/compose {"track_faces": true}`. Measured: 20 s of a multi-person talk tracked in 1.5 s.

**The edit is data** - `services/video_composer/timeline.py`. A `Timeline` is an ordered list of `Clip`s, each with its
source window, framing (static focus or a tracked path), captions, on-screen text, lower third and audio. Whatever makes
it - a cut proposal, a script's scenes, the AI's own choices - produces one of these, and a person moving a handle
changes the same object. `GET /api/v1/renders/{id}/timeline` returns it with every clip's position.

**It can leave.** `GET /api/v1/renders/{id}/export?format=fcpxml|edl|srt|json`:

| Format | Opens in | Carries |
|---|---|---|
| `fcpxml` (1.10) | DaVinci Resolve, Final Cut | assets as `file://` paths, frame-accurate offsets, on-screen text, tracking notes |
| `edl` (CMX3600) | Premiere, Avid, Resolve, anything | source and record timecode per event, file name per clip to relink |
| `srt` | any player / YouTube | the captions, timed against the finished cut |
| `json` | us, and any script | the timeline exactly as we hold it |

Nothing is re-encoded for the hand-off: every clip points at the original file at its original timecode, so the finishing
work happens on the masters.

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
