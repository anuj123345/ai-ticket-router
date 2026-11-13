-- Data platform under Solace: medallion layers, pipeline run log, idempotent
-- uploads.
--
--   bronze    the app's own tables in public (tickets, feedback, ...), as written
--   silver    cleaned, typed, PII removed           (built by dbt, analytics/)
--   gold      star schema + aggregates for reporting (built by dbt)
--   snapshots SCD Type 2 history of tickets          (built by dbt)
--
-- dbt connects as its own role, dbt_runner, which can read bronze and write
-- only its own schemas. The app's API (PostgREST) never sees those schemas.
--
-- Adds only. Safe to re-run.

-- 1. tickets.updated_at: the watermark for incremental loads and snapshots ----

alter table public.tickets add column if not exists updated_at timestamptz;

update public.tickets
   set updated_at = coalesce(resolved_at, created_at, now())
 where updated_at is null;

alter table public.tickets alter column updated_at set default now();
alter table public.tickets alter column updated_at set not null;

create or replace function public.touch_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists tickets_touch_updated_at on public.tickets;
create trigger tickets_touch_updated_at
  before update on public.tickets
  for each row execute function public.touch_updated_at();

create index if not exists tickets_updated_at_idx on public.tickets (updated_at);

-- 2. documents.content_hash: the same file uploaded twice is stored once ------

alter table public.documents add column if not exists content_hash text;

create unique index if not exists documents_content_hash_key
  on public.documents (content_hash) where content_hash is not null;

create index if not exists documents_name_idx on public.documents (name);

-- 3. Pipeline run log ---------------------------------------------------------
-- One row per run of any pipeline: an upload, a dbt build. The admin
-- Pipeline page reads it; failures are visible there and in GitHub Actions.

create table if not exists public.pipeline_runs (
  id           bigint generated always as identity primary key,
  pipeline     text not null,                 -- 'upload', 'dbt'
  run_key      text,                          -- dbt invocation id, file name...
  status       text not null check (status in ('running', 'success', 'failed', 'skipped')),
  started_at   timestamptz not null default now(),
  finished_at  timestamptz,
  rows_in      bigint,
  rows_out     bigint,
  details      jsonb not null default '{}'::jsonb,
  error        text
);

create index if not exists pipeline_runs_pipeline_started_idx
  on public.pipeline_runs (pipeline, started_at desc);

alter table public.pipeline_runs enable row level security;
-- No policies: only the backend (service_role) and dbt_runner use it.
revoke all on public.pipeline_runs from anon, authenticated;
grant select, insert, update on public.pipeline_runs to service_role;

-- 4. dbt_runner role and its schemas ------------------------------------------
-- Created without a login. To let dbt connect, set a password once
-- (see analytics/README.md):  alter role dbt_runner with login password '...';

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'dbt_runner') then
    create role dbt_runner nologin;
  end if;
end $$;

-- The admin running this (postgres on Supabase) must be a member to hand
-- schemas to dbt_runner, and pipeline_status() below, which the admin owns,
-- reads gold through that membership.
do $$ begin execute format('grant dbt_runner to %I', current_user); end $$;

create schema if not exists silver;
create schema if not exists gold;
create schema if not exists snapshots;

alter schema silver    owner to dbt_runner;
alter schema gold      owner to dbt_runner;
alter schema snapshots owner to dbt_runner;

revoke all on schema silver, gold, snapshots from public, anon, authenticated;

-- Read bronze: only the tables the models use. No users, no documents' text.
-- These tables have row-level security on and no policies, so without a
-- policy dbt_runner would see zero rows - silently, not as an error.
grant usage on schema public to dbt_runner;
grant select on public.tickets, public.conversations, public.feedback to dbt_runner;

drop policy if exists dbt_runner_read on public.tickets;
create policy dbt_runner_read on public.tickets       for select to dbt_runner using (true);
drop policy if exists dbt_runner_read on public.conversations;
create policy dbt_runner_read on public.conversations for select to dbt_runner using (true);
drop policy if exists dbt_runner_read on public.feedback;
create policy dbt_runner_read on public.feedback      for select to dbt_runner using (true);

-- Write its own run log rows, and nothing else there.
revoke all on public.pipeline_runs from dbt_runner;
grant insert on public.pipeline_runs to dbt_runner;
drop policy if exists dbt_runner_log on public.pipeline_runs;
create policy dbt_runner_log on public.pipeline_runs
  for insert to dbt_runner with check (pipeline = 'dbt');

-- The app reads gold only through this function (service_role only).
create or replace function public.pipeline_status()
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  result jsonb;
  gold   jsonb;
begin
  select jsonb_build_object(
    'runs', coalesce((
      select jsonb_agg(r order by r.started_at desc)
        from (select id, pipeline, run_key, status, started_at, finished_at,
                     rows_in, rows_out, details, error
                from public.pipeline_runs
               order by started_at desc
               limit 30) r), '[]'::jsonb),
    'last_dbt', (
      select to_jsonb(r)
        from (select status, started_at, finished_at, details, error
                from public.pipeline_runs
               where pipeline = 'dbt'
               order by started_at desc
               limit 1) r),
    'bronze_tickets', (select count(*) from public.tickets),
    'bronze_max_updated_at', (select max(updated_at) from public.tickets)
  ) into result;

  -- Gold tables exist only after the first dbt run.
  if to_regclass('gold.fct_tickets') is not null then
    execute $q$
      select jsonb_build_object(
        'gold_tickets', (select count(*) from gold.fct_tickets),
        'gold_max_updated_at', (select max(updated_at) from gold.fct_tickets),
        'by_department', (
          select coalesce(jsonb_agg(d order by d.tickets desc), '[]'::jsonb)
            from (select dd.department, count(*) as tickets,
                         count(*) filter (where f.is_resolved) as resolved,
                         round(avg(f.resolution_hours)::numeric, 2) as avg_resolution_hours
                    from gold.fct_tickets f
                    join gold.dim_department dd using (department_key)
                   group by dd.department) d)
      )$q$ into gold;
    result := result || gold;
  end if;
  return result;
end;
$$;

revoke all on function public.pipeline_status() from public, anon, authenticated;
grant execute on function public.pipeline_status() to service_role;
