-- Switch embeddings to a new model after NVIDIA retired nv-embedqa-e5-v5
-- (HTTP 410 since 2026-08-25).
--
-- Embeddings from different models live in different "spaces" and can't be
-- compared, so every existing embedding is cleared and re-created with the
-- new model via the admin page ("Finish indexing"). Chunk text is untouched,
-- so keyword search keeps working throughout.
--
-- Also records which model produced each embedding, so the next model change
-- can target exactly the stale rows.

-- 1. Remember which model made each embedding -------------------------------
alter table public.doc_chunks add column if not exists embedding_model text;

-- 2. Clear embeddings from the retired model ---------------------------------
-- Old vectors can't be compared with new-model query vectors; leaving them
-- would make semantic search return nonsense instead of nothing.
update public.doc_chunks
   set embedding = null, embedding_model = null
 where embedding is not null
   and embedding_model is distinct from 'nvidia/nemotron-3-embed-1b';

-- 3. Batch writer now also records the model --------------------------------
drop function if exists public.set_chunk_embeddings(jsonb);

create or replace function public.set_chunk_embeddings(items jsonb, model text)
returns integer
language plpgsql
security invoker
set search_path = public, pg_catalog
as $$
declare
  n integer;
begin
  update public.doc_chunks c
     set embedding       = (e->>'embedding')::vector,
         embedding_model = model
    from jsonb_array_elements(items) e
   where c.id = (e->>'id')::bigint
     and c.embedding is null;
  get diagnostics n = row_count;
  return n;
end;
$$;

revoke all on function public.set_chunk_embeddings(jsonb, text) from public, anon, authenticated;
grant execute on function public.set_chunk_embeddings(jsonb, text) to service_role;
