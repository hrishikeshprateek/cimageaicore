-- V0.7: images for content - frames pulled from analysed videos and a curated local library (never the open internet).

CREATE TABLE IF NOT EXISTS images (
    id                 TEXT PRIMARY KEY,
    kind               TEXT NOT NULL CHECK (kind IN ('frame', 'library')),
    job_id             TEXT REFERENCES processing_jobs (id) ON DELETE CASCADE,   -- frames: the analysed video
    media_id           TEXT REFERENCES media (id),
    block_id           TEXT,                                   -- media / key_moment block the frame was taken for
    timestamp_seconds  DOUBLE PRECISION,                       -- frames: position in the video
    path               TEXT NOT NULL,
    width              INTEGER,
    height             INTEGER,
    size_bytes         BIGINT,
    description        TEXT NOT NULL DEFAULT '',               -- what the picture shows (block description or typed by the editor)
    tags               JSONB NOT NULL DEFAULT '[]'::jsonb,
    suitable_for       JSONB NOT NULL DEFAULT '[]'::jsonb,     -- thumbnail | blog_hero | social_post | press | archive
    source_name        TEXT,                                   -- video file name / original photo file name
    sha256             TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS images_job_idx ON images (job_id);
CREATE INDEX IF NOT EXISTS images_kind_idx ON images (kind, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS images_block_uq ON images (block_id) WHERE block_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS images_sha_uq ON images (sha256) WHERE sha256 IS NOT NULL;

-- drafts learn where their pictures go: [{image_id, placement: hero|inline, caption, alt_text}]
ALTER TABLE drafts ADD COLUMN IF NOT EXISTS images JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE drafts ADD COLUMN IF NOT EXISTS hero_image_id TEXT;
ALTER TABLE drafts ADD COLUMN IF NOT EXISTS depth TEXT NOT NULL DEFAULT 'standard';

INSERT INTO schema_migrations (version) VALUES ('011_images') ON CONFLICT DO NOTHING;
