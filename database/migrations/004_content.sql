-- V0.5: content opportunities queue, drafts, agent runs.

CREATE TABLE IF NOT EXISTS content_opportunities (
    id                 TEXT PRIMARY KEY,
    job_id             TEXT REFERENCES processing_jobs (id) ON DELETE CASCADE,
    media_id           TEXT REFERENCES media (id),
    block_id           TEXT,                                   -- knowledge_blocks.id that proposed it (null when manual)
    source             TEXT NOT NULL DEFAULT 'ai' CHECK (source IN ('ai', 'manual')),
    title              TEXT NOT NULL,
    reason             TEXT,
    suggested_formats  JSONB NOT NULL DEFAULT '[]'::jsonb,
    audience           TEXT,
    confidence         REAL,
    status             TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'accepted', 'dismissed', 'drafted')),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS content_opportunities_block_uq ON content_opportunities (block_id) WHERE block_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS content_opportunities_status_idx ON content_opportunities (status, created_at DESC);

CREATE TABLE IF NOT EXISTS drafts (
    id               TEXT PRIMARY KEY,
    opportunity_id   TEXT REFERENCES content_opportunities (id) ON DELETE SET NULL,
    kind             TEXT NOT NULL DEFAULT 'blog',
    status           TEXT NOT NULL DEFAULT 'generating'
                     CHECK (status IN ('generating', 'new', 'in_review', 'approved', 'rejected', 'failed')),
    brief            TEXT NOT NULL,
    title            TEXT,
    slug             TEXT,
    body_markdown    TEXT,
    seo              JSONB NOT NULL DEFAULT '{}'::jsonb,       -- seo_title, meta_description, tags
    social           JSONB NOT NULL DEFAULT '{}'::jsonb,       -- linkedin, instagram, facebook
    citations        JSONB NOT NULL DEFAULT '[]'::jsonb,       -- [{block_id, used_for}]
    evidence         JSONB NOT NULL DEFAULT '{}'::jsonb,       -- the evidence pack given to the model
    hero_block_id    TEXT,
    model            TEXT,
    prompt_version   TEXT,
    usage            JSONB NOT NULL DEFAULT '{}'::jsonb,
    warnings         JSONB NOT NULL DEFAULT '[]'::jsonb,
    error            TEXT,
    version          INTEGER NOT NULL DEFAULT 1,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS drafts_status_idx ON drafts (status, created_at DESC);

CREATE TABLE IF NOT EXISTS draft_versions (                     -- every edit / regeneration keeps the previous text
    id             BIGSERIAL PRIMARY KEY,
    draft_id       TEXT NOT NULL REFERENCES drafts (id) ON DELETE CASCADE,
    version        INTEGER NOT NULL,
    title          TEXT,
    body_markdown  TEXT,
    edited_by      TEXT NOT NULL DEFAULT 'agent',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id              TEXT PRIMARY KEY,
    agent           TEXT NOT NULL,
    draft_id        TEXT REFERENCES drafts (id) ON DELETE SET NULL,
    opportunity_id  TEXT,
    model           TEXT,
    prompt_version  TEXT,
    status          TEXT NOT NULL,
    usage           JSONB NOT NULL DEFAULT '{}'::jsonb,
    seconds         DOUBLE PRECISION,
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
