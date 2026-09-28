-- Hand-written flattening of FHIR Patient. Phase 4 will compare this with the
-- equivalent SQL on FHIR ViewDefinition (fhir/view_definitions/).
select
    json_extract_string(resource, '$.id')                        as patient_id,
    json_extract_string(resource, '$.gender')                    as gender,
    try_cast(json_extract_string(resource, '$.birthDate') as date) as birth_date,
    json_extract_string(resource, '$.address[0].state')          as state,
    json_extract_string(resource, '$.address[0].postalCode')     as postal_code,
    _loaded_at
from {{ source('fhir', 'patient') }}
