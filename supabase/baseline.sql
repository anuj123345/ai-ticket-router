-- Baseline: the production tables that were created before migrations were
-- kept in this repo. Matches the live Supabase schema (checked 2026-11-13).
--
-- Used only to build a throwaway copy of the database for tests and CI:
--   psql -f supabase/baseline.sql, then every file in supabase/migrations/.
-- Never run this against production.

-- Supabase's built-in roles, so grants in the migrations work.
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'anon')          then create role anon nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role')  then create role service_role nologin bypassrls; end if;
end $$;

grant usage on schema public to anon, authenticated, service_role;

create table if not exists public.tickets (
  id              bigserial primary key,
  submitter       text not null,
  email           text not null,
  subject         text not null,
  description     text not null,
  department      text,
  priority        text,
  confidence      real,
  status          text default 'pending',
  assignee        text,
  draft_response  text,
  sources         text,
  created_at      timestamptz default now(),
  resolved_at     timestamptz,
  reasoning       text,
  assignee_team   text,
  assignee_email  text,
  assignee_slack  text,
  final_response  text
);

create table if not exists public.documents (
  id           bigserial primary key,
  name         text not null,
  file_type    text not null,
  size_bytes   integer,
  uploaded_by  text default 'admin',
  is_flagged   boolean default false,
  flag_reason  text,
  created_at   timestamptz default now()
);

create table if not exists public.doc_chunks (
  id             bigserial primary key,
  document_id    bigint references public.documents(id) on delete cascade,
  document_name  text not null,
  chunk_index    integer not null,
  content        text not null,
  search_vector  tsvector generated always as (to_tsvector('english', content)) stored,
  created_at     timestamptz default now(),
  embedding_model text
);

create or replace function public.search_docs(query_text text, max_results integer default 6)
returns table(id bigint, document_id bigint, document_name text, content text, rank real)
language sql
set search_path to 'public'
as $function$
  select dc.id, dc.document_id, dc.document_name, dc.content,
         ts_rank(dc.search_vector, websearch_to_tsquery('english', query_text)) as rank
  from doc_chunks dc
  where dc.search_vector @@ websearch_to_tsquery('english', query_text)
  order by rank desc
  limit max_results;
$function$;

-- The embedding column and vector search need pgvector; CI's Postgres image has it.
do $$
begin
  if exists (select 1 from pg_available_extensions where name = 'vector') then
    create extension if not exists vector;
    alter table public.doc_chunks add column if not exists embedding vector(1024);
    execute $f$
      create or replace function public.match_doc_chunks(
        query_embedding vector, match_threshold double precision default 0.3,
        match_count integer default 10, doc_name_filter text default null)
      returns table(id integer, document_id integer, document_name text, content text,
                    similarity double precision)
      language plpgsql
      set search_path to 'public'
      as $body$
      begin
        return query
        select dc.id::integer, dc.document_id::integer, dc.document_name, dc.content,
               1 - (dc.embedding <=> query_embedding) as similarity
        from doc_chunks dc
        where dc.embedding is not null
          and 1 - (dc.embedding <=> query_embedding) > match_threshold
          and (doc_name_filter is null or dc.document_name = doc_name_filter)
        order by dc.embedding <=> query_embedding
        limit match_count;
      end;
      $body$
    $f$;
  end if;
end $$;

create table if not exists public.conversations (
  id                  bigserial primary key,
  question            text not null,
  answer              text not null,
  sources             jsonb,
  is_outdated_flagged boolean default false,
  created_at          timestamptz default now()
);

create table if not exists public.feedback (
  id              bigserial primary key,
  conversation_id bigint references public.conversations(id) on delete cascade,
  rating          integer check (rating = any (array[1, -1])),
  is_flagged      boolean default false,
  flag_comment    text,
  document_id     bigint references public.documents(id) on delete set null,
  created_at      timestamptz default now()
);

create table if not exists public.verified_qa (
  id            bigserial primary key,
  question      text not null,
  answer        text not null,
  upvotes       integer default 1,
  created_at    timestamptz default now(),
  search_vector tsvector generated always as (to_tsvector('english', question || ' ' || answer)) stored
);

create table if not exists public.users (
  id            serial primary key,
  email         text not null unique,
  password_hash text not null,
  name          text not null,
  role          text default 'user' check (role = any (array['user', 'admin'])),
  created_at    timestamptz default now()
);

alter table public.tickets       enable row level security;
alter table public.documents     enable row level security;
alter table public.doc_chunks    enable row level security;
alter table public.conversations enable row level security;
alter table public.feedback      enable row level security;
alter table public.verified_qa   enable row level security;
alter table public.users         enable row level security;

grant all on all tables    in schema public to service_role;
grant all on all sequences in schema public to service_role;
