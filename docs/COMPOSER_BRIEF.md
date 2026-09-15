# Video Composer — brief for the parallel build session

You are building the **Video Composer** component of the CIMAGE AI Media Platform in this repo,
in parallel with another session that is building embeddings/search and the Blog Agent.
Read `README.md` and `docs/BUILD_SPEC.md` first. Do not redesign the platform.

## Goal
Turn a review/testimonial video that has already been analysed into a branded short:
the review clip sits inside the college template (top and bottom layers), with burned-in
captions and a lower-third, exported for Instagram Reels / YouTube Shorts (9:16), feed (1:1)
and YouTube (16:9). AI proposes the cut; a human previews, nudges and approves; ffmpeg renders.

## What already exists (do not rebuild)
- Video → knowledge blocks: `POST /api/v1/analyze`, `GET /api/v1/jobs/{id}`,
  `GET /api/v1/jobs/{id}/blocks?block_type=quote|key_moment|transcript|person|media`.
  Block payloads are in `services/block_engine/schemas.py` (timestamps are `HH:MM:SS`).
- PostgreSQL store (`apps/api/pg_store.py`, migrations in `database/migrations/`), JSON fallback.
- AI Gateway (`services/ai_gateway/`) — the only place Gemini is called. Add new prompts as
  versioned files; never edit an existing prompt version in place (create `..._v2.md`).
- ffmpeg/ffprobe are on PATH (`services/block_engine/media.py` has helpers).
- Settings via `.env` / `apps/api/config.py` (pydantic-settings).

## Files you own (create freely)
- `services/video_composer/**`            renderer, cut selection, template model
- `apps/api/composer_routes.py`           mounted with ONE line in `apps/api/main.py`
- `database/migrations/010_composer.sql`  numbers 010+ are reserved for you (003–009 are the other session's)
- `prompts/video-composer/**`
- `web/composer.html` (+ a link from `web/index.html` header only)
- `tests/test_composer*.py`
- `data/renders/` output (git-ignored), `data/nas-test/composer/` inputs

## Files you must NOT edit (owned by the other session)
`services/block_engine/**`, `services/ai_gateway/**` (except adding a new method in a new file is
fine if unavoidable — say so in your summary), `apps/api/jobs.py`, `apps/api/pg_store.py`,
`apps/api/routes.py`, `apps/api/db.py`, `web/index.html` beyond one nav link, migrations 003–009.
Config: add your settings with a `COMPOSER_` prefix; feature flag `COMPOSER_ENABLED=false` by default.

## Build order
1. Template model: canvas size, video rectangle, layer files (PNG with alpha / .mov ProRes 4444 / WebM alpha),
   font, colours, safe areas. Start with a **generated placeholder template** so nothing waits on assets.
2. Renderer (`ffmpeg` via subprocess): cut by in/out → scale/pad into rectangle → overlay layers →
   burn captions (SRT/ASS generated from `transcript` blocks, Devanagari-capable font) → lower-third →
   export presets 1080×1920, 1080×1080, 1920×1080. Deterministic; unit-test with a synthetic clip
   (`ffmpeg -f lavfi testsrc2`), no network.
3. Cut selection: from a job's `quote` + `key_moment` + `transcript` blocks propose 2–3 cuts
   (in/out, caption lines, lower-third text from `person` blocks). Rule-based first; a Gemini prompt
   (`prompts/video-composer/cuts_v1.md`, structured JSON) can refine it.
4. `renders` table (job_id, cut in/out, preset, template, status, output path, created_at) +
   `POST /api/v1/jobs/{id}/compose`, `GET /api/v1/renders/{id}` (+ MP4 stream for preview).
5. `web/composer.html`: pick a job → see proposed cuts → preview → nudge trim → render.
6. Staging: renders stay in `data/renders/` and the UI only; no publishing (that goes through the
   approval gate the other session is building).

## Assets (arrive later from the user; design so they slot in)
Top/bottom layers, logo, fonts, brand colours, layout spec, 3–5 raw review videos, one hand-edited
reference output, target channels.

## Definition of done
- Synthetic clip + placeholder template → valid MP4 in all three presets, captions visible, tests pass offline.
- A real analysed job → 2–3 proposed cuts → preview → render, from the UI.
- Swapping the placeholder for real assets is config only.

## Framing (added 2026-09-14)
Most sources are 16:9 talks; the reel's clip window is 1080×920. Letterboxing there shrinks the clip
to a 608px strip, so a layout's `fit` is now `contain | cover | auto` (default **auto**):

- **auto** fills the window and crops (cover) unless the crop would keep < 45% of the source
  (`template.COVER_MIN_RETAINED`) — a 16:9 clip in the reel keeps 66% → cropped; a 9:16 phone clip in
  the 16:9 preset would keep 32% → letterboxed. Landscape layouts keep an explicit `contain`.
- The crop window is placed by `focus_x/focus_y` (0..1, default centre) on the `RenderSpec` / compose
  request. `GET /jobs/{id}/framing?cut_in&cut_out&preset` resolves the fit, samples 5 frames across the
  cut, finds faces with OpenCV YuNet (`services/video_composer/framing.py`, model vendored under
  `models/`; `pip install -e ".[composer]"`) and returns a focus that keeps the people in frame.
  Without OpenCV the suggestion is the centre and the UI slider still works.
- `GET /jobs/{id}/frame?at=` returns one JPEG for the crop preview in the composer's **Framing** panel
  (Auto / Fill / Fit, focus slider, ↻ faces). `cover_crop()` produces explicit pixel values so the
  ffmpeg `scale=…,crop=W:H:X:Y` chain and the preview box agree exactly.
- Online (YouTube) jobs have no local file and cannot be composed; a source file must be attached/re-analysed.

## UI location (2026-09-15)
The composer UI now lives inside the single admin app: `web/admin/views/composer.js` (same features, Material styling).
`web/composer.html` is retired under `web/_legacy/` and `/composer` redirects to `/admin#composer`. API routes are unchanged.
