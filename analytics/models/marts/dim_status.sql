select
    md5(status) as status_key,
    status,
    label,
    is_open
from (values
    ('pending',        'Pending',        true),
    ('pending_review', 'Pending review', true),
    ('escalated',      'Escalated',      true),
    ('resolved',       'Resolved',       false),
    ('unknown',        'Unknown',        true)
) as s(status, label, is_open)
