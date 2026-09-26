-- V1.0: video scripts. An idea (typed or dictated) + the knowledge base -> a timed, shootable script, reviewed like a draft.

CREATE TABLE IF NOT EXISTS scripts (
    id               TEXT PRIMARY KEY,
    status           TEXT NOT NULL DEFAULT 'generating'
                     CHECK (status IN ('generating', 'new', 'in_review', 'approved', 'rejected', 'failed')),
    idea             TEXT NOT NULL,                             -- what the user said or typed
    spoken           BOOLEAN NOT NULL DEFAULT false,            -- came in through the microphone
    language         TEXT NOT NULL DEFAULT 'hi' CHECK (language IN ('hi', 'en', 'hinglish')),
    style            TEXT NOT NULL DEFAULT 'viral_reel',
    target_seconds   INTEGER NOT NULL DEFAULT 45,
    job_id           TEXT REFERENCES processing_jobs (id) ON DELETE SET NULL,   -- anchored to one video, or null = whole library
    title            TEXT,
    hook             TEXT,
    cta              TEXT,
    caption          TEXT,
    scenes           JSONB NOT NULL DEFAULT '[]'::jsonb,        -- [{n, seconds, visual, voiceover, on_screen_text, b_roll_block_id, b_roll_image_id, evidence_ids}]
    hashtags         JSONB NOT NULL DEFAULT '[]'::jsonb,
    extras           JSONB NOT NULL DEFAULT '{}'::jsonb,        -- thumbnail_idea, music_mood, shot_list, evidence_gaps
    evidence         JSONB NOT NULL DEFAULT '{}'::jsonb,        -- the evidence pack the writer was given
    model            TEXT,
    prompt_version   TEXT,
    usage            JSONB NOT NULL DEFAULT '{}'::jsonb,
    warnings         JSONB NOT NULL DEFAULT '[]'::jsonb,
    error            TEXT,
    version          INTEGER NOT NULL DEFAULT 1,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS scripts_status_idx ON scripts (status, created_at DESC);

CREATE TABLE IF NOT EXISTS script_versions (                    -- every edit / regeneration keeps the previous scenes
    id          BIGSERIAL PRIMARY KEY,
    script_id   TEXT NOT NULL REFERENCES scripts (id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    title       TEXT,
    scenes      JSONB NOT NULL DEFAULT '[]'::jsonb,
    edited_by   TEXT NOT NULL DEFAULT 'agent',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS script_id TEXT REFERENCES scripts (id) ON DELETE SET NULL;
