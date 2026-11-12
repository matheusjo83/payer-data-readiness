-- The attribution list of each provider as a FHIR Group conforming to the Da
-- Vinci PDex Provider Group profile (STU 2.1.0, based on the Da Vinci ATR Group):
-- the members attributed to the provider (provider_attribution), without the
-- ones who opted out, whose number goes in the members-opted-out extension.
-- The Provider Access API exports a list with Group/[id]/$davinci-data-export.
--
--   managingEntity   the payer, which manages the list (PDex: "The payer SHALL
--                    manage the list"); the provider is the characteristic, by
--                    reference and, when valid, NPI.
--   member           the member, the period the relationship was first seen in
--                    the attribution window, the coverage on the reference date
--                    and the attributed provider (ATR extensions). The API adds
--                    the last-transmission extensions when it exports.
--   status           final (ATR attribution list status).
{% set pdex = 'http://hl7.org/fhir/us/davinci-pdex/' %}
{% set atr = 'http://hl7.org/fhir/us/davinci-atr/StructureDefinition/' %}
with members as (
    select
        provider_id,
        min(attribution_window_start) as attribution_window_start,
        count(*) filter (where is_opted_out) as opted_out,
        list(json_object(
            'extension', json_array(
                json_object('url', '{{ atr }}ext-coverageReference',
                            'valueReference', json_object('reference', 'Coverage/cov-' || eligibility_id)),
                json_object('url', '{{ atr }}ext-attributedProvider',
                            'valueReference', json_object('reference', 'Organization/' || provider_id))),
            'entity', json_object('reference', 'Patient/' || member_id),
            'period', json_object('start', strftime(first_service_date, '%Y-%m-%d'))
        ) order by member_id) filter (where not is_opted_out) as members,
        count(*) filter (where not is_opted_out) as attributed
    from {{ ref('provider_attribution') }}
    group by provider_id
)

select
    'attribution-' || p.provider_id as id,
    p.provider_id,
    m.attributed,
    m.opted_out,
    json_merge_patch('{}', json_object(
        'resourceType', 'Group',
        'id', 'attribution-' || p.provider_id,
        'meta', json_object(
            'lastUpdated', strftime({{ as_of_date() }}::timestamp, '%Y-%m-%dT%H:%M:%SZ'),
            'profile', json_array('{{ pdex }}StructureDefinition/pdex-provider-group')),
        'text', {{ fhir_narrative("concat_ws(', ', 'Members attributed to ' || p.provider_name,
            m.attributed || ' members',
            m.opted_out || ' opted out',
            'treatment relationships since ' || strftime(m.attribution_window_start, '%Y-%m-%d'))") }},
        'extension', json_array(
            json_object('url', '{{ atr }}ext-attributionListStatus', 'valueCode', 'final'),
            json_object('url', '{{ pdex }}StructureDefinition/base-ext-members-opted-out',
                        'valueQuantity', json_object('value', m.opted_out))),
        'identifier', case when p.npi_valid then json_array(json_object(
            'system', 'http://hl7.org/fhir/sid/us-npi', 'value', p.npi)) end,
        'active', true,
        'type', 'person',
        'actual', true,
        'code', json_object('coding', json_array(json_object(
            'system', '{{ pdex }}CodeSystem/PdexMemberAttributionCS',
            'code', 'pdexprovidergroup',
            'display', 'PDex Provider Group'))),
        'name', 'Members attributed to ' || p.provider_name,
        'quantity', m.attributed,
        'managingEntity', json_object('reference', 'Organization/payer'),
        'characteristic', json_array(json_object(
            'code', json_object('coding', json_array(json_object(
                'system', '{{ pdex }}CodeSystem/PdexMemberAttributionCS',
                'code', 'pdexprovidergroup',
                'display', 'PDex Provider Group'))),
            'valueReference', json_merge_patch('{}', json_object(
                'reference', 'Organization/' || p.provider_id,
                'identifier', case when p.npi_valid then json_object(
                    'system', 'http://hl7.org/fhir/sid/us-npi', 'value', p.npi) end)),
            'exclude', false,
            'period', json_object('start', strftime(m.attribution_window_start, '%Y-%m-%d')))),
        'member', m.members
    )) as resource
from members m
join {{ ref('dim_provider') }} p using (provider_id)
where m.attributed > 0
