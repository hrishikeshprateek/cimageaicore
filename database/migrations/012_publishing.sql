-- V1.0: publishing targets (WordPress first; other channels later) and what was published where.

CREATE TABLE IF NOT EXISTS publish_targets (
    id                 TEXT PRIMARY KEY,
    kind               TEXT NOT NULL CHECK (kind IN ('wordpress')),
    name               TEXT NOT NULL,
    url                TEXT NOT NULL,                         -- site root, e.g. https://cimage.in
    username           TEXT NOT NULL,
    secret_enc         TEXT NOT NULL,                         -- application password, Fernet-encrypted with PUBLISH_SECRET_KEY / data/.secret_key
    mode               TEXT NOT NULL DEFAULT 'draft' CHECK (mode IN ('draft', 'publish')),   -- how an approved article lands on the site
    auto_on_approval   BOOLEAN NOT NULL DEFAULT true,
    enabled            BOOLEAN NOT NULL DEFAULT true,
    default_category   TEXT,                                  -- category name, created if missing
    seo_plugin         TEXT NOT NULL DEFAULT 'none' CHECK (seo_plugin IN ('none', 'yoast', 'rankmath')),
    rest_prefix        TEXT NOT NULL DEFAULT '/wp-json',      -- '/?rest_route=' when pretty permalinks are off
    site_title         TEXT,
    last_test_at       TIMESTAMPTZ,
    last_test_ok       BOOLEAN,
    last_error         TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS publications (
    id               TEXT PRIMARY KEY,
    draft_id         TEXT NOT NULL REFERENCES drafts (id) ON DELETE CASCADE,
    target_id        TEXT NOT NULL REFERENCES publish_targets (id) ON DELETE CASCADE,
    status           TEXT NOT NULL CHECK (status IN ('queued', 'publishing', 'draft', 'published', 'failed')),
    mode             TEXT NOT NULL CHECK (mode IN ('draft', 'publish')),
    remote_id        TEXT,                                    -- WordPress post id
    remote_url       TEXT,
    edit_url         TEXT,
    draft_version    INTEGER,                                 -- which version of the article went out
    media_map        JSONB NOT NULL DEFAULT '{}'::jsonb,      -- image_id -> {id, url} on the site (reused on re-publish)
    error            TEXT,
    detail           JSONB NOT NULL DEFAULT '{}'::jsonb,
    triggered_by     TEXT NOT NULL DEFAULT 'editor',          -- editor | approval
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (draft_id, target_id)
);
CREATE INDEX IF NOT EXISTS publications_draft_idx ON publications (draft_id);
CREATE INDEX IF NOT EXISTS publications_status_idx ON publications (status, updated_at DESC);

INSERT INTO schema_migrations (version) VALUES ('012_publishing') ON CONFLICT DO NOTHING;
