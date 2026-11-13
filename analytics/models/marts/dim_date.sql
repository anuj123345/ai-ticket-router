-- Gold date dimension: one row per day from the first ticket to a year ahead.
-- Gap-free, so daily charts and rolling averages never skip empty days.

with bounds as (

    select
        coalesce(min(created_at)::date, current_date) as first_day,
        (current_date + interval '365 days')::date    as last_day
    from {{ ref('stg_tickets') }}

)

select
    to_char(d, 'YYYYMMDD')::int        as date_key,
    d::date                            as date_day,
    extract(year from d)::int          as year,
    extract(quarter from d)::int       as quarter,
    extract(month from d)::int         as month,
    to_char(d, 'FMMonth')              as month_name,
    extract(isoyear from d)::int       as iso_year,
    extract(week from d)::int          as iso_week,
    extract(isodow from d)::int        as day_of_week,      -- 1 = Monday
    to_char(d, 'FMDay')                as day_name,
    extract(isodow from d) in (6, 7)   as is_weekend
from bounds,
     generate_series(bounds.first_day, bounds.last_day, interval '1 day') as d
