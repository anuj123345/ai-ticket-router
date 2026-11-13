{#
  Gold fact table: one row per ticket, loaded incrementally.

  Watermark: only tickets changed since the newest updated_at already loaded,
  minus a one-hour look-back for rows committed late. delete+insert on
  ticket_id makes re-processing those rows safe: running twice changes
  nothing.

  Hard deletes: an incremental load cannot see a deleted row, so the
  post-hook removes tickets that no longer exist in bronze.
#}
{{
  config(
    materialized = 'incremental',
    unique_key = 'ticket_id',
    incremental_strategy = 'delete+insert',
    on_schema_change = 'append_new_columns',
    post_hook = [
      "delete from {{ this }} f
        where not exists (select 1 from {{ source('bronze', 'tickets') }} t where t.id = f.ticket_id)",
      "create index if not exists {{ this.name }}_updated_at_idx on {{ this }} (updated_at)"
    ]
  )
}}

with tickets as (

    select * from {{ ref('stg_tickets') }}

    {% if is_incremental() %}
    where updated_at > (
        select coalesce(max(updated_at), '1900-01-01'::timestamptz) - interval '1 hour'
        from {{ this }}
    )
    {% endif %}

)

select
    t.ticket_id,
    to_char(t.created_at, 'YYYYMMDD')::int                      as created_date_key,
    case when t.resolved_at is not null
         then to_char(t.resolved_at, 'YYYYMMDD')::int end       as resolved_date_key,
    md5(t.department)                                           as department_key,
    md5(t.priority)                                             as priority_key,
    md5(t.status)                                               as status_key,
    t.assignee_team,
    t.confidence,
    t.has_draft,
    t.is_resolved,
    t.resolution_hours,
    t.created_at,
    t.resolved_at,
    t.updated_at
from tickets t
