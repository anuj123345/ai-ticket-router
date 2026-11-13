-- The daily aggregate adds up to the fact table (tickets created up to today).

with agg as (
    select coalesce(sum(tickets_created), 0) as n from {{ ref('agg_tickets_daily') }}
),
fct as (
    select count(*) as n from {{ ref('fct_tickets') }} where created_at::date <= current_date
)
select agg.n as agg_total, fct.n as fact_total
from agg, fct
where agg.n <> fct.n
