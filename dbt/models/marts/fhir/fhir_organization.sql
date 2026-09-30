-- Organizations referenced by the other FHIR resources, as CARIN Blue Button
-- Organization resources: the payer (EOB.insurer, Coverage.payor) and the
-- legacy providers (EOB.provider), which are clinics. The payer is defined by
-- this project rather than the legacy source, so its lastUpdated is the
-- reference date (var as_of_date). Only NPIs with a valid check digit are exported.
select
    'payer' as id,
    json_object(
        'resourceType', 'Organization',
        'id', 'payer',
        'meta', {{ carin_meta('C4BB-Organization', as_of_date() ~ '::timestamp') }},
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
        'identifier', case when npi_valid then json_array(json_object(
            'type', json_object('coding', json_array(json_object(
                'system', 'http://terminology.hl7.org/CodeSystem/v2-0203',
                'code', 'NPI'))),
            'system', 'http://hl7.org/fhir/sid/us-npi',
            'value', npi)) end,
        'active', true,
        'name', provider_name,
        'address', case when state is not null then
            json_array(json_object('state', state, 'country', 'US')) end
    )) as resource
from {{ ref('dim_provider') }}
