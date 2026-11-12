-- Data Studio: exact, database-side calculation engine.
--
-- Before: rows were stored as loose JSON, downloaded to Python on every click,
-- summed as floats, typed from a 50-row sample, and capped at 20,000 rows.
--
-- After:
--   datasets          one per imported sheet, with its import report
--   dataset_columns   each column's type, default calculation and statistics
--   dataset_records   every non-blank row of the file, with its file row
--                     number, the text as it was in the file (raw) and the
--                     value as it was understood (val)
--   ds_query()        counts / sums / averages computed by Postgres on exact
--                     numeric values; blanks grouped as their own bucket;
--                     "Other" computed from the real rows, not from the chart
--   ds_rows()         the rows behind any number
--
-- Adds only. Existing tables and data are not changed. Safe to re-run.

-- 1. Tables -------------------------------------------------------------------

create table if not exists public.datasets (
  id            bigint generated always as identity primary key,
  document_id   bigint not null references public.documents(id) on delete cascade,
  document_name text   not null,
  sheet_name    text   not null default '',
  sheet_index   integer not null default 0,
  header_row    integer not null default 1,
  row_count     integer not null default 0,
  options       jsonb  not null default '{}'::jsonb,
  report        jsonb  not null default '{}'::jsonb,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

create index if not exists datasets_document_id_idx on public.datasets (document_id);

create table if not exists public.dataset_columns (
  dataset_id    bigint  not null references public.datasets(id) on delete cascade,
  pos           integer not null,
  name          text    not null,
  type          text    not null check (type in ('number', 'date', 'text')),
  detected_type text    not null check (detected_type in ('number', 'date', 'text')),
  default_agg   text    not null check (default_agg in ('sum', 'avg', 'count', 'none')),
  format        text    not null default 'plain',
  stats         jsonb   not null default '{}'::jsonb,
  warnings      jsonb   not null default '[]'::jsonb,
  primary key (dataset_id, pos)
);

create table if not exists public.dataset_records (
  dataset_id bigint  not null references public.datasets(id) on delete cascade,
  row_num    integer not null,                 -- row number in the uploaded file
  status     text    not null default 'data'
             check (status in ('data', 'header', 'above_header', 'total')),
  raw        jsonb   not null,                 -- cells as text, exactly as read
  val        jsonb,                            -- cells as understood (data rows only)
  primary key (dataset_id, row_num)
);

-- Backend-only tables: RLS on with no policies, like the rest of the schema.
alter table public.datasets        enable row level security;
alter table public.dataset_columns enable row level security;
alter table public.dataset_records enable row level security;

-- 2. Expression builders (private) --------------------------------------------
-- Column positions are integers and every user-supplied value goes through
-- format('%L'), so nothing from the request is ever spliced in as SQL.

-- A column as text, for grouping and equality filters. NULL = blank.
create or replace function public.ds__dim(p_types jsonb, p_pos integer, p_grain text)
returns text
language plpgsql immutable
set search_path = pg_catalog
as $fn$
declare
  t    text := p_types ->> p_pos::text;
  cell text := format('(r.val ->> %s)', p_pos);
  d    text;
begin
  if t is null then
    raise exception 'Unknown column position %', p_pos;
  end if;
  if t = 'date' then
    d := format('(case when %1$s ~ ''^[0-9]{4}-[0-9]{2}-[0-9]{2}'' then left(%1$s, 10)::date end)', cell);
    case coalesce(p_grain, 'day')
      when 'day'     then return format('to_char(%s, ''YYYY-MM-DD'')', d);
      when 'month'   then return format('to_char(%s, ''YYYY-MM'')', d);
      when 'quarter' then return format('to_char(%s, ''YYYY-"Q"Q'')', d);
      when 'year'    then return format('to_char(%s, ''YYYY'')', d);
      else raise exception 'Unknown date grain %', p_grain;
    end case;
  end if;
  return format('nullif(%s, '''')', cell);
end;
$fn$;

-- A column as its real type (numeric / date / text), for maths and comparisons.
create or replace function public.ds__val(p_types jsonb, p_pos integer)
returns text
language plpgsql immutable
set search_path = pg_catalog
as $fn$
declare
  t    text := p_types ->> p_pos::text;
  cell text := format('(r.val ->> %s)', p_pos);
begin
  if t is null then
    raise exception 'Unknown column position %', p_pos;
  end if;
  if t = 'number' then
    return format('(case when %1$s ~ ''^-?[0-9]+(\.[0-9]+)?$'' then %1$s::numeric end)', cell);
  elsif t = 'date' then
    return format('(case when %1$s ~ ''^[0-9]{4}-[0-9]{2}-[0-9]{2}'' then left(%1$s, 10)::date end)', cell);
  end if;
  return format('nullif(%s, '''')', cell);
end;
$fn$;

-- WHERE clause from a list of filters:
--   {"col": 2, "op": "in",       "values": ["India", null]}      null = blank
--   {"col": 2, "op": "not_in",   "values": [...]}
--   {"col": 5, "grain": "quarter", "op": "in", "values": ["2014-Q1"]}
--   {"col": 7, "op": "gte" | "gt" | "lte" | "lt" | "eq" | "ne", "value": "100"}
--   {"col": 7, "op": "between",  "from": "10", "to": "20"}        inclusive
--   {"col": 1, "op": "contains", "value": "abc"}
--   {"col": 1, "op": "blank" | "not_blank"}
create or replace function public.ds__where(p_types jsonb, p_filters jsonb)
returns text
language plpgsql immutable
set search_path = public, pg_catalog
as $fn$
declare
  f      jsonb;
  v_sql  text := '';
  v_pos  integer;
  v_op   text;
  v_type text;
  v_dim  text;
  v_val  text;
  v_cast text;
  v_arr  text[];
  v_null boolean;
  v_cond text;
begin
  if p_filters is null or jsonb_typeof(p_filters) <> 'array' then
    return '';
  end if;
  for f in select * from jsonb_array_elements(p_filters) loop
    v_pos  := (f ->> 'col')::integer;
    v_op   := f ->> 'op';
    v_type := p_types ->> v_pos::text;
    v_dim  := ds__dim(p_types, v_pos, f ->> 'grain');
    v_val  := ds__val(p_types, v_pos);
    v_cast := case v_type when 'number' then 'numeric' when 'date' then 'date' else 'text' end;

    if v_op in ('in', 'not_in') then
      select coalesce(array_agg(e.value #>> '{}') filter (where jsonb_typeof(e.value) <> 'null'), '{}'),
             coalesce(bool_or(jsonb_typeof(e.value) = 'null'), false)
        into v_arr, v_null
        from jsonb_array_elements(coalesce(f -> 'values', '[]'::jsonb)) e;
      v_cond := format('coalesce(%s = any(%L::text[]), false)', v_dim, v_arr);
      if v_null then
        v_cond := format('(%s or %s is null)', v_cond, v_dim);
      end if;
      if v_op = 'not_in' then
        v_cond := format('not (%s)', v_cond);
      end if;
    elsif v_op in ('gt', 'gte', 'lt', 'lte', 'eq', 'ne') then
      v_cond := format('%s %s %L::%s', v_val,
                       case v_op when 'gt' then '>' when 'gte' then '>=' when 'lt' then '<'
                                 when 'lte' then '<=' when 'eq' then '=' else '<>' end,
                       f ->> 'value', v_cast);
    elsif v_op = 'between' then
      v_cond := 'true';
      if f ->> 'from' is not null then
        v_cond := v_cond || format(' and %s >= %L::%s', v_val, f ->> 'from', v_cast);
      end if;
      if f ->> 'to' is not null then
        v_cond := v_cond || format(' and %s <= %L::%s', v_val, f ->> 'to', v_cast);
      end if;
      v_cond := '(' || v_cond || ')';
    elsif v_op = 'contains' then
      v_cond := format('position(lower(%L) in lower(coalesce(%s, ''''))) > 0', f ->> 'value', v_dim);
    elsif v_op = 'blank' then
      v_cond := format('%s is null', v_dim);
    elsif v_op = 'not_blank' then
      v_cond := format('%s is not null', v_dim);
    else
      raise exception 'Unknown filter operator %', v_op;
    end if;
    v_sql := v_sql || ' and ' || v_cond;
  end loop;
  return v_sql;
end;
$fn$;

-- 3. One query ----------------------------------------------------------------
-- spec:
--   {"filters":  [...],
--    "group_by": {"col": 3, "grain": "month"}      optional
--    "measures": [{"agg": "sum", "col": 7}, {"agg": "count_rows"}],
--    "limit":    8                                  optional: keep the top N groups by
--                                                   the first measure; the rest become
--                                                   one "Other" group computed from
--                                                   their rows
--   }
-- aggs: count_rows | count (filled cells) | distinct | sum | avg | min | max
--
-- result:
--   {"groups": [{"g": "India" | null, "other": false, "n": 140, "m": ["1234.50", "140"]}],
--    "total":  {"n": 700, "m": [...]},     the same measures over all matching rows
--    "group_count": 5}
-- Measures are returned as text so no precision is lost in transit.
create or replace function public.ds__run(p_dataset_id bigint, p_types jsonb, p_spec jsonb)
returns jsonb
language plpgsql stable
set search_path = public, pg_catalog
as $fn$
declare
  m        jsonb;
  i        integer := 0;
  v_agg    text;
  v_pos    integer;
  v_type   text;
  v_inputs text := '';
  v_aggs   text := '';
  v_marr   text := '';
  v_where  text := ds__where(p_types, p_spec -> 'filters');
  v_group  jsonb := p_spec -> 'group_by';
  v_dim    text;
  v_limit  bigint := coalesce((p_spec ->> 'limit')::bigint, 9223372036854775807);
  v_sql    text;
  v_out    jsonb;
begin
  if jsonb_typeof(p_spec -> 'measures') is distinct from 'array'
     or jsonb_array_length(p_spec -> 'measures') = 0 then
    raise exception 'A query needs at least one measure';
  end if;
  if v_limit < 1 then
    raise exception 'limit must be at least 1';
  end if;

  for m in select * from jsonb_array_elements(p_spec -> 'measures') loop
    i := i + 1;
    v_agg := m ->> 'agg';
    if v_agg = 'count_rows' then
      v_inputs := v_inputs || format(', 1 as x%s', i);
      v_aggs   := v_aggs   || format(', count(*) as m%s', i);
    else
      v_pos  := (m ->> 'col')::integer;
      v_type := p_types ->> v_pos::text;
      if v_type is null then
        raise exception 'Unknown column position %', v_pos;
      end if;
      if v_agg in ('count', 'distinct') then
        v_inputs := v_inputs || format(', %s as x%s', ds__dim(p_types, v_pos, null), i);
        v_aggs   := v_aggs   || format(', count(%s x%s) as m%s',
                                       case when v_agg = 'distinct' then 'distinct' else '' end, i, i);
      elsif v_agg in ('sum', 'avg') then
        if v_type <> 'number' then
          raise exception 'Cannot % a % column (position %)', v_agg, v_type, v_pos;
        end if;
        v_inputs := v_inputs || format(', %s as x%s', ds__val(p_types, v_pos), i);
        v_aggs   := v_aggs   || format(', %s(x%s) as m%s', v_agg, i, i);
      elsif v_agg in ('min', 'max') then
        if v_type = 'text' then
          raise exception 'Cannot take % of a text column (position %)', v_agg, v_pos;
        end if;
        v_inputs := v_inputs || format(', %s as x%s', ds__val(p_types, v_pos), i);
        v_aggs   := v_aggs   || format(', %s(x%s) as m%s', v_agg, i, i);
      else
        raise exception 'Unknown aggregation %', v_agg;
      end if;
    end if;
    v_marr := v_marr || case when i > 1 then ', ' else '' end || format('m%s::text', i);
  end loop;

  if v_group is null or jsonb_typeof(v_group) = 'null' then
    v_sql := format($q$
      with base as (
        select 1 as one %1$s
        from dataset_records r
        where r.dataset_id = $1 and r.status = 'data' %2$s
      ),
      tot as (select count(*) as n %3$s from base)
      select jsonb_build_object(
        'groups', '[]'::jsonb,
        'total', (select jsonb_build_object('n', n, 'm', jsonb_build_array(%4$s)) from tot),
        'group_count', 0)
    $q$, v_inputs, v_where, v_aggs, v_marr);
  else
    v_dim := ds__dim(p_types, (v_group ->> 'col')::integer, v_group ->> 'grain');
    -- gk is a join key that is never NULL, so the blank group joins like any other.
    v_sql := format($q$
      with base as (
        select %1$s as g %2$s
        from dataset_records r
        where r.dataset_id = $1 and r.status = 'data' %3$s
      ),
      keyed as (select b.*, coalesce('v:' || b.g, 'null') as gk from base b),
      grp as (select gk, min(g) as g, count(*) as n %4$s from keyed group by gk),
      ranked as (
        select gk, row_number() over (order by m1 desc nulls last, n desc, g collate "C" asc nulls last) as rn
        from grp
      ),
      bucketed as (
        select k.*, (rk.rn > $2) as is_other, rk.rn
        from keyed k join ranked rk using (gk)
      ),
      fin as (
        select is_other,
               case when is_other then null else gk end as gk,
               min(case when is_other then null else g end) as g,
               min(rn) as rn,
               count(*) as n %4$s
        from bucketed
        group by 1, 2
      ),
      tot as (select count(*) as n %4$s from base)
      select jsonb_build_object(
        'groups', coalesce((
            select jsonb_agg(jsonb_build_object(
                     'g', g, 'other', is_other, 'n', n, 'm', jsonb_build_array(%5$s))
                   order by is_other, rn)
            from fin), '[]'::jsonb),
        'total', (select jsonb_build_object('n', n, 'm', jsonb_build_array(%5$s)) from tot),
        'group_count', (select count(*) from grp))
    $q$, v_dim, v_inputs, v_where, v_aggs, v_marr);
  end if;

  execute v_sql into v_out using p_dataset_id, v_limit;
  return v_out;
end;
$fn$;

-- 4. Public entry points (backend only) ---------------------------------------

-- Run several queries in one round trip. Returns a JSON array, one result per spec.
create or replace function public.ds_query(p_dataset_id bigint, p_specs jsonb)
returns jsonb
language plpgsql stable security invoker
set search_path = public, pg_catalog
as $fn$
declare
  v_types jsonb;
  v_spec  jsonb;
  v_out   jsonb := '[]'::jsonb;
begin
  select jsonb_object_agg(c.pos::text, c.type) into v_types
  from dataset_columns c where c.dataset_id = p_dataset_id;
  if v_types is null then
    raise exception 'Dataset % not found', p_dataset_id;
  end if;
  for v_spec in select * from jsonb_array_elements(p_specs) loop
    v_out := v_out || jsonb_build_array(ds__run(p_dataset_id, v_types, v_spec));
  end loop;
  return v_out;
end;
$fn$;

-- The rows behind a number: {"filters": [...], "limit": 100, "offset": 0}
-- Returns {"matched": 140, "rows": [{"row": 12, "cells": [...]}]} with cells as in the file.
create or replace function public.ds_rows(p_dataset_id bigint, p_spec jsonb)
returns jsonb
language plpgsql stable security invoker
set search_path = public, pg_catalog
as $fn$
declare
  v_types  jsonb;
  v_where  text;
  v_limit  integer := least(greatest(coalesce((p_spec ->> 'limit')::integer, 100), 1), 500);
  v_offset integer := greatest(coalesce((p_spec ->> 'offset')::integer, 0), 0);
  v_out    jsonb;
begin
  select jsonb_object_agg(c.pos::text, c.type) into v_types
  from dataset_columns c where c.dataset_id = p_dataset_id;
  if v_types is null then
    raise exception 'Dataset % not found', p_dataset_id;
  end if;
  v_where := ds__where(v_types, p_spec -> 'filters');
  execute format($q$
    with hit as (
      select r.row_num, r.raw
      from dataset_records r
      where r.dataset_id = $1 and r.status = 'data' %s
    )
    select jsonb_build_object(
      'matched', (select count(*) from hit),
      'rows', coalesce((
          select jsonb_agg(jsonb_build_object('row', p.row_num, 'cells', p.raw) order by p.row_num)
          from (select * from hit order by row_num limit $2 offset $3) p), '[]'::jsonb))
  $q$, v_where) into v_out using p_dataset_id, v_limit, v_offset;
  return v_out;
end;
$fn$;

-- Used when column settings change: read rows back, and write new values.
create or replace function public.ds_get_records(p_dataset_id bigint, p_after integer, p_limit integer)
returns jsonb
language sql stable security invoker
set search_path = public, pg_catalog
as $fn$
  select coalesce(jsonb_agg(jsonb_build_array(p.row_num, p.raw) order by p.row_num), '[]'::jsonb)
  from (
    select r.row_num, r.raw
    from dataset_records r
    where r.dataset_id = p_dataset_id and r.row_num > p_after
    order by r.row_num
    limit least(greatest(p_limit, 1), 10000)
  ) p;
$fn$;

-- p_items: [[row_num, status, val], ...]
create or replace function public.ds_put_records(p_dataset_id bigint, p_items jsonb)
returns integer
language plpgsql security invoker
set search_path = public, pg_catalog
as $fn$
declare
  n integer;
begin
  update dataset_records r
     set status = e.item ->> 1,
         val    = nullif(e.item -> 2, 'null'::jsonb)
    from jsonb_array_elements(p_items) as e(item)
   where r.dataset_id = p_dataset_id
     and r.row_num = (e.item ->> 0)::integer;
  get diagnostics n = row_count;
  return n;
end;
$fn$;

-- 5. Permissions: backend (service_role) only ---------------------------------
revoke all on function public.ds__dim(jsonb, integer, text)          from public, anon, authenticated;
revoke all on function public.ds__val(jsonb, integer)                from public, anon, authenticated;
revoke all on function public.ds__where(jsonb, jsonb)                from public, anon, authenticated;
revoke all on function public.ds__run(bigint, jsonb, jsonb)          from public, anon, authenticated;
revoke all on function public.ds_query(bigint, jsonb)                from public, anon, authenticated;
revoke all on function public.ds_rows(bigint, jsonb)                 from public, anon, authenticated;
revoke all on function public.ds_get_records(bigint, integer, integer) from public, anon, authenticated;
revoke all on function public.ds_put_records(bigint, jsonb)          from public, anon, authenticated;

grant execute on function public.ds__dim(jsonb, integer, text)          to service_role;
grant execute on function public.ds__val(jsonb, integer)                to service_role;
grant execute on function public.ds__where(jsonb, jsonb)                to service_role;
grant execute on function public.ds__run(bigint, jsonb, jsonb)          to service_role;
grant execute on function public.ds_query(bigint, jsonb)                to service_role;
grant execute on function public.ds_rows(bigint, jsonb)                 to service_role;
grant execute on function public.ds_get_records(bigint, integer, integer) to service_role;
grant execute on function public.ds_put_records(bigint, jsonb)          to service_role;

revoke all on public.datasets, public.dataset_columns, public.dataset_records from anon, authenticated;
grant all on public.datasets, public.dataset_columns, public.dataset_records to service_role;
