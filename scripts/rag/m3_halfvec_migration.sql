\set ON_ERROR_STOP on
\timing on
SET statement_timeout = 0;
SET maintenance_work_mem = '4GB';
SET max_parallel_maintenance_workers = 0;

\echo === BEFORE ===
SELECT (SELECT count(*) FROM public.chunks) AS chunks,
       (SELECT udt_name FROM information_schema.columns
        WHERE table_name='chunks' AND column_name='embedding') AS col_type,
       pg_size_pretty(pg_total_relation_size('public.chunks')) AS total,
       pg_size_pretty(pg_relation_size('public.idx_chunks_embedding')) AS ivfflat,
       pg_size_pretty(pg_database_size(current_database())) AS db;

-- One transaction on purpose. ALTER COLUMN TYPE would try to rebuild
-- idx_chunks_embedding with vector_cosine_ops, which does not exist for
-- halfvec, so the index must go first — and if the rebuild then failed outside
-- a transaction the corpus would be left converted with NO vector index at all,
-- silently seq-scanning 2.1 million rows. Atomic: either both, or neither.
BEGIN;
\echo === dropping the ivfflat (its opclass does not apply to halfvec) ===
DROP INDEX public.idx_chunks_embedding;
\echo === converting the column to float16 ===
ALTER TABLE public.chunks
  ALTER COLUMN embedding TYPE halfvec(768) USING embedding::halfvec(768);
\echo === rebuilding, halfvec_cosine_ops, lists=1449 = round(sqrt(2098293)) ===
CREATE INDEX idx_chunks_embedding ON public.chunks
  USING ivfflat (embedding halfvec_cosine_ops) WITH (lists = 1449);
COMMIT;

ANALYZE public.chunks;

\echo === AFTER ===
SELECT (SELECT count(*) FROM public.chunks) AS chunks,
       (SELECT udt_name FROM information_schema.columns
        WHERE table_name='chunks' AND column_name='embedding') AS col_type,
       pg_size_pretty(pg_total_relation_size('public.chunks')) AS total,
       pg_size_pretty(pg_relation_size('public.idx_chunks_embedding')) AS ivfflat,
       pg_size_pretty(pg_database_size(current_database())) AS db;
SELECT count(*) AS invalid_indexes FROM pg_index WHERE NOT indisvalid;
