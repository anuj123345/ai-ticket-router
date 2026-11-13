{{ config(severity = 'warn') }}

-- Data quality warning: a resolved time earlier than the created time is a
-- source problem worth seeing, but should not stop the load.

select ticket_id, created_at, resolved_at
from {{ ref('stg_tickets') }}
where resolved_at is not null and resolved_at < created_at
