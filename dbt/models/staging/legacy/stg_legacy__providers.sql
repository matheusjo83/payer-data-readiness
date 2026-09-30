-- Providers with decoded specialty and NPI checks: the format (10 digits) and
-- the check digit (Luhn algorithm, see macros/npi_check_digit_valid.sql).
select
    prv_id                                              as provider_id,
    npi,
    regexp_full_match(coalesce(npi, ''), '[0-9]{10}')   as npi_format_valid,
    coalesce({{ npi_check_digit_valid('npi') }}, false) as npi_valid,
    trim(prv_nm)                                        as provider_name,
    case spclty_cd
        when 'FM'   then 'family_medicine'
        when 'IM'   then 'internal_medicine'
        when 'PED'  then 'pediatrics'
        when 'CARD' then 'cardiology'
        when 'ORTH' then 'orthopedics'
        when 'RAD'  then 'radiology'
        when 'ER'   then 'emergency_medicine'
        when 'OBGY' then 'obstetrics_gynecology'
    end                                                 as specialty,
    st_cd                                               as state,
    upd_ts                                              as updated_at
from {{ legacy_current_state('prv', ['prv_id']) }}
