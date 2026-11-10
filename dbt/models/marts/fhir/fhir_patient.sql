-- Legacy members as FHIR R4 Patient resources conforming to the CARIN Blue
-- Button Patient profile (STU 2.1.0). Legacy gender codes outside M/F/U become
-- 'unknown': the profile requires a gender, and FHIR's 'unknown' states that
-- it is not known instead of guessing one. A member linked to PBM cardholders
-- (gold.member_xref) also carries each cardholder ID as a member identifier: a
-- person can hold more than one (gold.pbm_persons).
--
-- FHIR JSON may not contain nulls. json_merge_patch('{}', obj) drops null keys
-- (RFC 7386) through nested objects, but not inside arrays, so each object that
-- goes into an array is stripped on its own.
select
    m.member_id as id,
    json_merge_patch('{}', json_object(
        'resourceType', 'Patient',
        'id', m.member_id,
        'meta', {{ carin_meta('C4BB-Patient', 'm.updated_at') }},
        'identifier', list_concat(
            [{{ member_identifier("'https://payer-data-readiness.example/member-id'", 'm.member_id') }}],
            coalesce(x.cardholder_identifiers, [])),
        'name', case when first_name is not null or last_name is not null then
            json_array(json_merge_patch('{}', json_object(
                'family', last_name,
                'given', case when first_name is not null then json_array(first_name) end)))
        end,
        'gender', case when gender in ('male', 'female') then gender else 'unknown' end,
        'birthDate', strftime(birth_date, '%Y-%m-%d'),
        'address', json_array(json_merge_patch('{}', json_object(
            'postalCode', zip_code,
            'state', state,
            'country', 'US')))
    )) as resource
from {{ ref('dim_member') }} m
left join (
    select member_id,
           list({{ member_identifier("'https://pbm.example/cardholder-id'", 'cardholder_id') }}
                order by cardholder_id) as cardholder_identifiers
    from {{ ref('member_xref') }}
    where member_id is not null
    group by member_id
) x on x.member_id = m.member_id
