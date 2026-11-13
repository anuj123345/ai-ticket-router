-- Gold aggregate: tickets per day per department, with a running total and a
-- 7-day rolling average. Built on a full date x department grid so days with
-- no tickets count as 0 instead of disappearing (which would make a "7-day"
-- window cover more than 7 days).

with days as (

    select date_key, date_day
    from {{ ref('dim_date') }}
    where date_day <= current_date

),

departments as (

    select distinct department_key from {{ ref('fct_tickets') }}

),

grid as (

    select d.date_key, d.date_day, dep.department_key
    from days d
    cross join departments dep

),

created as (

    select created_date_key as date_key, department_key,
           count(*)                                   as tickets_created,
           count(*) filter (where status_key = md5('escalated')) as tickets_escalated
    from {{ ref('fct_tickets') }}
    group by 1, 2

),

resolved as (

    select resolved_date_key as date_key, department_key,
           count(*)               as tickets_resolved,
           avg(resolution_hours)  as avg_resolution_hours
    from {{ ref('fct_tickets') }}
    where resolved_date_key is not null
    group by 1, 2

),

daily as (

    select
        g.date_key,
        g.date_day,
        g.department_key,
        coalesce(c.tickets_created, 0)    as tickets_created,
        coalesce(c.tickets_escalated, 0)  as tickets_escalated,
        coalesce(r.tickets_resolved, 0)   as tickets_resolved,
        round(r.avg_resolution_hours, 2)  as avg_resolution_hours
    from grid g
    left join created  c on c.date_key = g.date_key and c.department_key = g.department_key
    left join resolved r on r.date_key = g.date_key and r.department_key = g.department_key

)

select
    daily.*,
    sum(tickets_created) over w_running                      as tickets_created_running_total,
    round(avg(tickets_created) over w_7d, 3)                 as tickets_created_7d_avg,
    sum(tickets_created - tickets_resolved) over w_running   as open_backlog_estimate
from daily
window
    w_running as (partition by department_key order by date_day
                  rows between unbounded preceding and current row),
    w_7d      as (partition by department_key order by date_day
                  rows between 6 preceding and current row)
