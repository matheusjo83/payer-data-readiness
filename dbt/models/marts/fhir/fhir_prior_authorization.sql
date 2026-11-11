-- Legacy prior authorization requests as FHIR R4 ExplanationOfBenefit resources
-- conforming to the Da Vinci PDex Prior Authorization profile (STU 2.1.0, the
-- version CMS lists for the Patient Access API that is not derived from expired
-- standards). use = preauthorization tells them apart from the claims.
--
--   decision    item.adjudication.extension:reviewAction, code from X12 306
--               (Health Care Services Review Decision): A1 Certified in total
--               (approved) and A4 Pending, as in the PDex and PAS examples; A3
--               Not Certified (denied), which no open source shows (X12 code
--               lists are licensed). The decision time is base-ext-when-adjudicated.
--   amount      the requested amount, as the 'submitted' adjudication the
--               profile requires next to the review action.
--   urgency     priority: stat for an expedited request, normal otherwise
--               (process-priority, an HL7 code system).
--   outcome     queued while pending, complete once decided.
--
-- The profile is declared without a version: its meta.profile slice requires
-- the canonical URL as is. Only requests with a known member, a coverage span
-- on the request date and a known provider are exported.
{% set pdex = 'http://hl7.org/fhir/us/davinci-pdex/StructureDefinition/' %}
with exported as (
    select
        a.*,
        e.eligibility_id,
        case a.decision
            when 'approved' then 'A1'
            when 'denied' then 'A3'
            when 'pending' then 'A4'
        end as review_action_code,
        case a.decision
            when 'approved' then 'Certified in total'
            when 'denied' then 'Not Certified'
            when 'pending' then 'Pending'
        end as review_action_display
    from {{ ref('stg_legacy__prior_auths') }} a
    join {{ ref('dim_member') }} m using (member_id)
    join {{ ref('dim_provider') }} p using (provider_id)
    join {{ ref('stg_legacy__eligibility') }} e
        on e.member_id = a.member_id
       and a.requested_at::date between e.coverage_start_date and coalesce(e.coverage_end_date, date '9999-12-31')
    qualify row_number() over (partition by a.prior_auth_id order by e.coverage_start_date desc, e.eligibility_id) = 1
)

select
    prior_auth_id as id,
    json_merge_patch('{}', json_object(
        'resourceType', 'ExplanationOfBenefit',
        'id', prior_auth_id,
        'meta', json_object(
            'lastUpdated', strftime(updated_at, '%Y-%m-%dT%H:%M:%SZ'),
            'profile', json_array('{{ pdex }}pdex-priorauthorization')),
        'text', {{ fhir_narrative("concat_ws(', ', 'Prior authorization ' || prior_auth_id,
            'member ' || member_id,
            'service ' || service_code,
            case when is_expedited then 'expedited' else 'standard' end,
            'requested ' || strftime(requested_at, '%Y-%m-%d'),
            decision || coalesce(' on ' || strftime(decided_at, '%Y-%m-%d'), ''))") }},
        'identifier', json_array(json_object(
            'system', 'https://payer-data-readiness.example/prior-auth-id',
            'value', prior_auth_id)),
        'status', 'active',
        'type', json_object('coding', json_array(json_object(
            'system', 'http://terminology.hl7.org/CodeSystem/claim-type',
            'code', 'professional'))),
        'use', 'preauthorization',
        'patient', json_object('reference', 'Patient/' || member_id),
        'created', strftime(requested_at, '%Y-%m-%dT%H:%M:%SZ'),
        'insurer', json_object('reference', 'Organization/payer'),
        'provider', json_object('reference', 'Organization/' || provider_id),
        'priority', json_object('coding', json_array(json_object(
            'system', 'http://terminology.hl7.org/CodeSystem/processpriority',
            'code', case when is_expedited then 'stat' else 'normal' end))),
        'outcome', case when decision = 'pending' then 'queued' else 'complete' end,
        'insurance', json_array(json_object(
            'focal', true,
            'coverage', json_object('reference', 'Coverage/cov-' || eligibility_id))),
        'item', json_array(json_object(
            'sequence', 1,
            'productOrService', json_object('coding', json_array(json_object(
                'system', 'http://www.ama-assn.org/go/cpt',
                'code', service_code))),
            'adjudication', json_array(json_merge_patch('{}', json_object(
                'extension', list_concat(
                    [json_object(
                        'url', '{{ pdex }}extension-reviewAction',
                        'extension', json_array(json_object(
                            'url', '{{ pdex }}extension-reviewActionCode',
                            'valueCodeableConcept', json_object('coding', json_array(json_object(
                                'system', 'https://codesystem.x12.org/005010/306',
                                'code', review_action_code,
                                'display', review_action_display))))))],
                    case when decided_at is not null then [json_object(
                        'url', '{{ pdex }}base-ext-when-adjudicated',
                        'valueDateTime', strftime(decided_at, '%Y-%m-%dT%H:%M:%SZ'))] else [] end),
                'category', json_object('coding', json_array(json_object(
                    'system', 'http://terminology.hl7.org/CodeSystem/adjudication',
                    'code', 'submitted'))),
                'amount', json_object('value', requested_amount, 'currency', 'USD'))))))
    )) as resource
from exported
