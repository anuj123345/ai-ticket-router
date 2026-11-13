select
    md5(priority) as priority_key,
    priority,
    sort_order
from (values
    ('Critical', 1),
    ('High',     2),
    ('Medium',   3),
    ('Low',      4),
    ('Unknown',  9)
) as p(priority, sort_order)
