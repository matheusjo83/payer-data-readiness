-- Legacy members as FHIR R4 Patient resources, aligned with the CARIN Blue
-- Button Patient profile (identifier, name, gender, birthDate, address).
-- Legacy gender codes outside M/F/U are omitted rather than guessed.
--
-- FHIR JSON may not contain nulls. json_merge_patch('{}', obj) drops null keys
-- (RFC 7386) through nested objects, but not inside arrays, so each object that
-- goes into an array is stripped on its own.
select
    member_id as id,
    json_merge_patch('{}', json_object(
        'resourceType', 'Patient',
        'id', member_id,
        'identifier', json_array(json_object(
            'system', 'https://payer-data-readiness.example/member-id',
            'value', member_id)),
        'name', case when first_name is not null or last_name is not null then
            json_array(json_merge_patch('{}', json_object(
                'family', last_name,
                'given', case when first_name is not null then json_array(first_name) end)))
        end,
        'gender', case when gender in ('male', 'female', 'unknown') then gender end,
        'birthDate', strftime(birth_date, '%Y-%m-%d'),
        'address', json_array(json_merge_patch('{}', json_object(
            'postalCode', zip_code,
            'state', state,
            'country', 'US')))
    )) as resource
from {{ ref('dim_member') }}
