-- Silver: one row per rating. Free-text comments stay in bronze.

select
    id                              as feedback_id,
    conversation_id,
    rating,
    rating = 1                      as is_positive,
    coalesce(is_flagged, false)     as is_flagged,
    created_at
from {{ source('bronze', 'feedback') }}
where rating in (1, -1)
