-- PBM network pharmacies, with the same NPI check as the legacy providers.
select
    pharmacy_id,
    npi,
    coalesce({{ npi_check_digit_valid('npi') }}, false) as npi_valid,
    trim(pharmacy_name)                                 as pharmacy_name,
    trim(state)                                         as state,
    try_cast(updated_at as timestamp)                   as updated_at
from {{ source('pbm', 'pharmacies') }}
