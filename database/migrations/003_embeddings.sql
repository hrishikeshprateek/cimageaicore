-- V0.3: vector embeddings on knowledge blocks (Gemini Embedding 2 @ 768 dims, unit-normalised).
ALTER TABLE knowledge_blocks ADD COLUMN IF NOT EXISTS embedding        vector(768);
ALTER TABLE knowledge_blocks ADD COLUMN IF NOT EXISTS embedding_model  TEXT;
ALTER TABLE knowledge_blocks ADD COLUMN IF NOT EXISTS embedded_at      TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS knowledge_blocks_embedding_hnsw
    ON knowledge_blocks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS knowledge_blocks_unembedded_idx
    ON knowledge_blocks (created_at) WHERE embedding IS NULL;
