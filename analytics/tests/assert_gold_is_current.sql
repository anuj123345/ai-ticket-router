-- The incremental load missed nothing: every ticket's status, department and
-- priority in gold match silver right now.

select s.ticket_id
from {{ ref('stg_tickets') }} s
join {{ ref('fct_tickets') }} f using (ticket_id)
where f.status_key     <> md5(s.status)
   or f.department_key <> md5(s.department)
   or f.priority_key   <> md5(s.priority)
   or f.updated_at     <> s.updated_at
