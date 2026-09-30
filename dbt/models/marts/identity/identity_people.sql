-- Person records from both sources in one shape, normalized the same way for
-- matching: names lower case with letters only, gender only when male or female,
-- five-digit ZIP. first_name_canonical replaces a known nickname with the formal
-- name (seed first_name_nicknames). The legacy side is the deduplicated member table.
with records as (
    select 'legacy' as source_dataset, member_id as unique_id, first_name, last_name, birth_date,
           gender, zip_code, state
    from {{ ref('stg_legacy__members') }}
    union all
    select 'pbm', cardholder_id, first_name, last_name, birth_date, gender, zip_code, state
    from {{ ref('stg_pbm__members') }}
),


normalized as (
    select
        source_dataset,
        unique_id,
        nullif(regexp_replace(lower(first_name), '[^a-z]', '', 'g'), '')   as first_name,
        nullif(regexp_replace(lower(last_name), '[^a-z]', '', 'g'), '')    as last_name,
        birth_date,
        case when gender in ('male', 'female') then gender end             as gender,
        nullif(left(regexp_replace(zip_code, '[^0-9]', '', 'g'), 5), '')   as zip_code,
        upper(state)                                                       as state
    from records
)

select
    r.source_dataset,
    r.unique_id,
    r.first_name,
    coalesce(n.first_name, r.first_name)    as first_name_canonical,
    r.last_name,
    r.birth_date,
    r.gender,
    r.zip_code,
    r.state
from normalized r
left join {{ ref('first_name_nicknames') }} n on n.nickname = r.first_name
