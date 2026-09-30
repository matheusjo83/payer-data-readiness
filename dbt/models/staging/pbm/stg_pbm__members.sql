-- PBM cardholders: parses the MM/DD/YYYY birth date and decodes gender.
select
    cardholder_id,
    trim(first_name)                                            as first_name,
    trim(last_name)                                             as last_name,
    try_strptime(nullif(trim(birth_date), ''), '%m/%d/%Y')::date as birth_date,
    case upper(trim(gender))
        when 'M' then 'male'
        when 'F' then 'female'
        when 'U' then 'unknown'
    end                                                         as gender,
    trim(zip_code)                                              as zip_code,
    trim(state)                                                 as state
from {{ source('pbm', 'members') }}
