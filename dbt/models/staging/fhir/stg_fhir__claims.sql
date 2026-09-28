select
    json_extract_string(resource, '$.id')                            as claim_id,
    regexp_replace(json_extract_string(resource, '$.patient.reference'), '^(urn:uuid:|Patient/)', '') as patient_ref,
    json_extract_string(resource, '$.status')                        as status,
    json_extract_string(resource, '$.type.coding[0].code')           as claim_type,
    try_cast(json_extract_string(resource, '$.billablePeriod.start') as timestamp) as billable_start,
    try_cast(json_extract_string(resource, '$.total.value') as decimal(12, 2))    as total_value,
    _loaded_at
from {{ source('fhir', 'claim') }}
