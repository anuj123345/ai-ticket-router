-- Silver: one row per Smart Search answer. Question and answer text stay in bronze.

select
    id                                                    as conversation_id,
    created_at,
    coalesce(is_outdated_flagged, false)                  as is_outdated_flagged,
    case when jsonb_typeof(sources) = 'array'
         then jsonb_array_length(sources) else 0 end      as source_count,
    -- Answered by the exact spreadsheet engine rather than from text excerpts.
    coalesce(sources::text like '%"dataset_id"%', false)  as answered_from_dataset
from {{ source('bronze', 'conversations') }}
