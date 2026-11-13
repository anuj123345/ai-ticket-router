-- Gold: one row per status change, read from the SCD Type 2 snapshot.
-- How long did each ticket sit in each status before it moved?

with history as (

    select
        ticket_id,
        status,
        department,
        priority,
        dbt_valid_from as valid_from,
        dbt_valid_to   as valid_to,
        lag(status) over (partition by ticket_id order by dbt_valid_from) as previous_status
    from {{ ref('snap_tickets') }}

),

-- A snapshot version is also written when only department or priority
-- changes. Keep the first version and real status moves.
status_moves as (

    select *
    from history
    where previous_status is null or previous_status <> status

)

select
    ticket_id,
    row_number() over w                                      as change_number,
    coalesce(previous_status, '(created)')                   as from_status,
    status                                                   as to_status,
    valid_from                                               as changed_at,
    round((extract(epoch from valid_from - lag(valid_from) over w) / 3600.0)::numeric, 4)
                                                             as hours_in_previous_status,
    md5(department)                                          as department_key,
    md5(priority)                                            as priority_key
from status_moves
window w as (partition by ticket_id order by valid_from)
