-- Resumable, batched embedding for uploaded documents.
--
-- Uploads now store chunks as text first (keyword search works immediately)
-- and embed them afterwards in short batches, so a large file can no longer
-- hit Vercel's 60s limit halfway through and leave the document half-indexed.

-- 1. Indexes ------------------------------------------------------------------
-- Covers the doc_chunks -> documents foreign key (flagged by the performance
-- advisor) and per-document lookups ordered by chunk_index.
create index if not exists doc_chunks_document_id_idx
  on public.doc_chunks (document_id, chunk_index);

-- Small index over only the chunks still waiting for an embedding.
create index if not exists doc_chunks_pending_embedding_idx
  on public.doc_chunks (document_id)
  where embedding is null;

-- 2. Per-document indexing status in one call ---------------------------------
-- Replaces one count query per document on the admin page.
create or replace function public.document_index_status()
returns table (document_id bigint, total_chunks bigint, embedded_chunks bigint)
language sql
stable
security invoker
set search_path = public, pg_catalog
as $$
  select c.document_id, count(*), count(c.embedding)
  from public.doc_chunks c
  group by c.document_id;
$$;

revoke all on function public.document_index_status() from public, anon, authenticated;
grant execute on function public.document_index_status() to service_role;

-- 3. Write a batch of embeddings in one round trip ----------------------------
-- items: [{"id": 123, "embedding": [0.1, ...]}, ...]
-- Only fills chunks that are still empty, so two overlapping workers can't
-- overwrite each other. Returns how many rows were updated.
create or replace function public.set_chunk_embeddings(items jsonb)
returns integer
language plpgsql
security invoker
set search_path = public, pg_catalog
as $$
declare
  n integer;
begin
  update public.doc_chunks c
     set embedding = (e->>'embedding')::vector
    from jsonb_array_elements(items) e
   where c.id = (e->>'id')::bigint
     and c.embedding is null;
  get diagnostics n = row_count;
  return n;
end;
$$;

revoke all on function public.set_chunk_embeddings(jsonb) from public, anon, authenticated;
grant execute on function public.set_chunk_embeddings(jsonb) to service_role;
