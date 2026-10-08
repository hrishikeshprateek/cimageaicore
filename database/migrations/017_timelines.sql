-- V1.4: saved edits. A timeline is the edit itself - an ordered list of clips from one or several analysed videos -
-- which the AI fills in and a person then rearranges. Rendering turns it into an MP4; exporting hands it to Resolve.

CREATE TABLE IF NOT EXISTS timelines (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    job_id      TEXT REFERENCES processing_jobs (id) ON DELETE SET NULL,   -- where most of the footage comes from
    preset      TEXT NOT NULL DEFAULT 'reels',
    template    TEXT,
    fit         TEXT,
    audio       TEXT NOT NULL DEFAULT 'source' CHECK (audio IN ('source', 'voiceover', 'both')),
    seconds     DOUBLE PRECISION NOT NULL DEFAULT 0,
    clips       JSONB NOT NULL DEFAULT '[]'::jsonb,
    source      JSONB NOT NULL DEFAULT '{}'::jsonb,     -- {kind: cut|script|manual, id: ...}
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS timelines_job_idx ON timelines (job_id, updated_at DESC);
