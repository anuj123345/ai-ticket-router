-- Guards against a silent failure: if row-level security hides bronze rows
-- from dbt_runner, every model builds "successfully" with zero rows and the
-- reconciliation test passes too (it sees the same zero).
-- Postgres's table statistics are not filtered by row-level security, so
-- "the table has rows but I see none" means access is broken.

with stats as (
    select relname, n_live_tup
    from pg_stat_user_tables
    where schemaname = 'public' and relname = 'tickets'
),
visible as (
    select count(*) as n from {{ source('bronze', 'tickets') }}
)
select stats.relname, stats.n_live_tup as rows_in_table, visible.n as rows_visible_to_dbt
from stats, visible
where stats.n_live_tup > 0 and visible.n = 0
