-- Reconciliation: every ticket in bronze is in gold exactly once, and gold
-- has nothing bronze doesn't. Returns a row (= test fails) on any mismatch.

with bronze as (
    select count(*) as n from {{ source('bronze', 'tickets') }}
),
gold as (
    select count(*) as n, count(distinct ticket_id) as n_distinct from {{ ref('fct_tickets') }}
),
missing as (
    select count(*) as n
    from {{ source('bronze', 'tickets') }} t
    where not exists (select 1 from {{ ref('fct_tickets') }} f where f.ticket_id = t.id)
)
select bronze.n as bronze_rows, gold.n as gold_rows, gold.n_distinct, missing.n as missing_in_gold
from bronze, gold, missing
where bronze.n <> gold.n or gold.n <> gold.n_distinct or missing.n > 0
