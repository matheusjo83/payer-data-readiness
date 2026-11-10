-- Legacy coverage spans as FHIR R4 Coverage resources conforming to the CARIN
-- Blue Button Coverage profile (STU 2.1.0): beneficiary, relationship, period,
-- payor (the payer Organization in fhir_organization) and plan class. The type
-- is the plan's Source of Payment Typology code (PHDSC/NAHDO SOPT v9.2, the Payer
-- Type value set CARIN binds): 511 Commercial Managed Care - HMO, 512 Commercial
-- Managed Care - PPO, 11 Medicare Managed Care (includes Medicare Advantage), 21
-- Medicaid Managed Care, 23 SCHIP. Nulls are stripped as described in
-- fhir_patient.sql.
select
    'cov-' || e.eligibility_id as id,
    json_merge_patch('{}', json_object(
        'resourceType', 'Coverage',
        'id', 'cov-' || e.eligibility_id,
        'meta', {{ carin_meta('C4BB-Coverage', 'e.updated_at') }},
        'text', {{ fhir_narrative("concat_ws(', ', 'Coverage of member ' || e.member_id,
            p.plan_name || ' (' || p.line_of_business || ')',
            'from ' || strftime(e.coverage_start_date, '%Y-%m-%d'),
            'to ' || strftime(e.coverage_end_date, '%Y-%m-%d'))") }},
        'status', 'active',
        'type', json_merge_patch('{}', json_object(
            'coding', case when s.sopt_code is not null then json_array(json_object(
                'system', 'https://nahdo.org/sopt', 'code', s.sopt_code)) end,
            'text', p.line_of_business)),
        'subscriberId', e.member_id,
        'beneficiary', json_object('reference', 'Patient/' || e.member_id),
        'relationship', json_object('coding', json_array(json_object(
            'system', 'http://terminology.hl7.org/CodeSystem/subscriber-relationship',
            'code', 'self'))),
        'period', json_object(
            'start', strftime(e.coverage_start_date, '%Y-%m-%d'),
            'end', strftime(e.coverage_end_date, '%Y-%m-%d')),
        'payor', json_array(json_object('reference', 'Organization/payer')),
        'class', json_array(json_merge_patch('{}', json_object(
            'type', json_object('coding', json_array(json_object(
                'system', 'http://terminology.hl7.org/CodeSystem/coverage-class',
                'code', 'plan'))),
            'value', e.plan_id,
            'name', p.plan_name)))
    )) as resource
from {{ ref('stg_legacy__eligibility') }} e
left join {{ ref('stg_legacy__plans') }} p using (plan_id)
left join (
    select plan_id,
           case
               when line_of_business = 'commercial' and plan_name ilike '%HMO%' then '511'
               when line_of_business = 'commercial' and plan_name ilike '%PPO%' then '512'
               when line_of_business = 'medicare_advantage' then '11'
               when line_of_business = 'medicaid' then '21'
               when line_of_business = 'chip' then '23'
           end as sopt_code
    from {{ ref('stg_legacy__plans') }}
) s using (plan_id)
