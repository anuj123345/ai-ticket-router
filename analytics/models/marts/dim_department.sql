-- Gold dimension. The key is a hash of the name, so it never changes between
-- rebuilds and the incremental fact table never points at a stale key.

with departments as (

    select department from (values
        ('IT'), ('HR'), ('Finance'), ('Engineering'), ('General'), ('Unknown')
    ) as known(department)

    union

    select distinct department from {{ ref('stg_tickets') }}

)

select
    md5(department) as department_key,
    department
from departments
