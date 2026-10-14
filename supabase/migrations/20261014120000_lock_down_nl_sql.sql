-- Lock down model-written SQL (Ops Analytics) and remove string-built SQL
-- from the onboarding assistant.
--
-- Before: public.run_query was SECURITY DEFINER (ran as the table owner),
-- callable by anon/authenticated, and only checked that the text started
-- with "SELECT". Any caller, or any prompt-injected LLM output, could read
-- every table, including users.password_hash.
--
-- After:
--   * Model-written SQL runs as nlq_reader, a no-login role that can SELECT
--     public.tickets and nothing else (no users, documents, conversations...).
--   * public.run_query is a SECURITY INVOKER wrapper, executable by
--     service_role only.
--   * verified_qa lookup is a parameterised function, so user text is never
--     spliced into SQL.

-- 1. Read-only role for model-written SQL --------------------------------
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'nlq_reader') then
    create role nlq_reader nologin noinherit;
  end if;
end
$$;

-- postgres must be a member to hand ownership of the function to nlq_reader.
grant nlq_reader to postgres;

grant usage on schema public to nlq_reader;
grant select on public.tickets to nlq_reader;

-- tickets has RLS on; without a policy nlq_reader would see zero rows.
drop policy if exists nlq_reader_read_tickets on public.tickets;
create policy nlq_reader_read_tickets on public.tickets
  for select to nlq_reader using (true);

-- 2. Private schema holding the function that actually runs the SQL ------
-- Kept out of the exposed API schemas so it can't be reached over REST.
create schema if not exists nlq;
revoke all on schema nlq from public;
grant usage, create on schema nlq to nlq_reader;   -- create: needed to own the function
grant usage on schema nlq to service_role;

create or replace function nlq.run_select(query_text text)
returns jsonb
language plpgsql
security definer                      -- runs as owner = nlq_reader
set search_path = public, pg_catalog
as $$
declare
  q      text := btrim(query_text);
  result jsonb;
begin
  -- strip one trailing semicolon, then refuse anything that could stack statements
  q := regexp_replace(q, ';\s*$', '');
  if position(';' in q) > 0 then
    raise exception 'Only a single SELECT statement is allowed';
  end if;
  if upper(q) not like 'SELECT%' then
    raise exception 'Only SELECT queries are allowed';
  end if;

  execute 'select coalesce(jsonb_agg(row_to_json(t)), ''[]''::jsonb) from (' || q || ') t'
    into result;
  return result;
end;
$$;

alter function nlq.run_select(text) owner to nlq_reader;
revoke all on function nlq.run_select(text) from public, anon, authenticated;
grant execute on function nlq.run_select(text) to service_role;

-- 3. Public entry point the app calls via supabase-py rpc("run_query") ----
create or replace function public.run_query(query_text text)
returns jsonb
language sql
security invoker
set search_path = public, pg_catalog
as $$
  select nlq.run_select(query_text);
$$;

revoke all on function public.run_query(text) from public, anon, authenticated;
grant execute on function public.run_query(text) to service_role;

-- 4. Parameterised verified-answer search (replaces string-built SQL) ----
create or replace function public.search_verified_qa(query_text text, max_results integer default 3)
returns table (question text, answer text, upvotes integer)
language sql
stable
security invoker
set search_path = public, pg_catalog
as $$
  select v.question, v.answer, v.upvotes
  from public.verified_qa v
  where v.search_vector @@ websearch_to_tsquery('english', query_text)
  order by v.upvotes desc
  limit least(greatest(coalesce(max_results, 3), 1), 20);
$$;

revoke all on function public.search_verified_qa(text, integer) from public, anon, authenticated;
grant execute on function public.search_verified_qa(text, integer) to service_role;

-- 5. Backend-only search functions: no reason for anon/authenticated access
revoke all on function public.search_docs(text, integer) from public, anon, authenticated;
grant execute on function public.search_docs(text, integer) to service_role;
revoke all on function public.match_doc_chunks(vector, double precision, integer, text) from public, anon, authenticated;
grant execute on function public.match_doc_chunks(vector, double precision, integer, text) to service_role;
