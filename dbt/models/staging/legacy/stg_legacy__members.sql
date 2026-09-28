-- One row per member: keeps the most recent version of duplicated records,
-- parses legacy VARCHAR dates and standardizes gender codes.
with ranked as (
    select
        *,
        row_number() over (partition by mbr_id order by upd_ts desc) as _rn,
        count(*)     over (partition by mbr_id)                      as _versions
    from {{ source('legacy', 'mbr_mstr') }}
)

select
    mbr_id                                         as member_id,
    trim(fst_nm)                                   as first_name,
    trim(lst_nm)                                   as last_name,
    try_strptime(nullif(trim(dob), ''), '%Y%m%d')::date as birth_date,
    case upper(trim(gndr_cd))
        when 'M' then 'male'
        when 'F' then 'female'
        when 'U' then 'unknown'
        else 'invalid'
    end                                            as gender,
    trim(zip_cd)                                   as zip_code,
    st_cd                                          as state,
    _versions > 1                                  as had_duplicates,
    upd_ts                                         as updated_at
from ranked
where _rn = 1
