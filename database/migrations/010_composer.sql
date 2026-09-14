-- Video Composer: one row per rendered output (a cut x an export preset). Outputs live under data/renders/.

CREATE TABLE IF NOT EXISTS renders (
    id                TEXT PRIMARY KEY,
    job_id            TEXT NOT NULL REFERENCES processing_jobs (id) ON DELETE CASCADE,
    media_id          TEXT REFERENCES media (id),
    status            TEXT NOT NULL CHECK (status IN ('QUEUED', 'RENDERING', 'DONE', 'FAILED')),
    preset            TEXT NOT NULL,                  -- reels | square | landscape
    template          TEXT NOT NULL,
    cut_in            DOUBLE PRECISION NOT NULL,      -- seconds into the source
    cut_out           DOUBLE PRECISION NOT NULL,
    title             TEXT,
    cut_id            TEXT,                           -- which proposal it came from (q0, k1, ai0, ...)
    spec              JSONB NOT NULL,                 -- full RenderSpec (captions, lower-third, fit, encoder settings)
    output_path       TEXT,
    captions_path     TEXT,                           -- SRT sidecar
    width             INTEGER,
    height            INTEGER,
    duration_seconds  DOUBLE PRECISION,
    size_bytes        BIGINT,
    ffmpeg_command    TEXT,
    render_seconds    DOUBLE PRECISION,
    error             TEXT,
    detail            JSONB NOT NULL DEFAULT '{}'::jsonb,   -- resolved layout, text shaping, log tail
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS renders_job_idx     ON renders (job_id, created_at DESC);
CREATE INDEX IF NOT EXISTS renders_status_idx  ON renders (status);

INSERT INTO schema_migrations (version) VALUES ('010_composer') ON CONFLICT DO NOTHING;
