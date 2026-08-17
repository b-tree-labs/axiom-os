-- Domain Consumer RAG Database Schema
-- PostgreSQL 16 + pgvector

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Document chunks with embeddings for RAG
CREATE TABLE IF NOT EXISTS chunks (
    id BIGSERIAL PRIMARY KEY,

    -- Source document info
    source_path TEXT NOT NULL,          -- relative path from repo root
    source_title TEXT DEFAULT '',       -- extracted document title
    source_type TEXT DEFAULT 'markdown', -- markdown, pdf, docx, transcript

    -- Chunk content
    chunk_text TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,       -- position within document
    start_line INTEGER DEFAULT 0,

    -- Embedding (1536 dimensions for text-embedding-3-small, 1024 for voyage)
    embedding vector(1536),

    -- Knowledge tier: personal, team, institutional
    tier TEXT NOT NULL DEFAULT 'institutional',
    owner TEXT DEFAULT NULL,            -- EID for personal tier
    team TEXT DEFAULT NULL,             -- team slug for team tier

    -- Metadata
    checksum TEXT,                      -- MD5 of source file (for change detection)
    indexed_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),

    -- Dedup constraint
    UNIQUE(source_path, chunk_index, tier, COALESCE(owner, ''))
);

-- Indexes for fast retrieval
CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks (source_path);
CREATE INDEX IF NOT EXISTS idx_chunks_tier ON chunks (tier);
CREATE INDEX IF NOT EXISTS idx_chunks_owner ON chunks (owner) WHERE owner IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_chunks_text_trgm ON chunks USING gin (chunk_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_chunks_title_trgm ON chunks USING gin (source_title gin_trgm_ops);

-- Full-text search
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', chunk_text)) STORED;
CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING gin (tsv);

-- Document registry (tracks what's been indexed)
CREATE TABLE IF NOT EXISTS documents (
    id BIGSERIAL PRIMARY KEY,
    source_path TEXT NOT NULL UNIQUE,
    source_type TEXT DEFAULT 'markdown',
    title TEXT DEFAULT '',
    checksum TEXT,                      -- current file checksum
    chunk_count INTEGER DEFAULT 0,
    tier TEXT NOT NULL DEFAULT 'institutional',
    owner TEXT DEFAULT NULL,
    first_indexed TIMESTAMPTZ DEFAULT NOW(),
    last_indexed TIMESTAMPTZ DEFAULT NOW()
);

-- Search function: hybrid vector + full-text
CREATE OR REPLACE FUNCTION search_chunks(
    query_embedding vector(1536),
    query_text TEXT DEFAULT '',
    match_tier TEXT DEFAULT 'institutional',
    match_owner TEXT DEFAULT NULL,
    match_limit INTEGER DEFAULT 5
) RETURNS TABLE (
    id BIGINT,
    source_path TEXT,
    source_title TEXT,
    chunk_text TEXT,
    chunk_index INTEGER,
    similarity FLOAT,
    text_rank FLOAT,
    combined_score FLOAT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        c.id,
        c.source_path,
        c.source_title,
        c.chunk_text,
        c.chunk_index,
        1 - (c.embedding <=> query_embedding) AS similarity,
        COALESCE(ts_rank(c.tsv, plainto_tsquery('english', query_text)), 0) AS text_rank,
        (0.7 * (1 - (c.embedding <=> query_embedding))) +
        (0.3 * COALESCE(ts_rank(c.tsv, plainto_tsquery('english', query_text)), 0)) AS combined_score
    FROM chunks c
    WHERE c.tier = match_tier
        OR (c.tier = 'personal' AND c.owner = match_owner)
        OR (c.tier = 'team')
    ORDER BY combined_score DESC
    LIMIT match_limit;
END;
$$ LANGUAGE plpgsql;
