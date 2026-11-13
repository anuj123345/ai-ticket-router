-- SCD2 integrity: at most one current version per ticket, and no version
-- whose validity window ends before it starts or overlaps the next one.

with versions as (
    select
        ticket_id,
        dbt_valid_from,
        dbt_valid_to,
        lead(dbt_valid_from) over (partition by ticket_id order by dbt_valid_from) as next_from
    from {{ ref('snap_tickets') }}
)
select ticket_id, 'more than one current version' as problem
from versions
where dbt_valid_to is null
group by ticket_id
having count(*) > 1

union all

select ticket_id, 'window ends before it starts'
from versions
where dbt_valid_to is not null and dbt_valid_to < dbt_valid_from

union all

select ticket_id, 'overlaps the next version'
from versions
where next_from is not null and (dbt_valid_to is null or dbt_valid_to > next_from)
