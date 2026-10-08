-- V1.2: measured word-level transcripts. Gemini's analysis gives meaning with second-ish timestamps; these are the
-- timings a cut is made on (word starts/ends from local Whisper), so a reel never clips a syllable.

CREATE TABLE IF NOT EXISTS transcripts (
    job_id          TEXT PRIMARY KEY REFERENCES processing_jobs (id) ON DELETE CASCADE,
    media_id        TEXT REFERENCES media (id),
    language        TEXT,
    model           TEXT NOT NULL,                       -- whisper:large-v3-turbo, mock, ...
    seconds         DOUBLE PRECISION NOT NULL DEFAULT 0, -- last word's end
    sentence_count  INTEGER NOT NULL DEFAULT 0,
    word_count      INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'ready' CHECK (status IN ('running', 'ready', 'failed')),
    error           TEXT,
    data            JSONB NOT NULL DEFAULT '{}'::jsonb,  -- the full Transcript (sentences with their words)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS transcripts_media_idx ON transcripts (media_id);
