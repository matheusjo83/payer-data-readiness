-- Organizations referenced by the other FHIR resources, as CARIN Blue Button
-- Organization resources: the payer (EOB.insurer, Coverage.payor), the legacy
-- providers, which are clinics, and the PBM's pharmacies (EOB.provider). The payer is defined by
-- this project rather than the legacy source, so its lastUpdated is the
-- reference date (var as_of_date), and so is its identifier: Da Vinci HRex, which
-- the PDex prior authorizations reference through the Coverage, requires an
-- identifier on the payer organization. Only NPIs with a valid check digit are exported.
-- An NPI is typed NPI, as CARIN requires, and also PRN (provider number), the
-- code of the base FHIR IdentifierType value set (extensible) that fits it: that
-- value set has no NPI code.
select
    'payer' as id,
    json_object(
        'resourceType', 'Organization',
        'id', 'payer',
        'meta', {{ carin_meta('C4BB-Organization', as_of_date() ~ '::timestamp') }},
        'text', {{ fhir_narrative("'Payer Data Readiness (synthetic), the payer'") }},
        'identifier', json_array(json_object(
            'system', 'https://payer-data-readiness.example/payer-id',
            'value', 'payer')),
        'active', true,
        'name', 'Payer Data Readiness (synthetic)'
    ) as resource

union all

select
    provider_id as id,
    json_merge_patch('{}', json_object(
        'resourceType', 'Organization',
        'id', provider_id,
        'meta', {{ carin_meta('C4BB-Organization', 'updated_at') }},
        'text', {{ fhir_narrative("concat_ws(', ', provider_name, case when npi_valid then 'NPI ' || npi end)") }},
        'identifier', case when npi_valid then json_array(json_object(
            'type', json_object('coding', json_array(
                json_object('system', 'http://terminology.hl7.org/CodeSystem/v2-0203', 'code', 'NPI'),
                json_object('system', 'http://terminology.hl7.org/CodeSystem/v2-0203', 'code', 'PRN'))),
            'system', 'http://hl7.org/fhir/sid/us-npi',
            'value', npi)) end,
        'active', true,
        'name', provider_name,
        'address', case when state is not null then
            json_array(json_object('state', state, 'country', 'US')) end
    )) as resource
from {{ ref('dim_provider') }}

union all

select
    pharmacy_id as id,
    json_merge_patch('{}', json_object(
        'resourceType', 'Organization',
        'id', pharmacy_id,
        'meta', {{ carin_meta('C4BB-Organization', 'updated_at') }},
        'text', {{ fhir_narrative("concat_ws(', ', pharmacy_name, case when npi_valid then 'NPI ' || npi end)") }},
        'identifier', case when npi_valid then json_array(json_object(
            'type', json_object('coding', json_array(
                json_object('system', 'http://terminology.hl7.org/CodeSystem/v2-0203', 'code', 'NPI'),
                json_object('system', 'http://terminology.hl7.org/CodeSystem/v2-0203', 'code', 'PRN'))),
            'system', 'http://hl7.org/fhir/sid/us-npi',
            'value', npi)) end,
        'active', true,
        'name', pharmacy_name,
        'address', case when state is not null then
            json_array(json_object('state', state, 'country', 'US')) end
    )) as resource
from {{ ref('stg_pbm__pharmacies') }}
