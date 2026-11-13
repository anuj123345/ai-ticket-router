-- Silver: one clean row per ticket.
-- Names, emails, subject, description and responses stay in bronze.

with source as (

    select * from {{ source('bronze', 'tickets') }}

),

cleaned as (

    select
        id                                                        as ticket_id,

        coalesce(nullif(trim(department), ''), 'Unknown')         as department,

        case lower(trim(coalesce(priority, '')))
            when 'critical' then 'Critical'
            when 'high'     then 'High'
            when 'medium'   then 'Medium'
            when 'low'      then 'Low'
            else 'Unknown'
        end                                                       as priority,

        case
            when lower(trim(status)) in ('pending', 'pending_review', 'resolved', 'escalated')
                then lower(trim(status))
            else 'unknown'
        end                                                       as status,

        -- The classifier returns 0-1. Guard against a 0-100 value or noise.
        case
            when confidence is null  then null
            when confidence > 1      then least(confidence / 100.0, 1)
            when confidence < 0      then 0
            else confidence
        end::numeric(4, 3)                                        as confidence,

        nullif(trim(coalesce(draft_response, '')), '') is not null as has_draft,
        nullif(trim(coalesce(assignee_team, '')), '')             as assignee_team,

        created_at,
        resolved_at,
        updated_at

    from source

)

select
    *,
    status = 'resolved'                                           as is_resolved,
    case
        when resolved_at is not null and resolved_at >= created_at
            then round((extract(epoch from resolved_at - created_at) / 3600.0)::numeric, 4)
    end                                                           as resolution_hours
from cleaned
