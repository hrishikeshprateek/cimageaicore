-- V0.2: core knowledge tables. Originals stay on the NAS; we store references, blocks and provenance.

CREATE TABLE IF NOT EXISTS media (
    id                TEXT PRIMARY KEY,
    kind              TEXT NOT NULL CHECK (kind IN ('upload', 'nas_file', 'online')),
    name              TEXT NOT NULL,
    path              TEXT,
    url               TEXT,
    mime_type         TEXT,
    size_bytes        BIGINT,
    sha256            TEXT,
    duration_seconds  DOUBLE PRECISION,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS media_sha256_uq ON media (sha256) WHERE sha256 IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS media_url_uq    ON media (url)    WHERE url IS NOT NULL;

CREATE TABLE IF NOT EXISTS processing_jobs (
    id                  TEXT PRIMARY KEY,
    media_id            TEXT NOT NULL REFERENCES media (id),
    state               TEXT NOT NULL,
    provider            TEXT NOT NULL,
    model               TEXT NOT NULL,
    prompt_version      TEXT,
    stages              JSONB NOT NULL DEFAULT '[]'::jsonb,
    error               TEXT,
    block_counts        JSONB NOT NULL DEFAULT '{}'::jsonb,
    usage               JSONB NOT NULL DEFAULT '{}'::jsonb,
    processing_seconds  DOUBLE PRECISION,
    warnings            JSONB NOT NULL DEFAULT '[]'::jsonb,
    repaired            BOOLEAN NOT NULL DEFAULT false,
    result              JSONB,               -- full AnalysisResult
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS processing_jobs_state_idx   ON processing_jobs (state);
CREATE INDEX IF NOT EXISTS processing_jobs_media_idx   ON processing_jobs (media_id);
CREATE INDEX IF NOT EXISTS processing_jobs_created_idx ON processing_jobs (created_at DESC);

CREATE TABLE IF NOT EXISTS knowledge_blocks (
    id                 TEXT PRIMARY KEY,              -- <job_id>:<block_type>:<ordinal>
    job_id             TEXT NOT NULL REFERENCES processing_jobs (id) ON DELETE CASCADE,
    media_id           TEXT NOT NULL REFERENCES media (id),
    block_type         TEXT NOT NULL,
    ordinal            INTEGER NOT NULL,
    timestamp_ts       TEXT,                          -- HH:MM:SS inside the video
    timestamp_seconds  DOUBLE PRECISION,
    text               TEXT NOT NULL,                 -- searchable rendering (embedded in V0.3)
    payload            JSONB NOT NULL,
    confidence         REAL,
    schema_version     TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS knowledge_blocks_media_idx ON knowledge_blocks (media_id);
CREATE INDEX IF NOT EXISTS knowledge_blocks_type_idx  ON knowledge_blocks (block_type);
CREATE INDEX IF NOT EXISTS knowledge_blocks_fts_idx   ON knowledge_blocks USING GIN (to_tsvector('simple', text));

CREATE TABLE IF NOT EXISTS audit_log (
    id           BIGSERIAL PRIMARY KEY,
    at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor        TEXT NOT NULL DEFAULT 'system',
    action       TEXT NOT NULL,
    entity_type  TEXT,
    entity_id    TEXT,
    detail       JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS audit_log_entity_idx ON audit_log (entity_type, entity_id);

INSERT INTO schema_migrations (version) VALUES ('002_core') ON CONFLICT DO NOTHING;
